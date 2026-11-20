"""Persist intent before HTTP mutation. Unknown outcomes are reconciled read-only."""

from contextlib import contextmanager
from sqlalchemy.orm import Session
from sqlalchemy import select, text, update, insert, func, cast, String, literal
from fastapi import HTTPException
from . import db
from .models import Operation, DataSource, ScanParagraphRef, ScanResultStorage
from .result_storage import result_models
from .maxkb_api import MaxKBAPI, APIError, document_snapshot
from .source import document_exists


@contextmanager
def atomic(connection):
    with Session(bind=connection, expire_on_commit=False) as session:
        with session.begin():
            yield session


def execute_operation(operation_id, user_id, reconcile=False):
    with db.transaction() as s:
        op = s.get(Operation, operation_id)
        if not op:
            raise HTTPException(404, "操作不存在")
        lock_key = f"{op.target['source_id']}:{op.target['document_id']}"
    # Session advisory lock spans the two local transactions and HTTP call.
    with db.engine.connect() as guard:
        locked = guard.execute(
            text("SELECT pg_try_advisory_lock(hashtextextended(:key,0))"),
            {"key": lock_key},
        ).scalar()
        guard.commit()
        if not locked:
            raise HTTPException(409, "该文档已有操作正在进行")

        def record(*args):
            return finish(*args, connection=guard)

        try:
            with atomic(guard) as s:
                if op.pair_id is not None:
                    result_models(s, op.scan_run_id)
                op = s.execute(
                    select(Operation)
                    .where(Operation.id == operation_id)
                    .with_for_update()
                ).scalar_one()
                if op.state == "succeeded":
                    return op
                if op.pair_id is None:
                    raise HTTPException(409, "扫描记录已删除，该操作仅保留历史记录")
                if reconcile:
                    if op.state not in {"executing", "unknown"}:
                        raise HTTPException(409, "此状态无需核实")
                elif op.state != "draft":
                    raise HTTPException(409, "该操作不能重复执行；结果不明确时请核实")
                if op.user_id != user_id:
                    raise HTTPException(403, "只能执行自己创建的操作")
                source = s.get(DataSource, op.target["source_id"], with_for_update=True)
                if not source or source.deleted_at is not None:
                    raise HTTPException(409, "数据源已删除")
                if not source.enabled:
                    raise HTTPException(409, "数据源已停用")
                target, proposed, kind, before = (
                    op.target.copy(),
                    op.proposed,
                    op.kind,
                    op.before,
                )
                # Validate credentials before transitioning to executing.
                api = MaxKBAPI(source)
                if not reconcile:
                    op.state = "executing"
            # Persisted executing is an intent, not evidence of an HTTP write.
            # Reconciliation concerns an earlier write whose outcome is unknown.
            mutation_started = reconcile
            try:
                with api:
                    if not reconcile:
                        current = (
                            document_snapshot(target, api.document(target))
                            if kind == "delete_document"
                            else api.current(target)
                        )
                        hash_key = (
                            "document_hash"
                            if kind == "delete_document"
                            else "content_hash"
                        )
                        if current is None or current[hash_key] != before[hash_key]:
                            return record(
                                operation_id,
                                user_id,
                                "conflict",
                                current,
                                "源内容已变化或已删除，请刷新后重新操作",
                            )
                        # No source-side conditional update contract was supplied.
                        mutation_started = True
                        api.mutate(kind, target, proposed)
                    if kind == "delete_document":
                        exists = document_exists(source, target)
                        actual = (
                            document_snapshot(target, api.document(target))
                            if exists
                            else None
                        )
                        return record(
                            operation_id,
                            user_id,
                            "unknown" if exists else "succeeded",
                            actual,
                            "源数据库中文档仍存在，需核实；未自动重试"
                            if exists
                            else None,
                        )
                    actual = api.current(target)
                    success = (
                        actual is None
                        if kind == "delete_paragraph"
                        else actual is not None
                        and all(actual[k] == proposed[k] for k in ["title", "content"])
                    )
                    return record(
                        operation_id,
                        user_id,
                        "succeeded" if success else "unknown",
                        actual,
                        None
                        if success
                        else "回读内容与预期不符，需人工核实；未自动重试",
                    )
            except APIError as exc:
                # Once mutation may have been sent, read failures are also unknown.
                return record(
                    operation_id,
                    user_id,
                    "unknown" if mutation_started else "failed",
                    None,
                    (
                        str(exc) if mutation_started else
                        f"未发送修改/删除请求：{exc}；请刷新原文后重新操作"
                    ),
                )
            except Exception:
                return record(
                    operation_id, user_id,
                    "unknown" if mutation_started else "failed", None,
                    (
                        "操作结果待核实，未自动重试" if mutation_started else
                        "读取源内容失败，未发送修改/删除请求；请刷新原文后重新操作"
                    ),
                )
        finally:
            guard.execute(
                text("SELECT pg_advisory_unlock(hashtextextended(:key,0))"),
                {"key": lock_key},
            )
            guard.commit()


def finish(operation_id, user_id, state, actual, error, connection):
    with atomic(connection) as s:
        op = s.get(Operation, operation_id)
        target = op.target
        identity = [
            ScanParagraphRef.source_id == target["source_id"],
            ScanParagraphRef.workspace_id == target["workspace_id"],
            ScanParagraphRef.knowledge_id == target["knowledge_id"],
            ScanParagraphRef.document_id == target["document_id"],
        ]
        if op.kind != "delete_document":
            identity.append(ScanParagraphRef.paragraph_id == target["paragraph_id"])
        affected = []
        if state == "succeeded":
            run_ids = s.scalars(select(ScanParagraphRef.run_id).where(*identity).distinct()).all()
            # Same ordered registry locks as deletion and all pair API access.
            for run_id in sorted(run_ids):
                storage = s.scalar(select(ScanResultStorage).where(
                    ScanResultStorage.run_id == run_id
                ).with_for_update())
                if storage is not None:
                    affected.append((run_id, result_models(s, run_id)))
        op.state, op.actual, op.error = state, actual, error
        s.flush()
        for run_id, models in affected:
            Pair, Decision = models.Pair, models.Decision
            ids = select(ScanParagraphRef.paragraph_id).where(
                ScanParagraphRef.run_id == run_id, *identity
            )
            status = "source_changed" if op.kind == "update_paragraph" else "resolved"
            note = "内容已变化，需重新扫描" if status == "source_changed" else "源系统操作回读确认成功"
            changed = update(Pair.__table__).where(
                Pair.left_id.in_(ids) | Pair.right_id.in_(ids)
            ).values(status=status).returning(Pair.id).cte("changed_pairs")
            # Keep high-cardinality results in PostgreSQL, not Python memory.
            s.execute(insert(Decision.__table__).from_select(
                ["id", "pair_id", "user_id", "status", "note", "created_at"],
                select(cast(func.gen_random_uuid(), String), changed.c.id,
                       literal(user_id), literal(status), literal(f"{note}；关联操作 {op.id}"), func.now()),
            ))
        return op
