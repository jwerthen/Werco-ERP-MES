"""Flow metrics service (Lean Phase 1 / issue #88, work item 1c).

MEASURED flow -- lead time, queue time, WIP aging, Little's Law throughput and
process-cycle efficiency -- from durable production evidence:

* Lead time per completed WO: ``released_at -> actual_end`` (plus release ->
  first/last ship where shipments exist).
* Queue time per operation start: ``operation_ready`` OperationalEvent ->
  ``actual_start`` where the event exists (emitted at the PENDING->READY flips
  since this phase); falling back to predecessor ``actual_end -> actual_start``,
  then to WO ``released_at`` for a first operation.
* WIP aging: open WOs with days since release and days in the current operation.
* Little's Law: average open-WO count / daily completion rate over the window.
* PCE: value-add RUN hours / elapsed lead time.

Provenance rule (cross-cutting): labor booked through backfill/import channels
(``BASELINE_EXCLUDED_SOURCES``) is excluded from the value-add baseline and
reported separately -- paper catch-up must not read as measured flow. All
queries tenant-scoped via ``company_id``.
"""

import logging
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from app.models.operational_event import OperationalEvent
from app.models.shipping import Shipment, ShipmentStatus
from app.models.time_entry import BASELINE_EXCLUDED_SOURCES, TimeEntry, TimeEntryType
from app.models.work_center import WorkCenter
from app.models.work_order import WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.schemas.analytics import (
    FlowMetricsResponse,
    FlowSummary,
    FlowWorkOrderDetail,
    QueueTimeByWorkCenter,
    WIPAgingItem,
    WIPAgingResponse,
)

logger = logging.getLogger(__name__)

# Open (work-in-process) statuses: released to the floor and not terminal.
WIP_STATUSES = [WorkOrderStatus.RELEASED, WorkOrderStatus.IN_PROGRESS, WorkOrderStatus.ON_HOLD]

# Statuses whose actual_end marks a genuine completion for flow purposes.
COMPLETED_STATUSES = [WorkOrderStatus.COMPLETE, WorkOrderStatus.CLOSED]


def _days(delta_from: datetime, delta_to: datetime) -> float:
    return (delta_to - delta_from).total_seconds() / 86400.0


def _hours(delta_from: datetime, delta_to: datetime) -> float:
    return (delta_to - delta_from).total_seconds() / 3600.0


def _ship_span_by_work_order(db: Session, company_id: int, work_order_ids: List[int]) -> Dict[int, Tuple[date, date]]:
    """{wo_id: (first_ship_date, last_ship_date)} over counted shipments."""
    if not work_order_ids:
        return {}
    rows = (
        db.query(
            Shipment.work_order_id,
            func.min(Shipment.ship_date).label("first_ship"),
            func.max(Shipment.ship_date).label("last_ship"),
        )
        .filter(
            Shipment.company_id == company_id,
            Shipment.is_deleted == False,  # noqa: E712
            Shipment.status != ShipmentStatus.CANCELLED,
            Shipment.ship_date.isnot(None),
            Shipment.work_order_id.in_(work_order_ids),
        )
        .group_by(Shipment.work_order_id)
        .all()
    )
    return {row.work_order_id: (row.first_ship, row.last_ship) for row in rows}


def _value_add_hours_by_work_order(db: Session, company_id: int, work_order_ids: List[int]) -> Dict[int, float]:
    """Closed RUN-entry hours per WO, EXCLUDING backfill/import (provenance rule).

    RUN is the value-adding portion of clocked labor (setup/inspection/rework are
    classic lean non-value-add), which is what PCE measures against elapsed time.
    NULL source stays in the baseline (unknown-but-contemporaneous capture).
    """
    if not work_order_ids:
        return {}
    rows = (
        db.query(TimeEntry.work_order_id, func.coalesce(func.sum(TimeEntry.duration_hours), 0.0))
        .filter(
            TimeEntry.company_id == company_id,
            TimeEntry.work_order_id.in_(work_order_ids),
            TimeEntry.clock_out.isnot(None),
            TimeEntry.entry_type == TimeEntryType.RUN,
            or_(TimeEntry.source.is_(None), TimeEntry.source.notin_(BASELINE_EXCLUDED_SOURCES)),
        )
        .group_by(TimeEntry.work_order_id)
        .all()
    )
    return {wo_id: float(hours or 0) for wo_id, hours in rows}


def _excluded_backfill_hours(db: Session, company_id: int, start_dt: datetime, end_dt: datetime) -> float:
    """Total closed labor hours in the window booked via backfill/import channels."""
    return float(
        db.query(func.coalesce(func.sum(TimeEntry.duration_hours), 0.0))
        .filter(
            TimeEntry.company_id == company_id,
            TimeEntry.clock_in >= start_dt,
            TimeEntry.clock_in <= end_dt,
            TimeEntry.clock_out.isnot(None),
            TimeEntry.source.in_(BASELINE_EXCLUDED_SOURCES),
        )
        .scalar()
        or 0.0
    )


def _ready_times_by_operation(db: Session, company_id: int, operation_ids: List[int]) -> Dict[int, datetime]:
    """Earliest operation_ready event time per operation id (tenant-scoped)."""
    if not operation_ids:
        return {}
    rows = (
        db.query(OperationalEvent.operation_id, func.min(OperationalEvent.occurred_at))
        .filter(
            OperationalEvent.company_id == company_id,
            OperationalEvent.event_type == "operation_ready",
            OperationalEvent.operation_id.in_(operation_ids),
        )
        .group_by(OperationalEvent.operation_id)
        .all()
    )
    return {op_id: ready_at for op_id, ready_at in rows if ready_at is not None}


def get_flow_metrics(db: Session, company_id: int, start: date, end: date) -> FlowMetricsResponse:
    """Window flow summary + per-WO lead-time detail + per-WC queue times."""
    start_dt = datetime.combine(start, datetime.min.time())
    end_dt = datetime.combine(end, datetime.max.time())
    now = datetime.utcnow()

    # ── Completed WOs in the window (anchor: actual_end) ────────────────────────
    completed = (
        db.query(WorkOrder)
        .options(joinedload(WorkOrder.part))
        .filter(
            WorkOrder.company_id == company_id,
            WorkOrder.is_deleted == False,  # noqa: E712
            WorkOrder.status.in_(COMPLETED_STATUSES),
            WorkOrder.actual_end.isnot(None),
            WorkOrder.actual_end >= start_dt,
            WorkOrder.actual_end <= end_dt,
        )
        .all()
    )
    completed_ids = [wo.id for wo in completed]
    ship_spans = _ship_span_by_work_order(db, company_id, completed_ids)
    value_add = _value_add_hours_by_work_order(db, company_id, completed_ids)

    details: List[FlowWorkOrderDetail] = []
    lead_times: List[float] = []
    release_to_last_ship: List[float] = []
    pce_values: List[float] = []
    for wo in completed:
        first_ship, last_ship = ship_spans.get(wo.id, (None, None))
        lead_days: Optional[float] = None
        if wo.released_at and wo.actual_end and wo.actual_end >= wo.released_at:
            lead_days = _days(wo.released_at, wo.actual_end)
            lead_times.append(lead_days)

        rel_to_first = rel_to_last = None
        if wo.released_at:
            released_date = wo.released_at.date()
            if first_ship and first_ship >= released_date:
                rel_to_first = float((first_ship - released_date).days)
            if last_ship and last_ship >= released_date:
                rel_to_last = float((last_ship - released_date).days)
                release_to_last_ship.append(rel_to_last)

        va_hours = value_add.get(wo.id, 0.0)
        pce: Optional[float] = None
        if lead_days is not None and lead_days > 0:
            pce = min(100.0, (va_hours / (lead_days * 24.0)) * 100.0)
            pce_values.append(pce)

        details.append(
            FlowWorkOrderDetail(
                work_order_id=wo.id,
                work_order_number=wo.work_order_number,
                part_number=wo.part.part_number if wo.part else None,
                customer_name=wo.customer_name,
                released_at=wo.released_at,
                actual_end=wo.actual_end,
                first_ship_date=first_ship,
                last_ship_date=last_ship,
                lead_time_days=round(lead_days, 2) if lead_days is not None else None,
                release_to_first_ship_days=rel_to_first,
                release_to_last_ship_days=rel_to_last,
                value_add_hours=round(va_hours, 2),
                pce_pct=round(pce, 1) if pce is not None else None,
            )
        )
    details.sort(key=lambda d: d.actual_end or now, reverse=True)

    # ── Queue times for operations STARTED in the window ───────────────────────
    # Join the owning WO to exclude soft-deleted work orders' operations from the
    # queue-time samples (consistent with every sibling query in this module).
    started_ops = (
        db.query(WorkOrderOperation)
        .join(WorkOrder, WorkOrderOperation.work_order_id == WorkOrder.id)
        .filter(
            WorkOrderOperation.company_id == company_id,
            WorkOrder.company_id == company_id,
            WorkOrder.is_deleted == False,  # noqa: E712
            WorkOrderOperation.actual_start.isnot(None),
            WorkOrderOperation.actual_start >= start_dt,
            WorkOrderOperation.actual_start <= end_dt,
        )
        .all()
    )
    ready_times = _ready_times_by_operation(db, company_id, [op.id for op in started_ops])

    # Predecessor lookup: all ops of the involved WOs, grouped in Python.
    involved_wo_ids = list({op.work_order_id for op in started_ops})
    ops_by_wo: Dict[int, List[WorkOrderOperation]] = {}
    if involved_wo_ids:
        for op in (
            db.query(WorkOrderOperation)
            .filter(WorkOrderOperation.company_id == company_id, WorkOrderOperation.work_order_id.in_(involved_wo_ids))
            .all()
        ):
            ops_by_wo.setdefault(op.work_order_id, []).append(op)
    released_by_wo: Dict[int, Optional[datetime]] = {}
    if involved_wo_ids:
        released_by_wo = dict(
            db.query(WorkOrder.id, WorkOrder.released_at)
            .filter(WorkOrder.company_id == company_id, WorkOrder.id.in_(involved_wo_ids))
            .all()
        )

    wc_samples: Dict[int, Dict] = {}
    all_queue_hours: List[float] = []
    for op in started_ops:
        anchor: Optional[datetime] = None
        from_event = False
        ready_at = ready_times.get(op.id)
        if ready_at is not None:
            # occurred_at is timezone-aware on some backends; normalize to naive UTC
            # to match the naive actual_start columns.
            anchor = ready_at.replace(tzinfo=None) if ready_at.tzinfo else ready_at
            from_event = True
        else:
            predecessors = [
                sibling
                for sibling in ops_by_wo.get(op.work_order_id, [])
                if sibling.sequence < op.sequence and sibling.actual_end is not None
            ]
            if predecessors:
                anchor = max(p.actual_end for p in predecessors)
            else:
                anchor = released_by_wo.get(op.work_order_id)
        if anchor is None or op.actual_start is None:
            continue
        queue_hours = max(0.0, _hours(anchor, op.actual_start))
        all_queue_hours.append(queue_hours)
        if op.work_center_id:
            bucket = wc_samples.setdefault(op.work_center_id, {"hours": [], "from_events": 0})
            bucket["hours"].append(queue_hours)
            if from_event:
                bucket["from_events"] += 1

    wc_rows = []
    if wc_samples:
        wc_info = {
            wc.id: wc
            for wc in db.query(WorkCenter)
            .filter(WorkCenter.company_id == company_id, WorkCenter.id.in_(list(wc_samples.keys())))
            .all()
        }
        for wc_id, bucket in wc_samples.items():
            hours = bucket["hours"]
            wc = wc_info.get(wc_id)
            wc_rows.append(
                QueueTimeByWorkCenter(
                    work_center_id=wc_id,
                    work_center_code=wc.code if wc else None,
                    work_center_name=wc.name if wc else None,
                    avg_queue_hours=round(sum(hours) / len(hours), 2) if hours else None,
                    max_queue_hours=round(max(hours), 2) if hours else None,
                    samples=len(hours),
                    from_ready_events=bucket["from_events"],
                )
            )
        wc_rows.sort(key=lambda r: r.avg_queue_hours or 0, reverse=True)

    # ── Little's Law over the window ────────────────────────────────────────────
    window_days = max(1, (end - start).days + 1)
    daily_completion_rate = len(completed) / window_days

    # Average WIP: daily open-WO count from released_at/actual_end evidence.
    # CANCELLED is excluded entirely (no cancellation timestamp exists to bound
    # its WIP span honestly).
    wip_candidates = (
        db.query(WorkOrder.released_at, WorkOrder.actual_end)
        .filter(
            WorkOrder.company_id == company_id,
            WorkOrder.is_deleted == False,  # noqa: E712
            WorkOrder.status != WorkOrderStatus.CANCELLED,
            WorkOrder.released_at.isnot(None),
            WorkOrder.released_at <= end_dt,
            or_(WorkOrder.actual_end.is_(None), WorkOrder.actual_end >= start_dt),
        )
        .all()
    )
    wip_total = 0
    day = start
    while day <= end:
        day_end = datetime.combine(day, datetime.max.time())
        wip_total += sum(
            1
            for released_at, actual_end in wip_candidates
            if released_at <= day_end and (actual_end is None or actual_end > day_end)
        )
        day += timedelta(days=1)
    avg_wip = wip_total / window_days if wip_candidates else 0.0

    littles_law = (avg_wip / daily_completion_rate) if daily_completion_rate > 0 else None

    sorted_leads = sorted(lead_times)
    median_lead = None
    if sorted_leads:
        mid = len(sorted_leads) // 2
        median_lead = (
            sorted_leads[mid] if len(sorted_leads) % 2 == 1 else (sorted_leads[mid - 1] + sorted_leads[mid]) / 2.0
        )

    summary = FlowSummary(
        work_orders_completed=len(completed),
        avg_lead_time_days=round(sum(lead_times) / len(lead_times), 2) if lead_times else None,
        median_lead_time_days=round(median_lead, 2) if median_lead is not None else None,
        avg_release_to_last_ship_days=(
            round(sum(release_to_last_ship) / len(release_to_last_ship), 2) if release_to_last_ship else None
        ),
        avg_queue_hours=round(sum(all_queue_hours) / len(all_queue_hours), 2) if all_queue_hours else None,
        avg_wip=round(avg_wip, 2) if wip_candidates else None,
        daily_completion_rate=round(daily_completion_rate, 3),
        littles_law_throughput_days=round(littles_law, 2) if littles_law is not None else None,
        avg_pce_pct=round(sum(pce_values) / len(pce_values), 1) if pce_values else None,
        excluded_backfill_import_hours=round(_excluded_backfill_hours(db, company_id, start_dt, end_dt), 2),
    )

    return FlowMetricsResponse(
        period_start=start,
        period_end=end,
        summary=summary,
        work_orders=details,
        queue_by_work_center=wc_rows,
        generated_at=datetime.utcnow(),
    )


def get_wip_aging(db: Session, company_id: int) -> WIPAgingResponse:
    """Open-WO aging list for the WIP table (tenant-scoped, current snapshot)."""
    now = datetime.utcnow()
    today = date.today()

    open_wos = (
        db.query(WorkOrder)
        .options(joinedload(WorkOrder.part))
        .filter(
            WorkOrder.company_id == company_id,
            WorkOrder.is_deleted == False,  # noqa: E712
            WorkOrder.status.in_(WIP_STATUSES),
        )
        .all()
    )

    current_op_ids = [wo.current_operation_id for wo in open_wos if wo.current_operation_id]
    ops_by_id: Dict[int, WorkOrderOperation] = {}
    if current_op_ids:
        ops_by_id = {
            op.id: op
            for op in db.query(WorkOrderOperation)
            .options(joinedload(WorkOrderOperation.work_center))
            .filter(WorkOrderOperation.company_id == company_id, WorkOrderOperation.id.in_(current_op_ids))
            .all()
        }
    ready_times = _ready_times_by_operation(db, company_id, list(ops_by_id.keys()))

    items: List[WIPAgingItem] = []
    for wo in open_wos:
        op = ops_by_id.get(wo.current_operation_id) if wo.current_operation_id else None
        days_in_op: Optional[float] = None
        if op is not None:
            anchor = op.actual_start
            if anchor is None:
                ready_at = ready_times.get(op.id)
                if ready_at is not None:
                    anchor = ready_at.replace(tzinfo=None) if ready_at.tzinfo else ready_at
            if anchor is not None and anchor <= now:
                days_in_op = round(_days(anchor, now), 2)

        items.append(
            WIPAgingItem(
                work_order_id=wo.id,
                work_order_number=wo.work_order_number,
                part_number=wo.part.part_number if wo.part else None,
                customer_name=wo.customer_name,
                status=wo.status.value if hasattr(wo.status, "value") else str(wo.status),
                priority=wo.priority,
                quantity_ordered=float(wo.quantity_ordered or 0),
                quantity_complete=float(wo.quantity_complete or 0),
                released_at=wo.released_at,
                days_since_release=(
                    round(_days(wo.released_at, now), 2) if wo.released_at and wo.released_at <= now else None
                ),
                current_operation_id=op.id if op else None,
                current_operation_number=op.operation_number if op else None,
                current_operation_name=op.name if op else None,
                current_work_center_name=op.work_center.name if op and op.work_center else None,
                days_in_current_operation=days_in_op,
                due_date=wo.due_date,
                days_to_due=(wo.due_date - today).days if wo.due_date else None,
            )
        )

    items.sort(key=lambda item: item.days_since_release or -1, reverse=True)
    return WIPAgingResponse(items=items, total_open=len(items), generated_at=datetime.utcnow())
