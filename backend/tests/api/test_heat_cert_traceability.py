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
