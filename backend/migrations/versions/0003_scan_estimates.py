"""Manual scan estimates and observed execution time."""

from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"


def upgrade():
    op.add_column(
        "scan_runs", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_table(
        "scan_estimates",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "task_id", sa.String(36), sa.ForeignKey("scan_tasks.id"), nullable=False
        ),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "run_id", sa.String(36), sa.ForeignKey("scan_runs.id"), nullable=True
        ),
    )


def downgrade():
    op.drop_table("scan_estimates")
    op.drop_column("scan_runs", "started_at")
