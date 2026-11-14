"""The production calendar: when a production day starts, which shifts run, at which work centers.

A production day starts at the company's configured production-day start on the local
clock (``Company.timezone``, default America/Chicago) and a shift that runs past midnight
belongs to the day it started -- so a 01:30 Wednesday weld entry is Tuesday's 2nd shift.
OEE, the wallboard "today" pulse, the shop-floor dashboard's ``completed_today``, the
daily-output / employee-time reports, downtime date filters, job-cost labor dates and the
analytics OEE read production days. With nothing configured the production day is the
company-local calendar day and there are no shifts.

Thin routes over ``services/production_calendar_service``; every route is tenant-scoped
through ``get_current_company_id``.
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.audit_guard import required_audit
from app.api.deps import get_audit_service, get_current_company_id, get_current_user, require_role
from app.core.cache import invalidate_work_centers_cache
from app.core.realtime import safe_broadcast
from app.core.websocket import broadcast_dashboard_update
from app.db.database import get_db
from app.models.user import User, UserRole
from app.schemas.production_calendar import (
    ProductionCalendarResponse,
    ProductionCalendarUpdate,
    ProductionMomentResponse,
    WorkingCalendarSyncResponse,
)
from app.services.audit_service import AuditService
from app.services.production_calendar_service import (
    STALE_VERSION_DETAIL,
    calendar_response,
    resolve_moment,
    save_production_calendar,
    sync_working_calendars,
)

router = APIRouter()

# Editing the calendar changes how every production figure is bucketed: ADMIN only.
CALENDAR_EDIT_ROLES = [UserRole.ADMIN]
# Same roles as PUT /scheduling/work-centers/{id}/calendar, whose rows the sync writes.
WORKING_CALENDAR_SYNC_ROLES = [UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]

CALENDAR_AUDIT_FAILED_DETAIL = (
    "The production calendar could not be saved because its audit record failed; nothing was changed. Retry."
)
SYNC_AUDIT_FAILED_DETAIL = (
    "The working calendars could not be synced because an audit record failed; nothing was changed. Retry."
)
CALENDAR_CONFLICT_DETAIL = (
    "The production calendar could not be saved because a record it changes was changed at the same time "
    "(for example, an OEE record started using a shift this save removes); nothing was saved. Reload and retry."
)


def _is_first_save_race(exc: IntegrityError) -> bool:
    """True only for the one-calendar-per-company unique key (Postgres names it, SQLite names the column)."""
    orig = getattr(exc, "orig", None)
    haystack = str(orig) if orig is not None else str(exc)
    return "uq_production_calendar_company" in haystack or "production_calendar_settings.company_id" in haystack


@router.get("", response_model=ProductionCalendarResponse)
def get_production_calendar(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Read the production calendar: day start, shift patterns with their work centers and shifts.

    ``today`` is the current production day with every active shift of every pattern on it
    as UTC instants, so a client never does time-zone math. ``configured: false`` means no
    calendar exists yet (production day = company-local calendar day, no shifts).
    ``planned_minutes`` is each shift's planned production time (breaks and lunch
    excluded); the ERP's own OEE keeps its staffed-time availability convention.
    """
    return calendar_response(db, company_id)


@router.put("", response_model=ProductionCalendarResponse)
def save_production_calendar_document(
    payload: ProductionCalendarUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(CALENDAR_EDIT_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Replace the WHOLE production calendar (Admin only) after a current-version check.

    Send ``expected_version`` as read (0 before the first save): a stale one is 409. The
    document is validated as a set (422): one default pattern at most, a work center in one
    pattern at most, unique shift codes, every shift inside its production day (starts at
    or after the day start, ends by the next one). Shifts and patterns keep their ``id``;
    anything omitted is removed -- except a shift an OEE record uses, which must be retired
    with ``is_active: false`` and keeps its code. An identical document is a no-op. Writes a
    required audit row (503, nothing saved, when it cannot). Does NOT change working
    calendars: run the working-calendar sync for that.
    """
    try:
        with required_audit(db, CALENDAR_AUDIT_FAILED_DETAIL):
            response = save_production_calendar(db, company_id, current_user.id, payload, audit)
            db.commit()
    except IntegrityError as exc:
        db.rollback()
        if _is_first_save_race(exc):
            # Two first saves raced past the version check; the unique company key refused one.
            raise HTTPException(409, STALE_VERSION_DETAIL) from exc
        # Anything else is a concurrent write to a row the save touches (an OEE record
        # that started using a shift this save removes): not a calendar version change.
        raise HTTPException(409, CALENDAR_CONFLICT_DETAIL) from exc
    except Exception:
        db.rollback()
        raise
    safe_broadcast(broadcast_dashboard_update, {"event": "production_calendar_updated"}, company_id=company_id)
    return response


@router.get("/resolve", response_model=ProductionMomentResponse)
def resolve_production_moment(
    at: Optional[datetime] = Query(None, description="ISO-8601 instant; an offset-less value is UTC. Default: now"),
    work_center_id: Optional[int] = Query(
        None, description="Use this work center's shift pattern (default: the default pattern)"
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Say which production day and shift an instant belongs to at a work center.

    In an overlap (a 15:30-16:00 hand-off) ``shift`` is the OUTGOING (earlier-start) shift
    and ``overlapping`` lists both. A work center that is not this company's is 404.
    """
    return resolve_moment(db, company_id, at, work_center_id)


@router.post("/working-calendars/sync", response_model=WorkingCalendarSyncResponse)
def sync_working_calendars_from_shifts(
    dry_run: bool = Query(True, description="true (default) only reports the changes; false writes them"),
    expected_version: Optional[int] = Query(
        None, description="The production calendar `version` the dry run showed; a different one is 409"
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(WORKING_CALENDAR_SYNC_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Set each work center's weekly working hours from its shifts (dry run by default).

    For every active work center covered by a shift pattern (its own or the default), the
    new Mon..Sun hours are the planned minutes of the active shifts starting each day
    (Werco weld: 20 h Mon-Fri, rest of shop: 16 h). ``dry_run=true`` writes nothing and
    shows current vs new per work center. ``dry_run=false`` writes every changed one through
    the scheduling calendar's own writer -- dated overrides kept, version bumped, one audit
    row per work center (503, nothing saved, when one cannot be written). Scheduling,
    capacity and delivery projections move with the new hours.
    """
    if dry_run:
        response, _ = sync_working_calendars(
            db, company_id, current_user.id, dry_run=True, expected_version=expected_version, audit=audit
        )
        return response
    try:
        with required_audit(db, SYNC_AUDIT_FAILED_DETAIL):
            response, saves = sync_working_calendars(
                db, company_id, current_user.id, dry_run=False, expected_version=expected_version, audit=audit
            )
            db.commit()
    except Exception:
        db.rollback()
        raise
    for saved in saves:
        invalidate_work_centers_cache(saved.center.id)
        safe_broadcast(
            broadcast_dashboard_update,
            {"event": "working_calendar_updated", "work_center_id": saved.center.id},
            company_id=company_id,
        )
    return response
