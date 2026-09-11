"""Track extraction candidates separately from effective document metadata."""
from alembic import op
import sqlalchemy as sa

revision = "0031_auto_metadata"
down_revision = "0030_partial_checksum_unique"
branch_labels = None
depends_on = None


def upgrade():
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("documents")}
    for column in (
        sa.Column("metadata_observations", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("metadata_status", sa.String(30), nullable=False, server_default="not_started"),
        sa.Column("metadata_revision", sa.Integer(), nullable=False, server_default="0"),
    ):
        if column.name not in columns:
            op.add_column("documents", column)
    indexes = {i["name"] for i in sa.inspect(op.get_bind()).get_indexes("documents")}
    if "ix_documents_metadata_status" not in indexes:
        op.create_index("ix_documents_metadata_status", "documents", ["metadata_status"])

    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("document_metadata_values")}
    for column in (sa.Column("value_number", sa.Float()), sa.Column("value_date", sa.Date()), sa.Column("value_type", sa.String(20))):
        if column.name not in columns:
            op.add_column("document_metadata_values", column)
    indexes = {i["name"] for i in sa.inspect(op.get_bind()).get_indexes("document_metadata_values")}
    for suffix in ("number", "date"):
        name = "ix_document_metadata_" + suffix
        if name not in indexes:
            op.create_index(name, "document_metadata_values", ["knowledge_base_id", "field_key", "value_" + suffix])
    # Backfill typed projections without rewriting documents or template snapshots.
    from datetime import date
    import json
    documents = sa.table("documents", sa.column("id"), sa.column("metadata_template_fields", sa.JSON()))
    values = sa.table("document_metadata_values", sa.column("id"), sa.column("document_id"), sa.column("field_key"), sa.column("value_text"), sa.column("value_number", sa.Float()), sa.column("value_date", sa.Date()), sa.column("value_type"))
    rows = op.get_bind().execute(sa.select(values.c.id, values.c.field_key, values.c.value_text, documents.c.metadata_template_fields).select_from(values.join(documents, values.c.document_id == documents.c.id))).mappings()
    for row in rows:
        fields = row["metadata_template_fields"] or []
        if isinstance(fields, str):
            fields = json.loads(fields)
        field = next((f for f in fields if f.get("key") == row["field_key"]), {})
        kind = field.get("field_type", "text")
        patch = {"value_type": kind}
        try:
            if kind == "date":
                patch["value_date"] = date.fromisoformat(row["value_text"])
            elif kind == "number":
                patch["value_number"] = float(row["value_text"])
        except (ValueError, TypeError):
            pass
        op.get_bind().execute(values.update().where(values.c.id == row["id"]).values(**patch))


def downgrade():
    for suffix in ("number", "date"):
        op.drop_index("ix_document_metadata_" + suffix, table_name="document_metadata_values")
    for name in ("value_number", "value_date", "value_type"):
        op.drop_column("document_metadata_values", name)
    op.drop_index("ix_documents_metadata_status", table_name="documents")
    for name in ("metadata_revision", "metadata_status", "metadata_observations"):
        op.drop_column("documents", name)
