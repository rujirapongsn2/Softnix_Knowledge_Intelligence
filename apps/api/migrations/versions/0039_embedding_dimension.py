"""Size document_chunks.embedding to EMBEDDING_DIMENSION and rename embedding error codes.

Changing the dimension drops every stored chunk embedding (vectors of another
model cannot be converted); POST /knowledge-bases/{id}/documents/reindex fills
them again. Re-running with the dimension unchanged does nothing, so a later
EMBEDDING_DIMENSION change is applied by running this upgrade path again
(``alembic downgrade 0038_processing_job_claims && alembic upgrade head``).
Downgrade returns the column to vector(1536), the OpenRouter-era size, and
also drops the stored embeddings.

Revision ID: 0039_embedding_dimension
Revises: 0038_processing_job_claims
"""
import sqlalchemy as sa
from alembic import op

from app.config import get_settings


revision = "0039_embedding_dimension"
down_revision = "0038_processing_job_claims"
branch_labels = None
depends_on = None

_RENAMED_CODES = ("REQUEST_TOO_LARGE", "INVALID_RESPONSE", "DIMENSION_MISMATCH")


def _resize_embedding_column(dimension: int) -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    current = bind.execute(sa.text(
        "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
        "WHERE attrelid = 'document_chunks'::regclass AND attname = 'embedding' AND NOT attisdropped"
    )).scalar()
    if current == f"vector({dimension})":
        return
    op.execute("DROP INDEX IF EXISTS ix_document_chunks_embedding_hnsw")
    op.execute("ALTER TABLE document_chunks DROP COLUMN IF EXISTS embedding")
    op.execute(f"ALTER TABLE document_chunks ADD COLUMN embedding vector({int(dimension)})")
    op.execute("CREATE INDEX ix_document_chunks_embedding_hnsw ON document_chunks USING hnsw (embedding vector_cosine_ops)")


def _rename_error_codes(old_prefix: str, new_prefix: str) -> None:
    for table in ("documents", "processing_jobs"):
        for code in _RENAMED_CODES:
            op.execute(sa.text(f"UPDATE {table} SET error_code = :new WHERE error_code = :old")
                       .bindparams(new=new_prefix + code, old=old_prefix + code))


def upgrade() -> None:
    _resize_embedding_column(get_settings().embedding_dimension)
    _rename_error_codes("OPENROUTER_EMBEDDING_", "EMBEDDING_")


def downgrade() -> None:
    _resize_embedding_column(1536)
    _rename_error_codes("EMBEDDING_", "OPENROUTER_EMBEDDING_")
