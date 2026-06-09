"""Behavior locks for the tenant-scoped operator-certification endpoints
(fix/wo-followups-round2, FIX 1).

Seven more endpoints in ``app/api/endpoints/operator_certifications.py`` were scoped to the
caller's active company via ``get_current_company_id`` (invariant #1):
  - ``GET  /certifications/dashboard``                 (certification_dashboard, aggregates)
  - ``GET  /certifications/expiring``                  (get_expiring_certifications)
  - ``GET  /certifications/user/{user_id}``            (get_user_certifications)
  - ``GET  /certifications/{cert_id}``                 (get_certification, by-id)
  - ``GET  /training/user/{user_id}``                  (get_user_training)
  - ``PUT  /training/{training_id}``                   (update_training, by-id)
  - ``PUT  /skill-matrix/{entry_id}``                  (update_skill_entry, by-id)

These tests authenticate as a COMPANY_A caller and prove a COMPANY_B row is invisible:
by-id GET/PUT return 404 cross-tenant (before any mutation), the dashboard counts only the
caller's company, and the user-scoped lists never surface another tenant's rows.
"""

from datetime import date, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.company import Company
from app.models.operator_certification import (
    CertificationStatus,
    CertificationType,
    OperatorCertification,
    SkillMatrix,
    TrainingRecord,
)
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2
TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"
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


def make_user(db: Session, *, role: UserRole = UserRole.ADMIN, company_id: int = COMPANY_A) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"cert-fu-{n}@co{company_id}.test",
        employee_id=f"CERTFU-{n:05d}",
        first_name="Cert",
        last_name=f"Co{company_id}",
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


def headers_for(user: User) -> dict:
    token = create_access_token(subject=user.id, company_id=user.company_id)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def make_work_center(db: Session, *, company_id: int = COMPANY_A) -> WorkCenter:
    _ensure_company(db, company_id)
    n = _next()
    wc = WorkCenter(
        name=f"CERTFU-WC-{n}",
        code=f"CERTFU-WC-{n}",
        work_center_type="welding",
        description="cert fixture work center",
        hourly_rate=100,
        is_active=True,
        company_id=company_id,
    )
    db.add(wc)
    db.commit()
    db.refresh(wc)
    return wc


def make_cert(
    db: Session,
    user: User,
    *,
    company_id: int = COMPANY_A,
    expiration_date=None,
    status_: CertificationStatus = CertificationStatus.ACTIVE,
) -> OperatorCertification:
    n = _next()
    cert = OperatorCertification(
        user_id=user.id,
        certification_type=CertificationType.WELDING,
        certification_name=f"Cert {n}",
        status=status_,
        expiration_date=expiration_date,
        company_id=company_id,
    )
    db.add(cert)
    db.commit()
    db.refresh(cert)
    return cert


def make_training(db: Session, user: User, *, company_id: int = COMPANY_A) -> TrainingRecord:
    n = _next()
    record = TrainingRecord(
        user_id=user.id,
        training_name=f"Training {n}",
        training_date=date.today(),
        hours=4.0,
        passed=True,
        company_id=company_id,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def make_skill(db: Session, user: User, wc: WorkCenter, *, company_id: int = COMPANY_A) -> SkillMatrix:
    sm = SkillMatrix(
        user_id=user.id,
        work_center_id=wc.id,
        skill_level=3,
        is_active=True,
        company_id=company_id,
    )
    db.add(sm)
    db.commit()
    db.refresh(sm)
    return sm


# ---------------------------------------------------------------------------
# get_certification (by-id): cross-tenant 404
# ---------------------------------------------------------------------------


def test_get_certification_cross_tenant_404(client: TestClient, db_session: Session):
    admin_a = make_user(db_session, company_id=COMPANY_A)
    op_b = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_B)
    cert_b = make_cert(db_session, op_b, company_id=COMPANY_B)
    db_session.commit()

    resp = client.get(f"/api/v1/certifications/certifications/{cert_b.id}", headers=headers_for(admin_a))
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text


def test_get_certification_sees_own_company_row(client: TestClient, db_session: Session):
    admin_a = make_user(db_session, company_id=COMPANY_A)
    op_a = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_A)
    cert_a = make_cert(db_session, op_a, company_id=COMPANY_A)
    db_session.commit()

    resp = client.get(f"/api/v1/certifications/certifications/{cert_a.id}", headers=headers_for(admin_a))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["id"] == cert_a.id


# ---------------------------------------------------------------------------
# get_user_certifications: excludes company-B rows for the same user_id
# ---------------------------------------------------------------------------


def test_get_user_certifications_excludes_company_b_rows(client: TestClient, db_session: Session):
    admin_a = make_user(db_session, company_id=COMPANY_A)
    # Same operator identity exists in both tenants is not realistic; use distinct users but
    # query with a user_id that has a company-B cert -- the company-A caller must see nothing.
    op_b = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_B)
    make_cert(db_session, op_b, company_id=COMPANY_B)
    db_session.commit()

    resp = client.get(f"/api/v1/certifications/certifications/user/{op_b.id}", headers=headers_for(admin_a))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json() == []


def test_get_user_certifications_returns_only_company_a_rows(client: TestClient, db_session: Session):
    admin_a = make_user(db_session, company_id=COMPANY_A)
    op_a = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_A)
    cert_a = make_cert(db_session, op_a, company_id=COMPANY_A)
    db_session.commit()

    resp = client.get(f"/api/v1/certifications/certifications/user/{op_a.id}", headers=headers_for(admin_a))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["id"] == cert_a.id


# ---------------------------------------------------------------------------
# get_expiring_certifications: company-B expiring cert never surfaces
# ---------------------------------------------------------------------------


def test_get_expiring_certifications_excludes_company_b(client: TestClient, db_session: Session):
    admin_a = make_user(db_session, company_id=COMPANY_A)
    op_a = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_A)
    op_b = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_B)
    soon = date.today() + timedelta(days=10)
    cert_a = make_cert(db_session, op_a, company_id=COMPANY_A, expiration_date=soon)
    cert_b = make_cert(db_session, op_b, company_id=COMPANY_B, expiration_date=soon)
    db_session.commit()

    resp = client.get("/api/v1/certifications/certifications/expiring?days=30", headers=headers_for(admin_a))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    returned_ids = {r["id"] for r in resp.json()}
    assert cert_a.id in returned_ids
    assert cert_b.id not in returned_ids


# ---------------------------------------------------------------------------
# certification_dashboard: aggregates count ONLY the caller's company
# ---------------------------------------------------------------------------


def test_certification_dashboard_counts_only_callers_company(client: TestClient, db_session: Session):
    admin_a = make_user(db_session, company_id=COMPANY_A)
    op_a = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_A)
    op_b = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_B)
    # One active cert in A, two in B (noise that must NOT be counted).
    make_cert(db_session, op_a, company_id=COMPANY_A)
    make_cert(db_session, op_b, company_id=COMPANY_B)
    make_cert(db_session, op_b, company_id=COMPANY_B)
    db_session.commit()

    resp = client.get("/api/v1/certifications/certifications/dashboard", headers=headers_for(admin_a))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    # Only company A's single cert is counted; company B's two are excluded.
    assert body["total_certifications"] == 1
    assert body["operators_with_certs"] == 1


# ---------------------------------------------------------------------------
# get_user_training: excludes company-B rows
# ---------------------------------------------------------------------------


def test_get_user_training_excludes_company_b_rows(client: TestClient, db_session: Session):
    admin_a = make_user(db_session, company_id=COMPANY_A)
    op_b = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_B)
    make_training(db_session, op_b, company_id=COMPANY_B)
    db_session.commit()

    resp = client.get(f"/api/v1/certifications/training/user/{op_b.id}", headers=headers_for(admin_a))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json() == []


# ---------------------------------------------------------------------------
# update_training (by-id): cross-tenant 404 BEFORE mutation
# ---------------------------------------------------------------------------


def test_update_training_cross_tenant_404_no_mutation(client: TestClient, db_session: Session):
    admin_a = make_user(db_session, company_id=COMPANY_A)
    op_b = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_B)
    record_b = make_training(db_session, op_b, company_id=COMPANY_B)
    original_name = record_b.training_name
    db_session.commit()

    resp = client.put(
        f"/api/v1/certifications/training/{record_b.id}",
        json={"training_name": "HACKED"},
        headers=headers_for(admin_a),
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text

    # The company-B row was NOT mutated by the company-A caller.
    db_session.expire_all()
    row = db_session.query(TrainingRecord).filter(TrainingRecord.id == record_b.id).first()
    assert row.training_name == original_name


def test_update_training_same_company_succeeds(client: TestClient, db_session: Session):
    admin_a = make_user(db_session, company_id=COMPANY_A)
    op_a = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_A)
    record_a = make_training(db_session, op_a, company_id=COMPANY_A)
    db_session.commit()

    resp = client.put(
        f"/api/v1/certifications/training/{record_a.id}",
        json={"training_name": "Updated Name"},
        headers=headers_for(admin_a),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["training_name"] == "Updated Name"


# ---------------------------------------------------------------------------
# update_skill_entry (by-id): cross-tenant 404 BEFORE mutation
# ---------------------------------------------------------------------------


def test_update_skill_entry_cross_tenant_404_no_mutation(client: TestClient, db_session: Session):
    admin_a = make_user(db_session, company_id=COMPANY_A)
    op_b = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_B)
    wc_b = make_work_center(db_session, company_id=COMPANY_B)
    entry_b = make_skill(db_session, op_b, wc_b, company_id=COMPANY_B)
    original_level = entry_b.skill_level
    db_session.commit()

    resp = client.put(
        f"/api/v1/certifications/skill-matrix/{entry_b.id}",
        json={"skill_level": 5},
        headers=headers_for(admin_a),
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text

    db_session.expire_all()
    row = db_session.query(SkillMatrix).filter(SkillMatrix.id == entry_b.id).first()
    assert row.skill_level == original_level


def test_update_skill_entry_same_company_succeeds(client: TestClient, db_session: Session):
    admin_a = make_user(db_session, company_id=COMPANY_A)
    op_a = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_A)
    wc_a = make_work_center(db_session, company_id=COMPANY_A)
    entry_a = make_skill(db_session, op_a, wc_a, company_id=COMPANY_A)
    db_session.commit()

    resp = client.put(
        f"/api/v1/certifications/skill-matrix/{entry_a.id}",
        json={"skill_level": 5},
        headers=headers_for(admin_a),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["skill_level"] == 5
