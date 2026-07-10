"""Read-only payload builder for the shop-floor TV wallboard (A0.5).

One call returns the whole board: per-work-center live state (who's on it,
what WO/op, elapsed time, queue depth, blockers, downtime) plus top-level
late / blocked work-order tickers.

DELIBERATELY READ-ONLY: unlike the interactive /shop-floor/dashboard, this
builder runs NO reconcile-on-read and writes NOTHING (no audit rows, no
events). Display tokens have no user identity to attribute writes to, and a
wall-mounted TV polling every 30s must never mutate state. Tenant scoping is
the caller's company_id (derived from the user token or the display_tokens
row — never from client input).

PRIVACY: operator identity is truncated to "First L." — this renders on a
public screen.

``operation_counts_by_work_center`` is shared with the /shop-floor/dashboard
handler so the two surfaces can't drift on what "active/queued" means.
"""

import logging
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import case, func
from sqlalchemy.orm import Session, joinedload

from app.models.downtime import DowntimeEvent
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.models.work_order_blocker import WorkOrderBlocker, WorkOrderBlockerStatus
from app.schemas.wallboard import (
    WallboardActiveJob,
    WallboardBlockedWorkOrder,
    WallboardDowntime,
    WallboardKPIStrip,
    WallboardLateWorkOrder,
    WallboardResponse,
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

# Cap the tickers — a TV ticker cycling 500 rows is unreadable anyway.
_TICKER_LIMIT = 25

# ── KPI strip cache (Lean Phase 1 / issue #88) ──────────────────────────────
# The trailing-30-day KPI legs (ship OTD, FPY, scrap rate, WIP aging) are far
# heavier than the live board queries, and the TV polls every 30s. A coarse
# per-company in-process TTL cache keeps the strip to ~one analytics pass per
# 5 minutes per company per worker process — trailing-30-day numbers do not
# meaningfully change faster than that. Successful computes only are cached; a
# failed compute returns None (strip omitted) and is retried on the next poll.
_KPI_STRIP_TTL_SECONDS = 300.0
_kpi_strip_cache: dict[int, tuple[float, WallboardKPIStrip]] = {}
_kpi_strip_lock = threading.Lock()


def reset_kpi_strip_cache() -> None:
    """Drop all cached KPI strips (test isolation helper)."""
    with _kpi_strip_lock:
        _kpi_strip_cache.clear()


def _compute_kpi_strip(db: Session, company_id: int) -> WallboardKPIStrip:
    """Trailing-30-day floor KPIs via the Lean Phase 1 metric services.

    READ-ONLY like the rest of this builder (all three services only query).
    Percentages are 0-100; None = insufficient data in the window ("n/a" on
    the TV), never a fake 0/100. The provenance rule rides along wherever the
    underlying data supports it (WIP/OTD/FPY are WO/op/shipment-anchored).
    """
    # Local imports keep this module cheap to import for its shared helpers
    # (shop_floor imports LABOR_ENTRY_TYPES et al at startup).
    from app.services.analytics_service import AnalyticsService
    from app.services.flow_metrics_service import get_wip_aging
    from app.services.quality_yield_service import get_fpy_rty, get_scrap_rate

    end = date.today()
    start = end - timedelta(days=30)

    otd_ship = AnalyticsService(db, company_id).get_ship_otd_value(start, end)
    fpy = get_fpy_rty(db, company_id, start, end).overall_fpy_pct
    scrap = get_scrap_rate(db, company_id, start, end)
    wip = get_wip_aging(db, company_id)
    ages = [item.days_since_release for item in wip.items if item.days_since_release is not None]

    return WallboardKPIStrip(
        otd_ship_pct_30d=round(otd_ship, 1) if otd_ship is not None else None,
        fpy_pct_30d=fpy,
        scrap_pct_30d=scrap,
        open_wip_count=wip.total_open,
        avg_wip_age_days=round(sum(ages) / len(ages), 1) if ages else None,
    )


def get_kpi_strip(db: Session, company_id: int) -> Optional[WallboardKPIStrip]:
    """Cached KPI strip for one company; None only when the compute failed.

    Best-effort: an analytics failure must never take down the live board, so
    errors are logged and the strip is simply omitted for that poll.
    """
    now_monotonic = time.monotonic()
    with _kpi_strip_lock:
        cached = _kpi_strip_cache.get(company_id)
        if cached is not None and cached[0] > now_monotonic:
            return cached[1]
    try:
        strip = _compute_kpi_strip(db, company_id)
    except Exception:  # pragma: no cover - strip failure must not break the board
        logger.exception("wallboard kpi_strip compute failed for company %s", company_id)
        return None
    with _kpi_strip_lock:
        _kpi_strip_cache[company_id] = (now_monotonic + _KPI_STRIP_TTL_SECONDS, strip)
    return strip


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


def build_wallboard_payload(db: Session, company_id: int, dept: Optional[str] = None) -> WallboardResponse:
    """Build the full wallboard snapshot for one company.

    ``dept`` optionally narrows the board to work centers whose
    ``work_center_type`` matches (case-insensitive) — one TV per department.
    Late = ``due_date`` strictly before today (naive date compare, v1).
    """
    now = datetime.utcnow()
    today = date.today()

    # --- Work centers (active, tenant-scoped, optional dept filter) ---------
    wc_query = db.query(WorkCenter).filter(
        WorkCenter.company_id == company_id,
        WorkCenter.is_active == True,  # noqa: E712
    )
    if dept:
        wc_query = wc_query.filter(func.lower(WorkCenter.work_center_type) == dept.strip().lower())
    work_centers = wc_query.order_by(WorkCenter.name).all()

    op_counts = operation_counts_by_work_center(db, company_id)

    # --- Live jobs: open time entries grouped by work center ---------------
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
    jobs_by_wc: dict[int, list[WallboardActiveJob]] = defaultdict(list)
    for entry in active_entries:
        operation = entry.operation
        work_order = entry.work_order
        jobs_by_wc[entry.work_center_id].append(
            WallboardActiveJob(
                wo_number=work_order.work_order_number if work_order else None,
                part_number=(work_order.part.part_number if work_order and work_order.part else None),
                op_name=operation.name if operation else None,
                operator_name=(
                    operator_display_name(entry.user.first_name, entry.user.last_name) if entry.user else None
                ),
                elapsed_minutes=_elapsed_minutes(entry.clock_in, now),
                qty_done=float(operation.quantity_complete or 0) if operation else 0.0,
                qty_target=operation_target_quantity(operation, work_order),
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
            # Keep this in lockstep with the blocked_wos ticker below: a
            # soft-deleted or terminal WO's blockers are off the board.
            WorkOrder.is_deleted == False,  # noqa: E712
            WorkOrder.status.not_in(_TERMINAL_WO_STATUSES),
        )
        .group_by(WorkOrderOperation.work_center_id)
        .all()
    )

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

    # --- Late work orders (due date past, still open, not soft-deleted) -----
    late_rows = (
        db.query(WorkOrder)
        .options(joinedload(WorkOrder.part))
        .filter(
            WorkOrder.company_id == company_id,
            WorkOrder.is_deleted == False,  # noqa: E712
            WorkOrder.due_date < today,
            WorkOrder.status.not_in(_TERMINAL_WO_STATUSES),
        )
        .order_by(WorkOrder.due_date.asc())
        .limit(_TICKER_LIMIT)
        .all()
    )
    late_wos = [
        WallboardLateWorkOrder(
            wo_number=wo.work_order_number,
            part_number=wo.part.part_number if wo.part else None,
            due_date=wo.due_date,
            days_late=(today - wo.due_date).days if wo.due_date else 0,
            status=wo.status.value if hasattr(wo.status, "value") else wo.status,
        )
        for wo in late_rows
    ]

    # --- Blocked work orders ticker -----------------------------------------
    blocked_rows = (
        db.query(WorkOrderBlocker, WorkOrder.work_order_number)
        .join(WorkOrder, WorkOrder.id == WorkOrderBlocker.work_order_id)
        .filter(
            WorkOrderBlocker.company_id == company_id,
            WorkOrderBlocker.status.in_(_UNRESOLVED_BLOCKER_STATUSES),
            WorkOrder.is_deleted == False,  # noqa: E712
            WorkOrder.status.not_in(_TERMINAL_WO_STATUSES),
        )
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

    return WallboardResponse(
        work_centers=wc_cards,
        late_wos=late_wos,
        blocked_wos=blocked_wos,
        # Trailing-30-day floor KPIs, company-wide (NOT narrowed by ``dept`` —
        # the strip is the same on every TV), TTL-cached, best-effort.
        kpi_strip=get_kpi_strip(db, company_id),
        generated_at=now,
    )
