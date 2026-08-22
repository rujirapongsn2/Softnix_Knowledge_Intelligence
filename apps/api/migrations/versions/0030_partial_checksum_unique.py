"""F8: partial unique index for document checksums

Revision ID: 0030_partial_checksum_unique
Revises: 0029_rbac_role_backfill
"""
from alembic import op
import sqlalchemy as sa

revision = "0030_partial_checksum_unique"
down_revision = "0029_rbac_role_backfill"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Replace the table-level unique constraint (which counts soft-deleted
    # rows and turns re-uploads after a delete into 500s) with a partial
    # unique index scoped to live rows only.
    #
    # Everything is inspector-guarded: 0001 builds the schema via
    # Base.metadata.create_all against the LIVE models, so on a fresh
    # install this migration runs against a DB that already has the new
    # partial index and never had the old constraint (review C1).
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "uq_document_checksum" in {c["name"] for c in insp.get_unique_constraints("documents")}:
        op.drop_constraint("uq_document_checksum", "documents", type_="unique")
    existing_indexes = {i["name"] for i in insp.get_indexes("documents")}
    if "uq_document_checksum_live" not in existing_indexes:
        op.create_index(
            "uq_document_checksum_live",
            "documents",
            ["knowledge_base_id", "checksum_sha256"],
            unique=True,
            postgresql_where=sa.text("deleted_at IS NULL"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing_indexes = {i["name"] for i in insp.get_indexes("documents")}
    if "uq_document_checksum_live" in existing_indexes:
        op.drop_index("uq_document_checksum_live", "documents")
    if "uq_document_checksum" not in {c["name"] for c in insp.get_unique_constraints("documents")}:
        op.create_unique_constraint("uq_document_checksum", "documents", ["knowledge_base_id", "checksum_sha256"])
