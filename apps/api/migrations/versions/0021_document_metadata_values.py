"""Add an indexed projection for exact, filterable document metadata."""
import json

from alembic import op
import sqlalchemy as sa


revision = "0021_document_metadata_values"
down_revision = "0020_metadata_capabilities"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    template_columns = {column["name"] for column in sa.inspect(bind).get_columns("document_metadata_templates")}
    if "custom_fields" not in template_columns:
        op.add_column("document_metadata_templates", sa.Column("custom_fields", sa.JSON(), nullable=True))
    if "document_metadata_values" not in tables:
        op.create_table(
            "document_metadata_values",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("knowledge_base_id", sa.String(length=36), sa.ForeignKey("knowledge_bases.id"), nullable=False),
            sa.Column("document_id", sa.String(length=36), sa.ForeignKey("documents.id"), nullable=False),
            sa.Column("field_key", sa.String(length=80), nullable=False),
            sa.Column("value_text", sa.String(length=10000), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("document_id", "field_key", name="uq_document_metadata_value"),
        )
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("document_metadata_values")}
    if "ix_document_metadata_values_knowledge_base_id" not in indexes:
        op.create_index("ix_document_metadata_values_knowledge_base_id", "document_metadata_values", ["knowledge_base_id"])
    if "ix_document_metadata_values_document_id" not in indexes:
        op.create_index("ix_document_metadata_values_document_id", "document_metadata_values", ["document_id"])
    if "ix_document_metadata_values_field_key" not in indexes:
        op.create_index("ix_document_metadata_values_field_key", "document_metadata_values", ["field_key"])
    if "ix_document_metadata_filter" not in indexes:
        op.create_index("ix_document_metadata_filter", "document_metadata_values", ["knowledge_base_id", "field_key", "value_text"])

    # Backfill only fields explicitly marked filterable. Existing documents
    # without the capability remain intentionally absent from this index.
    table = sa.table(
        "document_metadata_values",
        sa.column("id", sa.String()), sa.column("knowledge_base_id", sa.String()),
        sa.column("document_id", sa.String()), sa.column("field_key", sa.String()),
        sa.column("value_text", sa.String()), sa.column("created_at", sa.DateTime()),
    )
    documents = sa.table(
        "documents",
        sa.column("id", sa.String()), sa.column("knowledge_base_id", sa.String()),
        sa.column("metadata_template_fields", sa.JSON()), sa.column("document_metadata", sa.JSON()),
    )
    existing = {row[0] for row in bind.execute(sa.select(table.c.document_id, table.c.field_key)).all()}
    for row in bind.execute(sa.select(documents)).mappings():
        fields = row.get("metadata_template_fields") or []
        values = row.get("document_metadata") or {}
        if isinstance(fields, str):
            fields = json.loads(fields)
        if isinstance(values, str):
            values = json.loads(values)
        for field in fields if isinstance(fields, list) else []:
            if not isinstance(field, dict) or not field.get("filterable"):
                continue
            key = field.get("key")
            value = values.get(key) if isinstance(values, dict) else None
            marker = (row["id"], key)
            if not key or value in (None, "") or marker in existing:
                continue
            bind.execute(sa.insert(table).values(
                id=str(__import__("uuid").uuid4()), knowledge_base_id=row["knowledge_base_id"],
                document_id=row["id"], field_key=key, value_text=str(value)[:10000],
                created_at=sa.func.now(),
            ))
            existing.add(marker)


def downgrade() -> None:
    bind = op.get_bind()
    if "document_metadata_values" not in sa.inspect(bind).get_table_names():
        return
    for name in ("ix_document_metadata_filter", "ix_document_metadata_values_field_key",
                 "ix_document_metadata_values_document_id", "ix_document_metadata_values_knowledge_base_id"):
        if name in {index["name"] for index in sa.inspect(bind).get_indexes("document_metadata_values")}:
            op.drop_index(name, table_name="document_metadata_values")
    op.drop_table("document_metadata_values")
    if "custom_fields" in {column["name"] for column in sa.inspect(bind).get_columns("document_metadata_templates")}:
        op.drop_column("document_metadata_templates", "custom_fields")
