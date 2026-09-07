"""Durable idempotency receipts for additive production reports.

Revision ID: 095_kiosk_production_receipts
Revises: 094_document_deliveries
"""

import sqlalchemy as sa

from alembic import op

revision = "095_kiosk_production_receipts"
down_revision = "094_document_deliveries"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "production_receipts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("request_id", sa.String(100), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("operator_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("operation_id", sa.Integer(), sa.ForeignKey("work_order_operations.id"), nullable=False),
        sa.Column("time_entry_id", sa.Integer(), sa.ForeignKey("time_entries.id"), nullable=False),
        sa.Column("response", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("company_id", "request_id", name="uq_production_receipt_request"),
    )
    op.create_index("ix_production_receipts_company_id", "production_receipts", ["company_id"])
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE production_receipts ENABLE ROW LEVEL SECURITY")
        op.execute("REVOKE ALL ON TABLE production_receipts FROM PUBLIC")
        op.execute("REVOKE ALL ON SEQUENCE production_receipts_id_seq FROM PUBLIC")
        op.execute("""DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
            REVOKE ALL ON TABLE production_receipts FROM anon;
            REVOKE ALL ON SEQUENCE production_receipts_id_seq FROM anon;
          END IF;
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
            REVOKE ALL ON TABLE production_receipts FROM authenticated;
            REVOKE ALL ON SEQUENCE production_receipts_id_seq FROM authenticated;
          END IF;
        END $$""")


def downgrade():
    op.drop_table("production_receipts")
