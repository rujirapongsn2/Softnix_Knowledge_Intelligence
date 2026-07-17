"""Add deterministic legal work/version identity fields."""
from alembic import op
import sqlalchemy as sa

revision = "0017_legal_work_timeline"
down_revision = "0016_normalized_traces"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("legal_instruments", sa.Column("legal_work_key", sa.String(length=700), nullable=True))
    op.add_column("legal_instruments", sa.Column("document_class", sa.String(length=30), nullable=True))
    op.add_column("legal_instruments", sa.Column("version_date", sa.Date(), nullable=True))
    op.create_index("ix_legal_instruments_legal_work_key", "legal_instruments", ["legal_work_key"])
    op.create_index("ix_legal_instruments_document_class", "legal_instruments", ["document_class"])
    op.create_index("ix_legal_instruments_version_date", "legal_instruments", ["version_date"])


def downgrade() -> None:
    op.drop_index("ix_legal_instruments_version_date", table_name="legal_instruments")
    op.drop_index("ix_legal_instruments_document_class", table_name="legal_instruments")
    op.drop_index("ix_legal_instruments_legal_work_key", table_name="legal_instruments")
    op.drop_column("legal_instruments", "version_date")
    op.drop_column("legal_instruments", "document_class")
    op.drop_column("legal_instruments", "legal_work_key")
