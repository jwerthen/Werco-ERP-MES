"""API-level tenant-isolation coverage for QMS auto-evidence discovery.

Companion to tests/services/test_auto_evidence_tenant_isolation.py. Exercises
the live HTTP path that the fix on branch qa/full-pass-2026-06-04 hardened:

    GET /api/v1/qms-standards/clauses/{clause_id}/auto-evidence

The endpoint injects ``company_id`` from ``get_current_company_id`` (the JWT's
active-company claim) and passes it into ``discover_evidence_for_clause``.
Headline invariant: the discovered-evidence counts a caller receives reflect
ONLY their active company -- another company's rows must not leak in.

The clause itself carries no ``company_id`` (qms_clauses is not tenant-scoped),
so the same clause id is queried by both a company-A and a company-B caller;
the only thing that differs is the token's company claim, and that alone must
change the counts.

NOTE: the sibling ``POST /{standard_id}/auto-link`` endpoint is not exercised
here -- it filters on ``QMSStandard.company_id``, a column the model/test schema
does not define -- so the read endpoint is the API-level target. The persistence
side of auto-link is covered structurally by the service-level suite.
"""

from datetime import datetime, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.company import Company
from app.models.qms_standard import QMSClause, QMSStandard
from app.models.quality import NCRSource, NCRStatus, NonConformanceReport
from app.models.user import User, UserRole

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2

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


def _make_user(db: Session, *, company_id: int, role: UserRole = UserRole.QUALITY) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"evidence-api-{n}@co{company_id}.test",
        employee_id=f"EVDA-{n:05d}",
        first_name="Evidence",
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


def _headers_for(user: User, *, active_company_id: int = None) -> dict:
    cid = active_company_id if active_company_id is not None else user.company_id
    token = create_access_token(subject=user.id, company_id=cid)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def _make_ncr_clause(db: Session) -> QMSClause:
    """A clause whose text routes to the NCR rule via keyword matching."""
    standard = QMSStandard(name=f"STD-{_next()}", version="2015", is_active=True)
    db.add(standard)
    db.flush()
    clause = QMSClause(
        standard_id=standard.id,
        clause_number="8.7",
        title="Control of nonconforming output",
        description="handling of nonconforming product",
    )
    db.add(clause)
    db.commit()
    db.refresh(clause)
    return clause


def _make_ncr(db: Session, *, company_id: int) -> None:
    n = _next()
    ncr = NonConformanceReport(
        company_id=company_id,
        ncr_number=f"NCR-API-{n:06d}",
        title=f"NCR {n}",
        description="dimensional out of spec",
        source=NCRSource.IN_PROCESS,
        status=NCRStatus.CLOSED,
        created_at=datetime.utcnow() - timedelta(days=1),
    )
    db.add(ncr)


def _seed_ncrs(db: Session) -> dict:
    _ensure_company(db, COMPANY_A)
    _ensure_company(db, COMPANY_B)
    a_total, b_total = 2, 4
    for _ in range(a_total):
        _make_ncr(db, company_id=COMPANY_A)
    for _ in range(b_total):
        _make_ncr(db, company_id=COMPANY_B)
    db.commit()
    return {"a_total": a_total, "b_total": b_total}


def _ncr_evidence(body: dict) -> dict:
    """Pull the single NCR evidence block out of an auto-evidence response."""
    ncr_blocks = [e for e in body["discovered_evidence"] if e["evidence_type"] == "ncr"]
    assert len(ncr_blocks) == 1, body["discovered_evidence"]
    return ncr_blocks[0]


def test_auto_evidence_endpoint_scoped_to_active_company(client: TestClient, db_session: Session):
    """Company A's caller sees only A's NCR count; B's larger population is absent."""
    seeded = _seed_ncrs(db_session)
    clause = _make_ncr_clause(db_session)
    quality_a = _make_user(db_session, company_id=COMPANY_A, role=UserRole.QUALITY)

    resp = client.get(
        f"/api/v1/qms-standards/clauses/{clause.id}/auto-evidence",
        headers=_headers_for(quality_a),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["clause_id"] == clause.id

    ncr = _ncr_evidence(body)
    # Only A's NCRs: not a_total + b_total.
    assert ncr["total_count"] == seeded["a_total"]
    assert len(ncr["examples"]) == seeded["a_total"]
    identifiers = {ex["record_identifier"] for ex in ncr["examples"]}
    assert len(identifiers) == seeded["a_total"]


def test_auto_evidence_endpoint_company_b_sees_only_b(client: TestClient, db_session: Session):
    """Symmetric control: the SAME clause, queried with a company-B token,
    returns B's count -- proving the token's company claim is what scopes it."""
    seeded = _seed_ncrs(db_session)
    clause = _make_ncr_clause(db_session)
    quality_b = _make_user(db_session, company_id=COMPANY_B, role=UserRole.QUALITY)

    resp = client.get(
        f"/api/v1/qms-standards/clauses/{clause.id}/auto-evidence",
        headers=_headers_for(quality_b),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    ncr = _ncr_evidence(resp.json())
    assert ncr["total_count"] == seeded["b_total"]


def test_auto_evidence_endpoint_platform_admin_switched_company(client: TestClient, db_session: Session):
    """A platform admin whose token is switched into company B sees B's count,
    even though the admin's home company is A -- exactly how
    get_current_company_id scopes a context-switched request."""
    seeded = _seed_ncrs(db_session)
    clause = _make_ncr_clause(db_session)
    admin = _make_user(db_session, company_id=COMPANY_A, role=UserRole.PLATFORM_ADMIN)

    resp = client.get(
        f"/api/v1/qms-standards/clauses/{clause.id}/auto-evidence",
        headers=_headers_for(admin, active_company_id=COMPANY_B),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    ncr = _ncr_evidence(resp.json())
    assert ncr["total_count"] == seeded["b_total"]
