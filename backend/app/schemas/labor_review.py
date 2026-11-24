"""Request / response contracts for Labor Review (``/shop-floor/time-entries``).

The list read (``GET /shop-floor/time-entries``) and the supervisor clock-out verb
(``POST /shop-floor/time-entries/{id}/set-clock-out``). The logic lives in
``app/services/labor_review_service.py``; these are the wire shapes only.
"""

from datetime import date, datetime
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.base import UTCModel

# The review flags the list computes per entry. Kept as one closed vocabulary so the page can
# render a chip per value and a CSV column can carry them verbatim.
LaborReviewFlag = Literal[
    "open_past_shift_end",
    "long_entry",
    "overlap",
    "open_on_closed_work",
    "long_zero_pieces",
    "backfill",
]

LaborReviewStatus = Literal["open", "closed"]


class LaborReviewEntry(UTCModel):
    """One time entry as Labor Review shows it."""

    id: int
    version: int
    user_id: int
    employee_name: Optional[str] = None
    employee_id: Optional[str] = None
    work_order_id: Optional[int] = None
    work_order_number: Optional[str] = None
    work_order_status: Optional[str] = None
    operation_id: Optional[int] = None
    operation_number: Optional[str] = None
    operation_name: Optional[str] = None
    operation_status: Optional[str] = None
    work_center_id: Optional[int] = None
    work_center_name: Optional[str] = None
    entry_type: Optional[str] = None
    source: Optional[str] = None
    clock_in: datetime
    clock_out: Optional[datetime] = None
    duration_hours: Optional[float] = Field(None, description="Stored hours; null while the entry is open.")
    elapsed_hours: float = Field(..., description="Closed: the stored duration. Open: clock-in to now.")
    is_open: bool
    quantity_produced: float = 0.0
    quantity_scrapped: float = 0.0
    approved: Optional[datetime] = None
    approved_by: Optional[int] = None
    approved_by_name: Optional[str] = None
    production_day: date
    shift_name: Optional[str] = None
    shift_end: Optional[datetime] = Field(
        None, description="End of the shift the entry started in (the close dialog's default time), if any."
    )
    flags: List[LaborReviewFlag] = Field(default_factory=list)
    can_approve: bool = Field(False, description="The caller may approve this entry now (closed, not their own).")
    can_unapprove: bool = Field(False, description="The caller may clear this entry's approval (not their own).")
    can_set_clock_out: bool = Field(
        False, description="The caller may set this entry's clock-out (approver role, unapproved, not their own)."
    )


class LaborReviewListResponse(UTCModel):
    items: List[LaborReviewEntry]
    total: int
    page: int
    page_size: int
    has_next: bool
    start_date: Optional[date] = Field(None, description="First production day read; null = no lower bound.")
    end_date: Optional[date] = Field(None, description="Last production day read; null = no upper bound.")
    flag_counts: Dict[str, int] = Field(
        default_factory=dict, description="Entries carrying each flag across every page of the filtered result."
    )
    long_entry_hours: float
    long_zero_pieces_hours: float


class TimeEntryApprovalRequest(BaseModel):
    """Optional body of approve / unapprove: the entry's version as the approver last read it."""

    version: Optional[int] = Field(
        None,
        ge=1,
        description="The entry's version as last read; when given, a stale one is refused 409.",
    )

    model_config = ConfigDict(extra="forbid")


class SetClockOutRequest(BaseModel):
    """A supervisor's stated clock-out for another person's unapproved time entry."""

    clock_out: datetime = Field(..., description="The time the person actually stopped (UTC; naive is read as UTC).")
    reason: str = Field(..., min_length=1, max_length=500, description="Why the clock-out is being set.")
    version: int = Field(..., ge=1, description="The entry's version as last read; a stale one is refused 409.")

    model_config = ConfigDict(extra="forbid")

    @field_validator("reason")
    @classmethod
    def _reason_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("A reason is required")
        return stripped


class HoursRederivation(UTCModel):
    """What the verb did to the stored operation and work-order hours."""

    applied: bool
    skipped_reason: Optional[str] = None
    hours_delta: float = Field(..., description="Change in this entry's own hours (new minus old).")
    operation_bucket: Optional[Literal["setup", "run"]] = None
    operation_hours_before: Optional[float] = None
    operation_hours_after: Optional[float] = None
    work_order_hours_before: Optional[float] = None
    work_order_hours_after: Optional[float] = None
    job_cost_recomputed: bool = False
    actual_cost_recomputed: bool = False


class SetClockOutResponse(UTCModel):
    entry: LaborReviewEntry
    changed: bool
    previous_clock_out: Optional[datetime] = None
    previous_duration_hours: Optional[float] = None
    hours: Optional[HoursRederivation] = None
