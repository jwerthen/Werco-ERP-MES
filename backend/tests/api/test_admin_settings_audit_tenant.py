"""Tenant-attribution coverage for the admin-settings audit log.

Locks in the fix to ``log_change`` in
``app/api/endpoints/admin_settings.py`` (branch qa/full-pass-2026-06-04).
``log_change`` is the single chokepoint that builds every ``SettingsAuditLog``
row. It now tags the row with the *active* company resolved from the request --
``current_user._active_company_id`` (the company a platform admin switched into,
which ``get_current_company_id`` returns) -- falling back to the user's home
``company_id`` only when there is no active context (e.g. a background/login
path). Previously it used ``current_user.company_id`` (the HOME company), which
mis-attributed a platform admin's cross-company settings change to their own
home tenant. This mirrors ``AuditService._resolve_company_id`` so settings
audits attribute to the same tenant as every other write.

Scope / accuracy note (don't overstate severity)
-------------------------------------------------
The production switch-company flow (``app/api/endpoints/auth.py``) mints a
``read_only=True`` token for any switch into a *different* company, and
``get_current_user`` (deps.py) blocks non-GET requests under a read-only
context. So a real platform admin cannot today drive a settings WRITE while
switched into another company. The API-level headline test below therefore
deliberately mints a NON-read-only token for a switched context purely to
exercise the ``log_change`` write path. It locks in the tenant-attribution
invariant for defense-in-depth and parity with ``AuditService`` -- it is NOT
reproducing a currently-exploitable production path. The direct ``log_change``
unit tests are the unconditional guard on the attribution logic itself.

``SettingsAuditLog`` is a ``TenantMixin`` table (``company_id`` NOT NULL) defined
in ``app/models/quote_config.py``. Company id=1 ("Werco Manufacturing") is
pre-seeded by the ``db_session`` fixture; companies 2 and 3 are created here.
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.endpoints.admin_settings import log_change
from app.core.security import create_access_token
from app.models.company import Company
from app.models.quote_config import SettingsAuditLog
from app.models.user import User, UserRole

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"  # tokens are minted directly; never used for login

# Module-level counter so every user row gets a globally unique natural key,
# even across companies and across tests sharing a worker DB under -n auto.
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _ensure_company(db: Session, company_id: int) -> Company:
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        company = Company(
            id=company_id,
            name=f"Company {company_id}",
            slug=f"company-{company_id}",
            is_active=True,
        )
        db.add(company)
        db.commit()
    return company


def make_user(
    db: Session,
    *,
    role: UserRole,
    company_id: int,
    is_superuser: bool = False,
) -> User:
    """Persist and return a user in ``company_id`` with a globally-unique key.

    A freshly persisted/queried User has no ``_active_company_id`` attribute --
    that is only attached by ``get_current_user`` on a real request -- so a user
    returned here models the login / background-job path until a test sets it.
    """
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"settings-user-{n}@co{company_id}.test",
        employee_id=f"SET-{n:05d}",
        first_name=role.value.title(),
        last_name=f"C{company_id}",
        hashed_password=TEST_PASSWORD_HASH,
        role=role,
        is_active=True,
        is_superuser=is_superuser,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def headers_for(user: User, *, active_company_id: int = None) -> dict:
    """Auth headers for ``user``.

    ``active_company_id`` mints the token with a different ``company_id`` claim
    than the user's home company -- the way a platform-admin "switched" context
    is simulated (``get_current_user`` sets ``user._active_company_id`` from this
    claim and ``get_current_company_id`` returns it). Defaults to the user's home
    company. ``read_only`` is left False so the switched-context write path is
    reachable (see the module docstring's scope note).
    """
    cid = active_company_id if active_company_id is not None else user.company_id
    token = create_access_token(subject=user.id, company_id=cid)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def _material_payload(name: str) -> dict:
    """Minimal valid body for POST /materials.

    ``MaterialCreate`` (app/schemas/admin_settings.py) requires only ``name``
    (str) and ``category`` (a ``MaterialCategory`` enum value); every other field
    has a default. ``"steel"`` is ``MaterialCategory.STEEL``.
    """
    return {"name": name, "category": "steel"}


# ---------------------------------------------------------------------------
# 1. Headline (API-level): a switched platform admin attributes the audit row
#    to the ACTIVE company, not their home company.
# ---------------------------------------------------------------------------


def test_settings_write_attributes_audit_to_active_company(client: TestClient, db_session: Session):
    """A PLATFORM_ADMIN whose home company is 1, switched into company 2, makes a
    settings WRITE through ``log_change``. The resulting ``SettingsAuditLog`` row
    must carry company_id == 2 (the ACTIVE company), NOT 1 (home).

    This is the regression guard for the fix: against the pre-fix code (which
    used ``current_user.company_id``) the row would be stamped 1 and this would
    fail. ``admin_only = require_role([UserRole.ADMIN])`` admits a PLATFORM_ADMIN,
    so the switched token can hit the endpoint.
    """
    _ensure_company(db_session, 2)
    platform_admin = make_user(db_session, role=UserRole.PLATFORM_ADMIN, company_id=1)

    name = f"Inconel {_next()}"
    resp = client.post(
        "/api/v1/admin/settings/materials",
        json=_material_payload(name),
        headers=headers_for(platform_admin, active_company_id=2),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    # The audit row for this create must be stamped with the ACTIVE company (2).
    audit = (
        db_session.query(SettingsAuditLog)
        .filter(
            SettingsAuditLog.entity_type == "material",
            SettingsAuditLog.entity_name == name,
        )
        .one()
    )
    assert audit.action == "create"
    assert audit.changed_by == platform_admin.id
    assert audit.company_id == 2  # ACTIVE company, NOT the admin's home company (1)
    assert platform_admin.company_id == 1  # sanity: home company is unchanged


def test_overhead_write_attributes_audit_to_active_company(client: TestClient, db_session: Session):
    """Same invariant via a different endpoint/body: PUT /overhead/{key}
    (``SettingUpdate``) by a switched platform admin stamps the ACTIVE company."""
    _ensure_company(db_session, 2)
    platform_admin = make_user(db_session, role=UserRole.PLATFORM_ADMIN, company_id=1)

    key = f"markup_pct_{_next()}"
    resp = client.put(
        f"/api/v1/admin/settings/overhead/{key}",
        json={"value": "1.25", "setting_type": "number"},
        headers=headers_for(platform_admin, active_company_id=2),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    audit = (
        db_session.query(SettingsAuditLog)
        .filter(
            SettingsAuditLog.entity_type == "overhead",
            SettingsAuditLog.entity_name == key,
        )
        .one()
    )
    assert audit.company_id == 2  # ACTIVE company, NOT home (1)


# ---------------------------------------------------------------------------
# 2. Baseline (API-level): a normal company-1 ADMIN attributes to company 1.
# ---------------------------------------------------------------------------


def test_settings_write_attributes_audit_to_home_company_for_normal_admin(client: TestClient, db_session: Session):
    """A normal company-1 ADMIN performing the same write tags the audit row with
    company_id == 1. Here the active company *is* the home company, so the row is
    correctly attributed to 1 (and never leaks to another tenant)."""
    admin1 = make_user(db_session, role=UserRole.ADMIN, company_id=1)

    name = f"6061-T6 {_next()}"
    resp = client.post(
        "/api/v1/admin/settings/materials",
        json=_material_payload(name),
        headers=headers_for(admin1),  # no switch: active == home == 1
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    audit = (
        db_session.query(SettingsAuditLog)
        .filter(
            SettingsAuditLog.entity_type == "material",
            SettingsAuditLog.entity_name == name,
        )
        .one()
    )
    assert audit.action == "create"
    assert audit.changed_by == admin1.id
    assert audit.company_id == 1


# ---------------------------------------------------------------------------
# 3. Direct unit tests of log_change (no HTTP) -- the unconditional guard.
# ---------------------------------------------------------------------------


def test_log_change_falls_back_to_home_company_without_active_context(
    db_session: Session,
):
    """``log_change`` with a user that has NO ``_active_company_id`` stamps the
    user's home company. Mirrors
    test_resolve_company_id_falls_back_to_home_company and proves the fallback
    branch (the login / background path)."""
    user = make_user(db_session, role=UserRole.ADMIN, company_id=3)
    assert not hasattr(user, "_active_company_id")  # freshly queried: no switch context

    log_change(
        db_session,
        "material",
        entity_id=1,
        entity_name=f"FallbackMat {_next()}",
        action="create",
        current_user=user,
    )
    db_session.commit()

    audit = db_session.query(SettingsAuditLog).filter(SettingsAuditLog.changed_by == user.id).one()
    assert audit.company_id == 3  # home company, since there is no active context


def test_log_change_prefers_active_company_over_home(db_session: Session):
    """``log_change`` with ``_active_company_id`` set to a different company stamps
    the ACTIVE one, not the home company. This is the core of the fix exercised
    directly, independent of the read-only HTTP guard."""
    _ensure_company(db_session, 2)
    user = make_user(db_session, role=UserRole.PLATFORM_ADMIN, company_id=1)
    user._active_company_id = 2  # switched context, as get_current_user would set

    log_change(
        db_session,
        "overhead",
        entity_id=None,
        entity_name=f"ActiveWins {_next()}",
        action="update",
        current_user=user,
    )
    db_session.commit()

    audit = db_session.query(SettingsAuditLog).filter(SettingsAuditLog.changed_by == user.id).one()
    assert audit.company_id == 2  # active company wins
    assert user.company_id == 1  # sanity: home company unchanged
