"""Knowledge ingestion tables in a separate PostgreSQL schema."""
from pathlib import Path
from alembic import op
revision='0004'
down_revision='0003'
def upgrade():
    op.execute('CREATE SCHEMA IF NOT EXISTS ingestion')
    op.execute('SET LOCAL search_path TO ingestion, public')
    sql=(Path(__file__).resolve().parents[2]/'app/ingestion/schema.sql').read_text()
    for statement in sql.split(';'):
        if statement.strip():op.execute(statement)
    op.execute('SET LOCAL search_path TO public')
def downgrade():
    # Keep uploaded document history and receipt metadata; no automatic data deletion.
    raise RuntimeError("入库记录不支持自动降级删除；请先备份并制定回滚方案")
