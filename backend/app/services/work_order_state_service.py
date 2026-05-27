"""Shared work-order state rules used by office and shop-floor flows."""

import math
from typing import Optional

from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from app.models.time_entry import TimeEntry
from app.models.work_order import (
    OperationStatus,
    WorkOrder,
    WorkOrderOperation,
    WorkOrderStatus,
)


class WorkOrderStateError(ValueError):
    """Raised when a requested work-order transition is not valid."""


def operation_target_quantity(
    operation: Optional[WorkOrderOperation],
    work_order: Optional[WorkOrder] = None,
) -> float:
    """Quantity required for an operation, including component operation targets."""
    if operation and operation.component_quantity and float(operation.component_quantity) > 0:
        return float(operation.component_quantity)
    if work_order and work_order.quantity_ordered:
        return float(work_order.quantity_ordered)
    if operation and operation.work_order and operation.work_order.quantity_ordered:
        return float(operation.work_order.quantity_ordered)
    return 0.0


def has_incomplete_predecessors(
    db: Session,
    work_order_id: int,
    sequence: int,
    current_operation_id: Optional[int] = None,
    current_work_center_id: Optional[int] = None,
    allow_same_work_center: bool = False,
) -> bool:
    query = db.query(WorkOrderOperation).filter(
        and_(
            WorkOrderOperation.work_order_id == work_order_id,
            WorkOrderOperation.sequence < sequence,
            WorkOrderOperation.status != OperationStatus.COMPLETE,
        )
    )
    if current_operation_id is not None:
        query = query.filter(WorkOrderOperation.id != current_operation_id)
    if allow_same_work_center and current_work_center_id is not None:
        query = query.filter(WorkOrderOperation.work_center_id != current_work_center_id)
    return query.count() > 0


def release_first_ready_operation(
    work_order: WorkOrder,
) -> Optional[WorkOrderOperation]:
    if not work_order.operations:
        return None

    first_pending = min(
        (op for op in work_order.operations if op.status == OperationStatus.PENDING),
        key=lambda op: op.sequence,
        default=None,
    )
    if first_pending:
        first_pending.status = OperationStatus.READY
    return first_pending


def release_next_ready_operation(
    db: Session,
    work_order: WorkOrder,
    completed_op: WorkOrderOperation,
) -> Optional[WorkOrderOperation]:
    next_op = (
        db.query(WorkOrderOperation)
        .filter(
            and_(
                WorkOrderOperation.work_order_id == work_order.id,
                WorkOrderOperation.sequence > completed_op.sequence,
                WorkOrderOperation.status == OperationStatus.PENDING,
            )
        )
        .order_by(WorkOrderOperation.sequence)
        .first()
    )
    if next_op:
        next_op.status = OperationStatus.READY
    return next_op


def sync_work_order_quantity_complete(
    work_order: WorkOrder,
    operation: WorkOrderOperation,
    all_operations_complete: bool,
) -> None:
    if all_operations_complete:
        work_order.quantity_complete = float(work_order.quantity_ordered or 0)
    elif not operation.component_part_id:
        work_order.quantity_complete = min(
            float(operation.quantity_complete or 0),
            float(work_order.quantity_ordered or 0),
        )


def work_order_operation_progress(work_order: WorkOrder) -> dict:
    """Return route-progress metrics without changing finished WO quantity.

    Component operations can complete before the parent assembly is finished.
    Those completions should move the progress bar, but they should not be
    counted as finished work-order quantity for shipping or closeout.

    Operation rows can also be regenerated while preserving the same human job
    identity. In that case, count one progress slot per natural operation and
    let an older completed row satisfy the matching current row.
    """
    operations = list(work_order.operations or [])
    if not operations:
        quantity_ordered = float(work_order.quantity_ordered or 0)
        quantity_complete = float(work_order.quantity_complete or 0)
        progress_percent = (
            min(100.0, max(0.0, (quantity_complete / quantity_ordered) * 100.0)) if quantity_ordered > 0 else 0.0
        )
        return {
            "operation_count": 0,
            "operations_complete": 0,
            "operation_progress_percent": round(progress_percent, 1),
        }

    progress_by_key: dict[tuple, float] = {}
    completed_by_key: dict[tuple, bool] = {}
    for operation in operations:
        key = _operation_progress_key(operation)
        target_qty = operation_target_quantity(operation, work_order)
        complete_qty = float(operation.quantity_complete or 0)
        has_completion_evidence = _operation_has_completion_evidence(operation)

        if has_completion_evidence:
            ratio = 1.0
        elif target_qty > 0:
            ratio = min(1.0, max(0.0, complete_qty / target_qty))
        else:
            ratio = 0.0

        progress_by_key[key] = max(progress_by_key.get(key, 0.0), ratio)
        completed_by_key[key] = completed_by_key.get(key, False) or has_completion_evidence

    total_operations = len(progress_by_key)
    operations_complete = sum(1 for is_complete in completed_by_key.values() if is_complete)
    progress_total = sum(progress_by_key.values())
    return {
        "operation_count": total_operations,
        "operations_complete": operations_complete,
        "operation_progress_percent": round(
            (progress_total / total_operations) * 100.0,
            1,
        ),
    }


def reconcile_work_orders_from_completion_evidence(db: Session, work_orders: list[WorkOrder]) -> bool:
    """Repair operation rows from durable shop-floor completion evidence."""
    operations = [op for wo in work_orders for op in (wo.operations or [])]
    operation_ids = [op.id for op in operations if op.id is not None]
    if not operation_ids:
        return False

    changed = False
    produced_by_operation: dict[int, tuple[float, float]] = {}
    for row in (
        db.query(
            TimeEntry.operation_id,
            func.coalesce(func.sum(TimeEntry.quantity_produced), 0).label("quantity_produced"),
            func.coalesce(func.sum(TimeEntry.quantity_scrapped), 0).label("quantity_scrapped"),
        )
        .filter(TimeEntry.operation_id.in_(operation_ids))
        .group_by(TimeEntry.operation_id)
        .all()
    ):
        if row.operation_id is not None:
            produced_by_operation[row.operation_id] = (
                float(row.quantity_produced or 0),
                float(row.quantity_scrapped or 0),
            )

    closed_produced_by_operation: dict[int, float] = {}
    for row in (
        db.query(
            TimeEntry.operation_id,
            func.coalesce(func.sum(TimeEntry.quantity_produced), 0).label("quantity_produced"),
        )
        .filter(TimeEntry.operation_id.in_(operation_ids), TimeEntry.clock_out.isnot(None))
        .group_by(TimeEntry.operation_id)
        .all()
    ):
        if row.operation_id is not None:
            closed_produced_by_operation[row.operation_id] = float(row.quantity_produced or 0)

    latest_entry_by_operation: dict[int, TimeEntry] = {}
    latest_entries = (
        db.query(TimeEntry)
        .filter(TimeEntry.operation_id.in_(operation_ids), TimeEntry.clock_out.isnot(None))
        .order_by(TimeEntry.operation_id, TimeEntry.clock_out.desc())
        .all()
    )
    for entry in latest_entries:
        if entry.operation_id is not None and entry.operation_id not in latest_entry_by_operation:
            latest_entry_by_operation[entry.operation_id] = entry

    for operation in operations:
        produced_qty, scrapped_qty = produced_by_operation.get(operation.id, (0.0, 0.0))
        if produced_qty > float(operation.quantity_complete or 0):
            operation.quantity_complete = produced_qty
            changed = True
        if scrapped_qty > float(operation.quantity_scrapped or 0):
            operation.quantity_scrapped = scrapped_qty
            changed = True
        changed = _sync_operation_status_from_quantity(
            operation,
            latest_entry_by_operation.get(operation.id),
            closed_produced_by_operation.get(operation.id, 0.0) >= operation_target_quantity(operation),
        ) or changed

    for work_order in work_orders:
        changed = _copy_slot_completion_evidence(work_order) or changed
        changed = _sync_work_order_status_from_operations(work_order) or changed

    return changed


def _operation_progress_key(operation: WorkOrderOperation) -> tuple:
    if operation.sequence is not None:
        return ("sequence", int(operation.sequence))
    operation_number = _normalized_operation_number(operation.operation_number)
    if operation_number:
        return ("operation_number", operation_number)
    name = " ".join((operation.name or "").strip().lower().split())
    return (
        operation.work_center_id,
        operation.component_part_id,
        operation.operation_group,
        name or operation.operation_number or operation.sequence or operation.id,
    )


def _operation_has_completion_evidence(operation: WorkOrderOperation) -> bool:
    return operation.status == OperationStatus.COMPLETE or (
        operation.actual_end is not None and operation.completed_by is not None
    )


def _normalized_operation_number(operation_number: Optional[str]) -> Optional[str]:
    if not operation_number:
        return None
    digits = "".join(ch for ch in str(operation_number) if ch.isdigit())
    return digits or " ".join(str(operation_number).strip().lower().split()) or None


def _sync_operation_status_from_quantity(
    operation: WorkOrderOperation,
    latest_entry: Optional[TimeEntry] = None,
    has_closed_completion_evidence: bool = False,
) -> bool:
    target_qty = operation_target_quantity(operation)
    if target_qty <= 0:
        return False

    quantity_complete = float(operation.quantity_complete or 0)
    changed = False
    if operation.status == OperationStatus.COMPLETE:
        if not operation.actual_end and latest_entry:
            operation.actual_end = latest_entry.clock_out
            changed = True
        if not operation.completed_by and latest_entry:
            operation.completed_by = latest_entry.user_id
            changed = True
        if not operation.actual_start and latest_entry:
            operation.actual_start = latest_entry.clock_in
            changed = True
        if not operation.started_by and latest_entry:
            operation.started_by = latest_entry.user_id
            changed = True
    elif quantity_complete >= target_qty and has_closed_completion_evidence:
        operation.status = OperationStatus.COMPLETE
        operation.actual_end = operation.actual_end or (latest_entry.clock_out if latest_entry else None)
        operation.completed_by = operation.completed_by or (latest_entry.user_id if latest_entry else None)
        operation.actual_start = operation.actual_start or (latest_entry.clock_in if latest_entry else None)
        operation.started_by = operation.started_by or (latest_entry.user_id if latest_entry else None)
        changed = True
    elif quantity_complete > 0 and operation.status in (OperationStatus.PENDING, OperationStatus.READY):
        operation.status = OperationStatus.IN_PROGRESS
        operation.actual_start = operation.actual_start or (latest_entry.clock_in if latest_entry else None)
        operation.started_by = operation.started_by or (latest_entry.user_id if latest_entry else None)
        changed = True

    return changed


def _copy_slot_completion_evidence(work_order: WorkOrder) -> bool:
    changed = False
    operations_by_key: dict[tuple, list[WorkOrderOperation]] = {}
    for operation in work_order.operations or []:
        operations_by_key.setdefault(_operation_progress_key(operation), []).append(operation)

    for slot_operations in operations_by_key.values():
        completed_source = next((op for op in slot_operations if _operation_has_completion_evidence(op)), None)
        if not completed_source:
            continue

        for operation in slot_operations:
            target_qty = operation_target_quantity(operation, work_order)
            if target_qty > 0 and float(operation.quantity_complete or 0) < target_qty:
                operation.quantity_complete = target_qty
                changed = True
            if operation.quantity_scrapped is None and completed_source.quantity_scrapped is not None:
                operation.quantity_scrapped = completed_source.quantity_scrapped
                changed = True
            if operation.status != OperationStatus.COMPLETE:
                operation.status = OperationStatus.COMPLETE
                changed = True
            if not operation.actual_end and completed_source.actual_end:
                operation.actual_end = completed_source.actual_end
                changed = True
            if not operation.completed_by and completed_source.completed_by:
                operation.completed_by = completed_source.completed_by
                changed = True
            if not operation.actual_start and completed_source.actual_start:
                operation.actual_start = completed_source.actual_start
                changed = True
            if not operation.started_by and completed_source.started_by:
                operation.started_by = completed_source.started_by
                changed = True

    return changed


def _sync_work_order_status_from_operations(work_order: WorkOrder) -> bool:
    operations = list(work_order.operations or [])
    if not operations:
        return False

    changed = False
    all_operations_complete = all(operation.status == OperationStatus.COMPLETE for operation in operations)
    any_operation_progress = any(
        operation.status in (OperationStatus.IN_PROGRESS, OperationStatus.COMPLETE)
        or float(operation.quantity_complete or 0) > 0
        for operation in operations
    )

    if all_operations_complete:
        if work_order.status not in (WorkOrderStatus.COMPLETE, WorkOrderStatus.CLOSED):
            work_order.status = WorkOrderStatus.COMPLETE
            changed = True
        if not work_order.actual_end:
            completed_dates = [operation.actual_end for operation in operations if operation.actual_end]
            if completed_dates:
                work_order.actual_end = max(completed_dates)
                changed = True
        target_qty = float(work_order.quantity_ordered or 0)
        if target_qty > 0 and float(work_order.quantity_complete or 0) < target_qty:
            work_order.quantity_complete = target_qty
            changed = True
    elif any_operation_progress and work_order.status == WorkOrderStatus.RELEASED:
        work_order.status = WorkOrderStatus.IN_PROGRESS
        changed = True
        started_dates = [operation.actual_start for operation in operations if operation.actual_start]
        if started_dates and not work_order.actual_start:
            work_order.actual_start = min(started_dates)
            changed = True

    return changed


def validate_operation_quantity(quantity_complete: float, target_qty: float) -> None:
    if math.isnan(quantity_complete) or math.isinf(quantity_complete):
        raise WorkOrderStateError("Quantity must be a valid number")
    if quantity_complete < 0:
        raise WorkOrderStateError("Quantity cannot be negative")
    if target_qty <= 0:
        raise WorkOrderStateError("Operation quantity ordered is missing or invalid")
    if quantity_complete > target_qty:
        raise WorkOrderStateError(f"Quantity ({quantity_complete}) cannot exceed quantity ordered ({target_qty})")
