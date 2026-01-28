from typing import List, Optional
import math
from datetime import datetime, timezone, date
import hashlib
import json
from fastapi import APIRouter, Depends, HTTPException, status, Query, Header, Response
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import and_, or_, func, case
from pydantic import BaseModel
from app.db.database import get_db
from app.api.deps import get_current_user
from app.models.user import User
from app.models.work_order import WorkOrder, WorkOrderOperation, WorkOrderStatus, OperationStatus
from app.models.part import PartType
from app.models.work_center import WorkCenter
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.audit_log import AuditLog
from app.schemas.time_entry import ClockIn, ClockOut, TimeEntryResponse
from app.services.audit_service import AuditService
from app.services.scheduling_service import SchedulingService


class OperationCompleteRequest(BaseModel):
    quantity_complete: float
    notes: Optional[str] = None

router = APIRouter()


def _is_grouped_assembly(work_order: WorkOrder) -> bool:
    return bool(work_order.part and work_order.part.part_type == PartType.ASSEMBLY)


def _get_active_group(operations: List[WorkOrderOperation]) -> Optional[str]:
    remaining = [op for op in operations if op.status != OperationStatus.COMPLETE]
    if not remaining:
        return None
    first_op = min(remaining, key=lambda op: op.sequence)
    return first_op.operation_group


def _release_next_group(db: Session, work_order: WorkOrder, completed_op: WorkOrderOperation) -> None:
    if not _is_grouped_assembly(work_order) or not completed_op.operation_group:
        next_op = db.query(WorkOrderOperation).filter(
            and_(
                WorkOrderOperation.work_order_id == work_order.id,
                WorkOrderOperation.sequence > completed_op.sequence,
                WorkOrderOperation.status == OperationStatus.PENDING
            )
        ).order_by(WorkOrderOperation.sequence).first()
        if next_op:
            next_op.status = OperationStatus.READY
        return

    same_group_remaining = db.query(WorkOrderOperation).filter(
        and_(
            WorkOrderOperation.work_order_id == work_order.id,
            WorkOrderOperation.operation_group == completed_op.operation_group,
            WorkOrderOperation.status != OperationStatus.COMPLETE
        )
    ).count()
    if same_group_remaining > 0:
        return

    remaining = db.query(WorkOrderOperation).filter(
        and_(
            WorkOrderOperation.work_order_id == work_order.id,
            WorkOrderOperation.status != OperationStatus.COMPLETE
        )
    ).order_by(WorkOrderOperation.sequence).all()
    next_group = _get_active_group(remaining)
    if not next_group:
        return

    for op in remaining:
        if op.operation_group == next_group and op.status == OperationStatus.PENDING:
            op.status = OperationStatus.READY

@router.get("/my-active-job")
def get_my_active_job(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get the current user's active time entries (clocked in jobs)"""
    active_entries = db.query(TimeEntry).filter(
        and_(
            TimeEntry.user_id == current_user.id,
            TimeEntry.clock_out.is_(None)
        )
    ).all()
    
    if not active_entries:
        return {"active_jobs": [], "active_job": None}

    jobs = []
    for entry in active_entries:
        operation = db.query(WorkOrderOperation).filter(
            WorkOrderOperation.id == entry.operation_id
        ).first()
        
        work_order = db.query(WorkOrder).options(
            joinedload(WorkOrder.part)
        ).filter(WorkOrder.id == entry.work_order_id).first()
        
        jobs.append({
            "time_entry_id": entry.id,
            "clock_in": entry.clock_in,
            "entry_type": entry.entry_type,
            "work_order_number": work_order.work_order_number if work_order else None,
            "part_number": work_order.part.part_number if work_order and work_order.part else None,
            "part_name": work_order.part.name if work_order and work_order.part else None,
            "operation_name": operation.name if operation else None,
            "operation_number": operation.operation_number if operation else None,
        })
    
    return {
        "active_jobs": jobs,
        "active_job": jobs[0] if jobs else None
    }


@router.post("/clock-in", response_model=TimeEntryResponse)
def clock_in(
    clock_in_data: ClockIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Clock in to a work order operation"""
    # Prevent duplicate clock-ins for the same operation
    existing = db.query(TimeEntry).filter(
        and_(
            TimeEntry.user_id == current_user.id,
            TimeEntry.operation_id == clock_in_data.operation_id,
            TimeEntry.clock_out.is_(None)
        )
    ).first()
    
    if existing:
        raise HTTPException(
            status_code=400,
            detail="You are already clocked in to this operation."
        )
    
    # Verify work order and operation
    operation = db.query(WorkOrderOperation).options(
        joinedload(WorkOrderOperation.work_order).joinedload(WorkOrder.part)
    ).filter(WorkOrderOperation.id == clock_in_data.operation_id).first()
    
    if not operation:
        raise HTTPException(status_code=404, detail="Operation not found")
    
    if operation.work_order_id != clock_in_data.work_order_id:
        raise HTTPException(status_code=400, detail="Operation does not belong to this work order")

    if operation.work_center_id != clock_in_data.work_center_id:
        raise HTTPException(status_code=400, detail="Operation does not belong to this work center")

    if operation.status not in [OperationStatus.READY, OperationStatus.IN_PROGRESS]:
        raise HTTPException(status_code=400, detail="Operation is not ready to start")

    # Prevent out-of-sequence starts (allow parallel within active group for assembly WOs)
    work_order = operation.work_order
    if not work_order:
        raise HTTPException(status_code=404, detail="Work order not found")

    if _is_grouped_assembly(work_order) and operation.operation_group:
        active_group = _get_active_group(work_order.operations)
        if active_group and operation.operation_group != active_group:
            raise HTTPException(
                status_code=400,
                detail="Operations in this group are not released yet"
            )
    else:
        prev_ops = db.query(WorkOrderOperation).filter(
            and_(
                WorkOrderOperation.work_order_id == operation.work_order_id,
                WorkOrderOperation.sequence < operation.sequence,
                WorkOrderOperation.status != OperationStatus.COMPLETE
            )
        ).count()

        if prev_ops > 0:
            raise HTTPException(
                status_code=400,
                detail="Previous operations must be completed first"
            )
    
    # Update operation status
    if operation.status == OperationStatus.READY:
        operation.status = OperationStatus.IN_PROGRESS
        if not operation.actual_start:
            operation.actual_start = datetime.utcnow()
        operation.started_by = current_user.id
    
    # Update work order status
    if work_order.status == WorkOrderStatus.RELEASED:
        work_order.status = WorkOrderStatus.IN_PROGRESS
        work_order.actual_start = datetime.utcnow()
    
    # Create time entry
    time_entry = TimeEntry(
        user_id=current_user.id,
        work_order_id=clock_in_data.work_order_id,
        operation_id=clock_in_data.operation_id,
        work_center_id=clock_in_data.work_center_id,
        entry_type=clock_in_data.entry_type,
        clock_in=datetime.utcnow(),
        notes=clock_in_data.notes
    )
    
    db.add(time_entry)
    db.commit()
    db.refresh(time_entry)
    
    return time_entry


@router.post("/clock-out/{time_entry_id}", response_model=TimeEntryResponse)
def clock_out(
    time_entry_id: int,
    clock_out_data: ClockOut,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Clock out from a work order operation"""
    time_entry = db.query(TimeEntry).filter(
        and_(
            TimeEntry.id == time_entry_id,
            TimeEntry.user_id == current_user.id
        )
    ).first()
    
    if not time_entry:
        raise HTTPException(status_code=404, detail="Time entry not found")
    
    if time_entry.clock_out:
        raise HTTPException(status_code=400, detail="Already clocked out")

    if clock_out_data.quantity_produced < 0 or clock_out_data.quantity_scrapped < 0:
        raise HTTPException(status_code=400, detail="Quantities cannot be negative")
    
    # Update time entry
    time_entry.clock_out = datetime.utcnow()
    time_entry.duration_hours = (time_entry.clock_out - time_entry.clock_in).total_seconds() / 3600
    time_entry.quantity_produced = clock_out_data.quantity_produced
    time_entry.quantity_scrapped = clock_out_data.quantity_scrapped
    time_entry.scrap_reason = clock_out_data.scrap_reason
    time_entry.notes = clock_out_data.notes or time_entry.notes
    
    # Update work order totals
    work_order = db.query(WorkOrder).filter(
        WorkOrder.id == time_entry.work_order_id
    ).first()
    
    # Update operation actual hours
    operation = db.query(WorkOrderOperation).filter(
        WorkOrderOperation.id == time_entry.operation_id
    ).first()
    
    if operation:
        if work_order and (operation.quantity_complete + clock_out_data.quantity_produced) > work_order.quantity_ordered:
            raise HTTPException(status_code=400, detail="Quantity produced exceeds quantity ordered")

        if time_entry.entry_type == TimeEntryType.SETUP:
            operation.actual_setup_hours += time_entry.duration_hours
        else:
            operation.actual_run_hours += time_entry.duration_hours
        
        operation.quantity_complete += clock_out_data.quantity_produced
        operation.quantity_scrapped += clock_out_data.quantity_scrapped
    
    if work_order:
        work_order.actual_hours += time_entry.duration_hours
    
    # Update statuses if operation complete
    if operation and work_order:
        is_fully_complete = operation.quantity_complete >= work_order.quantity_ordered

        if is_fully_complete:
            operation.status = OperationStatus.COMPLETE
            operation.actual_end = datetime.utcnow()
            operation.completed_by = current_user.id

            remaining_ops = db.query(WorkOrderOperation).filter(
                and_(
                    WorkOrderOperation.work_order_id == work_order.id,
                    WorkOrderOperation.id != operation.id,
                    WorkOrderOperation.status != OperationStatus.COMPLETE
                )
            ).count()

            if remaining_ops == 0:
                work_order.status = WorkOrderStatus.COMPLETE
                work_order.actual_end = datetime.utcnow()
                work_order.quantity_complete = operation.quantity_complete
                SchedulingService(db).update_availability_rates(
                    work_center_ids=[operation.work_center_id],
                    horizon_days=90
                )
            else:
                _release_next_group(db, work_order, operation)
                ready_wcs = {
                    op.work_center_id
                    for op in work_order.operations
                    if op.status == OperationStatus.READY and op.work_center_id
                }
                affected_work_centers = {operation.work_center_id} | ready_wcs
                SchedulingService(db).update_availability_rates(
                    work_center_ids=list(affected_work_centers),
                    horizon_days=90
                )

        work_order.quantity_complete = operation.quantity_complete
        work_order.updated_at = datetime.utcnow()
    
    db.commit()
    db.refresh(time_entry)
    
    return time_entry


@router.get("/work-center-queue/{work_center_id}")
def get_work_center_queue(
    work_center_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get operations queued at a work center"""
    operations = db.query(WorkOrderOperation).options(
        joinedload(WorkOrderOperation.work_order).joinedload(WorkOrder.part)
    ).filter(
        and_(
            WorkOrderOperation.work_center_id == work_center_id,
            WorkOrderOperation.status.in_([OperationStatus.READY, OperationStatus.IN_PROGRESS])
        )
    ).order_by(WorkOrderOperation.scheduled_start).all()
    
    queue = []
    for op in operations:
        wo = op.work_order
        queue.append({
            "operation_id": op.id,
            "work_order_id": wo.id,
            "work_order_number": wo.work_order_number,
            "part_number": wo.part.part_number if wo.part else None,
            "part_name": wo.part.name if wo.part else None,
            "operation_number": op.operation_number,
            "operation_name": op.name,
            "status": op.status,
            "quantity_ordered": wo.quantity_ordered,
            "quantity_complete": op.quantity_complete,
            "priority": wo.priority,
            "due_date": wo.due_date,
            "setup_time_hours": op.setup_time_hours,
            "run_time_hours": op.run_time_hours,
        })
    
    return {"queue": queue}


@router.get("/dashboard")
def shop_floor_dashboard(
    response: Response,
    if_none_match: Optional[str] = Header(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get shop floor dashboard data with ETag support for conditional requests.
    
    Supports If-None-Match header for cache validation.
    Returns 304 Not Modified if data hasn't changed, saving bandwidth.
    
    OPTIMIZATION: Uses aggregation queries to avoid N+1 query problem.
    Before: 1 query for work centers + 2 queries per work center (N+1 pattern)
            For 25 work centers = 51 queries
    After:  3 queries total (work centers + aggregated operation counts + summary stats)
    """
    from datetime import date
    
    # Active work orders
    active_wos = db.query(WorkOrder).filter(
        WorkOrder.status == WorkOrderStatus.IN_PROGRESS
    ).count()
    
    # Work orders due today
    due_today = db.query(WorkOrder).filter(
        and_(
            WorkOrder.due_date == date.today(),
            WorkOrder.status.not_in([WorkOrderStatus.COMPLETE, WorkOrderStatus.CLOSED])
        )
    ).count()
    
    # Overdue work orders
    overdue = db.query(WorkOrder).filter(
        and_(
            WorkOrder.due_date < date.today(),
            WorkOrder.status.not_in([WorkOrderStatus.COMPLETE, WorkOrderStatus.CLOSED, WorkOrderStatus.CANCELLED])
        )
    ).count()
    
    # OPTIMIZATION: Single aggregation query for operation counts by work center
    # Uses conditional aggregation (SUM with CASE) instead of N separate COUNT queries
    operation_counts = db.query(
        WorkOrderOperation.work_center_id,
        func.sum(
            case((WorkOrderOperation.status == OperationStatus.IN_PROGRESS, 1), else_=0)
        ).label('active_count'),
        func.sum(
            case((WorkOrderOperation.status == OperationStatus.READY, 1), else_=0)
        ).label('queued_count')
    ).filter(
        WorkOrderOperation.work_center_id.isnot(None)
    ).group_by(
        WorkOrderOperation.work_center_id
    ).all()
    
    # Build lookup dict for O(1) access - avoids repeated dictionary lookups in loop
    op_counts_by_wc = {
        row.work_center_id: {
            'active': int(row.active_count or 0),
            'queued': int(row.queued_count or 0)
        }
        for row in operation_counts
    }
    
    # Get work centers (single query)
    work_centers = db.query(WorkCenter).filter(WorkCenter.is_active == True).all()
    
    # Build response using pre-computed counts
    wc_status = []
    for wc in work_centers:
        counts = op_counts_by_wc.get(wc.id, {'active': 0, 'queued': 0})
        wc_status.append({
            "id": wc.id,
            "code": wc.code,
            "name": wc.name,
            "type": wc.work_center_type.value if hasattr(wc.work_center_type, 'value') else wc.work_center_type,
            "status": wc.current_status,
            "active_operations": counts['active'],
            "queued_operations": counts['queued']
        })
    
    # Recent completions
    recent = db.query(WorkOrder).filter(
        WorkOrder.status == WorkOrderStatus.COMPLETE
    ).order_by(WorkOrder.actual_end.desc()).limit(5).all()
    
    data = {
        "summary": {
            "active_work_orders": active_wos,
            "due_today": due_today,
            "overdue": overdue
        },
        "work_centers": wc_status,
        "recent_completions": [
            {
                "work_order_number": wo.work_order_number,
                "completed_at": wo.actual_end.isoformat() if wo.actual_end else None,
                "quantity_complete": wo.quantity_complete
            } for wo in recent
        ]
    }
    
    # Generate ETag from response data
    etag = hashlib.md5(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()
    etag_header = f'"{etag}"'
    
    # Check If-None-Match header for conditional request
    if if_none_match and if_none_match.strip('"') == etag:
        return Response(status_code=304)
    
    # Set cache headers
    response.headers["ETag"] = etag_header
    response.headers["Cache-Control"] = "private, max-age=10"
    
    return data


@router.get("/active-users")
def get_active_shop_users(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get list of users currently clocked in"""
    active_entries = db.query(TimeEntry).options(
        joinedload(TimeEntry.user),
        joinedload(TimeEntry.work_order),
        joinedload(TimeEntry.operation),
        joinedload(TimeEntry.work_center)
    ).filter(TimeEntry.clock_out.is_(None)).all()
    
    users = []
    for entry in active_entries:
        users.append({
            "user_id": entry.user_id,
            "user_name": entry.user.full_name if entry.user else None,
            "work_order_number": entry.work_order.work_order_number if entry.work_order else None,
            "operation": entry.operation.name if entry.operation else None,
            "work_center": entry.work_center.name if entry.work_center else None,
            "clock_in": entry.clock_in,
            "entry_type": entry.entry_type
        })
    
    return {"active_users": users}


# ============ SIMPLIFIED OPERATION WORKFLOW ============

@router.get("/operations")
def get_all_operations(
    work_center_id: Optional[int] = Query(None, description="Filter by work center"),
    status: Optional[str] = Query(None, description="Filter by status: pending, ready, in_progress, complete, on_hold"),
    search: Optional[str] = Query(None, description="Search by WO number or part number"),
    due_today: bool = Query(False, description="Filter operations due today"),
    page: int = Query(1, ge=1, description="Page number (1-indexed)"),
    page_size: int = Query(50, ge=1, le=200, description="Items per page (max 200)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get operations with filters and pagination for the shop floor view.
    
    Returns paginated operations that are not complete or cancelled.
    Default: 50 items per page, max 200.
    
    Response includes pagination metadata for building UI controls.
    """
    from app.core.pagination import paginate_query
    
    query = db.query(WorkOrderOperation).options(
        joinedload(WorkOrderOperation.work_order).joinedload(WorkOrder.part),
        joinedload(WorkOrderOperation.work_center)
    ).join(WorkOrder)
    
    # Exclude completed/cancelled work orders
    query = query.filter(
        WorkOrder.status.not_in([WorkOrderStatus.COMPLETE, WorkOrderStatus.CLOSED, WorkOrderStatus.CANCELLED])
    )
    
    # Filter by work center
    if work_center_id:
        query = query.filter(WorkOrderOperation.work_center_id == work_center_id)
    
    # Filter by operation status
    if status:
        try:
            op_status = OperationStatus(status)
            query = query.filter(WorkOrderOperation.status == op_status)
        except ValueError:
            pass  # Invalid status, ignore filter
    else:
        # Default: exclude completed operations
        query = query.filter(WorkOrderOperation.status != OperationStatus.COMPLETE)
    
    # Search by WO number or part number
    if search:
        search_term = f"%{search}%"
        from app.models.part import Part
        query = query.join(Part, WorkOrder.part_id == Part.id).filter(
            or_(
                WorkOrder.work_order_number.ilike(search_term),
                Part.part_number.ilike(search_term)
            )
        )

    if due_today:
        query = query.filter(WorkOrder.due_date == date.today())
    
    # Order by priority, then due date
    query = query.order_by(
        WorkOrder.priority,
        WorkOrder.due_date,
        WorkOrderOperation.sequence
    )
    
    # Apply pagination
    paginated_query, pagination_meta = paginate_query(query, page, page_size)
    operations = paginated_query.all()
    
    # Build response data
    result = []
    for op in operations:
        wo = op.work_order
        wc = op.work_center
        result.append({
            "id": op.id,
            "work_order_id": wo.id,
            "work_order_number": wo.work_order_number,
            "part_number": wo.part.part_number if wo.part else None,
            "part_name": wo.part.name if wo.part else None,
            "operation_number": op.operation_number,
            "operation_name": op.name,
            "description": op.description,
            "work_center_id": wc.id if wc else None,
            "work_center_name": wc.name if wc else None,
            "status": op.status.value,
            "quantity_ordered": wo.quantity_ordered,
            "quantity_complete": op.quantity_complete,
            "quantity_scrapped": op.quantity_scrapped,
            "priority": wo.priority,
            "due_date": wo.due_date.isoformat() if wo.due_date else None,
            "customer_name": wo.customer_name,
            "customer_po": wo.customer_po,
            "actual_start": op.actual_start.isoformat() if op.actual_start else None,
            "setup_instructions": op.setup_instructions,
            "run_instructions": op.run_instructions,
            "requires_inspection": op.requires_inspection,
        })
    
    return {
        "operations": result,
        "total": pagination_meta.total_count,  # Backward compatibility
        "pagination": {
            "page": pagination_meta.page,
            "page_size": pagination_meta.page_size,
            "total_count": pagination_meta.total_count,
            "total_pages": pagination_meta.total_pages,
            "has_next": pagination_meta.has_next,
            "has_previous": pagination_meta.has_previous
        }
    }


@router.put("/operations/{operation_id}/start")
def start_operation(
    operation_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Mark operation as in progress (simplified workflow - no time clock).
    - Sets status to IN_PROGRESS
    - Records actual_start_time
    - Updates work order status if needed
    """
    operation = db.query(WorkOrderOperation).options(
        joinedload(WorkOrderOperation.work_order).joinedload(WorkOrder.part)
    ).filter(WorkOrderOperation.id == operation_id).first()
    
    if not operation:
        raise HTTPException(status_code=404, detail="Operation not found")
    
    # Validate operation can be started
    if operation.status == OperationStatus.COMPLETE:
        raise HTTPException(status_code=400, detail="Operation is already complete")
    
    if operation.status == OperationStatus.IN_PROGRESS:
        raise HTTPException(status_code=400, detail="Operation is already in progress")
    
    work_order = operation.work_order
    if not work_order:
        raise HTTPException(status_code=404, detail="Work order not found")

    if _is_grouped_assembly(work_order) and operation.operation_group:
        active_group = _get_active_group(work_order.operations)
        if active_group and operation.operation_group != active_group:
            raise HTTPException(status_code=400, detail="Operations in this group are not released yet")
    else:
    work_order = operation.work_order
    if not work_order:
        raise HTTPException(status_code=404, detail="Work order not found")

    if _is_grouped_assembly(work_order) and operation.operation_group:
        active_group = _get_active_group(work_order.operations)
        if active_group and operation.operation_group != active_group:
            raise HTTPException(status_code=400, detail="Operations in this group are not released yet")
    else:
        # Check if previous operations are complete (if not first operation)
        prev_ops = db.query(WorkOrderOperation).filter(
            and_(
                WorkOrderOperation.work_order_id == operation.work_order_id,
                WorkOrderOperation.sequence < operation.sequence,
                WorkOrderOperation.status != OperationStatus.COMPLETE
            )
        ).count()
        
        if prev_ops > 0:
            raise HTTPException(
                status_code=400, 
                detail="Previous operations must be completed first"
            )
    
    # Update operation
    operation.status = OperationStatus.IN_PROGRESS
    operation.actual_start = datetime.utcnow()
    operation.started_by = current_user.id
    operation.updated_at = datetime.utcnow()
    
    # Update work order status if needed
    if work_order.status in [WorkOrderStatus.DRAFT, WorkOrderStatus.RELEASED]:
        work_order.status = WorkOrderStatus.IN_PROGRESS
        if not work_order.actual_start:
            work_order.actual_start = datetime.utcnow()
    
    # Create audit log
    AuditService(db, current_user).log(
        action="START_OPERATION",
        resource_type="work_order_operation",
        resource_id=operation_id,
        description=f"Started operation {operation.operation_number} on WO {work_order.work_order_number}"
    )
    
    db.commit()
    db.refresh(operation)
    
    return {
        "message": "Operation started successfully",
        "operation": {
            "id": operation.id,
            "status": operation.status.value,
            "actual_start": operation.actual_start.isoformat() if operation.actual_start else None
        }
    }


@router.post("/operations/{operation_id}/complete")
def complete_operation(
    operation_id: int,
    completion_data: OperationCompleteRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Mark operation as complete (full or partial).
    - Updates quantity_complete
    - If qty_complete >= qty_ordered: status = COMPLETE, record actual_end_time
    - If qty_complete < qty_ordered: status remains IN_PROGRESS
    - Optionally record notes
    """
    operation = db.query(WorkOrderOperation).options(
        joinedload(WorkOrderOperation.work_order).joinedload(WorkOrder.part)
    ).filter(WorkOrderOperation.id == operation_id).first()
    
    if not operation:
        raise HTTPException(status_code=404, detail="Operation not found")
    
    work_order = operation.work_order
    if not work_order:
        raise HTTPException(status_code=404, detail="Work order not found for this operation")
    
    # Validate operation state
    if operation.status == OperationStatus.COMPLETE:
        raise HTTPException(status_code=400, detail="Operation is already complete")
    
    if operation.status not in [OperationStatus.IN_PROGRESS, OperationStatus.READY]:
        raise HTTPException(
            status_code=400, 
            detail=f"Cannot complete operation with status: {operation.status.value}"
        )
    
    # Validate quantity
    if math.isnan(completion_data.quantity_complete) or math.isinf(completion_data.quantity_complete):
        raise HTTPException(status_code=400, detail="Quantity must be a valid number")

    if completion_data.quantity_complete < 0:
        raise HTTPException(status_code=400, detail="Quantity cannot be negative")
    
    ordered_qty = float(work_order.quantity_ordered or 0)
    if ordered_qty <= 0:
        raise HTTPException(status_code=400, detail="Work order quantity ordered is missing or invalid")

    if completion_data.quantity_complete > ordered_qty:
        raise HTTPException(
            status_code=400, 
            detail=f"Quantity ({completion_data.quantity_complete}) cannot exceed quantity ordered ({ordered_qty})"
        )
    
    # Auto-start if not already in progress
    if operation.status != OperationStatus.IN_PROGRESS:
        operation.status = OperationStatus.IN_PROGRESS
        if not operation.actual_start:
            operation.actual_start = datetime.utcnow()
            operation.started_by = current_user.id
    
    # Update quantity
    operation.quantity_complete = completion_data.quantity_complete
    operation.updated_at = datetime.utcnow()
    
    # Check if fully complete
    is_fully_complete = completion_data.quantity_complete >= ordered_qty
    
    if is_fully_complete:
        operation.status = OperationStatus.COMPLETE
        operation.actual_end = datetime.utcnow()
        operation.completed_by = current_user.id
        
        # Check if this is the last operation
        remaining_ops = db.query(WorkOrderOperation).filter(
            and_(
                WorkOrderOperation.work_order_id == work_order.id,
                WorkOrderOperation.id != operation_id,
                WorkOrderOperation.status != OperationStatus.COMPLETE
            )
        ).count()
        
        if remaining_ops == 0:
            # All operations complete - mark work order complete
            work_order.status = WorkOrderStatus.COMPLETE
            work_order.actual_end = datetime.utcnow()
            work_order.quantity_complete = completion_data.quantity_complete
            if operation.work_center_id:
                SchedulingService(db).update_availability_rates(
                    work_center_ids=[operation.work_center_id],
                    horizon_days=90
                )
        else:
            _release_next_group(db, work_order, operation)
            ready_wcs = {
                op.work_center_id
                for op in work_order.operations
                if op.status == OperationStatus.READY and op.work_center_id
            }
            affected_work_centers = {operation.work_center_id} | ready_wcs
            SchedulingService(db).update_availability_rates(
                work_center_ids=list(affected_work_centers),
                horizon_days=90
            )
    
    # Update work order quantity tracking
    work_order.quantity_complete = completion_data.quantity_complete
    work_order.updated_at = datetime.utcnow()
    
    # Create audit log
    AuditService(db, current_user).log(
        action="COMPLETE_OPERATION" if is_fully_complete else "UPDATE_OPERATION_PROGRESS",
        resource_type="work_order_operation",
        resource_id=operation_id,
        description=(
            f"{'Completed' if is_fully_complete else 'Updated'} operation {operation.operation_number} on WO {work_order.work_order_number}. "
            f"Qty: {completion_data.quantity_complete}/{work_order.quantity_ordered}"
            + (f". Notes: {completion_data.notes}" if completion_data.notes else "")
        )
    )
    
    db.commit()
    db.refresh(operation)
    
    return {
        "message": "Operation completed" if is_fully_complete else "Progress updated",
        "operation": {
            "id": operation.id,
            "status": operation.status.value,
            "quantity_complete": operation.quantity_complete,
            "actual_start": operation.actual_start.isoformat() if operation.actual_start else None,
            "actual_end": operation.actual_end.isoformat() if operation.actual_end else None,
        },
        "work_order": {
            "id": work_order.id,
            "status": work_order.status.value,
            "quantity_complete": work_order.quantity_complete
        },
        "is_fully_complete": is_fully_complete
    }


@router.get("/operations/{operation_id}")
def get_operation_details(
    operation_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get detailed information about a specific operation"""
    operation = db.query(WorkOrderOperation).options(
        joinedload(WorkOrderOperation.work_order).joinedload(WorkOrder.part),
        joinedload(WorkOrderOperation.work_center)
    ).filter(WorkOrderOperation.id == operation_id).first()
    
    if not operation:
        raise HTTPException(status_code=404, detail="Operation not found")
    
    wo = operation.work_order
    wc = operation.work_center
    
    # Get all operations for this work order
    all_ops = db.query(WorkOrderOperation).filter(
        WorkOrderOperation.work_order_id == wo.id
    ).order_by(WorkOrderOperation.sequence).all()
    
    # Get recent history (audit logs)
    history = db.query(AuditLog).filter(
        and_(
            AuditLog.resource_type == "work_order_operation",
            AuditLog.resource_id == operation_id
        )
    ).order_by(AuditLog.timestamp.desc()).limit(10).all()
    
    return {
        "operation": {
            "id": operation.id,
            "operation_number": operation.operation_number,
            "name": operation.name,
            "description": operation.description,
            "status": operation.status.value,
            "quantity_complete": operation.quantity_complete,
            "quantity_scrapped": operation.quantity_scrapped,
            "setup_instructions": operation.setup_instructions,
            "run_instructions": operation.run_instructions,
            "setup_time_hours": operation.setup_time_hours,
            "run_time_hours": operation.run_time_hours,
            "actual_setup_hours": operation.actual_setup_hours,
            "actual_run_hours": operation.actual_run_hours,
            "actual_start": operation.actual_start.isoformat() if operation.actual_start else None,
            "actual_end": operation.actual_end.isoformat() if operation.actual_end else None,
            "requires_inspection": operation.requires_inspection,
            "inspection_type": operation.inspection_type,
            "inspection_complete": operation.inspection_complete,
        },
        "work_order": {
            "id": wo.id,
            "work_order_number": wo.work_order_number,
            "status": wo.status.value,
            "quantity_ordered": wo.quantity_ordered,
            "quantity_complete": wo.quantity_complete,
            "due_date": wo.due_date.isoformat() if wo.due_date else None,
            "customer_name": wo.customer_name,
            "customer_po": wo.customer_po,
            "notes": wo.notes,
            "special_instructions": wo.special_instructions,
            "part": {
                "part_number": wo.part.part_number if wo.part else None,
                "name": wo.part.name if wo.part else None,
                "description": wo.part.description if wo.part else None,
            }
        },
        "work_center": {
            "id": wc.id if wc else None,
            "name": wc.name if wc else None,
            "code": wc.code if wc else None,
        },
        "all_operations": [
            {
                "id": op.id,
                "sequence": op.sequence,
                "operation_number": op.operation_number,
                "name": op.name,
                "status": op.status.value,
                "quantity_complete": op.quantity_complete,
                "is_current": op.id == operation_id
            }
            for op in all_ops
        ],
        "history": [
            {
                "action": h.action,
                "details": h.description,
                "created_at": h.timestamp.isoformat() if h.timestamp else None
            }
            for h in history
        ]
    }


@router.put("/operations/{operation_id}/hold")
def put_operation_on_hold(
    operation_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Put an operation on hold"""
    operation = db.query(WorkOrderOperation).options(
        joinedload(WorkOrderOperation.work_order)
    ).filter(WorkOrderOperation.id == operation_id).first()
    
    if not operation:
        raise HTTPException(status_code=404, detail="Operation not found")
    
    if operation.status == OperationStatus.COMPLETE:
        raise HTTPException(status_code=400, detail="Cannot put completed operation on hold")
    
    operation.status = OperationStatus.ON_HOLD
    operation.updated_at = datetime.utcnow()
    
    # Create audit log
    AuditService(db, current_user).log(
        action="HOLD_OPERATION",
        resource_type="work_order_operation",
        resource_id=operation_id,
        description=f"Put operation {operation.operation_number} on hold"
    )
    
    db.commit()
    
    return {"message": "Operation placed on hold", "status": operation.status.value}


@router.put("/operations/{operation_id}/resume")
def resume_operation(
    operation_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Resume an operation that was on hold"""
    operation = db.query(WorkOrderOperation).options(
        joinedload(WorkOrderOperation.work_order)
    ).filter(WorkOrderOperation.id == operation_id).first()
    
    if not operation:
        raise HTTPException(status_code=404, detail="Operation not found")
    
    if operation.status != OperationStatus.ON_HOLD:
        raise HTTPException(status_code=400, detail="Operation is not on hold")
    
    # Resume to previous state
    operation.status = OperationStatus.IN_PROGRESS if operation.actual_start else OperationStatus.READY
    operation.updated_at = datetime.utcnow()
    
    # Create audit log
    AuditService(db, current_user).log(
        action="RESUME_OPERATION",
        resource_type="work_order_operation",
        resource_id=operation_id,
        description=f"Resumed operation {operation.operation_number}"
    )
    
    db.commit()
    
    return {"message": "Operation resumed", "status": operation.status.value}
