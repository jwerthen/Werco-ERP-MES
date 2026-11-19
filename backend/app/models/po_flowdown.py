"""Supplier quality flow-down on purchase orders (AS9100D 8.4.3).

AS9100D 8.4.3 requires the organization to communicate its quality requirements to external
providers. A purchase order carries them as a block printed between the lines table and the
notes: the company's standard flow-down paragraph, a reference to its supplier quality
requirements document, and the clauses selected for that order.

* ``POFlowdownProfile`` -- one row per company: whether the block prints at all, its heading,
  the controlled document it cites (``document_reference``, e.g. "SQR-100 Rev C") and the
  standard paragraph. ``version`` is the compare-and-swap counter ``PUT
  /purchasing/flowdown-profile`` checks. No row reads as "disabled" (the GET answers a default
  unsaved shape with ``version`` 0).
* ``POQualityClause`` -- the company's clause catalog. ``code`` is the business key (unique per
  company) and is immutable once created; a clause is retired with ``is_active = false``, never
  deleted, so every purchase order that cited it keeps a resolvable code.
  ``default_selected`` clauses apply to a purchase order whose own selection is NULL.

A purchase order's selection lives on ``purchase_orders.quality_clause_codes`` and the frozen
block on ``purchase_orders.flowdown_snapshot`` (``app.models.purchasing``); the freeze listener
there and the pure block builder in ``app.services.po_flowdown_service`` read these two tables.

Lock-step with migration ``128_po_flowdown``: every column, index and constraint here is created
there with the same name, and the ``after_create`` listeners below mirror its RLS / REVOKE DDL
for ``create_all`` bootstraps.
"""

from datetime import datetime

from sqlalchemy import DDL, Boolean, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, event

from app.db.database import Base
from app.db.mixins import OptimisticLockMixin, TenantMixin

DEFAULT_FLOWDOWN_HEADING = "Supplier Quality Requirements"


class POFlowdownProfile(Base, TenantMixin, OptimisticLockMixin):
    """The company's purchase-order quality flow-down settings (one row per company)."""

    __tablename__ = "po_flowdown_profiles"
    __table_args__ = (UniqueConstraint("company_id", name="uq_po_flowdown_profiles_company"),)

    id = Column(Integer, primary_key=True)
    enabled = Column(Boolean, nullable=False, default=False, server_default="false")
    heading = Column(
        String(255), nullable=False, default=DEFAULT_FLOWDOWN_HEADING, server_default=DEFAULT_FLOWDOWN_HEADING
    )
    document_reference = Column(String(100), nullable=True)
    body = Column(Text, nullable=True)
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    # Declared here, overriding ``OptimisticLockMixin.updated_at``: the mixin's quoted
    # ``server_default='now()'`` would be a fixed creation-time literal on Postgres, and migration
    # 128 creates this column without a server default. ``version`` still comes from the mixin.
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class POQualityClause(Base, TenantMixin):
    """One clause of the company's supplier quality requirements catalog."""

    __tablename__ = "po_quality_clauses"
    __table_args__ = (UniqueConstraint("company_id", "code", name="uq_po_quality_clauses_company_code"),)

    id = Column(Integer, primary_key=True)
    code = Column(String(20), nullable=False)
    title = Column(String(255), nullable=False)
    body = Column(Text, nullable=True)
    default_selected = Column(Boolean, nullable=False, default=False, server_default="false")
    is_active = Column(Boolean, nullable=False, default=True, server_default="true")
    sort_order = Column(Integer, nullable=False, default=0, server_default="0")
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


def security_sql(table: str) -> tuple:
    """Deny-by-default RLS with zero policies, mirrored from migration ``128_po_flowdown``.

    FastAPI owns authentication and tenancy (docs/SUPABASE_SECURITY.md), so neither Supabase
    client role gets a Data API path. The ``anon`` / ``authenticated`` REVOKEs sit under a
    ``pg_roles`` check because those roles exist only on Supabase, and on plain Postgres a
    statement naming a missing role aborts.
    """
    # B608 below: ``table`` is always one of this module's own table names, never input.
    return (
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"REVOKE ALL ON TABLE {table} FROM PUBLIC",
        f"REVOKE ALL ON SEQUENCE {table}_id_seq FROM PUBLIC",
        f"""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON TABLE {table} FROM anon;
        REVOKE ALL ON SEQUENCE {table}_id_seq FROM anon;
      END IF;
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL ON TABLE {table} FROM authenticated;
        REVOKE ALL ON SEQUENCE {table}_id_seq FROM authenticated;
      END IF;
    END $$""",  # nosec B608
    )


# Mirror migration 128 for model-bootstrap (create_all) installations.
for _model in (POFlowdownProfile, POQualityClause):
    for _statement in security_sql(_model.__tablename__):
        event.listen(_model.__table__, "after_create", DDL(_statement).execute_if(dialect="postgresql"))
