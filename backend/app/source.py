import json
import math
from contextlib import contextmanager
from uuid import UUID
import psycopg
from psycopg.rows import dict_row
from .security import decrypt, content_hash


def validated_ids(values):
    return sorted({str(UUID(v)) for v in values})


@contextmanager
def connect(source, statement_timeout_ms=60000):
    if not source.connection:
        raise ValueError("此连接仅配置 API，扫描需要数据库连接")
    # All source transactions, including discovery and current-text reads, are read-only.
    with psycopg.connect(
        decrypt(source.connection),
        row_factory=dict_row,
        connect_timeout=10,
        options=f"-c default_transaction_read_only=on -c statement_timeout={int(statement_timeout_ms)}",
    ) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        yield conn


def list_knowledge(source):
    with connect(source) as conn:
        return conn.execute("""
            SELECT k.id::text AS id, k.name, k.workspace_id, k.embedding_model_id::text,
                   m.name AS model_name
            FROM knowledge k LEFT JOIN model m ON m.id=k.embedding_model_id
            WHERE EXISTS (SELECT 1 FROM paragraph p WHERE p.knowledge_id=k.id)
            ORDER BY k.workspace_id, k.name, k.id
        """).fetchall()


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


# LEFT JOIN is intentional: dangling records fail the snapshot rather than silently disappearing.
SNAPSHOT_SQL = f"""
    SELECT e.id AS embedding_id, vector_dims(e.embedding) AS dims, vector_norm(e.embedding) AS norm,
           e.paragraph_id::text, e.document_id::text, e.knowledge_id::text,
           p.title, p.content, p.update_time, p.document_id::text AS p_document_id,
           p.knowledge_id::text AS p_knowledge_id, d.knowledge_id::text AS d_knowledge_id,
           d.name AS document_name, k.name AS knowledge_name, k.workspace_id
    FROM embedding e
    LEFT JOIN paragraph p ON p.id=e.paragraph_id
    LEFT JOIN document d ON d.id=e.document_id
    LEFT JOIN knowledge k ON k.id=e.knowledge_id
    WHERE e.knowledge_id=ANY(%(knowledge_ids)s::uuid[]) AND e.source_type='1'
      AND {document_scope('e')}
    ORDER BY e.id
"""


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
