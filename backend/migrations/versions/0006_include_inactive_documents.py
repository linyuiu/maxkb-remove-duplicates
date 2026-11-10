"""Allow scan tasks to include or exclude disabled MaxKB documents."""

from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"


def upgrade():
    op.add_column(
        "scan_tasks",
        sa.Column(
            "include_inactive_documents", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
    )


def downgrade():
    op.drop_column("scan_tasks", "include_inactive_documents")
