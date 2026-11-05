"""Remote directory previews: enumerate metadata without downloading source content."""

import json
import time


class RemoteScans:
    def init_remote_scans(self):
        pass  # Schema is managed by parent Alembic migrations.

    def remote_scan_result(self, source_id):
        _, config = self.source(source_id)
        with self.store.connect() as db:
            row = db.execute(
                "SELECT * FROM source_remote_scans WHERE source_key=? AND mirror_id=?",
                (source_id, config["mirror_id"]),
            ).fetchone()
        if not row:
            return None
        result = json.loads(row["result"])
        # Old saved previews are also presented as one fresh pending batch.
        for entry in result["files"]:
            entry["status"] = "pending"
        result["counts"] = {"pending": len(result["files"])}
        result["pending"] = len(result["files"])
        result["pending_bytes"] = sum(f["bytes"] for f in result["files"])
        return result

    def submit_remote_scan(self, source_id, job_id):
        _, config = self.source(source_id)
        return self.insert_job(job_id, source_id, "scan", config)

    def scan_remote(self, job):
        from .sources import SourceError

        config = self.store.decrypt(job["config"])
        _, current = self.source(job["source_key"])
        if any(config[k] != current[k] for k in ("host", "port", "mirror_id")):
            raise SourceError("来源连接已变化，请新建远程扫描任务")
        config["fingerprint"] = current["fingerprint"]
        self.update(job["id"], message="正在扫描远程目录（不下载文件）")
        with self.ssh_lock:
            with self.connection(job["source_key"], config) as sftp:
                files, ignored = self.remote_manifest(sftp, config, job["id"])
            result_files = [
                {
                    "path": entry["path"],
                    "bytes": entry["bytes"],
                    "mtime": str(entry["mtime"]),
                    "status": "pending",
                }
                for entry in files
            ]
            self.checkpoint(job["id"])
            result = {
                "job_id": job["id"],
                "scanned_at": time.time(),
                "files": result_files,
                "counts": {"pending": len(files)},
                "pending": len(files),
                "pending_bytes": sum(f["bytes"] for f in result_files),
                "ignored": ignored,
            }
            with self.store.connect() as db:
                db.execute(
                    "INSERT INTO source_remote_scans VALUES(?,?,?,?,?) ON CONFLICT(source_key) DO UPDATE SET mirror_id=excluded.mirror_id,job_id=excluded.job_id,scanned_at=excluded.scanned_at,result=excluded.result",
                    (
                        job["source_key"],
                        config["mirror_id"],
                        job["id"],
                        result["scanned_at"],
                        json.dumps(result, ensure_ascii=False),
                    ),
                )
            self.update(
                job["id"],
                total_files=len(files),
                done_files=len(files),
                total_bytes=0,
                done_bytes=0,
            )
            self.log(
                job["id"],
                f"远程扫描完成：本次 {len(files)} 个待同步文件；不按历史记录跳过，未下载文件",
            )
