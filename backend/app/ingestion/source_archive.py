"""Persistent source archiving after confirmed import, independent of pipeline retries."""

import errno
import hashlib
import json
import os
import posixpath
import stat
import time
import uuid
from pathlib import Path

from .lineage import durable_mkdir, file_digest

ARCHIVE = "已处理文档"


def digest_stream(stream):
    digest = hashlib.sha256()
    while chunk := stream.read(1024**2):
        digest.update(chunk)
    return digest.hexdigest()


class SourceArchives:
    def enroll_archive(self, file_id):
        with self.store.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO source_archives(file_id,updated_at,created_at) "
                "VALUES(?,?,?)",
                (file_id, time.time(), time.time()),
            )

    def archive_update(self, file_id, status, error=""):
        with self.store.connect() as db:
            db.execute(
                "UPDATE source_archives SET status=?,phase=CASE WHEN ? IN ('moving','published') "
                "THEN ? ELSE phase END,error=?,updated_at=? WHERE file_id=?",
                (status, status, status, error, time.time(), file_id),
            )

    def archive_jobs(self):
        with self.store.connect() as db:
            rows = db.execute("""SELECT a.*,r.source_key,r.source_path,r.bytes,f.status AS pipeline
                FROM source_archives a JOIN intake_receipts r ON r.file_id=a.file_id
                JOIN files f ON f.id=a.file_id ORDER BY a.created_at DESC LIMIT 100""").fetchall()
        return [dict(r) for r in rows]

    def retry_archive(self, file_id):
        from .sources import SourceError

        with self.store.connect() as db:
            row = db.execute(
                "SELECT status FROM source_archives WHERE file_id=?", (file_id,)
            ).fetchone()
        if not row or row[0] != "failed":
            raise SourceError("归档记录不存在或无需重试")
        self.archive_update(file_id, "pending")
        self.wake.set()
        return {"status": "pending"}

    def archive_one(self):
        with self.store.connect() as db:
            db.execute("SELECT pg_advisory_xact_lock(762190317)")
            row = db.execute("""SELECT r.*,a.phase AS archive_status
                FROM source_archives a JOIN intake_receipts r ON r.file_id=a.file_id
                JOIN files f ON f.id=a.file_id
                WHERE a.status IN ('pending','moving','published') AND f.status='imported'
                AND f.stop_requested=0
                ORDER BY a.created_at LIMIT 1""").fetchone()
            if not row:
                return False
            db.execute(
                "UPDATE source_archives SET status='moving' WHERE file_id=?", (row["file_id"],)
            )
        row = dict(row)
        try:
            if row["source_kind"] == "mapped":
                self.archive_local(row)
            else:
                with self.ssh_lock:
                    self.archive_remote(row)
            self.archive_update(row["file_id"], "completed")
        except Exception as exc:
            if self.stop.is_set():
                self.archive_update(row["file_id"], "pending")
                return False
            self.archive_update(row["file_id"], "failed", self.safe_error(exc))
        return True

    def archive_local(self, row):
        from .sources import SourceError, contained, relative_path

        origin = json.loads(row["origin"])
        root = Path(origin["root"])
        if root.resolve() != self.mounted.resolve():
            raise SourceError("映射根目录已变化，禁止移动旧任务的文件")
        relative_path(row["source_path"])
        if row["source_path"].split("/")[0] == ARCHIVE:
            raise SourceError("不能再次归档已处理文档")
        source = contained(root, row["source_path"])
        target = contained(root, ARCHIVE + "/" + row["source_path"])
        if not source.exists():
            if (
                row["archive_status"] in {"moving", "published"}
                and target.is_file()
                and file_digest(target) == row["sha256"]
            ):
                return
            raise SourceError("源文件已不存在，无法归档")
        before = source.stat()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size != row["bytes"]
            or str(before.st_mtime_ns) != row["source_mtime"]
            or file_digest(source) != row["sha256"]
        ):
            raise SourceError("源文件已变化，保留原文件；请重新扫描处理新版本")
        if row["archive_status"] == "published":
            if not target.is_file() or file_digest(target) != row["sha256"]:
                raise SourceError("归档目标已变化，保留源文件")
        else:
            if target.exists():
                raise SourceError("归档目标已有同名文件，未覆盖；请处理冲突后重试归档")
            durable_mkdir(target.parent)
            self.archive_update(row["file_id"], "moving")
            part = target.parent / (".archive-" + str(uuid.uuid4()))
            try:
                with source.open("rb") as src, part.open("xb") as dst:
                    while chunk := src.read(1024**2):
                        dst.write(chunk)
                    dst.flush()
                    os.fsync(dst.fileno())
                if file_digest(part) != row["sha256"]:
                    raise SourceError("归档复制校验失败，保留源文件")
                os.utime(part, ns=(before.st_atime_ns, before.st_mtime_ns))
                # Publish without overwriting; works across source/target filesystems.
                os.link(part, target)
            finally:
                part.unlink(missing_ok=True)
            fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            self.archive_update(row["file_id"], "published")
        current = source.stat()
        if (current.st_ino, current.st_size, current.st_mtime_ns) != (
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) or file_digest(source) != row["sha256"]:
            raise SourceError("移动前源文件发生变化，保留源文件")
        source.unlink()
        fd = os.open(source.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def archive_remote(self, row):
        from .sources import SourceError, relative_path

        origin = json.loads(row["origin"])
        _, config = self.source(row["source_key"])
        if any(
            origin.get(k) != config[k]
            for k in ("host", "port", "username", "remote_path", "mirror_id", "fingerprint")
        ):
            raise SourceError("SSH 来源配置已变化，禁止移动旧来源文件")
        relative_path(row["source_path"])
        if row["source_path"].split("/")[0] == ARCHIVE:
            raise SourceError("不能再次归档已处理文档")
        with self.connection(row["source_key"], config) as sftp:
            root = sftp.normalize(config["remote_path"])

            def checked(relative, mkdir=False):
                path = root
                for index, component in enumerate(relative.split("/")):
                    path = posixpath.join(path, component)
                    try:
                        info = sftp.lstat(path)
                    except OSError as exc:
                        if exc.errno != errno.ENOENT:
                            raise
                        if mkdir and index < len(relative.split("/")) - 1:
                            sftp.mkdir(path)
                            info = sftp.lstat(path)
                        else:
                            continue
                    if stat.S_ISLNK(info.st_mode):
                        raise SourceError("归档路径不允许符号链接")
                    if index < len(relative.split("/")) - 1 and not stat.S_ISDIR(info.st_mode):
                        raise SourceError("归档路径包含非目录条目")
                return path

            source = checked(row["source_path"])
            target = checked(ARCHIVE + "/" + row["source_path"], mkdir=True)
            try:
                before = sftp.lstat(source)
            except OSError as exc:
                if exc.errno == errno.ENOENT and row["archive_status"] == "moving":
                    with sftp.open(target, "rb") as stream:
                        if digest_stream(stream) == row["sha256"]:
                            return
                raise
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_size != row["bytes"]
                or before.st_mtime != int(row["source_mtime"]) // 10**9
            ):
                raise SourceError("远程源文件已变化，保留原文件")
            with sftp.open(source, "rb") as stream:
                if digest_stream(stream) != row["sha256"]:
                    raise SourceError("远程源文件内容已变化，保留原文件")
            try:
                sftp.lstat(target)
            except OSError as exc:
                if exc.errno != errno.ENOENT:
                    raise
            else:
                raise SourceError("归档目标已有同名文件，未覆盖；请处理冲突后重试归档")
            after = sftp.lstat(source)
            if (after.st_size, after.st_mtime) != (before.st_size, before.st_mtime):
                raise SourceError("远程源文件在校验期间变化，保留原文件")
            self.archive_update(row["file_id"], "moving")
            # Standard SFTP rename requires a nonexistent target (never posix_rename).
            # A cross-filesystem server refusal is safe: retain the source and report failure.
            sftp.rename(source, target)
