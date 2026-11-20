"""Stream one source snapshot, then compare bounded vector blocks in the app."""

import json
from contextlib import closing
import threading
import time
from datetime import timedelta
from uuid import uuid4
from sqlalchemy import select, update, func, text
from sqlalchemy.dialects.postgresql import insert
from .db import transaction, scan_connection
from .profiling import RunTiming, current as current_timing, measure, timed, timed_rows, publish, count, log
from .models import Run, DataSource, ScanTiming, now
from .result_storage import result_models
from .bulk_writer import BulkWriter, StopSignal, Stopped, Shutdown, STATE_SQL, check_state, writer_scope
from .result_pipeline import ResultPipeline
from .scan_diagnostics import capture_index_stats
from .scan_plan import execution_plan
from .matrix import ENGINE, MatrixSettings, VectorStore, compare
from .source import (
    VECTOR_SNAPSHOT_SQL, VECTOR_SNAPSHOT_COLUMNS, VECTOR_SNAPSHOT_TYPES,
    PARAGRAPH_SNAPSHOT_SQL, PARAGRAPH_SNAPSHOT_COLUMNS, PARAGRAPH_SNAPSHOT_TYPES,
    check_models, check_vector_readiness, connect, paragraph_data, snapshot_rows,
    workspace_names,
)

LEASE_SECONDS = 180
SNAPSHOT_FETCH_ROWS = 1000
SNAPSHOT_WRITE_ROWS = 5000
SNAPSHOT_BUFFER_BYTES = 64 * 1024**2
SNAPSHOT_MAX_PARAGRAPH_BYTES = 8 * 1024**2
SNAPSHOT_CONTROL_INTERVAL = 2.0


class ScanControl:
    def __init__(self):
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.connection = None
        self.connections = set()

    def register(self, connection):
        with self.lock:
            self.check()
            self.connections.add(connection)

    def unregister(self, connection):
        with self.lock:
            self.connections.discard(connection)

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
            connections = self.connections | ({self.connection} if self.connection else set())
        for connection in connections:
            try:
                connection.cancel_safe(timeout=2)
            except Exception:
                pass  # Fencing/lease expiration still prevents stale commits.


def release_for_restart(run_id, token):
    with transaction() as s:
        s.execute(text("SET LOCAL lock_timeout = '2s'"))
        s.execute(text("SET LOCAL statement_timeout = '5s'"))
        run = s.execute(
            select(Run).where(Run.id == run_id).with_for_update(key_share=True)
        ).scalar_one()
        if run.state == "running" and run.token == token:
            run.lease_until = now() - timedelta(seconds=1)
            run.error = (
                "后端停止，临时向量快照无法延续；下次启动创建新运行重扫，部分结果保留"
            )


def check_lease(run, token):
    if run.token != token or run.state != "running" or run.lease_until <= now():
        raise Stopped("执行租约已失效")
    if run.cancel_requested:
        raise Stopped("用户取消")


@timed("lease", "租约校验及行锁")
def fenced(session, run_id, token):
    run = session.execute(
        select(Run)
        .where(Run.id == run_id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    ).scalar_one()
    check_lease(run, token)
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
            previous.error = "执行租约失效，临时向量快照已丢失；部分结果保留"
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


def heartbeat(run_id, token, stop, signal=None):
    if signal is None:
        # Compatibility for callers that only request lease renewal.
        while not stop.wait(15):
            try:
                with transaction() as s:
                    s.execute(update(Run).where(
                        Run.id == run_id, Run.token == token, Run.state == "running",
                        Run.lease_until > now(),
                    ).values(lease_until=now()+timedelta(seconds=LEASE_SECONDS)))
            except Exception:
                pass
        return
    last_success = last_renewal = time.monotonic()
    while not stop.is_set():
        try:
            with scan_connection() as conn:
                conn.execute("SET statement_timeout='5s'")
                conn.execute("SET lock_timeout='2s'")
                while not stop.wait(1):
                    signal.check()
                    state = conn.execute(STATE_SQL, (run_id,)).fetchone()
                    # Completion is acknowledged by the writer; it needs no more renewal.
                    if state and state["token"] == token and state["state"] == "succeeded":
                        return
                    check_state(state, token)
                    current = time.monotonic()
                    if current-last_renewal >= 15:
                        renewed = conn.execute("""UPDATE scan_runs
                            SET lease_until=clock_timestamp()+(%s * interval '1 second')
                            WHERE id=%s AND token=%s AND state='running' AND NOT cancel_requested
                            AND lease_until>clock_timestamp() RETURNING id""",
                            (LEASE_SECONDS,run_id,token)).fetchone()
                        if renewed is None:
                            check_state(conn.execute(STATE_SQL,(run_id,)).fetchone(),token)
                        last_renewal = time.monotonic()
                    last_success = time.monotonic()
        except Stopped as exc:
            signal.stop(exc)
            return
        except Exception:
            # A temporary outage is retried; transactions still fence on server time.
            if time.monotonic()-last_success >= LEASE_SECONDS:
                signal.stop(Stopped("取消检查或续租连接持续不可用"))
                return
            stop.wait(1)


@timed("snapshot", "提取快照")
def take_snapshot(run_id, token, conn, store=None, control=None):
    with transaction() as s:
        run = fenced(s, run_id, token)
        if run.snapshot_ready or run.total:
            raise ValueError("已有部分数据的运行不可接入新源快照，请创建重试运行")
        config = run.config.copy()
    with measure("models", "校验向量模型"):
        embedding_model_id = check_models(conn, config["knowledge_ids"])
    include_inactive_documents = config.get("include_inactive_documents", True)
    with measure("readiness", "校验向量就绪状态"):
        check_vector_readiness(conn, config["knowledge_ids"], include_inactive_documents)
    if store is None:
        raise ValueError("缺少本次扫描的临时向量存储")
    params = {
        "knowledge_ids": config["knowledge_ids"],
        "include_inactive_documents": include_inactive_documents,
        "max_paragraph_bytes": SNAPSHOT_MAX_PARAGRAPH_BYTES,
    }
    with writer_scope(run_id, token, control) as writer:
        last_control_check = time.monotonic()

        def check_control():
            nonlocal last_control_check
            if control:
                control.check()
            current = time.monotonic()
            if current - last_control_check >= SNAPSHOT_CONTROL_INTERVAL:
                with measure("control", "检查取消及租约"):
                    check_state(writer.conn.execute(STATE_SQL,(run_id,)).fetchone(),token)
                    writer.metrics(current_timing())
                last_control_check = time.monotonic()

        with closing(timed_rows(snapshot_rows(
            conn, VECTOR_SNAPSHOT_SQL, params, VECTOR_SNAPSHOT_COLUMNS, VECTOR_SNAPSHOT_TYPES,
        ), "vector_read", "源库读取向量及二进制解码")) as rows:
            for row in rows:
                check_control()
                if (
                    not row["has_content"]
                    or row["p_document_id"] != row["document_id"]
                    or row["p_knowledge_id"] != row["knowledge_id"]
                    or row["d_knowledge_id"] != row["knowledge_id"]
                ):
                    raise ValueError(f"向量 {row['embedding_id']} 的段落或文档关联缺失/不一致")
                with measure("vector_store", "归一化向量及写入本地快照"):
                    store.append(row)
                if store.count % SNAPSHOT_FETCH_ROWS == 0:
                    with measure("index_flush", "提交本地向量索引"):
                        store.flush()
                del row
        with measure("paragraph_index", "建立本地段落计数索引"):
            expected_paragraphs = store.prepare_paragraphs()
        with measure("workspaces", "读取工作空间名称"):
            workspace_ids = conn.execute(
                "SELECT DISTINCT workspace_id::text FROM knowledge WHERE id=ANY(%s::uuid[])",
                (config["knowledge_ids"],),
            ).fetchall()
            workspace_labels = workspace_names(conn, {row["workspace_id"] for row in workspace_ids})
        paragraphs = []
        pending_chunks = pending_bytes = saved_paragraphs = 0

        def flush():
            nonlocal pending_chunks, pending_bytes, saved_paragraphs, last_control_check
            if not paragraphs:
                return
            check_control()
            writer.snapshots(paragraphs)
            saved_paragraphs += len(paragraphs)
            paragraphs.clear()
            pending_chunks = pending_bytes = 0
            last_control_check = time.monotonic()
            writer.metrics(current_timing())

        with closing(timed_rows(snapshot_rows(
            conn, PARAGRAPH_SNAPSHOT_SQL, params, PARAGRAPH_SNAPSHOT_COLUMNS, PARAGRAPH_SNAPSHOT_TYPES,
        ), "paragraph_read", "源库读取正文及解码")) as rows:
            for row in rows:
                check_control()
                if row["payload_bytes"] > SNAPSHOT_MAX_PARAGRAPH_BYTES:
                    raise ValueError(
                        f"段落 {row['paragraph_id']} 的正文及元数据超过 "
                        f"{SNAPSHOT_MAX_PARAGRAPH_BYTES // 1024**2} MiB 读取上限，未截断正文；请拆分后重试"
                    )
                if row["content"] is None:
                    raise ValueError(f"段落 {row['paragraph_id']} 缺少正文")
                with measure("paragraph_prepare", "正文校验摘要及序列化计量"):
                    data = {
                        **paragraph_data(row),
                        "workspace_name": workspace_labels.get(row["workspace_id"]),
                    }
                    # Account for JSON escaping and Python object overhead. At most one
                    # additional bounded COPY row is alive alongside this write buffer.
                    size = len(json.dumps(data, ensure_ascii=True)) + 256
                if paragraphs and pending_bytes + size > SNAPSHOT_BUFFER_BYTES:
                    flush()
                with measure("paragraph_chunks", "查询本地段落对应的 chunk 数"):
                    chunks = store.paragraph_chunks(row["paragraph_id"])
                paragraphs.append((row["paragraph_id"],data,chunks))
                pending_bytes += size
                pending_chunks += chunks
                if pending_chunks >= SNAPSHOT_WRITE_ROWS or pending_bytes >= SNAPSHOT_BUFFER_BYTES:
                    flush()
                del row, data
        flush()
        with transaction() as s:
            run = fenced(s, run_id, token)
            if run.total != store.count or saved_paragraphs != expected_paragraphs:
                raise ValueError("正文快照、段落数与向量读取数量不一致")
            with measure("comparison_count", "统计有效比较组合"):
                comparisons = store.comparisons(config["exclude_same_document"])
            run.config = {
                **run.config,
                "actual_comparisons": comparisons,
                "actual_dimensions": store.dimension,
                "actual_embedding_model_id": embedding_model_id,
                "comparison_engine": ENGINE,
                "processed_comparisons": 0,
            }
            run.comparisons_total = comparisons
            run.snapshot_ready, run.phase = True, "compare"
            publish(run, force=True)


def save_matches(run_id, token, rows):
    with writer_scope(run_id, token) as writer:
        return writer.results(rows)


@timed("compare", "向量比较")
def compare_snapshot(run_id, token, store, control=None):
    settings = MatrixSettings.load()
    with transaction() as s:
        run = fenced(s, run_id, token)
        config = run.config.copy()
        plan = execution_plan(config,store.dimension,s,settings)
        block_size = plan["matrix_block_size"]
        run.config = {**config, "matrix_block_size": block_size,
                      "matrix_threads": settings.threads, "matrix_memory_mb": settings.memory_mb,
                      "execution_plan":plan}
    with writer_scope(run_id, token, control) as writer:
        capture_index_stats(writer,"before")
        try:
            with measure("pipeline", "计算与写入流水线"):
                with ResultPipeline(writer) as pipeline:
                    with measure("matrix_compute", "矩阵比较（含队列等待）"):
                        completed = compare(
                            store, threshold=config["threshold"],
                            exclude_same_document=config["exclude_same_document"],
                            block_size=block_size, threads=settings.threads,
                            consume=pipeline.submit, checkpoint=pipeline.checkpoint,
                        )
                    pipeline.checkpoint(completed,store.count)
        finally:
            # Exclude the final COUNT query, which could otherwise dominate reads.
            capture_index_stats(writer,"after")
        if completed != config["actual_comparisons"]:
            raise ValueError("向量比较数量与快照不一致")
        writer.finish(completed,store.count)


def execute(run_id, token, control=None):
    profile = RunTiming(run_id)
    try:
        with profile.activate():
            _execute(run_id, token, control)
    finally:
        profile.finish()
        # One final telemetry transaction captures the last result COMMIT. It
        # does not change task outcome and is excluded from scan elapsed time.
        try:
            with transaction() as s:
                s.execute(text("SET LOCAL lock_timeout = '2s'"))
                s.execute(text("SET LOCAL statement_timeout = '5s'"))
                run = s.execute(select(Run).where(Run.id == run_id, Run.token == token)
                                .with_for_update(key_share=True)).scalar_one_or_none()
                if run and (run.state in {"succeeded", "failed", "cancelled"}
                            or (run.lease_until and run.lease_until > now())):
                    statement = insert(ScanTiming).values(run_id=run_id,data=profile.snapshot())
                    s.execute(statement.on_conflict_do_update(index_elements=["run_id"],set_={"data":statement.excluded.data}))
        except Exception as exc:
            log.warning("scan_timing run=%s final persistence failed (%s)", run_id, type(exc).__name__)
        profile.report()


def _execute(run_id, token, control=None):
    stop = threading.Event()
    signal = StopSignal(control)
    thread = threading.Thread(target=heartbeat, args=(run_id, token, stop, signal), daemon=True)
    thread.start()
    try:
        with transaction() as s:
            run = fenced(s, run_id, token)
            source = s.get(DataSource, run.config["source_id"])
            if not source or not source.enabled:
                raise ValueError("数据源已停用")
            publish(run, force=True)
        with BulkWriter(run_id,token,signal), VectorStore() as store:
            # One repeatable-read source transaction; release before comparison.
            with connect(source, statement_timeout_ms=120000) as conn:
                signal.register(conn)
                if control:
                    control.attach(conn)
                try:
                    take_snapshot(run_id, token, conn, store, signal)
                finally:
                    signal.unregister(conn)
            if control:
                control.detach()
            compare_snapshot(run_id, token, store, signal)
    except Exception as exc:
        if control and control.stopped.is_set():
            stop.set()
            release_for_restart(run_id, token)
        else:
            with transaction() as s:
                Pair = result_models(s, run_id).Pair
                run = s.execute(
                    select(Run).where(Run.id == run_id).with_for_update(key_share=True)
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
