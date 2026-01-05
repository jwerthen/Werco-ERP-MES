from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime, date
from app.models.work_order import WorkOrderStatus, OperationStatus


class WorkOrderOperationBase(BaseModel):
    work_center_id: int
    sequence: int
    operation_number: Optional[str] = None
    name: str
    description: Optional[str] = None
    setup_instructions: Optional[str] = None
    run_instructions: Optional[str] = None
    setup_time_hours: float = 0.0
    run_time_hours: float = 0.0
    run_time_per_piece: float = 0.0
    requires_inspection: bool = False
    inspection_type: Optional[str] = None


class WorkOrderOperationCreate(WorkOrderOperationBase):
    pass


class WorkOrderOperationUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    setup_instructions: Optional[str] = None
    run_instructions: Optional[str] = None
    setup_time_hours: Optional[float] = None
    run_time_hours: Optional[float] = None
    run_time_per_piece: Optional[float] = None
    status: Optional[OperationStatus] = None
    quantity_complete: Optional[float] = None
    quantity_scrapped: Optional[float] = None
    requires_inspection: Optional[bool] = None
    inspection_complete: Optional[bool] = None


class WorkOrderOperationResponse(WorkOrderOperationBase):
    id: int
    work_order_id: int
    status: OperationStatus
    quantity_complete: float
    quantity_scrapped: float
    actual_setup_hours: float
    actual_run_hours: float
    scheduled_start: Optional[datetime]
    scheduled_end: Optional[datetime]
    actual_start: Optional[datetime]
    actual_end: Optional[datetime]
    inspection_complete: bool
    created_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True


class WorkOrderBase(BaseModel):
    part_id: int
    quantity_ordered: float
    priority: int = 5
    due_date: Optional[date] = None
    must_ship_by: Optional[date] = None
    customer_name: Optional[str] = None
    customer_po: Optional[str] = None
    lot_number: Optional[str] = None
    notes: Optional[str] = None
    special_instructions: Optional[str] = None


class WorkOrderCreate(WorkOrderBase):
    operations: List[WorkOrderOperationCreate] = []


class WorkOrderUpdate(BaseModel):
    quantity_ordered: Optional[float] = None
    priority: Optional[int] = None
    status: Optional[WorkOrderStatus] = None
    due_date: Optional[date] = None
    must_ship_by: Optional[date] = None
    customer_name: Optional[str] = None
    customer_po: Optional[str] = None
    lot_number: Optional[str] = None
    notes: Optional[str] = None
    special_instructions: Optional[str] = None
    quantity_complete: Optional[float] = None
    quantity_scrapped: Optional[float] = None


class WorkOrderResponse(WorkOrderBase):
    id: int
    work_order_number: str
    status: WorkOrderStatus
    quantity_complete: float
    quantity_scrapped: float
    scheduled_start: Optional[datetime]
    scheduled_end: Optional[datetime]
    actual_start: Optional[datetime]
    actual_end: Optional[datetime]
    estimated_hours: float
    actual_hours: float
    estimated_cost: float
    actual_cost: float
    created_at: datetime
    updated_at: datetime
    operations: List[WorkOrderOperationResponse] = []
    
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
    quantity_ordered: float
    quantity_complete: float
    due_date: Optional[date]
    customer_name: Optional[str]
    current_operation: Optional[str] = None
    
    class Config:
        from_attributes = True
