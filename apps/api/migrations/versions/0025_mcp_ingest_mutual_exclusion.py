"""Strip Ingest write access from tokens that also carry MCP tools.

MCP tokens and Ingest tokens are now managed on separate menus and
create_token() rejects new tokens that request both. This backfills any
token created before that check existed.
"""
from alembic import op
import sqlalchemy as sa


revision = "0025_mcp_ingest_mutual_exclusion"
down_revision = "0024_ingest_kb_scope"
branch_labels = None
depends_on = None

INGEST_SCOPE = "documents:write"


def upgrade() -> None:
    bind = op.get_bind()
    table = sa.table("token_keys", sa.column("id", sa.String), sa.column("allowed_tools", sa.JSON),
                     sa.column("allowed_scopes", sa.JSON), sa.column("allowed_ingest_knowledge_base_id", sa.String))
    rows = bind.execute(sa.select(table.c.id, table.c.allowed_tools, table.c.allowed_scopes))
    # Write access is the higher-privilege axis (it can add documents to a
    # Knowledge Base on its own) and losing it fails loudly at upload time, so
    # a mixed row keeps its MCP tools and loses Ingest access instead of the
    # other way around, which would fail silently at every MCP call.
    mixed_ids = [row.id for row in rows if (row.allowed_tools or []) and INGEST_SCOPE in (row.allowed_scopes or [])]
    for token_id in mixed_ids:
        bind.execute(table.update().where(table.c.id == token_id).values(
            allowed_scopes=[], allowed_ingest_knowledge_base_id=None))


def downgrade() -> None:
    # Data-only backfill: the pre-migration allowed_scopes value was not
    # recorded anywhere, so a mixed row cannot be reconstructed.
    pass
