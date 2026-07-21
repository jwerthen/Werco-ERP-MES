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
