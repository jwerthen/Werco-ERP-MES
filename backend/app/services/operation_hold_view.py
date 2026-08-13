"""WHY an operation is on hold, WHO placed it and WHEN -- batched, read-only.

The kiosk can now SEE a held operation (the ``held`` list on
``GET /shop-floor/work-center-queue/{id}``), and "this job is on hold" is not
enough to act on: the operator needs the reason, so an accidental hold reads as
an accident and a real quality/material stop reads as a stop. This module is the
one place that assembles that context.

WHERE THE FACTS COME FROM. There is no ``held_by`` / ``held_at`` column on
``work_order_operations`` -- the hold paths never persisted one -- so provenance
is reconstructed from the two records they DO write, and which one exists depends
on which path placed the hold:

* ``PUT /shop-floor/operations/{id}/hold`` WITH a note or a non-OTHER category
  creates a ``WorkOrderBlocker`` (``reported_by`` / ``reported_at``) and emits no
  ``operation_hold`` event.
* The same endpoint with a BARE hold -- no note, category OTHER, which is exactly
  the accidental fat-finger case this feature exists for -- emits an
  ``operation_hold`` ``OperationalEvent`` (``user_id`` / ``occurred_at``) and
  creates no blocker.
* ``WorkOrderBlockerService.create_blocker(put_operation_on_hold=True)`` (the
  blocker API, the kiosk's OOT->NCR one-tap hold) creates the blocker only.

MOST RECENT WINS. When both records exist they can disagree -- a blocker opened
days ago on an operation that was held bare an hour ago -- so the actor/timestamp
comes from whichever record is NEWER, which is the one that describes the hold
currently in force. The ``blocker`` field is independent of that choice: it is
always the newest still-open blocker if there is one, because that is the reason
text the operator has to read, whoever pressed hold last.

DELIBERATELY ABSENT: the ``audit_log``. It also records every hold, but it is the
tamper-evident chain, not a display source, and nothing outside ``AuditService``
reads it to render a screen. When neither a blocker nor an event exists (a hold
placed before either record was written) the fields are simply ``None`` -- this
module reports what was recorded and never infers a holder from, say,
``operation.updated_at``, which any later write moves.

PURE READ. No writes, no audit rows, no events -- it is called from a poll, and a
poll is not an actor. Two batched queries for the whole held set, regardless of
how many operations are on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Sequence

from sqlalchemy.orm import Session, joinedload

from app.core.time_utils import ensure_utc
from app.models.operational_event import OperationalEvent
from app.models.work_order_blocker import WorkOrderBlocker, WorkOrderBlockerStatus
from app.services.wallboard_service import operator_display_name

# The event a bare hold emits (shop_floor.hold_operation, else-branch).
HOLD_EVENT_TYPE = "operation_hold"

# A blocker still "explains" the hold while it is OPEN or ACKNOWLEDGED. RESOLVED
# / DISMISSED blockers are excluded on purpose: the resolve/dismiss flow is what
# auto-resumes an operation, so a closed blocker on a still-held operation is
# stale narrative, not the current reason. Same status pair the resume endpoint
# warns on (shop_floor.resume_operation), so the reason the kiosk shows BEFORE
# resuming and the warning it gets back AFTER resuming describe the same rows.
OPEN_BLOCKER_STATUSES = (WorkOrderBlockerStatus.OPEN.value, WorkOrderBlockerStatus.ACKNOWLEDGED.value)


@dataclass(frozen=True)
class HoldBlockerView:
    """The still-open blocker that explains a hold, projected for display."""

    id: int
    category: Optional[str]
    severity: Optional[str]
    status: Optional[str]
    title: Optional[str]
    note: Optional[str]
    reported_at: Optional[datetime]
    reported_by_user_id: Optional[int]
    reported_by_name: Optional[str]


@dataclass(frozen=True)
class HoldContext:
    """Who placed a hold, when, and the open blocker explaining it (if any)."""

    held_at: Optional[datetime] = None
    held_by_user_id: Optional[int] = None
    held_by_name: Optional[str] = None
    blocker: Optional[HoldBlockerView] = None


def _newest_blocker_per_operation(
    db: Session, company_id: int, operation_ids: Sequence[int]
) -> Dict[int, WorkOrderBlocker]:
    """Newest still-open blocker per operation, tenant-scoped. One query."""
    rows: List[WorkOrderBlocker] = (
        db.query(WorkOrderBlocker)
        .options(joinedload(WorkOrderBlocker.reporter))
        .filter(
            WorkOrderBlocker.company_id == company_id,
            WorkOrderBlocker.operation_id.in_(list(operation_ids)),
            WorkOrderBlocker.status.in_(OPEN_BLOCKER_STATUSES),
        )
        # Ascending, so the last write into the dict below is the newest row --
        # cheaper and more portable than a per-operation window function, and the
        # held set is capped (dispatch_service.MAX_HELD_OPERATIONS).
        .order_by(WorkOrderBlocker.reported_at.asc(), WorkOrderBlocker.id.asc())
        .all()
    )
    newest: Dict[int, WorkOrderBlocker] = {}
    for row in rows:
        if row.operation_id is not None:
            newest[row.operation_id] = row
    return newest


def _newest_hold_event_per_operation(
    db: Session, company_id: int, operation_ids: Sequence[int]
) -> Dict[int, OperationalEvent]:
    """Newest ``operation_hold`` event per operation, tenant-scoped. One query."""
    rows: List[OperationalEvent] = (
        db.query(OperationalEvent)
        .options(joinedload(OperationalEvent.user))
        .filter(
            OperationalEvent.company_id == company_id,
            OperationalEvent.event_type == HOLD_EVENT_TYPE,
            OperationalEvent.operation_id.in_(list(operation_ids)),
        )
        .order_by(OperationalEvent.occurred_at.asc(), OperationalEvent.id.asc())
        .all()
    )
    newest: Dict[int, OperationalEvent] = {}
    for row in rows:
        if row.operation_id is not None:
            newest[row.operation_id] = row
    return newest


def hold_contexts_for_operations(
    db: Session, *, company_id: int, operation_ids: Sequence[int]
) -> Dict[int, HoldContext]:
    """Map operation id -> :class:`HoldContext` for every id asked about.

    Every requested id gets a key, so a caller never has to branch on presence;
    an operation with neither record maps to an all-``None`` context. Tenant
    scope is the caller's ACTIVE company (the station's own row for a kiosk),
    never client input -- a cross-tenant operation id can therefore only ever
    come back empty, never populated from another company's blockers or events.
    """
    ids = [op_id for op_id in operation_ids if op_id is not None]
    if not ids:
        return {}

    blockers = _newest_blocker_per_operation(db, company_id, ids)
    events = _newest_hold_event_per_operation(db, company_id, ids)

    contexts: Dict[int, HoldContext] = {}
    for operation_id in ids:
        blocker = blockers.get(operation_id)
        event = events.get(operation_id)

        blocker_view = None
        if blocker is not None:
            reporter = blocker.reporter
            blocker_view = HoldBlockerView(
                id=blocker.id,
                category=blocker.category,
                severity=blocker.severity,
                status=blocker.status,
                title=blocker.title,
                note=blocker.note,
                reported_at=blocker.reported_at,
                reported_by_user_id=blocker.reported_by,
                reported_by_name=(
                    operator_display_name(reporter.first_name, reporter.last_name) if reporter is not None else None
                ),
            )

        # Most recent wins (see the module docstring). A record carrying no
        # timestamp loses to one that has a timestamp, and to nothing else.
        # Compared through ``ensure_utc``: both columns are ``DateTime(timezone=True)``,
        # so a naive value from one dialect and an aware value from the other would
        # raise TypeError on ``>`` -- and this runs inside the shop's most-polled
        # read, where that is a 500 on every kiosk in the building, not a log line.
        blocker_at = blocker.reported_at if blocker is not None else None
        event_at = event.occurred_at if event is not None else None
        use_event = False
        if event is not None:
            if blocker is None or blocker_at is None:
                use_event = True
            elif event_at is not None and ensure_utc(event_at) > ensure_utc(blocker_at):
                use_event = True

        if use_event and event is not None:
            actor = event.user
            contexts[operation_id] = HoldContext(
                held_at=event_at,
                held_by_user_id=event.user_id,
                held_by_name=(operator_display_name(actor.first_name, actor.last_name) if actor is not None else None),
                blocker=blocker_view,
            )
        elif blocker is not None:
            contexts[operation_id] = HoldContext(
                held_at=blocker_at,
                held_by_user_id=blocker.reported_by,
                held_by_name=blocker_view.reported_by_name if blocker_view is not None else None,
                blocker=blocker_view,
            )
        else:
            contexts[operation_id] = HoldContext()

    return contexts
