"""ARQ jobs for the SMS notification channel (``NOTIFICATIONS_PLAN.md`` §3.4).

Retry semantics mirror ``email_jobs.send_email_task``:

* a real transport failure **re-raises** so ARQ retries the job;
* everything terminal (egress disabled, unconfigured Twilio, unusable phone,
  provider 4xx rejection, storm suppression) is recorded and returned WITHOUT
  raising, so retries are never burned on an outcome that cannot improve.

Tenancy: the job receives ``company_id`` + ``user_id`` (never a phone number — PII
stays out of the Redis job payload) and re-resolves the recipient tenant-scoped and
``is_active``-filtered at send time, so a user deactivated between dispatch and
delivery is not messaged. Every ``NotificationLog`` row it writes or updates is
scoped/stamped with that same ``company_id``.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models.notification import NotificationLog
from app.models.user import User
from app.services.sms_content import build_overflow_sms_body
from app.services.sms_service import (
    InvalidPhoneNumberError,
    SMSEgressDisabledError,
    SMSPermanentError,
    SMSResult,
    peek_sms_overflow,
    scrub_phone_numbers,
    send_sms,
    settle_sms_overflow,
)

logger = logging.getLogger(__name__)

#: ``event_type`` recorded for the storm-control collapse message (not a catalog key —
#: it is a transport-level artifact, not a notifiable domain event).
SMS_COLLAPSE_EVENT_TYPE = "sms.storm_collapse"


def _record_delivery(
    db: Session,
    *,
    company_id: int,
    user_id: int,
    notification_log_id: Optional[int],
    event_type: str,
    body: Optional[str],
    sent: bool,
    error: Optional[str] = None,
    provider_message_id: Optional[str] = None,
    provider_status: Optional[str] = None,
) -> None:
    """Update the pre-created ``NotificationLog`` row, or insert one if absent.

    The dispatcher creates the row at enqueue time (``sent=False``) and passes its id,
    so retries update ONE row rather than appending a row per attempt. Callers without
    a pre-created row (the collapse message) get a fresh one. The update is tenant-
    scoped by ``company_id`` — a log row from another tenant can never be touched.
    """
    row: Optional[NotificationLog] = None
    if notification_log_id is not None:
        row = (
            db.query(NotificationLog)
            .filter(
                NotificationLog.id == notification_log_id,
                NotificationLog.company_id == company_id,
            )
            .first()
        )

    if row is None:
        row = NotificationLog(
            company_id=company_id,
            user_id=user_id,
            event_type=event_type,
            channel="sms",
            body=body,
        )
        db.add(row)

    row.sent = sent
    row.error = error
    row.provider_message_id = provider_message_id
    row.provider_status = provider_status
    db.commit()


async def send_sms_task(
    *,
    company_id: int,
    user_id: int,
    body: str,
    notification_log_id: Optional[int] = None,
    event_type: str = "sms",
) -> dict:
    """Deliver one SMS and record its terminal outcome on the delivery log.

    Raises (for an ARQ retry) only on a genuine transport failure; every terminal
    outcome returns a dict describing it.
    """
    db = SessionLocal()
    try:
        user = (
            db.query(User).filter(User.id == user_id, User.company_id == company_id, User.is_active.is_(True)).first()
        )
        if user is None or not user.phone:
            _record_delivery(
                db,
                company_id=company_id,
                user_id=user_id,
                notification_log_id=notification_log_id,
                event_type=event_type,
                body=body,
                sent=False,
                error="recipient is inactive or has no phone number on file",
            )
            return {"sent": False, "reason": "no_recipient_phone"}

        try:
            result: SMSResult = await send_sms(db=db, company_id=company_id, to=user.phone, body=body)
        except SMSEgressDisabledError:
            # CUI kill switch is OFF for this tenant. Terminal: no retry can change it.
            db.rollback()
            _record_delivery(
                db,
                company_id=company_id,
                user_id=user_id,
                notification_log_id=notification_log_id,
                event_type=event_type,
                body=body,
                sent=False,
                error="SMS egress is disabled for this company",
            )
            return {"sent": False, "reason": "egress_disabled"}
        except InvalidPhoneNumberError as exc:
            db.rollback()
            _record_delivery(
                db,
                company_id=company_id,
                user_id=user_id,
                notification_log_id=notification_log_id,
                event_type=event_type,
                body=body,
                sent=False,
                error=f"invalid phone number on file: {scrub_phone_numbers(str(exc))}",
            )
            return {"sent": False, "reason": "invalid_phone"}
        except SMSPermanentError as exc:
            db.rollback()
            _record_delivery(
                db,
                company_id=company_id,
                user_id=user_id,
                notification_log_id=notification_log_id,
                event_type=event_type,
                body=body,
                sent=False,
                error=f"provider rejected the message: {scrub_phone_numbers(str(exc))}",
                provider_status=str(exc.status) if exc.status is not None else None,
            )
            return {"sent": False, "reason": "provider_rejected"}
        except Exception as exc:
            # Transport failure -- record the attempt, then RE-RAISE so ARQ retries.
            db.rollback()
            try:
                _record_delivery(
                    db,
                    company_id=company_id,
                    user_id=user_id,
                    notification_log_id=notification_log_id,
                    event_type=event_type,
                    body=body,
                    sent=False,
                    error=f"transport failure (will retry): {scrub_phone_numbers(str(exc))}",
                )
            except Exception:  # pragma: no cover - logging must not mask the retry
                logger.exception("Could not record SMS transport failure for user %s", user_id)
            # Scrubbed here too: this line reaches application logs and Sentry, which are
            # readable by people who cannot see the recipient's phone in the app.
            logger.error("SMS job failed for user %s: %s", user_id, scrub_phone_numbers(str(exc)))
            raise

        _record_delivery(
            db,
            company_id=company_id,
            user_id=user_id,
            notification_log_id=notification_log_id,
            event_type=event_type,
            body=body,
            sent=result.sent,
            error=None if result.sent else f"skipped: {result.reason}",
            provider_message_id=result.sid,
            provider_status=result.provider_status,
        )
        return {"sent": result.sent, "sid": result.sid, "status": result.provider_status, "reason": result.reason}
    finally:
        db.close()


async def send_sms_overflow_task(*, company_id: int, user_id: int) -> dict:
    """Storm-control collapse: one "…and N more — check the app" message.

    Deferred by ``SMS_COLLAPSE_DELAY_SECONDS`` from the first suppressed alert so the
    count reflects the whole burst. Reads the counter, sends, and only then settles it,
    so a retried collapse never loses alerts. Deliberately bypasses the per-user cap —
    it IS the cap's safety valve — and still passes through the egress kill switch.
    """
    count = await peek_sms_overflow(user_id)
    if count <= 0:
        return {"sent": False, "reason": "nothing_to_collapse"}

    body = build_overflow_sms_body(count)
    db = SessionLocal()
    try:
        user = (
            db.query(User).filter(User.id == user_id, User.company_id == company_id, User.is_active.is_(True)).first()
        )
        if user is None or not user.phone:
            await settle_sms_overflow(user_id, count)
            return {"sent": False, "reason": "no_recipient_phone"}

        try:
            result = await send_sms(db=db, company_id=company_id, to=user.phone, body=body)
        except (SMSEgressDisabledError, InvalidPhoneNumberError, SMSPermanentError) as exc:
            db.rollback()
            _record_delivery(
                db,
                company_id=company_id,
                user_id=user_id,
                notification_log_id=None,
                event_type=SMS_COLLAPSE_EVENT_TYPE,
                body=body,
                sent=False,
                error=f"collapse message not delivered: {exc}",
            )
            # Terminal for this attempt: clear the count so the next burst starts clean.
            await settle_sms_overflow(user_id, count)
            return {"sent": False, "reason": "not_delivered"}
        except Exception:
            db.rollback()
            logger.warning("SMS collapse message failed for user %s; will retry", user_id, exc_info=True)
            raise  # counter deliberately NOT settled -- the retry re-reads it

        _record_delivery(
            db,
            company_id=company_id,
            user_id=user_id,
            notification_log_id=None,
            event_type=SMS_COLLAPSE_EVENT_TYPE,
            body=body,
            sent=result.sent,
            error=None if result.sent else f"skipped: {result.reason}",
            provider_message_id=result.sid,
            provider_status=result.provider_status,
        )
        await settle_sms_overflow(user_id, count)
        return {"sent": result.sent, "collapsed": count, "sid": result.sid}
    finally:
        db.close()
