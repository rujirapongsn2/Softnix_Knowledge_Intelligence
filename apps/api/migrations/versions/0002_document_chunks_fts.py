"""Add persistent document chunks and PostgreSQL full-text index.

Revision ID: 0002_document_chunks_fts
Revises: 0001_initial_schema
"""
import sqlalchemy as sa
from alembic import op


revision = "0002_document_chunks_fts"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("document_chunks"):
        op.create_table(
            "document_chunks",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("document_id", sa.String(length=36), sa.ForeignKey("documents.id"), nullable=False),
            sa.Column("knowledge_base_id", sa.String(length=36), sa.ForeignKey("knowledge_bases.id"), nullable=False),
            sa.Column("chunk_index", sa.Integer(), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("content_sha256", sa.String(length=64), nullable=False),
            sa.Column("char_start", sa.Integer(), nullable=False),
            sa.Column("char_end", sa.Integer(), nullable=False),
            sa.Column("token_count", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("document_id", "chunk_index", name="uq_document_chunk_index"),
        )
        op.create_index("ix_document_chunks_document_id", "document_chunks", ["document_id"])
        op.create_index("ix_document_chunks_knowledge_base_id", "document_chunks", ["knowledge_base_id"])
        op.create_index("ix_document_chunks_content_sha256", "document_chunks", ["content_sha256"])
    if bind.dialect.name == "postgresql":
        op.execute("CREATE INDEX IF NOT EXISTS ix_document_chunks_fts ON document_chunks USING gin (to_tsvector('simple', content))")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_document_chunks_fts")
    if sa.inspect(bind).has_table("document_chunks"):
        op.drop_table("document_chunks")
