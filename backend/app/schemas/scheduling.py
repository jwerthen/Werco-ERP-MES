from datetime import date
from typing import Annotated, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from app.schemas.material_readiness import MaterialReadiness

CalendarHours = Annotated[float, Field(ge=0, le=24)]


class WorkingCalendarOverride(BaseModel):
    date: date
    hours: CalendarHours
    reason: str = Field(min_length=1, max_length=200)


class WorkingCalendarUpdate(BaseModel):
    expected_version: int = Field(ge=0)
    weekly_hours: List[CalendarHours] = Field(min_length=7, max_length=7)
    overrides: List[WorkingCalendarOverride] = Field(default_factory=list, max_length=400)

    @model_validator(mode="after")
    def validate_dates(self):
        if len({row.date for row in self.overrides}) != len(self.overrides):
            raise ValueError("Each override date must be unique")
        if any(not row.reason.strip() for row in self.overrides):
            raise ValueError("Describe each calendar exception")
        return self


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


class SchedulingImpactRequest(BaseModel):
    action: Literal["earliest", "shift"]
    work_order_ids: List[int] = Field(min_length=1, max_length=50)
    shift_days: int = Field(default=0, ge=-30, le=30)
    horizon_days: int = Field(default=90, ge=1, le=365)

    @model_validator(mode="after")
    def validate_scope(self):
        if any(item <= 0 for item in self.work_order_ids) or len(set(self.work_order_ids)) != len(self.work_order_ids):
            raise ValueError("Select distinct valid work orders")
        if self.action == "shift" and self.shift_days == 0:
            raise ValueError("Choose a nonzero date shift")
        return self


class SchedulingImpactApplyRequest(BaseModel):
    plan_token: str = Field(min_length=20, max_length=500000)


class SchedulingImpactOperation(BaseModel):
    operation_id: int
    operation_number: Optional[str] = None
    operation_name: str
    work_center_id: int
    work_center_code: str
    before_start: Optional[str] = None
    before_end: Optional[str] = None
    after_start: str
    after_end: str
    before_status: str
    after_status: str


class SchedulingImpactJob(BaseModel):
    materials: Optional[MaterialReadiness] = None
    work_order_id: int
    work_order_number: str
    due_date: Optional[str] = None
    before_finish: Optional[str] = None
    after_finish: Optional[str] = None
    before_late_days: Optional[int] = None
    late_days: Optional[int] = None
    outcome: Literal["changed", "skipped", "blocked"]
    reason: Optional[str] = None
    operations: List[SchedulingImpactOperation] = Field(default_factory=list)


class SchedulingImpactAffectedJob(BaseModel):
    work_order_id: int
    work_order_number: str


class SchedulingImpactCapacity(BaseModel):
    work_center_id: int
    work_center_code: str
    date: str
    capacity_hours: float
    before_hours: float
    after_hours: float
    overload_hours: float
    affected_jobs: List[SchedulingImpactAffectedJob] = Field(default_factory=list)


class SchedulingImpactSummary(BaseModel):
    selected_jobs: int
    changed_jobs: int
    changed_operations: int
    skipped_jobs: int
    blocked_jobs: int
    late_jobs: int
    overloaded_days: int


class SchedulingImpactResponse(BaseModel):
    action: Literal["earliest", "shift"]
    shift_days: int
    plan_token: Optional[str] = None
    expires_at: str
    summary: SchedulingImpactSummary
    jobs: List[SchedulingImpactJob]
    capacity: List[SchedulingImpactCapacity]


class SchedulingImpactApplyResponse(BaseModel):
    message: str
    already_applied: bool
    applied_work_order_ids: List[int]
    changed_operations: int
