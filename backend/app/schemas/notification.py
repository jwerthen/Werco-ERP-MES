"""Pydantic response contracts for the notification inbox + catalog APIs.

Response schemas inherit ``UTCModel`` so datetimes serialize as UTC ISO-8601 with a
trailing ``Z`` (store UTC, serve UTC, display Central).
"""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel

from app.core.pagination import PaginationMeta
from app.schemas.base import UTCModel


class NotificationResponse(UTCModel):
    id: int
    event_key: str
    severity: str
    title: str
    body: Optional[str] = None
    link: Optional[str] = None
    related_type: Optional[str] = None
    related_id: Optional[int] = None
    is_read: bool
    read_at: Optional[datetime] = None
    created_at: datetime


class NotificationListResponse(UTCModel):
    items: List[NotificationResponse]
    pagination: PaginationMeta


class UnreadCountResponse(BaseModel):
    count: int


class MarkAllReadResponse(BaseModel):
    updated: int


class CatalogEntryResponse(BaseModel):
    event_key: str
    label: str
    description: str
    category: str
    severity: str
    default_channels: List[str]
    mandatory_channel: Optional[str] = None
    sms_eligible: bool
