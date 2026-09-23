"""Explicit, bounded proposals and durable receipts for Hank's first three actions."""

from datetime import date, datetime
from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.base import UTCModel
from app.schemas.hank_operations import DraftShipmentInput, ReceiveDeliveryInput, ReportProductionInput
from app.schemas.purchasing import POCreate, POLineCreate
from app.schemas.work_order import WorkOrderDuplicateRequest

HankActionKind = Literal[
    'repeat_job', 'draft_purchase_order', 'attach_document', 'receive_delivery', 'report_production', 'draft_shipment'
]
HankTaskKind = Literal[
    'repeat_job',
    'draft_purchase_order',
    'attach_document',
    'watch_work_order',
    'receive_delivery',
    'report_production',
    'draft_shipment',
]
HankTaskStatus = Literal['awaiting_review', 'completed', 'cancelled', 'needs_attention', 'watching', 'snoozed']


class RepeatJobInput(WorkOrderDuplicateRequest):
    model_config = ConfigDict(extra='forbid')
    source_work_order_id: int = Field(gt=0)


class DraftPOLine(POLineCreate):
    model_config = ConfigDict(extra='forbid')
    source_line_index: int | None = Field(default=None, ge=0, le=49)
    unit_of_measure: str | None = Field(default=None, min_length=1, max_length=50)


class DraftPurchaseOrderInput(POCreate):
    model_config = ConfigDict(extra='forbid')
    lines: list[DraftPOLine] = Field(min_length=1, max_length=50)
    source_intake_file_id: int | None = Field(default=None, gt=0)
    source_intake_version: int | None = Field(default=None, ge=1)
    po_number: str | None = Field(default=None, min_length=1, max_length=50)
    order_date: date | None = None
    ready_for_receiving: bool = False

    @field_validator("po_number")
    @classmethod
    def clean_po_number(cls, value):
        if value is not None:
            value = value.strip()
            if not value or any(ord(character) < 32 for character in value):
                raise ValueError("Enter a printable purchase order number")
        return value

    @model_validator(mode="after")
    def source_import_contract(self):
        if (self.source_intake_file_id is None) != (self.source_intake_version is None):
            raise ValueError("The intake source ID and version must be supplied together")
        if self.source_intake_file_id is None:
            if self.po_number is not None or self.order_date is not None or self.ready_for_receiving:
                raise ValueError("Imported order details require a reviewed intake source")
            if any(line.source_line_index is not None or line.unit_of_measure is not None for line in self.lines):
                raise ValueError('Imported line evidence requires a reviewed intake source')
        elif not self.po_number:
            raise ValueError("Review and enter the original purchase order number")
        elif any(line.source_line_index is None or not line.unit_of_measure for line in self.lines):
            raise ValueError("Each imported line requires its source index and reviewed stocking unit")
        return self


class AttachDocumentInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    document_id: int = Field(gt=0)
    work_order_id: int = Field(gt=0)


INPUT_SCHEMAS: dict[str, type[BaseModel]] = {
    'repeat_job': RepeatJobInput,
    'draft_purchase_order': DraftPurchaseOrderInput,
    'attach_document': AttachDocumentInput,
    'receive_delivery': ReceiveDeliveryInput,
    'report_production': ReportProductionInput,
    'draft_shipment': DraftShipmentInput,
}


class HankTaskCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_company_id: int = Field(gt=0)
    request_key: str
    kind: HankActionKind
    input: dict[str, Any]

    @field_validator('request_key')
    @classmethod
    def canonical_uuid(cls, value: str) -> str:
        return str(UUID(value))

    @model_validator(mode='after')
    def validate_action_input(self) -> 'HankTaskCreate':
        self.input = INPUT_SCHEMAS[self.kind].model_validate(self.input).model_dump(mode='json')
        if self.kind == 'draft_purchase_order' and self.input.get('source_intake_file_id') is None:
            # Existing retry keys hash the original POCreate shape. Newly optional
            # import metadata must not change a legacy manual command's identity.
            self.input = POCreate.model_validate(self.input).model_dump(mode='json')
        return self


class HankTaskCommand(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_company_id: int = Field(gt=0)
    expected_version: int = Field(ge=1)


class HankTaskReference(BaseModel):
    type: str
    id: int
    label: str
    url: str


class HankTaskPreview(BaseModel):
    summary: str
    changes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    references: list[HankTaskReference] = Field(default_factory=list)


class HankTaskResult(BaseModel):
    summary: str
    warnings: list[str] = Field(default_factory=list)
    references: list[HankTaskReference] = Field(default_factory=list)


class HankTaskResponse(UTCModel):
    id: int
    company_id: int
    kind: HankTaskKind
    title: str
    status: HankTaskStatus
    version: int
    input: dict[str, Any]
    preview: HankTaskPreview
    result: Optional[HankTaskResult] = None
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime] = None
    last_checked_at: Optional[datetime] = None
    snoozed_until: Optional[datetime] = None


class HankTaskList(BaseModel):
    tasks: list[HankTaskResponse]
    has_more: bool
    next_before_id: Optional[int] = None


class HankCapabilities(BaseModel):
    company_id: int
    allowed_kinds: list[HankActionKind]
    can_write: bool
    can_watch: bool = False
