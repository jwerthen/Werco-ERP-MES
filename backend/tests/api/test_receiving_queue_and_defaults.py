"""Coverage for the 2026-07-16 receiving fixes.

- TASK A: the receipt-level "requires inspection" default is a plain FALSE
  (owner's literal ask: "default to no inspection required when receiving
  material"). An omitted requires_inspection on /receive means dock-to-stock —
  the part master's Part.requires_inspection is NOT applied server-side (it is
  only an advisory hint in the receiving UI, exposed on the /open-pos and
  /po/{id} line payloads); an explicit true still holds the lot for inspection,
  and the model's Python-side column default is False.
- TASK B: the inspection queue never ages out a pending receipt (days_back is
  now optional with no default cutoff, but still filters when provided; bounded
  1..3650 so a negative value can't hide the queue and a huge one can't 500),
  and an orphaned receipt row degrades per-row to None context fields instead
  of 500ing the whole list — /history and /receipt/{id} degrade the same way,
  while /inspect/{id} refuses an orphan with a clear 400, never a 500.
- TASK C: empty-string dates from HTML forms coerce to None on the PO schemas
  (blank required/expected date no longer 422s) — on create AND update
  (POUpdate parity) — while the expected_date > required_date rule still
  enforces on real create values.
"""

from datetime import datetime, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.models.inventory import InventoryItem
from app.models.purchasing import InspectionStatus, POReceipt, ReceiptStatus
from app.models.user import UserRole
from app.schemas.purchasing import POCreate, POLineCreate, POUpdate, ReceiptCreate
from tests.api.test_receiving_compliance import (
    _next,
    headers_for,
    inspect_payload,
    make_pending_receipt,
    make_po_line,
    make_user,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]


# ---------------------------------------------------------------------------
# TASK A: omitted requires_inspection means False (dock-to-stock) — the part
# master flag is only an advisory hint in the UI, never a server-side default
# ---------------------------------------------------------------------------


def _set_part_inspection_flag(db: Session, line, value: bool) -> None:
    """Flip the part-master requires_inspection flag on a helper-built line."""
    line.part.requires_inspection = value
    db.commit()


def test_receipt_create_schema_defaults_to_false():
    """Omitted flag = plain False (no part-master deferral in the schema)."""
    body = ReceiptCreate(po_line_id=1, quantity_received=1, lot_number="LOT-DEFAULT")
    assert body.requires_inspection is False


def test_po_receipt_model_defaults_to_no_inspection(db_session: Session):
    """Python-side column default: a POReceipt written without the flag is False."""
    line = make_po_line(db_session, company_id=1)
    receiver = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    receipt = POReceipt(
        receipt_number=f"RCV-DEF-{_next():05d}",
        po_line_id=line.id,
        quantity_received=1,
        lot_number="LOT-MODEL-DEFAULT",
        received_by=receiver.id,
        company_id=1,
    )
    db_session.add(receipt)
    db_session.commit()
    db_session.refresh(receipt)
    assert receipt.requires_inspection is False


def test_receive_without_flag_defaults_to_no_inspection(client: TestClient, db_session: Session):
    """Omitted flag -> auto-accept (dock-to-stock), straight into inventory."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1)
    _set_part_inspection_flag(db_session, line, False)

    resp = client.post(
        "/api/v1/receiving/receive",
        headers=headers_for(admin),
        json={
            "po_line_id": line.id,
            "quantity_received": 5,
            "lot_number": "LOT-NO-FLAG",
            # requires_inspection deliberately omitted -> defaults to False
        },
    )

    assert resp.status_code in (status.HTTP_200_OK, status.HTTP_201_CREATED), resp.text
    body = resp.json()
    assert body["requires_inspection"] is False
    assert body["status"] == ReceiptStatus.ACCEPTED.value

    # Auto-accept means the material landed in inventory (lot-traceable).
    inv_item = db_session.query(InventoryItem).filter(InventoryItem.lot_number == "LOT-NO-FLAG").one()
    assert inv_item.company_id == 1
    assert float(inv_item.quantity_on_hand) == 5


def test_receive_without_flag_ignores_part_master_true(client: TestClient, db_session: Session):
    """Omitted flag + part.requires_inspection=True -> STILL dock-to-stock.

    The owner's literal ask: receiving defaults to "no inspection required".
    This tenant's part master was bulk-imported with requires_inspection=True
    on ~every part, so deferring to it would defeat the default — the part
    flag is only an advisory hint in the receiving UI, never a server-side
    override.
    """
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1)
    _set_part_inspection_flag(db_session, line, True)

    resp = client.post(
        "/api/v1/receiving/receive",
        headers=headers_for(admin),
        json={
            "po_line_id": line.id,
            "quantity_received": 5,
            "lot_number": "LOT-PART-FLAGGED",
            # requires_inspection deliberately omitted -> defaults to False
        },
    )

    assert resp.status_code in (status.HTTP_200_OK, status.HTTP_201_CREATED), resp.text
    body = resp.json()
    assert body["requires_inspection"] is False
    assert body["status"] == ReceiptStatus.ACCEPTED.value

    # Dock-to-stock: the material landed in inventory immediately.
    inv_item = db_session.query(InventoryItem).filter(InventoryItem.lot_number == "LOT-PART-FLAGGED").one()
    assert float(inv_item.quantity_on_hand) == 5


def test_receive_explicit_false_with_part_master_true_dock_to_stock(client: TestClient, db_session: Session):
    """An explicit false behaves like the default even when the part is flagged."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1)
    _set_part_inspection_flag(db_session, line, True)

    resp = client.post(
        "/api/v1/receiving/receive",
        headers=headers_for(admin),
        json={
            "po_line_id": line.id,
            "quantity_received": 5,
            "lot_number": "LOT-EXPLICIT-FALSE",
            "requires_inspection": False,
        },
    )

    assert resp.status_code in (status.HTTP_200_OK, status.HTTP_201_CREATED), resp.text
    body = resp.json()
    assert body["requires_inspection"] is False
    assert body["status"] == ReceiptStatus.ACCEPTED.value


def test_receive_with_explicit_true_still_lands_pending_inspection(client: TestClient, db_session: Session):
    """Regression guard on the flipped default: opting IN still queues inspection.

    An explicit requires_inspection=true must land PENDING_INSPECTION and must
    NOT put anything into inventory until inspection completes.
    """
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1)

    resp = client.post(
        "/api/v1/receiving/receive",
        headers=headers_for(admin),
        json={
            "po_line_id": line.id,
            "quantity_received": 5,
            "lot_number": "LOT-EXPLICIT-TRUE",
            "requires_inspection": True,
        },
    )

    assert resp.status_code in (status.HTTP_200_OK, status.HTTP_201_CREATED), resp.text
    body = resp.json()
    assert body["requires_inspection"] is True
    assert body["status"] == ReceiptStatus.PENDING_INSPECTION.value
    # Opting in queues the lot for inspection: inspection_status is PENDING (not
    # yet resolved), and no inspector/method is fabricated on receipt.
    assert body["inspection_status"] == InspectionStatus.PENDING.value
    assert body["inspection_method"] is None
    assert body["inspected_by"] is None
    assert body["inspected_at"] is None

    # Nothing in inventory yet — the lot is held for inspection.
    inv_item = db_session.query(InventoryItem).filter(InventoryItem.lot_number == "LOT-EXPLICIT-TRUE").first()
    assert inv_item is None


def test_receiving_po_payloads_expose_part_inspection_flag(client: TestClient, db_session: Session):
    """/open-pos and /po/{id} line payloads carry the part-master flag.

    The Receiving UI renders an advisory hint next to its (always-unchecked)
    "Requires Inspection" checkbox from this field, so the receiver can opt in
    deliberately for a part flagged in the part master.
    """
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1)
    _set_part_inspection_flag(db_session, line, True)
    po_id = line.purchase_order_id

    open_resp = client.get("/api/v1/receiving/open-pos", headers=headers_for(admin))
    assert open_resp.status_code == status.HTTP_200_OK, open_resp.text
    open_po = next(p for p in open_resp.json() if p["po_id"] == po_id)
    assert open_po["lines"][0]["requires_inspection"] is True

    detail_resp = client.get(f"/api/v1/receiving/po/{po_id}", headers=headers_for(admin))
    assert detail_resp.status_code == status.HTTP_200_OK, detail_resp.text
    assert detail_resp.json()["lines"][0]["requires_inspection"] is True


# ---------------------------------------------------------------------------
# TASK A (records integrity): dock-to-stock stamps inspection_status
# NOT_REQUIRED and fabricates NO inspector / method / time.
#
# The core AS9100D records-integrity guarantee of the PR #127 follow-up: a
# no-inspection (dock-to-stock) receipt must NOT assert a passed VISUAL
# inspection performed by the receiver — no inspection occurred. It lands
# inspection_status = NOT_REQUIRED (the honest state, distinct from PASSED),
# quantity_accepted = full received qty, status = ACCEPTED, and
# inspection_method / inspected_by / inspected_at stay NULL. Only received_by /
# received_at (who took delivery, and when) are recorded.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "flag_payload",
    [
        pytest.param({}, id="omitted_flag"),
        pytest.param({"requires_inspection": False}, id="explicit_false"),
    ],
)
def test_dock_to_stock_stamps_not_required_and_fabricates_no_inspection(
    client: TestClient, db_session: Session, flag_payload: dict
):
    """A no-inspection receive records NOT_REQUIRED with a NULL inspector/method/time.

    Covers both the omitted flag and an explicit requires_inspection=false — the
    records-integrity guarantee must hold identically for both.
    """
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1, quantity_ordered=10)
    lot = f"LOT-DTS-{_next():05d}"

    resp = client.post(
        "/api/v1/receiving/receive",
        headers=headers_for(admin),
        json={"po_line_id": line.id, "quantity_received": 7, "lot_number": lot, **flag_payload},
    )

    assert resp.status_code in (status.HTTP_200_OK, status.HTTP_201_CREATED), resp.text
    body = resp.json()

    # Wire contract (ReceiptResponse serializes enums as .value): NOT_REQUIRED, not
    # PASSED; ACCEPTED; and NO fabricated inspection method / inspector / time.
    assert body["inspection_status"] == InspectionStatus.NOT_REQUIRED.value == "not_required"
    assert body["status"] == ReceiptStatus.ACCEPTED.value
    assert body["requires_inspection"] is False
    assert body["inspection_method"] is None
    assert body["inspected_by"] is None
    assert body["inspected_at"] is None
    # Full received qty is accepted into stock (dock-to-stock).
    assert float(body["quantity_accepted"]) == 7
    assert float(body["quantity_received"]) == 7

    # Same guarantee at rest in the DB row.
    receipt = db_session.query(POReceipt).filter(POReceipt.id == body["id"]).one()
    assert receipt.inspection_status == InspectionStatus.NOT_REQUIRED
    assert receipt.inspection_method is None
    assert receipt.inspected_by is None
    assert receipt.inspected_at is None
    assert receipt.status == ReceiptStatus.ACCEPTED
    assert float(receipt.quantity_accepted) == 7

    # The lot is traceable in inventory (dock-to-stock landed the material).
    inv_item = db_session.query(InventoryItem).filter(InventoryItem.lot_number == lot).one()
    assert inv_item.company_id == 1
    assert float(inv_item.quantity_on_hand) == 7


# ---------------------------------------------------------------------------
# Inspect path regression guards (should be UNCHANGED by the NOT_REQUIRED work):
# a completed inspection resolves to passed / failed / partial and records a
# REAL inspection_method + inspected_by + inspected_at (the exact opposite of
# the dock-to-stock path above, which must record none of those).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "accepted, rejected, expected_inspection, expected_status, expects_ncr, expected_inventory",
    [
        pytest.param(6, 0, "passed", ReceiptStatus.ACCEPTED.value, False, 6, id="all_accepted_passed"),
        pytest.param(0, 6, "failed", ReceiptStatus.REJECTED.value, True, None, id="all_rejected_failed"),
        pytest.param(4, 2, "partial", ReceiptStatus.ACCEPTED.value, True, 4, id="mixed_partial"),
    ],
)
def test_inspect_result_status_mapping_records_real_inspection(
    client: TestClient,
    db_session: Session,
    accepted: float,
    rejected: float,
    expected_inspection: str,
    expected_status: str,
    expects_ncr: bool,
    expected_inventory,
):
    """/receiving/inspect maps accepted/rejected → passed/failed/partial and, unlike
    dock-to-stock, stamps a real inspection_method / inspected_by / inspected_at."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    quality = make_user(db_session, role=UserRole.QUALITY, company_id=1)
    line = make_po_line(db_session, company_id=1, quantity_ordered=10)
    lot = f"LOT-INSP-{_next():05d}"

    recv = client.post(
        "/api/v1/receiving/receive",
        headers=headers_for(admin),
        json={"po_line_id": line.id, "quantity_received": 6, "lot_number": lot, "requires_inspection": True},
    )
    assert recv.status_code in (status.HTTP_200_OK, status.HTTP_201_CREATED), recv.text
    receipt_id = recv.json()["id"]
    assert recv.json()["inspection_status"] == InspectionStatus.PENDING.value

    payload = {"quantity_accepted": accepted, "quantity_rejected": rejected, "inspection_method": "dimensional"}
    if rejected > 0:
        payload["defect_type"] = "dimensional"
        payload["inspection_notes"] = "Out of tolerance on the bore diameter."

    resp = client.post(f"/api/v1/receiving/inspect/{receipt_id}", headers=headers_for(quality), json=payload)
    assert resp.status_code in (status.HTTP_200_OK, status.HTTP_201_CREATED), resp.text
    result = resp.json()

    receipt_body = result["receipt"]
    assert receipt_body["inspection_status"] == expected_inspection
    assert receipt_body["status"] == expected_status
    # A REAL inspection is recorded on this path (contrast the dock-to-stock nulls).
    assert receipt_body["inspection_method"] == "dimensional"
    assert receipt_body["inspected_by"] == quality.id
    assert receipt_body["inspected_at"] is not None
    assert result["ncr_created"] is expects_ncr

    # Inventory reflects ONLY the accepted quantity (nothing for an all-rejected fail).
    inv_item = db_session.query(InventoryItem).filter(InventoryItem.lot_number == lot).first()
    if expected_inventory is None:
        assert inv_item is None
    else:
        assert inv_item is not None
        assert float(inv_item.quantity_on_hand) == expected_inventory


# ---------------------------------------------------------------------------
# TASK B: inspection queue never ages out; days_back still filters when given
# ---------------------------------------------------------------------------


def _make_old_pending_receipt(db: Session, *, company_id: int, days_old: int) -> POReceipt:
    receipt = make_pending_receipt(db, company_id=company_id)
    receipt.received_at = datetime.utcnow() - timedelta(days=days_old)
    db.commit()
    db.refresh(receipt)
    return receipt


def test_inspection_queue_includes_old_pending_by_default(client: TestClient, db_session: Session):
    """A pending receipt older than the old 30-day window still shows by default."""
    user = make_user(db_session, role=UserRole.QUALITY, company_id=1)
    old_receipt = _make_old_pending_receipt(db_session, company_id=1, days_old=45)

    resp = client.get("/api/v1/receiving/inspection-queue", headers=headers_for(user))

    assert resp.status_code == status.HTTP_200_OK, resp.text
    ids = [item["receipt_id"] for item in resp.json()]
    assert old_receipt.id in ids
    item = next(i for i in resp.json() if i["receipt_id"] == old_receipt.id)
    assert item["days_pending"] >= 44


def test_inspection_queue_days_back_still_filters(client: TestClient, db_session: Session):
    """Passing days_back keeps working as an explicit narrowing filter."""
    user = make_user(db_session, role=UserRole.QUALITY, company_id=1)
    old_receipt = _make_old_pending_receipt(db_session, company_id=1, days_old=45)
    fresh_receipt = make_pending_receipt(db_session, company_id=1)

    resp = client.get(
        "/api/v1/receiving/inspection-queue",
        params={"days_back": 30},
        headers=headers_for(user),
    )

    assert resp.status_code == status.HTTP_200_OK, resp.text
    ids = [item["receipt_id"] for item in resp.json()]
    assert fresh_receipt.id in ids
    assert old_receipt.id not in ids


@pytest.mark.parametrize("bad_value", [-30, 0, 3651, 10**9])
def test_inspection_queue_days_back_is_bounded(client: TestClient, db_session: Session, bad_value: int):
    """days_back is bounded 1..3650: a negative value can't yield a future
    cutoff (silently empty queue) and a huge value can't OverflowError the
    timedelta into a 500 — both 422 instead."""
    user = make_user(db_session, role=UserRole.QUALITY, company_id=1)

    resp = client.get(
        "/api/v1/receiving/inspection-queue",
        params={"days_back": bad_value},
        headers=headers_for(user),
    )

    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, resp.text


def _make_orphan_receipt(db: Session, *, company_id: int, received_by: int) -> POReceipt:
    """Simulate an orphaned receipt (dangling po_line_id).

    SQLite in tests doesn't enforce FKs, letting us create the orphaned-row
    shape that would 500 the old serializers (po_line dereference on None).
    """
    orphan = POReceipt(
        receipt_number=f"RCV-ORPHAN-{_next():05d}",
        po_line_id=99999999,
        quantity_received=3,
        lot_number="LOT-ORPHAN",
        status=ReceiptStatus.PENDING_INSPECTION,
        requires_inspection=True,
        received_by=received_by,
        company_id=company_id,
    )
    db.add(orphan)
    db.commit()
    db.refresh(orphan)
    return orphan


def test_inspection_queue_degrades_orphaned_row_instead_of_500(client: TestClient, db_session: Session):
    """One orphaned receipt (dangling po_line_id) must not 500 the whole queue."""
    user = make_user(db_session, role=UserRole.QUALITY, company_id=1)
    good_receipt = make_pending_receipt(db_session, company_id=1)
    orphan = _make_orphan_receipt(db_session, company_id=1, received_by=user.id)

    resp = client.get("/api/v1/receiving/inspection-queue", headers=headers_for(user))

    assert resp.status_code == status.HTTP_200_OK, resp.text
    items = {item["receipt_id"]: item for item in resp.json()}
    # Nothing is skipped: both rows are present.
    assert good_receipt.id in items
    assert orphan.id in items
    orphan_item = items[orphan.id]
    assert orphan_item["po_number"] is None
    assert orphan_item["po_id"] is None
    assert orphan_item["part_id"] is None
    assert orphan_item["part_number"] is None
    # The healthy row still serializes its full PO/part context.
    assert items[good_receipt.id]["po_number"] is not None
    assert items[good_receipt.id]["part_number"] is not None


def test_inspection_queue_count_matches_stats_pending_count(client: TestClient, db_session: Session):
    """The queue list and the /stats pending_inspection badge agree.

    /stats counts ALL pending receipts with no date cutoff; the default queue
    must return exactly that many rows (this is the invariant the old 30-day
    queue cutoff broke: badge said N, list showed fewer).
    """
    user = make_user(db_session, role=UserRole.QUALITY, company_id=1)
    _make_old_pending_receipt(db_session, company_id=1, days_old=45)
    make_pending_receipt(db_session, company_id=1)
    # A completed (non-pending) receipt must count in neither.
    done = make_pending_receipt(db_session, company_id=1)
    done.status = ReceiptStatus.ACCEPTED
    db_session.commit()

    queue_resp = client.get("/api/v1/receiving/inspection-queue", headers=headers_for(user))
    stats_resp = client.get("/api/v1/receiving/stats", headers=headers_for(user))

    assert queue_resp.status_code == status.HTTP_200_OK, queue_resp.text
    assert stats_resp.status_code == status.HTTP_200_OK, stats_resp.text
    assert stats_resp.json()["pending_inspection"] == 2
    assert len(queue_resp.json()) == 2


def test_inspection_queue_is_tenant_scoped(client: TestClient, db_session: Session):
    """The unbounded default query stays scoped to the active company."""
    user1 = make_user(db_session, role=UserRole.QUALITY, company_id=1)
    other_receipt = make_pending_receipt(db_session, company_id=2)

    resp = client.get("/api/v1/receiving/inspection-queue", headers=headers_for(user1))

    assert resp.status_code == status.HTTP_200_OK, resp.text
    ids = [item["receipt_id"] for item in resp.json()]
    assert other_receipt.id not in ids


def test_history_degrades_orphaned_row_instead_of_500(client: TestClient, db_session: Session):
    """/history serializes an orphaned receipt with None context, not a 500."""
    user = make_user(db_session, role=UserRole.QUALITY, company_id=1)
    good_receipt = make_pending_receipt(db_session, company_id=1)
    orphan = _make_orphan_receipt(db_session, company_id=1, received_by=user.id)

    resp = client.get("/api/v1/receiving/history", headers=headers_for(user))

    assert resp.status_code == status.HTTP_200_OK, resp.text
    items = {item["receipt_id"]: item for item in resp.json()}
    assert good_receipt.id in items
    assert orphan.id in items
    orphan_item = items[orphan.id]
    assert orphan_item["po_number"] is None
    assert orphan_item["vendor_name"] is None
    assert orphan_item["part_number"] is None
    assert orphan_item["part_name"] is None
    # The healthy row still serializes its full PO/part context.
    assert items[good_receipt.id]["po_number"] is not None
    assert items[good_receipt.id]["part_number"] is not None


def test_receipt_detail_degrades_orphaned_receipt_instead_of_500(client: TestClient, db_session: Session):
    """/receipt/{id} on an orphaned receipt returns None context, not a 500."""
    user = make_user(db_session, role=UserRole.QUALITY, company_id=1)
    orphan = _make_orphan_receipt(db_session, company_id=1, received_by=user.id)

    resp = client.get(f"/api/v1/receiving/receipt/{orphan.id}", headers=headers_for(user))

    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["receipt_number"] == orphan.receipt_number
    assert body["po_number"] is None
    assert body["po_id"] is None
    assert body["vendor_name"] is None
    assert body["vendor_code"] is None
    assert body["is_approved_vendor"] is False
    assert body["part_id"] is None
    assert body["part_number"] is None
    assert body["part_name"] is None


def test_inspect_orphaned_receipt_is_400_not_500(client: TestClient, db_session: Session):
    """Inspecting an orphaned receipt fails with a clear 400, never a 500 —
    the accepted quantity has no part/price context to post into inventory."""
    quality = make_user(db_session, role=UserRole.QUALITY, company_id=1)
    orphan = _make_orphan_receipt(db_session, company_id=1, received_by=quality.id)

    resp = client.post(
        f"/api/v1/receiving/inspect/{orphan.id}",
        headers=headers_for(quality),
        json=inspect_payload(quantity_accepted=3),
    )

    assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
    assert "PO line" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# TASK C: empty-string dates from HTML forms coerce to None
# ---------------------------------------------------------------------------


def test_po_create_accepts_blank_date_strings():
    po = POCreate(vendor_id=1, required_date="", expected_date="   ")
    assert po.required_date is None
    assert po.expected_date is None


def test_po_create_blank_required_with_real_expected_ok():
    """The expected>required model_validator still tolerates a None side."""
    po = POCreate(vendor_id=1, required_date="", expected_date="2026-08-01")
    assert po.required_date is None
    assert str(po.expected_date) == "2026-08-01"


def test_po_create_date_order_rule_still_enforced():
    with pytest.raises(ValidationError, match="Expected date must be after required date"):
        POCreate(vendor_id=1, required_date="2026-08-02", expected_date="2026-08-01")


def test_po_line_create_accepts_blank_required_date():
    line = POLineCreate(part_id=1, quantity_ordered=1, unit_price=0, required_date="")
    assert line.required_date is None


def test_po_update_accepts_blank_date_strings():
    """POUpdate parity: PUT payloads coerce blank dates to None like create."""
    upd = POUpdate(version=1, required_date="", expected_date="   ")
    assert upd.required_date is None
    assert upd.expected_date is None


def test_po_update_real_dates_still_parse():
    upd = POUpdate(version=1, required_date="2026-08-01", expected_date="2026-08-15")
    assert str(upd.required_date) == "2026-08-01"
    assert str(upd.expected_date) == "2026-08-15"


def test_po_update_still_rejects_garbage_date_strings():
    with pytest.raises(ValidationError):
        POUpdate(version=1, required_date="not-a-date")
    with pytest.raises(ValidationError):
        POUpdate(version=1, expected_date="not-a-date")


@pytest.mark.parametrize("garbage", ["not-a-date", "2026-13-45", "tomorrow"])
def test_po_create_still_rejects_garbage_date_strings(garbage: str):
    """Only blank/whitespace coerces to None — a malformed date still 422s."""
    with pytest.raises(ValidationError):
        POCreate(vendor_id=1, required_date=garbage)
    with pytest.raises(ValidationError):
        POCreate(vendor_id=1, expected_date=garbage)


def test_po_line_create_still_rejects_garbage_date_strings():
    with pytest.raises(ValidationError):
        POLineCreate(part_id=1, quantity_ordered=1, unit_price=0, required_date="not-a-date")
