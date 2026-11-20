"""Physical per-run storage lifecycle, atomic DDL, and retained operation history."""
import os
from types import SimpleNamespace

import pytest
from sqlalchemy import event, inspect, select, text
from sqlalchemy.orm import Session

from app import main
from app.db import Base
from app.models import DataSource, User, Task, Run, Operation, ScanResultStorage, ScanParagraphRef
from app.result_storage import models_for_run, result_models
from app.source_operations import atomic
from test_source_operations import connection

pytestmark = pytest.mark.skipif(os.environ.get("DEDUP_DISPOSABLE_TEST_DB") != "1", reason="Disposable database required")


def seed(connection):
    with Session(connection) as s, s.begin():
        s.add(User(id="user", username="user", password_hash="unused"))
        s.add(DataSource(id="source", name="source", connection="unused"))
        s.flush()
        s.add(Task(id="task", name="task", source_id="source", knowledge_ids=[]))
        s.flush()
        for rid in ("first", "second"):
            s.add(Run(id=rid, task_id="task", state="succeeded", config={"source_id": "source", "task_name": "Demo"}))
    for rid in ("first", "second"):
        models = models_for_run(rid)
        with Session(connection) as s, s.begin():
            s.add(models.ParagraphSnapshot(paragraph_id="p", data={
                "workspace_id": "default", "knowledge_id": "k", "document_id": "d",
                "title": "title", "content": "original", "knowledge_name": "K", "document_name": "D",
            }))
            s.add(models.Pair(id=f"{rid}:pair", left_id="p", right_id="p", similarity=1,
                              left_embedding_id="e", right_embedding_id="e"))
        with Session(connection) as s, s.begin():
            s.add(Operation(id=rid, scan_run_id=rid, pair_id=f"{rid}:pair", user_id="user",
                            target={"source_id": "source", "workspace_id": "default", "knowledge_id": "k", "document_id": "d", "paragraph_id": "p"},
                            kind="update_paragraph", before={"content": "before"}, proposed={"content": "after"},
                            actual={"content": "after"}, state="succeeded"))


def test_drop_run_keeps_other_scan_and_operation_text(connection, monkeypatch):
    seed(connection)
    monkeypatch.setattr(main, "transaction", lambda: atomic(connection))
    statements = []
    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(connection, "before_cursor_execute", record)
    try:
        assert main.delete_run("first", SimpleNamespace(id="user")) == {"ok": True}
    finally:
        event.remove(connection, "before_cursor_execute", record)
    deleted = models_for_run("first")
    for name in deleted.names.values():
        assert not inspect(connection).has_table(name)
        assert not any(sql.startswith("DELETE FROM " + name) for sql in statements)
    assert sum(sql.lstrip().startswith("DROP TABLE") for sql in statements) == 3
    with Session(connection) as s:
        assert s.get(Run, "first") is None
        assert s.get(ScanResultStorage, "first") is None
        assert s.get(ScanResultStorage, "second") is not None
        assert s.scalar(select(ScanParagraphRef).where(ScanParagraphRef.run_id == "first")) is None
        ledger = s.get(Operation, "first")
        assert ledger.scan_run_id == "first" and ledger.pair_id is None
        assert ledger.before == {"content": "before"} and ledger.actual == {"content": "after"}
        assert ledger.target["scan_pair_id"] == "first:pair"
        assert s.get(models_for_run("second").Pair, "second:pair") is not None


def test_failed_drop_rolls_back_tables_and_ledger(connection, monkeypatch):
    seed(connection)
    monkeypatch.setattr(main, "transaction", lambda: atomic(connection))
    dropped = []
    def fail(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().startswith("DROP TABLE"):
            dropped.append(statement)
            if len(dropped) == 2:
                raise RuntimeError("injected DDL failure")
    event.listen(connection, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError, match="injected DDL"):
            main.delete_run("first", SimpleNamespace(id="user"))
    finally:
        event.remove(connection, "before_cursor_execute", fail)
    assert len(dropped) == 2
    for name in models_for_run("first").names.values():
        assert inspect(connection).has_table(name)
    with Session(connection) as s:
        assert s.get(Run, "first") is not None
        assert s.get(Operation, "first").pair_id == "first:pair"
        assert s.get(Operation, "first").target.get("scan_pair_id") is None


@pytest.mark.parametrize("state", ["executing", "unknown"])
def test_unsettled_operation_blocks_drop(connection, monkeypatch, state):
    from fastapi import HTTPException
    seed(connection)
    with Session(connection) as s, s.begin():
        s.get(Operation, "first").state = state
    monkeypatch.setattr(main, "transaction", lambda: atomic(connection))
    with pytest.raises(HTTPException) as error:
        main.delete_run("first", SimpleNamespace(id="user"))
    assert error.value.status_code == 409
    assert inspect(connection).has_table(models_for_run("first").names["pairs"])


def test_table_creation_and_run_rollback_together(connection):
    seed(connection)
    names = models_for_run("failed").names
    with pytest.raises(RuntimeError, match="abort creation"):
        with Session(connection) as s, s.begin():
            s.add(Run(id="failed", task_id="task", config={"source_id": "source"}, state="queued"))
            s.flush()
            assert inspect(connection).has_table(names["pairs"])
            raise RuntimeError("abort creation")
    assert not any(inspect(connection).has_table(name) for name in names.values())
    with Session(connection) as s:
        assert s.get(Run, "failed") is None
        assert s.get(ScanResultStorage, "failed") is None


def test_invalid_pair_id_cannot_select_arbitrary_table(connection):
    from fastapi import HTTPException
    from app.result_storage import get_pair
    seed(connection)
    with Session(connection) as s:
        with pytest.raises(HTTPException):
            get_pair(s, "first;DROP TABLE users:pair")
        pair, models = get_pair(s, "first:pair")
        assert pair.run_id == "first"
        assert models.names == models_for_run("first").names


@pytest.mark.skipif(os.environ.get("DEDUP_NATIVE_TEST_DB") != "1", reason="Needs concurrent native PostgreSQL sessions")
def test_delete_waits_for_reader_and_returns_retryable_timeout(connection, monkeypatch):
    from fastapi import HTTPException
    from sqlalchemy import create_engine
    seed(connection)
    schema = connection.exec_driver_sql("SHOW search_path").scalar()
    connection.commit()
    engine = create_engine(connection.engine.url, connect_args={"options": f"-c search_path={schema}"})
    try:
        with engine.connect() as holder, Session(holder) as reader, reader.begin():
            result_models(reader, "first")
            monkeypatch.setattr(main, "transaction", lambda: atomic(connection))
            sql_text = main.text
            monkeypatch.setattr(main, "text", lambda sql: sql_text(sql.replace("'5s'", "'100ms'")))
            with pytest.raises(HTTPException) as error:
                main.delete_run("first", SimpleNamespace(id="user"))
            assert error.value.status_code == 409 and "等待超时" in error.value.detail
            assert inspect(connection).has_table(models_for_run("first").names["pairs"])
        assert main.delete_run("first", SimpleNamespace(id="user")) == {"ok": True}
    finally:
        engine.dispose()


def test_large_result_deletion_drops_tables_without_row_deletes(connection, monkeypatch):
    import time
    seed(connection)
    table = models_for_run("first").names["pairs"]
    connection.exec_driver_sql(f"""INSERT INTO {table}
        (id,run_id,left_id,right_id,similarity,left_embedding_id,right_embedding_id,status)
        SELECT 'first:'||i::text,'first','left-'||i::text,'right-'||i::text,0.95,'e1','e2','pending'
        FROM generate_series(1,100000) AS i""")
    connection.commit()
    monkeypatch.setattr(main, "transaction", lambda: atomic(connection))
    statements = []
    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(connection, "before_cursor_execute", record)
    started = time.perf_counter()
    try:
        main.delete_run("first", SimpleNamespace(id="user"))
    finally:
        event.remove(connection, "before_cursor_execute", record)
    elapsed = time.perf_counter() - started
    assert not inspect(connection).has_table(table)
    assert not any(sql.lstrip().startswith("DELETE FROM " + table) for sql in statements)
    print(f"100001 similarity pairs removed by DROP TABLE in {elapsed:.3f}s (disposable database)")
