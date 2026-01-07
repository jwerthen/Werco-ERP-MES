from pydantic import BaseModel
from typing import Optional, List
from datetime import date


class SchedulingRunRequest(BaseModel):
    work_center_ids: Optional[List[int]] = None
    horizon_days: int = 90
    optimize_setup: bool = False


class SchedulingConflict(BaseModel):
    work_center_id: int
    date: str
    used_hours: float
    capacity_hours: float
    overload_hours: float
    utilization_pct: float


class LoadChartRequest(BaseModel):
    work_center_id: int
    start_date: date
    end_date: date


class LoadChartDataPoint(BaseModel):
    date: str
    used_hours: float
    available_hours: float
    utilization_pct: float
