from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from contextlib import asynccontextmanager
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import bleach
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from markdown_it import MarkdownIt
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import UploadFile

from .formats import file_kind, validate_source, workbook_preview
from .history import read_history
from .history_cleanup import (
    CleanupConflict,
    MutationGate,
    cleanup_preview,
    clear_history,
    recover_cleanup,
)
from .imports import created_documents
from .knowledge import DirectoryPlan, install_knowledge_routes, validate_plan
from .lineage import durable_mkdir, durable_replace
from .logging_setup import configure_logging, request_id
from .pipeline import DocumentMissing, MaxKBClient, PipelineError
from .preview import MAX_IMAGE_BYTES, PreviewBusy, PreviewIndex
from .sources import Sources
from .sources_api import install_routes
from .store import Store, public_config, public_file
from .workers import Workers, settings_from

logger = logging.getLogger(__name__)
OSS_ID = re.compile(r"(?:^|/)oss/file/([A-Za-z0-9_-]{1,128})/?(?:\?.*)?$")
MARKDOWN = MarkdownIt("commonmark", {"html": True}).enable("table")


class ConfigInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    mineru_url: str = Field(default="http://mineru.example.com:8000", min_length=1, max_length=2048)
    maxkb_base_url: str = Field(min_length=1, max_length=2048)
    workspace_id: str = Field(min_length=1, max_length=200)
    # Retained for legacy API clients; new browser tasks resolve IDs from their plan.
    knowledge_id: str = Field(default="pending", min_length=1, max_length=200)
    # None retains the stored credential; an explicit empty string clears it.
    maxkb_token: str | None = Field(default=None, max_length=8192)
    mineru_key: str | None = Field(default=None, max_length=8192)
    tier: str = "standard"
    page_range: str = Field(default="", max_length=200)
    limit: int = Field(default=2500, ge=1, le=100000)
    patterns: list[str] = Field(
        default_factory=lambda: [r"(?<=^)# .*|(?<=\n)# .*"], min_length=1, max_length=20
    )
    with_filter: bool = True
    request_timeout: float = Field(default=60, gt=0, le=3600)
    transfer_timeout: float = Field(default=600, gt=0, le=86400)
    parse_timeout: float = Field(default=3600, gt=0, le=86400)
    poll_interval: float = Field(default=2, ge=0.5, le=60)
    batch_size: int = Field(default=100, ge=1, le=100)
    auto_import: bool = False
    knowledge_tags_enabled: bool = True
    document_tags_enabled: bool = True
    inherit_knowledge_tags: bool = True
    excel_mode: Literal["table", "qa"] = "table"
    import_poll_interval: float = Field(default=5, ge=1, le=300)
    import_timeout: float = Field(default=7200, ge=30, le=172800)


class ManifestFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=1000)
    bytes: int = Field(gt=0)
    excel_mode: Literal["table", "qa"] | None = None


class QueueInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: uuid.UUID
    files: list[ManifestFile] = Field(min_length=1, max_length=10000)
    config: ConfigInput
    knowledge_plan: DirectoryPlan | None = None


class ExcelModeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["table", "qa"]


class HistoryCleanupInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    confirmation: Literal["清除历史记录"]
    delete_web_files: bool = False
    delete_ssh_files: bool = False


class ReimportInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: bool = False
    maxkb_token: str | None = Field(default=None, max_length=8192)


class ReconcileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm_absent: bool = False
    maxkb_token: str | None = Field(default=None, max_length=8192)


class ImportInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_ids: list[str] = Field(min_length=1, max_length=100)


class RetryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mineru_url: str = Field(min_length=1, max_length=2048)
    # A blank browser field retains the saved key; an explicit empty string clears it.
    mineru_key: str | None = Field(default=None, max_length=8192)


class PreviewImages(HTMLParser):
    """Rewrite both Markdown and embedded HTML images to the scoped, authenticated proxy."""

    def __init__(self, file_id: str, *, inline_images: bool = True, available_ids=None):
        super().__init__(convert_charrefs=False)
        self.file_id, self.output = file_id, []
        self.inline_images = inline_images
        self.code_depth = 0
        self.available_ids = available_ids

    def handle_starttag(self, tag, attrs):
        if tag in {"pre", "code"}:
            self.code_depth += 1
        if tag != "img":
            self.output.append(self.get_starttag_text())
            return
        attrs = dict(attrs)
        source = attrs.get("src") or ""
        try:
            match = OSS_ID.search(source) if not urlsplit(source).netloc else None
        except ValueError:
            match = None
        if not match:
            self.output.append(escape(attrs.get("alt") or "[图片暂不可预览]"))
            return
        src = f"/api/ingestion/files/{self.file_id}/images/{match[1]}"
        if self.available_ids is not None and match[1] not in self.available_ids:
            self.output.append("[本地图片未关联，请展开本地原图查看]")
            return
        alt = escape(attrs.get("alt") or "", quote=True)
        self.output.append(f'<img src="{src}" alt="{alt}">')

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag in {"pre", "code"}:
            self.code_depth = max(0, self.code_depth - 1)
        self.output.append(f"</{tag}>")

    def handle_data(self, data):
        # CommonMark leaves Markdown following an HTML table in the same raw HTML
        # block until a blank line. MaxKB slices often omit that blank line.
        if self.inline_images and not self.code_depth and "![" in data:
            inline = PreviewImages(
                self.file_id, inline_images=False, available_ids=self.available_ids
            )
            inline.feed(MARKDOWN.renderInline(data))
            inline.close()
            self.output.extend(inline.output)
            return
        self.output.append(data)

    def handle_entityref(self, name):
        self.output.append(f"&{name};")

    def handle_charref(self, name):
        self.output.append(f"&#{name};")


def render_markdown(content: str, file_id: str, available_ids=None) -> str:
    images = PreviewImages(file_id, available_ids=available_ids)
    images.feed(MARKDOWN.render(content))
    images.close()
    html = "".join(images.output)
    return bleach.clean(
        html,
        tags={
            "p",
            "br",
            "hr",
            "strong",
            "em",
            "s",
            "blockquote",
            "pre",
            "code",
            "ul",
            "ol",
            "li",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "table",
            "thead",
            "tbody",
            "tfoot",
            "caption",
            "tr",
            "td",
            "th",
            "a",
            "img",
        },
        attributes={
            "a": ["href", "title"],
            "img": ["src", "alt", "title"],
            "td": ["rowspan", "colspan"],
            "th": ["rowspan", "colspan"],
        },
        protocols={"http", "https"},
        strip=True,
    )


class BodyLimit:
    """Reject excessive request bodies while streaming, before multipart parsing consumes them."""

    def __init__(self, app, maximum: int):
        self.app, self.maximum = app, maximum

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        upload = (
            path == "/api/ingestion/batches" or (path.startswith("/api/ingestion/queues/") and "/batches/" in path)
        ) and scope.get("method") == "POST"
        maximum = (
            self.maximum
            if upload
            else (
                12 * 1024**2
                if path == "/api/ingestion/queues"
                or (path.startswith("/api/ingestion/source-directories/") and path.endswith("/import"))
                else 128 * 1024
            )
        )
        headers = dict(scope.get("headers", []))
        try:
            size = int(headers.get(b"content-length", b"0"))
        except ValueError:
            size = maximum + 1
        if size > maximum:
            response = JSONResponse({"detail": "上传内容超过服务允许的大小"}, status_code=413)
            return await response(scope, receive, send)
        consumed = 0

        async def limited_receive():
            nonlocal consumed
            message = await receive()
            consumed += len(message.get("body", b""))
            if consumed > maximum:
                raise HTTPException(413, "上传内容超过服务允许的大小")
            return message

        return await self.app(scope, limited_receive, send)


def create_app(
    data_dir: Path | None = None,
    *,
    run_workers: bool = True,
    client_factory=None,
    source_dir: Path | None = None,
    ssh_factory=None,
) -> FastAPI:
    from ..config import PROJECT_ROOT
    mutation_gate = MutationGate()
    parse_count = int(os.environ.get("PARSE_WORKERS", "1"))
    import_count = int(os.environ.get("IMPORT_WORKERS", "5"))
    split_count = int(os.environ.get("SPLIT_WORKERS", "2"))
    max_files = int(os.environ.get("MAX_BATCH_FILES", "100"))
    max_file_bytes = int(os.environ.get("MAX_FILE_MB", "200")) * 1024**2
    max_batch_bytes = int(os.environ.get("MAX_BATCH_MB", "20480")) * 1024**2
    if not (
        1 <= parse_count <= 16
        and 1 <= import_count <= 16
        and 1 <= split_count <= 16
        and 1 <= max_files <= 100
    ):
        raise ValueError("工作线程必须为 1–16，单批文件数必须为 1–100")
    if min(max_file_bytes, max_batch_bytes) <= 0:
        raise ValueError("上传大小限制必须大于 0")

    @asynccontextmanager
    async def lifespan(app):
        root = data_dir or Path(os.environ.get("INGESTION_DATA_DIR", str(PROJECT_ROOT / "data" / "ingestion")))
        root.mkdir(parents=True, exist_ok=True)
        # One scheduler owns this volume. Prevent uvicorn multi-worker duplicate recovery.
        lock = (root / "service.lock").open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise RuntimeError("数据目录已被其他服务占用；请使用单个 Uvicorn 进程") from None
        try:
            store = Store(root)
            configure_logging(store.root)
            logger.info("知识入库 PostgreSQL 存储已就绪")
        except BaseException:
            lock.close()
            raise
        workers = Workers(
            store, parse_count, import_count, client_factory, split_workers=split_count
        )
        app.state.store, app.state.workers = store, workers
        app.state.preview_index = PreviewIndex(store)
        sources = Sources(
            store,
            source_dir or Path(os.environ.get("SOURCE_DIR", "/sources")),
            workers.wake,
            ssh_factory=ssh_factory,
            max_files=int(os.environ.get("MAX_QUEUE_FILES", "5000")),
            max_file_bytes=max_file_bytes,
            max_batch_bytes=max_batch_bytes,
        )
        app.state.sources = sources
        try:
            await asyncio.to_thread(recover_cleanup, store)
        except BaseException:
            lock.close()
            raise
        try:
            if run_workers:
                workers.start()
                sources.start()
            yield
        finally:
            sources_stopped = await asyncio.to_thread(sources.close)
            if await asyncio.to_thread(workers.close) and sources_stopped:
                lock.close()
            else:
                # Keep exclusive ownership until the old threads finish or the process exits.
                logging.getLogger(__name__).warning("仍有上游请求未退出，保留调度锁直到线程停止")

                def release_lock():
                    for thread in workers.threads + sources.threads:
                        thread.join()
                    lock.close()

                threading.Thread(
                    target=release_lock,
                    daemon=True,
                    name="shutdown-lock",
                ).start()

    app = FastAPI(
        title="MinerU → MaxKB",
        version="0.2.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.mutation_gate=mutation_gate
    app.add_middleware(BodyLimit, maximum=max_batch_bytes + 1024 * 1024)

    @app.middleware("http")
    async def guards(request: Request, call_next):
        guarded = request.method not in {"GET", "HEAD", "OPTIONS"}
        correlation = uuid.uuid4().hex
        context_token = request_id.set(correlation)
        started = time.monotonic()
        try:
            try:
                if guarded:
                    with mutation_gate.enter(
                        cleanup=request.url.path.startswith("/api/ingestion/history/cleanup")
                    ):
                        response = await call_next(request)
                else:
                    response = await call_next(request)
            except CleanupConflict as exc:
                response = JSONResponse({"detail": str(exc)}, status_code=409)
            except Exception:
                logger.exception(
                    "HTTP 请求处理异常",
                    extra={"method": request.method, "path": request.url.path},
                )
                response = JSONResponse(
                    {"detail": "服务处理失败，请凭请求编号查看服务日志"}, status_code=500
                )
            response.headers["X-Request-ID"] = correlation
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "same-origin"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
            )
            if request.url.path.startswith("/api/ingestion/"):
                response.headers.setdefault("Cache-Control", "no-store")
                if request.url.path != "/api/ingestion/health":
                    logger.log(
                        logging.WARNING if response.status_code >= 400 else logging.INFO,
                        "HTTP 响应头已返回",
                        extra={
                            "method": request.method,
                            "path": request.url.path,
                            "http_status": response.status_code,
                            "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
                        },
                    )
            return response
        finally:
            request_id.reset(context_token)

    @app.exception_handler(PipelineError)
    async def pipeline_error(request, exc):
        return JSONResponse(
            {"detail": str(exc)}, status_code=429 if isinstance(exc, PreviewBusy) else 422
        )

    def get_file(file_id: str):
        row = app.state.store.file(file_id)
        if not row:
            raise HTTPException(404, "文件不存在")
        return row

    @app.get("/api/ingestion/health")
    def health():
        with app.state.store.connect() as db:
            db.execute("SELECT 1").fetchone()
        if run_workers:
            dead = [
                thread.name
                for thread in app.state.workers.threads + app.state.sources.threads
                if not thread.is_alive()
            ]
            if dead:
                return JSONResponse(
                    {
                        "status": "degraded",
                        "storage": "postgresql",
                        "workers_down": dead,
                    },
                    status_code=503,
                )
        return {"status": "ok", "storage": "postgresql"}

    @app.get("/api/ingestion/settings")
    def get_settings():
        return {
            "config": public_config(app.state.store.settings()),
            "limits": {
                "parse_workers": parse_count,
                "import_workers": import_count,
                "split_workers": split_count,
                "max_files": max_files,
                "max_file_mb": max_file_bytes // 1024**2,
                "max_batch_mb": max_batch_bytes // 1024**2,
                "max_queue_files": int(os.environ.get("MAX_QUEUE_FILES", "5000")),
            },
        }

    @app.get("/api/ingestion/metrics")
    def metrics():
        with app.state.store.connect() as db:
            statuses = dict(
                db.execute("SELECT status,COUNT(*) FROM files GROUP BY status").fetchall()
            )
            durations = {}
            for field in ("parse_seconds", "split_seconds", "import_seconds"):
                row = db.execute(
                    f"SELECT COUNT({field}),AVG({field}),MAX({field}) FROM files"
                ).fetchone()
                durations[field] = {"samples": row[0], "average": row[1], "maximum": row[2]}
        return {
            "files": sum(statuses.values()),
            "statuses": statuses,
            "durations": durations,
            "workers": {
                "parse": parse_count,
                "split": split_count,
                "create": import_count,
                "monitor": import_count,
            },
        }

    @app.put("/api/ingestion/settings")
    def save_settings(body: ConfigInput):
        config = body.model_dump()
        old = app.state.store.settings()
        for key in ("knowledge_tags_enabled", "document_tags_enabled", "inherit_knowledge_tags"):
            if key not in body.model_fields_set:
                config[key] = old.get(key, True)
        for key in ("maxkb_token", "mineru_key"):
            if config[key] is None:
                config[key] = old[key]
        for pattern in config["patterns"]:
            if not pattern or len(pattern) > 2000:
                raise HTTPException(422, "每个切片规则需为 1–2000 个字符")
        settings_from(config)
        app.state.store.save_settings(config)
        return public_config(config)

    @app.get("/api/ingestion/batches")
    def list_batches():
        return app.state.store.batches()

    @app.get("/api/ingestion/history")
    def history_records(
        view: Literal["tasks", "batches", "files"] = "tasks",
        state: Literal[
            "all",
            "pending",
            "active",
            "review",
            "importing",
            "imported",
            "import_failed",
            "uncertain",
            "failed",
        ] = "all",
        search: str = Query(default="", max_length=200),
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=25, ge=1, le=100),
        batch_id: uuid.UUID | None = None,
    ):
        if batch_id and view != "files":
            raise HTTPException(422, "批次筛选仅用于文件记录")
        return read_history(
            app.state.store,
            view=view,
            state=state,
            search=search.strip(),
            offset=offset,
            limit=limit,
            batch_id=str(batch_id) if batch_id else None,
        )

    @app.post("/api/ingestion/files/{file_id}/stop")
    def stop_file(file_id: uuid.UUID):
        try:
            app.state.store.request_stop(file_id=str(file_id))
        except KeyError:
            raise HTTPException(404, "文件不存在") from None
        app.state.workers.wake.set()
        return public_file(app.state.store.file(str(file_id)))

    @app.post("/api/ingestion/queues/{queue_id}/stop")
    def stop_queue(queue_id: uuid.UUID):
        try:
            result = app.state.store.request_stop(queue_id=str(queue_id))
        except KeyError:
            raise HTTPException(404, "任务不存在") from None
        app.state.workers.wake.set()
        app.state.sources.wake.set()
        return result

    @app.post("/api/ingestion/batches/{batch_id}/stop")
    def stop_batch(batch_id: uuid.UUID):
        try:
            result = app.state.store.request_stop(batch_id=str(batch_id))
        except KeyError:
            raise HTTPException(404, "批次不存在") from None
        app.state.workers.wake.set()
        app.state.sources.wake.set()
        return result

    @app.get("/api/ingestion/history/cleanup")
    def preview_history_cleanup():
        return cleanup_preview(app.state.store)

    @app.post("/api/ingestion/history/cleanup")
    def delete_history(body: HistoryCleanupInput):
        with mutation_gate.execute_cleanup():
            try:
                result = clear_history(
                    app.state.store,
                    revision=body.revision,
                    delete_web_files=body.delete_web_files,
                    delete_ssh_files=body.delete_ssh_files,
                    protected_source=app.state.sources.mounted,
                )
            except CleanupConflict as exc:
                raise HTTPException(409, str(exc)) from exc
            except Exception as exc:
                logger.exception("本地历史清理失败")
                with app.state.store.connect() as db:
                    mutation_gate.blocked = bool(
                        db.execute(
                            "SELECT 1 FROM history_cleanups WHERE state='staging' LIMIT 1"
                        ).fetchone()
                    )
                raise HTTPException(500, "清理未完成，请查看服务日志；必要时重启服务恢复") from exc
            with app.state.preview_index.cache_lock:
                app.state.preview_index.cache.clear()
            return result

    @app.post("/api/ingestion/history/cleanup/retry")
    def retry_history_cleanup():
        with mutation_gate.execute_cleanup():
            recover_cleanup(app.state.store)
            return cleanup_preview(app.state.store)

    @app.get("/api/ingestion/mineru/files")
    def mineru_files(
        queue_id: uuid.UUID | None = None,
        batch_id: uuid.UUID | None = None,
        state: Literal["all", "pending", "processing", "succeeded", "failed"] = "all",
        search: str = Query(default="", max_length=200),
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=100),
    ):
        if bool(queue_id) == bool(batch_id):
            raise HTTPException(422, "必须指定一个队列或批次")
        result = app.state.store.parse_records(
            queue_id=str(queue_id) if queue_id else None,
            batch_id=str(batch_id) if batch_id else None,
            state=state,
            search=search.strip(),
            offset=offset,
            limit=limit,
        )
        if result is None:
            raise HTTPException(404, "队列或批次不存在")
        return result

    def validate_config(body: ConfigInput, fallback: dict | None = None):
        config = body.model_dump()
        saved = fallback or app.state.store.settings()
        for key in ("knowledge_tags_enabled", "document_tags_enabled", "inherit_knowledge_tags"):
            if key not in body.model_fields_set:
                config[key] = saved.get(key, True)
        for key in ("maxkb_token", "mineru_key"):
            if config[key] is None:
                config[key] = saved[key]
        if any(not p or len(p) > 2000 for p in config["patterns"]):
            raise HTTPException(422, "切片规则需为 1–2000 个字符")
        settings_from(config)
        return config

    install_routes(app, ConfigInput, validate_config, max_files)
    install_knowledge_routes(app, ConfigInput, validate_config)

    @app.post("/api/ingestion/queues", status_code=201)
    def create_queue(body: QueueInput):
        store = app.state.store
        if len(body.files) > int(os.environ.get("MAX_QUEUE_FILES", "5000")):
            raise HTTPException(413, "文件夹文件数超过队列上限")
        with store.connect() as db:
            existing = db.execute(
                "SELECT config FROM batches WHERE queue_id=? LIMIT 1", (str(body.id),)
            ).fetchone()
        config = validate_config(body.config, store.decrypt(existing[0]) if existing else None)
        if config["batch_size"] > max_files:
            raise HTTPException(422, f"每批文件数不能超过 {max_files}")
        manifest = []
        paths = set()
        for entry in body.files:
            path = entry.path.replace("\\", "/")
            name = path.split("/")[-1]
            if (
                path.startswith("/")
                or any(p in {"", "..", "."} for p in path.split("/"))
                or any(ord(c) < 32 for c in path)
                or len(name) > 200
                or not file_kind(name)
                or path in paths
            ):
                raise HTTPException(
                    422, "文件清单含重复路径或不支持的文件名（PDF、DOC/DOCX、XLS/XLSX）"
                )
            if entry.bytes > max_file_bytes:
                raise HTTPException(413, f"{name} 超过单文件大小限制")
            paths.add(path)
            if entry.excel_mode is not None and file_kind(name) != "excel":
                raise HTTPException(422, "仅 Excel 文件可以指定表格/问答对模式")
            manifest.append(
                {
                    "path": path,
                    "name": name,
                    "bytes": entry.bytes,
                    "import_mode": (entry.excel_mode or config["excel_mode"])
                    if file_kind(name) == "excel"
                    else "split",
                }
            )
        for start in range(0, len(manifest), config["batch_size"]):
            if (
                sum(f["bytes"] for f in manifest[start : start + config["batch_size"]])
                > max_batch_bytes
            ):
                raise HTTPException(413, "某一批文件总大小超过限制，请减小每批文件数")
        if (
            shutil.disk_usage(store.root).free
            < sum(f["bytes"] for f in manifest) * 3 + 64 * 1024**2
        ):
            raise HTTPException(507, "数据目录空间不足，请预留源文件和解析产物的存储空间")
        try:
            plan = validate_plan(body.knowledge_plan, [f["path"] for f in manifest])
            if not plan and config["knowledge_id"] == "pending":
                raise HTTPException(422, "请先确认要创建的知识库及目标目录")
            if plan:
                config["_knowledge_plan"] = plan
            store.create_queue(str(body.id), manifest, config)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return store.queue_snapshot(str(body.id))

    @app.get("/api/ingestion/queues/{queue_id}")
    def get_queue(
        queue_id: uuid.UUID,
        problem_offset: int = Query(default=0, ge=0),
        problem_limit: int = Query(default=50, ge=1, le=100),
    ):
        result = app.state.store.queue_snapshot(
            str(queue_id),
            problem_offset=problem_offset,
            problem_limit=problem_limit,
        )
        if result is None:
            raise HTTPException(404, "队列不存在")
        return result

    @app.get("/api/ingestion/queues/{queue_id}/manifest")
    def queue_manifest(queue_id: uuid.UUID):
        store = app.state.store
        if not store.queue_snapshot(str(queue_id)):
            raise HTTPException(404, "队列不存在")
        with store.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT f.relative_path,f.bytes,f.kind,f.import_mode FROM files f "
                    "JOIN batches b ON b.id=f.batch_id "
                    "WHERE b.queue_id=? ORDER BY f.queue_position",
                    (str(queue_id),),
                )
            ]

    @app.get("/api/ingestion/queues")
    def list_queues():
        return app.state.store.queues()

    @app.get("/api/ingestion/queues/{queue_id}/report")
    def queue_report(queue_id: uuid.UUID):
        store = app.state.store
        queue = store.queue_snapshot(str(queue_id))
        if not queue:
            raise HTTPException(404, "队列不存在")
        with store.connect() as db:
            files = [
                dict(r)
                for r in db.execute(
                    "SELECT f.id,f.name,f.relative_path,f.kind,f.status,f.error,f.attempt_id,"
                    "b.ordinal,"
                    "r.sha256,r.source_kind,r.source_key,r.source_job_id,r.source_mtime,"
                    "r.local_path "
                    "FROM files f JOIN batches b ON b.id=f.batch_id LEFT JOIN "
                    "intake_receipts r ON r.file_id=f.id "
                    "WHERE b.queue_id=? ORDER BY f.queue_position",
                    (str(queue_id),),
                )
            ]
            resources = [
                dict(r)
                for r in db.execute(
                    "SELECT r.* FROM remote_resources r JOIN files f ON f.id=r.file_id "
                    "JOIN batches b ON b.id=f.batch_id WHERE b.queue_id=?",
                    (str(queue_id),),
                )
            ]
        return JSONResponse(
            {"queue": queue, "files": files, "resources": resources},
            headers={"Content-Disposition": f'attachment; filename="queue-{queue_id}.json"'},
        )

    @app.post("/api/ingestion/batches", status_code=201)
    @app.post("/api/ingestion/queues/{queue_id}/batches/{wave_id}", status_code=201)
    async def upload(request: Request, queue_id: str | None = None, wave_id: str | None = None):
        store = app.state.store
        config = await asyncio.to_thread(store.settings)
        reserved = None
        if queue_id or wave_id:
            if not queue_id or not wave_id:
                raise HTTPException(422, "队列和批次 ID 必须同时提供")
            try:
                reserved = await asyncio.to_thread(store.claim_wave_upload, queue_id, wave_id)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            if reserved is None:
                return await asyncio.to_thread(store.snapshot, wave_id)
            config = await asyncio.to_thread(store.batch_config, wave_id)
        batch_id = wave_id or str(uuid.uuid4())
        entries = []
        directories = []
        failures = {}
        file_id = None
        batch_created = False
        batch_size = 0
        try:
            async with request.form(max_files=max_files, max_fields=1) as form:
                raw_config = form.get("config")
                if raw_config is not None and reserved is None:
                    try:
                        supplied = validate_config(
                            ConfigInput.model_validate_json(raw_config), config
                        )
                    except ValueError as exc:
                        raise HTTPException(422, "上传参数格式无效") from exc
                    for key in ("maxkb_token", "mineru_key"):
                        if supplied[key] is None:
                            supplied[key] = config[key]
                    config = supplied
                if reserved is None and config["knowledge_id"] == "pending":
                    raise HTTPException(422, "请通过工作台确认知识库后创建上传队列")
                settings_from(config)
                if any(not p or len(p) > 2000 for p in config["patterns"]):
                    raise HTTPException(422, "切片规则需为 1–2000 个字符")
                uploads = form.getlist("files")
                if not uploads or not all(isinstance(item, UploadFile) for item in uploads):
                    raise HTTPException(422, "请选择 PDF、Word 或 Excel 文件")
                if reserved is not None and len(uploads) != len(reserved):
                    raise HTTPException(422, "上传文件数与队列清单不一致")
                for index, item in enumerate(uploads):
                    file_id = reserved[index]["id"] if reserved is not None else None
                    name = (item.filename or "").replace("\\", "/").split("/")[-1]
                    if not file_kind(name) or len(name) > 200 or any(ord(c) < 32 for c in name):
                        raise HTTPException(
                            422, "仅支持 PDF、DOC/DOCX、XLS/XLSX，文件名最长 200 字符"
                        )
                    if reserved is not None:
                        expected = reserved[index]
                        if (
                            name != expected["name"]
                            or (item.filename or "").replace("\\", "/") != expected["relative_path"]
                        ):
                            raise HTTPException(422, "上传文件顺序或相对路径与队列清单不一致")
                    file_id = reserved[index]["id"] if reserved is not None else str(uuid.uuid4())
                    directory = store.directory(file_id, file_kind(name))
                    await asyncio.to_thread(durable_mkdir, directory, exist_ok=reserved is not None)
                    directories.append(directory)
                    size = 0
                    digest = hashlib.sha256()
                    first = True
                    destination = directory / name
                    part = directory / (name + ".part")
                    with part.open("wb") as stream:
                        while chunk := await item.read(1024 * 1024):
                            if (
                                reserved is None
                                and first
                                and file_kind(name) == "pdf"
                                and b"%PDF-" not in chunk[:1024]
                            ):
                                raise HTTPException(422, f"{name} 缺少 PDF 文件头")
                            first = False
                            size += len(chunk)
                            batch_size += len(chunk)
                            if size > max_file_bytes or batch_size > max_batch_bytes:
                                raise HTTPException(413, "文件或批次大小超过限制")
                            await asyncio.to_thread(stream.write, chunk)
                            digest.update(chunk)
                        await asyncio.to_thread(stream.flush)
                        await asyncio.to_thread(os.fsync, stream.fileno())
                    if first:
                        raise HTTPException(422, "不能上传空文件")
                    if reserved is not None and size != reserved[index]["bytes"]:
                        raise HTTPException(422, "文件大小与队列清单不一致，请重新选择原文件夹")
                    # Validate Office containers before exposing a completed upload.
                    if reserved is not None:
                        await asyncio.to_thread(
                            store.check_received_digest, file_id, digest.hexdigest()
                        )
                    await asyncio.to_thread(durable_replace, part, destination)
                    try:
                        await asyncio.to_thread(validate_source, destination)
                    except PipelineError as exc:
                        if reserved is None:
                            raise
                        failures[file_id] = str(exc)
                        logger.error(
                            "原文件格式异常：%s · %s · 队列 %s · %s",
                            file_id,
                            reserved[index]["relative_path"],
                            queue_id,
                            str(exc),
                        )
                    entries.append(
                        {"id": file_id, "name": name, "bytes": size, "sha256": digest.hexdigest()}
                    )
                    if reserved is not None:
                        await asyncio.to_thread(
                            store.receive_file,
                            file_id,
                            source_kind="browser",
                            source_key=queue_id,
                            source_path=reserved[index]["relative_path"],
                            sha256=digest.hexdigest(),
                        )
            if reserved is not None:
                await asyncio.to_thread(store.commit_wave_upload, batch_id, failures=failures)
            else:
                await asyncio.to_thread(store.create_batch, batch_id, entries, config, ready=False)
                batch_created = True
                for entry in entries:
                    await asyncio.to_thread(
                        store.receive_file,
                        entry["id"],
                        source_kind="browser",
                        source_key=batch_id,
                        source_path=entry["name"],
                        sha256=entry["sha256"],
                    )
                await asyncio.to_thread(store.complete_batch_intake, batch_id)
        except BaseException as exc:
            for directory in directories:
                if reserved is None and not batch_created:
                    await asyncio.to_thread(shutil.rmtree, directory, True)
                else:
                    for part in directory.glob("*.part"):
                        await asyncio.to_thread(part.unlink, missing_ok=True)
            if reserved is not None:
                await asyncio.to_thread(store.release_wave_upload, batch_id)
                if file_id:
                    error = (
                        str(exc.detail)
                        if isinstance(exc, HTTPException)
                        else str(exc)
                        if isinstance(exc, PipelineError)
                        else f"上传中断（{type(exc).__name__}）"
                    )
                    await asyncio.to_thread(
                        store.update,
                        file_id,
                        status="pending_upload",
                        error=error,
                        message="上传未完成，原件保留，等待重试",
                    )
            raise
        app.state.workers.wake.set()
        return await asyncio.to_thread(store.snapshot, batch_id)

    @app.get("/api/ingestion/files/{file_id}/provenance")
    def provenance(file_id: uuid.UUID):
        if not app.state.store.file(str(file_id)):
            raise HTTPException(404, "文件不存在")
        return app.state.store.provenance(str(file_id))

    @app.post("/api/ingestion/files/{file_id}/resolve-preparation", status_code=202)
    def resolve_preparation(file_id: uuid.UUID, body: ReimportInput):
        store = app.state.store
        fid = str(file_id)
        with store.connect() as db:
            db.execute("SELECT pg_advisory_xact_lock(762190317)")
            row = db.execute("SELECT status FROM files WHERE id=?", (fid,)).fetchone()
            if not row or row["status"] != "preparation_unknown" or not body.confirm:
                raise HTTPException(409, "需确认已在 MaxKB 核对并处理本次上传/切片遗留资源")
            db.execute(
                "UPDATE remote_calls SET state='manually_resolved',finished_at=? "
                "WHERE file_id=? AND state IN ('sending','unknown')",
                (time.time(), fid),
            )
            db.execute(
                "INSERT INTO audit_logs(file_id,created_at,event,detail) VALUES(?,?,?,?)",
                (
                    fid,
                    time.time(),
                    "resource_manual_resolution",
                    json.dumps({"message": "操作员确认上游残留资源已核对处理"}),
                ),
            )
            pending = db.execute(
                "SELECT 1 FROM remote_resources WHERE file_id=? AND deleted_at IS NULL "
                "AND retained_at IS NULL LIMIT 1",
                (fid,),
            ).fetchone()
            db.execute(
                "UPDATE files SET status=?,cleanup_target='failed',next_check=0,"
                "message='上游资源已人工核对，准备恢复',error='',updated_at=? WHERE id=?",
                ("cleanup_pending" if pending else "queued", time.time(), fid),
            )
            store._event(db, fid)
            store.batch_queue(db, fid)
        app.state.workers.wake.set()
        return {"file": public_file(store.file(fid))}

    def retry_credentials(file_id: str, token: str | None):
        store = app.state.store
        original = store.file_config(file_id)
        if token is None:
            saved = store.settings()
            row = store.file(file_id)
            baseline = store.batch_config(row["batch_id"])
            # Do not send another server's credential to this file's original server.
            if saved["maxkb_base_url"].rstrip("/") == original["maxkb_base_url"].rstrip("/") and (
                not row["maxkb_config"] or saved["maxkb_token"] != baseline["maxkb_token"]
            ):
                token = saved.get("maxkb_token") or original["maxkb_token"]
            else:
                token = original["maxkb_token"]
        config = {**original, "maxkb_token": token}
        settings_from(config)
        return config

    @app.post("/api/ingestion/files/{file_id}/retry-import", status_code=202)
    def retry_import(file_id: str, body: ReimportInput):
        get_file(file_id)
        try:
            config = retry_credentials(file_id, body.maxkb_token)
            app.state.store.queue_reimport(
                file_id, confirm=body.confirm, token=config["maxkb_token"]
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        app.state.workers.wake.set()
        return public_file(app.state.store.file(file_id))

    @app.get("/api/ingestion/files/{file_id}/imports")
    def imports(file_id: str):
        get_file(file_id)
        return app.state.store.import_history(file_id)

    @app.get("/api/ingestion/files/{file_id}/logs")
    def logs(
        file_id: str,
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=200),
        latest: bool = False,
    ):
        get_file(file_id)
        return app.state.store.logs(file_id, after, limit, latest=latest)

    @app.post("/api/ingestion/files/{file_id}/reconcile")
    def reconcile(file_id: str, body: ReconcileInput):
        store = app.state.store
        row = get_file(file_id)
        config = retry_credentials(file_id, body.maxkb_token)
        old = row["status"]
        with store.connect() as db:
            db.execute("SELECT pg_advisory_xact_lock(762190317)")
            row = dict(db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone())
            old = row["status"]
            if old not in {"import_unknown", "cleanup_failed"} or not row["attempt_id"]:
                raise HTTPException(409, "当前状态不能核对，或缺少入库尝试记录")
            db.execute(
                "UPDATE files SET status='reconciling',updated_at=? WHERE id=?",
                (time.time(), file_id),
            )
            db.execute(
                "UPDATE files SET maxkb_config=? WHERE id=?",
                (store.encrypt({"maxkb_token": config["maxkb_token"]}), file_id),
            )
            attempt = db.execute(
                "SELECT payload FROM import_attempts WHERE id=?", (row["attempt_id"],)
            ).fetchone()
            expected = json.loads(attempt[0])
        settings = settings_from(config)
        try:
            with app.state.workers.client_factory() as client:
                maxkb = MaxKBClient(settings, client)
                if row["kind"] == "excel" and not store.remote_documents(file_id):
                    if not body.confirm_absent:
                        store.update(file_id, status=old)
                        return {
                            "action": "confirm_absent",
                            "message": (
                                "Excel 创建响应未记录文档 ID，无法按源文件自动核对。"
                                "请在 MaxKB 检查本次工作簿的所有工作表文档；"
                                "只有确认完全未创建才继续。"
                                "若存在文档，请先人工处理，不要重复上传。"
                            ),
                        }
                    store.finish_attempt(file_id, "confirmed_absent")
                    store.update(
                        file_id,
                        status="import_rejected",
                        unresolved_create=0,
                        message="人工确认本次 Excel 未创建文档，可重新入库",
                        error="",
                    )
                    return {"action": "resolved", "file": public_file(store.file(file_id))}
                found = []
                # Excel names can be generated from sheets. Known IDs are the only safe identity.
                if row["kind"] == "excel":
                    for known in store.remote_documents(file_id):
                        if known["deleted_at"]:
                            continue
                        try:
                            candidate = maxkb.document(known["document_id"])
                        except DocumentMissing:
                            # Only explicit 404 can reach the existing manual absence confirmation.
                            continue
                        if (
                            candidate.get("name") != known["name"]
                            or (candidate.get("meta") or {}).get("source_file_id")
                            != known["source_file_id"]
                        ):
                            raise PipelineError("Excel 文档与创建记录不一致，请在 MaxKB 人工核对")
                        found.append(candidate)
                for document in [] if row["kind"] == "excel" else expected:
                    candidates = maxkb.document_list(document["name"])
                    for candidate in candidates:
                        if (
                            isinstance(candidate, dict)
                            and candidate.get("name") == document["name"]
                            and candidate.get("knowledge_id") == settings.knowledge_id
                            and (candidate.get("meta") or {}).get("source_file_id")
                            == document.get("source_file_id")
                        ):
                            found.append(candidate)
                known = store.remote_documents(file_id)
                if known:
                    if row["unresolved_create"]:
                        if len(found) > len(expected):
                            raise PipelineError("源文件对应重复文档，请先在 MaxKB 手动核对")
                        if not found and not body.confirm_absent:
                            store.update(file_id, status=old)
                            return {
                                "action": "confirm_absent",
                                "message": (
                                    "已清理返回的文档，但创建响应不完整。请确认不存在遗漏文档。"
                                ),
                            }
                        if found:
                            store.record_documents(
                                file_id,
                                row["attempt_id"],
                                created_documents({"data": found}, expected),
                            )
                            known = store.remote_documents(file_id)
                            accounted = {d["name"] for d in known if d["deleted_at"]}
                            accounted.update(d["name"] for d in found)
                            if len(accounted) < len(expected) and not body.confirm_absent:
                                store.update(file_id, status=old)
                                return {
                                    "action": "confirm_absent",
                                    "message": (
                                        "只找到部分提交文档。请确认剩余文档确实未创建且未被重命名，"
                                        "再解除创建结果不完整的限制。"
                                    ),
                                }
                        store.update(file_id, unresolved_create=0)
                        row["unresolved_create"] = 0
                    found_ids = {d.get("id") for d in found}
                    missing = [
                        d
                        for d in known
                        if not d["deleted_at"] and d["document_id"] not in found_ids
                    ]
                    if missing and not body.confirm_absent:
                        store.update(file_id, status=old)
                        return {
                            "action": "confirm_absent",
                            "message": (
                                "部分文档已不在列表中。请核对 MaxKB，"
                                "确认它们未被重命名且确实已删除。"
                            ),
                        }
                    for document in missing:
                        store.record_remote(
                            row["attempt_id"],
                            document["document_id"],
                            state="absent_confirmed",
                            deleted_at=time.time(),
                        )
                    live_ids = {d["document_id"] for d in known if not d["deleted_at"]}
                    if (
                        old == "import_unknown"
                        and live_ids
                        and live_ids == found_ids
                        and len(found) == len(live_ids)
                        and len(found) == len(expected)
                        and not row["unresolved_create"]
                    ):
                        # A timed-out or interrupted monitor is recoverable without
                        # deleting successful documents. Manual reconciliation
                        # explicitly grants a fresh monitoring window.
                        store.update(
                            file_id,
                            status="import_monitoring",
                            import_started_at=time.time(),
                            monitor_errors=0,
                            next_check=0,
                            error="",
                            message="已核对本次文档，继续核验后台入库状态",
                        )
                        app.state.workers.wake.set()
                        return {"action": "resolved", "file": public_file(store.file(file_id))}
                    # Only IDs originally recorded for this attempt are eligible for deletion.
                    store.update(
                        file_id,
                        status="cleanup_failed" if old == "cleanup_failed" else "import_unknown",
                        error="",
                        message="已核对文档，可手动重新入库",
                    )
                    return {"action": "resolved", "file": public_file(store.file(file_id))}
                if found:
                    returned = created_documents({"data": found}, expected)
                    if len(returned) > len(expected):
                        raise PipelineError(
                            "同一源文件出现重复文档，请先在 MaxKB 手动核对；禁止自动删除"
                        )
                    store.record_documents(file_id, row["attempt_id"], returned)
                    status = (
                        "import_monitoring" if len(returned) == len(expected) else "cleanup_pending"
                    )
                    store.update(
                        file_id,
                        status=status,
                        unresolved_create=int(len(returned) != len(expected)),
                        import_started_at=time.time(),
                        next_check=0,
                        error="",
                        message="已按源文件 ID 核对创建结果",
                    )
                    app.state.workers.wake.set()
                    return {"action": "resolved", "file": public_file(store.file(file_id))}
                if not body.confirm_absent:
                    store.update(file_id, status=old)
                    return {
                        "action": "confirm_absent",
                        "message": (
                            "未找到本次源文件创建的文档。"
                            "请在 MaxKB 确认未创建文档后再解除重试限制。"
                        ),
                    }
                store.finish_attempt(file_id, "confirmed_absent")
                store.update(
                    file_id,
                    status="import_rejected",
                    error="",
                    message="人工确认未创建文档，可手动重新入库",
                )
                return {"action": "resolved", "file": public_file(store.file(file_id))}
        except Exception as exc:
            store.update(file_id, status=old, error=settings.redact(str(exc))[:4000])
            raise HTTPException(409, "核对未完成，请检查 MaxKB 权限、连接及文档列表响应") from exc

    @app.get("/api/ingestion/batches/{batch_id}")
    def batch(batch_id: str):
        result = app.state.store.snapshot(batch_id)
        if not result:
            raise HTTPException(404, "批次不存在")
        return result

    @app.get("/api/ingestion/batches/{batch_id}/events")
    async def events(batch_id: str, request: Request):
        store = app.state.store
        epoch = store.history_epoch
        snapshot = await asyncio.to_thread(store.snapshot, batch_id)
        if not snapshot:
            raise HTTPException(404, "批次不存在")
        try:
            cursor = max(0, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            cursor = 0
        if cursor:
            floor = await asyncio.to_thread(store.event_floor, batch_id)
            if not floor or cursor < floor or cursor > snapshot["cursor"]:
                cursor = 0

        async def stream():
            nonlocal cursor
            # A fresh connection always receives a coherent snapshot. Reconnects replay changes.
            if not cursor:
                cursor = snapshot["cursor"]
                yield f"id: {cursor}\nevent: snapshot\ndata: {json.dumps(snapshot)}\n\n"
            last_heartbeat = time.monotonic()
            while not await request.is_disconnected():
                if store.history_epoch != epoch:
                    yield "event: history-cleared\ndata: {}\n\n"
                    return
                rows = await asyncio.to_thread(store.events, batch_id, cursor)
                for row in rows:
                    cursor = row["id"]
                    yield f"id: {cursor}\nevent: file\ndata: {row['payload']}\n\n"
                if time.monotonic() - last_heartbeat > 15:
                    yield ": keepalive\n\n"
                    last_heartbeat = time.monotonic()
                await asyncio.sleep(0.7)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "X-Accel-Buffering": "no",
                "Cache-Control": "no-cache",
            },
        )

    @app.put("/api/ingestion/files/{file_id}/excel-mode")
    def excel_mode(file_id: str, body: ExcelModeInput):
        store = app.state.store
        get_file(file_id)
        with store.connect() as db:
            db.execute("SELECT pg_advisory_xact_lock(762190317)")
            row = db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
            if row["kind"] != "excel" or row["status"] not in {
                "review",
                "failed",
                "queued",
                "waiting_wave",
                "pending_upload",
                "import_failed",
                "import_rejected",
            }:
                raise HTTPException(409, "仅尚未入库或已确认清理的 Excel 可以修改模式")
            if (
                row["unresolved_create"]
                or db.execute(
                    "SELECT 1 FROM remote_documents WHERE attempt_id=? AND deleted_at IS NULL",
                    (row["attempt_id"],),
                ).fetchone()
            ):
                raise HTTPException(409, "旧文档尚未确认清理，不能修改导入模式")
            db.execute(
                "UPDATE files SET import_mode=?,updated_at=? WHERE id=?",
                (body.mode, time.time(), file_id),
            )
            store._event(db, file_id)
        return public_file(store.file(file_id))

    workbook_slots = threading.BoundedSemaphore(2)

    @app.get("/api/ingestion/files/{file_id}/workbook")
    def workbook(
        file_id: str,
        sheet: int = Query(default=0, ge=0),
        offset: int = Query(default=0, ge=0, le=10000),
        limit: int = Query(default=20, ge=1, le=50),
    ):
        row = get_file(file_id)
        if row["kind"] != "excel" or row["status"] in {"pending_upload", "waiting_wave"}:
            raise HTTPException(409, "此 Excel 尚未准备好")
        if not workbook_slots.acquire(blocking=False):
            raise HTTPException(429, "工作簿预览繁忙，请稍后再试")
        try:
            return {
                "file_id": file_id,
                "name": row["name"],
                "mode": row["import_mode"],
                **workbook_preview(
                    app.state.store.directory(file_id, row["kind"]) / row["name"],
                    sheet,
                    offset,
                    limit,
                ),
            }
        except PipelineError:
            raise
        except Exception as exc:
            raise HTTPException(422, "无法读取工作簿，请检查文件是否损坏或加密") from exc
        finally:
            workbook_slots.release()

    @app.get("/api/ingestion/files/{file_id}/preview")
    def preview(
        file_id: str,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=20, ge=1, le=50),
    ):
        row = get_file(file_id)
        if not row["paragraph_count"]:
            raise HTTPException(409, "此文件尚未生成切片")
        index = app.state.preview_index.get(row)
        paragraphs = app.state.store.preview(file_id, offset, limit)
        for item in paragraphs:
            item["html"] = render_markdown(item["content"], file_id, index["images"])
        if not row["previewed_at"]:
            app.state.store.update(file_id, previewed_at=time.time())
        return {
            "file_id": file_id,
            "name": row["name"],
            "total": row["paragraph_count"],
            "offset": offset,
            "local_image_count": len(index["assets"]),
            "unmapped_image_count": index["unmapped"],
            "paragraphs": paragraphs,
        }

    def local_image_response(row, relative: str, kind: str, request: Request):
        parsed = app.state.store.mineru_directory(row["id"]) / "parsed"
        path = (parsed / relative).resolve()
        if not path.is_relative_to(parsed.resolve()) or not path.is_file():
            raise HTTPException(404, "本地图片不存在")
        stat = path.stat()
        if stat.st_size > MAX_IMAGE_BYTES:
            raise HTTPException(413, "图片过大")
        response = FileResponse(
            path,
            media_type=kind,
            stat_result=stat,
            headers={"Cache-Control": "private, no-cache"},
        )
        # Revalidate cached browser images after authentication; unchanged bytes
        # return 304 without opening and transferring the image again.
        etags = request.headers.get("if-none-match", "").split(",")
        if any(
            value.strip().removeprefix("W/") in {"*", response.headers["etag"]} for value in etags
        ):
            return Response(
                status_code=304,
                headers={
                    "ETag": response.headers["etag"],
                    "Cache-Control": "private, no-cache",
                },
            )
        return response

    @app.get("/api/ingestion/files/{file_id}/images/{image_id}")
    def preview_image(file_id: str, image_id: str, request: Request):
        row = get_file(file_id)
        index = app.state.preview_index.get(row)
        relative = index["images"].get(image_id)
        kind = index["types"].get(relative)
        if not relative or not kind:
            raise HTTPException(404, "本地图片未关联，请查看本地原图")
        return local_image_response(row, relative, kind, request)

    @app.get("/api/ingestion/files/{file_id}/local-images")
    def local_images(
        file_id: str,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=12, ge=1, le=50),
    ):
        index = app.state.preview_index.get(get_file(file_id))
        assets = list(index["assets"].items())
        return {
            "total": len(assets),
            "offset": offset,
            "images": [
                {
                    "name": asset["name"],
                    "bytes": asset["bytes"],
                    "url": f"/api/ingestion/files/{file_id}/local-images/{asset_id}",
                }
                for asset_id, asset in assets[offset : offset + limit]
            ],
        }

    @app.get("/api/ingestion/files/{file_id}/local-images/{asset_id}")
    def local_image(file_id: str, asset_id: str, request: Request):
        row = get_file(file_id)
        asset = app.state.preview_index.get(row)["assets"].get(asset_id)
        if not asset:
            raise HTTPException(404, "本地图片不存在")
        return local_image_response(row, asset["path"], asset["type"], request)

    @app.post("/api/ingestion/batches/{batch_id}/import", status_code=202)
    def queue_import(batch_id: str, body: ImportInput):
        if not app.state.store.snapshot(batch_id):
            raise HTTPException(404, "批次不存在")
        ids = list(dict.fromkeys(body.file_ids))
        try:
            app.state.store.queue_import(batch_id, ids)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        app.state.workers.wake.set()
        return {"queued": len(ids)}

    @app.post("/api/ingestion/files/{file_id}/retry", status_code=202)
    def retry(file_id: str, body: RetryInput | None = None):
        get_file(file_id)
        store = app.state.store
        original = store.file_config(file_id)
        saved = store.settings(fallback=original)
        connection = (
            body.model_dump() if body else {k: saved[k] for k in ("mineru_url", "mineru_key")}
        )
        if connection["mineru_key"] is None:
            connection["mineru_key"] = saved["mineru_key"]
        connection["mineru_url"] = connection["mineru_url"].strip().rstrip("/")
        settings_from({**original, **connection})
        try:
            store.retry(file_id, connection)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        app.state.workers.wake.set()
        return {
            "status": "queued",
            "mineru_url": connection["mineru_url"],
            "file": public_file(store.file(file_id)),
        }

    @app.post("/api/ingestion/files/{file_id}/reparse", status_code=202)
    def reparse(file_id: str):
        get_file(file_id)
        store = app.state.store
        config = store.settings(fallback=store.file_config(file_id))
        settings_from(config)
        try:
            store.retry(file_id, {k: config[k] for k in ("mineru_url", "mineru_key")}, reparse=True)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        app.state.workers.wake.set()
        return {"file": public_file(store.file(file_id))}

    @app.post("/api/ingestion/files/{file_id}/resplit", status_code=202)
    def resplit(file_id: str):
        if get_file(file_id)["kind"] == "excel":
            raise HTTPException(409, "Excel 直接导入，不调用文档切片接口")
        try:
            app.state.store.resplit(file_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        app.state.workers.wake.set()
        return {"status": "queued"}

    @app.get("/api/ingestion/files/{file_id}/download")
    def download(file_id: str, artifact: str = "split"):
        row = get_file(file_id)
        names = {"split": "split.json", "markdown": "maxkb.zip", "import": "import.json"}
        if artifact not in names:
            raise HTTPException(404, "产物不存在")
        directory = (
            app.state.store.mineru_directory(file_id)
            if artifact == "markdown"
            else app.state.store.directory(file_id, row["kind"])
        )
        path = directory / names[artifact]
        if not path.is_file():
            raise HTTPException(404, "产物尚未生成")
        return FileResponse(path, filename=row["name"].rsplit(".", 1)[0] + "-" + names[artifact])

    
    return app



