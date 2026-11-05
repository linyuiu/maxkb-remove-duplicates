"""Persist intent before HTTP mutation. Unknown outcomes are reconciled read-only."""

from contextlib import contextmanager
from sqlalchemy.orm import Session
from sqlalchemy import select, text
from fastapi import HTTPException
from . import db
from .models import Operation, DataSource, Pair, Decision, Run, ParagraphSnapshot
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
                source = s.get(DataSource, op.target["source_id"])
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
                    "unknown"
                    if reconcile or exc.uncertain or op.state == "executing"
                    else "failed",
                    None,
                    str(exc),
                )
            except Exception:
                return record(
                    operation_id, user_id, "unknown", None, "操作结果待核实，未自动重试"
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
        op.state, op.actual, op.error = state, actual, error
        if state == "succeeded":
            pair = s.get(Pair, op.pair_id)
            affected = [pair]
            if op.kind == "delete_document":
                ids = select(ParagraphSnapshot.paragraph_id).where(
                    ParagraphSnapshot.data["document_id"].as_string()
                    == op.target["document_id"]
                )
                affected = s.scalars(
                    select(Pair)
                    .join(Run, Run.id == Pair.run_id)
                    .where(
                        Run.config["source_id"].as_string() == op.target["source_id"],
                        Pair.left_id.in_(ids) | Pair.right_id.in_(ids),
                    )
                ).all()
            for item in affected:
                item.status = "resolved"

            s.add(
                Decision(
                    pair_id=pair.id,
                    user_id=user_id,
                    status="resolved",
                    note="源系统操作回读确认成功",
                )
            )
        return op
