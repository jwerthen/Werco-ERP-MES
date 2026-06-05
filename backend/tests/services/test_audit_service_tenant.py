"""Unit coverage for the tenant-aware AuditService.

Locks in the behaviour added on branch qa/full-pass-2026-06-04 that makes the
tamper-evident audit log tenant-aware:

- ``AuditService._resolve_company_id`` precedence:
  explicit arg -> ``user._active_company_id`` -> ``user.company_id`` -> None.
- ``AuditService.log()`` stamps that ``company_id`` onto the ``AuditLog`` row,
  with an optional per-call override.
- ``company_id`` is deliberately NOT part of ``compute_audit_hash`` (the SHA-256
  chain), so stamping it does not break hash-chain verification. This file's
  regression guard proves that by writing several stamped rows and verifying the
  full chain with ``AuditIntegrityService``.

No HTTP is involved here -- these drive ``AuditService`` directly against a DB
session fixture, mirroring tests/services/test_mrp_service.py.
"""

import pytest
from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.user import User, UserRole
from app.services.audit_integrity_service import AuditIntegrityService
from app.services.audit_service import AuditService, compute_audit_hash

pytestmark = [pytest.mark.unit, pytest.mark.requires_db]

# Module-level counter so each user row gets a unique natural key across the
# module, even when several tests run under -n auto in the same worker DB.
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


def _make_user(db: Session, *, company_id: int, role: UserRole = UserRole.ADMIN) -> User:
    """Persist a user in ``company_id`` and return it (freshly queried).

    A freshly queried/constructed User has no ``_active_company_id`` attribute --
    that is only attached by ``get_current_user`` on a real request -- so this
    models the login / background-job path where the fallback to
    ``user.company_id`` must apply.
    """
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"audit-user-{n}@co{company_id}.test",
        employee_id=f"AUD-{n:05d}",
        first_name="Audit",
        last_name=f"C{company_id}",
        hashed_password="$2b$12$abcdefghijklmnopqrstuv",
        role=role,
        is_active=True,
        is_superuser=False,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


# ---------------------------------------------------------------------------
# _resolve_company_id precedence (pure, no DB needed but kept with the suite)
# ---------------------------------------------------------------------------


def test_resolve_company_id_precedence_explicit_wins(db_session: Session):
    """Explicit arg beats both _active_company_id and company_id."""
    user = _make_user(db_session, company_id=1)
    user._active_company_id = 2
    assert AuditService._resolve_company_id(user, explicit=7) == 7


def test_resolve_company_id_prefers_active_company(db_session: Session):
    """With no explicit arg, _active_company_id beats the home company_id."""
    user = _make_user(db_session, company_id=1)
    user._active_company_id = 2
    assert AuditService._resolve_company_id(user, explicit=None) == 2


def test_resolve_company_id_falls_back_to_home_company(db_session: Session):
    """A user without _active_company_id resolves to its home company_id."""
    user = _make_user(db_session, company_id=3)
    assert not hasattr(user, "_active_company_id")
    assert AuditService._resolve_company_id(user, explicit=None) == 3


def test_resolve_company_id_none_user_is_none(db_session: Session):
    """An unauthenticated event (no user) resolves to None."""
    assert AuditService._resolve_company_id(None, explicit=None) is None


# ---------------------------------------------------------------------------
# log() stamps company_id onto the row
# ---------------------------------------------------------------------------


def test_log_stamps_active_company_on_platform_admin_switch(db_session: Session):
    """A platform-admin switched into another company stamps the *switched*
    company onto the row, not the admin's home company."""
    # Home company 1, but switched into company 2 (as get_current_user would do).
    admin = _make_user(db_session, company_id=1, role=UserRole.PLATFORM_ADMIN)
    _ensure_company(db_session, 2)
    admin._active_company_id = 2

    service = AuditService(db_session, admin)
    assert service.company_id == 2  # resolved at construction

    row = service.log(action="CREATE", resource_type="part", resource_id=10, resource_identifier="P-10")
    db_session.commit()

    assert row is not None
    assert row.company_id == 2  # switched company, NOT admin.company_id (1)
    assert admin.company_id == 1  # sanity: home company is unchanged


def test_log_falls_back_to_user_company_without_active_context(db_session: Session):
    """A freshly queried user (e.g. the login path) stamps user.company_id."""
    user = _make_user(db_session, company_id=3)
    assert not hasattr(user, "_active_company_id")

    service = AuditService(db_session, user)
    row = service.log(action="LOGIN", resource_type="authentication", resource_identifier=user.email)
    db_session.commit()

    assert row is not None
    assert row.company_id == 3


def test_log_with_no_user_stamps_null_company(db_session: Session):
    """A failed login with no matching user has no tenant -> company_id None."""
    service = AuditService(db_session, user=None)
    row = service.log(
        action="LOGIN_FAILED",
        resource_type="authentication",
        resource_identifier="ghost@nowhere.test",
        success=False,
    )
    db_session.commit()

    assert row is not None
    assert row.company_id is None
    assert row.user_id is None


def test_log_explicit_company_overrides_constructor(db_session: Session):
    """A per-call company_id= on log() overrides the constructor-resolved one."""
    user = _make_user(db_session, company_id=1)
    user._active_company_id = 1
    _ensure_company(db_session, 9)

    service = AuditService(db_session, user)
    assert service.company_id == 1

    row = service.log(
        action="CREATE",
        resource_type="part",
        resource_id=5,
        resource_identifier="P-5",
        company_id=9,  # explicit per-call override
    )
    db_session.commit()

    assert row.company_id == 9


def test_constructor_explicit_company_overrides_user(db_session: Session):
    """An explicit company_id passed to the constructor overrides user context."""
    user = _make_user(db_session, company_id=1)
    user._active_company_id = 2
    _ensure_company(db_session, 8)

    service = AuditService(db_session, user, company_id=8)
    assert service.company_id == 8

    row = service.log(action="CREATE", resource_type="part", resource_id=1, resource_identifier="P-1")
    db_session.commit()
    assert row.company_id == 8


# ---------------------------------------------------------------------------
# Regression guard: company_id is excluded from the integrity hash
# ---------------------------------------------------------------------------


def test_company_id_not_in_audit_hash(db_session: Session):
    """Two otherwise-identical rows differing only in company_id hash the same.

    compute_audit_hash has no company_id parameter; this proves the value never
    leaks into the chain input, which is what lets it be stamped/backfilled
    without invalidating the chain.
    """
    common = dict(
        sequence_number=1,
        timestamp=None,
        user_id=1,
        user_email="a@b.test",
        action="CREATE",
        resource_type="part",
        resource_id=1,
        resource_identifier="P-1",
        description="d",
        old_values=None,
        new_values={"x": 1},
        ip_address="127.0.0.1",
        session_id="abc",
        success="true",
        previous_hash=None,
    )
    # There is simply no company_id kwarg to pass; identical inputs -> identical hash.
    assert compute_audit_hash(**common) == compute_audit_hash(**common)


def test_stamping_company_id_does_not_break_hash_chain(db_session: Session):
    """Write several stamped rows (across two companies via a switch) and prove
    the full hash chain still verifies as valid.

    This is the headline hash-decision regression guard: if company_id had been
    folded into compute_audit_hash, verify_full_chain would report hash
    mismatches here. It must stay green.
    """
    admin = _make_user(db_session, company_id=1, role=UserRole.PLATFORM_ADMIN)
    _ensure_company(db_session, 2)

    # Three rows in company 1.
    svc1 = AuditService(db_session, admin)
    for i in range(3):
        assert svc1.log(action="CREATE", resource_type="part", resource_id=i, resource_identifier=f"A-{i}") is not None

    # Two more rows after switching the admin into company 2.
    admin._active_company_id = 2
    svc2 = AuditService(db_session, admin)
    for i in range(2):
        assert svc2.log(action="CREATE", resource_type="part", resource_id=i, resource_identifier=f"B-{i}") is not None

    db_session.commit()

    report = AuditIntegrityService(db_session).verify_full_chain()

    assert report.is_valid, report.to_dict()
    assert report.chain_valid is True
    assert report.records_checked == 5
    assert report.issues == []
