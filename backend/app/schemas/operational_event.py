from datetime import datetime
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class OperationalEventCreate(BaseModel):
    event_type: str = Field(..., min_length=2, max_length=80)
    source_module: str = Field(..., min_length=2, max_length=80)
    entity_type: Optional[str] = Field(None, max_length=80)
    entity_id: Optional[int] = Field(None, gt=0)
    work_order_id: Optional[int] = Field(None, gt=0)
    operation_id: Optional[int] = Field(None, gt=0)
    severity: str = Field("info", pattern="^(info|low|medium|high|critical)$")
    event_payload: Dict[str, Any] = Field(default_factory=dict)
    occurred_at: Optional[datetime] = None


class OperationalEventResponse(OperationalEventCreate):
    id: int
    company_id: int
    user_id: Optional[int] = None
    occurred_at: datetime
    created_at: datetime

    class Config:
        from_attributes = True
