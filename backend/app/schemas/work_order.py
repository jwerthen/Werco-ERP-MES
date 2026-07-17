import json
from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel, Field, field_serializer, field_validator, model_validator

from app.core.time_utils import to_utc_iso
from app.core.validation import (
    DescriptionLong,
    Money,
    MoneySmall,
)
from app.models.work_order import OperationStatus, WorkOrderStatus
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
    operation_number: Optional[str] = Field(None, max_length=50)
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
    planned_runs: int = 1
    material: Optional[str] = None
    thickness: Optional[str] = None
    sheet_size: Optional[str] = None
    confidence: Optional[str] = None
    # Always populated: every ``ParsedLaserNest.as_dict()`` sets ``source_file``
    # to the nest's relative path (PDF rel path for PDF nests, CNC file rel path
    # for CNC-file nests). It is the frontend's row-matching / React key, so it is
    # a required ``str`` to match the frontend's non-optional typing.
    source_file: str


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


class WorkOrderOperationCreate(WorkOrderOperationBase):
    pass


class WorkOrderOperationUpdate(BaseModel):
    version: int = Field(..., ge=0, description="Version for optimistic locking")
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
    version: int = Field(..., ge=0, description="Version for optimistic locking")
    quantity_ordered: Optional[Decimal] = Field(None, gt=Decimal("0"))
    priority: Optional[int] = Field(None, ge=1, le=10)
    status: Optional[WorkOrderStatus] = None
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


class WorkOrderResponse(WorkOrderBase):
    id: int
    # READ-side relaxation: standalone laser-cutting nest WOs carry no part, so
    # part_id may be NULL on responses. WorkOrderCreate keeps the base's required
    # part_id -- part-less WOs are born only via the standalone nest import.
    part_id: Optional[int] = Field(None, description="Part ID (None for standalone laser-cutting work orders)")
    version: Optional[int] = 0
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


class WorkOrderSummary(UTCModel):
    """Lightweight work order for lists/dashboards"""

    id: int
    work_order_number: str
    # None for standalone laser-cutting nest WOs (no part).
    part_id: Optional[int] = None
    parent_work_order_id: Optional[int] = None
    work_order_type: str = "production"
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
