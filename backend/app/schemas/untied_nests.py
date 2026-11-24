"""Response contracts for the untied-laser-nest reads (``/laser-nests/untied`` and friends).

Every schema here describes a PURE READ: nothing in ``services/untied_nest_service.py``
writes a ledger row, an audit row or an event. The one action the worklist offers,
"Tie this sheet", is the EXISTING ``POST /work-orders/{id}/material-allocations`` verb;
``proposed_tie`` below is the body the client sends to it, never something this read
acts on itself.
"""

from datetime import date, datetime
from typing import List, Literal, Optional

from pydantic import Field

from app.schemas.base import UTCModel

UntiedNestStatusFilter = Literal["open", "completed", "all"]


class SheetCandidate(UTCModel):
    """One stock part the sheet matcher proposes for a nest (advisory)."""

    part_id: int
    part_number: str
    part_name: str
    unit_of_measure: Optional[str] = None
    score: float = Field(..., description="Matcher score: 100 is exact thickness + grade + size")
    reason: str = Field(..., description="The matcher's one-sentence evidence for this part")
    basis: str = Field("deterministic", description="'deterministic' (spec) or 'history' (past ties)")
    on_hand: float = Field(0.0, description="Company-owned, drawable on-hand of this part")
    on_hand_known: bool = Field(True, description="False when the stock read failed; on_hand is then 0")


class SheetSuggestionView(UTCModel):
    """The sheet matcher's answer for one nest or one sheet spec.

    ``status`` is ``matched`` (one part identified, ``candidate`` set), ``ambiguous`` (a
    shortlist in ``candidates``, nothing chosen) or ``unmatched`` (no part survived the
    thickness / grade gates). ``diagnostic`` is the matcher's sentence explaining a refusal.
    """

    status: Literal["matched", "ambiguous", "unmatched"]
    candidate: Optional[SheetCandidate] = Field(
        None, description="The matcher's chosen part; set only when status is 'matched'"
    )
    candidates: List[SheetCandidate] = Field(default_factory=list, description="Shortlist, best first (max 5)")
    diagnostic: Optional[str] = None


class ProposedNestTie(UTCModel):
    """The body for ``POST /work-orders/{work_order_id}/material-allocations``.

    The same shape the nest import creates: an OPERATION-scoped ``nest`` tie at one sheet
    per run, planned for the nest's planned runs, unpinned (FIFO at consume time).
    """

    work_order_id: int = Field(..., description="Path parameter for the tie endpoint (the nest's laser job)")
    part_id: int
    work_order_operation_id: int
    source: Literal["nest"] = "nest"
    qty_per_run: float = 1.0
    qty_planned: float


class UntiedNestRow(UTCModel):
    """One laser nest operation that carries no OPEN operation-scoped material tie."""

    laser_nest_id: int
    work_order_operation_id: int
    operation_number: Optional[str] = None
    operation_name: Optional[str] = None
    operation_status: str
    work_order_id: int
    work_order_number: str
    work_order_status: str
    parent_work_order_id: Optional[int] = None
    parent_work_order_number: Optional[str] = None
    work_center_id: Optional[int] = None
    work_center_name: Optional[str] = None
    nest_name: str
    cnc_number: Optional[str] = Field(None, description="The program number keyed on the laser")
    material: Optional[str] = None
    thickness: Optional[str] = None
    sheet_size: Optional[str] = None
    sheet_descriptor: str = Field(..., description="material / thickness / sheet size, normalized, for display")
    planned_runs: float
    runs_complete: float = Field(..., description="The operation's good count (one run = one sheet)")
    runs_scrapped: float
    completed_at: Optional[datetime] = Field(None, description="When the operation completed; null while open")
    is_completed: bool
    job_level_tie_count: int = Field(
        0,
        description=(
            "OPEN work-order-scoped ties on this nest's laser job. A job-level sheet tie already consumes at "
            "work-order completion, so tying the nest as well could draw the sheet twice -- check it first."
        ),
    )
    tieable: bool = Field(..., description="False when the tie endpoint would refuse (the laser job is finished)")
    tie_blocked_reason: Optional[str] = None
    suggestion: Optional[SheetSuggestionView] = Field(
        None, description="The sheet matcher's answer; null only when the matcher could not run"
    )
    proposed_tie: Optional[ProposedNestTie] = Field(
        None, description="Set only when the matcher identified one part AND the nest is tieable"
    )


class UntiedNestListResponse(UTCModel):
    items: List[UntiedNestRow]
    total: int = Field(..., description="Rows matching the filters, before limit/offset")
    limit: int
    offset: int
    completed_from: Optional[datetime] = Field(None, description="UTC start of the completed-nest window applied")
    completed_to: Optional[datetime] = Field(None, description="UTC exclusive end of the completed-nest window")
    matcher_available: bool = Field(True, description="False when the sheet matcher failed for this page")


class NestTieCoverageMonth(UTCModel):
    month: str = Field(..., description="Company-local calendar month, YYYY-MM")
    completed_nests: int
    tied_nests: int
    untied_nests: int
    tied_percent: Optional[float] = Field(None, description="tied / completed x 100, one decimal; null with none")


class NestTieCoverageResponse(UTCModel):
    months: List[NestTieCoverageMonth] = Field(..., description="Oldest first, one entry per month, zeros included")
    completed_nests: int
    tied_nests: int
    tied_percent: Optional[float] = None


class OpenCycleCountRef(UTCModel):
    id: int
    count_number: str
    status: str
    scheduled_date: Optional[date] = None


class UntiedSheetGroup(UTCModel):
    """Completed untied nests that read as one physical sheet spec."""

    spec_key: str = Field(..., description="Read-time grouping key (grade|thickness|size); approximate")
    material: Optional[str] = None
    thickness: Optional[str] = None
    sheet_size: Optional[str] = None
    sheet_descriptor: str
    nest_count: int
    sheets_burned: float = Field(..., description="Runs completed + scrapped across the group, one sheet per run")
    work_order_count: int
    work_order_numbers: List[str] = Field(..., description="Up to 10, most recent first")
    first_completed_at: Optional[datetime] = None
    last_completed_at: Optional[datetime] = None
    suggestion: Optional[SheetSuggestionView] = None
    open_cycle_counts: List[OpenCycleCountRef] = Field(
        default_factory=list,
        description="Scheduled / in-progress cycle counts already scoped to the matched part",
    )


class UntiedSheetReportResponse(UTCModel):
    groups: List[UntiedSheetGroup] = Field(..., description="Most sheets burned first")
    nest_count: int
    sheets_burned: float
    completed_from: Optional[datetime] = None
    completed_to: Optional[datetime] = None
    truncated: bool = Field(False, description="True when more completed untied nests existed than were read")
    matcher_available: bool = True


class WorkOrderNestTieRow(UTCModel):
    laser_nest_id: int
    work_order_operation_id: int
    work_order_id: int
    work_order_number: str
    operation_number: Optional[str] = None
    cnc_number: Optional[str] = None
    nest_name: str
    operation_status: str


class WorkOrderNestTieSummary(UTCModel):
    """How many of a job's laser nests have no sheet tied (its own nests plus laser child jobs')."""

    work_order_id: int
    work_order_number: str
    nest_count: int
    tied_count: int
    untied_count: int
    job_level_tie_count: int = Field(
        0, description="OPEN work-order-scoped ties on the laser job(s) these nests belong to"
    )
    message: Optional[str] = Field(None, description="'N of M nests have no sheet tied', or null when none")
    untied: List[WorkOrderNestTieRow] = Field(default_factory=list)
