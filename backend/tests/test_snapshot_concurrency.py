"""Real multi-session PostgreSQL tests; never uses a configured business DB.

Start disposable PostgreSQL on 127.0.0.1:15440, then set
DEDUP_NATIVE_TEST_DB=1. PGlite multiplexing cannot validate row-lock concurrency.
"""

from datetime import timedelta
import os
import threading
import time
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

from test_snapshot_batches import SourceStream

pytestmark = pytest.mark.skipif(
    os.environ.get("DEDUP_NATIVE_TEST_DB") != "1",
    reason="Requires real disposable PostgreSQL with separate backend sessions",
)


@pytest.mark.parametrize("cancel", [False, True])
def test_heartbeat_and_cancel_can_run_during_snapshot_insert(monkeypatch, cancel):
    from app import db, main, scanning
    from app.matrix import VectorStore
    from app.models import DataSource, Task, Run, ParagraphSnapshot, now

    url = "postgresql+psycopg://postgres@127.0.0.1:15440/postgres?sslmode=disable"
    schema = "snapshot_concurrency_" + uuid4().hex
    admin = create_engine(url)
    engine = None
    inserted, release, finished, stopped = [threading.Event() for _ in range(4)]
    errors, threads = [], []
    try:
        with admin.begin() as conn:
            conn.exec_driver_sql("CREATE SCHEMA " + schema)
        engine = create_engine(url, connect_args={
            "options": f"-c search_path={schema} -c lock_timeout=300ms -c statement_timeout=3s",
        })
        db.Base.metadata.create_all(engine)
        sessions = sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(db, "Session", sessions)
        monkeypatch.setattr(db,"engine",engine)
        monkeypatch.setattr(scanning, "check_models", lambda *args: None)
        monkeypatch.setattr(scanning, "check_vector_readiness", lambda *args: None)
        monkeypatch.setattr(scanning, "workspace_names", lambda *args: {})
        source = SourceStream(1)
        monkeypatch.setattr(scanning, "snapshot_rows", source.rows)
        initial_lease = now() + timedelta(seconds=0.8)
        with sessions.begin() as s:
            s.add(DataSource(id="source", name="source", connection="unused"))
            s.flush()
            s.add(Task(id="task", name="task", source_id="source", knowledge_ids=["knowledge"]))
            s.flush()
            s.add(Run(id="run", task_id="task", state="running", token="token",
                      lease_until=initial_lease,
                      config={"knowledge_ids": ["knowledge"], "exclude_same_document": False}))

        from app.bulk_writer import BulkWriter
        original_guard=BulkWriter._guard
        def hold_insert(writer,seq,key,delta):
            if delta["kind"]=="snapshots":
                inserted.set()
                assert release.wait(5), "test did not release the snapshot write"
            return original_guard(writer,seq,key,delta)
        monkeypatch.setattr(BulkWriter,"_guard",hold_insert)

        def snapshot():
            try:
                with VectorStore() as store:
                    scanning.take_snapshot("run", "token", source, store)
            except Exception as exc:
                errors.append(exc)
            finally:
                finished.set()
        worker = threading.Thread(target=snapshot)
        threads.append(worker)
        worker.start()
        assert inserted.wait(3), errors

        class FastHeartbeat:
            def wait(self, seconds):
                return stopped.wait(0.02)
        renewer = threading.Thread(target=scanning.heartbeat, args=("run", "token", FastHeartbeat()))
        threads.append(renewer)
        renewer.start()
        deadline = time.monotonic() + 0.6
        while True:
            with sessions() as s:
                lease = s.get(Run, "run").lease_until
            if lease > initial_lease:
                break
            assert time.monotonic() < deadline, "heartbeat blocked behind the snapshot INSERT"
            stopped.wait(0.02)
        # Keep the write open past the original lease. A heartbeat must keep it valid.
        stopped.wait(0.9)
        assert not finished.is_set() and now() > initial_lease
        with sessions() as s:
            assert s.get(Run, "run").lease_until > now()
        if cancel:
            started = time.monotonic()
            main.cancel("run", user=None)
            assert time.monotonic() - started < 0.5
        release.set()
        assert finished.wait(3)
        if cancel:
            assert len(errors) == 1 and isinstance(errors[0], scanning.Stopped)
            assert str(errors[0]) == "用户取消"
        else:
            assert errors == []
        with sessions() as s:
            run = s.get(Run, "run")
            assert run.snapshot_ready is not cancel
            assert run.total == (0 if cancel else 1)
            assert s.scalar(select(func.count()).select_from(ParagraphSnapshot)) == (0 if cancel else 1)
    finally:
        release.set()
        stopped.set()
        for thread in threads:
            thread.join(timeout=5)
        if engine:
            engine.dispose()
        with admin.begin() as conn:
            conn.exec_driver_sql("DROP SCHEMA IF EXISTS " + schema + " CASCADE")
        admin.dispose()


def test_native_copy_timeout_cancels_only_stalled_reads():
    import psycopg
    from psycopg.rows import dict_row
    from app.source import snapshot_rows

    with psycopg.connect("host=127.0.0.1 port=15440 dbname=postgres user=postgres sslmode=disable",
                         row_factory=dict_row) as conn:
        conn.execute("SET LOCAL statement_timeout='50ms'")
        # Exceed the server's socket buffer per row so it actually sends data
        # between sleeps, instead of buffering all six tiny rows until EOF.
        rows = list(snapshot_rows(
            conn, "SELECT i::text || repeat('x',16384) FROM generate_series(1,6) i CROSS JOIN LATERAL pg_sleep(0.03+i*0)",
            {}, ("value",), ("text",), read_timeout=0.15,
        ))
        assert len(rows) == 6  # Whole COPY > 50ms; each read is below its deadline.
        assert conn.execute("SHOW statement_timeout").fetchone()["statement_timeout"] == "50ms"
        started = time.monotonic()
        with pytest.raises(TimeoutError, match="未返回数据"):
            list(snapshot_rows(conn, "SELECT pg_sleep(2)::text", {}, ("value",), ("text",), read_timeout=0.05))
        assert time.monotonic() - started < 1
        conn.rollback()
        assert conn.execute("SELECT 1 AS value").fetchone()["value"] == 1
