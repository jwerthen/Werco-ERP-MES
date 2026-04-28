from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_current_company_id, get_current_user
from app.db.database import get_db
from app.models.notification import NotificationLog
from app.models.user import User, UserRole

router = APIRouter()


class NotificationLogResponse(BaseModel):
    id: int
    user_id: int
    event_type: str
    channel: str
    subject: Optional[str] = None
    body: Optional[str] = None
    sent: bool
    error: Optional[str] = None
    related_type: Optional[str] = None
    related_id: Optional[int] = None
    sent_at: Optional[datetime] = None

    class Config:
        from_attributes = True


@router.get("/logs", response_model=List[NotificationLogResponse])
def list_notification_logs(
    limit: int = Query(25, ge=1, le=100),
    status: Optional[str] = Query(None, pattern="^(sent|failed)$"),
    mine_only: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Return recent notification activity for the action inbox."""
    query = db.query(NotificationLog).filter(NotificationLog.company_id == company_id)

    if mine_only or current_user.role not in {UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR}:
        query = query.filter(NotificationLog.user_id == current_user.id)

    if status == "sent":
        query = query.filter(NotificationLog.sent == True)
    elif status == "failed":
        query = query.filter(NotificationLog.sent == False)

    return query.order_by(NotificationLog.sent_at.desc(), NotificationLog.id.desc()).limit(limit).all()
