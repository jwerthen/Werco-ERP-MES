from typing import FrozenSet, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_current_company_id, get_current_user
from app.db.database import get_db
from app.models.user import User
from app.schemas.operational_event import OperationalEventCreate, OperationalEventResponse
from app.services.dispatch_service import DISPATCH_NOTICE_EVENT_TYPE
from app.services.operational_event_service import OperationalEventService

router = APIRouter()

# Event types the SERVER emits itself and whose rows a floor screen trusts as "the
# system said this". A client may not post one: a forged ``dispatch_order_changed``
# row is read back by every kiosk / crew-station / my-active-job poll at that
# machine as the planner's RUN NEXT banner (and fans out a ``dispatch.changed``
# notification to the operators clocked in there). Extend this set -- rather than
# adding a second check -- when another server-written type gains a reader that
# trusts it. Matched case-insensitively after trimming, so a spelling variant cannot
# slip past the check.
SERVER_ONLY_EVENT_TYPES: FrozenSet[str] = frozenset({DISPATCH_NOTICE_EVENT_TYPE})


def server_only_event_type_detail(event_type: str) -> str:
    """The 400 ``detail`` for a refused server-only event type (shared with the docs/tests)."""
    return (
        f"Event type '{event_type}' is recorded by the system itself and cannot be posted. "
        "To change a machine's run order, use the Dispatch Board."
    )


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
    """Record a tenant-scoped operational event for real-time AI context.

    Refused with 400 for event types the system emits itself: today only
    ``dispatch_order_changed``, the Dispatch Board's "the planner changed this
    machine's order" notice. Floor screens show it as the RUN NEXT banner and it
    notifies the operators at that machine, so only the dispatch-order routes may
    write it; change a machine's run order through the Dispatch Board instead. Every
    other event type is accepted as before.
    """
    if data.event_type.strip().lower() in SERVER_ONLY_EVENT_TYPES:
        raise HTTPException(status_code=400, detail=server_only_event_type_detail(data.event_type))
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
