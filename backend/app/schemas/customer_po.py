"""Read-only production tracking for the customer PO references on work orders."""

from datetime import date
from typing import Optional

from pydantic import BaseModel

from app.models.work_order import OperationStatus
from app.schemas.work_order import WorkOrderSummary


class CustomerPOStats(BaseModel):
    total_work_orders: int
    active_work_orders: int
    completed_work_orders: int
    cancelled_work_orders: int
    in_progress_work_orders: int
    on_hold_work_orders: int
    overdue_work_orders: int
    due_today_work_orders: int
    completion_percent: float
    next_due_date: Optional[date]


class CustomerPOSummary(BaseModel):
    customer_name: Optional[str]
    customer_po: str
    stats: CustomerPOStats


class CustomerPOSuggestion(BaseModel):
    customer_name: Optional[str]
    customer_po: str


class CustomerPOSuggestionsResponse(BaseModel):
    items: list[CustomerPOSuggestion]
    has_more: bool


class CustomerPOListResponse(BaseModel):
    items: list[CustomerPOSummary]
    total: int
    skip: int
    limit: int
    has_next: bool
    as_of_date: date


class CustomerPOCurrentOperation(BaseModel):
    id: int
    operation_number: Optional[str]
    name: str
    status: OperationStatus
    work_center_name: Optional[str]


class CustomerPOWorkOrder(WorkOrderSummary):
    po_date: Optional[date]
    must_ship_by: Optional[date]
    quantity_scrapped: float
    is_overdue: bool
    current_operations: list[CustomerPOCurrentOperation]


class CustomerPODashboardResponse(BaseModel):
    customer_name: Optional[str]
    customer_po: str
    stats: CustomerPOStats
    items: list[CustomerPOWorkOrder]
    total: int
    skip: int
    limit: int
    has_next: bool
    as_of_date: date
