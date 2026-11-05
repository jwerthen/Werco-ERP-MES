import logging
from datetime import datetime, timezone
from typing import FrozenSet, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_current_company_id, get_current_user
from app.db.database import get_db
from app.models.user import User
from app.schemas.operational_event import OperationalEventCreate, OperationalEventResponse
from app.services.dispatch_service import DISPATCH_NOTICE_EVENT_TYPE
from app.services.notification_catalog import SOURCE_EVENT_TYPE_TO_KEY
from app.services.operation_hold_view import HOLD_EVENT_TYPE
from app.services.operational_event_service import OperationalEventService

logger = logging.getLogger(__name__)

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
    """A client-supplied ``occurred_at`` no later than ``now``, as NAIVE UTC. Backdating is kept.

    Readers order by ``occurred_at``; a row dated in the future would stay "the newest"
    in each of them until that date passed. ``now`` is tz-aware UTC; a naive input is
    read as UTC. The result is the same instant stored the way ``OperationalEventService.emit``
    stamps every server row (naive ``datetime.utcnow()``), so a client row and a server
    row compare alike in every reader whatever the database session's time zone.
    ``None`` stays ``None`` (the service stamps the time itself).

    Raises ``OverflowError`` for an instant whose UTC form is outside the representable
    range (e.g. ``0001-01-01T00:00:00+05:00``). A far-FUTURE value never raises: aware
    comparison does not convert, and it clamps to ``now``.
    """
    if occurred_at is None:
        return None
    aware = occurred_at if occurred_at.tzinfo is not None else occurred_at.replace(tzinfo=timezone.utc)
    if aware > now:
        aware = now
    return aware.astimezone(timezone.utc).replace(tzinfo=None)


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
    """Record a custom event for AI context (e.g. ``agent_observation``). Types the system
    records itself -- every one that sends a notification or that a screen or report reads
    as fact -- are refused with 400; call the endpoint that performs the action instead. A
    future ``occurred_at`` is stored as now. Posted events never notify.

    (Only the paragraph above reaches the generated MCP tool's description, so it carries
    the rules an agent needs.) Refused types include work order released / started /
    completed / closed / priority updated and blocker created / escalated / updated;
    operation ready / completed / hold / production reduced; NCR, CAR and FAI created and
    NCR / FAI updated; purchase order sent / received and receipt inspected / corrected /
    voided / inspection cleared; shipment shipped; downtime started / resolved; backflush
    and material-allocation shortages / failures; CoC generation failures; and dispatch
    order changes. The full list is ``SERVER_ONLY_EVENT_TYPES`` in this module.

    ``occurred_at`` may be in the past (kept, normalized to UTC); a value later than the
    server's clock is stored as the current time, and one outside the representable
    range is 422. A ``work_order_id`` or ``operation_id`` from another company is 404.
    """
    if is_server_only_event_type(data.event_type):
        # Leave a trace of the attempt: a refused post writes no row anywhere else.
        logger.warning(
            "operational event post refused (server-only event type): company_id=%s user_id=%s event_type=%r",
            company_id,
            current_user.id,
            data.event_type,
        )
        raise HTTPException(status_code=400, detail=server_only_event_type_detail(data.event_type))
    try:
        occurred_at = clamp_occurred_at(data.occurred_at, datetime.now(timezone.utc))
    except OverflowError as exc:
        raise HTTPException(status_code=422, detail="occurred_at is outside the supported date range") from exc
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
