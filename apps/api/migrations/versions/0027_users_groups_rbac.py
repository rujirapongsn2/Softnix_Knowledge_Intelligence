"""Users & groups RBAC: roles, groups, KB ownership, token creators.

- users.role          'user' | 'manager' | 'admin' (existing admin backfilled)
- users.group_id      FK groups.id, nullable (v1: single group per user)
- groups              name unique, description
- kb_owners           KB ↔ owner many-to-many (v1: one row = the creator)
- token_keys.created_by  FK users.id, nullable (legacy rows -> bootstrap admin)

Idempotent guards mirror 0026: re-running against a partially-migrated DB is
safe, and the same checks let bootstrap() create the schema on fresh dev/test
databases where Alembic never runs.
"""
from alembic import op
import sqlalchemy as sa


revision = "0027_users_groups_rbac"
down_revision = "0026_knowledge_base_icon"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "groups" not in tables:
        op.create_table(
            "groups",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_groups_name", "groups", ["name"], unique=True)

    if "kb_owners" not in tables:
        op.create_table(
            "kb_owners",
            sa.Column("kb_id", sa.String(length=36), sa.ForeignKey("knowledge_bases.id"), primary_key=True),
            sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id"), primary_key=True),
        )
        op.create_index("ix_kb_owners_user", "kb_owners", ["user_id"])

    user_columns = {column["name"] for column in inspector.get_columns("users")}
    user_indexes = {index["name"] for index in inspector.get_indexes("users")}
    if "role" not in user_columns:
        op.add_column("users", sa.Column("role", sa.String(length=20), nullable=False, server_default="user"))
    if "ix_users_role" not in user_indexes:
        op.create_index("ix_users_role", "users", ["role"], unique=False)
    if "group_id" not in user_columns:
        op.add_column(
            "users",
            sa.Column("group_id", sa.String(length=36), sa.ForeignKey("groups.id"), nullable=True),
        )

    token_columns = {column["name"] for column in inspector.get_columns("token_keys")}
    if "created_by" not in token_columns:
        op.add_column(
            "token_keys",
            sa.Column("created_by", sa.String(length=36), sa.ForeignKey("users.id"), nullable=True),
        )

    # Backfills ----------------------------------------------------------------
    # Every existing account becomes an admin: today the deployment has exactly
    # one human admin created from INITIAL_ADMIN_*, and pre-RBAC every login
    # already had full rights — so this preserves behaviour exactly.  (If real
    # multi-user rows somehow exist, making them admins is still the least
    # surprising outcome for a deploy that predates roles.)
    op.execute("UPDATE users SET role = 'admin' WHERE role IS NULL OR role = ''")
    # Legacy tokens predate created_by; attribute them to the oldest admin so
    # they remain manageable instead of becoming invisible to everyone.
    op.execute(
        "UPDATE token_keys SET created_by = ("
        "  SELECT id FROM users WHERE role = 'admin' ORDER BY created_at ASC LIMIT 1"
        ") WHERE created_by IS NULL"
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    token_columns = {column["name"] for column in inspector.get_columns("token_keys")}
    if "created_by" in token_columns:
        op.drop_column("token_keys", "created_by")

    user_columns = {column["name"] for column in inspector.get_columns("users")}
    if "group_id" in user_columns:
        op.drop_column("users", "group_id")
    op.drop_index("ix_users_role", table_name="users")
    if "role" in user_columns:
        op.drop_column("users", "role")

    tables = set(inspector.get_table_names())
    if "kb_owners" in tables:
        op.drop_table("kb_owners")
    if "groups" in tables:
        op.drop_table("groups")
