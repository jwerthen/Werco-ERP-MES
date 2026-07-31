"""Pydantic response contracts for the notification inbox + catalog APIs.

Response schemas inherit ``UTCModel`` so datetimes serialize as UTC ISO-8601 with a
trailing ``Z`` (store UTC, serve UTC, display Central).
"""

from datetime import datetime
from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

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


# ---------------------------------------------------------------------------
# Self-service notification preferences
#
# PR 4 slice: the SMS channel only. The PERSISTED JSON keeps the full
# ``{in_app, email, sms, digest}`` shape per event so PR 3's complete matrix extends
# this without a migration; the REQUEST model accepts only ``sms`` and forbids extra
# keys, so a PR-3-shaped payload fails loudly (422) instead of silently dropping the
# channels this endpoint does not yet own.
# ---------------------------------------------------------------------------


class NotificationChannelUpdate(BaseModel):
    """Per-event channel changes. PR 4 scope: ``sms`` only."""

    model_config = ConfigDict(extra="forbid")

    sms: bool = Field(..., description="Deliver this event over SMS (SMS-eligible events only)")


class NotificationPreferencesUpdate(BaseModel):
    """Body of ``PUT /users/me/notification-preferences`` (self-scoped)."""

    model_config = ConfigDict(extra="forbid")

    preferences: Dict[str, NotificationChannelUpdate] = Field(
        ...,
        max_length=200,
        description="Map of catalog event_key -> channel changes",
    )


class NotificationPreferencesResponse(BaseModel):
    """Effective notification preferences for the current user.

    ``preferences`` is the RESOLVED per-event channel map the dispatcher would apply
    right now (catalog defaults where the user has saved nothing, plus any mandatory
    channel forced on) — not the raw stored row, so the UI can never disagree with
    what actually gets sent.
    """

    preferences: Dict[str, Dict[str, bool]]
    has_saved_preferences: bool
    phone: Optional[str] = None
    sms_egress_enabled: bool = False
    sms_configured: bool = False


class TestSMSResponse(BaseModel):
    """Result of ``POST /users/me/test-sms``."""

    status: str
    sid: Optional[str] = None
    provider_status: Optional[str] = None
    detail: str
