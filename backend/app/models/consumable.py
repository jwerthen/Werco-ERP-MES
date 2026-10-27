"""Immutable employee attribution for consumable inventory issues."""

from datetime import datetime, timezone

from sqlalchemy import (
    DDL,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
)

from app.db.database import Base
from app.db.mixins import TenantMixin


class ConsumableCheckout(Base, TenantMixin):
    __tablename__ = "consumable_checkouts"
    __table_args__ = (
        UniqueConstraint("company_id", "request_id", name="uq_consumable_checkout_request"),
        UniqueConstraint("inventory_transaction_id", name="uq_consumable_checkout_transaction"),
        CheckConstraint("quantity > 0 AND quantity <= 1000000000", name="ck_consumable_checkout_quantity"),
        CheckConstraint("length(request_id) = 36 AND length(request_hash) = 64", name="ck_consumable_checkout_request"),
        Index("ix_consumable_checkout_company_time", "company_id", "issued_at", "id"),
        Index("ix_consumable_checkout_employee_time", "company_id", "employee_id", "issued_at", "id"),
        Index("ix_consumable_checkout_part_time", "company_id", "part_id", "issued_at", "id"),
    )

    id = Column(Integer, primary_key=True)
    inventory_item_id = Column(Integer, ForeignKey("inventory_items.id"), nullable=False)
    inventory_transaction_id = Column(Integer, ForeignKey("inventory_transactions.id"), nullable=False)
    part_id = Column(Integer, ForeignKey("parts.id"), nullable=False)
    part_number = Column(String(100), nullable=False)
    part_name = Column(String(255), nullable=False)
    unit_of_measure = Column(String(20), nullable=False)
    location = Column(String(100), nullable=False)
    lot_number = Column(String(100), nullable=True)
    employee_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    employee_name = Column(String(255), nullable=False)
    employee_code = Column(String(50), nullable=False)
    department = Column(String(100), nullable=True)
    issued_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    issued_by_name = Column(String(255), nullable=False)
    issued_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    quantity = Column(Float, nullable=False)
    notes = Column(Text, nullable=True)
    request_id = Column(String(36), nullable=False)
    request_hash = Column(String(64), nullable=False)


@event.listens_for(ConsumableCheckout, "before_update")
@event.listens_for(ConsumableCheckout, "before_delete")
def _immutable_checkout(mapper, connection, target):
    raise ValueError("Consumable checkout records cannot be modified or deleted")


# Match the migration on create_all bootstraps. All writes go through the API.
for _statement in (
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
    """CREATE TRIGGER tr_consumable_checkout_immutable BEFORE UPDATE OR DELETE ON consumable_checkouts
       FOR EACH ROW EXECUTE FUNCTION prevent_consumable_checkout_change()""",
):
    event.listen(ConsumableCheckout.__table__, "after_create", DDL(_statement).execute_if(dialect="postgresql"))
