"""Pydantic contracts for the visitor sign-in / sign-out log.

Visitor and host names are CUI/PII (see model docstring) and must never cross an
external boundary. These contracts only shape the internal API I/O.
"""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.visitor_log import VisitorPurpose, VisitorStatus


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


class VisitorLogResponse(BaseModel):
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


class VisitorLogListResponse(BaseModel):
    items: List[VisitorLogResponse]
    total: int


class VisitorSignOutMatch(BaseModel):
    """One open visitor row in a sign-out name-disambiguation list (no PII beyond company)."""

    id: int
    visitor_company: Optional[str] = None
    signed_in_at: datetime
