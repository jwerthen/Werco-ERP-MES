from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.schemas.base import UTCModel

DeliveryEntity = Literal['quote', 'purchase_order']
DeliveryStatus = Literal['prepared', 'sending', 'accepted', 'failed', 'unknown']


class DocumentDeliveryPreview(BaseModel):
    entity_type: DeliveryEntity
    entity_id: int = Field(gt=0)
    new_attempt: bool = False


class DocumentDeliverySend(BaseModel):
    expected_version: int = Field(ge=1)
    request_key: str = Field(min_length=8, max_length=100, pattern=r'^[A-Za-z0-9_-]+$')
    recipient: EmailStr = Field(max_length=320)
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=10000)

    @field_validator('subject')
    @classmethod
    def single_line_subject(cls, value):
        if '\r' in value or '\n' in value or not value.strip():
            raise ValueError('Subject must be one nonempty line')
        return value.strip()


class DocumentDeliveryReconcile(BaseModel):
    expected_version: int = Field(ge=1)
    outcome: Literal['accepted', 'failed']
    verification_note: str = Field(min_length=10, max_length=1000)

    @field_validator('verification_note')
    @classmethod
    def meaningful_note(cls, value):
        if len(value.strip()) < 10:
            raise ValueError('Describe how the mail-server outcome was verified')
        return value.strip()


class DocumentDeliveryResponse(UTCModel):
    id: int
    entity_type: DeliveryEntity
    entity_id: int
    document_number: str
    issue_date: Optional[date] = None
    recipient: str
    subject: str
    body: str
    attachment_name: str
    attachment_sha256: str
    attachment_size: int
    status: DeliveryStatus
    status_detail: Optional[str]
    version: int
    provider_message_id: Optional[str]
    created_at: datetime
    attempted_at: Optional[datetime]
    accepted_at: Optional[datetime]
    send_available: bool
    unavailable_reason: Optional[str]
    delivered: None = None  # SMTP acceptance cannot establish recipient delivery.
    replayed: bool = False
    manually_verified: bool = False
    verified_at: Optional[datetime] = None
    verification_note: Optional[str] = None
