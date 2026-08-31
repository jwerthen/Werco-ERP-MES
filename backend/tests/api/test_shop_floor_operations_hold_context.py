"""``GET /shop-floor/operations`` says WHY an ON_HOLD row is held.

THE DEFECT. ``ShopFloorSimple`` is the desk page that carries the only Clear Hold
control on the shop-floor list, and this endpoint feeds it. The row it served for
a held operation carried the status and nothing else -- no category, no note, no
"held by" -- so an operator lifted a stop whose reason had never been on screen.
The kiosk has shown all of it since the ``held`` list shipped; the desk had not.

The block comes from ``operation_hold_view.hold_contexts_for_operations`` -- the
SAME batched, pure-read view the kiosk's held list uses -- so the desk and the
floor cannot tell two different stories about one hold, and it is assembled with
no writes, no audit rows and no events (this is a 30-second poll, and a poll is
not an actor).

What this pins:

* ON_HOLD rows carry ``hold``; EVERY OTHER ROW IS BYTE-IDENTICAL to what it was,
  the key absent rather than null, so no existing consumer changes behavior;
* reason (``blocker``) and attribution (``held_by_name`` / ``held_at``) are
  INDEPENDENT -- a BARE hold, the accidental fat-finger case, files no blocker
  and must still name who pressed it;
* FREE TEXT rides this response. ``_hold_blocker_payload`` withholds title/note
  from a crew-STATION principal, and this endpoint has none: it depends on
  ``get_current_user``, so every caller is an identified session. Withholding the
  reason on the one screen built to act on it would leave the bug unfixed;
* a RESOLVED blocker is not the current reason (the resolve flow is what
  auto-resumes an operation, so a closed blocker on a still-held op is stale
  narrative);
* the read stays tenant-scoped.
"""

from datetime import datetime, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.operational_event import OperationalEvent
from app.models.work_order import OperationStatus, WorkOrderOperation
from app.models.work_order_blocker import (
    WorkOrderBlocker,
    WorkOrderBlockerCategory,
    WorkOrderBlockerSeverity,
    WorkOrderBlockerStatus,
)
from tests.api.kiosk_test_helpers import (
    COMPANY_A,
    COMPANY_B,
    make_user,
    make_wo_with_operation,
    make_work_center,
    user_headers,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

OPERATIONS_URL = "/api/v1/shop-floor/operations"


def _blocker(
    db: Session,
    *,
    operation: WorkOrderOperation,
    reported_by: int,
    company_id: int = COMPANY_A,
    category: str = WorkOrderBlockerCategory.MATERIAL_MISSING.value,
    severity: str = WorkOrderBlockerSeverity.HIGH.value,
    blocker_status: str = WorkOrderBlockerStatus.OPEN.value,
    note: str = "Wrong sheet on the rack",
    title: str = "Material Missing: nest 3",
    reported_at: datetime = None,
) -> WorkOrderBlocker:
    blocker = WorkOrderBlocker(
        company_id=company_id,
        work_order_id=operation.work_order_id,
        operation_id=operation.id,
        category=category,
        severity=severity,
        status=blocker_status,
        title=title,
        note=note,
        reported_by=reported_by,
        reported_at=reported_at or datetime.utcnow(),
    )
    db.add(blocker)
    db.commit()
    db.refresh(blocker)
    return blocker


def _hold_event(
    db: Session,
    *,
    operation: WorkOrderOperation,
    user_id: int,
    company_id: int = COMPANY_A,
    occurred_at: datetime = None,
) -> OperationalEvent:
    """The record a BARE hold leaves (no note, category OTHER) -- the accident case."""
    event = OperationalEvent(
        company_id=company_id,
        event_type="operation_hold",
        source_module="shop_floor",
        entity_type="work_order_operation",
        entity_id=operation.id,
        work_order_id=operation.work_order_id,
        operation_id=operation.id,
        user_id=user_id,
        severity="medium",
        occurred_at=occurred_at or datetime.utcnow(),
        event_payload={},
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def _rows(client: TestClient, user, **params) -> dict:
    response = client.get(OPERATIONS_URL, headers=user_headers(user), params=params)
    assert response.status_code == status.HTTP_200_OK
    return {row["id"]: row for row in response.json()["operations"]}


def test_held_row_carries_the_reason_who_and_when(client: TestClient, db_session: Session):
    wc = make_work_center(db_session, company_id=COMPANY_A)
    user = make_user(db_session, company_id=COMPANY_A)
    _, held_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.ON_HOLD)
    _blocker(db_session, operation=held_op, reported_by=user.id)

    row = _rows(client, user, work_center_id=wc.id)[held_op.id]

    hold = row["hold"]
    assert hold["blocker"]["category"] == WorkOrderBlockerCategory.MATERIAL_MISSING.value
    assert hold["blocker"]["severity"] == WorkOrderBlockerSeverity.HIGH.value
    assert hold["blocker"]["status"] == WorkOrderBlockerStatus.OPEN.value
    assert hold["held_by_name"]
    assert hold["held_at"] is not None


def test_free_text_rides_this_response(client: TestClient, db_session: Session):
    """Every caller here is an identified session -- there is no station principal.

    Withholding it (the crew-station rule) on the one screen built to ACT on the
    hold would leave the reported defect unfixed: the owner still could not find
    out why the nest was stopped.
    """
    wc = make_work_center(db_session, company_id=COMPANY_A)
    user = make_user(db_session, company_id=COMPANY_A)
    _, held_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.ON_HOLD)
    _blocker(db_session, operation=held_op, reported_by=user.id, note="4140 plate not at the saw")

    blocker = _rows(client, user, work_center_id=wc.id)[held_op.id]["hold"]["blocker"]

    assert blocker["note"] == "4140 plate not at the saw"
    assert blocker["title"] == "Material Missing: nest 3"
    assert blocker["free_text_withheld"] is False


def test_bare_hold_names_who_pressed_it_with_no_blocker(client: TestClient, db_session: Session):
    """Reason and attribution are INDEPENDENT.

    A bare hold (no note, category OTHER) files NO blocker -- exactly the
    accidental case this whole feature exists for. Gating the attribution on the
    blocker would render that case anonymous AND reasonless, the one case that
    most needs to read as an accident.
    """
    wc = make_work_center(db_session, company_id=COMPANY_A)
    user = make_user(db_session, company_id=COMPANY_A)
    _, held_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.ON_HOLD)
    _hold_event(db_session, operation=held_op, user_id=user.id)

    hold = _rows(client, user, work_center_id=wc.id)[held_op.id]["hold"]

    assert hold["blocker"] is None
    assert hold["held_by_user_id"] == user.id
    assert hold["held_by_name"]


def test_hold_with_no_record_at_all_is_a_real_state_not_an_error(client: TestClient, db_session: Session):
    """A hold placed before either record was written. All-null, never a 500."""
    wc = make_work_center(db_session, company_id=COMPANY_A)
    user = make_user(db_session, company_id=COMPANY_A)
    _, held_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.ON_HOLD)

    hold = _rows(client, user, work_center_id=wc.id)[held_op.id]["hold"]

    assert hold == {"held_at": None, "held_by_user_id": None, "held_by_name": None, "blocker": None}


def test_a_resolved_blocker_is_not_the_current_reason(client: TestClient, db_session: Session):
    """The resolve/dismiss flow is what auto-resumes an operation.

    So a CLOSED blocker on a still-held operation is stale narrative, not the
    reason in force -- same OPEN/ACKNOWLEDGED pair the resume endpoint warns on.
    """
    wc = make_work_center(db_session, company_id=COMPANY_A)
    user = make_user(db_session, company_id=COMPANY_A)
    _, held_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.ON_HOLD)
    _blocker(
        db_session,
        operation=held_op,
        reported_by=user.id,
        blocker_status=WorkOrderBlockerStatus.RESOLVED.value,
    )

    assert _rows(client, user, work_center_id=wc.id)[held_op.id]["hold"]["blocker"] is None


def test_newest_record_wins_between_the_blocker_and_the_event(client: TestClient, db_session: Session):
    """Both records can exist and disagree; the NEWER one describes the hold in force."""
    wc = make_work_center(db_session, company_id=COMPANY_A)
    reporter = make_user(db_session, company_id=COMPANY_A)
    holder = make_user(db_session, company_id=COMPANY_A)
    _, held_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.ON_HOLD)
    _blocker(
        db_session,
        operation=held_op,
        reported_by=reporter.id,
        reported_at=datetime.utcnow() - timedelta(days=3),
    )
    _hold_event(db_session, operation=held_op, user_id=holder.id, occurred_at=datetime.utcnow())

    hold = _rows(client, user=reporter, work_center_id=wc.id)[held_op.id]["hold"]

    # Attribution follows the newer record...
    assert hold["held_by_user_id"] == holder.id
    # ...while the blocker is still the reason TEXT the operator has to read.
    assert hold["blocker"] is not None


def test_unheld_rows_are_byte_identical_to_before(client: TestClient, db_session: Session):
    """The key is ABSENT on a queued row, not sent as null.

    Additive-only: a consumer that never asked about holds sees exactly the
    payload it always saw.
    """
    wc = make_work_center(db_session, company_id=COMPANY_A)
    user = make_user(db_session, company_id=COMPANY_A)
    _, ready_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.READY)
    _, progress_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.IN_PROGRESS)

    rows = _rows(client, user, work_center_id=wc.id)

    assert "hold" not in rows[ready_op.id]
    assert "hold" not in rows[progress_op.id]


def test_hold_context_is_tenant_scoped(client: TestClient, db_session: Session):
    """A blocker filed under another company can never populate this block."""
    wc = make_work_center(db_session, company_id=COMPANY_A)
    user = make_user(db_session, company_id=COMPANY_A)
    other_user = make_user(db_session, company_id=COMPANY_B)
    _, held_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.ON_HOLD)
    # Same operation id, wrong tenant on the blocker row.
    _blocker(db_session, operation=held_op, reported_by=other_user.id, company_id=COMPANY_B)

    hold = _rows(client, user, work_center_id=wc.id)[held_op.id]["hold"]

    assert hold["blocker"] is None
