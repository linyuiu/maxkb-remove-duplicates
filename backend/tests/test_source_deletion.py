"""Source removal against an isolated schema in the disposable test database."""

import os
from types import SimpleNamespace

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import pytest
from sqlalchemy.orm import sessionmaker

from test_source_operations import connection

pytestmark = pytest.mark.skipif(
    os.environ.get("DEDUP_DISPOSABLE_TEST_DB") != "1",
    reason="Requires disposable test socket server",
)
HEADERS = {"X-Requested-With": "dedup"}


@pytest.fixture
def source_api(connection, monkeypatch):
    from app import db, main
    from app.models import DataSource, User
    from app.security import encrypt, passwords

    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    sessions = sessionmaker(bind=connection, expire_on_commit=False)
    monkeypatch.setattr(db, "Session", sessions)
    monkeypatch.setattr(main, "Session", sessions)
    with sessions.begin() as s:
        account = User(id="user", username="remove-test", password_hash=passwords.hash("test-long-password"))
        source = DataSource(
            id="source", name="remove test",
            connection=encrypt("host=unused.invalid port=5432 dbname=test user=test password=fake-password"),
            api_base_url="http://maxkb.test/admin/api", api_key=encrypt("fake-key"),
        )
        s.add_all([account, source])
    client = TestClient(main.app)
    assert client.post("/api/login", headers=HEADERS, json={
        "username": "remove-test", "password": "test-long-password"
    }).status_code == 200
    yield SimpleNamespace(client=client, sessions=sessions, main=main)
    client.close()


def test_delete_auth_confirmation_effect_and_idempotence(source_api):
    from app.models import DataSource
    api = source_api
    anonymous = TestClient(api.main.app)
    try:
        assert anonymous.delete("/api/sources/source", headers=HEADERS).status_code == 401
    finally:
        anonymous.close()
    assert api.client.delete("/api/sources/source").status_code == 403
    assert api.client.delete("/api/sources/source", headers=HEADERS).status_code == 200
    assert api.client.delete("/api/sources/source", headers=HEADERS).status_code == 200
    assert api.client.delete("/api/sources/missing", headers=HEADERS).status_code == 404
    assert api.client.get("/api/sources").json() == []
    with api.sessions() as s:
        row = s.get(DataSource, "source")
        assert row.deleted_at is not None and not row.enabled
        assert row.connection == row.api_base_url == row.api_key == ""
    for url in ("/api/sources/source/test", "/api/ingestion/connection/source"):
        assert api.client.post(url, headers=HEADERS).status_code == 404
    assert api.client.get("/api/sources/source/knowledge").status_code == 404
    assert api.client.put("/api/sources/source", headers=HEADERS, json={
        "name": "restore", "api_base_url": "http://maxkb.test/admin/api", "enabled": True
    }).status_code == 404


def test_source_referenced_by_task_requires_removing_task_first(source_api):
    from app.models import DataSource, Task
    api = source_api
    with api.sessions.begin() as s:
        s.add(Task(id="task", name="task", source_id="source", knowledge_ids=[]))
    result = api.client.delete("/api/sources/source", headers=HEADERS)
    assert result.status_code == 409 and "扫描任务" in result.json()["detail"]
    with api.sessions() as s:
        assert s.get(DataSource, "source").deleted_at is None
    assert api.client.delete("/api/tasks/task", headers=HEADERS).status_code == 200
    assert api.client.delete("/api/sources/source", headers=HEADERS).status_code == 200
    with api.sessions() as s:
        assert s.get(Task, "task") is not None


@pytest.mark.parametrize("state", ["queued", "running"])
def test_active_scan_blocks_deletion_until_finished(source_api, state):
    from app.models import Task, Run, now
    api = source_api
    with api.sessions.begin() as s:
        s.add(Task(id="task", name="task", source_id="source", knowledge_ids=[], deleted_at=now()))
        s.flush()
        s.add(Run(id="run", task_id="task", config={"source_id": "source"}, state=state))
    result = api.client.delete("/api/sources/source", headers=HEADERS)
    assert result.status_code == 409 and "扫描" in result.json()["detail"]
    with api.sessions.begin() as s:
        s.get(Run, "run").state = "cancelled"
    assert api.client.delete("/api/sources/source", headers=HEADERS).status_code == 200


@pytest.mark.parametrize("state", ["executing", "unknown"])
def test_unfinished_mutation_blocks_deletion(source_api, state):
    from app.models import Operation
    api = source_api
    with api.sessions.begin() as s:
        s.add(Operation(id="op", user_id="user", target={"source_id": "source"},
                        kind="delete_document", state=state))
    result = api.client.delete("/api/sources/source", headers=HEADERS)
    assert result.status_code == 409 and "操作" in result.json()["detail"]
    with api.sessions.begin() as s:
        s.get(Operation, "op").state = "failed"
    assert api.client.delete("/api/sources/source", headers=HEADERS).status_code == 200


def test_deletion_keeps_history_and_snapshots_readable(source_api):
    from app.models import Task, Run, Pair, ParagraphSnapshot, Operation, now
    api = source_api
    with api.sessions.begin() as s:
        s.add(Task(id="task", name="task", source_id="source", knowledge_ids=[], deleted_at=now()))
        s.flush()
        s.add(Run(id="run", task_id="task", config={"source_id": "source"}, state="succeeded"))
        s.flush()
        s.add(Pair(id="pair", run_id="run", left_id="left", right_id="right",
                   left_embedding_id="e1", right_embedding_id="e2", similarity=1))
        for paragraph_id in ("left", "right"):
            s.add(ParagraphSnapshot(run_id="run", paragraph_id=paragraph_id, data={
                "paragraph_id": paragraph_id, "title": "title", "content": "snapshot text",
                "knowledge_id": "knowledge", "knowledge_name": "Knowledge",
                "document_id": paragraph_id, "document_name": "Document", "workspace_id": "default",
            }))
        s.flush()
        s.add(Operation(id="op", pair_id="pair", user_id="user",
                        target={"source_id": "source", "document_id": "left"},
                        kind="update_paragraph", state="draft"))
    assert api.client.delete("/api/sources/source", headers=HEADERS).status_code == 200
    result = api.client.get("/api/runs/run/pairs")
    assert result.status_code == 200 and result.json()["total"] == 1
    assert "snapshot text" in result.text
    assert api.client.get("/api/runs/run/knowledge").status_code == 200
    with api.sessions() as s:
        assert s.get(Task, "task") and s.get(Run, "run") and s.get(Pair, "pair")
        assert s.get(Operation, "op").state == "draft"
    # Source access from retained history is no longer permitted.
    assert api.client.get("/api/pairs/pair/current/left").status_code == 404


def test_additive_migration_preserves_existing_source(connection):
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    connection.exec_driver_sql("ALTER TABLE data_sources DROP COLUMN deleted_at")
    connection.exec_driver_sql("INSERT INTO data_sources(id,name,connection,api_base_url,api_key,enabled,created_at) VALUES ('existing','Existing','encrypted','https://maxkb.test','encrypted-key',true,now())")
    path = Path(__file__).parents[1] / "migrations/versions/0009_source_deletion.py"
    spec = importlib.util.spec_from_file_location("source_deletion_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with Operations.context(MigrationContext.configure(connection)):
        migration.upgrade()
    row = connection.exec_driver_sql("SELECT name,connection,enabled,deleted_at FROM data_sources WHERE id='existing'").one()
    assert tuple(row) == ("Existing", "encrypted", True, None)
    connection.commit()
