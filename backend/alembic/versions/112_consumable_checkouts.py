"""Add immutable employee attribution for consumable inventory issues.

Revision ID: 112_consumable_checkouts
Revises: 111_continuous_improvement
"""

import sqlalchemy as sa

from alembic import op

revision = "112_consumable_checkouts"
down_revision = "111_continuous_improvement"
branch_labels = None
depends_on = None

TABLE = "consumable_checkouts"


def _exists():
    return not op.get_context().as_sql and sa.inspect(op.get_bind()).has_table(TABLE)


def _index(name, columns):
    if op.get_context().as_sql or name not in {row["name"] for row in sa.inspect(op.get_bind()).get_indexes(TABLE)}:
        op.create_index(name, TABLE, columns)


def upgrade():
    # create_all already includes this shape. On existing installations this is
    # additive: historical inventory issues are not guessed into employee records.
    if not _exists():
        op.create_table(
            TABLE,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
            sa.Column("inventory_item_id", sa.Integer(), sa.ForeignKey("inventory_items.id"), nullable=False),
            sa.Column(
                "inventory_transaction_id", sa.Integer(), sa.ForeignKey("inventory_transactions.id"), nullable=False
            ),
            sa.Column("part_id", sa.Integer(), sa.ForeignKey("parts.id"), nullable=False),
            sa.Column("part_number", sa.String(100), nullable=False),
            sa.Column("part_name", sa.String(255), nullable=False),
            sa.Column("unit_of_measure", sa.String(20), nullable=False),
            sa.Column("location", sa.String(100), nullable=False),
            sa.Column("lot_number", sa.String(100), nullable=True),
            sa.Column("employee_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("employee_name", sa.String(255), nullable=False),
            sa.Column("employee_code", sa.String(50), nullable=False),
            sa.Column("department", sa.String(100), nullable=True),
            sa.Column("issued_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("issued_by_name", sa.String(255), nullable=False),
            sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("quantity", sa.Float(), nullable=False),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("request_id", sa.String(36), nullable=False),
            sa.Column("request_hash", sa.String(64), nullable=False),
            sa.UniqueConstraint("company_id", "request_id", name="uq_consumable_checkout_request"),
            sa.UniqueConstraint("inventory_transaction_id", name="uq_consumable_checkout_transaction"),
            sa.CheckConstraint("quantity > 0 AND quantity <= 1000000000", name="ck_consumable_checkout_quantity"),
            sa.CheckConstraint(
                "length(request_id) = 36 AND length(request_hash) = 64", name="ck_consumable_checkout_request"
            ),
        )
    _index("ix_consumable_checkouts_company_id", ["company_id"])
    _index("ix_consumable_checkout_company_time", ["company_id", "issued_at", "id"])
    _index("ix_consumable_checkout_employee_time", ["company_id", "employee_id", "issued_at", "id"])
    _index("ix_consumable_checkout_part_time", ["company_id", "part_id", "issued_at", "id"])

    if op.get_bind().dialect.name == "postgresql":
        for statement in (
            "ALTER TABLE consumable_checkouts ENABLE ROW LEVEL SECURITY",
            "REVOKE ALL ON TABLE consumable_checkouts FROM PUBLIC",
            "REVOKE ALL ON SEQUENCE consumable_checkouts_id_seq FROM PUBLIC",
            """DO $$ BEGIN
              IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
                REVOKE ALL ON TABLE consumable_checkouts FROM anon;
                REVOKE ALL ON SEQUENCE consumable_checkouts_id_seq FROM anon;
              END IF;
              IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
                REVOKE ALL ON TABLE consumable_checkouts FROM authenticated;
                REVOKE ALL ON SEQUENCE consumable_checkouts_id_seq FROM authenticated;
              END IF;
            END $$""",
            """CREATE OR REPLACE FUNCTION prevent_consumable_checkout_change() RETURNS trigger
               LANGUAGE plpgsql SET search_path = pg_catalog AS $$ BEGIN
                 RAISE EXCEPTION 'Consumable checkout records cannot be modified or deleted';
               END $$""",
            "REVOKE ALL ON FUNCTION prevent_consumable_checkout_change() FROM PUBLIC",
            "DROP TRIGGER IF EXISTS tr_consumable_checkout_immutable ON consumable_checkouts",
            """CREATE TRIGGER tr_consumable_checkout_immutable BEFORE UPDATE OR DELETE ON consumable_checkouts
               FOR EACH ROW EXECUTE FUNCTION prevent_consumable_checkout_change()""",
        ):
            op.execute(statement)


def downgrade():
    if op.get_context().as_sql or _exists():
        op.drop_table(TABLE)
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION IF EXISTS prevent_consumable_checkout_change()")
