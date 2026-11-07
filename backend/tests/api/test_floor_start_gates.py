"""Owner ruling 2026-11-05 (ruling 2): the floor cannot start work on a DRAFT or ON_HOLD work order.

Pinned here, on the DATABASE ROWS as well as the HTTP answer:

1. The matrix. ``POST /shop-floor/clock-in`` (operator), ``PUT /shop-floor/operations/{id}/start``
   (operator) and ``POST /work-orders/operations/{id}/start`` (supervisor) refuse **409** with
   the shared constant on a DRAFT ("release first") and an ON_HOLD ("Resume Work Order")
   work order -- for a PENDING and a READY operation alike -- and write NOTHING: the
   operation's status / actual_start / started_by and the work order's status are unchanged,
   no TimeEntry exists, and no start / status-change audit row exists.

   ``AuditService.log`` swallows its own failures, so "zero audit rows" proves nothing on its
   own. Every zero-row assertion here is PAIRED with a positive control in this module that
   runs the identical query against the RELEASED success case and finds exactly one row.

2. Ordering. DRAFT outranks the predecessor gate (409, not 400); RELEASED keeps the 400.
   CANCELLED on clock-in is now a 409 with the start verbs' terminal text (it had no
   terminal rung). A soft-deleted work order is a 404 on all three verbs and on
   ``POST /work-orders/{id}/start``.

3. An operation-level hold on a RELEASED job is refused **409** by both start verbs (they used
   to un-hold it silently, leaving the blocker OPEN). Clock-in keeps its 400.

4. No exemptions: a badge-minted kiosk token gets the 409 (not the 400 the crew client reads
   as "already clocked in"), and a supervisor's paper back-entry (``source='backfill'``) is
   refused like a live clock-in, with no TimeEntry and no back-fill audit row.

5. The office op start now refuses an operation that is already COMPLETE or IN_PROGRESS with
   the shop-floor twin's exact 400s, instead of re-opening / re-stamping it.

6. The read payloads carry ``start_blocked_reason`` / ``work_order_status`` on
   ``/shop-floor/operations``, ``/shop-floor/operations/{id}`` (operation AND all_operations)
   and the kiosk queue + held rows -- for a user token and a crew-station token -- and list
   membership is unchanged.
"""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.time_entry import TimeEntry
from app.models.user import UserRole
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.models.work_order_blocker import WorkOrderBlocker, WorkOrderBlockerStatus
from app.services.operation_action_gates import (
    MSG_ALREADY_COMPLETE,
    MSG_ALREADY_IN_PROGRESS,
    MSG_OPERATION_ON_HOLD_NO_START,
    MSG_PREDECESSORS_INCOMPLETE,
    MSG_WORK_ORDER_NOT_RELEASED,
    MSG_WORK_ORDER_ON_HOLD,
)
from tests.api.kiosk_test_helpers import (
    bearer,
    kiosk_token_for,
    make_kiosk_station,
    make_user,
    make_wo_with_operation,
    make_work_center,
    queue_url,
    user_headers,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

CLOCK_IN = "/api/v1/shop-floor/clock-in"
VERBS = ("clock_in", "shop_start", "office_start")
REASON_BY_STATUS = {
    WorkOrderStatus.DRAFT: MSG_WORK_ORDER_NOT_RELEASED,
    WorkOrderStatus.ON_HOLD: MSG_WORK_ORDER_ON_HOLD,
}


def badge_headers(user) -> dict:
    """A 5-minute badge-minted operator token, as POST /auth/kiosk-badge-token mints it."""
    return bearer(
        create_access_token(
            subject=user.id, company_id=user.company_id, expires_delta=timedelta(minutes=5), scope="kiosk"
        )
    )


def run_verb(client: TestClient, verb: str, op: WorkOrderOperation, *, operator, supervisor, source=None):
    if verb == "clock_in":
        body = {
            "work_order_id": op.work_order_id,
            "operation_id": op.id,
            "work_center_id": op.work_center_id,
            "entry_type": "run",
        }
        if source is not None:
            body["source"] = source
        return client.post(CLOCK_IN, json=body, headers=user_headers(operator))
    if verb == "shop_start":
        return client.put(f"/api/v1/shop-floor/operations/{op.id}/start", headers=user_headers(operator))
    if verb == "office_start":
        return client.post(f"/api/v1/work-orders/operations/{op.id}/start", headers=user_headers(supervisor))
    raise AssertionError(verb)


# The audit row each START verb writes on success. Clock-in writes none for a live entry
# (its evidence is the labor_clock_in OperationalEvent), so its "nothing happened" proof is
# the TimeEntry count, which the clock-in positive control pins as exactly one.
START_AUDIT_ACTION = {"shop_start": "START_OPERATION", "office_start": "STATUS_CHANGE"}


def start_audit_rows(db: Session, verb: str, op_id: int) -> list[AuditLog]:
    action = START_AUDIT_ACTION[verb]
    return (
        db.query(AuditLog)
        .filter(
            AuditLog.resource_type == "work_order_operation",
            AuditLog.resource_id == op_id,
            AuditLog.action == action,
        )
        .all()
    )


def time_entries(db: Session, op_id: int) -> list[TimeEntry]:
    return db.query(TimeEntry).filter(TimeEntry.operation_id == op_id).all()


def assert_no_pending_writes(db: Session) -> None:
    """Call right after a REFUSED request, before :func:`snapshot`.

    The app runs on this very session (conftest's get_db override) with autoflush off, so
    a refusal raised AFTER an in-memory mutation leaves dirty, unflushed state -- which
    snapshot()'s expire_all() would silently discard, letting "row unchanged" pass against
    a gate that runs too late.
    """
    assert not db.dirty, f"refused request left unflushed changes: {db.dirty!r}"
    assert not db.new, f"refused request left unflushed new rows: {db.new!r}"


def snapshot(db: Session, wo: WorkOrder, op: WorkOrderOperation) -> tuple:
    db.expire_all()
    op_row = db.query(WorkOrderOperation).filter(WorkOrderOperation.id == op.id).one()
    wo_row = db.query(WorkOrder).filter(WorkOrder.id == wo.id).one()
    return (op_row.status, op_row.actual_start, op_row.started_by, wo_row.status, wo_row.version)


@pytest.fixture
def floor(db_session: Session):
    wc = make_work_center(db_session)
    operator = make_user(db_session, role=UserRole.OPERATOR)
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    return wc, operator, supervisor


# =========================================================================== 1. the matrix


@pytest.mark.parametrize("verb", VERBS)
@pytest.mark.parametrize("wo_status", [WorkOrderStatus.DRAFT, WorkOrderStatus.ON_HOLD])
@pytest.mark.parametrize("op_status", [OperationStatus.PENDING, OperationStatus.READY])
def test_start_verbs_refuse_draft_and_on_hold_work_orders_and_write_nothing(
    client, db_session, floor, verb, wo_status, op_status
):
    wc, operator, supervisor = floor
    wo, op = make_wo_with_operation(db_session, work_center=wc, wo_status=wo_status, op_status=op_status)
    before = snapshot(db_session, wo, op)

    resp = run_verb(client, verb, op, operator=operator, supervisor=supervisor)

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == REASON_BY_STATUS[wo_status]
    assert_no_pending_writes(db_session)
    assert snapshot(db_session, wo, op) == before
    assert time_entries(db_session, op.id) == []
    if verb in START_AUDIT_ACTION:
        assert start_audit_rows(db_session, verb, op.id) == []
    # Nothing at all was written about this operation or this work order.
    assert (
        db_session.query(AuditLog)
        .filter(
            ((AuditLog.resource_type == "work_order_operation") & (AuditLog.resource_id == op.id))
            | ((AuditLog.resource_type == "work_order") & (AuditLog.resource_id == wo.id))
        )
        .count()
        == 0
    )


@pytest.mark.parametrize("verb", VERBS)
@pytest.mark.parametrize("wo_status", [WorkOrderStatus.RELEASED, WorkOrderStatus.IN_PROGRESS])
def test_released_and_in_progress_work_orders_are_accepted_positive_control(client, db_session, floor, verb, wo_status):
    """The positive control for every zero-row assertion above: the SAME queries find the row."""
    wc, operator, supervisor = floor
    wo, op = make_wo_with_operation(db_session, work_center=wc, wo_status=wo_status, op_status=OperationStatus.READY)

    resp = run_verb(client, verb, op, operator=operator, supervisor=supervisor)

    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    row = db_session.query(WorkOrderOperation).filter(WorkOrderOperation.id == op.id).one()
    assert row.status == OperationStatus.IN_PROGRESS
    assert db_session.query(WorkOrder).filter(WorkOrder.id == wo.id).one().status == WorkOrderStatus.IN_PROGRESS
    if verb in START_AUDIT_ACTION:
        assert len(start_audit_rows(db_session, verb, op.id)) == 1
    if verb in ("clock_in", "shop_start"):
        assert len(time_entries(db_session, op.id)) == 1


def test_shop_start_no_longer_lifts_a_draft_work_order_to_in_progress(client, db_session, floor):
    """The live Release bypass: PUT start used to move DRAFT straight to IN_PROGRESS."""
    wc, operator, supervisor = floor
    wo, op = make_wo_with_operation(db_session, work_center=wc, wo_status=WorkOrderStatus.DRAFT)

    resp = run_verb(client, "shop_start", op, operator=operator, supervisor=supervisor)

    assert resp.status_code == 409
    db_session.expire_all()
    row = db_session.query(WorkOrder).filter(WorkOrder.id == wo.id).one()
    assert row.status == WorkOrderStatus.DRAFT
    assert row.actual_start is None and row.released_at is None


# =========================================================================== 2. ordering


def _two_op_routing(db_session, wc_a, wc_b, wo_status):
    """OP10 READY at work center A, OP20 PENDING at work center B: OP20 is predecessor-blocked."""
    wo, op10 = make_wo_with_operation(db_session, work_center=wc_a, wo_status=wo_status)
    op20 = WorkOrderOperation(
        work_order_id=wo.id,
        work_center_id=wc_b.id,
        sequence=20,
        operation_number="OP20",
        name="Paint",
        status=OperationStatus.PENDING,
        company_id=wo.company_id,
    )
    db_session.add(op20)
    db_session.commit()
    db_session.refresh(op20)
    return wo, op10, op20


@pytest.mark.parametrize("verb", VERBS)
def test_draft_outranks_the_predecessor_gate(client, db_session, floor, verb):
    wc, operator, supervisor = floor
    wc_b = make_work_center(db_session)
    _, _, op20 = _two_op_routing(db_session, wc, wc_b, WorkOrderStatus.DRAFT)

    resp = run_verb(client, verb, op20, operator=operator, supervisor=supervisor)

    assert resp.status_code == 409
    assert resp.json()["detail"] == MSG_WORK_ORDER_NOT_RELEASED


@pytest.mark.parametrize("verb", VERBS)
def test_released_predecessor_blocked_keeps_its_400(client, db_session, floor, verb):
    wc, operator, supervisor = floor
    wc_b = make_work_center(db_session)
    _, _, op20 = _two_op_routing(db_session, wc, wc_b, WorkOrderStatus.RELEASED)

    resp = run_verb(client, verb, op20, operator=operator, supervisor=supervisor)

    assert resp.status_code == 400
    assert resp.json()["detail"] == MSG_PREDECESSORS_INCOMPLETE


def test_clock_in_on_a_cancelled_work_order_is_a_409_with_the_terminal_text(client, db_session, floor):
    wc, operator, supervisor = floor
    wo, op = make_wo_with_operation(db_session, work_center=wc, wo_status=WorkOrderStatus.CANCELLED)

    resp = run_verb(client, "clock_in", op, operator=operator, supervisor=supervisor)

    assert resp.status_code == 409
    assert resp.json()["detail"] == "cannot start operation: work order is cancelled"
    assert time_entries(db_session, op.id) == []


@pytest.mark.parametrize("verb", VERBS + ("wo_start",))
def test_soft_deleted_released_work_order_is_a_404(client, db_session, floor, verb):
    wc, operator, supervisor = floor
    wo, op = make_wo_with_operation(db_session, work_center=wc, wo_status=WorkOrderStatus.RELEASED)
    wo.soft_delete(supervisor.id)
    db_session.commit()

    if verb == "wo_start":
        resp = client.post(f"/api/v1/work-orders/{wo.id}/start", headers=user_headers(supervisor))
    else:
        resp = run_verb(client, verb, op, operator=operator, supervisor=supervisor)

    assert resp.status_code == 404, resp.text
    assert time_entries(db_session, op.id) == []
    db_session.expire_all()
    assert db_session.query(WorkOrderOperation).filter(WorkOrderOperation.id == op.id).one().actual_start is None


# =========================================================================== 3. op-level hold


@pytest.mark.parametrize("verb", ["shop_start", "office_start"])
def test_start_verbs_refuse_an_operation_on_hold_instead_of_lifting_it(client, db_session, floor, verb):
    wc, operator, supervisor = floor
    wo, op = make_wo_with_operation(
        db_session, work_center=wc, wo_status=WorkOrderStatus.RELEASED, op_status=OperationStatus.ON_HOLD
    )
    blocker = WorkOrderBlocker(
        work_order_id=wo.id,
        operation_id=op.id,
        title="Material short",
        status=WorkOrderBlockerStatus.OPEN.value,
        company_id=wo.company_id,
    )
    db_session.add(blocker)
    db_session.commit()
    before = snapshot(db_session, wo, op)

    resp = run_verb(client, verb, op, operator=operator, supervisor=supervisor)

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == MSG_OPERATION_ON_HOLD_NO_START
    assert_no_pending_writes(db_session)
    assert snapshot(db_session, wo, op) == before
    assert before[0] == OperationStatus.ON_HOLD
    assert db_session.query(WorkOrderBlocker).filter(WorkOrderBlocker.id == blocker.id).one().status == "open"
    assert time_entries(db_session, op.id) == []


def test_clock_in_on_an_operation_hold_keeps_its_400(client, db_session, floor):
    wc, operator, supervisor = floor
    _, op = make_wo_with_operation(
        db_session, work_center=wc, wo_status=WorkOrderStatus.RELEASED, op_status=OperationStatus.ON_HOLD
    )
    resp = run_verb(client, "clock_in", op, operator=operator, supervisor=supervisor)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Operation is not ready to start"


# =========================================================================== 4. no exemptions


def test_badge_token_clock_in_on_a_held_work_order_is_a_409_not_a_400(client, db_session, floor):
    wc, operator, _ = floor
    wo, op = make_wo_with_operation(db_session, work_center=wc, wo_status=WorkOrderStatus.ON_HOLD)

    resp = client.post(
        CLOCK_IN,
        json={"work_order_id": wo.id, "operation_id": op.id, "work_center_id": wc.id, "entry_type": "run"},
        headers=badge_headers(operator),
    )

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == MSG_WORK_ORDER_ON_HOLD
    assert time_entries(db_session, op.id) == []


def _backfill_audit_rows(db: Session) -> list[AuditLog]:
    return db.query(AuditLog).filter(AuditLog.resource_type == "time_entry", AuditLog.action == "CREATE").all()


def test_paper_back_entry_on_a_held_work_order_is_refused_like_any_clock_in(client, db_session, floor):
    wc, _, supervisor = floor
    held_wo, held_op = make_wo_with_operation(db_session, work_center=wc, wo_status=WorkOrderStatus.ON_HOLD)

    resp = client.post(
        CLOCK_IN,
        json={
            "work_order_id": held_wo.id,
            "operation_id": held_op.id,
            "work_center_id": wc.id,
            "entry_type": "run",
            "source": "backfill",
        },
        headers=user_headers(supervisor),
    )

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == MSG_WORK_ORDER_ON_HOLD
    assert time_entries(db_session, held_op.id) == []
    assert _backfill_audit_rows(db_session) == []

    # Positive control: the identical back-entry on a RELEASED job writes its audit row.
    wo, op = make_wo_with_operation(db_session, work_center=wc, wo_status=WorkOrderStatus.RELEASED)
    ok = client.post(
        CLOCK_IN,
        json={
            "work_order_id": wo.id,
            "operation_id": op.id,
            "work_center_id": wc.id,
            "entry_type": "run",
            "source": "backfill",
        },
        headers=user_headers(supervisor),
    )
    assert ok.status_code == 200, ok.text
    rows = _backfill_audit_rows(db_session)
    assert len(rows) == 1 and rows[0].extra_data["source"] == "backfill"


# =========================================================================== 5. office op start op-status


def test_office_start_refuses_a_complete_operation(client, db_session, floor):
    wc, _, supervisor = floor
    wo, op = make_wo_with_operation(
        db_session, work_center=wc, wo_status=WorkOrderStatus.IN_PROGRESS, op_status=OperationStatus.COMPLETE
    )
    finished_at = datetime(2026, 11, 1, 12, 0, 0)
    op.actual_start = finished_at
    op.completed_by = supervisor.id
    db_session.commit()

    resp = client.post(f"/api/v1/work-orders/operations/{op.id}/start", headers=user_headers(supervisor))

    assert resp.status_code == 400
    assert resp.json()["detail"] == MSG_ALREADY_COMPLETE
    assert_no_pending_writes(db_session)
    db_session.expire_all()
    row = db_session.query(WorkOrderOperation).filter(WorkOrderOperation.id == op.id).one()
    assert row.status == OperationStatus.COMPLETE
    assert row.actual_start == finished_at
    assert row.completed_by == supervisor.id


def test_office_start_refuses_an_operation_already_in_progress_without_restamping(client, db_session, floor):
    wc, operator_a, _ = floor
    supervisor_b = make_user(db_session, role=UserRole.SUPERVISOR)
    wo, op = make_wo_with_operation(
        db_session, work_center=wc, wo_status=WorkOrderStatus.IN_PROGRESS, op_status=OperationStatus.IN_PROGRESS
    )
    started_at = datetime(2026, 11, 2, 7, 30, 0)
    op.actual_start = started_at
    op.started_by = operator_a.id
    db_session.commit()

    resp = client.post(f"/api/v1/work-orders/operations/{op.id}/start", headers=user_headers(supervisor_b))

    assert resp.status_code == 400
    assert resp.json()["detail"] == MSG_ALREADY_IN_PROGRESS
    assert_no_pending_writes(db_session)
    db_session.expire_all()
    row = db_session.query(WorkOrderOperation).filter(WorkOrderOperation.id == op.id).one()
    assert row.started_by == operator_a.id
    assert row.actual_start == started_at


# =========================================================================== 6. read payloads


def _list_rows(client, user, wc_id) -> list[dict]:
    resp = client.get("/api/v1/shop-floor/operations", params={"work_center_id": wc_id}, headers=user_headers(user))
    assert resp.status_code == 200, resp.text
    return resp.json()["operations"]


@pytest.mark.parametrize(
    "wo_status,reason",
    [
        (WorkOrderStatus.DRAFT, MSG_WORK_ORDER_NOT_RELEASED),
        (WorkOrderStatus.ON_HOLD, MSG_WORK_ORDER_ON_HOLD),
        (WorkOrderStatus.RELEASED, None),
    ],
)
def test_shop_floor_operation_rows_carry_the_start_blocked_reason(client, db_session, floor, wo_status, reason):
    wc, operator, _ = floor
    wo, op = make_wo_with_operation(db_session, work_center=wc, wo_status=wo_status)

    [row] = _list_rows(client, operator, wc.id)

    assert row["id"] == op.id
    assert row["start_blocked_reason"] == reason
    assert row["work_order_status"] == wo_status.value
    assert row["blocked_by_previous_operations"] is False
    assert row["can_check_in"] is (reason is None)

    detail = client.get(f"/api/v1/shop-floor/operations/{op.id}", headers=user_headers(operator))
    assert detail.status_code == 200, detail.text
    body = detail.json()
    for payload in (body["operation"], body["all_operations"][0]):
        assert payload["start_blocked_reason"] == reason
        assert payload["work_order_status"] == wo_status.value
        assert payload["can_check_in"] is (reason is None)
        assert payload["blocked_by_previous_operations"] is False


@pytest.mark.parametrize("principal", ["user", "station"])
def test_kiosk_queue_and_held_rows_carry_the_reason_and_stay_listed(client, db_session, floor, principal):
    wc, operator, _ = floor
    wo, ready_op = make_wo_with_operation(db_session, work_center=wc, wo_status=WorkOrderStatus.ON_HOLD)
    held_op = WorkOrderOperation(
        work_order_id=wo.id,
        work_center_id=wc.id,
        sequence=20,
        operation_number="OP20",
        name="Grind",
        status=OperationStatus.ON_HOLD,
        company_id=wo.company_id,
    )
    db_session.add(held_op)
    db_session.commit()
    _, released_op = make_wo_with_operation(db_session, work_center=wc, wo_status=WorkOrderStatus.RELEASED)

    if principal == "user":
        headers = user_headers(operator)
    else:
        station = make_kiosk_station(db_session, work_center=wc)
        headers = bearer(kiosk_token_for(station))
    resp = client.get(queue_url(wc.id), headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    queue = {row["operation_id"]: row for row in body["queue"]}
    held = {row["operation_id"]: row for row in body["held"]}
    # Membership unchanged: the held job's READY op is still queued, its held op still held.
    assert ready_op.id in queue and released_op.id in queue
    assert held_op.id in held
    assert queue[ready_op.id]["start_blocked_reason"] == MSG_WORK_ORDER_ON_HOLD
    assert queue[ready_op.id]["work_order_status"] == "on_hold"
    assert queue[released_op.id]["start_blocked_reason"] is None
    assert queue[released_op.id]["work_order_status"] == "released"
    assert held[held_op.id]["start_blocked_reason"] == MSG_WORK_ORDER_ON_HOLD
    assert held[held_op.id]["work_order_status"] == "on_hold"
    # Same row shape on both lists.
    assert {"start_blocked_reason", "work_order_status"} <= set(held[held_op.id]) & set(queue[ready_op.id])
