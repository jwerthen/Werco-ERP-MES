"""OperationalEvent emission is best-effort everywhere it rides a business operation.

Operational events are telemetry, not audit data. Before this fix,
``_emit_work_order_event`` (work_orders.py) and ~40 sibling emit sites called
``OperationalEventService.emit`` unguarded, so an event-store failure would 500 the
business operation that triggered it (e.g. a work-order release). The fix routes
every such site through ``OperationalEventService.emit_best_effort``, which mirrors
the guard semantics of ``services/completion_signal_service.py``: catch everything,
log a WARNING with context, continue (flush-only -- the caller's transaction still
commits).

These tests make the WHOLE event store raise (every ``emit``, not one event type --
contrast tests/api/test_operation_ready_events.py, which scopes its failure to the
operation_ready guard) and prove the formerly-500ing operations still succeed:
  1. work-order release (the reported bug: ``work_order_released``);
  2. mark-shipped (``shipment_shipped`` + the guarded ``work_order_closed``).
Plus the service-level contract of ``emit_best_effort`` itself.
"""

import logging

import pytest
from fastapi import status as http_status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import app.services.operational_event_service as oes
from app.models.operational_event import OperationalEvent
from app.models.shipping import Shipment, ShipmentStatus
from app.models.user import UserRole
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderStatus
from tests.lean_phase1_helpers import (
    COMPANY_A,
    ensure_company,
    headers_for,
    make_op,
    make_part,
    make_shipment,
    make_user,
    make_wo,
    make_work_center,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

EMIT_LOGGER = "app.services.operational_event_service"


def _break_event_store(monkeypatch):
    """Make EVERY OperationalEventService.emit raise -- the event store is down."""

    def _boom(self, **kwargs):
        raise RuntimeError("event store down")

    monkeypatch.setattr(oes.OperationalEventService, "emit", _boom)


def _warning_messages(caplog):
    return [r.getMessage() for r in caplog.records if r.name == EMIT_LOGGER and r.levelno == logging.WARNING]


def test_release_succeeds_when_event_store_raises(client: TestClient, db_session: Session, monkeypatch, caplog):
    """A raising event store must NOT fail a work-order release.

    Regression test for the unguarded ``_emit_work_order_event``: with every emit
    raising, POST /release still returns 200, the WO lands RELEASED, and the
    work_order_released failure is logged at WARNING (not raised)."""
    _break_event_store(monkeypatch)

    manager = make_user(db_session, role=UserRole.MANAGER)
    part = make_part(db_session)
    wc = make_work_center(db_session)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.DRAFT)
    op = make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.PENDING)

    with caplog.at_level(logging.WARNING, logger=EMIT_LOGGER):
        resp = client.post(f"/api/v1/work-orders/{wo.id}/release", headers=headers_for(manager))

    assert resp.status_code == http_status.HTTP_200_OK, resp.text

    db_session.expire_all()
    refreshed = db_session.get(WorkOrder, wo.id)
    assert refreshed is not None and refreshed.status == WorkOrderStatus.RELEASED
    assert refreshed.released_by == manager.id

    # The emit really failed (nothing was written) and was logged, not raised.
    assert db_session.query(OperationalEvent).filter(OperationalEvent.work_order_id == wo.id).count() == 0
    warnings = _warning_messages(caplog)
    assert any("event_type=work_order_released" in m and f"work_order_id={wo.id}" in m for m in warnings), warnings
    assert any(f"company_id={COMPANY_A}" in m for m in warnings)
    # ... and the PENDING->READY flip still landed despite its (separately guarded) emit failing too.
    db_session.expire_all()
    assert db_session.get(type(op), op.id).status == OperationStatus.READY


def test_mark_shipped_succeeds_when_event_store_raises(client: TestClient, db_session: Session, monkeypatch, caplog):
    """A raising event store must NOT fail mark-shipped (another formerly-unguarded
    site: the ``shipment_shipped`` emit). The shipment still flips SHIPPED and the
    terminal WO close still lands."""
    _break_event_store(monkeypatch)

    manager = make_user(db_session, role=UserRole.MANAGER)
    part = make_part(db_session)
    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS)
    shipment = make_shipment(db_session, wo, status=ShipmentStatus.PENDING, quantity_shipped=5)

    with caplog.at_level(logging.WARNING, logger=EMIT_LOGGER):
        resp = client.post(f"/api/v1/shipping/{shipment.id}/ship", headers=headers_for(manager))

    assert resp.status_code == http_status.HTTP_200_OK, resp.text
    assert resp.json()["shipment_number"] == shipment.shipment_number

    db_session.expire_all()
    assert db_session.get(Shipment, shipment.id).status == ShipmentStatus.SHIPPED
    assert db_session.get(WorkOrder, wo.id).status == WorkOrderStatus.CLOSED

    assert db_session.query(OperationalEvent).filter(OperationalEvent.work_order_id == wo.id).count() == 0
    warnings = _warning_messages(caplog)
    assert any("event_type=shipment_shipped" in m and f"work_order_id={wo.id}" in m for m in warnings), warnings


def test_emit_best_effort_swallows_logs_and_leaves_session_usable(db_session: Session, monkeypatch, caplog):
    """Service-level contract: a failing emit_best_effort returns None, logs at
    WARNING with context, and leaves the session healthy so the parent unit of
    work can still write and commit."""
    ensure_company(db_session, COMPANY_A)
    _break_event_store(monkeypatch)

    with caplog.at_level(logging.WARNING, logger=EMIT_LOGGER):
        result = oes.OperationalEventService(db_session).emit_best_effort(
            company_id=COMPANY_A,
            event_type="unit_test_event",
            source_module="tests",
        )

    assert result is None
    warnings = _warning_messages(caplog)
    assert any("event_type=unit_test_event" in m and "source_module=tests" in m for m in warnings), warnings

    # Session not poisoned: the parent operation can still write + commit.
    survivor = make_user(db_session, role=UserRole.OPERATOR)
    assert survivor.id is not None


def test_emit_best_effort_success_path_still_persists(db_session: Session):
    """When the store is healthy, emit_best_effort behaves exactly like emit."""
    ensure_company(db_session, COMPANY_A)

    event = oes.OperationalEventService(db_session).emit_best_effort(
        company_id=COMPANY_A,
        event_type="unit_test_event_ok",
        source_module="tests",
        event_payload={"hello": "world"},
    )
    assert event is not None
    db_session.commit()

    stored = db_session.query(OperationalEvent).filter(OperationalEvent.id == event.id).one()
    assert stored.event_type == "unit_test_event_ok"
    assert stored.company_id == COMPANY_A
    assert stored.event_payload == {"hello": "world"}
