"""
Analytics Service - Core aggregation and calculation logic
"""

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

from sqlalchemy import Date, and_, case, cast, func, or_
from sqlalchemy.orm import Session, joinedload

from app.models.downtime import DowntimeEvent, DowntimePlannedType
from app.models.inventory import InventoryItem, InventoryTransaction, TransactionType
from app.models.part import Part
from app.models.purchasing import InspectionStatus, POReceipt, Vendor
from app.models.quality import NCRStatus, NonConformanceReport
from app.models.quote import Quote, QuoteStatus
from app.models.shipping import Shipment, ShipmentStatus
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.schemas.analytics import (
    CostAnalysisResponse,
    CostBreakdown,
    DateGranularity,
    DefectPareto,
    InventoryAnalyticsResponse,
    InventoryTurnover,
    JobCostAnalysis,
    KPIDashboard,
    KPIValue,
    OEEComponents,
    OEEDataPoint,
    OEEResponse,
    ProductionDataPoint,
    ProductionTrendsResponse,
    PromiseHygieneRow,
    QualityMetricsResponse,
    ShipOTDCustomerRollup,
    ShipOTDReportResponse,
    ShipOTDRow,
    TrendDirection,
    VendorQuality,
)
from app.services.labor_cost_service import (
    is_approved_labor_required,
    is_labor_cost_rollup_enabled,
    resolve_labor_rates,
    resolve_overhead_rate,
)

logger = logging.getLogger(__name__)

# Default KPI targets
DEFAULT_TARGETS = {
    "oee": 85.0,
    "on_time_delivery": 95.0,
    "first_pass_yield": 98.0,
    "scrap_rate": 2.0,
    "open_ncrs": 0,
    "quote_win_rate": None,  # Track trend only
    "backlog_hours": None,  # Context only
    "inventory_turnover": 4.0,  # 4x per year
}

# ── OEE convention (Batch 8 / rank 11 — OEE-4/5/7) ──────────────────────────────
# OEE = Availability × Performance × Quality, computed per work center over a window
# from the STAFFED-time (clocked) convention:
#   * Availability = productive run time ÷ STAFFED (clocked) time at the WC.
#       Staffed time   = Σ duration_hours of EVERY clocked TimeEntry at the WC in the
#                        window (RUN/SETUP/REWORK/INSPECTION/DOWNTIME/BREAK), i.e. the
#                        time operators were on the clock there. This is per-WC actual
#                        labor, NOT the plant calendar, so idle/un-clocked machine time
#                        is excluded by construction and availability is no longer pinned
#                        near 1.0 against a whole-plant denominator (OEE-4).
#       Productive run = RUN+SETUP duration MINUS DowntimeEvent minutes logged for the
#                        WC in the window (real machine downtime within clocked time).
#   * Performance   = (ideal cycle × total pieces) ÷ productive run time, ideal cycle
#                     derived from WorkOrderOperation.run_time_per_piece (OEE-7), cap 1.0.
#   * Quality       = good ÷ (good + scrapped); scrapped from TimeEntry.quantity_scrapped
#                     on the production-bearing entry types (OEE-7), NOT assumed all-good.
# Pieces/scrap are counted from PRODUCTION_BEARING_ENTRY_TYPES uniformly across the OEE,
# performance, quality and ideal-hours legs so a quantity logged on any of them is never
# silently dropped (OEE-5). A window with no staffed time at the WC is genuinely
# uncomputable -> the value helpers return None ("n/a"), never a misleading 0/100.
PRODUCTION_BEARING_ENTRY_TYPES = [TimeEntryType.RUN, TimeEntryType.REWORK]


def calculate_trend(current: float, prior: float) -> Tuple[TrendDirection, float]:
    """Calculate trend direction and percentage change."""
    if prior == 0:
        return TrendDirection.FLAT, 0.0

    change_pct = ((current - prior) / abs(prior)) * 100

    if abs(change_pct) < 1.0:
        return TrendDirection.FLAT, change_pct
    elif change_pct > 0:
        return TrendDirection.UP, change_pct
    else:
        return TrendDirection.DOWN, change_pct


def get_date_range(
    period: str, custom_start: Optional[date] = None, custom_end: Optional[date] = None
) -> Tuple[date, date]:
    """Convert period string to date range."""
    today = date.today()

    if period == "custom" and custom_start and custom_end:
        return custom_start, custom_end

    period_map = {
        "today": (today, today),
        "7d": (today - timedelta(days=7), today),
        "30d": (today - timedelta(days=30), today),
        "90d": (today - timedelta(days=90), today),
        "ytd": (date(today.year, 1, 1), today),
    }

    return period_map.get(period, (today - timedelta(days=30), today))


def get_prior_period(start: date, end: date) -> Tuple[date, date]:
    """Get the prior period of same length for comparison."""
    period_length = (end - start).days
    prior_end = start - timedelta(days=1)
    prior_start = prior_end - timedelta(days=period_length)
    return prior_start, prior_end


class AnalyticsService:
    def __init__(self, db: Session, company_id: int):
        self.db = db
        self.company_id = company_id

    # ============ KPI CALCULATIONS ============

    def get_kpi_dashboard(self, start_date: date, end_date: date, work_center_id: Optional[int] = None) -> KPIDashboard:
        """Calculate all KPIs for the dashboard."""
        prior_start, prior_end = get_prior_period(start_date, end_date)

        return KPIDashboard(
            oee=self._calculate_oee_kpi(start_date, end_date, prior_start, prior_end, work_center_id),
            on_time_delivery=self._calculate_otd_kpi(start_date, end_date, prior_start, prior_end),
            on_time_delivery_ship=self._calculate_ship_otd_kpi(start_date, end_date, prior_start, prior_end),
            otif=self._calculate_otif_kpi(start_date, end_date, prior_start, prior_end),
            first_pass_yield=self._calculate_fpy_kpi(start_date, end_date, prior_start, prior_end),
            scrap_rate=self._calculate_scrap_kpi(start_date, end_date, prior_start, prior_end),
            open_ncrs=self._calculate_ncr_kpi(start_date, end_date, prior_start, prior_end),
            quote_win_rate=self._calculate_quote_kpi(start_date, end_date, prior_start, prior_end),
            backlog_hours=self._calculate_backlog_kpi(),
            inventory_turnover=self._calculate_turnover_kpi(start_date, end_date, prior_start, prior_end),
            period_start=start_date,
            period_end=end_date,
            generated_at=datetime.utcnow(),
        )

    def _calculate_oee_kpi(
        self, start: date, end: date, prior_start: date, prior_end: date, work_center_id: Optional[int] = None
    ) -> KPIValue:
        """Calculate OEE for the period.

        ``value``/``prior_value`` are ``None`` ("n/a") when there is no staffed time
        in the window (genuinely uncomputable) rather than a misleading 0% (OEE-4).
        """
        current_oee = self._get_oee_value(start, end, work_center_id)
        prior_oee = self._get_oee_value(prior_start, prior_end, work_center_id)
        sparkline = self._get_oee_sparkline(start, end, work_center_id)

        if current_oee is None or prior_oee is None:
            trend, change_pct = TrendDirection.FLAT, None
        else:
            trend, change_pct = calculate_trend(current_oee, prior_oee)

        return KPIValue(
            value=round(current_oee, 1) if current_oee is not None else None,
            target=DEFAULT_TARGETS["oee"],
            prior_value=round(prior_oee, 1) if prior_oee is not None else None,
            change_pct=round(change_pct, 1) if change_pct is not None else None,
            trend=trend,
            sparkline=sparkline,
        )

    def _downtime_event_hours(self, start: date, end: date, work_center_id: Optional[int] = None) -> float:
        """Logged machine-downtime hours for the WC(s) in the window (OEE-7).

        Reads ``DowntimeEvent`` (which the old availability calc never consulted) so
        real reported downtime reduces availability on top of un-clocked idle time.
        Tenant-scoped. Returns hours (the model stores ``duration_minutes``).
        """
        query = self.db.query(func.coalesce(func.sum(DowntimeEvent.duration_minutes), 0.0)).filter(
            DowntimeEvent.company_id == self.company_id,
            DowntimeEvent.start_time >= datetime.combine(start, datetime.min.time()),
            DowntimeEvent.start_time <= datetime.combine(end, datetime.max.time()),
            DowntimeEvent.planned_type == DowntimePlannedType.UNPLANNED,
        )
        if work_center_id:
            query = query.filter(DowntimeEvent.work_center_id == work_center_id)
        return float(query.scalar() or 0.0) / 60.0

    def _get_oee_value(self, start: date, end: date, work_center_id: Optional[int] = None) -> Optional[float]:
        """
        Calculate OEE = Availability × Performance × Quality on the STAFFED-time
        convention (Batch 8 / rank 11 — see ``PRODUCTION_BEARING_ENTRY_TYPES``).

        Availability = productive run ÷ STAFFED (clocked) time at the WC (OEE-4); the
        denominator is the operators' actual clocked hours in the window, not the
        whole-plant calendar, so idle/un-clocked time is excluded by construction and
        the metric is no longer pinned near 1.0. Productive run = RUN+SETUP duration
        minus logged ``DowntimeEvent`` hours (OEE-7). Performance = ideal cycle (from
        ``run_time_per_piece``) × pieces ÷ productive run. Quality = good ÷
        (good+scrapped). Pieces/scrap are counted across the production-bearing entry
        types uniformly (OEE-5).

        Returns ``None`` ("n/a") when the WC has no staffed time in the window — that
        is genuinely uncomputable, not zero (do not let "no data" read as 0% OEE).

        OPTIMIZATION: a single conditional-aggregation query (no per-row Python loop).
        """
        start_dt = datetime.combine(start, datetime.min.time())
        end_dt = datetime.combine(end, datetime.max.time())

        # Single aggregation query: staffed hours (ALL clocked entries), productive
        # run hours (RUN+SETUP), and production-bearing pieces/scrap in one pass.
        time_entry_stats = self.db.query(
            # Staffed/clocked hours: EVERY entry type (availability denominator, OEE-4)
            func.coalesce(func.sum(TimeEntry.duration_hours), 0).label('staffed_hours'),
            # Productive run hours: RUN + SETUP (availability numerator / perf denominator)
            func.coalesce(
                func.sum(
                    case(
                        (TimeEntry.entry_type.in_([TimeEntryType.RUN, TimeEntryType.SETUP]), TimeEntry.duration_hours),
                        else_=0,
                    )
                ),
                0,
            ).label('run_hours'),
            # Good pieces = quantity_produced (production-bearing entry types, OEE-5)
            func.coalesce(
                func.sum(
                    case(
                        (TimeEntry.entry_type.in_(PRODUCTION_BEARING_ENTRY_TYPES), TimeEntry.quantity_produced),
                        else_=0,
                    )
                ),
                0,
            ).label('good_units'),
            # Scrapped pieces (same entry-type set, OEE-5/OEE-7)
            func.coalesce(
                func.sum(
                    case(
                        (TimeEntry.entry_type.in_(PRODUCTION_BEARING_ENTRY_TYPES), TimeEntry.quantity_scrapped),
                        else_=0,
                    )
                ),
                0,
            ).label('scrapped_units'),
        ).filter(
            TimeEntry.company_id == self.company_id,
            TimeEntry.clock_in >= start_dt,
            TimeEntry.clock_in <= end_dt,
            TimeEntry.clock_out.isnot(None),
        )

        # G5-A opt-in: when REQUIRE_APPROVED_LABOR_FOR_COST is ON, the staffed/run hours
        # that feed this OEE/labor leg count ONLY supervisor-approved entries (uniform
        # with the job-costing recompute + completion cost rollup, so the metrics agree
        # on which labor is "counted"). Default OFF -> no predicate -> byte-identical OEE.
        if is_approved_labor_required(self.company_id):
            time_entry_stats = time_entry_stats.filter(TimeEntry.approved.isnot(None))

        if work_center_id:
            time_entry_stats = time_entry_stats.filter(TimeEntry.work_center_id == work_center_id)

        stats = time_entry_stats.first()

        staffed_hours = float(stats.staffed_hours or 0)
        run_hours = float(stats.run_hours or 0)
        # quantity_produced is the GOOD count (it increments quantity_complete on
        # clock-out), so total pieces cycled = good + scrap (OEE-7 — do not subtract
        # scrap from a good-only count, which would understate quality).
        good_units = int(stats.good_units or 0)
        scrapped_units = int(stats.scrapped_units or 0)
        total_units = good_units + scrapped_units

        # No staffed time at this WC in the window -> genuinely uncomputable (OEE-4/OEE-6
        # honesty): there is no availability denominator, so return n/a rather than 0.
        if staffed_hours <= 0:
            return None

        # Productive run = clocked RUN+SETUP minus reported machine downtime (OEE-7).
        downtime_hours = self._downtime_event_hours(start, end, work_center_id)
        productive_run_hours = max(0.0, run_hours - downtime_hours)

        # Ideal production hours from routing (run_time_per_piece) for the same
        # production-bearing pieces (OEE-7).
        ideal_hours = self._get_ideal_production_hours(start, end, work_center_id)

        # Availability = productive run ÷ staffed (clocked) time (OEE-4).
        availability = max(0.0, min(productive_run_hours / staffed_hours, 1.0))

        # Performance = ideal time ÷ productive run time (cap at 100%).
        performance = ideal_hours / productive_run_hours if productive_run_hours > 0 else 0.0
        performance = max(0.0, min(performance, 1.0))

        # Quality = good ÷ (good + scrapped) (not assumed all-good, OEE-7).
        quality = good_units / total_units if total_units > 0 else 1.0
        quality = max(0.0, min(quality, 1.0))

        oee = availability * performance * quality * 100
        return min(oee, 100.0)

    def _get_good_units(self, start: date, end: date, work_center_id: Optional[int] = None) -> int:
        """Get good units = produced − scrapped across production-bearing entries (OEE-5)."""
        query = self.db.query(
            func.coalesce(func.sum(TimeEntry.quantity_produced), 0)
            - func.coalesce(func.sum(TimeEntry.quantity_scrapped), 0)
        ).filter(
            TimeEntry.company_id == self.company_id,
            TimeEntry.clock_in >= datetime.combine(start, datetime.min.time()),
            TimeEntry.clock_in <= datetime.combine(end, datetime.max.time()),
            TimeEntry.entry_type.in_(PRODUCTION_BEARING_ENTRY_TYPES),
        )

        if work_center_id:
            query = query.filter(TimeEntry.work_center_id == work_center_id)

        result = query.scalar()
        return int(result or 0)

    def _get_ideal_production_hours(self, start: date, end: date, work_center_id: Optional[int] = None) -> float:
        """Ideal (standard) run hours for the pieces CYCLED in the window (OEE-7).

        ideal hours = Σ((quantity_produced + quantity_scrapped) × WorkOrderOperation.run_time_per_piece),
        i.e. the routing standard cycle time per piece — NOT a hardcoded 60 s — over
        the same production-bearing entry types the pieces are counted from (OEE-5),
        so the performance numerator and the piece count agree. ``run_time_per_piece``
        is stored in hours (alongside ``setup_time_hours``/``run_time_hours``), so the
        product is already in hours.

        Standard OEE Performance counts EVERY piece run through the cycle — including
        scrap — because cycle time was spent producing the scrapped pieces too (scrap
        is discounted separately in the Quality leg). Weighting by
        ``quantity_produced + quantity_scrapped`` here matches ``auto_calculate_oee``
        in ``app/api/endpoints/oee.py`` so the /analytics/kpis OEE headline and the
        persisted ``OEERecord`` agree for identical data.
        """
        query = (
            self.db.query(
                func.sum(
                    (TimeEntry.quantity_produced + TimeEntry.quantity_scrapped) * WorkOrderOperation.run_time_per_piece
                )
            )
            # Explicit left side: the (produced + scrapped) sum spans both mappers, so the
            # join FROM is ambiguous to autoresolution — anchor it on TimeEntry (mirrors
            # the same join in ``auto_calculate_oee``).
            .select_from(TimeEntry)
            .join(WorkOrderOperation, TimeEntry.operation_id == WorkOrderOperation.id)
            .filter(
                TimeEntry.company_id == self.company_id,
                TimeEntry.clock_in >= datetime.combine(start, datetime.min.time()),
                TimeEntry.clock_in <= datetime.combine(end, datetime.max.time()),
                TimeEntry.entry_type.in_(PRODUCTION_BEARING_ENTRY_TYPES),
            )
        )

        if work_center_id:
            query = query.filter(TimeEntry.work_center_id == work_center_id)

        result = query.scalar()
        return float(result or 0)

    def _get_oee_sparkline(self, start: date, end: date, work_center_id: Optional[int] = None) -> List[float]:
        """
        Get daily OEE values for sparkline on the SAME staffed-time convention as
        ``_get_oee_value`` (OEE-4): availability = productive run ÷ staffed (clocked)
        time, performance = ideal time ÷ productive run, quality = good ÷ total — NOT
        the old operating-÷-plant-capacity proxy. A day with no staffed time renders
        as ``0.0`` (sparklines are a ``List[float]`` glyph, not a headline figure; the
        headline value already carries the n/a via ``KPIValue.value``).

        OPTIMIZATION: two GROUP-BY-date aggregations (entry stats + routing ideal
        hours) instead of N per-day queries.
        """
        start_dt = datetime.combine(start, datetime.min.time())
        end_dt = datetime.combine(end, datetime.max.time())

        # Daily entry stats: staffed (all entries), productive run (RUN+SETUP),
        # production-bearing pieces/scrap.
        daily_stats_query = (
            self.db.query(
                cast(TimeEntry.clock_in, Date).label('entry_date'),
                func.coalesce(func.sum(TimeEntry.duration_hours), 0).label('staffed_hours'),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                TimeEntry.entry_type.in_([TimeEntryType.RUN, TimeEntryType.SETUP]),
                                TimeEntry.duration_hours,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ).label('run_hours'),
                func.coalesce(
                    func.sum(
                        case(
                            (TimeEntry.entry_type.in_(PRODUCTION_BEARING_ENTRY_TYPES), TimeEntry.quantity_produced),
                            else_=0,
                        )
                    ),
                    0,
                ).label('good_units'),
                func.coalesce(
                    func.sum(
                        case(
                            (TimeEntry.entry_type.in_(PRODUCTION_BEARING_ENTRY_TYPES), TimeEntry.quantity_scrapped),
                            else_=0,
                        )
                    ),
                    0,
                ).label('scrapped_units'),
            )
            .filter(
                TimeEntry.company_id == self.company_id,
                TimeEntry.clock_in >= start_dt,
                TimeEntry.clock_in <= end_dt,
                TimeEntry.clock_out.isnot(None),
            )
            .group_by(cast(TimeEntry.clock_in, Date))
            .order_by(cast(TimeEntry.clock_in, Date))
        )

        if work_center_id:
            daily_stats_query = daily_stats_query.filter(TimeEntry.work_center_id == work_center_id)

        daily_stats = daily_stats_query.all()
        stats_by_date = {row.entry_date: row for row in daily_stats}

        # Daily ideal (routing-standard) hours = Σ(pieces × run_time_per_piece).
        ideal_query = (
            self.db.query(
                cast(TimeEntry.clock_in, Date).label('entry_date'),
                func.coalesce(func.sum(TimeEntry.quantity_produced * WorkOrderOperation.run_time_per_piece), 0).label(
                    'ideal_hours'
                ),
            )
            .join(WorkOrderOperation, TimeEntry.operation_id == WorkOrderOperation.id)
            .filter(
                TimeEntry.company_id == self.company_id,
                TimeEntry.clock_in >= start_dt,
                TimeEntry.clock_in <= end_dt,
                TimeEntry.clock_out.isnot(None),
                TimeEntry.entry_type.in_(PRODUCTION_BEARING_ENTRY_TYPES),
            )
            .group_by(cast(TimeEntry.clock_in, Date))
        )
        if work_center_id:
            ideal_query = ideal_query.filter(TimeEntry.work_center_id == work_center_id)
        ideal_by_date = {row.entry_date: float(row.ideal_hours or 0) for row in ideal_query.all()}

        # Daily reported machine downtime (UNPLANNED DowntimeEvents), in hours.
        downtime_query = self.db.query(
            cast(DowntimeEvent.start_time, Date).label('event_date'),
            func.coalesce(func.sum(DowntimeEvent.duration_minutes), 0).label('downtime_minutes'),
        ).filter(
            DowntimeEvent.company_id == self.company_id,
            DowntimeEvent.start_time >= start_dt,
            DowntimeEvent.start_time <= end_dt,
            DowntimeEvent.planned_type == DowntimePlannedType.UNPLANNED,
        )
        if work_center_id:
            downtime_query = downtime_query.filter(DowntimeEvent.work_center_id == work_center_id)
        downtime_by_date = {
            row.event_date: float(row.downtime_minutes or 0) / 60.0
            for row in downtime_query.group_by(cast(DowntimeEvent.start_time, Date)).all()
        }

        # Calculate OEE for each day on the staffed-time convention.
        sparkline = []
        current = start
        while current <= end:
            row = stats_by_date.get(current)
            staffed = float(row.staffed_hours or 0) if row else 0.0
            if row is not None and staffed > 0:
                run_hours = float(row.run_hours or 0)
                good_units = int(row.good_units or 0)
                scrapped = int(row.scrapped_units or 0)
                total_units = good_units + scrapped  # good = produced; total = good + scrap
                productive_run = max(0.0, run_hours - downtime_by_date.get(current, 0.0))
                ideal_hours = ideal_by_date.get(current, 0.0)

                availability = max(0.0, min(productive_run / staffed, 1.0))
                performance = ideal_hours / productive_run if productive_run > 0 else 0.0
                performance = max(0.0, min(performance, 1.0))
                quality = good_units / total_units if total_units > 0 else 1.0
                quality = max(0.0, min(quality, 1.0))

                daily_oee = availability * performance * quality * 100
                sparkline.append(round(min(daily_oee, 100.0), 1))
            else:
                sparkline.append(0.0)

            current += timedelta(days=1)

        # Limit to last 7 points for sparkline
        return sparkline[-7:] if len(sparkline) > 7 else sparkline

    def _calculate_otd_kpi(self, start: date, end: date, prior_start: date, prior_end: date) -> KPIValue:
        """Calculate On-Time Delivery rate.

        ``value`` is ``None`` ("n/a") when no work order with a due date completed in
        the window (empty denominator) — not a misleading 100% (OEE-6).
        """
        current_otd = self._get_otd_value(start, end)
        prior_otd = self._get_otd_value(prior_start, prior_end)
        sparkline = self._get_otd_sparkline(start, end)

        if current_otd is None or prior_otd is None:
            trend, change_pct = TrendDirection.FLAT, None
        else:
            trend, change_pct = calculate_trend(current_otd, prior_otd)

        return KPIValue(
            value=round(current_otd, 1) if current_otd is not None else None,
            target=DEFAULT_TARGETS["on_time_delivery"],
            prior_value=round(prior_otd, 1) if prior_otd is not None else None,
            change_pct=round(change_pct, 1) if change_pct is not None else None,
            trend=trend,
            sparkline=sparkline,
        )

    def _get_otd_value(self, start: date, end: date) -> Optional[float]:
        """Calculate OTD percentage (OEE-6 honesty).

        A work order counts toward OTD when it reached COMPLETE *in the window* — its
        completion time is ``actual_end`` when stamped, else ``updated_at`` as a
        fallback so a COMPLETE WO that never got an ``actual_end`` is NOT silently
        dropped from the denominator (the old query excluded it, biasing OTD up).
        Such a WO is counted as **not on time** (no verifiable completion date), so a
        late job with a null ``actual_end`` can no longer read as on-time.

        Returns ``None`` ("n/a") when the denominator is empty (no completed WO with a
        due date in the window) rather than conflating "no data" with a perfect 100%.
        Tenant-scoped and soft-delete-filtered.
        """
        start_dt = datetime.combine(start, datetime.min.time())
        end_dt = datetime.combine(end, datetime.max.time())

        # Completion-time anchor: actual_end when present, else updated_at.
        completion_time = func.coalesce(WorkOrder.actual_end, WorkOrder.updated_at)

        completed = (
            self.db.query(WorkOrder)
            .filter(
                WorkOrder.company_id == self.company_id,
                WorkOrder.is_deleted == False,
                WorkOrder.status == WorkOrderStatus.COMPLETE,
                completion_time >= start_dt,
                completion_time <= end_dt,
                WorkOrder.due_date.isnot(None),
            )
            .all()
        )

        if not completed:
            return None

        # On-time only when a real actual_end exists AND is on/before the due date.
        # A COMPLETE WO with a null actual_end has no verifiable completion date and is
        # counted as NOT on time.
        on_time = sum(1 for wo in completed if wo.actual_end is not None and wo.actual_end.date() <= wo.due_date)
        return (on_time / len(completed)) * 100

    def _get_otd_sparkline(self, start: date, end: date) -> List[float]:
        """Weekly OTD for sparkline. A week with no completed WOs renders as 0.0 (the
        headline ``KPIValue.value`` carries the honest n/a)."""
        sparkline = []
        current = start
        while current <= end:
            week_end = min(current + timedelta(days=6), end)
            weekly_otd = self._get_otd_value(current, week_end)
            sparkline.append(round(weekly_otd, 1) if weekly_otd is not None else 0.0)
            current = week_end + timedelta(days=1)
        return sparkline[-7:] if len(sparkline) > 7 else sparkline

    # ── Ship-based OTD / OTIF (Lean Phase 1, issue #88) ─────────────────────────
    # The completion-based OTD above measures when a WO finished PRODUCTION; the
    # customer experiences when it SHIPPED. These legs measure Shipment.ship_date
    # against the promise, with precedence must_ship_by || due_date:
    #   * OTD (fulfillment-anchored): of the WOs whose FULL ordered quantity
    #     finished shipping in the window, the share whose full-ship date was
    #     on/before the promise. Multiple partial shipments roll up cumulatively;
    #     the full-ship date is the ship_date of the shipment that crossed the
    #     ordered quantity.
    #   * OTIF (promise-anchored): of the WOs PROMISED in the window, the share
    #     that had shipped IN FULL by their promise date — so an open WO past its
    #     promise counts as a miss the moment the promise passes, not never.
    # Only real shipments count: ship_date NOT NULL, not soft-deleted, not a
    # CANCELLED shipment. Both values return None ("n/a") on an empty denominator.

    def _work_order_promise(self, wo: WorkOrder) -> Tuple[Optional[str], Optional[date]]:
        """Promise precedence: must_ship_by || due_date. Returns (source, date)."""
        if wo.must_ship_by is not None:
            return "must_ship_by", wo.must_ship_by
        if wo.due_date is not None:
            return "due_date", wo.due_date
        return None, None

    def _shipment_facts(self, work_order_ids: List[int]) -> Dict[int, Dict]:
        """Cumulative shipment facts per WO id, from all its counted shipments.

        Facts: first_ship_date, last_ship_date, total_shipped, and full_ship(qty)
        support via the ordered (ship_date, quantity) list. Tenant-scoped.
        """
        facts: Dict[int, Dict] = {}
        if not work_order_ids:
            return facts
        rows = (
            self.db.query(Shipment.work_order_id, Shipment.ship_date, Shipment.quantity_shipped)
            .filter(
                Shipment.company_id == self.company_id,
                Shipment.is_deleted == False,  # noqa: E712
                Shipment.status != ShipmentStatus.CANCELLED,
                Shipment.ship_date.isnot(None),
                Shipment.work_order_id.in_(work_order_ids),
            )
            .order_by(Shipment.work_order_id, Shipment.ship_date)
            .all()
        )
        for wo_id, ship_date, qty in rows:
            entry = facts.setdefault(
                wo_id,
                {"shipments": [], "total_shipped": 0.0, "first_ship_date": None, "last_ship_date": None},
            )
            qty = float(qty or 0)
            entry["shipments"].append((ship_date, qty))
            entry["total_shipped"] += qty
            if entry["first_ship_date"] is None or ship_date < entry["first_ship_date"]:
                entry["first_ship_date"] = ship_date
            if entry["last_ship_date"] is None or ship_date > entry["last_ship_date"]:
                entry["last_ship_date"] = ship_date
        return facts

    @staticmethod
    def _full_ship_date(fact: Optional[Dict], ordered_qty: float) -> Optional[date]:
        """Date the cumulative shipped quantity first reached ``ordered_qty``."""
        if not fact or ordered_qty <= 0:
            return None
        running = 0.0
        for ship_date, qty in fact["shipments"]:  # already ordered by ship_date
            running += qty
            if running >= ordered_qty:
                return ship_date
        return None

    @staticmethod
    def _shipped_by(fact: Optional[Dict], cutoff: date) -> float:
        """Cumulative quantity shipped on/before ``cutoff``."""
        if not fact:
            return 0.0
        return sum(qty for ship_date, qty in fact["shipments"] if ship_date <= cutoff)

    def _ship_otd_candidates(self, start: date, end: date) -> List[WorkOrder]:
        """WOs with at least one counted shipment in the window (tenant-scoped).

        Any WO whose FULL-ship date lands in the window necessarily has the
        crossing shipment's ship_date in the window, so this prefilter is exact
        for the fulfillment-anchored OTD population. CANCELLED WOs are excluded
        exactly as in ``_get_otif_value`` (a cancelled order is not a delivery
        hit OR miss), so the two ship legs agree on the population rule.
        """
        return (
            self.db.query(WorkOrder)
            .join(Shipment, Shipment.work_order_id == WorkOrder.id)
            .filter(
                WorkOrder.company_id == self.company_id,
                WorkOrder.is_deleted == False,  # noqa: E712
                WorkOrder.status != WorkOrderStatus.CANCELLED,
                Shipment.company_id == self.company_id,
                Shipment.is_deleted == False,  # noqa: E712
                Shipment.status != ShipmentStatus.CANCELLED,
                Shipment.ship_date.isnot(None),
                Shipment.ship_date >= start,
                Shipment.ship_date <= end,
            )
            .distinct()
            .all()
        )

    def get_ship_otd_value(self, start: date, end: date) -> Optional[float]:
        """Public accessor for the ship-based OTD percentage (0-100, or None).

        Exists so external consumers (the wallboard KPI strip) don't reach into
        the underscore-private KPI helpers.
        """
        return self._get_ship_otd_value(start, end)

    def get_total_shipped(self, work_order_ids: List[int]) -> Dict[int, float]:
        """Public accessor: cumulative COUNTED shipped quantity per WO id.

        "Counted" is the ship-OTD rule (``_shipment_facts``): ship_date NOT
        NULL, not soft-deleted, not a CANCELLED shipment. WOs with no counted
        shipments are simply absent from the dict. Exists so the wallboard
        ship panel doesn't reach into the underscore-private helpers (the
        ``get_ship_otd_value`` precedent).
        """
        facts = self._shipment_facts(work_order_ids)
        return {wo_id: float(fact["total_shipped"]) for wo_id, fact in facts.items()}

    def _get_ship_otd_value(self, start: date, end: date) -> Optional[float]:
        """Fulfillment-anchored ship OTD. None ('n/a') on an empty denominator."""
        candidates = self._ship_otd_candidates(start, end)
        if not candidates:
            return None
        facts = self._shipment_facts([wo.id for wo in candidates])
        measured = 0
        on_time = 0
        for wo in candidates:
            _, promise = self._work_order_promise(wo)
            ordered = float(wo.quantity_ordered or 0)
            if promise is None or ordered <= 0:
                continue
            full_ship = self._full_ship_date(facts.get(wo.id), ordered)
            if full_ship is None or not (start <= full_ship <= end):
                continue
            measured += 1
            if full_ship <= promise:
                on_time += 1
        if measured == 0:
            return None
        return (on_time / measured) * 100

    def _get_otif_value(self, start: date, end: date) -> Optional[float]:
        """Promise-anchored OTIF. None ('n/a') on an empty denominator.

        CANCELLED WOs are excluded (a cancelled order is not a delivery miss).
        """
        promise = func.coalesce(WorkOrder.must_ship_by, WorkOrder.due_date)
        promised = (
            self.db.query(WorkOrder)
            .filter(
                WorkOrder.company_id == self.company_id,
                WorkOrder.is_deleted == False,  # noqa: E712
                WorkOrder.status != WorkOrderStatus.CANCELLED,
                WorkOrder.quantity_ordered > 0,
                promise.isnot(None),
                promise >= start,
                promise <= end,
            )
            .all()
        )
        if not promised:
            return None
        facts = self._shipment_facts([wo.id for wo in promised])
        in_full = 0
        for wo in promised:
            _, promise_date = self._work_order_promise(wo)
            shipped = self._shipped_by(facts.get(wo.id), promise_date)
            if shipped >= float(wo.quantity_ordered or 0):
                in_full += 1
        return (in_full / len(promised)) * 100

    def _weekly_sparkline(self, start: date, end: date, value_fn) -> List[float]:
        """Weekly sparkline over ``value_fn(week_start, week_end)`` (0.0 for n/a weeks)."""
        sparkline = []
        current = start
        while current <= end:
            week_end = min(current + timedelta(days=6), end)
            weekly = value_fn(current, week_end)
            sparkline.append(round(weekly, 1) if weekly is not None else 0.0)
            current = week_end + timedelta(days=1)
        return sparkline[-7:] if len(sparkline) > 7 else sparkline

    def _calculate_ship_otd_kpi(self, start: date, end: date, prior_start: date, prior_end: date) -> KPIValue:
        """Ship-based OTD KPI (same target as the completion-based leg)."""
        current = self._get_ship_otd_value(start, end)
        prior = self._get_ship_otd_value(prior_start, prior_end)
        if current is None or prior is None:
            trend, change_pct = TrendDirection.FLAT, None
        else:
            trend, change_pct = calculate_trend(current, prior)
        return KPIValue(
            value=round(current, 1) if current is not None else None,
            target=DEFAULT_TARGETS["on_time_delivery"],
            prior_value=round(prior, 1) if prior is not None else None,
            change_pct=round(change_pct, 1) if change_pct is not None else None,
            trend=trend,
            sparkline=self._weekly_sparkline(start, end, self._get_ship_otd_value),
        )

    def _calculate_otif_kpi(self, start: date, end: date, prior_start: date, prior_end: date) -> KPIValue:
        """OTIF KPI (promise-anchored on-time-in-full)."""
        current = self._get_otif_value(start, end)
        prior = self._get_otif_value(prior_start, prior_end)
        if current is None or prior is None:
            trend, change_pct = TrendDirection.FLAT, None
        else:
            trend, change_pct = calculate_trend(current, prior)
        return KPIValue(
            value=round(current, 1) if current is not None else None,
            target=DEFAULT_TARGETS["on_time_delivery"],
            prior_value=round(prior, 1) if prior is not None else None,
            change_pct=round(change_pct, 1) if change_pct is not None else None,
            trend=trend,
            sparkline=self._weekly_sparkline(start, end, self._get_otif_value),
        )

    def get_ship_otd_report(self, start: date, end: date) -> ShipOTDReportResponse:
        """Per-WO ship-vs-promise detail + per-customer rollup + promise hygiene.

        Population: WOs with a counted shipment in the window UNION WOs promised
        in the window (so unshipped-past-promise WOs surface as misses). The
        hygiene section lists WOs shipped in the window or still open that carry
        NEITHER must_ship_by nor due_date -- they are unmeasurable and poison the
        denominator until someone enters a promise.
        """
        today = date.today()
        shipped_in_window = self._ship_otd_candidates(start, end)

        promise = func.coalesce(WorkOrder.must_ship_by, WorkOrder.due_date)
        promised_in_window = (
            self.db.query(WorkOrder)
            .options(joinedload(WorkOrder.part))
            .filter(
                WorkOrder.company_id == self.company_id,
                WorkOrder.is_deleted == False,  # noqa: E712
                WorkOrder.status != WorkOrderStatus.CANCELLED,
                promise.isnot(None),
                promise >= start,
                promise <= end,
            )
            .all()
        )

        by_id: Dict[int, WorkOrder] = {wo.id: wo for wo in shipped_in_window}
        for wo in promised_in_window:
            by_id.setdefault(wo.id, wo)
        work_orders = list(by_id.values())
        facts = self._shipment_facts(list(by_id.keys()))

        rows: List[ShipOTDRow] = []
        for wo in work_orders:
            source, promise_date = self._work_order_promise(wo)
            ordered = float(wo.quantity_ordered or 0)
            fact = facts.get(wo.id)
            total_shipped = float(fact["total_shipped"]) if fact else 0.0
            full_ship = self._full_ship_date(fact, ordered)
            fully_shipped = full_ship is not None

            on_time: Optional[bool] = None
            days_late: Optional[int] = None
            if promise_date is not None:
                if fully_shipped:
                    on_time = full_ship <= promise_date
                    days_late = (full_ship - promise_date).days
                elif promise_date < today:
                    # Promise passed with the order not fully shipped: a live miss.
                    on_time = False
                    days_late = (today - promise_date).days

            rows.append(
                ShipOTDRow(
                    work_order_id=wo.id,
                    work_order_number=wo.work_order_number,
                    customer_name=wo.customer_name,
                    part_number=wo.part.part_number if wo.part else None,
                    status=wo.status.value if hasattr(wo.status, "value") else str(wo.status),
                    quantity_ordered=ordered,
                    quantity_shipped=total_shipped,
                    promise_source=source,
                    promise_date=promise_date,
                    first_ship_date=fact["first_ship_date"] if fact else None,
                    last_ship_date=fact["last_ship_date"] if fact else None,
                    full_ship_date=full_ship,
                    fully_shipped=fully_shipped,
                    on_time=on_time,
                    days_late=days_late,
                )
            )
        rows.sort(key=lambda r: (r.promise_date or date.max, r.work_order_number))

        # Per-customer rollup over the DETERMINABLE rows (on_time is not None).
        rollup: Dict[str, Dict] = defaultdict(lambda: {"work_orders": 0, "on_time": 0, "late": 0, "late_days": []})
        for row in rows:
            if row.on_time is None:
                continue
            bucket = rollup[row.customer_name or "Unknown"]
            bucket["work_orders"] += 1
            if row.on_time:
                bucket["on_time"] += 1
            else:
                bucket["late"] += 1
                if row.days_late is not None:
                    bucket["late_days"].append(row.days_late)
        by_customer = [
            ShipOTDCustomerRollup(
                customer_name=name,
                work_orders=b["work_orders"],
                on_time=b["on_time"],
                late=b["late"],
                otd_pct=round(b["on_time"] / b["work_orders"] * 100, 1) if b["work_orders"] else None,
                avg_days_late=(round(sum(b["late_days"]) / len(b["late_days"]), 1) if b["late_days"] else None),
            )
            for name, b in sorted(rollup.items(), key=lambda kv: kv[1]["work_orders"], reverse=True)
        ]

        # Promise hygiene: shipped-in-window or open WOs with NEITHER promise field.
        open_statuses = [
            WorkOrderStatus.DRAFT,
            WorkOrderStatus.RELEASED,
            WorkOrderStatus.IN_PROGRESS,
            WorkOrderStatus.ON_HOLD,
        ]
        promiseless_conditions = [WorkOrder.status.in_(open_statuses)]
        shipped_ids = [wo.id for wo in shipped_in_window]
        if shipped_ids:
            promiseless_conditions.append(WorkOrder.id.in_(shipped_ids))
        promiseless = (
            self.db.query(WorkOrder)
            .filter(
                WorkOrder.company_id == self.company_id,
                WorkOrder.is_deleted == False,  # noqa: E712
                WorkOrder.must_ship_by.is_(None),
                WorkOrder.due_date.is_(None),
                or_(*promiseless_conditions),
            )
            .all()
        )
        hygiene_facts = self._shipment_facts([wo.id for wo in promiseless])
        promise_hygiene = [
            PromiseHygieneRow(
                work_order_id=wo.id,
                work_order_number=wo.work_order_number,
                customer_name=wo.customer_name,
                status=wo.status.value if hasattr(wo.status, "value") else str(wo.status),
                quantity_ordered=float(wo.quantity_ordered or 0),
                quantity_shipped=float(hygiene_facts[wo.id]["total_shipped"]) if wo.id in hygiene_facts else 0.0,
                last_ship_date=hygiene_facts[wo.id]["last_ship_date"] if wo.id in hygiene_facts else None,
            )
            for wo in sorted(promiseless, key=lambda w: w.work_order_number)
        ]

        otd_ship = self._get_ship_otd_value(start, end)
        otif = self._get_otif_value(start, end)
        return ShipOTDReportResponse(
            period_start=start,
            period_end=end,
            otd_ship_pct=round(otd_ship, 1) if otd_ship is not None else None,
            otif_pct=round(otif, 1) if otif is not None else None,
            rows=rows,
            by_customer=by_customer,
            promise_hygiene=promise_hygiene,
            generated_at=datetime.utcnow(),
        )

    def _calculate_fpy_kpi(self, start: date, end: date, prior_start: date, prior_end: date) -> KPIValue:
        """Calculate First Pass Yield."""
        current_fpy = self._get_fpy_value(start, end)
        prior_fpy = self._get_fpy_value(prior_start, prior_end)

        trend, change_pct = calculate_trend(current_fpy, prior_fpy)

        return KPIValue(
            value=round(current_fpy, 1),
            target=DEFAULT_TARGETS["first_pass_yield"],
            prior_value=round(prior_fpy, 1),
            change_pct=round(change_pct, 1),
            trend=trend,
            sparkline=[],
        )

    def _get_fpy_value(self, start: date, end: date) -> float:
        """Calculate First Pass Yield percentage.

        FPY = (RUN-produced − scrap − rework) ÷ RUN-produced. The first-pass
        denominator is the RUN production; scrap is counted across the
        production-bearing entry types (OEE-5) so scrap logged on a REWORK clock-out
        is no longer silently dropped, and REWORK production is subtracted as
        not-first-pass.
        """
        # First-pass denominator: RUN production only.
        total = (
            self.db.query(func.sum(TimeEntry.quantity_produced))
            .filter(
                TimeEntry.company_id == self.company_id,
                TimeEntry.clock_in >= datetime.combine(start, datetime.min.time()),
                TimeEntry.clock_in <= datetime.combine(end, datetime.max.time()),
                TimeEntry.entry_type == TimeEntryType.RUN,
            )
            .scalar()
            or 0
        )

        # Scrap across ALL production-bearing entry types (OEE-5 consistency).
        scrapped = (
            self.db.query(func.sum(TimeEntry.quantity_scrapped))
            .filter(
                TimeEntry.company_id == self.company_id,
                TimeEntry.clock_in >= datetime.combine(start, datetime.min.time()),
                TimeEntry.clock_in <= datetime.combine(end, datetime.max.time()),
                TimeEntry.entry_type.in_(PRODUCTION_BEARING_ENTRY_TYPES),
            )
            .scalar()
            or 0
        )

        # Rework production: not first pass.
        rework = (
            self.db.query(func.sum(TimeEntry.quantity_produced))
            .filter(
                TimeEntry.company_id == self.company_id,
                TimeEntry.clock_in >= datetime.combine(start, datetime.min.time()),
                TimeEntry.clock_in <= datetime.combine(end, datetime.max.time()),
                TimeEntry.entry_type == TimeEntryType.REWORK,
            )
            .scalar()
            or 0
        )

        if total == 0:
            return 100.0

        good_first_time = total - scrapped - rework
        return max(0.0, (good_first_time / total) * 100)

    def _calculate_scrap_kpi(self, start: date, end: date, prior_start: date, prior_end: date) -> KPIValue:
        """Calculate Scrap Rate."""
        current_scrap = self._get_scrap_value(start, end)
        prior_scrap = self._get_scrap_value(prior_start, prior_end)

        # For scrap, DOWN is good
        trend, change_pct = calculate_trend(current_scrap, prior_scrap)
        if trend == TrendDirection.DOWN:
            trend = TrendDirection.UP  # Flip for display (lower is better)
        elif trend == TrendDirection.UP:
            trend = TrendDirection.DOWN

        return KPIValue(
            value=round(current_scrap, 2),
            target=DEFAULT_TARGETS["scrap_rate"],
            prior_value=round(prior_scrap, 2),
            change_pct=round(change_pct, 1),
            trend=trend,
            sparkline=[],
        )

    def _get_scrap_value(self, start: date, end: date) -> float:
        """Calculate scrap rate percentage = scrapped ÷ produced (existing convention,
        where ``quantity_produced`` is the good count).

        Both legs are summed across the production-bearing entry types (OEE-5) so scrap
        reported on a REWORK clock-out is no longer silently dropped from the rate.
        """
        result = (
            self.db.query(
                func.coalesce(func.sum(TimeEntry.quantity_produced), 0).label('total'),
                func.coalesce(func.sum(TimeEntry.quantity_scrapped), 0).label('scrapped'),
            )
            .filter(
                TimeEntry.company_id == self.company_id,
                TimeEntry.clock_in >= datetime.combine(start, datetime.min.time()),
                TimeEntry.clock_in <= datetime.combine(end, datetime.max.time()),
                TimeEntry.entry_type.in_(PRODUCTION_BEARING_ENTRY_TYPES),
            )
            .first()
        )

        total = float(result.total or 0)
        scrapped = float(result.scrapped or 0)

        if total == 0:
            return 0.0

        return (scrapped / total) * 100

    def _calculate_ncr_kpi(self, start: date, end: date, prior_start: date, prior_end: date) -> KPIValue:
        """Calculate Open NCR count."""
        current_ncrs = (
            self.db.query(func.count(NonConformanceReport.id))
            .filter(
                NonConformanceReport.company_id == self.company_id,
                NonConformanceReport.status.in_(
                    [NCRStatus.OPEN, NCRStatus.UNDER_REVIEW, NCRStatus.PENDING_DISPOSITION]
                ),
            )
            .scalar()
            or 0
        )

        # Prior period: NCRs that were open at that time (approximation)
        prior_ncrs = (
            self.db.query(func.count(NonConformanceReport.id))
            .filter(
                NonConformanceReport.company_id == self.company_id,
                NonConformanceReport.created_at <= datetime.combine(prior_end, datetime.max.time()),
                or_(NonConformanceReport.closed_date.is_(None), NonConformanceReport.closed_date > prior_end),
            )
            .scalar()
            or 0
        )

        trend, change_pct = calculate_trend(float(current_ncrs), float(prior_ncrs))
        # For NCRs, DOWN is good
        if trend == TrendDirection.DOWN:
            trend = TrendDirection.UP
        elif trend == TrendDirection.UP:
            trend = TrendDirection.DOWN

        return KPIValue(
            value=float(current_ncrs),
            target=DEFAULT_TARGETS["open_ncrs"],
            prior_value=float(prior_ncrs),
            change_pct=round(change_pct, 1) if prior_ncrs > 0 else 0,
            trend=trend,
            sparkline=[],
        )

    def _calculate_quote_kpi(self, start: date, end: date, prior_start: date, prior_end: date) -> KPIValue:
        """Calculate Quote Win Rate."""
        current_rate = self._get_quote_win_rate(start, end)
        prior_rate = self._get_quote_win_rate(prior_start, prior_end)

        trend, change_pct = calculate_trend(current_rate, prior_rate)

        return KPIValue(
            value=round(current_rate, 1),
            target=None,
            prior_value=round(prior_rate, 1),
            change_pct=round(change_pct, 1),
            trend=trend,
            sparkline=[],
        )

    def _get_quote_win_rate(self, start: date, end: date) -> float:
        """Calculate quote win rate percentage."""
        # Quotes that reached a final status in the period
        total = (
            self.db.query(func.count(Quote.id))
            .filter(
                Quote.company_id == self.company_id,
                Quote.updated_at >= datetime.combine(start, datetime.min.time()),
                Quote.updated_at <= datetime.combine(end, datetime.max.time()),
                Quote.status.in_([QuoteStatus.ACCEPTED, QuoteStatus.REJECTED, QuoteStatus.CONVERTED]),
            )
            .scalar()
            or 0
        )

        won = (
            self.db.query(func.count(Quote.id))
            .filter(
                Quote.company_id == self.company_id,
                Quote.updated_at >= datetime.combine(start, datetime.min.time()),
                Quote.updated_at <= datetime.combine(end, datetime.max.time()),
                Quote.status.in_([QuoteStatus.ACCEPTED, QuoteStatus.CONVERTED]),
            )
            .scalar()
            or 0
        )

        if total == 0:
            return 0.0

        return (won / total) * 100

    def _calculate_backlog_kpi(self) -> KPIValue:
        """Calculate current backlog hours."""
        backlog = (
            self.db.query(func.sum(WorkOrder.estimated_hours))
            .filter(
                WorkOrder.company_id == self.company_id,
                WorkOrder.status.in_([WorkOrderStatus.RELEASED, WorkOrderStatus.IN_PROGRESS]),
            )
            .scalar()
            or 0
        )

        return KPIValue(
            value=round(backlog, 1),
            target=None,
            prior_value=None,
            change_pct=None,
            trend=TrendDirection.FLAT,
            sparkline=[],
        )

    def _calculate_turnover_kpi(self, start: date, end: date, prior_start: date, prior_end: date) -> KPIValue:
        """Calculate Inventory Turnover."""
        current_turnover = self._get_turnover_value(start, end)
        prior_turnover = self._get_turnover_value(prior_start, prior_end)

        trend, change_pct = calculate_trend(current_turnover, prior_turnover)

        return KPIValue(
            value=round(current_turnover, 2),
            target=DEFAULT_TARGETS["inventory_turnover"],
            prior_value=round(prior_turnover, 2),
            change_pct=round(change_pct, 1),
            trend=trend,
            sparkline=[],
        )

    def _get_turnover_value(self, start: date, end: date) -> float:
        """Calculate inventory turnover ratio."""
        # COGS approximation: sum of issued inventory value
        cogs = (
            self.db.query(func.sum(func.abs(InventoryTransaction.total_cost)))
            .filter(
                InventoryTransaction.company_id == self.company_id,
                InventoryTransaction.transaction_type == TransactionType.ISSUE,
                InventoryTransaction.created_at >= datetime.combine(start, datetime.min.time()),
                InventoryTransaction.created_at <= datetime.combine(end, datetime.max.time()),
            )
            .scalar()
            or 0
        )

        # Average inventory value
        avg_inventory = (
            self.db.query(func.avg(InventoryItem.quantity_on_hand * InventoryItem.unit_cost))
            .filter(InventoryItem.company_id == self.company_id, InventoryItem.is_active == True)
            .scalar()
            or 1
        )  # Avoid division by zero

        # Annualize if period is less than a year
        days = (end - start).days + 1
        annualized_cogs = (cogs / days) * 365 if days < 365 else cogs

        return annualized_cogs / avg_inventory if avg_inventory > 0 else 0

    # ============ OEE DETAILED ============

    def get_oee_details(
        self,
        start_date: date,
        end_date: date,
        work_center_id: Optional[int] = None,
        granularity: DateGranularity = DateGranularity.DAY,
    ) -> OEEResponse:
        """Get detailed OEE breakdown.

        ``OEEComponents``/``OEEDataPoint`` carry ``oee: float`` (chart series), so an
        uncomputable window (``_get_oee_value`` -> None) coalesces to 0.0 here; the
        headline KPI on ``/kpis`` is the path that surfaces the honest n/a.
        """
        # Summary for period
        summary = OEEComponents(
            availability=0,
            performance=0,
            quality=0,
            oee=self._get_oee_value(start_date, end_date, work_center_id) or 0.0,
        )

        # Time series
        time_series = []
        current = start_date
        while current <= end_date:
            if granularity == DateGranularity.DAY:
                period_end = current
                next_period = current + timedelta(days=1)
            elif granularity == DateGranularity.WEEK:
                period_end = min(current + timedelta(days=6), end_date)
                next_period = current + timedelta(days=7)
            else:  # MONTH
                if current.month == 12:
                    period_end = date(current.year + 1, 1, 1) - timedelta(days=1)
                else:
                    period_end = date(current.year, current.month + 1, 1) - timedelta(days=1)
                period_end = min(period_end, end_date)
                next_period = period_end + timedelta(days=1)

            oee_val = self._get_oee_value(current, period_end, work_center_id) or 0.0
            time_series.append(
                OEEDataPoint(
                    date=current,
                    work_center_id=work_center_id,
                    availability=0,  # Would need detailed calc
                    performance=0,
                    quality=0,
                    oee=oee_val,
                    planned_time=0,
                    operating_time=0,
                    downtime=0,
                    ideal_cycle_time=0,
                    actual_cycle_time=0,
                    total_units=0,
                    good_units=0,
                    defect_units=0,
                )
            )
            current = next_period

        # By work center
        by_work_center = []
        work_centers = (
            self.db.query(WorkCenter)
            .filter(WorkCenter.company_id == self.company_id, WorkCenter.is_active == True)
            .all()
        )
        for wc in work_centers:
            oee_val = self._get_oee_value(start_date, end_date, wc.id) or 0.0
            by_work_center.append(
                OEEDataPoint(
                    date=start_date,
                    work_center_id=wc.id,
                    work_center_name=wc.name,
                    availability=0,
                    performance=0,
                    quality=0,
                    oee=oee_val,
                    planned_time=0,
                    operating_time=0,
                    downtime=0,
                    ideal_cycle_time=0,
                    actual_cycle_time=0,
                    total_units=0,
                    good_units=0,
                    defect_units=0,
                )
            )

        return OEEResponse(summary=summary, time_series=time_series, by_work_center=by_work_center)

    # ============ PRODUCTION TRENDS ============

    def get_production_trends(
        self,
        start_date: date,
        end_date: date,
        group_by: Optional[str] = None,  # work_center, part, customer
        granularity: DateGranularity = DateGranularity.DAY,
    ) -> ProductionTrendsResponse:
        """Get production trend data."""
        # Combine time entries and simplified operations without double-counting
        time_entries_query = self.db.query(
            func.date(TimeEntry.clock_in).label('date'),
            func.sum(TimeEntry.quantity_produced).label('units_produced'),
            func.sum(TimeEntry.quantity_scrapped).label('units_scrapped'),
            func.sum(TimeEntry.duration_hours).label('total_hours'),
        ).filter(
            TimeEntry.company_id == self.company_id,
            TimeEntry.clock_in >= datetime.combine(start_date, datetime.min.time()),
            TimeEntry.clock_in <= datetime.combine(end_date, datetime.max.time()),
            TimeEntry.entry_type == TimeEntryType.RUN,
        )

        if group_by == "work_center":
            time_entries_query = time_entries_query.add_columns(TimeEntry.work_center_id.label('group_key')).group_by(
                func.date(TimeEntry.clock_in), TimeEntry.work_center_id
            )
        else:
            time_entries_query = time_entries_query.group_by(func.date(TimeEntry.clock_in))

        time_entry_results = time_entries_query.order_by(func.date(TimeEntry.clock_in)).all()

        # Only include operations that have no time entries (simplified workflow)
        ops_query = (
            self.db.query(
                func.date(WorkOrderOperation.actual_end).label('date'),
                func.sum(WorkOrderOperation.quantity_complete).label('units_produced'),
                func.sum(WorkOrderOperation.quantity_scrapped).label('units_scrapped'),
                func.sum((WorkOrderOperation.actual_setup_hours + WorkOrderOperation.actual_run_hours)).label(
                    'total_hours'
                ),
            )
            .outerjoin(
                TimeEntry,
                and_(TimeEntry.operation_id == WorkOrderOperation.id, TimeEntry.entry_type == TimeEntryType.RUN),
            )
            .filter(
                WorkOrderOperation.company_id == self.company_id,
                WorkOrderOperation.actual_end.isnot(None),
                WorkOrderOperation.actual_end >= datetime.combine(start_date, datetime.min.time()),
                WorkOrderOperation.actual_end <= datetime.combine(end_date, datetime.max.time()),
                WorkOrderOperation.status == OperationStatus.COMPLETE,
                TimeEntry.id.is_(None),
            )
        )

        if group_by == "work_center":
            ops_query = ops_query.add_columns(WorkOrderOperation.work_center_id.label('group_key')).group_by(
                func.date(WorkOrderOperation.actual_end), WorkOrderOperation.work_center_id
            )
        else:
            ops_query = ops_query.group_by(func.date(WorkOrderOperation.actual_end))

        ops_results = ops_query.order_by(func.date(WorkOrderOperation.actual_end)).all()

        # Merge results by date + optional group
        combined: Dict[Tuple[date, Optional[str]], Dict[str, float]] = defaultdict(
            lambda: {"units_produced": 0.0, "units_scrapped": 0.0, "total_hours": 0.0}
        )

        for row in time_entry_results:
            key = (row.date, str(row.group_key) if hasattr(row, 'group_key') else None)
            combined[key]["units_produced"] += float(row.units_produced or 0)
            combined[key]["units_scrapped"] += float(row.units_scrapped or 0)
            combined[key]["total_hours"] += float(row.total_hours or 0)

        for row in ops_results:
            key = (row.date, str(row.group_key) if hasattr(row, 'group_key') else None)
            combined[key]["units_produced"] += float(row.units_produced or 0)
            combined[key]["units_scrapped"] += float(row.units_scrapped or 0)
            combined[key]["total_hours"] += float(row.total_hours or 0)

        results = [
            {
                "date": result_date,
                "group_key": group_key,
                "units_produced": values["units_produced"],
                "units_scrapped": values["units_scrapped"],
                "total_hours": values["total_hours"],
            }
            for (result_date, group_key), values in sorted(combined.items(), key=lambda x: x[0][0])
        ]

        # Build time series
        time_series = []
        for row in results:
            time_series.append(
                ProductionDataPoint(
                    date=row["date"],
                    group_key=str(row["group_key"]) if row["group_key"] is not None else None,
                    units_produced=int(row["units_produced"] or 0),
                    units_scrapped=int(row["units_scrapped"] or 0),
                    work_orders_completed=0,  # Would need separate query
                    work_orders_started=0,
                    total_hours=float(row["total_hours"] or 0),
                )
            )

        # Calculate totals
        totals = {
            "units_produced": sum(dp.units_produced for dp in time_series),
            "units_scrapped": sum(dp.units_scrapped for dp in time_series),
            "total_hours": sum(dp.total_hours for dp in time_series),
        }

        return ProductionTrendsResponse(time_series=time_series, totals=totals, by_group=None)

    # ============ COST ANALYSIS ============

    def get_cost_analysis(
        self, start_date: date, end_date: date, work_order_id: Optional[int] = None
    ) -> CostAnalysisResponse:
        """Get cost analysis for jobs.

        COST-5: the labor-cost breakdown is derived from each operation's actual hours
        at the SHARED work-center labor rate (``labor_cost_service``) -- the SAME source
        the completion cost rollup uses -- replacing the old hardcoded
        ``actual_hours * 50``. The material leg reads the issued-material cost the rollup
        computed (Batch-6 ISSUE txns), so this report and ``WorkOrder.actual_cost`` agree.

        OPT-IN gating (Batch 7): the computed labor + overhead legs only surface when
        ``LABOR_COST_ROLLUP_ENABLED`` is ON for this company. Flag-OFF (the default), the
        labor/overhead figures are reported as 0 (not tracked) -- the SAME stance the
        live and reconcile completion paths take, so a WO completed under either path
        surfaces $0 computed labor here regardless of how it completed. The material leg
        is NOT gated: it is real issued-material from inventory (Batch-6 ISSUE txns), not
        a labor-estimate-derived figure, so it stays accurate either way. Tenant-scoped on
        ``self.company_id`` throughout (the flag is also resolved for that company).
        """
        # Resolve the OPT-IN cost-rollup flag for THIS company. Flag-OFF: surface no
        # computed labor/overhead so the report is consistently zero across live- and
        # reconcile-completed WOs (no path leaks a non-zero labor figure flag-OFF).
        rollup_enabled = is_labor_cost_rollup_enabled(self.company_id)
        query = self.db.query(WorkOrder).filter(
            WorkOrder.company_id == self.company_id,
            WorkOrder.status.in_([WorkOrderStatus.COMPLETE, WorkOrderStatus.CLOSED]),
        )

        if work_order_id:
            query = query.filter(WorkOrder.id == work_order_id)
        else:
            query = query.filter(
                WorkOrder.actual_end >= datetime.combine(start_date, datetime.min.time()),
                WorkOrder.actual_end <= datetime.combine(end_date, datetime.max.time()),
            )

        work_orders = query.options(joinedload(WorkOrder.operations), joinedload(WorkOrder.part)).all()

        # Batch-resolve labor rates for every work center the operations touch in ONE
        # query (COST-5 shared resolver), so the per-operation labor cost reflects WHERE
        # the work happened and the report agrees with the completion rollup. Only needed
        # when the rollup flag is ON; flag-OFF we skip the rate resolution entirely (the
        # labor/overhead legs are reported as 0, so no rate is consulted).
        labor_rates: dict[Optional[int], float] = {}
        overhead_rate = 0.0
        if rollup_enabled:
            all_wc_ids = [op.work_center_id for wo in work_orders for op in (wo.operations or [])]
            labor_rates = resolve_labor_rates(self.db, self.company_id, all_wc_ids)
            overhead_rate = resolve_overhead_rate(self.db, self.company_id, None)

        jobs = []
        total_estimated = 0
        total_actual = 0

        for wo in work_orders:
            variance = wo.actual_cost - wo.estimated_cost
            variance_pct = (variance / wo.estimated_cost * 100) if wo.estimated_cost > 0 else 0

            # Labor + overhead are surfaced ONLY when the cost-rollup flag is ON for this
            # company; flag-OFF they report 0 (not tracked), uniformly across live- and
            # reconcile-completed WOs. Material is always real issued-material -- not gated.
            labor_cost = 0.0
            overhead_cost = 0.0
            if rollup_enabled:
                for op in wo.operations or []:
                    op_hours = float(op.actual_setup_hours or 0) + float(op.actual_run_hours or 0)
                    if op_hours <= 0:
                        continue
                    rate = labor_rates.get(op.work_center_id, labor_rates[None])
                    labor_cost += op_hours * rate
                    overhead_cost += op_hours * overhead_rate
            material_cost = self._issued_material_cost(wo.id)

            jobs.append(
                JobCostAnalysis(
                    work_order_id=wo.id,
                    work_order_number=wo.work_order_number,
                    part_number=wo.part.part_number if wo.part else None,
                    customer_name=wo.customer_name,
                    estimated_cost=wo.estimated_cost,
                    actual_cost=wo.actual_cost,
                    variance=variance,
                    variance_pct=variance_pct,
                    cost_breakdown=CostBreakdown(
                        material_cost=material_cost,
                        labor_cost=labor_cost,
                        overhead_cost=overhead_cost,
                        outside_services=0,
                        total_cost=wo.actual_cost,
                    ),
                )
            )

            total_estimated += wo.estimated_cost
            total_actual += wo.actual_cost

        avg_variance = ((total_actual - total_estimated) / total_estimated * 100) if total_estimated > 0 else 0

        return CostAnalysisResponse(
            jobs=jobs,
            summary={
                "total_estimated": total_estimated,
                "total_actual": total_actual,
                "total_variance": total_actual - total_estimated,
            },
            avg_margin=0,  # Would need pricing data
            avg_variance_pct=avg_variance,
            time_series=[],
        )

    def _issued_material_cost(self, work_order_id: int) -> float:
        """Cost of material ISSUEd to a WO (Batch-6 ISSUE txns), tenant-scoped.

        Mirrors ``completion_cost_service._issued_material_cost`` so the analytics
        material leg equals the rollup's material leg. ISSUE quantities/costs are stored
        negative, so the magnitude is summed.
        """
        total = (
            self.db.query(func.coalesce(func.sum(func.abs(InventoryTransaction.total_cost)), 0.0))
            .filter(
                InventoryTransaction.company_id == self.company_id,
                InventoryTransaction.reference_type == "work_order",
                InventoryTransaction.reference_id == work_order_id,
                InventoryTransaction.transaction_type == TransactionType.ISSUE,
            )
            .scalar()
        )
        return float(total or 0.0)

    # ============ QUALITY METRICS ============

    def get_quality_metrics(self, start_date: date, end_date: date, metric_type: str = "all") -> QualityMetricsResponse:
        """Get quality metrics and charts data."""
        # Defect Pareto
        defect_counts = (
            self.db.query(NonConformanceReport.disposition, func.count(NonConformanceReport.id).label('count'))
            .filter(
                NonConformanceReport.company_id == self.company_id,
                NonConformanceReport.created_at >= datetime.combine(start_date, datetime.min.time()),
                NonConformanceReport.created_at <= datetime.combine(end_date, datetime.max.time()),
            )
            .group_by(NonConformanceReport.disposition)
            .order_by(func.count(NonConformanceReport.id).desc())
            .all()
        )

        total_defects = sum(d.count for d in defect_counts)
        defect_pareto = []
        cumulative = 0
        for d in defect_counts:
            pct = (d.count / total_defects * 100) if total_defects > 0 else 0
            cumulative += pct
            defect_pareto.append(
                DefectPareto(
                    defect_type=d.disposition.value if d.disposition else "unknown",
                    count=d.count,
                    percentage=round(pct, 1),
                    cumulative_pct=round(cumulative, 1),
                )
            )

        # Vendor quality
        vendor_stats = (
            self.db.query(
                Vendor.id,
                Vendor.name,
                func.count(POReceipt.id).label('receipts'),
                # "Accepted" = taken into stock without rejection. Dock-to-stock
                # receipts (no inspection required) now land as NOT_REQUIRED rather
                # than PASSED, so count both — otherwise the acceptance rate for a
                # vendor received dock-to-stock (the receiving default since PR #127)
                # would collapse toward 0% even with zero rejections.
                # NOTE: get_quality_metrics is currently unreachable — the vendor
                # join below (POReceipt.po_line.has(...)) is a pre-existing malformed
                # predicate that raises at query build (tracked separately). This
                # predicate is kept correct-by-construction so that when that join is
                # fixed, NOT_REQUIRED already counts as accepted (no 0%-rate regression).
                func.sum(
                    case(
                        (POReceipt.inspection_status.in_([InspectionStatus.PASSED, InspectionStatus.NOT_REQUIRED]), 1),
                        else_=0,
                    )
                ).label('accepted'),
                func.sum(case((POReceipt.inspection_status == InspectionStatus.FAILED, 1), else_=0)).label('rejected'),
            )
            .join(POReceipt, POReceipt.po_line.has(purchase_order=Vendor.purchase_orders))
            .filter(
                Vendor.company_id == self.company_id,
                POReceipt.company_id == self.company_id,
                POReceipt.received_at >= datetime.combine(start_date, datetime.min.time()),
                POReceipt.received_at <= datetime.combine(end_date, datetime.max.time()),
            )
            .group_by(Vendor.id, Vendor.name)
            .all()
        )

        by_vendor = []
        for v in vendor_stats:
            acceptance_rate = (v.accepted / v.receipts * 100) if v.receipts > 0 else 0
            by_vendor.append(
                VendorQuality(
                    vendor_id=v.id,
                    vendor_name=v.name,
                    receipts_count=v.receipts,
                    accepted_count=v.accepted or 0,
                    rejected_count=v.rejected or 0,
                    acceptance_rate=round(acceptance_rate, 1),
                    ncr_count=0,  # Would need to join NCRs
                )
            )

        # Summary
        fpy = self._get_fpy_value(start_date, end_date)
        scrap = self._get_scrap_value(start_date, end_date)

        return QualityMetricsResponse(
            summary={
                "first_pass_yield": fpy,
                "scrap_rate": scrap,
                "total_ncrs": total_defects,
                "defect_rate": 100 - fpy,
            },
            defect_pareto=defect_pareto,
            time_series=[],
            by_vendor=by_vendor,
            control_limits={
                "ucl": 5.0,  # Upper control limit
                "lcl": 0.0,  # Lower control limit
                "center": 2.0,  # Center line
            },
        )

    # ============ INVENTORY ANALYTICS ============

    def get_inventory_analytics(
        self, start_date: date, end_date: date, category: Optional[str] = None
    ) -> InventoryAnalyticsResponse:
        """
        Get inventory turnover and analytics.

        OPTIMIZATION: Uses bulk queries with GROUP BY instead of per-part queries.
        Before: 2 queries per part × 50 parts = 100+ database round trips
        After:  3 queries total (COGS aggregation + inventory aggregation + parts)

        Query reduction: ~97% fewer database calls
        """
        # OPTIMIZATION: Bulk query for COGS by part (single query instead of N)
        cogs_by_part = (
            self.db.query(
                InventoryTransaction.part_id, func.sum(func.abs(InventoryTransaction.total_cost)).label('cogs')
            )
            .filter(
                InventoryTransaction.company_id == self.company_id,
                InventoryTransaction.transaction_type == TransactionType.ISSUE,
                InventoryTransaction.created_at >= datetime.combine(start_date, datetime.min.time()),
                InventoryTransaction.created_at <= datetime.combine(end_date, datetime.max.time()),
            )
            .group_by(InventoryTransaction.part_id)
            .all()
        )

        # Build lookup dict for O(1) access
        cogs_map = {row.part_id: float(row.cogs or 0) for row in cogs_by_part}

        # OPTIMIZATION: Bulk query for average inventory value by part
        avg_inv_by_part = (
            self.db.query(
                InventoryItem.part_id,
                func.avg(InventoryItem.quantity_on_hand * InventoryItem.unit_cost).label('avg_inv'),
            )
            .filter(InventoryItem.company_id == self.company_id, InventoryItem.is_active == True)
            .group_by(InventoryItem.part_id)
            .all()
        )

        # Build lookup dict for O(1) access
        avg_inv_map = {row.part_id: float(row.avg_inv or 1) for row in avg_inv_by_part}

        # Get parts (single query)
        parts = self.db.query(Part).filter(Part.company_id == self.company_id, Part.is_active == True).limit(50).all()

        # Calculate turnover using pre-fetched data (no additional queries)
        days = (end_date - start_date).days + 1
        turnover_data = []

        for part in parts:
            cogs = cogs_map.get(part.id, 0)
            avg_inv = avg_inv_map.get(part.id, 1)

            # Ensure we don't divide by zero
            if avg_inv <= 0:
                avg_inv = 1

            annualized_cogs = (cogs / days) * 365 if days < 365 else cogs
            turnover = annualized_cogs / avg_inv if avg_inv > 0 else 0

            turnover_data.append(
                InventoryTurnover(
                    part_id=part.id,
                    part_number=part.part_number,
                    avg_inventory_value=avg_inv,
                    cogs=annualized_cogs,
                    turnover_ratio=round(turnover, 2),
                    days_on_hand=round(365 / turnover, 1) if turnover > 0 else 999,
                )
            )

        # Sort by turnover (low first = problem items)
        turnover_data.sort(key=lambda x: x.turnover_ratio)

        # Calculate total inventory value (single query)
        total_inventory_value = (
            self.db.query(func.sum(InventoryItem.quantity_on_hand * InventoryItem.unit_cost))
            .filter(InventoryItem.company_id == self.company_id, InventoryItem.is_active == True)
            .scalar()
            or 0
        )

        return InventoryAnalyticsResponse(
            turnover_by_category=[],
            low_turnover_items=turnover_data[:10],
            stock_trends=[],
            summary={
                "total_inventory_value": total_inventory_value,
                "avg_turnover": (
                    sum(t.turnover_ratio for t in turnover_data) / len(turnover_data) if turnover_data else 0
                ),
            },
        )
