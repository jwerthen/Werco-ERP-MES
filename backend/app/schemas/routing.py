from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime


class RoutingOperationBase(BaseModel):
    sequence: int
    operation_number: Optional[str] = None
    name: str
    description: Optional[str] = None
    work_center_id: int
    setup_hours: float = 0.0
    run_hours_per_unit: float = 0.0
    move_hours: float = 0.0
    queue_hours: float = 0.0
    cycle_time_seconds: Optional[float] = None
    pieces_per_cycle: int = 1
    labor_rate_override: Optional[float] = None
    overhead_rate: float = 0.0
    is_inspection_point: bool = False
    inspection_instructions: Optional[str] = None
    work_instructions: Optional[str] = None
    setup_instructions: Optional[str] = None
    tooling_requirements: Optional[str] = None
    fixture_requirements: Optional[str] = None
    is_outside_operation: bool = False
    vendor_id: Optional[int] = None
    outside_cost: float = 0.0
    outside_lead_days: int = 0


class RoutingOperationCreate(RoutingOperationBase):
    pass


class RoutingOperationUpdate(BaseModel):
    sequence: Optional[int] = None
    operation_number: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    work_center_id: Optional[int] = None
    setup_hours: Optional[float] = None
    run_hours_per_unit: Optional[float] = None
    move_hours: Optional[float] = None
    queue_hours: Optional[float] = None
    cycle_time_seconds: Optional[float] = None
    pieces_per_cycle: Optional[int] = None
    labor_rate_override: Optional[float] = None
    overhead_rate: Optional[float] = None
    is_inspection_point: Optional[bool] = None
    inspection_instructions: Optional[str] = None
    work_instructions: Optional[str] = None
    setup_instructions: Optional[str] = None
    tooling_requirements: Optional[str] = None
    fixture_requirements: Optional[str] = None
    is_outside_operation: Optional[bool] = None
    vendor_id: Optional[int] = None
    outside_cost: Optional[float] = None
    outside_lead_days: Optional[int] = None
    is_active: Optional[bool] = None


class WorkCenterSummary(BaseModel):
    id: int
    code: str
    name: str
    work_center_type: str
    hourly_rate: float
    
    class Config:
        from_attributes = True


class RoutingOperationResponse(RoutingOperationBase):
    id: int
    routing_id: int
    work_center: Optional[WorkCenterSummary] = None
    is_active: bool
    created_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True
        use_enum_values = True


class RoutingBase(BaseModel):
    part_id: int
    revision: str = "A"
    description: Optional[str] = None


class RoutingCreate(RoutingBase):
    pass


class RoutingUpdate(BaseModel):
    revision: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    is_active: Optional[bool] = None


class PartSummary(BaseModel):
    id: int
    part_number: str
    name: str
    part_type: str
    
    class Config:
        from_attributes = True
        use_enum_values = True


class RoutingResponse(RoutingBase):
    id: int
    status: str
    is_active: bool
    effective_date: Optional[datetime]
    total_setup_hours: float
    total_run_hours_per_unit: float
    total_labor_cost: float
    total_overhead_cost: float
    part: Optional[PartSummary] = None
    operations: List[RoutingOperationResponse] = []
    created_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True


class RoutingListResponse(BaseModel):
    id: int
    part_id: int
    part: Optional[PartSummary] = None
    revision: str
    status: str
    is_active: bool
    total_setup_hours: float
    total_run_hours_per_unit: float
    operation_count: int = 0
    created_at: datetime
    
    class Config:
        from_attributes = True
