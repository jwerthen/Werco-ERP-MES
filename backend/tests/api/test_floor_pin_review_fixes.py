"""Regressions for the PR #369 review findings on the floor PIN (badge sign-on, AS9100D 8.5.2).

1/2. **A badge+PIN session ends when its PIN does.** The tokens carry ``pst`` -- the verified
     PIN's ``floor_pin_set_at`` -- and every request, refresh and company switch refuses the
     session 401 ``floor_pin_reauth_required`` once that PIN is reset, cleared, or cleared by a
     promotion. The scenario the review traced: a Manager sets an Operator's PIN, signs in as
     the Operator, the Operator is promoted to Quality -- the Manager's session must die at its
     next request, not live on as a Quality identity. A lock does NOT end a session.
3/4. **PIN guessing is capped.** Wrong PINs are cumulative until a correct PIN or an admin
     unlock / set / clear: 5 -> 15 minutes, 10 -> 2 hours, 15 -> admin-locked (only an
     Admin / Manager unlock, set or clear lifts it). On the PUBLIC employee-login route, wrong
     PINs charge their own per-IP budget, which blocks only further PIN attempts (429).
5.   **A promotion requires a new PIN** rather than downgrading the badge to badge-only:
     ``floor_pin_reset_required`` refuses badge sign-on 403 whatever the policy, until an Admin
     sets a new PIN or clears it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.endpoints import auth as auth_endpoints
from app.core.config import settings
from app.core.floor_pin import (
    FLOOR_PIN_ADMIN_LOCK_FAILURES,
    FLOOR_PIN_LOCKOUT_MINUTES,
    FLOOR_PIN_LONG_LOCK_FAILURES,
    FLOOR_PIN_LONG_LOCKOUT_MINUTES,
    FLOOR_PIN_MAX_FAILURES,
    floor_pin_stamp,
    hash_floor_pin,
)
from app.core.login_throttle import FLOOR_PIN_IP_MAX_WRONG_PINS, employee_login_throttle, floor_pin_ip_throttle
from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.user import User, UserRole
from tests.api.kiosk_test_helpers import BADGE_TOKEN_URL, bearer, ensure_company, kiosk_token_for, make_kiosk_station
from tests.conftest import TEST_PASSWORD_HASH

pytestmark = pytest.mark.api

API = settings.API_V1_PREFIX.rstrip("/")
EMPLOYEE_LOGIN = f"{API}/auth/employee-login"
REFRESH = f"{API}/auth/refresh"
ME = f"{API}/users/me"
XHR = {"X-Requested-With": "XMLHttpRequest"}
TESTCLIENT_IP = "testclient"
PIN = "830416"
NEW_PIN = "529038"
WRONG_PIN = "174926"
INVALID = "Invalid badge or PIN"
REAUTH = {"code": "floor_pin_reauth_required", "message": "Sign in again with your badge and PIN."}
RESET_REQUIRED = {
    "code": "floor_pin_reset_required",
    "message": "Your role changed. Ask an Admin to set a new floor PIN.",
}

_seq = {"n": 0}


@pytest.fixture(autouse=True)
def _memory_mode_throttles(monkeypatch):
    monkeypatch.setattr(settings, "REDIS_URL", None)
    employee_login_throttle.reset()
    floor_pin_ip_throttle.reset()
    yield
    employee_login_throttle.reset()
    floor_pin_ip_throttle.reset()


def make_user(db: Session, *, role: UserRole = UserRole.OPERATOR, pin: Optional[str] = None) -> User:
    ensure_company(db, 1)
    _seq["n"] += 1
    badge = f"RV-{_seq['n']:04d}"
    user = User(
        email=f"{badge.lower()}@werco.com",
        employee_id=badge,
        first_name="Review",
        last_name="Fixes",
        hashed_password=TEST_PASSWORD_HASH,
        role=role,
        is_active=True,
        company_id=1,
    )
    if pin:
        user.floor_pin_hash = hash_floor_pin(pin)
        user.floor_pin_set_at = datetime.utcnow()
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def fresh(db: Session, user: User) -> User:
    db.expire_all()
    return db.get(User, user.id)


def claims(token: str, refresh: bool = False) -> Dict[str, Any]:
    key = settings.REFRESH_TOKEN_SECRET_KEY if refresh else settings.SECRET_KEY
    return jwt.decode(token, key, algorithms=[settings.ALGORITHM])


def rows(db: Session, action: Optional[str] = None) -> List[AuditLog]:
    db.expire_all()
    query = db.query(AuditLog)
    if action:
        query = query.filter(AuditLog.action == action)
    return query.order_by(AuditLog.id).all()


def badge_login(client: TestClient, user: User, pin: Optional[str] = None):
    body: Dict[str, Any] = {"employee_id": user.employee_id}
    if pin is not None:
        body["pin"] = pin
    return client.post(EMPLOYEE_LOGIN, json=body, headers=XHR)


def auth(token: str) -> Dict[str, str]:
    return {**XHR, "Authorization": f"Bearer {token}"}


def pin_url(user: User, suffix: str = "") -> str:
    return f"{API}/users/{user.id}/floor-pin{suffix}"


def ip_pin_failures() -> int:
    entry = floor_pin_ip_throttle._memory.get(TESTCLIENT_IP)
    return 0 if entry is None else entry[0]


def badge_pin_session(client: TestClient, user: User, pin: str = PIN) -> Dict[str, str]:
    response = badge_login(client, user, pin)
    assert response.status_code == 200, response.text
    tokens = response.json()
    assert claims(tokens["access_token"])["amr"] == ["badge", "pin"]
    return tokens


def assert_session_ended(client: TestClient, db: Session, tokens: Dict[str, str]) -> None:
    response = client.get(ME, headers=auth(tokens["access_token"]))
    assert response.status_code == 401, response.text
    assert response.json()["detail"] == REAUTH
    refreshed = client.post(REFRESH, json={"refresh_token": tokens["refresh_token"]}, headers=XHR)
    assert refreshed.status_code == 401, refreshed.text
    assert refreshed.json()["detail"] == REAUTH
    assert "access_token" not in refreshed.text
    assert rows(db, "TOKEN_REFRESH_FAILED")[-1].error_message == "PIN changed"


# ===========================================================================
# 1/2. A badge+PIN session ends when its PIN changes
# ===========================================================================
class TestBadgePinSessionEndsWithItsPin:
    def test_the_tokens_carry_the_verified_pins_set_time(self, client, db_session):
        user = make_user(db_session, pin=PIN)
        tokens = badge_pin_session(client, user)

        stamp = floor_pin_stamp(fresh(db_session, user).floor_pin_set_at)
        assert claims(tokens["access_token"])["pst"] == stamp
        assert claims(tokens["refresh_token"], refresh=True)["pst"] == stamp
        refreshed = client.post(REFRESH, json={"refresh_token": tokens["refresh_token"]}, headers=XHR)
        assert refreshed.status_code == 200, refreshed.text
        # Carried exactly, like amr.
        assert claims(refreshed.json()["access_token"])["pst"] == stamp
        assert claims(refreshed.json()["refresh_token"], refresh=True)["pst"] == stamp
        assert client.get(ME, headers=auth(refreshed.json()["access_token"])).status_code == 200

    def test_a_badge_only_session_carries_no_pst(self, client, db_session):
        user = make_user(db_session)
        response = badge_login(client, user)
        assert response.status_code == 200
        assert "pst" not in claims(response.json()["access_token"])
        assert "pst" not in claims(response.json()["refresh_token"], refresh=True)

    def test_a_promotion_ends_a_managers_badge_pin_session_as_the_operator(
        self, client, db_session, admin_headers, manager_headers
    ):
        """The review's scenario: the Manager must not end up holding a Quality identity."""
        operator = make_user(db_session)
        assert client.put(pin_url(operator), json={"pin": PIN}, headers=manager_headers).status_code == 200
        held_by_manager = badge_pin_session(client, operator)
        assert client.get(ME, headers=auth(held_by_manager["access_token"])).status_code == 200

        promoted = client.put(f"{API}/users/{operator.id}", json={"role": "quality"}, headers=admin_headers)
        assert promoted.status_code == 200, promoted.text

        assert_session_ended(client, db_session, held_by_manager)
        assert not [row for row in rows(db_session, "TOKEN_REFRESHED") if row.user_id == operator.id]

    def test_a_reset_ends_the_session(self, client, db_session, admin_headers):
        user = make_user(db_session)
        assert client.put(pin_url(user), json={"pin": PIN}, headers=admin_headers).status_code == 200
        tokens = badge_pin_session(client, user)

        assert client.put(pin_url(user), json={"pin": NEW_PIN}, headers=admin_headers).status_code == 200

        assert_session_ended(client, db_session, tokens)
        assert badge_pin_session(client, user, NEW_PIN)

    def test_a_clear_ends_the_session(self, client, db_session, admin_headers):
        user = make_user(db_session)
        assert client.put(pin_url(user), json={"pin": PIN}, headers=admin_headers).status_code == 200
        tokens = badge_pin_session(client, user)

        assert client.delete(pin_url(user), headers=admin_headers).status_code == 200

        assert_session_ended(client, db_session, tokens)

    def test_a_lock_and_an_unlock_do_not_end_the_session(self, client, db_session, admin_headers):
        user = make_user(db_session)
        assert client.put(pin_url(user), json={"pin": PIN}, headers=admin_headers).status_code == 200
        tokens = badge_pin_session(client, user)
        locked = fresh(db_session, user)
        locked.floor_pin_locked_until = datetime.utcnow() + timedelta(minutes=10)
        locked.floor_pin_admin_locked = True
        db_session.commit()

        assert client.get(ME, headers=auth(tokens["access_token"])).status_code == 200
        refreshed = client.post(REFRESH, json={"refresh_token": tokens["refresh_token"]}, headers=XHR)
        assert refreshed.status_code == 200, refreshed.text

        assert client.post(pin_url(user, "/unlock"), headers=admin_headers).status_code == 200
        assert client.get(ME, headers=auth(refreshed.json()["access_token"])).status_code == 200

    def test_a_pin_session_token_without_pst_is_refused(self, client, db_session):
        """Fail closed: amr says "pin" but nothing pins the session to a PIN."""
        user = make_user(db_session, pin=PIN)
        token = create_access_token(subject=user.id, company_id=1, auth_methods=["badge", "pin"])

        response = client.get(ME, headers=auth(token))

        assert response.status_code == 401 and response.json()["detail"] == REAUTH

    def test_a_password_session_is_untouched_by_a_pin_change(self, client, db_session, admin_headers):
        user = make_user(db_session, pin=PIN)
        token = create_access_token(subject=user.id, company_id=1)
        assert client.put(pin_url(user), json={"pin": NEW_PIN}, headers=admin_headers).status_code == 200
        assert client.get(ME, headers=auth(token)).status_code == 200

    def test_switch_company_refuses_a_changed_pin_session_with_its_audit_row(self, client, db_session):
        ensure_company(db_session, 2)
        platform = make_user(db_session, role=UserRole.PLATFORM_ADMIN, pin=PIN)
        tokens = badge_pin_session(client, platform)
        switched = client.post(f"{API}/auth/switch-company/2", headers=auth(tokens["access_token"]))
        assert switched.status_code == 200, switched.text
        assert claims(switched.json()["access_token"])["pst"] == claims(tokens["access_token"])["pst"]

        row = fresh(db_session, platform)
        row.floor_pin_set_at = datetime.utcnow() + timedelta(seconds=1)  # the PIN was set again
        db_session.commit()

        refused = client.post(f"{API}/auth/switch-company/2", headers=auth(tokens["access_token"]))
        assert refused.status_code == 401 and refused.json()["detail"] == REAUTH
        failed = rows(db_session, "COMPANY_SWITCH_FAILED")[-1]
        assert failed.error_message == "PIN changed" and failed.user_id == platform.id

    def test_a_crew_station_token_ends_with_its_pin(self, client, db_session, admin_headers):
        station = make_kiosk_station(db_session, company_id=1)
        user = make_user(db_session)
        assert client.put(pin_url(user), json={"pin": PIN}, headers=admin_headers).status_code == 200
        minted = client.post(
            BADGE_TOKEN_URL,
            json={"employee_id": user.employee_id, "pin": PIN},
            headers=bearer(kiosk_token_for(station)),
        )
        assert minted.status_code == 200, minted.text
        token = minted.json()["access_token"]
        assert "pst" in claims(token)
        logout = f"{API}/auth/employee-logout"
        assert client.post(logout, json={"employee_id": user.employee_id}, headers=bearer(token)).status_code == 200

        assert client.put(pin_url(user), json={"pin": NEW_PIN}, headers=admin_headers).status_code == 200

        refused = client.post(logout, json={"employee_id": user.employee_id}, headers=bearer(token))
        assert refused.status_code == 401 and refused.json()["detail"] == REAUTH


# ===========================================================================
# 3/4. Cumulative lock tiers and the per-IP wrong-PIN budget
# ===========================================================================
class TestLockTiers:
    def test_the_tiers_escalate_on_a_cumulative_count(self, client, db_session, admin_headers):
        user = make_user(db_session, pin=PIN)

        # Tier 1: 5 wrong PINs -> 15 minutes; the count is NOT reset.
        for _ in range(FLOOR_PIN_MAX_FAILURES):
            assert badge_login(client, user, WRONG_PIN).status_code == 401
        state = fresh(db_session, user)
        assert state.floor_pin_failed_attempts == FLOOR_PIN_MAX_FAILURES
        assert state.floor_pin_locked_until > datetime.utcnow() + timedelta(minutes=FLOOR_PIN_LOCKOUT_MINUTES - 1)
        assert not state.floor_pin_admin_locked

        # Tier 2: the 10th cumulative wrong PIN -> 2 hours.
        state.floor_pin_locked_until = datetime.utcnow() - timedelta(seconds=1)
        state.floor_pin_failed_attempts = FLOOR_PIN_LONG_LOCK_FAILURES - 1
        db_session.commit()
        assert badge_login(client, user, WRONG_PIN).status_code == 401
        state = fresh(db_session, user)
        assert state.floor_pin_failed_attempts == FLOOR_PIN_LONG_LOCK_FAILURES
        assert state.floor_pin_locked_until > datetime.utcnow() + timedelta(minutes=FLOOR_PIN_LONG_LOCKOUT_MINUTES - 1)
        assert rows(db_session, "EMPLOYEE_LOGIN_FAILED")[-1].extra_data == {"floor_pin_locked": True}

        # Tier 3: the 15th -> admin-locked, marked on its row.
        state.floor_pin_locked_until = datetime.utcnow() - timedelta(seconds=1)
        state.floor_pin_failed_attempts = FLOOR_PIN_ADMIN_LOCK_FAILURES - 1
        db_session.commit()
        response = badge_login(client, user, WRONG_PIN)
        assert response.status_code == 401 and response.json()["detail"] == INVALID
        state = fresh(db_session, user)
        assert state.floor_pin_admin_locked is True
        locking = rows(db_session, "EMPLOYEE_LOGIN_FAILED")[-1]
        assert locking.error_message == "Wrong PIN"
        assert locking.extra_data == {"floor_pin_locked": True, "floor_pin_admin_locked": True}

        # No time lifts it: even the right PIN gets the uniform 401, with a row, and bcrypt never runs.
        state.floor_pin_locked_until = None
        db_session.commit()
        refused = badge_login(client, user, PIN)
        assert refused.status_code == 401 and refused.json()["detail"] == INVALID
        held = rows(db_session, "EMPLOYEE_LOGIN_FAILED")[-1]
        assert held.error_message == "PIN locked" and held.extra_data == {"floor_pin_admin_locked": True}
        assert fresh(db_session, user).floor_pin_failed_attempts == FLOOR_PIN_ADMIN_LOCK_FAILURES

        # The unlock lifts the counter, the timed lock and the admin lock.
        unlocked = client.post(pin_url(user, "/unlock"), headers=admin_headers)
        assert unlocked.status_code == 200, unlocked.text
        assert unlocked.json()["floor_pin_admin_locked"] is False
        state = fresh(db_session, user)
        assert (state.floor_pin_failed_attempts, state.floor_pin_locked_until, state.floor_pin_admin_locked) == (
            0,
            None,
            False,
        )
        assert rows(db_session, "FLOOR_PIN_UNLOCKED")[-1].extra_data["floor_pin_admin_locked"] is True
        assert badge_login(client, user, PIN).status_code == 200

    def test_an_admin_lock_is_never_lifted_by_time_and_bcrypt_never_runs(self, client, db_session, monkeypatch):
        user = make_user(db_session, pin=PIN)
        state = fresh(db_session, user)
        state.floor_pin_admin_locked = True
        db_session.commit()

        def must_not_verify(*_a, **_k):
            raise AssertionError("an admin-locked PIN must not be checked")

        monkeypatch.setattr(auth_endpoints, "verify_floor_pin", must_not_verify)
        response = badge_login(client, user, PIN)

        assert response.status_code == 401 and response.json()["detail"] == INVALID
        assert fresh(db_session, user).floor_pin_failed_attempts == 0

    def test_a_correct_pin_resets_the_cumulative_count(self, client, db_session):
        user = make_user(db_session, pin=PIN)
        state = fresh(db_session, user)
        state.floor_pin_failed_attempts = 7
        db_session.commit()

        assert badge_login(client, user, PIN).status_code == 200
        assert fresh(db_session, user).floor_pin_failed_attempts == 0

    @pytest.mark.parametrize("verb", ["set", "clear"])
    def test_a_set_or_clear_lifts_an_admin_lock(self, client, db_session, admin_headers, verb):
        user = make_user(db_session, pin=PIN)
        state = fresh(db_session, user)
        state.floor_pin_admin_locked = True
        state.floor_pin_failed_attempts = FLOOR_PIN_ADMIN_LOCK_FAILURES
        db_session.commit()

        if verb == "set":
            response = client.put(pin_url(user), json={"pin": NEW_PIN}, headers=admin_headers)
        else:
            response = client.delete(pin_url(user), headers=admin_headers)

        assert response.status_code == 200, response.text
        assert response.json()["floor_pin_admin_locked"] is False
        assert fresh(db_session, user).floor_pin_failed_attempts == 0

    def test_a_manager_unlock_follows_the_manager_scope(self, client, db_session, manager_headers):
        operator = make_user(db_session, pin=PIN)
        quality = make_user(db_session, role=UserRole.QUALITY, pin=PIN)
        for user in (operator, quality):
            state = fresh(db_session, user)
            state.floor_pin_admin_locked = True
        db_session.commit()

        assert client.post(pin_url(operator, "/unlock"), headers=manager_headers).status_code == 200
        assert client.post(pin_url(quality, "/unlock"), headers=manager_headers).status_code == 403
        assert fresh(db_session, quality).floor_pin_admin_locked is True

    def test_the_crew_station_follows_the_same_tiers(self, client, db_session):
        station = make_kiosk_station(db_session, company_id=1)
        user = make_user(db_session, pin=PIN)
        state = fresh(db_session, user)
        state.floor_pin_failed_attempts = FLOOR_PIN_ADMIN_LOCK_FAILURES - 1
        db_session.commit()

        response = client.post(
            BADGE_TOKEN_URL,
            json={"employee_id": user.employee_id, "pin": WRONG_PIN},
            headers=bearer(kiosk_token_for(station)),
        )

        assert response.status_code == 401 and response.json()["detail"] == INVALID
        assert fresh(db_session, user).floor_pin_admin_locked is True
        failed = rows(db_session, "KIOSK_BADGE_TOKEN_FAILED")[-1]
        assert failed.extra_data["floor_pin_admin_locked"] is True and failed.extra_data["badge_user_id"] == user.id
        assert ip_pin_failures() == 0, "the station-gated mint charges no IP budget"


class TestPerIpWrongPinBudget:
    def test_wrong_pins_charge_their_own_budget_and_block_only_pin_attempts(self, client, db_session):
        pinned = make_user(db_session, pin=PIN)
        other = make_user(db_session, pin=NEW_PIN)
        badge_only = make_user(db_session)
        for _ in range(FLOOR_PIN_IP_MAX_WRONG_PINS - 1):
            floor_pin_ip_throttle.register_failure(TESTCLIENT_IP)

        last_wrong = badge_login(client, other, WRONG_PIN)
        assert last_wrong.status_code == 401
        assert ip_pin_failures() == FLOOR_PIN_IP_MAX_WRONG_PINS
        assert employee_login_throttle._memory.get(TESTCLIENT_IP) is None, "never the shared badge budget"

        # A PIN attempt from this IP -- even the right PIN -- is now 429, before any reservation.
        blocked = badge_login(client, pinned, PIN)
        assert blocked.status_code == 429, blocked.text
        assert int(blocked.headers["Retry-After"]) > 0
        assert fresh(db_session, pinned).floor_pin_failed_attempts == 0
        assert rows(db_session, "EMPLOYEE_LOGIN_BLOCKED")[-1].user_id == pinned.id

        # Everything that is not a PIN attempt carries on.
        challenge = badge_login(client, pinned)
        assert challenge.status_code == 401 and challenge.json()["detail"]["code"] == "floor_pin_required"
        assert badge_login(client, badge_only).status_code == 200
        unknown = client.post(EMPLOYEE_LOGIN, json={"employee_id": "UNKNOWN-9"}, headers=XHR)
        assert unknown.status_code == 401 and unknown.json()["detail"] == "Invalid employee ID"


# ===========================================================================
# 5. A promotion requires a new PIN
# ===========================================================================
class TestPromotionRequiresANewPin:
    def test_a_promoted_pin_holder_is_refused_until_an_admin_sets_a_new_pin(self, client, db_session, admin_headers):
        user = make_user(db_session, pin=PIN)
        promoted = client.put(f"{API}/users/{user.id}", json={"role": "manager"}, headers=admin_headers)
        assert promoted.status_code == 200, promoted.text
        assert promoted.json()["floor_pin_reset_required"] is True and promoted.json()["has_floor_pin"] is False
        marker_row = rows(db_session, "UPDATE")[-1]
        assert marker_row.extra_data["floor_pin_cleared_on_promotion"] is True
        assert marker_row.extra_data["floor_pin_reset_required"] is True
        assert db_session.get(Company, 1).require_floor_pin is False

        for body_pin in (None, PIN):
            refused = badge_login(client, user, body_pin)
            assert refused.status_code == 403 and refused.json()["detail"] == RESET_REQUIRED
        assert rows(db_session, "EMPLOYEE_LOGIN_FAILED")[-1].error_message == "PIN reset required"

        assert client.put(pin_url(user), json={"pin": NEW_PIN}, headers=admin_headers).status_code == 200
        assert fresh(db_session, user).floor_pin_reset_required is False
        assert badge_pin_session(client, user, NEW_PIN)

    def test_an_admin_clear_is_the_explicit_decision_to_allow_badge_only(self, client, db_session, admin_headers):
        user = make_user(db_session, pin=PIN)
        assert client.put(f"{API}/users/{user.id}", json={"role": "quality"}, headers=admin_headers).status_code == 200

        cleared = client.delete(pin_url(user), headers=admin_headers)

        assert cleared.status_code == 200 and cleared.json()["floor_pin_reset_required"] is False
        assert rows(db_session, "FLOOR_PIN_CLEARED")[-1].extra_data["cleared_pin_reset_required"] is True
        signed_in = badge_login(client, user)
        assert signed_in.status_code == 200 and claims(signed_in.json()["access_token"])["amr"] == ["badge"]

    def test_the_crew_station_refuses_it_too(self, client, db_session, admin_headers):
        station = make_kiosk_station(db_session, company_id=1)
        user = make_user(db_session, pin=PIN)
        assert client.put(f"{API}/users/{user.id}", json={"role": "quality"}, headers=admin_headers).status_code == 200

        response = client.post(
            BADGE_TOKEN_URL, json={"employee_id": user.employee_id}, headers=bearer(kiosk_token_for(station))
        )

        assert response.status_code == 403 and response.json()["detail"] == RESET_REQUIRED
        assert rows(db_session, "KIOSK_BADGE_TOKEN_FAILED")[-1].error_message == "PIN reset required"

    def test_a_badge_only_session_from_before_the_promotion_cannot_refresh(self, client, db_session, admin_headers):
        """A badge-only session of a user who later got a PIN and then a promotion is refused too."""
        user = make_user(db_session)
        tokens = badge_login(client, user).json()
        user = fresh(db_session, user)
        user.floor_pin_hash = hash_floor_pin(PIN)
        user.floor_pin_set_at = datetime.utcnow()
        db_session.commit()
        assert client.put(f"{API}/users/{user.id}", json={"role": "quality"}, headers=admin_headers).status_code == 200

        refused = client.post(REFRESH, json={"refresh_token": tokens["refresh_token"]}, headers=XHR)

        assert refused.status_code == 401 and refused.json()["detail"] == REAUTH
        assert rows(db_session, "TOKEN_REFRESH_FAILED")[-1].error_message == "PIN now required"

    def test_a_promotion_without_a_pin_requires_nothing(self, client, db_session, admin_headers):
        user = make_user(db_session)
        promoted = client.put(f"{API}/users/{user.id}", json={"role": "quality"}, headers=admin_headers)
        assert promoted.status_code == 200 and promoted.json()["floor_pin_reset_required"] is False
        assert badge_login(client, user).status_code == 200
