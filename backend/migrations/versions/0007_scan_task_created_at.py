"""Store the creation time of new scan tasks."""

from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"


def upgrade():
    op.add_column(
        "scan_tasks", sa.Column("created_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade():
    op.drop_column("scan_tasks", "created_at")
