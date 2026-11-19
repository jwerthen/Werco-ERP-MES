"""Regressions for the PR #369 review findings on the floor PIN (badge sign-on, AS9100D 8.5.2).

1/2. **A badge+PIN session ends when its PIN does.** The tokens carry ``pst`` -- the verified
     PIN's ``floor_pin_set_at`` -- and every request, refresh and company switch refuses the
     session 401 ``floor_pin_reauth_required`` once that PIN is reset, cleared, or cleared by a
     promotion. The scenario the review traced: a Manager sets an Operator's PIN, signs in as
     the Operator, the Operator is promoted to Quality -- the Manager's session must die at its
     next request, not live on as a Quality identity. A lock does NOT end a session.
3/4. **PIN guessing is capped, and no lock is permanent** (re-check of 90296e58). Wrong PINs are
     cumulative until a correct PIN or an admin unlock / set / clear, and every 5th starts a
     longer, EXPIRING lock: 15 minutes, 2 hours, 24 hours, 48 hours, 96 hours, then 7 days per
     further 5. The attempt that reaches a tier sets its lock in the same UPDATE that reserves
     it, so no burst runs past a tier; a right PIN settles by compare-and-set. A lock of a day or
     more alerts the company's Admins (``floor_pin.lockout``). On the PUBLIC employee-login
     route, PIN attempts spend a per-IP budget of 60 at reservation and a correct PIN refunds
     it; spent, it blocks only further PIN attempts (429).
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
    FLOOR_PIN_LOCKOUT_MINUTES,
    FLOOR_PIN_MAX_FAILURES,
    floor_pin_lock_minutes,
    floor_pin_stamp,
    hash_floor_pin,
)
from app.core.login_throttle import FLOOR_PIN_IP_MAX_WRONG_PINS, employee_login_throttle, floor_pin_ip_throttle
from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.operational_event import OperationalEvent
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
        locked.floor_pin_locked_until = datetime.utcnow() + timedelta(days=7)
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
    @pytest.mark.parametrize(
        "count, minutes",
        [(5, 15), (10, 120), (15, 24 * 60), (20, 48 * 60), (25, 96 * 60), (30, 7 * 24 * 60), (45, 7 * 24 * 60)],
    )
    def test_every_fifth_wrong_pin_starts_a_longer_expiring_lock(self, client, db_session, count, minutes):
        user = make_user(db_session, pin=PIN)
        state = fresh(db_session, user)
        state.floor_pin_failed_attempts = count - 1
        db_session.commit()

        before = datetime.utcnow()
        response = badge_login(client, user, WRONG_PIN)

        assert response.status_code == 401 and response.json()["detail"] == INVALID
        state = fresh(db_session, user)
        assert state.floor_pin_failed_attempts == count, "the count is cumulative across locks"
        assert before + timedelta(minutes=minutes) <= state.floor_pin_locked_until
        assert state.floor_pin_locked_until <= datetime.utcnow() + timedelta(minutes=minutes)
        assert rows(db_session, "EMPLOYEE_LOGIN_FAILED")[-1].extra_data == {"floor_pin_locked": True}

    @pytest.mark.parametrize("count", [1, 4, 6, 9, 11, 14, 29, 31])
    def test_a_count_between_tiers_locks_nothing(self, client, db_session, count):
        user = make_user(db_session, pin=PIN)
        state = fresh(db_session, user)
        state.floor_pin_failed_attempts = count - 1
        db_session.commit()

        assert badge_login(client, user, WRONG_PIN).status_code == 401
        state = fresh(db_session, user)
        assert state.floor_pin_failed_attempts == count and state.floor_pin_locked_until is None
        assert rows(db_session, "EMPLOYEE_LOGIN_FAILED")[-1].extra_data is None

    def test_no_lock_is_permanent_even_at_the_cap(self, client, db_session):
        user = make_user(db_session, pin=PIN)
        state = fresh(db_session, user)
        state.floor_pin_failed_attempts = 100
        state.floor_pin_locked_until = datetime.utcnow() - timedelta(seconds=1)  # a 7-day lock that ran out
        db_session.commit()

        assert badge_login(client, user, PIN).status_code == 200
        state = fresh(db_session, user)
        assert (state.floor_pin_failed_attempts, state.floor_pin_locked_until) == (0, None)

    def test_the_tiers_escalate_end_to_end_and_an_unlock_lifts_them(self, client, db_session, admin_headers):
        user = make_user(db_session, pin=PIN)

        # Tier 1: the 5th wrong PIN locks for 15 minutes; the count carries on.
        for _ in range(FLOOR_PIN_MAX_FAILURES):
            assert badge_login(client, user, WRONG_PIN).status_code == 401
        state = fresh(db_session, user)
        assert state.floor_pin_failed_attempts == FLOOR_PIN_MAX_FAILURES
        assert state.floor_pin_locked_until > datetime.utcnow() + timedelta(minutes=FLOOR_PIN_LOCKOUT_MINUTES - 1)

        # While locked even the right PIN is refused, unverified and uncounted.
        assert badge_login(client, user, PIN).status_code == 401
        assert fresh(db_session, user).floor_pin_failed_attempts == FLOOR_PIN_MAX_FAILURES

        # Tier 3 after the lock runs out: the 15th -> 24 hours.
        state = fresh(db_session, user)
        state.floor_pin_locked_until = datetime.utcnow() - timedelta(seconds=1)
        state.floor_pin_failed_attempts = 14
        db_session.commit()
        assert badge_login(client, user, WRONG_PIN).status_code == 401
        state = fresh(db_session, user)
        assert state.floor_pin_locked_until > datetime.utcnow() + timedelta(hours=23)

        # An unlock lifts it and resets the count; the PIN works at once.
        unlocked = client.post(pin_url(user, "/unlock"), headers=admin_headers)
        assert unlocked.status_code == 200, unlocked.text
        assert "floor_pin_admin_locked" not in unlocked.json()
        state = fresh(db_session, user)
        assert (state.floor_pin_failed_attempts, state.floor_pin_locked_until) == (0, None)
        assert badge_login(client, user, PIN).status_code == 200

    def test_a_correct_pin_resets_the_cumulative_count(self, client, db_session):
        user = make_user(db_session, pin=PIN)
        state = fresh(db_session, user)
        state.floor_pin_failed_attempts = 7
        db_session.commit()

        assert badge_login(client, user, PIN).status_code == 200
        assert fresh(db_session, user).floor_pin_failed_attempts == 0

    def test_a_right_pin_on_the_boundary_attempt_clears_the_lock_it_set(self, client, db_session):
        """The 5th attempt locks the PIN as it is reserved; when it turns out to be the RIGHT PIN,
        the compare-and-set settle (count still 5) clears that very lock and signs in."""
        user = make_user(db_session, pin=PIN)
        state = fresh(db_session, user)
        state.floor_pin_failed_attempts = FLOOR_PIN_MAX_FAILURES - 1
        db_session.commit()

        assert badge_login(client, user, PIN).status_code == 200
        state = fresh(db_session, user)
        assert (state.floor_pin_failed_attempts, state.floor_pin_locked_until) == (0, None)

    @pytest.mark.parametrize("verb", ["set", "clear"])
    def test_a_set_or_clear_lifts_a_lock_and_resets_the_count(self, client, db_session, admin_headers, verb):
        user = make_user(db_session, pin=PIN)
        state = fresh(db_session, user)
        state.floor_pin_locked_until = datetime.utcnow() + timedelta(days=7)
        state.floor_pin_failed_attempts = 30
        db_session.commit()

        if verb == "set":
            response = client.put(pin_url(user), json={"pin": NEW_PIN}, headers=admin_headers)
        else:
            response = client.delete(pin_url(user), headers=admin_headers)

        assert response.status_code == 200, response.text
        assert response.json()["floor_pin_locked_until"] is None
        assert fresh(db_session, user).floor_pin_failed_attempts == 0

    def test_a_manager_unlock_follows_the_manager_scope(self, client, db_session, manager_headers):
        operator = make_user(db_session, pin=PIN)
        quality = make_user(db_session, role=UserRole.QUALITY, pin=PIN)
        for user in (operator, quality):
            state = fresh(db_session, user)
            state.floor_pin_locked_until = datetime.utcnow() + timedelta(days=1)
        db_session.commit()

        assert client.post(pin_url(operator, "/unlock"), headers=manager_headers).status_code == 200
        assert client.post(pin_url(quality, "/unlock"), headers=manager_headers).status_code == 403
        assert fresh(db_session, quality).floor_pin_locked_until is not None

    def test_the_crew_station_follows_the_same_tiers(self, client, db_session):
        station = make_kiosk_station(db_session, company_id=1)
        user = make_user(db_session, pin=PIN)
        state = fresh(db_session, user)
        state.floor_pin_failed_attempts = 14
        db_session.commit()

        response = client.post(
            BADGE_TOKEN_URL,
            json={"employee_id": user.employee_id, "pin": WRONG_PIN},
            headers=bearer(kiosk_token_for(station)),
        )

        assert response.status_code == 401 and response.json()["detail"] == INVALID
        assert fresh(db_session, user).floor_pin_locked_until > datetime.utcnow() + timedelta(hours=23)
        failed = rows(db_session, "KIOSK_BADGE_TOKEN_FAILED")[-1]
        assert failed.extra_data == {"badge_user_id": user.id, "floor_pin_locked": True}
        assert lockout_events(db_session), "a day-long lock from the station alerts the Admins too"
        assert ip_pin_failures() == 0, "the station-gated mint charges no IP budget"


class TestReservationUnderConcurrency:
    """Direct statement sequencing of interleaved attempts on one badge (the reservation and the
    settle are each ONE conditional statement, so their order IS the interleaving)."""

    def test_the_attempt_reaching_a_tier_locks_it_and_the_rest_of_a_burst_is_refused(self, db_session):
        user = make_user(db_session, pin=PIN)
        state = fresh(db_session, user)
        state.floor_pin_failed_attempts = 3
        db_session.commit()
        now = datetime.utcnow()

        burst = [auth_endpoints._reserve_floor_pin_attempt(db_session, user.id, now) for _ in range(6)]

        # Attempt 4 is free, attempt 5 reaches the tier and locks in the same statement; the
        # other four reserve nothing -- the burst cannot run past the tier.
        assert burst == [4, 5, None, None, None, None]
        state = fresh(db_session, user)
        assert state.floor_pin_failed_attempts == 5
        assert state.floor_pin_locked_until == now + timedelta(minutes=FLOOR_PIN_LOCKOUT_MINUTES)

    def test_a_burst_can_never_skip_a_tier(self, db_session):
        user = make_user(db_session, pin=PIN)
        state = fresh(db_session, user)
        state.floor_pin_failed_attempts = 8
        db_session.commit()
        now = datetime.utcnow()

        assert [auth_endpoints._reserve_floor_pin_attempt(db_session, user.id, now) for _ in range(4)] == [
            9,
            10,
            None,
            None,
        ]
        assert fresh(db_session, user).floor_pin_locked_until == now + timedelta(minutes=floor_pin_lock_minutes(10))

    def test_a_right_pin_whose_count_moved_on_to_a_lock_is_refused(self, db_session):
        user = make_user(db_session, pin=PIN)
        state = fresh(db_session, user)
        state.floor_pin_failed_attempts = 3
        db_session.commit()
        now = datetime.utcnow()

        mine = auth_endpoints._reserve_floor_pin_attempt(db_session, user.id, now)  # 4: the right PIN
        racing = auth_endpoints._reserve_floor_pin_attempt(db_session, user.id, now)  # 5: locks the PIN

        assert (mine, racing) == (4, 5)
        assert auth_endpoints._settle_verified_floor_pin(db_session, user.id, mine, now) is False
        state = fresh(db_session, user)
        assert state.floor_pin_failed_attempts == 5 and state.floor_pin_locked_until is not None

    def test_a_right_pin_with_an_unlocking_race_signs_in_without_forgiving_the_race(self, db_session):
        user = make_user(db_session, pin=PIN)
        now = datetime.utcnow()

        mine = auth_endpoints._reserve_floor_pin_attempt(db_session, user.id, now)  # 1: the right PIN
        racing = auth_endpoints._reserve_floor_pin_attempt(db_session, user.id, now)  # 2: no tier

        assert auth_endpoints._settle_verified_floor_pin(db_session, user.id, mine, now) is True
        # The compare-and-set saw a count of 2, not 1: nothing is reset, so the racing guess stays counted.
        assert fresh(db_session, user).floor_pin_failed_attempts == racing == 2

    def test_a_right_pin_settles_by_compare_and_set_when_nobody_raced(self, db_session):
        user = make_user(db_session, pin=PIN)
        state = fresh(db_session, user)
        state.floor_pin_failed_attempts = 4
        db_session.commit()
        now = datetime.utcnow()

        mine = auth_endpoints._reserve_floor_pin_attempt(db_session, user.id, now)  # 5: locks as it reserves
        assert fresh(db_session, user).floor_pin_locked_until is not None

        assert auth_endpoints._settle_verified_floor_pin(db_session, user.id, mine, now) is True
        state = fresh(db_session, user)
        assert (state.floor_pin_failed_attempts, state.floor_pin_locked_until) == (0, None)


def lockout_events(db: Session) -> List[OperationalEvent]:
    db.expire_all()
    return db.query(OperationalEvent).filter(OperationalEvent.event_type == "floor_pin_lockout").all()


class TestLongLockAlertsTheAdmins:
    def test_a_day_long_lock_records_a_server_event_with_no_pin_or_badge(self, client, db_session):
        user = make_user(db_session, pin=PIN)
        state = fresh(db_session, user)
        state.floor_pin_failed_attempts = 14
        db_session.commit()

        assert badge_login(client, user, WRONG_PIN).status_code == 401

        (event,) = lockout_events(db_session)
        assert event.company_id == 1 and event.entity_type == "user" and event.entity_id == user.id
        assert event.user_id is None
        assert event.event_payload == {"user_id": user.id, "tier": 3, "lock_hours": 24}
        text = str(event.event_payload)
        assert user.employee_id not in text and WRONG_PIN not in text and PIN not in text

    @pytest.mark.parametrize("count", [5, 10])
    def test_a_shorter_lock_does_not_alert(self, client, db_session, count):
        user = make_user(db_session, pin=PIN)
        state = fresh(db_session, user)
        state.floor_pin_failed_attempts = count - 1
        db_session.commit()

        assert badge_login(client, user, WRONG_PIN).status_code == 401
        assert lockout_events(db_session) == []

    def test_a_right_pin_on_a_day_long_boundary_does_not_alert(self, client, db_session):
        user = make_user(db_session, pin=PIN)
        state = fresh(db_session, user)
        state.floor_pin_failed_attempts = 14
        db_session.commit()

        assert badge_login(client, user, PIN).status_code == 200
        assert lockout_events(db_session) == []

    def test_the_alert_reaches_the_companys_admins_in_app_with_a_users_link(self, db_session, monkeypatch):
        import asyncio
        from unittest.mock import AsyncMock

        import app.services.notification_dispatch as dispatch
        from app.models.notification import Notification
        from app.services.notification_catalog import CATALOG, CHANNEL_IN_APP, SOURCE_EVENT_TYPE_TO_KEY

        monkeypatch.setattr(dispatch, "enqueue_job", AsyncMock())
        monkeypatch.setattr(dispatch, "_dedup_reserve", AsyncMock(return_value=True))
        entry = CATALOG[SOURCE_EVENT_TYPE_TO_KEY["floor_pin_lockout"]]
        assert entry.event_key == "floor_pin.lockout" and entry.roles == (UserRole.ADMIN,)
        assert entry.default_channels == frozenset({CHANNEL_IN_APP}) and entry.mandatory_channel == CHANNEL_IN_APP
        assert entry.sms_eligible is False

        admin = make_user(db_session, role=UserRole.ADMIN)
        manager = make_user(db_session, role=UserRole.MANAGER)
        locked = make_user(db_session, pin=PIN)
        ensure_company(db_session, 2)
        foreign_admin = make_user(db_session, role=UserRole.ADMIN)
        foreign_admin.company_id = 2
        db_session.commit()
        event = OperationalEvent(
            company_id=1,
            event_type="floor_pin_lockout",
            source_module="auth",
            entity_type="user",
            entity_id=locked.id,
            user_id=None,
            severity="warning",
            event_payload={"user_id": locked.id, "tier": 3, "lock_hours": 24},
        )
        db_session.add(event)
        db_session.commit()

        asyncio.run(dispatch.dispatch_for_event(db_session, event))
        db_session.commit()

        notified = {row.user_id: row for row in db_session.query(Notification).all()}
        assert admin.id in notified
        assert manager.id not in notified and foreign_admin.id not in notified and locked.id not in notified
        assert notified[admin.id].event_key == "floor_pin.lockout"
        assert notified[admin.id].link == "/users"


class TestPerIpPinBudget:
    def test_the_budget_is_sixty_and_correct_sign_ins_cost_nothing(self, client, db_session):
        assert FLOOR_PIN_IP_MAX_WRONG_PINS == 60
        user = make_user(db_session, pin=PIN)
        for _ in range(3):
            assert badge_login(client, user, PIN).status_code == 200
        assert ip_pin_failures() == 0, "a correct PIN refunds the unit its attempt spent"

        assert badge_login(client, user, WRONG_PIN).status_code == 401
        assert ip_pin_failures() == 1

    def test_a_spent_budget_blocks_only_pin_attempts(self, client, db_session):
        pinned = make_user(db_session, pin=PIN)
        other = make_user(db_session, pin=NEW_PIN)
        badge_only = make_user(db_session)
        for _ in range(FLOOR_PIN_IP_MAX_WRONG_PINS - 1):
            assert floor_pin_ip_throttle.try_spend(TESTCLIENT_IP)

        last_wrong = badge_login(client, other, WRONG_PIN)
        assert last_wrong.status_code == 401
        assert ip_pin_failures() == FLOOR_PIN_IP_MAX_WRONG_PINS
        assert employee_login_throttle._memory.get(TESTCLIENT_IP) is None, "never the shared badge budget"

        # A PIN attempt from this address -- even the right PIN -- is now 429, before any reservation.
        blocked = badge_login(client, pinned, PIN)
        assert blocked.status_code == 429, blocked.text
        assert int(blocked.headers["Retry-After"]) > 0
        assert fresh(db_session, pinned).floor_pin_failed_attempts == 0
        assert rows(db_session, "EMPLOYEE_LOGIN_BLOCKED")[-1].user_id == pinned.id
        assert ip_pin_failures() == FLOOR_PIN_IP_MAX_WRONG_PINS, "a refused attempt spends nothing"

        # Everything that is not a PIN attempt carries on.
        challenge = badge_login(client, pinned)
        assert challenge.status_code == 401 and challenge.json()["detail"]["code"] == "floor_pin_required"
        assert badge_login(client, badge_only).status_code == 200
        unknown = client.post(EMPLOYEE_LOGIN, json={"employee_id": "UNKNOWN-9"}, headers=XHR)
        assert unknown.status_code == 401 and unknown.json()["detail"] == "Invalid employee ID"

    def test_the_budget_is_spent_at_reservation_so_a_burst_cannot_overshoot_it(self):
        for _ in range(FLOOR_PIN_IP_MAX_WRONG_PINS - 1):
            assert floor_pin_ip_throttle.try_spend(TESTCLIENT_IP)
        # Two attempts arriving together: the spend IS the check, so exactly one gets through.
        assert [floor_pin_ip_throttle.try_spend(TESTCLIENT_IP) for _ in range(2)] == [True, False]
        assert ip_pin_failures() == FLOOR_PIN_IP_MAX_WRONG_PINS


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
