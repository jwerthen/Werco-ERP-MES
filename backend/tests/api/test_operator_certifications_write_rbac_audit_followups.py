"""Behavior locks for the operator-certification WRITE-path compliance fix
(fix/operator-cert-write-rbac-audit).

The 7 write endpoints in ``app/api/endpoints/operator_certifications.py`` gained three
compliance guarantees that this file locks down:

1. **RBAC.** Each write now sits behind ``require_role``:
   - Certification + Training writes (create/update/delete cert, create/update training)
     -> ``[ADMIN, MANAGER, QUALITY]``.
   - Skill-matrix writes (create/update entry) -> ``[ADMIN, MANAGER, SUPERVISOR]``.
   A privileged role succeeds; a non-privileged authenticated user gets 403 and writes nothing.

2. **Audit.** Each successful write emits exactly one tamper-evident ``audit_log`` row with the
   right ``resource_type`` (``operator_certification`` / ``training_record`` / ``skill_matrix``)
   and action (CREATE / UPDATE / DELETE). A 403'd call writes none.

3. **Create-time FK-in-company validation.** A supplied ``user_id`` / ``work_center_id`` that does
   not resolve in the caller's ACTIVE company -> 422 before insert (cross-tenant FK injection
   guard). ``update_training`` re-pointing ``work_center_id`` cross-company is likewise 422.

The READ endpoints are unchanged (any authenticated user, tenant-scoped) -- a light check
confirms an OPERATOR can still read.
"""

from datetime import date

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
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
        email=f"cert-rbac-{n}@co{company_id}.test",
        employee_id=f"CERTRBAC-{n:05d}",
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
        name=f"CERTRBAC-WC-{n}",
        code=f"CERTRBAC-WC-{n}",
        work_center_type="welding",
        description="cert rbac fixture work center",
        hourly_rate=100,
        is_active=True,
        company_id=company_id,
    )
    db.add(wc)
    db.commit()
    db.refresh(wc)
    return wc


def make_cert(db: Session, user: User, *, company_id: int = COMPANY_A) -> OperatorCertification:
    n = _next()
    cert = OperatorCertification(
        user_id=user.id,
        certification_type=CertificationType.WELDING,
        certification_name=f"Cert {n}",
        status=CertificationStatus.ACTIVE,
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


def _audit_rows(db: Session, resource_type: str, resource_id: int, company_id: int = COMPANY_A) -> list[AuditLog]:
    """All audit rows for a given resource (this test DB starts empty per function)."""
    db.expire_all()
    return (
        db.query(AuditLog)
        .filter(
            AuditLog.resource_type == resource_type,
            AuditLog.resource_id == resource_id,
            AuditLog.company_id == company_id,
        )
        .all()
    )


def _audit_count(db: Session, resource_type: str, company_id: int = COMPANY_A) -> int:
    db.expire_all()
    return db.query(AuditLog).filter(AuditLog.resource_type == resource_type, AuditLog.company_id == company_id).count()


# ===========================================================================
# 1+2. create_certification: RBAC + audit
# ===========================================================================


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY])
def test_create_certification_privileged_succeeds_and_audits(client: TestClient, db_session: Session, role):
    actor = make_user(db_session, role=role, company_id=COMPANY_A)
    op = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_A)
    db_session.commit()

    resp = client.post(
        "/api/v1/certifications/certifications/",
        json={"user_id": op.id, "certification_type": "welding", "certification_name": "W-1"},
        headers=headers_for(actor),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    cert_id = resp.json()["id"]

    rows = _audit_rows(db_session, "operator_certification", cert_id)
    assert len(rows) == 1
    assert rows[0].action == "CREATE"


def test_create_certification_operator_forbidden_no_audit(client: TestClient, db_session: Session):
    actor = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_A)
    op = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_A)
    db_session.commit()

    resp = client.post(
        "/api/v1/certifications/certifications/",
        json={"user_id": op.id, "certification_type": "welding", "certification_name": "W-1"},
        headers=headers_for(actor),
    )
    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text
    assert _audit_count(db_session, "operator_certification") == 0
    assert db_session.query(OperatorCertification).count() == 0


# ===========================================================================
# 1+2. update_certification: RBAC + audit
# ===========================================================================


def test_update_certification_privileged_succeeds_and_audits(client: TestClient, db_session: Session):
    actor = make_user(db_session, role=UserRole.QUALITY, company_id=COMPANY_A)
    op = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_A)
    cert = make_cert(db_session, op, company_id=COMPANY_A)
    db_session.commit()

    resp = client.put(
        f"/api/v1/certifications/certifications/{cert.id}",
        json={"certification_name": "Renamed"},
        headers=headers_for(actor),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["certification_name"] == "Renamed"

    rows = _audit_rows(db_session, "operator_certification", cert.id)
    assert len(rows) == 1
    assert rows[0].action == "UPDATE"


def test_update_certification_operator_forbidden_no_mutation_no_audit(client: TestClient, db_session: Session):
    actor = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_A)
    op = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_A)
    cert = make_cert(db_session, op, company_id=COMPANY_A)
    original_name = cert.certification_name
    db_session.commit()

    resp = client.put(
        f"/api/v1/certifications/certifications/{cert.id}",
        json={"certification_name": "HACKED"},
        headers=headers_for(actor),
    )
    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text
    assert _audit_count(db_session, "operator_certification") == 0
    db_session.expire_all()
    assert db_session.query(OperatorCertification).get(cert.id).certification_name == original_name


# ===========================================================================
# 1+2. delete_certification: RBAC + audit
# ===========================================================================


def test_delete_certification_privileged_succeeds_and_audits(client: TestClient, db_session: Session):
    actor = make_user(db_session, role=UserRole.MANAGER, company_id=COMPANY_A)
    op = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_A)
    cert = make_cert(db_session, op, company_id=COMPANY_A)
    cert_id = cert.id
    db_session.commit()

    resp = client.delete(f"/api/v1/certifications/certifications/{cert_id}", headers=headers_for(actor))
    assert resp.status_code == status.HTTP_200_OK, resp.text

    rows = _audit_rows(db_session, "operator_certification", cert_id)
    assert len(rows) == 1
    assert rows[0].action == "DELETE"
    db_session.expire_all()
    assert db_session.query(OperatorCertification).get(cert_id) is None


def test_delete_certification_operator_forbidden_row_survives_no_audit(client: TestClient, db_session: Session):
    actor = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_A)
    op = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_A)
    cert = make_cert(db_session, op, company_id=COMPANY_A)
    db_session.commit()

    resp = client.delete(f"/api/v1/certifications/certifications/{cert.id}", headers=headers_for(actor))
    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text
    assert _audit_count(db_session, "operator_certification") == 0
    db_session.expire_all()
    assert db_session.query(OperatorCertification).get(cert.id) is not None


# ===========================================================================
# 1+2. create_training: RBAC + audit
# ===========================================================================


def test_create_training_privileged_succeeds_and_audits(client: TestClient, db_session: Session):
    actor = make_user(db_session, role=UserRole.MANAGER, company_id=COMPANY_A)
    op = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_A)
    db_session.commit()

    resp = client.post(
        "/api/v1/certifications/training/",
        json={"user_id": op.id, "training_name": "Safety 101", "training_date": date.today().isoformat()},
        headers=headers_for(actor),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    record_id = resp.json()["id"]

    rows = _audit_rows(db_session, "training_record", record_id)
    assert len(rows) == 1
    assert rows[0].action == "CREATE"


def test_create_training_operator_forbidden_no_audit(client: TestClient, db_session: Session):
    actor = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_A)
    op = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_A)
    db_session.commit()

    resp = client.post(
        "/api/v1/certifications/training/",
        json={"user_id": op.id, "training_name": "Safety 101", "training_date": date.today().isoformat()},
        headers=headers_for(actor),
    )
    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text
    assert _audit_count(db_session, "training_record") == 0
    assert db_session.query(TrainingRecord).count() == 0


# ===========================================================================
# 1+2. update_training: RBAC + audit
# ===========================================================================


def test_update_training_privileged_succeeds_and_audits(client: TestClient, db_session: Session):
    actor = make_user(db_session, role=UserRole.QUALITY, company_id=COMPANY_A)
    op = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_A)
    record = make_training(db_session, op, company_id=COMPANY_A)
    db_session.commit()

    resp = client.put(
        f"/api/v1/certifications/training/{record.id}",
        json={"training_name": "Renamed Training"},
        headers=headers_for(actor),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    rows = _audit_rows(db_session, "training_record", record.id)
    assert len(rows) == 1
    assert rows[0].action == "UPDATE"


def test_update_training_operator_forbidden_no_mutation_no_audit(client: TestClient, db_session: Session):
    actor = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_A)
    op = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_A)
    record = make_training(db_session, op, company_id=COMPANY_A)
    original_name = record.training_name
    db_session.commit()

    resp = client.put(
        f"/api/v1/certifications/training/{record.id}",
        json={"training_name": "HACKED"},
        headers=headers_for(actor),
    )
    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text
    assert _audit_count(db_session, "training_record") == 0
    db_session.expire_all()
    assert db_session.query(TrainingRecord).get(record.id).training_name == original_name


# ===========================================================================
# 1+2. create_skill_entry: RBAC + audit  (SUPERVISOR is privileged here)
# ===========================================================================


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])
def test_create_skill_entry_privileged_succeeds_and_audits(client: TestClient, db_session: Session, role):
    actor = make_user(db_session, role=role, company_id=COMPANY_A)
    op = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_A)
    wc = make_work_center(db_session, company_id=COMPANY_A)
    db_session.commit()

    resp = client.post(
        "/api/v1/certifications/skill-matrix/",
        json={"user_id": op.id, "work_center_id": wc.id, "skill_level": 3},
        headers=headers_for(actor),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    entry_id = resp.json()["id"]

    rows = _audit_rows(db_session, "skill_matrix", entry_id)
    assert len(rows) == 1
    assert rows[0].action == "CREATE"


@pytest.mark.parametrize("role", [UserRole.OPERATOR, UserRole.QUALITY])
def test_create_skill_entry_non_privileged_forbidden_no_audit(client: TestClient, db_session: Session, role):
    """QUALITY can write certs/training but NOT the skill matrix (supervisor-and-above)."""
    actor = make_user(db_session, role=role, company_id=COMPANY_A)
    op = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_A)
    wc = make_work_center(db_session, company_id=COMPANY_A)
    db_session.commit()

    resp = client.post(
        "/api/v1/certifications/skill-matrix/",
        json={"user_id": op.id, "work_center_id": wc.id, "skill_level": 3},
        headers=headers_for(actor),
    )
    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text
    assert _audit_count(db_session, "skill_matrix") == 0
    assert db_session.query(SkillMatrix).count() == 0


# ===========================================================================
# 1+2. update_skill_entry: RBAC + audit
# ===========================================================================


def test_update_skill_entry_privileged_succeeds_and_audits(client: TestClient, db_session: Session):
    actor = make_user(db_session, role=UserRole.SUPERVISOR, company_id=COMPANY_A)
    op = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_A)
    wc = make_work_center(db_session, company_id=COMPANY_A)
    entry = make_skill(db_session, op, wc, company_id=COMPANY_A)
    db_session.commit()

    resp = client.put(
        f"/api/v1/certifications/skill-matrix/{entry.id}",
        json={"skill_level": 5},
        headers=headers_for(actor),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["skill_level"] == 5

    rows = _audit_rows(db_session, "skill_matrix", entry.id)
    assert len(rows) == 1
    assert rows[0].action == "UPDATE"


def test_update_skill_entry_quality_forbidden_no_mutation_no_audit(client: TestClient, db_session: Session):
    actor = make_user(db_session, role=UserRole.QUALITY, company_id=COMPANY_A)
    op = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_A)
    wc = make_work_center(db_session, company_id=COMPANY_A)
    entry = make_skill(db_session, op, wc, company_id=COMPANY_A)
    original_level = entry.skill_level
    db_session.commit()

    resp = client.put(
        f"/api/v1/certifications/skill-matrix/{entry.id}",
        json={"skill_level": 5},
        headers=headers_for(actor),
    )
    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text
    assert _audit_count(db_session, "skill_matrix") == 0
    db_session.expire_all()
    assert db_session.query(SkillMatrix).get(entry.id).skill_level == original_level


# ===========================================================================
# 3. FK-in-company validation -> 422 before insert
# ===========================================================================


def test_create_certification_cross_company_user_422(client: TestClient, db_session: Session):
    admin_a = make_user(db_session, role=UserRole.ADMIN, company_id=COMPANY_A)
    op_b = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_B)
    db_session.commit()

    resp = client.post(
        "/api/v1/certifications/certifications/",
        json={"user_id": op_b.id, "certification_type": "welding", "certification_name": "W-1"},
        headers=headers_for(admin_a),
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, resp.text
    assert db_session.query(OperatorCertification).count() == 0
    assert _audit_count(db_session, "operator_certification") == 0


def test_create_certification_same_company_user_succeeds(client: TestClient, db_session: Session):
    admin_a = make_user(db_session, role=UserRole.ADMIN, company_id=COMPANY_A)
    op_a = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_A)
    db_session.commit()

    resp = client.post(
        "/api/v1/certifications/certifications/",
        json={"user_id": op_a.id, "certification_type": "welding", "certification_name": "W-1"},
        headers=headers_for(admin_a),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["user_id"] == op_a.id


def test_create_training_cross_company_work_center_422(client: TestClient, db_session: Session):
    admin_a = make_user(db_session, role=UserRole.ADMIN, company_id=COMPANY_A)
    op_a = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_A)
    wc_b = make_work_center(db_session, company_id=COMPANY_B)
    db_session.commit()

    resp = client.post(
        "/api/v1/certifications/training/",
        json={
            "user_id": op_a.id,
            "training_name": "Safety 101",
            "training_date": date.today().isoformat(),
            "work_center_id": wc_b.id,
        },
        headers=headers_for(admin_a),
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, resp.text
    assert db_session.query(TrainingRecord).count() == 0


def test_update_training_repoint_cross_company_work_center_422(client: TestClient, db_session: Session):
    admin_a = make_user(db_session, role=UserRole.ADMIN, company_id=COMPANY_A)
    op_a = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_A)
    record = make_training(db_session, op_a, company_id=COMPANY_A)
    wc_b = make_work_center(db_session, company_id=COMPANY_B)
    db_session.commit()

    resp = client.put(
        f"/api/v1/certifications/training/{record.id}",
        json={"work_center_id": wc_b.id},
        headers=headers_for(admin_a),
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, resp.text
    db_session.expire_all()
    assert db_session.query(TrainingRecord).get(record.id).work_center_id is None


def test_create_skill_entry_cross_company_work_center_422(client: TestClient, db_session: Session):
    admin_a = make_user(db_session, role=UserRole.ADMIN, company_id=COMPANY_A)
    op_a = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_A)
    wc_b = make_work_center(db_session, company_id=COMPANY_B)
    db_session.commit()

    resp = client.post(
        "/api/v1/certifications/skill-matrix/",
        json={"user_id": op_a.id, "work_center_id": wc_b.id, "skill_level": 3},
        headers=headers_for(admin_a),
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, resp.text
    assert db_session.query(SkillMatrix).count() == 0


def test_create_skill_entry_cross_company_user_422(client: TestClient, db_session: Session):
    admin_a = make_user(db_session, role=UserRole.ADMIN, company_id=COMPANY_A)
    op_b = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_B)
    wc_a = make_work_center(db_session, company_id=COMPANY_A)
    db_session.commit()

    resp = client.post(
        "/api/v1/certifications/skill-matrix/",
        json={"user_id": op_b.id, "work_center_id": wc_a.id, "skill_level": 3},
        headers=headers_for(admin_a),
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, resp.text
    assert db_session.query(SkillMatrix).count() == 0


def test_create_skill_entry_same_company_fks_succeed(client: TestClient, db_session: Session):
    admin_a = make_user(db_session, role=UserRole.ADMIN, company_id=COMPANY_A)
    op_a = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_A)
    wc_a = make_work_center(db_session, company_id=COMPANY_A)
    db_session.commit()

    resp = client.post(
        "/api/v1/certifications/skill-matrix/",
        json={"user_id": op_a.id, "work_center_id": wc_a.id, "skill_level": 3},
        headers=headers_for(admin_a),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["user_id"] == op_a.id and resp.json()["work_center_id"] == wc_a.id


# ===========================================================================
# 4. READ endpoints are unaffected -- an OPERATOR can still read (tenant-scoped)
# ===========================================================================


def test_read_endpoints_unaffected_for_operator(client: TestClient, db_session: Session):
    operator = make_user(db_session, role=UserRole.OPERATOR, company_id=COMPANY_A)
    db_session.commit()

    # A spread of READ endpoints across all three resource families -- all 200 for a plain operator.
    for path in (
        "/api/v1/certifications/certifications/",
        "/api/v1/certifications/certifications/dashboard",
        "/api/v1/certifications/certifications/expiring?days=30",
        "/api/v1/certifications/training/",
        "/api/v1/certifications/skill-matrix/",
    ):
        resp = client.get(path, headers=headers_for(operator))
        assert resp.status_code == status.HTTP_200_OK, f"{path} -> {resp.status_code}: {resp.text}"
