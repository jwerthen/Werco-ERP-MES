"""``material_tie_view.tie_views_for_operations`` -- the one read both shop-floor
surfaces share.

The dispatch board asks it for every card on the board; the kiosk work-center queue
asks it every 10-15 seconds per station, all day. Both go through this function so the
manager's chip and the operator's line can never disagree about what a tie means or how
short it is, which makes its exact semantics load-bearing in two places at once.

What is pinned here:

1. **PURE READ.** It has no write path and must never grow one -- a queue poll is not an
   actor, has no intent and records no reason, so it may not move stock. Asserted
   against the ledger, the audit chain and ``qty_consumed``.
2. **``on_hand`` answers two different questions.** A PINNED tie scores against THAT LOT
   ALONE (held/inactive included -- consumption will draw from it regardless); an
   UNPINNED tie sums the lots matching the FIFO predicate verbatim, which deliberately
   EXCLUDES a NULL ``status`` because ``status = 'available'`` does not match NULL in
   SQL and FIFO will not draw from such a lot either.
3. **OPEN and OPERATION-scoped only.** ``CLOSED`` is never written by any code in
   ``app/``, so "fully consumed" is derived from the quantities and never from status --
   while "live" is the opposite and the ``OPEN`` filter is spelled out, because a
   CANCELLED tie must never paint a chip on an operator's screen.
4. **An untied operation gets NO KEY**, not an empty list -- the surface half of the
   feature's central invariant (invariant 6(d)): an untied work order behaves, and now
   looks, exactly as it did before this feature existed.
5. **Tenant isolation (invariant 1)** on all three legs: allocations, part labels and
   the inventory aggregate. An on-hand rollup that sums another company's lots is a
   security defect wearing a display bug's clothes.
6. **Bounded query budget** -- at most three SELECTs for a whole page, and ZERO for an
   empty operation list.
"""

from typing import List

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.inventory import InventoryItem, InventoryTransaction
from app.models.part import Part
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.models.work_order_material import AllocationSource, AllocationStatus, WorkOrderMaterialAllocation
from app.services.material_consumption_service import AVAILABLE_ITEM_STATUS
from app.services.material_tie_view import tie_views_for_operations

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2

_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _ensure_company(db: Session, company_id: int) -> None:
    if not db.query(Company).filter(Company.id == company_id).first():
        db.add(Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True))
        db.commit()


def make_part(db: Session, *, company_id: int = COMPANY_A, uom: str = "sheets") -> Part:
    _ensure_company(db, company_id)
    n = _next()
    part = Part(
        part_number=f"MTV-P-{n:05d}",
        name=f"Sheet stock {n}",
        part_type="raw_material",
        unit_of_measure=uom,
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def make_lot(
    db: Session,
    part: Part,
    *,
    qty: float,
    status: str = AVAILABLE_ITEM_STATUS,
    is_active: bool = True,
    company_id: int = COMPANY_A,
) -> InventoryItem:
    """One inventory lot. ``status=None`` writes the legacy NULL-status shape."""
    n = _next()
    item = InventoryItem(
        part_id=part.id,
        location="RAW-A",
        warehouse="MAIN",
        quantity_on_hand=qty,
        quantity_allocated=0.0,
        quantity_available=qty,
        lot_number=f"LOT-{n:05d}",
        unit_cost=80.0,
        status=status,
        is_active=is_active,
        company_id=company_id,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def make_operation(db: Session, *, company_id: int = COMPANY_A) -> WorkOrderOperation:
    """A queued operation on its own work order (the nest shape)."""
    _ensure_company(db, company_id)
    n = _next()
    work_center = WorkCenter(
        name=f"MTV-WC-{n}",
        code=f"MTV-WC-{n}",
        work_center_type="laser",
        hourly_rate=100,
        is_active=True,
        company_id=company_id,
    )
    db.add(work_center)
    fg = Part(
        part_number=f"MTV-FG-{n:05d}",
        name=f"Assembly {n}",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    db.add(fg)
    db.flush()
    work_order = WorkOrder(
        work_order_number=f"MTV-WO-{n:05d}",
        part_id=fg.id,
        quantity_ordered=10,
        status=WorkOrderStatus.RELEASED,
        priority=3,
        company_id=company_id,
    )
    db.add(work_order)
    db.flush()
    operation = WorkOrderOperation(
        work_order_id=work_order.id,
        work_center_id=work_center.id,
        sequence=10,
        operation_number=f"OP{n}",
        name=f"Laser cut {n}",
        operation_group="LASER",
        status=OperationStatus.READY,
        company_id=company_id,
    )
    db.add(operation)
    db.commit()
    db.refresh(operation)
    return operation


def make_tie(
    db: Session,
    operation: WorkOrderOperation,
    part: Part,
    *,
    qty_per_run: float = 1.0,
    qty_planned: float = 5.0,
    qty_consumed: float = 0.0,
    pinned: InventoryItem = None,
    status: AllocationStatus = AllocationStatus.OPEN,
    work_order_scoped: bool = False,
    company_id: int = COMPANY_A,
    work_order_id: int = None,
) -> WorkOrderMaterialAllocation:
    allocation = WorkOrderMaterialAllocation(
        company_id=company_id,
        work_order_id=work_order_id if work_order_id is not None else operation.work_order_id,
        work_order_operation_id=None if work_order_scoped else operation.id,
        part_id=part.id,
        source=AllocationSource.NEST,
        status=status,
        qty_per_run=None if work_order_scoped else qty_per_run,
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


def count_queries(db: Session, fn) -> int:
    """Statements ``fn`` runs against the session's OWN engine.

    Binds to ``db.get_bind()`` deliberately -- ``tests/`` has no ``__init__.py``, so
    ``tests.conftest`` and pytest's own ``conftest`` are two module objects with two
    independent engines, and listening on the wrong one silently counts zero. See the
    same note in ``test_dispatch_nest_details._count_select_queries``.
    """
    statements: List[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    bind = db.get_bind()
    event.listen(bind, "after_cursor_execute", _record)
    try:
        fn()
    finally:
        event.remove(bind, "after_cursor_execute", _record)
    return len(statements)


# ---------------------------------------------------------------------------
# Scope: which ties are visible at all
# ---------------------------------------------------------------------------


def test_untied_operation_gets_no_key_at_all(db_session: Session):
    """Not an empty list -- NO KEY. Callers do ``views.get(op.id)`` and render
    nothing, which is the surface half of the byte-identical-untied invariant."""
    operation = make_operation(db_session)

    views = tie_views_for_operations(db_session, company_id=COMPANY_A, operation_ids=[operation.id])

    assert views == {}
    assert operation.id not in views


def test_open_operation_scoped_tie_is_returned_with_its_fields(db_session: Session):
    operation = make_operation(db_session)
    part = make_part(db_session, uom="sheets")
    make_lot(db_session, part, qty=12.0)
    allocation = make_tie(db_session, operation, part, qty_per_run=2.0, qty_planned=10.0, qty_consumed=4.0)

    views = tie_views_for_operations(db_session, company_id=COMPANY_A, operation_ids=[operation.id])

    [view] = views[operation.id]
    assert view.allocation_id == allocation.id
    assert view.work_order_id == operation.work_order_id
    assert view.work_order_operation_id == operation.id
    assert view.part_id == part.id
    assert view.part_number == part.part_number
    assert view.part_name == part.name
    assert view.unit_of_measure == "sheets"
    assert view.qty_per_run == 2.0
    assert view.qty_planned == 10.0
    assert view.qty_consumed == 4.0
    assert view.on_hand == 12.0
    assert view.qty_remaining == 6.0
    assert view.short_by == 0.0


def test_cancelled_tie_is_excluded(db_session: Session):
    """The OPEN filter is spelled out rather than "not cancelled": a CANCELLED tie
    must never paint a chip on an operator's screen."""
    operation = make_operation(db_session)
    part = make_part(db_session)
    make_lot(db_session, part, qty=50.0)
    make_tie(db_session, operation, part, status=AllocationStatus.CANCELLED)

    assert tie_views_for_operations(db_session, company_id=COMPANY_A, operation_ids=[operation.id]) == {}


def test_closed_tie_is_excluded_too(db_session: Session):
    """``CLOSED`` is reserved and never written by ``app/`` today. The filter is
    ``== OPEN`` precisely so it stays correct the day something writes it."""
    operation = make_operation(db_session)
    part = make_part(db_session)
    make_lot(db_session, part, qty=50.0)
    make_tie(db_session, operation, part, status=AllocationStatus.CLOSED)

    assert tie_views_for_operations(db_session, company_id=COMPANY_A, operation_ids=[operation.id]) == {}


def test_work_order_scoped_tie_is_excluded(db_session: Session):
    """A work-order-scoped tie belongs to the whole job and drains through the
    one-shot backflush; hanging it on operations would fan one tie across every
    card of that work order and read as N separate ties."""
    operation = make_operation(db_session)
    part = make_part(db_session)
    make_lot(db_session, part, qty=50.0)
    make_tie(db_session, operation, part, work_order_scoped=True)

    assert tie_views_for_operations(db_session, company_id=COMPANY_A, operation_ids=[operation.id]) == {}


def test_fully_consumed_but_still_open_tie_reports_zero_remaining(db_session: Session):
    """Nothing in ``app/`` ever writes ``CLOSED``, so a fully consumed tie stays
    OPEN forever. "Fully consumed" must therefore be derived from the QUANTITIES
    (``qty_remaining == 0``), never from status."""
    operation = make_operation(db_session)
    part = make_part(db_session)
    make_lot(db_session, part, qty=1.0)
    allocation = make_tie(db_session, operation, part, qty_planned=5.0, qty_consumed=5.0)

    [view] = tie_views_for_operations(db_session, company_id=COMPANY_A, operation_ids=[operation.id])[operation.id]

    assert allocation.status == AllocationStatus.OPEN, "the fixture must model the OPEN-forever shape"
    assert view.qty_consumed == 5.0
    assert view.qty_remaining == 0.0
    # Nothing left to draw, so a thin lot is NOT a shortage.
    assert view.short_by == 0.0


def test_float_residue_does_not_paint_a_false_shortage(db_session: Session):
    """These are ``Float`` columns: a covered tie can land at 4e-16 remaining. A
    bare ``max(0, ...)`` would let that through and flag a SHORTAGE on a fully
    stocked tie -- a false alarm is how operators learn to ignore the indicator."""
    operation = make_operation(db_session)
    part = make_part(db_session)
    make_lot(db_session, part, qty=0.0000000001)
    make_tie(db_session, operation, part, qty_planned=3.0, qty_consumed=2.9999999999)

    [view] = tie_views_for_operations(db_session, company_id=COMPANY_A, operation_ids=[operation.id])[operation.id]

    assert view.qty_remaining == 0.0
    assert view.short_by == 0.0


def test_qty_per_run_is_carried_raw_including_null(db_session: Session):
    """A NULL means "not run-scaled" (reads as 1.0) which is NOT the same fact as
    an explicit 1 -- the tie editor needs to tell them apart, so the COALESCE is
    the caller's job, not this module's."""
    explicit = make_operation(db_session)
    implicit = make_operation(db_session)
    part = make_part(db_session)
    make_lot(db_session, part, qty=50.0)
    make_tie(db_session, explicit, part, qty_per_run=1.0)
    tie = make_tie(db_session, implicit, part)
    tie.qty_per_run = None
    db_session.commit()

    views = tie_views_for_operations(db_session, company_id=COMPANY_A, operation_ids=[explicit.id, implicit.id])

    assert views[explicit.id][0].qty_per_run == 1.0
    assert views[implicit.id][0].qty_per_run is None


def test_ties_are_ordered_by_allocation_id_within_an_operation(db_session: Session):
    """Stable, and identical to ``open_allocations_for_work_order`` -- so the
    board chip (which shows the FIRST tie) and the kiosk's first listed line are
    the same tie, poll after poll."""
    operation = make_operation(db_session)
    first = make_tie(db_session, operation, make_part(db_session))
    second = make_tie(db_session, operation, make_part(db_session))
    third = make_tie(db_session, operation, make_part(db_session))

    views = tie_views_for_operations(db_session, company_id=COMPANY_A, operation_ids=[operation.id])

    assert [v.allocation_id for v in views[operation.id]] == sorted([first.id, second.id, third.id])


def test_duplicate_operation_ids_do_not_duplicate_rows(db_session: Session):
    """The same operation can appear twice in a caller's list (two cards of one
    job); the IN list is de-duplicated, so the tie is not reported twice."""
    operation = make_operation(db_session)
    part = make_part(db_session)
    make_lot(db_session, part, qty=50.0)
    make_tie(db_session, operation, part)

    views = tie_views_for_operations(
        db_session, company_id=COMPANY_A, operation_ids=[operation.id, operation.id, operation.id]
    )

    assert len(views[operation.id]) == 1


# ---------------------------------------------------------------------------
# on_hand -- the unpinned (FIFO-predicate) leg
# ---------------------------------------------------------------------------


def test_unpinned_tie_sums_only_fifo_eligible_lots(db_session: Session):
    """The predicate is ``_fifo_source_items`` verbatim: active AND available AND
    on-hand > 0. Anything looser promises stock FIFO will refuse to touch."""
    operation = make_operation(db_session)
    part = make_part(db_session)
    make_lot(db_session, part, qty=4.0)  # counted
    make_lot(db_session, part, qty=6.0)  # counted
    make_lot(db_session, part, qty=100.0, status="on_hold")  # held
    make_lot(db_session, part, qty=100.0, status="quarantine")
    make_lot(db_session, part, qty=100.0, is_active=False)  # inactive
    make_lot(db_session, part, qty=0.0)  # nothing on the shelf
    make_tie(db_session, operation, part, qty_planned=20.0)

    [view] = tie_views_for_operations(db_session, company_id=COMPANY_A, operation_ids=[operation.id])[operation.id]

    assert view.on_hand == 10.0
    assert view.qty_remaining == 20.0
    assert view.short_by == 10.0


def test_null_status_lot_stays_excluded_from_the_unpinned_sum(db_session: Session):
    """A legacy NULL ``status`` does NOT match ``= 'available'`` in SQL, and that
    asymmetry is intentional rather than a bug to fix here.

    ``is_consumable_item`` reads a NULL as available (the column default) while the
    FIFO *query* skips it -- so such a lot is passed over rather than silently
    consumed. This view must show what the engine will actually DRAW FROM, so it
    follows the SQL. The positive control alongside proves the exclusion is the
    status predicate and not a broken fixture.
    """
    operation = make_operation(db_session)
    part = make_part(db_session)
    make_lot(db_session, part, qty=7.0)  # available -- the positive control
    null_status = make_lot(db_session, part, qty=93.0)
    null_status.status = None
    db_session.commit()
    make_tie(db_session, operation, part, qty_planned=50.0)

    [view] = tie_views_for_operations(db_session, company_id=COMPANY_A, operation_ids=[operation.id])[operation.id]

    assert db_session.get(InventoryItem, null_status.id).status is None, "the fixture must really be NULL"
    assert view.on_hand == 7.0
    assert view.short_by == 43.0


def test_unpinned_sum_never_reaches_across_tenants(db_session: Session):
    """Invariant 1 on an aggregate. A lot row carrying another company's
    ``company_id`` -- the exact shape a leak takes -- must not be summed in, and
    the failure direction (reading as short) is the conservative one."""
    operation = make_operation(db_session)
    part = make_part(db_session)
    _ensure_company(db_session, COMPANY_B)
    make_lot(db_session, part, qty=2.0, company_id=COMPANY_A)
    make_lot(db_session, part, qty=999.0, company_id=COMPANY_B)
    make_tie(db_session, operation, part, qty_planned=10.0)

    [view] = tie_views_for_operations(db_session, company_id=COMPANY_A, operation_ids=[operation.id])[operation.id]

    assert view.on_hand == 2.0
    assert view.short_by == 8.0


# ---------------------------------------------------------------------------
# on_hand -- the pinned (single-lot) leg
# ---------------------------------------------------------------------------


def test_pinned_tie_is_scored_against_that_lot_alone(db_session: Session):
    """Pinning is a lot-directed instruction and ``_consume_one_allocation``
    honors it absolutely -- an insufficient pinned lot is driven NEGATIVE rather
    than spilled onto a different (uncertified, wrong-heat) lot. Scoring the pin
    against the part's total would paint a green chip over stock consumption will
    never touch."""
    operation = make_operation(db_session)
    part = make_part(db_session)
    pinned = make_lot(db_session, part, qty=3.0)
    make_lot(db_session, part, qty=500.0)  # plenty -- and irrelevant to this tie
    make_tie(db_session, operation, part, qty_planned=10.0, pinned=pinned)

    [view] = tie_views_for_operations(db_session, company_id=COMPANY_A, operation_ids=[operation.id])[operation.id]

    assert view.pinned_inventory_item_id == pinned.id
    assert view.pinned_lot_number == pinned.lot_number
    assert view.on_hand == 3.0
    assert view.short_by == 7.0


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"status": "on_hold"}, id="held"),
        pytest.param({"status": "quarantine"}, id="quarantined"),
        pytest.param({"is_active": False}, id="inactive"),
    ],
)
def test_pinned_lot_reports_its_on_hand_even_when_held_or_inactive(db_session: Session, kwargs):
    """Consumption proceeds from a pinned lot regardless of hold state (writing a
    ``HELD_MATERIAL_CONSUMED`` audit row), so this view reports what is physically
    there rather than zeroing it and inventing a shortage that will not happen."""
    operation = make_operation(db_session)
    part = make_part(db_session)
    pinned = make_lot(db_session, part, qty=9.0, **kwargs)
    make_tie(db_session, operation, part, qty_planned=5.0, pinned=pinned)

    [view] = tie_views_for_operations(db_session, company_id=COMPANY_A, operation_ids=[operation.id])[operation.id]

    assert view.on_hand == 9.0
    assert view.short_by == 0.0


def test_pinned_lot_that_is_missing_reads_zero(db_session: Session):
    operation = make_operation(db_session)
    part = make_part(db_session)
    make_lot(db_session, part, qty=400.0)  # the part has stock; the PIN does not
    tie = make_tie(db_session, operation, part, qty_planned=6.0)
    tie.pinned_inventory_item_id = 999_999
    tie.pinned_lot_number = "GONE"
    db_session.commit()

    [view] = tie_views_for_operations(db_session, company_id=COMPANY_A, operation_ids=[operation.id])[operation.id]

    assert view.on_hand == 0.0
    assert view.short_by == 6.0


def test_pinned_lot_belonging_to_another_tenant_reads_zero(db_session: Session):
    """Company-scoped on both legs. A pin naming another tenant's lot yields 0.0
    rather than that tenant's quantity."""
    operation = make_operation(db_session)
    part = make_part(db_session)
    _ensure_company(db_session, COMPANY_B)
    foreign_lot = make_lot(db_session, part, qty=250.0, company_id=COMPANY_B)
    tie = make_tie(db_session, operation, part, qty_planned=6.0)
    tie.pinned_inventory_item_id = foreign_lot.id
    tie.pinned_lot_number = foreign_lot.lot_number
    db_session.commit()

    [view] = tie_views_for_operations(db_session, company_id=COMPANY_A, operation_ids=[operation.id])[operation.id]

    assert view.on_hand == 0.0
    assert view.short_by == 6.0


def test_a_pinned_ties_part_total_is_not_borrowed_by_the_pin(db_session: Session):
    """Both legs in one call: the pinned tie sees only its lot, the unpinned tie
    on the SAME part sees the FIFO total. A pinned lot's part is deliberately not
    added to the part-total leg."""
    pinned_op = make_operation(db_session)
    unpinned_op = make_operation(db_session)
    part = make_part(db_session)
    pinned = make_lot(db_session, part, qty=2.0)
    make_lot(db_session, part, qty=30.0)
    make_tie(db_session, pinned_op, part, qty_planned=4.0, pinned=pinned)
    make_tie(db_session, unpinned_op, part, qty_planned=4.0)

    views = tie_views_for_operations(db_session, company_id=COMPANY_A, operation_ids=[pinned_op.id, unpinned_op.id])

    assert views[pinned_op.id][0].on_hand == 2.0
    assert views[unpinned_op.id][0].on_hand == 32.0


# ---------------------------------------------------------------------------
# Tenant isolation of the tie rows themselves
# ---------------------------------------------------------------------------


def test_another_tenants_tie_on_the_same_operation_is_invisible(db_session: Session):
    """The leak shape: an allocation row stamped with Company B pointing at
    Company A's operation. Reading company A must not see it, and reading company
    B must not see A's."""
    operation = make_operation(db_session)
    _ensure_company(db_session, COMPANY_B)
    part_a = make_part(db_session, company_id=COMPANY_A)
    part_b = make_part(db_session, company_id=COMPANY_B)
    make_lot(db_session, part_a, qty=10.0)
    mine = make_tie(db_session, operation, part_a)
    theirs = make_tie(db_session, operation, part_b, company_id=COMPANY_B)

    a_views = tie_views_for_operations(db_session, company_id=COMPANY_A, operation_ids=[operation.id])
    b_views = tie_views_for_operations(db_session, company_id=COMPANY_B, operation_ids=[operation.id])

    assert [v.allocation_id for v in a_views[operation.id]] == [mine.id]
    assert [v.allocation_id for v in b_views[operation.id]] == [theirs.id]


def test_part_labels_never_cross_tenants(db_session: Session):
    """The label lookup is tenant-scoped too: a tie pointing at another company's
    part renders blank rather than disclosing that part's number."""
    operation = make_operation(db_session)
    _ensure_company(db_session, COMPANY_B)
    foreign_part = make_part(db_session, company_id=COMPANY_B)
    make_tie(db_session, operation, foreign_part, company_id=COMPANY_A)

    [view] = tie_views_for_operations(db_session, company_id=COMPANY_A, operation_ids=[operation.id])[operation.id]

    assert view.part_number is None
    assert view.part_name is None


def test_soft_deleted_part_still_labels_its_tie(db_session: Session):
    """Deliberately NOT filtered on ``is_deleted``: this is a label lookup for a
    row the tie already references. Dropping it would hide nothing -- the tie
    still exists and still consumes -- it would just blank the part number for
    the operator holding the material."""
    operation = make_operation(db_session)
    part = make_part(db_session)
    make_lot(db_session, part, qty=10.0)
    make_tie(db_session, operation, part)
    part.is_deleted = True
    db_session.commit()

    [view] = tie_views_for_operations(db_session, company_id=COMPANY_A, operation_ids=[operation.id])[operation.id]

    assert view.part_number == part.part_number


# ---------------------------------------------------------------------------
# Query budget + the pure-read guarantee
# ---------------------------------------------------------------------------


def test_empty_operation_ids_issues_zero_queries(db_session: Session):
    """The dispatch board renders an empty column often, and a page of untied
    work must cost nothing at all."""
    assert (
        count_queries(db_session, lambda: tie_views_for_operations(db_session, company_id=COMPANY_A, operation_ids=[]))
        == 0
    )


def test_all_none_operation_ids_also_short_circuit(db_session: Session):
    """The second short-circuit: a list that de-duplicates down to nothing."""
    assert (
        count_queries(
            db_session,
            lambda: tie_views_for_operations(db_session, company_id=COMPANY_A, operation_ids=[None, None]),
        )
        == 0
    )


def test_untied_page_costs_exactly_one_select(db_session: Session):
    """A page of untied operations pays for the allocation probe and stops there
    -- no part lookup, no inventory aggregate."""
    # Ids materialized BEFORE expiring: reading ``op.id`` off an expired instance
    # inside the counted callable would issue a refresh SELECT per operation and
    # measure the fixture rather than the function under test.
    operation_ids = [make_operation(db_session).id for _ in range(6)]
    db_session.expire_all()

    cost = count_queries(
        db_session,
        lambda: tie_views_for_operations(db_session, company_id=COMPANY_A, operation_ids=operation_ids),
    )
    assert cost == 1


def test_query_budget_is_three_and_flat_in_the_number_of_ties(db_session: Session):
    """At most THREE SELECTs for a whole page: allocations, part labels, one
    inventory read. The kiosk polls this every 10-15s per station shop-wide, so
    the cost must not grow with the queue -- extend it by adding a UNION leg, not
    a query."""
    first = make_operation(db_session)
    part = make_part(db_session)
    make_lot(db_session, part, qty=10.0)
    make_tie(db_session, first, part)
    # Ids captured before expiring -- see the note in the test above.
    first_id = first.id
    db_session.expire_all()

    one_tie = count_queries(
        db_session,
        lambda: tie_views_for_operations(db_session, company_id=COMPANY_A, operation_ids=[first_id]),
    )

    operation_ids = [first_id]
    for _ in range(9):
        operation = make_operation(db_session)
        other = make_part(db_session)
        make_lot(db_session, other, qty=10.0)
        # Half pinned, half unpinned, so BOTH union legs are populated.
        pinned = make_lot(db_session, other, qty=4.0) if len(operation_ids) % 2 else None
        make_tie(db_session, operation, other, pinned=pinned)
        operation_ids.append(operation.id)
    db_session.expire_all()

    ten_ties = count_queries(
        db_session,
        lambda: tie_views_for_operations(db_session, company_id=COMPANY_A, operation_ids=operation_ids),
    )

    assert one_tie == 3
    assert ten_ties == one_tie, "the tie read must be flat in the number of ties, not per-row"


def test_the_view_builder_writes_nothing(db_session: Session):
    """It is a PURE READ and must never grow a write path. ``consume_tied_materials
    _for_work_order`` posts ISSUE rows, audit rows and shortage events; this must
    post none of them, even when the operation has completed runs and a genuine
    positive delta is available to post."""
    operation = make_operation(db_session)
    operation.quantity_complete = 3
    operation.quantity_scrapped = 1
    db_session.commit()
    part = make_part(db_session)
    lot = make_lot(db_session, part, qty=25.0)
    allocation = make_tie(db_session, operation, part, qty_per_run=1.0, qty_planned=5.0)

    txns_before = db_session.query(InventoryTransaction).count()
    audit_before = db_session.query(AuditLog).count()

    views = tie_views_for_operations(db_session, company_id=COMPANY_A, operation_ids=[operation.id])
    db_session.commit()  # anything pending would land here
    db_session.expire_all()

    assert views[operation.id], "the read must have actually seen the tie"
    assert db_session.query(InventoryTransaction).count() == txns_before
    assert db_session.query(AuditLog).count() == audit_before
    assert db_session.get(WorkOrderMaterialAllocation, allocation.id).qty_consumed == 0.0
    assert db_session.get(InventoryItem, lot.id).quantity_on_hand == 25.0
