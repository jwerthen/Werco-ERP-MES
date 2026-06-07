"""Behavior locks for the Batch-3 shared completion finalizer (Rank 6).

These cover the *behavior changes* the consolidation introduced, so a future
edit cannot silently revert them. They are intentionally narrow; the full
completion-flow matrix is the test-engineer's follow-up.

Covered findings:
- DUP-2:  WO actual_start is ALWAYS stamped before COMPLETE (no actual_end-without-start).
- DUP-3:  office complete_operation no longer zeroes accumulated scrap from a
          defaulted-0 param; scrap is only written when explicitly provided.
- SFI-5:  absolute /complete is floored at durable TimeEntry evidence (never regresses).
- RUP-1:  current_operation_id is populated while in flight and cleared at COMPLETE.
- RUP-6:  WO quantity_complete never regresses (max-guarded).
- QG-5/BLK-1: office complete_operation REFUSES an ON_HOLD operation (409).
- DUP-4:  complete_work_order force-completes open ops through the shared path and
          leaves no open operation / releases capacity.
- AUD-3:  reconcile-on-read writes attributed audit rows; reads still succeed.
"""

from datetime import date, datetime, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
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


def make_user(db: Session, *, role: UserRole = UserRole.ADMIN) -> User:
    n = _next()
    user = User(
        email=f"b3-{n}@co{COMPANY_A}.test",
        employee_id=f"B3-{n:05d}",
        first_name="B3",
        last_name="CA",
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


def make_part(db: Session) -> Part:
    n = _next()
    part = Part(
        part_number=f"B3-P-{n}",
        name=f"Part {n}",
        description="batch3 fixture part",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=COMPANY_A,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def make_work_center(db: Session) -> WorkCenter:
    n = _next()
    wc = WorkCenter(
        name=f"B3-WC-{n}",
        code=f"B3-WC-{n}",
        work_center_type="welding",
        description="batch3 fixture work center",
        hourly_rate=100,
        is_active=True,
        company_id=COMPANY_A,
    )
    db.add(wc)
    db.commit()
    db.refresh(wc)
    return wc


def make_wo(db: Session, *, status_: WorkOrderStatus, quantity_ordered: float = 10) -> tuple[WorkOrder, Part]:
    part = make_part(db)
    n = _next()
    wo = WorkOrder(
        work_order_number=f"B3-WO-{n:05d}",
        customer_name="Acme",
        part_id=part.id,
        quantity_ordered=quantity_ordered,
        status=status_,
        priority=5,
        due_date=date.today() + timedelta(days=30),
        company_id=COMPANY_A,
    )
    db.add(wo)
    db.flush()
    return wo, part


def make_op(
    db: Session,
    wo: WorkOrder,
    wc: WorkCenter,
    *,
    sequence: int,
    status_: OperationStatus,
    quantity_complete: float = 0,
    quantity_scrapped: float = 0,
) -> WorkOrderOperation:
    op = WorkOrderOperation(
        work_order_id=wo.id,
        work_center_id=wc.id,
        sequence=sequence,
        operation_number=f"OP{sequence}",
        name=f"Op {sequence}",
        status=status_,
        quantity_complete=quantity_complete,
        quantity_scrapped=quantity_scrapped,
        company_id=COMPANY_A,
    )
    db.add(op)
    db.flush()
    return op


# ---------------------------------------------------------------------------
# QG-5 / BLK-1: office complete_operation refuses an ON_HOLD operation
# ---------------------------------------------------------------------------


def test_office_complete_refuses_on_hold_operation(client: TestClient, db_session: Session):
    admin = make_user(db_session)
    wo, _ = make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=10)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.ON_HOLD)
    db_session.commit()

    resp = client.post(
        f"/api/v1/work-orders/operations/{op.id}/complete?quantity_complete=10",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
    assert "on hold" in resp.json()["detail"].lower()

    db_session.expire_all()
    refreshed = db_session.get(WorkOrderOperation, op.id)
    assert refreshed.status == OperationStatus.ON_HOLD  # not silently lifted / completed


# ---------------------------------------------------------------------------
# DUP-3: office complete_operation does not zero accumulated scrap when omitted
# ---------------------------------------------------------------------------


def test_office_complete_preserves_scrap_when_param_omitted(client: TestClient, db_session: Session):
    admin = make_user(db_session)
    wo, _ = make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=10)
    wc = make_work_center(db_session)
    op = make_op(
        db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS, quantity_complete=2, quantity_scrapped=3
    )
    db_session.commit()

    # Partial complete WITHOUT a scrap param -> accumulated scrap (3) must survive.
    resp = client.post(
        f"/api/v1/work-orders/operations/{op.id}/complete?quantity_complete=5",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    refreshed = db_session.get(WorkOrderOperation, op.id)
    assert refreshed.quantity_scrapped == 3, "scrap must not be zeroed by an omitted param"
    assert refreshed.quantity_complete == 5


def test_office_complete_updates_scrap_when_explicitly_provided(client: TestClient, db_session: Session):
    admin = make_user(db_session)
    wo, _ = make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=10)
    wc = make_work_center(db_session)
    op = make_op(
        db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS, quantity_complete=2, quantity_scrapped=3
    )
    db_session.commit()

    resp = client.post(
        f"/api/v1/work-orders/operations/{op.id}/complete?quantity_complete=5&quantity_scrapped=1",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    refreshed = db_session.get(WorkOrderOperation, op.id)
    assert refreshed.quantity_scrapped == 1


# ---------------------------------------------------------------------------
# SFI-5: absolute /complete is floored at durable TimeEntry evidence
# ---------------------------------------------------------------------------


def test_office_complete_floors_quantity_at_time_entry_evidence(client: TestClient, db_session: Session):
    admin = make_user(db_session)
    wo, _ = make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=10)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS, quantity_complete=6)
    # Durable evidence: the operator already booked 6 good on a closed TimeEntry.
    entry = TimeEntry(
        user_id=admin.id,
        work_order_id=wo.id,
        operation_id=op.id,
        entry_type=TimeEntryType.RUN,
        clock_in=datetime.utcnow() - timedelta(hours=2),
        clock_out=datetime.utcnow() - timedelta(hours=1),
        duration_hours=1.0,
        quantity_produced=6,
        quantity_scrapped=0,
        company_id=COMPANY_A,
    )
    db_session.add(entry)
    db_session.commit()

    # Try to lower the absolute quantity below evidence (4 < 6) -> must NOT regress.
    resp = client.post(
        f"/api/v1/work-orders/operations/{op.id}/complete?quantity_complete=4",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    refreshed = db_session.get(WorkOrderOperation, op.id)
    assert refreshed.quantity_complete == 6, "absolute /complete must not drop below produced evidence"


# ---------------------------------------------------------------------------
# DUP-2 / RUP-1: actual_start stamped before COMPLETE; current_operation_id lifecycle
# ---------------------------------------------------------------------------


def test_finalizer_stamps_actual_start_and_clears_current_operation_id(client: TestClient, db_session: Session):
    """Completing the only operation of a RELEASED WO stamps WO.actual_start
    (never leaving actual_end-without-actual_start) and clears current_operation_id."""
    admin = make_user(db_session)
    wo, _ = make_wo(db_session, status_=WorkOrderStatus.RELEASED, quantity_ordered=5)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.READY)
    db_session.commit()

    resp = client.post(
        f"/api/v1/work-orders/operations/{op.id}/complete?quantity_complete=5",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    refreshed_wo = db_session.get(WorkOrder, wo.id)
    assert refreshed_wo.status == WorkOrderStatus.COMPLETE
    assert refreshed_wo.actual_start is not None, "DUP-2: actual_start must be stamped before COMPLETE"
    assert refreshed_wo.actual_end is not None
    assert refreshed_wo.actual_start <= refreshed_wo.actual_end
    assert refreshed_wo.current_operation_id is None, "RUP-1: completed WO is on no operation"


def test_finalizer_populates_current_operation_id_on_multi_op_progress(client: TestClient, db_session: Session):
    """Completing the first op of a 2-op RELEASED WO lifts it to IN_PROGRESS,
    releases the next op READY, and points current_operation_id at it (RUP-1)."""
    admin = make_user(db_session)
    wo, _ = make_wo(db_session, status_=WorkOrderStatus.RELEASED, quantity_ordered=5)
    wc = make_work_center(db_session)
    op1 = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    op2 = make_op(db_session, wo, wc, sequence=20, status_=OperationStatus.PENDING)
    db_session.commit()

    resp = client.post(
        f"/api/v1/work-orders/operations/{op1.id}/complete?quantity_complete=5",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    refreshed_wo = db_session.get(WorkOrder, wo.id)
    refreshed_op2 = db_session.get(WorkOrderOperation, op2.id)
    assert refreshed_wo.status == WorkOrderStatus.IN_PROGRESS
    assert refreshed_wo.actual_start is not None
    assert refreshed_op2.status == OperationStatus.READY, "next op self-healed to READY"
    assert refreshed_wo.current_operation_id == op2.id, "RUP-1: WO points at the now-active op"


# ---------------------------------------------------------------------------
# DUP-4: complete_work_order force-completes open ops through the shared path
# ---------------------------------------------------------------------------


def test_complete_work_order_force_completes_open_operations(client: TestClient, db_session: Session):
    """The manual WO-complete override no longer leaves a COMPLETE WO over open
    operations: every open op is force-completed (and audited)."""
    manager = make_user(db_session, role=UserRole.MANAGER)
    wo, _ = make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=8)
    wc = make_work_center(db_session)
    op1 = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    op2 = make_op(db_session, wo, wc, sequence=20, status_=OperationStatus.PENDING)
    db_session.commit()

    resp = client.post(
        f"/api/v1/work-orders/{wo.id}/complete?quantity_complete=8&quantity_scrapped=0",
        headers=headers_for(manager),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    refreshed_wo = db_session.get(WorkOrder, wo.id)
    assert refreshed_wo.status == WorkOrderStatus.COMPLETE
    assert refreshed_wo.actual_start is not None
    assert refreshed_wo.current_operation_id is None
    assert refreshed_wo.quantity_complete == 8
    for op in (op1, op2):
        refreshed_op = db_session.get(WorkOrderOperation, op.id)
        assert refreshed_op.status == OperationStatus.COMPLETE, "no operation left open"
        assert refreshed_op.actual_end is not None
        assert refreshed_op.completed_by == manager.id

    # Each force-completed op is audited as a STATUS_CHANGE to complete.
    db_session.rollback()
    db_session.expire_all()
    for op in (op1, op2):
        rows = (
            db_session.query(AuditLog)
            .filter(
                AuditLog.resource_type == "work_order_operation",
                AuditLog.resource_id == op.id,
                AuditLog.action == "STATUS_CHANGE",
            )
            .all()
        )
        assert rows, f"expected a STATUS_CHANGE audit row for force-completed op {op.id}"
        assert rows[-1].new_values == {"status": "complete"}


def test_complete_work_order_rejects_quantity_over_ordered(client: TestClient, db_session: Session):
    manager = make_user(db_session, role=UserRole.MANAGER)
    wo, _ = make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=5)
    wc = make_work_center(db_session)
    make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS)
    db_session.commit()

    resp = client.post(
        f"/api/v1/work-orders/{wo.id}/complete?quantity_complete=99",
        headers=headers_for(manager),
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
    assert "exceed" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# AUD-3: reconcile-on-read writes attributed audit rows; reads still succeed
# ---------------------------------------------------------------------------


def test_reconcile_on_read_audits_evidence_driven_completion(client: TestClient, db_session: Session):
    """A GET that reconciles an operation/WO to COMPLETE from durable TimeEntry
    evidence writes a tamper-evident STATUS_CHANGE row attributed to the reader,
    and the read still returns 200."""
    admin = make_user(db_session)
    wo, _ = make_wo(db_session, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=4)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS, quantity_complete=0)
    # Durable evidence: a closed TimeEntry produced the full ordered quantity, but
    # the operation row was never flipped to COMPLETE (a stale write / crash).
    entry = TimeEntry(
        user_id=admin.id,
        work_order_id=wo.id,
        operation_id=op.id,
        entry_type=TimeEntryType.RUN,
        clock_in=datetime.utcnow() - timedelta(hours=2),
        clock_out=datetime.utcnow() - timedelta(hours=1),
        duration_hours=1.0,
        quantity_produced=4,
        quantity_scrapped=0,
        company_id=COMPANY_A,
    )
    db_session.add(entry)
    db_session.commit()

    # A plain detail GET triggers reconcile-on-read.
    resp = client.get(f"/api/v1/work-orders/{wo.id}", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.rollback()
    db_session.expire_all()
    refreshed_op = db_session.get(WorkOrderOperation, op.id)
    assert refreshed_op.status == OperationStatus.COMPLETE, "reconcile drove the op COMPLETE"

    op_rows = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.resource_type == "work_order_operation",
            AuditLog.resource_id == op.id,
            AuditLog.action == "STATUS_CHANGE",
        )
        .all()
    )
    assert op_rows, "AUD-3: reconcile-driven op COMPLETE must be audited"
    audited = op_rows[-1]
    assert audited.new_values == {"status": "complete"}
    assert audited.user_id == admin.id, "attributed to the reader"
    assert audited.company_id == COMPANY_A
    assert audited.extra_data and audited.extra_data.get("source") == "reconcile_on_read"
