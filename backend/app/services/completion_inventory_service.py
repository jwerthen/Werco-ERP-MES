"""Inventory side-effects of work-order completion (Batch 6 / rank 9).

When a work order reaches ``WorkOrderStatus.COMPLETE`` the produced quantity must
become on-hand inventory (a finished-good RECEIPT), and -- when the finished part
is configured for it -- its BOM components must be consumed from stock (a
backflush ISSUE). Both legs write the same tamper-evident ``audit_log`` chain the
manual inventory endpoints use (INV-4) and reference the work order
(``reference_type='work_order'``).

As-built genealogy (INV-3 / TRACE-2 / TRACE-5) is NOT automatic from the FG-lot
trace alone: the FG-receipt RECEIVE txn carries the *finished-good* lot while the
component ISSUE txns carry the *component* lots, so a lot-keyed ``trace_lot`` of the
FG lot surfaces only the producing work order, not the consumed component lots. The
genealogy second hop -- FG lot -> producing WO (RECEIVE ``reference_id``) -> that
WO's component ISSUE txns -> consumed component part/lot/qty -- is reconstructed in
``trace_lot`` (``api/endpoints/traceability.py``), which enumerates a
``consumed_components`` section for work-order-produced lots.

Design rules (these functions are I/O-light but DB-mutating):

* **No commit.** Every function joins the CALLER's unit of work; the completion
  handler owns ``db.commit()`` so the inventory writes land ATOMICALLY with the
  status change on the live paths. (The reconcile-on-read caller commits too, but
  best-effort -- see the read-safe wrapper there.)
* **Idempotent**, but by two different mechanisms:
    - FG receipt: EXISTENCE-keyed. ANY ``RECEIVE`` txn with
      ``reference_type='work_order', reference_id=work_order.id, company_id`` -> already
      received, no-op. Backed by ``uq_wo_inventory_receipt`` (migration 041/076).
    - Component consumption: ARITHMETIC. Each demand source reconciles to target against
      the signed ledger net of its OWN history
      (``delta = target - net``; post only when ``delta > 0``, never a reversal), under
      ``reference_type='work_order_backflush'`` -- a shape NO unique index covers,
      because reconcile-to-target needs N rows per (work order, part) plus a later
      top-up row. What makes the arithmetic valid is that every entry into this seam is
      already serialized on the ``work_orders`` row; see
      ``backflush_components_for_work_order``.
    - The legacy ``('work_order', ISSUE)`` shape is no longer WRITTEN, and
      ``_component_already_issued`` is kept keyed on it exactly as it was, as a permanent
      fence: a work order carrying one of those pre-PR-4.4 summed rows is fenced out of
      the reconciling engine entirely, so no historical row is re-keyed, re-interpreted
      or backfilled.
* **Tenant-scoped.** ``company_id`` is stamped on every row and every lookup filters
  it (invariant #1). The caller passes the ACTIVE company.
* **Audited.** Each new ``InventoryTransaction`` is logged via ``AuditService``
  (mirrors ``receiving.py`` / ``inventory.py``) so stock movement lands on the hash
  chain, not just the AI ``OperationalEvent`` store.

Lot-only (no serialization flag exists yet): on FG receipt we assign
``work_order.lot_number`` if empty (a per-company-unique lot derived from the WO
number) and leave ``InventoryItem.serial_number`` NULL. Serial assignment is a
tracked follow-up.

**The BOM/routing backflush leg IS NO LONGER DARK. PR 4.5 gave it a door.**
``Part.backflush_components`` is writable from ``PUT /parts/{id}`` and
``PUT /materials/{id}`` (``PartUpdate``, behind ``assert_backflush_change_allowed``'s
refusal gate) and from the part-detail UI, so ``_resolve_backflush_components`` and
everything it calls CAN now execute against production data on any part a supervisor has
armed. Through PR 4.4 the column had no writer anywhere in ``app/``, which is why the leg
shipped treating a whole-job component quantity as a per-unit rate, summing one
component's demand once per operation that touched it, letting one routed operation
cancel an entire BOM explosion, consuming both a sub-assembly and its raw material, and
reading neither ``is_alternate`` nor ``is_optional`` nor reference lines. Those are fixed;
the LICENCE that history granted -- "change it freely, nothing runs it" -- **expired with
PR 4.5**. Treat every change here as a change to live, stock-moving code.

What remains of that history is a POSTURE, not a permission: the leg still refuses rather
than guesses. Every ``blocking`` diagnostic the resolver raises now REFUSES the demand it
describes (the component, or the whole leg when the condition is structural) and writes a
``BACKFLUSH_DEMAND_REFUSED`` chain row saying so -- see ``_resolve_backflush_components``.

``_issue_one_component`` is the OPPOSITE case -- work-order-scoped material ties drive it
today, so its behavior is LIVE. PR 4.4 changed it deliberately and with that in mind: it
now shares ``material_consumption_service.consumable_source_items`` with the per-run
engine instead of taking the lowest-id active row with no ``status`` predicate. Two
engines naming different heats for one physical draw is an AS9100D 8.5.2 problem, and
the shared predicate is NULL-status-tolerant (``COALESCE(status, 'available')``), so the
widening cannot hide legacy stock the way a bare ``status = 'available'`` would have.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Container, Iterable, Optional, TypeVar, cast

from sqlalchemy import and_, case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError
from sqlalchemy.sql.elements import ColumnElement

from app.db.ledger_filter import (
    BACKFLUSH_REFERENCE_TYPE,
    LEDGER_QUANTITY_EPSILON,
    OPERATION_REFERENCE_TYPE,
    WORK_ORDER_REFERENCE_TYPE,
)
from app.models.bom import BOM, BOMItem, BOMItemType, BOMLineType
from app.models.inventory import InventoryItem, InventoryTransaction, TransactionType
from app.models.operational_event import OperationalEvent  # noqa: F401  (imported for type/test discoverability)
from app.models.part import Part, uom_disagrees, uom_label
from app.models.shipping import Shipment, ShipmentStatus
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation
from app.models.work_order_material import WorkOrderMaterialAllocation
from app.services.audit_service import AuditService
from app.services.operational_event_service import OperationalEventService
from app.services.work_order_state_service import is_laser_dispatch_work_order

logger = logging.getLogger(__name__)

# Tamper-evident audit action + operational-event type for a backflush shortage
# (a component driven negative on-hand). A silent negative stock is a material-trail
# control gap in a regulated (AS9100D/CMMC-L2) system, so a shortage is recorded on
# the hash chain AND emitted as a warning OperationalEvent (item 3).
BACKFLUSH_SHORTAGE_AUDIT_ACTION = "BACKFLUSH_SHORTAGE"
BACKFLUSH_SHORTAGE_EVENT_TYPE = "backflush_shortage"

# Tamper-evident audit action for a BOM/routing backflush demand line that was SUPPRESSED
# because the ledger already shows that part leaving stock against this work order under
# the OPERATION reference shape (tied consumption). Recorded rather than left silent: the
# suppression is the system declining to issue material a planner's BOM asked for, and
# "correct but invisible" is exactly the shape of control gap an AS9100D 8.5.2 as-built
# review cannot reconstruct after the fact. See ``_drop_ledger_covered_parts``.
BACKFLUSH_DOUBLE_ISSUE_BLOCKED_AUDIT_ACTION = "BACKFLUSH_DOUBLE_ISSUE_BLOCKED"

# Tamper-evident audit action for a BLOCKING demand diagnostic raised on the COMPLETION
# path -- i.e. the resolver worked out that this BOM/routing cannot be resolved cleanly
# for a part somebody armed, and refused the affected demand rather than issuing a figure
# it does not trust. Distinct from ``BACKFLUSH_DOUBLE_ISSUE_BLOCKED`` on purpose: that one
# means "the material already left, correctly, by another route", this one means "the
# demand is WRONG and no material left at all".
#
# Before PR 4.5 these diagnostics were computed at completion and DISCARDED -- eight of
# them without even a log line -- so a BOM edited after a part was armed (a supervisor's
# opt-in is checked once, at the flip, and every input it reads is mutable afterwards by
# anyone with ``boms:edit``) moved material against demand the system itself had judged
# untrustworthy, leaving no production trace but an ordinary-looking ledger row. That is
# the "correct but invisible" gap ``_record_ledger_suppression`` and
# ``_record_backflush_demand_suppressed`` already exist to close for SUPPRESSION.
BACKFLUSH_DEMAND_REFUSED_AUDIT_ACTION = "BACKFLUSH_DEMAND_REFUSED"

# ...and its operational-event type, for the reason written out at
# ``BACKFLUSH_COMPONENT_FAILED_EVENT_TYPE`` below, which describes this action verbatim: a
# degraded path must not be QUIETER than the lesser condition it degrades from. A shortage
# still moves material and emits ``backflush_shortage`` -> ``material.backflush_shortage``
# into Purchasing's inbox; a REFUSAL moves nothing at all, which is the worse material-trail
# gap, and an audit row nobody is watching is not a control.
#
# It is not self-limiting either. The refusal happens at COMPLETION, on a part that is
# ALREADY armed, and nothing here disarms it -- so absent a notification the same component
# silently under-issues on every subsequent job, with the BOM defect that caused it
# untouched. (The opt-in gate cannot help: it runs once, at the flip, and every input it
# reads stays editable by anyone with ``boms:edit``.) Catalog entry:
# ``material.backflush_demand_refused``, sibling of ``material.backflush_failed``.
BACKFLUSH_DEMAND_REFUSED_EVENT_TYPE = "backflush_demand_refused"

# Tamper-evident audit action for a component whose consumption RAISED and was rolled
# back to its own savepoint. The exact twin of the tie engine's
# ``ALLOCATION_CONSUMPTION_FAILED``, and required for the same reason: material that
# should have depleted and did not is a material-trail control gap strictly worse than a
# shortage, which already writes a chain row. It is also what keeps a reconcile-on-read
# GET alive -- the ``quantity_on_hand`` UPDATE in ``_post_stock_movement_txn`` sits
# OUTSIDE ``_insert_txn_with_savepoint``'s guard, so a driven-negative draw hitting
# ``chk_inventory_items_quantity_non_negative`` (if that constraint is live) would
# otherwise poison the session and turn the GET into a ``PendingRollbackError`` 500.
BACKFLUSH_COMPONENT_FAILED_AUDIT_ACTION = "BACKFLUSH_COMPONENT_FAILED"

# ...and its operational-event type, so the failure reaches the SAME people the shortage
# reaches. Without it the degraded path is strictly quieter than the lesser condition:
# a shortage emits ``backflush_shortage`` -> ``material.backflush_shortage`` and lands in
# Purchasing's inbox, while "the draw raised and was rolled back, so NOTHING was
# consumed" produced an audit row nobody is watching and no notification at all. That gap
# is not hypothetical -- it is exactly what a live
# ``chk_inventory_items_quantity_non_negative`` turns every shortage into.
BACKFLUSH_COMPONENT_FAILED_EVENT_TYPE = "backflush_component_failed"

# Tamper-evident audit actions + operational-event types for ship-side discrepancies
# (G2). A ship with no matching finished-good lot row (receipt skipped / lot changed /
# stock already moved) and a ship that drives cumulative shipped beyond what was
# produced are both regulated material-trail control gaps. Each is recorded on the hash
# chain AND emitted as a warning OperationalEvent -- but NEITHER fails the ship/close
# (mirrors the warn-and-record backflush-shortage posture).
SHIP_FG_MISSING_AUDIT_ACTION = "SHIP_FG_LOT_MISSING"
SHIP_FG_MISSING_EVENT_TYPE = "ship_fg_lot_missing"
OVER_SHIP_AUDIT_ACTION = "OVER_SHIP"
OVER_SHIP_EVENT_TYPE = "over_ship"

# ONE float-comparison epsilon for the whole completion-inventory surface (this module
# and ``material_consumption_service``, which re-exports it as ``CONSUMPTION_EPSILON``).
# Quantities are ``Float`` columns, so "is this quantity zero?" must never be an exact
# ``== 0`` / ``> 0`` test: a target computed as ``0.1 * 3`` is not ``0.3``. Previously
# three different spellings were in use -- a bare ``1e-9`` here, ``_EPSILON`` in the
# consumption engine, and plain ``<= 0`` / ``> 0`` on the tie legs -- so the same
# near-zero delta could be "zero" in one guard and "positive" in the next.
#
# The DEFINITION now lives in ``app/db/ledger_filter.py``, alongside the ledger predicate
# it is used with. This module keeps the private name so its own call sites read
# unchanged, but it is an alias, not a source: a read-only genealogy endpoint should not
# have to import the consumption engine to learn what "zero" means.
_EPSILON = LEDGER_QUANTITY_EPSILON


def _insert_txn_with_savepoint(db: Session, txn: InventoryTransaction) -> bool:
    """Insert one ``InventoryTransaction`` inside a SAVEPOINT, returning success.

    The work-order RECEIVE / ISSUE keys carry a partial UNIQUE index (added by the
    migration specialist); a concurrent second insert of the same key (the
    double-receive / double-issue race) raises ``IntegrityError`` on flush. We wrap
    the INSERT in ``db.begin_nested()`` so that on collision we roll back ONLY the
    savepoint (not the outer completion / reconcile unit of work) and treat it as a
    clean no-op -- the other transaction already wrote the row.

    Returns ``True`` when the row was actually inserted (caller may now mutate the
    on-hand quantity), ``False`` when it was a duplicate no-op (caller must NOT
    mutate on-hand, or it would double-count against the winning transaction's row).

    Crucially this keeps the OUTER transaction usable on BOTH paths: the live paths
    stay atomic with the completion, and on the reconcile path a duplicate insert
    can never abort the whole reconcile (only the savepoint is rolled back, so the
    status transition still commits).
    """
    nested = db.begin_nested()
    try:
        db.add(txn)
        db.flush()
    except IntegrityError:
        nested.rollback()
        return False
    return True


# Finished-goods receipt location. A module constant (rather than a Part field) so it
# is configurable in one place; the warehouse mirrors InventoryItem's MAIN default.
FINISHED_GOODS_WAREHOUSE = "MAIN"
FINISHED_GOODS_LOCATION = "FINISHED-GOODS"


@dataclass
class ComponentShortage:
    """A backflush ISSUE that drove (or would have driven) a source lot negative.

    Recorded but NOT fatal: a shortage must never fail a completion (negative
    on-hand is the existing system's behavior -- the manual ``/inventory/adjust``
    path also permits it). Surfaced so the caller can log / report it.
    """

    part_id: int
    part_number: Optional[str]
    required_quantity: float
    available_quantity: float
    shortfall: float


@dataclass
class IssueOutcome:
    """What one ``_issue_one_component`` call actually did.

    ``posted_any`` distinguishes "at least one ISSUE row landed" from "nothing was
    written" -- the caller must not advance a tie's ``qty_consumed`` cache on the second,
    since that cache is the exact field the untie endpoint refuses against (409 once
    ``qty_consumed > 0``). Named for its operation-scoped twin in
    ``_consume_one_allocation``, which has always made the same distinction.

    It deliberately carries NO quantity. The leg posts N rows now, so the obvious
    ``quantity_posted`` companion field looks like the natural addition -- but the one
    consumer that needs a number, ``_advance_tie_consumed``, re-reads the signed LEDGER
    net instead, on purpose: the cache it writes must equal what the ledger holds, and a
    figure accumulated in Python would be a second, drifting source for the same fact.
    A field nothing reads is a contract nothing keeps.
    """

    posted_any: bool = False
    shortage: Optional[ComponentShortage] = None


@dataclass
class BackflushResult:
    issued_part_ids: list[int] = field(default_factory=list)
    shortages: list[ComponentShortage] = field(default_factory=list)


# ``WORK_ORDER_REFERENCE_TYPE`` under the two names that say what the literal MEANS at
# the two single-shape predicates below. Both must stay NARROW, and that is the whole
# reason for naming them: since PR 4.4 the module-level constant a reader reaches for is
# ``WORK_ORDER_ID_KEYED_REFERENCE_TYPES`` -- a TUPLE that also covers
# ``work_order_backflush`` -- so "helpfully" widening either predicate to it is a one-word
# edit that looks like a consistency fix and is a live defect:
#
# * ``LEGACY_COMPONENT_ISSUE_REFERENCE_TYPE`` -- ``_component_already_issued``'s fence.
#   Widened, the reconciling leg's OWN rows would fence its work order out of the engine
#   from its first post onward, permanently, and emit a spurious suppression row saying so.
# * ``FINISHED_GOOD_RECEIPT_REFERENCE_TYPE`` -- ``_existing_work_order_receipt``'s
#   idempotency key, which must mirror ``uq_wo_inventory_receipt``'s partial predicate
#   EXACTLY; that index keys on ``reference_type = 'work_order'`` alone.
LEGACY_COMPONENT_ISSUE_REFERENCE_TYPE = WORK_ORDER_REFERENCE_TYPE
FINISHED_GOOD_RECEIPT_REFERENCE_TYPE = WORK_ORDER_REFERENCE_TYPE


def _existing_work_order_receipt(db: Session, work_order_id: int, company_id: int) -> bool:
    """True if a finished-good RECEIVE for this WO already exists (idempotency key)."""
    return (
        db.query(InventoryTransaction.id)
        .filter(
            InventoryTransaction.company_id == company_id,
            InventoryTransaction.reference_type == FINISHED_GOOD_RECEIPT_REFERENCE_TYPE,
            InventoryTransaction.reference_id == work_order_id,
            InventoryTransaction.transaction_type == TransactionType.RECEIVE,
        )
        .first()
        is not None
    )


def _component_already_issued(db: Session, work_order_id: int, component_part_id: int, company_id: int) -> bool:
    """True if a LEGACY one-shot ISSUE row exists for this (work order, component).

    **The predicate is deliberately unchanged, and that is the whole mechanism.** It
    still reads ``LEGACY_COMPONENT_ISSUE_REFERENCE_TYPE`` (``'work_order'``) ``AND
    transaction_type='ISSUE'`` -- named rather than spelled so the literal can never be
    widened to the now-plural ``WORK_ORDER_ID_KEYED_REFERENCE_TYPES`` -- which since
    PR 4.4 nothing writes -- the reconciling leg posts ``work_order_backflush``. So this
    now matches ONLY rows written before that change, and it is consulted on BOTH legs of
    ``backflush_components_for_work_order`` as a permanent fence: a work order carrying
    one of the old summed rows is kept out of the new engine entirely.

    That is what makes PR 4.4 correct-forward with NO backfill. No historical ledger row
    is rewritten, re-keyed or reinterpreted; legacy work orders keep exactly the
    behaviour they have, forever. It is also what makes the deploy order-free: a v-old
    instance writes the legacy shape (which v-new fences) and a v-new instance writes the
    new shape (which v-old cannot see, and cannot reach, since a completed work order is
    never re-entered).

    Applying it to the TIE leg as well is not belt-and-braces. A legacy *summed* row
    carries the FIRST tie's ``allocation_id`` over a quantity that also covered BOM
    demand and any second tie, so a second tie on that part has zero ledger rows of its
    own, nets to 0, and would re-issue material the summed row already took.
    """
    return (
        db.query(InventoryTransaction.id)
        .filter(
            InventoryTransaction.company_id == company_id,
            InventoryTransaction.reference_type == LEGACY_COMPONENT_ISSUE_REFERENCE_TYPE,
            InventoryTransaction.reference_id == work_order_id,
            InventoryTransaction.transaction_type == TransactionType.ISSUE,
            InventoryTransaction.part_id == component_part_id,
        )
        .first()
        is not None
    )


def _assign_finished_good_lot(db: Session, work_order: WorkOrder, company_id: int) -> str:
    """Return ``work_order.lot_number``, assigning a per-company-unique lot if empty.

    The lot is derived from the WO number (``LOT-<wo_number>``) and de-collided with
    a ``-NN`` suffix within the company if a same-named lot already exists on an
    InventoryItem. Idempotent: an already-assigned lot is returned untouched.
    """
    if work_order.lot_number:
        return work_order.lot_number

    base = f"LOT-{work_order.work_order_number}"
    candidate = base
    suffix = 1
    while (
        db.query(InventoryItem.id)
        .filter(InventoryItem.company_id == company_id, InventoryItem.lot_number == candidate)
        .first()
        is not None
    ):
        suffix += 1
        candidate = f"{base}-{suffix}"
    work_order.lot_number = candidate
    return candidate


def receive_finished_goods_for_work_order(
    db: Session,
    work_order: WorkOrder,
    *,
    user_id: int,
    company_id: int,
    audit: AuditService,
) -> Optional[InventoryTransaction]:
    """Receive a completed WO's output into finished-goods inventory (INV-1 / TRACE-3).

    Creates or increments an ``InventoryItem`` for ``work_order.part_id`` at the FG
    location and writes a positive ``RECEIVE`` ``InventoryTransaction`` referencing
    the work order, with the assigned finished-good lot and ``standard_cost`` unit
    cost. Idempotent (skips if a WO RECEIVE already exists) so reconcile re-entry /
    re-completion can't double-receive. Does NOT commit -- the caller owns the
    transaction so the receipt is atomic with the completion.

    Returns the created transaction, or ``None`` when it was a no-op (already
    received, laser nest-dispatch WO, or nothing to receive).
    """
    if is_laser_dispatch_work_order(work_order):
        # A laser nest-dispatch WO is a DISPATCH POOL, not a unit of product: its
        # ``quantity_complete`` counts pooled nest RUNS, and on a PARENTED child its
        # ``part_id`` is the PARENT assembly's part (work_orders.py,
        # ``_ensure_laser_child_work_order``). Receiving here would mint phantom
        # FINISHED_GOODS of the parent part (5 nests x 8 runs = 40 phantom units) that
        # the parent's own completion later books for real. Part-less STANDALONE nest
        # WOs are the same shape with no part at all. Expected behavior -> debug, not
        # a warning.
        logger.debug(
            "FG receipt skipped for laser nest-dispatch WO %s (company %s): nest runs are not finished goods",
            work_order.id,
            company_id,
        )
        return None

    if _existing_work_order_receipt(db, work_order.id, company_id):
        return None

    quantity = float(work_order.quantity_complete or 0)
    if quantity <= 0:
        # Nothing produced (e.g. fully scrapped) -- no finished good to receive.
        return None

    part = db.query(Part).filter(Part.id == work_order.part_id, Part.company_id == company_id).first()
    if part is None:
        # With the laser-dispatch skip above, a part-less WO no longer reaches here;
        # a non-null part_id that resolves to nothing is still an anomaly worth a log
        # line, but not an alarm -- info, per the laser-nest-flow/info finding.
        logger.info(
            "FG receipt skipped: part %s not found for WO %s (company %s)",
            work_order.part_id,
            work_order.id,
            company_id,
        )
        return None

    lot_number = _assign_finished_good_lot(db, work_order, company_id)
    unit_cost = float(part.standard_cost or 0)

    # Match on part + location + lot + company (create if none) so a re-run after a
    # partial completion would aggregate onto the same FG lot row.
    inv_item = (
        db.query(InventoryItem)
        .filter(
            InventoryItem.company_id == company_id,
            InventoryItem.part_id == work_order.part_id,
            InventoryItem.location == FINISHED_GOODS_LOCATION,
            InventoryItem.lot_number == lot_number,
        )
        .first()
    )

    # Order matters (item 1 / BLOCKER companion): create/flush the InventoryItem row
    # (at its EXISTING quantity -- no increment yet) so the RECEIVE txn has an
    # ``inventory_item_id`` to reference, then insert the txn FIRST under a savepoint.
    # Only if that insert actually committed to the savepoint do we increment on-hand;
    # a duplicate (already-received race the unique index catches) is a clean no-op and
    # must NOT mutate the quantity, or it would double the on-hand vs. the winner's row.
    old_quantity_on_hand = inv_item.quantity_on_hand if inv_item else None
    if inv_item is None:
        inv_item = InventoryItem(
            part_id=work_order.part_id,
            location=FINISHED_GOODS_LOCATION,
            warehouse=FINISHED_GOODS_WAREHOUSE,
            quantity_on_hand=0.0,
            quantity_allocated=0.0,
            quantity_available=0.0,
            lot_number=lot_number,
            unit_cost=unit_cost,
            received_date=datetime.utcnow(),
            status="available",
        )
        inv_item.company_id = company_id
        db.add(inv_item)
        db.flush()

    txn = InventoryTransaction(
        company_id=company_id,
        inventory_item_id=inv_item.id,
        part_id=work_order.part_id,
        transaction_type=TransactionType.RECEIVE,
        quantity=quantity,
        to_location=FINISHED_GOODS_LOCATION,
        lot_number=lot_number,
        # Named, not spelled: this WRITE and ``_existing_work_order_receipt``'s read must
        # match ``uq_wo_inventory_receipt``'s partial predicate and each other exactly.
        reference_type=FINISHED_GOOD_RECEIPT_REFERENCE_TYPE,
        reference_id=work_order.id,
        reference_number=work_order.work_order_number,
        unit_cost=unit_cost,
        total_cost=quantity * unit_cost,
        notes=f"Finished-goods receipt from work order {work_order.work_order_number}",
        created_by=user_id,
    )
    if not _insert_txn_with_savepoint(db, txn):
        # A concurrent RECEIVE already wrote this WO's finished-good receipt (the unique
        # index fired). Treat as an idempotent no-op: do NOT increment on-hand (the
        # winning txn's row owns the quantity) and leave any freshly-created empty item
        # row at zero. The outer transaction stays usable on both live and reconcile.
        return None

    # The RECEIVE insert succeeded -> NOW apply the on-hand increment.
    inv_item.quantity_on_hand = float(inv_item.quantity_on_hand or 0) + quantity
    inv_item.quantity_available = inv_item.quantity_on_hand - float(inv_item.quantity_allocated or 0)
    if not inv_item.unit_cost:
        inv_item.unit_cost = unit_cost
    db.flush()

    # Tamper-evident audit (hash chain) for the stock movement (INV-4). Flushed inside
    # the caller's unit of work so the audit row commits with the inventory write.
    audit.log_create(
        "inventory",
        txn.id,
        str(txn.id),
        new_values=txn,
        description=(
            f"Received {quantity} of part {part.part_number} into {FINISHED_GOODS_LOCATION} "
            f"lot {lot_number} from work order {work_order.work_order_number}"
        ),
    )
    if old_quantity_on_hand is not None:
        audit.log_update(
            "inventory",
            inv_item.id,
            f"{part.part_number} @ {FINISHED_GOODS_LOCATION}",
            old_values={"quantity_on_hand": old_quantity_on_hand},
            new_values={"quantity_on_hand": inv_item.quantity_on_hand},
            description=f"FG receipt: stock for part {part.part_number} at {FINISHED_GOODS_LOCATION}",
        )

    return txn


def _open_allocations(
    db: Session,
    work_order: WorkOrder,
    company_id: int,
    allocations: Optional[list[WorkOrderMaterialAllocation]],
) -> list[WorkOrderMaterialAllocation]:
    """The OPEN ties on this work order -- the caller's pre-fetched list, or one SELECT.

    ``apply_completion_inventory_effects`` reads them ONCE and threads the list through
    the consume leg, the backflush-precedence drop and the backflush's work-order-scoped
    tie leg, which used to issue the same tenant-scoped SELECT up to three times per
    completion. The parameter stays optional so the individual legs remain independently
    callable (tests and any future call site) without a behavior change.

    Threading the list is ALSO what lets ``backflush_components_for_work_order`` answer
    "is this work order untied?" with zero queries, which is invariant 6(d)'s guarantee.
    """
    if allocations is not None:
        return allocations
    from app.services.material_consumption_service import open_allocations_for_work_order

    return open_allocations_for_work_order(db, work_order.id, company_id)


# ---------------------------------------------------------------- diagnostics
# The resolver has ~14 conditions on which it "cannot answer cleanly". Every one of them
# was a ``logger.warning`` at best and completely SILENT at worst, with no return channel
# of any kind -- the resolver's type was a bare ``dict[int, float]``, so a caller could
# not tell "this BOM resolves cleanly" from "this BOM resolves to a number nobody should
# trust". That was survivable only while the leg was dark. Now that
# ``Part.backflush_components`` is settable (PR 4.5), those conditions are the difference
# between material moving correctly and material moving wrongly and permanently into an
# as-built record (AS9100D 8.5.2), so they carry the channel a REFUSAL is built on.
#
# **``severity`` IS LOAD-BEARING ON THE WRITE PATH.** A ``blocking`` diagnostic REFUSES the
# demand it describes at COMPLETION -- the component, or the whole leg when the condition
# is structural -- and writes a ``BACKFLUSH_DEMAND_REFUSED`` chain row saying so. Three
# readers act on these, and all three must keep agreeing:
#
#   * ``backflush_readiness_for_part`` -- refuses the part-level opt-in (409) over any
#     blocking diagnostic in the part's own BOM explosion;
#   * ``_refuse_blocked_demand`` (the COMPLETION path, via ``_resolve_backflush_components``)
#     -- drops the condemned demand and records each blocker;
#   * ``preview_backflush_for_work_order`` -- mirrors that refusal through the PURE
#     ``blocked_demand_refusal`` as ``suppression_reason='blocking_diagnostic'``, so the
#     dry run states the answer the completion will act on without writing anything.
#
# What is still ADDITIVE is the RESOLVER itself: every existing log line stays where it
# was, and ``_resolve_backflush_demand`` never drops demand over a diagnostic -- a pure
# read that quietly returned a different answer than it reported would be the worst of
# both. The refusal lives one layer up, in the write-path wrapper. Adding a diagnostic
# here, or promoting one to ``blocking``, therefore CHANGES WHAT MOVES OUT OF STOCK on
# every armed part; it is not a reporting-only edit.
BACKFLUSH_BLOCKING = "blocking"
BACKFLUSH_ADVISORY = "advisory"

# Mirrors ``api/endpoints/bom.py``'s ``explode_bom_recursive(max_levels=20)``. The
# visited-set cycle guard alone does NOT bound recursion depth -- it bounds repetition of
# a PART, and a legitimately deep (or maliciously wide) structure can still exhaust the
# stack. A ``RecursionError`` raised here would be swallowed whole by the
# ``except Exception: pass`` at the two reconcile-on-read call sites (``shop_floor.py`` /
# ``work_orders.py``), silently losing the ENTIRE completion's inventory effects -- FG
# receipt included -- with nothing but a log line. A cap that emits a diagnostic degrades
# to "this part cannot opt in" instead.
_MAX_BOM_LEVELS = 20


@dataclass
class _BackflushDiagnostic:
    """One thing the demand resolver could not answer cleanly, and what it means.

    ``severity`` is the whole point of the type:

    * ``blocking`` -- the resolved demand for this component is WRONG or absent, and no
      caller should act on it. These are the sentences the part-level refusal gate joins
      into its 409, so each ``detail`` is written to follow that template exactly: what is
      wrong (naming the BOM line or operation) then what to change, in a form that reads
      correctly after ``"Part {pn} cannot enable automatic backflush: "``.
    * ``advisory`` -- the demand is usable but something about it is worth a human's
      attention, or the condition is WORK-ORDER-scoped and therefore cannot be gated at
      part opt-in at all (a zero ordered quantity, a non-COMPLETE operation contributing
      demand, a tie whose basis disagrees with the BOM's).

    Every field except ``code``/``severity``/``detail`` is optional context for the UI to
    link back to the row that produced it; none of them is load-bearing.
    """

    code: str
    severity: str
    detail: str
    bom_item_id: Optional[int] = None
    component_part_id: Optional[int] = None
    component_part_number: Optional[str] = None
    operation_id: Optional[int] = None


def backflush_refusal_sentence(part_number: Optional[str], diagnostic: _BackflushDiagnostic) -> str:
    """One blocking diagnostic as the sentence the refusal gate shows the operator.

    ONE template for every blocker (``docs`` and the recon brief both state it):
    ``Part {part_number} cannot enable automatic backflush: {what is wrong}. {what to
    change}.`` The second half lives in ``_BackflushDiagnostic.detail``; this adds the
    subject so a joined string of N blockers still names the part on every sentence
    rather than only the first.
    """
    return f"Part {part_number or '?'} cannot enable automatic backflush: {diagnostic.detail}"


@dataclass
class _BackflushBomExplosion:
    """What the BOM explosion says about a work order's component demand.

    ``demand`` is the quantity to ISSUE per part. ``excluded_part_ids`` is every part the
    explosion walked past WITHOUT giving it demand -- an alternate/optional/reference
    line, a phantom that was exploded through, or any part inside a ``make``
    sub-assembly's own subtree. That set is not bookkeeping: the routing leg's
    ``component_part_id`` values are generated from ``_collect_bom_components``, which
    applies NONE of these rules and DOES recurse into ``make``, so without the exclusion
    set the routing would re-introduce through the back door exactly the lines the
    explosion just declined to issue.

    ``diagnostics`` is the channel the walk previously did not have: conditions the
    explosion had to make a judgement call about, recorded so a refusal gate can read
    them. It never affects ``demand``.
    """

    demand: dict[int, float] = field(default_factory=dict)
    excluded_part_ids: set[int] = field(default_factory=set)
    diagnostics: list[_BackflushDiagnostic] = field(default_factory=list)


def _is_non_consumed_bom_line(item: BOMItem) -> bool:
    """True when a BOM line states demand that a backflush must NOT issue.

    Three families, all of which the backflush read NOWHERE before this and all of which
    fail in the expensive direction -- material issued into an as-built record that the
    job never consumed (AS9100D 8.5.2):

    * **``is_alternate``** -- a member of an alternate group. The group is an OR, not an
      AND: issuing every member multiplies the group's demand by its size. ``mrp_service``
      has always skipped alternates (``explode_bom_for_mrp``); the backflush now agrees,
      so planning and consumption cannot state different demand for one BOM.
    * **``is_optional``** -- present on some units and not others, with nothing on the
      work order recording which. Nothing here can know, so it issues nothing.
    * **``line_type == reference``** -- documentation and tooling. The enum's own comment
      is "Reference only - not consumed".

    Under-issuing is the safe direction and is not silent: the material is still on the
    shelf, so the operator who needs it draws it manually, and the job's cost shows the
    gap. Over-issuing writes material into a genealogy record that never contained it,
    which no downstream reader can distinguish from the truth.
    """
    if bool(item.is_alternate) or bool(item.is_optional):
        return True
    return (item.line_type or "").lower() == BOMLineType.REFERENCE.value


def bom_line_is_backflush_consumed(item: BOMItem) -> bool:
    """Public inverse of ``_is_non_consumed_bom_line``, for readers outside this module.

    ``GET /bom/uom-mismatches`` reports it per row as ``blocks_backflush``: a line the
    backflush would never issue also never raises a per-line diagnostic, so listing it
    without that distinction would send someone to fix a line that refuses nothing.

    It answers the LINE half only. Whether a consuming explosion ever REACHES the line is
    the other half and depends on which part is being armed (``make`` sub-assembly
    subtrees are walked exclude-only, phantoms open up), which no flat cross-BOM listing
    can model. See ``_explode_backflush_bom``.
    """
    return not _is_non_consumed_bom_line(item)


def _uom_label(value) -> str:
    """A ``unit_of_measure`` (enum, enum value or raw string) as a comparable lowercase str.

    Module-local alias for ``models.part.uom_label``. The normalisation moved down to the
    model so the BLOCKING ``unit_of_measure_mismatch`` diagnostic below and the
    ``GET /bom/uom-mismatches`` remediation report cannot drift apart -- a report that
    listed a different set of rows than the gate refuses would send people to fix lines
    that never blocked, or hide lines that still do.
    """
    return uom_label(value)


def _disclosable_component_id(component_part_id: Optional[int], resolved_part_ids: Container[int]) -> Optional[int]:
    """The component id a diagnostic may carry: the id, or ``None`` if it is not ours.

    ONE rule for every diagnostic site, because the surface has to read the same way
    everywhere. ``missing_component_part`` already withholds ``component_part_id`` -- the
    id did not resolve to a part in this company, and "hard-deleted", "never existed" and
    "another tenant's" are three states this side of the fence must not distinguish -- but
    ``alternate_group_without_primary`` and the three routing diagnostics were built from
    a raw FK (``BOMItem.component_part_id`` / ``WorkOrderOperation.component_part_id``),
    which ``bom.py``'s unscoped add-line validator lets a client point anywhere. The
    asymmetry was not a disclosure (it is the tenant's own column value, already readable
    through ``GET /bom/{id}``) but it was an asymmetry, and these sentences are served to
    every authenticated tenant user by ``GET /parts/{id}/backflush-readiness`` and the
    refusal 409. Resolve-or-withhold, uniformly.

    **Withholding has a REFUSAL consequence, and it is the intended one.** Scope in
    ``blocked_demand_refusal`` keys on ``component_part_id is None``, so a withheld id puts
    that diagnostic in the STRUCTURAL tier and refuses the whole backflush leg rather than
    one component -- which is exactly ``missing_component_part``'s own documented rationale
    ("a line's demand vanished and nothing can say whose"). We cannot name the component,
    so we cannot scope the refusal to it; refusing wide is the under-issue direction this
    module already prefers, and it is recorded either way.
    """
    if component_part_id is None:
        return None
    return int(component_part_id) if int(component_part_id) in resolved_part_ids else None


def _record_alternate_group_diagnostics(
    bom: BOM,
    items: list[BOMItem],
    components: dict[int, Part],
    out: _BackflushBomExplosion,
) -> None:
    """Flag alternate groups on this BOM level that contain no non-alternate primary line.

    An alternate group is an OR: ONE member is issued and the rest are substitutes, which
    is why ``_is_non_consumed_bom_line`` skips every ``is_alternate`` line. A group in
    which EVERY line carries ``is_alternate`` therefore contributes exactly zero demand --
    and did so with no log line of any kind, which is the worst shape a wrong answer can
    take: indistinguishable from a BOM that genuinely has nothing to issue.

    Lines with no ``alternate_group`` are pooled under one ``None`` key rather than
    flagged individually, and that is deliberately conservative: an ordinary BOM line
    (``is_alternate`` False, no group) lands in that same pool and satisfies it, so a
    normal BOM carrying one stray ungrouped alternate is NOT flagged. Only a level whose
    ungrouped lines are ALL alternates -- i.e. one that really would issue nothing -- is.

    ``components`` is the caller's TENANT-SCOPED resolution of this level's
    ``component_part_id`` values; the diagnostic names the first member's id only when it
    resolves there. See ``_disclosable_component_id``.
    """
    groups: dict[Optional[str], list[BOMItem]] = {}
    for item in items:
        groups.setdefault(item.alternate_group, []).append(item)
    for group, members in groups.items():
        if not members or any(not bool(member.is_alternate) for member in members):
            continue
        label = f"'{group}'" if group else "of ungrouped lines"
        numbers = ", ".join(str(member.item_number) for member in members)
        out.diagnostics.append(
            _BackflushDiagnostic(
                code="alternate_group_without_primary",
                severity=BACKFLUSH_BLOCKING,
                detail=(
                    f"alternate group {label} on BOM {bom.id} (line(s) {numbers}) has no non-alternate "
                    "primary line, so no material would ever be issued for it. Mark one line as the primary"
                ),
                bom_item_id=members[0].id,
                component_part_id=_disclosable_component_id(members[0].component_part_id, components),
            )
        )


def _record_bom_line_diagnostics(
    item: BOMItem,
    component: Part,
    company_id: int,
    out: _BackflushBomExplosion,
) -> None:
    """Per-line conditions that make one BOM line's demand untrustworthy.

    Four families, none of which was read anywhere on this path before:

    * **zero quantity** -- ``float(item.quantity or 1)`` coerces a stored ``0.0`` to ONE
      PER PARENT UNIT. ``BOMItem.quantity`` has no CHECK behind ``schemas/bom.py``'s
      ``gt=0``, so a row written by an importer or by hand can hold it.
    * **negative quantity / scrap factor <= -1** -- produces a negative ``extended`` that
      is NETTED against positive demand for the same part elsewhere in the structure,
      quietly under-issuing rather than failing.
    * **unit-of-measure mismatch** -- ``BOMItem.unit_of_measure`` is documented as "may
      differ from part UOM" and is read NOWHERE on this path. Demand is a bare float and
      nothing in the platform converts units, so a line stating ``each`` against a part
      stocked in ``sheets`` issues the wrong quantity of the right material. New lines now
      inherit the component's unit instead of a literal "each", so this fires on stated
      disagreement rather than on an unchosen default; pre-existing lines are NOT rewritten
      and are worked off through ``GET /bom/uom-mismatches``.
    * **soft-deleted component** -- at issue time a soft-deleted component is still
      resolved and consumed; the shop believes that part is gone. Recorded here rather
      than moved silently.

    ``component`` is resolved TENANT-SCOPED by ``_tenant_components`` before this is
    called, so the cross-tenant case cannot normally reach here at all -- it is caught one
    level up as an unresolvable line. The ``foreign`` branch below is kept as a structural
    guard, and it is the one place in this module that must never interpolate the
    component's identity: this diagnostic's ``detail`` is rendered by
    ``GET /parts/{id}/backflush-readiness`` (open to any authenticated tenant user) and
    echoed in the refusal 409, so a part number read off another company's row would leave
    the server through a read this PR opened. Same-tenant soft-deleted components DO name
    themselves -- that is this company's own part, and the operator cannot act on the
    sentence otherwise.
    """
    quantity = item.quantity
    scrap_factor = float(item.scrap_factor or 0)
    if quantity is None or abs(float(quantity)) <= _EPSILON:
        out.diagnostics.append(
            _BackflushDiagnostic(
                code="zero_bom_quantity",
                severity=BACKFLUSH_BLOCKING,
                detail=(
                    f"BOM line {item.item_number} for {component.part_number} has quantity 0, which would be "
                    "treated as 1 per unit. State the real quantity"
                ),
                bom_item_id=item.id,
                component_part_id=component.id,
                component_part_number=component.part_number,
            )
        )
    elif float(quantity) < 0 or scrap_factor <= -1:
        out.diagnostics.append(
            _BackflushDiagnostic(
                code="negative_bom_quantity",
                severity=BACKFLUSH_BLOCKING,
                detail=(
                    f"BOM line {item.item_number} for {component.part_number} has a negative quantity or scrap "
                    "factor, which would cancel out demand from other lines for the same part. Correct the line"
                ),
                bom_item_id=item.id,
                component_part_id=component.id,
                component_part_number=component.part_number,
            )
        )

    line_uom = _uom_label(item.unit_of_measure)
    part_uom = _uom_label(component.unit_of_measure)
    # STILL BLOCKING, and still deliberately so -- do NOT quietly soften the severity here.
    #
    # The owner decision of 2026-07-27 fixed the DEFAULT rather than this severity: a new
    # BOM line with no stated unit now inherits the COMPONENT PART's unit (see
    # ``api/endpoints/bom.py`` -> ``_resolve_line_uom``, applied on all four BOM-line write
    # paths), so the diagnostic no longer fires on a value nobody chose. What it did NOT do
    # is rewrite history: this series is correct-forward, so every line written before that
    # change keeps its stored "each" and still blocks here. The remediation worklist is
    # ``GET /bom/uom-mismatches``, which shares ``uom_disagrees`` with this branch precisely
    # so the two cannot list different rows. See ``docs/MATERIAL_CONSUMPTION_PLAN.md`` ->
    # "Exposing the flag (PR 4.5)" -> the unit-of-measure callout.
    if uom_disagrees(item.unit_of_measure, component.unit_of_measure):
        out.diagnostics.append(
            _BackflushDiagnostic(
                code="unit_of_measure_mismatch",
                severity=BACKFLUSH_BLOCKING,
                detail=(
                    f"BOM line {item.item_number} states {line_uom} but part {component.part_number} is stocked "
                    f"in {part_uom}, and nothing converts units. State the line in {part_uom}"
                ),
                bom_item_id=item.id,
                component_part_id=component.id,
                component_part_number=component.part_number,
            )
        )

    foreign = getattr(component, "company_id", company_id) != company_id
    if foreign:
        # NO identity of any kind: not the part number, not the id. See the docstring --
        # this sentence is served to every authenticated user of THIS company.
        out.diagnostics.append(
            _BackflushDiagnostic(
                code="foreign_component_part",
                severity=BACKFLUSH_BLOCKING,
                detail=(
                    f"BOM line {item.item_number} names a component that is not a part in this company, so it "
                    "would be issued with no part number and no cost. Point the line at a live part in this "
                    "company"
                ),
                bom_item_id=item.id,
            )
        )
    elif bool(getattr(component, "is_deleted", False)):
        out.diagnostics.append(
            _BackflushDiagnostic(
                code="foreign_component_part",
                severity=BACKFLUSH_BLOCKING,
                detail=(
                    f"BOM line {item.item_number} names a component ({component.part_number}) that is deleted, so "
                    "backflush would issue a part the shop believes is gone. Point the line at a live part"
                ),
                bom_item_id=item.id,
                component_part_id=component.id,
                component_part_number=component.part_number,
            )
        )


def _tenant_components(db: Session, company_id: int, part_ids: Iterable[Optional[int]]) -> dict[int, Part]:
    """``{part_id: Part}`` for one BOM level's components, TENANT-SCOPED, in ONE query.

    Replaces ``joinedload(BOMItem.component_part)``. The relationship joins on
    ``component_part_id`` alone (``models/bom.py``) and applies no ``company_id``
    predicate, so it happily materialises ANOTHER COMPANY's ``Part`` -- which was harmless
    only while nothing rendered it. PR 4.5 renders diagnostics: the readiness GET is open
    to every authenticated tenant user and the refusal 409 echoes the same sentences, and
    ``bom.py``'s add-line validator resolves ``component_part_id`` unscoped, so a foreign
    id is reachable through supported verbs. Scoping the LOOKUP means the foreign object
    is never materialised at all, rather than materialised and then carefully not printed.

    Soft-deleted rows of THIS company are deliberately included: they resolve, get a
    ``foreign_component_part`` diagnostic that names them (an operator can act on that),
    and are refused. An id that does not come back is "not a part in this company" --
    hard-deleted, never existed, or another tenant's, three states this side of the fence
    cannot and must not distinguish.
    """
    ids = {int(part_id) for part_id in part_ids if part_id is not None}
    if not ids:
        return {}
    rows = db.query(Part).filter(Part.company_id == company_id, Part.id.in_(ids)).all()
    return {row.id: row for row in rows}


def _explode_backflush_bom(
    db: Session,
    bom: BOM,
    company_id: int,
    *,
    parent_qty: float,
    visited_part_ids: set[int],
    out: _BackflushBomExplosion,
    consumed: bool,
    depth: int = 0,
) -> None:
    """Explode a BOM with BACKFLUSH semantics: phantoms open up, ``make`` items do not.

    Deliberately NOT ``_collect_bom_components``. That helper appends a ``make`` /
    ``phantom`` sub-assembly AND recurses into its children, producing a flat list in
    which the assembly and its raw material both appear -- correct for the four callers
    that want "every part anywhere in this structure" (routing generation, cost estimate,
    MRP-ish reads), and a double-consume for a backflush. It also has three callers
    outside this module whose behavior must not change inside a backflush PR, and its
    flat output cannot express the make/phantom distinction at all (a line's parent is
    not recoverable from it). So the traversal is re-stated here, with the same
    ``item_number, id`` ordering, the same ``quantity x parent_qty x (1 + scrap_factor)``
    extension and the same visited-set cycle guard, and a different rule at each node:

    * **phantom** -- the assembly is a planning fiction that is never stocked, so it is
      EXCLUDED and its children are exploded in its place at the phantom's extended
      quantity. A phantom with no active BOM has nothing to explode into; it is treated
      as a stocked line instead (and logged), because dropping it would make the line
      vanish with no ledger row, no shortage and no signal of any kind.
    * **make** -- a stocked unit. The assembly is issued; its children are NOT, because
      they were consumed when IT was built and issuing both consumes the same material
      twice. Its subtree is still walked, in exclude-only mode, so the routing leg cannot
      issue those children either.
    * **buy** -- a stocked unit with no structure below it. Issued, never recursed into
      (matching ``_collect_bom_components``).

    ``consumed=False`` walks a subtree purely to collect ``excluded_part_ids`` and
    contributes no demand. Does not commit and writes nothing.

    ``depth`` bounds the recursion at ``_MAX_BOM_LEVELS`` (mirroring ``bom.py``'s
    ``explode_bom_recursive``) and emits a diagnostic instead of raising -- see that
    constant for why a ``RecursionError`` here would be far worse than a wrong answer.

    Diagnostics are collected on ``out`` for every condition the walk has to make a
    judgement call about. They are only collected for a line this walk would actually
    ISSUE (``line_consumed`` -- a consuming walk AND a line that is not an
    alternate/optional/reference): an exclude-only pass over a ``make`` sub-assembly's
    subtree issues nothing, and neither does a reference line, so a wrong quantity, a
    mismatched UoM, an unresolvable component or a cut cycle down there cannot move
    material and flagging it would refuse an opt-in over a line the leg never reads. The
    depth cap is the exception -- it is a runtime hazard, not a data-quality one.
    """
    if depth >= _MAX_BOM_LEVELS:
        logger.warning(
            "BOM explosion for BOM %s (company %s) hit the %s-level cap; deeper levels are not backflushed",
            bom.id,
            company_id,
            _MAX_BOM_LEVELS,
        )
        out.diagnostics.append(
            _BackflushDiagnostic(
                code="bom_depth_exceeded",
                severity=BACKFLUSH_BLOCKING,
                detail=(
                    f"the BOM structure is deeper than {_MAX_BOM_LEVELS} levels at BOM {bom.id}, so the "
                    "components below that point would never be issued. Flatten the structure"
                ),
            )
        )
        return
    items = (
        db.query(BOMItem)
        .filter(BOMItem.bom_id == bom.id, BOMItem.company_id == company_id)
        .order_by(BOMItem.item_number.asc(), BOMItem.id.asc())
        .all()
    )
    # TENANT-SCOPED, replacing ``joinedload(BOMItem.component_part)``. See
    # ``_tenant_components``: the relationship carries no ``company_id`` predicate, and
    # since PR 4.5 these objects' identities are rendered to any authenticated user.
    components = _tenant_components(db, company_id, (item.component_part_id for item in items))
    if consumed:
        _record_alternate_group_diagnostics(bom, items, components, out)
    for item in items:
        component = components.get(item.component_part_id) if item.component_part_id is not None else None
        # Computed BEFORE the two skips below, not after: both of them describe demand
        # this walk would have issued, so on a line the walk never issues (an alternate,
        # an optional, a reference line, or any line inside an exclude-only pass) they
        # would refuse an opt-in over a line that contributes nothing either way.
        line_consumed = consumed and not _is_non_consumed_bom_line(item)
        if component is None:
            # PREVIOUSLY MERGED with the cycle skip below into one unlogged ``continue``.
            # They are opposite conditions with opposite remedies: this one is a BOM line
            # whose component does not resolve to a part in this company (hard-deleted out
            # from under it, an FK never enforced -- SQLite does not enforce them at all --
            # or another tenant's id written by ``bom.py``'s unscoped add-line validator),
            # and it silently removes that line's demand.
            #
            # It carries NO ``component_part_id``. The three states above are deliberately
            # indistinguishable here, and the only one whose id would be new information to
            # the reader is the cross-tenant one. Every other diagnostic site now follows the
            # same resolve-or-withhold rule via ``_disclosable_component_id``; this one is
            # where the rule came from.
            if line_consumed:
                out.diagnostics.append(
                    _BackflushDiagnostic(
                        code="missing_component_part",
                        severity=BACKFLUSH_BLOCKING,
                        detail=(
                            f"BOM line {item.item_number} on BOM {bom.id} names a component that does not resolve "
                            "to a part in this company, so its demand would be dropped without a trace. Repoint "
                            "or remove the line"
                        ),
                        bom_item_id=item.id,
                    )
                )
            continue
        if component.id in visited_part_ids:
            if line_consumed:
                out.diagnostics.append(
                    _BackflushDiagnostic(
                        code="circular_bom",
                        severity=BACKFLUSH_BLOCKING,
                        detail=(
                            f"the BOM contains a cycle: line {item.item_number} on BOM {bom.id} reaches part "
                            f"{component.part_number}, which is already an ancestor of it, so that branch is "
                            "cut and its demand is dropped. Break the cycle"
                        ),
                        bom_item_id=item.id,
                        component_part_id=component.id,
                        component_part_number=component.part_number,
                    )
                )
            continue

        if line_consumed:
            _record_bom_line_diagnostics(item, component, company_id, out)
        extended = float(item.quantity or 1) * parent_qty * (1 + float(item.scrap_factor or 0))
        item_type = (item.item_type or "").lower()
        child_visited = visited_part_ids | {component.id}
        child_bom: Optional[BOM] = None
        if item_type != BOMItemType.BUY.value:
            # Imported lazily to avoid an import cycle with the endpoints module. One
            # definition of "the active BOM for a part" for the whole platform.
            from app.api.endpoints.work_orders import _get_active_bom

            child_bom = _get_active_bom(db, component.id, company_id)

        if item_type == BOMItemType.PHANTOM.value and child_bom is not None:
            out.excluded_part_ids.add(component.id)
            _explode_backflush_bom(
                db,
                child_bom,
                company_id,
                parent_qty=extended,
                visited_part_ids=child_visited,
                out=out,
                consumed=line_consumed,
                depth=depth + 1,
            )
            continue
        if item_type == BOMItemType.PHANTOM.value:
            logger.warning(
                "BOM item %s on BOM %s (company %s) is a phantom with no active BOM to explode; "
                "backflushing part %s as a stocked line",
                item.id,
                bom.id,
                company_id,
                component.id,
            )
            if line_consumed:
                out.diagnostics.append(
                    _BackflushDiagnostic(
                        code="phantom_without_bom",
                        severity=BACKFLUSH_BLOCKING,
                        detail=(
                            f"BOM line {item.item_number} ({component.part_number}) is a phantom with no active "
                            "BOM to explode, so it would be issued as if it were stocked. Give it an active BOM "
                            "or change the line type"
                        ),
                        bom_item_id=item.id,
                        component_part_id=component.id,
                        component_part_number=component.part_number,
                    )
                )

        if line_consumed:
            out.demand[component.id] = out.demand.get(component.id, 0.0) + extended
        else:
            out.excluded_part_ids.add(component.id)

        if child_bom is not None:
            # A stocked sub-assembly. Walk its subtree EXCLUDE-ONLY: its children were
            # consumed when it was built, and the walk exists so the routing leg -- which
            # was generated from a traversal that does recurse here -- cannot issue them.
            _explode_backflush_bom(
                db,
                child_bom,
                company_id,
                parent_qty=extended,
                visited_part_ids=child_visited,
                out=out,
                consumed=False,
                depth=depth + 1,
            )


def _part_numbers(db: Session, company_id: int, part_ids: Iterable[Optional[int]]) -> dict[int, Optional[str]]:
    """``{part_id: part_number}`` for the given ids, tenant-scoped, in ONE query.

    Diagnostics and suppression records both name components by part NUMBER -- an id is
    not something an operator can act on -- and both would otherwise issue a lookup per
    row. ``None``s in the input are dropped; an empty input costs no query at all.
    """
    ids = {int(part_id) for part_id in part_ids if part_id is not None}
    if not ids:
        return {}
    return {
        part_id: part_number
        for part_id, part_number in db.query(Part.id, Part.part_number)
        .filter(Part.company_id == company_id, Part.id.in_(ids))
        .all()
    }


def _routing_backflush_demand(
    db: Session,
    work_order: WorkOrder,
    company_id: int,
    *,
    basis: float,
    diagnostics: list[_BackflushDiagnostic],
) -> dict[int, float]:
    """Component demand stated by the ROUTING (``component_part_id`` on an operation).

    Three things here are not what the previous shape of this leg assumed:

    * **``component_quantity`` is the WHOLE-JOB total, not a per-unit rate.** Both writers
      (``_create_assembly_routing_operations`` and
      ``_reconcile_operation_component_quantities``) store
      ``qty_per_assembly x work_order.quantity_ordered``. Multiplying it by the produced
      quantity again -- which is what this leg did -- squares the demand: a 100-piece job
      with 2 per unit asked for 20,000. The rate is recovered by dividing back out by
      ``quantity_ordered`` and re-scaling to ``basis``, so the routing and the BOM answer
      in the same units. A work order with no ordered quantity has no rate to recover, so
      the stated total is used verbatim.
    * **One component's demand is REPLICATED across every operation that touches it.** A
      component with a three-operation routing gets three operations all carrying the same
      whole-job ``component_quantity``; summing them tripled the demand. Reduced with
      ``max``: the stated value when the rows agree, and incapable of multiplying by the
      operation count.

      An earlier version of this comment justified ``max`` over ``min`` by claiming
      ``min`` would silently under-issue to 0 on an unreconciled row. That is **wrong**,
      and the refutation is two lines below: rows with no ``component_quantity`` are
      dropped by the ``stated <= _EPSILON`` skip BEFORE the reduction, so ``min`` could
      never be dragged to zero by one. With that argument gone, ``max`` is a bare choice
      of the OVER-issue direction on a genuine disagreement -- which sits awkwardly beside
      this module's own rule that over-issuing writes material into a genealogy record
      that never contained it. It is kept because rows only disagree when something
      upstream failed to reconcile, where the larger stated demand is the likelier truth
      and a shortage is visible while a silent under-issue is not. The disagreement is
      logged so it is answerable; if that log is ever seen in practice, REFUSING is the
      better answer than picking a side, and this leg is flag-gated so a refusal costs
      nothing today.
    * **Self-consumption is refused.** An operation naming the work order's OWN part would
      ISSUE the part the finished-goods leg just RECEIVED, netting the job's own output
      out of stock and writing the produced part into its own as-built record.

    Operation STATUS is deliberately not filtered. ``OperationStatus`` has no cancelled or
    skipped member, so no status means "this work did not happen"; the quantity is
    job-scoped and replicated, so filtering would change the JOB's demand according to
    which replica happened to survive; and at work-order completion every operation is
    terminal by construction (force-complete stamps the stragglers). A non-COMPLETE
    operation contributing demand is logged instead, because the only way it happens is a
    completion path that closed the work order without closing its operations -- a
    production-record defect worth surfacing rather than silently absorbing.
    """
    operations = (
        db.query(WorkOrderOperation)
        .filter(
            WorkOrderOperation.work_order_id == work_order.id,
            WorkOrderOperation.company_id == company_id,
            WorkOrderOperation.component_part_id.isnot(None),
        )
        .all()
    )
    ordered = float(work_order.quantity_ordered or 0)
    part_numbers = _part_numbers(db, company_id, (op.component_part_id for op in operations))
    demand: dict[int, float] = {}
    if operations and ordered <= _EPSILON:
        # WORK-ORDER-scoped and therefore advisory: it cannot be gated at part opt-in.
        # ``component_quantity`` is a WHOLE-JOB total, so with no ordered quantity there is
        # no per-unit rate to recover and the stated total is used verbatim -- silently,
        # until now. ``WorkOrder.quantity_ordered`` carries no positive constraint.
        diagnostics.append(
            _BackflushDiagnostic(
                code="zero_quantity_ordered",
                severity=BACKFLUSH_ADVISORY,
                detail=(
                    f"Work order {work_order.work_order_number} has an ordered quantity of 0, so routing "
                    "component demand is taken as a whole-job total rather than a per-unit rate."
                ),
            )
        )
    for op in operations:
        component_part_id = op.component_part_id
        if component_part_id is None:
            continue
        if work_order.part_id is not None and component_part_id == work_order.part_id:
            logger.warning(
                "Operation %s on WO %s (company %s) names the work order's own part %s as a component; "
                "backflush self-consumption refused",
                op.id,
                work_order.id,
                company_id,
                component_part_id,
            )
            diagnostics.append(
                _BackflushDiagnostic(
                    code="operation_names_own_part",
                    severity=BACKFLUSH_BLOCKING,
                    detail=(
                        f"operation {op.operation_number or op.id} names the work order's own part as a "
                        "component, which would issue the part the finished-goods receipt just received. "
                        "Clear that operation's component"
                    ),
                    # Resolve-or-withhold, uniformly with every other diagnostic site --
                    # ``WorkOrderOperation.component_part_id`` is as unscoped an FK as the
                    # BOM's. See ``_disclosable_component_id``.
                    component_part_id=_disclosable_component_id(component_part_id, part_numbers),
                    component_part_number=part_numbers.get(component_part_id),
                    operation_id=op.id,
                )
            )
            continue
        stated = float(op.component_quantity or 0)
        if stated <= _EPSILON:
            continue
        line = (stated / ordered) * basis if ordered > _EPSILON else stated
        previous = demand.get(component_part_id)
        if previous is not None and abs(previous - line) > _EPSILON:
            logger.warning(
                "WO %s (company %s) operations disagree on component %s demand (%s vs %s); " "backflushing the larger",
                work_order.id,
                company_id,
                component_part_id,
                previous,
                line,
            )
            diagnostics.append(
                _BackflushDiagnostic(
                    code="operations_disagree_on_component",
                    severity=BACKFLUSH_BLOCKING,
                    detail=(
                        f"two operations state different quantities for component "
                        f"{part_numbers.get(component_part_id) or component_part_id} "
                        f"({previous:g} vs {line:g}); the larger would be issued. Reconcile the routing"
                    ),
                    component_part_id=_disclosable_component_id(component_part_id, part_numbers),
                    component_part_number=part_numbers.get(component_part_id),
                    operation_id=op.id,
                )
            )
        demand[component_part_id] = line if previous is None else max(previous, line)
        if op.status != OperationStatus.COMPLETE:
            logger.warning(
                "Operation %s on WO %s (company %s) is %s but contributes backflush demand for part %s",
                op.id,
                work_order.id,
                company_id,
                getattr(op.status, "value", op.status),
                component_part_id,
            )
            diagnostics.append(
                _BackflushDiagnostic(
                    code="incomplete_operation_demand",
                    severity=BACKFLUSH_ADVISORY,
                    detail=(
                        f"Operation {op.operation_number or op.id} is "
                        f"{getattr(op.status, 'value', op.status)} but contributes backflush demand for "
                        f"{part_numbers.get(component_part_id) or component_part_id}."
                    ),
                    component_part_id=_disclosable_component_id(component_part_id, part_numbers),
                    component_part_number=part_numbers.get(component_part_id),
                    operation_id=op.id,
                )
            )
    return demand


def _record_backflush_demand_suppressed(
    db: Session,
    work_order: WorkOrder,
    *,
    part_id: int,
    quantity: float,
    company_id: int,
    audit: AuditService,
    reason: str,
    ledger_net: Optional[float] = None,
) -> None:
    """Record that resolved backflush demand was dropped without issuing material.

    Demand for this component -- from the BOM/routing, or from an explicit
    work-order-scoped tie -- did not move material on THIS completion. Whether that is
    correct (the material already left under a tie) or merely unavoidable (a legacy
    one-shot row fences this work order out) it must not be silent: an as-built review
    cannot reconstruct a decision the system never wrote down, and "correct but
    invisible" is the control gap this action exists to close.

    ``reason`` distinguishes the two, because they have opposite remedies:

    * ``ledger_consumed`` -- a tie already drew this material. Nothing is wrong; the row
      exists so the absence of a backflush ISSUE is explicable.
    * ``already_issued`` -- a LEGACY (pre-PR-4.4) ``('work_order', ISSUE)`` row exists on
      this work order for this part. That shape is covered by ``uq_wo_inventory_issue``,
      which permits exactly one such row per (company, work order, part) forever, so this
      work order is fenced out of the reconciling engine for this part permanently --
      including after a RETURN, which appends a credit rather than removing the ISSUE.
      The remedy is an OPERATION-scoped tie, which posts outside that index. Only
      pre-existing work orders can reach this; nothing writes the legacy shape any more.

    ``quantity`` is always the demand that DID NOT MOVE -- never the gross demand. On leg
    1 that is the resolved target (its suppression layers have already run, and the fence
    drops the whole thing); on leg 2 it is the tie's UNMET REMAINDER, ``qty_planned``
    minus the tie's own ledger net, which is passed alongside as ``ledger_net``. Recording
    a gross ``qty_planned`` on a partly-drained tie would put a false figure on the hash
    chain -- claiming material was dropped that had in fact already been issued.
    """
    part_number = (
        db.query(Part.part_number).filter(Part.company_id == company_id, Part.id == part_id).scalar()  # noqa: E501
    )
    logger.warning(
        "Backflush demand of %s for part %s on WO %s (company %s) suppressed (%s)",
        quantity,
        part_id,
        work_order.id,
        company_id,
        reason,
    )
    audit.log(
        action=BACKFLUSH_DOUBLE_ISSUE_BLOCKED_AUDIT_ACTION,
        resource_type="inventory",
        resource_id=part_id,
        resource_identifier=part_number or str(part_id),
        description=(
            f"Backflush of {quantity} of component {part_number or part_id} on work order "
            f"{work_order.work_order_number} was not issued ({reason})"
        ),
        new_values={"suppressed_quantity": quantity, "ledger_net_issued": ledger_net},
        extra_data={
            "work_order_id": work_order.id,
            "work_order_number": work_order.work_order_number,
            "component_part_id": part_id,
            "component_part_number": part_number,
            "suppressed_quantity": quantity,
            "ledger_net_issued": ledger_net,
            "suppression_reason": reason,
        },
        company_id=company_id,
    )


def _backflush_basis(work_order: WorkOrder) -> float:
    """Units of the finished part whose material this job actually drew.

    ``quantity_complete + scrapped`` -- the same shape the per-run tie engine reconciles
    to, so one shop cannot report two different consumptions for one physical event
    depending on whether the material happened to be tied. A scrapped unit consumed its
    material exactly like a good one; leaving scrap out is why a fully-scrapped work
    order used to backflush NOTHING.

    **The scrap term comes from the OPERATIONS, and getting this wrong is invisible.**
    ``WorkOrder.quantity_complete`` IS rolled up from operations
    (``sync_work_order_quantity_complete``, ``max()``-guarded and monotonic-up), so it is
    trustworthy. ``WorkOrder.quantity_scrapped`` is NOT rolled up at all -- its only
    writers are a child reset, a null-guard, force-complete's explicit override and the
    manual office edit. Reading the header column would therefore report scrap as ZERO
    for the ordinary case: an operator who scraps 3 of 10 at the kiosk leaves
    ``operation.quantity_scrapped = 3`` and the header at ``0``, so the tie engine would
    consume for 10 and this leg for 7 -- precisely the divergence including scrap is
    meant to close.

    SUMMED across operations, not maxed: a unit scrapped at operation 10 and a unit
    scrapped at operation 20 are DIFFERENT units, and both drew this job's material.

    The header column is a FALLBACK only, used when no operation carries scrap -- which
    is exactly the force-complete-with-explicit-scrap path, the one writer that sets it.
    """
    produced = float(work_order.quantity_complete or 0)
    operation_scrap = sum(float(op.quantity_scrapped or 0) for op in (work_order.operations or []))
    scrapped = operation_scrap if operation_scrap > _EPSILON else float(work_order.quantity_scrapped or 0)
    return produced + scrapped


@dataclass
class _BackflushResolution:
    """Everything the demand resolver worked out, INCLUDING what it declined to issue.

    The pure ``_resolve_backflush_demand``'s return type, and the reason that function
    was split out of ``_resolve_backflush_components`` at all. The old shape was a bare
    ``dict[int, float]``, which could express only the surviving demand -- so the two
    suppression layers had nowhere to report what they had dropped except by WRITING an
    audit row from inside the resolution itself.

    That coupling is what made a dry run structurally impossible: the only way to learn
    what a completion would do was to run the thing that writes hash-chain rows saying it
    had happened. Reporting the facts and RECORDING them are now different jobs, done by
    different functions, and only one of them takes an ``AuditService``.

    * ``demand`` -- the surviving target per component part.
    * ``allocation_blocked`` -- ``{part_id: dropped demand}`` for parts an OPEN
      operation-scoped tie owns. Never audited (the tie is the demand carrier; nothing was
      lost), but the preview must show it or it would over-state what will move.
    * ``ledger_blocked`` -- ``{part_id: (dropped demand, signed ledger net)}`` for parts
      the ledger already shows leaving this job. THIS is the set that becomes
      ``BACKFLUSH_DOUBLE_ISSUE_BLOCKED`` rows -- on the write path only.
    * ``diagnostics`` -- see ``_BackflushDiagnostic``.
    """

    basis: float = 0.0
    demand: dict[int, float] = field(default_factory=dict)
    allocation_blocked: dict[int, float] = field(default_factory=dict)
    ledger_blocked: dict[int, tuple[float, float]] = field(default_factory=dict)
    diagnostics: list[_BackflushDiagnostic] = field(default_factory=list)


def _resolve_backflush_components(
    db: Session,
    work_order: WorkOrder,
    company_id: int,
    allocations: Optional[list[WorkOrderMaterialAllocation]] = None,
    *,
    audit: AuditService,
    user_id: Optional[int] = None,
) -> dict[int, float]:
    """Required quantity per component part for backflushing this WO. **Writes.**

    The WRITE-PATH wrapper over the pure ``_resolve_backflush_demand``. It does two
    things the read must not: it records each ledger-suppressed part as a
    ``BACKFLUSH_DOUBLE_ISSUE_BLOCKED`` chain row, and it applies the resolver's BLOCKING
    diagnostics -- refusing the demand they describe and recording each one as a
    ``BACKFLUSH_DEMAND_REFUSED`` chain row.

    **Keep the audit writes here and nowhere below it.** The suppression/refusal DECISION
    is a fact a read is entitled to ask for; the audit ROW is a claim that a completion
    happened and declined to issue something. Fusing them -- which is what the previous
    shape did -- meant a preview could not exist without polluting the hash chain with
    rows describing nothing that occurred, and falsifying ``_drop_ledger_covered_parts``'
    own documented cardinality ("bounded, not per-request... not on every read"). This
    feature has already had to defend that boundary once: ``material_tie_view`` exists
    because a poll is not an actor and records no reason. A structural split keeps it true
    by construction rather than by discipline -- an ``audit=None`` flag would not.

    ================================================================ WHY IT REFUSES
    **A blocking diagnostic REFUSES the demand it describes rather than issuing it.**
    Through PR 4.4 these diagnostics were computed here and dropped on the floor, and the
    permissive answer was defensible only because the leg was dark. PR 4.5 arms it from a
    form, and the incoherence became live: the opt-in gate refuses to ARM a part over
    exactly these conditions ("the automation would issue the WRONG material"), while the
    completion path met the same condition and issued anyway, silently. A gate that
    protects only the instant of the flip protects nothing -- every input it reads is
    mutable afterwards by anyone with ``boms:edit``.

    The direction follows this module's own rule (``_is_non_consumed_bom_line``):
    under-issuing leaves the material on the shelf where the operator who needs it draws
    it manually and the job's cost shows the gap; over-issuing writes material into an
    as-built genealogy record that never contained it, which no downstream reader can
    distinguish from the truth. Refusing is the under-issuing direction, and -- unlike the
    old silent-and-wrong answer -- it is RECORDED: one chain row per blocking diagnostic,
    naming the condition, the component and the quantity that did not move.

    Scope of a refusal, and it is deliberately two-tier:

    * **Per component** when the diagnostic names one (``component_part_id``) -- a zero /
      negative quantity, a UoM mismatch, a soft-deleted component, a phantom with no BOM,
      a routing/BOM disagreement, two operations disagreeing. Only that component is
      dropped; the rest of the BOM still consumes, which is the same per-part precedence
      rule the routing leg follows. It can OVER-refuse (a part flagged as one line's
      alternate that is also a primary line elsewhere loses its demand too) -- that is the
      safe direction and it is recorded.
    * **The whole leg** when the diagnostic names no component, because the condition is
      structural and the resolved demand is incomplete in a way no component owns:
      ``deleted_active_bom`` (the structure the shop deleted is the structure moving
      stock), ``bom_depth_exceeded`` (everything below the cap is missing) and
      ``missing_component_part`` (a line's demand vanished and nothing can say whose).

    ``no_demand_source`` is ADVISORY at resolver scope precisely so it cannot reach the
    structural tier: "this job has no BOM" is the common case for a turned part or a
    part-less nest package, not a defect. Its BLOCKING form lives at part-opt-in scope in
    ``backflush_readiness_for_part``, where it means something ("arming this would consume
    nothing").

    ``user_id`` is carried only so the refusal's ``OperationalEvent`` can be attributed. It
    is ``Optional`` because the reconcile-on-read entries have no actor to name, exactly as
    ``_record_backflush_component_failed`` already models.
    """
    resolution = _resolve_backflush_demand(db, work_order, company_id, allocations)
    _refuse_blocked_demand(db, work_order, resolution, company_id=company_id, audit=audit, user_id=user_id)
    if resolution.ledger_blocked:
        part_numbers = _part_numbers(db, company_id, resolution.ledger_blocked.keys())
        for part_id, (demand, net) in resolution.ledger_blocked.items():
            _record_ledger_suppression(
                work_order,
                part_id=part_id,
                part_number=part_numbers.get(part_id),
                demand=demand,
                net=net,
                company_id=company_id,
                audit=audit,
            )
    return resolution.demand


def blocked_demand_refusal(resolution: _BackflushResolution) -> tuple[bool, set[int]]:
    """What a completion would REFUSE over blocking diagnostics. **PURE — decides only.**

    Returns ``(refuses the whole leg, {component part ids refused})``. The read half of
    ``_refuse_blocked_demand``, split out for the same reason the resolver was split from
    the recorder: the dry-run preview has to state the SAME answer the completion will
    act on, and it may not write anything to learn it. A preview that showed a component
    posting 20 while the completion refused it would break the one promise the preview
    exists to keep.

    A blocking diagnostic that names no component is structural -- the resolved demand is
    incomplete in a way no component owns -- and refuses the whole BOM/routing leg. See
    ``_resolve_backflush_components`` for the full argument and the code list.
    """
    blocking = [d for d in resolution.diagnostics if d.severity == BACKFLUSH_BLOCKING]
    whole_leg = any(d.component_part_id is None for d in blocking)
    named_ids = {int(d.component_part_id) for d in blocking if d.component_part_id is not None}
    return whole_leg, named_ids


def _refuse_blocked_demand(
    db: Session,
    work_order: WorkOrder,
    resolution: _BackflushResolution,
    *,
    company_id: int,
    audit: AuditService,
    user_id: Optional[int] = None,
) -> None:
    """Drop the demand every BLOCKING diagnostic condemns, and record each one. **Writes.**

    Mutates ``resolution.demand`` in place and appends one
    ``BACKFLUSH_DEMAND_REFUSED`` chain row per blocking diagnostic. See
    ``_resolve_backflush_components`` for WHY it refuses and for the two-tier scope; this
    function is only the mechanics.

    The row is written for EVERY blocking diagnostic, including ones whose component
    carried no surviving demand to refuse (an alternate group that would issue nothing, a
    cut cycle, an operation naming the job's own part). A refusal that changed no quantity
    is still the fact an as-built review needs: it says the system read this BOM at
    completion time, judged it wrong, and declined -- which is not reconstructable later
    from a ledger that simply has no row.

    **``refused_quantity`` is attributed ONCE PER REFUSED SCOPE, never once per row.** Both
    tiers can emit several rows naming the same thing: ``_record_bom_line_diagnostics``
    alone can raise ``zero_bom_quantity`` AND ``unit_of_measure_mismatch`` AND the
    deleted-component branch for ONE line, and two lines can name one component. Charging
    each of those the component's full demand would put a FALSE FIGURE on the hash chain --
    an auditor summing ``BACKFLUSH_DEMAND_REFUSED`` would read double what actually failed
    to move. This module already refuses to do that elsewhere (see
    ``_record_backflush_demand_suppressed``, which records a tie's UNMET REMAINDER rather
    than its gross ``qty_planned``, for the same reason). So the FIRST row naming a given
    component carries the quantity and every later one carries ``0`` -- exactly as the
    structural tier already attributed its total to ``structural[0]``. The rows that carry
    the quantity are also the rows that emit the ``OperationalEvent``, so one refused
    component notifies once rather than once per condition it violates.
    """
    blocking = [d for d in resolution.diagnostics if d.severity == BACKFLUSH_BLOCKING]
    if not blocking:
        return

    structural = [d for d in blocking if d.component_part_id is None]
    _, named_ids = blocked_demand_refusal(resolution)
    part_numbers = _part_numbers(db, company_id, named_ids | set(resolution.demand))
    surviving_total = sum(resolution.demand.values())
    attributed: set[int] = set()

    for diagnostic in blocking:
        named = diagnostic.component_part_id
        if structural:
            # A structural blocker refuses the WHOLE leg, so the quantity it stopped is
            # every surviving line; naming one component's share would under-state it.
            # Attributed to the FIRST structural row so N rows do not each claim the
            # same total.
            attributes = diagnostic is structural[0]
            refused = surviving_total if attributes else 0.0
        else:
            # Same rule, per component: the first diagnostic naming this component owns the
            # quantity, the rest are recorded at 0.
            attributes = named is not None and int(named) not in attributed
            if named is not None:
                attributed.add(int(named))
            refused = resolution.demand.get(int(named), 0.0) if attributes and named is not None else 0.0
        _record_demand_refused(
            work_order,
            diagnostic=diagnostic,
            part_number=part_numbers.get(int(named)) if named is not None else None,
            refused_quantity=refused,
            whole_leg=bool(structural),
            company_id=company_id,
            audit=audit,
        )
        if attributes:
            _emit_demand_refused_event(
                db,
                work_order,
                diagnostic=diagnostic,
                part_number=part_numbers.get(int(named)) if named is not None else None,
                refused_quantity=refused,
                whole_leg=bool(structural),
                company_id=company_id,
                user_id=user_id,
            )

    if structural:
        logger.warning(
            "WO %s (company %s) backflush leg refused entirely (%s of component demand not issued): %s",
            work_order.id,
            company_id,
            surviving_total,
            ", ".join(d.code for d in structural),
        )
        resolution.demand.clear()
        return

    for part_id in named_ids:
        dropped = resolution.demand.pop(part_id, None)
        if dropped is not None:
            logger.warning(
                "WO %s (company %s) backflush demand of %s for component %s refused: a blocking diagnostic stands",
                work_order.id,
                company_id,
                dropped,
                part_id,
            )


def _record_demand_refused(
    work_order: WorkOrder,
    *,
    diagnostic: _BackflushDiagnostic,
    part_number: Optional[str],
    refused_quantity: float,
    whole_leg: bool,
    company_id: int,
    audit: AuditService,
) -> None:
    """The ``BACKFLUSH_DEMAND_REFUSED`` chain row for ONE blocking diagnostic.

    Keyed on the COMPONENT part when the diagnostic names one (so it lands beside that
    part's ``BACKFLUSH_SHORTAGE`` / ``BACKFLUSH_DOUBLE_ISSUE_BLOCKED`` history under
    ``resource_type='inventory'``), and on the work order otherwise -- a structural
    blocker is a fact about the job's BOM, not about any one component.

    ``extra_data`` carries the diagnostic verbatim (``code``, ``severity``, ``detail``,
    and whichever of ``bom_item_id`` / ``component_part_id`` / ``operation_id`` it
    populated) so the row names the row to fix, not merely the fact that something was
    wrong. ``detail`` is the same operator-facing sentence the refusal gate and the
    readiness read show, which is what stops the record and the UI drifting apart -- and
    it is written by the diagnostic sites, which never interpolate another tenant's data.
    """
    component_part_id = diagnostic.component_part_id
    scope = "the whole backflush leg" if whole_leg else f"component {part_number or component_part_id}"
    if component_part_id is not None:
        resource_type = "inventory"
        resource_id = component_part_id
        resource_identifier = part_number or str(component_part_id)
    else:
        resource_type = "work_order"
        resource_id = work_order.id
        resource_identifier = work_order.work_order_number or str(work_order.id)
    audit.log(
        action=BACKFLUSH_DEMAND_REFUSED_AUDIT_ACTION,
        resource_type=resource_type,
        resource_id=resource_id,
        resource_identifier=resource_identifier,
        description=(
            f"Backflush on work order {work_order.work_order_number} refused {scope} "
            f"({refused_quantity} not issued): {diagnostic.detail}"
        ),
        new_values={"refused_quantity": refused_quantity, "diagnostic_code": diagnostic.code},
        extra_data={
            "work_order_id": work_order.id,
            "work_order_number": work_order.work_order_number,
            "component_part_id": component_part_id,
            "component_part_number": part_number or diagnostic.component_part_number,
            "refused_quantity": refused_quantity,
            "refused_whole_leg": whole_leg,
            "diagnostic_code": diagnostic.code,
            "diagnostic_severity": diagnostic.severity,
            "diagnostic_detail": diagnostic.detail,
            "bom_item_id": diagnostic.bom_item_id,
            "operation_id": diagnostic.operation_id,
        },
        company_id=company_id,
    )


def _emit_demand_refused_event(
    db: Session,
    work_order: WorkOrder,
    *,
    diagnostic: _BackflushDiagnostic,
    part_number: Optional[str],
    refused_quantity: float,
    whole_leg: bool,
    company_id: int,
    user_id: Optional[int],
) -> None:
    """The warning ``OperationalEvent`` for ONE refused scope. Best-effort, never raises.

    The notification half of ``_record_demand_refused``, and split out for the same reason
    ``_record_backflush_component_failed`` keeps both: the chain row is the compliance
    record, the event is what reaches a human. Without it the refusal is strictly QUIETER
    than the shortage it is worse than -- see ``BACKFLUSH_DEMAND_REFUSED_EVENT_TYPE`` --
    and, because nothing disarms the part, it repeats silently on every subsequent job.

    Called ONCE per refused scope (the first diagnostic naming a component, or the first
    structural blocker), not once per diagnostic: a BOM line with a zero quantity AND a UoM
    mismatch is one refused component, not two notifications.

    **Its own savepoint, for the reason PR 4.4's two failure recorders state.**
    ``emit_best_effort`` swallows the exception, but a ``flush()`` that fails AT THE DB
    still deactivates the outer transaction, so the caller's next flush would raise
    ``PendingRollbackError`` with the true cause only in a WARNING log. This runs on a path
    reached from a reconcile-on-read GET whose whole contract is "never 500", and inside a
    loop that must survive to the next diagnostic. A ``None`` return is the documented
    failure signal, so rolling back to the savepoint on it restores a usable session.
    """
    component_part_id = diagnostic.component_part_id
    payload = {
        "work_order_id": work_order.id,
        "work_order_number": work_order.work_order_number,
        "component_part_id": component_part_id,
        "component_part_number": part_number or diagnostic.component_part_number,
        "refused_quantity": refused_quantity,
        "refused_whole_leg": whole_leg,
        "diagnostic_code": diagnostic.code,
        "diagnostic_detail": diagnostic.detail,
        "bom_item_id": diagnostic.bom_item_id,
        "operation_id": diagnostic.operation_id,
    }
    savepoint = db.begin_nested()
    if (
        OperationalEventService(db).emit_best_effort(
            company_id=company_id,
            event_type=BACKFLUSH_DEMAND_REFUSED_EVENT_TYPE,
            source_module="completion_inventory",
            # Keyed like the chain row: the component when one is named, the job otherwise.
            entity_type="work_order" if component_part_id is None else "inventory",
            entity_id=work_order.id if component_part_id is None else component_part_id,
            work_order_id=work_order.id,
            operation_id=diagnostic.operation_id,
            user_id=user_id,
            severity="warning",
            event_payload=payload,
        )
        is None
    ):
        savepoint.rollback()
    else:
        savepoint.commit()


def _resolve_backflush_demand(
    db: Session,
    work_order: WorkOrder,
    company_id: int,
    allocations: Optional[list[WorkOrderMaterialAllocation]] = None,
) -> _BackflushResolution:
    """Required quantity per component part for backflushing this WO. **PURE READ.**

    No session mutation, no audit row, nothing to commit -- the property that lets the
    dry-run preview shares the resolver with the completion path instead of approximating
    it. (It DOES read: the BOM walk and the ledger nets are SELECTs.)

    **Two callers, and only one of them is gated on the flag.**
    ``_resolve_backflush_components`` (the completion path) reaches this only when the
    finished part opted into ``Part.backflush_components``;
    ``preview_backflush_for_work_order`` calls it UNCONDITIONALLY and on purpose, because
    the operator reading a dry run is deciding whether to opt in and a preview that showed
    nothing until afterwards could not inform that decision. Every line the preview gets
    back carries ``requires_opt_in``.

    The part-level readiness check is NOT a caller: it has a ``Part`` and no work order,
    so it re-states the BOM half at a synthetic basis of 1.0 (see
    ``backflush_readiness_for_part``). A ``basis_override`` parameter existed here for it
    and had no caller anywhere in ``app/`` or ``tests/`` -- it could not have one, since
    this function requires a ``WorkOrder``. Removed rather than left as a comment claiming
    a user it does not have.

    **The basis is ``quantity_complete + quantity_scrapped``, not ``quantity_complete``.**
    A scrapped run physically used its material, and lot genealogy filters on ISSUE, so
    material consumed making scrap must appear as ISSUE or the parts most likely to be
    audited are the ones whose material vanished from the record. This is also the exact
    basis the per-run tie engine has always used (``qty_per_run x (complete + scrapped)``),
    which is what stops one shop from reporting two different consumptions for the same
    physical event depending on whether the material happened to be tied. Consequence: a
    fully-scrapped work order now backflushes, where before it backflushed nothing.

    **The routing no longer wins outright over the BOM.** It used to
    (``if not required:``), so ONE stray ``component_part_id`` on ONE operation silently
    disabled the entire BOM explosion for the whole work order -- and that is not a
    hypothetical: ``_create_assembly_routing_operations`` writes ``component_part_id``
    only for components that HAVE a released routing, so an assembly whose ten BOM lines
    include two routed components lost the other eight. Precedence is now PER PART, which
    is the only scope the routing actually speaks to: an operation naming a component
    states that component's demand and says nothing about the rest of the BOM. Routing
    demand wins for the parts it names; the BOM supplies every part it does not. A
    disagreement between the two on a shared part is logged.

    Suppression then runs in two layers, and BOTH are needed:

    1. ``_drop_allocation_covered_parts`` -- an OPEN operation-scoped tie owns that part's
       demand, including a tie that has not consumed yet (the material is coming).
    2. ``_drop_ledger_covered_parts`` -- the ledger already shows that material gone,
       whatever the tie's status now says.

    Diagnostics are REPORTED here and acted on by the caller: the completion path refuses
    the demand a blocking diagnostic condemns (``_refuse_blocked_demand``), the preview
    renders them, and the opt-in gate joins the blocking ones into its 409. This function
    itself never drops demand over a diagnostic -- a pure read that quietly returned a
    different answer than it reported would be the worst of both.
    """
    basis = _backflush_basis(work_order)
    resolution = _BackflushResolution(basis=basis)
    if basis <= _EPSILON:
        return resolution

    explosion = _BackflushBomExplosion()
    if work_order.part_id is not None:
        # Imported lazily to avoid an import cycle with the endpoints module.
        from app.api.endpoints.work_orders import _get_active_bom

        bom = _get_active_bom(db, work_order.part_id, company_id)
        if bom is not None:
            _record_bom_header_diagnostics(bom, explosion)
            _explode_backflush_bom(
                db,
                bom,
                company_id,
                parent_qty=basis,
                visited_part_ids={bom.part_id},
                out=explosion,
                consumed=True,
            )
    # A part reached as a primary line SOMEWHERE outranks its appearance as an alternate,
    # an optional, or a member of a make subtree elsewhere in the same structure.
    explosion.excluded_part_ids -= set(explosion.demand)
    resolution.diagnostics.extend(explosion.diagnostics)

    required: dict[int, float] = dict(explosion.demand)
    routing = _routing_backflush_demand(db, work_order, company_id, basis=basis, diagnostics=resolution.diagnostics)
    routing_part_numbers = _part_numbers(db, company_id, routing.keys())
    for part_id, quantity in routing.items():
        if part_id in explosion.excluded_part_ids:
            logger.warning(
                "WO %s (company %s) operation demand for part %s is dropped: the BOM reaches that part only "
                "as an alternate/optional/reference line or inside a make sub-assembly",
                work_order.id,
                company_id,
                part_id,
            )
            resolution.diagnostics.append(
                _BackflushDiagnostic(
                    code="routing_component_excluded_by_bom",
                    severity=BACKFLUSH_BLOCKING,
                    detail=(
                        f"a routing operation names component "
                        f"{routing_part_numbers.get(part_id) or part_id}, but the BOM reaches it only as an "
                        "alternate/optional/reference line or inside a make sub-assembly, so that demand "
                        "would be silently dropped. Reconcile the routing and the BOM"
                    ),
                    component_part_id=part_id,
                    component_part_number=routing_part_numbers.get(part_id),
                )
            )
            continue
        bom_quantity = required.get(part_id)
        if bom_quantity is not None and abs(bom_quantity - quantity) > _EPSILON:
            logger.warning(
                "WO %s (company %s) routing and BOM disagree on component %s (routing %s, BOM %s); routing wins",
                work_order.id,
                company_id,
                part_id,
                quantity,
                bom_quantity,
            )
            resolution.diagnostics.append(
                _BackflushDiagnostic(
                    code="routing_bom_quantity_disagreement",
                    severity=BACKFLUSH_BLOCKING,
                    detail=(
                        f"the routing and the BOM disagree on component "
                        f"{routing_part_numbers.get(part_id) or part_id} "
                        f"({quantity:g} routed vs {bom_quantity:g} on the BOM); the routing would win. "
                        "Reconcile them before enabling"
                    ),
                    component_part_id=part_id,
                    component_part_number=routing_part_numbers.get(part_id),
                )
            )
        required[part_id] = quantity

    if not required:
        # ADVISORY at WORK-ORDER scope, blocking only at part-opt-in scope
        # (``backflush_readiness_for_part`` raises its own copy). The condition means
        # "this job has no component demand", which for a single-op turned part, a
        # purchased item or a part-less standalone nest package is the ORDINARY case and
        # nothing is wrong -- the preview would otherwise paint a red "1 problem resolving
        # this demand" banner over a perfectly healthy job, which is the alarm fatigue the
        # whole severity vocabulary exists to avoid. It also must not reach
        # ``_refuse_blocked_demand``'s structural tier, where a no-component blocker
        # refuses the entire leg: there is nothing here to refuse, and "no BOM" is not a
        # reason to distrust a BOM. At OPT-IN it is a real refusal -- arming a part whose
        # completions would consume nothing is a shop believing an automation is running
        # when it is not.
        resolution.diagnostics.append(
            _BackflushDiagnostic(
                code="no_demand_source",
                severity=BACKFLUSH_ADVISORY,
                detail=(
                    "this work order has no active BOM line and no routing operation naming a component that "
                    "this leg would issue, so completing it would consume nothing."
                ),
            )
        )

    # ALLOCATION PRECEDENCE. A part covered by an OPEN operation-scoped material
    # allocation on this WO is owned by the material-consumption engine, which posts
    # per-run ISSUEs with reference_type='work_order_operation'. Those rows are OUTSIDE
    # the uq_wo_inventory_issue predicate, so nothing at the DB level would stop this
    # WO-level backflush from ALSO issuing the same material -- a silent double-issue.
    # Drop those parts here; the allocation is the sole demand carrier (we deliberately
    # do NOT write op.component_part_id from a tie).
    required, resolution.allocation_blocked = _drop_allocation_covered_parts(
        db, work_order, company_id, required, allocations, diagnostics=resolution.diagnostics
    )
    resolution.demand, resolution.ledger_blocked = _drop_ledger_covered_parts(db, work_order, company_id, required)
    return resolution


def _record_bom_header_diagnostics(bom: BOM, out: _BackflushBomExplosion) -> None:
    """Conditions about the ACTIVE BOM itself rather than any one of its lines.

    ``_get_active_bom`` filters ``is_active`` only -- not ``is_deleted`` -- while ``BOM``
    carries ``SoftDeleteMixin``. So a soft-deleted BOM that is still flagged active is
    resolved and exploded exactly as a live one, which is an invariant-3 violation with a
    material consequence rather than a bookkeeping one: the structure the shop believes it
    deleted is the structure that moves stock. Not FIXED here (changing which BOM resolves
    is a change to a live helper with four other callers) -- recorded, so a part sitting
    on one cannot opt in.
    """
    if bool(getattr(bom, "is_deleted", False)):
        out.diagnostics.append(
            _BackflushDiagnostic(
                code="deleted_active_bom",
                severity=BACKFLUSH_BLOCKING,
                detail=(
                    f"the active BOM for this part (BOM {bom.id}) is soft-deleted, so backflush would explode a "
                    "structure the shop has deleted. Restore it or activate a live BOM"
                ),
            )
        )


def _drop_allocation_covered_parts(
    db: Session,
    work_order: WorkOrder,
    company_id: int,
    required: dict[int, float],
    allocations: Optional[list[WorkOrderMaterialAllocation]] = None,
    *,
    diagnostics: Optional[list[_BackflushDiagnostic]] = None,
) -> tuple[dict[int, float], dict[int, float]]:
    """Remove parts covered by an OPEN OPERATION-scoped allocation from backflush demand.

    Returns ``(surviving demand, {part_id: dropped demand})``. The second half is not
    audited and must not be -- nothing was lost, the tie is simply the demand carrier --
    but a dry-run preview that omitted it would show the BOM's material moving twice.

    No allocations -> ``required`` is returned unchanged, and with the list threaded down
    from ``apply_completion_inventory_effects`` that costs ZERO extra queries -- which is
    what keeps an UNTIED work order byte-identical to its pre-feature behavior.

    A tie is only allowed to SUPPRESS the backflush if it can actually consume, so the
    operation it points at must still exist on THIS work order -- exactly the check the
    consume path already makes (``_consume_tied_materials`` skips such a tie). Without
    the symmetry a tie pointing at a vanished operation (a nest re-import that raced the
    cancel, a hand-edited row) suppressed the backflush AND never consumed: the part was
    silently neither issued nor depleted. The operation lookup only runs when an
    operation-scoped tie exists, so an untied WO still costs nothing here.

    This layer is STATUS-keyed, and that is its whole point: an OPEN tie that has not
    consumed a thing still suppresses, because the material is coming. It is no longer the
    only layer -- ``_drop_ledger_covered_parts`` covers the ties this one cannot see.

    **Suppression is all-or-nothing and stays that way.** ``required.pop`` drops the BOM's
    entire demand for the part however little the tie covers. Subtracting instead is not
    available: the tie's target is ``qty_per_run x runs`` on ONE operation and the BOM's
    demand is per finished unit across the whole job, so a difference between them is two
    incompatible bases, not a shortfall, and netting them would post a quantity nobody
    authored. What the difference IS is worth seeing, so it is logged -- a tie whose live
    target is nowhere near the BOM demand it just cancelled is a planning error somebody
    should look at, and before this it produced no signal at all.
    """
    dropped: dict[int, float] = {}
    if not required:
        return required, dropped

    allocations = [a for a in _open_allocations(db, work_order, company_id, allocations) if a.part_id in required]
    operation_ids = {a.work_order_operation_id for a in allocations if a.work_order_operation_id is not None}
    if not operation_ids:
        return required, dropped

    diagnostics = diagnostics if diagnostics is not None else []
    part_numbers = _part_numbers(db, company_id, (a.part_id for a in allocations))
    live_operations = {
        row.id: row
        for row in db.query(
            WorkOrderOperation.id,
            WorkOrderOperation.quantity_complete,
            WorkOrderOperation.quantity_scrapped,
        )
        .filter(
            WorkOrderOperation.company_id == company_id,
            WorkOrderOperation.work_order_id == work_order.id,
            WorkOrderOperation.id.in_(operation_ids),
        )
        .all()
    }
    for allocation in allocations:
        operation = live_operations.get(allocation.work_order_operation_id)
        if operation is not None:
            demand = required.pop(allocation.part_id, None)
            if demand is None:
                continue
            dropped[allocation.part_id] = demand
            per_run = float(allocation.qty_per_run if allocation.qty_per_run is not None else 1.0)
            tie_target = per_run * (float(operation.quantity_complete or 0) + float(operation.quantity_scrapped or 0))
            if abs(tie_target - demand) > _EPSILON:
                logger.warning(
                    "Allocation %s suppresses %s of part %s on WO %s (company %s) but its own live target is "
                    "%s; the tie's per-run basis and the BOM's per-unit demand disagree",
                    allocation.id,
                    demand,
                    allocation.part_id,
                    work_order.id,
                    company_id,
                    tie_target,
                )
                diagnostics.append(
                    _BackflushDiagnostic(
                        code="tie_basis_mismatch",
                        severity=BACKFLUSH_ADVISORY,
                        detail=(
                            f"Material tie {allocation.id} cancels {demand:g} of BOM demand for "
                            f"{part_numbers.get(allocation.part_id) or allocation.part_id} but its own live "
                            f"target is {tie_target:g}; the tie's per-run basis and the BOM's per-unit demand "
                            "disagree."
                        ),
                        component_part_id=allocation.part_id,
                        component_part_number=part_numbers.get(allocation.part_id),
                        operation_id=allocation.work_order_operation_id,
                    )
                )
        elif allocation.work_order_operation_id is not None:
            logger.warning(
                "Allocation %s points at operation %s which is not on WO %s (company %s); "
                "backflush demand for part %s is NOT suppressed",
                allocation.id,
                allocation.work_order_operation_id,
                work_order.id,
                company_id,
                allocation.part_id,
            )
            diagnostics.append(
                _BackflushDiagnostic(
                    code="tie_operation_missing",
                    severity=BACKFLUSH_ADVISORY,
                    detail=(
                        f"Material tie {allocation.id} points at operation "
                        f"{allocation.work_order_operation_id}, which is no longer on this work order, so it "
                        f"cannot consume and does not suppress backflush demand for "
                        f"{part_numbers.get(allocation.part_id) or allocation.part_id}."
                    ),
                    component_part_id=allocation.part_id,
                    component_part_number=part_numbers.get(allocation.part_id),
                    operation_id=allocation.work_order_operation_id,
                )
            )
    return required, dropped


def _net_issued_by_part(
    db: Session,
    *,
    company_id: int,
    part_ids: Iterable[int],
    reference_clause: ColumnElement[bool],
) -> dict[int, float]:
    """Signed ISSUE - RETURN per part over the ledger rows ``reference_clause`` selects.

    ONE signed-``CASE`` body serving every reader that asks "how much of this part has
    already left stock under <this reference shape>?". The shape is the caller's
    parameter precisely so the two public wrappers below cannot drift into two different
    ways of netting returns.

    The SIGN is keyed on ``transaction_type``, never on the stored sign of ``quantity``
    (ISSUE stores it negative, RETURN positive) -- the rule every other reader that learned
    to net returns follows, and the one that stops a credit being counted as more
    consumption. A dataset with no RETURN rows is numerically identical to a plain
    ``SUM(ABS(quantity))``.

    Tenant-scoped (invariant #1); the caller's clause must be tenant-scoped too where it
    resolves ids through a subquery. Empty input short-circuits to no query at all.
    """
    ids = [int(part_id) for part_id in part_ids]
    if not ids:
        return {}
    rows = (
        db.query(
            InventoryTransaction.part_id,
            func.sum(
                case(
                    (
                        InventoryTransaction.transaction_type == TransactionType.RETURN,
                        -func.abs(InventoryTransaction.quantity),
                    ),
                    else_=func.abs(InventoryTransaction.quantity),
                )
            ),
        )
        .filter(
            InventoryTransaction.company_id == company_id,
            InventoryTransaction.part_id.in_(ids),
            InventoryTransaction.transaction_type.in_((TransactionType.ISSUE, TransactionType.RETURN)),
            reference_clause,
        )
        .group_by(InventoryTransaction.part_id)
        .all()
    )
    return {part_id: float(total or 0.0) for part_id, total in rows}


def operation_scoped_net_issued_by_part(
    db: Session,
    *,
    work_order_id: int,
    company_id: int,
    part_ids: Iterable[int],
) -> dict[int, float]:
    """Signed ISSUE - RETURN per part, over this WO's OPERATION-scoped ledger rows.

    The reference shape is ``('work_order_operation', <an operation on this work order>)``
    -- the same resolution ``work_order_ledger_filter`` performs, narrowed to the one
    shape that matters here.

    **It MUST NOT learn any other shape, and that is load-bearing rather than tidy.** Its
    one caller is ``_drop_ledger_covered_parts``, which reads a positive net as "a tie
    already drew this part" and suppresses the BOM's demand for it, recording a
    ``BACKFLUSH_DOUBLE_ISSUE_BLOCKED`` chain row. If it also matched
    ``work_order_backflush``, the BOM leg's OWN first post would suppress every later
    pass and emit a spurious blocked row -- the leg silencing itself. (The legacy
    ``work_order`` shape is excluded for the older version of the same reason: those rows
    are covered by ``_component_already_issued``, and folding them in would turn an
    ordinary idempotent replay into a blocked-row-per-re-entry.)

    Tenant-scoped on the outer predicate and the operation subquery alike (invariant #1).
    Empty input short-circuits to no query at all.
    """
    operation_ids = select(WorkOrderOperation.id).where(
        WorkOrderOperation.company_id == company_id,
        WorkOrderOperation.work_order_id == work_order_id,
    )
    return _net_issued_by_part(
        db,
        company_id=company_id,
        part_ids=part_ids,
        reference_clause=and_(
            InventoryTransaction.reference_type == OPERATION_REFERENCE_TYPE,
            InventoryTransaction.reference_id.in_(operation_ids),
        ),
    )


def backflush_net_issued_by_part(
    db: Session,
    *,
    work_order_id: int,
    company_id: int,
    part_ids: Iterable[int],
) -> dict[int, float]:
    """Signed ISSUE - RETURN per part over the BOM leg's OWN history on this work order.

    ``reference_type='work_order_backflush' AND reference_id=work_order_id AND
    allocation_id IS NULL`` -- the ``posted`` term of the BOM leg's
    ``delta = target - posted``, which is what replaces the old existence-keyed one-shot
    guard with reconcile-to-target.

    **The ``allocation_id IS NULL`` filter is what keeps the two nets DISJOINT.** Every
    tie-driven row under this reference type carries a non-NULL ``allocation_id`` (both
    ``_write_issue_txn`` and ``_write_return_txn`` stamp or copy it, and migration 075
    shipped the column in the same commit as the tie engine, so no tie-driven row can
    predate it). So the BOM net can never see a tie's rows, and
    ``net_consumed_quantity_for_allocation`` -- which is ``allocation_id``-keyed and
    shape-agnostic -- can never see the BOM's.

    Tenant-scoped (invariant #1); index-backed by ``ix_inventory_txn_company_reference``
    (migration 075). Empty input short-circuits to no query at all.
    """
    return _net_issued_by_part(
        db,
        company_id=company_id,
        part_ids=part_ids,
        reference_clause=and_(
            InventoryTransaction.reference_type == BACKFLUSH_REFERENCE_TYPE,
            InventoryTransaction.reference_id == work_order_id,
            InventoryTransaction.allocation_id.is_(None),
        ),
    )


def net_consumed_quantity_for_allocation(db: Session, *, allocation_id: int, company_id: int) -> float:
    """Signed ISSUE - RETURN the ledger holds against ONE material tie. Tenant-scoped.

    The authoritative answer to "is this tie still holding material?", as opposed to
    ``WorkOrderMaterialAllocation.qty_consumed``, which the plan documents as a CACHE.
    Guards whose refusal protects the ledger must read the ledger: hard delete has since
    PR 1, nest re-import since PR 3, and the manual untie since PR 4.

    **It is also now the WRITER of that cache -- for WORK-ORDER-scoped ties only**, not
    merely the check against it: PR 4.4's ``_advance_tie_consumed`` sets ``qty_consumed``
    to this value after posting, where its predecessor wrote ``qty_planned`` regardless of
    what actually landed. Cache == net by construction, so the two verbs that key on the
    cache exactly -- ``return_and_untie`` giving back precisely ``qty_consumed``, and
    ``correct_over_consumption``'s ``qty_consumed - target`` allowance -- are exact rather
    than approximately right.

    **The OPERATION-scoped engine has NOT adopted this, and the asymmetry is real.**
    ``material_consumption_service._consume_one_allocation`` still writes
    ``qty_consumed = target`` (its run-scaled ``qty_per_run x (complete + scrapped)``), so
    the two engines now mean different things by the same column: on a work-order-scoped
    tie it is a RECORD of what the ledger holds, on an operation-scoped tie it remains the
    CLAIM the sum-delta arithmetic is computed from. That is why the fourth residual is
    closed on one engine and not both, and why a reader of either engine must not assume
    the other's meaning. (Closing it on the per-run engine is not a docstring's decision:
    that engine's ``delta`` is ``target - qty_consumed``, so re-pointing the cache at the
    ledger changes the arithmetic itself, not just what is stored.)

    It is ALSO the ``posted`` term of leg 2's ``delta = qty_planned - posted``:
    shape-agnostic and ``allocation_id``-keyed, so it can never see the BOM leg's rows
    (those carry a NULL ``allocation_id``) and the two nets stay disjoint.

    Signed, not existence-keyed. ``ledger_backed_allocation_ids`` answers a different
    question -- "would deleting this row orphan a ledger reference?" -- for which mere
    existence is correct, because a RETURN row references the tie just as durably as the
    ISSUE it compensates. Here the question is whether material is still OUT, and a tie
    that gave everything back is holding none.
    """
    total = (
        db.query(
            func.coalesce(
                func.sum(
                    case(
                        (
                            InventoryTransaction.transaction_type == TransactionType.RETURN,
                            -func.abs(InventoryTransaction.quantity),
                        ),
                        else_=func.abs(InventoryTransaction.quantity),
                    )
                ),
                0.0,
            )
        )
        .filter(
            InventoryTransaction.company_id == company_id,
            InventoryTransaction.allocation_id == allocation_id,
            InventoryTransaction.transaction_type.in_((TransactionType.ISSUE, TransactionType.RETURN)),
        )
        .scalar()
    )
    return float(total or 0.0)


def _drop_ledger_covered_parts(
    db: Session,
    work_order: WorkOrder,
    company_id: int,
    required: dict[int, float],
) -> tuple[dict[int, float], dict[int, tuple[float, float]]]:
    """Suppress backflush demand for a part the LEDGER already shows leaving this job.

    **PURE READ, and that is a structural property rather than a habit.** It returns
    ``(surviving demand, {part_id: (suppressed demand, ledger net)})`` and takes no
    ``AuditService`` at all; ``_resolve_backflush_components`` -- the write-path wrapper --
    turns the second half into ``BACKFLUSH_DOUBLE_ISSUE_BLOCKED`` rows. It used to write
    them itself, which meant a caller could not ask "what would this completion do?"
    without inserting hash-chain rows claiming it HAD, and falsified the cardinality this
    docstring asserts below. Splitting the decision from the record is what makes "a read
    never writes" true by construction; an ``audit=None`` parameter would have made it
    true only as long as every future caller remembered to pass it.

    The double-issue ``_drop_allocation_covered_parts`` cannot prevent on its own. That
    layer keys on tie STATUS, and three guards in a row miss the same case:

    * a tie that CONSUMED and is no longer ``OPEN`` is invisible to it;
    * ``_component_already_issued`` keys on ``reference_type='work_order'`` ISSUE rows and
      therefore cannot see tied consumption at all (it posts under
      ``work_order_operation``);
    * ``uq_wo_inventory_issue``'s partial predicate does not cover those rows either --
      that is the whole reason the reference type was split.

    **It reads ``operation_scoped_net_issued_by_part`` and nothing else, forever.** The
    BOM leg now posts its own rows under ``work_order_backflush``; teaching this
    suppression that shape would make the leg suppress ITSELF on every pass after the
    first, and emit a spurious ``BACKFLUSH_DOUBLE_ISSUE_BLOCKED`` row while doing it.
    The BOM leg's own convergence is handled arithmetically, one layer down, by
    ``backflush_net_issued_by_part``.

    So the same part leaves stock twice, and the as-built record carries two lines naming
    two DIFFERENT lots for one physical consumption (AS9100D 8.5.2). It is reachable
    through supported verbs: ``cancel_open_allocations_for_work_order`` cancels a tie
    regardless of ``qty_consumed``, and a restore only re-opens ties whose most recent
    DELETE audit row carries the delete's own reason -- so a missing or detached audit row,
    or a cancel that came from anywhere else, leaves a consumed tie CANCELLED forever.

    Keyed on the LEDGER because ``status`` and ``qty_consumed`` are both documented as
    non-authoritative planning state; every other guard of comparable consequence (hard
    delete since PR 1, nest re-import since PR 3) already reads it for the same reason.

    **A fully-returned tie nets to zero and IS permitted to re-issue. That is a decision,
    not an accident of the arithmetic.** PR 3's ``return_and_untie`` credits the material
    back to its source lots; the job then holds none of it, and the BOM's demand for that
    part is once again unmet. Suppressing on the mere EXISTENCE of ledger rows would leave
    the part permanently un-issuable on a job that genuinely gave the material back --
    refusing to consume material the shop is holding, and hiding the demand from the
    shortage machinery that would otherwise surface it.

    Suppression is RECORDED (``BACKFLUSH_DOUBLE_ISSUE_BLOCKED``) rather than silent. The
    row is bounded, not per-request: this fires on the completion paths and on a reconcile
    pass that actually applies a work-order transition, the same cardinality as
    ``BACKFLUSH_SHORTAGE``, not on every read.

    One known blind spot, inherited from ``work_order_ledger_filter`` and not introduced
    here: operation ids resolve through a LIVE subquery, so rows naming an operation a nest
    re-import deleted drop out of it. The structural fix is superseding operations instead
    of deleting them (see the plan's fourth residual gap).
    """
    suppressed: dict[int, tuple[float, float]] = {}
    if not required:
        return required, suppressed

    nets = operation_scoped_net_issued_by_part(
        db, work_order_id=work_order.id, company_id=company_id, part_ids=required.keys()
    )
    blocked = {part_id: net for part_id, net in nets.items() if net > _EPSILON and part_id in required}
    if not blocked:
        return required, suppressed

    for part_id, net in blocked.items():
        demand = required.pop(part_id)
        suppressed[part_id] = (demand, net)
        logger.warning(
            "Backflush demand of %s for part %s on WO %s (company %s) suppressed: the ledger already shows "
            "%s consumed against this work order's operations",
            demand,
            part_id,
            work_order.id,
            company_id,
            net,
        )
    return required, suppressed


def _record_ledger_suppression(
    work_order: WorkOrder,
    *,
    part_id: int,
    part_number: Optional[str],
    demand: float,
    net: float,
    company_id: int,
    audit: AuditService,
) -> None:
    """The ``BACKFLUSH_DOUBLE_ISSUE_BLOCKED`` chain row for ONE ledger-suppressed part.

    The WRITE half of what ``_drop_ledger_covered_parts`` decides, lifted out of it so
    the decision stays callable from a read. The row content is byte-for-byte what that
    function used to emit -- this is a relocation, not a rewording, because the prose and
    the ``extra_data`` keys are what an as-built review reads months later.

    Suppression is recorded rather than left silent: it is the system declining to issue
    material a planner's BOM asked for, and "correct but invisible" is exactly the shape
    of control gap an AS9100D 8.5.2 review cannot reconstruct after the fact. The row is
    bounded, not per-request -- it fires on the completion paths and on a reconcile pass
    that actually applies a work-order transition, the same cardinality as
    ``BACKFLUSH_SHORTAGE``, and (now structurally) never on a read.
    """
    audit.log(
        action=BACKFLUSH_DOUBLE_ISSUE_BLOCKED_AUDIT_ACTION,
        resource_type="inventory",
        resource_id=part_id,
        resource_identifier=part_number or str(part_id),
        description=(
            f"Backflush of {demand} of component {part_number or part_id} on work order "
            f"{work_order.work_order_number} was blocked: {net} has already been consumed against "
            "this work order's operations by a material tie"
        ),
        new_values={"suppressed_quantity": demand, "ledger_net_issued": net},
        extra_data={
            "work_order_id": work_order.id,
            "work_order_number": work_order.work_order_number,
            "component_part_id": part_id,
            "component_part_number": part_number,
            "suppressed_quantity": demand,
            "ledger_net_issued": net,
            "reference_type": OPERATION_REFERENCE_TYPE,
        },
        company_id=company_id,
    )


_AnchorT = TypeVar("_AnchorT")


def _shortfall_anchor(last_drawn: Optional[_AnchorT], source_items: list[_AnchorT]) -> Optional[_AnchorT]:
    """The stock row an UNMET REMAINDER is posted against. ONE definition, two callers.

    A draw that ``plan_stock_draw`` could not cover in full still posts the remainder --
    the true demand belongs on the ledger even when the stock does not exist -- against
    the last lot it drew, or the first candidate lot if it drew nothing, or (``None`` here)
    a minted placeholder row when there is no candidate at all.

    Extracted because the DRY RUN has to make the identical choice. It previously did not
    make it at all: the preview listed only ``plan_stock_draw``'s draws and reported the
    remainder as a bare scalar, so a 25 demand over lots A(10) and B(5) previewed
    "A:10, B:5, short 10" while the completion posted A:10, B:5 **and B:10** -- lot B
    contributing 15 and ending at -10. The lot number on that third row goes onto the
    as-built genealogy, which is the one thing this preview exists to get right, and three
    separate docstrings promised the two could not disagree.
    """
    if last_drawn is not None:
        return last_drawn
    return source_items[0] if source_items else None


def _placeholder_stock_row(
    db: Session,
    *,
    part_id: int,
    company_id: int,
    unit_cost: float,
) -> InventoryItem:
    """A zero-quantity stock row to carry a consumption when no lot exists at all.

    An ``InventoryTransaction`` with a dangling ``inventory_item_id`` would be worse than
    a negative on-hand, so the negative movement is still recorded against a REAL row.
    Shared by the backflush leg and the per-run consumption engine (which had its own
    byte-for-byte copy of this).
    """
    item = InventoryItem(
        part_id=part_id,
        location=FINISHED_GOODS_LOCATION,
        warehouse=FINISHED_GOODS_WAREHOUSE,
        quantity_on_hand=0.0,
        quantity_allocated=0.0,
        quantity_available=0.0,
        unit_cost=unit_cost,
        received_date=datetime.utcnow(),
        status="available",
    )
    item.company_id = company_id
    db.add(item)
    db.flush()
    return item


def _is_placeholder_stock_row(item: InventoryItem) -> bool:
    """True when a stock row has the shape ``_placeholder_stock_row`` creates.

    Lives next to its constructor deliberately: the two definitions must move together,
    and the reader that needs this is in another module (the RETURN engine).

    A placeholder is a LOT-LESS row at the finished-goods location, minted only so a
    consumption with no stock at all still has a real ``inventory_item_id`` to point at.
    It names no heat and carries no cert, so it is a fine sink for a negative movement
    and a bad SOURCE for a positive one: crediting material back into it would create
    unlabeled, FIFO-eligible stock out of a row that exists purely as a ledger anchor
    (AS9100D 8.5.2). The RETURN engine refuses rather than guessing.

    Identification is by SHAPE, not by a flag -- there is no column marking these -- so it
    matches the constructor's FULL shape (location AND warehouse), not a prefix of it.
    Matching on location alone OVER-matches, and not hypothetically: the finished-goods
    RECEIPT path mints genuine stock at ``FINISHED-GOODS`` with
    ``lot_number = work_order.lot_number``, which is NULL for any work order carrying no
    lot. A sub-assembly that is both produced and consumed can therefore hold real,
    lot-less finished-goods stock -- and calling that a placeholder would 409 a return
    that should have been allowed. The failure direction was safe (refuse, never
    mis-credit), but a refusal a user cannot act on is still a defect.
    """
    return (
        not item.lot_number and item.location == FINISHED_GOODS_LOCATION and item.warehouse == FINISHED_GOODS_WAREHOUSE
    )


def _issue_one_component(
    db: Session,
    work_order: WorkOrder,
    *,
    component_part_id: int,
    required_qty: float,
    company_id: int,
    user_id: int,
    audit: AuditService,
    allocation_id: Optional[int] = None,
    pinned_inventory_item_id: Optional[int] = None,
) -> IssueOutcome:
    """Draw ``required_qty`` of one component off stock: N ISSUE rows + N decrements.

    The reconciling twin of ``_consume_one_allocation``, and deliberately built to its
    shape. Every row posts ``reference_type='work_order_backflush', reference_id=
    work_order.id`` -- OUTSIDE ``uq_wo_inventory_issue``, which is what makes N rows per
    (work order, part) legal in the first place.

    **Lot selection is now ONE policy, shared with the per-run engine.** Unpinned demand
    walks ``consumable_source_items`` -- ``received_date`` FIFO over active, consumable,
    on-hand lots -- and spills across as many as the demand needs, so the as-built record
    names every heat that actually went into the part. The historical behaviour it
    replaces (lowest ``id``, ``status`` ignored entirely, ONE row for the whole quantity)
    could name a different heat than the per-run engine would for the same physical draw.
    The predicate is NULL-status-tolerant, so the widening does not hide legacy stock.

    ``pinned_inventory_item_id`` (a work-order-scoped tie's LOT PIN) consumes from THAT
    lot exclusively -- pinning is a lot-directed instruction, so an insufficient pinned
    lot is driven negative rather than silently spilling onto a different, uncertified,
    wrong-heat lot. The pin bypasses the ORDERING, not the hold check. **Row shape
    changed here and it is stated, not hidden:** an under-covered pinned draw now posts a
    ``take`` row plus a separate ``(SHORT n)`` row against the SAME pinned lot, exactly as
    ``_consume_one_allocation`` does, instead of one row for the full quantity. Same lot,
    same total, at most two rows; ``_plan_material_return`` walks N rows natively.

    **Held lots are SKIPPED, not silently consumed, and the skip is DISCLOSED.** That is
    the AS9100D 8.7-correct behaviour and it is what "adopt the per-run engine's rule"
    means. The unpinned leg can no longer pick an ``on_hold`` / ``quarantine`` /
    ``rejected`` lot at all, so its ``HELD_MATERIAL_CONSUMED`` row is gone; instead
    ``held_stock_summary`` puts the skipped quantity and its lot numbers on the SHORTAGE
    row, so a part whose stock is entirely segregated cannot report a bare shortage
    against material sitting on the rack. The PINNED leg still consumes a held lot and
    still writes ``HELD_MATERIAL_CONSUMED(pinned=True)`` -- the tie endpoint refuses to
    pin a held lot, so that row can only ever mean "held after it was pinned".

    **``available_total`` is summed over the lots actually walked and ``shortfall`` is the
    planner's remainder.** The previous form computed ``required - available_total`` from
    a single-lot draw against a multi-lot total, so a part with 10 in each of five lots
    and a demand of 25 went 15 negative on ONE lot with no ``ComponentShortage``, no
    ``BACKFLUSH_SHORTAGE`` chain row and no event. That is structurally impossible now.

    Posts with ``duplicate_is_noop=False``: no index covers this shape, so an
    ``IntegrityError`` is a real fault and must reach the per-component savepoint
    (``_issue_component_under_savepoint``), never be mistaken for a concurrent duplicate.
    A shortage still NEVER raises -- it is recorded and reported.
    """
    part = db.query(Part).filter(Part.id == component_part_id, Part.company_id == company_id).first()
    part_number = part.part_number if part else None
    unit_cost = float(part.standard_cost or 0) if part else 0.0

    # Imported lazily: ``material_consumption_service`` imports helpers from THIS module.
    from app.services.material_consumption_service import (
        consumable_source_items,
        is_consumable_item,
        plan_stock_draw,
        record_held_material_consumed,
        shortage_draw_disclosure,
    )

    held_item: Optional[InventoryItem] = None
    pinned: Optional[InventoryItem] = None
    if pinned_inventory_item_id is not None:
        # FOR UPDATE: the on-hand read-modify-write below must hold the row lock --
        # two completions of DIFFERENT work orders drawing this lot are otherwise
        # unserialized (the WO optimistic lock only covers same-WO). No-op on SQLite.
        pinned = (
            db.query(InventoryItem)
            .filter(
                InventoryItem.id == pinned_inventory_item_id,
                InventoryItem.company_id == company_id,
            )
            .with_for_update()
            .first()
        )
        source_items = [pinned] if pinned is not None else []
        if pinned is not None and not is_consumable_item(pinned):
            # Held AFTER it was pinned (the tie endpoint refuses to pin a held lot).
            # Consume anyway -- refusing from a reconcile-on-read GET would be
            # unattributable -- but never silently.
            held_item = pinned
    else:
        source_items = consumable_source_items(db, component_part_id, company_id, for_update=True)

    available_total = sum(float(i.quantity_on_hand or 0) for i in source_items)
    draws, shortfall = plan_stock_draw(source_items, required_qty)

    outcome = IssueOutcome()
    last_item: Optional[InventoryItem] = None
    for item, take in draws:
        require_posted_issue(
            _write_issue_txn(
                db,
                work_order,
                inventory_item=item,
                component_part_id=component_part_id,
                quantity=take,
                unit_cost=float(item.unit_cost or unit_cost),
                lot_number=item.lot_number,
                company_id=company_id,
                user_id=user_id,
                audit=audit,
                part_number=part_number,
                allocation_id=allocation_id,
                reference_type=BACKFLUSH_REFERENCE_TYPE,
                reference_id=work_order.id,
                duplicate_is_noop=False,
            ),
            what=f"Backflush consumption of part {component_part_id} on work order {work_order.id}",
        )
        last_item = item
        outcome.posted_any = True

    if shortfall > _EPSILON:
        # SHORTAGE: never fail the completion. Drive a lot negative so the true demand is
        # still on the ledger, then record it tamper-evidently + emit the warning.
        #
        # The anchor guard stays in THIS form, now SHARED with the dry run
        # (``_shortfall_anchor``) so the two cannot pick different heats. ``source_items``
        # is empty on exactly the no-stock-at-all path -- which IS a shortage path -- so a
        # bare ``source_items[0]`` would raise ``IndexError`` precisely when this branch
        # runs.
        anchor = _shortfall_anchor(last_item, source_items)
        if anchor is None:
            anchor = _placeholder_stock_row(db, part_id=component_part_id, company_id=company_id, unit_cost=unit_cost)
        require_posted_issue(
            _write_issue_txn(
                db,
                work_order,
                inventory_item=anchor,
                component_part_id=component_part_id,
                quantity=shortfall,
                unit_cost=float(anchor.unit_cost or unit_cost),
                lot_number=anchor.lot_number,
                company_id=company_id,
                user_id=user_id,
                audit=audit,
                part_number=part_number,
                allocation_id=allocation_id,
                reference_type=BACKFLUSH_REFERENCE_TYPE,
                reference_id=work_order.id,
                notes=(f"Backflush consumption for work order {work_order.work_order_number} " f"(SHORT {shortfall})"),
                duplicate_is_noop=False,
            ),
            what=f"Backflush shortage row for part {component_part_id} on work order {work_order.id}",
        )
        outcome.posted_any = True

        # AT MOST one extra query, only on this branch: on an UNPINNED draw, the stock the
        # predicate passed over, so the chain row cannot report a bare shortage against
        # segregated material. On a PINNED draw the pin is the constraint, not any lot's
        # status -- no query, and the clause names the restriction instead.
        held_quantity_skipped, held_lot_numbers, pinned_lot = shortage_draw_disclosure(
            db,
            part_id=component_part_id,
            company_id=company_id,
            pinned_inventory_item_id=pinned_inventory_item_id,
            pinned_item=pinned,
        )
        outcome.shortage = ComponentShortage(
            part_id=component_part_id,
            part_number=part_number,
            required_quantity=required_qty,
            available_quantity=available_total,
            shortfall=shortfall,
        )
        logger.warning(
            "Backflush shortage on WO %s component %s (company %s): required %s, available %s, short %s",
            work_order.id,
            component_part_id,
            company_id,
            required_qty,
            available_total,
            shortfall,
        )
        # Item 3: a negative on-hand is a regulated material-trail control gap, so
        # record the shortage tamper-evidently (audit_log hash chain) AND emit a
        # warning OperationalEvent -- not just a log line. Atomic with the completion
        # on the live paths (both flush, never commit). The consumed source lot of
        # the driven-negative row is carried for genealogy.
        _record_backflush_shortage(
            db,
            work_order,
            shortage=outcome.shortage,
            consumed_lot=anchor.lot_number,
            company_id=company_id,
            user_id=user_id,
            audit=audit,
            held_quantity_skipped=held_quantity_skipped,
            held_lot_numbers=held_lot_numbers,
            pinned_lot=pinned_lot,
        )

    if held_item is not None:
        # A PIN has exactly ONE source lot, so the whole demand came off the held lot --
        # whether through the normal take or by driving it negative. No ``posted_any``
        # guard: both callers gate on ``delta > _EPSILON``, and a positive demand always
        # produces a row (a take, a SHORT row, or both), so the guard was vacuous rather
        # than protective. This helper has no internal failure path -- a raise propagates
        # to the caller's savepoint -- so reaching here means something posted.
        record_held_material_consumed(
            work_order=work_order,
            operation=None,
            allocation_id=allocation_id,
            part_id=component_part_id,
            item=held_item,
            part_number=part_number,
            quantity=required_qty,
            company_id=company_id,
            audit=audit,
            pinned=True,
        )

    return outcome


def _record_backflush_shortage(
    db: Session,
    work_order: WorkOrder,
    *,
    shortage: ComponentShortage,
    consumed_lot: Optional[str],
    company_id: int,
    user_id: int,
    audit: AuditService,
    held_quantity_skipped: float = 0.0,
    held_lot_numbers: Optional[list[str]] = None,
    pinned_lot: Optional[str] = None,
) -> None:
    """Persist a backflush shortage as a tamper-evident audit row + OperationalEvent (item 3).

    Writes ONE ``audit_log`` row (action ``BACKFLUSH_SHORTAGE``) on the component part,
    carrying the shortfall qty + consumed lot + the producing WO in ``extra_data`` so the
    negative on-hand is on the immutable hash chain (never written directly). Then emits a
    ``backflush_shortage`` ``OperationalEvent`` (``severity="warning"``) for AI/realtime
    consumers -- which the notification catalog maps to ``material.backflush_shortage``.
    Tenant-scoped (``company_id`` on both). The audit ``log`` and the event ``emit`` both
    only flush (never commit), so the records land atomically with the completion on the
    live paths; the event emit is wrapped so a transient signal failure can never fail an
    in-flight completion (the audit row is the compliance record).

    ``held_quantity_skipped`` / ``held_lot_numbers`` disclose stock the draw PASSED OVER
    (``held_stock_summary``). The leg now skips held / inactive lots rather than
    consuming them, so without this a part whose stock is entirely segregated would
    report a bare shortage against material physically on the rack -- and the reader
    could not tell a purchasing problem from an MRB problem. ``pinned_lot`` is the
    mutually exclusive alternative, populated on a PINNED draw where the pin -- not any
    lot's status -- is why nothing else was drawn; ``shortage_draw_disclosure`` decides
    between them. Written through the same helper as the tie engine's shortage row so the
    two prose forms cannot drift.

    **Reading ``held_quantity_skipped`` from ``extra_data``: 0.0 means NOT MEASURED on a
    pinned draw, not "none held".** The pinned branch never runs the held-stock query at
    all (it would answer a question nobody asked -- the pin, not status, is why other
    stock was excluded), so it writes the zero default. The prose is unambiguous and
    ``pinned_lot`` is non-NULL exactly there, so a STRUCTURED consumer -- analytics, an
    AI sensor, a report summing segregated-stock incidents -- must gate on
    ``pinned_lot is None`` before trusting the number, or it will under-count.
    """
    from app.services.material_consumption_service import held_stock_disclosure

    held_lot_numbers = held_lot_numbers or []
    extra = {
        "work_order_id": work_order.id,
        "work_order_number": work_order.work_order_number,
        "component_part_id": shortage.part_id,
        "component_part_number": shortage.part_number,
        "required_quantity": shortage.required_quantity,
        "available_quantity": shortage.available_quantity,
        "shortfall": shortage.shortfall,
        "consumed_lot": consumed_lot,
        "held_quantity_skipped": held_quantity_skipped,
        "held_lot_numbers": held_lot_numbers,
        "pinned_lot": pinned_lot,
    }
    audit.log(
        action=BACKFLUSH_SHORTAGE_AUDIT_ACTION,
        resource_type="inventory",
        resource_id=shortage.part_id,
        resource_identifier=shortage.part_number or str(shortage.part_id),
        description=(
            f"Backflush shortage on WO {work_order.work_order_number}: component "
            f"{shortage.part_number or shortage.part_id} short {shortage.shortfall} "
            f"(required {shortage.required_quantity}, available {shortage.available_quantity})"
            + (f", lot {consumed_lot}" if consumed_lot else "")
            + held_stock_disclosure(held_quantity_skipped, held_lot_numbers, pinned_lot)
        ),
        new_values={"shortfall": shortage.shortfall},
        extra_data=extra,
        company_id=company_id,
    )
    try:
        OperationalEventService(db).emit(
            company_id=company_id,
            event_type=BACKFLUSH_SHORTAGE_EVENT_TYPE,
            source_module="completion_inventory",
            entity_type="inventory",
            entity_id=shortage.part_id,
            work_order_id=work_order.id,
            user_id=user_id,
            severity="warning",
            event_payload=extra,
        )
    except Exception:  # pragma: no cover - a warning signal must never fail a completion
        # The audit row above is the compliance record; the operational event is an
        # AI/realtime convenience. Swallow any emit failure so the completion the caller
        # is committing is unaffected (mirrors the quality-gate exception pattern).
        logger.exception(
            "backflush_shortage event emit failed for WO %s component %s (company %s)",
            work_order.id,
            shortage.part_id,
            company_id,
        )


def require_posted_issue(txn: Optional[InventoryTransaction], *, what: str) -> InventoryTransaction:
    """Narrow ``_write_issue_txn``'s Optional return to the row that must have landed.

    ONE posture for both component legs, replacing two that disagreed: the tie engine
    raised ``RuntimeError`` on a ``None`` while the backflush discarded the return
    entirely. Same invariant, so it now has one spelling.

    Under ``duplicate_is_noop=False`` -- which EVERY component ISSUE passes, since no
    unique index covers either the ``work_order_backflush`` or the
    ``work_order_operation`` shape -- ``_write_issue_txn`` either returns the row or
    raises. A ``None`` therefore means the insert discipline was changed without updating
    the caller, and raising is the honest response: it lands in the caller's per-component
    / per-allocation savepoint and becomes a ``BACKFLUSH_COMPONENT_FAILED`` /
    ``ALLOCATION_CONSUMPTION_FAILED`` chain row, rather than a silent under-consumption
    with no record at all.
    """
    if txn is None:  # pragma: no cover - unreachable under duplicate_is_noop=False
        raise RuntimeError(f"{what} was not written")
    return txn


def _write_issue_txn(
    db: Session,
    work_order: WorkOrder,
    *,
    inventory_item: InventoryItem,
    component_part_id: int,
    quantity: float,
    unit_cost: float,
    lot_number: Optional[str],
    company_id: int,
    user_id: int,
    audit: AuditService,
    part_number: Optional[str],
    reference_type: str,
    duplicate_is_noop: bool,
    allocation_id: Optional[int] = None,
    reference_id: Optional[int] = None,
    notes: Optional[str] = None,
    movement_verb: str = "Backflushed",
    movement_label: str = "Backflush",
    movement_suffix: str = "",
    extra_data: Optional[dict] = None,
) -> Optional[InventoryTransaction]:
    """Write one negative ISSUE txn (carrying the consumed lot), decrement, + audit.

    THE single way material leaves stock against a work order. The per-run consumption
    engine used to carry a near-verbatim copy of this (``_post_consumption_txn``)
    differing only in reference shape, notes and description -- while its module docstring
    claimed it "REUSES its helpers rather than reimplementing them". The variable parts
    are now parameters:

    * ``reference_type`` / ``reference_id`` -- ``('work_order_backflush', work_order.id)``
      for the reconciling component leg (BOM demand and work-order-scoped ties alike),
      ``('work_order_operation', operation.id)`` for per-run consumption. BOTH sit outside
      the ``uq_wo_inventory_*`` predicates by design;
    * ``notes`` -- the ledger row's own note (defaults to the backflush wording);
    * ``movement_verb`` / ``movement_label`` / ``movement_suffix`` -- the audit prose;
    * ``extra_data`` -- extra audit context (the tie + operation ids on the per-run leg);
    * ``duplicate_is_noop`` -- the insert discipline. **Both live legs pass ``False``**,
      because neither reference shape is covered by any unique index, so an
      ``IntegrityError`` on them is a real fault rather than a lost race. See
      ``_post_stock_movement_txn``.

    **``reference_type`` and ``duplicate_is_noop`` are REQUIRED, with no defaults, and
    that is a deliberate hazard removal rather than housekeeping.** They previously
    defaulted to ``'work_order'`` / ``True`` -- the historical values, kept so the
    signature "read as the one it replaced". Both are now precisely the wrong answer: a
    call site that omitted them would post a LEGACY-shaped component ISSUE *inside* the
    ``uq_wo_inventory_issue`` predicate (invisible to ``backflush_net_issued_by_part``,
    matched by ``_component_already_issued``, so that work order is fenced out of the
    reconciling engine for that part forever) *and* would swallow every real
    ``IntegrityError`` on it into a recorded shortage -- re-creating, silently and in one
    stroke, the two defects PR 4.4 exists to fix. Forcing both to be stated makes that
    unrepresentable by accident.

    The insert -> decrement -> dual-audit body itself lives in
    ``_post_stock_movement_txn``, shared with the compensating ``_write_return_txn``; the
    only thing that differs between consuming and returning is the SIGN of the on-hand
    move. Read that helper for the ordering rule (insert first, under a savepoint; move
    on-hand only when the insert actually landed) and for why ``quantity_available`` is
    recomputed in the same block.

    Returns the inserted ``InventoryTransaction``, or -- only under
    ``duplicate_is_noop=True`` -- ``None`` on a duplicate no-op.
    """
    txn = InventoryTransaction(
        company_id=company_id,
        inventory_item_id=inventory_item.id,
        part_id=component_part_id,
        transaction_type=TransactionType.ISSUE,
        quantity=-quantity,
        from_location=inventory_item.location,
        lot_number=lot_number,
        reference_type=reference_type,
        reference_id=work_order.id if reference_id is None else reference_id,
        reference_number=work_order.work_order_number,
        # NULL for a plain BOM/routing backflush; set when a material tie contributed the
        # demand, so the ledger row walks back to the tie.
        allocation_id=allocation_id,
        unit_cost=unit_cost,
        total_cost=quantity * unit_cost,
        notes=notes if notes is not None else f"Backflush consumption for work order {work_order.work_order_number}",
        created_by=user_id,
    )
    return _post_stock_movement_txn(
        db,
        txn=txn,
        inventory_item=inventory_item,
        # NEGATIVE: an ISSUE takes material off the shelf.
        on_hand_delta=-quantity,
        audit=audit,
        movement_description=(
            f"{movement_verb} {quantity} of part {part_number or component_part_id} "
            f"for work order {work_order.work_order_number}"
            + movement_suffix
            + (f" lot {lot_number}" if lot_number else "")
        ),
        stock_identifier=f"{part_number or component_part_id} @ {inventory_item.location}",
        stock_description=f"{movement_label}: stock for part {part_number or component_part_id}",
        extra_data=extra_data,
        duplicate_is_noop=duplicate_is_noop,
    )


def _write_return_txn(
    db: Session,
    work_order: WorkOrder,
    *,
    inventory_item: InventoryItem,
    part_id: int,
    quantity: float,
    unit_cost: float,
    lot_number: Optional[str],
    company_id: int,
    user_id: int,
    audit: AuditService,
    part_number: Optional[str],
    allocation_id: Optional[int],
    reference_type: str,
    reference_id: int,
    reason_code: str,
    notes: str,
    movement_verb: str = "Returned",
    movement_label: str = "Material return",
    movement_suffix: str = "",
    extra_data: Optional[dict] = None,
) -> InventoryTransaction:
    """Write one POSITIVE ``RETURN`` txn against a source lot, increment it, + audit.

    The compensating sibling of ``_write_issue_txn``, sharing its
    construct -> insert -> move-on-hand -> dual-audit body (``_post_stock_movement_txn``)
    rather than copying it. The insert/increment ORDER and the dual-audit shape are the
    compliance-visible parts of a stock movement; a drifted copy is exactly what the
    shared helper exists to prevent.

    Deliberate choices, each of which has a wrong-looking alternative:

    * **``TransactionType.RETURN``, positive quantity.** Not a positive ``ISSUE`` -- five
      readers ``abs()`` an ISSUE row into MORE consumption, and under
      ``reference_type='work_order'`` a second ISSUE row collides with
      ``uq_wo_inventory_issue``. Not ``ADJUST`` either: that means "a count changed", not
      "material came back off a job".
    * **``reference_type`` / ``reference_id`` MIRROR the rows being compensated**
      (``('work_order_operation', operation.id)`` for an operation-scoped tie,
      ``('work_order_backflush', work_order.id)`` for a work-order-scoped one). That is
      the single most load-bearing field here: ``work_order_ledger_filter`` matches on
      reference SHAPE only, never on ``transaction_type``, so a correctly-referenced
      RETURN is picked up by job cost, analytics, lot genealogy and
      ``GET /inventory/transactions?work_order_id=`` with no change to those readers.
    * **``unit_cost`` is the COMPENSATED ROW'S**, not the lot's current ``unit_cost`` --
      a lot revaluation between consume and return would otherwise leave residual (or
      negative) material cost stranded on the job.
    * **``to_location``** mirrors the ISSUE's ``from_location``: the material goes back
      where it came from.

    This NEVER treats a duplicate as a no-op. A RETURN row sits outside both
    ``uq_wo_inventory_*`` predicates (they require ``transaction_type`` of ``RECEIVE`` /
    ``ISSUE``), so there is no idempotency index to lose a race with: an
    ``IntegrityError`` here is a real fault and must reach the write handler that asked
    for the return, not be swallowed into a silent "nothing moved". Hence the non-optional
    return type.

    That rule is no longer this function's alone -- it is the rule, and both live ISSUE
    legs now follow it. Neither ``work_order_backflush`` nor ``work_order_operation`` is
    covered by any unique index either, so both pass ``duplicate_is_noop=False`` and let a
    genuine fault reach a savepoint that records it. Only the FG RECEIPT keeps the
    swallow-a-duplicate discipline, because ``uq_wo_inventory_receipt`` genuinely backs it
    and it is genuinely exposed to the lock-free reconcile race migration 041 addressed.
    """
    txn = InventoryTransaction(
        company_id=company_id,
        inventory_item_id=inventory_item.id,
        part_id=part_id,
        transaction_type=TransactionType.RETURN,
        quantity=quantity,
        to_location=inventory_item.location,
        lot_number=lot_number,
        reference_type=reference_type,
        reference_id=reference_id,
        reference_number=work_order.work_order_number,
        # The tie the material came off, so the compensating row walks back to the same
        # allocation its ISSUE rows do (and the ledger-backed guards stay armed).
        allocation_id=allocation_id,
        unit_cost=unit_cost,
        total_cost=quantity * unit_cost,
        reason_code=reason_code,
        notes=notes,
        created_by=user_id,
    )
    posted = _post_stock_movement_txn(
        db,
        txn=txn,
        inventory_item=inventory_item,
        # POSITIVE: a RETURN puts material back on the shelf. Returning INTO a negative
        # lot is expected -- a shortage-driven consumption drove it below zero and this
        # unwinds it toward zero -- so there is deliberately no guard against it.
        on_hand_delta=quantity,
        audit=audit,
        movement_description=(
            f"{movement_verb} {quantity} of part {part_number or part_id} "
            f"to stock from work order {work_order.work_order_number}"
            + movement_suffix
            + (f" lot {lot_number}" if lot_number else "")
        ),
        stock_identifier=f"{part_number or part_id} @ {inventory_item.location}",
        stock_description=f"{movement_label}: stock for part {part_number or part_id}",
        extra_data=extra_data,
        duplicate_is_noop=False,
    )
    if posted is None:  # pragma: no cover - unreachable: duplicate_is_noop=False never returns None
        raise RuntimeError(f"Material return transaction for work order {work_order.id} was not written")
    return posted


def _post_stock_movement_txn(
    db: Session,
    *,
    txn: InventoryTransaction,
    inventory_item: InventoryItem,
    on_hand_delta: float,
    audit: AuditService,
    movement_description: str,
    stock_identifier: str,
    stock_description: str,
    extra_data: Optional[dict] = None,
    duplicate_is_noop: bool = True,
) -> Optional[InventoryTransaction]:
    """Insert one ledger row, move the lot's on-hand by ``on_hand_delta``, write the audit.

    THE single implementation of the four steps every work-order material movement owes,
    in the order it owes them (item 1):

    1. INSERT the ``InventoryTransaction`` first (under a savepoint when the caller has an
       idempotency index behind it);
    2. move ``quantity_on_hand`` ONLY when that insert actually landed -- a duplicate
       inserted nothing, so decrementing/incrementing would double-count against the
       winning transaction's row;
    3. recompute ``quantity_available`` in the SAME block as the ``quantity_on_hand``
       mutation, because skipping it silently desyncs the denormalized column the
       receipt-void guard and MRP read;
    4. write the DUAL audit rows -- an ``inventory`` CREATE for the movement plus an
       ``inventory`` UPDATE for the on-hand change it produced (the canonical shape,
       shared with ``inventory.py``'s ``_audit_stock_movement``).

    The sign of ``on_hand_delta`` is the whole difference between consuming and
    returning; everything else about the two is identical, which is exactly why they
    share this body instead of each carrying a copy of it.

    ``duplicate_is_noop`` picks the insert discipline, and the rule is simply "is this
    row backed by a unique index?":

    * ``True`` -- the FG RECEIPT, and only the FG receipt. ``uq_wo_inventory_receipt``
      backs it and it is genuinely exposed to the lock-free reconcile-on-read race, so a
      concurrent duplicate raises ``IntegrityError``; roll back just the savepoint and
      report ``None``, leaving the outer completion / reconcile transaction usable.
    * ``False`` -- every component movement (``work_order_backflush`` ISSUE,
      ``work_order_operation`` ISSUE, and every RETURN). NO index covers those shapes, so
      an ``IntegrityError`` is a real fault -- an FK, a NOT NULL, or
      ``chk_inventory_items_quantity_non_negative`` -- and swallowing it would record a
      genuine failure as "a concurrent completion already wrote this", i.e. as a shortage
      or a silent under-consumption. Insert plainly and let it propagate to the caller's
      savepoint (``_issue_component_under_savepoint`` /
      ``_consume_allocation_under_savepoint``) or to the write handler.
    """
    if duplicate_is_noop:
        if not _insert_txn_with_savepoint(db, txn):
            return None
    else:
        db.add(txn)
        db.flush()

    old_on_hand = inventory_item.quantity_on_hand
    inventory_item.quantity_on_hand = float(inventory_item.quantity_on_hand or 0) + on_hand_delta
    inventory_item.quantity_available = inventory_item.quantity_on_hand - float(inventory_item.quantity_allocated or 0)
    db.flush()

    audit.log_create(
        "inventory",
        txn.id,
        str(txn.id),
        new_values=txn,
        description=movement_description,
        extra_data=extra_data,
    )
    if old_on_hand is not None:
        audit.log_update(
            "inventory",
            inventory_item.id,
            stock_identifier,
            old_values={"quantity_on_hand": old_on_hand},
            new_values={"quantity_on_hand": inventory_item.quantity_on_hand},
            description=stock_description,
        )
    return txn


def _record_backflush_component_failed(
    db: Session,
    *,
    work_order: WorkOrder,
    component_part_id: int,
    allocation_id: Optional[int],
    error: BaseException,
    company_id: int,
    user_id: Optional[int],
    audit: AuditService,
) -> None:
    """Record a rolled-back component consumption on the chain AND notify.

    The exact twin of ``material_consumption_service._record_consumption_failed``, and
    written for the same reason: material that SHOULD have depleted and did not is a
    material-trail control gap strictly worse than the shortage case, which already
    writes a chain row. Without this the only trace is a log line the compliance record
    never sees.

    **The warning ``OperationalEvent`` is not decoration.** The audit row alone made the
    degraded path strictly QUIETER than the lesser condition it degrades from: a shortage
    emits ``backflush_shortage``, which the catalog routes to Purchasing as
    ``material.backflush_shortage``, while "the draw raised and rolled back, so nothing
    was consumed at all" reached nobody. That is not a corner case -- on a database where
    ``chk_inventory_items_quantity_non_negative`` is live, EVERY shortage arrives here
    instead, so the notification this PR ships as its headline deliverable would be
    exactly the one that never fires. ``material.backflush_failed`` carries it.

    Safe on the post-``nested.rollback()`` outer transaction: ``AuditService.log`` opens
    its OWN savepoint around the INSERT and swallows every failure (returns ``None``), so
    it can neither propagate nor re-poison the session, and the emit is ``best_effort``.
    Still wrapped defensively -- a reconcile-on-read GET must never 500 because the
    failure record failed.
    """
    extra = {
        "work_order_id": work_order.id,
        "work_order_number": work_order.work_order_number,
        "component_part_id": component_part_id,
        "allocation_id": allocation_id,
        "error": f"{type(error).__name__}: {error}"[:500],
    }
    try:
        audit.log(
            action=BACKFLUSH_COMPONENT_FAILED_AUDIT_ACTION,
            resource_type="inventory",
            resource_id=component_part_id,
            resource_identifier=str(component_part_id),
            description=(
                f"Backflush consumption FAILED and was rolled back for component {component_part_id} "
                f"on work order {work_order.work_order_number}; material was NOT depleted"
            ),
            success=False,
            error_message=f"{type(error).__name__}: {error}"[:500],
            extra_data=extra,
            company_id=company_id,
        )
    except Exception:  # pragma: no cover - the record must never break the caller
        logger.exception(
            "Failed to record BACKFLUSH_COMPONENT_FAILED for part %s on WO %s (company %s)",
            component_part_id,
            work_order.id,
            company_id,
        )
    # Emitted under its OWN savepoint. ``emit_best_effort`` swallows the exception, but
    # its docstring records the residue that leaves: a ``flush()`` that fails AT THE DB
    # still deactivates the outer transaction, so the caller's next flush raises
    # ``PendingRollbackError`` with the true cause only in a WARNING log. That is
    # unacceptable here specifically -- this recorder runs inside a per-component loop
    # that must survive to the next component, and is reached from a reconcile-on-read
    # GET whose whole contract is "never 500". A ``None`` return is the documented
    # failure signal, so rolling back to the savepoint on it restores a usable session.
    # (``begin_nested`` is the closer ``emit_best_effort``'s own docstring names.)
    event_savepoint = db.begin_nested()
    if (
        OperationalEventService(db).emit_best_effort(
            company_id=company_id,
            event_type=BACKFLUSH_COMPONENT_FAILED_EVENT_TYPE,
            source_module="completion_inventory",
            entity_type="inventory",
            entity_id=component_part_id,
            work_order_id=work_order.id,
            user_id=user_id,
            severity="warning",
            event_payload=extra,
        )
        is None
    ):
        event_savepoint.rollback()
    else:
        event_savepoint.commit()


def _issue_component_under_savepoint(
    db: Session,
    work_order: WorkOrder,
    *,
    component_part_id: int,
    required_qty: float,
    company_id: int,
    user_id: int,
    audit: AuditService,
    allocation_id: Optional[int] = None,
    pinned_inventory_item_id: Optional[int] = None,
    propagate_lock_conflict: bool,
) -> IssueOutcome:
    """Draw ONE component inside its OWN savepoint; degrade all but a lock conflict.

    A direct mirror of ``material_consumption_service._consume_allocation_under_savepoint``
    -- including the ``propagate_lock_conflict`` parameter and its rationale -- and it is
    REQUIRED, not decorative. ``_post_stock_movement_txn`` moves ``quantity_on_hand``
    OUTSIDE ``_insert_txn_with_savepoint``'s guard, so if
    ``chk_inventory_items_quantity_non_negative`` is live in production a driven-negative
    draw would poison the session and turn a reconcile-on-read GET into a
    ``PendingRollbackError`` 500. Here it rolls back that ONE component, leaves the outer
    transaction committable, and writes a ``BACKFLUSH_COMPONENT_FAILED`` chain row.

    **``propagate_lock_conflict=False`` is what every caller passes today, and that is a
    matched posture rather than an oversight.** This seam is reached from
    ``apply_completion_inventory_effects``, which runs from reconcile-on-read GETs where
    there is no 409 to return and no actor to attribute one to -- exactly the situation in
    which ``consume_tied_materials_for_work_order`` already passes ``False`` at the same
    seam. Degrading loses nothing the write paths need: they hold ``SELECT ... FOR UPDATE``
    on the work order, and ``backflush_components_for_work_order``'s pre-savepoint
    ``db.flush()`` keeps any version conflict outside the savepoint entirely. Changing it
    is a change to BOTH legs at once, not a unilateral divergence here.
    """
    nested = db.begin_nested()
    try:
        outcome = _issue_one_component(
            db,
            work_order,
            component_part_id=component_part_id,
            required_qty=required_qty,
            company_id=company_id,
            user_id=user_id,
            audit=audit,
            allocation_id=allocation_id,
            pinned_inventory_item_id=pinned_inventory_item_id,
        )
        nested.commit()
        return outcome
    except StaleDataError:
        # INVARIANT 4, and the ONE failure the caller gets to decide about -- see the
        # docstring, and the fuller statement of the same choice on the tie engine's twin.
        nested.rollback()
        if propagate_lock_conflict:
            raise
        logger.exception(
            "Backflush hit a lock conflict for component %s on WO %s (company %s)",
            component_part_id,
            work_order.id,
            company_id,
        )
        _record_backflush_component_failed(
            db,
            work_order=work_order,
            component_part_id=component_part_id,
            allocation_id=allocation_id,
            error=StaleDataError("optimistic lock conflict during backflush"),
            company_id=company_id,
            user_id=user_id,
            audit=audit,
        )
        return IssueOutcome()
    except Exception as exc:  # degrade per-component, never break a GET
        nested.rollback()
        logger.exception(
            "Backflush failed for component %s on WO %s (company %s)",
            component_part_id,
            work_order.id,
            company_id,
        )
        _record_backflush_component_failed(
            db,
            work_order=work_order,
            component_part_id=component_part_id,
            allocation_id=allocation_id,
            error=exc,
            company_id=company_id,
            user_id=user_id,
            audit=audit,
        )
        return IssueOutcome()


def backflush_components_for_work_order(
    db: Session,
    work_order: WorkOrder,
    *,
    user_id: int,
    company_id: int,
    audit: AuditService,
    allocations: Optional[list[WorkOrderMaterialAllocation]] = None,
) -> BackflushResult:
    """Consume a completed WO's component material from inventory (INV-2).

    TWO demand sources, reconciled SEPARATELY and posted as separately-attributable rows:

    * **LEG 2 -- work-order-scoped material ties.** NOT gated on
      ``Part.backflush_components``: an explicit tie IS the opt-in. Target is the tie's
      ``qty_planned``; ``posted`` is ``net_consumed_quantity_for_allocation`` (keyed on
      ``allocation_id``, shape-agnostic, signed ISSUE - RETURN).
    * **LEG 1 -- the BOM / routing backflush.** GATED on
      ``work_order.part.backflush_components`` (opt-in per part, default False) so
      material a shop issued manually is never double-consumed. **Since PR 4.5 that column
      is writable** -- ``PUT /parts/{id}`` / ``PUT /materials/{id}`` behind
      ``assert_backflush_change_allowed``'s readiness gate, plus the part-detail card --
      so this leg RUNS for any part a supervisor has armed and is no longer dark code.
      Target is the resolved demand after BOTH suppression layers and after
      ``_refuse_blocked_demand`` has dropped anything a blocking diagnostic condemns;
      ``posted`` is ``backflush_net_issued_by_part`` (this leg's own history:
      ``work_order_backflush`` rows with ``allocation_id IS NULL``).

    In both cases ``delta = target - posted`` and a row is written only when
    ``delta > _EPSILON``. **A non-positive delta is a silent no-op, NEVER an
    auto-reversal** (invariant 6(b)): this path runs from a GET where there is no actor
    and no reason, so un-consuming stays PR 3's reasoned, audited ``return_tied_material``.

    **LEG 2 RUNS FIRST, deliberately.** Explicit planner intent outranks derived demand,
    so a shortage lands on the derived side rather than on a tie somebody deliberately
    created -- and the tie's LOT PIN gets first claim on stock.

    **The two nets are disjoint and complete.** Leg 1 filters ``allocation_id IS NULL``;
    every tie-driven row carries one. So leg 1 can never suppress itself and leg 2 can
    never see leg 1's rows. A part carrying BOTH kinds of demand still consumes
    BOM + tie in total -- as it always did -- but as TWO rows, each walkable to its own
    origin, with the tie's pin governing the tie's quantity rather than the whole sum.

    **``_component_already_issued`` is consulted on BOTH legs as the LEGACY FENCE.** It
    still keys on ``reference_type='work_order'``, which nothing writes any more, so it
    matches only pre-PR-4.4 rows and fences those work orders out of this engine
    entirely. Suppression is RECORDED (``_record_backflush_demand_suppressed``), never
    silent.

    Idempotency is ARITHMETIC, not an index, and it is only valid because the writes are
    serialized: all four write entries hold ``SELECT ... FOR UPDATE`` on the
    ``work_orders`` row before reaching here, and the two reconcile-on-read entries always
    UPDATE that same ``version_id_col``-mapped row, so a loser raises ``StaleDataError``
    and its whole reconcile rolls back.

    A shortage NEVER fails the completion; it is recorded tamper-evidently (a
    ``BACKFLUSH_SHORTAGE`` ``audit_log`` row + a ``backflush_shortage``
    ``OperationalEvent``) inside ``_issue_one_component``, on the live paths AND the
    reconcile path alike. Each consumed source lot is carried on its own ISSUE row for
    as-built genealogy. Does NOT commit.

    **This function adds no re-entry trigger.** The leg still runs exactly once per work
    order lifetime -- every operation-completion handler refuses a terminal parent,
    ``complete_work_order`` early-returns for COMPLETE/CLOSED, reconcile-on-read strips
    terminal work orders from its candidate set, and terminal -> non-terminal is blocked.
    Making the arithmetic convergent is the PRECONDITION for ever adding one safely; it is
    not the thing itself.
    """
    result = BackflushResult()

    part = work_order.part
    if part is None and work_order.part_id is not None:
        part = db.query(Part).filter(Part.id == work_order.part_id, Part.company_id == company_id).first()

    # INVARIANT 6(d). This gate reads ONLY the allocation list ``apply_completion_
    # inventory_effects`` already threaded in and the part it just resolved -- no net
    # read, no flush, no savepoint above it -- so an untied, non-opted-in work order
    # leaves here having issued ZERO extra queries and written NOTHING. That equivalence
    # is the feature's headline guarantee and is locked by tests that must never be
    # relaxed to accommodate a change here.
    open_wo_ties = [
        a
        for a in _open_allocations(db, work_order, company_id, allocations)
        if a.work_order_operation_id is None and float(a.qty_planned or 0) > _EPSILON
    ]
    backflush_enabled = part is not None and bool(getattr(part, "backflush_components", False))
    if backflush_enabled and is_laser_dispatch_work_order(work_order):
        # Same predicate as the FG-receipt skip: a laser nest-dispatch WO is a dispatch
        # pool whose ``part_id`` is the PARENT part (on a parented child) and whose
        # ``quantity_complete`` counts nest RUNS -- exploding the parent's BOM against
        # those numbers would consume the parent's components on the wrong demand basis
        # while the parent's own completion runs the real backflush. Only LEG 1 is
        # gated: material TIES (both legs' explicit ones) are the nest flow's actual
        # consumption mechanism and stay untouched.
        logger.debug(
            "BOM backflush skipped for laser nest-dispatch WO %s (company %s): nest runs are not parent demand",
            work_order.id,
            company_id,
        )
        backflush_enabled = False
    if not backflush_enabled and not open_wo_ties:
        return result

    # BEFORE the first savepoint, and load-bearing. An autoflush ``StaleDataError`` from a
    # pending versioned UPDATE (the work order's own status change) raised INSIDE a
    # per-component savepoint would be degraded into a ``BACKFLUSH_COMPONENT_FAILED`` row
    # instead of aborting the request. Flushing here puts the versioned UPDATE outside
    # every savepoint, so a version conflict propagates normally -- the same lesson
    # already written at the four operation-completion call sites.
    db.flush()

    # ------------------------------------------------------------------ LEG 2: ties
    for allocation in open_wo_ties:
        # DELTA FIRST, fence second -- deliberately the opposite order from leg 1, and the
        # difference is not stylistic. Leg 1's suppression layers PRODUCE its target, so
        # they cannot run after the delta test; leg 2's target is the tie's own
        # ``qty_planned`` and owes the fence nothing, so the cheap correctness wins:
        #   * a converged replay (delta <= 0) writes NO suppression row, because nothing
        #     was suppressed -- a row saying otherwise would be a false record;
        #   * the recorded quantity is the UNMET REMAINDER, not the whole ``qty_planned``.
        #     Recording ``qty_planned`` on a partly-drained tie would claim material was
        #     dropped that had already been issued.
        # Neither is reachable while this leg runs once per work-order lifetime; both stay
        # correct the moment a re-entry trigger is added (PR 4.5), which is the whole
        # reason to spend one query on the fenced path to get the order right now.
        already = net_consumed_quantity_for_allocation(db, allocation_id=allocation.id, company_id=company_id)
        delta = float(allocation.qty_planned or 0) - already
        if delta <= _EPSILON:
            continue
        if _component_already_issued(db, work_order.id, allocation.part_id, company_id):
            # A LEGACY one-shot row covers this (work order, part). It may well have been
            # a SUMMED row carrying this tie's own quantity; re-issuing against it would
            # double-consume. Fence out and record.
            _record_backflush_demand_suppressed(
                db,
                work_order,
                part_id=allocation.part_id,
                quantity=delta,
                company_id=company_id,
                audit=audit,
                reason="already_issued",
                ledger_net=already,
            )
            continue
        outcome = _issue_component_under_savepoint(
            db,
            work_order,
            component_part_id=allocation.part_id,
            required_qty=delta,
            company_id=company_id,
            user_id=user_id,
            audit=audit,
            allocation_id=allocation.id,
            pinned_inventory_item_id=allocation.pinned_inventory_item_id,
            propagate_lock_conflict=False,
        )
        if outcome.posted_any:
            _advance_tie_consumed(db, work_order, allocation, company_id=company_id, audit=audit)
        if outcome.shortage is not None:
            result.shortages.append(outcome.shortage)

    # ------------------------------------------------------- LEG 1: BOM / routing
    required_by_component: dict[int, float] = (
        _resolve_backflush_components(db, work_order, company_id, allocations, audit=audit, user_id=user_id)
        if backflush_enabled
        else {}
    )
    nets = backflush_net_issued_by_part(
        db, work_order_id=work_order.id, company_id=company_id, part_ids=required_by_component.keys()
    )
    for component_part_id, target in required_by_component.items():
        if target <= _EPSILON:
            continue
        # FENCE FIRST here, unlike leg 2, and it has to be: ``_resolve_backflush_components``
        # above has ALREADY run both suppression layers to produce ``target``, so there is
        # no ordering in which the fence could follow the delta test and still describe
        # what was dropped. The cost is a suppression row on a converged replay of a
        # legacy-fenced part -- unreachable while this leg runs once per work order.
        if _component_already_issued(db, work_order.id, component_part_id, company_id):
            _record_backflush_demand_suppressed(
                db,
                work_order,
                part_id=component_part_id,
                quantity=target,
                company_id=company_id,
                audit=audit,
                reason="already_issued",
            )
            continue
        delta = target - nets.get(component_part_id, 0.0)
        if delta <= _EPSILON:
            # Already reconciled (a replay, or a target that fell). NO-OP and NO audit
            # row: a converged replay is not a suppression.
            continue
        outcome = _issue_component_under_savepoint(
            db,
            work_order,
            component_part_id=component_part_id,
            required_qty=delta,
            company_id=company_id,
            user_id=user_id,
            audit=audit,
            allocation_id=None,
            pinned_inventory_item_id=None,
            propagate_lock_conflict=False,
        )
        if outcome.posted_any:
            result.issued_part_ids.append(component_part_id)
        if outcome.shortage is not None:
            result.shortages.append(outcome.shortage)

    return result


def _advance_tie_consumed(
    db: Session,
    work_order: WorkOrder,
    allocation: WorkOrderMaterialAllocation,
    *,
    company_id: int,
    audit: AuditService,
) -> None:
    """Set a work-order-scoped tie's ``qty_consumed`` to its OWN signed ledger net.

    Re-read from the ledger AFTER posting, never written as ``qty_planned``. Its
    predecessor set ``qty_consumed = qty_planned`` regardless of what actually posted --
    the plan's fourth residual -- which made the cache a claim rather than a record. It
    matters because two verbs key on it exactly: ``return_and_untie`` gives back exactly
    ``qty_consumed``, and ``correct_over_consumption``'s allowance is
    ``qty_consumed - target``. Reading the ledger makes cache == net by construction, so
    both are exact.

    The ledger remains authoritative (model docstring, invariant #6); this keeps the
    denormalized cache honest so the untie guard ("409 once ``qty_consumed > 0``") and the
    UI agree with the movement that just posted. Tenant-scoped; only flushes.

    NOTE the asymmetry this creates and does not hide: the operation-scoped twin in
    ``material_consumption_service._consume_one_allocation`` still writes
    ``qty_consumed = target``, so the same column is a ledger-backed RECORD here and a
    computed CLAIM there. See ``net_consumed_quantity_for_allocation`` for why.

    AUDITED (invariant #2), mirroring the operation-scoped twin in
    ``material_consumption_service._consume_one_allocation``. This is a state change on a
    TenantMixin row and it writes the exact field a later verb refuses against, so an
    unaudited write would change what the system will later refuse with nothing on the
    hash chain saying what changed it. ``log_update`` self-suppresses when the value did
    not actually move, so a converged replay adds no row.

    ONLY called when a row actually posted (``IssueOutcome.posted_any``) -- see the call
    site.
    """
    net = net_consumed_quantity_for_allocation(db, allocation_id=allocation.id, company_id=company_id)
    old_consumed = float(allocation.qty_consumed or 0)
    allocation.qty_consumed = net
    db.flush()

    part_number = (
        db.query(Part.part_number).filter(Part.company_id == company_id, Part.id == allocation.part_id).scalar()
    )
    audit.log_update(
        "work_order_material_allocation",
        allocation.id,
        f"WO {work_order.work_order_number} / part {part_number or allocation.part_id}",
        old_values={"qty_consumed": old_consumed},
        new_values={"qty_consumed": allocation.qty_consumed},
        description=(
            f"Consumed {allocation.qty_consumed} {allocation.unit_of_measure} of part "
            f"{part_number or allocation.part_id} against work order {work_order.work_order_number} "
            "(work-order-scoped tie, drained by the completion backflush)"
        ),
        extra_data={
            "work_order_id": work_order.id,
            "reference_type": BACKFLUSH_REFERENCE_TYPE,
            "part_id": allocation.part_id,
            "allocation_id": allocation.id,
        },
    )


# ================================================================ DRY RUN (reads only)
#
# Everything below models what ``backflush_components_for_work_order`` WOULD do, without
# doing any of it. Two callers: the part-level refusal gate behind
# ``Part.backflush_components`` and the work-order dry-run preview endpoint.
#
# The rule these functions live by is the one ``material_tie_view`` was created to state:
# **a poll is not an actor and records no reason.** No session mutation, no audit row, no
# operational event, nothing to commit. That is enforceable here only because the
# resolution layer was split from the recording layer first (see
# ``_resolve_backflush_demand`` / ``_record_ledger_suppression``); a preview built by
# savepoint-and-rollback around the real writer would have coupled a read to a write path
# forever, for the sake of one audit row.

# ``BackflushPreviewLine.source`` values.
BACKFLUSH_SOURCE_BOM = "bom_routing"
BACKFLUSH_SOURCE_TIE = "work_order_tie"

# ``BackflushPreviewLine.suppression_reason`` values. The first two mirror
# ``_record_backflush_demand_suppressed``'s ``reason`` vocabulary exactly, so a preview
# and the chain row a completion later writes use the same word for the same fact.
SUPPRESSION_ALREADY_ISSUED = "already_issued"
SUPPRESSION_LEDGER_CONSUMED = "ledger_consumed"
SUPPRESSION_OPEN_OPERATION_TIE = "open_operation_tie"
SUPPRESSION_CONVERGED = "converged"
# The completion would REFUSE this line over a blocking diagnostic and record a
# ``BACKFLUSH_DEMAND_REFUSED`` chain row (``blocked_demand_refusal``). Reported here for
# the same reason the other four are: a preview that showed the demand posting would
# disagree with the outcome, which is the only failure this panel exists to prevent.
SUPPRESSION_BLOCKING_DIAGNOSTIC = "blocking_diagnostic"


@dataclass
class _PreviewLot:
    """A DETACHED copy of one stock row's on-hand, used to plan a draw without touching it.

    Load-bearing, not a convenience. The real leg consumes lots in sequence and each draw
    sees the previous draw's decrement; a preview that planned every line against the
    committed on-hand would name the same lot twice on a work order whose BOM and whose
    tie both want the same part -- a state the completion cannot produce. Simulating
    requires DECREMENTING as we go, and decrementing an ``InventoryItem`` the Session is
    tracking would be written out by the next autoflush. So the simulation runs on copies
    the Session has never seen.
    """

    inventory_item_id: int
    lot_number: Optional[str]
    location: Optional[str]
    unit_cost: float
    quantity_on_hand: float


@dataclass
class BackflushPreviewLot:
    """One ISSUE row the completion would post: which lot, and how much.

    ``is_shortfall`` marks the row the writer posts for the part of the demand no
    permitted lot could cover. It is not a fourth kind of information -- the writer really
    does post it, against ``_shortfall_anchor``'s lot, driving that lot negative -- and
    omitting it is how a preview comes to under-state a named heat's contribution.
    """

    inventory_item_id: int
    lot_number: Optional[str] = None
    location: Optional[str] = None
    quantity: float = 0.0
    unit_cost: float = 0.0
    is_shortfall: bool = False


@dataclass
class BackflushPreviewLine:
    """One component's whole decision: target, what already posted, and the lots it hits."""

    component_part_id: int
    component_part_number: Optional[str] = None
    component_part_name: Optional[str] = None
    unit_of_measure: Optional[str] = None
    source: str = BACKFLUSH_SOURCE_BOM
    # True on BOM/routing lines: they move only once ``Part.backflush_components`` is on.
    # A work-order-scoped tie IS its own opt-in and consumes regardless.
    requires_opt_in: bool = True
    allocation_id: Optional[int] = None
    required_quantity: float = 0.0
    already_issued: float = 0.0
    delta_quantity: float = 0.0
    suppressed: bool = False
    suppression_reason: Optional[str] = None
    available_quantity: float = 0.0
    shortfall: float = 0.0
    would_go_negative: bool = False
    held_quantity_skipped: float = 0.0
    held_lot_numbers: list[str] = field(default_factory=list)
    pinned_inventory_item_id: Optional[int] = None
    pinned_lot_number: Optional[str] = None
    # The pinned lot has gone on_hold / quarantine / rejected / inactive SINCE it was
    # pinned (the tie endpoint refuses to pin a held lot), and the writer consumes it
    # anyway -- refusing from a reconcile-on-read GET would be unattributable -- recording
    # ``HELD_MATERIAL_CONSUMED(pinned=True)``. It is the single most consequential thing a
    # pre-completion dry run can warn about, and the ``held_*`` fields above cannot carry
    # it: those describe stock the draw PASSED OVER on an unpinned shortage, which by
    # construction is never populated on a pinned line.
    pinned_lot_is_held: bool = False
    # True when NO stock row for this part exists at all, so the writer would MINT a
    # placeholder ``InventoryItem`` and post the shortfall against it. The preview cannot
    # model it as a lot (it has no id yet, and minting one would be a write).
    shortfall_creates_placeholder: bool = False
    lots: list[BackflushPreviewLot] = field(default_factory=list)


@dataclass
class BackflushPreview:
    """What a completion of this work order would move, and what it could not answer."""

    work_order_id: int
    work_order_number: Optional[str] = None
    part_id: Optional[int] = None
    part_number: Optional[str] = None
    backflush_components: bool = False
    basis: float = 0.0
    lines: list[BackflushPreviewLine] = field(default_factory=list)
    diagnostics: list[_BackflushDiagnostic] = field(default_factory=list)


def _preview_source_lots(
    db: Session,
    *,
    part_id: int,
    company_id: int,
    proxies: dict[int, _PreviewLot],
    part_sources: dict[int, list[int]],
) -> list[_PreviewLot]:
    """The lots an UNPINNED draw for this part would walk, as simulation proxies.

    Ordering comes from ``consumable_source_items`` -- ``received_date`` FIFO over active,
    consumable, positive-on-hand lots, ``COALESCE(status,'available')`` so legacy
    NULL-status stock is not hidden. That is THE policy both engines share since PR 4.4,
    and reusing the query rather than restating its predicate is the only way the preview
    can name the heat the completion will actually draw.

    Cached per part so a second draw on the same part (a work-order-scoped tie AND BOM
    demand for one component is a supported state) sees the first draw's decrements.

    The cached list is re-filtered to POSITIVE on-hand on every call, because the writer
    re-runs ``consumable_source_items`` per component and that query carries
    ``quantity_on_hand > 0``. A lot the previous line drove to zero -- or negative, which a
    shortfall row does -- is therefore invisible to the writer's next draw, and must be
    invisible here too, or the second line would report an availability the completion
    does not see.
    """
    if part_id not in part_sources:
        # Imported lazily: ``material_consumption_service`` imports helpers from THIS module.
        from app.services.material_consumption_service import consumable_source_items

        ordered_ids: list[int] = []
        for item in consumable_source_items(db, part_id, company_id):
            proxies.setdefault(
                item.id,
                _PreviewLot(
                    inventory_item_id=item.id,
                    lot_number=item.lot_number,
                    location=item.location,
                    unit_cost=float(item.unit_cost or 0),
                    quantity_on_hand=float(item.quantity_on_hand or 0),
                ),
            )
            ordered_ids.append(item.id)
        part_sources[part_id] = ordered_ids
    return [proxies[item_id] for item_id in part_sources[part_id] if proxies[item_id].quantity_on_hand > _EPSILON]


def _plan_preview_line(
    db: Session,
    *,
    part_id: int,
    quantity: float,
    company_id: int,
    pinned_inventory_item_id: Optional[int],
    proxies: dict[int, _PreviewLot],
    part_sources: dict[int, list[int]],
    line: BackflushPreviewLine,
) -> None:
    """Fill in ``line``'s lot plan, availability and shortfall. Mutates only ``line``/proxies.

    A faithful replay of ``_issue_one_component``'s decisions, using the SAME
    ``plan_stock_draw`` the writer uses rather than a second copy of the spill arithmetic
    -- a preview built on its own predicate is how a dialog comes to promise a heat the
    engine will never touch. ``plan_stock_draw`` reads only ``quantity_on_hand``, so the
    detached proxies satisfy it structurally; the ``cast`` says that and nothing more.

    The pinned branch mirrors the writer too: a pin is a lot-directed instruction, so it
    draws from THAT lot exclusively and is driven negative rather than spilling onto a
    different heat. Held stock is disclosed exactly where the writer discloses it -- on a
    shortage, and only on an UNPINNED draw, because on a pinned one the pin (not any
    lot's status) is why nothing else was drawn -- with ONE addition the writer's
    disclosure cannot express: ``pinned_lot_is_held``, the case where the writer consumes
    a lot that went on hold AFTER it was pinned and records
    ``HELD_MATERIAL_CONSUMED(pinned=True)``. That draw is not short, so no shortage
    disclosure runs, and without this flag the dry run would show a clean pinned line over
    quarantined material about to go into product.

    **The unmet remainder is a LOT ROW, not just a scalar.** ``plan_stock_draw``'s
    ``draws`` are only the covered part; on a shortfall the writer posts a SECOND ISSUE
    row against ``_shortfall_anchor``'s lot, carrying that lot number onto the as-built
    record. It is appended here, flagged ``is_shortfall``, against the same anchor the
    writer picks -- and the anchor proxy is decremented, so a later line for the same part
    sees the negative exactly as the writer's re-query would (i.e. not at all: the lot
    drops out under the positive-on-hand filter).
    """
    from app.services.material_consumption_service import (
        is_consumable_item,
        plan_stock_draw,
        shortage_draw_disclosure,
    )

    pinned_item: Optional[InventoryItem] = None
    if pinned_inventory_item_id is not None:
        pinned_item = (
            db.query(InventoryItem)
            .filter(
                InventoryItem.id == pinned_inventory_item_id,
                InventoryItem.company_id == company_id,
            )
            .first()
        )
        if pinned_item is None:
            source_lots: list[_PreviewLot] = []
        else:
            source_lots = [
                proxies.setdefault(
                    pinned_item.id,
                    _PreviewLot(
                        inventory_item_id=pinned_item.id,
                        lot_number=pinned_item.lot_number,
                        location=pinned_item.location,
                        unit_cost=float(pinned_item.unit_cost or 0),
                        quantity_on_hand=float(pinned_item.quantity_on_hand or 0),
                    ),
                )
            ]
            line.pinned_lot_number = pinned_item.lot_number
            # The writer's own test (``_issue_one_component``), run here for the one
            # thing a dry run most needs to say.
            line.pinned_lot_is_held = not is_consumable_item(pinned_item)
    else:
        source_lots = _preview_source_lots(
            db, part_id=part_id, company_id=company_id, proxies=proxies, part_sources=part_sources
        )

    line.available_quantity = sum(lot.quantity_on_hand for lot in source_lots)
    draws, shortfall = plan_stock_draw(cast(Any, source_lots), quantity)
    last_drawn: Optional[_PreviewLot] = None
    for lot, take in draws:
        preview_lot = cast(_PreviewLot, lot)
        preview_lot.quantity_on_hand -= take
        last_drawn = preview_lot
        line.lots.append(
            BackflushPreviewLot(
                inventory_item_id=preview_lot.inventory_item_id,
                lot_number=preview_lot.lot_number,
                location=preview_lot.location,
                quantity=take,
                unit_cost=preview_lot.unit_cost,
            )
        )
    line.shortfall = shortfall if shortfall > _EPSILON else 0.0
    line.would_go_negative = line.shortfall > 0.0
    if line.would_go_negative:
        anchor = _shortfall_anchor(last_drawn, source_lots)
        if anchor is None:
            # No candidate lot at all: the writer mints a placeholder stock row and posts
            # against that. It has no id until it is written, and writing is exactly what
            # a dry run may not do, so the FACT is reported instead of a fictional lot.
            line.shortfall_creates_placeholder = True
        else:
            anchor.quantity_on_hand -= line.shortfall
            line.lots.append(
                BackflushPreviewLot(
                    inventory_item_id=anchor.inventory_item_id,
                    lot_number=anchor.lot_number,
                    location=anchor.location,
                    quantity=line.shortfall,
                    unit_cost=anchor.unit_cost,
                    is_shortfall=True,
                )
            )
        held_quantity, held_lots, pinned_lot = shortage_draw_disclosure(
            db,
            part_id=part_id,
            company_id=company_id,
            pinned_inventory_item_id=pinned_inventory_item_id,
            pinned_item=pinned_item,
        )
        line.held_quantity_skipped = held_quantity
        line.held_lot_numbers = held_lots
        if pinned_lot is not None:
            line.pinned_lot_number = line.pinned_lot_number or pinned_lot


def preview_backflush_for_work_order(db: Session, work_order: WorkOrder, *, company_id: int) -> BackflushPreview:
    """What a completion of this work order would consume. **PURE READ — writes NOTHING.**

    Models ``backflush_components_for_work_order`` in full, both legs, in the same ORDER
    (ties first, so the tie's lot pin gets first claim on stock and a shortage lands on the
    derived side), applying the same fences and the same arithmetic:

    * **LEG 2, work-order-scoped ties** -- target ``qty_planned``, ``posted``
      ``net_consumed_quantity_for_allocation``, delta tested BEFORE the legacy fence, so a
      converged tie reports ``converged`` and not a suppression that did not happen.
    * **LEG 1, BOM / routing demand** -- the two suppression layers run first (they
      PRODUCE the target), then the blocking-diagnostic refusal
      (``blocked_demand_refusal``, the pure half of what the completion path acts on),
      then the legacy fence, then ``delta = target - backflush_net_issued_by_part``.

    Modelling the RESOLVER alone would have been the easy version and a wrong one: the
    issue loop makes decisions the resolver never sees -- the legacy ``('work_order',
    ISSUE)`` fence, the ties' own demand, and which lot the draw actually lands on -- so a
    preview stopping at resolved demand would disagree with the outcome on exactly the
    work orders where it matters.

    **BOM/routing lines are reported whether or not the part has opted in** (each carries
    ``requires_opt_in``). That is the point of a dry run: the operator is deciding WHETHER
    to opt in, and a preview that showed nothing until after the flag was set could not
    inform that decision. ``backflush_components`` on the response says which world the
    work order is in today.
    """
    from app.services.material_consumption_service import open_allocations_for_work_order

    part = work_order.part
    if part is None and work_order.part_id is not None:
        part = db.query(Part).filter(Part.id == work_order.part_id, Part.company_id == company_id).first()

    preview = BackflushPreview(
        work_order_id=work_order.id,
        work_order_number=work_order.work_order_number,
        part_id=work_order.part_id,
        part_number=part.part_number if part else None,
        backflush_components=bool(getattr(part, "backflush_components", False)) if part else False,
    )

    allocations = open_allocations_for_work_order(db, work_order.id, company_id)
    resolution = _resolve_backflush_demand(db, work_order, company_id, allocations)
    preview.basis = resolution.basis
    preview.diagnostics = list(resolution.diagnostics)

    proxies: dict[int, _PreviewLot] = {}
    part_sources: dict[int, list[int]] = {}

    display_part_ids = (
        set(resolution.demand)
        | set(resolution.allocation_blocked)
        | set(resolution.ledger_blocked)
        | {a.part_id for a in allocations if a.part_id is not None}
    )
    display_parts = (
        {
            row.id: row
            for row in db.query(Part.id, Part.part_number, Part.name, Part.unit_of_measure)
            .filter(Part.company_id == company_id, Part.id.in_(display_part_ids))
            .all()
        }
        if display_part_ids
        else {}
    )

    def _new_line(part_id: int, source: str) -> BackflushPreviewLine:
        row = display_parts.get(part_id)
        return BackflushPreviewLine(
            component_part_id=part_id,
            component_part_number=row.part_number if row else None,
            component_part_name=row.name if row else None,
            unit_of_measure=_uom_label(row.unit_of_measure) if row else None,
            source=source,
            requires_opt_in=source == BACKFLUSH_SOURCE_BOM,
        )

    # ------------------------------------------------------------------ LEG 2: ties
    for allocation in allocations:
        if allocation.work_order_operation_id is not None or float(allocation.qty_planned or 0) <= _EPSILON:
            continue
        line = _new_line(allocation.part_id, BACKFLUSH_SOURCE_TIE)
        line.allocation_id = allocation.id
        line.unit_of_measure = allocation.unit_of_measure or line.unit_of_measure
        line.pinned_inventory_item_id = allocation.pinned_inventory_item_id
        line.pinned_lot_number = allocation.pinned_lot_number
        line.required_quantity = float(allocation.qty_planned or 0)
        line.already_issued = net_consumed_quantity_for_allocation(
            db, allocation_id=allocation.id, company_id=company_id
        )
        delta = line.required_quantity - line.already_issued
        if delta <= _EPSILON:
            line.suppressed = True
            line.suppression_reason = SUPPRESSION_CONVERGED
        elif _component_already_issued(db, work_order.id, allocation.part_id, company_id):
            line.suppressed = True
            line.suppression_reason = SUPPRESSION_ALREADY_ISSUED
        else:
            line.delta_quantity = delta
            _plan_preview_line(
                db,
                part_id=allocation.part_id,
                quantity=delta,
                company_id=company_id,
                pinned_inventory_item_id=allocation.pinned_inventory_item_id,
                proxies=proxies,
                part_sources=part_sources,
                line=line,
            )
        preview.lines.append(line)

    # ------------------------------------------------------- LEG 1: BOM / routing
    nets = backflush_net_issued_by_part(
        db, work_order_id=work_order.id, company_id=company_id, part_ids=resolution.demand.keys()
    )
    refuses_leg, refused_part_ids = blocked_demand_refusal(resolution)
    for part_id, target in resolution.demand.items():
        if target <= _EPSILON:
            continue
        line = _new_line(part_id, BACKFLUSH_SOURCE_BOM)
        line.required_quantity = target
        line.already_issued = nets.get(part_id, 0.0)
        # FIRST, exactly as on the write path: ``_refuse_blocked_demand`` runs before the
        # issue loop, so a refused component never reaches the legacy fence or the delta.
        if refuses_leg or part_id in refused_part_ids:
            line.suppressed = True
            line.suppression_reason = SUPPRESSION_BLOCKING_DIAGNOSTIC
        elif _component_already_issued(db, work_order.id, part_id, company_id):
            line.suppressed = True
            line.suppression_reason = SUPPRESSION_ALREADY_ISSUED
        else:
            delta = target - line.already_issued
            if delta <= _EPSILON:
                line.suppressed = True
                line.suppression_reason = SUPPRESSION_CONVERGED
            else:
                line.delta_quantity = delta
                _plan_preview_line(
                    db,
                    part_id=part_id,
                    quantity=delta,
                    company_id=company_id,
                    pinned_inventory_item_id=None,
                    proxies=proxies,
                    part_sources=part_sources,
                    line=line,
                )
        preview.lines.append(line)

    # The two suppression layers' output, reported rather than omitted: material a tie
    # owns still moves, just not on this leg, and a preview that dropped these lines
    # would read as "the BOM asks for nothing here".
    for part_id, demand in resolution.allocation_blocked.items():
        line = _new_line(part_id, BACKFLUSH_SOURCE_BOM)
        line.required_quantity = demand
        line.suppressed = True
        line.suppression_reason = SUPPRESSION_OPEN_OPERATION_TIE
        preview.lines.append(line)
    for part_id, (demand, net) in resolution.ledger_blocked.items():
        line = _new_line(part_id, BACKFLUSH_SOURCE_BOM)
        line.required_quantity = demand
        line.already_issued = net
        line.suppressed = True
        line.suppression_reason = SUPPRESSION_LEDGER_CONSUMED
        preview.lines.append(line)

    return preview


def backflush_readiness_for_part(db: Session, part: Part, *, company_id: int) -> list[_BackflushDiagnostic]:
    """Can this part safely opt into automatic backflush? **PURE READ — writes NOTHING.**

    Runs the same BOM explosion the completion leg runs, at a SYNTHETIC BASIS OF 1.0, and
    returns its diagnostics. The basis is the trap this function exists to avoid: the real
    resolver takes ``quantity_complete + operation scrap`` and short-circuits to an empty
    answer below epsilon (``_backflush_basis``), so a readiness check run at opt-in time --
    when by definition no work order for the part has produced anything yet -- would walk
    no BOM at all and pronounce EVERY part clean. One unit of demand exercises every line
    of the structure without needing a job.

    Only the BOM half can be answered here. Routing conditions (an operation naming the
    work order's own part, two operations disagreeing, routing demand the BOM excludes) are
    WORK-ORDER-scoped: there is no work order at opt-in time, and the routing of a job that
    does not exist yet cannot be checked. Those surface on the dry-run preview instead,
    which is why the gate can never be sound as a one-time check -- every input it reads is
    mutable afterwards by other people. Re-run it.

    Blockers are the caller's refusal set; advisories are informational. See
    ``_BackflushDiagnostic``.

    **The SUBJECT part's own soft-delete is checked here, and that is deliberate
    placement.** ``Part`` carries ``SoftDeleteMixin``, the list endpoints filter it, and
    the ``PUT /parts/{id}`` / ``PUT /materials/{id}`` lookups do NOT -- a pre-existing
    omission this feature inherits rather than introduces. It stops being cosmetic here:
    ``delete_part`` checks dependencies only on a HARD delete, so a soft-deleted part
    keeps its in-flight work orders, and arming it would move component stock on behalf of
    a part the shop believes is gone. One blocking diagnostic closes BOTH doors (the
    refusal gate calls this function, and so does the readiness GET) without touching a
    lookup four other handlers share.
    """
    # Imported lazily to avoid an import cycle with the endpoints module.
    from app.api.endpoints.work_orders import _get_active_bom

    explosion = _BackflushBomExplosion()
    if bool(getattr(part, "is_deleted", False)):
        explosion.diagnostics.append(
            _BackflushDiagnostic(
                code="deleted_part",
                severity=BACKFLUSH_BLOCKING,
                detail=(
                    "this part is deleted, so arming it would consume component stock for a part the shop "
                    "believes is gone. Restore the part first"
                ),
                # NO ``component_part_id`` / ``component_part_number``. Those two fields mean
                # "the COMPONENT this diagnostic is about", and every reader glosses them that
                # way -- the readiness card renders "· {component_part_number}" beside the
                # sentence. This diagnostic is about the SUBJECT part, which the response
                # already names at the top level, so populating them made the card read as
                # though the subject part were a component of itself.
            )
        )
    bom = _get_active_bom(db, part.id, company_id)
    routing_names_a_component = (
        db.query(WorkOrderOperation.id)
        .join(WorkOrder, WorkOrder.id == WorkOrderOperation.work_order_id)
        .filter(
            WorkOrderOperation.company_id == company_id,
            WorkOrder.company_id == company_id,
            WorkOrder.part_id == part.id,
            WorkOrderOperation.component_part_id.isnot(None),
        )
        .first()
        is not None
    )

    if bom is not None:
        _record_bom_header_diagnostics(bom, explosion)
        _explode_backflush_bom(
            db,
            bom,
            company_id,
            parent_qty=1.0,
            visited_part_ids={bom.part_id},
            out=explosion,
            consumed=True,
        )
        explosion.excluded_part_ids -= set(explosion.demand)
    elif routing_names_a_component:
        explosion.diagnostics.append(
            _BackflushDiagnostic(
                code="routing_only_no_bom",
                severity=BACKFLUSH_ADVISORY,
                detail=(
                    f"Part {part.part_number} has no active BOM; its component demand would come only from "
                    "routing operations that name a component."
                ),
            )
        )

    if not explosion.demand and not routing_names_a_component:
        explosion.diagnostics.append(
            _BackflushDiagnostic(
                code="no_demand_source",
                severity=BACKFLUSH_BLOCKING,
                detail=(
                    "it has no active BOM line that would be issued and no routing operation names a "
                    "component, so enabling backflush would consume nothing. Give it an active BOM first"
                ),
            )
        )
    return explosion.diagnostics


def apply_completion_inventory_effects(
    db: Session,
    work_order: WorkOrder,
    *,
    user_id: int,
    company_id: int,
    audit: AuditService,
) -> BackflushResult:
    """Run the full completion inventory effect: FG receipt ALWAYS, backflush if gated.

    Single entry point for the completion handlers. The FG receipt is always
    performed (idempotent); the backflush only runs when the finished part opts in.
    Returns the backflush result (shortages) so the caller can surface / log them.
    Does NOT commit -- the caller owns the transaction so these writes are atomic
    with the completion on the live paths.

    Material consumption for OPERATION-scoped ties runs here too, so it inherits every
    existing completion call site (kiosk clock-out, shop-floor + office op complete,
    force-complete, reconcile-on-read) WITHOUT adding one. It is a no-op -- not one
    write -- on an untied work order, and it never raises (see
    ``consume_tied_materials_for_work_order``). Imported lazily: that module imports
    helpers from THIS one.

    The work order's OPEN material ties are read ONCE here and threaded into both legs.
    Three separate legs used to issue the same tenant-scoped SELECT (the consume engine,
    the backflush-precedence drop, and the work-order-scoped tie leg), so a completion
    cost 2 unconditional reads of ``work_order_material_allocations`` plus a third when
    the part opted into backflush. It is now exactly ONE, tied or not -- and that is also
    what lets ``backflush_components_for_work_order``'s invariant-6(d) gate answer "is
    this work order untied?" with ZERO queries.
    """
    from app.services.material_consumption_service import (
        consume_tied_materials_for_work_order,
        open_allocations_for_work_order,
    )

    allocations = open_allocations_for_work_order(db, work_order.id, company_id)

    receive_finished_goods_for_work_order(db, work_order, user_id=user_id, company_id=company_id, audit=audit)
    consume_tied_materials_for_work_order(
        db, work_order, user_id=user_id, company_id=company_id, audit=audit, allocations=allocations
    )
    return backflush_components_for_work_order(
        db, work_order, user_id=user_id, company_id=company_id, audit=audit, allocations=allocations
    )


def apply_operation_completion_inventory_effects(
    db: Session,
    work_order: WorkOrder,
    operation: WorkOrderOperation,
    *,
    user_id: int,
    company_id: int,
    audit: AuditService,
) -> None:
    """Run the inventory effects of ONE OPERATION completing. Today: tied consumption.

    The operation-level analogue of ``apply_completion_inventory_effects``, and the one
    seam the four operation-completion handlers call right after
    ``finalize_operation_completion``. It exists even though it currently does a single
    thing so that the NEXT operation-scoped inventory effect has an obvious home and
    lands on all four handlers at once -- the same reason its work-order-level sibling
    exists (CLAUDE.md: add completion side-effects at the seam, not at the call sites).

    **The finished-goods RECEIVE and the component backflush deliberately DO NOT fire
    here, and the reason is their DEMAND BASIS, not an index.** Both are correctly
    WORK-ORDER-scoped: the FG receipt books the job's produced quantity into stock
    against ``work_order.lot_number``, and the backflush's demand comes from the WHOLE
    JOB -- a BOM exploded against ``quantity_complete + scrapped`` for the finished part,
    and work-order-scoped ties that name no operation at all. Neither has an
    operation-sized unit of work to be computed from, so firing them per operation would
    double-receive a multi-operation job and repeatedly re-explode one job's BOM.

    (The older statement of this rested on ``uq_wo_inventory_issue`` "physically
    constraining the backflush to one row per (work order, part)". That premise is no
    longer true for this leg -- it posts ``work_order_backflush``, outside that index,
    precisely so it can write N rows -- but the conclusion is unchanged, because it never
    depended on the index.)

    Does NOT commit -- joins the caller's unit of work, so consumption lands atomically
    with the operation status change. Writes NOTHING at all when the operation carries no
    ties.

    **Propagates ``StaleDataError`` and nothing else.** Every other failure degrades into
    a recorded ``ALLOCATION_CONSUMPTION_FAILED`` row inside the engine; an optimistic-lock
    conflict (invariant 4) is re-raised so it becomes the calling handler's documented
    409 rather than a 200 that silently skipped a material deduction. Do NOT wrap this
    call in a bare ``except`` -- that would restore exactly the failure it prevents.
    Callers must gate on the work order not being terminal.
    Imported lazily: that module imports helpers from THIS one.
    """
    from app.services.material_consumption_service import consume_tied_materials_for_operation

    consume_tied_materials_for_operation(db, work_order, operation, user_id=user_id, company_id=company_id, audit=audit)


def _existing_shipment_ship_txn(db: Session, shipment_id: int, company_id: int) -> bool:
    """True if a SHIP txn for this shipment already exists (idempotency key, G2).

    The offsetting finished-goods decrement on ship is keyed on a prior SHIP
    ``InventoryTransaction`` with ``reference_type='shipment', reference_id=shipment.id``
    so a re-submitted / concurrent double-ship can never double-decrement. (There is no
    DB partial-unique index for SHIP txns this batch -- see the follow-up note in
    ``decrement_finished_goods_for_shipment`` -- so this application check, combined with
    the caller's ``with_for_update`` row lock on the shipment, is the idempotency guard.)
    """
    return (
        db.query(InventoryTransaction.id)
        .filter(
            InventoryTransaction.company_id == company_id,
            InventoryTransaction.reference_type == "shipment",
            InventoryTransaction.reference_id == shipment_id,
            InventoryTransaction.transaction_type == TransactionType.SHIP,
        )
        .first()
        is not None
    )


def _record_ship_discrepancy(
    db: Session,
    *,
    work_order: WorkOrder,
    shipment: Shipment,
    audit_action: str,
    event_type: str,
    description: str,
    extra: dict,
    company_id: int,
    user_id: int,
    audit: AuditService,
) -> None:
    """Persist a ship-side discrepancy as a tamper-evident audit row + warning event (G2).

    Mirrors ``_record_backflush_shortage``: ONE ``audit_log`` row on the immutable hash
    chain (never written directly) + a warning ``OperationalEvent`` for AI/realtime
    consumers. Tenant-scoped. Both only flush (never commit), so the records land
    atomically with the ship/close on the caller's unit of work; the event emit is
    wrapped so a transient signal failure can never fail an in-flight ship (the audit
    row is the compliance record). Used for BOTH the FG-not-found case and the over-ship
    case -- neither fails the ship (warn-and-record posture).
    """
    audit.log(
        action=audit_action,
        resource_type="shipment",
        resource_id=shipment.id,
        resource_identifier=shipment.shipment_number,
        description=description,
        new_values={"shipment_number": shipment.shipment_number},
        extra_data=extra,
        company_id=company_id,
    )
    try:
        OperationalEventService(db).emit(
            company_id=company_id,
            event_type=event_type,
            source_module="shipping",
            entity_type="shipment",
            entity_id=shipment.id,
            work_order_id=work_order.id,
            user_id=user_id,
            severity="warning",
            event_payload=extra,
        )
    except Exception:  # pragma: no cover - a warning signal must never fail a ship
        logger.exception(
            "%s event emit failed for shipment %s WO %s (company %s)",
            event_type,
            shipment.id,
            work_order.id,
            company_id,
        )


def record_over_ship_if_needed(
    db: Session,
    *,
    work_order: WorkOrder,
    shipment: Shipment,
    company_id: int,
    user_id: int,
    audit: AuditService,
) -> Optional[float]:
    """Warn-and-record over-ship (G2): cumulative shipped beyond produced quantity.

    Ceiling = ``WorkOrder.quantity_complete`` (what was actually produced/received into
    FG; there is no SalesOrder table). Cumulative shipped = SUM(``quantity_shipped``)
    over this WO's NON-CANCELLED shipments (this shipment is already SHIPPED at the call
    site, so it is included). If cumulative shipped exceeds the produced quantity, the
    ship is ALLOWED (no 400) but a warning ``OperationalEvent`` + tamper-evident audit
    row record the over-ship (mirrors the backflush-shortage recording). Tenant-scoped;
    joins the caller's unit of work (no commit). Returns the overage when one was
    recorded, else ``None``.
    """
    produced = float(work_order.quantity_complete or 0)
    cumulative = (
        db.query(func.coalesce(func.sum(Shipment.quantity_shipped), 0.0))
        .filter(
            Shipment.company_id == company_id,
            Shipment.work_order_id == work_order.id,
            Shipment.status != ShipmentStatus.CANCELLED,
        )
        .scalar()
    )
    cumulative_shipped = float(cumulative or 0.0)
    overage = cumulative_shipped - produced
    if overage <= _EPSILON:
        return None

    extra = {
        "work_order_id": work_order.id,
        "work_order_number": work_order.work_order_number,
        "shipment_number": shipment.shipment_number,
        "quantity_shipped": float(shipment.quantity_shipped or 0),
        "cumulative_shipped": cumulative_shipped,
        "quantity_complete": produced,
        "overage": overage,
    }
    _record_ship_discrepancy(
        db,
        work_order=work_order,
        shipment=shipment,
        audit_action=OVER_SHIP_AUDIT_ACTION,
        event_type=OVER_SHIP_EVENT_TYPE,
        description=(
            f"Over-ship on WO {work_order.work_order_number}: cumulative shipped {cumulative_shipped} "
            f"exceeds produced {produced} by {overage} (shipment {shipment.shipment_number})"
        ),
        extra=extra,
        company_id=company_id,
        user_id=user_id,
        audit=audit,
    )
    logger.warning(
        "Over-ship on WO %s (company %s): cumulative %s > produced %s by %s",
        work_order.id,
        company_id,
        cumulative_shipped,
        produced,
        overage,
    )
    return overage


def decrement_finished_goods_for_shipment(
    db: Session,
    *,
    work_order: WorkOrder,
    shipment: Shipment,
    company_id: int,
    user_id: int,
    audit: AuditService,
) -> Optional[InventoryTransaction]:
    """Write the offsetting finished-goods decrement when a shipment ships (G2).

    The mirror of Batch-6's ``receive_finished_goods_for_work_order``: that RECEIVEs the
    produced quantity into the FG lot on completion; this writes the negative SHIP txn
    and decrements on-hand/available when those finished goods leave on a shipment. The
    decremented row is located by ``company_id + part_id == work_order.part_id +
    location == FINISHED_GOODS_LOCATION + lot_number == work_order.lot_number`` -- exactly
    the row the receipt created (the constants are imported, not re-declared, so the two
    sides stay in lock-step).

    Idempotency + concurrency: the caller holds a ``with_for_update`` row lock on the
    shipment when it re-checks status, serializing concurrent double-ship; here we ALSO
    short-circuit if a SHIP txn for this shipment already exists
    (``_existing_shipment_ship_txn``) and insert under a savepoint so a duplicate is a
    clean no-op rather than aborting the ship/close. (FOLLOW-UP: a DB partial-unique
    index on ``(company_id, reference_type='shipment', reference_id, SHIP)`` would harden
    this the way ``uq_wo_inventory_receipt`` hardens the receipt; deferred -- this batch
    is migration-free.)

    FG-not-found: if no matching FINISHED-GOODS lot row exists (receipt skipped, lot
    changed, stock already moved), DO NOT fail the ship -- record a discrepancy warning
    (audit row + OperationalEvent) and return ``None``, leaving the caller to proceed
    with the ship/close (mirrors ``_record_backflush_shortage``).

    Does NOT commit -- joins the caller's unit of work so the decrement + audit land
    atomically with the SHIPPED status change + WO close. Returns the SHIP txn, or
    ``None`` on a no-op (already shipped, nothing to ship, or FG row not found).
    """
    if _existing_shipment_ship_txn(db, shipment.id, company_id):
        return None

    quantity = float(shipment.quantity_shipped or 0)
    if quantity <= 0:
        # Nothing to decrement (a zero-quantity shipment); not a discrepancy.
        return None

    lot_number = work_order.lot_number

    inv_item = (
        db.query(InventoryItem)
        .filter(
            InventoryItem.company_id == company_id,
            InventoryItem.part_id == work_order.part_id,
            InventoryItem.location == FINISHED_GOODS_LOCATION,
            InventoryItem.lot_number == lot_number,
        )
        .first()
        if lot_number
        else None
    )

    if inv_item is None:
        # FG-not-found: receipt was skipped, the lot changed, or stock already moved.
        # Warn-and-record (audit + event) and proceed with the ship/close -- never fail.
        part = db.query(Part).filter(Part.id == work_order.part_id, Part.company_id == company_id).first()
        extra = {
            "work_order_id": work_order.id,
            "work_order_number": work_order.work_order_number,
            "shipment_number": shipment.shipment_number,
            "part_id": work_order.part_id,
            "part_number": part.part_number if part else None,
            "lot_number": lot_number,
            "location": FINISHED_GOODS_LOCATION,
            "quantity_shipped": quantity,
        }
        _record_ship_discrepancy(
            db,
            work_order=work_order,
            shipment=shipment,
            audit_action=SHIP_FG_MISSING_AUDIT_ACTION,
            event_type=SHIP_FG_MISSING_EVENT_TYPE,
            description=(
                f"Ship of {quantity} for WO {work_order.work_order_number} found no finished-good lot "
                f"{lot_number or '(none)'} at {FINISHED_GOODS_LOCATION}; on-hand not decremented "
                f"(shipment {shipment.shipment_number})"
            ),
            extra=extra,
            company_id=company_id,
            user_id=user_id,
            audit=audit,
        )
        logger.warning(
            "Ship FG lot missing for WO %s lot %s (company %s); on-hand not decremented",
            work_order.id,
            lot_number,
            company_id,
        )
        return None

    part = db.query(Part).filter(Part.id == work_order.part_id, Part.company_id == company_id).first()
    part_number = part.part_number if part else None
    unit_cost = float(inv_item.unit_cost or (part.standard_cost if part else 0) or 0)

    # Insert the SHIP txn FIRST under a savepoint; the decrement applies ONLY when the
    # insert actually committed (mirrors the receipt/issue order). The savepoint also keeps
    # the outer ship/close transaction usable if a future DB unique index ever fires.
    txn = InventoryTransaction(
        company_id=company_id,
        inventory_item_id=inv_item.id,
        part_id=work_order.part_id,
        transaction_type=TransactionType.SHIP,
        quantity=-quantity,
        from_location=FINISHED_GOODS_LOCATION,
        lot_number=lot_number,
        reference_type="shipment",
        reference_id=shipment.id,
        reference_number=shipment.shipment_number,
        unit_cost=unit_cost,
        total_cost=quantity * unit_cost,
        notes=(f"Finished-goods shipment {shipment.shipment_number} for work order " f"{work_order.work_order_number}"),
        created_by=user_id,
    )
    if not _insert_txn_with_savepoint(db, txn):
        # A concurrent ship already wrote this shipment's SHIP txn -> idempotent no-op
        # (do NOT decrement; the winning txn owns the movement).
        return None

    old_quantity_on_hand = inv_item.quantity_on_hand
    inv_item.quantity_on_hand = float(inv_item.quantity_on_hand or 0) - quantity
    inv_item.quantity_available = inv_item.quantity_on_hand - float(inv_item.quantity_allocated or 0)
    db.flush()

    # Tamper-evident audit (hash chain) for the outbound stock movement (mirrors the
    # FG-receipt audit). Flushed inside the caller's unit of work.
    audit.log_create(
        "inventory",
        txn.id,
        str(txn.id),
        new_values=txn,
        description=(
            f"Shipped {quantity} of part {part_number or work_order.part_id} from {FINISHED_GOODS_LOCATION} "
            f"lot {lot_number} on shipment {shipment.shipment_number} (WO {work_order.work_order_number})"
        ),
    )
    if old_quantity_on_hand is not None:
        audit.log_update(
            "inventory",
            inv_item.id,
            f"{part_number or work_order.part_id} @ {FINISHED_GOODS_LOCATION}",
            old_values={"quantity_on_hand": old_quantity_on_hand},
            new_values={"quantity_on_hand": inv_item.quantity_on_hand},
            description=f"Shipment decrement: stock for part {part_number or work_order.part_id}",
        )

    return txn
