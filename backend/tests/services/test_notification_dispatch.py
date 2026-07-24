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
* a recurring event suppresses a second in-app row while an unread one exists.

Redis is never touched: ``enqueue_job`` (email) and ``_dedup_reserve`` are stubbed.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.orm import Session

import app.services.notification_dispatch as dispatch
from app.models.company import Company
from app.models.notification import DigestQueue, Notification, NotificationLog, NotificationPreference
from app.models.operational_event import OperationalEvent
from app.models.user import User, UserRole
from app.services.notification_catalog import CHANNEL_EMAIL, CHANNEL_IN_APP, get_entry
from app.services.notification_dispatch import dispatch_direct, dispatch_for_event

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
) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"disp-{n}@co{company_id}.test",
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
    channels = dispatch._resolve_channels(db_session, user, get_entry("ncr.created"))
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

    channels = dispatch._resolve_channels(db_session, user, get_entry("ncr.created"))
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

    channels = dispatch._resolve_channels(db_session, user, get_entry("wo.released"))
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
