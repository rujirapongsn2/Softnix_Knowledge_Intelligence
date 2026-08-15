"""Store the selected, allow-listed Knowledge Base icon key."""
from alembic import op
import sqlalchemy as sa


revision = "0026_knowledge_base_icon"
down_revision = "0025_mcp_ingest_mutual_exclusion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("knowledge_bases")}
    if "icon" not in columns:
        op.add_column("knowledge_bases", sa.Column("icon", sa.String(length=40), nullable=True))
    op.execute("UPDATE knowledge_bases SET icon = 'auto' WHERE icon IS NULL")


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("knowledge_bases")}
    if "icon" in columns:
        op.drop_column("knowledge_bases", "icon")
