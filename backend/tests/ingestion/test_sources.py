"""SSH/mapped source contracts, including real import before source archiving."""

import os
from pathlib import Path

import pytest
from .support import PDF, SSH, ident, plan


def choose(client, sid):
    response = client.get(f"/api/ingestion/source-directories/{sid}/files")
    assert response.status_code == 200, response.text
    return [{key: f[key] for key in ("path", "bytes", "mtime")} for f in response.json()["files"]]


def import_source(workbench, sid):
    selected = choose(workbench.client, sid)
    response = workbench.client.post(
        f"/api/ingestion/source-directories/{sid}/import",
        json={
            "id": ident(),
            "files": selected,
            "config": workbench.config,
            "knowledge_plan": plan([f["path"] for f in selected]),
        },
    )
    assert response.status_code == 202, response.text
    workbench.sources.run_one("import")
    workbench.provision()
    workbench.prepare()
    queue = workbench.store.queue_snapshot(response.json()["queue_id"])
    for wave in queue["batches"]:
        workbench.import_wave(wave["id"])
    return workbench.store.snapshot(queue["batches"][0]["id"])["files"][0]


def test_remote_scan_reads_metadata_without_downloading(workbench):
    (workbench.remote.root / "new.pdf").write_bytes(PDF)
    archive = workbench.remote.root / "已处理文档"
    archive.mkdir()
    (archive / "old.pdf").write_bytes(PDF)
    sid = workbench.ssh_source()
    job = workbench.source_job(sid, "scan")
    assert job["status"] == "completed"
    result = workbench.client.get(f"/api/ingestion/ssh-sources/{sid}/scan").json()
    assert result["counts"] == {"pending": 1}
    assert result["files"][0]["path"] == "new.pdf"
    assert result["scanned_at"] > 0 and not workbench.remote.reads
    assert not (workbench.store.root / "sync").exists()
    assert workbench.client.get(f"/api/ingestion/source-directories/{sid}/files").status_code == 409


def test_every_remote_file_is_pending_and_redownloaded_without_historical_skip(workbench):
    for name in ("same.pdf", "changed.pdf", "corrupt.pdf", "missing.pdf"):
        (workbench.remote.root / name).write_bytes(PDF)
    sid = workbench.ssh_source()
    assert workbench.source_job(sid, "sync")["status"] == "completed"
    cache = workbench.sources.directory(sid)
    (workbench.remote.root / "changed.pdf").write_bytes(PDF + b"change")
    corrupt = cache / "corrupt.pdf"
    info = corrupt.stat()
    corrupt.write_bytes(b"X" * len(PDF))
    os.utime(corrupt, ns=(info.st_atime_ns, info.st_mtime_ns))
    (cache / "missing.pdf").unlink()
    (workbench.remote.root / "new.pdf").write_bytes(PDF)
    reads = len(workbench.remote.reads)
    workbench.source_job(sid, "scan")
    result = workbench.client.get(f"/api/ingestion/ssh-sources/{sid}/scan").json()
    assert result["counts"] == {"pending": 5}
    assert len(workbench.remote.reads) == reads
    assert workbench.source_job(sid, "sync")["skipped"] == 0
    downloaded = {path for path, _, _ in workbench.remote.reads[reads:]}
    assert downloaded == {
        "/documents/" + name
        for name in ("same.pdf", "changed.pdf", "corrupt.pdf", "missing.pdf", "new.pdf")
    }
    reads = len(workbench.remote.reads)
    assert workbench.source_job(sid, "sync")["skipped"] == 0
    assert {path for path, _, _ in workbench.remote.reads[reads:]} == downloaded


def test_scan_failure_preserves_last_success_and_can_be_retried(workbench):
    sid = workbench.ssh_source()
    workbench.source_job(sid, "scan")
    previous = workbench.client.get(f"/api/ingestion/ssh-sources/{sid}/scan").json()
    workbench.remote.fail_listing = True
    failed = workbench.source_job(sid, "scan")
    assert failed["status"] == "failed"
    assert workbench.client.get(f"/api/ingestion/ssh-sources/{sid}/scan").json() == previous
    workbench.remote.fail_listing = False
    assert workbench.client.post(f"/api/ingestion/source-jobs/{failed['id']}/retry").status_code == 202
    assert workbench.sources.run_one("scan")
    assert workbench.sources.job(failed["id"])["status"] == "completed"


def test_active_scan_excludes_second_task_and_allows_cancel(workbench):
    sid, jid = workbench.ssh_source(), ident()
    client = workbench.client
    assert client.post(f"/api/ingestion/ssh-sources/{sid}/scan", json={"id": jid}).status_code == 202
    assert client.post(f"/api/ingestion/ssh-sources/{sid}/sync", json={"id": ident()}).status_code == 409
    assert client.post(f"/api/ingestion/source-jobs/{jid}/cancel").status_code == 200
    assert not workbench.sources.run_one("scan")
    assert workbench.source_job(sid, "sync")["status"] == "completed"


def test_interrupted_sync_reconnects_and_completes_without_truncation(workbench):
    content = PDF + b"x" * 100000
    (workbench.remote.root / "big.pdf").write_bytes(content)
    workbench.remote.fail_read_at = 32768
    sid = workbench.ssh_source()
    job = workbench.source_job(sid, "sync")
    assert job["status"] == "completed"
    assert workbench.remote.connections >= 2
    assert (workbench.sources.directory(sid) / "big.pdf").read_bytes() == content


@pytest.mark.parametrize("kind", ["mapped", "ssh"])
def test_successful_import_archives_original_and_hides_cached_version(workbench, kind):
    root = workbench.mapped if kind == "mapped" else workbench.remote.root
    (root / "部门/设备").mkdir(parents=True)
    original = root / "部门/设备/a.pdf"
    original.write_bytes(PDF)
    sid = "mapped" if kind == "mapped" else workbench.ssh_source()
    if kind == "ssh":
        assert workbench.source_job(sid, "sync")["status"] == "completed"
    file = import_source(workbench, sid)
    assert file["status"] == "imported", file
    assert workbench.sources.run_one("archive")
    assert not original.exists()
    assert (root / "已处理文档/部门/设备/a.pdf").read_bytes() == PDF
    result = workbench.client.get(f"/api/ingestion/source-directories/{sid}/files").json()
    assert result["files"] == [] and result["bytes"] == 0
    assert workbench.client.get("/api/ingestion/source-archives").json()[0]["status"] == "completed"


@pytest.mark.parametrize("failure", ["conflict", "changed", "symlink"])
def test_archive_failure_preserves_source_and_never_reimports(workbench, failure):
    original = workbench.mapped / "a.pdf"
    original.write_bytes(PDF)
    file = import_source(workbench, "mapped")
    target = workbench.mapped / "已处理文档/a.pdf"
    if failure == "conflict":
        target.parent.mkdir()
        target.write_bytes(b"keep existing")
    elif failure == "changed":
        original.write_bytes(PDF + b"new version")
    else:
        outside = workbench.mapped.parent / "outside"
        outside.mkdir()
        target.parent.symlink_to(outside, target_is_directory=True)
    assert workbench.sources.run_one("archive")
    record = workbench.client.get("/api/ingestion/source-archives").json()[0]
    assert record["status"] == "failed" and original.exists()
    assert workbench.store.file(file["id"])["status"] == "imported"
    assert len(workbench.upstream.documents) == 1
    if failure == "conflict":
        assert target.read_bytes() == b"keep existing"
        target.unlink()
        assert workbench.client.post(f"/api/ingestion/source-archives/{file['id']}/retry").status_code == 202
        workbench.sources.run_one("archive")
        assert not original.exists() and len(workbench.upstream.documents) == 1


def test_published_archive_resumes_source_removal_after_failure(workbench, monkeypatch):
    original = workbench.mapped / "a.pdf"
    original.write_bytes(PDF)
    file = import_source(workbench, "mapped")
    unlink = Path.unlink

    def denied(path, *args, **kwargs):
        if path == original:
            raise PermissionError("deny source removal")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", denied)
        workbench.sources.run_one("archive")
    assert original.exists() and (workbench.mapped / "已处理文档/a.pdf").exists()
    assert workbench.client.post(f"/api/ingestion/source-archives/{file['id']}/retry").status_code == 202
    workbench.sources.run_one("archive")
    assert not original.exists()


@pytest.mark.parametrize("folder", ["../", "/tmp", "已处理文档", "已处理文档/sub"])
def test_invalid_or_processed_folder_cannot_be_scanned(workbench, folder):
    response = workbench.client.get(
        "/api/ingestion/source-directories/mapped/files", params={"folder": folder}
    )
    assert response.status_code == 409


def test_changed_source_configuration_invalidates_remote_snapshot(workbench):
    sid = workbench.ssh_source()
    workbench.source_job(sid, "scan")
    response = workbench.client.put(f"/api/ingestion/ssh-sources/{sid}", json={**SSH, "remote_path": "/new"})
    assert response.status_code == 200
    assert workbench.client.get(f"/api/ingestion/ssh-sources/{sid}/scan").json() is None


@pytest.mark.parametrize("kind", ["mapped", "ssh"])
def test_reintroduced_same_name_content_and_mtime_is_a_new_round(workbench, kind):
    root = workbench.mapped if kind == "mapped" else workbench.remote.root
    original = root / "manual.pdf"
    original.write_bytes(PDF)
    info = original.stat()
    sid = "mapped" if kind == "mapped" else workbench.ssh_source()
    if kind == "ssh":
        assert workbench.source_job(sid, "sync")["status"] == "completed"
    first = import_source(workbench, sid)
    assert workbench.sources.run_one("archive")
    assert not choose(workbench.client, sid)
    original.write_bytes(PDF)
    os.utime(original, ns=(info.st_atime_ns, info.st_mtime_ns))
    if kind == "ssh":
        reads = len(workbench.remote.reads)
        assert workbench.source_job(sid, "scan")["status"] == "completed"
        preview = workbench.client.get(f"/api/ingestion/ssh-sources/{sid}/scan").json()
        assert preview["pending"] == 1
        assert workbench.source_job(sid, "sync")["skipped"] == 0
        assert len(workbench.remote.reads) > reads
    assert len(choose(workbench.client, sid)) == 1
    second = import_source(workbench, sid)
    assert second["id"] != first["id"] and second["status"] == "imported"
    assert len(workbench.upstream.documents) == 2
