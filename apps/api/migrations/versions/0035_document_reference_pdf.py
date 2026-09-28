"""Store an optional evidence PDF alongside a Markdown document."""

from alembic import op
import sqlalchemy as sa


revision = "0035_document_reference_pdf"
down_revision = "0034_multi_select_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("documents") as batch_op:
        batch_op.add_column(sa.Column("reference_pdf_path", sa.String(length=1000), nullable=True))
        batch_op.add_column(sa.Column("reference_pdf_filename", sa.String(length=500), nullable=True))
        batch_op.add_column(sa.Column("reference_pdf_size", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("reference_pdf_checksum_sha256", sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("documents") as batch_op:
        batch_op.drop_column("reference_pdf_checksum_sha256")
        batch_op.drop_column("reference_pdf_size")
        batch_op.drop_column("reference_pdf_filename")
        batch_op.drop_column("reference_pdf_path")
