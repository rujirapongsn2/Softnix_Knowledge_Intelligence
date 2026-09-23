"""Cache deterministic document quality reports."""
from alembic import op
import sqlalchemy as sa


revision = "0032_document_quality_cache"
down_revision = "0031_auto_metadata"
branch_labels = None
depends_on = None


def upgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("documents")}
    for column in (
        sa.Column("quality_report", sa.JSON(), nullable=True),
        sa.Column("quality_fingerprint", sa.String(64), nullable=True),
        sa.Column("quality_evaluated_at", sa.DateTime(), nullable=True),
    ):
        if column.name not in columns:
            op.add_column("documents", column)
def downgrade():
    for name in ("quality_evaluated_at", "quality_fingerprint", "quality_report"):
        op.drop_column("documents", name)
