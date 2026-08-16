"""User credentials_version: invalidates outstanding session JWTs on password change/reset."""
from alembic import op
import sqlalchemy as sa


revision = "0028_user_credentials_version"
down_revision = "0027_users_groups_rbac"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("users")}
    if "credentials_version" not in columns:
        op.add_column("users", sa.Column("credentials_version", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("users")}
    if "credentials_version" in columns:
        op.drop_column("users", "credentials_version")
