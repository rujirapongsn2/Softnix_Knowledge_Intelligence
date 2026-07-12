"""add structured legal metadata to documents

Revision ID: 0009_legal_metadata
Revises: 0008_job_retry
"""
from alembic import op
import sqlalchemy as sa

revision = "0009_legal_metadata"
down_revision = "0008_job_retry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("legal_metadata", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("documents", "legal_metadata")
