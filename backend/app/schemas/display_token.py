"""Pydantic contracts for scoped TV-display tokens (A0.5 wallboard).

The raw JWT is returned exactly once — in ``DisplayTokenIssueResponse`` at
creation time. It is never stored server-side and never appears in the list
response, so a leaked listing cannot recover a usable token.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.base import UTCModel


class DisplayTokenCreate(BaseModel):
    label: str = Field(
        ..., min_length=1, max_length=100, description="Human label for the screen, e.g. 'North wall TV'"
    )
    expires_days: int = Field(
        90,
        ge=1,
        le=365,
        description="Token lifetime in days (default 90, capped at 365).",
    )
    dept: Optional[str] = Field(
        None,
        max_length=50,
        description="Optional work-center-type preset the TV opens with (e.g. 'machining').",
    )


class DisplayTokenResponse(UTCModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    label: str
    dept: Optional[str] = None
    expires_at: datetime
    revoked: bool
    revoked_at: Optional[datetime] = None
    created_by: int
    created_at: datetime


class DisplayTokenIssueResponse(DisplayTokenResponse):
    """Returned ONLY from POST /auth/display-token — carries the one-time JWT
    plus the short one-time TV setup code (15-min TTL, single-use)."""

    token: str
    setup_code: str
    setup_code_expires_at: datetime


class DisplayTokenListResponse(BaseModel):
    display_tokens: list[DisplayTokenResponse]


class SetupCodeReissueResponse(UTCModel):
    """POST /auth/display-token/{id}/setup-code — a fresh one-time pairing code.

    Shown once, like the issuance JWT; the previous code is dead the moment
    this response exists.
    """

    id: int
    label: str
    dept: Optional[str] = None
    setup_code: str
    setup_code_expires_at: datetime


class DisplayTokenClaimRequest(BaseModel):
    code: str = Field(
        ...,
        min_length=1,
        max_length=20,
        description="The 8-char setup code shown at issuance (case-, space- and dash-insensitive).",
    )


class DisplayTokenClaimResponse(UTCModel):
    """Returned from the PUBLIC POST /auth/display-token/claim — the wallboard
    display JWT (re-minted from the row; same revocation anchor)."""

    token: str
    label: str
    dept: Optional[str] = None
    expires_at: datetime
