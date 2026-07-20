"""Add deterministic legal work/version identity fields."""
from alembic import op
import sqlalchemy as sa

revision = "0017_legal_work_timeline"
down_revision = "0016_normalized_traces"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("legal_instruments")}
    indexes = {index["name"] for index in inspector.get_indexes("legal_instruments")}
    if "legal_work_key" not in columns:
        op.add_column("legal_instruments", sa.Column("legal_work_key", sa.String(length=700), nullable=True))
    if "document_class" not in columns:
        op.add_column("legal_instruments", sa.Column("document_class", sa.String(length=30), nullable=True))
    if "version_date" not in columns:
        op.add_column("legal_instruments", sa.Column("version_date", sa.Date(), nullable=True))
    if "ix_legal_instruments_legal_work_key" not in indexes:
        op.create_index("ix_legal_instruments_legal_work_key", "legal_instruments", ["legal_work_key"])
    if "ix_legal_instruments_document_class" not in indexes:
        op.create_index("ix_legal_instruments_document_class", "legal_instruments", ["document_class"])
    if "ix_legal_instruments_version_date" not in indexes:
        op.create_index("ix_legal_instruments_version_date", "legal_instruments", ["version_date"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = {index["name"] for index in inspector.get_indexes("legal_instruments")}
    columns = {column["name"] for column in inspector.get_columns("legal_instruments")}
    for name in ("ix_legal_instruments_version_date", "ix_legal_instruments_document_class", "ix_legal_instruments_legal_work_key"):
        if name in indexes:
            op.drop_index(name, table_name="legal_instruments")
    for name in ("version_date", "document_class", "legal_work_key"):
        if name in columns:
            op.drop_column("legal_instruments", name)
