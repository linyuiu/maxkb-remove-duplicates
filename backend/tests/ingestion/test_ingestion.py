"""Exercise actual upload → provision → parse/split → import → monitor state transitions."""

import json

import pytest
from .support import CONFIG, PDF, ident, office, plan

from app.ingestion.knowledge import KnowledgeProvisioner


@pytest.mark.parametrize(
    "name,content",
    [
        ("A/manual.pdf", PDF),
        ("A/manual.docx", office("word")),
        ("A/table.xlsx", office("excel")),
    ],
    ids=["pdf", "word", "excel"],
)
def test_import_formats_through_public_intake_and_workers(workbench, name, content):
    _, queue = workbench.queue([name], contents={name: content})
    workbench.provision()
    workbench.prepare()
    wave = queue["batches"][0]["id"]
    file = workbench.store.snapshot(wave)["files"][0]
    assert file["status"] == "review", file
    workbench.import_wave(wave)
    assert workbench.store.file(file["id"])["status"] == "imported"
    provenance = workbench.client.get(f"/api/ingestion/files/{file['id']}/provenance").json()
    assert provenance["source"]["sha256"]
    assert len(workbench.upstream.documents) == 1
    requests = workbench.upstream.requests
    if name.endswith(".pdf"):
        assert any(r.url.host == "mineru.test" for r in requests)
    else:
        assert all(r.url.host != "mineru.test" for r in requests)


def test_lost_import_response_is_not_automatically_recreated(workbench):
    _, queue = workbench.queue()
    workbench.provision()
    workbench.prepare()
    workbench.upstream.create_outcome = "lost"
    wave = queue["batches"][0]["id"]
    workbench.import_wave(wave)
    file = workbench.store.snapshot(wave)["files"][0]
    assert file["status"] == "import_unknown"
    assert workbench.store.claim(True) is None
    creates = [r for r in workbench.upstream.requests if r.url.path.endswith("/batch_create")]
    assert len(creates) == 1 and len(workbench.upstream.documents) == 1


def test_successful_import_waits_for_tags_and_retry_only_rebinds_labels(workbench):
    tag = {"key": "部门", "value": "研发"}
    p = plan(["A/a.pdf"])
    p["groups"][0]["tags"] = [tag]
    _, queue = workbench.queue(knowledge_plan=p)
    workbench.provision()
    workbench.prepare()
    workbench.upstream.tag_failure = True
    wave = queue["batches"][0]["id"]
    workbench.import_wave(wave)
    file = workbench.store.snapshot(wave)["files"][0]
    assert file["status"] == "import_unknown"
    before = len(workbench.upstream.documents)
    workbench.upstream.tag_failure = False
    response = workbench.client.post(f"/api/ingestion/files/{file['id']}/tags/retry")
    assert response.status_code == 200, response.text
    row = workbench.store.claim_monitor()
    with workbench.upstream.factory() as client:
        workbench.workers.monitor(row, client)
    assert workbench.store.file(file["id"])["status"] == "imported"
    assert len(workbench.upstream.documents) == before
    assert workbench.store.tag_receipts(file["id"])


def test_failed_remote_processing_cleans_only_owned_document(workbench):
    _, queue = workbench.queue()
    workbench.provision()
    workbench.prepare()
    workbench.upstream.document_state = "2nn3"
    wave = queue["batches"][0]["id"]
    workbench.import_wave(wave)
    file = workbench.store.snapshot(wave)["files"][0]
    assert file["status"] in {"import_failed", "cleanup_pending"}, file
    if file["status"] == "cleanup_pending":
        with workbench.upstream.factory() as client:
            workbench.workers.monitor(workbench.store.claim_monitor(), client)
    assert workbench.store.file(file["id"])["status"] == "import_failed"
    assert not workbench.upstream.documents


def test_original_digest_change_prevents_processing(workbench):
    _, queue = workbench.queue()
    workbench.provision()
    file = workbench.store.snapshot(queue["batches"][0]["id"])["files"][0]
    (workbench.store.directory(file["id"]) / file["name"]).write_bytes(PDF + b"modified")
    workbench.prepare()
    assert workbench.store.file(file["id"])["status"] == "failed"
    assert not workbench.upstream.documents
    assert not any(r.url.host == "mineru.test" for r in workbench.upstream.requests)


def test_one_wave_does_not_activate_before_previous_import_is_verified(workbench):
    _, queue = workbench.queue(["A/a.pdf", "A/b.pdf"], config={"batch_size": 1})
    workbench.provision()
    workbench.prepare()
    first, second = queue["batches"]
    assert workbench.store.snapshot(second["id"])["files"][0]["status"] == "waiting_wave"
    workbench.import_wave(first["id"])
    workbench.prepare()
    assert workbench.store.snapshot(second["id"])["files"][0]["status"] == "review"


def existing(ident="existing", folder="default", name="A"):
    return {"id": ident, "name": name, "folder_id": folder, "workspace_id": "default", "type": 0}


def test_unique_existing_knowledge_is_reused(workbench):
    workbench.upstream.knowledge["existing"] = existing()
    _, queue = workbench.queue()
    workbench.provision()
    target = workbench.store.knowledge_targets(queue["id"])[0]
    assert target["reused"] and target["knowledge_id"] == "existing"
    assert not any(
        r.method == "POST" and r.url.path.endswith("/knowledge/base")
        for r in workbench.upstream.requests
    )


def test_ambiguous_knowledge_requires_a_real_manual_choice(workbench):
    workbench.upstream.knowledge = {key: existing(key) for key in ("old", "new")}
    _, queue = workbench.queue()
    workbench.provision()
    target = workbench.store.knowledge_targets(queue["id"])[0]
    assert len(target["candidates"]) == 2 and not target["knowledge_id"]
    assert workbench.store.claim() is None
    response = workbench.client.post(
        f"/api/ingestion/queues/{queue['id']}/knowledge/retry", json={"folder": "A", "knowledge_id": "old"}
    )
    assert response.status_code == 200, response.text
    workbench.provision()
    assert workbench.store.knowledge_targets(queue["id"])[0]["knowledge_id"] == "old"
    assert len(workbench.upstream.knowledge) == 2


@pytest.mark.parametrize("field,value", [("folder_id", "elsewhere"), ("workspace_id", "other")])
def test_knowledge_scope_is_not_crossed(workbench, field, value):
    workbench.upstream.knowledge["existing"] = {**existing(), field: value}
    _, queue = workbench.queue()
    workbench.provision()
    target = workbench.store.knowledge_targets(queue["id"])[0]
    assert not target["reused"] and target["knowledge_id"] != "existing"


def test_failed_lookup_does_not_create_duplicate_knowledge(workbench):
    workbench.upstream.overrides.append(
        lambda r: (
            workbench.upstream.response(None, 500) if r.url.path.endswith("/knowledge") else None
        )
    )
    _, queue = workbench.queue()
    workbench.provision()
    assert workbench.store.knowledge_targets(queue["id"])[0]["status"] == "failed"
    assert not workbench.upstream.knowledge


@pytest.mark.parametrize(
    "knowledge,document,inherit",
    [
        (True, True, True),
        (True, True, False),
        (False, True, True),
        (True, False, True),
        (False, False, False),
    ],
)
def test_tag_switches_and_per_document_rules_are_independent(
    workbench, knowledge, document, inherit
):
    paths = ["A/a.pdf", "A/b.pdf"]
    p = plan(paths)
    p["groups"][0]["tags"] = [{"key": "部门", "value": "研发"}]
    p["document_rules"] = [{"paths": [paths[0]], "tags": [{"key": "用途", "value": "培训"}]}]
    _, queue = workbench.queue(
        paths,
        knowledge_plan=p,
        config={
            "knowledge_tags_enabled": knowledge,
            "document_tags_enabled": document,
            "inherit_knowledge_tags": inherit,
        },
    )
    workbench.provision()
    target = workbench.store.knowledge_targets(queue["id"])[0]
    lookup = workbench.upstream.tags.get(target["knowledge_id"], {})
    assert (("部门", "研发") in lookup) == knowledge
    assert (("用途", "培训") in lookup) == document
    files = sorted(
        workbench.store.snapshot(queue["batches"][0]["id"])["files"], key=lambda f: f["name"]
    )
    assigned = [set(workbench.store.file_config(f["id"])["document_tag_ids"]) for f in files]
    base = {lookup[("部门", "研发")]} if knowledge and document and inherit else set()
    extra = {lookup[("用途", "培训")]} if document else set()
    assert assigned == [base | extra, base]


def test_tag_rules_reject_unselected_paths(workbench):
    response = workbench.client.post(
        "/api/ingestion/queues",
        json={
            "id": ident(),
            "config": workbench.config,
            "files": [{"path": "A/a.pdf", "bytes": len(PDF)}],
            "knowledge_plan": plan(
                ["A/a.pdf"],
                document_rules=[{"paths": ["A/missing.pdf"], "tags": [{"key": "k", "value": "v"}]}],
            ),
        },
    )
    assert response.status_code == 409
    assert "未选择" in response.json()["detail"]


def test_batch_tags_use_the_correct_payload_and_resume_only_failed_chunks(workbench):
    from app.ingestion.pipeline import MaxKBClient, PipelineError
    from app.ingestion.workers import settings_from

    workbench.upstream.knowledge["existing"] = existing()
    _, queue = workbench.queue()
    workbench.provision()
    file = workbench.store.snapshot(queue["batches"][0]["id"])["files"][0]
    file["attempt_id"] = "attempt-test"
    settings = settings_from(
        {**workbench.store.file_config(file["id"]), "document_tag_ids": ("t1", "t1")}
    )
    count = [0]

    def fail_second(request):
        if request.url.path.endswith("/batch_add_tag"):
            count[0] += 1
            if count[0] == 2:
                return workbench.upstream.response(None, 500)
        return None

    workbench.upstream.overrides.append(fail_second)
    docs = [{"document_id": f"d-{i}"} for i in range(105)]
    with workbench.upstream.factory() as client:
        api = MaxKBClient(settings, client)
        with pytest.raises(PipelineError, match="标签绑定未确认"):
            workbench.workers.bind_document_tags(file, docs, settings, api)
        workbench.workers.bind_document_tags(file, docs, settings, api)
    payloads = [
        json.loads(r.content)
        for r in workbench.upstream.requests
        if r.url.path.endswith("/batch_add_tag")
    ]
    assert [len(p["document_ids"]) for p in payloads] == [100, 5, 5]
    assert all(p["tag_ids"] == ["t1"] for p in payloads)


def test_matching_phase_recovers_without_new_creation(workbench):
    workbench.upstream.knowledge["existing"] = existing()
    _, queue = workbench.queue()
    with workbench.store.connect() as db:
        db.execute("UPDATE knowledge_targets SET status='matching'")
    KnowledgeProvisioner(workbench.workers).recover()
    workbench.provision()
    assert len(workbench.upstream.knowledge) == 1
    assert workbench.store.knowledge_targets(queue["id"])[0]["reused"]


def test_preview_and_parse_records_use_actual_processed_output(workbench):
    _, queue = workbench.queue()
    workbench.provision()
    workbench.prepare()
    file = workbench.store.snapshot(queue["batches"][0]["id"])["files"][0]
    response = workbench.client.get(f"/api/ingestion/files/{file['id']}/preview")
    assert response.status_code == 200, response.text
    assert "Text" in response.text
    records = workbench.client.get("/api/ingestion/mineru/files", params={"batch_id": file["batch_id"]})
    assert records.status_code == 200 and file["name"] in records.text
    assert CONFIG["maxkb_token"] not in records.text and CONFIG["mineru_key"] not in records.text
