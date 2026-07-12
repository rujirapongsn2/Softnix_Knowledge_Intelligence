"""Add pgvector embeddings to document chunks.

Revision ID: 0003_chunk_embeddings_pgvector
Revises: 0002_document_chunks_fts
"""
import sqlalchemy as sa
from alembic import op


revision = "0003_chunk_embeddings_pgvector"
down_revision = "0002_document_chunks_fts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    columns = {column["name"] for column in sa.inspect(bind).get_columns("document_chunks")}
    if "embedding" not in columns:
        op.execute("ALTER TABLE document_chunks ADD COLUMN embedding vector(1536)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_document_chunks_embedding_hnsw ON document_chunks USING hnsw (embedding vector_cosine_ops)")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_document_chunks_embedding_hnsw")
        op.execute("ALTER TABLE document_chunks DROP COLUMN IF EXISTS embedding")
