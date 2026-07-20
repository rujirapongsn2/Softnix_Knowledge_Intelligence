"""Add a publisher-facing version role to legal instruments."""
from alembic import op
import sqlalchemy as sa


revision = "0022_legal_version_role"
down_revision = "0021_document_metadata_values"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("legal_instruments")}
    if "version_role" not in columns:
        op.add_column("legal_instruments", sa.Column("version_role", sa.String(length=40), nullable=True))
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("legal_instruments")}
    if "ix_legal_instruments_version_role" not in indexes:
        op.create_index("ix_legal_instruments_version_role", "legal_instruments", ["version_role"])
    # Legacy rows retain their existing broad classification until the legal
    # metadata re-extraction rollout assigns a more precise role.
    bind.execute(sa.text("UPDATE legal_instruments SET version_role = document_class WHERE version_role IS NULL"))


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("legal_instruments")}
    if "version_role" not in columns:
        return
    if "ix_legal_instruments_version_role" in {index["name"] for index in sa.inspect(bind).get_indexes("legal_instruments")}:
        op.drop_index("ix_legal_instruments_version_role", table_name="legal_instruments")
    op.drop_column("legal_instruments", "version_role")
