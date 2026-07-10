"""Structured scrap-code behavior on the three scrap write paths (Lean Phase 1).

``scrap_reason_code_id`` rides POST /shop-floor/clock-out/{id}, POST
/shop-floor/operations/{id}/production, and POST /work-orders/{id}/complete.
Locked here, per path:
  * persistence: the code lands on the TimeEntry AND the operation (and the WO
    for /complete); scrap > 0 passes with code-only, text-only, or both,
  * validation BEFORE mutation: unknown or cross-tenant id -> 404 (a foreign id
    is indistinguishable from a missing one), inactive -> 422, nothing persisted,
  * never-clear (entry/op): a later code-less write never nulls a recorded code;
    /complete's WO field instead follows its explicit-scrap-write-replaces-wholly
    semantics (documented in work_orders.py),
  * FPY plumbing: produced quantity booked on a REWORK entry increments
    ``operation.quantity_reworked`` (clock-out and in-shift report); RUN does not.
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.user import UserRole
from app.models.work_order import WorkOrder, WorkOrderOperation, WorkOrderStatus
from tests.lean_phase1_helpers import (
    COMPANY_B,
    headers_for,
    make_entry,
    make_scrap_code,
    make_user,
    make_wo_with_op,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]


def _clock_out_url(entry_id: int) -> str:
    return f"/api/v1/shop-floor/clock-out/{entry_id}"


def _production_url(op_id: int) -> str:
    return f"/api/v1/shop-floor/operations/{op_id}/production"


def _complete_url(wo_id: int) -> str:
    return f"/api/v1/work-orders/{wo_id}/complete"


def _open_run_entry(db, user, wo, op, wc, entry_type=TimeEntryType.RUN) -> TimeEntry:
    return make_entry(db, user, wo, op, wc, entry_type=entry_type, open_entry=True)


# ===========================================================================
# Clock-out
# ===========================================================================


def test_clock_out_code_only_persists_to_entry_and_operation(client: TestClient, db_session: Session):
    """Scrap with a CODE and no free text satisfies the reason rule and lands on
    both the TimeEntry and the operation."""
    operator = make_user(db_session, role=UserRole.OPERATOR)
    code = make_scrap_code(db_session, code="OT")
    wo, op, wc = make_wo_with_op(db_session)
    entry = _open_run_entry(db_session, operator, wo, op, wc)

    resp = client.post(
        _clock_out_url(entry.id),
        json={"quantity_produced": 5, "quantity_scrapped": 2, "scrap_reason_code_id": code.id},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["scrap_reason_code_id"] == code.id

    db_session.expire_all()
    entry = db_session.get(TimeEntry, entry.id)
    assert entry.clock_out is not None
    assert entry.quantity_scrapped == 2
    assert entry.scrap_reason_code_id == code.id
    assert entry.scrap_reason is None  # no free text sent, none invented

    op = db_session.get(WorkOrderOperation, op.id)
    assert op.quantity_scrapped == 2
    assert op.scrap_reason_code_id == code.id


def test_clock_out_text_only_still_passes_and_code_stays_null(client: TestClient, db_session: Session):
    operator = make_user(db_session, role=UserRole.OPERATOR)
    wo, op, wc = make_wo_with_op(db_session)
    entry = _open_run_entry(db_session, operator, wo, op, wc)

    resp = client.post(
        _clock_out_url(entry.id),
        json={"quantity_produced": 5, "quantity_scrapped": 1, "scrap_reason": "Porosity in weld"},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    entry = db_session.get(TimeEntry, entry.id)
    assert entry.scrap_reason == "Porosity in weld"
    assert entry.scrap_reason_code_id is None
    assert db_session.get(WorkOrderOperation, op.id).scrap_reason_code_id is None


def test_clock_out_code_and_text_both_persist(client: TestClient, db_session: Session):
    operator = make_user(db_session, role=UserRole.OPERATOR)
    code = make_scrap_code(db_session, code="MAT")
    wo, op, wc = make_wo_with_op(db_session)
    entry = _open_run_entry(db_session, operator, wo, op, wc)

    resp = client.post(
        _clock_out_url(entry.id),
        json={
            "quantity_produced": 4,
            "quantity_scrapped": 1,
            "scrap_reason": "inclusion on face",
            "scrap_reason_code_id": code.id,
        },
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    entry = db_session.get(TimeEntry, entry.id)
    assert entry.scrap_reason == "inclusion on face"
    assert entry.scrap_reason_code_id == code.id


@pytest.mark.parametrize(
    "code_kind,expected_status",
    [
        ("unknown", status.HTTP_404_NOT_FOUND),
        ("cross_tenant", status.HTTP_404_NOT_FOUND),
        ("inactive", status.HTTP_422_UNPROCESSABLE_ENTITY),
    ],
)
def test_clock_out_invalid_code_rejected_before_any_mutation(
    client: TestClient, db_session: Session, code_kind: str, expected_status: int
):
    """Unknown and cross-tenant ids are the same 404 (no tenant disclosure);
    inactive is 422. The entry must stay open and un-scrapped in all cases."""
    operator = make_user(db_session, role=UserRole.OPERATOR)
    wo, op, wc = make_wo_with_op(db_session)
    entry = _open_run_entry(db_session, operator, wo, op, wc)

    if code_kind == "unknown":
        code_id = 999_999
    elif code_kind == "cross_tenant":
        code_id = make_scrap_code(db_session, company_id=COMPANY_B, code="FRGN").id
    else:
        code_id = make_scrap_code(db_session, code="RETIRED", is_active=False).id

    resp = client.post(
        _clock_out_url(entry.id),
        json={"quantity_produced": 5, "quantity_scrapped": 2, "scrap_reason_code_id": code_id},
        headers=headers_for(operator),
    )
    assert resp.status_code == expected_status, resp.text

    # Discard any request-side pending state, then read committed truth.
    db_session.rollback()
    db_session.expire_all()
    entry = db_session.get(TimeEntry, entry.id)
    assert entry.clock_out is None, "a rejected clock-out must not close the entry"
    assert entry.quantity_scrapped == 0
    assert entry.scrap_reason_code_id is None
    assert db_session.get(WorkOrderOperation, op.id).quantity_scrapped == 0


def test_code_less_writes_never_clear_a_recorded_code(client: TestClient, db_session: Session):
    """Production report stamps code A on entry+op; later code-less writes (text
    reasons only) must NOT null it -- never-clear semantics on both rows."""
    operator = make_user(db_session, role=UserRole.OPERATOR)
    code = make_scrap_code(db_session, code="A1")
    wo, op, wc = make_wo_with_op(db_session)
    entry = _open_run_entry(db_session, operator, wo, op, wc)

    first = client.post(
        _production_url(op.id),
        json={"quantity_complete_delta": 2, "quantity_scrapped_delta": 1, "scrap_reason_code_id": code.id},
        headers=headers_for(operator),
    )
    assert first.status_code == status.HTTP_200_OK, first.text

    second = client.post(
        _production_url(op.id),
        json={"quantity_complete_delta": 1, "quantity_scrapped_delta": 1, "scrap_reason": "second lot"},
        headers=headers_for(operator),
    )
    assert second.status_code == status.HTTP_200_OK, second.text

    clock_out = client.post(
        _clock_out_url(entry.id),
        json={"quantity_produced": 1, "quantity_scrapped": 1, "scrap_reason": "end of shift"},
        headers=headers_for(operator),
    )
    assert clock_out.status_code == status.HTTP_200_OK, clock_out.text

    db_session.expire_all()
    assert db_session.get(TimeEntry, entry.id).scrap_reason_code_id == code.id
    assert db_session.get(WorkOrderOperation, op.id).scrap_reason_code_id == code.id


# ===========================================================================
# REWORK -> operation.quantity_reworked (FPY plumbing)
# ===========================================================================


def test_rework_clock_out_increments_operation_quantity_reworked(client: TestClient, db_session: Session):
    operator = make_user(db_session, role=UserRole.OPERATOR)
    wo, op, wc = make_wo_with_op(db_session)
    entry = _open_run_entry(db_session, operator, wo, op, wc, entry_type=TimeEntryType.REWORK)

    resp = client.post(
        _clock_out_url(entry.id),
        json={"quantity_produced": 3, "quantity_scrapped": 0},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    assert db_session.get(WorkOrderOperation, op.id).quantity_reworked == 3


def test_run_clock_out_does_not_touch_quantity_reworked(client: TestClient, db_session: Session):
    operator = make_user(db_session, role=UserRole.OPERATOR)
    wo, op, wc = make_wo_with_op(db_session)
    entry = _open_run_entry(db_session, operator, wo, op, wc, entry_type=TimeEntryType.RUN)

    resp = client.post(
        _clock_out_url(entry.id),
        json={"quantity_produced": 3, "quantity_scrapped": 0},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    assert (db_session.get(WorkOrderOperation, op.id).quantity_reworked or 0) == 0


def test_rework_production_report_increments_quantity_reworked(client: TestClient, db_session: Session):
    operator = make_user(db_session, role=UserRole.OPERATOR)
    wo, op, wc = make_wo_with_op(db_session)
    _open_run_entry(db_session, operator, wo, op, wc, entry_type=TimeEntryType.REWORK)

    resp = client.post(
        _production_url(op.id),
        json={"quantity_complete_delta": 2},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    assert db_session.get(WorkOrderOperation, op.id).quantity_reworked == 2


# ===========================================================================
# Production report (in-shift)
# ===========================================================================


def test_production_report_code_persists_to_entry_and_operation(client: TestClient, db_session: Session):
    operator = make_user(db_session, role=UserRole.OPERATOR)
    code = make_scrap_code(db_session, code="TOOL")
    wo, op, wc = make_wo_with_op(db_session)
    entry = _open_run_entry(db_session, operator, wo, op, wc)

    resp = client.post(
        _production_url(op.id),
        json={"quantity_complete_delta": 3, "quantity_scrapped_delta": 2, "scrap_reason_code_id": code.id},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    assert db_session.get(TimeEntry, entry.id).scrap_reason_code_id == code.id
    op = db_session.get(WorkOrderOperation, op.id)
    assert op.scrap_reason_code_id == code.id
    assert op.quantity_scrapped == 2


@pytest.mark.parametrize(
    "code_kind,expected_status",
    [("unknown", status.HTTP_404_NOT_FOUND), ("inactive", status.HTTP_422_UNPROCESSABLE_ENTITY)],
)
def test_production_report_invalid_code_rejected_and_nothing_persisted(
    client: TestClient, db_session: Session, code_kind: str, expected_status: int
):
    operator = make_user(db_session, role=UserRole.OPERATOR)
    wo, op, wc = make_wo_with_op(db_session)
    _open_run_entry(db_session, operator, wo, op, wc)
    code_id = 999_999 if code_kind == "unknown" else make_scrap_code(db_session, is_active=False).id

    resp = client.post(
        _production_url(op.id),
        json={"quantity_complete_delta": 1, "quantity_scrapped_delta": 2, "scrap_reason_code_id": code_id},
        headers=headers_for(operator),
    )
    assert resp.status_code == expected_status, resp.text

    db_session.rollback()
    db_session.expire_all()
    op = db_session.get(WorkOrderOperation, op.id)
    assert op.quantity_scrapped == 0
    assert op.quantity_complete == 0
    assert op.scrap_reason_code_id is None


# ===========================================================================
# POST /work-orders/{id}/complete (office override)
# ===========================================================================


def test_complete_wo_code_only_passes_and_persists_on_the_wo(client: TestClient, db_session: Session):
    manager = make_user(db_session, role=UserRole.MANAGER)
    code = make_scrap_code(db_session, code="FINAL")
    wo, op, wc = make_wo_with_op(db_session, quantity_ordered=10)

    resp = client.post(
        _complete_url(wo.id),
        params={"quantity_complete": 8, "quantity_scrapped": 2, "scrap_reason_code_id": code.id},
        headers=headers_for(manager),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    wo = db_session.get(WorkOrder, wo.id)
    assert wo.status == WorkOrderStatus.COMPLETE
    assert wo.quantity_scrapped == 2
    assert wo.scrap_reason_code_id == code.id
    assert wo.scrap_reason is None


def test_complete_wo_code_and_text_both_persist(client: TestClient, db_session: Session):
    manager = make_user(db_session, role=UserRole.MANAGER)
    code = make_scrap_code(db_session, code="FIN2")
    wo, op, wc = make_wo_with_op(db_session, quantity_ordered=10)

    resp = client.post(
        _complete_url(wo.id),
        params={
            "quantity_complete": 8,
            "quantity_scrapped": 2,
            "scrap_reason": "Failed final inspection",
            "scrap_reason_code_id": code.id,
        },
        headers=headers_for(manager),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    wo = db_session.get(WorkOrder, wo.id)
    assert wo.scrap_reason == "Failed final inspection"
    assert wo.scrap_reason_code_id == code.id


@pytest.mark.parametrize(
    "code_kind,expected_status",
    [
        ("unknown", status.HTTP_404_NOT_FOUND),
        ("cross_tenant", status.HTTP_404_NOT_FOUND),
        ("inactive", status.HTTP_422_UNPROCESSABLE_ENTITY),
    ],
)
def test_complete_wo_invalid_code_rejected_and_wo_untouched(
    client: TestClient, db_session: Session, code_kind: str, expected_status: int
):
    manager = make_user(db_session, role=UserRole.MANAGER)
    wo, op, wc = make_wo_with_op(db_session, quantity_ordered=10)

    if code_kind == "unknown":
        code_id = 999_999
    elif code_kind == "cross_tenant":
        code_id = make_scrap_code(db_session, company_id=COMPANY_B).id
    else:
        code_id = make_scrap_code(db_session, is_active=False).id

    resp = client.post(
        _complete_url(wo.id),
        params={"quantity_complete": 8, "quantity_scrapped": 2, "scrap_reason_code_id": code_id},
        headers=headers_for(manager),
    )
    assert resp.status_code == expected_status, resp.text

    db_session.rollback()
    db_session.expire_all()
    wo = db_session.get(WorkOrder, wo.id)
    assert wo.status == WorkOrderStatus.IN_PROGRESS, "a rejected complete must not flip the WO"
    assert (wo.quantity_scrapped or 0) == 0
    assert wo.scrap_reason_code_id is None


def test_complete_wo_explicit_scrap_write_replaces_categorization_wholly(client: TestClient, db_session: Session):
    """/complete's WO semantics differ from the entry/op never-clear rule by
    design: an explicit scrap write replaces the stored categorization, so a
    text-only scrap write nulls a previously recorded code (work_orders.py)."""
    manager = make_user(db_session, role=UserRole.MANAGER)
    code = make_scrap_code(db_session, code="OLD")
    wo, op, wc = make_wo_with_op(db_session, quantity_ordered=10)
    wo.quantity_scrapped = 1
    wo.scrap_reason_code_id = code.id
    db_session.commit()

    resp = client.post(
        _complete_url(wo.id),
        params={"quantity_complete": 8, "quantity_scrapped": 2, "scrap_reason": "recount at final"},
        headers=headers_for(manager),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    wo = db_session.get(WorkOrder, wo.id)
    assert wo.quantity_scrapped == 2
    assert wo.scrap_reason == "recount at final"
    assert wo.scrap_reason_code_id is None  # replaced wholly by the explicit write


def test_complete_wo_omitted_scrap_keeps_existing_code(client: TestClient, db_session: Session):
    """No quantity_scrapped in the call -> the stored scrap AND its code survive."""
    manager = make_user(db_session, role=UserRole.MANAGER)
    code = make_scrap_code(db_session, code="KEPT")
    wo, op, wc = make_wo_with_op(db_session, quantity_ordered=10)
    wo.quantity_scrapped = 2
    wo.scrap_reason_code_id = code.id
    db_session.commit()

    resp = client.post(
        _complete_url(wo.id),
        params={"quantity_complete": 8},
        headers=headers_for(manager),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    wo = db_session.get(WorkOrder, wo.id)
    assert wo.quantity_scrapped == 2
    assert wo.scrap_reason_code_id == code.id
