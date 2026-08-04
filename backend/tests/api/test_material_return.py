"""PR 3 behavior locks: the reasoned RETURN verb — the only un-consume there is.

Consumption never auto-reverses (invariant 6b): a negative delta is a no-op, because
the consume path also runs from a reconcile-on-read GET where there is no actor, no
intent and no reason to record. ``return_tied_material`` is that same reversal performed
by an ACTOR, with a REASON, on the tamper-evident chain — the compensating-transaction
pattern the receiving corrections established.

What this file pins, and why each one earns its place:

1. **The bound converges.** After a bounded ``correct_over_consumption`` the engine can
   never re-draw — asserted by replaying BOTH consumption seams *and* driving a real
   reconcile-on-read GET that completes the work order. This is the whole safety
   argument for leaving the tie OPEN; if it fails, the design is wrong, not the test.
2. **Unbounded is refused**, and the refusal names ``return_and_untie``.
3. **``return_and_untie``** gives everything back, cancels the tie with
   ``reason="material_returned"``, and a work-order delete/restore round trip must NOT
   resurrect it (``reopen_allocations_cancelled_by_delete`` keys on the *delete* reason).
4. **FIFO-spill unwind** credits the lots the material came off, newest-first — and a
   second return cannot over-credit, because the per-``(allocation, lot)`` cap is
   ``issued − already-returned``. That cap is the entire idempotency story: there is no
   unique index on RETURN rows.
5. **``unit_cost`` comes from the compensated ROW, not the lot**, so a revaluation
   between consume and return cannot strand material cost on the job.
6. **Refusals**: gone source lot (409), placeholder stock row (409), blank reason (422
   from Pydantic), over ``qty_consumed`` (422), nothing consumed (422).
7. **A negative lot** (shortage-driven consumption) is unwound toward zero, not refused.
8. **THE READERS.** For traceability, ``completion_cost_service``, all four
   ``analytics``-family COGS queries and ``prediction_service``: (a) a returned quantity
   is NETTED rather than added, and (b) a work order with NO returns is byte-identical
   to its pre-PR-3 numbers. Property (b) is what protects every existing job, so it is
   asserted with an explicit fingerprint comparison rather than inferred.
9. **Nest re-import stays refused after a full return** — the guard is ledger-keyed, so
   zeroing the ``qty_consumed`` cache must not unlock it. An owner decision, pinned.
10. **The reduce guard split**: the office verb now corrects a COMPLETE operation; the
    operator's shop-floor verb still 409s on one.
11. **RBAC**: below ADMIN/MANAGER/SUPERVISOR the return is refused and the ledger is
    untouched on the 403 path.
12. **``PATCH qty_per_run``** now guards over-consumption (it previously had none,
    unlike ``qty_planned``) — the cheapest way in the API to manufacture an unbounded
    correction allowance.
13. ``AllocationStatus.CLOSED`` is still never written.
"""

from datetime import date, datetime, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import and_, or_, text
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.db.ledger_filter import (
    BACKFLUSH_REFERENCE_TYPE,
    OPERATION_REFERENCE_TYPE,
    WORK_ORDER_ID_KEYED_REFERENCE_TYPES,
)
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.inventory import InventoryItem, InventoryTransaction, TransactionType
from app.models.part import Part
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus, WorkOrderType
from app.models.work_order_material import AllocationSource, AllocationStatus, WorkOrderMaterialAllocation
from app.services.analytics_service import AnalyticsService
from app.services.audit_service import AuditService
from app.services.completion_cost_service import _issued_material_cost as wo_issued_material_cost
from app.services.completion_inventory_service import (
    FINISHED_GOODS_LOCATION,
    apply_completion_inventory_effects,
    apply_operation_completion_inventory_effects,
)
from app.services.material_consumption_service import (
    MATERIAL_RETURN_REASON_CODE,
    MATERIAL_RETURNED_CANCEL_REASON,
    MaterialAllocationConsumedError,
    cancel_allocations_for_operations,
)
from app.services.prediction_service import PredictionService

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2
TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


# ---------------------------------------------------------------------------
# Fixtures (deliberately local; the sibling suites' helpers are private to them)
# ---------------------------------------------------------------------------


def _ensure_company(db: Session, company_id: int = COMPANY_A) -> Company:
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
        email=f"mret-{n}@co{company_id}.test",
        employee_id=f"MRET-{n:05d}",
        first_name="Ret",
        last_name="Verb",
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


def make_part(
    db: Session,
    *,
    standard_cost: float = 5.0,
    uom: str = "each",
    part_type: str = "manufactured",
    company_id: int = COMPANY_A,
) -> Part:
    _ensure_company(db, company_id)
    n = _next()
    part = Part(
        part_number=f"MRET-P-{n}",
        name=f"Part {n}",
        description="material-return fixture part",
        part_type=part_type,
        unit_of_measure=uom,
        standard_cost=standard_cost,
        backflush_components=False,
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def make_sheet(db: Session, *, company_id: int = COMPANY_A) -> Part:
    return make_part(db, uom="sheets", part_type="raw_material", standard_cost=80.0, company_id=company_id)


def make_work_center(db: Session, *, company_id: int = COMPANY_A) -> WorkCenter:
    _ensure_company(db, company_id)
    n = _next()
    wc = WorkCenter(
        name=f"MRET-WC-{n}",
        code=f"MRET-WC-{n}",
        work_center_type="laser",
        description="material-return fixture work center",
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
    quantity_ordered: float = 6,
    quantity_complete: float = 0,
    status_: WorkOrderStatus = WorkOrderStatus.IN_PROGRESS,
    laser: bool = False,
    company_id: int = COMPANY_A,
) -> WorkOrder:
    n = _next()
    wo = WorkOrder(
        work_order_number=f"MRET-WO-{n:05d}",
        customer_name="Acme",
        part_id=part.id,
        work_order_type=(WorkOrderType.LASER_CUTTING if laser else WorkOrderType.PRODUCTION).value,
        quantity_ordered=quantity_ordered,
        quantity_complete=quantity_complete,
        status=status_,
        priority=5,
        due_date=date.today() + timedelta(days=30),
        company_id=company_id,
    )
    db.add(wo)
    db.commit()
    db.refresh(wo)
    return wo


def make_op(
    db: Session,
    wo: WorkOrder,
    wc: WorkCenter,
    *,
    sequence: int = 10,
    runs: float = 5.0,
    quantity_complete: float = 0,
    quantity_scrapped: float = 0,
    status_: OperationStatus = OperationStatus.IN_PROGRESS,
    company_id: int = COMPANY_A,
) -> WorkOrderOperation:
    op = WorkOrderOperation(
        work_order_id=wo.id,
        work_center_id=wc.id,
        sequence=sequence,
        operation_number=f"OP{sequence}",
        name=f"Step {sequence}",
        status=status_,
        component_quantity=runs,
        quantity_complete=quantity_complete,
        quantity_scrapped=quantity_scrapped,
        company_id=company_id,
    )
    db.add(op)
    db.commit()
    db.refresh(op)
    return op


def make_inventory(
    db: Session,
    part: Part,
    *,
    qty: float = 50.0,
    lot: str = None,
    unit_cost: float = 80.0,
    location: str = "RAW-A",
    received_date: datetime = None,
    item_status: str = "available",
    company_id: int = COMPANY_A,
) -> InventoryItem:
    item = InventoryItem(
        part_id=part.id,
        location=location,
        warehouse="MAIN",
        quantity_on_hand=qty,
        quantity_allocated=0.0,
        quantity_available=qty,
        lot_number=lot if lot is not None else f"MRET-LOT-{_next():05d}",
        unit_cost=unit_cost,
        received_date=received_date,
        status=item_status,
        is_active=True,
        company_id=company_id,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def tie(
    db: Session,
    wo: WorkOrder,
    part: Part,
    *,
    operation: WorkOrderOperation = None,
    qty_per_run: float = 1.0,
    qty_planned: float = 5.0,
    qty_consumed: float = 0.0,
    status_: AllocationStatus = AllocationStatus.OPEN,
    company_id: int = COMPANY_A,
) -> WorkOrderMaterialAllocation:
    allocation = WorkOrderMaterialAllocation(
        company_id=company_id,
        work_order_id=wo.id,
        work_order_operation_id=operation.id if operation is not None else None,
        part_id=part.id,
        source=AllocationSource.NEST,
        status=status_,
        qty_per_run=qty_per_run if operation is not None else None,
        qty_planned=qty_planned,
        unit_of_measure=getattr(part.unit_of_measure, "value", part.unit_of_measure) or "each",
        qty_consumed=qty_consumed,
    )
    db.add(allocation)
    db.commit()
    db.refresh(allocation)
    return allocation


def closed_entry(
    db: Session,
    user: User,
    operation: WorkOrderOperation,
    *,
    quantity_produced: float,
    quantity_scrapped: float = 0,
) -> TimeEntry:
    """Durable CLOSED labor evidence — what both the reduce allowance and the
    read-time reconcile are computed from."""
    entry = TimeEntry(
        user_id=user.id,
        work_order_id=operation.work_order_id,
        operation_id=operation.id,
        work_center_id=operation.work_center_id,
        entry_type=TimeEntryType.RUN,
        clock_in=datetime.utcnow() - timedelta(hours=2),
        clock_out=datetime.utcnow() - timedelta(hours=1),
        duration_hours=1.0,
        quantity_produced=quantity_produced,
        quantity_scrapped=quantity_scrapped,
        company_id=user.company_id,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


# ---------------------------------------------------------------------------
# Drivers / observation helpers
# ---------------------------------------------------------------------------


def return_url(wo: WorkOrder, allocation: WorkOrderMaterialAllocation) -> str:
    return f"/api/v1/work-orders/{wo.id}/material-allocations/{allocation.id}/return"


def consumption_url(wo: WorkOrder, allocation: WorkOrderMaterialAllocation) -> str:
    return f"/api/v1/work-orders/{wo.id}/material-allocations/{allocation.id}/consumption"


def do_return(
    client: TestClient,
    user: User,
    wo: WorkOrder,
    allocation: WorkOrderMaterialAllocation,
    *,
    quantity: float,
    intent: str,
    reason: str = "over-counted the tray",
):
    return client.post(
        return_url(wo, allocation),
        headers=headers_for(user),
        json={"quantity": quantity, "intent": intent, "reason": reason},
    )


def complete_operation(client: TestClient, user: User, op: WorkOrderOperation, *, quantity: float):
    """Shop-floor operation complete — one of PR 2.5's four consumption call sites."""
    return client.post(
        f"/api/v1/shop-floor/operations/{op.id}/complete",
        headers=headers_for(user),
        json={"quantity_complete": quantity},
    )


def office_reduce(client: TestClient, user: User, op: WorkOrderOperation, *, delta: float, reason: str = "over-count"):
    return client.post(
        f"/api/v1/work-orders/operations/{op.id}/reduce-production",
        headers=headers_for(user),
        json={"quantity_delta": delta, "reason": reason},
    )


def operator_reduce(client: TestClient, user: User, op: WorkOrderOperation, *, delta: float):
    return client.post(
        f"/api/v1/shop-floor/operations/{op.id}/reduce-production",
        headers=headers_for(user),
        json={"quantity_delta": delta, "reason": "double-scanned the tray"},
    )


def allocation_ledger(
    db: Session, allocation: WorkOrderMaterialAllocation, *, company_id: int = COMPANY_A
) -> list[InventoryTransaction]:
    """Every ledger row carrying this tie's ``allocation_id``, oldest first."""
    return (
        db.query(InventoryTransaction)
        .filter(
            InventoryTransaction.company_id == company_id,
            InventoryTransaction.allocation_id == allocation.id,
        )
        .order_by(InventoryTransaction.id)
        .all()
    )


def allocation_movements(db: Session, allocation: WorkOrderMaterialAllocation) -> list[tuple]:
    """``(type, quantity, inventory_item_id, unit_cost)`` per tie row — the shape a
    replay must not change."""
    return [
        (t.transaction_type.value, t.quantity, t.inventory_item_id, t.unit_cost)
        for t in allocation_ledger(db, allocation)
    ]


def ledger_fingerprint(db: Session, *, company_id: int = COMPANY_A) -> list[tuple]:
    """A comparable fingerprint of every ledger row in a tenant (ids/timestamps out)."""
    return sorted(
        (
            t.part_id,
            t.transaction_type.value,
            t.quantity,
            t.reference_type,
            t.reference_number,
            t.lot_number,
            t.from_location,
            t.to_location,
            t.allocation_id,
            t.unit_cost,
            t.total_cost,
        )
        for t in db.query(InventoryTransaction).filter(InventoryTransaction.company_id == company_id).all()
    )


def wo_ledger_rows(db: Session, wo: WorkOrder, *, company_id: int = COMPANY_A) -> list[InventoryTransaction]:
    """EVERY ledger row belonging to one work order, under ALL THREE reference shapes.

    ``work_order`` (FG receipt + legacy one-shot rows), ``work_order_backflush`` (the
    reconciling component leg, which a work-order-scoped tie's RETURN now mirrors) and
    ``work_order_operation`` (per-run tie consumption and its returns).
    """
    operation_ids = [
        row[0]
        for row in db.query(WorkOrderOperation.id)
        .filter(WorkOrderOperation.company_id == company_id, WorkOrderOperation.work_order_id == wo.id)
        .all()
    ]
    return (
        db.query(InventoryTransaction)
        .filter(
            InventoryTransaction.company_id == company_id,
            or_(
                and_(
                    InventoryTransaction.reference_type.in_(WORK_ORDER_ID_KEYED_REFERENCE_TYPES),
                    InventoryTransaction.reference_id == wo.id,
                ),
                and_(
                    InventoryTransaction.reference_type == OPERATION_REFERENCE_TYPE,
                    InventoryTransaction.reference_id.in_(operation_ids or [-1]),
                ),
            ),
        )
        .order_by(InventoryTransaction.id)
        .all()
    )


def on_hand(db: Session, part: Part, *, company_id: int = COMPANY_A) -> float:
    return sum(
        float(row.quantity_on_hand or 0)
        for row in db.query(InventoryItem)
        .filter(InventoryItem.company_id == company_id, InventoryItem.part_id == part.id)
        .all()
    )


def consume_operation(db: Session, wo: WorkOrder, op: WorkOrderOperation, user: User) -> None:
    """Replay the PER-OPERATION consumption seam (PR 2.5's trigger)."""
    apply_operation_completion_inventory_effects(
        db, wo, op, user_id=user.id, company_id=COMPANY_A, audit=AuditService(db, user)
    )
    db.commit()


def consume_work_order(db: Session, wo: WorkOrder, user: User) -> None:
    """Replay the WHOLE-WORK-ORDER reconcile seam — the same entry point
    ``_apply_reconcile_inventory_effects`` calls from a GET."""
    apply_completion_inventory_effects(db, wo, user_id=user.id, company_id=COMPANY_A, audit=AuditService(db, user))
    db.commit()


# ===========================================================================
# 1. THE BOUND CONVERGES — the whole safety argument for a still-OPEN tie
# ===========================================================================


def _corrected_and_returned(client: TestClient, db_session: Session):
    """consume 5 -> office-reduce the count to 3 -> return the 2 now over-consumed.

    The settled state every convergence assertion below starts from: the tie is OPEN at
    ``qty_consumed == live target == 3``, so the engine's delta is zero on every path.
    """
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    operator = make_user(db_session, role=UserRole.OPERATOR)
    wc = make_work_center(db_session)
    sheet = make_sheet(db_session)
    lot = make_inventory(db_session, sheet, qty=20, unit_cost=80.0)

    wo = make_wo(db_session, make_part(db_session), quantity_ordered=5)
    op1 = make_op(db_session, wo, wc, sequence=10, runs=5.0)
    op2 = make_op(db_session, wo, wc, sequence=20, runs=5.0)
    allocation = tie(db_session, wo, sheet, operation=op1, qty_per_run=1.0, qty_planned=5.0)

    closed_entry(db_session, operator, op1, quantity_produced=5)
    assert complete_operation(client, operator, op1, quantity=5).status_code == status.HTTP_200_OK
    db_session.expire_all()
    assert db_session.get(WorkOrderMaterialAllocation, allocation.id).qty_consumed == 5.0
    assert on_hand(db_session, sheet) == 15.0

    reduced = office_reduce(client, supervisor, op1, delta=2, reason="counted the scrap tray twice")
    assert reduced.status_code == status.HTTP_200_OK, reduced.text
    db_session.expire_all()
    assert db_session.get(WorkOrderOperation, op1.id).quantity_complete == 3.0

    returned = do_return(
        client, supervisor, wo, allocation, quantity=2, intent="correct_over_consumption", reason="2 sheets uncut"
    )
    assert returned.status_code == status.HTTP_200_OK, returned.text
    body = returned.json()
    assert body["quantity_returned"] == 2.0
    assert body["qty_consumed_before"] == 5.0
    assert body["qty_consumed"] == 3.0
    assert body["status"] == AllocationStatus.OPEN.value
    assert [lot_line["inventory_item_id"] for lot_line in body["returned_lots"]] == [lot.id]

    db_session.expire_all()
    assert on_hand(db_session, sheet) == 17.0
    settled = allocation_movements(db_session, allocation)
    assert settled == [("issue", -5.0, lot.id, 80.0), ("return", 2.0, lot.id, 80.0)]
    return supervisor, operator, sheet, lot, wo, op1, op2, allocation, settled


def test_bounded_correction_leaves_nothing_for_the_engine_to_redraw(client: TestClient, db_session: Session):
    """After a bounded ``correct_over_consumption``, replaying EITHER seam posts nothing.

    ``correct_over_consumption`` leaves the tie OPEN, so the only thing standing between
    the returned material and an automatic re-draw is the arithmetic bound
    ``qty_consumed - live_target``. Below it, ``target - qty_consumed`` turns positive
    again and the next completion re-consumes what was just returned, re-running FIFO
    onto a possibly different lot.

    Both consumption seams are replayed against the settled state: the per-operation
    trigger (PR 2.5) and the whole-work-order reconcile (the self-heal behind it, and the
    same entry point ``_apply_reconcile_inventory_effects`` calls from a GET).

    The end-to-end reconcile-on-read leg lives in the test below, because it currently
    FAILS for a reason outside this verb — see that test.
    """
    _s, operator, sheet, _lot, wo, op1, _op2, allocation, settled = _corrected_and_returned(client, db_session)
    settled_on_hand = on_hand(db_session, sheet)

    # (a) the PER-OPERATION seam: target 3, consumed 3 => delta 0.
    consume_operation(
        db_session, db_session.get(WorkOrder, wo.id), db_session.get(WorkOrderOperation, op1.id), operator
    )
    db_session.expire_all()
    assert allocation_movements(db_session, allocation) == settled, "the per-operation seam re-drew"

    # (b) the WHOLE-WORK-ORDER reconcile seam.
    consume_work_order(db_session, db_session.get(WorkOrder, wo.id), operator)
    db_session.expire_all()
    assert allocation_movements(db_session, allocation) == settled, "the work-order reconcile re-drew"

    assert db_session.get(WorkOrderMaterialAllocation, allocation.id).qty_consumed == 3.0
    assert on_hand(db_session, sheet) == settled_on_hand


def test_reconcile_on_read_must_not_redraw_returned_material(client: TestClient, db_session: Session):
    """A GET must never move stock — there is no actor, no intent and no reason to record.

    This is the leg the whole bounded-return design rests on. ``correct_over_consumption``
    is bounded precisely so that "the engine can never draw again" holds on EVERY path
    including reconcile-on-read; if a page load can re-issue the material, the bound is
    not a bound and the two-intent split buys nothing.

    What actually happens today: the reconcile pushes the corrected operation count from
    3 back to its target of 5, the target-based delta becomes +2, and the engine posts a
    second ISSUE of 2 sheets attributed to whoever loaded the page. The tie's OWN
    arithmetic is correct throughout — it is the operation quantity underneath it that
    moves — which is why this is marked as an open defect rather than a return-verb bug.
    """
    supervisor, operator, sheet, _lot, wo, _op1, op2, allocation, settled = _corrected_and_returned(client, db_session)
    settled_on_hand = on_hand(db_session, sheet)

    closed_entry(db_session, operator, op2, quantity_produced=5)
    read = client.get(f"/api/v1/work-orders/{wo.id}", headers=headers_for(supervisor))
    assert read.status_code == status.HTTP_200_OK, read.text
    db_session.rollback()
    db_session.expire_all()
    assert (
        db_session.get(WorkOrder, wo.id).status == WorkOrderStatus.COMPLETE
    ), "the GET must really have completed the work order, or this leg proves nothing"

    assert allocation_movements(db_session, allocation) == settled, "reconcile-on-read re-drew the returned material"
    assert db_session.get(WorkOrderMaterialAllocation, allocation.id).qty_consumed == 3.0
    assert on_hand(db_session, sheet) == settled_on_hand


# ===========================================================================
# 2. Unbounded is refused, and the refusal names the way through
# ===========================================================================


def test_correction_past_the_live_bound_is_422_and_names_return_and_untie(client: TestClient, db_session: Session):
    """Below the bound the engine re-consumes, so the API refuses rather than allowing it.

    The tie has consumed 5 against a live target of 3, so exactly 2 is correctable. 3 is
    one more than the work still accounts for; returning it would leave
    ``target - qty_consumed`` positive and the next completion (or reconcile-on-read GET)
    would silently draw it back, re-running FIFO onto a possibly different lot.
    """
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    operator = make_user(db_session, role=UserRole.OPERATOR)
    wc = make_work_center(db_session)
    sheet = make_sheet(db_session)
    make_inventory(db_session, sheet, qty=20)

    wo = make_wo(db_session, make_part(db_session), quantity_ordered=5)
    op = make_op(db_session, wo, wc, sequence=10, runs=5.0)
    make_op(db_session, wo, wc, sequence=20, runs=5.0)  # keeps the WO open
    allocation = tie(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=5.0)

    closed_entry(db_session, operator, op, quantity_produced=5)
    assert complete_operation(client, operator, op, quantity=5).status_code == status.HTTP_200_OK
    assert office_reduce(client, supervisor, op, delta=2).status_code == status.HTTP_200_OK
    db_session.expire_all()

    frozen = ledger_fingerprint(db_session)
    resp = do_return(client, supervisor, wo, allocation, quantity=3, intent="correct_over_consumption")
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, resp.text
    detail = resp.json()["detail"]
    assert "return_and_untie" in detail, detail
    assert "re-consumed automatically" in detail, detail

    db_session.expire_all()
    assert ledger_fingerprint(db_session) == frozen, "a refused return must leave the ledger untouched"
    assert db_session.get(WorkOrderMaterialAllocation, allocation.id).qty_consumed == 5.0

    # …and the bound itself is honoured: exactly 2 goes through.
    assert (
        do_return(client, supervisor, wo, allocation, quantity=2, intent="correct_over_consumption").status_code
        == status.HTTP_200_OK
    )


def test_a_work_order_scoped_tie_is_bounded_by_its_plan(client: TestClient, db_session: Session):
    """A work-order-scoped tie's live target is ``qty_planned``, not a run count.

    Since PR 4.4 the backflush's tie leg reconciles that target directly
    (``delta = qty_planned - net_consumed_quantity_for_allocation``), so leaving
    ``qty_consumed`` below the plan re-arms it. A tie drained exactly to plan therefore
    has a ZERO correction allowance and ``return_and_untie`` is its only way back — the
    documented consequence, pinned so nobody "relaxes" it.

    The hand-built ISSUE standing in for that drain now carries the shape the leg really
    writes (``work_order_backflush``), and the compensating RETURN must mirror it: a
    return that landed under the legacy ``work_order`` shape would put a credit inside
    ``uq_wo_inventory_issue``'s neighbourhood for a tie that never wrote a row there.
    """
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    sheet = make_sheet(db_session)
    lot = make_inventory(db_session, sheet, qty=20)
    wo = make_wo(db_session, make_part(db_session), quantity_ordered=4)
    allocation = tie(db_session, wo, sheet, operation=None, qty_planned=4.0, qty_consumed=4.0)

    # A hand-built ISSUE standing in for leg 2's drain, in the shape that leg really writes.
    db_session.add(
        InventoryTransaction(
            company_id=COMPANY_A,
            inventory_item_id=lot.id,
            part_id=sheet.id,
            transaction_type=TransactionType.ISSUE,
            quantity=-4.0,
            from_location=lot.location,
            lot_number=lot.lot_number,
            reference_type=BACKFLUSH_REFERENCE_TYPE,
            reference_id=wo.id,
            reference_number=wo.work_order_number,
            allocation_id=allocation.id,
            unit_cost=80.0,
            total_cost=320.0,
            created_by=supervisor.id,
        )
    )
    lot.quantity_on_hand = 16.0
    db_session.commit()

    refused = do_return(client, supervisor, wo, allocation, quantity=1, intent="correct_over_consumption")
    assert refused.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, refused.text
    assert "return_and_untie" in refused.json()["detail"]

    allowed = do_return(client, supervisor, wo, allocation, quantity=4, intent="return_and_untie")
    assert allowed.status_code == status.HTTP_200_OK, allowed.text
    db_session.expire_all()
    assert db_session.get(WorkOrderMaterialAllocation, allocation.id).status == AllocationStatus.CANCELLED
    # The compensating row mirrors the ISSUE's work-order reference shape.
    rows = allocation_ledger(db_session, allocation)
    assert [(r.transaction_type.value, r.reference_type, r.reference_id) for r in rows] == [
        ("issue", BACKFLUSH_REFERENCE_TYPE, wo.id),
        ("return", BACKFLUSH_REFERENCE_TYPE, wo.id),
    ]


# ===========================================================================
# 3. return_and_untie — full credit, cancelled tie, and it stays cancelled
# ===========================================================================


def test_return_and_untie_cancels_the_tie_and_survives_a_delete_restore_round_trip(
    client: TestClient, db_session: Session
):
    """The cancel reason is what stops a restore resurrecting a deliberately-closed tie.

    ``reopen_allocations_cancelled_by_delete`` re-opens exactly those ties whose most
    recent DELETE audit row carries the *work-order-delete* reason. A return-and-untie
    stamps ``material_returned`` instead, so a delete/restore round trip must leave it
    cancelled — otherwise a restore re-arms demand for material somebody explicitly gave
    back, and (once ``backflush_components`` is exposed) the same part gets issued twice.
    """
    admin = make_user(db_session, role=UserRole.ADMIN)
    operator = make_user(db_session, role=UserRole.OPERATOR)
    wc = make_work_center(db_session)
    sheet = make_sheet(db_session)
    make_inventory(db_session, sheet, qty=20)

    wo = make_wo(db_session, make_part(db_session), quantity_ordered=4)
    op = make_op(db_session, wo, wc, sequence=10, runs=4.0)
    make_op(db_session, wo, wc, sequence=20, runs=4.0)  # keeps the WO open
    allocation = tie(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=4.0)

    closed_entry(db_session, operator, op, quantity_produced=4)
    assert complete_operation(client, operator, op, quantity=4).status_code == status.HTTP_200_OK
    db_session.expire_all()
    assert on_hand(db_session, sheet) == 16.0

    resp = do_return(client, admin, wo, allocation, quantity=4, intent="return_and_untie", reason="job cancelled")
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["quantity_returned"] == 4.0
    assert body["qty_consumed"] == 0.0
    assert body["status"] == AllocationStatus.CANCELLED.value

    db_session.expire_all()
    assert on_hand(db_session, sheet) == 20.0, "everything went back"
    live = db_session.get(WorkOrderMaterialAllocation, allocation.id)
    assert live.status == AllocationStatus.CANCELLED
    assert live.qty_consumed == 0.0

    # The cancel's own audit row carries the discriminator the restore reads.
    cancel_rows = [
        row
        for row in db_session.query(AuditLog)
        .filter(
            AuditLog.company_id == COMPANY_A,
            AuditLog.resource_type == "work_order_material_allocation",
            AuditLog.resource_id == allocation.id,
        )
        .order_by(AuditLog.id)
        .all()
        if (row.extra_data or {}).get("reason") == MATERIAL_RETURNED_CANCEL_REASON
    ]
    assert len(cancel_rows) == 1, "exactly one material_returned cancel row"
    assert cancel_rows[0].action.upper() in ("DELETE", "SOFT_DELETE"), cancel_rows[0].action

    # -- the round trip: soft delete then restore ---------------------------------
    assert (
        client.delete(f"/api/v1/work-orders/{wo.id}", headers=headers_for(admin)).status_code
        == status.HTTP_204_NO_CONTENT
    )
    restored = client.post(f"/api/v1/work-orders/{wo.id}/restore", headers=headers_for(admin))
    assert restored.status_code == status.HTTP_200_OK, restored.text

    db_session.expire_all()
    assert (
        db_session.get(WorkOrderMaterialAllocation, allocation.id).status == AllocationStatus.CANCELLED
    ), "a restore must not resurrect a tie that was cancelled because its material was RETURNED"

    # And the engine cannot draw against a cancelled tie either.
    frozen = allocation_movements(db_session, allocation)
    consume_work_order(db_session, db_session.get(WorkOrder, wo.id), operator)
    db_session.expire_all()
    assert allocation_movements(db_session, allocation) == frozen
    assert on_hand(db_session, sheet) == 20.0


def test_return_and_untie_never_closes_a_tie_with_material_still_out(
    client: TestClient, db_session: Session, monkeypatch
):
    """A completion racing into the lock window must not cost the shop 3 sheets.

    The handler reads the tie, then ``_lock_return_scope`` takes the row locks. A
    completion committing in that window advances BOTH the ledger and the
    ``qty_consumed`` cache — while the request's in-session copy of the allocation still
    holds the pre-lock value. ``return_and_untie`` computes ``consumed_before`` from that
    copy, so it can credit the OLD total and then CANCEL the tie, stranding the
    difference: material issued to a job, a closed tie, and a success toast.

    The invariant asserted here is deliberately not "it returns 422" or "it returns 8" —
    either is a sound resolution. It is the one both must satisfy and the bug cannot:

        **if the tie ends CANCELLED, the ledger's net consumed for it must be zero.**

    A ``return_and_untie`` that closes a tie while the ledger still says material is out
    is the failure, whichever way the verb chooses to avoid it.
    """
    import app.services.material_consumption_service as mcs

    supervisor, _operator, sheet, lot, wo, _op, allocation = _consumed_scenario(client, db_session, qty=5.0)
    assert on_hand(db_session, sheet) == 5.0  # 10 - 5

    original_lock = mcs._lock_return_scope
    fired = {"done": False}

    def racing_lock(db, *, work_order, allocation, company_id):
        operation = original_lock(db, work_order=work_order, allocation=allocation, company_id=company_id)
        if not fired["done"]:
            fired["done"] = True
            # A concurrent completion consumes 3 more: a real ledger row, the cache
            # advanced, the lot decremented. The UPDATE is raw SQL precisely so the
            # request's already-loaded ``allocation`` keeps its STALE qty_consumed --
            # which is exactly what another transaction's commit looks like from here.
            db.add(
                InventoryTransaction(
                    company_id=company_id,
                    inventory_item_id=lot.id,
                    part_id=sheet.id,
                    transaction_type=TransactionType.ISSUE,
                    quantity=-3.0,
                    from_location=lot.location,
                    lot_number=lot.lot_number,
                    reference_type=OPERATION_REFERENCE_TYPE,
                    reference_id=allocation.work_order_operation_id,
                    reference_number=work_order.work_order_number,
                    allocation_id=allocation.id,
                    unit_cost=80.0,
                    total_cost=240.0,
                    created_by=supervisor.id,
                )
            )
            db.execute(
                text("UPDATE work_order_material_allocations SET qty_consumed = 8.0 WHERE id = :allocation_id"),
                {"allocation_id": allocation.id},
            )
            db.execute(
                text("UPDATE inventory_items SET quantity_on_hand = quantity_on_hand - 3.0 WHERE id = :item_id"),
                {"item_id": lot.id},
            )
            db.flush()
        return operation

    monkeypatch.setattr(mcs, "_lock_return_scope", racing_lock)

    # The client submits the quantity it could see before the race: 5.
    response = do_return(client, supervisor, wo, allocation, quantity=5, intent="return_and_untie")
    assert fired["done"], "the simulated race never ran — the test would be vacuous"

    db_session.expire_all()
    live = db_session.get(WorkOrderMaterialAllocation, allocation.id)
    rows = allocation_ledger(db_session, allocation)
    net_out = sum(
        (-1.0 if row.transaction_type == TransactionType.RETURN else 1.0) * abs(float(row.quantity or 0))
        for row in rows
    )
    if live.status == AllocationStatus.CANCELLED:
        assert net_out == pytest.approx(0.0), (
            f"the tie was closed with {net_out} still issued against it (HTTP {response.status_code}) — "
            "a racing completion cost the shop that material"
        )
    else:
        # The other sound resolution: refuse, credit nothing, close nothing. (The
        # handler's rollback also unwinds the simulated race, because this harness runs
        # the whole request in the test's own transaction -- so the ledger is asserted to
        # carry no RETURN row rather than a particular issued total.)
        assert response.status_code in (
            status.HTTP_409_CONFLICT,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
        ), response.text
        assert not any(
            row.transaction_type == TransactionType.RETURN for row in rows
        ), "a refused return must not have credited anything"
        # It refused because it re-read the tie UNDER THE LOCK: the detail names the
        # racing completion's total, not the stale 5 the client submitted.
        assert "8.0" in response.json()["detail"], response.text


def test_return_and_untie_refuses_a_partial_quantity(client: TestClient, db_session: Session):
    """The quantity is a CONFIRMATION of what was consumed, so a mismatch is refused.

    Catches the stale client — a completion that landed between the page load and the
    submit — instead of returning a different amount than the operator was looking at.
    """
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    operator = make_user(db_session, role=UserRole.OPERATOR)
    wc = make_work_center(db_session)
    sheet = make_sheet(db_session)
    make_inventory(db_session, sheet, qty=20)

    wo = make_wo(db_session, make_part(db_session), quantity_ordered=4)
    op = make_op(db_session, wo, wc, sequence=10, runs=4.0)
    make_op(db_session, wo, wc, sequence=20, runs=4.0)
    allocation = tie(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=4.0)

    closed_entry(db_session, operator, op, quantity_produced=4)
    assert complete_operation(client, operator, op, quantity=4).status_code == status.HTTP_200_OK
    db_session.expire_all()

    frozen = ledger_fingerprint(db_session)
    resp = do_return(client, supervisor, wo, allocation, quantity=3, intent="return_and_untie")
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, resp.text
    assert "returns everything consumed" in resp.json()["detail"]
    db_session.expire_all()
    assert ledger_fingerprint(db_session) == frozen
    assert db_session.get(WorkOrderMaterialAllocation, allocation.id).status == AllocationStatus.OPEN


# ===========================================================================
# 4. FIFO-spill unwind, and the per-lot cap that IS the idempotency story
# ===========================================================================


def _spilled_scenario(client: TestClient, db_session: Session):
    """A consumption of 5 spread across two FIFO lots: 2 from the older, 3 from the newer."""
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    operator = make_user(db_session, role=UserRole.OPERATOR)
    wc = make_work_center(db_session)
    sheet = make_sheet(db_session)
    old_lot = make_inventory(
        db_session, sheet, qty=2, unit_cost=50.0, lot="MRET-OLD", received_date=datetime(2026, 1, 1)
    )
    new_lot = make_inventory(
        db_session, sheet, qty=10, unit_cost=90.0, lot="MRET-NEW", received_date=datetime(2026, 6, 1)
    )

    wo = make_wo(db_session, make_part(db_session), quantity_ordered=5)
    op = make_op(db_session, wo, wc, sequence=10, runs=5.0)
    make_op(db_session, wo, wc, sequence=20, runs=5.0)  # keeps the WO open
    allocation = tie(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=5.0)

    closed_entry(db_session, operator, op, quantity_produced=5)
    assert complete_operation(client, operator, op, quantity=5).status_code == status.HTTP_200_OK
    db_session.expire_all()

    # FIFO drained the OLDER lot first, then spilled onto the newer one.
    assert allocation_movements(db_session, allocation) == [
        ("issue", -2.0, old_lot.id, 50.0),
        ("issue", -3.0, new_lot.id, 90.0),
    ]
    assert db_session.get(InventoryItem, old_lot.id).quantity_on_hand == 0.0
    assert db_session.get(InventoryItem, new_lot.id).quantity_on_hand == 7.0
    return supervisor, operator, sheet, old_lot, new_lot, wo, op, allocation


def test_a_spilled_consumption_returns_to_those_lots_newest_first(client: TestClient, db_session: Session):
    """Material goes back to the lots it came off — never to a convenient sink.

    Crediting any other lot would invent heat/cert linkage in an as-built record
    (AS9100D 8.5.2). The walk is newest-first because that is the reverse of the order
    consumption posted, so an over-count correction compensates the most recent ISSUE.
    """
    supervisor, _operator, sheet, old_lot, new_lot, wo, op, allocation = _spilled_scenario(client, db_session)

    assert office_reduce(client, supervisor, op, delta=4).status_code == status.HTTP_200_OK
    db_session.expire_all()

    # target is now 1, consumed 5 => 4 correctable. Take 3 first.
    first = do_return(client, supervisor, wo, allocation, quantity=3, intent="correct_over_consumption")
    assert first.status_code == status.HTTP_200_OK, first.text
    assert [(lot["inventory_item_id"], lot["quantity"]) for lot in first.json()["returned_lots"]] == [
        (new_lot.id, 3.0)
    ], "the NEWEST issue row is compensated first"

    db_session.expire_all()
    assert db_session.get(InventoryItem, new_lot.id).quantity_on_hand == 10.0
    assert db_session.get(InventoryItem, old_lot.id).quantity_on_hand == 0.0, "the older lot is untouched so far"

    # The second return CANNOT re-credit the newer lot: its capacity is exhausted
    # (issued 3, already returned 3), so the residual falls to the older lot's row.
    second = do_return(client, supervisor, wo, allocation, quantity=1, intent="correct_over_consumption")
    assert second.status_code == status.HTTP_200_OK, second.text
    assert [(lot["inventory_item_id"], lot["quantity"]) for lot in second.json()["returned_lots"]] == [
        (old_lot.id, 1.0)
    ]

    db_session.expire_all()
    assert db_session.get(InventoryItem, new_lot.id).quantity_on_hand == 10.0, "no over-credit onto the newer lot"
    assert db_session.get(InventoryItem, old_lot.id).quantity_on_hand == 1.0
    assert on_hand(db_session, sheet) == 11.0, "2 + 10 issued 5, returned 4"

    # Per-lot invariant: returns can never exceed what that lot issued.
    per_lot: dict[int, float] = {}
    for row in allocation_ledger(db_session, allocation):
        sign = -1.0 if row.transaction_type == TransactionType.RETURN else 1.0
        per_lot[row.inventory_item_id] = per_lot.get(row.inventory_item_id, 0.0) + sign * abs(float(row.quantity))
    assert per_lot == {old_lot.id: 1.0, new_lot.id: 0.0}
    assert all(value >= 0 for value in per_lot.values()), "a lot was credited more than it ever issued"


def test_the_ledger_not_the_cache_caps_what_can_come_back(client: TestClient, db_session: Session):
    """When the ``qty_consumed`` cache disagrees with the ledger, the LEDGER wins (409).

    ``qty_consumed`` is documented as a cache, and the ledger's ``allocation_id`` rows are
    authoritative. Trusting the cache would credit stock no ISSUE row ever took, so the
    cache is deliberately inflated here — the one state that isolates this guard from the
    quantity/bound checks in front of it.
    """
    supervisor, _operator, sheet, _old_lot, _new_lot, wo, op, allocation = _spilled_scenario(client, db_session)

    assert office_reduce(client, supervisor, op, delta=5).status_code == status.HTTP_200_OK
    # Drive the cache above the ledger without touching the ledger itself.
    live = db_session.get(WorkOrderMaterialAllocation, allocation.id)
    live.qty_consumed = 9.0
    db_session.commit()

    frozen = ledger_fingerprint(db_session)
    resp = do_return(client, supervisor, wo, allocation, quantity=7, intent="correct_over_consumption")
    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
    detail = resp.json()["detail"]
    assert "only 5" in detail, detail
    assert "ledger is authoritative" in detail, detail

    db_session.expire_all()
    assert ledger_fingerprint(db_session) == frozen, "nothing may post before a refusal"
    assert on_hand(db_session, sheet) == 7.0


def test_the_consumption_read_previews_exactly_what_a_return_will_do(client: TestClient, db_session: Session):
    """``GET .../consumption`` is the return dialog's disclosure, and it must not lie.

    Ordering and per-lot ``net`` mirror the return planner exactly (newest source lot
    first, ``net = issued - returned`` per lot), so the preview and the outcome cannot
    disagree about which lot gets what. A fully squared-up lot is still LISTED — dropping
    it would make a returned tie look as though the material never touched that lot.
    """
    supervisor, _operator, _sheet, old_lot, new_lot, wo, op, allocation = _spilled_scenario(client, db_session)

    before = client.get(consumption_url(wo, allocation), headers=headers_for(supervisor))
    assert before.status_code == status.HTTP_200_OK, before.text
    assert [(line["inventory_item_id"], line["issued"], line["returned"], line["net"]) for line in before.json()] == [
        (new_lot.id, 3.0, 0.0, 3.0),
        (old_lot.id, 2.0, 0.0, 2.0),
    ], "newest source lot first — the order a return credits in"

    assert office_reduce(client, supervisor, op, delta=4).status_code == status.HTTP_200_OK
    assert (
        do_return(client, supervisor, wo, allocation, quantity=3, intent="correct_over_consumption").status_code
        == status.HTTP_200_OK
    )

    after = client.get(consumption_url(wo, allocation), headers=headers_for(supervisor))
    assert after.status_code == status.HTTP_200_OK, after.text
    assert [(line["inventory_item_id"], line["issued"], line["returned"], line["net"]) for line in after.json()] == [
        (new_lot.id, 3.0, 3.0, 0.0),
        (old_lot.id, 2.0, 0.0, 2.0),
    ], "a squared-up lot stays listed at net 0"


# ===========================================================================
# 5. unit_cost comes from the COMPENSATED ROW, not the lot
# ===========================================================================


def test_a_return_prices_off_the_issue_row_so_job_cost_nets_to_zero(client: TestClient, db_session: Session):
    """Revalue the lot between consume and return: the RETURN still carries 80, not 250.

    Pricing off the lot's *current* cost would strand residual (or negative) material
    cost on the job for material that physically came back. The job's net material cost
    after a full return must be exactly zero, in BOTH cost readers.
    """
    admin = make_user(db_session, role=UserRole.ADMIN)
    operator = make_user(db_session, role=UserRole.OPERATOR)
    wc = make_work_center(db_session)
    sheet = make_sheet(db_session)
    lot = make_inventory(db_session, sheet, qty=10, unit_cost=80.0)

    wo = make_wo(db_session, make_part(db_session), quantity_ordered=3)
    op = make_op(db_session, wo, wc, sequence=10, runs=3.0)
    make_op(db_session, wo, wc, sequence=20, runs=3.0)
    allocation = tie(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=3.0)

    closed_entry(db_session, operator, op, quantity_produced=3)
    assert complete_operation(client, operator, op, quantity=3).status_code == status.HTTP_200_OK
    db_session.expire_all()

    live_wo = db_session.get(WorkOrder, wo.id)
    assert wo_issued_material_cost(db_session, live_wo, COMPANY_A) == pytest.approx(240.0)
    assert AnalyticsService(db_session, COMPANY_A)._issued_material_cost(wo.id) == pytest.approx(240.0)

    # A revaluation lands between the consumption and the return.
    db_session.get(InventoryItem, lot.id).unit_cost = 250.0
    db_session.commit()

    resp = do_return(client, admin, wo, allocation, quantity=3, intent="return_and_untie", reason="job cancelled")
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert [line["unit_cost"] for line in resp.json()["returned_lots"]] == [80.0], "the COMPENSATED row's cost"

    db_session.expire_all()
    return_row = [r for r in allocation_ledger(db_session, allocation) if r.transaction_type == TransactionType.RETURN]
    assert len(return_row) == 1
    assert return_row[0].unit_cost == 80.0
    assert return_row[0].total_cost == pytest.approx(240.0)
    assert return_row[0].reason_code == MATERIAL_RETURN_REASON_CODE

    live_wo = db_session.get(WorkOrder, wo.id)
    assert wo_issued_material_cost(db_session, live_wo, COMPANY_A) == pytest.approx(0.0)
    assert AnalyticsService(db_session, COMPANY_A)._issued_material_cost(wo.id) == pytest.approx(0.0)


# ===========================================================================
# 6. Refusals
# ===========================================================================


def _consumed_scenario(client: TestClient, db_session: Session, *, qty: float = 3.0, lot_qty: float = 10.0):
    """One operation-scoped tie that has consumed ``qty`` from a single lot."""
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    operator = make_user(db_session, role=UserRole.OPERATOR)
    wc = make_work_center(db_session)
    sheet = make_sheet(db_session)
    lot = make_inventory(db_session, sheet, qty=lot_qty)

    wo = make_wo(db_session, make_part(db_session), quantity_ordered=qty)
    op = make_op(db_session, wo, wc, sequence=10, runs=qty)
    make_op(db_session, wo, wc, sequence=20, runs=qty)  # keeps the WO open
    allocation = tie(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=qty)

    closed_entry(db_session, operator, op, quantity_produced=qty)
    assert complete_operation(client, operator, op, quantity=qty).status_code == status.HTTP_200_OK
    db_session.expire_all()
    return supervisor, operator, sheet, lot, wo, op, allocation


def test_a_missing_source_lot_is_409_naming_the_row(client: TestClient, db_session: Session):
    """Nothing in ``app/`` deletes stock rows, so a gone lot means an out-of-band write.

    Crediting the quantity anywhere else would put material on a lot that never held it,
    so the verb refuses rather than guessing — receiving's "409 rather than guess".
    """
    supervisor, _operator, _sheet, lot, wo, _op, allocation = _consumed_scenario(client, db_session)

    lot_id = lot.id
    db_session.delete(db_session.get(InventoryItem, lot_id))
    db_session.commit()

    frozen = ledger_fingerprint(db_session)
    resp = do_return(client, supervisor, wo, allocation, quantity=3, intent="return_and_untie")
    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
    detail = resp.json()["detail"]
    assert f"inventory item {lot_id}" in detail, detail
    assert "no longer exists" in detail, detail

    db_session.expire_all()
    assert ledger_fingerprint(db_session) == frozen
    assert db_session.get(WorkOrderMaterialAllocation, allocation.id).status == AllocationStatus.OPEN


def test_a_placeholder_stock_row_is_409_and_never_becomes_real_stock(client: TestClient, db_session: Session):
    """The lot-less finished-goods anchor is a ledger anchor, not a lot.

    ``_placeholder_stock_row`` is minted when a part has NO stock at all and a shortage
    still has to be recorded. Crediting it back would turn that anchor into unlabeled,
    FIFO-eligible stock with no heat and no cert (AS9100D 8.5.2).
    """
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    operator = make_user(db_session, role=UserRole.OPERATOR)
    wc = make_work_center(db_session)
    sheet = make_sheet(db_session)
    # Deliberately NO stock row for the sheet: the shortage leg must mint a placeholder.

    wo = make_wo(db_session, make_part(db_session), quantity_ordered=2)
    op = make_op(db_session, wo, wc, sequence=10, runs=2.0)
    make_op(db_session, wo, wc, sequence=20, runs=2.0)
    allocation = tie(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=2.0)

    closed_entry(db_session, operator, op, quantity_produced=2)
    assert complete_operation(client, operator, op, quantity=2).status_code == status.HTTP_200_OK
    db_session.expire_all()

    placeholder = (
        db_session.query(InventoryItem)
        .filter(InventoryItem.company_id == COMPANY_A, InventoryItem.part_id == sheet.id)
        .one()
    )
    assert placeholder.location == FINISHED_GOODS_LOCATION and not placeholder.lot_number
    assert placeholder.quantity_on_hand == -2.0, "the shortage drove the anchor negative"

    frozen = ledger_fingerprint(db_session)
    resp = do_return(client, supervisor, wo, allocation, quantity=2, intent="return_and_untie")
    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
    detail = resp.json()["detail"]
    assert "placeholder stock row" in detail, detail

    db_session.expire_all()
    assert ledger_fingerprint(db_session) == frozen
    assert db_session.get(InventoryItem, placeholder.id).quantity_on_hand == -2.0


@pytest.mark.parametrize("reason", ["", "   ", "\t\n"])
def test_a_blank_reason_is_422_from_pydantic(client: TestClient, db_session: Session, reason: str):
    """An unreasoned compensating movement is exactly what the audit chain must not hold.

    Validated at the Pydantic boundary (like ``ReceiptCorrection.reason``), so a blank
    reason is FastAPI's own 422 rather than a hand-rolled refusal downstream.
    """
    supervisor, _operator, sheet, _lot, wo, _op, allocation = _consumed_scenario(client, db_session)

    frozen = ledger_fingerprint(db_session)
    resp = do_return(client, supervisor, wo, allocation, quantity=3, intent="return_and_untie", reason=reason)
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, resp.text
    db_session.expire_all()
    assert ledger_fingerprint(db_session) == frozen
    assert on_hand(db_session, sheet) == 7.0


def test_more_than_qty_consumed_is_422(client: TestClient, db_session: Session):
    supervisor, _operator, sheet, _lot, wo, _op, allocation = _consumed_scenario(client, db_session)

    frozen = ledger_fingerprint(db_session)
    resp = do_return(client, supervisor, wo, allocation, quantity=4, intent="return_and_untie")
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, resp.text
    assert "only 3.0 has been consumed" in resp.json()["detail"], resp.text
    db_session.expire_all()
    assert ledger_fingerprint(db_session) == frozen
    assert on_hand(db_session, sheet) == 7.0


def test_nothing_consumed_is_422_and_points_at_untie(client: TestClient, db_session: Session):
    """A tie that never consumed has nothing to give back — untie it instead."""
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    wc = make_work_center(db_session)
    sheet = make_sheet(db_session)
    make_inventory(db_session, sheet, qty=10)
    wo = make_wo(db_session, make_part(db_session), quantity_ordered=3)
    op = make_op(db_session, wo, wc, sequence=10, runs=3.0)
    allocation = tie(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=3.0)

    frozen = ledger_fingerprint(db_session)
    resp = do_return(client, supervisor, wo, allocation, quantity=1, intent="correct_over_consumption")
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, resp.text
    detail = resp.json()["detail"]
    assert "Nothing has been consumed" in detail, detail
    assert "Untie it instead" in detail, detail
    db_session.expire_all()
    assert ledger_fingerprint(db_session) == frozen


def test_a_non_positive_quantity_is_422(client: TestClient, db_session: Session):
    supervisor, _operator, _sheet, _lot, wo, _op, allocation = _consumed_scenario(client, db_session)
    for bad in (0, -1):
        resp = do_return(client, supervisor, wo, allocation, quantity=bad, intent="correct_over_consumption")
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, resp.text


def test_another_tenants_tie_is_404_never_403(client: TestClient, db_session: Session):
    """An id must not be probeable: cross-tenant is 404, the same as "does not exist"."""
    _ensure_company(db_session, COMPANY_B)
    _supervisor_a, _operator, _sheet, _lot, wo, _op, allocation = _consumed_scenario(client, db_session)
    intruder = make_user(db_session, role=UserRole.ADMIN, company_id=COMPANY_B)

    frozen = ledger_fingerprint(db_session)
    resp = do_return(client, intruder, wo, allocation, quantity=1, intent="correct_over_consumption")
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
    db_session.expire_all()
    assert ledger_fingerprint(db_session) == frozen


# ===========================================================================
# 7. A negative lot is unwound toward zero, not refused
# ===========================================================================


def test_a_shortage_driven_negative_lot_is_unwound_by_a_return(client: TestClient, db_session: Session):
    """Shortage never blocks production, so a negative lot is an EXPECTED shape.

    The return credits it back toward zero. Refusing here would leave the one state the
    verb most obviously exists for with no way out.
    """
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    operator = make_user(db_session, role=UserRole.OPERATOR)
    wc = make_work_center(db_session)
    sheet = make_sheet(db_session)
    lot = make_inventory(db_session, sheet, qty=1)  # only 1 on hand, 4 will be consumed

    wo = make_wo(db_session, make_part(db_session), quantity_ordered=4)
    op = make_op(db_session, wo, wc, sequence=10, runs=4.0)
    make_op(db_session, wo, wc, sequence=20, runs=4.0)
    allocation = tie(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=4.0)

    closed_entry(db_session, operator, op, quantity_produced=4)
    assert complete_operation(client, operator, op, quantity=4).status_code == status.HTTP_200_OK
    db_session.expire_all()
    assert db_session.get(InventoryItem, lot.id).quantity_on_hand == -3.0, "the shortage drove the lot negative"

    assert office_reduce(client, supervisor, op, delta=2).status_code == status.HTTP_200_OK
    resp = do_return(client, supervisor, wo, allocation, quantity=2, intent="correct_over_consumption")
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    live_lot = db_session.get(InventoryItem, lot.id)
    assert live_lot.quantity_on_hand == -1.0, "unwound toward zero, not clamped and not refused"
    assert live_lot.quantity_available == live_lot.quantity_on_hand - float(live_lot.quantity_allocated or 0)


# ===========================================================================
# 8. THE READERS — netting, and byte-identity for a job with no returns
# ===========================================================================


def _fg_lot_scenario(
    client: TestClient,
    db_session: Session,
    *,
    consume: float = 4.0,
    lot_name: str = "MRET-TRACE",
    reduce_by: float = 0.0,
):
    """A COMPLETED work order (so it has an FG lot to trace) that consumed a tie.

    Two operations: a TIED one that consumes, and an untied closer that drives the work
    order terminal and triggers the finished-goods receipt. ``reduce_by`` walks the tied
    operation's count back **while the work order is still open**, which is the honest
    ordering the plan argues for (reduce first — the count is the record — then return
    the material the lower count no longer accounts for) and the only way to open a
    ``correct_over_consumption`` allowance: both reduce verbs refuse a TERMINAL work
    order, so a correction cannot be made after the job closes.
    """
    admin = make_user(db_session, role=UserRole.ADMIN)
    operator = make_user(db_session, role=UserRole.OPERATOR)
    wc = make_work_center(db_session)
    sheet = make_sheet(db_session)
    make_inventory(db_session, sheet, qty=20, lot=lot_name, unit_cost=80.0)

    fg = make_part(db_session, standard_cost=500.0)
    wo = make_wo(db_session, fg, quantity_ordered=consume)
    op = make_op(db_session, wo, wc, sequence=10, runs=consume)
    closer = make_op(db_session, wo, wc, sequence=20, runs=consume)
    allocation = tie(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=consume)

    closed_entry(db_session, operator, op, quantity_produced=consume)
    assert complete_operation(client, operator, op, quantity=consume).status_code == status.HTTP_200_OK
    db_session.expire_all()
    assert db_session.get(WorkOrderMaterialAllocation, allocation.id).qty_consumed == consume

    if reduce_by:
        reduced = office_reduce(client, admin, op, delta=reduce_by, reason="counted the scrap tray twice")
        assert reduced.status_code == status.HTTP_200_OK, reduced.text
        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, op.id).quantity_complete == consume - reduce_by

    closed_entry(db_session, operator, closer, quantity_produced=consume)
    assert complete_operation(client, operator, closer, quantity=consume).status_code == status.HTTP_200_OK
    db_session.expire_all()

    live_wo = db_session.get(WorkOrder, wo.id)
    assert live_wo.status == WorkOrderStatus.COMPLETE, "the closer operation must drive the work order terminal"
    assert live_wo.lot_number, "a completed work order must have received its finished-goods lot"
    return admin, operator, sheet, fg, wo, op, allocation


def consumed_components(client: TestClient, user: User, wo: WorkOrder, db: Session) -> list[dict]:
    db.expire_all()
    fg_lot = db.get(WorkOrder, wo.id).lot_number
    assert fg_lot, "the work order must have received its finished-goods lot"
    resp = client.get(f"/api/v1/traceability/lot/{fg_lot}", headers=headers_for(user))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    return resp.json()["consumed_components"]


def test_traceability_nets_a_return_out_of_the_as_built_record(client: TestClient, db_session: Session):
    """A returned lot must stop being claimed as built into the part (AS9100D 8.5.2).

    Two failure modes are covered at once: reading ISSUE alone keeps claiming a sheet
    that physically went back to the rack, and adding RETURN to the type filter WITHOUT
    the sign flip would be worse — the aggregation's ``abs()`` turns a credit into MORE
    consumption. A partial return reports the NET; a full return drops the line entirely.
    """
    # The count was corrected 4 -> 2 before the job closed, so 2 is returnable.
    admin, _operator, sheet, _fg, wo, _op, allocation = _fg_lot_scenario(client, db_session, reduce_by=2.0)

    lines = consumed_components(client, admin, wo, db_session)
    assert [(c["component_part_id"], c["lot_number"], c["quantity"]) for c in lines] == [(sheet.id, "MRET-TRACE", 4.0)]

    # A PARTIAL return nets down rather than up.
    assert (
        do_return(client, admin, wo, allocation, quantity=2, intent="correct_over_consumption").status_code
        == status.HTTP_200_OK
    )
    lines = consumed_components(client, admin, wo, db_session)
    assert [(c["component_part_id"], c["quantity"]) for c in lines] == [
        (sheet.id, 2.0)
    ], "a returned quantity must be SUBTRACTED, never added"

    # A FULL return removes the line: that material was never built into the part.
    assert (
        do_return(client, admin, wo, allocation, quantity=2, intent="return_and_untie").status_code
        == status.HTTP_200_OK
    )
    lines = consumed_components(client, admin, wo, db_session)
    assert all(c["component_part_id"] != sheet.id for c in lines), lines


def _part_cogs(db: Session, part_id: int, *, start: date = None, end: date = None) -> float:
    """The per-part COGS the ``get_inventory_analytics`` bulk read produces.

    Read through the PUBLIC method so the real grouped query runs. The default window is
    deliberately longer than a year, because the method only annualizes
    (``(cogs / days) * 365``) when ``days < 365`` — so a wide window makes the number
    read back the aggregation's own answer rather than a scaled one.
    """
    start = start or date.today() - timedelta(days=400)
    end = end or date.today() + timedelta(days=1)
    response = AnalyticsService(db, COMPANY_A).get_inventory_analytics(start, end)
    matches = [row for row in response.low_turnover_items if row.part_id == part_id]
    assert matches, f"part {part_id} is not in the reported turnover rows — the assertion would be vacuous"
    return float(matches[0].cogs or 0)


def test_a_work_order_with_no_returns_reads_byte_identically_to_before(client: TestClient, db_session: Session):
    """THE protection for every existing job: no RETURN row => nothing changed.

    Every reader PR 3 touched keys its sign switch on ``transaction_type``, never on the
    stored sign, so the ISSUE leg evaluates to precisely the pre-PR-3 expression. The
    CONTROL work order here never has a return; a SUBJECT work order in the same tenant
    does, which proves the netting code is demonstrably live rather than dormant — and
    the control's fingerprint across every reader must still match a HAND-COMPUTED
    pre-PR-3 answer, so this is a real lock and not a self-comparison.
    """
    admin, _operator, sheet, _fg, control_wo, _control_op, _control_tie = _fg_lot_scenario(
        client, db_session, consume=4.0, lot_name="MRET-CONTROL"
    )

    def control_fingerprint() -> tuple:
        db_session.expire_all()
        live = db_session.get(WorkOrder, control_wo.id)
        analytics = AnalyticsService(db_session, COMPANY_A)
        return (
            # traceability — the as-built record
            sorted(
                (c["component_part_id"], c["lot_number"], c["quantity"])
                for c in consumed_components(client, admin, control_wo, db_session)
            ),
            # completion_cost_service._issued_material_cost
            round(wo_issued_material_cost(db_session, live, COMPANY_A), 6),
            # analytics_service._issued_material_cost (work-order-scoped)
            round(analytics._issued_material_cost(control_wo.id), 6),
            # analytics_service.get_inventory_analytics (the grouped per-part COGS read)
            round(_part_cogs(db_session, sheet.id), 6),
            # prediction_service._calculate_daily_usage (drives reorder points)
            round(PredictionService(db_session, COMPANY_A)._calculate_daily_usage(sheet.id), 8),
        )

    before = control_fingerprint()
    assert before[0] == [(sheet.id, "MRET-CONTROL", 4.0)]
    assert before[1] == pytest.approx(320.0)  # 4 sheets x $80
    assert before[2] == pytest.approx(320.0)
    assert before[3] == pytest.approx(320.0)
    assert before[4] == pytest.approx(4.0 / 90)

    # A SECOND, TIED work order in the same tenant DOES return material.
    subject_admin, _op_user, subject_sheet, _sfg, subject_wo, _subject_op, subject_tie = _fg_lot_scenario(
        client, db_session, consume=4.0, lot_name="MRET-SUBJECT"
    )
    assert (
        do_return(client, subject_admin, subject_wo, subject_tie, quantity=4, intent="return_and_untie").status_code
        == status.HTTP_200_OK
    )
    db_session.expire_all()
    assert any(
        row.transaction_type == TransactionType.RETURN
        for row in db_session.query(InventoryTransaction).filter(InventoryTransaction.company_id == COMPANY_A).all()
    ), "the netting path must be demonstrably live, or the control proves nothing"

    # The untouched work order's numbers are unmoved, in every reader.
    assert control_fingerprint() == before

    # …and the subject's own numbers netted all the way to zero.
    db_session.expire_all()
    assert wo_issued_material_cost(db_session, db_session.get(WorkOrder, subject_wo.id), COMPANY_A) == pytest.approx(
        0.0
    )
    assert AnalyticsService(db_session, COMPANY_A)._issued_material_cost(subject_wo.id) == pytest.approx(0.0)
    assert _part_cogs(db_session, subject_sheet.id) == pytest.approx(0.0)
    assert PredictionService(db_session, COMPANY_A)._calculate_daily_usage(subject_sheet.id) == pytest.approx(0.0)
    assert consumed_components(client, subject_admin, subject_wo, db_session) == []


def test_the_window_scoped_cogs_read_nets_a_return_out(client: TestClient, db_session: Session):
    """``_get_turnover_value`` is company-wide, so it gets its own assertion.

    It returns a RATIO (annualized COGS / average inventory value), and the denominator
    moves when material comes back on the shelf — so the discriminating assertion is the
    FULL return, where the numerator nets to exactly zero and the ratio must therefore be
    exactly 0.0 regardless of the denominator. That single value separates all three
    possible implementations: correct netting gives 0, ignoring RETURN gives the original
    ratio, and summing RETURN under the old bare ``abs()`` gives DOUBLE it.
    """
    admin, _operator, _sheet, _fg, wo, _op, allocation = _fg_lot_scenario(
        client, db_session, consume=4.0, lot_name="MRET-TURN"
    )
    analytics = AnalyticsService(db_session, COMPANY_A)
    window = (date.today() - timedelta(days=1), date.today() + timedelta(days=1))
    before = analytics._get_turnover_value(*window)
    assert before > 0, "the window must actually contain the consumption, or this proves nothing"

    assert (
        do_return(client, admin, wo, allocation, quantity=4, intent="return_and_untie").status_code
        == status.HTTP_200_OK
    )
    db_session.expire_all()
    assert (
        AnalyticsService(db_session, COMPANY_A)._get_turnover_value(*window) == 0.0
    ), "a fully returned job contributed no cost of goods sold"


def test_window_scoped_cogs_clamps_a_boundary_artifact_instead_of_going_negative(
    client: TestClient, db_session: Session
):
    """Issue OUTSIDE the window, return INSIDE it: a negative COGS is a reporting artifact.

    The two window-scoped reads clamp at zero (a negative turnover ratio is meaningless);
    the two work-order-scoped ones deliberately do NOT, because there a negative would be
    real drift worth surfacing. Pinning both halves so a future "consistency" cleanup
    cannot quietly flip either one.
    """
    admin, _operator, sheet, _fg, wo, _op, allocation = _fg_lot_scenario(
        client, db_session, consume=4.0, lot_name="MRET-WINDOW"
    )
    assert (
        do_return(client, admin, wo, allocation, quantity=4, intent="return_and_untie").status_code
        == status.HTTP_200_OK
    )
    db_session.expire_all()

    # Backdate every ISSUE out of the reporting window, leaving only the RETURN inside.
    for row in db_session.query(InventoryTransaction).filter(
        InventoryTransaction.company_id == COMPANY_A,
        InventoryTransaction.transaction_type == TransactionType.ISSUE,
    ):
        row.created_at = datetime.utcnow() - timedelta(days=30)
    db_session.commit()

    analytics = AnalyticsService(db_session, COMPANY_A)
    window = (date.today() - timedelta(days=1), date.today() + timedelta(days=1))
    assert analytics._get_turnover_value(*window) == 0.0, "a window-scoped COGS must never go negative"
    assert _part_cogs(db_session, sheet.id, start=window[0], end=window[1]) == 0.0
    assert PredictionService(db_session, COMPANY_A)._calculate_daily_usage(sheet.id) == 0.0, "usage cannot be negative"

    # The WORK-ORDER-scoped readers are unclamped by design and still net to zero here.
    assert wo_issued_material_cost(db_session, db_session.get(WorkOrder, wo.id), COMPANY_A) == pytest.approx(0.0)
    assert analytics._issued_material_cost(wo.id) == pytest.approx(0.0)


def test_a_return_is_not_counted_as_more_usage_by_the_reorder_predictor(client: TestClient, db_session: Session):
    """Counting a credit as usage makes the shop re-buy material sitting on the rack.

    Simply widening the type filter would have been WORSE than leaving RETURN out: the
    ``abs()`` in the old expression turns the credit into MORE usage. Half the
    consumption is returned, so the daily usage must HALVE — not double.
    """
    supervisor, _operator, sheet, _lot, wo, op, allocation = _consumed_scenario(client, db_session, qty=4.0)

    predictor = PredictionService(db_session, COMPANY_A)
    assert predictor._calculate_daily_usage(sheet.id) == pytest.approx(4.0 / 90)

    assert office_reduce(client, supervisor, op, delta=2).status_code == status.HTTP_200_OK
    assert (
        do_return(client, supervisor, wo, allocation, quantity=2, intent="correct_over_consumption").status_code
        == status.HTTP_200_OK
    )
    db_session.expire_all()

    assert PredictionService(db_session, COMPANY_A)._calculate_daily_usage(sheet.id) == pytest.approx(
        2.0 / 90
    ), "a return must REDUCE forecast usage, never raise it"


# ===========================================================================
# 9. Nest re-import stays refused after a full return (an owner decision)
# ===========================================================================


def test_nest_reimport_stays_refused_after_a_full_return(client: TestClient, db_session: Session):
    """Zeroing the ``qty_consumed`` cache must NOT unlock the re-import wipe.

    The guard reads the LEDGER (``ledger_backed_allocation_ids``), and after a full
    return the ISSUE **and** RETURN rows both still carry
    ``reference_type='work_order_operation'`` pointing at the operations a rebuild would
    delete. Waving it through would silently drop those rows out of job cost, analytics
    and lot genealogy (``work_order_ledger_filter`` resolves operation ids through a live
    subquery) and, on Postgres, raise an FK ``IntegrityError`` the import endpoint turns
    into a misleading 400. The remedy stays "raise a new work order" — pinned here so
    nobody "fixes" it into a cache-keyed guard later.
    """
    admin = make_user(db_session, role=UserRole.ADMIN)
    operator = make_user(db_session, role=UserRole.OPERATOR)
    wc = make_work_center(db_session)
    sheet = make_sheet(db_session)
    make_inventory(db_session, sheet, qty=10)

    wo = make_wo(db_session, make_part(db_session), quantity_ordered=3, laser=True)
    op = make_op(db_session, wo, wc, sequence=10, runs=3.0)
    make_op(db_session, wo, wc, sequence=20, runs=3.0)  # keeps the WO open
    allocation = tie(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=3.0)

    closed_entry(db_session, operator, op, quantity_produced=3)
    assert complete_operation(client, operator, op, quantity=3).status_code == status.HTTP_200_OK
    db_session.expire_all()

    # Give ALL of it back and cancel the tie: the cache reads 0 afterwards.
    assert (
        do_return(client, admin, wo, allocation, quantity=3, intent="return_and_untie").status_code
        == status.HTTP_200_OK
    )
    db_session.expire_all()
    live = db_session.get(WorkOrderMaterialAllocation, allocation.id)
    assert live.qty_consumed == 0.0 and live.status == AllocationStatus.CANCELLED

    # The ledger still names the operation, so the wipe is still refused.
    with pytest.raises(MaterialAllocationConsumedError):
        cancel_allocations_for_operations(
            db_session,
            work_order=db_session.get(WorkOrder, wo.id),
            operation_ids=[op.id],
            company_id=COMPANY_A,
            audit=AuditService(db_session, admin),
        )


# ===========================================================================
# 10. The reduce guard split — both verbs, in one place
# ===========================================================================


def test_office_corrects_a_completed_operation_while_the_operator_verb_still_refuses(
    client: TestClient, db_session: Session
):
    """The referral in ``MSG_COMPLETED_WORK`` is only honest because the office door opened.

    Before PR 3 both verbs hit the identical 409, so an operator was told to ask a
    supervisor whose own endpoint refused the same thing. Correcting finished work stays
    a supervised act — the operator's self-service verb keeps its refusal — but the
    office verb now succeeds, because the reasoned RETURN is the walk-back its refusal
    was justified by. A TERMINAL work order is still refused on BOTH.
    """
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    operator = make_user(db_session, role=UserRole.OPERATOR)
    wc = make_work_center(db_session)
    sheet = make_sheet(db_session)
    make_inventory(db_session, sheet, qty=20)

    wo = make_wo(db_session, make_part(db_session), quantity_ordered=5)
    op = make_op(db_session, wo, wc, sequence=10, runs=5.0)
    make_op(db_session, wo, wc, sequence=20, runs=5.0)  # keeps the WO open
    tie(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=5.0)

    closed_entry(db_session, operator, op, quantity_produced=5)
    assert complete_operation(client, operator, op, quantity=5).status_code == status.HTTP_200_OK
    db_session.expire_all()
    assert db_session.get(WorkOrderOperation, op.id).status == OperationStatus.COMPLETE

    # (a) the operator's self-service verb: still 409, still pointing at a supervisor.
    refused = operator_reduce(client, operator, op, delta=2)
    assert refused.status_code == status.HTTP_409_CONFLICT, refused.text
    assert "supervisor" in refused.json()["detail"]

    # (b) the office verb: succeeds, and the operation stays COMPLETE with a truthful count.
    allowed = office_reduce(client, supervisor, op, delta=2, reason="counted the scrap tray twice")
    assert allowed.status_code == status.HTTP_200_OK, allowed.text
    db_session.expire_all()
    live_op = db_session.get(WorkOrderOperation, op.id)
    assert live_op.quantity_complete == 3.0
    assert live_op.status == OperationStatus.COMPLETE, "a corrected COMPLETE operation stays COMPLETE"


@pytest.mark.parametrize("wo_status", [WorkOrderStatus.COMPLETE, WorkOrderStatus.CLOSED, WorkOrderStatus.CANCELLED])
def test_a_terminal_work_order_is_still_refused_on_both_reduce_verbs(
    client: TestClient, db_session: Session, wo_status: WorkOrderStatus
):
    """The office relaxation is scoped to a completed OPERATION on a LIVE work order."""
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    operator = make_user(db_session, role=UserRole.OPERATOR)
    wc = make_work_center(db_session)
    wo = make_wo(db_session, make_part(db_session), quantity_ordered=5, status_=wo_status)
    op = make_op(db_session, wo, wc, sequence=10, runs=5.0, quantity_complete=5.0)
    closed_entry(db_session, operator, op, quantity_produced=5)

    office = office_reduce(client, supervisor, op, delta=1, reason="job is done")
    assert office.status_code == status.HTTP_409_CONFLICT, office.text
    assert "supervisor" not in office.json()["detail"], "a terminal WO must not point at a supervisor"
    assert operator_reduce(client, operator, op, delta=1).status_code == status.HTTP_409_CONFLICT


# ===========================================================================
# 11. RBAC — and the ledger is untouched on the refused path
# ===========================================================================


@pytest.mark.parametrize("role", [UserRole.OPERATOR, UserRole.VIEWER])
def test_the_return_verb_is_refused_below_the_write_tier(client: TestClient, db_session: Session, role: UserRole):
    """Moving stock back with a reason is a bigger power than tying it, not a smaller one.

    The router's write gate is ADMIN / MANAGER / SUPERVISOR and deliberately sits outside
    the kiosk path fence. The ledger must be byte-identical after the refusal.
    """
    _supervisor, _operator, sheet, _lot, wo, _op, allocation = _consumed_scenario(client, db_session)
    intruder = make_user(db_session, role=role)

    frozen = ledger_fingerprint(db_session)
    resp = do_return(client, intruder, wo, allocation, quantity=3, intent="return_and_untie")
    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text

    db_session.expire_all()
    assert ledger_fingerprint(db_session) == frozen, "a 403 must not move stock"
    assert on_hand(db_session, sheet) == 7.0
    assert db_session.get(WorkOrderMaterialAllocation, allocation.id).qty_consumed == 3.0
    assert db_session.get(WorkOrderMaterialAllocation, allocation.id).status == AllocationStatus.OPEN


def test_the_consumption_read_is_open_to_any_authenticated_tenant_user(client: TestClient, db_session: Session):
    """It discloses ledger facts about material this company already owns, and a return
    dialog that cannot show them would ask for a confirmation nobody could give."""
    _supervisor, _operator, _sheet, lot, wo, _op, allocation = _consumed_scenario(client, db_session)
    viewer = make_user(db_session, role=UserRole.VIEWER)

    resp = client.get(consumption_url(wo, allocation), headers=headers_for(viewer))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert [(line["inventory_item_id"], line["net"]) for line in resp.json()] == [(lot.id, 3.0)]

    # …but not to another tenant.
    _ensure_company(db_session, COMPANY_B)
    intruder = make_user(db_session, role=UserRole.ADMIN, company_id=COMPANY_B)
    assert (
        client.get(consumption_url(wo, allocation), headers=headers_for(intruder)).status_code
        == status.HTTP_404_NOT_FOUND
    )


# ===========================================================================
# 12. PATCH qty_per_run now guards over-consumption
# ===========================================================================


def test_lowering_qty_per_run_under_consumed_material_is_422(client: TestClient, db_session: Session):
    """The operation-scoped twin of the ``qty_planned`` guard, and it was missing.

    ``qty_per_run`` is the number the engine actually consumes against, so lowering it
    toward zero under posted consumption rewrites the plan beneath the ledger AND opens
    an unbounded ``correct_over_consumption`` allowance on a tie that stays OPEN — the
    exact middle ground the two named intents exist to close.
    """
    supervisor, _operator, _sheet, _lot, wo, op, allocation = _consumed_scenario(client, db_session, qty=3.0)
    url = f"/api/v1/work-orders/{wo.id}/material-allocations/{allocation.id}"

    resp = client.patch(url, headers=headers_for(supervisor), json={"qty_per_run": 0.5})
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, resp.text
    detail = resp.json()["detail"]
    assert "qty_per_run cannot be lowered" in detail, detail
    assert "3 run(s) recorded" in detail, detail

    db_session.expire_all()
    assert db_session.get(WorkOrderMaterialAllocation, allocation.id).qty_per_run == 1.0

    # RAISING it is allowed — that reduces the gap this guard exists to prevent.
    assert (
        client.patch(url, headers=headers_for(supervisor), json={"qty_per_run": 2.0}).status_code == status.HTTP_200_OK
    )
    db_session.expire_all()
    assert db_session.get(WorkOrderMaterialAllocation, allocation.id).qty_per_run == 2.0

    # And lowering back to a rate the consumption still covers is fine.
    assert (
        client.patch(url, headers=headers_for(supervisor), json={"qty_per_run": 1.0}).status_code == status.HTTP_200_OK
    )

    # The plan guard it is twinned with is unchanged.
    lowered_plan = client.patch(url, headers=headers_for(supervisor), json={"qty_planned": 1.0})
    assert lowered_plan.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, lowered_plan.text
    assert "Return the over-consumed material first" in lowered_plan.json()["detail"]


def test_qty_per_run_is_unguarded_when_nothing_was_consumed(client: TestClient, db_session: Session):
    """The guard is about protecting POSTED consumption, not about freezing the plan."""
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    wc = make_work_center(db_session)
    sheet = make_sheet(db_session)
    make_inventory(db_session, sheet, qty=10)
    wo = make_wo(db_session, make_part(db_session), quantity_ordered=3)
    op = make_op(db_session, wo, wc, sequence=10, runs=3.0, quantity_complete=3.0)
    allocation = tie(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=3.0)

    resp = client.patch(
        f"/api/v1/work-orders/{wo.id}/material-allocations/{allocation.id}",
        headers=headers_for(supervisor),
        json={"qty_per_run": 0.25},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text


# ===========================================================================
# 13. AllocationStatus.CLOSED is still never written
# ===========================================================================


def test_no_return_path_ever_writes_closed(client: TestClient, db_session: Session):
    """A ``return_and_untie`` cancels; nothing in ``app/`` writes ``CLOSED``.

    Deliberate: a CLOSED tie would vanish from ``_drop_allocation_covered_parts`` and let
    the BOM backflush double-issue the same part once ``backflush_components`` is exposed.
    """
    supervisor, _operator, _sheet, _lot, wo, op, allocation = _consumed_scenario(client, db_session)

    assert office_reduce(client, supervisor, op, delta=1).status_code == status.HTTP_200_OK
    assert (
        do_return(client, supervisor, wo, allocation, quantity=1, intent="correct_over_consumption").status_code
        == status.HTTP_200_OK
    )
    assert (
        do_return(client, supervisor, wo, allocation, quantity=2, intent="return_and_untie").status_code
        == status.HTTP_200_OK
    )
    db_session.expire_all()

    assert db_session.get(WorkOrderMaterialAllocation, allocation.id).status == AllocationStatus.CANCELLED
    assert (
        db_session.query(WorkOrderMaterialAllocation)
        .filter(WorkOrderMaterialAllocation.status == AllocationStatus.CLOSED)
        .count()
        == 0
    )


def test_a_cancelled_tie_still_answers_the_consumption_read(client: TestClient, db_session: Session):
    """A consumed-then-cancelled tie is exactly what an operator most needs to look at."""
    supervisor, _operator, _sheet, lot, wo, _op, allocation = _consumed_scenario(client, db_session)
    assert (
        do_return(client, supervisor, wo, allocation, quantity=3, intent="return_and_untie").status_code
        == status.HTTP_200_OK
    )

    resp = client.get(consumption_url(wo, allocation), headers=headers_for(supervisor))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert [(line["inventory_item_id"], line["issued"], line["returned"], line["net"]) for line in resp.json()] == [
        (lot.id, 3.0, 3.0, 0.0)
    ]


def test_untie_now_names_return_and_untie_as_the_remedy(client: TestClient, db_session: Session):
    """The 409 must point at a verb that EXISTS — it used to say "reverse consumption first"."""
    supervisor, _operator, _sheet, _lot, wo, _op, allocation = _consumed_scenario(client, db_session)

    resp = client.delete(
        f"/api/v1/work-orders/{wo.id}/material-allocations/{allocation.id}",
        headers=headers_for(supervisor),
    )
    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
    detail = resp.json()["detail"]
    assert "return_and_untie" in detail, detail
    assert "Reverse consumption first" not in detail, detail
