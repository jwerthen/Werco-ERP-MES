"""SMS channel coverage (NOTIFICATIONS_PLAN.md §3.4 / §8): CUI-safe bodies, the
fail-closed egress kill switch, storm control, and the dispatcher's SMS leg.

The headline compliance assertions here are:

* the SMS body is built ONLY from the catalog label + a sanitized record identifier --
  free text (customer names, part descriptions, quantities) can never reach Twilio;
* ``Company.allow_sms_egress`` fails CLOSED: unknown tenant, missing tenant context,
  and a DB error all deny, and a denied tenant never reaches the provider;
* the SMS leg fires only for an SMS-ELIGIBLE event the user opted into AND has a phone
  for, and every ``NotificationLog`` row it writes stamps the EVENT's ``company_id``;
* storm control caps per-user sends and collapses the overflow into one message, but
  fails OPEN when Redis is unavailable (a dropped critical alert is the worse failure).

No network and no Redis: Twilio and the Redis pool are stubbed at their single seams.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.orm import Session

import app.services.notification_dispatch as dispatch
import app.services.sms_service as sms
from app.models.company import Company
from app.models.notification import Notification, NotificationLog, NotificationPreference
from app.models.operational_event import OperationalEvent
from app.models.user import User, UserRole
from app.services.notification_catalog import ALL_CHANNELS, CHANNEL_SMS, get_entry
from app.services.notification_dispatch import dispatch_for_event
from app.services.sms_content import build_overflow_sms_body, build_sms_body, safe_identifier

pytestmark = [pytest.mark.requires_db]

_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _ensure_company(db: Session, company_id: int, *, allow_sms: bool = True) -> Company:
    company = db.query(Company).filter(Company.id == company_id).first()
    if company is None:
        company = Company(id=company_id, name=f"Co {company_id}", slug=f"sms-co-{company_id}", is_active=True)
        db.add(company)
    company.allow_sms_egress = allow_sms
    db.commit()
    return company


def _make_user(
    db: Session,
    *,
    company_id: int = 1,
    role: UserRole = UserRole.QUALITY,
    phone: str = "+15125550134",
    is_active: bool = True,
) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"sms-{n}@co{company_id}.test",
        employee_id=f"SMS-{n:05d}",
        first_name="Sms",
        last_name=f"C{company_id}",
        hashed_password="$2b$12$abcdefghijklmnopqrstuv",
        role=role,
        phone=phone,
        is_active=is_active,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _opt_in_sms(db: Session, user: User, event_key: str, *, company_id: int = 1) -> None:
    """Persist an explicit SMS opt-in in the full 4-key channel shape."""
    entry = get_entry(event_key)
    prefs = {channel: channel in entry.default_channels for channel in sorted(ALL_CHANNELS)}
    prefs[CHANNEL_SMS] = True
    row = NotificationPreference(user_id=user.id, preferences={event_key: prefs})
    row.company_id = company_id
    db.add(row)
    db.commit()


def _ncr_event(*, company_id: int = 1, actor_id: int = None, entity_id: int = 555) -> OperationalEvent:
    """An ncr_created event -> ncr.created (SMS-eligible, critical)."""
    return OperationalEvent(
        company_id=company_id,
        event_type="ncr_created",
        source_module="quality",
        entity_type="ncr",
        entity_id=entity_id,
        user_id=actor_id,
        severity="critical",
        event_payload={"ncr_number": f"NCR-{entity_id}"},
    )


def _patch_dispatch_offline(monkeypatch):
    """Stub the dispatcher's Redis touch-points. Returns the enqueue spy."""
    enqueue_spy = AsyncMock()
    monkeypatch.setattr(dispatch, "enqueue_job", enqueue_spy)
    monkeypatch.setattr(dispatch, "_dedup_reserve", AsyncMock(return_value=True))
    monkeypatch.setattr(dispatch, "reserve_sms_quota", AsyncMock(return_value=sms.SMSQuotaDecision(send=True, count=1)))
    return enqueue_spy


# ---------------------------------------------------------------------------
# CUI-safe content rule (§3.4 / §11.1)
# ---------------------------------------------------------------------------


def test_sms_body_is_identifier_plus_label_only():
    body = build_sms_body(label=get_entry("wo.blocker_created").label, identifier="WO-1042")
    assert body == "Werco: WO-1042 - Work order blocked / on hold. Log in to view."
    assert len(body) <= 160


def test_sms_body_without_identifier_still_points_at_the_app():
    body = build_sms_body(label=get_entry("inspection.failed").label)
    assert body == "Werco: Incoming inspection failed. Log in to view."


@pytest.mark.parametrize(
    "hostile",
    [
        "Acme Aerospace bracket, 12 ea",  # customer name + qty + description
        "<script>alert(1)</script>",
        "x" * 60,
        "   ",
    ],
)
def test_non_record_number_identifiers_are_refused(hostile):
    """Anything that is not record-number-shaped is DROPPED, never sent."""
    assert safe_identifier(hostile) is None
    body = build_sms_body(label="NCR created", identifier=hostile)
    if hostile.strip():
        assert hostile.strip() not in body
    assert body == "Werco: NCR created. Log in to view."


def test_sms_body_is_capped_to_one_segment():
    body = build_sms_body(label="x" * 400, identifier="WO-1")
    assert len(body) == 160
    assert body.endswith("Log in to view.")


def test_overflow_body_carries_no_identifiers():
    assert build_overflow_sms_body(7) == "Werco: 7 more alerts - check the app. Log in to view."
    assert build_overflow_sms_body(1).startswith("Werco: 1 more alert ")


# ---------------------------------------------------------------------------
# Fail-closed egress gate (§8.5)
# ---------------------------------------------------------------------------


def test_egress_allowed_when_company_opted_in(db_session: Session):
    _ensure_company(db_session, 1, allow_sms=True)
    assert sms._sms_egress_allowed(db_session, 1) is True


def test_egress_denied_when_company_opted_out(db_session: Session):
    _ensure_company(db_session, 1, allow_sms=False)
    assert sms._sms_egress_allowed(db_session, 1) is False


def test_egress_denied_for_unknown_tenant(db_session: Session):
    assert sms._sms_egress_allowed(db_session, 987654) is False


def test_egress_denied_without_company_context(db_session: Session):
    """Stricter than the AI gate on purpose: every SMS caller has a tenant."""
    assert sms._sms_egress_allowed(db_session, None) is False


def test_egress_denied_when_the_lookup_raises():
    broken = MagicMock()
    broken.query.side_effect = RuntimeError("db down")
    assert sms._sms_egress_allowed(broken, 1) is False


def test_send_sms_refuses_before_touching_twilio(db_session: Session, monkeypatch):
    _ensure_company(db_session, 1, allow_sms=False)
    provider = MagicMock()
    monkeypatch.setattr(sms, "_send_via_twilio", provider)
    monkeypatch.setattr(sms, "sms_configured", lambda: True)

    with pytest.raises(sms.SMSEgressDisabledError):
        asyncio.run(sms.send_sms(db=db_session, company_id=1, to="+15125550134", body="x"))

    provider.assert_not_called()


def test_send_sms_soft_skips_when_unconfigured(db_session: Session, monkeypatch):
    """Unconfigured must NOT raise -- an ARQ retry storm would be the wrong answer."""
    _ensure_company(db_session, 1, allow_sms=True)
    monkeypatch.setattr(sms, "sms_configured", lambda: False)
    result = asyncio.run(sms.send_sms(db=db_session, company_id=1, to="+15125550134", body="x"))
    assert result.status == "skipped"
    assert result.reason == "not_configured"
    assert result.sent is False


def test_send_sms_records_sid_and_status(db_session: Session, monkeypatch):
    _ensure_company(db_session, 1, allow_sms=True)
    monkeypatch.setattr(sms, "sms_configured", lambda: True)
    monkeypatch.setattr(
        sms,
        "_send_via_twilio",
        MagicMock(return_value=MagicMock(sid="SM" + "0" * 32, status="queued")),
    )
    result = asyncio.run(sms.send_sms(db=db_session, company_id=1, to="512-555-0134", body="x"))
    assert result.sent is True
    assert result.sid == "SM" + "0" * 32
    assert result.provider_status == "queued"


def test_provider_4xx_is_terminal_and_5xx_retries(db_session: Session, monkeypatch):
    _ensure_company(db_session, 1, allow_sms=True)
    monkeypatch.setattr(sms, "sms_configured", lambda: True)

    permanent = RuntimeError("bad number")
    permanent.status = 400
    permanent.code = 21211
    monkeypatch.setattr(sms, "_send_via_twilio", MagicMock(side_effect=permanent))
    with pytest.raises(sms.SMSPermanentError):
        asyncio.run(sms.send_sms(db=db_session, company_id=1, to="+15125550134", body="x"))

    transient = RuntimeError("gateway blew up")
    transient.status = 503
    monkeypatch.setattr(sms, "_send_via_twilio", MagicMock(side_effect=transient))
    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(sms.send_sms(db=db_session, company_id=1, to="+15125550134", body="x"))
    assert not isinstance(excinfo.value, sms.SMSPermanentError)


# ---------------------------------------------------------------------------
# Phone normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["512-555-0134", "(512) 555-0134", "+1 512 555 0134", "5125550134"])
def test_phone_normalizes_to_e164(raw):
    assert sms.normalize_phone(raw) == "+15125550134"


@pytest.mark.parametrize("raw", ["", None, "12345", "not-a-phone", "+1 555 555 5555"])
def test_invalid_phone_is_rejected(raw):
    with pytest.raises(sms.InvalidPhoneNumberError):
        sms.normalize_phone(raw)


# ---------------------------------------------------------------------------
# Storm control (§3.4)
# ---------------------------------------------------------------------------


class _FakeRedis:
    def __init__(self):
        self.values = {}
        self.set_calls = []

    async def incr(self, key):
        self.values[key] = int(self.values.get(key, 0)) + 1
        return self.values[key]

    async def decrby(self, key, amount):
        self.values[key] = int(self.values.get(key, 0)) - amount
        return self.values[key]

    async def expire(self, key, seconds):
        return True

    async def get(self, key):
        return self.values.get(key)

    async def delete(self, key):
        self.values.pop(key, None)

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.values:
            return None
        self.values[key] = value
        self.set_calls.append(key)
        return True


def test_quota_caps_then_collapses(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(sms, "get_redis_pool", AsyncMock(return_value=fake))

    decisions = [asyncio.run(sms.reserve_sms_quota(42)) for _ in range(sms.SMS_HOURLY_CAP_PER_USER + 3)]

    assert all(d.send for d in decisions[: sms.SMS_HOURLY_CAP_PER_USER])
    assert not any(d.send for d in decisions[sms.SMS_HOURLY_CAP_PER_USER :])
    # The collapse message is armed EXACTLY once for the burst.
    assert sum(1 for d in decisions if d.arm_collapse) == 1
    # ... and the suppressed alerts are counted for it.
    assert asyncio.run(sms.peek_sms_overflow(42)) == 3


def test_settle_decrements_so_late_arrivals_survive(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(sms, "get_redis_pool", AsyncMock(return_value=fake))
    for _ in range(sms.SMS_HOURLY_CAP_PER_USER + 3):
        asyncio.run(sms.reserve_sms_quota(43))

    # A collapse summarizes 3, then one more alert is suppressed while it was in flight.
    asyncio.run(sms.settle_sms_overflow(43, 3))
    assert asyncio.run(sms.peek_sms_overflow(43)) == 0
    asyncio.run(sms.reserve_sms_quota(43))
    assert asyncio.run(sms.peek_sms_overflow(43)) == 1


def test_quota_fails_open_when_redis_is_down(monkeypatch):
    """Deliberate trade-off: an extra SMS beats dropping a critical quality alert."""
    monkeypatch.setattr(sms, "get_redis_pool", AsyncMock(side_effect=RuntimeError("redis down")))
    decision = asyncio.run(sms.reserve_sms_quota(44))
    assert decision.send is True
    assert decision.arm_collapse is False


# ---------------------------------------------------------------------------
# Dispatcher SMS leg
# ---------------------------------------------------------------------------


def _sms_logs(db: Session):
    return db.query(NotificationLog).filter(NotificationLog.channel == CHANNEL_SMS).all()


def test_sms_leg_requires_explicit_opt_in(db_session: Session, monkeypatch):
    """No catalog event ships SMS by default, so defaults alone must send nothing."""
    enqueue_spy = _patch_dispatch_offline(monkeypatch)
    _make_user(db_session, company_id=1, role=UserRole.QUALITY)

    asyncio.run(dispatch_for_event(db_session, _ncr_event(company_id=1)))
    db_session.flush()

    assert _sms_logs(db_session) == []
    assert all(call.args[0] != "send_sms_job" for call in enqueue_spy.await_args_list)


def test_sms_leg_fires_for_opted_in_user_with_a_phone(db_session: Session, monkeypatch):
    enqueue_spy = _patch_dispatch_offline(monkeypatch)
    user = _make_user(db_session, company_id=1, role=UserRole.QUALITY)
    _opt_in_sms(db_session, user, "ncr.created")

    asyncio.run(dispatch_for_event(db_session, _ncr_event(company_id=1, entity_id=555)))
    db_session.flush()

    logs = _sms_logs(db_session)
    assert len(logs) == 1
    log = logs[0]
    assert log.user_id == user.id
    # company_id is stamped from the EVENT, never from the recipient.
    assert log.company_id == 1
    assert log.sent is False  # the job settles it with the Twilio SID/status
    assert log.body == "Werco: NCR-555 - NCR created. Log in to view."
    # Linked to the in-app inbox row for the same event.
    inbox = db_session.query(Notification).filter(Notification.user_id == user.id).one()
    assert log.notification_id == inbox.id

    sms_jobs = [call for call in enqueue_spy.await_args_list if call.args[0] == "send_sms_job"]
    assert len(sms_jobs) == 1
    kwargs = sms_jobs[0].kwargs
    assert kwargs["company_id"] == 1
    assert kwargs["user_id"] == user.id
    assert kwargs["notification_log_id"] == log.id
    # PII stays out of the Redis job payload -- the job re-resolves the phone.
    assert "phone" not in kwargs and user.phone not in str(kwargs)


def test_sms_toggle_is_inert_without_a_phone(db_session: Session, monkeypatch):
    enqueue_spy = _patch_dispatch_offline(monkeypatch)
    user = _make_user(db_session, company_id=1, role=UserRole.QUALITY, phone=None)
    _opt_in_sms(db_session, user, "ncr.created")

    asyncio.run(dispatch_for_event(db_session, _ncr_event(company_id=1)))
    db_session.flush()

    assert _sms_logs(db_session) == []
    assert all(call.args[0] != "send_sms_job" for call in enqueue_spy.await_args_list)


def test_sms_leg_is_tenant_isolated(db_session: Session, monkeypatch):
    """A foreign-tenant opted-in user with the same role receives nothing."""
    _patch_dispatch_offline(monkeypatch)
    mine = _make_user(db_session, company_id=1, role=UserRole.QUALITY)
    theirs = _make_user(db_session, company_id=2, role=UserRole.QUALITY)
    _opt_in_sms(db_session, mine, "ncr.created", company_id=1)
    _opt_in_sms(db_session, theirs, "ncr.created", company_id=2)

    asyncio.run(dispatch_for_event(db_session, _ncr_event(company_id=1)))
    db_session.flush()

    logs = _sms_logs(db_session)
    assert [log.user_id for log in logs] == [mine.id]
    assert all(log.company_id == 1 for log in logs)


def test_over_cap_is_recorded_as_suppressed_and_arms_the_collapse(db_session: Session, monkeypatch):
    enqueue_spy = _patch_dispatch_offline(monkeypatch)
    monkeypatch.setattr(
        dispatch,
        "reserve_sms_quota",
        AsyncMock(return_value=sms.SMSQuotaDecision(send=False, arm_collapse=True, count=6)),
    )
    user = _make_user(db_session, company_id=1, role=UserRole.QUALITY)
    _opt_in_sms(db_session, user, "ncr.created")

    asyncio.run(dispatch_for_event(db_session, _ncr_event(company_id=1)))
    db_session.flush()

    log = _sms_logs(db_session)[0]
    assert log.sent is False
    assert "suppressed" in (log.error or "")
    # One collapse job, no individual send job.
    job_names = [call.args[0] for call in enqueue_spy.await_args_list]
    assert "send_sms_overflow_job" in job_names
    assert "send_sms_job" not in job_names


# ---------------------------------------------------------------------------
# The CUI content rule holds THROUGH the dispatcher, not just in sms_content
# ---------------------------------------------------------------------------


def test_a_free_text_identifier_never_reaches_the_sms_body(db_session: Session, monkeypatch):
    """Defense in depth: the sanitizer is what protects the boundary, not the payload.

    The in-app/email title is composed from the payload identifier verbatim -- that is
    fine, it stays inside the CUI boundary. The SMS body is built independently and
    sanitizes the same value, so a payload key that someday carries a description
    instead of a record number degrades the SMS to the bare catalog label rather than
    shipping customer/part/quantity detail to Twilio.
    """
    _patch_dispatch_offline(monkeypatch)
    user = _make_user(db_session, company_id=1, role=UserRole.QUALITY)
    _opt_in_sms(db_session, user, "ncr.created")

    leaky = "Acme Aerospace bracket, 12 ea"
    event = _ncr_event(company_id=1)
    event.event_payload = {"ncr_number": leaky}

    asyncio.run(dispatch_for_event(db_session, event))
    db_session.flush()

    log = _sms_logs(db_session)[0]
    assert log.body == "Werco: NCR created. Log in to view."
    assert leaky not in log.body
    assert "Acme" not in log.body and "12 ea" not in log.body
    # The in-app row (behind the login) may still carry it -- that is the whole point of
    # the "log in to view" pointer, and it proves the SMS sanitization is independent.
    inbox = db_session.query(Notification).filter(Notification.user_id == user.id).one()
    assert leaky in inbox.title


def test_dispatch_direct_never_borrows_the_title_for_the_sms_body(db_session: Session, monkeypatch):
    """Direct callers (crons/MRP) compose ``title``/``body`` freely.

    An SMS-eligible direct dispatch that passes no ``sms_identifier`` must send the
    catalog label alone -- never the caller's free-text title (§3.4 / §11.1).
    """
    _patch_dispatch_offline(monkeypatch)
    user = _make_user(db_session, company_id=1, role=UserRole.QUALITY)
    _opt_in_sms(db_session, user, "quality.hold")

    asyncio.run(
        dispatch.dispatch_direct(
            db_session,
            event_key="quality.hold",
            company_id=1,
            recipients=[user],
            title="Quality hold: Acme Aerospace bracket 55-2210 (12 ea) failed flatness",
            body="Operator J. Ruiz held 12 ea of 55-2210 for Acme Aerospace.",
            commit=False,
        )
    )
    db_session.flush()

    log = _sms_logs(db_session)[0]
    assert log.body == "Werco: Quality hold raised. Log in to view."
    for leaked in ("Acme", "55-2210", "12 ea", "Ruiz", "flatness"):
        assert leaked not in log.body


def test_sms_leg_still_fires_when_redis_is_down(db_session: Session, monkeypatch):
    """Storm control fails OPEN end-to-end: a Redis outage must not swallow an alert.

    Exercises the REAL ``reserve_sms_quota`` (and the real dedup reservation) with the
    pool unavailable, so this covers the dispatcher's behavior rather than the service
    function in isolation: the send job is still enqueued, nothing raises, and the
    delivery row is not marked suppressed.
    """
    enqueue_spy = AsyncMock()
    monkeypatch.setattr(dispatch, "enqueue_job", enqueue_spy)
    down = AsyncMock(side_effect=RuntimeError("redis down"))
    monkeypatch.setattr(dispatch, "get_redis_pool", down)
    monkeypatch.setattr(sms, "get_redis_pool", down)

    user = _make_user(db_session, company_id=1, role=UserRole.QUALITY)
    _opt_in_sms(db_session, user, "ncr.created")

    asyncio.run(dispatch_for_event(db_session, _ncr_event(company_id=1)))
    db_session.flush()

    log = _sms_logs(db_session)[0]
    assert log.sent is False and not log.error, "a Redis outage must not record a suppression"
    assert [call.args[0] for call in enqueue_spy.await_args_list].count("send_sms_job") == 1
