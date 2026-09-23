"""Add an uploadable cover image to Knowledge Bases."""
from alembic import op
import sqlalchemy as sa


revision = "0033_knowledge_base_cover"
down_revision = "0032_document_quality_cache"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("knowledge_bases")}
    if "cover_image_path" not in columns:
        op.add_column("knowledge_bases", sa.Column("cover_image_path", sa.String(500), nullable=True))
    if "cover_image_mime_type" not in columns:
        op.add_column("knowledge_bases", sa.Column("cover_image_mime_type", sa.String(80), nullable=True))


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("knowledge_bases")}
    for name in ("cover_image_mime_type", "cover_image_path"):
        if name in columns:
            op.drop_column("knowledge_bases", name)
