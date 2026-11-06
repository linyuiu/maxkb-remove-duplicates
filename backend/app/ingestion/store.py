"""PostgreSQL 持久队列。长耗时 HTTP 操作不占用数据库事务。"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .formats import file_kind
from .imports import ImportStore
from .knowledge import KnowledgeStore
from .lineage import LineageStore, durable_mkdir, durable_replace
from .logging_setup import request_id
from .queues import QueueStore
from .task_stop import TaskStopStore

DEFAULT_CONFIG = {
    "mineru_url": "http://mineru.example.com:8000",
    "maxkb_base_url": "http://maxkb.example.com:18080/admin/api",
    "workspace_id": "default",
    "knowledge_id": "pending",
    "maxkb_token": "",
    "mineru_key": "",
    "tier": "standard",
    "page_range": "",
    "limit": 2500,
    "patterns": [r"(?<=^)# .*|(?<=\n)# .*"],
    "with_filter": True,
    "request_timeout": 60,
    "transfer_timeout": 600,
    "parse_timeout": 3600,
    "poll_interval": 2,
    "batch_size": 100,
    "auto_import": False,
    "knowledge_tags_enabled": True,
    "document_tags_enabled": True,
    "inherit_knowledge_tags": True,
    "import_poll_interval": 5,
    "import_timeout": 7200,
    "excel_mode": "table",
}
PREPARING = {"uploading", "parsing", "downloading", "packaging", "splitting"}
FINISHED = {"failed", "imported", "import_failed", "import_rejected", "stopped"}
PUBLIC_FILE_FIELDS = (
    "id",
    "kind",
    "import_mode",
    "batch_id",
    "name",
    "bytes",
    "status",
    "message",
    "error",
    "created_at",
    "updated_at",
    "started_at",
    "finished_at",
    "job_id",
    "job_status",
    "paragraph_count",
    "document_count",
    "remote_paragraph_count",
    "previewed_at",
    "prepared_at",
    "relative_path",
    "attempt_id",
    "import_attempts",
    "import_started_at",
    "import_finished_at",
    "parse_seconds",
    "parse_state",
    "parse_error",
    "parse_started_at",
    "parse_finished_at",
    "split_seconds",
    "split_started_at",
    "import_seconds",
    "stop_requested",
)


def public_config(config: dict) -> dict:
    return {
        **{k: v for k, v in config.items() if k not in {"maxkb_token", "mineru_key"}},
        "has_maxkb_token": bool(config.get("maxkb_token")),
        "has_mineru_key": bool(config.get("mineru_key")),
    }


def public_file(row) -> dict:
    return {key: row[key] for key in PUBLIC_FILE_FIELDS}


class Store(ImportStore, QueueStore, LineageStore, KnowledgeStore, TaskStopStore):
    def __init__(self, directory: Path):
        self.root = directory.resolve()
        durable_mkdir(self.root)
        durable_mkdir(self.root / 'tmp')
        for kind in ('pdf', 'word', 'excel', 'mineru'):
            durable_mkdir(self.root / 'files' / kind)
        from ..security import cipher
        self.cipher = cipher()
        self.history_epoch = 0

    @contextmanager
    def connect(self):
        from .pgstore import connect
        with connect() as database:
            yield database

    def encrypt(self, config: dict) -> str:
        return self.cipher.encrypt(json.dumps(config).encode()).decode()

    def decrypt(self, value: str) -> dict:
        return json.loads(self.cipher.decrypt(value.encode()))

    def settings(self, fallback: dict | None = None) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT config FROM settings WHERE id=1").fetchone()
        return (
            {**DEFAULT_CONFIG, **self.decrypt(row[0])} if row else dict(fallback or DEFAULT_CONFIG)
        )

    def save_settings(self, config: dict):
        with self.connect() as db:
            db.execute("INSERT INTO settings VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET config=excluded.config", (self.encrypt(config),))

    def create_batch(self, batch_id: str, files: list[dict], config: dict, *, ready=True):
        now = time.time()
        with self.connect() as db:
            db.execute(
                "INSERT INTO batches(id,name,created_at,config) VALUES (?, ?, ?, ?)",
                (
                    batch_id,
                    time.strftime("%m-%d %H:%M") + " · " + files[0]["name"],
                    now,
                    self.encrypt(config),
                ),
            )
            for entry in files:
                db.execute(
                    """INSERT INTO files
                    (id,batch_id,name,bytes,status,message,created_at,updated_at,kind,import_mode)
                    VALUES (?,?,?,?,'queued','等待准备',?,?,?,?)""",
                    (
                        entry["id"],
                        batch_id,
                        entry["name"],
                        entry["bytes"],
                        now,
                        now,
                        file_kind(entry["name"]),
                        config.get("excel_mode", "table")
                        if file_kind(entry["name"]) == "excel"
                        else "split",
                    ),
                )
                self._event(db, entry["id"])
            if not ready:
                db.execute(
                    "UPDATE files SET status='waiting_wave',message='等待原文件接收凭证' "
                    "WHERE batch_id=?",
                    (batch_id,),
                )

    def complete_batch_intake(self, batch_id):
        with self.connect() as db:
            db.execute("SELECT pg_advisory_xact_lock(762190317)")
            if db.execute(
                "SELECT 1 FROM files f LEFT JOIN intake_receipts r ON r.file_id=f.id "
                "WHERE f.batch_id=? AND r.file_id IS NULL",
                (batch_id,),
            ).fetchone():
                raise ValueError("原文件接收凭证不完整")
            for row in db.execute("SELECT id FROM files WHERE batch_id=?", (batch_id,)).fetchall():
                db.execute(
                    "UPDATE files SET status='queued',message='原文件接收完成，等待处理' "
                    "WHERE id=?",
                    (row[0],),
                )
                self._event(db, row[0])

    def _event(self, db, file_id: str):
        row = db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
        db.execute(
            "INSERT INTO events(batch_id,payload,created_at) VALUES(?,?,?)",
            (
                row["batch_id"],
                json.dumps(public_file(row), ensure_ascii=False),
                time.time(),
            ),
        )
        db.execute(
            "INSERT INTO audit_logs(file_id,created_at,event,detail) VALUES(?,?,?,?)",
            (
                file_id,
                time.time(),
                row["status"],
                json.dumps(
                    {
                        "message": row["message"],
                        "error": row["error"],
                        "attempt_id": row["attempt_id"],
                        "name": row["name"],
                        "relative_path": row["relative_path"],
                        "batch_id": row["batch_id"],
                        "kind": row["kind"],
                        "import_mode": row["import_mode"],
                        "parse_state": row["parse_state"],
                        "parse_error": row["parse_error"],
                        "job_id": row["job_id"],
                        "job_status": row["job_status"],
                        "mineru_source_id": row["source_id"],
                        "request_id": request_id.get() or None,
                    },
                    ensure_ascii=False,
                ),
            ),
        )

    def update(self, file_id: str, **fields):
        # Field names come only from internal worker calls, never request data.
        if not fields or not set(fields) <= set(PUBLIC_FILE_FIELDS) | {
            "source_id",
            "next_check",
            "monitor_errors",
            "reimport_requested",
            "unresolved_create",
            "cleanup_target",
        }:
            raise ValueError("Invalid file update")
        with self.connect() as db:
            db.execute("SELECT pg_advisory_xact_lock(762190317)")
            fields["updated_at"] = time.time()
            finish_sql, finish_params = "", ()
            if "status" in fields:
                # Preview/log updates must not move a completed task's end time.
                end = fields["updated_at"] if fields["status"] in FINISHED else None
                finish_sql = (
                    ",finished_at=CASE WHEN status=? THEN COALESCE(finished_at,?) ELSE ? END"
                    if end is not None
                    else ",finished_at=NULL"
                )
                if end is not None:
                    finish_params = (fields["status"], end, end)
            db.execute(
                f"UPDATE files SET {','.join(k + '=?' for k in fields)}{finish_sql} WHERE id=?",
                (*fields.values(), *finish_params, file_id),
            )
            self._event(db, file_id)
            self.batch_queue(db, file_id)

    def claim(self, importing: bool = False, *, splitting: bool = False):
        pending, active = (
            ("split_queued", "splitting")
            if splitting
            else ("import_queued", "importing")
            if importing
            else ("queued", "uploading")
        )
        condition = "status=?"
        parameters = (pending,)
        if splitting:
            condition = "(status='split_queued' OR (status='queued' AND kind!='pdf'))"
            parameters = ()
        elif not importing:
            condition += " AND kind='pdf'"
        condition += " AND stop_requested=0"
        condition += (
            " AND NOT EXISTS (SELECT 1 FROM files blocker JOIN batches bb ON "
            "bb.id=blocker.batch_id "
            "WHERE bb.queue_id=(SELECT queue_id FROM batches WHERE id=files.batch_id) "
            "AND blocker.status IN ('import_unknown','cleanup_failed','preparation_unknown'))"
            " AND EXISTS (SELECT 1 FROM batches b LEFT JOIN queues q ON q.id=b.queue_id "
            "WHERE b.id=files.batch_id AND (b.queue_id IS NULL OR "
            "(q.intake_state='ready' AND b.wave_state IN ('active','completed'))))"
        )
        with self.connect() as db:
            if importing and self.import_slots_full(db):
                return None
            if not db.execute(
                "SELECT 1 FROM files WHERE " + condition + " LIMIT 1", parameters
            ).fetchone():
                return None
            db.execute("SELECT pg_advisory_xact_lock(762190317)")
            row = db.execute(
                "SELECT * FROM files WHERE " + condition + " ORDER BY created_at,ingest_order LIMIT 1",
                parameters,
            ).fetchone()
            if row is None:
                return None
            if importing and self.import_slots_full(db):
                return None
            now = time.time()
            db.execute(
                "UPDATE files SET status=?,updated_at=?,started_at=COALESCE(started_at,?),"
                "finished_at=NULL "
                "WHERE id=?",
                (active, now, now, row["id"]),
            )
            if not importing and not splitting and row["parse_state"] != "succeeded":
                db.execute(
                    "UPDATE files SET parse_state='uploading',parse_error='',"
                    "parse_finished_at=NULL,parse_started_at=COALESCE(parse_started_at,?) "
                    "WHERE id=?",
                    (now, row["id"]),
                )
            self._event(db, row["id"])
            row = db.execute("SELECT * FROM files WHERE id=?", (row["id"],)).fetchone()
            return dict(row)

    def import_slots_full(self, db):
        count = db.execute(
            "SELECT COUNT(*) FROM files WHERE status IN "
            "('importing','import_monitoring','import_checking',"
            "'cleanup_pending','cleanup_running') "
            "OR (status IN ('import_unknown','cleanup_failed') AND attempt_id IS NOT NULL)"
        ).fetchone()[0]
        return count >= getattr(self, "import_capacity", 5)

    def recover(self):
        with self.connect() as db:
            db.execute("SELECT pg_advisory_xact_lock(762190317)")
            db.execute("UPDATE files SET operation_active=0")
            db.execute(
                "UPDATE files SET status='stopped',message='已停止，不再自动执行', "
                "finished_at=COALESCE(finished_at,?),updated_at=? WHERE stop_requested=1",
                (time.time(), time.time()),
            )
            # The standalone multipart API also has an intake barrier. A crash
            # between receipt persistence and activation must not strand it forever.
            for batch in db.execute(
                "SELECT b.id FROM batches b WHERE b.queue_id IS NULL AND EXISTS "
                "(SELECT 1 FROM files f WHERE f.batch_id=b.id AND f.status='waiting_wave')"
            ).fetchall():
                missing = db.execute(
                    "SELECT 1 FROM files f LEFT JOIN intake_receipts r ON r.file_id=f.id "
                    "WHERE f.batch_id=? AND r.file_id IS NULL LIMIT 1",
                    (batch["id"],),
                ).fetchone()
                for file in db.execute(
                    "SELECT id FROM files WHERE batch_id=? AND status='waiting_wave'",
                    (batch["id"],),
                ).fetchall():
                    db.execute(
                        "UPDATE files SET status=?,message=?,error=?,updated_at=?,finished_at=? "
                        "WHERE id=?",
                        (
                            "failed" if missing else "queued",
                            "服务重启，原文件接收未完成" if missing else "接收凭证完整，恢复处理",
                            "原文件接收凭证不完整，请重新上传；已保存原件保留" if missing else "",
                            time.time(),
                            time.time() if missing else None,
                            file["id"],
                        ),
                    )
                    self._event(db, file["id"])
            for row in db.execute("SELECT id,status FROM files").fetchall():
                if row["status"] in PREPARING:
                    uncertain = db.execute(
                        "SELECT 1 FROM remote_calls WHERE file_id=? AND state='sending'",
                        (row["id"],),
                    ).fetchone()
                    db.execute(
                        "UPDATE files SET status=?,message='服务重启，恢复处理',"
                        "updated_at=? WHERE id=?",
                        (
                            "preparation_unknown"
                            if uncertain
                            else "split_queued"
                            if row["status"] == "splitting"
                            else "queued",
                            time.time(),
                            row["id"],
                        ),
                    )
                    db.execute(
                        "UPDATE files SET parse_state='pending' WHERE id=? AND kind='pdf' "
                        "AND parse_state IN ('uploading','waiting','running')",
                        (row["id"],),
                    )
                    self._event(db, row["id"])
                elif row["status"] == "importing":
                    db.execute(
                        "UPDATE files SET status='import_unknown',error=?,updated_at=? WHERE id=?",
                        (
                            "服务在入库期间停止，结果待确认。请在 MaxKB 核对；不会自动重试。",
                            time.time(),
                            row["id"],
                        ),
                    )
                    self._event(db, row["id"])
                elif row["status"] in {"import_checking", "cleanup_running"}:
                    status = (
                        "cleanup_pending"
                        if row["status"] == "cleanup_running"
                        else "import_monitoring"
                    )
                    db.execute(
                        "UPDATE files SET status=?,next_check=0 WHERE id=?", (status, row["id"])
                    )
                elif row["status"] == "reconciling":
                    db.execute("UPDATE files SET status='import_unknown' WHERE id=?", (row["id"],))
            db.execute(
                "UPDATE batches SET wave_state='pending_upload' WHERE wave_state='uploading'"
            )
            # An older build may have left an otherwise settled wave blocked by
            # retained OSS. Re-evaluate after recovery instead of waiting for a new event.
            for queue in db.execute("SELECT id FROM queues WHERE intake_state='ready'").fetchall():
                self.queue_advance(db, queue["id"])

    def file(self, file_id: str):
        with self.connect() as db:
            row = db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
        return dict(row) if row else None

    def batch_config(self, batch_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT config FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not row:
            raise KeyError(batch_id)
        return self.decrypt(row[0])

    def file_config(self, file_id: str) -> dict:
        with self.connect() as db:
            row = db.execute(
                "SELECT b.config,f.mineru_config,f.maxkb_config FROM files f "
                "JOIN batches b ON b.id=f.batch_id WHERE f.id=?",
                (file_id,),
            ).fetchone()
        if not row:
            raise KeyError(file_id)
        config = {**DEFAULT_CONFIG, **self.decrypt(row["config"])}
        if row["mineru_config"]:
            config.update(self.decrypt(row["mineru_config"]))
        if row["maxkb_config"]:
            config.update(self.decrypt(row["maxkb_config"]))
        target = self.target_for_file(file_id)
        if target:
            config["knowledge_id"] = target["knowledge_id"]
            config["document_tag_ids"] = (
                tuple(target["document_tag_ids"])
                if config.get("document_tags_enabled", True)
                else ()
            )
        return config

    def snapshot(self, batch_id: str):
        with self.connect() as db:
            db.execute("BEGIN")  # Files and event cursor must describe one coherent snapshot.
            batch = db.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
            if batch is None:
                return None
            files = db.execute(
                "SELECT * FROM files WHERE batch_id=? ORDER BY created_at,ingest_order", (batch_id,)
            ).fetchall()
            cursor = db.execute(
                "SELECT COALESCE(MAX(id),0) FROM events WHERE batch_id=?", (batch_id,)
            ).fetchone()[0]
        return {
            "id": batch_id,
            "name": batch["name"],
            "created_at": batch["created_at"],
            "config": public_config(self.decrypt(batch["config"])),
            "queue_id": batch["queue_id"],
            "ordinal": batch["ordinal"],
            "wave_state": batch["wave_state"],
            "files": [public_file(row) for row in files],
            "cursor": cursor,
        }

    def batches(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("""SELECT b.id,b.name,b.created_at,b.queue_id,b.ordinal,
                COUNT(f.id) AS total,
                SUM(f.status='imported') AS imported, SUM(f.status='review') AS ready
                FROM batches b JOIN files f ON f.batch_id=b.id GROUP BY b.id
                ORDER BY b.created_at DESC LIMIT 50""").fetchall()
        return [dict(row) for row in rows]

    def parse_records(
        self, *, queue_id=None, batch_id=None, state="all", search="", offset=0, limit=50
    ):
        """Read a bounded PDF-only list; never inspect archives or query upstream services."""
        if bool(queue_id) == bool(batch_id):
            raise ValueError("必须指定一个队列或批次")
        groups = {
            "pending": ("pending",),
            "processing": ("uploading", "waiting", "running"),
            "succeeded": ("succeeded",),
            "failed": ("failed", "stopped"),
        }
        if state != "all" and state not in groups:
            raise ValueError("解析状态筛选无效")
        with self.connect() as db:
            db.execute("BEGIN")
            table, identity = ("queues", queue_id) if queue_id else ("batches", batch_id)
            if not db.execute(f"SELECT 1 FROM {table} WHERE id=?", (identity,)).fetchone():
                return None
            scope = "b.queue_id=?" if queue_id else "b.id=?"
            base = " FROM files f JOIN batches b ON b.id=f.batch_id WHERE f.kind='pdf' AND "
            counts = {name: 0 for name in groups}
            for row in db.execute(
                "SELECT f.parse_state,COUNT(*) AS n" + base + scope + " GROUP BY f.parse_state",
                (identity,),
            ):
                for name, states in groups.items():
                    if row["parse_state"] in states:
                        counts[name] += row["n"]
            condition, params = scope, [identity]
            if state != "all":
                condition += " AND f.parse_state IN (" + ",".join("?" for _ in groups[state]) + ")"
                params.extend(groups[state])
            if search:
                # Search literal text, including filenames containing '%' and '_'.
                pattern = (
                    "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                )
                condition += (
                    " AND (f.name ILIKE ? ESCAPE '\\' OR f.relative_path ILIKE ? ESCAPE '\\')"
                )
                params.extend((pattern, pattern))
            total = db.execute("SELECT COUNT(*)" + base + condition, params).fetchone()[0]
            rows = db.execute(
                "SELECT f.*,b.ordinal"
                + base
                + condition
                + " ORDER BY b.created_at,b.ordinal,f.queue_position,f.created_at,f.id"
                " LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return {
            "files": [{**public_file(r), "ordinal": r["ordinal"]} for r in rows],
            "total": total,
            "counts": counts,
            "offset": offset,
            "limit": limit,
        }

    def events(self, batch_id: str, after: int):
        with self.connect() as db:
            rows = db.execute(
                "SELECT id,payload FROM events WHERE batch_id=? AND id>? ORDER BY id LIMIT 100",
                (batch_id, after),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_paragraphs(self, file_id: str, documents: list[dict], *, auto_import: bool = False):
        with self.connect() as db:
            db.execute("SELECT pg_advisory_xact_lock(762190317)")
            db.execute("DELETE FROM paragraphs WHERE file_id=?", (file_id,))
            index = 0
            for document in documents:
                for paragraph in document["paragraphs"]:
                    db.execute(
                        "INSERT INTO paragraphs VALUES (?,?,?,?,?)",
                        (
                            file_id,
                            index,
                            document["name"],
                            paragraph.get("title", ""),
                            paragraph["content"],
                        ),
                    )
                    index += 1
            row = db.execute(
                "SELECT reimport_requested,kind FROM files WHERE id=?", (file_id,)
            ).fetchone()
            automatic = auto_import or bool(row[0])
            db.execute(
                "UPDATE files SET status=?,message=?,error='',document_count=?,paragraph_count=?,"
                "prepared_at=?,previewed_at=NULL,reimport_requested=0,updated_at=? WHERE id=?",
                (
                    "import_queued" if automatic else "review",
                    (
                        "Excel 已保存，已授权自动入库"
                        if automatic
                        else "Excel 已保存，选择模式后确认入库"
                    )
                    if row["kind"] == "excel"
                    else ("切片完成，已授权自动入库" if automatic else "切片完成，等待预览与入库"),
                    len(documents),
                    index,
                    time.time(),
                    time.time(),
                    file_id,
                ),
            )
            self._event(db, file_id)
            self.batch_queue(db, file_id)

    def logs(self, file_id: str, after: int = 0, limit: int = 100, *, latest=False):
        with self.connect() as db:
            rows = [
                dict(r)
                for r in db.execute(
                    "SELECT id,created_at,event,detail FROM audit_logs WHERE file_id=? AND id>? "
                    + ("ORDER BY id DESC LIMIT ?" if latest else "ORDER BY id LIMIT ?"),
                    (file_id, after, limit),
                )
            ]
        return rows[::-1] if latest else rows

    def preview(self, file_id: str, offset: int, limit: int):
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT position,document_name,title,content FROM paragraphs "
                    "WHERE file_id=? ORDER BY position LIMIT ? OFFSET ?",
                    (file_id, limit, offset),
                ).fetchall()
            ]

    def queue_import(self, batch_id: str, ids: list[str]):
        with self.connect() as db:
            db.execute("SELECT pg_advisory_xact_lock(762190317)")
            rows = [
                db.execute(
                    "SELECT * FROM files WHERE id=? AND batch_id=?", (fid, batch_id)
                ).fetchone()
                for fid in ids
            ]
            if any(row is None or row["status"] != "review" for row in rows):
                raise ValueError("只能导入当前批次中等待入库的文件，请刷新状态")
            for row in rows:
                db.execute(
                    "UPDATE files SET status='import_queued',message='等待入库',updated_at=? "
                    "WHERE id=?",
                    (time.time(), row["id"]),
                )
                self._event(db, row["id"])

    def retry(self, file_id: str, mineru_config: dict | None = None):
        retired = None
        with self.connect() as db:
            db.execute("SELECT pg_advisory_xact_lock(762190317)")
            row = db.execute(
                "SELECT f.*,b.config AS batch_config FROM files f "
                "JOIN batches b ON b.id=f.batch_id WHERE f.id=?",
                (file_id,),
            ).fetchone()
            if not row or row["status"] != "failed":
                raise ValueError("只有明确失败的解析任务可以重试；入库失败不自动重试")
            if not db.execute(
                "SELECT 1 FROM intake_receipts WHERE file_id=?", (file_id,)
            ).fetchone():
                raise ValueError("原文件接收凭证不完整，请重新上传；此任务不能直接重试")
            old = self.decrypt(row["batch_config"])
            if row["mineru_config"]:
                old.update(self.decrypt(row["mineru_config"]))
            connection = mineru_config or {k: old[k] for k in ("mineru_url", "mineru_key")}
            if set(connection) != {"mineru_url", "mineru_key"}:
                raise ValueError("重试只允许更新 MinerU 连接参数")
            changed = (
                connection["mineru_url"].rstrip("/") != old["mineru_url"].rstrip("/")
                or connection["mineru_key"] != old["mineru_key"]
            )
            # IDs and cached ZIPs belong to the previous MinerU service. Clear them before
            # making the file claimable; always retain the original uploaded PDF.
            if changed:
                directory = self.directory(file_id, row["kind"])
                for name in (
                    "split.json",
                    "create_payload.json",
                ):
                    (directory / name).unlink(missing_ok=True)
                mineru_directory = self.mineru_directory(file_id)
                if mineru_directory.exists():
                    # Rename on the same filesystem is independent of the number of images.
                    # The worker can create a fresh directory after commit; slow removal of
                    # the old directory must not hold the repository's transaction lock.
                    retired = mineru_directory.with_name(
                        f".retired-{uuid.UUID(file_id).hex}-{uuid.uuid4().hex}"
                    )
                    durable_replace(mineru_directory, retired)
                db.execute(
                    "UPDATE files SET source_id=NULL,job_id=NULL,job_status=NULL WHERE id=?",
                    (file_id,),
                )
            # A terminal failed parse job needs a new submission; active jobs resume by ID.
            elif row["job_status"] in {"failed", "partial", "canceled"}:
                db.execute("UPDATE files SET job_id=NULL,job_status=NULL WHERE id=?", (file_id,))
            if changed or row["job_status"] != "completed" or row["parse_state"] != "succeeded":
                db.execute(
                    "UPDATE files SET parse_state='pending',parse_error='',parse_started_at=NULL,"
                    "parse_finished_at=NULL,parse_seconds=NULL WHERE id=?",
                    (file_id,),
                )
            db.execute("DELETE FROM paragraphs WHERE file_id=?", (file_id,))
            db.execute(
                "UPDATE files SET status='queued',error='',message='等待重试',updated_at=?,"
                "started_at=NULL,finished_at=NULL,paragraph_count=0,document_count=0,prepared_at=NULL,"
                "previewed_at=NULL,mineru_config=? "
                "WHERE id=?",
                (time.time(), self.encrypt(connection), file_id),
            )
            self._event(db, file_id)
        if retired is not None:
            self.cleanup_retired_mineru(retired)

    def cleanup_retired_mineru(self, target: Path | None = None):
        import re

        candidates = (
            [target] if target is not None else (self.root / "files" / "mineru").glob(".retired-*")
        )
        for candidate in candidates:
            if not re.fullmatch(r"\.retired-[0-9a-f]{32}-[0-9a-f]{32}", candidate.name):
                continue
            if candidate.parent != self.root / "files" / "mineru" or candidate.is_symlink():
                continue
            try:
                shutil.rmtree(candidate)
            except FileNotFoundError:
                pass
            except OSError:
                logging.getLogger(__name__).warning(
                    "已隔离的解析缓存暂未清理，稍后重试：%s", candidate.name
                )

    def resplit(self, file_id: str):
        with self.connect() as db:
            db.execute("SELECT pg_advisory_xact_lock(762190317)")
            row = db.execute("SELECT status FROM files WHERE id=?", (file_id,)).fetchone()
            if not row or row["status"] not in {"review", "import_rejected"}:
                raise ValueError("只有等待入库或明确被拒绝的文件可以重新切片")
            db.execute("DELETE FROM paragraphs WHERE file_id=?", (file_id,))
            resources = db.execute(
                "SELECT 1 FROM remote_resources WHERE file_id=? AND deleted_at IS NULL "
                "AND retained_at IS NULL LIMIT 1",
                (file_id,),
            ).fetchone()
            db.execute(
                "UPDATE files SET status=?,error='',message='清理旧源文件后重新切片',"
                "paragraph_count=0,document_count=0,prepared_at=NULL,previewed_at=NULL,"
                "cleanup_target='queued',next_check=0,reimport_requested=0,"
                "updated_at=?,finished_at=NULL WHERE id=?",
                ("cleanup_pending" if resources else "queued", time.time(), file_id),
            )
            self._event(db, file_id)

    def directory(self, file_id: str, kind: str | None = None) -> Path:
        """Original uploads and MaxKB checkpoints, grouped by document type.

        Uploads provide kind before their database row exists. Callers with an
        existing row can also supply it to avoid another database lookup.
        """
        # All directories are UUIDs issued by the server, never user filenames.
        uuid.UUID(file_id)
        kind = kind if kind is not None else self.file(file_id)["kind"]
        if kind not in {"pdf", "word", "excel"}:
            raise ValueError("不支持的文件分类")
        return self.root / "files" / kind / file_id

    def mineru_directory(self, file_id: str) -> Path:
        """MinerU ZIPs, extracted Markdown/images and preview indexes."""
        uuid.UUID(file_id)
        return self.root / "files" / "mineru" / file_id

    def prune_logs(self, days: int = 30):
        if days <= 0:
            return
        before = time.time() - days * 86400
        with self.connect() as db:
            for table in ("audit_logs", "events"):
                db.execute(
                    f"DELETE FROM {table} WHERE id IN "
                    f"(SELECT id FROM {table} WHERE created_at<? LIMIT 5000)",
                    (before,),
                )

    def event_floor(self, batch_id: str):
        with self.connect() as db:
            return db.execute(
                "SELECT COALESCE(MIN(id),0) FROM events WHERE batch_id=?", (batch_id,)
            ).fetchone()[0]
