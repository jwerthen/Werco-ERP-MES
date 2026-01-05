"""
Schemas for Analytics & Business Intelligence Module
"""
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import date, datetime
from enum import Enum


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
    value: float
    target: Optional[float] = None
    prior_value: Optional[float] = None
    change_pct: Optional[float] = None
    trend: TrendDirection = TrendDirection.FLAT
    sparkline: List[float] = []
    
    class Config:
        from_attributes = True


class KPIDashboard(BaseModel):
    oee: KPIValue
    on_time_delivery: KPIValue
    first_pass_yield: KPIValue
    scrap_rate: KPIValue
    open_ncrs: KPIValue
    quote_win_rate: KPIValue
    backlog_hours: KPIValue
    inventory_turnover: KPIValue
    period_start: date
    period_end: date
    generated_at: datetime


# ============ OEE SCHEMAS ============

class OEEComponents(BaseModel):
    availability: float
    performance: float
    quality: float
    oee: float


class OEEDataPoint(BaseModel):
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

class ProductionDataPoint(BaseModel):
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


class QualityDataPoint(BaseModel):
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


class StockLevel(BaseModel):
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
    filters: List[ReportFilter] = []
    group_by: List[ReportGroupBy] = []
    sort: List[ReportSort] = []
    limit: Optional[int] = 1000


class ReportTemplateCreate(BaseModel):
    name: str
    description: Optional[str] = None
    data_source: ReportDataSource
    columns: List[ReportColumn]
    filters: List[ReportFilter] = []
    group_by: List[ReportGroupBy] = []
    sort: List[ReportSort] = []
    is_shared: bool = False


class ReportTemplateResponse(BaseModel):
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


class CapacityForecast(BaseModel):
    week_start: date
    week_end: date
    work_centers: List[WorkCenterForecast]
    total_committed: float
    total_available: float
    overall_utilization: float


class CapacityForecastResponse(BaseModel):
    weeks: List[CapacityForecast]
    alerts: List[Dict[str, Any]]


class OperationPrediction(BaseModel):
    operation_id: int
    operation_name: str
    work_center_name: str
    predicted_start: datetime
    predicted_end: datetime
    queue_position: int
    estimated_hours: float


class DeliveryPrediction(BaseModel):
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


class StockoutPrediction(BaseModel):
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
