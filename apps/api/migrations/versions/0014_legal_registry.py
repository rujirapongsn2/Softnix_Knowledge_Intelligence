"""Add the legal registry (families, instruments, cross-instrument relations)
and section identity columns on document_chunks.

Revision ID: 0014_legal_registry
Revises: 0013_document_published_at
"""
import sqlalchemy as sa
from alembic import op


revision = "0014_legal_registry"
down_revision = "0013_document_published_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    existing_tables = set(sa.inspect(bind).get_table_names())

    if "legal_families" not in existing_tables:
        op.create_table(
            "legal_families",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("knowledge_base_id", sa.String(length=36), sa.ForeignKey("knowledge_bases.id"), nullable=False),
            sa.Column("base_title", sa.String(length=500), nullable=False),
            sa.Column("normalized_key", sa.String(length=700), nullable=False),
            sa.UniqueConstraint("knowledge_base_id", "normalized_key", name="uq_legal_family_key"),
        )
        op.create_index("ix_legal_families_knowledge_base_id", "legal_families", ["knowledge_base_id"])
        op.create_index("ix_legal_families_normalized_key", "legal_families", ["normalized_key"])

    if "legal_instruments" not in existing_tables:
        op.create_table(
            "legal_instruments",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("knowledge_base_id", sa.String(length=36), sa.ForeignKey("knowledge_bases.id"), nullable=False),
            sa.Column("document_id", sa.String(length=36), sa.ForeignKey("documents.id"), nullable=False, unique=True),
            sa.Column("family_id", sa.String(length=36), sa.ForeignKey("legal_families.id"), nullable=True),
            sa.Column("kind", sa.String(length=40), nullable=False, server_default="other"),
            sa.Column("authority_level", sa.Integer(), nullable=False, server_default="20"),
            sa.Column("official_title", sa.String(length=500), nullable=True),
            sa.Column("official_number", sa.String(length=120), nullable=True),
            sa.Column("issuer", sa.String(length=300), nullable=True),
            sa.Column("jurisdiction", sa.String(length=120), nullable=True),
            sa.Column("version_label", sa.String(length=120), nullable=True),
            sa.Column("enacted_year", sa.Integer(), nullable=True),
            sa.Column("effective_from", sa.Date(), nullable=True),
            sa.Column("effective_to", sa.Date(), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="unknown"),
            sa.Column("status_source", sa.String(length=20), nullable=False, server_default="resolver"),
            sa.Column("status_reason", sa.Text(), nullable=True),
            sa.Column("review_status", sa.String(length=20), nullable=False, server_default="unreviewed"),
        )
        op.create_index("ix_legal_instruments_knowledge_base_id", "legal_instruments", ["knowledge_base_id"])
        op.create_index("ix_legal_instruments_document_id", "legal_instruments", ["document_id"])
        op.create_index("ix_legal_instruments_family_id", "legal_instruments", ["family_id"])
        op.create_index("ix_legal_instruments_kind", "legal_instruments", ["kind"])
        op.create_index("ix_legal_instruments_effective_from", "legal_instruments", ["effective_from"])
        op.create_index("ix_legal_instruments_effective_to", "legal_instruments", ["effective_to"])
        op.create_index("ix_legal_instruments_status", "legal_instruments", ["status"])

    if "legal_instrument_relations" not in existing_tables:
        op.create_table(
            "legal_instrument_relations",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("knowledge_base_id", sa.String(length=36), sa.ForeignKey("knowledge_bases.id"), nullable=False),
            sa.Column("source_instrument_id", sa.String(length=36), sa.ForeignKey("legal_instruments.id"), nullable=False),
            sa.Column("target_instrument_id", sa.String(length=36), sa.ForeignKey("legal_instruments.id"), nullable=True),
            sa.Column("relationship_id", sa.String(length=36), sa.ForeignKey("relationships.id"), nullable=True),
            sa.Column("target_text", sa.String(length=700), nullable=True),
            sa.Column("target_provision", sa.String(length=120), nullable=True),
            sa.Column("relation", sa.String(length=30), nullable=False),
            sa.Column("evidence_quote", sa.Text(), nullable=True),
            sa.Column("confidence", sa.Float(), nullable=True),
            sa.Column("origin", sa.String(length=30), nullable=False, server_default="legal_schema"),
            sa.Column("review_status", sa.String(length=20), nullable=False, server_default="suggested"),
            sa.UniqueConstraint("source_instrument_id", "relation", "target_instrument_id", "target_provision",
                                name="uq_legal_instrument_relation"),
        )
        op.create_index("ix_legal_instrument_relations_knowledge_base_id", "legal_instrument_relations", ["knowledge_base_id"])
        op.create_index("ix_legal_instrument_relations_source_instrument_id", "legal_instrument_relations", ["source_instrument_id"])
        op.create_index("ix_legal_instrument_relations_target_instrument_id", "legal_instrument_relations", ["target_instrument_id"])
        op.create_index("ix_legal_instrument_relations_relationship_id", "legal_instrument_relations", ["relationship_id"])
        op.create_index("ix_legal_instrument_relations_relation", "legal_instrument_relations", ["relation"])
        op.create_index("ix_legal_instrument_relations_review_status", "legal_instrument_relations", ["review_status"])

    chunk_columns = {column["name"] for column in sa.inspect(bind).get_columns("document_chunks")}
    if "section_kind" not in chunk_columns:
        with op.batch_alter_table("document_chunks") as batch:
            batch.add_column(sa.Column("section_kind", sa.String(length=30), nullable=True))
            batch.add_column(sa.Column("section_number", sa.String(length=60), nullable=True))
            batch.add_column(sa.Column("section_label", sa.String(length=200), nullable=True))
        op.create_index("ix_document_chunks_section_number", "document_chunks", ["section_number"])


def downgrade() -> None:
    op.drop_index("ix_document_chunks_section_number", table_name="document_chunks")
    with op.batch_alter_table("document_chunks") as batch:
        batch.drop_column("section_label")
        batch.drop_column("section_number")
        batch.drop_column("section_kind")
    op.drop_table("legal_instrument_relations")
    op.drop_table("legal_instruments")
    op.drop_table("legal_families")
