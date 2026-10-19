"""Claim tokens, lost-worker and busy-deferral counters for processing jobs."""
import sqlalchemy as sa
from alembic import op


revision = "0038_processing_job_claims"
down_revision = "0037_document_purged_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("processing_jobs") as batch_op:
        batch_op.add_column(sa.Column("claim_token", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("lost_count", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("deferral_count", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table("processing_jobs") as batch_op:
        batch_op.drop_column("deferral_count")
        batch_op.drop_column("lost_count")
        batch_op.drop_column("claim_token")
