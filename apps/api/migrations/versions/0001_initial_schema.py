"""Initial production schema.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-07-11
"""
from alembic import op

from app import models  # noqa: F401 - register all models
from app.db import Base

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # Later models include pgvector columns; provision the type before the
        # metadata bootstrap so a fresh install can run every revision safely.
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
