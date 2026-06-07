from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_audit_service, get_current_company_id, get_current_user, require_role
from app.core.realtime import safe_broadcast
from app.core.websocket import broadcast_dashboard_update, broadcast_shop_floor_update, broadcast_work_order_update
from app.db.database import get_db
from app.models.user import User, UserRole
from app.models.work_order_blocker import WorkOrderBlocker
from app.schemas.work_order_blocker import (
    WorkOrderBlockerCreate,
    WorkOrderBlockerResolve,
    WorkOrderBlockerResponse,
    WorkOrderBlockerUpdate,
)
from app.services.audit_service import AuditService
from app.services.work_order_blocker_service import WorkOrderBlockerService

router = APIRouter()


def _to_response(blocker: WorkOrderBlocker) -> WorkOrderBlockerResponse:
    return WorkOrderBlockerResponse(
        id=blocker.id,
        company_id=blocker.company_id,
        work_order_id=blocker.work_order_id,
        operation_id=blocker.operation_id,
        material_part_id=blocker.material_part_id,
        category=blocker.category,
        severity=blocker.severity,
        status=blocker.status,
        title=blocker.title,
        note=blocker.note,
        resolution_note=blocker.resolution_note,
        reported_by=blocker.reported_by,
        assigned_to=blocker.assigned_to,
        resolved_by=blocker.resolved_by,
        reported_at=blocker.reported_at,
        acknowledged_at=blocker.acknowledged_at,
        resolved_at=blocker.resolved_at,
        created_at=blocker.created_at,
        updated_at=blocker.updated_at,
        work_order_number=blocker.work_order.work_order_number if blocker.work_order else None,
        operation_name=blocker.operation.name if blocker.operation else None,
        material_part_number=blocker.material_part.part_number if blocker.material_part else None,
    )


def _broadcast_blocker(blocker: WorkOrderBlocker, event: str) -> None:
    safe_broadcast(
        broadcast_work_order_update,
        blocker.work_order_id,
        {
            "event": event,
            "work_order_id": blocker.work_order_id,
            "operation_id": blocker.operation_id,
            "blocker_id": blocker.id,
            "category": blocker.category,
            "status": blocker.status,
        },
    )
    safe_broadcast(
        broadcast_dashboard_update,
        {
            "event": event,
            "work_order_id": blocker.work_order_id,
            "operation_id": blocker.operation_id,
            "blocker_id": blocker.id,
            "category": blocker.category,
            "status": blocker.status,
        },
    )
    if blocker.operation and blocker.operation.work_center_id:
        safe_broadcast(
            broadcast_shop_floor_update,
            blocker.operation.work_center_id,
            {
                "event": event,
                "work_order_id": blocker.work_order_id,
                "operation_id": blocker.operation_id,
                "blocker_id": blocker.id,
            },
        )


@router.get("/", response_model=List[WorkOrderBlockerResponse])
def list_work_order_blockers(
    work_order_id: Optional[int] = Query(None, gt=0),
    status: Optional[str] = Query(None, pattern="^(open|acknowledged|resolved|dismissed)$"),
    category: Optional[str] = Query(None, max_length=40),
    limit: int = Query(100, ge=1, le=250),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """List tenant-scoped work-order blockers for managers, copilots, and NL search."""
    blockers = WorkOrderBlockerService(db).list_blockers(
        company_id=company_id,
        work_order_id=work_order_id,
        status=status,
        category=category,
        limit=limit,
    )
    return [_to_response(blocker) for blocker in blockers]


@router.post("/work-orders/{work_order_id}", response_model=WorkOrderBlockerResponse)
def create_work_order_blocker(
    work_order_id: int,
    data: WorkOrderBlockerCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Let an operator report why a job is blocked, including missing material."""
    service = WorkOrderBlockerService(db)
    try:
        blocker = service.create_blocker(
            company_id=company_id, user=current_user, work_order_id=work_order_id, data=data, audit=audit
        )
        db.commit()
        db.refresh(blocker)
        _broadcast_blocker(blocker, "work_order_blocker_created")
        return _to_response(blocker)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.put("/{blocker_id}", response_model=WorkOrderBlockerResponse)
def update_work_order_blocker(
    blocker_id: int,
    data: WorkOrderBlockerUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Acknowledge, assign, dismiss, or update a blocker without losing the original operator signal."""
    service = WorkOrderBlockerService(db)
    try:
        blocker = service.update_blocker(
            company_id=company_id, user=current_user, blocker_id=blocker_id, data=data, audit=audit
        )
        db.commit()
        db.refresh(blocker)
        _broadcast_blocker(blocker, "work_order_blocker_updated")
        return _to_response(blocker)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{blocker_id}/resolve", response_model=WorkOrderBlockerResponse)
def resolve_work_order_blocker(
    blocker_id: int,
    data: WorkOrderBlockerResolve,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Resolve a blocker and release its operation if no other blockers remain."""
    service = WorkOrderBlockerService(db)
    try:
        blocker = service.resolve_blocker(
            company_id=company_id,
            user=current_user,
            blocker_id=blocker_id,
            resolution_note=data.resolution_note,
            audit=audit,
        )
        db.commit()
        db.refresh(blocker)
        _broadcast_blocker(blocker, "work_order_blocker_resolved")
        return _to_response(blocker)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
