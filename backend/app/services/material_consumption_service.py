"""Material consumption — depleting stock tied to a work order as work completes.

The headline case: a laser nest operation tied to a sheet part consumes
``qty_per_run`` sheets for every completed (or scrapped) run. The tie itself is the
``WorkOrderMaterialAllocation`` row (``models/work_order_material.py``); this module
is the engine that turns a tie into ledger movement.

It deliberately matches the contract of its sibling ``completion_inventory_service``
(read that module's docstring first) and REUSES its helpers rather than
reimplementing them -- ``_insert_txn_with_savepoint`` and ``_write_issue_txn`` above
all (the ledger write, the on-hand decrement and the dual audit rows are that shared
helper's, parameterized by reference shape and prose, not a copy of it):

* **No commit.** Every function joins the CALLER's unit of work; the completion
  handler owns ``db.commit()`` so consumption lands ATOMICALLY with the status
  change on the live paths.
* **Tenant-scoped.** Every read filters ``company_id`` (invariant #1); the caller
  passes the ACTIVE company.
* **Audited.** Every ``InventoryTransaction`` is logged through ``AuditService``
  (invariant #2) -- never a direct ``audit_log`` write.
* **Idempotent under REPLAY** (sequential re-entry). See below, including what that
  does and does not cover.

Sum-delta / reconcile-to-target
-------------------------------
For each OPEN operation-scoped allocation on the work order::

    target = qty_per_run * (op.quantity_complete + op.quantity_scrapped)
    delta  = target - allocation.qty_consumed
    delta > 0  -> post ISSUE for -delta, set qty_consumed = target
    delta <= 0 -> NO-OP

Because ``target`` is RECOMPUTED from live operation state on every call, a replay
converges instead of double-issuing: the second call sees ``delta == 0``. That is
why this path needs no ``uq_*`` index of its own (and why it deliberately posts with
``reference_type='work_order_operation'``, OUTSIDE the ``uq_wo_inventory_issue``
predicate, so it can never collide with the backflush idempotency guard).

**What carries concurrency is the WORK-ORDER LOCK, not this engine.** The convergence
argument above is a SEQUENTIAL one: it holds for a re-entry that observes the previous
call's committed ``qty_consumed``. It does not, on its own, make two *simultaneous*
completions of one operation safe -- these rows sit outside ``uq_wo_inventory_issue``
by design and ``WorkOrderMaterialAllocation`` carries no ``version`` column, so nothing
here would stop both racers from computing the same positive delta and both posting.
What actually serializes them is invariant #4: ``WorkOrder`` and ``WorkOrderOperation``
map ``version_id_col`` directly, so every call site that drives a completion takes the
optimistic lock and a stale concurrent writer raises ``StaleDataError`` (-> HTTP 409)
before it can reach here. Treat that lock as load-bearing for this engine: a future
call site that mutates neither row would step outside the protection, and the
per-allocation savepoint plus the "advance ``qty_consumed`` only when an insert actually
landed" rule are damage control, not a substitute.

**Scrap consumes.** A scrapped run physically used the sheet, so scrap is inside
``target``. It is posted as ``TransactionType.ISSUE``, NOT ``SCRAP``: lot genealogy
(``api/endpoints/traceability.py``) reconstructs consumed components by filtering
``transaction_type == ISSUE``, so a ``SCRAP`` row would make audited scrap material
VANISH from the as-built record -- an AS9100D traceability hole. The good/scrap
split is recorded in the transaction ``notes`` instead.

**Never auto-reverse.** A negative delta is a no-op, never a RETURN. The supervisor
reduce-over-count verb lowers ``quantity_complete`` after the sheet is already cut;
un-consuming it would be a lie. Reversal is an explicit, reasoned verb (a later PR).
This matters because consumption also runs from a GET (reconcile-on-read,
``shop_floor.py``) where there is no reason and the actor is whoever happens to be
reading -- exactly the context in which an automatic inventory reversal would be
unattributable.

Read-safety
-----------
The reconcile-on-read callers wrap their inventory effects in a bare
``except: pass``. Swallowing an exception AFTER a failed flush would leave the
session poisoned and the caller's commit would raise ``PendingRollbackError``, so
every allocation's work runs inside its OWN ``begin_nested()`` savepoint: a failure
rolls back just that allocation and leaves the outer transaction usable. The
top-level entry point additionally never propagates. Degrade and record, never
break a GET.

Shortage posture
----------------
A shortage NEVER fails the completion. Production truth outranks inventory
bookkeeping -- refusing would train operators to untie material. Mirroring
``_record_backflush_shortage``: drive the source lot negative, write a tamper-evident
``ALLOCATION_SHORTAGE`` audit row, and emit a warning ``OperationalEvent``
(``material_allocation_shortage`` -> notification catalog ``material.allocation_shortage``).

The same warn-and-record posture covers the other two things that can go wrong here,
because BOTH would otherwise be silent:

* **Held material consumed** (``HELD_MATERIAL_CONSUMED``). ``_fifo_source_items``
  excludes lots that are inactive or not ``available``, but a PINNED lot bypasses FIFO
  entirely, and a lot can be quarantined/held AFTER it was pinned. Consumption still
  proceeds -- the sheet was physically cut, and refusing from a GET would be
  unattributable -- but consuming nonconforming/held material into an as-built record
  is an AS9100D 8.7 event, so it goes on the hash chain. The tie-time control is the
  other half: ``work_order_materials.py`` REFUSES a pin of a held/inactive lot (422),
  so this row can only ever mean "held after it was pinned". ``record_held_material_consumed``
  is exported because the WORK-ORDER-scoped leg in ``completion_inventory_service``
  honors pins too and owes the identical record.

  **Disclosure -- nothing in ``app/`` ever writes a held ``InventoryItem.status``.**
  No endpoint, schema or service in this codebase sets ``on_hold`` / ``quarantine`` /
  ``rejected`` on a stock row (``status`` is written only as ``"available"``, on
  creation), and there is no deactivation verb for a lot. So both halves of this control
  -- the 422 pin refusal and this audit row -- can only fire on data set OUTSIDE the
  application (a direct DB write, an import, or a future hold verb). They are built and
  tested ahead of the feature that will produce that state; do not read them as evidence
  that lot-hold state is live in-system today.
* **Consumption failed** (``ALLOCATION_CONSUMPTION_FAILED``). A per-allocation
  savepoint rollback used to leave nothing but a log line, so material that should
  have depleted and did not was invisible -- weaker treatment than the LESSER shortage
  condition gets. ``AuditService.log`` inserts under its OWN savepoint and never
  propagates, so it is safe to call on the outer transaction after that rollback.
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy.orm import Session

from app.db.ledger_filter import OPERATION_REFERENCE_TYPE
from app.models.audit_log import AuditLog
from app.models.inventory import InventoryItem, InventoryTransaction
from app.models.part import Part
from app.models.work_order import WorkOrder, WorkOrderOperation
from app.models.work_order_material import AllocationStatus, WorkOrderMaterialAllocation
from app.services.audit_service import AuditService
from app.services.completion_inventory_service import (
    _EPSILON,
    _component_already_issued,
    _placeholder_stock_row,
    _write_issue_txn,
)
from app.services.operational_event_service import OperationalEventService

logger = logging.getLogger(__name__)

# ``OPERATION_REFERENCE_TYPE`` / ``WORK_ORDER_REFERENCE_TYPE`` / ``work_order_ledger_filter``
# live in ``app.db.ledger_filter`` -- the predicate is a GENERIC ledger question ("which
# rows belong to this work order?") that job costing, analytics, genealogy and the ledger
# list endpoint all ask, and homing it here forced every one of them to import this whole
# engine (transitively ``completion_inventory_service`` + ``operational_event_service``)
# just to get a WHERE clause. Import them from there, NOT through this module: a re-export
# here would rebuild exactly the coupling the move removed.

# Tamper-evident audit action + operational-event type for a consumption that drove a
# source lot negative. Distinct from ``BACKFLUSH_SHORTAGE`` so the two mechanisms stay
# separable in the audit trail and in the notification catalog.
ALLOCATION_SHORTAGE_AUDIT_ACTION = "ALLOCATION_SHORTAGE"
ALLOCATION_SHORTAGE_EVENT_TYPE = "material_allocation_shortage"

# Tamper-evident audit action for consuming a PINNED lot that is on hold / quarantined /
# rejected / inactive. See the module docstring: the pin is refused at tie time, so this
# only fires when the lot was held AFTER it was pinned. Consumption still proceeds.
HELD_MATERIAL_CONSUMED_AUDIT_ACTION = "HELD_MATERIAL_CONSUMED"

# Tamper-evident audit action for an allocation whose consumption raised and was rolled
# back to its savepoint. Recorded so a FAILED depletion is at least as visible as a
# shortage (which already writes a chain row).
ALLOCATION_CONSUMPTION_FAILED_AUDIT_ACTION = "ALLOCATION_CONSUMPTION_FAILED"

# The only ``InventoryItem.status`` a consumption may draw from without comment. Anything
# else (``on_hold`` / ``quarantine`` / ``rejected`` -- see ``models/inventory.py``) is
# nonconforming or held material under AS9100D 8.7.
AVAILABLE_ITEM_STATUS = "available"

# Public alias of the shared float-comparison epsilon (defined once in
# ``completion_inventory_service``). Exported so callers OUTSIDE the services layer --
# the tie endpoint's untie guard above all -- test "has anything been consumed?" exactly
# the way the engine does. These are ``Float`` columns; a bare ``> 0`` at the API
# boundary and an ``> _EPSILON`` inside the engine can disagree about the same row.
CONSUMPTION_EPSILON = _EPSILON

# The audit ``extra_data.reason`` a work-order SOFT DELETE stamps on the ties it
# auto-cancels. It is the ONLY marker distinguishing "cancelled because the work order
# was deleted" (which restore must undo) from every other cancellation -- a manual untie,
# or a nest re-import superseding the operation -- which restore must NOT resurrect.
WORK_ORDER_DELETED_CANCEL_REASON = "work_order_deleted"

# The same, for the nest-re-import wipe.
REIMPORT_CANCEL_REASON = "superseded_by_reimport"


@dataclass
class AllocationShortage:
    """A consumption that drove (or would have driven) a source lot negative.

    Recorded but NOT fatal -- see the module docstring's shortage posture.
    """

    allocation_id: int
    part_id: int
    part_number: Optional[str]
    required_quantity: float
    available_quantity: float
    shortfall: float


@dataclass
class MaterialConsumptionResult:
    """What one ``consume_tied_materials_for_work_order`` call actually did."""

    consumed_allocation_ids: list[int] = field(default_factory=list)
    transactions: list[InventoryTransaction] = field(default_factory=list)
    shortages: list[AllocationShortage] = field(default_factory=list)
    failed_allocation_ids: list[int] = field(default_factory=list)


def open_allocations_for_work_order(
    db: Session,
    work_order_id: int,
    company_id: int,
) -> list[WorkOrderMaterialAllocation]:
    """Every OPEN allocation on a work order, tenant-scoped (invariant #1).

    Index-backed by ``ix_wo_material_alloc_company_wo``. An UNTIED work order returns
    ``[]`` here and every caller then short-circuits with ZERO writes -- that is what
    keeps untied work orders byte-identical to their pre-feature behavior.

    Called ONCE per completion, by ``apply_completion_inventory_effects``, which threads
    the result into the consume engine, the backflush-precedence drop and the
    work-order-scoped demand. Call it directly only outside that seam.
    """
    return (
        db.query(WorkOrderMaterialAllocation)
        .filter(
            WorkOrderMaterialAllocation.company_id == company_id,
            WorkOrderMaterialAllocation.work_order_id == work_order_id,
            WorkOrderMaterialAllocation.status == AllocationStatus.OPEN,
        )
        .order_by(WorkOrderMaterialAllocation.id)
        .all()
    )


def is_consumable_item(item: InventoryItem) -> bool:
    """True when a lot may be consumed into product without a compliance comment.

    Mirrors what ``_fifo_source_items`` filters on, exposed so the tie endpoint can
    refuse a PIN of a held lot up front (a 422 at tie time, where a human is present to
    answer) instead of leaving the divergence to be discovered at consume time -- which
    runs from a GET, where refusing is not an option.

    A NULL ``status`` reads as ``available`` (the column's own default), so a legacy row
    written outside the ORM is not treated as held. The FIFO SQL is stricter -- a NULL
    simply never matches ``status = 'available'`` -- which is the safe direction: such a
    lot is skipped by FIFO rather than silently consumed.
    """
    return bool(item.is_active) and (item.status or AVAILABLE_ITEM_STATUS) == AVAILABLE_ITEM_STATUS


def _fifo_source_items(db: Session, part_id: int, company_id: int) -> list[InventoryItem]:
    """Active, available, on-hand stock for a part in FIFO order (tenant-scoped).

    ``received_date ASC NULLS LAST, id ASC``: oldest receipt first, rows with no
    received date last (they cannot be ordered by age, so they are the fallback), and
    ``id`` as the deterministic tie-break.
    """
    return (
        db.query(InventoryItem)
        .filter(
            InventoryItem.company_id == company_id,
            InventoryItem.part_id == part_id,
            InventoryItem.is_active == True,  # noqa: E712
            InventoryItem.status == AVAILABLE_ITEM_STATUS,
            InventoryItem.quantity_on_hand > 0,
        )
        .order_by(
            InventoryItem.received_date.is_(None),
            InventoryItem.received_date.asc(),
            InventoryItem.id.asc(),
        )
        .all()
    )


def _post_consumption_txn(
    db: Session,
    *,
    work_order: WorkOrder,
    operation: WorkOrderOperation,
    allocation: WorkOrderMaterialAllocation,
    inventory_item: InventoryItem,
    quantity: float,
    company_id: int,
    user_id: int,
    audit: AuditService,
    part_number: Optional[str],
    notes: str,
) -> Optional[InventoryTransaction]:
    """Write ONE negative ISSUE against a source lot, decrement it, and audit.

    A THIN adapter over the shared ``_write_issue_txn`` -- it supplies only what differs
    on this leg (the operation reference shape, the run-scaled note, the audit prose and
    the tie/operation ids in ``extra_data``). The construct -> savepoint -> decrement ->
    dual-audit sequence itself, including recomputing ``quantity_available``, belongs to
    the shared helper; this used to be a near-verbatim second copy of it.

    Returns the inserted transaction, or ``None`` on a duplicate no-op.
    """
    return _write_issue_txn(
        db,
        work_order,
        inventory_item=inventory_item,
        component_part_id=allocation.part_id,
        quantity=quantity,
        unit_cost=float(inventory_item.unit_cost or 0),
        lot_number=inventory_item.lot_number,
        company_id=company_id,
        user_id=user_id,
        audit=audit,
        part_number=part_number,
        allocation_id=allocation.id,
        reference_type=OPERATION_REFERENCE_TYPE,
        reference_id=operation.id,
        notes=notes,
        movement_verb="Consumed",
        movement_label="Material consumption",
        movement_suffix=f" operation {operation.operation_number or operation.id}",
        extra_data={"allocation_id": allocation.id, "work_order_operation_id": operation.id},
    )


def _record_allocation_shortage(
    db: Session,
    *,
    work_order: WorkOrder,
    operation: WorkOrderOperation,
    shortage: AllocationShortage,
    consumed_lot: Optional[str],
    company_id: int,
    user_id: int,
    audit: AuditService,
) -> None:
    """Persist a consumption shortage as a tamper-evident audit row + warning event.

    Mirrors ``_record_backflush_shortage``: ONE ``audit_log`` row (action
    ``ALLOCATION_SHORTAGE``) on the immutable hash chain -- never a direct table write --
    plus a ``material_allocation_shortage`` ``OperationalEvent`` (severity ``warning``),
    which the notification catalog maps to ``material.allocation_shortage``. Both only
    flush, so they land atomically with the completion; the emit is best-effort so a
    signal failure can never fail an in-flight completion (the audit row is the
    compliance record).
    """
    extra = {
        "work_order_id": work_order.id,
        "work_order_number": work_order.work_order_number,
        "work_order_operation_id": operation.id,
        "operation_number": operation.operation_number,
        "allocation_id": shortage.allocation_id,
        "part_id": shortage.part_id,
        "part_number": shortage.part_number,
        "required_quantity": shortage.required_quantity,
        "available_quantity": shortage.available_quantity,
        "shortfall": shortage.shortfall,
        "consumed_lot": consumed_lot,
    }
    audit.log(
        action=ALLOCATION_SHORTAGE_AUDIT_ACTION,
        resource_type="inventory",
        resource_id=shortage.part_id,
        resource_identifier=shortage.part_number or str(shortage.part_id),
        description=(
            f"Material shortage on WO {work_order.work_order_number} operation "
            f"{operation.operation_number or operation.id}: part "
            f"{shortage.part_number or shortage.part_id} short {shortage.shortfall} "
            f"(required {shortage.required_quantity}, available {shortage.available_quantity})"
            + (f", lot {consumed_lot}" if consumed_lot else "")
        ),
        new_values={"shortfall": shortage.shortfall},
        extra_data=extra,
        company_id=company_id,
    )
    OperationalEventService(db).emit_best_effort(
        company_id=company_id,
        event_type=ALLOCATION_SHORTAGE_EVENT_TYPE,
        source_module="material_consumption",
        entity_type="inventory",
        entity_id=shortage.part_id,
        work_order_id=work_order.id,
        operation_id=operation.id,
        user_id=user_id,
        severity="warning",
        event_payload=extra,
    )


def record_held_material_consumed(
    *,
    work_order: WorkOrder,
    operation: Optional[WorkOrderOperation],
    allocation_id: Optional[int],
    part_id: Optional[int],
    item: InventoryItem,
    part_number: Optional[str],
    quantity: float,
    company_id: int,
    audit: AuditService,
    pinned: bool = True,
) -> None:
    """Record that a held/inactive lot was consumed anyway (AS9100D 8.7).

    Consumption is NOT refused: this path also runs from a reconcile-on-read GET, the
    material is already physically in the part, and refusing would leave the ledger
    lying about what was burned. What is NOT acceptable is doing it silently -- so the
    fact goes on the tamper-evident hash chain, naming the lot, its status and the tie,
    for the MRB/segregation review that has to follow.

    PUBLIC (no leading underscore) and ``operation``-optional because THREE call sites
    owe this row: the per-run engine below, and BOTH lot-selection branches of the
    one-shot work-order-scoped leg in ``completion_inventory_service._issue_one_component``.
    A work-order-scoped tie has no operation, so the row simply omits it.

    ``pinned`` says which branch selected the lot, because the two mean different things
    to whoever reviews the row:

    * ``True`` (both PINNED branches) -- only reachable when the lot was held AFTER it
      was pinned, since the tie endpoint refuses to pin a non-``available`` or inactive
      lot in the first place.
    * ``False`` (the UNPINNED work-order-scoped branch) -- that branch selects the
      lowest-id active on-hand lot with no ``status`` predicate at all, so it can pick a
      lot that was ALREADY held. Its selection is deliberately left alone (tightening it
      would change the pre-existing BOM backflush by excluding legacy NULL-status rows);
      this row is what stops the consumption being silent.
    """
    scope = f" operation {operation.operation_number or operation.id}" if operation is not None else ""
    audit.log(
        action=HELD_MATERIAL_CONSUMED_AUDIT_ACTION,
        resource_type="inventory",
        resource_id=item.id,
        resource_identifier=item.lot_number or str(item.id),
        description=(
            f"Consumed {quantity} of part {part_number or part_id} from "
            f"{'PINNED lot' if pinned else 'lot'} "
            f"{item.lot_number or item.id} on WO {work_order.work_order_number}{scope} "
            f"while that lot was {'inactive' if not item.is_active else item.status}"
        ),
        new_values={"status": item.status, "is_active": bool(item.is_active)},
        extra_data={
            "work_order_id": work_order.id,
            "work_order_number": work_order.work_order_number,
            "work_order_operation_id": operation.id if operation is not None else None,
            "allocation_id": allocation_id,
            "part_id": part_id,
            "part_number": part_number,
            "inventory_item_id": item.id,
            "lot_number": item.lot_number,
            "item_status": item.status,
            "item_is_active": bool(item.is_active),
            "quantity": quantity,
            "pin_directed": pinned,
        },
        company_id=company_id,
    )
    logger.warning(
        "Held material consumed: WO %s op %s allocation %s %s lot %s (status=%s active=%s, company %s)",
        work_order.id,
        operation.id if operation is not None else None,
        allocation_id,
        "pinned" if pinned else "unpinned",
        item.lot_number or item.id,
        item.status,
        item.is_active,
        company_id,
    )


def _consume_one_allocation(
    db: Session,
    *,
    work_order: WorkOrder,
    allocation: WorkOrderMaterialAllocation,
    operation: WorkOrderOperation,
    company_id: int,
    user_id: int,
    audit: AuditService,
    result: MaterialConsumptionResult,
) -> None:
    """Reconcile ONE operation-scoped allocation to its target (the sum-delta rule).

    Lot selection: a PINNED allocation consumes from that lot only -- pinning is a
    lot-directed instruction, so an insufficient pinned lot is driven negative rather
    than silently spilling onto a different (uncertified, wrong-heat) lot. An UNPINNED
    allocation walks FIFO stock, spilling across lots when the head lot cannot cover
    the delta.

    The pin bypasses FIFO's ordering, NOT its hold check. A pinned lot that is inactive
    or not ``available`` (on hold / quarantined / rejected) is still consumed -- see
    ``record_held_material_consumed`` for why refusing here would be worse -- but the
    fact is written to the tamper-evident chain instead of passing silently.
    """
    good = float(operation.quantity_complete or 0)
    scrapped = float(operation.quantity_scrapped or 0)
    # COALESCE(qty_per_run, 1.0) -- a NULL on an operation-scoped row means
    # "not run-scaled" per the model docstring.
    per_run = float(allocation.qty_per_run if allocation.qty_per_run is not None else 1.0)
    target = per_run * (good + scrapped)
    delta = target - float(allocation.qty_consumed or 0)
    if delta <= _EPSILON:
        # Includes the reduce-over-count case (target fell below what was consumed):
        # a NO-OP, never an auto-reversal. See the module docstring.
        return

    part = db.query(Part).filter(Part.id == allocation.part_id, Part.company_id == company_id).first()
    part_number = part.part_number if part else None
    notes = (
        f"Material consumption for work order {work_order.work_order_number} operation "
        f"{operation.operation_number or operation.id}: {good} complete + {scrapped} scrapped runs "
        f"@ {per_run} {allocation.unit_of_measure}/run"
    )

    held_item: Optional[InventoryItem] = None
    if allocation.pinned_inventory_item_id is not None:
        pinned = (
            db.query(InventoryItem)
            .filter(
                InventoryItem.id == allocation.pinned_inventory_item_id,
                InventoryItem.company_id == company_id,
            )
            .first()
        )
        source_items = [pinned] if pinned is not None else []
        if pinned is not None and not is_consumable_item(pinned):
            # Held AFTER it was pinned (the tie endpoint refuses to pin a held lot).
            # Consume anyway, but never silently -- recorded below, once the quantity
            # actually taken from this lot is known.
            held_item = pinned
    else:
        source_items = _fifo_source_items(db, allocation.part_id, company_id)

    available_total = sum(float(i.quantity_on_hand or 0) for i in source_items)

    remaining = delta
    posted_any = False
    last_item: Optional[InventoryItem] = None
    for item in source_items:
        if remaining <= _EPSILON:
            break
        take = min(remaining, float(item.quantity_on_hand or 0))
        if take <= _EPSILON:
            continue
        txn = _post_consumption_txn(
            db,
            work_order=work_order,
            operation=operation,
            allocation=allocation,
            inventory_item=item,
            quantity=take,
            company_id=company_id,
            user_id=user_id,
            audit=audit,
            part_number=part_number,
            notes=notes,
        )
        last_item = item
        if txn is None:
            # A duplicate no-op: nothing was inserted and nothing was decremented, so
            # this lot did NOT satisfy any of the demand. Leave ``remaining`` alone
            # (under-decrementing it here would silently under-consume) and move on --
            # the leftover falls through to the shortage leg, which still records the
            # full demand on the ledger.
            continue
        result.transactions.append(txn)
        posted_any = True
        remaining -= take

    if remaining > _EPSILON:
        # SHORTAGE: never fail the completion. Drive a lot negative so the true demand
        # is still on the ledger, then record it tamper-evidently + emit the warning.
        unit_cost = float(part.standard_cost or 0) if part else 0.0
        target_item = last_item or (source_items[0] if source_items else None)
        if target_item is None:
            target_item = _placeholder_stock_row(
                db, part_id=allocation.part_id, company_id=company_id, unit_cost=unit_cost
            )
        txn = _post_consumption_txn(
            db,
            work_order=work_order,
            operation=operation,
            allocation=allocation,
            inventory_item=target_item,
            quantity=remaining,
            company_id=company_id,
            user_id=user_id,
            audit=audit,
            part_number=part_number,
            notes=f"{notes} (SHORT {remaining})",
        )
        if txn is not None:
            result.transactions.append(txn)
            posted_any = True
        shortage = AllocationShortage(
            allocation_id=allocation.id,
            part_id=allocation.part_id,
            part_number=part_number,
            required_quantity=delta,
            available_quantity=available_total,
            shortfall=remaining,
        )
        result.shortages.append(shortage)
        logger.warning(
            "Material shortage on WO %s op %s allocation %s (company %s): required %s, available %s, short %s",
            work_order.id,
            operation.id,
            allocation.id,
            company_id,
            delta,
            available_total,
            remaining,
        )
        _record_allocation_shortage(
            db,
            work_order=work_order,
            operation=operation,
            shortage=shortage,
            consumed_lot=target_item.lot_number,
            company_id=company_id,
            user_id=user_id,
            audit=audit,
        )

    if not posted_any:
        # Every insert was a duplicate no-op (a concurrent completion already posted
        # this delta). Do NOT advance qty_consumed -- the winner's rows own it.
        return

    if held_item is not None:
        # A pin has exactly ONE source lot, so the whole delta came off the held lot
        # (whether through the normal take or by driving it negative). Recorded only
        # now, once we know a row actually posted.
        record_held_material_consumed(
            work_order=work_order,
            operation=operation,
            allocation_id=allocation.id,
            part_id=allocation.part_id,
            item=held_item,
            part_number=part_number,
            quantity=delta,
            company_id=company_id,
            audit=audit,
        )

    old_consumed = float(allocation.qty_consumed or 0)
    allocation.qty_consumed = target
    db.flush()
    audit.log_update(
        "work_order_material_allocation",
        allocation.id,
        f"WO {work_order.work_order_number} / part {part_number or allocation.part_id}",
        old_values={"qty_consumed": old_consumed},
        new_values={"qty_consumed": allocation.qty_consumed},
        description=(
            f"Consumed {delta} {allocation.unit_of_measure} of part "
            f"{part_number or allocation.part_id} against work order "
            f"{work_order.work_order_number} operation {operation.operation_number or operation.id}"
        ),
        extra_data={"work_order_id": work_order.id, "work_order_operation_id": operation.id},
    )
    result.consumed_allocation_ids.append(allocation.id)


def _record_consumption_failed(
    *,
    work_order: WorkOrder,
    allocation_id: int,
    part_id: Optional[int],
    operation_id: Optional[int],
    error: BaseException,
    company_id: int,
    audit: AuditService,
) -> None:
    """Record a rolled-back consumption on the tamper-evident chain.

    Material that SHOULD have depleted and did not is a material-trail control gap --
    strictly worse than the shortage case, which already writes a chain row. Without
    this the only trace was a log line the compliance record never sees.

    Safe on the post-``nested.rollback()`` outer transaction: ``AuditService.log`` opens
    its OWN savepoint around the INSERT and swallows every failure (returns ``None``),
    so it can neither propagate nor re-poison the session. Still wrapped defensively --
    a reconcile-on-read GET must never 500 because the failure record failed.
    """
    try:
        audit.log(
            action=ALLOCATION_CONSUMPTION_FAILED_AUDIT_ACTION,
            resource_type="work_order_material_allocation",
            resource_id=allocation_id,
            resource_identifier=f"WO {work_order.work_order_number} / allocation {allocation_id}",
            description=(
                f"Material consumption FAILED and was rolled back for allocation {allocation_id} on work "
                f"order {work_order.work_order_number}; tied material was NOT depleted"
            ),
            success=False,
            error_message=f"{type(error).__name__}: {error}"[:500],
            extra_data={
                "work_order_id": work_order.id,
                "work_order_number": work_order.work_order_number,
                "work_order_operation_id": operation_id,
                "allocation_id": allocation_id,
                "part_id": part_id,
            },
            company_id=company_id,
        )
    except Exception:  # pragma: no cover - the record must never break the caller
        logger.exception(
            "Failed to record ALLOCATION_CONSUMPTION_FAILED for allocation %s on WO %s (company %s)",
            allocation_id,
            work_order.id,
            company_id,
        )


def consume_tied_materials_for_work_order(
    db: Session,
    work_order: WorkOrder,
    *,
    user_id: int,
    company_id: int,
    audit: AuditService,
    allocations: Optional[list[WorkOrderMaterialAllocation]] = None,
) -> MaterialConsumptionResult:
    """Reconcile every OPEN operation-scoped allocation on a WO to its target.

    The single entry point wired into ``apply_completion_inventory_effects`` so it
    inherits every existing completion call site without adding one.

    UNTIED WORK ORDERS ARE UNTOUCHED: with no allocation rows this returns immediately --
    no inventory row, no ledger row, no audit row, no event. That equivalence is the
    single most important property of this feature and is locked by a test. The OPEN ties
    are read ONCE by ``apply_completion_inventory_effects`` and passed in as
    ``allocations``; omit the argument and this reads them itself.

    Does NOT commit (joins the caller's unit of work) and NEVER raises: each allocation
    runs inside its own SAVEPOINT so a failure rolls back only that allocation and
    leaves the outer transaction usable, and the ENTIRE body -- the operation lookup and
    the ``begin_nested()`` included, both of which sat outside the guard and could 500 a
    live completion -- runs under the wrapper below.
    """
    result = MaterialConsumptionResult()
    try:
        _consume_tied_materials(
            db,
            work_order,
            user_id=user_id,
            company_id=company_id,
            audit=audit,
            result=result,
            allocations=allocations,
        )
    except Exception:  # pragma: no cover - degrade, never break a completion or a GET
        logger.exception(
            "Material consumption aborted for WO %s (company %s)",
            work_order.id,
            company_id,
        )
    return result


def _consume_tied_materials(
    db: Session,
    work_order: WorkOrder,
    *,
    user_id: int,
    company_id: int,
    audit: AuditService,
    result: MaterialConsumptionResult,
    allocations: Optional[list[WorkOrderMaterialAllocation]] = None,
) -> None:
    """The body of ``consume_tied_materials_for_work_order``; may raise (wrapped there)."""
    if allocations is None:
        allocations = open_allocations_for_work_order(db, work_order.id, company_id)
    if not allocations:
        return

    operation_ids = [a.work_order_operation_id for a in allocations if a.work_order_operation_id is not None]
    if not operation_ids:
        # Work-order-scoped ties only; those merge into the one-shot backflush
        # (completion_inventory_service), not this per-run engine.
        return

    operations = {
        op.id: op
        for op in db.query(WorkOrderOperation)
        .filter(
            WorkOrderOperation.company_id == company_id,
            WorkOrderOperation.work_order_id == work_order.id,
            WorkOrderOperation.id.in_(operation_ids),
        )
        .all()
    }

    for allocation in allocations:
        if allocation.work_order_operation_id is None:
            continue
        operation = operations.get(allocation.work_order_operation_id)
        if operation is None:
            # The tie points at an operation that is no longer on this WO (or another
            # tenant's). Never consume against it.
            logger.warning(
                "Skipping allocation %s: operation %s not on WO %s (company %s)",
                allocation.id,
                allocation.work_order_operation_id,
                work_order.id,
                company_id,
            )
            continue
        # Captured BEFORE the savepoint: a rollback expires the instance, and the
        # failure record must not depend on re-loading a row the rollback disturbed.
        allocation_id = allocation.id
        allocation_part_id = allocation.part_id
        operation_id = operation.id
        nested = db.begin_nested()
        try:
            _consume_one_allocation(
                db,
                work_order=work_order,
                allocation=allocation,
                operation=operation,
                company_id=company_id,
                user_id=user_id,
                audit=audit,
                result=result,
            )
            nested.commit()
        except Exception as exc:  # degrade per-allocation, never break a GET
            nested.rollback()
            result.failed_allocation_ids.append(allocation_id)
            logger.exception(
                "Material consumption failed for allocation %s on WO %s (company %s)",
                allocation_id,
                work_order.id,
                company_id,
            )
            _record_consumption_failed(
                work_order=work_order,
                allocation_id=allocation_id,
                part_id=allocation_part_id,
                operation_id=operation_id,
                error=exc,
                company_id=company_id,
                audit=audit,
            )


# ---------------------------------------------------------------------------
# Lifecycle helpers (nest re-import, work-order delete)
# ---------------------------------------------------------------------------


class MaterialAllocationConsumedError(Exception):
    """A lifecycle action would orphan CONSUMED material. Routers map this to HTTP 409.

    Raised when an operation (nest re-import) or a work order (hard delete) that
    already has posted consumption is about to be destroyed. Deleting the operation
    out from under its ISSUE rows would orphan lot genealogy -- the ledger row's
    ``reference_id`` would point at nothing -- so the caller must reverse the
    consumption explicitly first (an explicit, reasoned verb; a later PR).
    """


def cancel_allocations_for_operations(
    db: Session,
    *,
    work_order: WorkOrder,
    operation_ids: list[int],
    company_id: int,
    audit: AuditService,
) -> list[int]:
    """Guard + DETACH every allocation scoped to operations that are about to be WIPED.

    ``build_laser_nest_child_work_order`` rebuilds a laser WO's operations wholesale
    (import replaces everything). Any allocation on a wiped operation with
    ``qty_consumed > 0`` raises ``MaterialAllocationConsumedError`` -> HTTP 409; the
    OPEN ones are CANCELLED (status is the tombstone -- these rows are never deleted)
    with an audit row so the untie is traceable. Returns the ids of the ties this call
    actually CANCELLED (already-cancelled rows are detached, not re-cancelled, so they
    are not in the list). Tenant-scoped; does not commit.

    **The tie's ``work_order_operation_id`` is CLEARED here, and that is load-bearing.**
    The caller's very next act is ``db.delete(operation)`` on exactly these operations,
    and ``work_order_material_allocations.work_order_operation_id`` is a plain FK with
    NO ``ON DELETE`` (migration 074) declared many-to-one with no parent backref -- so
    SQLAlchemy does not null it and Postgres raises ``IntegrityError`` on the DELETE,
    which the import endpoint turns into a misleading 400 ("a nest conflicts with an
    existing record"). The documented cancel-and-rebuild path was therefore unreachable
    in production on any laser WO that had a material tie -- the feature's headline flow.
    The suite could not see it: SQLite does not enforce foreign keys unless
    ``PRAGMA foreign_keys=ON``, which this project never sets.

    **The selection is deliberately status-BLIND, and that is the round-3 fix.** This
    used to filter ``status != CANCELLED``, which made the fix above incomplete in two
    ways, because an ALREADY-cancelled tie keeps pointing at its operation:

    * ``work_order_materials.delete_material_allocation`` (a manual untie) and
      ``cancel_open_allocations_for_work_order`` (the work-order soft delete) both set
      ``CANCELLED`` and deliberately RETAIN ``work_order_operation_id``. Such a row was
      neither guarded nor detached, so the FK violation -- and the misleading 400 --
      survived on any WO where a tie had been untied before the re-import. That made the
      rebuild permanently impossible for that work order through supported verbs alone.
    * Worse, the consumed-material guard was BYPASSED: the soft delete cancels every
      OPEN tie regardless of ``qty_consumed``, so a tie carrying real consumption could
      reach the wipe as a ``CANCELLED`` row the ``qty_consumed`` check never saw. The
      409 that exists precisely to stop ``work_order_operation``-referenced ISSUE rows
      being orphaned did not fire. Reading EVERY status widens the guard correctly.

    Nulling is safe and correct for these rows specifically: after this call every one
    of them is ``CANCELLED``, so NEITHER partial unique index applies
    (``uq_wo_material_alloc_open_op`` requires ``work_order_operation_id IS NOT NULL AND
    status='OPEN'``; ``uq_wo_material_alloc_open_wo`` requires ``IS NULL AND
    status='OPEN'``), and none carries consumption (guarded above), so no ledger row
    references the operation id being dropped. The ORIGINAL scope is preserved on the
    hash chain -- in the audit row's ``old_values`` *and*
    ``extra_data.work_order_operation_id`` -- so the tie's history still says which
    operation it was tied to.

    A detach of an already-cancelled tie is itself a state change on a tenant row, so it
    gets its OWN chain row (invariant #2): a ``log_update`` recording
    ``work_order_operation_id: <old> -> None`` with ``reason=superseded_by_reimport``.
    It is deliberately an UPDATE, not a second DELETE -- the row was already untied, and
    ``reopen_allocations_cancelled_by_delete`` reads the tie's most recent DELETE row to
    decide what a restore may resurrect, so a second DELETE here would rewrite that
    history. That reader has its own detach check; keep the two in lock-step.

    ``audit`` is REQUIRED, not optional. Cancelling a tie is a state change on a tenant
    table (invariant #2), and an optional-with-``None``-default audit made an unaudited
    CANCEL a one-line mistake for the next caller.
    """
    if not operation_ids:
        return []
    allocations = (
        db.query(WorkOrderMaterialAllocation)
        .filter(
            WorkOrderMaterialAllocation.company_id == company_id,
            WorkOrderMaterialAllocation.work_order_operation_id.in_(operation_ids),
        )
        .all()
    )
    if not allocations:
        return []

    consumed = [a for a in allocations if float(a.qty_consumed or 0) > _EPSILON]
    if consumed:
        raise MaterialAllocationConsumedError(
            "Cannot rebuild this work order's operations: material has already been consumed "
            f"against {len(consumed)} tied allocation(s). Reverse consumption first."
        )

    cancelled: list[int] = []
    for allocation in allocations:
        old_operation_id = allocation.work_order_operation_id
        old_status = allocation.status
        # Detach from the operation that is about to be physically deleted (see above).
        # Done for EVERY status: a row that is already cancelled still holds the FK.
        allocation.work_order_operation_id = None
        if old_status == AllocationStatus.OPEN:
            allocation.status = AllocationStatus.CANCELLED
            cancelled.append(allocation.id)
            audit.log_delete(
                "work_order_material_allocation",
                allocation.id,
                f"WO {work_order.work_order_number} / part {allocation.part_id}",
                old_values={
                    "status": AllocationStatus.OPEN.value,
                    "qty_consumed": allocation.qty_consumed,
                    "work_order_operation_id": old_operation_id,
                },
                description=(
                    f"Cancelled material allocation on WO {work_order.work_order_number}: "
                    f"the tied operation ({old_operation_id}) was superseded by a nest re-import"
                ),
                soft_delete=True,
                extra_data={
                    "reason": REIMPORT_CANCEL_REASON,
                    "work_order_id": work_order.id,
                    # The scope the tie HAD. The column is cleared so the operation delete
                    # below cannot FK-violate; this is where that fact survives.
                    "work_order_operation_id": old_operation_id,
                    "work_order_operation_id_cleared": True,
                    "part_id": allocation.part_id,
                    "new_status": AllocationStatus.CANCELLED.value,
                },
            )
        else:
            # Already CANCELLED (a manual untie, or a work-order soft delete) -- there is
            # no status change to record, but the DETACH is one, and without this row the
            # tie's original scope would exist nowhere: the column is about to read NULL,
            # which is indistinguishable from a tie that was always work-order-scoped.
            audit.log_update(
                "work_order_material_allocation",
                allocation.id,
                f"WO {work_order.work_order_number} / part {allocation.part_id}",
                old_values={"work_order_operation_id": old_operation_id},
                new_values={"work_order_operation_id": None},
                description=(
                    f"Detached {old_status.value} material allocation from operation "
                    f"({old_operation_id}) on WO {work_order.work_order_number}: the operation was "
                    "superseded by a nest re-import"
                ),
                extra_data={
                    "reason": REIMPORT_CANCEL_REASON,
                    "work_order_id": work_order.id,
                    "work_order_operation_id": old_operation_id,
                    "work_order_operation_id_cleared": True,
                    "part_id": allocation.part_id,
                    "status": old_status.value,
                },
            )
    db.flush()
    return cancelled


def cancel_open_allocations_for_work_order(
    db: Session,
    *,
    work_order: WorkOrder,
    company_id: int,
    audit: AuditService,
    reason: str = WORK_ORDER_DELETED_CANCEL_REASON,
) -> list[int]:
    """Auto-CANCEL every OPEN allocation when a work order is soft-deleted.

    Consumption already posted STANDS -- the material was physically used, and the
    ledger is the compliance record. The delete is never refused for a tie; only the
    forward-looking demand is closed out. Audited, tenant-scoped, does not commit.

    ``reason`` lands in the audit row's ``extra_data`` and is the ONLY thing that later
    tells ``reopen_allocations_cancelled_by_delete`` which ties this delete closed --
    keep the two in lock-step. Unlike the nest-re-import cancel, ``work_order_operation_id``
    is deliberately LEFT INTACT: a soft delete removes no operation rows, so there is no
    FK to detach from, and the tie must come back pointing at the same operation if the
    work order is restored.

    ``audit`` is REQUIRED (invariant #2): every CANCEL written here is a state change on
    a tenant table, so there is no caller for whom skipping the chain row is correct.
    """
    allocations = open_allocations_for_work_order(db, work_order.id, company_id)
    cancelled: list[int] = []
    for allocation in allocations:
        allocation.status = AllocationStatus.CANCELLED
        cancelled.append(allocation.id)
        audit.log_delete(
            "work_order_material_allocation",
            allocation.id,
            f"WO {work_order.work_order_number} / part {allocation.part_id}",
            old_values={"status": AllocationStatus.OPEN.value, "qty_consumed": allocation.qty_consumed},
            description=(
                f"Cancelled material allocation on WO {work_order.work_order_number}: "
                "the work order was deleted (consumption already posted stands)"
            ),
            soft_delete=True,
            extra_data={
                "reason": reason,
                "work_order_id": work_order.id,
                "work_order_operation_id": allocation.work_order_operation_id,
                "part_id": allocation.part_id,
                "new_status": AllocationStatus.CANCELLED.value,
            },
        )
    if cancelled:
        db.flush()
    return cancelled


def reopen_allocations_cancelled_by_delete(
    db: Session,
    *,
    work_order: WorkOrder,
    company_id: int,
    audit: AuditService,
) -> list[int]:
    """Re-OPEN the ties that this work order's own soft delete auto-cancelled.

    The delete/restore pair has to be symmetric. Soft delete cancels every OPEN tie to
    close out forward-looking demand; leaving them cancelled on restore means the work
    order runs to completion and its tied material SILENTLY never depletes -- and with no
    UI in PR 1, the only signal is a ``cancelled`` row nobody looks at. It gets worse
    once ``backflush_components`` is exposed (PR 4): an operation-scoped tie that has
    already consumed and was then cancelled by a delete/restore round trip no longer
    suppresses the BOM backflush in ``_drop_allocation_covered_parts``, so the same part
    is issued TWICE.

    **Only the ties THIS delete cancelled are resurrected.** A tie the planner untied by
    hand, or one a nest re-import superseded, was cancelled for a reason that a restore
    does not undo. The discriminator is the cancel's own audit row: every cancel path
    stamps ``extra_data.reason`` (``work_order_deleted`` here, ``superseded_by_reimport``
    for the re-import, absent for a manual untie), and the tie's MOST RECENT DELETE row
    is what its current CANCELLED state means. Reading the chain is a read -- the row is
    never written or altered here.

    **A tie DETACHED after that delete is not resurrected either.** A nest re-import
    clears ``work_order_operation_id`` on every tie scoped to an operation it wipes,
    including ones that were already cancelled -- and it records the detach as an UPDATE
    so it does not rewrite the DELETE history this function reads. Reopening such a tie
    would bring it back as a WORK-ORDER-scoped tie (its operation is NULL and the
    operation itself no longer exists), re-arming one-shot demand the planner never
    asked for and risking a collision with ``uq_wo_material_alloc_open_wo``. So a
    candidate whose cancel row named an operation while the row now holds NULL is left
    alone; the cancel's ``extra_data.work_order_operation_id`` is what makes that
    detectable without a second query.

    Degrades safely: if a cancel's audit row is missing (``AuditService.log`` swallows its
    own failures), that tie simply is not reopened -- the conservative direction, since
    the alternative is resurrecting a tie whose provenance we cannot establish.

    Audited (``action="restore"``), tenant-scoped, does not commit.
    """
    candidates = (
        db.query(WorkOrderMaterialAllocation)
        .filter(
            WorkOrderMaterialAllocation.company_id == company_id,
            WorkOrderMaterialAllocation.work_order_id == work_order.id,
            WorkOrderMaterialAllocation.status == AllocationStatus.CANCELLED,
        )
        .order_by(WorkOrderMaterialAllocation.id)
        .all()
    )
    if not candidates:
        return []

    by_id = {allocation.id: allocation for allocation in candidates}
    # Ascending id => the LAST row seen per allocation is its most recent cancel.
    latest_cancel: dict[int, dict] = {}
    for resource_id, extra_data in (
        db.query(AuditLog.resource_id, AuditLog.extra_data)
        .filter(
            AuditLog.company_id == company_id,
            AuditLog.resource_type == "work_order_material_allocation",
            AuditLog.action == "DELETE",
            AuditLog.resource_id.in_(list(by_id.keys())),
        )
        .order_by(AuditLog.id)
        .all()
    ):
        latest_cancel[resource_id] = extra_data or {}

    reopened: list[int] = []
    for allocation in candidates:
        cancel_extra = latest_cancel.get(allocation.id, {})
        if cancel_extra.get("reason") != WORK_ORDER_DELETED_CANCEL_REASON:
            continue
        if cancel_extra.get("work_order_operation_id") is not None and allocation.work_order_operation_id is None:
            # DETACHED after this delete cancelled it -- a nest re-import wiped the
            # operation it named. Reopening it would re-arm it as a work-order-scoped
            # tie the planner never created. See the docstring.
            continue
        allocation.status = AllocationStatus.OPEN
        reopened.append(allocation.id)
    if not reopened:
        return []

    db.flush()
    for allocation_id in reopened:
        allocation = by_id[allocation_id]
        audit.log_update(
            "work_order_material_allocation",
            allocation.id,
            f"WO {work_order.work_order_number} / part {allocation.part_id}",
            old_values={"status": AllocationStatus.CANCELLED.value},
            new_values={"status": AllocationStatus.OPEN.value},
            description=(
                f"Re-opened material allocation on WO {work_order.work_order_number}: " "the work order was restored"
            ),
            action="restore",
            extra_data={
                "reason": "work_order_restored",
                "work_order_id": work_order.id,
                "work_order_operation_id": allocation.work_order_operation_id,
                "part_id": allocation.part_id,
            },
        )
    return reopened


def allocations_on_work_order(
    db: Session,
    *,
    work_order_id: int,
    company_id: int,
) -> list[WorkOrderMaterialAllocation]:
    """EVERY allocation row on a work order, whatever its status. Tenant-scoped.

    Used by the HARD-delete path, which physically removes the work order and its
    operations: every allocation row pointing at them must go too, and one carrying
    consumption cannot, because the ledger's ``allocation_id`` back-reference must always
    resolve. The caller pairs this with ``ledger_backed_allocation_ids`` -- 409 when the
    LEDGER references any of them, physically remove the rest alongside the operations.

    (Named for what it returns. It was previously ``allocations_blocking_hard_delete``,
    which read as "the ones that block" -- it is every tie on the work order, and the
    blocking subset is whatever the ledger check hands back.)
    """
    return (
        db.query(WorkOrderMaterialAllocation)
        .filter(
            WorkOrderMaterialAllocation.company_id == company_id,
            WorkOrderMaterialAllocation.work_order_id == work_order_id,
        )
        .all()
    )


def ledger_backed_allocation_ids(
    db: Session,
    *,
    allocation_ids: list[int],
    company_id: int,
) -> set[int]:
    """Which of these allocations actually have ledger rows pointing at them.

    The LOAD-BEARING check for the hard-delete guard. ``qty_consumed`` is a documented
    CACHE (see the model docstring) and the ``inventory_transactions.allocation_id`` FK
    carries no ``ON DELETE``, so keying the guard on the cache means any drift between
    cache and ledger surfaces as an FK ``IntegrityError`` -- a 500 -- instead of the
    intended 409. Ask the ledger directly. Tenant-scoped; empty input short-circuits.
    """
    if not allocation_ids:
        return set()
    rows = (
        db.query(InventoryTransaction.allocation_id)
        .filter(
            InventoryTransaction.company_id == company_id,
            InventoryTransaction.allocation_id.in_(allocation_ids),
        )
        .distinct()
        .all()
    )
    return {row[0] for row in rows if row[0] is not None}


def work_order_tie_is_already_issued(
    db: Session,
    *,
    work_order_id: int,
    part_id: int,
    company_id: int,
) -> bool:
    """True when a WORK-ORDER-scoped tie on this part could NEVER consume.

    A work-order-scoped tie drains through the one-shot backflush, which skips any
    component that already has a WO-level ISSUE row (its idempotency key) -- and
    ``uq_wo_inventory_issue`` physically forbids the second ISSUE that would be needed
    anyway. So a tie created AFTER the part was issued to this work order is dead on
    arrival: it stays OPEN with ``qty_consumed`` at 0 forever, indistinguishable in the
    API from a tie that simply has not consumed yet. A planner would tie a sheet,
    believe stock will deplete, and it silently never would. The tie endpoint 409s on
    this instead.

    Operation-scoped ties are unaffected -- they post under ``work_order_operation``,
    outside that unique index -- which is exactly the remedy the 409 names.
    """
    return _component_already_issued(db, work_order_id, part_id, company_id)
