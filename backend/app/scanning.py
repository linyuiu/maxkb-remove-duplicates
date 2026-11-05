"""Exact all-pairs comparison in MaxKB, within one read-only source snapshot."""

import math
import threading
from datetime import timedelta
from uuid import uuid4
from sqlalchemy import select, update, func, text
from sqlalchemy.dialects.postgresql import insert
from .db import transaction
from .models import Run, Pair, ParagraphSnapshot, DataSource, now
from .source import connect, check_models, SNAPSHOT_SQL, paragraph_data

LEASE_SECONDS = 180


class Stopped(Exception):
    pass


class Shutdown(Stopped):
    pass


class ScanControl:
    def __init__(self):
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.connection = None

    def check(self):
        if self.stopped.is_set():
            raise Shutdown("后端正在关闭")

    def attach(self, connection):
        with self.lock:
            self.check()
            self.connection = connection

    def detach(self):
        with self.lock:
            self.connection = None

    def request_stop(self):
        self.stopped.set()
        with self.lock:
            connection = self.connection
        if connection is not None:
            try:
                connection.cancel_safe(timeout=2)
            except Exception:
                pass  # Fencing/lease expiration still prevents stale commits.


def release_for_restart(run_id, token):
    with transaction() as s:
        s.execute(text("SET LOCAL lock_timeout = '2s'"))
        s.execute(text("SET LOCAL statement_timeout = '5s'"))
        run = s.execute(
            select(Run).where(Run.id == run_id).with_for_update()
        ).scalar_one()
        if run.state == "running" and run.token == token:
            run.lease_until = now() - timedelta(seconds=1)
            run.error = (
                "后端停止，当前快照无法延续；下次启动创建新运行重扫，部分结果保留"
            )


def fenced(session, run_id, token):
    run = session.execute(
        select(Run)
        .where(Run.id == run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one()
    if run.token != token or run.state != "running" or run.lease_until <= now():
        raise Stopped("执行租约已失效")
    if run.cancel_requested:
        raise Stopped("用户取消")
    return run


def claim():
    with transaction() as s:
        run = s.execute(
            select(Run)
            .where(
                (Run.state == "queued")
                | ((Run.state == "running") & (Run.lease_until < now()))
            )
            .order_by(Run.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        ).scalar_one_or_none()
        if not run:
            return None
        if run.state == "running":
            # A lost source transaction cannot be resumed. Keep its partial results
            # and start a separate run with a fresh source snapshot.
            previous = run
            previous.state = "cancelled" if previous.cancel_requested else "failed"
            previous.error = "执行租约失效，源数据库快照已丢失；部分结果保留"
            previous.finished_at = now()
            s.flush()
            if previous.cancel_requested:
                return None
            run = Run(
                task_id=previous.task_id,
                config={**previous.config, "retry_of": previous.id},
            )
            s.add(run)
            s.flush()
        run.started_at = now()
        run.state, run.token, run.lease_until = (
            "running",
            str(uuid4()),
            now() + timedelta(seconds=LEASE_SECONDS),
        )
        return run.id, run.token


def heartbeat(run_id, token, stop):
    while not stop.wait(15):
        try:
            with transaction() as s:
                s.execute(
                    update(Run)
                    .where(
                        Run.id == run_id,
                        Run.token == token,
                        Run.state == "running",
                        Run.lease_until > now(),
                    )
                    .values(lease_until=now() + timedelta(seconds=LEASE_SECONDS))
                )
        except Exception:
            pass  # Every result commit is still fenced.


def take_snapshot(run_id, token, conn, control=None):
    with transaction() as s:
        run = fenced(s, run_id, token)
        if run.snapshot_ready or run.total:
            raise ValueError("已有部分数据的运行不可接入新源快照，请创建重试运行")
        config = run.config.copy()
    check_models(conn, config["knowledge_ids"])
    dimension, seen = None, set()
    group_counts = {}
    with conn.cursor(name="dedup_paragraph_snapshot") as cursor:
        cursor.execute(SNAPSHOT_SQL, (config["knowledge_ids"],))
        while rows := cursor.fetchmany(500):
            if control:
                control.check()
            paragraphs = []
            for row in rows:
                if (
                    row["content"] is None
                    or row["p_document_id"] != row["document_id"]
                    or row["p_knowledge_id"] != row["knowledge_id"]
                    or row["d_knowledge_id"] != row["knowledge_id"]
                ):
                    raise ValueError(
                        f"向量 {row['embedding_id']} 的段落或文档关联缺失/不一致"
                    )
                group = (
                    row["document_id"]
                    if config["exclude_same_document"]
                    else row["paragraph_id"]
                )
                group_counts[group] = group_counts.get(group, 0) + 1
                dims, norm = row["dims"], row["norm"]
                if not dims or norm is None or not math.isfinite(norm) or norm <= 0:
                    raise ValueError(f"向量 {row['embedding_id']} 为空、非有限或零向量")
                if dimension is not None and dims != dimension:
                    raise ValueError("向量维度不一致")
                dimension = dims
                if row["paragraph_id"] not in seen:
                    seen.add(row["paragraph_id"])
                    paragraphs.append(
                        ParagraphSnapshot(
                            run_id=run_id,
                            paragraph_id=row["paragraph_id"],
                            data=paragraph_data(row),
                        )
                    )
            with transaction() as s:
                run = fenced(s, run_id, token)
                s.add_all(paragraphs)
                run.total += len(rows)
    with transaction() as s:
        run = fenced(s, run_id, token)
        from .estimation import comparison_count

        run.config = {
            **run.config,
            "actual_comparisons": comparison_count(run.total, group_counts.values()),
            "actual_dimensions": dimension,
        }
        run.snapshot_ready, run.phase = True, "compare"


# LIMIT is used only to choose anchor batches, never to truncate candidate matches.
# Both sides are filtered by the selected knowledge IDs; they need not be equal.
ANCHORS_SQL = """
SELECT id FROM embedding
WHERE knowledge_id=ANY(%(knowledge_ids)s::uuid[]) AND source_type='1'
  AND (%(start)s::text IS NULL OR id>%(start)s)
ORDER BY id LIMIT %(batch_size)s
"""
COMPARE_SQL = """
WITH scores AS MATERIALIZED (
    SELECT LEAST(a.paragraph_id::text,b.paragraph_id::text) AS left_id,
           GREATEST(a.paragraph_id::text,b.paragraph_id::text) AS right_id,
           CASE WHEN a.paragraph_id::text < b.paragraph_id::text THEN a.id ELSE b.id END AS left_embedding_id,
           CASE WHEN a.paragraph_id::text < b.paragraph_id::text THEN b.id ELSE a.id END AS right_embedding_id,
           1 - (a.embedding <=> b.embedding) AS similarity
    FROM embedding a JOIN embedding b ON b.id<a.id
    WHERE a.knowledge_id=ANY(%(knowledge_ids)s::uuid[]) AND a.source_type='1'
      AND b.knowledge_id=ANY(%(knowledge_ids)s::uuid[]) AND b.source_type='1'
      AND (%(start)s::text IS NULL OR a.id>%(start)s) AND a.id<=%(end)s
      AND a.paragraph_id<>b.paragraph_id
      AND (NOT %(exclude_doc)s OR a.document_id<>b.document_id)
)
SELECT DISTINCT ON (left_id,right_id) * FROM scores
WHERE similarity >= %(threshold)s
ORDER BY left_id,right_id,similarity DESC,left_embedding_id,right_embedding_id
"""


def compare_batch(run_id, token, conn, after=None, control=None):
    if control:
        control.check()
    with transaction() as s:
        run = fenced(s, run_id, token)
        config = run.config.copy()
    params = dict(
        knowledge_ids=config["knowledge_ids"],
        start=after,
        batch_size=config["batch_size"],
        threshold=config["threshold"],
        exclude_doc=config["exclude_same_document"],
    )
    anchors = conn.execute(ANCHORS_SQL, params).fetchall()
    if not anchors:
        with transaction() as s:
            run = fenced(s, run_id, token)
            run.state, run.phase, run.finished_at = "succeeded", "done", now()
        return False, after
    params["end"] = anchors[-1]["id"]
    # Stream large match sets; a partial batch remains only in this incomplete run.
    with conn.cursor(name="dedup_matches") as cursor:
        cursor.execute(COMPARE_SQL, params)
        while rows := cursor.fetchmany(500):
            if control:
                control.check()
            with transaction() as s:
                fenced(s, run_id, token)
                statement = insert(Pair).values(
                    [
                        dict(r, id=str(uuid4()), run_id=run_id, status="pending")
                        for r in rows
                    ]
                )
                s.execute(
                    statement.on_conflict_do_update(
                        index_elements=["run_id", "left_id", "right_id"],
                        set_={
                            key: getattr(statement.excluded, key)
                            for key in [
                                "similarity",
                                "left_embedding_id",
                                "right_embedding_id",
                            ]
                        },
                        where=statement.excluded.similarity > Pair.similarity,
                    )
                )
    if control:
        control.check()
    with transaction() as s:
        run = fenced(s, run_id, token)
        run.cursor += len(anchors)
        run.processed += len(anchors)
        run.pair_count = s.scalar(
            select(func.count()).select_from(Pair).where(Pair.run_id == run_id)
        )
    return True, params["end"]


def execute(run_id, token, control=None):
    stop = threading.Event()
    thread = threading.Thread(target=heartbeat, args=(run_id, token, stop), daemon=True)
    thread.start()
    try:
        with transaction() as s:
            run = fenced(s, run_id, token)
            source = s.get(DataSource, run.config["source_id"])
            if not source or not source.enabled:
                raise ValueError("数据源已停用")
        # Validation, paragraph copies and every comparison share this transaction.
        with connect(source, statement_timeout_ms=120000) as conn:
            if control:
                control.attach(conn)
            take_snapshot(run_id, token, conn, control)
            after = None
            while True:
                more, after = compare_batch(run_id, token, conn, after, control)
                if not more:
                    break
    except Exception as exc:
        if control and control.stopped.is_set():
            stop.set()
            release_for_restart(run_id, token)
        else:
            with transaction() as s:
                run = s.execute(
                    select(Run).where(Run.id == run_id).with_for_update()
                ).scalar_one()
                if (
                    run.token == token
                    and run.state == "running"
                    and run.lease_until > now()
                ):
                    run.state = "cancelled" if run.cancel_requested else "failed"
                    run.error, run.finished_at = str(exc), now()
                    run.pair_count = s.scalar(
                        select(func.count())
                        .select_from(Pair)
                        .where(Pair.run_id == run_id)
                    )
    finally:
        if control:
            control.detach()
        stop.set()
        thread.join(timeout=2)
