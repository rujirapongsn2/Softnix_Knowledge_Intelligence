"""Generalize Markdown evidence attachments beyond PDF."""

from alembic import op
import sqlalchemy as sa


revision = "0036_document_reference_file"
down_revision = "0035_document_reference_pdf"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("documents") as batch_op:
        batch_op.alter_column("reference_pdf_path", new_column_name="reference_file_path", existing_type=sa.String(1000))
        batch_op.alter_column("reference_pdf_filename", new_column_name="reference_file_filename", existing_type=sa.String(500))
        batch_op.alter_column("reference_pdf_size", new_column_name="reference_file_size", existing_type=sa.Integer())
        batch_op.alter_column("reference_pdf_checksum_sha256", new_column_name="reference_file_checksum_sha256", existing_type=sa.String(64))
        batch_op.add_column(sa.Column("reference_file_mime_type", sa.String(150), nullable=True))
    op.execute("UPDATE documents SET reference_file_mime_type = 'application/pdf' WHERE reference_file_path IS NOT NULL")


def downgrade() -> None:
    # Only PDF attachments are representable in the previous schema.
    non_pdf_count = op.get_bind().execute(sa.text(
        "SELECT COUNT(*) FROM documents WHERE reference_file_path IS NOT NULL AND "
        "(reference_file_mime_type IS NULL OR reference_file_mime_type <> 'application/pdf')"
    )).scalar_one()
    if non_pdf_count:
        raise RuntimeError("Cannot downgrade while non-PDF reference files are attached")
    with op.batch_alter_table("documents") as batch_op:
        batch_op.drop_column("reference_file_mime_type")
        batch_op.alter_column("reference_file_path", new_column_name="reference_pdf_path", existing_type=sa.String(1000))
        batch_op.alter_column("reference_file_filename", new_column_name="reference_pdf_filename", existing_type=sa.String(500))
        batch_op.alter_column("reference_file_size", new_column_name="reference_pdf_size", existing_type=sa.Integer())
        batch_op.alter_column("reference_file_checksum_sha256", new_column_name="reference_pdf_checksum_sha256", existing_type=sa.String(64))
