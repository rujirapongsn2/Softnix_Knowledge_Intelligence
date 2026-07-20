"""Add configurable document metadata templates."""
from alembic import op
import sqlalchemy as sa


revision = "0018_document_metadata_templates"
down_revision = "0017_legal_work_timeline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "document_metadata_templates" not in tables:
        op.create_table(
            "document_metadata_templates",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("knowledge_base_id", sa.String(length=36), sa.ForeignKey("knowledge_bases.id"), nullable=False),
            sa.Column("code", sa.String(length=120), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("base_document_type", sa.String(length=40), nullable=False, server_default="general"),
            sa.Column("fields", sa.JSON(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("knowledge_base_id", "code", name="uq_document_metadata_template_code"),
        )
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("document_metadata_templates")}
    for name, column in (("ix_document_metadata_templates_knowledge_base_id", "knowledge_base_id"),
                         ("ix_document_metadata_templates_code", "code"),
                         ("ix_document_metadata_templates_base_document_type", "base_document_type"),
                         ("ix_document_metadata_templates_is_active", "is_active")):
        if name not in indexes:
            op.create_index(name, "document_metadata_templates", [column])
    document_columns = {column["name"] for column in sa.inspect(bind).get_columns("documents")}
    for name, column in (("metadata_template_id", sa.String(length=36)), ("metadata_template_name", sa.String(length=255)),
                         ("metadata_template_version", sa.Integer()), ("document_metadata", sa.JSON())):
        if name not in document_columns:
            nullable = name == "metadata_template_id" or name == "metadata_template_name" or name == "metadata_template_version"
            kwargs = {"nullable": nullable}
            if name == "document_metadata":
                kwargs.update(nullable=False, server_default=sa.text("'{}'"))
            op.add_column("documents", sa.Column(name, column, **kwargs))
    if "ix_documents_metadata_template_id" not in {index["name"] for index in sa.inspect(bind).get_indexes("documents")}:
        op.create_index("ix_documents_metadata_template_id", "documents", ["metadata_template_id"])


def downgrade() -> None:
    op.drop_index("ix_documents_metadata_template_id", table_name="documents")
    op.drop_column("documents", "document_metadata")
    op.drop_column("documents", "metadata_template_version")
    op.drop_column("documents", "metadata_template_name")
    op.drop_column("documents", "metadata_template_id")
    op.drop_index("ix_document_metadata_templates_is_active", table_name="document_metadata_templates")
    op.drop_index("ix_document_metadata_templates_base_document_type", table_name="document_metadata_templates")
    op.drop_index("ix_document_metadata_templates_code", table_name="document_metadata_templates")
    op.drop_index("ix_document_metadata_templates_knowledge_base_id", table_name="document_metadata_templates")
    op.drop_table("document_metadata_templates")
