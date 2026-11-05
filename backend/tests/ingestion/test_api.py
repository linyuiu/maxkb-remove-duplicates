"""Public API contracts: validation, immutable intake and credential handling."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from .support import CONFIG, PDF, ident, plan



def test_credentials_are_not_returned_and_empty_is_distinct_from_omitted(workbench):
    client = workbench.client
    response = client.get("/api/ingestion/settings")
    assert "test-only-maxkb" not in response.text
    assert response.json()["config"]["has_maxkb_token"]
    assert client.put("/api/ingestion/settings", json={**CONFIG, "maxkb_token": None}).status_code == 200
    assert workbench.store.settings()["maxkb_token"] == CONFIG["maxkb_token"]
    assert client.put("/api/ingestion/settings", json={**CONFIG, "mineru_key": ""}).status_code == 200
    assert workbench.store.settings()["mineru_key"] == ""
    with workbench.store.connect() as db:
        stored=db.execute("SELECT config FROM settings WHERE id=1").fetchone()[0]
        assert CONFIG["maxkb_token"] not in stored


@pytest.mark.parametrize(
    "change",
    [
        {"batch_size": 0},
        {"batch_size": 101},
        {"limit": 0},
        {"import_timeout": 0},
        {"workspace_id": "../unsafe"},
        {"mineru_url": "file:///tmp"},
        {"mineru_url": "http://mineru.test/v1"},
        {"patterns": []},
        {"unexpected": True},
    ],
)
def test_invalid_settings_rejected_without_saving(workbench, change):
    old = workbench.store.settings()
    response = workbench.client.put("/api/ingestion/settings", json={**CONFIG, **change})
    assert response.status_code in {409, 422}
    assert workbench.store.settings() == old


@pytest.mark.parametrize("path", ["../a.pdf", "/a.pdf", "A/../a.pdf", "a.txt", "A//a.pdf"])
def test_invalid_manifest_is_rejected_before_queue_creation(workbench, path):
    response = workbench.client.post(
        "/api/ingestion/queues",
        json={
            "id": ident(),
            "config": CONFIG,
            "files": [{"path": path, "bytes": len(PDF)}],
            "knowledge_plan": plan(["A/a.pdf"]),
        },
    )
    assert response.status_code == 422
    assert workbench.store.queues() == []


def test_queue_replay_preserves_id_and_rejects_changed_plan(workbench):
    body, queue = workbench.queue()
    assert workbench.client.post("/api/ingestion/queues", json=body).json()["id"] == queue["id"]
    body["knowledge_plan"]["groups"][0]["tags"] = [{"key": "部门", "value": "研发"}]
    assert workbench.client.post("/api/ingestion/queues", json=body).status_code == 409
    assert len(workbench.store.queues()) == 1


def test_wrong_upload_never_becomes_a_received_original(workbench):
    body = {
        "id": ident(),
        "config": CONFIG,
        "knowledge_plan": plan(["A/a.pdf"]),
        "files": [{"path": "A/a.pdf", "bytes": len(PDF)}],
    }
    queue = workbench.client.post("/api/ingestion/queues", json=body).json()
    wave = queue["batches"][0]["id"]
    response = workbench.client.post(
        f"/api/ingestion/queues/{queue['id']}/batches/{wave}",
        files=[("files", ("A/a.pdf", PDF + b"changed"))],
    )
    assert response.status_code == 422
    assert workbench.store.claim() is None


def test_same_file_can_only_be_claimed_by_one_worker(workbench):
    workbench.queue()
    workbench.provision()
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(lambda _: workbench.store.claim(), range(4)))
    assert sum(row is not None for row in claims) == 1


def test_legacy_settings_do_not_reset_disabled_tag_switches(workbench):
    client = workbench.client
    disabled = {**CONFIG, "knowledge_tags_enabled": False, "document_tags_enabled": False}
    assert client.put("/api/ingestion/settings", json=disabled).status_code == 200
    assert client.put("/api/ingestion/settings", json=CONFIG).status_code == 200
    _, queue = workbench.queue()
    config = workbench.store.batch_config(queue["batches"][0]["id"])
    assert not config["knowledge_tags_enabled"] and not config["document_tags_enabled"]


def test_history_cleanup_requires_confirmation_and_current_revision(workbench):
    workbench.queue()
    client = workbench.client
    preview = client.get("/api/ingestion/history/cleanup").json()
    payload = {
        "revision": preview["revision"],
        "confirmation": "wrong",
        "delete_web_files": False,
        "delete_ssh_files": False,
    }
    assert client.post("/api/ingestion/history/cleanup", json=payload).status_code == 422
    payload["confirmation"] = "清除历史记录"
    payload["revision"] = "stale"
    assert client.post("/api/ingestion/history/cleanup", json=payload).status_code in {409, 422}
    payload["revision"] = preview["revision"]
    response = client.post("/api/ingestion/history/cleanup", json=payload)
    assert response.status_code == 200, response.text
    assert client.get("/api/ingestion/queues").json() == []


def test_browser_upload_without_target_cannot_inherit_an_unrelated_knowledge(workbench):
    response = workbench.client.post(
        "/api/ingestion/batches", data={"config": json.dumps(CONFIG)}, files=[("files", ("a.pdf", PDF))]
    )
    assert response.status_code == 422


@pytest.mark.parametrize("view", ["tasks", "batches", "files"])
def test_history_groups_and_searches_real_uploaded_files(workbench, view):
    _, queue = workbench.queue(["A/unique-manual.pdf", "B/other.pdf"])
    response = workbench.client.get(
        "/api/ingestion/history", params={"view": view, "search": "unique-manual"}
    )
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 1
    if view == "tasks":
        assert items[0]["id"] == queue["id"]
    elif view == "batches":
        assert items[0]["id"] == queue["batches"][0]["id"]
    else:
        assert "unique-manual" in response.text
    assert (
        workbench.client.get(
            "/api/ingestion/history", params={"view": view, "search": "does-not-exist"}
        ).json()["items"]
        == []
    )
    assert CONFIG["maxkb_token"] not in response.text


def test_history_cleanup_is_blocked_while_source_task_is_active(workbench):
    sid = workbench.ssh_source()
    assert (
        workbench.client.post(f"/api/ingestion/ssh-sources/{sid}/scan", json={"id": ident()}).status_code
        == 202
    )
    preview = workbench.client.get("/api/ingestion/history/cleanup").json()
    assert not preview["can_clear"]

