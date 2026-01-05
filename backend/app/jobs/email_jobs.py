from app.services.email_service import email_service
from app.db.session import SessionLocal
from app.services.notification_service import NotificationService
from datetime import datetime, timedelta
import logging

logger = logging.getLogger(__name__)


async def send_email_task(
    to: str,
    subject: str,
    body: str = None,
    template: str = None,
    context: dict = None
):
    """
    Background job to send email

    Args:
        to: Recipient email
        subject: Email subject
        body: Plain text body
        template: Template name
        context: Template context
    """
    try:
        result = await email_service.send_email(
            to=to,
            subject=subject,
            body=body,
            template=template,
            context=context or {}
        )

        return {"sent": result, "to": to}

    except Exception as e:
        logger.error(f"Email job failed: {e}")
        raise


async def send_daily_digest_task():
    """
    Send daily digest emails to all users

    Aggregates notifications from the digest queue and sends a single email
    """
    db = SessionLocal()
    try:
        notification_service = NotificationService(db)

        # Get all users with digest enabled
        from app.models.notification import NotificationPreference
        from app.models.user import User

        prefs = db.query(NotificationPreference).filter(
            NotificationPreference.digest_enabled == True,
            NotificationPreference.digest_frequency == "DAILY"
        ).all()

        digest_count = 0

        for pref in prefs:
            user = db.query(User).filter(User.id == pref.user_id).first()
            if not user or not user.is_active:
                continue

            # Get digest items for this user (last 24 hours)
            since = datetime.utcnow() - timedelta(days=1)
            items = notification_service.get_digest_items(user.id, since)

            if not items:
                continue

            # Group items by event type
            grouped_events = {}
            for item in items:
                event_type = item.event_type
                if event_type not in grouped_events:
                    grouped_events[event_type] = []
                grouped_events[event_type].append(item.event_data)

            # Send digest email
            context = {
                "user": user,
                "events": grouped_events,
                "date": datetime.utcnow().strftime("%Y-%m-%d")
            }

            await email_service.send_email(
                to=user.email,
                subject=f"Werco ERP Daily Digest - {datetime.utcnow().strftime('%B %d, %Y')}",
                template="daily_digest",
                context=context
            )

            # Mark items as processed
            notification_service.mark_digest_processed(items)
            digest_count += 1

        logger.info(f"Sent {digest_count} daily digest emails")
        return {"digests_sent": digest_count}

    except Exception as e:
        logger.error(f"Daily digest job failed: {e}")
        raise
    finally:
        db.close()
