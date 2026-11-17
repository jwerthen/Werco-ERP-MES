"""HTTP coverage for the preventive-maintenance auto-evidence verdict.

Companion to ``tests/services/test_auto_evidence_maintenance.py``. Drives the live path

    GET /api/v1/qms-standards/clauses/{clause_id}/auto-evidence

for an "Infrastructure" clause in the scenario that used to read "compliant": active PM
schedules that have never been performed, first due in the future, and no maintenance work
orders. The endpoint calls the rule with no injected ``today`` (the company's local date), so
due dates are built from ``company_today``.
"""

from datetime import timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.company import Company
from app.models.maintenance import MaintenanceFrequency, MaintenanceSchedule, MaintenanceType
from app.models.qms_standard import QMSClause, QMSStandard
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.services.company_time import company_today

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
        company = Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True)
        db.add(company)
        db.commit()
    return company


def _make_user(db: Session, *, company_id: int) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"pm-evidence-{n}@co{company_id}.test",
        employee_id=f"PMEV-{n:05d}",
        first_name="Evidence",
        last_name=f"C{company_id}",
        hashed_password="$2b$12$abcdefghijklmnopqrstuv",
        role=UserRole.QUALITY,
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


def _make_infrastructure_clause(db: Session, *, company_id: int) -> QMSClause:
    _ensure_company(db, company_id)
    standard = QMSStandard(name=f"STD-PM-API-{_next()}", version="2016", is_active=True, company_id=company_id)
    db.add(standard)
    db.flush()
    clause = QMSClause(standard_id=standard.id, company_id=company_id, clause_number="7.1.3", title="Infrastructure")
    db.add(clause)
    db.commit()
    db.refresh(clause)
    return clause


def _seed_schedules(db: Session, *, company_id: int, count: int, performed: bool) -> None:
    _ensure_company(db, company_id)
    today = company_today(db, company_id)
    n = _next()
    wc = WorkCenter(company_id=company_id, code=f"PMAPI-{n:05d}", name=f"Test Mill {n}", work_center_type="machining")
    db.add(wc)
    db.flush()
    for i in range(count):
        db.add(
            MaintenanceSchedule(
                company_id=company_id,
                work_center_id=wc.id,
                name=f"Spindle check {n}-{i}",
                maintenance_type=MaintenanceType.PREVENTIVE,
                frequency=MaintenanceFrequency.MONTHLY,
                next_due_date=today + timedelta(days=15),
                last_completed_date=(today - timedelta(days=15)) if performed else None,
                is_active=True,
            )
        )
    db.commit()


def _maintenance_block(body: dict) -> dict:
    blocks = [e for e in body["discovered_evidence"] if e["title"] == "Preventive Maintenance & Infrastructure"]
    assert len(blocks) == 1, body["discovered_evidence"]
    return blocks[0]


def test_auto_evidence_never_performed_schedules_not_compliant(client: TestClient, db_session: Session):
    _seed_schedules(db_session, company_id=COMPANY_A, count=3, performed=False)
    # Company B's schedules are all performed and current; they must not leak into A's verdict.
    _seed_schedules(db_session, company_id=COMPANY_B, count=2, performed=True)
    clause = _make_infrastructure_clause(db_session, company_id=COMPANY_A)
    user = _make_user(db_session, company_id=COMPANY_A)

    resp = client.get(f"/api/v1/qms-standards/clauses/{clause.id}/auto-evidence", headers=_headers_for(user))

    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["clause_id"] == clause.id
    assert body["overall_suggested_compliance"] != "compliant"
    assert body["overall_suggested_compliance"] == "not_assessed"

    pm = _maintenance_block(body)
    assert pm["health_status"] == "warning"
    assert pm["suggested_compliance"] == "not_assessed"
    assert pm["recent_count"] == 3
    assert pm["total_count"] == 0
    assert "No PM performed yet" in pm["health_detail"]


def test_auto_evidence_performed_schedules_compliant(client: TestClient, db_session: Session):
    """Control: the same clause reads compliant once the active schedules are performed."""
    _seed_schedules(db_session, company_id=COMPANY_A, count=3, performed=True)
    clause = _make_infrastructure_clause(db_session, company_id=COMPANY_A)
    user = _make_user(db_session, company_id=COMPANY_A)

    resp = client.get(f"/api/v1/qms-standards/clauses/{clause.id}/auto-evidence", headers=_headers_for(user))

    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["overall_suggested_compliance"] == "compliant"
    pm = _maintenance_block(body)
    assert (pm["health_status"], pm["suggested_compliance"]) == ("healthy", "compliant")
    assert pm["recent_count"] == 3
