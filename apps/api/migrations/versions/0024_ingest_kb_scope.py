"""Split the ingest Knowledge Base scope out of the shared MCP allowed_knowledge_base_ids list."""
from alembic import op
import sqlalchemy as sa


revision = "0024_ingest_kb_scope"
down_revision = "0023_token_ingest_scope"
branch_labels = None
depends_on = None

INGEST_SCOPE = "documents:write"


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("token_keys")}
    if "allowed_ingest_knowledge_base_id" not in columns:
        op.add_column("token_keys", sa.Column("allowed_ingest_knowledge_base_id", sa.String(length=36), nullable=True))
    table = sa.table("token_keys", sa.column("id", sa.String), sa.column("allowed_scopes", sa.JSON),
                     sa.column("allowed_knowledge_base_ids", sa.JSON), sa.column("allowed_ingest_knowledge_base_id", sa.String))
    # Before this revision, a write-scoped token's ingest authority was every
    # Knowledge Base in the shared MCP list. Pin it to the first one so an
    # already-issued token keeps writing to the same place it always did.
    rows = bind.execute(sa.select(table.c.id, table.c.allowed_scopes, table.c.allowed_knowledge_base_ids))
    updates = [
        {"id": row.id, "allowed_ingest_knowledge_base_id": row.allowed_knowledge_base_ids[0]}
        for row in rows
        if INGEST_SCOPE in (row.allowed_scopes or []) and (row.allowed_knowledge_base_ids or [])
    ]
    for update in updates:
        bind.execute(table.update().where(table.c.id == update["id"]).values(
            allowed_ingest_knowledge_base_id=update["allowed_ingest_knowledge_base_id"]))


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("token_keys")}
    if "allowed_ingest_knowledge_base_id" in columns:
        op.drop_column("token_keys", "allowed_ingest_knowledge_base_id")
