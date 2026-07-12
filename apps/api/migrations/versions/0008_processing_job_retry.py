"""add bounded processing job retry schedule

Revision ID: 0008_job_retry
Revises: 0007_query_timestamp
"""
from alembic import op
import sqlalchemy as sa


revision = "0008_job_retry"
down_revision = "0007_query_timestamp"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("processing_jobs", sa.Column("next_attempt_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False))
    op.create_index("ix_processing_jobs_next_attempt_at", "processing_jobs", ["next_attempt_at"])


def downgrade() -> None:
    op.drop_index("ix_processing_jobs_next_attempt_at", table_name="processing_jobs")
    op.drop_column("processing_jobs", "next_attempt_at")
