"""graph layout, document lifecycle, and retrieval feedback

Revision ID: 0006_graph_layout
Revises: 0005_audit_logs
"""

from alembic import op
import sqlalchemy as sa


revision = "0006_graph_layout"
down_revision = "0005_audit_logs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("deleted_at", sa.DateTime(), nullable=True))
    op.create_index("ix_documents_deleted_at", "documents", ["deleted_at"])
    op.create_table(
        "graph_node_layouts",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("knowledge_base_id", sa.String(length=36), sa.ForeignKey("knowledge_bases.id"), nullable=False),
        sa.Column("entity_id", sa.String(length=36), sa.ForeignKey("entities.id"), nullable=False),
        sa.Column("position_x", sa.Float(), nullable=False, server_default="0"),
        sa.Column("position_y", sa.Float(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("knowledge_base_id", "entity_id", name="uq_graph_node_layout"),
    )
    op.create_index("ix_graph_node_layouts_knowledge_base_id", "graph_node_layouts", ["knowledge_base_id"])
    op.create_index("ix_graph_node_layouts_entity_id", "graph_node_layouts", ["entity_id"])
    op.create_table(
        "query_feedback",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("result_id", sa.String(length=36), sa.ForeignKey("query_results.id"), nullable=False),
        sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_query_feedback_result_id", "query_feedback", ["result_id"])
    op.create_index("ix_query_feedback_user_id", "query_feedback", ["user_id"])
    op.create_index("ix_query_feedback_created_at", "query_feedback", ["created_at"])


def downgrade() -> None:
    op.drop_table("query_feedback")
    op.drop_table("graph_node_layouts")
    op.drop_index("ix_documents_deleted_at", table_name="documents")
    op.drop_column("documents", "deleted_at")
