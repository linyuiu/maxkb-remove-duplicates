"""Deterministic timing accounting and unchanged commit/rollback semantics."""

from types import SimpleNamespace
import threading

import pytest

from app.profiling import RunTiming, current, measure, publish, timed_rows


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def by_key(profile, key):
    return [row for row in profile.snapshot()["stages"] if row["key"] == key]


def test_parent_totals_and_exclusive_seconds_do_not_double_count():
    clock = Clock()
    profile = RunTiming("run", clock)
    with profile.activate(), measure("compare", "比较"):
        clock.advance(2)
        with measure("write", "写库"):
            clock.advance(3)
        clock.advance(5)
    profile.finish()
    data = profile.snapshot()
    assert data["elapsed_seconds"] == 10
    assert data["unmeasured_seconds"] == 0
    assert by_key(profile, "compare")[0]["seconds"] == 10
    assert by_key(profile, "compare")[0]["self_seconds"] == 7
    assert by_key(profile, "write")[0]["self_seconds"] == 3
    assert sum(row["self_seconds"] for row in data["stages"]) == 10
    assert current() is None and data["final"]


def test_active_spans_and_repeated_calls_have_bounded_aggregates():
    clock = Clock()
    profile = RunTiming("run", clock)
    with profile.activate(), measure("parent", "父阶段"):
        clock.advance(1)
        for _ in range(1000):
            with measure("child", "子阶段"):
                clock.advance(2)
        with measure("active", "运行中"):
            clock.advance(3)
            data = profile.snapshot()
            assert len(data["stages"]) == 3
            assert sum(row["self_seconds"] for row in data["stages"]) == 2004
            assert by_key(profile, "parent")[0]["self_seconds"] == 1
    child = by_key(profile, "child")[0]
    assert child["calls"] == 1000 and child["seconds"] == 2000 and child["max_seconds"] == 2


def test_exceptions_close_spans_and_background_threads_do_not_share_profile():
    clock = Clock()
    profile = RunTiming("run", clock)
    seen = []
    with profile.activate():
        thread = threading.Thread(target=lambda: seen.append(current()))
        thread.start(); thread.join(timeout=2)
        with pytest.raises(ValueError):
            with measure("failure", "失败"):
                clock.advance(1)
                raise ValueError("expected")
    assert seen == [None] and profile.stack == []
    assert by_key(profile, "failure")[0]["errors"] == 1


def test_stream_timing_excludes_consumer_work_and_closes_generator():
    clock = Clock()
    profile = RunTiming("run", clock)
    closed = []
    def rows():
        try:
            for i in range(2):
                clock.advance(1)
                yield i
        finally:
            clock.advance(1)
            closed.append(True)
    with profile.activate():
        for row in timed_rows(rows(), "read", "读取"):
            clock.advance(100)
    assert closed == [True]
    assert by_key(profile, "read")[0]["seconds"] == 3
    assert profile.snapshot()["elapsed_seconds"] == 203


def test_publish_is_throttled_and_preserves_other_config(monkeypatch):
    import sqlalchemy.orm
    from app.models import Run
    clock=Clock()
    profile=RunTiming("run",clock)
    run=Run(id="run",config={"threshold":0.9})
    saved=[]
    monkeypatch.setattr(sqlalchemy.orm,"object_session",lambda obj:SimpleNamespace(execute=saved.append))
    with profile.activate():
        publish(run)
        assert saved==[]
        publish(run,force=True)
        assert len(saved)==1
        clock.advance(9)
        assert not profile.due()
        clock.advance(1)
        assert profile.due()
        publish(run)
        assert len(saved)==2 and run.config=={"threshold":0.9}


@pytest.mark.parametrize("fail", [None, "body", "flush", "commit"])
def test_profiled_transaction_keeps_flush_commit_rollback_and_close(monkeypatch, fail):
    from app import db
    calls = []
    class Session:
        def connection(self):
            calls.append("connection")
        def flush(self):
            calls.append("flush")
            if fail == "flush": raise ValueError("flush failed")
        def commit(self):
            calls.append("commit")
            if fail == "commit": raise ValueError("commit failed")
        def rollback(self):
            calls.append("rollback")
        def close(self):
            calls.append("close")
    monkeypatch.setattr(db, "Session", Session)
    profile = RunTiming("run")
    def execute():
        with profile.activate(), db.transaction():
            calls.append("body")
            if fail == "body": raise ValueError("body failed")
    if fail:
        with pytest.raises(ValueError, match="failed"): execute()
        assert calls[-2:] == ["rollback", "close"]
    else:
        execute()
        assert calls == ["connection", "body", "flush", "commit", "close"]
    assert bool(by_key(profile, "rollback")) is bool(fail)


def test_matrix_accounting_excludes_writes_from_filter_self_time():
    from app.matrix import VectorStore, compare
    clock = Clock()
    profile = RunTiming("run", clock)
    matches = []
    def consume(rows):
        with measure("result_write", "结果持久化"):
            matches.extend(rows)
            clock.advance(5)
    with VectorStore() as store:
        for i in range(4):
            store.append(dict(embedding_id=f"e{i}", paragraph_id=f"p{i}", document_id=f"d{i}", vector=[1, 0]))
        store.flush()
        with profile.activate():
            done = compare(store, threshold=0.9, exclude_same_document=False, block_size=2,
                           threads=1, consume=consume, checkpoint=lambda *args: None)
    assert done == len(matches) == 6
    assert profile.counters["matrix_tiles"] == 3
    assert all(stage["self_seconds"] == 0 for stage in by_key(profile, "filter"))
    assert sum(stage["seconds"] for stage in by_key(profile, "result_write")) == 15
