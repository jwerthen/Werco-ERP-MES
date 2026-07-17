import math
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.time_utils import to_utc_iso
from app.models.time_entry import TimeEntrySource, TimeEntryType
from app.schemas.work_order import QualityExceptionInfo


class ProductionReductionRequest(BaseModel):
    """Over-count correction request (inverse of the additive production report).

    Shared by BOTH reduce-production endpoints -- the operator self-service verb
    (``POST /shop-floor/operations/{id}/reduce-production``, bounded to the caller's
    own unapproved evidence) and the supervisor/office verb
    (``POST /work-orders/operations/{id}/reduce-production``, bounded to ALL
    unapproved evidence on the operation). Removes good-count quantity that was
    over-reported, BEFORE the operation / work order is complete. This is a miscount
    correction, NOT a scrap move: it never touches scrap fields, never changes
    status, and never mutates APPROVED labor (approval is the immutability boundary;
    unapprove first).
    """

    quantity_delta: float = Field(
        ...,
        gt=0,
        description="Good-count quantity to REMOVE from this operation. Must be > 0 and no greater "
        "than the unapproved evidence the endpoint may walk down (the caller's own entries for the "
        "shop-floor verb; any operator's for the office verb).",
    )
    # Correction reason for the tamper-evident audit trail -- this is NOT a scrap reason
    # (no scrap moves here). Required and non-blank so every walk-back is explainable.
    reason: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Why the count is being corrected (e.g. 'double-scanned the tray'). Required "
        "for the audit trail; this is a correction reason, NOT a scrap reason.",
    )
    # A0.1 adoption telemetry: same trust model / channel semantics as the additive twin.
    source: Optional[TimeEntrySource] = Field(
        None,
        description="Adoption-telemetry channel of this correction (kiosk | desktop | scanner | "
        "backfill). Omit to keep the entry's existing channel. 'import' is rejected (422) here "
        "(reserved for the bulk-migration loaders); a kiosk-scoped operator token forces 'kiosk' "
        "regardless of this hint.",
    )
    notes: Optional[str] = Field(
        None,
        description="Optional extra note. On the shop-floor verb it is appended to the caller's "
        "active time entry; on the office verb it is recorded on the audit row only.",
    )

    @field_validator("quantity_delta")
    @classmethod
    def _finite_delta(cls, value: float) -> float:
        # Mirror the additive report's numeric guard: reject NaN/Inf at the boundary
        # (gt=0 already rejects NaN and negatives, but +Inf slips past a bare gt).
        if math.isnan(value) or math.isinf(value):
            raise ValueError("quantity_delta must be a finite number")
        return value

    @field_validator("reason")
    @classmethod
    def _reason_not_blank(cls, value: str) -> str:
        # Whitespace-only is meaningless for an audit trail; treat it as missing.
        if not value.strip():
            raise ValueError("reason is required")
        return value.strip()


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
        "(kiosk | desktop | scanner | backfill). Omit when unknown -- stored NULL, never guessed. "
        "'import' is rejected (422) here -- it is reserved for the bulk-migration loaders that write "
        "TimeEntry directly; a kiosk-scoped operator token forces 'kiosk' regardless of this hint.",
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
    # Lean Phase 1: structured scrap categorization. Validated server-side (must
    # exist, be active, and belong to the active company); free-text scrap_reason
    # stays as narrative detail alongside it.
    scrap_reason_code_id: Optional[int] = Field(
        None,
        description="Id of a predefined scrap reason code (see /quality/scrap-reason-codes). "
        "Preferred over free text; either satisfies the scrap-requires-a-reason rule.",
    )
    notes: Optional[str] = None
    # A0.1 adoption telemetry: channel of THIS clock-out write. Optional; when
    # omitted the entry keeps whatever channel clock-in recorded.
    source: Optional[TimeEntrySource] = Field(
        None,
        description="Adoption-telemetry channel of this clock-out write "
        "(kiosk | desktop | scanner | backfill). Omit to keep the channel recorded at clock-in. "
        "'import' is rejected (422) here -- it is reserved for the bulk-migration loaders; a "
        "kiosk-scoped operator token forces 'kiosk' regardless of this hint.",
    )

    @model_validator(mode="after")
    def _require_scrap_reason(self) -> "ClockOut":
        # AS9100D defect-traceability invariant (compliance, not cosmetics): any scrapped
        # quantity MUST carry a reason. Enforced at the data boundary -- not just in the
        # kiosk/desktop UIs -- so a scripted/API client can't record reasonless scrap.
        # Lean Phase 1: EITHER a structured scrap_reason_code_id OR non-blank free text
        # satisfies the rule (code preferred; old clients sending only text keep working).
        # A blank/whitespace-only reason is treated as missing. Raised as a Pydantic
        # ValueError -> FastAPI returns 422. scrap == 0 with no reason stays valid (the
        # kiosk COMPLETE flow clocks out with zero scrap and no reason); negatives/NaN
        # fall through to the handler's existing numeric guards.
        has_reason = (self.scrap_reason and self.scrap_reason.strip()) or self.scrap_reason_code_id is not None
        if (self.quantity_scrapped or 0) > 0 and not has_reason:
            raise ValueError(
                "scrap_reason or scrap_reason_code_id is required when quantity_scrapped is greater than 0"
            )
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
    # Lean Phase 1: structured scrap categorization (null = uncoded/legacy row).
    scrap_reason_code_id: Optional[int] = None
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
