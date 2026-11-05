"""Server directory intake and durable SSH/SFTP synchronization, independent of uploads."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import posixpath
import shutil
import socket
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import paramiko

from .formats import file_kind, validate_source
from .lineage import durable_mkdir, durable_replace, file_digest
from .pipeline import PipelineError, write_json
from .remote_scan import RemoteScans
from .source_archive import ARCHIVE, SourceArchives

logger = logging.getLogger(__name__)
SECRETS = {"password", "private_key", "passphrase"}
ACTIVE = {"queued", "running"}
PUBLIC_JOB_COLUMNS = (
    "id,source_key,kind,status,created_at,updated_at,total_files,done_files,total_bytes,"
    "done_bytes,skipped,current_path,error,message,cancel_requested,queue_id"
)


class SourceError(ValueError):
    pass


class Cancelled(Exception):
    pass


def relative_path(value: str, *, empty=False) -> str:
    if empty and not value:
        return ""
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or len(value) > 1000
        or any(ord(c) < 32 for c in value)
        or any(p in {"", ".", "..", ".sync-partial"} for p in value.split("/"))
    ):
        raise SourceError("目录或文件路径无效，只允许来源目录内的相对路径")
    return value


def contained(root: Path, relative: str) -> Path:
    relative_path(relative)
    path = root
    for part in relative.split("/"):
        path = path / part
        if path.is_symlink():
            raise SourceError("不允许读取符号链接")
    if not path.resolve().is_relative_to(root.resolve()):
        raise SourceError("文件超出来源目录")
    return path


def public_source(row, config):
    return {
        "id": row["id"],
        "name": config["name"],
        **{
            k: config[k]
            for k in (
                "host",
                "port",
                "username",
                "remote_path",
                "auth",
                "fingerprint",
                "bandwidth_mb",
            )
        },
        **{"has_" + k: bool(config.get(k)) for k in SECRETS},
    }


class PinPolicy(paramiko.MissingHostKeyPolicy):
    def __init__(self, expected):
        self.expected, self.fingerprint = expected, ""

    def missing_host_key(self, client, hostname, key):
        self.fingerprint = "SHA256:" + base64.b64encode(
            hashlib.sha256(key.asbytes()).digest()
        ).decode().rstrip("=")
        if self.expected and self.expected != self.fingerprint:
            raise SourceError("SSH 主机指纹已变化，连接已拒绝；请核对源服务器身份")


def private_key(config):
    for cls in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
        try:
            return cls.from_private_key(
                io.StringIO(config["private_key"]), password=config.get("passphrase") or None
            )
        except (paramiko.SSHException, ValueError):
            pass
    raise SourceError("SSH 私钥格式或私钥口令不正确")


class Sources(SourceArchives, RemoteScans):
    def __init__(
        self,
        store,
        mounted: Path,
        wake,
        *,
        ssh_factory=None,
        max_files=5000,
        max_file_bytes=200 * 1024**2,
        max_batch_bytes=1024**3,
    ):
        self.store, self.mounted, self.pipeline_wake = store, mounted, wake
        self.max_files, self.max_file_bytes = max_files, max_file_bytes
        self.max_batch_bytes, self.ssh_factory = max_batch_bytes, ssh_factory
        self.stop, self.wake = threading.Event(), threading.Event()
        self.threads = []
        self.ssh_lock = threading.Lock()
        self.clients, self.clients_lock = set(), threading.Lock()
        self.checked = {}
        self.init_remote_scans()

    def source(self, source_id):
        uuid.UUID(source_id)
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM ssh_sources WHERE id=?", (source_id,)).fetchone()
        if not row:
            raise SourceError("SSH 来源不存在")
        return dict(row), self.store.decrypt(row["config"])

    def list_sources(self):
        with self.store.connect() as db:
            rows = db.execute("SELECT * FROM ssh_sources ORDER BY updated_at DESC").fetchall()
        return [public_source(r, self.store.decrypt(r["config"])) for r in rows]

    def save(self, source_id, supplied):
        uuid.UUID(source_id)
        with self.store.connect() as db:
            db.execute("SELECT pg_advisory_xact_lock(762190317)")
            if db.execute(
                "SELECT 1 FROM source_jobs WHERE source_key=? AND status IN ('queued','running')",
                (source_id,),
            ).fetchone():
                raise SourceError("来源有任务正在执行，完成或取消后再修改连接")
            row = db.execute("SELECT config FROM ssh_sources WHERE id=?", (source_id,)).fetchone()
            old = self.store.decrypt(row[0]) if row else {}
            config = {**supplied}
            for key in SECRETS:
                if config[key] is None:
                    config[key] = old.get(key, "")
            if (config["auth"] == "password" and not config["password"]) or (
                config["auth"] == "key" and not config["private_key"]
            ):
                raise SourceError("请填写对应的 SSH 密码或私钥")
            if config["auth"] == "key":
                private_key(config)
            same_server = all(config[k] == old.get(k) for k in ("host", "port"))
            config["fingerprint"] = old.get("fingerprint", "") if same_server else ""
            same_root = same_server and all(
                config[k] == old.get(k) for k in ("username", "remote_path")
            )
            config["mirror_id"] = old["mirror_id"] if same_root else str(uuid.uuid4())
            db.execute(
                "INSERT INTO ssh_sources VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET config=excluded.config,updated_at=excluded.updated_at",
                (source_id, self.store.encrypt(config), time.time()),
            )
        return public_source({"id": source_id}, config)

    def pin(self, source_id, config, fingerprint):
        with self.store.connect() as db:
            db.execute("SELECT pg_advisory_xact_lock(762190317)")
            row = db.execute("SELECT config FROM ssh_sources WHERE id=?", (source_id,)).fetchone()
            current = self.store.decrypt(row[0])
            if any(current[k] != config[k] for k in ("host", "port", "mirror_id")):
                raise SourceError("连接参数已变化，请重试")
            if current["fingerprint"] and current["fingerprint"] != fingerprint:
                raise SourceError("SSH 主机指纹不匹配")
            current["fingerprint"] = fingerprint
            db.execute(
                "UPDATE ssh_sources SET config=? WHERE id=?",
                (self.store.encrypt(current), source_id),
            )

    @contextmanager
    def connection(self, source_id, config):
        if self.ssh_factory:
            with self.ssh_factory(config) as sftp:
                yield sftp
            return
        client = paramiko.SSHClient()
        policy = PinPolicy(config["fingerprint"])
        client.set_missing_host_key_policy(policy)
        with self.clients_lock:
            self.clients.add(client)
        try:
            client.connect(
                hostname=config["host"],
                port=config["port"],
                username=config["username"],
                password=config["password"] if config["auth"] == "password" else None,
                pkey=private_key(config) if config["auth"] == "key" else None,
                allow_agent=False,
                look_for_keys=False,
                timeout=10,
                banner_timeout=15,
                auth_timeout=15,
                channel_timeout=15,
            )
            self.pin(source_id, config, policy.fingerprint)
            client.get_transport().set_keepalive(15)
            with client.open_sftp() as sftp:
                sftp.get_channel().settimeout(30)
                yield sftp
        finally:
            client.close()
            with self.clients_lock:
                self.clients.discard(client)

    def test(self, source_id):
        _, config = self.source(source_id)
        if not self.ssh_lock.acquire(blocking=False):
            raise SourceError("SSH 同步或连接测试正在执行，请稍后再试")
        try:
            with self.connection(source_id, config) as sftp:
                if not stat.S_ISDIR(sftp.stat(config["remote_path"]).st_mode):
                    raise SourceError("远程路径不是目录")
                next(iter(sftp.listdir_iter(config["remote_path"], read_aheads=1)), None)
            return {
                "message": "SSH 连接及目录读取正常",
                "source": self.list_sources_by_id(source_id),
            }
        except SourceError:
            raise
        except Exception as exc:
            raise SourceError(self.safe_error(exc)) from None
        finally:
            self.ssh_lock.release()

    def list_sources_by_id(self, source_id):
        row, config = self.source(source_id)
        return public_source(row, config)

    def directory(self, source_key):
        if source_key == "mapped":
            root = self.mounted
        else:
            _, config = self.source(source_key)
            root = self.store.root / "sync" / source_key / config["mirror_id"] / "files"
        return root

    def directories(self):
        result = [
            {
                "id": "mapped",
                "name": "映射目录",
                "kind": "mapped",
                "path": str(self.mounted),
                "available": self.mounted.is_dir(),
            }
        ]
        for s in self.list_sources():
            root = self.directory(s["id"])
            result.append(
                {
                    "id": s["id"],
                    "name": "同步目录 · " + s["name"],
                    "kind": "ssh",
                    "path": str(root),
                    "available": root.is_dir() and self.sync_ready(s["id"]),
                }
            )
        return result

    def sync_ready(self, source_key):
        if source_key == "mapped":
            return True
        _, config = self.source(source_key)
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT status,config FROM source_jobs WHERE source_key=? "
                "AND kind='sync' ORDER BY created_at DESC",
                (source_key,),
            ).fetchall()
        for row in rows:
            if self.store.decrypt(row["config"])["mirror_id"] == config["mirror_id"]:
                return row["status"] == "completed"
        return False

    def require_synced(self, source_key):
        if not self.sync_ready(source_key):
            raise SourceError("本次目录同步尚未全部成功，请完成或重试同步后再选择文件")

    def synced_paths(self, source_key):
        if source_key == "mapped":
            return None
        _, config = self.source(source_key)
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT id,config FROM source_jobs WHERE source_key=? AND kind='sync' "
                "AND status='completed' ORDER BY created_at DESC",
                (source_key,),
            ).fetchall()
            for row in rows:
                if self.store.decrypt(row["config"])["mirror_id"] == config["mirror_id"]:
                    return {
                        r[0]
                        for r in db.execute(
                            "SELECT i.path FROM source_inventory i "
                            "WHERE i.job_id=? AND i.state='received' "
                            "AND NOT EXISTS (SELECT 1 FROM source_archives a "
                            "JOIN intake_receipts r ON r.file_id=a.file_id "
                            "WHERE a.status='completed' AND r.source_key=i.source_key "
                            "AND r.source_path=i.path "
                            "AND (r.origin::jsonb->>'sync_job_id')=i.job_id)",
                            (row["id"],),
                        )
                    }
        return set()

    def scan(self, source_key, folder=""):
        self.require_synced(source_key)
        root = self.directory(source_key)
        relative_path(folder, empty=True)
        if folder.split("/")[0] == ARCHIVE:
            raise SourceError("已处理文档目录不参与扫描")
        start = contained(root, folder) if folder else root
        if not start.is_dir():
            raise SourceError("目录尚不存在，请先配置映射或完成文件同步")
        files, ignored = [], 0
        allowed = self.synced_paths(source_key)
        count = 0

        def walk_error(error):
            raise SourceError("目录无法读取，请检查容器用户的读取权限")

        for parent, dirs, names in os.walk(start, followlinks=False, onerror=walk_error):
            count += len(dirs) + len(names)
            if count > 100000:
                raise SourceError("目录条目超过 100000，请选择更小的子目录")
            dirs[:] = sorted(
                d
                for d in dirs
                if not d.startswith(".")
                and not (Path(parent) / d).is_symlink()
                and (Path(parent) != root or d != ARCHIVE)
            )
            for name in sorted(names):
                if name.startswith(".") or not file_kind(name):
                    ignored += 1
                    continue
                path = Path(parent) / name
                relative = path.relative_to(root).as_posix()
                if allowed is not None and relative not in allowed:
                    ignored += 1
                    continue
                try:
                    path = contained(root, relative)
                    info = path.stat()
                    if (
                        not stat.S_ISREG(info.st_mode)
                        or not 0 < info.st_size <= self.max_file_bytes
                    ):
                        ignored += 1
                        continue
                    relative_path(relative)
                    if len(name) > 200:
                        raise SourceError("文件名过长")
                except (SourceError, FileNotFoundError):
                    ignored += 1
                    continue
                files.append(
                    {
                        "path": relative,
                        "name": name,
                        "bytes": info.st_size,
                        "mtime": str(info.st_mtime_ns),
                        "kind": file_kind(name),
                    }
                )
                if len(files) > self.max_files:
                    raise SourceError(f"文件数超过 {self.max_files}，请选择更小的子目录")
        files.sort(key=lambda f: f["path"])
        return {"files": files, "ignored": ignored, "bytes": sum(f["bytes"] for f in files)}

    def jobs(self):
        with self.store.connect() as db:
            rows = db.execute(
                f"SELECT {PUBLIC_JOB_COLUMNS} FROM source_jobs ORDER BY created_at DESC LIMIT 100"
            ).fetchall()
        return [self.public_job(r) for r in rows]

    @staticmethod
    def public_job(row):
        return {k: row[k] for k in row.keys() if k != "config"}

    def job(self, job_id, *, public=False):
        uuid.UUID(job_id)
        with self.store.connect() as db:
            columns = PUBLIC_JOB_COLUMNS if public else "*"
            row = db.execute(f"SELECT {columns} FROM source_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise SourceError("来源任务不存在")
        return dict(row)

    def update(self, job_id, **values):
        values["updated_at"] = time.time()
        with self.store.connect() as db:
            db.execute(
                "UPDATE source_jobs SET " + ",".join(k + "=?" for k in values) + " WHERE id=?",
                (*values.values(), job_id),
            )

    def log(self, job_id, message):
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO source_job_logs(job_id,created_at,message) "
                "SELECT ?,?,? WHERE EXISTS(SELECT 1 FROM source_jobs WHERE id=?)",
                (job_id, time.time(), message, job_id),
            )
            # Bound per-job logs; keep recent details and persistent aggregate progress.
            db.execute(
                "DELETE FROM source_job_logs WHERE job_id=? AND id NOT IN "
                "(SELECT id FROM source_job_logs WHERE job_id=? ORDER BY id DESC LIMIT 300)",
                (job_id, job_id),
            )
        logger.info(message, extra={"source_job_id": job_id})

    def logs(self, job_id):
        self.job(job_id)
        with self.store.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM source_job_logs WHERE job_id=? ORDER BY id DESC LIMIT 100",
                    (job_id,),
                )
            ]

    def submit_sync(self, source_id, job_id):
        _, config = self.source(source_id)
        return self.insert_job(job_id, source_id, "sync", config)

    def insert_job(self, job_id, source_key, kind, config, queue_id=None):
        uuid.UUID(job_id)
        with self.store.connect() as db:
            db.execute("SELECT pg_advisory_xact_lock(762190317)")
            old = db.execute("SELECT * FROM source_jobs WHERE id=?", (job_id,)).fetchone()
            if old:
                if old["source_key"] != source_key or old["kind"] != kind:
                    raise SourceError("任务 ID 已被其他来源使用")
                if kind == "import" and self.store.decrypt(old["config"]) != config:
                    raise SourceError("任务 ID 已存在，文件清单不一致")
                return self.public_job(old)
            if db.execute(
                "SELECT 1 FROM source_jobs WHERE source_key=? AND status IN ('queued','running')",
                (source_key,),
            ).fetchone():
                raise SourceError("该目录已有扫描、同步或导入任务，请等待完成或取消")
            now = time.time()
            db.execute(
                "INSERT INTO source_jobs(id,source_key,kind,status,config,created_at,"
                "updated_at,queue_id) VALUES(?,?,?,'queued',?,?,?,?)",
                (job_id, source_key, kind, self.store.encrypt(config), now, now, queue_id),
            )
        self.wake.set()
        return self.public_job(self.job(job_id))

    def submit_import(self, source_key, job_id, selected, config):
        # The request ID also identifies the queue. Retries cannot create duplicate queues.
        try:
            existing = self.job(job_id)
        except SourceError:
            existing = None
        if existing:
            old = self.store.decrypt(existing["config"])
            if (
                existing["kind"] != "import"
                or existing["source_key"] != source_key
                or (old["selected"] != selected or old["pipeline_config"] != config)
            ):
                raise SourceError("任务 ID 已存在，来源、文件或参数不一致")
            return self.public_job(existing)
        # Validate only selected paths; importing a selected subdirectory must not
        # require rescanning a larger root that exceeds the catalog limit.
        self.require_synced(source_key)
        root = self.directory(source_key)
        allowed = self.synced_paths(source_key)
        if not root.is_dir():
            raise SourceError("来源目录不存在，请先完成映射或同步")
        manifest, paths = [], set()
        for entry in selected:
            relative_path(entry["path"])
            if entry["path"].split("/")[0] == ARCHIVE:
                raise SourceError("已处理文档不能再次提交")
            if allowed is not None and entry["path"] not in allowed:
                raise SourceError("所选文件不属于最新完成的同步清单，请重新扫描")
            path = contained(root, entry["path"])
            try:
                info = path.stat()
            except OSError:
                raise SourceError("来源文件不存在或无法读取，请重新扫描") from None
            kind = file_kind(path.name)
            if (
                not kind
                or len(path.name) > 200
                or not stat.S_ISREG(info.st_mode)
                or not (0 < info.st_size <= self.max_file_bytes)
                or info.st_size != entry["bytes"]
                or info.st_mtime_ns != int(entry["mtime"])
            ):
                raise SourceError("来源文件已变化，请重新扫描后选择")
            if entry.get("excel_mode") is not None and kind != "excel":
                raise SourceError("仅 Excel 可以指定表格或问答对模式")
            f = {
                "path": entry["path"],
                "name": path.name,
                "bytes": info.st_size,
                "mtime": info.st_mtime_ns,
                "kind": kind,
            }
            if f["path"] in paths:
                raise SourceError("选择了重复文件")
            paths.add(f["path"])
            manifest.append(
                {
                    **f,
                    "import_mode": entry.get("excel_mode") or config["excel_mode"]
                    if f["kind"] == "excel"
                    else "split",
                }
            )
        if not manifest:
            raise SourceError("请选择文件")
        for start in range(0, len(manifest), config["batch_size"]):
            if (
                sum(f["bytes"] for f in manifest[start : start + config["batch_size"]])
                > self.max_batch_bytes
            ):
                raise SourceError("某一批文件总大小超过限制，请减小每批文件数")
        if (
            shutil.disk_usage(self.store.root).free
            < sum(f["bytes"] for f in manifest) * 3 + 64 * 1024**2
        ):
            raise SourceError("磁盘空间不足，请预留原文件和解析产物的空间")
        snapshot = {
            "root": str(self.directory(source_key)),
            "manifest": manifest,
            "selected": selected,
            "pipeline_config": config,
            "origin": {"root": str(root), "archive": True},
        }
        if source_key != "mapped":
            _, source_config = self.source(source_key)
            snapshot["origin"].update(
                {
                    k: source_config[k]
                    for k in ("host", "port", "username", "remote_path", "mirror_id", "fingerprint")
                }
            )
            with self.store.connect() as db:
                candidates = db.execute(
                    "SELECT id,config FROM source_jobs WHERE source_key=? AND "
                    "kind='sync' AND status='completed' ORDER BY created_at DESC",
                    (source_key,),
                ).fetchall()
            snapshot["origin"]["sync_job_id"] = next(
                r["id"]
                for r in candidates
                if self.store.decrypt(r["config"])["mirror_id"] == source_config["mirror_id"]
            )
        return self.insert_job(job_id, source_key, "import", snapshot, queue_id=job_id)

    def cancel(self, job_id):
        row = self.job(job_id)
        if row["status"] not in ACTIVE:
            raise SourceError("任务已结束")
        with self.store.connect() as db:
            db.execute(
                "UPDATE source_jobs SET cancel_requested=1,status=CASE WHEN status='queued' "
                "THEN 'canceled' ELSE status END WHERE id=? AND status IN ('queued','running')",
                (job_id,),
            )
        return self.public_job(self.job(job_id))

    def retry(self, job_id):
        row = self.job(job_id)
        if row["status"] not in {"failed", "canceled"}:
            raise SourceError("只有失败或取消的任务可以重试")
        config = (
            self.source(row["source_key"])[1]
            if row["kind"] in {"sync", "scan"}
            else self.store.decrypt(row["config"])
        )
        if row["kind"] in {"sync", "scan"}:
            original = self.store.decrypt(row["config"])
            if any(config[k] != original[k] for k in ("host", "port", "mirror_id")):
                raise SourceError("来源服务器或目录已变化，请新建同步任务；旧任务及清单保留")
        with self.store.connect() as db:
            db.execute("SELECT pg_advisory_xact_lock(762190317)")
            if db.execute(
                "SELECT 1 FROM source_jobs WHERE source_key=? AND status IN ('queued','running')",
                (row["source_key"],),
            ).fetchone():
                raise SourceError("该来源仍有任务正在执行")
            db.execute(
                "UPDATE source_jobs SET status='queued',cancel_requested=0,error='',"
                "message='等待重试',config=?,updated_at=? WHERE id=?",
                (self.store.encrypt(config), time.time(), job_id),
            )
        self.wake.set()
        return self.public_job(self.job(job_id))

    def checkpoint(self, job_id):
        if self.stop.is_set():
            raise InterruptedError()
        if time.monotonic() - self.checked.get(job_id, 0) >= 0.5:
            with self.store.connect() as db:
                row = db.execute(
                    "SELECT cancel_requested FROM source_jobs WHERE id=?", (job_id,)
                ).fetchone()
            if row and row[0]:
                raise Cancelled()
            self.checked[job_id] = time.monotonic()

    def start(self):
        with self.store.connect() as db:
            db.execute(
                "UPDATE source_jobs SET status=CASE WHEN cancel_requested=1 THEN 'canceled' "
                "ELSE 'queued' END,message='服务重启，恢复任务' WHERE status='running'"
            )
        for kind in ("sync", "import", "archive", "scan"):
            thread = threading.Thread(
                target=self.loop, args=(kind,), daemon=True, name="source-" + kind
            )
            thread.start()
            self.threads.append(thread)

    def close(self):
        self.stop.set()
        self.wake.set()
        with self.clients_lock:
            clients = list(self.clients)
        for client in clients:
            client.close()
        for thread in self.threads:
            thread.join(timeout=4)
        return not any(t.is_alive() for t in self.threads)

    def loop(self, kind):
        while not self.stop.is_set():
            try:
                worked = self.run_one(kind)
            except Exception:
                logger.exception("来源任务调度异常")
                worked = False
            if not worked:
                self.wake.wait(1)
                self.wake.clear()

    def run_one(self, kind):
        if kind == "archive":
            return self.archive_one()
        with self.store.connect() as db:
            if not db.execute(
                "SELECT 1 FROM source_jobs WHERE kind=? AND status='queued' LIMIT 1", (kind,)
            ).fetchone():
                return False
            db.execute("SELECT pg_advisory_xact_lock(762190317)")
            row = db.execute(
                "SELECT * FROM source_jobs WHERE kind=? AND status='queued' "
                "ORDER BY created_at LIMIT 1",
                (kind,),
            ).fetchone()
            if not row:
                return False
            db.execute("UPDATE source_jobs SET status='running' WHERE id=?", (row["id"],))
        job = dict(row)
        self.checked.pop(job["id"], None)
        try:
            self.log(
                job["id"],
                {"sync": "开始文件同步", "scan": "开始远程扫描", "import": "开始目录文件导入"}[
                    kind
                ],
            )
            if kind == "sync":
                self.sync(job)
            elif kind == "scan":
                self.scan_remote(job)
            else:
                self.intake(job)
            self.checkpoint(job["id"])
            self.update(
                job["id"],
                status="completed",
                current_path="",
                message={
                    "sync": "同步完成，可在工作台选择同步目录",
                    "scan": "远程扫描完成，未下载文件；可查看待同步清单",
                    "import": "原文件已保存，处理队列已创建",
                }[kind],
            )
            self.log(job["id"], "任务完成")
        except InterruptedError:
            self.update(job["id"], status="queued", message="服务停止，等待恢复")
        except Cancelled:
            self.update(job["id"], status="canceled", message="已取消；已完成的文件及任务保留")
        except Exception as exc:
            if self.stop.is_set():
                self.update(job["id"], status="queued", message="服务停止，等待恢复")
                return True
            message = self.safe_error(exc)
            self.update(job["id"], status="failed", error=message, message="任务失败，可重试")
            self.log(job["id"], message)
            logger.error(message, exc_info=True, extra={"source_job_id": job["id"]})
        finally:
            self.checked.pop(job["id"], None)
        return True

    @staticmethod
    def safe_error(exc):
        if isinstance(exc, (SourceError, PipelineError)):
            return str(exc)
        if isinstance(exc, paramiko.AuthenticationException):
            return "SSH 身份验证失败，请检查用户名、密码或私钥"
        if isinstance(
            exc,
            (
                paramiko.SSHException,
                paramiko.ssh_exception.NoValidConnectionsError,
                EOFError,
                TimeoutError,
                socket.gaierror,
                ConnectionError,
            ),
        ):
            return "SSH 连接中断或超时，可重试继续同步"
        if isinstance(exc, PermissionError):
            return "文件访问被拒绝，请检查源目录读取权限和数据目录写入权限"
        return "文件操作失败（" + type(exc).__name__ + "），请核对连接、文件和磁盘空间"

    def remote_manifest(self, sftp, config, job_id):
        root = sftp.normalize(config["remote_path"])
        if not stat.S_ISDIR(sftp.stat(root).st_mode):
            raise SourceError("远程路径不是目录")
        stack, files, count, ignored = [(root, "")], [], 0, 0
        while stack:
            parent, prefix = stack.pop()
            self.checkpoint(job_id)
            for entry in sftp.listdir_iter(parent, read_aheads=4):
                self.checkpoint(job_id)
                count += 1
                if count > 100000:
                    raise SourceError("远程目录条目超过 100000，请配置更小的源目录")
                name = entry.filename
                relative = prefix + name
                try:
                    relative_path(relative)
                except SourceError:
                    ignored += 1
                    continue
                if not prefix and name == ARCHIVE:
                    ignored += 1
                    continue
                if name.startswith(".") or len(name) > 200:
                    ignored += 1
                elif stat.S_ISDIR(entry.st_mode):
                    stack.append((posixpath.join(parent, name), relative + "/"))
                elif (
                    stat.S_ISREG(entry.st_mode)
                    and file_kind(name)
                    and (0 < entry.st_size <= self.max_file_bytes)
                ):
                    files.append(
                        {
                            "path": relative,
                            "bytes": entry.st_size,
                            "mtime": entry.st_mtime,
                            "remote": posixpath.join(parent, name),
                        }
                    )
                    if len(files) > self.max_files:
                        raise SourceError(f"远程文件数超过 {self.max_files}，请配置更小的源目录")
                else:
                    ignored += 1
        return sorted(files, key=lambda f: f["path"]), ignored

    def sync(self, job):
        config = self.store.decrypt(job["config"])
        _, current = self.source(job["source_key"])
        if any(config[k] != current[k] for k in ("host", "port", "mirror_id")):
            raise SourceError("来源连接已变化，请重试同步以使用新配置")
        # Recovery must enforce the pin captured after the original job was submitted.
        config["fingerprint"] = current["fingerprint"]
        target = self.store.root / "sync" / job["source_key"] / config["mirror_id"]
        root, temporary = target / "files", target / ".sync-partial"
        durable_mkdir(root)
        durable_mkdir(temporary)
        with self.ssh_lock:
            for attempt in range(3):
                self.checkpoint(job["id"])
                try:
                    with self.connection(job["source_key"], config) as sftp:
                        self.sync_files(job, config, sftp, root, temporary)
                    return
                except paramiko.AuthenticationException:
                    raise
                except (
                    TimeoutError,
                    paramiko.SSHException,
                    paramiko.ssh_exception.NoValidConnectionsError,
                    EOFError,
                    socket.gaierror,
                    ConnectionError,
                ):
                    if self.stop.is_set():
                        raise InterruptedError() from None
                    if attempt == 2:
                        raise
                    self.log(job["id"], "网络中断，正在重新连接并恢复未完成文件")
                    if self.stop.wait(2**attempt):
                        raise InterruptedError() from None
                    _, current = self.source(job["source_key"])
                    config["fingerprint"] = current["fingerprint"]

    def sync_files(self, job, config, sftp, root, temporary):
        self.update(job["id"], message="正在扫描远程源目录")
        files, ignored = self.remote_manifest(sftp, config, job["id"])
        with self.store.connect() as db:
            saved = db.execute(
                "SELECT path,bytes,mtime FROM source_inventory WHERE job_id=? ORDER BY path",
                (job["id"],),
            ).fetchall()
        if saved and [(r["path"], r["bytes"], int(r["mtime"])) for r in saved] != [
            (f["path"], f["bytes"], f["mtime"]) for f in files
        ]:
            raise SourceError("重试期间远程清单已变化，原任务保留；请新建同步任务")
        self.update(
            job["id"],
            total_files=len(files),
            total_bytes=sum(f["bytes"] for f in files),
            done_files=0,
            done_bytes=0,
            skipped=0,
            message="正在同步文件",
        )
        self.log(job["id"], f"发现 {len(files)} 个支持的文件，忽略 {ignored} 个其他条目")
        with self.store.connect() as db:
            db.executemany(
                "INSERT OR IGNORE INTO source_inventory "
                "(job_id,source_key,path,bytes,mtime,kind,local_path,state,updated_at) "
                "VALUES(?,?,?,?,?,?,?,'pending',?)",
                [
                    (
                        job["id"],
                        job["source_key"],
                        f["path"],
                        f["bytes"],
                        str(f["mtime"]),
                        file_kind(f["path"]),
                        str(contained(root, f["path"])),
                        time.time(),
                    )
                    for f in files
                ],
            )
        done, skipped = 0, 0
        for index, entry in enumerate(files):
            self.checkpoint(job["id"])
            destination = contained(root, entry["path"])
            durable_mkdir(destination.parent)
            self.update(job["id"], current_path=entry["path"])
            cached_digest = None
            with self.store.connect() as db:
                previous = db.execute(
                    "SELECT sha256 FROM source_inventory WHERE job_id=? AND path=? "
                    "AND state='received' AND bytes=? AND mtime=? AND sha256 IS NOT NULL",
                    (job["id"], entry["path"], entry["bytes"], str(entry["mtime"])),
                ).fetchone()
            if previous and destination.is_file():
                cached_digest = file_digest(destination)
            reuse = (
                destination.is_file()
                and destination.stat().st_size == entry["bytes"]
                and (destination.stat().st_mtime_ns == int(entry["mtime"]) * 10**9)
                and previous
                and cached_digest == previous["sha256"]
            )
            if reuse:
                skipped += 1
            else:
                token = hashlib.sha256(entry["path"].encode()).hexdigest()
                part, metadata = temporary / (token + ".part"), temporary / (token + ".json")
                version = {
                    "sync_job_id": job["id"],
                    **{k: entry[k] for k in ("path", "bytes", "mtime")},
                }
                try:
                    saved = json.loads(metadata.read_text())
                except (OSError, ValueError):
                    saved = None
                if saved != version or (part.exists() and part.stat().st_size > entry["bytes"]):
                    part.unlink(missing_ok=True)
                write_json(metadata, version)
                offset = part.stat().st_size if part.exists() else 0
                digest = hashlib.sha256()
                if offset:
                    with part.open("rb") as prefix:
                        while content := prefix.read(1024**2):
                            self.checkpoint(job["id"])
                            digest.update(content)
                if shutil.disk_usage(root).free < entry["bytes"] - offset + 64 * 1024**2:
                    raise SourceError("同步目录磁盘空间不足")
                started, received, last_progress = time.monotonic(), 0, 0
                with sftp.open(entry["remote"], "rb") as remote, part.open("ab") as local:
                    before = remote.stat()
                    if (before.st_size, before.st_mtime) != (entry["bytes"], entry["mtime"]):
                        raise SourceError("同步期间源文件发生变化，请重试")
                    while offset < entry["bytes"]:
                        self.checkpoint(job["id"])
                        # Bounded readv windows avoid per-packet RTT and whole-file prefetch RAM.
                        end = min(offset + 1024**2, entry["bytes"])
                        blocks = [(p, min(32768, end - p)) for p in range(offset, end, 32768)]
                        for content in remote.readv(blocks, max_concurrent_prefetch_requests=16):
                            self.checkpoint(job["id"])
                            if not content:
                                raise SourceError("远程文件提前结束，请重试")
                            local.write(content)
                            digest.update(content)
                            offset += len(content)
                            received += len(content)
                            speed = config.get("bandwidth_mb", 0) * 1024**2
                            if speed:
                                while received / speed > time.monotonic() - started:
                                    self.checkpoint(job["id"])
                                    self.stop.wait(
                                        min(0.2, received / speed - (time.monotonic() - started))
                                    )
                        if time.monotonic() - last_progress >= 1:
                            self.update(job["id"], done_bytes=done + offset)
                            last_progress = time.monotonic()
                    local.flush()
                    os.fsync(local.fileno())
                    after = remote.stat()
                    if offset != entry["bytes"] or (after.st_size, after.st_mtime) != (
                        entry["bytes"],
                        entry["mtime"],
                    ):
                        part.unlink(missing_ok=True)
                        raise SourceError("同步期间源文件发生变化，请重试")
                # Partial files stay outside the selectable directory until fully transferred.
                os.utime(part, ns=(int(entry["mtime"]) * 10**9,) * 2)
                durable_replace(part, destination)
                metadata.unlink(missing_ok=True)
                self.log(job["id"], "已同步：" + entry["path"])
            done += entry["bytes"]
            digest = cached_digest if reuse else digest.hexdigest()
            with self.store.connect() as db:
                db.execute(
                    "UPDATE source_inventory SET sha256=?,state='received',updated_at=? "
                    "WHERE job_id=? AND path=?",
                    (digest, time.time(), job["id"], entry["path"]),
                )
            self.update(job["id"], done_files=index + 1, done_bytes=done, skipped=skipped)
        current, _ = self.remote_manifest(sftp, config, job["id"])
        if [(f["path"], f["bytes"], f["mtime"]) for f in current] != [
            (f["path"], f["bytes"], f["mtime"]) for f in files
        ]:
            raise SourceError("同步完成校验发现远程清单发生变化，请新建同步任务")

    def intake(self, job):
        config = self.store.decrypt(job["config"])
        root, manifest = Path(config["root"]), config["manifest"]
        self.require_synced(job["source_key"])
        pipeline_config = config["pipeline_config"]
        self.store.create_queue(job["queue_id"], manifest, pipeline_config)
        with self.store.connect() as db:
            db.execute(
                "UPDATE queues SET source_key=? WHERE id=?", (job["source_key"], job["queue_id"])
            )
            db.execute(
                "UPDATE files SET message='等待服务器保存原文件' WHERE status='pending_upload' "
                "AND batch_id IN (SELECT id FROM batches WHERE queue_id=?)",
                (job["queue_id"],),
            )
        queue = self.store.queue_snapshot(job["queue_id"])
        self.update(
            job["id"],
            total_files=len(manifest),
            total_bytes=sum(f["bytes"] for f in manifest),
            done_files=0,
            done_bytes=0,
            message="保存原文件并创建分批任务",
        )
        done = count = 0
        for wave in queue["batches"]:
            self.checkpoint(job["id"])
            reserved = self.store.claim_wave_upload(queue["id"], wave["id"])
            start = wave["ordinal"] * queue["batch_size"]
            entries = manifest[start : start + queue["batch_size"]]
            if reserved is not None:
                directories = []
                failures = {}
                try:
                    for row, entry in zip(reserved, entries, strict=True):
                        self.checkpoint(job["id"])
                        source = contained(root, entry["path"])
                        if shutil.disk_usage(self.store.root).free < entry["bytes"] + 64 * 1024**2:
                            raise SourceError("数据目录磁盘空间不足")
                        before = source.stat()
                        if not stat.S_ISREG(before.st_mode) or (
                            before.st_size,
                            before.st_mtime_ns,
                        ) != (entry["bytes"], entry["mtime"]):
                            raise SourceError("待导入文件已变化，请重新扫描并新建任务")
                        directory = self.store.directory(row["id"], row["kind"])
                        durable_mkdir(directory)
                        directories.append(directory)
                        part = directory / (row["name"] + ".part")
                        self.update(job["id"], current_path=entry["path"])
                        with source.open("rb") as src, part.open("wb") as dst:
                            digest = hashlib.sha256()
                            while content := src.read(1024**2):
                                self.checkpoint(job["id"])
                                dst.write(content)
                                digest.update(content)
                            dst.flush()
                            os.fsync(dst.fileno())
                            after = os.fstat(src.fileno())
                        current = source.stat()
                        if (
                            (after.st_size, after.st_mtime_ns, after.st_ino)
                            != (before.st_size, before.st_mtime_ns, before.st_ino)
                            or (current.st_size, current.st_mtime_ns, current.st_ino)
                            != (before.st_size, before.st_mtime_ns, before.st_ino)
                            or part.stat().st_size != entry["bytes"]
                        ):
                            raise SourceError("复制期间源文件发生变化，请重新扫描")
                        destination = directory / row["name"]
                        self.store.check_received_digest(row["id"], digest.hexdigest())
                        durable_replace(part, destination)
                        try:
                            validate_source(destination)
                        except PipelineError as exc:
                            failures[row["id"]] = str(exc)
                            self.log(job["id"], f"文件格式异常：{entry['path']} · {exc}")
                        self.store.receive_file(
                            row["id"],
                            source_kind="mapped" if job["source_key"] == "mapped" else "ssh",
                            source_key=job["source_key"],
                            source_job_id=job["id"],
                            source_path=entry["path"],
                            source_mtime=entry["mtime"],
                            sha256=digest.hexdigest(),
                            origin=config["origin"],
                        )
                        if config["origin"].get("archive"):
                            self.enroll_archive(row["id"])
                    self.store.commit_wave_upload(wave["id"], failures=failures)
                    self.pipeline_wake.set()
                except BaseException:
                    for directory in directories:
                        for part in directory.glob("*.part"):
                            part.unlink(missing_ok=True)
                    self.store.release_wave_upload(wave["id"])
                    raise
            done += sum(f["bytes"] for f in entries)
            count += len(entries)
            self.update(job["id"], done_files=count, done_bytes=done)
            self.log(job["id"], f"第 {wave['ordinal'] + 1} 批原文件已保存")
