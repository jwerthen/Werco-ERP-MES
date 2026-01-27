"""
Analytics Service - Core aggregation and calculation logic
"""
import logging
from datetime import datetime, date, timedelta
from typing import Optional, List, Dict, Any, Tuple
from sqlalchemy.orm import Session
from sqlalchemy import func, case, and_, or_, extract, cast, Date
from collections import defaultdict

from app.models.work_order import WorkOrder, WorkOrderOperation, WorkOrderStatus, OperationStatus
from app.models.work_center import WorkCenter
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.quality import NonConformanceReport, NCRStatus, NCRDisposition
from app.models.inventory import InventoryItem, InventoryTransaction, TransactionType
from app.models.purchasing import POReceipt, InspectionStatus, Vendor
from app.models.quote import Quote, QuoteStatus
from app.models.part import Part
from app.schemas.analytics import (
    KPIValue, KPIDashboard, TrendDirection,
    OEEComponents, OEEDataPoint, OEEResponse,
    ProductionDataPoint, ProductionTrendsResponse,
    CostBreakdown, JobCostAnalysis, CostAnalysisResponse,
    DefectPareto, QualityDataPoint, VendorQuality, QualityMetricsResponse,
    InventoryTurnover, StockLevel, InventoryAnalyticsResponse,
    DateGranularity
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


def get_date_range(period: str, custom_start: Optional[date] = None, custom_end: Optional[date] = None) -> Tuple[date, date]:
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
    def __init__(self, db: Session):
        self.db = db
    
    # ============ KPI CALCULATIONS ============
    
    def get_kpi_dashboard(
        self, 
        start_date: date, 
        end_date: date,
        work_center_id: Optional[int] = None
    ) -> KPIDashboard:
        """Calculate all KPIs for the dashboard."""
        prior_start, prior_end = get_prior_period(start_date, end_date)
        
        return KPIDashboard(
            oee=self._calculate_oee_kpi(start_date, end_date, prior_start, prior_end, work_center_id),
            on_time_delivery=self._calculate_otd_kpi(start_date, end_date, prior_start, prior_end),
            first_pass_yield=self._calculate_fpy_kpi(start_date, end_date, prior_start, prior_end),
            scrap_rate=self._calculate_scrap_kpi(start_date, end_date, prior_start, prior_end),
            open_ncrs=self._calculate_ncr_kpi(start_date, end_date, prior_start, prior_end),
            quote_win_rate=self._calculate_quote_kpi(start_date, end_date, prior_start, prior_end),
            backlog_hours=self._calculate_backlog_kpi(),
            inventory_turnover=self._calculate_turnover_kpi(start_date, end_date, prior_start, prior_end),
            period_start=start_date,
            period_end=end_date,
            generated_at=datetime.utcnow()
        )
    
    def _calculate_oee_kpi(
        self, start: date, end: date, 
        prior_start: date, prior_end: date,
        work_center_id: Optional[int] = None
    ) -> KPIValue:
        """Calculate OEE for the period."""
        current_oee = self._get_oee_value(start, end, work_center_id)
        prior_oee = self._get_oee_value(prior_start, prior_end, work_center_id)
        sparkline = self._get_oee_sparkline(start, end, work_center_id)
        
        trend, change_pct = calculate_trend(current_oee, prior_oee)
        
        return KPIValue(
            value=round(current_oee, 1),
            target=DEFAULT_TARGETS["oee"],
            prior_value=round(prior_oee, 1),
            change_pct=round(change_pct, 1),
            trend=trend,
            sparkline=sparkline
        )
    
    def _get_oee_value(self, start: date, end: date, work_center_id: Optional[int] = None) -> float:
        """
        Calculate OEE = Availability × Performance × Quality.
        
        OPTIMIZATION: Uses SQL aggregation instead of loading all time entries into memory.
        Before: 1 query to load N entries + Python loop = O(N) memory, O(N) CPU
        After:  1 aggregation query = O(1) memory, database-optimized
        
        Query reduction: From loading potentially 10,000+ rows to 1 aggregated result
        """
        start_dt = datetime.combine(start, datetime.min.time())
        end_dt = datetime.combine(end, datetime.max.time())
        
        # OPTIMIZATION: Single aggregation query for all time entry metrics
        # Uses conditional aggregation (SUM with CASE) to compute multiple metrics in one pass
        time_entry_stats = self.db.query(
            # Operating hours: RUN + SETUP time
            func.coalesce(
                func.sum(
                    case(
                        (TimeEntry.entry_type.in_([TimeEntryType.RUN, TimeEntryType.SETUP]), 
                         TimeEntry.duration_hours),
                        else_=0
                    )
                ), 0
            ).label('operating_hours'),
            # Downtime hours
            func.coalesce(
                func.sum(
                    case(
                        (TimeEntry.entry_type == TimeEntryType.DOWNTIME, TimeEntry.duration_hours),
                        else_=0
                    )
                ), 0
            ).label('downtime_hours'),
            # Total units produced (from RUN entries only)
            func.coalesce(
                func.sum(
                    case(
                        (TimeEntry.entry_type == TimeEntryType.RUN, TimeEntry.quantity_produced),
                        else_=0
                    )
                ), 0
            ).label('total_units'),
            # Scrapped units
            func.coalesce(
                func.sum(
                    case(
                        (TimeEntry.entry_type == TimeEntryType.RUN, TimeEntry.quantity_scrapped),
                        else_=0
                    )
                ), 0
            ).label('scrapped_units')
        ).filter(
            TimeEntry.clock_in >= start_dt,
            TimeEntry.clock_in <= end_dt,
            TimeEntry.clock_out.isnot(None)
        )
        
        if work_center_id:
            time_entry_stats = time_entry_stats.filter(TimeEntry.work_center_id == work_center_id)
        
        stats = time_entry_stats.first()
        
        # Extract values with null safety
        total_operating_hours = float(stats.operating_hours or 0)
        total_downtime_hours = float(stats.downtime_hours or 0)
        total_units = int(stats.total_units or 0)
        scrapped_units = int(stats.scrapped_units or 0)
        good_units = total_units - scrapped_units
        
        # No production data - return 0
        if total_units == 0 and total_operating_hours == 0:
            return 0.0
        
        # Get planned capacity from work centers (single query with aggregation)
        days_in_period = (end - start).days + 1
        capacity_query = self.db.query(
            func.coalesce(func.sum(WorkCenter.capacity_hours_per_day), 0)
        ).filter(WorkCenter.is_active == True)
        
        if work_center_id:
            capacity_query = capacity_query.filter(WorkCenter.id == work_center_id)
        
        daily_capacity = float(capacity_query.scalar() or 8.0)
        total_planned_hours = daily_capacity * days_in_period
        
        # Get ideal production hours (already optimized with SQL)
        ideal_hours = self._get_ideal_production_hours(start, end, work_center_id)
        
        # Calculate OEE components with division-by-zero protection
        # Availability = (Planned - Downtime) / Planned
        availability = (total_planned_hours - total_downtime_hours) / total_planned_hours if total_planned_hours > 0 else 0
        availability = max(0, min(availability, 1.0))  # Clamp to [0, 1]
        
        # Performance = Ideal Time / Actual Operating Time
        performance = ideal_hours / total_operating_hours if total_operating_hours > 0 else 0
        performance = max(0, min(performance, 1.0))  # Cap at 100%
        
        # Quality = Good Units / Total Units
        quality = good_units / total_units if total_units > 0 else 1.0
        quality = max(0, min(quality, 1.0))  # Clamp to [0, 1]
        
        oee = availability * performance * quality * 100
        return min(oee, 100.0)
    
    def _get_good_units(self, start: date, end: date, work_center_id: Optional[int] = None) -> int:
        """Get units that passed inspection first time."""
        # Units produced minus scrapped
        query = self.db.query(
            func.sum(TimeEntry.quantity_produced) - func.coalesce(func.sum(TimeEntry.quantity_scrapped), 0)
        ).filter(
            TimeEntry.clock_in >= datetime.combine(start, datetime.min.time()),
            TimeEntry.clock_in <= datetime.combine(end, datetime.max.time()),
            TimeEntry.entry_type == TimeEntryType.RUN
        )
        
        if work_center_id:
            query = query.filter(TimeEntry.work_center_id == work_center_id)
        
        result = query.scalar()
        return int(result or 0)
    
    def _get_ideal_production_hours(self, start: date, end: date, work_center_id: Optional[int] = None) -> float:
        """Calculate ideal hours based on routing times and actual production."""
        # Sum of (quantity_produced * run_time_per_piece) for all completed operations
        query = self.db.query(
            func.sum(TimeEntry.quantity_produced * WorkOrderOperation.run_time_per_piece)
        ).join(
            WorkOrderOperation, TimeEntry.operation_id == WorkOrderOperation.id
        ).filter(
            TimeEntry.clock_in >= datetime.combine(start, datetime.min.time()),
            TimeEntry.clock_in <= datetime.combine(end, datetime.max.time()),
            TimeEntry.entry_type == TimeEntryType.RUN
        )
        
        if work_center_id:
            query = query.filter(TimeEntry.work_center_id == work_center_id)
        
        result = query.scalar()
        return float(result or 0)
    
    def _get_oee_sparkline(self, start: date, end: date, work_center_id: Optional[int] = None) -> List[float]:
        """
        Get daily OEE values for sparkline.
        
        OPTIMIZATION: Single query with GROUP BY date instead of N separate queries.
        Before: 1 query per day × 30 days = 30+ queries
        After:  1 query with daily aggregation
        """
        start_dt = datetime.combine(start, datetime.min.time())
        end_dt = datetime.combine(end, datetime.max.time())
        
        # Get daily aggregated stats in a single query
        daily_stats_query = self.db.query(
            cast(TimeEntry.clock_in, Date).label('entry_date'),
            # Operating hours (RUN + SETUP)
            func.coalesce(
                func.sum(
                    case(
                        (TimeEntry.entry_type.in_([TimeEntryType.RUN, TimeEntryType.SETUP]), 
                         TimeEntry.duration_hours),
                        else_=0
                    )
                ), 0
            ).label('operating_hours'),
            # Downtime
            func.coalesce(
                func.sum(
                    case(
                        (TimeEntry.entry_type == TimeEntryType.DOWNTIME, TimeEntry.duration_hours),
                        else_=0
                    )
                ), 0
            ).label('downtime_hours'),
            # Total units
            func.coalesce(
                func.sum(
                    case(
                        (TimeEntry.entry_type == TimeEntryType.RUN, TimeEntry.quantity_produced),
                        else_=0
                    )
                ), 0
            ).label('total_units'),
            # Scrapped units
            func.coalesce(
                func.sum(
                    case(
                        (TimeEntry.entry_type == TimeEntryType.RUN, TimeEntry.quantity_scrapped),
                        else_=0
                    )
                ), 0
            ).label('scrapped_units')
        ).filter(
            TimeEntry.clock_in >= start_dt,
            TimeEntry.clock_in <= end_dt,
            TimeEntry.clock_out.isnot(None)
        ).group_by(
            cast(TimeEntry.clock_in, Date)
        ).order_by(
            cast(TimeEntry.clock_in, Date)
        )
        
        if work_center_id:
            daily_stats_query = daily_stats_query.filter(TimeEntry.work_center_id == work_center_id)
        
        daily_stats = daily_stats_query.all()
        
        # Build lookup by date
        stats_by_date = {row.entry_date: row for row in daily_stats}
        
        # Get daily capacity (same for all days)
        capacity_query = self.db.query(
            func.coalesce(func.sum(WorkCenter.capacity_hours_per_day), 8.0)
        ).filter(WorkCenter.is_active == True)
        
        if work_center_id:
            capacity_query = capacity_query.filter(WorkCenter.id == work_center_id)
        
        daily_capacity = float(capacity_query.scalar() or 8.0)
        
        # Calculate OEE for each day
        sparkline = []
        current = start
        while current <= end:
            if current in stats_by_date:
                row = stats_by_date[current]
                operating = float(row.operating_hours or 0)
                downtime = float(row.downtime_hours or 0)
                total_units = int(row.total_units or 0)
                scrapped = int(row.scrapped_units or 0)
                good_units = total_units - scrapped
                
                # Calculate components
                availability = (daily_capacity - downtime) / daily_capacity if daily_capacity > 0 else 0
                availability = max(0, min(availability, 1.0))
                
                # For sparkline, use simplified performance (operating/capacity)
                performance = operating / daily_capacity if daily_capacity > 0 else 0
                performance = max(0, min(performance, 1.0))
                
                quality = good_units / total_units if total_units > 0 else 1.0
                quality = max(0, min(quality, 1.0))
                
                daily_oee = availability * performance * quality * 100
                sparkline.append(round(min(daily_oee, 100.0), 1))
            else:
                sparkline.append(0.0)
            
            current += timedelta(days=1)
        
        # Limit to last 7 points for sparkline
        return sparkline[-7:] if len(sparkline) > 7 else sparkline
    
    def _calculate_otd_kpi(self, start: date, end: date, prior_start: date, prior_end: date) -> KPIValue:
        """Calculate On-Time Delivery rate."""
        current_otd = self._get_otd_value(start, end)
        prior_otd = self._get_otd_value(prior_start, prior_end)
        sparkline = self._get_otd_sparkline(start, end)
        
        trend, change_pct = calculate_trend(current_otd, prior_otd)
        
        return KPIValue(
            value=round(current_otd, 1),
            target=DEFAULT_TARGETS["on_time_delivery"],
            prior_value=round(prior_otd, 1),
            change_pct=round(change_pct, 1),
            trend=trend,
            sparkline=sparkline
        )
    
    def _get_otd_value(self, start: date, end: date) -> float:
        """Calculate OTD percentage."""
        # Work orders completed in period with due date
        completed = self.db.query(WorkOrder).filter(
            WorkOrder.status == WorkOrderStatus.COMPLETE,
            WorkOrder.actual_end >= datetime.combine(start, datetime.min.time()),
            WorkOrder.actual_end <= datetime.combine(end, datetime.max.time()),
            WorkOrder.due_date.isnot(None)
        ).all()
        
        if not completed:
            return 100.0
        
        on_time = sum(1 for wo in completed if wo.actual_end.date() <= wo.due_date)
        return (on_time / len(completed)) * 100
    
    def _get_otd_sparkline(self, start: date, end: date) -> List[float]:
        """Weekly OTD for sparkline."""
        sparkline = []
        current = start
        while current <= end:
            week_end = min(current + timedelta(days=6), end)
            weekly_otd = self._get_otd_value(current, week_end)
            sparkline.append(round(weekly_otd, 1))
            current = week_end + timedelta(days=1)
        return sparkline[-7:] if len(sparkline) > 7 else sparkline
    
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
            sparkline=[]
        )
    
    def _get_fpy_value(self, start: date, end: date) -> float:
        """Calculate FPY percentage."""
        # Total units produced vs units that passed first inspection
        result = self.db.query(
            func.sum(TimeEntry.quantity_produced).label('total'),
            func.sum(TimeEntry.quantity_scrapped).label('scrapped')
        ).filter(
            TimeEntry.clock_in >= datetime.combine(start, datetime.min.time()),
            TimeEntry.clock_in <= datetime.combine(end, datetime.max.time()),
            TimeEntry.entry_type == TimeEntryType.RUN
        ).first()
        
        total = result.total or 0
        scrapped = result.scrapped or 0
        
        # Also count rework as not first pass
        rework = self.db.query(func.sum(TimeEntry.quantity_produced)).filter(
            TimeEntry.clock_in >= datetime.combine(start, datetime.min.time()),
            TimeEntry.clock_in <= datetime.combine(end, datetime.max.time()),
            TimeEntry.entry_type == TimeEntryType.REWORK
        ).scalar() or 0
        
        if total == 0:
            return 100.0
        
        good_first_time = total - scrapped - rework
        return (good_first_time / total) * 100
    
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
            sparkline=[]
        )
    
    def _get_scrap_value(self, start: date, end: date) -> float:
        """Calculate scrap rate percentage."""
        result = self.db.query(
            func.sum(TimeEntry.quantity_produced).label('total'),
            func.sum(TimeEntry.quantity_scrapped).label('scrapped')
        ).filter(
            TimeEntry.clock_in >= datetime.combine(start, datetime.min.time()),
            TimeEntry.clock_in <= datetime.combine(end, datetime.max.time()),
            TimeEntry.entry_type == TimeEntryType.RUN
        ).first()
        
        total = result.total or 0
        scrapped = result.scrapped or 0
        
        if total == 0:
            return 0.0
        
        return (scrapped / total) * 100
    
    def _calculate_ncr_kpi(self, start: date, end: date, prior_start: date, prior_end: date) -> KPIValue:
        """Calculate Open NCR count."""
        current_ncrs = self.db.query(func.count(NonConformanceReport.id)).filter(
            NonConformanceReport.status.in_([NCRStatus.OPEN, NCRStatus.UNDER_REVIEW, NCRStatus.PENDING_DISPOSITION])
        ).scalar() or 0
        
        # Prior period: NCRs that were open at that time (approximation)
        prior_ncrs = self.db.query(func.count(NonConformanceReport.id)).filter(
            NonConformanceReport.created_at <= datetime.combine(prior_end, datetime.max.time()),
            or_(
                NonConformanceReport.closed_date.is_(None),
                NonConformanceReport.closed_date > prior_end
            )
        ).scalar() or 0
        
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
            sparkline=[]
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
            sparkline=[]
        )
    
    def _get_quote_win_rate(self, start: date, end: date) -> float:
        """Calculate quote win rate percentage."""
        # Quotes that reached a final status in the period
        total = self.db.query(func.count(Quote.id)).filter(
            Quote.updated_at >= datetime.combine(start, datetime.min.time()),
            Quote.updated_at <= datetime.combine(end, datetime.max.time()),
            Quote.status.in_([QuoteStatus.ACCEPTED, QuoteStatus.REJECTED, QuoteStatus.CONVERTED])
        ).scalar() or 0
        
        won = self.db.query(func.count(Quote.id)).filter(
            Quote.updated_at >= datetime.combine(start, datetime.min.time()),
            Quote.updated_at <= datetime.combine(end, datetime.max.time()),
            Quote.status.in_([QuoteStatus.ACCEPTED, QuoteStatus.CONVERTED])
        ).scalar() or 0
        
        if total == 0:
            return 0.0
        
        return (won / total) * 100
    
    def _calculate_backlog_kpi(self) -> KPIValue:
        """Calculate current backlog hours."""
        backlog = self.db.query(func.sum(WorkOrder.estimated_hours)).filter(
            WorkOrder.status.in_([WorkOrderStatus.RELEASED, WorkOrderStatus.IN_PROGRESS])
        ).scalar() or 0
        
        return KPIValue(
            value=round(backlog, 1),
            target=None,
            prior_value=None,
            change_pct=None,
            trend=TrendDirection.FLAT,
            sparkline=[]
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
            sparkline=[]
        )
    
    def _get_turnover_value(self, start: date, end: date) -> float:
        """Calculate inventory turnover ratio."""
        # COGS approximation: sum of issued inventory value
        cogs = self.db.query(
            func.sum(func.abs(InventoryTransaction.total_cost))
        ).filter(
            InventoryTransaction.transaction_type == TransactionType.ISSUE,
            InventoryTransaction.created_at >= datetime.combine(start, datetime.min.time()),
            InventoryTransaction.created_at <= datetime.combine(end, datetime.max.time())
        ).scalar() or 0
        
        # Average inventory value
        avg_inventory = self.db.query(
            func.avg(InventoryItem.quantity_on_hand * InventoryItem.unit_cost)
        ).filter(
            InventoryItem.is_active == True
        ).scalar() or 1  # Avoid division by zero
        
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
        granularity: DateGranularity = DateGranularity.DAY
    ) -> OEEResponse:
        """Get detailed OEE breakdown."""
        # Summary for period
        summary = OEEComponents(
            availability=0,
            performance=0,
            quality=0,
            oee=self._get_oee_value(start_date, end_date, work_center_id)
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
            
            oee_val = self._get_oee_value(current, period_end, work_center_id)
            time_series.append(OEEDataPoint(
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
                defect_units=0
            ))
            current = next_period
        
        # By work center
        by_work_center = []
        work_centers = self.db.query(WorkCenter).filter(WorkCenter.is_active == True).all()
        for wc in work_centers:
            oee_val = self._get_oee_value(start_date, end_date, wc.id)
            by_work_center.append(OEEDataPoint(
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
                defect_units=0
            ))
        
        return OEEResponse(
            summary=summary,
            time_series=time_series,
            by_work_center=by_work_center
        )
    
    # ============ PRODUCTION TRENDS ============
    
    def get_production_trends(
        self,
        start_date: date,
        end_date: date,
        group_by: Optional[str] = None,  # work_center, part, customer
        granularity: DateGranularity = DateGranularity.DAY
    ) -> ProductionTrendsResponse:
        """Get production trend data."""
        # Prefer time entries when available; fallback to completed operations for simplified workflows
        time_entry_count = self.db.query(func.count(TimeEntry.id)).filter(
            TimeEntry.clock_in >= datetime.combine(start_date, datetime.min.time()),
            TimeEntry.clock_in <= datetime.combine(end_date, datetime.max.time()),
            TimeEntry.entry_type == TimeEntryType.RUN
        ).scalar() or 0

        if time_entry_count > 0:
            base_query = self.db.query(
                func.date(TimeEntry.clock_in).label('date'),
                func.sum(TimeEntry.quantity_produced).label('units_produced'),
                func.sum(TimeEntry.quantity_scrapped).label('units_scrapped'),
                func.sum(TimeEntry.duration_hours).label('total_hours')
            ).filter(
                TimeEntry.clock_in >= datetime.combine(start_date, datetime.min.time()),
                TimeEntry.clock_in <= datetime.combine(end_date, datetime.max.time()),
                TimeEntry.entry_type == TimeEntryType.RUN
            )

            if group_by == "work_center":
                base_query = base_query.add_columns(
                    TimeEntry.work_center_id.label('group_key')
                ).group_by(func.date(TimeEntry.clock_in), TimeEntry.work_center_id)
            else:
                base_query = base_query.group_by(func.date(TimeEntry.clock_in))

            results = base_query.order_by(func.date(TimeEntry.clock_in)).all()
        else:
            base_query = self.db.query(
                func.date(WorkOrderOperation.actual_end).label('date'),
                func.sum(WorkOrderOperation.quantity_complete).label('units_produced'),
                func.sum(WorkOrderOperation.quantity_scrapped).label('units_scrapped'),
                func.sum(
                    (WorkOrderOperation.actual_setup_hours + WorkOrderOperation.actual_run_hours)
                ).label('total_hours')
            ).filter(
                WorkOrderOperation.actual_end.isnot(None),
                WorkOrderOperation.actual_end >= datetime.combine(start_date, datetime.min.time()),
                WorkOrderOperation.actual_end <= datetime.combine(end_date, datetime.max.time()),
                WorkOrderOperation.status == OperationStatus.COMPLETE
            )

            if group_by == "work_center":
                base_query = base_query.add_columns(
                    WorkOrderOperation.work_center_id.label('group_key')
                ).group_by(func.date(WorkOrderOperation.actual_end), WorkOrderOperation.work_center_id)
            else:
                base_query = base_query.group_by(func.date(WorkOrderOperation.actual_end))

            results = base_query.order_by(func.date(WorkOrderOperation.actual_end)).all()
        
        # Build time series
        time_series = []
        for row in results:
            time_series.append(ProductionDataPoint(
                date=row.date,
                group_key=str(row.group_key) if hasattr(row, 'group_key') else None,
                units_produced=int(row.units_produced or 0),
                units_scrapped=int(row.units_scrapped or 0),
                work_orders_completed=0,  # Would need separate query
                work_orders_started=0,
                total_hours=float(row.total_hours or 0)
            ))
        
        # Calculate totals
        totals = {
            "units_produced": sum(dp.units_produced for dp in time_series),
            "units_scrapped": sum(dp.units_scrapped for dp in time_series),
            "total_hours": sum(dp.total_hours for dp in time_series)
        }
        
        return ProductionTrendsResponse(
            time_series=time_series,
            totals=totals,
            by_group=None
        )
    
    # ============ COST ANALYSIS ============
    
    def get_cost_analysis(
        self,
        start_date: date,
        end_date: date,
        work_order_id: Optional[int] = None
    ) -> CostAnalysisResponse:
        """Get cost analysis for jobs."""
        query = self.db.query(WorkOrder).filter(
            WorkOrder.status.in_([WorkOrderStatus.COMPLETE, WorkOrderStatus.CLOSED])
        )
        
        if work_order_id:
            query = query.filter(WorkOrder.id == work_order_id)
        else:
            query = query.filter(
                WorkOrder.actual_end >= datetime.combine(start_date, datetime.min.time()),
                WorkOrder.actual_end <= datetime.combine(end_date, datetime.max.time())
            )
        
        work_orders = query.all()
        
        jobs = []
        total_estimated = 0
        total_actual = 0
        
        for wo in work_orders:
            variance = wo.actual_cost - wo.estimated_cost
            variance_pct = (variance / wo.estimated_cost * 100) if wo.estimated_cost > 0 else 0
            
            jobs.append(JobCostAnalysis(
                work_order_id=wo.id,
                work_order_number=wo.work_order_number,
                part_number=wo.part.part_number if wo.part else None,
                customer_name=wo.customer_name,
                estimated_cost=wo.estimated_cost,
                actual_cost=wo.actual_cost,
                variance=variance,
                variance_pct=variance_pct,
                cost_breakdown=CostBreakdown(
                    material_cost=0,  # Would need detailed tracking
                    labor_cost=wo.actual_hours * 50,  # Estimated hourly rate
                    overhead_cost=0,
                    outside_services=0,
                    total_cost=wo.actual_cost
                )
            ))
            
            total_estimated += wo.estimated_cost
            total_actual += wo.actual_cost
        
        avg_variance = ((total_actual - total_estimated) / total_estimated * 100) if total_estimated > 0 else 0
        
        return CostAnalysisResponse(
            jobs=jobs,
            summary={
                "total_estimated": total_estimated,
                "total_actual": total_actual,
                "total_variance": total_actual - total_estimated
            },
            avg_margin=0,  # Would need pricing data
            avg_variance_pct=avg_variance,
            time_series=[]
        )
    
    # ============ QUALITY METRICS ============
    
    def get_quality_metrics(
        self,
        start_date: date,
        end_date: date,
        metric_type: str = "all"
    ) -> QualityMetricsResponse:
        """Get quality metrics and charts data."""
        # Defect Pareto
        defect_counts = self.db.query(
            NonConformanceReport.disposition,
            func.count(NonConformanceReport.id).label('count')
        ).filter(
            NonConformanceReport.created_at >= datetime.combine(start_date, datetime.min.time()),
            NonConformanceReport.created_at <= datetime.combine(end_date, datetime.max.time())
        ).group_by(NonConformanceReport.disposition).order_by(func.count(NonConformanceReport.id).desc()).all()
        
        total_defects = sum(d.count for d in defect_counts)
        defect_pareto = []
        cumulative = 0
        for d in defect_counts:
            pct = (d.count / total_defects * 100) if total_defects > 0 else 0
            cumulative += pct
            defect_pareto.append(DefectPareto(
                defect_type=d.disposition.value if d.disposition else "unknown",
                count=d.count,
                percentage=round(pct, 1),
                cumulative_pct=round(cumulative, 1)
            ))
        
        # Vendor quality
        vendor_stats = self.db.query(
            Vendor.id,
            Vendor.name,
            func.count(POReceipt.id).label('receipts'),
            func.sum(case((POReceipt.inspection_status == InspectionStatus.PASSED, 1), else_=0)).label('accepted'),
            func.sum(case((POReceipt.inspection_status == InspectionStatus.FAILED, 1), else_=0)).label('rejected')
        ).join(
            POReceipt, POReceipt.po_line.has(purchase_order=Vendor.purchase_orders)
        ).filter(
            POReceipt.received_at >= datetime.combine(start_date, datetime.min.time()),
            POReceipt.received_at <= datetime.combine(end_date, datetime.max.time())
        ).group_by(Vendor.id, Vendor.name).all()
        
        by_vendor = []
        for v in vendor_stats:
            acceptance_rate = (v.accepted / v.receipts * 100) if v.receipts > 0 else 0
            by_vendor.append(VendorQuality(
                vendor_id=v.id,
                vendor_name=v.name,
                receipts_count=v.receipts,
                accepted_count=v.accepted or 0,
                rejected_count=v.rejected or 0,
                acceptance_rate=round(acceptance_rate, 1),
                ncr_count=0  # Would need to join NCRs
            ))
        
        # Summary
        fpy = self._get_fpy_value(start_date, end_date)
        scrap = self._get_scrap_value(start_date, end_date)
        
        return QualityMetricsResponse(
            summary={
                "first_pass_yield": fpy,
                "scrap_rate": scrap,
                "total_ncrs": total_defects,
                "defect_rate": 100 - fpy
            },
            defect_pareto=defect_pareto,
            time_series=[],
            by_vendor=by_vendor,
            control_limits={
                "ucl": 5.0,  # Upper control limit
                "lcl": 0.0,  # Lower control limit
                "center": 2.0  # Center line
            }
        )
    
    # ============ INVENTORY ANALYTICS ============
    
    def get_inventory_analytics(
        self,
        start_date: date,
        end_date: date,
        category: Optional[str] = None
    ) -> InventoryAnalyticsResponse:
        """
        Get inventory turnover and analytics.
        
        OPTIMIZATION: Uses bulk queries with GROUP BY instead of per-part queries.
        Before: 2 queries per part × 50 parts = 100+ database round trips
        After:  3 queries total (COGS aggregation + inventory aggregation + parts)
        
        Query reduction: ~97% fewer database calls
        """
        # OPTIMIZATION: Bulk query for COGS by part (single query instead of N)
        cogs_by_part = self.db.query(
            InventoryTransaction.part_id,
            func.sum(func.abs(InventoryTransaction.total_cost)).label('cogs')
        ).filter(
            InventoryTransaction.transaction_type == TransactionType.ISSUE,
            InventoryTransaction.created_at >= datetime.combine(start_date, datetime.min.time()),
            InventoryTransaction.created_at <= datetime.combine(end_date, datetime.max.time())
        ).group_by(
            InventoryTransaction.part_id
        ).all()
        
        # Build lookup dict for O(1) access
        cogs_map = {row.part_id: float(row.cogs or 0) for row in cogs_by_part}
        
        # OPTIMIZATION: Bulk query for average inventory value by part
        avg_inv_by_part = self.db.query(
            InventoryItem.part_id,
            func.avg(InventoryItem.quantity_on_hand * InventoryItem.unit_cost).label('avg_inv')
        ).filter(
            InventoryItem.is_active == True
        ).group_by(
            InventoryItem.part_id
        ).all()
        
        # Build lookup dict for O(1) access
        avg_inv_map = {row.part_id: float(row.avg_inv or 1) for row in avg_inv_by_part}
        
        # Get parts (single query)
        parts = self.db.query(Part).filter(Part.is_active == True).limit(50).all()
        
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
            
            turnover_data.append(InventoryTurnover(
                part_id=part.id,
                part_number=part.part_number,
                avg_inventory_value=avg_inv,
                cogs=annualized_cogs,
                turnover_ratio=round(turnover, 2),
                days_on_hand=round(365 / turnover, 1) if turnover > 0 else 999
            ))
        
        # Sort by turnover (low first = problem items)
        turnover_data.sort(key=lambda x: x.turnover_ratio)
        
        # Calculate total inventory value (single query)
        total_inventory_value = self.db.query(
            func.sum(InventoryItem.quantity_on_hand * InventoryItem.unit_cost)
        ).filter(InventoryItem.is_active == True).scalar() or 0
        
        return InventoryAnalyticsResponse(
            turnover_by_category=[],
            low_turnover_items=turnover_data[:10],
            stock_trends=[],
            summary={
                "total_inventory_value": total_inventory_value,
                "avg_turnover": sum(t.turnover_ratio for t in turnover_data) / len(turnover_data) if turnover_data else 0
            }
        )
