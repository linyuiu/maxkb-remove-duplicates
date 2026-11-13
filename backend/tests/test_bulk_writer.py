"""COPY ACK recovery and bounded result buffering against disposable PG 18."""

from datetime import timedelta
import threading
import os
from types import SimpleNamespace

import psycopg
import pytest
from sqlalchemy import func, select

from app.bulk_writer import BulkWriter, RESULT_BATCH_ROWS, StopSignal, Stopped
from app.models import Pair, ParagraphSnapshot, Run, now
from app.profiling import RunTiming, count, measure
from app.result_pipeline import ResultPipeline, row_bytes
from test_snapshot_batches import snapshot_db
from test_source_operations import connection

pytestmark=pytest.mark.skipif(os.environ.get("DEDUP_DISPOSABLE_TEST_DB")!="1",reason="Requires disposable PostgreSQL 18")


def match(index=0, score=0.95, embedding="e1"):
    return dict(left_id=f"left-{index:06}",right_id=f"right-{index:06}",similarity=score,
                left_embedding_id=embedding,right_embedding_id="e2")


def test_copy_preserves_id_status_and_highest_score_with_deterministic_ties(snapshot_db):
    with BulkWriter("run","token") as writer:
        assert writer.results([match(),match(score=0.9)])["inserted"]==1
        with snapshot_db.sessions.begin() as s:
            pair=s.scalar(select(Pair))
            pair.status="ignored"
            pair_id=pair.id
        assert writer.results([match(score=0.99,embedding="z")])["updated"]==1
        assert writer.results([match(score=0.99,embedding="a")])["updated"]==1
        assert writer.results([match(score=0.99,embedding="b"),match(score=0.9)])["unchanged"]==1
    with snapshot_db.sessions() as s:
        pair=s.scalar(select(Pair))
        run=s.get(Run,"run")
        assert (pair.id,pair.status,pair.similarity,pair.left_embedding_id)==(pair_id,"ignored",0.99,"a")
        assert (run.pair_count,run.result_submitted,run.result_improved,run.result_unchanged)==(1,4,2,1)
        assert run.batch_seq==4


@pytest.mark.parametrize("after_commit",[False,True])
@pytest.mark.parametrize("kind",["results","snapshots"])
def test_unknown_commit_retries_or_acknowledges_once(snapshot_db, monkeypatch, after_commit, kind):
    original=BulkWriter._commit
    attempts=[]
    def lost(writer):
        if not attempts:
            attempts.append(writer.conn.info.backend_pid)
            if after_commit:
                original(writer)
            raise psycopg.OperationalError("injected lost commit acknowledgement")
        return original(writer)
    monkeypatch.setattr(BulkWriter,"_commit",lost)
    profile=RunTiming("run")
    with profile.activate(),BulkWriter("run","token") as writer:
        if kind=="results":
            delta=writer.results([match()])
            assert delta["inserted"]==1
        else:
            delta=writer.snapshots([("p",{"content":"正文"},7)])
            assert delta["chunks"]==7
        assert writer.seq==1 and writer.conn.info.backend_pid!=attempts[0]
    with snapshot_db.sessions() as s:
        run=s.get(Run,"run")
        assert run.batch_seq==1
        assert (run.pair_count,run.result_submitted,run.total)==((1,1,0) if kind=="results" else (0,0,7))
        assert s.scalar(select(func.count()).select_from(Pair))==(1 if kind=="results" else 0)
        assert s.scalar(select(func.count()).select_from(ParagraphSnapshot))==(0 if kind=="results" else 1)
    batch=profile.snapshot()["batch_series"][0]
    assert batch["attempts"]==2 and batch["status"]=="committed"
    assert batch["recovered"] is after_commit
    assert batch["copy_seconds"]>0 and batch["merge_seconds"]>0
    assert profile.counters["reconnect_attempts"]==1
    assert profile.counters["inserted_pairs" if kind=="results" else "snapshot_chunks"]==(1 if kind=="results" else 7)


@pytest.mark.parametrize("change",["token","cancel","expire"])
def test_tail_guard_rolls_back_results_when_ownership_changes(snapshot_db, monkeypatch, change):
    original=BulkWriter._guard
    def rejected(writer,seq,key,delta):
        with snapshot_db.sessions.begin() as s:
            run=s.get(Run,"run")
            if change=="token":
                run.token="new-owner"
            elif change=="cancel":
                run.cancel_requested=True
            else:
                run.lease_until=now()-timedelta(seconds=1)
        return original(writer,seq,key,delta)
    monkeypatch.setattr(BulkWriter,"_guard",rejected)
    with BulkWriter("run","token") as writer,pytest.raises(Stopped):
        writer.results([match()])
    with snapshot_db.sessions() as s:
        run=s.get(Run,"run")
        assert (run.batch_seq,run.pair_count,run.result_submitted)==(0,0,0)
        assert s.scalar(select(func.count()).select_from(Pair))==0


def test_mismatched_acknowledgement_stops_instead_of_double_counting(snapshot_db,monkeypatch):
    original=BulkWriter._commit
    def changed(writer):
        original(writer)
        with snapshot_db.sessions.begin() as s:
            s.get(Run,"run").batch_key="different-batch"
        raise psycopg.OperationalError("injected missing ack")
    monkeypatch.setattr(BulkWriter,"_commit",changed)
    with BulkWriter("run","token") as writer,pytest.raises(Stopped,match="内容不一致"):
        writer.results([match()])
    with snapshot_db.sessions() as s:
        assert s.get(Run,"run").pair_count==1
        assert s.scalar(select(func.count()).select_from(Pair))==1


def test_50000_result_batch_and_tail_are_fully_committed(snapshot_db,monkeypatch):
    sizes=[]
    original=BulkWriter._copy
    def record(writer,kind,rows):
        sizes.append(len(rows))
        return original(writer,kind,rows)
    monkeypatch.setattr(BulkWriter,"_copy",record)
    profile=RunTiming("run")
    with profile.activate(),BulkWriter("run","token") as writer:
        with ResultPipeline(writer) as pipeline:
            pipeline.submit(match(i) for i in range(50003))
            pipeline.checkpoint(50003,2)
    assert RESULT_BATCH_ROWS==50000 and sizes==[50000,3]
    series=profile.snapshot()["batch_series"]
    assert [row["rows"] for row in series]==[50000,3]
    assert [row["batch_no"] for row in series]==[1,2]
    assert all(row["copy_seconds"]>0 and row["merge_seconds"]>0 for row in series)
    with snapshot_db.sessions() as s:
        run=s.get(Run,"run")
        assert run.pair_count==run.result_submitted==50003
        assert run.batch_seq==2 and run.comparisons_done==50003
        assert s.scalar(select(func.count()).select_from(Pair))==50003


def test_completion_acknowledges_lost_commit_only_after_all_results(snapshot_db,monkeypatch):
    with snapshot_db.sessions.begin() as s:
        run=s.get(Run,"run")
        run.snapshot_ready=True
        run.comparisons_total=1
        run.total=2
    with BulkWriter("run","token") as writer:
        with ResultPipeline(writer) as pipeline:
            pipeline.submit([match()])
            pipeline.checkpoint(1,2)
        original=writer._commit
        calls=[]
        def lost():
            original()
            calls.append(True)
            raise psycopg.OperationalError("injected lost final ack")
        monkeypatch.setattr(writer,"_commit",lost)
        writer.finish(1,2)
        assert len(calls)==1
    with snapshot_db.sessions() as s:
        run=s.get(Run,"run")
        assert run.state=="succeeded" and run.pair_count==1 and run.comparisons_done==1


def test_progress_has_one_five_second_throttle_and_does_not_rewrite_config(snapshot_db,monkeypatch):
    import app.bulk_writer as module
    clock=[100.0]
    monkeypatch.setattr(module,"time",SimpleNamespace(monotonic=lambda:clock[0]))
    with BulkWriter("run","token") as writer:
        assert writer.progress(1,1)
        clock[0]+=4.9
        assert not writer.progress(5,2)
        clock[0]+=0.1
        assert writer.progress(6,3)
    with snapshot_db.sessions() as s:
        run=s.get(Run,"run")
        assert run.comparisons_done==6 and run.processed==3
        assert "processed_comparisons" not in run.config


class MemoryWriter:
    def __init__(self,fail=False):
        self.signal=StopSignal()
        self.rows=[]
        self.fail=fail
        self.received=threading.Event()
        self.release=threading.Event()
        self.progress_rows=[]
    def results(self,rows):
        rows=list(rows)
        self.received.set()
        if self.fail:
            raise ValueError("injected writer failure")
        self.rows.extend(rows)
    def progress(self,completed,processed,force=False):
        self.progress_rows.append((completed,processed,force))
    def metrics(self,profile):
        pass


def test_small_byte_budget_applies_backpressure_without_losing_tail():
    writer=MemoryWriter()
    size=row_bytes(tuple(match().values()))
    with ResultPipeline(writer,buffer_bytes=size*3) as pipeline:
        pipeline.submit(match(i) for i in range(103))
        pipeline.checkpoint(103,14)
        assert pipeline.used<=pipeline.limit
    assert len(writer.rows)==103
    assert writer.progress_rows[-1]==(103,14,True)


def test_writer_failure_wakes_producer_and_never_reports_completion():
    writer=MemoryWriter(fail=True)
    with pytest.raises(ValueError,match="writer failure"):
        with ResultPipeline(writer,batch_rows=1,buffer_bytes=4096) as pipeline:
            pipeline.submit(match(i) for i in range(1000))
    assert not pipeline.thread.is_alive()
    assert not any(force for _,_,force in writer.progress_rows)


def test_parallel_timing_keeps_each_thread_tree_and_merges_counts():
    profile=RunTiming("run")
    writer=MemoryWriter()
    with profile.activate(),measure("compare","比较"):
        with ResultPipeline(writer,batch_rows=2) as pipeline:
            pipeline.submit([match(0),match(1),match(2)])
            count("main_count",3)
    profile.finish()
    data=profile.snapshot()
    assert data["parallel"] and data["final"]
    assert len([s for s in data["stages"] if s["parent_id"] is None])==2
    assert data["counters"]["main_count"]==3
    assert all(s["self_seconds"]>=0 for s in data["stages"])


def test_cancellation_interrupts_in_flight_writer_query(snapshot_db,monkeypatch):
    started,finished=threading.Event(),threading.Event()
    errors=[]
    original=BulkWriter._copy
    def stalled(writer,kind,rows):
        started.set()
        writer.conn.execute("SELECT pg_sleep(10)")
        return original(writer,kind,rows)
    monkeypatch.setattr(BulkWriter,"_copy",stalled)
    with BulkWriter("run","token") as writer:
        def write():
            try:
                writer.results([match()])
            except Exception as exc:
                errors.append(exc)
            finally:
                finished.set()
        thread=threading.Thread(target=write)
        thread.start()
        assert started.wait(2)
        writer.signal.stop(Stopped("用户取消"))
        assert finished.wait(2)
        thread.join(timeout=2)
        assert len(errors)==1 and isinstance(errors[0],Stopped)
    with snapshot_db.sessions() as s:
        assert s.get(Run,"run").pair_count==0
        assert s.scalar(select(func.count()).select_from(Pair))==0
