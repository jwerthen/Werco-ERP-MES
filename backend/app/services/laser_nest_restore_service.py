"""Restore a cancelled nest without losing its operation, drawing or run history."""

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.laser_nest import LaserNest
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.models.work_order_blocker import WorkOrderBlocker, WorkOrderBlockerStatus
from app.services.audit_service import AuditService
from app.services.laser_nest_service import _recompute_child_quantity_ordered
from app.services.work_order_state_service import (
    TERMINAL_WO_STATUSES,
    is_laser_dispatch_work_order,
    promote_ready_operations,
)


def restore_laser_nest(db: Session, *, nest_id: int, company_id: int, audit: AuditService) -> LaserNest:
    """Restore with required audit evidence in the caller's transaction; never commit.

    Deleted nests are deliberately readable here. Lock parent, operation, then nest
    and recheck the state so concurrent restores cannot both record a restoration.
    Existing blockers stay open and keep the restored operation on hold.
    """
    link = (
        db.query(WorkOrderOperation.work_order_id, WorkOrderOperation.id)
        .join(LaserNest, LaserNest.work_order_operation_id == WorkOrderOperation.id)
        .filter(
            LaserNest.id == nest_id,
            LaserNest.company_id == company_id,
            WorkOrderOperation.company_id == company_id,
        )
        .first()
    )
    if link is None:
        raise HTTPException(status_code=404, detail="Laser nest or its operation not found")
    work_order = (
        db.query(WorkOrder)
        .filter(WorkOrder.id == link.work_order_id, WorkOrder.company_id == company_id, WorkOrder.is_deleted.is_(False))
        .populate_existing()
        .with_for_update()
        .first()
    )
    if work_order is None:
        raise HTTPException(status_code=404, detail="Work order not found")
    if work_order.status in TERMINAL_WO_STATUSES:
        raise HTTPException(status_code=409, detail="Cannot restore a nest on a finished or cancelled work order")
    if not is_laser_dispatch_work_order(work_order):
        raise HTTPException(status_code=409, detail="The nest must belong to a laser work order")
    operation = (
        db.query(WorkOrderOperation)
        .filter(
            WorkOrderOperation.id == link.id,
            WorkOrderOperation.work_order_id == work_order.id,
            WorkOrderOperation.company_id == company_id,
        )
        .populate_existing()
        .with_for_update()
        .first()
    )
    nest = (
        db.query(LaserNest)
        .filter(LaserNest.id == nest_id, LaserNest.company_id == company_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if operation is None or nest is None or nest.work_order_operation_id != operation.id:
        raise HTTPException(status_code=409, detail="The nest's operation changed; reload the work order")
    if not nest.is_deleted:
        raise HTTPException(status_code=409, detail="This laser nest is already active")
    if operation.status != OperationStatus.ON_HOLD or operation.actual_end is not None:
        raise HTTPException(status_code=409, detail="This nest has completion or status changes that require review")

    old_nest = {
        "is_deleted": True,
        "deleted_at": nest.deleted_at.isoformat() if nest.deleted_at else None,
        "deleted_by": nest.deleted_by,
    }
    old_quantity = float(work_order.quantity_ordered)
    old_status = operation.status.value
    has_blocker = (
        db.query(WorkOrderBlocker.id)
        .filter(
            WorkOrderBlocker.company_id == company_id,
            WorkOrderBlocker.operation_id == operation.id,
            WorkOrderBlocker.status.in_([WorkOrderBlockerStatus.OPEN.value, WorkOrderBlockerStatus.ACKNOWLEDGED.value]),
        )
        .first()
        is not None
    )
    nest.restore()
    if not has_blocker:
        operation.status = (
            OperationStatus.IN_PROGRESS
            if operation.actual_start and work_order.status != WorkOrderStatus.DRAFT
            else OperationStatus.PENDING
        )
        if work_order.status != WorkOrderStatus.DRAFT:
            # Laser pools have no predecessor ordering. Only this nest may be
            # changed by the restore, so do not promote its pending siblings.
            promote_ready_operations(work_order, [operation], db=db, user_id=None)
    _recompute_child_quantity_ordered(db, work_order, company_id)
    extra = {"transition": "restore_laser_nest", "nest_id": nest.id, "work_order_id": work_order.id}
    audit.log_required(
        "RESTORE",
        "laser_nest",
        resource_id=nest.id,
        resource_identifier=nest.cnc_number or nest.nest_name,
        old_values=old_nest,
        new_values={"is_deleted": False, "deleted_at": None, "deleted_by": None},
        extra_data=extra,
    )
    if old_status != operation.status.value:
        audit.log_required(
            "STATUS_CHANGE",
            "work_order_operation",
            resource_id=operation.id,
            resource_identifier=operation.operation_number,
            old_values={"status": old_status},
            new_values={"status": operation.status.value},
            extra_data=extra,
        )
    if old_quantity != float(work_order.quantity_ordered):
        audit.log_required(
            "UPDATE",
            "work_order",
            resource_id=work_order.id,
            resource_identifier=work_order.work_order_number,
            old_values={"quantity_ordered": old_quantity},
            new_values={"quantity_ordered": float(work_order.quantity_ordered)},
            extra_data=extra,
        )
    return nest
