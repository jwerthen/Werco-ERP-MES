"""Soft-delete / void read-sweep regressions.

These lock in the compliance read-path fixes that shipped with the delete/void
feature: once a record is voided or soft-deleted it must disappear from every
*read* surface (lists, queues, dashboards, reports, search, export), and it must
no longer accept downstream writes. Each leak below was closed on this branch.

Grouped by record type:

- **Voided NCR** — excluded from the reports quality-metrics NCR counts and the
  quality-summary disposition counts; can no longer be updated (PUT -> 404) or
  spawn a CAR (POST create-car -> 404). (List / get exclusion is covered in
  ``test_ncr_void_restore.py`` and is not duplicated here.)
- **Voided receipt** — excluded from the receiving history, inspection queue,
  stats, and the reports quality-metrics receipt aggregate.
- **Soft-deleted PO** — excluded from the PO list, global search, the PO export,
  and the PO *lines* export (the child rows carry no soft-delete flag of their
  own, so only the parent's predicate on the join can drop them).
- **Soft-deleted customer** — excluded from ``GET /customers/names``, the
  dropdown feed behind every quote / RFQ / order customer picker.
- **Cross-tenant** — reports quality-metrics for company A never counts company
  B's receipts or NCRs (the ``company_id`` filter blocker fix).
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.user import UserRole
from tests.api.test_receiving_compliance import (
    headers_for,
    make_pending_receipt,
    make_po_line,
    make_user,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

_NCR_BODY = {
    "source": "in_process",
    "title": "Read-sweep NCR title",
    "description": "A sufficiently long non-conformance description for the read-sweep tests.",
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _create_ncr(client: TestClient, headers: dict) -> int:
    resp = client.post("/api/v1/quality/ncr", headers=headers, json=_NCR_BODY)
    assert resp.status_code in (status.HTTP_200_OK, status.HTTP_201_CREATED), resp.text
    return resp.json()["id"]


def _void_ncr(client: TestClient, headers: dict, ncr_id: int, reason: str = "read-sweep void"):
    # httpx's TestClient.delete() rejects a body; the void reason must go via .request.
    return client.request("DELETE", f"/api/v1/quality/ncr/{ncr_id}", headers=headers, json={"reason": reason})


def _receive(client: TestClient, headers: dict, line_id: int, *, qty: float, lot: str):
    resp = client.post(
        "/api/v1/receiving/receive",
        headers=headers,
        json={
            "po_line_id": line_id,
            "quantity_received": qty,
            "lot_number": lot,
            "requires_inspection": True,  # -> PENDING_INSPECTION (shows in the inspection queue)
        },
    )
    assert resp.status_code in (status.HTTP_200_OK, status.HTTP_201_CREATED), resp.text
    return resp.json()


def _void_receipt(client: TestClient, headers: dict, receipt_id: int, reason: str = "read-sweep void"):
    return client.post(f"/api/v1/receiving/receipt/{receipt_id}/void", headers=headers, json={"reason": reason})


# ===========================================================================
# Voided NCR
# ===========================================================================


def test_voided_ncr_excluded_from_reports_quality_metrics(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    headers = headers_for(admin)
    live_id = _create_ncr(client, headers)  # noqa: F841 - kept live as the control
    void_id = _create_ncr(client, headers)

    before = client.get("/api/v1/reports/quality-metrics", headers=headers).json()
    assert before["total_ncrs"] == 2

    assert _void_ncr(client, headers, void_id).status_code == status.HTTP_200_OK

    after = client.get("/api/v1/reports/quality-metrics", headers=headers).json()
    assert after["total_ncrs"] == 1  # only the live NCR remains


def test_voided_ncr_excluded_from_quality_summary_dispositions(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    headers = headers_for(admin)
    _create_ncr(client, headers)  # live
    void_id = _create_ncr(client, headers)

    # Both new NCRs default to the "pending" disposition.
    before = client.get("/api/v1/quality/summary", headers=headers).json()
    assert before["ncr_dispositions"].get("pending") == 2

    assert _void_ncr(client, headers, void_id).status_code == status.HTTP_200_OK

    after = client.get("/api/v1/quality/summary", headers=headers).json()
    assert after["ncr_dispositions"].get("pending") == 1


def test_voided_ncr_cannot_be_updated(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    headers = headers_for(admin)
    ncr_id = _create_ncr(client, headers)
    assert _void_ncr(client, headers, ncr_id).status_code == status.HTTP_200_OK

    # A well-formed NCRUpdate body (version is the one required field) so the request
    # passes schema validation and reaches the handler's live-row lookup, which 404s
    # on the voided (is_deleted) NCR rather than the body 422ing first.
    resp = client.put(f"/api/v1/quality/ncr/{ncr_id}", headers=headers, json={"version": 0, "root_cause": None})
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text


def test_voided_ncr_cannot_spawn_car(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    headers = headers_for(admin)
    ncr_id = _create_ncr(client, headers)
    assert _void_ncr(client, headers, ncr_id).status_code == status.HTTP_200_OK

    resp = client.post(f"/api/v1/quality/ncr/{ncr_id}/create-car", headers=headers)
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text


# ===========================================================================
# Voided receipt
# ===========================================================================


def test_voided_receipt_excluded_from_history_queue_and_stats(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    headers = headers_for(admin)
    line = make_po_line(db_session, company_id=1, quantity_ordered=20)

    keep = _receive(client, headers, line.id, qty=3, lot="LOT-KEEP")
    drop = _receive(client, headers, line.id, qty=5, lot="LOT-DROP")

    # Both are visible before the void.
    queue = client.get("/api/v1/receiving/inspection-queue", headers=headers).json()
    assert {keep["id"], drop["id"]} <= {r["receipt_id"] for r in queue}
    assert client.get("/api/v1/receiving/stats", headers=headers).json()["receipts_in_period"] == 2

    assert _void_receipt(client, headers, drop["id"]).status_code == status.HTTP_200_OK

    # History: only the kept receipt.
    history_ids = {r["receipt_id"] for r in client.get("/api/v1/receiving/history", headers=headers).json()}
    assert keep["id"] in history_ids
    assert drop["id"] not in history_ids

    # Inspection queue: only the kept receipt.
    queue_ids = {r["receipt_id"] for r in client.get("/api/v1/receiving/inspection-queue", headers=headers).json()}
    assert keep["id"] in queue_ids
    assert drop["id"] not in queue_ids

    # Stats: the voided receipt no longer counts.
    assert client.get("/api/v1/receiving/stats", headers=headers).json()["receipts_in_period"] == 1


def test_voided_receipt_excluded_from_reports_quality_metrics(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    headers = headers_for(admin)
    line = make_po_line(db_session, company_id=1, quantity_ordered=20)

    _receive(client, headers, line.id, qty=3, lot="LOT-QM-KEEP")
    drop = _receive(client, headers, line.id, qty=5, lot="LOT-QM-DROP")

    before = client.get("/api/v1/reports/quality-metrics", headers=headers).json()
    assert float(before["receiving_total_qty"]) == 8.0

    assert _void_receipt(client, headers, drop["id"]).status_code == status.HTTP_200_OK

    after = client.get("/api/v1/reports/quality-metrics", headers=headers).json()
    assert float(after["receiving_total_qty"]) == 3.0  # only the kept receipt's qty


# ===========================================================================
# Soft-deleted purchase order
# ===========================================================================


def test_soft_deleted_po_excluded_from_list_search_and_export(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    headers = headers_for(admin)

    live_line = make_po_line(db_session, company_id=1, quantity_ordered=10)
    doomed_line = make_po_line(db_session, company_id=1, quantity_ordered=10)
    live, doomed = live_line.purchase_order, doomed_line.purchase_order
    live_number, doomed_number = live.po_number, doomed.po_number
    live_part, doomed_part = live_line.part.part_number, doomed_line.part.part_number
    doomed_id = doomed.id

    assert (
        client.delete(f"/api/v1/purchasing/purchase-orders/{doomed_id}", headers=headers).status_code
        == status.HTTP_200_OK
    )

    # 1. PO list excludes the deleted PO.
    list_ids = {p["id"] for p in client.get("/api/v1/purchasing/purchase-orders", headers=headers).json()}
    assert doomed_id not in list_ids

    # 2. Global search: the deleted PO's number returns no purchase_order hit, but the
    #    live PO's number still does (control that search itself works).
    deleted_hits = client.get("/api/v1/search/", headers=headers, params={"q": doomed_number}).json()["results"]
    assert not any(r["type"] == "purchase_order" and r["id"] == doomed_id for r in deleted_hits)
    live_hits = client.get("/api/v1/search/", headers=headers, params={"q": live_number}).json()["results"]
    assert any(r["type"] == "purchase_order" and r["id"] == live.id for r in live_hits)

    # 3. CSV export excludes the deleted PO but keeps the live one.
    export = client.get("/api/v1/exports/purchase-orders/export", headers=headers, params={"format": "csv"})
    assert export.status_code == status.HTTP_200_OK, export.text
    assert doomed_number not in export.text
    assert live_number in export.text

    # 4. The PO *lines* export drops the deleted PO's lines too. This one needs its
    #    own assertion rather than riding on (3): `PurchaseOrderLine` has no
    #    SoftDeleteMixin and `delete_purchase_order` does not touch the child rows,
    #    so the lines survive the parent's deletion and only the parent's
    #    `is_deleted` predicate on the join can exclude them. Without it the two
    #    exports disagree by construction -- and because the delete guard refuses
    #    once any line has been received, the surviving lines are exactly the open
    #    commitments (ordered qty, unit price, required date) a manager reconciles
    #    from, so the divergence lands on the reconciliation itself.
    line_export = client.get("/api/v1/exports/purchase-orders/lines/export", headers=headers, params={"format": "csv"})
    assert line_export.status_code == status.HTTP_200_OK, line_export.text
    assert doomed_number not in line_export.text
    assert doomed_part not in line_export.text
    assert live_number in line_export.text
    assert live_part in line_export.text


# ===========================================================================
# Soft-deleted customer
# ===========================================================================


def test_customer_names_dropdown_excludes_soft_deleted_customers(client: TestClient, db_session: Session):
    """``GET /customers/names`` filtered on ``is_active`` only, never ``is_deleted``.

    Soft-delete does not imply ``is_active=False`` at the model level --
    ``SoftDeleteMixin.soft_delete()`` touches neither flag -- so the two can
    diverge, and this endpoint is the feed behind every quote / RFQ / order
    customer picker. The sibling ``list_customers`` has always applied the
    predicate; this one did not.

    The divergent state is reachable through the API, which is why this is a
    real leak rather than a defence-in-depth nicety: ``DELETE /customers/{id}``
    happens to set ``is_active=False`` alongside the soft delete, but
    ``PUT /customers/{id}`` accepts ``is_active`` and carries **no**
    ``is_deleted`` guard, so a deleted customer can be flipped back to active
    while staying deleted -- and reappear in every dropdown. This test walks
    exactly that path.
    """
    headers = headers_for(make_user(db_session, role=UserRole.ADMIN, company_id=1))

    def _create(name: str) -> int:
        resp = client.post("/api/v1/customers/", headers=headers, json={"name": name})
        assert resp.status_code in (status.HTTP_200_OK, status.HTTP_201_CREATED), resp.text
        return resp.json()["id"]

    live_id = _create("Read-Sweep Live Customer")
    doomed_id = _create("Read-Sweep Doomed Customer")

    assert client.delete(f"/api/v1/customers/{doomed_id}", headers=headers).status_code == status.HTTP_200_OK

    # Re-activate the soft-deleted row: is_deleted stays True, is_active goes
    # back to True. This is the state the missing predicate leaked.
    reactivate = client.put(f"/api/v1/customers/{doomed_id}", headers=headers, json={"is_active": True})
    assert reactivate.status_code == status.HTTP_200_OK, reactivate.text

    names = client.get("/api/v1/customers/names", headers=headers)
    assert names.status_code == status.HTTP_200_OK, names.text
    ids = {row["id"] for row in names.json()}

    assert doomed_id not in ids, "a soft-deleted customer must not be selectable in any picker"
    assert live_id in ids, "control: a live customer is still offered"


# ===========================================================================
# Cross-tenant isolation on the reports aggregate
# ===========================================================================


def test_quality_metrics_excludes_other_company_receipts(client: TestClient, db_session: Session):
    """Company A's quality-metrics receipt aggregate must not include company B's
    receipts — the company_id filter that was the blocker fix."""
    admin_a = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line_a = make_po_line(db_session, company_id=1, quantity_ordered=20)
    _receive(client, headers_for(admin_a), line_a.id, qty=3, lot="LOT-A-ONLY")

    # Company B receipt with a large qty that would swamp the total if it leaked.
    make_pending_receipt(db_session, company_id=2, quantity=100)

    metrics = client.get("/api/v1/reports/quality-metrics", headers=headers_for(admin_a)).json()
    assert float(metrics["receiving_total_qty"]) == 3.0  # company B's 100 is excluded


# NOTE: a cross-tenant NCR-count test is intentionally NOT included here. The
# reports quality-metrics NCR query IS correctly company_id-scoped, but creating an
# NCR under a second company on the same calendar day trips a *pre-existing,
# unrelated* defect: ``ncrs.ncr_number`` carries a GLOBAL ``unique=True`` while
# ``generate_ncr_number`` numbers per-company, so company B's first NCR of the day
# ("NCR-YYYYMMDD-001") collides with company A's. That is a latent multi-tenant bug
# outside this feature's scope (see the test-engineer report); the receipt aggregate
# above already proves the company_id blocker fix.
