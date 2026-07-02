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


class DisplayTokenResponse(UTCModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    label: str
    expires_at: datetime
    revoked: bool
    revoked_at: Optional[datetime] = None
    created_by: int
    created_at: datetime


class DisplayTokenIssueResponse(DisplayTokenResponse):
    """Returned ONLY from POST /auth/display-token — carries the one-time JWT."""

    token: str


class DisplayTokenListResponse(BaseModel):
    display_tokens: list[DisplayTokenResponse]
