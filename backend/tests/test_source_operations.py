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


@pytest.mark.parametrize("kind", [
    "delete_document",
    "delete_paragraph",
])
def test_deletion_matches_each_pairs_own_snapshot(connection, kind):
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
            ("original", "source-a"), ("later", "source-a"), ("another-run", "source-a"),
            ("other-source", "source-b"), ("other-knowledge", "source-a"),
            ("other-workspace", "source-a"),
        ]:
            session.add(Run(id=rid, task_id=sid, config={"source_id": sid}, state="succeeded"))
        session.flush()
        session.add_all([
            snapshot("original", "deleted-paragraph"),
            snapshot("another-run", "deleted-paragraph"),
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
            pair("same-paragraph-another-run", "another-run", "deleted-paragraph"),
            pair("shared-paragraph", "original", "peer", "deleted-paragraph"),
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
            kind=kind, state="executing",
            before={"paragraphs": [{"id": "deleted-paragraph", "content": "original text"}]},
        ))

    finish("operation", "user", "succeeded", None, None, connection=connection)

    with Session(connection) as session:
        assert {row.id: row.status for row in session.scalars(select(Pair))} == {
            "target": "resolved", "shared-paragraph": "resolved",
            "same-paragraph-another-run": "resolved",
            "same-document-right": "resolved" if kind == "delete_document" else "pending",
            "unrelated": "ignored", "moved-paragraph": "pending",
            "same-document-another-run": "resolved" if kind == "delete_document" else "pending", "different-source": "pending",
            "different-knowledge": "pending", "different-workspace": "pending",
        }
        operation = session.get(Operation, "operation")
        assert operation.state == "succeeded" and operation.actual is None
        assert operation.before["paragraphs"][0]["content"] == "original text"
        decisions = session.scalars(select(Decision)).all()
        resolved = set(session.scalars(select(Pair.id).where(Pair.status == "resolved")))
        assert {decision.pair_id for decision in decisions} == resolved
        assert len(decisions) == len(resolved)
        assert all(decision.status == "resolved" and 'operation' in decision.note for decision in decisions)


@pytest.mark.parametrize('scenario,kind,expected', [
    (scenario, kind, expected)
    for kind in ['update_paragraph', 'delete_paragraph', 'delete_document']
    for scenario, expected in [
        ('normal', 'succeeded'), ('read_timeout', 'failed'),
        ('read_generic', 'failed'), ('read_denied', 'failed'),
        ('write_timeout', 'unknown'), ('readback_timeout', 'unknown'),
        ('reconcile_timeout', 'unknown'),
    ]
] + [('wrong_readback','update_paragraph','unknown')])
def test_audit_operation_outcomes(connection, monkeypatch, scenario, kind, expected):
    from contextlib import nullcontext
    from types import SimpleNamespace
    import httpx
    from cryptography.fernet import Fernet
    from app import source_operations as ops
    from app.security import encrypt, content_hash
    from app.maxkb_api import MaxKBAPI, document_snapshot

    monkeypatch.setenv('ENCRYPTION_KEY', Fernet.generate_key().decode())
    monkeypatch.setattr(ops.db, 'transaction', lambda: ops.atomic(connection))
    monkeypatch.setattr(ops.db, 'engine', SimpleNamespace(connect=lambda: nullcontext(connection)))
    target=dict(source_id='s',workspace_id='default',knowledge_id='k',document_id='d',paragraph_id='p')
    old=dict(id='p',document_id='d',title='title',content='before')
    rows=[old.copy()]
    writes=[]
    def handler(request):
        if request.method=='GET':
            if scenario in {'read_timeout','reconcile_timeout'} or (scenario=='readback_timeout' and writes):
                raise httpx.ReadTimeout('test',request=request)
            if scenario=='read_generic':raise RuntimeError('test')
            if scenario=='read_denied':return httpx.Response(403)
            return httpx.Response(200,json={'code':200,'data':{'total':len(rows),'records':rows}})
        writes.append(request.method)
        if scenario=='write_timeout':raise httpx.ReadTimeout('test',request=request)
        if scenario!='wrong_readback':
            if kind in {'delete_paragraph','delete_document'}:rows.clear()
            else:rows[0]['content']='after'
        return httpx.Response(200,json={'code':200,'data':True})
    monkeypatch.setattr(ops,'MaxKBAPI',lambda source: MaxKBAPI(source,transport=httpx.MockTransport(handler)))
    def document_exists(*args):
        if scenario in {'readback_timeout','reconcile_timeout'}:raise TimeoutError('test')
        return False
    monkeypatch.setattr(ops,'document_exists',document_exists)
    before = document_snapshot(target, rows) if kind=='delete_document' else {**target,**old,'content_hash':content_hash('title','before')}
    with Session(connection) as s, s.begin():
        s.add(User(id='u',username='audit',password_hash='unused'))
        s.add(DataSource(id='s',name='audit',connection='unused',api_base_url='http://maxkb.example.com/admin/api',api_key=encrypt('test-key')));s.flush()
        s.add(Task(id='t',name='audit',source_id='s',knowledge_ids=['k']));s.flush()
        s.add(Run(id='r',task_id='t',config={'source_id':'s'},state='succeeded'));s.flush()
        s.add(Pair(id='pair',run_id='r',left_id='p',right_id='q',left_embedding_id='e',right_embedding_id='f',similarity=1));s.flush()
        s.add(ParagraphSnapshot(run_id='r',paragraph_id='p',data=target))
        s.add(Operation(id='op',pair_id='pair',user_id='u',target=target,kind=kind,state='unknown' if scenario=='reconcile_timeout' else 'draft',before=before,proposed={'title':'title','content':'after'} if kind=='update_paragraph' else None))
    result=ops.execute_operation('op','u',reconcile=scenario=='reconcile_timeout')
    assert writes==([] if scenario in {'read_timeout','read_generic','read_denied','reconcile_timeout'} else ['PUT' if kind=='update_paragraph' else 'DELETE'])
    assert result.state==expected
    if expected=='succeeded':
        assert (result.before['paragraphs'][0]['content'] if kind=='delete_document' else result.before['content'])=='before'
        assert result.actual is None if kind!='update_paragraph' else result.actual['content']=='after'
    with Session(connection) as s:
        assert s.get(Operation,'op').state==expected
        assert s.get(Pair,'pair').status==('resolved' if expected=='succeeded' else 'pending')
    if expected=='failed':assert '未发送修改/删除请求' in result.error
