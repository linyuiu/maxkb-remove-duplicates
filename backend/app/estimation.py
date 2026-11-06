"""O(N) read-only counts; never executes the O(N²) similarity join."""

import math
import os
from statistics import median
from sqlalchemy import select
from .models import Run
from .source import connect, check_models, check_vector_readiness

COUNT_SQL = """
WITH p AS (
 SELECT knowledge_id,document_id,paragraph_id,count(*) AS n,
        min(vector_dims(embedding)) AS min_dims,max(vector_dims(embedding)) AS max_dims
 FROM embedding WHERE knowledge_id=ANY(%s::uuid[]) AND source_type='1'
 GROUP BY knowledge_id,document_id,paragraph_id
), d AS (
 SELECT knowledge_id,document_id,sum(n) AS n FROM p GROUP BY knowledge_id,document_id
), ks AS (
 SELECT knowledge_id,sum(n) AS chunks,count(*) AS paragraphs,min(min_dims) AS min_dims,
        max(max_dims) AS max_dims,sum(n::numeric*(n-1)/2) AS paragraph_pairs FROM p GROUP BY knowledge_id
), ds AS (
 SELECT knowledge_id,count(*) AS documents,sum(n*(n-1)/2) AS document_pairs FROM d GROUP BY knowledge_id
)
SELECT k.id::text AS id,k.name,k.workspace_id,coalesce(ks.chunks,0) AS chunks,
       coalesce(ks.paragraphs,0) AS paragraphs,coalesce(ds.documents,0) AS documents,
       ks.min_dims,ks.max_dims,coalesce(ks.paragraph_pairs,0) AS paragraph_pairs,
       coalesce(ds.document_pairs,0) AS document_pairs
FROM knowledge k LEFT JOIN ks ON ks.knowledge_id=k.id LEFT JOIN ds ON ds.knowledge_id=k.id
WHERE k.id=ANY(%s::uuid[]) ORDER BY k.name,k.id
"""


def comparison_count(total, excluded_group_counts):
    return total * (total - 1) // 2 - sum(
        n * (n - 1) // 2 for n in excluded_group_counts
    )


def task_config(task):
    return {
        key: getattr(task, key)
        for key in [
            "name",
            "source_id",
            "knowledge_ids",
            "threshold",
            "exclude_same_document",
            "batch_size",
        ]
    } | {"task_name": task.name}


def scan_config(task):
    values = task_config(task)
    values.pop("name")
    return values


def timing(comparisons, chunks, dimension, historical_rates):
    baseline = float(os.environ.get("SCAN_ESTIMATE_COMPARISONS_PER_SECOND", "50000"))
    if not math.isfinite(baseline) or baseline <= 0:
        raise ValueError("SCAN_ESTIMATE_COMPARISONS_PER_SECOND 必须是正数")
    rates = [r for r in historical_rates if math.isfinite(r) and r > 0]
    rate = median(rates) if rates else baseline * 768 / max(dimension or 768, 1)
    seconds = comparisons / rate + chunks / 2000
    lo, hi = (0.6, 2) if rates else (0.25, 4)
    return {
        "seconds_low": max(3, math.ceil(seconds * lo)),
        "seconds_high": max(15, math.ceil(seconds * hi)),
        "basis": "history" if rates else "initial",
        "history_samples": len(rates),
    }


def estimate(source, config, session):
    with connect(source) as connection:
        check_models(connection, config["knowledge_ids"])
        check_vector_readiness(connection, config["knowledge_ids"])
        rows = connection.execute(
            COUNT_SQL, (config["knowledge_ids"], config["knowledge_ids"])
        ).fetchall()
    dimensions = {
        int(r[k]) for r in rows for k in ["min_dims", "max_dims"] if r[k] is not None
    }
    if len(dimensions) > 1:
        raise ValueError("所选数据向量维度不一致")
    dims = next(iter(dimensions), 0)
    chunks = sum(int(r["chunks"]) for r in rows)
    excluded = sum(
        int(
            r["document_pairs"]
            if config["exclude_same_document"]
            else r["paragraph_pairs"]
        )
        for r in rows
    )
    comparisons = chunks * (chunks - 1) // 2 - excluded
    recent = session.scalars(
        select(Run)
        .where(Run.state == "succeeded", Run.started_at.is_not(None))
        .order_by(Run.finished_at.desc())
        .limit(50)
    ).all()
    rates = []
    for run in recent:
        if any(
            run.config.get(k) != config[k]
            for k in ["source_id", "exclude_same_document", "batch_size", "threshold"]
        ):
            continue
        if run.config.get("actual_dimensions") != dims or not run.finished_at:
            continue
        elapsed = (run.finished_at - run.started_at).total_seconds()
        work = run.config.get("actual_comparisons", 0)
        if elapsed >= 1 and work >= 10000:
            rates.append(work / elapsed)
    return {
        "chunks": chunks,
        "paragraphs": sum(int(r["paragraphs"]) for r in rows),
        "documents": sum(int(r["documents"]) for r in rows),
        "dimensions": dims,
        "comparisons": comparisons,
        "knowledge": [
            {
                "id": r["id"],
                "name": r["name"],
                "workspace_id": r["workspace_id"],
                "chunks": int(r["chunks"]),
                "paragraphs": int(r["paragraphs"]),
                "documents": int(r["documents"]),
            }
            for r in rows
        ],
        **timing(comparisons, chunks, dims, rates),
    }
