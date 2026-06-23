"""Coverage for the routing time-standard editing rules on ``update_operation``.

The ``PUT /api/v1/routing/{routing_id}/operations/{operation_id}`` endpoint no
longer hard-blocks released routings. The rules under test:

* DRAFT  -> every field editable, and the change is now audit-logged.
* RELEASED -> only time-standard fields may change; any other changed field is a
  400 with a fixed contract message; a time-only change persists, recalculates
  routing totals, and writes an audit_log UPDATE row.
* OBSOLETE -> fully locked (400).
* RBAC -> the endpoint requires ADMIN/MANAGER/SUPERVISOR; a lower role gets 403.
* add/delete operation are audit-logged and still block on a released routing;
  release writes a STATUS_CHANGE audit row.
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.part import Part
from app.models.routing import Routing, RoutingOperation
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter

# The exact contract message the frontend asserts against. It contains an em dash.
RELEASED_EDIT_MESSAGE = (
    "Released routing: only time standards (setup, run/unit, move, queue, cycle) "
    "can be edited — create a new revision to change the process."
)

OBSOLETE_EDIT_MESSAGE = "Cannot modify an obsolete routing"
RELEASED_MODIFY_MESSAGE = "Cannot modify released routing"


def _audit_rows(db: Session, *, resource_type: str, resource_id: int, action: str = None):
    """Fetch committed AuditLog rows for a resource, newest first.

    ``expire_all`` first so rows committed through the endpoint's session (the
    same ``db_session`` the client overrides ``get_db`` with) are reloaded
    instead of served stale from the identity map.
    """
    db.expire_all()
    q = db.query(AuditLog).filter(
        AuditLog.resource_type == resource_type,
        AuditLog.resource_id == resource_id,
    )
    if action is not None:
        q = q.filter(AuditLog.action == action)
    return q.order_by(AuditLog.sequence_number.desc()).all()


def _supervisor_headers(db: Session) -> dict:
    """Mint a SUPERVISOR user + token (no shared fixture exists for this role).

    SUPERVISOR clears the decorator-level ``require_role`` on ``update_operation`` (which admits
    ADMIN/MANAGER/SUPERVISOR for draft edits) but must be rejected by the in-handler released-path
    check, so it is the role that actually exercises the new 403 rule.
    """
    user = User(
        email="supervisor-routing-ts@werco.com",
        employee_id="EMP-SUP-RT-TS",
        first_name="Sup",
        last_name="Visor",
        hashed_password="$2b$12$abcdefghijklmnopqrstuv",
        role=UserRole.SUPERVISOR,
        is_active=True,
        company_id=1,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    token = create_access_token(subject=user.id, company_id=user.company_id)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def _make_part(db: Session, part_number: str) -> Part:
    part = Part(
        part_number=part_number,
        name="Time Standard Part",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=1,
    )
    db.add(part)
    db.flush()
    return part


def _make_work_center(db: Session, code: str, name: str, hourly_rate: float = 100.0) -> WorkCenter:
    work_center = WorkCenter(
        code=code,
        name=name,
        work_center_type="machining",
        hourly_rate=hourly_rate,
        is_active=True,
        company_id=1,
    )
    db.add(work_center)
    db.flush()
    return work_center


def _make_routing(
    db: Session,
    *,
    part_id: int,
    status_value: str,
    work_center_id: int,
    setup_hours: float = 1.0,
    run_hours_per_unit: float = 0.5,
) -> tuple[Routing, RoutingOperation]:
    """Create a routing in the given status with one operation, totals pre-computed."""
    routing = Routing(
        part_id=part_id,
        revision="A",
        status=status_value,
        is_active=status_value != "obsolete",
        total_setup_hours=setup_hours,
        total_run_hours_per_unit=run_hours_per_unit,
        company_id=1,
    )
    db.add(routing)
    db.flush()
    operation = RoutingOperation(
        routing_id=routing.id,
        sequence=10,
        operation_number="Op 10",
        name="Mill Face",
        work_center_id=work_center_id,
        setup_hours=setup_hours,
        run_hours_per_unit=run_hours_per_unit,
        move_hours=0.0,
        queue_hours=0.0,
        pieces_per_cycle=1,
        is_active=True,
        company_id=1,
    )
    db.add(operation)
    db.commit()
    db.refresh(routing)
    db.refresh(operation)
    return routing, operation


@pytest.mark.api
@pytest.mark.requires_db
class TestRoutingTimeStandardEditing:
    # ------------------------------------------------------------------
    # 1. Released + time-only update -> 200, persisted, totals recalculated, audited
    # ------------------------------------------------------------------
    def test_released_time_standard_update_succeeds_and_audits(
        self, client: TestClient, manager_headers: dict, test_user: User, db_session: Session
    ):
        part = _make_part(db_session, "RT-TS-001")
        wc = _make_work_center(db_session, "WC-TS-001", "TS Mill")
        routing, operation = _make_routing(
            db_session,
            part_id=part.id,
            status_value="released",
            work_center_id=wc.id,
            setup_hours=1.0,
            run_hours_per_unit=0.5,
        )
        # Released by someone else originally; the edit should re-stamp it to the editor.
        assert routing.approved_by is None

        response = client.put(
            f"/api/v1/routing/{routing.id}/operations/{operation.id}",
            headers=manager_headers,
            json={"setup_hours": 3.25},
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["setup_hours"] == 3.25

        db_session.expire_all()
        reloaded_op = db_session.get(RoutingOperation, operation.id)
        assert reloaded_op.setup_hours == 3.25

        # Routing totals recalculated to include the new setup value.
        reloaded_routing = db_session.get(Routing, routing.id)
        assert reloaded_routing.total_setup_hours == 3.25
        assert reloaded_routing.total_run_hours_per_unit == 0.5

        # Re-approval stamp: the routing's approval signature now reflects the editor + now,
        # while the revision letter is unchanged (in-place edit, not a new revision).
        assert reloaded_routing.approved_by == test_user.id
        assert reloaded_routing.approved_at is not None
        assert reloaded_routing.revision == "A"

        # An UPDATE audit row for the operation exists.
        rows = _audit_rows(
            db_session,
            resource_type="routing_operation",
            resource_id=operation.id,
            action="UPDATE",
        )
        assert len(rows) == 1

    # ------------------------------------------------------------------
    # 2. Released + non-time field change -> 400, nothing changed, no audit row
    # ------------------------------------------------------------------
    def test_released_non_time_field_change_is_rejected(
        self, client: TestClient, manager_headers: dict, db_session: Session
    ):
        part = _make_part(db_session, "RT-TS-002")
        wc = _make_work_center(db_session, "WC-TS-002", "TS Mill 2")
        routing, operation = _make_routing(
            db_session,
            part_id=part.id,
            status_value="released",
            work_center_id=wc.id,
        )
        original_name = operation.name

        response = client.put(
            f"/api/v1/routing/{routing.id}/operations/{operation.id}",
            headers=manager_headers,
            json={"name": "Renamed Operation"},
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.json()["detail"] == RELEASED_EDIT_MESSAGE

        # Nothing changed: name is unchanged and no UPDATE audit row was written.
        db_session.expire_all()
        reloaded_op = db_session.get(RoutingOperation, operation.id)
        assert reloaded_op.name == original_name

        rows = _audit_rows(
            db_session,
            resource_type="routing_operation",
            resource_id=operation.id,
            action="UPDATE",
        )
        assert rows == []

    def test_released_work_center_change_is_rejected(
        self, client: TestClient, manager_headers: dict, db_session: Session
    ):
        """A work-center swap is a process change, blocked on released routings."""
        part = _make_part(db_session, "RT-TS-002B")
        wc = _make_work_center(db_session, "WC-TS-002B", "TS Mill 2B")
        other_wc = _make_work_center(db_session, "WC-TS-002C", "TS Lathe 2C")
        routing, operation = _make_routing(
            db_session,
            part_id=part.id,
            status_value="released",
            work_center_id=wc.id,
        )

        response = client.put(
            f"/api/v1/routing/{routing.id}/operations/{operation.id}",
            headers=manager_headers,
            json={"work_center_id": other_wc.id},
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.json()["detail"] == RELEASED_EDIT_MESSAGE

        db_session.expire_all()
        reloaded_op = db_session.get(RoutingOperation, operation.id)
        assert reloaded_op.work_center_id == wc.id

    # ------------------------------------------------------------------
    # 3. Released + mixed payload (time + non-time) -> 400, NOTHING applied
    # ------------------------------------------------------------------
    def test_released_mixed_payload_rejected_before_any_mutation(
        self, client: TestClient, manager_headers: dict, db_session: Session
    ):
        part = _make_part(db_session, "RT-TS-003")
        wc = _make_work_center(db_session, "WC-TS-003", "TS Mill 3")
        routing, operation = _make_routing(
            db_session,
            part_id=part.id,
            status_value="released",
            work_center_id=wc.id,
            setup_hours=1.0,
        )

        response = client.put(
            f"/api/v1/routing/{routing.id}/operations/{operation.id}",
            headers=manager_headers,
            json={"setup_hours": 9.0, "name": "Renamed Op"},
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.json()["detail"] == RELEASED_EDIT_MESSAGE

        # The gate runs before mutation: the time field must NOT have persisted either.
        db_session.expire_all()
        reloaded_op = db_session.get(RoutingOperation, operation.id)
        assert reloaded_op.setup_hours == 1.0
        assert reloaded_op.name == "Mill Face"

        rows = _audit_rows(
            db_session,
            resource_type="routing_operation",
            resource_id=operation.id,
            action="UPDATE",
        )
        assert rows == []

    def test_released_unchanged_non_time_field_is_not_a_change(
        self, client: TestClient, manager_headers: dict, db_session: Session
    ):
        """Sending a non-time field with its CURRENT value is not a change -> 200."""
        part = _make_part(db_session, "RT-TS-003B")
        wc = _make_work_center(db_session, "WC-TS-003B", "TS Mill 3B")
        routing, operation = _make_routing(
            db_session,
            part_id=part.id,
            status_value="released",
            work_center_id=wc.id,
            setup_hours=2.0,
        )

        response = client.put(
            f"/api/v1/routing/{routing.id}/operations/{operation.id}",
            headers=manager_headers,
            # name unchanged (== current), setup_hours actually changes.
            json={"name": operation.name, "setup_hours": 4.0},
        )

        assert response.status_code == status.HTTP_200_OK
        db_session.expire_all()
        reloaded_op = db_session.get(RoutingOperation, operation.id)
        assert reloaded_op.setup_hours == 4.0

    # ------------------------------------------------------------------
    # 4. Draft + full update (incl. non-time field) -> 200, all applied, audited
    # ------------------------------------------------------------------
    def test_draft_full_update_applies_all_fields_and_audits(
        self, client: TestClient, manager_headers: dict, db_session: Session
    ):
        part = _make_part(db_session, "RT-TS-004")
        wc = _make_work_center(db_session, "WC-TS-004", "TS Mill 4")
        routing, operation = _make_routing(
            db_session,
            part_id=part.id,
            status_value="draft",
            work_center_id=wc.id,
            setup_hours=1.0,
        )

        response = client.put(
            f"/api/v1/routing/{routing.id}/operations/{operation.id}",
            headers=manager_headers,
            json={"name": "Deburr", "setup_hours": 2.5},
        )

        assert response.status_code == status.HTTP_200_OK
        db_session.expire_all()
        reloaded_op = db_session.get(RoutingOperation, operation.id)
        assert reloaded_op.name == "Deburr"
        assert reloaded_op.setup_hours == 2.5

        reloaded_routing = db_session.get(Routing, routing.id)
        assert reloaded_routing.total_setup_hours == 2.5

        # A draft edit must NOT re-stamp approval -- the routing is not yet approved/released.
        assert reloaded_routing.approved_by is None
        assert reloaded_routing.approved_at is None

        # This draft-edit path was previously unlogged -- now it writes an UPDATE row.
        rows = _audit_rows(
            db_session,
            resource_type="routing_operation",
            resource_id=operation.id,
            action="UPDATE",
        )
        assert len(rows) == 1

    # ------------------------------------------------------------------
    # 5. Obsolete + any update -> 400 with the obsolete message
    # ------------------------------------------------------------------
    def test_obsolete_update_is_rejected(self, client: TestClient, manager_headers: dict, db_session: Session):
        part = _make_part(db_session, "RT-TS-005")
        wc = _make_work_center(db_session, "WC-TS-005", "TS Mill 5")
        routing, operation = _make_routing(
            db_session,
            part_id=part.id,
            status_value="obsolete",
            work_center_id=wc.id,
            setup_hours=1.0,
        )

        response = client.put(
            f"/api/v1/routing/{routing.id}/operations/{operation.id}",
            headers=manager_headers,
            json={"setup_hours": 7.0},
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.json()["detail"] == OBSOLETE_EDIT_MESSAGE

        db_session.expire_all()
        reloaded_op = db_session.get(RoutingOperation, operation.id)
        assert reloaded_op.setup_hours == 1.0

    # ------------------------------------------------------------------
    # 6. RBAC: a non-privileged role editing a released time standard -> 403
    # ------------------------------------------------------------------
    def test_operator_cannot_edit_released_time_standard(
        self, client: TestClient, operator_headers: dict, db_session: Session
    ):
        part = _make_part(db_session, "RT-TS-006")
        wc = _make_work_center(db_session, "WC-TS-006", "TS Mill 6")
        routing, operation = _make_routing(
            db_session,
            part_id=part.id,
            status_value="released",
            work_center_id=wc.id,
            setup_hours=1.0,
        )

        response = client.put(
            f"/api/v1/routing/{routing.id}/operations/{operation.id}",
            headers=operator_headers,
            json={"setup_hours": 5.0},
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

        # The dependency rejected before any handler logic: value unchanged.
        db_session.expire_all()
        reloaded_op = db_session.get(RoutingOperation, operation.id)
        assert reloaded_op.setup_hours == 1.0

    # ------------------------------------------------------------------
    # 6b. RBAC: SUPERVISOR clears the decorator but is blocked on the released path -> 403
    # ------------------------------------------------------------------
    def test_supervisor_cannot_edit_released_time_standard(self, client: TestClient, db_session: Session):
        """SUPERVISOR may edit DRAFT routings but not RELEASED ones (release-adjacent authority).

        Unlike OPERATOR, SUPERVISOR passes the decorator-level ``require_role`` -- so this 403 comes
        from the in-handler released-path check, not the dependency. Routing release/edit on live
        content is Admin/Manager only per docs/RBAC_PERMISSIONS.md.
        """
        headers = _supervisor_headers(db_session)
        part = _make_part(db_session, "RT-TS-007")
        wc = _make_work_center(db_session, "WC-TS-007", "TS Mill 7")
        routing, operation = _make_routing(
            db_session,
            part_id=part.id,
            status_value="released",
            work_center_id=wc.id,
            setup_hours=1.0,
        )

        response = client.put(
            f"/api/v1/routing/{routing.id}/operations/{operation.id}",
            headers=headers,
            json={"setup_hours": 5.0},
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

        # Nothing applied: time standard unchanged, no re-approval stamp, no UPDATE audit row.
        db_session.expire_all()
        reloaded_op = db_session.get(RoutingOperation, operation.id)
        assert reloaded_op.setup_hours == 1.0
        reloaded_routing = db_session.get(Routing, routing.id)
        assert reloaded_routing.approved_by is None
        assert reloaded_routing.approved_at is None

        rows = _audit_rows(
            db_session,
            resource_type="routing_operation",
            resource_id=operation.id,
            action="UPDATE",
        )
        assert rows == []

    # ------------------------------------------------------------------
    # 6c. RBAC: SUPERVISOR CAN still edit a DRAFT routing -> 200 (draft path unchanged)
    # ------------------------------------------------------------------
    def test_supervisor_can_edit_draft_time_standard(self, client: TestClient, db_session: Session):
        """Regression: the draft edit path stays open to SUPERVISOR -- only the released path tightened."""
        headers = _supervisor_headers(db_session)
        part = _make_part(db_session, "RT-TS-008")
        wc = _make_work_center(db_session, "WC-TS-008", "TS Mill 8")
        routing, operation = _make_routing(
            db_session,
            part_id=part.id,
            status_value="draft",
            work_center_id=wc.id,
            setup_hours=1.0,
        )

        response = client.put(
            f"/api/v1/routing/{routing.id}/operations/{operation.id}",
            headers=headers,
            json={"setup_hours": 6.0},
        )

        assert response.status_code == status.HTTP_200_OK
        db_session.expire_all()
        reloaded_op = db_session.get(RoutingOperation, operation.id)
        assert reloaded_op.setup_hours == 6.0
        # Draft edits never re-stamp approval.
        reloaded_routing = db_session.get(Routing, routing.id)
        assert reloaded_routing.approved_by is None


@pytest.mark.api
@pytest.mark.requires_db
class TestRoutingOperationLifecycleAudit:
    # ------------------------------------------------------------------
    # 7a. add_operation on a draft -> 200 + CREATE audit row
    # ------------------------------------------------------------------
    def test_add_operation_on_draft_audits_create(self, client: TestClient, manager_headers: dict, db_session: Session):
        part = _make_part(db_session, "RT-LC-001")
        wc = _make_work_center(db_session, "WC-LC-001", "LC Mill 1")
        routing = Routing(
            part_id=part.id,
            revision="A",
            status="draft",
            is_active=True,
            company_id=1,
        )
        db_session.add(routing)
        db_session.commit()
        db_session.refresh(routing)

        response = client.post(
            f"/api/v1/routing/{routing.id}/operations",
            headers=manager_headers,
            json={
                "sequence": 10,
                "name": "Saw",
                "work_center_id": wc.id,
                "setup_hours": 0.5,
                "run_hours_per_unit": 0.2,
            },
        )

        assert response.status_code == status.HTTP_200_OK
        new_op_id = response.json()["id"]

        rows = _audit_rows(
            db_session,
            resource_type="routing_operation",
            resource_id=new_op_id,
            action="CREATE",
        )
        assert len(rows) == 1

    # ------------------------------------------------------------------
    # 7b. delete_operation on a draft -> success + DELETE audit row
    # ------------------------------------------------------------------
    def test_delete_operation_on_draft_audits_delete(
        self, client: TestClient, manager_headers: dict, db_session: Session
    ):
        part = _make_part(db_session, "RT-LC-002")
        wc = _make_work_center(db_session, "WC-LC-002", "LC Mill 2")
        routing, operation = _make_routing(
            db_session,
            part_id=part.id,
            status_value="draft",
            work_center_id=wc.id,
        )

        response = client.delete(
            f"/api/v1/routing/{routing.id}/operations/{operation.id}",
            headers=manager_headers,
        )

        assert response.status_code == status.HTTP_200_OK

        rows = _audit_rows(
            db_session,
            resource_type="routing_operation",
            resource_id=operation.id,
            action="DELETE",
        )
        assert len(rows) == 1

        # Operation row really gone (hard delete).
        db_session.expire_all()
        assert db_session.get(RoutingOperation, operation.id) is None

    # ------------------------------------------------------------------
    # 7c. release_routing (draft w/ >=1 op) -> success + STATUS_CHANGE audit row
    # ------------------------------------------------------------------
    def test_release_routing_audits_status_change(self, client: TestClient, manager_headers: dict, db_session: Session):
        part = _make_part(db_session, "RT-LC-003")
        wc = _make_work_center(db_session, "WC-LC-003", "LC Mill 3")
        routing, _operation = _make_routing(
            db_session,
            part_id=part.id,
            status_value="draft",
            work_center_id=wc.id,
        )

        response = client.post(
            f"/api/v1/routing/{routing.id}/release",
            headers=manager_headers,
        )

        assert response.status_code == status.HTTP_200_OK

        db_session.expire_all()
        reloaded_routing = db_session.get(Routing, routing.id)
        assert reloaded_routing.status == "released"

        rows = _audit_rows(
            db_session,
            resource_type="routing",
            resource_id=routing.id,
            action="STATUS_CHANGE",
        )
        assert len(rows) == 1
        assert rows[0].old_values == {"status": "draft"}
        assert rows[0].new_values == {"status": "released"}

    # ------------------------------------------------------------------
    # 8. Regression: add/delete on a RELEASED routing still 400
    # ------------------------------------------------------------------
    def test_add_operation_on_released_routing_still_blocked(
        self, client: TestClient, manager_headers: dict, db_session: Session
    ):
        part = _make_part(db_session, "RT-LC-004")
        wc = _make_work_center(db_session, "WC-LC-004", "LC Mill 4")
        routing, _operation = _make_routing(
            db_session,
            part_id=part.id,
            status_value="released",
            work_center_id=wc.id,
        )

        response = client.post(
            f"/api/v1/routing/{routing.id}/operations",
            headers=manager_headers,
            json={
                "sequence": 20,
                "name": "Extra Op",
                "work_center_id": wc.id,
                "setup_hours": 0.1,
                "run_hours_per_unit": 0.1,
            },
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.json()["detail"] == RELEASED_MODIFY_MESSAGE

    def test_delete_operation_on_released_routing_still_blocked(
        self, client: TestClient, manager_headers: dict, db_session: Session
    ):
        part = _make_part(db_session, "RT-LC-005")
        wc = _make_work_center(db_session, "WC-LC-005", "LC Mill 5")
        routing, operation = _make_routing(
            db_session,
            part_id=part.id,
            status_value="released",
            work_center_id=wc.id,
        )

        response = client.delete(
            f"/api/v1/routing/{routing.id}/operations/{operation.id}",
            headers=manager_headers,
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.json()["detail"] == RELEASED_MODIFY_MESSAGE

        db_session.expire_all()
        assert db_session.get(RoutingOperation, operation.id) is not None
