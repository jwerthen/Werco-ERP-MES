from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator

from app.core.time_utils import to_utc_iso
from app.models.time_entry import TimeEntrySource, TimeEntryType
from app.schemas.work_order import QualityExceptionInfo


class StepsIncompleteInfo(BaseModel):
    """Process-sheet gate warning (PR 3): this clock-out brought the operation to its
    target quantity, but required snapshot steps lack live conforming records, so the
    operation was left IN_PROGRESS instead of auto-completing. ``missing`` carries the
    same ``[{step_id, label, serials}]`` items as the complete endpoints' 409 payload;
    complete the operation via /complete once the records exist."""

    code: str = "STEPS_INCOMPLETE"
    missing: List[Dict[str, Any]] = Field(default_factory=list)


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
    # A0.1 adoption telemetry: client channel (kiosk/desktop/scanner/import/backfill).
    # Optional -- omitted means unknown (stored NULL); unknown values are a 422.
    source: Optional[TimeEntrySource] = Field(
        None,
        description="Adoption-telemetry channel that produced this clock-in "
        "(kiosk | desktop | scanner | import | backfill). Omit when unknown -- stored NULL, never guessed.",
    )


class ClockOut(BaseModel):
    """For completing work"""

    quantity_produced: float = 0.0
    quantity_scrapped: float = 0.0
    # max_length matches the TimeEntry.scrap_reason String(255) column.
    scrap_reason: Optional[str] = Field(
        None,
        max_length=255,
        description="Reason for scrapped parts; omit when nothing was scrapped -- an omitted "
        "reason never clears one recorded by an in-shift production report.",
    )
    notes: Optional[str] = None
    # A0.1 adoption telemetry: channel of THIS clock-out write. Optional; when
    # omitted the entry keeps whatever channel clock-in recorded.
    source: Optional[TimeEntrySource] = Field(
        None,
        description="Adoption-telemetry channel of this clock-out write "
        "(kiosk | desktop | scanner | import | backfill). Omit to keep the channel recorded at clock-in.",
    )

    @model_validator(mode="after")
    def _require_scrap_reason(self) -> "ClockOut":
        # AS9100D defect-traceability invariant (compliance, not cosmetics): any scrapped
        # quantity MUST carry a reason. Enforced at the data boundary -- not just in the
        # kiosk/desktop UIs -- so a scripted/API client can't record reasonless scrap.
        # A blank/whitespace-only reason is treated as missing. Raised as a Pydantic
        # ValueError -> FastAPI returns 422. scrap == 0 with no reason stays valid (the
        # kiosk COMPLETE flow clocks out with zero scrap and no reason); negatives/NaN
        # fall through to the handler's existing numeric guards.
        if (self.quantity_scrapped or 0) > 0 and not (self.scrap_reason and self.scrap_reason.strip()):
            raise ValueError("scrap_reason is required when quantity_scrapped is greater than 0")
        return self


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
    # A0.1 adoption telemetry: channel that produced this labor record (NULL = unknown).
    # Deliberately plain str on the read path (requests validate against TimeEntrySource):
    # the column is a plain VARCHAR so a row written by a newer release (or a bulk
    # import) with a channel this code doesn't know yet must read back fine, not 500.
    source: Optional[str] = Field(
        None,
        description="Adoption-telemetry channel that produced this labor record; null = unknown/legacy.",
    )
    created_at: datetime
    updated_at: datetime
    # Warn-and-record (Batch 4 / rank 7): quality gates that were unsatisfied when a
    # clock-out completed the operation/WO. Backward-compatible -- defaults to empty.
    quality_exceptions: List[QualityExceptionInfo] = Field(default_factory=list)
    # Warn-and-record (Batch 11C / G5-B): operator-qualification gates that were
    # unsatisfied at clock-in (skill level below Basic, or a missing/expired required
    # certification for the work center). Backward-compatible -- defaults to empty, so
    # an all-clear clock-in is indistinguishable from the pre-G5-B response shape. The
    # ``code`` values here are ``operator_not_skill_qualified`` /
    # ``operator_certification_missing_or_expired``; reuses QualityExceptionInfo's shape.
    qualification_exceptions: List[QualityExceptionInfo] = Field(default_factory=list)
    # Process-sheet gate (PR 3): set when THIS clock-out reached the operation target
    # but required steps lack records — the op stayed IN_PROGRESS (never trapped the
    # entry; labor closed normally). Backward-compatible: null when not applicable.
    steps_incomplete: Optional[StepsIncompleteInfo] = None

    class Config:
        from_attributes = True
        json_encoders = {datetime: to_utc_iso}
