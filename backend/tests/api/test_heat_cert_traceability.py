"""Heat number and mill cert follow material from the dock to the stock row and the trace page.

Receiving records the heat, cert number and cert file on the PO receipt. These tests pin
that the stock row the receipt places carries the heat, cert and the PO's supplier from
all three placing paths (dock-to-stock, inspection accept, cleared hold), that a merge
keeps the first value, that a receipt Correct keeps its stock row in step, that a
transfer to a new bin keeps them, and that the lot trace and the lot search fall back to
the receipt (matched on lot AND part) where the stock row is blank.
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.inventory import InventoryItem
from app.models.part import Part
from app.models.purchasing import POReceipt
from app.models.user import UserRole
from tests.api.test_receiving_compliance import _next, headers_for, make_location, make_po_line, make_user

pytestmark = [pytest.mark.api, pytest.mark.requires_db]


def _receive(client: TestClient, user, line, *, lot, heat=None, cert=None, requires_inspection=False, qty=5):
    body = {
        "po_line_id": line.id,
        "quantity_received": qty,
        "lot_number": lot,
        "requires_inspection": requires_inspection,
    }
    if heat is not None:
        body["heat_number"] = heat
    if cert is not None:
        body["cert_number"] = cert
    resp = client.post("/api/v1/receiving/receive", headers=headers_for(user), json=body)
    assert resp.status_code in (status.HTTP_200_OK, status.HTTP_201_CREATED), resp.text
    return resp.json()


def _stock_rows(db: Session, lot: str):
    db.expire_all()
    return db.query(InventoryItem).filter(InventoryItem.lot_number == lot).all()


# ---------------------------------------------------------------------------
# Placing paths stamp heat, cert and supplier
# ---------------------------------------------------------------------------


def test_dock_to_stock_stamps_heat_cert_and_supplier(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1)
    lot = f"LOT-HT-{_next():05d}"
    _receive(client, admin, line, lot=lot, heat="  H-1001  ", cert="MC-77")

    (row,) = _stock_rows(db_session, lot)
    assert row.heat_lot == "H-1001"
    assert row.cert_number == "MC-77"
    assert row.supplier_id == line.purchase_order.vendor_id


def test_dock_to_stock_without_heat_leaves_columns_blank(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1)
    lot = f"LOT-HT-{_next():05d}"
    _receive(client, admin, line, lot=lot, heat="   ")

    (row,) = _stock_rows(db_session, lot)
    assert row.heat_lot is None
    assert row.cert_number is None
    assert row.supplier_id == line.purchase_order.vendor_id


def test_inspection_accept_stamps_heat_cert_and_supplier(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1)
    lot = f"LOT-HT-{_next():05d}"
    receipt = _receive(client, admin, line, lot=lot, heat="H-2002", cert="MC-88", requires_inspection=True)
    assert _stock_rows(db_session, lot) == []

    resp = client.post(
        f"/api/v1/receiving/inspect/{receipt['id']}",
        headers=headers_for(admin),
        json={"quantity_accepted": 5, "quantity_rejected": 0, "inspection_method": "visual"},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    (row,) = _stock_rows(db_session, lot)
    assert (row.heat_lot, row.cert_number, row.supplier_id) == ("H-2002", "MC-88", line.purchase_order.vendor_id)


def test_cleared_hold_stamps_heat_cert_and_supplier(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1)
    lot = f"LOT-HT-{_next():05d}"
    receipt = _receive(client, admin, line, lot=lot, heat="H-3003", cert="MC-99", requires_inspection=True)

    resp = client.post(
        f"/api/v1/receiving/receipt/{receipt['id']}/clear-inspection",
        headers=headers_for(admin),
        json={"reason": "Box was ticked by mistake."},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    (row,) = _stock_rows(db_session, lot)
    assert (row.heat_lot, row.cert_number, row.supplier_id) == ("H-3003", "MC-99", line.purchase_order.vendor_id)


def test_merge_keeps_first_heat_and_fills_only_blanks(client: TestClient, db_session: Session):
    """Two receipts of one lot at one location share a stock row: the first heat wins, and
    a field the row was missing is filled by the later receipt."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1, quantity_ordered=20)
    lot = f"LOT-HT-{_next():05d}"
    _receive(client, admin, line, lot=lot, heat="H-FIRST")
    _receive(client, admin, line, lot=lot, heat="H-SECOND", cert="MC-LATE")

    (row,) = _stock_rows(db_session, lot)
    assert float(row.quantity_on_hand) == 10
    assert row.heat_lot == "H-FIRST"
    assert row.cert_number == "MC-LATE"

    merge_audit = (
        db_session.query(AuditLog)
        .filter(AuditLog.resource_type == "inventory", AuditLog.resource_id == row.id, AuditLog.action == "UPDATE")
        .order_by(AuditLog.sequence_number.desc())
        .first()
    )
    changes = merge_audit.extra_data["changes"]
    assert changes["cert_number"]["new"] == "MC-LATE"
    assert "heat_lot" not in changes


# ---------------------------------------------------------------------------
# Correct keeps the stock row in step
# ---------------------------------------------------------------------------


def test_correct_heat_and_cert_updates_the_stock_row(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1)
    lot = f"LOT-HT-{_next():05d}"
    receipt = _receive(client, admin, line, lot=lot, heat="H-TYPO", cert="MC-1")

    resp = client.patch(
        f"/api/v1/receiving/receipt/{receipt['id']}",
        headers=headers_for(admin),
        json={"quantity_received": 3, "heat_number": "H-RIGHT", "cert_number": "MC-2", "reason": "Fat-fingered heat."},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    (row,) = _stock_rows(db_session, lot)
    # The quantity correction and the traceability sync both land on the one row.
    assert float(row.quantity_on_hand) == 3
    assert (row.heat_lot, row.cert_number) == ("H-RIGHT", "MC-2")
    sync_audit = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.resource_type == "inventory",
            AuditLog.resource_id == row.id,
            AuditLog.description.like("Traceability corrected from receipt%"),
        )
        .one()
    )
    assert sync_audit.extra_data["changes"]["heat_lot"] == {"old": "H-TYPO", "new": "H-RIGHT"}


def test_correct_heat_fills_a_blank_stock_row(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1)
    lot = f"LOT-HT-{_next():05d}"
    receipt = _receive(client, admin, line, lot=lot)

    resp = client.patch(
        f"/api/v1/receiving/receipt/{receipt['id']}",
        headers=headers_for(admin),
        json={"quantity_received": 5, "heat_number": "H-LATE", "reason": "Mill cert arrived."},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    (row,) = _stock_rows(db_session, lot)
    assert row.heat_lot == "H-LATE"


def test_correct_does_not_rewrite_a_row_another_receipt_fed(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1, quantity_ordered=20)
    lot = f"LOT-HT-{_next():05d}"
    _receive(client, admin, line, lot=lot, heat="H-SHARED")
    second = _receive(client, admin, line, lot=lot, heat="H-SHARED")

    resp = client.patch(
        f"/api/v1/receiving/receipt/{second['id']}",
        headers=headers_for(admin),
        json={"quantity_received": 5, "heat_number": "H-OTHER", "reason": "Second skid was a different heat."},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    (row,) = _stock_rows(db_session, lot)
    assert row.heat_lot == "H-SHARED"
    assert db_session.get(POReceipt, second["id"]).heat_number == "H-OTHER"


def test_correct_held_receipt_then_accept_stamps_corrected_heat(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1)
    lot = f"LOT-HT-{_next():05d}"
    receipt = _receive(client, admin, line, lot=lot, heat="H-OLD", requires_inspection=True)

    resp = client.patch(
        f"/api/v1/receiving/receipt/{receipt['id']}",
        headers=headers_for(admin),
        json={"quantity_received": 5, "heat_number": "H-NEW", "reason": "Heat mis-keyed."},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    resp = client.post(
        f"/api/v1/receiving/inspect/{receipt['id']}",
        headers=headers_for(admin),
        json={"quantity_accepted": 5, "quantity_rejected": 0, "inspection_method": "visual"},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    (row,) = _stock_rows(db_session, lot)
    assert row.heat_lot == "H-NEW"


# ---------------------------------------------------------------------------
# Transfer keeps the traceability
# ---------------------------------------------------------------------------


def test_transfer_to_new_bin_carries_heat_cert_po_and_supplier(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1)
    src = make_location(db_session, company_id=1)
    dst = make_location(db_session, company_id=1)
    lot = f"LOT-HT-{_next():05d}"
    resp = client.post(
        "/api/v1/inventory/receive",
        headers=headers_for(admin),
        json={
            "part_id": line.part_id,
            "quantity": 10,
            "location_code": src.code,
            "lot_number": lot,
            "po_number": "PO-MANUAL-1",
            "cert_number": "MC-T",
            "heat_lot": "H-T",
        },
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    (source,) = _stock_rows(db_session, lot)
    source.supplier_id = line.purchase_order.vendor_id
    db_session.commit()

    resp = client.post(
        "/api/v1/inventory/transfer",
        headers=headers_for(admin),
        json={"inventory_item_id": source.id, "quantity": 4, "to_location_code": dst.code},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    dest = next(r for r in _stock_rows(db_session, lot) if r.location == dst.code)
    assert (dest.heat_lot, dest.cert_number, dest.po_number, dest.supplier_id) == (
        "H-T",
        "MC-T",
        "PO-MANUAL-1",
        line.purchase_order.vendor_id,
    )


# ---------------------------------------------------------------------------
# Trace and search read the receipt
# ---------------------------------------------------------------------------


def test_trace_falls_back_to_receipt_heat_cert_and_document(client: TestClient, db_session: Session):
    """A stock row placed before heat rode to stock is blank; the trace header reads the receipt."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1)
    lot = f"LOT-HT-{_next():05d}"
    receipt = _receive(client, admin, line, lot=lot, heat="H-LEGACY", cert="MC-LEGACY")
    (row,) = _stock_rows(db_session, lot)
    row.heat_lot = None
    row.cert_number = None
    stored = db_session.get(POReceipt, receipt["id"])
    stored.certificate_document_id = 4242
    db_session.commit()

    resp = client.get(f"/api/v1/traceability/lot/{lot}", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["heat_lot"] == "H-LEGACY"
    assert body["cert_number"] == "MC-LEGACY"
    assert body["certificate_document_id"] == 4242
    assert body["supplier_name"] == line.purchase_order.vendor.name
    received = next(h for h in body["history"] if h["event_type"] == "received")
    assert received["details"]["heat_number"] == "H-LEGACY"
    assert received["details"]["certificate_document_id"] == 4242


def test_trace_stock_row_value_wins_over_receipt(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1)
    lot = f"LOT-HT-{_next():05d}"
    _receive(client, admin, line, lot=lot, heat="H-RECEIPT")
    (row,) = _stock_rows(db_session, lot)
    row.heat_lot = "H-ROW"
    db_session.commit()

    body = client.get(f"/api/v1/traceability/lot/{lot}", headers=headers_for(admin)).json()
    assert body["heat_lot"] == "H-ROW"


def test_trace_matches_receipt_on_part_as_well_as_lot(client: TestClient, db_session: Session):
    """Lot numbers repeat across parts: another part's receipt of the same lot is not this
    lot's heat, PO or supplier."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    lot = f"LOT-HT-{_next():05d}"
    mine = make_po_line(db_session, company_id=1)
    theirs = make_po_line(db_session, company_id=1)
    # This part's lot was received by hand (no heat on the row); the other part's PO
    # receipt of the same lot number carries a heat.
    loc = make_location(db_session, company_id=1)
    resp = client.post(
        "/api/v1/inventory/receive",
        headers=headers_for(admin),
        json={"part_id": mine.part_id, "quantity": 2, "location_code": loc.code, "lot_number": lot},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    _receive(client, admin, theirs, lot=lot, heat="H-NOT-MINE", cert="MC-NOT-MINE")
    # Make this part's row the one the trace reads first.
    first = db_session.query(InventoryItem).filter(InventoryItem.lot_number == lot).order_by(InventoryItem.id).first()
    assert first.part_id == mine.part_id

    body = client.get(f"/api/v1/traceability/lot/{lot}", headers=headers_for(admin)).json()
    assert body["part_id"] == mine.part_id
    assert body["heat_lot"] is None
    assert body["cert_number"] is None
    assert body["certificate_document_id"] is None
    assert body["supplier_name"] is None
    assert body["po_number"] != theirs.purchase_order.po_number


def test_trace_of_held_lot_names_part_and_heat_from_receipt(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1)
    lot = f"LOT-HT-{_next():05d}"
    _receive(client, admin, line, lot=lot, heat="H-HELD", requires_inspection=True)

    body = client.get(f"/api/v1/traceability/lot/{lot}", headers=headers_for(admin)).json()
    assert body["part_id"] == line.part_id
    assert body["heat_lot"] == "H-HELD"
    assert body["po_number"] == line.purchase_order.po_number


def test_trace_reads_through_a_soft_deleted_supplier(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1)
    lot = f"LOT-HT-{_next():05d}"
    _receive(client, admin, line, lot=lot, heat="H-GONE")
    vendor = line.purchase_order.vendor
    vendor_name = vendor.name
    vendor.soft_delete(admin.id)
    db_session.commit()

    body = client.get(f"/api/v1/traceability/lot/{lot}", headers=headers_for(admin)).json()
    assert body["supplier_name"] == vendor_name
    assert body["heat_lot"] == "H-GONE"


def test_trace_ignores_a_voided_receipts_heat(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1)
    lot = f"LOT-HT-{_next():05d}"
    receipt = _receive(client, admin, line, lot=lot, heat="H-VOID", requires_inspection=True)
    resp = client.post(
        f"/api/v1/receiving/receipt/{receipt['id']}/void",
        headers=headers_for(admin),
        json={"reason": "Wrong PO."},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    body = client.get(f"/api/v1/traceability/lot/{lot}", headers=headers_for(admin)).json()
    assert body["heat_lot"] is None


def test_search_finds_lot_by_receipt_heat_and_cert(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1)
    lot = f"LOT-HT-{_next():05d}"
    heat = f"HEATQ{_next():05d}"
    _receive(client, admin, line, lot=lot, heat=heat, cert=f"CERTQ{_next():05d}")
    (row,) = _stock_rows(db_session, lot)
    row.heat_lot = None  # a pre-change row: only the receipt knows the heat
    db_session.commit()

    results = client.get("/api/v1/traceability/search", params={"q": heat}, headers=headers_for(admin)).json()
    (hit,) = [r for r in results if r["number"] == lot]
    part = db_session.get(Part, line.part_id)
    assert hit["type"] == "lot"
    assert hit["part_number"] == part.part_number
    assert hit["heat_lot"] == heat
    assert float(hit["quantity"]) == 5


def test_search_finds_held_lot_by_heat_with_zero_on_hand(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1)
    lot = f"LOT-HT-{_next():05d}"
    heat = f"HEATH{_next():05d}"
    _receive(client, admin, line, lot=lot, heat=heat, requires_inspection=True)

    results = client.get("/api/v1/traceability/search", params={"q": heat}, headers=headers_for(admin)).json()
    (hit,) = [r for r in results if r["number"] == lot]
    assert hit["quantity"] == 0
    assert hit["location"] is None


def test_search_does_not_return_another_companys_receipt(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    other_admin = make_user(db_session, role=UserRole.ADMIN, company_id=2)
    other_line = make_po_line(db_session, company_id=2)
    lot = f"LOT-HT-{_next():05d}"
    heat = f"HEATX{_next():05d}"
    _receive(client, other_admin, other_line, lot=lot, heat=heat)

    results = client.get("/api/v1/traceability/search", params={"q": heat}, headers=headers_for(admin)).json()
    assert results == []


# ---------------------------------------------------------------------------
# Test-engineer additions: the delivery path, refusals, purity, tenancy, edges
# ---------------------------------------------------------------------------


def _audit_count(db: Session) -> int:
    db.expire_all()
    return db.query(AuditLog).count()


def _receive_manual(client: TestClient, user, part_id: int, location_code: str, lot: str, **extra):
    resp = client.post(
        "/api/v1/inventory/receive",
        headers=headers_for(user),
        json={"part_id": part_id, "quantity": 10, "location_code": location_code, "lot_number": lot, **extra},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    return resp.json()


def test_delivery_stamps_heat_cert_and_supplier_on_its_dock_to_stock_line(
    client: TestClient, db_session: Session, monkeypatch
):
    """POST /receiving/deliveries is the fourth door onto the dock-to-stock path."""
    monkeypatch.setattr("app.api.endpoints.receiving.enqueue_job_best_effort", lambda *a, **k: None)
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1)
    lot = f"LOT-HT-{_next():05d}"
    resp = client.post(
        "/api/v1/receiving/deliveries",
        headers=headers_for(admin),
        json={
            "idempotency_key": f"heat-delivery-{_next():05d}",
            "purchase_order_id": line.purchase_order_id,
            "lines": [
                {
                    "po_line_id": line.id,
                    "quantity_received": 3,
                    "lot_number": lot,
                    "heat_number": " H-DLV ",
                    "cert_number": "MC-DLV",
                }
            ],
        },
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    (row,) = _stock_rows(db_session, lot)
    assert (row.heat_lot, row.cert_number, row.supplier_id) == ("H-DLV", "MC-DLV", line.purchase_order.vendor_id)


def test_merge_fills_a_whitespace_only_value_on_the_existing_row(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1, quantity_ordered=20)
    lot = f"LOT-HT-{_next():05d}"
    _receive(client, admin, line, lot=lot)
    (row,) = _stock_rows(db_session, lot)
    row.heat_lot = "   "
    db_session.commit()

    _receive(client, admin, line, lot=lot, heat="H-REAL")
    (row,) = _stock_rows(db_session, lot)
    assert row.heat_lot == "H-REAL"


def test_correct_refused_for_operator_leaves_receipt_row_and_audit_untouched(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    operator = make_user(db_session, role=UserRole.OPERATOR, company_id=1)
    line = make_po_line(db_session, company_id=1)
    lot = f"LOT-HT-{_next():05d}"
    receipt = _receive(client, admin, line, lot=lot, heat="H-KEEP")
    before = _audit_count(db_session)

    resp = client.patch(
        f"/api/v1/receiving/receipt/{receipt['id']}",
        headers=headers_for(operator),
        json={"quantity_received": 5, "heat_number": "H-OPERATOR", "reason": "Not my call."},
    )
    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text
    (row,) = _stock_rows(db_session, lot)
    assert row.heat_lot == "H-KEEP"
    assert db_session.get(POReceipt, receipt["id"]).heat_number == "H-KEEP"
    assert _audit_count(db_session) == before


def test_correct_another_companys_receipt_is_404_with_no_change_and_no_audit(client: TestClient, db_session: Session):
    owner = make_user(db_session, role=UserRole.ADMIN, company_id=2)
    intruder = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=2)
    lot = f"LOT-HT-{_next():05d}"
    receipt = _receive(client, owner, line, lot=lot, heat="H-THEIRS")
    before = _audit_count(db_session)

    resp = client.patch(
        f"/api/v1/receiving/receipt/{receipt['id']}",
        headers=headers_for(intruder),
        json={"quantity_received": 5, "heat_number": "H-MINE", "reason": "Cross-tenant attempt."},
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
    (row,) = _stock_rows(db_session, lot)
    assert row.heat_lot == "H-THEIRS"
    assert db_session.get(POReceipt, receipt["id"]).heat_number == "H-THEIRS"
    assert _audit_count(db_session) == before


def test_correct_quantity_only_writes_no_traceability_audit_row(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1)
    lot = f"LOT-HT-{_next():05d}"
    receipt = _receive(client, admin, line, lot=lot, heat="H-SAME")

    resp = client.patch(
        f"/api/v1/receiving/receipt/{receipt['id']}",
        headers=headers_for(admin),
        json={"quantity_received": 4, "heat_number": "H-SAME", "reason": "Counted again."},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    (row,) = _stock_rows(db_session, lot)
    assert float(row.quantity_on_hand) == 4
    assert row.heat_lot == "H-SAME"
    assert (
        db_session.query(AuditLog)
        .filter(AuditLog.description.like(f"Traceability corrected from receipt {receipt['receipt_number']}%"))
        .count()
        == 0
    )


def test_correct_keeps_a_stock_row_value_that_was_changed_on_its_own(client: TestClient, db_session: Session):
    """The row follows only while it still holds the receipt's OLD value; a value someone
    set on the row itself is not the receipt's to overwrite."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1)
    lot = f"LOT-HT-{_next():05d}"
    receipt = _receive(client, admin, line, lot=lot, heat="H-A")
    (row,) = _stock_rows(db_session, lot)
    row.heat_lot = "H-ROW"
    db_session.commit()

    resp = client.patch(
        f"/api/v1/receiving/receipt/{receipt['id']}",
        headers=headers_for(admin),
        json={"quantity_received": 5, "heat_number": "H-B", "reason": "Receipt heat corrected."},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    (row,) = _stock_rows(db_session, lot)
    assert row.heat_lot == "H-ROW"
    assert db_session.get(POReceipt, receipt["id"]).heat_number == "H-B"


def test_correct_does_not_rewrite_a_row_a_manual_receive_also_fed(client: TestClient, db_session: Session):
    """A manual /inventory/receive into the same row is another RECEIVE: the row is shared."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1)
    lot = f"LOT-HT-{_next():05d}"
    loc = make_location(db_session, company_id=1)
    resp = client.post(
        "/api/v1/receiving/receive",
        headers=headers_for(admin),
        json={
            "po_line_id": line.id,
            "quantity_received": 5,
            "lot_number": lot,
            "heat_number": "H-ONE",
            "requires_inspection": False,
            "location_id": loc.id,
        },
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    receipt = resp.json()
    (row,) = _stock_rows(db_session, lot)
    assert row.location == loc.code
    _receive_manual(client, admin, row.part_id, loc.code, lot)
    (row,) = _stock_rows(db_session, lot)
    assert float(row.quantity_on_hand) == 15

    resp = client.patch(
        f"/api/v1/receiving/receipt/{receipt['id']}",
        headers=headers_for(admin),
        json={"quantity_received": 5, "heat_number": "H-TWO", "reason": "Receipt heat corrected."},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    (row,) = _stock_rows(db_session, lot)
    assert row.heat_lot == "H-ONE"


def test_transfer_into_an_existing_destination_row_keeps_its_own_heat(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1)
    src = make_location(db_session, company_id=1)
    dst = make_location(db_session, company_id=1)
    lot = f"LOT-HT-{_next():05d}"
    _receive_manual(client, admin, line.part_id, src.code, lot, heat_lot="H-SRC", cert_number="MC-SRC")
    _receive_manual(client, admin, line.part_id, dst.code, lot, heat_lot="H-DST", cert_number="MC-DST")
    source = next(r for r in _stock_rows(db_session, lot) if r.location == src.code)

    resp = client.post(
        "/api/v1/inventory/transfer",
        headers=headers_for(admin),
        json={"inventory_item_id": source.id, "quantity": 4, "to_location_code": dst.code},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    dest = next(r for r in _stock_rows(db_session, lot) if r.location == dst.code)
    assert float(dest.quantity_on_hand) == 14
    assert (dest.heat_lot, dest.cert_number) == ("H-DST", "MC-DST")


def test_transfer_refused_for_operator_creates_no_destination_row(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    operator = make_user(db_session, role=UserRole.OPERATOR, company_id=1)
    line = make_po_line(db_session, company_id=1)
    src = make_location(db_session, company_id=1)
    dst = make_location(db_session, company_id=1)
    lot = f"LOT-HT-{_next():05d}"
    _receive_manual(client, admin, line.part_id, src.code, lot, heat_lot="H-SRC")
    (source,) = _stock_rows(db_session, lot)

    resp = client.post(
        "/api/v1/inventory/transfer",
        headers=headers_for(operator),
        json={"inventory_item_id": source.id, "quantity": 4, "to_location_code": dst.code},
    )
    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text
    assert len(_stock_rows(db_session, lot)) == 1


def test_trace_and_search_are_pure_reads(client: TestClient, db_session: Session):
    """The receipt fallback reads the receipt; it never back-fills the stock row."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1)
    lot = f"LOT-HT-{_next():05d}"
    heat = f"HEATP{_next():05d}"
    _receive(client, admin, line, lot=lot, heat=heat, cert="MC-PURE")
    (row,) = _stock_rows(db_session, lot)
    row.heat_lot = None
    row.cert_number = None
    row.supplier_id = None
    db_session.commit()
    before = _audit_count(db_session)

    assert client.get(f"/api/v1/traceability/lot/{lot}", headers=headers_for(admin)).json()["heat_lot"] == heat
    assert client.get("/api/v1/traceability/search", params={"q": heat}, headers=headers_for(admin)).json()

    (row,) = _stock_rows(db_session, lot)
    assert (row.heat_lot, row.cert_number, row.supplier_id) == (None, None, None)
    assert _audit_count(db_session) == before


def test_trace_never_falls_back_to_another_companys_receipt_of_the_same_lot(client: TestClient, db_session: Session):
    mine = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    theirs = make_user(db_session, role=UserRole.ADMIN, company_id=2)
    my_line = make_po_line(db_session, company_id=1)
    their_line = make_po_line(db_session, company_id=2)
    loc = make_location(db_session, company_id=1)
    lot = f"LOT-HT-{_next():05d}"
    _receive_manual(client, mine, my_line.part_id, loc.code, lot)
    _receive(client, theirs, their_line, lot=lot, heat="H-FOREIGN", cert="MC-FOREIGN")
    receipt = db_session.query(POReceipt).filter(POReceipt.lot_number == lot).one()
    receipt.certificate_document_id = 9191
    db_session.commit()

    body = client.get(f"/api/v1/traceability/lot/{lot}", headers=headers_for(mine)).json()
    assert body["part_id"] == my_line.part_id
    assert body["heat_lot"] is None
    assert body["cert_number"] is None
    assert body["certificate_document_id"] is None
    assert body["supplier_name"] is None
    assert not [h for h in body["history"] if h["event_type"] == "received"]


def test_trace_of_another_companys_held_lot_names_nothing(client: TestClient, db_session: Session):
    mine = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    theirs = make_user(db_session, role=UserRole.ADMIN, company_id=2)
    their_line = make_po_line(db_session, company_id=2)
    lot = f"LOT-HT-{_next():05d}"
    _receive(client, theirs, their_line, lot=lot, heat="H-FOREIGN-HELD", requires_inspection=True)

    resp = client.get(f"/api/v1/traceability/lot/{lot}", headers=headers_for(mine))
    if resp.status_code == status.HTTP_200_OK:
        body = resp.json()
        assert body["part_id"] is None
        assert body["heat_lot"] is None
        assert body["po_number"] is None
    else:
        assert resp.status_code == status.HTTP_404_NOT_FOUND


def test_trace_of_a_held_lot_shared_by_two_parts_names_neither(client: TestClient, db_session: Session):
    """No stock row names the part and the receipts disagree, so none speaks for the header."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    lot = f"LOT-HT-{_next():05d}"
    _receive(client, admin, make_po_line(db_session, company_id=1), lot=lot, heat="H-P1", requires_inspection=True)
    _receive(client, admin, make_po_line(db_session, company_id=1), lot=lot, heat="H-P2", requires_inspection=True)

    body = client.get(f"/api/v1/traceability/lot/{lot}", headers=headers_for(admin)).json()
    assert body["part_id"] is None
    assert body["heat_lot"] is None
    assert len([h for h in body["history"] if h["event_type"] == "received"]) == 2


def test_trace_earliest_receipt_wins_among_receipts(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1, quantity_ordered=20)
    lot = f"LOT-HT-{_next():05d}"
    _receive(client, admin, line, lot=lot, heat="H-EARLY", requires_inspection=True)
    _receive(client, admin, line, lot=lot, heat="H-LATER", requires_inspection=True)

    body = client.get(f"/api/v1/traceability/lot/{lot}", headers=headers_for(admin)).json()
    assert body["heat_lot"] == "H-EARLY"


def test_trace_supplier_from_stock_row_reads_through_soft_delete_but_not_across_tenants(
    client: TestClient, db_session: Session
):
    """A manually received lot has no receipt: its supplier comes from the row's supplier_id,
    read through a soft-deleted vendor, and never from another company's vendor row."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1)
    foreign_line = make_po_line(db_session, company_id=2)
    loc = make_location(db_session, company_id=1)
    lot = f"LOT-HT-{_next():05d}"
    _receive_manual(client, admin, line.part_id, loc.code, lot)
    (row,) = _stock_rows(db_session, lot)
    vendor = line.purchase_order.vendor
    vendor_name = vendor.name
    row.supplier_id = vendor.id
    vendor.soft_delete(admin.id)
    db_session.commit()

    body = client.get(f"/api/v1/traceability/lot/{lot}", headers=headers_for(admin)).json()
    assert body["supplier_name"] == vendor_name

    (row,) = _stock_rows(db_session, lot)
    row.supplier_id = foreign_line.purchase_order.vendor_id
    db_session.commit()
    body = client.get(f"/api/v1/traceability/lot/{lot}", headers=headers_for(admin)).json()
    assert body["supplier_name"] is None


def test_search_ignores_a_voided_receipt(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1)
    lot = f"LOT-HT-{_next():05d}"
    heat = f"HEATV{_next():05d}"
    receipt = _receive(client, admin, line, lot=lot, heat=heat, requires_inspection=True)
    resp = client.post(
        f"/api/v1/receiving/receipt/{receipt['id']}/void", headers=headers_for(admin), json={"reason": "Wrong PO."}
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    results = client.get("/api/v1/traceability/search", params={"q": heat}, headers=headers_for(admin)).json()
    assert results == []


def test_search_lists_a_lot_once_when_both_the_row_and_its_receipt_match(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1)
    lot = f"LOTDUP{_next():05d}"
    _receive(client, admin, line, lot=lot, heat="H-DUP")

    results = client.get("/api/v1/traceability/search", params={"q": lot}, headers=headers_for(admin)).json()
    hits = [r for r in results if r["number"] == lot]
    assert len(hits) == 1
    assert (hits[0]["heat_lot"], float(hits[0]["quantity"])) == ("H-DUP", 5)


# ---------------------------------------------------------------------------
# Review follow-ups: mixed heats are put on record, transfer copies are flagged,
# lot search keys on lot AND part, Correct stores the trimmed value
# ---------------------------------------------------------------------------


def test_po_merge_with_a_different_heat_puts_both_on_record(client: TestClient, db_session: Session):
    from app.models.inventory import InventoryTransaction

    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1, quantity_ordered=20)
    lot = f"LOT-HT-{_next():05d}"
    _receive(client, admin, line, lot=lot, heat="H-ONE", cert="MC-ONE")
    second = _receive(client, admin, line, lot=lot, heat="H-TWO", cert="MC-ONE")

    (row,) = _stock_rows(db_session, lot)
    assert row.heat_lot == "H-ONE"
    note = "heat received H-TWO differs from stock row heat H-ONE"
    merge_audit = (
        db_session.query(AuditLog)
        .filter(AuditLog.resource_type == "inventory", AuditLog.resource_id == row.id, AuditLog.action == "UPDATE")
        .order_by(AuditLog.sequence_number.desc())
        .first()
    )
    assert note in merge_audit.description
    assert "cert received" not in merge_audit.description
    txn = (
        db_session.query(InventoryTransaction)
        .filter(InventoryTransaction.reference_number == second["receipt_number"])
        .one()
    )
    assert note in txn.notes


def test_manual_receive_into_existing_row_fills_blank_heat_and_cert(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1)
    loc = make_location(db_session, company_id=1)
    lot = f"LOT-HT-{_next():05d}"
    _receive_manual(client, admin, line.part_id, loc.code, lot)
    _receive_manual(client, admin, line.part_id, loc.code, lot, heat_lot=" H-7781 ", cert_number="C-55")

    (row,) = _stock_rows(db_session, lot)
    assert float(row.quantity_on_hand) == 20
    assert (row.heat_lot, row.cert_number) == ("H-7781", "C-55")
    audit_row = (
        db_session.query(AuditLog)
        .filter(AuditLog.resource_type == "inventory", AuditLog.resource_id == row.id, AuditLog.action == "UPDATE")
        .order_by(AuditLog.sequence_number.desc())
        .first()
    )
    changes = audit_row.extra_data["changes"]
    assert changes["heat_lot"]["new"] == "H-7781"
    assert changes["cert_number"]["new"] == "C-55"


def test_manual_receive_with_a_different_heat_keeps_the_row_and_records_the_incoming(
    client: TestClient, db_session: Session
):
    from app.models.inventory import InventoryTransaction, TransactionType

    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1)
    loc = make_location(db_session, company_id=1)
    lot = f"LOT-HT-{_next():05d}"
    _receive_manual(client, admin, line.part_id, loc.code, lot, heat_lot="H-1111")
    _receive_manual(client, admin, line.part_id, loc.code, lot, heat_lot="H-2222", notes="Second skid")

    (row,) = _stock_rows(db_session, lot)
    assert row.heat_lot == "H-1111"
    txn = (
        db_session.query(InventoryTransaction)
        .filter(
            InventoryTransaction.inventory_item_id == row.id,
            InventoryTransaction.transaction_type == TransactionType.RECEIVE,
        )
        .order_by(InventoryTransaction.id.desc())
        .first()
    )
    note = "heat received H-2222 differs from stock row heat H-1111"
    assert txn.notes == f"Second skid; {note}"
    audit_row = (
        db_session.query(AuditLog)
        .filter(AuditLog.resource_type == "inventory", AuditLog.resource_id == row.id, AuditLog.action == "UPDATE")
        .order_by(AuditLog.sequence_number.desc())
        .first()
    )
    assert note in audit_row.description


def test_correct_flags_a_transfer_copy_still_holding_the_old_heat(client: TestClient, db_session: Session):
    """Known limit: the copy a transfer made is not rewritten, but the correction names it."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1)
    src = make_location(db_session, company_id=1)
    dst = make_location(db_session, company_id=1)
    lot = f"LOT-HT-{_next():05d}"
    resp = client.post(
        "/api/v1/receiving/receive",
        headers=headers_for(admin),
        json={
            "po_line_id": line.id,
            "quantity_received": 5,
            "lot_number": lot,
            "heat_number": "H1",
            "requires_inspection": False,
            "location_id": src.id,
        },
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    receipt = resp.json()
    (placed,) = _stock_rows(db_session, lot)
    resp = client.post(
        "/api/v1/inventory/transfer",
        headers=headers_for(admin),
        json={"inventory_item_id": placed.id, "quantity": 2, "to_location_code": dst.code},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    copy = next(r for r in _stock_rows(db_session, lot) if r.location == dst.code)
    assert copy.heat_lot == "H1"

    resp = client.patch(
        f"/api/v1/receiving/receipt/{receipt['id']}",
        headers=headers_for(admin),
        json={"quantity_received": 5, "heat_number": "H1-A", "reason": "Heat mis-keyed."},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    rows = {r.location: r for r in _stock_rows(db_session, lot)}
    assert rows[src.code].heat_lot == "H1-A"
    assert rows[dst.code].heat_lot == "H1"
    receipt_audit = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.resource_type == "receipt",
            AuditLog.resource_id == receipt["id"],
            AuditLog.description.like("Corrected receipt:%"),
        )
        .one()
    )
    assert receipt_audit.extra_data["stale_traceability_stock_row_ids"] == [copy.id]
    assert str(copy.id) in receipt_audit.description


def test_correct_without_a_transfer_flags_nothing(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1)
    lot = f"LOT-HT-{_next():05d}"
    receipt = _receive(client, admin, line, lot=lot, heat="H-X")
    resp = client.patch(
        f"/api/v1/receiving/receipt/{receipt['id']}",
        headers=headers_for(admin),
        json={"quantity_received": 5, "heat_number": "H-Y", "reason": "Heat mis-keyed."},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    receipt_audit = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.resource_type == "receipt",
            AuditLog.resource_id == receipt["id"],
            AuditLog.description.like("Corrected receipt:%"),
        )
        .one()
    )
    assert "stale_traceability_stock_row_ids" not in receipt_audit.extra_data


def test_correct_stores_the_trimmed_heat_and_clears_to_null(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1)
    lot = f"LOT-HT-{_next():05d}"
    receipt = _receive(client, admin, line, lot=lot, heat="H-OLD", cert="MC-OLD")
    resp = client.patch(
        f"/api/v1/receiving/receipt/{receipt['id']}",
        headers=headers_for(admin),
        json={"quantity_received": 5, "heat_number": " H2 ", "cert_number": "  ", "reason": "Cert withdrawn."},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    db_session.expire_all()
    stored = db_session.get(POReceipt, receipt["id"])
    assert (stored.heat_number, stored.cert_number) == ("H2", None)
    (row,) = _stock_rows(db_session, lot)
    assert (row.heat_lot, row.cert_number) == ("H2", None)


def test_search_keys_receipt_lot_hits_on_part_as_well_as_lot(client: TestClient, db_session: Session):
    """Part A's stock row of lot L must not hide part B's held receipt of lot L."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line_a = make_po_line(db_session, company_id=1)
    line_b = make_po_line(db_session, company_id=1)
    loc = make_location(db_session, company_id=1)
    lot = f"LOT-HT-{_next():05d}"
    heat = f"HEATB{_next():05d}"
    _receive_manual(client, admin, line_a.part_id, loc.code, lot)
    _receive(client, admin, line_b, lot=lot, heat=heat, requires_inspection=True)
    part_b = db_session.get(Part, line_b.part_id)
    part_a = db_session.get(Part, line_a.part_id)

    by_heat = client.get("/api/v1/traceability/search", params={"q": heat}, headers=headers_for(admin)).json()
    (hit,) = [r for r in by_heat if r["number"] == lot]
    assert (hit["part_number"], hit["heat_lot"]) == (part_b.part_number, heat)

    by_lot = client.get("/api/v1/traceability/search", params={"q": lot}, headers=headers_for(admin)).json()
    assert sorted(r["part_number"] for r in by_lot if r["number"] == lot) == sorted(
        [part_a.part_number, part_b.part_number]
    )
