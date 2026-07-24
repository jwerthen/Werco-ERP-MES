from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.deps import get_current_company_id, get_current_user
from app.core.pagination import paginate_query
from app.db.database import get_db
from app.models.notification import Notification, NotificationLog
from app.models.user import User, UserRole
from app.schemas.notification import (
    CatalogEntryResponse,
    MarkAllReadResponse,
    NotificationListResponse,
    NotificationResponse,
    UnreadCountResponse,
)
from app.services.notification_catalog import CATALOG

router = APIRouter()


# ---------------------------------------------------------------------------
# In-app inbox (self + tenant scoped)
# ---------------------------------------------------------------------------


@router.get("", response_model=NotificationListResponse)
def list_notifications(
    unread: Optional[bool] = Query(None, description="True=only unread, False=only read, omit=all"),
    category: Optional[str] = Query(None),
    severity: Optional[str] = Query(None, pattern="^(info|warning|critical)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Paged in-app notification inbox for the current user (self + tenant scoped)."""
    query = db.query(Notification).filter(
        Notification.company_id == company_id,
        Notification.user_id == current_user.id,
    )

    if unread is True:
        query = query.filter(Notification.is_read.is_(False))
    elif unread is False:
        query = query.filter(Notification.is_read.is_(True))

    if category:
        keys = [key for key, entry in CATALOG.items() if entry.category == category]
        # An unknown category yields no keys -> empty result (rather than "all rows").
        query = query.filter(Notification.event_key.in_(keys or [""]))

    if severity:
        query = query.filter(Notification.severity == severity)

    query = query.order_by(Notification.created_at.desc(), Notification.id.desc())
    paginated, meta = paginate_query(query, page=page, page_size=page_size, max_page_size=100)
    return NotificationListResponse(
        items=[NotificationResponse.model_validate(row) for row in paginated.all()],
        pagination=meta,
    )


@router.get("/unread-count", response_model=UnreadCountResponse)
def get_unread_count(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Cheap unread badge count for the current user (self + tenant scoped)."""
    count = (
        db.query(func.count(Notification.id))
        .filter(
            Notification.company_id == company_id,
            Notification.user_id == current_user.id,
            Notification.is_read.is_(False),
        )
        .scalar()
        or 0
    )
    return UnreadCountResponse(count=count)


@router.get("/catalog", response_model=List[CatalogEntryResponse])
def get_catalog(current_user: User = Depends(get_current_user)):
    """Return the notification event catalog for the settings matrix (all roles)."""
    return [
        CatalogEntryResponse(
            event_key=entry.event_key,
            label=entry.label,
            description=entry.description,
            category=entry.category,
            severity=entry.severity,
            default_channels=sorted(entry.default_channels),
            mandatory_channel=entry.mandatory_channel,
            sms_eligible=entry.sms_eligible,
        )
        for entry in CATALOG.values()
    ]


@router.post("/{notification_id}/read", response_model=NotificationResponse)
def mark_notification_read(
    notification_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Mark one notification read. NOT audited (UI state, not domain state)."""
    notification = (
        db.query(Notification)
        .filter(
            Notification.id == notification_id,
            Notification.company_id == company_id,
            Notification.user_id == current_user.id,
        )
        .first()
    )
    if notification is None:
        raise HTTPException(status_code=404, detail="Notification not found")

    if not notification.is_read:
        notification.is_read = True
        notification.read_at = datetime.utcnow()
        db.commit()
        db.refresh(notification)
    return NotificationResponse.model_validate(notification)


@router.post("/read-all", response_model=MarkAllReadResponse)
def mark_all_read(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Mark all of the current user's unread notifications read. NOT audited."""
    updated = (
        db.query(Notification)
        .filter(
            Notification.company_id == company_id,
            Notification.user_id == current_user.id,
            Notification.is_read.is_(False),
        )
        .update({Notification.is_read: True, Notification.read_at: datetime.utcnow()}, synchronize_session=False)
    )
    db.commit()
    return MarkAllReadResponse(updated=updated or 0)


# ---------------------------------------------------------------------------
# Delivery-attempt log (retained; admin-scoped hardening is PR 3)
# ---------------------------------------------------------------------------


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
    """Return recent notification delivery activity (email/SMS attempt log)."""
    query = db.query(NotificationLog).filter(NotificationLog.company_id == company_id)

    if mine_only or current_user.role not in {UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR}:
        query = query.filter(NotificationLog.user_id == current_user.id)

    if status == "sent":
        query = query.filter(NotificationLog.sent == True)
    elif status == "failed":
        query = query.filter(NotificationLog.sent == False)

    return query.order_by(NotificationLog.sent_at.desc(), NotificationLog.id.desc()).limit(limit).all()
