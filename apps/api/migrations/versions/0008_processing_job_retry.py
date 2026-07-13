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
    bind = op.get_bind(); inspector = sa.inspect(bind)
    if "next_attempt_at" not in {column["name"] for column in inspector.get_columns("processing_jobs")}:
        op.add_column("processing_jobs", sa.Column("next_attempt_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False))
    if "ix_processing_jobs_next_attempt_at" not in {index["name"] for index in sa.inspect(bind).get_indexes("processing_jobs")}:
        op.create_index("ix_processing_jobs_next_attempt_at", "processing_jobs", ["next_attempt_at"])


def downgrade() -> None:
    op.drop_index("ix_processing_jobs_next_attempt_at", table_name="processing_jobs")
    op.drop_column("processing_jobs", "next_attempt_at")
