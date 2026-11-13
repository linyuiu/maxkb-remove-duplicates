"""Timing series, honest cumulative-stat deltas, and pool setting restoration."""

from copy import deepcopy
from contextlib import contextmanager
import json
import os
from types import SimpleNamespace

import pytest
from psycopg.pq import TransactionStatus

from app import db
from app.bulk_writer import BulkWriter
from app.profiling import BATCH_SERIES_LIMIT, RunTiming
from app.scan_diagnostics import BatchTiming, capture_index_stats, index_delta
from test_profiling import Clock
from test_snapshot_batches import snapshot_db
from test_source_operations import connection

requires_db=pytest.mark.skipif(os.environ.get("DEDUP_DISPOSABLE_TEST_DB")!="1",reason="Requires disposable PostgreSQL 18")


def test_batch_times_are_relative_to_scan_and_match_phases(caplog):
    clock=Clock()
    profile=RunTiming("run",clock)
    clock.advance(10)
    with profile.activate():
        child=profile.worker()
        with child.activate(),BatchTiming("run","results",26,50000) as batch:
            batch.value["attempts"]=1
            with batch.phase("copy"):clock.advance(1.2)
            with batch.phase("merge"):clock.advance(2.4)
            with batch.phase("commit"):clock.advance(0.1)
            batch.confirm(dict(inserted=49000,updated=500,unchanged=500))
    value=profile.snapshot()["batch_series"][0]
    assert value["started_seconds"]==10 and value["finished_seconds"]==pytest.approx(13.7)
    assert value["seconds"]==pytest.approx(3.7)
    assert value["copy_seconds"]==pytest.approx(1.2) and value["merge_seconds"]==pytest.approx(2.4)
    assert (value["rows"],value["batch_no"],value["batch_seq"],value["status"])==(50000,1,26,"committed")
    assert profile.snapshot()["batch_series_total"]==1
    value["rows"]=0
    assert profile.snapshot()["batch_series"][0]["rows"]==50000


def test_unconfirmed_batch_records_duration_and_only_error_type(caplog):
    caplog.set_level("INFO",logger="uvicorn.error")
    profile=RunTiming("run")
    with profile.activate(),pytest.raises(RuntimeError):
        with BatchTiming("run","results",1,25) as batch:
            with batch.phase("copy"):
                raise RuntimeError("a-sensitive-value")
    value=profile.snapshot()["batch_series"][0]
    assert value["status"]=="unconfirmed" and value["error_type"]=="RuntimeError"
    assert value["copy_seconds"]>0 and value["merge_seconds"]==0
    assert "a-sensitive-value" not in caplog.text
    assert "scan_batch_timing" in caplog.text
    assert not profile.counters


def test_series_memory_is_bounded_and_keeps_first_and_last_with_full_logs(monkeypatch):
    from app import profiling
    emitted=[]
    monkeypatch.setattr(profiling.log,"info",lambda fmt,value:emitted.append(json.loads(value)["batch_no"]))
    profile=RunTiming("run")
    for i in range(BATCH_SERIES_LIMIT+9):
        profile.record_batch(dict(kind="results",rows=50000,batch_seq=i+1))
    value=profile.snapshot()
    assert len(value["batch_series"])==BATCH_SERIES_LIMIT
    assert value["batch_series_omitted"]==9 and value["batch_series_total"]==BATCH_SERIES_LIMIT+9
    assert value["batch_series"][0]["batch_no"]==1
    assert value["batch_series"][-1]["batch_no"]==BATCH_SERIES_LIMIT+9
    assert emitted==list(range(1,BATCH_SERIES_LIMIT+10))


def snapshot(read=100,hit=200):
    return dict(status="ok",server_started="start",stats_reset=None,table_oid=1,block_size=8192,
                indexes=[dict(index_oid=2,index_name="idx",size_bytes=8192,idx_blks_read=read,idx_blks_hit=hit)])


def test_index_deltas_include_scope_size_and_do_not_claim_physical_disk():
    before,after=snapshot(),snapshot(107,231)
    after["indexes"][0]["size_bytes"]=16384
    value=index_delta(before,after)
    assert value["status"]=="ok" and value["includes_other_sessions"] and value["read_includes_os_cache"]
    row=value["indexes"][0]
    assert (row["idx_blks_read"],row["idx_blks_hit"],row["read_bytes"])==(7,31,7*8192)
    assert row["size_before_bytes"]==8192 and row["size_after_bytes"]==16384
    assert before==snapshot()


@pytest.mark.parametrize("key",["server_started","stats_reset","table_oid","block_size"])
def test_restart_reset_and_relation_change_invalidate_delta(key):
    before,after=snapshot(),snapshot(107,231)
    after[key]="changed"
    value=index_delta(before,after)
    assert value["status"]=="invalid" and value["reason"]==key+"_changed"
    assert not value["indexes"]


def test_index_recreation_or_reset_is_not_silently_clamped_to_zero():
    before,after=snapshot(),snapshot(99,231)
    value=index_delta(before,after)
    assert value["status"]=="partial" and value["indexes"][0]["reason"]=="counter_reset"
    after=snapshot();after["indexes"][0]["index_oid"]=3
    value=index_delta(before,after)
    assert value["status"]=="partial" and len(value["indexes"])==2
    assert all("idx_blks_read" not in row for row in value["indexes"])
    assert index_delta(None,after)["status"]=="unavailable"


def test_diagnostic_failure_cannot_fail_scan_or_leak_error_text():
    class Broken:
        closed=False
        info=SimpleNamespace(transaction_status=TransactionStatus.IDLE)
        @contextmanager
        def transaction(self):yield
        def execute(self,*args):raise RuntimeError("secret connection details")
    profile=RunTiming("run")
    with profile.activate():capture_index_stats(SimpleNamespace(conn=Broken()),"after")
    value=profile.snapshot()["index_io"]
    assert value["after"]["status"]=="unavailable" and value["after"]["reason"]=="RuntimeError"
    assert value["delta"]["status"]=="unavailable"
    assert "secret" not in json.dumps(value)


@requires_db
@pytest.mark.parametrize("fail",[False,True])
def test_writer_work_mem_and_settings_are_restored_to_pooled_connection(snapshot_db,fail):
    raw=db.engine.raw_connection()
    conn=raw.driver_connection
    factory,autocommit=conn.row_factory,conn.autocommit
    conn.autocommit=True
    conn.execute("SET work_mem='12MB'")
    conn.execute("SET statement_timeout='72s'")
    conn.execute("SET lock_timeout='7s'")
    pid=conn.info.backend_pid
    conn.autocommit=autocommit
    raw.close()
    def borrow():
        with db.scan_connection(work_mem=True) as writer:
            assert writer.info.backend_pid==pid
            assert writer.execute("SHOW work_mem").fetchone()["work_mem"]=="64MB"
            writer.execute("BEGIN")
            if fail:raise ValueError("injected")
    if fail:
        with pytest.raises(ValueError):borrow()
    else:borrow()
    raw=db.engine.raw_connection()
    try:
        conn=raw.driver_connection
        assert conn.info.backend_pid==pid and conn.row_factory is factory and conn.autocommit is autocommit
        assert conn.execute("SHOW work_mem").fetchone()[0]=="12MB"
        assert conn.execute("SHOW statement_timeout").fetchone()[0]=="72s"
        assert conn.execute("SHOW lock_timeout").fetchone()[0]=="7s"
    finally:raw.close()


@requires_db
def test_native_index_boundaries_flush_own_counters_and_preserve_timeouts(snapshot_db):
    profile=RunTiming("run")
    with profile.activate(),BulkWriter("run","token") as writer:
        capture_index_stats(writer,"before")
        writer.results([dict(left_id="l",right_id="r",similarity=1.0,left_embedding_id="e1",right_embedding_id="e2")])
        capture_index_stats(writer,"after")
        assert writer.conn.execute("SHOW statement_timeout").fetchone()["statement_timeout"]=="2min"
        assert writer.conn.execute("SHOW work_mem").fetchone()["work_mem"]=="64MB"
    value=profile.snapshot()
    assert value["index_io"]["before"]["flush_forced"]
    assert value["index_io"]["after"]["flush_forced"]
    delta=value["index_io"]["delta"]
    assert delta["status"]=="ok" and len(delta["indexes"])==4
    assert sum(row["idx_blks_hit"] for row in delta["indexes"])>0
    assert all(row["idx_blks_read"]>=0 for row in delta["indexes"])
    assert value["batch_series"][0]["rows"]==1 and value["batch_series"][0]["status"]=="committed"


def test_disabled_tracking_is_unavailable_instead_of_reporting_zero_io():
    before,after=snapshot(),snapshot()
    after["track_counts"]=False
    value=index_delta(before,after)
    assert value["status"]=="unavailable" and value["reason"]=="track_counts_disabled"
