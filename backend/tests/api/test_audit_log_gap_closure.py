"""Compliance coverage for AUDIT-LOG gaps closed in the QA full-pass.

Locks in invariant #2 (every create/update/delete/status-change is recorded in
the tamper-evident, hash-chained ``audit_log`` via ``AuditService``) for three
state-changing paths that previously emitted only an append-only
``OperationalEvent``:

- INV-4: inventory stock-movement endpoints (/receive, /issue, /transfer,
  /adjust) now write an ``inventory`` CREATE (per InventoryTransaction) plus
  UPDATE rows for the stock levels they mutate.
- BLK-3: the work-order blocker create / resolve flow now writes a
  ``work_order_blocker`` CREATE / STATUS_CHANGE plus the ``work_order_operation``
  STATUS_CHANGE for the hold/resume it triggers.
- EVT-1: marking a shipment shipped closes its work order; that terminal
  RELEASED/COMPLETE -> CLOSED transition now writes a ``work_order``
  STATUS_CHANGE.

Each test also asserts the hash chain stays well-formed (strictly increasing
sequence numbers, non-null integrity hashes, previous_hash links).
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.inventory import InventoryItem, InventoryLocation
from app.models.part import Part
from app.models.shipping import Shipment, ShipmentStatus
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import (
    OperationStatus,
    WorkOrder,
    WorkOrderOperation,
    WorkOrderStatus,
)
from app.models.work_order_blocker import WorkOrderBlocker, WorkOrderBlockerStatus

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"  # tokens are minted directly; not used for login

# Module-level counter so every fixture row gets a globally unique natural key
# (test DBs are per-xdist-worker SQLite files; unique keys avoid cross-test collisions).
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _ensure_company(db: Session, company_id: int) -> Company:
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        company = Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True)
        db.add(company)
        db.commit()
    return company


def make_user(db: Session, *, role: UserRole, company_id: int) -> User:
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


def make_part(db: Session, *, company_id: int) -> Part:
    _ensure_company(db, company_id)
    n = _next()
    part = Part(
        part_number=f"P-{n:05d}",
        name=f"Part {n}",
        description="Test part",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


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


def make_inventory_item(
    db: Session, *, company_id: int, part: Part, location: InventoryLocation, qty: float
) -> InventoryItem:
    n = _next()
    item = InventoryItem(
        part_id=part.id,
        location=location.code,
        warehouse=location.warehouse,
        quantity_on_hand=qty,
        quantity_available=qty,
        lot_number=f"LOT-{n:05d}",
        unit_cost=2.0,
        company_id=company_id,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def make_work_order(db: Session, *, company_id: int, status_: WorkOrderStatus) -> WorkOrder:
    part = make_part(db, company_id=company_id)
    n = _next()
    wo = WorkOrder(
        work_order_number=f"WO-{n:05d}",
        part_id=part.id,
        quantity_ordered=10,
        status=status_,
        company_id=company_id,
    )
    db.add(wo)
    db.commit()
    db.refresh(wo)
    return wo


def make_operation(
    db: Session, *, company_id: int, work_order: WorkOrder, status_: OperationStatus
) -> WorkOrderOperation:
    n = _next()
    wc = WorkCenter(
        code=f"WC-{n:05d}",
        name=f"Work Center {n}",
        work_center_type="production",
        company_id=company_id,
    )
    db.add(wc)
    db.flush()
    op = WorkOrderOperation(
        work_order_id=work_order.id,
        work_center_id=wc.id,
        sequence=10,
        name=f"Op {n}",
        status=status_,
        company_id=company_id,
    )
    db.add(op)
    db.commit()
    db.refresh(op)
    return op


def _assert_hash_chain_intact(logs) -> None:
    seqs = [log.sequence_number for log in logs]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)
    assert all(log.integrity_hash for log in logs)
    for prev, curr in zip(logs, logs[1:]):
        assert curr.previous_hash == prev.integrity_hash


# ---------------------------------------------------------------------------
# INV-4: stock-movement endpoints write tamper-evident audit rows
# ---------------------------------------------------------------------------


def test_receive_into_new_lot_writes_inventory_create_audit(client: TestClient, db_session: Session):
    """A receive into a fresh lot emits a single inventory CREATE (the txn), no UPDATE."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    part = make_part(db_session, company_id=1)
    loc = make_location(db_session, company_id=1)

    resp = client.post(
        "/api/v1/inventory/receive",
        headers=headers_for(admin),
        json={
            "part_id": part.id,
            "quantity": 7,
            "location_code": loc.code,
            "lot_number": "LOT-RCV-NEW",
            "unit_cost": 3.0,
        },
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    logs = db_session.query(AuditLog).order_by(AuditLog.sequence_number).all()
    inv_creates = [log for log in logs if log.resource_type == "inventory" and log.action == "CREATE"]
    inv_updates = [log for log in logs if log.resource_type == "inventory" and log.action == "UPDATE"]
    assert len(inv_creates) == 1
    assert len(inv_updates) == 0
    assert inv_creates[0].company_id == 1
    _assert_hash_chain_intact(logs)


def test_receive_into_existing_lot_writes_create_and_update(client: TestClient, db_session: Session):
    """Receiving into an existing lot emits a CREATE (txn) AND an UPDATE (stock level)."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    part = make_part(db_session, company_id=1)
    loc = make_location(db_session, company_id=1)
    item = make_inventory_item(db_session, company_id=1, part=part, location=loc, qty=5)

    resp = client.post(
        "/api/v1/inventory/receive",
        headers=headers_for(admin),
        json={
            "part_id": part.id,
            "quantity": 4,
            "location_code": loc.code,
            "lot_number": item.lot_number,
        },
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    logs = db_session.query(AuditLog).order_by(AuditLog.sequence_number).all()
    inv_creates = [log for log in logs if log.resource_type == "inventory" and log.action == "CREATE"]
    inv_updates = [log for log in logs if log.resource_type == "inventory" and log.action == "UPDATE"]
    assert len(inv_creates) == 1
    assert len(inv_updates) == 1
    # The UPDATE captures the on-hand change 5 -> 9.
    changes = inv_updates[0].extra_data["changes"]["quantity_on_hand"]
    assert changes["old"] == 5
    assert changes["new"] == 9
    _assert_hash_chain_intact(logs)


def test_issue_writes_inventory_create_and_update(client: TestClient, db_session: Session):
    """Issuing stock emits a CREATE (the ISSUE txn) and an UPDATE (decremented stock)."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    part = make_part(db_session, company_id=1)
    loc = make_location(db_session, company_id=1)
    item = make_inventory_item(db_session, company_id=1, part=part, location=loc, qty=10)

    resp = client.post(
        "/api/v1/inventory/issue",
        headers=headers_for(admin),
        json={"inventory_item_id": item.id, "quantity": 3, "work_order_number": "WO-XYZ"},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    logs = db_session.query(AuditLog).order_by(AuditLog.sequence_number).all()
    inv_creates = [log for log in logs if log.resource_type == "inventory" and log.action == "CREATE"]
    inv_updates = [log for log in logs if log.resource_type == "inventory" and log.action == "UPDATE"]
    assert len(inv_creates) == 1
    assert len(inv_updates) == 1
    changes = inv_updates[0].extra_data["changes"]["quantity_on_hand"]
    assert changes["old"] == 10
    assert changes["new"] == 7
    _assert_hash_chain_intact(logs)


def test_transfer_writes_create_and_source_update(client: TestClient, db_session: Session):
    """Transfer to a fresh destination emits a CREATE (txn) and a source UPDATE."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    part = make_part(db_session, company_id=1)
    src_loc = make_location(db_session, company_id=1)
    dst_loc = make_location(db_session, company_id=1)
    item = make_inventory_item(db_session, company_id=1, part=part, location=src_loc, qty=10)

    resp = client.post(
        "/api/v1/inventory/transfer",
        headers=headers_for(admin),
        json={"inventory_item_id": item.id, "quantity": 4, "to_location_code": dst_loc.code},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    logs = db_session.query(AuditLog).order_by(AuditLog.sequence_number).all()
    inv_creates = [log for log in logs if log.resource_type == "inventory" and log.action == "CREATE"]
    inv_updates = [log for log in logs if log.resource_type == "inventory" and log.action == "UPDATE"]
    assert len(inv_creates) == 1  # the TRANSFER txn
    # Fresh destination -> only the source decrement is an UPDATE (dest is a new row).
    assert len(inv_updates) == 1
    changes = inv_updates[0].extra_data["changes"]["quantity_on_hand"]
    assert changes["old"] == 10
    assert changes["new"] == 6
    _assert_hash_chain_intact(logs)


def test_adjust_writes_inventory_create_and_update(client: TestClient, db_session: Session):
    """Adjusting stock emits a CREATE (the ADJUST txn) and an UPDATE (new level)."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    part = make_part(db_session, company_id=1)
    loc = make_location(db_session, company_id=1)
    item = make_inventory_item(db_session, company_id=1, part=part, location=loc, qty=8)

    resp = client.post(
        "/api/v1/inventory/adjust",
        headers=headers_for(admin),
        json={"inventory_item_id": item.id, "new_quantity": 6, "reason_code": "shrinkage"},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    logs = db_session.query(AuditLog).order_by(AuditLog.sequence_number).all()
    inv_creates = [log for log in logs if log.resource_type == "inventory" and log.action == "CREATE"]
    inv_updates = [log for log in logs if log.resource_type == "inventory" and log.action == "UPDATE"]
    assert len(inv_creates) == 1
    assert len(inv_updates) == 1
    changes = inv_updates[0].extra_data["changes"]["quantity_on_hand"]
    assert changes["old"] == 8
    assert changes["new"] == 6
    _assert_hash_chain_intact(logs)


# ---------------------------------------------------------------------------
# BLK-3: blocker create/resolve write tamper-evident audit rows
# ---------------------------------------------------------------------------


def test_blocker_create_and_resolve_write_audit_rows(client: TestClient, db_session: Session):
    """Reporting a blocker (holding an op) and resolving it (resuming the op) are audited."""
    operator = make_user(db_session, role=UserRole.OPERATOR, company_id=1)
    manager = make_user(db_session, role=UserRole.MANAGER, company_id=1)
    wo = make_work_order(db_session, company_id=1, status_=WorkOrderStatus.RELEASED)
    op = make_operation(db_session, company_id=1, work_order=wo, status_=OperationStatus.IN_PROGRESS)

    # Report blocker -> blocker CREATE + operation STATUS_CHANGE (IN_PROGRESS -> ON_HOLD).
    create_resp = client.post(
        f"/api/v1/work-order-blockers/work-orders/{wo.id}",
        headers=headers_for(operator),
        json={"operation_id": op.id, "category": "quality_hold", "title": "QA hold", "put_operation_on_hold": True},
    )
    assert create_resp.status_code == status.HTTP_200_OK, create_resp.text
    blocker_id = create_resp.json()["id"]

    logs = db_session.query(AuditLog).order_by(AuditLog.sequence_number).all()
    blocker_creates = [log for log in logs if log.resource_type == "work_order_blocker" and log.action == "CREATE"]
    op_changes = [log for log in logs if log.resource_type == "work_order_operation" and log.action == "STATUS_CHANGE"]
    assert len(blocker_creates) == 1
    assert blocker_creates[0].resource_id == blocker_id
    assert len(op_changes) == 1
    assert op_changes[0].old_values == {"status": OperationStatus.IN_PROGRESS.value}
    assert op_changes[0].new_values == {"status": OperationStatus.ON_HOLD.value}

    # Resolve blocker -> blocker STATUS_CHANGE + operation STATUS_CHANGE (ON_HOLD -> IN_PROGRESS).
    resolve_resp = client.post(
        f"/api/v1/work-order-blockers/{blocker_id}/resolve",
        headers=headers_for(manager),
        json={"resolution_note": "cleared"},
    )
    assert resolve_resp.status_code == status.HTTP_200_OK, resolve_resp.text

    logs = db_session.query(AuditLog).order_by(AuditLog.sequence_number).all()
    blocker_status_changes = [
        log for log in logs if log.resource_type == "work_order_blocker" and log.action == "STATUS_CHANGE"
    ]
    assert len(blocker_status_changes) == 1
    assert blocker_status_changes[0].new_values == {"status": WorkOrderBlockerStatus.RESOLVED.value}

    # Two operation status changes total: hold on create, resume on resolve.
    # The op has no actual_start, so resume returns it to READY (not IN_PROGRESS).
    op_changes = [log for log in logs if log.resource_type == "work_order_operation" and log.action == "STATUS_CHANGE"]
    assert len(op_changes) == 2
    assert op_changes[-1].old_values == {"status": OperationStatus.ON_HOLD.value}
    assert op_changes[-1].new_values == {"status": OperationStatus.READY.value}

    # State actually changed, and every audit row is tenant-tagged.
    assert (
        db_session.query(WorkOrderBlocker).filter_by(id=blocker_id).one().status
        == WorkOrderBlockerStatus.RESOLVED.value
    )
    assert all(log.company_id == 1 for log in logs)
    _assert_hash_chain_intact(logs)


# ---------------------------------------------------------------------------
# EVT-1: marking a shipment shipped audits the WO -> CLOSED transition
# ---------------------------------------------------------------------------


def test_mark_shipped_audits_work_order_close(client: TestClient, db_session: Session):
    """mark_shipped closes the WO; the COMPLETE -> CLOSED transition is audited."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    wo = make_work_order(db_session, company_id=1, status_=WorkOrderStatus.COMPLETE)
    n = _next()
    shipment = Shipment(
        shipment_number=f"SHP-{n:05d}",
        work_order_id=wo.id,
        status=ShipmentStatus.PENDING,
        quantity_shipped=10,
        company_id=1,
    )
    db_session.add(shipment)
    db_session.commit()
    db_session.refresh(shipment)

    resp = client.post(
        f"/api/v1/shipping/{shipment.id}/ship",
        headers=headers_for(admin),
        json=None,
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    logs = db_session.query(AuditLog).order_by(AuditLog.sequence_number).all()
    wo_status_changes = [
        log
        for log in logs
        if log.resource_type == "work_order" and log.action == "STATUS_CHANGE" and log.resource_id == wo.id
    ]
    assert len(wo_status_changes) == 1
    assert wo_status_changes[0].old_values == {"status": WorkOrderStatus.COMPLETE.value}
    assert wo_status_changes[0].new_values == {"status": WorkOrderStatus.CLOSED.value}
    assert wo_status_changes[0].company_id == 1

    db_session.refresh(wo)
    assert wo.status == WorkOrderStatus.CLOSED
    _assert_hash_chain_intact(logs)
