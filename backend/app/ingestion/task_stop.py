"""Cooperative cancellation: settled failures stop immediately, executing calls drain first."""

import time

EXECUTING = {
    "uploading",
    "parsing",
    "downloading",
    "packaging",
    "splitting",
    "importing",
    "import_checking",
    "cleanup_running",
    "reconciling",
}


class TaskStopStore:
    def stop_requested(self, file_id):
        with self.connect() as db:
            row = db.execute("SELECT stop_requested FROM files WHERE id=?", (file_id,)).fetchone()
        return bool(row and row[0])

    def set_operation_active(self, file_id, active):
        with self.connect() as db:
            db.execute("UPDATE files SET operation_active=? WHERE id=?", (int(active), file_id))

    def acknowledge_stop(self, file_id):
        with self.connect() as db:
            db.execute("SELECT pg_advisory_xact_lock(762190317)")
            row = db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
            if not row or not row["stop_requested"] or row["status"] == "stopped":
                return
            db.execute(
                "UPDATE files SET status='stopped',message='已停止，不再自动执行',"
                "parse_state=CASE WHEN kind='pdf' AND parse_state NOT IN ('succeeded','failed') "
                "THEN 'stopped' ELSE parse_state END,finished_at=?,updated_at=? WHERE id=?",
                (time.time(), time.time(), file_id),
            )
            self._event(db, file_id)
            self.batch_queue(db, file_id)

    def request_stop(self, *, file_id=None, batch_id=None, queue_id=None):
        with self.connect() as db:
            db.execute("SELECT pg_advisory_xact_lock(762190317)")
            if batch_id:
                batch = db.execute(
                    "SELECT queue_id FROM batches WHERE id=?", (batch_id,)
                ).fetchone()
                if not batch:
                    raise KeyError(batch_id)
                queue_id = batch[0]
            if queue_id:
                if not db.execute("SELECT 1 FROM queues WHERE id=?", (queue_id,)).fetchone():
                    raise KeyError(queue_id)
                db.execute("UPDATE queues SET intake_state='stopped' WHERE id=?", (queue_id,))
                db.execute(
                    "UPDATE knowledge_targets SET status='stopped',updated_at=? WHERE queue_id=? "
                    "AND status IN ('queued','failed','unknown')",
                    (time.time(), queue_id),
                )
                db.execute(
                    "UPDATE source_jobs SET cancel_requested=1,status=CASE WHEN status='queued' "
                    "THEN 'canceled' ELSE status END WHERE queue_id=? "
                    "AND status IN ('queued','running')",
                    (queue_id,),
                )
                scope, args = "batch_id IN (SELECT id FROM batches WHERE queue_id=?)", (queue_id,)
            elif batch_id:
                scope, args = "batch_id=?", (batch_id,)
            else:
                scope, args = "id=?", (file_id,)
            rows = db.execute("SELECT * FROM files WHERE " + scope, args).fetchall()
            if not rows:
                raise KeyError(file_id or batch_id or queue_id)
            db.execute(
                "UPDATE source_archives SET status='canceled',updated_at=? WHERE status='pending' "
                "AND file_id IN (SELECT id FROM files WHERE " + scope + ")",
                (time.time(), *args),
            )
            requested = 0
            for row in rows:
                if row["status"] in {"imported", "stopped"}:
                    continue
                requested += 1
                executing = row["operation_active"] or row["status"] in EXECUTING
                db.execute(
                    "UPDATE files SET stop_requested=1,status=?,message=?,"
                    "finished_at=?,updated_at=?,parse_state=CASE WHEN ?=0 AND kind='pdf' "
                    "AND parse_state NOT IN ('succeeded','failed') "
                    "THEN 'stopped' ELSE parse_state END "
                    "WHERE id=?",
                    (
                        row["status"] if executing else "stopped",
                        "正在停止，等待当前请求退出" if executing else "已停止，不再自动执行",
                        row["finished_at"] if executing else time.time(),
                        time.time(),
                        int(bool(executing)),
                        row["id"],
                    ),
                )
                self._event(db, row["id"])
            for row in rows:
                self.batch_queue(db, row["id"])
        return {"requested": requested, "message": "已提交停止请求；已失败的任务已停止"}
