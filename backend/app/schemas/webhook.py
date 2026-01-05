from pydantic import BaseModel, HttpUrl
from typing import Optional, List
from datetime import datetime


class WebhookBase(BaseModel):
    name: str
    url: str
    events: List[str]
    description: Optional[str] = None


class WebhookCreate(WebhookBase):
    secret: str


class WebhookUpdate(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    events: Optional[List[str]] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None


class WebhookResponse(WebhookBase):
    id: int
    is_active: bool
    failed_deliveries: int
    last_failure: Optional[datetime] = None
    created_at: datetime
    created_by: Optional[str] = None

    class Config:
        from_attributes = True


class WebhookDeliveryResponse(BaseModel):
    id: int
    webhook_id: int
    event: str
    payload: dict
    attempt: int
    status_code: Optional[int] = None
    error: Optional[str] = None
    delivered: bool
    sent_at: datetime
    delivered_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class WebhookTestPayload(BaseModel):
    test_data: dict = {"message": "Test webhook delivery"}
