"""Immutable intake receipts and an explicit ledger of owned upstream resources."""

import hashlib
import json
import os
import time
import uuid


def file_digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024**2):
            digest.update(chunk)
    return digest.hexdigest()


def durable_replace(part, destination):
    part.replace(destination)
    fd = os.open(destination.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def durable_mkdir(path, *, exist_ok=True):
    missing, current = [], path
    while not current.exists():
        missing.append(current)
        current = current.parent
    path.mkdir(parents=True, exist_ok=exist_ok)
    for created in reversed(missing):
        for directory in (created, created.parent):
            fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)


class LineageStore:
    def init_lineage(self):
        pass  # Schema is managed by parent Alembic migrations.

    def receive_file(
        self,
        file_id,
        *,
        source_kind,
        source_key,
        source_job_id=None,
        source_path,
        source_mtime=None,
        sha256=None,
        origin=None,
    ):
        row = self.file(file_id)
        path = self.directory(file_id, row["kind"]) / row["name"]
        digest = sha256 or file_digest(path)
        if path.stat().st_size != row["bytes"]:
            raise ValueError("原文件大小与清单不一致")
        with self.connect() as db:
            old = db.execute(
                "SELECT sha256 FROM intake_receipts WHERE file_id=?", (file_id,)
            ).fetchone()
            if old and old[0] != digest:
                raise ValueError("已保存的原文件内容不一致，禁止覆盖")
            db.execute(
                "INSERT OR IGNORE INTO intake_receipts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    file_id,
                    source_kind,
                    source_key,
                    source_job_id,
                    source_path,
                    str(source_mtime) if source_mtime is not None else None,
                    row["name"],
                    row["kind"],
                    row["bytes"],
                    digest,
                    str(path),
                    time.time(),
                    json.dumps(origin or {}, ensure_ascii=False),
                ),
            )

    def check_received_digest(self, file_id, digest):
        from .pipeline import PipelineError

        with self.connect() as db:
            row = db.execute(
                "SELECT sha256 FROM intake_receipts WHERE file_id=?", (file_id,)
            ).fetchone()
        if row and row[0] != digest:
            raise PipelineError("已接收的原件内容不一致，禁止覆盖；请使用原文件重试或新建任务")

    def provenance(self, file_id):
        with self.connect() as db:
            receipt = db.execute(
                "SELECT * FROM intake_receipts WHERE file_id=?", (file_id,)
            ).fetchone()
            resources = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM remote_resources WHERE file_id=? ORDER BY acquired_at",
                    (file_id,),
                )
            ]
            calls = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM remote_calls WHERE file_id=? ORDER BY created_at", (file_id,)
                )
            ]
        return {
            "source": dict(receipt) if receipt else None,
            "resources": resources,
            "calls": calls,
            "tag_receipts": self.tag_receipts(file_id),
        }

    def verify_original(self, file_id):
        from pathlib import Path

        from .formats import validate_source
        from .pipeline import PipelineError

        with self.connect() as db:
            row = db.execute("SELECT * FROM intake_receipts WHERE file_id=?", (file_id,)).fetchone()
        if not row:
            raise PipelineError("缺少原文件接收凭证，禁止处理；请重新上传原文件")
        path = Path(row["local_path"])
        if (
            not path.is_file()
            or path.stat().st_size != row["bytes"]
            or file_digest(path) != row["sha256"]
        ):
            raise PipelineError("保存的原文件与接收校验值不一致，禁止处理；请核对源数据")
        validate_source(path)

    def begin_remote_call(self, file_id, kind):
        call_id = str(uuid.uuid4())
        with self.connect() as db:
            db.execute(
                "INSERT INTO remote_calls(id,file_id,kind,state,created_at) "
                "VALUES(?,?,?,'sending',?)",
                (call_id, file_id, kind, time.time()),
            )
        return call_id

    def finish_remote_call(self, call_id, *, state, response_path=None, error=""):
        with self.connect() as db:
            db.execute(
                "UPDATE remote_calls SET state=?,finished_at=?,response_path=?,error=? WHERE id=?",
                (state, time.time(), str(response_path) if response_path else None, error, call_id),
            )

    def own_resource(self, file_id, resource_id, role, config):
        import re

        from .pipeline import PipelineError

        if not isinstance(resource_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,128}", resource_id
        ):
            raise PipelineError("OSS 资源 ID 无效")
        with self.connect() as db:
            db.execute(
                "INSERT INTO remote_resources "
                "(file_id,resource_id,role,endpoint,workspace_id,knowledge_id,acquired_at) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(file_id,resource_id) DO UPDATE SET "
                "retained_at=NULL,cleanup_via='',error='',deleted_at=NULL "
                "WHERE remote_resources.endpoint=excluded.endpoint "
                "AND remote_resources.workspace_id=excluded.workspace_id "
                "AND remote_resources.knowledge_id=excluded.knowledge_id",
                (
                    file_id,
                    resource_id,
                    role,
                    config["maxkb_base_url"].rstrip("/"),
                    config["workspace_id"],
                    config["knowledge_id"],
                    time.time(),
                ),
            )

    def pending_resources(self, file_id):
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM remote_resources WHERE file_id=? AND deleted_at IS NULL "
                    "AND retained_at IS NULL",
                    (file_id,),
                )
            ]

    def confirm_document_deleted(self, file_id, attempt_id, document_id, body):
        """One receipt confirms the document deletion and its source-file cascade."""
        from .pipeline import PipelineError

        if type(body.get("code")) is not int or body["code"] != 200 or body.get("data") is not True:
            raise PipelineError("文档删除未确认成功，禁止记录关联源文件清理")
        with self.connect() as db:
            db.execute("SELECT pg_advisory_xact_lock(762190317)")
            document = db.execute(
                "SELECT d.* FROM remote_documents d JOIN files f ON f.attempt_id=d.attempt_id "
                "WHERE f.id=? AND d.attempt_id=? AND d.document_id=?",
                (file_id, attempt_id, document_id),
            ).fetchone()
            if not document:
                raise PipelineError("删除文档与当前入库尝试不匹配")
            now = time.time()
            db.execute(
                "UPDATE remote_documents SET state='deleted',deleted_at=?,checked_at=?,error='' "
                "WHERE attempt_id=? AND document_id=?",
                (now, now, attempt_id, document_id),
            )
            db.execute(
                "UPDATE remote_resources SET deleted_at=?,retained_at=NULL,"
                "cleanup_via='document_delete',error='' WHERE file_id=? "
                "AND resource_id=?",
                (now, file_id, document["source_file_id"]),
            )
            db.execute(
                "INSERT INTO audit_logs(file_id,created_at,event,detail) VALUES(?,?,?,?)",
                (
                    file_id,
                    now,
                    "document_deleted",
                    json.dumps(
                        {
                            "document_id": document_id,
                            "source_file_id": document["source_file_id"],
                            "cleanup_via": "document_delete",
                            "response": body,
                            "message": "文档删除成功；关联源文件由 MaxKB 随文档清理",
                        },
                        ensure_ascii=False,
                    ),
                ),
            )

    def retain_unconfirmed_resources(self, file_id):
        """Keep known OSS without a confirmed deletable document; never claim it was deleted."""
        with self.connect() as db:
            db.execute("SELECT pg_advisory_xact_lock(762190317)")
            if db.execute(
                "SELECT 1 FROM remote_documents d JOIN files f ON f.attempt_id=d.attempt_id "
                "WHERE f.id=? AND d.deleted_at IS NULL LIMIT 1",
                (file_id,),
            ).fetchone():
                raise ValueError("仍有文档未确认删除，禁止完成清理")
            resources = db.execute(
                "SELECT resource_id FROM remote_resources WHERE file_id=? "
                "AND deleted_at IS NULL AND retained_at IS NULL",
                (file_id,),
            ).fetchall()
            now = time.time()
            for resource in resources:
                db.execute(
                    "UPDATE remote_resources SET retained_at=?,cleanup_via='retained',"
                    "error='' WHERE file_id=? AND resource_id=?",
                    (now, file_id, resource["resource_id"]),
                )
                db.execute(
                    "INSERT INTO audit_logs(file_id,created_at,event,detail) VALUES(?,?,?,?)",
                    (
                        file_id,
                        now,
                        "oss_retained",
                        json.dumps(
                            {
                                "id": resource["resource_id"],
                                "message": (
                                    "未确认有可删除的关联文档；保留 OSS，不单独调用 OSS 删除接口"
                                ),
                            },
                            ensure_ascii=False,
                        ),
                    ),
                )
        return len(resources)

    def assert_resource_exclusive(self, file_id, resource_id, endpoint):
        with self.connect() as db:
            if db.execute(
                "SELECT 1 FROM remote_resources WHERE resource_id=? AND endpoint=? "
                "AND file_id!=? AND deleted_at IS NULL LIMIT 1",
                (resource_id, endpoint, file_id),
            ).fetchone():
                raise ValueError("OSS 资源被其他文件引用，禁止删除，需人工核对")
