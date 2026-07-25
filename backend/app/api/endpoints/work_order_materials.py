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

Nothing here posts inventory. Consumption rides the existing completion call sites via
``apply_completion_inventory_effects``; these endpoints only manage the planning row.

RBAC: reads are open to any authenticated tenant user; every mutating verb requires
ADMIN / MANAGER / SUPERVISOR. Deliberately NOT under ``/api/v1/shop-floor`` — the kiosk
path fence (``deps.py``) exists to keep operator-scoped tokens off office endpoints, and
tying material is an office/planning action.
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_audit_service, get_current_active_user, get_current_company_id, require_role
from app.db.database import get_db
from app.db.tenant_filter import tenant_query
from app.models.inventory import InventoryItem
from app.models.part import Part
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder, WorkOrderOperation
from app.models.work_order_material import AllocationStatus, WorkOrderMaterialAllocation
from app.schemas.work_order_material import (
    MaterialAllocationCreate,
    MaterialAllocationResponse,
    MaterialAllocationUpdate,
)
from app.services.audit_service import AuditService
from app.services.material_consumption_service import (
    CONSUMPTION_EPSILON,
    is_consumable_item,
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


def _display_maps(
    db: Session, allocations: List[WorkOrderMaterialAllocation], company_id: int
) -> tuple[dict[int, Part], dict[int, Optional[str]]]:
    """Batch the two display lookups ``_serialize`` needs, tenant-scoped.

    One SELECT for the parts and one for the operations, whatever the row count. Doing
    them per row is an N+1 that scales with the number of ties on a work order — cheap
    today (ties ship dark) and exactly the sort of thing that is never revisited once the
    UI lands in PR 2.
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
    return parts, operation_numbers


def _serialize(
    db: Session,
    allocation: WorkOrderMaterialAllocation,
    company_id: int,
    parts: Optional[dict[int, Part]] = None,
    operation_numbers: Optional[dict[int, Optional[str]]] = None,
) -> MaterialAllocationResponse:
    """One tie as its response schema.

    ``parts`` / ``operation_numbers`` are the batched maps from ``_display_maps``; pass
    them from any multi-row caller. Omitted (the single-row verbs) they are built for
    this one row, which is the same two queries the per-row version always issued.
    """
    if parts is None or operation_numbers is None:
        parts, operation_numbers = _display_maps(db, [allocation], company_id)
    part = parts.get(allocation.part_id)
    operation_number: Optional[str] = None
    if allocation.work_order_operation_id is not None:
        operation_number = operation_numbers.get(allocation.work_order_operation_id)
    return MaterialAllocationResponse(
        id=allocation.id,
        work_order_id=allocation.work_order_id,
        work_order_operation_id=allocation.work_order_operation_id,
        operation_number=operation_number,
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
    """
    _load_work_order(db, work_order_id, company_id)
    query = tenant_query(db, WorkOrderMaterialAllocation, company_id).filter(
        WorkOrderMaterialAllocation.work_order_id == work_order_id
    )
    if not include_inactive:
        query = query.filter(WorkOrderMaterialAllocation.status == AllocationStatus.OPEN)
    allocations = query.order_by(WorkOrderMaterialAllocation.id).all()
    parts, operation_numbers = _display_maps(db, allocations, company_id)
    return [_serialize(db, allocation, company_id, parts, operation_numbers) for allocation in allocations]


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
    pin into the one-shot backflush ISSUE. A held lot is refused here (422); a lot held
    AFTER pinning still consumes and writes ``HELD_MATERIAL_CONSUMED``.

    Refused with **409** when the tie could never consume:

    * the work order is TERMINAL (complete / closed / cancelled) — nothing will drive a
      completion through the consumption engine again; and
    * a work-order-scoped tie whose part was ALREADY issued to this work order — the
      one-shot backflush skips an already-issued component and ``uq_wo_inventory_issue``
      forbids the second ISSUE row, so the tie would sit OPEN at ``qty_consumed`` 0
      forever.
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
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Part {part.part_number} was already issued to work order {work_order.work_order_number}; "
                "this tie could never consume — reverse the issue, or tie at the operation level instead."
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
      Reverse the consumption first (a later PR) rather than editing the plan under it.
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
    if payload.qty_planned is not None and payload.qty_planned < consumed:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"qty_planned cannot be lowered to {payload.qty_planned}: {consumed} "
                f"{allocation.unit_of_measure} has already been consumed against this allocation. "
                "Reverse consumption first."
            ),
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
        if allocation.work_order_operation_id is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="qty_per_run applies to operation-scoped ties only.",
            )
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

    Refused with 409 once ANY material has been consumed. Cancelling a tie that already
    moved stock would strand ``inventory_transactions.allocation_id`` rows against a
    tombstone with no explanation of where the material went; the reversal (RETURN) is a
    separate, reasoned verb.

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

    if float(allocation.qty_consumed or 0) > CONSUMPTION_EPSILON:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{allocation.qty_consumed} {allocation.unit_of_measure} has already been consumed "
                "against this allocation. Reverse consumption first."
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
        },
    )
    db.commit()
    db.refresh(allocation)
    return _serialize(db, allocation, company_id)
