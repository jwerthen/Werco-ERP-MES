"""Read contracts for Hank's deterministic, permission-scoped shift briefing."""

from datetime import datetime
from typing import Literal, Optional

from pydantic import Field

from app.schemas.base import UTCModel


class HankBriefingItem(UTCModel):
    key: str = Field(max_length=100)
    source_kind: str = Field(max_length=50)
    source_id: int
    title: str = Field(max_length=300)
    detail: str = Field(max_length=1200)
    severity: Literal['high', 'medium', 'low']
    href: str = Field(max_length=250)
    suggested_action: str = Field(max_length=500)
    owner_name: Optional[str] = Field(default=None, max_length=210)
    is_mine: bool = False


class HankBriefingSection(UTCModel):
    key: str
    title: str
    description: str
    # Count of matching signals collected. It is a lower bound if a source was
    # capped; truncated also marks a section with more than five matching items.
    total: int = Field(ge=0)
    truncated: bool = False
    items: list[HankBriefingItem] = Field(max_length=5)


class HankBriefingResponse(UTCModel):
    checked_at: datetime
    role: str
    headline: str
    summary: str
    sections: list[HankBriefingSection]
    coverage_notes: list[str]
