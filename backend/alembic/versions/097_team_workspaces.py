"""Company-shared table views, separate from private drafts.

Revision ID: 097_team_workspaces
Revises: 096_working_calendars
"""

import sqlalchemy as sa

from alembic import op

revision = "097_team_workspaces"
down_revision = "096_working_calendars"
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
    if offline or not _has_index("work_orders", "ix_work_orders_company_live_priority_due"):
        op.create_index(
            "ix_work_orders_company_live_priority_due",
            "work_orders",
            ["company_id", "is_deleted", "priority", "due_date", "id"],
        )
    if offline or not _has_table("team_workspace_records"):
        op.create_table(
            "team_workspace_records",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
            sa.Column("namespace", sa.String(60), nullable=False),
            sa.Column("key", sa.String(80), nullable=False),
            sa.Column("name", sa.String(100), nullable=False),
            sa.Column("data", sa.JSON(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("updated_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("company_id", "namespace", "key", name="uq_team_workspace_key"),
        )
    if offline or not _has_index("team_workspace_records", "ix_team_workspace_company_namespace"):
        op.create_index("ix_team_workspace_company_namespace", "team_workspace_records", ["company_id", "namespace"])
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE team_workspace_records ENABLE ROW LEVEL SECURITY")
        op.execute("REVOKE ALL ON team_workspace_records FROM PUBLIC")
        op.execute("REVOKE ALL ON SEQUENCE team_workspace_records_id_seq FROM PUBLIC")
        # FastAPI verifies custom ERP JWTs. No Supabase Data API access is granted.
        op.execute("""DO $$ BEGIN
          IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'anon') THEN
            REVOKE ALL ON team_workspace_records FROM anon;
            REVOKE ALL ON SEQUENCE team_workspace_records_id_seq FROM anon;
          END IF;
          IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'authenticated') THEN
            REVOKE ALL ON team_workspace_records FROM authenticated;
            REVOKE ALL ON SEQUENCE team_workspace_records_id_seq FROM authenticated;
          END IF;
        END $$""")


def downgrade():
    offline = _offline()
    if offline or _has_index("work_orders", "ix_work_orders_company_live_priority_due"):
        op.drop_index("ix_work_orders_company_live_priority_due", table_name="work_orders")
    if offline or _has_index("team_workspace_records", "ix_team_workspace_company_namespace"):
        op.drop_index("ix_team_workspace_company_namespace", table_name="team_workspace_records")
    if offline or _has_table("team_workspace_records"):
        op.drop_table("team_workspace_records")
