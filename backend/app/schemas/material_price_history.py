"""Purchase-order cost observations, grouped by item and purchase order."""

from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, Field

PriceHistoryTrend = Literal["all", "up", "down", "unchanged", "new"]
PriceHistorySort = Literal["recent", "increase", "decrease", "name"]


class PriceHistoryPoint(BaseModel):
    purchase_order_id: int
    order_date: date
    unit_price: float


class PriceHistorySummary(BaseModel):
    part_id: int
    part_number: str
    part_name: str
    part_type: str
    unit_of_measure: Optional[str] = None
    # The existing PO schema does not store currency. Do not invent an ISO code.
    currency: None = None
    latest_unit_price: float
    previous_unit_price: Optional[float] = None
    price_change: Optional[float] = None
    price_change_percent: Optional[float] = None
    last_order_date: date
    latest_po_id: int
    latest_po_number: str
    latest_vendor_id: int
    latest_vendor_name: str
    order_count: int
    total_quantity: float
    total_spend: float
    sparkline: list[PriceHistoryPoint] = Field(default_factory=list)


class PriceHistoryCounts(BaseModel):
    tracked_parts: int
    price_increases: int
    price_decreases: int
    unchanged_parts: int
    new_parts: int


class PriceHistoryListResponse(BaseModel):
    items: list[PriceHistorySummary]
    total: int
    page: int
    page_size: int
    # Counts apply search/type, before the trend filter and pagination.
    summary: PriceHistoryCounts


class PriceHistoryObservation(BaseModel):
    purchase_order_id: int
    po_number: str
    order_date: date
    status: str
    vendor_id: int
    vendor_name: str
    quantity_ordered: float
    unit_price: float
    extended_price: float
    line_count: int
    previous_unit_price: Optional[float] = None
    price_change: Optional[float] = None
    price_change_percent: Optional[float] = None
    unit_of_measure: Optional[str] = None
    currency: None = None


class PriceHistoryChartPoint(PriceHistoryPoint):
    vendor_name: str
    quantity_ordered: float
    po_number: str


class PriceHistoryStats(BaseModel):
    latest_unit_price: Optional[float] = None
    previous_unit_price: Optional[float] = None
    price_change: Optional[float] = None
    price_change_percent: Optional[float] = None
    lowest_unit_price: Optional[float] = None
    highest_unit_price: Optional[float] = None
    weighted_average_unit_price: Optional[float] = None
    total_quantity: float = 0
    total_spend: float = 0
    order_count: int = 0


class PriceHistoryVendor(BaseModel):
    id: int
    name: str


class PriceHistoryDetailResponse(BaseModel):
    # All-time context remains available when a filter has no matching orders.
    part: PriceHistorySummary
    history: list[PriceHistoryObservation]
    total: int
    page: int
    page_size: int
    stats: PriceHistoryStats
    chart: list[PriceHistoryChartPoint]
    chart_truncated: bool
    vendor_options: list[PriceHistoryVendor]
    notes: list[str]
