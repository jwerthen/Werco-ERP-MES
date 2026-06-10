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

from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import case, func
from sqlalchemy.orm import Session, joinedload

from app.models.downtime import DowntimeEvent
from app.models.time_entry import TimeEntry
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.models.work_order_blocker import WorkOrderBlocker, WorkOrderBlockerStatus
from app.schemas.wallboard import (
    WallboardActiveJob,
    WallboardBlockedWorkOrder,
    WallboardDowntime,
    WallboardLateWorkOrder,
    WallboardResponse,
    WallboardWorkCenter,
)
from app.services.work_order_state_service import operation_target_quantity

# Blocker states that still block (RESOLVED / DISMISSED do not).
_UNRESOLVED_BLOCKER_STATUSES = [WorkOrderBlockerStatus.OPEN.value, WorkOrderBlockerStatus.ACKNOWLEDGED.value]

# Cap the tickers — a TV ticker cycling 500 rows is unreadable anyway.
_TICKER_LIMIT = 25


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
            WorkOrder.status.not_in([WorkOrderStatus.COMPLETE, WorkOrderStatus.CLOSED, WorkOrderStatus.CANCELLED]),
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
        .filter(TimeEntry.company_id == company_id, TimeEntry.clock_out.is_(None))
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
        .filter(
            WorkOrderBlocker.company_id == company_id,
            WorkOrderBlocker.status.in_(_UNRESOLVED_BLOCKER_STATUSES),
            WorkOrderOperation.work_center_id.isnot(None),
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
            WorkOrder.status.not_in([WorkOrderStatus.COMPLETE, WorkOrderStatus.CLOSED, WorkOrderStatus.CANCELLED]),
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
        generated_at=now,
    )
