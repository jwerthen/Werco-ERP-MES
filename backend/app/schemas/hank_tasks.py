"""Explicit, bounded proposals and durable receipts for Hank's first three actions."""

from datetime import datetime
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


class DraftPurchaseOrderInput(POCreate):
    model_config = ConfigDict(extra='forbid')
    lines: list[DraftPOLine] = Field(min_length=1, max_length=50)


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
