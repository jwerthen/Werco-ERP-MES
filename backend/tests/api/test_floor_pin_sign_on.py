"""Floor PIN at badge / employee-ID sign-on (PR D, AS9100D 8.5.2 / 7.5.3).

Both doors the floor signs in through -- ``POST /auth/employee-login`` (desk Badge Only mode
and the solo kiosk) and ``POST /auth/kiosk-badge-token`` (every crew-station scan) -- run ONE
PIN step (``auth._floor_pin_sign_on_step``). This file pins that step from the outside, the
refusals as hard as the happy path:

* **Challenge.** A user with a PIN, signing in without one, gets 401
  ``{"code": "floor_pin_required"}`` -- no audit row, no IP-throttle charge, no counter move.
* **Wrong PIN.** 401 "Invalid badge or PIN", a FAILED "Wrong PIN" row with no PIN in it, and
  one count on the per-user counter -- a single conditional SQL ``UPDATE ... n + 1``, RESERVED
  and committed before the PIN is checked, so racing or crashing attempts never get a guess
  back. No outcome of the PIN step charges the per-IP throttle: the per-user lock is the limit
  on PIN guessing, so one person's typos never lock a shared kiosk IP (unknown badges still
  charge it, as before).
* **Lock at 5** for 15 minutes; while locked the right PIN is refused too ("PIN locked"),
  the PIN is not even verified, and nothing is counted. A racing lock is never extended. The
  lock runs out on its own and the next window starts at zero. The lock is on the PIN, never
  the password login.
* **Policy.** With ``require_floor_pin`` on, a badge with no PIN is 403
  ``{"code": "floor_pin_not_set"}``; off, a supplied PIN is ignored.
* **amr.** Tokens carry ``["badge"]`` / ``["badge", "pin"]``; a password login carries none.
  Refresh and switch-company keep it exactly, and refuse a badge-only session (401
  ``floor_pin_reauth_required``) once a PIN or the policy applies -- while a badge+PIN session
  with a locked PIN still renews.
* **Every later write** records ``extra_data.auth_methods``; password sessions and API tokens
  do not, even on the same identity-mapped ``User`` object.
* **Crew station.** Same rules; failure rows are station-keyed and name ``badge_user_id``; no
  per-IP throttle; a foreign-tenant badge never reaches the PIN step.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app.api.endpoints import auth as auth_endpoints
from app.core.config import settings
from app.core.floor_pin import FLOOR_PIN_LOCKOUT_MINUTES, FLOOR_PIN_MAX_FAILURES, hash_floor_pin
from app.core.login_throttle import EMPLOYEE_LOGIN_MAX_FAILURES, employee_login_throttle
from app.core.security import create_refresh_token, normalize_auth_methods
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.user import User, UserRole
from app.services.audit_service import AuditService
from tests.api.export_control_helpers import instant_strings, issue_token, without_known_instants
from tests.api.kiosk_test_helpers import BADGE_TOKEN_URL, bearer, ensure_company, kiosk_token_for, make_kiosk_station
from tests.conftest import TEST_PASSWORD, TEST_PASSWORD_HASH

pytestmark = pytest.mark.api

EMPLOYEE_LOGIN = "/api/v1/auth/employee-login"
PASSWORD_LOGIN = "/api/v1/auth/login"
REFRESH = "/api/v1/auth/refresh"
EMPLOYEE_LOGOUT = "/api/v1/auth/employee-logout"
PHONE = "/api/v1/users/me/phone"
XHR = {"X-Requested-With": "XMLHttpRequest"}

# Six digits each, so neither can turn up by chance in an id or a short field we scan. A
# microsecond field is six digits too: search a body that carries instants through
# without_known_instants, naming the exact instants it legitimately holds.
PIN = "830416"
WRONG_PIN = "947215"

CHALLENGE = {"code": "floor_pin_required", "message": "Enter your PIN."}
NOT_SET = {"code": "floor_pin_not_set", "message": "No PIN is set for this badge. Ask a supervisor to set one."}
REAUTH = {"code": "floor_pin_reauth_required", "message": "Sign in again with your badge and PIN."}
INVALID = "Invalid badge or PIN"
TESTCLIENT_IP = "testclient"


@pytest.fixture(autouse=True)
def _memory_mode_throttle(monkeypatch):
    """Deterministic per-IP throttle state: memory mode, empty before and after."""
    monkeypatch.setattr(settings, "REDIS_URL", None)
    employee_login_throttle.reset()
    yield
    employee_login_throttle.reset()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def make_badge_user(
    db: Session,
    employee_id: str,
    *,
    pin: Optional[str] = None,
    role: UserRole = UserRole.OPERATOR,
    company_id: int = 1,
    is_active: bool = True,
) -> User:
    ensure_company(db, company_id)
    user = User(
        email=f"{employee_id.lower()}@werco.com",
        employee_id=employee_id,
        first_name="Floor",
        last_name="Operator",
        hashed_password=TEST_PASSWORD_HASH,
        role=role,
        is_active=is_active,
        company_id=company_id,
    )
    if pin is not None:
        user.floor_pin_hash = hash_floor_pin(pin)
        user.floor_pin_set_at = datetime.utcnow()
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def fresh(db: Session, user: User) -> User:
    db.expire_all()
    return db.get(User, user.id)


def access_claims(token: str) -> Dict[str, Any]:
    return jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])


def refresh_claims(token: str) -> Dict[str, Any]:
    return jwt.decode(token, settings.REFRESH_TOKEN_SECRET_KEY, algorithms=[settings.ALGORITHM])


def audit_rows(db: Session, action: Optional[str] = None) -> List[AuditLog]:
    db.expire_all()
    query = db.query(AuditLog)
    if action is not None:
        query = query.filter(AuditLog.action == action)
    return query.order_by(AuditLog.id).all()


def ip_failures() -> int:
    entry = employee_login_throttle._memory.get(TESTCLIENT_IP)
    return 0 if entry is None else entry[0]


def set_policy(db: Session, on: bool, company_id: int = 1) -> None:
    ensure_company(db, company_id)
    company = db.get(Company, company_id)
    company.require_floor_pin = on
    db.commit()


def badge_login(client: TestClient, employee_id: str, **body: Any):
    return client.post(EMPLOYEE_LOGIN, json={"employee_id": employee_id, **body}, headers=XHR)


def password_login(client: TestClient, username: str):
    return client.post(PASSWORD_LOGIN, data={"username": username, "password": TEST_PASSWORD}, headers=XHR)


def assert_no_pin_in(rows: List[AuditLog], *pins: str) -> None:
    """No PIN value in any field a person or a tool reads off an audit row."""
    for row in rows:
        readable = json.dumps(
            [
                row.extra_data,
                row.old_values,
                row.new_values,
                row.description,
                row.error_message,
                row.resource_identifier,
            ],
            default=str,
        )
        for pin in pins:
            assert pin not in readable, f"PIN leaked into audit row {row.action}: {readable}"


# ===========================================================================
# 1. No PIN, policy off: sign-on is what it always was, plus amr ["badge"]
# ===========================================================================
class TestNoPinPolicyOff:
    def test_signs_in_on_the_badge_alone_with_a_badge_amr(self, client, db_session):
        make_badge_user(db_session, "FP-4821")

        response = badge_login(client, "FP-4821")

        assert response.status_code == 200, response.text
        assert access_claims(response.json()["access_token"])["amr"] == ["badge"]
        assert refresh_claims(response.json()["refresh_token"])["amr"] == ["badge"]
        (row,) = audit_rows(db_session, "EMPLOYEE_LOGIN_SUCCESS")
        assert row.extra_data["auth_methods"] == ["badge"]

    @pytest.mark.parametrize("supplied", [PIN, WRONG_PIN, "0", "not-a-pin"])
    def test_a_supplied_pin_is_ignored_and_never_upgrades_the_amr(self, client, db_session, supplied):
        user = make_badge_user(db_session, "FP-4822")

        response = badge_login(client, "FP-4822", pin=supplied)

        assert response.status_code == 200, response.text
        assert access_claims(response.json()["access_token"])["amr"] == ["badge"], "a PIN nobody set proved nothing"
        assert fresh(db_session, user).floor_pin_failed_attempts == 0
        assert ip_failures() == 0

    def test_a_password_login_carries_no_amr_and_the_badge_token_only_adds_it(self, client, db_session):
        make_badge_user(db_session, "FP-4823", pin=PIN)

        password = password_login(client, "fp-4823@werco.com")
        badge = badge_login(client, "FP-4823", pin=PIN)

        assert password.status_code == 200 and badge.status_code == 200
        pw_access = access_claims(password.json()["access_token"])
        pw_refresh = refresh_claims(password.json()["refresh_token"])
        assert "amr" not in pw_access and "amr" not in pw_refresh
        # Byte-identical claim SET otherwise: the badge+PIN token is the password token plus amr
        # and pst (the verified PIN's set time, which ends the session when the PIN changes).
        assert "pst" not in pw_access and "pst" not in pw_refresh
        assert set(access_claims(badge.json()["access_token"])) == set(pw_access) | {"amr", "pst"}
        assert set(refresh_claims(badge.json()["refresh_token"])) == set(pw_refresh) | {"amr", "pst"}


# ===========================================================================
# 2. The challenge: not a failure
# ===========================================================================
class TestChallenge:
    @pytest.mark.parametrize("body", [{}, {"pin": None}, {"pin": ""}], ids=["absent", "null", "empty"])
    def test_a_badge_with_a_pin_and_no_pin_sent_is_challenged_and_nothing_is_written(self, client, db_session, body):
        user = make_badge_user(db_session, "FP-5101", pin=PIN)
        rows_before = len(audit_rows(db_session))

        response = badge_login(client, "FP-5101", **body)

        assert response.status_code == 401, response.text
        assert response.json()["detail"] == CHALLENGE
        assert "access_token" not in response.text
        assert len(audit_rows(db_session)) == rows_before, "a challenge is not an audit event"
        assert ip_failures() == 0, "a challenge must not charge the shared kiosk IP"
        user = fresh(db_session, user)
        assert user.floor_pin_failed_attempts == 0 and user.floor_pin_locked_until is None

    def test_repeated_challenges_never_reach_the_ip_throttle(self, client, db_session):
        """EMPLOYEE_LOGIN_MAX_FAILURES challenges in a row, then the right PIN still signs in: a
        challenge counted as a failure would have 429'd that last request."""
        make_badge_user(db_session, "FP-5102", pin=PIN)
        for _ in range(EMPLOYEE_LOGIN_MAX_FAILURES):
            assert badge_login(client, "FP-5102").json()["detail"] == CHALLENGE

        response = badge_login(client, "FP-5102", pin=PIN)

        assert response.status_code == 200, response.text
        assert ip_failures() == 0

    def test_a_locked_pin_still_answers_the_challenge_without_saying_it_is_locked(self, client, db_session):
        user = make_badge_user(db_session, "FP-5103", pin=PIN)
        user.floor_pin_locked_until = datetime.utcnow() + timedelta(minutes=10)
        db_session.commit()
        rows_before = len(audit_rows(db_session))

        response = badge_login(client, "FP-5103")

        assert response.status_code == 401 and response.json()["detail"] == CHALLENGE
        assert len(audit_rows(db_session)) == rows_before

    def test_an_inactive_account_is_refused_before_the_pin_step(self, client, db_session):
        user = make_badge_user(db_session, "FP-5104", pin=PIN, is_active=False)

        response = badge_login(client, "FP-5104", pin=WRONG_PIN)

        assert response.status_code == 403
        assert response.json()["detail"] == "User account is disabled", "the PIN step never runs for it"
        assert fresh(db_session, user).floor_pin_failed_attempts == 0

    def test_an_unknown_badge_is_refused_before_the_pin_step(self, client, db_session):
        response = badge_login(client, "FP-5199", pin=PIN)

        assert response.status_code == 401
        assert response.json()["detail"] == "Invalid employee ID"

    def test_a_password_locked_account_is_refused_before_the_pin_step_and_the_pin_counter_is_untouched(
        self, client, db_session
    ):
        user = make_badge_user(db_session, "FP-5105", pin=PIN)
        user.locked_until = datetime.utcnow() + timedelta(minutes=30)
        db_session.commit()

        response = badge_login(client, "FP-5105", pin=WRONG_PIN)

        assert response.status_code == 403
        assert response.json()["detail"] == "Account is locked. Please contact administrator."
        assert fresh(db_session, user).floor_pin_failed_attempts == 0
        assert audit_rows(db_session, "EMPLOYEE_LOGIN_FAILED") == []


# ===========================================================================
# 3. Right and wrong PINs
# ===========================================================================
class TestRightPin:
    def test_the_right_pin_mints_badge_and_pin_and_records_it(self, client, db_session):
        user = make_badge_user(db_session, "FP-6201", pin=PIN)

        response = badge_login(client, "FP-6201", pin=PIN)

        assert response.status_code == 200, response.text
        assert access_claims(response.json()["access_token"])["amr"] == ["badge", "pin"]
        assert refresh_claims(response.json()["refresh_token"])["amr"] == ["badge", "pin"]
        # The user's created_at / updated_at end in six-digit microsecond fields, the PIN's own
        # shape. Remove exactly the instants the user row holds, read back after the sign-on
        # stamped it; any other instant in the body fails the test outright. (The two JWTs are
        # base64url: no raw instant, and a given six-digit run in them is ~1e-10 per token.)
        assert PIN not in without_known_instants(response.text, *instant_strings(fresh(db_session, user)))
        (row,) = audit_rows(db_session, "EMPLOYEE_LOGIN_SUCCESS")
        assert row.user_id == user.id
        assert row.extra_data["auth_methods"] == ["badge", "pin"]
        assert_no_pin_in(audit_rows(db_session), PIN)
        assert ip_failures() == 0

    def test_a_pin_sent_as_a_json_number_is_read_as_its_digits(self, client, db_session):
        make_badge_user(db_session, "FP-6202", pin=PIN)

        response = badge_login(client, "FP-6202", pin=int(PIN))

        assert response.status_code == 200, response.text
        assert access_claims(response.json()["access_token"])["amr"] == ["badge", "pin"]

    def test_the_right_pin_clears_the_wrong_pin_count_and_an_expired_lock(self, client, db_session):
        user = make_badge_user(db_session, "FP-6203", pin=PIN)
        user.floor_pin_failed_attempts = FLOOR_PIN_MAX_FAILURES - 1
        user.floor_pin_locked_until = datetime.utcnow() - timedelta(seconds=1)
        db_session.commit()

        response = badge_login(client, "FP-6203", pin=PIN)

        assert response.status_code == 200, response.text
        user = fresh(db_session, user)
        assert user.floor_pin_failed_attempts == 0
        assert user.floor_pin_locked_until is None


class TestWrongPin:
    def test_a_wrong_pin_is_refused_counted_and_audited_without_the_value(self, client, db_session):
        user = make_badge_user(db_session, "FP-6301", pin=PIN)

        response = badge_login(client, "FP-6301", pin=WRONG_PIN)

        assert response.status_code == 401
        assert response.json()["detail"] == INVALID
        assert WRONG_PIN not in response.text and PIN not in response.text
        user = fresh(db_session, user)
        assert user.floor_pin_failed_attempts == 1
        assert user.floor_pin_locked_until is None
        # Separate counters: the password lockout is untouched.
        assert user.failed_login_attempts in (0, None) and user.locked_until is None
        assert ip_failures() == 0, "the per-user lock, not the shared kiosk IP, bounds PIN guessing"
        (row,) = audit_rows(db_session, "EMPLOYEE_LOGIN_FAILED")
        assert row.error_message == "Wrong PIN"
        assert row.user_id == user.id
        assert "floor_pin_locked" not in (row.extra_data or {}), "only the locking attempt carries the flag"
        assert_no_pin_in(audit_rows(db_session), PIN, WRONG_PIN)

    @pytest.mark.parametrize(
        "malformed",
        [[8, 3, 0, 4, 1, 6], {"pin": PIN}, True, 830416.0, "83 04 16", "8304160000000"],
        ids=["list", "object", "bool", "float", "spaced", "too-long"],
    )
    def test_a_malformed_pin_is_a_wrong_pin_never_a_422_that_echoes_it(self, client, db_session, malformed):
        user = make_badge_user(db_session, "FP-6302", pin=PIN)

        response = badge_login(client, "FP-6302", pin=malformed)

        assert response.status_code == 401, response.text
        assert response.json()["detail"] == INVALID
        assert PIN not in response.text
        assert fresh(db_session, user).floor_pin_failed_attempts == 1

    def test_the_wrong_pin_count_is_one_sql_increment(self, client, db_session):
        """The increment is ``SET n = coalesce(n, 0) + 1`` in SQL: one UPDATE that reads the
        column itself, not a value computed from the loaded row and written back."""
        make_badge_user(db_session, "FP-6303", pin=PIN)
        engine = db_session.get_bind()
        statements: List[str] = []

        def capture(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", capture)
        try:
            response = badge_login(client, "FP-6303", pin=WRONG_PIN)
        finally:
            event.remove(engine, "before_cursor_execute", capture)

        assert response.status_code == 401
        counter_writes = [
            s for s in statements if re.match(r"\s*UPDATE\s+users\b", s, re.I) and "floor_pin_failed_attempts" in s
        ]
        assert len(counter_writes) == 1, counter_writes
        assert re.search(
            r"floor_pin_failed_attempts\s*=\s*\(?\s*coalesce\(\s*users\.floor_pin_failed_attempts",
            counter_writes[0],
            re.I,
        ), counter_writes[0]


class TestAttemptReservation:
    """The per-user counter is spent BEFORE bcrypt runs, by one conditional SQL ``UPDATE``.

    Two wrong PINs racing on one badge must spend two guesses, never one; a lock in force
    reserves nothing; a racing lock is never extended; and a right PIN verified while a racing
    wrong guess locked the badge is refused as locked.
    """

    def test_the_reservation_counts_from_the_database_not_from_the_loaded_row(self, db_session):
        user = make_badge_user(db_session, "FP-6401", pin=PIN)
        # Another request already counted three wrong PINs; this session's copy still reads 0.
        db_session.execute(text("UPDATE users SET floor_pin_failed_attempts = 3 WHERE id = :id"), {"id": user.id})
        assert user.floor_pin_failed_attempts == 0

        attempts = auth_endpoints._reserve_floor_pin_attempt(db_session, user.id, datetime.utcnow())

        assert attempts == 4, "a read-modify-write of the loaded row would have counted 1"
        assert fresh(db_session, user).floor_pin_failed_attempts == 4

    def test_a_lock_in_force_reserves_nothing(self, db_session):
        user = make_badge_user(db_session, "FP-6402", pin=PIN)
        user.floor_pin_failed_attempts = 2
        user.floor_pin_locked_until = datetime.utcnow() + timedelta(minutes=5)
        db_session.commit()

        assert auth_endpoints._reserve_floor_pin_attempt(db_session, user.id, datetime.utcnow()) is None
        assert fresh(db_session, user).floor_pin_failed_attempts == 2

    def test_an_expired_lock_does_not_block_the_reservation(self, db_session):
        user = make_badge_user(db_session, "FP-6403", pin=PIN)
        user.floor_pin_locked_until = datetime.utcnow() - timedelta(seconds=1)
        db_session.commit()

        assert auth_endpoints._reserve_floor_pin_attempt(db_session, user.id, datetime.utcnow()) == 1

    def test_the_reservation_reaching_a_tier_sets_its_lock_and_keeps_the_count(self, db_session):
        """PR #369 re-check: the attempt that REACHES a tier locks the PIN in the same statement
        that reserves it, so no concurrent attempt runs past the tier; the count is cumulative."""
        user = make_badge_user(db_session, "FP-6404", pin=PIN)
        user.floor_pin_failed_attempts = FLOOR_PIN_MAX_FAILURES - 1
        db_session.commit()
        now = datetime.utcnow()

        assert auth_endpoints._reserve_floor_pin_attempt(db_session, user.id, now) == FLOOR_PIN_MAX_FAILURES

        user = fresh(db_session, user)
        assert user.floor_pin_failed_attempts == FLOOR_PIN_MAX_FAILURES
        assert user.floor_pin_locked_until == now + timedelta(minutes=FLOOR_PIN_LOCKOUT_MINUTES)

    def test_a_lock_in_force_is_never_extended_by_a_racing_attempt(self, db_session):
        user = make_badge_user(db_session, "FP-6405", pin=PIN)
        first_lock = datetime.utcnow() + timedelta(minutes=3)
        user.floor_pin_failed_attempts = FLOOR_PIN_MAX_FAILURES - 1
        user.floor_pin_locked_until = first_lock
        db_session.commit()

        assert auth_endpoints._reserve_floor_pin_attempt(db_session, user.id, datetime.utcnow()) is None
        user = fresh(db_session, user)
        assert user.floor_pin_locked_until == first_lock
        assert user.floor_pin_failed_attempts == FLOOR_PIN_MAX_FAILURES - 1

    def test_settling_a_right_pin_is_refused_when_a_racing_attempt_locked_it(self, db_session):
        """The settle is a compare-and-set on the reserved count: another attempt reserved since
        (3 is no longer this attempt's 2) and locked the PIN, so the right PIN is refused and
        nothing is reset."""
        user = make_badge_user(db_session, "FP-6406", pin=PIN)
        user.floor_pin_failed_attempts = 3
        user.floor_pin_locked_until = datetime.utcnow() + timedelta(minutes=3)
        db_session.commit()

        assert auth_endpoints._settle_verified_floor_pin(db_session, user.id, 2, datetime.utcnow()) is False
        user = fresh(db_session, user)
        assert user.floor_pin_failed_attempts == 3 and user.floor_pin_locked_until is not None

    def test_the_guess_is_spent_even_when_the_check_crashes(self, client, db_session, monkeypatch):
        """The reservation is committed before the PIN is checked: a crash afterwards never
        hands the guess back."""
        user = make_badge_user(db_session, "FP-6407", pin=PIN)

        def crash(*_args, **_kwargs):
            raise RuntimeError("KDF exploded")

        monkeypatch.setattr(auth_endpoints, "verify_floor_pin", crash)

        with pytest.raises(RuntimeError):
            badge_login(client, "FP-6407", pin=WRONG_PIN)

        db_session.rollback()
        assert fresh(db_session, user).floor_pin_failed_attempts == 1

    def test_the_reservation_survives_a_lost_audit_row(self, client, db_session, monkeypatch):
        """The count is committed before the failure row is written: losing the row (here, the
        audit writer blowing up) never hands the guess back."""
        user = make_badge_user(db_session, "FP-6409", pin=PIN)

        def broken_audit(*_args, **_kwargs):
            raise RuntimeError("audit down")

        monkeypatch.setattr(auth_endpoints, "log_auth_event", broken_audit)

        with pytest.raises(RuntimeError):
            badge_login(client, "FP-6409", pin=WRONG_PIN)

        db_session.rollback()
        assert fresh(db_session, user).floor_pin_failed_attempts == 1

    def test_a_right_pin_verified_while_a_racing_guess_locked_the_badge_is_refused(
        self, client, db_session, monkeypatch
    ):
        user = make_badge_user(db_session, "FP-6408", pin=PIN)
        real_verify = auth_endpoints.verify_floor_pin

        def verify_while_another_station_locks_it(pin, hashed):
            # A racing wrong guess at another station reserves (count + 1) and, reaching a tier,
            # locks the badge while this bcrypt runs -- exactly what its reservation would write.
            db_session.execute(
                text(
                    "UPDATE users SET floor_pin_failed_attempts = floor_pin_failed_attempts + 1, "
                    "floor_pin_locked_until = :until WHERE id = :id"
                ),
                {"until": datetime.utcnow() + timedelta(minutes=15), "id": user.id},
            )
            return real_verify(pin, hashed)

        monkeypatch.setattr(auth_endpoints, "verify_floor_pin", verify_while_another_station_locks_it)

        response = badge_login(client, "FP-6408", pin=PIN)

        assert response.status_code == 401
        assert response.json()["detail"] == INVALID
        assert "access_token" not in response.text
        assert audit_rows(db_session, "EMPLOYEE_LOGIN_SUCCESS") == []
        assert audit_rows(db_session, "EMPLOYEE_LOGIN_FAILED")[-1].error_message == "PIN locked"


# ===========================================================================
# 4. Lock at 5, lock expiry, and the password login
# ===========================================================================
class TestLock:
    def test_five_wrong_pins_lock_the_pin_for_fifteen_minutes(self, client, db_session):
        user = make_badge_user(db_session, "FP-7101", pin=PIN)
        for attempt in range(1, FLOOR_PIN_MAX_FAILURES):
            assert badge_login(client, "FP-7101", pin=WRONG_PIN).status_code == 401
            assert fresh(db_session, user).floor_pin_failed_attempts == attempt
            assert fresh(db_session, user).floor_pin_locked_until is None

        before = datetime.utcnow()
        response = badge_login(client, "FP-7101", pin=WRONG_PIN)
        after = datetime.utcnow()

        assert response.status_code == 401 and response.json()["detail"] == INVALID, "the lock is not disclosed"
        user = fresh(db_session, user)
        assert user.floor_pin_failed_attempts == FLOOR_PIN_MAX_FAILURES, "the count is cumulative across locks"
        window = timedelta(minutes=FLOOR_PIN_LOCKOUT_MINUTES)
        assert before + window <= user.floor_pin_locked_until <= after + window
        rows = audit_rows(db_session, "EMPLOYEE_LOGIN_FAILED")
        assert [r.error_message for r in rows] == ["Wrong PIN"] * FLOOR_PIN_MAX_FAILURES
        assert [bool((r.extra_data or {}).get("floor_pin_locked")) for r in rows] == [False] * (
            FLOOR_PIN_MAX_FAILURES - 1
        ) + [True]
        assert ip_failures() == 0
        assert_no_pin_in(rows, PIN, WRONG_PIN)

    def test_while_locked_the_right_pin_is_refused_unverified_and_uncharged(self, client, db_session, monkeypatch):
        user = make_badge_user(db_session, "FP-7102", pin=PIN)
        user.floor_pin_locked_until = datetime.utcnow() + timedelta(minutes=10)
        db_session.commit()

        def must_not_verify(*_args, **_kwargs):
            raise AssertionError("a locked PIN must not be checked at all")

        monkeypatch.setattr(auth_endpoints, "verify_floor_pin", must_not_verify)

        response = badge_login(client, "FP-7102", pin=PIN)

        assert response.status_code == 401
        assert response.json()["detail"] == INVALID, "locked and wrong answer the same text"
        (row,) = audit_rows(db_session, "EMPLOYEE_LOGIN_FAILED")
        assert row.error_message == "PIN locked"
        assert row.user_id == user.id
        assert ip_failures() == 0
        assert fresh(db_session, user).floor_pin_failed_attempts == 0

    def test_an_expired_lock_lets_the_right_pin_in_and_the_next_window_starts_at_zero(self, client, db_session):
        user = make_badge_user(db_session, "FP-7103", pin=PIN)
        user.floor_pin_locked_until = datetime.utcnow() - timedelta(seconds=1)
        db_session.commit()

        wrong = badge_login(client, "FP-7103", pin=WRONG_PIN)

        assert wrong.status_code == 401
        user = fresh(db_session, user)
        assert user.floor_pin_failed_attempts == 1, "one wrong PIN after a lock runs out is not a re-lock"
        assert user.floor_pin_locked_until < datetime.utcnow()
        assert audit_rows(db_session, "EMPLOYEE_LOGIN_FAILED")[-1].error_message == "Wrong PIN"

        right = badge_login(client, "FP-7103", pin=PIN)

        assert right.status_code == 200, right.text
        user = fresh(db_session, user)
        assert user.floor_pin_failed_attempts == 0 and user.floor_pin_locked_until is None

    def test_a_pin_lock_never_locks_the_password_login(self, client, db_session):
        user = make_badge_user(db_session, "FP-7104", pin=PIN)
        for _ in range(FLOOR_PIN_MAX_FAILURES):
            badge_login(client, "FP-7104", pin=WRONG_PIN)
        assert fresh(db_session, user).floor_pin_locked_until is not None

        response = password_login(client, "fp-7104@werco.com")

        assert response.status_code == 200, response.text
        assert "amr" not in access_claims(response.json()["access_token"])
        user = fresh(db_session, user)
        assert user.locked_until is None and user.failed_login_attempts in (0, None)
        assert user.floor_pin_locked_until is not None, "the password login does not lift the PIN lock either"


# ===========================================================================
# 5. The company policy
# ===========================================================================
class TestPolicy:
    def test_policy_on_refuses_a_badge_with_no_pin(self, client, db_session):
        user = make_badge_user(db_session, "FP-8101")
        set_policy(db_session, True)

        response = badge_login(client, "FP-8101")

        assert response.status_code == 403
        assert response.json()["detail"] == NOT_SET
        assert "access_token" not in response.text
        (row,) = audit_rows(db_session, "EMPLOYEE_LOGIN_FAILED")
        assert row.error_message == "No PIN set" and row.user_id == user.id
        assert ip_failures() == 0, "a missing PIN is an admin gap, not a guess"
        assert audit_rows(db_session, "EMPLOYEE_LOGIN_SUCCESS") == []

    def test_policy_on_a_supplied_pin_does_not_get_a_no_pin_badge_in(self, client, db_session):
        make_badge_user(db_session, "FP-8102")
        set_policy(db_session, True)

        response = badge_login(client, "FP-8102", pin=PIN)

        assert response.status_code == 403 and response.json()["detail"] == NOT_SET

    def test_policy_on_a_badge_with_a_pin_is_challenged_then_signs_in(self, client, db_session):
        make_badge_user(db_session, "FP-8103", pin=PIN)
        set_policy(db_session, True)

        assert badge_login(client, "FP-8103").json()["detail"] == CHALLENGE
        response = badge_login(client, "FP-8103", pin=PIN)

        assert response.status_code == 200, response.text
        assert access_claims(response.json()["access_token"])["amr"] == ["badge", "pin"]

    def test_policy_off_again_lets_the_badge_alone_back_in(self, client, db_session):
        make_badge_user(db_session, "FP-8104")
        set_policy(db_session, True)
        assert badge_login(client, "FP-8104").status_code == 403
        set_policy(db_session, False)

        response = badge_login(client, "FP-8104")

        assert response.status_code == 200
        assert access_claims(response.json()["access_token"])["amr"] == ["badge"]

    def test_another_companys_policy_does_not_govern_this_badge(self, client, db_session):
        make_badge_user(db_session, "FP-8105")
        set_policy(db_session, True, company_id=2)

        response = badge_login(client, "FP-8105")

        assert response.status_code == 200, response.text

    def test_policy_on_an_inactive_badge_is_refused_as_inactive_not_as_pinless(self, client, db_session):
        make_badge_user(db_session, "FP-8106", is_active=False)
        set_policy(db_session, True)

        response = badge_login(client, "FP-8106")

        assert response.status_code == 403
        assert response.json()["detail"] == "User account is disabled"


# ===========================================================================
# 6. The per-IP throttle: charged by wrong PINs only
# ===========================================================================
class TestIpThrottle:
    """The PIN step never charges ``employee_login_throttle``: the per-user lock bounds PIN
    guessing, and crew stations share one egress IP, so one person's typos must never lock badge
    sign-in for the whole floor. Unknown badges still charge it, exactly as before PR D."""

    def test_no_outcome_of_the_pin_step_charges_the_throttle(self, client, db_session):
        make_badge_user(db_session, "FP-9101", pin=PIN)
        make_badge_user(db_session, "FP-9102")

        badge_login(client, "FP-9101")  # challenge
        badge_login(client, "FP-9101", pin=WRONG_PIN)  # wrong PIN
        badge_login(client, "FP-9101", pin=PIN)  # right PIN
        badge_login(client, "FP-9102", pin=WRONG_PIN)  # no PIN set, policy off: the pin is ignored
        set_policy(db_session, True)
        badge_login(client, "FP-9102")  # no PIN set, policy on: 403

        assert ip_failures() == 0

        badge_login(client, "FP-9199")  # an unknown badge still counts
        assert ip_failures() == 1

    def test_a_locked_out_badge_does_not_lock_the_shared_ip(self, client, db_session):
        """Nine wrong / locked PINs from one kiosk IP (more than EMPLOYEE_LOGIN_MAX_FAILURES),
        then a colleague's badge at the same station still signs in."""
        make_badge_user(db_session, "FP-9201", pin=PIN)
        make_badge_user(db_session, "FP-9202", pin=PIN)
        for _ in range(EMPLOYEE_LOGIN_MAX_FAILURES + 1):
            assert badge_login(client, "FP-9201", pin=WRONG_PIN).status_code == 401

        response = badge_login(client, "FP-9202", pin=PIN)

        assert response.status_code == 200, response.text
        assert ip_failures() == 0

    def test_eight_wrong_pins_on_two_badges_leave_an_unknown_badge_a_401_not_a_429(self, client, db_session):
        """EMPLOYEE_LOGIN_MAX_FAILURES wrong PINs, split across two badges so neither locks, then an
        unknown badge from the same IP: had the wrong PINs been charged, this would be the 429."""
        make_badge_user(db_session, "FP-9401", pin=PIN)
        make_badge_user(db_session, "FP-9402", pin=PIN)
        half = EMPLOYEE_LOGIN_MAX_FAILURES // 2
        for employee_id in ["FP-9401"] * half + ["FP-9402"] * (EMPLOYEE_LOGIN_MAX_FAILURES - half):
            assert badge_login(client, employee_id, pin=WRONG_PIN).status_code == 401

        response = badge_login(client, "FP-9499")

        assert response.status_code == 401, response.text
        assert response.json()["detail"] == "Invalid employee ID"
        assert ip_failures() == 1

    def test_unknown_badges_still_engage_the_ip_throttle(self, client, db_session):
        make_badge_user(db_session, "FP-9301", pin=PIN)
        for n in range(EMPLOYEE_LOGIN_MAX_FAILURES):
            assert badge_login(client, f"FP-93{50 + n}").status_code == 401

        response = badge_login(client, "FP-9301", pin=PIN)

        assert response.status_code == 429, response.text


# ===========================================================================
# 7. Refresh and switch-company carry amr -- and refuse a stale badge-only session
# ===========================================================================
class TestRefresh:
    def test_a_badge_pin_session_refreshes_keeping_exactly_badge_and_pin(self, client, db_session):
        make_badge_user(db_session, "FP-1101", pin=PIN)
        login = badge_login(client, "FP-1101", pin=PIN)

        first = client.post(REFRESH, json={"refresh_token": login.json()["refresh_token"]}, headers=XHR)
        second = client.post(REFRESH, json={"refresh_token": first.json()["refresh_token"]}, headers=XHR)

        for response in (first, second):
            assert response.status_code == 200, response.text
            assert access_claims(response.json()["access_token"])["amr"] == ["badge", "pin"]
            assert refresh_claims(response.json()["refresh_token"])["amr"] == ["badge", "pin"]
        rows = audit_rows(db_session, "TOKEN_REFRESHED")
        assert [r.extra_data["auth_methods"] for r in rows] == [["badge", "pin"]] * 2

    def test_a_badge_only_session_refreshes_as_badge_only_while_nothing_changed(self, client, db_session):
        make_badge_user(db_session, "FP-1102")
        login = badge_login(client, "FP-1102")

        response = client.post(REFRESH, json={"refresh_token": login.json()["refresh_token"]}, headers=XHR)

        assert response.status_code == 200, response.text
        assert access_claims(response.json()["access_token"])["amr"] == ["badge"], "a refresh never gains pin"
        assert refresh_claims(response.json()["refresh_token"])["amr"] == ["badge"]

    def test_a_password_session_refreshes_claim_free(self, client, db_session):
        make_badge_user(db_session, "FP-1103", pin=PIN)
        set_policy(db_session, True)
        login = password_login(client, "fp-1103@werco.com")

        response = client.post(REFRESH, json={"refresh_token": login.json()["refresh_token"]}, headers=XHR)

        assert response.status_code == 200, response.text
        assert "amr" not in access_claims(response.json()["access_token"])
        assert "amr" not in refresh_claims(response.json()["refresh_token"])
        (row,) = audit_rows(db_session, "TOKEN_REFRESHED")
        assert "auth_methods" not in (row.extra_data or {})

    @pytest.mark.parametrize("change", ["pin_set", "policy_on"])
    def test_a_badge_only_session_is_refused_once_a_pin_applies(self, client, db_session, change):
        user = make_badge_user(db_session, "FP-1104")
        login = badge_login(client, "FP-1104")
        assert login.status_code == 200
        if change == "pin_set":
            user.floor_pin_hash = hash_floor_pin(PIN)
            db_session.commit()
        else:
            set_policy(db_session, True)

        response = client.post(REFRESH, json={"refresh_token": login.json()["refresh_token"]}, headers=XHR)

        assert response.status_code == 401
        assert response.json()["detail"] == REAUTH
        assert "access_token" not in response.text and "refresh_token" not in response.text
        assert audit_rows(db_session, "TOKEN_REFRESHED") == []
        (row,) = audit_rows(db_session, "TOKEN_REFRESH_FAILED")
        assert row.error_message == "PIN now required"
        assert row.user_id == user.id
        assert row.extra_data["auth_methods"] == ["badge"]

    def test_a_badge_pin_session_with_a_locked_pin_still_refreshes(self, client, db_session):
        user = make_badge_user(db_session, "FP-1105", pin=PIN)
        set_policy(db_session, True)
        login = badge_login(client, "FP-1105", pin=PIN)
        user = fresh(db_session, user)
        user.floor_pin_locked_until = datetime.utcnow() + timedelta(minutes=10)
        db_session.commit()

        response = client.post(REFRESH, json={"refresh_token": login.json()["refresh_token"]}, headers=XHR)

        assert response.status_code == 200, response.text
        assert access_claims(response.json()["access_token"])["amr"] == ["badge", "pin"]

    def test_a_refresh_token_without_amr_from_before_the_feature_still_refreshes(self, client, db_session):
        """Tokens minted before PR D carry no claim; they are password-shaped and unaffected."""
        user = make_badge_user(db_session, "FP-1106", pin=PIN)
        set_policy(db_session, True)
        legacy, _sid, _exp = create_refresh_token(subject=user.id, company_id=user.company_id)

        response = client.post(REFRESH, json={"refresh_token": legacy}, headers=XHR)

        assert response.status_code == 200, response.text
        assert "amr" not in access_claims(response.json()["access_token"])


class TestSwitchCompany:
    @staticmethod
    def _platform_badge_session(client, db_session, employee_id, *, pin=None):
        ensure_company(db_session, 2)
        user = make_badge_user(db_session, employee_id, pin=pin, role=UserRole.PLATFORM_ADMIN)
        body = {"pin": pin} if pin else {}
        login = badge_login(client, employee_id, **body)
        assert login.status_code == 200, login.text
        return user, {**XHR, "Authorization": f"Bearer {login.json()['access_token']}"}

    def test_a_switch_keeps_badge_and_pin_and_records_it(self, client, db_session):
        user, headers = self._platform_badge_session(client, db_session, "FP-1201", pin=PIN)

        response = client.post("/api/v1/auth/switch-company/2", headers=headers)

        assert response.status_code == 200, response.text
        assert access_claims(response.json()["access_token"])["amr"] == ["badge", "pin"]
        assert refresh_claims(response.json()["refresh_token"])["amr"] == ["badge", "pin"]
        row = audit_rows(db_session, "COMPANY_SWITCH")[-1]
        assert row.user_id == user.id and row.extra_data["auth_methods"] == ["badge", "pin"]

    @pytest.mark.parametrize("change", ["pin_set", "policy_on"])
    def test_a_badge_only_session_cannot_switch_once_a_pin_applies(self, client, db_session, change):
        user, headers = self._platform_badge_session(client, db_session, "FP-1202")
        ok = client.post("/api/v1/auth/switch-company/2", headers=headers)
        assert ok.status_code == 200 and access_claims(ok.json()["access_token"])["amr"] == ["badge"]
        if change == "pin_set":
            user = fresh(db_session, user)
            user.floor_pin_hash = hash_floor_pin(PIN)
            db_session.commit()
        else:
            set_policy(db_session, True)
        switches_before = len(audit_rows(db_session, "COMPANY_SWITCH"))

        response = client.post("/api/v1/auth/switch-company/2", headers=headers)

        assert response.status_code == 401
        assert response.json()["detail"] == REAUTH
        assert "access_token" not in response.text
        assert len(audit_rows(db_session, "COMPANY_SWITCH")) == switches_before
        row = audit_rows(db_session, "COMPANY_SWITCH_FAILED")[-1]
        assert row.error_message == "PIN now required" and row.extra_data["auth_methods"] == ["badge"]

    def test_the_target_companys_policy_does_not_refuse_the_switch(self, client, db_session):
        """The policy that governs a badge sign-on is the user's OWN company's."""
        _user, headers = self._platform_badge_session(client, db_session, "FP-1203")
        set_policy(db_session, True, company_id=2)

        response = client.post("/api/v1/auth/switch-company/2", headers=headers)

        assert response.status_code == 200, response.text
        assert access_claims(response.json()["access_token"])["amr"] == ["badge"]

    def test_a_badge_pin_session_with_a_locked_pin_still_switches(self, client, db_session):
        user, headers = self._platform_badge_session(client, db_session, "FP-1204", pin=PIN)
        user = fresh(db_session, user)
        user.floor_pin_locked_until = datetime.utcnow() + timedelta(minutes=10)
        db_session.commit()

        response = client.post("/api/v1/auth/switch-company/2", headers=headers)

        assert response.status_code == 200, response.text
        assert access_claims(response.json()["access_token"])["amr"] == ["badge", "pin"]


# ===========================================================================
# 8. Every later write records how the person signed in
# ===========================================================================
class TestAuthMethodsFold:
    @staticmethod
    def _phone_row(db_session, user):
        rows = [
            r
            for r in audit_rows(db_session, "UPDATE")
            if r.resource_type == "user" and r.resource_id == user.id and (r.new_values or {}).get("phone")
        ]
        return rows[-1]

    def test_badge_pin_badge_only_password_and_api_token_writes_on_one_identity_map(
        self, client, db_session, admin_user, admin_headers
    ):
        """All four sessions of ONE person write through the same ``User`` object in the test's
        shared session: each row says how its own request signed in, never the last one's."""
        admin_user.floor_pin_hash = hash_floor_pin(PIN)
        db_session.commit()
        pin_login = badge_login(client, admin_user.employee_id, pin=PIN)
        assert pin_login.status_code == 200, pin_login.text
        pin_headers = {**XHR, "Authorization": f"Bearer {pin_login.json()['access_token']}"}
        _token_id, api_token = issue_token(client, admin_headers, admin_user)
        token_headers = {**XHR, "Authorization": f"Bearer {api_token}"}

        sequence = [
            (pin_headers, "+13125550101", ["badge", "pin"], False),
            (admin_headers, "+13125550102", None, False),
            (token_headers, "+13125550103", None, True),
            (pin_headers, "+13125550104", ["badge", "pin"], False),
        ]
        for headers, phone, expected, is_token in sequence:
            response = client.put(PHONE, json={"phone": phone}, headers=headers)
            assert response.status_code == 200, response.text
            row = self._phone_row(db_session, admin_user)
            assert row.new_values["phone"] == phone
            if expected is None:
                assert "auth_methods" not in (row.extra_data or {}), (phone, row.extra_data)
            else:
                assert row.extra_data["auth_methods"] == expected
            assert ("credential" in (row.extra_data or {})) is is_token
            assert row.extra_data["source"] == "self_service", "the route's own extra_data survives the fold"

    def test_a_badge_only_session_write_records_badge(self, client, db_session):
        user = make_badge_user(db_session, "FP-1301")
        login = badge_login(client, "FP-1301")
        headers = {**XHR, "Authorization": f"Bearer {login.json()['access_token']}"}

        response = client.put(PHONE, json={"phone": "+13125550111"}, headers=headers)

        assert response.status_code == 200, response.text
        assert self._phone_row(db_session, user).extra_data["auth_methods"] == ["badge"]

    def test_the_service_fold_wins_its_key_and_ignores_anything_but_a_list_of_names(self, db_session, admin_user):
        audit = AuditService(db_session, admin_user)
        assert audit._with_credential({"a": 1}) == {"a": 1}

        admin_user._auth_methods = ["badge", "pin"]
        folded = AuditService(db_session, admin_user)._with_credential({"auth_methods": ["forged"], "a": 1})
        assert folded == {"auth_methods": ["badge", "pin"], "a": 1}

        for junk in ([], "badge", ["badge", 5], object()):
            admin_user._auth_methods = junk
            assert AuditService(db_session, admin_user)._with_credential(None) is None, junk
        admin_user._auth_methods = None


class TestAmrClaimShape:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (["badge"], ["badge"]),
            (["badge", "pin"], ["badge", "pin"]),
            (("badge", "pin"), ["badge", "pin"]),
            (None, None),
            ([], None),
            ("badge", None),
            (["badge", 5], None),
            (["badge", ""], None),
            (["x" * 33], None),
            (["m"] * 9, None),
        ],
    )
    def test_a_malformed_claim_reads_as_no_claim_never_a_partial_one(self, value, expected):
        assert normalize_auth_methods(value) == expected


# ===========================================================================
# 9. The crew-station badge mint
# ===========================================================================
class TestCrewStation:
    @staticmethod
    def _station(db_session):
        station = make_kiosk_station(db_session, company_id=1)
        return station, bearer(kiosk_token_for(station))

    @staticmethod
    def _mint(client, headers, employee_id, **body):
        return client.post(BADGE_TOKEN_URL, json={"employee_id": employee_id, **body}, headers=headers)

    def test_no_pin_mints_a_badge_only_kiosk_token(self, client, db_session):
        _station, headers = self._station(db_session)
        user = make_badge_user(db_session, "FP-2101")

        response = self._mint(client, headers, "FP-2101")

        assert response.status_code == 200, response.text
        claims = access_claims(response.json()["access_token"])
        assert claims["amr"] == ["badge"] and claims["scope"] == "kiosk"
        assert "refresh_token" not in response.json()
        issued = audit_rows(db_session, "KIOSK_BADGE_TOKEN_ISSUED")[-1]
        assert issued.user_id == user.id and issued.extra_data["auth_methods"] == ["badge"]

    def test_challenge_then_right_pin(self, client, db_session):
        _station, headers = self._station(db_session)
        user = make_badge_user(db_session, "FP-2102", pin=PIN)
        rows_before = len(audit_rows(db_session))

        challenge = self._mint(client, headers, "FP-2102")

        assert challenge.status_code == 401 and challenge.json()["detail"] == CHALLENGE
        assert len(audit_rows(db_session)) == rows_before

        response = self._mint(client, headers, "FP-2102", pin=PIN)

        assert response.status_code == 200, response.text
        claims = access_claims(response.json()["access_token"])
        assert claims["amr"] == ["badge", "pin"] and claims["scope"] == "kiosk"
        issued = audit_rows(db_session, "KIOSK_BADGE_TOKEN_ISSUED")[-1]
        assert issued.user_id == user.id and issued.extra_data["auth_methods"] == ["badge", "pin"]

    def test_failure_rows_are_station_keyed_and_name_the_badge_user(self, client, db_session):
        station, headers = self._station(db_session)
        user = make_badge_user(db_session, "FP-2103", pin=PIN)
        for _ in range(FLOOR_PIN_MAX_FAILURES):
            response = self._mint(client, headers, "FP-2103", pin=WRONG_PIN)
            assert response.status_code == 401 and response.json()["detail"] == INVALID
        locked = self._mint(client, headers, "FP-2103", pin=PIN)
        assert locked.status_code == 401 and locked.json()["detail"] == INVALID

        rows = audit_rows(db_session, "KIOSK_BADGE_TOKEN_FAILED")
        assert [r.error_message for r in rows] == ["Wrong PIN"] * FLOOR_PIN_MAX_FAILURES + ["PIN locked"]
        for row in rows:
            assert row.resource_type == "kiosk_station" and row.resource_id == station.id
            assert row.user_id is None, "the actor of a failed scan is the station"
            assert row.company_id == station.company_id
            assert row.extra_data["badge_user_id"] == user.id
        assert rows[FLOOR_PIN_MAX_FAILURES - 1].extra_data.get("floor_pin_locked") is True
        assert all(not r.extra_data.get("floor_pin_locked") for r in rows[: FLOOR_PIN_MAX_FAILURES - 1])
        assert_no_pin_in(rows, PIN, WRONG_PIN)
        assert ip_failures() == 0, "the station route has no per-IP throttle to charge"

    def test_policy_on_refuses_a_pinless_badge_with_a_station_keyed_row(self, client, db_session):
        station, headers = self._station(db_session)
        user = make_badge_user(db_session, "FP-2104")
        set_policy(db_session, True)

        response = self._mint(client, headers, "FP-2104")

        assert response.status_code == 403 and response.json()["detail"] == NOT_SET
        row = audit_rows(db_session, "KIOSK_BADGE_TOKEN_FAILED")[-1]
        assert row.error_message == "No PIN set"
        assert row.resource_id == station.id and row.extra_data["badge_user_id"] == user.id

    def test_a_foreign_tenant_badge_never_reaches_the_pin_step(self, client, db_session):
        _station, headers = self._station(db_session)
        foreign = make_badge_user(db_session, "FP-2105", pin=PIN, company_id=2)

        challenge = self._mint(client, headers, "FP-2105")
        guess = self._mint(client, headers, "FP-2105", pin=WRONG_PIN)

        for response in (challenge, guess):
            assert response.status_code == 401
            assert response.json()["detail"] == "Invalid badge", "no challenge: the badge is not in this company"
        assert fresh(db_session, foreign).floor_pin_failed_attempts == 0
        rows = audit_rows(db_session, "KIOSK_BADGE_TOKEN_FAILED")
        assert [r.error_message for r in rows] == ["Badge not recognized"] * 2
        assert all("badge_user_id" not in (r.extra_data or {}) for r in rows)

    def test_the_lock_is_one_lock_across_both_doors(self, client, db_session):
        """3 wrong PINs at the crew station + 2 at employee-login = 5 on the ONE per-user counter."""
        _station, headers = self._station(db_session)
        user = make_badge_user(db_session, "FP-2106", pin=PIN)
        for _ in range(3):
            assert self._mint(client, headers, "FP-2106", pin=WRONG_PIN).status_code == 401
        for _ in range(FLOOR_PIN_MAX_FAILURES - 3):
            assert badge_login(client, "FP-2106", pin=WRONG_PIN).status_code == 401

        assert fresh(db_session, user).floor_pin_locked_until is not None
        refused = self._mint(client, headers, "FP-2106", pin=PIN)
        assert refused.status_code == 401
        assert audit_rows(db_session, "KIOSK_BADGE_TOKEN_FAILED")[-1].error_message == "PIN locked"
        assert ip_failures() == 0, "neither door charges the IP for a PIN"

    def test_a_write_under_the_kiosk_token_records_badge_and_pin(self, client, db_session):
        _station, headers = self._station(db_session)
        user = make_badge_user(db_session, "FP-2107", pin=PIN)
        minted = self._mint(client, headers, "FP-2107", pin=PIN)

        response = client.post(
            EMPLOYEE_LOGOUT, json={"employee_id": "FP-2107"}, headers=bearer(minted.json()["access_token"])
        )

        assert response.status_code == 200, response.text
        row = audit_rows(db_session, "EMPLOYEE_LOGOUT")[-1]
        assert row.user_id == user.id and row.extra_data["auth_methods"] == ["badge", "pin"]


# ===========================================================================
# 10. A 422 on the sign-on routes never echoes a PIN
# ===========================================================================
class TestValidationErrorsNeverEchoThePin:
    @pytest.mark.parametrize("url", [EMPLOYEE_LOGIN, BADGE_TOKEN_URL])
    def test_an_invalid_employee_id_beside_a_pin_echoes_only_the_employee_id(self, client, db_session, url):
        station = make_kiosk_station(db_session, company_id=1)
        headers = bearer(kiosk_token_for(station)) if url == BADGE_TOKEN_URL else XHR

        response = client.post(url, json={"employee_id": "", "pin": PIN}, headers=headers)

        assert response.status_code == 422
        assert PIN not in response.text

    @pytest.mark.parametrize("url", [EMPLOYEE_LOGIN, BADGE_TOKEN_URL])
    def test_a_pin_without_an_employee_id_is_not_echoed(self, client, db_session, url):
        """Regression: a missing ``employee_id`` is a pydantic error whose ``input`` is the WHOLE
        body, which the 422 handler used to echo -- PIN included."""
        station = make_kiosk_station(db_session, company_id=1)
        headers = bearer(kiosk_token_for(station)) if url == BADGE_TOKEN_URL else XHR

        response = client.post(url, json={"pin": PIN}, headers=headers)

        assert response.status_code == 422
        assert PIN not in response.text
        (error,) = response.json()["detail"]
        assert error["loc"][-1] == "employee_id"
        assert error["input"] == {"pin": "[redacted]"}

    @pytest.mark.parametrize(
        "body",
        [
            {"PIN": PIN},
            {"floor_pin": PIN, "note": "visible"},
            {"employee_id": None, "pin": PIN},
            {"wrapper": {"deeper": [{"pin": PIN}]}},
        ],
        ids=["upper-case-key", "floor_pin-key", "null-employee-id", "nested"],
    )
    def test_a_pin_key_anywhere_in_the_echoed_body_is_redacted(self, client, db_session, body):
        response = client.post(EMPLOYEE_LOGIN, json=body, headers=XHR)

        assert response.status_code == 422
        assert PIN not in response.text

    def test_the_redaction_is_targeted_not_a_blanket_wipe(self, client, db_session):
        response = client.post(EMPLOYEE_LOGIN, json={"pin": PIN, "note": "visible-context"}, headers=XHR)

        assert response.status_code == 422
        assert "visible-context" in response.text, "only PIN keys are redacted; the rest of the 422 is unchanged"
        assert "[redacted]" in response.text
        assert PIN not in response.text

    def test_a_body_that_is_a_list_of_pins_is_redacted_too(self, client, db_session):
        response = client.post(EMPLOYEE_LOGIN, json=[{"pin": PIN}, {"pin": WRONG_PIN}], headers=XHR)

        assert response.status_code == 422
        assert PIN not in response.text and WRONG_PIN not in response.text
