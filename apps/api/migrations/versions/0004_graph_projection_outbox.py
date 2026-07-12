"""Add durable Neo4j projection outbox.

Revision ID: 0004_graph_projection_outbox
Revises: 0003_chunk_embeddings_pgvector
"""
import sqlalchemy as sa
from alembic import op


revision = "0004_graph_projection_outbox"
down_revision = "0003_chunk_embeddings_pgvector"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("graph_projection_events"):
        return
    op.create_table(
        "graph_projection_events",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("event_type", sa.String(length=30), nullable=False),
        sa.Column("entity_id", sa.String(length=36), sa.ForeignKey("entities.id")),
        sa.Column("relationship_id", sa.String(length=36), sa.ForeignKey("relationships.id")),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=False),
        sa.Column("last_error", sa.Text()),
        sa.Column("completed_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_graph_projection_events_status", "graph_projection_events", ["status"])
    op.create_index("ix_graph_projection_events_next_attempt_at", "graph_projection_events", ["next_attempt_at"])


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("graph_projection_events"):
        op.drop_table("graph_projection_events")
