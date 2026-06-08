from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel, Field, field_serializer, model_validator

from app.core.time_utils import to_central_iso
from app.core.validation import (
    DescriptionLong,
    Money,
    MoneySmall,
)
from app.models.work_order import OperationStatus, WorkOrderStatus


def _serialize_decimal_as_number(value: Optional[Decimal]) -> Optional[float]:
    if value is None:
        return None
    return float(value)


class QualityExceptionInfo(BaseModel):
    """One unsatisfied quality gate / data-quality signal on a completion response.

    WARN-AND-RECORD posture: the presence of these in a completion response means the
    operation / work order completed while a quality gate was unsatisfied. Completion
    still SUCCEEDED; the warning is here so the client can show it and the bypass is
    also recorded in the tamper-evident audit trail. Backward-compatible: every
    completion response defaults this to an empty list, so an all-clear completion is
    indistinguishable from the pre-Batch-4 shape.

    ``code`` values: ``inspection_incomplete``, ``open_ncr``, ``fai_not_passed``,
    ``open_blocker`` (Batch 4 / rank 7 quality gates), and ``no_labor_recorded``
    (Batch 7 / rank 10 data-quality signal: an operation completed with zero recorded
    labor, so cost/hour actuals may be understated -- fires regardless of the
    ``LABOR_COST_ROLLUP_ENABLED`` flag).
    """

    code: str
    message: str
    reference_type: str
    reference_id: Optional[int] = None
    severity: Optional[str] = None


class WorkOrderOperationBase(BaseModel):
    work_center_id: int = Field(..., gt=0, description="Work center ID")
    sequence: int = Field(
        ...,
        ge=10,
        le=990,
        multiple_of=10,
        description="Sequence (10-990, multiples of 10)",
    )
    operation_number: Optional[str] = Field(None, max_length=50)
    name: str = Field(..., min_length=2, max_length=255, description="Operation name")
    description: Optional[DescriptionLong] = None
    setup_instructions: Optional[str] = Field(None, max_length=5000)
    run_instructions: Optional[str] = Field(None, max_length=5000)
    setup_time_hours: MoneySmall = Field(default=Decimal("0.0"), ge=Decimal("0"))
    run_time_hours: Money = Field(default=Decimal("0.0"), ge=Decimal("0"))
    run_time_per_piece: MoneySmall = Field(default=Decimal("0.0"), ge=Decimal("0"))
    requires_inspection: bool = False
    inspection_type: Optional[str] = Field(None, max_length=100)
    component_part_id: Optional[int] = Field(None, gt=0)
    component_quantity: Optional[float] = Field(None, ge=0)
    operation_group: Optional[str] = Field(None, max_length=50)


class LaserNestOperationInfo(BaseModel):
    id: int
    nest_name: str
    cnc_file_name: str
    cnc_file_path: Optional[str] = None
    planned_runs: int
    completed_runs: float
    remaining_runs: float = 0.0
    material: Optional[str] = None
    thickness: Optional[str] = None
    sheet_size: Optional[str] = None

    class Config:
        from_attributes = True


class WorkOrderOperationCreate(WorkOrderOperationBase):
    pass


class WorkOrderOperationUpdate(BaseModel):
    version: int = Field(..., ge=0, description="Version for optimistic locking")
    name: Optional[str] = Field(None, min_length=2, max_length=255)
    description: Optional[DescriptionLong] = None
    setup_instructions: Optional[str] = Field(None, max_length=5000)
    run_instructions: Optional[str] = Field(None, max_length=5000)
    setup_time_hours: Optional[Decimal] = Field(None, ge=Decimal("0"))
    run_time_hours: Optional[Decimal] = Field(None, ge=Decimal("0"))
    run_time_per_piece: Optional[Decimal] = Field(None, ge=Decimal("0"))
    status: Optional[OperationStatus] = None
    quantity_complete: Optional[Decimal] = Field(None, ge=Decimal("0"))
    quantity_scrapped: Optional[Decimal] = Field(None, ge=Decimal("0"))
    requires_inspection: Optional[bool] = None
    inspection_complete: Optional[bool] = None


class WorkOrderOperationResponse(WorkOrderOperationBase):
    id: int
    version: Optional[int] = 0
    work_order_id: int
    description: Optional[str] = None  # Override to allow empty strings
    status: OperationStatus
    quantity_complete: MoneySmall
    quantity_scrapped: MoneySmall
    actual_setup_hours: MoneySmall
    actual_run_hours: Money
    estimated_hours: Optional[float] = None
    actual_hours: Optional[float] = None
    work_center_name: Optional[str] = None
    scheduled_start: Optional[datetime]
    scheduled_end: Optional[datetime]
    actual_start: Optional[datetime]
    actual_end: Optional[datetime]
    inspection_complete: bool
    created_at: datetime
    updated_at: datetime

    # Component tracking for assembly WOs
    component_part_id: Optional[int] = None
    component_part_number: Optional[str] = None
    component_part_name: Optional[str] = None
    component_quantity: Optional[float] = None
    operation_group: Optional[str] = None
    started_by: Optional[int] = None
    completed_by: Optional[int] = None
    laser_nest: Optional[LaserNestOperationInfo] = None

    @field_serializer(
        "setup_time_hours",
        "run_time_hours",
        "run_time_per_piece",
        "quantity_complete",
        "quantity_scrapped",
        "actual_setup_hours",
        "actual_run_hours",
        when_used="json",
    )
    def serialize_decimal_number(self, value: Optional[Decimal]) -> Optional[float]:
        return _serialize_decimal_as_number(value)

    @field_serializer(
        "scheduled_start",
        "scheduled_end",
        "actual_start",
        "actual_end",
        "created_at",
        "updated_at",
        when_used="json",
    )
    def serialize_central_datetime(self, value: Optional[datetime]) -> Optional[str]:
        return to_central_iso(value)

    class Config:
        from_attributes = True


class WorkOrderBase(BaseModel):
    part_id: int = Field(..., gt=0, description="Part ID")
    parent_work_order_id: Optional[int] = Field(None, gt=0)
    work_order_type: str = Field(default="production", max_length=50)
    quantity_ordered: MoneySmall = Field(..., gt=Decimal("0"), description="Quantity ordered")
    priority: int = Field(default=5, ge=1, le=10, description="Priority (1=highest, 10=lowest)")
    due_date: Optional[date] = Field(None, description="Due date")
    customer_name: Optional[str] = Field(None, max_length=255)
    customer_po: Optional[str] = Field(None, max_length=50, description="Customer PO number")
    notes: Optional[str] = Field(None, max_length=2000)
    special_instructions: Optional[str] = Field(None, max_length=2000)


class WorkOrderCreate(WorkOrderBase):
    operations: List[WorkOrderOperationCreate] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_dates(self) -> "WorkOrderCreate":
        """Validate date relationships on input"""
        today = date.today()

        if self.due_date and self.due_date < today:
            raise ValueError("Due date cannot be in the past")

        return self


class WorkOrderUpdate(BaseModel):
    version: int = Field(..., ge=0, description="Version for optimistic locking")
    quantity_ordered: Optional[Decimal] = Field(None, gt=Decimal("0"))
    priority: Optional[int] = Field(None, ge=1, le=10)
    status: Optional[WorkOrderStatus] = None
    due_date: Optional[date] = None
    customer_name: Optional[str] = Field(None, max_length=255)
    customer_po: Optional[str] = Field(None, max_length=50)
    notes: Optional[str] = Field(None, max_length=2000)
    special_instructions: Optional[str] = Field(None, max_length=2000)
    quantity_complete: Optional[Decimal] = Field(None, ge=Decimal("0"))
    quantity_scrapped: Optional[Decimal] = Field(None, ge=Decimal("0"))


class WorkOrderResponse(WorkOrderBase):
    id: int
    version: Optional[int] = 0
    work_order_number: str
    status: WorkOrderStatus
    quantity_complete: MoneySmall
    quantity_scrapped: MoneySmall
    scheduled_start: Optional[datetime]
    scheduled_end: Optional[datetime]
    actual_start: Optional[datetime]
    actual_end: Optional[datetime]
    estimated_hours: Money
    actual_hours: Money
    estimated_cost: Money
    actual_cost: Money
    operation_count: int = 0
    operations_complete: int = 0
    operation_progress_percent: float = 0.0
    created_at: datetime
    updated_at: datetime
    operations: List[WorkOrderOperationResponse] = Field(default_factory=list)

    @field_serializer(
        "quantity_ordered",
        "quantity_complete",
        "quantity_scrapped",
        "estimated_hours",
        "actual_hours",
        "estimated_cost",
        "actual_cost",
        when_used="json",
    )
    def serialize_decimal_number(self, value: Optional[Decimal]) -> Optional[float]:
        return _serialize_decimal_as_number(value)

    @field_serializer(
        "scheduled_start",
        "scheduled_end",
        "actual_start",
        "actual_end",
        "created_at",
        "updated_at",
        when_used="json",
    )
    def serialize_central_datetime(self, value: Optional[datetime]) -> Optional[str]:
        return to_central_iso(value)

    class Config:
        from_attributes = True


class WorkOrderSummary(BaseModel):
    """Lightweight work order for lists/dashboards"""

    id: int
    work_order_number: str
    part_id: int
    parent_work_order_id: Optional[int] = None
    work_order_type: str = "production"
    part_number: Optional[str] = None
    part_name: Optional[str] = None
    part_type: Optional[str] = None
    status: WorkOrderStatus
    priority: int
    quantity_ordered: MoneySmall
    quantity_complete: MoneySmall
    operation_count: int = 0
    operations_complete: int = 0
    operation_progress_percent: float = 0.0
    due_date: Optional[date]
    customer_name: Optional[str]
    current_operation: Optional[str] = None

    @field_serializer(
        "quantity_ordered",
        "quantity_complete",
        when_used="json",
    )
    def serialize_decimal_number(self, value: Optional[Decimal]) -> Optional[float]:
        return _serialize_decimal_as_number(value)

    class Config:
        from_attributes = True
