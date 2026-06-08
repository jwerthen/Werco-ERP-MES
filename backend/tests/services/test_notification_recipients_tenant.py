"""Tenant-isolation tests for get_notification_recipients (Batch-1 hardening).

``get_notification_recipients`` originally returned every active user across ALL
tenants for a given role/department. The MRP and scheduling jobs (per-company)
used it to pick "managers" to email, so each tenant's run fanned its alerts out
to every other tenant's managers -- a tenant-isolation defect (invariant #1).

The function now takes an optional ``company_id``: when provided it restricts to
that tenant's users; when ``None`` (the default) it preserves the legacy
all-tenants behavior for the periodic cross-tenant jobs that still want it.
"""

import pytest
from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.user import User, UserRole
from app.services.notification_service import get_notification_recipients

# Module-level counter for globally-unique natural keys across the worker DB
# (tests run under -n auto with a shared per-worker SQLite file).
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _seed_company(db: Session, company_id: int) -> None:
    if not db.query(Company).filter(Company.id == company_id).first():
        db.add(Company(id=company_id, name=f"Co {company_id}", slug=f"notif-co-{company_id}", is_active=True))
        db.commit()


def _make_user(
    db: Session,
    *,
    company_id: int,
    role: UserRole = UserRole.MANAGER,
    department: str | None = None,
    is_active: bool = True,
) -> User:
    _seed_company(db, company_id)
    n = _next()
    user = User(
        email=f"notif-{n}@co{company_id}.test",
        employee_id=f"NOTIF-{n:05d}",
        first_name="Notif",
        last_name=f"C{company_id}",
        hashed_password="$2b$12$abcdefghijklmnopqrstuv",
        role=role,
        department=department,
        is_active=is_active,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.mark.requires_db
def test_recipients_scoped_to_company_excludes_other_tenant(db_session: Session):
    a_mgr = _make_user(db_session, company_id=1, role=UserRole.MANAGER)
    b_mgr = _make_user(db_session, company_id=2, role=UserRole.MANAGER)

    recipients = get_notification_recipients(db_session, role="manager", company_id=1)
    ids = {u.id for u in recipients}

    assert a_mgr.id in ids, "company 1's manager should be a recipient"
    assert b_mgr.id not in ids, "company 2's manager must NOT be a recipient when scoped to company 1"
    assert all(u.company_id == 1 for u in recipients), "every recipient must belong to the scoped company"


@pytest.mark.requires_db
def test_recipients_scoped_by_department_and_company(db_session: Session):
    a_purch = _make_user(db_session, company_id=1, role=UserRole.OPERATOR, department="Purchasing")
    b_purch = _make_user(db_session, company_id=2, role=UserRole.OPERATOR, department="Purchasing")

    recipients = get_notification_recipients(db_session, department="Purchasing", company_id=1)
    ids = {u.id for u in recipients}

    assert a_purch.id in ids
    assert b_purch.id not in ids
    assert all(u.company_id == 1 and u.department == "Purchasing" for u in recipients)


@pytest.mark.requires_db
def test_recipients_default_none_is_backward_compatible_all_tenants(db_session: Session):
    """Without company_id the legacy all-tenants behavior is preserved (the
    periodic notification_jobs tasks still rely on it)."""
    a_mgr = _make_user(db_session, company_id=1, role=UserRole.MANAGER)
    b_mgr = _make_user(db_session, company_id=2, role=UserRole.MANAGER)

    recipients = get_notification_recipients(db_session, role="manager")
    ids = {u.id for u in recipients}

    assert a_mgr.id in ids
    assert b_mgr.id in ids, "default (company_id=None) must keep returning all tenants' managers"


@pytest.mark.requires_db
def test_recipients_excludes_inactive_users_within_company(db_session: Session):
    active_mgr = _make_user(db_session, company_id=1, role=UserRole.MANAGER, is_active=True)
    inactive_mgr = _make_user(db_session, company_id=1, role=UserRole.MANAGER, is_active=False)

    recipients = get_notification_recipients(db_session, role="manager", company_id=1)
    ids = {u.id for u in recipients}

    assert active_mgr.id in ids
    assert inactive_mgr.id not in ids, "inactive users must never be notification recipients"
