import logging
from datetime import datetime
from typing import List, Optional

from sqlalchemy.orm import Session

from app.models.notification import DigestQueue
from app.models.user import User

logger = logging.getLogger(__name__)


class NotificationEvent:
    """Legacy notification event-type constants.

    Retained for backward compatibility: the daily-digest infrastructure and the
    visitor check-in host-email path still reference these string keys. New code drives
    notifications through the event catalog (``notification_catalog``) + the dispatcher
    (``notification_dispatch``), keyed by dot-notation ``event_key`` instead.
    """

    WO_RELEASED = "WO_RELEASED"
    WO_LATE = "WO_LATE"
    WO_BLOCKED = "WO_BLOCKED"
    WO_COMPLETED = "WO_COMPLETED"
    PO_RECEIVED = "PO_RECEIVED"
    INSPECTION_FAILED = "INSPECTION_FAILED"
    NCR_CREATED = "NCR_CREATED"
    LOW_STOCK = "LOW_STOCK"
    CALIBRATION_DUE = "CALIBRATION_DUE"
    QUOTE_EXPIRING = "QUOTE_EXPIRING"
    CAPACITY_OVERLOAD = "CAPACITY_OVERLOAD"
    VISITOR_CHECK_IN = "VISITOR_CHECK_IN"


class NotificationService:
    """Digest read/write helpers.

    The immediate-send + per-event dispatch responsibilities moved to
    ``notification_dispatch`` (the transactional-outbox pipeline). This service now only
    owns the digest queue read/write helpers used by the daily-digest job. NOTE: the old
    ``_get_user_preference`` auto-created a ``NotificationPreference`` WITHOUT
    ``company_id`` (a non-null TenantMixin column) -- an IntegrityError on Postgres (§9.8).
    That auto-create is intentionally gone: preferences are resolved in memory at dispatch
    time and a row is persisted only when a user explicitly saves.
    """

    def __init__(self, db: Session):
        self.db = db

    def get_digest_items(self, user_id: int, since: datetime = None) -> List[DigestQueue]:
        """Get unprocessed digest items for user."""
        query = self.db.query(DigestQueue).filter(DigestQueue.user_id == user_id, DigestQueue.processed == False)

        if since:
            query = query.filter(DigestQueue.created_at >= since)

        return query.order_by(DigestQueue.created_at).all()

    def mark_digest_processed(self, items: List[DigestQueue]):
        """Mark digest items as processed."""
        for item in items:
            item.processed = True
        self.db.commit()


def get_notification_recipients(
    db: Session, *, role: Optional[str] = None, department: Optional[str] = None, company_id: int
) -> List[User]:
    """
    Get active users for a notification, tenant-scoped.

    Args:
        db: Database session
        role: Filter by role (e.g., 'supervisor', 'manager')
        department: Filter by department
        company_id: REQUIRED. Restrict to a single tenant's users. Tenant isolation is
            not optional for recipient resolution (invariant §8.3) -- there is no
            all-tenants mode.

    Returns:
        List of active User objects in the company matching the role/department filters.
    """
    query = db.query(User).filter(User.is_active == True, User.company_id == company_id)

    if role:
        query = query.filter(User.role == role)

    if department:
        query = query.filter(User.department == department)

    return query.all()
