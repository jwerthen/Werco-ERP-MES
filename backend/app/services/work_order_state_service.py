"""Shared work-order state rules used by office and shop-floor flows."""

import math
from typing import Optional

from sqlalchemy import and_
from sqlalchemy.orm import Session

from app.models.work_order import (
    OperationStatus,
    WorkOrder,
    WorkOrderOperation,
)


class WorkOrderStateError(ValueError):
    """Raised when a requested work-order transition is not valid."""


def operation_target_quantity(
    operation: Optional[WorkOrderOperation],
    work_order: Optional[WorkOrder] = None,
) -> float:
    """Quantity required for an operation, including component operation targets."""
    if (
        operation
        and operation.component_quantity
        and float(operation.component_quantity) > 0
    ):
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
        query = query.filter(
            WorkOrderOperation.work_center_id != current_work_center_id
        )
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
            min(100.0, max(0.0, (quantity_complete / quantity_ordered) * 100.0))
            if quantity_ordered > 0
            else 0.0
        )
        return {
            "operation_count": 0,
            "operations_complete": 0,
            "operation_progress_percent": round(progress_percent, 1),
        }

    progress_by_key: dict[tuple, float] = {}
    for operation in operations:
        key = _operation_progress_key(operation)
        target_qty = operation_target_quantity(operation, work_order)
        complete_qty = float(operation.quantity_complete or 0)

        if _operation_has_completion_evidence(operation):
            ratio = 1.0
        elif target_qty > 0:
            ratio = min(1.0, max(0.0, complete_qty / target_qty))
        else:
            ratio = 0.0

        progress_by_key[key] = max(progress_by_key.get(key, 0.0), ratio)

    total_operations = len(progress_by_key)
    operations_complete = sum(1 for ratio in progress_by_key.values() if ratio >= 1.0)
    progress_total = sum(progress_by_key.values())
    return {
        "operation_count": total_operations,
        "operations_complete": operations_complete,
        "operation_progress_percent": round(
            (progress_total / total_operations) * 100.0,
            1,
        ),
    }


def _operation_progress_key(operation: WorkOrderOperation) -> tuple:
    name = " ".join((operation.name or "").strip().lower().split())
    return (
        operation.work_center_id,
        operation.component_part_id,
        operation.operation_group,
        name or operation.operation_number or operation.sequence or operation.id,
    )


def _operation_has_completion_evidence(operation: WorkOrderOperation) -> bool:
    return (
        operation.status == OperationStatus.COMPLETE
        or (
            operation.actual_end is not None
            and operation.completed_by is not None
        )
    )


def validate_operation_quantity(quantity_complete: float, target_qty: float) -> None:
    if math.isnan(quantity_complete) or math.isinf(quantity_complete):
        raise WorkOrderStateError("Quantity must be a valid number")
    if quantity_complete < 0:
        raise WorkOrderStateError("Quantity cannot be negative")
    if target_qty <= 0:
        raise WorkOrderStateError("Operation quantity ordered is missing or invalid")
    if quantity_complete > target_qty:
        raise WorkOrderStateError(
            f"Quantity ({quantity_complete}) cannot exceed quantity ordered ({target_qty})"
        )
