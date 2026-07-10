"""
Schemas for Analytics & Business Intelligence Module
"""

from datetime import date, datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.schemas.base import UTCModel


class DateGranularity(str, Enum):
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    QUARTER = "quarter"
    YEAR = "year"


class TrendDirection(str, Enum):
    UP = "up"
    DOWN = "down"
    FLAT = "flat"


# ============ KPI SCHEMAS ============


class KPIValue(BaseModel):
    # ``value`` is Optional so a genuinely-uncomputable KPI (no staffed time for OEE,
    # empty denominator for OTD — Batch 8 / OEE-4/OEE-6) returns null ("n/a") instead
    # of a misleading 0/100. Frontend must null-guard before formatting.
    value: Optional[float] = None
    target: Optional[float] = None
    prior_value: Optional[float] = None
    change_pct: Optional[float] = None
    trend: TrendDirection = TrendDirection.FLAT
    sparkline: List[float] = Field(default_factory=list)

    class Config:
        from_attributes = True


class KPIDashboard(UTCModel):
    oee: KPIValue
    on_time_delivery: KPIValue
    first_pass_yield: KPIValue
    scrap_rate: KPIValue
    open_ncrs: KPIValue
    quote_win_rate: KPIValue
    backlog_hours: KPIValue
    inventory_turnover: KPIValue
    # Lean Phase 1 (issue #88): SHIP-based delivery KPIs alongside the completion-based
    # on_time_delivery above. ``on_time_delivery_ship`` anchors on fulfillment (WOs whose
    # FULL quantity finished shipping in the window, on/before promise = must_ship_by ||
    # due_date); ``otif`` anchors on the promise date (WOs promised in the window that had
    # shipped in full BY that date). Optional+defaulted so pre-existing consumers/fixtures
    # of this payload keep validating; the live endpoint always populates both.
    on_time_delivery_ship: Optional[KPIValue] = None
    otif: Optional[KPIValue] = None
    period_start: date
    period_end: date
    generated_at: datetime


# ============ OEE SCHEMAS ============


class OEEComponents(BaseModel):
    availability: float
    performance: float
    quality: float
    oee: float


class OEEDataPoint(UTCModel):
    date: date
    work_center_id: Optional[int] = None
    work_center_name: Optional[str] = None
    availability: float
    performance: float
    quality: float
    oee: float
    planned_time: float
    operating_time: float
    downtime: float
    ideal_cycle_time: float
    actual_cycle_time: float
    total_units: int
    good_units: int
    defect_units: int


class OEEResponse(BaseModel):
    summary: OEEComponents
    time_series: List[OEEDataPoint]
    by_work_center: List[OEEDataPoint]


# ============ PRODUCTION TRENDS ============


class ProductionDataPoint(UTCModel):
    date: date
    group_key: Optional[str] = None
    group_name: Optional[str] = None
    units_produced: int
    units_scrapped: int
    work_orders_completed: int
    work_orders_started: int
    total_hours: float


class ProductionTrendsResponse(BaseModel):
    time_series: List[ProductionDataPoint]
    totals: Dict[str, float]
    by_group: Optional[Dict[str, List[ProductionDataPoint]]] = None


# ============ COST ANALYSIS ============


class CostBreakdown(BaseModel):
    material_cost: float
    labor_cost: float
    overhead_cost: float
    outside_services: float
    total_cost: float


class JobCostAnalysis(BaseModel):
    work_order_id: int
    work_order_number: str
    part_number: Optional[str] = None
    customer_name: Optional[str] = None
    estimated_cost: float
    actual_cost: float
    variance: float
    variance_pct: float
    cost_breakdown: CostBreakdown
    margin: Optional[float] = None
    margin_pct: Optional[float] = None


class CostAnalysisResponse(BaseModel):
    jobs: List[JobCostAnalysis]
    summary: Dict[str, float]
    avg_margin: float
    avg_variance_pct: float
    time_series: List[Dict[str, Any]]


# ============ QUALITY METRICS ============


class DefectPareto(BaseModel):
    defect_type: str
    count: int
    percentage: float
    cumulative_pct: float


class QualityDataPoint(UTCModel):
    date: date
    defect_rate: float
    first_pass_yield: float
    ncr_count: int
    units_inspected: int
    units_passed: int
    units_failed: int


class VendorQuality(BaseModel):
    vendor_id: int
    vendor_name: str
    receipts_count: int
    accepted_count: int
    rejected_count: int
    acceptance_rate: float
    ncr_count: int


class QualityMetricsResponse(BaseModel):
    summary: Dict[str, float]
    defect_pareto: List[DefectPareto]
    time_series: List[QualityDataPoint]
    by_vendor: List[VendorQuality]
    control_limits: Dict[str, float]


# ============ INVENTORY ANALYTICS ============


class InventoryTurnover(BaseModel):
    category: Optional[str] = None
    part_id: Optional[int] = None
    part_number: Optional[str] = None
    avg_inventory_value: float
    cogs: float
    turnover_ratio: float
    days_on_hand: float


class StockLevel(UTCModel):
    date: date
    part_id: int
    part_number: str
    quantity_on_hand: float
    reorder_point: float
    is_below_reorder: bool


class InventoryAnalyticsResponse(BaseModel):
    turnover_by_category: List[InventoryTurnover]
    low_turnover_items: List[InventoryTurnover]
    stock_trends: List[StockLevel]
    summary: Dict[str, float]


# ============ CUSTOM REPORT BUILDER ============


class ReportDataSource(str, Enum):
    WORK_ORDERS = "work_orders"
    PARTS = "parts"
    INVENTORY = "inventory"
    QUALITY = "quality"
    PRODUCTION = "production"
    PURCHASING = "purchasing"
    QUOTES = "quotes"


class AggregateFunction(str, Enum):
    SUM = "sum"
    AVG = "avg"
    COUNT = "count"
    MIN = "min"
    MAX = "max"


class ReportFilter(BaseModel):
    field: str
    operator: str  # eq, ne, gt, gte, lt, lte, in, like, between
    value: Any
    value2: Optional[Any] = None  # For between operator


class ReportColumn(BaseModel):
    field: str
    alias: Optional[str] = None
    aggregate: Optional[AggregateFunction] = None


class ReportGroupBy(BaseModel):
    field: str
    granularity: Optional[DateGranularity] = None  # For date fields


class ReportSort(BaseModel):
    field: str
    direction: str = "asc"


class CustomReportRequest(BaseModel):
    data_source: ReportDataSource
    columns: List[ReportColumn]
    filters: List[ReportFilter] = Field(default_factory=list)
    group_by: List[ReportGroupBy] = Field(default_factory=list)
    sort: List[ReportSort] = Field(default_factory=list)
    limit: Optional[int] = 1000


class ReportTemplateCreate(BaseModel):
    name: str
    description: Optional[str] = None
    data_source: ReportDataSource
    columns: List[ReportColumn]
    filters: List[ReportFilter] = Field(default_factory=list)
    group_by: List[ReportGroupBy] = Field(default_factory=list)
    sort: List[ReportSort] = Field(default_factory=list)
    is_shared: bool = False


class ReportTemplateResponse(UTCModel):
    id: int
    name: str
    description: Optional[str]
    data_source: ReportDataSource
    columns: List[ReportColumn]
    filters: List[ReportFilter]
    group_by: List[ReportGroupBy]
    sort: List[ReportSort]
    is_shared: bool
    created_by: int
    created_at: datetime

    class Config:
        from_attributes = True


# ============ PREDICTIVE ANALYTICS ============


class WorkCenterForecast(BaseModel):
    work_center_id: int
    work_center_name: str
    committed_hours: float
    available_hours: float
    utilization_pct: float
    is_overloaded: bool


class CapacityForecast(UTCModel):
    week_start: date
    week_end: date
    work_centers: List[WorkCenterForecast]
    total_committed: float
    total_available: float
    overall_utilization: float


class CapacityForecastResponse(BaseModel):
    weeks: List[CapacityForecast]
    alerts: List[Dict[str, Any]]


class OperationPrediction(UTCModel):
    operation_id: int
    operation_name: str
    work_center_name: str
    predicted_start: datetime
    predicted_end: datetime
    queue_position: int
    estimated_hours: float


class DeliveryPrediction(UTCModel):
    work_order_id: int
    work_order_number: str
    part_number: str
    quantity: float
    due_date: Optional[date]
    predicted_completion: datetime
    confidence: float
    on_time_probability: float
    operations: List[OperationPrediction]
    bottleneck_work_center: Optional[str] = None


class StockoutPrediction(UTCModel):
    part_id: int
    part_number: str
    part_name: str
    current_stock: float
    daily_usage_rate: float
    predicted_stockout_date: Optional[date]
    days_until_stockout: Optional[int]
    open_po_quantity: float
    next_po_due: Optional[date]
    urgency: str  # critical, warning, ok


class InventoryDemandResponse(BaseModel):
    predictions: List[StockoutPrediction]
    critical_count: int
    warning_count: int


# ============ SHIP-BASED OTD / OTIF (Lean Phase 1, issue #88) ============


class ShipOTDRow(UTCModel):
    """One work order's ship-vs-promise record for the detail report."""

    work_order_id: int
    work_order_number: str
    customer_name: Optional[str] = None
    part_number: Optional[str] = None
    status: str
    quantity_ordered: float
    quantity_shipped: float  # cumulative shipped to date (all non-cancelled shipments)
    promise_source: Optional[str] = None  # 'must_ship_by' | 'due_date' | None
    promise_date: Optional[date] = None
    first_ship_date: Optional[date] = None
    last_ship_date: Optional[date] = None
    # Date the cumulative shipped quantity first reached the ordered quantity.
    full_ship_date: Optional[date] = None
    fully_shipped: bool = False
    # True/False once determinable; None while open with the promise still in the future.
    on_time: Optional[bool] = None
    # full_ship - promise (positive = late). For an open WO past promise: days past
    # promise so far (grows daily until it ships).
    days_late: Optional[int] = None


class ShipOTDCustomerRollup(BaseModel):
    customer_name: str
    work_orders: int
    on_time: int
    late: int
    otd_pct: Optional[float] = None
    avg_days_late: Optional[float] = None


class PromiseHygieneRow(UTCModel):
    """WO shipped/open with NEITHER must_ship_by nor due_date -- unmeasurable."""

    work_order_id: int
    work_order_number: str
    customer_name: Optional[str] = None
    status: str
    quantity_ordered: float
    quantity_shipped: float
    last_ship_date: Optional[date] = None


class ShipOTDReportResponse(UTCModel):
    period_start: date
    period_end: date
    # Headline values match the KPI dashboard legs: fulfillment-anchored OTD and
    # promise-anchored OTIF. None = empty denominator ("n/a"), never a fake 100.
    otd_ship_pct: Optional[float] = None
    otif_pct: Optional[float] = None
    rows: List[ShipOTDRow]
    by_customer: List[ShipOTDCustomerRollup]
    promise_hygiene: List[PromiseHygieneRow]
    generated_at: datetime


# ============ FLOW METRICS (Lean Phase 1, issue #88) ============


class FlowWorkOrderDetail(UTCModel):
    """Measured flow for one work order completed in the window."""

    work_order_id: int
    work_order_number: str
    part_number: Optional[str] = None
    customer_name: Optional[str] = None
    released_at: Optional[datetime] = None
    actual_end: Optional[datetime] = None
    first_ship_date: Optional[date] = None
    last_ship_date: Optional[date] = None
    lead_time_days: Optional[float] = None  # released_at -> actual_end
    release_to_first_ship_days: Optional[float] = None
    release_to_last_ship_days: Optional[float] = None
    # Value-add labor (RUN TimeEntry hours; backfill/import excluded per provenance rule).
    value_add_hours: float = 0.0
    pce_pct: Optional[float] = None  # value_add_hours / (lead_time_days * 24)


class QueueTimeByWorkCenter(BaseModel):
    work_center_id: int
    work_center_code: Optional[str] = None
    work_center_name: Optional[str] = None
    avg_queue_hours: Optional[float] = None
    max_queue_hours: Optional[float] = None
    samples: int = 0
    # How many samples were measured from an operation_ready event (vs the
    # predecessor actual_end -> actual_start fallback).
    from_ready_events: int = 0


class FlowSummary(BaseModel):
    work_orders_completed: int
    avg_lead_time_days: Optional[float] = None
    median_lead_time_days: Optional[float] = None
    avg_release_to_last_ship_days: Optional[float] = None
    avg_queue_hours: Optional[float] = None
    # Little's Law: avg open-WO count / (completions per day) over the window.
    avg_wip: Optional[float] = None
    daily_completion_rate: Optional[float] = None
    littles_law_throughput_days: Optional[float] = None
    avg_pce_pct: Optional[float] = None
    # Provenance rule: labor booked via backfill/import channels, excluded from the
    # value-add baseline above and reported separately here.
    excluded_backfill_import_hours: float = 0.0


class FlowMetricsResponse(UTCModel):
    period_start: date
    period_end: date
    summary: FlowSummary
    work_orders: List[FlowWorkOrderDetail]
    queue_by_work_center: List[QueueTimeByWorkCenter]
    generated_at: datetime


class WIPAgingItem(UTCModel):
    work_order_id: int
    work_order_number: str
    part_number: Optional[str] = None
    customer_name: Optional[str] = None
    status: str
    priority: Optional[int] = None
    quantity_ordered: float
    quantity_complete: float
    released_at: Optional[datetime] = None
    days_since_release: Optional[float] = None
    current_operation_id: Optional[int] = None
    current_operation_number: Optional[str] = None
    current_operation_name: Optional[str] = None
    current_work_center_name: Optional[str] = None
    # Days since the current operation started (its actual_start) or, when it has
    # not started, since it became READY (operation_ready event) -- None if neither.
    days_in_current_operation: Optional[float] = None
    due_date: Optional[date] = None
    days_to_due: Optional[int] = None  # negative = past due


class WIPAgingResponse(UTCModel):
    items: List[WIPAgingItem]
    total_open: int
    generated_at: datetime


# ============ FPY / RTY + SCRAP PARETO (Lean Phase 1, issue #88) ============


class FPYGroup(BaseModel):
    """Quantity-weighted FPY (and RTY where applicable) for one part/work center."""

    key: str  # part_number or work-center code
    name: Optional[str] = None
    operations: int = 0
    units_attempted: float = 0.0  # quantity_complete + quantity_scrapped
    first_pass_units: float = 0.0  # complete - reworked - scrapped (clamped >= 0)
    fpy_pct: Optional[float] = None
    # Per part: mean of its window WOs' RTY (product of per-op FPYs). None for WC rows.
    rty_pct: Optional[float] = None
    work_orders: int = 0


class FPYResponse(UTCModel):
    period_start: date
    period_end: date
    overall_fpy_pct: Optional[float] = None
    overall_rty_pct: Optional[float] = None
    by_part: List[FPYGroup]
    by_work_center: List[FPYGroup]
    generated_at: datetime


class ScrapParetoBucket(BaseModel):
    scrap_reason_code_id: Optional[int] = None  # None = the 'unspecified' bucket
    code: str  # reason code, or 'unspecified'
    name: Optional[str] = None
    category: Optional[str] = None
    quantity: float = 0.0
    cost: float = 0.0  # quantity x part.standard_cost where available
    percentage: float = 0.0  # share of total quantity
    cumulative_pct: float = 0.0


class ScrapParetoResponse(UTCModel):
    period_start: date
    period_end: date
    total_quantity: float
    total_cost: float
    buckets: List[ScrapParetoBucket]
    # Provenance rule: scrap booked on backfill/import-sourced time entries,
    # excluded from the buckets above and reported separately.
    excluded_backfill_import_quantity: float = 0.0
    generated_at: datetime


# ============ ADOPTION + HIDDEN FACTORY (Lean Phase 1, issue #88) ============


class AdoptionWeek(UTCModel):
    week_start: date
    operation_completions: int = 0
    live_completions: int = 0  # event payload source in kiosk/desktop/scanner
    backfill_completions: int = 0  # source in backfill/import
    unknown_completions: int = 0  # no source reported
    digital_completion_pct: Optional[float] = None  # live / all completions
    clock_in_coverage_pct: Optional[float] = None  # completed ops with >=1 live labor entry
    time_entries: int = 0
    backfill_entries: int = 0
    backfill_rate_pct: Optional[float] = None  # backfill+import entries / all entries


class MaintenanceMixMetrics(BaseModel):
    planned_count: int = 0  # preventive + predictive
    reactive_count: int = 0  # corrective + emergency
    planned_pct: Optional[float] = None


class WorkCenterReliability(BaseModel):
    work_center_id: int
    work_center_code: Optional[str] = None
    work_center_name: Optional[str] = None
    unplanned_downtime_events: int = 0
    unplanned_downtime_hours: float = 0.0
    staffed_run_hours: float = 0.0  # clocked RUN+SETUP hours (provenance-filtered)
    mtbf_hours: Optional[float] = None  # staffed run hours / unplanned event count
    mttr_hours: Optional[float] = None  # mean unplanned event duration (full span)


class HiddenFactoryMetrics(BaseModel):
    rework_hours: float = 0.0
    total_labor_hours: float = 0.0
    rework_hours_pct: Optional[float] = None
    rework_quantity: float = 0.0
    total_quantity: float = 0.0
    rework_quantity_pct: Optional[float] = None
    maintenance: MaintenanceMixMetrics
    reliability_by_work_center: List[WorkCenterReliability]
    # Provenance rule: labor hours booked via backfill/import, excluded above.
    excluded_backfill_import_hours: float = 0.0


class AdoptionMetricsResponse(UTCModel):
    period_start: date
    period_end: date
    digital_completion_pct: Optional[float] = None
    clock_in_coverage_pct: Optional[float] = None
    backfill_rate_pct: Optional[float] = None
    live_completions: int = 0
    backfill_completions: int = 0
    unknown_completions: int = 0
    weekly: List[AdoptionWeek]
    hidden_factory: HiddenFactoryMetrics
    generated_at: datetime
