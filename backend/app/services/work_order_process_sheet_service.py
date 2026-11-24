"""Process sheets on work orders whose operations the planner supplied (wo_change/2).

``POST /work-orders`` has two ways to build a job's operations. The routed path
(``auto_routing`` with no ``operations``) copies the part's released routing and snapshots
every attached process sheet onto the new operations. The supplied path -- New Work Order
sends its own list the moment a planner edits a routed step, and always for a restored
form draft -- used to build those rows with NO snapshot at all, so the required checks
never reached the kiosk and the completion gate passed freely.

This module is what lets the supplied path keep them, without a second copy of the
snapshot rule:

* ``released_routing_steps`` -- the routing operations the routed path would copy for a
  part, in the order and with the sequence / operation number it would assign them. It
  is the ONE reader behind ``GET /work-orders/preview-operations`` (so every preview row
  carries the ``routing_operation_id`` the form sends back) and behind the validation
  below, so a link is checked against exactly the set the planner was shown.
* ``link_supplied_operations`` -- matches each supplied row to its routing operation BY
  ID (never by sequence or name), refuses a link that is not on the part's released
  routing (409, every bad row named, nothing written), and hands back the (operation,
  process_sheet_id) pairs for ``process_sheet_service.snapshot_steps_for_work_order`` --
  the same snapshot the routed path runs, so a family with no released revision is the
  same 409 and rolls the whole creation back. It also names every sheet-bearing step the
  planner left out, for the creation audit row (owner default: a removed step is allowed,
  warned on screen and recorded).
* ``process_sheet_gaps`` -- the read-only Quality worklist: live work orders whose part's
  released routing carries process sheets but whose operations hold no snapshotted step.

Nothing here writes outside the caller's unit of work, commits, or writes audit rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from fastapi import HTTPException
from sqlalchemy import exists
from sqlalchemy.orm import Session, selectinload

from app.db.tenant_filter import tenant_query
from app.models.bom import BOM
from app.models.part import Part, PartType
from app.models.process_sheet import ProcessSheet, WOOperationStep
from app.models.routing import Routing, RoutingOperation
from app.models.work_center import WorkCenter
from app.models.work_order import WorkOrder, WorkOrderOperation, WorkOrderStatus, WorkOrderType
from app.services import process_sheet_service

ROUTING_OPERATION_UNMATCHED = "ROUTING_OPERATION_UNMATCHED"

# Reasons a supplied ``routing_operation_id`` is refused (the 409's per-row ``reason``).
REASON_NOT_FOUND = "not_found"
REASON_NOT_ON_RELEASED_ROUTING = "not_on_released_routing"
REASON_COMPONENT_MISMATCH = "component_mismatch"

# Work orders that are finished with: a gap on one of these is history, not a worklist row.
TERMINAL_WORK_ORDER_STATUSES = (
    WorkOrderStatus.COMPLETE,
    WorkOrderStatus.CLOSED,
    WorkOrderStatus.CANCELLED,
)


@dataclass(frozen=True)
class RoutedStep:
    """One routing operation the routed create path would copy onto a work order."""

    routing_operation: RoutingOperation
    work_center: Optional[WorkCenter]
    # The BOM component whose routing this step belongs to; None for the job part's own routing.
    component_part: Optional[Part]
    component_quantity: float
    # What the routed path would assign the copied row.
    sequence: int
    operation_number: str
    name: str
    process_sheet_id: Optional[int]
    process_sheet_number: Optional[str]
    # None when not resolved (``resolve_release=False``) or when no sheet is attached.
    process_sheet_released: Optional[bool]

    @property
    def routing_operation_id(self) -> int:
        return int(self.routing_operation.id)


def _released_routings_by_part(db: Session, company_id: int, part_ids: Iterable[int]) -> Dict[int, Routing]:
    ids = {int(pid) for pid in part_ids if pid}
    if not ids:
        return {}
    routings = (
        tenant_query(db, Routing, company_id)
        .options(selectinload(Routing.operations).selectinload(RoutingOperation.work_center))
        .filter(
            Routing.part_id.in_(ids),
            Routing.is_active == True,  # noqa: E712
            Routing.status == "released",
            Routing.is_deleted == False,  # noqa: E712
        )
        .order_by(Routing.id.asc())
        .all()
    )
    by_part: Dict[int, Routing] = {}
    for routing in routings:
        # ``.first()`` semantics of the create path: the lowest id wins on a duplicate release.
        by_part.setdefault(int(routing.part_id), routing)
    return by_part


def released_routing_steps(
    db: Session,
    part: Part,
    company_id: int,
    quantity: float = 1.0,
    *,
    resolve_release: bool = True,
) -> List[RoutedStep]:
    """The routing operations the routed create path would copy for ``part``, in its order.

    Mirrors ``create_routing_operations_for_work_order``: an assembly (part type assembly,
    or any part with an active BOM) takes each BOM component's released routing once, in
    BOM order, then its own routing with inspection operations last, renumbered 10, 20, ...;
    any other part takes its own released routing at the routing's own sequence and
    operation number. Inactive routing operations are never copied.
    """
    # Lazy: the router imports this module at load time (the duplicate service's precedent).
    from app.api.endpoints.work_orders import _collect_bom_components, _get_active_bom, _is_inspection_operation

    bom = _get_active_bom(db, part.id, company_id)
    is_assembly = part.part_type == PartType.ASSEMBLY or bom is not None
    rows: List[Tuple[RoutingOperation, Optional[Part], float, int, str, str]] = []

    if is_assembly:
        sequence = 10
        component_items = _collect_bom_components(db, bom, company_id) if bom else []
        routings = _released_routings_by_part(
            db, company_id, [component.id for _, component, _ in component_items] + [part.id]
        )
        quantity_by_component: Dict[int, float] = {}
        for _, component, qty_per in component_items:
            quantity_by_component[component.id] = quantity_by_component.get(component.id, 0.0) + (
                float(qty_per or 0) * float(quantity or 0)
            )
        seen: set = set()
        for _, component, _ in component_items:
            if component.id in seen:
                continue
            seen.add(component.id)
            routing = routings.get(int(component.id))
            if routing is None:
                continue
            for rop in sorted(routing.operations, key=lambda op: op.sequence):
                if not rop.is_active:
                    continue
                rows.append(
                    (
                        rop,
                        component,
                        quantity_by_component.get(component.id, 0.0),
                        sequence,
                        str(sequence),
                        f"{component.part_number} - {rop.name}",
                    )
                )
                sequence += 10
        own = routings.get(int(part.id))
        if own is not None:
            active = [op for op in sorted(own.operations, key=lambda op: op.sequence) if op.is_active]
            ordered = [op for op in active if not _is_inspection_operation(op)] + [
                op for op in active if _is_inspection_operation(op)
            ]
            for rop in ordered:
                rows.append((rop, None, float(quantity or 0), sequence, str(sequence), rop.name))
                sequence += 10
    else:
        own = _released_routings_by_part(db, company_id, [part.id]).get(int(part.id))
        if own is not None:
            for rop in sorted(own.operations, key=lambda op: op.sequence):
                if not rop.is_active:
                    continue
                rows.append(
                    (
                        rop,
                        None,
                        float(quantity or 0),
                        int(rop.sequence),
                        rop.operation_number or str(rop.sequence),
                        rop.name,
                    )
                )

    sheet_ids = {int(rop.process_sheet_id) for rop, *_ in rows if rop.process_sheet_id}
    sheets_by_id: Dict[int, ProcessSheet] = {}
    if sheet_ids:
        sheets_by_id = {
            int(sheet.id): sheet
            for sheet in tenant_query(db, ProcessSheet, company_id).filter(ProcessSheet.id.in_(sheet_ids)).all()
        }
    released_by_number: Dict[str, bool] = {}

    steps: List[RoutedStep] = []
    for rop, component, component_quantity, sequence, operation_number, name in rows:
        sheet = sheets_by_id.get(int(rop.process_sheet_id)) if rop.process_sheet_id else None
        released: Optional[bool] = None
        if sheet is not None and resolve_release:
            if sheet.sheet_number not in released_by_number:
                released_by_number[sheet.sheet_number] = (
                    process_sheet_service.resolve_released_revision(db, company_id, sheet.sheet_number) is not None
                )
            released = released_by_number[sheet.sheet_number]
        elif rop.process_sheet_id and sheet is None and resolve_release:
            # Attached to a row this company cannot resolve: the snapshot would 409 on it.
            released = False
        steps.append(
            RoutedStep(
                routing_operation=rop,
                work_center=rop.work_center,
                component_part=component,
                component_quantity=component_quantity,
                sequence=sequence,
                operation_number=operation_number,
                name=name,
                process_sheet_id=int(rop.process_sheet_id) if rop.process_sheet_id else None,
                process_sheet_number=sheet.sheet_number if sheet is not None else None,
                process_sheet_released=released,
            )
        )
    return steps


def _removed_step_entry(step: RoutedStep) -> Dict[str, Any]:
    return {
        "routing_operation_id": step.routing_operation_id,
        "operation_number": step.operation_number,
        "operation_sequence": step.sequence,
        "name": step.name,
        "component_part_id": step.component_part.id if step.component_part is not None else None,
        "component_part_number": step.component_part.part_number if step.component_part is not None else None,
        "process_sheet_id": step.process_sheet_id,
        "sheet_number": step.process_sheet_number,
    }


@dataclass
class SuppliedLinkResult:
    """What ``link_supplied_operations`` hands back to ``create_work_order``."""

    # (operation, attached process_sheet_id) for snapshot_steps_for_work_order.
    operation_sheet_pairs: List[Tuple[WorkOrderOperation, Optional[int]]]
    linked_routing_operation_ids: List[int]
    # Sheet-bearing routing steps the routed path would have copied that no supplied row links to.
    removed_process_sheet_steps: List[Dict[str, Any]]


def link_supplied_operations(
    db: Session,
    part: Part,
    company_id: int,
    quantity: float,
    supplied: Sequence[Tuple[WorkOrderOperation, Optional[int]]],
) -> SuppliedLinkResult:
    """Match supplied operations to their routing operations by id and validate every link.

    ``supplied`` is (new operation, the row's ``routing_operation_id`` or None). A row with
    no link is a planner-added step and snapshots nothing. A linked row must name an
    operation on the part's released routing (or a BOM component's), and its
    ``component_part_id`` must agree with whose routing that is; otherwise the whole
    creation is refused 409 ``ROUTING_OPERATION_UNMATCHED`` naming every bad row -- a
    stale id from a restored draft is never silently dropped and never re-matched by
    sequence or name. Raised BEFORE the snapshot, so nothing about the job is kept.
    """
    expected = released_routing_steps(db, part, company_id, quantity, resolve_release=False)
    expected_by_id: Dict[int, RoutedStep] = {step.routing_operation_id: step for step in expected}

    problems: List[Dict[str, Any]] = []
    pairs: List[Tuple[WorkOrderOperation, Optional[int]]] = []
    linked_ids: List[int] = []
    unknown_ids = {int(rop_id) for _, rop_id in supplied if rop_id is not None and int(rop_id) not in expected_by_id}
    resolvable_unknown: set = set()
    if unknown_ids:
        resolvable_unknown = {
            int(row.id)
            for row in tenant_query(db, RoutingOperation, company_id).filter(RoutingOperation.id.in_(unknown_ids)).all()
        }

    for operation, rop_id in supplied:
        if rop_id is None:
            continue
        rop_id = int(rop_id)
        step = expected_by_id.get(rop_id)
        reason: Optional[str] = None
        if step is None:
            # Missing and another company's row read the same: never confirm a foreign id exists.
            reason = REASON_NOT_ON_RELEASED_ROUTING if rop_id in resolvable_unknown else REASON_NOT_FOUND
        else:
            supplied_component = operation.component_part_id
            if step.component_part is not None:
                if supplied_component != step.component_part.id:
                    reason = REASON_COMPONENT_MISMATCH
            elif supplied_component not in (None, part.id):
                reason = REASON_COMPONENT_MISMATCH
        if reason is not None:
            problems.append(
                {
                    "sequence": operation.sequence,
                    "operation_number": str(operation.operation_number or operation.sequence),
                    "routing_operation_id": rop_id,
                    "reason": reason,
                }
            )
            continue
        if step is None:  # unreachable: a missing step always set ``reason`` above
            continue
        linked_ids.append(rop_id)
        pairs.append((operation, step.process_sheet_id))

    if problems:
        names = ", ".join(problem["operation_number"] for problem in problems)
        raise HTTPException(
            status_code=409,
            detail={
                "code": ROUTING_OPERATION_UNMATCHED,
                "detail": (
                    f"Cannot create work order: operation(s) {names} are linked to a routing step that is not on "
                    "this part's released routing. Reload the routing, or remove the link from those operations, "
                    "and try again."
                ),
                "operations": problems,
            },
        )

    linked = set(linked_ids)
    removed = [
        _removed_step_entry(step)
        for step in expected
        if step.process_sheet_id is not None and step.routing_operation_id not in linked
    ]
    return SuppliedLinkResult(
        operation_sheet_pairs=pairs,
        linked_routing_operation_ids=linked_ids,
        removed_process_sheet_steps=removed,
    )


def _parts_with_sheet_bearing_routing(db: Session, company_id: int) -> set:
    rows = (
        tenant_query(db, Routing, company_id)
        .join(RoutingOperation, RoutingOperation.routing_id == Routing.id)
        .filter(
            Routing.is_active == True,  # noqa: E712
            Routing.status == "released",
            Routing.is_deleted == False,  # noqa: E712
            RoutingOperation.company_id == company_id,
            RoutingOperation.is_active == True,  # noqa: E712
            RoutingOperation.process_sheet_id.isnot(None),
        )
        .with_entities(Routing.part_id)
        .distinct()
        .all()
    )
    return {int(part_id) for (part_id,) in rows}


def _parts_with_active_bom(db: Session, company_id: int, part_ids: Iterable[int]) -> set:
    ids = {int(pid) for pid in part_ids}
    if not ids:
        return set()
    rows = (
        tenant_query(db, BOM, company_id)
        .filter(
            BOM.part_id.in_(ids),
            BOM.is_active == True,  # noqa: E712
            BOM.is_deleted == False,  # noqa: E712
        )
        .with_entities(BOM.part_id)
        .distinct()
        .all()
    )
    return {int(part_id) for (part_id,) in rows}


def process_sheet_gaps(db: Session, company_id: int) -> List[Dict[str, Any]]:
    """Live work orders whose part's released routing carries process sheets, holding none.

    "Live" = not soft-deleted and not complete / closed / cancelled. Laser-cutting work
    orders are left out: their operations are nests, never a copy of the part's routing.
    "Carries process sheets" uses the same routing set the create path copies
    (``released_routing_steps``: the part's own routing and, for an assembly, its BOM
    components'), evaluated against the routing as it is TODAY -- so a job created before
    a sheet was attached to its routing is listed too, which is exactly the case Quality
    has to decide on. Pure read: no write, no audit row, no event.
    """
    has_snapshot = exists().where(
        WOOperationStep.company_id == company_id,
        WOOperationStep.work_order_operation_id == WorkOrderOperation.id,
        WorkOrderOperation.work_order_id == WorkOrder.id,
        WorkOrderOperation.company_id == company_id,
    )
    candidates: List[WorkOrder] = (
        tenant_query(db, WorkOrder, company_id)
        .options(selectinload(WorkOrder.part), selectinload(WorkOrder.operations))
        .filter(
            WorkOrder.is_deleted == False,  # noqa: E712
            WorkOrder.status.notin_(TERMINAL_WORK_ORDER_STATUSES),
            WorkOrder.work_order_type != WorkOrderType.LASER_CUTTING.value,
            WorkOrder.part_id.isnot(None),
            ~has_snapshot,
        )
        .order_by(WorkOrder.due_date.is_(None), WorkOrder.due_date.asc(), WorkOrder.id.asc())
        .all()
    )
    if not candidates:
        return []

    # Cheap pre-filter before the per-part routing walk: a part can only carry a sheet
    # through its own released routing or, when it has an active BOM, a component's.
    direct = _parts_with_sheet_bearing_routing(db, company_id)
    if not direct:
        return []
    candidate_part_ids = {int(wo.part_id) for wo in candidates}
    with_bom = _parts_with_active_bom(db, company_id, candidate_part_ids)
    worth_walking = {pid for pid in candidate_part_ids if pid in direct or pid in with_bom}

    steps_by_part: Dict[int, List[RoutedStep]] = {}
    items: List[Dict[str, Any]] = []
    for wo in candidates:
        part_id = int(wo.part_id)
        if part_id not in worth_walking or wo.part is None or wo.part.company_id != company_id:
            continue
        if part_id not in steps_by_part:
            steps_by_part[part_id] = [
                step
                for step in released_routing_steps(db, wo.part, company_id, resolve_release=False)
                if step.process_sheet_id is not None
            ]
        sheet_steps = steps_by_part[part_id]
        if not sheet_steps:
            continue
        items.append(
            {
                "work_order_id": wo.id,
                "work_order_number": wo.work_order_number,
                "status": wo.status.value if hasattr(wo.status, "value") else wo.status,
                "part_id": part_id,
                "part_number": wo.part.part_number,
                "part_name": wo.part.name,
                "quantity_ordered": float(wo.quantity_ordered or 0),
                "due_date": wo.due_date,
                "customer_name": wo.customer_name,
                "created_at": wo.created_at,
                "operation_count": len([op for op in wo.operations if op.company_id == company_id]),
                "expected_process_sheet_steps": [_removed_step_entry(step) for step in sheet_steps],
            }
        )
    return items
