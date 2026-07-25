"""Twilio SMS transport for the notification pipeline (``NOTIFICATIONS_PLAN.md`` §3.4).

This module is the SINGLE enforcement point for outbound SMS, mirroring
``services/llm_client.py``'s role for Anthropic egress:

* **Fail-closed CUI kill switch.** Before any Twilio call, :func:`_sms_egress_allowed`
  resolves ``Company.allow_sms_egress``; a disabled (or unknown, or unresolvable)
  tenant raises :class:`SMSEgressDisabledError` and NOTHING leaves the boundary — no
  HTTP request, no phone number, no message body.
* **Unconfigured is a soft skip, not a failure.** With no Twilio credentials the
  service logs and returns a ``"skipped"`` result WITHOUT raising, exactly like the
  unconfigured-SMTP path in ``EmailService.send_email`` — an unconfigured dev/test
  environment must not spam ARQ retries.
* **Real transport failures RAISE** so the enqueuing ARQ job retries. A *permanent*
  provider rejection (Twilio 4xx other than 429 — bad number, unsubscribed recipient,
  blocked region) raises :class:`SMSPermanentError` instead, which the job records as
  a terminal outcome without burning retries.

Credentials come exclusively from ``Settings`` (environment). Nothing is hardcoded.

Two Twilio auth modes are supported (see :func:`_twilio_credentials`):

1. **API-key auth (preferred)** — ``TWILIO_ACCOUNT_SID`` + ``TWILIO_API_KEY_SID``
   (``SK…``) + ``TWILIO_API_KEY_SECRET``. Revocable per key without rotating the
   account credential.
2. **Legacy auth-token auth** — ``TWILIO_ACCOUNT_SID`` + ``TWILIO_AUTH_TOKEN``.

Sending also needs a sender: ``TWILIO_MESSAGING_SERVICE_SID`` (preferred) or
``TWILIO_FROM_NUMBER``.

Message bodies are built by ``services/sms_content.py`` — see the CUI content rule
there. This module never composes body text from domain data.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Optional, Tuple

from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.queue import get_redis_pool

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Storm control (§3.4)
# ---------------------------------------------------------------------------

#: Per-user SMS cap. SMS is critical-events-only and opt-in, so this is a storm
#: valve, not a quota: alerts beyond it are collapsed into ONE "N more" message.
SMS_HOURLY_CAP_PER_USER = 5

#: Fixed window (seconds) the cap is counted over, starting at the user's first SMS.
SMS_QUOTA_WINDOW_SECONDS = 3600

#: How long the collapse message waits before flushing, so a burst of alerts that
#: overflows the cap is summarized by ONE message carrying a real count rather than a
#: trickle of "1 more" notices.
SMS_COLLAPSE_DELAY_SECONDS = 120

#: Per-user hourly budget for the self-service "Send test SMS" button
#: (``POST /users/me/test-sms``). Small on purpose: a couple of tries is enough to
#: confirm a number, and without a PER-IDENTITY bound the endpoint's only limit is a
#: per-IP one — which lets a single authenticated user run up a carrier bill from a
#: handful of addresses. Kept separate from ``SMS_HOURLY_CAP_PER_USER`` so testing the
#: button never eats into the critical-alert budget (and vice versa).
SMS_TEST_HOURLY_CAP_PER_USER = 3

#: Connect/read timeout (seconds) applied to every Twilio HTTP call. Without an
#: explicit timeout the SDK inherits ``requests``' default of NONE: a black-holed
#: request would pin a threadpool worker AND hold the job's DB transaction open until
#: ARQ's 600s job timeout fired, after which the retry could re-send a message Twilio
#: had in fact accepted. Short and explicit: Twilio's message-create API answers in
#: well under a second normally, and a genuine transport failure re-raises into the
#: normal ARQ retry path anyway.
TWILIO_HTTP_TIMEOUT_SECONDS = 10.0

_QUOTA_KEY = "werco:notify:sms:quota:{user_id}"
_OVERFLOW_KEY = "werco:notify:sms:overflow:{user_id}"
_COLLAPSE_ARM_KEY = "werco:notify:sms:collapse:{user_id}"
_TEST_QUOTA_KEY = "werco:notify:sms:test:{user_id}"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SMSEgressDisabledError(RuntimeError):
    """Raised when a company has SMS egress disabled (``allow_sms_egress`` False).

    The Twilio call is NOT made — no CUI (and no phone number) leaves the boundary.
    Also raised when the tenant cannot be resolved: this is a CUI control, so it
    fails closed.
    """

    def __init__(self, company_id: Optional[int]):
        self.company_id = company_id
        super().__init__("SMS egress is disabled for this company")


class InvalidPhoneNumberError(ValueError):
    """Raised when a phone number cannot be parsed/validated as a real E.164 number."""


class SMSPermanentError(RuntimeError):
    """Twilio permanently rejected the message (4xx other than 429).

    Terminal: retrying cannot help (invalid/unreachable number, opted-out recipient,
    unpermitted region). Carries the provider status/error code for the delivery log.
    """

    def __init__(self, message: str, *, status: Optional[int] = None, code: Optional[int] = None):
        self.status = status
        self.code = code
        super().__init__(message)


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SMSResult:
    """Outcome of one :func:`send_sms` call.

    ``status`` is ``"sent"`` or ``"skipped"``. ``sid`` / ``provider_status`` are the
    Twilio message SID and its initial status (``queued``/``accepted``/…) and are
    recorded on the ``NotificationLog`` row by the caller.
    """

    status: str
    sid: Optional[str] = None
    provider_status: Optional[str] = None
    reason: Optional[str] = None

    @property
    def sent(self) -> bool:
        return self.status == "sent"


# ---------------------------------------------------------------------------
# Fail-closed egress gate (mirrors llm_client._ai_egress_allowed)
# ---------------------------------------------------------------------------


def _sms_egress_allowed(db: Session, company_id: Optional[int]) -> bool:
    """Resolve the ``Company.allow_sms_egress`` CUI kill switch — fail-closed.

    Returns ``True`` ONLY when egress is affirmatively allowed for a resolvable
    tenant. Every uncertain path denies:

    - ``company_id is None`` → ``False``. (Deliberately STRICTER than
      ``llm_client._ai_egress_allowed``, which allows the no-tenant edge: every SMS
      caller has a tenant — the dispatcher stamps ``company_id`` from the triggering
      event and the API path takes it from ``get_current_company_id`` — so a missing
      one is a bug, and a bug must not egress.)
    - company row not found → ``False`` (never egress for an unknown tenant).
    - any exception (DB down, …) → ``False``; a control that cannot verify "allowed"
      must deny.
    """
    if company_id is None:
        logger.error("SMS egress check denied: no company context")
        return False
    try:
        from app.models.company import Company

        allowed = db.query(Company.allow_sms_egress).filter(Company.id == company_id).scalar()
    except Exception:
        logger.exception("SMS egress check failed for company %s; denying (fail-closed)", company_id)
        return False

    if allowed is None:
        logger.warning("SMS egress check denied: company %s not found", company_id)
        return False
    return bool(allowed)


# ---------------------------------------------------------------------------
# Phone helpers (E.164)
# ---------------------------------------------------------------------------


def normalize_phone(raw: Optional[str], region: Optional[str] = None) -> str:
    """Validate ``raw`` and return it in E.164 (e.g. ``+15125551234``).

    Numbers typed without a country code are parsed against ``region`` (default
    ``settings.SMS_DEFAULT_REGION``, "US" — the shop-local default). Raises
    :class:`InvalidPhoneNumberError` for anything that is not a real, dialable
    number; storage is E.164 only so the transport never has to guess.
    """
    if raw is None:
        raise InvalidPhoneNumberError("Phone number is required")
    value = str(raw).strip()
    if not value:
        raise InvalidPhoneNumberError("Phone number is required")

    import phonenumbers

    try:
        parsed = phonenumbers.parse(value, region or settings.SMS_DEFAULT_REGION or "US")
    except phonenumbers.NumberParseException as exc:
        raise InvalidPhoneNumberError(f"Invalid phone number: {value}") from exc

    if not phonenumbers.is_valid_number(parsed):
        raise InvalidPhoneNumberError(f"Invalid phone number: {value}")

    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def is_valid_phone(raw: Optional[str], region: Optional[str] = None) -> bool:
    """True when :func:`normalize_phone` would succeed."""
    try:
        normalize_phone(raw, region)
        return True
    except InvalidPhoneNumberError:
        return False


def mask_phone(phone: Optional[str]) -> str:
    """Log-safe rendering of a phone number (last 4 digits only)."""
    if not phone:
        return "<none>"
    digits = "".join(ch for ch in str(phone) if ch.isdigit())
    return f"***{digits[-4:]}" if len(digits) >= 4 else "***"


# ---------------------------------------------------------------------------
# Twilio configuration + lazy singleton client
# ---------------------------------------------------------------------------

_client: Optional[Any] = None
_client_lock = threading.Lock()


def _twilio_credentials() -> Optional[Tuple[str, str, str]]:
    """Resolve ``(username, password, account_sid)`` for the Twilio client, or None.

    API-key auth wins when ``TWILIO_API_KEY_SID`` + ``TWILIO_API_KEY_SECRET`` are both
    present (the operator's ``SK…`` key); otherwise the legacy account-SID +
    auth-token pair is used. ``TWILIO_ACCOUNT_SID`` is required either way — in
    API-key mode it identifies the account the key acts on.
    """
    account_sid = (settings.TWILIO_ACCOUNT_SID or "").strip()
    if not account_sid:
        return None

    api_key_sid = (settings.TWILIO_API_KEY_SID or "").strip()
    api_key_secret = (settings.TWILIO_API_KEY_SECRET or "").strip()
    if api_key_sid and api_key_secret:
        return (api_key_sid, api_key_secret, account_sid)

    auth_token = (settings.TWILIO_AUTH_TOKEN or "").strip()
    if auth_token:
        return (account_sid, auth_token, account_sid)

    return None


def _twilio_sender() -> Optional[dict]:
    """Sender kwargs for ``messages.create``: messaging service SID, else from-number."""
    messaging_service_sid = (settings.TWILIO_MESSAGING_SERVICE_SID or "").strip()
    if messaging_service_sid:
        return {"messaging_service_sid": messaging_service_sid}
    from_number = (settings.TWILIO_FROM_NUMBER or "").strip()
    if from_number:
        return {"from_": from_number}
    return None


def sms_configured() -> bool:
    """True when credentials AND a sender are configured (no Twilio import needed)."""
    return _twilio_credentials() is not None and _twilio_sender() is not None


def auth_mode() -> Optional[str]:
    """``"api_key"`` / ``"auth_token"`` / ``None`` — for diagnostics and logs only."""
    account_sid = (settings.TWILIO_ACCOUNT_SID or "").strip()
    if not account_sid:
        return None
    if (settings.TWILIO_API_KEY_SID or "").strip() and (settings.TWILIO_API_KEY_SECRET or "").strip():
        return "api_key"
    if (settings.TWILIO_AUTH_TOKEN or "").strip():
        return "auth_token"
    return None


def get_twilio_client() -> Any:
    """Return the module-level singleton ``twilio.rest.Client``.

    Built lazily on first use from ``Settings`` so importing this module needs neither
    the SDK nor credentials (tests monkeypatch this single seam).
    """
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is None:
            credentials = _twilio_credentials()
            if credentials is None:  # pragma: no cover - callers check sms_configured()
                raise RuntimeError("Twilio credentials are not configured")
            username, password, account_sid = credentials

            from twilio.rest import Client

            _client = Client(username, password, account_sid)
    return _client


def reset_twilio_client() -> None:
    """Drop the cached client (tests / credential rotation)."""
    global _client
    with _client_lock:
        _client = None


# ---------------------------------------------------------------------------
# Storm control
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SMSQuotaDecision:
    """Outcome of a storm-control reservation.

    ``send`` — deliver this message individually.
    ``arm_collapse`` — the caller won the right to schedule the deferred collapse
    ("…and N more") message for this user; it must enqueue the flush job.
    ``count`` — messages counted in the current window (0 when Redis was unavailable).
    """

    send: bool
    arm_collapse: bool = False
    count: int = 0


async def reserve_sms_quota(user_id: int, cap: int = SMS_HOURLY_CAP_PER_USER) -> SMSQuotaDecision:
    """Reserve one slot in the per-user hourly SMS window.

    Storm control (§3.4): the first ``cap`` messages in a rolling window send
    normally; everything beyond is suppressed and counted, and the first overflow
    arms a deferred collapse message that reports the suppressed total ONCE.

    **Redis-down trade-off (deliberate): FAIL OPEN.** If Redis is unreachable the
    counter cannot be read, and this returns ``send=True`` with a WARNING. SMS here
    is critical-events-only, opt-in, and gated by a per-company kill switch, so
    dropping a critical quality alert (an AS9100D awareness control) is a worse
    failure than delivering an extra message during an infrastructure outage. The
    same call site still applies the per-recipient/channel dedup window, and the
    cap resumes the moment Redis returns. (Contrast the dedup reservation, which
    also fails open, and the *egress* gate, which fails CLOSED — that one protects
    the CUI boundary, where the safe default is the opposite.)
    """
    try:
        redis = await get_redis_pool()
        quota_key = _QUOTA_KEY.format(user_id=user_id)
        count = int(await redis.incr(quota_key))
        if count == 1:
            await redis.expire(quota_key, SMS_QUOTA_WINDOW_SECONDS)
        if count <= cap:
            return SMSQuotaDecision(send=True, count=count)

        # Over cap: suppress this individual message and accrue it into the collapse.
        overflow_key = _OVERFLOW_KEY.format(user_id=user_id)
        overflow = int(await redis.incr(overflow_key))
        if overflow == 1:
            await redis.expire(overflow_key, SMS_QUOTA_WINDOW_SECONDS)
        armed = bool(
            await redis.set(
                _COLLAPSE_ARM_KEY.format(user_id=user_id),
                "1",
                ex=SMS_COLLAPSE_DELAY_SECONDS,
                nx=True,
            )
        )
        logger.info(
            "SMS storm cap reached for user %s (%d in window, %d suppressed)%s",
            user_id,
            count,
            overflow,
            "; collapse armed" if armed else "",
        )
        return SMSQuotaDecision(send=False, arm_collapse=armed, count=count)
    except Exception:
        logger.warning(
            "SMS storm-control counter unavailable (Redis); failing OPEN and sending for user %s",
            user_id,
            exc_info=True,
        )
        return SMSQuotaDecision(send=True)


async def peek_sms_overflow(user_id: int) -> int:
    """Read (without clearing) how many messages the cap has suppressed for a user.

    The collapse job reads the count, sends ONE "N more" message, and only then
    settles it via :func:`settle_sms_overflow` — so a failed/retried collapse never
    loses the count, and messages suppressed while the collapse was in flight are
    preserved for the next one. Returns 0 on Redis error.
    """
    try:
        redis = await get_redis_pool()
        raw = await redis.get(_OVERFLOW_KEY.format(user_id=user_id))
        return int(raw) if raw else 0
    except Exception:
        logger.warning("Could not read SMS overflow counter for user %s", user_id, exc_info=True)
        return 0


async def settle_sms_overflow(user_id: int, count: int) -> None:
    """Deduct ``count`` already-summarized messages from the user's overflow counter.

    Decrement (not delete) so alerts suppressed *between* the collapse read and this
    settle survive into the next collapse. Best-effort: a Redis error is logged and
    ignored (worst case the next collapse over-reports).
    """
    if count <= 0:
        return
    key = _OVERFLOW_KEY.format(user_id=user_id)
    try:
        redis = await get_redis_pool()
        remaining = int(await redis.decrby(key, count))
        if remaining <= 0:
            await redis.delete(key)
    except Exception:
        logger.warning("Could not settle SMS overflow counter for user %s", user_id, exc_info=True)


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------


def _send_via_twilio(*, to: str, body: str) -> Any:
    """Blocking Twilio call. Runs in a worker thread (the SDK is synchronous)."""
    sender = _twilio_sender() or {}
    client = get_twilio_client()
    return client.messages.create(to=to, body=body, **sender)


async def send_sms(*, db: Session, company_id: Optional[int], to: str, body: str) -> SMSResult:
    """Send one SMS, enforcing the CUI kill switch first.

    Order of operations matters — the egress gate runs BEFORE the number is
    normalized, the client is built, or anything is logged with the destination:

    1. ``allow_sms_egress`` denied/unresolvable → :class:`SMSEgressDisabledError`
       (nothing leaves the boundary).
    2. Twilio unconfigured → ``SMSResult(status="skipped")``, logged, **no raise**
       (an unconfigured environment must not spam ARQ retries).
    3. Unparseable destination → :class:`InvalidPhoneNumberError` (terminal).
    4. Twilio permanent rejection (4xx, not 429) → :class:`SMSPermanentError`
       (terminal — retrying cannot help).
    5. Any other transport failure → the original exception **propagates**, so the
       enqueuing ARQ job retries.

    Returns the Twilio message SID + status so the caller can record them on the
    ``NotificationLog`` row.
    """
    if not _sms_egress_allowed(db, company_id):
        raise SMSEgressDisabledError(company_id)

    if not sms_configured():
        logger.warning("Twilio not configured; skipping SMS send (company %s)", company_id)
        return SMSResult(status="skipped", reason="not_configured")

    destination = normalize_phone(to)

    try:
        message = await run_in_threadpool(_send_via_twilio, to=destination, body=body)
    except ImportError:
        # Credentials present but the SDK is missing — a deployment defect, not a
        # transport failure. Soft-skip (no retry storm) and log loudly.
        logger.error("twilio package is not installed; skipping SMS send (company %s)", company_id)
        return SMSResult(status="skipped", reason="library_missing")
    except Exception as exc:
        status = getattr(exc, "status", None)
        code = getattr(exc, "code", None)
        if isinstance(status, int) and 400 <= status < 500 and status != 429:
            logger.error(
                "Twilio permanently rejected SMS to %s (company %s, status %s, code %s)",
                mask_phone(destination),
                company_id,
                status,
                code,
            )
            raise SMSPermanentError(str(exc), status=status, code=code) from exc
        logger.warning(
            "Twilio SMS send failed for %s (company %s); will retry",
            mask_phone(destination),
            company_id,
            exc_info=True,
        )
        raise

    sid = getattr(message, "sid", None)
    provider_status = getattr(message, "status", None)
    logger.info(
        "SMS sent to %s (company %s, sid %s, status %s)",
        mask_phone(destination),
        company_id,
        sid,
        provider_status,
    )
    return SMSResult(status="sent", sid=sid, provider_status=provider_status)
