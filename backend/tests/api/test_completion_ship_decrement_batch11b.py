"""Behavior locks for the Batch-11B FG-decrement-on-ship + over-ship guard (G2).

This is the outbound mirror of the Batch-6 FG receipt: Batch-6's
``receive_finished_goods_for_work_order`` RECEIVEs the produced quantity into the
FINISHED-GOODS lot on completion; Batch-11B's
``decrement_finished_goods_for_shipment`` writes the offsetting negative SHIP txn and
decrements that lot's on-hand/available when the finished goods leave on a shipment.
``record_over_ship_if_needed`` is the warn-and-record guard against shipping more than
was produced. Both join the ship's unit of work and NEITHER fails the ship.

Covered contracts (all via the live ``POST /shipping/{id}/ship`` path):
- Happy path: complete a WO (Batch-6 lands the FG lot), create + mark_shipped a
  shipment -> exactly one ``TransactionType.SHIP`` txn (quantity = -quantity_shipped,
  reference_type='shipment', reference_id=shipment.id) AND the FG
  ``InventoryItem.quantity_on_hand`` / ``quantity_available`` drop by quantity_shipped.
- Idempotency: a re-entrant decrement (the double-ship race) does NOT double-decrement
  -- still exactly one SHIP txn, on-hand decremented once.
- FG-not-found: shipping a WO whose FG lot row doesn't exist still SUCCEEDS
  (warn-and-record) with NO SHIP txn / NO decrement, plus a ``SHIP_FG_LOT_MISSING``
  audit row + a ``ship_fg_lot_missing`` warning OperationalEvent.
- Over-ship: cumulative non-cancelled shipped quantity exceeding
  ``WorkOrder.quantity_complete`` still SUCCEEDS (no 400) and writes an ``OVER_SHIP``
  audit row + an ``over_ship`` warning OperationalEvent.
"""

from datetime import date, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.inventory import InventoryItem, InventoryTransaction, TransactionType
from app.models.part import Part
from app.models.shipping import Shipment, ShipmentStatus
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.services.audit_service import AuditService
from app.services.completion_inventory_service import (
    FINISHED_GOODS_LOCATION,
    OVER_SHIP_AUDIT_ACTION,
    OVER_SHIP_EVENT_TYPE,
    SHIP_FG_MISSING_AUDIT_ACTION,
    SHIP_FG_MISSING_EVENT_TYPE,
    decrement_finished_goods_for_shipment,
)
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
        email=f"b11b-g2-{n}@co{company_id}.test",
        employee_id=f"B11BG2-{n:05d}",
        first_name="B11B",
        last_name="G2",
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


def make_part(db: Session, *, standard_cost: float = 7.5, company_id: int = COMPANY_A) -> Part:
    _ensure_company(db, company_id)
    n = _next()
    part = Part(
        part_number=f"B11BG2-P-{n}",
        name=f"Part {n}",
        description="batch11b G2 fixture part",
        part_type="manufactured",
        unit_of_measure="each",
        standard_cost=standard_cost,
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
        name=f"B11BG2-WC-{n}",
        code=f"B11BG2-WC-{n}",
        work_center_type="welding",
        description="batch11b G2 fixture work center",
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
    status_: WorkOrderStatus = WorkOrderStatus.IN_PROGRESS,
    company_id: int = COMPANY_A,
) -> WorkOrder:
    n = _next()
    wo = WorkOrder(
        work_order_number=f"B11BG2-WO-{n:05d}",
        customer_name="Acme",
        part_id=part.id,
        quantity_ordered=quantity_ordered,
        quantity_complete=0,
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


def complete_wo_via_op(client: TestClient, admin: User, op: WorkOrderOperation, qty: float) -> None:
    """Office complete_operation path: drives the WO to COMPLETE + lands the Batch-6 FG lot."""
    resp = client.post(
        f"/api/v1/work-orders/operations/{op.id}/complete?quantity_complete={qty}",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text


def create_shipment(
    client: TestClient,
    user: User,
    wo: WorkOrder,
    *,
    quantity_shipped: float,
) -> dict:
    resp = client.post(
        "/api/v1/shipping/",
        headers=headers_for(user),
        json={"work_order_id": wo.id, "quantity_shipped": quantity_shipped},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    return resp.json()


def mark_shipped(client: TestClient, user: User, shipment_id: int) -> dict:
    resp = client.post(f"/api/v1/shipping/{shipment_id}/ship", headers=headers_for(user))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    return resp.json()


def ship_txns(db: Session, shipment_id: int, *, company_id: int = COMPANY_A) -> list[InventoryTransaction]:
    """All SHIP txns keyed to a shipment (the idempotency key set)."""
    return (
        db.query(InventoryTransaction)
        .filter(
            InventoryTransaction.company_id == company_id,
            InventoryTransaction.reference_type == "shipment",
            InventoryTransaction.reference_id == shipment_id,
            InventoryTransaction.transaction_type == TransactionType.SHIP,
        )
        .all()
    )


def fg_item(db: Session, part_id: int, lot_number: str, *, company_id: int = COMPANY_A) -> InventoryItem:
    return (
        db.query(InventoryItem)
        .filter(
            InventoryItem.company_id == company_id,
            InventoryItem.part_id == part_id,
            InventoryItem.location == FINISHED_GOODS_LOCATION,
            InventoryItem.lot_number == lot_number,
        )
        .one()
    )


# ---------------------------------------------------------------------------
# Happy path: ship decrements the FG lot via a single SHIP txn
# ---------------------------------------------------------------------------


def test_ship_writes_ship_txn_and_decrements_fg_on_hand(client: TestClient, db_session: Session):
    """Complete a WO (Batch-6 lands an FG lot at FINISHED-GOODS), create + ship a
    shipment for the full quantity -> exactly one SHIP txn (qty = -quantity_shipped,
    reference_type='shipment', reference_id=shipment.id) AND the FG lot's on-hand AND
    available both drop by quantity_shipped."""
    admin = make_user(db_session)
    part = make_part(db_session, standard_cost=12.0)
    wo = make_wo(db_session, part, quantity_ordered=10)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10)
    db_session.commit()

    complete_wo_via_op(client, admin, op, 10)
    db_session.expire_all()
    wo = db_session.get(WorkOrder, wo.id)
    fg_before = fg_item(db_session, part.id, wo.lot_number)
    assert fg_before.quantity_on_hand == 10
    assert fg_before.quantity_available == 10

    shipment = create_shipment(client, admin, wo, quantity_shipped=4)
    mark_shipped(client, admin, shipment["id"])

    db_session.expire_all()
    txns = ship_txns(db_session, shipment["id"])
    assert len(txns) == 1, "exactly one SHIP txn for the shipment"
    txn = txns[0]
    assert txn.quantity == -4, "SHIP txn quantity is negative quantity_shipped"
    assert txn.transaction_type == TransactionType.SHIP
    assert txn.reference_type == "shipment"
    assert txn.reference_id == shipment["id"]
    assert txn.from_location == FINISHED_GOODS_LOCATION
    assert txn.lot_number == wo.lot_number
    assert txn.part_id == part.id
    assert txn.company_id == COMPANY_A
    assert txn.unit_cost == 12.0
    assert txn.total_cost == pytest.approx(48.0)  # 4 x $12

    fg_after = fg_item(db_session, part.id, wo.lot_number)
    assert fg_after.quantity_on_hand == 6, "on-hand decremented by quantity_shipped (10 - 4)"
    assert fg_after.quantity_available == 6, "available recomputed (on_hand - allocated)"

    # Outbound stock movement lands on the tamper-evident audit hash chain.
    audit_rows = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.company_id == COMPANY_A,
            AuditLog.resource_type == "inventory",
            AuditLog.resource_id == txn.id,
        )
        .all()
    )
    assert audit_rows, "SHIP movement must write a tamper-evident audit_log row"


# ---------------------------------------------------------------------------
# Idempotency: a re-entrant decrement does not double-decrement
# ---------------------------------------------------------------------------


def test_reentrant_decrement_does_not_double_decrement(client: TestClient, db_session: Session):
    """A re-entrant ``decrement_finished_goods_for_shipment`` (the double-ship race the
    SHIP-txn idempotency key + row lock guard) does NOT write a second SHIP txn and does
    NOT decrement on-hand twice.

    The live ``/ship`` endpoint short-circuits a re-submitted ship at its own
    already-SHIPPED guard (so a second HTTP call never reaches the decrement). To prove
    the decrement is itself idempotent (the actual G2 contract -- concurrent callers that
    both pass the status guard), we invoke the service a second time directly on the same
    shipment after a real ship, exactly as a racing second worker would."""
    admin = make_user(db_session)
    part = make_part(db_session, standard_cost=5.0)
    wo = make_wo(db_session, part, quantity_ordered=8)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10)
    db_session.commit()

    complete_wo_via_op(client, admin, op, 8)
    db_session.expire_all()
    wo = db_session.get(WorkOrder, wo.id)

    shipment_json = create_shipment(client, admin, wo, quantity_shipped=3)
    mark_shipped(client, admin, shipment_json["id"])

    db_session.expire_all()
    assert len(ship_txns(db_session, shipment_json["id"])) == 1
    assert fg_item(db_session, part.id, wo.lot_number).quantity_on_hand == 5  # 8 - 3

    # Re-entrant call (a racing second worker): MUST be a clean no-op.
    shipment = db_session.get(Shipment, shipment_json["id"])
    audit = AuditService(db_session, admin)
    result = decrement_finished_goods_for_shipment(
        db_session,
        work_order=wo,
        shipment=shipment,
        company_id=COMPANY_A,
        user_id=admin.id,
        audit=audit,
    )
    db_session.commit()
    assert result is None, "re-entrant decrement returns None (no-op)"

    db_session.expire_all()
    assert len(ship_txns(db_session, shipment_json["id"])) == 1, "no second SHIP txn"
    assert fg_item(db_session, part.id, wo.lot_number).quantity_on_hand == 5, "on-hand decremented ONCE"

    # And the live endpoint itself is also idempotent (already-SHIPPED -> no-op response).
    second = client.post(f"/api/v1/shipping/{shipment_json['id']}/ship", headers=headers_for(admin))
    assert second.status_code == status.HTTP_200_OK, second.text
    assert second.json().get("already_shipped") is True
    db_session.expire_all()
    assert len(ship_txns(db_session, shipment_json["id"])) == 1, "endpoint re-ship writes no second SHIP txn"
    assert fg_item(db_session, part.id, wo.lot_number).quantity_on_hand == 5


# ---------------------------------------------------------------------------
# FG-not-found: ship succeeds, warn-and-record, no decrement
# ---------------------------------------------------------------------------


def test_ship_with_missing_fg_lot_succeeds_and_records_discrepancy(client: TestClient, db_session: Session):
    """A WO whose FG lot row does NOT exist (completion receipt skipped / lot changed):
    the ship still SUCCEEDS (warn-and-record), NO SHIP txn is written, NO on-hand is
    decremented, and a ``SHIP_FG_LOT_MISSING`` audit row + a ``ship_fg_lot_missing``
    warning OperationalEvent are written.

    We force the missing-lot condition by giving the WO a lot_number but never creating
    the matching FINISHED-GOODS InventoryItem (i.e. simulate a completion that skipped the
    FG receipt). The WO is hand-built COMPLETE so we control exactly what inventory exists.
    """
    admin = make_user(db_session)
    part = make_part(db_session, standard_cost=9.0)
    wo = make_wo(db_session, part, quantity_ordered=5, status_=WorkOrderStatus.COMPLETE)
    wo.quantity_complete = 5
    wo.lot_number = "LOT-NO-RECEIPT"  # a lot the FG receipt never created a row for
    db_session.commit()

    # Sanity: there is genuinely no FG inventory row for this lot.
    assert (
        db_session.query(InventoryItem)
        .filter(
            InventoryItem.company_id == COMPANY_A,
            InventoryItem.part_id == part.id,
            InventoryItem.location == FINISHED_GOODS_LOCATION,
        )
        .first()
        is None
    )

    shipment = create_shipment(client, admin, wo, quantity_shipped=5)
    # The ship endpoint must NOT 4xx -- warn-and-record posture.
    resp = client.post(f"/api/v1/shipping/{shipment['id']}/ship", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    # No SHIP txn / no decrement for a missing FG lot.
    assert ship_txns(db_session, shipment["id"]) == [], "no SHIP txn when the FG lot is missing"
    # Shipment + WO close still proceeded.
    shp = db_session.get(Shipment, shipment["id"])
    assert shp.status == ShipmentStatus.SHIPPED
    assert db_session.get(WorkOrder, wo.id).status == WorkOrderStatus.CLOSED

    # Discrepancy audit row on the tamper-evident chain.
    miss_audits = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.company_id == COMPANY_A,
            AuditLog.action == SHIP_FG_MISSING_AUDIT_ACTION,
            AuditLog.resource_type == "shipment",
            AuditLog.resource_id == shipment["id"],
        )
        .all()
    )
    assert len(miss_audits) == 1, "exactly one SHIP_FG_LOT_MISSING audit row"
    extra = miss_audits[0].extra_data or {}
    assert extra.get("lot_number") == "LOT-NO-RECEIPT"
    assert extra.get("work_order_id") == wo.id
    assert extra.get("quantity_shipped") == 5

    # Warning OperationalEvent for AI/realtime consumers.
    events = OperationalEventService(db_session).list_events(
        company_id=COMPANY_A, event_type=SHIP_FG_MISSING_EVENT_TYPE, work_order_id=wo.id
    )
    assert len(events) == 1, "exactly one ship_fg_lot_missing OperationalEvent"
    assert events[0].severity == "warning"


# ---------------------------------------------------------------------------
# Over-ship: ship succeeds, warn-and-record over-ship
# ---------------------------------------------------------------------------


def test_over_ship_succeeds_and_records_over_ship(client: TestClient, db_session: Session):
    """A single shipment whose quantity exceeds ``WorkOrder.quantity_complete`` still
    SUCCEEDS (no 400) and writes an ``OVER_SHIP`` audit row + an ``over_ship`` warning
    OperationalEvent (warn-and-record posture).

    Produced (quantity_complete) = 10; a single shipment of 12 pushes cumulative shipped
    to 12 > 10 -> the over-ship is recorded but the ship/close proceeds.

    NOTE: this exercises the SINGLE-shipment over-ship path. The MULTI-shipment cumulative
    over-ship path (a second distinct shipment on an already-CLOSED WO pushing the running
    total past produced) is now reachable through the endpoint and is covered by
    ``test_second_distinct_shipment_decrements_and_does_not_reclose_wo``.
    """
    admin = make_user(db_session)
    part = make_part(db_session, standard_cost=6.0)
    wo = make_wo(db_session, part, quantity_ordered=10)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10)
    db_session.commit()

    complete_wo_via_op(client, admin, op, 10)  # quantity_complete = 10, FG lot of 10 on hand
    db_session.expire_all()
    wo = db_session.get(WorkOrder, wo.id)

    # A single shipment of 12 over the produced 10 -> over-ship recorded, ship still OK.
    ship = create_shipment(client, admin, wo, quantity_shipped=12)
    resp = client.post(f"/api/v1/shipping/{ship['id']}/ship", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    over_audits = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.company_id == COMPANY_A,
            AuditLog.action == OVER_SHIP_AUDIT_ACTION,
            AuditLog.resource_type == "shipment",
            AuditLog.resource_id == ship["id"],
        )
        .all()
    )
    assert len(over_audits) == 1, "exactly one OVER_SHIP audit row on the over-shipping shipment"
    extra = over_audits[0].extra_data or {}
    assert extra.get("cumulative_shipped") == pytest.approx(12.0)
    assert extra.get("quantity_complete") == pytest.approx(10.0)
    assert extra.get("overage") == pytest.approx(2.0)

    events = OperationalEventService(db_session).list_events(
        company_id=COMPANY_A, event_type=OVER_SHIP_EVENT_TYPE, work_order_id=wo.id
    )
    assert len(events) == 1, "exactly one over_ship OperationalEvent"
    assert events[0].severity == "warning"


def test_within_bounds_single_ship_records_no_over_ship(client: TestClient, db_session: Session):
    """A single shipment within the produced quantity records NO over-ship (the guard is
    bounded -- it only fires when cumulative shipped exceeds quantity_complete)."""
    admin = make_user(db_session)
    part = make_part(db_session, standard_cost=6.0)
    wo = make_wo(db_session, part, quantity_ordered=10)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10)
    db_session.commit()

    complete_wo_via_op(client, admin, op, 10)
    db_session.expire_all()
    wo = db_session.get(WorkOrder, wo.id)

    ship = create_shipment(client, admin, wo, quantity_shipped=10)  # exactly the produced qty
    mark_shipped(client, admin, ship["id"])

    db_session.expire_all()
    assert (
        db_session.query(AuditLog)
        .filter(AuditLog.company_id == COMPANY_A, AuditLog.action == OVER_SHIP_AUDIT_ACTION)
        .count()
        == 0
    ), "a within-bounds ship must not record an over-ship"
    assert (
        OperationalEventService(db_session).list_events(
            company_id=COMPANY_A, event_type=OVER_SHIP_EVENT_TYPE, work_order_id=wo.id
        )
        == []
    )


def test_second_distinct_shipment_decrements_and_does_not_reclose_wo(client: TestClient, db_session: Session):
    """FIX (was ``test_second_shipment_is_short_circuited_by_wo_closed_guard``): a second
    DISTINCT, not-yet-shipped shipment on a WO that an EARLIER shipment already CLOSED
    must STILL ship and decrement finished goods. The endpoint's idempotency early-return
    is keyed ONLY on ``shipment.status == SHIPPED`` (a same-shipment resubmit), NOT on the
    WO being closed -- so partial / multi-shipment WOs are supported.

    Flow: complete a WO (quantity_complete = 10, FG lot of 10), ship a FIRST shipment of 4
    (WO -> CLOSED, FG -> 6), then ship a SECOND distinct shipment of 8 on the same
    now-CLOSED WO. The second ship MUST:
      - succeed (200, NOT short-circuited / no ``already_shipped`` flag),
      - write its OWN SHIP txn (reference_id = ship2.id, qty = -8),
      - decrement FG on-hand by 8 (6 -> -2; FG can go negative -- warn-and-record posture,
        not a hard stop),
      - record an OVER_SHIP audit row + over_ship event, since cumulative shipped
        (4 + 8 = 12) now exceeds quantity_complete (10) by 2.
    AND the WO must NOT be re-closed: exactly ONE work_order STATUS_CHANGE audit row exists
    across both ships (the close fired once, on ship1), and no duplicate close handling
    runs on ship2.
    Same-shipment re-ship remains a no-op (one SHIP txn per shipment).
    """
    admin = make_user(db_session)
    part = make_part(db_session, standard_cost=6.0)
    wo = make_wo(db_session, part, quantity_ordered=10)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, sequence=10)
    db_session.commit()

    complete_wo_via_op(client, admin, op, 10)
    db_session.expire_all()
    wo = db_session.get(WorkOrder, wo.id)

    # --- First shipment: closes the WO, decrements FG, no over-ship (4 <= 10). ---
    ship1 = create_shipment(client, admin, wo, quantity_shipped=4)
    mark_shipped(client, admin, ship1["id"])
    db_session.expire_all()
    assert db_session.get(WorkOrder, wo.id).status == WorkOrderStatus.CLOSED
    assert len(ship_txns(db_session, ship1["id"])) == 1  # first ship DID decrement
    assert fg_item(db_session, part.id, wo.lot_number).quantity_on_hand == 6  # 10 - 4

    # Baseline of work_order STATUS_CHANGE audit rows after ship1 has closed the WO.
    # (The operation-completion path writes IN_PROGRESS->COMPLETE; ship1 writes the
    # COMPLETE->CLOSED close -- so >=1 here. What matters for the "not re-closed" contract
    # is that ship2 adds ZERO further status-change rows; we assert that delta below.)
    def _wo_status_change_count() -> int:
        return (
            db_session.query(AuditLog)
            .filter(
                AuditLog.company_id == COMPANY_A,
                AuditLog.action == "STATUS_CHANGE",
                AuditLog.resource_type == "work_order",
                AuditLog.resource_id == wo.id,
            )
            .count()
        )

    wo_status_changes_after_ship1 = _wo_status_change_count()
    # Exactly one work_order_closed event so far (from ship1).
    assert (
        len(
            OperationalEventService(db_session).list_events(
                company_id=COMPANY_A, event_type="work_order_closed", work_order_id=wo.id
            )
        )
        == 1
    ), "ship1 closes the WO exactly once"
    # No over-ship recorded for the within-bounds first shipment.
    assert (
        db_session.query(AuditLog)
        .filter(AuditLog.company_id == COMPANY_A, AuditLog.action == OVER_SHIP_AUDIT_ACTION)
        .count()
        == 0
    )

    # --- Second DISTINCT shipment on the already-CLOSED WO: ships + decrements. ---
    ship2 = create_shipment(client, admin, wo, quantity_shipped=8)  # pushes cumulative to 12 > 10
    resp = client.post(f"/api/v1/shipping/{ship2['id']}/ship", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body.get("already_shipped") is not True, "a distinct unshipped shipment is NOT short-circuited"

    db_session.expire_all()

    # Second shipment writes its OWN SHIP txn keyed to ship2 (qty = -8).
    txns2 = ship_txns(db_session, ship2["id"])
    assert len(txns2) == 1, "second distinct shipment writes exactly one SHIP txn"
    assert txns2[0].quantity == -8, "SHIP txn quantity = negative quantity_shipped for ship2"
    assert txns2[0].reference_id == ship2["id"], "SHIP txn keyed to the SECOND shipment"
    assert txns2[0].reference_type == "shipment"
    assert txns2[0].transaction_type == TransactionType.SHIP
    # ship1's single SHIP txn is undisturbed.
    assert len(ship_txns(db_session, ship1["id"])) == 1, "ship1 SHIP txn unchanged by ship2"

    # FG on-hand decremented by the second quantity (6 - 8 = -2; can go negative).
    assert fg_item(db_session, part.id, wo.lot_number).quantity_on_hand == pytest.approx(
        -2.0
    ), "FG on-hand decremented by the second shipment quantity"

    # Cumulative over-ship (4 + 8 = 12 > 10) recorded on the SECOND shipment.
    over_audits = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.company_id == COMPANY_A,
            AuditLog.action == OVER_SHIP_AUDIT_ACTION,
            AuditLog.resource_type == "shipment",
            AuditLog.resource_id == ship2["id"],
        )
        .all()
    )
    assert len(over_audits) == 1, "exactly one OVER_SHIP audit row on the over-shipping second shipment"
    extra = over_audits[0].extra_data or {}
    assert extra.get("cumulative_shipped") == pytest.approx(12.0)
    assert extra.get("quantity_complete") == pytest.approx(10.0)
    assert extra.get("overage") == pytest.approx(2.0)
    over_events = OperationalEventService(db_session).list_events(
        company_id=COMPANY_A, event_type=OVER_SHIP_EVENT_TYPE, work_order_id=wo.id
    )
    assert len(over_events) == 1, "exactly one over_ship OperationalEvent on the cumulative over-ship"
    assert over_events[0].severity == "warning"

    # The WO is NOT re-closed: ship2 adds ZERO further work_order STATUS_CHANGE audit rows
    # (no CLOSED->CLOSED row), because the close-once block is gated on `wo.status != CLOSED`.
    assert (
        _wo_status_change_count() == wo_status_changes_after_ship1
    ), "ship2 must NOT write a second CLOSED->CLOSED status-change audit row"
    # Exactly ONE work_order_closed event total (the close-once side effects skip on ship2).
    closed_events = OperationalEventService(db_session).list_events(
        company_id=COMPANY_A, event_type="work_order_closed", work_order_id=wo.id
    )
    assert len(closed_events) == 1, "work_order_closed handling fired once (on ship1), not again on ship2"

    # --- Idempotency still holds: re-shipping the SAME (already-SHIPPED) shipment is a no-op. ---
    re_resp = client.post(f"/api/v1/shipping/{ship2['id']}/ship", headers=headers_for(admin))
    assert re_resp.status_code == status.HTTP_200_OK, re_resp.text
    assert re_resp.json().get("already_shipped") is True, "re-shipping the SAME shipment is short-circuited"
    db_session.expire_all()
    assert len(ship_txns(db_session, ship2["id"])) == 1, "same-shipment re-ship writes NO second SHIP txn"
    assert fg_item(db_session, part.id, wo.lot_number).quantity_on_hand == pytest.approx(
        -2.0
    ), "same-shipment re-ship does not decrement again"


# ---------------------------------------------------------------------------
# Tenant isolation: company-A ship never touches company-B inventory
# ---------------------------------------------------------------------------


def test_ship_decrement_is_tenant_isolated(client: TestClient, db_session: Session):
    """A company-A ship writes ONLY company-A inventory: no company-B SHIP txn is
    created and the company-B FG stock is untouched (invariant #1)."""
    admin_a = make_user(db_session, company_id=COMPANY_A)
    part_a = make_part(db_session, standard_cost=8.0, company_id=COMPANY_A)
    wo_a = make_wo(db_session, part_a, quantity_ordered=6, company_id=COMPANY_A)
    wc_a = make_work_center(db_session, company_id=COMPANY_A)
    op_a = make_op(db_session, wo_a, wc_a, sequence=10, company_id=COMPANY_A)

    # Pre-existing company-B FG stock that must remain untouched.
    _ensure_company(db_session, COMPANY_B)
    part_b = make_part(db_session, standard_cost=99.0, company_id=COMPANY_B)
    fg_b = InventoryItem(
        part_id=part_b.id,
        location=FINISHED_GOODS_LOCATION,
        warehouse="MAIN",
        quantity_on_hand=50,
        quantity_available=50,
        lot_number="B-LOT",
        unit_cost=99.0,
        status="available",
        is_active=True,
        company_id=COMPANY_B,
    )
    db_session.add(fg_b)
    db_session.commit()

    complete_wo_via_op(client, admin_a, op_a, 6)
    db_session.expire_all()
    wo_a = db_session.get(WorkOrder, wo_a.id)
    shipment = create_shipment(client, admin_a, wo_a, quantity_shipped=6)
    mark_shipped(client, admin_a, shipment["id"])

    db_session.expire_all()
    # Company-A SHIP txn exists and is stamped A.
    txns_a = ship_txns(db_session, shipment["id"], company_id=COMPANY_A)
    assert len(txns_a) == 1
    assert txns_a[0].company_id == COMPANY_A

    # No company-B SHIP txn was created, and company-B FG stock is unchanged.
    txns_b = db_session.query(InventoryTransaction).filter(InventoryTransaction.company_id == COMPANY_B).all()
    assert txns_b == [], "company A ship must not create any company B txn"
    assert db_session.get(InventoryItem, fg_b.id).quantity_on_hand == 50, "company B FG stock untouched"
