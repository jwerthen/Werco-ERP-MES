"""Coverage for POST /receiving/receipt/{id}/clear-inspection.

The owner's case: receiving ticks "requires inspection" on a line that never
needed it, and the lot then sits in the inspection queue with no honest way out.
Until this verb the ONLY exit was /void, which un-receives the material entirely
and forces the whole receipt to be re-keyed.

Clearing the hold is the NON-DESTRUCTIVE alternative: the receipt and its
lot / heat / cert stay exactly as keyed, the receipt is re-classified as the
dock-to-stock receipt it should have been (ACCEPTED / NOT_REQUIRED), the material
posts into inventory on the same key the receive path uses, and the row drops off
the inspection queue. Nothing is destroyed and nothing is re-keyed.

Three properties get the most attention here, because each of them is a rule
rather than a behavior:

- RECORDS INTEGRITY (AS9100D, the PR #127 defect): no incoming inspection
  happened, so the record must NOT assert one. ``inspection_status`` is
  NOT_REQUIRED (never PASSED) and ``inspection_method`` / ``inspected_by`` /
  ``inspected_at`` all stay NULL. Asserted explicitly, field by field.
- THE requires_inspection FLIP: ``_reconcile_receipt_quantity`` (the shared
  correct/void engine) derives ``inventory_placed = not
  receipt.requires_inspection`` -- that flag is the ONLY record of whether stock
  was placed. If it stayed True after this verb posted stock, a later Void would
  believe nothing was placed and would NOT reverse it, silently stranding the
  material on hand. The end-to-end receive -> clear -> void test below is the
  regression that pins it: delete the flip in the endpoint and that test fails
  with stock left on hand.
- THE PO LINE IS NOT TOUCHED: ``po_line.quantity_received`` / ``is_closed`` /
  ``po.status`` were all advanced when the receipt was CREATED (a
  PENDING_INSPECTION receipt still counts as received; only the inventory
  posting waits on inspection). Re-running that arithmetic here would
  double-count what arrived.
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.inventory import InventoryItem, InventoryTransaction, TransactionType
from app.models.operational_event import OperationalEvent
from app.models.purchasing import InspectionStatus, POReceipt, POStatus, ReceiptStatus
from app.models.user import UserRole
from tests.api.test_receiving_compliance import (
    _next,
    headers_for,
    inspect_payload,
    make_location,
    make_pending_receipt,
    make_po_line,
    make_user,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

CLEAR_REASON = "Receiving ticked the inspection box by mistake; this part is dock-to-stock."


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _receive(client: TestClient, user, line, *, qty, lot, requires_inspection=True, location_id=None):
    """Receive against a PO line through the real endpoint (so the PO line / PO
    status are advanced exactly the way production advances them)."""
    body = {
        "po_line_id": line.id,
        "quantity_received": qty,
        "lot_number": lot,
        "requires_inspection": requires_inspection,
    }
    if location_id is not None:
        body["location_id"] = location_id
    resp = client.post("/api/v1/receiving/receive", headers=headers_for(user), json=body)
    assert resp.status_code in (status.HTTP_200_OK, status.HTTP_201_CREATED), resp.text
    return resp.json()


def _clear(client: TestClient, user, receipt_id: int, reason: str = CLEAR_REASON):
    return client.post(
        f"/api/v1/receiving/receipt/{receipt_id}/clear-inspection",
        headers=headers_for(user),
        json={"reason": reason},
    )


def _inv_item(db: Session, lot: str):
    """Find the inventory row by LOT (a unique natural key per test), not by count."""
    return db.query(InventoryItem).filter(InventoryItem.lot_number == lot).one_or_none()


def _txns(db: Session, inventory_item_id: int):
    return db.query(InventoryTransaction).filter(InventoryTransaction.inventory_item_id == inventory_item_id).all()


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------


def test_clear_inspection_flips_receipt_to_dock_to_stock(client: TestClient, db_session: Session):
    """A PENDING_INSPECTION receipt becomes the dock-to-stock receipt it should
    have been: ACCEPTED / NOT_REQUIRED / requires_inspection False, with the full
    received quantity accepted."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1, quantity_ordered=10)
    lot = f"LOT-CLEAR-{_next():05d}"
    receipt_body = _receive(client, admin, line, qty=5, lot=lot)
    receipt_id = receipt_body["id"]
    assert receipt_body["status"] == ReceiptStatus.PENDING_INSPECTION.value
    assert receipt_body["requires_inspection"] is True
    assert float(receipt_body["quantity_accepted"]) == 0

    resp = _clear(client, admin, receipt_id)

    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    # Wire contract (ReceiptResponse serializes enums as .value).
    assert body["id"] == receipt_id
    assert body["status"] == ReceiptStatus.ACCEPTED.value
    assert body["inspection_status"] == InspectionStatus.NOT_REQUIRED.value == "not_required"
    assert body["requires_inspection"] is False
    assert float(body["quantity_accepted"]) == float(body["quantity_received"]) == 5

    # Same guarantee at rest in the DB row.
    receipt = db_session.query(POReceipt).filter(POReceipt.id == receipt_id).one()
    assert receipt.status == ReceiptStatus.ACCEPTED
    assert receipt.inspection_status == InspectionStatus.NOT_REQUIRED
    assert receipt.requires_inspection is False
    assert float(receipt.quantity_accepted) == 5.0
    assert float(receipt.quantity_received) == 5.0
    # Non-destructive: the lot / traceability keyed at receiving is untouched.
    assert receipt.lot_number == lot
    assert receipt.is_deleted is False


def test_clear_inspection_preserves_lot_heat_cert_verbatim(client: TestClient, db_session: Session):
    """Nothing gets re-keyed: the traceability fields survive byte-identical."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    receipt = make_pending_receipt(db_session, company_id=1, quantity=4)
    receipt.heat_number = "HEAT-99871"
    receipt.cert_number = "CERT-ABC-2026"
    receipt.serial_numbers = "SN-1,SN-2,SN-3,SN-4"
    db_session.commit()
    lot = receipt.lot_number

    resp = _clear(client, admin, receipt.id)

    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["lot_number"] == lot
    assert body["heat_number"] == "HEAT-99871"
    assert body["cert_number"] == "CERT-ABC-2026"
    assert body["serial_numbers"] == "SN-1,SN-2,SN-3,SN-4"


# ---------------------------------------------------------------------------
# 2. RECORDS INTEGRITY (AS9100D / PR #127): no inspection happened, so the
#    record must not assert that one did.
# ---------------------------------------------------------------------------


def test_clear_inspection_never_fabricates_an_inspection(client: TestClient, db_session: Session):
    """inspection_status is NOT_REQUIRED -- never PASSED -- and method /
    inspector / timestamp all stay NULL.

    This is the exact records-integrity defect flagged on PR #127 (where the
    dock-to-stock receive path used to stamp a VISUAL inspection performed by the
    receiver). Waiving a hold is NOT a passing inspection: no one looked at the
    material. Each field is asserted explicitly so a regression names itself.
    """
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1, quantity_ordered=10)
    lot = f"LOT-NOINSP-{_next():05d}"
    receipt_id = _receive(client, admin, line, qty=6, lot=lot)["id"]

    resp = _clear(client, admin, receipt_id)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()

    # Wire.
    assert body["inspection_status"] == "not_required"
    assert body["inspection_status"] != InspectionStatus.PASSED.value
    assert body["inspection_method"] is None
    assert body["inspected_by"] is None
    assert body["inspected_at"] is None
    assert body["defect_type"] is None

    # At rest.
    receipt = db_session.query(POReceipt).filter(POReceipt.id == receipt_id).one()
    assert receipt.inspection_status == InspectionStatus.NOT_REQUIRED
    assert receipt.inspection_status is not InspectionStatus.PASSED
    assert receipt.inspection_method is None
    assert receipt.inspected_by is None
    assert receipt.inspected_at is None
    assert receipt.defect_type is None
    # Who took delivery, and when, IS recorded -- that part really happened.
    assert receipt.received_by == admin.id
    assert receipt.received_at is not None


# ---------------------------------------------------------------------------
# 3. The material is posted into inventory on the receive path's exact key
# ---------------------------------------------------------------------------


def test_clear_inspection_posts_material_into_inventory(client: TestClient, db_session: Session):
    """Stock lands at (company, part, location code, lot) with a RECEIVE txn."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1, quantity_ordered=10)
    location = make_location(db_session, company_id=1)
    lot = f"LOT-POST-{_next():05d}"
    receipt_body = _receive(client, admin, line, qty=7, lot=lot, location_id=location.id)
    receipt_id = receipt_body["id"]
    # Nothing is placed while the lot is held for inspection.
    assert _inv_item(db_session, lot) is None

    resp = _clear(client, admin, receipt_id)
    assert resp.status_code == status.HTTP_200_OK, resp.text

    inv = _inv_item(db_session, lot)
    assert inv is not None
    assert inv.company_id == 1
    assert inv.part_id == line.part_id
    # The location KEY is the location's code (not its id) -- the same key
    # _reconcile_receipt_quantity re-finds the row by on a later correct/void.
    assert inv.location == location.code
    assert inv.lot_number == lot
    assert float(inv.quantity_on_hand) == 7
    assert float(inv.quantity_available) == 7
    assert float(inv.unit_cost) == float(line.unit_price)

    receives = [t for t in _txns(db_session, inv.id) if t.transaction_type == TransactionType.RECEIVE]
    assert len(receives) == 1
    txn = receives[0]
    assert txn.company_id == 1
    assert float(txn.quantity) == 7
    assert txn.lot_number == lot
    assert txn.to_location == location.code
    assert txn.reference_type == "po_receipt"
    assert txn.reference_number == receipt_body["receipt_number"]
    # The LEDGER says why the material entered stock. Warehouse -> Inventory ->
    # Stock Movements is the app's only rendering of inventory_transactions, so an
    # unstamped row there is indistinguishable from a normal receive, and a waived
    # quality hold would be invisible on the one screen that shows material moving.
    # Mirrors RECEIPT_VOID / RECEIPT_CORRECTION on the correct/void reconciler.
    assert txn.reason_code == "INSPECTION_HOLD_CLEARED"
    assert CLEAR_REASON in (txn.notes or "")


def test_clear_inspection_stamps_the_waiver_on_the_receipt_record(client: TestClient, db_session: Session):
    """The reason must live on the RECEIPT, not only in audit_log.

    GET /audit/ is ADMIN/MANAGER-only, so QUALITY and SUPERVISOR -- two of the four
    roles authorized to waive -- cannot read their own decision back. Without this stamp the
    cleared receipt is byte-indistinguishable from a receipt that was dock-to-stock
    from the first keystroke, on the receipt detail, in History and in the lot
    genealogy, and an auditor asking "why did this lot skip incoming inspection?"
    gets no answer from the quality record.

    It is appended to `notes` (never overwriting what receiving typed) and never to
    `inspection_notes`, which would imply inspection activity that did not happen.
    """
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    receipt = make_pending_receipt(db_session, company_id=1, quantity=3)
    receipt.notes = "Pallet arrived shrink-wrapped."
    db_session.commit()

    reason = "Commodity fastener, dock-to-stock; box ticked by mistake."
    resp = _clear(client, admin, receipt.id, reason=reason)
    assert resp.status_code == status.HTTP_200_OK, resp.text

    body = resp.json()
    assert "Pallet arrived shrink-wrapped." in body["notes"], "the receiver's own note was destroyed"
    assert "Inspection hold cleared by" in body["notes"]
    assert reason in body["notes"]
    # No inspection happened, so nothing may land on the inspection fields.
    assert body["inspection_notes"] is None

    db_session.expire_all()
    row = db_session.query(POReceipt).filter(POReceipt.id == receipt.id).one()
    assert reason in (row.notes or "")
    assert row.inspection_notes is None


def test_clear_inspection_stamps_the_waiver_when_the_receipt_had_no_notes(client: TestClient, db_session: Session):
    """The append must not depend on there being an existing note."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    receipt = make_pending_receipt(db_session, company_id=1, quantity=2)
    assert not receipt.notes

    resp = _clear(client, admin, receipt.id)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert (resp.json()["notes"] or "").startswith("[")
    assert CLEAR_REASON in resp.json()["notes"]


def test_clear_inspection_defaults_location_to_recv_01(client: TestClient, db_session: Session):
    """A receipt with no location posts to the "RECV-01" default -- byte-identical
    to the receive path's fallback, which is what makes the stock re-findable."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    receipt = make_pending_receipt(db_session, company_id=1, quantity=3)
    assert receipt.location_id is None
    lot = receipt.lot_number

    resp = _clear(client, admin, receipt.id)
    assert resp.status_code == status.HTTP_200_OK, resp.text

    inv = _inv_item(db_session, lot)
    assert inv is not None
    assert inv.location == "RECV-01"
    assert float(inv.quantity_on_hand) == 3

    receives = [t for t in _txns(db_session, inv.id) if t.transaction_type == TransactionType.RECEIVE]
    assert len(receives) == 1
    assert receives[0].to_location == "RECV-01"


# ---------------------------------------------------------------------------
# 4. THE REGRESSION THAT MATTERS: receive -> clear -> void reverses the stock.
#
# This is what the requires_inspection=False flip exists to guarantee. Remove the
# flip from the endpoint and _reconcile_receipt_quantity computes
# inventory_placed = False, skips the reversal entirely, and the void leaves the
# material stranded on hand with the receipt soft-deleted -- the worst outcome
# available (phantom stock nothing points at).
# ---------------------------------------------------------------------------


def test_void_after_clear_reverses_the_posted_stock(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1, quantity_ordered=10)
    lot = f"LOT-CLRVOID-{_next():05d}"
    receipt_body = _receive(client, admin, line, qty=5, lot=lot)
    receipt_id = receipt_body["id"]

    cleared = _clear(client, admin, receipt_id)
    assert cleared.status_code == status.HTTP_200_OK, cleared.text
    inv = _inv_item(db_session, lot)
    assert inv is not None and float(inv.quantity_on_hand) == 5

    void = client.post(
        f"/api/v1/receiving/receipt/{receipt_id}/void",
        headers=headers_for(admin),
        json={"reason": "Cleared in error; this receipt was a duplicate."},
    )
    assert void.status_code == status.HTTP_200_OK, void.text

    # THE assertion: the stock this verb posted is fully reversed.
    db_session.refresh(inv)
    assert float(inv.quantity_on_hand) == 0, "clear-inspection posted stock the void failed to reverse"
    assert float(inv.quantity_available) == 0

    # Reversed the AS9100D way: the historical RECEIVE row is preserved and a
    # compensating signed ADJUST is appended.
    txns = _txns(db_session, inv.id)
    receives = [t for t in txns if t.transaction_type == TransactionType.RECEIVE]
    adjusts = [t for t in txns if t.transaction_type == TransactionType.ADJUST]
    assert len(receives) == 1 and float(receives[0].quantity) == 5
    assert len(adjusts) == 1
    assert float(adjusts[0].quantity) == -5
    assert adjusts[0].reason_code == "RECEIPT_VOID"

    # And the rest of the void's contract still holds on a cleared receipt.
    receipt = db_session.query(POReceipt).filter(POReceipt.id == receipt_id).one()
    assert receipt.is_deleted is True
    assert float(receipt.quantity_received) == 0
    db_session.refresh(line)
    assert float(line.quantity_received) == 0
    assert line.is_closed is False
    assert line.purchase_order.status == POStatus.SENT


def test_correct_after_clear_reconciles_the_posted_stock(client: TestClient, db_session: Session):
    """Same discriminator, the other consumer: a post-clear quantity correction
    must reconcile inventory too (5 -> 2 leaves 2 on hand, not 5)."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1, quantity_ordered=10)
    lot = f"LOT-CLRCORR-{_next():05d}"
    receipt_id = _receive(client, admin, line, qty=5, lot=lot)["id"]

    assert _clear(client, admin, receipt_id).status_code == status.HTTP_200_OK

    corr = client.patch(
        f"/api/v1/receiving/receipt/{receipt_id}",
        headers=headers_for(admin),
        json={"quantity_received": 2, "reason": "Recount after the hold was cleared."},
    )
    assert corr.status_code == status.HTTP_200_OK, corr.text

    inv = _inv_item(db_session, lot)
    assert float(inv.quantity_on_hand) == 2, "post-clear correction did not reconcile the posted stock"
    adjusts = [t for t in _txns(db_session, inv.id) if t.transaction_type == TransactionType.ADJUST]
    assert len(adjusts) == 1 and float(adjusts[0].quantity) == -3
    assert adjusts[0].reason_code == "RECEIPT_CORRECTION"
    # Dock-to-stock semantics now apply: accepted tracks received.
    assert float(corr.json()["quantity_accepted"]) == 2


# ---------------------------------------------------------------------------
# 5. The PO line / PO status are NOT touched (they were advanced at receive time)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ordered, received, expect_closed, expect_po_status",
    [
        pytest.param(10, 10, True, POStatus.RECEIVED, id="fully_received_line_stays_closed"),
        pytest.param(10, 4, False, POStatus.PARTIAL, id="partial_line_stays_open"),
    ],
)
def test_clear_inspection_leaves_po_line_and_po_status_untouched(
    client: TestClient,
    db_session: Session,
    ordered: float,
    received: float,
    expect_closed: bool,
    expect_po_status: POStatus,
):
    """quantity_received / is_closed / po.status are byte-identical before and after.

    The material was already counted as received when the receipt was created --
    a PENDING_INSPECTION receipt advances the PO line just like a dock-to-stock
    one; only the inventory posting waits. Re-running that arithmetic here would
    double-count what arrived.
    """
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1, quantity_ordered=ordered)
    lot = f"LOT-POLINE-{_next():05d}"
    receipt_id = _receive(client, admin, line, qty=received, lot=lot)["id"]

    db_session.refresh(line)
    before = (float(line.quantity_received), line.is_closed, line.purchase_order.status)
    assert before == (float(received), expect_closed, expect_po_status)

    resp = _clear(client, admin, receipt_id)
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.refresh(line)
    db_session.refresh(line.purchase_order)
    after = (float(line.quantity_received), line.is_closed, line.purchase_order.status)
    assert after == before

    # No purchase_order STATUS_CHANGE audit row was written by the clear (the
    # receive's SENT->PARTIAL/RECEIVED row is the only one, and it predates it).
    po_changes = [
        log
        for log in db_session.query(AuditLog).all()
        if log.resource_type == "purchase_order"
        and log.action == "STATUS_CHANGE"
        and log.resource_id == line.purchase_order_id
    ]
    assert len(po_changes) == 1
    assert po_changes[0].new_values == {"status": expect_po_status.value}


# ---------------------------------------------------------------------------
# 6. The receipt drops off the inspection queue and out of the stats badge
# ---------------------------------------------------------------------------


def test_clear_inspection_removes_receipt_from_queue_and_stats(client: TestClient, db_session: Session):
    """The queue list and the /stats pending_inspection badge both drop by one --
    they must agree (the invariant the old 30-day queue cutoff used to break)."""
    quality = make_user(db_session, role=UserRole.QUALITY, company_id=1)
    target = make_pending_receipt(db_session, company_id=1, quantity=2)
    other = make_pending_receipt(db_session, company_id=1, quantity=2)

    queue = client.get("/api/v1/receiving/inspection-queue", headers=headers_for(quality))
    stats = client.get("/api/v1/receiving/stats", headers=headers_for(quality))
    assert queue.status_code == status.HTTP_200_OK, queue.text
    assert stats.status_code == status.HTTP_200_OK, stats.text
    assert {i["receipt_id"] for i in queue.json()} == {target.id, other.id}
    assert stats.json()["pending_inspection"] == 2

    resp = _clear(client, quality, target.id)
    assert resp.status_code == status.HTTP_200_OK, resp.text

    queue_after = client.get("/api/v1/receiving/inspection-queue", headers=headers_for(quality))
    stats_after = client.get("/api/v1/receiving/stats", headers=headers_for(quality))
    ids_after = {i["receipt_id"] for i in queue_after.json()}
    assert target.id not in ids_after
    assert other.id in ids_after, "clearing one hold must not disturb the others"
    assert stats_after.json()["pending_inspection"] == 1

    # The receipt is still there -- dropped off the QUEUE, not deleted.
    assert client.get(f"/api/v1/receiving/receipt/{target.id}", headers=headers_for(quality)).status_code == 200


# ---------------------------------------------------------------------------
# 7. Guards
# ---------------------------------------------------------------------------


def test_clear_inspection_replay_is_409_and_does_not_double_post(client: TestClient, db_session: Session):
    """The PENDING_INSPECTION guard doubles as the replay guard: a second call
    refuses, so stock can never be posted twice."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1, quantity_ordered=10)
    lot = f"LOT-REPLAY-{_next():05d}"
    receipt_id = _receive(client, admin, line, qty=5, lot=lot)["id"]

    first = _clear(client, admin, receipt_id)
    assert first.status_code == status.HTTP_200_OK, first.text

    second = _clear(client, admin, receipt_id, reason="Clicked the button twice.")
    assert second.status_code == status.HTTP_409_CONFLICT, second.text
    assert second.json()["detail"] == "Receipt is not pending inspection"

    # Stock posted exactly once.
    inv = _inv_item(db_session, lot)
    assert float(inv.quantity_on_hand) == 5
    receives = [t for t in _txns(db_session, inv.id) if t.transaction_type == TransactionType.RECEIVE]
    assert len(receives) == 1


def test_clear_inspection_on_dock_to_stock_receipt_is_409(client: TestClient, db_session: Session):
    """An ACCEPTED (never-held) receipt has no hold to clear."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1, quantity_ordered=10)
    lot = f"LOT-DTS409-{_next():05d}"
    receipt_body = _receive(client, admin, line, qty=5, lot=lot, requires_inspection=False)
    assert receipt_body["status"] == ReceiptStatus.ACCEPTED.value

    resp = _clear(client, admin, receipt_body["id"])
    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
    assert resp.json()["detail"] == "Receipt is not pending inspection"

    # And the already-placed stock was not topped up a second time.
    inv = _inv_item(db_session, lot)
    assert float(inv.quantity_on_hand) == 5


@pytest.mark.parametrize(
    "accepted, rejected, expect_status, expect_inspection",
    [
        pytest.param(5, 0, ReceiptStatus.ACCEPTED, InspectionStatus.PASSED, id="passed_inspection"),
        pytest.param(0, 5, ReceiptStatus.REJECTED, InspectionStatus.FAILED, id="failed_inspection"),
    ],
)
def test_clear_inspection_on_already_inspected_receipt_is_409(
    client: TestClient,
    db_session: Session,
    accepted: float,
    rejected: float,
    expect_status: ReceiptStatus,
    expect_inspection: InspectionStatus,
):
    """A real inspection already happened -- waiving it retroactively would
    rewrite a quality record. Refuse, and leave the inspection result intact."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    quality = make_user(db_session, role=UserRole.QUALITY, company_id=1)
    line = make_po_line(db_session, company_id=1, quantity_ordered=10)
    lot = f"LOT-INSPD-{_next():05d}"
    receipt_id = _receive(client, admin, line, qty=5, lot=lot)["id"]

    payload = {"quantity_accepted": accepted, "quantity_rejected": rejected, "inspection_method": "visual"}
    if rejected > 0:
        payload["defect_type"] = "dimensional"
        payload["inspection_notes"] = "Bore diameter out of tolerance on every piece."
    insp = client.post(f"/api/v1/receiving/inspect/{receipt_id}", headers=headers_for(quality), json=payload)
    assert insp.status_code in (status.HTTP_200_OK, status.HTTP_201_CREATED), insp.text

    resp = _clear(client, admin, receipt_id)
    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
    assert resp.json()["detail"] == "Receipt is not pending inspection"

    # The recorded inspection is untouched -- no downgrade to NOT_REQUIRED.
    receipt = db_session.query(POReceipt).filter(POReceipt.id == receipt_id).one()
    assert receipt.status == expect_status
    assert receipt.inspection_status == expect_inspection
    assert receipt.inspected_by == quality.id
    assert receipt.inspected_at is not None


def test_clear_inspection_nonexistent_receipt_is_404(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)

    resp = _clear(client, admin, 99999999)
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
    assert resp.json()["detail"] == "Receipt not found"


def test_clear_inspection_voided_receipt_is_404(client: TestClient, db_session: Session):
    """A voided (soft-deleted) receipt is invisible to this verb -- never a
    resurrection path that would re-post the material it just un-received."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1, quantity_ordered=10)
    lot = f"LOT-VOIDED-{_next():05d}"
    receipt_id = _receive(client, admin, line, qty=5, lot=lot)["id"]

    void = client.post(
        f"/api/v1/receiving/receipt/{receipt_id}/void",
        headers=headers_for(admin),
        json={"reason": "Wrong PO line."},
    )
    assert void.status_code == status.HTTP_200_OK, void.text

    resp = _clear(client, admin, receipt_id)
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
    assert resp.json()["detail"] == "Receipt not found"
    # Nothing was posted for the voided lot.
    assert _inv_item(db_session, lot) is None


def test_clear_inspection_orphaned_po_line_is_400_not_500(client: TestClient, db_session: Session):
    """An orphaned receipt (dangling po_line_id) has no part / unit price /
    vendor context to post into inventory. Clear 400, never a 500 -- and no
    half-mutated receipt left behind.

    SQLite in tests doesn't enforce FKs, which is what lets us build the shape.
    """
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    orphan = POReceipt(
        receipt_number=f"RCV-ORPHAN-{_next():05d}",
        po_line_id=99999999,
        quantity_received=3,
        lot_number=f"LOT-ORPHAN-{_next():05d}",
        status=ReceiptStatus.PENDING_INSPECTION,
        inspection_status=InspectionStatus.PENDING,
        requires_inspection=True,
        received_by=admin.id,
        company_id=1,
    )
    db_session.add(orphan)
    db_session.commit()
    db_session.refresh(orphan)

    resp = _clear(client, admin, orphan.id)

    assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
    assert "PO line no longer exists" in resp.json()["detail"]

    # Guards run before ANY mutation: the receipt is exactly as it was.
    db_session.refresh(orphan)
    assert orphan.status == ReceiptStatus.PENDING_INSPECTION
    assert orphan.inspection_status == InspectionStatus.PENDING
    assert orphan.requires_inspection is True
    assert _inv_item(db_session, orphan.lot_number) is None


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"reason": ""}, id="empty_string"),
        pytest.param({"reason": "   "}, id="whitespace_only"),
        pytest.param({"reason": "\t\n "}, id="tabs_and_newlines"),
        pytest.param({}, id="missing_field"),
    ],
)
def test_clear_inspection_requires_a_non_blank_reason(client: TestClient, db_session: Session, body: dict):
    """Waiving an AS9100D inspection hold must always carry a stated
    justification -- the audit row is worthless without one."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    receipt = make_pending_receipt(db_session, company_id=1, quantity=2)

    resp = client.post(
        f"/api/v1/receiving/receipt/{receipt.id}/clear-inspection",
        headers=headers_for(admin),
        json=body,
    )

    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, resp.text
    # Rejected at the schema boundary: nothing mutated, nothing posted.
    db_session.refresh(receipt)
    assert receipt.status == ReceiptStatus.PENDING_INSPECTION
    assert receipt.requires_inspection is True
    assert _inv_item(db_session, receipt.lot_number) is None


def test_clear_inspection_stores_the_reason_stripped(client: TestClient, db_session: Session):
    """The validator strips, mirroring ReceiptVoidRequest."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    receipt = make_pending_receipt(db_session, company_id=1, quantity=2)

    resp = _clear(client, admin, receipt.id, reason="   Box ticked in error.   ")
    assert resp.status_code == status.HTTP_200_OK, resp.text

    log = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.resource_type == "receipt",
            AuditLog.action == "STATUS_CHANGE",
            AuditLog.resource_id == receipt.id,
        )
        .one()
    )
    # extra_data also carries part_requires_inspection (recorded, never enforced --
    # asserted in full by the audit-row test below); this test pins the stripping.
    assert log.extra_data["reason"] == "Box ticked in error."


# ---------------------------------------------------------------------------
# 8. TENANT ISOLATION
# ---------------------------------------------------------------------------


def test_clear_inspection_cross_company_receipt_is_404_and_unmutated(client: TestClient, db_session: Session):
    """A company-2 admin gets 404 (never 403, never 200) for a company-1 receipt,
    and company 1's record is completely untouched.

    404 rather than 403 matters: a 403 would confirm the receipt id exists in
    another tenant. The caller's role WOULD pass the gate, so this proves the
    refusal is the tenant scoping, not RBAC.
    """
    company1_receipt = make_pending_receipt(db_session, company_id=1, quantity=5)
    admin2 = make_user(db_session, role=UserRole.ADMIN, company_id=2)
    lot = company1_receipt.lot_number

    resp = _clear(client, admin2, company1_receipt.id)

    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
    assert resp.json()["detail"] == "Receipt not found"

    db_session.refresh(company1_receipt)
    assert company1_receipt.company_id == 1
    assert company1_receipt.status == ReceiptStatus.PENDING_INSPECTION
    assert company1_receipt.inspection_status == InspectionStatus.PENDING
    assert company1_receipt.requires_inspection is True
    assert float(company1_receipt.quantity_accepted or 0) == 0
    assert company1_receipt.is_deleted is False
    # No stock leaked into either tenant.
    assert _inv_item(db_session, lot) is None
    assert db_session.query(InventoryTransaction).count() == 0
    # And no operational event was emitted for the untouched receipt.
    assert (
        db_session.query(OperationalEvent).filter(OperationalEvent.event_type == "receipt_inspection_cleared").count()
        == 0
    )


# ---------------------------------------------------------------------------
# 9. RBAC
#
# ADMIN / MANAGER / SUPERVISOR / QUALITY may clear. OPERATOR / SHIPPING / VIEWER
# may not. That list is deliberately IDENTICAL to the one on
# POST /receiving/inspect/{id}, and the identity is the whole argument.
#
# An earlier draft of this endpoint excluded SUPERVISOR on a segregation-of-duties
# reading: the receive tier that ticks "requires inspection" should not be the tier
# that waives it alone. The owner OVERRULED that, and the reasoning is pinned here
# because it is not obvious and a future reader will otherwise "restore" it.
#
# Excluding SUPERVISOR closed the honest exit while leaving the dishonest one wide
# open. The same supervisor still holds /inspect, so a mis-ticked receipt with no
# manager on site does not sit on the queue -- it gets pushed through
# Inspect -> Visual -> Pass, stamping a named inspector and a timestamp onto an
# inspection that never happened. That is a FABRICATED quality record: precisely the
# AS9100D records-integrity defect PR #127 fixed in code, only performed by a human
# instead of by the code. So widening this gate is a records-integrity IMPROVEMENT,
# not a convenience concession -- of the two records a supervisor can already
# produce, the reasoned, attributed, hash-chained waiver is the strictly more
# truthful one.
#
# This gate is NOT a two-person control and never was: every role that can clear a
# hold can also have placed it. What it enforces is attribution and visibility.
#
# The lockstep with /inspect is pinned BEHAVIORALLY below
# (test_clear_inspection_role_gate_is_in_lockstep_with_inspect) rather than by
# reading the source: if either list moves without the other, the dishonest exit
# reopens. VOID stays tighter (ADMIN/MANAGER) because that is delete authority -- a
# different question from whether an inspection was owed -- and is pinned in
# tests/api/test_receipt_correction_void.py.
# ---------------------------------------------------------------------------

# Every tenant role except PLATFORM_ADMIN, which is excluded on purpose: it clears
# ``require_role`` unconditionally (deps.py short-circuits on it before the list is
# consulted), so including it would prove nothing about either role list.
_TENANT_ROLES = [
    UserRole.ADMIN,
    UserRole.MANAGER,
    UserRole.SUPERVISOR,
    UserRole.OPERATOR,
    UserRole.QUALITY,
    UserRole.SHIPPING,
    UserRole.VIEWER,
]


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR, UserRole.QUALITY])
def test_clear_inspection_allowed_for_admin_manager_supervisor_quality(
    client: TestClient, db_session: Session, role: UserRole
):
    user = make_user(db_session, role=role, company_id=1)
    receipt = make_pending_receipt(db_session, company_id=1, quantity=2)

    resp = _clear(client, user, receipt.id)

    assert resp.status_code != status.HTTP_403_FORBIDDEN, resp.text
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["status"] == ReceiptStatus.ACCEPTED.value


@pytest.mark.parametrize("role", [UserRole.OPERATOR, UserRole.SHIPPING, UserRole.VIEWER])
def test_clear_inspection_forbidden_for_operator_shipping_viewer(
    client: TestClient, db_session: Session, role: UserRole
):
    """The gate is still a gate.

    Kept as a real differential alongside the allowed-roles case above: without a
    role the endpoint genuinely refuses, an ungated endpoint would pass the whole
    RBAC section. These three are the floor roles that must never be able to take a
    lot off the inspection queue.
    """
    user = make_user(db_session, role=role, company_id=1)
    receipt = make_pending_receipt(db_session, company_id=1, quantity=2)

    resp = _clear(client, user, receipt.id)

    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text
    # Refused before anything moved.
    db_session.refresh(receipt)
    assert receipt.status == ReceiptStatus.PENDING_INSPECTION
    assert receipt.requires_inspection is True
    assert _inv_item(db_session, receipt.lot_number) is None


@pytest.mark.parametrize("role", _TENANT_ROLES)
def test_clear_inspection_role_gate_is_in_lockstep_with_inspect(
    client: TestClient, db_session: Session, role: UserRole
):
    """Role for role, whatever /inspect admits, clear-inspection admits.

    This is the assertion the endpoint comment asks for ("keep this list in lockstep
    with /inspect: if one moves, the other moves, or the dishonest exit reopens").
    It is asserted through the WIRE, not by parsing ``require_role`` lists, so it
    survives a refactor of how either gate is expressed.

    The direction that matters is /inspect admitting a role this verb refuses: that
    person's only exit from a mis-ticked hold becomes Inspect -> Visual -> Pass,
    which fabricates an inspection that never happened. Equality is asserted rather
    than one-way containment because the reverse drift is also wrong -- a role that
    may waive an inspection but not perform one is a gate nobody designed.
    """
    user = make_user(db_session, role=role, company_id=1)

    inspect_target = make_pending_receipt(db_session, company_id=1, quantity=2)
    inspect_resp = client.post(
        f"/api/v1/receiving/inspect/{inspect_target.id}",
        headers=headers_for(user),
        json=inspect_payload(quantity_accepted=2),
    )

    clear_target = make_pending_receipt(db_session, company_id=1, quantity=2)
    clear_resp = _clear(client, user, clear_target.id)

    inspect_refused = inspect_resp.status_code == status.HTTP_403_FORBIDDEN
    clear_refused = clear_resp.status_code == status.HTTP_403_FORBIDDEN
    assert clear_refused == inspect_refused, (
        f"{role.value}: /receiving/inspect returned {inspect_resp.status_code} but "
        f"clear-inspection returned {clear_resp.status_code}. The two role lists have "
        "drifted apart -- see this test's docstring before changing either."
    )


def test_supervisor_clear_is_the_whole_verb_end_to_end(client: TestClient, db_session: Session):
    """The role the owner added gets the WHOLE verb, not merely a non-403.

    The same SUPERVISOR receives the material (ticking the inspection box) and then
    waives the hold -- the exact scenario the widened gate exists for, and the one a
    narrower gate pushed into a fabricated Visual pass. All four effects are asserted
    on that path, because "supervisor gets a 200" would still pass if the widened
    gate reached a half-working code path.
    """
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR, company_id=1)
    line = make_po_line(db_session, company_id=1, quantity_ordered=10)
    location = make_location(db_session, company_id=1)
    lot = f"LOT-SUPCLEAR-{_next():05d}"
    reason = "Second-shift supervisor: commodity washer is dock-to-stock; the box was ticked in error."

    # The supervisor is the receive tier, so the badge that ticked the box...
    receipt_body = _receive(client, supervisor, line, qty=4, lot=lot, location_id=location.id)
    receipt_id = receipt_body["id"]
    assert receipt_body["status"] == ReceiptStatus.PENDING_INSPECTION.value
    assert _inv_item(db_session, lot) is None, "nothing is placed while the lot is held"

    # ...is the badge that clears the hold it created.
    resp = _clear(client, supervisor, receipt_id, reason=reason)
    assert resp.status_code == status.HTTP_200_OK, resp.text

    # (1) Re-classified as the dock-to-stock receipt it should have been.
    body = resp.json()
    assert body["status"] == ReceiptStatus.ACCEPTED.value
    assert body["inspection_status"] == InspectionStatus.NOT_REQUIRED.value
    assert body["requires_inspection"] is False
    assert float(body["quantity_accepted"]) == float(body["quantity_received"]) == 4

    db_session.expire_all()
    receipt = db_session.query(POReceipt).filter(POReceipt.id == receipt_id).one()
    assert receipt.status == ReceiptStatus.ACCEPTED
    assert (
        receipt.requires_inspection is False
    ), "the flip is what keeps a later Correct/Void able to reverse the stock this verb placed"
    assert receipt.lot_number == lot

    # (2) RECORDS INTEGRITY on the widened path -- the whole reason the owner widened
    #     it. A fabricated pass would look exactly like PASSED with the supervisor's
    #     name and a timestamp on it, so the supervisor path is asserted field by
    #     field, not assumed to inherit the admin path's guarantee.
    assert receipt.inspection_status == InspectionStatus.NOT_REQUIRED
    assert receipt.inspection_status is not InspectionStatus.PASSED
    assert receipt.inspection_method is None
    assert receipt.inspected_by is None, "no one inspected; the clearing supervisor must not be stamped as inspector"
    assert receipt.inspected_at is None
    assert receipt.defect_type is None
    assert receipt.inspection_notes is None
    # Taking delivery DID happen, and stays attributed.
    assert receipt.received_by == supervisor.id

    # (3) The material posts, on the receive path's exact key, stamped in the ledger.
    inv = _inv_item(db_session, lot)
    assert inv is not None, "the supervisor's waiver did not place the stock"
    assert inv.company_id == 1
    assert inv.part_id == line.part_id
    assert inv.location == location.code
    assert inv.lot_number == lot
    assert float(inv.quantity_on_hand) == 4

    receives = [t for t in _txns(db_session, inv.id) if t.transaction_type == TransactionType.RECEIVE]
    assert len(receives) == 1
    txn = receives[0]
    assert txn.company_id == 1
    assert float(txn.quantity) == 4
    assert txn.reference_type == "po_receipt"
    assert txn.reference_number == receipt_body["receipt_number"]
    assert txn.reason_code == "INSPECTION_HOLD_CLEARED"
    assert reason in (txn.notes or "")

    # (4) The waiver is attributed to the SUPERVISOR on the tamper-evident chain,
    #     carrying the typed reason.
    logs = db_session.query(AuditLog).order_by(AuditLog.sequence_number).all()
    status_changes = [
        log
        for log in logs
        if log.resource_type == "receipt" and log.action == "STATUS_CHANGE" and log.resource_id == receipt_id
    ]
    assert len(status_changes) == 1
    row = status_changes[0]
    assert row.user_id == supervisor.id
    assert row.company_id == 1
    assert row.old_values == {"status": ReceiptStatus.PENDING_INSPECTION.value}
    assert row.new_values == {"status": ReceiptStatus.ACCEPTED.value}
    assert reason in row.description
    assert row.extra_data["reason"] == reason
    assert row.resource_identifier == receipt_body["receipt_number"]
    assert row.integrity_hash

    # ...and onto the RECEIPT itself, which matters more for SUPERVISOR than for
    # anyone: GET /audit/ is ADMIN/MANAGER-only, so a supervisor cannot read that
    # audit row back. The note is the only place their own justification is legible
    # to them, and to an auditor reading the quality record.
    assert "Inspection hold cleared by" in (receipt.notes or "")
    assert reason in (receipt.notes or "")


# ---------------------------------------------------------------------------
# 10. AUDIT + operational event
# ---------------------------------------------------------------------------


def test_clear_inspection_writes_a_status_change_audit_row_with_the_reason(client: TestClient, db_session: Session):
    """pending_inspection -> accepted, carrying the typed reason, on the
    tamper-evident hash chain. _add_to_inventory contributes its own rows."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1, quantity_ordered=10)
    lot = f"LOT-AUDIT-{_next():05d}"
    receipt_body = _receive(client, admin, line, qty=5, lot=lot)
    receipt_id = receipt_body["id"]
    reason = "Buyer confirmed this commodity is dock-to-stock; hold was a mis-click."

    resp = _clear(client, admin, receipt_id, reason=reason)
    assert resp.status_code == status.HTTP_200_OK, resp.text

    logs = db_session.query(AuditLog).order_by(AuditLog.sequence_number).all()

    status_changes = [
        log
        for log in logs
        if log.resource_type == "receipt" and log.action == "STATUS_CHANGE" and log.resource_id == receipt_id
    ]
    assert len(status_changes) == 1
    row = status_changes[0]
    assert row.old_values == {"status": ReceiptStatus.PENDING_INSPECTION.value}
    assert row.new_values == {"status": ReceiptStatus.ACCEPTED.value}
    assert reason in row.description
    # part_requires_inspection records whether the PART MASTER flagged this part as
    # needing inspection at the moment of the waiver, so a waiver that contradicted
    # the master data is findable afterwards. Recorded, never enforced.
    assert set(row.extra_data) == {"reason", "part_requires_inspection"}
    assert row.extra_data["reason"] == reason
    assert row.company_id == 1
    assert row.user_id == admin.id
    assert row.resource_identifier == receipt_body["receipt_number"]

    # The inventory posting is audited too (fresh lot -> CREATE).
    inventory_logs = [log for log in logs if log.resource_type == "inventory" and log.action in ("CREATE", "UPDATE")]
    assert len(inventory_logs) >= 1

    # Hash chain sanity: strictly increasing sequence numbers, linked hashes.
    seqs = [log.sequence_number for log in logs]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)
    assert all(log.integrity_hash for log in logs)
    for prev, curr in zip(logs, logs[1:]):
        assert curr.previous_hash == prev.integrity_hash


def test_clear_inspection_emits_operational_event(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1, quantity_ordered=10)
    lot = f"LOT-EVENT-{_next():05d}"
    receipt_body = _receive(client, admin, line, qty=5, lot=lot)
    receipt_id = receipt_body["id"]

    resp = _clear(client, admin, receipt_id)
    assert resp.status_code == status.HTTP_200_OK, resp.text

    event = (
        db_session.query(OperationalEvent)
        .filter(
            OperationalEvent.event_type == "receipt_inspection_cleared",
            OperationalEvent.entity_id == receipt_id,
        )
        .one()
    )
    assert event.company_id == 1
    assert event.source_module == "purchasing"
    assert event.entity_type == "po_receipt"
    assert event.severity == "medium"
    assert event.user_id == admin.id

    payload = event.event_payload
    assert payload["receipt_number"] == receipt_body["receipt_number"]
    assert payload["po_id"] == line.purchase_order_id
    assert payload["po_number"] == line.purchase_order.po_number
    assert payload["po_line_id"] == line.id
    assert payload["part_id"] == line.part_id
    assert float(payload["quantity_received"]) == 5
    assert payload["reason"] == CLEAR_REASON
    assert "part_requires_inspection" in payload


def test_clear_inspection_event_is_wired_to_a_notification_catalog_entry(client: TestClient, db_session: Session):
    """The waiver must NOT be the silent one of the three post-receipt verbs.

    receipt.voided and receipt.corrected both notify MANAGER + QUALITY. Clearing a
    hold is the most quality-relevant of the three -- it takes a lot OFF the
    inspection queue and puts uninspected material on the shelf -- and the audit row
    does not cover it, because GET /audit/ is ADMIN/MANAGER-only while QUALITY and
    SUPERVISOR -- two of the four roles authorized to perform the waiver -- cannot read
    it. An uncataloged event type is
    silently ignored by the outbox tee (operational_event_service), so this asserts
    the wiring rather than the emit.
    """
    from app.services.notification_catalog import CHANNEL_IN_APP, entry_for_event_type

    entry = entry_for_event_type("receipt_inspection_cleared")
    assert entry is not None, "receipt_inspection_cleared has no catalog entry -- the waiver notifies nobody"
    assert entry.event_key == "receipt.inspection_cleared"
    assert CHANNEL_IN_APP in entry.default_channels
    assert UserRole.MANAGER in entry.roles and UserRole.QUALITY in entry.roles
