from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from app.schemas.base import UTCModel


class WorkCenterBase(UTCModel):
    code: str
    name: str
    work_center_type: str
    description: Optional[str] = None
    # Bounded to match chk_work_centers_hourly_rate_non_negative, the DB CHECK
    # migration 080 restored: a negative rate is a 422 at the boundary, not an
    # IntegrityError 500 at flush. Safe on the shared Base (WorkCenterResponse
    # inherits it) -- the 2026-07-31 prod pre-flight found zero rows below 0, and
    # the CHECK now keeps it that way.
    hourly_rate: float = Field(default=0.0, ge=0)
    capacity_hours_per_day: float = 8.0
    efficiency_factor: float = 1.0
    building: Optional[str] = None
    area: Optional[str] = None


class WorkCenterCreate(WorkCenterBase):
    pass


class WorkCenterUpdate(BaseModel):
    version: int  # Required for optimistic locking
    name: Optional[str] = None
    work_center_type: Optional[str] = None
    description: Optional[str] = None
    hourly_rate: Optional[float] = Field(None, ge=0)
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
