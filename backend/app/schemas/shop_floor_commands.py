"""Shared production and hold inputs for canonical ERP and reviewed Hank commands."""

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.time_entry import TimeEntrySource
from app.models.work_order_blocker import WorkOrderBlockerCategory, WorkOrderBlockerSeverity


class ProductionReportRequest(BaseModel):
    request_id: Optional[str] = Field(
        None,
        min_length=8,
        max_length=100,
        pattern=r"^[A-Za-z0-9_:-]+$",
        description="Stable ID for one report, reused unchanged on retries.",
    )
    quantity_complete_delta: float = 0.0
    quantity_scrapped_delta: float = 0.0
    notes: Optional[str] = None
    # A0.3: structured scrap reason, same shape as ClockOut.scrap_reason (the
    # TimeEntry.scrap_reason column is String(255), hence the max_length).
    scrap_reason: Optional[str] = Field(
        None,
        max_length=255,
        description="Reason for scrapped parts; stored only when quantity_scrapped_delta > 0 "
        "and never cleared by a later reason-less report.",
    )
    # Lean Phase 1: structured scrap categorization (validated: exists, active,
    # belongs to the company). Either the code or free text satisfies the
    # scrap-requires-a-reason rule; the code is preferred, text stays narrative.
    scrap_reason_code_id: Optional[int] = Field(
        None,
        description="Id of a predefined scrap reason code (see /quality/scrap-reason-codes). "
        "Applied only when quantity_scrapped_delta > 0; never cleared by a code-less report.",
    )
    # A0.1 adoption telemetry: client channel (kiosk/desktop/scanner/import/backfill).
    source: Optional[TimeEntrySource] = Field(
        None,
        description="Adoption-telemetry channel of this production report (kiosk | desktop | scanner | "
        "backfill). Omit to keep the active entry's existing channel. 'import' is rejected (422) here "
        "(reserved for the bulk-migration loaders); a kiosk-scoped operator token forces 'kiosk' "
        "regardless of this hint.",
    )
    # Kiosk Foundry redesign (scrap -> NCR): file a Non-Conformance Report for the
    # scrap in THIS report, in the same transaction. Deliberately NO hold and NO
    # blocker (contrast with the process-step OOT quality hold) -- the machine
    # keeps running; Quality is notified through the NCR + operational event.
    open_ncr: bool = Field(
        False,
        description="File an NCR (source=in_process) for this report's scrap in the same transaction. "
        "Requires quantity_scrapped_delta > 0 (400 otherwise). No hold/blocker is created.",
    )
    ncr_description: Optional[str] = Field(
        None,
        max_length=2000,
        description="Optional operator narrative for the NCR; falls back to the scrap reason text/code.",
    )

    @model_validator(mode="after")
    def _require_scrap_reason(self) -> "ProductionReportRequest":
        # AS9100D defect-traceability invariant (same rule as ClockOut): a scrap delta MUST
        # carry a reason. Enforced at the data boundary so a scripted/API client can't post
        # reasonless scrap that the UIs already block. Lean Phase 1: EITHER a structured
        # scrap_reason_code_id OR non-blank free text satisfies the rule (old text-only
        # clients keep working). Blank/whitespace counts as missing; raised as a Pydantic
        # ValueError -> 422. A zero scrap delta with no reason stays valid; negatives/NaN
        # fall through to the handler's existing numeric guards.
        has_reason = (self.scrap_reason and self.scrap_reason.strip()) or self.scrap_reason_code_id is not None
        if (self.quantity_scrapped_delta or 0) > 0 and not has_reason:
            raise ValueError(
                "scrap_reason or scrap_reason_code_id is required when quantity_scrapped_delta is greater than 0"
            )
        return self


class OperationCompleteRequest(BaseModel):
    """Body of ``POST /shop-floor/operations/{id}/complete`` -- a CLOSE, not a count.

    The verb books the operation COMPLETE at the good count the floor already recorded
    (clock-outs and production reports); it records no good pieces of its own. Owner
    ruling 1 (2026-11-05): when that good count does not cover every unit that reached the
    operation, the shortfall is either declared here as scrap with a reason or the close
    is refused 409 and the operation stays open -- it is never closed short at the
    counted quantity. Unknown keys are refused (422) so a client that still sends a field
    this verb no longer honors learns that instead of being silently ignored.
    """

    model_config = ConfigDict(extra="forbid")

    quantity_complete: Optional[float] = Field(
        None,
        description="DEPRECATED, optional: the good count the caller expects is recorded. An expectation check "
        "only -- it is never stored. Refused 409 when it exceeds the good already recorded on the operation; "
        "omit it.",
    )
    quantity_scrapped: float = Field(
        0.0,
        description="Pieces LOST at this operation that were never reported, declared by the person closing it "
        "(an ADDITIVE delta on the operation's scrap, refused 409 above what is still unaccounted). Requires "
        "scrap_reason or scrap_reason_code_id.",
    )
    # max_length matches the WorkOrderOperation.scrap_reason String(255) column.
    scrap_reason: Optional[str] = Field(
        None,
        max_length=255,
        description="Reason for the scrap declared at close; never clears a reason already recorded.",
    )
    scrap_reason_code_id: Optional[int] = Field(
        None,
        description="Id of a predefined scrap reason code (see /quality/scrap-reason-codes); either this or "
        "scrap_reason satisfies the scrap-requires-a-reason rule.",
    )
    acknowledge_open_entries: bool = Field(
        False,
        description="A short close clocks everyone still clocked in out WITHOUT counting pieces they have not "
        "reported, so it is refused 409 naming them unless this is true. Not needed for a full-count close.",
    )
    notes: Optional[str] = None
    # A0.1 adoption telemetry: client channel (kiosk/desktop/scanner/import/backfill).
    # Optional -- omitted means unknown; unknown values are rejected with a 422.
    source: Optional[TimeEntrySource] = Field(
        None,
        description="Adoption-telemetry channel of this completion (kiosk | desktop | scanner | "
        "backfill). Also fills the channel on auto-closed open entries that have none; never overwrites "
        "a recorded channel. Omit when unknown. 'import' is rejected (422) here (reserved for the "
        "bulk-migration loaders); a kiosk-scoped operator token forces 'kiosk' regardless of this hint.",
    )

    # The scrap-reason rule (scrap declared at close carries a code or non-blank text,
    # AS9100D defect traceability) is enforced in the HANDLER, after the work-order start
    # rung, not as a model validator here: a READY operation on a held or draft job must get
    # the gates' 409 before any input check (the office twin's ladder), and a Pydantic
    # validator would answer 422 first.


class OperationHoldRequest(BaseModel):
    category: WorkOrderBlockerCategory = WorkOrderBlockerCategory.OTHER
    severity: WorkOrderBlockerSeverity = WorkOrderBlockerSeverity.MEDIUM
    note: Optional[str] = None
    # A0.1 adoption telemetry: client channel (kiosk/desktop/scanner/import/backfill).
    source: Optional[TimeEntrySource] = Field(
        None,
        description="Adoption-telemetry channel of this hold (kiosk | desktop | scanner | backfill). "
        "Also fills the channel on the open entries the hold auto-closes when they have none; never "
        "overwrites a recorded channel. Omit when unknown. 'import' is rejected (422) here (reserved "
        "for the bulk-migration loaders); a kiosk-scoped operator token forces 'kiosk' regardless of "
        "this hint.",
    )
