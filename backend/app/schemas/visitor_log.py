"""Pydantic contracts for the visitor sign-in / sign-out log.

Visitor and host names are CUI/PII (see model docstring) and must never cross an
external boundary. These contracts only shape the internal API I/O.
"""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.time_utils import ensure_utc
from app.models.visitor_log import VisitorPurpose, VisitorStatus
from app.schemas.base import UTCModel


class VisitorSignInRequest(BaseModel):
    visitor_name: str = Field(..., min_length=1, max_length=120)
    visitor_company: Optional[str] = Field(None, max_length=120)
    visitor_phone: Optional[str] = Field(None, max_length=40)
    host_name: Optional[str] = Field(None, max_length=120)
    purpose: VisitorPurpose
    purpose_note: Optional[str] = Field(None, max_length=255)
    safety_acknowledged: bool = Field(..., description="Safety/NDA acknowledgment — must be true to sign in")

    @field_validator("visitor_name", "visitor_company", "visitor_phone", "host_name", "purpose_note")
    @classmethod
    def _strip(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        return v or None

    @field_validator("visitor_name")
    @classmethod
    def _name_required(cls, v: Optional[str]) -> str:
        if not v:
            raise ValueError("visitor_name is required")
        return v

    @field_validator("safety_acknowledged")
    @classmethod
    def _must_acknowledge(cls, v: bool) -> bool:
        if not v:
            raise ValueError("safety_acknowledged must be accepted before signing in")
        return v

    @model_validator(mode="after")
    def _purpose_note_required_for_other(self) -> "VisitorSignInRequest":
        if self.purpose == VisitorPurpose.OTHER and not (self.purpose_note and self.purpose_note.strip()):
            raise ValueError("purpose_note is required when purpose is 'other'")
        return self


class VisitorManualEntryRequest(VisitorSignInRequest):
    """Staff back-entry of an offline visit, recorded with its ACTUAL times.

    Inherits every visitor-field rule from ``VisitorSignInRequest`` (strip,
    ``visitor_name`` required, ``safety_acknowledged`` must be true,
    ``purpose_note`` required when purpose is 'other') and adds the real sign-in
    / sign-out timestamps. Unlike the tablet sign-in (which stamps ``utcnow()``),
    an ADMIN/MANAGER supplies the true times for a paper-logged visit after a
    lobby-tablet outage — so ``signed_in_at`` is REQUIRED and both times must be
    in the PAST, and ``signed_out_at`` (if given) must be on or after
    ``signed_in_at``. Times are normalized to naive UTC to match the stored
    columns.
    """

    signed_in_at: datetime = Field(..., description="Actual sign-in time (UTC; must be in the past)")
    signed_out_at: Optional[datetime] = Field(
        None,
        description="Actual sign-out time (UTC; >= signed_in_at and in the past). Omit if still on-site.",
    )

    @field_validator("signed_in_at", "signed_out_at")
    @classmethod
    def _normalize_to_naive_utc(cls, v: Optional[datetime]) -> Optional[datetime]:
        # Store naive UTC to match the DB columns and datetime.utcnow() used
        # throughout the service. ensure_utc treats a zone-less value as UTC.
        dt = ensure_utc(v)
        return dt.replace(tzinfo=None) if dt is not None else None

    @model_validator(mode="after")
    def _validate_times(self) -> "VisitorManualEntryRequest":
        now = datetime.utcnow()
        if self.signed_in_at > now:
            raise ValueError("signed_in_at must be in the past")
        if self.signed_out_at is not None:
            if self.signed_out_at > now:
                raise ValueError("signed_out_at must be in the past")
            if self.signed_out_at < self.signed_in_at:
                raise ValueError("signed_out_at must be on or after signed_in_at")
        return self


class VisitorSignOutRequest(BaseModel):
    """Sign out by exact visitor_log_id (preferred, staff) OR by visitor name (tablet)."""

    name: Optional[str] = Field(None, max_length=120)
    visitor_log_id: Optional[int] = Field(None, gt=0)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        return v or None

    @model_validator(mode="after")
    def _one_required(self) -> "VisitorSignOutRequest":
        if self.visitor_log_id is None and not self.name:
            raise ValueError("Provide either visitor_log_id or name")
        return self


class VisitorLogResponse(UTCModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    visitor_name: str
    visitor_company: Optional[str] = None
    visitor_phone: Optional[str] = None
    host_name: Optional[str] = None
    host_user_id: Optional[int] = None
    purpose: VisitorPurpose
    purpose_note: Optional[str] = None
    safety_acknowledged: bool
    status: VisitorStatus
    signed_in_at: datetime
    signed_out_at: Optional[datetime] = None
    signin_station_id: Optional[int] = None
    station_label: Optional[str] = None
    # Present (non-null) iff this row was back-entered by staff after the fact,
    # not captured live at the tablet — lets the UI badge it as such.
    entered_by_user_id: Optional[int] = None


class VisitorLogListResponse(BaseModel):
    items: List[VisitorLogResponse]
    total: int


class VisitorSignOutMatch(UTCModel):
    """One open visitor row in a sign-out name-disambiguation list (no PII beyond company)."""

    id: int
    visitor_company: Optional[str] = None
    signed_in_at: datetime
