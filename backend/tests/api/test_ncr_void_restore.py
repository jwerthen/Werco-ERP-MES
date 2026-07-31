"""NCR void (soft-delete) + restore endpoint coverage.

Covers the quality-record void path added to ``app/api/endpoints/quality.py``:
role gating, the blank-reason guard, the "already voided" / "not voided" states,
the active-work-order-blocker guardrail, tamper-evident audit emission (which the
plain ``update_ncr`` path deliberately does NOT do), and restore-reopens-to-OPEN.
"""

from app.models.audit_log import AuditLog
from app.models.quality import NCRStatus, NonConformanceReport
from app.models.work_order_blocker import WorkOrderBlocker, WorkOrderBlockerStatus

_VALID_NCR = {
    "source": "in_process",
    "title": "Void test NCR title",
    "description": "A sufficiently long defect description for the void/restore tests.",
}


def _create_ncr(client, headers) -> int:
    resp = client.post("/api/v1/quality/ncr", headers=headers, json=_VALID_NCR)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


def _void(client, headers, ncr_id, body):
    # httpx's TestClient.delete() convenience method rejects a request body, so a
    # DELETE that carries the void-reason JSON must go through .request(...).
    return client.request("DELETE", f"/api/v1/quality/ncr/{ncr_id}", headers=headers, json=body)


def _audit_rows(db_session, ncr_id, action=None):
    query = db_session.query(AuditLog).filter(AuditLog.resource_type == "ncr", AuditLog.resource_id == ncr_id)
    if action is not None:
        query = query.filter(AuditLog.action == action)
    return query.all()


class TestVoidNCR:
    def test_void_marks_deleted_and_voids_status(self, client, admin_headers, db_session):
        ncr_id = _create_ncr(client, admin_headers)

        resp = _void(client, admin_headers, ncr_id, {"reason": "Duplicate of NCR-X"})

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["can_restore"] is True
        assert "voided" in body["message"]

        ncr = db_session.get(NonConformanceReport, ncr_id)
        db_session.refresh(ncr)
        assert ncr.is_deleted is True
        assert ncr.deleted_at is not None
        assert ncr.status == NCRStatus.VOID

    def test_void_emits_status_change_and_delete_audit(self, client, admin_headers, db_session):
        ncr_id = _create_ncr(client, admin_headers)

        _void(client, admin_headers, ncr_id, {"reason": "Raised in error"})

        # update_ncr does NOT audit; the void path MUST — both a STATUS_CHANGE and a DELETE row.
        assert len(_audit_rows(db_session, ncr_id, "STATUS_CHANGE")) == 1
        delete_rows = _audit_rows(db_session, ncr_id, "DELETE")
        assert len(delete_rows) == 1
        assert delete_rows[0].extra_data.get("reason") == "Raised in error"

    def test_manager_can_void(self, client, manager_headers, db_session):
        ncr_id = _create_ncr(client, manager_headers)
        resp = _void(client, manager_headers, ncr_id, {"reason": "Manager void"})
        assert resp.status_code == 200, resp.text

    def test_operator_forbidden(self, client, admin_headers, operator_headers):
        ncr_id = _create_ncr(client, admin_headers)
        resp = _void(client, operator_headers, ncr_id, {"reason": "nope"})
        assert resp.status_code == 403

    def test_blank_reason_rejected(self, client, admin_headers):
        ncr_id = _create_ncr(client, admin_headers)
        resp = _void(client, admin_headers, ncr_id, {"reason": "   "})
        assert resp.status_code == 422

    def test_missing_reason_rejected(self, client, admin_headers):
        ncr_id = _create_ncr(client, admin_headers)
        resp = _void(client, admin_headers, ncr_id, {})
        assert resp.status_code == 422

    def test_already_voided_returns_400(self, client, admin_headers):
        ncr_id = _create_ncr(client, admin_headers)
        first = _void(client, admin_headers, ncr_id, {"reason": "first"})
        assert first.status_code == 200, first.text
        second = _void(client, admin_headers, ncr_id, {"reason": "again"})
        assert second.status_code == 400
        assert "already voided" in second.json()["detail"]

    def test_not_found_returns_404(self, client, admin_headers):
        resp = _void(client, admin_headers, 999999, {"reason": "x"})
        assert resp.status_code == 404

    def test_active_blocker_refuses_void(self, client, admin_headers, db_session, test_work_order):
        ncr_id = _create_ncr(client, admin_headers)
        blocker = WorkOrderBlocker(
            work_order_id=test_work_order.id,
            ncr_id=ncr_id,
            title="Quality hold on this NCR",
            status=WorkOrderBlockerStatus.OPEN.value,
            company_id=1,
        )
        db_session.add(blocker)
        db_session.commit()

        resp = _void(client, admin_headers, ncr_id, {"reason": "try"})
        assert resp.status_code == 400
        assert "blocking work order" in resp.json()["detail"]

        # NCR stays live — nothing voided.
        ncr = db_session.get(NonConformanceReport, ncr_id)
        db_session.refresh(ncr)
        assert ncr.is_deleted is False

    def test_resolved_blocker_allows_void(self, client, admin_headers, db_session, test_work_order):
        ncr_id = _create_ncr(client, admin_headers)
        blocker = WorkOrderBlocker(
            work_order_id=test_work_order.id,
            ncr_id=ncr_id,
            title="Resolved quality hold",
            status=WorkOrderBlockerStatus.RESOLVED.value,
            company_id=1,
        )
        db_session.add(blocker)
        db_session.commit()

        resp = _void(client, admin_headers, ncr_id, {"reason": "resolved already"})
        assert resp.status_code == 200, resp.text

    def test_voided_ncr_hidden_from_list_and_get(self, client, admin_headers):
        ncr_id = _create_ncr(client, admin_headers)
        _void(client, admin_headers, ncr_id, {"reason": "hide me"})

        get_resp = client.get(f"/api/v1/quality/ncr/{ncr_id}", headers=admin_headers)
        assert get_resp.status_code == 404

        list_resp = client.get("/api/v1/quality/ncr", headers=admin_headers)
        assert list_resp.status_code == 200
        assert all(row["id"] != ncr_id for row in list_resp.json())


class TestRestoreNCR:
    def test_restore_reopens_to_open(self, client, admin_headers, db_session):
        ncr_id = _create_ncr(client, admin_headers)
        _void(client, admin_headers, ncr_id, {"reason": "voided then restored"})

        resp = client.post(f"/api/v1/quality/ncr/{ncr_id}/restore", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert "restored" in resp.json()["message"]

        ncr = db_session.get(NonConformanceReport, ncr_id)
        db_session.refresh(ncr)
        assert ncr.is_deleted is False
        assert ncr.status == NCRStatus.OPEN

    def test_restore_emits_audit(self, client, admin_headers, db_session):
        ncr_id = _create_ncr(client, admin_headers)
        _void(client, admin_headers, ncr_id, {"reason": "audit restore"})
        client.post(f"/api/v1/quality/ncr/{ncr_id}/restore", headers=admin_headers)

        assert len(_audit_rows(db_session, ncr_id, "RESTORE")) == 1

    def test_restore_non_voided_returns_400(self, client, admin_headers):
        ncr_id = _create_ncr(client, admin_headers)
        resp = client.post(f"/api/v1/quality/ncr/{ncr_id}/restore", headers=admin_headers)
        assert resp.status_code == 400
        assert "not voided" in resp.json()["detail"]

    def test_restore_not_found_returns_404(self, client, admin_headers):
        resp = client.post("/api/v1/quality/ncr/999999/restore", headers=admin_headers)
        assert resp.status_code == 404

    def test_restore_forbidden_for_operator(self, client, admin_headers, operator_headers):
        ncr_id = _create_ncr(client, admin_headers)
        _void(client, admin_headers, ncr_id, {"reason": "void"})
        resp = client.post(f"/api/v1/quality/ncr/{ncr_id}/restore", headers=operator_headers)
        assert resp.status_code == 403
