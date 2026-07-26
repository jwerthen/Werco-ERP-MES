"""PR 4 behavior locks: hardening the DARK BOM/routing backflush leg.

``Part.backflush_components`` has no writer anywhere in ``app/`` — no schema field, no
endpoint, no UI, ``server_default="false"`` — so every existing part has it OFF and this
leg has never executed against production data. PR 4 fixes it and deliberately does NOT
expose the flag; a follow-up does that.

That shapes what this file has to prove, in order of consequence:

1. **Flag-off is unchanged.** Section 1 pins it two ways: STRUCTURALLY (the changed
   functions are never even *called* on a flag-off completion, whatever the work order's
   shape) and BEHAVIORALLY (a work order carrying every BOM/routing shape PR 4 touched
   moves no component stock, writes no extra audit row, and fingerprints identically to a
   bare control). The structural half matters more — it proves unreachability rather than
   sampling it.
2. **The double-issue is actually blocked** (§2), through the reachable state: an
   operation-scoped tie that CONSUMED and was cancelled by a work-order soft delete
   (which ignores ``qty_consumed``) and never reopened. With a negative control, so the
   suppression cannot pass by the demand never existing. And the deliberate counterpart:
   a FULLY RETURNED tie nets to zero and the backflush IS free to re-issue.
3. **The two quantity bugs** (§3) that would have shipped: ``component_quantity`` is a
   WHOLE-JOB total that the old leg multiplied by the produced quantity a second time,
   and one component's demand is replicated across every operation that touches it, which
   the old leg summed. A 100-piece job at 2/unit asked for 20,000; a three-operation
   routing tripled it.
4. **BOM line semantics** (§4): alternates / optional / reference lines are not issued;
   a ``phantom`` explodes to its children, a ``make`` sub-assembly is issued as a unit and
   its children are not.
5. **Routing precedence** (§5): per PART, not all-or-nothing — routing wins for the parts
   it names and the BOM still supplies the rest (the old leg lost the other eight lines of
   a ten-line BOM). Self-consumption is refused.
6. **Scrap basis** (§6): ``quantity_complete + quantity_scrapped``, matching the per-run
   tie engine, so a fully-scrapped work order backflushes something rather than nothing.
7. **The untie guard re-key** (§7) — the one genuinely LIVE change in this PR. It reads
   the SIGNED ledger net (ISSUE − RETURN) instead of the ``qty_consumed`` cache, in both
   directions.
8. **``AllocationStatus.CLOSED`` is still never written** (§8).
"""

from datetime import date, datetime, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import app.services.completion_inventory_service as cis
from app.core.security import create_access_token
from app.db.ledger_filter import OPERATION_REFERENCE_TYPE, WORK_ORDER_REFERENCE_TYPE
from app.models.audit_log import AuditLog
from app.models.bom import BOM, BOMItem
from app.models.company import Company
from app.models.inventory import InventoryItem, InventoryTransaction, TransactionType
from app.models.part import Part
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.models.work_order_material import AllocationSource, AllocationStatus, WorkOrderMaterialAllocation
from app.services.audit_service import AuditService
from app.services.completion_inventory_service import (
    BACKFLUSH_DOUBLE_ISSUE_BLOCKED_AUDIT_ACTION,
    _drop_allocation_covered_parts,
    apply_completion_inventory_effects,
)
from app.services.material_consumption_service import cancel_open_allocations_for_work_order

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


# ---------------------------------------------------------------------------
# Fixtures (local, like every sibling suite in this feature)
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
        email=f"bfb-{n}@co{company_id}.test",
        employee_id=f"BFB-{n:05d}",
        first_name="Back",
        last_name="Flush",
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
        part_number=f"BFB-P-{n}",
        name=f"Part {n}",
        description="backflush-breadth fixture part",
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
        name=f"BFB-WC-{n}",
        code=f"BFB-WC-{n}",
        work_center_type="laser",
        description="backflush-breadth fixture work center",
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
    quantity_scrapped: float = 0,
    status_: WorkOrderStatus = WorkOrderStatus.IN_PROGRESS,
    company_id: int = COMPANY_A,
) -> WorkOrder:
    n = _next()
    wo = WorkOrder(
        work_order_number=f"BFB-WO-{n:05d}",
        customer_name="Acme",
        part_id=part.id,
        quantity_ordered=quantity_ordered,
        quantity_complete=quantity_complete,
        quantity_scrapped=quantity_scrapped,
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
    status_: OperationStatus = OperationStatus.COMPLETE,
    component_part: Part = None,
    component_quantity: float = None,
    company_id: int = COMPANY_A,
) -> WorkOrderOperation:
    op = WorkOrderOperation(
        work_order_id=wo.id,
        work_center_id=wc.id,
        sequence=sequence,
        operation_number=f"OP{sequence}",
        name=f"Op {sequence}",
        status=status_,
        quantity_complete=quantity_complete,
        quantity_scrapped=quantity_scrapped,
        component_part_id=component_part.id if component_part is not None else None,
        component_quantity=component_quantity,
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
    qty: float = 500.0,
    lot: str = None,
    unit_cost: float = 2.0,
    location: str = "RAW-A",
    company_id: int = COMPANY_A,
) -> InventoryItem:
    item = InventoryItem(
        part_id=part.id,
        location=location,
        warehouse="MAIN",
        quantity_on_hand=qty,
        quantity_allocated=0.0,
        quantity_available=qty,
        lot_number=lot if lot is not None else f"BFB-LOT-{_next():05d}",
        unit_cost=unit_cost,
        status="available",
        is_active=True,
        company_id=company_id,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def make_bom(db: Session, part: Part, *, company_id: int = COMPANY_A) -> BOM:
    bom = BOM(part_id=part.id, revision="A", is_active=True, company_id=company_id)
    db.add(bom)
    db.commit()
    db.refresh(bom)
    return bom


def add_bom_item(
    db: Session,
    bom: BOM,
    component: Part,
    *,
    quantity: float = 1.0,
    item_number: int = 10,
    item_type: str = "buy",
    line_type: str = "component",
    scrap_factor: float = 0.0,
    is_alternate: bool = False,
    is_optional: bool = False,
    alternate_group: str = None,
    company_id: int = COMPANY_A,
) -> BOMItem:
    item = BOMItem(
        bom_id=bom.id,
        component_part_id=component.id,
        item_number=item_number,
        quantity=quantity,
        item_type=item_type,
        line_type=line_type,
        scrap_factor=scrap_factor,
        is_alternate=is_alternate,
        is_optional=is_optional,
        alternate_group=alternate_group,
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


# ---------------------------------------------------------------------------
# Drivers / observation helpers
# ---------------------------------------------------------------------------


def run_effects(db: Session, wo: WorkOrder, user: User, *, company_id: int = COMPANY_A) -> None:
    """The real completion entry point the five work-order-completion call sites share."""
    apply_completion_inventory_effects(db, wo, user_id=user.id, company_id=company_id, audit=AuditService(db, user))
    db.commit()


def wo_issues(db: Session, wo: WorkOrder, *, company_id: int = COMPANY_A) -> dict[int, float]:
    """``{part_id: signed total}`` over this work order's WO-scoped ISSUE rows.

    The backflush's own output shape: one ISSUE per (work order, part) under
    ``reference_type='work_order'``, which is all ``uq_wo_inventory_issue`` permits.
    """
    totals: dict[int, float] = {}
    rows = (
        db.query(InventoryTransaction)
        .filter(
            InventoryTransaction.company_id == company_id,
            InventoryTransaction.reference_type == WORK_ORDER_REFERENCE_TYPE,
            InventoryTransaction.reference_id == wo.id,
            InventoryTransaction.transaction_type == TransactionType.ISSUE,
        )
        .all()
    )
    for row in rows:
        totals[row.part_id] = totals.get(row.part_id, 0.0) + float(row.quantity or 0)
    return totals


def op_scoped_rows(db: Session, op: WorkOrderOperation, *, company_id: int = COMPANY_A) -> list[InventoryTransaction]:
    return (
        db.query(InventoryTransaction)
        .filter(
            InventoryTransaction.company_id == company_id,
            InventoryTransaction.reference_type == OPERATION_REFERENCE_TYPE,
            InventoryTransaction.reference_id == op.id,
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


def blocked_audit_rows(db: Session, *, company_id: int = COMPANY_A) -> list[AuditLog]:
    return (
        db.query(AuditLog)
        .filter(
            AuditLog.company_id == company_id,
            AuditLog.action == BACKFLUSH_DOUBLE_ISSUE_BLOCKED_AUDIT_ACTION,
        )
        .order_by(AuditLog.id)
        .all()
    )


def wo_audit_fingerprint(db: Session, wo: WorkOrder, *, company_id: int = COMPANY_A) -> list[tuple]:
    """``(action, resource_type)`` for every audit row that NAMES this work order."""
    return sorted(
        (row.action, row.resource_type)
        for row in db.query(AuditLog)
        .filter(AuditLog.company_id == company_id, AuditLog.description.contains(wo.work_order_number))
        .all()
    )


def wo_ledger_fingerprint(db: Session, wo: WorkOrder, *, company_id: int = COMPANY_A) -> list[tuple]:
    """The MOVEMENT this work order caused, normalized so two structurally identical
    work orders compare equal (identity — WO number, derived FG lot, own part id — is
    replaced with placeholders; everything describing the movement is verbatim)."""
    operation_ids = [
        row[0]
        for row in db.query(WorkOrderOperation.id)
        .filter(WorkOrderOperation.company_id == company_id, WorkOrderOperation.work_order_id == wo.id)
        .all()
    ] or [-1]

    def scrub(value):
        return value.replace(wo.work_order_number, "<WO>") if isinstance(value, str) else value

    rows = (
        db.query(InventoryTransaction)
        .filter(InventoryTransaction.company_id == company_id)
        .filter(
            (
                (InventoryTransaction.reference_type == WORK_ORDER_REFERENCE_TYPE)
                & (InventoryTransaction.reference_id == wo.id)
            )
            | (
                (InventoryTransaction.reference_type == OPERATION_REFERENCE_TYPE)
                & (InventoryTransaction.reference_id.in_(operation_ids))
            )
        )
        .all()
    )
    return sorted(
        (
            "<FG>" if row.part_id == wo.part_id else row.part_id,
            row.transaction_type.value,
            row.quantity,
            row.reference_type,
            scrub(row.reference_number),
            scrub(row.lot_number),
            row.from_location,
            row.to_location,
            row.allocation_id,
            row.unit_cost,
        )
        for row in rows
    )


# ===========================================================================
# 1. THE PROPERTY THAT MATTERS MOST — with the flag off, nothing changed
# ===========================================================================


def test_flag_off_never_reaches_any_code_this_pr_changed(db_session: Session, monkeypatch):
    """STRUCTURAL proof, not a sample: the changed functions are never called.

    Every part in production has ``backflush_components`` False, so the only honest
    statement of "flag-off is byte-identical" is that PR 4's demand resolution is
    UNREACHABLE — not that it happens to produce the same answer on the shapes a test
    thought to try. ``backflush_components_for_work_order`` gates
    ``_resolve_backflush_components`` behind ``backflush_enabled``, and
    ``_resolve_backflush_components`` is the sole caller of ``_explode_backflush_bom``,
    ``_routing_backflush_demand``, ``_drop_allocation_covered_parts`` and
    ``_drop_ledger_covered_parts``. So one spy on each of the two entry points covers
    the whole changed surface.

    Driven with FOUR work-order shapes, because each takes a different route through
    ``backflush_components_for_work_order``: untied (the early return), a BOM +
    routing job (would produce demand if the flag were on), an OPERATION-scoped tie
    (the per-run engine posts real rows), and a WORK-ORDER-scoped tie (which reaches
    the issue loop with ``backflush_enabled`` False — the one shape that gets past the
    early return).

    This is also the answer to "did ``_drop_allocation_covered_parts`` change live
    behavior?" — it cannot have, because nothing live calls it.
    """
    calls: list[str] = []
    real_resolve = cis._resolve_backflush_components
    real_ledger_drop = cis._drop_ledger_covered_parts

    def spy_resolve(*args, **kwargs):
        calls.append("_resolve_backflush_components")
        return real_resolve(*args, **kwargs)

    def spy_ledger_drop(*args, **kwargs):
        calls.append("_drop_ledger_covered_parts")
        return real_ledger_drop(*args, **kwargs)

    monkeypatch.setattr(cis, "_resolve_backflush_components", spy_resolve)
    monkeypatch.setattr(cis, "_drop_ledger_covered_parts", spy_ledger_drop)

    user = make_user(db_session)
    wc = make_work_center(db_session)
    component = make_part(db_session, part_type="purchased")
    make_inventory(db_session, component)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    make_inventory(db_session, sheet)

    # (a) untied, no BOM.
    plain = make_wo(db_session, make_part(db_session), quantity_ordered=4, quantity_complete=4)
    make_op(db_session, plain, wc, quantity_complete=4)

    # (b) a BOM *and* a routing operation that WOULD produce demand if the flag were on.
    routed_part = make_part(db_session, backflush=False)
    add_bom_item(db_session, make_bom(db_session, routed_part), component, quantity=2)
    routed = make_wo(db_session, routed_part, quantity_ordered=4, quantity_complete=4)
    make_op(db_session, routed, wc, quantity_complete=4, component_part=component, component_quantity=8)

    # (c) an OPERATION-scoped tie: the per-run engine really runs on this one.
    op_tied = make_wo(db_session, make_part(db_session), quantity_ordered=3, quantity_complete=3)
    op_tied_op = make_op(db_session, op_tied, wc, quantity_complete=3)
    tie(db_session, op_tied, sheet, operation=op_tied_op, qty_per_run=1.0, qty_planned=3)

    # (d) a WORK-ORDER-scoped tie: reaches the issue loop with the flag off.
    wo_tied = make_wo(db_session, make_part(db_session), quantity_ordered=2, quantity_complete=2)
    make_op(db_session, wo_tied, wc, quantity_complete=2)
    wo_tie = tie(db_session, wo_tied, sheet, operation=None, qty_planned=2)

    for work_order in (plain, routed, op_tied, wo_tied):
        run_effects(db_session, work_order, user)
    db_session.expire_all()

    assert calls == [], f"flag-off must not reach PR 4's demand resolution, but called {calls}"

    # The drivers must actually have done work, or the assertion above proves nothing.
    assert [t.quantity for t in op_scoped_rows(db_session, op_tied_op)] == [-3], "the per-run engine must have run"
    assert wo_issues(db_session, wo_tied) == {sheet.id: -2.0}, "the WO-scoped tie must still drain"
    assert wo_issues(db_session, routed) == {}, "no component ISSUE with the flag off"
    assert db_session.get(WorkOrderMaterialAllocation, wo_tie.id).qty_consumed == 2.0


def test_flag_off_work_order_with_every_changed_bom_shape_is_identical_to_a_bare_one(db_session: Session):
    """BEHAVIORAL half: a work order carrying every shape PR 4 touched moves no stock.

    Alternates, optional and reference lines, a phantom, a ``make`` sub-assembly, a
    routing operation naming a component, and reported scrap — the whole changed
    surface, on a part whose ``backflush_components`` is False (i.e. every part that
    exists). Its ledger and audit fingerprints must equal a CONTROL work order with a
    bare part and no structure at all: no component ISSUE, no
    ``BACKFLUSH_DOUBLE_ISSUE_BLOCKED`` row, nothing.
    """
    user = make_user(db_session)
    wc = make_work_center(db_session)

    control_part = make_part(db_session, standard_cost=9.0)
    control = make_wo(db_session, control_part, quantity_ordered=6, quantity_complete=4, quantity_scrapped=2)
    make_op(db_session, control, wc, quantity_complete=4, quantity_scrapped=2)
    run_effects(db_session, control, user)
    control_ledger = wo_ledger_fingerprint(db_session, control)
    control_audit = wo_audit_fingerprint(db_session, control)
    assert control_ledger, "the control must still receive its finished good"

    subject_part = make_part(db_session, backflush=False, standard_cost=9.0)
    subject_bom = make_bom(db_session, subject_part)

    primary = make_part(db_session, part_type="purchased")
    alternate = make_part(db_session, part_type="purchased")
    optional = make_part(db_session, part_type="purchased")
    reference = make_part(db_session, part_type="purchased")
    phantom = make_part(db_session)
    phantom_child = make_part(db_session, part_type="purchased")
    sub_assembly = make_part(db_session)
    sub_child = make_part(db_session, part_type="purchased")
    for component in (primary, alternate, optional, reference, phantom_child, sub_assembly, sub_child):
        make_inventory(db_session, component)

    add_bom_item(db_session, subject_bom, primary, quantity=2, item_number=10)
    add_bom_item(db_session, subject_bom, alternate, quantity=2, item_number=20, is_alternate=True, alternate_group="G")
    add_bom_item(db_session, subject_bom, optional, quantity=1, item_number=30, is_optional=True)
    add_bom_item(db_session, subject_bom, reference, quantity=1, item_number=40, line_type="reference")
    add_bom_item(db_session, subject_bom, phantom, quantity=1, item_number=50, item_type="phantom")
    add_bom_item(db_session, subject_bom, sub_assembly, quantity=1, item_number=60, item_type="make")
    add_bom_item(db_session, make_bom(db_session, phantom), phantom_child, quantity=3)
    add_bom_item(db_session, make_bom(db_session, sub_assembly), sub_child, quantity=5)

    subject = make_wo(db_session, subject_part, quantity_ordered=6, quantity_complete=4, quantity_scrapped=2)
    make_op(db_session, subject, wc, quantity_complete=4, quantity_scrapped=2)
    make_op(db_session, subject, wc, sequence=20, quantity_complete=4, component_part=primary, component_quantity=12)

    run_effects(db_session, subject, user)
    db_session.expire_all()

    assert wo_ledger_fingerprint(db_session, subject) == control_ledger
    assert wo_audit_fingerprint(db_session, subject) == control_audit
    assert wo_issues(db_session, subject) == {}, "flag off consumes no component, whatever the BOM says"
    assert blocked_audit_rows(db_session) == []
    for component in (primary, alternate, optional, reference, phantom_child, sub_assembly, sub_child):
        assert on_hand(db_session, component) == 500.0, f"{component.part_number} stock must be untouched"


def test_drop_allocation_covered_parts_keeps_its_three_documented_answers(db_session: Session):
    """The unit-level check on the one changed helper the flag-off spy can only prove
    unreachable. Its lookup now selects quantity columns and keys a dict where it used
    to build a set of ids, and ``live_operations.get(None)`` replaced
    ``None in live_operation_ids``. All three answers must be what they were:

    * an OPEN tie on a LIVE operation of this work order suppresses its part;
    * a tie pointing at an operation NOT on this work order does not (it cannot
      consume either — the consume path makes the identical check);
    * a WORK-ORDER-scoped tie (``work_order_operation_id`` NULL) is not this layer's
      business at all and must fall straight through — the case the ``None`` key
      lookup covers, where a dict ``.get`` and a set membership test could have
      diverged.
    """
    user = make_user(db_session)
    wc = make_work_center(db_session)
    fg = make_part(db_session, backflush=True)
    covered = make_part(db_session, part_type="purchased")
    foreign = make_part(db_session, part_type="purchased")
    wo_scoped = make_part(db_session, part_type="purchased")

    wo = make_wo(db_session, fg, quantity_ordered=4, quantity_complete=4)
    live_op = make_op(db_session, wo, wc, quantity_complete=4)
    other_wo = make_wo(db_session, fg, quantity_ordered=1)
    foreign_op = make_op(db_session, other_wo, wc, sequence=20)

    allocations = [
        tie(db_session, wo, covered, operation=live_op, qty_per_run=1.0, qty_planned=4),
        tie(db_session, wo, foreign, operation=foreign_op, qty_per_run=1.0, qty_planned=4),
        tie(db_session, wo, wo_scoped, operation=None, qty_planned=4),
    ]
    required = {covered.id: 4.0, foreign.id: 4.0, wo_scoped.id: 4.0}

    result = _drop_allocation_covered_parts(db_session, wo, COMPANY_A, dict(required), allocations)

    assert covered.id not in result, "a live operation-scoped tie owns its part's demand"
    assert result[foreign.id] == 4.0, "a tie off this work order must not suppress the backflush"
    assert result[wo_scoped.id] == 4.0, "a work-order-scoped tie is handled by the issue loop, not here"
    assert AuditService(db_session, user) is not None  # keeps the fixture user meaningful


# ===========================================================================
# 2. The double-issue is actually blocked — and a full return re-opens it
# ===========================================================================


def _consumed_then_cancelled(db_session: Session, user: User, *, sheet_on_hand: float = 50.0):
    """A tie that CONSUMED and was then cancelled by a work-order soft delete.

    Reachable through supported verbs and NOT hypothetical:
    ``cancel_open_allocations_for_work_order`` (what the soft delete calls) cancels an
    OPEN tie regardless of ``qty_consumed``, and ``reopen_allocations_cancelled_by_delete``
    only resurrects ties whose most recent DELETE audit row carries the delete's own
    reason — so a cancel from anywhere else, or a missing chain row, leaves a CONSUMED
    tie CANCELLED forever. The service function is called directly rather than through
    ``DELETE /work-orders/{id}`` so the work order stays completable afterwards; it is
    the same function on the same arguments.
    """
    wc = make_work_center(db_session)
    fg = make_part(db_session, backflush=True)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    lot = make_inventory(db_session, sheet, qty=sheet_on_hand, lot="SHEET-DBL")
    add_bom_item(db_session, make_bom(db_session, fg), sheet, quantity=1.0)

    wo = make_wo(db_session, fg, quantity_ordered=4, quantity_complete=4)
    op = make_op(db_session, wo, wc, quantity_complete=4)
    allocation = tie(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=4)

    run_effects(db_session, wo, user)
    db_session.expire_all()
    assert [t.quantity for t in op_scoped_rows(db_session, op)] == [-4], "the tie must really have consumed"
    assert wo_issues(db_session, wo) == {}, "layer 1 suppressed the backflush while the tie was OPEN"
    return wo, op, sheet, lot, allocation


def test_a_consumed_then_cancelled_tie_still_blocks_the_backflush_and_says_so(db_session: Session):
    """THE double-issue this PR exists to stop, with the audit row that records it.

    Once the tie is CANCELLED, ``_drop_allocation_covered_parts`` cannot see it, and
    neither ``_component_already_issued`` (which keys on ``reference_type='work_order'``
    rows) nor ``uq_wo_inventory_issue`` (whose predicate deliberately excludes the
    operation reference shape) can either. Without the ledger layer the same sheet
    leaves stock twice, and the as-built record carries two lines naming two different
    lots for one physical consumption.
    """
    user = make_user(db_session)
    wo, op, sheet, lot, allocation = _consumed_then_cancelled(db_session, user)

    cancel_open_allocations_for_work_order(
        db_session, work_order=wo, company_id=COMPANY_A, audit=AuditService(db_session, user)
    )
    db_session.commit()
    db_session.expire_all()
    assert db_session.get(WorkOrderMaterialAllocation, allocation.id).status == AllocationStatus.CANCELLED

    run_effects(db_session, db_session.get(WorkOrder, wo.id), user)
    db_session.expire_all()

    assert wo_issues(db_session, wo) == {}, "the backflush must NOT issue the sheet a second time"
    assert [t.quantity for t in op_scoped_rows(db_session, op)] == [-4], "and the original consumption stands"
    assert on_hand(db_session, sheet) == 46.0, "50 - 4, consumed exactly once"

    rows = blocked_audit_rows(db_session)
    assert len(rows) == 1, "the suppression is RECORDED, not silent"
    row = rows[0]
    assert row.resource_type == "inventory"
    assert row.resource_id == sheet.id
    assert (row.extra_data or {})["ledger_net_issued"] == 4.0
    assert (row.extra_data or {})["suppressed_quantity"] == 4.0
    assert (row.extra_data or {})["work_order_id"] == wo.id
    assert (row.extra_data or {})["reference_type"] == OPERATION_REFERENCE_TYPE
    assert sheet.part_number in (row.description or "")


def test_negative_control_a_cancelled_tie_that_never_consumed_does_not_block(db_session: Session):
    """The suppression above must come from the LEDGER, not from the tie existing.

    Identical setup with no consumption: the cancelled tie is invisible to layer 1, the
    ledger has nothing, and the BOM demand issues normally. Without this, the test above
    would pass just as well if the backflush had been broken outright.
    """
    user = make_user(db_session)
    wc = make_work_center(db_session)
    fg = make_part(db_session, backflush=True)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    make_inventory(db_session, sheet, qty=50, lot="SHEET-NOCONSUME")
    add_bom_item(db_session, make_bom(db_session, fg), sheet, quantity=1.0)

    wo = make_wo(db_session, fg, quantity_ordered=4, quantity_complete=4)
    op = make_op(db_session, wo, wc, quantity_complete=4)
    tie(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=4, status_=AllocationStatus.CANCELLED)

    run_effects(db_session, wo, user)
    db_session.expire_all()

    assert op_scoped_rows(db_session, op) == [], "a cancelled tie never consumes"
    assert wo_issues(db_session, wo) == {sheet.id: -4.0}, "so the BOM demand must be issued"
    assert on_hand(db_session, sheet) == 46.0
    assert blocked_audit_rows(db_session) == [], "nothing was blocked, so nothing to record"


def test_a_fully_returned_tie_nets_to_zero_and_the_backflush_may_re_issue(client: TestClient, db_session: Session):
    """The deliberate counterpart — an owner decision, pinned so nobody "hardens" it.

    ``return_and_untie`` credits the material back to its source lots. The job then
    holds NONE of it and the BOM's demand is once again unmet, so the backflush is free
    to issue. Suppressing on the mere EXISTENCE of ledger rows would leave the part
    permanently un-issuable on a job that genuinely gave the material back — refusing to
    consume material the shop is standing next to, and hiding the gap from the shortage
    machinery built to surface it.
    """
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    wo, op, sheet, lot, allocation = _consumed_then_cancelled(db_session, supervisor)
    assert on_hand(db_session, sheet) == 46.0

    resp = client.post(
        f"/api/v1/work-orders/{wo.id}/material-allocations/{allocation.id}/return",
        headers=headers_for(supervisor),
        json={"quantity": 4, "intent": "return_and_untie", "reason": "nest scrapped before cutting"},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    db_session.expire_all()
    assert on_hand(db_session, sheet) == 50.0, "the material is back on its source lot"
    assert db_session.get(WorkOrderMaterialAllocation, allocation.id).status == AllocationStatus.CANCELLED

    run_effects(db_session, db_session.get(WorkOrder, wo.id), supervisor)
    db_session.expire_all()

    assert wo_issues(db_session, wo) == {sheet.id: -4.0}, "a net of zero must NOT suppress the backflush"
    assert on_hand(db_session, sheet) == 46.0, "issued once, by the backflush this time"
    assert blocked_audit_rows(db_session) == [], "nothing was suppressed, so no blocked row"
    assert [(t.transaction_type.value, t.quantity) for t in op_scoped_rows(db_session, op)] == [
        ("issue", -4.0),
        ("return", 4.0),
    ], "and nothing historical was mutated to achieve it"


def test_a_fully_returned_work_order_scoped_tie_leaves_its_part_un_issuable_but_RECORDED(
    client: TestClient, db_session: Session
):
    """The asymmetric counterpart, and the harm is SILENCE — not a corrupted cache.

    A work-order-scoped tie's ISSUE is written under ``reference_type='work_order'`` and
    survives the return, so ``_component_already_issued`` keeps firing.
    ``uq_wo_inventory_issue`` permits exactly ONE such row per (company, work order,
    part), so a second issue is physically unavailable — netting the guard here would not
    enable a re-issue, it would attempt one and lose to the index.

    The part therefore stays un-issuable on this work order — that half is not choosable.
    What WAS choosable is whether it happens silently, and it no longer does: the skip now
    writes a ``BACKFLUSH_DOUBLE_ISSUE_BLOCKED`` row carrying the suppressed quantity and
    ``suppression_reason='already_issued'``. Recording the recoverable suppression (a tie
    already drew the material) while staying silent on the PERMANENT one was exactly
    backwards — an as-built review cannot reconstruct a decision nothing wrote down.

    The tie's ``qty_consumed`` is still NOT falsely advanced
    (``_mark_work_order_ties_consumed`` is gated on ``IssueOutcome.posted``, never
    reached), so the cache stays honest. The remedy remains an OPERATION-scoped tie, which
    posts outside that index — the 409 on ``POST`` already says exactly that.
    """
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    wc = make_work_center(db_session)
    fg = make_part(db_session, backflush=True)
    stock = make_part(db_session, part_type="purchased")
    make_inventory(db_session, stock, qty=50, lot="STOCK-WOTIE")
    add_bom_item(db_session, make_bom(db_session, fg), stock, quantity=1)

    wo = make_wo(db_session, fg, quantity_ordered=4, quantity_complete=4)
    make_op(db_session, wo, wc, quantity_complete=4)
    allocation = tie(db_session, wo, stock, operation=None, qty_planned=4)

    run_effects(db_session, wo, supervisor)
    db_session.expire_all()
    assert wo_issues(db_session, wo) == {stock.id: -8.0}, "4 from the BOM + 4 from the tie, summed into one row"
    assert db_session.get(WorkOrderMaterialAllocation, allocation.id).qty_consumed == 4.0

    returned = client.post(
        f"/api/v1/work-orders/{wo.id}/material-allocations/{allocation.id}/return",
        headers=headers_for(supervisor),
        json={"quantity": 4, "intent": "return_and_untie", "reason": "material went back on the rack"},
    )
    assert returned.status_code == status.HTTP_200_OK, returned.text
    db_session.expire_all()
    assert db_session.get(WorkOrderMaterialAllocation, allocation.id).qty_consumed == 0.0
    assert on_hand(db_session, stock) == 46.0, "50 - 8 issued + 4 returned"

    audit_before = db_session.query(AuditLog).count()
    run_effects(db_session, db_session.get(WorkOrder, wo.id), supervisor)
    db_session.expire_all()

    issue_rows = (
        db_session.query(InventoryTransaction)
        .filter(
            InventoryTransaction.company_id == COMPANY_A,
            InventoryTransaction.reference_type == WORK_ORDER_REFERENCE_TYPE,
            InventoryTransaction.reference_id == wo.id,
            InventoryTransaction.part_id == stock.id,
            InventoryTransaction.transaction_type == TransactionType.ISSUE,
        )
        .all()
    )
    assert len(issue_rows) == 1, "the index permits exactly one, and the return does not free it"
    assert on_hand(db_session, stock) == 46.0, "no second draw"

    # The unmet demand is now ON THE RECORD rather than silent.
    blocked = blocked_audit_rows(db_session)
    assert len(blocked) == 1, "the permanent suppression must be recorded, not silent"
    assert (blocked[0].extra_data or {}).get("suppression_reason") == "already_issued"
    assert (blocked[0].extra_data or {}).get("component_part_id") == stock.id
    assert db_session.query(AuditLog).count() > audit_before, "the unmet demand is written down"
    assert (
        db_session.get(WorkOrderMaterialAllocation, allocation.id).qty_consumed == 0.0
    ), "and the cache is NOT falsely advanced — the issue loop never reached _issue_one_component"


# ===========================================================================
# 3. The two quantity bugs — catastrophic on exposure, invisible until then
# ===========================================================================


def test_routing_component_quantity_is_a_whole_job_total_and_is_not_squared(db_session: Session):
    """``component_quantity`` is ``qty_per_assembly x quantity_ordered``, not a rate.

    Both writers store the whole-job total (``_create_assembly_routing_operations`` and
    ``_reconcile_operation_component_quantities``). The old leg multiplied it by the
    produced quantity AGAIN: a 10-piece job at 2 per unit demanded 200 instead of 20 —
    and at 100 pieces, 20,000. The rate is recovered by dividing back out by
    ``quantity_ordered`` and re-scaling to the basis.
    """
    user = make_user(db_session)
    wc = make_work_center(db_session)
    fg = make_part(db_session, backflush=True)
    component = make_part(db_session, part_type="purchased")
    make_inventory(db_session, component, qty=500)

    wo = make_wo(db_session, fg, quantity_ordered=10, quantity_complete=10)
    # 2 per unit x 10 ordered = 20, exactly what the routing writers store.
    make_op(db_session, wo, wc, quantity_complete=10, component_part=component, component_quantity=20)

    run_effects(db_session, wo, user)
    db_session.expire_all()

    assert wo_issues(db_session, wo) == {component.id: -20.0}, "2/unit x 10 produced = 20"
    assert wo_issues(db_session, wo)[component.id] != -200.0, "the squared demand must be impossible to reintroduce"
    assert on_hand(db_session, component) == 480.0


def test_routing_demand_rescales_when_less_than_the_ordered_quantity_was_produced(db_session: Session):
    """Recovering the rate is only half of it — it must re-scale to what was produced.

    Same stored total, half the job finished: 20 total over 10 ordered is 2 per unit,
    and 5 produced draws 10. Reading the stored figure verbatim would over-issue by the
    unfinished half of the job.
    """
    user = make_user(db_session)
    wc = make_work_center(db_session)
    fg = make_part(db_session, backflush=True)
    component = make_part(db_session, part_type="purchased")
    make_inventory(db_session, component, qty=500)

    wo = make_wo(db_session, fg, quantity_ordered=10, quantity_complete=5)
    make_op(db_session, wo, wc, quantity_complete=5, component_part=component, component_quantity=20)

    run_effects(db_session, wo, user)
    db_session.expire_all()

    assert wo_issues(db_session, wo) == {component.id: -10.0}, "2/unit x 5 produced = 10"


def test_one_component_across_three_operations_is_issued_once(db_session: Session):
    """A routed component's whole-job demand is REPLICATED on every operation that
    touches it — ``_create_assembly_routing_operations`` writes the same
    ``component_quantity`` onto each operation of that component's routing. The old leg
    summed them, so a three-operation routing tripled the demand. Reduced with ``max``.
    """
    user = make_user(db_session)
    wc = make_work_center(db_session)
    fg = make_part(db_session, backflush=True)
    component = make_part(db_session, part_type="purchased")
    make_inventory(db_session, component, qty=500)

    wo = make_wo(db_session, fg, quantity_ordered=10, quantity_complete=10)
    for sequence in (10, 20, 30):
        make_op(
            db_session, wo, wc, sequence=sequence, quantity_complete=10, component_part=component, component_quantity=20
        )

    run_effects(db_session, wo, user)
    db_session.expire_all()

    assert wo_issues(db_session, wo) == {component.id: -20.0}, "one component, one demand — not 3 x 20"
    assert on_hand(db_session, component) == 480.0


# ===========================================================================
# 4. BOM line semantics — what a backflush must not issue, and multi-level
# ===========================================================================


def test_alternate_optional_and_reference_lines_are_never_issued(db_session: Session):
    """Three families the backflush read NOWHERE before, all failing expensively.

    An alternate GROUP is an OR, not an AND — issuing every member multiplies the
    group's demand by its size, and ``mrp_service`` has always skipped alternates. An
    optional line is present on some units and nothing on the work order records which.
    A reference line is documentation and tooling; the enum's own comment says "not
    consumed". Over-issuing writes material into a genealogy record that never contained
    it, which no downstream reader can distinguish from the truth.
    """
    user = make_user(db_session)
    wc = make_work_center(db_session)
    fg = make_part(db_session, backflush=True)
    bom = make_bom(db_session, fg)

    primary = make_part(db_session, part_type="purchased")
    alternate = make_part(db_session, part_type="purchased")
    optional = make_part(db_session, part_type="purchased")
    reference = make_part(db_session, part_type="purchased")
    for component in (primary, alternate, optional, reference):
        make_inventory(db_session, component, qty=500)

    add_bom_item(db_session, bom, primary, quantity=2, item_number=10, alternate_group="G1")
    add_bom_item(db_session, bom, alternate, quantity=2, item_number=20, is_alternate=True, alternate_group="G1")
    add_bom_item(db_session, bom, optional, quantity=1, item_number=30, is_optional=True)
    add_bom_item(db_session, bom, reference, quantity=1, item_number=40, line_type="reference")

    wo = make_wo(db_session, fg, quantity_ordered=3, quantity_complete=3)
    make_op(db_session, wo, wc, quantity_complete=3)

    run_effects(db_session, wo, user)
    db_session.expire_all()

    assert wo_issues(db_session, wo) == {primary.id: -6.0}, "only the non-alternate primary line is consumed"
    for skipped in (alternate, optional, reference):
        assert on_hand(db_session, skipped) == 500.0, f"{skipped.part_number} must not move"


def test_a_phantom_explodes_to_its_children_and_is_not_itself_issued(db_session: Session):
    """A phantom is a planning fiction that is never stocked, so it is EXCLUDED and its
    children are exploded in its place at the phantom's extended quantity."""
    user = make_user(db_session)
    wc = make_work_center(db_session)
    fg = make_part(db_session, backflush=True)
    phantom = make_part(db_session)
    child = make_part(db_session, part_type="purchased")
    make_inventory(db_session, phantom, qty=500)
    make_inventory(db_session, child, qty=500)

    add_bom_item(db_session, make_bom(db_session, fg), phantom, quantity=2, item_type="phantom")
    add_bom_item(db_session, make_bom(db_session, phantom), child, quantity=3)

    wo = make_wo(db_session, fg, quantity_ordered=4, quantity_complete=4)
    make_op(db_session, wo, wc, quantity_complete=4)

    run_effects(db_session, wo, user)
    db_session.expire_all()

    # 2 phantoms per unit x 4 units = 8; 3 children per phantom = 24.
    assert wo_issues(db_session, wo) == {child.id: -24.0}
    assert on_hand(db_session, phantom) == 500.0, "a phantom is never stocked, so it is never issued"


def test_a_make_sub_assembly_is_issued_as_a_unit_and_its_children_are_not(db_session: Session):
    """A ``make`` item is a STOCKED unit. Its children were consumed when it was built,
    so issuing both consumes the same material twice — which is exactly what
    ``_collect_bom_components`` (the shared flat exploder, correct for its own four
    callers) would have produced here."""
    user = make_user(db_session)
    wc = make_work_center(db_session)
    fg = make_part(db_session, backflush=True)
    sub_assembly = make_part(db_session)
    sub_child = make_part(db_session, part_type="purchased")
    make_inventory(db_session, sub_assembly, qty=500)
    make_inventory(db_session, sub_child, qty=500)

    add_bom_item(db_session, make_bom(db_session, fg), sub_assembly, quantity=2, item_type="make")
    add_bom_item(db_session, make_bom(db_session, sub_assembly), sub_child, quantity=5)

    wo = make_wo(db_session, fg, quantity_ordered=3, quantity_complete=3)
    make_op(db_session, wo, wc, quantity_complete=3)

    run_effects(db_session, wo, user)
    db_session.expire_all()

    assert wo_issues(db_session, wo) == {sub_assembly.id: -6.0}, "2 per unit x 3 units, as a stocked unit"
    assert on_hand(db_session, sub_child) == 500.0, "its raw material was consumed when IT was built"


def test_a_phantom_with_no_bom_is_backflushed_as_a_stocked_line(db_session: Session):
    """A phantom with nothing to explode into is treated as a stocked line and logged.

    Dropping it would make the line vanish with no ledger row, no shortage and no signal
    of any kind — the failure mode this whole PR is about.
    """
    user = make_user(db_session)
    wc = make_work_center(db_session)
    fg = make_part(db_session, backflush=True)
    orphan_phantom = make_part(db_session)
    make_inventory(db_session, orphan_phantom, qty=500)

    add_bom_item(db_session, make_bom(db_session, fg), orphan_phantom, quantity=2, item_type="phantom")

    wo = make_wo(db_session, fg, quantity_ordered=3, quantity_complete=3)
    make_op(db_session, wo, wc, quantity_complete=3)

    run_effects(db_session, wo, user)
    db_session.expire_all()

    assert wo_issues(db_session, wo) == {orphan_phantom.id: -6.0}


# ===========================================================================
# 5. Routing precedence is PER PART, and self-consumption is refused
# ===========================================================================


def test_routing_wins_for_the_parts_it_names_and_the_bom_supplies_the_rest(db_session: Session):
    """The old leg was ``if not required:`` — ONE stray ``component_part_id`` on ONE
    operation silently disabled the entire BOM explosion for the whole work order. Not
    hypothetical: ``_create_assembly_routing_operations`` writes ``component_part_id``
    only for components that HAVE a released routing, so an assembly whose ten BOM lines
    include two routed components lost the other eight.
    """
    user = make_user(db_session)
    wc = make_work_center(db_session)
    fg = make_part(db_session, backflush=True)
    bom = make_bom(db_session, fg)

    routed = make_part(db_session, part_type="purchased")
    plain_b = make_part(db_session, part_type="purchased")
    plain_c = make_part(db_session, part_type="purchased")
    for component in (routed, plain_b, plain_c):
        make_inventory(db_session, component, qty=500)

    add_bom_item(db_session, bom, routed, quantity=1, item_number=10)
    add_bom_item(db_session, bom, plain_b, quantity=2, item_number=20)
    add_bom_item(db_session, bom, plain_c, quantity=3, item_number=30)

    wo = make_wo(db_session, fg, quantity_ordered=5, quantity_complete=5)
    make_op(db_session, wo, wc, quantity_complete=5)
    # The routing disagrees with the BOM on its own part (4/unit x 5 ordered = 20).
    make_op(db_session, wo, wc, sequence=20, quantity_complete=5, component_part=routed, component_quantity=20)

    run_effects(db_session, wo, user)
    db_session.expire_all()

    assert wo_issues(db_session, wo) == {
        routed.id: -20.0,  # routing wins for the part it names
        plain_b.id: -10.0,  # ...and the BOM still supplies
        plain_c.id: -15.0,  # ...every part it does not
    }


def test_an_operation_naming_the_work_orders_own_part_never_self_consumes(db_session: Session):
    """It would ISSUE the part the finished-goods leg just RECEIVED — netting the job's
    own output out of stock and writing the produced part into its own as-built record.
    """
    user = make_user(db_session)
    wc = make_work_center(db_session)
    fg = make_part(db_session, backflush=True, standard_cost=9.0)
    component = make_part(db_session, part_type="purchased")
    make_inventory(db_session, component, qty=500)
    make_inventory(db_session, fg, qty=500, location="RAW-A")
    add_bom_item(db_session, make_bom(db_session, fg), component, quantity=1)

    wo = make_wo(db_session, fg, quantity_ordered=4, quantity_complete=4)
    make_op(db_session, wo, wc, quantity_complete=4, component_part=fg, component_quantity=8)

    run_effects(db_session, wo, user)
    db_session.expire_all()

    issues = wo_issues(db_session, wo)
    assert fg.id not in issues, "the work order must never consume its own part"
    assert issues == {component.id: -4.0}, "the BOM's real component is unaffected by the refusal"
    receipts = (
        db_session.query(InventoryTransaction)
        .filter(
            InventoryTransaction.company_id == COMPANY_A,
            InventoryTransaction.reference_type == WORK_ORDER_REFERENCE_TYPE,
            InventoryTransaction.reference_id == wo.id,
            InventoryTransaction.transaction_type == TransactionType.RECEIVE,
        )
        .all()
    )
    assert [t.quantity for t in receipts] == [4.0], "and the finished good is still received"


def test_routing_demand_for_an_excluded_bom_part_is_dropped(db_session: Session):
    """The routing's ``component_part_id`` values are generated from
    ``_collect_bom_components``, which applies NONE of the alternate/optional/reference
    rules and DOES recurse into ``make``. Without the exclusion set the routing would
    re-introduce through the back door exactly the lines the explosion just declined.
    """
    user = make_user(db_session)
    wc = make_work_center(db_session)
    fg = make_part(db_session, backflush=True)
    bom = make_bom(db_session, fg)

    primary = make_part(db_session, part_type="purchased")
    alternate = make_part(db_session, part_type="purchased")
    make_inventory(db_session, primary, qty=500)
    make_inventory(db_session, alternate, qty=500)

    add_bom_item(db_session, bom, primary, quantity=1, item_number=10, alternate_group="G1")
    add_bom_item(db_session, bom, alternate, quantity=1, item_number=20, is_alternate=True, alternate_group="G1")

    wo = make_wo(db_session, fg, quantity_ordered=5, quantity_complete=5)
    make_op(db_session, wo, wc, quantity_complete=5, component_part=alternate, component_quantity=5)

    run_effects(db_session, wo, user)
    db_session.expire_all()

    assert wo_issues(db_session, wo) == {primary.id: -5.0}
    assert on_hand(db_session, alternate) == 500.0, "an alternate the BOM excluded stays excluded"


# ===========================================================================
# 6. The scrap basis — a KNOWN DIVERGENCE, documented rather than blessed
# ===========================================================================
#
# PR 4 changed the backflush basis to ``work_order.quantity_complete +
# work_order.quantity_scrapped`` and its docstring (and the plan, and the CMMC row)
# claim this is "the exact basis the per-run tie engine has always used", closing the
# case where one shop reports two different consumptions for the same physical event.
#
# IT IS NOT, and these tests deliberately do not assert that it is. The tie engine reads
# OPERATION scrap, which IS rolled up from durable ``TimeEntry`` evidence
# (``work_order_state_service`` — ``operation.quantity_scrapped`` is raised to the summed
# evidence on every reconcile). ``WorkOrder.quantity_scrapped`` has NO such rollup: the
# only writers anywhere in ``app/`` are a child reset, a ``or 0`` null-guard,
# force-complete's explicit override, and the manual office edit through
# ``WorkOrderUpdate``. The asymmetry is visible in the same service —
# ``sync_work_order_quantity_complete`` exists and has no ``quantity_scrapped`` twin.
#
# So an operator who scraps 3 of 10 at the kiosk leaves ``op.quantity_scrapped = 3`` and
# ``work_order.quantity_scrapped = 0``, and the two legs disagree by exactly the scrap:
# the tie engine consumes for 10, the backflush for 7. That is precisely the divergence
# the change claims to close. Section 6 therefore pins the divergence as a defect —
# ``test_backflush_and_tie_engine_agree_on_floor_reported_scrap`` is a STRICT xfail, so
# fixing the basis turns it into an XPASS and fails the suite until this section is
# rewritten. Do not "repair" it by relaxing the marker.


def _floor_reported_scrap_job(db_session: Session, user: User):
    """A work order whose scrap was reported ON THE FLOOR: 7 good, 3 scrapped of 10.

    The operation quantities are the ones the reconcile rolls up from durable
    ``TimeEntry`` evidence; ``work_order.quantity_complete`` is synced from the operation
    (``sync_work_order_quantity_complete``) and ``work_order.quantity_scrapped`` is left
    at 0 because nothing anywhere rolls it up. Both a TIED part and a plain BOM component
    are on the job so the two legs can be read against each other.
    """
    wc = make_work_center(db_session)
    fg = make_part(db_session, backflush=True)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    bolt = make_part(db_session, part_type="purchased")
    make_inventory(db_session, sheet, qty=500)
    make_inventory(db_session, bolt, qty=500)
    add_bom_item(db_session, make_bom(db_session, fg), bolt, quantity=1)

    wo = make_wo(db_session, fg, quantity_ordered=10, quantity_complete=7, quantity_scrapped=0)
    op = make_op(db_session, wo, wc, quantity_complete=7, quantity_scrapped=3)
    tie(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=10)

    run_effects(db_session, wo, user)
    db_session.expire_all()
    return wo, op, sheet, bolt


def test_work_order_quantity_scrapped_has_no_rollup_from_operation_evidence(client: TestClient, db_session: Session):
    """The structural fact the divergence rests on, asserted through the real reconcile.

    Durable closed-``TimeEntry`` evidence of 7 good and 3 scrapped is rolled up onto the
    OPERATION by a plain detail GET, and ``work_order.quantity_scrapped`` does not move.
    Asserted here rather than assumed, because every claim below depends on it.
    """
    admin = make_user(db_session)
    wc = make_work_center(db_session)
    fg = make_part(db_session, backflush=True)
    wo = make_wo(db_session, fg, quantity_ordered=10)
    op = make_op(db_session, wo, wc, status_=OperationStatus.IN_PROGRESS)
    db_session.add(
        TimeEntry(
            user_id=admin.id,
            work_order_id=wo.id,
            operation_id=op.id,
            work_center_id=wc.id,
            entry_type=TimeEntryType.RUN,
            clock_in=datetime.utcnow() - timedelta(hours=2),
            clock_out=datetime.utcnow() - timedelta(hours=1),
            duration_hours=1.0,
            quantity_produced=7,
            quantity_scrapped=3,
            company_id=COMPANY_A,
        )
    )
    db_session.commit()

    assert client.get(f"/api/v1/work-orders/{wo.id}", headers=headers_for(admin)).status_code == status.HTTP_200_OK
    db_session.rollback()
    db_session.expire_all()

    assert db_session.get(WorkOrderOperation, op.id).quantity_scrapped == 3, "operation scrap IS rolled up"
    assert (
        db_session.get(WorkOrder, wo.id).quantity_scrapped == 0
    ), "work-order scrap has NO rollup — this is the defect the backflush basis inherits"


def test_the_header_scrap_column_is_still_not_rolled_up(db_session: Session):
    """The underlying asymmetry that made the naive basis wrong — pinned, because the FIX
    depends on it and a future rollup would silently double-count.

    ``WorkOrder.quantity_complete`` IS rolled up from operations;
    ``WorkOrder.quantity_scrapped`` is NOT. The basis therefore sums operation scrap and
    treats the header column as a fallback. If someone ever adds a header rollup, this
    test fails and the basis must switch back to reading it — otherwise the same scrap is
    counted from both places.
    """
    user = make_user(db_session)
    wo, op, sheet, bolt = _floor_reported_scrap_job(db_session, user)

    assert db_session.get(WorkOrderOperation, op.id).quantity_scrapped == 3, "operation scrap IS rolled up"
    assert (
        db_session.get(WorkOrder, wo.id).quantity_scrapped == 0
    ), "header scrap has NO rollup — the basis sums operations because of this"


def test_backflush_and_tie_engine_agree_on_floor_reported_scrap(db_session: Session):
    """Both legs consume for the same 10 physical units — the claim, now true.

    Was a strict xfail while the basis read the un-rolled-up header column. The basis now
    sums OPERATION scrap, so one physical event produces one consumption figure whether or
    not the material happened to be tied. Do not weaken this back into an xfail: the
    divergence it guards is invisible in the data, which is how it survived a review.
    """
    user = make_user(db_session)
    wo, op, sheet, bolt = _floor_reported_scrap_job(db_session, user)

    assert [t.quantity for t in op_scoped_rows(db_session, op)] == [-10.0]
    assert wo_issues(db_session, wo) == {bolt.id: -10.0}, "the backflush must see the 3 scrapped units too"


def test_only_job_level_scrap_reaches_the_basis_today(db_session: Session):
    """The other half of the divergence: scrap the basis CAN see is scrap somebody typed.

    ``work_order.quantity_scrapped`` is only ever non-zero through force-complete's
    explicit override or the office edit, so this is what the "a fully-scrapped work order
    now backflushes something rather than nothing" claim actually amounts to today —
    reachable, but not from the floor. Documented, not endorsed; expected to change with
    the basis.
    """
    user = make_user(db_session)
    wc = make_work_center(db_session)
    fg = make_part(db_session, backflush=True)
    component = make_part(db_session, part_type="purchased")
    make_inventory(db_session, component, qty=500)
    add_bom_item(db_session, make_bom(db_session, fg), component, quantity=2)

    wo = make_wo(db_session, fg, quantity_ordered=5, quantity_complete=0, quantity_scrapped=5)
    make_op(db_session, wo, wc, quantity_complete=0, quantity_scrapped=5)

    run_effects(db_session, wo, user)
    db_session.expire_all()

    assert wo_issues(db_session, wo) == {component.id: -10.0}, "2 per unit x 5 job-level scrapped units"
    receipts = (
        db_session.query(InventoryTransaction)
        .filter(
            InventoryTransaction.company_id == COMPANY_A,
            InventoryTransaction.reference_type == WORK_ORDER_REFERENCE_TYPE,
            InventoryTransaction.reference_id == wo.id,
            InventoryTransaction.transaction_type == TransactionType.RECEIVE,
        )
        .all()
    )
    assert receipts == [], "nothing good was produced, so no finished good is received"


# ===========================================================================
# 7. THE LIVE CHANGE — the untie guard reads the signed ledger, not the cache
# ===========================================================================


def _consumed_tie_via_operation_completion(client: TestClient, db_session: Session, *, qty: float = 3.0):
    """One operation-scoped tie that consumed ``qty`` through a real completion path."""
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    operator = make_user(db_session, role=UserRole.OPERATOR)
    wc = make_work_center(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material", standard_cost=80.0)
    lot = make_inventory(db_session, sheet, qty=10.0, unit_cost=80.0, lot="SHEET-UNTIE")

    wo = make_wo(db_session, make_part(db_session), quantity_ordered=qty)
    op = make_op(db_session, wo, wc, status_=OperationStatus.IN_PROGRESS)
    make_op(db_session, wo, wc, sequence=20, status_=OperationStatus.IN_PROGRESS)  # keeps the WO open
    allocation = tie(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=qty)

    db_session.add(
        TimeEntry(
            user_id=operator.id,
            work_order_id=wo.id,
            operation_id=op.id,
            work_center_id=wc.id,
            entry_type=TimeEntryType.RUN,
            clock_in=datetime.utcnow() - timedelta(hours=2),
            clock_out=datetime.utcnow() - timedelta(hours=1),
            duration_hours=1.0,
            quantity_produced=qty,
            quantity_scrapped=0,
            company_id=COMPANY_A,
        )
    )
    db_session.commit()

    resp = client.post(
        f"/api/v1/shop-floor/operations/{op.id}/complete",
        headers=headers_for(operator),
        json={"quantity_complete": qty},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    db_session.expire_all()
    assert [t.quantity for t in op_scoped_rows(db_session, op)] == [-qty], "the tie must really have consumed"
    return supervisor, wo, op, sheet, lot, allocation


def test_untie_is_refused_while_the_ledger_still_holds_material_out(client: TestClient, db_session: Session):
    """Direction one. Cancelling a tie that moved stock, without moving it back, strands
    ``inventory_transactions.allocation_id`` rows against a tombstone with no account of
    where the material went."""
    supervisor, wo, _op, _sheet, _lot, allocation = _consumed_tie_via_operation_completion(client, db_session)

    resp = client.delete(
        f"/api/v1/work-orders/{wo.id}/material-allocations/{allocation.id}",
        headers=headers_for(supervisor),
    )
    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
    detail = resp.json()["detail"]
    assert "still issued" in detail, detail
    assert "return_and_untie" in detail, detail
    db_session.expire_all()
    assert db_session.get(WorkOrderMaterialAllocation, allocation.id).status == AllocationStatus.OPEN


def test_untie_is_permitted_once_a_full_return_nets_the_ledger_to_zero(client: TestClient, db_session: Session):
    """Direction two, and the dead end an EXISTENCE-keyed guard would have created.

    A tie whose live target has been reduced to zero and whose consumption has been fully
    returned holds no material: the ledger nets ISSUE − RETURN = 0. Keying the refusal on
    the mere existence of ledger rows — the shape the hard-delete guard uses, correctly,
    for a different question — would refuse this untie FOREVER, while ``return_and_untie``
    would 422 with nothing left to return. Both verbs closed, tie stuck OPEN.
    """
    supervisor, wo, op, sheet, _lot, allocation = _consumed_tie_via_operation_completion(client, db_session)

    # Walk the operation's count back to zero: the live target goes to zero with it,
    # which is exactly what opens a full ``correct_over_consumption`` allowance.
    reduced = client.post(
        f"/api/v1/work-orders/operations/{op.id}/reduce-production",
        headers=headers_for(supervisor),
        json={"quantity_delta": 3, "reason": "the whole tray was double-scanned"},
    )
    assert reduced.status_code == status.HTTP_200_OK, reduced.text

    returned = client.post(
        f"/api/v1/work-orders/{wo.id}/material-allocations/{allocation.id}/return",
        headers=headers_for(supervisor),
        json={"quantity": 3, "intent": "correct_over_consumption", "reason": "no sheets were cut"},
    )
    assert returned.status_code == status.HTTP_200_OK, returned.text

    db_session.expire_all()
    allocation = db_session.get(WorkOrderMaterialAllocation, allocation.id)
    assert allocation.status == AllocationStatus.OPEN, "a bounded correction leaves the tie OPEN"
    assert allocation.qty_consumed == 0.0
    assert on_hand(db_session, sheet) == 10.0, "every sheet is back on its source lot"
    assert len(op_scoped_rows(db_session, op)) == 2, "the ISSUE and its compensating RETURN both stand"

    resp = client.delete(
        f"/api/v1/work-orders/{wo.id}/material-allocations/{allocation.id}",
        headers=headers_for(supervisor),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    db_session.expire_all()
    assert db_session.get(WorkOrderMaterialAllocation, allocation.id).status == AllocationStatus.CANCELLED
    assert len(op_scoped_rows(db_session, op)) == 2, "an untie moves no stock"


def test_untie_is_refused_when_the_cache_reads_zero_but_the_ledger_does_not(client: TestClient, db_session: Session):
    """The direction that actually protects the ledger, and the only one a cache-keyed
    guard gets WRONG in the dangerous direction.

    ``qty_consumed`` is documented as non-authoritative, and the completion backflush is
    on record writing it to ``qty_planned`` rather than to the quantity the ISSUE posted
    -- so cache-below-ledger is not a hypothetical. Keyed on the cache this untie
    proceeds and leaves an ``allocation_id`` row pointing at a tombstone; keyed on the
    ledger it is refused. Same fixture shape as
    ``test_hard_delete_guard_asks_the_ledger_not_the_cache``, which has pinned the
    identical property for its own guard since PR 1.
    """
    admin = make_user(db_session)
    wc = make_work_center(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    item = make_inventory(db_session, sheet, qty=20, lot="SHEET-DRIFT")
    wo = make_wo(db_session, make_part(db_session), quantity_ordered=3)
    op = make_op(db_session, wo, wc, status_=OperationStatus.IN_PROGRESS)
    allocation = tie(db_session, wo, sheet, operation=op, qty_planned=3, qty_consumed=0.0)
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

    resp = client.delete(
        f"/api/v1/work-orders/{wo.id}/material-allocations/{allocation.id}",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
    assert "still issued" in resp.json()["detail"], resp.json()["detail"]
    db_session.expire_all()
    assert db_session.get(WorkOrderMaterialAllocation, allocation.id).status == AllocationStatus.OPEN


def test_untie_is_permitted_when_the_cache_reads_above_an_empty_ledger(client: TestClient, db_session: Session):
    """Cache drift ABOVE the ledger must not manufacture a refusal.

    ``qty_consumed`` is documented as a CACHE and the ledger as authoritative. With no
    ledger row there is nothing to strand, so the untie proceeds — the same answer the
    hard-delete guard has given since PR 1
    (``test_hard_delete_proceeds_when_no_ledger_row_references_the_tie``). This is the
    asymmetry the re-key removed: two guards protecting the same ledger were keyed to
    two different sources of truth.
    """
    admin = make_user(db_session)
    wc = make_work_center(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    wo = make_wo(db_session, make_part(db_session), quantity_ordered=5)
    op = make_op(db_session, wo, wc, status_=OperationStatus.IN_PROGRESS)
    allocation = tie(db_session, wo, sheet, operation=op, qty_planned=5, qty_consumed=2.0)
    assert op_scoped_rows(db_session, op) == [], "no ledger row backs this cache value"

    resp = client.delete(
        f"/api/v1/work-orders/{wo.id}/material-allocations/{allocation.id}",
        headers=headers_for(admin),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    db_session.expire_all()
    assert db_session.get(WorkOrderMaterialAllocation, allocation.id).status == AllocationStatus.CANCELLED


# ===========================================================================
# 8. AllocationStatus.CLOSED is still never written
# ===========================================================================


def test_no_backflush_path_ever_writes_closed(db_session: Session):
    """A CLOSED tie would vanish from ``_drop_allocation_covered_parts`` — the status-keyed
    suppression layer — and the only thing left standing between it and a double-issue
    would be the ledger layer this PR added. Neither the per-run drain nor the
    work-order-scoped backflush drain may write it.
    """
    user = make_user(db_session)
    wc = make_work_center(db_session)
    fg = make_part(db_session, backflush=True)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    stock = make_part(db_session, part_type="purchased")
    make_inventory(db_session, sheet, qty=50)
    make_inventory(db_session, stock, qty=50)

    wo = make_wo(db_session, fg, quantity_ordered=4, quantity_complete=4)
    op = make_op(db_session, wo, wc, quantity_complete=4)
    op_tie = tie(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=4)
    wo_tie = tie(db_session, wo, stock, operation=None, qty_planned=6)

    run_effects(db_session, wo, user)
    db_session.expire_all()

    assert [t.quantity for t in op_scoped_rows(db_session, op)] == [-4], "the per-run drain ran"
    assert db_session.get(WorkOrderMaterialAllocation, wo_tie.id).qty_consumed == 6.0, "the WO-scoped drain ran"
    for allocation_id in (op_tie.id, wo_tie.id):
        assert db_session.get(WorkOrderMaterialAllocation, allocation_id).status == AllocationStatus.OPEN
    assert (
        db_session.query(WorkOrderMaterialAllocation)
        .filter(WorkOrderMaterialAllocation.status == AllocationStatus.CLOSED)
        .count()
        == 0
    )
