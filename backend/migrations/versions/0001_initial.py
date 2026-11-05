"""Initial schema. Future schema changes must use new revisions."""

from alembic import op
from pathlib import Path

revision = "0001"
down_revision = None


def upgrade():
    sql = Path(__file__).with_name("initial_schema.sql").read_text()
    for statement in sql.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade():
    for table in [
        "source_operations",
        "review_decisions",
        "similarity_pairs",
        "paragraph_snapshots",
        "scan_runs",
        "scan_tasks",
        "data_sources",
        "sessions",
        "users",
    ]:
        op.drop_table(table)
