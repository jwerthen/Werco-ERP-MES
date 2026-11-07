"""Completing a READY operation STARTS it -- so it passes the work-order start gate first.

Both complete verbs auto-start a READY operation before booking it. Until ruling 2 that
branch checked only for a TERMINAL work order, so a desk Complete on a READY operation of
an ON_HOLD job started it, booked the quantity and moved tied stock -- on a job a supervisor
had stopped. Pinned here:

1. ``POST /work-orders/operations/{id}/complete`` (office) and
   ``POST /shop-floor/operations/{id}/complete`` (floor) refuse **409** with the start verbs'
   exact text for a READY operation under an ON_HOLD (``MSG_WORK_ORDER_ON_HOLD``) or DRAFT
   (``MSG_WORK_ORDER_NOT_RELEASED``) work order, and write nothing: the operation stays READY
   at its quantity, no inventory transaction exists for its material tie, and (the last
   operation) the work order is not COMPLETE. A positive control on a RELEASED job proves the
   tie really does consume, so the negative means something.
2. An IN_PROGRESS operation under an ON_HOLD work order stays completable on BOTH verbs (the
   owner-question default; the kiosk finish flow completes after clock-out).
3. The scanner reports it: ``complete`` is not legal, the constant is in
   ``blockers['complete']``, and the real call's 409 detail is that blocker.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.db.ledger_filter import OPERATION_REFERENCE_TYPE
from app.models.inventory import InventoryItem, InventoryTransaction
from app.models.part import Part
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.user import UserRole
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.models.work_order_material import AllocationSource, AllocationStatus, WorkOrderMaterialAllocation
from app.services.operation_action_gates import MSG_WORK_ORDER_NOT_RELEASED, MSG_WORK_ORDER_ON_HOLD
from tests.api.kiosk_test_helpers import make_user, make_wo_with_operation, make_work_center, user_headers

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

QTY = 4
REASON_BY_STATUS = {
    WorkOrderStatus.DRAFT: MSG_WORK_ORDER_NOT_RELEASED,
    WorkOrderStatus.ON_HOLD: MSG_WORK_ORDER_ON_HOLD,
}
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def tie_material(db: Session, wo: WorkOrder, op: WorkOrderOperation) -> Part:
    """An operation-scoped sheet tie (1 sheet per piece) with stock to consume."""
    n = _next()
    sheet = Part(
        part_number=f"GATE-SHEET-{n}",
        name=f"Gate sheet {n}",
        part_type="raw_material",
        unit_of_measure="sheets",
        standard_cost=50.0,
        is_active=True,
        company_id=wo.company_id,
    )
    db.add(sheet)
    db.flush()
    db.add(
        InventoryItem(
            part_id=sheet.id,
            location="RAW-A",
            warehouse="MAIN",
            quantity_on_hand=100.0,
            quantity_allocated=0.0,
            quantity_available=100.0,
            lot_number=f"GATE-LOT-{n}",
            unit_cost=50.0,
            status="available",
            is_active=True,
            company_id=wo.company_id,
        )
    )
    db.add(
        WorkOrderMaterialAllocation(
            company_id=wo.company_id,
            work_order_id=wo.id,
            work_order_operation_id=op.id,
            part_id=sheet.id,
            source=AllocationSource.NEST,
            status=AllocationStatus.OPEN,
            qty_per_run=1.0,
            qty_planned=float(QTY),
            unit_of_measure="sheets",
            qty_consumed=0.0,
        )
    )
    db.commit()
    return sheet


def closed_entry(db: Session, user, op: WorkOrderOperation) -> TimeEntry:
    entry = TimeEntry(
        user_id=user.id,
        work_order_id=op.work_order_id,
        operation_id=op.id,
        work_center_id=op.work_center_id,
        entry_type=TimeEntryType.RUN,
        clock_in=datetime.utcnow() - timedelta(hours=1),
        clock_out=datetime.utcnow() - timedelta(minutes=5),
        company_id=user.company_id,
    )
    db.add(entry)
    db.commit()
    return entry


def consumption(db: Session, op_id: int) -> list[InventoryTransaction]:
    return (
        db.query(InventoryTransaction)
        .filter(
            InventoryTransaction.reference_type == OPERATION_REFERENCE_TYPE,
            InventoryTransaction.reference_id == op_id,
        )
        .all()
    )


def office_complete(client, op, user):
    return client.post(
        f"/api/v1/work-orders/operations/{op.id}/complete",
        params={"quantity_complete": QTY},
        headers=user_headers(user),
    )


def floor_complete(client, op, user):
    return client.post(
        f"/api/v1/shop-floor/operations/{op.id}/complete",
        json={"quantity_complete": QTY},
        headers=user_headers(user),
    )


@pytest.fixture
def people(db_session: Session):
    return make_user(db_session, role=UserRole.OPERATOR), make_user(db_session, role=UserRole.SUPERVISOR)


def _rows(db: Session, wo: WorkOrder, op: WorkOrderOperation) -> tuple[WorkOrder, WorkOrderOperation]:
    db.expire_all()
    return (
        db.query(WorkOrder).filter(WorkOrder.id == wo.id).one(),
        db.query(WorkOrderOperation).filter(WorkOrderOperation.id == op.id).one(),
    )


@pytest.mark.parametrize("verb", ["office", "floor"])
@pytest.mark.parametrize("wo_status", [WorkOrderStatus.ON_HOLD, WorkOrderStatus.DRAFT])
def test_completing_a_ready_operation_on_a_held_or_draft_job_is_refused_and_moves_nothing(
    client, db_session, people, verb, wo_status
):
    operator, supervisor = people
    wc = make_work_center(db_session)
    wo, op = make_wo_with_operation(db_session, work_center=wc, wo_status=wo_status, quantity_ordered=QTY)
    tie_material(db_session, wo, op)
    closed_entry(db_session, operator, op)  # the floor verb's labor gate must not be what refuses it

    resp = office_complete(client, op, supervisor) if verb == "office" else floor_complete(client, op, operator)

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == REASON_BY_STATUS[wo_status]
    wo_row, op_row = _rows(db_session, wo, op)
    assert op_row.status == OperationStatus.READY
    assert float(op_row.quantity_complete or 0) == 0
    assert op_row.actual_start is None
    assert consumption(db_session, op.id) == []
    # It is the only operation: a completion here would have completed the work order.
    assert wo_row.status == wo_status


@pytest.mark.parametrize("verb", ["office", "floor"])
def test_positive_control_the_same_completion_on_a_released_job_consumes_and_completes(
    client, db_session, people, verb
):
    operator, supervisor = people
    wc = make_work_center(db_session)
    wo, op = make_wo_with_operation(
        db_session, work_center=wc, wo_status=WorkOrderStatus.RELEASED, quantity_ordered=QTY
    )
    tie_material(db_session, wo, op)
    closed_entry(db_session, operator, op)

    resp = office_complete(client, op, supervisor) if verb == "office" else floor_complete(client, op, operator)

    assert resp.status_code == 200, resp.text
    wo_row, op_row = _rows(db_session, wo, op)
    assert op_row.status == OperationStatus.COMPLETE
    assert consumption(db_session, op.id), "the tie must consume on a released job, or the negative proves nothing"
    assert wo_row.status == WorkOrderStatus.COMPLETE


@pytest.mark.parametrize("verb", ["office", "floor"])
def test_an_in_progress_operation_on_a_held_job_stays_completable(client, db_session, people, verb):
    """Owner-question default: work already running when the job was held can be finished."""
    operator, supervisor = people
    wc = make_work_center(db_session)
    wo, op = make_wo_with_operation(
        db_session,
        work_center=wc,
        wo_status=WorkOrderStatus.ON_HOLD,
        op_status=OperationStatus.IN_PROGRESS,
        quantity_ordered=QTY,
    )
    closed_entry(db_session, operator, op)

    resp = office_complete(client, op, supervisor) if verb == "office" else floor_complete(client, op, operator)

    assert resp.status_code == 200, resp.text
    _, op_row = _rows(db_session, wo, op)
    assert op_row.status == OperationStatus.COMPLETE


def test_scanner_reports_the_complete_refusal_with_the_real_calls_text(client, db_session, people):
    operator, _ = people
    wc = make_work_center(db_session)
    wo, op = make_wo_with_operation(db_session, work_center=wc, wo_status=WorkOrderStatus.ON_HOLD, quantity_ordered=QTY)
    closed_entry(db_session, operator, op)

    resolved = client.post(
        "/api/v1/scanner/resolve-action", json={"code": f"OP:{op.id}"}, headers=user_headers(operator)
    )
    assert resolved.status_code == 200, resolved.text
    body = resolved.json()
    assert "complete" not in body["legal_actions"]
    assert MSG_WORK_ORDER_ON_HOLD in body["blockers"]["complete"]
    assert MSG_WORK_ORDER_ON_HOLD in body["blockers"]["clock_in"]

    real = floor_complete(client, op, operator)
    assert real.status_code == 409
    assert real.json()["detail"] in body["blockers"]["complete"]


def test_scanner_does_not_report_the_gate_for_an_in_progress_operation(client, db_session, people):
    operator, _ = people
    wc = make_work_center(db_session)
    _, op = make_wo_with_operation(
        db_session,
        work_center=wc,
        wo_status=WorkOrderStatus.ON_HOLD,
        op_status=OperationStatus.IN_PROGRESS,
        quantity_ordered=QTY,
    )
    closed_entry(db_session, operator, op)

    body = client.post(
        "/api/v1/scanner/resolve-action", json={"code": f"OP:{op.id}"}, headers=user_headers(operator)
    ).json()
    assert "complete" in body["legal_actions"]
