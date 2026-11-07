"""``PUT /work-orders/{id}`` refuses DRAFT -> ON_HOLD (ruling 2).

That PUT is the only writer of a work-order ON_HOLD. Refusing it on a draft means every
hold placed from now on is on RELEASED work, which is what lets the resume verb decide
where a held job goes back to without guessing -- and closes the "MCP-imported DRAFT laser
job -> on_hold -> resume -> RELEASED" chain that would have released work with no Release.
Holding released / in-progress work, re-sending on_hold, and ON_HOLD -> DRAFT stay allowed.
"""

import pytest
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.user import UserRole
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.services.operation_action_gates import MSG_DRAFT_CANNOT_BE_HELD
from tests.api.kiosk_test_helpers import make_user, make_wo_with_operation, make_work_center, user_headers

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


def test_resending_on_hold_and_returning_a_held_job_to_draft_stay_allowed(client, db_session, supervisor):
    wc = make_work_center(db_session)
    wo, _ = make_wo_with_operation(db_session, work_center=wc, wo_status=WorkOrderStatus.ON_HOLD)

    again = put_status(client, wo, supervisor, "on_hold")
    assert again.status_code == 200, again.text

    wo = reread(db_session, wo.id)
    to_draft = put_status(client, wo, supervisor, "draft")
    assert to_draft.status_code == 200, to_draft.text
    assert reread(db_session, wo.id).status == WorkOrderStatus.DRAFT
