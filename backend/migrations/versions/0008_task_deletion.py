"""Keep scan history when removing task configurations."""

from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"


def upgrade():
    op.add_column(
        "scan_tasks", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade():
    op.drop_column("scan_tasks", "deleted_at")
