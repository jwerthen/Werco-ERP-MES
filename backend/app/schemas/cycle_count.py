"""Explicit public count workspace contracts; timestamps are served in UTC."""

from datetime import date, datetime
from typing import List, Optional

from app.schemas.base import UTCModel


class CycleCountSummary(UTCModel):
    id: int
    count_number: str
    status: str
    scheduled_date: date
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    warehouse: Optional[str]
    location_code: Optional[str]
    part_id: Optional[int]
    assigned_to: Optional[int]
    assigned_to_name: Optional[str]
    total_items: int
    items_counted: int
    items_adjusted: int
    total_variance_value: float
    notes: Optional[str]


class CycleCountLine(UTCModel):
    id: int
    inventory_item_id: int
    part_id: Optional[int]
    part_number: str
    part_name: str
    unit_of_measure: str
    location: Optional[str]
    lot_number: Optional[str]
    serial_number: Optional[str]
    system_quantity: float
    current_quantity: Optional[float]
    counted_quantity: Optional[float]
    variance: Optional[float]
    variance_value: Optional[float]
    posting_delta: float
    stock_changed: bool
    is_counted: bool
    requires_recount: bool
    counted_at: Optional[datetime]
    notes: Optional[str]


class CycleCountDetail(CycleCountSummary):
    items: List[CycleCountLine]


class CycleCountReview(CycleCountDetail):
    review_token: str


class CycleCountWorkspace(UTCModel):
    items: List[CycleCountSummary]
    total: int
    has_more: bool


class CycleCounter(UTCModel):
    id: int
    name: str
