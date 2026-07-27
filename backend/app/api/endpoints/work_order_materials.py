"""Work-order material allocations — the OPTIONAL tie between a WO and stock material.

Mounted as a SIBLING router under the same ``/work-orders`` prefix rather than added to
``work_orders.py``. Two reasons, and the precedent is already in ``api/router.py``
(``scrap_reasons`` sits under ``/quality`` for exactly this reason):

  * ``work_orders.py`` is >4k lines and already carries the completion, laser-nest and
    import machinery; material ties are a self-contained noun with their own service.
  * No route shadowing is possible: every path here is three segments
    (``/{work_order_id}/material-allocations[...]``) with a LITERAL middle segment, so
    it cannot collide with ``work_orders.py``'s ``/{work_order_id}`` or its other
    literal sub-resources.

**One verb here posts inventory, and exactly one: the RETURN.** Consumption still rides
the completion call sites only (``apply_operation_completion_inventory_effects`` /
``apply_completion_inventory_effects``) and every other endpoint below manages the
planning row alone. The return is the deliberate exception — an actor, a required
reason, and an appended compensating transaction — because un-consuming can never be
something a completion path does for you (invariant 6b).

RBAC: reads are open to any authenticated tenant user; every mutating verb requires
ADMIN / MANAGER / SUPERVISOR. Deliberately NOT under ``/api/v1/shop-floor`` — the kiosk
path fence (``deps.py``) exists to keep operator-scoped tokens off office endpoints, and
tying material is an office/planning action. The return sits on the same side of that
fence for a stronger reason than the rest: moving stock back with a reason is a bigger
power than tying it.
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_audit_service, get_current_active_user, get_current_company_id, require_role
from app.db.database import get_db
from app.db.tenant_filter import tenant_query
from app.models.audit_log import AuditLog
from app.models.inventory import InventoryItem, InventoryTransaction, TransactionType
from app.models.part import Part
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder, WorkOrderOperation
from app.models.work_order_material import AllocationStatus, WorkOrderMaterialAllocation
from app.schemas.work_order_material import (
    MaterialAllocationCreate,
    MaterialAllocationResponse,
    MaterialAllocationUpdate,
    MaterialConsumptionLine,
    MaterialReturnLot,
    MaterialReturnRequest,
    MaterialReturnResponse,
)
from app.services.audit_service import AuditService
from app.services.completion_inventory_service import net_consumed_quantity_for_allocation
from app.services.material_consumption_service import (
    CONSUMPTION_EPSILON,
    MaterialReturnRefused,
    is_consumable_item,
    return_tied_material,
    work_order_tie_is_already_issued,
)
from app.services.work_order_state_service import TERMINAL_WO_STATUSES

logger = logging.getLogger(__name__)

router = APIRouter()

WRITE_ROLES = [UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]


def _load_work_order(db: Session, work_order_id: int, company_id: int) -> WorkOrder:
    """Tenant-scoped, non-deleted work order or 404 (invariant #1 + soft-delete)."""
    work_order = (
        tenant_query(db, WorkOrder, company_id)
        .filter(WorkOrder.id == work_order_id, WorkOrder.is_deleted == False)  # noqa: E712
        .first()
    )
    if work_order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Work order not found")
    return work_order


def _load_allocation(
    db: Session, work_order_id: int, allocation_id: int, company_id: int
) -> WorkOrderMaterialAllocation:
    allocation = (
        tenant_query(db, WorkOrderMaterialAllocation, company_id)
        .filter(
            WorkOrderMaterialAllocation.id == allocation_id,
            WorkOrderMaterialAllocation.work_order_id == work_order_id,
        )
        .first()
    )
    if allocation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Material allocation not found")
    return allocation


def _uom_value(part: Part) -> str:
    """``Part.unit_of_measure`` as the lowercase enum VALUE the snapshot column stores."""
    uom = part.unit_of_measure
    return getattr(uom, "value", uom) or "each"


def _resolve_pinned_item(
    db: Session,
    *,
    pinned_inventory_item_id: int,
    part: Part,
    company_id: int,
) -> InventoryItem:
    """Validate a lot pin: same company, and the SAME part as the tie.

    A pin naming another tenant's lot is a security defect, so the lookup is
    company-scoped and a miss is a 404 (never a leak of "exists elsewhere").

    A pin naming a DIFFERENT part is refused with 422. There is no unit conversion
    anywhere in this platform, so a cross-UOM tie could only ever consume the wrong
    quantity of the wrong material; the error names the UOM clash when the two parts
    also disagree on units.

    A pin naming a HELD lot (``on_hold`` / ``quarantine`` / ``rejected``) or an inactive
    one is refused with 422. FIFO already excludes those lots; the pinned branch does
    not, so without this check a lot-directed tie would consume nonconforming or held
    material straight into an as-built record with no signal (AS9100D 8.7). The refusal
    belongs HERE, at tie time, and not at consume time: consumption also runs from a GET
    where refusing is not an option. A lot held AFTER it was pinned still consumes, but
    writes a ``HELD_MATERIAL_CONSUMED`` audit row (``material_consumption_service``).
    """
    item = tenant_query(db, InventoryItem, company_id).filter(InventoryItem.id == pinned_inventory_item_id).first()
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pinned inventory lot not found")
    if not is_consumable_item(item):
        state = "inactive" if not item.is_active else f"'{item.status}'"
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Lot {item.lot_number or item.id} is {state} and may not be tied to work: pinning it would "
                "consume held material into product. Release the lot, or pin an available one."
            ),
        )
    if item.part_id != part.id:
        pinned_part = tenant_query(db, Part, company_id).filter(Part.id == item.part_id).first()
        pinned_uom = _uom_value(pinned_part) if pinned_part else None
        part_uom = _uom_value(part)
        if pinned_uom and pinned_uom != part_uom:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"Unit-of-measure mismatch: the tie is in '{part_uom}' but the pinned lot is "
                    f"'{pinned_uom}'. No unit conversion exists — pin a lot of part "
                    f"{part.part_number}."
                ),
            )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"The pinned lot belongs to a different part; pin a lot of part {part.part_number}.",
        )
    return item


def _detached_operation_ids(
    db: Session, allocations: List[WorkOrderMaterialAllocation], company_id: int
) -> dict[int, int]:
    """Allocation id -> the operation a nest re-import DETACHED that tie from.

    A re-import clears ``work_order_operation_id`` on every tie scoped to an operation it
    wipes, because that FK has no ``ON DELETE`` and the operation row is about to go
    (``material_consumption_service.cancel_allocations_for_operations``). The original
    scope therefore survives ONLY on the tamper-evident hash chain — which meant a
    detached tie read back over the API as ``work_order_operation_id: null``, byte-
    identical to a tie that was always work-order-scoped, with nothing on the row to
    tell the two apart. This reads that one fact back so the API can echo it.

    The chain stays the record of record: this is a display read, and a cancel row that
    ``AuditService.log`` failed to write simply yields no echo rather than a wrong one.
    Candidates are narrowed to detached-SHAPED rows (no operation, no longer OPEN) so the
    lookup is skipped entirely for the common all-live-ties case.
    """
    candidates = [a.id for a in allocations if a.work_order_operation_id is None and a.status != AllocationStatus.OPEN]
    if not candidates:
        return {}
    detached: dict[int, int] = {}
    # Ascending id => the LAST detach row seen per allocation wins.
    for resource_id, extra_data in (
        db.query(AuditLog.resource_id, AuditLog.extra_data)
        .filter(
            AuditLog.company_id == company_id,
            AuditLog.resource_type == "work_order_material_allocation",
            AuditLog.resource_id.in_(candidates),
        )
        .order_by(AuditLog.id)
        .all()
    ):
        extra = extra_data or {}
        old_operation_id = extra.get("work_order_operation_id")
        if extra.get("work_order_operation_id_cleared") and old_operation_id is not None:
            detached[resource_id] = old_operation_id
    return detached


def _display_maps(
    db: Session, allocations: List[WorkOrderMaterialAllocation], company_id: int
) -> tuple[dict[int, Part], dict[int, Optional[str]], dict[int, int]]:
    """Batch the display lookups ``_serialize`` needs, tenant-scoped.

    One SELECT for the parts, one for the operations and (only when some tie actually
    looks detached) one for the detach markers on the audit chain, whatever the row
    count. Doing them per row is an N+1 that scales with the number of ties on a work
    order — cheap today (ties ship dark) and exactly the sort of thing that is never
    revisited once the UI lands in PR 2.
    """
    part_ids = {a.part_id for a in allocations if a.part_id is not None}
    parts = (
        {p.id: p for p in tenant_query(db, Part, company_id).filter(Part.id.in_(part_ids)).all()} if part_ids else {}
    )
    operation_ids = {a.work_order_operation_id for a in allocations if a.work_order_operation_id is not None}
    operation_numbers = (
        {
            op_id: op_number
            for op_id, op_number in tenant_query(db, WorkOrderOperation, company_id)
            .with_entities(WorkOrderOperation.id, WorkOrderOperation.operation_number)
            .filter(WorkOrderOperation.id.in_(operation_ids))
            .all()
        }
        if operation_ids
        else {}
    )
    return parts, operation_numbers, _detached_operation_ids(db, allocations, company_id)


def _serialize(
    db: Session,
    allocation: WorkOrderMaterialAllocation,
    company_id: int,
    parts: Optional[dict[int, Part]] = None,
    operation_numbers: Optional[dict[int, Optional[str]]] = None,
    detached_operations: Optional[dict[int, int]] = None,
) -> MaterialAllocationResponse:
    """One tie as its response schema.

    ``parts`` / ``operation_numbers`` / ``detached_operations`` are the batched maps from
    ``_display_maps``; pass them from any multi-row caller. Omitted (the single-row
    verbs) they are built for this one row, which is the same queries the per-row version
    always issued.
    """
    if parts is None or operation_numbers is None or detached_operations is None:
        parts, operation_numbers, detached_operations = _display_maps(db, [allocation], company_id)
    part = parts.get(allocation.part_id)
    operation_number: Optional[str] = None
    if allocation.work_order_operation_id is not None:
        operation_number = operation_numbers.get(allocation.work_order_operation_id)
    return MaterialAllocationResponse(
        id=allocation.id,
        work_order_id=allocation.work_order_id,
        work_order_operation_id=allocation.work_order_operation_id,
        operation_number=operation_number,
        detached_from_operation_id=detached_operations.get(allocation.id),
        part_id=allocation.part_id,
        part_number=part.part_number if part else None,
        part_name=part.name if part else None,
        source=allocation.source,
        status=allocation.status,
        qty_per_run=allocation.qty_per_run,
        qty_planned=allocation.qty_planned,
        unit_of_measure=allocation.unit_of_measure,
        qty_consumed=allocation.qty_consumed,
        pinned_inventory_item_id=allocation.pinned_inventory_item_id,
        pinned_lot_number=allocation.pinned_lot_number,
        notes=allocation.notes,
        created_by=allocation.created_by,
        created_at=allocation.created_at,
        updated_at=allocation.updated_at,
    )


def _assert_qty_per_run_not_under_consumed(
    db: Session,
    *,
    allocation: WorkOrderMaterialAllocation,
    new_qty_per_run: float,
    consumed: float,
    company_id: int,
) -> None:
    """Refuse a ``qty_per_run`` edit that would push the LIVE target under ``qty_consumed``.

    The operation-scoped twin of the ``qty_planned`` guard, and it was missing: the plan
    quantity was protected while the per-run rate — the number the engine actually
    consumes against — could be lowered freely, which made this the cheapest way in the
    whole API to manufacture ``consumed > target``.

    Why refuse rather than merely allow it (over-consumption IS an ordinary steady state
    since PR 2.5): the two arrive by opposite routes. There it is the residue of a REAL
    event — a supervisor walking back a count, a scrap absolute-write — recorded on the
    chain with its own reason. Here nothing happened on the floor; the plan is being
    rewritten UNDER posted consumption. And it is not harmless bookkeeping, because
    ``target`` is exactly what bounds ``correct_over_consumption``: lowering
    ``qty_per_run`` toward zero would open an unbounded return against a tie that stays
    OPEN, which is precisely the middle ground the two named intents exist to close
    (``MaterialReturnIntent``). The honest sequence is the other one — correct the
    production record, or return the material and untie, then state the corrected rate on
    a new tie.

    The predicate is "never WORSEN the gap", not "never have a gap": an edit is allowed
    when the new target covers what is consumed OR when it is at least as high as the
    current one. Refusing on the gap alone would block a planner RAISING ``qty_per_run``
    on a tie already over-consumed from a walk-back — an edit that reduces the very
    problem this guard exists to prevent.

    An operation that is no longer on the work order is skipped: its live target is
    already 0 by ``_live_consumption_target``'s own rule, no consumption leg can reach
    the tie, and the edit therefore manufactures nothing.
    """
    if consumed <= CONSUMPTION_EPSILON:
        return
    operation = (
        tenant_query(db, WorkOrderOperation, company_id)
        .filter(
            WorkOrderOperation.id == allocation.work_order_operation_id,
            WorkOrderOperation.work_order_id == allocation.work_order_id,
        )
        .first()
    )
    if operation is None:
        return
    runs = float(operation.quantity_complete or 0) + float(operation.quantity_scrapped or 0)
    old_target = float(allocation.qty_per_run if allocation.qty_per_run is not None else 1.0) * runs
    new_target = float(new_qty_per_run) * runs
    if new_target >= consumed - CONSUMPTION_EPSILON or new_target >= old_target - CONSUMPTION_EPSILON:
        return
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail=(
            f"qty_per_run cannot be lowered to {new_qty_per_run}: this operation has {runs:g} run(s) "
            f"recorded, so the tie would only account for {round(new_target, 6)} "
            f"{allocation.unit_of_measure} while {consumed} has already been consumed. Return the "
            "over-consumed material first, or correct the recorded production."
        ),
    )


def _consumption_lines(
    db: Session,
    allocation: WorkOrderMaterialAllocation,
    company_id: int,
) -> List[MaterialConsumptionLine]:
    """Per-source-lot issued / returned / net for one tie, straight off the LEDGER.

    ``qty_consumed`` on the tie is a documented cache; this is the authoritative answer,
    and it is the one the return dialog must show — the return credits lots, not a tie,
    so a single total would hide the only fact the operator can act on.

    Ordering and aggregation deliberately mirror
    ``material_consumption_service._plan_material_return``: ISSUE rows walked newest-first
    (``id DESC``), so a lot's position here is the order a return would credit it in, and
    ``net = issued - returned`` per ``(allocation, inventory_item)`` is exactly that
    plan's per-lot capacity. Preview and outcome therefore cannot disagree.

    Lots whose ``net`` is zero are still listed: they are part of this tie's movement
    history, and dropping them would make a fully-returned tie look as though the material
    had never touched that lot. Pure read — no session mutation, nothing to commit.
    """
    rows = (
        db.query(InventoryTransaction)
        .filter(
            InventoryTransaction.company_id == company_id,
            InventoryTransaction.allocation_id == allocation.id,
        )
        .order_by(InventoryTransaction.id.desc())
        .all()
    )
    order: List[int] = []
    issued: dict[int, float] = {}
    returned: dict[int, float] = {}
    ledger_lot: dict[int, Optional[str]] = {}
    for txn in rows:
        item_id = txn.inventory_item_id
        if item_id is None:
            # No stock row => nothing to credit back to. Cannot occur on this path (both
            # writers resolve or mint a row) and is skipped rather than guessed at, the
            # same way the return planner skips it.
            continue
        quantity = abs(float(txn.quantity or 0))
        if txn.transaction_type == TransactionType.ISSUE:
            if item_id not in issued:
                issued[item_id] = 0.0
                order.append(item_id)
                ledger_lot[item_id] = txn.lot_number
            issued[item_id] += quantity
        elif txn.transaction_type == TransactionType.RETURN:
            returned[item_id] = returned.get(item_id, 0.0) + quantity

    # Current lot numbers, tenant-scoped: this is what a return would actually credit
    # (it reads the stock row, not the historical ledger label). Falls back to the
    # ledger row's own lot_number when the stock row is gone — a state the return itself
    # refuses with 409, so the preview should still name what it can.
    items = (
        {
            item.id: item
            for item in tenant_query(db, InventoryItem, company_id).filter(InventoryItem.id.in_(order)).all()
        }
        if order
        else {}
    )

    lines: List[MaterialConsumptionLine] = []
    for item_id in order:
        issued_qty = issued.get(item_id, 0.0)
        returned_qty = returned.get(item_id, 0.0)
        net = issued_qty - returned_qty
        item = items.get(item_id)
        lines.append(
            MaterialConsumptionLine(
                inventory_item_id=item_id,
                lot_number=(item.lot_number if item is not None else ledger_lot.get(item_id)),
                issued=issued_qty,
                returned=returned_qty,
                # Clamp float dust so a squared-up lot reads 0, not 4e-16 (the
                # receiving-correction precedent, and what the engine's cache does).
                net=0.0 if abs(net) <= CONSUMPTION_EPSILON else net,
            )
        )
    return lines


@router.get(
    "/{work_order_id}/material-allocations",
    response_model=List[MaterialAllocationResponse],
    summary="List the material ties on a work order",
)
def list_material_allocations(
    work_order_id: int,
    include_inactive: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    company_id: int = Depends(get_current_company_id),
):
    """Every material allocation on a work order, oldest first (ascending id).

    CANCELLED/CLOSED rows are included by default: they are the tombstones the ledger's
    ``allocation_id`` back-reference resolves to, so hiding them would make consumed
    material look untied. Pass ``include_inactive=false`` for the live (OPEN) ties only.

    A tie a nest re-import DETACHED reads back with ``work_order_operation_id: null`` —
    the column is cleared so the superseded operation can be deleted — and carries
    ``detached_from_operation_id`` naming the operation it used to be scoped to, so it is
    not confused with a tie that was always work-order-scoped.
    """
    _load_work_order(db, work_order_id, company_id)
    query = tenant_query(db, WorkOrderMaterialAllocation, company_id).filter(
        WorkOrderMaterialAllocation.work_order_id == work_order_id
    )
    if not include_inactive:
        query = query.filter(WorkOrderMaterialAllocation.status == AllocationStatus.OPEN)
    allocations = query.order_by(WorkOrderMaterialAllocation.id).all()
    parts, operation_numbers, detached_operations = _display_maps(db, allocations, company_id)
    return [
        _serialize(db, allocation, company_id, parts, operation_numbers, detached_operations)
        for allocation in allocations
    ]


@router.post(
    "/{work_order_id}/material-allocations",
    response_model=MaterialAllocationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Tie a material part to a work order or one of its operations",
)
def create_material_allocation(
    work_order_id: int,
    payload: MaterialAllocationCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Create the tie that makes material deplete as this work completes.

    Every referenced row — work order, part, operation and pinned lot — is validated
    company-scoped; a tie pointing at another tenant's operation or lot is a security
    defect, not a 404 nuisance.

    ``qty_per_run`` applies to OPERATION-scoped ties only, where it defaults to ``1.0``
    when omitted. Supplying it on a work-order-scoped tie (no ``work_order_operation_id``)
    is **422** — there are no runs to scale by, and silently discarding it used to leave
    the planner believing a per-run rule was in force when the stored value was NULL.
    ``PATCH`` refuses the same combination the same way.

    ``pinned_inventory_item_id`` is honored on BOTH tie shapes: the per-run engine
    consumes from the pinned lot exclusively, and a work-order-scoped tie carries its
    pin into the completion backflush's tie leg. A held lot is refused here (422); a lot
    held AFTER pinning still consumes and writes ``HELD_MATERIAL_CONSUMED``.

    Refused with **409** when the tie could never consume:

    * the work order is TERMINAL (complete / closed / cancelled) — nothing will drive a
      completion through the consumption engine again; and
    * a work-order-scoped tie whose part already carries a LEGACY (pre-PR-4.4) one-shot
      ``('work_order', ISSUE)`` row on this work order. The backflush's legacy fence
      (``_component_already_issued``) drops such a part, and ``uq_wo_inventory_issue``
      forbids a second ISSUE under that shape at any price, so the tie would sit OPEN at
      ``qty_consumed`` 0 forever. **This refusal is UNREACHABLE** — creating a tie needs a
      non-terminal work order, a ``work_order``-shaped component ISSUE needs the
      backflush, the backflush only runs at COMPLETE, and COMPLETE → non-terminal is
      blocked — and it is kept anyway: it costs one existence query, fails safe, and is
      the correct fence if that reachability argument ever stops holding.
    """
    work_order = _load_work_order(db, work_order_id, company_id)

    # A tie that can never consume is a lie: it sits OPEN at qty_consumed 0 forever,
    # indistinguishable in the API from a tie that simply has not consumed YET, while
    # advertising demand to whoever reads the tie list. A terminal work order has
    # finished its lifecycle and will not be driven through the consumption engine
    # again, so refuse rather than create one. (Deliberately NOT justified by what any
    # individual completion path guards on — force-complete, for one, refuses a terminal
    # work order through its own explicit checks rather than ``TERMINAL_WO_STATUSES``,
    # so a rationale phrased as "every path guards on this constant" would be wrong on
    # the mechanism while right on the conclusion.)
    if work_order.status in TERMINAL_WO_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Work order {work_order.work_order_number} is {work_order.status.value}; material cannot be "
                "tied to a work order that has finished — this tie could never consume."
            ),
        )

    if payload.qty_per_run is not None and payload.work_order_operation_id is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="qty_per_run applies to operation-scoped ties only.",
        )

    part = (
        tenant_query(db, Part, company_id)
        .filter(Part.id == payload.part_id, Part.is_deleted == False)  # noqa: E712
        .first()
    )
    if part is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Material part not found")

    if payload.work_order_operation_id is None and work_order_tie_is_already_issued(
        db, work_order_id=work_order.id, part_id=part.id, company_id=company_id
    ):
        # WHAT THIS GUARD NOW MEANS. ``_component_already_issued`` still keys on
        # ``reference_type='work_order'`` ISSUE rows, and since PR 4.4 nothing writes
        # that shape -- the backflush posts ``work_order_backflush``. So this fires only
        # on a work order carrying a LEGACY pre-4.4 one-shot row, and it is that work
        # order's permanent fence out of the reconciling engine.
        #
        # The remedy named here is deliberately NOT "return the material". PR 3's RETURN
        # verb appends a compensating row and never removes the ISSUE row, while this
        # guard (and ``uq_wo_inventory_issue`` behind it) keys on the ISSUE row's
        # existence -- so a return would leave this 409 firing exactly as before, having
        # moved stock for nothing. Operation-scoped ties post outside that index, which is
        # why they are the way through.
        #
        # UNREACHABLE, and kept: creating a tie requires a NON-terminal work order, a
        # ``work_order``-shaped component ISSUE requires the backflush, the backflush only
        # runs at COMPLETE, and COMPLETE -> non-terminal is blocked. No operator can
        # produce the state. Shipping a refusal whose stated reason is WRONG would be a
        # records-integrity defect even when nobody can trigger it, which is why the
        # wording is corrected rather than left alone.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Part {part.part_number} already has a one-time issue recorded against work order "
                f"{work_order.work_order_number}; this tie could never consume — tie the material at "
                "the operation level instead, which posts outside the one-issue-per-work-order guard."
            ),
        )

    operation: Optional[WorkOrderOperation] = None
    if payload.work_order_operation_id is not None:
        operation = (
            tenant_query(db, WorkOrderOperation, company_id)
            .filter(
                WorkOrderOperation.id == payload.work_order_operation_id,
                WorkOrderOperation.work_order_id == work_order.id,
            )
            .first()
        )
        if operation is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Operation not found on this work order",
            )

    pinned_item: Optional[InventoryItem] = None
    if payload.pinned_inventory_item_id is not None:
        pinned_item = _resolve_pinned_item(
            db,
            pinned_inventory_item_id=payload.pinned_inventory_item_id,
            part=part,
            company_id=company_id,
        )

    # App-layer uniqueness. The two partial unique indexes are the RACE backstop; this
    # check is what turns a duplicate tie into an intelligible 409 instead of a 500.
    duplicate = (
        tenant_query(db, WorkOrderMaterialAllocation, company_id)
        .filter(
            WorkOrderMaterialAllocation.work_order_id == work_order.id,
            WorkOrderMaterialAllocation.part_id == part.id,
            WorkOrderMaterialAllocation.status == AllocationStatus.OPEN,
            (
                WorkOrderMaterialAllocation.work_order_operation_id == payload.work_order_operation_id
                if payload.work_order_operation_id is not None
                else WorkOrderMaterialAllocation.work_order_operation_id.is_(None)
            ),
        )
        .first()
    )
    if duplicate is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Part {part.part_number} is already tied to this "
                f"{'operation' if payload.work_order_operation_id else 'work order'} "
                f"(allocation {duplicate.id}). Edit or untie that allocation instead."
            ),
        )

    allocation = WorkOrderMaterialAllocation(
        company_id=company_id,
        work_order_id=work_order.id,
        work_order_operation_id=payload.work_order_operation_id,
        part_id=part.id,
        source=payload.source,
        status=AllocationStatus.OPEN,
        # Operation-scoped ties are run-scaled and get an explicit 1.0 default;
        # work-order-scoped ties leave it NULL (a supplied value was 422'd above, so
        # this can only be the omitted case).
        qty_per_run=(
            (payload.qty_per_run if payload.qty_per_run is not None else 1.0)
            if payload.work_order_operation_id is not None
            else None
        ),
        qty_planned=payload.qty_planned,
        # Snapshot so the tie stays readable after the part's UoM is changed.
        unit_of_measure=_uom_value(part),
        qty_consumed=0.0,
        pinned_inventory_item_id=pinned_item.id if pinned_item else None,
        pinned_lot_number=pinned_item.lot_number if pinned_item else None,
        notes=payload.notes,
        created_by=current_user.id,
    )
    db.add(allocation)
    try:
        db.flush()
    except IntegrityError as exc:
        # The partial unique index fired: a concurrent request won the same key between
        # our check and this insert. Same user-facing answer as the check above.
        db.rollback()
        logger.info("Material allocation uniqueness race on WO %s part %s", work_order.id, part.id)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Part {part.part_number} is already tied to this work order.",
        ) from exc

    audit.log_create(
        "work_order_material_allocation",
        allocation.id,
        f"WO {work_order.work_order_number} / part {part.part_number}",
        new_values=allocation,
        description=(
            f"Tied {allocation.qty_planned} {allocation.unit_of_measure} of part {part.part_number} "
            f"to work order {work_order.work_order_number}"
            + (f" operation {operation.operation_number or operation.id}" if operation else "")
        ),
        extra_data={
            "work_order_id": work_order.id,
            "work_order_operation_id": allocation.work_order_operation_id,
            "part_id": part.id,
            "source": allocation.source.value,
            "pinned_inventory_item_id": allocation.pinned_inventory_item_id,
        },
    )
    db.commit()
    db.refresh(allocation)
    return _serialize(db, allocation, company_id)


@router.patch(
    "/{work_order_id}/material-allocations/{allocation_id}",
    response_model=MaterialAllocationResponse,
    summary="Edit a material tie's quantities, lot pin, or notes",
)
def update_material_allocation(
    work_order_id: int,
    allocation_id: int,
    payload: MaterialAllocationUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Adjust an OPEN tie. Consumption already posted is untouched.

    Raising ``qty_per_run`` re-targets the sum-delta engine, so the NEXT completion
    tops up the difference; lowering it is a no-op until the target overtakes what was
    already consumed (the engine never auto-reverses).

    Two combinations are refused with **422** rather than silently resolved:

    * ``clear_pinned_inventory_item=true`` together with a ``pinned_inventory_item_id``.
      The two say opposite things about the same field, and the clear used to win
      silently — so a caller who wanted the new pin got an UNPINNED tie and a 200.
      Which lot is consumed is a genealogy fact; ambiguity about it is refused.
    * ``qty_planned`` lowered BELOW what has already been consumed. The engine never
      auto-reverses, so the row would immediately read as over-consumed and the
      work-order-scoped drain (``qty_planned - qty_consumed``) would go negative.
      Return the consumed material first rather than editing the plan under it.
    * ``qty_per_run`` lowered so far that the LIVE target
      (``qty_per_run x (quantity_complete + quantity_scrapped)``) falls below
      ``qty_consumed`` — the operation-scoped twin of the ``qty_planned`` rule, and
      until PR 3 the cheapest way to manufacture an over-consumed tie. See the guard
      itself for why it is refused rather than merely recorded.
    """
    work_order = _load_work_order(db, work_order_id, company_id)
    allocation = _load_allocation(db, work_order_id, allocation_id, company_id)
    if allocation.status != AllocationStatus.OPEN:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This allocation is {allocation.status.value}; only an open tie can be edited.",
        )

    if payload.clear_pinned_inventory_item and payload.pinned_inventory_item_id is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Send either clear_pinned_inventory_item or pinned_inventory_item_id, not both — "
                "they ask for opposite things."
            ),
        )

    consumed = float(allocation.qty_consumed or 0)
    # Epsilon, not a bare `<`: qty_consumed accumulates float sums (0.1 x 3 stores
    # 0.30000000000000004), so an exact comparison refuses a planner lowering
    # qty_planned to the value the tie has genuinely consumed.
    if payload.qty_planned is not None and payload.qty_planned < consumed - CONSUMPTION_EPSILON:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"qty_planned cannot be lowered to {payload.qty_planned}: {consumed} "
                f"{allocation.unit_of_measure} has already been consumed against this allocation. "
                "Return the over-consumed material first (Return material on this tie), then lower the plan."
            ),
        )

    if payload.qty_per_run is not None:
        if allocation.work_order_operation_id is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="qty_per_run applies to operation-scoped ties only.",
            )
        _assert_qty_per_run_not_under_consumed(
            db,
            allocation=allocation,
            new_qty_per_run=payload.qty_per_run,
            consumed=consumed,
            company_id=company_id,
        )

    part = tenant_query(db, Part, company_id).filter(Part.id == allocation.part_id).first()
    old_values = {
        "qty_per_run": allocation.qty_per_run,
        "qty_planned": allocation.qty_planned,
        "pinned_inventory_item_id": allocation.pinned_inventory_item_id,
        "pinned_lot_number": allocation.pinned_lot_number,
        "notes": allocation.notes,
    }

    if payload.qty_per_run is not None:
        # Scope + over-consumption already validated above, before any mutation.
        allocation.qty_per_run = payload.qty_per_run
    if payload.qty_planned is not None:
        allocation.qty_planned = payload.qty_planned
    if payload.clear_pinned_inventory_item:
        allocation.pinned_inventory_item_id = None
        allocation.pinned_lot_number = None
    elif payload.pinned_inventory_item_id is not None:
        if part is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Material part not found")
        pinned_item = _resolve_pinned_item(
            db,
            pinned_inventory_item_id=payload.pinned_inventory_item_id,
            part=part,
            company_id=company_id,
        )
        allocation.pinned_inventory_item_id = pinned_item.id
        allocation.pinned_lot_number = pinned_item.lot_number
    if payload.notes is not None:
        allocation.notes = payload.notes

    db.flush()
    audit.log_update(
        "work_order_material_allocation",
        allocation.id,
        f"WO {work_order.work_order_number} / part {part.part_number if part else allocation.part_id}",
        old_values=old_values,
        new_values={
            "qty_per_run": allocation.qty_per_run,
            "qty_planned": allocation.qty_planned,
            "pinned_inventory_item_id": allocation.pinned_inventory_item_id,
            "pinned_lot_number": allocation.pinned_lot_number,
            "notes": allocation.notes,
        },
        description=f"Updated material allocation {allocation.id} on WO {work_order.work_order_number}",
        extra_data={"work_order_id": work_order.id, "part_id": allocation.part_id},
    )
    db.commit()
    db.refresh(allocation)
    return _serialize(db, allocation, company_id)


@router.delete(
    "/{work_order_id}/material-allocations/{allocation_id}",
    response_model=MaterialAllocationResponse,
    summary="Untie material from a work order",
)
def delete_material_allocation(
    work_order_id: int,
    allocation_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Untie: sets ``status = CANCELLED``. The row is never physically deleted.

    Refused with 409 while the LEDGER still holds material out against this tie.
    Cancelling a tie that moved stock, without moving it back, would strand
    ``inventory_transactions.allocation_id`` rows against a tombstone with no account of
    where the material went; the reversal (RETURN) is a separate, reasoned verb.

    **The guard reads the ledger, not ``qty_consumed``.** It used to read the cache, while
    hard delete (since PR 1) and nest re-import (since PR 3) both read the ledger — an
    asymmetry nobody chose, and one the cache's own documented semantics make unsafe. Two
    concrete failures it produced, in opposite directions:

    * ``correct_over_consumption`` down to a zero live target leaves ``qty_consumed`` at 0
      on a still-OPEN tie. The cache said "nothing consumed" and permitted the untie, on a
      tie the ledger could still be backing.
    * the completion backflush advances a work-order-scoped tie's ``qty_consumed`` to
      ``qty_planned``, which is not the quantity the ISSUE posted (the row carries the
      summed BOM + tie demand). A cache reading above the ledger refused an untie that
      would have stranded nothing.

    The ledger figure is SIGNED — ISSUE minus RETURN — so a tie that gave everything back
    is untieable. That is deliberate and it closes a dead end rather than opening one: an
    existence-keyed guard (the shape hard delete uses, correctly, since a RETURN row
    references the tie as durably as the ISSUE it compensates) would refuse forever, while
    ``return_and_untie`` would 422 with nothing left to return.

    One residual, stated precisely because an earlier draft of this docstring overstated
    it: if the tie's cache has drifted BELOW the signed ledger net, this DELETE's 409 can
    stand while ledger rows remain. It is **not** a dead end for the user.
    ``return_and_untie`` does not route through this guard at all — it cancels the tie
    inside the return service, and this handler early-returns on an already-CANCELLED tie
    before the net is ever computed. So the tie always closes; what the residual net then
    represents is the BOM's material, legitimately still out, not the tie's. The drift
    itself requires the ISSUE row to carry summed BOM + tie demand, which requires
    ``Part.backflush_components`` — unset anywhere in ``app/`` — so it is dark today.

    Audit verb: ``log_delete(soft_delete=True)``. The HTTP verb is DELETE, the operator
    intent is "remove this tie", and the question an auditor asks is "who removed it and
    when" — a DELETE-action query. ``log_update`` would bury the untie among quantity
    edits. ``extra_data`` records that the tombstone is ``status``, not ``is_deleted``,
    so the row's retention (required for the ledger back-reference) is explicit.
    """
    work_order = _load_work_order(db, work_order_id, company_id)
    allocation = _load_allocation(db, work_order_id, allocation_id, company_id)

    if allocation.status == AllocationStatus.CANCELLED:
        # Idempotent untie: already cancelled, nothing to do and nothing to audit.
        return _serialize(db, allocation, company_id)

    consumed_net = net_consumed_quantity_for_allocation(db, allocation_id=allocation.id, company_id=company_id)
    if consumed_net > CONSUMPTION_EPSILON:
        # The remedy is a single verb rather than an impossibility: return_and_untie gives
        # the material back to its source lots AND cancels the tie in one transaction,
        # which is exactly what a caller reaching this 409 is asking for. Untie stays
        # refused here on its own terms.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{round(consumed_net, 6)} {allocation.unit_of_measure} of material is still issued "
                "against this allocation. Return the material with intent 'return_and_untie', which "
                "credits it back to its source lots and closes this tie in one step."
            ),
        )

    part = tenant_query(db, Part, company_id).filter(Part.id == allocation.part_id).first()
    old_status = allocation.status
    allocation.status = AllocationStatus.CANCELLED
    db.flush()
    audit.log_delete(
        "work_order_material_allocation",
        allocation.id,
        f"WO {work_order.work_order_number} / part {part.part_number if part else allocation.part_id}",
        old_values={"status": old_status.value, "qty_consumed": allocation.qty_consumed},
        description=(
            f"Untied part {part.part_number if part else allocation.part_id} from work order "
            f"{work_order.work_order_number}"
        ),
        soft_delete=True,
        extra_data={
            "work_order_id": work_order.id,
            "work_order_operation_id": allocation.work_order_operation_id,
            "part_id": allocation.part_id,
            "new_status": AllocationStatus.CANCELLED.value,
            "tombstone": "status",
            # The figure this untie was actually authorized against, and the test that
            # was applied to it. The guard moved from the qty_consumed CACHE to the
            # signed ledger net, so a tie can now be untied while ISSUE and RETURN rows
            # still carry its allocation_id — an auditor asking "why was this permitted
            # when ledger rows exist?" needs the answer on the chain, not inferable only
            # by re-deriving it from the ledger months later.
            "guard_basis": "signed_ledger_net",
            "ledger_net_issued": consumed_net,
        },
    )
    db.commit()
    db.refresh(allocation)
    return _serialize(db, allocation, company_id)


@router.get(
    "/{work_order_id}/material-allocations/{allocation_id}/consumption",
    response_model=List[MaterialConsumptionLine],
    summary="Per-lot ledger position of a material tie (issued / returned / net)",
)
def get_material_allocation_consumption(
    work_order_id: int,
    allocation_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    company_id: int = Depends(get_current_company_id),
):
    """Where this tie's material came from, and how much of each lot is still out.

    Read-only and open to any authenticated tenant user, like the tie list — it discloses
    ledger facts about material this company already owns, and a return dialog that
    cannot show them would be asking for a confirmation nobody could give.

    One line per source lot: ``issued`` and ``returned`` totals from the ledger, and
    ``net`` = the quantity that lot can still take back. Ordered newest source lot first,
    which is the order a return credits in. There is no lot to choose — material goes
    back where it came from — so this is a disclosure, not a picker.

    Answers from ``inventory_transactions``, never from the tie's ``qty_consumed``
    cache, and works on a CANCELLED tie (whose consumption is exactly what an operator
    most often needs to see). Nothing here writes: a read is not an actor and records no
    reason.
    """
    _load_work_order(db, work_order_id, company_id)
    allocation = _load_allocation(db, work_order_id, allocation_id, company_id)
    return _consumption_lines(db, allocation, company_id)


@router.post(
    "/{work_order_id}/material-allocations/{allocation_id}/return",
    response_model=MaterialReturnResponse,
    summary="Return consumed material to its source lots (reasoned, audited)",
)
def return_material_allocation(
    work_order_id: int,
    allocation_id: int,
    payload: MaterialReturnRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Put consumed material back on the lots it came off — the only un-consume there is.

    Consumption never auto-reverses (invariant 6b), because the consume path also runs
    from a reconcile-on-read GET where there is no actor, no intent and no reason to
    record. This is that same reversal with all three attached: the compensating
    ``RETURN`` transaction + required reason + audit pattern the receiving corrections
    established. Historical rows are never touched — every credit is an APPENDED row.

    **Two named intents, and nothing in between** (``MaterialReturnIntent``):

    * ``correct_over_consumption`` — the tie stays OPEN, so the return is bounded by
      ``qty_consumed - live target``. That bound is the engine's own arithmetic: below
      it, the next completion **or the next reconcile-on-read GET** re-consumes what was
      just returned, re-running FIFO and possibly crediting a different lot than the
      material came from.
    * ``return_and_untie`` — give everything back and CANCEL the tie in the same
      transaction, so no OPEN row survives for the engine to draw against. ``quantity``
      must equal the full ``qty_consumed``; the mismatch 422 catches a stale client
      rather than returning a different amount than the operator was looking at.

    Allowed on a CANCELLED tie: a consumed-then-cancelled tie is a real state (a
    work-order soft delete cancels open ties regardless of consumption) and is exactly
    what the hard-delete 409 points at, so refusing there would leave that refusal with
    no self-service path. A SOFT-DELETED work order is still 404 here, like every other
    verb on this router — restore the work order (an audited verb that also re-opens the
    ties the delete cancelled) and then return, rather than moving stock against a job
    that is currently deleted.

    RBAC is the router's write gate (ADMIN / MANAGER / SUPERVISOR) and deliberately
    stays outside the kiosk path fence: moving stock back with a reason is a stronger
    power than tying it, not a weaker one.

    Status codes — the service decides each one and the ``detail`` is returned verbatim
    (the client is non-optimistic and renders exactly what the server says):

    * **404** — work order or tie not on this company / work order (never 403).
    * **422** — blank reason (FastAPI's own validation, from the schema), non-positive
      quantity, nothing consumed, more than ``qty_consumed``, a
      ``correct_over_consumption`` past the live bound (the detail names
      ``return_and_untie``), or a ``return_and_untie`` that is not the full quantity.
    * **409** — the ledger has less returnable than asked, or a source lot is gone or is
      a placeholder row. Receiving's "409 rather than guess" posture.

    Concurrency: the service takes ``SELECT ... FOR UPDATE`` on the operation then the
    work order (its ``_lock_return_scope``, the completion paths' order) BEFORE computing
    the bound, so a completion landing mid-request cannot raise the target underneath the
    check. It is deliberately not re-taken here — a second lock in the opposite order is
    how deadlocks are built.

    A returned tie does NOT unlock a nest re-import. That guard reads the ledger, not the
    cache, and the ISSUE **and** RETURN rows both still name the operations a rebuild
    would delete; the remedy stays "raise a new work order".

    **What a full return leaves behind differs by TIE SCOPE, and the difference is now a
    decision.** Until this was written down it was neither — it fell out of two guards
    having been keyed differently by accident:

    * an **operation-scoped** tie, fully returned, nets to zero in the ledger. Once
      ``Part.backflush_components`` is exposed, the BOM backflush is then free to issue
      that part again (``completion_inventory_service._drop_ledger_covered_parts``). That
      is the right answer: the material physically came back, the job holds none of it,
      and the BOM's demand is once again unmet. Suppressing forever would refuse to
      consume material the shop is standing next to, and would hide the gap from the
      shortage machinery that exists to surface it.
    * a **work-order-scoped** tie, fully returned, nets to zero too **as of PR 4.4** —
      its ISSUE rows now post under ``reference_type='work_order_backflush'``, outside
      ``uq_wo_inventory_issue``, and the leg reconciles ``qty_planned`` against
      ``net_consumed_quantity_for_allocation``. So the ARITHMETIC no longer refuses a
      re-draw the way the index did. It does not follow that one happens: **there is no
      next completion.** That leg runs exactly once per work-order lifetime -- every
      operation-completion handler refuses a terminal parent, ``complete_work_order``
      early-returns for COMPLETE/CLOSED, reconcile-on-read strips terminal work orders,
      and terminal -> non-terminal is blocked. So, as with the bullet above: once a
      re-entry trigger exists (PR 4.5 or later), a fully-returned open tie would be
      re-drawn exactly as an operation-scoped one is; today nothing re-enters to draw it.
      (``return_and_untie`` CANCELs the tie in the same transaction anyway, precisely so
      that cannot happen behind the operator's back; ``correct_over_consumption`` is
      bounded so it cannot leave a live tie below its target.)

      The asymmetry survives only for **LEGACY work orders** — those carrying a pre-4.4
      ``('work_order', ISSUE)`` row. There the part is un-issuable on that work order
      forever: the row survives the return, ``_component_already_issued`` keeps firing,
      and ``uq_wo_inventory_issue`` makes a second issue on that shape physically
      unavailable at any price. A guard that netted returns for those rows would attempt
      the re-issue, lose to the index, and claim a consumption that never posted — worse
      than refusing.

    The remedy in that legacy case is the first bullet: tie at the OPERATION level, which
    posts outside that index. ``POST`` returns 409 with exactly that wording when a
    work-order-scoped tie is created on a part carrying such a row, and a return does not
    change that refusal.
    """
    work_order = _load_work_order(db, work_order_id, company_id)
    allocation = _load_allocation(db, work_order_id, allocation_id, company_id)

    try:
        result = return_tied_material(
            db,
            work_order,
            allocation,
            quantity=payload.quantity,
            intent=payload.intent,
            reason=payload.reason,
            user_id=current_user.id,
            company_id=company_id,
            audit=audit,
        )
    except MaterialReturnRefused as exc:
        # The service carries the status with the refusal so the 422 ("ask differently")
        # / 409 ("the ledger cannot express this") split cannot drift from the reasons
        # that produced it. Nothing was written — every refusal fires before the first
        # ledger row (see ``_plan_material_return``) — so the rollback is a formality
        # that keeps the session clean for the dependency teardown.
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    # One unit of work: the RETURN rows, the on-hand moves, the tie's qty_consumed, the
    # cancel on an untie, and every audit row commit together or not at all.
    db.commit()
    return MaterialReturnResponse(
        allocation_id=result.allocation_id,
        work_order_id=result.work_order_id,
        part_id=result.part_id,
        part_number=result.part_number,
        intent=result.intent,
        unit_of_measure=result.unit_of_measure,
        quantity_returned=result.quantity_returned,
        qty_consumed_before=result.qty_consumed_before,
        qty_consumed=result.qty_consumed,
        status=result.status,
        returned_lots=[
            MaterialReturnLot(
                inventory_item_id=lot.inventory_item_id,
                lot_number=lot.lot_number,
                quantity=lot.quantity,
                unit_cost=lot.unit_cost,
                transaction_id=lot.transaction_id,
                compensated_transaction_id=lot.compensated_transaction_id,
            )
            for lot in result.returned_lots
        ],
    )
