"""PR 2.5 behavior locks: tied material depletes when an OPERATION completes.

Through PRs 1 and 2 the only depletion moment was WORK-ORDER completion, so a laser
child work order carrying one operation per nest moved no stock at all until the last
nest closed. PR 2.5 adds a second, narrower seam --
``apply_operation_completion_inventory_effects`` ->
``consume_tied_materials_for_operation`` -- wired at the four
``finalize_operation_completion`` call sites (kiosk clock-out, shop-floor operation
complete, office operation complete, force-complete).

What this file pins, in the order the plan (docs/MATERIAL_CONSUMPTION_PLAN.md) argues
it:

1. **The headline.** Completing nest 1 of 3 deducts nest 1's sheet AND NOTHING ELSE --
   the operations that did not complete must not move stock.
2. **Replay.** The whole-work-order reconcile at work-order completion is now the
   SELF-HEAL behind the new trigger; it must compute ``delta == 0`` and post nothing
   for a tie the operation-level call already drained. This is the property that makes
   two triggers safe together.
3. **Invariant 6(d), the most important test here.** An UNTIED work order driven
   through operation completion on ALL FOUR handlers writes zero: no
   ``InventoryTransaction``, no allocation audit row, no ``material_consumption``
   event -- and its full audit footprint equals a control run in a world where the
   allocation table is empty.
4. **A terminal work order never consumes** through any of the four paths.
5. **Production reporting is deliberately NOT a trigger** (a safety property: an
   ``IN_PROGRESS`` operation is still reducible and consumption never auto-reverses).
6. **Reduce-after-consume stays a no-op** (invariant 6(b)) -- ordinary now that
   material posts per operation, rare when it posted once at the end.
7. **Force-complete consumes nothing** -- the documented residual, pinned so it is a
   recorded decision rather than a surprise.
8. **Reconcile-on-read does not consume** on an operation-only transition.
9. ``AllocationStatus.CLOSED`` is still never written.
10. The clock-out and shop-floor-complete audit rows now carry ``ip_address`` /
    ``user_agent``, because both handlers stopped shadowing their injected
    request-scoped ``AuditService``.
11. **RBAC.** ``POST /work-orders/operations/{id}/complete`` is gated to the
    work-order-edit tier now that completing an operation moves stock.
12. **Invariant 4.** A ``StaleDataError`` inside the engine surfaces as the handler's
    409 -- never as a 200 that silently skipped a material deduction.
"""

from datetime import date, datetime, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.core.security import create_access_token
from app.db.ledger_filter import OPERATION_REFERENCE_TYPE
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.inventory import InventoryItem, InventoryTransaction
from app.models.operational_event import OperationalEvent
from app.models.part import Part
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus, WorkOrderType
from app.models.work_order_material import AllocationSource, AllocationStatus, WorkOrderMaterialAllocation
from app.services.audit_service import AuditService
from app.services.completion_inventory_service import apply_operation_completion_inventory_effects
from app.services.material_consumption_service import consume_tied_materials_for_work_order

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"
_seq = {"n": 0}

# The four operation-completion handlers PR 2.5 wired. Named here once so every
# multi-handler test below covers exactly the same set and a fifth handler cannot be
# added without this list going stale in a visible way.
HANDLERS = ("clock_out", "shop_floor_complete", "office_complete", "force_complete")


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


# ---------------------------------------------------------------------------
# Fixtures (deliberately local; the sibling suite's helpers are private to it)
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
        email=f"mcop-{n}@co{company_id}.test",
        employee_id=f"MCOP-{n:05d}",
        first_name="Op",
        last_name="Trigger",
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
        part_number=f"MCOP-P-{n}",
        name=f"Part {n}",
        description="operation-trigger fixture part",
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
        name=f"MCOP-WC-{n}",
        code=f"MCOP-WC-{n}",
        work_center_type="laser",
        description="operation-trigger fixture work center",
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
    laser: bool = True,
    company_id: int = COMPANY_A,
) -> WorkOrder:
    """A work order that defaults to the LASER dispatch-pool shape.

    Laser is the default because it is the feature's headline case AND because a
    dispatch-pool work order is exempt from the predecessor gate
    (``is_laser_dispatch_work_order``): nests complete in any order, which is exactly
    what "finish nest 2 before nest 1" tests need.
    """
    n = _next()
    wo = WorkOrder(
        work_order_number=f"MCOP-WO-{n:05d}",
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
    runs: float = 2.0,
    quantity_complete: float = 0,
    quantity_scrapped: float = 0,
    status_: OperationStatus = OperationStatus.IN_PROGRESS,
    company_id: int = COMPANY_A,
) -> WorkOrderOperation:
    """One nest / routing step. ``runs`` becomes ``component_quantity`` = its target."""
    op = WorkOrderOperation(
        work_order_id=wo.id,
        work_center_id=wc.id,
        sequence=sequence,
        operation_number=f"OP{sequence}",
        name=f"Nest {sequence}",
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
    unit_cost: float = 75.0,
    company_id: int = COMPANY_A,
) -> InventoryItem:
    item = InventoryItem(
        part_id=part.id,
        location="RAW-A",
        warehouse="MAIN",
        quantity_on_hand=qty,
        quantity_allocated=0.0,
        quantity_available=qty,
        lot_number=lot or f"MCOP-LOT-{_next():05d}",
        unit_cost=unit_cost,
        status="available",
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
    qty_planned: float = 2.0,
    company_id: int = COMPANY_A,
) -> WorkOrderMaterialAllocation:
    allocation = WorkOrderMaterialAllocation(
        company_id=company_id,
        work_order_id=wo.id,
        work_order_operation_id=operation.id if operation is not None else None,
        part_id=part.id,
        source=AllocationSource.NEST,
        status=AllocationStatus.OPEN,
        qty_per_run=qty_per_run if operation is not None else None,
        qty_planned=qty_planned,
        unit_of_measure=getattr(part.unit_of_measure, "value", part.unit_of_measure) or "each",
        qty_consumed=0.0,
    )
    db.add(allocation)
    db.commit()
    db.refresh(allocation)
    return allocation


def clock_in_entry(db: Session, user: User, operation: WorkOrderOperation) -> TimeEntry:
    entry = TimeEntry(
        user_id=user.id,
        work_order_id=operation.work_order_id,
        operation_id=operation.id,
        work_center_id=operation.work_center_id,
        entry_type=TimeEntryType.RUN,
        clock_in=datetime.utcnow() - timedelta(minutes=30),
        company_id=user.company_id,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


# ---------------------------------------------------------------------------
# Observation helpers
# ---------------------------------------------------------------------------


def consumption_txns(db: Session, op_id: int, *, company_id: int = COMPANY_A) -> list[InventoryTransaction]:
    return (
        db.query(InventoryTransaction)
        .filter(
            InventoryTransaction.company_id == company_id,
            InventoryTransaction.reference_type == OPERATION_REFERENCE_TYPE,
            InventoryTransaction.reference_id == op_id,
        )
        .order_by(InventoryTransaction.id)
        .all()
    )


def consumed_quantities(db: Session, op_id: int) -> list[float]:
    return [t.quantity for t in consumption_txns(db, op_id)]


def ledger_fingerprint(db: Session, *, company_id: int = COMPANY_A) -> list[tuple]:
    """Every ledger row for a tenant, normalized for structural comparison.

    Ids and timestamps are excluded so two structurally identical runs compare equal;
    ``allocation_id`` is INCLUDED so a tied run can never masquerade as an untied one.
    """
    return sorted(
        (
            t.part_id,
            t.transaction_type.value,
            t.quantity,
            t.reference_type,
            t.reference_id,
            t.lot_number,
            t.allocation_id,
        )
        for t in db.query(InventoryTransaction).filter(InventoryTransaction.company_id == company_id).all()
    )


def wo_ledger_rows(db: Session, wo: WorkOrder, *, company_id: int = COMPANY_A) -> list[InventoryTransaction]:
    """EVERY ledger row belonging to one work order, under BOTH reference types."""
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
                    InventoryTransaction.reference_type == "work_order",
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


def wo_ledger_fingerprint(db: Session, wo: WorkOrder, *, company_id: int = COMPANY_A) -> list[tuple]:
    """A PER-WORK-ORDER ledger fingerprint, scrubbed of identity.

    Two structurally identical work orders differ only in identity, so the WO number,
    the finished-good lot derived from it, and the WO's own part id are replaced with
    placeholders. Everything describing the MOVEMENT is compared verbatim.
    """

    def scrub(value):
        if isinstance(value, str):
            return value.replace(wo.work_order_number, "<WO>")
        return value

    return sorted(
        (
            "<FG>" if t.part_id == wo.part_id else t.part_id,
            t.transaction_type.value,
            t.quantity,
            t.reference_type,
            scrub(t.reference_number),
            scrub(t.lot_number),
            t.allocation_id,
            t.unit_cost,
            t.total_cost,
        )
        for t in wo_ledger_rows(db, wo, company_id=company_id)
    )


def wo_audit_fingerprint(db: Session, wo: WorkOrder, *, company_id: int = COMPANY_A) -> list[tuple]:
    """(action, resource_type) for every audit row that NAMES this work order."""
    return sorted(
        (row.action, row.resource_type)
        for row in db.query(AuditLog)
        .filter(AuditLog.company_id == company_id, AuditLog.description.contains(wo.work_order_number))
        .all()
    )


def allocation_audit_count(db: Session, *, company_id: int = COMPANY_A) -> int:
    return (
        db.query(AuditLog)
        .filter(
            AuditLog.company_id == company_id,
            AuditLog.resource_type == "work_order_material_allocation",
        )
        .count()
    )


def consumption_event_count(db: Session, *, company_id: int = COMPANY_A) -> int:
    return (
        db.query(OperationalEvent)
        .filter(
            OperationalEvent.company_id == company_id,
            OperationalEvent.source_module == "material_consumption",
        )
        .count()
    )


def on_hand(db: Session, part: Part, *, company_id: int = COMPANY_A) -> float:
    return sum(
        float(row.quantity_on_hand or 0)
        for row in db.query(InventoryItem)
        .filter(InventoryItem.company_id == company_id, InventoryItem.part_id == part.id)
        .all()
    )


# ---------------------------------------------------------------------------
# Handler drivers -- one per PR 2.5 call site
# ---------------------------------------------------------------------------


def drive_clock_out(client: TestClient, db: Session, user: User, op: WorkOrderOperation, *, quantity: float):
    """Kiosk clock-out (``shop_floor.py`` :1631)."""
    entry = clock_in_entry(db, user, op)
    return client.post(
        f"/api/v1/shop-floor/clock-out/{entry.id}",
        headers=headers_for(user),
        json={"quantity_produced": quantity, "quantity_scrapped": 0},
    )


def drive_shop_floor_complete(client: TestClient, db: Session, user: User, op: WorkOrderOperation, *, quantity: float):
    """Shop-floor operation complete (``shop_floor.py`` :3959)."""
    return client.post(
        f"/api/v1/shop-floor/operations/{op.id}/complete",
        headers=headers_for(user),
        json={"quantity_complete": quantity},
    )


def drive_office_complete(client: TestClient, db: Session, user: User, op: WorkOrderOperation, *, quantity: float):
    """Office operation complete (``work_orders.py`` :4422). Query-param verb."""
    return client.post(
        f"/api/v1/work-orders/operations/{op.id}/complete?quantity_complete={quantity}",
        headers=headers_for(user),
    )


def drive_force_complete(client: TestClient, db: Session, user: User, op: WorkOrderOperation, *, quantity: float):
    """Privileged force-complete of the whole work order (``work_orders.py`` :3502)."""
    return client.post(
        f"/api/v1/work-orders/{op.work_order_id}/complete?quantity_complete={quantity}",
        headers=headers_for(user),
    )


DRIVERS = {
    "clock_out": drive_clock_out,
    "shop_floor_complete": drive_shop_floor_complete,
    "office_complete": drive_office_complete,
    "force_complete": drive_force_complete,
}


# ---------------------------------------------------------------------------
# 1. The headline: one nest completes, one nest's sheets leave
# ---------------------------------------------------------------------------


def test_completing_one_nest_consumes_only_that_nests_sheets(client: TestClient, db_session: Session):
    """Nest 1 of 3 completes -> nest 1's sheets move, nests 2 and 3 move NOTHING.

    This is the ask docs/MATERIAL_CONSUMPTION_PLAN.md mis-stated as shipped for two
    PRs. The negative half is the load-bearing half: before PR 2.5 completing nest 1
    moved nothing, and a naive fix (reconcile the WHOLE work order at operation
    completion) would move all three -- consuming against operations that are still
    IN_PROGRESS and therefore still reducible.
    """
    user = make_user(db_session)
    wc = make_work_center(db_session)
    fg = make_part(db_session)
    sheet = make_sheet(db_session)
    make_inventory(db_session, sheet, qty=50)

    wo = make_wo(db_session, fg, quantity_ordered=6)
    nests = [make_op(db_session, wo, wc, sequence=10 * (i + 1), runs=2.0) for i in range(3)]
    ties = [tie(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=2.0) for op in nests]

    # --- nest 1 closes -------------------------------------------------------
    response = drive_shop_floor_complete(client, db_session, user, nests[0], quantity=2)
    assert response.status_code == status.HTTP_200_OK, response.text
    db_session.expire_all()

    assert consumed_quantities(db_session, nests[0].id) == [-2.0]
    assert consumed_quantities(db_session, nests[1].id) == [], "nest 2 must not move stock before it completes"
    assert consumed_quantities(db_session, nests[2].id) == [], "nest 3 must not move stock before it completes"
    assert [db_session.get(WorkOrderMaterialAllocation, t.id).qty_consumed for t in ties] == [2.0, 0.0, 0.0]
    assert on_hand(db_session, sheet) == 48.0

    # The ISSUE carries the tie's genealogy key and the operation as its reference.
    row = consumption_txns(db_session, nests[0].id)[0]
    assert row.allocation_id == ties[0].id
    assert row.reference_type == OPERATION_REFERENCE_TYPE
    assert row.reference_id == nests[0].id

    # --- nests 2 and 3 close, each posting only its own ----------------------
    for op in nests[1:]:
        response = drive_shop_floor_complete(client, db_session, user, op, quantity=2)
        assert response.status_code == status.HTTP_200_OK, response.text
    db_session.expire_all()

    assert consumed_quantities(db_session, nests[0].id) == [-2.0], "nest 1 must not post a second time"
    assert consumed_quantities(db_session, nests[1].id) == [-2.0]
    assert consumed_quantities(db_session, nests[2].id) == [-2.0]
    assert [db_session.get(WorkOrderMaterialAllocation, t.id).qty_consumed for t in ties] == [2.0, 2.0, 2.0]
    assert on_hand(db_session, sheet) == 44.0


def test_nests_may_complete_out_of_order(client: TestClient, db_session: Session):
    """A dispatch pool has no run order -- nest 3 first must deduct nest 3's sheet."""
    user = make_user(db_session)
    wc = make_work_center(db_session)
    sheet = make_sheet(db_session)
    make_inventory(db_session, sheet, qty=20)

    wo = make_wo(db_session, make_part(db_session), quantity_ordered=6)
    nests = [make_op(db_session, wo, wc, sequence=10 * (i + 1), runs=2.0) for i in range(3)]
    ties = [tie(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=2.0) for op in nests]

    response = drive_shop_floor_complete(client, db_session, user, nests[2], quantity=2)
    assert response.status_code == status.HTTP_200_OK, response.text
    db_session.expire_all()

    assert consumed_quantities(db_session, nests[2].id) == [-2.0]
    assert consumed_quantities(db_session, nests[0].id) == []
    assert consumed_quantities(db_session, nests[1].id) == []
    assert db_session.get(WorkOrderMaterialAllocation, ties[2].id).qty_consumed == 2.0


def test_scrapped_runs_consume_at_operation_completion(client: TestClient, db_session: Session):
    """``target`` scales on (complete + scrapped): a scrapped sheet was still cut."""
    user = make_user(db_session)
    wc = make_work_center(db_session)
    sheet = make_sheet(db_session)
    make_inventory(db_session, sheet, qty=20)

    wo = make_wo(db_session, make_part(db_session), quantity_ordered=3)
    op = make_op(db_session, wo, wc, runs=3.0, quantity_scrapped=1.0)
    allocation = tie(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=4.0)

    response = drive_shop_floor_complete(client, db_session, user, op, quantity=3)
    assert response.status_code == status.HTTP_200_OK, response.text
    db_session.expire_all()

    # 3 good + 1 scrapped = 4 sheets, posted as ISSUE (never SCRAP) so lot genealogy
    # -- which filters on ISSUE -- still sees the material most likely to be audited.
    assert consumed_quantities(db_session, op.id) == [-4.0]
    assert consumption_txns(db_session, op.id)[0].transaction_type.value == "issue"
    assert db_session.get(WorkOrderMaterialAllocation, allocation.id).qty_consumed == 4.0


# ---------------------------------------------------------------------------
# 2. Replay: the whole-WO reconcile is the SELF-HEAL, and posts nothing twice
# ---------------------------------------------------------------------------


def test_work_order_completion_reconcile_posts_nothing_after_operation_consumed(
    client: TestClient, db_session: Session
):
    """The property that makes TWO triggers safe: the second pass sees ``delta == 0``.

    Operation 1's tie drains at operation completion. Operation 2 then closes the work
    order, which runs ``apply_completion_inventory_effects`` -- the WHOLE-work-order
    reconcile over every open operation-scoped tie, operation 1's included. Sum-delta
    recomputes ``target`` from live operation state, so it must post nothing.

    Asserted three ways: the ledger is unchanged, the cached ``qty_consumed`` is
    unchanged, and a THIRD, direct invocation of the whole-WO engine returns an empty
    ``consumed_allocation_ids`` (delta == 0, not "posted a compensating row").
    """
    user = make_user(db_session)
    wc = make_work_center(db_session)
    sheet = make_sheet(db_session)
    make_inventory(db_session, sheet, qty=20)

    wo = make_wo(db_session, make_part(db_session), quantity_ordered=4)
    op1 = make_op(db_session, wo, wc, sequence=10, runs=2.0)
    op2 = make_op(db_session, wo, wc, sequence=20, runs=2.0)
    allocation = tie(db_session, wo, sheet, operation=op1, qty_per_run=1.0, qty_planned=2.0)

    assert drive_shop_floor_complete(client, db_session, user, op1, quantity=2).status_code == status.HTTP_200_OK
    db_session.expire_all()
    after_operation = ledger_fingerprint(db_session)
    assert consumed_quantities(db_session, op1.id) == [-2.0]
    assert db_session.get(WorkOrderMaterialAllocation, allocation.id).qty_consumed == 2.0

    # Closing op2 drives the WO COMPLETE -> the whole-WO reconcile runs.
    assert drive_shop_floor_complete(client, db_session, user, op2, quantity=2).status_code == status.HTTP_200_OK
    db_session.expire_all()
    assert db_session.get(WorkOrder, wo.id).status == WorkOrderStatus.COMPLETE

    assert consumed_quantities(db_session, op1.id) == [-2.0], "the self-heal must not re-post op1's tie"
    assert db_session.get(WorkOrderMaterialAllocation, allocation.id).qty_consumed == 2.0
    assert on_hand(db_session, sheet) == 18.0
    # The only ledger rows added by the WO completion are the FG receipt -- nothing
    # under the operation reference type.
    added = [row for row in ledger_fingerprint(db_session) if row not in after_operation]
    assert all(row[3] != OPERATION_REFERENCE_TYPE for row in added), added

    # A third pass, straight at the whole-WO engine: delta == 0, nothing consumed.
    result = consume_tied_materials_for_work_order(
        db_session,
        db_session.get(WorkOrder, wo.id),
        user_id=user.id,
        company_id=COMPANY_A,
        audit=AuditService(db_session, user),
    )
    db_session.commit()
    db_session.expire_all()
    assert result.consumed_allocation_ids == []
    assert result.transactions == []
    assert result.failed_allocation_ids == []
    assert consumed_quantities(db_session, op1.id) == [-2.0]


def test_a_tie_the_operation_seam_missed_still_flushes_at_work_order_completion(
    client: TestClient, db_session: Session
):
    """The self-heal must still HEAL -- a tie created after the operation closed.

    Without this, "the whole-WO reconcile is now the self-heal" would be an untested
    claim, and test 2 above would pass just as well if the reconcile had been removed.
    """
    user = make_user(db_session)
    wc = make_work_center(db_session)
    sheet = make_sheet(db_session)
    make_inventory(db_session, sheet, qty=20)

    wo = make_wo(db_session, make_part(db_session), quantity_ordered=4)
    op1 = make_op(db_session, wo, wc, sequence=10, runs=2.0)
    op2 = make_op(db_session, wo, wc, sequence=20, runs=2.0)

    # op1 completes UNTIED -- nothing to consume.
    assert drive_shop_floor_complete(client, db_session, user, op1, quantity=2).status_code == status.HTTP_200_OK
    db_session.expire_all()
    assert consumed_quantities(db_session, op1.id) == []

    # The tie is created afterwards, pointing at the already-complete operation.
    allocation = tie(db_session, wo, sheet, operation=op1, qty_per_run=1.0, qty_planned=2.0)

    assert drive_shop_floor_complete(client, db_session, user, op2, quantity=2).status_code == status.HTTP_200_OK
    db_session.expire_all()

    assert consumed_quantities(db_session, op1.id) == [-2.0], "the whole-WO reconcile must catch it"
    assert db_session.get(WorkOrderMaterialAllocation, allocation.id).qty_consumed == 2.0


# ---------------------------------------------------------------------------
# 3. Invariant 6(d): an UNTIED work order is byte-identical on all four handlers
# ---------------------------------------------------------------------------


def _scenario(db: Session, user: User, wc: WorkCenter, *, handler: str):
    """A work order shaped so ``handler`` can complete ONE operation on it.

    Two operations for the three per-operation handlers, so completing the first does
    NOT complete the work order and the observation isolates the operation-completion
    effect. Force-complete is work-order-scoped by nature, so it gets one operation and
    its fingerprint legitimately includes the finished-goods receipt -- which is fine,
    because control and subject are compared to each other, not to zero.

    Force-complete uses a PRODUCTION work order: a laser dispatch-pool WO deliberately
    books NO finished-goods receipt any more (its ``quantity_complete`` counts nest
    runs, not product -- the phantom-FG fix), which would leave this handler's control
    fingerprint empty and defeat the "the FG receipt legitimately posts" channel. The
    three per-operation handlers keep the laser shape (dispatch pools are exempt from
    the predecessor gate, which is what lets one operation complete in isolation).
    """
    wo = make_wo(db, make_part(db, standard_cost=9.0), quantity_ordered=4, laser=(handler != "force_complete"))
    op1 = make_op(db, wo, wc, sequence=10, runs=2.0)
    if handler != "force_complete":
        make_op(db, wo, wc, sequence=20, runs=2.0)
    return wo, op1


@pytest.mark.parametrize("handler", HANDLERS)
def test_untied_operation_completion_is_byte_identical(client: TestClient, db_session: Session, handler: str):
    """An UNTIED work order must write NOTHING extra on any of the four handlers.

    The comparison is against a CONTROL that ran while ``work_order_material_
    allocations`` was provably EMPTY -- the pre-feature world -- and the SUBJECT runs
    only after a TIED work order has demonstrably consumed through the SAME handler in
    the same tenant and session. So the engine is live and populated when the subject
    runs, and the test cannot pass by never reaching the new code.

    Three channels are compared, because invariant 6(d) covers three: the ledger, the
    tamper-evident audit chain, and the operational-event stream.
    """
    user = make_user(db_session)
    wc = make_work_center(db_session)
    drive = DRIVERS[handler]

    # --- CONTROL: the allocation table is empty ------------------------------
    assert db_session.query(WorkOrderMaterialAllocation).count() == 0, "the control must run pre-feature"
    control_wo, control_op = _scenario(db_session, user, wc, handler=handler)
    before_control = ledger_fingerprint(db_session)
    response = drive(client, db_session, user, control_op, quantity=2)
    assert response.status_code == status.HTTP_200_OK, response.text
    db_session.expire_all()
    control_ledger = wo_ledger_fingerprint(db_session, control_wo)
    control_audit = wo_audit_fingerprint(db_session, control_wo)
    control_ledger_delta = [row for row in ledger_fingerprint(db_session) if row not in before_control]
    if handler == "force_complete":
        # Work-order-scoped by nature: the finished-goods receipt legitimately posts,
        # so control and subject are compared against a NON-empty expectation.
        assert control_ledger_delta, "force-complete must still receive the finished good"
    else:
        # Completing ONE operation of a still-open work order moves nothing at all --
        # the FG receipt is work-order-scoped and deliberately did not move earlier.
        assert control_ledger_delta == []

    # --- the engine is demonstrably LIVE on this very handler ----------------
    sheet = make_sheet(db_session)
    make_inventory(db_session, sheet, qty=50)
    tied_wo, tied_op = _scenario(db_session, user, wc, handler=handler)
    if handler == "force_complete":
        # Force-complete never writes ``operation.quantity_complete`` (the documented
        # residual, pinned in its own test below), so the only way it can consume is
        # an operation that already carries produced quantity from a partial report.
        tied_op.quantity_complete = 2.0
        db_session.commit()
    tied_allocation = tie(db_session, tied_wo, sheet, operation=tied_op, qty_per_run=1.0, qty_planned=2.0)
    response = drive(client, db_session, user, tied_op, quantity=2)
    assert response.status_code == status.HTTP_200_OK, response.text
    db_session.expire_all()
    assert consumed_quantities(db_session, tied_op.id) == [-2.0], f"{handler}: the engine must really consume here"
    assert db_session.get(WorkOrderMaterialAllocation, tied_allocation.id).qty_consumed == 2.0

    # --- SUBJECT: structurally identical to the control, still untied --------
    subject_wo, subject_op = _scenario(db_session, user, wc, handler=handler)
    before_subject_ledger = ledger_fingerprint(db_session)
    before_subject_alloc_audit = allocation_audit_count(db_session)
    before_subject_events = consumption_event_count(db_session)

    response = drive(client, db_session, user, subject_op, quantity=2)
    assert response.status_code == status.HTTP_200_OK, response.text
    db_session.expire_all()

    subject_ledger_delta = [row for row in ledger_fingerprint(db_session) if row not in before_subject_ledger]

    # (a) the ledger. ``wo_ledger_fingerprint`` is the byte-identity comparison: it
    # scrubs only IDENTITY (the WO number, its derived finished-good lot, its own part
    # id) and compares the MOVEMENT verbatim -- type, signed quantity, reference shape,
    # ``allocation_id``, unit and total cost. The delta is compared by COUNT rather than
    # value because a raw fingerprint row carries unscrubbed part / reference ids that
    # differ between two structurally identical work orders by construction.
    assert wo_ledger_fingerprint(db_session, subject_wo) == control_ledger
    assert len(subject_ledger_delta) == len(control_ledger_delta)
    assert all(row[3] != OPERATION_REFERENCE_TYPE for row in subject_ledger_delta)
    assert all(row[6] is None for row in subject_ledger_delta)

    # (b) the tamper-evident chain: same actions, and not one allocation row added
    assert wo_audit_fingerprint(db_session, subject_wo) == control_audit
    assert allocation_audit_count(db_session) == before_subject_alloc_audit

    # (c) the event stream
    assert consumption_event_count(db_session) == before_subject_events
    assert (
        db_session.query(OperationalEvent)
        .filter(
            OperationalEvent.company_id == COMPANY_A,
            OperationalEvent.work_order_id.in_([control_wo.id, subject_wo.id]),
            OperationalEvent.source_module == "material_consumption",
        )
        .count()
        == 0
    )

    # No ledger row anywhere on either untied work order references an allocation.
    for wo in (control_wo, subject_wo):
        assert all(row.allocation_id is None for row in wo_ledger_rows(db_session, wo)), wo.work_order_number


# ---------------------------------------------------------------------------
# 4. A terminal work order never consumes, on any path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("handler", HANDLERS)
@pytest.mark.parametrize(
    "terminal_status",
    [WorkOrderStatus.COMPLETE, WorkOrderStatus.CLOSED, WorkOrderStatus.CANCELLED],
)
def test_terminal_work_order_never_consumes(
    client: TestClient, db_session: Session, handler: str, terminal_status: WorkOrderStatus
):
    """Finished or cancelled work never moves material, whichever verb is used.

    The handlers refuse in different ways on purpose and the test does NOT pin one
    shared status code: clock-out ALWAYS closes the operator's time entry (a 409 would
    strand it forever) and simply skips every completion side effect; the two
    operation-complete twins 409; force-complete no-ops on COMPLETE/CLOSED and 409s on
    CANCELLED. What must be identical across all of them is the ledger: nothing moves.
    """
    user = make_user(db_session)
    wc = make_work_center(db_session)
    sheet = make_sheet(db_session)
    make_inventory(db_session, sheet, qty=20)

    wo = make_wo(db_session, make_part(db_session), quantity_ordered=2, status_=terminal_status)
    op = make_op(db_session, wo, wc, runs=2.0)
    allocation = tie(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=2.0)

    before = ledger_fingerprint(db_session)
    response = DRIVERS[handler](client, db_session, user, op, quantity=2)
    assert response.status_code != status.HTTP_500_INTERNAL_SERVER_ERROR, response.text
    db_session.expire_all()

    assert consumed_quantities(db_session, op.id) == []
    assert db_session.get(WorkOrderMaterialAllocation, allocation.id).qty_consumed == 0.0
    assert ledger_fingerprint(db_session) == before
    assert on_hand(db_session, sheet) == 20.0


# ---------------------------------------------------------------------------
# 5. Production reporting is deliberately NOT a consumption trigger
# ---------------------------------------------------------------------------


def test_production_reporting_is_not_a_consumption_trigger(client: TestClient, db_session: Session):
    """Reporting 3 of 6 runs on a still-open nest deducts NOTHING. Deliberate.

    This is a SAFETY property, not an omission, so it is pinned rather than left
    implicit. An ``IN_PROGRESS`` operation is still REDUCIBLE --
    ``production_reduction_service`` only refuses a walk-back once the operation is
    COMPLETE -- and consumption never auto-reverses, with no RETURN verb until PR 3.
    Consuming here would let a supervisor's legitimate over-count correction strand
    material nothing can give back.

    The same operation is then completed, and the sheets move: the test therefore
    distinguishes "reporting does not consume" from "this fixture never consumes".
    """
    user = make_user(db_session)
    wc = make_work_center(db_session)
    sheet = make_sheet(db_session)
    make_inventory(db_session, sheet, qty=20)

    wo = make_wo(db_session, make_part(db_session), quantity_ordered=6)
    op = make_op(db_session, wo, wc, runs=6.0)
    allocation = tie(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=6.0)
    clock_in_entry(db_session, user, op)

    before = ledger_fingerprint(db_session)
    response = client.post(
        f"/api/v1/shop-floor/operations/{op.id}/production",
        headers=headers_for(user),
        json={"quantity_complete_delta": 3, "quantity_scrapped_delta": 0},
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    db_session.expire_all()

    assert db_session.get(WorkOrderOperation, op.id).quantity_complete == 3.0, "the report must really have landed"
    assert db_session.get(WorkOrderOperation, op.id).status == OperationStatus.IN_PROGRESS
    assert consumed_quantities(db_session, op.id) == []
    assert db_session.get(WorkOrderMaterialAllocation, allocation.id).qty_consumed == 0.0
    assert ledger_fingerprint(db_session) == before
    assert on_hand(db_session, sheet) == 20.0

    # ... and the SAME operation moves stock the moment it completes.
    assert drive_shop_floor_complete(client, db_session, user, op, quantity=6).status_code == status.HTTP_200_OK
    db_session.expire_all()
    assert consumed_quantities(db_session, op.id) == [-6.0]


# ---------------------------------------------------------------------------
# 6. Reduce-after-consume stays a no-op (invariant 6(b))
# ---------------------------------------------------------------------------


def test_reduce_after_operation_consumption_is_a_no_op(client: TestClient, db_session: Session):
    """Walking a count back after material posted moves NO stock, by either verb.

    Three layers, because three things could go wrong:

    * The OPERATOR's reduce endpoint refuses a COMPLETE operation with 409 -- that
      refusal is the reason the consumption trigger is scoped to one completed
      operation, so it is asserted here rather than assumed.
    * The OFFICE verb now SUCCEEDS on a COMPLETE operation (PR 3 relaxed it, because the
      reasoned RETURN verb is the walk-back its refusal was justified by) -- and it must
      still move no stock. Lowering the count is what OPENS a return allowance; it never
      posts a reversal itself.
    * Even so, the engine itself must no-op on a negative delta. The operation's
      quantity is walked back directly and the NEW per-operation seam re-run, which is
      the code path that did not exist before PR 2.5: ``delta <= 0`` is a no-op, never
      an automatic reversal.
    """
    user = make_user(db_session)
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    wc = make_work_center(db_session)
    sheet = make_sheet(db_session)
    make_inventory(db_session, sheet, qty=20)

    wo = make_wo(db_session, make_part(db_session), quantity_ordered=6)
    op = make_op(db_session, wo, wc, sequence=10, runs=4.0)
    make_op(db_session, wo, wc, sequence=20, runs=2.0)  # keeps the WO open
    allocation = tie(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=4.0)

    entry = clock_in_entry(db_session, user, op)
    response = client.post(
        f"/api/v1/shop-floor/clock-out/{entry.id}",
        headers=headers_for(user),
        json={"quantity_produced": 4, "quantity_scrapped": 0},
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    db_session.expire_all()
    assert consumed_quantities(db_session, op.id) == [-4.0]

    frozen_ledger = ledger_fingerprint(db_session)
    frozen_consumed = db_session.get(WorkOrderMaterialAllocation, allocation.id).qty_consumed

    # (a) the operator verb -- refused: a COMPLETE operation is reduce-immune
    operator_response = client.post(
        f"/api/v1/shop-floor/operations/{op.id}/reduce-production",
        headers=headers_for(user),
        json={"quantity_delta": 2, "reason": "double-scanned the tray"},
    )
    assert operator_response.status_code == status.HTTP_409_CONFLICT, operator_response.text

    # (b) the supervisor/office verb -- ALLOWED since PR 3, and it still moves no stock.
    # Correcting the count is what opens the bounded return allowance; the material
    # itself only comes back through the explicit, reasoned RETURN verb.
    office_response = client.post(
        f"/api/v1/work-orders/operations/{op.id}/reduce-production",
        headers=headers_for(supervisor),
        json={"quantity_delta": 2, "reason": "double-scanned the tray"},
    )
    assert office_response.status_code == status.HTTP_200_OK, office_response.text

    db_session.expire_all()
    assert db_session.get(WorkOrderOperation, op.id).quantity_complete == 2.0
    assert ledger_fingerprint(db_session) == frozen_ledger, "a reduction must never post a reversal"
    assert db_session.get(WorkOrderMaterialAllocation, allocation.id).qty_consumed == frozen_consumed

    # (c) even with the quantity forced down, the NEW seam never auto-reverses
    live_op = db_session.get(WorkOrderOperation, op.id)
    live_op.quantity_complete = 1.0
    db_session.commit()
    apply_operation_completion_inventory_effects(
        db_session,
        db_session.get(WorkOrder, wo.id),
        live_op,
        user_id=user.id,
        company_id=COMPANY_A,
        audit=AuditService(db_session, user),
    )
    db_session.commit()
    db_session.expire_all()

    assert ledger_fingerprint(db_session) == frozen_ledger, "a negative delta must never post a reversal"
    assert db_session.get(WorkOrderMaterialAllocation, allocation.id).qty_consumed == frozen_consumed
    assert on_hand(db_session, sheet) == 16.0


# ---------------------------------------------------------------------------
# 7. Force-complete consumes nothing -- the documented residual
# ---------------------------------------------------------------------------


def test_force_complete_consumes_nothing_because_target_is_zero(client: TestClient, db_session: Session):
    """Pinned so the residual is a RECORDED DECISION rather than a field surprise.

    ``complete_work_order`` force-closes every still-open operation but never writes
    ``operation.quantity_complete``, and ``finalize_operation_completion`` does not
    write it either. So the engine runs and computes
    ``target = qty_per_run * (0 complete + 0 scrapped) = 0`` and posts no ledger row.

    The second half of the test is what makes the first half meaningful: the identical
    force-complete on an operation that DOES carry produced quantity from an earlier
    partial report consumes normally. The zero above is therefore about a quantity
    nobody recorded -- not about the seam being unwired.
    """
    user = make_user(db_session)
    wc = make_work_center(db_session)
    sheet = make_sheet(db_session)
    make_inventory(db_session, sheet, qty=20)

    # (a) nothing reported -> nothing consumed
    wo = make_wo(db_session, make_part(db_session), quantity_ordered=2)
    op = make_op(db_session, wo, wc, runs=2.0)
    allocation = tie(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=2.0)

    response = drive_force_complete(client, db_session, user, op, quantity=2)
    assert response.status_code == status.HTTP_200_OK, response.text
    db_session.expire_all()

    assert db_session.get(WorkOrder, wo.id).status == WorkOrderStatus.COMPLETE
    assert db_session.get(WorkOrderOperation, op.id).status == OperationStatus.COMPLETE
    assert db_session.get(WorkOrderOperation, op.id).quantity_complete == 0.0
    assert consumed_quantities(db_session, op.id) == []
    assert db_session.get(WorkOrderMaterialAllocation, allocation.id).qty_consumed == 0.0

    # (b) the same verb on an operation carrying an earlier partial report DOES consume
    wo2 = make_wo(db_session, make_part(db_session), quantity_ordered=2)
    op2 = make_op(db_session, wo2, wc, runs=2.0, quantity_complete=2.0)
    allocation2 = tie(db_session, wo2, sheet, operation=op2, qty_per_run=1.0, qty_planned=2.0)

    response = drive_force_complete(client, db_session, user, op2, quantity=2)
    assert response.status_code == status.HTTP_200_OK, response.text
    db_session.expire_all()

    assert consumed_quantities(db_session, op2.id) == [-2.0]
    assert db_session.get(WorkOrderMaterialAllocation, allocation2.id).qty_consumed == 2.0


# ---------------------------------------------------------------------------
# 8. Reconcile-on-read does not consume on an operation-only transition
# ---------------------------------------------------------------------------


def test_reconcile_on_read_does_not_consume_for_an_operation_only_transition(client: TestClient, db_session: Session):
    """A GET that flips an OPERATION complete must not move stock.

    ``_apply_reconcile_inventory_effects`` keys only on ``resource_type ==
    "work_order"`` transitions, so an operation completed by the read-time reconcile
    waits for work-order completion. Deliberate, on the rule this feature has held
    since PR 1: a read has no actor, no intent and no reason to record, and the
    never-auto-reverse posture makes an unattributable deduction expensive.

    The work order stays open (a second operation is still running), the operation is
    asserted to have ACTUALLY flipped COMPLETE on the read, and the ledger is unmoved.
    """
    user = make_user(db_session)
    wc = make_work_center(db_session)
    sheet = make_sheet(db_session)
    make_inventory(db_session, sheet, qty=20)

    wo = make_wo(db_session, make_part(db_session), quantity_ordered=4)
    op1 = make_op(db_session, wo, wc, sequence=10, runs=2.0)
    make_op(db_session, wo, wc, sequence=20, runs=2.0)
    allocation = tie(db_session, wo, sheet, operation=op1, qty_per_run=1.0, qty_planned=2.0)

    # Durable CLOSED labor evidence at target, with the operation still IN_PROGRESS --
    # the shape the read-time reconcile completes from.
    entry = clock_in_entry(db_session, user, op1)
    entry.clock_out = datetime.utcnow()
    entry.duration_hours = 0.5
    entry.quantity_produced = 2.0
    db_session.commit()

    before = ledger_fingerprint(db_session)
    response = client.get(f"/api/v1/work-orders/{wo.id}", headers=headers_for(user))
    assert response.status_code == status.HTTP_200_OK, response.text
    db_session.expire_all()

    assert (
        db_session.get(WorkOrderOperation, op1.id).status == OperationStatus.COMPLETE
    ), "the reconcile must really have completed the operation, or this proves nothing"
    assert db_session.get(WorkOrder, wo.id).status != WorkOrderStatus.COMPLETE
    assert consumed_quantities(db_session, op1.id) == []
    assert db_session.get(WorkOrderMaterialAllocation, allocation.id).qty_consumed == 0.0
    assert ledger_fingerprint(db_session) == before
    assert on_hand(db_session, sheet) == 20.0


# ---------------------------------------------------------------------------
# 9. AllocationStatus.CLOSED is still never written
# ---------------------------------------------------------------------------


def test_fully_consumed_tie_stays_open(client: TestClient, db_session: Session):
    """A fully drained tie ends ``open`` with ``qty_consumed == qty_planned``.

    ``AllocationStatus.CLOSED`` exists in the model for a later PR and nothing writes
    it. Moving consumption earlier did not change that, and a consumer reading
    ``status`` as "is there anything left" would be wrong either way.
    """
    user = make_user(db_session)
    wc = make_work_center(db_session)
    sheet = make_sheet(db_session)
    make_inventory(db_session, sheet, qty=20)

    wo = make_wo(db_session, make_part(db_session), quantity_ordered=2)
    op = make_op(db_session, wo, wc, runs=2.0)
    allocation = tie(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=2.0)

    assert drive_shop_floor_complete(client, db_session, user, op, quantity=2).status_code == status.HTTP_200_OK
    db_session.expire_all()

    live = db_session.get(WorkOrderMaterialAllocation, allocation.id)
    assert live.qty_consumed == live.qty_planned == 2.0
    assert live.status == AllocationStatus.OPEN
    assert (
        db_session.query(WorkOrderMaterialAllocation)
        .filter(WorkOrderMaterialAllocation.status == AllocationStatus.CLOSED)
        .count()
        == 0
    )


# ---------------------------------------------------------------------------
# 10. The clock-out audit rows now carry request context
# ---------------------------------------------------------------------------


def test_clock_out_completion_audit_rows_carry_request_context(client: TestClient, db_session: Session):
    """The de-shadowed ``AuditService`` puts ip_address / user_agent on these rows.

    ``clock_out`` used to build its own ``AuditService(db, current_user)`` inside the
    completion branch, shadowing the ``Depends(get_audit_service)`` instance the
    handler already injects. The shadow carried no ``Request``, so every audit row
    written in that branch -- the operation and work-order completion status changes,
    the finished-goods receipt, the backflush, the cost rollup -- stored NULL
    ``ip_address`` and NULL ``user_agent``, silently contradicting the comment two
    dozen lines below that claims the request-scoped service is used precisely so the
    row captures them.

    ``ip_address`` is also an input to ``compute_audit_hash``, so the change moves what
    is hashed for these rows; the chain stays verifiable because the hash is computed
    over the value that is stored. Nothing else about the rows changes: ``AuditService``
    holds no other per-instance state, and the company tag resolves identically with or
    without a request.
    """
    user = make_user(db_session)
    wc = make_work_center(db_session)
    wo = make_wo(db_session, make_part(db_session), quantity_ordered=2, laser=False)
    op = make_op(db_session, wo, wc, runs=2.0)

    entry = clock_in_entry(db_session, user, op)
    response = client.post(
        f"/api/v1/shop-floor/clock-out/{entry.id}",
        headers=headers_for(user),
        json={"quantity_produced": 2, "quantity_scrapped": 0},
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    db_session.expire_all()
    assert db_session.get(WorkOrder, wo.id).status == WorkOrderStatus.COMPLETE

    rows = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.company_id == COMPANY_A,
            AuditLog.description.contains(wo.work_order_number),
        )
        .all()
    )
    completion_rows = [row for row in rows if row.resource_type in ("work_order", "work_order_operation")]
    assert completion_rows, "the clock-out completion must write status-change rows"
    for row in completion_rows:
        assert row.ip_address, f"{row.action}/{row.resource_type} lost its request context"
        assert row.user_agent, f"{row.action}/{row.resource_type} lost its request context"

    # The finished-goods receipt is written by apply_completion_inventory_effects with
    # the SAME injected service, so it must carry the context too.
    receipt_rows = [row for row in rows if row.resource_type == "inventory"]
    for row in receipt_rows:
        assert row.ip_address
        assert row.user_agent


def test_clock_out_consumption_and_completion_rows_share_one_audit_identity(client: TestClient, db_session: Session):
    """Consumption and completion rows come from ONE service, not two.

    The consumption call sits BEFORE the completion audit branch, so the shadow meant
    the two halves of a single clock-out were written by two different
    ``AuditService`` instances with different request context. They must now agree.
    """
    user = make_user(db_session)
    wc = make_work_center(db_session)
    sheet = make_sheet(db_session)
    make_inventory(db_session, sheet, qty=20)

    wo = make_wo(db_session, make_part(db_session), quantity_ordered=2)
    op = make_op(db_session, wo, wc, runs=2.0)
    tie(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=2.0)

    entry = clock_in_entry(db_session, user, op)
    response = client.post(
        f"/api/v1/shop-floor/clock-out/{entry.id}",
        headers=headers_for(user),
        json={"quantity_produced": 2, "quantity_scrapped": 0},
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    db_session.expire_all()
    assert consumed_quantities(db_session, op.id) == [-2.0]

    consumption_rows = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.company_id == COMPANY_A,
            AuditLog.resource_type == "work_order_material_allocation",
        )
        .all()
    )
    assert consumption_rows, "the consumption must be on the tamper-evident chain"
    completion_rows = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.company_id == COMPANY_A,
            AuditLog.resource_type == "work_order_operation",
            AuditLog.description.contains(wo.work_order_number),
        )
        .all()
    )
    assert completion_rows

    contexts = {(row.ip_address, row.user_agent) for row in consumption_rows + completion_rows}
    assert len(contexts) == 1, f"one clock-out wrote rows with mixed request context: {contexts}"
    ip_address, user_agent = contexts.pop()
    assert ip_address and user_agent


def test_shop_floor_operation_complete_audit_rows_carry_request_context(client: TestClient, db_session: Session):
    """The same de-shadowing, applied to ``clock_out``'s twin.

    ``shop_floor.complete_operation`` built its own ``AuditService(db, current_user)``
    for exactly the same reason and with exactly the same consequence: the operation's
    completion row, and everything the work-order-completion block writes after it,
    stored NULL ``ip_address`` / ``user_agent``. The handler now injects
    ``Depends(get_audit_service)`` and threads it through, so the consumption rows this
    endpoint newly writes and the completion rows it has always written share ONE
    attribution identity on the hash chain.
    """
    user = make_user(db_session)
    wc = make_work_center(db_session)
    sheet = make_sheet(db_session)
    make_inventory(db_session, sheet, qty=20)

    wo = make_wo(db_session, make_part(db_session), quantity_ordered=2)
    op = make_op(db_session, wo, wc, runs=2.0)
    tie(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=2.0)

    assert drive_shop_floor_complete(client, db_session, user, op, quantity=2).status_code == status.HTTP_200_OK
    db_session.expire_all()
    assert consumed_quantities(db_session, op.id) == [-2.0]

    rows = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.company_id == COMPANY_A,
            AuditLog.resource_type.in_(("work_order", "work_order_operation", "work_order_material_allocation")),
        )
        .all()
    )
    rows = [row for row in rows if wo.work_order_number in (row.description or "")]
    assert rows, "the completion must write to the tamper-evident chain"
    contexts = {(row.ip_address, row.user_agent) for row in rows}
    assert len(contexts) == 1, f"one completion wrote rows with mixed request context: {contexts}"
    ip_address, user_agent = contexts.pop()
    assert ip_address and user_agent


# ---------------------------------------------------------------------------
# 11. RBAC: the office operation-complete verb now moves stock, so it is gated
# ---------------------------------------------------------------------------

# ``POST /work-orders/operations/{id}/complete`` was open to ANY authenticated tenant
# user. That was already too loose; it became load-bearing the moment operation
# completion started depleting inventory, because a VIEWER could then decrement stock
# and write ledger + hash-chain rows from a page they were only meant to read.
#
# The allowed set deliberately matches ``complete_work_order`` -- the larger sibling
# that completes EVERY operation on the work order -- not ``reduce-production``.
# Excluding QUALITY would produce the incoherent state where a Quality user may
# complete a whole work order but not one of its operations. reduce-production is
# stricter for a reason that does not apply here: it rewrites other operators'
# recorded labor.
OFFICE_COMPLETE_ALLOWED = (UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR, UserRole.QUALITY)
OFFICE_COMPLETE_DENIED = (UserRole.VIEWER, UserRole.SHIPPING, UserRole.OPERATOR)


@pytest.mark.parametrize("role", OFFICE_COMPLETE_DENIED)
def test_office_operation_complete_is_refused_below_the_work_order_edit_tier(
    client: TestClient, db_session: Session, role: UserRole
):
    """403 -- and, because the refusal is what protects the ledger, NOTHING moves.

    A status code alone would be a weak assertion here: the point of the gate is that
    an unauthorized caller cannot decrement stock, so the ledger, the tie cache, the
    operation status and the allocation audit channel are all asserted untouched.
    """
    user = make_user(db_session, role=role)
    wc = make_work_center(db_session)
    sheet = make_sheet(db_session)
    make_inventory(db_session, sheet, qty=20)

    wo = make_wo(db_session, make_part(db_session), quantity_ordered=2)
    op = make_op(db_session, wo, wc, runs=2.0)
    allocation = tie(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=2.0)

    before = ledger_fingerprint(db_session)
    before_allocation_audit = allocation_audit_count(db_session)

    response = client.post(
        f"/api/v1/work-orders/operations/{op.id}/complete?quantity_complete=2",
        headers=headers_for(user),
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN, response.text
    db_session.expire_all()

    assert consumed_quantities(db_session, op.id) == []
    assert db_session.get(WorkOrderMaterialAllocation, allocation.id).qty_consumed == 0.0
    assert db_session.get(WorkOrderOperation, op.id).status == OperationStatus.IN_PROGRESS
    assert ledger_fingerprint(db_session) == before
    assert allocation_audit_count(db_session) == before_allocation_audit
    assert on_hand(db_session, sheet) == 20.0


@pytest.mark.parametrize("role", OFFICE_COMPLETE_ALLOWED)
def test_office_operation_complete_is_allowed_for_the_work_order_edit_tier(
    client: TestClient, db_session: Session, role: UserRole
):
    """Each allowed role gets through AND actually moves the material.

    Asserting the consumption rather than just the 200 is what keeps the gate honest in
    both directions: a gate tightened to the point where a legitimate completer is
    refused would fail here, and QUALITY is in this list precisely because the earlier
    draft that omitted it was caught by a completion test rather than by inspection.
    """
    user = make_user(db_session, role=role)
    wc = make_work_center(db_session)
    sheet = make_sheet(db_session)
    make_inventory(db_session, sheet, qty=20)

    wo = make_wo(db_session, make_part(db_session), quantity_ordered=2)
    op = make_op(db_session, wo, wc, runs=2.0)
    allocation = tie(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=2.0)

    response = client.post(
        f"/api/v1/work-orders/operations/{op.id}/complete?quantity_complete=2",
        headers=headers_for(user),
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    db_session.expire_all()

    assert consumed_quantities(db_session, op.id) == [-2.0]
    assert db_session.get(WorkOrderMaterialAllocation, allocation.id).qty_consumed == 2.0


def test_an_operator_still_completes_and_consumes_through_the_shop_floor_verb(client: TestClient, db_session: Session):
    """The gate must not take work away from the floor -- the half that would hurt.

    An OPERATOR is refused the office verb (above) and that is correct, but it is only
    correct because the shop-floor endpoint is how operators complete work. If the two
    facts were ever to come apart, the floor would lose the ability to close an
    operation at all. Both halves are asserted in one test so they cannot drift.
    """
    operator = make_user(db_session, role=UserRole.OPERATOR)
    wc = make_work_center(db_session)
    sheet = make_sheet(db_session)
    make_inventory(db_session, sheet, qty=20)

    wo = make_wo(db_session, make_part(db_session), quantity_ordered=2)
    op = make_op(db_session, wo, wc, runs=2.0)
    allocation = tie(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=2.0)

    refused = client.post(
        f"/api/v1/work-orders/operations/{op.id}/complete?quantity_complete=2",
        headers=headers_for(operator),
    )
    assert refused.status_code == status.HTTP_403_FORBIDDEN, refused.text

    allowed = drive_shop_floor_complete(client, db_session, operator, op, quantity=2)
    assert allowed.status_code == status.HTTP_200_OK, allowed.text
    db_session.expire_all()

    assert consumed_quantities(db_session, op.id) == [-2.0]
    assert db_session.get(WorkOrderMaterialAllocation, allocation.id).qty_consumed == 2.0


# ---------------------------------------------------------------------------
# 12. An optimistic-lock conflict must surface as a 409, never a silent skip
# ---------------------------------------------------------------------------


def test_lock_conflict_during_consumption_surfaces_as_409_not_a_silent_skip(
    client: TestClient, db_session: Session, monkeypatch
):
    """A ``StaleDataError`` inside the engine is RE-RAISED, not degraded (invariant 4).

    Every other failure in the per-allocation savepoint is a local problem worth
    recording and stepping over. An optimistic-lock conflict is not: it says another
    transaction moved this work order underneath us, so the operation quantities the
    consumption was computed from are stale. Degrading it would turn the handler's
    documented 409 into a **200 that silently skipped a material deduction** -- the
    operator sees success, stock never moves, and the only trace is one audit row
    nobody is watching.

    The observable contract asserted here is the whole point: the caller gets a 409,
    and NOTHING landed -- no ledger row, no advanced ``qty_consumed``, no
    ``ALLOCATION_CONSUMPTION_FAILED`` row, and the operation is still IN_PROGRESS
    rather than completed-but-unconsumed.
    """
    from app.services import material_consumption_service as mcs

    user = make_user(db_session)
    wc = make_work_center(db_session)
    sheet = make_sheet(db_session)
    make_inventory(db_session, sheet, qty=20)

    wo = make_wo(db_session, make_part(db_session), quantity_ordered=2)
    op = make_op(db_session, wo, wc, runs=2.0)
    allocation = tie(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=2.0)

    def _stale(*args, **kwargs):
        raise StaleDataError("simulated concurrent version bump")

    monkeypatch.setattr(mcs, "_consume_one_allocation", _stale)

    response = client.post(
        f"/api/v1/shop-floor/operations/{op.id}/complete",
        headers=headers_for(user),
        json={"quantity_complete": 2},
    )
    assert response.status_code == status.HTTP_409_CONFLICT, response.text

    # The handler raised mid-transaction; the app-wide handler only builds the
    # response, so the test session is still holding the aborted unit of work.
    db_session.rollback()
    db_session.expire_all()

    assert consumed_quantities(db_session, op.id) == []
    assert db_session.get(WorkOrderMaterialAllocation, allocation.id).qty_consumed == 0.0
    assert db_session.get(WorkOrderOperation, op.id).status == OperationStatus.IN_PROGRESS
    assert on_hand(db_session, sheet) == 20.0
    assert (
        db_session.query(AuditLog)
        .filter(
            AuditLog.company_id == COMPANY_A,
            AuditLog.action == "ALLOCATION_CONSUMPTION_FAILED",
        )
        .count()
        == 0
    ), "a lock conflict is a 409, not a degraded failure record"


def test_a_lock_conflict_on_the_tie_READ_is_re_raised_too(client: TestClient, db_session: Session, monkeypatch):
    """The read is OUTSIDE every savepoint, so degrading it left no record at all.

    ``_open_allocations_for_operation`` runs before the per-allocation savepoint exists.
    A conflict raised by its autoflush is caught by the entry point's own guard, which
    means the degraded path would have produced NO audit row whatsoever -- just a log
    line -- and a completion that silently consumed nothing. That is strictly worse than
    the shortage case, which at least writes to the hash chain.

    This is also what makes the guarantee structural rather than conventional: the four
    call sites each ``db.flush()`` first so the conflict surfaces there instead, but a
    fifth that forgets must still fail loudly.
    """
    from app.services import material_consumption_service as mcs

    user = make_user(db_session)
    wc = make_work_center(db_session)
    sheet = make_sheet(db_session)
    make_inventory(db_session, sheet, qty=20)

    wo = make_wo(db_session, make_part(db_session), quantity_ordered=2)
    op = make_op(db_session, wo, wc, runs=2.0)
    tie(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=2.0)

    def _stale(*args, **kwargs):
        raise StaleDataError("simulated conflict on the tie read's autoflush")

    monkeypatch.setattr(mcs, "_open_allocations_for_operation", _stale)

    response = client.post(
        f"/api/v1/shop-floor/operations/{op.id}/complete",
        headers=headers_for(user),
        json={"quantity_complete": 2},
    )
    assert response.status_code == status.HTTP_409_CONFLICT, response.text

    db_session.rollback()
    db_session.expire_all()
    assert consumed_quantities(db_session, op.id) == []
    assert db_session.get(WorkOrderOperation, op.id).status == OperationStatus.IN_PROGRESS


def test_a_non_lock_failure_still_degrades_into_a_recorded_failure(
    client: TestClient, db_session: Session, monkeypatch
):
    """The CONTRAST that makes the two tests above mean something.

    Without this, "a StaleDataError produces a 409" would be indistinguishable from
    "any exception produces a 409", and the read-safety posture the engine is built on
    -- degrade one allocation, never break a completion or a reconcile-on-read GET --
    would be silently untested at this seam.

    A plain failure still completes the operation, still writes the
    ``ALLOCATION_CONSUMPTION_FAILED`` row on the tamper-evident chain, and still moves
    no stock.

    A SECOND operation keeps the work order open on purpose. On a single-operation work
    order this completion would also close the WO and run the whole-work-order
    reconcile, which attempts the same allocation again and correctly records a SECOND
    failure row -- true, but it would blur which seam the count is measuring. Two
    operations isolate the per-operation trigger to exactly one attempt.
    """
    from app.services import material_consumption_service as mcs

    user = make_user(db_session)
    wc = make_work_center(db_session)
    sheet = make_sheet(db_session)
    make_inventory(db_session, sheet, qty=20)

    wo = make_wo(db_session, make_part(db_session), quantity_ordered=4)
    op = make_op(db_session, wo, wc, sequence=10, runs=2.0)
    make_op(db_session, wo, wc, sequence=20, runs=2.0)
    allocation = tie(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=2.0)

    def _boom(*args, **kwargs):
        raise RuntimeError("ledger insert exploded")

    monkeypatch.setattr(mcs, "_consume_one_allocation", _boom)

    response = client.post(
        f"/api/v1/shop-floor/operations/{op.id}/complete",
        headers=headers_for(user),
        json={"quantity_complete": 2},
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    db_session.expire_all()

    assert db_session.get(WorkOrderOperation, op.id).status == OperationStatus.COMPLETE
    assert consumed_quantities(db_session, op.id) == []
    assert db_session.get(WorkOrderMaterialAllocation, allocation.id).qty_consumed == 0.0
    failed_rows = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.company_id == COMPANY_A,
            AuditLog.action == "ALLOCATION_CONSUMPTION_FAILED",
        )
        .all()
    )
    assert len(failed_rows) == 1
    assert failed_rows[0].integrity_hash, "on the tamper-evident chain, not just a log line"
    assert failed_rows[0].success == "false"
