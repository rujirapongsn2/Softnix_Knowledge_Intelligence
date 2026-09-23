"""Index each member of multi-select metadata independently."""
from alembic import op


revision = "0034_multi_select_metadata"
down_revision = "0033_knowledge_base_cover"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("document_metadata_values") as batch_op:
        batch_op.drop_constraint("uq_document_metadata_value", type_="unique")


def downgrade() -> None:
    # The indexed projection is rebuildable from document_metadata, so collapse
    # each multi-valued field to one row before restoring the old constraint.
    op.execute("""
        DELETE FROM document_metadata_values
        WHERE id NOT IN (
            SELECT MIN(id) FROM document_metadata_values GROUP BY document_id, field_key
        )
    """)
    with op.batch_alter_table("document_metadata_values") as batch_op:
        batch_op.create_unique_constraint(
            "uq_document_metadata_value",
            ["document_id", "field_key"],
        )
