import json
from datetime import date, datetime
from decimal import Decimal
from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator, model_validator

from app.core.time_utils import to_utc_iso
from app.core.validation import (
    DescriptionLong,
    Money,
    MoneySmall,
    OperationNumber,
)
from app.models.work_order import OperationStatus, WorkOrderStatus, WorkOrderType
from app.schemas.base import UTCModel


def _serialize_decimal_as_number(value: Optional[Decimal]) -> Optional[float]:
    if value is None:
        return None
    return float(value)


class QualityExceptionInfo(BaseModel):
    """One unsatisfied quality gate / data-quality signal on a completion response.

    WARN-AND-RECORD posture: the presence of these in a completion response means the
    operation / work order completed while a quality gate was unsatisfied. Completion
    still SUCCEEDED; the warning is here so the client can show it and the bypass is
    also recorded in the tamper-evident audit trail. Backward-compatible: every
    completion response defaults this to an empty list, so an all-clear completion is
    indistinguishable from the pre-Batch-4 shape.

    ``code`` values: ``inspection_incomplete``, ``open_ncr``, ``fai_not_passed``,
    ``open_blocker`` (Batch 4 / rank 7 quality gates), and ``no_labor_recorded``
    (Batch 7 / rank 10 data-quality signal: an operation completed with zero recorded
    labor, so cost/hour actuals may be understated -- fires regardless of the
    ``LABOR_COST_ROLLUP_ENABLED`` flag).
    """

    code: str
    message: str
    reference_type: str
    reference_id: Optional[int] = None
    severity: Optional[str] = None


class WorkOrderOperationBase(UTCModel):
    work_center_id: int = Field(..., gt=0, description="Work center ID")
    sequence: int = Field(
        ...,
        ge=10,
        le=990,
        multiple_of=10,
        description="Sequence (10-990, multiples of 10)",
    )
    operation_number: Optional[OperationNumber] = None
    name: str = Field(..., min_length=2, max_length=255, description="Operation name")
    description: Optional[DescriptionLong] = None
    setup_instructions: Optional[str] = Field(None, max_length=5000)
    run_instructions: Optional[str] = Field(None, max_length=5000)
    setup_time_hours: MoneySmall = Field(default=Decimal("0.0"), ge=Decimal("0"))
    run_time_hours: Money = Field(default=Decimal("0.0"), ge=Decimal("0"))
    run_time_per_piece: MoneySmall = Field(default=Decimal("0.0"), ge=Decimal("0"))
    requires_inspection: bool = False
    inspection_type: Optional[str] = Field(None, max_length=100)
    component_part_id: Optional[int] = Field(None, gt=0)
    component_quantity: Optional[float] = Field(None, ge=0)
    operation_group: Optional[str] = Field(None, max_length=50)


class LaserNestOperationInfo(BaseModel):
    id: int
    nest_name: str
    # NULLABLE: manual nests have no uploaded CNC file (cnc_file_name IS NULL).
    cnc_file_name: Optional[str] = None
    cnc_file_path: Optional[str] = None
    # Operator-/machine-facing program number (manual + imported).
    cnc_number: Optional[str] = None
    planned_runs: int
    completed_runs: float
    remaining_runs: float = 0.0
    material: Optional[str] = None
    thickness: Optional[str] = None
    sheet_size: Optional[str] = None
    # Optional reference PDF attached via the Document model. has_document /
    # document_file_name are NOT ORM columns -- they are injected as in-memory
    # attrs on the nest in the work-order enrich step before validation.
    document_id: Optional[int] = None
    has_document: bool = False
    document_file_name: Optional[str] = None

    class Config:
        from_attributes = True


class LaserNestManualCreate(BaseModel):
    """Request body for manually keying one laser nest onto an assembly WO."""

    cnc_number: str = Field(..., min_length=1, max_length=100, description="CNC program number")
    planned_runs: int = Field(..., ge=1, description="Planned sheet runs")
    nest_name: Optional[str] = Field(None, max_length=255)
    material: Optional[str] = Field(None, max_length=100)
    thickness: Optional[str] = Field(None, max_length=50)
    sheet_size: Optional[str] = Field(None, max_length=100)
    material_part_id: Optional[int] = Field(
        None,
        gt=0,
        description="Optional material tie: the stock part (sheet/plate) this nest consumes. Creates an "
        "operation-scoped material allocation on the nest's operation, so the material is deducted when "
        "the nest's operation completes; the work-order completion reconcile is the self-heal. Must "
        "resolve to a non-deleted MATERIAL part in this company: an unknown, cross-tenant or "
        "soft-deleted id is 404, and a part the shop produces (manufactured / assembly) is 422.",
    )
    qty_per_run: Optional[float] = Field(
        None,
        gt=0,
        description="Material consumed per completed run (e.g. sheets per nest run). Defaults to 1.0 when "
        "``material_part_id`` is supplied without it; requires ``material_part_id``.",
    )

    @model_validator(mode="after")
    def _validate_material_tie(self) -> "LaserNestManualCreate":
        # A per-run quantity with nothing to consume is meaningless -- and silently
        # dropping it would let a planner believe material will deplete when no tie
        # exists at all. Refuse at the data boundary (Pydantic ValueError -> 422).
        if self.qty_per_run is not None and self.material_part_id is None:
            raise ValueError("qty_per_run requires material_part_id")
        return self


class LaserNestUpdate(BaseModel):
    """Partial update for a manual laser nest. All fields optional."""

    cnc_number: Optional[str] = Field(None, min_length=1, max_length=100)
    nest_name: Optional[str] = Field(None, max_length=255)
    planned_runs: Optional[int] = Field(None, ge=1)
    material: Optional[str] = Field(None, max_length=100)
    thickness: Optional[str] = Field(None, max_length=50)
    sheet_size: Optional[str] = Field(None, max_length=100)


class LaserNestAttachDocument(BaseModel):
    """Attach an already-uploaded PDF Document to a nest by id."""

    document_id: int = Field(..., gt=0)


class LaserNestManualResponse(BaseModel):
    """Compact response for create/patch/attach/detach on a manual nest.

    Carries the created nest id AND its backing operation (id + status) so the
    frontend can immediately render the nest as a clock-in-able operation.
    """

    id: int
    nest_name: str
    cnc_number: Optional[str] = None
    planned_runs: int
    completed_runs: float
    remaining_runs: float = 0.0
    material: Optional[str] = None
    thickness: Optional[str] = None
    sheet_size: Optional[str] = None
    work_order_operation_id: Optional[int] = None
    operation_status: Optional[OperationStatus] = None
    document_id: Optional[int] = None
    has_document: bool = False
    document_file_name: Optional[str] = None


class LaserNestPdfExtractionResponse(BaseModel):
    """Result of auto-extracting nest fields from a single laser-nest report PDF.

    Stateless single-PDF extract endpoint contract. ``confidence`` is the overall
    extraction confidence ("high" | "medium" | "low"), mapped from the extraction
    service's ``extraction_confidence`` key. ``source`` is "ai" or "filename"
    (the latter when the model could not pin the CNC number and the filename stem
    was used as the fallback). ``warning`` is None on success.
    """

    cnc_number: Optional[str] = None
    material: Optional[str] = None
    thickness: Optional[str] = None
    sheet_size: Optional[str] = None
    planned_runs: Optional[int] = None
    confidence: Optional[str] = None
    source: str
    warning: Optional[str] = None
    # Number of AI reads behind the result: 2 when the independent verification
    # pass ran and was merged, 1 when it was skipped (or the extraction degraded).
    passes: Optional[int] = None


class SheetMatchDiagnostic(UTCModel):
    """One machine-readable reason the matcher wants the planner to see.

    ``severity`` is ``gate`` (something the matcher REFUSED on -- a thickness that
    could not be read, an alloy that disagreed) or ``advisory`` (something true but
    not disqualifying -- short stock, a truncated catalog, a same-family alternate).
    A ``gate`` diagnostic is why a row is not pre-filled; an ``advisory`` one can sit
    on a row that IS pre-filled.

    Carries NO datetime by construction (see ``SheetPartSuggestion``).
    """

    code: str
    severity: str
    detail: str = Field(..., max_length=300)


class SheetPartCandidate(UTCModel):
    """One stock part the matcher believes could be this nest's sheet.

    ``part_number`` is a PLAIN ``str``, deliberately NOT the ``PartNumber``
    annotated type used on the parts contracts: the matcher reads the ORM row
    directly (``_load_catalog``), so it can legitimately surface a part number
    with a space or an inch mark in it -- real stock that ``materials.py``'s
    ``_part_to_response`` would refuse to serialize. Constraining it here would
    500 the preview on exactly the rows the shortlist exists to reveal.

    The matcher's internal ``alloy_score`` is NOT part of this contract and is
    dropped before serialization -- it is the raw agreement weight behind
    ``score``, not something a planner can act on.
    """

    part_id: int
    part_number: str
    part_name: str
    unit_of_measure: Optional[str] = None
    score: float = Field(..., ge=0.0, le=100.0, description="0-100 confidence; 60 is the shortlist floor.")
    on_hand: float = 0.0
    on_hand_known: bool = Field(
        True,
        description="False when the on-hand read failed; the stock annotation is then unknown, not zero.",
    )
    demand: float = Field(0.0, description="Sheets this nest row would draw (qty_per_run x planned_runs).")
    projected_on_hand: float = Field(
        0.0,
        description="On hand minus this package's cumulative demand on the part, so two nests sharing a sheet stack.",
    )
    stock_state: str = Field(
        "unknown",
        description="``covered`` | ``short`` | ``none`` | ``unknown``. Annotates; never ranks.",
    )
    spec_thickness: Optional[str] = None
    spec_sheet_size: Optional[str] = None
    is_sheet_like: bool = True
    prior_tie_count: int = Field(
        0,
        description="How many times planners tied this spec to this part inside the history window. Corroboration only.",
    )
    reason: str = Field(..., min_length=1, max_length=300, description="One sentence a planner can check by eye.")
    basis: str = Field(
        ...,
        description=(
            "How this candidate reached its position: ``deterministic`` (the spec gates alone), "
            "``history`` (promoted because planners have repeatedly tied this exact spec to it), or "
            "``ai_disambiguated`` (promoted by the AI resolver inside an already-gated ambiguous "
            "shortlist). Neither of the latter two can ADMIT a candidate -- they only re-order and "
            "annotate what the deterministic gates already let through -- and neither can set "
            "``auto_fill_part_id``. Left a bare ``str``, not an enum: a client that narrowed it would "
            "stop compiling against a truthful response the day a fourth basis is added."
        ),
    )
    diagnostics: List[SheetMatchDiagnostic] = Field(default_factory=list)


class SheetPartSuggestion(UTCModel):
    """The matcher's advisory answer for one nest row.

    NO DATETIME FIELD MAY BE ADDED TO THIS SCHEMA OR ITS CHILDREN. ``UTCModel``
    carries its ``Z``-suffixing ``json_encoders`` as Pydantic-v2 *model-level*
    config, and these schemas are nested inside ``LaserNestPreviewResponse``, a
    plain ``BaseModel`` -- a datetime here would not reliably serialize with the
    trailing ``Z`` this codebase requires (invariant: store UTC, serve UTC ``Z``,
    display Central). ``SheetPartCandidate.prior_tie_count`` carries the
    "planners have done this before" signal without a timestamp; keep it that way.
    """

    status: str = Field(
        ...,
        description=(
            "``matched`` (one part cleared the deterministic gate and is pre-filled), "
            "``ambiguous`` (a shortlist is offered, nothing pre-filled), or "
            "``unmatched`` (no candidate survived the hard thickness gate)."
        ),
    )
    auto_fill_part_id: Optional[int] = Field(
        None,
        gt=0,
        description=(
            "The one part the wizard pre-fills the sheet picker with, or null. "
            "ASSIGNED ONLY BY THE ``sheet_stock_matcher`` DETERMINISTIC GATE -- score "
            ">= 90, margin over the runner-up >= 8, a stated-and-agreeing alloy, and a "
            "sheet-like part. It is structurally unreachable from the AI resolver, "
            "which runs afterwards, may only re-rank and annotate an ``ambiguous`` "
            "shortlist, and never writes this field. "
            "PRE-FILL IS A PROPOSAL, NOT A DECISION: the tie it seeds drives real "
            "inventory depletion at operation completion into an as-built record that "
            "never auto-reverses, so the planner confirms it on Import. Nothing here "
            "commits a tie."
        ),
    )
    candidates: List[SheetPartCandidate] = Field(
        default_factory=list,
        max_length=5,
        description="Best-first shortlist, capped at MAX_CANDIDATES.",
    )
    diagnostic: Optional[str] = Field(
        None,
        max_length=300,
        description="One sentence explaining a non-``matched`` status, for the row's hint text.",
    )


class LaserNestPreviewRow(BaseModel):
    """One detected nest in a package-preview response.

    Backward-compatible with the existing CNC-program-file preview: every field
    except a sensible name has a default, so a CNC-file row (which carries only
    ``nest_name`` / ``cnc_file_name`` / ``planned_runs`` and the filename-inferred
    ``material`` / ``thickness`` / ``sheet_size``) still validates. The PDF path
    additionally populates ``cnc_number``, ``confidence`` (overall) and
    ``source_file`` (the PDF's relative path within the package, echoed back on
    import as the row key).
    """

    nest_name: str
    cnc_file_name: Optional[str] = None
    cnc_number: Optional[str] = None
    planned_runs: int = Field(
        1,
        description=(
            "Planned sheet runs. NON-OPTIONAL and floored at 1 by "
            "``_coerce_planned_runs``, so a nest whose run count could not be read "
            "and a nest that genuinely runs once are the SAME 1 here. "
            "``field_confidence['planned_runs'] == 'low'`` is the only thing that "
            "separates them -- consumers must read it before treating this as an "
            "extracted value. (The single-PDF ``POST /laser-nests/extract`` "
            "response is the one place that keeps a real null.)"
        ),
    )
    material: Optional[str] = None
    thickness: Optional[str] = None
    sheet_size: Optional[str] = None
    confidence: Optional[str] = None
    # Always populated: every ``ParsedLaserNest.as_dict()`` sets ``source_file``
    # to the nest's relative path (PDF rel path for PDF nests, CNC file rel path
    # for CNC-file nests). It is the frontend's row-matching / React key, so it is
    # a required ``str`` to match the frontend's non-optional typing.
    source_file: str
    # Bare-multi-page-PDF extras; all default None so ZIP/CNC rows validate
    # unchanged. source_pages is the segment's 1-based page list in the original
    # upload (echoed back on import so the server can re-split deterministically);
    # field_confidence is the merged per-field confidence from the two-pass
    # extraction; warning surfaces a degraded/partially-verified extraction;
    # passes is the number of AI reads (1 or 2) behind the row.
    source_pages: Optional[List[int]] = None
    field_confidence: Optional[Dict[str, str]] = None
    warning: Optional[str] = None
    passes: Optional[int] = None
    # The sheet-stock matcher's ADVISORY answer for this row. None on every
    # non-preview construction of this schema (the import response echo builds
    # rows straight from ``ParsedLaserNest.as_dict()``), and None whenever
    # matching was skipped or degraded -- a preview never fails because matching
    # did. Never populated from planner input; it is server-derived, read-only,
    # and ties nothing.
    sheet_suggestion: Optional[SheetPartSuggestion] = None


class LaserNestImportRow(BaseModel):
    """One planner-confirmed nest row in the PDF confirm-and-commit import body.

    Validates the raw ``rows`` JSON before anything is persisted, so a negative /
    huge / non-numeric ``planned_runs`` or an over-long string is rejected with a
    clean 400 rather than reaching the DB as a 500 or poisoned data. Field
    constraints mirror ``LaserNestManualCreate`` (the manual single-nest path).

    ``source_file`` is the row key the wizard echoes back from the preview; it is
    resolved (with a path-traversal guard) to the PDF inside the re-sent package.
    """

    source_file: str = Field(..., min_length=1, max_length=1000)
    cnc_number: Optional[str] = Field(None, max_length=100)
    nest_name: Optional[str] = Field(None, max_length=255)
    planned_runs: int = Field(..., ge=1, description="Planned sheet runs")
    material: Optional[str] = Field(None, max_length=100)
    thickness: Optional[str] = Field(None, max_length=50)
    sheet_size: Optional[str] = Field(None, max_length=100)
    confidence: Optional[str] = Field(None, max_length=50)
    work_center_id: Optional[int] = Field(
        None,
        gt=0,
        description="Per-nest work-center override: this nest's operation is created on this work center "
        "instead of the package-level laser work center. Must resolve to an active work center.",
    )
    material_part_id: Optional[int] = Field(
        None,
        gt=0,
        description="Per-nest material tie: the stock part (sheet/plate) this nest consumes. Creates an "
        "operation-scoped material allocation on the nest's operation, so the material is deducted when "
        "the nest's operation completes; the work-order completion reconcile is the self-heal. Must "
        "resolve to a non-deleted MATERIAL part in this company: an unknown, cross-tenant or "
        "soft-deleted id is 404, and a part the shop produces (manufactured / assembly) is 422.",
    )
    qty_per_run: Optional[float] = Field(
        None,
        gt=0,
        description="Material consumed per completed run (e.g. sheets per nest run). Defaults to 1.0 when "
        "``material_part_id`` is supplied without it; requires ``material_part_id``.",
    )
    source_pages: Optional[List[int]] = Field(
        None,
        description=(
            "1-based page numbers of this nest's segment in the uploaded bare PDF "
            "(ascending, consecutive). Required for every row of a bare-PDF import; "
            "absent for ZIP-package rows."
        ),
    )

    @field_validator("source_pages")
    @classmethod
    def _validate_source_pages(cls, value: Optional[List[int]]) -> Optional[List[int]]:
        # The server re-splits the re-sent PDF by these page lists with NO AI
        # re-segmentation, so a malformed list must die here as a clean 400: a
        # non-empty, >=1, ascending-consecutive run is the only shape
        # ``split_pdf_segments`` (and its deterministic naming) accepts.
        if value is None:
            return value
        if not value:
            raise ValueError("source_pages must not be empty when provided")
        if any(page < 1 for page in value):
            raise ValueError("source_pages entries must be >= 1")
        if any(later != earlier + 1 for earlier, later in zip(value, value[1:])):
            raise ValueError("source_pages must be ascending and consecutive")
        return value

    @model_validator(mode="after")
    def _validate_material_tie(self) -> "LaserNestImportRow":
        # A per-run quantity with nothing to consume is meaningless -- and silently
        # dropping it would let a planner believe material will deplete when no tie
        # exists at all. Rows are validated before anything is persisted, so this is
        # a clean 400 on the import endpoint.
        if self.qty_per_run is not None and self.material_part_id is None:
            raise ValueError("qty_per_run requires material_part_id")
        return self


class WorkOrderOperationCreate(WorkOrderOperationBase):
    pass


class WorkOrderOperationUpdate(BaseModel):
    version: int = Field(..., ge=0, description="Version for optimistic locking")
    work_center_id: Optional[int] = Field(
        None,
        gt=0,
        description="Move the operation to another work center (planner reassignment; e.g. re-dispatching a "
        "laser nest to a different laser). Must be an active work center; refused while the operation is in "
        "progress or has an open time session.",
    )
    name: Optional[str] = Field(None, min_length=2, max_length=255)
    description: Optional[DescriptionLong] = None
    setup_instructions: Optional[str] = Field(None, max_length=5000)
    run_instructions: Optional[str] = Field(None, max_length=5000)
    setup_time_hours: Optional[Decimal] = Field(None, ge=Decimal("0"))
    run_time_hours: Optional[Decimal] = Field(None, ge=Decimal("0"))
    run_time_per_piece: Optional[Decimal] = Field(None, ge=Decimal("0"))
    status: Optional[OperationStatus] = None
    quantity_complete: Optional[Decimal] = Field(None, ge=Decimal("0"))
    quantity_scrapped: Optional[Decimal] = Field(None, ge=Decimal("0"))
    # max_length matches the WorkOrderOperation.scrap_reason String(255) column (migration 055).
    scrap_reason: Optional[str] = Field(
        None,
        max_length=255,
        description="Reason for scrapped parts; required when quantity_scrapped > 0, ignored otherwise.",
    )
    requires_inspection: Optional[bool] = None
    inspection_complete: Optional[bool] = None

    @model_validator(mode="after")
    def _require_scrap_reason(self) -> "WorkOrderOperationUpdate":
        # AS9100D defect-traceability invariant (compliance, not cosmetics): any scrapped
        # quantity MUST carry a reason. Enforced at the data boundary -- not just in the
        # office/admin UIs -- so a scripted/API client can't record reasonless scrap.
        # A blank/whitespace-only reason is treated as missing. Raised as a Pydantic
        # ValueError -> FastAPI returns 422. quantity_scrapped is Optional on this partial
        # update, so the ``is not None`` guard means an update that doesn't touch scrap is
        # never forced to supply a reason. scrap == 0 with no reason stays valid; negatives
        # are already rejected by the field's ge=0 constraint.
        if (
            self.quantity_scrapped is not None
            and self.quantity_scrapped > 0
            and not (self.scrap_reason and self.scrap_reason.strip())
        ):
            raise ValueError("scrap_reason is required when quantity_scrapped is greater than 0")
        return self


class WorkOrderOperationResponse(WorkOrderOperationBase):
    id: int
    version: Optional[int] = 0
    work_order_id: int
    description: Optional[str] = None  # Override to allow empty strings
    status: OperationStatus
    quantity_complete: MoneySmall
    quantity_scrapped: MoneySmall
    actual_setup_hours: MoneySmall
    actual_run_hours: Money
    estimated_hours: Optional[float] = None
    actual_hours: Optional[float] = None
    work_center_name: Optional[str] = None
    scheduled_start: Optional[datetime]
    scheduled_end: Optional[datetime]
    actual_start: Optional[datetime]
    actual_end: Optional[datetime]
    inspection_complete: bool
    created_at: datetime
    updated_at: datetime

    # Component tracking for assembly WOs
    component_part_id: Optional[int] = None
    component_part_number: Optional[str] = None
    component_part_name: Optional[str] = None
    component_quantity: Optional[float] = None
    operation_group: Optional[str] = None
    started_by: Optional[int] = None
    completed_by: Optional[int] = None
    laser_nest: Optional[LaserNestOperationInfo] = None

    @field_serializer(
        "setup_time_hours",
        "run_time_hours",
        "run_time_per_piece",
        "quantity_complete",
        "quantity_scrapped",
        "actual_setup_hours",
        "actual_run_hours",
        when_used="json",
    )
    def serialize_decimal_number(self, value: Optional[Decimal]) -> Optional[float]:
        return _serialize_decimal_as_number(value)

    @field_serializer(
        "scheduled_start",
        "scheduled_end",
        "actual_start",
        "actual_end",
        "created_at",
        "updated_at",
        when_used="json",
    )
    def serialize_utc_datetime(self, value: Optional[datetime]) -> Optional[str]:
        return to_utc_iso(value)

    class Config:
        from_attributes = True


class WorkOrderBase(UTCModel):
    part_id: int = Field(..., gt=0, description="Part ID")
    parent_work_order_id: Optional[int] = Field(None, gt=0)
    work_order_type: str = Field(default="production", max_length=50)
    quantity_ordered: MoneySmall = Field(..., gt=Decimal("0"), description="Quantity ordered")
    priority: int = Field(default=5, ge=1, le=10, description="Priority (1=highest, 10=lowest)")
    due_date: Optional[date] = Field(None, description="Due date")
    customer_name: Optional[str] = Field(None, max_length=255)
    customer_po: Optional[str] = Field(None, max_length=50, description="Customer PO number")
    notes: Optional[str] = Field(None, max_length=2000)
    special_instructions: Optional[str] = Field(None, max_length=2000)
    # 081. Defaults TRUE here, which is the CREATE-side default: a new work order is a
    # sequenced routing unless the caller says otherwise. This deliberately DISAGREES
    # with the column server_default (false) -- that one exists only to backfill rows
    # predating the column with the pooled behavior they were released under.
    sequential_operations: bool = Field(
        default=True,
        description="True = a sequenced ROUTING: an operation becomes READY only once every lower-sequence "
        "operation is COMPLETE, its own work center included. False = a DISPATCH POOL: "
        "operations sharing a work center are mutually startable and go READY together. "
        "Ignored on laser_cutting work orders, which are always pools.",
    )


class WorkOrderCreate(WorkOrderBase):
    operations: List[WorkOrderOperationCreate] = Field(default_factory=list)
    # PR 4 (process sheets): per-unit serial numbers for a serialized work order.
    # Stored to the existing JSON-in-Text ``WorkOrder.serial_numbers`` column; the
    # shop-floor capture endpoints then key step records per serial end-to-end.
    serial_numbers: Optional[List[str]] = Field(
        None,
        description="Serial numbers for a serialized work order — unique, non-empty, exactly one per unit "
        "(count must equal quantity_ordered). Omit for non-serialized work.",
    )

    @field_validator("work_order_type")
    @classmethod
    def validate_work_order_type(cls, value: str) -> str:
        """CREATE-side vocabulary gate (audit follow-up on B3).

        The column is a free string and ``create_work_order`` persists it verbatim, so
        without this an API client could mint a WO of an arbitrary type -- worst of
        all ``laser_cutting``: the FG-receipt and BOM-backflush skips key on exactly
        that value (``is_laser_dispatch_work_order``), so a hand-created
        'laser_cutting' WO with a real part and routed operations would silently lose
        its finished-goods receipt and backflush at completion. Nest-dispatch WOs are
        minted ONLY internally (``_ensure_laser_child_work_order`` and the nest import
        paths construct the ORM model directly, never this schema), so refusing the
        value here closes the API surface without touching the internal flow.
        Deliberately on WorkOrderCreate, not WorkOrderBase: WorkOrderResponse inherits
        the base and must keep serializing existing laser WOs.
        """
        try:
            wo_type = WorkOrderType(value)
        except ValueError:
            allowed = ", ".join(sorted(t.value for t in WorkOrderType))
            raise ValueError(f"work_order_type must be one of: {allowed}") from None
        if wo_type is WorkOrderType.LASER_CUTTING:
            raise ValueError(
                "work_order_type 'laser_cutting' cannot be set on create: laser nest-dispatch work orders "
                "are created only by the nest package import (POST /work-orders/laser-nest-packages/...)"
            )
        return value

    @model_validator(mode="after")
    def validate_dates(self) -> "WorkOrderCreate":
        """Validate date relationships on input"""
        today = date.today()

        if self.due_date and self.due_date < today:
            raise ValueError("Due date cannot be in the past")

        return self

    @model_validator(mode="after")
    def validate_serial_numbers(self) -> "WorkOrderCreate":
        """Serialized WO invariants: trimmed, non-empty, unique, count == quantity_ordered."""
        if self.serial_numbers is None:
            return self
        cleaned = [s.strip() if isinstance(s, str) else s for s in self.serial_numbers]
        if any(not s for s in cleaned):
            raise ValueError("serial_numbers entries must be non-empty strings")
        if any(len(s) > 100 for s in cleaned):
            raise ValueError("serial_numbers entries must be 100 characters or fewer")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("serial_numbers must be unique")
        if Decimal(len(cleaned)) != self.quantity_ordered:
            raise ValueError(
                f"serial_numbers count ({len(cleaned)}) must equal quantity_ordered ({self.quantity_ordered})"
            )
        self.serial_numbers = cleaned
        return self


class WorkOrderUpdate(BaseModel):
    version: int = Field(
        ...,
        ge=0,
        description=(
            "Optimistic-lock version: must equal the work order's current version "
            "(from any work-order response) or the update is rejected with 409."
        ),
    )
    quantity_ordered: Optional[Decimal] = Field(None, gt=Decimal("0"))
    priority: Optional[int] = Field(None, ge=1, le=10)
    status: Optional[WorkOrderStatus] = None
    # 081. Optional, so an update that never mentions sequencing never changes it.
    # Flipping False -> True also DEMOTES un-started READY operations the pooled rule
    # had promoted (see work_orders.update_work_order): the only write in the system
    # that moves an operation backwards, and it is audited per operation.
    sequential_operations: Optional[bool] = Field(
        default=None,
        description="True = a sequenced ROUTING: an operation becomes READY only once every lower-sequence "
        "operation is COMPLETE, its own work center included. False = a DISPATCH POOL: "
        "operations sharing a work center are mutually startable and go READY together. "
        "Ignored on laser_cutting work orders, which are always pools.",
    )
    due_date: Optional[date] = None
    customer_name: Optional[str] = Field(None, max_length=255)
    customer_po: Optional[str] = Field(None, max_length=50)
    notes: Optional[str] = Field(None, max_length=2000)
    special_instructions: Optional[str] = Field(None, max_length=2000)
    quantity_complete: Optional[Decimal] = Field(None, ge=Decimal("0"))
    quantity_scrapped: Optional[Decimal] = Field(None, ge=Decimal("0"))
    # max_length matches the WorkOrder.scrap_reason String(255) column (migration 055).
    scrap_reason: Optional[str] = Field(
        None,
        max_length=255,
        description="Reason for scrapped parts; required when quantity_scrapped > 0, ignored otherwise.",
    )

    @model_validator(mode="after")
    def _require_scrap_reason(self) -> "WorkOrderUpdate":
        # AS9100D defect-traceability invariant (compliance, not cosmetics): any scrapped
        # quantity MUST carry a reason. Enforced at the data boundary -- not just in the
        # office/admin UIs -- so a scripted/API client can't record reasonless scrap.
        # A blank/whitespace-only reason is treated as missing. Raised as a Pydantic
        # ValueError -> FastAPI returns 422. quantity_scrapped is Optional on this partial
        # update, so the ``is not None`` guard means an update that doesn't touch scrap is
        # never forced to supply a reason. scrap == 0 with no reason stays valid; negatives
        # are already rejected by the field's ge=0 constraint.
        if (
            self.quantity_scrapped is not None
            and self.quantity_scrapped > 0
            and not (self.scrap_reason and self.scrap_reason.strip())
        ):
            raise ValueError("scrap_reason is required when quantity_scrapped is greater than 0")
        return self


class WorkOrderDuplicateRequest(BaseModel):
    """Body for ``POST /work-orders/{id}/duplicate``.

    Only two things about a duplicate are the caller's to decide: how many, and by
    when. Everything else is copied from the source work order (see
    ``services/work_order_duplicate_service``) — which is the whole point, since the
    motivating case is re-running a 40-nest laser package without re-confirming it.

    The response is a ``WorkOrderDuplicateResponse`` ENVELOPE — the new work order under
    ``work_order`` (the same shape ``GET /work-orders/{id}`` returns, so the client can
    navigate straight to it) plus the things the copy had to leave behind. Read that
    schema's docstring before treating the envelope as a formality.

    ``due_date`` deliberately carries NO "not in the past" validator, unlike
    ``WorkOrderCreate``. That rule is planning hygiene for a job being planned; a
    duplicate is most often raised to re-run something that is ALREADY late, and
    refusing yesterday's date there would block the case the endpoint exists for.
    """

    quantity_ordered: MoneySmall = Field(
        ...,
        gt=Decimal("0"),
        description="Quantity ordered on the NEW work order. Not copied from the source — a duplicate is "
        "usually a re-run at a different quantity. Note that for a laser nest work order this does NOT "
        "rescale the copied nests: each nest keeps its own planned_runs, and the stored quantity is "
        "DERIVED from the sum of those runs, so it may differ from the value sent here. Read it back "
        "from the response rather than assuming this value was stored.",
    )
    due_date: Optional[date] = Field(
        None,
        description="Due date for the new work order. Null leaves it unset. The source's due date is never "
        "carried, and neither is its must_ship_by promise date.",
    )


class WorkOrderResponse(WorkOrderBase):
    id: int
    # READ-side relaxation: standalone laser-cutting nest WOs carry no part, so
    # part_id may be NULL on responses. WorkOrderCreate keeps the base's required
    # part_id -- part-less WOs are born only via the standalone nest import.
    part_id: Optional[int] = Field(None, description="Part ID (None for standalone laser-cutting work orders)")
    version: Optional[int] = Field(
        0,
        description="Optimistic-lock version counter; echo this value back in PUT /work-orders/{id}.",
    )
    work_order_number: str
    status: WorkOrderStatus
    quantity_complete: MoneySmall
    quantity_scrapped: MoneySmall
    scheduled_start: Optional[datetime]
    scheduled_end: Optional[datetime]
    actual_start: Optional[datetime]
    actual_end: Optional[datetime]
    estimated_hours: Money
    actual_hours: Money
    estimated_cost: Money
    actual_cost: Money
    operation_count: int = 0
    operations_complete: int = 0
    operation_progress_percent: float = 0.0
    created_at: datetime
    updated_at: datetime
    operations: List[WorkOrderOperationResponse] = Field(default_factory=list)
    # PR 4: serials on a serialized WO (parsed from the JSON-in-Text column; None
    # for non-serialized work). Read-only — set at creation via WorkOrderCreate.
    serial_numbers: Optional[List[str]] = None

    @field_validator("serial_numbers", mode="before")
    @classmethod
    def _parse_serial_numbers_json(cls, value):
        """ORM hands the raw JSON Text column through; parse it defensively."""
        if value is None or isinstance(value, list):
            return value
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, list) else None

    @field_serializer(
        "quantity_ordered",
        "quantity_complete",
        "quantity_scrapped",
        "estimated_hours",
        "actual_hours",
        "estimated_cost",
        "actual_cost",
        when_used="json",
    )
    def serialize_decimal_number(self, value: Optional[Decimal]) -> Optional[float]:
        return _serialize_decimal_as_number(value)

    @field_serializer(
        "scheduled_start",
        "scheduled_end",
        "actual_start",
        "actual_end",
        "created_at",
        "updated_at",
        when_used="json",
    )
    def serialize_utc_datetime(self, value: Optional[datetime]) -> Optional[str]:
        return to_utc_iso(value)

    class Config:
        from_attributes = True


class WorkOrderDuplicateSkippedOperation(UTCModel):
    """One source operation the duplicate deliberately did not copy.

    Built by ``services/work_order_duplicate_service`` AS THIS MODEL, inside the copy's
    transaction — not as a hand-rolled dict validated after the commit. A mistyped key is
    then a ``ValidationError`` that rolls the whole duplicate back, rather than a 500 on a
    work order that already exists, which is precisely the "the planner never sees the
    skip" outcome this envelope exists to prevent.

    ``extra="forbid"`` is what extends that guarantee to the OPTIONAL fields. A misspelled
    required field already fails as "field required"; without ``forbid``, a misspelled
    ``operation_number`` would be silently dropped and the entry would reach the planner
    naming nothing. Safe on a response model — this is only ever constructed server-side
    from keywords, never validated from client input.
    """

    model_config = ConfigDict(extra="forbid")

    source_operation_id: int = Field(..., description="Id of the operation on the SOURCE work order.")
    operation_number: Optional[str] = Field(None, description="The source operation's number, for display.")
    sequence: Optional[int] = Field(None, description="The source operation's sequence, for display.")
    reason: str = Field(
        ...,
        description="Machine-readable reason. Currently only 'laser_nest_deleted' — the operation's laser "
        "nest was soft-deleted, so copying it would put a nest task with no nest on the kiosk queue.",
    )


class WorkOrderDuplicateSkippedAllocation(UTCModel):
    """One source material tie the duplicate deliberately did not copy.

    Built by ``services/work_order_duplicate_service`` as this model, and sealed with
    ``extra="forbid"``, for the same reasons ``WorkOrderDuplicateSkippedOperation`` is.
    """

    model_config = ConfigDict(extra="forbid")

    source_allocation_id: int = Field(..., description="Id of the allocation row on the SOURCE work order.")
    part_id: int = Field(
        ...,
        description="The tied part, so the client can name it. Never null — "
        "``work_order_material_allocations.part_id`` is NOT NULL.",
    )
    source_work_order_operation_id: Optional[int] = Field(
        None,
        description="The source operation the tie was scoped to, or null for a work-order-scoped tie. Joins "
        "to skipped_operations when an operation skip is what caused this one.",
    )
    reason: str = Field(
        ...,
        description="Machine-readable reason. Three values are producible: 'part_not_available' (the tied part "
        "has been deleted), 'part_not_tieable' (the tied part is one the shop PRODUCES — a manufactured part "
        "or an assembly — which both live tie-write doors refuse 422; reachable only from a LEGACY tie created "
        "before that gate) and 'operation_not_copied' (its operation was skipped). A fourth, "
        "'nest_runs_unavailable', is kept server-side as DEFENCE and is not currently reachable — a "
        "nest-backed operation with no live nest is skipped first, so its tie reports 'operation_not_copied'. "
        "Treat the set as open and tolerate an unknown reason rather than switching on it exhaustively.",
    )


class WorkOrderDuplicateResponse(UTCModel):
    """Response for ``POST /work-orders/{id}/duplicate``: the new work order AND what it lost.

    The envelope exists because the two skip lists are SAFETY information, not telemetry,
    and ``WorkOrderResponse`` has nowhere to put them. The failing scenario is specific:
    the source's sheet part was soft-deleted since the source ran, so the material tie is
    skipped; the planner sees only "created as a draft", releases the laser work order
    believing it carries its material demand, no shortage shows, the nests run, and stock
    is never deducted. A skip that reaches only the audit chain is a skip nobody reads
    until the inventory count disagrees.

    Both lists are normally EMPTY, which is the "clean copy" signal — clients should say
    something when either is non-empty and stay quiet when both are. Neither list is an
    error: the work order was created and is a valid draft. Conditions the duplicate
    refuses outright (a retired produced part, a process-sheet family with no released
    revision) fail the whole call with a 409 instead and produce no response body.
    """

    work_order: WorkOrderResponse = Field(
        ...,
        description="The new DRAFT work order, in the same shape GET /work-orders/{id} returns.",
    )
    skipped_operations: List[WorkOrderDuplicateSkippedOperation] = Field(
        default_factory=list,
        description="Source operations not copied. Empty on a clean duplicate.",
    )
    skipped_material_allocations: List[WorkOrderDuplicateSkippedAllocation] = Field(
        default_factory=list,
        description="Source material ties not copied — the planner must re-tie these by hand or the job "
        "will run without the material demand the source had. Empty on a clean duplicate.",
    )


class WorkOrderRestoreSkippedAllocation(UTCModel):
    """One material tie ``POST /work-orders/{id}/restore`` deliberately left CANCELLED.

    The soft delete auto-CANCELs every OPEN tie and the restore is its inverse, so a tie
    that does NOT come back is an omission the planner has to know about — the same
    failure mode ``WorkOrderDuplicateSkippedAllocation`` exists for, reached from the other
    direction. Silence would mean the restored job runs, no shortage shows, and its
    material is never deducted until the inventory count disagrees.

    Built by ``material_consumption_service.reopen_allocations_cancelled_by_delete`` AS
    THIS MODEL, inside the restore's transaction, and sealed with ``extra="forbid"`` for
    the same reasons its duplicate-side sibling is: a mistyped key is then a
    ``ValidationError`` that rolls the restore back, rather than a 500 on a work order that
    is already restored — which is exactly the "the planner never sees the skip" outcome
    this envelope exists to prevent.

    Field names are the RESTORE's, not the duplicate's: nothing here is a "source" row.
    """

    model_config = ConfigDict(extra="forbid")

    allocation_id: int = Field(..., description="Id of the tie row that stayed `cancelled`.")
    part_id: int = Field(
        ...,
        description="The tied part, so the client can name it. Never null — "
        "``work_order_material_allocations.part_id`` is NOT NULL.",
    )
    work_order_operation_id: Optional[int] = Field(
        None,
        description="The operation the tie is scoped to, or null for a work-order-scoped tie.",
    )
    reason: str = Field(
        ...,
        description="Machine-readable reason. Currently only 'part_not_tieable' — the tied part is now one "
        "the shop PRODUCES (a manufactured part or an assembly), so re-opening the tie would re-arm standing "
        "demand that depletes finished goods; both live tie-write doors refuse such a part 422. "
        "Treat the set as open and tolerate an unknown reason rather than switching on it exhaustively.",
    )


class WorkOrderRestoreResponse(UTCModel):
    """Response for ``POST /work-orders/{id}/restore``: the message AND what did not come back.

    The endpoint used to return a bare ``{"message": ...}``. It grew an envelope for one
    reason: a restore is allowed to leave a tie CANCELLED (its part was reclassified into
    something the shop produces while the work order was deleted), and a dropped tie with
    no channel is the failure this system's whole tie-skip convention exists to prevent.
    ``message`` is unchanged and still first, so every existing caller keeps working.

    ``skipped_material_allocations`` is normally EMPTY, which is the "clean restore"
    signal — clients should say something when it is non-empty and stay quiet when it is
    not. It is not an error: the work order WAS restored.

    **It is not a complete inventory of every tie that stayed cancelled**, and saying so
    is more useful than implying otherwise. A tie the delete cancelled and a later nest
    re-import then DETACHED is also left alone (its operation no longer exists, so
    re-opening would silently convert it into a work-order-scoped tie nobody created) —
    that pre-existing case has no entry here. Read the list as "these ties were REFUSED,
    and why", not as "these are the only ties that did not come back".
    """

    message: str = Field(..., description="Human-readable confirmation, unchanged from the pre-envelope shape.")
    skipped_material_allocations: List[WorkOrderRestoreSkippedAllocation] = Field(
        default_factory=list,
        description="Ties the delete had cancelled that were deliberately NOT re-opened — the planner must "
        "correct the part class or re-tie by hand, or the job will run without that material demand. Empty "
        "on a clean restore.",
    )


class WorkOrderSummary(UTCModel):
    """Lightweight work order for lists/dashboards"""

    id: int
    work_order_number: str
    # None for standalone laser-cutting nest WOs (no part).
    part_id: Optional[int] = None
    parent_work_order_id: Optional[int] = None
    work_order_type: str = "production"
    # 081. Defaults False on this list shape (unlike WorkOrderBase, which is also the
    # CREATE contract): a summary is READ from an existing row, so the safe reading of a
    # missing value is the pooled behavior every pre-081 row was released under.
    sequential_operations: bool = False
    part_number: Optional[str] = None
    part_name: Optional[str] = None
    part_type: Optional[str] = None
    status: WorkOrderStatus
    priority: int
    quantity_ordered: MoneySmall
    quantity_complete: MoneySmall
    operation_count: int = 0
    operations_complete: int = 0
    operation_progress_percent: float = 0.0
    due_date: Optional[date]
    customer_name: Optional[str]
    current_operation: Optional[str] = None

    @field_serializer(
        "quantity_ordered",
        "quantity_complete",
        when_used="json",
    )
    def serialize_decimal_number(self, value: Optional[Decimal]) -> Optional[float]:
        return _serialize_decimal_as_number(value)

    class Config:
        from_attributes = True
