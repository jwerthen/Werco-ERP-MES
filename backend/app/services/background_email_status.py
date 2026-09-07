"""Visible, non-recursive in-app warnings for failed background email."""

from app.models.notification import Notification


def notify_background_email_failure(db, log):
    if log.provider_status not in {'failed', 'skipped', 'unknown'}:
        return
    existing = (
        db.query(Notification.id)
        .filter(
            Notification.company_id == log.company_id,
            Notification.user_id == log.user_id,
            Notification.event_key == 'email.delivery_failed',
            Notification.related_type == 'notification_delivery',
            Notification.related_id == log.id,
        )
        .first()
    )
    if existing:
        return
    unknown = log.provider_status == 'unknown'
    db.add(
        Notification(
            company_id=log.company_id,
            user_id=log.user_id,
            event_key='email.delivery_failed',
            severity='warning',
            title='Background email outcome is unknown' if unknown else 'Background email was not sent',
            body=log.error or 'Review the email delivery activity for details.',
            link=f'/notifications?delivery={log.id}',
            related_type='notification_delivery',
            related_id=log.id,
        )
    )
