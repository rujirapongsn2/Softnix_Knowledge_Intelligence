"""add document type for processing policy

Revision ID: 0010_document_type
Revises: 0009_legal_metadata
"""

from alembic import op
import sqlalchemy as sa


revision = "0010_document_type"
down_revision = "0009_legal_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind(); inspector = sa.inspect(bind)
    if "document_type" not in {column["name"] for column in inspector.get_columns("documents")}:
        op.add_column("documents", sa.Column("document_type", sa.String(length=40), nullable=False, server_default="general"))
    if "ix_documents_document_type" not in {index["name"] for index in sa.inspect(bind).get_indexes("documents")}:
        op.create_index("ix_documents_document_type", "documents", ["document_type"])


def downgrade() -> None:
    op.drop_index("ix_documents_document_type", table_name="documents")
    op.drop_column("documents", "document_type")
