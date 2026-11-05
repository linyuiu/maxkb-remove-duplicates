"""MaxKB admin API. No automatic retries for source mutations."""

from urllib.parse import urlsplit, urlunsplit, quote
import httpx
from .security import decrypt, content_hash


class APIError(Exception):
    def __init__(self, message, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


def normalize_base(value):
    if not value:
        return ""
    parts = urlsplit(value.strip())
    if (
        parts.scheme not in {"http", "https"}
        or not parts.netloc
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise ValueError("API 地址必须是无认证信息和查询参数的 HTTP(S) 地址")
    path = parts.path.rstrip("/") or "/admin/api"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


class MaxKBAPI:
    def __init__(self, source, transport=None):
        if not source.api_base_url or not source.api_key:
            raise APIError("请先在数据源设置 MaxKB API 地址和 Bearer Key")
        self.base = normalize_base(source.api_base_url)
        key = decrypt(source.api_key).strip()
        if key.lower().startswith("bearer "):
            key = key[7:].strip()
        if not key:
            raise APIError("Bearer Key 不能为空")
        self.client = httpx.Client(
            headers={"Authorization": "Bearer " + key},
            timeout=20,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    def close(self):
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def path(self, target):
        return "/workspace/{}/knowledge/{}/document/{}/paragraph".format(
            *[
                quote(str(target[k]), safe="")
                for k in ["workspace_id", "knowledge_id", "document_id"]
            ]
        )

    def request(self, method, path, payload=None):
        mutation = method != "GET"
        try:
            response = self.client.request(method, self.base + path, json=payload)
        except httpx.RequestError:
            raise APIError("源系统请求超时或连接中断", uncertain=mutation) from None
        if response.status_code >= 400:
            raise APIError(
                f"源系统 HTTP 状态 {response.status_code}",
                uncertain=mutation and response.status_code >= 500,
            )
        try:
            body = response.json()
            if not isinstance(body, dict) or body.get("code") != 200:
                raise APIError("源系统返回业务失败", uncertain=mutation)
            return body.get("data")
        except (ValueError, TypeError):
            raise APIError("源系统返回格式异常", uncertain=mutation) from None

    def document(self, target):
        records, seen = [], set()
        page = 1
        total = None
        while page <= 10000:
            data = self.request("GET", self.path(target) + f"/{page}/30")
            if (
                not isinstance(data, dict)
                or not isinstance(data.get("records"), list)
                or not isinstance(data.get("total"), int)
            ):
                raise APIError("文档分页响应缺少 records 或 total")
            if total is None:
                total = data["total"]
            if data["total"] != total:
                raise APIError("读取期间文档段落数量变化，请刷新重试")
            for row in data["records"]:
                if (
                    not isinstance(row, dict)
                    or not row.get("id")
                    or row["id"] in seen
                    or str(row.get("document_id")) != target["document_id"]
                ):
                    raise APIError("文档分页数据重复或所属文档不匹配，请刷新重试")
                if not isinstance(row.get("content"), str) or not isinstance(
                    row.get("title", ""), str
                ):
                    raise APIError("源段落文本格式异常")
                seen.add(row["id"])
                records.append(row)
            if len(records) == total:
                return records
            if not data["records"] or len(records) > total:
                raise APIError("文档分页不完整，请刷新重试")
            page += 1
        raise APIError("文档分页超出读取上限")

    def current(self, target):
        row = next(
            (r for r in self.document(target) if r["id"] == target["paragraph_id"]),
            None,
        )
        if row is None:
            return None
        title = row.get("title", "")
        content = row["content"]
        return {
            **target,
            "title": title,
            "content": content,
            "source_updated_at": row.get("update_time"),
            "content_hash": content_hash(title, content),
        }

    def mutate(self, kind, target, proposed):
        if kind == "delete_document":
            result = self.request(
                "DELETE", self.path(target).removesuffix("/paragraph")
            )
            if result is not True:
                raise APIError("文档删除未返回 data=true，结果需核实", uncertain=True)
            return result
        if kind not in {"update_paragraph", "delete_paragraph"}:
            raise APIError("不支持的操作类型")
        return self.request(
            "PUT" if kind == "update_paragraph" else "DELETE",
            self.path(target) + "/" + quote(target["paragraph_id"], safe=""),
            proposed if kind == "update_paragraph" else None,
        )


def document_hash(records):
    """Fingerprint all paragraph identities, order and text; ignore volatile status."""
    import json
    from .security import digest

    rows = sorted(
        [
            [r["id"], r.get("title", ""), r["content"], r.get("position")]
            for r in records
        ],
        key=lambda row: row[0],
    )
    return digest(json.dumps(rows, ensure_ascii=False, separators=(",", ":")))


def document_snapshot(target, records):
    return {**target, "paragraphs": records, "document_hash": document_hash(records)}
