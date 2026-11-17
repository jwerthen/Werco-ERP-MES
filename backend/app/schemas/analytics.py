"""
Schemas for Analytics & Business Intelligence Module
"""

from datetime import date, datetime
from enum import Enum
from typing import Annotated, Any, Dict, List, Literal, Optional

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, WithJsonSchema, model_validator
from pydantic_core import PydanticCustomError

from app.core.unicode_text import is_utf8_encodable
from app.schemas.base import UTCModel
from app.schemas.material_readiness import MaterialReadiness


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
#
# Two tiers of models, on purpose:
#
# * The SHARED shapes (``ReportDataSource``, ``ReportColumn``, ``ReportFilter``,
#   ``ReportGroupBy``, ``ReportSort``) carry type checks only and NO validators.
#   ``ReportTemplateResponse`` reuses them to render STORED templates, and stored rows
#   were saved with type checks only (through the API and the MCP tool
#   ``create_report_template``). A rule added to one of them would make
#   ``GET /analytics/custom-report/templates`` answer 500 for the whole company the
#   moment one old template broke it.
# * The REQUEST shapes (``ReportDefinitionInput`` and the ``*Input`` subclasses below,
#   ``CustomReportRequest``, ``ReportTemplateCreate``, ``ReportTemplateUpdate``) forbid
#   unknown keys and run every semantic rule through ONE function,
#   ``app.services.report_builder.definition_problems``. A stored template is judged by
#   building the same request model from its JSON (``report_template_service``), so the
#   list's ``definition_errors`` and the 409 a run answers can never disagree.


class ReportDataSource(str, Enum):
    WORK_ORDERS = "work_orders"
    PARTS = "parts"
    INVENTORY = "inventory"
    QUALITY = "quality"
    # Retired: it never had a backing model (every report on it answered 500). The member
    # stays so a stored template that names it still renders; every request schema
    # refuses it with 422 and the /data-sources catalog leaves it out.
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


# Request caps. A definition is stored verbatim in ``report_templates`` and copied into
# immutable audit rows, so every list is bounded.
REPORT_MAX_COLUMNS = 40
REPORT_MAX_FILTERS = 20
REPORT_MAX_GROUP_BY = 10
REPORT_MAX_SORT = 10
REPORT_DEFAULT_LIMIT = 1000
REPORT_MAX_LIMIT = 10_000

_QUERYABLE_DATA_SOURCES = [s.value for s in ReportDataSource if s is not ReportDataSource.PRODUCTION]

# Request-side data source: validation still accepts every ``ReportDataSource`` member so
# ``production`` gets a precise 422 message from ``definition_problems`` rather than a
# generic enum error, but the published schema (OpenAPI, and the MCP tool schemas built
# from it) advertises only the sources that can run.
QueryableReportDataSource = Annotated[
    ReportDataSource,
    WithJsonSchema(
        {
            "type": "string",
            "enum": _QUERYABLE_DATA_SOURCES,
            "title": "Data Source",
            "description": "Which records the report reads. GET /analytics/data-sources lists each one's fields.",
        }
    ),
]


# ``ValidationError`` type carrying ``definition_problems`` output; its ``ctx["problems"]``
# is the list ``report_template_service`` reads back for a stored template.
REPORT_DEFINITION_ERROR_TYPE = "report_definition"


# Unknown keys are refused. ``from_attributes`` lets a Python caller pass the shared shape
# (e.g. a ``ReportTemplateResponse``'s columns) where a request item is expected.
_INPUT_ITEM_CONFIG = ConfigDict(extra="forbid", from_attributes=True)


class ReportColumnInput(ReportColumn):
    """A report column in a request. ``field`` is a field name from GET /analytics/data-sources;
    ``alias`` renames the output key; ``aggregate`` (sum/avg on number fields, min/max on
    number/date/datetime/string fields, count on any) makes it an aggregated column."""

    model_config = _INPUT_ITEM_CONFIG


class ReportFilterInput(ReportFilter):
    """A report filter in a request. Operators by field type: string eq/ne/in/like; number
    eq/ne/gt/gte/lt/lte/in/between; date and datetime eq/ne/gt/gte/lt/lte/between; enum
    eq/ne/in. ``value`` null is allowed only with eq/ne (IS NULL / IS NOT NULL);
    ``between`` needs ``value2``; ``in`` takes a non-empty list."""

    model_config = _INPUT_ITEM_CONFIG


class ReportGroupByInput(ReportGroupBy):
    """A group-by field in a request. ``granularity`` is not supported yet and must be null."""

    model_config = _INPUT_ITEM_CONFIG


class ReportSortInput(ReportSort):
    """A sort in a request. ``field`` is a catalog field or the output key (alias, else field)
    of a selected column; ``direction`` is asc or desc."""

    model_config = _INPUT_ITEM_CONFIG


class ReportDefinitionInput(BaseModel):
    """What a report reads and how it shapes it. Every rule lives in
    ``app.services.report_builder.definition_problems``; a broken definition is one 422 whose
    message lists every problem."""

    model_config = ConfigDict(extra="forbid")

    data_source: QueryableReportDataSource
    columns: List[ReportColumnInput] = Field(..., min_length=1, max_length=REPORT_MAX_COLUMNS)
    filters: List[ReportFilterInput] = Field(default_factory=list, max_length=REPORT_MAX_FILTERS)
    group_by: List[ReportGroupByInput] = Field(default_factory=list, max_length=REPORT_MAX_GROUP_BY)
    sort: List[ReportSortInput] = Field(default_factory=list, max_length=REPORT_MAX_SORT)

    @model_validator(mode="after")
    def _check_definition(self) -> "ReportDefinitionInput":
        # Lazy import: the services module imports this one for its enums.
        from app.services.report_builder import definition_problems

        problems = definition_problems(self.data_source, self.columns, self.filters, self.group_by, self.sort)
        if problems:
            raise PydanticCustomError(
                REPORT_DEFINITION_ERROR_TYPE,
                "{detail}",
                {"detail": "; ".join(problems), "problems": problems},
            )
        for sort in self.sort:
            sort.direction = sort.direction.lower()
        return self


class CustomReportRequest(ReportDefinitionInput):
    """Run an ad-hoc report. ``limit`` caps the rows returned (1-10,000, default 1000);
    the result's ``truncated`` says whether more rows matched."""

    limit: int = Field(REPORT_DEFAULT_LIMIT, ge=1, le=REPORT_MAX_LIMIT)


def _storable_template_text(value: str) -> str:
    """Refuse a template name / description no database column can hold.

    A NUL fails at bind on Postgres -- a 500 for a caller's input error. An unpaired UTF-16
    surrogate (JSON ``"\\ud800"``) cannot be encoded at all; pydantic-core's constrained-str
    check already refuses one (``string_unicode``), so that branch is the backstop should it
    ever stop. The message never echoes the value.
    """
    if "\x00" in value:
        raise PydanticCustomError("unstorable_text", "Text cannot contain a NUL (0x00) character")
    if not is_utf8_encodable(value):
        raise PydanticCustomError(
            "unstorable_text", "Text cannot contain characters that cannot be stored (an unpaired UTF-16 surrogate)"
        )
    return value


TemplateName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=255),
    AfterValidator(_storable_template_text),
]
TemplateDescription = Annotated[
    str, StringConstraints(strip_whitespace=True, max_length=2000), AfterValidator(_storable_template_text)
]


class ReportTemplateCreate(ReportDefinitionInput):
    """Save a report definition as a named template (private unless ``is_shared``)."""

    name: TemplateName
    description: Optional[TemplateDescription] = None
    is_shared: bool = False


class ReportTemplateUpdate(BaseModel):
    """Change a saved template. Send only what changes; ``definition`` replaces the whole
    definition and is validated like a new one. ``description`` may be null (clears it);
    ``name``, ``is_shared`` and ``definition`` may not."""

    model_config = ConfigDict(extra="forbid")

    name: Optional[TemplateName] = None
    description: Optional[TemplateDescription] = None
    is_shared: Optional[bool] = None
    definition: Optional[ReportDefinitionInput] = None

    @model_validator(mode="after")
    def _check_fields(self) -> "ReportTemplateUpdate":
        if not self.model_fields_set:
            raise PydanticCustomError("empty_update", "Send at least one of name, description, is_shared or definition")
        for key in ("name", "is_shared", "definition"):
            if key in self.model_fields_set and getattr(self, key) is None:
                raise PydanticCustomError("null_not_allowed", "{field} cannot be null", {"field": key})
        return self


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
    updated_at: Optional[datetime] = None
    # Why this stored definition can no longer run (empty when it can). A template saved
    # before a rule existed still lists and opens; running or exporting it answers 409
    # until it is edited and saved again.
    definition_errors: List[str] = Field(default_factory=list)

    class Config:
        from_attributes = True


class CustomReportColumnInfo(UTCModel):
    key: str
    label: str
    type: str


class CustomReportNote(UTCModel):
    code: str
    message: str
    fields: List[str] = Field(default_factory=list)


class CustomReportResult(UTCModel):
    columns: List[CustomReportColumnInfo]
    rows: List[Dict[str, Any]]
    row_count: int
    truncated: bool
    notes: List[CustomReportNote] = Field(default_factory=list)


class ReportFieldInfo(UTCModel):
    name: str
    label: str
    type: Literal["string", "number", "date", "datetime", "enum"]
    options: Optional[List[str]] = None


class ReportDataSourceInfo(UTCModel):
    label: str
    fields: List[ReportFieldInfo]


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
    predicted_start: Optional[datetime]
    predicted_end: Optional[datetime]
    queue_position: int
    estimated_hours: float


class DeliveryPrediction(UTCModel):
    work_order_id: int
    work_order_number: str
    part_number: str
    quantity: float
    due_date: Optional[date]
    predicted_completion: Optional[datetime]
    confidence: float
    on_time_probability: Optional[float]
    materials: Optional[MaterialReadiness] = None
    warnings: List[str] = Field(default_factory=list)
    basis: str = "Calendar-day estimate using historical queue time; not a committed promise."
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
