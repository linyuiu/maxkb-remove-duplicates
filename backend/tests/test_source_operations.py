"""Regression tests using isolated schemas in the disposable test database.

Start `node tests/sql/serve.mjs`, then set DEDUP_DISPOSABLE_TEST_DB=1.
No MaxKB API or production database is used.
"""

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import Base
from app.models import (
    DataSource, Decision, Operation, Pair, ParagraphSnapshot, Run, Task, User,
)
from app.source_operations import finish

pytestmark = pytest.mark.skipif(
    os.environ.get("DEDUP_DISPOSABLE_TEST_DB") != "1",
    reason="Requires disposable test socket server",
)


@pytest.fixture
def connection():
    engine = create_engine(
        "postgresql+psycopg://postgres:postgres@127.0.0.1:15438/postgres?sslmode=disable",
        pool_size=1,
        max_overflow=0,
    )
    schema = "operation_test_" + uuid4().hex
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("CREATE SCHEMA " + schema)
            conn.exec_driver_sql("SET search_path TO " + schema)
            conn.commit()
            try:
                Base.metadata.create_all(conn)
                conn.commit()
                yield conn
            finally:
                conn.rollback()
                conn.exec_driver_sql("SET search_path TO public")
                conn.exec_driver_sql("DROP SCHEMA " + schema + " CASCADE")
                conn.commit()
    finally:
        engine.dispose()


def test_document_deletion_matches_each_pairs_own_snapshot(connection):
    target = {
        "source_id": "source-a",
        "workspace_id": "default",
        "knowledge_id": "knowledge",
        "document_id": "deleted-document",
        "paragraph_id": "deleted-paragraph",
    }

    def snapshot(run_id, paragraph_id, **changes):
        return ParagraphSnapshot(
            run_id=run_id,
            paragraph_id=paragraph_id,
            data={**target, "paragraph_id": paragraph_id, **changes},
        )

    def pair(key, run_id, left_id, right_id="peer", status="pending"):
        return Pair(
            id=key, run_id=run_id, left_id=left_id, right_id=right_id,
            left_embedding_id="embedding-left", right_embedding_id="embedding-right",
            similarity=1, status=status,
        )

    with Session(connection) as session, session.begin():
        session.add(User(id="user", username="review", password_hash="unused"))
        for sid in ["source-a", "source-b"]:
            session.add(DataSource(id=sid, name=sid, connection="unused"))
        session.flush()
        for sid in ["source-a", "source-b"]:
            session.add(Task(id=sid, name=sid, source_id=sid, knowledge_ids=[]))
        session.flush()
        for rid, sid in [
            ("original", "source-a"), ("later", "source-a"),
            ("other-source", "source-b"), ("other-knowledge", "source-a"),
            ("other-workspace", "source-a"),
        ]:
            session.add(Run(id=rid, task_id=sid, config={"source_id": sid}, state="succeeded"))
        session.flush()
        session.add_all([
            snapshot("original", "deleted-paragraph"),
            snapshot("original", "another-deleted-paragraph"),
            snapshot("original", "foreign-paragraph", document_id="unrelated-document"),
            # This run associated the same paragraph ID with a different document.
            snapshot("later", "deleted-paragraph", document_id="unrelated-document"),
            snapshot("later", "another-deleted-paragraph"),
            # A cloned source contains overlapping IDs with different associations.
            snapshot("other-source", "foreign-paragraph"),
            snapshot("other-knowledge", "deleted-paragraph", knowledge_id="other-knowledge"),
            snapshot("other-workspace", "deleted-paragraph", workspace_id="other-workspace"),
        ])
        session.add_all([
            pair("target", "original", "deleted-paragraph"),
            pair("same-document-right", "original", "peer", "another-deleted-paragraph"),
            pair("unrelated", "original", "foreign-paragraph", status="ignored"),
            pair("moved-paragraph", "later", "deleted-paragraph"),
            pair("same-document-another-run", "later", "another-deleted-paragraph"),
            pair("different-source", "other-source", "foreign-paragraph"),
            pair("different-knowledge", "other-knowledge", "deleted-paragraph"),
            pair("different-workspace", "other-workspace", "deleted-paragraph"),
        ])
        session.flush()
        session.add(Operation(
            id="operation", pair_id="target", user_id="user", target=target,
            kind="delete_document", state="executing",
            before={"paragraphs": [{"id": "deleted-paragraph", "content": "original text"}]},
        ))

    finish("operation", "user", "succeeded", None, None, connection=connection)

    with Session(connection) as session:
        assert {row.id: row.status for row in session.scalars(select(Pair))} == {
            "target": "resolved", "same-document-right": "resolved",
            "unrelated": "ignored", "moved-paragraph": "pending",
            "same-document-another-run": "resolved", "different-source": "pending",
            "different-knowledge": "pending", "different-workspace": "pending",
        }
        operation = session.get(Operation, "operation")
        assert operation.state == "succeeded" and operation.actual is None
        assert operation.before["paragraphs"][0]["content"] == "original text"
        decision = session.scalars(select(Decision)).one()
        assert decision.pair_id == "target" and decision.status == "resolved"
