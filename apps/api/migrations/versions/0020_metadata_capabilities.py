"""Add searchable metadata projection for document type fields."""
from alembic import op
import sqlalchemy as sa


revision = "0020_metadata_capabilities"
down_revision = "0019_document_template_snapshot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("documents")}
    if "metadata_search_text" not in columns:
        op.add_column("documents", sa.Column("metadata_search_text", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("documents")}
    if "metadata_search_text" in columns:
        op.drop_column("documents", "metadata_search_text")
