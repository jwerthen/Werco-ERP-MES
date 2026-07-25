"""Job-level coverage for the SMS channel (``app/jobs/sms_jobs.py``, PR 4 / §3.4).

The service tests (tests/services/test_sms_channel.py) prove the *content* rule and the
*gate* in isolation, and the API tests prove the request surface. This file covers the
seam between them -- the ARQ job that actually reaches Twilio -- where a different set of
compliance failures live, because a job runs LATER and ELSEWHERE than the decision to
enqueue it:

* **The egress kill switch is re-checked at SEND time, not enqueue time.** A job queued
  while ``allow_sms_egress`` was ON must NOT send once an admin turns it OFF. This is the
  property that makes the switch a real stop button rather than an advisory flag, and it
  is only observable here (the dispatcher's leg has already run by then).
* **Tenant isolation across the queue boundary.** The job carries ``company_id`` +
  ``user_id`` (never a phone -- PII stays out of Redis) and must refuse to resolve a
  recipient from another tenant, and must never settle another tenant's
  ``NotificationLog`` row.
* **Retry discipline.** Terminal outcomes (egress off, unusable phone, provider 4xx,
  unconfigured) are RECORDED and returned; only a genuine transport failure re-raises so
  ARQ retries. Getting this backwards either burns retries on an outcome that cannot
  improve or silently drops a critical alert.
* **Provider provenance** lands on the pre-created row -- one delivery attempt, one log
  row, with the Twilio SID/status that ties it to the carrier's record (the columns
  migration 073 adds).

No network and no Redis: Twilio is stubbed at ``_send_via_twilio`` and the storm-control
counters at their ``sms_jobs`` import sites.
"""

import asyncio
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session

import app.jobs.sms_jobs as sms_jobs
import app.services.sms_service as sms
from app.models.company import Company
from app.models.notification import NotificationLog
from app.models.user import User, UserRole

pytestmark = [pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2
SID = "SM" + "0" * 32

_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _ensure_company(db: Session, company_id: int, *, allow_sms: bool = True) -> Company:
    company = db.query(Company).filter(Company.id == company_id).first()
    if company is None:
        company = Company(id=company_id, name=f"Co {company_id}", slug=f"smsjob-co-{company_id}", is_active=True)
        db.add(company)
    company.allow_sms_egress = allow_sms
    db.commit()
    return company


def _make_user(
    db: Session,
    *,
    company_id: int = COMPANY_A,
    phone: str = "+15125550134",
    is_active: bool = True,
) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"smsjob-{n}@co{company_id}.test",
        employee_id=f"SMSJOB-{n:05d}",
        first_name="Sms",
        last_name=f"Job{company_id}",
        hashed_password="$2b$12$abcdefghijklmnopqrstuv",
        role=UserRole.QUALITY,
        phone=phone,
        is_active=is_active,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _pending_log(db: Session, *, company_id: int, user_id: int, body: str = "Werco: NCR-9 - NCR created.") -> int:
    """The row the dispatcher creates at enqueue time (``sent=False``). Returns its id."""
    row = NotificationLog(
        company_id=company_id,
        user_id=user_id,
        event_type="ncr.created",
        channel="sms",
        body=body,
        sent=False,
    )
    db.add(row)
    db.commit()
    return row.id


def _use_test_session(db: Session, monkeypatch) -> None:
    """Point the job's own ``SessionLocal`` at the test session (and keep it open)."""
    monkeypatch.setattr(sms_jobs, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)


def _twilio(monkeypatch, *, configured: bool = True, side_effect=None) -> MagicMock:
    """Stub the single Twilio seam. Returns the spy so callers can assert on it."""
    provider = MagicMock(
        return_value=MagicMock(sid=SID, status="queued"),
        side_effect=side_effect,
    )
    monkeypatch.setattr(sms, "_send_via_twilio", provider)
    monkeypatch.setattr(sms, "sms_configured", lambda: configured)
    return provider


def _sms_rows(db: Session, *, company_id: int = None):
    query = db.query(NotificationLog).filter(NotificationLog.channel == "sms")
    if company_id is not None:
        query = query.filter(NotificationLog.company_id == company_id)
    return query.all()


# ---------------------------------------------------------------------------
# The kill switch is re-evaluated at SEND time (§8.5)
# ---------------------------------------------------------------------------


def test_egress_revoked_between_enqueue_and_send_stops_the_message(db_session: Session, monkeypatch):
    """The headline stop-button property.

    The dispatcher enqueued this job while ``allow_sms_egress`` was ON. By the time the
    worker picks it up an admin has turned it OFF -- and nothing may leave the boundary.
    A gate evaluated only at enqueue time would send this message.
    """
    _use_test_session(db_session, monkeypatch)
    user = _make_user(db_session, company_id=COMPANY_A)
    _ensure_company(db_session, COMPANY_A, allow_sms=True)
    log_id = _pending_log(db_session, company_id=COMPANY_A, user_id=user.id)

    # ... admin flips the switch while the job sits in the queue ...
    _ensure_company(db_session, COMPANY_A, allow_sms=False)
    provider = _twilio(monkeypatch)

    result = asyncio.run(
        sms_jobs.send_sms_task(
            company_id=COMPANY_A,
            user_id=user.id,
            body="Werco: NCR-9 - NCR created. Log in to view.",
            notification_log_id=log_id,
            event_type="ncr.created",
        )
    )

    assert result == {"sent": False, "reason": "egress_disabled"}
    provider.assert_not_called()

    db_session.expire_all()
    rows = _sms_rows(db_session)
    # Terminal outcome recorded on the SAME row -- no duplicate, nothing silently dropped.
    assert len(rows) == 1 and rows[0].id == log_id
    assert rows[0].sent is False
    assert "egress is disabled" in (rows[0].error or "")
    assert rows[0].provider_message_id is None and rows[0].provider_status is None


def test_missing_company_row_fails_closed_at_send_time(db_session: Session, monkeypatch):
    """An unresolvable tenant denies -- the gate never assumes "allowed".

    The recipient resolves fine here (the user really is in that company), so the ONLY
    thing standing between the job and Twilio is the egress lookup, which finds no
    ``Company`` row and must therefore deny rather than default to permitted.
    """
    _use_test_session(db_session, monkeypatch)
    orphan_company_id = 987654
    # Deliberately NOT creating the Company row -- e.g. a tenant removed while the job
    # sat in the queue. (SQLite does not enforce the FK, which is what makes the state
    # reproducible here.)
    user = User(
        email=f"smsjob-orphan-{_next()}@orphan.test",
        employee_id=f"SMSORPH-{_next():05d}",
        first_name="Orphan",
        last_name="Tenant",
        hashed_password="$2b$12$abcdefghijklmnopqrstuv",
        role=UserRole.QUALITY,
        phone="+15125550134",
        is_active=True,
        company_id=orphan_company_id,
    )
    db_session.add(user)
    db_session.commit()
    provider = _twilio(monkeypatch)

    result = asyncio.run(sms_jobs.send_sms_task(company_id=orphan_company_id, user_id=user.id, body="x"))

    assert result == {"sent": False, "reason": "egress_disabled"}
    provider.assert_not_called()
    assert "egress is disabled" in (_sms_rows(db_session)[0].error or "")


# ---------------------------------------------------------------------------
# Tenant isolation across the queue boundary
# ---------------------------------------------------------------------------


def test_job_will_not_message_a_recipient_from_another_tenant(db_session: Session, monkeypatch):
    """The recipient lookup is tenant-scoped: a foreign user id resolves to nothing."""
    _use_test_session(db_session, monkeypatch)
    foreign = _make_user(db_session, company_id=COMPANY_B, phone="+15125550199")
    _ensure_company(db_session, COMPANY_A, allow_sms=True)
    provider = _twilio(monkeypatch)

    result = asyncio.run(sms_jobs.send_sms_task(company_id=COMPANY_A, user_id=foreign.id, body="x"))

    assert result == {"sent": False, "reason": "no_recipient_phone"}
    provider.assert_not_called()
    rows = _sms_rows(db_session)
    assert len(rows) == 1
    # The failure row is stamped with the JOB's tenant, never the foreign user's.
    assert rows[0].company_id == COMPANY_A


def test_job_never_settles_another_tenants_log_row(db_session: Session, monkeypatch):
    """``_record_delivery`` filters by ``company_id``: a foreign log id is not touched.

    A mis-routed (or forged) ``notification_log_id`` must not let one tenant stamp
    provider provenance onto another tenant's delivery record.
    """
    _use_test_session(db_session, monkeypatch)
    foreign_user = _make_user(db_session, company_id=COMPANY_B, phone="+15125550199")
    foreign_log_id = _pending_log(db_session, company_id=COMPANY_B, user_id=foreign_user.id)
    mine = _make_user(db_session, company_id=COMPANY_A)
    _ensure_company(db_session, COMPANY_A, allow_sms=True)
    _twilio(monkeypatch)

    result = asyncio.run(
        sms_jobs.send_sms_task(
            company_id=COMPANY_A,
            user_id=mine.id,
            body="Werco: NCR-9 - NCR created. Log in to view.",
            notification_log_id=foreign_log_id,
        )
    )
    assert result["sent"] is True

    db_session.expire_all()
    foreign_row = db_session.query(NotificationLog).filter(NotificationLog.id == foreign_log_id).one()
    assert foreign_row.company_id == COMPANY_B
    assert foreign_row.sent is False, "another tenant's delivery row was mutated"
    assert foreign_row.provider_message_id is None

    # The outcome landed on a NEW row belonging to the job's own tenant instead.
    mine_rows = _sms_rows(db_session, company_id=COMPANY_A)
    assert len(mine_rows) == 1
    assert mine_rows[0].sent is True and mine_rows[0].provider_message_id == SID


def test_deactivated_recipient_is_not_messaged(db_session: Session, monkeypatch):
    """A user deactivated between dispatch and delivery does not get the SMS."""
    _use_test_session(db_session, monkeypatch)
    user = _make_user(db_session, company_id=COMPANY_A, is_active=False)
    _ensure_company(db_session, COMPANY_A, allow_sms=True)
    provider = _twilio(monkeypatch)

    result = asyncio.run(sms_jobs.send_sms_task(company_id=COMPANY_A, user_id=user.id, body="x"))

    assert result == {"sent": False, "reason": "no_recipient_phone"}
    provider.assert_not_called()


def test_phone_cleared_between_enqueue_and_send_is_terminal(db_session: Session, monkeypatch):
    _use_test_session(db_session, monkeypatch)
    user = _make_user(db_session, company_id=COMPANY_A, phone=None)
    _ensure_company(db_session, COMPANY_A, allow_sms=True)
    provider = _twilio(monkeypatch)

    result = asyncio.run(sms_jobs.send_sms_task(company_id=COMPANY_A, user_id=user.id, body="x"))

    assert result == {"sent": False, "reason": "no_recipient_phone"}
    provider.assert_not_called()
    assert "no phone number" in (_sms_rows(db_session)[0].error or "")


# ---------------------------------------------------------------------------
# Outcome recording + retry discipline
# ---------------------------------------------------------------------------


def test_success_settles_the_pending_row_with_provider_provenance(db_session: Session, monkeypatch):
    """One attempt, one row -- carrying the Twilio SID/status (the 073 columns)."""
    _use_test_session(db_session, monkeypatch)
    user = _make_user(db_session, company_id=COMPANY_A)
    _ensure_company(db_session, COMPANY_A, allow_sms=True)
    log_id = _pending_log(db_session, company_id=COMPANY_A, user_id=user.id)
    provider = _twilio(monkeypatch)

    result = asyncio.run(
        sms_jobs.send_sms_task(
            company_id=COMPANY_A,
            user_id=user.id,
            body="Werco: NCR-9 - NCR created. Log in to view.",
            notification_log_id=log_id,
        )
    )

    assert result["sent"] is True and result["sid"] == SID
    # The job re-resolves the number itself; the body is passed through verbatim.
    assert provider.call_args.kwargs["to"] == "+15125550134"
    assert provider.call_args.kwargs["body"] == "Werco: NCR-9 - NCR created. Log in to view."

    db_session.expire_all()
    rows = _sms_rows(db_session)
    assert len(rows) == 1 and rows[0].id == log_id
    assert rows[0].sent is True
    assert rows[0].provider_message_id == SID
    assert rows[0].provider_status == "queued"
    assert rows[0].error is None


def test_transport_failure_records_the_attempt_and_reraises_for_retry(db_session: Session, monkeypatch):
    """A 5xx must reach ARQ as an exception, and still leave a trace in the log."""
    _use_test_session(db_session, monkeypatch)
    user = _make_user(db_session, company_id=COMPANY_A)
    _ensure_company(db_session, COMPANY_A, allow_sms=True)
    log_id = _pending_log(db_session, company_id=COMPANY_A, user_id=user.id)

    transient = RuntimeError("gateway blew up")
    transient.status = 503
    _twilio(monkeypatch, side_effect=transient)

    with pytest.raises(RuntimeError):
        asyncio.run(sms_jobs.send_sms_task(company_id=COMPANY_A, user_id=user.id, body="x", notification_log_id=log_id))

    db_session.expire_all()
    rows = _sms_rows(db_session)
    assert len(rows) == 1 and rows[0].id == log_id
    assert rows[0].sent is False
    assert "will retry" in (rows[0].error or "")


def test_provider_rejection_is_terminal_and_does_not_retry(db_session: Session, monkeypatch):
    """A 4xx cannot improve on retry -- record it (with the provider status) and stop."""
    _use_test_session(db_session, monkeypatch)
    user = _make_user(db_session, company_id=COMPANY_A)
    _ensure_company(db_session, COMPANY_A, allow_sms=True)
    log_id = _pending_log(db_session, company_id=COMPANY_A, user_id=user.id)

    permanent = RuntimeError("unsubscribed recipient")
    permanent.status = 400
    permanent.code = 21610
    _twilio(monkeypatch, side_effect=permanent)

    result = asyncio.run(
        sms_jobs.send_sms_task(company_id=COMPANY_A, user_id=user.id, body="x", notification_log_id=log_id)
    )

    assert result == {"sent": False, "reason": "provider_rejected"}
    db_session.expire_all()
    row = db_session.query(NotificationLog).filter(NotificationLog.id == log_id).one()
    assert row.sent is False
    assert "provider rejected" in (row.error or "")
    assert row.provider_status == "400"
    assert row.provider_message_id is None


def test_unconfigured_twilio_is_a_recorded_skip_not_a_retry(db_session: Session, monkeypatch):
    """An unconfigured environment must not spam ARQ retries."""
    _use_test_session(db_session, monkeypatch)
    user = _make_user(db_session, company_id=COMPANY_A)
    _ensure_company(db_session, COMPANY_A, allow_sms=True)
    log_id = _pending_log(db_session, company_id=COMPANY_A, user_id=user.id)
    _twilio(monkeypatch, configured=False)

    result = asyncio.run(
        sms_jobs.send_sms_task(company_id=COMPANY_A, user_id=user.id, body="x", notification_log_id=log_id)
    )

    assert result["sent"] is False and result["reason"] == "not_configured"
    db_session.expire_all()
    row = db_session.query(NotificationLog).filter(NotificationLog.id == log_id).one()
    assert "skipped: not_configured" in (row.error or "")


# ---------------------------------------------------------------------------
# Storm-control collapse job
# ---------------------------------------------------------------------------


def _patch_counters(monkeypatch, *, pending: int) -> list:
    """Stub the Redis-backed overflow counter. Returns the settle-call recorder."""
    settled: list = []

    async def _peek(user_id):
        return pending

    async def _settle(user_id, count):
        settled.append((user_id, count))

    monkeypatch.setattr(sms_jobs, "peek_sms_overflow", _peek)
    monkeypatch.setattr(sms_jobs, "settle_sms_overflow", _settle)
    return settled


def test_collapse_is_a_noop_when_nothing_was_suppressed(db_session: Session, monkeypatch):
    _use_test_session(db_session, monkeypatch)
    _patch_counters(monkeypatch, pending=0)
    provider = _twilio(monkeypatch)

    result = asyncio.run(sms_jobs.send_sms_overflow_task(company_id=COMPANY_A, user_id=1))

    assert result == {"sent": False, "reason": "nothing_to_collapse"}
    provider.assert_not_called()


def test_collapse_passes_through_the_kill_switch_and_clears_the_burst(db_session: Session, monkeypatch):
    """The collapse bypasses the per-user CAP (it is the cap's safety valve) but NOT the
    egress gate -- and a terminal refusal still settles the counter so the next burst
    starts clean."""
    _use_test_session(db_session, monkeypatch)
    user = _make_user(db_session, company_id=COMPANY_A)
    _ensure_company(db_session, COMPANY_A, allow_sms=False)
    settled = _patch_counters(monkeypatch, pending=4)
    provider = _twilio(monkeypatch)

    result = asyncio.run(sms_jobs.send_sms_overflow_task(company_id=COMPANY_A, user_id=user.id))

    assert result == {"sent": False, "reason": "not_delivered"}
    provider.assert_not_called()
    assert settled == [(user.id, 4)]

    row = db_session.query(NotificationLog).filter(NotificationLog.event_type == sms_jobs.SMS_COLLAPSE_EVENT_TYPE).one()
    assert row.company_id == COMPANY_A
    assert row.sent is False
    assert "not delivered" in (row.error or "")


def test_collapse_sends_one_cui_free_message_and_settles(db_session: Session, monkeypatch):
    _use_test_session(db_session, monkeypatch)
    user = _make_user(db_session, company_id=COMPANY_A)
    _ensure_company(db_session, COMPANY_A, allow_sms=True)
    settled = _patch_counters(monkeypatch, pending=7)
    provider = _twilio(monkeypatch)

    result = asyncio.run(sms_jobs.send_sms_overflow_task(company_id=COMPANY_A, user_id=user.id))

    assert result["sent"] is True and result["collapsed"] == 7
    assert provider.call_count == 1
    # The collapse body carries a count and a pointer -- no identifiers at all.
    assert provider.call_args.kwargs["body"] == "Werco: 7 more alerts - check the app. Log in to view."
    assert settled == [(user.id, 7)]

    row = db_session.query(NotificationLog).filter(NotificationLog.event_type == sms_jobs.SMS_COLLAPSE_EVENT_TYPE).one()
    assert row.sent is True and row.provider_message_id == SID


def test_collapse_transport_failure_keeps_the_counter_for_the_retry(db_session: Session, monkeypatch):
    """Settling before a successful send would lose the suppressed alerts entirely."""
    _use_test_session(db_session, monkeypatch)
    user = _make_user(db_session, company_id=COMPANY_A)
    _ensure_company(db_session, COMPANY_A, allow_sms=True)
    settled = _patch_counters(monkeypatch, pending=3)

    transient = RuntimeError("gateway blew up")
    transient.status = 503
    _twilio(monkeypatch, side_effect=transient)

    with pytest.raises(RuntimeError):
        asyncio.run(sms_jobs.send_sms_overflow_task(company_id=COMPANY_A, user_id=user.id))

    assert settled == [], "the overflow counter must survive so the retry re-reads it"
