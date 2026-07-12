"""Add persistent administrator audit log.

Revision ID: 0005_audit_logs
Revises: 0004_graph_projection_outbox
"""
import sqlalchemy as sa
from alembic import op


revision = "0005_audit_logs"
down_revision = "0004_graph_projection_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("audit_logs"):
        return
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("actor_user_id", sa.String(length=36), sa.ForeignKey("users.id")),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("target_type", sa.String(length=80)),
        sa.Column("target_id", sa.String(length=36)),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"])
    op.create_index("ix_audit_logs_actor_user_id", "audit_logs", ["actor_user_id"])
    op.create_index("ix_audit_logs_target_id", "audit_logs", ["target_id"])
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("audit_logs"):
        op.drop_table("audit_logs")
