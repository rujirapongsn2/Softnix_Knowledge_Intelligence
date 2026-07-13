"""Add a trusted publication date for retrieval filtering."""
import sqlalchemy as sa
from alembic import op


revision = "0013_document_published_at"
down_revision = "0012_retrieval_policy_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("documents")}
    if "published_at" not in columns:
        op.add_column("documents", sa.Column("published_at", sa.Date(), nullable=True))
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("documents")}
    if "ix_documents_kb_published_at" not in indexes:
        op.create_index("ix_documents_kb_published_at", "documents", ["knowledge_base_id", "published_at"])


def downgrade() -> None:
    op.drop_index("ix_documents_kb_published_at", table_name="documents")
    op.drop_column("documents", "published_at")
