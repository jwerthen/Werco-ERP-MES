"""Read-only payload builder for the shop-floor TV wallboard (A0.5).

One call returns the whole board: the JOB WALL (open work-order tiles with
their current operation — the main wall since the 2026-07-15 owner feedback),
per-work-center live state (who's on it, what WO/op, elapsed time, queue
depth, blockers, downtime — kept for old TV bundles and the exception rail),
the late / blocked work-order rails with true uncapped totals, and the
plant-wide ship / today / quality blocks. The trailing-30d ``kpi_strip`` was
dropped from the TV — the response field survives for wire back-compat but is
always None and nothing computes it anymore.

DELIBERATELY READ-ONLY: unlike the interactive /shop-floor/dashboard, this
builder runs NO reconcile-on-read and writes NOTHING (no audit rows, no
events). Display tokens have no user identity to attribute writes to, and a
wall-mounted TV polling every 30s must never mutate state. Tenant scoping is
the caller's company_id (derived from the user token or the display_tokens
row — never from client input).

PRIVACY: operator identity is truncated to "First L." — this renders on a
public screen. The ship/today/quality blocks carry counts, ages, WO/part
numbers and dates ONLY: no customer names, no ship-to addresses, no dollar
figures, no NCR titles/descriptions.

"Late" everywhere on the board means: promise date — coalesce(must_ship_by,
due_date), the OTD precedence — strictly before today's CENTRAL date, on a
non-terminal, non-deleted WO. ``_late_wo_filters`` is the single predicate
shared by the late_wos list, late_total, and the per-job ``is_late`` flag so
they cannot drift.

``operation_counts_by_work_center`` is shared with the /shop-floor/dashboard
handler so the two surfaces can't drift on what "active/queued" means.
"""

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import case, func, or_
from sqlalchemy.orm import Session, joinedload

from app.core.time_utils import CENTRAL_TIME_ZONE
from app.models.downtime import DowntimeEvent
from app.models.purchasing import POReceipt
from app.models.quality import NCRStatus, NonConformanceReport
from app.models.time_entry import BASELINE_EXCLUDED_SOURCES, TimeEntry, TimeEntryType
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.models.work_order_blocker import WorkOrderBlocker, WorkOrderBlockerStatus
from app.schemas.wallboard import (
    WallboardActiveJob,
    WallboardBlockedWorkOrder,
    WallboardDowntime,
    WallboardJob,
    WallboardJobOp,
    WallboardLateWorkOrder,
    WallboardQuality,
    WallboardResponse,
    WallboardShip,
    WallboardShipRow,
    WallboardToday,
    WallboardWorkCenter,
)
from app.services.work_order_state_service import operation_target_quantity

logger = logging.getLogger(__name__)

# Blocker states that still block (RESOLVED / DISMISSED do not).
_UNRESOLVED_BLOCKER_STATUSES = [WorkOrderBlockerStatus.OPEN.value, WorkOrderBlockerStatus.ACKNOWLEDGED.value]

# Work orders in these states are off the board everywhere (counts, blockers, tickers).
_TERMINAL_WO_STATUSES = [WorkOrderStatus.COMPLETE, WorkOrderStatus.CLOSED, WorkOrderStatus.CANCELLED]

# Entry types that represent labor on an operation. Open BREAK/DOWNTIME entries
# are clocked time but not jobs — they must not render as ghost rows on the TV.
# Public: the crew-kiosk roster (shop_floor work-center-queue) shares this so
# the two surfaces can never silently diverge on what counts as crew.
LABOR_ENTRY_TYPES = [TimeEntryType.SETUP, TimeEntryType.RUN, TimeEntryType.REWORK, TimeEntryType.INSPECTION]

# Cap the late/blocked rails. The rail renders a fixed panel (not a cycling
# ticker), so 12 rows is the readable maximum; the true uncapped counts ride
# separately as late_total / blocked_total.
_TICKER_LIMIT = 12

# Bound the "next due to ship" forward scan: promised-but-unshipped WOs are
# resolved in promise order, so 200 rows is far past any realistic backlog
# before the first not-fully-shipped promise date is found.
_NEXT_DUE_SCAN_LIMIT = 200

# The job wall renders WORK ORDERS (owner feedback 2026-07-15). Population:
# open (RELEASED / IN_PROGRESS) WOs only. ON_HOLD is deliberately EXCLUDED
# from the wall — the quality rail already counts holds; DRAFT and terminal
# statuses are off the board like everywhere else.
_JOB_WALL_WO_STATUSES = [WorkOrderStatus.RELEASED, WorkOrderStatus.IN_PROGRESS]

# Cap the job wall at a readable TV grid; jobs_total rides separately as the
# true uncapped count for the "+N more" affordance.
_JOB_WALL_LIMIT = 24

# current-op precedence lives in _current_operation: IN_PROGRESS with open
# labor > IN_PROGRESS > READY > PENDING, lowest sequence within each class.


def operator_display_name(first_name: Optional[str], last_name: Optional[str]) -> Optional[str]:
    """Public-screen-safe operator name: first name + last initial ("Jon W.")."""
    first = (first_name or "").strip()
    last = (last_name or "").strip()
    if first and last:
        return f"{first} {last[0].upper()}."
    return first or None


def _elapsed_minutes(since: Optional[datetime], now: datetime) -> int:
    if since is None:
        return 0
    reference = now
    if since.tzinfo is not None and reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    elif since.tzinfo is None and reference.tzinfo is not None:
        reference = reference.replace(tzinfo=None)
    return max(int((reference - since).total_seconds() // 60), 0)


def _crew_summary(entries: list[TimeEntry], now: datetime) -> tuple[list[str], int, int, TimeEntry]:
    """Crew facts for one operation's open labor entries.

    Returns (crew names in clock-in order capped at 3, TRUE headcount with
    duplicate open entries by one operator counted once, elapsed minutes from
    the EARLIEST open clock_in, the earliest entry itself). Shared by the
    machine wall's active_jobs rows and the job wall's current_op so the two
    can never drift on what "crew" means. ``entries`` must be non-empty.
    """
    ordered = sorted(entries, key=lambda item: (item.clock_in is None, item.clock_in))
    first = ordered[0]
    crew: list[str] = []
    seen_user_ids: set[Optional[int]] = set()
    for entry in ordered:
        if entry.user_id in seen_user_ids:
            continue  # duplicate open entries by one operator are one head
        seen_user_ids.add(entry.user_id)
        name = operator_display_name(entry.user.first_name, entry.user.last_name) if entry.user else None
        if name and len(crew) < 3:
            crew.append(name)
    return crew, len(seen_user_ids), _elapsed_minutes(first.clock_in, now), first


def _current_operation(
    operations: list[WorkOrderOperation], labor_op_ids: Optional[set[int]] = None
) -> Optional[WorkOrderOperation]:
    """THE current op of a WO for the job wall: lowest-sequence IN_PROGRESS,
    else lowest READY, else lowest PENDING; None when all are complete (or
    the WO has no routed operations).

    Among IN_PROGRESS candidates, ops carrying OPEN LABOR (``labor_op_ids``)
    win first: overlapping in-progress ops are permitted, and pinning to an
    idle earlier op would render the tile WAITING with no crew while people
    are actively working the WO — the exact question the wall exists to
    answer ("what op is it on, who's on it").
    """
    in_progress = [op for op in operations if op.status == OperationStatus.IN_PROGRESS]
    if labor_op_ids:
        worked = [op for op in in_progress if op.id in labor_op_ids]
        if worked:
            return min(worked, key=lambda op: (op.sequence is None, op.sequence, op.id))
    if in_progress:
        return min(in_progress, key=lambda op: (op.sequence is None, op.sequence, op.id))
    for wanted in (OperationStatus.READY, OperationStatus.PENDING):
        candidates = [op for op in operations if op.status == wanted]
        if candidates:
            return min(candidates, key=lambda op: (op.sequence is None, op.sequence, op.id))
    return None


def _work_center_type_norm(work_center: Optional[WorkCenter]) -> Optional[str]:
    """Lowercased work_center_type, the Python mirror of the SQL
    ``func.lower(WorkCenter.work_center_type)`` dept comparisons."""
    if work_center is None or work_center.work_center_type is None:
        return None
    raw = work_center.work_center_type
    value = raw.value if hasattr(raw, "value") else str(raw)
    return value.strip().lower()


def _job_sort_key(job: WallboardJob):
    """Deterministic job-wall priority: blocked/down first, then late (worst
    days_late first), then running, then the rest by promise_date asc (nulls
    last); wo_number breaks every tie."""
    return (
        0 if (job.blocked or job.down) else 1,
        -job.days_late,
        0 if job.running else 1,
        job.promise_date is None,  # nulls last
        job.promise_date or date.max,
        job.wo_number,
    )


def _build_job_wall(
    wall_wos: list[WorkOrder],
    ops_by_wo: dict[int, list[WorkOrderOperation]],
    entries_by_operation: dict[int, list[TimeEntry]],
    blocked_wo_ids: set[int],
    down_wc_ids: set[int],
    late_wo_ids: set[int],
    dept_norm: Optional[str],
    central_today: date,
    now: datetime,
) -> tuple[list[WallboardJob], int]:
    """Assemble the job wall from pre-fetched rows — pure Python, ZERO queries.

    PRIVACY: a WallboardJob carries NO customer_name (the WorkOrder model has
    that column — it must never reach the public TV), no dollars, no notes.

    When ``dept_norm`` is set, a job belongs to the dept TV via its CURRENT
    op's work-center type (a WO with no current op drops off dept boards).
    Returns (priority-sorted list capped at _JOB_WALL_LIMIT, true count).
    """
    jobs: list[WallboardJob] = []
    labor_op_ids = set(entries_by_operation.keys())
    for wo in wall_wos:
        operations = ops_by_wo.get(wo.id, [])
        current = _current_operation(operations, labor_op_ids)
        current_wc = current.work_center if current is not None else None
        if dept_norm is not None and _work_center_type_norm(current_wc) != dept_norm:
            continue

        running = False
        current_op: Optional[WallboardJobOp] = None
        if current is not None:
            crew: list[str] = []
            crew_count = 0
            elapsed = 0
            op_entries = entries_by_operation.get(current.id, [])
            if op_entries:
                running = True
                crew, crew_count, elapsed, _first = _crew_summary(op_entries, now)
            current_op = WallboardJobOp(
                sequence=current.sequence,
                name=current.name,
                work_center_code=current_wc.code if current_wc else None,
                work_center_name=current_wc.name if current_wc else None,
                status=current.status.value if hasattr(current.status, "value") else str(current.status),
                qty_done=float(current.quantity_complete or 0),
                qty_target=operation_target_quantity(current, wo),
                crew=crew,
                crew_count=crew_count,
                elapsed_minutes=elapsed,
            )

        # Lateness comes from the SAME predicate as the late rail (the ids in
        # ``late_wo_ids`` were selected via _late_wo_filters); days_late via
        # the shared promise helper so the tile and rail can never disagree.
        is_late = wo.id in late_wo_ids
        promise = work_order_promise_date(wo)
        days_late = max((central_today - promise).days, 0) if (is_late and promise is not None) else 0

        jobs.append(
            WallboardJob(
                wo_number=wo.work_order_number,
                part_number=wo.part.part_number if wo.part else None,
                status=wo.status.value if hasattr(wo.status, "value") else str(wo.status),
                qty_complete=float(wo.quantity_complete or 0),
                qty_ordered=float(wo.quantity_ordered or 0),
                promise_date=promise,
                is_late=is_late,
                days_late=days_late,
                blocked=wo.id in blocked_wo_ids,
                down=bool(current is not None and current.work_center_id in down_wc_ids),
                running=running,
                current_op=current_op,
                ops_completed=sum(1 for op in operations if op.status == OperationStatus.COMPLETE),
                ops_total=len(operations),
            )
        )

    jobs.sort(key=_job_sort_key)
    return jobs[:_JOB_WALL_LIMIT], len(jobs)


def _central_today() -> date:
    """Today's date on the SHOP's wall clock (America/Chicago), never naive
    date.today() — a UTC server flips dates at 6/7pm Central otherwise."""
    return datetime.now(CENTRAL_TIME_ZONE).date()


def central_day_window_utc() -> tuple[datetime, datetime]:
    """(Central midnight, now) as NAIVE-UTC datetimes for DateTime columns.

    The shop_floor dashboard "completed today" boundary pattern; public so
    tests can seed rows that are guaranteed inside the live window.
    """
    central_now = datetime.now(CENTRAL_TIME_ZONE)
    central_day_start = central_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return (
        central_day_start.astimezone(timezone.utc).replace(tzinfo=None),
        central_now.astimezone(timezone.utc).replace(tzinfo=None),
    )


def _promise_expr():
    """SQL promise-date expression: coalesce(must_ship_by, due_date).

    The OTD precedence (AnalyticsService._work_order_promise) — the wallboard
    must agree with the analytics on what "the promise" means.
    """
    return func.coalesce(WorkOrder.must_ship_by, WorkOrder.due_date)


def work_order_promise_date(wo: WorkOrder) -> Optional[date]:
    """Python mirror of ``_promise_expr()``: must_ship_by || due_date."""
    return wo.must_ship_by if wo.must_ship_by is not None else wo.due_date


def _late_wo_filters(company_id: int, central_today: date) -> list:
    """THE lateness predicate: promise < Central today on a live, non-terminal WO.

    Single source shared by the late_wos rail, late_total, AND the per-job
    ``is_late`` flag (which applies these filters to the active jobs' WO ids)
    so the three can never drift.
    """
    return [
        WorkOrder.company_id == company_id,
        WorkOrder.is_deleted == False,  # noqa: E712
        WorkOrder.status.not_in(_TERMINAL_WO_STATUSES),
        _promise_expr().isnot(None),
        _promise_expr() < central_today,
    ]


def _dept_open_op_wo_ids(db: Session, company_id: int, dept_norm: str):
    """Subquery: WO ids with >=1 open (non-COMPLETE) operation routed to a
    work center of the given type — how a late WO "belongs" to a dept TV."""
    return (
        db.query(WorkOrderOperation.work_order_id)
        .join(WorkCenter, WorkCenter.id == WorkOrderOperation.work_center_id)
        .filter(
            WorkOrderOperation.company_id == company_id,
            WorkCenter.company_id == company_id,
            WorkOrderOperation.status != OperationStatus.COMPLETE,
            func.lower(WorkCenter.work_center_type) == dept_norm,
        )
    )


def _apply_blocked_filters(query, company_id: int, dept_norm: Optional[str]):
    """Blocked-WO predicate, applied identically to the rail rows and the total.

    Joins WorkOrder for liveness/terminal exclusion; when ``dept_norm`` is
    given, attributes the blocker to a dept via ITS operation's work center
    (a blocker with no operation cannot be dept-attributed and drops off the
    dept-scoped view — it stays on the unfiltered board).
    """
    query = query.join(WorkOrder, WorkOrder.id == WorkOrderBlocker.work_order_id)
    if dept_norm is not None:
        query = (
            query.join(WorkOrderOperation, WorkOrderOperation.id == WorkOrderBlocker.operation_id)
            .join(WorkCenter, WorkCenter.id == WorkOrderOperation.work_center_id)
            .filter(
                WorkCenter.company_id == company_id,
                func.lower(WorkCenter.work_center_type) == dept_norm,
            )
        )
    return query.filter(
        WorkOrderBlocker.company_id == company_id,
        WorkOrderBlocker.status.in_(_UNRESOLVED_BLOCKER_STATUSES),
        # Keep in lockstep with the per-WC blocked_count: a soft-deleted or
        # terminal WO's blockers are off the board.
        WorkOrder.is_deleted == False,  # noqa: E712
        WorkOrder.status.not_in(_TERMINAL_WO_STATUSES),
    )


def operation_counts_by_work_center(db: Session, company_id: int) -> dict[int, dict[str, int]]:
    """Active (IN_PROGRESS) / queued (READY) operation counts per work center.

    Single conditional-aggregation query over open work orders, tenant-scoped.
    Shared by /shop-floor/dashboard and /shop-floor/wallboard.
    """
    operation_counts = (
        db.query(
            WorkOrderOperation.work_center_id,
            func.sum(case((WorkOrderOperation.status == OperationStatus.IN_PROGRESS, 1), else_=0)).label(
                "active_count"
            ),
            func.sum(case((WorkOrderOperation.status == OperationStatus.READY, 1), else_=0)).label("queued_count"),
        )
        .join(WorkOrder, WorkOrder.id == WorkOrderOperation.work_order_id)
        .filter(
            WorkOrder.company_id == company_id,
            WorkOrder.is_deleted == False,  # noqa: E712 — soft-deleted WOs must not inflate counts
            WorkOrder.status.not_in(_TERMINAL_WO_STATUSES),
            WorkOrderOperation.work_center_id.isnot(None),
        )
        .group_by(WorkOrderOperation.work_center_id)
        .all()
    )
    return {
        row.work_center_id: {"active": int(row.active_count or 0), "queued": int(row.queued_count or 0)}
        for row in operation_counts
    }


# ── Plant-wide blocks (ship / today / quality) ──────────────────────────────
# Each is cheap indexed aggregates, recomputed per 30s poll, and each is
# independently best-effort in build_wallboard_payload: a failed block is
# None, never a failed payload. ZERO-WRITE.


def _compute_ship(db: Session, company_id: int, central_today: date) -> WallboardShip:
    """Ship panel: what is promised out the door (Central-day window).

    Promise = coalesce(must_ship_by, due_date), identical to the OTD analytics;
    "fully shipped" uses the AnalyticsService counted-shipment rules via its
    PUBLIC ``get_total_shipped`` accessor — never the underscore privates.
    CANCELLED WOs are excluded exactly like the OTIF population rule.
    due_today/shipped_today are ONE population (WOs promised today): the
    denominator and numerator of the TV's "shipped / due" fraction.
    """
    from app.services.analytics_service import AnalyticsService

    analytics = AnalyticsService(db, company_id)
    promise = _promise_expr()
    base_filters = [
        WorkOrder.company_id == company_id,
        WorkOrder.is_deleted == False,  # noqa: E712
        WorkOrder.status != WorkOrderStatus.CANCELLED,
        WorkOrder.quantity_ordered > 0,
    ]

    week_end = central_today + timedelta(days=6)
    week_wos = (
        db.query(WorkOrder)
        .options(joinedload(WorkOrder.part))
        .filter(*base_filters, promise >= central_today, promise <= week_end)
        .all()
    )
    shipped_by_id = analytics.get_total_shipped([wo.id for wo in week_wos])

    def _remaining(wo: WorkOrder) -> float:
        return max(float(wo.quantity_ordered or 0) - shipped_by_id.get(wo.id, 0.0), 0.0)

    open_week = [wo for wo in week_wos if _remaining(wo) > 0]
    # The TV fraction is "shipped_today / due_today" — one population: WOs
    # PROMISED today. due_today counts them all (shipped or not) so the
    # fraction can't read COMPLETE while a due-today WO is still open.
    promised_today = [wo for wo in week_wos if work_order_promise_date(wo) == central_today]
    due_today_wos = [wo for wo in promised_today if _remaining(wo) > 0]
    due_today_rows = [
        WallboardShipRow(
            wo_number=wo.work_order_number,
            part_number=wo.part.part_number if wo.part else None,
            promise_date=work_order_promise_date(wo),
            qty_remaining=_remaining(wo),
        )
        for wo in sorted(due_today_wos, key=_remaining, reverse=True)[:2]
    ]

    next_due_date: Optional[date] = None
    next_due_count = 0
    if not promised_today:
        future_wos = (
            db.query(WorkOrder)
            .filter(*base_filters, promise.isnot(None), promise > central_today)
            .order_by(promise.asc(), WorkOrder.id.asc())
            .limit(_NEXT_DUE_SCAN_LIMIT)
            .all()
        )
        future_shipped = analytics.get_total_shipped([wo.id for wo in future_wos])
        for wo in future_wos:  # promise-ordered: first unshipped promise date wins
            remaining = max(float(wo.quantity_ordered or 0) - future_shipped.get(wo.id, 0.0), 0.0)
            if remaining <= 0:
                continue
            promise_date = work_order_promise_date(wo)
            if next_due_date is None:
                next_due_date = promise_date
            if promise_date == next_due_date:
                next_due_count += 1
            else:
                break

    return WallboardShip(
        due_today=len(promised_today),
        shipped_today=len(promised_today) - len(due_today_wos),
        due_this_week=len(open_week),
        due_today_rows=due_today_rows,
        next_due_date=next_due_date,
        next_due_count=next_due_count,
    )


def _compute_today(db: Session, company_id: int) -> WallboardToday:
    """Today-so-far pulse over the Central-midnight → now window.

    Aggregate counts only. Pieces/hours/scrap apply the Lean provenance rule
    (BASELINE_EXCLUDED_SOURCES) and, together with ops_completed, the
    scrap-Pareto WO-liveness shape: backfill/import rows never masquerade as
    live capture, soft-deleted WOs' records are out, WO-less entries stay
    counted. operators_on_clock is the lone exception to provenance — an open
    entry is live capture by definition.
    """
    day_start, day_end = central_day_window_utc()
    baseline = or_(TimeEntry.source.is_(None), TimeEntry.source.notin_(BASELINE_EXCLUDED_SOURCES))
    live_wo = or_(WorkOrder.id.is_(None), WorkOrder.is_deleted == False)  # noqa: E712

    ops_completed = (
        db.query(WorkOrderOperation)
        .outerjoin(WorkOrder, WorkOrderOperation.work_order_id == WorkOrder.id)
        .filter(
            WorkOrderOperation.company_id == company_id,
            WorkOrderOperation.status == OperationStatus.COMPLETE,
            WorkOrderOperation.actual_end >= day_start,
            WorkOrderOperation.actual_end <= day_end,
            live_wo,
        )
        .count()
    )

    pieces = (
        db.query(func.coalesce(func.sum(TimeEntry.quantity_produced), 0.0))
        .select_from(TimeEntry)
        .outerjoin(WorkOrder, TimeEntry.work_order_id == WorkOrder.id)
        .filter(
            TimeEntry.company_id == company_id,
            TimeEntry.entry_type.in_([TimeEntryType.RUN, TimeEntryType.REWORK]),
            TimeEntry.clock_in >= day_start,
            TimeEntry.clock_in <= day_end,
            baseline,
            live_wo,
        )
        .scalar()
    )

    wos_completed = (
        db.query(WorkOrder)
        .filter(
            WorkOrder.company_id == company_id,
            WorkOrder.is_deleted == False,  # noqa: E712
            WorkOrder.status.in_([WorkOrderStatus.COMPLETE, WorkOrderStatus.CLOSED]),
            WorkOrder.actual_end >= day_start,
            WorkOrder.actual_end <= day_end,
        )
        .count()
    )

    operators_on_clock = int(
        db.query(func.count(func.distinct(TimeEntry.user_id)))
        .filter(TimeEntry.company_id == company_id, TimeEntry.clock_out.is_(None))
        .scalar()
        or 0
    )

    # Hours logged today: closed labor durations + elapsed time on still-open
    # labor entries, for entries STARTED within the Central day (an overnight
    # entry is attributed to its start day). BREAK/DOWNTIME are not labor.
    # Provenance-excluded + WO-liveness-filtered like pieces/scrap: an Excel
    # backfill batch or a soft-deleted WO's labor must not inflate the TV.
    labor_rows = (
        db.query(TimeEntry.clock_in, TimeEntry.clock_out, TimeEntry.duration_hours)
        .outerjoin(WorkOrder, TimeEntry.work_order_id == WorkOrder.id)
        .filter(
            TimeEntry.company_id == company_id,
            TimeEntry.entry_type.in_(LABOR_ENTRY_TYPES),
            TimeEntry.clock_in >= day_start,
            TimeEntry.clock_in <= day_end,
            baseline,
            live_wo,
        )
        .all()
    )
    hours = 0.0
    for clock_in, clock_out, duration_hours in labor_rows:
        if clock_out is not None:
            if duration_hours is not None:
                hours += float(duration_hours)
            else:
                hours += max((clock_out - clock_in).total_seconds() / 3600.0, 0.0)
        elif clock_in is not None:
            hours += max((day_end - clock_in).total_seconds() / 3600.0, 0.0)

    receipts = (
        db.query(POReceipt)
        .filter(
            POReceipt.company_id == company_id,
            POReceipt.is_deleted == False,  # noqa: E712
            POReceipt.received_at >= day_start,
            POReceipt.received_at <= day_end,
        )
        .count()
    )

    scrap_events = (
        db.query(TimeEntry)
        .outerjoin(WorkOrder, TimeEntry.work_order_id == WorkOrder.id)
        .filter(
            TimeEntry.company_id == company_id,
            TimeEntry.quantity_scrapped > 0,
            TimeEntry.clock_in >= day_start,
            TimeEntry.clock_in <= day_end,
            baseline,
            live_wo,
        )
        .count()
    )

    return WallboardToday(
        ops_completed=ops_completed,
        pieces_completed=int(round(float(pieces or 0))),
        wos_completed=wos_completed,
        operators_on_clock=operators_on_clock,
        hours_logged=round(hours, 1),
        receipts=receipts,
        scrap_events=scrap_events,
    )


def _compute_quality(db: Session, company_id: int) -> WallboardQuality:
    """Quality panel: counts and ages ONLY — never NCR titles/descriptions."""
    open_ncr_filters = [
        NonConformanceReport.company_id == company_id,
        NonConformanceReport.is_deleted == False,  # noqa: E712
        NonConformanceReport.status.not_in([NCRStatus.CLOSED, NCRStatus.VOID]),
    ]
    open_ncr_count = db.query(NonConformanceReport).filter(*open_ncr_filters).count()

    newest_created = (
        db.query(NonConformanceReport.created_at)
        .filter(*open_ncr_filters)
        .order_by(NonConformanceReport.created_at.desc())
        .limit(1)
        .scalar()
    )
    newest_ncr_age_days: Optional[int] = None
    if newest_created is not None:
        newest_ncr_age_days = max(int((datetime.utcnow() - newest_created).total_seconds() // 86400), 0)

    wos_on_hold = (
        db.query(WorkOrder)
        .filter(
            WorkOrder.company_id == company_id,
            WorkOrder.is_deleted == False,  # noqa: E712
            WorkOrder.status == WorkOrderStatus.ON_HOLD,
        )
        .count()
    )

    return WallboardQuality(
        open_ncr_count=open_ncr_count,
        newest_ncr_age_days=newest_ncr_age_days,
        wos_on_hold=wos_on_hold,
    )


def build_wallboard_payload(db: Session, company_id: int, dept: Optional[str] = None) -> WallboardResponse:
    """Build the full wallboard snapshot for one company.

    ``dept`` optionally narrows the board to work centers whose
    ``work_center_type`` matches (case-insensitive) — one TV per department —
    and scopes the JOB WALL (via each job's CURRENT op's work-center type)
    plus the late/blocked rails AND their totals to that dept (late via any
    open op routed to a dept work center; blocked via the blocker's
    operation's work center; down via the work center itself). The ship /
    today / quality blocks stay plant-wide on every TV.

    The job wall is core like work_centers — computed inline, not best-effort.
    ``kpi_strip`` is deprecated and no longer computed (always None).

    Late = promise (coalesce(must_ship_by, due_date)) strictly before today's
    CENTRAL date on a live, non-terminal WO — see ``_late_wo_filters``.
    """
    now = datetime.utcnow()
    central_today = _central_today()
    dept_norm = dept.strip().lower() if dept else None

    # --- Work centers (active, tenant-scoped, optional dept filter) ---------
    wc_query = db.query(WorkCenter).filter(
        WorkCenter.company_id == company_id,
        WorkCenter.is_active == True,  # noqa: E712
    )
    if dept_norm:
        wc_query = wc_query.filter(func.lower(WorkCenter.work_center_type) == dept_norm)
    work_centers = wc_query.order_by(WorkCenter.name).all()

    op_counts = operation_counts_by_work_center(db, company_id)

    # --- Live jobs: open labor entries grouped into ONE row per operation ---
    active_entries = (
        db.query(TimeEntry)
        .options(
            joinedload(TimeEntry.user),
            joinedload(TimeEntry.operation),
            joinedload(TimeEntry.work_order).joinedload(WorkOrder.part),
        )
        .filter(
            TimeEntry.company_id == company_id,
            TimeEntry.clock_out.is_(None),
            # Only real labor on an operation is a "job" — an open BREAK or
            # DOWNTIME entry (no operation, or non-labor type) must not render
            # a ghost job row on the TV.
            TimeEntry.operation_id.isnot(None),
            TimeEntry.entry_type.in_(LABOR_ENTRY_TYPES),
        )
        .all()
    )

    # Crew-station grouping: several operators clocked into the same operation
    # are ONE job row with a crew list, not N duplicate rows.
    entries_by_op: dict[tuple[Optional[int], Optional[int]], list[TimeEntry]] = defaultdict(list)
    # The job wall shares the SAME single open-labor query, re-grouped by
    # operation only (its crew hangs off the current op, not a work center).
    entries_by_operation: dict[int, list[TimeEntry]] = defaultdict(list)
    for entry in active_entries:
        entries_by_op[(entry.work_center_id, entry.operation_id)].append(entry)
        if entry.operation_id is not None:
            entries_by_operation[entry.operation_id].append(entry)

    # --- Job wall population: ONE query for open WOs, ONE for their ops -----
    wall_wos = (
        db.query(WorkOrder)
        .options(joinedload(WorkOrder.part))
        .filter(
            WorkOrder.company_id == company_id,
            WorkOrder.is_deleted == False,  # noqa: E712
            WorkOrder.status.in_(_JOB_WALL_WO_STATUSES),
        )
        .all()
    )
    ops_by_wo: dict[int, list[WorkOrderOperation]] = defaultdict(list)
    if wall_wos:
        wall_ops = (
            db.query(WorkOrderOperation)
            .options(joinedload(WorkOrderOperation.work_center))
            .filter(
                WorkOrderOperation.company_id == company_id,
                WorkOrderOperation.work_order_id.in_([wo.id for wo in wall_wos]),
            )
            .order_by(WorkOrderOperation.sequence.asc(), WorkOrderOperation.id.asc())
            .all()
        )
        for op in wall_ops:
            ops_by_wo[op.work_order_id].append(op)

    # Per-job lateness comes from the SAME predicate as the late rail (ONE
    # id-scoped query serving both the machine wall's active-job flags and
    # the job wall's tiles, so none of the three can disagree on "late").
    job_wo_ids = {entry.work_order_id for entry in active_entries if entry.work_order_id}
    job_wo_ids |= {wo.id for wo in wall_wos}
    late_job_wo_ids: set[int] = set()
    if job_wo_ids:
        late_job_wo_ids = {
            row[0]
            for row in db.query(WorkOrder.id)
            .filter(*_late_wo_filters(company_id, central_today), WorkOrder.id.in_(job_wo_ids))
            .all()
        }

    jobs_by_wc: dict[int, list[WallboardActiveJob]] = defaultdict(list)
    for (wc_id, _op_id), group in entries_by_op.items():
        crew, crew_count, elapsed, first = _crew_summary(group, now)  # earliest clock_in drives elapsed
        operation = first.operation
        work_order = first.work_order
        jobs_by_wc[wc_id].append(
            WallboardActiveJob(
                wo_number=work_order.work_order_number if work_order else None,
                part_number=(work_order.part.part_number if work_order and work_order.part else None),
                op_name=operation.name if operation else None,
                operator_name=crew[0] if crew else None,  # back-compat alias of crew[0]
                crew=crew,
                crew_count=crew_count,
                elapsed_minutes=elapsed,
                qty_done=float(operation.quantity_complete or 0) if operation else 0.0,
                qty_target=operation_target_quantity(operation, work_order),
                is_late=bool(work_order is not None and work_order.id in late_job_wo_ids),
            )
        )

    # --- Unresolved blockers per work center (via the blocked operation) ----
    blocker_counts = dict(
        db.query(WorkOrderOperation.work_center_id, func.count(WorkOrderBlocker.id))
        .join(WorkOrderOperation, WorkOrderOperation.id == WorkOrderBlocker.operation_id)
        .join(WorkOrder, WorkOrder.id == WorkOrderBlocker.work_order_id)
        .filter(
            WorkOrderBlocker.company_id == company_id,
            WorkOrderBlocker.status.in_(_UNRESOLVED_BLOCKER_STATUSES),
            WorkOrderOperation.work_center_id.isnot(None),
            # Keep this in lockstep with the blocked_wos rail below: a
            # soft-deleted or terminal WO's blockers are off the board.
            WorkOrder.is_deleted == False,  # noqa: E712
            WorkOrder.status.not_in(_TERMINAL_WO_STATUSES),
        )
        .group_by(WorkOrderOperation.work_center_id)
        .all()
    )

    # --- Blocked flag for the job wall: ONE grouped query on work_order_id --
    # WO-level (any unresolved blocker on the WO, routed or not) — the tile
    # turns orange whether or not the blocker names an operation.
    blocked_job_wo_ids: set[int] = set()
    if wall_wos:
        blocked_job_wo_ids = {
            row[0]
            for row in db.query(WorkOrderBlocker.work_order_id)
            .filter(
                WorkOrderBlocker.company_id == company_id,
                WorkOrderBlocker.status.in_(_UNRESOLVED_BLOCKER_STATUSES),
                WorkOrderBlocker.work_order_id.in_([wo.id for wo in wall_wos]),
            )
            .group_by(WorkOrderBlocker.work_order_id)
            .all()
        }

    # --- Active downtime per work center (open DowntimeEvent) ---------------
    open_downtime = (
        db.query(DowntimeEvent)
        .filter(
            DowntimeEvent.company_id == company_id,
            DowntimeEvent.end_time.is_(None),
        )
        .order_by(DowntimeEvent.start_time.desc())
        .all()
    )
    downtime_by_wc: dict[int, WallboardDowntime] = {}
    for event in open_downtime:
        if event.work_center_id in downtime_by_wc:
            continue  # keep the most recent open event per work center
        category = event.category.value if hasattr(event.category, "value") else str(event.category)
        downtime_by_wc[event.work_center_id] = WallboardDowntime(
            category=category,
            since=event.start_time,
            minutes=_elapsed_minutes(event.start_time, now),
        )

    wc_cards = [
        WallboardWorkCenter(
            id=wc.id,
            code=wc.code,
            name=wc.name,
            status=wc.current_status,
            active_jobs=jobs_by_wc.get(wc.id, []),
            queued_count=op_counts.get(wc.id, {}).get("queued", 0),
            blocked_count=int(blocker_counts.get(wc.id, 0)),
            down=downtime_by_wc.get(wc.id),
        )
        for wc in work_centers
    ]

    # --- Job wall: pure assembly over the rows fetched above (no queries) ---
    # Downtime attributes to a job via its CURRENT op's work center; the map
    # already holds every open downtime event for the company.
    jobs, jobs_total = _build_job_wall(
        wall_wos,
        ops_by_wo,
        entries_by_operation,
        blocked_job_wo_ids,
        set(downtime_by_wc.keys()),
        late_job_wo_ids,
        dept_norm,
        central_today,
        now,
    )

    # --- Late work orders rail: worst-first (most days late), capped --------
    late_filters = _late_wo_filters(company_id, central_today)
    if dept_norm:
        late_filters = late_filters + [WorkOrder.id.in_(_dept_open_op_wo_ids(db, company_id, dept_norm))]
    late_rows = (
        db.query(WorkOrder)
        .options(joinedload(WorkOrder.part))
        .filter(*late_filters)
        .order_by(_promise_expr().asc(), WorkOrder.id.asc())  # earliest promise = most days late
        .limit(_TICKER_LIMIT)
        .all()
    )
    late_wos = []
    for wo in late_rows:
        promise_date = work_order_promise_date(wo)
        late_wos.append(
            WallboardLateWorkOrder(
                wo_number=wo.work_order_number,
                part_number=wo.part.part_number if wo.part else None,
                due_date=promise_date,
                days_late=(central_today - promise_date).days if promise_date else 0,
                status=wo.status.value if hasattr(wo.status, "value") else wo.status,
            )
        )
    late_total = int(db.query(func.count(WorkOrder.id)).filter(*late_filters).scalar() or 0)

    # --- Blocked work orders rail: oldest-first, capped ----------------------
    blocked_rows = (
        _apply_blocked_filters(db.query(WorkOrderBlocker, WorkOrder.work_order_number), company_id, dept_norm)
        .order_by(WorkOrderBlocker.reported_at.asc())
        .limit(_TICKER_LIMIT)
        .all()
    )
    blocked_wos = [
        WallboardBlockedWorkOrder(
            wo_number=wo_number,
            category=blocker.category,
            age_hours=round(_elapsed_minutes(blocker.reported_at, now) / 60.0, 1),
        )
        for blocker, wo_number in blocked_rows
    ]
    blocked_total = int(
        _apply_blocked_filters(
            db.query(func.count(WorkOrderBlocker.id)).select_from(WorkOrderBlocker), company_id, dept_norm
        ).scalar()
        or 0
    )

    # --- Down total: active work centers with an open downtime event --------
    down_query = (
        db.query(func.count(func.distinct(DowntimeEvent.work_center_id)))
        .select_from(DowntimeEvent)
        .join(WorkCenter, WorkCenter.id == DowntimeEvent.work_center_id)
        .filter(
            DowntimeEvent.company_id == company_id,
            DowntimeEvent.end_time.is_(None),
            WorkCenter.company_id == company_id,
            WorkCenter.is_active == True,  # noqa: E712
        )
    )
    if dept_norm:
        down_query = down_query.filter(func.lower(WorkCenter.work_center_type) == dept_norm)
    down_total = int(down_query.scalar() or 0)

    # --- Plant-wide blocks, each independently best-effort -------------------
    # The lambdas resolve the module-level compute functions at call time so a
    # failure in one block nulls THAT block only (and stays monkeypatchable).
    def _best_effort(label: str, compute):
        try:
            return compute()
        except Exception:  # a broken panel must never blank the whole TV
            logger.exception("wallboard %s compute failed for company %s", label, company_id)
            return None

    ship = _best_effort("ship", lambda: _compute_ship(db, company_id, central_today))
    today_block = _best_effort("today", lambda: _compute_today(db, company_id))
    quality = _best_effort("quality", lambda: _compute_quality(db, company_id))

    return WallboardResponse(
        work_centers=wc_cards,
        late_wos=late_wos,
        blocked_wos=blocked_wos,
        # kpi_strip is DEPRECATED (Job Wall redesign) — deliberately left at
        # its None default; nothing computes it anymore.
        jobs=jobs,
        jobs_total=jobs_total,
        late_total=late_total,
        blocked_total=blocked_total,
        down_total=down_total,
        ship=ship,
        today=today_block,
        quality=quality,
        generated_at=now,
    )
