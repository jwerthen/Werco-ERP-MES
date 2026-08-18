"""``POST /auth/login`` takes a badge as well as an address.

The password-carrying login route used to read ``username`` as an email address only, so
a badge-only account -- one registered with an employee ID and no email
(``test_public_registration_identifiers.py``) -- had exactly one door: the passwordless
``/auth/employee-login``. ``login()`` now splits on ``"@"``: an input containing one takes
the pre-existing email resolver, anything else takes ``_find_user_by_employee_id`` (badge
normalization included). ``/auth/employee-login`` is untouched -- nobody loses a way in.

What these tests are protecting
-------------------------------
1. **The 401 must not become an account oracle.** The badge keyspace is ~10^4 (the
   resolver normalizes to four trailing digits), so "no such badge" and "wrong password"
   answering differently would let an unauthenticated caller sweep the shop's badge
   numbers with no password at all. §2 asserts the two answers are EQUAL TO EACH OTHER --
   not that each matches a literal -- so the test still holds if the wording is ever
   changed, and fails the moment they diverge.

2. **The email path must be byte-identical.** Everything else in the app signs in with an
   address; this feature is only allowed to ADD a path. §3 pins the exact pre-existing
   ``"Invalid email or password"`` detail and a successful email login, and pins that a
   failure at an ORDINARY domain never feeds the throttle (§5 covers the addresses that
   deliberately do).

3. **The two brute-force controls both still engage, and on the right thing.** The badge
   path drives the 5-failure ACCOUNT lockout (§4) -- which also locks the operator out of
   the kiosk, an operational consequence worth pinning explicitly -- and it carries the
   per-IP FAILED-attempt throttle ``/auth/employee-login`` uses (§5), because a route that
   can lock any account from a guessable four-digit identifier needs a bound that is not
   just the 5/min per-path limit. §5 is keyed on whether the SUBMITTED IDENTIFIER IS
   ENUMERABLE, not on whether it contains an ``"@"`` -- see that section's header for the
   bypass the distinction closes.

4. **Ambiguity still refuses rather than guessing** (§6): employee IDs are unique
   PER COMPANY, this route is install-wide and unauthenticated, so a badge held in two
   tenants is 409 -- never an arbitrary tenant's account.

5. **The badge resolver must answer the same way twice, or refuse** (§7). Its normalized
   fallback narrows in SQL with a single-character ``ilike`` core, so on a real shop's
   user table the candidate window is large; the window is ordered, capped, and a
   TRUNCATED window is refused rather than answered from. Getting the cap wrong is
   outage-shaped in BOTH directions, which is why §7 pins it from both sides.

Two fixtures shape the file
---------------------------
* ``_reset_throttle`` (autouse) forces the per-IP throttle into memory mode and clears it,
  exactly as ``test_employee_login_throttle.py`` does, so each test gets a deterministic
  failure budget.
* ``_login`` clears the slowapi counter before each request. ``/auth/login`` allows 5/min
  per IP and several tests here deliberately need 8-10 attempts to reach the *other* two
  controls; the 5/min limit itself is a separate control with its own coverage in
  ``test_auth_rate_limit.py``, and letting it fire here would mask the behavior under test
  behind a 429 that means something else entirely.
"""

import ast
import pathlib
import re
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException, status
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import event
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateTable

import app.api.endpoints.auth as auth_module
import app.main as app_main
import app.services.user_identity as user_identity
from app.api.endpoints.auth import (
    _AMBIGUOUS_EMPLOYEE_ID_DETAIL,
    _AUDIT_ERROR_CONFLICT_UNCLASSIFIED,
    _AUDIT_ERROR_EMPLOYEE_ID_AMBIGUOUS,
    _AUDIT_ERROR_EMPLOYEE_ID_UNRESOLVABLE,
    _AUDIT_IDENTIFIER_MAX_LENGTH,
    _AUDIT_TRUNCATION_MARKER,
    _EMPLOYEE_ID_CANDIDATE_CAP,
    _UNRESOLVABLE_EMPLOYEE_ID_DETAIL,
    MAX_LOGIN_IDENTIFIER_LENGTH,
    _AmbiguousIdentifier,
    _bounded_audit_value,
    _conflict_audit_error,
    _find_user_by_employee_id_in_company,
    _normalized_employee_id_matches,
    _UnresolvableIdentifier,
)
from app.core import login_throttle
from app.core.config import settings
from app.core.login_throttle import (
    EMPLOYEE_LOGIN_COOLDOWN_SECONDS,
    EMPLOYEE_LOGIN_MAX_FAILURES,
    PASSWORD_LOGIN_COOLDOWN_SECONDS,
    PASSWORD_LOGIN_MAX_FAILURES,
    employee_login_throttle,
    password_login_throttle,
)
from app.core.security import get_password_hash
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.user import User, UserRole
from app.services.user_identity import (
    LEGACY_RESERVED_EMAIL_DOMAIN,
    MAX_IDENTIFIER_CANDIDATES,
    SYNTHETIC_EMAIL_DOMAIN,
    IdentifierDerivationExhausted,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1  # the seeded company
COMPANY_B = 2

LOGIN_URL = "/api/v1/auth/login"
EMPLOYEE_LOGIN_URL = "/api/v1/auth/employee-login"

PASSWORD = "SecureP@ss123!"
WRONG_PASSWORD = "NotThePassword999!"

EMAIL_401 = "Invalid email or password"
BADGE_401 = "Invalid employee ID or password"

_seq = {"n": 0}


def _all_throttles():
    """EVERY ``FailedLoginThrottle`` the app defines, discovered by walking the module.

    Naming the instances would have been enough on the day this was written, and that is
    exactly how it leaked: the fixture reset the kiosk counter only, so when ``/auth/login``
    got its own instance the new counter accumulated across the whole file and later tests
    saw a 429 left behind by an earlier one -- a failure that reproduced only in file order
    and vanished when the test was run alone.

    Walking the module means the throttle added for the NEXT route is isolated on the day
    it is added, rather than on the day it silently corrupts somebody else's test.
    """
    return [value for value in vars(login_throttle).values() if isinstance(value, login_throttle.FailedLoginThrottle)]


@pytest.fixture(autouse=True)
def _reset_throttle(monkeypatch):
    """Fresh per-IP throttle state per test, forced into memory mode."""
    monkeypatch.setattr(settings, "REDIS_URL", None)
    for throttle in _all_throttles():
        throttle.reset()
    yield
    for throttle in _all_throttles():
        throttle.reset()


def _reset_path_limiter() -> None:
    """Clear the slowapi per-path counter (see the module docstring for why)."""
    limiter = getattr(app_main.app.state, "limiter", None)
    if limiter is None:
        return
    try:
        limiter.reset()
    except Exception:  # pragma: no cover - storage backends differ
        storage = getattr(limiter, "_storage", None)
        if storage is not None:
            storage.reset()


def _login(client: TestClient, username: str, password: str = PASSWORD):
    """One ``/auth/login`` attempt with a fresh per-path rate-limit budget."""
    _reset_path_limiter()
    return client.post(LOGIN_URL, data={"username": username, "password": password})


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _ensure_company(db: Session, company_id: int) -> Company:
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        company = Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True)
        db.add(company)
        db.commit()
    return company


def make_user(
    db: Session,
    *,
    company_id: int = COMPANY_A,
    email: str = None,
    employee_id: str = None,
    password: str = PASSWORD,
    role: UserRole = UserRole.OPERATOR,
    is_active: bool = True,
) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=email or f"badge-login-{n}@co{company_id}.example.com",
        employee_id=employee_id or f"BADGELOGIN-{n:05d}",
        first_name="Badge",
        last_name="Login",
        hashed_password=get_password_hash(password),
        role=role,
        is_active=is_active,
        is_superuser=False,
        company_id=company_id,
        failed_login_attempts=0,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def committed_audit_rows(db: Session, action: str):
    """Audit rows that survive a rollback -- i.e. rows the handler really committed.

    The ``client`` fixture shares one open transaction with the endpoint, so a merely
    flushed row would still be visible to a plain query. Borrowed from
    ``test_auth_audit_persistence.py``; call it AFTER the user-state assertions.
    """
    db.rollback()
    return db.query(AuditLog).filter(AuditLog.action == action).all()


def _assert_throttled(response, *, cooldown_seconds: int = PASSWORD_LOGIN_COOLDOWN_SECONDS):
    """A throttle 429, bounded by the cooldown of the ROUTE that produced it.

    The bound is a parameter because the two routes carry different cooldowns on purpose
    (``/auth/login`` 1 h -- its 6 h figure is the failure WINDOW, not the block --
    ``/auth/employee-login`` 15 min). Defaulted to the password
    route's, since that is what this file mostly drives; §5B passes the kiosk's.
    """
    assert response.status_code == status.HTTP_429_TOO_MANY_REQUESTS, response.text
    assert "Too many failed sign-in attempts" in response.json()["detail"]
    retry_after = response.headers.get("Retry-After")
    assert retry_after is not None, "the throttle 429 must carry Retry-After"
    assert 1 <= int(retry_after) <= cooldown_seconds


# ===========================================================================
# 1. A badge and a password sign in
# ===========================================================================


def test_a_badge_and_the_right_password_mint_tokens(client: TestClient, db_session: Session):
    """The feature itself, plus the shape of the record it leaves.

    ``log_auth_event`` keys ``resource_identifier`` on the badge under an explicit
    ``employee-id:`` prefix rather than passing it through ``email=``. That matters
    permanently: migrations 008/060 refuse UPDATE and DELETE on ``audit_logs``, so a row
    that records a badge under a key named "email" can never be corrected, and a consumer
    filtering that key for addresses would swallow exactly the rows a badge sign-in
    produces.
    """
    user = make_user(db_session, employee_id="EMP-4242")

    response = _login(client, "EMP-4242")

    assert response.status_code == status.HTTP_200_OK, response.text
    payload = response.json()
    assert payload["access_token"] and payload["refresh_token"]

    db_session.expire_all()
    assert db_session.get(User, user.id).failed_login_attempts == 0

    (row,) = committed_audit_rows(db_session, "LOGIN_SUCCESS")
    assert row.resource_identifier == "employee-id:EMP-4242"
    assert row.extra_data == {"employee_id": "EMP-4242"}, "the badge must not travel under an 'email' key"
    assert row.success == "true"  # AuditLog.success is a String(10) column


def test_the_badge_resolver_is_the_kiosk_one_normalization_included(client: TestClient, db_session: Session):
    """``_find_user_by_employee_id`` is REUSED, not re-implemented.

    The kiosk normalizes a scanned badge to its four trailing digits, so ``EMP-00339``,
    ``339`` and ``0339`` are one person there. If this route had grown its own exact-match
    lookup instead, the same badge would identify someone at the scanner and nobody at the
    keyboard -- and the operator would be told their password was wrong.
    """
    make_user(db_session, employee_id="0339")

    assert _login(client, "EMP-00339").status_code == status.HTTP_200_OK
    assert _login(client, "339").status_code == status.HTTP_200_OK


# ===========================================================================
# 2. The 401 is not an account oracle
# ===========================================================================


def test_a_wrong_password_is_indistinguishable_from_an_unknown_badge(client: TestClient, db_session: Session):
    """THE anti-enumeration test, asserted as an EQUALITY between the two answers.

    A caller with no password at all must not be able to learn which badge numbers exist.
    Comparing the two responses to each other (rather than each to a literal) is what
    keeps this true after a future wording change, and is what fails if a later edit adds
    a friendlier "no such employee ID" message to one branch.
    """
    make_user(db_session, employee_id="EMP-5150", password=PASSWORD)

    wrong_password = _login(client, "EMP-5150", WRONG_PASSWORD)
    unknown_badge = _login(client, "EMP-9999", PASSWORD)

    assert wrong_password.status_code == unknown_badge.status_code == status.HTTP_401_UNAUTHORIZED
    assert wrong_password.json() == unknown_badge.json(), "the 401 distinguishes a real badge from a fake one"
    assert wrong_password.json()["detail"] == BADGE_401
    assert "access_token" not in wrong_password.text


def test_an_inactive_badge_account_is_refused(client: TestClient, db_session: Session):
    """A pending (unapproved) badge registrant cannot sign in with their password either.

    Public registration creates accounts inactive by design, so this is the state every
    badge-only signup starts in; a route that authenticated them would turn the approval
    step into decoration.
    """
    make_user(db_session, employee_id="EMP-6060", is_active=False)

    response = _login(client, "EMP-6060")

    assert response.status_code == status.HTTP_403_FORBIDDEN, response.text
    assert response.json()["detail"] == "User account is disabled"
    assert "access_token" not in response.text


# ===========================================================================
# 3. The email path is unchanged
# ===========================================================================


def test_an_input_containing_an_at_sign_still_takes_the_email_path(client: TestClient, db_session: Session):
    """The split is on ``"@"`` and nothing else, and the email answers are pinned to their
    exact pre-existing strings -- a login screen, a saved credential and an E2E assertion
    all read them."""
    make_user(db_session, email="office.person@wercomfg.com")

    unknown = _login(client, "nobody-at-all@wercomfg.com")
    assert unknown.status_code == status.HTTP_401_UNAUTHORIZED, unknown.text
    assert unknown.json()["detail"] == EMAIL_401

    wrong_password = _login(client, "office.person@wercomfg.com", WRONG_PASSWORD)
    assert wrong_password.status_code == status.HTTP_401_UNAUTHORIZED
    assert wrong_password.json()["detail"] == EMAIL_401

    good = _login(client, "office.person@wercomfg.com")
    assert good.status_code == status.HTTP_200_OK, good.text
    assert good.json()["access_token"]


def test_the_two_paths_answer_with_their_own_wording(client: TestClient, db_session: Session):
    """The two 401s differ from each other, which is deliberate and is NOT a leak: the
    wording describes what the CALLER submitted, which they already know. What must never
    differ is two answers within one path (§2)."""
    unknown_email = _login(client, "nobody@wercomfg.com")
    unknown_badge = _login(client, "NOBODY")

    assert unknown_email.json()["detail"] == EMAIL_401
    assert unknown_badge.json()["detail"] == BADGE_401


def test_an_ordinary_address_at_a_real_domain_never_feeds_the_throttle(client: TestClient, db_session: Session):
    """The boundary the throttle draws, stated as ENUMERABILITY (see §5).

    An address at a domain this system does not own is not enumerable the way a
    four-digit badge is, so it deliberately does not count -- on either outcome. Were it
    wired in, a shared office NAT with one person fat-fingering their password would take
    the whole floor's badge login offline for fifteen minutes: a self-inflicted outage on
    the path the shop depends on, bought in exchange for nothing.

    Both directions are asserted, because "does not count" is a claim about the SHARED
    counter and only one direction of it is visible from a single check: a real-domain
    failure must not throttle the badge path, AND it must not throttle the minted-address
    path either (that one is new -- a minted address IS counted, so a fix that keyed the
    exemption off "no user was found" rather than off the submitted domain would leak
    here).
    """
    for _ in range(PASSWORD_LOGIN_MAX_FAILURES + 2):
        assert _login(client, "nobody@wercomfg.com").status_code == status.HTTP_401_UNAUTHORIZED

    # Neither enumerable spelling is blocked from the same IP -- ordinary 401, not 429.
    badge_attempt = _login(client, "NOBODY")
    assert badge_attempt.status_code == status.HTTP_401_UNAUTHORIZED, badge_attempt.text
    minted_attempt = _login(client, f"emp-nobody@{SYNTHETIC_EMAIL_DOMAIN}")
    assert minted_attempt.status_code == status.HTTP_401_UNAUTHORIZED, minted_attempt.text


# ===========================================================================
# 4. The account lockout still engages -- and it reaches the kiosk
# ===========================================================================


def test_five_failed_badge_attempts_lock_the_account_and_the_kiosk_with_it(client: TestClient, db_session: Session):
    """The coupling, pinned because it is a real operational consequence.

    Five failed passwords on THIS route set ``locked_until`` on the user row, and
    ``/auth/employee-login`` reads the same column -- so an operator who mistypes at the
    office keyboard cannot then badge in at the kiosk for thirty minutes. That is the
    intended CMMC behavior, not a defect, but it is exactly why the badge path also needs
    the per-IP throttle in §5: without it, one IP could take an arbitrary number of
    operators off the floor by guessing four-digit badges.
    """
    user = make_user(db_session, employee_id="EMP-7007")

    for attempt in range(5):
        response = _login(client, "EMP-7007", WRONG_PASSWORD)
        assert response.status_code == status.HTTP_401_UNAUTHORIZED, f"attempt {attempt}: {response.text}"

    db_session.expire_all()
    locked = db_session.get(User, user.id)
    assert locked.failed_login_attempts >= 5
    assert locked.locked_until is not None

    # The CORRECT password is now refused too -- a lock, not a counter.
    with_right_password = _login(client, "EMP-7007")
    assert with_right_password.status_code == status.HTTP_403_FORBIDDEN, with_right_password.text
    assert "locked" in with_right_password.json()["detail"].lower()

    # ...and so is the kiosk badge door, which shares the column.
    kiosk = client.post(EMPLOYEE_LOGIN_URL, json={"employee_id": "EMP-7007"})
    assert kiosk.status_code == status.HTTP_403_FORBIDDEN, kiosk.text
    assert "locked" in kiosk.json()["detail"].lower()


# ===========================================================================
# 5. The per-IP failed-attempt throttle -- keyed on ENUMERABILITY, not on "@"
# ===========================================================================
#
# THE BYPASS THIS SECTION EXISTS TO KEEP CLOSED. The throttle was originally gated on
# "did the submitted identifier contain an ``@``", on the rationale that an address space
# is not enumerable the way a four-digit badge is. That rationale is false for exactly the
# population the throttle protects. A badge-only or CSV-imported user HAS no real address,
# so the system MINTED one from the badge -- ``emp-<sanitized-badge>@users.werco.com``
# (``services/user_identity``) -- and ``_find_user_by_auth_email`` resolves it exactly,
# legacy ``@werco.local`` rows included. ``emp-0000@…`` through ``emp-9999@…`` therefore
# reaches the same accounts as sweeping badges 0000-9999, drives the same 5-failure
# ACCOUNT lockout, and was not throttled at all: the attacker types one extra character.
#
# The gate is now ``(not is_email_login) or is_synthetic_email(submitted)``, decided from
# the SUBMITTED STRING ALONE so it can still run above the user lookup. The tests below
# assert the bypass path directly, assert the two spellings of ONE account share a single
# budget (so they cannot be played off against each other), and assert an ordinary address
# at a real domain is still exempt -- that exemption is not laziness, it is what keeps one
# person mistyping their password on a shared office NAT from taking the floor offline.


def test_the_badge_path_is_throttled_per_ip_after_the_configured_failures(client: TestClient, db_session: Session):
    """N failures from one IP, then 429 with ``Retry-After`` -- and the block is checked
    BEFORE the user lookup, so a throttled IP does zero account probing. The last
    assertion proves that: a VALID badge and password are refused while blocked."""
    make_user(db_session, employee_id="EMP-8080")

    for attempt in range(PASSWORD_LOGIN_MAX_FAILURES):
        response = _login(client, f"NOBODY-{attempt}")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED, f"attempt {attempt}: {response.text}"

    _assert_throttled(_login(client, "NOBODY-LAST"))

    blocked_but_valid = _login(client, "EMP-8080")
    assert blocked_but_valid.status_code == status.HTTP_429_TOO_MANY_REQUESTS, blocked_but_valid.text
    assert "access_token" not in blocked_but_valid.text

    rows = committed_audit_rows(db_session, "LOGIN_BLOCKED")
    throttled = [row for row in rows if (row.error_message or "").startswith("Throttled")]
    assert throttled, "a throttled rejection must be audited"
    assert throttled[0].success == "false"


def test_a_successful_badge_login_does_not_count_toward_the_throttle(client: TestClient, db_session: Session):
    """Successes are never counted -- a shift change cycling badges correctly must stay
    fast. Proven by ARITHMETIC, not by a green success: N-1 failures, a success, then one
    more failure that is still answered 401 (it is the Nth, so the success cannot have
    been counted) and only THEN does the next attempt trip."""
    make_user(db_session, employee_id="EMP-9090")

    for attempt in range(PASSWORD_LOGIN_MAX_FAILURES - 1):
        assert _login(client, f"NOBODY-{attempt}").status_code == status.HTTP_401_UNAUTHORIZED

    success = _login(client, "EMP-9090")
    assert success.status_code == status.HTTP_200_OK, success.text

    # Still only N-1 failures on the books: this one is answered, not blocked...
    assert _login(client, "NOBODY-FINAL").status_code == status.HTTP_401_UNAUTHORIZED
    # ...and it is the Nth, which arms the block for the attempt after it.
    _assert_throttled(_login(client, "NOBODY-AFTER"))


def _minted(badge: str) -> str:
    """The address this system mints for a badge-only account (``user_identity``)."""
    return f"emp-{badge.lower()}@{SYNTHETIC_EMAIL_DOMAIN}"


def _fail_until_locked(client: TestClient, db: Session, user: User, identifier: str) -> int:
    """Wrong-password attempts until this account locks itself; returns how many it took.

    The lockout threshold is a bare ``5`` inside ``login()`` with no constant to import, so
    it is DERIVED here by driving the account rather than written down. A test that hard-
    coded 5 would keep passing, meaninglessly, if the lockout were ever retuned -- and the
    budget-sizing property below is a relationship BETWEEN the two numbers, so it has to
    read the real one.
    """
    for count in range(1, PASSWORD_LOGIN_MAX_FAILURES + 1):
        response = _login(client, identifier, WRONG_PASSWORD)
        assert response.status_code == status.HTTP_401_UNAUTHORIZED, f"attempt {count}: {response.text}"
        db.expire_all()
        if db.get(User, user.id).locked_until is not None:
            return count
    raise AssertionError("the account never locked within one IP budget -- the lockout is not engaging")


def test_a_sweep_of_MINTED_addresses_is_counted_and_the_ip_is_cut_off(client: TestClient, db_session: Session):
    """THE BYPASS, written as the regression test.

    Badge-only accounts attacked through the addresses the SYSTEM minted for them -- every
    request on this path carries an ``"@"``, so under the old "email means not enumerable"
    gate none of it counted, the sweep was bounded only by the 5/min per-path limit, and it
    ran to as many account lockouts as the attacker had patience for.

    THE CLAIM IS THE SHAPE, NOT A NUMBER, and that distinction is why this test was
    rewritten rather than re-tuned. It used to assert that the budget ran out before a
    SECOND account could be locked, which was true at a budget of 8 and is deliberately
    false at 60: the budget is now sized from the other end, so that ten users behind one
    NAT egress can each reach their own lockout without taking the floor's login offline
    (see ``PASSWORD_LOGIN_MAX_FAILURES``, and §5B for that property asserted directly).
    Encoding the old number here would have quietly re-argued a sizing decision from a
    test file.

    What must hold, and is asserted: a minted address COUNTS, the budget is finite, and
    once it is gone the IP is refused ABOVE the user lookup -- so the next account in the
    sweep is never probed at all. Account A is driven to its own lockout honestly; the rest
    of the budget is spent on addresses that resolve to nobody, so the test does not pay
    bcrypt sixty times to assert a counter.
    """
    locked_target = make_user(db_session, employee_id="EMP-0001", email=_minted("EMP-0001"))
    protected_target = make_user(db_session, employee_id="EMP-0002", email=_minted("EMP-0002"))

    spent = _fail_until_locked(client, db_session, locked_target, _minted("EMP-0001"))
    assert spent < PASSWORD_LOGIN_MAX_FAILURES, "precondition: one account's lockout cannot exhaust the IP budget"

    for attempt in range(spent, PASSWORD_LOGIN_MAX_FAILURES):
        response = _login(client, _minted(f"nobody-{attempt}"), WRONG_PASSWORD)
        assert response.status_code == status.HTTP_401_UNAUTHORIZED, f"attempt {attempt}: {response.text}"

    # The budget is spent: every further attempt from this IP is refused before any lookup.
    _assert_throttled(_login(client, _minted("EMP-0002"), WRONG_PASSWORD))

    db_session.expire_all()
    assert db_session.get(User, locked_target.id).locked_until is not None, "precondition: A really did lock"
    survivor = db_session.get(User, protected_target.id)
    assert survivor.failed_login_attempts == 0, "the throttle must refuse before the next account is even probed"
    assert survivor.locked_until is None

    rows = committed_audit_rows(db_session, "LOGIN_BLOCKED")
    throttled = [row for row in rows if (row.error_message or "").startswith("Throttled")]
    assert throttled, "a throttled rejection on a minted address must be audited"
    assert throttled[0].success == "false"


def test_the_legacy_reserved_domain_is_throttled_too(client: TestClient, db_session: Session):
    """``@werco.local`` is the same keyspace wearing an older name.

    ``is_synthetic_email`` covers both minted domains, and it has to: a legacy import only
    stops carrying ``@werco.local`` after its first successful login, when
    ``_ensure_valid_auth_email`` rewrites it. Those rows -- the ones nobody has signed into
    yet -- are exactly the accounts an operator would not notice being swept. Their local
    part is derived from the badge just the same, so the address is just as guessable.
    """
    for attempt in range(PASSWORD_LOGIN_MAX_FAILURES):
        response = _login(client, f"emp-{attempt:04d}@{LEGACY_RESERVED_EMAIL_DOMAIN}")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED, f"attempt {attempt}: {response.text}"

    _assert_throttled(_login(client, f"emp-9999@{LEGACY_RESERVED_EMAIL_DOMAIN}"))


def test_the_badge_and_its_minted_address_share_one_budget(client: TestClient, db_session: Session):
    """One account, two spellings -- and they must not be playable off against each other.

    A badge-only account is reachable as ``EMP-0055`` and as
    ``emp-emp-0055@users.werco.com``. If each spelling had its own counter (or if one were
    exempt), an attacker would simply alternate and get double the budget against the same
    lockout. Proven by arithmetic across the two forms: four failures spelled one way and
    four spelled the other exhaust ONE eight-failure budget, and then BOTH spellings are
    refused -- the second assertion is what fails if the exemption is ever reintroduced
    for either form.
    """
    make_user(db_session, employee_id="EMP-0055", email=_minted("emp-EMP-0055"))

    for attempt in range(PASSWORD_LOGIN_MAX_FAILURES // 2):
        assert _login(client, f"NOBODY-{attempt}").status_code == status.HTTP_401_UNAUTHORIZED
    for attempt in range(PASSWORD_LOGIN_MAX_FAILURES // 2):
        assert _login(client, _minted(f"nobody-{attempt}")).status_code == status.HTTP_401_UNAUTHORIZED

    _assert_throttled(_login(client, "EMP-0055"))
    _assert_throttled(_login(client, _minted("emp-EMP-0055")))


def test_an_ordinary_address_failure_does_not_advance_the_enumerable_budget(client: TestClient, db_session: Session):
    """The exemption, proven by ARITHMETIC rather than by one unthrottled request.

    Seven minted-address failures, then a pile of real-domain ones, then the EIGHTH minted
    failure -- still answered 401, which it could not be if the real-domain attempts had
    been counted. Only the ninth trips. That ordering is the whole assertion: a test that
    merely showed a real-domain 401 after the block engaged would pass against a build
    that counted everything.
    """
    for attempt in range(PASSWORD_LOGIN_MAX_FAILURES - 1):
        assert _login(client, _minted(f"nobody-{attempt}")).status_code == status.HTTP_401_UNAUTHORIZED

    for attempt in range(5):
        assert _login(client, f"office-{attempt}@wercomfg.com").status_code == status.HTTP_401_UNAUTHORIZED

    # Still only 7 enumerable failures on the books: this is the 8th, answered not blocked...
    assert _login(client, _minted("nobody-final")).status_code == status.HTTP_401_UNAUTHORIZED
    # ...and it is what arms the block for the next one.
    _assert_throttled(_login(client, _minted("nobody-after")))


def test_a_throttled_ip_does_zero_account_probing_even_with_correct_credentials(
    client: TestClient, db_session: Session
):
    """The block is checked ABOVE the user lookup, asserted on the DATABASE and not only
    on the status code.

    A valid badge with its valid password is refused 429 while the IP is blocked -- and
    the account's ``failed_login_attempts`` is UNCHANGED afterwards. That second half is
    the real claim: a successful login resets the counter to zero and a failed one
    increments it, so a counter that neither moved nor cleared proves the handler never
    read or wrote the row. If the throttle check ever slid below the lookup, an attacker
    could still probe (and still move a stranger's lockout counter) from a blocked IP,
    while every status-code assertion in this file stayed green.
    """
    user = make_user(db_session, employee_id="EMP-0606")
    user.failed_login_attempts = 3  # mid-lockout, so both directions of movement are visible
    db_session.commit()

    for attempt in range(PASSWORD_LOGIN_MAX_FAILURES):
        assert _login(client, _minted(f"nobody-{attempt}")).status_code == status.HTTP_401_UNAUTHORIZED

    blocked = _login(client, "EMP-0606", PASSWORD)
    assert blocked.status_code == status.HTTP_429_TOO_MANY_REQUESTS, blocked.text
    assert "access_token" not in blocked.text

    db_session.expire_all()
    untouched = db_session.get(User, user.id)
    assert untouched.failed_login_attempts == 3, "the row was read/written from a blocked IP"
    assert untouched.locked_until is None
    assert committed_audit_rows(db_session, "LOGIN_SUCCESS") == []


# ===========================================================================
# 5B. The two routes hold SEPARATE budgets -- neither can starve the other
# ===========================================================================
#
# THE MONDAY-MORNING OUTAGE, and the reason this section exists rather than a comment.
# ``/auth/login`` and ``/auth/employee-login`` briefly shared ONE ``FailedLoginThrottle``
# instance. They must not, for two independent reasons:
#
#   * ``/auth/login`` counts WRONG-PASSWORD failures -- an outcome the passwordless badge
#     route cannot even produce. The kiosk budget is sized on the premise that a failure
#     is an UNKNOWN BADGE, which is rare; a mistyped password is not. Shared, ordinary
#     office typos drain the kiosk's budget, and an empty kiosk budget answers 429 to
#     BADGE SIGN-IN for every operator behind that egress IP for the whole cooldown, with
#     no admin reset and no way for the floor to tell what happened. The login screen
#     actively steers badge-only operators onto the password path, so both routes see the
#     same people from the same IP all day.
#   * The right budget differs. One legitimate user can spend a full account-lockout's
#     worth of failures on ``/auth/login`` before their own lockout stops them, so a
#     budget sized for rare unknown badges cannot absorb two of them.
#
# The tests assert the independence in BOTH directions, then assert each route still
# throttles ITSELF -- because "they do not interfere" is trivially satisfied by a build
# that throttles nothing, and the fix that separates the counters is one line away from
# the fix that removes one.


def _exhaust_password_route(client: TestClient) -> None:
    """Spend ``/auth/login``'s whole per-IP budget on enumerable identifiers.

    The assertion message names the shared-counter hypothesis on purpose: if the routes are
    ever re-collapsed onto one instance, this loop is where it surfaces first (the kiosk
    budget is far smaller), and "attempt 8 returned 429" is otherwise a mystifying failure.
    """
    for attempt in range(PASSWORD_LOGIN_MAX_FAILURES):
        response = _login(client, f"NOBODY-{attempt}")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED, (
            f"attempt {attempt} of {PASSWORD_LOGIN_MAX_FAILURES} answered {response.status_code}: /auth/login ran "
            f"out of budget early, i.e. it is spending some other route's counter. {response.text}"
        )


def _employee_login(client: TestClient, employee_id: str):
    """One ``/auth/employee-login`` attempt with a fresh per-path rate-limit budget.

    That route allows 10/min and these tests need more than that to reach the FAILED-
    attempt throttle behind it -- same reason ``_login`` resets the counter (module
    docstring). The 10/min limit has its own coverage in ``test_auth_rate_limit.py``.
    """
    _reset_path_limiter()
    return client.post(EMPLOYEE_LOGIN_URL, json={"employee_id": employee_id})


def _exhaust_employee_route(client: TestClient) -> None:
    """Spend ``/auth/employee-login``'s whole per-IP budget on unknown badges.

    The badges are ``9xxx``: outside the ``71xx``/``BADGELOGIN`` bands this file seeds, so
    they resolve to nobody no matter what else a test has created.
    """
    for attempt in range(EMPLOYEE_LOGIN_MAX_FAILURES):
        response = _employee_login(client, f"9{attempt:03d}")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED, f"attempt {attempt}: {response.text}"


def test_exhausting_the_password_route_leaves_the_kiosk_badge_route_serving(client: TestClient, db_session: Session):
    """THE REGRESSION: a floor that cannot badge in because the office mistyped passwords.

    ``/auth/login``'s budget is spent from one IP, that route is confirmed blocked, and
    then a REAL badge is scanned at ``/auth/employee-login`` from the SAME IP. It must sign
    in. While the two routes shared one counter this answered 429 -- every operator behind
    the shop's egress IP locked out of the kiosk for the cooldown, with the only visible
    symptom being badges that stopped working at shift change.

    The kiosk sign-in is asserted all the way to the resolved USER, not merely to a 200:
    the outage being guarded against is a specific person unable to start their shift.
    """
    operator = make_user(db_session, employee_id="EMP-7001")

    _exhaust_password_route(client)
    _assert_throttled(_login(client, "NOBODY-LAST"))

    badge_in = _employee_login(client, "EMP-7001")

    assert badge_in.status_code == status.HTTP_200_OK, badge_in.text
    assert badge_in.json()["access_token"]
    assert badge_in.json()["user"]["id"] == operator.id


def test_exhausting_the_kiosk_route_leaves_the_password_route_serving(client: TestClient, db_session: Session):
    """The converse, which is not implied by the first and fails for a different reason.

    A shared counter starves BOTH ways: a badge scanner left face-down on a bench, or one
    operator repeatedly scanning a decommissioned badge, would drain the kiosk budget and
    -- shared -- take the OFFICE off ``/auth/login`` at the same IP. Asserted with a badge
    and password rather than an address, because that is the credential a badge-only user
    has and therefore the sign-in that would actually be lost.
    """
    operator = make_user(db_session, employee_id="EMP-7002")

    _exhaust_employee_route(client)
    _assert_throttled(_employee_login(client, "9999"), cooldown_seconds=EMPLOYEE_LOGIN_COOLDOWN_SECONDS)

    signed_in = _login(client, "EMP-7002")

    assert signed_in.status_code == status.HTTP_200_OK, signed_in.text
    assert signed_in.json()["user"]["id"] == operator.id


def test_each_route_still_throttles_itself_after_the_split(client: TestClient, db_session: Session):
    """NEITHER CONTROL WAS REMOVED -- the claim the two tests above cannot make.

    "The routes do not interfere" is satisfied perfectly by a build that throttles nothing
    at all, and separating the counters is one line away from deleting one of them. So both
    budgets are spent in the same test, from the same IP, and both routes are asserted to
    refuse afterwards -- each with its OWN cooldown bound, which is the second half of the
    separation (one shared instance would also mean one shared cooldown).
    """
    _exhaust_password_route(client)
    _assert_throttled(_login(client, "NOBODY-LAST"))

    # The kiosk budget is untouched, so all of these are still answered...
    _exhaust_employee_route(client)
    # ...and only now is the kiosk blocked too, on its own counter and its own cooldown.
    _assert_throttled(_employee_login(client, "9999"), cooldown_seconds=EMPLOYEE_LOGIN_COOLDOWN_SECONDS)


def test_two_users_reaching_their_own_lockout_do_not_trip_the_shared_ip_throttle(
    client: TestClient, db_session: Session
):
    """The LOWER BOUND on the password budget, asserted as the property rather than as 60.

    Everyone behind one NAT egress shares a client IP. Each user can contribute at most an
    account-lockout's worth of failures before their own account stops them, so the per-IP
    budget must absorb several such users or an honest bad morning takes the floor's login
    offline -- a self-inflicted outage bought in exchange for nothing.

    Both users are attacked through their MINTED addresses, i.e. the enumerable spelling
    that the throttle does count, so this is the worst case rather than a convenient one.
    Neither number is written down: the lockout threshold is derived by driving an account
    (``_fail_until_locked``) and the headroom is asserted against
    ``PASSWORD_LOGIN_MAX_FAILURES``, so re-tuning the budget downward fails HERE instead of
    silently breaking the property it was tuned to satisfy.
    """
    first = make_user(db_session, employee_id="EMP-7011", email=_minted("EMP-7011"))
    second = make_user(db_session, employee_id="EMP-7012", email=_minted("EMP-7012"))

    spent_first = _fail_until_locked(client, db_session, first, _minted("EMP-7011"))
    spent_second = _fail_until_locked(client, db_session, second, _minted("EMP-7012"))

    assert spent_first == spent_second, "precondition: the lockout threshold is per ACCOUNT, not per IP"
    assert spent_first + spent_second <= PASSWORD_LOGIN_MAX_FAILURES, (
        f"the per-IP budget ({PASSWORD_LOGIN_MAX_FAILURES}) cannot absorb two NAT-shared users each reaching "
        f"their own {spent_first}-failure account lockout"
    )

    # The IP is NOT blocked: a third person behind the same NAT still gets an ordinary
    # answer, not a 429 caused by two colleagues' bad morning.
    third = _login(client, "NOBODY-THIRD")
    assert third.status_code == status.HTTP_401_UNAUTHORIZED, third.text

    db_session.expire_all()
    for locked in (first, second):
        assert db_session.get(User, locked.id).locked_until is not None, "precondition: both accounts really locked"


def test_the_two_routes_hold_separate_counters_under_separate_key_prefixes():
    """The mechanism itself, pinned so a re-collapse fails with a one-line diagnosis.

    Distinct OBJECTS is not sufficient and asserting only that would be a false comfort:
    two instances sharing a KEY PREFIX share the Redis key, which is the same outage
    wearing a different shape -- and one this suite could never observe behaviorally,
    because in memory mode (which is what these tests force) each instance owns its own
    dict and the collision simply does not happen. Production runs on Redis.
    """
    assert password_login_throttle is not employee_login_throttle, "the two routes share one counter again"
    assert password_login_throttle._key_prefix != employee_login_throttle._key_prefix, (
        "separate instances sharing a key prefix share the Redis key in production, "
        "which is the shared-counter outage with none of the local symptoms"
    )


# ===========================================================================
# 5C. The 429 BODY names the wait THIS route's cooldown implies
# ===========================================================================
#
# ``Login.tsx`` renders the server's ``detail`` verbatim, so this string IS the entire
# explanation a blocked person gets -- there is no second screen and no support link. It
# used to read "wait a few minutes", copied from the kiosk route where it is TRUE (that
# cooldown really is 15 minutes) and where, being a machine-to-machine 429, it was never
# rendered to anybody. On ``/auth/login`` the block runs a full hour, so the copy sent an
# operator back to the form every few minutes for an hour: the control was working exactly
# as designed and the app read as broken, which is how a correct refusal turns into a
# support call and then into somebody asking for the throttle to be turned off.
#
# The wait is now DERIVED from the ``Retry-After`` the same response carries, so the copy
# cannot drift from ``PASSWORD_LOGIN_COOLDOWN_SECONDS`` when that constant is retuned -- and
# it is expected to be retuned; login_throttle.py names raising it as the next lever if
# sweep resistance is ever worth more than the recovery time. THESE TESTS THEREFORE COMPUTE
# THE EXPECTED WAIT FROM THE CONSTANT AND FROM THE HEADER AND NEVER WRITE "60": a test
# carrying that literal would have to be edited in lockstep with the very change it exists
# to catch, which makes it a speed bump rather than a guard.
#
# The kiosk route keeps its own body, and that is asserted here rather than left implied --
# "make the copy honest" is one careless sed away from rewording the route whose fifteen
# minutes genuinely are a few minutes.

# The kiosk route's 429 body, pinned as the literal it must remain.
KIOSK_THROTTLE_DETAIL = "Too many failed sign-in attempts — wait a few minutes"

_WAIT_MINUTES = re.compile(r"try again in about (?P<minutes>\d+) (?P<unit>minutes?)\b")


def _hold_the_password_block_with(monkeypatch, seconds_remaining: int) -> None:
    """Step the throttle's OWN clock forward so its live block has that much time left.

    Uses the ``_now`` seam the class documents as the test hook, rather than reaching into
    the memory store: the store is keyed by client IP, and a test that wrote that key would
    silently stop exercising anything the day the key derivation changed.

    Call it AFTER the block has been tripped -- ``register_failure`` reads the same seam, so
    shifting first would move the expiry it writes along with the clock and change nothing.
    """
    real_now = password_login_throttle._now
    shift = PASSWORD_LOGIN_COOLDOWN_SECONDS - seconds_remaining
    monkeypatch.setattr(password_login_throttle, "_now", lambda: real_now() + shift)


def test_the_password_route_429_names_the_wait_its_own_cooldown_implies(client: TestClient, db_session: Session):
    """The body a blocked person actually reads, checked against the block they are under.

    Two independent consistency claims, because either one alone can be satisfied by a
    wrong build: the stated wait must agree with the ``Retry-After`` on the SAME response
    (a body derived from some other quantity fails here), and it must agree with
    ``PASSWORD_LOGIN_COOLDOWN_SECONDS`` (a body derived correctly from the KIOSK throttle's
    remaining time would pass the first and fail this one). The one-minute tolerance on the
    second is test-execution time, not slack in the contract.
    """
    _exhaust_password_route(client)

    blocked = _login(client, "NOBODY-LAST")
    assert blocked.status_code == status.HTTP_429_TOO_MANY_REQUESTS, blocked.text

    detail = blocked.json()["detail"]
    retry_after = int(blocked.headers["Retry-After"])
    match = _WAIT_MINUTES.search(detail)
    assert match, f"the body must NAME a wait, not gesture at one: {detail!r}"
    minutes = int(match.group("minutes"))

    assert minutes == max(1, round(retry_after / 60)), (
        f"the body says {minutes} minute(s) but this same response's Retry-After is "
        f"{retry_after}s -- the copy is not derived from the block it describes"
    )
    assert abs(minutes - PASSWORD_LOGIN_COOLDOWN_SECONDS / 60) <= 1, (
        f"the body says {minutes} minute(s) against a cooldown of "
        f"{PASSWORD_LOGIN_COOLDOWN_SECONDS}s -- it is describing some other route's block"
    )

    assert "a few minutes" not in detail, "the kiosk's wording is back on the hour-long block"
    assert detail != KIOSK_THROTTLE_DETAIL
    assert "Badge sign-in at the kiosk is unaffected." in detail, (
        "the body must also say what still works: the kiosk holds a separate budget (§5B), "
        "so an operator turned away here can still start their shift at the station"
    )


def test_the_kiosk_429_body_is_unchanged_and_states_no_derived_wait(client: TestClient, db_session: Session):
    """``/auth/employee-login`` keeps its own copy, because its cooldown really is short.

    Asserted as the exact literal on purpose. The two bodies differ BECAUSE the two
    cooldowns differ (§5B), so the same edit that made the password route honest is the one
    that would flatten this one back onto shared wording -- and nobody would notice, since
    this 429 is answered to a kiosk rather than to a person.
    """
    assert (
        EMPLOYEE_LOGIN_COOLDOWN_SECONDS != PASSWORD_LOGIN_COOLDOWN_SECONDS
    ), "precondition: the two bodies are allowed to differ only because the two blocks do"

    _exhaust_employee_route(client)

    blocked = _employee_login(client, "9999")
    assert blocked.status_code == status.HTTP_429_TOO_MANY_REQUESTS, blocked.text

    detail = blocked.json()["detail"]
    assert detail == KIOSK_THROTTLE_DETAIL, "the kiosk body changed; its cooldown did not"
    assert _WAIT_MINUTES.search(detail) is None, "this route states no derived wait -- it never did"

    retry_after = int(blocked.headers["Retry-After"])
    assert 1 <= retry_after <= EMPLOYEE_LOGIN_COOLDOWN_SECONDS


@pytest.mark.parametrize(
    "seconds_remaining, expected_phrase",
    [
        # Under a minute left: the wait rounds to zero and is floored at one, which is the
        # only place the singular is ever produced -- and the only place "1 minutes" can be.
        (30, "try again in about 1 minute."),
        (120, "try again in about 2 minutes."),
    ],
)
def test_the_stated_wait_agrees_with_itself_at_the_singular_boundary(
    client: TestClient, db_session: Session, monkeypatch, seconds_remaining: int, expected_phrase: str
):
    """The pluralization, at the only boundary a derived number can get wrong.

    A wait computed from a live clock spends its last minute at 1, so "1 minutes" is not a
    hypothetical -- it is what the last sixty seconds of every block would read like under a
    hard-coded ``s``. Cheap to get right, and it lands on the screen of somebody who is
    already convinced the app is broken.

    The bottom case also pins the FLOOR: the remaining time rounds to zero minutes there,
    and "try again in about 0 minutes" would be worse than saying nothing.
    """
    _exhaust_password_route(client)
    _hold_the_password_block_with(monkeypatch, seconds_remaining)

    blocked = _login(client, "NOBODY-LAST")
    assert blocked.status_code == status.HTTP_429_TOO_MANY_REQUESTS, blocked.text

    detail = blocked.json()["detail"]
    assert expected_phrase in detail, detail
    assert "1 minutes" not in detail
    assert "0 minute" not in detail, "a floored wait must never round down to nothing"


# ===========================================================================
# 6. Ambiguity refuses instead of guessing
# ===========================================================================


def test_a_badge_held_in_two_companies_refuses_409(client: TestClient, db_session: Session):
    """Employee IDs are unique PER COMPANY (``uq_users_company_employee_id``) and this
    route is install-wide and unauthenticated, so the same badge can legitimately exist
    twice. Authenticating an arbitrary one would land the person in a nondeterministic
    tenant -- the defect ``test_auth_identity_resolution.py`` pins for addresses, reaching
    the badge path with this feature.
    """
    user_a = make_user(db_session, company_id=COMPANY_A, employee_id="SHARED-01", password=PASSWORD)
    user_b = make_user(db_session, company_id=COMPANY_B, employee_id="SHARED-01", password=WRONG_PASSWORD)

    response = _login(client, "SHARED-01")

    assert response.status_code == status.HTTP_409_CONFLICT, response.text
    assert "access_token" not in response.text

    # Refused BEFORE verify_password, so neither bystander's lockout counter moved.
    db_session.expire_all()
    assert db_session.get(User, user_a.id).failed_login_attempts == 0
    assert db_session.get(User, user_b.id).failed_login_attempts == 0

    rows = committed_audit_rows(db_session, "LOGIN_BLOCKED")
    assert len(rows) == 1
    assert rows[0].error_message == "Employee ID resolves to more than one account"
    assert rows[0].resource_identifier == "employee-id:SHARED-01"
    assert rows[0].success == "false"


def test_an_ambiguous_badge_does_not_burn_the_ip_throttle_budget(client: TestClient, db_session: Session):
    """Deliberate, and easy to "fix" wrongly: an ambiguous badge is an ADMIN DATA problem
    and the account provably exists, so it is not a wrong guess. Counting it would let one
    duplicated row lock an IP out of a login that is failing through no fault of the person
    typing. ``/auth/employee-login`` treats its own 409 the same way.
    """
    make_user(db_session, company_id=COMPANY_A, employee_id="SHARED-02")
    make_user(db_session, company_id=COMPANY_B, employee_id="SHARED-02")

    for _ in range(PASSWORD_LOGIN_MAX_FAILURES + 1):
        assert _login(client, "SHARED-02").status_code == status.HTTP_409_CONFLICT

    # Still not throttled: an ordinary 401 is what an unknown badge gets here.
    assert _login(client, "NOBODY").status_code == status.HTTP_401_UNAUTHORIZED


# ===========================================================================
# 7. The badge resolver answers the same way twice, or refuses
# ===========================================================================
#
# The normalized fallback narrows in SQL before comparing in Python, and the narrowing is
# ``employee_id ILIKE '%<core>%'`` where ``core`` is the input's digits with leading zeros
# stripped -- ONE CHARACTER for any badge below 10 (``0001`` -> ``%1%``). On a real user
# table that matches a large fraction of every row, which is why the query is capped. Two
# separate jobs live in that cap and conflating them breaks the route in opposite
# directions:
#
#   * the LIMIT bounds a runaway query. Too LOW and ordinary badge logins start failing at
#     a realistic shop size -- an outage on the floor's own door, strictly worse than the
#     bug it would be fixing. That is why the first test below seeds a shop-sized table
#     rather than three users.
#   * the truncation 409 stops an incomplete window from masquerading as a unique answer.
#     Without it a genuine duplicate outside the window returns an arbitrary single row
#     and a genuine match outside it reads as "not found" -- on a path that verifies a
#     password and DRIVES a lockout, so resolving onto the wrong row moves a stranger's
#     counter and can lock a stranger's account in another tenant.
#
# ``order_by(User.id)`` is the third piece and it is not cosmetic: unordered, WHICH rows
# fall inside the window is whatever the query plan produces, so the same badge could
# resolve differently across two identical requests. SQLite will not demonstrate that on
# its own (the suite runs on in-memory SQLite; production is Postgres), so the tests below
# assert the ORDER of what comes back and the stability of repeated calls rather than
# pretending to reproduce a plan change.


def _seed_badge_noise(db: Session, count: int, *, company_id: int = COMPANY_A, start: int = 2000) -> None:
    """Bulk-seed users whose badges all match ``%1%`` but normalize AWAY from ``0001``.

    ``SHOP-1<n>`` for n = 2000, 2001, ... : every badge contains a ``1`` so it survives the
    SQL narrowing (that is the point -- these are the candidate window), while its four
    trailing digits are ``2000``, ``2001``, ... so none of them is a real match. They never
    authenticate, so they carry a literal hash rather than paying bcrypt 500 times.
    """
    _ensure_company(db, company_id)
    db.add_all(
        [
            User(
                email=f"noise-{company_id}-{n}@co{company_id}.example.com",
                employee_id=f"SHOP-1{n}",
                first_name="Noise",
                last_name=f"{n}",
                hashed_password="$2b$12$abcdefghijklmnopqrstuv",
                role=UserRole.OPERATOR,
                is_active=True,
                is_superuser=False,
                company_id=company_id,
                failed_login_attempts=0,
            )
            for n in range(start, start + count)
        ]
    )
    db.commit()


def test_a_real_badge_still_resolves_at_shop_size_on_the_single_character_core_path(
    client: TestClient, db_session: Session
):
    """THE OUTAGE GUARD, and the reason the cap is 500 and not 50.

    Badge ``0001`` narrows to ``%1%``, so at 130 users the candidate window is the whole
    table -- and a cap of 50 plus a truncation 409 would answer this perfectly ordinary
    login with "contact an administrator". Every operator whose badge starts with zeros
    would be off both login routes at once, on a shop far smaller than Werco.

    The window size is asserted directly, not implied, so the test states the condition it
    is protecting rather than merely exercising it.
    """
    _seed_badge_noise(db_session, 130)
    target = make_user(db_session, employee_id="EMP-00001")

    window = db_session.query(User).filter(User.employee_id.ilike("%1%")).count()
    assert window == 131, "precondition: the SQL narrowing matches the whole seeded table"
    assert window <= _EMPLOYEE_ID_CANDIDATE_CAP, "the cap must clear a realistic shop by a wide margin"

    response = _login(client, "0001")

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["user"]["id"] == target.id
    # The Python-side comparison is what picks the one real match out of the window.
    assert [u.id for u in _normalized_employee_id_matches(db_session, "0001")] == [target.id]


def test_the_cap_refuses_a_truncated_window_instead_of_answering_from_it(client: TestClient, db_session: Session):
    """The boundary, from BOTH sides, in one test so the edge is unambiguous.

    At exactly ``_EMPLOYEE_ID_CANDIDATE_CAP`` candidates the badge still resolves and the
    operator signs in. ONE more row -- a row that is not even a match, just noise inside
    the ``%1%`` narrowing -- and the resolver refuses, because at that point it can no
    longer tell whether the rows it did not look at contain a second match.

    The refusal is a 409 with its OWN wording. "Not unique" would be a claim the query
    never established, and it points an admin at the wrong remediation: there may well be
    no duplicate at all, only a table this lookup can no longer scan.
    """
    _seed_badge_noise(db_session, _EMPLOYEE_ID_CANDIDATE_CAP - 1)
    target = make_user(db_session, employee_id="EMP-00001")

    at_the_cap = _login(client, "0001")
    assert at_the_cap.status_code == status.HTTP_200_OK, at_the_cap.text
    assert at_the_cap.json()["user"]["id"] == target.id

    # One row past the cap: the window is now incomplete and the answer is withheld.
    _seed_badge_noise(db_session, 1, start=9000)

    over_the_cap = _login(client, "0001")
    assert over_the_cap.status_code == status.HTTP_409_CONFLICT, over_the_cap.text
    assert over_the_cap.json()["detail"] == _UNRESOLVABLE_EMPLOYEE_ID_DETAIL
    assert over_the_cap.json()["detail"] != _AMBIGUOUS_EMPLOYEE_ID_DETAIL, "truncation is not a duplicate"
    assert "access_token" not in over_the_cap.text


def test_a_genuine_normalized_duplicate_still_refuses_409(client: TestClient, db_session: Session):
    """The other 409, unchanged by the cap work.

    ``EMP-0339`` and ``00339`` are different strings, so both can exist (the unique
    constraint is on the exact value, per company) -- but they normalize to the same four
    digits, so a scan of ``339`` matches BOTH. Authenticating an arbitrary one would verify
    a password against a stranger's hash and move a stranger's lockout counter, which is
    what the refusal exists to prevent. Neither bystander's counter may move.
    """
    first = make_user(db_session, employee_id="EMP-0339")
    second = make_user(db_session, employee_id="00339")

    response = _login(client, "339")

    assert response.status_code == status.HTTP_409_CONFLICT, response.text
    assert response.json()["detail"] == _AMBIGUOUS_EMPLOYEE_ID_DETAIL
    assert "access_token" not in response.text

    db_session.expire_all()
    for bystander in (first, second):
        assert db_session.get(User, bystander.id).failed_login_attempts == 0


def test_the_candidate_window_comes_back_ordered_and_repeats_identically(db_session: Session):
    """Determinism, asserted on the helper because the endpoint can only show one row.

    Three rows normalizing to ``0001`` make the login 409 (above), so the ORDER of the
    window is invisible from HTTP -- yet order is exactly what decides which rows a
    truncated window would have contained. Asserted here instead: the list comes back in
    ``User.id`` order and two identical calls return identical lists.

    This cannot reproduce a Postgres plan change on in-memory SQLite, and it does not
    pretend to. What it pins is the contract a plan change would violate, so a future edit
    that drops ``order_by(User.id)`` fails a test that names the reason.
    """
    _seed_badge_noise(db_session, 20)
    matches = [make_user(db_session, employee_id=badge) for badge in ("EMP-00001", "00001", "0001")]

    first_call = _normalized_employee_id_matches(db_session, "0001")
    second_call = _normalized_employee_id_matches(db_session, "0001")

    ids = [u.id for u in first_call]
    assert ids == sorted(ids), "the window must be ordered by id, not by whatever the plan returns"
    assert ids == [u.id for u in second_call], "two identical calls returned different windows"
    assert sorted(ids) == sorted(u.id for u in matches)


def test_the_company_scoped_twin_shares_the_cap_and_fences_it_per_tenant(db_session: Session):
    """The kiosk/crew-station resolver is the SAME implementation, fenced to one company.

    It used to be a copy -- same ``limit(50)``, same missing ``order_by``, same silent
    resolve -- so the fix had to reach it or the badge doors would have started disagreeing
    with each other. Two claims here, and the second is the tenancy one: a foreign tenant's
    500 rows must not consume company A's window, or one noisy tenant could make every
    other tenant's kiosk refuse.
    """
    _seed_badge_noise(db_session, _EMPLOYEE_ID_CANDIDATE_CAP + 1, company_id=COMPANY_B)
    target = make_user(db_session, company_id=COMPANY_A, employee_id="EMP-00001")

    resolved = _find_user_by_employee_id_in_company(db_session, "0001", COMPANY_A)
    assert resolved is not None and resolved.id == target.id, "a foreign tenant's rows truncated our window"

    with pytest.raises(HTTPException) as excinfo:
        _find_user_by_employee_id_in_company(db_session, "0001", COMPANY_B)
    assert excinfo.value.status_code == status.HTTP_409_CONFLICT
    assert excinfo.value.detail == _UNRESOLVABLE_EMPLOYEE_ID_DETAIL


# ===========================================================================
# 8. ONE source for the placeholder domains -- the throttle cannot drift off the resolver
# ===========================================================================
#
# §5 throttles an address only when ``is_synthetic_email`` says it lies in an enumerable
# space, and that predicate is correct only while it describes the SAME set of addresses
# ``_find_user_by_auth_email`` can actually reach. The resolver maps a repaired
# ``@users.werco.com`` address back onto a legacy ``@werco.local`` row, so both domains are
# reachable, badge-derived and guessable -- and an address shape the resolver resolves but
# the predicate does not recognise is a fully enumerable identifier with NO per-IP bound on
# it. That is the bypass §5 exists to close, reopened by a one-line edit to one literal.
#
# Two strings that happen to be spelled the same are not agreement. These tests assert the
# two sites read ONE object, that no placeholder domain is spelled inline in ``auth.py`` at
# all, and that every domain the resolver interpolates is one the predicate recognises.
#
# The source checks walk the AST rather than grepping, which is not fussiness: several
# docstrings and comments legitimately NAME the domains for a human reader, and a grep
# would have to exclude them by hand (and would go blind the first time somebody reworded
# one). Comments are absent from the tree entirely, and docstrings are skipped explicitly.


def _auth_module_tree() -> ast.Module:
    return ast.parse(pathlib.Path(auth_module.__file__).read_text())


def _docstring_node_ids(tree: ast.Module) -> set:
    """``id()`` of every Constant that is a module/class/function docstring."""
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    ids.add(id(body[0].value))
    return ids


def test_the_resolver_and_the_throttle_read_the_domain_constants_from_one_module():
    """Same OBJECT, not merely the same spelling.

    ``auth.py`` imports both names from ``services/user_identity``; ``is_synthetic_email``
    builds its membership set from those same two names. An identity check is a cheap
    detector for a re-spelling (it fails immediately), and the AST guard below is what
    covers the direction identity cannot -- CPython may intern two equal literals into one
    object, so ``is`` passing is not by itself proof of a single source.
    """
    assert auth_module.SYNTHETIC_EMAIL_DOMAIN is user_identity.SYNTHETIC_EMAIL_DOMAIN
    assert auth_module.LEGACY_RESERVED_EMAIL_DOMAIN is user_identity.LEGACY_RESERVED_EMAIL_DOMAIN
    assert user_identity._UNDELIVERABLE_EMAIL_DOMAINS == {
        user_identity.SYNTHETIC_EMAIL_DOMAIN,
        user_identity.LEGACY_RESERVED_EMAIL_DOMAIN,
    }, "the throttle's enumerability set must be derived from the constants, not listed separately"


def test_no_placeholder_domain_is_spelled_inline_anywhere_in_the_auth_module():
    """There must be no SECOND literal for a future edit to change in isolation.

    This is the guard that makes the coupling structural instead of a convention. A
    developer replacing ``users.werco.com`` with a new domain has to change the constant --
    at which point ``is_synthetic_email`` follows automatically, because it reads the same
    constant. Reintroducing an inline literal here is precisely how the two would drift
    apart again, and it fails here rather than silently in production.
    """
    tree = _auth_module_tree()
    docstrings = _docstring_node_ids(tree)
    domains = (user_identity.SYNTHETIC_EMAIL_DOMAIN, user_identity.LEGACY_RESERVED_EMAIL_DOMAIN)

    offenders = sorted(
        {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and any(domain in node.value for domain in domains)
        }
    )

    assert offenders == [], (
        "a placeholder domain is spelled inline in auth.py instead of read from "
        f"services/user_identity: {offenders}. Two literals cannot be kept in agreement; "
        "the throttle's enumerability test derives from the constants, so an inline copy "
        "reopens the sweep bypass the moment one of them is edited."
    )


def test_every_domain_the_resolver_interpolates_is_one_the_throttle_recognises():
    """The pairing asserted from the RESOLVER's side, which the two tests above do not.

    They pin that the constants are shared and that nothing is spelled inline. Neither
    would notice a THIRD placeholder domain given its own constant and its own
    ``endswith`` branch in the resolver while ``_UNDELIVERABLE_EMAIL_DOMAINS`` stayed at
    two -- a reachable, badge-derived address shape with no throttle on it, i.e. the
    original bypass with a new name.

    So the two functions that resolve and repair placeholder addresses are read for the
    module-level string constants they interpolate, and each one must be a domain
    ``is_synthetic_email`` recognises. The empty-result assertion is deliberate: if the
    functions are ever rewritten to stop interpolating constants, this guard has gone blind
    and must fail rather than pass vacuously.
    """
    tree = _auth_module_tree()
    interpolated = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in ("_find_user_by_auth_email", "_ensure_valid_auth_email"):
            for inner in ast.walk(node):
                if isinstance(inner, ast.FormattedValue) and isinstance(inner.value, ast.Name):
                    value = getattr(auth_module, inner.value.id, None)
                    # Module-level string constants only -- locals like ``local_part``
                    # resolve to None here and are not domains.
                    if isinstance(value, str) and "." in value:
                        interpolated[inner.value.id] = value

    assert interpolated, "the resolver no longer interpolates any domain constant -- this guard has gone blind"
    for name, value in sorted(interpolated.items()):
        assert value in user_identity._UNDELIVERABLE_EMAIL_DOMAINS, (
            f"{name}={value!r} is reachable through the login resolver but is not in "
            "_UNDELIVERABLE_EMAIL_DOMAINS, so is_synthetic_email does not recognise it and "
            "/auth/login will not throttle a sweep of it"
        )
        assert user_identity.is_synthetic_email(f"emp-0001@{value}") is True


def test_a_lookalike_domain_is_neither_synthetic_nor_throttled(client: TestClient, db_session: Session):
    """The other side of the exact-match rule, driven through the route.

    ``bob@users.werco.com.example.org`` merely ENDS with a placeholder domain's words; it
    is an ordinary address at a real domain and mail genuinely reaches it. A substring test
    would classify it as enumerable and start counting real people's password typos against
    the shared per-IP budget -- the self-inflicted outage §5's exemption exists to avoid.
    """
    lookalike = f"bob@{SYNTHETIC_EMAIL_DOMAIN}.example.org"
    assert user_identity.is_synthetic_email(lookalike) is False

    for _ in range(PASSWORD_LOGIN_MAX_FAILURES + 2):
        assert _login(client, lookalike).status_code == status.HTTP_401_UNAUTHORIZED

    # None of it counted: the genuinely enumerable spellings are still served.
    assert _login(client, "NOBODY").status_code == status.HTTP_401_UNAUTHORIZED


# ===========================================================================
# 9. An over-length identifier is refused on SHAPE, before either lookup
# ===========================================================================
#
# ``limit_json_body_size`` (app/main.py) caps ``application/json`` bodies at 256 KB, and
# this route is FORM-encoded -- so ``/auth/login`` is the one unauthenticated endpoint that
# cap does not cover, and an arbitrarily long ``username`` reached the handler, where it was
# lowercased, regex-scanned by ``_normalize_employee_id`` and bound into a query against
# ``users``.
#
# The bound is ``MAX_LOGIN_IDENTIFIER_LENGTH``, the ``users.email`` column width, and that
# choice is what makes refusing SAFE rather than merely convenient: email is the widest
# identifier column any account can hold, so a longer value provably cannot match a stored
# row on either resolver. Nothing a lookup could have found is discarded.
#
# Three properties beyond "it is refused", each of which a plausible implementation gets
# wrong: the answer must be the ORDINARY 401 (an explicit "too long" is a new
# distinguishable outcome on an unauthenticated route); the refusal must NOT spend the
# per-IP budget (it establishes nothing about any account, so counting it would hand an
# attacker a CHEAPER lockout than the sweep the budget exists to bound); and the audit row
# must record a truncated, marked identifier that fits its own column.


def _over_length(prefix: str = "N") -> str:
    return prefix + "N" * MAX_LOGIN_IDENTIFIER_LENGTH


def test_an_over_length_identifier_answers_exactly_like_an_ordinary_bad_credential(
    client: TestClient, db_session: Session
):
    """The two responses are asserted EQUAL TO EACH OTHER, not to a literal.

    The claim is indistinguishability, so comparing each against a hard-coded string would
    still pass on a build where both had been changed to something distinguishable from
    each other. Both identifier shapes are covered, because the wording differs per shape
    (§3) and a refusal that answered with the wrong shape's wording would itself be a
    signal about how the input was parsed.
    """
    ordinary_badge = _login(client, "NOBODY")
    long_badge = _login(client, _over_length())
    assert (long_badge.status_code, long_badge.json()) == (ordinary_badge.status_code, ordinary_badge.json())
    assert long_badge.status_code == status.HTTP_401_UNAUTHORIZED, long_badge.text

    ordinary_email = _login(client, "nobody@wercomfg.com")
    long_email = _login(client, "n" * MAX_LOGIN_IDENTIFIER_LENGTH + "@wercomfg.com")
    assert (long_email.status_code, long_email.json()) == (ordinary_email.status_code, ordinary_email.json())
    assert long_email.status_code == status.HTTP_401_UNAUTHORIZED, long_email.text


def test_the_length_bound_is_the_widest_identifier_column_the_schema_can_hold():
    """Read off the MODELS, so a column widening that forgets this constant fails here.

    ``MAX_LOGIN_IDENTIFIER_LENGTH`` is only safe because nothing longer can match a stored
    row: widening ``users.email`` past it would start refusing addresses that DO exist --
    a lockout for real accounts, produced by a migration that looks unrelated. The audit
    column is checked too, because the refusal writes a row and a value that overran
    ``resource_identifier`` would raise out of the commit rather than refuse cleanly.
    """
    email_width = User.__table__.c.email.type.length
    badge_width = User.__table__.c.employee_id.type.length

    assert MAX_LOGIN_IDENTIFIER_LENGTH == max(email_width, badge_width) == email_width
    assert AuditLog.__table__.c.resource_identifier.type.length >= MAX_LOGIN_IDENTIFIER_LENGTH


def test_an_identifier_at_exactly_the_bound_still_reaches_the_lookup(client: TestClient, db_session: Session):
    """The boundary is ``>``, not ``>=``, asserted through the AUDIT ROW.

    Both outcomes are a 401 with the same body -- by design -- so the status code cannot
    tell the shape refusal from an ordinary miss. The recorded cause can: an identifier of
    exactly the maximum length must be looked up and recorded as "User not found", never as
    a shape refusal. Off-by-one here would refuse the longest legitimate address in the
    install.
    """
    at_the_limit = "N" * MAX_LOGIN_IDENTIFIER_LENGTH
    assert len(at_the_limit) == MAX_LOGIN_IDENTIFIER_LENGTH

    response = _login(client, at_the_limit)
    assert response.status_code == status.HTTP_401_UNAUTHORIZED, response.text

    rows = committed_audit_rows(db_session, "LOGIN_FAILED")
    assert [row.error_message for row in rows] == ["User not found"], "the maximum-length identifier was not looked up"


def test_an_over_length_identifier_is_refused_before_any_lookup_and_audited_truncated(
    client: TestClient, db_session: Session
):
    """Refused above the lookup, asserted on the DATABASE rather than only on the status.

    A user whose badge is a PREFIX of the over-long submission is seeded with a non-zero
    ``failed_login_attempts``: a counter that neither moved nor cleared proves the handler
    never read or wrote the row, which is the "before any lookup" claim. Mid-lockout is the
    right starting value because both directions of movement are then visible.

    The audit row is the other half. It records the identifier TRUNCATED and explicitly
    marked, so the row can never be read back as the whole submitted value and cannot be
    used to amplify storage -- and it fits ``audit_logs.resource_identifier`` (String(255)),
    so an over-long submission cannot raise a truncation error out of the commit instead of
    refusing cleanly.
    """
    user = make_user(db_session, employee_id="EMP-7777")
    user.failed_login_attempts = 3
    db_session.commit()

    submitted = "EMP-7777" + "X" * MAX_LOGIN_IDENTIFIER_LENGTH
    response = _login(client, submitted, PASSWORD)

    assert response.status_code == status.HTTP_401_UNAUTHORIZED, response.text
    assert response.json()["detail"] == BADGE_401
    assert "access_token" not in response.text

    db_session.expire_all()
    untouched = db_session.get(User, user.id)
    assert untouched.failed_login_attempts == 3, "the row was read/written despite the shape refusal"
    assert untouched.locked_until is None

    rows = committed_audit_rows(db_session, "LOGIN_FAILED")
    refusals = [row for row in rows if "refused before lookup" in (row.error_message or "")]
    assert len(refusals) == 1, f"the shape refusal must be audited exactly once: {[r.error_message for r in rows]}"
    (row,) = refusals
    assert str(MAX_LOGIN_IDENTIFIER_LENGTH) in row.error_message
    assert row.success == "false"  # AuditLog.success is a String(10) column

    assert row.resource_identifier.startswith("employee-id:EMP-7777"), "the row must name what was tried"
    assert row.resource_identifier.endswith("[truncated]"), "a partial value must never read as the whole one"
    assert submitted not in row.resource_identifier
    assert len(row.resource_identifier) <= AuditLog.__table__.c.resource_identifier.type.length
    assert row.extra_data["employee_id"].endswith("[truncated]")
    assert "email" not in row.extra_data, "a badge must not travel under an 'email' key"


def test_an_over_length_identifier_does_not_spend_the_per_ip_budget(client: TestClient, db_session: Session):
    """Proven by ARITHMETIC, so a build that counted them could not pass by accident.

    ``MAX-1`` genuine enumerable failures, then a pile of over-long submissions, then the
    ``MAX``th genuine failure -- still ANSWERED, which it could not be if the malformed
    input had been counted. Only the one after it trips. A test that merely showed an
    over-long request returning 401 would pass against a build that counted every one of
    them.

    The property matters because the alternative is a cheaper attack than the one the
    budget bounds: malformed input costs an attacker nothing to generate, so counting it
    would let them exhaust an IP's budget -- and with it every colleague behind the same
    NAT -- without ever probing an account.
    """
    for attempt in range(PASSWORD_LOGIN_MAX_FAILURES - 1):
        assert _login(client, f"NOBODY-{attempt}").status_code == status.HTTP_401_UNAUTHORIZED

    for _ in range(10):
        assert _login(client, _over_length()).status_code == status.HTTP_401_UNAUTHORIZED

    # Still only MAX-1 counted: this is the MAXth, answered rather than blocked...
    assert _login(client, "NOBODY-FINAL").status_code == status.HTTP_401_UNAUTHORIZED
    # ...and it is what arms the block for the attempt after it.
    _assert_throttled(_login(client, "NOBODY-AFTER"))


def test_the_throttle_429_still_outranks_the_length_refusal(client: TestClient, db_session: Session):
    """Ordering, pinned because both checks sit at the top of the handler.

    A throttled IP must be refused 429 for ANY enumerable submission, including a malformed
    one. Were the length check first, an attacker could keep a blocked IP producing 401s and
    audit rows indefinitely -- and the 429 that tells an operator a sweep is happening would
    stop being written.
    """
    _exhaust_password_route(client)

    _assert_throttled(_login(client, _over_length()))


# ===========================================================================
# 10. Every audit identifier fits its column -- bounded where the row is COMPOSED
# ===========================================================================
#
# ``audit_logs.resource_identifier`` is ``String(255)``. ``log_auth_event`` does not write
# the submitted value, it writes a COMPOSED one -- ``employee-id:<badge>`` -- and 12
# characters is exactly the margin by which a badge that §9's length gate legitimately
# ACCEPTS (255) overruns the column. Worse, the throttle's 429 branch logs the submitted
# identifier BEFORE the length gate has run at all, so on that path there is no bound
# upstream to inherit.
#
# On Postgres an over-long INSERT is ``StringDataRightTruncation`` -> ``DataError``, which
# ``AuditService.log`` and ``log_auth_event`` both swallow -- and the poisoned session then
# fails the handler's own ``db.commit()``, so the attempt loses its audit row AND the
# request 500s. That 500 is itself a distinguishable outcome on an unauthenticated route.
#
# THESE TESTS CANNOT PROVE THAT BY INSERTING. The suite runs on in-memory SQLite, which
# declares VARCHAR widths and then ignores them (docs/DEVELOPMENT.md -> "Why the tests run
# on SQLite"), so an over-long row would insert happily here and the test would pass on a
# build that 500s in production. So the column width is read OFF THE MODEL and asserted
# against the composed value's length, and the one place a statement is involved is
# DIALECT-COMPILED rather than executed.


def _postgres_ddl_for_audit_log() -> str:
    return str(CreateTable(AuditLog.__table__).compile(dialect=postgresql.dialect()))


def test_the_bound_is_the_audit_column_and_postgres_really_enforces_that_width():
    """Compiled, never executed -- the only way this suite can see a Postgres-only rule.

    Two halves, and both are needed for the bound to mean anything:
      * the guard's limit is READ OFF the model column, so widening the column widens the
        guard with it (a hard-coded 255 left behind by a migration would keep truncating
        values the column had grown to hold, and nothing would fail);
      * the width is REAL on the production engine -- the compiled Postgres DDL declares
        ``VARCHAR(255)`` -- and the value reaches it as a BIND PARAMETER, so nothing in the
        driver or the SQL layer trims it on the way down. Together those say: an
        application-level bound is the only thing standing between a 267-character
        composed identifier and a DataError.
    """
    assert _AUDIT_IDENTIFIER_MAX_LENGTH == AuditLog.__table__.c.resource_identifier.type.length

    assert f"resource_identifier VARCHAR({_AUDIT_IDENTIFIER_MAX_LENGTH})" in _postgres_ddl_for_audit_log()

    over_long = "N" * (_AUDIT_IDENTIFIER_MAX_LENGTH + 12)
    compiled = AuditLog.__table__.insert().values(resource_identifier=over_long).compile(dialect=postgresql.dialect())
    assert "%(resource_identifier)s" in str(compiled), "the identifier is bound, not inlined -- nothing trims it"
    assert compiled.params["resource_identifier"] == over_long, "the whole value would reach VARCHAR(255)"

    # The marker must fit INSIDE the limit, or marking the truncation is what overruns it.
    assert len(_AUDIT_TRUNCATION_MARKER) < _AUDIT_IDENTIFIER_MAX_LENGTH


@pytest.mark.parametrize("length", [0, 1, 12, 254, 255])
def test_a_value_that_already_fits_comes_back_byte_identical(length: int):
    """No silent reformatting of ordinary rows -- asserted with ``is``, not ``==``.

    Every existing audit row must keep the identifier it had. ``is`` is the strongest
    available statement of that: the guard returns the caller's own object untouched, so a
    build that normalized, stripped or re-encoded "safe" values fails here even if the
    resulting string happened to compare equal.
    """
    value = "N" * length
    assert _bounded_audit_value(value) is value


def test_an_over_long_value_is_cut_to_fit_and_says_so():
    """The cut lands INSIDE the column, marker included, and never reads as the whole value."""
    over_long = "N" * (_AUDIT_IDENTIFIER_MAX_LENGTH + 12)

    bounded = _bounded_audit_value(over_long)

    assert len(bounded) == _AUDIT_IDENTIFIER_MAX_LENGTH, "the marker must be inside the limit, not appended past it"
    assert bounded.endswith(_AUDIT_TRUNCATION_MARKER)
    assert bounded != over_long
    assert over_long.startswith(bounded[: -len(_AUDIT_TRUNCATION_MARKER)]), "the kept prefix must be the real one"


def test_a_maximum_length_badge_fits_the_audit_column_once_the_prefix_is_added(client: TestClient, db_session: Session):
    """THE GAP: a badge §9 legitimately ACCEPTS still overruns the audit column.

    255 characters passes the length gate (§9 pins that boundary as ``>``, not ``>=``), so
    this request is looked up like any other -- the ``"User not found"`` cause proves it was
    not refused on shape. ``employee-id:`` then makes the composed identifier 267, and the
    12-character overrun is invisible on SQLite. Asserted on the LENGTH against the model
    column, which is the only assertion that could ever fail here.

    ``extra_data`` is the other half and is checked at the same boundary: 255 fits exactly,
    so the badge must appear there byte-for-byte -- the bound must not start shortening
    values that were always legal.
    """
    badge = "B" * MAX_LOGIN_IDENTIFIER_LENGTH
    assert len(f"employee-id:{badge}") > _AUDIT_IDENTIFIER_MAX_LENGTH, "precondition: composing overruns the column"

    response = _login(client, badge)
    assert response.status_code == status.HTTP_401_UNAUTHORIZED, response.text

    rows = committed_audit_rows(db_session, "LOGIN_FAILED")
    assert len(rows) == 1
    (row,) = rows
    assert row.error_message == "User not found", "the badge was refused on shape instead of being looked up"

    assert row.resource_identifier.startswith("employee-id:BBB")
    assert len(row.resource_identifier) == _AUDIT_IDENTIFIER_MAX_LENGTH
    assert row.resource_identifier.endswith(_AUDIT_TRUNCATION_MARKER)
    assert badge not in row.resource_identifier

    assert row.extra_data["employee_id"] == badge, "a value that fits must not be reformatted"
    assert len(row.extra_data["employee_id"]) <= _AUDIT_IDENTIFIER_MAX_LENGTH


def test_an_ordinary_identifier_is_recorded_exactly_as_submitted(client: TestClient, db_session: Session):
    """The regression guard for every audit row this app already writes.

    A bound applied too eagerly is silent: rows keep being written, they just stop naming
    what was tried. Both identifier shapes are pinned to the exact composed string,
    including the email path, which this feature is only allowed to leave alone.
    """
    assert _login(client, "NOBODY-042").status_code == status.HTTP_401_UNAUTHORIZED
    assert _login(client, "opal.rivera@wercomfg.com").status_code == status.HTTP_401_UNAUTHORIZED

    rows = committed_audit_rows(db_session, "LOGIN_FAILED")
    identifiers = sorted(row.resource_identifier for row in rows)
    assert identifiers == ["employee-id:NOBODY-042", "opal.rivera@wercomfg.com"]
    for row in rows:
        assert _AUDIT_TRUNCATION_MARKER not in row.resource_identifier
        for value in (row.extra_data or {}).values():
            assert _AUDIT_TRUNCATION_MARKER not in value


def test_the_throttled_429_row_is_bounded_even_though_it_is_written_above_the_length_gate(
    client: TestClient, db_session: Session
):
    """The branch with NO upstream bound to inherit, driven for both identifier shapes.

    §9's length refusal sits BELOW the throttle check, so on a blocked IP an arbitrarily
    long identifier reaches ``log_auth_event`` unfiltered -- an attacker chooses the length
    and the row is written before anything has looked at it. Both shapes are covered
    because they land in different ``extra_data`` keys and a fix applied to one only would
    leave the other unbounded (the synthetic-address shape is throttled precisely because
    it is badge-derived and enumerable, §5).

    Also a storage-amplification guard: unbounded, a blocked IP could keep writing rows
    sized by the attacker into a table nothing is allowed to delete (008/060).
    """
    _exhaust_password_route(client)

    long_badge = "Z" * (MAX_LOGIN_IDENTIFIER_LENGTH + 45)
    long_synthetic = "z" * (MAX_LOGIN_IDENTIFIER_LENGTH + 45) + f"@{SYNTHETIC_EMAIL_DOMAIN}"

    _assert_throttled(_login(client, long_badge))
    _assert_throttled(_login(client, long_synthetic))

    rows = [r for r in committed_audit_rows(db_session, "LOGIN_BLOCKED") if (r.error_message or "").startswith("Thro")]
    assert len(rows) == 2, [r.error_message for r in rows]

    by_key = {}
    for row in rows:
        assert len(row.resource_identifier) == _AUDIT_IDENTIFIER_MAX_LENGTH
        assert row.resource_identifier.endswith(_AUDIT_TRUNCATION_MARKER)
        assert len(row.extra_data) == 1
        ((key, value),) = row.extra_data.items()
        assert len(value) == _AUDIT_IDENTIFIER_MAX_LENGTH, "extra_data is a second copy and needs the same bound"
        assert value.endswith(_AUDIT_TRUNCATION_MARKER)
        by_key[key] = value

    assert set(by_key) == {"employee_id", "email"}, f"both shapes must reach their own key: {by_key}"
    assert long_badge not in by_key["employee_id"]
    assert long_synthetic not in by_key["email"]


# ===========================================================================
# 11. WHICH 409 it was is carried structurally, and it is permanent
# ===========================================================================
#
# Both badge resolvers raise 409 for two genuinely different facts:
#
#   * an ESTABLISHED duplicate -- more than one row matched, uniqueness was violated;
#   * a TRUNCATED candidate window -- the query refused to answer, so uniqueness was never
#     established at all.
#
# The handlers used to hard-code "Employee ID resolves to more than one account" for both.
# On a truncation that sentence states something no query checked, and it is PERMANENT:
# migrations 008/060 refuse UPDATE and DELETE on ``audit_logs`` and invariant 2 forbids
# backfilling. An admin reads it forever and goes hunting a duplicate that may not exist,
# instead of at a user table the resolver can no longer scan.
#
# The cause therefore travels as a TYPE (``_AmbiguousIdentifier`` / ``_UnresolvableIdentifier``)
# with the audit sentence attached, never as the ``detail`` string -- ``detail`` is UI copy
# anyone may reword, and coupling a permanent fact to mutable copy means a wording edit
# silently starts recording the wrong cause. Both routes are covered: /auth/login had the
# wrong sentence, and /auth/employee-login -- the door the floor actually uses -- wrote no
# row at all.


def _seed_ambiguous_and_truncating_table(db: Session) -> None:
    """One table exhibiting BOTH 409 causes at once, on two different badges.

    ``SHARED-09`` is held twice, so an EXACT match returns two rows -- an established
    duplicate, no normalization involved. ``0001`` narrows to ``%1%``, which one row past
    ``_EMPLOYEE_ID_CANDIDATE_CAP`` of noise makes incomplete -- a window that truncated.
    Seeding both together is what lets one test compare the two rows directly instead of
    across files, which is the whole point: the claim is that they DIFFER.
    """
    make_user(db, company_id=COMPANY_A, employee_id="SHARED-09")
    make_user(db, company_id=COMPANY_B, employee_id="SHARED-09")
    _seed_badge_noise(db, _EMPLOYEE_ID_CANDIDATE_CAP + 1)


def test_the_two_409_causes_earn_different_permanent_sentences_on_the_password_route(
    client: TestClient, db_session: Session
):
    """Both rows in one test, asserted to differ AND each pinned to its own exact sentence.

    Asserting only that they differ would pass on a build that swapped them; asserting only
    the literals would pass on a build where both had been changed to the same new string.
    The ambiguity wording is additionally pinned to the exact pre-existing sentence, because
    that row is not new -- it is what every admin's saved query already looks for.
    """
    _seed_ambiguous_and_truncating_table(db_session)

    ambiguous = _login(client, "SHARED-09")
    truncated = _login(client, "0001")

    assert ambiguous.status_code == status.HTTP_409_CONFLICT, ambiguous.text
    assert truncated.status_code == status.HTTP_409_CONFLICT, truncated.text
    assert ambiguous.json()["detail"] == _AMBIGUOUS_EMPLOYEE_ID_DETAIL
    assert truncated.json()["detail"] == _UNRESOLVABLE_EMPLOYEE_ID_DETAIL

    rows = {row.resource_identifier: row for row in committed_audit_rows(db_session, "LOGIN_BLOCKED")}
    assert set(rows) == {"employee-id:SHARED-09", "employee-id:0001"}, rows

    ambiguity_row = rows["employee-id:SHARED-09"]
    truncation_row = rows["employee-id:0001"]

    assert ambiguity_row.error_message == "Employee ID resolves to more than one account"
    assert ambiguity_row.error_message == _AUDIT_ERROR_EMPLOYEE_ID_AMBIGUOUS
    assert truncation_row.error_message == _AUDIT_ERROR_EMPLOYEE_ID_UNRESOLVABLE
    assert truncation_row.error_message != ambiguity_row.error_message, (
        "a truncated window establishes NO duplicate -- recording it as one is a claim "
        "nobody checked, on a row that can never be corrected"
    )
    assert "never established" in truncation_row.error_message
    for row in (ambiguity_row, truncation_row):
        assert row.success == "false"  # AuditLog.success is a String(10) column


def test_the_two_409_causes_earn_different_permanent_sentences_on_the_kiosk_route(
    client: TestClient, db_session: Session
):
    """The floor's own door, which used to return 409 with NO audit row at all.

    Nothing else in the system reports either cause; the operator just sees a badge that
    stopped working. The badge IS recorded here -- unlike the ordinary failure rows on this
    route, which deliberately log none -- because a 409 means the value MATCHED real rows,
    so it is a known-good badge rather than a possibly-mistyped credential fragment, and it
    is the only thing that tells an admin which rows to merge.
    """
    _seed_ambiguous_and_truncating_table(db_session)

    ambiguous = _employee_login(client, "SHARED-09")
    truncated = _employee_login(client, "0001")

    assert ambiguous.status_code == status.HTTP_409_CONFLICT, ambiguous.text
    assert truncated.status_code == status.HTTP_409_CONFLICT, truncated.text

    rows = {row.resource_identifier: row for row in committed_audit_rows(db_session, "EMPLOYEE_LOGIN_BLOCKED")}
    assert set(rows) == {"employee-id:SHARED-09", "employee-id:0001"}, rows

    assert rows["employee-id:SHARED-09"].error_message == _AUDIT_ERROR_EMPLOYEE_ID_AMBIGUOUS
    assert rows["employee-id:0001"].error_message == _AUDIT_ERROR_EMPLOYEE_ID_UNRESOLVABLE
    assert rows["employee-id:0001"].error_message != rows["employee-id:SHARED-09"].error_message


def test_the_kiosk_409_does_not_burn_the_badge_routes_failure_budget(client: TestClient, db_session: Session):
    """Mirrors /auth/login's treatment of the same refusal, and for the sharper reason.

    An ambiguous badge is an admin data problem and the account provably exists, so it is
    not a wrong guess. Counting it would let ONE duplicated row lock a whole shop's egress
    IP out of badge sign-in for the 15-minute cooldown, with no admin reset -- an outage
    caused by the data error rather than by anyone attacking.
    """
    make_user(db_session, company_id=COMPANY_A, employee_id="SHARED-10")
    make_user(db_session, company_id=COMPANY_B, employee_id="SHARED-10")

    for _ in range(EMPLOYEE_LOGIN_MAX_FAILURES + 1):
        assert _employee_login(client, "SHARED-10").status_code == status.HTTP_409_CONFLICT

    # An unknown badge still gets the ordinary 401, not a 429 left behind by the 409s.
    assert _employee_login(client, "9911").status_code == status.HTTP_401_UNAUTHORIZED


def test_the_cause_is_read_from_the_exception_type_never_from_the_reworded_detail():
    """The coupling this fix broke, pinned directly so it cannot be "simplified" back.

    ``detail`` is UI copy. A future reword -- entirely reasonable, entirely local -- would,
    under a ``detail``-sniffing implementation, silently start writing the OTHER cause onto
    every row from that day on. So the classifier is driven here with deliberately WRONG
    detail strings: the audit sentence must follow the type regardless.

    The unclassified fallback is the third case: a 409 a later edit raises from somewhere
    else must record "we do not know why" rather than inherit whichever specific claim
    happened to be written first.
    """
    assert _AUDIT_ERROR_EMPLOYEE_ID_AMBIGUOUS != _AUDIT_ERROR_EMPLOYEE_ID_UNRESOLVABLE

    misleading = _AmbiguousIdentifier(_UNRESOLVABLE_EMPLOYEE_ID_DETAIL, _AUDIT_ERROR_EMPLOYEE_ID_AMBIGUOUS)
    assert _conflict_audit_error(misleading) == _AUDIT_ERROR_EMPLOYEE_ID_AMBIGUOUS

    also_misleading = _UnresolvableIdentifier(_AMBIGUOUS_EMPLOYEE_ID_DETAIL, _AUDIT_ERROR_EMPLOYEE_ID_UNRESOLVABLE)
    assert _conflict_audit_error(also_misleading) == _AUDIT_ERROR_EMPLOYEE_ID_UNRESOLVABLE

    bare = HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_AMBIGUOUS_EMPLOYEE_ID_DETAIL)
    assert _conflict_audit_error(bare) == _AUDIT_ERROR_CONFLICT_UNCLASSIFIED
    assert _conflict_audit_error(bare) != _AUDIT_ERROR_EMPLOYEE_ID_AMBIGUOUS


# ===========================================================================
# 12. A blank identifier is refused on SHAPE, and costs the budget nothing
# ===========================================================================
#
# A blank ``username`` contains no ``"@"``, so it takes the badge branch and counts as
# ENUMERABLE -- which meant it walked all the way to "user not found" and SPENT one unit of
# the per-IP password-login budget. Both resolvers return ``None`` on a blank string before
# touching the database, so the attempt establishes nothing about any account; letting it
# drain a budget shared with everyone behind the same NAT is a cheaper route to the lockout
# that budget exists to bound than the sweep itself.
#
# WHITESPACE-ONLY is the reachable half, which is why the guard tests ``.strip()`` rather
# than truthiness: FastAPI answers 422 above the handler for a genuinely empty required
# ``Form`` field, so ``""`` never charges anything. Both are exercised below so the
# behavior does not silently depend on that framework detail.


@pytest.mark.parametrize("blank", ["   ", "\t", " \t "])
def test_a_whitespace_identifier_answers_exactly_like_an_ordinary_bad_credential(
    client: TestClient, db_session: Session, blank: str
):
    """Equal to the ordinary miss, asserted against it rather than against a literal.

    A distinct "identifier required" would be a new distinguishable outcome on an
    unauthenticated route and a trivially cheap one to probe.
    """
    blank_response = _login(client, blank)
    ordinary = _login(client, "NOBODY")

    assert (blank_response.status_code, blank_response.json()) == (ordinary.status_code, ordinary.json())
    assert blank_response.status_code == status.HTTP_401_UNAUTHORIZED, blank_response.text
    assert blank_response.json()["detail"] == BADGE_401


def test_the_blank_refusal_is_recorded_and_names_no_identifier(client: TestClient, db_session: Session):
    """One row, and it records that a blank submission was refused -- not a value.

    There is no identifier to key on, so ``resource_identifier`` and ``extra_data`` must
    both be empty. A row keyed on ``""`` would be worse than none: it reads as an account
    that was tried.
    """
    assert _login(client, "   ").status_code == status.HTTP_401_UNAUTHORIZED

    rows = committed_audit_rows(db_session, "LOGIN_FAILED")
    assert len(rows) == 1
    (row,) = rows
    assert row.error_message == "Empty identifier; refused before lookup"
    assert row.resource_identifier is None
    assert row.extra_data is None
    assert row.success == "false"  # AuditLog.success is a String(10) column


def test_a_genuinely_empty_username_never_reaches_the_handler(client: TestClient, db_session: Session):
    """The framework detail the guard deliberately does not depend on.

    FastAPI treats ``""`` on a required ``Form`` field as MISSING and answers above the
    route, so the empty case cannot charge the throttle either. Pinned loosely (refused,
    no token, no audit row) rather than to a status code, because which code the framework
    picks is not this feature's contract -- what matters is that nothing was spent.
    """
    response = _login(client, "")

    assert response.status_code in (status.HTTP_401_UNAUTHORIZED, status.HTTP_422_UNPROCESSABLE_CONTENT), response.text
    assert "access_token" not in response.text
    assert committed_audit_rows(db_session, "LOGIN_FAILED") == []


def test_blank_submissions_do_not_spend_the_per_ip_budget(client: TestClient, db_session: Session):
    """Proven by ARITHMETIC, the same shape §9 uses, so a counting build cannot pass.

    ``MAX-1`` genuine enumerable failures, a pile of blanks, then the ``MAX``th genuine
    failure -- still ANSWERED, which it could not be if the blanks had been counted. Only
    the attempt after it trips. A test that merely showed a blank returning 401 would pass
    against a build that charged every one of them.
    """
    for _ in range(10):
        assert _login(client, "   ").status_code == status.HTTP_401_UNAUTHORIZED
        assert _login(client, "").status_code in (
            status.HTTP_401_UNAUTHORIZED,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    for attempt in range(PASSWORD_LOGIN_MAX_FAILURES - 1):
        assert _login(client, f"NOBODY-{attempt}").status_code == status.HTTP_401_UNAUTHORIZED

    # Still only MAX-1 counted: this is the MAXth, answered rather than blocked...
    assert _login(client, "NOBODY-FINAL").status_code == status.HTTP_401_UNAUTHORIZED
    # ...and it is what arms the block for the attempt after it.
    _assert_throttled(_login(client, "NOBODY-AFTER"))


# ===========================================================================
# 13. An ALREADY-LOCKED account costs the shared per-IP budget nothing
# ===========================================================================
#
# Every other failure branch on this route counts against ``password_login_throttle``,
# and the locked branch used to as well. It is the one branch where counting buys
# nothing: the account is ALREADY locked, the password is not even checked, so a further
# attempt establishes nothing an attacker did not already have from the five failures that
# caused the lock. What it does establish is a cost -- and the population paying it is the
# legitimate one. The person retrying their own locked account during the 30-minute window
# blows straight past the 5-failures-per-user figure the budget is sized on, and the budget
# is per IP: one operator hammering their own locked account can take PASSWORD SIGN-IN
# offline for everyone behind the shop's egress address, for the whole cooldown, with no
# admin reset.
#
# Not counting is NOT not recording. The branch still writes its LOGIN_BLOCKED /
# "Account locked" audit row, which is asserted below alongside the arithmetic -- an
# "optimization" that dropped the row would make a locked account's retries invisible.


def test_retrying_an_already_locked_account_does_not_spend_the_per_ip_budget(client: TestClient, db_session: Session):
    """Proven by ARITHMETIC, the way §5 proves the ordinary-address exemption.

    N-1 enumerable failures, then a pile of attempts against a locked account, then the
    Nth failure -- still answered 401, which it could not be if the locked attempts had
    been counted. Only the one after it trips. A test that merely showed a 403 while
    unblocked would pass against a build that counted every one of them.

    The correct password is used for the locked attempts on purpose: it is a lock, not a
    counter, so the 403 is reached without bcrypt and without any claim about credentials.
    """
    locked = make_user(db_session, employee_id="EMP-4141")
    locked.locked_until = datetime.utcnow() + timedelta(minutes=30)
    locked.failed_login_attempts = 5
    db_session.commit()

    for attempt in range(PASSWORD_LOGIN_MAX_FAILURES - 1):
        assert _login(client, f"NOBODY-{attempt}").status_code == status.HTTP_401_UNAUTHORIZED

    for attempt in range(10):
        refused = _login(client, "EMP-4141")
        assert refused.status_code == status.HTTP_403_FORBIDDEN, f"attempt {attempt}: {refused.text}"
        assert "locked" in refused.json()["detail"].lower()

    # Still only N-1 enumerable failures on the books: this is the Nth, answered not blocked...
    assert _login(client, "NOBODY-FINAL").status_code == status.HTTP_401_UNAUTHORIZED, (
        "the locked-account retries spent the shared per-IP budget -- one operator retrying "
        "their own lock can then take password sign-in offline for their whole floor"
    )
    # ...and it is what arms the block for the next one.
    _assert_throttled(_login(client, "NOBODY-AFTER"))

    db_session.expire_all()
    still_locked = db_session.get(User, locked.id)
    assert still_locked.failed_login_attempts == 5, "the locked branch must not move the account's own counter either"
    assert still_locked.locked_until is not None

    # NOT COUNTED IS NOT NOT RECORDED: every refusal is still on the permanent record.
    locked_rows = [
        row
        for row in committed_audit_rows(db_session, "LOGIN_BLOCKED")
        if (row.error_message or "") == "Account locked"
    ]
    assert len(locked_rows) == 10, f"every locked-account attempt must be audited, got {len(locked_rows)}"
    assert all(row.success == "false" for row in locked_rows)  # AuditLog.success is a String(10) column


# ===========================================================================
# 14. A legacy address that CANNOT be repaired must not cost the operator their sign-in
# ===========================================================================
#
# ``_ensure_valid_auth_email`` rewrites a legacy ``@werco.local`` address to the synthetic
# ``emp-...@users.werco.com`` shape on the user's first successful login. The rewrite runs
# through ``synthetic_email_for_employee_id``, which is a BOUNDED walk: when every spelling
# it can reach is held by another account it raises ``IdentifierDerivationExhausted``.
#
# That is the worst place in the system for an uncaught exception. It runs on the LOGIN
# path, AFTER the password has been verified, and BEFORE the terminal commit -- so an
# escape takes the failed-attempt reset and the LOGIN_SUCCESS audit row down with it. The
# operator's credentials were correct, the system knows it, and the record says nothing.
#
# The repair is opportunistic; the sign-in is not. Caught, the login proceeds on the
# address the row already holds -- nothing is worse than before the repair was attempted.
#
# THE RESIDUAL IS REAL AND IS ASSERTED AROUND, NOT PAPERED OVER. ``UserResponse.email`` is
# an ``EmailStr`` and pydantic rejects ``@werco.local`` as a reserved special-use domain, so
# an unrepaired row still fails when the token response is built. What the catch converts
# is "lost audit row + lost counter reset + 500" into "correct state + honest audit row +
# 500 on serialization" -- and the fix for a genuinely unrepairable row is an admin editing
# it, not this seam inventing an address the probe just refused (which would land a real
# operator on somebody else's address). So the test asserts the AUTHENTICATION outcome,
# tolerates either response, and fails loudly on the one thing that must never happen
# again: the derivation exception escaping the login path.


def _hold_every_minted_spelling(db: Session, badge: str, *, company_id: int = COMPANY_A) -> None:
    """Commit rows occupying every address the mint can reach for ``badge``.

    ``emp-<badge>@users.werco.com`` plus ``-2`` ... ``-100``: the full candidate set the
    walk offers before it gives up. The holders never authenticate, so they carry a literal
    hash rather than paying bcrypt a hundred times.
    """
    _ensure_company(db, company_id)
    local = badge.lower()
    spellings = [f"emp-{local}@{SYNTHETIC_EMAIL_DOMAIN}"] + [
        f"emp-{local}-{n}@{SYNTHETIC_EMAIL_DOMAIN}" for n in range(2, MAX_IDENTIFIER_CANDIDATES + 1)
    ]
    assert len(spellings) == MAX_IDENTIFIER_CANDIDATES
    db.add_all(
        [
            User(
                email=address,
                employee_id=f"HOLDER-{_next():05d}",
                first_name="Address",
                last_name="Holder",
                hashed_password="$2b$12$abcdefghijklmnopqrstuv",
                role=UserRole.OPERATOR,
                is_active=True,
                is_superuser=False,
                company_id=company_id,
                failed_login_attempts=0,
            )
            for address in spellings
        ]
    )
    db.commit()


def test_an_unrepairable_legacy_address_still_completes_the_sign_in(client: TestClient, db_session: Session):
    """The authentication outcome, asserted on the DATABASE because the response cannot say it.

    ``failed_login_attempts`` back to zero and a committed LOGIN_SUCCESS row are exactly
    what an escaping ``IdentifierDerivationExhausted`` destroys: it is raised before the
    terminal commit, so the reset and the row roll back with it and the operator's correct
    password leaves no trace at all.
    """
    legacy = make_user(db_session, employee_id="LEGACY-77", email=f"emp-legacy-77@{LEGACY_RESERVED_EMAIL_DOMAIN}")
    legacy.failed_login_attempts = 3  # so the reset is VISIBLE, not merely already-zero
    db_session.commit()
    _hold_every_minted_spelling(db_session, "LEGACY-77")

    response = None
    try:
        response = _login(client, "LEGACY-77")
    except IdentifierDerivationExhausted as exc:  # pragma: no cover - the regression itself
        pytest.fail(f"the exhausted mint escaped the login path: {exc}")
    except ValidationError:
        # The documented residual: the row still holds an address ``UserResponse`` refuses.
        # Tolerated rather than asserted, so that the day the residual is closed this test
        # tightens onto the stronger claim below instead of failing for a good change.
        pass

    db_session.expire_all()
    signed_in = db_session.get(User, legacy.id)
    assert signed_in.failed_login_attempts == 0, "the password was correct; the counter must have been reset"
    assert signed_in.locked_until is None
    assert signed_in.email == f"emp-legacy-77@{LEGACY_RESERVED_EMAIL_DOMAIN}", (
        "the address must be left EXACTLY as it was -- inventing one the probe just refused "
        "would put this operator on an address another account already owns"
    )

    success_rows = committed_audit_rows(db_session, "LOGIN_SUCCESS")
    assert [row.resource_identifier for row in success_rows] == ["employee-id:LEGACY-77"], (
        "the sign-in really happened, so it must be on the permanent record -- an escaping "
        "exception rolls this row back and the successful login becomes invisible"
    )

    if response is not None:  # the residual is gone: then the whole route must work
        assert response.status_code == status.HTTP_200_OK, response.text
        assert response.json()["access_token"]


def test_the_repair_seam_itself_leaves_the_address_alone_rather_than_raising(client: TestClient, db_session: Session):
    """The same guarantee one layer down, where the response schema cannot obscure it.

    Driving ``_ensure_valid_auth_email`` directly is what pins "does not raise" and "does
    not mutate" as one claim: the endpoint test above can only observe them through a
    request that fails validation afterwards for an unrelated reason.

    The second half is the regression guard facing the other way -- a repair that CAN
    converge must still happen. A catch that swallowed everything would pass the first
    assertion and quietly stop repairing every legacy row in the install.
    """
    unrepairable = make_user(db_session, employee_id="LEGACY-88", email=f"emp-legacy-88@{LEGACY_RESERVED_EMAIL_DOMAIN}")
    _hold_every_minted_spelling(db_session, "LEGACY-88")

    auth_module._ensure_valid_auth_email(unrepairable, db_session)  # must not raise

    assert unrepairable.email == f"emp-legacy-88@{LEGACY_RESERVED_EMAIL_DOMAIN}"

    repairable = make_user(db_session, employee_id="LEGACY-99", email=f"emp-legacy-99@{LEGACY_RESERVED_EMAIL_DOMAIN}")
    auth_module._ensure_valid_auth_email(repairable, db_session)

    assert (
        repairable.email == f"emp-legacy-99@{SYNTHETIC_EMAIL_DOMAIN}"
    ), "the catch must be narrow: a legacy row whose repair CAN converge still has to be repaired"


# ===========================================================================
# 15. The wide candidate window is READ WIDE and HYDRATED NARROW
# ===========================================================================
#
# ``_normalized_employee_id_matches`` reads up to ``_EMPLOYEE_ID_CANDIDATE_CAP + 1`` rows
# to answer a badge scan and then discards all but the handful that normalize to the
# submitted badge. Loading them as mapped ``User`` entities meant hydrating ~501 objects
# (plus identity-map bookkeeping) to compare one string -- on the query the crew station
# runs on EVERY scan. The window is therefore read as two columns and the entities are
# loaded only for the ids that actually matched.
#
# The contract must not change with it: callers get real ``User`` objects, ordered by id.
# A refactor that returned ``Row`` tuples would break every caller's attribute access, and
# one that dropped the second ``order_by`` would make which row a badge resolves to depend
# on the plan -- the determinism §7 exists to pin.


def _statements_during(db: Session, work):
    """Every SQL statement the session issues while ``work()`` runs, in order."""
    statements: list = []
    bind = db.get_bind()

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(bind, "before_cursor_execute", record)
    try:
        result = work()
    finally:
        event.remove(bind, "before_cursor_execute", record)
    return result, statements


def test_the_window_is_read_as_two_columns_and_only_matches_are_hydrated(db_session: Session):
    """The cost, asserted on the SQL rather than on a stopwatch.

    The wide read must not be an entity load -- ``users.hashed_password`` appearing in it is
    the tell, since a mapped ``User`` SELECT lists every column. The narrow follow-up runs
    only when something matched, which is what the zero-match half pins: no match, no second
    query at all.
    """
    _seed_badge_noise(db_session, 40)  # badges containing "1", none normalizing to 0001
    match = make_user(db_session, employee_id="EMP-0001")

    matches, statements = _statements_during(db_session, lambda: _normalized_employee_id_matches(db_session, "0001"))

    assert [user.id for user in matches] == [match.id]
    assert isinstance(matches[0], User), "callers read mapped attributes off these; Row tuples would break them"
    assert matches[0].email == match.email, "the matched rows really are hydrated entities"

    assert len(statements) == 2, f"expected a wide two-column read then a narrow entity load, got: {statements}"
    wide, narrow = statements
    assert (
        "hashed_password" not in wide
    ), "the wide window is loading whole User entities again -- ~501 of them, on every badge scan"
    assert "employee_id" in wide
    assert "hashed_password" in narrow, "the matched ids must come back as full entities"


def test_a_window_with_no_match_issues_no_second_query(db_session: Session):
    """The common case on an unknown badge: one query, one empty answer, nothing hydrated."""
    _seed_badge_noise(db_session, 40)

    matches, statements = _statements_during(db_session, lambda: _normalized_employee_id_matches(db_session, "0001"))

    assert matches == []
    assert len(statements) == 1, f"a no-match window must not issue the entity load: {statements}"
