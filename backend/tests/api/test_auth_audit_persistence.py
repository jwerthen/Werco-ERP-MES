"""Committed-only audit-persistence coverage for the auth endpoints.

Locks in the audit-ordering fix on branch qa/full-pass-2026-06-04. The four
handlers exercised here -- ``login``, ``employee_login``, ``register`` and
``register_public`` in ``app.api.endpoints.auth`` -- emit an ``AuditService``
row (resource_type ``"authentication"``) for the security event and are now
responsible for calling ``db.commit()`` AFTER ``log_auth_event(...)`` so the
audit row is durably persisted. The old, buggy ordering logged the auth event
AFTER ``db.commit()`` (or did not commit at all), leaving the audit row merely
flushed into a never-committed transaction.

Why a naive test cannot catch that bug
--------------------------------------
The ``client`` fixture (tests/conftest.py) overrides ``get_db`` to yield ONE
shared, never-closed ``db_session``, so the endpoint and the test share a
single open transaction. ``AuditService.log()`` only ``flush()``es; whether the
row is durable depends entirely on the handler committing afterwards. A row that
was only flushed is still VISIBLE to a plain ``db.query(AuditLog)`` issued by
the test, because that read happens inside the same open transaction -- so a
naive assertion passes even against the broken (audit-after-commit) code.

The guard, borrowed verbatim from tests/api/test_qms_soft_delete_audit.py, is to
``db.rollback()`` BEFORE querying ``AuditLog``: a COMMITTED audit row survives
the rollback (the commit already ended its transaction), while a
flushed-but-uncommitted one is discarded. Every assertion below therefore reads
through ``_committed_audit_rows`` -- it would FAIL against the old
audit-after-commit code because the rollback would throw the still-uncommitted
audit row away, leaving zero rows.

We never INSERT ``AuditLog`` rows directly (they carry a tamper-evident hash
chain); they are produced by the endpoints and only read back here. The default
seeded company is id=1 (tests/conftest.py).
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.security import create_access_token, get_password_hash
from app.models.audit_log import AuditLog
from app.models.user import User, UserRole

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
AUTH_RESOURCE = "authentication"
PASSWORD = "SecureP@ss123!"

# Module-level counter so every fixture row gets a globally unique natural key,
# even across tests sharing a worker DB under -n auto.
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _make_user(
    db: Session,
    *,
    company_id: int = COMPANY_A,
    role: UserRole = UserRole.ADMIN,
    is_active: bool = True,
    employee_id: str = None,
) -> User:
    n = _next()
    user = User(
        email=f"auth-ap-{n}-co{company_id}@example.com",
        employee_id=employee_id or f"AUTHAP-{n:05d}",
        first_name="Auth",
        last_name="Tester",
        hashed_password=get_password_hash(PASSWORD),
        role=role,
        is_active=is_active,
        is_superuser=False,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _headers_for(user: User) -> dict:
    token = create_access_token(subject=user.id, company_id=user.company_id)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def _audit_rows(db: Session, *, resource_id: int = None, action: str = None):
    """Fetch authentication AuditLog rows, newest first, optional filters.

    ``expire_all`` first so rows committed through the endpoint's session (the
    same shared ``db_session`` the client overrides ``get_db`` with) are
    reloaded instead of served stale from the identity map.
    """
    db.expire_all()
    q = db.query(AuditLog).filter(AuditLog.resource_type == AUTH_RESOURCE)
    if resource_id is not None:
        q = q.filter(AuditLog.resource_id == resource_id)
    if action is not None:
        q = q.filter(AuditLog.action == action)
    return q.order_by(AuditLog.sequence_number.desc()).all()


def _committed_audit_rows(db: Session, *, resource_id: int = None, action: str = None):
    """Fetch authentication AuditLog rows that were COMMITTED, not merely flushed.

    This is the real guard against the production bug. The ``client`` fixture
    overrides ``get_db`` to yield this one shared, never-closed ``db_session``,
    so the endpoint and the test share a single open transaction.
    ``AuditService.log()`` only ``flush()``es; the handler is responsible for the
    ``commit()``. If a handler logged the auth event AFTER ``db.commit()`` (the
    bug that was just fixed), the audit row would be flushed into a fresh,
    never-committed transaction -- yet a plain ``db.query(AuditLog)`` in the test
    would still SEE it, because the read happens inside that same open
    transaction. So a naive assertion passes against broken code.

    Rolling back BEFORE querying closes that loophole: a committed audit row
    survives the rollback, while a flushed-but-uncommitted one is discarded.
    """
    db.rollback()
    return _audit_rows(db, resource_id=resource_id, action=action)


# ---------------------------------------------------------------------------
# 1. login (POST /api/v1/auth/login)
# ---------------------------------------------------------------------------


def test_login_success_commits_audit(client: TestClient, db_session: Session):
    """A successful login commits a LOGIN_SUCCESS authentication audit row,
    tagged with the user's id and company.

    FAILS against the old code: login logged LOGIN_SUCCESS but the audit row was
    only flushed (audit-after/without-commit), so the rollback in
    ``_committed_audit_rows`` discards it and we'd see zero rows."""
    user = _make_user(db_session, role=UserRole.OPERATOR)

    resp = client.post(
        "/api/v1/auth/login",
        data={"username": user.email, "password": PASSWORD},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    rows = _committed_audit_rows(db_session, resource_id=user.id, action="LOGIN_SUCCESS")
    assert len(rows) == 1, "expected exactly one COMMITTED LOGIN_SUCCESS audit row"
    assert rows[0].action == "LOGIN_SUCCESS"
    assert rows[0].resource_type == AUTH_RESOURCE
    assert rows[0].resource_id == user.id
    assert rows[0].company_id == COMPANY_A


def test_login_failed_bad_password_commits_audit(client: TestClient, db_session: Session):
    """A wrong-password login (known user) commits a LOGIN_FAILED audit row,
    company-tagged -- the failed-attempt path must persist its audit row too.

    FAILS against the old code: the LOGIN_FAILED row would be flushed but not
    committed, and the pre-query rollback throws it away (along with the
    failed-attempt increment), leaving zero committed rows."""
    user = _make_user(db_session, role=UserRole.OPERATOR)

    resp = client.post(
        "/api/v1/auth/login",
        data={"username": user.email, "password": "WrongP@ssw0rd!"},
    )
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED, resp.text

    rows = _committed_audit_rows(db_session, resource_id=user.id, action="LOGIN_FAILED")
    assert len(rows) == 1, "expected exactly one COMMITTED LOGIN_FAILED audit row"
    assert rows[0].action == "LOGIN_FAILED"
    assert rows[0].resource_type == AUTH_RESOURCE
    assert rows[0].resource_id == user.id
    assert rows[0].company_id == COMPANY_A
    assert rows[0].success == "false"


def test_login_failed_unknown_user_commits_audit(client: TestClient, db_session: Session):
    """A login for a non-existent email commits a LOGIN_FAILED audit row that
    carries the attempted email but no tenant attribution (user=None).

    FAILS against the old code: the audit row was flushed-only, so the rollback
    discards it and no LOGIN_FAILED row remains."""
    missing_email = f"ghost-{_next()}@nowhere.test"

    resp = client.post(
        "/api/v1/auth/login",
        data={"username": missing_email, "password": "WhateverP@ss1"},
    )
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED, resp.text

    # user is None on this path -> resource_id is None, company_id is None.
    rows = [
        r for r in _committed_audit_rows(db_session, action="LOGIN_FAILED") if r.resource_identifier == missing_email
    ]
    assert len(rows) == 1, "expected exactly one COMMITTED LOGIN_FAILED row for the unknown email"
    assert rows[0].resource_type == AUTH_RESOURCE
    assert rows[0].resource_id is None
    assert rows[0].company_id is None
    assert rows[0].success == "false"


# ---------------------------------------------------------------------------
# 2. employee_login (POST /api/v1/auth/employee-login)
# ---------------------------------------------------------------------------


def test_employee_login_success_commits_audit(client: TestClient, db_session: Session):
    """A successful employee-ID login commits an EMPLOYEE_LOGIN_SUCCESS audit
    row, tagged with the user's id and company.

    FAILS against the old code: the EMPLOYEE_LOGIN_SUCCESS row would be
    flushed-only and discarded by the pre-query rollback."""
    n = _next()
    user = _make_user(db_session, role=UserRole.OPERATOR, employee_id=f"EMPID{n:04d}")

    resp = client.post(
        "/api/v1/auth/employee-login",
        json={"employee_id": user.employee_id},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["user"]["id"] == user.id

    rows = _committed_audit_rows(db_session, resource_id=user.id, action="EMPLOYEE_LOGIN_SUCCESS")
    assert len(rows) == 1, "expected exactly one COMMITTED EMPLOYEE_LOGIN_SUCCESS audit row"
    assert rows[0].action == "EMPLOYEE_LOGIN_SUCCESS"
    assert rows[0].resource_type == AUTH_RESOURCE
    assert rows[0].resource_id == user.id
    assert rows[0].company_id == COMPANY_A


def test_employee_login_failed_unknown_id_commits_audit(client: TestClient, db_session: Session):
    """An employee-ID login for an unknown badge commits an
    EMPLOYEE_LOGIN_FAILED audit row (user=None -> no tenant attribution).

    FAILS against the old code: the failure audit row was flushed-only, so the
    rollback discards it before the query runs."""
    unknown_badge = f"NOBADGE{_next():05d}"

    resp = client.post(
        "/api/v1/auth/employee-login",
        json={"employee_id": unknown_badge},
    )
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED, resp.text

    rows = _committed_audit_rows(db_session, action="EMPLOYEE_LOGIN_FAILED")
    assert len(rows) >= 1, "expected a COMMITTED EMPLOYEE_LOGIN_FAILED audit row"
    assert rows[0].resource_type == AUTH_RESOURCE
    assert rows[0].resource_id is None
    assert rows[0].company_id is None
    assert rows[0].success == "false"


# ---------------------------------------------------------------------------
# 3. register (POST /api/v1/auth/register) -- admin only
# ---------------------------------------------------------------------------


def test_register_commits_audit(client: TestClient, db_session: Session):
    """An admin registering a new user commits a USER_REGISTERED audit row,
    keyed to the NEW user's id and tagged with the admin's active company.

    FAILS against the old code: register flushed the audit row but the commit
    timing left it uncommitted, so the pre-query rollback discards it -- zero
    USER_REGISTERED rows remain."""
    admin = _make_user(db_session, role=UserRole.ADMIN)
    n = _next()
    new_email = f"reg-target-{n}@example.com"

    resp = client.post(
        "/api/v1/auth/register",
        headers=_headers_for(admin),
        json={
            "email": new_email,
            "employee_id": f"REGT-{n:05d}",
            "first_name": "Reg",
            "last_name": "Target",
            "password": PASSWORD,
            "role": "operator",
        },
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    new_id = resp.json()["id"]

    rows = _committed_audit_rows(db_session, resource_id=new_id, action="USER_REGISTERED")
    assert len(rows) == 1, "expected exactly one COMMITTED USER_REGISTERED audit row"
    assert rows[0].action == "USER_REGISTERED"
    assert rows[0].resource_type == AUTH_RESOURCE
    assert rows[0].resource_id == new_id
    assert rows[0].company_id == COMPANY_A


# ---------------------------------------------------------------------------
# 4. register_public (POST /api/v1/auth/register-public)
# ---------------------------------------------------------------------------


def test_register_public_subsequent_user_commits_audit(client: TestClient, db_session: Session):
    """When users already exist, public self-registration commits a
    PUBLIC_REGISTRATION audit row keyed to the new (pending) user.

    A pre-existing user is created first so this is the non-bootstrap branch.

    FAILS against the old code: the PUBLIC_REGISTRATION row was flushed-only, so
    the pre-query rollback discards it -- no committed row to find."""
    # Ensure at least one user exists so register_public takes the VIEWER branch.
    _make_user(db_session, role=UserRole.ADMIN)
    n = _next()
    new_email = f"public-reg-{n}@example.com"

    resp = client.post(
        "/api/v1/auth/register-public",
        json={
            "email": new_email,
            "first_name": "Public",
            "last_name": "Reg",
            "password": PASSWORD,
        },
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["is_first_user"] is False

    # The response does not carry the id; look up the freshly created user.
    db_session.expire_all()
    new_user = db_session.query(User).filter(func.lower(User.email) == new_email.lower()).first()
    assert new_user is not None
    new_id = new_user.id

    rows = _committed_audit_rows(db_session, resource_id=new_id, action="PUBLIC_REGISTRATION")
    assert len(rows) == 1, "expected exactly one COMMITTED PUBLIC_REGISTRATION audit row"
    assert rows[0].action == "PUBLIC_REGISTRATION"
    assert rows[0].resource_type == AUTH_RESOURCE
    assert rows[0].resource_id == new_id
    assert rows[0].company_id == COMPANY_A


def test_register_public_first_user_commits_audit(client: TestClient, db_session: Session):
    """On an empty system (no users), public registration bootstraps the first
    admin and commits a FIRST_USER_REGISTERED audit row.

    The conftest fixture seeds company id=1 but no users, so register-public
    takes the first-user bootstrap branch.

    FAILS against the old code: the FIRST_USER_REGISTERED row was flushed-only,
    so the pre-query rollback discards it -- zero committed rows."""
    # Sanity: this test relies on the empty-users precondition.
    assert db_session.query(User).count() == 0, "expected an empty users table for the bootstrap branch"
    n = _next()
    new_email = f"first-admin-{n}@example.com"

    resp = client.post(
        "/api/v1/auth/register-public",
        json={
            "email": new_email,
            "first_name": "First",
            "last_name": "Admin",
            "password": PASSWORD,
        },
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["is_first_user"] is True

    db_session.expire_all()
    new_user = db_session.query(User).filter(func.lower(User.email) == new_email.lower()).first()
    assert new_user is not None
    new_id = new_user.id

    rows = _committed_audit_rows(db_session, resource_id=new_id, action="FIRST_USER_REGISTERED")
    assert len(rows) == 1, "expected exactly one COMMITTED FIRST_USER_REGISTERED audit row"
    assert rows[0].action == "FIRST_USER_REGISTERED"
    assert rows[0].resource_type == AUTH_RESOURCE
    assert rows[0].resource_id == new_id
    assert rows[0].company_id == COMPANY_A
