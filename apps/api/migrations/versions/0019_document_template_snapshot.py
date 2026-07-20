"""Keep metadata-field definitions stable for each uploaded document."""
from alembic import op
import sqlalchemy as sa


revision = "0019_document_template_snapshot"
down_revision = "0018_document_metadata_templates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("documents")}
    if "metadata_template_fields" not in columns:
        op.add_column("documents", sa.Column("metadata_template_fields", sa.JSON(), nullable=False, server_default=sa.text("'[]'")))


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("documents")}
    if "metadata_template_fields" in columns:
        op.drop_column("documents", "metadata_template_fields")
