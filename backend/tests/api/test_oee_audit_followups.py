"""Behavior locks for the now-audited OEE write endpoints
(fix/wo-followups-round2, FIX 2).

Invariant #2 (every state change is recorded in the tamper-evident, hash-chained ``audit_log``
via ``AuditService``) was missing on the OEE write paths. These endpoints now write exactly one
audit row, committed ATOMICALLY with the record/target mutation:
  - ``POST   /oee/records``           -> oee_record  CREATE
  - ``PUT    /oee/records/{id}``      -> oee_record  UPDATE
  - ``DELETE /oee/records/{id}``      -> oee_record  DELETE
  - ``POST   /oee/calculate/{wc}``    -> oee_record  CREATE (fresh) / UPDATE (recompute)
  - ``POST   /oee/targets``           -> oee_target  CREATE (new) / UPDATE (existing)
  - ``PUT    /oee/targets/{id}``      -> oee_target  UPDATE
  - ``DELETE /oee/targets/{id}``      -> oee_target  DELETE

Each test asserts: the right action verb + resource_type, exactly one row per call, the row
commits with the data change (atomicity), and NO audit row is written when an unauthorized
role is rejected with 403 (the write never happened, so nothing to audit).
"""

from datetime import date

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.oee import OEERecord, OEETarget
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


def make_user(db: Session, *, role: UserRole = UserRole.MANAGER, company_id: int = COMPANY_A) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"oee-fu-{n}@co{company_id}.test",
        employee_id=f"OEEFU-{n:05d}",
        first_name="Oee",
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
        name=f"OEEFU-WC-{n}",
        code=f"OEEFU-WC-{n}",
        work_center_type="welding",
        description="oee fixture work center",
        hourly_rate=100,
        is_active=True,
        company_id=company_id,
    )
    db.add(wc)
    db.commit()
    db.refresh(wc)
    return wc


def _audit_rows(db: Session, *, resource_type: str, resource_id, action=None, company_id: int = COMPANY_A):
    q = db.query(AuditLog).filter(
        AuditLog.company_id == company_id,
        AuditLog.resource_type == resource_type,
        AuditLog.resource_id == resource_id,
    )
    if action is not None:
        q = q.filter(AuditLog.action == action)
    return q.all()


def _record_payload(wc_id: int) -> dict:
    return {
        "work_center_id": wc_id,
        "record_date": date.today().isoformat(),
        "planned_production_time_minutes": 480.0,
        "actual_run_time_minutes": 400.0,
        "total_parts_produced": 100,
        "ideal_cycle_time_seconds": 60.0,
        "actual_operating_time_minutes": 400.0,
        "good_parts": 95,
        "total_parts": 100,
    }


# ---------------------------------------------------------------------------
# create_oee_record: one CREATE audit row, committed atomically
# ---------------------------------------------------------------------------


def test_create_oee_record_writes_one_create_audit(client: TestClient, db_session: Session):
    mgr = make_user(db_session, role=UserRole.MANAGER)
    wc = make_work_center(db_session)
    db_session.commit()

    resp = client.post("/api/v1/oee/records", json=_record_payload(wc.id), headers=headers_for(mgr))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    record_id = resp.json()["id"]

    db_session.expire_all()
    # The OEE record committed.
    assert db_session.query(OEERecord).filter(OEERecord.id == record_id).first() is not None
    # Exactly one CREATE audit row for it.
    rows = _audit_rows(db_session, resource_type="oee_record", resource_id=record_id, action="CREATE")
    assert len(rows) == 1
    assert rows[0].resource_type == "oee_record"


def test_update_oee_record_writes_one_update_audit(client: TestClient, db_session: Session):
    mgr = make_user(db_session, role=UserRole.MANAGER)
    wc = make_work_center(db_session)
    db_session.commit()

    created = client.post("/api/v1/oee/records", json=_record_payload(wc.id), headers=headers_for(mgr))
    record_id = created.json()["id"]

    resp = client.put(
        f"/api/v1/oee/records/{record_id}",
        json={"good_parts": 80, "total_parts": 100},
        headers=headers_for(mgr),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    rows = _audit_rows(db_session, resource_type="oee_record", resource_id=record_id, action="UPDATE")
    assert len(rows) == 1
    # The mutation committed alongside the audit row.
    assert db_session.query(OEERecord).filter(OEERecord.id == record_id).first().good_parts == 80


def test_delete_oee_record_writes_one_delete_audit(client: TestClient, db_session: Session):
    mgr = make_user(db_session, role=UserRole.MANAGER)
    wc = make_work_center(db_session)
    db_session.commit()

    created = client.post("/api/v1/oee/records", json=_record_payload(wc.id), headers=headers_for(mgr))
    record_id = created.json()["id"]

    resp = client.delete(f"/api/v1/oee/records/{record_id}", headers=headers_for(mgr))
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    # The record was deleted, and exactly one DELETE audit row remains.
    assert db_session.query(OEERecord).filter(OEERecord.id == record_id).first() is None
    rows = _audit_rows(db_session, resource_type="oee_record", resource_id=record_id, action="DELETE")
    assert len(rows) == 1


def test_create_oee_record_forbidden_for_operator_writes_no_audit(client: TestClient, db_session: Session):
    operator = make_user(db_session, role=UserRole.OPERATOR)
    wc = make_work_center(db_session)
    db_session.commit()

    resp = client.post("/api/v1/oee/records", json=_record_payload(wc.id), headers=headers_for(operator))
    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text

    db_session.expire_all()
    # The write was rejected -> no OEE record, no audit row.
    assert db_session.query(OEERecord).count() == 0
    assert db_session.query(AuditLog).filter(AuditLog.resource_type == "oee_record").count() == 0


# ---------------------------------------------------------------------------
# auto_calculate_oee: one audit row (CREATE for the fresh record)
# ---------------------------------------------------------------------------


def test_auto_calculate_oee_writes_one_audit(client: TestClient, db_session: Session):
    mgr = make_user(db_session, role=UserRole.MANAGER)
    wc = make_work_center(db_session)
    db_session.commit()

    resp = client.post(
        f"/api/v1/oee/calculate/{wc.id}?record_date={date.today().isoformat()}",
        headers=headers_for(mgr),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    record_id = resp.json()["id"]

    db_session.expire_all()
    # A single representative audit row was written for the (empty-day) computed record.
    rows = _audit_rows(db_session, resource_type="oee_record", resource_id=record_id)
    assert len(rows) == 1
    assert rows[0].action in ("CREATE", "UPDATE")


# ---------------------------------------------------------------------------
# OEE targets: create / update / delete each write one oee_target audit row
# ---------------------------------------------------------------------------


def test_create_oee_target_writes_one_create_audit(client: TestClient, db_session: Session):
    mgr = make_user(db_session, role=UserRole.MANAGER)
    wc = make_work_center(db_session)
    db_session.commit()

    resp = client.post(
        "/api/v1/oee/targets",
        json={"work_center_id": wc.id, "target_oee_pct": 88.0},
        headers=headers_for(mgr),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    target_id = resp.json()["id"]

    db_session.expire_all()
    assert db_session.query(OEETarget).filter(OEETarget.id == target_id).first() is not None
    rows = _audit_rows(db_session, resource_type="oee_target", resource_id=target_id, action="CREATE")
    assert len(rows) == 1


def test_update_oee_target_writes_one_update_audit(client: TestClient, db_session: Session):
    mgr = make_user(db_session, role=UserRole.MANAGER)
    wc = make_work_center(db_session)
    db_session.commit()

    created = client.post(
        "/api/v1/oee/targets",
        json={"work_center_id": wc.id, "target_oee_pct": 88.0},
        headers=headers_for(mgr),
    )
    target_id = created.json()["id"]

    resp = client.put(
        f"/api/v1/oee/targets/{target_id}",
        json={"target_oee_pct": 92.0},
        headers=headers_for(mgr),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    rows = _audit_rows(db_session, resource_type="oee_target", resource_id=target_id, action="UPDATE")
    assert len(rows) == 1
    assert db_session.query(OEETarget).filter(OEETarget.id == target_id).first().target_oee_pct == 92.0


def test_delete_oee_target_writes_one_delete_audit(client: TestClient, db_session: Session):
    mgr = make_user(db_session, role=UserRole.MANAGER)
    wc = make_work_center(db_session)
    db_session.commit()

    created = client.post(
        "/api/v1/oee/targets",
        json={"work_center_id": wc.id, "target_oee_pct": 88.0},
        headers=headers_for(mgr),
    )
    target_id = created.json()["id"]

    resp = client.delete(f"/api/v1/oee/targets/{target_id}", headers=headers_for(mgr))
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    assert db_session.query(OEETarget).filter(OEETarget.id == target_id).first() is None
    rows = _audit_rows(db_session, resource_type="oee_target", resource_id=target_id, action="DELETE")
    assert len(rows) == 1


def test_create_oee_target_forbidden_for_operator_writes_no_audit(client: TestClient, db_session: Session):
    operator = make_user(db_session, role=UserRole.OPERATOR)
    wc = make_work_center(db_session)
    db_session.commit()

    resp = client.post(
        "/api/v1/oee/targets",
        json={"work_center_id": wc.id, "target_oee_pct": 88.0},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text

    db_session.expire_all()
    assert db_session.query(OEETarget).count() == 0
    assert db_session.query(AuditLog).filter(AuditLog.resource_type == "oee_target").count() == 0


# ---------------------------------------------------------------------------
# tenant scope sanity: a cross-tenant OEE record id 404s and writes no audit
# ---------------------------------------------------------------------------


def test_update_oee_record_cross_tenant_404_no_audit(client: TestClient, db_session: Session):
    mgr_a = make_user(db_session, role=UserRole.MANAGER, company_id=COMPANY_A)
    wc_b = make_work_center(db_session, company_id=COMPANY_B)
    record_b = OEERecord(
        work_center_id=wc_b.id,
        record_date=date.today(),
        good_parts=10,
        total_parts=10,
        company_id=COMPANY_B,
    )
    db_session.add(record_b)
    db_session.commit()
    db_session.refresh(record_b)

    resp = client.put(
        f"/api/v1/oee/records/{record_b.id}",
        json={"good_parts": 1},
        headers=headers_for(mgr_a),
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text

    db_session.expire_all()
    assert _audit_rows(db_session, resource_type="oee_record", resource_id=record_b.id, company_id=COMPANY_B) == []
    assert _audit_rows(db_session, resource_type="oee_record", resource_id=record_b.id, company_id=COMPANY_A) == []
