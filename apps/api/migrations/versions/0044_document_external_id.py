"""Let an Ingest API sender name a document with its own key.

external_id is optional. A Knowledge Base holds at most one live document per key, so a sender that resends after
a lost response gets 409 EXTERNAL_ID_EXISTS with the document's id instead of a second copy.

Revision ID: 0044_document_external_id
Revises: 0043_buddhist_year_dates
"""
import sqlalchemy as sa
from alembic import op


revision = "0044_document_external_id"
down_revision = "0043_buddhist_year_dates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("external_id", sa.String(200), nullable=True))
    op.create_index("uq_document_external_id_live", "documents", ["knowledge_base_id", "external_id"], unique=True,
                    postgresql_where=sa.text("deleted_at IS NULL AND external_id IS NOT NULL"))


def downgrade() -> None:
    op.drop_index("uq_document_external_id_live", table_name="documents")
    op.drop_column("documents", "external_id")
