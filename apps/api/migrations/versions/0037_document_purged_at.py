"""Record when a deleted document's content was permanently purged."""
import sqlalchemy as sa
from alembic import op


revision = "0037_document_purged_at"
down_revision = "0036_document_reference_file"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("documents") as batch_op:
        batch_op.add_column(sa.Column("purged_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("documents") as batch_op:
        batch_op.drop_column("purged_at")
