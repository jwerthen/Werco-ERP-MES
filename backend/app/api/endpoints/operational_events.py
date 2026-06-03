from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_current_company_id, get_current_user
from app.db.database import get_db
from app.models.user import User
from app.schemas.operational_event import OperationalEventCreate, OperationalEventResponse
from app.services.operational_event_service import OperationalEventService

router = APIRouter()


@router.get("/", response_model=List[OperationalEventResponse])
def list_operational_events(
    source_module: Optional[str] = Query(None, max_length=80),
    event_type: Optional[str] = Query(None, max_length=80),
    work_order_id: Optional[int] = Query(None, gt=0),
    limit: int = Query(100, ge=1, le=250),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Read the durable operational event stream for the active tenant."""
    return OperationalEventService(db).list_events(
        company_id=company_id,
        source_module=source_module,
        event_type=event_type,
        work_order_id=work_order_id,
        limit=limit,
    )


@router.post("/", response_model=OperationalEventResponse)
def record_operational_event(
    data: OperationalEventCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Record a tenant-scoped operational event for real-time AI context."""
    try:
        event = OperationalEventService(db).emit(
            company_id=company_id,
            user_id=current_user.id,
            event_type=data.event_type,
            source_module=data.source_module,
            entity_type=data.entity_type,
            entity_id=data.entity_id,
            work_order_id=data.work_order_id,
            operation_id=data.operation_id,
            severity=data.severity,
            event_payload=data.event_payload,
            occurred_at=data.occurred_at,
        )
        db.commit()
        db.refresh(event)
        return event
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
