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
    if offline or not _has_table("production_receipts"):
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
    if offline or not _has_index("production_receipts", "ix_production_receipts_company_id"):
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
    if _offline() or _has_table("production_receipts"):
        op.drop_table("production_receipts")
