"""HTTP endpoints for server directories and SSH file synchronization."""

from __future__ import annotations

import re
import uuid
from typing import Literal

from fastapi import HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .knowledge import DirectoryPlan, validate_plan
from .sources import SourceError


class SSHInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    name: str = Field(min_length=1, max_length=100)
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=200)
    remote_path: str = Field(min_length=1, max_length=1000)
    auth: Literal["password", "key"] = "password"
    password: str | None = Field(default=None, max_length=8192)
    private_key: str | None = Field(default=None, max_length=65536)
    passphrase: str | None = Field(default=None, max_length=8192)
    bandwidth_mb: float = Field(default=0, ge=0, le=10000)

    @model_validator(mode="after")
    def valid_address(self):
        if not re.fullmatch(r"[A-Za-z0-9_.:-]+", self.host):
            raise ValueError("SSH 地址填写 IP 或主机名，无需协议和端口")
        if not self.remote_path.startswith("/") or any(
            ord(c) < 32 for c in self.remote_path + self.username + self.name
        ):
            raise ValueError("源目录必须是绝对路径，参数不能包含控制字符")
        return self


class JobInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: uuid.UUID


class DirectoryFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=1000)
    bytes: int = Field(gt=0)
    # Nanosecond timestamps exceed JavaScript's safe integer range. Keep them as text.
    mtime: str = Field(pattern=r"^\d{1,20}$")
    excel_mode: Literal["table", "qa"] | None = None


class DirectoryImport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: uuid.UUID
    files: list[DirectoryFile] = Field(min_length=1, max_length=10000)
    config: dict
    knowledge_plan: DirectoryPlan | None = None


def install_routes(app, config_model, validate_config, max_batch_files):
    @app.get("/api/ingestion/source-archives")
    def archives():
        return app.state.sources.archive_jobs()

    @app.post("/api/ingestion/source-archives/{file_id}/retry", status_code=202)
    def retry_archive(file_id: uuid.UUID):
        return app.state.sources.retry_archive(str(file_id))

    @app.get("/api/ingestion/source-jobs/{job_id}/inventory")
    def source_inventory(job_id: uuid.UUID, offset: int = 0, limit: int = 100):
        job = app.state.sources.job(str(job_id))
        if offset < 0 or not 1 <= limit <= 200:
            raise HTTPException(422, "清单分页参数无效")
        with app.state.store.connect() as db:
            rows = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM source_inventory WHERE job_id=? ORDER BY path LIMIT ? OFFSET ?",
                    (str(job_id), limit, offset),
                )
            ]
        return {"job": app.state.sources.public_job(job), "files": rows}

    @app.exception_handler(SourceError)
    async def source_error(request, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse({"detail": str(exc)}, status_code=409)

    def source_key(key):
        if key != "mapped":
            try:
                uuid.UUID(key)
            except ValueError:
                raise HTTPException(422, "来源 ID 无效") from None
        return key

    @app.get("/api/ingestion/ssh-sources")
    def list_sources():
        return app.state.sources.list_sources()

    @app.put("/api/ingestion/ssh-sources/{source_id}")
    def save_source(source_id: uuid.UUID, body: SSHInput):
        return app.state.sources.save(str(source_id), body.model_dump())

    @app.post("/api/ingestion/ssh-sources/{source_id}/test")
    def test_source(source_id: uuid.UUID):
        return app.state.sources.test(str(source_id))

    @app.get("/api/ingestion/ssh-sources/{source_id}/scan")
    def remote_scan_result(source_id: uuid.UUID):
        return app.state.sources.remote_scan_result(str(source_id))

    @app.post("/api/ingestion/ssh-sources/{source_id}/scan", status_code=202)
    def scan_remote(source_id: uuid.UUID, body: JobInput):
        return app.state.sources.submit_remote_scan(str(source_id), str(body.id))

    @app.post("/api/ingestion/ssh-sources/{source_id}/sync", status_code=202)
    def sync_source(source_id: uuid.UUID, body: JobInput):
        return app.state.sources.submit_sync(str(source_id), str(body.id))

    @app.get("/api/ingestion/source-directories")
    def directories():
        return app.state.sources.directories()

    @app.get("/api/ingestion/source-directories/{directory_id}/files")
    def directory_files(directory_id: str, folder: str = Query(default="", max_length=1000)):
        return app.state.sources.scan(source_key(directory_id), folder)

    @app.post("/api/ingestion/source-directories/{directory_id}/import", status_code=202)
    def import_directory(directory_id: str, body: DirectoryImport):
        manager = app.state.sources
        if len(body.files) > manager.max_files:
            raise HTTPException(413, "选择的文件数超过队列上限")
        # Preserve the immutable queue configuration on a replay with the same ID.
        with manager.store.connect() as db:
            existing = db.execute(
                "SELECT config FROM source_jobs WHERE id=?", (str(body.id),)
            ).fetchone()
        fallback = manager.store.decrypt(existing[0]).get("pipeline_config") if existing else None
        try:
            config = validate_config(config_model.model_validate(body.config), fallback)
        except ValidationError:
            raise HTTPException(422, "处理参数格式无效，请检查参数页面") from None
        if config["batch_size"] > max_batch_files:
            raise HTTPException(422, "每批文件数超过服务上限")
        try:
            plan = validate_plan(body.knowledge_plan, [f.path for f in body.files])
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if not plan and config["knowledge_id"] == "pending":
            raise HTTPException(422, "请先确认要创建的知识库及目标目录")
        if plan:
            config["_knowledge_plan"] = plan
        return manager.submit_import(
            source_key(directory_id),
            str(body.id),
            [f.model_dump(exclude_none=True) for f in body.files],
            config,
        )

    @app.get("/api/ingestion/source-jobs")
    def jobs():
        return app.state.sources.jobs()

    @app.get("/api/ingestion/source-jobs/{job_id}")
    def job(job_id: uuid.UUID):
        return app.state.sources.job(str(job_id), public=True)

    @app.get("/api/ingestion/source-jobs/{job_id}/logs")
    def logs(job_id: uuid.UUID):
        return app.state.sources.logs(str(job_id))

    @app.post("/api/ingestion/source-jobs/{job_id}/cancel")
    def cancel(job_id: uuid.UUID):
        return app.state.sources.cancel(str(job_id))

    @app.post("/api/ingestion/source-jobs/{job_id}/retry", status_code=202)
    def retry(job_id: uuid.UUID):
        return app.state.sources.retry(str(job_id))
