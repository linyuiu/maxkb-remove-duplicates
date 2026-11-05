"""Recoverable local history cleanup; upstream documents and source hosts are never touched."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .lineage import durable_mkdir, durable_replace

logger = logging.getLogger(__name__)
SAFE = {
    "review",
    "imported",
    "failed",
    "import_failed",
    "import_rejected",
    "pending_upload",
    "waiting_wave",
    "stopped",
    "preparation_unknown",
    "import_unknown",
    "cleanup_failed",
}


class CleanupConflict(ValueError):
    pass


class MutationGate:
    """Exclude cleanup from other HTTP mutations, including streaming uploads."""

    def __init__(self):
        self.lock = threading.Lock()
        self.active = 0
        self.cleaning = False
        self.blocked = False
        self.executing = False

    @contextmanager
    def enter(self, cleanup=False):
        with self.lock:
            if self.blocked:
                raise CleanupConflict("上次清理需要恢复，请先重启服务后再操作")
            if self.cleaning or self.executing or (cleanup and self.active):
                raise CleanupConflict("有上传或其他操作正在执行，请完成后再清理")
            if cleanup:
                self.cleaning = True
            else:
                self.active += 1
        try:
            yield
        finally:
            with self.lock:
                if cleanup:
                    self.cleaning = False
                else:
                    self.active -= 1

    @contextmanager
    def execute_cleanup(self):
        # Keep exclusion until the worker really finishes, even if the HTTP client disconnects.
        with self.lock:
            if self.active or self.executing:
                raise CleanupConflict("有其他操作正在执行，请稍后清理")
            self.executing = True
        try:
            yield
        finally:
            with self.lock:
                self.executing = False


def init_cleanup(store):
    store.history_epoch = 0

def _snapshot(db):
    files = [
        dict(r)
        for r in db.execute("""
        SELECT f.id AS file_id,f.kind,f.name,f.bytes,f.status,f.updated_at,f.unresolved_create,
            f.error,f.operation_active,f.stop_requested,
            r.source_kind,r.sha256,q.source_key
        FROM files f JOIN batches b ON b.id=f.batch_id
        LEFT JOIN queues q ON q.id=b.queue_id LEFT JOIN intake_receipts r ON r.file_id=f.id
        ORDER BY f.id
    """)
    ]
    retained = {
        r["file_id"]: json.loads(r["metadata"])
        for r in db.execute("SELECT * FROM retained_sources ORDER BY file_id")
    }
    for row in files:
        # Legacy rows without an intake receipt are classified by their queue source.
        source = row["source_kind"] or (
            "mapped" if row["source_key"] == "mapped" else "ssh" if row["source_key"] else "browser"
        )
        retained[row["file_id"]] = {
            key: row[key] for key in ("file_id", "kind", "name", "bytes", "sha256")
        }
        retained[row["file_id"]]["source_kind"] = source
    jobs = [
        dict(r) for r in db.execute("SELECT id,kind,status,updated_at FROM source_jobs ORDER BY id")
    ]
    archives = [
        dict(r)
        for r in db.execute(
            "SELECT a.*,f.status AS pipeline FROM source_archives a JOIN files f ON f.id=a.file_id "
            "ORDER BY a.file_id"
        )
    ]
    batches = [tuple(r) for r in db.execute("SELECT id FROM batches ORDER BY id")]
    revision = hashlib.sha256(
        json.dumps(
            [
                files,
                retained,
                jobs,
                batches,
                archives,
                [
                    dict(r)
                    for r in db.execute("SELECT * FROM knowledge_targets ORDER BY queue_id,folder")
                ],
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()
    blocked = [
        {"name": r["name"], "status": r["status"]}
        for r in files
        if r["status"] not in SAFE or r["operation_active"]
    ]
    blocked.extend(
        {"name": r["id"], "status": r["status"]}
        for r in jobs
        if r["status"] in {"queued", "running"}
    )
    blocked.extend(
        {"name": r["name"], "status": r["status"]}
        for r in db.execute(
            "SELECT name,status FROM knowledge_targets "
            "WHERE status IN ('matching','creating','tagging')"
        )
    )
    blocked.extend(
        {"name": r["file_id"], "status": "等待归档完成"}
        for r in archives
        if r["pipeline"] == "imported" and r["status"] in {"pending", "moving", "published"}
    )
    warnings = [
        {"name": r["name"], "status": r["status"]}
        for r in files
        if r["status"] in {"stopped", "preparation_unknown", "import_unknown", "cleanup_failed"}
        or r["unresolved_create"]
    ]
    warnings.extend(
        {"name": r["name"], "status": r["status"]}
        for r in db.execute(
            "SELECT name,status FROM knowledge_targets WHERE status IN ('unknown','stopped')"
        )
    )
    warnings.extend(
        {"name": r["file_id"], "status": "归档失败，原文件保留"}
        for r in archives
        if r["status"] in {"failed", "canceled"}
    )
    counts = {
        "batches": len(batches),
        "history_files": len(files),
        **{
            kind: sum(r["source_kind"] == kind for r in retained.values())
            for kind in ("browser", "ssh", "mapped")
        },
    }
    counts["sync_files"] = db.execute(
        "SELECT COUNT(DISTINCT i.local_path) FROM source_inventory i "
        "JOIN source_jobs j ON j.id=i.job_id WHERE j.kind='sync' AND i.state='received'"
    ).fetchone()[0]
    return {
        "revision": revision,
        "counts": counts,
        "can_clear": not blocked,
        "blocked": blocked[:20],
        "blocked_count": len(blocked),
        "warnings": warnings[:20],
        "warning_count": len(warnings),
    }, retained


def cleanup_preview(store):
    with store.connect() as db:
        db.execute("BEGIN")
        result, _ = _snapshot(db)
        result["pending_cleanup"] = db.execute(
            "SELECT COUNT(*) FROM history_cleanups WHERE state NOT IN ('completed','rolled_back')"
        ).fetchone()[0]
        if db.execute("SELECT 1 FROM history_cleanups WHERE state='staging'").fetchone():
            result["can_clear"] = False
            result["blocked_count"] += 1
            result["blocked"].append({"name": "上次清理中断", "status": "请重启服务恢复"})
    return result


def _managed_path(store, relative):
    """Only generated UUID directories and the local SSH mirror root are eligible."""
    path = Path(relative)
    parts = path.parts
    if str(path) != relative or path.is_absolute() or ".." in parts:
        raise ValueError("非法清理路径")
    if relative != "sync":
        if (
            len(parts) != 3
            or parts[0] != "files"
            or parts[1] not in {"pdf", "word", "excel", "mineru"}
        ):
            raise ValueError("清理路径不属于本服务管理目录")
        uuid.UUID(parts[2])
    target = store.root / path
    for parent in target.parents:
        if parent == store.root:
            break
        if parent.is_symlink():
            raise ValueError("清理路径的父目录是符号链接，拒绝操作")
    return target


def _trash(store, operation, index):
    uuid.UUID(operation)
    directory = store.root / ".history-trash" / operation
    for path in (directory.parent, directory):
        if path.is_symlink():
            raise ValueError("清理暂存目录不能是符号链接")
    return directory / str(index)


def _state(store, operation, state, error=""):
    with store.connect() as db:
        db.execute(
            "UPDATE history_cleanups SET state=?,error=? WHERE id=?",
            (state, error[:2000], operation),
        )


def _move(source, destination):
    durable_replace(source, destination)
    fd = os.open(source.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _restore(store, operation, payload):
    for index, relative in reversed(list(enumerate(payload["targets"]))):
        staged = _trash(store, operation, index)
        if not staged.exists() and not staged.is_symlink():
            continue
        original = _managed_path(store, relative)
        if original.exists() or original.is_symlink():
            raise RuntimeError("恢复清理暂存时发现同名文件，请人工核对：" + relative)
        durable_mkdir(original.parent)
        _move(staged, original)
    with store.connect() as db:
        for identity, state in payload["queue_states"]:
            db.execute("UPDATE queues SET intake_state=? WHERE id=?", (state, identity))
        db.execute(
            "UPDATE history_cleanups SET state='rolled_back',error='' WHERE id=?", (operation,)
        )


def _discard(store, operation, payload):
    errors = []
    for index, _ in enumerate(payload["targets"]):
        path = _trash(store, operation, index)
        try:
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.exists():
                shutil.rmtree(path)
        except OSError as exc:
            errors.append(f"暂存项 {index}：{exc}")
    error = "; ".join(errors)
    directory = _trash(store, operation, 0).parent
    if directory.is_dir():
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    _state(store, operation, "delete_failed" if errors else "completed", error)
    return error


def recover_cleanup(store):
    """Run before workers start; never retry deletion against a reused original path."""
    with store.connect() as db:
        rows = db.execute(
            "SELECT * FROM history_cleanups WHERE state NOT IN "
            "('completed','rolled_back') ORDER BY created_at"
        ).fetchall()
    for row in rows:
        payload = json.loads(row["payload"])
        if row["state"] == "staging":
            _restore(store, row["id"], payload)
        else:
            error = _discard(store, row["id"], payload)
            if error:
                logger.warning("历史清理尚有本地文件未删除 operation=%s %s", row["id"], error)


def clear_history(
    store, *, revision, delete_web_files=False, delete_ssh_files=False, protected_source=None
):
    operation = str(uuid.uuid4())
    with store.connect() as db:
        db.execute("SELECT pg_advisory_xact_lock(762190317)")
        preview, records = _snapshot(db)
        if preview["revision"] != revision:
            raise CleanupConflict("历史记录或同步文件已变化，请重新打开清理确认")
        if not preview["can_clear"]:
            raise CleanupConflict("仍有任务正在执行或正在停止，请等待当前请求退出后再清理")
        if db.execute("SELECT 1 FROM history_cleanups WHERE state='staging'").fetchone():
            raise CleanupConflict("上次清理尚在恢复，请重启服务完成恢复")
        targets, keep = set(), []
        for row in records.values():
            deleting = (row["source_kind"] == "browser" and delete_web_files) or (
                row["source_kind"] == "ssh" and delete_ssh_files
            )
            if deleting:
                for kind in (row["kind"], "mineru"):
                    relative = f"files/{kind}/{row['file_id']}"
                    _managed_path(store, relative)
                    targets.add(relative)
            else:
                keep.append(row)
        if delete_ssh_files:
            _managed_path(store, "sync")
            targets.add("sync")
        if protected_source is not None:
            protected = Path(protected_source).resolve()
            for relative in targets:
                target = _managed_path(store, relative)
                if not target.is_symlink() and (
                    protected.is_relative_to(target) or target.is_relative_to(protected)
                ):
                    raise CleanupConflict("清理目录与映射源目录重叠，请调整文件清理选项")
        destinations = {}
        for row in db.execute("SELECT id,config FROM batches"):
            config = store.decrypt(row["config"])
            destinations[row["id"]] = {
                key: config[key] for key in ("maxkb_base_url", "workspace_id", "knowledge_id")
            }
        payload = {
            "targets": sorted(targets),
            "counts": preview["counts"],
            "delete_web_files": delete_web_files,
            "delete_ssh_files": delete_ssh_files,
            "queue_states": [tuple(r) for r in db.execute("SELECT id,intake_state FROM queues")],
            "documents": [
                dict(r)
                for r in db.execute(
                    "SELECT a.file_id,f.batch_id,b.queue_id,f.relative_path,"
                    "d.document_id,d.source_file_id "
                    "FROM remote_documents d JOIN import_attempts a "
                    "ON a.id=d.attempt_id JOIN files f ON f.id=a.file_id "
                    "JOIN batches b ON b.id=f.batch_id"
                )
            ],
            "destinations": destinations,
            "knowledge_targets": [dict(r) for r in db.execute("SELECT * FROM knowledge_targets")],
            "tag_receipts": [dict(r) for r in db.execute("SELECT * FROM document_tag_receipts")],
            "stopped_or_uncertain": [
                dict(r)
                for r in db.execute(
                    "SELECT id,status,error,unresolved_create,attempt_id,job_id,"
                    "source_id,stop_requested "
                    "FROM files WHERE status IN ('stopped','preparation_unknown','import_unknown',"
                    "'cleanup_failed') OR unresolved_create=1"
                )
            ],
            "remote_calls": [dict(r) for r in db.execute("SELECT * FROM remote_calls")],
            "import_attempts": [dict(r) for r in db.execute("SELECT * FROM import_attempts")],
            "archive_records": [dict(r) for r in db.execute("SELECT * FROM source_archives")],
            "sources": [
                dict(r) for r in db.execute("SELECT file_id,resource_id FROM remote_resources")
            ],
        }
        db.execute(
            "INSERT INTO history_cleanups VALUES(?,?,'staging',?,'')",
            (operation, time.time(), json.dumps(payload)),
        )
        # Maintenance must not activate waiting waves while directories are being staged.
        db.execute("UPDATE queues SET intake_state='clearing'")
    try:
        for index, relative in enumerate(payload["targets"]):
            original = _managed_path(store, relative)
            if original.exists() or original.is_symlink():
                staged = _trash(store, operation, index)
                durable_mkdir(staged.parent)
                _move(original, staged)
        with store.connect() as db:
            db.execute("SELECT pg_advisory_xact_lock(762190317)")
            db.execute("DELETE FROM retained_sources")
            db.executemany(
                "INSERT INTO retained_sources VALUES(?,?,?)",
                [(r["file_id"], r["source_kind"], json.dumps(r)) for r in keep],
            )
            for table in (
                "document_tag_receipts",
                "remote_documents",
                "import_attempts",
                "remote_calls",
                "remote_resources",
                "source_archives",
                "intake_receipts",
                "paragraphs",
                "audit_logs",
                "events",
                "files",
                "batches",
                "queues",
            ):
                db.execute(f"DELETE FROM {table}")
            job_filter = "1=1" if delete_ssh_files else "kind!='sync'"
            for table in ("source_inventory", "source_job_logs"):
                db.execute(
                    f"DELETE FROM {table} WHERE job_id IN "
                    f"(SELECT id FROM source_jobs WHERE {job_filter})"
                )
            db.execute(f"DELETE FROM source_jobs WHERE {job_filter}")
            db.execute("UPDATE history_cleanups SET state='deleting' WHERE id=?", (operation,))
    except BaseException:
        _restore(store, operation, payload)
        raise
    store.history_epoch += 1
    error = _discard(store, operation, payload)
    logger.info(
        "历史清理 operation=%s counts=%s delete_web=%s delete_ssh=%s state=%s",
        operation,
        preview["counts"],
        delete_web_files,
        delete_ssh_files,
        "delete_failed" if error else "completed",
    )
    return {
        "id": operation,
        "history_cleared": True,
        "state": "delete_failed" if error else "completed",
        "counts": preview["counts"],
        "error": error,
    }
