"""PUT /vendors/{vendor_id} — editable vendor code + committed audit coverage.

Locks in the feat/vendor-code-editable change to ``app.api.endpoints.purchasing``:

- ``VendorUpdate`` gained an optional ``code`` field (min_length=2, max_length=20,
  pattern ``^[A-Z0-9\\-]+$``) with a mode='before' validator that strips and
  uppercases, mirroring ``VendorBase`` — so a lowercase code normalizes BEFORE the
  pattern constraint runs.
- ``update_vendor`` re-normalizes (``strip().upper()``), rejects an explicit
  null/blank code with 400 "Vendor code cannot be blank", and enforces
  PER-COMPANY uniqueness with 400 "Vendor code already exists" (the DB backs this
  with ``uq_vendors_company_code`` on (company_id, code)).
- ``update_vendor`` now logs through ``AuditService.log_update`` BEFORE the
  terminal ``db.commit()`` (this handler was previously unaudited). The snapshot
  is column-only, so the vestigial ``VendorUpdate.version`` field (Vendor has no
  version column) never enters the audited changes diff.

Audit assertions use the committed-only pattern from
tests/api/test_customers_audit_persistence.py / test_qms_soft_delete_audit.py:
the ``client`` fixture shares ONE open session with the endpoint, so a
flushed-but-uncommitted audit row would still be visible to a naive query.
``_committed_audit_rows`` rolls back BEFORE querying — only a row committed
atomically with the vendor change survives. We never insert AuditLog rows
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
COMPANY_B = 2
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
        email=f"vend-code-{n}@co{company_id}.test",
        employee_id=f"VENDCODE-{n:05d}",
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


def _reload_vendor(db: Session, vendor_id: int) -> Vendor:
    """Re-read the vendor through the shared session, dropping identity-map state."""
    db.expire_all()
    return db.query(Vendor).filter(Vendor.id == vendor_id).first()


def _audit_rows(db: Session, *, resource_id: int, action: str = None):
    """Fetch AuditLog rows for the vendor resource, newest first."""
    db.expire_all()
    q = db.query(AuditLog).filter(
        AuditLog.resource_type == RESOURCE,
        AuditLog.resource_id == resource_id,
    )
    if action is not None:
        q = q.filter(AuditLog.action == action)
    return q.order_by(AuditLog.sequence_number.desc()).all()


def _committed_audit_rows(db: Session, *, resource_id: int, action: str = None):
    """Fetch AuditLog rows that were actually COMMITTED, not merely flushed.

    Rolling back BEFORE querying guards against the audit-after-commit bug class:
    a committed audit row survives the rollback, a flushed-but-uncommitted one is
    discarded. See the module docstring.
    """
    db.rollback()
    return _audit_rows(db, resource_id=resource_id, action=action)


# ---------------------------------------------------------------------------
# Rename: lowercase input normalizes to uppercase and persists
# ---------------------------------------------------------------------------


def test_rename_vendor_code_normalizes_lowercase_to_uppercase(client: TestClient, db_session: Session):
    """PUT with a lowercase, whitespace-padded code succeeds; the response AND the
    persisted row carry the stripped, uppercased code (schema before-validator +
    endpoint normalization, matching the CSV import path)."""
    admin = _make_user(db_session)
    vendor = _make_vendor(db_session)

    resp = client.put(
        f"/api/v1/purchasing/vendors/{vendor.id}",
        headers=_headers_for(admin),
        json={"version": 0, "code": "  acme-77 "},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["code"] == "ACME-77"

    row = _reload_vendor(db_session, vendor.id)
    assert row.code == "ACME-77"


def test_resending_own_code_unchanged_is_not_a_duplicate(client: TestClient, db_session: Session):
    """The frontend always includes ``code`` in the PUT payload. Re-sending the
    vendor's OWN code (even lowercased) alongside another field change must not
    trip the uniqueness check — the duplicate probe only runs when the normalized
    code actually differs."""
    admin = _make_user(db_session)
    vendor = _make_vendor(db_session)
    own_code = vendor.code
    original_name = vendor.name

    resp = client.put(
        f"/api/v1/purchasing/vendors/{vendor.id}",
        headers=_headers_for(admin),
        json={"version": 0, "code": own_code.lower(), "name": "Same Code New Name"},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["code"] == own_code
    assert resp.json()["name"] == "Same Code New Name"

    # The audit diff must not claim a code change that didn't happen.
    rows = _committed_audit_rows(db_session, resource_id=vendor.id, action="UPDATE")
    assert len(rows) == 1
    changes = (rows[0].extra_data or {}).get("changes") or {}
    assert "code" not in changes
    assert changes.get("name") == {"old": original_name, "new": "Same Code New Name"}


# ---------------------------------------------------------------------------
# Uniqueness: per-company, not global
# ---------------------------------------------------------------------------


def test_duplicate_code_within_same_company_returns_400(client: TestClient, db_session: Session):
    """Renaming a vendor to another vendor's code in the SAME company is a 400
    with detail "Vendor code already exists", and nothing is persisted or audited."""
    admin = _make_user(db_session)
    vendor_a = _make_vendor(db_session)
    vendor_b = _make_vendor(db_session)
    original_b_code = vendor_b.code

    # Lowercase on purpose: the collision must be detected on the NORMALIZED code.
    resp = client.put(
        f"/api/v1/purchasing/vendors/{vendor_b.id}",
        headers=_headers_for(admin),
        json={"version": 0, "code": vendor_a.code.lower()},
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
    assert resp.json()["detail"] == "Vendor code already exists"

    row = _reload_vendor(db_session, vendor_b.id)
    assert row.code == original_b_code

    # A rejected update must not write an UPDATE audit row.
    assert _committed_audit_rows(db_session, resource_id=vendor_b.id, action="UPDATE") == []


def test_same_code_in_different_company_is_allowed(client: TestClient, db_session: Session):
    """The uniqueness constraint is per-company (uq_vendors_company_code): a
    company-2 vendor may take a code that a company-1 vendor already holds."""
    vendor_a = _make_vendor(db_session, company_id=COMPANY_A)
    admin_b = _make_user(db_session, company_id=COMPANY_B)
    vendor_b = _make_vendor(db_session, company_id=COMPANY_B)

    resp = client.put(
        f"/api/v1/purchasing/vendors/{vendor_b.id}",
        headers=_headers_for(admin_b),
        json={"version": 0, "code": vendor_a.code.lower()},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["code"] == vendor_a.code

    # Both rows now hold the code, one per company.
    db_session.expire_all()
    holders = db_session.query(Vendor).filter(Vendor.code == vendor_a.code).all()
    assert {(v.id, v.company_id) for v in holders} == {
        (vendor_a.id, COMPANY_A),
        (vendor_b.id, COMPANY_B),
    }


# ---------------------------------------------------------------------------
# Tenant isolation & RBAC on the now-more-powerful update endpoint
# ---------------------------------------------------------------------------


def test_cross_tenant_vendor_update_is_404_and_untouched(client: TestClient, db_session: Session):
    """A company-B admin PUTting a company-A vendor_id (valid body incl. code)
    gets 404 "Vendor not found" — the lookup is scoped to the caller's active
    company via get_current_company_id, so another tenant's row is invisible,
    not merely forbidden — and the company-A row is unchanged. The canonical
    tenant-isolation regression now that this endpoint can rename vendor codes."""
    vendor_a = _make_vendor(db_session, company_id=COMPANY_A)
    original_code = vendor_a.code
    original_name = vendor_a.name
    admin_b = _make_user(db_session, company_id=COMPANY_B)

    resp = client.put(
        f"/api/v1/purchasing/vendors/{vendor_a.id}",
        headers=_headers_for(admin_b),
        json={"version": 0, "code": "HIJACK-1", "name": "Hijacked Vendor"},
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
    assert resp.json()["detail"] == "Vendor not found"

    row = _reload_vendor(db_session, vendor_a.id)
    assert row.code == original_code
    assert row.name == original_name
    assert row.company_id == COMPANY_A

    # No mutation happened, so no UPDATE audit row may exist either.
    assert _committed_audit_rows(db_session, resource_id=vendor_a.id, action="UPDATE") == []


def test_update_vendor_rejects_unprivileged_role_403(client: TestClient, db_session: Session):
    """update_vendor is gated by require_role([ADMIN, MANAGER]): an OPERATOR in
    the vendor's OWN company gets 403 "Insufficient permissions" and the row is
    unchanged — the RBAC dependency rejects before the handler loads the vendor."""
    vendor = _make_vendor(db_session)
    original_code = vendor.code
    original_name = vendor.name
    operator = _make_user(db_session, role=UserRole.OPERATOR)

    resp = client.put(
        f"/api/v1/purchasing/vendors/{vendor.id}",
        headers=_headers_for(operator),
        json={"version": 0, "code": "OPER-DENIED-1", "name": "Operator Rename"},
    )
    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text
    assert resp.json()["detail"] == "Insufficient permissions"

    row = _reload_vendor(db_session, vendor.id)
    assert row.code == original_code
    assert row.name == original_name

    assert _committed_audit_rows(db_session, resource_id=vendor.id, action="UPDATE") == []


# ---------------------------------------------------------------------------
# Blank / null code is rejected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("blank_code", ["", "   "])
def test_blank_code_is_rejected_by_schema_422(client: TestClient, db_session: Session, blank_code: str):
    """An empty or whitespace-only code strips to "" in the before-validator and
    then fails min_length=2 — a 422 (string_too_short) on the ``code`` field.
    The endpoint's 400 guard is the backstop for explicit null (next test)."""
    admin = _make_user(db_session)
    vendor = _make_vendor(db_session)
    original_code = vendor.code

    resp = client.put(
        f"/api/v1/purchasing/vendors/{vendor.id}",
        headers=_headers_for(admin),
        json={"version": 0, "code": blank_code},
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, resp.text
    errors = resp.json()["detail"]
    code_errors = [e for e in errors if e["loc"][-1] == "code"]
    assert code_errors, f"expected a validation error on 'code', got: {errors}"
    assert code_errors[0]["type"] == "string_too_short"

    row = _reload_vendor(db_session, vendor.id)
    assert row.code == original_code


def test_explicit_null_code_is_rejected_400_cannot_be_blank(client: TestClient, db_session: Session):
    """``code: null`` passes the Optional schema field but is an explicit attempt
    to blank the code — the endpoint rejects it with 400 "Vendor code cannot be
    blank" and persists nothing."""
    admin = _make_user(db_session)
    vendor = _make_vendor(db_session)
    original_code = vendor.code

    resp = client.put(
        f"/api/v1/purchasing/vendors/{vendor.id}",
        headers=_headers_for(admin),
        json={"version": 0, "code": None},
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
    assert resp.json()["detail"] == "Vendor code cannot be blank"

    row = _reload_vendor(db_session, vendor.id)
    assert row.code == original_code

    assert _committed_audit_rows(db_session, resource_id=vendor.id, action="UPDATE") == []


# ---------------------------------------------------------------------------
# Omitting code leaves it untouched
# ---------------------------------------------------------------------------


def test_omitting_code_updates_other_fields_and_preserves_code(client: TestClient, db_session: Session):
    """A payload without ``code`` (exclude_unset) still updates other fields and
    leaves the code exactly as it was — omission is not blanking."""
    admin = _make_user(db_session)
    vendor = _make_vendor(db_session)
    original_code = vendor.code

    resp = client.put(
        f"/api/v1/purchasing/vendors/{vendor.id}",
        headers=_headers_for(admin),
        json={"version": 0, "name": "Renamed Without Code", "payment_terms": "NET 45"},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["code"] == original_code
    assert body["name"] == "Renamed Without Code"
    assert body["payment_terms"] == "NET 45"

    row = _reload_vendor(db_session, vendor.id)
    assert row.code == original_code
    assert row.name == "Renamed Without Code"

    # The audit diff records what changed — and code is not in it.
    rows = _committed_audit_rows(db_session, resource_id=vendor.id, action="UPDATE")
    assert len(rows) == 1
    changes = (rows[0].extra_data or {}).get("changes") or {}
    assert "code" not in changes
    assert set(changes) == {"name", "payment_terms"}


# ---------------------------------------------------------------------------
# Audit: the update writes a COMMITTED UPDATE row carrying the code old→new
# ---------------------------------------------------------------------------


def test_code_change_persists_committed_update_audit_row(client: TestClient, db_session: Session):
    """PUT /vendors/{id} emits exactly one COMMITTED UPDATE AuditLog row for
    resource_type 'vendor' whose changes diff carries the code old→new, tagged
    with the acting user and company. The row is logged BEFORE the handler's
    commit, so it commits atomically with the vendor change.

    FAILS against audit-after-commit code: the row would land in a never-committed
    transaction and be discarded by the rollback in ``_committed_audit_rows``.
    FAILS against the previously-unaudited handler: no row at all.
    """
    admin = _make_user(db_session)
    vendor = _make_vendor(db_session)
    old_code = vendor.code

    resp = client.put(
        f"/api/v1/purchasing/vendors/{vendor.id}",
        headers=_headers_for(admin),
        json={"version": 0, "code": "aud-rename-1"},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    rows = _committed_audit_rows(db_session, resource_id=vendor.id, action="UPDATE")
    assert len(rows) == 1, "expected exactly one COMMITTED UPDATE audit row for the vendor code change"
    row = rows[0]
    assert row.action == "UPDATE"
    assert row.resource_type == RESOURCE
    assert row.resource_id == vendor.id
    # resource_identifier is captured after mutation — the new, normalized code.
    assert row.resource_identifier == "AUD-RENAME-1"
    assert row.company_id == COMPANY_A
    assert row.user_id == admin.id

    # Old/new snapshots carry the code transition...
    assert row.old_values["code"] == old_code
    assert row.new_values["code"] == "AUD-RENAME-1"

    # ...and the changes diff is exactly the code change: the column-only snapshot
    # keeps the vestigial VendorUpdate.version (no Vendor column) out of the diff.
    changes = (row.extra_data or {}).get("changes") or {}
    assert changes == {"code": {"old": old_code, "new": "AUD-RENAME-1"}}
