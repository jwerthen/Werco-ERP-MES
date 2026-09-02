"""Pydantic contracts for long-lived, per-user API tokens.

The raw JWT is returned exactly once -- in ``ApiTokenIssueResponse`` at
creation time. It is never stored server-side and never appears in the list
or revoke responses, so a leaked listing cannot recover a usable token; the
only handle on a row's identity is ``jti_prefix`` (the first eight characters
of the JWT id, which mints nothing without ``SECRET_KEY``).
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.base import UTCModel

MAX_API_TOKEN_EXPIRES_DAYS = 3650


class ApiTokenCreate(BaseModel):
    user_id: int = Field(..., gt=0, description="The user this token acts AS. It carries exactly that user's role.")
    label: str = Field(
        ..., min_length=1, max_length=100, description="Human label for the holder, e.g. 'Werco Assistant - Grok Bot'."
    )
    expires_days: Optional[int] = Field(
        None,
        ge=1,
        le=MAX_API_TOKEN_EXPIRES_DAYS,
        description="Lifetime in days (1..3650). Omit for a token that never expires -- the default for a standing bot.",
    )

    @field_validator("label")
    @classmethod
    def _label_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("label must not be blank")
        return stripped


class ApiTokenRevoke(BaseModel):
    reason: str = Field(
        ...,
        min_length=3,
        max_length=255,
        description="Why the token is being revoked -- recorded on the row and audited.",
    )

    @field_validator("reason")
    @classmethod
    def _reason_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if len(stripped) < 3:
            raise ValueError("reason must be at least 3 characters")
        return stripped


class ApiTokenResponse(UTCModel):
    """Metadata only -- no secret, ever."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    label: str
    user_id: int
    jti_prefix: str
    expires_at: Optional[datetime] = None
    revoked: bool
    revoked_at: Optional[datetime] = None
    revoked_by: Optional[int] = None
    revoke_reason: Optional[str] = None
    last_used_at: Optional[datetime] = None
    created_by: int
    created_at: datetime


class ApiTokenIssueResponse(ApiTokenResponse):
    """Returned ONLY from ``POST /api-tokens/`` -- carries the one-time JWT."""

    token: str


class ApiTokenListResponse(BaseModel):
    api_tokens: list[ApiTokenResponse]
