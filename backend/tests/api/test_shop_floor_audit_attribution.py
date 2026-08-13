"""Shop-floor audit rows carry request attribution (finding B8).

``report_operation_production`` and its hold / resume / inspect siblings constructed
``AuditService(db, current_user)`` locally -- WITHOUT the request -- so their
tamper-evident chain rows landed with NULL ``ip_address`` / ``user_agent`` while every
other write path recorded both. All four now take the request-scoped
``get_audit_service`` dependency (the clock_out pattern), and deliberately keep ONE
audit identity per handler (the clock_out shadow lesson: two AuditService instances in
one handler fork the chain context).

Under TestClient the request context is ``client.host == "testclient"`` and a
``user-agent: testclient`` header, so a non-null assertion here is exactly the
"the request reached the service" proof.
"""

from datetime import date, datetime, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.part import Part
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
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


def make_user(db: Session, *, role: UserRole = UserRole.ADMIN) -> User:
    _ensure_company(db, COMPANY_A)
    n = _next()
    user = User(
        email=f"sfaudit-{n}@co{COMPANY_A}.test",
        employee_id=f"SFA-{n:05d}",
        first_name="Audit",
        last_name="Attribution",
        hashed_password=TEST_PASSWORD_HASH,
        role=role,
        is_active=True,
        is_superuser=False,
        company_id=COMPANY_A,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def headers_for(user: User) -> dict:
    token = create_access_token(subject=user.id, company_id=user.company_id)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def make_wo_with_op(db: Session, *, op_status: OperationStatus) -> tuple[WorkOrder, WorkOrderOperation]:
    n = _next()
    part = Part(
        part_number=f"SFA-P-{n}",
        name=f"Part {n}",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=COMPANY_A,
    )
    db.add(part)
    db.flush()
    wc = WorkCenter(
        name=f"SFA-WC-{n}",
        code=f"SFA-WC-{n}",
        work_center_type="welding",
        hourly_rate=100,
        is_active=True,
        company_id=COMPANY_A,
    )
    db.add(wc)
    db.flush()
    wo = WorkOrder(
        work_order_number=f"SFA-WO-{n:05d}",
        part_id=part.id,
        quantity_ordered=10,
        status=WorkOrderStatus.IN_PROGRESS,
        priority=5,
        due_date=date.today() + timedelta(days=30),
        company_id=COMPANY_A,
    )
    db.add(wo)
    db.flush()
    op = WorkOrderOperation(
        work_order_id=wo.id,
        work_center_id=wc.id,
        sequence=10,
        operation_number="OP10",
        name="Audit op",
        status=op_status,
        quantity_complete=0,
        quantity_scrapped=0,
        company_id=COMPANY_A,
    )
    db.add(op)
    db.commit()
    db.refresh(wo)
    db.refresh(op)
    return wo, op


def clock_in(db: Session, user: User, op: WorkOrderOperation) -> TimeEntry:
    entry = TimeEntry(
        user_id=user.id,
        work_order_id=op.work_order_id,
        operation_id=op.id,
        clock_in=datetime.utcnow() - timedelta(hours=1),
        clock_out=None,
        entry_type=TimeEntryType.RUN,
        company_id=COMPANY_A,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def audit_row(db: Session, action: str) -> AuditLog:
    db.expire_all()
    rows = db.query(AuditLog).filter(AuditLog.action == action).order_by(AuditLog.sequence_number).all()
    assert rows, f"expected an audit row for {action}"
    return rows[-1]


def assert_attributed(row: AuditLog) -> None:
    assert row.ip_address, "the chain row must carry the caller's ip"
    assert row.user_agent, "the chain row must carry the caller's user agent"


def test_report_production_audit_row_carries_ip_and_user_agent(client: TestClient, db_session: Session):
    user = make_user(db_session)
    _wo, op = make_wo_with_op(db_session, op_status=OperationStatus.IN_PROGRESS)
    clock_in(db_session, user, op)

    resp = client.post(
        f"/api/v1/shop-floor/operations/{op.id}/production",
        headers=headers_for(user),
        json={"quantity_complete_delta": 2, "quantity_scrapped_delta": 0},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert_attributed(audit_row(db_session, "REPORT_OPERATION_PRODUCTION"))


def test_hold_audit_row_carries_ip_and_user_agent(client: TestClient, db_session: Session):
    user = make_user(db_session)
    _wo, op = make_wo_with_op(db_session, op_status=OperationStatus.IN_PROGRESS)

    resp = client.put(f"/api/v1/shop-floor/operations/{op.id}/hold", headers=headers_for(user))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert_attributed(audit_row(db_session, "HOLD_OPERATION"))


def test_resume_audit_row_carries_ip_and_user_agent(client: TestClient, db_session: Session):
    user = make_user(db_session)
    _wo, op = make_wo_with_op(db_session, op_status=OperationStatus.ON_HOLD)

    resp = client.put(f"/api/v1/shop-floor/operations/{op.id}/resume", headers=headers_for(user))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    # Resume audits as a STATUS_CHANGE (carrying before -> after), not the old
    # bespoke RESUME_OPERATION action; the verb lives in extra_data.transition.
    row = audit_row(db_session, "STATUS_CHANGE")
    assert row.resource_type == "work_order_operation"
    assert row.extra_data["transition"] == "resume_operation"
    assert_attributed(row)


def test_inspection_audit_row_carries_ip_and_user_agent(client: TestClient, db_session: Session):
    user = make_user(db_session, role=UserRole.QUALITY)
    _wo, op = make_wo_with_op(db_session, op_status=OperationStatus.IN_PROGRESS)

    resp = client.post(
        f"/api/v1/shop-floor/operations/{op.id}/inspection",
        headers=headers_for(user),
        json={"inspection_type": "in_process", "notes": "looks good"},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert_attributed(audit_row(db_session, "MARK_OPERATION_INSPECTED"))
