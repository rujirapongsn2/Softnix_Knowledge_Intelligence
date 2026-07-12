"""Add stable legal-graph identities, origin, and review state.

Revision ID: 0011_legal_graph_schema_v2
Revises: 0010_document_type
"""
import sqlalchemy as sa
from alembic import op


revision = "0011_legal_graph_schema_v2"
down_revision = "0010_document_type"
branch_labels = None
depends_on = None


LEGAL_ENTITY_TYPES = ("Article", "LegalDocument", "Obligation", "Amendment", "Party")
LEGAL_RELATIONSHIP_TYPES = (
    "CONTAINS_ARTICLE", "CONTAINS_PROVISION", "ISSUED_BY", "PARTY_TO", "REQUIRES",
    "GRANTS_RIGHT", "PROHIBITS", "DEFINES", "ISSUED_UNDER", "IMPLEMENTS", "AMENDS",
    "REPEALS", "REFERS_TO", "GOVERNED_BY",
)


def upgrade() -> None:
    bind = op.get_bind()
    # 0001 bootstraps from current metadata on a brand-new installation.  In
    # that path the new columns already exist; an existing production database
    # reaches the migration without them and receives the data conversion.
    if "identity_key" in {column["name"] for column in sa.inspect(bind).get_columns("entities")}:
        return
    with op.batch_alter_table("entities") as batch:
        batch.add_column(sa.Column("identity_key", sa.String(length=700), nullable=True))
        batch.add_column(sa.Column("origin", sa.String(length=30), nullable=True))
        batch.add_column(sa.Column("review_status", sa.String(length=20), nullable=True))
        batch.add_column(sa.Column("is_legal", sa.Boolean(), nullable=True))
    with op.batch_alter_table("relationships") as batch:
        batch.add_column(sa.Column("origin", sa.String(length=30), nullable=True))
        batch.add_column(sa.Column("review_status", sa.String(length=20), nullable=True))
        batch.add_column(sa.Column("is_legal", sa.Boolean(), nullable=True))

    # Existing graph rows stay available.  A rebuild will replace only the
    # rows recognized as legal-schema output; manual graph work is untouched.
    op.execute("UPDATE entities SET identity_key = 'generic:' || canonical_name, origin = 'manual', review_status = 'verified', is_legal = FALSE")
    op.execute("UPDATE relationships SET origin = 'manual', review_status = 'verified', is_legal = FALSE")
    quoted_entity_types = ", ".join(f"'{value}'" for value in LEGAL_ENTITY_TYPES)
    quoted_relationship_types = ", ".join(f"'{value}'" for value in LEGAL_RELATIONSHIP_TYPES)
    op.execute(f"UPDATE entities SET origin = 'legal_schema', is_legal = TRUE WHERE entity_type IN ({quoted_entity_types})")
    op.execute(f"UPDATE relationships SET origin = 'legal_schema', is_legal = TRUE WHERE relationship_type IN ({quoted_relationship_types})")
    op.execute("UPDATE relationships SET origin = 'ai_suggestion', review_status = 'suggested', is_legal = TRUE WHERE relationship_type = 'RELATED_TO'")

    with op.batch_alter_table("entities") as batch:
        batch.alter_column("identity_key", existing_type=sa.String(length=700), nullable=False)
        batch.alter_column("origin", existing_type=sa.String(length=30), nullable=False, server_default="manual")
        batch.alter_column("review_status", existing_type=sa.String(length=20), nullable=False, server_default="verified")
        batch.alter_column("is_legal", existing_type=sa.Boolean(), nullable=False, server_default=sa.false())
        if bind.dialect.name == "postgresql":
            batch.drop_constraint("uq_entity_canonical", type_="unique")
        batch.create_unique_constraint("uq_entity_identity", ["knowledge_base_id", "identity_key", "entity_type"])
        batch.create_index("ix_entities_identity_key", ["identity_key"])
        batch.create_index("ix_entities_origin", ["origin"])
        batch.create_index("ix_entities_review_status", ["review_status"])
        batch.create_index("ix_entities_is_legal", ["is_legal"])
    with op.batch_alter_table("relationships") as batch:
        batch.alter_column("origin", existing_type=sa.String(length=30), nullable=False, server_default="manual")
        batch.alter_column("review_status", existing_type=sa.String(length=20), nullable=False, server_default="verified")
        batch.alter_column("is_legal", existing_type=sa.Boolean(), nullable=False, server_default=sa.false())
        batch.create_index("ix_relationships_origin", ["origin"])
        batch.create_index("ix_relationships_review_status", ["review_status"])
        batch.create_index("ix_relationships_is_legal", ["is_legal"])


def downgrade() -> None:
    with op.batch_alter_table("relationships") as batch:
        batch.drop_index("ix_relationships_is_legal")
        batch.drop_index("ix_relationships_review_status")
        batch.drop_index("ix_relationships_origin")
        batch.drop_column("is_legal")
        batch.drop_column("review_status")
        batch.drop_column("origin")
    with op.batch_alter_table("entities") as batch:
        batch.drop_constraint("uq_entity_identity", type_="unique")
        batch.create_unique_constraint("uq_entity_canonical", ["knowledge_base_id", "canonical_name", "entity_type"])
        batch.drop_index("ix_entities_is_legal")
        batch.drop_index("ix_entities_review_status")
        batch.drop_index("ix_entities_origin")
        batch.drop_index("ix_entities_identity_key")
        batch.drop_column("is_legal")
        batch.drop_column("review_status")
        batch.drop_column("origin")
        batch.drop_column("identity_key")
