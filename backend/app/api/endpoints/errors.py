"""
Frontend Error Logging Endpoint

Receives error logs from the frontend for monitoring and debugging, and routes
them to the application logger (and Sentry, for global-boundary errors).

THIS ENDPOINT DELIBERATELY WRITES NOTHING TO THE DATABASE. It is unauthenticated
by necessity — ``navigator.sendBeacon`` on page unload cannot attach an
Authorization header — and its ``userId`` is client-supplied, unverified data
read out of sessionStorage. It previously resolved that string to a ``User`` row
(unscoped, no tenant filter) and handed it to ``AuditService``, which let any
internet caller inject arbitrary rows into the tamper-evident audit chain
attributed to a named employee in a named company, permanently (audit rows are
immutable by DB trigger, migrations 008/060). Worse, each entry in the batch
became one audit INSERT taking the single install-wide
``pg_advisory_xact_lock`` that serializes the hash chain, so one oversized
request could stall EVERY audited write in the system while it drained.

Both problems are removed at the root: no audit row is written here at all.
Nothing ever consumed those rows — ``FRONTEND_ERROR`` had exactly one occurrence
repo-wide (the write itself) — and they were not merely useless, they were
actively misleading. ``AuditService._resolve_company_id`` returns
``user.company_id`` whenever a user resolves, and the real client DOES send a
resolvable id (``frontend/src/services/errorLogging.ts`` fills ``userId`` from
``sessionStorage['user'].id``), so a logged-in user's error row carried their
real ``company_id`` and WAS visible to any Admin/Manager at
``GET /api/v1/audit/``. Only anonymous, pre-login errors landed with
``company_id IS NULL``. So an Admin reading the audit log saw
"FRONTEND_ERROR — Admin User, company 1", with an attacker-chosen
``ip_address``, for rows any internet caller could fabricate at will. Do not
reintroduce a DB write on this path; if frontend errors ever need durable
storage, give them their own non-audit table behind an authenticated route.

The request caps below (list length + per-field lengths) bound what a single
unauthenticated call can make the app parse and log; ``MAX_JSON_BODY_BYTES``
(413) is the outer bound on the body itself, and main.py's ENDPOINT_RATE_LIMITS
entry bounds call frequency. ``MAX_GLOBAL_ALERTS_PER_REQUEST`` bounds the one
remaining outbound side-effect (Sentry).
"""

import logging
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Request
from pydantic import BaseModel, Field

from app.core.time_utils import to_utc_iso

router = APIRouter(prefix="/errors", tags=["errors"])
logger = logging.getLogger(__name__)

# Matches ``maxQueueSize`` in frontend/src/services/errorLogging.ts — the client
# never batches more than this, so a larger batch is not a real SPA tab.
MAX_ERRORS_PER_REQUEST = 50

# ``boundaryLevel`` is attacker-supplied, and the value "global" is what drives
# send_error_alert -> sentry_sdk.capture_message. Uncapped, one request could
# spend 50 Sentry events and the rate limit allows 60 requests/minute — 3,000
# events/minute from a spoofable field, burning the quota that buys real error
# visibility. A genuine page only ever reports one global-boundary crash per
# flush, so a small per-request ceiling costs nothing real.
MAX_GLOBAL_ALERTS_PER_REQUEST = 3


class ErrorLogEntry(BaseModel):
    """One client-reported error. Every field is untrusted, client-supplied text.

    The ``max_length`` caps are generous relative to real browser payloads (React
    component stacks run well under 10 KB) but bound abuse; they reject rather
    than truncate, so an over-cap batch is a loud 422 instead of silent data loss.
    """

    id: str = Field(..., max_length=128)
    message: str = Field(..., max_length=4000)
    stack: Optional[str] = Field(default=None, max_length=20000)
    componentStack: Optional[str] = Field(default=None, max_length=20000)
    boundaryName: Optional[str] = Field(default=None, max_length=200)
    boundaryLevel: Optional[str] = Field(default=None, max_length=50)
    url: str = Field(..., max_length=2000)
    timestamp: str = Field(..., max_length=64)
    userAgent: str = Field(..., max_length=1000)
    # Client-claimed only. NEVER resolved to a User row or used for attribution.
    userId: Optional[str] = Field(default=None, max_length=64)
    sessionId: Optional[str] = Field(default=None, max_length=128)
    # NOTE: the client also sends a free-form ``metadata`` object (screen size,
    # language, and `preservedData` — arbitrary in-progress form contents). It is
    # deliberately NOT declared here: it was the one field with no length bound,
    # it is never logged or emitted anywhere, and dropping it keeps unbounded
    # client data (potentially a user's half-typed record) out of the process
    # entirely. Pydantic ignores undeclared fields, so the client is unaffected.


class ErrorLogRequest(BaseModel):
    errors: List[ErrorLogEntry] = Field(..., max_length=MAX_ERRORS_PER_REQUEST)


class ErrorLogResponse(BaseModel):
    status: str
    count: int


@router.post("/log", response_model=ErrorLogResponse)
async def log_errors(request: ErrorLogRequest, background_tasks: BackgroundTasks, http_request: Request):
    """
    Log frontend errors for monitoring and debugging.

    Errors are processed in the background to avoid blocking the client.
    Critical errors (global boundary level) trigger immediate alerts.

    Unauthenticated and CSRF-exempt by necessity (sendBeacon). Nothing is
    persisted — see the module docstring for why the audit write was removed.
    """
    # Get client IP for additional context
    client_ip = http_request.client.host if http_request.client else "unknown"

    # Process errors in background
    background_tasks.add_task(process_error_logs, request.errors, client_ip)

    return ErrorLogResponse(status="queued", count=len(request.errors))


async def process_error_logs(errors: List[ErrorLogEntry], client_ip: str):
    """Emit client-reported errors to the application logger (and Sentry).

    Log-only by design: this path has no database access. See the module
    docstring — do not add one. Global-boundary alerts are capped per request
    (``MAX_GLOBAL_ALERTS_PER_REQUEST``) because ``boundaryLevel`` is
    attacker-supplied and each "global" entry costs a Sentry event.
    """
    global_alerts_sent = 0

    for error in errors:
        try:
            # Log to application logger
            log_level = logging.ERROR if error.boundaryLevel == "global" else logging.WARNING

            logger.log(
                log_level,
                f"Frontend Error [{error.id}]: {error.message}",
                extra={
                    "error_id": error.id,
                    "boundary_name": error.boundaryName,
                    "boundary_level": error.boundaryLevel,
                    "url": error.url,
                    # Client-claimed, unverified — named so no log consumer
                    # mistakes it for an authenticated actor.
                    "claimed_user_id": error.userId,
                    "session_id": error.sessionId,
                    "client_ip": client_ip,
                    "user_agent": error.userAgent,
                    "stack": error.stack[:1000] if error.stack else None,
                    "component_stack": error.componentStack[:500] if error.componentStack else None,
                },
            )

            # Alert on critical errors, bounded per request.
            if error.boundaryLevel == "global":
                if global_alerts_sent < MAX_GLOBAL_ALERTS_PER_REQUEST:
                    global_alerts_sent += 1
                    await send_error_alert(error)
                elif global_alerts_sent == MAX_GLOBAL_ALERTS_PER_REQUEST:
                    global_alerts_sent += 1  # log the suppression notice exactly once
                    logger.warning(
                        "Suppressing further global-boundary alerts for this batch " "(cap=%s, batch=%s, client_ip=%s)",
                        MAX_GLOBAL_ALERTS_PER_REQUEST,
                        len(errors),
                        client_ip,
                    )

        except Exception as e:
            # Don't let error logging errors crash the system
            logger.exception(f"Failed to process error log: {e}")


async def send_error_alert(error: ErrorLogEntry):
    """
    Send alert for critical errors.

    Current implementation:
    - Logs critical errors prominently to server logs
    - If Sentry DSN is configured, errors are captured via Sentry

    Future integration options (configure via environment variables):
    - Slack: Set SLACK_WEBHOOK_URL for Slack notifications
    - Email: Use SMTP settings for email alerts
    - PagerDuty: Set PAGERDUTY_API_KEY for incident management

    The error boundary information helps identify where in the React
    component tree the error occurred, aiding in debugging.
    """
    from app.core.config import settings

    # Log critical error prominently to server logs
    logger.critical(
        f"CRITICAL FRONTEND ERROR [{error.id}]: {error.message}\n"
        f"URL: {error.url}\n"
        f"Claimed user (unverified): {error.userId or 'anonymous'}\n"
        f"Boundary: {error.boundaryName}"
    )

    # If Sentry is configured, capture the error there
    # Sentry integration is already handled in main.py lifespan
    # Critical errors are automatically captured by Sentry's logging integration
    if settings.SENTRY_DSN:
        try:
            import sentry_sdk

            sentry_sdk.capture_message(
                f"Frontend Error [{error.boundaryName}]: {error.message}",
                level="error",
                extras={
                    "error_id": error.id,
                    "url": error.url,
                    "claimed_user_id": error.userId,
                    "boundary_name": error.boundaryName,
                    "boundary_level": error.boundaryLevel,
                    "stack": error.stack,
                    "component_stack": error.componentStack,
                },
            )
            logger.info(f"Error {error.id} sent to Sentry")
        except ImportError:
            logger.debug("Sentry SDK not installed, skipping Sentry capture")
        except Exception as e:
            logger.warning(f"Failed to send error to Sentry: {e}")

    # NOTE: Additional alerting integrations can be added here as needed:
    #
    # Slack Integration (future):
    # if settings.SLACK_WEBHOOK_URL:
    #     await slack_client.send_webhook(settings.SLACK_WEBHOOK_URL, {
    #         "text": f"🚨 Frontend Error: {error.message}",
    #         "blocks": [...error details...]
    #     })
    #
    # Email Integration (future):
    # if settings.ALERT_EMAIL:
    #     await email_service.send_alert(
    #         to=settings.ALERT_EMAIL,
    #         subject=f"Frontend Error Alert: {error.boundaryName}",
    #         body=f"Error: {error.message}\nURL: {error.url}"
    #     )


@router.get("/health")
async def error_logging_health():
    """Health check for error logging service."""
    return {"status": "healthy", "timestamp": to_utc_iso(datetime.utcnow())}
