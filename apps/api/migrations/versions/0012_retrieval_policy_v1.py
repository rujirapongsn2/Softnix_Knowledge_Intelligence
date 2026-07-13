"""Normalize Knowledge Base retrieval policy JSON to version 1."""
import sqlalchemy as sa
from alembic import op


revision = "0012_retrieval_policy_v1"
down_revision = "0011_legal_graph_schema_v2"
branch_labels = None
depends_on = None


DEFAULTS = {
    "version": 1,
    "retrieval_mode": "auto",
    "enable_vector": True,
    "enable_fulltext": True,
    "enable_graph": True,
    "enable_lightrag": True,
    "enable_reranker": True,
    "planner_llm_fallback": True,
    "default_top_k": 12,
    "maximum_top_k": 30,
    "maximum_graph_depth": 3,
    "citation_required": True,
}


def upgrade() -> None:
    bind = op.get_bind()
    table = sa.table("knowledge_bases", sa.column("id", sa.String), sa.column("retrieval_config", sa.JSON))
    rows = bind.execute(sa.select(table.c.id, table.c.retrieval_config)).all()
    for knowledge_base_id, config in rows:
        normalized = {**DEFAULTS, **(config if isinstance(config, dict) else {})}
        bind.execute(table.update().where(table.c.id == knowledge_base_id).values(retrieval_config=normalized))


def downgrade() -> None:
    # Policy JSON is backward-compatible; retain values on downgrade.
    pass
