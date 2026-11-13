"""Remove source credentials while retaining scan and operation history."""

from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"


def upgrade():
    op.add_column(
        "data_sources", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade():
    op.drop_column("data_sources", "deleted_at")
