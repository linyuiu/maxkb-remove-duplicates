"""Durable import attempts and strict MaxKB task-state decoding.

MaxKB v2: EMBEDDING is the rightmost status character; TOKENIZE is fourth
from the right. n=ignored, 0=pending, 1=running, 2=success, 3/4/5=failure/cancel.
Source: 1Panel-dev/MaxKB/apps/knowledge/models/knowledge.py (v2).
"""

from __future__ import annotations

import json
import re
import time
import uuid

from .pipeline import PipelineError


def task_state(document: dict) -> str:
    status = document.get("status")
    if not isinstance(status, str) or not re.fullmatch(r"[012345n]{1,20}", status):
        raise PipelineError("MaxKB 文档状态编码无效，不能确认入库结果")
    embedding = status[-1]
    tokenize = status[-4] if len(status) >= 4 else "n"
    meta = document.get("status_meta") or {}
    if not isinstance(meta, dict):
        raise PipelineError("MaxKB status_meta 格式无效")
    states = [embedding, tokenize]
    aggs = meta.get("aggs", [])
    if not isinstance(aggs, list):
        raise PipelineError("MaxKB 段落状态统计格式无效")
    for agg in aggs:
        if not isinstance(agg, dict) or type(agg.get("count")) is not int or agg["count"] < 0:
            raise PipelineError("MaxKB 段落状态统计格式无效")
        value = agg.get("status")
        if agg["count"] > 0:
            if not isinstance(value, str) or not re.fullmatch(r"[012345n]{1,20}", value):
                raise PipelineError("MaxKB 段落状态编码无效")
            states += [value[-1], value[-4] if len(value) >= 4 else "n"]
    if any(s in "345" for s in states):
        return "failed"
    if embedding == "2" and not any(s in "01" for s in states):
        return "success"
    return "pending"


def created_documents(body: dict, expected: list[dict]) -> list[dict]:
    data = body.get("data")
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list) or not data:
        raise PipelineError("创建响应缺少文档 ID，入库结果待核对")
    documents = []
    ids = set()
    matched_names = set()
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise PipelineError("创建响应的文档格式无效")
        document_id = item.get("id")
        if (
            not isinstance(document_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", document_id)
            or document_id in ids
        ):
            raise PipelineError("创建响应的文档 ID 无效或重复")
        if item.get("meta") is not None and not isinstance(item["meta"], dict):
            raise PipelineError("创建响应的文档元信息无效")
        source = (item.get("meta") or {}).get("source_file_id")
        if source is not None and (not isinstance(source, str) or not source):
            raise PipelineError("创建响应的源文件 ID 无效")
        ids.add(document_id)
        name = item.get("name") or (expected[index]["name"] if index < len(expected) else "")
        if not isinstance(name, str) or not name:
            raise PipelineError("创建响应缺少有效文档名")
        if any(d.get("source_file_id") for d in expected):
            planned = next((d for d in expected if d["name"] == name), None)
            if planned is None or (source is not None and source != planned.get("source_file_id")):
                raise PipelineError("创建响应包含非本次提交的文档/源文件，禁止自动处理，需核对结果")
            if name in matched_names:
                raise PipelineError("创建响应重复返回同一提交文档，入库结果待核对")
            matched_names.add(name)
        documents.append({"id": document_id, "name": name, "raw": item})
    return documents


class ImportStore:
    def begin_import(self, file_id: str, documents: list[dict]) -> str:
        with self.connect() as db:
            db.execute("SELECT pg_advisory_xact_lock(762190317)")
            row = db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
            if row["status"] != "importing":
                raise ValueError("文件没有持有入库执行权")
            attempt_id = str(uuid.uuid4())
            now = time.time()
            db.execute(
                "INSERT INTO import_attempts(id,file_id,created_at,phase,payload,mode) "
                "VALUES(?,?,?,'creating',?,?)",
                (
                    attempt_id,
                    file_id,
                    now,
                    json.dumps(
                        [
                            {
                                **{k: d.get(k) for k in ("name", "source_file_id")},
                                "paragraph_count": len(d["paragraphs"])
                                if row["kind"] != "excel"
                                else None,
                            }
                            for d in documents
                        ],
                        ensure_ascii=False,
                    ),
                    row["import_mode"],
                ),
            )
            db.execute(
                "UPDATE files SET attempt_id=?,import_attempts=import_attempts+1,"
                "import_started_at=?,import_finished_at=NULL,import_seconds=NULL,monitor_errors=0,"
                "unresolved_create=0,cleanup_target='import_failed' WHERE id=?",
                (attempt_id, now, file_id),
            )
            self._event(db, file_id)
        return attempt_id

    def record_documents(self, file_id: str, attempt_id: str, documents: list[dict]):
        with self.connect() as db:
            db.execute("SELECT pg_advisory_xact_lock(762190317)")
            row = db.execute("SELECT attempt_id FROM files WHERE id=?", (file_id,)).fetchone()
            if row["attempt_id"] != attempt_id:
                raise ValueError("入库尝试 ID 不匹配")
            payload = json.loads(
                db.execute(
                    "SELECT payload FROM import_attempts WHERE id=?", (attempt_id,)
                ).fetchone()[0]
            )
            sources = {d["name"]: d.get("source_file_id") for d in payload}
            counts = {d["name"]: d.get("paragraph_count") for d in payload}
            for item in documents:
                db.execute(
                    "INSERT OR IGNORE INTO remote_documents"
                    "(attempt_id,document_id,name,source_file_id,state) VALUES(?,?,?,?,'pending')",
                    (
                        attempt_id,
                        item["id"],
                        item["name"],
                        sources.get(item["name"])
                        or (item["raw"].get("meta") or {}).get("source_file_id"),
                    ),
                )
                db.execute(
                    "UPDATE remote_documents SET expected_paragraph_count=? "
                    "WHERE attempt_id=? AND document_id=?",
                    (counts.get(item["name"]), attempt_id, item["id"]),
                )
            db.execute("UPDATE import_attempts SET phase='monitoring' WHERE id=?", (attempt_id,))
            db.execute(
                "UPDATE files SET document_count=(SELECT COUNT(*) FROM remote_documents "
                "WHERE attempt_id=?),remote_paragraph_count=? WHERE id=?",
                (
                    attempt_id,
                    sum(
                        d["raw"].get("paragraph_count", 0)
                        for d in documents
                        if type(d["raw"].get("paragraph_count", 0)) is int
                    ),
                    file_id,
                ),
            )
        config = self.file_config(file_id)
        for item in documents:
            source_id = (item["raw"].get("meta") or {}).get("source_file_id")
            if source_id:
                self.own_resource(file_id, source_id, "document_source", config)

    def remote_documents(self, file_id: str) -> list[dict]:
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT d.* FROM remote_documents d JOIN files f ON f.attempt_id=d.attempt_id "
                    "WHERE f.id=? ORDER BY d.document_id",
                    (file_id,),
                )
            ]

    def record_remote(self, attempt_id: str, document_id: str, **fields):
        if not fields or not set(fields) <= {"state", "raw_status", "error", "deleted_at"}:
            raise ValueError("Invalid document update")
        with self.connect() as db:
            db.execute(
                f"UPDATE remote_documents SET {','.join(k + '=?' for k in fields)},checked_at=? "
                "WHERE attempt_id=? AND document_id=?",
                (*fields.values(), time.time(), attempt_id, document_id),
            )

    def finish_attempt(self, file_id: str, phase: str, error: str = ""):
        with self.connect() as db:
            db.execute(
                "UPDATE import_attempts SET phase=?,error=?,finished_at=? "
                "WHERE id=(SELECT attempt_id FROM files WHERE id=?)",
                (phase, error[:4000], time.time(), file_id),
            )

    def claim_monitor(self):
        # Read first: idle workers must not continuously acquire the repository's transaction lock.
        now = time.time()
        with self.connect() as db:
            if not db.execute(
                "SELECT 1 FROM files WHERE stop_requested=0 "
                "AND status IN ('import_monitoring','cleanup_pending') "
                "AND COALESCE(next_check,0)<=? LIMIT 1",
                (now,),
            ).fetchone():
                return None
            db.execute("SELECT pg_advisory_xact_lock(762190317)")
            row = db.execute(
                "SELECT * FROM files WHERE stop_requested=0 "
                "AND status IN ('import_monitoring','cleanup_pending') "
                "AND COALESCE(next_check,0)<=? ORDER BY COALESCE(next_check,0),ingest_order LIMIT 1",
                (now,),
            ).fetchone()
            if not row:
                return None
            status = "cleanup_running" if row["status"] == "cleanup_pending" else "import_checking"
            db.execute(
                "UPDATE files SET status=?,updated_at=? WHERE id=?", (status, now, row["id"])
            )
            return {**dict(row), "status": status}

    def schedule_monitor(self, file_id: str, interval: float):
        message = "等待 MaxKB 向量化/分词完成"
        with self.connect() as db:
            db.execute("SELECT pg_advisory_xact_lock(762190317)")
            row = db.execute("SELECT message,error FROM files WHERE id=?", (file_id,)).fetchone()
            db.execute(
                "UPDATE files SET status='import_monitoring',next_check=?,monitor_errors=0,"
                "message=?,error='' WHERE id=?",
                (time.time() + interval, message, file_id),
            )
            if row["message"] != message or row["error"]:
                db.execute("UPDATE files SET updated_at=? WHERE id=?", (time.time(), file_id))
                self._event(db, file_id)

    def queue_reimport(self, file_id: str, *, confirm: bool, token: str | None = None):
        if confirm is not True:
            raise ValueError("请确认清理本文件已创建的文档后重新入库")
        with self.connect() as db:
            db.execute("SELECT pg_advisory_xact_lock(762190317)")
            row = db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
            if not row or row["status"] not in {
                "import_failed",
                "import_rejected",
                "cleanup_failed",
                "import_unknown",
                "imported",
            }:
                raise ValueError("当前状态不允许重新入库")
            if (
                row["status"] in {"import_unknown", "imported"}
                and not db.execute(
                    "SELECT 1 FROM remote_documents WHERE attempt_id=?", (row["attempt_id"],)
                ).fetchone()
            ):
                raise ValueError("入库结果未知且无文档 ID；请先核对入库结果，禁止盲目重建")
            if row["unresolved_create"]:
                raise ValueError("创建响应不完整，请先核对全部文档后再重建")
            if token is not None:
                db.execute(
                    "UPDATE files SET maxkb_config=? WHERE id=?",
                    (self.encrypt({"maxkb_token": token}), file_id),
                )
            status = (
                "cleanup_pending"
                if db.execute(
                    "SELECT 1 FROM remote_documents WHERE attempt_id=? AND deleted_at IS NULL",
                    (row["attempt_id"],),
                ).fetchone()
                or db.execute(
                    "SELECT 1 FROM remote_resources WHERE file_id=? AND deleted_at IS NULL "
                    "AND retained_at IS NULL",
                    (file_id,),
                ).fetchone()
                else "queued"
            )
            db.execute(
                "UPDATE files SET status=?,reimport_requested=1,next_check=0,error='',"
                "cleanup_target='import_failed',"
                "message='等待清理并重新入库',updated_at=?,finished_at=NULL WHERE id=?",
                (status, time.time(), file_id),
            )
            if status == "queued":
                db.execute("UPDATE files SET prepared_at=NULL WHERE id=?", (file_id,))
            self._event(db, file_id)
            self.batch_queue(db, file_id)

    def import_history(self, file_id: str):
        with self.connect() as db:
            attempts = [
                dict(r)
                for r in db.execute(
                    "SELECT id,created_at,finished_at,phase,error,mode FROM import_attempts "
                    "WHERE file_id=? ORDER BY created_at DESC LIMIT 50",
                    (file_id,),
                )
            ]
            for attempt in attempts:
                attempt["documents"] = [
                    dict(r)
                    for r in db.execute(
                        "SELECT document_id,name,state,raw_status,error,checked_at,deleted_at "
                        "FROM remote_documents WHERE attempt_id=? ORDER BY document_id",
                        (attempt["id"],),
                    )
                ]
        return attempts
