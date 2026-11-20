"""Per-scan result tables. Public registry rows fence all table access and deletion."""

from functools import lru_cache
from hashlib import sha256
import re
from types import SimpleNamespace
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import (
    Table, Column, String, Integer, Float, JSON, Text, DateTime,
    ForeignKey, UniqueConstraint, Index, select, event,
)
from sqlalchemy.orm import registry

from .models import Run, ScanResultStorage, ScanParagraphRef, now


def table_names(run_id):
    # Fixed-length, server-generated identifiers, independent of request/table input.
    suffix = sha256(run_id.encode()).hexdigest()[:32]
    return {kind: f"scan_{kind}_{suffix}" for kind in ("pairs", "paragraphs", "decisions")}


@lru_cache(maxsize=128)
def models_for_run(run_id):
    names = table_names(run_id)
    mapper = registry()
    metadata = mapper.metadata
    paragraphs = Table(
        names["paragraphs"], metadata,
        Column("id", Integer, primary_key=True),
        Column("run_id", String(36), nullable=False, default=run_id),
        Column("paragraph_id", String(36), nullable=False, unique=True),
        Column("data", JSON, nullable=False),
    )
    pairs = Table(
        names["pairs"], metadata,
        Column("id", String(80), primary_key=True, default=lambda: f"{run_id}:{uuid4().hex}"),
        Column("run_id", String(36), nullable=False, default=run_id),
        Column("left_id", String(36), nullable=False),
        Column("right_id", String(36), nullable=False),
        Column("similarity", Float, nullable=False),
        Column("left_embedding_id", String(128), nullable=False),
        Column("right_embedding_id", String(128), nullable=False),
        Column("status", String(30), nullable=False, default="pending"),
        UniqueConstraint("run_id", "left_id", "right_id"),
    )
    for column in ("left_id", "right_id", "status"):
        Index(f"{names['pairs']}_{column}", pairs.c[column])
    Index(f"{names['pairs']}_score", pairs.c.similarity.desc(), pairs.c.id)
    decisions = Table(
        names["decisions"], metadata,
        Column("id", String(36), primary_key=True, default=lambda: str(uuid4())),
        Column("pair_id", String(80), ForeignKey(pairs.c.id), nullable=False, index=True),
        Column("user_id", String(36), nullable=False),
        Column("status", String(30), nullable=False),
        Column("note", Text, nullable=False, default=""),
        Column("created_at", DateTime(timezone=True), nullable=False, default=now),
    )
    classes = {}
    for name, table in (("Pair", pairs), ("ParagraphSnapshot", paragraphs), ("Decision", decisions)):
        model = type(name, (), {})
        mapper.map_imperatively(model, table)
        classes[name] = model
    @event.listens_for(classes["ParagraphSnapshot"], "after_insert")
    def register_paragraph(mapper, connection, paragraph):
        from sqlalchemy.dialects.postgresql import insert
        config = connection.execute(select(Run.config).where(Run.id == run_id)).scalar_one()
        data = paragraph.data
        connection.execute(insert(ScanParagraphRef).values(
            run_id=run_id, paragraph_id=paragraph.paragraph_id,
            source_id=config["source_id"], workspace_id=data["workspace_id"],
            knowledge_id=data["knowledge_id"], document_id=data["document_id"],
        ).on_conflict_do_nothing())
    return SimpleNamespace(**classes, metadata=metadata, names=names)


def create_storage(connection, run_id):
    models = models_for_run(run_id)
    models.metadata.create_all(connection, checkfirst=False)
    connection.execute(ScanResultStorage.__table__.insert().values(
        run_id=run_id, pairs_table=models.names["pairs"],
        paragraphs_table=models.names["paragraphs"], decisions_table=models.names["decisions"],
        schema_version=1,
    ))


def result_models(session, run_id, *, exclusive=False):
    storage = session.scalar(
        select(ScanResultStorage).where(ScanResultStorage.run_id == run_id)
        .with_for_update(read=not exclusive)
    )
    if storage is None:
        raise HTTPException(404, "扫描结果不存在或已删除")
    if storage.schema_version != 1:
        raise RuntimeError("不支持的扫描结果结构版本")
    expected = table_names(run_id)
    if any(getattr(storage, f"{key}_table") != value for key, value in expected.items()):
        raise RuntimeError("扫描结果表登记不一致")
    return models_for_run(run_id)


def get_pair(session, pair_id):
    run_id, separator, suffix = pair_id.partition(":")
    if not separator or not re.fullmatch(r"[A-Za-z0-9_-]{1,36}", run_id) or not suffix:
        raise HTTPException(404, "相似对不存在")
    models = result_models(session, run_id)
    pair = session.get(models.Pair, pair_id)
    if pair is None:
        raise HTTPException(404, "相似对不存在")
    return pair, models
