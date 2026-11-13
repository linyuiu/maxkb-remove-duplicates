import json
import math
import threading
import time
from contextlib import contextmanager
from uuid import UUID
import psycopg
from psycopg.errors import InsufficientPrivilege, UndefinedColumn, UndefinedTable
from psycopg.rows import dict_row
from psycopg.pq import TransactionStatus
from .security import decrypt, content_hash
from .profiling import measure


def validated_ids(values):
    return sorted({str(UUID(v)) for v in values})


@contextmanager
def connect(source, statement_timeout_ms=60000):
    if getattr(source, "deleted_at", None) is not None:
        raise ValueError("数据源已删除")
    if not source.connection:
        raise ValueError("此连接仅配置 API，扫描需要数据库连接")
    # All source transactions, including discovery and current-text reads, are read-only.
    with measure("source_connection", "建立 MaxKB 源库连接"):
        connection = psycopg.connect(
            decrypt(source.connection),
            row_factory=dict_row,
            connect_timeout=30,
            options=f"-c default_transaction_read_only=on -c statement_timeout={int(statement_timeout_ms)}",
        )
    with connection as conn:
        with measure("source_begin", "开启源库只读快照事务"):
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        yield conn


def list_knowledge(source):
    with connect(source) as conn:
        rows = conn.execute("""
            SELECT k.id::text AS id, k.name, k.workspace_id, k.embedding_model_id::text,
                   m.name AS model_name
            FROM knowledge k LEFT JOIN model m ON m.id=k.embedding_model_id
            WHERE EXISTS (SELECT 1 FROM paragraph p WHERE p.knowledge_id=k.id)
            ORDER BY k.workspace_id, k.name, k.id
        """).fetchall()

        names = workspace_names(conn, {row["workspace_id"] for row in rows})
        return [dict(row, workspace_name=names.get(row["workspace_id"])) for row in rows]


def workspace_names(conn, ids=None):
    if ids is not None and not ids:
        return {}
    query = "SELECT id::text, name FROM workspace"
    params = ()
    if ids is not None:
        query += " WHERE id::text = ANY(%s::text[])"
        params = (list(ids),)
    try:
        # The workspace catalog is optional for community editions and restricted roles.
        with conn.transaction():
            rows = conn.execute(query, params).fetchall()
    except (UndefinedTable, UndefinedColumn, InsufficientPrivilege):
        return {}
    return {row["id"]: row["name"] for row in rows}


def check_models(conn, ids):
    rows = conn.execute(
        "SELECT id::text, embedding_model_id::text FROM knowledge WHERE id=ANY(%s::uuid[])",
        (ids,),
    ).fetchall()
    if {r["id"] for r in rows} != set(ids):
        raise ValueError("选择的知识库不存在")
    models = {r["embedding_model_id"] for r in rows}
    if None in models or len(models) != 1:
        raise ValueError("所选知识库必须使用同一向量模型；不同模型不能直接比较")
    return next(iter(models))


def parse_vector(value, expected_dimension=None):
    result = json.loads(value) if isinstance(value, str) else list(value)
    if not result or any(
        not isinstance(v, (float, int)) or not math.isfinite(v) for v in result
    ):
        raise ValueError("存在空向量或非有限向量")
    if not any(v != 0 for v in result):
        raise ValueError("存在零向量，无法计算余弦相似度")
    if expected_dimension is not None and len(result) != expected_dimension:
        raise ValueError("向量维度不一致")
    return result


def paragraph_data(row):
    return dict(
        workspace_id=row["workspace_id"],
        knowledge_id=str(row["knowledge_id"]),
        document_id=str(row["document_id"]),
        paragraph_id=str(row["paragraph_id"]),
        knowledge_name=row["knowledge_name"],
        document_name=row["document_name"],
        title=row["title"],
        content=row["content"],
        source_updated_at=str(row["update_time"]),
        content_hash=content_hash(row["title"], row["content"]),
    )


def document_scope(row_alias):
    # Keep missing documents in scope so snapshot validation can detect dangling rows.
    return f"""(%(include_inactive_documents)s OR NOT EXISTS (
        SELECT 1 FROM document scope_document
        WHERE scope_document.id = {row_alias}.document_id
          AND scope_document.is_active IS FALSE
    ))"""


# COPY streams one row at a time. No text vectors or repeated paragraph bodies
# cross the wire. LEFT JOIN keeps dangling records visible to validation.
VECTOR_SNAPSHOT_SQL = f"""
    SELECT e.id::text AS embedding_id, vector_send(e.embedding) AS vector,
           e.paragraph_id::text, e.document_id::text, e.knowledge_id::text,
           p.content IS NOT NULL AS has_content, p.document_id::text AS p_document_id,
           p.knowledge_id::text AS p_knowledge_id, d.knowledge_id::text AS d_knowledge_id
    FROM embedding e
    LEFT JOIN paragraph p ON p.id=e.paragraph_id
    LEFT JOIN document d ON d.id=e.document_id
    WHERE e.knowledge_id=ANY(%(knowledge_ids)s::uuid[]) AND e.source_type='1'
      AND {document_scope('e')}
"""
VECTOR_SNAPSHOT_COLUMNS = (
    "embedding_id", "vector", "paragraph_id", "document_id", "knowledge_id",
    "has_content", "p_document_id", "p_knowledge_id", "d_knowledge_id",
)
VECTOR_SNAPSHOT_TYPES = ("text", "bytea", "text", "text", "text", "bool", "text", "text", "text")

# Reject oversized text on the server before COPY allocates/transmits the row.
# It is never truncated. EXISTS returns each paragraph once, even with many chunks.
PARAGRAPH_SNAPSHOT_SQL = f"""
    WITH scoped AS (
        SELECT p.id::text AS paragraph_id, p.document_id::text, p.knowledge_id::text,
               k.workspace_id::text, p.title, p.content, p.update_time::text,
               d.name AS document_name, k.name AS knowledge_name,
               coalesce(octet_length(p.content),0)::bigint
                 + coalesce(octet_length(p.title),0)
                 + coalesce(octet_length(d.name),0)
                 + coalesce(octet_length(k.name),0)
                 + coalesce(octet_length(k.workspace_id::text),0) AS payload_bytes
        FROM paragraph p
        LEFT JOIN document d ON d.id=p.document_id
        LEFT JOIN knowledge k ON k.id=p.knowledge_id
        WHERE EXISTS (
            SELECT 1 FROM embedding e WHERE e.paragraph_id=p.id
              AND e.knowledge_id=ANY(%(knowledge_ids)s::uuid[]) AND e.source_type='1'
              AND {document_scope('e')}
        )
    )
    SELECT paragraph_id, document_id, knowledge_id,
           CASE WHEN payload_bytes<=%(max_paragraph_bytes)s THEN workspace_id END AS workspace_id,
           CASE WHEN payload_bytes<=%(max_paragraph_bytes)s THEN title END AS title,
           CASE WHEN payload_bytes<=%(max_paragraph_bytes)s THEN content END AS content,
           update_time,
           CASE WHEN payload_bytes<=%(max_paragraph_bytes)s THEN document_name END AS document_name,
           CASE WHEN payload_bytes<=%(max_paragraph_bytes)s THEN knowledge_name END AS knowledge_name,
           payload_bytes
    FROM scoped
"""
PARAGRAPH_SNAPSHOT_COLUMNS = (
    "paragraph_id", "document_id", "knowledge_id", "workspace_id", "title",
    "content", "update_time", "document_name", "knowledge_name", "payload_bytes",
)
PARAGRAPH_SNAPSHOT_TYPES = ("text",) * 9 + ("int8",)


class _ReadDeadline:
    """Bound stalled network reads, excluding time spent consuming a COPY row."""

    def __init__(self, conn, seconds):
        self.conn, self.seconds = conn, seconds
        self.condition = threading.Condition()
        self.deadline = None
        self.stopped = self.expired = False
        self.thread = threading.Thread(target=self._watch, daemon=True)

    def _watch(self):
        with self.condition:
            while not self.stopped:
                if self.deadline is None:
                    self.condition.wait()
                elif (remaining := self.deadline - time.monotonic()) > 0:
                    self.condition.wait(remaining)
                else:
                    self.expired = True
                    try:
                        self.conn.cancel_safe(timeout=2)
                    except Exception:
                        pass
                    return

    def resume(self):
        with self.condition:
            self.deadline = time.monotonic() + self.seconds
            self.condition.notify()

    def pause(self):
        with self.condition:
            self.deadline = None
            if self.expired:
                raise TimeoutError(f"读取源快照连续 {self.seconds:g} 秒未返回数据")

    def close(self):
        with self.condition:
            self.stopped = True
            self.condition.notify()
        self.thread.join(timeout=3)


def snapshot_rows(conn, query, params, columns, types, read_timeout=120):
    # A COPY statement lasts for the whole transfer: statement_timeout would
    # otherwise include every slow metadata write while its generator is paused.
    timeout = conn.execute("SHOW statement_timeout").fetchone()["statement_timeout"]
    conn.execute("SELECT set_config('statement_timeout','0',true)")
    deadline = _ReadDeadline(conn, read_timeout)
    deadline.thread.start()
    try:
        with conn.cursor() as cursor:
            deadline.resume()
            with cursor.copy(f"COPY ({query}) TO STDOUT (FORMAT BINARY)", params) as stream:
                deadline.pause()
                stream.set_types(types)
                while True:
                    deadline.resume()
                    values = stream.read_row()
                    deadline.pause()
                    if values is None:
                        break
                    # No fetchmany() list: one bounded COPY record is decoded.
                    yield dict(zip(columns, values, strict=True))
                    del values
    except Exception as exc:
        if deadline.expired:
            raise TimeoutError(f"读取源快照连续 {read_timeout:g} 秒未返回数据") from exc
        raise
    finally:
        deadline.close()
        if conn.info.transaction_status == TransactionStatus.INTRANS:
            conn.execute("SELECT set_config('statement_timeout',%s,true)", (timeout,))


def current_paragraph(source, target, connection=None):
    if connection is None:
        with connect(source) as conn:
            return current_paragraph(source, target, conn)
    else:
        conn = connection
        row = conn.execute(
            """SELECT p.id::text AS paragraph_id, p.document_id::text,
            p.knowledge_id::text, p.title, p.content, p.update_time, k.workspace_id,
            k.name AS knowledge_name, d.name AS document_name
            FROM paragraph p JOIN document d ON d.id=p.document_id
            JOIN knowledge k ON k.id=p.knowledge_id
            WHERE p.id=%s::uuid AND p.document_id=%s::uuid AND p.knowledge_id=%s::uuid
              AND k.workspace_id=%s""",
            (
                target["paragraph_id"],
                target["document_id"],
                target["knowledge_id"],
                target["workspace_id"],
            ),
        ).fetchone()
        return paragraph_data(row) if row else None


def document_exists(source, target):
    with connect(source) as conn:
        return conn.execute(
            """SELECT EXISTS(SELECT 1 FROM document d
            JOIN knowledge k ON k.id=d.knowledge_id
            WHERE d.id=%s::uuid AND d.knowledge_id=%s::uuid AND k.workspace_id=%s) AS present""",
            (target["document_id"], target["knowledge_id"], target["workspace_id"]),
        ).fetchone()["present"]


# MaxKB encodes EMBEDDING (TaskType=1) in the last status character.
READINESS_SQL = f"""
WITH paragraphs AS (
 SELECT p.document_id, p.knowledge_id,
        CASE right(coalesce(p.status,''),1)
          WHEN '0' THEN '等待向量化'
          WHEN '1' THEN '正在向量化'
          WHEN '3' THEN '向量化失败'
          WHEN '4' THEN '向量化取消中'
          WHEN '5' THEN '向量化已取消'
          WHEN '2' THEN CASE WHEN EXISTS (
            SELECT 1 FROM embedding e WHERE e.paragraph_id=p.id
              AND e.document_id=p.document_id AND e.knowledge_id=p.knowledge_id
              AND e.source_type='1' AND e.embedding IS NOT NULL
          ) THEN NULL ELSE '缺少正文向量' END
          ELSE '向量化未完成或状态未知'
        END AS reason
 FROM paragraph p WHERE p.knowledge_id=ANY(%(knowledge_ids)s::uuid[])
   AND {document_scope('p')}
), issues AS (
 SELECT knowledge_id, document_id, reason, count(*) AS count
 FROM paragraphs WHERE reason IS NOT NULL
 GROUP BY knowledge_id, document_id, reason
)
SELECT i.*, k.name AS knowledge_name, d.name AS document_name,
       sum(i.count) OVER () AS total
FROM issues i LEFT JOIN knowledge k ON k.id=i.knowledge_id
LEFT JOIN document d ON d.id=i.document_id
ORDER BY i.knowledge_id, i.document_id, i.reason LIMIT 20
"""


def check_vector_readiness(conn, ids, include_inactive_documents=True):
    rows = conn.execute(
        READINESS_SQL,
        {"knowledge_ids": ids, "include_inactive_documents": include_inactive_documents},
    ).fetchall()
    if rows:
        details = '\n'.join(
            f"{r['knowledge_name'] or r['knowledge_id']} / "
            f"{r['document_name'] or r['document_id']}：{r['reason']} {r['count']} 段"
            for r in rows
        )
        raise ValueError(
            f"未执行扫描：所选知识库有 {int(rows[0]['total'])} 个段落的向量未就绪。"
            f"请在 MaxKB 完成向量化后重新扫描。\n问题明细（最多 20 项）：\n{details}"
        )
