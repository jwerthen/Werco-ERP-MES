"""Exclusive email audiences: admin controls, defaults, dispatch and queued delivery."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

import app.jobs.email_jobs as jobs
import app.services.notification_dispatch as dispatch
from app.core.security import create_access_token
from app.models.company import Company
from app.models.notification import DigestQueue, Notification, NotificationLog, NotificationPreference
from app.models.operational_event import OperationalEvent
from app.models.quote_config import QuoteSettings, SettingsAuditLog
from app.models.user import User, UserRole
from app.services.notification_email_recipients import email_recipient_ids, setting_key

pytestmark = [pytest.mark.requires_db]
URL = "/api/v1/admin/settings/email-recipients"


def headers(user, company_id=None):
    token = create_access_token(subject=user.id, company_id=company_id or user.company_id)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def make_user(db, email, *, company_id=1, active=True, role=UserRole.OPERATOR, department=None):
    user = User(
        company_id=company_id,
        email=email,
        employee_id=email,
        first_name=email.split("@")[0],
        last_name="Recipient",
        hashed_password="unused",
        role=role,
        department=department,
        is_active=active,
    )
    db.add(user)
    db.commit()
    return user


def set_ids(db, event_key, ids, company_id=1):
    db.add(
        QuoteSettings(
            company_id=company_id,
            setting_key=setting_key(event_key),
            setting_value=json.dumps(ids),
            setting_type="json",
        )
    )
    db.commit()


def events(response):
    assert response.status_code == 200, response.text
    return {event["event_key"]: event for event in response.json()["events"]}


def test_default_recipients_are_exact_addresses_and_read_does_not_persist(client, db_session, admin_user):
    ashley = make_user(db_session, "AWERTHEN@wercomfg.com")
    jon = make_user(db_session, "jwerthen@wercomfg.com")
    junior = make_user(db_session, "jmw@wercomfg.com")
    make_user(db_session, "other@wercomfg.com", role=UserRole.MANAGER)
    result = events(client.get(URL, headers=headers(admin_user)))
    for key in ("wo.completed", "receipt.created"):
        assert result[key]["user_ids"] == [ashley.id, jon.id, junior.id]
        assert result[key]["missing_default_emails"] == []
        assert not result[key]["is_custom"]
    assert result["ncr.created"]["user_ids"] is None
    assert "account.locked" not in result
    assert db_session.query(QuoteSettings).count() == 0


def test_missing_defaults_fail_closed_and_are_visible(client, db_session, admin_user):
    result = events(client.get(URL, headers=headers(admin_user)))["wo.completed"]
    assert result["user_ids"] == []
    assert set(result["missing_default_emails"]) == {
        "awerthen@wercomfg.com",
        "jwerthen@wercomfg.com",
        "jmw@wercomfg.com",
    }
    assert email_recipient_ids(db_session, 1, "wo.completed") == set()


def test_save_is_per_event_audited_and_empty_stays_disabled_after_reload(client, db_session, admin_user, test_user):
    result = events(
        client.put(f"{URL}/wo.completed", json={"user_ids": [test_user.id, test_user.id]}, headers=headers(admin_user))
    )
    assert result["wo.completed"]["user_ids"] == [test_user.id]
    assert result["wo.completed"]["is_custom"]
    assert not result["receipt.created"]["is_custom"]
    result = events(client.put(f"{URL}/wo.completed", json={"user_ids": []}, headers=headers(admin_user)))
    assert result["wo.completed"]["user_ids"] == []
    assert events(client.get(URL, headers=headers(admin_user)))["wo.completed"]["user_ids"] == []
    audits = db_session.query(SettingsAuditLog).filter(SettingsAuditLog.entity_type == "email_recipients").all()
    assert len(audits) == 2
    assert all(row.company_id == 1 and row.changed_by == admin_user.id for row in audits)


def test_restore_returns_to_three_address_default(client, db_session, admin_user, test_user):
    ashley = make_user(db_session, "awerthen@wercomfg.com")
    set_ids(db_session, "wo.completed", [test_user.id])
    result = events(client.put(f"{URL}/wo.completed", json={"user_ids": None}, headers=headers(admin_user)))
    assert result["wo.completed"]["user_ids"] == [ashley.id]
    assert not result["wo.completed"]["is_custom"]


def test_personal_channel_matrix_reflects_admin_email_selection(client, db_session, admin_user, test_user):
    set_ids(db_session, "wo.completed", [test_user.id])
    own = client.get("/api/v1/users/me/notification-preferences", headers=headers(test_user))
    assert own.status_code == 200, own.text
    assert own.json()["preferences"]["wo.completed"]["email"] is True
    assert own.json()["preferences"]["receipt.created"]["email"] is False
    other = client.get("/api/v1/users/me/notification-preferences", headers=headers(admin_user))
    assert other.json()["preferences"]["wo.completed"]["email"] is False


@pytest.mark.parametrize("field", ["inactive", "synthetic", "foreign", "nonexistent"])
def test_cannot_select_invalid_recipients(client, db_session, admin_user, field):
    db_session.add(Company(id=2, name="Other", slug="other"))
    db_session.commit()
    user = make_user(
        db_session,
        "employee-1234@users.werco.com" if field == "synthetic" else "invalid@example.test",
        company_id=2 if field == "foreign" else 1,
        active=field != "inactive",
    )
    response = client.put(
        f"{URL}/wo.completed",
        json={"user_ids": [999999 if field == "nonexistent" else user.id]},
        headers=headers(admin_user),
    )
    assert response.status_code == 400
    assert db_session.query(QuoteSettings).count() == 0


@pytest.mark.parametrize("user_ids", [[True], ["1"], [1.5], "everyone"])
def test_ids_require_integers(client, admin_user, user_ids):
    assert (
        client.put(f"{URL}/wo.completed", json={"user_ids": user_ids}, headers=headers(admin_user)).status_code == 422
    )


def test_only_admins_can_read_or_change_audiences(client, test_user):
    assert client.get(URL, headers=headers(test_user)).status_code == 403
    assert client.put(f"{URL}/wo.completed", json={"user_ids": []}, headers=headers(test_user)).status_code == 403


def test_no_security_email_or_generic_setting_bypass(client, admin_user):
    for key in ("account.locked", "does.not.exist"):
        assert client.put(f"{URL}/{key}", json={"user_ids": []}, headers=headers(admin_user)).status_code == 400
    assert (
        client.put(
            f'/api/v1/admin/settings/overhead/{setting_key("wo.completed")}',
            json={"value": "[1]"},
            headers=headers(admin_user),
        ).status_code
        == 404
    )


def test_company_settings_and_user_directory_are_isolated(client, db_session, admin_user, test_user):
    db_session.add(Company(id=2, name="Other", slug="other"))
    db_session.commit()
    other_admin = make_user(db_session, "other-admin@example.test", company_id=2, role=UserRole.ADMIN)
    foreign = client.get(URL, headers=headers(other_admin))
    assert events(foreign)["wo.completed"]["user_ids"] is None
    assert [user["id"] for user in foreign.json()["users"]] == [other_admin.id]
    events(client.put(f"{URL}/wo.completed", json={"user_ids": [test_user.id]}, headers=headers(admin_user)))
    assert events(client.get(URL, headers=headers(other_admin)))["wo.completed"]["user_ids"] is None
    # A switched platform admin uses the active tenant for settings and audits.
    admin_user.role = UserRole.PLATFORM_ADMIN
    db_session.commit()
    events(client.put(f"{URL}/wo.completed", json={"user_ids": [other_admin.id]}, headers=headers(admin_user, 2)))
    audit = db_session.query(SettingsAuditLog).order_by(SettingsAuditLog.id.desc()).first()
    assert audit.company_id == 2
    assert events(client.get(URL, headers=headers(admin_user)))["wo.completed"]["user_ids"] == [test_user.id]


@pytest.mark.parametrize(
    ("event_type", "event_key"),
    [("work_order_completed", "wo.completed"), ("purchase_order_received", "receipt.created")],
)
def test_default_fanout_only_emails_three_users_and_preserves_in_app(db_session, monkeypatch, event_type, event_key):
    selected = [
        make_user(db_session, email) for email in ("awerthen@wercomfg.com", "jwerthen@wercomfg.com", "jmw@wercomfg.com")
    ]
    automatic = make_user(db_session, "automatic@example.test", role=UserRole.MANAGER, department="Purchasing")
    email = AsyncMock()
    monkeypatch.setattr(dispatch, "_enqueue_email", email)
    monkeypatch.setattr(dispatch, "_dedup_reserve", AsyncMock(return_value=True))
    event = OperationalEvent(
        company_id=1,
        event_type=event_type,
        entity_type="work_order",
        entity_id=42,
        source_module="test",
        event_payload={},
        user_id=selected[0].id,
    )
    asyncio.run(dispatch.dispatch_for_event(db_session, event))
    db_session.flush()
    assert {call.kwargs["user"].id for call in email.call_args_list} == {user.id for user in selected}
    assert {row.user_id for row in db_session.query(Notification).all()} == {automatic.id}
    assert {
        row.user_id for row in db_session.query(NotificationLog).filter(NotificationLog.event_type == event_key).all()
    } == {user.id for user in selected}


def test_direct_dispatch_override_ignores_old_email_prefs_and_excludes_foreign_inactive(db_session, monkeypatch):
    chosen = make_user(db_session, "chosen@example.test")
    automatic = make_user(db_session, "automatic@example.test", role=UserRole.MANAGER)
    inactive = make_user(db_session, "inactive@example.test", active=False)
    db_session.add(Company(id=2, name="Other", slug="other"))
    db_session.commit()
    foreign = make_user(db_session, "foreign@example.test", company_id=2)
    # Even malformed/manual data cannot bypass tenant/active filtering at dispatch.
    set_ids(db_session, "ncr.created", [chosen.id, inactive.id, foreign.id])
    db_session.add(
        NotificationPreference(company_id=1, user_id=chosen.id, preferences={"ncr.created": {"email": False}})
    )
    db_session.add(
        NotificationPreference(
            company_id=1, user_id=automatic.id, preferences={"ncr.created": {"email": True, "digest": True}}
        )
    )
    db_session.commit()
    email = AsyncMock()
    monkeypatch.setattr(dispatch, "_enqueue_email", email)
    monkeypatch.setattr(dispatch, "_dedup_reserve", AsyncMock(return_value=True))
    asyncio.run(
        dispatch.dispatch_direct(
            db_session,
            event_key="ncr.created",
            company_id=1,
            recipients=[automatic, foreign, inactive],
            actor_user_id=chosen.id,
            title="NCR",
        )
    )
    assert [call.kwargs["user"].id for call in email.call_args_list] == [chosen.id]
    assert db_session.query(DigestQueue).count() == 0
    assert {row.user_id for row in db_session.query(Notification).all()} == {automatic.id}


@pytest.mark.parametrize("stored", ["[]", '{"unexpected":true}', "broken"])
def test_disabled_or_invalid_saved_list_never_falls_back_to_roles(db_session, monkeypatch, test_user, stored):
    db_session.add(QuoteSettings(company_id=1, setting_key=setting_key("wo.completed"), setting_value=stored))
    db_session.commit()
    email = AsyncMock()
    monkeypatch.setattr(dispatch, "_enqueue_email", email)
    monkeypatch.setattr(dispatch, "_dedup_reserve", AsyncMock(return_value=True))
    asyncio.run(
        dispatch.dispatch_direct(
            db_session, event_key="wo.completed", company_id=1, recipients=[test_user], title="Completed"
        )
    )
    email.assert_not_called()
    assert db_session.query(Notification).count() == 1


@pytest.mark.parametrize("event_key", ["wo.completed", "receipt.created"])
def test_queued_email_is_suppressed_after_recipient_change(db_session, monkeypatch, admin_user, event_key):
    log = NotificationLog(
        company_id=1, user_id=admin_user.id, event_type=event_key, channel="email", provider_status="queued", sent=False
    )
    db_session.add(log)
    db_session.commit()
    log_id, user_id = log.id, admin_user.id
    transport = AsyncMock(return_value=True)
    monkeypatch.setattr(jobs, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(jobs.email_service, "send_email", transport)
    result = asyncio.run(
        jobs.send_email_task(
            to=admin_user.email, subject="Old queued message", notification_log_id=log_id, company_id=1, user_id=user_id
        )
    )
    assert result == {"sent": False, "status": "suppressed"}
    transport.assert_not_called()
    assert db_session.get(NotificationLog, log_id).provider_status == "suppressed"


@pytest.mark.parametrize("keep_other", [False, True])
def test_queued_digest_filters_restricted_events(db_session, monkeypatch, admin_user, keep_other):
    log = NotificationLog(
        company_id=1,
        user_id=admin_user.id,
        event_type="email.daily_digest",
        channel="email",
        provider_status="queued",
        sent=False,
    )
    db_session.add(log)
    db_session.commit()
    transport = AsyncMock(return_value=True)
    monkeypatch.setattr(jobs, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(jobs.email_service, "send_email", transport)
    context = {"events": {"wo.completed": [{"title": "Restricted"}]}}
    if keep_other:
        context["events"]["ncr.created"] = [{"title": "Allowed"}]
    result = asyncio.run(
        jobs.send_email_task(
            to=admin_user.email,
            subject="Digest",
            context=context,
            notification_log_id=log.id,
            company_id=1,
            user_id=admin_user.id,
        )
    )
    if keep_other:
        assert result["sent"]
        assert transport.call_args.kwargs["context"]["events"] == {"ncr.created": [{"title": "Allowed"}]}
    else:
        assert result["status"] == "suppressed"
        transport.assert_not_called()
