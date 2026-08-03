"""``POST /api/v1/auth/employee-logout`` must not forge audit rows or enumerate badges.

Until this change the endpoint took **no auth dependency at all**. It resolved
``payload.employee_id`` through the globally unscoped ``_find_user_by_employee_id``
(contrast ``_find_user_by_employee_id_in_company``, which exists precisely so a
foreign tenant's badge cannot resolve on the kiosk badge mint), then wrote a
chain-linked ``EMPLOYEE_LOGOUT`` row and committed. Two defects fell out of that:

* **Audit forgery, and a visible one.** Unlike the ``FRONTEND_ERROR`` rows removed
  in this same change, these were tenant-tagged: an anonymous
  ``POST {"employee_id": "339"}`` produced ``EMPLOYEE_LOGOUT / user_id=1 /
  admin@werco.com / company_id=1 / success=true``, which any Admin or Manager
  reads back at ``GET /api/v1/audit/?resource_type=authentication``.
* **A cross-tenant badge-enumeration oracle.** Badges normalize to four digits and
  a miss returned 404 while a hit returned 200, with no per-path rate limit (only
  the global default) and no ``employee_login_throttle`` (that is wired to
  /employee-login alone) — so the whole 4-digit space was walkable, across
  tenants, at 100 guesses/minute.

Deleting the route was preferred but unavailable: the office badge-confirm logout
calls it, and the kiosk scope fence allowlists it. So identity now comes from the
bearer token via ``get_current_user`` — chosen over a hand-rolled optional bearer
so the kiosk path fence and the read-only-context write guard, which live inside
``get_current_user``, stay in force.
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import app.main as app_main
from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.user import UserRole
from tests.api.kiosk_test_helpers import COMPANY_A, COMPANY_B, bearer, make_user

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

ENDPOINT = "/api/v1/auth/employee-logout"


def _logout_rows(db: Session):
    return db.query(AuditLog).filter(AuditLog.action == "EMPLOYEE_LOGOUT").all()


def test_unauthenticated_logout_is_refused_and_writes_no_audit_row(client: TestClient, db_session: Session):
    """THE FIX: no token, no audit row. Previously this returned 200 and wrote one."""
    victim = make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN)

    response = client.post(ENDPOINT, json={"employee_id": victim.employee_id})

    assert response.status_code == status.HTTP_401_UNAUTHORIZED, response.text
    assert _logout_rows(db_session) == []


def test_unknown_and_real_badges_are_indistinguishable_without_a_token(client: TestClient, db_session: Session):
    """THE ORACLE IS GONE: a real badge and a made-up one answer identically.

    The 200-vs-404 split was the enumeration primitive. Both are now 401 with the
    same body, so an unauthenticated caller learns nothing about which badges
    exist — in this or any other tenant.
    """
    real = make_user(db_session, company_id=COMPANY_A)
    foreign = make_user(db_session, company_id=COMPANY_B)

    responses = [
        client.post(ENDPOINT, json={"employee_id": real.employee_id}),
        client.post(ENDPOINT, json={"employee_id": foreign.employee_id}),
        client.post(ENDPOINT, json={"employee_id": "0000"}),
        client.post(ENDPOINT, json={"employee_id": "NO-SUCH-BADGE-9999"}),
    ]

    statuses = {r.status_code for r in responses}
    bodies = {r.text for r in responses}
    assert statuses == {status.HTTP_401_UNAUTHORIZED}, [r.status_code for r in responses]
    assert len(bodies) == 1, bodies
    assert _logout_rows(db_session) == []


def test_authenticated_logout_records_the_token_user(client: TestClient, db_session: Session):
    """The happy path still works and attributes the row to the caller."""
    user = make_user(db_session, company_id=COMPANY_A)
    token = create_access_token(subject=user.id, company_id=COMPANY_A)

    response = client.post(ENDPOINT, headers=bearer(token), json={"employee_id": user.employee_id})

    assert response.status_code == status.HTTP_200_OK, response.text
    rows = _logout_rows(db_session)
    assert len(rows) == 1
    assert rows[0].user_id == user.id
    assert rows[0].company_id == COMPANY_A


def test_body_cannot_override_the_token_identity(client: TestClient, db_session: Session):
    """Identity comes from the TOKEN. Naming someone else in the body changes nothing.

    This is the forgery attempt in its authenticated form: an operator in company
    A posting an ADMIN's badge must not produce an audit row attributed to that
    admin.
    """
    operator = make_user(db_session, company_id=COMPANY_A, role=UserRole.OPERATOR)
    admin = make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN)
    token = create_access_token(subject=operator.id, company_id=COMPANY_A)

    response = client.post(ENDPOINT, headers=bearer(token), json={"employee_id": admin.employee_id})

    assert response.status_code == status.HTTP_200_OK, response.text
    rows = _logout_rows(db_session)
    assert len(rows) == 1
    assert rows[0].user_id == operator.id, "audit row must name the token's user, not the body's"
    assert rows[0].user_id != admin.id


def test_foreign_tenant_badge_in_the_body_is_ignored(client: TestClient, db_session: Session):
    """A company-A token naming a company-B badge writes a company-A row.

    The old unscoped lookup would have resolved the foreign user and tagged the
    row with company B — a cross-tenant audit write.
    """
    caller = make_user(db_session, company_id=COMPANY_A)
    foreign = make_user(db_session, company_id=COMPANY_B)
    token = create_access_token(subject=caller.id, company_id=COMPANY_A)

    response = client.post(ENDPOINT, headers=bearer(token), json={"employee_id": foreign.employee_id})

    assert response.status_code == status.HTTP_200_OK, response.text
    rows = _logout_rows(db_session)
    assert len(rows) == 1
    assert rows[0].user_id == caller.id
    assert rows[0].company_id == COMPANY_A


def test_kiosk_scoped_token_still_works(client: TestClient, db_session: Session):
    """The crew station must keep recording its own logouts.

    This is why the endpoint is authenticated via ``get_current_user`` rather
    than a bespoke bearer: the ``KIOSK_TOKEN_EXACT_PATHS`` fence in deps.py is
    consulted only there, and it is now what authorizes this call.
    """
    operator = make_user(db_session, company_id=COMPANY_A, role=UserRole.OPERATOR)
    token = create_access_token(subject=operator.id, company_id=COMPANY_A, scope="kiosk")

    response = client.post(ENDPOINT, headers=bearer(token), json={"employee_id": operator.employee_id})

    assert response.status_code == status.HTTP_200_OK, response.text
    rows = _logout_rows(db_session)
    assert len(rows) == 1
    assert rows[0].user_id == operator.id


def test_employee_logout_rate_limit_is_registered():
    """It also gains a per-path ceiling; it previously had none."""
    limits = getattr(app_main, "AUTH_RATE_LIMITS", None)
    if limits is None:
        pytest.skip("Rate limiting disabled in this environment")
    assert limits[ENDPOINT] == "30/minute"
