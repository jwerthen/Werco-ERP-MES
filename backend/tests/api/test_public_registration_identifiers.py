"""Public registration takes EITHER identifier: an email, a badge, or both.

``POST /auth/register-public`` used to require an email and treat ``employee_id`` as an
optional nicety ("auto-generated if not provided"). That is backwards for the population
this system actually signs in: a shop-floor operator's real credential is the badge, and
plenty of them have no work address at all. The route now refuses only the request that
carries NEITHER identifier, and mints whichever one is missing -- both columns on
``models/user.py`` are ``nullable=False``, so a badge-only signup is stored under a
synthetic ``emp-<badge>@users.werco.com`` address rather than the column being widened.

What these tests are actually protecting
----------------------------------------
1. **The anti-enumeration control must survive the new path.** ``test_auth_identity_
   resolution.py`` pins that a taken email and a taken badge are indistinguishable from a
   fresh signup. Making email optional adds a THIRD accept shape, and the obvious way to
   get it wrong is for a badge-only duplicate to answer differently (or to skip the audit
   row that makes the attempt visible to an operator). So the badge-only refusal is
   asserted on the ``(status_code, body)`` TUPLE against a badge-only accept in the same
   test -- the same technique, and for the same reason, as that file's §1.

2. **The mint is a storage detail, not an outcome.** A minted address has to be a real,
   resolvable credential, not a placeholder: ``test_a_badge_only_account_can_sign_in_both
   _ways`` drives the account back through BOTH login routes once an admin approves it.
   Without that, "the column got a value" and "the person can sign in" are different
   claims and only the first one would be tested.

3. **Dedup scope is install-wide, deliberately.** Email is unique PER COMPANY, but this
   route is unauthenticated and its duplicate probes span every tenant. If the mint were
   company-scoped it could hand company A an address company B already holds -- the exact
   two-tenants-one-address state that makes ``_find_user_by_auth_email`` refuse 409, i.e.
   a registration that silently breaks a stranger's login. Pinned twice: once against a
   holder in another company, once end-to-end through two public registrations.

4. **An audit row must key on what the registrant SUBMITTED.** ``log_auth_event`` picks
   one identifier for ``resource_identifier``, and the handler derives whichever one the
   registrant did not supply -- so the wrong pick keys a permanent, uncorrectable row on a
   value the server invented. §6 pins that column on the ACCEPTED row for all three accept
   shapes, positively and negatively, after a regression that keyed email registrants on
   their derived badge slipped through with every other assertion here green.

A note on the collision test
----------------------------
Two badge-only *registrations* can never collide on the minted address, and the test
below is shaped around that rather than pretending otherwise. ``PublicRegister`` restricts
``employee_id`` to ``[A-Za-z0-9\\-_]+``, and every one of those characters survives the
email-local-part sanitizer, so ``sanitize(lower(badge)) == lower(badge)`` -- two badges
that mint the same address are two badges that are equal case-insensitively, and the
badge duplicate probe (also case-insensitive, also install-wide) refuses the second one
first. A mint collision therefore requires a row that already HOLDS the synthetic address
by another route: an email-only registrant who typed one, or the per-company CSV
importer, which mints the identical shape in some other tenant -- both pinned in §2.

Rate limits are real in the test environment (register-public 3/min per fixed TestClient
IP) and the autouse ``_reset_rate_limiter`` fixture gives each test a fresh budget -- so
no test below issues more than two registrations.
"""

import json

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.endpoints.auth import (
    _EMPLOYEE_ID_CANDIDATE_CAP,
    _REJECTED_EMAIL_UNDERIVABLE,
    _REJECTED_EMPLOYEE_ID_UNDERIVABLE,
    _REJECTED_EMPLOYEE_ID_UNRESOLVABLE,
    _REJECTED_IDENTIFIER_TAKEN,
    _normalize_employee_id,
)
from app.core.security import get_password_hash
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.user import User, UserRole
from app.services.user_identity import (
    MAX_IDENTIFIER_CANDIDATES,
    SYNTHETIC_EMAIL_DOMAIN,
    IdentifierDerivationExhausted,
    employee_id_from_email,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1  # the seeded company
COMPANY_B = 2

REGISTER_URL = "/api/v1/auth/register-public"
LOGIN_URL = "/api/v1/auth/login"
EMPLOYEE_LOGIN_URL = "/api/v1/auth/employee-login"

PASSWORD = "SecureP@ss123!"

# The one body every non-bootstrap registration outcome must return.
PENDING_BODY = {"message": "Account submitted for approval", "is_first_user": False}
BOOTSTRAP_BODY = {"message": "Admin account created successfully", "is_first_user": True}

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
    company_id: int = COMPANY_A,
    email: str = None,
    employee_id: str = None,
    is_active: bool = True,
) -> User:
    """Seed a committed user. Used to defeat the first-user bootstrap branch, and to
    stand in for rows that reached the DB by some route other than this endpoint.

    THE DEFAULT BADGE SITS IN A RESERVED NORMALIZED BAND (``71xx``), and that is a
    correctness requirement of the fixture, not a decoration. ``_employee_id_taken`` now
    refuses a registration whose badge collides with an existing row under
    ``_normalize_employee_id`` -- four trailing digits, zero-padded -- so a seed badge is
    no longer inert background: it OCCUPIES a slot in the ~10^4 keyspace every badge in
    this file is registered into. The old ``REGIDENT-{n:05d}`` normalized to ``0001``,
    ``0002``, ... which silently collided with ``Keep-ME_01`` (-> ``0001``) and turned an
    accept-shape test into a refusal. Keep new badges in this file clear of ``71xx``, and
    keep this band clear of anything a test registers.
    """
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=email or f"reg-ident-{n}@co{company_id}.example.com",
        employee_id=employee_id or f"REGIDENT-{7100 + n:05d}",  # -> normalizes to 71xx; see the docstring
        first_name="Seed",
        last_name="User",
        hashed_password=get_password_hash(PASSWORD),
        role=UserRole.ADMIN,
        is_active=is_active,
        is_superuser=False,
        company_id=company_id,
        failed_login_attempts=0,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def payload(*, email: str = None, employee_id: str = None, first: str = "New", last: str = "Signup") -> dict:
    """Build a registration body carrying only the identifiers explicitly asked for.

    Omission matters: the point of the feature is that the KEY can be absent, not that it
    can be sent empty (an empty ``email`` fails ``EmailStr`` and an empty ``employee_id``
    fails the field pattern -- both 422 on the field, never reaching the new validator).
    """
    body = {"first_name": first, "last_name": last, "password": PASSWORD}
    if email is not None:
        body["email"] = email
    if employee_id is not None:
        body["employee_id"] = employee_id
    return body


def user_by_badge(db: Session, employee_id: str) -> User:
    db.expire_all()
    return db.query(User).filter(func.lower(User.employee_id) == employee_id.lower()).one()


def committed_audit_rows(db: Session, action: str):
    """Audit rows that survive a rollback -- i.e. rows the handler really committed.

    The ``client`` fixture shares one open transaction with the endpoint, so a merely
    flushed row would still be visible to a plain query. Borrowed from
    ``test_auth_identity_resolution.py``; call it AFTER the user-state assertions, since
    the rollback discards anything this test flushed but did not commit.
    """
    db.rollback()
    return db.query(AuditLog).filter(AuditLog.action == action).all()


# ===========================================================================
# 1. Each identifier alone is enough
# ===========================================================================


def test_badge_only_registration_mints_an_email_and_stores_the_badge_verbatim(client: TestClient, db_session: Session):
    """THE new path: a signup carrying a badge and no address at all.

    Two separate claims, and both matter. The BADGE is stored byte-for-byte including
    case -- it is what the operator types at the kiosk, and a normalizing write would
    make the printed badge and the stored one different strings. The EMAIL is derived,
    and its exact shape is load-bearing rather than cosmetic: ``_find_user_by_auth_email``
    hard-codes ``@users.werco.com`` for the legacy-address fallback, so a different
    domain here would mint addresses that route differently at login.
    """
    make_user(db_session)  # not the first user

    response = client.post(REGISTER_URL, json=payload(employee_id="EMP-0777"))

    assert (response.status_code, response.json()) == (status.HTTP_200_OK, PENDING_BODY), response.text
    created = user_by_badge(db_session, "EMP-0777")
    assert created.employee_id == "EMP-0777", "the badge is the credential; it must not be rewritten"
    assert created.email == "emp-emp-0777@users.werco.com"
    # Still an ordinary pending signup -- minting an address grants nothing.
    assert created.role == UserRole.VIEWER
    assert created.is_active is False


def test_email_only_registration_still_derives_the_badge_from_the_local_part(client: TestClient, db_session: Session):
    """Regression guard on the OLD behaviour, which the change had to leave alone.

    The derivation runs in the opposite direction and keeps CASE (the badge column is
    matched case-insensitively but stored as given), so ``JMW@...`` yields ``JMW``, not
    ``jmw``. This is also the branch that used to dereference ``user_in.email`` blindly --
    now guarded by ``if not employee_id and user_in.email`` -- so an email-only signup
    reaching here at all is half the point of the test.
    """
    make_user(db_session)

    response = client.post(REGISTER_URL, json=payload(email="JMW@wercomfg.com"))

    assert (response.status_code, response.json()) == (status.HTTP_200_OK, PENDING_BODY), response.text
    db_session.expire_all()
    created = db_session.query(User).filter(func.lower(User.email) == "jmw@wercomfg.com").one()
    assert created.employee_id == "JMW"


def test_registration_with_both_identifiers_stores_both_verbatim(client: TestClient, db_session: Session):
    """Nothing is derived when nothing is missing. The mint sits behind
    ``if not normalized_email`` and the badge derivation behind ``if not employee_id``,
    so a request carrying both must come out the other side untouched -- an eager mint
    would silently replace an address the registrant actually reads mail at."""
    make_user(db_session)

    response = client.post(REGISTER_URL, json=payload(email="both.hands@wercomfg.com", employee_id="Keep-ME_01"))

    assert (response.status_code, response.json()) == (status.HTTP_200_OK, PENDING_BODY), response.text
    created = user_by_badge(db_session, "Keep-ME_01")
    assert created.email == "both.hands@wercomfg.com"
    assert created.employee_id == "Keep-ME_01"


def test_registration_with_neither_identifier_is_refused_422_naming_both(client: TestClient, db_session: Session):
    """A password and a name is not a registrable account.

    Refused by ``PublicRegister.require_an_identifier`` -- a 422 on the request SHAPE --
    which is deliberately upstream of the handler: the handler's duplicate probes are the
    thing that must never become an account-existence oracle, and a request that cannot
    name an account has no business reaching them. The message has to name BOTH ways out,
    since the caller cannot tell from a bare "field required" which field to fill.
    """
    make_user(db_session)
    before = db_session.query(User).count()

    response = client.post(REGISTER_URL, json=payload())

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, response.text
    messages = " ".join(item.get("msg", "") for item in response.json()["detail"])
    # Asserted on the message itself, not on a substring of the whole body -- FastAPI
    # echoes the submitted `input` back inside `detail`, so a body-wide search could be
    # satisfied by the caller's own request rather than by anything the server said.
    assert "Provide an email address or an employee ID" in messages, response.text
    db_session.expire_all()
    assert db_session.query(User).count() == before, "a refused request must insert nothing"


# ===========================================================================
# 2. The mint has to dedup, and in the right scope
# ===========================================================================


def test_a_minted_address_already_held_gains_the_suffix_and_both_accounts_survive(
    client: TestClient, db_session: Session
):
    """Two public registrations that end up fighting over one address.

    The first registrant simply types an address that happens to be the synthetic shape
    -- ``@users.werco.com`` is a real, registerable domain as far as this route is
    concerned, and nothing reserves it. The second is a badge-only signup whose mint
    lands on that exact string. The suffix sequence (``-2``, ``-3``, ...) is what keeps
    the second registration from either failing on the unique constraint or, worse,
    succeeding into a duplicate address that would later make BOTH accounts unable to log
    in by email (``_find_user_by_auth_email`` refuses 409 on ambiguity).

    THE FIRST REGISTRANT SUPPLIES A BADGE, and it has to. Left to derive one, the handler
    would take it from the address's local part -- ``emp-emp-4242`` -- which normalizes to
    the same four digits as the second registrant's ``EMP-4242``. The badge-collision
    refusal (§7) would then reject the second registration before the mint ran at all, and
    this test would be asserting the refusal path while claiming to assert the suffix walk.
    ``OFFICE-TYPIST`` carries no digits, so ``_normalize_employee_id`` returns ``None`` for
    it and it provably cannot occupy a slot in the badge keyspace -- which isolates the
    ADDRESS collision this test is about. (That the two rules interact at all is the point
    of §7's own coverage; here it would only be noise.)

    Two requests; the budget is three.
    """
    make_user(db_session)  # not the first user

    first = client.post(REGISTER_URL, json=payload(email="emp-emp-4242@users.werco.com", employee_id="OFFICE-TYPIST"))
    second = client.post(REGISTER_URL, json=payload(employee_id="EMP-4242"))

    assert (first.status_code, first.json()) == (status.HTTP_200_OK, PENDING_BODY), first.text
    assert (second.status_code, second.json()) == (status.HTTP_200_OK, PENDING_BODY), second.text

    db_session.expire_all()
    typed = db_session.query(User).filter(func.lower(User.email) == "emp-emp-4242@users.werco.com").one()
    minted = user_by_badge(db_session, "EMP-4242")
    assert minted.email == "emp-emp-4242-2@users.werco.com"
    assert typed.id != minted.id, "both registrations must have produced a row"
    assert typed.email != minted.email


def test_the_mint_dedups_install_wide_not_per_company(client: TestClient, db_session: Session):
    """The scope claim, driven from another tenant.

    ``emp-<badge>@users.werco.com`` is exactly what the user CSV importer
    (``api/endpoints/users.py::_generated_email``) mints, and that importer dedups
    PER COMPANY -- so company B legitimately holds this address without company A ever
    having seen it. A company-scoped probe here would mint the same string again and
    leave one address resolving to two accounts, which is the state email login refuses
    409 on. Both users would then be unable to sign in by email, neither having done
    anything wrong.
    """
    holder = make_user(db_session, company_id=COMPANY_B, email="emp-emp-5150@users.werco.com", employee_id="IMPORTED-1")

    response = client.post(REGISTER_URL, json=payload(employee_id="EMP-5150"))

    assert (response.status_code, response.json()) == (status.HTTP_200_OK, PENDING_BODY), response.text
    created = user_by_badge(db_session, "EMP-5150")
    assert created.company_id != holder.company_id
    assert created.email == "emp-emp-5150-2@users.werco.com", "the mint probed only its own tenant"


# ===========================================================================
# 3. The anti-enumeration control, on the new path
# ===========================================================================


def test_a_taken_badge_without_an_email_is_refused_indistinguishably_and_recorded(
    client: TestClient, db_session: Session
):
    """THE control test for the badge-only path, asserted on the tuple.

    Badge numbers are short, sequential and printed on the badge, so confirming one is a
    far cheaper attack than confirming an address -- and the badge-only shape is the one
    that makes the probe a single field. The refusal must therefore be byte-identical to
    an ACCEPT, which is why both are issued here and compared to each other rather than
    to a remembered literal: the assertion fails if either drifts later.

    Silent to the caller is not silent to the operator. The ``PUBLIC_REGISTRATION_REJECTED``
    row is the only channel that records the attempt, and it has to name what was tried --
    a badge-only attempt whose row read "attempt for unknown" would gut the control while
    leaving this test's response assertions green, which is precisely why the identifier
    is asserted here too. It is prefixed so a badge can never be read back as an address.

    Two requests; the budget is three.
    """
    seeded = make_user(db_session, company_id=COMPANY_B, employee_id="EMP-0999")
    before = db_session.query(User).count()

    taken_badge = client.post(REGISTER_URL, json=payload(employee_id="emp-0999"))  # case-differing, still a duplicate
    fresh_badge = client.post(REGISTER_URL, json=payload(employee_id="EMP-1000"))

    answers = [(r.status_code, r.json()) for r in (taken_badge, fresh_badge)]
    assert answers[0] == answers[1], f"badge-only outcomes are distinguishable: {answers}"
    assert answers[0] == (status.HTTP_200_OK, PENDING_BODY)

    db_session.expire_all()
    assert db_session.query(User).count() == before + 1, "only the fresh badge may insert"
    assert db_session.query(User).filter(func.lower(User.employee_id) == "emp-0999").one().id == seeded.id
    assert db_session.query(User).filter(User.email.like("emp-emp-0999%")).count() == 0, "no address was minted"
    assert user_by_badge(db_session, "EMP-1000").email == "emp-emp-1000@users.werco.com"

    rows = committed_audit_rows(db_session, "PUBLIC_REGISTRATION_REJECTED")
    assert len(rows) == 1
    assert rows[0].success == "false"  # AuditLog.success is a String(10) column
    assert rows[0].resource_identifier == "employee-id:emp-0999", "the rejected attempt must name what was tried"


# ===========================================================================
# 4. Bootstrap, and the round trip
# ===========================================================================


def test_first_user_bootstrap_by_badge_alone_creates_the_platform_admin(client: TestClient, db_session: Session):
    """A PIN ON THE PATH THE CHANGE MUST NOT BREAK.

    The one-time setup branch returns a different body on purpose and is the only way a
    fresh install gets its first admin. It now has to work for a founder who registers
    with a badge and no address -- and the mint runs AFTER the duplicate refusal but
    BEFORE this branch, so an ordering slip would either skip the address entirely (an
    insert that violates NOT NULL) or leave the bootstrap admin pending approval, locking
    the install out of itself.
    """
    assert db_session.query(User).count() == 0, "this test needs the empty-users precondition"

    response = client.post(REGISTER_URL, json=payload(employee_id="FOUNDER-1", first="Ada", last="Lovelace"))

    assert (response.status_code, response.json()) == (status.HTTP_200_OK, BOOTSTRAP_BODY), response.text
    created = user_by_badge(db_session, "FOUNDER-1")
    assert created.role == UserRole.PLATFORM_ADMIN
    assert created.is_superuser is True
    assert created.is_active is True, "the bootstrap admin must not be left pending approval"
    assert created.email == "emp-founder-1@users.werco.com", "a real minted address, not a placeholder"


def test_a_badge_only_account_can_sign_in_both_ways_once_approved(client: TestClient, db_session: Session):
    """The round trip -- the claim that "the column got a value" does not make on its own.

    A minted address that no login path resolves would be a placeholder, and the feature
    would be a way to create accounts nobody can use. So the account is approved the way
    an admin would and then driven through BOTH doors: the badge (``/auth/employee-login``,
    the kiosk path this population actually uses) and the minted address plus the password
    they chose (``/auth/login``).

    The email leg carries a second assertion worth keeping: ``_ensure_valid_auth_email``
    runs on every successful login and REWRITES reserved ``@werco.local`` addresses in
    place. ``@users.werco.com`` is the repair's OUTPUT, not its input, so a minted address
    must survive a login untouched -- if that ever inverted, every badge-only account
    would walk its own address one suffix further on each sign-in.
    """
    make_user(db_session)
    registered = client.post(REGISTER_URL, json=payload(employee_id="EMP-7788"))
    assert registered.status_code == status.HTTP_200_OK, registered.text

    created = user_by_badge(db_session, "EMP-7788")
    minted_email = created.email
    assert minted_email == "emp-emp-7788@users.werco.com"
    created.is_active = True  # what admin approval does
    db_session.commit()

    by_badge = client.post(EMPLOYEE_LOGIN_URL, json={"employee_id": "EMP-7788"})
    assert by_badge.status_code == status.HTTP_200_OK, by_badge.text
    assert by_badge.json()["access_token"]

    by_email = client.post(LOGIN_URL, data={"username": minted_email, "password": PASSWORD})
    assert by_email.status_code == status.HTTP_200_OK, by_email.text
    assert by_email.json()["access_token"]

    db_session.expire_all()
    assert db_session.get(User, created.id).email == minted_email, "login rewrote a minted address"


def test_a_badge_only_registrant_signs_in_with_the_badge_and_the_password_they_chose(
    client: TestClient, db_session: Session
):
    """THE end-to-end claim, and the reason the two changes shipped together.

    ``POST /auth/login`` now resolves a badge as well as an address, so a badge-only
    signup can use the door they expect: the number on their badge plus the password they
    typed into the registration form. Before that, this account's only working credential
    was the minted ``emp-...@users.werco.com`` address -- a string the registrant never
    chose, never saw and could not be told over the phone without spelling it -- or the
    passwordless kiosk route, which is not available at an office keyboard.

    Everything here is driven through HTTP end to end (register -> approve -> sign in)
    rather than seeding a row, because the two halves are only useful if the address the
    REGISTRATION minted is the row the LOGIN resolves. Seeding a user would test the login
    resolver against a fixture and prove nothing about the pair.
    """
    make_user(db_session)  # defeat the first-user bootstrap branch

    registered = client.post(REGISTER_URL, json=payload(employee_id="EMP-3311"))
    assert registered.status_code == status.HTTP_200_OK, registered.text

    created = user_by_badge(db_session, "EMP-3311")
    assert created.is_active is False, "a public signup starts pending approval"

    # The password is real: it cannot be used until an admin approves the account.
    before_approval = client.post(LOGIN_URL, data={"username": "EMP-3311", "password": PASSWORD})
    assert before_approval.status_code == status.HTTP_403_FORBIDDEN, before_approval.text

    created.is_active = True  # what admin approval does
    db_session.commit()

    signed_in = client.post(LOGIN_URL, data={"username": "EMP-3311", "password": PASSWORD})
    assert signed_in.status_code == status.HTTP_200_OK, signed_in.text
    assert signed_in.json()["access_token"] and signed_in.json()["refresh_token"]

    # The badge -- not the minted address -- is what the sign-in is recorded under, so the
    # registration row and the login row for this person join on the same identifier.
    db_session.expire_all()
    assert db_session.get(User, created.id).email == "emp-emp-3311@users.werco.com"
    rows = committed_audit_rows(db_session, "LOGIN_SUCCESS")
    assert [row.resource_identifier for row in rows] == ["employee-id:EMP-3311"]


# ===========================================================================
# 5. Each identifier is recorded under its OWN key
# ===========================================================================
#
# ``log_auth_event`` grew an ``employee_id=`` parameter because a badge-only registrant
# has no address to key on and the badge was previously passed through ``email=`` -- which
# landed it in ``extra_data`` under a key literally named "email". That mislabel is
# PERMANENT: migrations 008/060 install triggers that refuse UPDATE and DELETE on
# ``audit_logs``, and invariant 2 forbids backfilling rows out of band, so there is no
# correcting pass anyone could ever run. A consumer filtering ``extra_data->>'email'`` for
# addresses would silently swallow every badge-only row -- and those are precisely the rows
# the anti-enumeration design depends on being readable, since the HTTP response is
# deliberately identical for a refusal and an accept (§3).
#
# So the assertions below are on the exact ``extra_data`` DICT, not on a key lookup: the
# failure being guarded against is a badge sitting under the wrong key, and
# ``extra_data["employee_id"] == badge`` alone stays green while "email" also carries it.


def audit_row_contents(row) -> str:
    """Every operator-readable field of an audit row, flattened for substring checks."""
    return json.dumps(
        {
            "resource_identifier": row.resource_identifier,
            "description": row.description,
            "error_message": row.error_message,
            "old_values": row.old_values,
            "new_values": row.new_values,
            "extra_data": row.extra_data,
            "user_email": row.user_email,
            "user_name": row.user_name,
        },
        default=str,
    )


def assert_carries_no_secret_material(row, label: str):
    """The audit log is read by operators and exported; a registration row must name WHO
    tried, never WHAT they typed as a password. Checked on every field at once because
    the risk is a future maintainer widening ``extra_data`` to "the submitted body"."""
    blob = audit_row_contents(row)
    assert PASSWORD not in blob, f"{label} row leaks the submitted password"
    # bcrypt's identifier, in case the HASH is ever attached instead of the plaintext.
    assert "$2b$" not in blob and "$2a$" not in blob, f"{label} row leaks a password hash"
    assert "password" not in blob.lower(), f"{label} row mentions a password field"


def test_a_badge_only_attempt_is_recorded_under_employee_id_and_the_two_rows_join(
    client: TestClient, db_session: Session
):
    """One badge, both halves of its history -- the accept and the later duplicate refusal.

    The same badge is registered and then registered again, so ONE identifier produces a
    ``PUBLIC_REGISTRATION`` row and a ``PUBLIC_REGISTRATION_REJECTED`` row. That pairing is
    the point: without ``employee_id=`` on the accepted call the accepted row keys on
    ``user.email`` -- a MINTED ``emp-...@users.werco.com`` address that the registrant never
    typed and an investigator has no reason to search for -- leaving the two halves sharing
    no queryable key at all. Here they join on ``extra_data['employee_id']``.

    ``resource_identifier`` is asserted in its unchanged shape too: prefixed
    ``employee-id:`` so a badge can never be read back as an address by a consumer that
    only knows this column.

    Two requests; the budget is three.
    """
    make_user(db_session)  # not the first user -- the bootstrap branch answers differently
    before = db_session.query(User).count()

    accepted = client.post(REGISTER_URL, json=payload(employee_id="EMP-3301"))
    refused = client.post(REGISTER_URL, json=payload(employee_id="EMP-3301"))

    answers = [(r.status_code, r.json()) for r in (accepted, refused)]
    assert answers[0] == answers[1] == (status.HTTP_200_OK, PENDING_BODY), answers
    db_session.expire_all()
    assert db_session.query(User).count() == before + 1, "the second attempt must not insert"

    rejected_rows = committed_audit_rows(db_session, "PUBLIC_REGISTRATION_REJECTED")
    accepted_rows = committed_audit_rows(db_session, "PUBLIC_REGISTRATION")
    assert len(rejected_rows) == 1 and len(accepted_rows) == 1

    rejected, created = rejected_rows[0], accepted_rows[0]
    assert rejected.resource_identifier == "employee-id:EMP-3301"
    assert rejected.extra_data == {"employee_id": "EMP-3301"}, "the badge must not travel under an 'email' key"
    assert "email" not in rejected.extra_data
    assert created.extra_data == {"employee_id": "EMP-3301"}
    # THE join. An operator asking "what happened with badge EMP-3301" gets both rows from
    # one predicate, which is the whole reason the parameter exists.
    assert created.extra_data["employee_id"] == rejected.extra_data["employee_id"] == "EMP-3301"

    for label, row in (("accepted", created), ("rejected", rejected)):
        assert_carries_no_secret_material(row, label)


def test_an_email_only_attempt_records_exactly_what_it_always_did(client: TestClient, db_session: Session):
    """The other direction, pinned as a REGRESSION guard rather than a new claim.

    Adding a second identifier key is the kind of change that quietly restructures the
    first one -- e.g. by always emitting both keys with a ``None`` for the missing half,
    which would make every pre-change row and every post-change row differently shaped for
    no gain. An email-only refusal must still produce ``{"email": ...}`` and nothing else,
    and its ``resource_identifier`` must still be the bare address with no ``employee-id:``
    prefix in front of it.

    The badge IS derived for this request (``employee_id_from_email``), which is exactly
    why the absence matters: the derived value is a storage detail the registrant never
    supplied, and recording it would put a badge nobody chose into a permanent row.

    One request; the budget is three.
    """
    make_user(db_session, email="dup-keys@example.com")
    before = db_session.query(User).count()

    response = client.post(REGISTER_URL, json=payload(email="dup-keys@example.com"))

    assert (response.status_code, response.json()) == (status.HTTP_200_OK, PENDING_BODY), response.text
    db_session.expire_all()
    assert db_session.query(User).count() == before, "a refused registration must insert nothing"

    rows = committed_audit_rows(db_session, "PUBLIC_REGISTRATION_REJECTED")
    assert len(rows) == 1
    assert rows[0].resource_identifier == "dup-keys@example.com"
    assert "employee-id:" not in (rows[0].resource_identifier or "")
    assert rows[0].extra_data == {"email": "dup-keys@example.com"}, "byte-identical to the pre-change shape"
    assert "employee_id" not in rows[0].extra_data, "the DERIVED badge is not something the caller submitted"
    assert_carries_no_secret_material(rows[0], "rejected")


# ===========================================================================
# 6. The ACCEPTED row keys on what the registrant SUBMITTED
# ===========================================================================
#
# ``log_auth_event`` resolves
#     resource_identifier = email or f"employee-id:{employee_id}" or user.email
# so the accepted-registration call has to pass BOTH identifiers for that chain to land
# on a submitted value in every shape. Passing only ``employee_id=`` keys an EMAIL
# registrant's row on the badge ``employee_id_from_email`` DERIVED -- a string the
# registrant never typed and an investigator has no reason to search for. That is a real
# regression this file did not catch: §5 asserts ``extra_data`` on the accepted row and
# stays green either way, and nothing pinned ``resource_identifier`` on an ACCEPT at all.
#
# The mirror-image mistake is the tempting "fix": reordering the chain so ``user.email``
# comes before the badge. That keys a BADGE-only registrant's row on the minted
# ``emp-...@users.werco.com`` address -- again a value the server invented, again the
# column an operator searches. So each shape below is pinned POSITIVELY and NEGATIVELY,
# the negative naming the derived-or-minted string that must not appear. Both halves have
# to hold at once; either alone is satisfiable by a broken chain.
#
# This is permanent either way: migrations 008/060 refuse UPDATE and DELETE on
# ``audit_logs`` and invariant 2 forbids backfilling, so a row written under the wrong key
# stays wrong forever and no corrective pass exists.
#
# ``extra_data`` is asserted alongside each, unchanged, for two reasons. It must carry the
# badge under ``employee_id`` -- that key is what joins an accepted row to the later
# ``PUBLIC_REGISTRATION_REJECTED`` row for the same badge (§5), and it is where the badge
# stays recoverable on the shapes where the address wins the identifier. And it must carry
# NO ``email`` key: ``log_auth_event`` gates that on ``if email and not user``, and an
# accepted registration always has a user row, so an ``email`` key appearing here would
# mean the guard had been loosened rather than that the row got richer.


def test_the_accepted_row_for_an_email_registrant_keys_on_the_submitted_address(
    client: TestClient, db_session: Session
):
    """THE regression, in the shape it actually shipped in.

    An email-only signup has its badge DERIVED (``employee_id_from_email``). With only
    ``employee_id=`` passed, the accepted row read ``employee-id:opalrivera`` -- a badge
    the registrant never chose, on the account of someone who registered by address. The
    submitted address has to win whenever there is one.

    One request; the budget is three.
    """
    make_user(db_session)  # not the first user -- the bootstrap branch logs a different action

    response = client.post(REGISTER_URL, json=payload(email="opal.rivera@wercomfg.com"))

    assert (response.status_code, response.json()) == (status.HTTP_200_OK, PENDING_BODY), response.text
    db_session.expire_all()
    created = db_session.query(User).filter(func.lower(User.email) == "opal.rivera@wercomfg.com").one()
    derived_badge = created.employee_id
    assert derived_badge == "opalrivera", "precondition: the badge is derived here, never submitted"

    rows = committed_audit_rows(db_session, "PUBLIC_REGISTRATION")
    assert len(rows) == 1
    row = rows[0]
    assert row.success == "true"  # AuditLog.success is a String(10) column
    assert row.resource_identifier == "opal.rivera@wercomfg.com"
    # The negative, stated separately: the derived badge must not be what the row keys on.
    assert row.resource_identifier != f"employee-id:{derived_badge}"
    assert not (row.resource_identifier or "").startswith(
        "employee-id:"
    ), "an address was submitted; the row must not be keyed to a badge"
    assert row.extra_data == {"employee_id": derived_badge}, "unchanged: the join key, and only it"
    assert "email" not in row.extra_data, "log_auth_event excludes it when a user row exists"
    assert_carries_no_secret_material(row, "accepted")


def test_the_accepted_row_for_a_badge_registrant_keys_on_the_badge_not_the_minted_address(
    client: TestClient, db_session: Session
):
    """The same claim from the other side -- and the guard against "just reorder the chain".

    A badge-only signup has its address MINTED. Dropping ``employee_id=`` and letting
    ``user.email`` answer instead would key this row on ``emp-emp-8814@users.werco.com``,
    a string the server invented, while leaving the email-only test above green. Both
    tests exist so no single ordering of the fallback chain can satisfy one by breaking
    the other.

    One request; the budget is three.
    """
    make_user(db_session)

    response = client.post(REGISTER_URL, json=payload(employee_id="EMP-8814"))

    assert (response.status_code, response.json()) == (status.HTTP_200_OK, PENDING_BODY), response.text
    created = user_by_badge(db_session, "EMP-8814")
    minted_email = created.email
    assert minted_email == "emp-emp-8814@users.werco.com", "precondition: the address is minted here"

    rows = committed_audit_rows(db_session, "PUBLIC_REGISTRATION")
    assert len(rows) == 1
    row = rows[0]
    assert row.success == "true"
    assert row.resource_identifier == "employee-id:EMP-8814"
    # The negative: no minted address, under any spelling, on a row whose registrant
    # supplied only a badge.
    assert row.resource_identifier != minted_email
    assert "@" not in (row.resource_identifier or ""), "the minted address must not surface as the identifier"
    assert row.extra_data == {"employee_id": "EMP-8814"}
    assert "email" not in row.extra_data
    assert_carries_no_secret_material(row, "accepted")


def test_the_accepted_row_for_a_registrant_who_supplied_both_keys_on_the_address(
    client: TestClient, db_session: Session
):
    """Precedence, pinned where nothing is derived and both answers are defensible.

    Both identifiers were submitted, so neither would be a server invention -- but the
    chain has to resolve to exactly one, and the address is it. Nothing is lost by that:
    the badge stays queryable in ``extra_data['employee_id']``, which is the key the
    refusal rows join on, so an operator searching either identifier still finds this row.

    One request; the budget is three.
    """
    make_user(db_session)

    response = client.post(REGISTER_URL, json=payload(email="dora.chen@wercomfg.com", employee_id="EMP-9002"))

    assert (response.status_code, response.json()) == (status.HTTP_200_OK, PENDING_BODY), response.text
    created = user_by_badge(db_session, "EMP-9002")
    assert (created.email, created.employee_id) == ("dora.chen@wercomfg.com", "EMP-9002"), "both stored verbatim"

    rows = committed_audit_rows(db_session, "PUBLIC_REGISTRATION")
    assert len(rows) == 1
    row = rows[0]
    assert row.success == "true"
    assert row.resource_identifier == "dora.chen@wercomfg.com"
    assert row.resource_identifier != "employee-id:EMP-9002", "the submitted address wins when there is one"
    assert row.extra_data == {"employee_id": "EMP-9002"}, "the badge stays recoverable under its own key"
    assert "email" not in row.extra_data
    assert_carries_no_secret_material(row, "accepted")


# ===========================================================================
# 7. A registration cannot POISON an existing operator's badge
# ===========================================================================
#
# The duplicate probe used to compare badges EXACTLY, while both login resolvers compare
# them under ``_normalize_employee_id`` -- four trailing digits, zero-padded. A write path
# that tests collisions differently from how the read path resolves them does not have a
# false-negative problem; it has a REMOTE DENIAL-OF-SERVICE problem, reachable by an
# unauthenticated caller:
#
#   1. a real operator holds ``EMP-0339``;
#   2. anyone registers ``00339`` -- exact comparison says "free", so the row inserts;
#   3. from that moment ``0339`` normalizes to TWO rows, so ``_find_user_by_employee_id``
#      refuses 409 and the operator is off ``/auth/login``, ``/auth/employee-login``, the
#      kiosk and the crew station at once. Nothing in the system reports the collision;
#      the operator just sees a badge that stopped working.
#
# The attack costs one unauthenticated request per badge and the account it disables was
# never touched -- so the tests below assert the OPERATOR'S SIGN-IN, not merely that no
# row was inserted. "No row" is the mechanism; "the operator still gets in" is the harm.
#
# The refusal stays invisible to the caller, and that is deliberate rather than a
# compromise: widening what counts as "taken" only grows the set of inputs that silently
# do nothing, and the response is the uniform pending body either way. Asserted on the
# ``(status_code, body)`` TUPLE against a fresh-badge registration in the same test, the
# way §1/§3 already do it, because an oracle added here would be worth more to an attacker
# than the collision it refuses.


def test_a_badge_colliding_under_normalization_is_refused_indistinguishably(client: TestClient, db_session: Session):
    """THE poisoning refusal, next to an accept it must be byte-identical to.

    ``EMP-1339`` is the control arm and it is chosen, not arbitrary: it CONTAINS ``339``,
    so it survives the resolver's ``ilike('%339%')`` narrowing exactly like the colliding
    badge does, and it is refused nothing. That pins the refusal to the NORMALIZED value
    rather than to a substring match -- a probe written against the SQL narrowing instead
    of against ``_normalize_employee_id`` would reject this registration too, and every
    other assertion here would stay green.

    Two requests; the budget is three.
    """
    operator = make_user(db_session, employee_id="EMP-0339")
    assert _normalize_employee_id("00339") == _normalize_employee_id("EMP-0339") == "0339", "precondition"
    assert _normalize_employee_id("EMP-1339") == "1339", "the control arm must NOT collide"
    before = db_session.query(User).count()

    poisoning = client.post(REGISTER_URL, json=payload(employee_id="00339"))
    fresh = client.post(REGISTER_URL, json=payload(employee_id="EMP-1339"))

    answers = [(r.status_code, r.json()) for r in (poisoning, fresh)]
    assert answers[0] == answers[1], f"a colliding badge is distinguishable from a fresh one: {answers}"
    assert answers[0] == (status.HTTP_200_OK, PENDING_BODY)

    db_session.expire_all()
    assert db_session.query(User).count() == before + 1, "only the non-colliding badge may insert"
    assert db_session.query(User).filter(func.lower(User.employee_id) == "00339").count() == 0
    assert user_by_badge(db_session, "EMP-1339").id != operator.id
    # No address was minted for the refused attempt either -- the mint runs after the
    # refusal, so a row appearing here would mean the ordering had been inverted.
    assert db_session.query(User).filter(User.email.like("emp-00339%")).count() == 0

    rows = committed_audit_rows(db_session, "PUBLIC_REGISTRATION_REJECTED")
    assert len(rows) == 1
    assert rows[0].resource_identifier == "employee-id:00339", "the refused attempt must name what was tried"
    assert rows[0].success == "false"  # AuditLog.success is a String(10) column


def test_the_operator_whose_badge_was_targeted_can_still_sign_in_both_ways(client: TestClient, db_session: Session):
    """THE HARM, asserted as an outcome rather than as the absence of a row.

    A test that stopped at "no user was created" would stay green under a fix that
    inserted the row but taught only ONE resolver to tolerate it -- and the operator would
    still be locked out of the other door. So the badge is driven through both: the
    password route (``/auth/login``, the office keyboard) and the passwordless kiosk route
    (``/auth/employee-login``), using the NORMALIZED spelling ``0339`` that the scanner
    produces and that the poisoning row would have made ambiguous.

    Both logins are resolved through the normalized fallback, not an exact match -- the
    operator's stored badge is ``EMP-0339`` -- which is precisely the code path a second
    row normalizing to ``0339`` would turn into a 409.
    """
    operator = make_user(db_session, employee_id="EMP-0339")

    refused = client.post(REGISTER_URL, json=payload(employee_id="00339"))
    assert (refused.status_code, refused.json()) == (status.HTTP_200_OK, PENDING_BODY), refused.text

    by_password = client.post(LOGIN_URL, data={"username": "0339", "password": PASSWORD})
    assert by_password.status_code == status.HTTP_200_OK, by_password.text
    assert by_password.json()["user"]["id"] == operator.id, "the badge resolved to somebody else"

    by_badge = client.post(EMPLOYEE_LOGIN_URL, json={"employee_id": "0339"})
    assert by_badge.status_code == status.HTTP_200_OK, by_badge.text
    assert by_badge.json()["access_token"]


def test_a_derived_badge_that_would_collide_steps_past_it_instead_of_poisoning(client: TestClient, db_session: Session):
    """The same attack through the EMAIL field, and the reason one shared predicate matters.

    An email-only signup never submits a badge -- the handler derives one from the local
    part -- so ``0339@attacker.example.com`` yields the badge ``0339`` and lands the
    identical collision without the caller ever naming a badge. The fix works here because
    ``employee_id_from_email`` is handed the SAME ``_employee_id_taken`` predicate: the
    collision is seen, and the derivation steps away from it.

    So this registration SUCCEEDS, and that is the correct outcome -- refusing it would
    punish a legitimate registrant for an address that merely starts with someone's badge
    digits.

    WHERE IT STEPS TO CHANGED, deliberately, and this test was updated with it. It used to
    land on ``0339-2`` -- a different slot in the SAME four-digit keyspace (``3392``), which
    is only safe as long as nobody holds that badge either. The derivation now leaves the
    keyspace altogether: a base carrying digits that comes back taken drops to the sanitized
    local part with its digits stripped, which here is empty, so the ``user`` fallback is
    what the account gets. ``_normalize_employee_id`` maps it to ``None``, so it cannot
    collide with ANY badge under normalization rather than merely missing this one.
    """
    operator = make_user(db_session, employee_id="EMP-0339")

    response = client.post(REGISTER_URL, json=payload(email="0339@attacker.example.com"))

    assert (response.status_code, response.json()) == (status.HTTP_200_OK, PENDING_BODY), response.text
    db_session.expire_all()
    created = db_session.query(User).filter(func.lower(User.email) == "0339@attacker.example.com").one()
    assert created.employee_id == "user", "the derivation must step out of the badge keyspace, not into it"
    assert _normalize_employee_id(created.employee_id) is None
    assert _normalize_employee_id(created.employee_id) != _normalize_employee_id(operator.employee_id)

    # The operator's badge still resolves to exactly one row -- the claim the 409 would break.
    signed_in = client.post(LOGIN_URL, data={"username": "0339", "password": PASSWORD})
    assert signed_in.status_code == status.HTTP_200_OK, signed_in.text
    assert signed_in.json()["user"]["id"] == operator.id


# ===========================================================================
# 8. A refusal the query never established is recorded as such -- permanently
# ===========================================================================
#
# ``_employee_id_taken_reason`` has TWO ways to say "do not use this badge", and they are
# different facts:
#
#   * ``_TAKEN_COLLISION``  -- a colliding row was FOUND;
#   * ``_TAKEN_UNRESOLVABLE`` -- the collision probe reused the resolvers' capped query,
#     the candidate window TRUNCATED, and it refused to answer. Nothing was established
#     about uniqueness at all.
#
# Both refuse the insert, and both return the uniform pending body -- the caller cannot
# tell them apart and must not be able to (§3/§7). The AUDIT ROW is where the distinction
# has to survive, and the reason it is worth a section of its own is that it is permanent:
# migrations 008/060 refuse UPDATE and DELETE on ``audit_logs`` and invariant 2 forbids
# backfilling, so whichever sentence is written is the one an admin reads forever. "Already
# in use" for a truncated window is a claim nobody checked, and it sends that admin hunting
# a duplicate that may not exist instead of at a user table the resolver can no longer scan.
#
# The route already draws exactly this line for the HTTP response
# (``_UNRESOLVABLE_EMPLOYEE_ID_DETAIL`` vs ``_AMBIGUOUS_EMPLOYEE_ID_DETAIL``); these tests
# carry it onto the record that cannot be corrected.


def _seed_badge_noise(db: Session, count: int, *, company_id: int = COMPANY_A, start: int = 2000) -> None:
    """Bulk-seed users whose badges all match ``%1%`` but normalize AWAY from ``0001``.

    ``SHOP-1<n>`` for n = 2000, 2001, ... : every badge contains a ``1`` so it survives the
    collision probe's SQL narrowing (that is the point -- these ARE the candidate window),
    while its four trailing digits are ``2000``, ``2001``, ... so none is a real match.
    They never authenticate, so they carry a literal hash rather than paying bcrypt 500
    times. Mirrors the helper in ``test_auth_badge_password_login.py`` -- same window, read
    from the write path instead of the read path.
    """
    _ensure_company(db, company_id)
    db.add_all(
        [
            User(
                email=f"regnoise-{company_id}-{n}@co{company_id}.example.com",
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


def test_a_truncated_collision_probe_refuses_and_records_a_DIFFERENT_cause(client: TestClient, db_session: Session):
    """THE FIX: the two refusals are indistinguishable to the caller and distinct in the log.

    One test covers both arms so the comparison is direct rather than across files. The
    collision arm registers a badge that genuinely duplicates a seeded operator; the
    truncation arm registers ``0001``, whose probe narrows to ``%1%`` -- one row past
    ``_EMPLOYEE_ID_CANDIDATE_CAP`` of noise makes that window incomplete, so the probe
    withholds its answer.

    Asserted in three parts:
      * the RESPONSES are equal to each other and are the uniform pending body -- widening
        what counts as unusable must not add an oracle;
      * neither badge inserted, so a truncated probe fails CLOSED (refusing an honest
        registrant costs one retry and an admin can still create the account through
        ``POST /users/``; a wrong "free" would take a real operator off the floor);
      * the two audit rows carry DIFFERENT ``error`` sentences, and the truncation one is
        explicitly not the collision one.
    """
    operator = make_user(db_session, employee_id="EMP-0339")
    _seed_badge_noise(db_session, _EMPLOYEE_ID_CANDIDATE_CAP + 1)
    before = db_session.query(User).count()

    collision = client.post(REGISTER_URL, json=payload(employee_id="00339"))
    truncated = client.post(REGISTER_URL, json=payload(employee_id="0001"))

    answers = [(r.status_code, r.json()) for r in (collision, truncated)]
    assert answers[0] == answers[1], f"the two refusals are distinguishable to the caller: {answers}"
    assert answers[0] == (status.HTTP_200_OK, PENDING_BODY)

    db_session.expire_all()
    assert db_session.query(User).count() == before, "neither refused registration may insert"
    assert db_session.query(User).filter(func.lower(User.employee_id) == "0001").count() == 0

    rows = committed_audit_rows(db_session, "PUBLIC_REGISTRATION_REJECTED")
    by_badge = {row.resource_identifier: row for row in rows}
    assert set(by_badge) == {"employee-id:00339", "employee-id:0001"}, by_badge

    collision_row = by_badge["employee-id:00339"]
    truncation_row = by_badge["employee-id:0001"]
    assert collision_row.error_message == _REJECTED_IDENTIFIER_TAKEN
    assert truncation_row.error_message == _REJECTED_EMPLOYEE_ID_UNRESOLVABLE
    assert truncation_row.error_message != collision_row.error_message, (
        "a truncated candidate window establishes NO collision -- recording it as one is a "
        "claim nobody checked, on a row that can never be corrected"
    )
    assert "no duplicate was established" in truncation_row.error_message
    for row in (collision_row, truncation_row):
        assert row.success == "false"  # AuditLog.success is a String(10) column

    # The seeded operator is untouched by either attempt -- the harm §7 describes.
    login = client.post(LOGIN_URL, data={"username": "0339", "password": PASSWORD})
    assert login.status_code == status.HTTP_200_OK, login.text
    assert login.json()["user"]["id"] == operator.id


def test_an_established_collision_wins_the_wording_when_both_are_true(client: TestClient, db_session: Session):
    """The tie-break, because the two flags are not mutually exclusive.

    A registration can carry an address that is genuinely taken AND a badge whose probe
    truncated. "Already in use" is then a FACT and it is the one that explains the refusal,
    so it must win -- otherwise the row reports the vaguer of two causes and buries the
    actionable one. The opposite precedence would be invisible in every other test here,
    since each of them establishes only one cause.
    """
    existing = make_user(db_session, email="taken.address@wercomfg.com")
    _seed_badge_noise(db_session, _EMPLOYEE_ID_CANDIDATE_CAP + 1)
    before = db_session.query(User).count()

    response = client.post(REGISTER_URL, json=payload(email=existing.email, employee_id="0001"))

    assert (response.status_code, response.json()) == (status.HTTP_200_OK, PENDING_BODY), response.text
    db_session.expire_all()
    assert db_session.query(User).count() == before

    rows = committed_audit_rows(db_session, "PUBLIC_REGISTRATION_REJECTED")
    assert len(rows) == 1
    assert rows[0].error_message == _REJECTED_IDENTIFIER_TAKEN
    assert rows[0].error_message != _REJECTED_EMPLOYEE_ID_UNRESOLVABLE
    # Both submitted identifiers are on the row, each under its own name.
    assert rows[0].extra_data["email"] == existing.email
    assert rows[0].extra_data["employee_id"] == "0001"


def test_a_truncating_probe_on_the_DERIVED_path_does_not_poison_the_rejection_reason(
    client: TestClient, db_session: Session
):
    """The reason is read from the DECISIVE probe only, never from the suffix walk.

    An email-only signup derives its badge, and the derivation's suffix walk runs the same
    predicate over candidate after candidate -- rejecting each and STEPPING PAST it. Those
    rejections are not rejections of the registration, so none of them may set the flags
    that end up on a permanent audit row.

    Here the derived base ``0001`` narrows to ``%1%``, which the seeded noise has truncated,
    so the walk's very first candidate answers "unusable" for a reason that is nobody's
    fault and says nothing about this applicant. The correct outcome is the one asserted:
    the walk steps past it onto a candidate whose own probe is clean, the account is
    created, and NO rejection row exists -- because nothing was rejected.

    The candidate it steps ONTO changed with the letter-suffix fix and this test was
    updated with it. ``0001`` carries digits, so a collision on it drops the derivation to
    the digits-stripped local part -- empty here, hence the ``user`` fallback -- rather than
    to ``0001-2``. That is what makes the step reliable rather than lucky: ``user``
    normalizes to ``None``, so ``_employee_id_taken_reason`` answers "free" on the exact
    probe alone and never reaches the truncating normalized query at all. Under the old
    numeric spelling every candidate DID reach it, every one came back True for the same
    table-shaped reason, and the walk exhausted -- refusing an applicant, permanently, with
    a cause that described somebody else's noise.

    The failure this guards against is the tempting simplification of letting the shared
    ``is_taken`` predicate set ``employee_id_unresolvable`` directly: the request would then
    be refused, and refused with a permanent ``error`` describing a probe that was only ever
    a step in a search. An applicant would be turned away and the log would explain it with
    a cause that had nothing to do with them.
    """
    operator = make_user(db_session, employee_id="EMP-0001")
    _seed_badge_noise(db_session, _EMPLOYEE_ID_CANDIDATE_CAP + 1)
    before = db_session.query(User).count()

    response = client.post(REGISTER_URL, json=payload(email="0001@applicant.example.com"))

    assert (response.status_code, response.json()) == (status.HTTP_200_OK, PENDING_BODY), response.text
    db_session.expire_all()
    assert db_session.query(User).count() == before + 1, "the walk must step past the unusable candidate, not refuse"

    created = db_session.query(User).filter(func.lower(User.email) == "0001@applicant.example.com").one()
    assert (
        created.employee_id == "user"
    ), f"the derivation did not step past the unusable candidate: {created.employee_id}"
    assert _normalize_employee_id(created.employee_id) is None, (
        "the candidate it settled on must be outside the four-digit keyspace -- that is what "
        "keeps it clear of the truncating probe as well as of the operator's badge"
    )
    assert _normalize_employee_id(created.employee_id) != _normalize_employee_id(operator.employee_id)

    assert committed_audit_rows(db_session, "PUBLIC_REGISTRATION_REJECTED") == [], (
        "nothing was rejected, so no permanent rejection row may exist -- a candidate the "
        "suffix walk stepped past is not a cause for refusing the request"
    )


# ===========================================================================
# 9. A derivation that cannot converge REFUSES -- it does not loop, and does not guess
# ===========================================================================
#
# Both derivations walk a suffix until ``is_taken`` says a candidate is free, and this
# route's predicate is NOT a function of the candidate: ``_employee_id_taken`` answers True
# whenever the collision probe's window truncated, which describes the user TABLE. Pinned
# True, an unbounded walk never terminates -- and every turn issues a database query from an
# UNAUTHENTICATED route, so one request pins a worker and holds a connection forever.
#
# ``services/user_identity`` therefore caps both walks at ``MAX_IDENTIFIER_CANDIDATES``
# offers and raises ``IdentifierDerivationExhausted`` (unit-pinned in
# ``tests/services/test_user_identity.py`` §6, including that a build with the cap removed
# FAILS rather than hangs). What these tests own is the ROUTE's half of the contract:
# exhaustion must refuse exactly as every other cause here refuses -- 200, the uniform
# pending body, nothing inserted -- while naming its own cause on the permanent audit row.
#
# The alternative to refusing is worse than slow: returning the last candidate hands the
# insert a value the probe just said not to use, which lands on
# ``uq_users_company_email`` / ``uq_users_company_employee_id`` as a 500 (a new
# distinguishable outcome on an anti-enumeration route) or, worse, puts a badge-only
# operator on another account's address.
#
# The seeding is REAL DATA, not a patched predicate: every candidate the walk will offer is
# occupied by a committed row, so the probe answers True for genuine reasons and the walk
# exhausts the way production would exhaust it.


def _seed_rows_holding(db: Session, *, emails=(), badges=(), company_id: int = COMPANY_A) -> None:
    """Commit users occupying exactly these addresses / badges.

    Whichever list is short is filled with values in a band nothing here registers. The
    rows never authenticate, so they carry a literal hash rather than paying bcrypt 100
    times.
    """
    _ensure_company(db, company_id)
    count = max(len(emails), len(badges))
    db.add_all(
        [
            User(
                email=emails[i] if i < len(emails) else f"walkseed-{_next()}@co{company_id}.example.com",
                employee_id=badges[i] if i < len(badges) else f"WALKSEED-{7400 + _next():06d}",
                first_name="Walk",
                last_name=f"{i}",
                hashed_password="$2b$12$abcdefghijklmnopqrstuv",
                role=UserRole.OPERATOR,
                is_active=True,
                is_superuser=False,
                company_id=company_id,
                failed_login_attempts=0,
            )
            for i in range(count)
        ]
    )
    db.commit()


def _walk_candidates(email: str) -> list:
    """Every badge candidate ``employee_id_from_email`` can offer for ``email``, in order.

    READ OFF THE DERIVATION rather than spelled out here, and that is deliberate. What this
    section needs is "every candidate the route's walk can reach is already occupied" --
    a claim about the WALK, not about its alphabet, which is pinned candidate-by-candidate
    in ``tests/services/test_user_identity.py`` §7. Writing the spellings out here made this
    test fail for the wrong reason the day the suffixes changed from digits to letters: the
    seeded set stopped describing the walk, so the walk converged on its first unseeded
    candidate and the exhaustion path went uncovered while the failure pointed at the cause
    sentence instead.
    """
    probed: list = []

    def is_taken(candidate: str) -> bool:
        probed.append(candidate)
        return True

    with pytest.raises(IdentifierDerivationExhausted):
        employee_id_from_email(email, is_taken)
    return probed


def test_a_badge_derivation_that_cannot_converge_refuses_and_names_that_cause(client: TestClient, db_session: Session):
    """The email-only path, with every candidate the walk can offer genuinely occupied.

    ``1@applicant.example.com`` derives the base badge ``1``; because that base carries
    digits, a collision on it drops the walk to the digits-stripped fallback ``user`` and
    then to ``user-b``, ``user-c``, ... All of them are held by committed rows here, so the
    walk exhausts for real reasons and must raise rather than return its last candidate --
    a value the probe had just refused, and one that belongs to a seeded row.

    THE SEEDING IS DERIVED, NOT SPELLED OUT (see ``_walk_candidates``). Exhaustion is now
    genuinely hard to reach -- which is the point of the letter suffixes, and is why §11
    exists -- so the only honest way to still cover the refusal is to occupy exactly the
    hundred candidates the walk really offers.

    The cause is its own sentence, and that is the point of the assertion pair below:
    "already in use" would be a claim about a duplicate of THIS applicant's identifier (there
    is none -- the address is free), and "the probe truncated" describes a single query that
    refused to answer rather than a search that ran out of room. Audit rows cannot be
    corrected (008/060 refuse UPDATE and DELETE), so the wrong sentence misdirects an admin
    permanently.
    """
    walk_candidates = _walk_candidates("1@applicant.example.com")
    assert len(walk_candidates) == MAX_IDENTIFIER_CANDIDATES
    _seed_rows_holding(db_session, badges=walk_candidates)
    before = db_session.query(User).count()

    response = client.post(REGISTER_URL, json=payload(email="1@applicant.example.com"))

    assert (response.status_code, response.json()) == (status.HTTP_200_OK, PENDING_BODY), response.text

    db_session.expire_all()
    assert db_session.query(User).count() == before, "an exhausted walk must insert nothing"
    assert db_session.query(User).filter(func.lower(User.email) == "1@applicant.example.com").count() == 0

    rows = committed_audit_rows(db_session, "PUBLIC_REGISTRATION_REJECTED")
    assert len(rows) == 1
    (row,) = rows
    assert row.error_message == _REJECTED_EMPLOYEE_ID_UNDERIVABLE
    assert row.error_message != _REJECTED_IDENTIFIER_TAKEN, "no duplicate of the submitted identifier was found"
    assert row.error_message != _REJECTED_EMPLOYEE_ID_UNRESOLVABLE, "the walk ran out; no single probe truncated"
    assert row.resource_identifier == "1@applicant.example.com", "the row must name what the applicant submitted"
    assert row.success == "false"  # AuditLog.success is a String(10) column


def test_a_mint_that_cannot_converge_refuses_the_badge_only_signup_the_same_way(
    client: TestClient, db_session: Session
):
    """The other direction: a badge-only signup whose synthetic address cannot be minted.

    ``User.email`` is NOT NULL and a shop-floor registrant may have no address at all, so
    the badge-only path mints ``emp-<badge>@users.werco.com``. Here every spelling the mint
    can reach is already held, so the walk exhausts -- and the refusal has to look exactly
    like the taken-identifier refusal to the caller while carrying its own cause in the log.

    Inserting the last candidate instead would be the sharpest version of the failure this
    route exists to prevent: a badge-only operator landing on an address another account
    already owns, which is precisely the two-rows-one-address state that makes
    ``_find_user_by_auth_email`` refuse 409 and takes the OTHER person's login down.
    """
    minted = [f"emp-mintloop@{SYNTHETIC_EMAIL_DOMAIN}"] + [
        f"emp-mintloop-{n}@{SYNTHETIC_EMAIL_DOMAIN}" for n in range(2, MAX_IDENTIFIER_CANDIDATES + 1)
    ]
    assert len(minted) == MAX_IDENTIFIER_CANDIDATES
    _seed_rows_holding(db_session, emails=minted)
    before = db_session.query(User).count()

    response = client.post(REGISTER_URL, json=payload(employee_id="MINTLOOP"))

    assert (response.status_code, response.json()) == (status.HTTP_200_OK, PENDING_BODY), response.text

    db_session.expire_all()
    assert db_session.query(User).count() == before, "an exhausted mint must insert nothing"
    assert db_session.query(User).filter(func.lower(User.employee_id) == "mintloop").count() == 0

    rows = committed_audit_rows(db_session, "PUBLIC_REGISTRATION_REJECTED")
    assert len(rows) == 1
    (row,) = rows
    assert row.error_message == _REJECTED_EMAIL_UNDERIVABLE
    assert row.error_message != _REJECTED_IDENTIFIER_TAKEN
    assert row.error_message != _REJECTED_EMPLOYEE_ID_UNDERIVABLE, "the BADGE was fine; the address could not be minted"
    assert row.resource_identifier == "employee-id:MINTLOOP"
    assert "email" not in (row.extra_data or {}), "a badge-only registrant has no address to record"


def test_the_four_refusal_causes_are_four_distinct_permanent_sentences():
    """Stated once, directly, because every test above asserts only two of them at a time.

    Four causes now refuse this route, and each earns its own ``error``: an established
    duplicate, a truncated collision probe, an exhausted badge walk, an exhausted mint. Two
    of them collapsing onto one string would be invisible in the pairwise assertions above
    and would quietly merge two different remediations in the log an admin reads.
    """
    causes = [
        _REJECTED_IDENTIFIER_TAKEN,
        _REJECTED_EMPLOYEE_ID_UNRESOLVABLE,
        _REJECTED_EMPLOYEE_ID_UNDERIVABLE,
        _REJECTED_EMAIL_UNDERIVABLE,
    ]
    assert len(set(causes)) == len(causes), f"two refusal causes share a sentence: {causes}"


# ===========================================================================
# 10. The lost race records the SAME cause as the duplicate it lost to
# ===========================================================================
#
# Two concurrent registrations of the same identifier both pass the duplicate probe and one
# of them loses at ``uq_users_company_email`` / ``uq_users_company_employee_id``. That
# loser is a duplicate -- the probe simply asked a moment too early -- so it must answer
# exactly as the ordinary duplicate does (a 500 here would be the same existence oracle
# wearing a different status code) and record the SAME cause.
#
# It has to be the CONSTANT and not a re-spelling of it: an inline literal drifting from
# ``_REJECTED_IDENTIFIER_TAKEN`` would split one cause across two sentences on a table
# nobody can correct afterwards, and the drift would be invisible until an admin's query
# for one of them started missing half the rows.
#
# The race is produced by making the handler's terminal commit fail ONCE, which is what the
# database does under a real race -- the refusal path's own commit then runs for real.


def test_the_lost_race_records_the_same_cause_as_the_duplicate_it_lost_to(
    client: TestClient, db_session: Session, monkeypatch
):
    """Both rows in one test, compared to EACH OTHER as well as to the constant.

    The ordinary duplicate goes first, on the untouched code path. Then the terminal commit
    is armed to fail once and a genuinely FREE identifier is registered -- free, so the only
    thing that can refuse it is the race, and the refusal cannot be the duplicate probe
    quietly firing instead.
    """
    holder = make_user(db_session, email="race.holder@wercomfg.com")

    ordinary = client.post(REGISTER_URL, json=payload(email=holder.email))
    assert (ordinary.status_code, ordinary.json()) == (status.HTTP_200_OK, PENDING_BODY), ordinary.text
    before = db_session.query(User).count()

    real_commit = db_session.commit
    calls = {"n": 0}

    def commit_losing_the_first_race():
        calls["n"] += 1
        if calls["n"] == 1:
            raise IntegrityError(
                "INSERT INTO users ...", {}, Exception("duplicate key value violates unique constraint")
            )
        return real_commit()

    monkeypatch.setattr(db_session, "commit", commit_losing_the_first_race)
    raced = client.post(REGISTER_URL, json=payload(email="race.winner@wercomfg.com"))
    monkeypatch.undo()

    assert calls["n"] >= 2, "the refusal path must still commit its own audit row"
    assert (raced.status_code, raced.json()) == (ordinary.status_code, ordinary.json()), (
        "the lost race answered differently from the duplicate it lost to -- that difference "
        "is the account-existence oracle this route exists to remove"
    )

    db_session.expire_all()
    assert db_session.query(User).count() == before, "the rolled-back insert must not survive"
    assert db_session.query(User).filter(func.lower(User.email) == "race.winner@wercomfg.com").count() == 0

    rows = {row.resource_identifier: row for row in committed_audit_rows(db_session, "PUBLIC_REGISTRATION_REJECTED")}
    assert set(rows) == {"race.holder@wercomfg.com", "race.winner@wercomfg.com"}, rows

    duplicate_row = rows["race.holder@wercomfg.com"]
    race_row = rows["race.winner@wercomfg.com"]
    assert race_row.error_message == _REJECTED_IDENTIFIER_TAKEN
    assert race_row.error_message == duplicate_row.error_message, (
        "one cause must be one sentence -- a re-spelling would split it across two, "
        "permanently, on a table 008/060 refuse to UPDATE or DELETE"
    )
    assert race_row.success == "false"  # AuditLog.success is a String(10) column


# ===========================================================================
# 11. A CONTIGUOUS BADGE TABLE NO LONGER SWALLOWS AN EMAIL-ONLY SIGNUP
# ===========================================================================
#
# THE BUG, and the reason it survived a green suite. The derivation used to suffix ``-2``,
# ``-3``, ... and ``_normalize_employee_id`` reads any badge as its LAST FOUR DIGITS -- so
# ``jmw-2`` IS badge ``0002`` and ``jmw-3`` IS ``0003``. ``_employee_id_taken_reason``
# refuses normalized collisions (§7 is why it must), so on the ordinary shop-floor shape --
# badges issued contiguously from 0001 -- every candidate the walk could offer collided
# with a real operator. The walk ran to ``MAX_IDENTIFIER_CANDIDATES``, raised, and the
# handler refused the registration.
#
# NOTHING IN THE RESPONSE SAID SO. This route answers the same 200 + uniform pending body
# whether it inserted a row or refused one -- that is the anti-enumeration design, and it
# is correct. It also means a test that asserts on the response body alone cannot tell a
# successful signup from a silently dropped one, which is exactly how a walk that created
# NOTHING for a whole class of applicants passed every existing assertion here. So the
# tests below assert on the created ROW: it exists, it holds a real badge, and that badge
# is outside the four-digit keyspace the shop's operators live in.
#
# ``_normalize_employee_id(...) is None`` is the strong form of "did not poison anything":
# it does not merely miss THIS shop's badges, it cannot collide with any badge under
# normalization on any table.


def _seed_contiguous_badge_shop(db: Session, *, first: int = 1, last: int = 150, company_id: int = COMPANY_A) -> None:
    """Commit operators holding badges ``0001`` ... ``0150`` -- an ordinary shop's table.

    Contiguity is the whole point: it is what makes EVERY numeric suffix a real operator's
    badge. The rows are login-capable through the passwordless badge route (real names, so
    ``UserResponse`` validates) but never verify a password, so they carry a literal hash
    rather than paying bcrypt 150 times.

    The band stays clear of this file's ``71xx`` seed band (see ``make_user``).
    """
    _ensure_company(db, company_id)
    db.add_all(
        [
            User(
                email=f"floor-{n:04d}@co{company_id}.example.com",
                employee_id=f"{n:04d}",
                first_name="Floor",
                last_name="Operator",
                hashed_password="$2b$12$abcdefghijklmnopqrstuv",
                role=UserRole.OPERATOR,
                is_active=True,
                is_superuser=False,
                company_id=company_id,
                failed_login_attempts=0,
            )
            for n in range(first, last + 1)
        ]
    )
    db.commit()


def test_an_email_only_signup_survives_a_shop_whose_badges_are_contiguous(client: TestClient, db_session: Session):
    """THE REGRESSION TEST for the walk that could not converge.

    The shape is the production one: badges ``0001``-``0150`` issued in order, and the
    derived base badge (``jmw``, from the local part) already held by somebody. Under the
    numeric suffixes every candidate -- ``jmw-2`` ... ``jmw-100`` -> ``0002`` ... ``0100`` --
    normalized onto one of those operators, the walk exhausted, and the applicant was
    refused with no account, no notice and a 200.

    ASSERTED ON THE ROW, NEVER ON THE BODY. The response is uniform by design (§3), so it
    cannot distinguish "created" from "silently refused" -- which is precisely why the
    original defect passed a green suite. ``.one()`` is the assertion: it raises if the
    walk created nothing.
    """
    _seed_contiguous_badge_shop(db_session)
    make_user(db_session, employee_id="jmw")  # the derived BASE badge is taken too

    response = client.post(REGISTER_URL, json=payload(email="jmw@applicant.example.com"))

    assert (response.status_code, response.json()) == (status.HTTP_200_OK, PENDING_BODY), response.text

    db_session.expire_all()
    created = db_session.query(User).filter(func.lower(User.email) == "jmw@applicant.example.com").one()
    assert created.employee_id == "jmw-b", "the walk must step past the taken base with a LETTER"
    assert created.role == UserRole.VIEWER, "still an ordinary pending signup"
    assert created.is_active is False

    # The strong form of "poisoned nobody": not merely clear of these 150 badges, but
    # outside the normalized keyspace altogether, for any table.
    assert _normalize_employee_id(created.employee_id) is None

    # Nothing was refused, so no permanent rejection row may exist.
    assert committed_audit_rows(db_session, "PUBLIC_REGISTRATION_REJECTED") == []

    # ...and the shop is untouched: a badge in the middle of the band still resolves to its
    # own operator on the door the floor actually uses.
    badge_login = client.post(EMPLOYEE_LOGIN_URL, json={"employee_id": "0042"})
    assert badge_login.status_code == status.HTTP_200_OK, badge_login.text
    assert badge_login.json()["user"]["employee_id"] == "0042"


def test_the_candidate_set_that_used_to_exhaust_the_walk_now_converges(client: TestClient, db_session: Session):
    """The old walk's OWN candidate list, seeded row for row, no longer stops a signup.

    ``1``, ``1-2``, ... ``1-100`` is exactly what ``employee_id_from_email`` used to offer
    for ``1@applicant.example.com``, and holding all hundred was enough to exhaust it. The
    letter walk never offers any of them: the base carries digits, so a collision on it
    drops straight to the digits-stripped fallback -- ``user`` here -- which is free and,
    being digit-free, is not even in the keyspace those seeded rows occupy.

    Paired with the test above on purpose. That one shows the realistic shape; this one
    shows the literal seeding the exhaustion test used to rely on, so the delta between
    "refused" and "created" is pinned against the exact data that produced it.
    """
    exhausting_under_the_old_walk = ["1"] + [f"1-{n}" for n in range(2, MAX_IDENTIFIER_CANDIDATES + 1)]
    assert len(exhausting_under_the_old_walk) == MAX_IDENTIFIER_CANDIDATES
    _seed_rows_holding(db_session, badges=exhausting_under_the_old_walk)
    before = db_session.query(User).count()

    response = client.post(REGISTER_URL, json=payload(email="1@applicant.example.com"))

    assert (response.status_code, response.json()) == (status.HTTP_200_OK, PENDING_BODY), response.text

    db_session.expire_all()
    assert db_session.query(User).count() == before + 1, "the signup must be created, not silently dropped"
    created = db_session.query(User).filter(func.lower(User.email) == "1@applicant.example.com").one()
    assert created.employee_id == "user"
    assert _normalize_employee_id(created.employee_id) is None
    assert committed_audit_rows(db_session, "PUBLIC_REGISTRATION_REJECTED") == []
