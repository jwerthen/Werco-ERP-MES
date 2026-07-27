"""Ledger predicates — "which ``inventory_transactions`` rows belong to a work order?".

A generic WHERE-clause helper, deliberately NOT part of the material-consumption
engine. It lives here for two reasons:

* **It is not consumption-specific.** Job costing, analytics, lot genealogy and the
  ledger list endpoint all ask the same question and must get the same answer; a
  predicate that only one feature owns is a predicate the others will re-implement
  with string literals (which is exactly what had happened).
* **Import weight.** Homed in ``material_consumption_service`` it forced
  ``completion_cost_service`` and ``analytics_service`` to import the whole
  consumption engine -- and transitively ``completion_inventory_service`` and
  ``operational_event_service`` -- just to get a WHERE clause.

The three reference shapes
--------------------------
A work order's material movement lands under THREE ``reference_type`` values, and any
reader that sees only some of them under-reports what the job consumed:

  * ``work_order``           -- the finished-good RECEIVE, plus every LEGACY (pre-PR-4.4)
    component ISSUE: the one-shot BOM backflush and work-order-scoped tie ISSUEs, which
    used to be summed into a single row. ``reference_id`` is the WORK ORDER. This is the
    shape the ``uq_wo_inventory_receipt`` / ``uq_wo_inventory_issue`` partial unique
    indexes key on, and nothing writes a component ISSUE under it any more.
  * ``work_order_backflush`` -- RECONCILED component consumption against a work order:
    BOM/routing backflush demand AND work-order-scoped material ties
    (``completion_inventory_service``). ``reference_id`` is the WORK ORDER.
  * ``work_order_operation`` -- per-run consumption of material tied to an operation
    (``material_consumption_service``); ``reference_id`` is the OPERATION.

The last two sit deliberately OUTSIDE the ``uq_wo_inventory_receipt`` /
``uq_wo_inventory_issue`` partial predicates (which key on
``reference_type = 'work_order'``), and for two DIFFERENT reasons:

  * the operation shape, so per-run consumption can never collide with the backflush's
    one-shot idempotency guard;
  * the backflush shape, because reconcile-to-target is not expressible under that
    index at all -- the leg spills across as many lots as the demand needs and posts a
    later top-up row when the target rises, so it needs N rows per (work order, part)
    where the index permits exactly one. Its idempotency is ARITHMETIC
    (``delta = target - signed ledger net``), not an index; see
    ``completion_inventory_service.backflush_components_for_work_order``.

Whatever the reason, the consequence is the same and is why this module exists: the
split has to be re-joined by every reader.

All three are company-scoped here, outer predicate and operation subquery alike
(invariant #1).
"""

from typing import Iterable, Union

from sqlalchemy import and_, or_, select
from sqlalchemy.sql.elements import ColumnElement

from app.models.inventory import InventoryTransaction
from app.models.work_order import WorkOrderOperation

# The ledger ``reference_type`` the FG receipt posts under, and the ONLY shape the
# ``uq_wo_inventory_receipt`` / ``uq_wo_inventory_issue`` partial unique indexes cover.
# ``reference_id`` = the work order. Component ISSUE rows carrying it are LEGACY
# (pre-PR-4.4) one-shot backflush / work-order-scoped tie rows; nothing writes one now,
# and ``completion_inventory_service._component_already_issued`` is kept keyed on exactly
# this shape as the permanent fence over them.
WORK_ORDER_REFERENCE_TYPE = "work_order"

# The ledger ``reference_type`` for RECONCILED component consumption against a work
# order: BOM/routing backflush demand AND work-order-scoped material ties.
# ``reference_id`` = the WORK ORDER (BOM demand has no operation --
# ``_routing_backflush_demand`` reduces whole-job quantities with ``max()``, deliberately
# discarding operation identity, so naming one would fabricate as-built attribution).
#
# Deliberately OUTSIDE the ``uq_wo_inventory_issue`` predicate: this leg reconciles to
# target and spills across lots, so it needs N rows per (work order, part) plus a later
# top-up row -- neither expressible under a one-row-per-(company, WO, ISSUE, part) unique
# index. Tie-driven rows are told apart from BOM rows by a non-NULL ``allocation_id``.
BACKFLUSH_REFERENCE_TYPE = "work_order_backflush"

# The ledger ``reference_type`` for OPERATION-scoped material consumption.
# ``reference_id`` = the operation, NOT the work order. See the module docstring for
# why it is deliberately outside the ``uq_wo_inventory_*`` predicates.
OPERATION_REFERENCE_TYPE = "work_order_operation"

# The reference shapes whose ``reference_id`` IS the work order id -- i.e. the ones
# ``work_order_ledger_filter`` can match without resolving through the operation table.
WORK_ORDER_ID_KEYED_REFERENCE_TYPES = (WORK_ORDER_REFERENCE_TYPE, BACKFLUSH_REFERENCE_TYPE)

# Every reference_type meaning "this movement belongs to a work order". Readers that
# only need a membership test (e.g. "which WO numbers touched this lot?") use this;
# readers that need id resolution use ``work_order_ledger_filter``.
WORK_ORDER_REFERENCE_TYPES = (*WORK_ORDER_ID_KEYED_REFERENCE_TYPES, OPERATION_REFERENCE_TYPE)


# The float-comparison epsilon for LEDGER QUANTITIES, homed here for the same two
# reasons the predicate above is -- and it is used alongside it, by the same readers.
#
# * **It is not consumption-specific.** Ledger quantities are ``Float`` columns, so "is
#   this quantity zero?" is never an exact ``== 0`` / ``> 0`` test: a target computed as
#   ``0.1 * 3`` is not ``0.3``, and a net that cancels to ``4e-16`` is zero in every
#   sense a human means. Lot genealogy, the consumption engine, the tie endpoints and
#   the display layer all have to answer that question the SAME way, or a tie reads as
#   consumed in one guard and squared-up in the next.
# * **Import weight.** It previously lived in ``completion_inventory_service`` (aliased
#   public as ``material_consumption_service.CONSUMPTION_EPSILON``), so a READ-ONLY
#   genealogy endpoint pulled the whole consumption engine -- transitively
#   ``completion_inventory_service`` and ``operational_event_service`` -- to get one
#   float. That is precisely the coupling this module exists to prevent, and the
#   argument does not weaken just because the thing being shared is a constant rather
#   than a WHERE clause.
#
# The services keep their own names for it (``_EPSILON`` internally,
# ``CONSUMPTION_EPSILON`` as the public alias) so call sites read in their own idiom --
# but both now resolve HERE. There must never be a second literal: the frontend's
# ``TIE_EPSILON`` is deliberately LOOSER than this and asserts ``>=`` against it in a
# parity test, which only means anything while this is the one backend definition.
LEDGER_QUANTITY_EPSILON = 1e-9


def work_order_ledger_filter(
    work_order_ids: Union[int, Iterable[int]],
    company_id: int,
) -> ColumnElement[bool]:
    """Predicate matching EVERY ledger row that belongs to the given work order(s).

    Accepts a single work-order id or any iterable of them, so a per-job cost read and
    a multi-job genealogy trace share one predicate rather than two hand-built ones.
    An EMPTY collection yields a predicate that matches nothing (rather than one that
    matches everything, which is the dangerous failure mode).

    TWO arms, THREE shapes: the first matches every work-order-id-keyed shape
    (``work_order`` and ``work_order_backflush``) directly, the second resolves
    ``work_order_operation`` rows through this work order's operations. Widening the
    first arm is the ONLY downstream reader edit PR 4.4 needed -- job costing, analytics,
    lot genealogy and ``GET /inventory/transactions?work_order_id=`` all come through
    here and all already net on ``transaction_type``.

    Shared by ``completion_cost_service``, ``analytics_service``, the traceability
    genealogy hop and ``GET /inventory/transactions?work_order_id=`` so the stored
    ``WorkOrder.actual_cost`` / ``JobCost``, the analytics variance, the as-built record
    and the ledger list can never drift apart -- before this, a nest that burned six $80
    sheets left $480 of real, ledgered, audited material cost out of all of them.
    """
    if isinstance(work_order_ids, int):
        ids = [work_order_ids]
    else:
        ids = list(work_order_ids)
    if not ids:
        # Match nothing. A bare ``in_([])`` is already false in SQLAlchemy, but being
        # explicit keeps a caller from ever reading this as "no filter".
        return InventoryTransaction.id.is_(None)

    operation_ids = select(WorkOrderOperation.id).where(
        WorkOrderOperation.company_id == company_id,
        WorkOrderOperation.work_order_id.in_(ids),
    )
    return or_(
        and_(
            InventoryTransaction.reference_type.in_(WORK_ORDER_ID_KEYED_REFERENCE_TYPES),
            InventoryTransaction.reference_id.in_(ids),
        ),
        and_(
            InventoryTransaction.reference_type == OPERATION_REFERENCE_TYPE,
            InventoryTransaction.reference_id.in_(operation_ids),
        ),
    )
