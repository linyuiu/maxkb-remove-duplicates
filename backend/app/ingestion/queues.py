"""Persistent folder manifests: uploads are bounded; batches execute in order."""

from __future__ import annotations

import time
import uuid

from .formats import file_kind

# Manual preview holds the wave until its imports have actually completed too.
VERIFIED_DONE = {"failed", "imported", "import_failed", "import_rejected", "stopped"}


class QueueStore:
    def queues(self):
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute("""
                SELECT q.*,COUNT(f.id) AS total,COUNT(DISTINCT b.id) AS batches,
                SUM(f.status='imported') AS imported,SUM(f.status='review') AS ready,
                SUM(f.status IN ('failed','import_failed','import_unknown','import_rejected',
                'cleanup_failed','preparation_unknown','stopped')) AS failed
                FROM queues q JOIN batches b ON b.queue_id=q.id
                JOIN files f ON f.batch_id=b.id GROUP BY q.id ORDER BY q.created_at DESC LIMIT 50
            """)
            ]

    def create_queue(self, queue_id: str, manifest: list[dict], config: dict):
        plan = config.get("_knowledge_plan")
        config = dict(config)
        if plan:
            config["_knowledge_plan"] = {"root": plan["root"]}
            if any(g["folder"] == "" for g in plan["groups"]):
                config["_knowledge_plan"]["root_knowledge"] = True
        now = time.time()
        width = config.get("batch_size", 100)
        with self.connect() as db:
            db.execute("SELECT pg_advisory_xact_lock(762190317)")
            if db.execute("SELECT 1 FROM queues WHERE id=?", (queue_id,)).fetchone():
                # A lost create response may be replayed with the same client-generated ID.
                existing = db.execute(
                    "SELECT relative_path,bytes,import_mode FROM files WHERE batch_id IN "
                    "(SELECT id FROM batches WHERE queue_id=?) ORDER BY queue_position",
                    (queue_id,),
                ).fetchall()
                if [(r[0], r[1], r[2]) for r in existing] != [
                    (
                        f["path"],
                        f["bytes"],
                        f.get(
                            "import_mode",
                            config.get("excel_mode", "table")
                            if file_kind(f["name"]) == "excel"
                            else "split",
                        ),
                    )
                    for f in manifest
                ]:
                    raise ValueError("队列 ID 已存在，文件清单不一致")
                old = db.execute(
                    "SELECT config FROM batches WHERE queue_id=? ORDER BY ordinal LIMIT 1",
                    (queue_id,),
                ).fetchone()
                old_plan = db.execute(
                    "SELECT plan FROM knowledge_plans WHERE queue_id=?", (queue_id,)
                ).fetchone()
                if (self.decrypt(old_plan[0]) if old_plan else None) != plan:
                    raise ValueError("队列 ID 已存在，知识库或标签计划不一致")
                if self.decrypt(old[0]) != config:
                    raise ValueError("队列 ID 已存在，参数不一致；请继续原队列或新建队列")
                return
            db.execute(
                "INSERT INTO queues(id,name,created_at,batch_size,auto_import) VALUES(?,?,?,?,?)",
                (
                    queue_id,
                    manifest[0]["path"].split("/")[0],
                    now,
                    width,
                    int(config.get("auto_import", False)),
                ),
            )
            self.init_targets(db, queue_id, plan)
            for ordinal, start in enumerate(range(0, len(manifest), width)):
                bid = str(uuid.uuid4())
                db.execute(
                    "INSERT INTO batches(id,name,created_at,config,queue_id,ordinal,wave_state) "
                    "VALUES(?,?,?,?,?,?,'pending_upload')",
                    (bid, f"第 {ordinal + 1} 批", now, self.encrypt(config), queue_id, ordinal),
                )
                for position, f in enumerate(manifest[start : start + width], start):
                    db.execute(
                        "INSERT INTO files(id,batch_id,name,bytes,status,message,created_at,"
                        "updated_at,relative_path,queue_position,kind,import_mode) "
                        "VALUES(?,?,?,?,'pending_upload','等待浏览器上传',?,?,?,?,?,?)",
                        (
                            str(uuid.uuid4()),
                            bid,
                            f["name"],
                            f["bytes"],
                            now,
                            now,
                            f["path"],
                            position,
                            file_kind(f["name"]),
                            f.get(
                                "import_mode",
                                config.get("excel_mode", "table")
                                if file_kind(f["name"]) == "excel"
                                else "split",
                            ),
                        ),
                    )

    def queue_snapshot(self, queue_id: str, *, problem_offset: int = 0, problem_limit: int = 50):
        if problem_offset < 0 or not 1 <= problem_limit <= 100:
            raise ValueError("异常清单分页参数无效")
        with self.connect() as db:
            db.execute("BEGIN")  # Counts and wave states must describe the same snapshot.
            queue = db.execute("SELECT * FROM queues WHERE id=?", (queue_id,)).fetchone()
            if not queue:
                return None
            batches = [
                dict(r)
                for r in db.execute(
                    "SELECT b.id,b.ordinal,b.wave_state,COUNT(f.id) AS total,"
                    "SUM(f.status='imported') AS imported,SUM(f.status='review') AS ready,"
                    "SUM(f.status IN ('failed','import_failed','import_unknown','import_rejected',"
                    "'cleanup_failed','preparation_unknown','stopped')) AS failed,"
                    "SUM(f.status IN ('failed','import_failed','import_rejected',"
                    "'imported','stopped')) AS settled,"
                    "SUM(f.status IN ('cleanup_failed','import_unknown',"
                    "'preparation_unknown')) AS blocked "
                    "FROM batches b JOIN files f ON f.batch_id=b.id "
                    "WHERE b.queue_id=? GROUP BY b.id ORDER BY b.ordinal",
                    (queue_id,),
                )
            ]
            total = sum(wave["total"] for wave in batches)
            unsettled = sum(wave["total"] - wave["settled"] for wave in batches)
            problems = [
                dict(r)
                for r in db.execute(
                    "SELECT f.id,f.name,f.relative_path,f.status,f.error,b.ordinal FROM files f "
                    "JOIN batches b ON b.id=f.batch_id WHERE b.queue_id=? "
                    "AND f.status IN "
                    "('failed','import_failed','import_rejected','import_unknown',"
                    "'cleanup_failed','preparation_unknown','stopped') "
                    "ORDER BY f.queue_position,f.id LIMIT ? OFFSET ?",
                    (queue_id, problem_limit, problem_offset),
                )
            ]
        for wave in batches:
            wave["verification"] = (
                "unresolved"
                if wave["blocked"]
                else "pending"
                if wave["settled"] < wave["total"]
                else "success"
                if wave["imported"] == wave["total"]
                else "completed_with_errors"
            )
        targets = self.knowledge_targets(queue_id)
        problem_total = sum(wave["failed"] for wave in batches)
        result = {
            **dict(queue),
            "knowledge_targets": targets,
            "total": total,
            "batches": batches,
            "problems": problems,
            "problem_total": problem_total,
            "problem_offset": problem_offset,
            "problem_limit": problem_limit,
        }
        result["state"] = (
            "stopped"
            if queue["intake_state"] == "stopped"
            else "receiving"
            if queue["intake_state"] != "ready"
            else "paused"
            if any(wave["blocked"] for wave in batches)
            or any(t["status"] in {"failed", "unknown"} for t in targets)
            else "completed_with_errors"
            if problem_total
            and not unsettled
            and all(b["wave_state"] == "completed" for b in batches)
            else "completed"
            if not unsettled and all(b["wave_state"] == "completed" for b in batches)
            else "processing"
        )
        return result

    def queue_advance(self, db, queue_id: str):
        queue = db.execute("SELECT * FROM queues WHERE id=?", (queue_id,)).fetchone()
        if not queue or queue["intake_state"] != "ready":
            return
        if db.execute(
            "SELECT 1 FROM knowledge_targets WHERE queue_id=? AND status!='ready' LIMIT 1",
            (queue_id,),
        ).fetchone():
            return
        if db.execute(
            "SELECT 1 FROM files f JOIN batches b ON b.id=f.batch_id "
            "WHERE b.queue_id=? AND f.status IN "
            "('import_unknown',"
            "'cleanup_failed','preparation_unknown') LIMIT 1",
            (queue_id,),
        ).fetchone():
            return
        for wave in db.execute(
            "SELECT * FROM batches WHERE queue_id=? AND wave_state!='completed' ORDER BY ordinal",
            (queue_id,),
        ).fetchall():
            if wave["wave_state"] == "pending_upload":
                return
            if wave["wave_state"] == "waiting":
                db.execute("UPDATE batches SET wave_state='active' WHERE id=?", (wave["id"],))
                ids = db.execute(
                    "SELECT id FROM files WHERE batch_id=? AND status='waiting_wave'", (wave["id"],)
                ).fetchall()
                for row in ids:
                    db.execute(
                        "UPDATE files SET status='queued',message='等待解析',updated_at=? "
                        "WHERE id=?",
                        (time.time(), row["id"]),
                    )
                    self._event(db, row["id"])
                if ids:
                    return
            states = {
                r[0]
                for r in db.execute(
                    "SELECT DISTINCT status FROM files WHERE batch_id=?", (wave["id"],)
                )
            }
            if not states <= VERIFIED_DONE:
                return
            if db.execute(
                "SELECT 1 FROM remote_resources r JOIN files f ON f.id=r.file_id "
                "WHERE f.batch_id=? AND f.status!='imported' AND r.deleted_at IS NULL "
                "AND r.retained_at IS NULL LIMIT 1",
                (wave["id"],),
            ).fetchone():
                return
            db.execute("UPDATE batches SET wave_state='completed' WHERE id=?", (wave["id"],))

    def commit_wave_upload(self, batch_id: str, *, failures: dict | None = None):
        with self.connect() as db:
            db.execute("SELECT pg_advisory_xact_lock(762190317)")
            batch = db.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
            if batch["wave_state"] != "uploading":
                raise ValueError("该批次没有持有上传权")
            db.execute("UPDATE batches SET wave_state='waiting' WHERE id=?", (batch_id,))
            db.execute(
                "UPDATE files SET status='waiting_wave',error='',message='等待全部原文件接收完成',"
                "updated_at=? "
                "WHERE batch_id=? AND stop_requested=0",
                (time.time(), batch_id),
            )
            for file_id, error in (failures or {}).items():
                db.execute(
                    "UPDATE files SET status='failed',message='原文件格式异常',"
                    "error=?,finished_at=? "
                    "WHERE id=? AND batch_id=?",
                    (error, time.time(), file_id, batch_id),
                )
                self._event(db, file_id)
            # Completion is a queue-wide barrier, not a per-upload-request signal.
            if not db.execute(
                "SELECT 1 FROM batches WHERE queue_id=? "
                "AND wave_state IN ('pending_upload','uploading') LIMIT 1",
                (batch["queue_id"],),
            ).fetchone():
                missing = db.execute(
                    "SELECT 1 FROM files f JOIN batches b ON b.id=f.batch_id "
                    "LEFT JOIN intake_receipts r ON r.file_id=f.id "
                    "WHERE b.queue_id=? AND r.file_id IS NULL LIMIT 1",
                    (batch["queue_id"],),
                ).fetchone()
                if missing:
                    raise ValueError("原文件接收凭证不完整，禁止启动处理")
                db.execute(
                    "UPDATE queues SET intake_state='ready' WHERE id=? AND intake_state!='stopped'",
                    (batch["queue_id"],),
                )
            self.queue_advance(db, batch["queue_id"])

    def claim_wave_upload(self, queue_id: str, batch_id: str):
        with self.connect() as db:
            db.execute("SELECT pg_advisory_xact_lock(762190317)")
            batch = db.execute(
                "SELECT * FROM batches WHERE id=? AND queue_id=?", (batch_id, queue_id)
            ).fetchone()
            if not batch:
                raise ValueError("队列批次不存在")
            queue = db.execute("SELECT intake_state FROM queues WHERE id=?", (queue_id,)).fetchone()
            if queue and queue[0] == "stopped":
                raise ValueError("任务已停止，请新建任务；不能继续上传")
            if batch["wave_state"] == "uploading":
                raise ValueError("该批次正在上传，请稍后刷新状态")
            if batch["wave_state"] != "pending_upload":
                return None  # Idempotent: completed upload must never be overwritten.
            db.execute("UPDATE batches SET wave_state='uploading' WHERE id=?", (batch_id,))
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM files WHERE batch_id=? ORDER BY queue_position", (batch_id,)
                )
            ]

    def release_wave_upload(self, batch_id: str):
        with self.connect() as db:
            db.execute(
                "UPDATE batches SET wave_state='pending_upload' WHERE id=? "
                "AND wave_state='uploading'",
                (batch_id,),
            )

    def batch_queue(self, db, file_id: str):
        row = db.execute(
            "SELECT b.queue_id FROM batches b JOIN files f ON f.batch_id=b.id WHERE f.id=?",
            (file_id,),
        ).fetchone()
        if row and row[0]:
            self.queue_advance(db, row[0])
