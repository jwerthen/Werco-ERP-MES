"""``operation_action_gates.work_order_start_blocker`` and its use in ``complete_blockers``.

The helper is THE one copy of the work-order half of the start gate (ruling 2, 2026-11-05):
every start verb, clock-in, the READY auto-start branch of both complete verbs, the scanner
and the floor read payloads call it. Pinned here, without HTTP:

* every ``WorkOrderStatus`` maps to exactly one answer, for an enum-valued AND a plain
  string-valued status (rows read back through some paths carry the bare string);
* the terminal text is byte-identical to the f-string ``PUT /shop-floor/operations/{id}/start``
  raised before the helper existed (``test_completion_terminal_lock_batch11`` pins the
  HTTP side);
* ``complete_blockers`` reports the start refusal for a READY operation under a DRAFT or
  ON_HOLD job, never for an IN_PROGRESS one, and reports a terminal job exactly once.
"""

from types import SimpleNamespace

import pytest

from app.models.work_order import OperationStatus, WorkOrderStatus
from app.services.operation_action_gates import (
    MSG_WORK_ORDER_NOT_RELEASED,
    MSG_WORK_ORDER_ON_HOLD,
    complete_blockers,
    work_order_start_blocker,
)
from tests.api.kiosk_test_helpers import make_wo_with_operation, make_work_center

EXPECTED = {
    WorkOrderStatus.DRAFT: MSG_WORK_ORDER_NOT_RELEASED,
    WorkOrderStatus.RELEASED: None,
    WorkOrderStatus.IN_PROGRESS: None,
    WorkOrderStatus.ON_HOLD: MSG_WORK_ORDER_ON_HOLD,
    WorkOrderStatus.COMPLETE: "cannot start operation: work order is complete",
    WorkOrderStatus.CLOSED: "cannot start operation: work order is closed",
    WorkOrderStatus.CANCELLED: "cannot start operation: work order is cancelled",
}


@pytest.mark.unit
def test_every_status_is_covered():
    """A new WorkOrderStatus member must be decided here, not silently treated as startable."""
    assert set(EXPECTED) == set(WorkOrderStatus)


@pytest.mark.unit
@pytest.mark.parametrize("status_", list(WorkOrderStatus))
def test_enum_valued_status(status_):
    assert work_order_start_blocker(SimpleNamespace(status=status_)) == EXPECTED[status_]


@pytest.mark.unit
@pytest.mark.parametrize("status_", list(WorkOrderStatus))
def test_string_valued_status(status_):
    assert work_order_start_blocker(SimpleNamespace(status=status_.value)) == EXPECTED[status_]


@pytest.mark.unit
@pytest.mark.parametrize("status_", [WorkOrderStatus.COMPLETE, WorkOrderStatus.CLOSED, WorkOrderStatus.CANCELLED])
def test_terminal_text_is_byte_identical_to_the_old_inline_f_string(status_):
    work_order = SimpleNamespace(status=status_)
    legacy = f"cannot start operation: work order is {work_order.status.value}"
    assert work_order_start_blocker(work_order) == legacy


@pytest.mark.unit
def test_no_work_order_is_the_resolvers_404_not_a_refusal():
    assert work_order_start_blocker(None) is None


# --------------------------------------------------------------------------- complete_blockers


@pytest.mark.requires_db
@pytest.mark.parametrize("wo_status", [WorkOrderStatus.DRAFT, WorkOrderStatus.ON_HOLD])
def test_complete_blockers_reports_the_start_refusal_for_a_ready_operation(db_session, wo_status):
    wo, op = make_wo_with_operation(db_session, work_center=make_work_center(db_session), wo_status=wo_status)
    reasons = complete_blockers(db_session, op, wo, wo.company_id)
    assert EXPECTED[wo_status] in reasons


@pytest.mark.requires_db
def test_complete_blockers_positive_control_released_ready_operation_has_no_start_refusal(db_session):
    wo, op = make_wo_with_operation(
        db_session, work_center=make_work_center(db_session), wo_status=WorkOrderStatus.RELEASED
    )
    reasons = complete_blockers(db_session, op, wo, wo.company_id)
    assert MSG_WORK_ORDER_NOT_RELEASED not in reasons and MSG_WORK_ORDER_ON_HOLD not in reasons


@pytest.mark.requires_db
@pytest.mark.parametrize("wo_status", [WorkOrderStatus.DRAFT, WorkOrderStatus.ON_HOLD])
def test_complete_blockers_does_not_gate_an_in_progress_operation(db_session, wo_status):
    wo, op = make_wo_with_operation(
        db_session,
        work_center=make_work_center(db_session),
        wo_status=wo_status,
        op_status=OperationStatus.IN_PROGRESS,
    )
    reasons = complete_blockers(db_session, op, wo, wo.company_id)
    assert EXPECTED[wo_status] not in reasons


@pytest.mark.requires_db
def test_complete_blockers_reports_a_terminal_work_order_once(db_session):
    wo, op = make_wo_with_operation(
        db_session, work_center=make_work_center(db_session), wo_status=WorkOrderStatus.CANCELLED
    )
    reasons = complete_blockers(db_session, op, wo, wo.company_id)
    terminal = [reason for reason in reasons if "work order is cancelled" in reason]
    assert terminal == ["cannot complete operation: work order is cancelled"]
