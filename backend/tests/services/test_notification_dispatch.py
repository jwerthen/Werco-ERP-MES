"""Dispatch-core coverage: tenant isolation, preference resolution, actor exclusion,
is_active filtering, and recurring re-notify suppression (NOTIFICATIONS_PLAN.md §8,
PR1_DESIGN_SPEC.md §C/§D).

The dispatcher runs in the ARQ worker with NO request-scoped tenant protection, so these
are the headline compliance tests:

* every recipient-resolution source filters by the triggering event's ``company_id`` and
  ``User.is_active`` -- a foreign-company user (even one whose id collides with the event's
  ``related_id``) receives NOTHING;
* every row written (``Notification`` / ``NotificationLog`` / ``DigestQueue``) stamps
  ``company_id`` from the event;
* preferences resolve IN MEMORY with no row auto-create (§9.8) -- a user with no
  ``NotificationPreference`` row must not raise, must not get a row created, and gets catalog
  defaults; a partial row is honored; the mandatory channel is forced on regardless;
* the actor is never notified of their own action;
* a recurring event suppresses a second in-app row while an unread one exists;
* the email leg never records a delivery it did not attempt (§7): a recipient whose stored
  address is a placeholder this system minted (badge-only accounts, legacy ``.local``
  imports) is not enqueued, its ``NotificationLog`` row is written ``sent=False`` with the
  cause in ``error``, and where the event's MANDATORY channel is EMAIL the in-app channel
  is forced on so the "can never be fully muted" guarantee still lands somewhere.

Redis is never touched: ``enqueue_job`` (email) and ``_dedup_reserve`` are stubbed.
"""

import asyncio
import inspect
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.orm import Session

import app.services.notification_dispatch as dispatch
from app.api.endpoints.users import _effective_preferences
from app.core.security import create_access_token
from app.models.company import Company
from app.models.notification import DigestQueue, Notification, NotificationLog, NotificationPreference
from app.models.operational_event import OperationalEvent
from app.models.user import User, UserRole
from app.services.notification_catalog import ALL_CHANNELS, CATALOG, CHANNEL_EMAIL, CHANNEL_IN_APP, get_entry
from app.services.notification_dispatch import dispatch_direct, dispatch_for_event
from app.services.user_identity import LEGACY_RESERVED_EMAIL_DOMAIN, SYNTHETIC_EMAIL_DOMAIN

pytestmark = [pytest.mark.requires_db]

_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _ensure_company(db: Session, company_id: int) -> None:
    if not db.query(Company).filter(Company.id == company_id).first():
        db.add(Company(id=company_id, name=f"Co {company_id}", slug=f"disp-co-{company_id}", is_active=True))
        db.commit()


def _make_user(
    db: Session,
    *,
    company_id: int = 1,
    role: UserRole = UserRole.QUALITY,
    department: str = None,
    is_active: bool = True,
    email: str = None,
) -> User:
    """``email`` is explicit only for §7 (deliverability); the default is a per-user
    address at a fabricated but ORDINARY domain, so every other test in this file keeps
    exercising the deliverable path exactly as it did before that section existed."""
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=email or f"disp-{n}@co{company_id}.test",
        employee_id=f"DISP-{n:05d}",
        first_name="Disp",
        last_name=f"C{company_id}",
        hashed_password="$2b$12$abcdefghijklmnopqrstuv",
        role=role,
        department=department,
        is_active=is_active,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _ncr_event(*, company_id: int = 1, actor_id: int = None, entity_id: int = 777) -> OperationalEvent:
    """An in-memory ncr_created event (-> ncr.created: roles QUALITY/MANAGER + dept Quality,
    default channels in_app+email, mandatory in_app)."""
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


def _patch_no_redis(monkeypatch):
    """Stub the two Redis touch-points so dispatch runs fully offline. Returns the
    email-enqueue spy."""
    email_spy = AsyncMock()
    monkeypatch.setattr(dispatch, "enqueue_job", email_spy)
    monkeypatch.setattr(dispatch, "_dedup_reserve", AsyncMock(return_value=True))
    return email_spy


def _notifs_for(db: Session, user_id: int, event_key: str = "ncr.created"):
    return db.query(Notification).filter(Notification.user_id == user_id, Notification.event_key == event_key).all()


# ---------------------------------------------------------------------------
# 2. Tenant isolation (the headline compliance test)
# ---------------------------------------------------------------------------


def test_dispatcher_is_tenant_isolated(db_session: Session, monkeypatch):
    email_spy = _patch_no_redis(monkeypatch)

    c1_quality = _make_user(db_session, company_id=1, role=UserRole.QUALITY)
    c1_manager = _make_user(db_session, company_id=1, role=UserRole.MANAGER)
    actor = _make_user(db_session, company_id=1, role=UserRole.ADMIN)  # not a recipient of ncr.created
    c1_inactive = _make_user(db_session, company_id=1, role=UserRole.QUALITY, is_active=False)

    # Company 2 users -- one whose id is FORCED to collide with the event's related_id
    # to prove an id collision never leaks across tenants.
    c2_quality = _make_user(db_session, company_id=2, role=UserRole.QUALITY)
    c2_manager = _make_user(db_session, company_id=2, role=UserRole.MANAGER)

    event = _ncr_event(company_id=1, actor_id=actor.id, entity_id=c2_quality.id)

    created = asyncio.run(dispatch_for_event(db_session, event))
    # dispatch_for_event flushes lazily and does NOT commit (the ARQ task commits); flush so
    # the trailing NotificationLog/DigestQueue rows are queryable in this session.
    db_session.flush()

    # Exactly the two active company-1 recipients (Quality + Manager) got in-app rows.
    assert created == 2
    assert len(_notifs_for(db_session, c1_quality.id)) == 1
    assert len(_notifs_for(db_session, c1_manager.id)) == 1

    # Nobody else: actor (own action), inactive user, or ANY company-2 user.
    for uid in (actor.id, c1_inactive.id, c2_quality.id, c2_manager.id):
        assert _notifs_for(db_session, uid) == []

    # Every written row across all three tables is stamped company_id == 1; none for company 2.
    for model in (Notification, NotificationLog, DigestQueue):
        assert db_session.query(model).filter(model.company_id == 2).count() == 0
        rows = db_session.query(model).all()
        assert all(r.company_id == 1 for r in rows), f"{model.__name__} leaked a non-tenant company_id"

    # Email leg fired only for the company-1 recipients (both have emails).
    assert email_spy.await_count == 2
    logged_users = {log.user_id for log in db_session.query(NotificationLog).all()}
    assert logged_users == {c1_quality.id, c1_manager.id}


# ---------------------------------------------------------------------------
# 3. Preference resolution WITHOUT a row (§9.8) + partial row + mandatory forced
# ---------------------------------------------------------------------------


def test_no_pref_row_uses_defaults_and_creates_no_row(db_session: Session, monkeypatch):
    _patch_no_redis(monkeypatch)
    user = _make_user(db_session, company_id=1, role=UserRole.QUALITY)
    assert db_session.query(NotificationPreference).count() == 0

    event = _ncr_event(company_id=1, actor_id=None)
    asyncio.run(dispatch_for_event(db_session, event))

    # No IntegrityError, catalog defaults applied (an in-app row exists) ...
    assert len(_notifs_for(db_session, user.id)) == 1
    # ... and CRUCIALLY no NotificationPreference row was auto-created (§9.8 defect).
    assert db_session.query(NotificationPreference).count() == 0


def test_resolve_channels_defaults_when_no_row(db_session: Session):
    user = _make_user(db_session, company_id=1, role=UserRole.QUALITY)
    channels = dispatch.resolve_channels(db_session, user, get_entry("ncr.created"))
    assert channels == {CHANNEL_IN_APP, CHANNEL_EMAIL}
    assert db_session.query(NotificationPreference).count() == 0


def test_resolve_channels_honors_partial_row_and_forces_mandatory(db_session: Session):
    """A saved row disabling the mandatory in_app channel is overridden ON; the user's
    other saved choices are honored."""
    user = _make_user(db_session, company_id=1, role=UserRole.QUALITY)
    db_session.add(
        NotificationPreference(
            user_id=user.id,
            company_id=1,
            preferences={"ncr.created": {"email": True, "in_app": False, "sms": False, "digest": False}},
        )
    )
    db_session.commit()

    channels = dispatch.resolve_channels(db_session, user, get_entry("ncr.created"))
    # email kept (saved True); in_app forced on despite the saved False (mandatory).
    assert channels == {CHANNEL_EMAIL, CHANNEL_IN_APP}


def test_resolve_channels_saved_disable_of_non_mandatory_is_respected(db_session: Session):
    """wo.released has no mandatory channel, so a user who turns everything off gets
    nothing -- proving saved channels win when not overridden by a mandatory flag."""
    user = _make_user(db_session, company_id=1, role=UserRole.SUPERVISOR)
    db_session.add(
        NotificationPreference(
            user_id=user.id,
            company_id=1,
            preferences={"wo.released": {"in_app": False, "email": False, "sms": False, "digest": False}},
        )
    )
    db_session.commit()

    channels = dispatch.resolve_channels(db_session, user, get_entry("wo.released"))
    assert channels == set()


# ---------------------------------------------------------------------------
# 4. Actor exclusion
# ---------------------------------------------------------------------------


def test_actor_is_never_notified_of_own_action(db_session: Session, monkeypatch):
    _patch_no_redis(monkeypatch)
    actor_manager = _make_user(db_session, company_id=1, role=UserRole.MANAGER)  # would match by role
    other_quality = _make_user(db_session, company_id=1, role=UserRole.QUALITY)

    event = _ncr_event(company_id=1, actor_id=actor_manager.id)
    asyncio.run(dispatch_for_event(db_session, event))

    assert _notifs_for(db_session, actor_manager.id) == [], "actor must not be notified of their own action"
    assert len(_notifs_for(db_session, other_quality.id)) == 1


# ---------------------------------------------------------------------------
# 5. is_active filter
# ---------------------------------------------------------------------------


def test_deactivated_recipient_gets_nothing(db_session: Session, monkeypatch):
    _patch_no_redis(monkeypatch)
    inactive = _make_user(db_session, company_id=1, role=UserRole.QUALITY, is_active=False)
    active = _make_user(db_session, company_id=1, role=UserRole.QUALITY, is_active=True)

    event = _ncr_event(company_id=1, actor_id=None)
    asyncio.run(dispatch_for_event(db_session, event))

    assert _notifs_for(db_session, inactive.id) == []
    assert len(_notifs_for(db_session, active.id)) == 1
    # No NotificationLog (email) row for the inactive user either.
    assert db_session.query(NotificationLog).filter(NotificationLog.user_id == inactive.id).count() == 0


# ---------------------------------------------------------------------------
# 6. Recurring re-notify suppression
# ---------------------------------------------------------------------------


def test_recurring_event_suppresses_second_in_app_until_read(db_session: Session, monkeypatch):
    """wo.late is a recurring detector: while an unread in-app row for the same
    (event_key, entity, user) exists, a second dispatch creates NO new in-app row; once
    read, a new one is allowed again."""
    _patch_no_redis(monkeypatch)
    user = _make_user(db_session, company_id=1, role=UserRole.SUPERVISOR)

    def _dispatch_late():
        return asyncio.run(
            dispatch_direct(
                db_session,
                event_key="wo.late",
                company_id=1,
                recipients=[user],
                related_type="WorkOrder",
                related_id=555,
                title="Work Order WO-555 is 3 days late",
                body="This work order is past its due date.",
                link="/work-orders/555",
            )
        )

    def _late_rows():
        return (
            db_session.query(Notification)
            .filter(
                Notification.user_id == user.id,
                Notification.event_key == "wo.late",
                Notification.related_id == 555,
            )
            .all()
        )

    # First dispatch -> one inbox row.
    _dispatch_late()
    assert len(_late_rows()) == 1

    # Second dispatch while unread -> suppressed (still one row).
    _dispatch_late()
    assert len(_late_rows()) == 1

    # Mark it read, then a third dispatch is allowed to create a fresh row.
    rows = _late_rows()
    rows[0].is_read = True
    db_session.commit()
    _dispatch_late()
    assert len(_late_rows()) == 2


def test_dispatch_direct_uncataloged_key_is_safe_noop(db_session: Session, monkeypatch):
    _patch_no_redis(monkeypatch)
    user = _make_user(db_session, company_id=1, role=UserRole.SUPERVISOR)
    created = asyncio.run(
        dispatch_direct(
            db_session,
            event_key="not.a.real.key",
            company_id=1,
            recipients=[user],
            related_type="WorkOrder",
            related_id=1,
            title="x",
        )
    )
    assert created == 0
    assert db_session.query(Notification).count() == 0


# ---------------------------------------------------------------------------
# 7. The email leg never records a delivery it did not attempt
# ---------------------------------------------------------------------------
#
# ``User.email`` is NOT NULL, so an account that has no address still holds one: a
# placeholder this system minted itself on a domain it owns and never delivers to
# (``user_identity.is_synthetic_email`` -- ``@users.werco.com`` for badge-only signups,
# ``@werco.local`` for legacy imports that have not logged in since). It is syntactically
# valid and non-empty, so every truthiness check the dispatcher had passed it straight to
# send_email_job, and the leg then wrote ``sent=True``.
#
# WHY THE ROW MUST STILL EXIST, AND WHY IT MUST SAY sent=False. ``notification_logs`` is
# what an auditor reconstructs "who was notified of NCR-1234, and when" from. Two wrong
# fixes are available and both are worse than the defect looks:
#
#   * ``sent=True`` (the old behavior) is evidence that a control operated when it did
#     not. That is the kind of artifact that ENDS an inquiry ("they were told") rather
#     than starting one.
#   * writing NOTHING is indistinguishable, from the recipient's side, from a
#     notification that was never raised -- and it hides which accounts need a real
#     address, since the whole population is silent.
#
# So the tests below assert the row EXISTS and carries the content, with sent False and a
# non-empty cause. A test that only asserted "no email was enqueued" would pass against
# the silent-drop fix, which is why every case here checks the row too.


def _logs_for(db: Session, user_id: int):
    return db.query(NotificationLog).filter(NotificationLog.user_id == user_id).all()


def _dispatch_ncr(db: Session, recipients, *, related_id: int = 901):
    return asyncio.run(
        dispatch_direct(
            db,
            event_key="ncr.created",
            company_id=1,
            recipients=list(recipients),
            related_type="ncr",
            related_id=related_id,
            title=f"NCR-{related_id} created",
            body="A nonconformance was raised.",
            link="/quality?tab=ncr",
        )
    )


def test_a_placeholder_address_is_not_enqueued_and_its_row_says_so(db_session: Session, monkeypatch):
    """Both placeholder domains, alongside a real one in the SAME fan-out.

    Driving all three through one dispatch is the point: the deliverability decision is
    per-recipient, and a fix that short-circuited the whole leg (or that keyed off the
    first recipient) would still satisfy a single-user test.
    """
    email_spy = _patch_no_redis(monkeypatch)

    real = _make_user(db_session, company_id=1, email="real.person@wercomfg.com")
    badge_only = _make_user(db_session, company_id=1, email=f"emp-1234@{SYNTHETIC_EMAIL_DOMAIN}")
    legacy = _make_user(db_session, company_id=1, email=f"old.import@{LEGACY_RESERVED_EMAIL_DOMAIN}")

    _dispatch_ncr(db_session, [real, badge_only, legacy])
    db_session.flush()

    # Exactly ONE address was handed to the mail job, and it is the real one.
    assert email_spy.await_count == 1
    assert email_spy.await_args.kwargs["to"] == "real.person@wercomfg.com"

    for user in (badge_only, legacy):
        rows = _logs_for(db_session, user.id)
        assert len(rows) == 1, f"the undeliverable row must EXIST for {user.email}, not be skipped"
        row = rows[0]
        assert row.sent is False, f"{user.email} was recorded as sent"
        assert row.error, "an undeliverable row must name its cause"
        assert "deliverable" in row.error.lower()
        # The notification really was raised -- the row keeps its content, so the record
        # shows WHAT this person was not told, not merely that something happened.
        assert row.subject == "NCR-901 created"
        assert row.body == "A nonconformance was raised."
        assert row.channel == CHANNEL_EMAIL
        assert row.company_id == 1
        assert row.related_type == "ncr" and row.related_id == 901

    # The in-app leg is untouched: an undeliverable address is not an undeliverable
    # PERSON, and ncr.created's in-app channel is on by default for all three.
    for user in (real, badge_only, legacy):
        assert len(_notifs_for(db_session, user.id)) == 1


def test_a_real_address_is_enqueued_and_logged_exactly_as_before(db_session: Session, monkeypatch):
    """The regression half: for an ordinary address nothing about this leg changed.

    ``sent=True`` still records the ENQUEUE (not confirmed SMTP delivery) and ``error``
    stays NULL, and the row still links to the in-app row created in the same pass -- the
    ``notification_id`` linkage is easy to drop when a branch is threaded through here.
    """
    email_spy = _patch_no_redis(monkeypatch)
    user = _make_user(db_session, company_id=1, email="ordinary@wercomfg.com")

    _dispatch_ncr(db_session, [user], related_id=902)
    db_session.flush()

    assert email_spy.await_count == 1
    assert email_spy.await_args.kwargs["to"] == "ordinary@wercomfg.com"

    (row,) = _logs_for(db_session, user.id)
    assert row.sent is True
    assert row.error is None
    (inbox,) = _notifs_for(db_session, user.id)
    assert row.notification_id == inbox.id


# --- §8.9: a mandatory channel that cannot be delivered must fall back ------
#
# ``channels_from_pref`` forces ``entry.mandatory_channel`` on so a critical event "can
# never be fully muted". For ``account.locked`` that channel is EMAIL -- so for a
# badge-only recipient the guarantee resolved to a mail job for a mailbox that does not
# exist, i.e. to nothing at all. The person whose account just locked is precisely the
# person who cannot be reached any other way.


def test_the_mandatory_email_and_in_app_entries_are_the_ones_these_tests_assume(db_session: Session):
    """The catalog partition, read from the catalog rather than written down.

    The fallback below is scoped to ``mandatory_channel == EMAIL``. If a second EMAIL-
    mandatory entry is ever added, its recipients inherit that behavior untested, and if
    an IN_APP-mandatory entry is switched to EMAIL the next test stops covering the
    entries it names. Both show up here first.

    (The count in ``_fan_out``'s comment says "six" IN_APP-mandatory entries; there are
    five. Asserting the SET, not a number, is what keeps this test honest either way.)
    """
    by_channel = {}
    for key, entry in CATALOG.items():
        if entry.mandatory_channel:
            by_channel.setdefault(entry.mandatory_channel, set()).add(key)

    assert by_channel[CHANNEL_EMAIL] == {"account.locked"}
    assert by_channel[CHANNEL_IN_APP] == {
        "wo.blocker_created",
        "ncr.created",
        "quality.hold",
        "inspection.failed",
        "comment.mention",
    }


def _mute_everything(db: Session, user: User, *event_keys: str) -> None:
    """A saved preference row turning every channel OFF for each named event."""
    db.add(
        NotificationPreference(
            user_id=user.id,
            company_id=user.company_id,
            preferences={key: {"in_app": False, "email": False, "sms": False, "digest": False} for key in event_keys},
        )
    )
    db.commit()


def _dispatch_account_locked(db: Session, user: User):
    return asyncio.run(
        dispatch_direct(
            db,
            event_key="account.locked",
            company_id=1,
            recipients=[user],
            related_type="user",
            related_id=user.id,
            title="Account locked",
            body="An account was locked after repeated failed logins.",
        )
    )


def test_mandatory_email_falls_back_to_in_app_when_the_address_is_a_placeholder(db_session: Session, monkeypatch):
    """§8.9 for a badge-only admin who has turned everything off.

    Without the fallback this resolves to {EMAIL} -> an enqueue to nowhere -> an inbox
    with nothing in it.

    The baseline assertion on ``resolve_channels`` pins the DEFAULT, and it is worth
    reading carefully because the same line used to mean the opposite thing. The fallback
    now lives in ``channels_from_pref`` -- the shared resolver -- and not in the fan-out,
    because the settings API renders from that same function and the two disagreed for
    exactly the recipients the fallback protects (§9). What keeps this call answering
    {EMAIL} is that it passes NO ``email_deliverable``: the parameter defaults to True, so
    a caller with no user in hand resolves precisely as it did before the parameter
    existed. The fan-out passes the real answer; that is the difference.
    """
    email_spy = _patch_no_redis(monkeypatch)
    badge_only = _make_user(db_session, company_id=1, role=UserRole.ADMIN, email=f"emp-0777@{SYNTHETIC_EMAIL_DOMAIN}")
    _mute_everything(db_session, badge_only, "account.locked")

    # Preference resolution alone still forces ONLY the catalog's mandatory channel on.
    assert dispatch.resolve_channels(db_session, badge_only, get_entry("account.locked")) == {CHANNEL_EMAIL}

    created = _dispatch_account_locked(db_session, badge_only)
    db_session.flush()

    assert created == 1, "a muted badge-only recipient must still get the in-app row"
    (inbox,) = db_session.query(Notification).filter(Notification.user_id == badge_only.id).all()
    assert inbox.event_key == "account.locked"

    # EMAIL is deliberately NOT removed from the channel set: the undeliverable row is the
    # record that this recipient's ADDRESS is the problem, and dropping it would leave the
    # in-app row as the only trace that anything was wrong.
    (log,) = _logs_for(db_session, badge_only.id)
    assert log.sent is False and log.error
    assert log.notification_id == inbox.id
    assert email_spy.await_count == 0


def test_the_fallback_leaves_a_deliverable_mandatory_email_recipient_alone(db_session: Session, monkeypatch):
    """The other edge: the fallback must not become "everybody gets an in-app row".

    An admin with a real address who muted ``account.locked`` still gets exactly what
    §8.9 promises today -- the mandatory email, and NO inbox row. Widening the fallback to
    every recipient would be invisible in the test above and would quietly override a
    preference the user set.
    """
    email_spy = _patch_no_redis(monkeypatch)
    admin = _make_user(db_session, company_id=1, role=UserRole.ADMIN, email="admin@wercomfg.com")
    _mute_everything(db_session, admin, "account.locked")

    created = _dispatch_account_locked(db_session, admin)
    db_session.flush()

    assert created == 0, "a deliverable recipient's muted in_app preference must be honored"
    assert db_session.query(Notification).filter(Notification.user_id == admin.id).count() == 0
    (log,) = _logs_for(db_session, admin.id)
    assert log.sent is True and log.error is None
    assert log.notification_id is None
    assert email_spy.await_count == 1
    assert email_spy.await_args.kwargs["to"] == "admin@wercomfg.com"


def test_the_in_app_mandatory_entries_behave_identically_deliverable_or_not(db_session: Session, monkeypatch):
    """The entries the fallback must NOT touch.

    Every IN_APP-mandatory entry already lands in the inbox, so the branch is scoped to
    ``mandatory_channel == EMAIL``. The failure being guarded against is a fallback
    written as "undeliverable -> force in_app" with no channel test, which would look
    correct here (these already force in_app) but would also start forcing EMAIL into the
    channel set for a muted badge-only recipient -- inventing an undeliverable log row for
    an event nobody asked to be emailed about.

    Asserted as an EQUALITY between a placeholder-address recipient and a real-address one
    with identical preferences, so the claim is "unaffected", not merely "still works".
    """
    email_spy = _patch_no_redis(monkeypatch)
    in_app_mandatory = sorted(key for key, entry in CATALOG.items() if entry.mandatory_channel == CHANNEL_IN_APP)
    assert in_app_mandatory, "the catalog partition test above should have caught this"

    badge_only = _make_user(db_session, company_id=1, email=f"emp-3131@{SYNTHETIC_EMAIL_DOMAIN}")
    real = _make_user(db_session, company_id=1, email="real.reader@wercomfg.com")
    _mute_everything(db_session, badge_only, *in_app_mandatory)
    _mute_everything(db_session, real, *in_app_mandatory)

    for offset, event_key in enumerate(in_app_mandatory):
        for user in (badge_only, real):
            assert dispatch.resolve_channels(db_session, user, get_entry(event_key)) == {CHANNEL_IN_APP}
            asyncio.run(
                dispatch_direct(
                    db_session,
                    event_key=event_key,
                    company_id=1,
                    recipients=[user],
                    related_type="thing",
                    related_id=7000 + offset,
                    title=f"{event_key} happened",
                    body="b",
                )
            )
    db_session.flush()

    for user in (badge_only, real):
        keys = sorted(
            row.event_key for row in db_session.query(Notification).filter(Notification.user_id == user.id).all()
        )
        assert keys == in_app_mandatory, f"{user.email} did not get one inbox row per entry"
        # No email channel was ever in play, so no log row -- deliverable or not.
        assert _logs_for(db_session, user.id) == []

    assert email_spy.await_count == 0


# ---------------------------------------------------------------------------
# 9. ONE resolver: what the settings screen renders is what the sender does
# ---------------------------------------------------------------------------
#
# ``channels_from_pref`` is shared on purpose -- ``api/endpoints/users.py`` renders the
# self-service settings matrix from it and the dispatcher sends from it -- so a branch
# that lives in only one of the two callers is a screen that lies. The mandatory-EMAIL ->
# IN_APP fallback was such a branch: it sat in the fan-out, so a badge-only recipient who
# had muted ``in_app`` for ``account.locked`` was shown "nothing will be sent" and then
# sent an in-app row. It now lives in the resolver, parameterized by
# ``email_deliverable``.
#
# The parameter is what makes the two agree, and it is also the thing a caller can forget:
# it DEFAULTS to True (today's behavior) so a caller with no user in hand cannot silently
# widen anybody's channel set, which means a caller that HAS the user and does not pass it
# resolves the wrong recipient's answer. §9 asserts the agreement where it is wired, pins
# the default, and marks the one caller where the wiring is still missing.


def _channels_the_dispatcher_acted_on(db: Session, user: User, event_key: str) -> set:
    """The channels a dispatch really used for this user, read back off the rows.

    Deliberately reconstructed from the DATABASE rather than from the return value: the
    claim under test is that the RESOLVER predicts what the SENDER does, so reading the
    resolver's own answer back would make the assertion circular.
    """
    acted = set()
    if db.query(Notification).filter(Notification.user_id == user.id, Notification.event_key == event_key).count():
        acted.add(CHANNEL_IN_APP)
    if db.query(NotificationLog).filter(NotificationLog.user_id == user.id).count():
        acted.add(CHANNEL_EMAIL)
    return acted


def test_the_resolver_predicts_the_delivery_path_for_an_undeliverable_recipient(db_session: Session, monkeypatch):
    """THE shared-resolver claim, for the recipient the fallback exists for.

    A badge-only admin who muted ``account.locked`` entirely. What the settings screen
    renders for them -- ``channels_from_pref`` with this user's real deliverability -- must
    equal what the fan-out actually did, channel for channel. Both are asserted against the
    literal expected set as well, so a build where BOTH went wrong the same way cannot pass
    by agreeing with itself.
    """
    _patch_no_redis(monkeypatch)
    badge_only = _make_user(db_session, company_id=1, role=UserRole.ADMIN, email=f"emp-0808@{SYNTHETIC_EMAIL_DOMAIN}")
    _mute_everything(db_session, badge_only, "account.locked")

    assert dispatch.email_deliverable_for_user(badge_only) is False, "precondition"
    rendered = dispatch.channels_from_pref(
        dispatch.get_preference_row(db_session, badge_only.id),
        get_entry("account.locked"),
        email_deliverable=dispatch.email_deliverable_for_user(badge_only),
    )

    _dispatch_account_locked(db_session, badge_only)
    db_session.flush()
    acted = _channels_the_dispatcher_acted_on(db_session, badge_only, "account.locked")

    assert rendered == acted, f"the settings screen renders {sorted(rendered)}, the sender did {sorted(acted)}"
    assert rendered == {CHANNEL_EMAIL, CHANNEL_IN_APP}


def test_the_resolver_predicts_the_delivery_path_for_a_deliverable_recipient(db_session: Session, monkeypatch):
    """The same claim on the other side of the branch, so "agreement" cannot be reached by
    forcing IN_APP on for everyone.

    An admin with a real address who muted ``account.locked`` gets the mandatory email and
    NO inbox row -- and the screen says exactly that.
    """
    _patch_no_redis(monkeypatch)
    admin = _make_user(db_session, company_id=1, role=UserRole.ADMIN, email="reachable@wercomfg.com")
    _mute_everything(db_session, admin, "account.locked")

    assert dispatch.email_deliverable_for_user(admin) is True, "precondition"
    rendered = dispatch.channels_from_pref(
        dispatch.get_preference_row(db_session, admin.id),
        get_entry("account.locked"),
        email_deliverable=dispatch.email_deliverable_for_user(admin),
    )

    _dispatch_account_locked(db_session, admin)
    db_session.flush()
    acted = _channels_the_dispatcher_acted_on(db_session, admin, "account.locked")

    assert rendered == acted == {CHANNEL_EMAIL}


def _resolution_before_the_parameter_existed(pref, entry) -> set:
    """The pre-change body of ``channels_from_pref``, transcribed.

    Kept as a literal copy rather than imported, because its whole job is to be the thing
    the current implementation is compared AGAINST: sharing code with the subject would
    make the comparison vacuous.
    """
    channels = set(entry.default_channels)
    if pref is not None and isinstance(pref.preferences, dict):
        raw = pref.preferences.get(entry.event_key)
        if isinstance(raw, dict):
            channels = {channel for channel in ALL_CHANNELS if raw.get(channel)}
    if entry.mandatory_channel:
        channels.add(entry.mandatory_channel)
    return channels


def test_the_default_leaves_every_existing_caller_byte_identical(db_session: Session):
    """The whole catalog, both preference shapes, against the pre-change implementation.

    ``email_deliverable`` had to default to True or the parameter would have been a silent
    behavior change for every caller that does not pass it -- including any not yet
    written. Asserted across the entire catalog rather than on one entry, because the new
    branch is gated on ``mandatory_channel == EMAIL`` and a scoping slip would show up on
    the entries nobody thought to check.

    ``resolve_channels`` is checked the same way: it takes the parameter but must NOT
    derive it from the user it already holds. A silent derivation there would make the two
    resolution entry points disagree in the opposite direction -- one deriving, one
    defaulting -- which is the divergence this parameter exists to end.
    """
    badge_only = _make_user(db_session, company_id=1, email=f"emp-0909@{SYNTHETIC_EMAIL_DOMAIN}")
    muted = NotificationPreference(
        user_id=badge_only.id,
        company_id=badge_only.company_id,
        preferences={key: {"in_app": False, "email": False, "sms": False, "digest": False} for key in CATALOG},
    )

    for event_key, entry in CATALOG.items():
        for label, pref in (("no row", None), ("everything muted", muted)):
            expected = _resolution_before_the_parameter_existed(pref, entry)
            assert dispatch.channels_from_pref(pref, entry) == expected, f"{event_key} / {label}"
            assert dispatch.channels_from_pref(pref, entry, email_deliverable=True) == expected

    # The wrapper carries the same default, for a recipient whose address is undeliverable.
    for event_key, entry in CATALOG.items():
        assert dispatch.resolve_channels(db_session, badge_only, entry) == _resolution_before_the_parameter_existed(
            None, entry
        ), f"{event_key}: resolve_channels derived deliverability instead of defaulting"


def test_an_empty_address_writes_an_undeliverable_row_rather_than_nothing(db_session: Session, monkeypatch):
    """The address shape the leg used to drop SILENTLY -- the loudest one there is.

    The email leg was guarded by ``and user.email``, so a literal empty string fell out of
    it entirely: no enqueue (correct) and no ``NotificationLog`` row at all (not correct).
    A missing row is indistinguishable, months later, from a notification that was never
    raised, and it hides the accounts that need a real address -- while every OTHER
    undeliverable shape was being recorded. Emptiness is now judged where the placeholder
    domains are judged, so the leg behaves the way its comment describes.

    The cause is recorded as its own wording, not the placeholder one. ``NotificationLog.
    error`` is not correctable after the fact, and a row claiming a placeholder was minted
    would send an operator looking for a badge-only account that does not exist.
    """
    email_spy = _patch_no_redis(monkeypatch)
    user = _make_user(db_session, company_id=1, email="placeholder@wercomfg.com")
    user.email = ""  # what a row with no address on file actually holds
    db_session.commit()

    _dispatch_ncr(db_session, [user], related_id=903)
    db_session.flush()

    assert email_spy.await_count == 0, "an empty address must never be handed to the mail job"
    (row,) = _logs_for(db_session, user.id)
    assert row.sent is False
    assert row.error == dispatch._MISSING_EMAIL_ERROR
    assert row.error != dispatch._UNDELIVERABLE_EMAIL_ERROR, "the two causes are different problems"
    assert "no email address on file" in row.error
    # The notification really was raised, so the row keeps what this person was not told.
    assert row.subject == "NCR-903 created" and row.body == "A nonconformance was raised."
    assert row.channel == CHANNEL_EMAIL and row.company_id == 1

    # ...and the in-app leg is untouched: no address is not the same as no person.
    assert len(_notifs_for(db_session, user.id)) == 1


def test_the_settings_endpoint_shows_what_the_dispatcher_will_actually_do(client, db_session: Session, monkeypatch):
    """The half of the shared-resolver claim that is still open, pinned end to end.

    Driven through HTTP rather than through ``_effective_preferences`` on purpose. The fix
    will change that function's signature, and a test calling it directly would either
    break on the new signature or -- worse -- keep passing the old default and go on
    testing nothing. The endpoint's contract is stable across either shape, so this test
    starts passing exactly when the user-visible defect is actually fixed.

    The defect is a screen that contradicts the sender for the recipient least able to
    notice: a badge-only admin who muted ``account.locked`` is TOLD nothing will reach them
    and is then sent an in-app row -- which is the one place the "can never be fully muted"
    guarantee lands for them.
    """
    _patch_no_redis(monkeypatch)
    badge_only = _make_user(db_session, company_id=1, role=UserRole.ADMIN, email=f"emp-1010@{SYNTHETIC_EMAIL_DOMAIN}")
    _mute_everything(db_session, badge_only, "account.locked")
    token = create_access_token(subject=badge_only.id, company_id=badge_only.company_id)

    response = client.get(
        "/api/v1/users/me/notification-preferences",
        headers={"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"},
    )
    assert response.status_code == 200, response.text
    rendered = response.json()["preferences"]["account.locked"]

    created = _dispatch_account_locked(db_session, badge_only)
    db_session.flush()
    assert created == 1, "precondition: the dispatcher really does write the in-app row"

    assert rendered["in_app"] is True, "the screen says nothing will be sent; the sender sends an in-app row"


def _rendered_channel_set(client, user: User, event_key: str) -> set:
    """The channels the SETTINGS SCREEN shows as enabled for this user, read over HTTP.

    Driven through the endpoint rather than through ``_effective_preferences`` deliberately:
    that function's SIGNATURE is what the fix changed, so a direct call is the one shape of
    test that can keep passing while testing nothing (it would supply the old default and
    go on rendering a hypothetical recipient). The endpoint's contract is stable across
    either shape, so this reads the same JSON the browser does.
    """
    token = create_access_token(subject=user.id, company_id=user.company_id)
    response = client.get(
        "/api/v1/users/me/notification-preferences",
        headers={"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"},
    )
    assert response.status_code == 200, response.text
    return {channel for channel, enabled in response.json()["preferences"][event_key].items() if enabled}


def test_the_settings_api_and_the_fan_out_agree_for_an_undeliverable_recipient(
    client, db_session: Session, monkeypatch
):
    """ "They can never disagree" asserted as an EQUALITY between the two halves.

    The test above this one pins the user-visible symptom (the screen must not say
    "nothing"); this one pins the claim the shared resolver actually makes -- that what the
    settings API renders IS what the dispatcher does, channel for channel, for the recipient
    the fallback exists to protect. Neither side is read from the resolver: the rendered set
    comes back over HTTP and the acted set is reconstructed from the ROWS the fan-out wrote,
    so the comparison cannot be satisfied by the resolver agreeing with itself.

    Both are also asserted against the literal expected set, because an equality alone would
    be satisfied by a build where BOTH halves were wrong in the same direction -- which is
    precisely the failure mode a shared resolver makes easy.
    """
    _patch_no_redis(monkeypatch)
    badge_only = _make_user(db_session, company_id=1, role=UserRole.ADMIN, email=f"emp-1212@{SYNTHETIC_EMAIL_DOMAIN}")
    _mute_everything(db_session, badge_only, "account.locked")
    assert dispatch.email_deliverable_for_user(badge_only) is False, "precondition"

    rendered = _rendered_channel_set(client, badge_only, "account.locked")

    _dispatch_account_locked(db_session, badge_only)
    db_session.flush()
    acted = _channels_the_dispatcher_acted_on(db_session, badge_only, "account.locked")

    assert rendered == acted, f"the settings API renders {sorted(rendered)}, the sender did {sorted(acted)}"
    assert rendered == {CHANNEL_EMAIL, CHANNEL_IN_APP}


def test_the_settings_api_and_the_fan_out_agree_for_a_deliverable_recipient(client, db_session: Session, monkeypatch):
    """The unchanged half, so "agreement" cannot be reached by forcing IN_APP on for everyone.

    An admin with a real address who muted ``account.locked`` gets the mandatory email and
    NO inbox row -- and the settings API says exactly that. Without this arm, a build that
    rendered and sent ``{email, in_app}`` for EVERY recipient would satisfy the test above
    while quietly overriding a preference this user did set.
    """
    _patch_no_redis(monkeypatch)
    admin = _make_user(db_session, company_id=1, role=UserRole.ADMIN, email="settings.reader@wercomfg.com")
    _mute_everything(db_session, admin, "account.locked")
    assert dispatch.email_deliverable_for_user(admin) is True, "precondition"

    rendered = _rendered_channel_set(client, admin, "account.locked")

    _dispatch_account_locked(db_session, admin)
    db_session.flush()
    acted = _channels_the_dispatcher_acted_on(db_session, admin, "account.locked")

    assert rendered == acted == {CHANNEL_EMAIL}


def test_the_settings_resolver_cannot_default_its_way_back_into_disagreement():
    """The recipient is a REQUIRED parameter, pinned as a signature.

    Deliverability is a property of one specific user, so the caller has to hand the user
    over -- and a defaulted parameter is exactly how the two halves drifted apart the first
    time: the screen resolved a hypothetical recipient with a working mailbox and nobody
    noticed, because the default was the common case. Giving ``user`` a default again would
    reintroduce the divergence silently, with every behavioral test above still green for
    the callers that do pass it.
    """
    parameters = inspect.signature(_effective_preferences).parameters

    assert "user" in parameters, "_effective_preferences no longer takes the recipient at all"
    assert parameters["user"].default is inspect.Parameter.empty, (
        "the recipient must stay REQUIRED: a default here is how the settings screen and "
        "the dispatcher disagreed for badge-only recipients"
    )
