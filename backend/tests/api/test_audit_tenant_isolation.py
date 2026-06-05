"""Tenant-isolation + RBAC coverage for the audit endpoints.

Locks in the behaviour added on branch qa/full-pass-2026-06-04 that makes the
tamper-evident audit log tenant-aware. Headline invariant: an admin in company A
must never retrieve company B's audit entries.

Covers:
- GET /api/v1/audit/ , /summary , /actions , /resource-types are scoped to the
  caller's active company (A sees only A's rows; counts reflect only A).
- GET /api/v1/audit/integrity/record/{seq}: a company-scoped ADMIN gets 404 for
  a record belonging to another company, 200 for one of its own; a platform
  admin gets 200 for either.
- GET /api/v1/audit/integrity/{verify,verify-recent,status} were tightened from
  ADMIN to platform-admin: a company ADMIN now gets 403; platform admin /
  superuser get 200.

Audit rows are seeded through AuditService (so they are stamped + chained the
real way). Company-B rows are written by a platform admin switched into company
2, exactly as get_current_company_id would scope a real cross-company write.
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.user import User, UserRole
from app.services.audit_service import AuditService

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"  # tokens are minted directly; never used for login

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


def make_user(
    db: Session,
    *,
    role: UserRole,
    company_id: int,
    is_superuser: bool = False,
) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"user{n}@co{company_id}.test",
        employee_id=f"EMP-{n:05d}",
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
    is simulated (get_current_user sets user._active_company_id from this claim).
    Defaults to the user's home company.
    """
    cid = active_company_id if active_company_id is not None else user.company_id
    token = create_access_token(subject=user.id, company_id=cid)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def seed_audit_rows(db: Session) -> dict:
    """Write a small, valid, interleaved audit chain spanning two companies.

    Returns a dict of useful sequence numbers / ids for assertions:
      {"a_seqs", "b_seqs", "a_record_seq", "b_record_seq",
       "a_actions", "b_actions", "a_resources", "b_resources"}.

    All rows go through AuditService so they are stamped with company_id and
    linked via the real hash chain. Company-2 rows are written by a platform
    admin switched into company 2.
    """
    _ensure_company(db, 1)
    _ensure_company(db, 2)

    writer1 = make_user(db, role=UserRole.PLATFORM_ADMIN, company_id=1)

    # --- Company 1 rows: distinctive actions/resources ---
    svc1 = AuditService(db, writer1)  # resolves to company 1 (home)
    a_rows = []
    a_rows.append(svc1.log(action="CREATE", resource_type="part", resource_id=101, resource_identifier="A-PART-1"))
    a_rows.append(svc1.log(action="UPDATE", resource_type="part", resource_id=101, resource_identifier="A-PART-1"))
    a_rows.append(svc1.log(action="DELETE", resource_type="work_order", resource_id=201, resource_identifier="A-WO-1"))
    db.flush()

    # --- Company 2 rows: a *different* action + resource set, via a switch ---
    writer1._active_company_id = 2
    svc2 = AuditService(db, writer1)  # now resolves to company 2
    assert svc2.company_id == 2
    b_rows = []
    b_rows.append(svc2.log(action="APPROVE", resource_type="quote", resource_id=301, resource_identifier="B-QUOTE-1"))
    b_rows.append(svc2.log(action="EXPORT", resource_type="shipment", resource_id=302, resource_identifier="B-SHIP-1"))
    db.commit()

    for r in a_rows:
        assert r is not None and r.company_id == 1
    for r in b_rows:
        assert r is not None and r.company_id == 2

    return {
        "a_seqs": [r.sequence_number for r in a_rows],
        "b_seqs": [r.sequence_number for r in b_rows],
        "a_record_seq": a_rows[0].sequence_number,
        "b_record_seq": b_rows[0].sequence_number,
        "a_actions": {"CREATE", "UPDATE", "DELETE"},
        "b_actions": {"APPROVE", "EXPORT"},
        "a_resources": {"part", "work_order"},
        "b_resources": {"quote", "shipment"},
    }


# ---------------------------------------------------------------------------
# 1. GET /api/v1/audit/ is scoped to the active company
# ---------------------------------------------------------------------------


def test_list_audit_logs_only_returns_own_company(client: TestClient, db_session: Session):
    """A company-1 admin listing audit logs sees only company-1 rows."""
    seed = seed_audit_rows(db_session)
    admin1 = make_user(db_session, role=UserRole.ADMIN, company_id=1)

    resp = client.get("/api/v1/audit/", headers=headers_for(admin1))
    assert resp.status_code == status.HTTP_200_OK, resp.text

    rows = resp.json()
    seqs = {r["sequence_number"] for r in rows}
    # Every company-1 row is present; no company-2 row leaks in.
    assert set(seed["a_seqs"]).issubset(seqs)
    assert seqs.isdisjoint(set(seed["b_seqs"]))
    # And the distinctive company-2 identifiers never appear.
    identifiers = {r["resource_identifier"] for r in rows}
    assert "B-QUOTE-1" not in identifiers
    assert "B-SHIP-1" not in identifiers


def test_list_audit_logs_company_b_sees_only_b(client: TestClient, db_session: Session):
    """Symmetric control: a company-2 admin sees only company-2 rows."""
    seed = seed_audit_rows(db_session)
    admin2 = make_user(db_session, role=UserRole.ADMIN, company_id=2)

    resp = client.get("/api/v1/audit/", headers=headers_for(admin2))
    assert resp.status_code == status.HTTP_200_OK, resp.text

    seqs = {r["sequence_number"] for r in resp.json()}
    assert set(seed["b_seqs"]).issubset(seqs)
    assert seqs.isdisjoint(set(seed["a_seqs"]))


# ---------------------------------------------------------------------------
# 2. /summary , /actions , /resource-types are scoped to the active company
# ---------------------------------------------------------------------------


def test_summary_counts_reflect_only_own_company(client: TestClient, db_session: Session):
    """Company-1 summary counts only company-1 events; B's actions are absent."""
    seed = seed_audit_rows(db_session)
    admin1 = make_user(db_session, role=UserRole.ADMIN, company_id=1)

    resp = client.get("/api/v1/audit/summary", headers=headers_for(admin1))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()

    # 3 company-1 rows were seeded in the last 30 days; only those count.
    assert body["total_events"] == len(seed["a_seqs"])
    by_action = body["by_action"]
    # Company-1 actions present, company-2 actions absent.
    assert set(by_action).issuperset(seed["a_actions"])
    assert set(by_action).isdisjoint(seed["b_actions"])
    by_resource = body["by_resource"]
    assert set(by_resource).issuperset(seed["a_resources"])
    assert set(by_resource).isdisjoint(seed["b_resources"])


def test_actions_endpoint_scoped_to_company(client: TestClient, db_session: Session):
    """/actions returns only the distinct actions present for the caller's company."""
    seed = seed_audit_rows(db_session)
    admin1 = make_user(db_session, role=UserRole.ADMIN, company_id=1)

    resp = client.get("/api/v1/audit/actions", headers=headers_for(admin1))
    assert resp.status_code == status.HTTP_200_OK, resp.text

    actions = set(resp.json())
    assert actions == seed["a_actions"]
    assert actions.isdisjoint(seed["b_actions"])


def test_resource_types_endpoint_scoped_to_company(client: TestClient, db_session: Session):
    """/resource-types returns only the distinct resources for the caller's company."""
    seed = seed_audit_rows(db_session)
    admin1 = make_user(db_session, role=UserRole.ADMIN, company_id=1)

    resp = client.get("/api/v1/audit/resource-types", headers=headers_for(admin1))
    assert resp.status_code == status.HTTP_200_OK, resp.text

    resources = set(resp.json())
    assert resources == seed["a_resources"]
    assert resources.isdisjoint(seed["b_resources"])


# ---------------------------------------------------------------------------
# 3. GET /integrity/record/{seq} tenant isolation (404 for the other company)
# ---------------------------------------------------------------------------


def test_record_cross_company_is_404_for_company_admin(client: TestClient, db_session: Session):
    """A company-1 admin requesting a company-2 record gets 404 (not 403)."""
    seed = seed_audit_rows(db_session)
    admin1 = make_user(db_session, role=UserRole.ADMIN, company_id=1)

    resp = client.get(
        f"/api/v1/audit/integrity/record/{seed['b_record_seq']}",
        headers=headers_for(admin1),
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert resp.json()["detail"] == "Audit record not found"


def test_record_own_company_is_200_for_company_admin(client: TestClient, db_session: Session):
    """Positive control: a company-1 admin can fetch a company-1 record."""
    seed = seed_audit_rows(db_session)
    admin1 = make_user(db_session, role=UserRole.ADMIN, company_id=1)

    resp = client.get(
        f"/api/v1/audit/integrity/record/{seed['a_record_seq']}",
        headers=headers_for(admin1),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["sequence_number"] == seed["a_record_seq"]
    assert body["hash_valid"] is True
    assert body["chain_valid"] is True


def test_record_any_company_is_200_for_platform_admin(client: TestClient, db_session: Session):
    """A platform admin can fetch records from either company."""
    seed = seed_audit_rows(db_session)
    platform_admin = make_user(db_session, role=UserRole.PLATFORM_ADMIN, company_id=1)
    headers = headers_for(platform_admin)

    resp_a = client.get(f"/api/v1/audit/integrity/record/{seed['a_record_seq']}", headers=headers)
    resp_b = client.get(f"/api/v1/audit/integrity/record/{seed['b_record_seq']}", headers=headers)

    assert resp_a.status_code == status.HTTP_200_OK, resp_a.text
    assert resp_b.status_code == status.HTTP_200_OK, resp_b.text
    assert resp_a.json()["sequence_number"] == seed["a_record_seq"]
    assert resp_b.json()["sequence_number"] == seed["b_record_seq"]


def test_record_any_company_is_200_for_superuser(client: TestClient, db_session: Session):
    """A superuser (is_superuser, non-platform role) can fetch either record."""
    seed = seed_audit_rows(db_session)
    superuser = make_user(db_session, role=UserRole.ADMIN, company_id=1, is_superuser=True)
    headers = headers_for(superuser)

    resp_b = client.get(f"/api/v1/audit/integrity/record/{seed['b_record_seq']}", headers=headers)
    assert resp_b.status_code == status.HTTP_200_OK, resp_b.text
    assert resp_b.json()["sequence_number"] == seed["b_record_seq"]


def test_record_missing_sequence_is_404(client: TestClient, db_session: Session):
    """A non-existent sequence number returns 404 for a platform admin too."""
    seed_audit_rows(db_session)
    platform_admin = make_user(db_session, role=UserRole.PLATFORM_ADMIN, company_id=1)

    resp = client.get("/api/v1/audit/integrity/record/999999", headers=headers_for(platform_admin))
    assert resp.status_code == status.HTTP_404_NOT_FOUND


# ---------------------------------------------------------------------------
# 4. /integrity/{verify,verify-recent,status} require platform admin
# ---------------------------------------------------------------------------

INTEGRITY_GLOBAL_ENDPOINTS = [
    "/api/v1/audit/integrity/verify",
    "/api/v1/audit/integrity/verify-recent",
    "/api/v1/audit/integrity/status",
]


@pytest.mark.parametrize("path", INTEGRITY_GLOBAL_ENDPOINTS)
def test_integrity_endpoints_forbidden_for_company_admin(client: TestClient, db_session: Session, path: str):
    """A company ADMIN is now 403 on the global integrity endpoints (tightened
    from require_role([ADMIN]) to require_platform_admin)."""
    seed_audit_rows(db_session)
    admin1 = make_user(db_session, role=UserRole.ADMIN, company_id=1)

    resp = client.get(path, headers=headers_for(admin1))
    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text


@pytest.mark.parametrize("path", INTEGRITY_GLOBAL_ENDPOINTS)
def test_integrity_endpoints_allowed_for_platform_admin(client: TestClient, db_session: Session, path: str):
    """A platform admin gets 200 on every global integrity endpoint."""
    seed_audit_rows(db_session)
    platform_admin = make_user(db_session, role=UserRole.PLATFORM_ADMIN, company_id=1)

    resp = client.get(path, headers=headers_for(platform_admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text


@pytest.mark.parametrize("path", INTEGRITY_GLOBAL_ENDPOINTS)
def test_integrity_endpoints_allowed_for_superuser(client: TestClient, db_session: Session, path: str):
    """A superuser also gets 200 on every global integrity endpoint."""
    seed_audit_rows(db_session)
    superuser = make_user(db_session, role=UserRole.ADMIN, company_id=1, is_superuser=True)

    resp = client.get(path, headers=headers_for(superuser))
    assert resp.status_code == status.HTTP_200_OK, resp.text


def test_integrity_verify_reports_valid_chain(client: TestClient, db_session: Session):
    """Sanity on the verify payload: the seeded chain verifies as valid for a
    platform admin (the chain spans both companies and stays intact)."""
    seed = seed_audit_rows(db_session)
    platform_admin = make_user(db_session, role=UserRole.PLATFORM_ADMIN, company_id=1)

    resp = client.get("/api/v1/audit/integrity/verify", headers=headers_for(platform_admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["is_valid"] is True
    assert body["chain_valid"] is True
    # All seeded rows (both companies) are part of the single global chain.
    expected = len(seed["a_seqs"]) + len(seed["b_seqs"])
    assert body["records_checked"] == expected
