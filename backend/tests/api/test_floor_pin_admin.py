"""Floor PIN administration (PR D, AS9100D 8.5.2): the PIN rules, the per-user routes and the
company policy switch.

* ``app.core.floor_pin.validate_floor_pin`` -- the ONE rule set: digits only, 4 to 6 long, not
  one digit repeated, not a straight run up or down, and not the user's own badge digits (the
  full digit string, the last-4 normalization, and the trailing 5 and 6 digits). Every refusal
  names its rule and never the PIN.
* ``PUT`` / ``DELETE /users/{user_id}/floor-pin`` and ``POST .../floor-pin/unlock`` -- Admin, or a
  Manager for Operator / Supervisor / Shipping / Viewer accounts only (never an Admin's, a
  Manager's or a Quality user's PIN); a platform principal's PIN needs a platform principal; the
  target must be in the active company (404 otherwise); ANY API token is refused 403 before
  anything is read; the audit row is REQUIRED (503, nothing saved) and never carries the PIN or
  its hash; a DELETE (or an unlock) with nothing to do is a 200 with no row.
* ``PUT /companies/me/floor-pin-policy`` -- Admin only, tokens refused, two required rows,
  ``users_without_pin`` counting the active company's active BADGE users with no PIN (an
  employee ID, not a platform principal).
* None of the four is an MCP tool.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.endpoints.companies import FLOOR_PIN_POLICY_AUDIT_UNAVAILABLE_DETAIL
from app.api.endpoints.users import _MANAGER_FLOOR_PIN_SCOPE_DETAIL
from app.core.config import settings
from app.core.credentials import API_TOKEN_FLOOR_PIN_ADMIN, READ_ONLY_ROUTE_WRITES_DETAIL
from app.core.floor_pin import (
    PIN_RULE_BADGE,
    PIN_RULE_DIGITS,
    PIN_RULE_REPEATED,
    PIN_RULE_SEQUENCE,
    FloorPinRuleError,
    hash_floor_pin,
    validate_floor_pin,
    verify_floor_pin,
)
from app.core.security import create_access_token
from app.main import app
from app.mcp.catalog import (
    EXCLUDED_OPERATIONS,
    FLOOR_PIN_OPERATIONS,
    build_catalog,
    iter_secured_operations,
)
from app.mcp.convenience import CONVENIENCE_TOOL_NAMES, SHADOWED_OPERATIONS
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.user import User, UserRole
from app.services.audit_service import AuditService, AuditWriteError
from tests.api.export_control_helpers import issue_token
from tests.api.kiosk_test_helpers import ensure_company
from tests.conftest import TEST_PASSWORD_HASH

pytestmark = pytest.mark.api

API = settings.API_V1_PREFIX.rstrip("/")
POLICY = f"{API}/companies/me/floor-pin-policy"
EMPLOYEE_LOGIN = f"{API}/auth/employee-login"
XHR = {"X-Requested-With": "XMLHttpRequest"}

# Valid under every rule for every badge used below.
PIN = "830416"
OTHER_PIN = "529038"
MANAGER_SCOPE_DETAIL = _MANAGER_FLOOR_PIN_SCOPE_DETAIL
MANAGER_TARGETS = [UserRole.OPERATOR, UserRole.SUPERVISOR, UserRole.SHIPPING, UserRole.VIEWER]
ADMIN_ONLY_TARGETS = [UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY]
PLATFORM_DETAIL = "Only platform administrators can manage platform accounts"
AUDIT_UNAVAILABLE = "Unable to save audit record"


def pin_url(user_id: int) -> str:
    return f"{API}/users/{user_id}/floor-pin"


_seq = {"n": 0}


def make_user(
    db: Session,
    *,
    role: UserRole = UserRole.OPERATOR,
    company_id: int = 1,
    employee_id: Optional[str] = None,
    pin: Optional[str] = None,
    is_active: bool = True,
    superuser: bool = False,
) -> User:
    ensure_company(db, company_id)
    _seq["n"] += 1
    n = _seq["n"]
    user = User(
        email=f"floorpin{n}-{company_id}@werco.com",
        employee_id=employee_id if employee_id is not None else f"FPA-{7000 + n}",
        first_name="Pin",
        # Names are letters only (UserResponse's pattern): spell the counter in letters.
        last_name="Person" + "".join(chr(ord("a") + int(digit)) for digit in str(n)),
        hashed_password=TEST_PASSWORD_HASH,
        role=role,
        is_active=is_active,
        is_superuser=superuser,
        company_id=company_id,
    )
    if pin is not None:
        user.floor_pin_hash = hash_floor_pin(pin)
        user.floor_pin_set_at = datetime.utcnow() - timedelta(days=3)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def headers_for(user: User) -> Dict[str, str]:
    token = create_access_token(subject=user.id, company_id=user.company_id)
    return {**XHR, "Authorization": f"Bearer {token}"}


def token_headers(token: str) -> Dict[str, str]:
    return {**XHR, "Authorization": f"Bearer {token}"}


def pin_state(db: Session, user: User) -> tuple:
    db.expire_all()
    row = db.get(User, user.id)
    return (row.floor_pin_hash, row.floor_pin_set_at, row.floor_pin_failed_attempts, row.floor_pin_locked_until)


def audit_rows(db: Session, action: Optional[str] = None) -> List[AuditLog]:
    db.expire_all()
    query = db.query(AuditLog)
    if action is not None:
        query = query.filter(AuditLog.action == action)
    return query.order_by(AuditLog.id).all()


def row_text(row: AuditLog) -> str:
    return json.dumps(
        [row.extra_data, row.old_values, row.new_values, row.description, row.error_message, row.resource_identifier],
        default=str,
    )


# ===========================================================================
# 1. The PIN rules (one validator)
# ===========================================================================
class TestPinRules:
    @pytest.mark.parametrize("pin", ["2580", "13579", "830416", "0912", "8901", "1235", "2468", "9753"])
    def test_acceptable_pins(self, pin):
        assert validate_floor_pin(pin, employee_id=None) == pin

    @pytest.mark.parametrize(
        "pin",
        ["", "258", "2580135", "25a0", "25 80", " 2580", "2580 ", "25-80", "+2580", "２５８０", "٢٥٨٠", "2.58"],
        ids=[
            "empty",
            "three",
            "seven",
            "letter",
            "inner-space",
            "lead-space",
            "trail-space",
            "dash",
            "plus",
            "fullwidth",
            "arabic-indic",
            "dot",
        ],
    )
    def test_not_four_to_six_ascii_digits(self, pin):
        with pytest.raises(FloorPinRuleError) as caught:
            validate_floor_pin(pin, employee_id=None)
        assert str(caught.value) == PIN_RULE_DIGITS

    @pytest.mark.parametrize("pin", [2580, 2580.0, None, ["2", "5", "8", "0"], b"2580"])
    def test_a_non_string_is_refused_by_the_digits_rule(self, pin):
        with pytest.raises(FloorPinRuleError) as caught:
            validate_floor_pin(pin, employee_id=None)
        assert str(caught.value) == PIN_RULE_DIGITS

    @pytest.mark.parametrize("pin", ["0000", "1111", "77777", "999999", "000000"])
    def test_one_digit_repeated(self, pin):
        with pytest.raises(FloorPinRuleError) as caught:
            validate_floor_pin(pin, employee_id=None)
        assert str(caught.value) == PIN_RULE_REPEATED

    @pytest.mark.parametrize("pin", ["1234", "0123", "4321", "123456", "654321", "3456", "98765", "6789", "9876"])
    def test_a_straight_run_up_or_down(self, pin):
        with pytest.raises(FloorPinRuleError) as caught:
            validate_floor_pin(pin, employee_id=None)
        assert str(caught.value) == PIN_RULE_SEQUENCE

    @pytest.mark.parametrize(
        "employee_id,pin",
        [
            ("W-9073518", "3518"),  # last 4 (the badge_last_four normalization)
            ("W-9073518", "73518"),  # trailing 5
            ("W-9073518", "073518"),  # trailing 6, leading zero kept
            ("E80264", "80264"),  # the full digit string (also its trailing 5)
            ("EMP-602917", "602917"),  # full digits = trailing 6
            ("4729", "4729"),  # a bare 4-digit badge
            ("E-907", "0907"),  # a 3-digit badge, read zero-padded like the badge resolver reads it
            ("B-12-3-58", "2358"),  # digits gathered across separators
        ],
    )
    def test_the_users_own_badge_digits(self, employee_id, pin):
        with pytest.raises(FloorPinRuleError) as caught:
            validate_floor_pin(pin, employee_id=employee_id)
        assert str(caught.value) == PIN_RULE_BADGE

    @pytest.mark.parametrize(
        "employee_id,pin",
        [
            ("W-9073518", "9073"),  # LEADING digits are not the badge's last digits
            ("W-9073518", "90735"),
            ("E80264", "8026"),
            ("E-907", "9070"),
            (None, "4729"),
            ("", "4729"),
            ("ADMIN", "4729"),  # a badge with no digits constrains nothing
        ],
    )
    def test_other_digits_of_or_around_the_badge_are_allowed(self, employee_id, pin):
        assert validate_floor_pin(pin, employee_id=employee_id) == pin

    @pytest.mark.parametrize(
        "pin,employee_id",
        [("25a0", None), ("0000", None), ("1234", None), ("3518", "W-9073518"), ("258", None)],
    )
    def test_no_refusal_text_contains_the_pin(self, pin, employee_id):
        with pytest.raises(FloorPinRuleError) as caught:
            validate_floor_pin(pin, employee_id=employee_id)
        assert pin not in str(caught.value)

    @pytest.mark.parametrize("pin", ["2580\n", "13579\n", "\n2580", "2580\r\n"])
    def test_a_trailing_or_leading_newline_is_refused_by_the_digits_rule(self, pin):
        """Regression: ``re.match(r"^[0-9]+$")`` let ``"2580\\n"`` through (``$`` matches before a
        trailing newline) and the run check then crashed on ``int("\\n")`` -- a 500."""
        with pytest.raises(FloorPinRuleError) as caught:
            validate_floor_pin(pin, employee_id=None)
        assert str(caught.value) == PIN_RULE_DIGITS

    def test_verify_never_runs_the_kdf_on_a_value_that_cannot_be_a_pin(self):
        hashed = hash_floor_pin(PIN)
        assert verify_floor_pin(PIN, hashed) is True
        for candidate in (OTHER_PIN, "", None, "83041", "8304160", "83O416", "x" * 200, 830416):
            assert verify_floor_pin(candidate, hashed) is False, candidate
        assert verify_floor_pin(PIN, None) is False
        assert verify_floor_pin(PIN, "not-a-bcrypt-hash") is False

    def test_the_stored_value_is_a_bcrypt_hash_never_the_pin(self):
        hashed = hash_floor_pin(PIN)
        assert hashed.startswith("$2") and PIN not in hashed


# ===========================================================================
# 2. Set / reset / clear
# ===========================================================================
class TestSetResetClear:
    def test_set_stores_a_hash_records_the_set_and_never_returns_the_pin(self, client, db_session, admin_headers):
        target = make_user(db_session)

        response = client.put(pin_url(target.id), json={"pin": PIN}, headers=admin_headers)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["has_floor_pin"] is True and body["floor_pin_set_at"]
        assert body["floor_pin_locked_until"] is None
        assert "floor_pin_hash" not in body
        stored_hash, set_at, attempts, locked_until = pin_state(db_session, target)
        assert stored_hash and verify_floor_pin(PIN, stored_hash)
        assert PIN not in response.text and stored_hash not in response.text
        assert set_at is not None and attempts == 0 and locked_until is None
        (row,) = audit_rows(db_session, "FLOOR_PIN_SET")
        assert row.resource_type == "user" and row.resource_id == target.id
        assert row.old_values is None and row.new_values is None
        assert row.extra_data == {"source": "admin_set", "replaced_existing_pin": False, "cleared_pin_lockout": False}
        assert PIN not in row_text(row) and stored_hash not in row_text(row)

    def test_the_new_pin_works_at_sign_on(self, client, db_session, admin_headers):
        target = make_user(db_session)
        assert client.put(pin_url(target.id), json={"pin": PIN}, headers=admin_headers).status_code == 200

        challenge = client.post(EMPLOYEE_LOGIN, json={"employee_id": target.employee_id}, headers=XHR)
        signed_in = client.post(EMPLOYEE_LOGIN, json={"employee_id": target.employee_id, "pin": PIN}, headers=XHR)

        assert challenge.status_code == 401 and challenge.json()["detail"]["code"] == "floor_pin_required"
        assert signed_in.status_code == 200, signed_in.text

    def test_reset_replaces_the_pin_and_lifts_a_lock(self, client, db_session, admin_headers):
        target = make_user(db_session, pin=PIN)
        target.floor_pin_failed_attempts = 3
        target.floor_pin_locked_until = datetime.utcnow() + timedelta(minutes=10)
        db_session.commit()

        response = client.put(pin_url(target.id), json={"pin": OTHER_PIN}, headers=admin_headers)

        assert response.status_code == 200, response.text
        stored_hash, _set_at, attempts, locked_until = pin_state(db_session, target)
        assert verify_floor_pin(OTHER_PIN, stored_hash) and not verify_floor_pin(PIN, stored_hash)
        assert attempts == 0 and locked_until is None
        assert response.json()["floor_pin_locked_until"] is None
        (row,) = audit_rows(db_session, "FLOOR_PIN_SET")
        assert row.extra_data["replaced_existing_pin"] is True
        assert row.extra_data["cleared_pin_lockout"] is True
        assert row.description.startswith("Reset floor PIN")
        assert OTHER_PIN not in row_text(row) and PIN not in row_text(row)

        old = client.post(EMPLOYEE_LOGIN, json={"employee_id": target.employee_id, "pin": PIN}, headers=XHR)
        new = client.post(EMPLOYEE_LOGIN, json={"employee_id": target.employee_id, "pin": OTHER_PIN}, headers=XHR)
        assert old.status_code == 401 and new.status_code == 200

    def test_clear_removes_the_pin_and_the_lock(self, client, db_session, admin_headers):
        target = make_user(db_session, pin=PIN)
        target.floor_pin_locked_until = datetime.utcnow() + timedelta(minutes=10)
        db_session.commit()

        response = client.delete(pin_url(target.id), headers=admin_headers)

        assert response.status_code == 200, response.text
        assert response.json()["has_floor_pin"] is False
        assert response.json()["floor_pin_set_at"] is None
        assert pin_state(db_session, target) == (None, None, 0, None)
        (row,) = audit_rows(db_session, "FLOOR_PIN_CLEARED")
        assert row.resource_id == target.id
        assert row.extra_data == {"source": "admin_clear", "had_pin": True, "cleared_pin_lockout": True}

        signed_in = client.post(EMPLOYEE_LOGIN, json={"employee_id": target.employee_id}, headers=XHR)
        assert signed_in.status_code == 200, "a cleared PIN is not asked for (policy off)"

    def test_clear_with_nothing_to_clear_is_a_200_with_no_row(self, client, db_session, admin_headers):
        target = make_user(db_session)

        first = client.delete(pin_url(target.id), headers=admin_headers)
        second = client.delete(pin_url(target.id), headers=admin_headers)

        assert first.status_code == 200 and second.status_code == 200
        assert first.json()["has_floor_pin"] is False
        assert audit_rows(db_session) == []

    def test_clear_twice_writes_one_row(self, client, db_session, admin_headers):
        target = make_user(db_session, pin=PIN)

        assert client.delete(pin_url(target.id), headers=admin_headers).status_code == 200
        assert client.delete(pin_url(target.id), headers=admin_headers).status_code == 200

        assert len(audit_rows(db_session, "FLOOR_PIN_CLEARED")) == 1

    @pytest.mark.parametrize(
        "pin,rule",
        [
            ("0000", PIN_RULE_REPEATED),
            ("1234", PIN_RULE_SEQUENCE),
            ("654321", PIN_RULE_SEQUENCE),
            ("25a0", PIN_RULE_DIGITS),
            ("258", PIN_RULE_DIGITS),
            ("2580135", PIN_RULE_DIGITS),
            ("", PIN_RULE_DIGITS),
        ],
    )
    def test_a_broken_rule_is_a_422_naming_it_and_nothing_changes(self, client, db_session, admin_headers, pin, rule):
        target = make_user(db_session, pin=PIN)
        before = pin_state(db_session, target)

        response = client.put(pin_url(target.id), json={"pin": pin}, headers=admin_headers)

        assert response.status_code == 422
        assert response.json()["detail"] == rule
        if pin:
            assert pin not in response.text
        assert pin_state(db_session, target) == before
        assert audit_rows(db_session) == []

    @pytest.mark.parametrize("sent", [7391, 739.1, True, [7, 3, 9, 1], {"pin": "7391"}])
    def test_a_non_string_pin_is_a_422_that_never_echoes_it(self, client, db_session, admin_headers, sent):
        target = make_user(db_session, employee_id="FPA-7391")

        response = client.put(pin_url(target.id), json={"pin": sent}, headers=admin_headers)

        assert response.status_code == 422
        assert "7391" not in response.text and "739.1" not in response.text
        assert pin_state(db_session, target)[0] is None

    def test_the_badge_digits_sent_as_a_number_are_refused_by_the_badge_rule_unechoed(
        self, client, db_session, admin_headers
    ):
        target = make_user(db_session, employee_id="EMP-602917")

        response = client.put(pin_url(target.id), json={"pin": 602917}, headers=admin_headers)

        assert response.status_code == 422
        assert response.json()["detail"] == PIN_RULE_BADGE
        assert "602917" not in response.text

    @pytest.mark.parametrize("body", [{}, {"pin": None}])
    def test_a_missing_or_null_pin_is_a_422(self, client, db_session, admin_headers, body):
        target = make_user(db_session)
        response = client.put(pin_url(target.id), json=body, headers=admin_headers)
        assert response.status_code == 422
        assert pin_state(db_session, target)[0] is None

    def test_a_pin_with_a_trailing_newline_is_a_422_not_a_500(self, client, db_session, admin_headers):
        target = make_user(db_session)

        response = client.put(pin_url(target.id), json={"pin": "2580\n"}, headers=admin_headers)

        assert response.status_code == 422
        assert response.json()["detail"] == PIN_RULE_DIGITS

    def test_reads_show_pin_state_never_the_hash(self, client, db_session, admin_headers):
        target = make_user(db_session, pin=PIN)
        target.floor_pin_locked_until = datetime.utcnow() + timedelta(minutes=5)
        db_session.commit()
        stored_hash = pin_state(db_session, target)[0]

        listing = client.get(f"{API}/users/", headers=admin_headers)
        detail = client.get(f"{API}/users/{target.id}", headers=admin_headers)
        me = client.get(f"{API}/users/me", headers=headers_for(target))

        for response in (listing, detail, me):
            assert response.status_code == 200, response.text
            assert stored_hash not in response.text and "floor_pin_hash" not in response.text
        listed = {u["id"]: u for u in listing.json()}[target.id]
        for shown in (listed, detail.json(), me.json()):
            assert shown["has_floor_pin"] is True
            assert shown["floor_pin_set_at"] and shown["floor_pin_locked_until"]
            assert shown["floor_pin_locked_until"].endswith("Z")


# ===========================================================================
# 3. Who may set / clear
# ===========================================================================
VERBS = [("put", {"pin": PIN}), ("delete", None), ("post:unlock", None)]
VERB_IDS = ["set", "clear", "unlock"]


def call(client: TestClient, verb: str, user_id: int, body: Optional[Dict[str, Any]], headers: Dict[str, str]):
    method, _, suffix = verb.partition(":")
    url = pin_url(user_id) + (f"/{suffix}" if suffix else "")
    return client.request(method, url, json=body, headers=headers)


def locked_target(db: Session, **kwargs: Any) -> User:
    """A target every verb has work to do on: a PIN, two wrong tries, a lock in force."""
    user = make_user(db, pin=OTHER_PIN, **kwargs)
    user.floor_pin_failed_attempts = 2
    user.floor_pin_locked_until = datetime.utcnow() + timedelta(minutes=10)
    db.commit()
    return user


class TestRoleMatrix:
    @pytest.mark.parametrize("verb,body", VERBS, ids=VERB_IDS)
    @pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.MANAGER])
    def test_admin_and_manager_may_manage_an_operators_pin(self, client, db_session, verb, body, role):
        actor = make_user(db_session, role=role)
        target = locked_target(db_session)

        response = call(client, verb, target.id, body, headers_for(actor))

        assert response.status_code == 200, response.text
        row = audit_rows(db_session)[-1]
        assert row.user_id == actor.id and row.resource_id == target.id

    @pytest.mark.parametrize("verb,body", VERBS, ids=VERB_IDS)
    @pytest.mark.parametrize(
        "role", [UserRole.SUPERVISOR, UserRole.OPERATOR, UserRole.QUALITY, UserRole.SHIPPING, UserRole.VIEWER]
    )
    def test_every_other_role_is_refused_and_nothing_changes(self, client, db_session, verb, body, role):
        actor = make_user(db_session, role=role)
        target = make_user(db_session, pin=OTHER_PIN)
        before = pin_state(db_session, target)

        response = call(client, verb, target.id, body, headers_for(actor))

        assert response.status_code == 403
        assert response.json()["detail"] == "Insufficient permissions"
        assert pin_state(db_session, target) == before
        assert audit_rows(db_session) == []

    def test_an_operator_cannot_set_their_own_pin(self, client, db_session):
        operator = make_user(db_session)
        response = client.put(pin_url(operator.id), json={"pin": PIN}, headers=headers_for(operator))
        assert response.status_code == 403
        assert pin_state(db_session, operator)[0] is None

    @pytest.mark.parametrize("verb,body", VERBS, ids=VERB_IDS)
    @pytest.mark.parametrize("target_role", ADMIN_ONLY_TARGETS)
    def test_a_manager_cannot_touch_an_admin_manager_or_quality_pin(self, client, db_session, verb, body, target_role):
        """A PIN the caller chose plus the badge is a sign-on: a Manager must never be able to
        sign in as a peer, a superior or the person who signs quality off."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        target = locked_target(db_session, role=target_role)
        before = pin_state(db_session, target)

        response = call(client, verb, target.id, body, headers_for(manager))

        assert response.status_code == 403
        assert response.json()["detail"] == MANAGER_SCOPE_DETAIL
        assert pin_state(db_session, target) == before
        assert audit_rows(db_session) == []

    @pytest.mark.parametrize("verb,body", VERBS, ids=VERB_IDS)
    @pytest.mark.parametrize("target_role", MANAGER_TARGETS + [UserRole.ADMIN])
    def test_a_manager_cannot_touch_a_superusers_pin_whatever_its_role(
        self, client, db_session, verb, body, target_role
    ):
        """A superuser target is refused even when its role is one a Manager may manage. The
        platform-principal rule (``require_manageable_user``) answers first, so the text is the
        platform one, not the Manager-scope one."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        target = locked_target(db_session, role=target_role, superuser=True)
        before = pin_state(db_session, target)

        response = call(client, verb, target.id, body, headers_for(manager))

        assert response.status_code == 403
        assert response.json()["detail"] == PLATFORM_DETAIL
        assert pin_state(db_session, target) == before
        assert audit_rows(db_session) == []

    def test_a_manager_cannot_set_their_own_pin(self, client, db_session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        response = client.put(pin_url(manager.id), json={"pin": PIN}, headers=headers_for(manager))
        assert response.status_code == 403
        assert pin_state(db_session, manager)[0] is None

    @pytest.mark.parametrize("verb,body", VERBS, ids=VERB_IDS)
    @pytest.mark.parametrize("target_role", MANAGER_TARGETS)
    def test_a_manager_may_manage_floor_roles(self, client, db_session, verb, body, target_role):
        manager = make_user(db_session, role=UserRole.MANAGER)
        target = locked_target(db_session, role=target_role)

        response = call(client, verb, target.id, body, headers_for(manager))

        assert response.status_code == 200, response.text
        assert audit_rows(db_session)[-1].user_id == manager.id

    @pytest.mark.parametrize("verb,body", VERBS, ids=VERB_IDS)
    @pytest.mark.parametrize("target_role", ADMIN_ONLY_TARGETS)
    def test_an_admin_may_manage_admin_manager_and_quality_pins(self, client, db_session, verb, body, target_role):
        actor = make_user(db_session, role=UserRole.ADMIN)
        target = locked_target(db_session, role=target_role)

        response = call(client, verb, target.id, body, headers_for(actor))

        assert response.status_code == 200, response.text
        assert audit_rows(db_session)[-1].user_id == actor.id


class TestPlatformPrincipalTargets:
    @pytest.mark.parametrize("verb,body", VERBS, ids=VERB_IDS)
    @pytest.mark.parametrize("actor_role", [UserRole.ADMIN, UserRole.MANAGER])
    @pytest.mark.parametrize(
        "target_role,superuser", [(UserRole.PLATFORM_ADMIN, False), (UserRole.ADMIN, True)], ids=["platform", "su"]
    )
    def test_a_tenant_actor_cannot_touch_a_platform_principals_pin(
        self, client, db_session, verb, body, actor_role, target_role, superuser
    ):
        actor = make_user(db_session, role=actor_role)
        target = locked_target(db_session, role=target_role, superuser=superuser)
        before = pin_state(db_session, target)

        response = call(client, verb, target.id, body, headers_for(actor))

        assert response.status_code == 403
        assert response.json()["detail"] == PLATFORM_DETAIL
        assert pin_state(db_session, target) == before
        assert audit_rows(db_session) == []

    @pytest.mark.parametrize("verb,body", VERBS, ids=VERB_IDS)
    def test_a_stored_platform_principal_may(self, client, db_session, verb, body):
        actor = make_user(db_session, role=UserRole.PLATFORM_ADMIN)
        target = locked_target(db_session, role=UserRole.PLATFORM_ADMIN)

        response = call(client, verb, target.id, body, headers_for(actor))

        assert response.status_code == 200, response.text
        assert audit_rows(db_session)[-1].user_id == actor.id


class TestTenantIsolation:
    @pytest.mark.parametrize("verb,body", VERBS, ids=VERB_IDS)
    def test_another_companys_user_is_404_and_untouched(self, client, db_session, admin_headers, verb, body):
        foreign = make_user(db_session, company_id=2, pin=OTHER_PIN)
        before = pin_state(db_session, foreign)

        response = call(client, verb, foreign.id, body, admin_headers)

        assert response.status_code == 404
        assert response.json()["detail"] == "User not found"
        assert pin_state(db_session, foreign) == before
        assert audit_rows(db_session) == []

    @pytest.mark.parametrize("verb,body", VERBS, ids=VERB_IDS)
    def test_a_foreign_admin_target_is_404_not_the_admin_rule(self, client, db_session, verb, body):
        """The tenant lookup runs first, so a Manager learns nothing about another company's Admin."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        foreign_admin = make_user(db_session, role=UserRole.ADMIN, company_id=2)
        response = call(client, verb, foreign_admin.id, body, headers_for(manager))
        assert response.status_code == 404

    def test_an_unknown_user_is_404(self, client, db_session, admin_headers):
        assert client.put(pin_url(987654), json={"pin": PIN}, headers=admin_headers).status_code == 404
        assert client.delete(pin_url(987654), headers=admin_headers).status_code == 404


# ===========================================================================
# 4. API tokens are refused, before anything is read
# ===========================================================================
class TestApiTokens:
    @pytest.mark.parametrize("verb,body", VERBS, ids=VERB_IDS)
    def test_an_admin_bound_full_token_is_refused_and_nothing_changes(
        self, client, db_session, admin_user, admin_headers, verb, body
    ):
        _id, token = issue_token(client, admin_headers, admin_user)
        target = make_user(db_session, pin=OTHER_PIN)
        before = pin_state(db_session, target)
        rows_before = len(audit_rows(db_session))

        response = call(client, verb, target.id, body, token_headers(token))

        assert response.status_code == 403
        assert response.json()["detail"] == API_TOKEN_FLOOR_PIN_ADMIN
        assert pin_state(db_session, target) == before
        assert len(audit_rows(db_session)) == rows_before

    @pytest.mark.parametrize("verb,body", VERBS, ids=VERB_IDS)
    @pytest.mark.parametrize("target_kind", ["missing", "foreign"])
    def test_the_token_refusal_comes_before_the_target_lookup(
        self, client, db_session, admin_user, admin_headers, verb, body, target_kind
    ):
        _id, token = issue_token(client, admin_headers, admin_user)
        target_id = 987654 if target_kind == "missing" else make_user(db_session, company_id=2).id

        response = call(client, verb, target_id, body, token_headers(token))

        assert response.status_code == 403, "a 404 here would make the token an account oracle"
        assert response.json()["detail"] == API_TOKEN_FLOOR_PIN_ADMIN

    @pytest.mark.parametrize("verb,body", VERBS, ids=VERB_IDS)
    def test_a_read_only_token_is_refused_by_the_read_only_gate(
        self, client, db_session, admin_user, admin_headers, verb, body
    ):
        _id, token = issue_token(client, admin_headers, admin_user, scope="read_only", ai_external=False)
        target = make_user(db_session, pin=OTHER_PIN)
        before = pin_state(db_session, target)

        response = call(client, verb, target.id, body, token_headers(token))

        assert response.status_code == 403
        assert response.json()["detail"] == READ_ONLY_ROUTE_WRITES_DETAIL
        assert pin_state(db_session, target) == before


# ===========================================================================
# 5. The audit row is REQUIRED evidence
# ===========================================================================
class TestRequiredAudit:
    @staticmethod
    def _fail_required_audit(monkeypatch):
        def refuse(self, action, resource_type, **kwargs):
            raise AuditWriteError("simulated audit outage")

        monkeypatch.setattr(AuditService, "log_required", refuse)

    @pytest.mark.parametrize("verb,body", VERBS, ids=VERB_IDS)
    def test_audit_loss_is_a_503_with_nothing_saved(self, client, db_session, admin_headers, monkeypatch, verb, body):
        target = make_user(db_session, pin=OTHER_PIN)
        target.floor_pin_failed_attempts = 2
        target.floor_pin_locked_until = datetime.utcnow() + timedelta(minutes=10)
        db_session.commit()
        before = pin_state(db_session, target)
        self._fail_required_audit(monkeypatch)

        response = call(client, verb, target.id, body, admin_headers)

        assert response.status_code == 503, response.text
        assert response.json()["detail"] == AUDIT_UNAVAILABLE
        assert pin_state(db_session, target) == before, "the PIN, its counter and its lock all roll back"
        assert audit_rows(db_session) == []

    def test_audit_loss_on_a_first_set_leaves_no_pin(self, client, db_session, admin_headers, monkeypatch):
        target = make_user(db_session)
        self._fail_required_audit(monkeypatch)

        response = client.put(pin_url(target.id), json={"pin": PIN}, headers=admin_headers)

        assert response.status_code == 503
        assert pin_state(db_session, target)[0] is None
        sign_on = client.post(EMPLOYEE_LOGIN, json={"employee_id": target.employee_id}, headers=XHR)
        assert sign_on.status_code == 200, "no PIN was saved, so none is asked for"


# ===========================================================================
# 5b. Unlock: lift a PIN lock early, keep the PIN
# ===========================================================================
class TestUnlock:
    def test_unlock_lifts_the_lock_keeps_the_pin_and_records_what_it_lifted(self, client, db_session, admin_headers):
        target = locked_target(db_session)
        stored_hash, set_at, _attempts, locked_until = pin_state(db_session, target)

        response = call(client, "post:unlock", target.id, None, admin_headers)

        assert response.status_code == 200, response.text
        assert response.json()["floor_pin_locked_until"] is None
        assert response.json()["has_floor_pin"] is True
        assert pin_state(db_session, target) == (stored_hash, set_at, 0, None), "the PIN itself is unchanged"
        (row,) = audit_rows(db_session, "FLOOR_PIN_UNLOCKED")
        assert row.resource_type == "user" and row.resource_id == target.id
        assert row.extra_data == {
            "source": "admin_unlock",
            "floor_pin_locked_until": locked_until.isoformat(),
            "floor_pin_failed_attempts": 2,
        }
        assert stored_hash not in row_text(row) and OTHER_PIN not in row_text(row)

        signed_in = client.post(EMPLOYEE_LOGIN, json={"employee_id": target.employee_id, "pin": OTHER_PIN}, headers=XHR)
        assert signed_in.status_code == 200, "the existing PIN works again at once"

    @pytest.mark.parametrize("lock", [None, -1], ids=["never-locked", "lock-expired"])
    def test_unlock_with_no_lock_in_force_is_a_200_with_no_row(self, client, db_session, admin_headers, lock):
        target = make_user(db_session, pin=OTHER_PIN)
        if lock is not None:
            target.floor_pin_locked_until = datetime.utcnow() + timedelta(minutes=lock)
            db_session.commit()
        before = pin_state(db_session, target)

        response = call(client, "post:unlock", target.id, None, admin_headers)

        assert response.status_code == 200
        assert pin_state(db_session, target) == before
        assert audit_rows(db_session) == []

    def test_unlock_does_not_touch_the_password_lockout(self, client, db_session, admin_headers):
        target = locked_target(db_session)
        password_lock = datetime.utcnow() + timedelta(minutes=30)
        target.failed_login_attempts = 5
        target.locked_until = password_lock
        db_session.commit()

        assert call(client, "post:unlock", target.id, None, admin_headers).status_code == 200

        db_session.expire_all()
        refreshed = db_session.get(User, target.id)
        assert refreshed.failed_login_attempts == 5 and refreshed.locked_until == password_lock


class TestCredentialColumnsNeverAudited:
    def test_a_model_snapshot_never_carries_the_pin_hash_whatever_the_caller_excludes(self, db_session):
        user = make_user(db_session, pin=PIN)
        audit = AuditService(db_session)

        for exclude in (None, set(), {"email"}):
            snapshot = audit._model_to_dict(user, exclude_fields=exclude)
            assert "floor_pin_hash" not in snapshot, exclude
            assert "hashed_password" not in snapshot, exclude
            assert user.floor_pin_hash not in json.dumps(snapshot, default=str)
        assert "floor_pin_set_at" in audit._model_to_dict(user), "PIN STATE may be audited, the hash never"


# ===========================================================================
# 5c. A promotion INTO a judgment role clears the PIN (user_provisioning.clear_floor_pin_on_promotion)
# ===========================================================================
PROMOTION_MARKER = "floor_pin_cleared_on_promotion"


def manager_set_pin(client: TestClient, db: Session, target: User, *, lock: bool = True) -> None:
    """The realistic path the rule exists for: a Manager chose this person's PIN."""
    manager = make_user(db, role=UserRole.MANAGER)
    response = client.put(pin_url(target.id), json={"pin": OTHER_PIN}, headers=headers_for(manager))
    assert response.status_code == 200, response.text
    if lock:
        db.expire_all()
        row = db.get(User, target.id)
        row.floor_pin_failed_attempts = 3
        row.floor_pin_locked_until = datetime.utcnow() + timedelta(minutes=10)
        db.commit()


def role_rows(db: Session, action: str, user: User) -> List[AuditLog]:
    return [r for r in audit_rows(db, action) if r.resource_type == "user" and r.resource_id == user.id]


def assert_no_credential_in(row: AuditLog, stored_hash: Optional[str]) -> None:
    text = row_text(row)
    assert OTHER_PIN not in text and "floor_pin_hash" not in text and "$2" not in text
    if stored_hash:
        assert stored_hash not in text


class TestPromotionClearsThePin:
    @pytest.mark.parametrize(
        "from_role,to_role",
        [
            (UserRole.OPERATOR, UserRole.QUALITY),
            (UserRole.OPERATOR, UserRole.ADMIN),
            (UserRole.SUPERVISOR, UserRole.MANAGER),
            (UserRole.SHIPPING, UserRole.ADMIN),
            (UserRole.VIEWER, UserRole.QUALITY),
        ],
    )
    def test_a_role_change_into_a_judgment_role_clears_the_pin_and_marks_the_row(
        self, client, db_session, admin_headers, from_role, to_role
    ):
        target = make_user(db_session, role=from_role)
        manager_set_pin(client, db_session, target)
        stored_hash = pin_state(db_session, target)[0]

        response = client.put(f"{API}/users/{target.id}", json={"role": to_role.value}, headers=admin_headers)

        assert response.status_code == 200, response.text
        assert response.json()["has_floor_pin"] is False
        assert response.json()["floor_pin_set_at"] is None and response.json()["floor_pin_locked_until"] is None
        assert pin_state(db_session, target) == (None, None, 0, None), "hash, set date, counter and lock all go"
        (row,) = role_rows(db_session, "UPDATE", target)
        assert row.extra_data[PROMOTION_MARKER] is True
        assert row.extra_data["changes"]["role"]["new"] in (to_role.value, str(to_role))
        assert_no_credential_in(row, stored_hash)

    def test_the_old_pin_no_longer_counts_and_the_badge_alone_is_badge_only(self, client, db_session, admin_headers):
        target = make_user(db_session, role=UserRole.OPERATOR)
        manager_set_pin(client, db_session, target, lock=False)
        promoted = client.put(f"{API}/users/{target.id}", json={"role": "quality"}, headers=admin_headers)
        assert promoted.status_code == 200, promoted.text

        old_pin = client.post(EMPLOYEE_LOGIN, json={"employee_id": target.employee_id, "pin": OTHER_PIN}, headers=XHR)
        badge_only = client.post(EMPLOYEE_LOGIN, json={"employee_id": target.employee_id}, headers=XHR)

        for response in (old_pin, badge_only):
            assert response.status_code == 200, response.text
            claims = jwt.decode(response.json()["access_token"], settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
            assert claims["amr"] == ["badge"], "the old PIN proves nothing any more"

        db_session.get(Company, 1).require_floor_pin = True
        db_session.commit()
        refused = client.post(EMPLOYEE_LOGIN, json={"employee_id": target.employee_id, "pin": OTHER_PIN}, headers=XHR)
        assert refused.status_code == 403 and refused.json()["detail"]["code"] == "floor_pin_not_set"

    def test_after_the_promotion_a_manager_cannot_set_a_new_pin_but_an_admin_can(
        self, client, db_session, admin_headers
    ):
        target = make_user(db_session, role=UserRole.OPERATOR)
        manager_set_pin(client, db_session, target, lock=False)
        assert (
            client.put(f"{API}/users/{target.id}", json={"role": "manager"}, headers=admin_headers).status_code == 200
        )
        manager = make_user(db_session, role=UserRole.MANAGER)

        by_manager = client.put(pin_url(target.id), json={"pin": OTHER_PIN}, headers=headers_for(manager))
        by_admin = client.put(pin_url(target.id), json={"pin": PIN}, headers=admin_headers)

        assert by_manager.status_code == 403 and by_manager.json()["detail"] == MANAGER_SCOPE_DETAIL
        assert by_admin.status_code == 200, by_admin.text
        assert verify_floor_pin(PIN, pin_state(db_session, target)[0])

    @pytest.mark.parametrize(
        "from_role,to_role",
        [
            (UserRole.MANAGER, UserRole.ADMIN),  # judgment -> judgment
            (UserRole.QUALITY, UserRole.MANAGER),  # judgment -> judgment
            (UserRole.ADMIN, UserRole.MANAGER),  # demotion within the judgment roles
            (UserRole.QUALITY, UserRole.OPERATOR),  # demotion out of them
            (UserRole.ADMIN, UserRole.VIEWER),  # demotion out of them
            (UserRole.OPERATOR, UserRole.SUPERVISOR),  # non-judgment move
            (UserRole.VIEWER, UserRole.SHIPPING),  # non-judgment move
        ],
    )
    def test_changes_that_are_not_a_promotion_into_judgment_keep_the_pin(
        self, client, db_session, admin_headers, from_role, to_role
    ):
        target = make_user(db_session, role=from_role, pin=OTHER_PIN)
        target.floor_pin_failed_attempts = 1
        db_session.commit()
        before = pin_state(db_session, target)

        response = client.put(f"{API}/users/{target.id}", json={"role": to_role.value}, headers=admin_headers)

        assert response.status_code == 200, response.text
        assert response.json()["has_floor_pin"] is True
        assert pin_state(db_session, target) == before
        (row,) = role_rows(db_session, "UPDATE", target)
        assert PROMOTION_MARKER not in (row.extra_data or {})

    def test_a_promotion_with_no_pin_state_writes_no_marker(self, client, db_session, admin_headers):
        target = make_user(db_session, role=UserRole.OPERATOR)

        response = client.put(f"{API}/users/{target.id}", json={"role": "admin"}, headers=admin_headers)

        assert response.status_code == 200, response.text
        (row,) = role_rows(db_session, "UPDATE", target)
        assert PROMOTION_MARKER not in (row.extra_data or {})
        assert pin_state(db_session, target) == (None, None, 0, None)

    def test_a_promotion_clears_a_leftover_lock_even_without_a_pin(self, client, db_session, admin_headers):
        target = make_user(db_session, role=UserRole.OPERATOR)
        target.floor_pin_failed_attempts = 2
        target.floor_pin_locked_until = datetime.utcnow() + timedelta(minutes=5)
        db_session.commit()

        response = client.put(f"{API}/users/{target.id}", json={"role": "quality"}, headers=admin_headers)

        assert response.status_code == 200, response.text
        assert pin_state(db_session, target) == (None, None, 0, None)
        (row,) = role_rows(db_session, "UPDATE", target)
        assert row.extra_data[PROMOTION_MARKER] is True

    def test_an_update_that_does_not_change_the_role_keeps_the_pin(self, client, db_session, admin_headers):
        target = make_user(db_session, role=UserRole.OPERATOR, pin=OTHER_PIN)
        before = pin_state(db_session, target)

        response = client.put(f"{API}/users/{target.id}", json={"department": "Weld"}, headers=admin_headers)

        assert response.status_code == 200, response.text
        assert pin_state(db_session, target) == before

    @pytest.mark.parametrize("to_role,cleared", [("manager", True), ("quality", True), ("operator", False)])
    def test_approval_follows_the_same_rule(self, client, db_session, admin_headers, to_role, cleared):
        pending = make_user(db_session, role=UserRole.VIEWER, pin=OTHER_PIN, is_active=False)
        before = pin_state(db_session, pending)

        response = client.post(f"{API}/users/{pending.id}/approve", json={"role": to_role}, headers=admin_headers)

        assert response.status_code == 200, response.text
        (row,) = role_rows(db_session, "APPROVE", pending)
        if cleared:
            assert pin_state(db_session, pending) == (None, None, 0, None)
            assert row.extra_data[PROMOTION_MARKER] is True
            assert_no_credential_in(row, before[0])
        else:
            assert pin_state(db_session, pending) == before
            assert PROMOTION_MARKER not in (row.extra_data or {})

    @pytest.mark.parametrize("route", ["update", "approve"])
    def test_audit_loss_rolls_the_clear_back_with_the_role_change(
        self, client, db_session, admin_headers, monkeypatch, route
    ):
        target = make_user(db_session, role=UserRole.VIEWER, pin=OTHER_PIN, is_active=(route == "update"))
        target.floor_pin_locked_until = datetime.utcnow() + timedelta(minutes=5)
        db_session.commit()
        before = pin_state(db_session, target)

        def refuse(self, action, resource_type, **kwargs):
            raise AuditWriteError("simulated audit outage")

        monkeypatch.setattr(AuditService, "log_required", refuse)

        if route == "update":
            response = client.put(f"{API}/users/{target.id}", json={"role": "admin"}, headers=admin_headers)
        else:
            response = client.post(f"{API}/users/{target.id}/approve", json={"role": "admin"}, headers=admin_headers)

        assert response.status_code == 503, response.text
        assert response.json()["detail"] == AUDIT_UNAVAILABLE
        assert pin_state(db_session, target) == before, "the PIN survives a role change that did not happen"
        db_session.expire_all()
        assert db_session.get(User, target.id).role == UserRole.VIEWER
        assert audit_rows(db_session) == []


# ===========================================================================
# 6. The company policy
# ===========================================================================
class TestPolicyRoute:
    def test_an_admin_turns_it_on_with_two_required_rows_and_a_count(
        self, client, db_session, admin_user, admin_headers
    ):
        make_user(db_session, pin=PIN)  # has a PIN: not counted
        make_user(db_session)  # active, no PIN: counted
        make_user(db_session, is_active=False)  # inactive: not counted
        make_user(db_session, company_id=2)  # another company: not counted
        make_user(db_session, employee_id="")  # no badge to sign in with: not counted
        make_user(db_session, role=UserRole.PLATFORM_ADMIN)  # platform oversight, not a floor badge
        make_user(db_session, role=UserRole.ADMIN, superuser=True)  # a superuser likewise

        response = client.put(POLICY, json={"require_floor_pin": True}, headers=admin_headers)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["require_floor_pin"] is True
        assert body["users_without_pin"] == 2, "admin_user + the one active PIN-less operator"
        db_session.expire_all()
        assert db_session.get(Company, 1).require_floor_pin is True
        assert db_session.get(Company, 2).require_floor_pin is False, "only the caller's own company"
        rows = [r for r in audit_rows(db_session) if r.resource_type == "company"]
        assert [r.action for r in rows] == ["UPDATE", "STATUS_CHANGE"]
        assert rows[0].old_values == {"require_floor_pin": False}
        assert rows[0].new_values == {"require_floor_pin": True}
        assert rows[0].extra_data["changes"] == {"require_floor_pin": {"old": False, "new": True}}
        assert rows[1].old_values == {"status": "floor_pin_optional"}
        assert rows[1].new_values == {"status": "floor_pin_required"}
        assert {r.user_id for r in rows} == {admin_user.id}
        assert {r.resource_id for r in rows} == {1}

    def test_the_policy_is_readable_on_the_company_record(self, client, db_session, admin_headers):
        assert client.get(f"{API}/companies/me", headers=admin_headers).json()["require_floor_pin"] is False
        client.put(POLICY, json={"require_floor_pin": True}, headers=admin_headers)
        assert client.get(f"{API}/companies/me", headers=admin_headers).json()["require_floor_pin"] is True

    def test_turning_it_off_also_reports_the_count(self, client, db_session, admin_headers):
        db_session.get(Company, 1).require_floor_pin = True
        db_session.commit()

        response = client.put(POLICY, json={"require_floor_pin": False}, headers=admin_headers)

        assert response.status_code == 200
        assert response.json()["require_floor_pin"] is False
        assert response.json()["users_without_pin"] == 1
        rows = [r for r in audit_rows(db_session) if r.resource_type == "company"]
        assert rows[1].new_values == {"status": "floor_pin_optional"}

    @pytest.mark.parametrize("value", [True, False])
    def test_setting_the_value_it_already_has_writes_nothing(self, client, db_session, admin_headers, value):
        db_session.get(Company, 1).require_floor_pin = value
        db_session.commit()

        response = client.put(POLICY, json={"require_floor_pin": value}, headers=admin_headers)

        assert response.status_code == 200
        assert response.json()["require_floor_pin"] is value
        assert audit_rows(db_session) == []

    @pytest.mark.parametrize(
        "role",
        [
            UserRole.MANAGER,
            UserRole.SUPERVISOR,
            UserRole.OPERATOR,
            UserRole.QUALITY,
            UserRole.SHIPPING,
            UserRole.VIEWER,
        ],
    )
    def test_only_an_admin(self, client, db_session, role):
        actor = make_user(db_session, role=role)

        response = client.put(POLICY, json={"require_floor_pin": True}, headers=headers_for(actor))

        assert response.status_code == 403
        db_session.expire_all()
        assert db_session.get(Company, 1).require_floor_pin is False
        assert audit_rows(db_session) == []

    @pytest.mark.parametrize("value", [True, False])
    def test_an_admin_bound_full_token_is_refused_either_way(
        self, client, db_session, admin_user, admin_headers, value
    ):
        db_session.get(Company, 1).require_floor_pin = not value
        db_session.commit()
        _id, token = issue_token(client, admin_headers, admin_user)
        rows_before = len(audit_rows(db_session))

        response = client.put(POLICY, json={"require_floor_pin": value}, headers=token_headers(token))

        assert response.status_code == 403
        assert response.json()["detail"] == API_TOKEN_FLOOR_PIN_ADMIN
        db_session.expire_all()
        assert db_session.get(Company, 1).require_floor_pin is (not value)
        assert len(audit_rows(db_session)) == rows_before

    def test_a_read_only_token_is_refused(self, client, db_session, admin_user, admin_headers):
        _id, token = issue_token(client, admin_headers, admin_user, scope="read_only", ai_external=False)
        response = client.put(POLICY, json={"require_floor_pin": True}, headers=token_headers(token))
        assert response.status_code == 403
        assert response.json()["detail"] == READ_ONLY_ROUTE_WRITES_DETAIL

    @pytest.mark.parametrize("failing_call", [1, 2], ids=["update_row_fails", "status_change_row_fails"])
    def test_audit_loss_rolls_the_flip_back(self, client, db_session, admin_headers, monkeypatch, failing_call):
        real = AuditService.log_required
        calls = {"n": 0}

        def flaky(self, action, resource_type, **kwargs):
            calls["n"] += 1
            if calls["n"] == failing_call:
                raise AuditWriteError("simulated audit outage")
            return real(self, action, resource_type, **kwargs)

        monkeypatch.setattr(AuditService, "log_required", flaky)

        response = client.put(POLICY, json={"require_floor_pin": True}, headers=admin_headers)

        assert response.status_code == 503, response.text
        assert response.json()["detail"] == FLOOR_PIN_POLICY_AUDIT_UNAVAILABLE_DETAIL
        assert calls["n"] == failing_call
        db_session.expire_all()
        assert db_session.get(Company, 1).require_floor_pin is False
        assert audit_rows(db_session) == []

    def test_a_non_boolean_is_a_422(self, client, db_session, admin_headers):
        response = client.put(POLICY, json={"require_floor_pin": "sometimes"}, headers=admin_headers)
        assert response.status_code == 422
        db_session.expire_all()
        assert db_session.get(Company, 1).require_floor_pin is False


# ===========================================================================
# 7. Not MCP tools
# ===========================================================================
class TestMcpExclusion:
    @pytest.fixture(scope="class")
    def spec(self):
        return app.openapi()

    def test_the_four_routes_are_the_floor_pin_exclusions(self):
        assert set(FLOOR_PIN_OPERATIONS) == {
            ("PUT", "/api/v1/users/{user_id}/floor-pin"),
            ("DELETE", "/api/v1/users/{user_id}/floor-pin"),
            ("POST", "/api/v1/users/{user_id}/floor-pin/unlock"),
            ("PUT", "/api/v1/companies/me/floor-pin-policy"),
        }
        assert set(FLOOR_PIN_OPERATIONS) <= set(EXCLUDED_OPERATIONS)

    def test_each_is_a_real_secured_route_so_the_exclusion_is_load_bearing(self, spec):
        secured = {(method, path) for method, path, _op, _params in iter_secured_operations(spec)}
        assert set(FLOOR_PIN_OPERATIONS) <= secured

    def test_none_is_a_tool_and_only_the_exclusion_keeps_them_out(self, spec):
        catalog = build_catalog(spec, shadowed=SHADOWED_OPERATIONS, reserved_names=CONVENIENCE_TOOL_NAMES)
        keys = {tool.key for tool in catalog}
        assert keys.isdisjoint(FLOOR_PIN_OPERATIONS)
        assert not any("floor_pin" in tool.name for tool in catalog)

        without = build_catalog(
            spec,
            shadowed=SHADOWED_OPERATIONS,
            reserved_names=CONVENIENCE_TOOL_NAMES,
            excluded_operations=set(EXCLUDED_OPERATIONS) - set(FLOOR_PIN_OPERATIONS),
        )
        assert set(FLOOR_PIN_OPERATIONS) <= {tool.key for tool in without}

    def test_the_pin_state_and_policy_reads_stay_tools(self, spec):
        catalog = build_catalog(spec, shadowed=SHADOWED_OPERATIONS, reserved_names=CONVENIENCE_TOOL_NAMES)
        keys = {tool.key for tool in catalog}
        assert ("GET", "/api/v1/users/{user_id}") in keys
        assert ("GET", "/api/v1/companies/me") in keys
