"""Coverage for receipt correction + void (the delicate reconciliation path).

A mis-keyed received quantity is corrected, or a receipt voided, by SAFELY
unwinding everything the receive propagated:

- PO line ``quantity_received`` / ``is_closed`` roll back or forward.
- PO status recomputes (RECEIVED / PARTIAL / back to SENT).
- Dock-to-stock inventory is reconciled by appending a compensating signed
  ADJUST transaction -- the historical RECEIVE transaction is NEVER mutated or
  deleted (AS9100D movement history preserved).

The state model is enforced strictly: only PENDING_INSPECTION and dock-to-stock
ACCEPTED receipts are correctable/voidable; an inspected receipt is refused; a
lot change after stock placement is refused; and a reversal is refused outright
when the stock has already been allocated/consumed. Every refusal is an
actionable 4xx, never a corrupt inventory/PO total.
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.inventory import InventoryItem, InventoryTransaction, TransactionType
from app.models.purchasing import InspectionStatus, POReceipt, POStatus, ReceiptStatus
from app.models.user import UserRole
from tests.api.test_receiving_compliance import (
    _next,
    headers_for,
    make_pending_receipt,
    make_po_line,
    make_user,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _receive(client: TestClient, admin, line, *, qty, lot, requires_inspection):
    resp = client.post(
        "/api/v1/receiving/receive",
        headers=headers_for(admin),
        json={
            "po_line_id": line.id,
            "quantity_received": qty,
            "lot_number": lot,
            "requires_inspection": requires_inspection,
        },
    )
    assert resp.status_code in (status.HTTP_200_OK, status.HTTP_201_CREATED), resp.text
    return resp.json()


def _inv_item(db: Session, lot: str) -> InventoryItem:
    return db.query(InventoryItem).filter(InventoryItem.lot_number == lot).one()


def _txns(db: Session, inventory_item_id: int):
    return db.query(InventoryTransaction).filter(InventoryTransaction.inventory_item_id == inventory_item_id).all()


# ---------------------------------------------------------------------------
# Dock-to-stock correction: quantity DOWN
# ---------------------------------------------------------------------------


def test_correct_dock_to_stock_down_reconciles_everything(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1, quantity_ordered=10)
    lot = f"LOT-CORR-{_next():05d}"
    receipt_body = _receive(client, admin, line, qty=5, lot=lot, requires_inspection=False)
    receipt_id = receipt_body["id"]

    resp = client.patch(
        f"/api/v1/receiving/receipt/{receipt_id}",
        headers=headers_for(admin),
        json={"quantity_received": 2, "reason": "Miscounted the skid; only 2 arrived."},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert float(body["quantity_received"]) == 2
    # Dock-to-stock keeps accepted == received.
    assert float(body["quantity_accepted"]) == 2

    # Inventory reduced by the delta (5 -> 2), available recomputed.
    inv = _inv_item(db_session, lot)
    assert float(inv.quantity_on_hand) == 2
    assert float(inv.quantity_available) == 2

    # The historical RECEIVE transaction is preserved; a compensating ADJUST(-3)
    # was appended (never a mutation/delete of the RECEIVE row).
    txns = _txns(db_session, inv.id)
    receives = [t for t in txns if t.transaction_type == TransactionType.RECEIVE]
    adjusts = [t for t in txns if t.transaction_type == TransactionType.ADJUST]
    assert len(receives) == 1 and float(receives[0].quantity) == 5
    assert len(adjusts) == 1
    assert float(adjusts[0].quantity) == -3
    assert adjusts[0].reason_code == "RECEIPT_CORRECTION"
    assert adjusts[0].company_id == 1
    assert adjusts[0].reference_type == "po_receipt"
    assert adjusts[0].reference_id == receipt_id

    # PO line rolled back; still open (2 of 10) so PO is PARTIAL.
    db_session.refresh(line)
    assert float(line.quantity_received) == 2
    assert line.is_closed is False
    assert line.purchase_order.status == POStatus.PARTIAL


def test_correct_dock_to_stock_full_receipt_reopens_line_to_partial(client: TestClient, db_session: Session):
    """Correcting a fully-received (RECEIVED) PO down reopens the line -> PARTIAL."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1, quantity_ordered=10)
    lot = f"LOT-FULL-{_next():05d}"
    receipt_body = _receive(client, admin, line, qty=10, lot=lot, requires_inspection=False)
    db_session.refresh(line)
    assert line.is_closed is True
    assert line.purchase_order.status == POStatus.RECEIVED

    resp = client.patch(
        f"/api/v1/receiving/receipt/{receipt_body['id']}",
        headers=headers_for(admin),
        json={"quantity_received": 6, "reason": "Two boxes were short-shipped."},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.refresh(line)
    assert float(line.quantity_received) == 6
    assert line.is_closed is False
    assert line.purchase_order.status == POStatus.PARTIAL
    assert float(_inv_item(db_session, lot).quantity_on_hand) == 6


# ---------------------------------------------------------------------------
# Dock-to-stock correction: quantity UP
# ---------------------------------------------------------------------------


def test_correct_dock_to_stock_up_adds_inventory(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1, quantity_ordered=10)
    lot = f"LOT-UP-{_next():05d}"
    receipt_body = _receive(client, admin, line, qty=3, lot=lot, requires_inspection=False)

    resp = client.patch(
        f"/api/v1/receiving/receipt/{receipt_body['id']}",
        headers=headers_for(admin),
        json={"quantity_received": 8, "reason": "Undercounted; a fourth box was on the truck."},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    inv = _inv_item(db_session, lot)
    assert float(inv.quantity_on_hand) == 8
    adjusts = [t for t in _txns(db_session, inv.id) if t.transaction_type == TransactionType.ADJUST]
    assert len(adjusts) == 1 and float(adjusts[0].quantity) == 5
    db_session.refresh(line)
    assert float(line.quantity_received) == 8


# ---------------------------------------------------------------------------
# PENDING_INSPECTION correction: PO line only, no inventory
# ---------------------------------------------------------------------------


def test_correct_pending_inspection_touches_no_inventory(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1, quantity_ordered=10)
    lot = f"LOT-PEND-{_next():05d}"
    receipt_body = _receive(client, admin, line, qty=5, lot=lot, requires_inspection=True)
    assert receipt_body["status"] == ReceiptStatus.PENDING_INSPECTION.value

    resp = client.patch(
        f"/api/v1/receiving/receipt/{receipt_body['id']}",
        headers=headers_for(admin),
        json={"quantity_received": 4, "reason": "Recount before inspection."},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert float(body["quantity_received"]) == 4
    # Nothing placed yet -> accepted stays 0, still pending.
    assert float(body["quantity_accepted"]) == 0
    assert body["status"] == ReceiptStatus.PENDING_INSPECTION.value

    # No inventory row exists for this lot at all.
    assert db_session.query(InventoryItem).filter(InventoryItem.lot_number == lot).first() is None

    db_session.refresh(line)
    assert float(line.quantity_received) == 4


def test_correct_pending_inspection_allows_lot_change(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1, quantity_ordered=10)
    lot = f"LOT-OLD-{_next():05d}"
    new_lot = f"LOT-NEW-{_next():05d}"
    receipt_body = _receive(client, admin, line, qty=5, lot=lot, requires_inspection=True)

    resp = client.patch(
        f"/api/v1/receiving/receipt/{receipt_body['id']}",
        headers=headers_for(admin),
        json={"quantity_received": 5, "lot_number": new_lot, "reason": "Correct lot from the cert."},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["lot_number"] == new_lot


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_correct_refuses_lot_change_after_dock_to_stock(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1, quantity_ordered=10)
    lot = f"LOT-LOCK-{_next():05d}"
    receipt_body = _receive(client, admin, line, qty=5, lot=lot, requires_inspection=False)

    resp = client.patch(
        f"/api/v1/receiving/receipt/{receipt_body['id']}",
        headers=headers_for(admin),
        json={"quantity_received": 5, "lot_number": "LOT-DIFFERENT", "reason": "try to change lot"},
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
    assert "Lot number cannot be changed" in resp.json()["detail"]


def test_correct_refuses_inspected_receipt(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    quality = make_user(db_session, role=UserRole.QUALITY, company_id=1)
    line = make_po_line(db_session, company_id=1, quantity_ordered=10)
    lot = f"LOT-INSP-{_next():05d}"
    receipt_body = _receive(client, admin, line, qty=5, lot=lot, requires_inspection=True)

    insp = client.post(
        f"/api/v1/receiving/inspect/{receipt_body['id']}",
        headers=headers_for(quality),
        json={"quantity_accepted": 5, "quantity_rejected": 0, "inspection_method": "visual"},
    )
    assert insp.status_code in (status.HTTP_200_OK, status.HTTP_201_CREATED), insp.text
    assert insp.json()["receipt"]["inspection_status"] == InspectionStatus.PASSED.value

    resp = client.patch(
        f"/api/v1/receiving/receipt/{receipt_body['id']}",
        headers=headers_for(admin),
        json={"quantity_received": 3, "reason": "too late"},
    )
    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
    assert "already been inspected" in resp.json()["detail"]


def test_correct_refuses_when_stock_already_allocated(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1, quantity_ordered=10)
    lot = f"LOT-ALLOC-{_next():05d}"
    receipt_body = _receive(client, admin, line, qty=5, lot=lot, requires_inspection=False)

    # Simulate the stock being allocated to a work order (available drops to 0).
    inv = _inv_item(db_session, lot)
    inv.quantity_allocated = 5
    inv.quantity_available = 0
    db_session.commit()

    resp = client.patch(
        f"/api/v1/receiving/receipt/{receipt_body['id']}",
        headers=headers_for(admin),
        json={"quantity_received": 2, "reason": "reduce"},
    )
    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
    assert "allocated or consumed" in resp.json()["detail"]
    # Nothing changed: inventory + PO line intact.
    db_session.refresh(inv)
    assert float(inv.quantity_on_hand) == 5
    db_session.refresh(line)
    assert float(line.quantity_received) == 5


def test_correct_blank_reason_is_422(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1, quantity_ordered=10)
    lot = f"LOT-NOREASON-{_next():05d}"
    receipt_body = _receive(client, admin, line, qty=5, lot=lot, requires_inspection=False)

    resp = client.patch(
        f"/api/v1/receiving/receipt/{receipt_body['id']}",
        headers=headers_for(admin),
        json={"quantity_received": 2, "reason": "   "},
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, resp.text


def test_correct_cross_company_receipt_is_404(client: TestClient, db_session: Session):
    admin1 = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    other = make_pending_receipt(db_session, company_id=2)

    resp = client.patch(
        f"/api/v1/receiving/receipt/{other.id}",
        headers=headers_for(admin1),
        json={"quantity_received": 1, "reason": "cross tenant"},
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text


@pytest.mark.parametrize("role", [UserRole.OPERATOR, UserRole.QUALITY, UserRole.VIEWER])
def test_correct_forbidden_for_unauthorized_roles(client: TestClient, db_session: Session, role: UserRole):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    user = make_user(db_session, role=role, company_id=1)
    line = make_po_line(db_session, company_id=1, quantity_ordered=10)
    lot = f"LOT-RBAC-{_next():05d}"
    receipt_body = _receive(client, admin, line, qty=5, lot=lot, requires_inspection=False)

    resp = client.patch(
        f"/api/v1/receiving/receipt/{receipt_body['id']}",
        headers=headers_for(user),
        json={"quantity_received": 2, "reason": "nope"},
    )
    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text


# ---------------------------------------------------------------------------
# Void
# ---------------------------------------------------------------------------


def test_void_dock_to_stock_reverses_and_soft_deletes(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1, quantity_ordered=10)
    lot = f"LOT-VOID-{_next():05d}"
    receipt_body = _receive(client, admin, line, qty=5, lot=lot, requires_inspection=False)
    receipt_id = receipt_body["id"]

    resp = client.post(
        f"/api/v1/receiving/receipt/{receipt_id}/void",
        headers=headers_for(admin),
        json={"reason": "Duplicate receipt keyed by mistake."},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert "voided" in resp.json()["message"]

    # Receipt soft-deleted, zeroed.
    receipt = db_session.query(POReceipt).filter(POReceipt.id == receipt_id).one()
    assert receipt.is_deleted is True
    assert receipt.deleted_by == admin.id
    assert float(receipt.quantity_received) == 0
    assert float(receipt.quantity_accepted) == 0

    # Inventory fully reversed; RECEIVE preserved + compensating ADJUST(-5) appended.
    inv = _inv_item(db_session, lot)
    assert float(inv.quantity_on_hand) == 0
    adjusts = [t for t in _txns(db_session, inv.id) if t.transaction_type == TransactionType.ADJUST]
    assert len(adjusts) == 1
    assert float(adjusts[0].quantity) == -5
    assert adjusts[0].reason_code == "RECEIPT_VOID"

    # PO line back to zero -> PO returns to SENT (pre-receipt open state).
    db_session.refresh(line)
    assert float(line.quantity_received) == 0
    assert line.is_closed is False
    assert line.purchase_order.status == POStatus.SENT

    # Audit: a soft-delete DELETE row for the receipt.
    delete_logs = [
        log
        for log in db_session.query(AuditLog).all()
        if log.resource_type == "receipt" and log.action == "DELETE" and log.resource_id == receipt_id
    ]
    assert len(delete_logs) == 1


def test_void_pending_inspection_reverses_po_line_only(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1, quantity_ordered=10)
    lot = f"LOT-VPEND-{_next():05d}"
    receipt_body = _receive(client, admin, line, qty=5, lot=lot, requires_inspection=True)
    receipt_id = receipt_body["id"]

    resp = client.post(
        f"/api/v1/receiving/receipt/{receipt_id}/void",
        headers=headers_for(admin),
        json={"reason": "Wrong PO line."},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    receipt = db_session.query(POReceipt).filter(POReceipt.id == receipt_id).one()
    assert receipt.is_deleted is True
    # No inventory was ever placed for a pending receipt.
    assert db_session.query(InventoryItem).filter(InventoryItem.lot_number == lot).first() is None
    db_session.refresh(line)
    assert float(line.quantity_received) == 0
    assert line.purchase_order.status == POStatus.SENT


def test_void_refuses_when_stock_already_consumed(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1, quantity_ordered=10)
    lot = f"LOT-VCONS-{_next():05d}"
    receipt_body = _receive(client, admin, line, qty=5, lot=lot, requires_inspection=False)

    inv = _inv_item(db_session, lot)
    inv.quantity_allocated = 5
    inv.quantity_available = 0
    db_session.commit()

    resp = client.post(
        f"/api/v1/receiving/receipt/{receipt_body['id']}/void",
        headers=headers_for(admin),
        json={"reason": "attempt void"},
    )
    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
    assert "allocated or consumed" in resp.json()["detail"]
    # Receipt is NOT deleted; nothing reversed.
    receipt = db_session.query(POReceipt).filter(POReceipt.id == receipt_body["id"]).one()
    assert receipt.is_deleted is False
    assert float(receipt.quantity_received) == 5


def test_void_refuses_inspected_receipt(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    quality = make_user(db_session, role=UserRole.QUALITY, company_id=1)
    line = make_po_line(db_session, company_id=1, quantity_ordered=10)
    lot = f"LOT-VINSP-{_next():05d}"
    receipt_body = _receive(client, admin, line, qty=5, lot=lot, requires_inspection=True)
    client.post(
        f"/api/v1/receiving/inspect/{receipt_body['id']}",
        headers=headers_for(quality),
        json={"quantity_accepted": 5, "quantity_rejected": 0, "inspection_method": "visual"},
    )

    resp = client.post(
        f"/api/v1/receiving/receipt/{receipt_body['id']}/void",
        headers=headers_for(admin),
        json={"reason": "too late"},
    )
    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
    assert "already been inspected" in resp.json()["detail"]


def test_void_is_terminal_no_double_void(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1, quantity_ordered=10)
    lot = f"LOT-TERM-{_next():05d}"
    receipt_body = _receive(client, admin, line, qty=5, lot=lot, requires_inspection=False)
    receipt_id = receipt_body["id"]

    first = client.post(
        f"/api/v1/receiving/receipt/{receipt_id}/void",
        headers=headers_for(admin),
        json={"reason": "first void"},
    )
    assert first.status_code == status.HTTP_200_OK, first.text

    # A voided (soft-deleted) receipt is no longer visible: re-void and re-correct 404.
    second = client.post(
        f"/api/v1/receiving/receipt/{receipt_id}/void",
        headers=headers_for(admin),
        json={"reason": "second void"},
    )
    assert second.status_code == status.HTTP_404_NOT_FOUND, second.text

    corr = client.patch(
        f"/api/v1/receiving/receipt/{receipt_id}",
        headers=headers_for(admin),
        json={"quantity_received": 2, "reason": "correct a voided receipt"},
    )
    assert corr.status_code == status.HTTP_404_NOT_FOUND, corr.text


def test_void_forbidden_for_supervisor(client: TestClient, db_session: Session):
    """Void is delete authority: ADMIN/MANAGER only. SUPERVISOR (who can receive
    and correct) is NOT allowed to void."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR, company_id=1)
    line = make_po_line(db_session, company_id=1, quantity_ordered=10)
    lot = f"LOT-VSUP-{_next():05d}"
    receipt_body = _receive(client, admin, line, qty=5, lot=lot, requires_inspection=False)

    resp = client.post(
        f"/api/v1/receiving/receipt/{receipt_body['id']}/void",
        headers=headers_for(supervisor),
        json={"reason": "supervisor tries to void"},
    )
    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.MANAGER])
def test_void_allowed_for_admin_and_manager(client: TestClient, db_session: Session, role: UserRole):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    voider = make_user(db_session, role=role, company_id=1)
    line = make_po_line(db_session, company_id=1, quantity_ordered=10)
    lot = f"LOT-VOK-{_next():05d}"
    receipt_body = _receive(client, admin, line, qty=5, lot=lot, requires_inspection=False)

    resp = client.post(
        f"/api/v1/receiving/receipt/{receipt_body['id']}/void",
        headers=headers_for(voider),
        json={"reason": "authorized void"},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text


def test_void_cross_company_receipt_is_404(client: TestClient, db_session: Session):
    admin1 = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    other = make_pending_receipt(db_session, company_id=2)

    resp = client.post(
        f"/api/v1/receiving/receipt/{other.id}/void",
        headers=headers_for(admin1),
        json={"reason": "cross tenant"},
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text


# ---------------------------------------------------------------------------
# Tenant scoping of the compensating transaction / audit
# ---------------------------------------------------------------------------


def test_correction_audit_and_txn_are_tenant_stamped(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1, quantity_ordered=10)
    lot = f"LOT-STAMP-{_next():05d}"
    receipt_body = _receive(client, admin, line, qty=5, lot=lot, requires_inspection=False)
    receipt_id = receipt_body["id"]

    resp = client.patch(
        f"/api/v1/receiving/receipt/{receipt_id}",
        headers=headers_for(admin),
        json={"quantity_received": 3, "reason": "recount"},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    inv = _inv_item(db_session, lot)
    adjust = [t for t in _txns(db_session, inv.id) if t.transaction_type == TransactionType.ADJUST][0]
    assert adjust.company_id == 1

    # A receipt UPDATE audit row was recorded for the correction.
    update_logs = [
        log
        for log in db_session.query(AuditLog).all()
        if log.resource_type == "receipt" and log.action == "UPDATE" and log.resource_id == receipt_id
    ]
    assert len(update_logs) >= 1
