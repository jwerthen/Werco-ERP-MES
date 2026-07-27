"""Behavior locks for the material-consumption engine and its tie API.

The single most important assertion in this file is
``test_untied_work_order_transaction_set_is_identical``: an UNTIED work order must
complete with byte-identical inventory movement to its pre-feature behavior. Everything
else in the feature is opt-in on top of that.

Also covered:
- consume-on-complete (the laser-nest headline: 1 sheet per completed run)
- incremental consumption across sessions (sum-delta reconcile-to-target)
- idempotent replay (target recomputed => second call is a no-op)
- scrap consumes, posted as ISSUE (never SCRAP) so genealogy still sees it
- never auto-reverse on a negative delta (the reduce-over-count case)
- backflush skip-precedence (no double-issue for an allocation-covered part)
- WO-scoped ties and BOM demand posting as SEPARATE attributed rows under
  reference_type='work_order_backflush' (PR 4.4 — they used to be summed into the one
  ISSUE uq_wo_inventory_issue allowed; the TOTAL is unchanged)
- quantity_available stays == quantity_on_hand - quantity_allocated
- FIFO spill across lots + lot pinning
- shortage: negative on-hand, ALLOCATION_SHORTAGE audit row, warning event
- tenant isolation of consumption and of every tie endpoint
- traceability sees reference_type='work_order_operation' consumption
- lifecycle: nest re-import guard, WO soft-delete auto-cancel
"""

from datetime import date, datetime, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.db.ledger_filter import (
    BACKFLUSH_REFERENCE_TYPE,
    OPERATION_REFERENCE_TYPE,
    WORK_ORDER_ID_KEYED_REFERENCE_TYPES,
    WORK_ORDER_REFERENCE_TYPE,
)
from app.models.audit_log import AuditLog
from app.models.bom import BOM, BOMItem
from app.models.company import Company
from app.models.inventory import InventoryItem, InventoryTransaction, TransactionType
from app.models.operational_event import OperationalEvent
from app.models.part import Part
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus, WorkOrderType
from app.models.work_order_material import AllocationSource, AllocationStatus, WorkOrderMaterialAllocation
from app.services.analytics_service import AnalyticsService
from app.services.audit_service import AuditService
from app.services.completion_cost_service import _issued_material_cost
from app.services.completion_inventory_service import (
    BACKFLUSH_SHORTAGE_AUDIT_ACTION,
    apply_completion_inventory_effects,
)
from app.services.material_consumption_service import (
    ALLOCATION_CONSUMPTION_FAILED_AUDIT_ACTION,
    ALLOCATION_SHORTAGE_AUDIT_ACTION,
    ALLOCATION_SHORTAGE_EVENT_TYPE,
    HELD_MATERIAL_CONSUMED_AUDIT_ACTION,
    MaterialAllocationConsumedError,
    cancel_allocations_for_operations,
    cancel_open_allocations_for_work_order,
    consume_tied_materials_for_work_order,
)
from tests.api.fk_test_helpers import sqlite_foreign_keys_enforced

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2
TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
        email=f"mc-{n}@co{company_id}.test",
        employee_id=f"MC-{n:05d}",
        first_name="Mat",
        last_name="Con",
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
    backflush: bool = False,
    standard_cost: float = 5.0,
    uom: str = "each",
    part_type: str = "manufactured",
    company_id: int = COMPANY_A,
) -> Part:
    _ensure_company(db, company_id)
    n = _next()
    part = Part(
        part_number=f"MC-P-{n}",
        name=f"Part {n}",
        description="material-consumption fixture part",
        part_type=part_type,
        unit_of_measure=uom,
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
        name=f"MC-WC-{n}",
        code=f"MC-WC-{n}",
        work_center_type="laser",
        description="material-consumption fixture work center",
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
        work_order_number=f"MC-WO-{n:05d}",
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
    db.commit()
    db.refresh(wo)
    return wo


def make_op(
    db: Session,
    wo: WorkOrder,
    wc: WorkCenter,
    *,
    sequence: int = 10,
    quantity_complete: float = 0,
    quantity_scrapped: float = 0,
    operation_group: str = None,
    company_id: int = COMPANY_A,
) -> WorkOrderOperation:
    op = WorkOrderOperation(
        work_order_id=wo.id,
        work_center_id=wc.id,
        sequence=sequence,
        operation_number=f"OP{sequence}",
        name=f"Op {sequence}",
        operation_group=operation_group,
        status=OperationStatus.IN_PROGRESS,
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
    qty: float,
    lot: str,
    location: str = "RAW-A",
    unit_cost: float = 2.0,
    received_date: datetime = None,
    allocated: float = 0.0,
    company_id: int = COMPANY_A,
) -> InventoryItem:
    item = InventoryItem(
        part_id=part.id,
        location=location,
        warehouse="MAIN",
        quantity_on_hand=qty,
        quantity_allocated=allocated,
        quantity_available=qty - allocated,
        lot_number=lot,
        unit_cost=unit_cost,
        received_date=received_date,
        status="available",
        is_active=True,
        company_id=company_id,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def make_allocation(
    db: Session,
    wo: WorkOrder,
    part: Part,
    *,
    operation: WorkOrderOperation = None,
    qty_per_run: float = 1.0,
    qty_planned: float = 10.0,
    pinned: InventoryItem = None,
    status_: AllocationStatus = AllocationStatus.OPEN,
    qty_consumed: float = 0.0,
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
        pinned_inventory_item_id=pinned.id if pinned is not None else None,
        pinned_lot_number=pinned.lot_number if pinned is not None else None,
    )
    db.add(allocation)
    db.commit()
    db.refresh(allocation)
    return allocation


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


def all_txn_fingerprints(db: Session, *, company_id: int = COMPANY_A) -> list[tuple]:
    """A comparable fingerprint of every ledger row for a company.

    Deliberately excludes ids/timestamps so two structurally identical runs compare
    equal, and INCLUDES ``allocation_id`` so a tied run could never look untied.
    """
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
        )
        for t in db.query(InventoryTransaction).filter(InventoryTransaction.company_id == company_id).all()
    )


def work_order_ledger_rows(db: Session, wo: WorkOrder, *, company_id: int = COMPANY_A) -> list[InventoryTransaction]:
    """EVERY ledger row belonging to one work order, under ALL THREE reference shapes.

    ``work_order`` (FG receipt + legacy one-shot rows), ``work_order_backflush`` (the
    reconciling component leg) and ``work_order_operation`` (per-run tie consumption).
    The backflush shape is here for the sake of the invariant-6(d) fingerprint below: a
    reader that knew only the two pre-4.4 shapes would go BLIND to a
    ``work_order_backflush`` row a regression started writing on an untied work order,
    and would then pass by failing to look.
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


def work_order_fingerprint(db: Session, wo: WorkOrder, *, company_id: int = COMPANY_A) -> list[tuple]:
    """A PER-WORK-ORDER ledger fingerprint, normalized for structural comparison.

    Two structurally identical work orders differ only in identity, so the WO number,
    the finished-good lot derived from it, and the WO's own part id are replaced with
    placeholders. Everything that describes the MOVEMENT — type, signed quantity,
    reference shape, locations, costs and ``allocation_id`` — is compared verbatim, so a
    tied run could never masquerade as an untied one.
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
            t.from_location,
            t.to_location,
            t.allocation_id,
            t.unit_cost,
            t.total_cost,
        )
        for t in work_order_ledger_rows(db, wo, company_id=company_id)
    )


def work_order_audit_fingerprint(db: Session, wo: WorkOrder, *, company_id: int = COMPANY_A) -> list[tuple]:
    """(action, resource_type) for every audit row that NAMES this work order.

    The ledger is only one of the three channels invariant 6(d) covers; this is the
    audit one. An untied work order must put nothing extra on the hash chain.
    """
    return sorted(
        (row.action, row.resource_type)
        for row in db.query(AuditLog)
        .filter(AuditLog.company_id == company_id, AuditLog.description.contains(wo.work_order_number))
        .all()
    )


def run_effects(db: Session, wo: WorkOrder, user: User, *, company_id: int = COMPANY_A) -> None:
    """Invoke the real completion entry point the five call sites share."""
    audit = AuditService(db, user)
    apply_completion_inventory_effects(db, wo, user_id=user.id, company_id=company_id, audit=audit)
    db.commit()


# ---------------------------------------------------------------------------
# THE untied-behavior lock
# ---------------------------------------------------------------------------


def test_untied_work_order_transaction_set_is_identical(db_session: Session):
    """An untied WO must produce EXACTLY the transaction set it produced before.

    Two structurally identical, untied work orders are ACTUALLY COMPLETED through the
    same entry point and their per-work-order fingerprints compared:

      * the CONTROL completes while ``work_order_material_allocations`` is empty — the
        pre-feature world;
      * the SUBJECT completes after a third, TIED work order has been completed in the
        same tenant and session, so the consumption engine is demonstrably live and has
        posted real rows (asserted, so the comparison can never be vacuous).

    Their ledger fingerprints must be identical, their audit footprints must be
    identical, and neither may carry an allocation-referencing ledger row, an allocation
    audit row, or an allocation event. This test is the CMMC record's evidence for the
    byte-identity promise, so it must genuinely run both sides.
    """
    user = make_user(db_session)
    wc = make_work_center(db_session)

    # CONTROL: completed with the allocation table entirely empty.
    control_part = make_part(db_session, standard_cost=9.0)
    control_wo = make_wo(db_session, control_part, quantity_ordered=4, quantity_complete=4)
    make_op(db_session, control_wo, wc, quantity_complete=4)
    assert db_session.query(WorkOrderMaterialAllocation).count() == 0, "the control must run pre-feature"

    run_effects(db_session, control_wo, user)
    control_ledger = work_order_fingerprint(db_session, control_wo)
    control_audit = work_order_audit_fingerprint(db_session, control_wo)
    assert control_ledger, "the control WO must still receive its finished good"

    # A TIED work order is completed in the same tenant: the engine really runs.
    tied_part = make_part(db_session, standard_cost=9.0)
    tied_wo = make_wo(db_session, tied_part, quantity_ordered=4, quantity_complete=4)
    tied_op = make_op(db_session, tied_wo, wc, quantity_complete=4)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    make_inventory(db_session, sheet, qty=50, lot="SHEET-A")
    make_allocation(db_session, tied_wo, sheet, operation=tied_op, qty_per_run=1.0, qty_planned=4)

    run_effects(db_session, tied_wo, user)
    db_session.expire_all()
    assert [t.quantity for t in consumption_txns(db_session, tied_op.id)] == [
        -4
    ], "the tied WO must actually consume, or the comparison below proves nothing"

    # SUBJECT: structurally identical to the control, untied, completed with the
    # allocation machinery live and populated.
    subject_part = make_part(db_session, standard_cost=9.0)
    subject_wo = make_wo(db_session, subject_part, quantity_ordered=4, quantity_complete=4)
    make_op(db_session, subject_wo, wc, quantity_complete=4)

    run_effects(db_session, subject_wo, user)
    db_session.expire_all()

    assert work_order_fingerprint(db_session, subject_wo) == control_ledger
    assert work_order_audit_fingerprint(db_session, subject_wo) == control_audit
    # Re-entry stays a no-op for the control too.
    run_effects(db_session, control_wo, user)
    db_session.expire_all()
    assert work_order_fingerprint(db_session, control_wo) == control_ledger

    for wo in (control_wo, subject_wo):
        rows = work_order_ledger_rows(db_session, wo)
        assert rows
        assert all(r.allocation_id is None for r in rows), wo.work_order_number
        assert all(r.reference_type == "work_order" for r in rows), wo.work_order_number

    # The feature's other two write channels must name only the tied WO.
    allocation_audit_rows = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.company_id == COMPANY_A,
            AuditLog.resource_type == "work_order_material_allocation",
        )
        .all()
    )
    assert all((row.extra_data or {}).get("work_order_id") == tied_wo.id for row in allocation_audit_rows)
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


def test_untied_work_order_is_unchanged_by_the_pr2_read_surfaces(client: TestClient, db_session: Session):
    """PR 2's half of invariant 6(d): the new READ surfaces change nothing.

    PR 2 hung a material-tie read on three payloads a work order passes through
    constantly -- the manager dispatch board, the kiosk work-center queue and the
    operator's active job. Each of those is polled, and one of them (the run-order
    PUT that reuses the board projection) COMMITS, so a read that quietly
    reconciled would be persisted by a manager's drag-reorder.

    An untied work order is driven through every one of those surfaces and THEN
    completed. Its ledger and audit fingerprints must equal a CONTROL work order
    that was completed without any of them being touched -- no ledger row, no
    audit row, no operational event, nothing.

    The reads are asserted to have actually happened (200s, and the tie fields
    present-but-empty), so this cannot pass by never reaching the code.
    """
    user = make_user(db_session)
    headers = headers_for(user)
    wc = make_work_center(db_session)

    # CONTROL: completed without any PR-2 surface being read.
    control_part = make_part(db_session, standard_cost=9.0)
    control_wo = make_wo(db_session, control_part, quantity_ordered=4, quantity_complete=4)
    make_op(db_session, control_wo, wc, quantity_complete=4)
    run_effects(db_session, control_wo, user)
    control_ledger = work_order_fingerprint(db_session, control_wo)
    control_audit = work_order_audit_fingerprint(db_session, control_wo)
    assert control_ledger, "the control WO must still receive its finished good"

    # SUBJECT: structurally identical, untied, read through every new surface first.
    subject_part = make_part(db_session, standard_cost=9.0)
    subject_wo = make_wo(db_session, subject_part, quantity_ordered=4, quantity_complete=4)
    subject_op = make_op(db_session, subject_wo, wc, quantity_complete=4)

    board = client.get("/api/v1/shop-floor/dispatch-board", headers=headers)
    assert board.status_code == status.HTTP_200_OK, board.text
    board_rows = [
        row
        for column in board.json()["work_centers"]
        for row in column["queue"]
        if row["operation_id"] == subject_op.id
    ]
    assert board_rows, "the subject operation must really be on the board"
    assert all(row["material_tie"] is None for row in board_rows)

    queue = client.get(f"/api/v1/shop-floor/work-center-queue/{wc.id}", headers=headers)
    assert queue.status_code == status.HTTP_200_OK, queue.text
    queue_rows = [row for row in queue.json()["queue"] if row["operation_id"] == subject_op.id]
    assert queue_rows, "the subject operation must really be on the kiosk queue"
    assert all(row["material_ties"] == [] for row in queue_rows)

    active = client.get("/api/v1/shop-floor/my-active-job", headers=headers)
    assert active.status_code == status.HTTP_200_OK, active.text

    ties = client.get(f"/api/v1/work-orders/{subject_wo.id}/material-allocations", headers=headers)
    assert ties.status_code == status.HTTP_200_OK, ties.text
    assert ties.json() == []

    run_effects(db_session, subject_wo, user)
    db_session.expire_all()

    assert work_order_fingerprint(db_session, subject_wo) == control_ledger
    assert work_order_audit_fingerprint(db_session, subject_wo) == control_audit

    # The three write channels the feature owns, all empty for this tenant.
    assert db_session.query(WorkOrderMaterialAllocation).count() == 0
    rows = work_order_ledger_rows(db_session, subject_wo)
    assert rows
    assert all(r.allocation_id is None for r in rows)
    assert all(r.reference_type == "work_order" for r in rows)
    assert (
        db_session.query(AuditLog)
        .filter(
            AuditLog.company_id == COMPANY_A,
            AuditLog.resource_type == "work_order_material_allocation",
        )
        .count()
        == 0
    )
    assert (
        db_session.query(OperationalEvent)
        .filter(
            OperationalEvent.company_id == COMPANY_A,
            OperationalEvent.source_module == "material_consumption",
        )
        .count()
        == 0
    )


# ---------------------------------------------------------------------------
# Consume-on-complete + sum-delta
# ---------------------------------------------------------------------------


def test_completed_runs_consume_one_sheet_each(db_session: Session):
    """The headline: a nest tied to a sheet part consumes 1 sheet per completed run."""
    user = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material", standard_cost=80.0)
    lot = make_inventory(db_session, sheet, qty=20, lot="SHEET-FIFO-1", unit_cost=75.0)
    wo = make_wo(db_session, fg, quantity_ordered=6, quantity_complete=6)
    wc = make_work_center(db_session)
    op = make_op(db_session, wo, wc, quantity_complete=6)
    allocation = make_allocation(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=6)

    run_effects(db_session, wo, user)
    db_session.expire_all()

    txns = consumption_txns(db_session, op.id)
    assert len(txns) == 1
    txn = txns[0]
    assert txn.transaction_type == TransactionType.ISSUE
    assert txn.quantity == -6
    assert txn.part_id == sheet.id
    assert txn.reference_type == OPERATION_REFERENCE_TYPE
    assert txn.reference_id == op.id
    assert txn.reference_number == wo.work_order_number
    assert txn.allocation_id == allocation.id
    assert txn.lot_number == "SHEET-FIFO-1"
    assert txn.from_location == lot.location
    assert txn.unit_cost == 75.0

    db_session.refresh(lot)
    assert lot.quantity_on_hand == 14
    db_session.refresh(allocation)
    assert allocation.qty_consumed == 6


def test_quantity_available_stays_reconciled_after_consumption(db_session: Session):
    """quantity_available == quantity_on_hand - quantity_allocated after a consume.

    The denormalized column is maintained at the CALL SITE, not inside the savepoint
    helper; forgetting it desyncs the value the receipt-void guard and MRP read.
    """
    user = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    lot = make_inventory(db_session, sheet, qty=20, lot="SHEET-AVAIL", allocated=3.0)
    wo = make_wo(db_session, fg, quantity_ordered=5, quantity_complete=5)
    op = make_op(db_session, wo, make_work_center(db_session), quantity_complete=5)
    make_allocation(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=5)

    run_effects(db_session, wo, user)
    db_session.refresh(lot)

    assert lot.quantity_on_hand == 15
    assert lot.quantity_allocated == 3
    assert lot.quantity_available == lot.quantity_on_hand - lot.quantity_allocated == 12


def test_incremental_consumption_across_sessions(db_session: Session):
    """Consumption tops up to the recomputed target as more runs complete."""
    user = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    lot = make_inventory(db_session, sheet, qty=30, lot="SHEET-INC")
    wo = make_wo(db_session, fg, quantity_ordered=10, quantity_complete=2)
    op = make_op(db_session, wo, make_work_center(db_session), quantity_complete=2)
    allocation = make_allocation(db_session, wo, sheet, operation=op, qty_per_run=2.0, qty_planned=20)

    audit = AuditService(db_session, user)
    consume_tied_materials_for_work_order(db_session, wo, user_id=user.id, company_id=COMPANY_A, audit=audit)
    db_session.commit()
    db_session.refresh(allocation)
    assert allocation.qty_consumed == 4  # 2 runs * 2/run

    # Second session: three more runs complete.
    op.quantity_complete = 5
    db_session.commit()
    consume_tied_materials_for_work_order(db_session, wo, user_id=user.id, company_id=COMPANY_A, audit=audit)
    db_session.commit()

    db_session.refresh(allocation)
    db_session.refresh(lot)
    assert allocation.qty_consumed == 10  # 5 runs * 2/run
    txns = consumption_txns(db_session, op.id)
    assert [t.quantity for t in txns] == [-4, -6], "second pass posts only the DELTA"
    assert lot.quantity_on_hand == 20


def test_replay_is_idempotent_by_construction(db_session: Session):
    """Re-running with unchanged operation state posts nothing new."""
    user = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    lot = make_inventory(db_session, sheet, qty=30, lot="SHEET-IDEM")
    wo = make_wo(db_session, fg, quantity_ordered=3, quantity_complete=3)
    op = make_op(db_session, wo, make_work_center(db_session), quantity_complete=3)

    make_allocation(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=3)

    for _ in range(3):
        run_effects(db_session, wo, user)

    db_session.expire_all()
    txns = consumption_txns(db_session, op.id)
    assert len(txns) == 1
    assert txns[0].quantity == -3
    db_session.refresh(lot)
    assert lot.quantity_on_hand == 27


def test_scrapped_runs_consume_and_post_as_issue(db_session: Session):
    """Scrap consumed the sheet, so it is inside the target — posted as ISSUE.

    Posting SCRAP would drop the material out of lot genealogy, which filters on ISSUE.
    """
    user = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    lot = make_inventory(db_session, sheet, qty=30, lot="SHEET-SCRAP")
    wo = make_wo(db_session, fg, quantity_ordered=10, quantity_complete=7)
    op = make_op(db_session, wo, make_work_center(db_session), quantity_complete=7, quantity_scrapped=3)
    make_allocation(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=10)

    run_effects(db_session, wo, user)
    db_session.expire_all()

    txns = consumption_txns(db_session, op.id)
    assert len(txns) == 1
    assert txns[0].transaction_type == TransactionType.ISSUE
    assert txns[0].quantity == -10, "7 good + 3 scrapped runs both consumed a sheet"
    assert "7" in txns[0].notes and "3" in txns[0].notes, "good/scrap split recorded in notes"
    assert not any(t.transaction_type == TransactionType.SCRAP for t in txns)
    db_session.refresh(lot)
    assert lot.quantity_on_hand == 20


def test_negative_delta_never_auto_reverses(db_session: Session):
    """A reduce-over-count lowering quantity_complete must NOT return material."""
    user = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    lot = make_inventory(db_session, sheet, qty=30, lot="SHEET-REDUCE")
    wo = make_wo(db_session, fg, quantity_ordered=10, quantity_complete=8)
    op = make_op(db_session, wo, make_work_center(db_session), quantity_complete=8)
    allocation = make_allocation(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=10)

    run_effects(db_session, wo, user)
    db_session.refresh(lot)
    assert lot.quantity_on_hand == 22

    # Supervisor walks the count back: the sheets are already cut.
    op.quantity_complete = 5
    db_session.commit()
    run_effects(db_session, wo, user)

    db_session.expire_all()
    txns = consumption_txns(db_session, op.id)
    assert len(txns) == 1, "no compensating RETURN/ISSUE may be posted"
    assert all(t.quantity < 0 for t in txns)
    db_session.refresh(lot)
    assert lot.quantity_on_hand == 22
    db_session.refresh(allocation)
    assert allocation.qty_consumed == 8, "qty_consumed never walks backwards"


# ---------------------------------------------------------------------------
# Lot selection
# ---------------------------------------------------------------------------


def test_fifo_spills_across_lots_oldest_first(db_session: Session):
    user = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    old = make_inventory(db_session, sheet, qty=4, lot="SHEET-OLD", received_date=datetime(2026, 1, 1), unit_cost=10.0)
    new = make_inventory(db_session, sheet, qty=10, lot="SHEET-NEW", received_date=datetime(2026, 6, 1), unit_cost=20.0)
    wo = make_wo(db_session, fg, quantity_ordered=6, quantity_complete=6)
    op = make_op(db_session, wo, make_work_center(db_session), quantity_complete=6)
    make_allocation(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=6)

    run_effects(db_session, wo, user)
    db_session.expire_all()

    txns = consumption_txns(db_session, op.id)
    assert [(t.lot_number, t.quantity) for t in txns] == [("SHEET-OLD", -4), ("SHEET-NEW", -2)]
    db_session.refresh(old)
    db_session.refresh(new)
    assert old.quantity_on_hand == 0
    assert new.quantity_on_hand == 8


@pytest.mark.parametrize(
    "field,value,expected_word",
    [("status", "quarantine", "quarantine"), ("status", "on_hold", "on_hold"), ("is_active", False, "inactive")],
)
def test_pinning_a_held_lot_is_refused_at_tie_time(
    client: TestClient, db_session: Session, field: str, value, expected_word: str
):
    """FIFO excludes held lots; the pinned branch does not — so refuse the PIN (422).

    Consuming ``quarantine`` / ``on_hold`` / ``rejected`` / inactive material into an
    as-built record is an AS9100D 8.7 event. The refusal has to land HERE, where a human
    is present to answer, because consumption runs from a GET where refusing is not an
    option.
    """
    admin = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    lot = make_inventory(db_session, sheet, qty=20, lot=f"HELD-{_next()}")
    setattr(lot, field, value)
    db_session.commit()

    wo = make_wo(db_session, fg, quantity_ordered=5)
    op = make_op(db_session, wo, make_work_center(db_session))

    body = {
        "part_id": sheet.id,
        "work_order_operation_id": op.id,
        "source": "nest",
        "qty_planned": 5,
        "pinned_inventory_item_id": lot.id,
    }
    resp = client.post(_tie_url(wo.id), json=body, headers=headers_for(admin))
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, resp.text
    assert expected_word in resp.json()["detail"]
    assert db_session.query(WorkOrderMaterialAllocation).filter_by(work_order_id=wo.id).count() == 0

    # PATCH goes through the same resolver, so re-pinning a held lot is refused too.
    allocation = make_allocation(db_session, wo, sheet, operation=op, qty_planned=5)
    resp = client.patch(
        _tie_url(wo.id, allocation.id),
        json={"pinned_inventory_item_id": lot.id},
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, resp.text


def test_lot_held_after_pinning_still_consumes_but_is_audited(db_session: Session):
    """Held AFTER the pin: consumption proceeds, and the hash chain says so.

    Refusing at consume time is not available (reconcile-on-read is a GET with no actor
    intent), and the sheet is already cut, so the ledger must record it. What is not
    acceptable is silence — hence the ``HELD_MATERIAL_CONSUMED`` row.
    """
    user = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    pinned = make_inventory(db_session, sheet, qty=20, lot="SHEET-HELD-LATER")
    wo = make_wo(db_session, fg, quantity_ordered=4, quantity_complete=4)
    op = make_op(db_session, wo, make_work_center(db_session), quantity_complete=4)
    allocation = make_allocation(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=4, pinned=pinned)

    # The lot is quarantined after the tie was made.
    pinned.status = "quarantine"
    db_session.commit()

    run_effects(db_session, wo, user)
    db_session.expire_all()

    txns = consumption_txns(db_session, op.id)
    assert [t.quantity for t in txns] == [-4], "production truth outranks the hold; it still consumes"
    db_session.refresh(pinned)
    assert pinned.quantity_on_hand == 16

    held_rows = (
        db_session.query(AuditLog)
        .filter(AuditLog.company_id == COMPANY_A, AuditLog.action == HELD_MATERIAL_CONSUMED_AUDIT_ACTION)
        .all()
    )
    assert len(held_rows) == 1
    extra = held_rows[0].extra_data
    assert extra["allocation_id"] == allocation.id
    assert extra["inventory_item_id"] == pinned.id
    assert extra["lot_number"] == "SHEET-HELD-LATER"
    assert extra["item_status"] == "quarantine"
    assert extra["quantity"] == 4
    assert held_rows[0].integrity_hash, "recorded on the tamper-evident chain"


def test_available_pinned_lot_records_no_held_audit_row(db_session: Session):
    """The held-material record must not fire on the ordinary pinned path."""
    user = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    pinned = make_inventory(db_session, sheet, qty=20, lot="SHEET-OK")
    wo = make_wo(db_session, fg, quantity_ordered=3, quantity_complete=3)
    op = make_op(db_session, wo, make_work_center(db_session), quantity_complete=3)
    make_allocation(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=3, pinned=pinned)

    run_effects(db_session, wo, user)

    assert (
        db_session.query(AuditLog)
        .filter(AuditLog.company_id == COMPANY_A, AuditLog.action == HELD_MATERIAL_CONSUMED_AUDIT_ACTION)
        .count()
        == 0
    )


def test_pinned_lot_is_used_exclusively(db_session: Session):
    user = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    older = make_inventory(db_session, sheet, qty=50, lot="SHEET-FIFO", received_date=datetime(2026, 1, 1))
    pinned = make_inventory(db_session, sheet, qty=50, lot="SHEET-PIN", received_date=datetime(2026, 6, 1))
    wo = make_wo(db_session, fg, quantity_ordered=5, quantity_complete=5)
    op = make_op(db_session, wo, make_work_center(db_session), quantity_complete=5)
    make_allocation(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=5, pinned=pinned)

    run_effects(db_session, wo, user)
    db_session.expire_all()

    txns = consumption_txns(db_session, op.id)
    assert [(t.lot_number, t.quantity) for t in txns] == [("SHEET-PIN", -5)]
    db_session.refresh(older)
    assert older.quantity_on_hand == 50, "the FIFO lot must be untouched when a pin exists"


# ---------------------------------------------------------------------------
# Shortage posture
# ---------------------------------------------------------------------------


def test_shortage_drives_negative_records_audit_and_event(db_session: Session):
    """A shortage never fails the completion; it is recorded and warned about."""
    user = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    lot = make_inventory(db_session, sheet, qty=2, lot="SHEET-SHORT")
    wo = make_wo(db_session, fg, quantity_ordered=5, quantity_complete=5)
    op = make_op(db_session, wo, make_work_center(db_session), quantity_complete=5)
    allocation = make_allocation(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=5)

    run_effects(db_session, wo, user)
    db_session.expire_all()

    txns = consumption_txns(db_session, op.id)
    assert sum(t.quantity for t in txns) == -5, "full demand still recorded on the ledger"
    db_session.refresh(lot)
    assert lot.quantity_on_hand == -3
    assert lot.quantity_available == lot.quantity_on_hand - lot.quantity_allocated

    shortage_rows = (
        db_session.query(AuditLog)
        .filter(AuditLog.action == ALLOCATION_SHORTAGE_AUDIT_ACTION, AuditLog.company_id == COMPANY_A)
        .all()
    )
    assert len(shortage_rows) == 1
    assert shortage_rows[0].extra_data["allocation_id"] == allocation.id
    assert shortage_rows[0].extra_data["shortfall"] == 3

    events = (
        db_session.query(OperationalEvent)
        .filter(
            OperationalEvent.company_id == COMPANY_A,
            OperationalEvent.event_type == ALLOCATION_SHORTAGE_EVENT_TYPE,
        )
        .all()
    )
    assert len(events) == 1
    assert events[0].severity == "warning"


def test_shortage_with_no_stock_at_all_still_records(db_session: Session):
    user = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material", standard_cost=42.0)
    wo = make_wo(db_session, fg, quantity_ordered=3, quantity_complete=3)
    op = make_op(db_session, wo, make_work_center(db_session), quantity_complete=3)
    make_allocation(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=3)

    run_effects(db_session, wo, user)
    db_session.expire_all()

    txns = consumption_txns(db_session, op.id)
    assert len(txns) == 1
    assert txns[0].quantity == -3
    assert txns[0].inventory_item_id is not None, "recorded against a real placeholder row"


# ---------------------------------------------------------------------------
# Backflush precedence
# ---------------------------------------------------------------------------


def _bom_with_component(db: Session, parent: Part, component: Part, qty: float = 1.0) -> BOM:
    bom = BOM(
        part_id=parent.id,
        revision="A",
        description="mc fixture bom",
        is_active=True,
        company_id=COMPANY_A,
    )
    db.add(bom)
    db.commit()
    db.refresh(bom)
    _add_bom_item(db, bom, component, qty)
    return bom


def _add_bom_item(db: Session, bom: BOM, component: Part, qty: float, item_number: int = 10) -> None:
    db.add(
        BOMItem(
            bom_id=bom.id,
            component_part_id=component.id,
            item_number=item_number,
            quantity=qty,
            item_type="buy",
            line_type="component",
            scrap_factor=0.0,
            company_id=COMPANY_A,
        )
    )
    db.commit()


def test_backflush_skips_parts_covered_by_an_operation_allocation(db_session: Session):
    """An allocation-covered part must NOT also get a WO-level backflush ISSUE.

    The two reference types sit in different index predicates, so nothing at the DB
    level would catch the double-issue — this precedence rule is the only guard.
    """
    user = make_user(db_session)
    fg = make_part(db_session, backflush=True)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    lot = make_inventory(db_session, sheet, qty=50, lot="SHEET-PRECEDENCE")
    _bom_with_component(db_session, fg, sheet, qty=1.0)

    wo = make_wo(db_session, fg, quantity_ordered=4, quantity_complete=4)
    op = make_op(db_session, wo, make_work_center(db_session), quantity_complete=4)
    make_allocation(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=4)

    run_effects(db_session, wo, user)
    db_session.expire_all()

    wo_level = (
        db_session.query(InventoryTransaction)
        .filter(
            InventoryTransaction.company_id == COMPANY_A,
            InventoryTransaction.reference_type == "work_order",
            InventoryTransaction.reference_id == wo.id,
            InventoryTransaction.transaction_type == TransactionType.ISSUE,
            InventoryTransaction.part_id == sheet.id,
        )
        .all()
    )
    assert wo_level == [], "the allocation is the sole demand carrier for this part"

    op_level = consumption_txns(db_session, op.id)
    assert [t.quantity for t in op_level] == [-4]
    db_session.refresh(lot)
    assert lot.quantity_on_hand == 46, "consumed exactly once"


def test_backflush_still_issues_uncovered_bom_components(db_session: Session):
    """Precedence is per-part: a component with no tie still backflushes normally."""
    user = make_user(db_session)
    fg = make_part(db_session, backflush=True)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    bolt = make_part(db_session, part_type="purchased")
    make_inventory(db_session, sheet, qty=50, lot="SHEET-MIX")
    bolt_lot = make_inventory(db_session, bolt, qty=100, lot="BOLT-MIX")

    bom = _bom_with_component(db_session, fg, sheet, qty=1.0)
    _add_bom_item(db_session, bom, bolt, 2.0, item_number=20)

    wo = make_wo(db_session, fg, quantity_ordered=3, quantity_complete=3)
    op = make_op(db_session, wo, make_work_center(db_session), quantity_complete=3)
    make_allocation(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=3)

    run_effects(db_session, wo, user)
    db_session.expire_all()

    db_session.refresh(bolt_lot)
    assert bolt_lot.quantity_on_hand == 94, "3 units * 2 bolts still backflushed"
    assert [t.quantity for t in consumption_txns(db_session, op.id)] == [-3]


def test_bom_and_tie_post_separate_attributed_rows(db_session: Session):
    """REWRITE of ``test_work_order_scoped_tie_merges_into_one_issue_row``.

    The merge existed ONLY because ``uq_wo_inventory_issue`` permitted a single ISSUE
    row per (company, work order, part), and it was arithmetically wrong under
    reconciliation: BOM demand is an absolute target while tie demand was a remainder, so
    summing them and subtracting one net double-counts the tie forever. The reconciling
    leg posts outside that index, so the two demand sources get their own rows.

    **The TOTAL is asserted first and is unchanged — 10 from the BOM + 7 from the tie is
    still 17, and the source lot still ends at 83.** That is deliberate: a rewrite of a
    quantity test must not be able to hide a quantity regression behind a shape change.
    What changed is attribution — ``allocation_id IS NULL`` on the BOM's row and the tie's
    own id on the tie's row, which is exactly what keeps the two nets disjoint — and that
    the tie's LOT PIN would now govern the tie's quantity rather than the whole sum.
    """
    user = make_user(db_session)
    fg = make_part(db_session, backflush=True)
    stock = make_part(db_session, part_type="purchased")
    lot = make_inventory(db_session, stock, qty=100, lot="MERGE-LOT")
    _bom_with_component(db_session, fg, stock, qty=2.0)

    wo = make_wo(db_session, fg, quantity_ordered=5, quantity_complete=5)
    make_op(db_session, wo, make_work_center(db_session), quantity_complete=5)
    allocation = make_allocation(db_session, wo, stock, operation=None, qty_planned=7)

    run_effects(db_session, wo, user)
    db_session.expire_all()

    issues = (
        db_session.query(InventoryTransaction)
        .filter(
            InventoryTransaction.company_id == COMPANY_A,
            InventoryTransaction.reference_type == BACKFLUSH_REFERENCE_TYPE,
            InventoryTransaction.reference_id == wo.id,
            InventoryTransaction.transaction_type == TransactionType.ISSUE,
            InventoryTransaction.part_id == stock.id,
        )
        .all()
    )
    assert sum(t.quantity for t in issues) == -17, "10 from the BOM + 7 from the tie — the total is unchanged"
    assert len(issues) == 2, "one row per demand source, not one merged row"
    assert {(t.allocation_id, t.quantity) for t in issues} == {(None, -10.0), (allocation.id, -7.0)}
    assert (
        db_session.query(InventoryTransaction)
        .filter(
            InventoryTransaction.company_id == COMPANY_A,
            InventoryTransaction.reference_type == WORK_ORDER_REFERENCE_TYPE,
            InventoryTransaction.reference_id == wo.id,
            InventoryTransaction.transaction_type == TransactionType.ISSUE,
        )
        .count()
        == 0
    ), "and nothing is written under the legacy one-shot shape"
    db_session.refresh(lot)
    assert lot.quantity_on_hand == 83
    db_session.refresh(allocation)
    assert allocation.qty_consumed == 7


def test_backflush_is_not_suppressed_by_a_tie_to_a_foreign_operation(db_session: Session):
    """A tie that cannot consume must not suppress the backflush either.

    The consume path already skips a tie whose operation is not on this WO; the
    backflush-precedence path did not check, so such a part was NEITHER consumed NOR
    backflushed — silently. The two must agree.
    """
    user = make_user(db_session)
    fg = make_part(db_session, backflush=True)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    lot = make_inventory(db_session, sheet, qty=50, lot="SHEET-ORPHAN-TIE")
    _bom_with_component(db_session, fg, sheet, qty=1.0)

    wo = make_wo(db_session, fg, quantity_ordered=4, quantity_complete=4)
    make_op(db_session, wo, make_work_center(db_session), quantity_complete=4)

    # The tie points at an operation on a DIFFERENT work order (a nest re-import that
    # raced the cancel, or a hand-edited row).
    other_wo = make_wo(db_session, fg, quantity_ordered=1)
    foreign_op = make_op(db_session, other_wo, make_work_center(db_session), sequence=20)
    make_allocation(db_session, wo, sheet, operation=foreign_op, qty_per_run=1.0, qty_planned=4)

    run_effects(db_session, wo, user)
    db_session.expire_all()

    assert consumption_txns(db_session, foreign_op.id) == [], "a tie off this WO must never consume"
    wo_level = (
        db_session.query(InventoryTransaction)
        .filter(
            InventoryTransaction.company_id == COMPANY_A,
            InventoryTransaction.reference_type == BACKFLUSH_REFERENCE_TYPE,
            InventoryTransaction.reference_id == wo.id,
            InventoryTransaction.transaction_type == TransactionType.ISSUE,
            InventoryTransaction.part_id == sheet.id,
        )
        .all()
    )
    assert [t.quantity for t in wo_level] == [-4], "the BOM demand must still backflush"
    db_session.refresh(lot)
    assert lot.quantity_on_hand == 46


def test_work_order_scoped_tie_consumes_without_backflush_opt_in(db_session: Session):
    """An explicit tie IS the opt-in; it does not need part.backflush_components."""
    user = make_user(db_session)
    fg = make_part(db_session, backflush=False)
    stock = make_part(db_session, part_type="purchased")
    lot = make_inventory(db_session, stock, qty=40, lot="TIE-ONLY")

    wo = make_wo(db_session, fg, quantity_ordered=2, quantity_complete=2)
    make_op(db_session, wo, make_work_center(db_session), quantity_complete=2)
    make_allocation(db_session, wo, stock, operation=None, qty_planned=6)

    run_effects(db_session, wo, user)
    db_session.refresh(lot)
    assert lot.quantity_on_hand == 34


# ---------------------------------------------------------------------------
# Job cost — tied material must appear in the actuals
# ---------------------------------------------------------------------------


def test_tied_material_is_in_job_cost_actuals(db_session: Session):
    """The headline nest: six $80 sheets are $480 of REAL material cost.

    Both cost implementations filtered on ``reference_type='work_order'`` only, so every
    operation-scoped consumption row fell out of ``WorkOrder.actual_cost``, the synced
    ``JobCost``, and the analytics variance — compliance-facing reports understating a
    job's material by its entire material leg.
    """
    user = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material", standard_cost=80.0)
    make_inventory(db_session, sheet, qty=20, lot="SHEET-COST", unit_cost=80.0)
    wo = make_wo(db_session, fg, quantity_ordered=6, quantity_complete=6)
    op = make_op(db_session, wo, make_work_center(db_session), quantity_complete=6)
    make_allocation(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=6)

    run_effects(db_session, wo, user)
    db_session.expire_all()

    assert _issued_material_cost(db_session, wo, COMPANY_A) == 480.0
    # The analytics leg must agree exactly — they share one predicate.
    assert AnalyticsService(db_session, COMPANY_A)._issued_material_cost(wo.id) == 480.0


def test_tied_and_backflushed_material_both_count_once(db_session: Session):
    """Widening must ADD the operation-scoped leg, not double-count the WO-scoped one."""
    user = make_user(db_session)
    fg = make_part(db_session, backflush=True)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material", standard_cost=80.0)
    bolt = make_part(db_session, part_type="purchased", standard_cost=3.0)
    make_inventory(db_session, sheet, qty=20, lot="SHEET-MIXCOST", unit_cost=80.0)
    make_inventory(db_session, bolt, qty=100, lot="BOLT-MIXCOST", unit_cost=3.0)
    _bom_with_component(db_session, fg, bolt, qty=2.0)

    wo = make_wo(db_session, fg, quantity_ordered=5, quantity_complete=5)
    op = make_op(db_session, wo, make_work_center(db_session), quantity_complete=5)
    make_allocation(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=5)

    run_effects(db_session, wo, user)
    db_session.expire_all()

    # 5 sheets @ 80 (tied, operation-scoped) + 10 bolts @ 3 (BOM backflush, WO-scoped).
    assert _issued_material_cost(db_session, wo, COMPANY_A) == 430.0
    assert AnalyticsService(db_session, COMPANY_A)._issued_material_cost(wo.id) == 430.0


def test_untied_work_order_material_cost_is_unchanged(db_session: Session):
    """The widened filter must not perturb a work order with no tied material."""
    user = make_user(db_session)
    fg = make_part(db_session, backflush=True)
    bolt = make_part(db_session, part_type="purchased", standard_cost=3.0)
    make_inventory(db_session, bolt, qty=100, lot="BOLT-PLAIN", unit_cost=3.0)
    _bom_with_component(db_session, fg, bolt, qty=2.0)

    wo = make_wo(db_session, fg, quantity_ordered=5, quantity_complete=5)
    make_op(db_session, wo, make_work_center(db_session), quantity_complete=5)

    run_effects(db_session, wo, user)
    db_session.expire_all()

    assert _issued_material_cost(db_session, wo, COMPANY_A) == 30.0


def test_material_cost_never_reaches_across_tenants(db_session: Session):
    """The operation subquery is company-scoped, so another tenant's ops can't match."""
    user_a = make_user(db_session, company_id=COMPANY_A)
    fg = make_part(db_session, company_id=COMPANY_A)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material", standard_cost=80.0, company_id=COMPANY_A)
    make_inventory(db_session, sheet, qty=20, lot="SHEET-TENANT", unit_cost=80.0)
    wo = make_wo(db_session, fg, quantity_ordered=2, quantity_complete=2, company_id=COMPANY_A)
    op = make_op(db_session, wo, make_work_center(db_session), quantity_complete=2)
    make_allocation(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=2)

    run_effects(db_session, wo, user_a)
    db_session.expire_all()

    assert _issued_material_cost(db_session, wo, COMPANY_A) == 160.0
    assert _issued_material_cost(db_session, wo, COMPANY_B) == 0.0
    assert AnalyticsService(db_session, COMPANY_B)._issued_material_cost(wo.id) == 0.0


# ---------------------------------------------------------------------------
# Audit coverage of the WO-scoped consumption write
# ---------------------------------------------------------------------------


def test_work_order_scoped_tie_consumption_is_audited(db_session: Session):
    """``qty_consumed`` 0 -> its ledger net on a WO-scoped tie is a state change: audit it.

    It is the field the untie guard keys on (409 once anything is consumed), so an
    unaudited write here changes what a later verb refuses with nothing on the chain
    saying why. The operation-scoped twin already audited it; this is the asymmetry.

    Since PR 4.4 the value written is the tie's OWN signed ledger net rather than
    ``qty_planned`` — here they coincide (the draw covered the plan), which is the point:
    cache == net by construction. The chain row's ``reference_type`` follows the shape
    the movement actually posted under.
    """
    user = make_user(db_session)
    fg = make_part(db_session)
    stock = make_part(db_session, part_type="purchased")
    make_inventory(db_session, stock, qty=40, lot="WO-TIE-AUDIT")
    wo = make_wo(db_session, fg, quantity_ordered=2, quantity_complete=2)
    make_op(db_session, wo, make_work_center(db_session), quantity_complete=2)
    allocation = make_allocation(db_session, wo, stock, operation=None, qty_planned=6)

    run_effects(db_session, wo, user)
    db_session.expire_all()

    rows = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.company_id == COMPANY_A,
            AuditLog.resource_type == "work_order_material_allocation",
            AuditLog.resource_id == allocation.id,
            AuditLog.action == AuditService.ACTIONS["UPDATE"],
        )
        .all()
    )
    assert len(rows) == 1, "exactly one chain row for the consumption write"
    row = rows[0]
    assert row.old_values["qty_consumed"] == 0
    assert row.new_values["qty_consumed"] == 6
    assert row.extra_data["work_order_id"] == wo.id
    assert row.extra_data["reference_type"] == BACKFLUSH_REFERENCE_TYPE
    assert row.extra_data["allocation_id"] == allocation.id
    assert row.integrity_hash and row.sequence_number is not None

    # A replay changes nothing, so it must not append a second row.
    run_effects(db_session, wo, user)
    db_session.expire_all()
    assert (
        db_session.query(AuditLog)
        .filter(
            AuditLog.company_id == COMPANY_A,
            AuditLog.resource_type == "work_order_material_allocation",
            AuditLog.resource_id == allocation.id,
            AuditLog.action == AuditService.ACTIONS["UPDATE"],
        )
        .count()
        == 1
    )


# ---------------------------------------------------------------------------
# Failure posture
# ---------------------------------------------------------------------------


def test_failed_consumption_is_recorded_on_the_chain(db_session: Session, monkeypatch):
    """A rolled-back consumption must not be silent — the lesser case already isn't.

    Also pins the two properties the record depends on: ``AuditService.log`` is safe on
    the outer transaction AFTER a savepoint rollback (it opens its own savepoint), and
    the caller's commit still succeeds.
    """
    from app.services import material_consumption_service as mcs

    user = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    lot = make_inventory(db_session, sheet, qty=20, lot="SHEET-BOOM")
    wo = make_wo(db_session, fg, quantity_ordered=3, quantity_complete=3)
    op = make_op(db_session, wo, make_work_center(db_session), quantity_complete=3)
    allocation = make_allocation(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=3)

    def _boom(*args, **kwargs):
        raise RuntimeError("ledger insert exploded")

    monkeypatch.setattr(mcs, "_post_consumption_txn", _boom)

    audit = AuditService(db_session, user)
    result = mcs.consume_tied_materials_for_work_order(
        db_session, wo, user_id=user.id, company_id=COMPANY_A, audit=audit
    )
    db_session.commit()  # the outer unit of work must still be usable

    assert result.failed_allocation_ids == [allocation.id]
    assert result.transactions == []

    failed_rows = (
        db_session.query(AuditLog)
        .filter(AuditLog.company_id == COMPANY_A, AuditLog.action == ALLOCATION_CONSUMPTION_FAILED_AUDIT_ACTION)
        .all()
    )
    assert len(failed_rows) == 1
    row = failed_rows[0]
    assert row.resource_id == allocation.id
    assert row.success == "false"
    assert "RuntimeError" in (row.error_message or "")
    assert row.extra_data["work_order_id"] == wo.id
    assert row.extra_data["work_order_operation_id"] == op.id
    assert row.integrity_hash, "on the tamper-evident chain, not just a log line"

    # Nothing moved, and the cache did not advance.
    db_session.refresh(lot)
    assert lot.quantity_on_hand == 20
    db_session.refresh(allocation)
    assert allocation.qty_consumed == 0


def test_failure_after_the_inner_savepoint_opened_leaves_the_caller_committable(db_session: Session, monkeypatch):
    """Fail AFTER the ledger INSERT landed, with a deeper savepoint still open.

    **The seam this test patched moved in PR 4.4 and the test moved with it rather than
    being deleted.** It used to wrap ``_insert_txn_with_savepoint``, which opens
    ``db.begin_nested()`` and on SUCCESS returns WITHOUT closing it — so a later raise
    unwound a savepoint stack two deep, the only shape that can actually poison the
    caller's unit of work. This leg no longer takes that path at all: it posts with
    ``duplicate_is_noop=False`` (no unique index has ever covered
    ``work_order_operation``, so an ``IntegrityError`` here is a real fault, not a lost
    race), which inserts plainly. Left alone, the patch would simply never fire and the
    test would pass by testing nothing.

    So the wrapper now sits on ``_post_stock_movement_txn``: it calls the REAL helper
    first — the INSERT, the decrement and the dual audit rows all genuinely land — then
    opens a nested savepoint of its own to reconstruct the exact stack the old helper
    left behind, and raises inside it. Both facts are asserted, so this cannot quietly
    decay into the shallow before-the-savepoint case the test above already covers.
    """
    import app.services.completion_inventory_service as cis

    user = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    lot = make_inventory(db_session, sheet, qty=20, lot="SHEET-INNER-SP")
    wo = make_wo(db_session, fg, quantity_ordered=3, quantity_complete=3)
    op = make_op(db_session, wo, make_work_center(db_session), quantity_complete=3)
    allocation = make_allocation(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=3)

    real_post = cis._post_stock_movement_txn
    posted: list[object] = []

    def _post_then_explode(db, **kwargs):
        posted.append(real_post(db, **kwargs))
        db.begin_nested()  # the stack _insert_txn_with_savepoint used to leave open
        raise RuntimeError("failed with a deeper savepoint still open")

    monkeypatch.setattr(cis, "_post_stock_movement_txn", _post_then_explode)

    audit = AuditService(db_session, user)
    result = consume_tied_materials_for_work_order(db_session, wo, user_id=user.id, company_id=COMPANY_A, audit=audit)
    # A session poisoned by the deeper rollback raises PendingRollbackError here.
    db_session.commit()

    assert len(posted) == 1 and posted[0] is not None, "the INSERT must have landed before the raise"
    assert result.failed_allocation_ids == [allocation.id]
    assert result.transactions == []

    db_session.expire_all()
    assert consumption_txns(db_session, op.id) == [], "the rolled-back INSERT must not survive the commit"
    assert db_session.get(InventoryItem, lot.id).quantity_on_hand == 20
    assert db_session.get(WorkOrderMaterialAllocation, allocation.id).qty_consumed == 0

    failed_rows = (
        db_session.query(AuditLog)
        .filter(AuditLog.company_id == COMPANY_A, AuditLog.action == ALLOCATION_CONSUMPTION_FAILED_AUDIT_ACTION)
        .all()
    )
    assert len(failed_rows) == 1, "the failure record must survive the deeper rollback too"
    assert failed_rows[0].integrity_hash, "on the tamper-evident chain"


def test_consumption_never_raises_even_when_the_operation_lookup_fails(db_session: Session, monkeypatch):
    """The docstring says NEVER raises; the operation SELECT sat outside the guard.

    These paths run from live completion handlers and from a reconcile-on-read GET, so
    an escape here is a 500 on a completion.
    """
    from app.services import material_consumption_service as mcs

    user = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    make_inventory(db_session, sheet, qty=10, lot="SHEET-RAISE")
    wo = make_wo(db_session, fg, quantity_ordered=2, quantity_complete=2)
    op = make_op(db_session, wo, make_work_center(db_session), quantity_complete=2)
    make_allocation(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=2)

    def _boom(*args, **kwargs):
        raise RuntimeError("operation lookup exploded")

    monkeypatch.setattr(mcs, "_consume_tied_materials", _boom)

    result = mcs.consume_tied_materials_for_work_order(
        db_session, wo, user_id=user.id, company_id=COMPANY_A, audit=AuditService(db_session, user)
    )
    assert result.consumed_allocation_ids == []


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


def test_consumption_never_touches_another_tenants_stock(db_session: Session):
    user_a = make_user(db_session, company_id=COMPANY_A)
    fg = make_part(db_session, company_id=COMPANY_A)
    sheet_a = make_part(db_session, uom="sheets", part_type="raw_material", company_id=COMPANY_A)
    sheet_b = make_part(db_session, uom="sheets", part_type="raw_material", company_id=COMPANY_B)
    lot_b = make_inventory(db_session, sheet_b, qty=99, lot="B-SHEET", company_id=COMPANY_B)

    wo = make_wo(db_session, fg, quantity_ordered=3, quantity_complete=3, company_id=COMPANY_A)
    op = make_op(db_session, wo, make_work_center(db_session), quantity_complete=3, company_id=COMPANY_A)
    make_allocation(db_session, wo, sheet_a, operation=op, qty_per_run=1.0, qty_planned=3)

    run_effects(db_session, wo, user_a)
    db_session.refresh(lot_b)
    assert lot_b.quantity_on_hand == 99
    assert consumption_txns(db_session, op.id, company_id=COMPANY_B) == []


# ---------------------------------------------------------------------------
# Traceability
# ---------------------------------------------------------------------------


def test_lot_trace_sees_operation_scoped_consumption(client: TestClient, db_session: Session):
    """Genealogy must reconstruct material consumed through an operation-scoped tie."""
    admin = make_user(db_session)
    fg = make_part(db_session, standard_cost=100.0)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    make_inventory(db_session, sheet, qty=20, lot="TRACE-SHEET")
    wo = make_wo(db_session, fg, quantity_ordered=2, quantity_complete=2)
    op = make_op(db_session, wo, make_work_center(db_session), quantity_complete=2)
    make_allocation(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=2)

    run_effects(db_session, wo, admin)
    db_session.refresh(wo)
    fg_lot = wo.lot_number
    assert fg_lot

    resp = client.get(f"/api/v1/traceability/lot/{fg_lot}", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    consumed = body["consumed_components"]
    assert any(
        c["component_part_id"] == sheet.id and c["lot_number"] == "TRACE-SHEET" and c["quantity"] == 2 for c in consumed
    ), consumed
    assert all(c["work_order_id"] == wo.id for c in consumed)

    # The consumed sheet lot's own trace must name the work order that burned it.
    resp = client.get("/api/v1/traceability/lot/TRACE-SHEET", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert wo.work_order_number in resp.json()["work_orders_used"]


# ---------------------------------------------------------------------------
# Lifecycle guards
# ---------------------------------------------------------------------------


def test_nest_reimport_guard_refuses_when_consumed(db_session: Session):
    """The wipe is refused once the LEDGER references the operations it would delete.

    PR 3 re-keyed this guard from the ``qty_consumed`` cache to
    ``ledger_backed_allocation_ids``, matching the hard-delete guard. The consumption is
    therefore driven for real here rather than by presetting the cache: the cache is
    documented as non-authoritative, and it is the ledger rows whose ``reference_id``
    would be orphaned by ``db.delete(operation)``.
    """
    user = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    make_inventory(db_session, sheet, qty=10, lot="REIMPORT-CONSUMED")
    wo = make_wo(db_session, fg, quantity_ordered=3, quantity_complete=3)
    op = make_op(db_session, wo, make_work_center(db_session), quantity_complete=3, operation_group="LASER")
    allocation = make_allocation(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=3)

    run_effects(db_session, wo, user)
    db_session.refresh(allocation)
    assert allocation.qty_consumed == 3.0
    assert consumption_txns(db_session, op.id), "the ledger must really carry the consumption"

    with pytest.raises(MaterialAllocationConsumedError):
        cancel_allocations_for_operations(
            db_session,
            work_order=wo,
            operation_ids=[op.id],
            company_id=COMPANY_A,
            audit=AuditService(db_session, user),
        )


def test_nest_reimport_guard_reads_the_ledger_not_the_qty_consumed_cache(db_session: Session):
    """A cache that claims consumption the LEDGER does not show must not block the wipe.

    ``qty_consumed`` is explicitly a cache (model docstring) and the ledger is
    authoritative — the same basis the hard-delete guard has always used. Keying on the
    cache is what PR 3 had to move away from: a full ``return_and_untie`` drives the cache
    to 0 while the ISSUE **and** RETURN rows both still name the operation, so a
    cache-keyed guard would wave through exactly the wipe that orphans them. The converse,
    pinned here, is that drift in the other direction does not manufacture a refusal:
    with no ledger row there is nothing to orphan, so the tie is simply cancelled.
    """
    user = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    wo = make_wo(db_session, fg, quantity_ordered=3, quantity_complete=3)
    op = make_op(db_session, wo, make_work_center(db_session), quantity_complete=3, operation_group="LASER")
    allocation = make_allocation(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=3, qty_consumed=3.0)
    assert consumption_txns(db_session, op.id) == [], "no ledger row backs this cache value"

    cancelled = cancel_allocations_for_operations(
        db_session,
        work_order=wo,
        operation_ids=[op.id],
        company_id=COMPANY_A,
        audit=AuditService(db_session, user),
    )
    db_session.commit()
    db_session.refresh(allocation)
    assert cancelled == [allocation.id]
    assert allocation.status == AllocationStatus.CANCELLED
    assert allocation.work_order_operation_id is None, "and it is DETACHED, so the operation delete is FK-safe"


def test_nest_reimport_cancels_unconsumed_allocations(db_session: Session):
    user = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    wo = make_wo(db_session, fg, quantity_ordered=3)
    op = make_op(db_session, wo, make_work_center(db_session), operation_group="LASER")
    allocation = make_allocation(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=3)

    cancelled = cancel_allocations_for_operations(
        db_session,
        work_order=wo,
        operation_ids=[op.id],
        company_id=COMPANY_A,
        audit=AuditService(db_session, user),
    )
    db_session.commit()
    db_session.refresh(allocation)

    assert cancelled == [allocation.id]
    assert allocation.status == AllocationStatus.CANCELLED
    audit_rows = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.company_id == COMPANY_A,
            AuditLog.resource_type == "work_order_material_allocation",
        )
        .all()
    )
    assert any(r.extra_data and r.extra_data.get("reason") == "superseded_by_reimport" for r in audit_rows)


def test_work_order_soft_delete_cancels_open_allocations(client: TestClient, db_session: Session):
    admin = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    wo = make_wo(db_session, fg, quantity_ordered=3, status_=WorkOrderStatus.RELEASED)
    op = make_op(db_session, wo, make_work_center(db_session))
    allocation = make_allocation(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=3)

    resp = client.delete(f"/api/v1/work-orders/{wo.id}", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_204_NO_CONTENT, resp.text

    db_session.expire_all()
    allocation = db_session.get(WorkOrderMaterialAllocation, allocation.id)
    assert allocation.status == AllocationStatus.CANCELLED


def test_work_order_soft_delete_is_not_refused_by_consumed_tie(client: TestClient, db_session: Session):
    """Consumption STANDS — the material was used — but the delete still succeeds."""
    admin = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    wo = make_wo(db_session, fg, quantity_ordered=3, status_=WorkOrderStatus.RELEASED)
    op = make_op(db_session, wo, make_work_center(db_session), quantity_complete=3)
    allocation = make_allocation(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=3, qty_consumed=3.0)

    resp = client.delete(f"/api/v1/work-orders/{wo.id}", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_204_NO_CONTENT, resp.text

    db_session.expire_all()
    allocation = db_session.get(WorkOrderMaterialAllocation, allocation.id)
    assert allocation.status == AllocationStatus.CANCELLED
    assert allocation.qty_consumed == 3.0


def test_hard_delete_guard_asks_the_ledger_not_the_cache(client: TestClient, db_session: Session):
    """``qty_consumed`` is a documented CACHE; the FK has no ON DELETE.

    Keying the guard on the cache means any drift surfaces as an IntegrityError 500
    instead of the intended 409, so the guard reads the ledger directly.
    """
    admin = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    item = make_inventory(db_session, sheet, qty=20, lot="SHEET-HARDDEL")
    wo = make_wo(db_session, fg, quantity_ordered=3, status_=WorkOrderStatus.DRAFT)
    op = make_op(db_session, wo, make_work_center(db_session))
    # CACHE SAYS NOTHING WAS CONSUMED, but a ledger row points at the tie.
    allocation = make_allocation(db_session, wo, sheet, operation=op, qty_planned=3, qty_consumed=0.0)
    db_session.add(
        InventoryTransaction(
            company_id=COMPANY_A,
            inventory_item_id=item.id,
            part_id=sheet.id,
            transaction_type=TransactionType.ISSUE,
            quantity=-3,
            reference_type=OPERATION_REFERENCE_TYPE,
            reference_id=op.id,
            reference_number=wo.work_order_number,
            allocation_id=allocation.id,
            created_by=admin.id,
        )
    )
    db_session.commit()

    resp = client.delete(f"/api/v1/work-orders/{wo.id}?hard_delete=true", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
    # PR 3: the remedy named must be one that EXISTS, and the RETURN verb is
    # deliberately NOT it here -- a return APPENDS a compensating row carrying the same
    # allocation_id, so a fully returned tie is still ledger-backed and this guard still
    # fires (correctly: the hard delete would remove the tie those rows resolve through).
    detail = resp.json()["detail"]
    assert "Material movement is on the inventory ledger" in detail, detail
    assert "Soft delete instead" in detail, detail
    assert "Reverse consumption first" not in detail, detail
    assert db_session.get(WorkOrder, wo.id) is not None


def test_hard_delete_proceeds_when_no_ledger_row_references_the_tie(client: TestClient, db_session: Session):
    """The converse: a stale cache value must not block a delete the ledger permits."""
    admin = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    wo = make_wo(db_session, fg, quantity_ordered=3, status_=WorkOrderStatus.DRAFT)
    op = make_op(db_session, wo, make_work_center(db_session))
    allocation = make_allocation(db_session, wo, sheet, operation=op, qty_planned=3, qty_consumed=3.0)

    resp = client.delete(f"/api/v1/work-orders/{wo.id}?hard_delete=true", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_204_NO_CONTENT, resp.text
    assert db_session.get(WorkOrderMaterialAllocation, allocation.id) is None


def test_lifecycle_cancels_require_an_audit_service(db_session: Session):
    """Audit is not optional on the tie-cancel entry points (invariant #2).

    They previously defaulted ``audit=None``, which made an unaudited CANCEL a one-line
    mistake for the next caller.
    """
    make_user(db_session)
    fg = make_part(db_session)
    wo = make_wo(db_session, fg, quantity_ordered=1)

    with pytest.raises(TypeError):
        cancel_open_allocations_for_work_order(db_session, work_order=wo, company_id=COMPANY_A)
    with pytest.raises(TypeError):
        cancel_allocations_for_operations(db_session, work_order=wo, operation_ids=[1], company_id=COMPANY_A)


# ---------------------------------------------------------------------------
# Tie API
# ---------------------------------------------------------------------------


def _tie_url(wo_id: int, allocation_id: int = None) -> str:
    base = f"/api/v1/work-orders/{wo_id}/material-allocations"
    return f"{base}/{allocation_id}" if allocation_id else base


def test_create_list_patch_untie_round_trip(client: TestClient, db_session: Session):
    admin = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    wo = make_wo(db_session, fg, quantity_ordered=5)
    op = make_op(db_session, wo, make_work_center(db_session))

    resp = client.post(
        _tie_url(wo.id),
        json={
            "part_id": sheet.id,
            "work_order_operation_id": op.id,
            "source": "nest",
            "qty_planned": 5,
            "notes": "one sheet per run",
        },
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    created = resp.json()
    assert created["qty_per_run"] == 1.0, "operation-scoped ties default to 1.0/run"
    assert created["unit_of_measure"] == "sheets", "UoM snapshotted from the part"
    assert created["status"] == "open"
    assert created["operation_number"] == op.operation_number
    allocation_id = created["id"]

    resp = client.get(_tie_url(wo.id), headers=headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK
    assert [row["id"] for row in resp.json()] == [allocation_id]

    resp = client.patch(
        _tie_url(wo.id, allocation_id),
        json={"qty_per_run": 2.0, "notes": "two sheets per run"},
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["qty_per_run"] == 2.0

    resp = client.delete(_tie_url(wo.id, allocation_id), headers=headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["status"] == "cancelled"

    # Untie is audited as a DELETE on the tamper-evident chain.
    audit_row = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.company_id == COMPANY_A,
            AuditLog.resource_type == "work_order_material_allocation",
            AuditLog.resource_id == allocation_id,
            AuditLog.action == AuditService.ACTIONS["DELETE"],
        )
        .first()
    )
    assert audit_row is not None
    assert audit_row.extra_data["new_status"] == "cancelled"
    assert audit_row.extra_data["tombstone"] == "status"


def test_duplicate_open_tie_is_a_readable_409(client: TestClient, db_session: Session):
    admin = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    wo = make_wo(db_session, fg, quantity_ordered=5)
    op = make_op(db_session, wo, make_work_center(db_session))
    body = {"part_id": sheet.id, "work_order_operation_id": op.id, "source": "nest", "qty_planned": 5}

    assert client.post(_tie_url(wo.id), json=body, headers=headers_for(admin)).status_code == 201
    resp = client.post(_tie_url(wo.id), json=body, headers=headers_for(admin))
    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
    assert "already tied" in resp.json()["detail"]


def test_retie_after_untie_is_allowed(client: TestClient, db_session: Session):
    """The unique indexes are PARTIAL on status=OPEN, so a re-tie must work."""
    admin = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    wo = make_wo(db_session, fg, quantity_ordered=5)
    op = make_op(db_session, wo, make_work_center(db_session))
    body = {"part_id": sheet.id, "work_order_operation_id": op.id, "source": "nest", "qty_planned": 5}

    first = client.post(_tie_url(wo.id), json=body, headers=headers_for(admin)).json()
    assert client.delete(_tie_url(wo.id, first["id"]), headers=headers_for(admin)).status_code == 200
    resp = client.post(_tie_url(wo.id), json=body, headers=headers_for(admin))
    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    assert resp.json()["id"] != first["id"]


def test_untie_is_refused_once_material_is_consumed(client: TestClient, db_session: Session):
    """PR 4 re-keyed this guard from ``qty_consumed`` to the SIGNED ledger net, so the
    tie is driven through a REAL consumption rather than a hand-set cache value.

    That is not a weaker fixture, it is the only honest one: the refusal exists to stop
    an untie stranding ``inventory_transactions.allocation_id`` rows against a tombstone,
    so the ledger rows have to be there for the refusal to be about anything. A tie whose
    cache reads above an EMPTY ledger has nothing to strand and is now untieable -- the
    same answer the hard-delete guard has given since PR 1; see
    ``test_untie_permitted_when_the_cache_reads_above_an_empty_ledger`` below.
    """
    admin = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    lot = make_inventory(db_session, sheet, qty=10, lot="SHEET-UNTIE-409")
    wo = make_wo(db_session, fg, quantity_ordered=5, quantity_complete=2)
    op = make_op(db_session, wo, make_work_center(db_session), quantity_complete=2)
    allocation = make_allocation(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=5)

    run_effects(db_session, wo, admin)
    db_session.expire_all()
    assert [t.quantity for t in consumption_txns(db_session, op.id)] == [-2], "the ledger must really hold 2 out"
    db_session.refresh(lot)
    assert lot.quantity_on_hand == 8

    resp = client.delete(_tie_url(wo.id, allocation.id), headers=headers_for(admin))
    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
    # PR 3: untie stays refused on its own terms -- cancelling a tie that moved stock,
    # without moving it back, strands the ledger's allocation_id rows against a tombstone
    # with no account of where the material went. What changed is that the refusal now
    # names a verb that exists and does exactly what the caller wants.
    detail = resp.json()["detail"]
    assert "return_and_untie" in detail, detail
    assert "Reverse consumption first" not in detail, detail
    db_session.expire_all()
    assert db_session.get(WorkOrderMaterialAllocation, allocation.id).status == AllocationStatus.OPEN


def test_untie_permitted_when_the_cache_reads_above_an_empty_ledger(client: TestClient, db_session: Session):
    """The converse of the guard's re-key, and the asymmetry it removed.

    ``qty_consumed`` is a documented CACHE; the ledger is authoritative. Until PR 4 this
    endpoint was the only guard of its class still keyed on the cache -- hard delete has
    read the ledger since PR 1 and nest re-import since PR 3 -- so a drifted cache value
    manufactured a 409 on a tie whose untie would have stranded nothing.
    """
    admin = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    wo = make_wo(db_session, fg, quantity_ordered=5)
    op = make_op(db_session, wo, make_work_center(db_session))
    allocation = make_allocation(db_session, wo, sheet, operation=op, qty_planned=5, qty_consumed=2.0)
    assert consumption_txns(db_session, op.id) == [], "no ledger row backs this cache value"

    resp = client.delete(_tie_url(wo.id, allocation.id), headers=headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    db_session.expire_all()
    assert db_session.get(WorkOrderMaterialAllocation, allocation.id).status == AllocationStatus.CANCELLED


def test_pinned_lot_of_a_different_uom_part_is_422(client: TestClient, db_session: Session):
    admin = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    bar = make_part(db_session, uom="feet", part_type="raw_material")
    bar_lot = make_inventory(db_session, bar, qty=10, lot="BAR-LOT")
    wo = make_wo(db_session, fg, quantity_ordered=5)
    op = make_op(db_session, wo, make_work_center(db_session))

    resp = client.post(
        _tie_url(wo.id),
        json={
            "part_id": sheet.id,
            "work_order_operation_id": op.id,
            "source": "nest",
            "qty_planned": 5,
            "pinned_inventory_item_id": bar_lot.id,
        },
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, resp.text
    assert "Unit-of-measure mismatch" in resp.json()["detail"]


def test_post_qty_per_run_on_a_work_order_scoped_tie_is_422(client: TestClient, db_session: Session):
    """POST and PATCH must answer the same input the same way.

    POST used to silently discard ``qty_per_run`` and store NULL, so a planner who typed
    a per-run rule got a tie that had none — while PATCH refused the identical
    combination with 422.
    """
    admin = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    wo = make_wo(db_session, fg, quantity_ordered=5)

    resp = client.post(
        _tie_url(wo.id),
        json={"part_id": sheet.id, "source": "manual", "qty_planned": 5, "qty_per_run": 2.0},
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, resp.text
    assert "operation-scoped ties only" in resp.json()["detail"]
    assert db_session.query(WorkOrderMaterialAllocation).filter_by(work_order_id=wo.id).count() == 0

    # PATCH already refused it; the two contracts now match verbatim.
    allocation = make_allocation(db_session, wo, sheet, operation=None, qty_planned=5)
    patch_resp = client.patch(_tie_url(wo.id, allocation.id), json={"qty_per_run": 2.0}, headers=headers_for(admin))
    assert patch_resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    assert patch_resp.json()["detail"] == resp.json()["detail"]


def test_work_order_scoped_tie_is_409_only_on_a_LEGACY_one_shot_row(client: TestClient, db_session: Session):
    """REWRITE of ``test_work_order_scoped_tie_on_an_already_issued_part_is_409``.

    The guard is unchanged and still keys on ``reference_type='work_order'`` ISSUE rows.
    What changed is WHAT CAN PRODUCE ONE: nothing does any more, so the refusal is now a
    permanent LEGACY fence rather than the ordinary consequence of a backflush. Both
    halves are asserted, because each on its own would be misleading:

    * a hand-built pre-4.4 ``('work_order', ISSUE)`` row — the only shape that can now
      reach it — still 409s, with the corrected wording and the remedy named;
    * a REAL backflush no longer produces that state at all, so the same tie is
      ACCEPTED (201) on a work order whose component left stock under the new shape.
      Under the old engine this exact call was the 409.
    """
    admin = make_user(db_session)
    fg = make_part(db_session, backflush=True)
    stock = make_part(db_session, part_type="purchased")
    lot = make_inventory(db_session, stock, qty=100, lot="ALREADY-ISSUED")
    _bom_with_component(db_session, fg, stock, qty=2.0)

    legacy_wo = make_wo(db_session, fg, quantity_ordered=5, quantity_complete=5)
    legacy_op = make_op(db_session, legacy_wo, make_work_center(db_session), quantity_complete=5)
    db_session.add(
        InventoryTransaction(
            company_id=COMPANY_A,
            inventory_item_id=lot.id,
            part_id=stock.id,
            transaction_type=TransactionType.ISSUE,
            quantity=-10.0,
            reference_type=WORK_ORDER_REFERENCE_TYPE,
            reference_id=legacy_wo.id,
            reference_number=legacy_wo.work_order_number,
            unit_cost=2.0,
            total_cost=20.0,
            created_by=admin.id,
        )
    )
    db_session.commit()

    resp = client.post(
        _tie_url(legacy_wo.id),
        json={"part_id": stock.id, "source": "manual", "qty_planned": 4},
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
    detail = resp.json()["detail"]
    assert "one-time issue recorded" in detail
    assert "operation level" in detail, "the remedy must be named"

    # The operation-scoped form is the remedy, and it is still allowed.
    ok = client.post(
        _tie_url(legacy_wo.id),
        json={
            "part_id": stock.id,
            "work_order_operation_id": legacy_op.id,
            "source": "manual",
            "qty_planned": 4,
        },
        headers=headers_for(admin),
    )
    assert ok.status_code == status.HTTP_201_CREATED, ok.text

    # And the negative half: a work order the CURRENT engine backflushed writes
    # ``work_order_backflush``, which this guard does not match, so the refusal it used
    # to raise is now unreachable through any supported verb.
    fresh_wo = make_wo(db_session, fg, quantity_ordered=5, quantity_complete=5)
    make_op(db_session, fresh_wo, make_work_center(db_session), quantity_complete=5)
    run_effects(db_session, fresh_wo, admin)
    fresh_wo.status = WorkOrderStatus.IN_PROGRESS  # keep the terminal guard out of the way
    db_session.commit()
    assert (
        db_session.query(InventoryTransaction)
        .filter(
            InventoryTransaction.company_id == COMPANY_A,
            InventoryTransaction.reference_type == BACKFLUSH_REFERENCE_TYPE,
            InventoryTransaction.reference_id == fresh_wo.id,
            InventoryTransaction.transaction_type == TransactionType.ISSUE,
        )
        .count()
        == 1
    ), "the backflush really ran, or the 201 below proves nothing"

    allowed = client.post(
        _tie_url(fresh_wo.id),
        json={"part_id": stock.id, "source": "manual", "qty_planned": 4},
        headers=headers_for(admin),
    )
    assert allowed.status_code == status.HTTP_201_CREATED, allowed.text


@pytest.mark.parametrize(
    "terminal_status",
    [WorkOrderStatus.COMPLETE, WorkOrderStatus.CLOSED, WorkOrderStatus.CANCELLED],
)
def test_tie_on_a_terminal_work_order_is_409(client: TestClient, db_session: Session, terminal_status: WorkOrderStatus):
    """Every completion path refuses a terminal WO, so a tie made now can never consume."""
    admin = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    wo = make_wo(db_session, fg, quantity_ordered=5, status_=terminal_status)
    op = make_op(db_session, wo, make_work_center(db_session))

    resp = client.post(
        _tie_url(wo.id),
        json={"part_id": sheet.id, "work_order_operation_id": op.id, "source": "nest", "qty_planned": 5},
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
    assert terminal_status.value in resp.json()["detail"]
    assert db_session.query(WorkOrderMaterialAllocation).filter_by(work_order_id=wo.id).count() == 0


def test_existing_tie_on_a_now_terminal_work_order_stays_editable(client: TestClient, db_session: Session):
    """The refusal is on CREATE only — a historical tie must stay readable and fixable."""
    admin = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    wo = make_wo(db_session, fg, quantity_ordered=5)
    op = make_op(db_session, wo, make_work_center(db_session))
    allocation = make_allocation(db_session, wo, sheet, operation=op, qty_planned=5)

    wo.status = WorkOrderStatus.COMPLETE
    db_session.commit()

    assert client.get(_tie_url(wo.id), headers=headers_for(admin)).status_code == status.HTTP_200_OK
    resp = client.patch(
        _tie_url(wo.id, allocation.id), json={"notes": "corrected after close"}, headers=headers_for(admin)
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text


def test_cross_tenant_references_are_refused(client: TestClient, db_session: Session):
    """A tie naming another company's operation, part or lot must never be created."""
    admin_a = make_user(db_session, company_id=COMPANY_A)
    fg = make_part(db_session, company_id=COMPANY_A)
    wo_a = make_wo(db_session, fg, quantity_ordered=5, company_id=COMPANY_A)
    op_a = make_op(db_session, wo_a, make_work_center(db_session), company_id=COMPANY_A)

    part_b = make_part(db_session, uom="sheets", part_type="raw_material", company_id=COMPANY_B)
    sheet_a = make_part(db_session, uom="sheets", part_type="raw_material", company_id=COMPANY_A)
    lot_b = make_inventory(db_session, part_b, qty=10, lot="B-LOT", company_id=COMPANY_B)
    wo_b = make_wo(db_session, part_b, quantity_ordered=5, company_id=COMPANY_B)
    op_b = make_op(db_session, wo_b, make_work_center(db_session, company_id=COMPANY_B), company_id=COMPANY_B)

    # Another tenant's material part.
    resp = client.post(
        _tie_url(wo_a.id),
        json={"part_id": part_b.id, "source": "manual", "qty_planned": 1},
        headers=headers_for(admin_a),
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text

    # Another tenant's operation.
    resp = client.post(
        _tie_url(wo_a.id),
        json={
            "part_id": sheet_a.id,
            "work_order_operation_id": op_b.id,
            "source": "manual",
            "qty_planned": 1,
        },
        headers=headers_for(admin_a),
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text

    # Another tenant's lot.
    resp = client.post(
        _tie_url(wo_a.id),
        json={
            "part_id": sheet_a.id,
            "work_order_operation_id": op_a.id,
            "source": "manual",
            "qty_planned": 1,
            "pinned_inventory_item_id": lot_b.id,
        },
        headers=headers_for(admin_a),
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text

    # And another tenant's work order is simply not found.
    resp = client.get(_tie_url(wo_b.id), headers=headers_for(admin_a))
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text


def test_patch_and_delete_never_reach_across_tenants(client: TestClient, db_session: Session):
    """The two MUTATING tie verbs must refuse another tenant's row.

    The test above covers POST and the list GET. PATCH and DELETE — the verbs that could
    actually alter another company's tie — had no lock at all. Both shapes are checked
    because they are refused by DIFFERENT filters: routing through the foreign work order
    fails in ``_load_work_order``, while naming the foreign allocation id under the
    caller's OWN work order gets past that and must be caught by ``_load_allocation``.
    """
    admin_a = make_user(db_session, company_id=COMPANY_A)
    fg_a = make_part(db_session, company_id=COMPANY_A)
    wo_a = make_wo(db_session, fg_a, quantity_ordered=5, company_id=COMPANY_A)

    fg_b = make_part(db_session, company_id=COMPANY_B)
    part_b = make_part(db_session, uom="sheets", part_type="raw_material", company_id=COMPANY_B)
    wo_b = make_wo(db_session, fg_b, quantity_ordered=5, company_id=COMPANY_B)
    op_b = make_op(db_session, wo_b, make_work_center(db_session, company_id=COMPANY_B), company_id=COMPANY_B)
    tie_b = make_allocation(
        db_session, wo_b, part_b, operation=op_b, qty_per_run=1.0, qty_planned=5, company_id=COMPANY_B
    )

    foreign = headers_for(admin_a)
    edit = {"qty_planned": 99}
    attempts = (
        ("PATCH via their work order", client.patch(_tie_url(wo_b.id, tie_b.id), json=edit, headers=foreign)),
        ("DELETE via their work order", client.delete(_tie_url(wo_b.id, tie_b.id), headers=foreign)),
        ("PATCH via my work order", client.patch(_tie_url(wo_a.id, tie_b.id), json=edit, headers=foreign)),
        ("DELETE via my work order", client.delete(_tie_url(wo_a.id, tie_b.id), headers=foreign)),
    )
    for label, resp in attempts:
        assert resp.status_code == status.HTTP_404_NOT_FOUND, f"{label}: {resp.status_code} {resp.text}"

    db_session.expire_all()
    survivor = db_session.get(WorkOrderMaterialAllocation, tie_b.id)
    assert survivor.status == AllocationStatus.OPEN, "the foreign tie must be untouched"
    assert survivor.qty_planned == 5


def test_operator_may_read_but_not_mutate_ties(client: TestClient, db_session: Session):
    operator = make_user(db_session, role=UserRole.OPERATOR)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    wo = make_wo(db_session, fg, quantity_ordered=5)
    op = make_op(db_session, wo, make_work_center(db_session))
    allocation = make_allocation(db_session, wo, sheet, operation=op, qty_planned=5)

    assert client.get(_tie_url(wo.id), headers=headers_for(operator)).status_code == status.HTTP_200_OK

    resp = client.post(
        _tie_url(wo.id),
        json={"part_id": sheet.id, "source": "manual", "qty_planned": 1},
        headers=headers_for(operator),
    )
    assert resp.status_code == status.HTTP_403_FORBIDDEN
    assert (
        client.patch(_tie_url(wo.id, allocation.id), json={"qty_planned": 9}, headers=headers_for(operator)).status_code
        == status.HTTP_403_FORBIDDEN
    )
    assert (
        client.delete(_tie_url(wo.id, allocation.id), headers=headers_for(operator)).status_code
        == status.HTTP_403_FORBIDDEN
    )


def test_supervisor_may_tie(client: TestClient, db_session: Session):
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    wo = make_wo(db_session, fg, quantity_ordered=5)

    resp = client.post(
        _tie_url(wo.id),
        json={"part_id": sheet.id, "source": "manual", "qty_planned": 5},
        headers=headers_for(supervisor),
    )
    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    assert resp.json()["qty_per_run"] is None, "work-order-scoped ties are not run-scaled"


# ---------------------------------------------------------------------------
# FK enforcement — the nest-re-import operation delete (round-2 BLOCKER B1)
#
# ``sqlite_foreign_keys_enforced`` now lives in ``tests/api/fk_test_helpers.py``
# (imported at the top of this file). It moved out when the PR-2 nest-import tests
# needed the same pragma: a test module imported BY another test module is loaded
# twice under two names and loses pytest's assertion rewriting, so the helper is
# homed where both can import it once.
# ---------------------------------------------------------------------------


def test_operation_delete_after_tie_cancel_survives_foreign_key_enforcement(db_session: Session):
    """A nest re-import must be able to DELETE the operations whose ties it cancelled.

    ``work_order_material_allocations.work_order_operation_id`` is a plain FK with no
    ``ON DELETE`` (migration 074) and no parent backref, so SQLAlchemy will not null it:
    a cancelled-but-still-pointing tie makes ``db.delete(operation)`` raise
    ``IntegrityError`` on Postgres, which the import endpoint turns into a misleading
    400. ``cancel_allocations_for_operations`` therefore CLEARS the column.

    Covers BOTH shapes the wipe can meet, because the round-2 fix only handled the
    first: a tie that is still OPEN (cancelled + detached here), and one that was
    ALREADY cancelled by a manual untie (nothing to cancel, but it still holds the FK
    and so must still be detached). Skipping the second left the crash reachable
    through supported verbs — tie, untie, re-import — and permanently un-rebuildable.

    The positive control below is what makes this test non-vacuous: with the tie still
    attached the delete DOES raise, proving FK enforcement is actually live for this
    test rather than silently off (which is the suite-wide default).
    """
    user = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    wo = make_wo(db_session, fg, quantity_ordered=3)
    wc = make_work_center(db_session)

    control_op = make_op(db_session, wo, wc, sequence=10, operation_group="LASER")
    make_allocation(db_session, wo, sheet, operation=control_op, qty_per_run=1.0, qty_planned=3)
    subject_op = make_op(db_session, wo, wc, sequence=20, operation_group="LASER")
    subject_sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    allocation = make_allocation(db_session, wo, subject_sheet, operation=subject_op, qty_per_run=1.0, qty_planned=3)
    # The shape a manual untie leaves behind: CANCELLED, qty_consumed 0, and still
    # pointing at its operation (delete_material_allocation deliberately keeps the id).
    untied_op = make_op(db_session, wo, wc, sequence=30, operation_group="LASER")
    untied_sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    untied = make_allocation(
        db_session,
        wo,
        untied_sheet,
        operation=untied_op,
        qty_per_run=1.0,
        qty_planned=3,
        status_=AllocationStatus.CANCELLED,
    )

    with sqlite_foreign_keys_enforced(db_session):
        # POSITIVE CONTROL: an attached tie makes the operation delete FK-violate.
        nested = db_session.begin_nested()
        with pytest.raises(IntegrityError):
            db_session.delete(control_op)
            db_session.flush()
        nested.rollback()

        # SECOND POSITIVE CONTROL: an ALREADY-CANCELLED tie violates it just the same —
        # the FK does not care about the tombstone.
        nested = db_session.begin_nested()
        with pytest.raises(IntegrityError):
            db_session.delete(untied_op)
            db_session.flush()
        nested.rollback()

        # THE PATH THE IMPORT TAKES: cancel the ties, then wipe the operations.
        cancelled = cancel_allocations_for_operations(
            db_session,
            work_order=wo,
            operation_ids=[subject_op.id, untied_op.id],
            company_id=COMPANY_A,
            audit=AuditService(db_session, user),
        )
        # Only the OPEN tie is CANCELLED; the already-cancelled one is detached, not
        # re-cancelled (no second tombstone, no duplicate DELETE row).
        assert cancelled == [allocation.id]

        db_session.delete(subject_op)
        db_session.delete(untied_op)
        db_session.flush()
        db_session.commit()

        db_session.expire_all()
        allocation = db_session.get(WorkOrderMaterialAllocation, allocation.id)
        assert allocation is not None, "the tie row must survive the operation delete"
        assert allocation.status == AllocationStatus.CANCELLED
        assert allocation.work_order_operation_id is None

        untied = db_session.get(WorkOrderMaterialAllocation, untied.id)
        assert untied is not None, "the already-cancelled tie must survive too"
        assert untied.status == AllocationStatus.CANCELLED
        assert untied.work_order_operation_id is None


def test_detaching_an_already_cancelled_tie_is_audited(db_session: Session):
    """The detach is a state change on a tenant row, so it owes a chain row (invariant 2).

    An already-cancelled tie has no status change to record — but clearing
    ``work_order_operation_id`` erases the tie's only remaining evidence of what it was
    scoped to, so the UPDATE row carries the old id, the cleared marker and the reason.
    It is an UPDATE and not a second DELETE on purpose: the restore discriminator reads
    the tie's most recent DELETE row, and a second one would rewrite that history.
    """
    user = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    wo = make_wo(db_session, fg, quantity_ordered=3)
    op = make_op(db_session, wo, make_work_center(db_session), operation_group="LASER")
    untied = make_allocation(
        db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=3, status_=AllocationStatus.CANCELLED
    )

    cancelled = cancel_allocations_for_operations(
        db_session,
        work_order=wo,
        operation_ids=[op.id],
        company_id=COMPANY_A,
        audit=AuditService(db_session, user),
    )
    db_session.commit()
    db_session.refresh(untied)

    assert cancelled == []
    assert untied.work_order_operation_id is None

    rows = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.company_id == COMPANY_A,
            AuditLog.resource_type == "work_order_material_allocation",
            AuditLog.resource_id == untied.id,
        )
        .order_by(AuditLog.id)
        .all()
    )
    assert [r.action for r in rows] == ["UPDATE"], "the detach is an UPDATE, never a second DELETE"
    detach = rows[0]
    assert detach.old_values["work_order_operation_id"] == op.id
    assert detach.new_values["work_order_operation_id"] is None
    assert detach.extra_data["reason"] == "superseded_by_reimport"
    assert detach.extra_data["work_order_operation_id"] == op.id
    assert detach.extra_data["work_order_operation_id_cleared"] is True


def test_reimport_guard_sees_consumption_on_an_already_cancelled_tie(db_session: Session):
    """The 409 must fire for a CANCELLED tie that carries consumption, not just an OPEN one.

    ``cancel_open_allocations_for_work_order`` (the work-order soft delete) cancels every
    OPEN tie REGARDLESS of ``qty_consumed`` — consumption already posted stands. While the
    wipe query skipped CANCELLED rows, such a tie sailed past the guard, its operation was
    deleted, and the ISSUE rows carrying that operation's lot genealogy were orphaned:
    exactly what the 409 exists to prevent, bypassed by a supported verb.

    The consumption is driven for real because PR 3 re-keyed the guard to the LEDGER (see
    ``test_nest_reimport_guard_reads_the_ledger_not_the_qty_consumed_cache``); those ledger
    rows are the thing the orphaning would strand.
    """
    user = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    make_inventory(db_session, sheet, qty=10, lot="REIMPORT-CANCELLED")
    wo = make_wo(db_session, fg, quantity_ordered=3, quantity_complete=3)
    op = make_op(db_session, wo, make_work_center(db_session), quantity_complete=3, operation_group="LASER")
    allocation = make_allocation(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=3)

    run_effects(db_session, wo, user)
    db_session.refresh(allocation)
    assert allocation.qty_consumed == 3.0

    # The state a work-order soft delete leaves: CANCELLED, consumption intact, still
    # pointing at the operation.
    cancel_open_allocations_for_work_order(
        db_session, work_order=wo, company_id=COMPANY_A, audit=AuditService(db_session, user)
    )
    db_session.commit()
    db_session.refresh(allocation)
    assert allocation.status == AllocationStatus.CANCELLED
    assert allocation.qty_consumed == 3.0
    assert allocation.work_order_operation_id == op.id

    with pytest.raises(MaterialAllocationConsumedError):
        cancel_allocations_for_operations(
            db_session,
            work_order=wo,
            operation_ids=[op.id],
            company_id=COMPANY_A,
            audit=AuditService(db_session, user),
        )


def test_reimport_against_a_soft_deleted_laser_child_is_refused(client: TestClient, db_session: Session):
    """A parent-addressed re-import must not resurrect a soft-deleted laser child.

    ``_ensure_laser_child_work_order`` had no ``is_deleted`` filter, so a parent-addressed
    import resolved the DELETED child and the import's very next act force-set it back to
    ``RELEASED`` — a deleted work order back on the floor with none of the restore path's
    controls, and (because the soft delete cancelled its ties and no re-open ran) running
    with its material demand silently closed out. It is also the door through which a
    CANCELLED-with-consumption tie reached the operation wipe.

    409, not 404: the child exists, and the remedy is to restore it so its nests and ties
    stay on one work order rather than forking into a second child.
    """
    admin = make_user(db_session)
    fg = make_part(db_session)
    parent = make_wo(db_session, fg, quantity_ordered=3, status_=WorkOrderStatus.RELEASED)
    make_work_center(db_session)  # an active laser work center must exist for the import
    child = make_wo(db_session, fg, quantity_ordered=1, status_=WorkOrderStatus.RELEASED)
    child.parent_work_order_id = parent.id
    child.work_order_type = WorkOrderType.LASER_CUTTING.value
    db_session.commit()

    assert client.delete(f"/api/v1/work-orders/{child.id}", headers=headers_for(admin)).status_code == 204

    # Both parent-addressed nest write paths funnel through the same helper
    # (`_resolve_laser_target` -> `_ensure_laser_child_work_order`).
    resp = client.post(
        f"/api/v1/work-orders/{parent.id}/laser-nests/manual",
        headers=headers_for(admin),
        json={"cnc_number": "REIMPORT-1", "planned_runs": 2},
    )
    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
    detail = resp.json()["detail"]
    assert child.work_order_number in detail
    # The refusal has to name the remedy AND who can perform it: restore is
    # ADMIN/MANAGER while a SUPERVISOR may run this import, so a bare "restore it"
    # would send that caller straight into a 403.
    assert "/work-orders/{id}/restore" in detail
    assert "admin or manager" in detail

    db_session.expire_all()
    child = db_session.get(WorkOrder, child.id)
    assert child.is_deleted, "the deleted child must not have been rebuilt"
    # No second laser child was created alongside it either.
    live_children = (
        db_session.query(WorkOrder)
        .filter(
            WorkOrder.company_id == COMPANY_A,
            WorkOrder.parent_work_order_id == parent.id,
            WorkOrder.is_deleted == False,  # noqa: E712
        )
        .all()
    )
    assert live_children == []


def test_laser_child_is_still_found_when_it_is_live(client: TestClient, db_session: Session):
    """The converse control: the ``is_deleted`` filter must not break find-or-create.

    Without this the 409 above could pass while every healthy import quietly forked a
    second laser child.
    """
    admin = make_user(db_session)
    fg = make_part(db_session)
    parent = make_wo(db_session, fg, quantity_ordered=3, status_=WorkOrderStatus.RELEASED)
    make_work_center(db_session)

    first = client.post(
        f"/api/v1/work-orders/{parent.id}/laser-nests/manual",
        headers=headers_for(admin),
        json={"cnc_number": "LIVE-1", "planned_runs": 2},
    )
    assert first.status_code == status.HTTP_201_CREATED, first.text
    second = client.post(
        f"/api/v1/work-orders/{parent.id}/laser-nests/manual",
        headers=headers_for(admin),
        json={"cnc_number": "LIVE-2", "planned_runs": 3},
    )
    assert second.status_code == status.HTTP_201_CREATED, second.text

    children = (
        db_session.query(WorkOrder)
        .filter(WorkOrder.company_id == COMPANY_A, WorkOrder.parent_work_order_id == parent.id)
        .all()
    )
    assert len(children) == 1, "find-or-create must reuse the live laser child"


def test_cancelled_tie_records_the_operation_it_was_detached_from(db_session: Session):
    """Clearing the FK must not erase the tie's original scope from the audit chain."""
    user = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    wo = make_wo(db_session, fg, quantity_ordered=3)
    op = make_op(db_session, wo, make_work_center(db_session), operation_group="LASER")
    allocation = make_allocation(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=3)

    cancel_allocations_for_operations(
        db_session,
        work_order=wo,
        operation_ids=[op.id],
        company_id=COMPANY_A,
        audit=AuditService(db_session, user),
    )
    db_session.commit()

    row = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.company_id == COMPANY_A,
            AuditLog.resource_type == "work_order_material_allocation",
            AuditLog.resource_id == allocation.id,
            AuditLog.action == "DELETE",
        )
        .one()
    )
    assert row.extra_data["work_order_operation_id"] == op.id
    assert row.extra_data["work_order_operation_id_cleared"] is True
    assert row.old_values["work_order_operation_id"] == op.id


def test_tie_list_echoes_the_operation_a_reimport_detached_it_from(client: TestClient, db_session: Session):
    """A detached tie must not read back as one that was always work-order-scoped.

    Both shapes serialize ``work_order_operation_id: null``, so without an echo the API
    lost the distinction entirely and the original scope existed only on the hash chain.
    """
    admin = make_user(db_session)
    fg = make_part(db_session)
    detached_part = make_part(db_session, uom="sheets", part_type="raw_material")
    wo_scoped_part = make_part(db_session, uom="sheets", part_type="raw_material")
    wo = make_wo(db_session, fg, quantity_ordered=3)
    op = make_op(db_session, wo, make_work_center(db_session), operation_group="LASER")
    detached = make_allocation(db_session, wo, detached_part, operation=op, qty_per_run=1.0, qty_planned=3)
    always_wo_scoped = make_allocation(db_session, wo, wo_scoped_part, operation=None, qty_planned=3)

    cancel_allocations_for_operations(
        db_session,
        work_order=wo,
        operation_ids=[op.id],
        company_id=COMPANY_A,
        audit=AuditService(db_session, admin),
    )
    db_session.commit()

    resp = client.get(_tie_url(wo.id), headers=headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    by_id = {row["id"]: row for row in resp.json()}

    assert by_id[detached.id]["work_order_operation_id"] is None
    assert by_id[detached.id]["detached_from_operation_id"] == op.id
    assert by_id[always_wo_scoped.id]["work_order_operation_id"] is None
    assert by_id[always_wo_scoped.id]["detached_from_operation_id"] is None


# ---------------------------------------------------------------------------
# A work-order-scoped tie honors its lot pin (round-2 BLOCKER B2)
# ---------------------------------------------------------------------------


def _wo_scoped_issue_rows(db: Session, wo: WorkOrder, part: Part) -> list[InventoryTransaction]:
    """Component ISSUE rows a work-order-scoped tie produced, under BOTH keyed shapes.

    ``work_order_backflush`` is what the leg writes now; ``work_order`` stays in the
    predicate so a hand-built legacy row (or a regression that started writing that shape
    again) is still visible to every caller.
    """
    return (
        db.query(InventoryTransaction)
        .filter(
            InventoryTransaction.company_id == COMPANY_A,
            InventoryTransaction.reference_type.in_(WORK_ORDER_ID_KEYED_REFERENCE_TYPES),
            InventoryTransaction.reference_id == wo.id,
            InventoryTransaction.part_id == part.id,
            InventoryTransaction.transaction_type == TransactionType.ISSUE,
        )
        .order_by(InventoryTransaction.id)
        .all()
    )


def test_work_order_scoped_tie_consumes_from_its_pinned_lot(db_session: Session):
    """The pin is a lot-directed instruction on BOTH tie shapes, not just the per-run one.

    Unpinned, this leg would walk FIFO and take the first lot, so pinning the SECOND lot
    is a real discriminator: before this, the tie's pin was dropped on the way into the
    demand object and the ledger issued from the wrong lot while the operator followed
    the pin — the as-built genealogy naming material never touched (AS9100D 8.5.2).
    """
    user = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    first_lot = make_inventory(db_session, sheet, qty=10, lot="LOT-FIFO-FIRST")
    pinned_lot = make_inventory(db_session, sheet, qty=10, lot="LOT-HEAT-CERT")
    wo = make_wo(db_session, fg, quantity_ordered=3, quantity_complete=3)
    allocation = make_allocation(db_session, wo, sheet, operation=None, qty_planned=3.0, pinned=pinned_lot)

    run_effects(db_session, wo, user)
    db_session.expire_all()

    rows = _wo_scoped_issue_rows(db_session, wo, sheet)
    assert len(rows) == 1
    assert rows[0].lot_number == "LOT-HEAT-CERT"
    assert rows[0].allocation_id == allocation.id
    assert db_session.get(InventoryItem, pinned_lot.id).quantity_on_hand == 7
    assert db_session.get(InventoryItem, first_lot.id).quantity_on_hand == 10, "FIFO lot must be untouched"
    assert db_session.get(WorkOrderMaterialAllocation, allocation.id).qty_consumed == 3.0


def test_work_order_scoped_pinned_lot_held_after_pinning_is_audited(db_session: Session):
    """A pinned lot held AFTER the tie was created still consumes — and says so.

    ``is_consumable_item`` was never consulted on this leg, so a work-order-scoped tie
    could consume ``on_hold`` / ``quarantine`` / ``rejected`` material into an as-built
    record with no ``HELD_MATERIAL_CONSUMED`` row at all (AS9100D 8.7). The per-run
    engine has always recorded it; the two legs now agree.
    """
    user = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    lot = make_inventory(db_session, sheet, qty=10, lot="LOT-QUARANTINED")
    wo = make_wo(db_session, fg, quantity_ordered=2, quantity_complete=2)
    allocation = make_allocation(db_session, wo, sheet, operation=None, qty_planned=2.0, pinned=lot)

    # Held AFTER pinning — the tie endpoint 422s a pin of an already-held lot.
    lot.status = "quarantine"
    db_session.commit()

    run_effects(db_session, wo, user)
    db_session.expire_all()

    rows = _wo_scoped_issue_rows(db_session, wo, sheet)
    assert len(rows) == 1 and rows[0].lot_number == "LOT-QUARANTINED"
    assert db_session.get(InventoryItem, lot.id).quantity_on_hand == 8

    held = (
        db_session.query(AuditLog)
        .filter(AuditLog.company_id == COMPANY_A, AuditLog.action == HELD_MATERIAL_CONSUMED_AUDIT_ACTION)
        .all()
    )
    assert len(held) == 1
    assert held[0].extra_data["allocation_id"] == allocation.id
    assert held[0].extra_data["item_status"] == "quarantine"
    assert held[0].extra_data["work_order_operation_id"] is None, "a work-order-scoped tie has no operation"


def test_available_pinned_lot_on_a_work_order_scoped_tie_records_no_held_row(db_session: Session):
    user = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    lot = make_inventory(db_session, sheet, qty=10, lot="LOT-OK")
    wo = make_wo(db_session, fg, quantity_ordered=2, quantity_complete=2)
    make_allocation(db_session, wo, sheet, operation=None, qty_planned=2.0, pinned=lot)

    run_effects(db_session, wo, user)

    assert (
        db_session.query(AuditLog)
        .filter(AuditLog.company_id == COMPANY_A, AuditLog.action == HELD_MATERIAL_CONSUMED_AUDIT_ACTION)
        .count()
        == 0
    )


def test_unpinned_work_order_scoped_tie_skips_a_held_lot_and_discloses_it(db_session: Session):
    """REWRITE of ``..._records_a_held_lot_it_consumes``: the UNPINNED leg no longer can.

    The behaviour it locked — this leg selecting with NO ``status`` predicate, consuming
    an ``on_hold`` / ``quarantine`` / ``rejected`` lot, and merely RECORDING it with a
    ``HELD_MATERIAL_CONSUMED`` row — was a deliberate hold-your-nose compromise: the leg
    was live, and tightening its SELECT would have newly excluded legacy NULL-status rows
    as collateral. PR 4.4 removes that objection (``COALESCE(status, 'available')`` keeps
    the legacy rows eligible) and adopts the per-run engine's rule, which is the AS9100D
    8.7-correct one: segregated material is NOT drawn into product.

    The replacement obligation is DISCLOSURE, and it is the whole point of the rewrite. A
    part whose only stock is held must not report a bare shortage against material
    physically on the rack, so the shortage row carries ``held_quantity_skipped`` and the
    held lot numbers. The PINNED leg is unchanged and still writes the 8.7 row — see the
    test two above.
    """
    user = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    held_lot = make_inventory(db_session, sheet, qty=10, lot="LOT-ON-HOLD")
    held_lot.status = "on_hold"
    db_session.commit()
    wo = make_wo(db_session, fg, quantity_ordered=2, quantity_complete=2)
    allocation = make_allocation(db_session, wo, sheet, operation=None, qty_planned=2.0)

    run_effects(db_session, wo, user)
    db_session.expire_all()

    assert db_session.get(InventoryItem, held_lot.id).quantity_on_hand == 10, "the held lot must NOT be drawn"
    rows = _wo_scoped_issue_rows(db_session, wo, sheet)
    assert [r.quantity for r in rows] == [-2.0], "the demand is still recorded, against placeholder stock"
    assert rows[0].lot_number is None, "a placeholder row names no lot"
    assert rows[0].allocation_id == allocation.id

    assert (
        db_session.query(AuditLog)
        .filter(AuditLog.company_id == COMPANY_A, AuditLog.action == HELD_MATERIAL_CONSUMED_AUDIT_ACTION)
        .count()
        == 0
    ), "nothing was consumed from a held lot, so there is no 8.7 consumption to record"

    shortage = (
        db_session.query(AuditLog)
        .filter(AuditLog.company_id == COMPANY_A, AuditLog.action == BACKFLUSH_SHORTAGE_AUDIT_ACTION)
        .all()
    )
    assert len(shortage) == 1, "the skip becomes a shortage, and a shortage is always recorded"
    extra = shortage[0].extra_data or {}
    assert extra["shortfall"] == 2.0
    assert extra["held_quantity_skipped"] == 10.0, "the disclosure is what stops the row lying by omission"
    assert extra["held_lot_numbers"] == ["LOT-ON-HOLD"]
    assert "segregated status" in (shortage[0].description or "")


def test_unpinned_leg_treats_a_null_status_lot_as_available(db_session: Session):
    """A legacy NULL-status row is not held — it is DRAWN, and writes no 8.7 row.

    ``status IS NULL`` predates the column's default and means "unknown, treat as
    available" (``is_consumable_item``), not "quarantined". The SQL now agrees, via
    ``COALESCE(status, 'available')``; before PR 4.4 the two disagreed and the FIFO query
    would have hidden such a lot. The decrement is asserted, not just the row count —
    without it this test would pass just as well against a placeholder-anchored shortage.
    """
    user = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    legacy_lot = make_inventory(db_session, sheet, qty=10, lot="LOT-LEGACY-NULL")
    legacy_lot.status = None
    db_session.commit()
    wo = make_wo(db_session, fg, quantity_ordered=2, quantity_complete=2)
    make_allocation(db_session, wo, sheet, operation=None, qty_planned=2.0)

    run_effects(db_session, wo, user)
    db_session.expire_all()

    rows = _wo_scoped_issue_rows(db_session, wo, sheet)
    assert [(r.quantity, r.lot_number) for r in rows] == [(-2.0, "LOT-LEGACY-NULL")]
    assert db_session.get(InventoryItem, legacy_lot.id).quantity_on_hand == 8, "the legacy lot IS drawn"
    assert (
        db_session.query(AuditLog)
        .filter(AuditLog.company_id == COMPANY_A, AuditLog.action == HELD_MATERIAL_CONSUMED_AUDIT_ACTION)
        .count()
        == 0
    )
    assert (
        db_session.query(AuditLog)
        .filter(AuditLog.company_id == COMPANY_A, AuditLog.action == BACKFLUSH_SHORTAGE_AUDIT_ACTION)
        .count()
        == 0
    ), "and no false shortage against stock the engine can see"


# ---------------------------------------------------------------------------
# A duplicate ISSUE no-op must not advance the qty_consumed cache (S1)
# ---------------------------------------------------------------------------


def test_a_concurrent_winners_rows_converge_the_delta_instead_of_double_issuing(
    db_session: Session,
):
    """REWRITE of ``test_duplicate_issue_no_op_does_not_advance_qty_consumed``.

    The old test's whole premise was the INDEX: it stubbed ``_component_already_issued``
    to miss a concurrent winner's row, let the second insert reach the savepoint, and
    asserted the ``uq_wo_inventory_issue`` violation was swallowed as a clean no-op. That
    mechanism is gone in both directions — the leg posts outside that index, and it no
    longer swallows an ``IntegrityError`` at all (``duplicate_is_noop=False``), because
    with no index behind the row such an error is a real fault, not a lost race.

    What replaces it is the mechanism that now does the job: ARITHMETIC. The winner's
    rows are seeded under the shape a concurrent completion would really have written
    (``work_order_backflush``, carrying this tie's ``allocation_id``), and the loser
    re-reads a net that already includes them, so ``delta = 2 − 2 = 0`` and it writes
    nothing. **All three of the original assertions survive verbatim** — one ISSUE row,
    ``qty_consumed`` not advanced, no second decrement — which is the point: the
    protection changed, the guarantee did not.
    """
    user = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    lot = make_inventory(db_session, sheet, qty=10, lot="LOT-RACE")
    wo = make_wo(db_session, fg, quantity_ordered=2, quantity_complete=2)
    allocation = make_allocation(db_session, wo, sheet, operation=None, qty_planned=2.0)

    # The concurrent winner's row, in the shape the reconciling leg actually writes.
    db_session.add(
        InventoryTransaction(
            company_id=COMPANY_A,
            inventory_item_id=lot.id,
            part_id=sheet.id,
            transaction_type=TransactionType.ISSUE,
            quantity=-2.0,
            reference_type=BACKFLUSH_REFERENCE_TYPE,
            reference_id=wo.id,
            reference_number=wo.work_order_number,
            allocation_id=allocation.id,
            unit_cost=2.0,
            total_cost=4.0,
            created_by=user.id,
        )
    )
    db_session.commit()

    run_effects(db_session, wo, user)
    db_session.expire_all()

    assert len(_wo_scoped_issue_rows(db_session, wo, sheet)) == 1, "the delta had already been posted"
    assert db_session.get(WorkOrderMaterialAllocation, allocation.id).qty_consumed == 0.0
    assert db_session.get(InventoryItem, lot.id).quantity_on_hand == 10, "a converged replay must not decrement"


# ---------------------------------------------------------------------------
# Restore un-cancels exactly the ties the delete cancelled (S2)
# ---------------------------------------------------------------------------


def test_restore_reopens_the_ties_the_delete_cancelled(client: TestClient, db_session: Session):
    admin = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    wo = make_wo(db_session, fg, quantity_ordered=3, status_=WorkOrderStatus.RELEASED)
    op = make_op(db_session, wo, make_work_center(db_session))
    allocation = make_allocation(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=3)

    assert client.delete(f"/api/v1/work-orders/{wo.id}", headers=headers_for(admin)).status_code == 204
    db_session.expire_all()
    assert db_session.get(WorkOrderMaterialAllocation, allocation.id).status == AllocationStatus.CANCELLED

    resp = client.post(f"/api/v1/work-orders/{wo.id}/restore", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    restored = db_session.get(WorkOrderMaterialAllocation, allocation.id)
    assert restored.status == AllocationStatus.OPEN
    assert restored.work_order_operation_id == op.id, "a soft delete detaches nothing"
    assert (
        db_session.query(AuditLog)
        .filter(
            AuditLog.company_id == COMPANY_A,
            AuditLog.resource_type == "work_order_material_allocation",
            AuditLog.action == "RESTORE",
            AuditLog.resource_id == allocation.id,
        )
        .count()
        == 1
    )


def test_restore_does_not_resurrect_a_manually_untied_tie(client: TestClient, db_session: Session):
    """Restore undoes the DELETE's own cancellations — not every cancellation ever."""
    admin = make_user(db_session)
    fg = make_part(db_session)
    untied_part = make_part(db_session, uom="sheets", part_type="raw_material")
    kept_part = make_part(db_session, uom="sheets", part_type="raw_material")
    wo = make_wo(db_session, fg, quantity_ordered=3, status_=WorkOrderStatus.RELEASED)
    untied = make_allocation(db_session, wo, untied_part, operation=None, qty_planned=3)
    kept = make_allocation(db_session, wo, kept_part, operation=None, qty_planned=3)

    assert client.delete(_tie_url(wo.id, untied.id), headers=headers_for(admin)).status_code == status.HTTP_200_OK
    assert client.delete(f"/api/v1/work-orders/{wo.id}", headers=headers_for(admin)).status_code == 204
    assert client.post(f"/api/v1/work-orders/{wo.id}/restore", headers=headers_for(admin)).status_code == 200

    db_session.expire_all()
    assert db_session.get(WorkOrderMaterialAllocation, untied.id).status == AllocationStatus.CANCELLED
    assert db_session.get(WorkOrderMaterialAllocation, kept.id).status == AllocationStatus.OPEN


def test_restore_does_not_resurrect_a_reimport_cancelled_tie(client: TestClient, db_session: Session):
    """``superseded_by_reimport`` is the OTHER named cancel reason a restore must ignore.

    The manual-untie case above covers a cancel carrying NO reason; this covers the
    re-import's explicit one. Resurrecting it would re-arm demand for an operation the
    import already replaced — and that operation no longer exists, so the tie would sit
    OPEN and detached forever.
    """
    admin = make_user(db_session)
    fg = make_part(db_session)
    superseded_part = make_part(db_session, uom="sheets", part_type="raw_material")
    kept_part = make_part(db_session, uom="sheets", part_type="raw_material")
    wo = make_wo(db_session, fg, quantity_ordered=3, status_=WorkOrderStatus.RELEASED)
    op = make_op(db_session, wo, make_work_center(db_session), operation_group="LASER")
    superseded = make_allocation(db_session, wo, superseded_part, operation=op, qty_per_run=1.0, qty_planned=3)
    kept = make_allocation(db_session, wo, kept_part, operation=None, qty_planned=3)

    cancel_allocations_for_operations(
        db_session,
        work_order=wo,
        operation_ids=[op.id],
        company_id=COMPANY_A,
        audit=AuditService(db_session, admin),
    )
    db_session.commit()

    assert client.delete(f"/api/v1/work-orders/{wo.id}", headers=headers_for(admin)).status_code == 204
    assert client.post(f"/api/v1/work-orders/{wo.id}/restore", headers=headers_for(admin)).status_code == 200

    db_session.expire_all()
    assert db_session.get(WorkOrderMaterialAllocation, superseded.id).status == AllocationStatus.CANCELLED
    assert db_session.get(WorkOrderMaterialAllocation, kept.id).status == AllocationStatus.OPEN


def test_restore_does_not_resurrect_a_tie_a_reimport_detached(client: TestClient, db_session: Session):
    """A tie the DELETE cancelled but a later re-import DETACHED stays cancelled.

    The delete's own cancel keeps ``work_order_operation_id`` (so a restore puts the tie
    back on the same operation) — but a re-import then clears it, and the operation it
    named is gone. Reopening it would silently convert an operation-scoped tie into a
    WORK-ORDER-scoped one, re-arming one-shot demand nobody asked for. The cancel row
    named an operation while the row now holds NULL, which is how the restore sees it.
    """
    admin = make_user(db_session)
    fg = make_part(db_session)
    detached_part = make_part(db_session, uom="sheets", part_type="raw_material")
    kept_part = make_part(db_session, uom="sheets", part_type="raw_material")
    wo = make_wo(db_session, fg, quantity_ordered=3, status_=WorkOrderStatus.RELEASED)
    op = make_op(db_session, wo, make_work_center(db_session), operation_group="LASER")
    detached = make_allocation(db_session, wo, detached_part, operation=op, qty_per_run=1.0, qty_planned=3)
    kept = make_allocation(db_session, wo, kept_part, operation=None, qty_planned=3)

    # The delete cancels BOTH ties, keeping the operation link on the first.
    assert client.delete(f"/api/v1/work-orders/{wo.id}", headers=headers_for(admin)).status_code == 204
    db_session.expire_all()
    assert db_session.get(WorkOrderMaterialAllocation, detached.id).work_order_operation_id == op.id

    # A nest re-import then supersedes that operation and detaches the cancelled tie.
    cancel_allocations_for_operations(
        db_session,
        work_order=db_session.get(WorkOrder, wo.id),
        operation_ids=[op.id],
        company_id=COMPANY_A,
        audit=AuditService(db_session, admin),
    )
    db_session.commit()

    assert client.post(f"/api/v1/work-orders/{wo.id}/restore", headers=headers_for(admin)).status_code == 200

    db_session.expire_all()
    assert db_session.get(WorkOrderMaterialAllocation, detached.id).status == AllocationStatus.CANCELLED
    assert db_session.get(WorkOrderMaterialAllocation, kept.id).status == AllocationStatus.OPEN


# ---------------------------------------------------------------------------
# GET /inventory/transactions?work_order_id= sees operation-scoped rows (S3)
# ---------------------------------------------------------------------------


def test_ledger_work_order_filter_includes_operation_scoped_consumption(client: TestClient, db_session: Session):
    user = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    make_inventory(db_session, sheet, qty=10, lot="LOT-LEDGER")
    wo = make_wo(db_session, fg, quantity_ordered=2, quantity_complete=2)
    op = make_op(db_session, wo, make_work_center(db_session), quantity_complete=2)
    make_allocation(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=2)

    run_effects(db_session, wo, user)

    resp = client.get(f"/api/v1/inventory/transactions?work_order_id={wo.id}", headers=headers_for(user))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    reference_types = {row["reference_type"] for row in resp.json()}
    assert OPERATION_REFERENCE_TYPE in reference_types, "per-run consumption must appear in the WO ledger filter"
    assert "work_order" in reference_types, "the finished-good receipt must still appear"


def test_ledger_work_order_filter_never_reaches_across_tenants(client: TestClient, db_session: Session):
    user = make_user(db_session)
    other_fg = make_part(db_session, company_id=COMPANY_B)
    other_sheet = make_part(db_session, uom="sheets", part_type="raw_material", company_id=COMPANY_B)
    make_inventory(db_session, other_sheet, qty=10, lot="LOT-B", company_id=COMPANY_B)
    other_wo = make_wo(db_session, other_fg, quantity_ordered=2, quantity_complete=2, company_id=COMPANY_B)
    other_op = make_op(db_session, other_wo, make_work_center(db_session, company_id=COMPANY_B), quantity_complete=2)
    make_allocation(
        db_session, other_wo, other_sheet, operation=other_op, qty_per_run=1.0, qty_planned=2, company_id=COMPANY_B
    )
    other_user = make_user(db_session, company_id=COMPANY_B)
    run_effects(db_session, other_wo, other_user, company_id=COMPANY_B)

    resp = client.get(f"/api/v1/inventory/transactions?work_order_id={other_wo.id}", headers=headers_for(user))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json() == []


# ---------------------------------------------------------------------------
# PATCH refuses ambiguous / impossible edits (round-2 nits)
# ---------------------------------------------------------------------------


def test_patch_refuses_both_pin_and_clear_pin(client: TestClient, db_session: Session):
    admin = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    lot = make_inventory(db_session, sheet, qty=5, lot="LOT-PATCH")
    wo = make_wo(db_session, fg, quantity_ordered=3)
    allocation = make_allocation(db_session, wo, sheet, operation=None, qty_planned=3)

    resp = client.patch(
        _tie_url(wo.id, allocation.id),
        json={"pinned_inventory_item_id": lot.id, "clear_pinned_inventory_item": True},
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, resp.text
    db_session.expire_all()
    assert db_session.get(WorkOrderMaterialAllocation, allocation.id).pinned_inventory_item_id is None


def test_patch_refuses_lowering_qty_planned_below_qty_consumed(client: TestClient, db_session: Session):
    admin = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    wo = make_wo(db_session, fg, quantity_ordered=5)
    allocation = make_allocation(db_session, wo, sheet, operation=None, qty_planned=5, qty_consumed=3.0)

    resp = client.patch(_tie_url(wo.id, allocation.id), json={"qty_planned": 2}, headers=headers_for(admin))
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, resp.text
    assert "already been consumed" in resp.json()["detail"]

    ok = client.patch(_tie_url(wo.id, allocation.id), json={"qty_planned": 3}, headers=headers_for(admin))
    assert ok.status_code == status.HTTP_200_OK, "lowering TO the consumed quantity is allowed"
