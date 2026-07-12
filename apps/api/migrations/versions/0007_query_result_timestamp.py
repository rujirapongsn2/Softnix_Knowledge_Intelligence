"""repair query result timestamp default

Revision ID: 0007_query_timestamp
Revises: 0006_graph_layout
"""
from alembic import op


revision = "0007_query_timestamp"
down_revision = "0006_graph_layout"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Early deployed schemas contained this required column without a default,
    # while the ORM model did not populate it. Keep both DB and ORM safe.
    op.execute("UPDATE query_results SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL")
    op.execute("ALTER TABLE query_results ALTER COLUMN created_at SET DEFAULT CURRENT_TIMESTAMP")


def downgrade() -> None:
    op.execute("ALTER TABLE query_results ALTER COLUMN created_at DROP DEFAULT")
