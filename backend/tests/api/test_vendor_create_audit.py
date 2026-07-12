"""POST /vendors — create_vendor now writes a COMMITTED CREATE audit row.

Locks in the fix/vendor-create-audit-logging change to
``app.api.endpoints.purchasing.create_vendor``, which previously violated
CLAUDE.md invariant 2 (state changes must be recorded through ``AuditService``):

- The handler now takes ``audit: AuditService = Depends(get_audit_service)``
  and logs ``audit.log_create("vendor", vendor.id, vendor.code,
  new_values=vendor)`` between ``db.flush()`` and ``db.commit()`` — the flush
  surfaces a duplicate-code IntegrityError BEFORE the audit write, and the
  audit row commits ATOMICALLY with the vendor row.
- The existing ``except IntegrityError`` TOCTOU backstop (rollback + 400
  "Vendor code already exists") still discards both rows together when a
  concurrent create slips past the pre-insert probe.

Audit assertions use the committed-only pattern from
tests/api/test_vendor_code_update.py / test_customers_audit_persistence.py:
the ``client`` fixture shares ONE open session with the endpoint, so a
flushed-but-uncommitted audit row would still be visible to a naive query.
``_committed_audit_rows`` rolls back BEFORE querying — only a row committed
atomically with the vendor row survives. We never insert AuditLog rows
directly (tamper-evident hash chain); the endpoint produces them.

The default seeded company is id=1 (tests/conftest.py).
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.purchasing import Vendor
from app.models.user import User, UserRole

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"  # tokens are minted directly; never used for login

COMPANY_A = 1
RESOURCE = "vendor"

# Module-level counter so every fixture row gets a globally unique natural key,
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


def _make_user(db: Session, *, company_id: int = COMPANY_A, role: UserRole = UserRole.ADMIN) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"vend-create-{n}@co{company_id}.test",
        employee_id=f"VENDCREATE-{n:05d}",
        first_name="Vend",
        last_name=f"C{company_id}",
        hashed_password=TEST_PASSWORD_HASH,
        role=role,
        is_active=True,
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


def _make_vendor(db: Session, *, company_id: int = COMPANY_A, code: str = None) -> Vendor:
    """Insert a vendor row DIRECTLY (no endpoint) — produces no audit rows."""
    _ensure_company(db, company_id)
    n = _next()
    vendor = Vendor(
        code=code or f"VND-{n:05d}",
        name=f"Vendor {n}",
        contact_name="Pat Lee",
        email=f"vendor{n}@supplier.test",
        is_active=True,
        company_id=company_id,
    )
    db.add(vendor)
    db.commit()
    db.refresh(vendor)
    return vendor


def _audit_rows(db: Session, *, resource_id: int = None, resource_identifier: str = None, action: str = None):
    """Fetch vendor AuditLog rows, newest first.

    A rejected create never gets an id, so callers may filter by
    ``resource_identifier`` (the code) instead of ``resource_id``.
    """
    db.expire_all()
    q = db.query(AuditLog).filter(AuditLog.resource_type == RESOURCE)
    if resource_id is not None:
        q = q.filter(AuditLog.resource_id == resource_id)
    if resource_identifier is not None:
        q = q.filter(AuditLog.resource_identifier == resource_identifier)
    if action is not None:
        q = q.filter(AuditLog.action == action)
    return q.order_by(AuditLog.sequence_number.desc()).all()


def _committed_audit_rows(db: Session, **filters):
    """Fetch AuditLog rows that were actually COMMITTED, not merely flushed.

    Rolling back BEFORE querying guards against the audit-after-commit bug class:
    a committed audit row survives the rollback, a flushed-but-uncommitted one is
    discarded. See the module docstring.
    """
    db.rollback()
    return _audit_rows(db, **filters)


def _committed_vendors_with_code(db: Session, code: str):
    """Committed-only Vendor rows holding ``code`` (rollback first, then query)."""
    db.rollback()
    db.expire_all()
    return db.query(Vendor).filter(Vendor.code == code).all()


# ---------------------------------------------------------------------------
# Happy path: the create writes a COMMITTED CREATE row tagged user + company
# ---------------------------------------------------------------------------


def test_create_vendor_persists_committed_create_audit_row(client: TestClient, db_session: Session):
    """POST /vendors emits exactly one COMMITTED CREATE AuditLog row for
    resource_type 'vendor' carrying the new vendor's id, code, acting user, and
    company, with a new_values snapshot of the created row. The row is logged
    after ``db.flush()`` (so the vendor id exists) and BEFORE the handler's
    terminal ``db.commit()``, so it commits atomically with the vendor row.

    FAILS against the previously-unaudited handler: no CREATE row at all.
    FAILS against audit-after-commit code: the row would land in a
    never-committed transaction and be discarded by the rollback in
    ``_committed_audit_rows``.
    """
    admin = _make_user(db_session)
    code = f"AUD-CR-{_next()}"

    resp = client.post(
        "/api/v1/purchasing/vendors",
        headers=_headers_for(admin),
        json={"code": code, "name": "Audited Create Vendor"},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["id"] is not None
    assert body["code"] == code

    rows = _committed_audit_rows(db_session, resource_id=body["id"], action="CREATE")
    assert len(rows) == 1, "expected exactly one COMMITTED CREATE audit row for the new vendor"
    row = rows[0]
    assert row.action == "CREATE"
    assert row.resource_type == RESOURCE
    assert row.resource_id == body["id"]
    assert row.resource_identifier == code
    assert row.company_id == COMPANY_A
    assert row.user_id == admin.id

    # The new_values snapshot captures the created row's business fields.
    assert row.new_values["code"] == code
    assert row.new_values["name"] == "Audited Create Vendor"


# ---------------------------------------------------------------------------
# Duplicate code: 400, no second vendor row, and no audit row
# ---------------------------------------------------------------------------


def test_duplicate_code_create_returns_400_and_writes_no_audit_row(client: TestClient, db_session: Session):
    """POSTing a code that already exists in the company is a 400 with detail
    "Vendor code already exists" — and the rejected create leaves NOTHING
    behind: still exactly one Vendor row with that code (the fixture's), and
    zero committed CREATE audit rows carrying that code as resource_identifier.

    The pre-existing vendor is inserted directly by ``_make_vendor`` (fixture
    rows bypass the endpoint, so they produce no audit rows), and the rejected
    create never gets an id — hence the resource_identifier filter.
    """
    admin = _make_user(db_session)
    code = f"DUP-CR-{_next()}"
    existing = _make_vendor(db_session, code=code)

    resp = client.post(
        "/api/v1/purchasing/vendors",
        headers=_headers_for(admin),
        json={"code": code, "name": "Duplicate Code Vendor"},
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
    assert resp.json()["detail"] == "Vendor code already exists"

    holders = _committed_vendors_with_code(db_session, code)
    assert [(v.id, v.company_id) for v in holders] == [(existing.id, COMPANY_A)]

    assert _committed_audit_rows(db_session, resource_identifier=code, action="CREATE") == []


# ---------------------------------------------------------------------------
# RBAC: an operator cannot create vendors, and no row of any kind is written
# ---------------------------------------------------------------------------


def test_create_vendor_rejects_unprivileged_role_403_and_writes_no_row(client: TestClient, db_session: Session):
    """create_vendor is gated by require_role([ADMIN, MANAGER]): an OPERATOR
    gets 403 "Insufficient permissions" before the handler runs — so neither a
    Vendor row with that code nor a committed CREATE audit row carrying it as
    resource_identifier may exist afterwards."""
    operator = _make_user(db_session, role=UserRole.OPERATOR)
    code = f"OP-CR-{_next()}"

    resp = client.post(
        "/api/v1/purchasing/vendors",
        headers=_headers_for(operator),
        json={"code": code, "name": "Operator Denied Vendor"},
    )
    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text
    assert resp.json()["detail"] == "Insufficient permissions"

    assert _committed_vendors_with_code(db_session, code) == []
    assert _committed_audit_rows(db_session, resource_identifier=code, action="CREATE") == []
