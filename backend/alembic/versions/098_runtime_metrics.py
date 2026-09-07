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


def upgrade():
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
    op.create_index("ix_runtime_metric_company_created", "runtime_metric_samples", ["company_id", "created_at"])
    op.create_index("ix_runtime_metric_retention", "runtime_metric_samples", ["created_at"])
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
    op.drop_table("runtime_metric_settings")
    op.drop_table("runtime_metric_samples")
