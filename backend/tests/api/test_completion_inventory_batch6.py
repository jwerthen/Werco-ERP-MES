"""Behavior locks for Batch-6 completion inventory effects (Rank 9).

Covered findings:
- INV-1 / TRACE-3: WO COMPLETE receives the finished good into inventory and assigns
                   a per-company-unique lot (lot-only, serial left NULL). Asserted for
                   EVERY live completion path (clock_out, shop_floor complete_operation,
                   office complete_operation, complete_work_order) AND the reconcile-on-
                   read path.
- INV-2:           components are backflushed ONLY when part.backflush_components is
                   True (opt-in, default OFF), as negative ISSUE txns carrying the
                   consumed source lot, decrementing source stock; scrap_factor is
                   applied (produced * qty_per * (1 + scrap)).
- INV-3/TRACE-2/TRACE-4: trace_lot AND trace_serial reconstruct the WO genealogy from
                   the FG-receipt + component-ISSUE txns (reference_type='work_order').
- Idempotency:     re-completing an already-COMPLETE WO, and a reconcile-on-read that
                   re-touches an already-COMPLETE WO, do not double-receive or double-
                   issue (the WO RECEIVE / component ISSUE txn is the key). THE headline
                   risk -- the finalizer re-enters on every reconcile read.
- INV-4:           the FG-receipt + backflush stock movements land on the tamper-
                   evident audit_log hash chain.
- MS-4:            MRP on_order reflects RELEASED/IN_PROGRESS WO output and EXCLUDES
                   COMPLETE WOs (no double-count against on_hand).
- Tenant scoping:  FG receipt / backflush for company A never touch company B inventory
                   and a company-B trace can't see company-A WO genealogy.
"""

from datetime import date, datetime, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.bom import BOM, BOMItem
from app.models.company import Company
from app.models.inventory import InventoryItem, InventoryTransaction, TransactionType
from app.models.part import Part
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.services.audit_service import AuditService
from app.services.completion_inventory_service import (
    FINISHED_GOODS_LOCATION,
    _insert_txn_with_savepoint,
    apply_completion_inventory_effects,
)
from app.services.mrp_service import MRPService
from app.services.operational_event_service import OperationalEventService

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2
TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"
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


def make_user(db: Session, *, role: UserRole = UserRole.ADMIN, company_id: int = COMPANY_A) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"b6-{n}@co{company_id}.test",
        employee_id=f"B6-{n:05d}",
        first_name="B6",
        last_name="CA",
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


def make_part(db: Session, *, backflush: bool = False, standard_cost: float = 7.5, company_id: int = COMPANY_A) -> Part:
    _ensure_company(db, company_id)
    n = _next()
    part = Part(
        part_number=f"B6-P-{n}",
        name=f"Part {n}",
        description="batch6 fixture part",
        part_type="manufactured",
        unit_of_measure="each",
        standard_cost=standard_cost,
        backflush_components=backflush,
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def make_work_center(db: Session, *, company_id: int = COMPANY_A) -> WorkCenter:
    _ensure_company(db, company_id)
    n = _next()
    wc = WorkCenter(
        name=f"B6-WC-{n}",
        code=f"B6-WC-{n}",
        work_center_type="welding",
        description="batch6 fixture work center",
        hourly_rate=100,
        is_active=True,
        company_id=company_id,
    )
    db.add(wc)
    db.commit()
    db.refresh(wc)
    return wc


def make_wo(
    db: Session,
    part: Part,
    *,
    quantity_ordered: float = 10,
    quantity_complete: float = 0,
    status_: WorkOrderStatus = WorkOrderStatus.IN_PROGRESS,
    company_id: int = COMPANY_A,
) -> WorkOrder:
    n = _next()
    wo = WorkOrder(
        work_order_number=f"B6-WO-{n:05d}",
        customer_name="Acme",
        part_id=part.id,
        quantity_ordered=quantity_ordered,
        quantity_complete=quantity_complete,
        status=status_,
        priority=5,
        due_date=date.today() + timedelta(days=30),
        company_id=company_id,
    )
    db.add(wo)
    db.flush()
    return wo


def make_op(
    db: Session,
    wo: WorkOrder,
    wc: WorkCenter,
    *,
    sequence: int = 10,
    company_id: int = COMPANY_A,
) -> WorkOrderOperation:
    op = WorkOrderOperation(
        work_order_id=wo.id,
        work_center_id=wc.id,
        sequence=sequence,
        operation_number=f"OP{sequence}",
        name=f"Op {sequence}",
        status=OperationStatus.IN_PROGRESS,
        quantity_complete=0,
        quantity_scrapped=0,
        company_id=company_id,
    )
    db.add(op)
    db.flush()
    return op


def make_inventory(
    db: Session,
    part: Part,
    *,
    qty: float,
    lot: str,
    location: str = "RAW-A",
    company_id: int = COMPANY_A,
) -> InventoryItem:
    item = InventoryItem(
        part_id=part.id,
        location=location,
        warehouse="MAIN",
        quantity_on_hand=qty,
        quantity_available=qty,
        lot_number=lot,
        unit_cost=2.0,
        status="available",
        is_active=True,
        company_id=company_id,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def fg_receipts(db: Session, wo_id: int, *, company_id: int = COMPANY_A) -> list[InventoryTransaction]:
    """All finished-goods RECEIVE txns for a WO (the idempotency key set)."""
    return (
        db.query(InventoryTransaction)
        .filter(
            InventoryTransaction.company_id == company_id,
            InventoryTransaction.reference_type == "work_order",
            InventoryTransaction.reference_id == wo_id,
            InventoryTransaction.transaction_type == TransactionType.RECEIVE,
        )
        .all()
    )


def fg_on_hand(db: Session, part_id: int, *, company_id: int = COMPANY_A) -> float:
    rows = (
        db.query(InventoryItem)
        .filter(
            InventoryItem.company_id == company_id,
            InventoryItem.part_id == part_id,
            InventoryItem.location == FINISHED_GOODS_LOCATION,
        )
        .all()
    )
    return sum(float(r.quantity_on_hand or 0) for r in rows)


def _complete_single_op_wo(client: TestClient, admin: User, op: WorkOrderOperation, qty: float) -> None:
    """Office complete_operation path: POST /work-orders/operations/{id}/complete."""
    resp = client.post(
        f"/api/v1/work-orders/operations/{op.id}/complete?quantity_complete={qty}",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text


# ---------------------------------------------------------------------------
# INV-1 / TRACE-3: FG receipt + lot assignment on WO COMPLETE
# ---------------------------------------------------------------------------


def test_completion_receives_finished_good_and_assigns_lot(client: TestClient, db_session: Session):
    admin = make_user(db_session)
    part = make_part(db_session, standard_cost=12.0)
    wo = make_wo(db_session, part, quantity_ordered=10)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10)
    db_session.commit()

    _complete_single_op_wo(client, admin, op, 10)

    db_session.expire_all()
    wo = db_session.get(WorkOrder, wo.id)
    assert wo.status == WorkOrderStatus.COMPLETE
    # Lot-only: lot_number assigned, serial left untouched.
    assert wo.lot_number, "WO lot_number must be auto-assigned on completion"

    fg = (
        db_session.query(InventoryItem)
        .filter(
            InventoryItem.company_id == COMPANY_A,
            InventoryItem.part_id == part.id,
            InventoryItem.location == FINISHED_GOODS_LOCATION,
        )
        .one()
    )
    assert fg.quantity_on_hand == 10
    assert fg.quantity_available == 10
    assert fg.lot_number == wo.lot_number
    assert fg.serial_number is None
    assert fg.unit_cost == 12.0

    txns = (
        db_session.query(InventoryTransaction)
        .filter(
            InventoryTransaction.company_id == COMPANY_A,
            InventoryTransaction.reference_type == "work_order",
            InventoryTransaction.reference_id == wo.id,
            InventoryTransaction.transaction_type == TransactionType.RECEIVE,
        )
        .all()
    )
    assert len(txns) == 1
    assert txns[0].quantity == 10
    assert txns[0].lot_number == wo.lot_number
    assert txns[0].created_by == admin.id
    assert txns[0].total_cost == 120.0

    # INV-4: stock movement is on the tamper-evident audit chain (CREATE of the txn).
    audit_rows = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.company_id == COMPANY_A,
            AuditLog.resource_type == "inventory",
            AuditLog.resource_id == txns[0].id,
        )
        .all()
    )
    assert audit_rows, "FG receipt must write a tamper-evident audit_log row"


# ---------------------------------------------------------------------------
# Idempotency: re-completing an already-COMPLETE WO does not double-receive
# ---------------------------------------------------------------------------


def test_recompletion_does_not_double_receive(client: TestClient, db_session: Session):
    admin = make_user(db_session)
    part = make_part(db_session)
    wo = make_wo(db_session, part, quantity_ordered=5)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10)
    db_session.commit()

    _complete_single_op_wo(client, admin, op, 5)

    # Privileged manual re-completion of the already-terminal WO must be a no-op for
    # inventory (idempotency guard on the WO RECEIVE txn).
    resp = client.post(
        f"/api/v1/work-orders/{wo.id}/complete?quantity_complete=5",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    receipts = (
        db_session.query(InventoryTransaction)
        .filter(
            InventoryTransaction.company_id == COMPANY_A,
            InventoryTransaction.reference_type == "work_order",
            InventoryTransaction.reference_id == wo.id,
            InventoryTransaction.transaction_type == TransactionType.RECEIVE,
        )
        .all()
    )
    assert len(receipts) == 1, "re-completion must not write a second FG receipt"


# ---------------------------------------------------------------------------
# INV-2: backflush is GATED by part.backflush_components (default OFF)
# ---------------------------------------------------------------------------


def test_backflush_off_by_default_consumes_no_components(client: TestClient, db_session: Session):
    admin = make_user(db_session)
    component = make_part(db_session, standard_cost=2.0)
    make_inventory(db_session, component, qty=100, lot="RAW-LOT-1")
    fg_part = make_part(db_session, backflush=False)  # OFF
    bom = BOM(part_id=fg_part.id, revision="A", is_active=True, company_id=COMPANY_A)
    db_session.add(bom)
    db_session.flush()
    db_session.add(
        BOMItem(
            bom_id=bom.id,
            component_part_id=component.id,
            item_number=10,
            quantity=2,
            item_type="buy",
            line_type="component",
            company_id=COMPANY_A,
        )
    )
    wo = make_wo(db_session, fg_part, quantity_ordered=4)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10)
    db_session.commit()

    _complete_single_op_wo(client, admin, op, 4)

    db_session.expire_all()
    issues = (
        db_session.query(InventoryTransaction)
        .filter(
            InventoryTransaction.company_id == COMPANY_A,
            InventoryTransaction.reference_type == "work_order",
            InventoryTransaction.reference_id == wo.id,
            InventoryTransaction.transaction_type == TransactionType.ISSUE,
        )
        .all()
    )
    assert issues == [], "backflush must NOT consume components when the flag is OFF"


# ---------------------------------------------------------------------------
# INV-2 / INV-3 / TRACE-2: backflush ON consumes components + genealogy via trace_lot
# ---------------------------------------------------------------------------


def test_backflush_on_consumes_components_and_builds_genealogy(client: TestClient, db_session: Session):
    admin = make_user(db_session)
    component = make_part(db_session, standard_cost=2.0)
    src = make_inventory(db_session, component, qty=100, lot="RAW-LOT-7")
    fg_part = make_part(db_session, backflush=True)  # ON
    bom = BOM(part_id=fg_part.id, revision="A", is_active=True, company_id=COMPANY_A)
    db_session.add(bom)
    db_session.flush()
    db_session.add(
        BOMItem(
            bom_id=bom.id,
            component_part_id=component.id,
            item_number=10,
            quantity=2,  # 2 per finished unit
            item_type="buy",
            line_type="component",
            scrap_factor=0.0,
            company_id=COMPANY_A,
        )
    )
    wo = make_wo(db_session, fg_part, quantity_ordered=4)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10)
    db_session.commit()

    _complete_single_op_wo(client, admin, op, 4)

    db_session.expire_all()
    # 2 per unit * 4 units = 8 consumed.
    issues = (
        db_session.query(InventoryTransaction)
        .filter(
            InventoryTransaction.company_id == COMPANY_A,
            InventoryTransaction.reference_type == "work_order",
            InventoryTransaction.reference_id == wo.id,
            InventoryTransaction.transaction_type == TransactionType.ISSUE,
        )
        .all()
    )
    assert len(issues) == 1
    assert issues[0].quantity == -8, "negative ISSUE for 8 consumed component units"
    assert issues[0].part_id == component.id
    assert issues[0].lot_number == "RAW-LOT-7", "ISSUE must carry the consumed source lot"

    src = db_session.get(InventoryItem, src.id)
    assert src.quantity_on_hand == 92, "source stock decremented by 8"

    # INV-3 / TRACE-2: trace_lot reconstructs the WO genealogy from the WO-referencing
    # txns. The FG lot trace shows the WO; the consumed component lot trace shows it too.
    wo = db_session.get(WorkOrder, wo.id)
    resp = client.get(f"/api/v1/traceability/lot/{wo.lot_number}", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert wo.work_order_number in resp.json()["work_orders_used"]

    resp_c = client.get("/api/v1/traceability/lot/RAW-LOT-7", headers=headers_for(admin))
    assert resp_c.status_code == status.HTTP_200_OK, resp_c.text
    assert wo.work_order_number in resp_c.json()["work_orders_used"]

    # Item 2 (as-built second hop): a SINGLE trace of the FG lot must now ALSO present the
    # consumed component genealogy -- the FG-receipt RECEIVE carries the FG lot, while the
    # consumed component lots ride the WO's ISSUE txns, so the FG-lot trace stitches them.
    consumed = resp.json()["consumed_components"]
    assert consumed, "FG-lot trace must enumerate consumed components (as-built genealogy)"
    match = [c for c in consumed if c["component_part_id"] == component.id]
    assert len(match) == 1, "exactly one consumed-component line for the single component lot"
    line = match[0]
    assert line["lot_number"] == "RAW-LOT-7", "consumed-component genealogy carries the source lot"
    assert line["quantity"] == 8, "consumed quantity reported positive (2/unit * 4 units)"
    assert line["work_order_number"] == wo.work_order_number
    assert line["component_part_number"] == component.part_number


# ---------------------------------------------------------------------------
# INV-2 shortage: insufficient stock records the issue but does NOT fail completion
# ---------------------------------------------------------------------------


def test_backflush_shortage_does_not_fail_completion(client: TestClient, db_session: Session):
    admin = make_user(db_session)
    component = make_part(db_session, standard_cost=2.0)
    src = make_inventory(db_session, component, qty=3, lot="RAW-LOT-SHORT")  # only 3 on hand
    fg_part = make_part(db_session, backflush=True)
    bom = BOM(part_id=fg_part.id, revision="A", is_active=True, company_id=COMPANY_A)
    db_session.add(bom)
    db_session.flush()
    db_session.add(
        BOMItem(
            bom_id=bom.id,
            component_part_id=component.id,
            item_number=10,
            quantity=2,
            item_type="buy",
            line_type="component",
            company_id=COMPANY_A,
        )
    )
    wo = make_wo(db_session, fg_part, quantity_ordered=4)  # needs 8, only 3 available
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10)
    db_session.commit()

    # Completion must still succeed (shortage is non-fatal).
    _complete_single_op_wo(client, admin, op, 4)

    db_session.expire_all()
    wo = db_session.get(WorkOrder, wo.id)
    assert wo.status == WorkOrderStatus.COMPLETE
    src = db_session.get(InventoryItem, src.id)
    # The full required 8 is consumed (driving the source lot negative), recording the
    # true demand and genealogy -- matching the system's permissive negative-stock policy.
    assert src.quantity_on_hand == -5

    # The shortage ISSUE is still RECORDED (genealogy + cost captured) and the FG was
    # still received -- the shortage must not abort either leg.
    issues = (
        db_session.query(InventoryTransaction)
        .filter(
            InventoryTransaction.company_id == COMPANY_A,
            InventoryTransaction.reference_type == "work_order",
            InventoryTransaction.reference_id == wo.id,
            InventoryTransaction.transaction_type == TransactionType.ISSUE,
        )
        .all()
    )
    assert sum(t.quantity for t in issues) == -8, "full demand recorded despite shortage"
    assert len(fg_receipts(db_session, wo.id)) == 1, "FG still received under a shortage"


# ---------------------------------------------------------------------------
# Item 3: a backflush shortage is RECORDED tamper-evidently (audit + event), not just logged
# ---------------------------------------------------------------------------


def test_backflush_shortage_writes_audit_and_operational_event(client: TestClient, db_session: Session):
    """A negative on-hand from backflush is a regulated material-trail control gap, so the
    shortage must land on the tamper-evident audit_log hash chain AND emit a warning
    OperationalEvent -- not only a logger.warning."""
    admin = make_user(db_session)
    component = make_part(db_session, standard_cost=2.0)
    make_inventory(db_session, component, qty=3, lot="RAW-LOT-SHORT-EVT")  # only 3 on hand
    fg_part = make_part(db_session, backflush=True)
    bom = BOM(part_id=fg_part.id, revision="A", is_active=True, company_id=COMPANY_A)
    db_session.add(bom)
    db_session.flush()
    db_session.add(
        BOMItem(
            bom_id=bom.id,
            component_part_id=component.id,
            item_number=10,
            quantity=2,
            item_type="buy",
            line_type="component",
            company_id=COMPANY_A,
        )
    )
    wo = make_wo(db_session, fg_part, quantity_ordered=4)  # needs 8, only 3 available
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10)
    db_session.commit()

    _complete_single_op_wo(client, admin, op, 4)

    db_session.expire_all()
    wo = db_session.get(WorkOrder, wo.id)
    assert wo.status == WorkOrderStatus.COMPLETE, "shortage must not fail the completion"

    # Tamper-evident audit row (hash chain) for the shortage, on the component part.
    shortage_audits = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.company_id == COMPANY_A,
            AuditLog.action == "BACKFLUSH_SHORTAGE",
            AuditLog.resource_type == "inventory",
            AuditLog.resource_id == component.id,
        )
        .all()
    )
    assert len(shortage_audits) == 1, "shortage must write exactly one BACKFLUSH_SHORTAGE audit row"
    extra = shortage_audits[0].extra_data or {}
    assert extra.get("shortfall") == 5, "audit extra_data carries the shortfall qty"
    assert extra.get("consumed_lot") == "RAW-LOT-SHORT-EVT", "audit extra_data carries the consumed lot"
    assert extra.get("work_order_id") == wo.id

    # Warning OperationalEvent for AI/realtime consumers.
    events = OperationalEventService(db_session).list_events(
        company_id=COMPANY_A, event_type="backflush_shortage", work_order_id=wo.id
    )
    assert len(events) == 1, "shortage must emit exactly one backflush_shortage OperationalEvent"
    assert events[0].severity == "warning"
    assert events[0].event_payload.get("shortfall") == 5
    assert events[0].event_payload.get("component_part_id") == component.id


# ===========================================================================
# Matrix #1: FG receipt for EVERY live completion path
# ===========================================================================
#
# Each live path drives a WO to COMPLETE; each must create exactly ONE RECEIVE txn
# (reference_type='work_order', reference_id=WO), add quantity_complete to FG on-hand,
# assign work_order.lot_number, set unit_cost=standard_cost, write a tamper-evident
# audit_log row, and company-stamp the item + txn.


def _assert_single_fg_receipt(
    db: Session,
    wo: WorkOrder,
    part: Part,
    *,
    expected_qty: float,
    expected_unit_cost: float,
    created_by: int,
) -> None:
    db.expire_all()
    wo = db.get(WorkOrder, wo.id)
    assert wo.status == WorkOrderStatus.COMPLETE
    assert wo.lot_number, "WO lot_number must be auto-assigned on completion"

    fg = (
        db.query(InventoryItem)
        .filter(
            InventoryItem.company_id == COMPANY_A,
            InventoryItem.part_id == part.id,
            InventoryItem.location == FINISHED_GOODS_LOCATION,
        )
        .one()
    )
    assert fg.quantity_on_hand == expected_qty
    assert fg.lot_number == wo.lot_number
    assert fg.serial_number is None, "lot-only: serial left NULL"
    assert fg.unit_cost == expected_unit_cost
    assert fg.company_id == COMPANY_A, "FG item must be company-stamped"

    receipts = fg_receipts(db, wo.id)
    assert len(receipts) == 1, "exactly ONE RECEIVE txn per completion"
    txn = receipts[0]
    assert txn.quantity == expected_qty
    assert txn.transaction_type == TransactionType.RECEIVE
    assert txn.reference_type == "work_order"
    assert txn.reference_id == wo.id
    assert txn.lot_number == wo.lot_number
    assert txn.unit_cost == expected_unit_cost
    assert txn.created_by == created_by
    assert txn.company_id == COMPANY_A, "FG txn must be company-stamped"

    # INV-4: tamper-evident audit row for the stock movement (CREATE of the txn).
    audit_rows = (
        db.query(AuditLog)
        .filter(
            AuditLog.company_id == COMPANY_A,
            AuditLog.resource_type == "inventory",
            AuditLog.resource_id == txn.id,
        )
        .all()
    )
    assert audit_rows, "FG receipt must write a tamper-evident audit_log row"


def test_fg_receipt_via_clock_out(client: TestClient, db_session: Session):
    """clock_out (shop-floor labor) path drives WO COMPLETE -> exactly one FG receipt."""
    admin = make_user(db_session)
    part = make_part(db_session, standard_cost=9.0)
    wo = make_wo(db_session, part, quantity_ordered=6)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10)
    db_session.commit()

    # Clock IN, then clock OUT booking the full ordered quantity -> WO COMPLETE.
    resp_in = client.post(
        "/api/v1/shop-floor/clock-in",
        json={"work_order_id": wo.id, "operation_id": op.id, "work_center_id": wc.id},
        headers=headers_for(admin),
    )
    assert resp_in.status_code == status.HTTP_200_OK, resp_in.text
    time_entry_id = resp_in.json()["id"]

    resp_out = client.post(
        f"/api/v1/shop-floor/clock-out/{time_entry_id}",
        json={"quantity_produced": 6, "quantity_scrapped": 0},
        headers=headers_for(admin),
    )
    assert resp_out.status_code == status.HTTP_200_OK, resp_out.text

    _assert_single_fg_receipt(db_session, wo, part, expected_qty=6, expected_unit_cost=9.0, created_by=admin.id)


def test_fg_receipt_via_shop_floor_complete_operation(client: TestClient, db_session: Session):
    """shop_floor /operations/{id}/complete drives WO COMPLETE -> exactly one FG receipt."""
    admin = make_user(db_session)
    part = make_part(db_session, standard_cost=4.25)
    wo = make_wo(db_session, part, quantity_ordered=8)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10)
    db_session.commit()

    resp = client.post(
        f"/api/v1/shop-floor/operations/{op.id}/complete",
        json={"quantity_complete": 8},
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    _assert_single_fg_receipt(db_session, wo, part, expected_qty=8, expected_unit_cost=4.25, created_by=admin.id)


def test_fg_receipt_via_office_complete_operation(client: TestClient, db_session: Session):
    """office /work-orders/operations/{id}/complete -> exactly one FG receipt."""
    admin = make_user(db_session)
    part = make_part(db_session, standard_cost=3.0)
    wo = make_wo(db_session, part, quantity_ordered=7)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10)
    db_session.commit()

    _complete_single_op_wo(client, admin, op, 7)

    _assert_single_fg_receipt(db_session, wo, part, expected_qty=7, expected_unit_cost=3.0, created_by=admin.id)


def test_fg_receipt_via_complete_work_order(client: TestClient, db_session: Session):
    """privileged /work-orders/{id}/complete force-completes -> exactly one FG receipt."""
    admin = make_user(db_session)
    part = make_part(db_session, standard_cost=15.0)
    wo = make_wo(db_session, part, quantity_ordered=5)
    wc = make_work_center(db_session)
    # The op must exist so complete_work_order has an open operation to force-complete,
    # but the endpoint addresses the WO (not the op), so the handle is intentionally unused.
    make_op(db_session, wo, wc, sequence=10)
    db_session.commit()

    resp = client.post(
        f"/api/v1/work-orders/{wo.id}/complete?quantity_complete=5",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    _assert_single_fg_receipt(db_session, wo, part, expected_qty=5, expected_unit_cost=15.0, created_by=admin.id)


# ===========================================================================
# Matrix #2: IDEMPOTENCY (THE headline risk)
# ===========================================================================
#
# Re-completing the same WO AND reconcile-on-read re-touching an already-COMPLETE WO
# must NOT create a second RECEIVE txn and must NOT double-add on-hand. The finalizer
# re-enters on every reconcile read, so we hammer the read paths after completion.


def test_idempotent_across_recompletion_and_many_reconcile_reads(client: TestClient, db_session: Session):
    """Multiple completions + many dashboard/list/detail GETs (reconcile re-entry)
    leave EXACTLY one FG receipt and a STABLE on-hand. This is the most important
    test -- a double-receive here is a real, data-corrupting bug."""
    admin = make_user(db_session)
    part = make_part(db_session, standard_cost=11.0)
    wo = make_wo(db_session, part, quantity_ordered=10)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10)
    db_session.commit()

    # First completion (office path).
    _complete_single_op_wo(client, admin, op, 10)
    db_session.expire_all()
    assert len(fg_receipts(db_session, wo.id)) == 1
    assert fg_on_hand(db_session, part.id) == 10

    h = headers_for(admin)

    # Re-complete via the privileged path (already_completed re-entry).
    r1 = client.post(f"/api/v1/work-orders/{wo.id}/complete?quantity_complete=10", headers=h)
    assert r1.status_code == status.HTTP_200_OK, r1.text

    # Re-complete via the office op path again (op already COMPLETE).
    client.post(f"/api/v1/work-orders/operations/{op.id}/complete?quantity_complete=10", headers=h)

    # Re-complete via the shop_floor op path (op already COMPLETE).
    client.post(
        f"/api/v1/shop-floor/operations/{op.id}/complete",
        json={"quantity_complete": 10},
        headers=h,
    )

    # Hammer the reconcile-on-read GET surfaces -- each re-enters the finalizer.
    for _ in range(3):
        assert client.get("/api/v1/work-orders/", headers=h).status_code == status.HTTP_200_OK
        assert client.get(f"/api/v1/work-orders/{wo.id}", headers=h).status_code == status.HTTP_200_OK
        assert client.get("/api/v1/shop-floor/operations", headers=h).status_code == status.HTTP_200_OK
        assert client.get(f"/api/v1/shop-floor/operations/{op.id}", headers=h).status_code == status.HTTP_200_OK

    db_session.expire_all()
    assert len(fg_receipts(db_session, wo.id)) == 1, "re-entry must NOT write a second FG receipt"
    assert fg_on_hand(db_session, part.id) == 10, "on-hand must be STABLE across all re-entry"


def test_backflush_idempotent_across_recompletion(client: TestClient, db_session: Session):
    """Backflush is idempotent per component: re-completing does not write a second ISSUE
    nor double-decrement the source lot."""
    admin = make_user(db_session)
    component = make_part(db_session, standard_cost=2.0)
    src = make_inventory(db_session, component, qty=100, lot="RAW-IDEM")
    fg_part = make_part(db_session, backflush=True)
    bom = BOM(part_id=fg_part.id, revision="A", is_active=True, company_id=COMPANY_A)
    db_session.add(bom)
    db_session.flush()
    db_session.add(
        BOMItem(
            bom_id=bom.id,
            component_part_id=component.id,
            item_number=10,
            quantity=3,
            item_type="buy",
            line_type="component",
            scrap_factor=0.0,
            company_id=COMPANY_A,
        )
    )
    wo = make_wo(db_session, fg_part, quantity_ordered=4)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10)
    db_session.commit()

    _complete_single_op_wo(client, admin, op, 4)  # consumes 3*4 = 12
    db_session.expire_all()
    assert db_session.get(InventoryItem, src.id).quantity_on_hand == 88

    # Re-complete + reconcile reads must not double-issue.
    h = headers_for(admin)
    client.post(f"/api/v1/work-orders/{wo.id}/complete?quantity_complete=4", headers=h)
    client.get(f"/api/v1/work-orders/{wo.id}", headers=h)
    client.get("/api/v1/work-orders/", headers=h)

    db_session.expire_all()
    issues = (
        db_session.query(InventoryTransaction)
        .filter(
            InventoryTransaction.company_id == COMPANY_A,
            InventoryTransaction.reference_type == "work_order",
            InventoryTransaction.reference_id == wo.id,
            InventoryTransaction.transaction_type == TransactionType.ISSUE,
            InventoryTransaction.part_id == component.id,
        )
        .all()
    )
    assert len(issues) == 1, "component must not be backflushed twice"
    assert db_session.get(InventoryItem, src.id).quantity_on_hand == 88, "source stock stable"


# ===========================================================================
# Matrix #3: reconcile-DRIVEN completion (WO completes purely on a GET)
# ===========================================================================


def test_reconcile_driven_completion_receives_fg_once_and_is_read_safe(client: TestClient, db_session: Session):
    """A WO driven to COMPLETE purely by reconcile-on-read (from durable TimeEntry
    evidence, via a GET) receives the FG exactly once, the GET returns 200, and a
    SECOND GET does not double-receive."""
    admin = make_user(db_session)
    part = make_part(db_session, standard_cost=6.0)
    wo = make_wo(db_session, part, quantity_ordered=4)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10)
    # Durable evidence: a closed TimeEntry produced the full ordered quantity, but the
    # operation row was never flipped to COMPLETE (a stale write / crash).
    entry = TimeEntry(
        user_id=admin.id,
        work_order_id=wo.id,
        operation_id=op.id,
        entry_type=TimeEntryType.RUN,
        clock_in=datetime.utcnow() - timedelta(hours=2),
        clock_out=datetime.utcnow() - timedelta(hours=1),
        duration_hours=1.0,
        quantity_produced=4,
        quantity_scrapped=0,
        company_id=COMPANY_A,
    )
    db_session.add(entry)
    db_session.commit()

    h = headers_for(admin)
    # A plain detail GET triggers reconcile-on-read, which drives the WO COMPLETE AND
    # applies the FG receipt best-effort.
    resp = client.get(f"/api/v1/work-orders/{wo.id}", headers=h)
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.rollback()
    db_session.expire_all()
    wo = db_session.get(WorkOrder, wo.id)
    assert wo.status == WorkOrderStatus.COMPLETE, "reconcile drove the WO COMPLETE"
    assert len(fg_receipts(db_session, wo.id)) == 1, "reconcile-driven completion receives FG once"
    assert fg_on_hand(db_session, part.id) == 4

    # A subsequent GET must NOT double-receive (reconcile re-entry on an already-COMPLETE WO).
    assert client.get(f"/api/v1/work-orders/{wo.id}", headers=h).status_code == status.HTTP_200_OK
    assert client.get("/api/v1/work-orders/", headers=h).status_code == status.HTTP_200_OK
    db_session.rollback()
    db_session.expire_all()
    assert len(fg_receipts(db_session, wo.id)) == 1, "second GET must not double-receive"
    assert fg_on_hand(db_session, part.id) == 4


# ===========================================================================
# Matrix #5: backflush scrap_factor math
# ===========================================================================


def test_backflush_applies_scrap_factor(client: TestClient, db_session: Session):
    """Backflush consumes produced * qty_per * (1 + scrap_factor) -- the same math the
    BOM exploder (_collect_bom_components) applies. 5 units * 2/unit * (1 + 0.10) = 11."""
    admin = make_user(db_session)
    component = make_part(db_session, standard_cost=2.0)
    src = make_inventory(db_session, component, qty=100, lot="RAW-SCRAP")
    fg_part = make_part(db_session, backflush=True)
    bom = BOM(part_id=fg_part.id, revision="A", is_active=True, company_id=COMPANY_A)
    db_session.add(bom)
    db_session.flush()
    db_session.add(
        BOMItem(
            bom_id=bom.id,
            component_part_id=component.id,
            item_number=10,
            quantity=2,  # 2 per finished unit
            item_type="buy",
            line_type="component",
            scrap_factor=0.10,  # +10% scrap allowance
            company_id=COMPANY_A,
        )
    )
    wo = make_wo(db_session, fg_part, quantity_ordered=5)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10)
    db_session.commit()

    _complete_single_op_wo(client, admin, op, 5)

    db_session.expire_all()
    issues = (
        db_session.query(InventoryTransaction)
        .filter(
            InventoryTransaction.company_id == COMPANY_A,
            InventoryTransaction.reference_type == "work_order",
            InventoryTransaction.reference_id == wo.id,
            InventoryTransaction.transaction_type == TransactionType.ISSUE,
        )
        .all()
    )
    # 5 * 2 * 1.10 = 11.0 consumed.
    assert sum(t.quantity for t in issues) == pytest.approx(-11.0)
    assert db_session.get(InventoryItem, src.id).quantity_on_hand == pytest.approx(89.0)


# ===========================================================================
# Matrix #6: genealogy via trace_serial + tenant-scoped trace isolation
# ===========================================================================


def test_trace_serial_mirrors_wo_genealogy(client: TestClient, db_session: Session):
    """TRACE-4: trace_serial mirrors trace_lot's WO collection. We assign a serial to
    the FG lot row produced by a completion; trace_serial then reports the producing
    WO via the FG-receipt txn's reference_type='work_order' linkage."""
    admin = make_user(db_session)
    part = make_part(db_session, standard_cost=5.0)
    wo = make_wo(db_session, part, quantity_ordered=3)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10)
    db_session.commit()

    _complete_single_op_wo(client, admin, op, 3)

    db_session.expire_all()
    wo = db_session.get(WorkOrder, wo.id)
    fg = (
        db_session.query(InventoryItem)
        .filter(
            InventoryItem.company_id == COMPANY_A,
            InventoryItem.part_id == part.id,
            InventoryItem.location == FINISHED_GOODS_LOCATION,
        )
        .one()
    )
    # Serialize the produced FG unit + stamp the serial onto the receipt txn so the
    # serial trace can reconstruct the WO (the lot trace already proved the txn linkage).
    serial = f"SN-{wo.work_order_number}"
    fg.serial_number = serial
    receipt = fg_receipts(db_session, wo.id)[0]
    receipt.serial_number = serial
    db_session.commit()

    resp = client.get(f"/api/v1/traceability/serial/{serial}", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert wo.work_order_number in body["work_orders_used"], "trace_serial must report the producing WO"
    assert body["lot_number"] == wo.lot_number


def test_trace_lot_is_tenant_scoped(client: TestClient, db_session: Session):
    """A company-B trace can't see company-A's WO genealogy: tracing the FG lot under a
    company-B token returns no company-A work orders (TRACE-1 / invariant #1)."""
    admin_a = make_user(db_session, company_id=COMPANY_A)
    part = make_part(db_session, standard_cost=5.0, company_id=COMPANY_A)
    wo = make_wo(db_session, part, quantity_ordered=4, company_id=COMPANY_A)
    wc = make_work_center(db_session, company_id=COMPANY_A)
    op = make_op(db_session, wo, wc, sequence=10, company_id=COMPANY_A)
    db_session.commit()

    _complete_single_op_wo(client, admin_a, op, 4)
    db_session.expire_all()
    wo = db_session.get(WorkOrder, wo.id)
    fg_lot = wo.lot_number

    # Company A sees the genealogy.
    resp_a = client.get(f"/api/v1/traceability/lot/{fg_lot}", headers=headers_for(admin_a))
    assert resp_a.status_code == status.HTTP_200_OK, resp_a.text
    assert wo.work_order_number in resp_a.json()["work_orders_used"]

    # Company B, tracing the SAME lot string, sees nothing of company A.
    admin_b = make_user(db_session, company_id=COMPANY_B)
    resp_b = client.get(f"/api/v1/traceability/lot/{fg_lot}", headers=headers_for(admin_b))
    assert resp_b.status_code == status.HTTP_200_OK, resp_b.text
    assert wo.work_order_number not in resp_b.json()["work_orders_used"], "cross-tenant genealogy leak"
    assert resp_b.json()["work_orders_used"] == []


# ===========================================================================
# Matrix #8: MRP on_order reflects in-flight WO output, excludes COMPLETE
# ===========================================================================


def test_mrp_on_order_counts_in_flight_excludes_completed(client: TestClient, db_session: Session):
    """MS-4: on_order = remaining output of RELEASED/IN_PROGRESS make-WOs; a COMPLETE WO
    is EXCLUDED (its output is now on_hand) so it is never double-counted."""
    admin = make_user(db_session)
    part = make_part(db_session, standard_cost=5.0)

    # In-flight supply: RELEASED (10 ordered, 0 done) + IN_PROGRESS (8 ordered, 3 done).
    make_wo(db_session, part, quantity_ordered=10, status_=WorkOrderStatus.RELEASED)
    make_wo(db_session, part, quantity_ordered=8, quantity_complete=3, status_=WorkOrderStatus.IN_PROGRESS)
    db_session.commit()

    mrp = MRPService(db_session, COMPANY_A)
    _on_hand, _allocated, on_order = mrp.get_inventory_summary(part.id)
    # 10 (remaining) + 5 (8 - 3) = 15.
    assert on_order == pytest.approx(15.0)

    # Now complete a WO for the same part via the live path: its output moves to on_hand
    # and it drops OUT of on_order. on_order must NOT change for the still-open WOs, and
    # the completed WO's 4 units must NOT be added to on_order (no double-count).
    wo_done = make_wo(db_session, part, quantity_ordered=4, status_=WorkOrderStatus.IN_PROGRESS)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo_done, wc, sequence=10)
    db_session.commit()
    _complete_single_op_wo(client, admin, op, 4)

    db_session.expire_all()
    mrp = MRPService(db_session, COMPANY_A)
    on_hand_after, _allocated2, on_order_after = mrp.get_inventory_summary(part.id)
    assert on_hand_after == pytest.approx(4.0), "completed WO output is now on_hand"
    assert on_order_after == pytest.approx(15.0), "completed WO excluded from on_order (no double-count)"


# ===========================================================================
# Matrix #9: FG receipt for company A never touches company B inventory
# ===========================================================================


def test_fg_receipt_is_tenant_isolated(client: TestClient, db_session: Session):
    """Completing company-A's WO writes ONLY company-A inventory: no company-B FG item
    or txn is created, and the company-A rows carry company_id=A."""
    admin_a = make_user(db_session, company_id=COMPANY_A)
    part_a = make_part(db_session, standard_cost=8.0, company_id=COMPANY_A)
    wo_a = make_wo(db_session, part_a, quantity_ordered=5, company_id=COMPANY_A)
    wc_a = make_work_center(db_session, company_id=COMPANY_A)
    op_a = make_op(db_session, wo_a, wc_a, sequence=10, company_id=COMPANY_A)

    # Pre-existing company-B state for the SAME part_id value space (different rows).
    _ensure_company(db_session, COMPANY_B)
    part_b = make_part(db_session, standard_cost=99.0, company_id=COMPANY_B)
    db_session.commit()

    _complete_single_op_wo(client, admin_a, op_a, 5)

    db_session.expire_all()
    # Company-A FG item + txn exist and are stamped A.
    fg_a = (
        db_session.query(InventoryItem)
        .filter(InventoryItem.company_id == COMPANY_A, InventoryItem.location == FINISHED_GOODS_LOCATION)
        .all()
    )
    assert len(fg_a) == 1
    assert fg_a[0].part_id == part_a.id
    assert fg_a[0].company_id == COMPANY_A

    # No company-B inventory item or txn was created by company A's completion.
    fg_b_items = db_session.query(InventoryItem).filter(InventoryItem.company_id == COMPANY_B).all()
    assert fg_b_items == [], "company A completion must not create company B inventory"
    fg_b_txns = db_session.query(InventoryTransaction).filter(InventoryTransaction.company_id == COMPANY_B).all()
    assert fg_b_txns == [], "company A completion must not create company B txns"
    # The company-B part is untouched.
    assert not any(i.part_id == part_b.id for i in fg_a)


# ===========================================================================
# Item 1: savepoint no-op on a duplicate inventory insert
# ===========================================================================
#
# Under the new partial unique index a concurrent second RECEIVE/ISSUE insert (the
# double-receive/issue race) raises IntegrityError. Each insert is wrapped in a
# SAVEPOINT so the duplicate is a clean no-op that does NOT double on-hand and does
# NOT abort the outer transaction. On SQLite (test DB) the postgresql_where partial
# index is not enforced, so the IntegrityError won't fire from the DB -- so we (a)
# prove the application-level idempotency guard makes a SECOND service call a no-op,
# and (b) force an IntegrityError directly to prove the savepoint catch is graceful.


def test_second_apply_completion_effects_does_not_double_on_hand_and_does_not_raise(
    client: TestClient, db_session: Session
):
    """A second apply_completion_inventory_effects (the duplicate-insert path the
    savepoint guards) must not double FG on-hand, not double-decrement the component,
    and not raise -- leaving the outer transaction usable."""
    admin = make_user(db_session)
    component = make_part(db_session, standard_cost=2.0)
    src = make_inventory(db_session, component, qty=100, lot="RAW-SP-1")
    fg_part = make_part(db_session, backflush=True, standard_cost=5.0)
    bom = BOM(part_id=fg_part.id, revision="A", is_active=True, company_id=COMPANY_A)
    db_session.add(bom)
    db_session.flush()
    db_session.add(
        BOMItem(
            bom_id=bom.id,
            component_part_id=component.id,
            item_number=10,
            quantity=2,
            item_type="buy",
            line_type="component",
            scrap_factor=0.0,
            company_id=COMPANY_A,
        )
    )
    wo = make_wo(db_session, fg_part, quantity_ordered=4, quantity_complete=4)
    db_session.commit()

    audit = AuditService(db_session, admin)
    # First application: receives 4 FG, issues 8 component.
    apply_completion_inventory_effects(db_session, wo, user_id=admin.id, company_id=COMPANY_A, audit=audit)
    db_session.flush()
    assert fg_on_hand(db_session, fg_part.id) == 4
    assert db_session.get(InventoryItem, src.id).quantity_on_hand == 92

    # Second application (simulating the duplicate-insert re-entry): MUST be a no-op.
    apply_completion_inventory_effects(db_session, wo, user_id=admin.id, company_id=COMPANY_A, audit=audit)
    db_session.commit()  # outer txn must still be usable (no aborted-transaction state)

    db_session.expire_all()
    assert fg_on_hand(db_session, fg_part.id) == 4, "second apply must NOT double FG on-hand"
    assert db_session.get(InventoryItem, src.id).quantity_on_hand == 92, "second apply must NOT re-decrement"
    assert len(fg_receipts(db_session, wo.id)) == 1, "no second FG receipt"
    issues = (
        db_session.query(InventoryTransaction)
        .filter(
            InventoryTransaction.company_id == COMPANY_A,
            InventoryTransaction.reference_type == "work_order",
            InventoryTransaction.reference_id == wo.id,
            InventoryTransaction.transaction_type == TransactionType.ISSUE,
        )
        .all()
    )
    assert len(issues) == 1, "no second component ISSUE"


def test_insert_txn_savepoint_catches_integrity_error_and_keeps_session_usable(db_session: Session):
    """_insert_txn_with_savepoint must roll back ONLY the savepoint on IntegrityError,
    return False (duplicate no-op), and leave the OUTER transaction usable -- so a
    SUBSEQUENT legitimate insert (a DIFFERENT WO) still commits.

    The model's ``uq_wo_inventory_receipt`` unique index already enforces RECEIVE-key
    uniqueness in the test DB (SQLite materializes it as a full unique index), so a
    genuine DUPLICATE of the WO RECEIVE key raises a real unique-violation
    IntegrityError -- exactly the production race the savepoint guards. We assert the
    catch yields a no-op (False) and keeps the OUTER transaction usable."""
    admin = make_user(db_session)
    part = make_part(db_session)

    def _wo_receipt(reference_id: int) -> InventoryTransaction:
        return InventoryTransaction(
            company_id=COMPANY_A,
            part_id=part.id,
            transaction_type=TransactionType.RECEIVE,
            quantity=1,
            reference_type="work_order",
            reference_id=reference_id,
            created_by=admin.id,
        )

    # First insert of WO 555 succeeds.
    assert _insert_txn_with_savepoint(db_session, _wo_receipt(555)) is True

    # A DUPLICATE WO-555 RECEIVE collides with the unique index -> IntegrityError, which
    # the savepoint catches as a clean no-op (False), NOT a raise.
    assert _insert_txn_with_savepoint(db_session, _wo_receipt(555)) is False, "duplicate insert is a no-op (False)"

    # The OUTER transaction must still be usable: a DIFFERENT WO's receipt + commit succeeds.
    good_txn = _wo_receipt(556)
    assert _insert_txn_with_savepoint(db_session, good_txn) is True, "outer txn still usable after caught error"
    db_session.commit()
    assert db_session.get(InventoryTransaction, good_txn.id) is not None

    # Exactly one WO-555 RECEIVE persisted (the duplicate was a no-op).
    wo555 = (
        db_session.query(InventoryTransaction)
        .filter(
            InventoryTransaction.reference_type == "work_order",
            InventoryTransaction.reference_id == 555,
            InventoryTransaction.transaction_type == TransactionType.RECEIVE,
        )
        .all()
    )
    assert len(wo555) == 1
