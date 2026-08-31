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
    BlockerOperationOutcome,
    OperationOpenBlocker,
    WorkOrderBlockerCreate,
    WorkOrderBlockerResolve,
    WorkOrderBlockerResponse,
    WorkOrderBlockerUpdate,
    WorkOrderBlockerWriteResponse,
)
from app.services.audit_service import AuditService
from app.services.work_order_blocker_service import BlockerResumeOutcome, WorkOrderBlockerService

router = APIRouter()


def _response_fields(blocker: WorkOrderBlocker) -> dict:
    return dict(
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


def _to_response(blocker: WorkOrderBlocker) -> WorkOrderBlockerResponse:
    return WorkOrderBlockerResponse(**_response_fields(blocker))


def _to_write_response(
    blocker: WorkOrderBlocker, outcome: Optional[BlockerOperationOutcome]
) -> WorkOrderBlockerWriteResponse:
    """The blocker row every caller already got, plus what the write did to its operation."""
    return WorkOrderBlockerWriteResponse(**_response_fields(blocker), operation_outcome=outcome)


def _operation_outcome(resume: Optional[BlockerResumeOutcome]) -> Optional[BlockerOperationOutcome]:
    """Turn the service's resume outcome into the wire shape. PURE READ, no writes.

    ``None`` in, ``None`` out: the call did not leave the blocker RESOLVED or
    DISMISSED, so no resume was attempted and there is nothing to account for.
    That is a THIRD state, distinct from "attempted and withheld" -- see
    ``WorkOrderBlockerWriteResponse``.

    CALL THIS BEFORE ``db.commit()``. Every value here is already final (the
    service flushed), and snapshotting them into plain Python now means the
    response cannot depend on re-loading rows that ``commit()`` expires.

    ``open_blockers`` deliberately reuses the resume payload's shape without its
    two free-text-gate fields -- see ``OperationOpenBlocker`` for the verification
    that the gate cannot apply on this router.
    """
    if resume is None:
        return None
    return BlockerOperationOutcome(
        # Read off the BLOCKER, not the operation row: in the OPERATION_MISSING
        # case the blocker still names an operation there is no row to ask.
        operation_id=resume.operation_id,
        operation_status=resume.operation_status,
        operation_resumed=resume.resumed,
        resume_withheld_reason=resume.withheld_reason,
        operation_still_held=resume.operation_still_held,
        open_blockers=[
            OperationOpenBlocker(
                id=other.id,
                title=other.title,
                category=other.category,
                severity=other.severity,
                status=other.status,
            )
            for other in resume.other_open_blockers
        ],
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


@router.put("/{blocker_id}", response_model=WorkOrderBlockerWriteResponse)
def update_work_order_blocker(
    blocker_id: int,
    data: WorkOrderBlockerUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Acknowledge, assign, dismiss, or update a blocker without losing the original operator signal.

    Carries ``operation_outcome`` for the same reason ``/resolve`` does, and it is
    not a courtesy: DISMISSING a blocker runs the identical resume side effect
    through the identical ``update_blocker`` body, so a caller of THIS verb could
    be misled in exactly the same way. ``null`` on an acknowledge or an assign --
    no resume was attempted there.
    """
    service = WorkOrderBlockerService(db)
    try:
        blocker, resume = service.update_blocker_with_outcome(
            company_id=company_id, user=current_user, blocker_id=blocker_id, data=data, audit=audit
        )
        # Snapshotted BEFORE the commit that expires these rows -- see _operation_outcome.
        outcome = _operation_outcome(resume)
        db.commit()
        db.refresh(blocker)
        _broadcast_blocker(blocker, "work_order_blocker_updated")
        return _to_write_response(blocker, outcome)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{blocker_id}/resolve", response_model=WorkOrderBlockerWriteResponse)
def resolve_work_order_blocker(
    blocker_id: int,
    data: WorkOrderBlockerResolve,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Resolve a blocker and release its operation if no other blockers remain.

    "IF NO OTHER BLOCKERS REMAIN" IS THE WHOLE PROBLEM, and this response now says
    which way it went. A 200 here used to be indistinguishable between a resolve
    that took the job off hold and one that left it exactly as it was, so the page
    fired a green toast over an operation that was still ON_HOLD. Read
    ``operation_outcome``: ``operation_still_held`` means a resume was owed and
    withheld, and ``operation_resumed`` with ``operation_status == "pending"``
    means the hold cleared but the job did NOT return to the dispatch board or the
    kiosk (both surface READY only).
    """
    service = WorkOrderBlockerService(db)
    try:
        blocker, resume = service.resolve_blocker_with_outcome(
            company_id=company_id,
            user=current_user,
            blocker_id=blocker_id,
            resolution_note=data.resolution_note,
            audit=audit,
        )
        # Snapshotted BEFORE the commit that expires these rows -- see _operation_outcome.
        outcome = _operation_outcome(resume)
        db.commit()
        db.refresh(blocker)
        _broadcast_blocker(blocker, "work_order_blocker_resolved")
        return _to_write_response(blocker, outcome)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
