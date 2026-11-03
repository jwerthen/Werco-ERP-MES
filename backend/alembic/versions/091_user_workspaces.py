"""Private saved views and resumable drafts.

Revision ID: 091_user_workspaces
Revises: 090_document_revision_chain
"""

import sqlalchemy as sa

from alembic import op

revision = "091_user_workspaces"
down_revision = "090_document_revision_chain"
branch_labels = None
depends_on = None

# Every object is guarded: the fresh bootstrap (create_all -> stamp 058 -> upgrade) runs
# this over a schema the models already built. RLS and the REVOKEs stay unconditional --
# create_all issues neither, and both are idempotent.


def _offline() -> bool:
    # ``--sql`` has no database to inspect: emit every statement, as before.
    return op.get_context().as_sql


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    return _inspector().has_table(table_name)


def _has_index(table_name: str, index_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(ix["name"] == index_name for ix in _inspector().get_indexes(table_name))


def upgrade():
    offline = _offline()
    if offline or not _has_table("user_workspace_records"):
        op.create_table(
            "user_workspace_records",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "company_id",
                sa.Integer(),
                sa.ForeignKey("companies.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "user_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("namespace", sa.String(60), nullable=False),
            sa.Column("kind", sa.String(10), nullable=False),
            sa.Column("key", sa.String(80), nullable=False),
            sa.Column("name", sa.String(100), nullable=False),
            sa.Column("data", sa.JSON(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint(
                "company_id",
                "user_id",
                "namespace",
                "kind",
                "key",
                name="uq_user_workspace_key",
            ),
        )
    if offline or not _has_index("user_workspace_records", "ix_user_workspace_owner"):
        op.create_index(
            "ix_user_workspace_owner",
            "user_workspace_records",
            ["company_id", "user_id", "namespace", "kind"],
        )
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE user_workspace_records ENABLE ROW LEVEL SECURITY")
        op.execute("REVOKE ALL ON user_workspace_records FROM PUBLIC")
        # ERP JWTs are verified by FastAPI, not Supabase Auth. No direct client access.
        op.execute("""DO $$ BEGIN
          IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'anon') THEN
            REVOKE ALL ON user_workspace_records FROM anon;
          END IF;
          IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'authenticated') THEN
            REVOKE ALL ON user_workspace_records FROM authenticated;
          END IF;
        END $$""")


def downgrade():
    offline = _offline()
    if offline or _has_index("user_workspace_records", "ix_user_workspace_owner"):
        op.drop_index("ix_user_workspace_owner", table_name="user_workspace_records")
    if offline or _has_table("user_workspace_records"):
        op.drop_table("user_workspace_records")
