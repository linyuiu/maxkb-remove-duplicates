"""Thin scan progress, acknowledged COPY batches, and separate timing storage."""
from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"


def upgrade():
    for name in ("comparisons_total", "comparisons_done", "batch_seq", "result_submitted", "result_improved", "result_unchanged"):
        op.add_column("scan_runs", sa.Column(name, sa.BigInteger(), nullable=False, server_default="0"))
    op.add_column("scan_runs", sa.Column("batch_key", sa.String(64), nullable=False, server_default=""))
    op.add_column("scan_runs", sa.Column("batch_delta", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")))
    op.alter_column("scan_runs", "pair_count", type_=sa.BigInteger(), existing_type=sa.Integer())
    op.create_table("scan_timings",
        sa.Column("run_id", sa.String(36), sa.ForeignKey("scan_runs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("data", sa.JSON(), nullable=False),
    )


def downgrade():
    op.drop_table("scan_timings")
    for name in ("comparisons_total", "comparisons_done", "batch_seq", "batch_key", "batch_delta", "result_submitted", "result_improved", "result_unchanged"):
        op.drop_column("scan_runs", name)
    op.alter_column("scan_runs", "pair_count", type_=sa.Integer(), existing_type=sa.BigInteger())
