from pydantic import BaseModel
from typing import Optional
from datetime import datetime
class WorkCenterBase(BaseModel):
    code: str
    name: str
    work_center_type: str
    description: Optional[str] = None
    hourly_rate: float = 0.0
    capacity_hours_per_day: float = 8.0
    efficiency_factor: float = 1.0
    building: Optional[str] = None
    area: Optional[str] = None


class WorkCenterCreate(WorkCenterBase):
    pass


class WorkCenterUpdate(BaseModel):
    version: int  # Required for optimistic locking
    name: Optional[str] = None
    description: Optional[str] = None
    hourly_rate: Optional[float] = None
    capacity_hours_per_day: Optional[float] = None
    efficiency_factor: Optional[float] = None
    is_active: Optional[bool] = None
    current_status: Optional[str] = None
    building: Optional[str] = None
    area: Optional[str] = None


class WorkCenterResponse(WorkCenterBase):
    id: int
    version: Optional[int] = 0  # For optimistic locking
    is_active: bool
    current_status: str
    availability_rate: Optional[float] = None
    created_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True
