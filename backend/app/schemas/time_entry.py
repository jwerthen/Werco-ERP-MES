from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from app.models.time_entry import TimeEntryType


class TimeEntryBase(BaseModel):
    work_order_id: Optional[int] = None
    operation_id: Optional[int] = None
    work_center_id: Optional[int] = None
    entry_type: TimeEntryType = TimeEntryType.RUN
    notes: Optional[str] = None


class ClockIn(BaseModel):
    """For starting work on an operation"""
    work_order_id: int
    operation_id: int
    work_center_id: int
    entry_type: TimeEntryType = TimeEntryType.RUN
    notes: Optional[str] = None


class ClockOut(BaseModel):
    """For completing work"""
    quantity_produced: float = 0.0
    quantity_scrapped: float = 0.0
    scrap_reason: Optional[str] = None
    notes: Optional[str] = None


class TimeEntryCreate(TimeEntryBase):
    clock_in: datetime
    clock_out: Optional[datetime] = None
    quantity_produced: float = 0.0
    quantity_scrapped: float = 0.0


class TimeEntryUpdate(BaseModel):
    clock_out: Optional[datetime] = None
    quantity_produced: Optional[float] = None
    quantity_scrapped: Optional[float] = None
    scrap_reason: Optional[str] = None
    downtime_reason: Optional[str] = None
    notes: Optional[str] = None


class TimeEntryResponse(TimeEntryBase):
    id: int
    user_id: int
    clock_in: datetime
    clock_out: Optional[datetime]
    duration_hours: Optional[float]
    quantity_produced: float
    quantity_scrapped: float
    scrap_reason: Optional[str]
    downtime_reason: Optional[str]
    approved: Optional[datetime]
    approved_by: Optional[int]
    created_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True
