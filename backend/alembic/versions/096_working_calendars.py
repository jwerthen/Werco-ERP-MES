"""Per-center weekly shifts and dated shutdown/capacity overrides.

Revision ID: 096_working_calendars
Revises: 095_kiosk_production_receipts
"""

import sqlalchemy as sa

from alembic import op

revision = "096_working_calendars"
down_revision = "095_kiosk_production_receipts"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "working_calendars",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("work_center_id", sa.Integer(), sa.ForeignKey("work_centers.id"), nullable=False),
        sa.Column("weekly_hours", sa.JSON(), nullable=False),
        sa.Column("overrides", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("company_id", "work_center_id", name="uq_working_calendar_center"),
    )
    op.create_index("ix_working_calendars_company_id", "working_calendars", ["company_id"])
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE working_calendars ENABLE ROW LEVEL SECURITY")
        op.execute("REVOKE ALL ON TABLE working_calendars FROM PUBLIC")
        op.execute("REVOKE ALL ON SEQUENCE working_calendars_id_seq FROM PUBLIC")
        op.execute("""DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
            REVOKE ALL ON TABLE working_calendars FROM anon;
            REVOKE ALL ON SEQUENCE working_calendars_id_seq FROM anon;
          END IF;
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
            REVOKE ALL ON TABLE working_calendars FROM authenticated;
            REVOKE ALL ON SEQUENCE working_calendars_id_seq FROM authenticated;
          END IF;
        END $$""")


def downgrade():
    op.drop_table("working_calendars")
