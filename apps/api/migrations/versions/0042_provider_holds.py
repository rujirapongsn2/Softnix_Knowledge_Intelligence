"""Add provider_holds: providers whose credits or key stopped serving jobs until a probe succeeds.

Revision ID: 0042_provider_holds
Revises: 0041_llm_error_codes
"""
import sqlalchemy as sa
from alembic import op


revision = "0042_provider_holds"
down_revision = "0041_llm_error_codes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provider_holds",
        sa.Column("provider", sa.String(30), primary_key=True),
        sa.Column("reason_code", sa.String(100), nullable=False),
        sa.Column("held_since", sa.DateTime(), nullable=False),
        sa.Column("next_probe_at", sa.DateTime(), nullable=False),
        sa.Column("probe_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_probe_error", sa.String(100), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("provider_holds")
