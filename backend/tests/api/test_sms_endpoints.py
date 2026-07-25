"""API coverage for the SMS surface (NOTIFICATIONS_PLAN.md §3.4 / §6 / §8):

1. ``PUT /companies/me/sms-egress`` — the ADMIN-only, double-audited CUI kill switch.
2. ``PUT /users/me/phone`` — self-service E.164 phone, audited, self-scoped.
3. ``GET/PUT /users/me/notification-preferences`` — the minimal SMS opt-in slice; the
   ONLY place a ``NotificationPreference`` row is created (and it stamps ``company_id``).
4. ``POST /users/me/test-sms`` — respects the kill switch and cannot target another
   number.
5. PII field minimization — ``phone`` must not appear in broad user serializations.
"""

from unittest.mock import MagicMock

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import app.services.sms_service as sms
from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.notification import NotificationLog, NotificationPreference
from app.models.user import User, UserRole

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

SMS_EGRESS_URL = "/api/v1/companies/me/sms-egress"
PREFS_URL = "/api/v1/users/me/notification-preferences"
PHONE_URL = "/api/v1/users/me/phone"
TEST_SMS_URL = "/api/v1/users/me/test-sms"
COMPANY_A = 1
COMPANY_B = 2

_seq = {"n": 0}


def _ensure_company(db: Session, company_id: int) -> Company:
    company = db.query(Company).filter(Company.id == company_id).first()
    if company is None:
        company = Company(id=company_id, name=f"Co {company_id}", slug=f"sms-api-co-{company_id}", is_active=True)
        db.add(company)
        db.commit()
    return company


def _make_user(
    db: Session,
    *,
    role: UserRole = UserRole.ADMIN,
    phone: str = None,
    company_id: int = COMPANY_A,
) -> User:
    _ensure_company(db, company_id)
    _seq["n"] += 1
    n = _seq["n"]
    user = User(
        email=f"sms-api-{n}@werco.test",
        employee_id=f"SMSAPI-{n:05d}",
        first_name="Sms",
        last_name="Api",
        hashed_password="$2b$12$abcdefghijklmnopqrstuv",  # tokens are minted directly
        role=role,
        phone=phone,
        is_active=True,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _headers_for(user: User) -> dict:
    token = create_access_token(subject=user.id, company_id=user.company_id)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def _set_egress(db: Session, enabled: bool, company_id: int = COMPANY_A) -> None:
    company = _ensure_company(db, company_id)
    company.allow_sms_egress = enabled
    db.commit()


def _egress_of(db: Session, company_id: int) -> bool:
    db.expire_all()
    return db.query(Company).filter(Company.id == company_id).first().allow_sms_egress


# ===========================================================================
# 1. PUT /companies/me/sms-egress
# ===========================================================================
class TestSMSEgressToggle:
    def test_admin_can_enable_and_it_persists(self, client: TestClient, db_session: Session):
        admin = _make_user(db_session, role=UserRole.ADMIN)
        resp = client.put(SMS_EGRESS_URL, headers=_headers_for(admin), json={"allow_sms_egress": True})
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert resp.json()["allow_sms_egress"] is True

        db_session.expire_all()
        assert db_session.query(Company).filter(Company.id == COMPANY_A).first().allow_sms_egress is True

    @pytest.mark.parametrize("role", [UserRole.MANAGER, UserRole.QUALITY, UserRole.OPERATOR])
    def test_non_admin_is_forbidden(self, client: TestClient, db_session: Session, role):
        user = _make_user(db_session, role=role)
        resp = client.put(SMS_EGRESS_URL, headers=_headers_for(user), json={"allow_sms_egress": True})
        assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text

        db_session.expire_all()
        assert db_session.query(Company).filter(Company.id == COMPANY_A).first().allow_sms_egress is False

    def test_flip_is_double_audited(self, client: TestClient, db_session: Session):
        admin = _make_user(db_session, role=UserRole.ADMIN)
        resp = client.put(SMS_EGRESS_URL, headers=_headers_for(admin), json={"allow_sms_egress": True})
        assert resp.status_code == status.HTTP_200_OK, resp.text

        rows = (
            db_session.query(AuditLog)
            .filter(AuditLog.resource_type == "company", AuditLog.resource_id == COMPANY_A)
            .all()
        )
        assert "UPDATE" in [r.action for r in rows]
        status_row = next(r for r in rows if r.action == "STATUS_CHANGE")
        assert status_row.old_values == {"status": "sms_egress_disabled"}
        assert status_row.new_values == {"status": "sms_egress_enabled"}
        assert status_row.company_id == COMPANY_A

    def test_no_op_writes_no_status_change(self, client: TestClient, db_session: Session):
        admin = _make_user(db_session, role=UserRole.ADMIN)
        resp = client.put(SMS_EGRESS_URL, headers=_headers_for(admin), json={"allow_sms_egress": False})
        assert resp.status_code == status.HTTP_200_OK, resp.text

        rows = (
            db_session.query(AuditLog)
            .filter(AuditLog.resource_type == "company", AuditLog.resource_id == COMPANY_A)
            .all()
        )
        assert "STATUS_CHANGE" not in [r.action for r in rows]


# ===========================================================================
# 2. PUT /users/me/phone
# ===========================================================================
class TestSelfPhone:
    def test_phone_is_normalized_to_e164_and_audited(self, client: TestClient, db_session: Session):
        user = _make_user(db_session, role=UserRole.OPERATOR)
        resp = client.put(PHONE_URL, headers=_headers_for(user), json={"phone": "(512) 555-0134"})
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert resp.json()["phone"] == "+15125550134"

        db_session.expire_all()
        assert db_session.query(User).filter(User.id == user.id).first().phone == "+15125550134"

        audited = (
            db_session.query(AuditLog)
            .filter(AuditLog.resource_type == "user", AuditLog.resource_id == user.id, AuditLog.action == "UPDATE")
            .all()
        )
        assert audited, "phone change must land on the tamper-evident trail"

    def test_invalid_phone_is_rejected(self, client: TestClient, db_session: Session):
        user = _make_user(db_session, role=UserRole.OPERATOR)
        resp = client.put(PHONE_URL, headers=_headers_for(user), json={"phone": "12345"})
        assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text

        db_session.expire_all()
        assert db_session.query(User).filter(User.id == user.id).first().phone is None

    def test_phone_can_be_cleared(self, client: TestClient, db_session: Session):
        user = _make_user(db_session, role=UserRole.OPERATOR, phone="+15125550134")
        resp = client.put(PHONE_URL, headers=_headers_for(user), json={"phone": None})
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert resp.json()["phone"] is None


# ===========================================================================
# 3. GET/PUT /users/me/notification-preferences
# ===========================================================================
class TestNotificationPreferences:
    def test_get_returns_defaults_without_creating_a_row(self, client: TestClient, db_session: Session):
        user = _make_user(db_session, role=UserRole.QUALITY)
        resp = client.get(PREFS_URL, headers=_headers_for(user))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        payload = resp.json()

        assert payload["has_saved_preferences"] is False
        # No catalog event ships SMS on by default -- SMS is opt-in everywhere.
        assert all(not channels["sms"] for channels in payload["preferences"].values())
        # ncr.created defaults: in-app + email, and its mandatory in-app channel is forced.
        assert payload["preferences"]["ncr.created"]["in_app"] is True
        assert payload["preferences"]["ncr.created"]["email"] is True
        # §9.8: reading preferences must NEVER auto-create a row.
        assert db_session.query(NotificationPreference).count() == 0

    def test_put_creates_the_row_with_company_id_and_full_shape(self, client: TestClient, db_session: Session):
        user = _make_user(db_session, role=UserRole.QUALITY)
        resp = client.put(PREFS_URL, headers=_headers_for(user), json={"preferences": {"ncr.created": {"sms": True}}})
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert resp.json()["preferences"]["ncr.created"]["sms"] is True
        assert resp.json()["has_saved_preferences"] is True

        row = db_session.query(NotificationPreference).filter(NotificationPreference.user_id == user.id).one()
        # TenantMixin column stamped from the active company (the §9.8 defect).
        assert row.company_id == COMPANY_A
        # Persisted in the full 4-key shape so PR 3 extends it without a migration.
        assert set(row.preferences["ncr.created"].keys()) == {"in_app", "email", "sms", "digest"}
        assert row.preferences["ncr.created"]["sms"] is True
        assert row.preferences["ncr.created"]["in_app"] is True  # catalog default preserved

    def test_put_is_audited(self, client: TestClient, db_session: Session):
        user = _make_user(db_session, role=UserRole.QUALITY)
        client.put(PREFS_URL, headers=_headers_for(user), json={"preferences": {"ncr.created": {"sms": True}}})
        rows = db_session.query(AuditLog).filter(AuditLog.resource_type == "notification_preference").all()
        assert rows and rows[0].company_id == COMPANY_A

    def test_sms_on_a_non_eligible_event_is_rejected(self, client: TestClient, db_session: Session):
        user = _make_user(db_session, role=UserRole.QUALITY)
        resp = client.put(PREFS_URL, headers=_headers_for(user), json={"preferences": {"wo.released": {"sms": True}}})
        assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
        assert db_session.query(NotificationPreference).count() == 0

    def test_unknown_event_key_is_rejected(self, client: TestClient, db_session: Session):
        user = _make_user(db_session, role=UserRole.QUALITY)
        resp = client.put(PREFS_URL, headers=_headers_for(user), json={"preferences": {"not.an.event": {"sms": True}}})
        assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text

    def test_pr3_shaped_payload_fails_loudly_rather_than_silently_dropping(
        self, client: TestClient, db_session: Session
    ):
        """Extra channel keys are forbidden in PR 4 so a full-matrix save can't
        silently no-op the channels this endpoint does not yet own."""
        user = _make_user(db_session, role=UserRole.QUALITY)
        resp = client.put(
            PREFS_URL,
            headers=_headers_for(user),
            json={"preferences": {"ncr.created": {"sms": True, "email": False}}},
        )
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, resp.text

    def test_preferences_are_self_scoped(self, client: TestClient, db_session: Session):
        """Two users saving prefs never touch each other's row."""
        a = _make_user(db_session, role=UserRole.QUALITY)
        b = _make_user(db_session, role=UserRole.QUALITY)
        client.put(PREFS_URL, headers=_headers_for(a), json={"preferences": {"ncr.created": {"sms": True}}})

        resp_b = client.get(PREFS_URL, headers=_headers_for(b))
        assert resp_b.json()["preferences"]["ncr.created"]["sms"] is False
        assert db_session.query(NotificationPreference).count() == 1


# ===========================================================================
# 4. POST /users/me/test-sms
# ===========================================================================
class TestTestSMS:
    def test_requires_a_phone_on_file(self, client: TestClient, db_session: Session):
        user = _make_user(db_session, role=UserRole.QUALITY, phone=None)
        _set_egress(db_session, True)
        resp = client.post(TEST_SMS_URL, headers=_headers_for(user))
        assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text

    def test_kill_switch_blocks_the_send(self, client: TestClient, db_session: Session, monkeypatch):
        user = _make_user(db_session, role=UserRole.QUALITY, phone="+15125550134")
        _set_egress(db_session, False)
        provider = MagicMock()
        monkeypatch.setattr(sms, "_send_via_twilio", provider)
        monkeypatch.setattr(sms, "sms_configured", lambda: True)

        resp = client.post(TEST_SMS_URL, headers=_headers_for(user))
        assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
        provider.assert_not_called()

        log = db_session.query(NotificationLog).filter(NotificationLog.event_type == "sms.test").one()
        assert log.sent is False
        assert log.company_id == COMPANY_A
        assert "egress is disabled" in (log.error or "")

    def test_successful_send_records_sid_and_status(self, client: TestClient, db_session: Session, monkeypatch):
        user = _make_user(db_session, role=UserRole.QUALITY, phone="+15125550134")
        _set_egress(db_session, True)
        monkeypatch.setattr(sms, "sms_configured", lambda: True)
        monkeypatch.setattr(
            sms, "_send_via_twilio", MagicMock(return_value=MagicMock(sid="SM" + "0" * 32, status="queued"))
        )

        resp = client.post(TEST_SMS_URL, headers=_headers_for(user))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert resp.json()["status"] == "sent"
        assert resp.json()["sid"] == "SM" + "0" * 32

        log = db_session.query(NotificationLog).filter(NotificationLog.event_type == "sms.test").one()
        assert log.sent is True
        assert log.provider_message_id == "SM" + "0" * 32
        assert log.provider_status == "queued"

    def test_unconfigured_server_reports_a_skip_not_an_error(
        self, client: TestClient, db_session: Session, monkeypatch
    ):
        user = _make_user(db_session, role=UserRole.QUALITY, phone="+15125550134")
        _set_egress(db_session, True)
        monkeypatch.setattr(sms, "sms_configured", lambda: False)

        resp = client.post(TEST_SMS_URL, headers=_headers_for(user))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert resp.json()["status"] == "skipped"


# ===========================================================================
# 5. PII field minimization (§8.12)
# ===========================================================================
class TestPhoneFieldMinimization:
    def test_phone_is_absent_from_the_auth_user_payload(self, client: TestClient, db_session: Session):
        """``/auth/me``-style serialization (app.schemas.user.UserResponse) has no phone."""
        from app.schemas.user import UserResponse

        assert "phone" not in UserResponse.model_fields

    def test_self_profile_exposes_own_phone(self, client: TestClient, db_session: Session):
        user = _make_user(db_session, role=UserRole.OPERATOR, phone="+15125550134")
        resp = client.get("/api/v1/users/me", headers=_headers_for(user))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert resp.json()["phone"] == "+15125550134"

    def test_user_list_is_admin_manager_only(self, client: TestClient, db_session: Session):
        """The only list carrying phone is the ADMIN/MANAGER user-management list."""
        operator = _make_user(db_session, role=UserRole.OPERATOR, phone="+15125550134")
        resp = client.get("/api/v1/users/", headers=_headers_for(operator))
        assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text


# ===========================================================================
# 6. Cross-tenant scoping (invariant #1)
#
# Every route in this PR derives its tenant from ``get_current_company_id`` and its
# subject from ``current_user`` -- none of them accepts a company id or a user id. The
# tests above all run inside COMPANY_A, so they cannot observe a scoping defect; these
# add a second tenant and pin that the derivation is real.
# ===========================================================================
class TestCrossTenantScoping:
    def test_egress_toggle_only_flips_the_callers_own_company(self, client: TestClient, db_session: Session):
        """The CUI kill switch is per-tenant: company B's admin cannot open company A."""
        _set_egress(db_session, False, COMPANY_A)
        admin_b = _make_user(db_session, role=UserRole.ADMIN, company_id=COMPANY_B)

        resp = client.put(SMS_EGRESS_URL, headers=_headers_for(admin_b), json={"allow_sms_egress": True})
        assert resp.status_code == status.HTTP_200_OK, resp.text

        assert _egress_of(db_session, COMPANY_B) is True
        assert _egress_of(db_session, COMPANY_A) is False, "one tenant enabled SMS egress for another"

        # The audit trail records the flip against company B only.
        rows = db_session.query(AuditLog).filter(AuditLog.resource_type == "company").all()
        assert rows, "the kill-switch flip must be audited"
        assert {r.company_id for r in rows} == {COMPANY_B}
        assert {r.resource_id for r in rows} == {COMPANY_B}

    def test_preferences_report_the_callers_own_kill_switch_and_stamp_their_tenant(
        self, client: TestClient, db_session: Session
    ):
        """``sms_egress_enabled`` comes from the CALLER's company, and the row a save
        creates carries that same ``company_id`` (the non-null TenantMixin column)."""
        _set_egress(db_session, True, COMPANY_A)
        _set_egress(db_session, False, COMPANY_B)
        user_b = _make_user(db_session, role=UserRole.QUALITY, company_id=COMPANY_B)

        resp = client.get(PREFS_URL, headers=_headers_for(user_b))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert resp.json()["sms_egress_enabled"] is False, "company A's kill switch leaked into company B"

        saved = client.put(
            PREFS_URL, headers=_headers_for(user_b), json={"preferences": {"ncr.created": {"sms": True}}}
        )
        assert saved.status_code == status.HTTP_200_OK, saved.text
        row = db_session.query(NotificationPreference).filter(NotificationPreference.user_id == user_b.id).one()
        assert row.company_id == COMPANY_B

    def test_phone_update_touches_only_the_caller(self, client: TestClient, db_session: Session):
        """No user id is accepted, so a save can only ever move the caller's own number."""
        mine = _make_user(db_session, role=UserRole.OPERATOR)
        other_tenant = _make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_B, phone="+15125550199")

        resp = client.put(PHONE_URL, headers=_headers_for(mine), json={"phone": "512-555-0134"})
        assert resp.status_code == status.HTTP_200_OK, resp.text

        db_session.expire_all()
        assert db_session.query(User).filter(User.id == mine.id).one().phone == "+15125550134"
        assert db_session.query(User).filter(User.id == other_tenant.id).one().phone == "+15125550199"

    def test_test_sms_cannot_be_pointed_at_another_number(self, client: TestClient, db_session: Session, monkeypatch):
        """The destination is ``current_user.phone`` and is never taken from the request.

        A caller-supplied number in the body must be ignored outright -- otherwise this
        endpoint would be an authenticated way to text arbitrary phones from the
        company's Twilio account.
        """
        user = _make_user(db_session, role=UserRole.QUALITY, phone="+15125550134")
        _set_egress(db_session, True)
        monkeypatch.setattr(sms, "sms_configured", lambda: True)
        provider = MagicMock(return_value=MagicMock(sid="SM" + "0" * 32, status="queued"))
        monkeypatch.setattr(sms, "_send_via_twilio", provider)

        resp = client.post(
            TEST_SMS_URL,
            headers=_headers_for(user),
            json={"phone": "+15125559999", "to": "+15125559999", "body": "exfiltrate me"},
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text

        assert provider.call_count == 1
        assert provider.call_args.kwargs["to"] == "+15125550134"
        assert "exfiltrate" not in provider.call_args.kwargs["body"]

        log = db_session.query(NotificationLog).filter(NotificationLog.event_type == "sms.test").one()
        assert log.company_id == COMPANY_A and log.user_id == user.id

    def test_test_sms_log_row_is_stamped_with_the_callers_tenant(
        self, client: TestClient, db_session: Session, monkeypatch
    ):
        user_b = _make_user(db_session, role=UserRole.QUALITY, phone="+15125550199", company_id=COMPANY_B)
        _set_egress(db_session, True, COMPANY_B)
        monkeypatch.setattr(sms, "sms_configured", lambda: True)
        monkeypatch.setattr(
            sms, "_send_via_twilio", MagicMock(return_value=MagicMock(sid="SM" + "1" * 32, status="queued"))
        )

        resp = client.post(TEST_SMS_URL, headers=_headers_for(user_b))
        assert resp.status_code == status.HTTP_200_OK, resp.text

        log = db_session.query(NotificationLog).filter(NotificationLog.event_type == "sms.test").one()
        assert log.company_id == COMPANY_B
