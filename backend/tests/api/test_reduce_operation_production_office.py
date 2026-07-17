"""Behavior locks for the supervisor/office over-count correction endpoint.

``POST /work-orders/operations/{id}/reduce-production`` is the role-gated twin of
the operator self-service verb: it walks the delta down ALL UNAPPROVED TimeEntry
evidence on the operation (any operator), open entries first then newest-first,
with NO open-clock-in requirement. APPROVED entries are the immutability boundary
(G5-A) -- they are excluded from the allowance and must be unapproved first via the
existing audited front door.

Both verbs share one core (``production_reduction_service`` +
``reduce_operation_produced_quantity``), so the guards locked here -- the
before-completion 409s, tenant 404, optimistic-lock 409, the recomputed WO rollup,
the per-entry audit trail -- are the SAME code paths as the shop-floor twin; these
tests lock the office-specific wiring (role gate, cross-operator eligibility,
approved-exclusion message) plus one end-to-end reconcile-safety proof.
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.time_entry import TimeEntry
from app.models.user import UserRole
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from tests.api.test_reduce_operation_production import (
    COMPANY_A,
    COMPANY_B,
    _ensure_company,
    add_operation,
    headers_for,
    make_closed_entry,
    make_open_entry,
    make_user,
    make_wo_op,
    set_wo_quantity_complete,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]


def office_reduce_url(op: WorkOrderOperation) -> str:
    return f"/api/v1/work-orders/operations/{op.id}/reduce-production"


def test_office_reduce_another_operators_unapproved_evidence(client: TestClient, db_session: Session):
    """Happy path: a supervisor (not clocked in) corrects an operator's closed session.

    This is the production case the self-service bound can't reach: the over-count
    lives on ANOTHER user's evidence. The walk lowers the operator's entry, the op
    total, and the WO rollup -- and survives the WO GET reconcile.
    """
    operator = make_user(db_session)
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    wo, op, _wc = make_wo_op(db_session)
    entry = make_closed_entry(db_session, operator, wo, op, quantity_produced=10)
    op_row = db_session.get(WorkOrderOperation, op.id)
    op_row.quantity_complete = 10
    wo_row = db_session.get(WorkOrder, wo.id)
    wo_row.quantity_complete = 10
    db_session.commit()

    resp = client.post(
        office_reduce_url(op),
        json={"quantity_delta": 4, "reason": "operator over-scanned; corrected at the desk"},
        headers=headers_for(supervisor),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["operation"]["quantity_complete"] == 6
    assert body["work_order"]["quantity_complete"] == 6
    assert body["reduced_time_entries"] == [
        {
            "time_entry_id": entry.id,
            "entry_type": "run",
            "quantity_produced_before": 10,
            "quantity_produced_after": 6,
        }
    ]

    db_session.expire_all()
    assert db_session.get(TimeEntry, entry.id).quantity_produced == 6
    assert db_session.get(WorkOrderOperation, op.id).quantity_complete == 6
    assert db_session.get(WorkOrder, wo.id).quantity_complete == 6

    # Reconcile-safety end-to-end: the WO GET re-derives from evidence; stays reduced.
    get_resp = client.get(f"/api/v1/work-orders/{wo.id}", headers=headers_for(supervisor))
    assert get_resp.status_code == status.HTTP_200_OK, get_resp.text
    assert get_resp.json()["quantity_complete"] == 6


def test_office_reduce_walks_across_multiple_operators(client: TestClient, db_session: Session):
    """The office allowance spans every operator's unapproved evidence, open first."""
    op_a = make_user(db_session)
    op_b = make_user(db_session)
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    wo, op, _wc = make_wo_op(db_session)
    closed_b = make_closed_entry(db_session, op_b, wo, op, quantity_produced=5, hours_ago=3)
    open_a = make_open_entry(db_session, op_a, wo, op, quantity_produced=4)
    op_row = db_session.get(WorkOrderOperation, op.id)
    op_row.quantity_complete = 9
    db_session.commit()

    # Remove 7: open (A) 4 -> 0, then closed (B) 5 -> 2.
    resp = client.post(
        office_reduce_url(op),
        json={"quantity_delta": 7, "reason": "batch ticket keyed twice"},
        headers=headers_for(supervisor),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    walked = resp.json()["reduced_time_entries"]
    assert [w["time_entry_id"] for w in walked] == [open_a.id, closed_b.id]

    db_session.expire_all()
    assert db_session.get(TimeEntry, open_a.id).quantity_produced == 0
    assert db_session.get(TimeEntry, closed_b.id).quantity_produced == 2
    assert db_session.get(WorkOrderOperation, op.id).quantity_complete == 2


def test_office_reduce_skips_interleaved_approved_entry_and_recomputes_rollup(client: TestClient, db_session: Session):
    """An APPROVED entry sitting BETWEEN two unapproved sessions is skipped, order preserved.

    Timeline (newest first): unapproved closed (3) -> APPROVED closed (5) -> unapproved
    closed (4, another operator). The walk consumes newest-first across the unapproved
    rows only -- the approved row in the middle is never touched and never appears in
    the slices. And on a MULTI-OP work order the rollup is RECOMPUTED through this
    multi-entry walk: the corrected op drops 12 -> 7, the sibling holds 6, so the WO
    lands at max(7, 6) = 7 and stays there through the WO GET reconcile.
    """
    op_a = make_user(db_session)
    op_b = make_user(db_session)
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    approver = make_user(db_session, role=UserRole.MANAGER)
    wo, op, wc = make_wo_op(db_session)
    oldest_unapproved = make_closed_entry(db_session, op_b, wo, op, quantity_produced=4, hours_ago=9)
    approved_middle = make_closed_entry(
        db_session, op_a, wo, op, quantity_produced=5, hours_ago=5, approved_by=approver.id
    )
    newest_unapproved = make_closed_entry(db_session, op_a, wo, op, quantity_produced=3, hours_ago=2)
    op_row = db_session.get(WorkOrderOperation, op.id)
    op_row.quantity_complete = 12
    add_operation(db_session, wo, wc, sequence=20, quantity_complete=6)  # sibling: the rollup floor
    set_wo_quantity_complete(db_session, wo, 12)

    # Remove 5: newest unapproved 3 -> 0, skip the approved 5, oldest unapproved 4 -> 2.
    resp = client.post(
        office_reduce_url(op),
        json={"quantity_delta": 5, "reason": "double-keyed across shifts"},
        headers=headers_for(supervisor),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    expected_slices = [
        {
            "time_entry_id": newest_unapproved.id,
            "entry_type": "run",
            "quantity_produced_before": 3,
            "quantity_produced_after": 0,
        },
        {
            "time_entry_id": oldest_unapproved.id,
            "entry_type": "run",
            "quantity_produced_before": 4,
            "quantity_produced_after": 2,
        },
    ]
    assert resp.json()["reduced_time_entries"] == expected_slices

    db_session.expire_all()
    assert db_session.get(TimeEntry, approved_middle.id).quantity_produced == 5, "approved row must never be walked"
    assert db_session.get(TimeEntry, newest_unapproved.id).quantity_produced == 0
    assert db_session.get(TimeEntry, oldest_unapproved.id).quantity_produced == 2
    assert db_session.get(WorkOrderOperation, op.id).quantity_complete == 7
    assert db_session.get(WorkOrder, wo.id).quantity_complete == 7, "rollup recomputed: max(7, sibling 6)"

    # The audit trail mirrors the walk -- approved row absent, order preserved.
    audit_row = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.resource_type == "work_order_operation",
            AuditLog.resource_id == op.id,
            AuditLog.action == "REDUCE_OPERATION_PRODUCTION",
        )
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert audit_row is not None
    assert audit_row.extra_data["time_entries"] == expected_slices

    # Reconcile-on-read: the reduced counts stick and WO >= max(op) holds.
    get_resp = client.get(f"/api/v1/work-orders/{wo.id}", headers=headers_for(supervisor))
    assert get_resp.status_code == status.HTTP_200_OK, get_resp.text
    body = get_resp.json()
    assert body["quantity_complete"] == 7
    assert body["quantity_complete"] >= max(o["quantity_complete"] for o in body["operations"])


def test_office_reduce_operator_role_is_403(client: TestClient, db_session: Session):
    """The office verb is Work Orders Edit power: OPERATOR (and their own evidence) -> 403."""
    operator = make_user(db_session)  # OPERATOR role
    wo, op, _wc = make_wo_op(db_session)
    make_closed_entry(db_session, operator, wo, op, quantity_produced=5)

    resp = client.post(
        office_reduce_url(op),
        json={"quantity_delta": 1, "reason": "not my lane"},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text

    db_session.expire_all()
    assert db_session.query(TimeEntry).filter(TimeEntry.operation_id == op.id).one().quantity_produced == 5


def test_office_reduce_approved_evidence_needs_unapprove_first(client: TestClient, db_session: Session):
    """Approved labor is excluded even for supervisors -- the 400 points at unapprove."""
    operator = make_user(db_session)
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    approver = make_user(db_session, role=UserRole.MANAGER)
    wo, op, _wc = make_wo_op(db_session)
    entry = make_closed_entry(db_session, operator, wo, op, quantity_produced=8, approved_by=approver.id)
    op_row = db_session.get(WorkOrderOperation, op.id)
    op_row.quantity_complete = 8
    db_session.commit()

    resp = client.post(
        office_reduce_url(op),
        json={"quantity_delta": 3, "reason": "already signed off"},
        headers=headers_for(supervisor),
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
    assert "unapprove it first" in resp.json()["detail"]

    db_session.expire_all()
    assert db_session.get(TimeEntry, entry.id).quantity_produced == 8
    assert db_session.get(WorkOrderOperation, op.id).quantity_complete == 8


def test_office_reduce_completed_operation_is_409(client: TestClient, db_session: Session):
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    operator = make_user(db_session)
    wo, op, _wc = make_wo_op(db_session, op_status=OperationStatus.COMPLETE)
    make_closed_entry(db_session, operator, wo, op, quantity_produced=5)

    resp = client.post(
        office_reduce_url(op),
        json={"quantity_delta": 1, "reason": "too late"},
        headers=headers_for(supervisor),
    )
    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text


@pytest.mark.parametrize(
    "wo_status",
    [WorkOrderStatus.COMPLETE, WorkOrderStatus.CLOSED, WorkOrderStatus.CANCELLED],
)
def test_office_reduce_terminal_work_order_is_409(client: TestClient, db_session: Session, wo_status: WorkOrderStatus):
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    operator = make_user(db_session)
    wo, op, _wc = make_wo_op(db_session, wo_status=wo_status)
    make_closed_entry(db_session, operator, wo, op, quantity_produced=5)

    resp = client.post(
        office_reduce_url(op),
        json={"quantity_delta": 1, "reason": "job is done"},
        headers=headers_for(supervisor),
    )
    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text


def test_office_reduce_cross_company_operation_is_404(client: TestClient, db_session: Session):
    """Tenant isolation: a company-A admin cannot see (or correct) a company-B operation."""
    _ensure_company(db_session, COMPANY_B)
    admin_a = make_user(db_session, role=UserRole.ADMIN, company_id=COMPANY_A)
    operator_b = make_user(db_session, company_id=COMPANY_B)
    wo_b, op_b, _wc = make_wo_op(db_session, company_id=COMPANY_B)
    make_closed_entry(db_session, operator_b, wo_b, op_b, quantity_produced=5, company_id=COMPANY_B)

    resp = client.post(
        office_reduce_url(op_b),
        json={"quantity_delta": 1, "reason": "cross-tenant"},
        headers=headers_for(admin_a),
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text


def test_office_reduce_missing_reason_is_422(client: TestClient, db_session: Session):
    """The shared schema's required correction reason applies to the office verb too."""
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    operator = make_user(db_session)
    wo, op, _wc = make_wo_op(db_session)
    make_closed_entry(db_session, operator, wo, op, quantity_produced=5)

    resp = client.post(office_reduce_url(op), json={"quantity_delta": 1}, headers=headers_for(supervisor))
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, resp.text


def test_office_reduce_rejects_import_source_422(client: TestClient, db_session: Session):
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    operator = make_user(db_session)
    wo, op, _wc = make_wo_op(db_session)
    make_closed_entry(db_session, operator, wo, op, quantity_produced=5)

    resp = client.post(
        office_reduce_url(op),
        json={"quantity_delta": 1, "reason": "nope", "source": "import"},
        headers=headers_for(supervisor),
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, resp.text
    db_session.expire_all()
    assert db_session.query(TimeEntry).filter(TimeEntry.operation_id == op.id).one().quantity_produced == 5


def test_office_reduce_writes_audit_row_with_per_entry_trail(client: TestClient, db_session: Session):
    """One tamper-evident log_update row, attributed to the SUPERVISOR, with per-entry slices."""
    operator = make_user(db_session)
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    wo, op, _wc = make_wo_op(db_session)
    entry = make_closed_entry(db_session, operator, wo, op, quantity_produced=9)
    op_row = db_session.get(WorkOrderOperation, op.id)
    op_row.quantity_complete = 9
    db_session.commit()

    resp = client.post(
        office_reduce_url(op),
        json={"quantity_delta": 5, "reason": "desk correction", "notes": "per floor call"},
        headers=headers_for(supervisor),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    audit_row = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.resource_type == "work_order_operation",
            AuditLog.resource_id == op.id,
            AuditLog.action == "REDUCE_OPERATION_PRODUCTION",
        )
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert audit_row is not None, "office correction must land on the tamper-evident chain"
    assert audit_row.user_id == supervisor.id
    assert audit_row.extra_data["reason"] == "desk correction"
    assert audit_row.extra_data["time_entries"] == [
        {
            "time_entry_id": entry.id,
            "entry_type": "run",
            "quantity_produced_before": 9,
            "quantity_produced_after": 4,
        }
    ]
    # A supervisor's note rides the audit row, not the operator's labor record.
    assert "per floor call" in (audit_row.description or "")
    db_session.expire_all()
    assert db_session.get(TimeEntry, entry.id).notes in (None, ""), "office notes must not touch the entry"


def test_office_reduce_translates_stale_version_to_409(client: TestClient, db_session: Session, monkeypatch):
    """A concurrent stale write (StaleDataError on commit) surfaces as 409, not 500."""
    from sqlalchemy.orm.exc import StaleDataError

    operator = make_user(db_session)
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    wo, op, _wc = make_wo_op(db_session)
    make_closed_entry(db_session, operator, wo, op, quantity_produced=6)
    op_row = db_session.get(WorkOrderOperation, op.id)
    op_row.quantity_complete = 6
    db_session.commit()

    original_commit = db_session.commit
    calls = {"n": 0}

    def flaky_commit(*args, **kwargs):
        if calls["n"] == 0:
            calls["n"] += 1
            raise StaleDataError("simulated concurrent version bump")
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(db_session, "commit", flaky_commit)

    resp = client.post(
        office_reduce_url(op),
        json={"quantity_delta": 2, "reason": "raced"},
        headers=headers_for(supervisor),
    )
    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
    assert "concurrently" in resp.json()["detail"]


def test_office_reduce_no_open_clockin_required(client: TestClient, db_session: Session):
    """Unlike the shop-floor twin, the supervisor needs no open entry anywhere."""
    operator = make_user(db_session)
    manager = make_user(db_session, role=UserRole.MANAGER)
    wo, op, _wc = make_wo_op(db_session)
    make_closed_entry(db_session, operator, wo, op, quantity_produced=3)
    op_row = db_session.get(WorkOrderOperation, op.id)
    op_row.quantity_complete = 3
    db_session.commit()

    # Sanity: the manager holds no TimeEntry at all.
    assert (
        db_session.query(TimeEntry).filter(TimeEntry.user_id == manager.id, TimeEntry.clock_out.is_(None)).count() == 0
    )

    resp = client.post(
        office_reduce_url(op),
        json={"quantity_delta": 1, "reason": "office correction without a clock-in"},
        headers=headers_for(manager),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    db_session.expire_all()
    assert db_session.get(WorkOrderOperation, op.id).quantity_complete == 2
