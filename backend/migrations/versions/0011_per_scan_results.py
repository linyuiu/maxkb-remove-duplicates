"""Fresh development schema: independent physical result tables per scan."""
from alembic import op
import sqlalchemy as sa

revision = "0011"
down_revision = "0010"


def upgrade():
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT EXISTS(SELECT 1 FROM scan_runs)")).scalar():
        raise RuntimeError("本版本使用全新扫描存储结构，请在空的开发数据库初始化；不支持旧扫描数据迁移")
    foreign_keys = sa.inspect(connection).get_foreign_keys("source_operations")
    for constraint in foreign_keys:
        if constraint["referred_table"] == "similarity_pairs":
            op.drop_constraint(constraint["name"], "source_operations", type_="foreignkey")
    op.alter_column("source_operations", "pair_id", type_=sa.String(80))
    op.add_column("source_operations", sa.Column("scan_run_id", sa.String(36), nullable=False))
    op.create_index("ix_source_operations_scan_run_id", "source_operations", ["scan_run_id"])
    op.create_index("ix_scan_estimates_run_id", "scan_estimates", ["run_id"])
    op.drop_table("review_decisions")
    op.drop_table("similarity_pairs")
    op.drop_table("paragraph_snapshots")
    op.create_table(
        "scan_result_storage",
        sa.Column("run_id", sa.String(36), sa.ForeignKey("scan_runs.id"), primary_key=True),
        sa.Column("pairs_table", sa.String(63), nullable=False, unique=True),
        sa.Column("paragraphs_table", sa.String(63), nullable=False, unique=True),
        sa.Column("decisions_table", sa.String(63), nullable=False, unique=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
    )
    op.create_table(
        "scan_paragraph_refs",
        sa.Column("run_id", sa.String(36), sa.ForeignKey("scan_result_storage.run_id"), primary_key=True),
        sa.Column("paragraph_id", sa.String(36), primary_key=True),
        *[sa.Column(name, sa.String(36), nullable=False) for name in
          ("source_id", "workspace_id", "knowledge_id", "document_id")],
    )
    op.create_index("scan_paragraph_identity", "scan_paragraph_refs", [
        "source_id", "workspace_id", "knowledge_id", "document_id", "paragraph_id",
    ])


def downgrade():
    raise RuntimeError("独立扫描结果表不支持降级，请重新初始化开发数据库")
