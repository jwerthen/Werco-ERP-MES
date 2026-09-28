"""Bounded suggestion commands and UTC activity receipts."""

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.base import UTCModel

ImprovementCategory = Literal[
    'poka_yoke',
    'five_s',
    'standard_work',
    'flow_layout',
    'quality',
    'safety_ergonomics',
    'setup_reduction',
    'equipment',
    'inventory',
    'other',
]
ImprovementStatus = Literal['new', 'under_review', 'approved', 'in_progress', 'implemented', 'on_hold', 'declined']
ImprovementPriority = Literal['low', 'medium', 'high']


class SuggestionCreate(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=200)
    problem: str = Field(min_length=1, max_length=10000)
    proposed_solution: str = Field(min_length=1, max_length=10000)
    expected_benefit: str = Field(min_length=1, max_length=10000)
    category: ImprovementCategory
    priority: ImprovementPriority = 'medium'
    area: str | None = Field(None, max_length=150)
    owner_id: int | None = Field(None, gt=0)
    target_date: date | None = None


class SuggestionUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    expected_version: int = Field(ge=1)
    title: str | None = Field(None, min_length=1, max_length=200)
    problem: str | None = Field(None, min_length=1, max_length=10000)
    proposed_solution: str | None = Field(None, min_length=1, max_length=10000)
    expected_benefit: str | None = Field(None, min_length=1, max_length=10000)
    category: ImprovementCategory | None = None
    priority: ImprovementPriority | None = None
    area: str | None = Field(None, max_length=150)
    owner_id: int | None = Field(None, gt=0)
    target_date: date | None = None
    status: ImprovementStatus | None = None
    implementation_notes: str | None = Field(None, max_length=10000)
    change_note: str | None = Field(None, max_length=5000)

    @model_validator(mode='after')
    def reject_null_required_fields(self):
        for field in ('title', 'problem', 'proposed_solution', 'expected_benefit', 'category', 'priority', 'status'):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f'{field} cannot be null')
        return self


class SuggestionComment(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    expected_version: int = Field(ge=1)
    body: str = Field(min_length=1, max_length=5000)


class SuggestionResponse(UTCModel):
    id: int
    company_id: int
    title: str
    problem: str
    proposed_solution: str
    expected_benefit: str
    category: ImprovementCategory
    priority: ImprovementPriority
    area: str | None
    status: ImprovementStatus
    owner_id: int | None
    owner_name: str | None
    target_date: date | None
    implementation_notes: str | None
    created_by: int
    created_by_name: str
    updated_by: int
    updated_by_name: str
    created_at: datetime
    updated_at: datetime
    reviewed_at: datetime | None
    implemented_at: datetime | None
    version: int


class ActivityResponse(UTCModel):
    id: int
    kind: Literal['submitted', 'updated', 'status_changed', 'comment']
    actor_id: int
    actor_name: str
    created_at: datetime
    body: str | None
    changes: dict[str, dict[str, Any]]


class SuggestionDetail(SuggestionResponse):
    history: list[ActivityResponse]


class SuggestionList(BaseModel):
    items: list[SuggestionResponse]
    total: int
    status_counts: dict[ImprovementStatus, int]


class MetadataOption(BaseModel):
    value: str
    label: str
    description: str | None = None


class OwnerOption(BaseModel):
    id: int
    name: str


class ImprovementMetadata(BaseModel):
    categories: list[MetadataOption]
    statuses: list[MetadataOption]
    priorities: list[MetadataOption]
    owners: list[OwnerOption]
    can_manage: bool
