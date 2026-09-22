"""Typed personal presentation choices; never authority or production policy."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.base import UTCModel


class HankPreferenceValues(BaseModel):
    model_config = ConfigDict(extra='forbid')
    briefing_detail: Literal['concise', 'standard'] = 'standard'
    focus_area: Literal['role_default', 'my_work', 'shop', 'quality', 'purchasing', 'inventory', 'shipping'] = (
        'role_default'
    )
    handoff_format: Literal['bullets', 'checklist'] = 'bullets'
    follow_up_alerts: bool = True


class HankPreferenceCommand(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_company_id: int = Field(gt=0)
    expected_version: int = Field(ge=0)


class HankPreferenceSave(HankPreferenceCommand):
    preferences: HankPreferenceValues


class HankPreferenceResponse(UTCModel):
    company_id: int
    version: int
    preferences: HankPreferenceValues
    updated_at: datetime | None = None
    can_edit: bool
