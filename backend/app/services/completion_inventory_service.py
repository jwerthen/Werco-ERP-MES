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

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.inventory import InventoryItem, InventoryTransaction, TransactionType
from app.models.operational_event import OperationalEvent  # noqa: F401  (imported for type/test discoverability)
from app.models.part import Part
from app.models.work_order import WorkOrder, WorkOrderOperation
from app.services.audit_service import AuditService
from app.services.operational_event_service import OperationalEventService

logger = logging.getLogger(__name__)

# Tamper-evident audit action + operational-event type for a backflush shortage
# (a component driven negative on-hand). A silent negative stock is a material-trail
# control gap in a regulated (AS9100D/CMMC-L2) system, so a shortage is recorded on
# the hash chain AND emitted as a warning OperationalEvent (item 3).
BACKFLUSH_SHORTAGE_AUDIT_ACTION = "BACKFLUSH_SHORTAGE"
BACKFLUSH_SHORTAGE_EVENT_TYPE = "backflush_shortage"


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


def _resolve_backflush_components(
    db: Session,
    work_order: WorkOrder,
    company_id: int,
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

    if required:
        return required

    # 2) Fall back to exploding the finished part's BOM (scrap_factor applied by the
    #    helper). Imported lazily to avoid an import cycle with the endpoints module.
    from app.api.endpoints.work_orders import _collect_bom_components, _get_active_bom

    bom = _get_active_bom(db, work_order.part_id, company_id)
    if not bom:
        return {}
    for _item, component, extended_qty in _collect_bom_components(db, bom, company_id, parent_qty=produced):
        required[component.id] = required.get(component.id, 0.0) + float(extended_qty or 0)
    return required


def _issue_one_component(
    db: Session,
    work_order: WorkOrder,
    *,
    component_part_id: int,
    required_qty: float,
    company_id: int,
    user_id: int,
    audit: AuditService,
) -> Optional[ComponentShortage]:
    """Backflush a single component: write ONE ISSUE txn + decrement source stock.

    The work-order ISSUE unique index keys on ``(company, WO, ISSUE, part_id)``, so a
    component is consumed by EXACTLY ONE ISSUE per WO. We therefore write a single
    negative ISSUE for the FULL ``required_qty`` against the primary source lot (lowest
    id with on-hand, else a placeholder created at the component's standard cost),
    carrying that lot on the txn for genealogy. If total on-hand is insufficient, the
    primary lot is driven NEGATIVE (consumption + true demand still RECORDED, matching
    the permissive ``/inventory/adjust`` behavior) and a ``ComponentShortage`` is
    returned (never raised) -- additionally recorded tamper-evidently (item 3). The
    ISSUE INSERT is wrapped in a SAVEPOINT (item 1): a concurrent duplicate (the
    double-issue race the index catches) rolls back only the savepoint and is a clean
    no-op (no decrement, no shortage record). Returns ``None`` when fully satisfied
    from stock, ``ComponentShortage`` on a shortfall, ``None`` on a duplicate no-op.
    """
    part = db.query(Part).filter(Part.id == component_part_id, Part.company_id == company_id).first()
    part_number = part.part_number if part else None

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

    # Primary consumed lot: the lowest-id on-hand row, or a placeholder row when none
    # exists (so the negative consumption is still recorded against a real item).
    unit_cost = float(part.standard_cost or 0) if part else 0.0
    target = source_items[0] if source_items else None
    if target is None:
        target = InventoryItem(
            part_id=component_part_id,
            location=FINISHED_GOODS_LOCATION,
            warehouse=FINISHED_GOODS_WAREHOUSE,
            quantity_on_hand=0.0,
            quantity_allocated=0.0,
            quantity_available=0.0,
            unit_cost=unit_cost,
            status="available",
        )
        target.company_id = company_id
        db.add(target)
        db.flush()

    # ONE ISSUE for the full required quantity, inserted FIRST under a savepoint; the
    # decrement applies ONLY when the insert actually committed. A duplicate (a
    # concurrent completion already issued this component) is a clean no-op -- no
    # decrement, no shortage record -- so it can never double-consume or abort the
    # outer completion / reconcile transaction.
    if not _write_issue_txn(
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
    ):
        return None

    shortage: Optional[ComponentShortage] = None
    shortfall = required_qty - available_total
    if shortfall > 1e-9:
        shortage = ComponentShortage(
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
            shortage=shortage,
            consumed_lot=target.lot_number,
            company_id=company_id,
            user_id=user_id,
            audit=audit,
        )

    return shortage


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
) -> bool:
    """Write one negative ISSUE txn (carrying the consumed lot), decrement, + audit.

    Order matters (item 1): the ISSUE txn is inserted FIRST under a savepoint. The
    source on-hand is decremented ONLY when the insert actually committed; a duplicate
    (the double-issue race the unique index catches) rolls back just the savepoint and
    is a clean no-op -- no decrement, no audit -- so it never double-consumes the
    component or aborts the outer completion / reconcile transaction.

    Returns ``True`` on a real insert (caller may treat the demand as consumed),
    ``False`` on a duplicate no-op.
    """
    txn = InventoryTransaction(
        company_id=company_id,
        inventory_item_id=inventory_item.id,
        part_id=component_part_id,
        transaction_type=TransactionType.ISSUE,
        quantity=-quantity,
        from_location=inventory_item.location,
        lot_number=lot_number,
        reference_type="work_order",
        reference_id=work_order.id,
        reference_number=work_order.work_order_number,
        unit_cost=unit_cost,
        total_cost=quantity * unit_cost,
        notes=f"Backflush consumption for work order {work_order.work_order_number}",
        created_by=user_id,
    )
    if not _insert_txn_with_savepoint(db, txn):
        return False

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
            f"Backflushed {quantity} of part {part_number or component_part_id} "
            f"for work order {work_order.work_order_number}" + (f" lot {lot_number}" if lot_number else "")
        ),
    )
    if old_on_hand is not None:
        audit.log_update(
            "inventory",
            inventory_item.id,
            f"{part_number or component_part_id} @ {inventory_item.location}",
            old_values={"quantity_on_hand": old_on_hand},
            new_values={"quantity_on_hand": inventory_item.quantity_on_hand},
            description=f"Backflush: stock for part {part_number or component_part_id}",
        )
    return True


def backflush_components_for_work_order(
    db: Session,
    work_order: WorkOrder,
    *,
    user_id: int,
    company_id: int,
    audit: AuditService,
) -> BackflushResult:
    """Consume a completed WO's BOM components from inventory (INV-2).

    GATED: only runs when ``work_order.part.backflush_components`` is True (opt-in
    per part, default False) so material a shop issued manually is never
    double-consumed. Idempotent per component (skips a component that already has a
    WO ISSUE txn). Each consumed source lot is carried on the ISSUE txn for as-built
    genealogy. A shortage NEVER fails the completion, but is recorded tamper-evidently
    (a ``BACKFLUSH_SHORTAGE`` ``audit_log`` row + a ``backflush_shortage``
    ``OperationalEvent``) inside ``_issue_one_component`` -- so it is captured on BOTH
    the live paths AND the reconcile path (the caller no longer needs to inspect the
    returned shortages to record them). Does NOT commit.
    """
    result = BackflushResult()

    part = work_order.part
    if part is None:
        part = db.query(Part).filter(Part.id == work_order.part_id, Part.company_id == company_id).first()
    if part is None or not getattr(part, "backflush_components", False):
        return result

    required_by_component = _resolve_backflush_components(db, work_order, company_id)
    if not required_by_component:
        return result

    for component_part_id, required_qty in required_by_component.items():
        if required_qty <= 0:
            continue
        if _component_already_issued(db, work_order.id, component_part_id, company_id):
            # Idempotency: this component was already backflushed for this WO.
            continue
        shortage = _issue_one_component(
            db,
            work_order,
            component_part_id=component_part_id,
            required_qty=required_qty,
            company_id=company_id,
            user_id=user_id,
            audit=audit,
        )
        result.issued_part_ids.append(component_part_id)
        if shortage is not None:
            result.shortages.append(shortage)

    return result


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
    """
    receive_finished_goods_for_work_order(db, work_order, user_id=user_id, company_id=company_id, audit=audit)
    return backflush_components_for_work_order(db, work_order, user_id=user_id, company_id=company_id, audit=audit)
