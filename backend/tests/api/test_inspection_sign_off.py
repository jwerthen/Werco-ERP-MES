"""The in-process inspection sign-off: ``POST /shop-floor/operations/{id}/inspection``.

Pins the contract the Work Order Detail and Shop Floor screens call:

- the response carries who signed and when (read from the audit row the verb just wrote --
  the operation has no inspected-by / inspected-at column);
- the audit row is REQUIRED evidence: when it cannot be written nothing is signed (503);
- a standing API token is refused 403 before anything is read or written, as an FAI final
  disposition is (a sign-off is a person's judgment), and the route is not an MCP tool;
- an operation on a deleted work order is 404;
- the generic operation ``PUT`` refuses ``inspection_complete`` (422), so the audited verb is the
  only writer of the flag.

The happy path, operator 403 and cross-tenant 404 live in ``test_completion_quality_gates_batch4.py``.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.credentials import API_TOKEN_INSPECTION_SIGN_OFF
from app.models.audit_log import AuditLog
from app.models.operational_event import OperationalEvent
from app.models.user import UserRole
from app.models.work_order import OperationStatus, WorkOrderOperation, WorkOrderStatus
from app.schemas.work_order import INSPECTION_COMPLETE_NOT_UPDATABLE
from app.services.audit_service import AuditService, AuditWriteError
from tests.api.export_control_helpers import bearer, issue_token
from tests.api.test_completion_quality_gates_batch4 import (
    headers_for,
    make_op,
    make_part,
    make_user,
    make_wo,
    make_work_center,
)

pytestmark = pytest.mark.api

URL = "/api/v1/shop-floor/operations/{op_id}/inspection"


def _inspection_point(db: Session):
    part = make_part(db)
    wo = make_wo(db, part, status_=WorkOrderStatus.IN_PROGRESS)
    wc = make_work_center(db)
    op = make_op(db, wo, wc, sequence=10, status_=OperationStatus.IN_PROGRESS, requires_inspection=True)
    db.commit()
    return wo, op


def _sign_off_rows(db: Session, op_id: int):
    return db.query(AuditLog).filter(AuditLog.action == "MARK_OPERATION_INSPECTED", AuditLog.resource_id == op_id).all()


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR, UserRole.QUALITY])
def test_sign_off_answers_who_and_when_from_its_audit_row(client: TestClient, db_session: Session, role):
    signer = make_user(db_session, role=role)
    wo, op = _inspection_point(db_session)

    resp = client.post(
        URL.format(op_id=op.id),
        json={"inspection_type": "in_process", "notes": "bore 0.500 in tolerance"},
        headers=headers_for(signer),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["operation_id"] == op.id
    assert body["work_order_id"] == wo.id
    assert body["requires_inspection"] is True
    assert body["inspection_complete"] is True
    assert body["inspection_type"] == "in_process"
    assert body["signed_off_by_id"] == signer.id
    assert body["signed_off_by_name"] == signer.full_name
    assert body["signed_off_at"].endswith("Z")

    rows = _sign_off_rows(db_session, op.id)
    assert len(rows) == 1
    row = rows[0]
    assert row.user_id == signer.id
    assert row.company_id == signer.company_id
    assert row.new_values["inspection_complete"] is True
    assert row.old_values["inspection_complete"] is False
    assert row.extra_data["notes"] == "bore 0.500 in tolerance"
    assert row.extra_data["work_order_id"] == wo.id
    signed_at = datetime.fromisoformat(body["signed_off_at"].rstrip("Z"))
    assert abs((signed_at - row.timestamp).total_seconds()) < 1


def test_api_token_is_refused_before_anything_is_written(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN)
    quality = make_user(db_session, role=UserRole.QUALITY)
    _, op = _inspection_point(db_session)
    _, token = issue_token(client, headers_for(admin), quality)
    audit_before = db_session.query(AuditLog).count()
    events_before = db_session.query(OperationalEvent).count()

    resp = client.post(URL.format(op_id=op.id), json={"notes": "agent says fine"}, headers=bearer(token))

    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text
    assert resp.json()["detail"] == API_TOKEN_INSPECTION_SIGN_OFF
    db_session.expire_all()
    assert db_session.get(WorkOrderOperation, op.id).inspection_complete is False
    assert _sign_off_rows(db_session, op.id) == []
    assert db_session.query(OperationalEvent).count() == events_before
    # The token mint wrote its own audit rows before the probe; the probe wrote none.
    assert db_session.query(AuditLog).count() == audit_before


def test_api_token_refusal_does_not_disclose_whether_the_operation_exists(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN)
    quality = make_user(db_session, role=UserRole.QUALITY)
    _, token = issue_token(client, headers_for(admin), quality)

    resp = client.post(URL.format(op_id=999999), json={}, headers=bearer(token))

    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text
    assert resp.json()["detail"] == API_TOKEN_INSPECTION_SIGN_OFF


def test_audit_outage_signs_nothing(client: TestClient, db_session: Session, monkeypatch):
    quality = make_user(db_session, role=UserRole.QUALITY)
    _, op = _inspection_point(db_session)

    def outage(self, action, resource_type, **kwargs):
        raise AuditWriteError("simulated audit outage")

    monkeypatch.setattr(AuditService, "log_required", outage)
    resp = client.post(URL.format(op_id=op.id), json={}, headers=headers_for(quality))

    assert resp.status_code == status.HTTP_503_SERVICE_UNAVAILABLE, resp.text
    assert "nothing was signed off" in resp.json()["detail"]
    db_session.expire_all()
    assert db_session.get(WorkOrderOperation, op.id).inspection_complete is False
    assert (
        db_session.query(OperationalEvent)
        .filter(OperationalEvent.event_type == "operation_inspected", OperationalEvent.operation_id == op.id)
        .count()
        == 0
    )


def test_operation_on_a_deleted_work_order_is_404(client: TestClient, db_session: Session):
    quality = make_user(db_session, role=UserRole.QUALITY)
    wo, op = _inspection_point(db_session)
    wo.soft_delete(quality.id)
    db_session.commit()

    resp = client.post(URL.format(op_id=op.id), json={}, headers=headers_for(quality))

    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
    db_session.expire_all()
    assert db_session.get(WorkOrderOperation, op.id).inspection_complete is False
    assert _sign_off_rows(db_session, op.id) == []


def test_signing_again_is_allowed_and_records_a_further_row(client: TestClient, db_session: Session):
    quality = make_user(db_session, role=UserRole.QUALITY)
    _, op = _inspection_point(db_session)

    first = client.post(URL.format(op_id=op.id), json={}, headers=headers_for(quality))
    second = client.post(URL.format(op_id=op.id), json={}, headers=headers_for(quality))

    assert first.status_code == second.status_code == status.HTTP_200_OK
    rows = sorted(_sign_off_rows(db_session, op.id), key=lambda r: r.id)
    assert [r.old_values["inspection_complete"] for r in rows] == [False, True]


def test_generic_operation_update_refuses_inspection_complete(client: TestClient, db_session: Session):
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    _, op = _inspection_point(db_session)
    version = op.version

    resp = client.put(
        f"/api/v1/work-orders/operations/{op.id}",
        json={"version": version, "inspection_complete": True},
        headers=headers_for(supervisor),
    )

    assert resp.status_code == 422, resp.text
    assert INSPECTION_COMPLETE_NOT_UPDATABLE in resp.text
    db_session.expire_all()
    refreshed = db_session.get(WorkOrderOperation, op.id)
    assert refreshed.inspection_complete is False
    assert refreshed.version == version


def test_generic_operation_update_schema_has_no_inspection_complete():
    from app.main import app

    schema = app.openapi()["components"]["schemas"]["WorkOrderOperationUpdate"]
    assert "inspection_complete" not in schema["properties"]
    assert "requires_inspection" in schema["properties"]


def test_shop_floor_list_row_reports_whether_the_inspection_is_signed(client: TestClient, db_session: Session):
    """The Shop Floor desk rows show "Inspection required" / "Inspected" from the list read alone."""
    quality = make_user(db_session, role=UserRole.QUALITY)
    _, op = _inspection_point(db_session)

    def _row():
        resp = client.get("/api/v1/shop-floor/operations", headers=headers_for(quality))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        return next(r for r in resp.json()["operations"] if r["id"] == op.id)

    before = _row()
    assert before["requires_inspection"] is True
    assert before["inspection_complete"] is False

    signed = client.post(URL.format(op_id=op.id), json={}, headers=headers_for(quality))
    assert signed.status_code == status.HTTP_200_OK, signed.text

    assert _row()["inspection_complete"] is True
