"""What counts as a FAILED notification delivery -- one definition for every reader.

``GET /notifications/logs?status=failed`` and ``GET /admin/system-health``'s failed-email count
both read it, so the activity list and the health tab can never disagree about which attempts
failed.
"""

from datetime import datetime, timedelta

from sqlalchemy import and_, or_

from app.models.notification import NotificationLog

# Provider states that mean "still on its way", not "failed".
IN_FLIGHT_PROVIDER_STATUSES = ("queued", "sending", "retrying")
# A row stuck in "sending" longer than this is treated as failed.
STUCK_SENDING_MINUTES = 10


def failed_delivery_clause(now: datetime):
    """Not sent, and not still in flight (or stuck "sending" past the grace window).

    ``now`` is naive UTC, matching how the delivery log compares ``sent_at``.
    """
    return and_(
        NotificationLog.sent == False,  # noqa: E712
        or_(
            NotificationLog.provider_status.is_(None),
            NotificationLog.provider_status.notin_(list(IN_FLIGHT_PROVIDER_STATUSES)),
            and_(
                NotificationLog.provider_status == "sending",
                NotificationLog.sent_at < now - timedelta(minutes=STUCK_SENDING_MINUTES),
            ),
        ),
    )
