"""有界线程工作池：解析和入库独立排队，数据库记录阶段和恢复检查点。"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from urllib.parse import quote
from weakref import WeakValueDictionary

import httpx

from .formats import validate_source
from .imports import created_documents, task_state
from .pipeline import (
    ImportOutcomeUnknown,
    ImportRejected,
    MaxKBClient,
    MinerUClient,
    MinerUResourceMissing,
    PipelineError,
    ReadUnavailable,
    Settings,
    build_documents,
    job_error_details,
    prepare_archive,
    required_string,
    write_json,
)
from .store import Store

logger = logging.getLogger(__name__)


def settings_from(config: dict) -> Settings:
    return Settings(
        **{
            **{k: v for k, v in config.items() if not k.startswith("_")},
            "patterns": tuple(config["patterns"]),
        }
    )


class Workers:
    def __init__(
        self,
        store: Store,
        parse_workers: int,
        import_workers: int,
        client_factory=None,
        *,
        split_workers: int = 2,
    ):
        self.store = store
        self.parse_workers = parse_workers
        self.import_workers = import_workers
        self.store.import_capacity = import_workers
        self.split_workers = split_workers
        self.client_factory = client_factory or (
            lambda: httpx.Client(
                trust_env=False,
                follow_redirects=False,
                limits=httpx.Limits(
                    max_connections=4, max_keepalive_connections=4, keepalive_expiry=60
                ),
            )
        )
        self.file_context = threading.local()
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.threads: list[threading.Thread] = []
        self.capabilities: dict[str, float] = {}
        self.capability_lock = threading.Lock()
        self.capability_checks = WeakValueDictionary()
        self.retention_days = int(os.environ.get("AUDIT_RETENTION_DAYS", "0"))

    def start(self):
        from .knowledge import KnowledgeProvisioner

        provisioner = KnowledgeProvisioner(self)
        provisioner.recover()
        thread = threading.Thread(target=provisioner.loop, daemon=True, name="knowledge-prepare")
        thread.start()
        self.threads.append(thread)
        self.store.recover()
        self.recover_split_receipts()
        self.recover_responses()
        self.store.prune_logs(self.retention_days)
        for importing, count in ((False, self.parse_workers), (True, self.import_workers)):
            for index in range(count):
                thread = threading.Thread(
                    target=self.loop,
                    args=(importing,),
                    daemon=True,
                    name=f"{'import' if importing else 'parse'}-{index}",
                )
                thread.start()
                self.threads.append(thread)
        for index in range(self.import_workers):
            thread = threading.Thread(
                target=self.monitor_loop, daemon=True, name=f"monitor-{index}"
            )
            thread.start()
            self.threads.append(thread)
        for index in range(self.split_workers):
            thread = threading.Thread(
                target=self.loop, args=(False, True), daemon=True, name=f"split-{index}"
            )
            thread.start()
            self.threads.append(thread)

        thread = threading.Thread(target=self.maintenance, daemon=True, name="maintenance")
        thread.start()
        self.threads.append(thread)

    def recover_responses(self):
        with self.store.connect() as db:
            rows = [
                dict(r)
                for r in db.execute(
                    "SELECT f.id,f.kind,f.attempt_id,a.payload FROM files f JOIN import_attempts a "
                    "ON a.id=f.attempt_id WHERE f.status='import_unknown' "
                    "AND a.phase IN ('creating','monitoring') AND f.unresolved_create=0"
                )
            ]
        for row in rows:
            try:
                body = json.loads(
                    (
                        self.store.directory(row["id"], row["kind"])
                        / f"import-{row['attempt_id']}.json"
                    ).read_text()
                )
                expected = json.loads(row["payload"])
                if type(body.get("code")) is not int or body["code"] != 200:
                    continue
                documents = created_documents(body, expected)
                if row["kind"] != "excel" and len(documents) != len(expected):
                    continue
                self.store.record_documents(row["id"], row["attempt_id"], documents)
                self.store.update(
                    row["id"],
                    status="import_monitoring",
                    next_check=0,
                    error="",
                    message="已恢复创建响应，继续查询入库状态",
                )
            except (OSError, ValueError, PipelineError):
                continue

    def close(self):
        self.stop.set()
        self.wake.set()
        deadline = time.monotonic() + 8
        for thread in self.threads:
            thread.join(max(0, deadline - time.monotonic()))
        return not any(thread.is_alive() for thread in self.threads)

    def maintenance(self):
        while not self.stop.wait(600):
            try:
                self.store.prune_logs(self.retention_days)
                self.store.cleanup_retired_mineru()
            except Exception:
                logger.exception("审计日志清理失败")

    def loop(self, importing: bool, splitting: bool = False):
        # Reuse connections for the lifetime of a worker, rather than once per PDF.
        with self.client_factory() as client:
            while not self.stop.is_set():
                try:
                    row = self.store.claim(importing, splitting=splitting)
                    if row:
                        self.execute(
                            row, importing, client=client, phase="split" if splitting else "parse"
                        )
                        continue
                except Exception:
                    logger.exception("后台任务调度异常，稍后重试")
                self.wake.wait(1)
                self.wake.clear()

    def check_capabilities(self, mineru):
        key = hashlib.sha256((mineru.base + "\0" + mineru.settings.mineru_key).encode()).hexdigest()
        with self.capability_lock:
            if time.monotonic() - self.capabilities.get(key, -1000) < 60:
                return
            check = self.capability_checks.get(key)
            if check is None:
                check = threading.Lock()
                self.capability_checks[key] = check
        # Deduplicate probes of one endpoint without holding a global lock over
        # network I/O. A slow old service must not block a different, healthy one.
        with check:
            with self.capability_lock:
                if time.monotonic() - self.capabilities.get(key, -1000) < 60:
                    return
            mineru.check_capabilities()
            with self.capability_lock:
                if len(self.capabilities) >= 128:
                    self.capabilities.clear()
                self.capabilities[key] = time.monotonic()

    def check_stop(self):
        file_id = getattr(self.file_context, "file_id", None)
        if file_id and self.store.stop_requested(file_id):
            raise InterruptedError("用户已请求停止任务")
        if self.stop.is_set():
            raise InterruptedError("服务停止，保留检查点")

    def wait_job(self, mineru: MinerUClient, row: dict) -> dict:
        self.check_stop()
        if row["job_id"]:
            job = mineru._json("GET", f"parse/jobs/{quote(row['job_id'], safe='')}", "恢复解析任务")
        else:
            entry = {"source": {"type": "file_id", "file_id": row["source_id"]}}
            if mineru.settings.page_range:
                entry["page_range"] = mineru.settings.page_range
            self.store.update(row["id"], message="正在提交 MinerU 解析任务")
            try:
                job = mineru._json(
                    "POST",
                    "parse/jobs",
                    "提交 MinerU 解析",
                    json={
                        "files": [entry],
                        "tier": mineru.settings.tier,
                        "output_formats": ["zip"],
                    },
                )
            except (httpx.HTTPError, ReadUnavailable) as exc:
                raise PipelineError(
                    "MinerU 提交结果未确认，服务端可能已接受任务；请先核对 MinerU，"
                    "手动重试可能重复计算。原文件 ID 已记录，未向 MaxKB 入库。"
                ) from exc
        job_id = required_string(job, "job_id", "MinerU 解析")
        self.store.update(row["id"], job_id=job_id, status="parsing", message="等待 MinerU 解析")
        deadline = time.monotonic() + mineru.settings.parse_timeout
        previous = None
        while True:
            self.check_stop()
            if job.get("job_id") != job_id:
                raise PipelineError("MinerU 任务 ID 不匹配")
            status = required_string(job, "status", "MinerU 解析状态")
            if status not in {"queued", "running", "completed", "partial", "failed", "canceled"}:
                raise PipelineError("MinerU 返回未知解析状态")
            if status != previous:
                write_json(self.store.mineru_directory(row["id"]) / "job.json", job)
                self.store.update(
                    row["id"],
                    job_status=status,
                    **(
                        {"parse_state": "waiting" if status == "queued" else "running"}
                        if status in {"queued", "running"}
                        else {}
                    ),
                    message={
                        "queued": "MinerU 排队中",
                        "running": "MinerU 正在解析",
                        "completed": "MinerU 解析完成",
                    }.get(status, "MinerU 解析失败"),
                )
                previous = status
            if status == "completed":
                files = job.get("files")
                if (
                    not isinstance(files, list)
                    or len(files) != 1
                    or not isinstance(files[0], dict)
                    or files[0].get("status") != "completed"
                ):
                    raise PipelineError("MinerU 任务完成但文件结果未完成，请检查解析任务记录")
                self.store.update(
                    row["id"],
                    parse_state="succeeded",
                    parse_error="",
                    parse_finished_at=time.time(),
                )
                return job
            if status in {"partial", "failed", "canceled"}:
                raise PipelineError(f"MinerU 状态 {status}：{job_error_details(job)}")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PipelineError("解析等待超时。上游任务未取消，重试将继续等待同一任务。")
            if self.stop.wait(min(mineru.settings.poll_interval, remaining)):
                self.check_stop()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                continue
            job = mineru._json(
                "GET",
                f"parse/jobs/{quote(job_id, safe='')}",
                "轮询解析状态",
                timeout=min(remaining, mineru.settings.request_timeout),
            )

    def execute(self, row: dict, importing: bool, *, client=None, phase: str = "all"):
        previous = getattr(self.file_context, "file_id", None)
        self.file_context.file_id = row["id"]
        if previous != row["id"]:
            self.store.set_operation_active(row["id"], True)
        try:
            self.check_stop()
            return self._execute(row, importing, client=client, phase=phase)
        except InterruptedError:
            return None
        finally:
            try:
                if previous != row["id"]:
                    self.store.acknowledge_stop(row["id"])
            finally:
                if previous != row["id"]:
                    self.store.set_operation_active(row["id"], False)
                self.file_context.file_id = previous

    def _execute(self, row: dict, importing: bool, *, client=None, phase: str = "all"):
        if client is None:
            with self.client_factory() as own_client:
                return self.execute(row, importing, client=own_client, phase=phase)
        file_id = row["id"]
        settings = None
        attempt_id = None
        split_start = None
        creation_sent = False
        response = None
        documents = []
        started = time.monotonic()
        logger.info(
            "开始处理",
            extra={
                "file_id": file_id,
                "batch_id": row["batch_id"],
                "stage": "import" if importing else phase,
            },
        )
        try:
            settings = settings_from(self.store.file_config(file_id))
            directory = self.store.directory(file_id, row["kind"])
            if (not importing and phase != "split") or row["kind"] != "pdf":
                self.store.verify_original(file_id)
            if client:
                maxkb = MaxKBClient(settings, client)
                if importing:
                    self.check_stop()
                    # The snapshot is immutable. Creation is deliberately never retried.
                    documents = json.loads((directory / "create_payload.json").read_text())
                    attempt_id = self.store.begin_import(file_id, documents)
                    self.store.update(file_id, message="正在导入 MaxKB")
                    creation_sent = True
                    response = (
                        maxkb.import_excel(directory / row["name"], row["import_mode"])
                        if row["kind"] == "excel"
                        else maxkb.create(documents)
                    )
                    write_json(directory / f"import-{attempt_id}.json", response)
                    write_json(directory / "import.json", response)
                    returned = created_documents(response, documents)
                    if row["kind"] != "excel" and len(returned) != len(documents):
                        self.store.record_documents(file_id, attempt_id, returned)
                        self.store.update(
                            file_id,
                            status="cleanup_pending",
                            next_check=0,
                            error="创建返回的文档数量不一致，清理本次已创建文档",
                            unresolved_create=1,
                        )
                        return
                    self.store.record_documents(file_id, attempt_id, returned)
                    self.store.update(
                        file_id,
                        status="import_monitoring",
                        next_check=0,
                        message="文档已创建，等待 MaxKB 完成向量化",
                        error="",
                    )
                    return
                if row["kind"] != "pdf":
                    source = directory / row["name"]
                    validate_source(source)
                    if row["kind"] == "excel":
                        # No API call yet: table/qa POST creates remote documents immediately.
                        documents = [
                            {"name": row["name"], "paragraphs": [], "source_file_id": None}
                        ]
                        write_json(directory / "create_payload.json", documents)
                        self.store.save_paragraphs(
                            file_id, documents, auto_import=settings.auto_import
                        )
                        return
                    self.check_stop()
                    self.store.update(
                        file_id,
                        status="splitting",
                        message="Word 直接上传 MaxKB 切片",
                        split_started_at=time.time(),
                        split_seconds=None,
                    )
                    split_start = time.monotonic()
                    split = self.prepare_maxkb(file_id, source, maxkb)
                    documents = build_documents(split)
                    write_json(directory / "split.json", split)
                    write_json(directory / "create_payload.json", documents)
                    self.store.update(file_id, split_seconds=time.monotonic() - split_start)
                    self.store.save_paragraphs(file_id, documents, auto_import=settings.auto_import)
                    return
                mineru_directory = self.store.mineru_directory(file_id)
                mineru_directory.mkdir(parents=True, exist_ok=True)
                archive = mineru_directory / "maxkb.zip"
                if phase == "split" and not archive.is_file():
                    raise PipelineError("切片队列缺少本地解析产物，请重试解析")
                if not archive.exists():
                    mineru = MinerUClient(settings, client)
                    self.check_capabilities(mineru)
                    raw = mineru_directory / "mineru.zip"
                    if not raw.exists():
                        self.check_stop()
                        if not row["source_id"]:
                            self.store.update(file_id, message="上传 PDF 到 MinerU")
                            source_id = mineru.upload(directory / row["name"])
                            self.store.update(file_id, source_id=source_id)
                            row["source_id"] = source_id
                        job = self.wait_job(mineru, row)
                        self.store.update(file_id, parse_seconds=time.monotonic() - started)
                        self.check_stop()
                        self.store.update(file_id, status="downloading", message="下载解析结果")
                        mineru.download_zip(job, raw)
                    self.check_stop()
                    self.store.update(file_id, status="packaging", message="整理 Markdown 与图片")
                    prepare_archive(
                        raw,
                        archive,
                        mineru_directory / "parsed",
                        markdown_name=row["name"].rsplit(".", 1)[0] + ".md",
                    )
                if phase == "parse":
                    self.store.update(
                        file_id, status="split_queued", message="解析产物已就绪，等待切片"
                    )
                    self.wake.set()
                    return
                self.check_stop()
                self.store.update(
                    file_id,
                    status="splitting",
                    message="上传并请求 MaxKB 切片",
                    split_started_at=time.time(),
                    split_seconds=None,
                )
                split_start = time.monotonic()
                # /document/split already saves the ZIP/images and returns source_file_id.
                # The separate OSS upload was unused and transferred the same ZIP twice.
                self.check_stop()
                split = self.prepare_maxkb(file_id, archive, maxkb)
                documents = build_documents(split)
                write_json(directory / "split.json", split)
                write_json(directory / "create_payload.json", documents)
                self.store.update(file_id, split_seconds=time.monotonic() - split_start)
                self.store.save_paragraphs(file_id, documents, auto_import=settings.auto_import)
        except InterruptedError:
            # Recovery turns active preparation back into queued, but never replays an import.
            return
        except Exception as exc:
            if split_start is not None:
                self.store.update(file_id, split_seconds=time.monotonic() - split_start)
            if isinstance(exc, MinerUResourceMissing):
                self.store.update(file_id, source_id=None, job_id=None, job_status=None)
            if isinstance(exc, httpx.HTTPError):
                message = f"网络请求失败（{type(exc).__name__}），请检查服务连接"
            elif isinstance(exc, (PipelineError, OSError, ValueError)):
                message = str(exc)
            else:
                message = f"处理失败（{type(exc).__name__}），请检查服务配置或日志"
            if settings:
                message = settings.redact(message)
            # Persist only the MinerU stage's error, independent of later download/split/import.
            current = self.store.file(file_id)
            if (
                not importing
                and phase != "split"
                and row["kind"] == "pdf"
                and current["parse_state"] != "succeeded"
            ):
                self.store.update(
                    file_id,
                    parse_state="failed"
                    if current["job_status"] in {"partial", "failed", "canceled", "completed"}
                    else "stopped",
                    parse_error=message[:4000],
                    parse_finished_at=time.time(),
                    parse_seconds=time.monotonic() - started,
                )
            status = "failed"
            if not importing or not creation_sent:
                with self.store.connect() as db:
                    unknown = db.execute(
                        "SELECT 1 FROM remote_calls WHERE file_id=? AND state IN "
                        "('sending','unknown')",
                        (file_id,),
                    ).fetchone()
                if unknown:
                    status = "preparation_unknown"
                    message += "；OSS/切片资源结果不明，请核对上游资源后恢复。"
                elif self.store.pending_resources(file_id):
                    status = "cleanup_pending"
                    self.store.update(file_id, cleanup_target="failed", next_check=0)
            if importing and creation_sent:
                status = "import_rejected" if isinstance(exc, ImportRejected) else "import_unknown"
                body = getattr(exc, "response", None) or response
                if body and attempt_id:
                    write_json(directory / f"import-{attempt_id}.json", body)
                    try:
                        returned = created_documents(body, documents)
                        self.store.record_documents(file_id, attempt_id, returned)
                        if row["kind"] != "excel" and len(returned) != len(documents):
                            self.store.update(file_id, unresolved_create=1)
                        status = "cleanup_pending"
                    except PipelineError:
                        pass
                if not isinstance(exc, (ImportOutcomeUnknown, ImportRejected)):
                    message += "；入库结果待确认，请到 MaxKB 核对，不会自动重试。"
                if status == "import_rejected" and self.store.pending_resources(file_id):
                    status = "cleanup_pending"
                    self.store.update(file_id, cleanup_target="import_failed", next_check=0)
            self.store.update(file_id, status=status, error=message[:4000], message="处理停止")
            logger.error(
                "文件 %s · %s · 来源 %s · 批次 %s · 状态 %s：%s",
                file_id,
                row["name"],
                row["relative_path"],
                row["batch_id"],
                status,
                message[:4000],
                exc_info=True,
                extra={"file_id": file_id, "batch_id": row["batch_id"], "attempt_id": attempt_id},
            )
            if importing and attempt_id:
                self.store.finish_attempt(file_id, status, message)

    def recover_split_receipts(self):
        with self.store.connect() as db:
            calls = [
                dict(r) for r in db.execute("SELECT * FROM remote_calls WHERE state='sending'")
            ]
        for call in calls:
            path = self.store.directory(call["file_id"]) / f"split-call-{call['id']}.json"
            try:
                body = json.loads(path.read_text())
                rows = body["data"]
                if (
                    type(body.get("code")) is not int
                    or body["code"] != 200
                    or not isinstance(rows, list)
                    or not rows
                ):
                    continue
                for document in rows:
                    self.store.own_resource(
                        call["file_id"],
                        document["source_file_id"],
                        "split_source",
                        self.store.file_config(call["file_id"]),
                    )
                self.store.finish_remote_call(call["id"], state="committed", response_path=path)
                self.store.update(
                    call["file_id"],
                    status="cleanup_pending",
                    cleanup_target="failed",
                    next_check=0,
                    error="服务重启恢复了切片响应，先清理资源再手动重试",
                    message="已恢复 OSS ID，等待清理",
                )
            except (OSError, ValueError, KeyError, TypeError, PipelineError):
                continue

    def prepare_maxkb(self, file_id, source, maxkb):
        """Checkpoint the unsafe split call; a lost response must not be replayed."""
        call_id = self.store.begin_remote_call(file_id, "split")
        directory = self.store.directory(file_id)
        path = directory / f"split-call-{call_id}.json"
        try:
            result = maxkb.split(source)
            write_json(path, result)
            rows = result.get("data")
            if not isinstance(rows, list) or not rows:
                raise PipelineError("切片响应没有文档，无法确认 OSS 资源")
            config = self.store.file_config(file_id)
            for document in rows:
                self.store.own_resource(
                    file_id, document.get("source_file_id"), "split_source", config
                )
            self.store.finish_remote_call(call_id, state="committed", response_path=path)
            return result
        except Exception as exc:
            self.store.finish_remote_call(
                call_id,
                state="unknown",
                response_path=path if path.exists() else None,
                error=f"上传/切片结果无法确认（{type(exc).__name__}）",
            )
            raise

    def monitor_loop(self):
        with self.client_factory() as client:
            while not self.stop.is_set():
                try:
                    row = self.store.claim_monitor()
                    if row:
                        self.monitor(row, client)
                        continue
                except Exception:
                    logger.exception("入库状态调度异常，稍后重试")
                self.wake.wait(1)
                self.wake.clear()

    def bind_document_tags(self, row, documents, settings, maxkb):
        tags = sorted(set(settings.document_tag_ids)) if settings.document_tags_enabled else []
        if not tags:
            return
        expected = json.dumps(tags)
        with self.store.connect() as db:
            receipts = {
                r["document_id"]: r["tag_ids"]
                for r in db.execute(
                    "SELECT document_id,tag_ids FROM document_tag_receipts WHERE attempt_id=?",
                    (row["attempt_id"],),
                )
            }
        pending = [
            d["document_id"] for d in documents if receipts.get(d["document_id"]) != expected
        ]
        for start in range(0, len(pending), 100):
            self.check_stop()
            chunk = pending[start : start + 100]
            try:
                body = (
                    maxkb.tag_document(chunk[0], tags)
                    if len(chunk) == 1
                    else maxkb.batch_tag_documents(chunk, tags)
                )
            except Exception as exc:
                raise PipelineError(
                    "文档已入库但标签绑定未确认，请补充标签；不会自动删除或重新创建文档"
                ) from exc
            with self.store.connect() as db:
                db.executemany(
                    "INSERT INTO document_tag_receipts VALUES(?,?,?,?) ON CONFLICT(attempt_id,document_id) DO UPDATE SET tag_ids=excluded.tag_ids,applied_at=excluded.applied_at",
                    [
                        (row["attempt_id"], document_id, expected, time.time())
                        for document_id in chunk
                    ],
                )
            chunk_id = hashlib.sha256(chr(0).join(chunk).encode()).hexdigest()[:16]
            write_json(
                self.store.directory(row["id"], row["kind"])
                / f"tags-{row['attempt_id']}-{chunk_id}.json",
                body,
            )

    def monitor(self, row: dict, client):
        previous = getattr(self.file_context, "file_id", None)
        self.file_context.file_id = row["id"]
        if previous != row["id"]:
            self.store.set_operation_active(row["id"], True)
        try:
            self.check_stop()
            return self._monitor(row, client)
        except InterruptedError:
            return None
        finally:
            try:
                if previous != row["id"]:
                    self.store.acknowledge_stop(row["id"])
            finally:
                if previous != row["id"]:
                    self.store.set_operation_active(row["id"], False)
                self.file_context.file_id = previous

    def _monitor(self, row: dict, client):
        file_id = row["id"]
        settings = None
        cleanup = row["status"] == "cleanup_running"
        try:
            # Setup belongs to the claimed operation too. A bad snapshot/checkpoint
            # must transition to a traceable failure instead of stranding the claim.
            settings = settings_from(self.store.file_config(file_id))
            maxkb = MaxKBClient(settings, client)
            documents = self.store.remote_documents(file_id)
            self.check_stop()
            if not documents and not cleanup:
                raise PipelineError("未记录创建文档的 ID，需人工核对入库结果")
            if cleanup:
                logger.error(
                    "开始失败清理：%s · %s · 来源 %s · 批次 %s · 尝试 %s · %s",
                    file_id,
                    row["name"],
                    row["relative_path"],
                    row["batch_id"],
                    row["attempt_id"],
                    row["error"],
                )
                # Document DELETE can itself remove source OSS. Check scope and
                # shared ownership before the first destructive request.
                linked_sources = {d["source_file_id"] for d in documents if not d["deleted_at"]}
                for resource in self.store.pending_resources(file_id):
                    if resource["resource_id"] not in linked_sources:
                        continue
                    if (
                        resource["endpoint"],
                        resource["workspace_id"],
                        resource["knowledge_id"],
                    ) != (
                        settings.maxkb_base_url.rstrip("/"),
                        settings.workspace_id,
                        settings.knowledge_id,
                    ):
                        raise PipelineError("OSS 资源与当前入库目标不一致，禁止删除")
                    self.store.assert_resource_exclusive(
                        file_id, resource["resource_id"], resource["endpoint"]
                    )
                for document in documents:
                    if not document["deleted_at"] and document["source_file_id"]:
                        self.store.assert_resource_exclusive(
                            file_id,
                            document["source_file_id"],
                            settings.maxkb_base_url.rstrip("/"),
                        )
                for document in documents:
                    if document["deleted_at"]:
                        continue
                    self.check_stop()
                    self.store.record_remote(
                        row["attempt_id"], document["document_id"], state="deleting"
                    )
                    body = maxkb.delete_document(
                        document["document_id"],
                        name=document["name"],
                        source_file_id=document["source_file_id"],
                    )
                    write_json(
                        self.store.directory(file_id, row["kind"])
                        / (f"delete-{row['attempt_id']}-{document['document_id']}.json"),
                        body,
                    )
                    self.store.confirm_document_deleted(
                        file_id,
                        row["attempt_id"],
                        document["document_id"],
                        body,
                    )
                retained = self.store.retain_unconfirmed_resources(file_id)
                if retained:
                    logger.info(
                        "保留 %s 个无确认关联文档的 OSS 资源，不单独删除：%s · %s · 批次 %s",
                        retained,
                        file_id,
                        row["name"],
                        row["batch_id"],
                    )
                self.store.finish_attempt(file_id, "cleaned", row["error"])
                logger.info(
                    "失败文档清理确认完成：%s · %s · 批次 %s", file_id, row["name"], row["batch_id"]
                )
                if row["reimport_requested"]:
                    self.store.update(
                        file_id,
                        status="queued",
                        prepared_at=None,
                        error="",
                        message="旧文档清理完成，重新切片并入库",
                    )
                elif row["cleanup_target"] == "queued" and not row["unresolved_create"]:
                    self.store.update(
                        file_id,
                        status="queued",
                        error="",
                        message="等待重新切片；未关联文档的旧源文件已保留并记录",
                    )
                else:
                    self.store.update(
                        file_id,
                        status="import_unknown"
                        if row["unresolved_create"]
                        else row["cleanup_target"],
                        import_finished_at=time.time(),
                        message="处理失败，本次文档已清理，可手动重试"
                        + ("；未关联文档的 OSS 已保留并记录" if retained else ""),
                    )
                return
            if time.time() - (row["import_started_at"] or time.time()) > settings.import_timeout:
                raise PipelineError("MaxKB 后台处理超时，结果待核对；不会自动删除正在处理的文档")
            states = []
            for document in documents:
                self.check_stop()
                remote = maxkb.document(document["document_id"])
                if remote.get("name") != document["name"]:
                    raise PipelineError("MaxKB 文档名称与本次创建记录不一致，停止自动处理")
                if (
                    not document["source_file_id"]
                    or (remote.get("meta") or {}).get("source_file_id")
                    != document["source_file_id"]
                ):
                    raise PipelineError("MaxKB 文档源文件 ID 与入库尝试不一致，停止自动处理")
                state = task_state(remote)
                count = remote.get("paragraph_count")
                if state == "success" and (
                    type(count) is not int
                    or count < 1
                    or (
                        document["expected_paragraph_count"] is not None
                        and count != document["expected_paragraph_count"]
                    )
                ):
                    raise PipelineError(
                        f"文档 {document['name']} 的入库段落数量异常："
                        f"实际 {count}，预期 {document['expected_paragraph_count']}；需人工核对"
                    )
                states.append(state)
                if state != document["state"] or remote.get("status") != document["raw_status"]:
                    self.store.record_remote(
                        row["attempt_id"],
                        document["document_id"],
                        state=state,
                        raw_status=remote["status"],
                    )
            if "failed" in states:
                logger.error(
                    "MaxKB 后台入库失败：%s · %s · 来源 %s · 批次 %s · 尝试 %s",
                    file_id,
                    row["name"],
                    row["relative_path"],
                    row["batch_id"],
                    row["attempt_id"],
                )
                self.store.update(
                    file_id,
                    status="cleanup_pending",
                    next_check=0,
                    error="MaxKB 向量化或分词失败/取消，清理本次创建的文档",
                    message="入库失败，等待清理失败文档",
                )
            elif all(state == "success" for state in states):
                self.bind_document_tags(row, documents, settings, maxkb)

                logger.info("入库完成", extra={"file_id": file_id, "attempt_id": row["attempt_id"]})
                self.store.finish_attempt(file_id, "success")
                self.store.update(
                    file_id,
                    status="imported",
                    error="",
                    message="MaxKB 后台处理完成，已入库",
                    import_finished_at=time.time(),
                    import_seconds=time.time() - (row["import_started_at"] or time.time()),
                )
            else:
                self.store.schedule_monitor(file_id, settings.import_poll_interval)
        except InterruptedError:
            return
        except Exception as exc:
            error = (
                settings.redact(str(exc))
                if settings is not None and isinstance(exc, PipelineError)
                else (f"MaxKB 状态/清理请求失败（{type(exc).__name__}）")
            )
            logger.error(
                error,
                exc_info=True,
                extra={
                    "file_id": file_id,
                    "batch_id": row["batch_id"],
                    "attempt_id": row["attempt_id"],
                    "stage": "cleanup" if cleanup else "monitor",
                },
            )
            if cleanup:
                logger.error(
                    "失败清理未确认：%s · %s · 批次 %s · %s",
                    file_id,
                    row["name"],
                    row["batch_id"],
                    error,
                )
                self.store.finish_attempt(file_id, "cleanup_failed", error)
                self.store.update(
                    file_id,
                    status="cleanup_failed",
                    error=error[:4000],
                    message="删除未确认成功，禁止重建；可手动重试或核对清理结果",
                )
            elif isinstance(exc, (httpx.HTTPError, ReadUnavailable)) and row["monitor_errors"] < 8:
                count = row["monitor_errors"] + 1
                self.store.update(
                    file_id,
                    status="import_monitoring",
                    monitor_errors=count,
                    next_check=time.time() + min(60, 2**count),
                    message="查询暂时失败，退避后重查",
                    error=error[:4000],
                )
            else:
                self.store.finish_attempt(file_id, "unknown", error)
                self.store.update(
                    file_id,
                    status="import_unknown",
                    error=error[:4000],
                    message="入库结果待核对，禁止自动重建",
                )
