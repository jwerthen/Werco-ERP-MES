from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Optional, List
from datetime import datetime, date
from decimal import Decimal
from app.models.work_order import WorkOrderStatus, OperationStatus
from app.core.validation import (
    PositiveInteger,
    NonNegativeInteger,
    MoneySmall,
    Money,
    DescriptionLong
)


class WorkOrderOperationBase(BaseModel):
    work_center_id: int = Field(..., gt=0, description="Work center ID")
    sequence: int = Field(..., ge=10, le=990, multiple_of=10, description="Sequence (10-990, multiples of 10)")
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
    
    class Config:
        from_attributes = True


class WorkOrderBase(BaseModel):
    part_id: int = Field(..., gt=0, description="Part ID")
    quantity_ordered: MoneySmall = Field(..., gt=Decimal("0"), description="Quantity ordered")
    priority: int = Field(default=5, ge=1, le=10, description="Priority (1=highest, 10=lowest)")
    due_date: Optional[date] = Field(None, description="Due date")
    must_ship_by: Optional[date] = Field(None, description="Must ship by date")
    customer_name: Optional[str] = Field(None, max_length=255)
    customer_po: Optional[str] = Field(None, max_length=50, description="Customer PO number")
    lot_number: Optional[str] = Field(None, max_length=50)
    notes: Optional[str] = Field(None, max_length=2000)
    special_instructions: Optional[str] = Field(None, max_length=2000)

    @model_validator(mode='after')
    def validate_dates(self) -> 'WorkOrderBase':
        """Validate date relationships"""
        today = date.today()

        if self.due_date and self.due_date < today:
            raise ValueError('Due date cannot be in the past')

        if self.must_ship_by and self.due_date:
            if self.must_ship_by < self.due_date:
                raise ValueError('Must ship by date must be after due date')

        return self


class WorkOrderCreate(WorkOrderBase):
    operations: List[WorkOrderOperationCreate] = Field(default_factory=list)


class WorkOrderUpdate(BaseModel):
    version: int = Field(..., ge=0, description="Version for optimistic locking")
    quantity_ordered: Optional[Decimal] = Field(None, gt=Decimal("0"))
    priority: Optional[int] = Field(None, ge=1, le=10)
    status: Optional[WorkOrderStatus] = None
    due_date: Optional[date] = None
    must_ship_by: Optional[date] = None
    customer_name: Optional[str] = Field(None, max_length=255)
    customer_po: Optional[str] = Field(None, max_length=50)
    lot_number: Optional[str] = Field(None, max_length=50)
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
    created_at: datetime
    updated_at: datetime
    operations: List[WorkOrderOperationResponse] = Field(default_factory=list)
    
    class Config:
        from_attributes = True


class WorkOrderSummary(BaseModel):
    """Lightweight work order for lists/dashboards"""
    id: int
    work_order_number: str
    part_id: int
    part_number: Optional[str] = None
    part_name: Optional[str] = None
    part_type: Optional[str] = None
    status: WorkOrderStatus
    priority: int
    quantity_ordered: MoneySmall
    quantity_complete: MoneySmall
    due_date: Optional[date]
    customer_name: Optional[str]
    current_operation: Optional[str] = None
    
    class Config:
        from_attributes = True
