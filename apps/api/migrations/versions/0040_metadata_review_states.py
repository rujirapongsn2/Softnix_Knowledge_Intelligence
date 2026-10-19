"""Replace the needs_review metadata status with unverified / needs_attention.

The stored observations decide which of the two a document is, and that rule lives in
app.metadata_extraction, so this migration does not compute it. It marks every
needs_review document needs_attention, which keeps it in front of a person; POST
/knowledge-bases/{id}/metadata-reevaluate then publishes its single-candidate values as
unverified and recomputes the exact status. Downgrade folds both states back into needs_review.

Revision ID: 0040_metadata_review_states
Revises: 0039_embedding_dimension
"""
from alembic import op


revision = "0040_metadata_review_states"
down_revision = "0039_embedding_dimension"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE documents SET metadata_status = 'needs_attention' WHERE metadata_status = 'needs_review'")


def downgrade() -> None:
    op.execute("UPDATE documents SET metadata_status = 'needs_review' WHERE metadata_status IN ('unverified', 'needs_attention')")
