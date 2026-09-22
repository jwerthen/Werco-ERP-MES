"""Typed handoffs and ordered procedures; procedure approval never grants ERP authority."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.base import UTCModel
from app.schemas.hank_tasks import HankTaskCommand, HankTaskReference

HandoffStatus = Literal['open', 'acknowledged', 'completed', 'cancelled']
RoutineStepKind = Literal[
    'readiness',
    'knowledge',
    'document_intake',
    'receive_delivery',
    'report_production',
    'shipping_packet',
    'draft_shipment',
    'purchasing_impact',
    'handoff',
    'checklist',
]


class IdentifiedCommand(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_company_id: int = Field(gt=0)
    request_key: str

    @field_validator('request_key')
    @classmethod
    def uuid_key(cls, value):
        return str(UUID(value))


class HandoffContent(BaseModel):
    model_config = ConfigDict(extra='forbid')
    summary: str = Field(min_length=1, max_length=1000)
    completed_work: str = Field(default='', max_length=3000)
    remaining_work: str = Field(default='', max_length=3000)
    problems: str = Field(default='', max_length=3000)
    quantity_remaining: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    document_ids: list[int] = Field(default_factory=list, max_length=10)

    @field_validator('summary')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('Enter a handoff summary')
        return value

    @field_validator('document_ids')
    @classmethod
    def document_keys(cls, values):
        if any(value <= 0 for value in values) or len(values) != len(set(values)):
            raise ValueError('Choose distinct positive document IDs')
        return values


class HandoffCreate(IdentifiedCommand, HandoffContent):
    work_order_id: int = Field(gt=0)
    recipient_id: int = Field(gt=0)


class HandoffPerson(BaseModel):
    id: int
    name: str


class HandoffCandidate(HandoffPerson):
    role: str


class HandoffPeople(BaseModel):
    people: list[HandoffCandidate]
    truncated: bool = False


class HandoffAttachment(BaseModel):
    id: str
    filename: str
    url: str
    mime_type: str


class HandoffResponse(UTCModel, HandoffContent):
    id: int
    company_id: int
    version: int
    status: HandoffStatus
    work_order_id: int
    work_order_number: str
    sender: HandoffPerson
    recipient: HandoffPerson
    attachments: list[HandoffAttachment]
    document_references: list[HankTaskReference]
    created_at: datetime
    updated_at: datetime
    acknowledged_at: datetime | None
    completed_at: datetime | None
    can_acknowledge: bool
    can_complete: bool
    can_cancel: bool


class HandoffList(BaseModel):
    handoffs: list[HandoffResponse]
    has_more: bool
    next_before_id: int | None


class RoutineStep(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: RoutineStepKind
    title: str = Field(min_length=1, max_length=160)
    instruction: str = Field(min_length=1, max_length=1000)


class RoutineValues(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(default='', max_length=2000)
    steps: list[RoutineStep] = Field(min_length=1, max_length=12)


class RoutineCreate(IdentifiedCommand, RoutineValues):
    pass


class RoutineUpdate(HankTaskCommand, RoutineValues):
    pass


class RoutineResponse(UTCModel, RoutineValues):
    id: int
    company_id: int
    version: int
    status: Literal['draft', 'approved', 'archived']
    created_by: int
    approved_by: int | None
    approved_at: datetime | None
    created_at: datetime
    updated_at: datetime
    can_manage: bool
    can_approve: bool


class RoutineList(BaseModel):
    routines: list[RoutineResponse]
    templates: list[RoutineValues]
    can_manage: bool
    can_approve: bool
    truncated: bool = False


class RoutineStart(IdentifiedCommand):
    expected_version: int = Field(ge=1)
    work_order_id: int | None = Field(default=None, gt=0)
    purchase_order_id: int | None = Field(default=None, gt=0)


class RoutineAdvance(HankTaskCommand):
    note: str = Field(default='', max_length=2000)
    task_id: int | None = Field(default=None, gt=0)
    intake_file_id: int | None = Field(default=None, gt=0)
    handoff_id: int | None = Field(default=None, gt=0)


class RoutineStepResult(UTCModel):
    step_index: int
    note: str
    completed_at: datetime
    evidence: list[HankTaskReference]


class RoutineRunResponse(UTCModel):
    id: int
    company_id: int
    routine_id: int
    routine_version: int
    title: str
    status: Literal['active', 'completed', 'cancelled']
    version: int
    current_step: int
    steps: list[RoutineStep]
    work_order_id: int | None
    purchase_order_id: int | None
    results: list[RoutineStepResult]
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    can_edit: bool


class RoutineRunList(BaseModel):
    runs: list[RoutineRunResponse]
    has_more: bool
    next_before_id: int | None


QueueState = Literal['working', 'waiting_on_you', 'waiting_on_other', 'finished']


class WorkQueueItem(UTCModel):
    key: str
    kind: Literal['task', 'handoff', 'routine', 'intake']
    id: int
    title: str
    state: QueueState
    status: str
    url: str
    updated_at: datetime


class WorkQueue(UTCModel):
    checked_at: datetime
    items: list[WorkQueueItem]
    truncated: bool
