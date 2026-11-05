"""从原单文件程序复制的协议与 ZIP 处理逻辑；独立维护，不读取原程序配置。"""

from __future__ import annotations

import json
import math
import os
import posixpath
import re
import shutil
import stat
import time
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlsplit

import httpx
from markdown_it import MarkdownIt

from .lineage import durable_replace, file_digest


class PipelineError(Exception):
    """A protocol, transport, or input error with a user-facing message."""


class ReadUnavailable(PipelineError):
    """A transient HTTP failure; only read operations may retry this automatically."""


class DocumentMissing(PipelineError):
    """A scoped document GET explicitly returned HTTP 404."""


class MinerUResourceMissing(PipelineError):
    """A job, upload or artifact was explicitly expired or lost by the server."""


class ImportOutcomeUnknown(PipelineError):
    """An import request was sent but its completion could not be confirmed."""

    def __init__(self, message: str, response: dict | None = None):
        super().__init__(message)
        self.response = response


class ImportRejected(PipelineError):
    """The import endpoint explicitly rejected the request."""

    def __init__(self, message: str, response: dict | None = None):
        super().__init__(message)
        self.response = response


DEFAULT_PATTERNS = (r"(?<=^)# .*|(?<=\n)# .*",)


def origin(url: str) -> tuple[str, str, int]:
    try:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError
        if parts.username or parts.password:
            raise ValueError
        return (
            parts.scheme,
            parts.hostname.lower(),
            parts.port or (443 if parts.scheme == "https" else 80),
        )
    except ValueError as exc:
        raise PipelineError("URL 必须为有效的 http(s) 地址，且不能包含用户名或密码") from exc


def bearer(token: str) -> dict[str, str]:
    token = token.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if "\r" in token or "\n" in token:
        raise PipelineError("凭据不能包含换行符")
    return {"Authorization": f"Bearer {token}"} if token else {}


@dataclass(frozen=True)
class Settings:
    mineru_url: str
    maxkb_base_url: str
    workspace_id: str
    knowledge_id: str
    maxkb_token: str = field(default="", repr=False)
    mineru_key: str = field(default="", repr=False)
    tier: str = "standard"
    page_range: str = ""
    request_timeout: float = 60
    transfer_timeout: float = 600
    parse_timeout: float = 3600
    poll_interval: float = 2
    limit: int = 2500
    patterns: tuple[str, ...] = DEFAULT_PATTERNS
    with_filter: bool = True
    batch_size: int = 100
    auto_import: bool = False
    knowledge_tags_enabled: bool = True
    document_tags_enabled: bool = True
    inherit_knowledge_tags: bool = True
    import_poll_interval: float = 5
    import_timeout: float = 7200
    excel_mode: str = "table"
    document_tag_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.excel_mode not in {"table", "qa"}:
            raise PipelineError("Excel 模式仅支持 table 或 qa")
        for url in (self.mineru_url, self.maxkb_base_url):
            origin(url)
        parts = urlsplit(self.maxkb_base_url)
        if parts.query or parts.fragment:
            raise PipelineError("maxkb_base_url 应为 API 根地址，不含查询参数或片段")
        for name, value in (
            ("workspace_id", self.workspace_id),
            ("knowledge_id", self.knowledge_id),
        ):
            if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
                raise PipelineError(f"{name} 必须为非空 ID，仅允许字母、数字、下划线和连字符")
        if not bearer(self.maxkb_token):
            raise PipelineError("请填写 MaxKB 访问令牌")
        bearer(self.mineru_key)
        for value in (
            self.request_timeout,
            self.transfer_timeout,
            self.parse_timeout,
            self.poll_interval,
            self.import_poll_interval,
            self.import_timeout,
        ):
            if not math.isfinite(value) or value <= 0:
                raise PipelineError("所有超时与轮询间隔必须为大于 0 的有限数值")
        if self.limit <= 0:
            raise PipelineError("切片长度 limit 必须大于 0")
        parts = urlsplit(self.mineru_url)
        if parts.query or parts.fragment or parts.path.rstrip("/").endswith("/v1"):
            raise PipelineError("--mineru-url 应为 /v1 之前的服务根地址，不含查询参数或片段")
        if self.tier not in {"flash", "basic", "standard", "advanced"}:
            raise PipelineError("不支持的 MinerU tier")

    def _document_url(self, action: str) -> str:
        return (
            f"{self.maxkb_base_url.rstrip('/')}/workspace/{self.workspace_id}"
            f"/knowledge/{self.knowledge_id}/document/{action}"
        )

    @property
    def maxkb_split_url(self) -> str:
        return self._document_url("split")

    @property
    def maxkb_create_url(self) -> str:
        return self._document_url("batch_create")

    def redact(self, message: str) -> str:
        for token in (self.maxkb_token, self.mineru_key):
            if token:
                message = message.replace(token, "[REDACTED]")
                clean = token.removeprefix("Bearer ").removeprefix("bearer ").strip()
                if clean:
                    message = message.replace(clean, "[REDACTED]")
        return message


def required_string(data: dict[str, Any], field: str, stage: str) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value.strip():
        raise PipelineError(f"{stage}：响应缺少有效的 {field}")
    return value


def check_http(response: httpx.Response, stage: str) -> None:
    if not 200 <= response.status_code < 300:
        if response.status_code in {408, 429, 500, 502, 503, 504}:
            raise ReadUnavailable(f"{stage}：HTTP {response.status_code}")
        raise PipelineError(f"{stage}：HTTP {response.status_code}")


def json_object(response: httpx.Response, stage: str) -> dict[str, Any]:
    check_http(response, stage)
    try:
        value = response.json()
    except ValueError as exc:
        raise PipelineError(f"{stage}：响应不是 JSON") from exc
    if not isinstance(value, dict):
        raise PipelineError(f"{stage}：响应必须为 JSON 对象")
    if value.get("error"):
        # Do not print raw response bodies; they can contain credentials or document text.
        error = value["error"]
        code = error.get("code", "unknown") if isinstance(error, dict) else "unknown"
        raise PipelineError(f"{stage}：服务端错误 {code}")
    return value


def job_error_details(job: dict[str, Any]) -> str:
    """Summarize per-file service errors without dumping the entire job response."""
    files = job.get("files")
    if not isinstance(files, list):
        return ""
    details = []
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("error"), dict):
            continue
        error = item["error"]
        code = error.get("code")
        message = error.get("message")
        if not isinstance(message, str) or not message.strip():
            continue
        # Bound and flatten service messages so tracebacks do not overwhelm console output.
        message = " ".join(message.split())[:1500]
        prefix = f"[{code}] " if isinstance(code, str) else ""
        details.append(prefix + message)
    return "；".join(details[:3])


class MinerUClient:
    """Self-hosted MinerU 4 V1 upload, job and artifact resources."""

    def __init__(self, settings: Settings, client: httpx.Client):
        self.settings = settings
        self.client = client
        self.base = settings.mineru_url.rstrip("/")
        self.headers = bearer(settings.mineru_key)

    def _url(self, path: str) -> str:
        return f"{self.base}/v1/{path}"

    def _json(self, method: str, path: str, stage: str, **kwargs: Any) -> dict[str, Any]:
        budget = kwargs.pop("timeout", self.settings.request_timeout)
        deadline = time.monotonic() + budget
        for attempt in range(3 if method == "GET" else 1):
            try:
                response = self.client.request(
                    method,
                    self._url(path),
                    headers=self.headers,
                    timeout=max(0.01, deadline - time.monotonic()),
                    **kwargs,
                )
                if response.status_code == 404 and (
                    path.startswith(("parse/jobs/", "uploads/")) or path == "parse/jobs"
                ):
                    raise MinerUResourceMissing(
                        "MinerU 任务或上传资源已不存在；重试将重新提交原 PDF"
                    )
                return json_object(response, stage)
            except (httpx.TransportError, ReadUnavailable):
                delay = 0.5 * 2**attempt
                if method != "GET" or attempt == 2 or time.monotonic() + delay >= deadline:
                    raise
                time.sleep(delay)
        raise PipelineError("读取上游响应失败")

    def check_capabilities(self) -> None:
        health = self._json("GET", "health", "MinerU 健康检查")
        features = health.get("features", {})
        formats = features.get("output_formats") if isinstance(features, dict) else None
        if not isinstance(formats, list) or "zip" not in formats:
            raise PipelineError("MinerU /v1/health 未声明 ZIP 输出能力，请核对服务版本与档位")

    def upload(self, pdf: Path) -> str:
        upload = self._json(
            "POST",
            "uploads",
            "创建 MinerU 上传",
            json={
                "filename": pdf.name,
                "bytes": pdf.stat().st_size,
                "mime_type": "application/pdf",
                "purpose": "parse",
                "sha256sum": file_digest(pdf),
            },
        )
        upload_id = required_string(upload, "id", "创建 MinerU 上传")
        if upload.get("status") == "pending":
            target = urljoin(self.base + "/", required_string(upload, "upload_url", "上传 PDF"))
            same_origin = origin(target) == origin(self.base)
            method = upload.get("upload_method", "PUT")
            if method != "PUT":
                raise PipelineError("MinerU 返回了不支持的字节上传方法（应为 PUT）")
            headers = upload.get("upload_headers") or {}
            if not isinstance(headers, dict) or not all(
                isinstance(k, str)
                and isinstance(v, str)
                and "\n" not in k + v
                and "\r" not in k + v
                for k, v in headers.items()
            ):
                raise PipelineError("MinerU upload_headers 格式无效")
            headers = httpx.Headers(headers)
            if same_origin:
                headers.update(self.headers)
            with pdf.open("rb") as stream:
                response = self.client.put(
                    target,
                    headers=headers,
                    content=stream,
                    timeout=self.settings.transfer_timeout,
                    follow_redirects=False,
                )
            check_http(response, "上传 PDF 字节")
            upload = self._json(
                "POST", f"uploads/{quote(upload_id, safe='')}/complete", "完成 MinerU 上传"
            )
        if upload.get("status") != "completed" or not isinstance(upload.get("file"), dict):
            raise PipelineError("MinerU 上传未完成或未返回 file")
        return required_string(upload["file"], "id", "完成 MinerU 上传")

    def download_zip(self, job: dict[str, Any], destination: Path) -> None:
        files = job.get("files")
        if not isinstance(files, list) or len(files) != 1 or not isinstance(files[0], dict):
            raise PipelineError("单 PDF 解析任务应返回一个文件结果")
        result = files[0]
        if result.get("status") != "completed":
            raise PipelineError("MinerU 文件结果未完成")
        outputs = result.get("output_files")
        ref = outputs.get("zip") if isinstance(outputs, dict) else None
        if not isinstance(ref, dict):
            raise PipelineError("MinerU 未返回 ZIP 产物引用")
        file_id = required_string(ref, "file_id", "下载 MinerU ZIP")
        target = self._url(f"files/{quote(file_id, safe='')}/content")
        part = destination.with_suffix(destination.suffix + ".part")
        try:
            for _ in range(6):
                headers = self.headers if origin(target) == origin(self.base) else {}
                with self.client.stream(
                    "GET",
                    target,
                    headers=headers,
                    timeout=self.settings.transfer_timeout,
                    follow_redirects=False,
                ) as response:
                    if response.status_code == 404:
                        raise MinerUResourceMissing("MinerU 解析产物已过期；重试将重新提交原 PDF")
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise PipelineError("MinerU 下载重定向缺少 Location")
                        target = urljoin(target, location)
                        origin(target)
                        continue
                    if response.status_code != 200:
                        raise PipelineError(f"下载 MinerU ZIP：HTTP {response.status_code}")
                    with part.open("wb") as stream:
                        downloaded = 0
                        checked = 0
                        for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                            downloaded += len(chunk)
                            if downloaded > MAX_EXPANDED_BYTES:
                                raise PipelineError("MinerU ZIP 下载超过 2 GiB 限制")
                            if downloaded - checked >= 16 * 1024**2:
                                if shutil.disk_usage(part.parent).free < 64 * 1024**2:
                                    raise PipelineError("数据目录空间不足，停止下载解析产物")
                                checked = downloaded
                            stream.write(chunk)
                        stream.flush()
                        os.fsync(stream.fileno())
                    durable_replace(part, destination)
                    return
            raise PipelineError("MinerU 下载重定向次数过多")
        finally:
            part.unlink(missing_ok=True)


def media_type(name: str) -> str:
    return {
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".doc": "application/msword",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xls": "application/vnd.ms-excel",
    }.get(Path(name).suffix.lower(), "application/zip")


class MaxKBClient:
    def __init__(self, settings: Settings, client: httpx.Client):
        self.settings = settings
        self.client = client
        self.headers = bearer(settings.maxkb_token)

    @staticmethod
    def _envelope(response: httpx.Response, stage: str) -> dict[str, Any]:
        body = json_object(response, stage)
        if type(body.get("code")) is not int or body.get("code") != 200 or "data" not in body:
            code = body.get("code") if type(body.get("code")) is int else "invalid"
            raise PipelineError(f"{stage}：MaxKB 业务响应失败（code={code}）")
        return body

    def bounded_request(self, method, url, **kwargs):
        # Bound response memory even when a proxy/upstream sends a huge JSON body.
        with self.client.stream(method, url, **kwargs) as response:
            content = bytearray()
            for chunk in response.iter_bytes(1024**2):
                if len(content) + len(chunk) > 64 * 1024**2:
                    raise PipelineError("MaxKB 响应超过 64 MiB，结果需要核对")
                content.extend(chunk)
            return httpx.Response(
                response.status_code,
                # iter_bytes already decoded gzip/deflate. Do not decode the plain
                # JSON a second time or preserve the compressed Content-Length.
                headers={
                    k: v
                    for k, v in response.headers.items()
                    if k.lower() not in {"content-encoding", "content-length"}
                },
                content=bytes(content),
                request=response.request,
            )

    def knowledge_request(self, method, path, **kwargs):
        url = (
            self.settings.maxkb_base_url.rstrip("/")
            + f"/workspace/{self.settings.workspace_id}/"
            + path
        )
        response = self.bounded_request(
            method, url, headers=self.headers, timeout=self.settings.request_timeout, **kwargs
        )
        return self._envelope(response, "MaxKB 知识库/标签")

    def embedding_models(self):
        data = self.knowledge_request("GET", "model_list", params={"model_type": "EMBEDDING"})[
            "data"
        ]
        if isinstance(data, dict):
            if not all(isinstance(data.get(key, []), list) for key in ("model", "shared_model")):
                raise PipelineError("向量模型分组响应无效")
            data = data.get("model", []) + data.get("shared_model", [])
        if not isinstance(data, list):
            raise PipelineError("向量模型响应不是列表或模型分组")
        models, seen = [], set()
        for item in data:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("id"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", item["id"])
            ):
                raise PipelineError("向量模型 ID 无效")
            if (
                item.get("status", "SUCCESS") != "SUCCESS"
                or item.get("model_type", "EMBEDDING") != "EMBEDDING"
                or item["id"] in seen
            ):
                continue
            seen.add(item["id"])
            models.append({"id": item["id"], "name": str(item.get("name") or item["id"])})
        return models

    def knowledge_folders(self):
        data = self.knowledge_request("GET", "KNOWLEDGE/folder")["data"]
        if not isinstance(data, list):
            raise PipelineError("知识库目录响应不是列表")
        roots = [n for n in data if isinstance(n, dict) and n.get("parent_id") is None]
        if len(roots) != 1:
            raise PipelineError("知识库目录缺少唯一根节点")
        root = roots[0]
        children = root.get("children")
        if not isinstance(children, list):
            raise PipelineError("知识库根目录 children 无效")
        result, seen = [], set()
        for node in [root, *children]:
            if (
                not isinstance(node, dict)
                or not isinstance(node.get("id"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", node["id"])
                or not isinstance(node.get("name"), str)
            ):
                raise PipelineError("知识库目录 ID 或名称无效")
            if node.get("workspace_id", self.settings.workspace_id) != self.settings.workspace_id:
                raise PipelineError("知识库目录工作空间不匹配")
            if node is not root and node.get("parent_id") != root["id"]:
                continue
            if node["id"] not in seen:
                result.append({"id": node["id"], "name": node["name"]})
                seen.add(node["id"])
        return result

    def matching_knowledge(self, name, folder_id):
        data = self.knowledge_request(
            "GET", "knowledge", params={"folder_id": folder_id, "name": name}
        )["data"]
        if not isinstance(data, list):
            raise PipelineError("知识库查询结果不是完整列表，禁止直接创建新知识库")
        matches = {}
        for item in data:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                raise PipelineError("知识库列表结构无效，禁止直接创建新知识库")
            if item["name"] != name:
                continue
            ident = item.get("id")
            if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", ident):
                raise PipelineError("同名知识库 ID 无效")
            if item.get("workspace_id", self.settings.workspace_id) != self.settings.workspace_id:
                continue
            if item.get("folder_id", folder_id) != folder_id:
                continue
            matches[ident] = {
                "id": ident,
                "name": name,
                "create_time": str(item.get("create_time") or ""),
            }
        return sorted(matches.values(), key=lambda item: (item["create_time"], item["id"]))

    def verify_knowledge_target(self, ident, name, folder_id):
        data = self.knowledge_request("GET", f"knowledge/{ident}")["data"]
        if (
            not isinstance(data, dict)
            or data.get("id") != ident
            or data.get("name") != name
            or data.get("workspace_id") != self.settings.workspace_id
            or data.get("folder_id", "default") != folder_id
            or data.get("type", 0) not in (0, "BASE")
        ):
            raise PipelineError("知识库 ID、名称、工作空间、目录或类型不匹配，禁止复用")
        return ident

    def create_knowledge(self, name, embedding_model_id, folder_id="default"):
        from .knowledge import KnowledgeRejected

        url = (
            self.settings.maxkb_base_url.rstrip("/")
            + f"/workspace/{self.settings.workspace_id}/knowledge/base"
        )
        try:
            response = self.bounded_request(
                "POST",
                url,
                headers=self.headers,
                json={
                    "folder_id": folder_id,
                    "name": name,
                    "desc": name,
                    "embedding_model_id": embedding_model_id,
                },
                timeout=self.settings.transfer_timeout,
            )
            if 400 <= response.status_code < 500 and response.status_code != 408:
                raise KnowledgeRejected("MaxKB 拒绝创建知识库，请检查权限和参数")
            body = json_object(response, "MaxKB 创建知识库")
            if response.status_code >= 500:
                raise ImportOutcomeUnknown("创建知识库响应异常，结果待核对")
            if type(body.get("code")) is int and body["code"] != 200:
                raise KnowledgeRejected("MaxKB 拒绝创建知识库：业务 code 非 200")
            data = body.get("data")
            if (
                type(body.get("code")) is not int
                or body["code"] != 200
                or not isinstance(data, dict)
                or not isinstance(data.get("id"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", data["id"])
                or data.get("name") != name
                or data.get("workspace_id") != self.settings.workspace_id
                or data.get("folder_id") != folder_id
            ):
                raise ImportOutcomeUnknown("创建知识库响应缺少有效 ID 或目标不匹配，请人工核对")
            return data["id"]
        except httpx.HTTPError as exc:
            raise ImportOutcomeUnknown("创建知识库请求中断，结果待核对，禁止自动重建") from exc

    def ensure_tags(self, knowledge_id, tags):
        if not tags:
            return []
        path = f"knowledge/{knowledge_id}/tags"

        def index():
            data = self.knowledge_request("GET", path)["data"]
            if not isinstance(data, list):
                raise PipelineError("知识库标签响应无效")
            result = {}
            for group in data:
                if not isinstance(group, dict) or not isinstance(group.get("values"), list):
                    raise PipelineError("知识库标签结构无效")
                for value in group["values"]:
                    if (
                        not isinstance(value, dict)
                        or not isinstance(value.get("id"), str)
                        or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value["id"])
                    ):
                        raise PipelineError("知识库标签 ID 无效")
                    result[(group.get("key"), value.get("value"))] = value["id"]
            return result

        known = index()
        missing = [t for t in tags if (t["key"], t["value"]) not in known]
        if missing:
            self.knowledge_request("POST", path, json=missing)
            known = index()
        try:
            return [known[(t["key"], t["value"])] for t in tags]
        except KeyError as exc:
            raise PipelineError("标签创建后未查询到全部标签 ID，请重试准备知识库") from exc

    @staticmethod
    def tag_identifiers(values):
        if (
            not isinstance(values, (list, tuple))
            or not values
            or any(
                not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value)
                for value in values
            )
        ):
            raise PipelineError("文档或标签 ID 无效")
        return list(dict.fromkeys(values))

    def tag_document(self, document_id, tag_ids):
        document_id = self.tag_identifiers([document_id])[0]
        return self.knowledge_request(
            "POST",
            f"knowledge/{self.settings.knowledge_id}/document/{document_id}/tags",
            json=self.tag_identifiers(tag_ids),
        )

    def batch_tag_documents(self, document_ids, tag_ids):
        documents = self.tag_identifiers(document_ids)
        if len(documents) > 100:
            raise PipelineError("每次最多批量绑定 100 篇文档")
        return self.knowledge_request(
            "POST",
            f"knowledge/{self.settings.knowledge_id}/document/batch_add_tag",
            json={"tag_ids": self.tag_identifiers(tag_ids), "document_ids": documents},
        )

    def split(self, archive: Path) -> dict[str, Any]:
        # Repeated multipart text parts preserve every patterns value.
        fields: list[tuple[str, Any]] = [
            ("patterns", (None, pattern)) for pattern in self.settings.patterns
        ]
        fields += [
            ("limit", (None, str(self.settings.limit))),
            ("with_filter", (None, str(self.settings.with_filter).lower())),
        ]
        with archive.open("rb") as stream:
            fields.append(("file", (archive.name, stream, media_type(archive.name))))
            response = self.bounded_request(
                "POST",
                self.settings.maxkb_split_url,
                headers=self.headers,
                files=fields,
                timeout=self.settings.transfer_timeout,
            )
        return self._envelope(response, "MaxKB 切片")

    def create(self, documents: list[dict[str, Any]]) -> dict[str, Any]:
        # Never retry document creation: a lost response may still mean documents were created.
        try:
            response = self.bounded_request(
                "PUT",
                self.settings.maxkb_create_url,
                headers=self.headers,
                json=documents,
                timeout=self.settings.transfer_timeout,
            )
        except httpx.TransportError as exc:
            raise ImportOutcomeUnknown(
                "MaxKB 导入请求已发送，但结果无法确认；请检查知识库，勿直接重跑以免重复导入"
            ) from exc
        if response.status_code == 408:
            raise ImportOutcomeUnknown("MaxKB 导入请求超时，创建结果待核对；不会自动重试")
        if 400 <= response.status_code < 500:
            raise ImportRejected(f"MaxKB 导入被拒绝：HTTP {response.status_code}")
        try:
            body = json_object(response, "MaxKB 导入")
        except PipelineError as exc:
            raise ImportOutcomeUnknown(
                "MaxKB 导入响应无效，入库结果待确认；请先检查知识库"
            ) from exc
        if type(body.get("code")) is int and body["code"] == 408:
            raise ImportOutcomeUnknown("MaxKB 返回创建超时，结果待核对", body)
        if isinstance(body.get("code"), int) and 400 <= body["code"] < 500:
            raise ImportRejected(f"MaxKB 导入被拒绝：code={body['code']}", body)
        if (
            type(body.get("code")) is not int
            or body.get("code") != 200
            or not isinstance(body.get("data"), (list, dict))
            or not body["data"]
        ):
            raise ImportOutcomeUnknown("MaxKB 导入未返回有效的成功结果；请先检查知识库", body)
        return body

    def import_excel(self, path: Path, mode: str) -> dict:
        """These POST endpoints create documents; never replay an uncertain response."""
        if mode not in {"table", "qa"}:
            raise ImportRejected("请选择 Excel 表格或问答对模式")
        try:
            with path.open("rb") as stream:
                response = self.bounded_request(
                    "POST",
                    self.settings._document_url(mode),
                    headers=self.headers,
                    files={"file": (path.name, stream, media_type(path.name))},
                    timeout=self.settings.transfer_timeout,
                )
        except httpx.TransportError as exc:
            raise ImportOutcomeUnknown(
                "Excel 创建响应丢失，请核对 MaxKB；不会自动重新上传"
            ) from exc
        if response.status_code == 408:
            raise ImportOutcomeUnknown("Excel 创建请求超时，结果待核对；不会自动重新上传")
        if 400 <= response.status_code < 500:
            raise ImportRejected(f"MaxKB Excel 导入被拒绝：HTTP {response.status_code}")
        try:
            body = json_object(response, "MaxKB Excel 导入")
        except PipelineError as exc:
            raise ImportOutcomeUnknown("Excel 创建响应无效，请核对 MaxKB") from exc
        if type(body.get("code")) is int and body["code"] == 408:
            raise ImportOutcomeUnknown("Excel 创建超时，结果待核对", body)
        if type(body.get("code")) is int and 400 <= body["code"] < 500:
            raise ImportRejected(f"MaxKB Excel 导入被拒绝：code={body['code']}", body)
        if type(body.get("code")) is not int or body["code"] != 200 or not body.get("data"):
            raise ImportOutcomeUnknown("Excel 创建未返回有效成功结果，请核对 MaxKB", body)
        return body

    def document(self, document_id: str) -> dict:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", document_id):
            raise PipelineError("MaxKB 文档 ID 无效")
        response = self.bounded_request(
            "GET",
            self.settings._document_url(document_id),
            headers=self.headers,
            timeout=self.settings.request_timeout,
        )
        if response.status_code == 404:
            raise DocumentMissing("MaxKB 文档详情返回 HTTP 404")
        data = self._envelope(response, "查询 MaxKB 文档")["data"]
        if not isinstance(data, dict) or data.get("id") != document_id:
            raise PipelineError("MaxKB 文档详情无效或文档 ID 不匹配")
        if data.get("knowledge_id") != self.settings.knowledge_id:
            raise PipelineError("MaxKB 文档不属于当前知识库，禁止操作")
        return data

    def delete_document(self, document_id: str, *, name: str, source_file_id: str | None) -> dict:
        # Vue Ref query parameters from the browser are not part of the API contract.
        document = self.document(document_id)
        if (
            not source_file_id
            or document.get("name") != name
            or (document.get("meta") or {}).get("source_file_id") != source_file_id
        ):
            raise PipelineError("文档与本次入库源文件不匹配，禁止自动删除")
        response = self.bounded_request(
            "DELETE",
            self.settings._document_url(document_id),
            headers=self.headers,
            timeout=self.settings.request_timeout,
        )
        body = self._envelope(response, "删除失败的 MaxKB 文档")
        if body["data"] is not True:
            raise PipelineError("删除 MaxKB 文档未返回 data=true，禁止重新入库")
        return body

    def document_list(self, name: str) -> list[dict]:
        records = []
        for page in range(1, 11):
            response = self.bounded_request(
                "GET",
                self.settings._document_url(f"{page}/100"),
                headers=self.headers,
                params={"name": name},
                timeout=self.settings.request_timeout,
            )
            data = self._envelope(response, "核对 MaxKB 文档列表")["data"]
            if not isinstance(data, dict) or not isinstance(data.get("records"), list):
                raise PipelineError("MaxKB 文档列表格式无效，不能确认入库结果")
            if not isinstance(data.get("total"), int) or data["total"] < 0:
                raise PipelineError("MaxKB 文档列表缺少有效的 total")
            records.extend(data["records"])
            if len(records) >= data["total"]:
                return records
            if not data["records"]:
                raise PipelineError("MaxKB 文档列表分页不完整")
        raise PipelineError("同名文档超过 1000 条，请在 MaxKB 手动核对文档 ID")


def build_documents(split: dict[str, Any]) -> list[dict[str, Any]]:
    rows = split.get("data")
    if not isinstance(rows, list) or not rows:
        raise PipelineError("MaxKB 切片未返回非空文档列表")
    documents = []
    names = set()
    for row in rows:
        if not isinstance(row, dict):
            raise PipelineError("MaxKB 切片文档格式无效")
        name = required_string(row, "name", "MaxKB 切片文档")
        if name in names:
            raise PipelineError("MaxKB 切片返回重复文档名，无法可靠关联创建结果，停止导入")
        names.add(name)
        source_file_id = required_string(row, "source_file_id", "MaxKB 切片文档")
        paragraphs = row.get("content")
        if not isinstance(paragraphs, list) or not paragraphs:
            raise PipelineError(f"文档 {name} 的切片为空，停止导入")
        for paragraph in paragraphs:
            if not isinstance(paragraph, dict) or not isinstance(paragraph.get("content"), str):
                raise PipelineError(f"文档 {name} 的段落格式无效")
            if not isinstance(paragraph.get("title", ""), str):
                raise PipelineError(f"文档 {name} 的段落标题格式无效")
        if not any(p["content"].strip() for p in paragraphs):
            raise PipelineError(f"文档 {name} 的切片没有正文，停止导入")
        documents.append({"name": name, "paragraphs": paragraphs, "source_file_id": source_file_id})
    return documents


MAX_ENTRIES = 10000

MAX_EXPANDED_BYTES = 2 * 1024**3

MAX_MARKDOWN_BYTES = 32 * 1024**2


class _HTMLImages(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.sources: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "img":
            self.sources.extend(value for name, value in attrs if name == "src" and value)


def image_sources(markdown: str) -> list[str]:
    sources = []
    tokens = list(MarkdownIt("commonmark").parse(markdown))
    while tokens:
        token = tokens.pop()
        if token.children:
            tokens.extend(token.children)
        if token.type == "image" and (source := token.attrGet("src")):
            sources.append(source)
        elif token.type in {"html_inline", "html_block"}:
            parser = _HTMLImages()
            parser.feed(token.content)
            sources.extend(parser.sources)
    return sources


def safe_member(name: str) -> str:
    path = PurePosixPath(name)
    if (
        not name
        or not path.parts
        or "\\" in name
        or "\x00" in name
        or path.is_absolute()
        or ".." in path.parts
        or ":" in path.parts[0]
    ):
        raise PipelineError("MinerU ZIP 包含不安全的文件路径")
    return path.as_posix()


def prepare_archive(
    source: Path, destination: Path, preview_dir: Path, *, markdown_name: str | None = None
) -> dict[str, list[str]]:
    """Keep Markdown and only its local images, preserving their relative paths.

    Dropping sidecar JSON/PDF files prevents MaxKB from splitting them into extra documents.
    Nothing is extracted until all archive names and referenced assets have been checked.
    """
    part = destination.with_suffix(".zip.part")
    try:
        with zipfile.ZipFile(source) as incoming:
            entries = incoming.infolist()
            if len(entries) > MAX_ENTRIES or sum(i.file_size for i in entries) > MAX_EXPANDED_BYTES:
                raise PipelineError("MinerU ZIP 超出本程序的解压限制（10000 项 / 2 GiB）")
            members: dict[str, zipfile.ZipInfo] = {}
            for info in entries:
                name = safe_member(info.filename)
                if info.is_dir():
                    continue
                if stat.S_ISLNK(info.external_attr >> 16) or info.flag_bits & 1:
                    raise PipelineError("MinerU ZIP 不应包含符号链接或加密文件")
                if name in members:
                    raise PipelineError("MinerU ZIP 包含重复文件名")
                members[name] = info
            markdown_names = sorted(name for name in members if name.lower().endswith(".md"))
            if not markdown_names:
                raise PipelineError("MinerU ZIP 中没有 Markdown 文件，未上传到 MaxKB")
            renamed: dict[str, str] = {}
            if markdown_name and len(markdown_names) == 1:
                # Official MinerU ZIP uses markdown.md for every PDF. Preserve the PDF name
                # in MaxKB while keeping the directory and all image-relative paths intact.
                if PurePosixPath(markdown_name).name != markdown_name:
                    raise PipelineError("Markdown 文件名不能包含目录")
                original_name = markdown_names[0]
                renamed[original_name] = safe_member(
                    str(PurePosixPath(original_name).with_name(markdown_name))
                )
            selected = set(markdown_names)
            images = set()
            for name in markdown_names:
                if members[name].file_size > MAX_MARKDOWN_BYTES:
                    raise PipelineError("单个 Markdown 超出 32 MiB 限制")
                text = incoming.read(members[name]).decode("utf-8-sig")
                if not text.strip():
                    raise PipelineError(f"MinerU Markdown {name} 内容为空")
                for source_url in image_sources(text):
                    parts = urlsplit(source_url)
                    if parts.scheme or parts.netloc:
                        continue  # Preserve remote/data image URLs without making network requests.
                    image = posixpath.normpath(
                        posixpath.join(posixpath.dirname(name), unquote(parts.path))
                    )
                    image = safe_member(image)
                    if image not in members:
                        raise PipelineError(f"Markdown {name} 引用的本地图片缺失：{image}")
                    selected.add(image)
                    images.add(image)
            with zipfile.ZipFile(part, "w", compression=zipfile.ZIP_DEFLATED) as outgoing:
                for name in sorted(selected):
                    output_name = renamed.get(name, name)
                    if output_name != name and output_name in selected:
                        raise PipelineError("重命名 Markdown 与 ZIP 内其他文件冲突")
                    target = preview_dir / output_name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if not target.resolve().is_relative_to(preview_dir.resolve()):
                        raise PipelineError("解析产物写入路径越界")
                    entry = zipfile.ZipInfo(output_name)
                    # Raster images are already compressed; deflating them again
                    # burns CPU with almost no network/disk size benefit.
                    entry.compress_type = (
                        zipfile.ZIP_STORED
                        if name in images
                        and PurePosixPath(name).suffix.lower()
                        in {".png", ".jpg", ".jpeg", ".gif", ".webp"}
                        else zipfile.ZIP_DEFLATED
                    )
                    with (
                        incoming.open(members[name]) as src,
                        outgoing.open(entry, "w") as dest,
                        target.open("wb") as preview,
                    ):
                        while chunk := src.read(1024 * 1024):
                            dest.write(chunk)
                            preview.write(chunk)
            with part.open("r+b") as stream:
                os.fsync(stream.fileno())
            durable_replace(part, destination)
        return {
            "markdown": [renamed.get(name, name) for name in markdown_names],
            "images": sorted(images),
        }
    except (zipfile.BadZipFile, UnicodeError, RuntimeError) as exc:
        raise PipelineError("MinerU 返回的 ZIP 损坏、加密或 Markdown 编码无效") from exc
    finally:
        part.unlink(missing_ok=True)


def write_json(path: Path, data: Any) -> None:
    part = path.with_suffix(path.suffix + ".part")
    try:
        with part.open("w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        part.replace(path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        part.unlink(missing_ok=True)
