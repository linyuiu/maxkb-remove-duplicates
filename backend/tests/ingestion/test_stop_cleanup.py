"""Stopped failures can be removed locally, but running threads must drain first."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest


def first_file(workbench, queue):
    return workbench.store.snapshot(queue["batches"][0]["id"])["files"][0]


def clear(workbench, **options):
    preview = workbench.client.get("/api/ingestion/history/cleanup").json()
    response = workbench.client.post(
        "/api/ingestion/history/cleanup",
        json={
            "revision": preview["revision"],
            "confirmation": "清除历史记录",
            **options,
        },
    )
    return preview, response


def split_failure(workbench):
    workbench.upstream.overrides.append(
        lambda r: (
            workbench.upstream.response(None, 500)
            if r.url.path.endswith("/document/split")
            else None
        )
    )
    _, queue = workbench.queue()
    workbench.provision()
    workbench.prepare()
    file = first_file(workbench, queue)
    assert file["status"] == "preparation_unknown"
    return queue, file


def test_failed_split_can_be_stopped_and_cleared_without_remote_operations(workbench):
    _, file = split_failure(workbench)
    stopped = workbench.client.post(f"/api/ingestion/files/{file['id']}/stop")
    assert stopped.status_code == 200 and stopped.json()["status"] == "stopped"
    requests = len(workbench.upstream.requests)
    preview, response = clear(workbench, delete_web_files=True)
    assert preview["can_clear"] and preview["warning_count"] == 1
    assert response.status_code == 200, response.text
    assert workbench.client.get("/api/ingestion/queues").json() == []
    assert len(workbench.upstream.requests) == requests
    with workbench.store.connect() as db:
        audit = json.loads(db.execute("SELECT payload FROM history_cleanups").fetchone()[0])
    assert audit["stopped_or_uncertain"][0]["status"] == "stopped"
    assert audit["remote_calls"][0]["state"] == "unknown"


def test_uncertain_split_can_be_cleared_directly_as_local_history(workbench):
    split_failure(workbench)
    preview, response = clear(workbench)
    assert preview["can_clear"] and response.status_code == 200


def test_lost_import_response_does_not_require_remote_deletion_to_clear_history(workbench):
    _, queue = workbench.queue()
    workbench.provision()
    workbench.prepare()
    workbench.upstream.create_outcome = "lost"
    workbench.import_wave(queue["batches"][0]["id"])
    file = first_file(workbench, queue)
    assert file["status"] == "import_unknown"
    assert workbench.client.post(f"/api/ingestion/queues/{queue['id']}/stop").status_code == 200
    preview, response = clear(workbench)
    assert preview["can_clear"] and response.status_code == 200
    assert len(workbench.upstream.documents) == 1
    assert not any(r.method == "DELETE" for r in workbench.upstream.requests)


def test_stopping_queue_prevents_new_claims_and_knowledge_creation(workbench):
    _, queue = workbench.queue(["A/a.pdf", "B/b.pdf"], config={"batch_size": 1})
    assert workbench.client.post(f"/api/ingestion/queues/{queue['id']}/stop").status_code == 200
    workbench.provision()
    assert workbench.store.claim() is None and workbench.store.claim(splitting=True) is None
    assert workbench.store.queue_snapshot(queue["id"])["state"] == "stopped"
    assert not workbench.upstream.knowledge
    assert all(
        first_file(workbench, {"batches": [wave]})["status"] == "stopped"
        for wave in queue["batches"]
    )
    assert clear(workbench)[1].status_code == 200


def test_running_request_must_exit_before_history_is_deleted(workbench):
    _, queue = workbench.queue()
    workbench.provision()
    row = workbench.store.claim()
    entered, release = threading.Event(), threading.Event()

    def pause(request):
        if request.url.path == "/v1/uploads":
            entered.set()
            assert release.wait(5)
        return None

    workbench.upstream.overrides.append(pause)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(workbench.workers.execute, row, False)
        try:
            assert entered.wait(5)
            response = workbench.client.post(f"/api/ingestion/files/{row['id']}/stop")
            assert response.json()["stop_requested"]
            assert response.json()["status"] != "stopped"
            preview, response = clear(workbench)
            assert not preview["can_clear"] and response.status_code == 409
        finally:
            release.set()
        future.result(timeout=5)
    assert workbench.store.file(row["id"])["status"] == "stopped"
    assert workbench.store.claim_monitor() is None
    assert clear(workbench)[1].status_code == 200
    assert not workbench.upstream.documents


def test_stopping_during_knowledge_lookup_does_not_create_a_library(workbench):
    from app.ingestion.knowledge import KnowledgeProvisioner

    _, queue = workbench.queue()
    entered, release = threading.Event(), threading.Event()

    def pause(request):
        if request.url.path.endswith("/knowledge"):
            entered.set()
            assert release.wait(5)
        return None

    workbench.upstream.overrides.append(pause)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(KnowledgeProvisioner(workbench.workers).run_one)
        try:
            assert entered.wait(5)
            assert workbench.client.post(f"/api/ingestion/queues/{queue['id']}/stop").status_code == 200
            assert not workbench.client.get("/api/ingestion/history/cleanup").json()["can_clear"]
        finally:
            release.set()
        future.result(timeout=5)
    assert not workbench.upstream.knowledge
    assert clear(workbench)[1].status_code == 200


@pytest.mark.parametrize("scope", ["files", "batches", "queues"])
def test_stop_endpoint_is_idempotent_and_missing_targets_return_404(workbench, scope):
    _, queue = workbench.queue()
    file = first_file(workbench, queue)
    ident = (
        file["id"]
        if scope == "files"
        else queue["batches"][0]["id"]
        if scope == "batches"
        else queue["id"]
    )
    for _ in range(2):
        assert workbench.client.post(f"/api/ingestion/{scope}/{ident}/stop").status_code == 200
    assert first_file(workbench, queue)["status"] == "stopped"
    from .support import ident as new_id

    assert workbench.client.post(f"/api/ingestion/{scope}/{new_id()}/stop").status_code == 404


def test_archiving_failure_does_not_permanently_block_local_cleanup(workbench):
    from .support import PDF
    from .test_sources import import_source

    original = workbench.mapped / "a.pdf"
    original.write_bytes(PDF)
    file = import_source(workbench, "mapped")
    archive = workbench.mapped / "已处理文档/a.pdf"
    archive.parent.mkdir()
    archive.write_bytes(b"preserve conflicting file")
    workbench.sources.run_one("archive")
    assert workbench.client.get("/api/ingestion/source-archives").json()[0]["status"] == "failed"
    assert clear(workbench)[1].status_code == 200
    assert original.read_bytes() == PDF and archive.read_bytes() == b"preserve conflicting file"
    assert file["status"] == "imported"


def test_stop_cancels_pending_archiving_without_moving_original(workbench):
    from .support import PDF
    from .test_sources import import_source

    original = workbench.mapped / "a.pdf"
    original.write_bytes(PDF)
    file = import_source(workbench, "mapped")
    assert workbench.client.post(f"/api/ingestion/files/{file['id']}/stop").status_code == 200
    assert not workbench.sources.run_one("archive")
    assert workbench.client.get("/api/ingestion/source-archives").json()[0]["status"] == "canceled"
    assert clear(workbench)[1].status_code == 200
    assert original.read_bytes() == PDF


def test_stopped_tasks_remain_stopped_after_recovery(workbench):
    _, queue = workbench.queue()
    file = first_file(workbench, queue)
    assert workbench.client.post(f"/api/ingestion/queues/{queue['id']}/stop").status_code == 200
    workbench.store.recover()
    assert workbench.store.file(file["id"])["status"] == "stopped"
    assert workbench.store.claim() is None
    wave = queue["batches"][0]["id"]
    from .support import PDF

    response = workbench.client.post(
        f"/api/ingestion/queues/{queue['id']}/batches/{wave}", files=[("files", ("A/a.pdf", PDF))]
    )
    assert response.status_code == 409
