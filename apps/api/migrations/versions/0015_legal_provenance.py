"""Add curator provenance fields to legal instruments.

Revision ID: 0015_legal_provenance
Revises: 0014_legal_registry
"""
import sqlalchemy as sa
from alembic import op


revision = "0015_legal_provenance"
down_revision = "0014_legal_registry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("legal_instruments")}
    with op.batch_alter_table("legal_instruments") as batch:
        if "source_uri" not in columns:
            batch.add_column(sa.Column("source_uri", sa.String(length=2000), nullable=True))
        if "source_reference" not in columns:
            batch.add_column(sa.Column("source_reference", sa.String(length=500), nullable=True))
        if "reviewed_at" not in columns:
            batch.add_column(sa.Column("reviewed_at", sa.DateTime(), nullable=True))
        if "reviewed_by" not in columns:
            batch.add_column(sa.Column("reviewed_by", sa.String(length=255), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("legal_instruments") as batch:
        batch.drop_column("reviewed_by")
        batch.drop_column("reviewed_at")
        batch.drop_column("source_reference")
        batch.drop_column("source_uri")
