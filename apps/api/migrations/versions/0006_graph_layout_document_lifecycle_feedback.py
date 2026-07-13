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
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "deleted_at" not in {column["name"] for column in inspector.get_columns("documents")}:
        op.add_column("documents", sa.Column("deleted_at", sa.DateTime(), nullable=True))
    if "ix_documents_deleted_at" not in {index["name"] for index in sa.inspect(bind).get_indexes("documents")}:
        op.create_index("ix_documents_deleted_at", "documents", ["deleted_at"])
    if not inspector.has_table("graph_node_layouts"):
        op.create_table("graph_node_layouts", sa.Column("id", sa.String(length=36), primary_key=True),
                        sa.Column("knowledge_base_id", sa.String(length=36), sa.ForeignKey("knowledge_bases.id"), nullable=False),
                        sa.Column("entity_id", sa.String(length=36), sa.ForeignKey("entities.id"), nullable=False),
                        sa.Column("position_x", sa.Float(), nullable=False, server_default="0"),
                        sa.Column("position_y", sa.Float(), nullable=False, server_default="0"),
                        sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False),
                        sa.UniqueConstraint("knowledge_base_id", "entity_id", name="uq_graph_node_layout"))
    inspector = sa.inspect(bind)
    for name, columns in (("ix_graph_node_layouts_knowledge_base_id", ["knowledge_base_id"]), ("ix_graph_node_layouts_entity_id", ["entity_id"])):
        if name not in {index["name"] for index in inspector.get_indexes("graph_node_layouts")}:
            op.create_index(name, "graph_node_layouts", columns)
    if not inspector.has_table("query_feedback"):
        op.create_table("query_feedback", sa.Column("id", sa.String(length=36), primary_key=True),
                        sa.Column("result_id", sa.String(length=36), sa.ForeignKey("query_results.id"), nullable=False),
                        sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id"), nullable=True),
                        sa.Column("rating", sa.Integer(), nullable=False), sa.Column("comment", sa.Text(), nullable=True),
                        sa.Column("created_at", sa.DateTime(), nullable=False))
    inspector = sa.inspect(bind)
    for name, columns in (("ix_query_feedback_result_id", ["result_id"]), ("ix_query_feedback_user_id", ["user_id"]), ("ix_query_feedback_created_at", ["created_at"])):
        if name not in {index["name"] for index in inspector.get_indexes("query_feedback")}:
            op.create_index(name, "query_feedback", columns)


def downgrade() -> None:
    op.drop_table("query_feedback")
    op.drop_table("graph_node_layouts")
    op.drop_index("ix_documents_deleted_at", table_name="documents")
    op.drop_column("documents", "deleted_at")
