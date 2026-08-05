"""Add a write scope to token keys and retire the empty-allowed_tools wildcard."""
from alembic import op
import sqlalchemy as sa


revision = "0023_token_ingest_scope"
down_revision = "0022_legal_version_role"
branch_labels = None
depends_on = None

# Frozen snapshot of the read-only MCP tools as of this revision. authorize()
# used to treat an empty allowed_tools list as "every tool", so these names are
# written onto legacy rows to preserve their exact authority before the wildcard
# is removed. Do not update this list when new tools ship.
LEGACY_WILDCARD_TOOLS = [
    "search_knowledge", "document_inventory_summary", "find_entities", "analyze_relationships",
    "analyze_impact", "get_sources", "resolve_legal_context", "get_legal_instrument", "get_provision_history",
]


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("token_keys")}
    if "allowed_scopes" not in columns:
        # An empty list is the intended value for every existing token: no write
        # access. Backfilling this column would be the privilege escalation.
        op.add_column("token_keys", sa.Column("allowed_scopes", sa.JSON(), nullable=False, server_default="[]"))
    table = sa.table("token_keys", sa.column("id", sa.String), sa.column("allowed_tools", sa.JSON))
    wildcard_ids = [row.id for row in bind.execute(sa.select(table.c.id, table.c.allowed_tools)) if not row.allowed_tools]
    if wildcard_ids:
        bind.execute(table.update().where(table.c.id.in_(wildcard_ids)).values(allowed_tools=LEGACY_WILDCARD_TOOLS))


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("token_keys")}
    if "allowed_scopes" in columns:
        op.drop_column("token_keys", "allowed_scopes")
