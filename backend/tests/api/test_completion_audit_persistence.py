"""Audit-row coverage for the work-order/operation completion paths.

Locks in the Batch-1 hardening (branch qa/full-pass-2026-06-04) that made the
completion endpoints write tamper-evident ``audit_log`` STATUS_CHANGE rows on the
hash chain (invariant #2). Before the fix these terminal COMPLETE transitions
left no durable audit trail (an AS9100D / CMMC AU-3.3.8 violation).

Gap items covered:
- RUP-5: POST /api/v1/work-orders/{id}/complete  -> work_order STATUS_CHANGE
- DUP-1: POST /api/v1/work-orders/operations/{id}/complete (office) ->
         work_order_operation STATUS_CHANGE (+ work_order when the WO finishes)
- AUD-1: POST /api/v1/shop-floor/clock-out/{id} that finishes the last op ->
         work_order_operation STATUS_CHANGE + work_order STATUS_CHANGE

Why the committed-row guard matters
------------------------------------
The ``client`` fixture (tests/conftest.py) overrides ``get_db`` to yield ONE
shared, never-closed ``db_session``. ``AuditService.log()`` only ``flush()``es;
the handler owns the terminal ``db.commit()``. A handler that logged AFTER its
commit would flush the audit row into a never-committed transaction, yet a naive
``db.query(AuditLog)`` in the test would still SEE it (same open transaction).
Rolling back BEFORE querying closes that loophole: a committed audit row survives
the rollback; a flushed-only one is discarded. So these assertions prove the
audit rows are actually durable, not merely flushed.

Each test also asserts the old->new status payload (log_status_change shape) and
that the hash chain stays well-formed (strictly increasing sequence numbers,
non-null integrity hashes, previous_hash links).
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
from app.models.work_order import (
    OperationStatus,
    WorkOrder,
    WorkOrderOperation,
    WorkOrderStatus,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1

TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"  # tokens are minted directly; never used for login

# Module-level counter so every fixture row gets a globally unique natural key,
# even across tests sharing a worker DB under -n auto.
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def make_user(db: Session, *, role: UserRole = UserRole.ADMIN) -> User:
    n = _next()
    user = User(
        email=f"comp-aud-{n}@co{COMPANY_A}.test",
        employee_id=f"CAUD-{n:05d}",
        first_name="Aud",
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
        part_number=f"CAUD-P-{n}",
        name=f"Part {n}",
        description="completion-audit fixture part",
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
        name=f"CAUD-WC-{n}",
        code=f"CAUD-WC-{n}",
        work_center_type="welding",
        description="completion-audit fixture work center",
        hourly_rate=100,
        is_active=True,
        company_id=COMPANY_A,
    )
    db.add(wc)
    db.commit()
    db.refresh(wc)
    return wc


def make_work_order_with_operation(
    db: Session,
    *,
    wo_status: WorkOrderStatus = WorkOrderStatus.IN_PROGRESS,
    op_status: OperationStatus = OperationStatus.IN_PROGRESS,
    quantity_ordered: float = 10,
) -> tuple[WorkOrder, WorkOrderOperation, WorkCenter]:
    part = make_part(db)
    wc = make_work_center(db)
    n = _next()
    wo = WorkOrder(
        work_order_number=f"CAUD-WO-{n:05d}",
        customer_name="Acme",
        part_id=part.id,
        quantity_ordered=quantity_ordered,
        status=wo_status,
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
        name="Audit Op",
        status=op_status,
        company_id=COMPANY_A,
    )
    db.add(op)
    db.commit()
    db.refresh(wo)
    db.refresh(op)
    return wo, op, wc


def _committed_audit_rows(db: Session, *, resource_type: str, resource_id: int, action: str = None):
    """Audit rows that were actually COMMITTED (not merely flushed), newest first.

    ``db.rollback()`` before reading discards any flushed-but-uncommitted audit
    row, so this only returns rows that survived the handler's terminal commit.
    """
    db.rollback()
    db.expire_all()
    q = db.query(AuditLog).filter(
        AuditLog.resource_type == resource_type,
        AuditLog.resource_id == resource_id,
    )
    if action is not None:
        q = q.filter(AuditLog.action == action)
    return q.order_by(AuditLog.sequence_number.desc()).all()


def _assert_hash_chain_intact(db: Session) -> None:
    """The full committed audit chain is well-formed: strictly increasing unique
    sequence numbers, non-null integrity hashes, and previous_hash links."""
    db.expire_all()
    logs = db.query(AuditLog).order_by(AuditLog.sequence_number).all()
    assert logs, "expected at least one committed audit row"
    seqs = [log.sequence_number for log in logs]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)
    assert all(log.integrity_hash for log in logs)
    for prev, curr in zip(logs, logs[1:]):
        assert curr.previous_hash == prev.integrity_hash


# ---------------------------------------------------------------------------
# RUP-5: POST /work-orders/{id}/complete  -> work_order STATUS_CHANGE
# ---------------------------------------------------------------------------


def test_complete_work_order_emits_committed_status_change_audit(client: TestClient, db_session: Session):
    """The privileged manual WO-complete writes a durable STATUS_CHANGE row with
    the old->new status, plus the UPDATE row for the recorded quantities."""
    admin = make_user(db_session, role=UserRole.ADMIN)
    wo, _op, _wc = make_work_order_with_operation(db_session, wo_status=WorkOrderStatus.IN_PROGRESS)

    resp = client.post(
        f"/api/v1/work-orders/{wo.id}/complete?quantity_complete=10&quantity_scrapped=0",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    rows = _committed_audit_rows(
        db_session, resource_type="work_order", resource_id=wo.id, action="STATUS_CHANGE"
    )
    assert len(rows) == 1, "expected exactly one COMMITTED work_order STATUS_CHANGE row"
    row = rows[0]
    assert row.resource_type == "work_order"
    assert row.resource_id == wo.id
    assert row.company_id == COMPANY_A
    assert row.old_values == {"status": "in_progress"}
    assert row.new_values == {"status": "complete"}
    assert row.sequence_number is not None
    assert row.integrity_hash

    # The accompanying quantities UPDATE row is also committed.
    update_rows = _committed_audit_rows(
        db_session, resource_type="work_order", resource_id=wo.id, action="UPDATE"
    )
    assert len(update_rows) == 1, "expected the quantities UPDATE row to be committed too"

    _assert_hash_chain_intact(db_session)


# ---------------------------------------------------------------------------
# DUP-1: POST /work-orders/operations/{id}/complete (office) -> STATUS_CHANGE
# ---------------------------------------------------------------------------


def test_office_complete_operation_emits_committed_status_change_audit(client: TestClient, db_session: Session):
    """The office op-complete path writes a durable work_order_operation
    STATUS_CHANGE (in_progress -> complete); as the sole operation it also
    finishes the work order, which gets its own STATUS_CHANGE row."""
    admin = make_user(db_session, role=UserRole.ADMIN)
    wo, op, _wc = make_work_order_with_operation(
        db_session, wo_status=WorkOrderStatus.IN_PROGRESS, op_status=OperationStatus.IN_PROGRESS
    )

    resp = client.post(
        f"/api/v1/work-orders/operations/{op.id}/complete?quantity_complete=10&quantity_scrapped=0",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["message"] == "Operation completed"

    op_rows = _committed_audit_rows(
        db_session, resource_type="work_order_operation", resource_id=op.id, action="STATUS_CHANGE"
    )
    assert len(op_rows) == 1, "expected exactly one COMMITTED operation STATUS_CHANGE row"
    op_row = op_rows[0]
    assert op_row.resource_id == op.id
    assert op_row.company_id == COMPANY_A
    assert op_row.old_values == {"status": "in_progress"}
    assert op_row.new_values == {"status": "complete"}

    # Last operation -> work order also completes and is audited.
    wo_rows = _committed_audit_rows(
        db_session, resource_type="work_order", resource_id=wo.id, action="STATUS_CHANGE"
    )
    assert len(wo_rows) == 1, "expected the work_order completion STATUS_CHANGE row"
    assert wo_rows[0].new_values == {"status": "complete"}

    _assert_hash_chain_intact(db_session)


def test_office_complete_operation_partial_emits_committed_update_audit(client: TestClient, db_session: Session):
    """A partial office op-complete (below ordered qty) writes an UPDATE row, not
    a COMPLETE STATUS_CHANGE, and leaves the operation IN_PROGRESS."""
    admin = make_user(db_session, role=UserRole.ADMIN)
    _wo, op, _wc = make_work_order_with_operation(
        db_session, op_status=OperationStatus.IN_PROGRESS, quantity_ordered=10
    )

    resp = client.post(
        f"/api/v1/work-orders/operations/{op.id}/complete?quantity_complete=4",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    update_rows = _committed_audit_rows(
        db_session, resource_type="work_order_operation", resource_id=op.id, action="UPDATE"
    )
    assert len(update_rows) == 1, "expected a committed UPDATE row for the partial progress"
    assert update_rows[0].new_values == {"quantity_complete": 4, "quantity_scrapped": 0}

    status_rows = _committed_audit_rows(
        db_session, resource_type="work_order_operation", resource_id=op.id, action="STATUS_CHANGE"
    )
    assert status_rows == [], "a partial completion must NOT emit a COMPLETE STATUS_CHANGE"

    _assert_hash_chain_intact(db_session)


# ---------------------------------------------------------------------------
# AUD-1: shop-floor clock-out that finishes the last op -> STATUS_CHANGE rows
# ---------------------------------------------------------------------------


def test_clock_out_completion_emits_committed_status_change_audit(client: TestClient, db_session: Session):
    """Clocking out with enough produced quantity to finish the sole operation
    writes a durable operation STATUS_CHANGE AND a work_order STATUS_CHANGE on the
    hash chain (the floor completion path)."""
    operator = make_user(db_session, role=UserRole.OPERATOR)
    wo, op, wc = make_work_order_with_operation(
        db_session, wo_status=WorkOrderStatus.IN_PROGRESS, op_status=OperationStatus.IN_PROGRESS
    )
    entry = TimeEntry(
        user_id=operator.id,
        work_order_id=wo.id,
        operation_id=op.id,
        work_center_id=wc.id,
        entry_type=TimeEntryType.RUN,
        clock_in=datetime.utcnow() - timedelta(hours=2),
        company_id=COMPANY_A,
    )
    db_session.add(entry)
    db_session.commit()
    db_session.refresh(entry)

    resp = client.post(
        f"/api/v1/shop-floor/clock-out/{entry.id}",
        headers=headers_for(operator),
        json={"quantity_produced": 10, "quantity_scrapped": 0},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    op_rows = _committed_audit_rows(
        db_session, resource_type="work_order_operation", resource_id=op.id, action="STATUS_CHANGE"
    )
    assert len(op_rows) == 1, "expected a committed operation STATUS_CHANGE row from clock-out"
    assert op_rows[0].company_id == COMPANY_A
    assert op_rows[0].old_values == {"status": "in_progress"}
    assert op_rows[0].new_values == {"status": "complete"}

    wo_rows = _committed_audit_rows(
        db_session, resource_type="work_order", resource_id=wo.id, action="STATUS_CHANGE"
    )
    assert len(wo_rows) == 1, "expected a committed work_order STATUS_CHANGE row from clock-out"
    assert wo_rows[0].old_values == {"status": "in_progress"}
    assert wo_rows[0].new_values == {"status": "complete"}

    _assert_hash_chain_intact(db_session)
