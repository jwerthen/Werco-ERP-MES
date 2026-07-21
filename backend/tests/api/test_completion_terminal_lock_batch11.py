"""Terminal-state lock coverage for the work-order completion paths (Batch 11A / G6-A).

A work order in a *terminal* status -- COMPLETE, CLOSED, or CANCELLED -- has
finished its lifecycle and must never be reopened or resurrected. Before the
G6-A fix the completion guards only checked COMPLETE/CLOSED, so a CANCELLED WO
slipped through: it could be driven to COMPLETE via the manual override, via an
operation-complete on its last open op, or even silently via reconcile-on-read
when all its ops happened to be COMPLETE. Each of those would re-fire FG
receipt / backflush / cost rollup and write a COMPLETE row onto the
tamper-evident audit chain -- a traceability defect (a cancelled job presented
as a finished, shippable one).

``app.services.work_order_state_service.TERMINAL_WO_STATUSES`` is the single
source of truth (COMPLETE/CLOSED/CANCELLED). These tests lock the contracts the
fix must hold:

- manual ``complete_work_order`` on a CANCELLED WO            -> 409   (work_orders.py)
- office  ``complete_operation``     on a CANCELLED WO's op   -> 409   (work_orders.py)
- shop-floor ``complete_operation``  on a CANCELLED WO's op   -> 409   (shop_floor.py)
- reconcile-on-read GET of a CANCELLED WO whose ops are all
  COMPLETE does NOT flip the WO to COMPLETE / IN_PROGRESS     (state service guard)
- ``update_work_order`` moving a CANCELLED/CLOSED WO back to
  a non-terminal status (IN_PROGRESS)                         -> 409   (work_orders.py)
- regression: a normal IN_PROGRESS WO still completes fine.

The 409 (or no-op for the read path) must occur BEFORE any mutation, so every
test re-reads the WO/op from the DB after the rejected call and asserts the
pre-call state survived (status unchanged, actual_end still null, qty still 0).

Fixtures mirror the sibling completion suites: rows are created directly in the
shared ``db_session`` (tests/conftest.py); requests use a directly-minted token;
the ``client`` fixture overrides ``get_db`` to yield that same session.
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
from app.services.work_order_state_service import TERMINAL_WO_STATUSES

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"  # tokens minted directly; never used for login
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def make_user(db: Session, *, role: UserRole = UserRole.ADMIN) -> User:
    n = _next()
    user = User(
        email=f"tlock-{n}@co{COMPANY_A}.test",
        employee_id=f"TLOCK-{n:05d}",
        first_name="TLock",
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
        part_number=f"TLOCK-P-{n}",
        name=f"Part {n}",
        description="terminal-lock fixture part",
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
        name=f"TLOCK-WC-{n}",
        code=f"TLOCK-WC-{n}",
        work_center_type="welding",
        description="terminal-lock fixture work center",
        hourly_rate=100,
        is_active=True,
        company_id=COMPANY_A,
    )
    db.add(wc)
    db.commit()
    db.refresh(wc)
    return wc


def make_wo_with_op(
    db: Session,
    *,
    wo_status: WorkOrderStatus,
    op_status: OperationStatus = OperationStatus.IN_PROGRESS,
    quantity_ordered: float = 10,
    op_quantity_complete: float = 0,
) -> tuple[WorkOrder, WorkOrderOperation, WorkCenter]:
    part = make_part(db)
    wc = make_work_center(db)
    n = _next()
    wo = WorkOrder(
        work_order_number=f"TLOCK-WO-{n:05d}",
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
        name="TLock Op",
        status=op_status,
        quantity_complete=op_quantity_complete,
        company_id=COMPANY_A,
    )
    db.add(op)
    db.commit()
    db.refresh(wo)
    db.refresh(op)
    return wo, op, wc


def make_closed_time_entry(
    db: Session,
    *,
    user: User,
    wo: WorkOrder,
    op: WorkOrderOperation,
    wc: WorkCenter,
    quantity_produced: float,
) -> TimeEntry:
    entry = TimeEntry(
        user_id=user.id,
        work_order_id=wo.id,
        operation_id=op.id,
        work_center_id=wc.id,
        entry_type=TimeEntryType.RUN,
        clock_in=datetime.utcnow() - timedelta(hours=2),
        clock_out=datetime.utcnow() - timedelta(hours=1),
        duration_hours=1.0,
        quantity_produced=quantity_produced,
        company_id=COMPANY_A,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def make_open_time_entry(
    db: Session,
    *,
    user: User,
    wo: WorkOrder,
    op: WorkOrderOperation,
    wc: WorkCenter,
) -> TimeEntry:
    """An OPEN (clock_out=None) RUN entry -- the operator is still clocked in.

    Mirrors what ``clock_in``/``start_operation`` produce: a live row the operator
    must be able to close even if the parent WO went terminal mid-operation.
    """
    entry = TimeEntry(
        user_id=user.id,
        work_order_id=wo.id,
        operation_id=op.id,
        work_center_id=wc.id,
        entry_type=TimeEntryType.RUN,
        clock_in=datetime.utcnow() - timedelta(hours=1),
        clock_out=None,
        company_id=COMPANY_A,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def _reload(db: Session, model, pk: int):
    db.expire_all()
    return db.get(model, pk)


def _operational_events(db: Session, *, event_type: str, work_order_id: int):
    db.expire_all()
    from app.models.operational_event import OperationalEvent

    return (
        db.query(OperationalEvent)
        .filter(
            OperationalEvent.event_type == event_type,
            OperationalEvent.work_order_id == work_order_id,
            OperationalEvent.company_id == COMPANY_A,
        )
        .all()
    )


def _committed_status_change_rows(db: Session, *, resource_type: str, resource_id: int):
    """STATUS_CHANGE audit rows that actually COMMITTED (rollback discards flush-only)."""
    db.rollback()
    db.expire_all()
    return (
        db.query(AuditLog)
        .filter(
            AuditLog.resource_type == resource_type,
            AuditLog.resource_id == resource_id,
            AuditLog.action == "STATUS_CHANGE",
        )
        .all()
    )


# ---------------------------------------------------------------------------
# Sanity: the terminal set is exactly {COMPLETE, CLOSED, CANCELLED}.
# ---------------------------------------------------------------------------


def test_terminal_status_set_contains_cancelled_closed_complete():
    assert TERMINAL_WO_STATUSES == {
        WorkOrderStatus.COMPLETE,
        WorkOrderStatus.CLOSED,
        WorkOrderStatus.CANCELLED,
    }


# ---------------------------------------------------------------------------
# 1. manual complete_work_order on a CANCELLED WO -> 409, no mutation.
# ---------------------------------------------------------------------------


def test_complete_cancelled_work_order_is_409_and_no_mutation(client: TestClient, db_session: Session):
    admin = make_user(db_session)
    wo, _op, _wc = make_wo_with_op(db_session, wo_status=WorkOrderStatus.CANCELLED)

    resp = client.post(
        f"/api/v1/work-orders/{wo.id}/complete?quantity_complete=10",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
    assert "cancelled" in resp.json()["detail"].lower()

    wo_after = _reload(db_session, WorkOrder, wo.id)
    assert wo_after.status == WorkOrderStatus.CANCELLED, "cancelled WO must not be resurrected to COMPLETE"
    assert wo_after.actual_end is None
    # No COMPLETE status-change audit row was written onto the chain.
    rows = _committed_status_change_rows(db_session, resource_type="work_order", resource_id=wo.id)
    assert rows == [], "a refused completion must not write a status-change audit row"


# ---------------------------------------------------------------------------
# 2. office complete_operation on a CANCELLED WO's op -> 409, no mutation.
# ---------------------------------------------------------------------------


def test_office_complete_operation_on_cancelled_wo_is_409_and_no_mutation(client: TestClient, db_session: Session):
    admin = make_user(db_session)
    wo, op, _wc = make_wo_with_op(
        db_session, wo_status=WorkOrderStatus.CANCELLED, op_status=OperationStatus.IN_PROGRESS
    )

    resp = client.post(
        f"/api/v1/work-orders/operations/{op.id}/complete?quantity_complete=10",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
    assert "cancelled" in resp.json()["detail"].lower()

    op_after = _reload(db_session, WorkOrderOperation, op.id)
    wo_after = _reload(db_session, WorkOrder, wo.id)
    assert op_after.status == OperationStatus.IN_PROGRESS, "op must not be completed on a cancelled WO"
    assert op_after.actual_end is None
    assert float(op_after.quantity_complete or 0) == 0.0
    assert wo_after.status == WorkOrderStatus.CANCELLED, "WO must stay CANCELLED"


# ---------------------------------------------------------------------------
# 3. shop-floor complete_operation on a CANCELLED WO's op -> 409, no mutation.
# ---------------------------------------------------------------------------


def test_shop_floor_complete_operation_on_cancelled_wo_is_409_and_no_mutation(client: TestClient, db_session: Session):
    operator = make_user(db_session, role=UserRole.OPERATOR)
    wo, op, _wc = make_wo_with_op(
        db_session, wo_status=WorkOrderStatus.CANCELLED, op_status=OperationStatus.IN_PROGRESS
    )

    resp = client.post(
        f"/api/v1/shop-floor/operations/{op.id}/complete",
        headers=headers_for(operator),
        json={"quantity_complete": 10},
    )
    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
    assert "cancelled" in resp.json()["detail"].lower()

    op_after = _reload(db_session, WorkOrderOperation, op.id)
    wo_after = _reload(db_session, WorkOrder, wo.id)
    assert op_after.status == OperationStatus.IN_PROGRESS, "op must not be completed on a cancelled WO"
    assert op_after.actual_end is None
    assert float(op_after.quantity_complete or 0) == 0.0
    assert wo_after.status == WorkOrderStatus.CANCELLED


# ---------------------------------------------------------------------------
# 4. reconcile-on-read of a CANCELLED WO whose ops are all COMPLETE must NOT
#    flip it to COMPLETE / IN_PROGRESS.
# ---------------------------------------------------------------------------


def test_reconcile_on_read_does_not_resurrect_cancelled_wo_with_complete_ops(client: TestClient, db_session: Session):
    """A GET drives ``_sync_work_order_status_from_operations`` (reconcile-on-read).
    Even with every operation already COMPLETE and durable evidence for the full
    ordered quantity, a CANCELLED WO must be left exactly as committed -- the
    terminal guard returns before any status flip."""
    admin = make_user(db_session)
    wo, op, wc = make_wo_with_op(
        db_session,
        wo_status=WorkOrderStatus.CANCELLED,
        op_status=OperationStatus.COMPLETE,
        quantity_ordered=4,
        op_quantity_complete=4,
    )
    # Durable closed evidence for the full quantity -- the strongest case for a
    # reconcile to try to flip the WO COMPLETE.
    make_closed_time_entry(db_session, user=admin, wo=wo, op=op, wc=wc, quantity_produced=4)

    resp = client.get(f"/api/v1/work-orders/{wo.id}", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    # The read reflects the committed (cancelled) state, not a reconciled COMPLETE.
    assert resp.json()["status"] == WorkOrderStatus.CANCELLED.value

    wo_after = _reload(db_session, WorkOrder, wo.id)
    assert wo_after.status == WorkOrderStatus.CANCELLED, "reconcile-on-read must not resurrect a cancelled WO"
    assert wo_after.actual_end is None
    # No reconcile-driven COMPLETE status-change audit row was written.
    rows = _committed_status_change_rows(db_session, resource_type="work_order", resource_id=wo.id)
    assert rows == [], "reconcile must not write a status-change row for a terminal WO"


def test_reconcile_on_read_does_not_reopen_cancelled_wo_to_in_progress(client: TestClient, db_session: Session):
    """Partial progress on a CANCELLED WO (one op IN_PROGRESS, durable evidence)
    must NOT lift the WO to IN_PROGRESS via reconcile-on-read."""
    admin = make_user(db_session)
    wo, op, wc = make_wo_with_op(
        db_session,
        wo_status=WorkOrderStatus.CANCELLED,
        op_status=OperationStatus.IN_PROGRESS,
        quantity_ordered=10,
        op_quantity_complete=3,
    )
    make_closed_time_entry(db_session, user=admin, wo=wo, op=op, wc=wc, quantity_produced=3)

    resp = client.get(f"/api/v1/work-orders/{wo.id}", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["status"] == WorkOrderStatus.CANCELLED.value

    wo_after = _reload(db_session, WorkOrder, wo.id)
    assert wo_after.status == WorkOrderStatus.CANCELLED, "reconcile-on-read must not reopen a cancelled WO"


# ---------------------------------------------------------------------------
# 5. update_work_order moving a terminal WO back to a non-terminal status -> 409.
# ---------------------------------------------------------------------------


def test_update_work_order_cancelled_to_in_progress_is_409(client: TestClient, db_session: Session):
    admin = make_user(db_session)
    wo, _op, _wc = make_wo_with_op(db_session, wo_status=WorkOrderStatus.CANCELLED)

    # WorkOrderUpdate requires `version` and it is REAL optimistic locking now (the
    # WO model maps version_id_col): a mismatch would 409 on staleness before the
    # G6-A terminal guard runs. Send the row's live version so the 409 asserted
    # below provably comes from the terminal guard, not the version check.
    resp = client.put(
        f"/api/v1/work-orders/{wo.id}",
        headers=headers_for(admin),
        json={"version": wo.version, "status": "in_progress"},
    )
    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
    assert "terminal" in resp.json()["detail"].lower()

    wo_after = _reload(db_session, WorkOrder, wo.id)
    assert wo_after.status == WorkOrderStatus.CANCELLED, "terminal WO must not be reopened via update"


def test_update_work_order_closed_to_in_progress_is_409(client: TestClient, db_session: Session):
    admin = make_user(db_session)
    wo, _op, _wc = make_wo_with_op(db_session, wo_status=WorkOrderStatus.CLOSED)

    # Live version (see the comment in the CANCELLED variant above): the asserted
    # 409 must come from the terminal guard, not the optimistic-lock check.
    resp = client.put(
        f"/api/v1/work-orders/{wo.id}",
        headers=headers_for(admin),
        json={"version": wo.version, "status": "in_progress"},
    )
    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
    assert "terminal" in resp.json()["detail"].lower()

    wo_after = _reload(db_session, WorkOrder, wo.id)
    assert wo_after.status == WorkOrderStatus.CLOSED, "closed WO must not be reopened via update"


def test_update_work_order_terminal_to_terminal_is_allowed(client: TestClient, db_session: Session):
    """The guard only blocks terminal -> NON-terminal. A terminal -> terminal move
    (CANCELLED -> CLOSED, e.g. archiving a cancelled job) is NOT blocked here."""
    admin = make_user(db_session)
    wo, _op, _wc = make_wo_with_op(db_session, wo_status=WorkOrderStatus.CANCELLED)

    resp = client.put(
        f"/api/v1/work-orders/{wo.id}",
        headers=headers_for(admin),
        json={"version": wo.version, "status": "closed"},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    wo_after = _reload(db_session, WorkOrder, wo.id)
    assert wo_after.status == WorkOrderStatus.CLOSED


# ---------------------------------------------------------------------------
# 6. Regression: a normal IN_PROGRESS WO still completes fine (no over-block).
# ---------------------------------------------------------------------------


def test_in_progress_work_order_still_completes(client: TestClient, db_session: Session):
    admin = make_user(db_session)
    wo, op, _wc = make_wo_with_op(
        db_session, wo_status=WorkOrderStatus.IN_PROGRESS, op_status=OperationStatus.IN_PROGRESS
    )

    resp = client.post(
        f"/api/v1/work-orders/{wo.id}/complete?quantity_complete=10",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    wo_after = _reload(db_session, WorkOrder, wo.id)
    op_after = _reload(db_session, WorkOrderOperation, op.id)
    assert wo_after.status == WorkOrderStatus.COMPLETE, "a non-terminal WO must still complete"
    assert op_after.status == OperationStatus.COMPLETE


def test_in_progress_operation_still_completes(client: TestClient, db_session: Session):
    """Office op-complete on a normal IN_PROGRESS WO still works (the terminal guard
    must not over-block the live path)."""
    admin = make_user(db_session)
    wo, op, _wc = make_wo_with_op(
        db_session, wo_status=WorkOrderStatus.IN_PROGRESS, op_status=OperationStatus.IN_PROGRESS
    )

    resp = client.post(
        f"/api/v1/work-orders/operations/{op.id}/complete?quantity_complete=10",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    op_after = _reload(db_session, WorkOrderOperation, op.id)
    wo_after = _reload(db_session, WorkOrder, wo.id)
    assert op_after.status == OperationStatus.COMPLETE
    assert wo_after.status == WorkOrderStatus.COMPLETE, "last op complete still finishes the WO"


# ---------------------------------------------------------------------------
# 7. shop-floor start_operation on a terminal WO -> 409, no mutation (HOLE 1).
#    You can never legitimately begin new work on a finished/cancelled job.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "wo_status",
    [WorkOrderStatus.CANCELLED, WorkOrderStatus.CLOSED, WorkOrderStatus.COMPLETE],
)
def test_start_operation_on_terminal_wo_is_409_and_no_mutation(
    client: TestClient, db_session: Session, wo_status: WorkOrderStatus
):
    operator = make_user(db_session, role=UserRole.OPERATOR)
    wo, op, _wc = make_wo_with_op(db_session, wo_status=wo_status, op_status=OperationStatus.PENDING)

    resp = client.put(
        f"/api/v1/shop-floor/operations/{op.id}/start",
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
    assert wo_status.value.lower() in resp.json()["detail"].lower()

    op_after = _reload(db_session, WorkOrderOperation, op.id)
    wo_after = _reload(db_session, WorkOrder, wo.id)
    # The guard runs BEFORE any mutation: op stays PENDING, no actual_start, no time
    # entry created, WO status untouched.
    assert op_after.status == OperationStatus.PENDING, "op must not be started on a terminal WO"
    assert op_after.actual_start is None
    assert wo_after.status == wo_status, "terminal WO status must be unchanged"
    open_entries = (
        db_session.query(TimeEntry).filter(TimeEntry.operation_id == op.id, TimeEntry.clock_out.is_(None)).all()
    )
    assert open_entries == [], "a refused start must not create an open time entry"


def test_start_operation_on_in_progress_wo_still_starts(client: TestClient, db_session: Session):
    """Regression: the start guard must not over-block a normal (RELEASED) WO."""
    operator = make_user(db_session, role=UserRole.OPERATOR)
    wo, op, _wc = make_wo_with_op(db_session, wo_status=WorkOrderStatus.RELEASED, op_status=OperationStatus.PENDING)

    resp = client.put(
        f"/api/v1/shop-floor/operations/{op.id}/start",
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    op_after = _reload(db_session, WorkOrderOperation, op.id)
    assert op_after.status == OperationStatus.IN_PROGRESS, "a non-terminal WO's op must still start"
    assert op_after.actual_start is not None


# ---------------------------------------------------------------------------
# 8. shop-floor clock_out of an operator clocked into a now-CANCELLED WO:
#    SUCCEEDS (closes the time entry so the operator is never trapped) but does
#    NOT roll up onto the terminal WO -- no op COMPLETE flip, no quantity_complete
#    bump, no actual_hours accrual, and no COMPLETE audit row / completion signal
#    (HOLE 2).
# ---------------------------------------------------------------------------


def test_clock_out_on_cancelled_wo_closes_entry_without_rollup(client: TestClient, db_session: Session):
    operator = make_user(db_session, role=UserRole.OPERATOR)
    # Quantity ordered == produced-on-clock-out so that, WITHOUT the terminal guard,
    # the op would be driven to COMPLETE and the WO finalized -- the strongest case
    # for an unwanted rollup. The guard must suppress all of it.
    wo, op, wc = make_wo_with_op(
        db_session,
        wo_status=WorkOrderStatus.CANCELLED,
        op_status=OperationStatus.IN_PROGRESS,
        quantity_ordered=5,
        op_quantity_complete=0,
    )
    wo.actual_hours = 0.0
    db_session.commit()
    entry = make_open_time_entry(db_session, user=operator, wo=wo, op=op, wc=wc)

    resp = client.post(
        f"/api/v1/shop-floor/clock-out/{entry.id}",
        headers=headers_for(operator),
        json={"quantity_produced": 5, "quantity_scrapped": 0},
    )
    # The operator is NEVER trapped: clock-out succeeds and the entry is closed.
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["clock_out"] is not None, "the time entry must be closed so the operator is freed"

    entry_after = _reload(db_session, TimeEntry, entry.id)
    op_after = _reload(db_session, WorkOrderOperation, op.id)
    wo_after = _reload(db_session, WorkOrder, wo.id)

    # The durable labor record is closed (clock_out + duration captured).
    assert entry_after.clock_out is not None
    assert entry_after.duration_hours is not None and entry_after.duration_hours > 0

    # But NOTHING rolled up onto the terminal WO/op.
    assert op_after.status == OperationStatus.IN_PROGRESS, "terminal-WO op must NOT be flipped to COMPLETE"
    assert op_after.actual_end is None
    assert float(op_after.quantity_complete or 0) == 0.0, "op quantity_complete must NOT be bumped"
    assert wo_after.status == WorkOrderStatus.CANCELLED, "WO must stay CANCELLED"
    assert float(wo_after.actual_hours or 0) == 0.0, "no labor cost may accrue onto a terminal WO"

    # No COMPLETE status-change audit row was written for the op or the WO.
    op_rows = _committed_status_change_rows(db_session, resource_type="work_order_operation", resource_id=op.id)
    wo_rows = _committed_status_change_rows(db_session, resource_type="work_order", resource_id=wo.id)
    assert op_rows == [], "terminal clock-out must not write an op STATUS_CHANGE row"
    assert wo_rows == [], "terminal clock-out must not write a WO STATUS_CHANGE row"

    # No completion OperationalEvents fired (labor_clock_out is fine; completion is not).
    assert _operational_events(db_session, event_type="operation_completed", work_order_id=wo.id) == []
    assert _operational_events(db_session, event_type="work_order_completed", work_order_id=wo.id) == []
    # The labor signal still fired so the entry is observable, flagged as terminal.
    labor_events = _operational_events(db_session, event_type="labor_clock_out", work_order_id=wo.id)
    assert len(labor_events) == 1, "the labor_clock_out signal must still fire"
    assert labor_events[0].event_payload.get("wo_terminal") is True


def test_clock_out_on_in_progress_wo_still_rolls_up(client: TestClient, db_session: Session):
    """Regression: clock-out on a NON-terminal WO still rolls qty/hours up and can
    complete the op (the terminal gate must not suppress the live rollup)."""
    operator = make_user(db_session, role=UserRole.OPERATOR)
    wo, op, wc = make_wo_with_op(
        db_session,
        wo_status=WorkOrderStatus.IN_PROGRESS,
        op_status=OperationStatus.IN_PROGRESS,
        quantity_ordered=5,
        op_quantity_complete=0,
    )
    wo.actual_hours = 0.0
    db_session.commit()
    entry = make_open_time_entry(db_session, user=operator, wo=wo, op=op, wc=wc)

    resp = client.post(
        f"/api/v1/shop-floor/clock-out/{entry.id}",
        headers=headers_for(operator),
        json={"quantity_produced": 5, "quantity_scrapped": 0},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    op_after = _reload(db_session, WorkOrderOperation, op.id)
    wo_after = _reload(db_session, WorkOrder, wo.id)
    assert op_after.status == OperationStatus.COMPLETE, "non-terminal op completes on full qty"
    assert float(op_after.quantity_complete or 0) == 5.0, "qty rolled up on a live WO"
    assert float(wo_after.actual_hours or 0) > 0.0, "labor accrued on a live WO"
