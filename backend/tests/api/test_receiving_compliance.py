"""Compliance coverage for the canonical material-receiving endpoints.

These tests lock in the AS9100D / CMMC Level 2 invariants of the receiving
endpoints that now live at /api/v1/receiving (the duplicate
/api/v1/purchasing/receiving* endpoints were removed):

- Tenant isolation: every lookup is scoped by the active company_id, and writes
  are stamped with it.
- RBAC: receive is gated to ADMIN/MANAGER/SUPERVISOR; inspect is gated to
  ADMIN/MANAGER/QUALITY (SUPERVISOR may NOT inspect).
- Audit: receive emits tamper-evident AuditLog entries (receipt CREATE,
  purchase_order STATUS_CHANGE, inventory CREATE/UPDATE).
"""

from datetime import date

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.inventory import InventoryItem, InventoryLocation, InventoryTransaction
from app.models.part import Part
from app.models.purchasing import (
    POReceipt,
    POStatus,
    PurchaseOrder,
    PurchaseOrderLine,
    ReceiptStatus,
    Vendor,
)
from app.models.quality import NonConformanceReport
from app.models.user import User, UserRole

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"  # not used for login; tokens are minted directly

# Module-level counters so every fixture row gets a globally unique natural key.
# Natural keys (vendor code, PO number, part number, user email/employee_id) are
# unique per company, but using a single incrementing counter keeps them unique
# across companies too, which avoids any accidental cross-test collisions.
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _ensure_company(db: Session, company_id: int) -> Company:
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        company = Company(
            id=company_id,
            name=f"Company {company_id}",
            slug=f"company-{company_id}",
            is_active=True,
        )
        db.add(company)
        db.commit()
    return company


def make_user(db: Session, *, role: UserRole, company_id: int) -> User:
    """Create a plain (non-superuser) user of the given role in the given company."""
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"user{n}@co{company_id}.test",
        employee_id=f"EMP-{n:05d}",
        first_name=role.value.title(),
        last_name=f"C{company_id}",
        hashed_password=TEST_PASSWORD_HASH,
        role=role,
        is_active=True,
        is_superuser=False,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def headers_for(user: User) -> dict:
    token = create_access_token(subject=user.id, company_id=user.company_id)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def make_po_line(
    db: Session,
    *,
    company_id: int,
    quantity_ordered: float = 10,
    status_: POStatus = POStatus.SENT,
) -> PurchaseOrderLine:
    """Create Vendor + PO(SENT) + PO line(open) + Part, all stamped company_id.

    Returns the PurchaseOrderLine.
    """
    _ensure_company(db, company_id)
    n = _next()

    vendor = Vendor(
        code=f"V{n:05d}",
        name=f"Vendor {n}",
        is_active=True,
        is_approved=True,
        company_id=company_id,
    )
    db.add(vendor)

    part = Part(
        part_number=f"P-{n:05d}",
        name=f"Part {n}",
        description="Test part",
        part_type="purchased",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.flush()

    po = PurchaseOrder(
        po_number=f"PO-{n:05d}",
        vendor_id=vendor.id,
        status=status_,
        order_date=date.today(),
        company_id=company_id,
    )
    db.add(po)
    db.flush()

    line = PurchaseOrderLine(
        purchase_order_id=po.id,
        line_number=1,
        part_id=part.id,
        quantity_ordered=quantity_ordered,
        quantity_received=0.0,
        unit_price=5.0,
        is_closed=False,
        company_id=company_id,
    )
    db.add(line)
    db.commit()
    db.refresh(line)
    return line


def make_location(db: Session, *, company_id: int) -> InventoryLocation:
    _ensure_company(db, company_id)
    n = _next()
    loc = InventoryLocation(
        code=f"LOC-{n:05d}",
        name=f"Location {n}",
        warehouse="MAIN",
        is_active=True,
        is_receivable=True,
        company_id=company_id,
    )
    db.add(loc)
    db.commit()
    db.refresh(loc)
    return loc


def receive_payload(po_line_id: int, **overrides) -> dict:
    """Build a ReceiptCreate body. lot_number is always non-empty (AS9100D)."""
    body = {
        "po_line_id": po_line_id,
        "quantity_received": 5,
        "lot_number": f"LOT-{_next():05d}",
        "requires_inspection": True,
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# 1. Tenant isolation on receive
# ---------------------------------------------------------------------------


def test_receive_cross_company_po_line_is_404(client: TestClient, db_session: Session):
    """A company-1 admin cannot receive against a company-2 PO line (404)."""
    admin1 = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    other_line = make_po_line(db_session, company_id=2)

    resp = client.post(
        "/api/v1/receiving/receive",
        headers=headers_for(admin1),
        json=receive_payload(other_line.id),
    )

    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert resp.json()["detail"] == "PO line not found"


def test_receive_own_company_po_line_succeeds(client: TestClient, db_session: Session):
    """Positive control: a company-1 admin receiving its own line succeeds."""
    admin1 = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1)

    resp = client.post(
        "/api/v1/receiving/receive",
        headers=headers_for(admin1),
        json=receive_payload(line.id),
    )

    assert resp.status_code in (status.HTTP_200_OK, status.HTTP_201_CREATED), resp.text
    body = resp.json()
    assert body["po_line_id"] == line.id
    assert body["receipt_number"].startswith("RCV-")


# ---------------------------------------------------------------------------
# 2. Audit entries are written for a receipt
# ---------------------------------------------------------------------------


def test_receive_writes_audit_entries(client: TestClient, db_session: Session):
    """A no-inspection receive emits receipt + inventory + PO status audit rows."""
    admin1 = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1, quantity_ordered=10)

    resp = client.post(
        "/api/v1/receiving/receive",
        headers=headers_for(admin1),
        json=receive_payload(
            line.id,
            quantity_received=10,  # fully receives -> PO status changes to RECEIVED
            requires_inspection=False,  # auto-accept -> inventory path runs
            lot_number="LOT-AUDIT-1",
        ),
    )
    assert resp.status_code in (status.HTTP_200_OK, status.HTTP_201_CREATED), resp.text
    receipt_id = resp.json()["id"]

    logs = db_session.query(AuditLog).order_by(AuditLog.sequence_number).all()

    # CREATE for the receipt
    receipt_creates = [log for log in logs if log.resource_type == "receipt" and log.action == "CREATE"]
    assert len(receipt_creates) == 1
    assert receipt_creates[0].resource_id == receipt_id

    # CREATE or UPDATE for inventory (fresh lot -> CREATE)
    inventory_logs = [log for log in logs if log.resource_type == "inventory" and log.action in ("CREATE", "UPDATE")]
    assert len(inventory_logs) >= 1

    # STATUS_CHANGE for the purchase_order (SENT -> RECEIVED)
    po_status_changes = [log for log in logs if log.resource_type == "purchase_order" and log.action == "STATUS_CHANGE"]
    assert len(po_status_changes) == 1
    assert po_status_changes[0].old_values == {"status": "sent"}
    assert po_status_changes[0].new_values == {"status": "received"}

    # Hash chain sanity: strictly increasing sequence numbers, no null integrity hash,
    # and the chain links via previous_hash.
    seqs = [log.sequence_number for log in logs]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)
    assert all(log.integrity_hash for log in logs)
    for prev, curr in zip(logs, logs[1:]):
        assert curr.previous_hash == prev.integrity_hash


def test_inspect_writes_audit_entries(client: TestClient, db_session: Session):
    """A partial inspection emits receipt STATUS_CHANGE + inventory + NCR audit rows.

    This locks in the audit coverage on the inspection path, mirroring
    test_receive_writes_audit_entries for the receive path. A single PARTIAL
    inspection (some accepted, some rejected) exercises all three audit writes:
    the inventory write for the accepted qty, the auto-NCR CREATE for the
    rejected qty, and the receipt STATUS_CHANGE (PENDING_INSPECTION -> ACCEPTED).
    """
    admin1 = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    quality1 = make_user(db_session, role=UserRole.QUALITY, company_id=1)
    line = make_po_line(db_session, company_id=1, quantity_ordered=10)

    # 1+2. Receive against the line requiring inspection -> PENDING_INSPECTION receipt.
    recv = client.post(
        "/api/v1/receiving/receive",
        headers=headers_for(admin1),
        json=receive_payload(
            line.id,
            quantity_received=6,
            requires_inspection=True,
            lot_number="LOT-INSPECT-1",
        ),
    )
    assert recv.status_code in (status.HTTP_200_OK, status.HTTP_201_CREATED), recv.text
    receipt_id = recv.json()["id"]
    assert recv.json()["status"] == ReceiptStatus.PENDING_INSPECTION.value

    # 3. Inspect with a PARTIAL result: 4 accepted, 2 rejected (sum 6 <= 6 received).
    # defect_type + inspection_notes are required because quantity_rejected > 0.
    resp = client.post(
        f"/api/v1/receiving/inspect/{receipt_id}",
        headers=headers_for(quality1),
        json={
            "quantity_accepted": 4,
            "quantity_rejected": 2,
            "inspection_method": "dimensional",
            "defect_type": "dimensional",
            "inspection_notes": "2 parts out of tolerance on the bore diameter.",
        },
    )
    assert resp.status_code in (status.HTTP_200_OK, status.HTTP_201_CREATED), resp.text
    result = resp.json()
    assert result["inventory_created"] is True
    assert result["ncr_created"] is True
    ncr_id = result["ncr_id"]

    logs = db_session.query(AuditLog).order_by(AuditLog.sequence_number).all()

    # 4a. STATUS_CHANGE for THIS receipt (PENDING_INSPECTION -> ACCEPTED on partial).
    # Scope by resource_id so we don't pick up the receive step's receipt CREATE.
    receipt_status_changes = [
        log
        for log in logs
        if log.resource_type == "receipt" and log.action == "STATUS_CHANGE" and log.resource_id == receipt_id
    ]
    assert len(receipt_status_changes) == 1
    assert receipt_status_changes[0].old_values == {"status": ReceiptStatus.PENDING_INSPECTION.value}
    assert receipt_status_changes[0].new_values == {"status": ReceiptStatus.ACCEPTED.value}

    # 4b. At least one inventory row (CREATE for a fresh lot, or UPDATE) from the accepted qty.
    inventory_logs = [log for log in logs if log.resource_type == "inventory" and log.action in ("CREATE", "UPDATE")]
    assert len(inventory_logs) >= 1

    # 4c. NCR CREATE from the rejected qty, for the NCR returned by the endpoint.
    ncr_creates = [log for log in logs if log.resource_type == "ncr" and log.action == "CREATE"]
    assert len(ncr_creates) == 1
    assert ncr_creates[0].resource_id == ncr_id

    # The created NCR row is stamped with the active company.
    ncr = db_session.query(NonConformanceReport).filter(NonConformanceReport.id == ncr_id).one()
    assert ncr.company_id == 1

    # Hash chain sanity: strictly increasing sequence numbers, no null integrity hash,
    # and the chain links via previous_hash.
    seqs = [log.sequence_number for log in logs]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)
    assert all(log.integrity_hash for log in logs)
    for prev, curr in zip(logs, logs[1:]):
        assert curr.previous_hash == prev.integrity_hash


# ---------------------------------------------------------------------------
# 3. RBAC on receive
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", [UserRole.OPERATOR, UserRole.QUALITY, UserRole.VIEWER])
def test_receive_forbidden_for_unauthorized_roles(client: TestClient, db_session: Session, role: UserRole):
    """OPERATOR, QUALITY, and VIEWER are not allowed to receive (403)."""
    user = make_user(db_session, role=role, company_id=1)
    line = make_po_line(db_session, company_id=1)

    resp = client.post(
        "/api/v1/receiving/receive",
        headers=headers_for(user),
        json=receive_payload(line.id),
    )

    assert resp.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])
def test_receive_allowed_for_authorized_roles(client: TestClient, db_session: Session, role: UserRole):
    """ADMIN, MANAGER, SUPERVISOR may receive (not 403). Fresh line per case."""
    user = make_user(db_session, role=role, company_id=1)
    line = make_po_line(db_session, company_id=1)

    resp = client.post(
        "/api/v1/receiving/receive",
        headers=headers_for(user),
        json=receive_payload(line.id),
    )

    assert resp.status_code != status.HTTP_403_FORBIDDEN, resp.text
    assert resp.status_code in (status.HTTP_200_OK, status.HTTP_201_CREATED), resp.text


# ---------------------------------------------------------------------------
# 4. company_id is stamped on every write
# ---------------------------------------------------------------------------


def test_receive_stamps_company_id_on_writes(client: TestClient, db_session: Session):
    """POReceipt, InventoryItem, and InventoryTransaction all get company_id=1."""
    admin1 = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1)

    resp = client.post(
        "/api/v1/receiving/receive",
        headers=headers_for(admin1),
        json=receive_payload(line.id, requires_inspection=False, lot_number="LOT-STAMP-1"),
    )
    assert resp.status_code in (status.HTTP_200_OK, status.HTTP_201_CREATED), resp.text
    receipt_id = resp.json()["id"]

    receipt = db_session.query(POReceipt).filter(POReceipt.id == receipt_id).one()
    assert receipt.company_id == 1

    inv_item = db_session.query(InventoryItem).filter(InventoryItem.lot_number == "LOT-STAMP-1").one()
    assert inv_item.company_id == 1

    txn = db_session.query(InventoryTransaction).filter(InventoryTransaction.inventory_item_id == inv_item.id).one()
    assert txn.company_id == 1


# ---------------------------------------------------------------------------
# 5. Location lookup is tenant-scoped
# ---------------------------------------------------------------------------


def test_receive_cross_company_location_is_404(client: TestClient, db_session: Session):
    """A company-1 receive passing a company-2 location_id gets 404."""
    admin1 = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    line = make_po_line(db_session, company_id=1)
    other_location = make_location(db_session, company_id=2)

    resp = client.post(
        "/api/v1/receiving/receive",
        headers=headers_for(admin1),
        json=receive_payload(line.id, location_id=other_location.id),
    )

    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert resp.json()["detail"] == "Location not found"


# ---------------------------------------------------------------------------
# Inspect helpers + RBAC / tenant isolation
# ---------------------------------------------------------------------------


def make_pending_receipt(db: Session, *, company_id: int, quantity: float = 5) -> POReceipt:
    """Create a receipt in PENDING_INSPECTION status for the given company."""
    line = make_po_line(db, company_id=company_id, quantity_ordered=10)
    receiver = make_user(db, role=UserRole.ADMIN, company_id=company_id)
    n = _next()
    receipt = POReceipt(
        receipt_number=f"RCV-FIX-{n:05d}",
        po_line_id=line.id,
        quantity_received=quantity,
        lot_number=f"LOT-{n:05d}",
        status=ReceiptStatus.PENDING_INSPECTION,
        requires_inspection=True,
        received_by=receiver.id,
        company_id=company_id,
    )
    db.add(receipt)
    db.commit()
    db.refresh(receipt)
    return receipt


def inspect_payload(quantity_accepted: float = 5) -> dict:
    return {
        "quantity_accepted": quantity_accepted,
        "quantity_rejected": 0,
        "inspection_method": "visual",
    }


# ---------------------------------------------------------------------------
# 6. Inspect RBAC narrowed (SUPERVISOR excluded, QUALITY included)
# ---------------------------------------------------------------------------


def test_inspect_forbidden_for_supervisor(client: TestClient, db_session: Session):
    """Regression guard: SUPERVISOR may receive but may NOT inspect (403)."""
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR, company_id=1)
    receipt = make_pending_receipt(db_session, company_id=1)

    resp = client.post(
        f"/api/v1/receiving/inspect/{receipt.id}",
        headers=headers_for(supervisor),
        json=inspect_payload(quantity_accepted=receipt.quantity_received),
    )

    assert resp.status_code == status.HTTP_403_FORBIDDEN


def test_inspect_allowed_for_quality(client: TestClient, db_session: Session):
    """A QUALITY user is allowed to inspect (not 403)."""
    quality = make_user(db_session, role=UserRole.QUALITY, company_id=1)
    receipt = make_pending_receipt(db_session, company_id=1)

    resp = client.post(
        f"/api/v1/receiving/inspect/{receipt.id}",
        headers=headers_for(quality),
        json=inspect_payload(quantity_accepted=receipt.quantity_received),
    )

    assert resp.status_code != status.HTTP_403_FORBIDDEN, resp.text
    assert resp.status_code in (status.HTTP_200_OK, status.HTTP_201_CREATED), resp.text


# ---------------------------------------------------------------------------
# 7. Inspect tenant isolation
# ---------------------------------------------------------------------------


def test_inspect_cross_company_receipt_is_404(client: TestClient, db_session: Session):
    """A company-1 QUALITY user inspecting a company-2 receipt gets 404."""
    quality1 = make_user(db_session, role=UserRole.QUALITY, company_id=1)
    other_receipt = make_pending_receipt(db_session, company_id=2)

    resp = client.post(
        f"/api/v1/receiving/inspect/{other_receipt.id}",
        headers=headers_for(quality1),
        json=inspect_payload(quantity_accepted=other_receipt.quantity_received),
    )

    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert resp.json()["detail"] == "Receipt not found"
