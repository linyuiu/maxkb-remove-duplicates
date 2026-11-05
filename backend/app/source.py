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
            WHERE EXISTS (SELECT 1 FROM embedding e WHERE e.knowledge_id=k.id AND e.source_type='1')
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


# LEFT JOIN is intentional: dangling records fail the snapshot rather than silently disappearing.
SNAPSHOT_SQL = """
    SELECT e.id AS embedding_id, vector_dims(e.embedding) AS dims, vector_norm(e.embedding) AS norm,
           e.paragraph_id::text, e.document_id::text, e.knowledge_id::text,
           p.title, p.content, p.update_time, p.document_id::text AS p_document_id,
           p.knowledge_id::text AS p_knowledge_id, d.knowledge_id::text AS d_knowledge_id,
           d.name AS document_name, k.name AS knowledge_name, k.workspace_id
    FROM embedding e
    LEFT JOIN paragraph p ON p.id=e.paragraph_id
    LEFT JOIN document d ON d.id=e.document_id
    LEFT JOIN knowledge k ON k.id=e.knowledge_id
    WHERE e.knowledge_id=ANY(%s::uuid[]) AND e.source_type='1'
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
