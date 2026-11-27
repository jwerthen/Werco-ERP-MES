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


def test_shop_floor_list_row_carries_the_planned_inspection_type(client: TestClient, db_session: Session):
    """The row serves ``inspection_type`` so the dialog can keep a routing's planned type."""
    quality = make_user(db_session, role=UserRole.QUALITY)
    _, op = _inspection_point(db_session)
    op.inspection_type = "final"
    db_session.commit()

    resp = client.get("/api/v1/shop-floor/operations", headers=headers_for(quality))

    assert resp.status_code == status.HTTP_200_OK, resp.text
    row = next(r for r in resp.json()["operations"] if r["id"] == op.id)
    assert row["inspection_type"] == "final"


# ---------------------------------------------------------------------------
# Refusals: roles, every kind of standing token, tenant, validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", [UserRole.OPERATOR, UserRole.SHIPPING, UserRole.VIEWER])
def test_roles_outside_the_route_are_refused_and_nothing_is_written(client: TestClient, db_session: Session, role):
    caller = make_user(db_session, role=role)
    _, op = _inspection_point(db_session)
    audit_before = db_session.query(AuditLog).count()

    resp = client.post(URL.format(op_id=op.id), json={"notes": "looks fine"}, headers=headers_for(caller))

    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text
    db_session.expire_all()
    assert db_session.get(WorkOrderOperation, op.id).inspection_complete is False
    assert db_session.query(AuditLog).count() == audit_before


@pytest.mark.parametrize(
    "token_role, mint_extra",
    [
        (UserRole.ADMIN, {}),
        (UserRole.SUPERVISOR, {}),
        (UserRole.QUALITY, {"scope": "read_only"}),
    ],
    ids=["full-admin-token", "full-supervisor-token", "read-only-quality-token"],
)
def test_every_standing_api_token_is_refused(client: TestClient, db_session: Session, token_role, mint_extra):
    """Whatever the token's role or scope, a standing token never signs an inspection."""
    admin = make_user(db_session, role=UserRole.ADMIN)
    holder = make_user(db_session, role=token_role)
    _, op = _inspection_point(db_session)
    _, token = issue_token(client, headers_for(admin), holder, **mint_extra)
    audit_before = db_session.query(AuditLog).count()

    resp = client.post(URL.format(op_id=op.id), json={"inspection_type": "final"}, headers=bearer(token))

    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text
    db_session.expire_all()
    refreshed = db_session.get(WorkOrderOperation, op.id)
    assert refreshed.inspection_complete is False
    assert refreshed.inspection_type is None
    assert db_session.query(AuditLog).count() == audit_before


def _kiosk_badge_token(user) -> str:
    from app.core.security import create_access_token

    return create_access_token(subject=user.id, company_id=user.company_id, scope="kiosk")


def test_crew_station_badge_of_an_operator_is_refused_by_role(client: TestClient, db_session: Session):
    operator = make_user(db_session, role=UserRole.OPERATOR)
    _, op = _inspection_point(db_session)

    resp = client.post(URL.format(op_id=op.id), json={}, headers=bearer(_kiosk_badge_token(operator)))

    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text
    db_session.expire_all()
    assert db_session.get(WorkOrderOperation, op.id).inspection_complete is False
    assert _sign_off_rows(db_session, op.id) == []


@pytest.mark.parametrize(
    "role",
    [UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR, UserRole.QUALITY],
    ids=lambda r: r.value,
)
def test_crew_station_badge_of_a_signing_role_is_fenced_off_the_desk_verb(
    client: TestClient, db_session: Session, role
):
    """The sign-off is a desk verb, as labor approval is: the kiosk path fence refuses it.

    A badge token carries its holder's live role, so without the fence a Supervisor or Quality
    badge left live on a shared crew terminal would let whoever stands there next sign an
    inspection in that person's name. The fence answers 403 before the route runs.
    """
    from app.api.deps import _is_kiosk_scope_allowed_path

    signer = make_user(db_session, role=role)
    _, op = _inspection_point(db_session)
    path = URL.format(op_id=op.id)
    assert _is_kiosk_scope_allowed_path(path) is False

    resp = client.post(path, json={}, headers=bearer(_kiosk_badge_token(signer)))

    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text
    db_session.expire_all()
    assert db_session.get(WorkOrderOperation, op.id).inspection_complete is False
    assert _sign_off_rows(db_session, op.id) == []


def test_kiosk_fence_still_admits_the_other_operation_verbs():
    """The inspection carve-out is suffix-exact: the floor's own operation verbs stay reachable."""
    from app.api.deps import _is_kiosk_scope_allowed_path

    assert _is_kiosk_scope_allowed_path("/api/v1/shop-floor/operations/7/start") is True
    assert _is_kiosk_scope_allowed_path("/api/v1/shop-floor/operations/7/complete") is True
    assert _is_kiosk_scope_allowed_path("/api/v1/shop-floor/operations/7") is True


def test_other_tenant_gets_404_with_no_row_change_no_audit_and_no_event(client: TestClient, db_session: Session):
    from tests.api.test_completion_quality_gates_batch4 import COMPANY_B

    outsider = make_user(db_session, role=UserRole.QUALITY, company_id=COMPANY_B)
    _, op = _inspection_point(db_session)
    version = op.version
    audit_before = db_session.query(AuditLog).count()

    resp = client.post(URL.format(op_id=op.id), json={"inspection_type": "final"}, headers=headers_for(outsider))

    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
    assert resp.json()["detail"] == "Operation not found"
    db_session.expire_all()
    refreshed = db_session.get(WorkOrderOperation, op.id)
    assert refreshed.inspection_complete is False
    assert refreshed.inspection_type is None
    assert refreshed.version == version
    assert db_session.query(AuditLog).count() == audit_before
    assert db_session.query(OperationalEvent).filter(OperationalEvent.operation_id == op.id).count() == 0


def test_missing_operation_is_404(client: TestClient, db_session: Session):
    quality = make_user(db_session, role=UserRole.QUALITY)
    resp = client.post(URL.format(op_id=987654), json={}, headers=headers_for(quality))
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text


@pytest.mark.parametrize(
    "body",
    [{"notes": "x" * 2001}, {"inspection_type": "t" * 101}],
    ids=["notes-too-long", "type-too-long"],
)
def test_overlong_fields_are_422_and_sign_nothing(client: TestClient, db_session: Session, body):
    quality = make_user(db_session, role=UserRole.QUALITY)
    _, op = _inspection_point(db_session)

    resp = client.post(URL.format(op_id=op.id), json=body, headers=headers_for(quality))

    assert resp.status_code == 422, resp.text
    db_session.expire_all()
    assert db_session.get(WorkOrderOperation, op.id).inspection_complete is False
    assert _sign_off_rows(db_session, op.id) == []


# ---------------------------------------------------------------------------
# What a sign-off records
# ---------------------------------------------------------------------------


def test_sign_off_records_type_change_and_emits_the_operational_event(client: TestClient, db_session: Session):
    quality = make_user(db_session, role=UserRole.QUALITY)
    wo, op = _inspection_point(db_session)

    resp = client.post(URL.format(op_id=op.id), json={"inspection_type": "first_article"}, headers=headers_for(quality))

    assert resp.status_code == status.HTTP_200_OK, resp.text
    (row,) = _sign_off_rows(db_session, op.id)
    assert row.old_values["inspection_type"] is None
    assert row.new_values["inspection_type"] == "first_article"
    assert row.extra_data["work_order_number"] == wo.work_order_number
    events = (
        db_session.query(OperationalEvent)
        .filter(OperationalEvent.event_type == "operation_inspected", OperationalEvent.operation_id == op.id)
        .all()
    )
    assert len(events) == 1
    assert events[0].user_id == quality.id
    assert events[0].company_id == quality.company_id


def test_sign_off_without_a_type_keeps_the_planned_type(client: TestClient, db_session: Session):
    quality = make_user(db_session, role=UserRole.QUALITY)
    _, op = _inspection_point(db_session)
    op.inspection_type = "final"
    db_session.commit()

    resp = client.post(URL.format(op_id=op.id), json={}, headers=headers_for(quality))

    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["inspection_type"] == "final"
    (row,) = _sign_off_rows(db_session, op.id)
    assert row.old_values["inspection_type"] == row.new_values["inspection_type"] == "final"


def test_sign_off_bumps_the_operation_version_so_a_stale_update_is_409(client: TestClient, db_session: Session):
    """The sign-off is a real write to a version-locked row: an editor holding the pre-sign-off
    version cannot then blind-overwrite the operation."""
    quality = make_user(db_session, role=UserRole.QUALITY)
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    _, op = _inspection_point(db_session)
    stale_version = op.version

    signed = client.post(URL.format(op_id=op.id), json={}, headers=headers_for(quality))
    assert signed.status_code == status.HTTP_200_OK, signed.text
    db_session.expire_all()
    assert db_session.get(WorkOrderOperation, op.id).version > stale_version

    stale = client.put(
        f"/api/v1/work-orders/operations/{op.id}",
        json={"version": stale_version, "requires_inspection": False, "change_reason": "routing test edit"},
        headers=headers_for(supervisor),
    )
    assert stale.status_code == status.HTTP_409_CONFLICT, stale.text
    db_session.expire_all()
    refreshed = db_session.get(WorkOrderOperation, op.id)
    assert refreshed.requires_inspection is True
    assert refreshed.inspection_complete is True


# ---------------------------------------------------------------------------
# The generic operation PUT never writes the flag
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [False, None, "true"], ids=["false", "null", "string"])
def test_generic_update_refuses_inspection_complete_whatever_its_value(client: TestClient, db_session: Session, value):
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    _, op = _inspection_point(db_session)
    op.inspection_complete = True  # signed earlier; a PUT must not be able to clear it either
    db_session.commit()
    version = op.version

    resp = client.put(
        f"/api/v1/work-orders/operations/{op.id}",
        json={"version": version, "inspection_complete": value},
        headers=headers_for(supervisor),
    )

    assert resp.status_code == 422, resp.text
    assert INSPECTION_COMPLETE_NOT_UPDATABLE in resp.text
    db_session.expire_all()
    refreshed = db_session.get(WorkOrderOperation, op.id)
    assert refreshed.inspection_complete is True
    assert refreshed.version == version


def test_generic_update_still_sets_requires_inspection_and_leaves_the_sign_off_alone(
    client: TestClient, db_session: Session
):
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    _, op = _inspection_point(db_session)

    resp = client.put(
        f"/api/v1/work-orders/operations/{op.id}",
        json={"version": op.version, "requires_inspection": False, "change_reason": "routing test edit"},
        headers=headers_for(supervisor),
    )

    assert resp.status_code == status.HTTP_200_OK, resp.text
    db_session.expire_all()
    refreshed = db_session.get(WorkOrderOperation, op.id)
    assert refreshed.requires_inspection is False
    assert refreshed.inspection_complete is False
    assert _sign_off_rows(db_session, op.id) == []


# ---------------------------------------------------------------------------
# Reads never sign
# ---------------------------------------------------------------------------


def test_operation_reads_never_sign_or_write_a_sign_off_row(client: TestClient, db_session: Session):
    quality = make_user(db_session, role=UserRole.QUALITY)
    _, op = _inspection_point(db_session)

    detail = client.get(f"/api/v1/shop-floor/operations/{op.id}", headers=headers_for(quality))
    listing = client.get("/api/v1/shop-floor/operations", headers=headers_for(quality))

    assert detail.status_code == listing.status_code == status.HTTP_200_OK
    assert detail.json()["operation"]["inspection_complete"] is False
    db_session.expire_all()
    assert db_session.get(WorkOrderOperation, op.id).inspection_complete is False
    assert _sign_off_rows(db_session, op.id) == []
    assert (
        db_session.query(OperationalEvent)
        .filter(OperationalEvent.event_type == "operation_inspected", OperationalEvent.operation_id == op.id)
        .count()
        == 0
    )
