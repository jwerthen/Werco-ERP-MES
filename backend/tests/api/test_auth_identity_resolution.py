"""Public registration is not an account oracle, and an ambiguous email cannot log in.

Two defects in ``api/endpoints/auth.py``, both rooted in the same place: email and
employee id are unique PER COMPANY (``uq_users_company_email`` /
``uq_users_company_employee_id`` on ``models/user.py``), but the two unauthenticated
paths that touch them are install-wide.

**A. ``POST /auth/register-public`` was a dual existence oracle.** It answered a distinct
400 for a taken email ("Email already registered") and another for a taken employee id
("Employee ID already exists"), each checked with NO company predicate. An
unauthenticated caller could therefore confirm any address or any badge number in ANY
tenant, and tell from the ``detail`` which of the two they had hit -- at 3 requests a
minute, but with no account and no clock on how long they kept at it. Both 400s are gone:
outside the first-user bootstrap every outcome returns one shared body and a duplicate
simply does not insert.

**B. Email login picked an arbitrary tenant.** ``_find_user_by_auth_email`` ended in an
unordered, unfiltered ``.first()``, so when the same address existed in two companies,
which account the credentials authenticated as was decided by row order. The impact is
NOT account takeover -- the token's ``company_id`` comes from the resolved row and you
still need that row's password -- it is (i) nondeterministic tenant landing and (ii) a
legitimate user whose row was not picked failing their own password against a stranger's
hash and incrementing THAT stranger's lockout counter, locking out a third party after
five tries. Resolution is now all-or-nothing: exactly one match logs in, two or more
refuse 409, before ``verify_password`` runs.

Two things shaped these tests
-----------------------------
* **Indistinguishability is asserted on the tuple, not eyeballed.** ``§1`` compares
  ``(status_code, body)`` across a fresh registration, a duplicate email and a duplicate
  employee id in one test -- so the assertion fails if ANY of the three drifts apart
  later, not merely if today's wording changes. Timing is explicitly out of scope here
  (the handler hashes before the check for that reason; a wall-clock assertion would be
  a flake factory in CI).

* **Order-independence needs both orderings, not one.** A "pick the lowest id" fix would
  pass a single-direction ambiguity test. ``§2`` drives the collision from BOTH sides --
  each duplicate's own correct password -- and asserts the same refusal, so nothing that
  merely re-ranks the candidates can satisfy it.

Rate limits are real in the test environment (register-public 3/min, login 5/min, per
fixed TestClient IP) and the autouse ``_reset_rate_limiter`` fixture gives each test a
fresh budget -- so no test below issues more than three registrations or five logins.
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.security import get_password_hash
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.user import User, UserRole

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1  # the seeded company
COMPANY_B = 2

REGISTER_URL = "/api/v1/auth/register-public"
LOGIN_URL = "/api/v1/auth/login"

PASSWORD = "SecureP@ss123!"
OTHER_PASSWORD = "DifferentP@ss456!"

# The one body every non-bootstrap registration outcome must return.
PENDING_BODY = {"message": "Account submitted for approval", "is_first_user": False}

_seq = {"n": 0}


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
    company_id: int,
    email: str = None,
    employee_id: str = None,
    password: str = PASSWORD,
    role: UserRole = UserRole.ADMIN,
    is_active: bool = True,
) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=email or f"auth-iso-{n}@co{company_id}.example.com",
        employee_id=employee_id or f"AUTHISO-{n:05d}",
        first_name="Tenant",
        last_name="Isolation",
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


def register_payload(email: str, *, employee_id: str = None) -> dict:
    payload = {"email": email, "first_name": "New", "last_name": "Signup", "password": PASSWORD}
    if employee_id is not None:
        payload["employee_id"] = employee_id
    return payload


def users_with_email(db: Session, email: str):
    db.expire_all()
    return db.query(User).filter(func.lower(User.email) == email.lower()).all()


def committed_audit_rows(db: Session, action: str):
    """Audit rows that survive a rollback -- i.e. rows the handler really committed.

    The ``client`` fixture shares one open transaction with the endpoint, so a merely
    flushed row would still be visible to a plain query. Borrowed from
    ``test_auth_audit_persistence.py``.
    """
    db.rollback()
    return db.query(AuditLog).filter(AuditLog.action == action).all()


# ===========================================================================
# 1. POST /auth/register-public — the dual existence oracle
# ===========================================================================


def test_registration_answers_identically_for_taken_email_taken_badge_and_fresh(
    client: TestClient, db_session: Session
):
    """THE oracle test: all three outcomes must be one response.

    Against the old code these were three DIFFERENT answers -- 400 "Email already
    registered", 400 "Employee ID already exists", and 200 -- which is exactly what let an
    unauthenticated caller enumerate accounts and badge numbers across every tenant, and
    tell which of the two attributes they had hit.

    Exactly three requests, which is the per-minute budget for this route.
    """
    existing = make_user(db_session, company_id=COMPANY_A, email="taken@example.com", employee_id="BADGE-TAKEN")

    taken_email = client.post(REGISTER_URL, json=register_payload("taken@example.com"))
    taken_badge = client.post(REGISTER_URL, json=register_payload("brand-new@example.com", employee_id="BADGE-TAKEN"))
    fresh_signup = client.post(REGISTER_URL, json=register_payload("someone-else@example.com"))

    answers = [(r.status_code, r.json()) for r in (taken_email, taken_badge, fresh_signup)]
    assert answers[0] == answers[1] == answers[2], f"registration outcomes are distinguishable: {answers}"
    assert answers[0] == (status.HTTP_200_OK, PENDING_BODY)

    # ...and the two duplicates really did not write anything.
    db_session.expire_all()
    assert db_session.query(User).filter(func.lower(User.email) == "taken@example.com").count() == 1
    assert db_session.query(User).filter(func.lower(User.email) == "brand-new@example.com").count() == 0
    assert db_session.query(User).filter(func.lower(User.employee_id) == "badge-taken").count() == 1
    assert db_session.query(User).filter(func.lower(User.email) == "someone-else@example.com").count() == 1
    assert existing.id is not None


def test_duplicate_email_registration_creates_no_user_and_is_recorded(client: TestClient, db_session: Session):
    """The refusal is silent to the CALLER but not to the operator: it commits a
    ``PUBLIC_REGISTRATION_REJECTED`` audit row, so enumeration attempts are visible to
    whoever reads the audit log even though the response gives nothing away."""
    make_user(db_session, company_id=COMPANY_A, email="dup@example.com")
    before = db_session.query(User).count()

    response = client.post(REGISTER_URL, json=register_payload("DUP@EXAMPLE.COM"))

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json() == PENDING_BODY
    db_session.expire_all()
    assert db_session.query(User).count() == before, "a refused registration must insert nothing"

    rows = committed_audit_rows(db_session, "PUBLIC_REGISTRATION_REJECTED")
    assert len(rows) == 1
    assert rows[0].success == "false"  # AuditLog.success is a String(10) column


def test_duplicate_employee_id_registration_creates_no_user(client: TestClient, db_session: Session):
    """The badge half of the oracle. Employee ids are short and highly guessable -- they
    are printed on the badge -- so confirming one is a cheaper attack than confirming an
    address."""
    make_user(db_session, company_id=COMPANY_A, employee_id="EMP-0042")
    before = db_session.query(User).count()

    response = client.post(REGISTER_URL, json=register_payload("newcomer@example.com", employee_id="emp-0042"))

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json() == PENDING_BODY
    db_session.expire_all()
    assert db_session.query(User).count() == before
    assert db_session.query(User).filter(func.lower(User.email) == "newcomer@example.com").count() == 0


def test_registration_does_not_confirm_an_account_in_another_company(client: TestClient, db_session: Session):
    """The tenancy framing of the oracle, and the reason it mattered on a multi-tenant
    install: the check spans EVERY company, so the address being probed need not have
    anything to do with the tenant the caller would be registered into. Company B's user
    list was enumerable through a route that only ever writes into company A."""
    make_user(db_session, company_id=COMPANY_B, email="cfo@othercompany.example.com", employee_id="OTHER-001")

    by_email = client.post(REGISTER_URL, json=register_payload("cfo@othercompany.example.com"))
    by_badge = client.post(REGISTER_URL, json=register_payload("probe@example.com", employee_id="OTHER-001"))

    assert (by_email.status_code, by_email.json()) == (status.HTTP_200_OK, PENDING_BODY)
    assert (by_badge.status_code, by_badge.json()) == (status.HTTP_200_OK, PENDING_BODY)
    db_session.expire_all()
    assert db_session.query(User).filter(func.lower(User.email) == "probe@example.com").count() == 0


def test_registration_never_generates_an_empty_employee_id(client: TestClient, db_session: Session):
    """An incidental hardening that shipped with the fix.

    The auto-generated badge is the email local part with everything outside
    ``[A-Za-z0-9-_]`` stripped. A local part of pure punctuation is a legal address and
    stripped to nothing, so the old code wrote ``employee_id = ""`` -- a row that then
    collides with the next such registration on a UNIQUE constraint and, worse, matches
    nothing in badge login.
    """
    response = client.post(REGISTER_URL, json=register_payload("+@example.com"))

    assert response.status_code == status.HTTP_200_OK, response.text
    db_session.expire_all()
    created = db_session.query(User).filter(func.lower(User.email) == "+@example.com").one()
    assert created.employee_id, "auto-generated employee_id must never be empty"


def test_first_user_bootstrap_still_creates_an_active_platform_admin(client: TestClient, db_session: Session):
    """A PIN ON THE PATH THE FIX MUST NOT BREAK -- it passes against the pre-fix code too.

    The uniform response and the ``already_taken`` guard both had to be threaded around
    the one-time setup branch, which returns a DIFFERENT body on purpose
    (``is_first_user: true``) and is the only way a fresh install gets its first admin.
    Turning setup into a silent no-op is the obvious way to get this fix wrong.
    """
    assert db_session.query(User).count() == 0, "this test needs the empty-users precondition"

    response = client.post(REGISTER_URL, json=register_payload("founder@example.com"))

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json() == {"message": "Admin account created successfully", "is_first_user": True}

    db_session.expire_all()
    created = db_session.query(User).filter(func.lower(User.email) == "founder@example.com").one()
    assert created.role == UserRole.PLATFORM_ADMIN
    assert created.is_superuser is True
    assert created.is_active is True, "the bootstrap admin must not be left pending approval"
    assert created.employee_id == "founder"


def test_a_pending_signup_is_created_inactive_with_the_viewer_role(client: TestClient, db_session: Session):
    """The other half of the shared body: an accepted registration still has to be inert
    until an admin approves it. Uniform responses are only safe because ACCEPTING is
    itself harmless."""
    make_user(db_session, company_id=COMPANY_A)

    response = client.post(REGISTER_URL, json=register_payload("pending@example.com"))

    assert (response.status_code, response.json()) == (status.HTTP_200_OK, PENDING_BODY)
    db_session.expire_all()
    created = db_session.query(User).filter(func.lower(User.email) == "pending@example.com").one()
    assert created.role == UserRole.VIEWER
    assert created.is_active is False
    assert created.is_superuser is False


# ===========================================================================
# 2. Login when one address exists in two companies
# ===========================================================================


def _duplicate_across_companies(db: Session, email: str = "shared@customer.example.com"):
    """The same address held by two DIFFERENT companies -- legal under the per-company
    unique constraint, and the exact state the old ``.first()`` resolved by row order."""
    user_a = make_user(db, company_id=COMPANY_A, email=email, password=PASSWORD)
    user_b = make_user(db, company_id=COMPANY_B, email=email, password=OTHER_PASSWORD)
    return user_a, user_b


def test_login_refuses_an_email_that_exists_in_two_companies(client: TestClient, db_session: Session):
    """The old code minted a token here -- for whichever row the database happened to
    return first. A user with the same work address at two tenants on this install landed
    in a nondeterministic company, with that company's data."""
    _duplicate_across_companies(db_session)

    response = client.post(LOGIN_URL, data={"username": "shared@customer.example.com", "password": PASSWORD})

    assert response.status_code == status.HTTP_409_CONFLICT, response.text
    assert "access_token" not in response.text


def test_the_ambiguity_refusal_does_not_depend_on_which_password_is_offered(client: TestClient, db_session: Session):
    """Order-independence, driven from BOTH sides.

    A "resolve it deterministically -- lowest id wins" fix would pass a single-direction
    test: user A's password would work every time. Offering EACH duplicate's own correct
    password, plus a wrong one, and getting the identical refusal is what pins that the
    outcome is a property of the DATA, not of which candidate happened to be ranked first.
    Four requests; the login budget is five.
    """
    _duplicate_across_companies(db_session)

    as_a = client.post(LOGIN_URL, data={"username": "shared@customer.example.com", "password": PASSWORD})
    as_b = client.post(LOGIN_URL, data={"username": "shared@customer.example.com", "password": OTHER_PASSWORD})
    wrong = client.post(LOGIN_URL, data={"username": "shared@customer.example.com", "password": "not-the-password-000"})
    mixed_case = client.post(LOGIN_URL, data={"username": "SHARED@CUSTOMER.EXAMPLE.COM", "password": PASSWORD})

    answers = [(r.status_code, r.json()) for r in (as_a, as_b, wrong, mixed_case)]
    assert answers[0] == answers[1] == answers[2] == answers[3], f"outcome depends on the request: {answers}"
    assert answers[0][0] == status.HTTP_409_CONFLICT


def test_an_ambiguous_login_does_not_touch_anyones_lockout_counter(client: TestClient, db_session: Session):
    """THE reason this defect was more than cosmetic.

    Old behaviour: the arbitrary winner was the row the password was checked against, so
    the OTHER user -- entering their own correct password at their own company -- failed
    against a stranger's hash and incremented the STRANGER'S ``failed_login_attempts``.
    Five attempts and a third party, who did nothing, is locked out for thirty minutes.
    The refusal now fires before ``verify_password``, so neither counter can move.
    """
    user_a, user_b = _duplicate_across_companies(db_session)

    for _ in range(3):
        response = client.post(LOGIN_URL, data={"username": "shared@customer.example.com", "password": OTHER_PASSWORD})
        assert response.status_code == status.HTTP_409_CONFLICT, response.text

    db_session.expire_all()
    assert db_session.get(User, user_a.id).failed_login_attempts == 0, "refusal incremented a bystander's counter"
    assert db_session.get(User, user_a.id).locked_until is None
    assert db_session.get(User, user_b.id).failed_login_attempts == 0
    assert db_session.get(User, user_b.id).locked_until is None


def test_an_ambiguous_login_is_recorded_for_an_admin_to_act_on(client: TestClient, db_session: Session):
    """Nothing else in the system reports the collision -- the affected users just see a
    login that stopped working -- so the audit row is the only way an admin learns which
    address needs renaming."""
    _duplicate_across_companies(db_session)

    response = client.post(LOGIN_URL, data={"username": "shared@customer.example.com", "password": PASSWORD})
    assert response.status_code == status.HTTP_409_CONFLICT, response.text

    rows = committed_audit_rows(db_session, "LOGIN_BLOCKED")
    assert len(rows) == 1
    assert rows[0].success == "false"  # AuditLog.success is a String(10) column
    assert rows[0].error_message == "Email resolves to more than one account"


def test_an_unambiguous_login_still_succeeds(client: TestClient, db_session: Session):
    """Positive control. The refusal must be triggered by the DUPLICATE, not by the mere
    existence of other tenants -- an address held by exactly one company logs in normally
    even while a same-named-but-different address exists elsewhere."""
    user = make_user(db_session, company_id=COMPANY_A, email="unique@customer.example.com", password=PASSWORD)
    make_user(db_session, company_id=COMPANY_B, email="unique@othercustomer.example.com", password=PASSWORD)

    response = client.post(LOGIN_URL, data={"username": "unique@customer.example.com", "password": PASSWORD})

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["access_token"]
    db_session.expire_all()
    assert db_session.get(User, user.id).failed_login_attempts == 0


def test_the_legacy_domain_fallback_refuses_when_it_is_ambiguous_too(client: TestClient, db_session: Session):
    """The second lookup, which is easy to miss.

    When the primary lookup finds nothing and the address ends ``@users.werco.com``, the
    handler retries against the legacy ``@werco.local`` form so an un-repaired imported
    account can still sign in. That probe had the same unscoped ``.first()``, so the
    ambiguity simply moved one line down; both legs are now held to one-or-refuse.
    """
    make_user(db_session, company_id=COMPANY_A, email="jdoe@werco.local", password=PASSWORD)
    make_user(db_session, company_id=COMPANY_B, email="jdoe@werco.local", password=OTHER_PASSWORD)

    response = client.post(LOGIN_URL, data={"username": "jdoe@users.werco.com", "password": PASSWORD})

    assert response.status_code == status.HTTP_409_CONFLICT, response.text
    assert "access_token" not in response.text


def test_an_unknown_address_is_still_a_plain_401(client: TestClient, db_session: Session):
    """Shape preservation: the new 409 must not swallow the established
    "no such user" answer, or the refusal becomes its own oracle -- 409 for
    "exists twice" versus 401 for "does not exist" is already a disclosure, and
    collapsing everything into 409 would be a different, worse one."""
    make_user(db_session, company_id=COMPANY_A, email="somebody@example.com")

    response = client.post(LOGIN_URL, data={"username": "nobody-at-all@example.com", "password": PASSWORD})

    assert response.status_code == status.HTTP_401_UNAUTHORIZED, response.text
