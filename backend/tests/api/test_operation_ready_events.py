"""operation_ready OperationalEvents at the PENDING->READY flip sites (Lean Phase 1).

Queue time is measured ready -> actual_start, so BOTH flip sites must emit:
  1. WO release (``release_first_ready_operation`` inside POST /{id}/release) --
     a user action, so ``user_id`` is the releasing user;
  2. successor promotion when an operation completes
     (``release_next_ready_operation`` in the finalizer) -- rule-driven, so
     ``user_id`` is None.

The event carries the documented payload (work_order_id / operation_id /
sequence / work_order_number / operation_number) and is BEST-EFFORT: an emitter
failure must never fail the release that triggered it.
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.operational_event import OperationalEvent
from app.models.user import UserRole
from app.models.work_order import OperationStatus, WorkOrderOperation, WorkOrderStatus
from tests.lean_phase1_helpers import (
    COMPANY_A,
    headers_for,
    make_op,
    make_part,
    make_user,
    make_wo,
    make_work_center,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]


def _ready_events(db: Session, operation_id: int):
    return (
        db.query(OperationalEvent)
        .filter(OperationalEvent.event_type == "operation_ready", OperationalEvent.operation_id == operation_id)
        .all()
    )


def _draft_wo_with_two_ops(db: Session):
    part = make_part(db)
    wc = make_work_center(db)
    wo = make_wo(db, part, status_=WorkOrderStatus.DRAFT)
    op1 = make_op(db, wo, wc, sequence=10, status_=OperationStatus.PENDING)
    op2 = make_op(db, wo, wc, sequence=20, status_=OperationStatus.PENDING)
    return wo, op1, op2


def test_release_emits_operation_ready_with_documented_payload(client: TestClient, db_session: Session):
    manager = make_user(db_session, role=UserRole.MANAGER)
    wo, op1, op2 = _draft_wo_with_two_ops(db_session)

    resp = client.post(f"/api/v1/work-orders/{wo.id}/release", headers=headers_for(manager))
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    assert db_session.get(WorkOrderOperation, op1.id).status == OperationStatus.READY

    events = _ready_events(db_session, op1.id)
    assert len(events) == 1
    event = events[0]
    assert event.company_id == COMPANY_A
    assert event.work_order_id == wo.id
    assert event.entity_type == "work_order_operation"
    assert event.entity_id == op1.id
    assert event.user_id == manager.id  # the release is a user action
    assert event.event_payload == {
        "work_order_id": wo.id,
        "operation_id": op1.id,
        "sequence": 10,
        "work_order_number": wo.work_order_number,
        "operation_number": "OP10",
    }
    # Only the FIRST op became ready -- no event (and no READY) for op2 yet.
    assert _ready_events(db_session, op2.id) == []
    assert db_session.get(WorkOrderOperation, op2.id).status == OperationStatus.PENDING


def test_successor_promotion_emits_rule_driven_event(client: TestClient, db_session: Session):
    manager = make_user(db_session, role=UserRole.MANAGER)
    wo, op1, op2 = _draft_wo_with_two_ops(db_session)
    client.post(f"/api/v1/work-orders/{wo.id}/release", headers=headers_for(manager))

    resp = client.post(
        f"/api/v1/work-orders/operations/{op1.id}/complete",
        params={"quantity_complete": 10},
        headers=headers_for(manager),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    assert db_session.get(WorkOrderOperation, op2.id).status == OperationStatus.READY

    events = _ready_events(db_session, op2.id)
    assert len(events) == 1
    event = events[0]
    assert event.user_id is None  # rule-driven promotion, not a user action
    assert event.event_payload["operation_id"] == op2.id
    assert event.event_payload["sequence"] == 20


def test_emission_failure_never_fails_the_release(client: TestClient, db_session: Session, monkeypatch):
    """Best-effort contract: the operation_ready emit blowing up must not turn a
    valid release into an error -- the flip itself still lands.

    The failure is scoped to event_type == 'operation_ready': the release
    endpoint's own work_order_released emit (_emit_work_order_event) is a
    separate, pre-existing UNGUARDED emitter, deliberately left out of scope."""
    import app.services.operational_event_service as oes

    real_emit = oes.OperationalEventService.emit

    def _boom(self, **kwargs):
        if kwargs.get("event_type") == "operation_ready":
            raise RuntimeError("event store down")
        return real_emit(self, **kwargs)

    monkeypatch.setattr(oes.OperationalEventService, "emit", _boom)

    manager = make_user(db_session, role=UserRole.MANAGER)
    wo, op1, _op2 = _draft_wo_with_two_ops(db_session)

    resp = client.post(f"/api/v1/work-orders/{wo.id}/release", headers=headers_for(manager))
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    assert db_session.get(WorkOrderOperation, op1.id).status == OperationStatus.READY
    assert _ready_events(db_session, op1.id) == []
