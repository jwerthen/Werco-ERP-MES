from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.base import UTCModel

SourceKind = Literal[
    'late_work_order', 'blocker', 'low_stock', 'quality_ncr', 'overdue_po_line', 'mrp_shortage', 'supplier_follow_up'
]


class InboxAssignee(BaseModel):
    id: int
    name: str
    sources: list[SourceKind]


class OperationalInboxItem(UTCModel):
    key: str
    source_kind: SourceKind
    source_id: int
    occurrence: str
    title: str
    detail: str
    severity: Literal['high', 'medium', 'low']
    href: str
    suggested_action: str
    owner_id: Optional[int] = None
    owner_name: Optional[str] = None
    next_action: str = ''
    acknowledged: bool = False
    snoozed_until: Optional[datetime] = None
    version: int = 0
    can_manage: bool = False


class OperationalInboxResponse(UTCModel):
    items: list[OperationalInboxItem]
    assignees: list[InboxAssignee]
    checked_at: datetime
    truncated_sources: list[str] = Field(default_factory=list)


class OperationalInboxUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_version: int = Field(ge=0)
    occurrence: str = Field(min_length=64, max_length=64, pattern='^[a-f0-9]+$')
    owner_id: Optional[int] = Field(default=None, gt=0)
    next_action: Optional[str] = Field(default=None, max_length=500)
    acknowledge: Optional[bool] = None
    snooze_hours: Optional[int] = Field(default=None, ge=0, le=168)

    @model_validator(mode='after')
    def valid_patch(self):
        actions = self.model_fields_set - {'expected_version', 'occurrence'}
        if not actions:
            raise ValueError('Choose an inbox action')
        for field in ('next_action', 'acknowledge', 'snooze_hours'):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f'{field} cannot be null')
        return self
