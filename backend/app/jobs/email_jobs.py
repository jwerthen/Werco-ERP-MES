import json
import logging
from datetime import datetime, timedelta

from app.db.session import SessionLocal
from app.services.email_service import email_service
from app.services.notification_service import NotificationService

logger = logging.getLogger(__name__)


async def send_email_task(
    to: str,
    subject: str,
    body: str = None,
    template: str = None,
    context: dict = None,
    notification_log_id: int = None,
    company_id: int = None,
    user_id: int = None,
    job_try: int = 1,
    max_attempts: int = 3,
):
    """Settle one queued notification log; never automatically repeat an uncertain send."""
    import uuid

    from arq import Retry

    from app.models.notification import NotificationLog
    from app.models.user import User
    from app.services.background_email_status import notify_background_email_failure
    from app.services.email_service import EmailBeforeSubmissionFailure, EmailRejected, EmailSubmissionUnknown
    from app.services.user_identity import is_synthetic_email

    # Preserve already queued pre-upgrade jobs. New dispatcher jobs always carry
    # all three identity fields and receive durable status/replay protection below.
    if notification_log_id is None and company_id is None and user_id is None:
        result = await email_service.send_email(
            to=to, subject=subject, body=body, template=template, context=context or {}
        )
        return {'sent': result, 'to': to}
    if notification_log_id is None or company_id is None or user_id is None:
        return {'sent': False, 'reason': 'incomplete_delivery_identity'}
    db = SessionLocal()
    try:

        def lookup():
            return (
                db.query(NotificationLog)
                .filter(
                    NotificationLog.id == notification_log_id,
                    NotificationLog.company_id == company_id,
                    NotificationLog.user_id == user_id,
                    NotificationLog.channel == 'email',
                )
                .with_for_update()
                .populate_existing()
                .first()
            )

        log = lookup()
        if log is None:
            # A fast worker may beat the dispatch transaction's commit. It must
            # not send without the durable row, or adopt another tenant's log.
            db.rollback()
            if job_try < 3:
                raise Retry(defer=5)
            return {'sent': False, 'reason': 'delivery_log_unavailable'}
        if log.provider_status not in {'queued', 'retrying'}:
            return {'sent': bool(log.sent), 'status': log.provider_status, 'replayed': True}
        user = db.query(User).filter(User.id == user_id, User.company_id == company_id, User.is_active == True).first()
        if user is None or not user.email or is_synthetic_email(user.email):
            log.sent = False
            log.provider_status = 'failed'
            log.error = 'Recipient is inactive or has no deliverable email address.'
            notify_background_email_failure(db, log)
            db.commit()
            return {'sent': False, 'status': 'failed'}
        recipient = str(user.email)
        claim_id = str(uuid.uuid4())
        log.provider_status = 'sending'
        log.provider_message_id = claim_id
        log.sent_at = datetime.utcnow()
        log.error = None
        db.commit()
        # Everything handed to SMTP is detached plain data. No pool connection or
        # row lock is retained while awaiting the network.
        retry = False
        try:
            accepted = await email_service.send_email(
                to=recipient,
                subject=subject,
                body=body,
                template=template,
                context=context or {},
                message_id=claim_id,
            )
            outcome = 'accepted' if accepted else 'skipped'
            error = None if accepted else 'SMTP is not configured. No email was sent.'
        except EmailBeforeSubmissionFailure:
            retry = job_try < max_attempts
            outcome = 'retrying' if retry else 'failed'
            error = 'Connection or authentication failed before email submission. ' + (
                f'Automatic retry {job_try + 1} of {max_attempts} is pending.'
                if retry
                else f'Email was not submitted after {max_attempts} attempt(s).'
            )
        except EmailRejected:
            outcome, error = 'failed', 'The mail server rejected this email. No automatic resend will occur.'
        except EmailSubmissionUnknown:
            outcome, error = (
                'unknown',
                'Email submission may have reached the mail server. Check its logs before resending; no automatic retry will occur.',
            )
        except Exception:
            # Template/other failures are not retried blindly. Avoid placing raw
            # exception text (which can contain transport secrets) in user logs.
            outcome, error = (
                'failed',
                'The email could not be prepared or sent. Ask an administrator to review the worker logs.',
            )
            logger.exception('Background email failed for delivery log %s', notification_log_id)
        log = lookup()
        if log is not None and log.provider_status == 'sending' and log.provider_message_id == claim_id:
            log.provider_status = outcome
            log.sent = outcome == 'accepted'
            log.error = error
            notify_background_email_failure(db, log)
            db.commit()
        if retry:
            raise Retry(defer=30 * job_try)
        return {'sent': outcome == 'accepted', 'status': outcome}
    finally:
        db.close()


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

        prefs = (
            db.query(NotificationPreference)
            .filter(NotificationPreference.digest_enabled == True, NotificationPreference.digest_frequency == "DAILY")
            .all()
        )

        digest_count = 0

        for pref in prefs:
            user = (
                db.query(User)
                .filter(
                    User.id == pref.user_id,
                    User.company_id == pref.company_id,
                )
                .with_for_update()
                .first()
            )
            if not user or not user.is_active:
                continue

            # Get digest items for this user (last 24 hours)
            since = datetime.utcnow() - timedelta(days=1)
            from app.models.notification import DigestQueue, NotificationLog

            items = (
                db.query(DigestQueue)
                .filter(
                    DigestQueue.user_id == user.id,
                    DigestQueue.company_id == user.company_id,
                    DigestQueue.processed == False,
                    DigestQueue.created_at >= since,
                )
                .order_by(DigestQueue.id)
                .limit(1000)
                .all()
            )
            # Recover the immutable item set recorded before SMTP. A worker may
            # die after acceptance but before marking the digest queue processed.
            prior = (
                db.query(NotificationLog)
                .filter(
                    NotificationLog.company_id == user.company_id,
                    NotificationLog.user_id == user.id,
                    NotificationLog.event_type == 'email.daily_digest',
                    NotificationLog.related_type == 'digest_queue',
                    NotificationLog.related_id.in_([item.id for item in items]),
                    NotificationLog.provider_status.in_(['queued', 'retrying', 'sending', 'accepted', 'unknown']),
                )
                .order_by(NotificationLog.id)
                .all()
                if items
                else []
            )
            queued_log = None
            for previous in prior:
                try:
                    snapshot_ids = set(json.loads(previous.body or '{}').get('digest_item_ids', []))
                except (TypeError, ValueError):
                    snapshot_ids = set()
                if previous.provider_status in {'sending', 'accepted', 'unknown'}:
                    for item in items:
                        if item.id in snapshot_ids:
                            item.processed = True
                    items = [item for item in items if item.id not in snapshot_ids]
                elif queued_log is None:
                    queued_log = previous
                    items = [item for item in items if item.id in snapshot_ids]
            if not items:
                db.commit()
                continue

            if not items:
                continue

            # Group items by event type
            grouped_events = {}
            for item in items:
                event_type = item.event_type
                if event_type not in grouped_events:
                    grouped_events[event_type] = []
                grouped_events[event_type].append(item.event_data)

            # Freeze template data before committing; no lazy ORM reads or open
            # digest transaction should survive across SMTP awaits.
            user_id, company_id, recipient = user.id, user.company_id, user.email
            item_ids = [item.id for item in items]
            context = {
                "user": {"first_name": user.first_name, "last_name": user.last_name, "full_name": user.full_name},
                "events": grouped_events,
                "date": datetime.utcnow().strftime("%Y-%m-%d"),
            }
            subject = f"Werco ERP Daily Digest - {datetime.utcnow().strftime('%B %d, %Y')}"
            log = queued_log or NotificationLog(
                company_id=company_id,
                user_id=user_id,
                event_type='email.daily_digest',
                related_type='digest_queue',
                related_id=item_ids[0],
                body=json.dumps({'digest_item_ids': item_ids}),
                channel='email',
                subject=subject,
                sent=False,
                provider_status='queued',
            )
            db.add(log)
            db.flush()
            log_id = log.id
            db.commit()
            try:
                result = await send_email_task(
                    to=recipient,
                    subject=subject,
                    template='daily_digest',
                    context=context,
                    notification_log_id=log_id,
                    company_id=company_id,
                    user_id=user_id,
                    max_attempts=1,
                )
            except Exception:
                logger.exception('Digest delivery log %s did not finish; a later pass will recover its claim', log_id)
                db.rollback()
                continue
            if result.get('sent') or result.get('status') == 'unknown':
                # Do not automatically repeat a digest whose SMTP outcome is
                # unknown. Its delivery warning remains visible for review.
                from app.models.notification import DigestQueue

                delivered_items = (
                    db.query(DigestQueue)
                    .filter(
                        DigestQueue.id.in_(item_ids),
                        DigestQueue.company_id == company_id,
                        DigestQueue.user_id == user_id,
                    )
                    .all()
                )
                notification_service.mark_digest_processed(delivered_items)
            if result.get('sent'):
                digest_count += 1

        logger.info(f"Sent {digest_count} daily digest emails")
        return {"digests_sent": digest_count}

    except Exception as e:
        logger.error(f"Daily digest job failed: {e}")
        raise
    finally:
        db.close()
