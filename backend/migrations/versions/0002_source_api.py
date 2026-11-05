"""MaxKB API connection settings; Bearer Key encrypted at rest."""

from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"


def upgrade():
    op.add_column(
        "data_sources",
        sa.Column("api_base_url", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "data_sources",
        sa.Column("api_key", sa.Text(), nullable=False, server_default=""),
    )


def downgrade():
    op.drop_column("data_sources", "api_key")
    op.drop_column("data_sources", "api_base_url")
