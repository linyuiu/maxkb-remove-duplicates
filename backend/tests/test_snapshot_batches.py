"""Read/write batching and atomic counters in disposable PostgreSQL."""

from datetime import datetime, timedelta, timezone
import json
import os
import struct
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from test_source_operations import connection

from app.result_storage import models_for_run
Pair = models_for_run("run").Pair
ParagraphSnapshot = models_for_run("run").ParagraphSnapshot

pytestmark = pytest.mark.skipif(
    os.environ.get("DEDUP_DISPOSABLE_TEST_DB") != "1",
    reason="Requires disposable test socket server",
)


class SourceStream:
    def __init__(self, count, *, paragraphs=None, content=None, on_row=None):
        self.count = count
        self.paragraphs = paragraphs if paragraphs is not None else count
        self.content = content
        self.on_row = on_row
        self.vectors_read = self.paragraphs_read = 0
        self.closed = []

    def execute(self, query, params):
        return SimpleNamespace(fetchall=lambda: [{"workspace_id": "default"}])

    def rows(self, conn, query, params, columns, types):
        vector = "embedding_id" in columns
        try:
            for i in range(self.count if vector else self.paragraphs):
                paragraph = i % self.paragraphs
                document_id = f"document-{paragraph // 7}"
                if vector:
                    self.vectors_read += 1
                    row = dict(
                        embedding_id=f"embedding-{i}", vector=struct.pack("!HHff", 2, 0, 1, 1),
                        paragraph_id=f"paragraph-{paragraph}", document_id=document_id,
                        knowledge_id="knowledge", p_document_id=document_id,
                        p_knowledge_id="knowledge", d_knowledge_id="knowledge", has_content=True,
                    )
                else:
                    self.paragraphs_read += 1
                    body = self.content(i) if self.content else f"snapshot text {paragraph}"
                    row = dict(
                        paragraph_id=f"paragraph-{paragraph}", document_id=document_id,
                        knowledge_id="knowledge", workspace_id="default", title="title", content=body,
                        document_name="Document", knowledge_name="Knowledge",
                        update_time=str(datetime(2026, 11, 11, tzinfo=timezone.utc)),
                        payload_bytes=len(body.encode()) + 64,
                    )
                if self.on_row:
                    self.on_row(self, vector)
                yield row
                del row
        finally:
            self.closed.append("vector" if vector else "paragraph")


@pytest.fixture
def snapshot_db(connection, monkeypatch):
    from app import db, scanning
    from app.models import DataSource, Task, Run, now

    schema = connection.exec_driver_sql("SHOW search_path").scalar()
    connection.commit()
    engine = create_engine(connection.engine.url,connect_args={"options":f"-c search_path={schema}"})
    monkeypatch.setattr(db,"engine",engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db, "Session", sessions)
    monkeypatch.setattr(scanning, "check_models", lambda *args: None)
    monkeypatch.setattr(scanning, "check_vector_readiness", lambda *args: None)
    monkeypatch.setattr(scanning, "workspace_names", lambda *args: {"default": "Default"})
    clock = [0.0]
    monkeypatch.setattr(scanning, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    with sessions.begin() as s:
        s.add(DataSource(id="source", name="source", connection="not used"))
        s.flush()
        s.add(Task(id="task", name="task", source_id="source", knowledge_ids=["knowledge"]))
        s.flush()
        s.add(Run(id="run", task_id="task", state="running", token="token",
                  lease_until=now() + timedelta(minutes=3),
                  config={"knowledge_ids": ["knowledge"], "exclude_same_document": False}))
    inserts, totals = [], []

    from app.bulk_writer import BulkWriter
    original_copy,original_guard = BulkWriter._copy,BulkWriter._guard
    def record_copy(writer,kind,rows):
        if kind=="snapshots":
            inserts.append(dict(size=len(rows),inserted=len(rows),
                payload_bytes=sum(len(json.dumps(row[1],ensure_ascii=True))+256 for row in rows)))
        return original_copy(writer,kind,rows)
    def record_guard(writer,seq,key,delta):
        result=original_guard(writer,seq,key,delta)
        if delta["kind"]=="snapshots":
            totals.append((totals[-1] if totals else 0)+delta["chunks"])
        return result
    monkeypatch.setattr(BulkWriter,"_copy",record_copy)
    monkeypatch.setattr(BulkWriter,"_guard",record_guard)
    monkeypatch.setattr(scanning, "snapshot_rows", None)
    yield SimpleNamespace(sessions=sessions, scanning=scanning, connection=connection,
                          inserts=inserts, totals=totals, clock=clock)
    engine.dispose()


def run_snapshot(env, cursor):
    from app.matrix import VectorStore
    env.scanning.snapshot_rows = cursor.rows
    source = cursor
    with VectorStore() as store:
        env.scanning.take_snapshot("run", "token", source, store)
        assert store.count == cursor.count
    return source


def test_reads_1000_and_writes_5000_with_tail(snapshot_db):
    from app.models import Run
    env = snapshot_db
    cursor = SourceStream(10037)
    run_snapshot(env, cursor)
    assert cursor.vectors_read == cursor.paragraphs_read == 10037
    assert [batch["size"] for batch in env.inserts] == [5000, 5000, 37]
    assert env.totals == [5000, 10000, 10037]
    assert cursor.closed == ["vector", "paragraph"]
    with env.sessions() as s:
        run = s.get(Run, "run")
        assert run.total == 10037 and run.snapshot_ready and run.phase == "compare"
        assert s.scalar(select(func.count()).select_from(ParagraphSnapshot)) == 10037


def test_duplicate_paragraphs_count_chunks_and_keep_one_snapshot(snapshot_db):
    from app.models import Run
    env = snapshot_db
    run_snapshot(env, SourceStream(6001, paragraphs=17))
    assert env.totals == [5295, 6001]
    assert [batch["size"] for batch in env.inserts] == [15, 2]
    assert [batch["inserted"] for batch in env.inserts] == [15, 2]
    with env.sessions() as s:
        assert s.get(Run, "run").total == 6001
        assert s.scalar(select(func.count()).select_from(ParagraphSnapshot)) == 17


def test_empty_snapshot_has_no_write_batch(snapshot_db):
    from app.models import Run
    env = snapshot_db
    run_snapshot(env, SourceStream(0))
    assert env.inserts == env.totals == []
    with env.sessions() as s:
        run = s.get(Run, "run")
        assert run.total == 0 and run.snapshot_ready


def test_large_text_flushes_on_bytes_and_sends_single_oversized_row(snapshot_db, monkeypatch):
    from app.models import Run
    env = snapshot_db
    monkeypatch.setattr(env.scanning, "SNAPSHOT_BUFFER_BYTES", 20_000)
    def content(i):
        return "超大正文" * 10000 if i == 9 else "正文" * 100
    run_snapshot(env, SourceStream(30, content=content))
    assert len(env.inserts) > 2
    assert all(batch["payload_bytes"] <= 20_000 or batch["size"] == 1 for batch in env.inserts)
    with env.sessions() as s:
        assert s.get(Run, "run").total == 30
        snapshot = s.scalar(select(ParagraphSnapshot).where(ParagraphSnapshot.paragraph_id == "paragraph-9"))
        assert snapshot.data["content"] == content(9)


@pytest.mark.parametrize("count", [5000, 6001])
def test_failed_counter_update_rolls_back_snapshot_rows(snapshot_db, monkeypatch, count):
    from app.models import Run
    from app.bulk_writer import BulkWriter
    env = snapshot_db
    cursor = SourceStream(count)
    original=BulkWriter._guard
    def fail(writer,seq,key,delta):
        if seq==(1 if count==5000 else 2):
            raise RuntimeError("injected counter failure")
        return original(writer,seq,key,delta)
    monkeypatch.setattr(BulkWriter,"_guard",fail)
    with pytest.raises(RuntimeError, match="injected counter failure"):
        run_snapshot(env, cursor)
    with env.sessions() as s:
        run = s.get(Run, "run")
        committed = 0 if count == 5000 else 5000
        assert run.total == committed and not run.snapshot_ready
        assert s.scalar(select(func.count()).select_from(ParagraphSnapshot)) == committed
    assert cursor.closed == ["vector", "paragraph"]


@pytest.mark.parametrize("cancel", [True, False])
def test_cancellation_and_expired_lease_stop_before_full_write_batch(snapshot_db, cancel):
    from app.models import Run, now
    env = snapshot_db
    def stop(cursor, vector):
        if vector and cursor.vectors_read == 2000:
            with env.sessions.begin() as s:
                run = s.get(Run, "run")
                if cancel:
                    run.cancel_requested = True
                else:
                    run.lease_until = now() - timedelta(seconds=1)
            env.clock[0] = 3.0
    cursor = SourceStream(6000, on_row=stop)
    with pytest.raises(env.scanning.Stopped, match="用户取消" if cancel else "租约"):
        run_snapshot(env, cursor)
    assert env.inserts == env.totals == []
    assert cursor.vectors_read == 2000 and cursor.closed == ["vector"]
    with env.sessions() as s:
        assert s.get(Run, "run").total == 0
        assert s.scalar(select(func.count()).select_from(ParagraphSnapshot)) == 0


def test_oversized_paragraph_fails_without_marking_snapshot_ready(snapshot_db, monkeypatch):
    from app.models import Run
    env = snapshot_db
    monkeypatch.setattr(env.scanning, "SNAPSHOT_MAX_PARAGRAPH_BYTES", 1000)
    cursor = SourceStream(10, content=lambda i: "正文" * 500)
    with pytest.raises(ValueError, match="读取上限"):
        run_snapshot(env, cursor)
    assert cursor.vectors_read == 10 and cursor.paragraphs_read == 1
    assert cursor.closed == ["vector", "paragraph"]
    with env.sessions() as s:
        assert not s.get(Run, "run").snapshot_ready
        assert s.scalar(select(func.count()).select_from(ParagraphSnapshot)) == 0


def test_copy_precedes_transaction_tail_guard(snapshot_db, monkeypatch):
    from app.bulk_writer import BulkWriter
    ordering=[]
    original_copy,original_guard=BulkWriter._copy,BulkWriter._guard
    def copy(writer,kind,rows):
        ordering.append("copy")
        return original_copy(writer,kind,rows)
    def guard(writer,seq,key,delta):
        ordering.append("guard")
        return original_guard(writer,seq,key,delta)
    monkeypatch.setattr(BulkWriter,"_copy",copy)
    monkeypatch.setattr(BulkWriter,"_guard",guard)
    run_snapshot(snapshot_db, SourceStream(5001))
    assert ordering==["copy","guard","copy","guard"]


@pytest.mark.parametrize("cancel", [True, False])
def test_final_fence_rejects_slow_write_and_rolls_back_batch(snapshot_db, monkeypatch, cancel):
    from app.models import Run, now
    from app.bulk_writer import BulkWriter
    env = snapshot_db
    original=BulkWriter._guard
    def reject(writer,seq,key,delta):
        with env.sessions.begin() as s:
            run=s.get(Run,"run")
            if cancel:
                run.cancel_requested=True
            else:
                run.lease_until=now()-timedelta(seconds=1)
        return original(writer,seq,key,delta)
    monkeypatch.setattr(BulkWriter,"_guard",reject)
    with pytest.raises(env.scanning.Stopped, match="用户取消" if cancel else "租约"):
        run_snapshot(env, SourceStream(5000))
    with env.sessions() as s:
        run=s.get(Run,"run")
        assert run.total==0 and not run.snapshot_ready
        assert s.scalar(select(func.count()).select_from(ParagraphSnapshot))==0


def test_reading_and_writing_long_paragraphs_keeps_a_bounded_buffer(snapshot_db, monkeypatch):
    import tracemalloc
    env = snapshot_db
    monkeypatch.setattr(env.scanning, "SNAPSHOT_BUFFER_BYTES", 256 * 1024)
    source = SourceStream(2000, content=lambda i: f"{i}:" + "x" * 10000)
    tracemalloc.start()
    try:
        run_snapshot(env, source)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 8 * 1024**2
    assert max(batch["payload_bytes"] for batch in env.inserts) <= 256 * 1024
    assert source.vectors_read == source.paragraphs_read == 2000


def test_profiled_snapshot_keeps_rows_counts_and_records_transactions(snapshot_db):
    from app.profiling import RunTiming
    from app.models import Run
    env = snapshot_db
    profile = RunTiming("run")
    with profile.activate():
        run_snapshot(env, SourceStream(20, paragraphs=4))
    profile.finish()
    keys = {row["key"] for row in profile.snapshot()["stages"]}
    assert {"vector_read", "vector_store", "paragraph_read", "snapshot_write", "connection", "flush", "commit"} <= keys
    assert profile.counters["snapshot_chunks"] == 20
    assert profile.counters["snapshot_paragraphs"] == 4
    with env.sessions() as s:
        run = s.get(Run, "run")
        assert run.snapshot_ready and run.total == 20
        assert run.timing.data["run_id"] == "run"


def test_profiled_result_failure_rolls_back_rows_and_committed_counters(snapshot_db, monkeypatch):
    from app.models import Run
    from app.profiling import RunTiming
    from app.bulk_writer import BulkWriter
    env = snapshot_db
    profile = RunTiming("run")
    def fail(*args):
        raise RuntimeError("injected profiled counter failure")
    monkeypatch.setattr(BulkWriter,"_guard",fail)
    with profile.activate(), pytest.raises(RuntimeError, match="profiled counter failure"):
        env.scanning.save_matches("run", "token", [dict(
            left_id="left", right_id="right", similarity=0.95,
            left_embedding_id="e1", right_embedding_id="e2",
        )])
    with env.sessions() as s:
        assert s.get(Run,"run").pair_count==0
        assert s.scalar(select(func.count()).select_from(Pair))==0
    assert profile.counters.get("inserted_pairs",0)==0
    assert any(stage["key"]=="rollback" for stage in profile.snapshot()["stages"])
