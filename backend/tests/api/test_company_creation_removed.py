"""Company creation is gone from the HTTP surface; everything around it still works.

Owner decision 2026-11-23: Werco is the only company on this system. Both company
creation routes were deleted -- the unauthenticated ``POST /api/v1/companies/register``
and the platform-admin ``POST /api/v1/platform/companies`` ("Delete it too"). The only
way a ``Company`` row comes to exist at runtime is the empty-install first-user bootstrap
in ``POST /api/v1/auth/register-public``.

These tests prove it over real HTTP, for the callers who matter:

* an anonymous caller and a platform admin both get 404/405 from each deleted route, and
  no company, user or audit row is written;
* the bootstrap still creates the one company on a truly empty database, and creates no
  second one afterwards;
* every remaining ``/platform`` route, and company context switching, still answers for a
  platform admin. Removing a door must not take the room with it.

The static half (no new ``Company(...)`` constructor, no new company-creation route) lives
in ``tests/test_company_creation_doors_guard.py``.
"""

import pytest

from app.core.security import create_access_token, get_password_hash
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.user import User, UserRole

pytestmark = pytest.mark.api

PASSWORD = "Zephyr9!Quill-Test"

DELETED_ROUTES = [
    (
        "/api/v1/companies/register",
        {
            "company_name": "Fictional Fabrication",
            "admin_email": "newadmin@example.com",
            "admin_first_name": "New",
            "admin_last_name": "Admin",
            "admin_password": PASSWORD,
        },
    ),
    (
        "/api/v1/platform/companies",
        {
            "name": "Fictional Fabrication",
            "admin_email": "newadmin@example.com",
            "admin_first_name": "New",
            "admin_last_name": "Admin",
            "admin_password": PASSWORD,
        },
    ),
]


def _platform_admin(db) -> User:
    user = User(
        company_id=1,
        email="platform-probe@example.com",
        employee_id="PLATFORM-PROBE-1",
        first_name="Platform",
        last_name="Probe",
        role=UserRole.PLATFORM_ADMIN,
        is_active=True,
        is_superuser=False,
        hashed_password=get_password_hash(PASSWORD),
    )
    db.add(user)
    db.commit()
    return user


def _auth(user: User, company_id: int | None = None) -> dict:
    token = create_access_token(subject=user.id, company_id=company_id or user.company_id)
    return {"Authorization": f"Bearer {token}"}


def _assert_nothing_written(db, *, companies: int, users: int, audit_rows: int) -> None:
    db.rollback()
    db.expire_all()
    assert db.query(Company).count() == companies
    assert db.query(Company).filter(Company.name == "Fictional Fabrication").count() == 0
    assert db.query(User).count() == users
    assert db.query(User).filter(User.email == "newadmin@example.com").count() == 0
    assert db.query(AuditLog).count() == audit_rows


# --- The deleted routes ---------------------------------------------------------------


@pytest.mark.parametrize("path,body", DELETED_ROUTES, ids=["companies-register", "platform-companies"])
def test_anonymous_caller_cannot_create_a_company(client, db_session, path, body):
    companies = db_session.query(Company).count()

    response = client.post(path, json=body)

    assert response.status_code in (404, 405), response.text
    _assert_nothing_written(db_session, companies=companies, users=0, audit_rows=0)


@pytest.mark.parametrize("path,body", DELETED_ROUTES, ids=["companies-register", "platform-companies"])
def test_platform_admin_cannot_create_a_company(client, db_session, path, body):
    actor = _platform_admin(db_session)
    companies = db_session.query(Company).count()
    audit_rows = db_session.query(AuditLog).count()

    response = client.post(path, headers=_auth(actor), json=body)

    assert response.status_code in (404, 405), response.text
    _assert_nothing_written(db_session, companies=companies, users=1, audit_rows=audit_rows)


@pytest.mark.parametrize("path", ["/api/v1/companies", "/api/v1/companies/", "/api/v1/platform/companies/"])
def test_no_neighbouring_company_collection_accepts_a_post(client, db_session, path):
    """A creation route that drifted to a sibling spelling would still be the same door."""
    actor = _platform_admin(db_session)
    companies = db_session.query(Company).count()

    response = client.post(path, headers=_auth(actor), json={"name": "Fictional Fabrication"})

    assert response.status_code in (404, 405, 307), response.text
    _assert_nothing_written(db_session, companies=companies, users=1, audit_rows=0)


# --- The bootstrap that stays ----------------------------------------------------------


def _public_body(n: int) -> dict:
    return {
        "email": f"bootstrap-{n}@example.com",
        "first_name": "Boot",
        "last_name": "Strap",
        "password": PASSWORD,
    }


def test_empty_install_bootstrap_creates_the_one_company_on_an_empty_database(client, db_session):
    # conftest seeds company 1; an empty install has no company at all.
    db_session.query(Company).delete()
    db_session.commit()
    db_session.expunge_all()  # drop the seeded row's identity so the new id 1 is not a clash
    assert db_session.query(Company).count() == 0
    assert db_session.query(User).count() == 0

    first = client.post("/api/v1/auth/register-public", json=_public_body(1))

    assert first.status_code == 200, first.text
    assert first.json()["is_first_user"] is True
    db_session.expire_all()
    company = db_session.query(Company).one()
    assert (company.slug, company.is_active) == ("werco", True)
    founder = db_session.query(User).filter_by(email="bootstrap-1@example.com").one()
    assert founder.company_id == company.id
    assert founder.is_active is True
    # The bootstrap is audited in the company it just created.
    rows = db_session.query(AuditLog).filter(AuditLog.action == "FIRST_USER_REGISTERED").all()
    assert [r.company_id for r in rows] == [company.id]

    # Once the install has a user, registration creates no second company.
    second = client.post("/api/v1/auth/register-public", json=_public_body(2))

    assert second.status_code == 200, second.text
    assert second.json()["is_first_user"] is False
    db_session.expire_all()
    assert db_session.query(Company).count() == 1
    joiner = db_session.query(User).filter_by(email="bootstrap-2@example.com").one()
    assert joiner.company_id == company.id
    assert (joiner.role, joiner.is_active, joiner.is_superuser) == (UserRole.VIEWER, False, False)


def test_bootstrap_ignores_company_fields_smuggled_into_the_body(client, db_session):
    """The public body has no company field; extra keys must not name or create one."""
    body = dict(_public_body(3), company_name="Fictional Fabrication", company_id=999, slug="fictional")

    response = client.post("/api/v1/auth/register-public", json=body)

    assert response.status_code in (200, 422), response.text
    db_session.expire_all()
    assert [c.slug for c in db_session.query(Company).all()] == ["werco"]
    assert {u.company_id for u in db_session.query(User).all()} <= {1}


# --- What stays ------------------------------------------------------------------------


def test_every_other_platform_route_still_answers_for_a_platform_admin(client, db_session):
    actor = _platform_admin(db_session)
    h = _auth(actor)

    listing = client.get("/api/v1/platform/companies", headers=h)
    assert listing.status_code == 200, listing.text
    assert [c["id"] for c in listing.json()] == [1]

    detail = client.get("/api/v1/platform/companies/1", headers=h)
    assert detail.status_code == 200, detail.text
    assert detail.json()["slug"] == "werco"

    users = client.get("/api/v1/platform/companies/1/users", headers=h)
    assert users.status_code == 200, users.text
    assert actor.email in {u["email"] for u in users.json()}

    dashboard = client.get("/api/v1/platform/companies/1/dashboard", headers=h)
    assert dashboard.status_code == 200, dashboard.text

    overview = client.get("/api/v1/platform/overview", headers=h)
    assert overview.status_code == 200, overview.text

    updated = client.put("/api/v1/platform/companies/1", headers=h, json={"phone": "555-0100"})
    assert updated.status_code == 200, updated.text
    db_session.expire_all()
    assert db_session.get(Company, 1).phone == "555-0100"
    # The update is still audited in the target company.
    assert db_session.query(AuditLog).filter_by(resource_type="company", resource_id=1, company_id=1).count() == 1


def test_platform_admin_can_still_switch_company_context(client, db_session):
    actor = _platform_admin(db_session)

    response = client.post("/api/v1/auth/switch-company/1", headers=_auth(actor))

    assert response.status_code == 200, response.text
    assert response.json()["access_token"]


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.MANAGER])
def test_remaining_platform_routes_still_refuse_tenant_roles(client, db_session, role):
    """Keeping the routes must not have loosened their gate."""
    user = User(
        company_id=1,
        email=f"tenant-{role.value}@example.com",
        employee_id=f"TENANT-{role.value}",
        first_name="Tenant",
        last_name="Person",
        role=role,
        is_active=True,
        hashed_password=get_password_hash(PASSWORD),
    )
    db_session.add(user)
    db_session.commit()
    h = _auth(user)

    for path in (
        "/api/v1/platform/companies",
        "/api/v1/platform/companies/1",
        "/api/v1/platform/companies/1/users",
        "/api/v1/platform/companies/1/dashboard",
        "/api/v1/platform/overview",
    ):
        assert client.get(path, headers=h).status_code == 403, path
