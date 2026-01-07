from sqlalchemy.orm import Session
from typing import Dict, List, Optional
from app.models.notification import NotificationPreference, NotificationLog, DigestQueue
from app.models.user import User
from app.core.queue import enqueue_job
from datetime import datetime, timedelta
import logging

logger = logging.getLogger(__name__)


class NotificationEvent:
    """Notification event types"""
    WO_RELEASED = "WO_RELEASED"
    WO_LATE = "WO_LATE"
    WO_COMPLETED = "WO_COMPLETED"
    PO_RECEIVED = "PO_RECEIVED"
    INSPECTION_FAILED = "INSPECTION_FAILED"
    NCR_CREATED = "NCR_CREATED"
    LOW_STOCK = "LOW_STOCK"
    CALIBRATION_DUE = "CALIBRATION_DUE"
    QUOTE_EXPIRING = "QUOTE_EXPIRING"
    CAPACITY_OVERLOAD = "CAPACITY_OVERLOAD"


class NotificationService:
    """Notification dispatcher service"""

    # Default preferences for new users
    DEFAULT_PREFERENCES = {
        NotificationEvent.WO_RELEASED: {"email": True, "digest": False},
        NotificationEvent.WO_LATE: {"email": True, "digest": True},
        NotificationEvent.WO_COMPLETED: {"email": True, "digest": False},
        NotificationEvent.PO_RECEIVED: {"email": True, "digest": False},
        NotificationEvent.INSPECTION_FAILED: {"email": True, "digest": False},
        NotificationEvent.NCR_CREATED: {"email": True, "digest": False},
        NotificationEvent.LOW_STOCK: {"email": False, "digest": True},
        NotificationEvent.CALIBRATION_DUE: {"email": True, "digest": False},
        NotificationEvent.QUOTE_EXPIRING: {"email": True, "digest": False},
        NotificationEvent.CAPACITY_OVERLOAD: {"email": True, "digest": False},
    }

    def __init__(self, db: Session):
        self.db = db

    async def send_notification(
        self,
        event_type: str,
        users: List[int] | List[User],
        subject: str,
        context: Dict,
        template: str = None,
        related_type: str = None,
        related_id: int = None
    ):
        """
        Send notification to users

        Args:
            event_type: Event type (e.g., WO_RELEASED)
            users: List of user IDs or User objects
            subject: Email subject
            context: Template context
            template: Email template name
            related_type: Related entity type (WorkOrder, etc)
            related_id: Related entity ID
        """
        # Normalize user list
        user_ids = []
        for user in users:
            if isinstance(user, int):
                user_ids.append(user)
            else:
                user_ids.append(user.id)

        # Get user preferences
        for user_id in user_ids:
            user = self.db.query(User).filter(User.id == user_id).first()
            if not user:
                continue

            pref = self._get_user_preference(user_id)
            event_pref = pref.preferences.get(event_type, {"email": True, "digest": False})

            # Check if user wants this notification
            if event_pref.get("digest", False):
                # Add to digest queue
                self._queue_for_digest(user_id, event_type, context)
            elif event_pref.get("email", True):
                # Send immediate email
                await self._send_immediate_email(
                    user=user,
                    event_type=event_type,
                    subject=subject,
                    context=context,
                    template=template,
                    related_type=related_type,
                    related_id=related_id
                )

    async def _send_immediate_email(
        self,
        user: User,
        event_type: str,
        subject: str,
        context: Dict,
        template: str,
        related_type: str,
        related_id: int
    ):
        """Send immediate email notification"""
        try:
            # Enqueue email job
            await enqueue_job(
                "send_email_job",
                to=user.email,
                subject=subject,
                body=None,
                template=template or event_type.lower(),
                context=context
            )

            # Log notification
            log = NotificationLog(
                user_id=user.id,
                event_type=event_type,
                channel="email",
                subject=subject,
                sent=True,
                related_type=related_type,
                related_id=related_id
            )
            self.db.add(log)
            self.db.commit()

        except Exception as e:
            logger.error(f"Failed to send notification to user {user.id}: {e}")

            # Log failed notification
            log = NotificationLog(
                user_id=user.id,
                event_type=event_type,
                channel="email",
                subject=subject,
                sent=False,
                error=str(e),
                related_type=related_type,
                related_id=related_id
            )
            self.db.add(log)
            self.db.commit()

    def _queue_for_digest(self, user_id: int, event_type: str, event_data: Dict):
        """Queue notification for digest"""
        digest_item = DigestQueue(
            user_id=user_id,
            event_type=event_type,
            event_data=event_data,
            digest_date=datetime.utcnow().date()
        )
        self.db.add(digest_item)
        self.db.commit()

    def _get_user_preference(self, user_id: int) -> NotificationPreference:
        """Get or create user notification preference"""
        pref = self.db.query(NotificationPreference).filter(
            NotificationPreference.user_id == user_id
        ).first()

        if not pref:
            pref = NotificationPreference(
                user_id=user_id,
                preferences=self.DEFAULT_PREFERENCES
            )
            self.db.add(pref)
            self.db.commit()
            self.db.refresh(pref)

        return pref

    def get_digest_items(self, user_id: int, since: datetime = None) -> List[DigestQueue]:
        """Get digest items for user"""
        query = self.db.query(DigestQueue).filter(
            DigestQueue.user_id == user_id,
            DigestQueue.processed == False
        )

        if since:
            query = query.filter(DigestQueue.created_at >= since)

        return query.order_by(DigestQueue.created_at).all()

    def mark_digest_processed(self, items: List[DigestQueue]):
        """Mark digest items as processed"""
        for item in items:
            item.processed = True
        self.db.commit()


def get_notification_recipients(db: Session, role: str = None, department: str = None) -> List[User]:
    """
    Get users for notification

    Args:
        db: Database session
        role: Filter by role (e.g., 'supervisor', 'manager')
        department: Filter by department

    Returns:
        List of User objects
    """
    query = db.query(User).filter(User.is_active == True)

    if role:
        query = query.filter(User.role == role)

    if department:
        query = query.filter(User.department == department)

    return query.all()
