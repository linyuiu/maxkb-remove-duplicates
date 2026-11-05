"""Keep independent operation history when scan results are deleted."""
from alembic import op
import sqlalchemy as sa
revision = "0005"
down_revision = "0004"


def upgrade():
    op.alter_column("source_operations", "pair_id", existing_type=sa.String(36), nullable=True)
    op.create_index("operations_pair_id", "source_operations", ["pair_id"])
    op.create_index("decisions_pair_id", "review_decisions", ["pair_id"])


def downgrade():
    op.drop_index("decisions_pair_id", table_name="review_decisions")
    op.drop_index("operations_pair_id", table_name="source_operations")
    op.alter_column("source_operations", "pair_id", existing_type=sa.String(36), nullable=False)
