from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import (
    String,
    Text,
    Integer,
    BigInteger,
    Float,
    Boolean,
    DateTime,
    JSON,
    ForeignKey,
    UniqueConstraint,
    Index,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .db import Base


def now():
    return datetime.now(timezone.utc)


def uid():
    return str(uuid4())


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    username: Mapped[str] = mapped_column(String(100), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)


class LoginSession(Base):
    __tablename__ = "sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DataSource(Base):
    __tablename__ = "data_sources"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(150))
    connection: Mapped[str] = mapped_column(Text)
    api_base_url: Mapped[str] = mapped_column(Text, default="")
    api_key: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Task(Base):
    __tablename__ = "scan_tasks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(150))
    source_id: Mapped[str] = mapped_column(ForeignKey("data_sources.id"))
    knowledge_ids: Mapped[list] = mapped_column(JSON)
    threshold: Mapped[float] = mapped_column(Float, default=0.9)
    exclude_same_document: Mapped[bool] = mapped_column(Boolean, default=False)
    include_inactive_documents: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true")
    )
    batch_size: Mapped[int] = mapped_column(Integer, default=1024)
    cron: Mapped[str | None] = mapped_column(String(100), nullable=True)
    timezone: Mapped[str] = mapped_column(String(80), default="Asia/Shanghai")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    next_fire: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    skipped_fires: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=now
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Run(Base):
    __tablename__ = "scan_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    task_id: Mapped[str] = mapped_column(ForeignKey("scan_tasks.id"))
    config: Mapped[dict] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    phase: Mapped[str] = mapped_column(String(30), default="snapshot")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    snapshot_ready: Mapped[bool] = mapped_column(Boolean, default=False)
    cursor: Mapped[int] = mapped_column(Integer, default=0)
    total: Mapped[int] = mapped_column(Integer, default=0)
    processed: Mapped[int] = mapped_column(Integer, default=0)
    pair_count: Mapped[int] = mapped_column(BigInteger, default=0)
    comparisons_total: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    comparisons_done: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    batch_seq: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    batch_key: Mapped[str] = mapped_column(String(64), default="", server_default="")
    batch_delta: Mapped[dict] = mapped_column(JSON, default=dict, server_default=text("'{}'::json"))
    result_submitted: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    result_improved: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    result_unchanged: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    timing = relationship("ScanTiming", uselist=False, lazy="selectin", viewonly=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    __table_args__ = (
        Index(
            "one_active_run_per_task",
            "task_id",
            unique=True,
            postgresql_where=text("state IN ('queued','running')"),
        ),
    )


class ScanTiming(Base):
    __tablename__ = "scan_timings"
    run_id: Mapped[str] = mapped_column(ForeignKey("scan_runs.id", ondelete="CASCADE"), primary_key=True)
    data: Mapped[dict] = mapped_column(JSON)


class ParagraphSnapshot(Base):
    __tablename__ = "paragraph_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("scan_runs.id"), index=True)
    paragraph_id: Mapped[str] = mapped_column(String(36))
    data: Mapped[dict] = mapped_column(JSON)
    __table_args__ = (UniqueConstraint("run_id", "paragraph_id"),)


class Pair(Base):
    __tablename__ = "similarity_pairs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    run_id: Mapped[str] = mapped_column(ForeignKey("scan_runs.id"), index=True)
    left_id: Mapped[str] = mapped_column(String(36))
    right_id: Mapped[str] = mapped_column(String(36))
    similarity: Mapped[float] = mapped_column(Float)
    left_embedding_id: Mapped[str] = mapped_column(String(128))
    right_embedding_id: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(30), default="pending")
    __table_args__ = (
        UniqueConstraint("run_id", "left_id", "right_id"),
        Index("pairs_run_score", "run_id", "similarity", "id"),
    )


class Decision(Base):
    __tablename__ = "review_decisions"
    __table_args__ = (Index("decisions_pair_id", "pair_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    pair_id: Mapped[str] = mapped_column(ForeignKey("similarity_pairs.id"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(30))
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Operation(Base):
    __tablename__ = "source_operations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    pair_id: Mapped[str | None] = mapped_column(ForeignKey("similarity_pairs.id"), nullable=True)
    __table_args__ = (Index("operations_pair_id", "pair_id"),)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    target: Mapped[dict] = mapped_column(JSON)
    kind: Mapped[str] = mapped_column(String(30))
    before: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    proposed: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    actual: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    state: Mapped[str] = mapped_column(String(30), default="draft")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ScanEstimate(Base):
    __tablename__ = "scan_estimates"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    task_id: Mapped[str] = mapped_column(ForeignKey("scan_tasks.id"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    config: Mapped[dict] = mapped_column(JSON)
    summary: Mapped[dict] = mapped_column(JSON)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    run_id: Mapped[str | None] = mapped_column(
        ForeignKey("scan_runs.id"), nullable=True
    )
