from datetime import datetime, timezone
from typing import FrozenSet, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_current_company_id, get_current_user
from app.core.time_utils import ensure_utc
from app.db.database import get_db
from app.models.user import User
from app.schemas.operational_event import OperationalEventCreate, OperationalEventResponse
from app.services.dispatch_service import DISPATCH_NOTICE_EVENT_TYPE
from app.services.notification_catalog import SOURCE_EVENT_TYPE_TO_KEY
from app.services.operation_hold_view import HOLD_EVENT_TYPE
from app.services.operational_event_service import OperationalEventService

router = APIRouter()

# Event types a READER selects by name and trusts as "the system recorded this". Each
# one is written by a server action and read back, by exact ``event_type``, as fact:
#
# * ``dispatch_order_changed`` -- every kiosk / crew-station / my-active-job poll shows
#   it as the planner's RUN NEXT banner (``dispatch_service.latest_dispatch_notices``).
# * ``operation_hold`` -- who held an operation, and when, on the kiosk queue and the
#   work-order page (``operation_hold_view``).
# * ``operation_ready`` -- the queue-time anchor in flow metrics (``flow_metrics_service``).
# * ``operation_completed`` -- the digital-completion share in adoption metrics.
# * ``ncr_updated`` / ``fai_updated`` -- quality rows on the work-order timeline.
#
# Listed here on their own, not left to the catalog union below, so a type that is
# ever dropped from the notification catalog stays refused while a reader still trusts
# it. ``tests/api/test_operational_event_forgery.py`` scans ``app/`` for every
# ``OperationalEvent.event_type`` filter and fails if one names a type missing here.
READER_TRUSTED_EVENT_TYPES: FrozenSet[str] = frozenset(
    {
        DISPATCH_NOTICE_EVENT_TYPE,
        HOLD_EVENT_TYPE,
        "operation_ready",
        "operation_completed",
        "ncr_updated",
        "fai_updated",
    }
)

# Every event type a client may NOT post. A client-posted row is indistinguishable from
# one the server wrote, so a type is refused when either:
#
# * it drives notifications -- every key of ``notification_catalog.SOURCE_EVENT_TYPE_TO_KEY``.
#   ``OperationalEventService.emit`` tags those rows for the outbox, so a posted one fans
#   real in-app / email / SMS notifications out to other users ("NCR created", "work
#   order released", ...) for something that never happened. Derived from the catalog,
#   so a newly cataloged type is refused with no change here; or
# * a reader trusts it (``READER_TRUSTED_EVENT_TYPES`` above).
#
# Matched case-insensitively after trimming (every member is lower-case), so a spelling
# variant cannot slip past the check. Every other event type -- a client's own
# annotation for AI context -- is still accepted.
SERVER_ONLY_EVENT_TYPES: FrozenSet[str] = READER_TRUSTED_EVENT_TYPES | frozenset(SOURCE_EVENT_TYPE_TO_KEY)


def is_server_only_event_type(event_type: str) -> bool:
    """True when ``event_type`` (trimmed, any case) is one only the system may record."""
    return event_type.strip().lower() in SERVER_ONLY_EVENT_TYPES


def server_only_event_type_detail(event_type: str) -> str:
    """The 400 ``detail`` for a refused server-only event type (shared with the docs/tests)."""
    detail = (
        f"Event type '{event_type}' is recorded by the system itself and cannot be posted. "
        "Perform the action through its own screen or endpoint; the system records the event when it happens."
    )
    if event_type.strip().lower() == DISPATCH_NOTICE_EVENT_TYPE:
        detail += " To change a machine's run order, use the Dispatch Board."
    return detail


def clamp_occurred_at(occurred_at: Optional[datetime], now: datetime) -> Optional[datetime]:
    """A client-supplied ``occurred_at`` no later than ``now`` (tz-aware UTC). Backdating is kept.

    Readers order by ``occurred_at``; a row dated in the future would stay "the newest"
    in each of them until that date passed. ``None`` stays ``None`` (the service stamps
    the time itself). The result is normalized to UTC -- the same instant -- and a naive
    value is read as UTC, matching ``ensure_utc``.
    """
    if occurred_at is None:
        return None
    utc = ensure_utc(occurred_at)
    return now if utc is None or utc > now else utc


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
    """Record a custom, tenant-scoped operational event as context for AI assistants.

    Use this for a note of your own (for example ``event_type: "agent_observation"``),
    not to report that something happened in the ERP: event types the system records
    itself are refused with 400. That covers every type that sends notifications (work
    order released / started / completed / closed, operation completed / ready / hold,
    NCR / CAR / FAI, receipts, shipments, purchase orders, downtime, backflush and
    material shortages, dispatch order changes, ...) and every type a screen or report
    reads as fact. To make one of those happen, call the endpoint that performs the
    action; it records the event itself.

    ``occurred_at`` may be in the past; a future value is stored as the current time.
    A ``work_order_id`` or ``operation_id`` from another company is 404. Events posted
    here never send notifications.
    """
    if is_server_only_event_type(data.event_type):
        raise HTTPException(status_code=400, detail=server_only_event_type_detail(data.event_type))
    occurred_at = clamp_occurred_at(data.occurred_at, datetime.now(timezone.utc))
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
            occurred_at=occurred_at,
        )
        db.commit()
        db.refresh(event)
        return event
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
