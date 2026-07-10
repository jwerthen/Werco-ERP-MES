"""Adoption + hidden-factory analytics (Lean Phase 1 / issue #88, work item 1f).

Adoption (is the floor actually living in the system?):
  * digital-completion %: share of ``operation_completed`` OperationalEvents
    whose payload ``source`` is a LIVE capture channel (kiosk/desktop/scanner)
    vs backfill/import vs unreported. Computed from EVENTS, not TimeEntry.source
    (the column is last-writer-wins; the event payload preserves the per-write
    channel -- see the note on TimeEntry.source).
  * clock-in coverage: completed operations (actual_end in window) having at
    least one closed labor TimeEntry that is NOT backfill/import-sourced.
  * backfill rate: share of the window's closed TimeEntries whose source is
    backfill/import (the provenance rule's separate report).
  * all three broken down by ISO week.

Hidden factory (the un-booked cost of poor quality / reactive work):
  * rework hours % (REWORK entry hours / all labor hours) and rework quantity
    share (REWORK-entry produced / all production-bearing produced), both
    provenance-filtered with the excluded hours reported separately.
  * planned-vs-reactive maintenance mix (MaintenanceWorkOrder.maintenance_type:
    preventive+predictive = planned; corrective+emergency = reactive).
  * MTBF/MTTR per work center from unplanned DowntimeEvents: MTBF = staffed
    run (RUN+SETUP clocked) hours / unplanned event count; MTTR = mean unplanned
    event duration (the event's FULL span, even when it ends after the window).

All queries tenant-scoped via ``company_id``.
"""

import logging
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.models.downtime import DowntimeEvent, DowntimePlannedType
from app.models.maintenance import MaintenanceType, MaintenanceWorkOrder
from app.models.operational_event import OperationalEvent
from app.models.time_entry import (
    BASELINE_EXCLUDED_SOURCES,
    LIVE_CAPTURE_SOURCES,
    TimeEntry,
    TimeEntryType,
)
from app.models.work_center import WorkCenter
from app.models.work_order import WorkOrder, WorkOrderOperation
from app.schemas.analytics import (
    AdoptionMetricsResponse,
    AdoptionWeek,
    HiddenFactoryMetrics,
    MaintenanceMixMetrics,
    WorkCenterReliability,
)

logger = logging.getLogger(__name__)

# Labor-bearing entry types for hour denominators (BREAK is not labor; DOWNTIME
# is machine state, not value work -- both excluded from the labor-hour base).
LABOR_ENTRY_TYPES = [TimeEntryType.SETUP, TimeEntryType.RUN, TimeEntryType.REWORK, TimeEntryType.INSPECTION]
PRODUCTION_BEARING_ENTRY_TYPES = [TimeEntryType.RUN, TimeEntryType.REWORK]
STAFFED_RUN_ENTRY_TYPES = [TimeEntryType.RUN, TimeEntryType.SETUP]

PLANNED_MAINTENANCE_TYPES = [MaintenanceType.PREVENTIVE, MaintenanceType.PREDICTIVE]
REACTIVE_MAINTENANCE_TYPES = [MaintenanceType.CORRECTIVE, MaintenanceType.EMERGENCY]


def _week_start(day: date) -> date:
    """Monday of the ISO week containing ``day``."""
    return day - timedelta(days=day.weekday())


def _pct(numerator: float, denominator: float) -> Optional[float]:
    if denominator <= 0:
        return None
    return round(numerator / denominator * 100.0, 1)


def get_adoption_metrics(db: Session, company_id: int, start: date, end: date) -> AdoptionMetricsResponse:
    start_dt = datetime.combine(start, datetime.min.time())
    end_dt = datetime.combine(end, datetime.max.time())

    # ── Digital completion % from operation_completed events ───────────────────
    events = (
        db.query(OperationalEvent.occurred_at, OperationalEvent.event_payload)
        .filter(
            OperationalEvent.company_id == company_id,
            OperationalEvent.event_type == "operation_completed",
            OperationalEvent.occurred_at >= start_dt,
            OperationalEvent.occurred_at <= end_dt,
        )
        .all()
    )
    live = backfill = unknown = 0
    weekly: Dict[date, Dict[str, float]] = {}

    def _week_bucket(day: date) -> Dict[str, float]:
        return weekly.setdefault(
            _week_start(day),
            {
                "completions": 0,
                "live": 0,
                "backfill": 0,
                "unknown": 0,
                "ops_completed": 0,
                "ops_covered": 0,
                "entries": 0,
                "backfill_entries": 0,
            },
        )

    for occurred_at, payload in events:
        source = (payload or {}).get("source")
        bucket = _week_bucket(occurred_at.date())
        bucket["completions"] += 1
        if source in LIVE_CAPTURE_SOURCES:
            live += 1
            bucket["live"] += 1
        elif source in BASELINE_EXCLUDED_SOURCES:
            backfill += 1
            bucket["backfill"] += 1
        else:
            unknown += 1
            bucket["unknown"] += 1
    total_completions = live + backfill + unknown

    # ── Clock-in coverage: completed ops with >=1 non-backfill labor entry ─────
    # Join the owning WO to exclude soft-deleted work orders' operations from the
    # coverage denominator (consistent with the sibling metric queries).
    completed_ops = (
        db.query(WorkOrderOperation.id, WorkOrderOperation.actual_end)
        .join(WorkOrder, WorkOrderOperation.work_order_id == WorkOrder.id)
        .filter(
            WorkOrderOperation.company_id == company_id,
            WorkOrder.company_id == company_id,
            WorkOrder.is_deleted == False,  # noqa: E712
            WorkOrderOperation.actual_end.isnot(None),
            WorkOrderOperation.actual_end >= start_dt,
            WorkOrderOperation.actual_end <= end_dt,
        )
        .all()
    )
    covered_op_ids: set = set()
    if completed_ops:
        op_ids = [op_id for op_id, _ in completed_ops]
        covered_op_ids = {
            row[0]
            for row in db.query(TimeEntry.operation_id)
            .filter(
                TimeEntry.company_id == company_id,
                TimeEntry.operation_id.in_(op_ids),
                TimeEntry.entry_type.in_(LABOR_ENTRY_TYPES),
                or_(TimeEntry.source.is_(None), TimeEntry.source.notin_(BASELINE_EXCLUDED_SOURCES)),
            )
            .distinct()
            .all()
        }
    for op_id, actual_end in completed_ops:
        bucket = _week_bucket(actual_end.date())
        bucket["ops_completed"] += 1
        if op_id in covered_op_ids:
            bucket["ops_covered"] += 1
    coverage_pct = _pct(len(covered_op_ids), len(completed_ops))

    # ── Backfill rate over the window's closed time entries ────────────────────
    # Outer-join the owning WO so entries on a soft-deleted work order drop out
    # of the rate, while entries with NO work order (nullable FK) stay counted.
    entries = (
        db.query(TimeEntry.clock_in, TimeEntry.source)
        .outerjoin(WorkOrder, TimeEntry.work_order_id == WorkOrder.id)
        .filter(
            TimeEntry.company_id == company_id,
            TimeEntry.clock_in >= start_dt,
            TimeEntry.clock_in <= end_dt,
            TimeEntry.clock_out.isnot(None),
            or_(WorkOrder.id.is_(None), WorkOrder.is_deleted == False),  # noqa: E712
        )
        .all()
    )
    backfill_entries = 0
    for clock_in, source in entries:
        bucket = _week_bucket(clock_in.date())
        bucket["entries"] += 1
        if source in BASELINE_EXCLUDED_SOURCES:
            backfill_entries += 1
            bucket["backfill_entries"] += 1
    backfill_rate = _pct(backfill_entries, len(entries))

    weekly_rows = [
        AdoptionWeek(
            week_start=week,
            operation_completions=int(b["completions"]),
            live_completions=int(b["live"]),
            backfill_completions=int(b["backfill"]),
            unknown_completions=int(b["unknown"]),
            digital_completion_pct=_pct(b["live"], b["completions"]),
            clock_in_coverage_pct=_pct(b["ops_covered"], b["ops_completed"]),
            time_entries=int(b["entries"]),
            backfill_entries=int(b["backfill_entries"]),
            backfill_rate_pct=_pct(b["backfill_entries"], b["entries"]),
        )
        for week, b in sorted(weekly.items())
    ]

    return AdoptionMetricsResponse(
        period_start=start,
        period_end=end,
        digital_completion_pct=_pct(live, total_completions),
        clock_in_coverage_pct=coverage_pct,
        backfill_rate_pct=backfill_rate,
        live_completions=live,
        backfill_completions=backfill,
        unknown_completions=unknown,
        weekly=weekly_rows,
        hidden_factory=get_hidden_factory_metrics(db, company_id, start, end),
        generated_at=datetime.utcnow(),
    )


def get_hidden_factory_metrics(db: Session, company_id: int, start: date, end: date) -> HiddenFactoryMetrics:
    start_dt = datetime.combine(start, datetime.min.time())
    end_dt = datetime.combine(end, datetime.max.time())

    # ── Rework share (hours + quantity), provenance-filtered ───────────────────
    baseline_filter = or_(TimeEntry.source.is_(None), TimeEntry.source.notin_(BASELINE_EXCLUDED_SOURCES))
    labor_rows = (
        db.query(
            TimeEntry.entry_type,
            func.coalesce(func.sum(TimeEntry.duration_hours), 0.0),
            func.coalesce(func.sum(TimeEntry.quantity_produced), 0.0),
        )
        .filter(
            TimeEntry.company_id == company_id,
            TimeEntry.clock_in >= start_dt,
            TimeEntry.clock_in <= end_dt,
            TimeEntry.clock_out.isnot(None),
            TimeEntry.entry_type.in_(LABOR_ENTRY_TYPES),
            baseline_filter,
        )
        .group_by(TimeEntry.entry_type)
        .all()
    )
    rework_hours = total_labor_hours = 0.0
    rework_qty = production_qty = 0.0
    for entry_type, hours, produced in labor_rows:
        hours = float(hours or 0)
        produced = float(produced or 0)
        total_labor_hours += hours
        if entry_type == TimeEntryType.REWORK:
            rework_hours += hours
            rework_qty += produced
        if entry_type in PRODUCTION_BEARING_ENTRY_TYPES:
            production_qty += produced

    excluded_hours = float(
        db.query(func.coalesce(func.sum(TimeEntry.duration_hours), 0.0))
        .filter(
            TimeEntry.company_id == company_id,
            TimeEntry.clock_in >= start_dt,
            TimeEntry.clock_in <= end_dt,
            TimeEntry.clock_out.isnot(None),
            TimeEntry.entry_type.in_(LABOR_ENTRY_TYPES),
            TimeEntry.source.in_(BASELINE_EXCLUDED_SOURCES),
        )
        .scalar()
        or 0.0
    )

    # ── Planned vs reactive maintenance mix ────────────────────────────────────
    maintenance_anchor = func.coalesce(MaintenanceWorkOrder.completed_at, MaintenanceWorkOrder.created_at)
    maintenance_rows = (
        db.query(MaintenanceWorkOrder.maintenance_type, func.count(MaintenanceWorkOrder.id))
        .filter(
            MaintenanceWorkOrder.company_id == company_id,
            maintenance_anchor >= start_dt,
            maintenance_anchor <= end_dt,
        )
        .group_by(MaintenanceWorkOrder.maintenance_type)
        .all()
    )
    planned = sum(count for mtype, count in maintenance_rows if mtype in PLANNED_MAINTENANCE_TYPES)
    reactive = sum(count for mtype, count in maintenance_rows if mtype in REACTIVE_MAINTENANCE_TYPES)
    maintenance = MaintenanceMixMetrics(
        planned_count=planned,
        reactive_count=reactive,
        planned_pct=_pct(planned, planned + reactive),
    )

    # ── MTBF / MTTR per work center from unplanned downtime ────────────────────
    downtime_rows = (
        db.query(
            DowntimeEvent.work_center_id,
            func.count(DowntimeEvent.id),
            func.coalesce(func.sum(DowntimeEvent.duration_minutes), 0.0),
            func.count(DowntimeEvent.duration_minutes),
        )
        .filter(
            DowntimeEvent.company_id == company_id,
            DowntimeEvent.planned_type == DowntimePlannedType.UNPLANNED,
            DowntimeEvent.start_time >= start_dt,
            DowntimeEvent.start_time <= end_dt,
        )
        .group_by(DowntimeEvent.work_center_id)
        .all()
    )
    staffed_run_rows = (
        db.query(TimeEntry.work_center_id, func.coalesce(func.sum(TimeEntry.duration_hours), 0.0))
        .filter(
            TimeEntry.company_id == company_id,
            TimeEntry.clock_in >= start_dt,
            TimeEntry.clock_in <= end_dt,
            TimeEntry.clock_out.isnot(None),
            TimeEntry.entry_type.in_(STAFFED_RUN_ENTRY_TYPES),
            TimeEntry.work_center_id.isnot(None),
            baseline_filter,
        )
        .group_by(TimeEntry.work_center_id)
        .all()
    )
    staffed_by_wc = {wc_id: float(hours or 0) for wc_id, hours in staffed_run_rows}

    reliability: List[WorkCenterReliability] = []
    if downtime_rows:
        wc_ids = [row[0] for row in downtime_rows]
        wc_info = {
            wc.id: wc
            for wc in db.query(WorkCenter).filter(WorkCenter.company_id == company_id, WorkCenter.id.in_(wc_ids)).all()
        }
        for wc_id, event_count, total_minutes, closed_count in downtime_rows:
            staffed_hours = staffed_by_wc.get(wc_id, 0.0)
            downtime_hours = float(total_minutes or 0) / 60.0
            wc = wc_info.get(wc_id)
            reliability.append(
                WorkCenterReliability(
                    work_center_id=wc_id,
                    work_center_code=wc.code if wc else None,
                    work_center_name=wc.name if wc else None,
                    unplanned_downtime_events=int(event_count),
                    unplanned_downtime_hours=round(downtime_hours, 2),
                    staffed_run_hours=round(staffed_hours, 2),
                    # MTBF = staffed run time between failures; None when nothing ran.
                    mtbf_hours=round(staffed_hours / event_count, 2) if event_count and staffed_hours > 0 else None,
                    # MTTR over events with a recorded (full-span) duration; an event
                    # still open has no honest duration yet and is excluded here.
                    mttr_hours=(round((float(total_minutes or 0) / closed_count) / 60.0, 2) if closed_count else None),
                )
            )
        reliability.sort(key=lambda row: row.unplanned_downtime_hours, reverse=True)

    return HiddenFactoryMetrics(
        rework_hours=round(rework_hours, 2),
        total_labor_hours=round(total_labor_hours, 2),
        rework_hours_pct=_pct(rework_hours, total_labor_hours),
        rework_quantity=round(rework_qty, 2),
        total_quantity=round(production_qty, 2),
        rework_quantity_pct=_pct(rework_qty, production_qty),
        maintenance=maintenance,
        reliability_by_work_center=reliability,
        excluded_backfill_import_hours=round(excluded_hours, 2),
    )
