"""Compensating backfill: pre-RBAC accounts become admins.

0027 intended every pre-existing account to become an admin (before RBAC
every login already had full rights).  Its backfill was buggy: the role
column was added with server_default 'user', so existing rows landed on
'user' immediately and the `WHERE role IS NULL OR role = ''` update
matched nothing.

This migration restores the intended outcome.  It is safe on every
deployment that ran the buggy 0027: while it was in effect, only
bootstrap could create users (POST /users requires an admin, and no
account had the admin role), and bootstrap stamps role='admin'
explicitly — so any 'user'/'manager' rows at this point are pre-RBAC
accounts that should be admins.
"""
from alembic import op


revision = "0029_rbac_role_backfill"
down_revision = "0028_user_credentials_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE users SET role = 'admin' WHERE role <> 'admin'")


def downgrade() -> None:
    # Intentionally a no-op: we cannot know which admins were pre-RBAC.
    pass
