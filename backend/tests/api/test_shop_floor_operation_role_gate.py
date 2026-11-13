"""P0.4: the shop-floor operation verbs are the floor's PERFORMING roles only (owner decision).

``PUT /shop-floor/operations/{id}/start`` and ``POST /shop-floor/operations/{id}/complete``
took a bare ``get_current_user`` -- any authenticated user, Viewer and Shipping included,
could start or complete an operation. They now take
``require_role([ADMIN, MANAGER, SUPERVISOR, OPERATOR, QUALITY])``. Pinned here:

- Viewer and Shipping get 403 ``"Insufficient permissions"`` on both verbs and NOTHING is
  written: the operation, its labor entries, the work order and the audit trail are as
  they were -- and on ``complete`` the operation was otherwise completable, so the 403 is
  the role gate, not a state refusal.
- The refusal precedes body validation (403, not 422) -- no existence oracle either.
- The same holds through a real crew-station BADGE token (the badge mint itself does not
  check role and still answers 200), and through an API token bound to a Viewer.
- Every performing role -- Admin, Manager, Supervisor, Operator, Quality -- is unchanged,
  through a desktop token, and the kiosk is unchanged: a badge-minted Operator token
  starts and completes, and so does the employee-login-style unscoped Operator token.
- The documented residual is pinned so widening the gate later is a conscious change: a
  Viewer's clock-in still answers 200.
"""

from __future__ import annotations

from typing import Tuple

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.time_entry import TimeEntry
from app.models.user import User, UserRole
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from tests.api.kiosk_test_helpers import (
    bearer,
    kiosk_token_for,
    make_kiosk_station,
    make_user,
    make_wo_with_operation,
    make_work_center,
    mint_badge_token,
    user_headers,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

START = "/api/v1/shop-floor/operations/{op}/start"
COMPLETE = "/api/v1/shop-floor/operations/{op}/complete"
CLOCK_IN = "/api/v1/shop-floor/clock-in"
PRODUCTION = "/api/v1/shop-floor/operations/{op}/production"
DENIED_DETAIL = "Insufficient permissions"

DENIED_ROLES = (UserRole.VIEWER, UserRole.SHIPPING)
FLOOR_ROLES = (UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR, UserRole.OPERATOR, UserRole.QUALITY)


@pytest.fixture(autouse=True)
def _no_completion_signal_enqueue(monkeypatch):
    """Skip the post-commit ARQ enqueue (it retries a missing Redis for seconds)."""
    monkeypatch.setattr("app.api.endpoints.work_orders.enqueue_work_order_completion_signals", lambda **_: None)
    monkeypatch.setattr("app.api.endpoints.shop_floor.enqueue_work_order_completion_signals", lambda **_: None)


def _ready_operation(db: Session) -> Tuple[WorkOrder, WorkOrderOperation]:
    wc = make_work_center(db)
    return make_wo_with_operation(db, work_center=wc, quantity_ordered=10)


def _clock_in(client: TestClient, headers: dict, wo: WorkOrder, op: WorkOrderOperation):
    return client.post(
        CLOCK_IN,
        headers=headers,
        json={"work_order_id": wo.id, "operation_id": op.id, "work_center_id": op.work_center_id, "entry_type": "run"},
    )


def _completable_operation(client: TestClient, db: Session) -> Tuple[WorkOrder, WorkOrderOperation, User]:
    """An IN_PROGRESS operation with the full 10 recorded good and an operator still clocked in --
    so a complete would succeed for a performing role: any 403 is the role gate."""
    wo, op = _ready_operation(db)
    operator = make_user(db, role=UserRole.OPERATOR)
    headers = user_headers(operator)
    assert _clock_in(client, headers, wo, op).status_code == 200
    report = client.post(PRODUCTION.format(op=op.id), headers=headers, json={"quantity_complete_delta": 10})
    assert report.status_code == 200, report.text
    return wo, op, operator


def _assert_untouched_ready(db: Session, wo: WorkOrder, op: WorkOrderOperation, audit_before: int) -> None:
    assert not db.dirty and not db.new
    db.expire_all()
    fresh = db.get(WorkOrderOperation, op.id)
    assert fresh.status == OperationStatus.READY
    assert fresh.actual_start is None and fresh.started_by is None
    assert db.get(WorkOrder, wo.id).status == WorkOrderStatus.RELEASED
    assert db.query(TimeEntry).filter(TimeEntry.operation_id == op.id).count() == 0
    assert db.query(AuditLog).count() == audit_before


def _assert_still_open(db: Session, wo: WorkOrder, op: WorkOrderOperation, operator: User, audit_before: int) -> None:
    db.expire_all()
    fresh = db.get(WorkOrderOperation, op.id)
    assert fresh.status == OperationStatus.IN_PROGRESS
    assert fresh.completed_by is None
    entry = db.query(TimeEntry).filter(TimeEntry.operation_id == op.id, TimeEntry.user_id == operator.id).one()
    assert entry.clock_out is None, "the operator's open entry was not closed"
    assert db.get(WorkOrder, wo.id).status != WorkOrderStatus.COMPLETE
    assert db.query(AuditLog).count() == audit_before


# --------------------------------------------------------------------------- denied


@pytest.mark.parametrize("role", DENIED_ROLES, ids=lambda role: role.value)
def test_denied_role_cannot_start_and_nothing_is_written(client: TestClient, db_session: Session, role: UserRole):
    wo, op = _ready_operation(db_session)
    user = make_user(db_session, role=role)
    audit_before = db_session.query(AuditLog).count()

    response = client.put(START.format(op=op.id), headers=user_headers(user))

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == DENIED_DETAIL
    _assert_untouched_ready(db_session, wo, op, audit_before)


@pytest.mark.parametrize("role", DENIED_ROLES, ids=lambda role: role.value)
def test_denied_role_cannot_complete_a_completable_operation(client: TestClient, db_session: Session, role: UserRole):
    wo, op, operator = _completable_operation(client, db_session)
    user = make_user(db_session, role=role)
    audit_before = db_session.query(AuditLog).count()

    response = client.post(COMPLETE.format(op=op.id), headers=user_headers(user), json={})

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == DENIED_DETAIL
    _assert_still_open(db_session, wo, op, operator, audit_before)


@pytest.mark.parametrize("role", DENIED_ROLES, ids=lambda role: role.value)
def test_the_role_gate_answers_before_body_validation(client: TestClient, db_session: Session, role: UserRole):
    """``OperationCompleteRequest`` forbids unknown keys (422) -- but a denied role is told 403 first."""
    _wo, op = _ready_operation(db_session)
    user = make_user(db_session, role=role)
    response = client.post(COMPLETE.format(op=op.id), headers=user_headers(user), json={"no_such_key": 1})
    assert response.status_code == 403
    assert response.json()["detail"] == DENIED_DETAIL


@pytest.mark.parametrize("role", DENIED_ROLES, ids=lambda role: role.value)
def test_the_role_gate_is_no_existence_oracle(client: TestClient, db_session: Session, role: UserRole):
    user = make_user(db_session, role=role)
    assert client.put(START.format(op=999999), headers=user_headers(user)).status_code == 403
    assert client.post(COMPLETE.format(op=999999), headers=user_headers(user), json={}).status_code == 403


@pytest.mark.parametrize("role", DENIED_ROLES, ids=lambda role: role.value)
def test_a_denied_badge_mints_at_the_crew_station_and_is_then_refused(
    client: TestClient, db_session: Session, role: UserRole
):
    """The badge mint does not check role (documented), so the refusal comes from the verbs'
    role gate -- "Insufficient permissions", not the kiosk path fence."""
    wo, op, operator = _completable_operation(client, db_session)
    station = make_kiosk_station(db_session, work_center=db_session.get(WorkOrderOperation, op.id).work_center)
    user = make_user(db_session, role=role)
    minted = mint_badge_token(client, kiosk_token_for(station), user.employee_id)
    assert minted.status_code == 200, minted.text
    badge = bearer(minted.json()["access_token"])
    audit_before = db_session.query(AuditLog).count()

    completed = client.post(COMPLETE.format(op=op.id), headers=badge, json={"source": "kiosk"})
    assert completed.status_code == 403, completed.text
    assert completed.json()["detail"] == DENIED_DETAIL
    _assert_still_open(db_session, wo, op, operator, audit_before)

    wo2, op2 = _ready_operation(db_session)
    started = client.put(START.format(op=op2.id), headers=badge)
    assert started.status_code == 403
    assert started.json()["detail"] == DENIED_DETAIL


def test_a_viewer_bound_api_token_is_refused_on_both_verbs(
    client: TestClient, db_session: Session, admin_headers: dict
):
    viewer = make_user(db_session, role=UserRole.VIEWER)
    issued = client.post(
        "/api/v1/api-tokens/", json={"user_id": viewer.id, "label": "viewer bot"}, headers=admin_headers
    )
    assert issued.status_code == 201, issued.text
    token = bearer(issued.json()["token"])
    _wo, op = _ready_operation(db_session)
    for response in (
        client.put(START.format(op=op.id), headers=token),
        client.post(COMPLETE.format(op=op.id), headers=token, json={}),
    ):
        assert response.status_code == 403
        assert response.json()["detail"] == DENIED_DETAIL


# --------------------------------------------------------------------------- allowed


@pytest.mark.parametrize("role", FLOOR_ROLES, ids=lambda role: role.value)
def test_every_floor_role_still_starts(client: TestClient, db_session: Session, role: UserRole):
    _wo, op = _ready_operation(db_session)
    user = make_user(db_session, role=role)
    response = client.put(START.format(op=op.id), headers=user_headers(user))
    assert response.status_code == 200, response.text
    db_session.expire_all()
    fresh = db_session.get(WorkOrderOperation, op.id)
    assert fresh.status == OperationStatus.IN_PROGRESS
    assert fresh.started_by == user.id


@pytest.mark.parametrize("role", FLOOR_ROLES, ids=lambda role: role.value)
def test_every_floor_role_still_completes(client: TestClient, db_session: Session, role: UserRole):
    _wo, op, _operator = _completable_operation(client, db_session)
    user = make_user(db_session, role=role)
    response = client.post(COMPLETE.format(op=op.id), headers=user_headers(user), json={})
    assert response.status_code == 200, response.text
    assert response.json()["is_fully_complete"] is True
    db_session.expire_all()
    fresh = db_session.get(WorkOrderOperation, op.id)
    assert fresh.status == OperationStatus.COMPLETE
    assert fresh.completed_by == user.id


def test_the_kiosk_is_unchanged_for_operators(client: TestClient, db_session: Session):
    """Both kiosk credentials are the operator's own access JWT with the live role: a
    badge-minted ``scope="kiosk"`` token and the single-operator (employee-login style)
    unscoped token both start and complete."""
    # Crew station: badge-minted operator token.
    wo, op = _ready_operation(db_session)
    station = make_kiosk_station(db_session, work_center=db_session.get(WorkOrderOperation, op.id).work_center)
    crew_operator = make_user(db_session, role=UserRole.OPERATOR)
    minted = mint_badge_token(client, kiosk_token_for(station), crew_operator.employee_id)
    assert minted.status_code == 200, minted.text
    badge = bearer(minted.json()["access_token"])
    assert client.put(START.format(op=op.id), headers=badge).status_code == 200
    report = client.post(PRODUCTION.format(op=op.id), headers=badge, json={"quantity_complete_delta": 10})
    assert report.status_code == 200, report.text
    completed = client.post(COMPLETE.format(op=op.id), headers=badge, json={"source": "kiosk"})
    assert completed.status_code == 200, completed.text

    # Single-operator kiosk: an unscoped operator access token.
    wo2, op2 = _ready_operation(db_session)
    solo = make_user(db_session, role=UserRole.OPERATOR)
    headers = user_headers(solo)
    assert client.put(START.format(op=op2.id), headers=headers).status_code == 200
    assert (
        client.post(PRODUCTION.format(op=op2.id), headers=headers, json={"quantity_complete_delta": 10}).status_code
        == 200
    )
    assert client.post(COMPLETE.format(op=op2.id), headers=headers, json={}).status_code == 200


# --------------------------------------------------------------------------- the residual


@pytest.mark.parametrize("role", DENIED_ROLES, ids=lambda role: role.value)
def test_documented_residual_a_denied_role_can_still_clock_in(client: TestClient, db_session: Session, role: UserRole):
    """P0.4 gated the two named verbs ONLY. Clock-in (which starts a READY operation) stays
    open to any authenticated user -- docs/RBAC_PERMISSIONS.md says so plainly. If this ever
    turns 403, the docs' residual note must change with it."""
    wo, op = _ready_operation(db_session)
    user = make_user(db_session, role=role)
    assert _clock_in(client, user_headers(user), wo, op).status_code == 200
