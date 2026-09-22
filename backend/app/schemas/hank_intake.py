"""Bounded extraction suggestions and explicit employee-reviewed filing contracts."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.document import DocumentType
from app.schemas.base import UTCModel

IntakeKind = Literal['purchase_order', 'vendor_quote', 'packing_slip', 'material_certificate', 'drawing', 'other']
IntakeStatus = Literal['queued', 'analyzing', 'awaiting_review', 'planned', 'completed', 'failed', 'cancelled']
FieldName = Literal[
    'document_number',
    'vendor_name',
    'customer_name',
    'po_number',
    'receipt_number',
    'packing_slip_number',
    'part_number',
    'work_order_number',
    'revision',
    'heat_number',
    'lot_number',
    'quantity',
    'unit_price',
    'total',
    'currency',
    'date',
    'due_date',
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class IntakeEvidence(StrictModel):
    page: int = Field(ge=1, le=25)
    excerpt: str = Field(min_length=1, max_length=400)


class IntakeField(StrictModel):
    name: FieldName
    value: str | None = Field(default=None, max_length=300)
    confidence: Literal['high', 'low', 'unknown'] = 'unknown'
    evidence: list[IntakeEvidence] = Field(default_factory=list, max_length=3)


class IntakeLine(StrictModel):
    description: str = Field(default='', max_length=500)
    part_number: str | None = Field(default=None, max_length=100)
    quantity: str | None = Field(default=None, max_length=100)
    unit_price: str | None = Field(default=None, max_length=100)
    lot_number: str | None = Field(default=None, max_length=100)
    heat_number: str | None = Field(default=None, max_length=100)
    confidence: Literal['high', 'low', 'unknown'] = 'unknown'
    evidence: list[IntakeEvidence] = Field(default_factory=list, max_length=3)


class IntakeExtraction(StrictModel):
    classification: IntakeKind
    confidence: Literal['high', 'low', 'unknown']
    summary: str = Field(max_length=1200)
    evidence: list[IntakeEvidence] = Field(default_factory=list, max_length=3)
    fields: list[IntakeField] = Field(default_factory=list, max_length=30)
    lines: list[IntakeLine] = Field(default_factory=list, max_length=50)
    warnings: list[str] = Field(default_factory=list, max_length=10)


class IntakeMatch(StrictModel):
    kind: Literal['part', 'work_order', 'vendor', 'purchase_order', 'receipt']
    id: int
    label: str
    href: str
    reason: str


class IntakeAnalysis(IntakeExtraction):
    matches: list[IntakeMatch] = Field(default_factory=list, max_length=25)
    has_duplicates: bool = Field(
        default=False, description='Matching content exists; private intake IDs remain hidden.'
    )
    duplicate_file_ids: list[int] = Field(default_factory=list, max_length=20)
    duplicate_document_ids: list[int] = Field(default_factory=list, max_length=20)


class IntakeCommand(StrictModel):
    expected_company_id: int = Field(gt=0)
    expected_version: int = Field(ge=1)


class IntakeReviewedField(StrictModel):
    name: FieldName
    value: str | None = Field(default=None, max_length=300)


class IntakePlanInput(StrictModel):
    filing_mode: Literal['draft', 'release_receipt_certificate'] = 'draft'
    title: str = Field(min_length=1, max_length=255)
    document_type: DocumentType
    revision: str = Field(default='A', min_length=1, max_length=20)
    description: str | None = Field(default=None, max_length=3000)
    part_id: int | None = Field(default=None, gt=0)
    work_order_id: int | None = Field(default=None, gt=0)
    vendor_id: int | None = Field(default=None, gt=0)
    purchase_order_id: int | None = Field(default=None, gt=0)
    receipt_id: int | None = Field(default=None, gt=0)
    reviewed_fields: list[IntakeReviewedField] = Field(default_factory=list, max_length=30)
    acknowledge_duplicate: bool = False


class IntakePlanCommand(IntakeCommand):
    plan: IntakePlanInput


class IntakePlanResponse(StrictModel):
    input: IntakePlanInput
    changes: list[str]
    warnings: list[str]
    references: list[IntakeMatch]


class IntakeReceipt(StrictModel):
    document_id: int
    document_number: str
    href: str
    summary: str
    references: list[IntakeMatch]
    warnings: list[str]


class IntakeFileResponse(UTCModel):
    id: int
    batch_id: int
    company_id: int
    filename: str
    file_size: int
    content_sha256: str
    page_count: int | None
    status: IntakeStatus
    version: int
    source_url: str
    analysis: IntakeAnalysis | None
    plan: IntakePlanResponse | None
    result: IntakeReceipt | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


class IntakeBatchResponse(UTCModel):
    id: int
    company_id: int
    request_key: str
    created_at: datetime
    files: list[IntakeFileResponse]


class IntakeBatchList(StrictModel):
    batches: list[IntakeBatchResponse]
    has_more: bool
    next_before_id: int | None
