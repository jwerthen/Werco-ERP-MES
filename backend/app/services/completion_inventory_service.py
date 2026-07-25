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
* **Idempotent.** Re-entry (reconcile re-read, re-completion of an already-terminal
  WO, a retried request) must never double-receive or double-issue. The idempotency
  key is the existence of a prior work-order-referencing ``InventoryTransaction``
  for the same company:
    - FG receipt: ANY ``RECEIVE`` txn with ``reference_type='work_order',
      reference_id=work_order.id, company_id`` -> already received, no-op.
    - Backflush: per component part, ANY ``ISSUE`` txn with
      ``reference_type='work_order', reference_id=work_order.id, part_id=<component>,
      company_id`` -> that component is already issued, skip it.
* **Tenant-scoped.** ``company_id`` is stamped on every row and every lookup filters
  it (invariant #1). The caller passes the ACTIVE company.
* **Audited.** Each new ``InventoryTransaction`` is logged via ``AuditService``
  (mirrors ``receiving.py`` / ``inventory.py``) so stock movement lands on the hash
  chain, not just the AI ``OperationalEvent`` store.

Lot-only (no serialization flag exists yet): on FG receipt we assign
``work_order.lot_number`` if empty (a per-company-unique lot derived from the WO
number) and leave ``InventoryItem.serial_number`` NULL. Serial assignment is a
tracked follow-up.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.ledger_filter import WORK_ORDER_REFERENCE_TYPE
from app.models.inventory import InventoryItem, InventoryTransaction, TransactionType
from app.models.operational_event import OperationalEvent  # noqa: F401  (imported for type/test discoverability)
from app.models.part import Part
from app.models.shipping import Shipment, ShipmentStatus
from app.models.work_order import WorkOrder, WorkOrderOperation
from app.models.work_order_material import WorkOrderMaterialAllocation
from app.services.audit_service import AuditService
from app.services.operational_event_service import OperationalEventService

logger = logging.getLogger(__name__)

# Tamper-evident audit action + operational-event type for a backflush shortage
# (a component driven negative on-hand). A silent negative stock is a material-trail
# control gap in a regulated (AS9100D/CMMC-L2) system, so a shortage is recorded on
# the hash chain AND emitted as a warning OperationalEvent (item 3).
BACKFLUSH_SHORTAGE_AUDIT_ACTION = "BACKFLUSH_SHORTAGE"
BACKFLUSH_SHORTAGE_EVENT_TYPE = "backflush_shortage"

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
# and ``material_consumption_service``, which imports it). Quantities are ``Float``
# columns, so "is this quantity zero?" must never be an exact ``== 0`` / ``> 0`` test:
# a target computed as ``0.1 * 3`` is not ``0.3``. Previously three different spellings
# were in use -- a bare ``1e-9`` here, ``_EPSILON`` in the consumption engine, and plain
# ``<= 0`` / ``> 0`` on the tie legs -- so the same near-zero delta could be "zero" in
# one guard and "positive" in the next.
_EPSILON = 1e-9


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
class WorkOrderMaterialAllocationDemand:
    """Summed one-shot demand a part carries from WORK-ORDER-scoped material ties.

    ``allocation_ids`` are every tie that contributed, so a single emitted ISSUE can
    mark all of them consumed (``uq_wo_inventory_issue`` allows only one ISSUE row per
    (company, WO, part), so the ledger row carries the FIRST tie's ``allocation_id``
    and the rest are reconciled through their ``qty_consumed``).

    ``pinned_inventory_item_id`` is the tie's LOT PIN, carried through to the ISSUE so a
    work-order-scoped tie honors its pin exactly as the operation-scoped engine does.
    Dropping it here (the shape this dataclass originally had) meant a planner could pin
    a heat-certified lot, get a 201, and watch the ledger issue from a different lot --
    the as-built genealogy naming material the operator never touched (AS9100D 8.5.2).
    ``uq_wo_material_alloc_open_wo`` permits at most ONE open work-order-scoped tie per
    (company, WO, part), so there is never a second, conflicting pin to reconcile; the
    summing branch below exists only for the CANCELLED-row edge and keeps the FIRST pin.
    """

    allocation_ids: list[int] = field(default_factory=list)
    quantity: float = 0.0
    pinned_inventory_item_id: Optional[int] = None


@dataclass
class IssueOutcome:
    """What one ``_issue_one_component`` call actually did.

    ``posted`` distinguishes "the ISSUE row landed" from "a concurrent completion had
    already written it, so this call inserted nothing" -- two cases the old
    ``Optional[ComponentShortage]`` return collapsed into ``None``. The caller must not
    advance a tie's ``qty_consumed`` cache on the second one: the winning transaction's
    rows own that quantity, and the cache is the exact field the untie endpoint refuses
    against (409 once ``qty_consumed > 0``). Its operation-scoped twin has always made
    this distinction (``posted_any`` in ``_consume_one_allocation``).
    """

    posted: bool = False
    shortage: Optional[ComponentShortage] = None


@dataclass
class BackflushResult:
    issued_part_ids: list[int] = field(default_factory=list)
    shortages: list[ComponentShortage] = field(default_factory=list)


def _existing_work_order_receipt(db: Session, work_order_id: int, company_id: int) -> bool:
    """True if a finished-good RECEIVE for this WO already exists (idempotency key)."""
    return (
        db.query(InventoryTransaction.id)
        .filter(
            InventoryTransaction.company_id == company_id,
            InventoryTransaction.reference_type == "work_order",
            InventoryTransaction.reference_id == work_order_id,
            InventoryTransaction.transaction_type == TransactionType.RECEIVE,
        )
        .first()
        is not None
    )


def _component_already_issued(db: Session, work_order_id: int, component_part_id: int, company_id: int) -> bool:
    """True if this component was already backflushed for this WO (idempotency key)."""
    return (
        db.query(InventoryTransaction.id)
        .filter(
            InventoryTransaction.company_id == company_id,
            InventoryTransaction.reference_type == "work_order",
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
    received, or nothing to receive).
    """
    if _existing_work_order_receipt(db, work_order.id, company_id):
        return None

    quantity = float(work_order.quantity_complete or 0)
    if quantity <= 0:
        # Nothing produced (e.g. fully scrapped) -- no finished good to receive.
        return None

    part = db.query(Part).filter(Part.id == work_order.part_id, Part.company_id == company_id).first()
    if part is None:
        logger.warning(
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
        reference_type="work_order",
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
    the consume leg, the backflush-precedence drop and the work-order-scoped demand,
    which used to issue the same tenant-scoped SELECT up to three times per completion.
    The parameter stays optional so the individual legs remain independently callable
    (tests and any future call site) without a behavior change.
    """
    if allocations is not None:
        return allocations
    from app.services.material_consumption_service import open_allocations_for_work_order

    return open_allocations_for_work_order(db, work_order.id, company_id)


def _resolve_backflush_components(
    db: Session,
    work_order: WorkOrder,
    company_id: int,
    allocations: Optional[list[WorkOrderMaterialAllocation]] = None,
) -> dict[int, float]:
    """Required quantity per component part for backflushing this WO.

    Prefers the WO operations' ``component_part_id`` / ``component_quantity`` (the
    routing already carries explicit component demand). Falls back to exploding the
    finished part's active BOM via the existing tenant-scoped ``_collect_bom_components``
    helper (which already applies ``BOMItem.scrap_factor``). Quantities are scaled by
    the produced quantity. Returns ``{}`` when no component demand exists.
    """
    produced = float(work_order.quantity_complete or 0)
    if produced <= 0:
        return {}

    required: dict[int, float] = {}

    # 1) Explicit operation component demand (assembly WOs with component ops).
    operations = (
        db.query(WorkOrderOperation)
        .filter(
            WorkOrderOperation.work_order_id == work_order.id,
            WorkOrderOperation.company_id == company_id,
            WorkOrderOperation.component_part_id.isnot(None),
        )
        .all()
    )
    for op in operations:
        if op.component_part_id is None:
            continue
        per_unit = float(op.component_quantity or 0)
        if per_unit <= 0:
            continue
        required[op.component_part_id] = required.get(op.component_part_id, 0.0) + per_unit * produced

    if not required:
        # 2) Fall back to exploding the finished part's BOM (scrap_factor applied by the
        #    helper). Imported lazily to avoid an import cycle with the endpoints module.
        from app.api.endpoints.work_orders import _collect_bom_components, _get_active_bom

        bom = _get_active_bom(db, work_order.part_id, company_id)
        if not bom:
            return {}
        for _item, component, extended_qty in _collect_bom_components(db, bom, company_id, parent_qty=produced):
            required[component.id] = required.get(component.id, 0.0) + float(extended_qty or 0)

    # 3) ALLOCATION PRECEDENCE. A part covered by an OPEN operation-scoped material
    #    allocation on this WO is owned by the material-consumption engine, which posts
    #    per-run ISSUEs with reference_type='work_order_operation'. Those rows are
    #    OUTSIDE the uq_wo_inventory_issue predicate, so nothing at the DB level would
    #    stop this WO-level backflush from ALSO issuing the same material -- a silent
    #    double-issue. Drop those parts here; the allocation is the sole demand carrier
    #    (we deliberately do NOT write op.component_part_id from a tie).
    return _drop_allocation_covered_parts(db, work_order, company_id, required, allocations)


def _drop_allocation_covered_parts(
    db: Session,
    work_order: WorkOrder,
    company_id: int,
    required: dict[int, float],
    allocations: Optional[list[WorkOrderMaterialAllocation]] = None,
) -> dict[int, float]:
    """Remove parts covered by an OPEN OPERATION-scoped allocation from backflush demand.

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
    """
    if not required:
        return required

    allocations = [a for a in _open_allocations(db, work_order, company_id, allocations) if a.part_id in required]
    operation_ids = {a.work_order_operation_id for a in allocations if a.work_order_operation_id is not None}
    if not operation_ids:
        return required

    live_operation_ids = {
        row[0]
        for row in db.query(WorkOrderOperation.id)
        .filter(
            WorkOrderOperation.company_id == company_id,
            WorkOrderOperation.work_order_id == work_order.id,
            WorkOrderOperation.id.in_(operation_ids),
        )
        .all()
    }
    for allocation in allocations:
        if allocation.work_order_operation_id in live_operation_ids:
            required.pop(allocation.part_id, None)
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
    return required


def _work_order_scoped_allocation_demand(
    db: Session,
    work_order: WorkOrder,
    company_id: int,
    allocations: Optional[list[WorkOrderMaterialAllocation]] = None,
) -> dict[int, WorkOrderMaterialAllocationDemand]:
    """One-shot demand contributed by WORK-ORDER-scoped (non-operation) ties.

    A work-order-scoped tie has no operation to scale against, so its demand is the
    full ``qty_planned``, consumed once at completion through the SAME one-shot
    backflush machinery (``reference_type='work_order'``). That matters because
    ``uq_wo_inventory_issue`` permits exactly ONE ISSUE per (company, WO, part): demand
    from the BOM/routing and demand from a tie must be SUMMED into that single row, not
    emitted twice (the second insert would be swallowed as a duplicate no-op and the
    tie would silently never consume).

    Unlike the BOM backflush this is NOT gated on ``part.backflush_components``: an
    explicit tie IS the opt-in. Keyed by part id; a second tie on the same part sums in.

    The tie's LOT PIN travels with the demand. It has to: this leg is the only consumer
    of a work-order-scoped tie, so a pin dropped here is a pin that never happens, and
    the ISSUE would silently pick a different lot than the one the tie names.
    """
    demand: dict[int, WorkOrderMaterialAllocationDemand] = {}
    for allocation in _open_allocations(db, work_order, company_id, allocations):
        if allocation.work_order_operation_id is not None:
            continue
        quantity = float(allocation.qty_planned or 0) - float(allocation.qty_consumed or 0)
        if quantity <= _EPSILON:
            continue
        existing = demand.get(allocation.part_id)
        if existing is None:
            demand[allocation.part_id] = WorkOrderMaterialAllocationDemand(
                allocation_ids=[allocation.id],
                quantity=quantity,
                pinned_inventory_item_id=allocation.pinned_inventory_item_id,
            )
        else:
            existing.allocation_ids.append(allocation.id)
            existing.quantity += quantity
            if existing.pinned_inventory_item_id is None:
                existing.pinned_inventory_item_id = allocation.pinned_inventory_item_id
    return demand


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
    """Backflush a single component: write ONE ISSUE txn + decrement source stock.

    The work-order ISSUE unique index keys on ``(company, WO, ISSUE, part_id)``, so a
    component is consumed by EXACTLY ONE ISSUE per WO. We therefore write a single
    negative ISSUE for the FULL ``required_qty`` against the primary source lot,
    carrying that lot on the txn for genealogy. If total on-hand is insufficient, the
    primary lot is driven NEGATIVE (consumption + true demand still RECORDED, matching
    the permissive ``/inventory/adjust`` behavior) and a ``ComponentShortage`` is
    reported (never raised) -- additionally recorded tamper-evidently (item 3). The
    ISSUE INSERT is wrapped in a SAVEPOINT (item 1): a concurrent duplicate (the
    double-issue race the index catches) rolls back only the savepoint and is a clean
    no-op (no decrement, no shortage record), reported as ``posted=False``.

    **Lot selection.** ``pinned_inventory_item_id`` (set only when a WORK-ORDER-scoped
    material tie carried a lot pin) consumes from THAT lot exclusively -- pinning is a
    lot-directed instruction, so an insufficient pinned lot is driven negative rather
    than silently spilling onto a different, uncertified, wrong-heat lot. This mirrors
    the operation-scoped engine exactly. Unpinned demand keeps the historical behavior:
    the lowest-id active row with on-hand, else a placeholder at standard cost.

    **The pin bypasses ordering, NOT the hold check.** A pinned lot that is inactive or
    not ``available`` is still consumed -- the material is already in the part and this
    path also runs from a reconcile-on-read GET, where refusing would be unattributable
    -- but the fact goes on the tamper-evident chain as ``HELD_MATERIAL_CONSUMED``
    (AS9100D 8.7). The tie endpoint refuses to PIN a held lot (422), so the row can only
    ever mean "held after it was pinned".
    """
    part = db.query(Part).filter(Part.id == component_part_id, Part.company_id == company_id).first()
    part_number = part.part_number if part else None
    unit_cost = float(part.standard_cost or 0) if part else 0.0

    # Imported lazily: ``material_consumption_service`` imports helpers from THIS module.
    from app.services.material_consumption_service import is_consumable_item, record_held_material_consumed

    held_item: Optional[InventoryItem] = None
    if pinned_inventory_item_id is not None:
        pinned = (
            db.query(InventoryItem)
            .filter(
                InventoryItem.id == pinned_inventory_item_id,
                InventoryItem.company_id == company_id,
            )
            .first()
        )
        source_items = [pinned] if pinned is not None else []
        if pinned is not None and not is_consumable_item(pinned):
            held_item = pinned
    else:
        source_items = (
            db.query(InventoryItem)
            .filter(
                InventoryItem.company_id == company_id,
                InventoryItem.part_id == component_part_id,
                InventoryItem.is_active == True,  # noqa: E712
                InventoryItem.quantity_on_hand > 0,
            )
            .order_by(InventoryItem.id)
            .all()
        )
    available_total = sum(float(i.quantity_on_hand or 0) for i in source_items)

    # Primary consumed lot: the pinned lot, else the lowest-id on-hand row, else a
    # placeholder row (so the negative consumption is still recorded against a real item).
    target = source_items[0] if source_items else None
    if target is None:
        target = _placeholder_stock_row(db, part_id=component_part_id, company_id=company_id, unit_cost=unit_cost)

    # ONE ISSUE for the full required quantity, inserted FIRST under a savepoint; the
    # decrement applies ONLY when the insert actually committed. A duplicate (a
    # concurrent completion already issued this component) is a clean no-op -- no
    # decrement, no shortage record -- so it can never double-consume or abort the
    # outer completion / reconcile transaction.
    txn = _write_issue_txn(
        db,
        work_order,
        inventory_item=target,
        component_part_id=component_part_id,
        quantity=required_qty,
        unit_cost=float(target.unit_cost or unit_cost),
        lot_number=target.lot_number,
        company_id=company_id,
        user_id=user_id,
        audit=audit,
        part_number=part_number,
        allocation_id=allocation_id,
    )
    if txn is None:
        return IssueOutcome(posted=False)

    outcome = IssueOutcome(posted=True)

    if held_item is not None:
        # A pin has exactly ONE source lot, so the whole demand came off the held lot
        # (whether through the normal take or by driving it negative).
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
        )

    shortfall = required_qty - available_total
    if shortfall > _EPSILON:
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
            consumed_lot=target.lot_number,
            company_id=company_id,
            user_id=user_id,
            audit=audit,
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
) -> None:
    """Persist a backflush shortage as a tamper-evident audit row + OperationalEvent (item 3).

    Writes ONE ``audit_log`` row (action ``BACKFLUSH_SHORTAGE``) on the component part,
    carrying the shortfall qty + consumed lot + the producing WO in ``extra_data`` so the
    negative on-hand is on the immutable hash chain (never written directly). Then emits a
    ``backflush_shortage`` ``OperationalEvent`` (``severity="warning"``) for AI/realtime
    consumers. Tenant-scoped (``company_id`` on both). The audit ``log`` and the event
    ``emit`` both only flush (never commit), so the records land atomically with the
    completion on the live paths; the event emit is wrapped so a transient signal failure
    can never fail an in-flight completion (the audit row is the compliance record).
    """
    extra = {
        "work_order_id": work_order.id,
        "work_order_number": work_order.work_order_number,
        "component_part_id": shortage.part_id,
        "component_part_number": shortage.part_number,
        "required_quantity": shortage.required_quantity,
        "available_quantity": shortage.available_quantity,
        "shortfall": shortage.shortfall,
        "consumed_lot": consumed_lot,
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
    allocation_id: Optional[int] = None,
    reference_type: str = WORK_ORDER_REFERENCE_TYPE,
    reference_id: Optional[int] = None,
    notes: Optional[str] = None,
    movement_verb: str = "Backflushed",
    movement_label: str = "Backflush",
    movement_suffix: str = "",
    extra_data: Optional[dict] = None,
) -> Optional[InventoryTransaction]:
    """Write one negative ISSUE txn (carrying the consumed lot), decrement, + audit.

    THE single construct -> savepoint -> decrement -> dual-audit implementation for every
    negative work-order material movement. The per-run consumption engine used to carry a
    near-verbatim copy of this (``_post_consumption_txn``) differing only in reference
    shape, notes and description -- while its module docstring claimed it "REUSES its
    helpers rather than reimplementing them". The variable parts are now parameters:

    * ``reference_type`` / ``reference_id`` -- ``('work_order', work_order.id)`` for the
      FG backflush and work-order-scoped ties, ``('work_order_operation', operation.id)``
      for per-run consumption (outside the ``uq_wo_inventory_*`` predicates by design);
    * ``notes`` -- the ledger row's own note (defaults to the backflush wording);
    * ``movement_verb`` / ``movement_label`` / ``movement_suffix`` -- the audit prose;
    * ``extra_data`` -- extra audit context (the tie + operation ids on the per-run leg).

    Order matters (item 1): the ISSUE txn is inserted FIRST under a savepoint. The
    source on-hand is decremented ONLY when the insert actually committed; a duplicate
    (the double-issue race the unique index catches) rolls back just the savepoint and
    is a clean no-op -- no decrement, no audit -- so it never double-consumes the
    component or aborts the outer completion / reconcile transaction.

    ``quantity_available`` is recomputed HERE, in the same block as the
    ``quantity_on_hand`` mutation; skipping it silently desyncs the denormalized column
    that the receipt-void guard and MRP read.

    Returns the inserted ``InventoryTransaction``, or ``None`` on a duplicate no-op.
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
    if not _insert_txn_with_savepoint(db, txn):
        return None

    # Insert committed to the savepoint -> NOW decrement the source stock.
    old_on_hand = inventory_item.quantity_on_hand
    inventory_item.quantity_on_hand = float(inventory_item.quantity_on_hand or 0) - quantity
    inventory_item.quantity_available = inventory_item.quantity_on_hand - float(inventory_item.quantity_allocated or 0)
    db.flush()

    audit.log_create(
        "inventory",
        txn.id,
        str(txn.id),
        new_values=txn,
        description=(
            f"{movement_verb} {quantity} of part {part_number or component_part_id} "
            f"for work order {work_order.work_order_number}"
            + movement_suffix
            + (f" lot {lot_number}" if lot_number else "")
        ),
        extra_data=extra_data,
    )
    if old_on_hand is not None:
        audit.log_update(
            "inventory",
            inventory_item.id,
            f"{part_number or component_part_id} @ {inventory_item.location}",
            old_values={"quantity_on_hand": old_on_hand},
            new_values={"quantity_on_hand": inventory_item.quantity_on_hand},
            description=f"{movement_label}: stock for part {part_number or component_part_id}",
        )
    return txn


def backflush_components_for_work_order(
    db: Session,
    work_order: WorkOrder,
    *,
    user_id: int,
    company_id: int,
    audit: AuditService,
    allocations: Optional[list[WorkOrderMaterialAllocation]] = None,
) -> BackflushResult:
    """Consume a completed WO's BOM components from inventory (INV-2).

    GATED: the BOM/routing leg only runs when ``work_order.part.backflush_components``
    is True (opt-in per part, default False) so material a shop issued manually is never
    double-consumed. Idempotent per component (skips a component that already has a
    WO ISSUE txn). Each consumed source lot is carried on the ISSUE txn for as-built
    genealogy. A shortage NEVER fails the completion, but is recorded tamper-evidently
    (a ``BACKFLUSH_SHORTAGE`` ``audit_log`` row + a ``backflush_shortage``
    ``OperationalEvent``) inside ``_issue_one_component`` -- so it is captured on BOTH
    the live paths AND the reconcile path (the caller no longer needs to inspect the
    returned shortages to record them). Does NOT commit.

    Also drains WORK-ORDER-scoped material ties (``work_order_material_allocations``
    with no operation). Those are NOT gated on ``backflush_components`` -- an explicit
    tie is itself the opt-in -- and their demand is SUMMED with any BOM demand for the
    same part so exactly ONE ISSUE row is emitted per (WO, part), as
    ``uq_wo_inventory_issue`` requires. OPERATION-scoped ties are handled by
    ``material_consumption_service`` and are excluded from this leg entirely. A
    work-order-scoped tie's LOT PIN is honored -- the ISSUE draws from the pinned lot
    exclusively, and a pinned lot held after pinning writes ``HELD_MATERIAL_CONSUMED``.
    """
    result = BackflushResult()

    part = work_order.part
    if part is None and work_order.part_id is not None:
        part = db.query(Part).filter(Part.id == work_order.part_id, Part.company_id == company_id).first()

    allocation_demand = _work_order_scoped_allocation_demand(db, work_order, company_id, allocations)
    backflush_enabled = part is not None and bool(getattr(part, "backflush_components", False))
    if not backflush_enabled and not allocation_demand:
        # Untied + not opted in -> exactly the pre-feature no-op.
        return result

    required_by_component: dict[int, float] = (
        _resolve_backflush_components(db, work_order, company_id, allocations) if backflush_enabled else {}
    )
    for part_id, demand in allocation_demand.items():
        required_by_component[part_id] = required_by_component.get(part_id, 0.0) + demand.quantity

    if not required_by_component:
        return result

    for component_part_id, required_qty in required_by_component.items():
        if required_qty <= _EPSILON:
            continue
        if _component_already_issued(db, work_order.id, component_part_id, company_id):
            # Idempotency: this component was already backflushed for this WO.
            continue
        demand = allocation_demand.get(component_part_id)
        outcome = _issue_one_component(
            db,
            work_order,
            component_part_id=component_part_id,
            required_qty=required_qty,
            company_id=company_id,
            user_id=user_id,
            audit=audit,
            allocation_id=demand.allocation_ids[0] if demand else None,
            pinned_inventory_item_id=demand.pinned_inventory_item_id if demand else None,
        )
        result.issued_part_ids.append(component_part_id)
        if demand is not None and outcome.posted:
            # ONLY when an ISSUE row actually landed. On a duplicate no-op nothing was
            # inserted and nothing was decremented, so the winning transaction's rows own
            # the quantity -- advancing qty_consumed here would corrupt the cache the
            # untie 409 keys on, claiming consumption this call never posted.
            _mark_work_order_ties_consumed(db, work_order, demand, company_id=company_id, audit=audit)
        if outcome.shortage is not None:
            result.shortages.append(outcome.shortage)

    return result


def _mark_work_order_ties_consumed(
    db: Session,
    work_order: WorkOrder,
    demand: WorkOrderMaterialAllocationDemand,
    *,
    company_id: int,
    audit: AuditService,
) -> None:
    """Advance ``qty_consumed`` to ``qty_planned`` on the ties a WO-level ISSUE drained.

    The ledger is authoritative (see the model docstring); this keeps the denormalized
    cache honest so the untie guard ("409 if qty_consumed > 0") and the UI agree with
    the movement that just posted. Tenant-scoped; only flushes.

    AUDITED (invariant #2), mirroring the operation-scoped twin in
    ``material_consumption_service._consume_one_allocation``. This is a state change on a
    TenantMixin row, and it writes the exact field a later verb keys on: once
    ``qty_consumed > 0`` the untie endpoint refuses with 409. An unaudited write here
    would change what the system will later refuse, with nothing on the hash chain
    saying who or what changed it. ``log_update`` self-suppresses when the value did not
    actually move, so a replay adds no row.

    ONLY called when the ISSUE actually posted (``IssueOutcome.posted``) -- see the call
    site. A duplicate no-op inserted nothing, so there is no consumption to cache.
    """
    rows = (
        db.query(WorkOrderMaterialAllocation)
        .filter(
            WorkOrderMaterialAllocation.company_id == company_id,
            WorkOrderMaterialAllocation.id.in_(demand.allocation_ids),
        )
        .all()
    )
    if not rows:
        return

    part_numbers = {
        row_id: number
        for row_id, number in db.query(Part.id, Part.part_number)
        .filter(Part.company_id == company_id, Part.id.in_({row.part_id for row in rows}))
        .all()
    }
    updates: list[tuple[WorkOrderMaterialAllocation, float]] = []
    for row in rows:
        old_consumed = float(row.qty_consumed or 0)
        row.qty_consumed = float(row.qty_planned or 0)
        updates.append((row, old_consumed))
    db.flush()

    for row, old_consumed in updates:
        part_number = part_numbers.get(row.part_id)
        audit.log_update(
            "work_order_material_allocation",
            row.id,
            f"WO {work_order.work_order_number} / part {part_number or row.part_id}",
            old_values={"qty_consumed": old_consumed},
            new_values={"qty_consumed": row.qty_consumed},
            description=(
                f"Consumed {row.qty_consumed} {row.unit_of_measure} of part "
                f"{part_number or row.part_id} against work order {work_order.work_order_number} "
                "(work-order-scoped tie, drained by the completion backflush)"
            ),
            extra_data={
                "work_order_id": work_order.id,
                "reference_type": "work_order",
                "part_id": row.part_id,
                "allocation_id": row.id,
            },
        )


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
    the backflush-precedence drop, and the work-order-scoped demand), so a completion
    cost 2 unconditional reads of ``work_order_material_allocations`` plus a third when
    the part opted into backflush. It is now exactly ONE, tied or not.
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
