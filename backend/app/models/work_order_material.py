"""Work-order material allocations — the OPTIONAL tie between a work order and stock material.

An allocation is the planning row that says "this work (or this operation of it)
consumes this material part". It is what lets material deplete as work completes —
the headline case being a laser nest tied to a sheet part, consuming
``qty_per_run`` sheets per completed run.

The tie is OPT-IN and additive. A work order with no allocation rows behaves
exactly as it does today: nothing in the completion path changes, no inventory
moves, no ledger rows appear. Absence of a row is the "not tied" state — there is
no flag on ``work_orders`` to migrate and no default allocation is ever created.

Why NO ``SoftDeleteMixin`` — status is the tombstone
---------------------------------------------------
This table is *planning state*, not the compliance record. The durable,
audit-relevant facts about consumed material live in two places that this table
never replaces:

  * the ``inventory_transactions`` ledger (from 075 each consumption row carries
    ``allocation_id``, the genealogy key that survives a re-tie), and
  * the tamper-evident ``audit_log`` hash chain, written by ``AuditService`` when
    an allocation is created, changed, or untied.

So "untie" is a lifecycle transition on this row, not a deletion:
``status = CANCELLED``. Adding ``SoftDeleteMixin`` on top would create a second,
redundant tombstone (``is_deleted`` vs ``status``) that every query would have to
filter on *and* keep consistent with ``status`` — a drift bug waiting to happen,
and one that would make the partial unique indexes below ambiguous (is an
``is_deleted`` OPEN row still occupying the slot?). One tombstone, one meaning:

  * ``OPEN``      — live tie; consumption posts against it.
  * ``CANCELLED`` — untied before consumption, the work order was deleted, or a
                    nest re-import wiped the operation this row was scoped to.
  * ``CLOSED``    — **defined but NEVER WRITTEN by any code in ``app/``.** Nothing
                    closes a tie on full consumption or on work-order completion, so
                    in practice a tie ends its life ``OPEN`` or ``CANCELLED``. The
                    member exists for a later PR; do not write a reader that assumes
                    a fully-consumed tie has left ``OPEN`` — test ``qty_consumed``
                    against the ledger instead.

Rows are never physically deleted. ``CANCELLED``/``CLOSED`` rows are retained so
the ledger's ``allocation_id`` back-reference always resolves.

Uniqueness — two partial unique indexes, both company-scoped
------------------------------------------------------------
At most ONE ``OPEN`` allocation may exist per material part per scope:

  * ``uq_wo_material_alloc_open_op`` — ``(company_id, work_order_operation_id, part_id)``
    WHERE ``work_order_operation_id IS NOT NULL AND status = 'OPEN'``
    (operation-scoped tie, e.g. a laser-nest operation).
  * ``uq_wo_material_alloc_open_wo`` — ``(company_id, work_order_id, part_id)``
    WHERE ``work_order_operation_id IS NULL AND status = 'OPEN'``
    (work-order-scoped tie).

The predicates are PARTIAL on purpose: any number of ``CLOSED``/``CANCELLED``
rows may pile up for the same key, which is exactly what makes untie-then-re-tie
(and nest re-import) work without ever deleting history.

**The predicate literal is the UPPERCASE enum MEMBER NAME (``'OPEN'``), not the
lowercase ``.value``.** ``AllocationStatus`` is declared with a plain
``SQLEnum(AllocationStatus)`` — no ``values_callable`` — and SQLAlchemy persists
``enum.name`` for that form, so ``AllocationStatus.OPEN`` binds the string
``'OPEN'`` on Postgres (native enum labels ``OPEN``/``CLOSED``/``CANCELLED``) and
on SQLite (VARCHAR holding ``'OPEN'``). This matches the precedent in
``inventory.py`` (``transaction_type = 'RECEIVE'``) and in migration 056. Note the
convention is NOT uniform in this codebase: ``Part.part_type`` /
``Part.unit_of_measure`` DO pass ``values_callable`` and therefore store the
lowercase values — so a predicate written against those columns would need the
lowercase form. Get this wrong and the index silently never matches, which reads
as "uniqueness is not enforced" rather than as an error. Keep these literals in
lock-step with ``migration 074_wo_material_allocations``.

Partial-index support and the app-layer check
---------------------------------------------
Both indexes declare ``sqlite_where`` alongside ``postgresql_where``. SQLite has
supported partial indexes since 3.8.0 (verified in-env on 3.50.4 via SQLAlchemy
2.0), so dev/pytest gets the SAME semantics as production Postgres rather than a
FULL unique index — which would wrongly reject a re-tie after a ``CANCELLED`` row
exists, a divergence that would only ever show up as a mystery failure locally.

The service layer must STILL do an application-level "is there already an OPEN
allocation for this scope?" check before inserting. The index is the last line of
defense against a race, and it surfaces as an ``IntegrityError``; the app-layer
check is what turns a duplicate tie into an intelligible HTTP 409.

Quantities — the ledger is authoritative
----------------------------------------
``qty_consumed`` is a denormalized CACHE for display and for cheap "is this
fully consumed?" reads. The authoritative total is always
``SUM(inventory_transactions.quantity)`` over rows carrying this row's
``allocation_id``. Reconcile from the ledger; never treat this column as the
source of truth in a compliance answer.

``qty_per_run`` applies to operation-scoped ties only: material consumed per
completed run (e.g. sheets per nest run). It is nullable and carries NO default —
a work-order-scoped tie leaves it NULL, and a NULL on an operation-scoped row
means "not run-scaled", which readers should treat as ``1.0``
(``COALESCE(qty_per_run, 1.0)``). The service sets ``1.0`` explicitly when it
creates an operation-scoped tie.

``unit_of_measure`` is a SNAPSHOT of ``Part.unit_of_measure`` taken at tie time,
stored as the lowercase enum VALUE (``"sheets"``, ``"each"``, …) because that is
what ``Part.unit_of_measure`` persists (it uses ``values_callable``). Snapshotting
it keeps a historical allocation readable after a part's UoM is changed.

``pinned_inventory_item_id`` is a lot-directed tie: consume from THIS lot.
NULL means "pick FIFO at consume time". ``pinned_lot_number`` is denormalized
alongside it purely for display and audit legibility.

Tenancy
-------
``TenantMixin`` -> ``company_id`` Integer FK ``companies.id`` NOT NULL, indexed.
Every read MUST be company-scoped via ``tenant_query()`` / ``tenant_filter()``;
``company_id`` is the leading column of all three indexes so the scoped reads are
index-backed. DDL lives in migration ``074_wo_material_allocations``.
"""

import enum

from sqlalchemy import Column, DateTime
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func, text

from app.db.database import Base
from app.db.mixins import TenantMixin


class AllocationSource(str, enum.Enum):
    """How the tie was created."""

    NEST = "nest"  # tied to a laser-nest operation
    BOM = "bom"  # materialized from a BOM line
    MANUAL = "manual"  # ad-hoc


class AllocationStatus(str, enum.Enum):
    """Lifecycle of a tie. This IS the tombstone — see the module docstring.

    Only ``OPEN`` and ``CANCELLED`` are reachable today: nothing in ``app/`` ever
    writes ``CLOSED``, so the state machine that runs is OPEN -> CANCELLED (or the tie
    simply stays OPEN once fully consumed).
    """

    OPEN = "open"
    # RESERVED — never written by any code in app/. Kept for a later PR; a fully
    # consumed tie currently stays OPEN.
    CLOSED = "closed"
    CANCELLED = "cancelled"  # untied, work order deleted, or nest re-import wiped the op


# Native Postgres enum type names. Explicit so the migration can create/drop the
# exact same types (SQLAlchemy would otherwise derive them from the class name).
ALLOCATION_SOURCE_ENUM_NAME = "allocationsource"
ALLOCATION_STATUS_ENUM_NAME = "allocationstatus"

# The literal SQLAlchemy binds for AllocationStatus.OPEN: the UPPERCASE member
# NAME, because these columns use a plain SQLEnum (no values_callable). Shared by
# both partial-index predicates and mirrored in migration 074.
OPEN_STATUS_SQL_LITERAL = "OPEN"

_OPEN_OPERATION_PREDICATE = f"work_order_operation_id IS NOT NULL AND status = '{OPEN_STATUS_SQL_LITERAL}'"
_OPEN_WORK_ORDER_PREDICATE = f"work_order_operation_id IS NULL AND status = '{OPEN_STATUS_SQL_LITERAL}'"


class WorkOrderMaterialAllocation(Base, TenantMixin):
    """One optional tie between a work order (or one of its operations) and a material part.

    See the module docstring for the load-bearing decisions: status-as-tombstone
    (no ``SoftDeleteMixin``), the two company-scoped partial unique indexes and the
    UPPERCASE enum literal their predicates depend on, and the ledger-is-
    authoritative rule for ``qty_consumed``.
    """

    __tablename__ = "work_order_material_allocations"

    __table_args__ = (
        # At most one OPEN allocation per (company, operation, material part).
        # Declared for BOTH dialects so pytest/SQLite enforces the same rule as
        # production Postgres instead of a FULL unique index (which would reject a
        # legitimate re-tie after a CANCELLED row). Predicate literal is the
        # uppercase enum member NAME — see the module docstring.
        Index(
            "uq_wo_material_alloc_open_op",
            "company_id",
            "work_order_operation_id",
            "part_id",
            unique=True,
            postgresql_where=text(_OPEN_OPERATION_PREDICATE),
            sqlite_where=text(_OPEN_OPERATION_PREDICATE),
        ),
        # At most one OPEN allocation per (company, work order, material part) for
        # work-order-scoped ties (no operation).
        Index(
            "uq_wo_material_alloc_open_wo",
            "company_id",
            "work_order_id",
            "part_id",
            unique=True,
            postgresql_where=text(_OPEN_WORK_ORDER_PREDICATE),
            sqlite_where=text(_OPEN_WORK_ORDER_PREDICATE),
        ),
        # Plain composite for the per-WO read ("what material is this WO tied to?"),
        # which returns every status, so it must NOT be one of the partial indexes.
        Index("ix_wo_material_alloc_company_wo", "company_id", "work_order_id"),
    )

    id = Column(Integer, primary_key=True, index=True)

    work_order_id = Column(Integer, ForeignKey("work_orders.id"), nullable=False, index=True)
    # Set => the tie is OPERATION-scoped (the nest case) and consumption is driven
    # by that operation's completed runs. NULL => work-order-scoped.
    work_order_operation_id = Column(Integer, ForeignKey("work_order_operations.id"), nullable=True, index=True)
    # The MATERIAL part being consumed (a sheet, bar, hardware item …) — never the
    # part being produced.
    part_id = Column(Integer, ForeignKey("parts.id"), nullable=False, index=True)

    source = Column(SQLEnum(AllocationSource, name=ALLOCATION_SOURCE_ENUM_NAME), nullable=False)
    # Indexed: the hot read is "OPEN allocations for this scope". App-side default
    # only (no server_default) — matching the Notification.severity precedent (072).
    status = Column(
        SQLEnum(AllocationStatus, name=ALLOCATION_STATUS_ENUM_NAME),
        nullable=False,
        default=AllocationStatus.OPEN,
        index=True,
    )

    # Operation-scoped only: material per completed run (e.g. sheets/run). NULL on a
    # work-order-scoped tie; readers treat NULL as 1.0 — COALESCE(qty_per_run, 1.0).
    qty_per_run = Column(Float, nullable=True)
    qty_planned = Column(Float, nullable=False)
    # Snapshot of Part.unit_of_measure at tie time, stored as the lowercase enum
    # VALUE ("sheets"/"each"/…) because that is what Part persists.
    unit_of_measure = Column(String(20), nullable=False)
    # CACHE. The ledger (inventory_transactions.allocation_id, migration 075) is
    # authoritative; reconcile from it rather than trusting this column.
    qty_consumed = Column(Float, nullable=False, default=0.0, server_default="0")

    # Lot-directed tie: consume from THIS lot. NULL => FIFO at consume time.
    pinned_inventory_item_id = Column(Integer, ForeignKey("inventory_items.id"), nullable=True)
    # Denormalized for display/audit legibility only — never a lookup key.
    pinned_lot_number = Column(String(100), nullable=True)

    notes = Column(Text, nullable=True)

    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships — deliberately one-directional (no back_populates), so adding
    # this model changes nothing on WorkOrder / WorkOrderOperation / Part.
    work_order = relationship("WorkOrder")
    operation = relationship("WorkOrderOperation")
    part = relationship("Part")
    pinned_inventory_item = relationship("InventoryItem")
    creator = relationship("User")
