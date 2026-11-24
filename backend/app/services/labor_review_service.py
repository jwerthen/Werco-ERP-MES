"""Labor Review: list time entries with review flags, and set a forgotten clock-out.

Two entry points behind ``/shop-floor/time-entries`` (``app/api/endpoints/shop_floor.py``):

* :func:`list_time_entries` -- the tenant-scoped, paged read behind ``GET
  /shop-floor/time-entries``. Entries are attributed to production days through the
  company's production calendar (``production_calendar_service.attribute_time_entries``,
  the same reader every floor report uses), filtered, flagged and paged in Python.
  Pure read: no audit row, no event, no write.
* :func:`set_clock_out` -- ``POST /shop-floor/time-entries/{id}/set-clock-out``. A
  supervisor, signed in to the app as themselves, sets ANOTHER person's clock-out on an
  UNAPPROVED entry to the time that person actually stopped, with a reason. It closes an
  open entry or corrects a closed one. It changes HOURS only -- never pieces, scrap,
  reasons, notes, the entry type or the source (owner decision D5 was not answered; this is
  the plan's recommended default, see the route docstring).

Stored hours. ``operation.actual_setup_hours`` / ``actual_run_hours`` and
``work_order.actual_hours`` are only ever RAISED by reconcile
(``completion_cost_service.rollup_labor_hours_from_evidence``), so a correction that
lowers an entry's duration must lower them explicitly here or the three figures disagree.
The verb applies the change in this entry's own hours (``delta``) to the bucket the entry
belongs to (SETUP -> setup, everything else -> run, the split clock-out uses) and to the
work order, under these rules:

* closing an OPEN entry adds its hours exactly as ``POST /shop-floor/clock-out`` would --
  except on a terminal work order, where clock-out never accrues (G6-A) and neither does
  this;
* correcting a CLOSED entry applies the delta only when the operation's stored bucket is
  explained by time entries (``work_order_state_service.operation_hours_source`` is
  ``time_entries`` AND the stored bucket is at least the closed-entry sum, so this entry's
  hours are in it). Hours that did not come from time entries (``manual``), or a bucket
  below its own evidence (some closed hours were never rolled up -- e.g. a clock-out
  against an already-terminal work order -- so it cannot be told whether this entry's
  were), are left alone, and the response and audit row say so;
* a correction never RAISES hours on a terminal work order;
* nothing goes below zero.

When the work order has a ``JobCost`` its TIME_ENTRY labor lines are regenerated
(``job_costing_service.recompute_from_time_entries``), and when the labor-cost rollup is on
and the work order is COMPLETE its stored ``actual_cost`` is recomputed. One REQUIRED audit
row records the old and new clock-out, duration and hours, and the reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.orm.exc import StaleDataError

from app.api.audit_guard import required_audit
from app.core.time_utils import ensure_utc, to_utc_iso
from app.db.tenant_filter import tenant_query
from app.models.job_costing import JobCost
from app.models.time_entry import BASELINE_EXCLUDED_SOURCES, TimeEntry, TimeEntryType
from app.models.user import User, UserRole
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.schemas.labor_review import (
    HoursRederivation,
    LaborReviewEntry,
    LaborReviewListResponse,
    SetClockOutRequest,
    SetClockOutResponse,
)
from app.services.audit_service import AuditService
from app.services.completion_cost_service import compute_and_store_actual_cost
from app.services.job_costing_access_service import job_cost_query
from app.services.job_costing_access_service import snapshot as job_cost_snapshot
from app.services.job_costing_service import recompute_from_time_entries
from app.services.labor_cost_service import is_labor_cost_rollup_enabled
from app.services.production_calendar import (
    ProductionCalendar,
    current_production_day,
    entry_attribution,
    production_days_window_utc,
    shift_for,
)
from app.services.production_calendar_service import attribute_time_entries, load_production_calendar
from app.services.wallboard_service import LABOR_ENTRY_TYPES
from app.services.work_order_state_service import (
    TERMINAL_WO_STATUSES,
    operation_hours_source,
    operation_labor_evidence,
)

# --------------------------------------------------------------------------------------
# Roles and thresholds
# --------------------------------------------------------------------------------------

# Who may read Labor Review and approve / unapprove labor: the existing approver set
# (shop_floor._TIME_ENTRY_APPROVAL_ROLES).
LABOR_REVIEW_ROLES = [UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR, UserRole.QUALITY]
# Who may set another person's clock-out (owner decision D5 unanswered; the plan's
# recommended default): Admin, Manager and Supervisor. Quality reviews and approves labor
# but does not rewrite it.
SET_CLOCK_OUT_ROLES = [UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]

# Review-flag thresholds (defaults chosen for this build, not owner decisions).
LONG_ENTRY_HOURS = 12.0
LONG_ZERO_PIECES_HOURS = 4.0
# Entry types that book pieces, for the zero-pieces flag (a SETUP or INSPECTION entry with
# no pieces is normal).
_PIECE_BEARING_TYPES = (TimeEntryType.RUN, TimeEntryType.REWORK)

DEFAULT_RANGE_DAYS = 7
MAX_RANGE_DAYS = 62
MAX_PAGE_SIZE = 200
# A clock-out up to this far ahead of the server clock is accepted (client clock skew).
FUTURE_SKEW = timedelta(minutes=2)
_EPS = 1e-6

# --------------------------------------------------------------------------------------
# Refusal texts (quoted by docs/API.md and docs/RBAC_PERMISSIONS.md)
# --------------------------------------------------------------------------------------

LABOR_JUDGMENT_API_TOKEN_DETAIL = (
    "Approving labor, clearing an approval and setting a clock-out are a person's judgment: an API token "
    "cannot do them. Sign in to the app."
)
LABOR_REVIEW_KIOSK_DETAIL = "Labor review is a desk workflow: it is not available from a kiosk badge sign-in."
OWN_ENTRY_CLOCK_OUT_DETAIL = "You cannot set the clock-out on your own time entry"
APPROVED_ENTRY_CLOCK_OUT_DETAIL = "This time entry is approved. Clear its approval before correcting its clock-out."
OPEN_ENTRY_APPROVAL_DETAIL = "This time entry is still clocked in. Close the clock before approving it."
STALE_TIME_ENTRY_DETAIL = "This time entry was modified concurrently. Refresh and retry."
CLOCK_OUT_BEFORE_CLOCK_IN_DETAIL = "The clock-out must be after the entry's clock-in"
CLOCK_OUT_IN_FUTURE_DETAIL = "The clock-out cannot be in the future"
TIME_ENTRY_NOT_FOUND_DETAIL = "Time entry not found"
SET_CLOCK_OUT_AUDIT_DETAIL = "The audit record for this clock-out could not be saved; nothing was changed. Retry."
RANGE_TOO_LONG_DETAIL = f"A date range may cover at most {MAX_RANGE_DAYS} production days"


# --------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------


def _naive_utc(value: datetime) -> datetime:
    """A datetime as naive UTC (the column convention); naive input is read as UTC."""
    aware = ensure_utc(value)
    assert aware is not None
    return aware.astimezone(timezone.utc).replace(tzinfo=None)


def _enum_value(value) -> Optional[str]:
    if value is None:
        return None
    return value.value if hasattr(value, "value") else str(value)


def _is_privileged(user: User) -> bool:
    return bool(getattr(user, "is_superuser", False)) or user.role == UserRole.PLATFORM_ADMIN


def viewer_may_set_clock_out(user: User) -> bool:
    return _is_privileged(user) or user.role in SET_CLOCK_OUT_ROLES


def _entry_end(entry: TimeEntry, now: datetime) -> datetime:
    return entry.clock_out if entry.clock_out is not None else now


def _elapsed_hours(entry: TimeEntry, now: datetime) -> float:
    if entry.clock_out is not None:
        if entry.duration_hours is not None:
            return float(entry.duration_hours)
        return max(0.0, (entry.clock_out - entry.clock_in).total_seconds() / 3600)
    return max(0.0, (now - entry.clock_in).total_seconds() / 3600)


def _bucket_for(entry: TimeEntry) -> str:
    """The operation hours bucket clock-out credits: SETUP -> setup, everything else -> run."""
    return "setup" if entry.entry_type == TimeEntryType.SETUP else "run"


# --------------------------------------------------------------------------------------
# Flags
# --------------------------------------------------------------------------------------


def _overlapping_ids(entries: Iterable[TimeEntry], now: datetime) -> set[int]:
    """Ids of labor entries that overlap another labor entry of the SAME person.

    Breaks and downtime entries are not labor and never count. Intervals are half-open
    ``[clock_in, clock_out or now)``.
    """
    by_user: Dict[int, List[TimeEntry]] = {}
    for entry in entries:
        if entry.entry_type is not None and entry.entry_type not in LABOR_ENTRY_TYPES:
            continue
        by_user.setdefault(entry.user_id, []).append(entry)
    flagged: set[int] = set()
    for rows in by_user.values():
        rows.sort(key=lambda e: (e.clock_in, e.id))
        latest: Optional[TimeEntry] = None
        latest_end: Optional[datetime] = None
        for entry in rows:
            end = _entry_end(entry, now)
            if latest is not None and latest_end is not None and entry.clock_in < latest_end:
                flagged.add(entry.id)
                flagged.add(latest.id)
            if latest_end is None or end > latest_end:
                latest, latest_end = entry, end
    return flagged


def _flags_for(
    entry: TimeEntry,
    *,
    now: datetime,
    overlapping: set[int],
    started_shift_end: Optional[datetime],
) -> List[str]:
    flags: List[str] = []
    is_open = entry.clock_out is None
    elapsed = _elapsed_hours(entry, now)
    if is_open and started_shift_end is not None and now >= started_shift_end:
        flags.append("open_past_shift_end")
    if elapsed > LONG_ENTRY_HOURS:
        flags.append("long_entry")
    if entry.id in overlapping:
        flags.append("overlap")
    if is_open:
        operation = entry.operation
        work_order = entry.work_order
        if (operation is not None and operation.status == OperationStatus.COMPLETE) or (
            work_order is not None and work_order.status in TERMINAL_WO_STATUSES
        ):
            flags.append("open_on_closed_work")
    if (
        not is_open
        and entry.entry_type in _PIECE_BEARING_TYPES
        and elapsed >= LONG_ZERO_PIECES_HOURS
        and float(entry.quantity_produced or 0) <= 0
        and float(entry.quantity_scrapped or 0) <= 0
    ):
        flags.append("long_zero_pieces")
    if entry.source in BASELINE_EXCLUDED_SOURCES:
        flags.append("backfill")
    return flags


def _started_shift(entry: TimeEntry, calendar: ProductionCalendar):
    if not calendar.has_shifts:
        return None
    return shift_for(entry.clock_in, calendar, entry.work_center_id)


def _row(
    entry: TimeEntry,
    *,
    viewer: User,
    now: datetime,
    calendar: ProductionCalendar,
    overlapping: set[int],
    production_day: date,
    attributed_shift_name: Optional[str],
    approver_names: Dict[int, str],
) -> LaborReviewEntry:
    started = _started_shift(entry, calendar)
    started_end = started.end_utc if started is not None and started.end_utc > entry.clock_in else None
    flags = _flags_for(entry, now=now, overlapping=overlapping, started_shift_end=started_end)
    own = entry.user_id == viewer.id
    is_open = entry.clock_out is None
    user = entry.user
    operation = entry.operation
    work_order = entry.work_order
    return LaborReviewEntry(
        id=entry.id,
        version=int(entry.version or 1),
        user_id=entry.user_id,
        employee_name=user.full_name if user is not None else None,
        employee_id=user.employee_id if user is not None else None,
        work_order_id=entry.work_order_id,
        work_order_number=work_order.work_order_number if work_order is not None else None,
        work_order_status=_enum_value(work_order.status) if work_order is not None else None,
        operation_id=entry.operation_id,
        operation_number=operation.operation_number if operation is not None else None,
        operation_name=operation.name if operation is not None else None,
        operation_status=_enum_value(operation.status) if operation is not None else None,
        work_center_id=entry.work_center_id,
        work_center_name=entry.work_center.name if entry.work_center is not None else None,
        entry_type=_enum_value(entry.entry_type),
        source=entry.source,
        clock_in=entry.clock_in,
        clock_out=entry.clock_out,
        duration_hours=entry.duration_hours,
        elapsed_hours=round(_elapsed_hours(entry, now), 4),
        is_open=is_open,
        quantity_produced=float(entry.quantity_produced or 0),
        quantity_scrapped=float(entry.quantity_scrapped or 0),
        approved=entry.approved,
        approved_by=entry.approved_by,
        approved_by_name=approver_names.get(entry.approved_by) if entry.approved_by is not None else None,
        production_day=production_day,
        shift_name=attributed_shift_name,
        shift_end=started_end,
        flags=flags,  # type: ignore[arg-type]
        can_approve=(not own) and (not is_open) and entry.approved is None,
        can_unapprove=(not own) and entry.approved is not None,
        can_set_clock_out=(not own) and entry.approved is None and viewer_may_set_clock_out(viewer),
    )


# --------------------------------------------------------------------------------------
# The list read
# --------------------------------------------------------------------------------------


def _resolve_range(
    calendar: ProductionCalendar,
    start_date: Optional[date],
    end_date: Optional[date],
    status: Optional[str],
    now: datetime,
) -> Tuple[Optional[date], Optional[date]]:
    """The production-day range to read; ``(None, None)`` = every OPEN entry, whenever it started.

    No dates: the last ``DEFAULT_RANGE_DAYS`` production days ending today -- except with
    ``status=open``, which reads every open entry (a clock forgotten a week ago is exactly
    what the page exists to find). One date: the other defaults (start -> through today;
    end -> the ``DEFAULT_RANGE_DAYS`` ending there).
    """
    if start_date is None and end_date is None:
        if status == "open":
            return None, None
        end_date = current_production_day(calendar, now)
        return end_date - timedelta(days=DEFAULT_RANGE_DAYS - 1), end_date
    if start_date is None:
        assert end_date is not None
        start_date = end_date - timedelta(days=DEFAULT_RANGE_DAYS - 1)
    if end_date is None:
        end_date = max(start_date, current_production_day(calendar, now))
    try:
        production_days_window_utc(start_date, end_date, calendar)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if (end_date - start_date).days + 1 > MAX_RANGE_DAYS:
        raise HTTPException(status_code=422, detail=RANGE_TOO_LONG_DETAIL)
    return start_date, end_date


def _base_query(db: Session, company_id: int):
    return tenant_query(db, TimeEntry, company_id).options(
        joinedload(TimeEntry.user),
        joinedload(TimeEntry.work_order),
        joinedload(TimeEntry.operation),
        joinedload(TimeEntry.work_center),
    )


def _approver_names(db: Session, company_id: int, approver_ids: set) -> Dict[int, str]:
    """Names of the approvers: this company's users, plus a platform admin or superuser who
    approved while switched into it (their home company differs) -- the same widening the
    job-cost creator join uses (``job_costing_access_service.cost_entry_query``)."""
    if not approver_ids:
        return {}
    approvers = (
        db.query(User)
        .filter(
            User.id.in_(approver_ids),
            or_(User.company_id == company_id, User.role == UserRole.PLATFORM_ADMIN, User.is_superuser.is_(True)),
        )
        .all()
    )
    return {approver.id: approver.full_name for approver in approvers}


def _attributed(
    db: Session,
    company_id: int,
    calendar: ProductionCalendar,
    first_day: Optional[date],
    last_day: Optional[date],
    now: datetime,
) -> List[Tuple[TimeEntry, date, Optional[str]]]:
    """Every entry of the range with its production day and shift name (open entries run to now)."""
    if first_day is None or last_day is None:
        rows = _base_query(db, company_id).filter(TimeEntry.clock_out.is_(None)).all()
        result = []
        for entry in rows:
            day, occ = entry_attribution(entry.clock_in, None, calendar, entry.work_center_id, now=now)
            result.append((entry, day, occ.shift.name if occ is not None else None))
        return result
    attributed = attribute_time_entries(
        _base_query(db, company_id),
        calendar,
        first_day,
        last_day,
        clock_in_column=TimeEntry.clock_in,
        clock_out_column=TimeEntry.clock_out,
        now=now,
    )
    return [(entry, day, occ.shift.name if occ is not None else None) for entry, day, occ in attributed]


def list_time_entries(
    db: Session,
    company_id: int,
    viewer: User,
    *,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    user_id: Optional[int] = None,
    work_center_id: Optional[int] = None,
    work_order_id: Optional[int] = None,
    status: Optional[str] = None,
    approved: Optional[bool] = None,
    flag: Optional[str] = None,
    flagged_only: bool = False,
    page: int = 1,
    page_size: int = 50,
    now: Optional[datetime] = None,
) -> LaborReviewListResponse:
    """The Labor Review page's read. Tenant-scoped; pure (writes nothing)."""
    now = _naive_utc(now) if now is not None else datetime.utcnow()
    calendar = load_production_calendar(db, company_id)
    first_day, last_day = _resolve_range(calendar, start_date, end_date, status, now)

    attributed = _attributed(db, company_id, calendar, first_day, last_day, now)
    # Overlaps are judged over the whole range (every person), before the filters narrow it,
    # so filtering to one work center still flags an entry that overlaps the same person's
    # entry at another.
    overlapping = _overlapping_ids((entry for entry, _day, _shift in attributed), now)

    approver_ids = {entry.approved_by for entry, _d, _s in attributed if entry.approved_by is not None}
    approver_names = _approver_names(db, company_id, approver_ids)

    rows: List[LaborReviewEntry] = []
    for entry, day, shift_name in attributed:
        if user_id is not None and entry.user_id != user_id:
            continue
        if work_center_id is not None and entry.work_center_id != work_center_id:
            continue
        if work_order_id is not None and entry.work_order_id != work_order_id:
            continue
        if status == "open" and entry.clock_out is not None:
            continue
        if status == "closed" and entry.clock_out is None:
            continue
        if approved is True and entry.approved is None:
            continue
        if approved is False and entry.approved is not None:
            continue
        rows.append(
            _row(
                entry,
                viewer=viewer,
                now=now,
                calendar=calendar,
                overlapping=overlapping,
                production_day=day,
                attributed_shift_name=shift_name,
                approver_names=approver_names,
            )
        )
    # Chip counts are per-flag totals over the other filters, counted BEFORE the flag filters
    # narrow the rows, so clicking one chip does not turn every other chip into "both flags".
    flag_counts: Dict[str, int] = {}
    for row in rows:
        for value in row.flags:
            flag_counts[value] = flag_counts.get(value, 0) + 1

    if flag is not None:
        rows = [row for row in rows if flag in row.flags]
    if flagged_only:
        rows = [row for row in rows if row.flags]

    # Newest first; ties by id so paging is stable.
    rows.sort(key=lambda row: (_naive_utc(row.clock_in), row.id), reverse=True)
    page = max(1, page)
    page_size = max(1, min(page_size, MAX_PAGE_SIZE))
    offset = (page - 1) * page_size
    items = rows[offset : offset + page_size]
    return LaborReviewListResponse(
        items=items,
        total=len(rows),
        page=page,
        page_size=page_size,
        has_next=offset + page_size < len(rows),
        start_date=first_day,
        end_date=last_day,
        flag_counts=flag_counts,
        long_entry_hours=LONG_ENTRY_HOURS,
        long_zero_pieces_hours=LONG_ZERO_PIECES_HOURS,
    )


def entry_row(db: Session, company_id: int, viewer: User, entry: TimeEntry, *, now: Optional[datetime] = None):
    """One entry in the list's shape (the set-clock-out response)."""
    now = _naive_utc(now) if now is not None else datetime.utcnow()
    calendar = load_production_calendar(db, company_id)
    day, occ = entry_attribution(entry.clock_in, entry.clock_out, calendar, entry.work_center_id, now=now)
    siblings = (
        tenant_query(db, TimeEntry, company_id)
        .filter(
            TimeEntry.user_id == entry.user_id,
            TimeEntry.clock_in < _entry_end(entry, now),
            or_(TimeEntry.clock_out.is_(None), TimeEntry.clock_out > entry.clock_in),
        )
        .all()
    )
    overlapping = _overlapping_ids(siblings, now)
    approver_names = _approver_names(db, company_id, {entry.approved_by} if entry.approved_by is not None else set())
    return _row(
        entry,
        viewer=viewer,
        now=now,
        calendar=calendar,
        overlapping=overlapping,
        production_day=day,
        attributed_shift_name=occ.shift.name if occ is not None else None,
        approver_names=approver_names,
    )


# --------------------------------------------------------------------------------------
# The clock-out verb
# --------------------------------------------------------------------------------------


@dataclass
class _HoursPlan:
    applied: bool
    skipped_reason: Optional[str]
    operation_hours_before: Optional[float] = None
    operation_hours_after: Optional[float] = None
    work_order_hours_before: Optional[float] = None
    work_order_hours_after: Optional[float] = None


def _plan_hours(
    db: Session,
    company_id: int,
    entry: TimeEntry,
    operation: Optional[WorkOrderOperation],
    work_order: Optional[WorkOrder],
    *,
    was_open: bool,
    delta: float,
) -> _HoursPlan:
    """Decide (and return) the stored-hours change; reads evidence BEFORE the entry is mutated."""
    if work_order is None:
        return _HoursPlan(applied=False, skipped_reason="no_work_order")
    terminal = work_order.status in TERMINAL_WO_STATUSES
    wo_before = float(work_order.actual_hours or 0)
    bucket = _bucket_for(entry)

    if was_open and terminal:
        # clock-out never accrues onto a terminal work order (G6-A); nor does this.
        return _HoursPlan(applied=False, skipped_reason="terminal_work_order", work_order_hours_before=wo_before)
    if terminal and delta > 0:
        return _HoursPlan(applied=False, skipped_reason="terminal_work_order", work_order_hours_before=wo_before)
    if abs(delta) <= _EPS:
        return _HoursPlan(applied=False, skipped_reason="no_change", work_order_hours_before=wo_before)

    op_before: Optional[float] = None
    if operation is not None:
        op_before = float((operation.actual_setup_hours if bucket == "setup" else operation.actual_run_hours) or 0)
        if not was_open:
            evidence = operation_labor_evidence(db, company_id, [operation.id]).get(operation.id)
            source = operation_hours_source(operation, evidence)
            if source != "time_entries":
                return _HoursPlan(
                    applied=False,
                    skipped_reason=f"operation_hours_{source}",
                    operation_hours_before=op_before,
                    work_order_hours_before=wo_before,
                )
            evidence_bucket = 0.0
            if evidence is not None:
                evidence_bucket = evidence.closed_setup_hours if bucket == "setup" else evidence.closed_run_hours
            if op_before + _EPS < evidence_bucket:
                return _HoursPlan(
                    applied=False,
                    skipped_reason="operation_hours_below_evidence",
                    operation_hours_before=op_before,
                    work_order_hours_before=wo_before,
                )
        op_after = max(0.0, op_before + delta)
        applied_delta = op_after - op_before
        return _HoursPlan(
            applied=True,
            skipped_reason=None,
            operation_hours_before=op_before,
            operation_hours_after=op_after,
            work_order_hours_before=wo_before,
            work_order_hours_after=max(0.0, wo_before + applied_delta),
        )
    return _HoursPlan(
        applied=True,
        skipped_reason=None,
        work_order_hours_before=wo_before,
        work_order_hours_after=max(0.0, wo_before + delta),
    )


def _load_entry(db: Session, company_id: int, time_entry_id: int, *, lock: bool = False) -> Optional[TimeEntry]:
    query = tenant_query(db, TimeEntry, company_id).filter(TimeEntry.id == time_entry_id)
    if lock:
        query = query.populate_existing().with_for_update()
    return query.first()


def _refuse_entry_state(entry: Optional[TimeEntry], actor: User, version: int) -> TimeEntry:
    if entry is None:
        raise HTTPException(status_code=404, detail=TIME_ENTRY_NOT_FOUND_DETAIL)
    if entry.user_id == actor.id:
        raise HTTPException(status_code=403, detail=OWN_ENTRY_CLOCK_OUT_DETAIL)
    if entry.approved is not None:
        raise HTTPException(status_code=409, detail=APPROVED_ENTRY_CLOCK_OUT_DETAIL)
    if int(entry.version or 1) != int(version):
        raise HTTPException(status_code=409, detail=STALE_TIME_ENTRY_DETAIL)
    return entry


def set_clock_out(
    db: Session,
    company_id: int,
    actor: User,
    audit: AuditService,
    time_entry_id: int,
    payload: SetClockOutRequest,
    *,
    now: Optional[datetime] = None,
) -> SetClockOutResponse:
    """Set another person's clock-out on an unapproved entry (hours only). Commits.

    The caller (the route) has already applied the role gate and refused API tokens and
    kiosk badges. Refusals, in order, all before any write: 404 not found (tenant-scoped);
    403 own entry; 409 approved; 409 stale version; 422 a clock-out at or before the
    clock-in; 422 a clock-out in the future. Lock order is operation, then work order, then
    the time entry (the clock-out handler's order), and the entry's state is re-checked
    under the locks.
    """
    now = _naive_utc(now) if now is not None else datetime.utcnow()
    entry = _refuse_entry_state(_load_entry(db, company_id, time_entry_id), actor, payload.version)

    new_clock_out = _naive_utc(payload.clock_out)
    if new_clock_out <= entry.clock_in:
        raise HTTPException(status_code=422, detail=CLOCK_OUT_BEFORE_CLOCK_IN_DETAIL)
    if new_clock_out > now + FUTURE_SKEW:
        raise HTTPException(status_code=422, detail=CLOCK_OUT_IN_FUTURE_DETAIL)

    # Lock order: OPERATION, then WORK ORDER, then the entry.
    operation: Optional[WorkOrderOperation] = None
    if entry.operation_id is not None:
        operation = (
            tenant_query(db, WorkOrderOperation, company_id)
            .filter(WorkOrderOperation.id == entry.operation_id)
            .populate_existing()
            .with_for_update()
            .first()
        )
    work_order: Optional[WorkOrder] = None
    if entry.work_order_id is not None:
        # A soft-deleted work order still owns the hours its entries rolled up; the
        # correction follows the record (labor history), so no is_deleted filter here.
        work_order = (
            tenant_query(db, WorkOrder, company_id)
            .filter(WorkOrder.id == entry.work_order_id)
            .populate_existing()
            .with_for_update()
            .first()
        )
    entry = _refuse_entry_state(_load_entry(db, company_id, time_entry_id, lock=True), actor, payload.version)

    previous_clock_out = entry.clock_out
    previous_duration = entry.duration_hours
    was_open = previous_clock_out is None
    new_duration = (new_clock_out - entry.clock_in).total_seconds() / 3600

    if not was_open and previous_clock_out == new_clock_out:
        # Same time as already stored: nothing to change, no audit row.
        db.rollback()
        entry = _load_entry(db, company_id, time_entry_id)
        assert entry is not None
        return SetClockOutResponse(
            entry=entry_row(db, company_id, actor, entry, now=now),
            changed=False,
            previous_clock_out=previous_clock_out,
            previous_duration_hours=previous_duration,
        )

    old_contribution = 0.0 if was_open else float(previous_duration or 0)
    delta = new_duration - old_contribution
    plan = _plan_hours(db, company_id, entry, operation, work_order, was_open=was_open, delta=delta)
    bucket = _bucket_for(entry) if operation is not None else None

    # --- writes ----------------------------------------------------------------------
    entry.clock_out = new_clock_out
    entry.duration_hours = new_duration
    if plan.applied:
        if operation is not None and plan.operation_hours_after is not None:
            if bucket == "setup":
                operation.actual_setup_hours = plan.operation_hours_after
            else:
                operation.actual_run_hours = plan.operation_hours_after
        if work_order is not None and plan.work_order_hours_after is not None:
            work_order.actual_hours = plan.work_order_hours_after

    job_cost_recomputed = False
    actual_cost_recomputed = False
    job_cost_before: Optional[float] = None
    job_cost_after: Optional[float] = None
    actual_cost_before: Optional[float] = None
    actual_cost_after: Optional[float] = None
    job_cost_skipped_reason: Optional[str] = None
    job_cost_snapshot_before: Optional[dict] = None
    job_cost: Optional[JobCost] = None
    try:
        db.flush()
        if work_order is not None:
            # The job cost is resolved and LOCKED the way the job-cost module's own writer does
            # (job_costing_access_service.resolve_job_cost(for_write=True)): FOR UPDATE OF the
            # JobCost so a concurrent Recalculate cannot rebuild the same labor lines alongside
            # this one (CostEntry has no version, so the loser would leave duplicates), and
            # never on a soft-deleted work order, whose job cost that module refuses to write.
            # Taken after the operation / work order / entry locks and before the audit writes,
            # so the global audit-chain lock still comes last.
            job_cost = (
                job_cost_query(db, company_id)
                .filter(JobCost.work_order_id == work_order.id, WorkOrder.is_deleted.is_(False))
                .with_for_update(of=JobCost)
                .first()
            )
            if job_cost is None and work_order.is_deleted:
                job_cost_skipped_reason = "work_order_deleted"
            if job_cost is not None:
                job_cost_before = float(job_cost.actual_total_cost or 0)
                job_cost_snapshot_before = {
                    "job_cost": job_cost_snapshot(job_cost),
                    "entries": [job_cost_snapshot(e) for e in job_cost.entries],
                }
                recompute_from_time_entries(db, job_cost=job_cost, company_id=company_id, user_id=actor.id)
                job_cost_after = float(job_cost.actual_total_cost or 0)
                job_cost_recomputed = True
            if (
                plan.applied
                and work_order.status == WorkOrderStatus.COMPLETE
                and is_labor_cost_rollup_enabled(company_id)
            ):
                actual_cost_before = float(work_order.actual_cost or 0)
                compute_and_store_actual_cost(db, work_order, company_id)
                actual_cost_after = float(work_order.actual_cost or 0)
                actual_cost_recomputed = True

        old_values = {
            "clock_out": to_utc_iso(previous_clock_out),
            "duration_hours": previous_duration,
        }
        new_values = {
            "clock_out": to_utc_iso(new_clock_out),
            "duration_hours": round(new_duration, 6),
        }
        if plan.applied:
            if plan.operation_hours_after is not None and bucket is not None:
                old_values[f"operation_actual_{bucket}_hours"] = plan.operation_hours_before
                new_values[f"operation_actual_{bucket}_hours"] = plan.operation_hours_after
            if plan.work_order_hours_after is not None:
                old_values["work_order_actual_hours"] = plan.work_order_hours_before
                new_values["work_order_actual_hours"] = plan.work_order_hours_after
        if actual_cost_recomputed:
            old_values["work_order_actual_cost"] = actual_cost_before
            new_values["work_order_actual_cost"] = actual_cost_after
        verb = "Closed" if was_open else "Corrected"
        with required_audit(db, SET_CLOCK_OUT_AUDIT_DETAIL):
            audit.log_update(
                resource_type="time_entry",
                resource_id=entry.id,
                resource_identifier=str(entry.id),
                old_values=old_values,
                new_values=new_values,
                description=(
                    f"{verb} the clock-out of time entry {entry.id} (user {entry.user_id}) at "
                    f"{to_utc_iso(new_clock_out)}: {payload.reason}"
                ),
                action="time_entry_set_clock_out",
                extra_data={
                    "reason": payload.reason,
                    "closed_open_entry": was_open,
                    "entry_user_id": entry.user_id,
                    "work_order_id": entry.work_order_id,
                    "operation_id": entry.operation_id,
                    "hours_delta": round(delta, 6),
                    "hours_rederived": plan.applied,
                    "hours_skipped_reason": plan.skipped_reason,
                    "job_cost_recomputed": job_cost_recomputed,
                    "job_cost_actual_total_before": job_cost_before,
                    "job_cost_actual_total_after": job_cost_after,
                    "job_cost_skipped_reason": job_cost_skipped_reason,
                },
                required=True,
            )
            if job_cost is not None and job_cost_recomputed:
                # The rebuilt labor lines are a job-cost mutation: the same required
                # RECALCULATE evidence (before/after job cost and every line) the job-cost
                # module's own recalculate writes, in this transaction.
                audit.log_required(
                    "RECALCULATE",
                    "job_cost",
                    resource_id=job_cost.id,
                    resource_identifier=str(job_cost.id),
                    old_values=job_cost_snapshot_before,
                    new_values={
                        "job_cost": job_cost_snapshot(job_cost),
                        "entries": [job_cost_snapshot(e) for e in job_cost.entries],
                    },
                    company_id=company_id,
                    extra_data={"trigger": "time_entry_set_clock_out", "time_entry_id": entry.id},
                )
        db.commit()
    except StaleDataError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=STALE_TIME_ENTRY_DETAIL) from exc

    db.refresh(entry)
    return SetClockOutResponse(
        entry=entry_row(db, company_id, actor, entry, now=now),
        changed=True,
        previous_clock_out=previous_clock_out,
        previous_duration_hours=previous_duration,
        hours=HoursRederivation(
            applied=plan.applied,
            skipped_reason=plan.skipped_reason,
            hours_delta=round(delta, 6),
            operation_bucket=bucket,  # type: ignore[arg-type]
            operation_hours_before=plan.operation_hours_before,
            operation_hours_after=plan.operation_hours_after if plan.applied else plan.operation_hours_before,
            work_order_hours_before=plan.work_order_hours_before,
            work_order_hours_after=(plan.work_order_hours_after if plan.applied else plan.work_order_hours_before),
            job_cost_recomputed=job_cost_recomputed,
            actual_cost_recomputed=actual_cost_recomputed,
        ),
    )
