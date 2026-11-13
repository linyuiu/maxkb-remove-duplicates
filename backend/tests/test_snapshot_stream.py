"""COPY row streaming, idle deadlines and native pgvector binary transport."""

from contextlib import closing
import os
import struct
import threading
from types import SimpleNamespace
from uuid import uuid4

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row
import pytest

from app.source import (
    _ReadDeadline, snapshot_rows, VECTOR_SNAPSHOT_SQL, VECTOR_SNAPSHOT_COLUMNS,
    VECTOR_SNAPSHOT_TYPES, PARAGRAPH_SNAPSHOT_SQL, PARAGRAPH_SNAPSHOT_COLUMNS,
    PARAGRAPH_SNAPSHOT_TYPES,
)


def test_read_deadline_pauses_during_slow_consumption_and_resets():
    cancelled = threading.Event()
    conn = SimpleNamespace(cancel_safe=lambda **kwargs: cancelled.set())
    deadline = _ReadDeadline(conn, 0.1)
    deadline.thread.start()
    try:
        deadline.resume()
        deadline.pause()
        assert not cancelled.wait(0.2)  # Longer than a read deadline, but paused.
        deadline.resume()
        assert cancelled.wait(2)
        with pytest.raises(TimeoutError, match="未返回数据"):
            deadline.pause()
    finally:
        deadline.close()
    assert not deadline.thread.is_alive()


class CopyConnection:
    def __init__(self):
        self.info = SimpleNamespace(transaction_status=TransactionStatus.INTRANS)
        self.settings = []
        self.closed = False
        self.cancelled = threading.Event()
        self.position = 0

    def execute(self, query, params=None):
        self.settings.append((query, params))
        return SimpleNamespace(fetchone=lambda: {"statement_timeout": "2min"})

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def copy(self, query, params):
        assert query.endswith("TO STDOUT (FORMAT BINARY)")
        return self

    def set_types(self, types):
        assert types == ("text",)

    def read_row(self):
        self.position += 1
        return ("row",) if self.position <= 10 else None

    def cancel_safe(self, **kwargs):
        self.cancelled.set()


def test_copy_generator_early_close_restores_timeout_and_closes_context():
    conn = CopyConnection()
    with closing(snapshot_rows(conn, "SELECT 'row'", {}, ("value",), ("text",))) as rows:
        assert next(rows) == {"value": "row"}
    assert conn.position == 1 and conn.closed and not conn.cancelled.is_set()
    assert conn.settings[-1] == ("SELECT set_config('statement_timeout',%s,true)", ("2min",))


def test_copy_stalled_read_cancels_and_reports_timeout():
    conn = CopyConnection()
    def blocked_read():
        assert conn.cancelled.wait(2)
        conn.info.transaction_status = TransactionStatus.INERROR
        raise psycopg.errors.QueryCanceled("cancelled by watchdog")
    conn.read_row = blocked_read
    with pytest.raises(TimeoutError, match="未返回数据"):
        list(snapshot_rows(conn, "SELECT 'row'", {}, ("value",), ("text",), read_timeout=0.05))
    assert conn.closed
    assert conn.settings[-1][0] == "SELECT set_config('statement_timeout','0',true)"


@pytest.fixture
def native_source():
    if os.environ.get("DEDUP_DISPOSABLE_TEST_DB") != "1":
        pytest.skip("Requires disposable test socket server")
    conn = psycopg.connect(
        "host=127.0.0.1 port=15439 dbname=postgres user=postgres password=postgres sslmode=disable",
        row_factory=dict_row, autocommit=True, prepare_threshold=None,
    )
    schema = "binary_snapshot_" + uuid4().hex
    k, d, p = [str(uuid4()) for _ in range(3)]
    try:
        conn.execute("CREATE SCHEMA " + schema)
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA " + schema)
        conn.execute("SET search_path TO " + schema + ", public")
        conn.execute("CREATE TABLE knowledge(id uuid PRIMARY KEY,name text,workspace_id text)")
        conn.execute("CREATE TABLE document(id uuid PRIMARY KEY,knowledge_id uuid,name text,is_active boolean)")
        conn.execute("CREATE TABLE paragraph(id uuid PRIMARY KEY,document_id uuid,knowledge_id uuid,title text,content text,update_time timestamptz)")
        conn.execute("CREATE TABLE embedding(id text PRIMARY KEY,paragraph_id uuid,document_id uuid,knowledge_id uuid,embedding vector,source_type text)")
        conn.execute("INSERT INTO knowledge VALUES (%s,'知识库','default')", (k,))
        conn.execute("INSERT INTO document VALUES (%s,%s,'文档',true)", (d, k))
        conn.execute("INSERT INTO paragraph VALUES (%s,%s,%s,'标题',%s,now())", (p, d, k, "正文" * 1000))
        for i in range(3):
            conn.execute("INSERT INTO embedding VALUES (%s,%s,%s,%s,'[1,0.125]','1')", (f"chunk-{i}", p, d, k))
        conn.autocommit = False
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        conn.execute("SET LOCAL statement_timeout='2min'")
        yield conn, {"knowledge_ids": [k], "include_inactive_documents": True, "max_paragraph_bytes": 8 * 1024**2}
    finally:
        conn.rollback()
        conn.autocommit = True
        conn.execute("SET search_path TO public")
        conn.execute("DROP SCHEMA IF EXISTS " + schema + " CASCADE")
        conn.close()


def test_native_copy_transfers_body_once_and_exact_binary_vectors(native_source):
    conn, params = native_source
    vectors = list(snapshot_rows(conn, VECTOR_SNAPSHOT_SQL, params, VECTOR_SNAPSHOT_COLUMNS, VECTOR_SNAPSHOT_TYPES))
    bodies = list(snapshot_rows(conn, PARAGRAPH_SNAPSHOT_SQL, params, PARAGRAPH_SNAPSHOT_COLUMNS, PARAGRAPH_SNAPSHOT_TYPES))
    assert len(vectors) == 3 and len(bodies) == 1
    assert "content" not in vectors[0]
    assert all(row["vector"] == struct.pack("!HHff", 2, 0, 1, 0.125) for row in vectors)
    assert bodies[0]["content"] == "正文" * 1000
    assert conn.execute("SHOW statement_timeout").fetchone()["statement_timeout"] == "2min"


def test_native_oversized_text_is_not_transmitted(native_source):
    conn, params = native_source
    params["max_paragraph_bytes"] = 100
    rows = list(snapshot_rows(conn, PARAGRAPH_SNAPSHOT_SQL, params, PARAGRAPH_SNAPSHOT_COLUMNS, PARAGRAPH_SNAPSHOT_TYPES))
    assert len(rows) == 1 and rows[0]["payload_bytes"] > 100
    assert rows[0]["content"] is rows[0]["title"] is rows[0]["knowledge_name"] is None
