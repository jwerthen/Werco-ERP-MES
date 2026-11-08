"""``PUT /work-orders/{id}`` refuses DRAFT -> ON_HOLD (ruling 2).

That PUT is the only writer of a work-order ON_HOLD. Refusing it on a draft means every
hold placed from now on is on RELEASED work, which is what lets the resume verb decide
where a held job goes back to without guessing -- and closes the "MCP-imported DRAFT laser
job -> on_hold -> resume -> RELEASED" chain that would have released work with no Release.
Holding released / in-progress work, re-sending on_hold, and ON_HOLD -> DRAFT stay allowed.

Any move INTO DRAFT through this PUT takes the job off the floor: every un-worked READY
operation (laser nests included) returns to PENDING with one REQUIRED audited status change,
the resume-to-DRAFT recipe -- pinned on the operation row AND on its absence from the
work-center queue, not only on the work order's status.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.user import UserRole
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus, WorkOrderType
from app.services.audit_service import AuditService
from app.services.operation_action_gates import MSG_DRAFT_CANNOT_BE_HELD, MSG_RETURN_TO_DRAFT_AUDIT_UNAVAILABLE
from tests.api.kiosk_test_helpers import make_user, make_wo_with_operation, make_work_center, queue_url, user_headers

pytestmark = [pytest.mark.api, pytest.mark.requires_db]


def put_status(client, wo: WorkOrder, user, status_value: str):
    return client.put(
        f"/api/v1/work-orders/{wo.id}",
        json={"version": wo.version, "status": status_value},
        headers=user_headers(user),
    )


def reread(db: Session, wo_id: int) -> WorkOrder:
    db.expire_all()
    return db.query(WorkOrder).filter(WorkOrder.id == wo_id).one()


def update_rows(db: Session, wo_id: int) -> list[AuditLog]:
    """The PUT's own evidence: UPDATE rows on THIS work order (keyed, never a global count)."""
    return (
        db.query(AuditLog)
        .filter(AuditLog.resource_type == "work_order", AuditLog.resource_id == wo_id, AuditLog.action == "UPDATE")
        .all()
    )


@pytest.fixture
def supervisor(db_session):
    return make_user(db_session, role=UserRole.SUPERVISOR)


def test_holding_a_draft_is_refused_before_any_write(client, db_session, supervisor):
    wc = make_work_center(db_session)
    wo, op = make_wo_with_operation(db_session, work_center=wc, wo_status=WorkOrderStatus.DRAFT)
    version, priority = wo.version, wo.priority

    # A second field rides along: "before the first setattr" means it must not land either.
    resp = client.put(
        f"/api/v1/work-orders/{wo.id}",
        json={"version": wo.version, "status": "on_hold", "priority": priority + 1},
        headers=user_headers(supervisor),
    )

    assert resp.status_code == 409
    assert resp.json()["detail"] == MSG_DRAFT_CANNOT_BE_HELD
    # The test session is the app's session and autoflush is off, so a refusal raised AFTER
    # the setattr loop would leave dirty, unflushed state that reread()'s expire_all()
    # silently discards. Assert there is none BEFORE re-reading.
    assert not db_session.dirty and not db_session.new
    row = reread(db_session, wo.id)
    assert row.status == WorkOrderStatus.DRAFT and row.version == version and row.priority == priority
    assert db_session.query(WorkOrderOperation).filter(WorkOrderOperation.id == op.id).one().status == (
        OperationStatus.READY
    )
    # Keyed; the positive control below runs the identical query and finds exactly one row.
    assert update_rows(db_session, wo.id) == []


@pytest.mark.parametrize("status_", [WorkOrderStatus.RELEASED, WorkOrderStatus.IN_PROGRESS])
def test_holding_released_or_in_progress_work_is_allowed(client, db_session, supervisor, status_):
    wc = make_work_center(db_session)
    wo, _ = make_wo_with_operation(db_session, work_center=wc, wo_status=status_)

    resp = put_status(client, wo, supervisor, "on_hold")

    assert resp.status_code == 200, resp.text
    assert reread(db_session, wo.id).status == WorkOrderStatus.ON_HOLD
    [row] = update_rows(db_session, wo.id)
    assert row.old_values["status"] == status_.value and row.new_values["status"] == "on_hold"


def op_status_rows(db: Session, op_id: int) -> list[AuditLog]:
    return (
        db.query(AuditLog)
        .filter(
            AuditLog.resource_type == "work_order_operation",
            AuditLog.resource_id == op_id,
            AuditLog.action == "STATUS_CHANGE",
        )
        .all()
    )


def op_row(db: Session, op_id: int) -> WorkOrderOperation:
    db.expire_all()
    return db.query(WorkOrderOperation).filter(WorkOrderOperation.id == op_id).one()


def queued_operation_ids(client, user, wc_id: int) -> set[int]:
    resp = client.get(queue_url(wc_id), headers=user_headers(user))
    assert resp.status_code == 200, resp.text
    return {row["operation_id"] for row in resp.json()["queue"]}


def add_op(db: Session, wo: WorkOrder, wc, *, sequence: int, status: OperationStatus) -> WorkOrderOperation:
    op = WorkOrderOperation(
        work_order_id=wo.id,
        work_center_id=wc.id,
        sequence=sequence,
        operation_number=f"OP{sequence}",
        name=f"Step {sequence}",
        status=status,
        company_id=wo.company_id,
    )
    db.add(op)
    db.commit()
    db.refresh(op)
    return op


def test_resending_on_hold_and_returning_a_held_job_to_draft_stay_allowed(client, db_session, supervisor):
    wc = make_work_center(db_session)
    wo, op = make_wo_with_operation(db_session, work_center=wc, wo_status=WorkOrderStatus.ON_HOLD)
    assert op.id in queued_operation_ids(client, supervisor, wc.id), "precondition: the READY card is listed"

    again = put_status(client, wo, supervisor, "on_hold")
    assert again.status_code == 200, again.text

    wo = reread(db_session, wo.id)
    to_draft = put_status(client, wo, supervisor, "draft")
    assert to_draft.status_code == 200, to_draft.text
    assert reread(db_session, wo.id).status == WorkOrderStatus.DRAFT
    # Off the floor, not just relabelled: the READY card is PENDING and gone from the queue.
    assert op_row(db_session, op.id).status == OperationStatus.PENDING
    assert op.id not in queued_operation_ids(client, supervisor, wc.id)
    [row] = op_status_rows(db_session, op.id)
    assert row.old_values == {"status": "ready"} and row.new_values == {"status": "pending"}
    assert row.extra_data["transition"] == "work_order_returned_to_draft"
    assert row.extra_data["operation_number"] == op.operation_number


@pytest.mark.parametrize("status_", [WorkOrderStatus.RELEASED, WorkOrderStatus.IN_PROGRESS])
def test_putting_released_work_back_to_draft_takes_its_unworked_cards_off_the_floor(
    client, db_session, supervisor, status_
):
    wc = make_work_center(db_session)
    operator = make_user(db_session, role=UserRole.OPERATOR)
    wo, nest1 = make_wo_with_operation(db_session, work_center=wc, wo_status=status_, sequential_operations=False)
    wo.work_order_type = WorkOrderType.LASER_CUTTING.value
    db_session.commit()
    nest2 = add_op(db_session, wo, wc, sequence=20, status=OperationStatus.READY)
    # A READY nest somebody has already logged labor against is worked: never demoted.
    worked = add_op(db_session, wo, wc, sequence=30, status=OperationStatus.READY)
    db_session.add(
        TimeEntry(
            user_id=operator.id,
            work_order_id=wo.id,
            operation_id=worked.id,
            work_center_id=wc.id,
            entry_type=TimeEntryType.RUN,
            clock_in=datetime.utcnow() - timedelta(hours=2),
            clock_out=datetime.utcnow() - timedelta(hours=1),
            company_id=wo.company_id,
        )
    )
    db_session.commit()
    wo = reread(db_session, wo.id)

    resp = put_status(client, wo, supervisor, "draft")

    assert resp.status_code == 200, resp.text
    assert reread(db_session, wo.id).status == WorkOrderStatus.DRAFT
    assert op_row(db_session, nest1.id).status == OperationStatus.PENDING, "laser nests are not exempt"
    assert op_row(db_session, nest2.id).status == OperationStatus.PENDING
    assert op_row(db_session, worked.id).status == OperationStatus.READY
    assert {o["id"]: o["status"] for o in resp.json()["operations"]} == {
        nest1.id: "pending",
        nest2.id: "pending",
        worked.id: "ready",
    }, "the response is what the database says"
    assert queued_operation_ids(client, supervisor, wc.id) & {nest1.id, nest2.id} == set()
    assert len(op_status_rows(db_session, nest1.id)) == 1 and len(op_status_rows(db_session, nest2.id)) == 1
    assert op_status_rows(db_session, worked.id) == []
    [update] = update_rows(db_session, wo.id)
    assert update.new_values["status"] == "draft"


def test_resending_draft_on_a_draft_is_not_a_move_and_demotes_nothing(client, db_session, supervisor):
    wc = make_work_center(db_session)
    # A hand-set READY on a draft is the legacy shape; a re-sent draft must not touch it.
    wo, op = make_wo_with_operation(db_session, work_center=wc, wo_status=WorkOrderStatus.DRAFT)

    resp = put_status(client, wo, supervisor, "draft")

    assert resp.status_code == 200, resp.text
    assert op_row(db_session, op.id).status == OperationStatus.READY
    assert op_status_rows(db_session, op.id) == []


def test_a_return_to_draft_whose_demotion_audit_fails_changes_nothing(client, db_session, supervisor, monkeypatch):
    """Fail-closed, like the resume: the demotion rows are REQUIRED evidence, so the whole
    PUT -- the work order's own status included -- rolls back with a 503."""
    wc = make_work_center(db_session)
    wo, op = make_wo_with_operation(db_session, work_center=wc, wo_status=WorkOrderStatus.RELEASED)
    version = wo.version
    real_log = AuditService.log

    def flaky_log(self, *args, **kwargs):
        if kwargs.get("resource_type") == "work_order_operation":
            return None  # the demotion row cannot be saved
        return real_log(self, *args, **kwargs)

    monkeypatch.setattr(AuditService, "log", flaky_log)

    resp = put_status(client, wo, supervisor, "draft")

    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"] == MSG_RETURN_TO_DRAFT_AUDIT_UNAVAILABLE
    row = reread(db_session, wo.id)
    assert row.status == WorkOrderStatus.RELEASED and row.version == version
    assert op_row(db_session, op.id).status == OperationStatus.READY
    assert update_rows(db_session, wo.id) == []
