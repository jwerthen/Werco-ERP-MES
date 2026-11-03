"""Bounded first-party browser performance measurements.

Revision ID: 098_runtime_metrics
Revises: 097_team_workspaces
"""

import sqlalchemy as sa

from alembic import op

revision = "098_runtime_metrics"
down_revision = "097_team_workspaces"
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
    if offline or not _has_table("runtime_metric_samples"):
        op.create_table(
            "runtime_metric_samples",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
            sa.Column("metric_id", sa.String(80), nullable=False),
            sa.Column("name", sa.String(3), nullable=False),
            sa.Column("route", sa.String(100), nullable=False),
            sa.Column("device", sa.String(10), nullable=False),
            sa.Column("navigation", sa.String(8), nullable=False),
            sa.Column("release", sa.String(40), nullable=False),
            sa.Column("value", sa.Float(), nullable=False),
            sa.Column("sequence", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("company_id", "metric_id", name="uq_runtime_metric_receipt"),
        )
    for name, columns in [
        ("ix_runtime_metric_company_created", ["company_id", "created_at"]),
        ("ix_runtime_metric_retention", ["created_at"]),
    ]:
        if offline or not _has_index("runtime_metric_samples", name):
            op.create_index(name, "runtime_metric_samples", columns)
    if offline or not _has_table("runtime_metric_settings"):
        op.create_table(
            "runtime_metric_settings",
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("enabled", sa.Boolean(), nullable=False),
        )
    if op.get_bind().dialect.name == "postgresql":
        # FastAPI uses its own JWT and tenant checks; browser/PostgREST roles get no access.
        for table in ("runtime_metric_samples", "runtime_metric_settings"):
            op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            op.execute(f"REVOKE ALL ON TABLE {table} FROM PUBLIC")
            op.execute(f"""DO $$ BEGIN
              IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
                REVOKE ALL ON TABLE {table} FROM anon;
              END IF;
              IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
                REVOKE ALL ON TABLE {table} FROM authenticated;
              END IF;
            END $$""")
        op.execute("REVOKE ALL ON SEQUENCE runtime_metric_samples_id_seq FROM PUBLIC")
        op.execute("""DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
            REVOKE ALL ON SEQUENCE runtime_metric_samples_id_seq FROM anon;
          END IF;
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
            REVOKE ALL ON SEQUENCE runtime_metric_samples_id_seq FROM authenticated;
          END IF;
        END $$""")


def downgrade():
    offline = _offline()
    if offline or _has_table("runtime_metric_settings"):
        op.drop_table("runtime_metric_settings")
    if offline or _has_table("runtime_metric_samples"):
        op.drop_table("runtime_metric_samples")
