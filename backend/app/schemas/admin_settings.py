from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field, StrictInt

from app.schemas.base import UTCModel

# ============ WORK CENTER RATES ============


class WorkCenterRateUpdate(BaseModel):
    hourly_rate: float


class WorkCenterRateResponse(BaseModel):
    id: int
    code: str
    name: str
    work_center_type: str
    hourly_rate: float
    is_active: bool

    class Config:
        from_attributes = True


# ============ WORK CENTER TYPES ============


class WorkCenterTypesUpdate(BaseModel):
    types: List[str]


class WorkCenterTypesResponse(BaseModel):
    types: List[str]
    in_use: List[str] = []


# ============ SETTINGS ============


class EmailRecipientsUpdate(BaseModel):
    # null restores defaults; [] explicitly disables this email.
    user_ids: Optional[List[StrictInt]] = Field(..., max_length=2000)


class EmailRecipientUser(BaseModel):
    id: int
    name: str
    email: str
    is_active: bool
    email_deliverable: bool


class EmailRecipientEvent(BaseModel):
    event_key: str
    label: str
    description: str
    category: str
    user_ids: Optional[List[int]]
    is_custom: bool
    missing_default_emails: List[str]


class EmailRecipientsResponse(BaseModel):
    events: List[EmailRecipientEvent]
    users: List[EmailRecipientUser]


# ============ AUDIT LOG ============


class AuditLogResponse(UTCModel):
    id: int
    entity_type: str
    entity_id: Optional[int] = None
    entity_name: Optional[str] = None
    action: str
    field_changed: Optional[str] = None
    old_value: Optional[str] = None
    new_value: Optional[str] = None
    changed_by: Optional[int] = None
    changed_at: datetime

    class Config:
        from_attributes = True


class AuditLogWithUser(AuditLogResponse):
    user_name: Optional[str] = None
