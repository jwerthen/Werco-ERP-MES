"""Tenant-isolation coverage for the QMS standards/clauses/evidence endpoints.

Locks in the fix on branch qa/full-pass-2026-06-04 that makes
``app.api.endpoints.qms_standards`` fully tenant-scoped after ``QMSStandard``,
``QMSClause`` and ``QMSClauseEvidence`` gained a NOT-NULL ``company_id`` via
``TenantMixin``. Every lookup now filters by the caller's active ``company_id``
(from ``get_current_company_id``), every create stamps it, and
``GET /audit-readiness`` aggregates only the active company's rows.

Headline invariants proved here:
- Cross-tenant reads/writes of a standard return **404** (not 403): the system
  must not disclose that another company's standard even exists.
- ``GET /`` lists only the caller's company's standards.
- ``GET /audit-readiness`` counts reflect only the caller's company -- this is
  the cross-tenant aggregation leak the fix closed.
- Creating a clause/evidence under another company's standard/clause is 404, and
  a created standard/clause is stamped with the *caller's* company, never the
  one named in the request.

The multi-company fixture shape mirrors tests/api/test_audit_tenant_isolation.py:
a second company plus a user/token for it. The default seeded company is id=1
(tests/conftest.py); company 2 is created on demand.
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.company import Company
from app.models.qms_standard import QMSClause, QMSClauseEvidence, QMSStandard
from app.models.user import User, UserRole

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2

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


def _make_user(db: Session, *, company_id: int, role: UserRole = UserRole.ADMIN) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"qms-user-{n}@co{company_id}.test",
        employee_id=f"QMS-{n:05d}",
        first_name="Qms",
        last_name=f"C{company_id}",
        hashed_password="$2b$12$abcdefghijklmnopqrstuv",  # tokens are minted directly; never used for login
        role=role,
        is_active=True,
        is_superuser=False,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _headers_for(user: User, *, active_company_id: int = None) -> dict:
    """Auth headers for ``user``.

    ``active_company_id`` mints the token with a different ``company_id`` claim
    than the user's home company -- how a platform-admin "switched" context is
    simulated. Defaults to the user's home company.
    """
    cid = active_company_id if active_company_id is not None else user.company_id
    token = create_access_token(subject=user.id, company_id=cid)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def _make_standard(db: Session, *, company_id: int, name: str = None, is_active: bool = True) -> QMSStandard:
    _ensure_company(db, company_id)
    n = _next()
    standard = QMSStandard(
        company_id=company_id,
        name=name or f"STD-{n}",
        version="2015",
        standard_body="ISO",
        is_active=is_active,
    )
    db.add(standard)
    db.commit()
    db.refresh(standard)
    return standard


def _make_clause(
    db: Session,
    *,
    standard: QMSStandard,
    compliance_status: str = "compliant",
) -> QMSClause:
    n = _next()
    clause = QMSClause(
        company_id=standard.company_id,
        standard_id=standard.id,
        clause_number=f"{n}.0",
        title=f"Clause {n}",
        description="requirement text",
        compliance_status=compliance_status,
    )
    db.add(clause)
    db.commit()
    db.refresh(clause)
    return clause


def _make_evidence(db: Session, *, clause: QMSClause, is_verified: bool = False) -> QMSClauseEvidence:
    n = _next()
    evidence = QMSClauseEvidence(
        company_id=clause.company_id,
        clause_id=clause.id,
        evidence_type="document",
        title=f"Evidence {n}",
        description="how this satisfies the clause",
        is_verified=is_verified,
    )
    db.add(evidence)
    db.commit()
    db.refresh(evidence)
    return evidence


# ---------------------------------------------------------------------------
# 1. Cross-tenant read/update/delete of a standard returns 404 (not 403)
# ---------------------------------------------------------------------------


def test_get_standard_cross_company_is_404(client: TestClient, db_session: Session):
    """A company-A admin requesting company B's standard gets 404 -- no
    cross-tenant existence disclosure (404, never 403)."""
    b_standard = _make_standard(db_session, company_id=COMPANY_B)
    admin_a = _make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN)

    resp = client.get(f"/api/v1/qms-standards/{b_standard.id}", headers=_headers_for(admin_a))
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
    assert resp.json()["detail"] == "QMS standard not found"


def test_get_own_standard_is_200(client: TestClient, db_session: Session):
    """Positive control: the same admin can read its own company's standard."""
    a_standard = _make_standard(db_session, company_id=COMPANY_A, name="AS9100D")
    admin_a = _make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN)

    resp = client.get(f"/api/v1/qms-standards/{a_standard.id}", headers=_headers_for(admin_a))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["id"] == a_standard.id
    assert resp.json()["name"] == "AS9100D"


def test_update_standard_cross_company_is_404(client: TestClient, db_session: Session):
    """A company-A admin cannot PUT company B's standard -- 404, and B's row is
    left untouched."""
    b_standard = _make_standard(db_session, company_id=COMPANY_B, name="B-Original")
    admin_a = _make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN)

    resp = client.put(
        f"/api/v1/qms-standards/{b_standard.id}",
        headers=_headers_for(admin_a),
        json={"name": "Hijacked-By-A"},
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text

    # B's standard is unchanged.
    db_session.expire_all()
    refreshed = db_session.query(QMSStandard).filter(QMSStandard.id == b_standard.id).first()
    assert refreshed.name == "B-Original"


def test_delete_standard_cross_company_is_404(client: TestClient, db_session: Session):
    """A company-A admin cannot DELETE company B's standard -- 404, and B's row
    survives."""
    b_standard = _make_standard(db_session, company_id=COMPANY_B)
    admin_a = _make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN)

    resp = client.delete(f"/api/v1/qms-standards/{b_standard.id}", headers=_headers_for(admin_a))
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text

    # B's standard still exists.
    still_there = db_session.query(QMSStandard).filter(QMSStandard.id == b_standard.id).first()
    assert still_there is not None


# ---------------------------------------------------------------------------
# 2. GET / lists only the caller's company's standards
# ---------------------------------------------------------------------------


def test_list_standards_only_returns_own_company(client: TestClient, db_session: Session):
    """The list endpoint returns only the caller's standards; B's are absent."""
    a1 = _make_standard(db_session, company_id=COMPANY_A, name="A-STD-1")
    a2 = _make_standard(db_session, company_id=COMPANY_A, name="A-STD-2")
    b1 = _make_standard(db_session, company_id=COMPANY_B, name="B-STD-1")
    admin_a = _make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN)

    resp = client.get("/api/v1/qms-standards/", headers=_headers_for(admin_a))
    assert resp.status_code == status.HTTP_200_OK, resp.text

    ids = {row["id"] for row in resp.json()}
    assert {a1.id, a2.id}.issubset(ids)
    assert b1.id not in ids


def test_list_standards_company_b_sees_only_b(client: TestClient, db_session: Session):
    """Symmetric control: a company-B admin sees only B's standards."""
    a1 = _make_standard(db_session, company_id=COMPANY_A, name="A-STD-1")
    b1 = _make_standard(db_session, company_id=COMPANY_B, name="B-STD-1")
    admin_b = _make_user(db_session, company_id=COMPANY_B, role=UserRole.ADMIN)

    resp = client.get("/api/v1/qms-standards/", headers=_headers_for(admin_b))
    assert resp.status_code == status.HTTP_200_OK, resp.text

    ids = {row["id"] for row in resp.json()}
    assert b1.id in ids
    assert a1.id not in ids


# ---------------------------------------------------------------------------
# 3. GET /audit-readiness is scoped to the active company (the closed leak)
# ---------------------------------------------------------------------------


def test_audit_readiness_counts_scoped_to_company(client: TestClient, db_session: Session):
    """``/audit-readiness`` aggregates ONLY the caller's company.

    Seed a deliberately *larger* population for company B so a leak would be
    obvious: A gets 1 standard / 2 clauses / 1 evidence link; B gets 2 standards
    / 5 clauses / 3 evidence links. A's summary must report A's totals exactly.
    """
    # --- Company A: 1 standard, 2 clauses, 1 evidence link ---
    a_std = _make_standard(db_session, company_id=COMPANY_A)
    a_c1 = _make_clause(db_session, standard=a_std, compliance_status="compliant")
    _make_clause(db_session, standard=a_std, compliance_status="partial")
    _make_evidence(db_session, clause=a_c1, is_verified=True)

    # --- Company B: 2 standards, 5 clauses, 3 evidence links (the would-be leak) ---
    b_std1 = _make_standard(db_session, company_id=COMPANY_B)
    b_std2 = _make_standard(db_session, company_id=COMPANY_B)
    for _ in range(3):
        _make_clause(db_session, standard=b_std1, compliance_status="compliant")
    b_c = _make_clause(db_session, standard=b_std2, compliance_status="non_compliant")
    _make_clause(db_session, standard=b_std2, compliance_status="not_assessed")
    for _ in range(3):
        _make_evidence(db_session, clause=b_c)

    admin_a = _make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN)
    resp = client.get("/api/v1/qms-standards/audit-readiness", headers=_headers_for(admin_a))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()

    # A's totals exactly -- not inflated by B's larger population.
    assert body["total_standards"] == 1
    assert body["total_clauses"] == 2
    assert body["total_evidence_links"] == 1
    assert body["verified_evidence"] == 1
    assert body["compliant"] == 1
    assert body["partial"] == 1
    # B's distinctive statuses must not bleed in.
    assert body["non_compliant"] == 0


def test_audit_readiness_company_b_sees_only_b(client: TestClient, db_session: Session):
    """Symmetric control: the SAME seeded data, queried by a company-B admin,
    reports B's larger totals -- proving the token's company claim scopes it."""
    a_std = _make_standard(db_session, company_id=COMPANY_A)
    _make_clause(db_session, standard=a_std, compliance_status="compliant")

    b_std1 = _make_standard(db_session, company_id=COMPANY_B)
    b_std2 = _make_standard(db_session, company_id=COMPANY_B)
    for _ in range(3):
        _make_clause(db_session, standard=b_std1, compliance_status="compliant")
    b_c = _make_clause(db_session, standard=b_std2, compliance_status="non_compliant")
    for _ in range(3):
        _make_evidence(db_session, clause=b_c)

    admin_b = _make_user(db_session, company_id=COMPANY_B, role=UserRole.ADMIN)
    resp = client.get("/api/v1/qms-standards/audit-readiness", headers=_headers_for(admin_b))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()

    assert body["total_standards"] == 2
    assert body["total_clauses"] == 4
    assert body["total_evidence_links"] == 3
    assert body["non_compliant"] == 1


# ---------------------------------------------------------------------------
# 4. Creates are stamped with the caller's company; cross-tenant nesting 404s
# ---------------------------------------------------------------------------


def test_create_standard_stamped_with_caller_company(client: TestClient, db_session: Session):
    """A created standard is persisted under the caller's active company."""
    admin_a = _make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN)

    resp = client.post(
        "/api/v1/qms-standards/",
        headers=_headers_for(admin_a),
        json={"name": "ISO 9001:2015", "version": "2015"},
    )
    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    new_id = resp.json()["id"]

    row = db_session.query(QMSStandard).filter(QMSStandard.id == new_id).first()
    assert row is not None
    assert row.company_id == COMPANY_A


def test_create_clause_under_other_company_standard_is_404(client: TestClient, db_session: Session):
    """Creating a clause under company B's standard returns 404 for a company-A
    admin, and no clause is created."""
    b_standard = _make_standard(db_session, company_id=COMPANY_B)
    admin_a = _make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN)

    resp = client.post(
        f"/api/v1/qms-standards/{b_standard.id}/clauses",
        headers=_headers_for(admin_a),
        json={"clause_number": "8.5.2", "title": "Identification and traceability"},
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
    # Nothing was created against B's standard.
    assert db_session.query(QMSClause).filter(QMSClause.standard_id == b_standard.id).count() == 0


def test_create_clause_under_own_standard_is_stamped(client: TestClient, db_session: Session):
    """A clause created under the caller's own standard is stamped with the
    caller's company."""
    a_standard = _make_standard(db_session, company_id=COMPANY_A)
    admin_a = _make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN)

    resp = client.post(
        f"/api/v1/qms-standards/{a_standard.id}/clauses",
        headers=_headers_for(admin_a),
        json={"clause_number": "8.5.2", "title": "Identification and traceability"},
    )
    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    clause_id = resp.json()["id"]

    row = db_session.query(QMSClause).filter(QMSClause.id == clause_id).first()
    assert row is not None
    assert row.company_id == COMPANY_A
    assert row.standard_id == a_standard.id


def test_add_evidence_under_other_company_clause_is_404(client: TestClient, db_session: Session):
    """Adding evidence to company B's clause returns 404 for a company-A admin,
    and no evidence row is created."""
    b_standard = _make_standard(db_session, company_id=COMPANY_B)
    b_clause = _make_clause(db_session, standard=b_standard)
    admin_a = _make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN)

    resp = client.post(
        f"/api/v1/qms-standards/clauses/{b_clause.id}/evidence",
        headers=_headers_for(admin_a),
        json={"evidence_type": "document", "title": "Procedure QP-7.5"},
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
    assert db_session.query(QMSClauseEvidence).filter(QMSClauseEvidence.clause_id == b_clause.id).count() == 0
