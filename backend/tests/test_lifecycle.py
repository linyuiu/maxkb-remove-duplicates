"""Optional database lifecycle tests against disposable PGlite socket servers.

Start: node tests/sql/serve.mjs
Run: DEDUP_DISPOSABLE_TEST_DB=1 uv run pytest tests/test_lifecycle.py -q
Never points at a production database. Ports and databases are hardcoded test-only.
"""

import os
from pathlib import Path
from uuid import uuid4
import pytest
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool
import psycopg
from cryptography.fernet import Fernet

pytestmark = pytest.mark.skipif(
    os.environ.get("DEDUP_DISPOSABLE_TEST_DB") != "1",
    reason="Requires disposable test socket servers",
)


def test_snapshot_resume_results_and_text_ledger(monkeypatch,tmp_path):
    monkeypatch.setenv("INGESTION_DATA_DIR",str(tmp_path/"ingestion"))
    from app import db, main
    from app.models import (
        User,
        DataSource,
        Task,
        Run,
        Pair,
        Operation,
        ParagraphSnapshot,
    )
    from app.security import encrypt, passwords
    from app.scanning import claim, take_snapshot, compare_batch, fenced, Stopped

    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    engine = create_engine(
        "postgresql+psycopg://postgres:postgres@127.0.0.1:15438/postgres?sslmode=disable",
        pool_size=1,
        max_overflow=0,
    )
    sessions = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "Session", sessions)
    monkeypatch.setattr(main, "Session", sessions)
    from app.initialize import initialize

    monkeypatch.setenv("ADMIN_USERNAME", "startup-admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-startup-password")
    initialize()
    initialize()
    with sessions() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(User)
                .where(User.username == "startup-admin")
            )
            == 1
        )
    with engine.connect() as conn:
        assert not conn.exec_driver_sql(
            "SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname='vector')"
        ).scalar()
        assert (
            conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar()
            == "0005"
        )
    dsn = "host=127.0.0.1 port=15439 dbname=postgres user=postgres password=postgres sslmode=disable"
    model = str(uuid4())
    kids = [str(uuid4()) for _ in range(3)]
    docs = [str(uuid4()) for _ in range(4)]
    pids = [str(uuid4()) for _ in range(4)]
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("CREATE EXTENSION vector")
        c.execute("CREATE TABLE model(id uuid primary key,name text)")
        c.execute(
            "CREATE TABLE knowledge(id uuid primary key,name text,workspace_id text,embedding_model_id uuid)"
        )
        c.execute(
            "CREATE TABLE document(id uuid primary key,name text,knowledge_id uuid)"
        )
        c.execute(
            "CREATE TABLE paragraph(id uuid primary key,title text,content text,document_id uuid,knowledge_id uuid,update_time timestamptz default now(),position int default 1)"
        )
        c.execute(
            "CREATE TABLE embedding(id text primary key,embedding vector,paragraph_id uuid,document_id uuid,knowledge_id uuid,source_type text)"
        )
        c.execute("INSERT INTO model VALUES (%s,%s)", (model, "test"))
        for i, k in enumerate(kids):
            c.execute(
                "INSERT INTO knowledge VALUES (%s,%s,%s,%s)",
                (k, "knowledge" + str(i), "default", model),
            )
        for i in range(4):
            k = kids[0 if i == 0 else 1 if i in [1, 2] else 2]
            c.execute(
                "INSERT INTO document VALUES (%s,%s,%s)",
                (docs[i], "document" + str(i), k),
            )
            c.execute(
                "INSERT INTO paragraph(id,title,content,document_id,knowledge_id) VALUES (%s,%s,%s,%s,%s)",
                (pids[i], "title" + str(i), "original text " + str(i), docs[i], k),
            )
            c.execute(
                "INSERT INTO embedding VALUES (%s,%s::vector,%s,%s,%s,%s)",
                (
                    "e" + str(i),
                    "[0,1]" if i == 2 else "[1,0]",
                    pids[i],
                    docs[i],
                    k,
                    "1",
                ),
            )
        c.execute(
            "INSERT INTO embedding SELECT %s,embedding,paragraph_id,document_id,knowledge_id,source_type FROM embedding WHERE id=%s",
            ("duplicate", "e0"),
        )
        c.execute(
            "INSERT INTO embedding SELECT 'problem','[1,0]'::vector,paragraph_id,document_id,knowledge_id,'0' FROM embedding WHERE id='e2'"
        )
    with db.transaction() as s:
        user = User(
            username="test", password_hash=passwords.hash("a-long-test-password")
        )
        s.add(user)
        source = DataSource(name="test", connection=encrypt(dsn))
        s.add(source)
        s.flush()
        task = Task(
            name="test",
            source_id=source.id,
            knowledge_ids=kids[:2],
            threshold=0.9,
            batch_size=1,
        )
        s.add(task)
        s.flush()
        sid = source.id
        task_id = task.id
    from fastapi import HTTPException

    scheduled = main.TaskBody(
        name="scheduled",
        source_id=sid,
        knowledge_ids=kids[:2],
        enabled=True,
        cron="0 0 * * *",
    )
    with pytest.raises(HTTPException):
        main.create_task(scheduled, user)
    schedule_preview = main.preview_schedule(scheduled, user=user)
    with sessions() as s:
        assert s.scalar(select(func.count()).select_from(Task)) == 1
        assert s.scalar(select(func.count()).select_from(Run)) == 0
    saved = main.create_task(
        scheduled.model_copy(
            update={"save_estimate": schedule_preview["save_estimate"]}
        ),
        user,
    )
    assert saved["enabled"] and saved["cron"] == "0 0 * * *"
    changed = scheduled.model_copy(update={"cron": "0 3 * * *"})
    with pytest.raises(HTTPException):
        main.edit_task(saved["id"], changed, user)
    edit_preview = main.preview_schedule(changed, task_id=saved["id"], user=user)
    with sessions() as s:
        assert s.get(Task, saved["id"]).cron == "0 0 * * *"
    updated = main.edit_task(
        saved["id"],
        changed.model_copy(update={"save_estimate": edit_preview["save_estimate"]}),
        user,
    )
    assert updated["cron"] == "0 3 * * *"
    preview = main.estimate_task(task_id, main.EstimateRequest(), user)
    assert (
        preview["chunks"] == 4
        and preview["paragraphs"] == 3
        and preview["documents"] == 3
    )
    assert preview["comparisons"] == 5 and len(preview["knowledge"]) == 2
    with sessions() as s:
        assert s.scalar(select(func.count()).select_from(Run)) == 0
    from app.models import ScanEstimate
    from app.models import now
    from datetime import timedelta
    from fastapi import HTTPException

    with db.transaction() as s:
        s.get(Task, task_id).threshold = 0.91
    with pytest.raises(HTTPException) as mismatch:
        main.run_task(
            task_id, main.ConfirmScan(estimate_id=preview["estimate_id"]), user
        )
    assert mismatch.value.status_code == 409
    with db.transaction() as s:
        s.get(Task, task_id).threshold = 0.9
        s.get(ScanEstimate, preview["estimate_id"]).expires_at = now() - timedelta(
            seconds=1
        )
    with pytest.raises(HTTPException) as expired:
        main.run_task(
            task_id, main.ConfirmScan(estimate_id=preview["estimate_id"]), user
        )
    assert expired.value.status_code == 409
    with db.transaction() as s:
        s.get(ScanEstimate, preview["estimate_id"]).expires_at = now() + timedelta(
            minutes=5
        )
    confirmed = main.run_task(
        task_id, main.ConfirmScan(estimate_id=preview["estimate_id"]), user
    )
    rid = confirmed["id"]
    assert (
        main.run_task(
            task_id, main.ConfirmScan(estimate_id=preview["estimate_id"]), user
        )["id"]
        == rid
    )
    job = claim()
    assert job[0] == rid
    with sessions() as s:
        source = s.get(DataSource, sid)
    from app.source import connect

    with connect(source) as conn:
        take_snapshot(*job, conn)
        more, after = compare_batch(*job, conn)
        assert more
    with db.transaction() as s:
        run = s.get(Run, rid)
        assert run.total == 4
        assert run.processed == 1
        run.state = "failed"
    preview_retry = main.estimate_task(
        task_id, main.EstimateRequest(retry_run_id=rid), user
    )
    retry = main.resume(
        rid, main.ConfirmScan(estimate_id=preview_retry["estimate_id"]), user
    )
    old_rid = rid
    assert retry["id"] != rid
    rid = retry["id"]
    newer = claim()
    assert newer[1] != job[1]
    with db.transaction() as s:
        with pytest.raises(Stopped):
            fenced(s, *job)
    with connect(source) as conn:
        take_snapshot(*newer, conn)
        after = None
        while True:
            more, after = compare_batch(*newer, conn, after)
            if not more:
                break
    with db.transaction() as s:
        run = s.get(Run, rid)
        assert run.state == "succeeded" and run.pair_count == 1 and run.processed == 4
        assert (
            s.scalar(
                select(func.count())
                .select_from(ParagraphSnapshot)
                .where(ParagraphSnapshot.run_id == rid)
            )
            == 3
        )
        assert s.get(Run, old_rid).state == "failed"
        pair = s.scalar(select(Pair).where(Pair.run_id == rid))
        assert {pair.left_id, pair.right_id} == set(pids[:2])
        assert pair.similarity == pytest.approx(1)
        pair_id = pair.id
        view = main.pairs(rid, s=s, user=user)
        assert view["total"] == 1
    with sessions() as s:
        live = main.current(pair_id, "left", s=s, user=user)["current"]
    body = main.OperationBody(
        side="left",
        kind="update_paragraph",
        title="new title",
        content="new text",
        expected_hash=live["content_hash"],
    )
    op = main.draft_operation(pair_id, body, user)
    assert (
        op["before"]["content"] == live["content"]
        and op["proposed"]["content"] == "new text"
    )
    assert op["state"] == "unconfigured" and op["actual"] is None
    with psycopg.connect(dsn) as c:
        assert (
            c.execute(
                "SELECT content FROM paragraph WHERE id=%s", (live["paragraph_id"],)
            ).fetchone()[0]
            == live["content"]
        )
    from fastapi.testclient import TestClient

    with TestClient(main.app) as client:
        headers = {"X-Requested-With": "dedup"}
        assert client.get("/api/sources").status_code == 401
        assert client.delete("/api/runs/"+rid,headers=headers).status_code == 401
        assert client.get("/api/ingestion/settings").status_code == 401
        assert client.post("/api/ingestion/queues",headers=headers,json={}).status_code == 401
        assert (
            client.post(
                "/api/login",
                json={"username": "test", "password": "a-long-test-password"},
                headers=headers,
            ).status_code
            == 200
        )
        assert client.get("/api/me").json()["username"] == "test"
        intake=client.get("/api/ingestion/settings")
        assert intake.status_code == 200
        assert len(intake.headers["X-Request-ID"]) == 32
        assert intake.headers["X-Content-Type-Options"] == "nosniff"
        assert client.put("/api/ingestion/settings",json={}).status_code == 403
        profile=client.post("/api/sources",headers=headers,json={
            "name":"API-only test", "api_base_url":"http://maxkb.test/admin/api", "api_key":"test-profile-key"
        })
        assert profile.status_code == 200, profile.text
        assert profile.json()["api_only"] and profile.json()["api_configured"]
        assert "test-profile-key" not in profile.text
        rejected_task=client.post("/api/tasks",headers=headers,json={"name":"invalid API scan","source_id":profile.json()["id"],"knowledge_ids":kids[:2]})
        assert rejected_task.status_code == 400
        applied=client.post("/api/ingestion/connection/"+profile.json()["id"],headers=headers)
        assert applied.status_code == 200,applied.text
        setting=client.get("/api/ingestion/settings")
        assert "test-profile-key" not in setting.text
        assert setting.json()["config"]["has_maxkb_token"]
        assert main.app.state.ingestion_app.state.store.settings()["maxkb_token"] == "test-profile-key"

        assert client.post("/api/logout").status_code == 403
        result = client.get(f"/api/runs/{rid}/pairs", params={"document": "document"})
        assert result.status_code == 200 and result.json()["total"] == 1
        assert (
            client.get("/api/operations").json()[0]["proposed"]["content"] == "new text"
        )
        # Password changes verify the current hash and revoke every old session.
        change={"current_password":"a-long-test-password","new_password":"new-long-test-password","confirm_password":"new-long-test-password"}
        assert client.post("/api/me/password",json=change).status_code == 403
        for values,status in [
            ({"current_password":"wrong-password"},400),
            ({"new_password":"short","confirm_password":"short"},422),
            ({"confirm_password":"different-password"},400),
            ({"new_password":"a-long-test-password","confirm_password":"a-long-test-password"},400),
        ]:
            result=client.post("/api/me/password",headers=headers,json={**change,**values})
            assert result.status_code == status,result.text
            assert client.get("/api/me").status_code == 200
        old_token=client.cookies.get("dedup_session")
        assert client.post("/api/login",headers=headers,json={"username":"test","password":"a-long-test-password"}).status_code == 200
        with sessions() as session:
            from app.models import LoginSession
            assert session.scalar(select(func.count()).select_from(LoginSession).where(LoginSession.user_id==user.id)) == 2
        result=client.post("/api/me/password",headers=headers,json=change)
        assert result.status_code == 200,result.text
        assert client.cookies.get("dedup_session") is None
        assert client.get("/api/me").status_code == 401
        assert client.get("/api/me",headers={"Cookie":"dedup_session="+old_token}).status_code == 401
        assert client.post("/api/me/password",headers=headers,json=change).status_code == 401
        with sessions() as session:
            account=session.get(User,user.id)
            assert account.password_hash != change["new_password"]
            assert passwords.verify(change["new_password"],account.password_hash)
            assert session.scalar(select(func.count()).select_from(LoginSession).where(LoginSession.user_id==user.id)) == 0
        assert client.post("/api/login",headers=headers,json={"username":"test","password":"a-long-test-password"}).status_code == 401
        assert client.post("/api/login",headers=headers,json={"username":"test","password":change["new_password"]}).status_code == 200
        assert client.post("/api/logout", headers=headers).status_code == 200
        assert client.get("/api/me").status_code == 401
    with db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(Pair)) == 1
        assert s.scalar(select(func.count()).select_from(Operation)) == 1
    # A queued run is consumed by the API itself; no external worker is started.
    from app.jobs import enqueue
    from app.runtime import BackgroundRuntime
    import time

    with db.transaction() as s:
        background_run = enqueue(s, s.get(Task, task_id))
        background_id = background_run.id
    with TestClient(main.app) as client:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with sessions() as s:
                background_state = s.get(Run, background_id).state
            if background_state in ["succeeded", "failed"]:
                break
            time.sleep(0.05)
        assert background_state == "succeeded"
        assert client.get("/api/health").status_code == 200
    import httpx
    from app import source_operations
    from app.maxkb_api import MaxKBAPI

    remote = {
        "id": live["paragraph_id"],
        "document_id": live["document_id"],
        "title": live["title"],
        "content": live["content"],
    }
    sent = []

    def handler(request):
        nonlocal remote
        sent.append(request.method)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "data": {
                        "total": int(remote is not None),
                        "records": [remote] if remote else [],
                    },
                },
            )
        if request.method == "PUT":
            import json

            remote.update(json.loads(request.content))
        if request.method == "DELETE":
            remote = None
        return httpx.Response(200, json={"code": 200, "data": remote})

    factory = lambda source: MaxKBAPI(source, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(main, "MaxKBAPI", factory)
    monkeypatch.setattr(source_operations, "MaxKBAPI", factory)
    with db.transaction() as s:
        configured = s.get(DataSource, sid)
        configured.api_base_url = "http://maxkb.test/admin/api"
        configured.api_key = encrypt("unit-test-key")
    changed = main.draft_operation(pair_id, body, user)
    assert changed["state"] == "draft"
    result = main.execute_source_operation(changed["id"], user)
    assert (
        result["state"] == "succeeded"
        and result["before"]["content"] == live["content"]
    )
    assert (
        result["proposed"]["content"] == "new text"
        and result["actual"]["content"] == "new text"
    )
    main.execute_source_operation(changed["id"], user)
    assert sent.count("PUT") == 1
    with sessions() as s:
        refreshed = main.current(pair_id, "left", s=s, user=user)["current"]
    deletion = main.draft_operation(
        pair_id,
        main.OperationBody(
            side="left",
            kind="delete_paragraph",
            expected_hash=refreshed["content_hash"],
        ),
        user,
    )
    result = main.execute_source_operation(deletion["id"], user)
    assert result["state"] == "succeeded" and result["actual"] is None
    assert result["before"]["content"] == "new text" and sent.count("DELETE") == 1
    # Document deletion: retain every paragraph, detect changes outside selected
    # paragraph, and reconcile a lost response without a second DELETE.
    document_rows = [
        {
            "id": live["paragraph_id"],
            "document_id": live["document_id"],
            "title": "first",
            "content": "first text",
            "position": 1,
        },
        {
            "id": str(uuid4()),
            "document_id": live["document_id"],
            "title": "second",
            "content": "second text",
            "position": 2,
        },
    ]
    document_deletes = []

    def document_handler(request):
        nonlocal document_rows
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "data": {"total": len(document_rows), "records": document_rows},
                },
            )
        assert request.method == "DELETE" and request.url.path.endswith(
            "/document/" + live["document_id"]
        )
        assert not request.url.query and not request.content
        document_deletes.append(request)
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute(
                "DELETE FROM paragraph WHERE document_id=%s", (live["document_id"],)
            )
            c.execute("DELETE FROM document WHERE id=%s", (live["document_id"],))
        document_rows = []
        raise httpx.ReadTimeout("lost acknowledgement", request=request)

    doc_factory = lambda source: MaxKBAPI(
        source, transport=httpx.MockTransport(document_handler)
    )
    monkeypatch.setattr(main, "MaxKBAPI", doc_factory)
    monkeypatch.setattr(source_operations, "MaxKBAPI", doc_factory)
    with sessions() as s:
        preview = main.document_content(pair_id, "left", s=s, user=user)
    body_document = main.OperationBody(
        side="left",
        kind="delete_document",
        expected_document_hash=preview["document_hash"],
    )
    document_op = main.draft_operation(pair_id, body_document, user)
    assert len(document_op["before"]["paragraphs"]) == 2
    document_rows[1]["content"] = "edited elsewhere"
    result = main.execute_source_operation(document_op["id"], user)
    assert result["state"] == "conflict" and not document_deletes
    with sessions() as s:
        preview = main.document_content(pair_id, "left", s=s, user=user)
    document_op = main.draft_operation(
        pair_id,
        main.OperationBody(
            side="left",
            kind="delete_document",
            expected_document_hash=preview["document_hash"],
        ),
        user,
    )
    result = main.execute_source_operation(document_op["id"], user)
    assert result["state"] == "unknown" and len(document_deletes) == 1
    result = main.reconcile_source_operation(document_op["id"], user)
    assert result["state"] == "succeeded" and result["actual"] is None
    assert result["before"]["paragraphs"][1]["content"] == "edited elsewhere"
    main.execute_source_operation(document_op["id"], user)
    assert len(document_deletes) == 1
    with psycopg.connect(dsn) as c:
        assert (
            c.execute(
                "SELECT count(*) FROM document WHERE id=%s", (live["document_id"],)
            ).fetchone()[0]
            == 0
        )
        assert c.execute("SELECT count(*) FROM document").fetchone()[0] == 3
    # Deleting a run removes its scan data but preserves independent text ledgers.
    from app.models import Decision, ScanEstimate
    with db.transaction() as s:
        pending_run = enqueue(s,s.get(Task,task_id))
        pending_id = pending_run.id
    for state in ["queued","running"]:
        with db.transaction() as s:s.get(Run,pending_id).state=state
        with pytest.raises(HTTPException) as rejected:
            main.delete_run(pending_id,user)
        assert rejected.value.status_code==409
    with db.transaction() as s:s.get(Run,pending_id).state="cancelled"
    assert main.delete_run(pending_id,user)["ok"]
    for state in ["executing","unknown"]:
        with db.transaction() as s:s.get(Operation,op["id"]).state=state
        with pytest.raises(HTTPException) as rejected:main.delete_run(rid,user)
        assert rejected.value.status_code==409
        with sessions() as s:assert s.get(Pair,pair_id) is not None
    with db.transaction() as s:s.get(Operation,op["id"]).state="unconfigured"
    with sessions() as s:
        ledgers={item.id:(item.before,item.proposed,item.actual) for item in s.scalars(select(Operation).where(Operation.pair_id==pair_id))}
    assert main.delete_run(rid,user)["ok"]
    with sessions() as s:
        assert s.get(Run,rid) is None and s.get(Task,task_id) is not None
        assert s.get(Run,old_rid) is not None and s.get(Run,background_id) is not None
        assert s.scalar(select(func.count()).select_from(Pair).where(Pair.run_id==rid))==0
        assert s.scalar(select(func.count()).select_from(ParagraphSnapshot).where(ParagraphSnapshot.run_id==rid))==0
        assert s.scalar(select(func.count()).select_from(Decision).where(Decision.pair_id==pair_id))==0
        assert s.scalar(select(func.count()).select_from(ScanEstimate).where(ScanEstimate.run_id==rid))==0
        for oid,texts in ledgers.items():
            item=s.get(Operation,oid)
            assert item.pair_id is None and item.target["scan_run_id"]==rid
            assert (item.before,item.proposed,item.actual)==texts
        assert all(item["id"]!=rid for item in main.runs(s=s,user=user))
    with pytest.raises(HTTPException) as missing:main.delete_run(rid,user)
    assert missing.value.status_code==404
    with pytest.raises(HTTPException) as archived:main.execute_source_operation(op["id"],user)
    assert archived.value.status_code==409
    with psycopg.connect(dsn) as c:
        assert c.execute("SELECT count(*) FROM document").fetchone()[0]==3
    engine.dispose()
