"""Request/response contracts for the production calendar (production day + shifts).

Wall times are ``HH:MM`` strings on the company's local clock (``Company.timezone``,
default America/Chicago). A shift whose ``end`` is at or before its ``start`` ends the
next day (``"15:30"`` -> ``"02:00"``); a shift always belongs to the production day it
started on. ``days`` are ``0`` = Monday .. ``6`` = Sunday (``date.weekday()``, the
``WorkingCalendar.weekly_hours`` order) and name the day a shift STARTS on. Instants in
responses are UTC with a trailing ``Z``; ``production_day`` values are plain dates.
"""

from datetime import date, datetime
from typing import Annotated, List, Optional

from pydantic import BaseModel, Field, field_validator

from app.schemas.base import UTCModel

WALL_TIME_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"

WallTime = Annotated[
    str,
    Field(pattern=WALL_TIME_PATTERN, description="Local wall time, HH:MM (00:00-23:59)", examples=["05:00"]),
]
Weekday = Annotated[int, Field(ge=0, le=6, description="0 = Monday ... 6 = Sunday")]

MAX_PATTERNS = 20
MAX_SHIFTS_PER_PATTERN = 12
MAX_WORK_CENTERS_PER_PATTERN = 500


class ShiftDefinitionIn(BaseModel):
    id: Optional[int] = Field(None, description="An existing shift's id to keep it; omit for a new shift")
    code: str = Field(min_length=1, max_length=20, description="Short unique code, e.g. W1")
    name: str = Field(min_length=1, max_length=50)
    start: WallTime
    end: WallTime = Field(description="Local wall time; at or before `start` means it ends the next day")
    days: List[Weekday] = Field(min_length=1, max_length=7, description="Days the shift STARTS on")
    planned_minutes: int = Field(
        ge=1,
        le=1440,
        description="Planned production minutes (breaks and lunch excluded); at most the shift's length",
    )
    is_active: bool = Field(True, description="false retires the shift (kept for history, no longer attributed)")
    sort_order: int = Field(0, ge=0, le=1000)

    @field_validator("code", "name")
    @classmethod
    def _stripped(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("days")
    @classmethod
    def _unique_days(cls, value: List[int]) -> List[int]:
        if len(set(value)) != len(value):
            raise ValueError("each day may appear once")
        return sorted(value)


class ShiftPatternIn(BaseModel):
    id: Optional[int] = Field(None, description="An existing pattern's id to keep it; omit for a new pattern")
    name: str = Field(min_length=1, max_length=50)
    is_default: bool = Field(False, description="The pattern every work center in no other pattern uses (one at most)")
    sort_order: int = Field(0, ge=0, le=1000)
    work_center_ids: List[int] = Field(default_factory=list, max_length=MAX_WORK_CENTERS_PER_PATTERN)
    shifts: List[ShiftDefinitionIn] = Field(default_factory=list, max_length=MAX_SHIFTS_PER_PATTERN)

    @field_validator("name")
    @classmethod
    def _stripped(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class ProductionCalendarUpdate(BaseModel):
    """The WHOLE calendar document; anything omitted is removed (a referenced shift must be retired instead)."""

    expected_version: int = Field(ge=0, description="The `version` you read (0 when nothing is configured yet)")
    production_day_start: WallTime = Field(description="Local wall time the production day starts, e.g. 05:00")
    patterns: List[ShiftPatternIn] = Field(default_factory=list, max_length=MAX_PATTERNS)


class ShiftDefinitionOut(UTCModel):
    id: int
    pattern_id: int
    code: str
    name: str
    start: str
    end: str
    start_minute: int = Field(description="Minutes after local midnight of the start day")
    end_minute: int = Field(description="Minutes after local midnight of the start day; above 1440 = next day")
    ends_next_day: bool
    days: List[int]
    planned_minutes: int
    is_active: bool
    sort_order: int


class ShiftPatternOut(UTCModel):
    id: int
    name: str
    is_default: bool
    sort_order: int
    work_center_ids: List[int]
    shifts: List[ShiftDefinitionOut]


class ShiftOccurrenceOut(UTCModel):
    shift_id: int
    code: str
    name: str
    pattern_id: int
    production_day: date
    start_utc: datetime
    end_utc: datetime
    planned_minutes: int


class ProductionDayOut(UTCModel):
    production_day: date
    start_utc: datetime
    end_utc: datetime
    occurrences: List[ShiftOccurrenceOut] = Field(
        default_factory=list, description="Every active shift of every pattern on this production day"
    )


class ProductionCalendarResponse(UTCModel):
    configured: bool = Field(description="false = no calendar: production day = company-local calendar day, no shifts")
    version: int
    timezone: str
    production_day_start: str
    production_day_start_minute: int
    updated_at: Optional[datetime] = None
    updated_by: Optional[int] = None
    patterns: List[ShiftPatternOut]
    today: ProductionDayOut


class ProductionMomentResponse(UTCModel):
    at: datetime
    work_center_id: Optional[int] = None
    pattern_id: Optional[int] = Field(None, description="The pattern whose shifts apply (explicit member or default)")
    production_day: date
    production_day_start_utc: datetime
    production_day_end_utc: datetime
    shift: Optional[ShiftOccurrenceOut] = Field(
        None, description="The shift the instant belongs to; in an overlap the outgoing (earlier-start) shift"
    )
    overlapping: List[ShiftOccurrenceOut] = Field(default_factory=list, description="Every shift running at `at`")
    configured: bool
    calendar_version: int


class WorkingCalendarSyncRow(UTCModel):
    work_center_id: int
    work_center_code: str
    work_center_name: str
    pattern_id: int
    pattern_name: str
    current_version: int = Field(description="0 = the work center has no saved working calendar yet")
    current_weekly_hours: List[float]
    new_weekly_hours: List[float]
    overrides_kept: int
    changed: bool


class WorkingCalendarSyncResponse(UTCModel):
    dry_run: bool
    configured: bool
    calendar_version: int
    changed_count: int
    rows: List[WorkingCalendarSyncRow]
