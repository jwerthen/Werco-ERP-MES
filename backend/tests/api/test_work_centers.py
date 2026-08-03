import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrderStatus
from tests.api.kiosk_test_helpers import COMPANY_A, make_wo_with_operation, make_work_center


@pytest.mark.api
@pytest.mark.requires_db
class TestWorkCenters:
    def test_update_work_center_type_persists(
        self,
        client: TestClient,
        auth_headers: dict,
        db_session: Session,
        test_work_center: WorkCenter,
    ):
        response = client.put(
            f"/api/v1/work-centers/{test_work_center.id}",
            headers=auth_headers,
            json={
                "version": getattr(test_work_center, "version", 0),
                "name": test_work_center.name,
                "work_center_type": "laser",
            },
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["work_center_type"] == "laser"

        db_session.refresh(test_work_center)
        assert test_work_center.work_center_type == "laser"


@pytest.mark.api
@pytest.mark.requires_db
class TestDeactivateWorkCenter:
    """Deactivation is refused while live work still references the machine.

    Deactivating a work center hides its column from the dispatch board while
    the operator kiosk keeps serving the queue -- stranded, invisible to the
    planner. So both DELETE and the PUT ``is_active`` False flip 409 until the
    queue is drained, and the clear paths now write the previously-missing
    audit rows.
    """

    @staticmethod
    def _committed_audit_rows(db: Session, work_center_id: int) -> list:
        """AuditLog rows for the work center that actually COMMITTED.

        Mirrors ``test_work_orders_audit_persistence._committed_audit_rows``:
        the ``client`` fixture shares one open transaction with the endpoint,
        so an audit row that was merely flushed (never committed) is still
        visible to a naive query. Rolling back BEFORE querying discards it --
        only a committed row survives the rollback.
        """
        db.rollback()
        db.expire_all()
        return (
            db.query(AuditLog)
            .filter(AuditLog.resource_type == "work_center", AuditLog.resource_id == work_center_id)
            .order_by(AuditLog.sequence_number.desc())
            .all()
        )

    def test_delete_with_ready_op_is_refused_and_untouched(
        self, client: TestClient, admin_headers: dict, db_session: Session
    ):
        wc = make_work_center(db_session)
        make_wo_with_operation(db_session, work_center=wc)  # RELEASED WO + READY op

        resp = client.delete(f"/api/v1/work-centers/{wc.id}", headers=admin_headers)

        assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
        detail = resp.json()["detail"]
        assert "deactivate" in detail.lower()
        assert wc.code in detail
        assert "1 ready" in detail  # the per-status breakdown
        # Refused-and-untouched: the flag never flipped.
        db_session.expire_all()
        assert db_session.get(WorkCenter, wc.id).is_active is True

    def test_delete_with_pending_op_is_refused(self, client: TestClient, admin_headers: dict, db_session: Session):
        """The guard is BROADER than the dispatch queue: PENDING work is off the
        board (READY/IN_PROGRESS only) but still routed to this machine, so it
        still blocks deactivation."""
        wc = make_work_center(db_session)
        make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.PENDING)

        resp = client.delete(f"/api/v1/work-centers/{wc.id}", headers=admin_headers)

        assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
        assert "1 pending" in resp.json()["detail"]
        db_session.expire_all()
        assert db_session.get(WorkCenter, wc.id).is_active is True

    def test_delete_ignores_dead_work_and_writes_committed_audit_row(
        self, client: TestClient, admin_headers: dict, db_session: Session
    ):
        """COMPLETE ops, ops on terminal WOs and ops on soft-deleted WOs do not
        block deactivation -- and the previously-missing audit row commits."""
        wc = make_work_center(db_session)
        make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.COMPLETE)
        make_wo_with_operation(db_session, work_center=wc, wo_status=WorkOrderStatus.CANCELLED)
        deleted_wo, _ = make_wo_with_operation(db_session, work_center=wc)
        deleted_wo.is_deleted = True
        db_session.commit()
        wc_id, wc_code = wc.id, wc.code

        resp = client.delete(f"/api/v1/work-centers/{wc_id}", headers=admin_headers)

        assert resp.status_code == status.HTTP_200_OK, resp.text
        db_session.expire_all()
        assert db_session.get(WorkCenter, wc_id).is_active is False

        rows = self._committed_audit_rows(db_session, wc_id)
        assert len(rows) == 1
        row = rows[0]
        assert row.action == "UPDATE"
        assert row.resource_identifier == wc_code
        assert row.company_id == COMPANY_A
        assert row.extra_data["changes"]["is_active"] == {"old": True, "new": False}

    def test_put_is_active_false_refused_and_nothing_mutated(
        self, client: TestClient, admin_headers: dict, db_session: Session
    ):
        """Flipping ``is_active`` off via PUT is the same action as DELETE, so
        the same guard applies -- checked BEFORE the setattr loop, so a refusal
        leaves EVERY field untouched, not just the flag."""
        wc = make_work_center(db_session)
        make_wo_with_operation(db_session, work_center=wc)
        original_name = wc.name

        resp = client.put(
            f"/api/v1/work-centers/{wc.id}",
            headers=admin_headers,
            # ``version`` is required by the schema but fake on WorkCenter (no
            # model column) -- unrelated to the guard under test.
            json={"version": 0, "name": "Should Not Stick", "is_active": False},
        )

        assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
        assert "deactivate" in resp.json()["detail"].lower()
        db_session.expire_all()
        refreshed = db_session.get(WorkCenter, wc.id)
        assert refreshed.is_active is True
        assert refreshed.name == original_name
        assert self._committed_audit_rows(db_session, wc.id) == []

    def test_put_update_writes_committed_audit_row(self, client: TestClient, admin_headers: dict, db_session: Session):
        wc = make_work_center(db_session)
        old_name = wc.name
        wc_id, wc_code = wc.id, wc.code

        resp = client.put(
            f"/api/v1/work-centers/{wc_id}",
            headers=admin_headers,
            json={"version": 0, "name": "Renamed Bay"},
        )

        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert resp.json()["name"] == "Renamed Bay"

        rows = self._committed_audit_rows(db_session, wc_id)
        assert len(rows) == 1
        row = rows[0]
        assert row.action == "UPDATE"
        assert row.resource_identifier == wc_code
        assert row.company_id == COMPANY_A
        assert row.extra_data["changes"]["name"] == {"old": old_name, "new": "Renamed Bay"}
        # The schema's fake ``version`` field must never leak into the diff.
        assert "version" not in row.extra_data["changes"]

    def test_put_explicit_null_is_active_is_no_change(
        self, client: TestClient, admin_headers: dict, db_session: Session
    ):
        """An explicit ``"is_active": null`` must be dropped, not written.

        The column is a nullable Boolean: SQL NULL slips past the ``is False``
        deactivation guard yet matches NEITHER board query (active columns
        filter ``== True``, flagged columns ``.isnot(True)`` -- but the flagged
        query only exists for pre-existing data), so a null write would hide a
        machine with live work from every planner surface. The endpoint treats
        it as no-change."""
        wc = make_work_center(db_session)
        make_wo_with_operation(db_session, work_center=wc)

        resp = client.put(
            f"/api/v1/work-centers/{wc.id}",
            headers=admin_headers,
            json={"version": 0, "is_active": None},
        )

        assert resp.status_code == status.HTTP_200_OK, resp.text
        db_session.expire_all()
        refreshed = db_session.get(WorkCenter, wc.id)
        assert refreshed.is_active is True
        # Dropped before the diff, so nothing changed and nothing was audited.
        assert self._committed_audit_rows(db_session, wc.id) == []


@pytest.mark.api
@pytest.mark.requires_db
class TestWorkCenterCreateAndStatusAudit:
    """POST / and POST /{id}/status: the two handlers that changed state with no trace.

    ``create_work_center`` wrote no CREATE row while the CSV importer -- which builds the
    identical row -- always did, so the machine roster's audit trail depended on which door
    was used. ``update_work_center_status`` had bare ``get_current_user`` RBAC *and* no
    audit row: any authenticated user in the tenant could flip a machine to
    ``offline``/``maintenance``, changing what the dispatch board and the kiosk show, with
    nothing recording who.
    """

    @staticmethod
    def _committed_audit_rows(db: Session, work_center_id: int) -> list:
        """See ``TestDeactivateWorkCenter._committed_audit_rows`` -- the rollback before the
        query is what distinguishes a COMMITTED row from a merely flushed one."""
        db.rollback()
        db.expire_all()
        return (
            db.query(AuditLog)
            .filter(AuditLog.resource_type == "work_center", AuditLog.resource_id == work_center_id)
            .order_by(AuditLog.sequence_number.desc())
            .all()
        )

    def test_create_writes_a_committed_create_row(self, client: TestClient, admin_headers: dict, db_session: Session):
        resp = client.post(
            "/api/v1/work-centers/",
            headers=admin_headers,
            json={"code": "AUD-CREATE-1", "name": "Audited Bay", "work_center_type": "welding"},
        )

        assert resp.status_code == status.HTTP_200_OK, resp.text
        wc_id = resp.json()["id"]

        rows = self._committed_audit_rows(db_session, wc_id)
        assert len(rows) == 1, "exactly one committed CREATE row"
        row = rows[0]
        assert row.action == "CREATE"
        assert row.resource_identifier == "AUD-CREATE-1"
        assert row.company_id == COMPANY_A
        # ``source: import`` is what distinguishes the CSV importer's row from this one.
        assert "source" not in (row.extra_data or {})

    def test_status_change_writes_a_committed_status_change_row(
        self, client: TestClient, admin_headers: dict, db_session: Session
    ):
        wc = make_work_center(db_session)
        wc_id, wc_code = wc.id, wc.code

        resp = client.post(f"/api/v1/work-centers/{wc_id}/status?status=maintenance", headers=admin_headers)

        assert resp.status_code == status.HTTP_200_OK, resp.text
        db_session.expire_all()
        assert db_session.get(WorkCenter, wc_id).current_status == "maintenance"

        rows = self._committed_audit_rows(db_session, wc_id)
        assert len(rows) == 1, "exactly one committed STATUS_CHANGE row"
        row = rows[0]
        assert row.action == "STATUS_CHANGE"
        assert row.resource_identifier == wc_code
        assert row.old_values == {"status": "available"}
        assert row.new_values == {"status": "maintenance"}

    def test_restating_the_same_status_writes_nothing(
        self, client: TestClient, admin_headers: dict, db_session: Session
    ):
        """``log_status_change`` does NOT self-suppress the way ``log_update`` does, so a
        no-op re-statement would otherwise put a meaningless 'available -> available' row on
        the tamper-evident chain. Mirrors ``test_put_explicit_null_is_active_is_no_change``."""
        wc = make_work_center(db_session)

        resp = client.post(f"/api/v1/work-centers/{wc.id}/status?status=available", headers=admin_headers)

        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert self._committed_audit_rows(db_session, wc.id) == []

    def test_status_change_is_refused_for_an_operator(
        self, client: TestClient, operator_headers: dict, db_session: Session
    ):
        """RBAC tightening: this endpoint accepted ANY authenticated user. It now matches
        PUT's Admin/Manager tier (not DELETE's Admin-only -- a status flip is reversible,
        and PUT already lets a Manager flip ``is_active``)."""
        wc = make_work_center(db_session)

        resp = client.post(f"/api/v1/work-centers/{wc.id}/status?status=offline", headers=operator_headers)

        assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text
        db_session.expire_all()
        assert db_session.get(WorkCenter, wc.id).current_status == "available", "a refusal leaves the row untouched"
        assert self._committed_audit_rows(db_session, wc.id) == []

    def test_status_change_is_allowed_for_a_manager(
        self, client: TestClient, manager_headers: dict, db_session: Session
    ):
        """The negative control on the tightening: a manager is exactly who runs the board."""
        wc = make_work_center(db_session)

        resp = client.post(f"/api/v1/work-centers/{wc.id}/status?status=offline", headers=manager_headers)

        assert resp.status_code == status.HTTP_200_OK, resp.text
        db_session.expire_all()
        assert db_session.get(WorkCenter, wc.id).current_status == "offline"
