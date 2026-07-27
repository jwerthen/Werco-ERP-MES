"""PR 4.4 behavior locks: one lot policy, and reconcile-to-target under a new shape.

PR 4.4 does two things to the work-order component-consumption leg, and this file is
where each is pinned:

**(a) ONE lot-selection policy.** Both engines now draw through
``material_consumption_service.consumable_source_items`` -- ``received_date`` FIFO over
active, consumable, on-hand lots, spilling across as many as the demand needs. The
backflush used to take the LOWEST-ID active row, ignore ``status`` entirely and write ONE
row for the whole quantity, so on the same material the two engines could name different
heats for one physical draw (AS9100D 8.5.2). The shared predicate is NULL-status-tolerant
(``COALESCE(status, 'available')``), so the widening cannot hide legacy stock; held /
inactive lots are SKIPPED rather than consumed, and the skip is DISCLOSED on the shortage
row.

**(b) RECONCILE-TO-TARGET under ``reference_type='work_order_backflush'``.** Each demand
source -- the BOM/routing explosion and each open work-order-scoped tie -- computes
``delta = target - signed ledger net of its own history`` and posts only a positive
delta. That shape is covered by NO unique index, deliberately: reconciliation needs N
rows per (work order, part) plus a later top-up row, and a one-row-per-(company, WO,
ISSUE, part) index cannot express either. ``_component_already_issued`` is kept keyed
VERBATIM on the legacy ``('work_order', ISSUE)`` shape and becomes a permanent fence over
pre-4.4 rows.

===========================================================================
THE FIFO TRAP -- read this before writing ANY lot-ordering test in this repo
===========================================================================

``make_inventory`` defaults ``received_date=None`` in EVERY sibling fixture in this
feature (``test_material_consumption.py``, ``test_backflush_breadth.py``,
``test_completion_inventory_batch6.py``, ``test_material_consumption_operation_trigger``).
With every date NULL, ``ORDER BY id`` and ``ORDER BY (received_date IS NULL,
received_date ASC, id ASC)`` return **the same rows in the same order** -- so the OLD
lowest-id policy and the NEW FIFO policy are indistinguishable, and a lot-ordering test
passes while proving nothing at all.

Every lot-ordering test here therefore passes EXPLICIT, NON-MONOTONIC ``received_date``
values, arranged so that **insertion order and FIFO order DISAGREE**: the older-dated lot
is inserted second (or third), giving it the HIGHER id. A test whose oldest lot also has
the lowest id is worthless. ``test_fifo_draw_follows_received_date_not_insertion_order``
asserts that guard directly and asserts the fixture's own id-vs-date inversion, so it
cannot silently decay if someone "tidies" the setup.

===========================================================================
Stated limitation, not papered over
===========================================================================

``with_for_update`` is a documented no-op on the SQLite test backend, so the row-lock half
of the serialization argument cannot be EXECUTED by pytest. The lock is pinned instead as
a structural fact by a source-level assertion
(``test_every_completion_handler_locks_the_work_order_before_the_seam``) -- the same
posture ``_lock_return_scope`` already takes. SQLite also does not enforce foreign keys,
which is why the IntegrityError test below manufactures a REAL unique-index violation
rather than relying on an FK.
"""

import inspect
import re
from datetime import date, datetime, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

import app.services.completion_inventory_service as cis
import app.services.material_consumption_service as mcs
from app.core.security import create_access_token
from app.db.ledger_filter import (
    BACKFLUSH_REFERENCE_TYPE,
    OPERATION_REFERENCE_TYPE,
    WORK_ORDER_REFERENCE_TYPE,
)
from app.models.audit_log import AuditLog
from app.models.bom import BOM, BOMItem
from app.models.company import Company
from app.models.inventory import InventoryItem, InventoryTransaction, TransactionType
from app.models.operational_event import OperationalEvent
from app.models.part import Part
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.models.work_order_material import AllocationSource, AllocationStatus, WorkOrderMaterialAllocation
from app.services.analytics_service import AnalyticsService
from app.services.audit_service import AuditService
from app.services.completion_cost_service import _issued_material_cost
from app.services.completion_inventory_service import (
    BACKFLUSH_COMPONENT_FAILED_AUDIT_ACTION,
    BACKFLUSH_COMPONENT_FAILED_EVENT_TYPE,
    BACKFLUSH_DOUBLE_ISSUE_BLOCKED_AUDIT_ACTION,
    BACKFLUSH_SHORTAGE_AUDIT_ACTION,
    BACKFLUSH_SHORTAGE_EVENT_TYPE,
    apply_completion_inventory_effects,
    apply_operation_completion_inventory_effects,
    backflush_components_for_work_order,
    backflush_net_issued_by_part,
    net_consumed_quantity_for_allocation,
    operation_scoped_net_issued_by_part,
)
from app.services.material_consumption_service import (
    ALLOCATION_CONSUMPTION_FAILED_AUDIT_ACTION,
    ALLOCATION_CONSUMPTION_FAILED_EVENT_TYPE,
    ALLOCATION_SHORTAGE_AUDIT_ACTION,
    HELD_MATERIAL_CONSUMED_AUDIT_ACTION,
    consumable_source_items,
    held_stock_disclosure,
    held_stock_summary,
    is_consumable_item,
    shortage_draw_disclosure,
)
from app.services.material_tie_view import tie_views_for_operations
from app.services.notification_catalog import entry_for_event_type

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"
_seq = {"n": 0}

# A fixed clock so every ``received_date`` in this file is written by hand. See THE FIFO
# TRAP above: a default of ``None`` here would silently collapse FIFO onto id order.
_EPOCH = datetime(2026, 3, 1, 12, 0, 0)


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def days(n: int) -> datetime:
    """``received_date`` n days after the fixture epoch. Lower n == OLDER == drawn FIRST."""
    return _EPOCH + timedelta(days=n)


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
        email=f"blp-{n}@co{company_id}.test",
        employee_id=f"BLP-{n:05d}",
        first_name="Lot",
        last_name="Policy",
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
        part_number=f"BLP-P-{n}",
        name=f"Part {n}",
        description="backflush lot-policy fixture part",
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
        name=f"BLP-WC-{n}",
        code=f"BLP-WC-{n}",
        work_center_type="laser",
        description="backflush lot-policy fixture work center",
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
        work_order_number=f"BLP-WO-{n:05d}",
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
        company_id=company_id,
    )
    db.add(op)
    db.commit()
    db.refresh(op)
    return op


def make_lot(
    db: Session,
    part: Part,
    *,
    qty: float,
    lot: str,
    received_date: datetime = None,
    unit_cost: float = 2.0,
    status_: str = "available",
    is_active: bool = True,
    location: str = "RAW-A",
    company_id: int = COMPANY_A,
) -> InventoryItem:
    """One stock lot.

    ``received_date`` is an ORDINARY parameter with a ``None`` default, exactly like the
    sibling fixtures -- but every ordering test in this file passes it explicitly. See
    THE FIFO TRAP in the module docstring: a NULL date makes FIFO and lowest-id agree,
    which is how a lot-ordering test comes to prove nothing.
    """
    item = InventoryItem(
        part_id=part.id,
        location=location,
        warehouse="MAIN",
        quantity_on_hand=qty,
        quantity_allocated=0.0,
        quantity_available=qty,
        lot_number=lot,
        unit_cost=unit_cost,
        received_date=received_date,
        status=status_,
        is_active=is_active,
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
    company_id: int = COMPANY_A,
) -> BOMItem:
    item = BOMItem(
        bom_id=bom.id,
        component_part_id=component.id,
        item_number=item_number,
        quantity=quantity,
        item_type="buy",
        line_type="component",
        scrap_factor=0.0,
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
    pinned: InventoryItem = None,
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
        pinned_inventory_item_id=pinned.id if pinned is not None else None,
        pinned_lot_number=pinned.lot_number if pinned is not None else None,
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


def backflush_rows(
    db: Session, wo: WorkOrder, part: Part = None, *, company_id: int = COMPANY_A
) -> list[InventoryTransaction]:
    """Every ``work_order_backflush`` row for a work order, oldest-inserted first.

    Ordered by ``id`` on purpose: insertion order IS draw order, which is what makes the
    FIFO assertions below meaningful.
    """
    query = db.query(InventoryTransaction).filter(
        InventoryTransaction.company_id == company_id,
        InventoryTransaction.reference_type == BACKFLUSH_REFERENCE_TYPE,
        InventoryTransaction.reference_id == wo.id,
    )
    if part is not None:
        query = query.filter(InventoryTransaction.part_id == part.id)
    return query.order_by(InventoryTransaction.id).all()


def issue_rows(db: Session, wo: WorkOrder, part: Part = None, **kwargs) -> list[InventoryTransaction]:
    return [r for r in backflush_rows(db, wo, part, **kwargs) if r.transaction_type == TransactionType.ISSUE]


def audit_rows(db: Session, action: str, *, company_id: int = COMPANY_A) -> list[AuditLog]:
    return (
        db.query(AuditLog)
        .filter(AuditLog.company_id == company_id, AuditLog.action == action)
        .order_by(AuditLog.id)
        .all()
    )


def on_hand(db: Session, part: Part, *, company_id: int = COMPANY_A) -> float:
    return sum(
        float(row.quantity_on_hand or 0)
        for row in db.query(InventoryItem)
        .filter(InventoryItem.company_id == company_id, InventoryItem.part_id == part.id)
        .all()
    )


def backflush_job(
    db: Session,
    *,
    component: Part,
    qty_per_unit: float = 1.0,
    quantity_complete: float = 5.0,
    quantity_ordered: float = 5.0,
) -> WorkOrder:
    """A work order whose finished part opted into the BOM/routing backflush."""
    fg = make_part(db, backflush=True)
    add_bom_item(db, make_bom(db, fg), component, quantity=qty_per_unit)
    wo = make_wo(db, fg, quantity_ordered=quantity_ordered, quantity_complete=quantity_complete)
    make_op(db, wo, make_work_center(db), quantity_complete=quantity_complete)
    return wo


# ===========================================================================
# 1. GOAL (a) -- ONE lot-selection policy
# ===========================================================================


def test_fifo_draw_follows_received_date_not_insertion_order(db_session: Session):
    """**THE TRAP TEST.** Three lots whose id order is the exact REVERSE of their date order.

    This is the only assertion in the file that can tell the new policy apart from the
    old one on its own. The backflush used to take ``ORDER BY id`` -- with every
    ``received_date`` NULL (the sibling fixtures' default) that is byte-identical to
    ``ORDER BY received_date IS NULL, received_date ASC, id ASC``, so a lot-ordering test
    written on those fixtures passes under BOTH policies and proves nothing.

    Here the newest lot is inserted first and therefore holds the LOWEST id. A draw that
    still followed id order would open with ``NEWEST``; FIFO opens with ``OLDEST``. The
    fixture's own inversion is asserted before the draw, so it cannot decay.
    """
    user = make_user(db_session)
    component = make_part(db_session, part_type="purchased")

    # Inserted newest-first: id order ASCENDING, date order DESCENDING.
    newest = make_lot(db_session, component, qty=10, lot="LOT-NEWEST", received_date=days(30))
    middle = make_lot(db_session, component, qty=10, lot="LOT-MIDDLE", received_date=days(20))
    oldest = make_lot(db_session, component, qty=10, lot="LOT-OLDEST", received_date=days(10))

    assert newest.id < middle.id < oldest.id, "the fixture must give the OLDEST lot the HIGHEST id"
    assert oldest.received_date < middle.received_date < newest.received_date, "…and the oldest date"

    wo = backflush_job(db_session, component=component, qty_per_unit=5.0, quantity_complete=5.0)
    run_effects(db_session, wo, user)
    db_session.expire_all()

    rows = issue_rows(db_session, wo, component)
    assert [(r.lot_number, r.quantity) for r in rows] == [
        ("LOT-OLDEST", -10.0),
        ("LOT-MIDDLE", -10.0),
        ("LOT-NEWEST", -5.0),
    ], "the draw must follow received_date, NOT insertion order"
    assert sum(r.quantity for r in rows) == -25.0
    assert db_session.get(InventoryItem, oldest.id).quantity_on_hand == 0
    assert db_session.get(InventoryItem, middle.id).quantity_on_hand == 0
    assert db_session.get(InventoryItem, newest.id).quantity_on_hand == 5


def test_backflush_unpinned_spills_across_lots(db_session: Session):
    """One logical draw becomes N rows naming N heats — which is the whole point.

    The old leg wrote ONE row for the full quantity against a single lot, so a 25-unit
    draw off three 10-unit lots recorded a genealogy naming one heat and drove that lot
    15 negative. The as-built record now names every heat that actually went into the
    part (AS9100D 8.5.2), and no lot is driven negative while stock remains elsewhere.
    """
    user = make_user(db_session)
    component = make_part(db_session, part_type="purchased")
    for offset, name in ((30, "SPILL-C"), (10, "SPILL-A"), (20, "SPILL-B")):
        make_lot(db_session, component, qty=10, lot=name, received_date=days(offset))

    wo = backflush_job(db_session, component=component, qty_per_unit=5.0, quantity_complete=5.0)
    run_effects(db_session, wo, user)
    db_session.expire_all()

    rows = issue_rows(db_session, wo, component)
    assert [(r.lot_number, r.quantity) for r in rows] == [
        ("SPILL-A", -10.0),
        ("SPILL-B", -10.0),
        ("SPILL-C", -5.0),
    ]
    assert len({r.lot_number for r in rows}) == 3, "three DISTINCT lots on the as-built record"
    assert sum(r.quantity for r in rows) == -25.0
    assert on_hand(db_session, component) == 5.0, "30 on hand less the 25 drawn — nothing driven negative"
    assert audit_rows(db_session, BACKFLUSH_SHORTAGE_AUDIT_ACTION) == [], "stock covered the demand"


def test_backflush_skips_held_lots_and_discloses_them(db_session: Session):
    """Held stock is SKIPPED (AS9100D 8.7) and the skip is DISCLOSED on the shortage row.

    "Adopt the per-run engine's rule" cuts both ways: the unpinned leg may no longer draw
    ``on_hold`` / ``quarantine`` / ``rejected`` material into product, but a part whose
    stock is mostly segregated must not then report a BARE shortage against material
    physically sitting on the rack. The chain row has to let a reader tell a purchasing
    problem from an MRB problem, so it carries the skipped quantity and the lot numbers.
    """
    user = make_user(db_session)
    component = make_part(db_session, part_type="purchased")
    held = make_lot(db_session, component, qty=60, lot="HELD-60", received_date=days(5), status_="on_hold")
    good = make_lot(db_session, component, qty=10, lot="GOOD-10", received_date=days(9))

    wo = backflush_job(db_session, component=component, qty_per_unit=10.0, quantity_complete=5.0)
    run_effects(db_session, wo, user)
    db_session.expire_all()

    assert db_session.get(InventoryItem, held.id).quantity_on_hand == 60, "the held lot is never drawn"
    rows = issue_rows(db_session, wo, component)
    assert [(r.lot_number, r.quantity) for r in rows] == [
        ("GOOD-10", -10.0),
        ("GOOD-10", -40.0),
    ], "10 drawn, and the 40 that could not be is still recorded against the same lot"
    assert db_session.get(InventoryItem, good.id).quantity_on_hand == -40

    assert (
        audit_rows(db_session, HELD_MATERIAL_CONSUMED_AUDIT_ACTION) == []
    ), "the unpinned leg no longer consumes held material, so it owes no 8.7 consumption row"
    shortage = audit_rows(db_session, BACKFLUSH_SHORTAGE_AUDIT_ACTION)
    assert len(shortage) == 1
    extra = shortage[0].extra_data or {}
    assert extra["shortfall"] == 40.0
    assert extra["available_quantity"] == 10.0, "available is summed over the lots actually walked"
    assert extra["held_quantity_skipped"] == 60.0
    assert extra["held_lot_numbers"] == ["HELD-60"]
    assert "60" in (shortage[0].description or "") and "segregated status" in (shortage[0].description or "")


def test_backflush_null_status_lot_is_consumable_and_quarantine_is_not(db_session: Session):
    """The ONE genuinely live widening, with its negative control.

    ``inventory_items.status`` has a Python-side default and no ``server_default``, so a
    legacy row written outside the ORM carries NULL. ``is_consumable_item`` has always
    read that as available; the SQL did not, which meant the engine skipped real stock and
    then recorded a false shortage against it. ``COALESCE(status, 'available')`` closes
    that. It is a strict WIDENING and can never make a held lot consumable, which is what
    the quarantine control proves.
    """
    user = make_user(db_session)
    component = make_part(db_session, part_type="purchased")
    legacy = make_lot(db_session, component, qty=8, lot="LEGACY-NULL", received_date=days(3))
    legacy.status = None
    quarantined = make_lot(
        db_session, component, qty=99, lot="QUARANTINED", received_date=days(1), status_="quarantine"
    )
    db_session.commit()
    assert db_session.get(InventoryItem, legacy.id).status is None, "the fixture must really be NULL"

    wo = backflush_job(db_session, component=component, qty_per_unit=1.0, quantity_complete=5.0)
    run_effects(db_session, wo, user)
    db_session.expire_all()

    rows = issue_rows(db_session, wo, component)
    assert [(r.lot_number, r.quantity) for r in rows] == [("LEGACY-NULL", -5.0)]
    assert db_session.get(InventoryItem, legacy.id).quantity_on_hand == 3
    assert db_session.get(InventoryItem, quarantined.id).quantity_on_hand == 99, "quarantine is still excluded"
    assert audit_rows(db_session, BACKFLUSH_SHORTAGE_AUDIT_ACTION) == [], "no false shortage against visible stock"


def test_tie_engine_null_status_lot_is_consumable(db_session: Session):
    """The same widening on the LIVE operation-scoped leg — the one shipped path it touches.

    Before PR 4.4 this query filtered ``status = 'available'`` literally, so a NULL-status
    lot was invisible to it: the engine minted a lot-less placeholder row, recorded a
    false shortage, and produced a row PR 3's RETURN engine refuses to credit back. That
    is why query 5 of the release gate exists.
    """
    user = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    legacy = make_lot(db_session, sheet, qty=10, lot="TIE-NULL", received_date=days(2))
    legacy.status = None
    db_session.commit()

    wo = make_wo(db_session, fg, quantity_ordered=3, quantity_complete=3)
    op = make_op(db_session, wo, make_work_center(db_session), quantity_complete=3)
    tie(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=3)

    apply_operation_completion_inventory_effects(
        db_session, wo, op, user_id=user.id, company_id=COMPANY_A, audit=AuditService(db_session, user)
    )
    db_session.commit()
    db_session.expire_all()

    rows = (
        db_session.query(InventoryTransaction)
        .filter(
            InventoryTransaction.company_id == COMPANY_A,
            InventoryTransaction.reference_type == OPERATION_REFERENCE_TYPE,
            InventoryTransaction.reference_id == op.id,
        )
        .all()
    )
    assert [(r.lot_number, r.quantity) for r in rows] == [("TIE-NULL", -3.0)], "the legacy lot IS drawn"
    assert db_session.get(InventoryItem, legacy.id).quantity_on_hand == 7
    assert (
        db_session.query(InventoryTransaction)
        .filter(InventoryTransaction.company_id == COMPANY_A, InventoryTransaction.lot_number.is_(None))
        .count()
        == 0
    ), "no lot-less placeholder row: the engine can see the stock now"


@pytest.mark.parametrize("status_value", [None, "available", "on_hold", "quarantine", "rejected"])
@pytest.mark.parametrize("active_value", [True, False, None])
def test_consumable_predicate_matches_is_consumable_item(db_session: Session, status_value, active_value):
    """PARITY: the SQL half and the Python half of one policy must agree, cell by cell.

    ``CONSUMABLE_ITEM_CLAUSES`` gates what the engine draws; ``is_consumable_item`` gates
    what the tie endpoint refuses to PIN (a 422 at tie time, where a human is present).
    A disagreement means a pin is accepted that consumption then skips, or refused for a
    lot consumption would happily take. The ``is_active IS NULL`` cells are the subtle
    ones: SQL ``is_active = TRUE`` is NULL (not FALSE) on such a row and therefore does
    not match, while ``bool(None)`` is False — the two agree, but only by argument, so
    they are asserted.
    """
    part = make_part(db_session, part_type="purchased")
    item = make_lot(db_session, part, qty=5, lot=f"PARITY-{_next()}", received_date=days(1))
    item.status = status_value
    item.is_active = active_value
    db_session.commit()
    db_session.expire_all()

    item = db_session.get(InventoryItem, item.id)
    sql_says = [row.id for row in consumable_source_items(db_session, part.id, COMPANY_A)]
    python_says = [item.id] if is_consumable_item(item) else []

    assert (
        sql_says == python_says
    ), f"status={status_value!r} is_active={active_value!r}: SQL said {sql_says}, Python said {python_says}"


def test_held_stock_summary_is_the_exact_complement_of_the_consumable_predicate(db_session: Session):
    """Every positive-on-hand lot is either DRAWN or DISCLOSED — never neither.

    The disclosure query is written as an explicit NULL-safe negation rather than
    ``~and_(*CONSUMABLE_ITEM_CLAUSES)``, and this is why. SQL three-valued logic makes the
    bare negation wrong: on a row with ``is_active IS NULL``, ``is_active = TRUE``
    evaluates to NULL, ``NOT NULL`` is also NULL, and the row falls out of BOTH sets —
    vanishing from the very disclosure the helper exists to write, on exactly the legacy
    shape (no ``server_default``) the widening was added for.
    """
    part = make_part(db_session, part_type="purchased")
    lots = {}
    for status_value in (None, "available", "on_hold", "quarantine", "rejected"):
        for active_value in (True, False, None):
            item = make_lot(db_session, part, qty=1, lot=f"CMPL-{_next()}", received_date=days(1))
            item.status = status_value
            item.is_active = active_value
            lots[item.lot_number] = item
    db_session.commit()
    db_session.expire_all()

    drawn = {row.lot_number for row in consumable_source_items(db_session, part.id, COMPANY_A)}
    held_quantity, held_lots = held_stock_summary(db_session, part.id, COMPANY_A)

    assert drawn & set(held_lots) == set(), "a lot cannot be both drawn and disclosed"
    assert drawn | set(held_lots) == set(lots), "and none may fall through the crack between them"
    assert held_quantity == float(len(held_lots)), "each disclosed lot holds 1"
    assert drawn, "sanity: some lots must be consumable, or the partition is trivially satisfied"
    assert held_lots, "sanity: some lots must be held"


def test_material_tie_view_on_hand_matches_what_the_engine_draws(db_session: Session):
    """The board's number and the engine's draw come from ONE predicate, not two copies.

    ``material_tie_view`` used to re-declare the FIFO predicate verbatim, with a comment
    arguing that excluding NULL-status lots was "kept, not fixed" because FIFO would not
    draw from such a lot either. That rationale inverted the moment FIFO did. It now
    splats ``CONSUMABLE_ITEM_CLAUSES`` in, so the on-hand hint a manager sees on the
    dispatch chip and an operator sees on the kiosk line cannot promise stock the engine
    refuses, nor hide stock it will take.
    """
    user = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    legacy = make_lot(db_session, sheet, qty=12, lot="VIEW-NULL", received_date=days(4))
    legacy.status = None
    make_lot(db_session, sheet, qty=99, lot="VIEW-HELD", received_date=days(1), status_="on_hold")
    db_session.commit()

    wo = make_wo(db_session, fg, quantity_ordered=12, quantity_complete=12)
    op = make_op(db_session, wo, make_work_center(db_session), quantity_complete=12)
    tie(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=12)

    [view] = tie_views_for_operations(db_session, company_id=COMPANY_A, operation_ids=[op.id])[op.id]
    assert view.on_hand == 12.0, "the NULL-status lot counts; the held one does not"
    assert view.short_by == 0.0

    apply_operation_completion_inventory_effects(
        db_session, wo, op, user_id=user.id, company_id=COMPANY_A, audit=AuditService(db_session, user)
    )
    db_session.commit()
    db_session.expire_all()

    drawn = -sum(
        float(r.quantity or 0)
        for r in db_session.query(InventoryTransaction).filter(
            InventoryTransaction.company_id == COMPANY_A,
            InventoryTransaction.reference_type == OPERATION_REFERENCE_TYPE,
            InventoryTransaction.reference_id == op.id,
        )
    )
    assert drawn == 12.0, "the engine took exactly what the view promised"
    assert db_session.get(InventoryItem, legacy.id).quantity_on_hand == 0


def test_multi_lot_shortfall_now_records_shortage(db_session: Session):
    """**THE RECORDED DEFECT.** A multi-lot part could go deeply negative in silence.

    The old leg drew from ONE lot but computed ``shortfall = required − available_total``
    across ALL lots, so with 10 in each of two lots and a demand of 50 it wrote a single
    −50 row against one lot (driving it to −40) and then computed ``50 − 20 = 30``…
    against lots it never touched. Worse, for the case where the total DID cover the
    demand across lots the shortfall went non-positive and the part went deeply negative
    with NO ``ComponentShortage``, NO ``BACKFLUSH_SHORTAGE`` chain row and NO event.

    ``available_total`` is now summed over the lots actually walked and ``shortfall`` is
    the planner's own remainder, so that is structurally impossible.
    """
    user = make_user(db_session)
    component = make_part(db_session, part_type="purchased")
    first = make_lot(db_session, component, qty=10, lot="SHORT-A", received_date=days(1))
    second = make_lot(db_session, component, qty=10, lot="SHORT-B", received_date=days(2))

    wo = backflush_job(db_session, component=component, qty_per_unit=10.0, quantity_complete=5.0)
    run_effects(db_session, wo, user)
    db_session.expire_all()

    rows = issue_rows(db_session, wo, component)
    assert [(r.lot_number, r.quantity) for r in rows] == [
        ("SHORT-A", -10.0),
        ("SHORT-B", -10.0),
        ("SHORT-B", -30.0),
    ], "both lots are exhausted first; only the remainder drives the anchor negative"
    assert sum(r.quantity for r in rows) == -50.0, "the full demand is still on the ledger"
    assert db_session.get(InventoryItem, first.id).quantity_on_hand == 0
    assert db_session.get(InventoryItem, second.id).quantity_on_hand == -30

    shortage = audit_rows(db_session, BACKFLUSH_SHORTAGE_AUDIT_ACTION)
    assert len(shortage) == 1, "exactly one BACKFLUSH_SHORTAGE chain row"
    extra = shortage[0].extra_data or {}
    assert (extra["required_quantity"], extra["available_quantity"], extra["shortfall"]) == (50.0, 20.0, 30.0)
    assert extra["held_quantity_skipped"] == 0.0, "nothing was skipped, so nothing is disclosed"

    events = (
        db_session.query(OperationalEvent)
        .filter(
            OperationalEvent.company_id == COMPANY_A,
            OperationalEvent.event_type == BACKFLUSH_SHORTAGE_EVENT_TYPE,
        )
        .all()
    )
    assert len(events) == 1 and events[0].severity == "warning"


def test_pinned_backflush_draw_stays_on_its_lot_and_splits_when_short(db_session: Session):
    """A PIN bypasses the ORDERING, never the lot. Row SHAPE changed; the lot did not.

    Pinning is a lot-directed instruction, so an insufficient pinned lot is driven
    NEGATIVE rather than spilling onto a different, uncertified, wrong-heat lot. What
    changed is that an under-covered pinned draw now posts a ``take`` row plus a separate
    ``(SHORT n)`` row against the SAME lot, exactly as the per-run engine does, instead of
    one row for the whole quantity. Same lot, same total, at most two rows — and
    ``_plan_material_return`` walks N rows natively.
    """
    user = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    # The FIFO head, which an unpinned draw would have taken first.
    fifo_head = make_lot(db_session, sheet, qty=100, lot="PIN-FIFO-HEAD", received_date=days(1))
    pinned_lot = make_lot(db_session, sheet, qty=6, lot="PIN-HEAT-CERT", received_date=days(50))

    wo = make_wo(db_session, fg, quantity_ordered=10, quantity_complete=10)
    make_op(db_session, wo, make_work_center(db_session), quantity_complete=10)
    allocation = tie(db_session, wo, sheet, operation=None, qty_planned=10.0, pinned=pinned_lot)

    run_effects(db_session, wo, user)
    db_session.expire_all()

    rows = issue_rows(db_session, wo, sheet)
    assert [(r.lot_number, r.quantity) for r in rows] == [
        ("PIN-HEAT-CERT", -6.0),
        ("PIN-HEAT-CERT", -4.0),
    ], "both rows name the pinned lot — no spill onto another heat"
    assert sum(r.quantity for r in rows) == -10.0
    assert {r.allocation_id for r in rows} == {allocation.id}
    assert db_session.get(InventoryItem, pinned_lot.id).quantity_on_hand == -4
    assert db_session.get(InventoryItem, fifo_head.id).quantity_on_hand == 100, "the FIFO head is untouched"


def test_pinned_lot_held_after_pinning_still_consumes_and_is_audited(db_session: Session):
    """The PINNED leg keeps its 8.7 row; only the unpinned one stopped drawing held stock.

    The tie endpoint refuses to pin a non-available lot (422), so this row can only ever
    mean "held AFTER it was pinned" — and consumption still proceeds, because the sheet
    was physically cut and refusing from a reconcile-on-read GET would be unattributable.
    """
    user = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    pinned_lot = make_lot(db_session, sheet, qty=10, lot="PIN-THEN-HELD", received_date=days(1))

    wo = make_wo(db_session, fg, quantity_ordered=4, quantity_complete=4)
    make_op(db_session, wo, make_work_center(db_session), quantity_complete=4)
    allocation = tie(db_session, wo, sheet, operation=None, qty_planned=4.0, pinned=pinned_lot)

    pinned_lot.status = "quarantine"  # held AFTER pinning
    db_session.commit()

    run_effects(db_session, wo, user)
    db_session.expire_all()

    assert [(r.lot_number, r.quantity) for r in issue_rows(db_session, wo, sheet)] == [("PIN-THEN-HELD", -4.0)]
    held = audit_rows(db_session, HELD_MATERIAL_CONSUMED_AUDIT_ACTION)
    assert len(held) == 1
    assert (held[0].extra_data or {})["allocation_id"] == allocation.id
    assert (held[0].extra_data or {})["item_status"] == "quarantine"
    assert (held[0].extra_data or {})["pin_directed"] is True, "only the pinned branch can reach this row now"


# ===========================================================================
# 2. GOAL (b) -- reconcile-to-target arithmetic
# ===========================================================================


def test_backflush_posts_under_the_new_reference_shape(db_session: Session):
    """Every component row is ``work_order_backflush``; the FG receipt keeps ``work_order``.

    That split is the whole mechanism: the component leg moves OUT of the
    ``uq_wo_inventory_issue`` predicate (so N rows plus a later top-up row are legal)
    while ``uq_wo_inventory_receipt`` keeps backing the single FG RECEIVE it genuinely
    covers. Nothing writes a component ISSUE under the legacy shape any more, which is
    what turns ``_component_already_issued`` into a permanent fence over pre-4.4 rows.
    """
    user = make_user(db_session)
    component = make_part(db_session, part_type="purchased")
    make_lot(db_session, component, qty=100, lot="SHAPE-A", received_date=days(1))

    wo = backflush_job(db_session, component=component, qty_per_unit=2.0, quantity_complete=5.0)
    run_effects(db_session, wo, user)
    db_session.expire_all()

    rows = issue_rows(db_session, wo, component)
    assert [(r.reference_type, r.reference_id, r.quantity) for r in rows] == [(BACKFLUSH_REFERENCE_TYPE, wo.id, -10.0)]
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
    ), "ZERO ('work_order', ISSUE) rows are written"

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
    assert len(receipts) == 1, "uq_wo_inventory_receipt still backs exactly one FG receipt"


def test_second_pass_with_a_raised_target_posts_only_the_increment(db_session: Session):
    """Composability: a risen target tops up by the DELTA, not by the whole target again.

    Driven by calling ``backflush_components_for_work_order`` directly, because **no
    endpoint triggers this today** — every operation-completion handler refuses a terminal
    parent, ``complete_work_order`` early-returns for COMPLETE/CLOSED, reconcile-on-read
    strips terminal work orders, and terminal → non-terminal is blocked. PR 4.4 makes the
    arithmetic convergent, which is the PRECONDITION for ever adding a re-entry trigger
    safely; it deliberately does not add one. This test is what proves the precondition
    holds before anything relies on it.
    """
    user = make_user(db_session)
    component = make_part(db_session, part_type="purchased")
    lot = make_lot(db_session, component, qty=100, lot="TOPUP-A", received_date=days(1))
    wo = backflush_job(db_session, component=component, qty_per_unit=1.0, quantity_complete=4.0, quantity_ordered=10)

    audit = AuditService(db_session, user)
    backflush_components_for_work_order(db_session, wo, user_id=user.id, company_id=COMPANY_A, audit=audit)
    db_session.commit()
    db_session.expire_all()
    assert [r.quantity for r in issue_rows(db_session, wo, component)] == [-4.0]

    wo = db_session.get(WorkOrder, wo.id)
    wo.quantity_complete = 7
    db_session.commit()

    backflush_components_for_work_order(
        db_session, wo, user_id=user.id, company_id=COMPANY_A, audit=AuditService(db_session, user)
    )
    db_session.commit()
    db_session.expire_all()

    rows = issue_rows(db_session, wo, component)
    assert [r.quantity for r in rows] == [-4.0, -3.0], "exactly the increment, never the whole target again"
    assert sum(r.quantity for r in rows) == -7.0
    assert db_session.get(InventoryItem, lot.id).quantity_on_hand == 93


def test_replay_posts_nothing_and_writes_no_audit_row(db_session: Session):
    """A converged replay is a SILENT no-op — not a suppression, and not a cache write.

    Three channels, because a replay that is quiet on one and noisy on another is still a
    defect: no ledger row, no ``BACKFLUSH_DOUBLE_ISSUE_BLOCKED`` row (a converged delta is
    not a blocked demand), and no second ``log_update`` on the tie (``log_update``
    self-suppresses on an unchanged value, and ``qty_consumed`` is written from the ledger
    net, so it does not move).
    """
    user = make_user(db_session)
    component = make_part(db_session, part_type="purchased")
    make_lot(db_session, component, qty=100, lot="REPLAY-A", received_date=days(1))
    wo = backflush_job(db_session, component=component, qty_per_unit=2.0, quantity_complete=5.0)
    allocation = tie(db_session, wo, component, operation=None, qty_planned=3.0)

    run_effects(db_session, wo, user)
    db_session.expire_all()
    first_pass = [(r.quantity, r.allocation_id) for r in issue_rows(db_session, wo, component)]
    assert sorted(q for q, _ in first_pass) == [-10.0, -3.0], "BOM 10 + tie 3, as two rows"

    def tie_update_rows() -> int:
        return (
            db_session.query(AuditLog)
            .filter(
                AuditLog.company_id == COMPANY_A,
                AuditLog.resource_type == "work_order_material_allocation",
                AuditLog.resource_id == allocation.id,
                AuditLog.action == AuditService.ACTIONS["UPDATE"],
            )
            .count()
        )

    assert tie_update_rows() == 1
    blocked_before = len(audit_rows(db_session, BACKFLUSH_DOUBLE_ISSUE_BLOCKED_AUDIT_ACTION))

    run_effects(db_session, db_session.get(WorkOrder, wo.id), user)
    db_session.expire_all()

    assert [(r.quantity, r.allocation_id) for r in issue_rows(db_session, wo, component)] == first_pass
    assert len(audit_rows(db_session, BACKFLUSH_DOUBLE_ISSUE_BLOCKED_AUDIT_ACTION)) == blocked_before == 0
    assert tie_update_rows() == 1, "a replay that changed nothing must not append a chain row"


def test_reduced_target_is_a_noop_never_a_reversal(db_session: Session):
    """INVARIANT 6(b): a negative delta is a silent no-op, NEVER an auto-reversal.

    This path runs from a reconcile-on-read GET, where there is no actor, no intent and
    no reason — the three things a compensating movement must carry. Un-consuming stays
    PR 3's reasoned, audited ``return_tied_material``. A leg that "helpfully" credited
    material back here would be moving regulated stock on a poll.
    """
    user = make_user(db_session)
    component = make_part(db_session, part_type="purchased")
    lot = make_lot(db_session, component, qty=100, lot="REDUCE-A", received_date=days(1))
    wo = backflush_job(db_session, component=component, qty_per_unit=1.0, quantity_complete=8.0, quantity_ordered=10)

    backflush_components_for_work_order(
        db_session, wo, user_id=user.id, company_id=COMPANY_A, audit=AuditService(db_session, user)
    )
    db_session.commit()
    db_session.expire_all()
    assert [r.quantity for r in issue_rows(db_session, wo, component)] == [-8.0]

    wo = db_session.get(WorkOrder, wo.id)
    wo.quantity_complete = 3  # the basis falls -> delta is negative
    db_session.commit()

    backflush_components_for_work_order(
        db_session, wo, user_id=user.id, company_id=COMPANY_A, audit=AuditService(db_session, user)
    )
    db_session.commit()
    db_session.expire_all()

    assert [r.quantity for r in issue_rows(db_session, wo, component)] == [-8.0], "nothing added"
    assert (
        db_session.query(InventoryTransaction)
        .filter(
            InventoryTransaction.company_id == COMPANY_A,
            InventoryTransaction.transaction_type == TransactionType.RETURN,
        )
        .count()
        == 0
    ), "and above all NOTHING returned"
    assert db_session.get(InventoryItem, lot.id).quantity_on_hand == 92


def test_tie_qty_consumed_equals_its_ledger_net(db_session: Session):
    """RESIDUAL 4 closed: the cache is a RECORD of the ledger, not a claim about the plan.

    Its predecessor set ``qty_consumed = qty_planned`` regardless of what actually posted.
    Two verbs key on the value exactly — ``return_and_untie`` gives back precisely
    ``qty_consumed``, and ``correct_over_consumption``'s allowance is
    ``qty_consumed − target`` — so a cache that over-claimed would authorize a credit the
    ledger cannot support. Asserted where the two genuinely differ: a tie that could not
    be fully covered still posts its full demand (a driven-negative shortage row), so cache
    and net agree on the DEMAND, not on the plan.
    """
    user = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    make_lot(db_session, sheet, qty=4, lot="NET-A", received_date=days(1))

    wo = make_wo(db_session, fg, quantity_ordered=9, quantity_complete=9)
    make_op(db_session, wo, make_work_center(db_session), quantity_complete=9)
    allocation = tie(db_session, wo, sheet, operation=None, qty_planned=9.0)

    run_effects(db_session, wo, user)
    db_session.expire_all()

    net = net_consumed_quantity_for_allocation(db_session, allocation_id=allocation.id, company_id=COMPANY_A)
    assert net == 9.0
    assert db_session.get(WorkOrderMaterialAllocation, allocation.id).qty_consumed == net
    assert sum(r.quantity for r in issue_rows(db_session, wo, sheet)) == -9.0


def test_return_and_untie_zeroes_a_spilled_work_order_scoped_tie(client: TestClient, db_session: Session):
    """A tie that spilled across three lots returns PER LOT and cancels.

    Spill is what makes this worth asserting: PR 3's planner walks the compensated ISSUE
    rows one at a time and credits each to its OWN source lot at that row's OWN unit
    cost, so three ISSUE rows produce three RETURN rows and every lot ends exactly where
    it started. A planner that credited one lump to the head lot would balance the totals
    and corrupt three genealogies.

    The credits come back NEWEST-FIRST — the reverse of the FIFO order they were drawn
    in — which is deliberate and is asserted rather than sorted away: an over-count
    correction compensates the most recent draw, and that same LIFO order is what makes
    the residual-capacity cap idempotent across replays.
    """
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    lots = [
        make_lot(db_session, sheet, qty=4, lot="RET-C", received_date=days(30), unit_cost=9.0),
        make_lot(db_session, sheet, qty=4, lot="RET-A", received_date=days(10), unit_cost=7.0),
        make_lot(db_session, sheet, qty=4, lot="RET-B", received_date=days(20), unit_cost=8.0),
    ]

    wo = make_wo(db_session, fg, quantity_ordered=10, quantity_complete=10)
    make_op(db_session, wo, make_work_center(db_session), quantity_complete=10)
    allocation = tie(db_session, wo, sheet, operation=None, qty_planned=10.0)

    run_effects(db_session, wo, supervisor)
    db_session.expire_all()
    assert [(r.lot_number, r.quantity) for r in issue_rows(db_session, wo, sheet)] == [
        ("RET-A", -4.0),
        ("RET-B", -4.0),
        ("RET-C", -2.0),
    ]

    resp = client.post(
        f"/api/v1/work-orders/{wo.id}/material-allocations/{allocation.id}/return",
        headers=headers_for(supervisor),
        json={"quantity": 10, "intent": "return_and_untie", "reason": "job cancelled before cutting"},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    db_session.expire_all()

    returns = [r for r in backflush_rows(db_session, wo, sheet) if r.transaction_type == TransactionType.RETURN]
    assert [(r.lot_number, r.quantity, r.unit_cost) for r in returns] == [
        ("RET-C", 2.0, 9.0),
        ("RET-B", 4.0, 8.0),
        ("RET-A", 4.0, 7.0),
    ], "one credit per compensated row, newest-first, at that row's own cost"
    assert all(r.reference_type == BACKFLUSH_REFERENCE_TYPE for r in returns), "the RETURN mirrors the ISSUE's shape"
    for item in lots:
        assert db_session.get(InventoryItem, item.id).quantity_on_hand == 4, "every lot back where it started"
    allocation = db_session.get(WorkOrderMaterialAllocation, allocation.id)
    assert allocation.qty_consumed == 0.0
    assert allocation.status == AllocationStatus.CANCELLED


def test_legacy_work_order_shape_row_fences_both_legs(db_session: Session):
    """**THE FENCE.** A pre-4.4 SUMMED row keeps a work order out of the new engine entirely.

    This is what makes PR 4.4 correct-forward with NO backfill: no historical ledger row
    is rewritten, re-keyed or reinterpreted, and legacy work orders keep exactly the
    behaviour they have. The seeded row is the genuinely dangerous historical shape — a
    SUMMED ISSUE carrying the FIRST tie's ``allocation_id`` over a quantity that also
    covered BOM demand.

    **The tie the fence actually protects is the one whose ``allocation_id`` is NOT on
    that row**, and the fixture is built to be exactly that. ``ALPHA`` is the tie the
    summed row names; it has since been untied (``CANCELLED``, the reachable shape — one
    OPEN work-order-scoped tie per part is unique-indexed, so a *second* one can only
    exist after an untie-then-re-tie). ``BRAVO`` is the live re-tie: its own signed net is
    **zero**, because the summed row names ALPHA, so ``delta = qty_planned > 0`` and the
    arithmetic alone would happily re-issue 7 that the summed row already took. Only the
    fence stops it, and it must RECORD doing so — an as-built review cannot reconstruct a
    decision the system never wrote down.

    (ALPHA is ``CANCELLED``, so it never reaches leg 2 at all and this test asserts only
    its NET -- 17, because ``net_consumed_quantity_for_allocation`` is ``allocation_id``-keyed
    and SHAPE-AGNOSTIC -- as an anti-decay guard on the fixture's premise. The behaviour
    that a still-OPEN but CONVERGED tie no-ops silently *before* the fence, writing no
    suppression row, is a different case and is pinned separately by
    ``test_a_converged_leg_2_tie_writes_no_suppression_row_even_though_it_is_fenced``,
    which asserts the fence is ARMED before asserting the absence of the row.)
    """
    user = make_user(db_session)
    component = make_part(db_session, part_type="purchased")
    lot = make_lot(db_session, component, qty=100, lot="LEGACY-SUMMED", received_date=days(1))

    wo = backflush_job(db_session, component=component, qty_per_unit=2.0, quantity_complete=5.0)
    alpha = tie(db_session, wo, component, operation=None, qty_planned=7.0, status_=AllocationStatus.CANCELLED)

    # The historical summed row: BOM 10 + tie 7, one row, ALPHA's allocation_id.
    db_session.add(
        InventoryTransaction(
            company_id=COMPANY_A,
            inventory_item_id=lot.id,
            part_id=component.id,
            transaction_type=TransactionType.ISSUE,
            quantity=-17.0,
            from_location=lot.location,
            lot_number=lot.lot_number,
            reference_type=WORK_ORDER_REFERENCE_TYPE,
            reference_id=wo.id,
            reference_number=wo.work_order_number,
            allocation_id=alpha.id,
            unit_cost=2.0,
            total_cost=34.0,
            created_by=user.id,
        )
    )
    lot.quantity_on_hand = 83.0
    db_session.commit()

    # The re-tie: same part, same work order, OPEN, and holding no ledger rows of its own.
    bravo = tie(db_session, wo, component, operation=None, qty_planned=7.0)
    assert (
        net_consumed_quantity_for_allocation(db_session, allocation_id=bravo.id, company_id=COMPANY_A) == 0.0
    ), "BRAVO's net MUST read zero, or the fence is not the thing being tested"
    assert (
        net_consumed_quantity_for_allocation(db_session, allocation_id=alpha.id, company_id=COMPANY_A) == 17.0
    ), "…while ALPHA's reads the summed row, shape-agnostically"

    run_effects(db_session, wo, user)
    db_session.expire_all()

    assert backflush_rows(db_session, wo) == [], "ZERO new ledger rows on a fenced work order"
    assert db_session.get(InventoryItem, lot.id).quantity_on_hand == 83.0, "and no second draw"
    assert db_session.get(WorkOrderMaterialAllocation, bravo.id).qty_consumed == 0.0, "the cache is not advanced"

    blocked = audit_rows(db_session, BACKFLUSH_DOUBLE_ISSUE_BLOCKED_AUDIT_ACTION)
    assert len(blocked) == 2, "BOTH legs record their suppression — the tie leg and the BOM leg"
    assert {(r.extra_data or {}).get("suppression_reason") for r in blocked} == {"already_issued"}
    assert {(r.extra_data or {}).get("component_part_id") for r in blocked} == {component.id}
    assert sorted((r.extra_data or {}).get("suppressed_quantity") for r in blocked) == [
        7.0,
        10.0,
    ], "each leg records the quantity IT was dropping, not a merged total"
    by_quantity = {(r.extra_data or {}).get("suppressed_quantity"): (r.extra_data or {}) for r in blocked}
    assert by_quantity[7.0]["ledger_net_issued"] == 0.0, "leg 2 carries the net its delta was computed from"
    assert by_quantity[10.0]["ledger_net_issued"] is None, "leg 1 reads no net on the fenced path"


def test_backflush_result_reports_shortages_from_both_legs_but_part_ids_only_from_the_bom(
    db_session: Session,
):
    """The returned ``BackflushResult`` shape, pinned because PR 4.4 NARROWED it.

    ``issued_part_ids`` used to receive a part whenever the loop reached
    ``_issue_one_component`` — which, under the summed one-row model, covered tie-driven
    demand too. Leg 2 no longer appends to it, so it now means "parts the BOM/routing leg
    posted for" and nothing else. That narrowing has NO production consumer today (both
    call sites explicitly discard the result — ``shop_floor.py:411``,
    ``work_orders.py:345``, each with a comment saying so), which is exactly why it is
    worth an assertion: an unread field is where a silent contract change hides until the
    first reader arrives.

    ``shortages`` is deliberately NOT narrowed — a shortage on either leg is a
    material-trail fact and both are reported — and both are ALSO recorded
    tamper-evidently inside ``_issue_one_component``, so the caller never has to inspect
    this object to stay compliant.
    """
    user = make_user(db_session)
    bom_part = make_part(db_session, part_type="purchased")
    tied_part = make_part(db_session, part_type="purchased")
    make_lot(db_session, bom_part, qty=1, lot="RESULT-BOM", received_date=days(1))  # short
    make_lot(db_session, tied_part, qty=1, lot="RESULT-TIE", received_date=days(1))  # short

    fg = make_part(db_session, backflush=True)
    add_bom_item(db_session, make_bom(db_session, fg), bom_part, quantity=2.0)
    wo = make_wo(db_session, fg, quantity_ordered=5, quantity_complete=5)
    make_op(db_session, wo, make_work_center(db_session), quantity_complete=5)
    tie(db_session, wo, tied_part, operation=None, qty_planned=6.0)

    result = backflush_components_for_work_order(
        db_session, wo, user_id=user.id, company_id=COMPANY_A, audit=AuditService(db_session, user)
    )
    db_session.commit()

    assert sorted(s.part_id for s in result.shortages) == sorted(
        [bom_part.id, tied_part.id]
    ), "a shortage on EITHER leg is reported"
    assert result.issued_part_ids == [bom_part.id], "…but issued_part_ids is the BOM leg's alone"
    assert (
        len(audit_rows(db_session, BACKFLUSH_SHORTAGE_AUDIT_ACTION)) == 2
    ), "and both are on the hash chain regardless of what the caller does with the result"


def test_backflush_net_excludes_tie_rows_and_vice_versa(db_session: Session):
    """``allocation_id IS NULL`` partitions the two nets EXACTLY, with nothing shared.

    If the BOM net could see the tie's rows it would under-issue by the tie's quantity
    forever; if the tie's net could see the BOM's it would consider itself already drawn.
    The partition holds because every tie-driven row carries an ``allocation_id`` (both
    the ISSUE and the RETURN writer stamp or copy it, and migration 075 shipped the column
    in the same commit as the tie engine, so no tie-driven row can predate it).
    """
    user = make_user(db_session)
    component = make_part(db_session, part_type="purchased")
    make_lot(db_session, component, qty=100, lot="PARTITION-A", received_date=days(1))

    wo = backflush_job(db_session, component=component, qty_per_unit=2.0, quantity_complete=5.0)
    allocation = tie(db_session, wo, component, operation=None, qty_planned=7.0)

    run_effects(db_session, wo, user)
    db_session.expire_all()

    bom_net = backflush_net_issued_by_part(
        db_session, work_order_id=wo.id, company_id=COMPANY_A, part_ids=[component.id]
    )
    tie_net = net_consumed_quantity_for_allocation(db_session, allocation_id=allocation.id, company_id=COMPANY_A)

    assert bom_net == {component.id: 10.0}, "the BOM's net sees only its own rows"
    assert tie_net == 7.0, "and the tie's net only its own"
    assert bom_net[component.id] + tie_net == 17.0, "together they account for every unit that left stock"
    assert on_hand(db_session, component) == 83.0


def test_ledger_suppression_layer_still_reads_only_the_operation_shape(db_session: Session):
    """``_drop_ledger_covered_parts`` MUST NOT learn the backflush shape, or the leg
    silences ITSELF.

    That layer reads a positive net as "a tie already drew this part" and suppresses the
    BOM's demand, writing a ``BACKFLUSH_DOUBLE_ISSUE_BLOCKED`` row. If it also matched
    ``work_order_backflush`` then the BOM leg's OWN first post would suppress every later
    pass and emit a spurious blocked row while doing it. Its convergence is handled one
    layer down, arithmetically, by ``backflush_net_issued_by_part``.
    """
    user = make_user(db_session)
    component = make_part(db_session, part_type="purchased")
    make_lot(db_session, component, qty=100, lot="SUPPRESS-A", received_date=days(1))
    wo = backflush_job(db_session, component=component, qty_per_unit=2.0, quantity_complete=5.0)

    run_effects(db_session, wo, user)
    db_session.expire_all()

    assert sum(r.quantity for r in issue_rows(db_session, wo, component)) == -10.0
    assert (
        operation_scoped_net_issued_by_part(
            db_session, work_order_id=wo.id, company_id=COMPANY_A, part_ids=[component.id]
        )
        == {}
    ), "the operation-scoped net must not see a single work_order_backflush row"

    run_effects(db_session, db_session.get(WorkOrder, wo.id), user)
    db_session.expire_all()
    assert (
        audit_rows(db_session, BACKFLUSH_DOUBLE_ISSUE_BLOCKED_AUDIT_ACTION) == []
    ), "no spurious blocked row: the leg must not read its own output as somebody else's draw"


# ===========================================================================
# 3. Concurrency and failure posture
# ===========================================================================


def test_every_completion_handler_locks_the_work_order_before_the_seam():
    """STRUCTURAL, because pytest cannot execute it: ``with_for_update`` is a SQLite no-op.

    The new shape is covered by no unique index, so its idempotency is arithmetic — and
    the arithmetic is only valid because the writes are SERIALIZED. On the four write
    paths that serialization is a real ``SELECT ... FOR UPDATE`` on the ``work_orders``
    row, taken BEFORE the inventory seam. SQLite ignores the clause entirely, so a
    behavioural test here would pass whether or not the lock existed. Asserting the source
    is the honest alternative: it cannot prove the lock WORKS, but it fails loudly if a
    refactor removes it or moves the seam above it, which is the regression that matters.

    (The two reconcile-on-read entries take no lock and do not need one — they always
    UPDATE the ``version_id_col``-mapped ``work_orders`` row, so a loser raises
    ``StaleDataError`` and its whole reconcile rolls back.)
    """
    from app.api.endpoints import shop_floor, work_orders

    handlers = [
        (shop_floor, "clock_out"),
        (shop_floor, "complete_operation"),
        (work_orders, "complete_operation"),
        (work_orders, "complete_work_order"),
    ]
    for module, name in handlers:
        source = inspect.getsource(getattr(module, name))
        locked = re.search(r"db\.query\(WorkOrder\)[\s\S]{0,800}?with_for_update\(\)", source)
        assert locked, f"{module.__name__}.{name} no longer row-locks the work order"
        seam = source.find("apply_completion_inventory_effects")
        assert seam != -1, f"{module.__name__}.{name} no longer reaches the completion inventory seam"
        assert locked.start() < seam, f"{module.__name__}.{name} takes the work-order lock AFTER the seam"


def test_convergence_comes_from_the_arithmetic_not_from_the_legacy_fence(db_session: Session, monkeypatch):
    """Two overlapping passes converge with the legacy fence MONKEYPATCHED AWAY.

    The old engine's protection was existence: ``_component_already_issued`` plus
    ``uq_wo_inventory_issue`` behind it. Neither applies now — the fence matches only
    pre-4.4 rows and no index covers the new shape — so the double-issue guard has to be
    the delta, and nothing else. Stubbing the fence to always miss is what proves the
    delta is carrying the load rather than riding along behind a guard that would have
    caught it anyway.
    """
    user = make_user(db_session)
    component = make_part(db_session, part_type="purchased")
    lot = make_lot(db_session, component, qty=100, lot="CONVERGE-A", received_date=days(1))
    wo = backflush_job(db_session, component=component, qty_per_unit=3.0, quantity_complete=5.0)
    allocation = tie(db_session, wo, component, operation=None, qty_planned=4.0)

    monkeypatch.setattr(cis, "_component_already_issued", lambda *args, **kwargs: False)

    audit = AuditService(db_session, user)
    for _ in range(2):
        backflush_components_for_work_order(db_session, wo, user_id=user.id, company_id=COMPANY_A, audit=audit)
        db_session.flush()
    db_session.commit()
    db_session.expire_all()

    rows = issue_rows(db_session, wo, component)
    assert sorted(r.quantity for r in rows) == [-15.0, -4.0], "BOM 15 + tie 4, posted exactly ONCE each"
    assert sum(r.quantity for r in rows) == -19.0
    assert db_session.get(InventoryItem, lot.id).quantity_on_hand == 81.0
    assert db_session.get(WorkOrderMaterialAllocation, allocation.id).qty_consumed == 4.0


def _force_duplicate_backflush_issue_index(db: Session) -> None:
    """Manufacture a REAL unique-index violation on the second spilled ISSUE row.

    There is deliberately no index over the ``work_order_backflush`` shape, so a genuine
    ``IntegrityError`` cannot arise from the schema as shipped — and SQLite does not
    enforce foreign keys either, so the usual cheap trigger is unavailable too. Rather
    than monkeypatching the very seam under test, this creates a one-row-per-(company, WO,
    part) unique index for the duration of one test: a draw that SPILLS then violates it
    on its second row, from inside the engine, exactly as an FK / NOT NULL /
    ``chk_inventory_items_quantity_non_negative`` violation would in production.

    Safe within a test: the ``db_session`` fixture drop_all/create_all's the whole schema
    per test, so nothing leaks to the next one.
    """
    db.commit()
    db.execute(
        text(
            "CREATE UNIQUE INDEX tmp_uq_backflush_issue ON inventory_transactions "
            "(company_id, reference_id, part_id) "
            "WHERE reference_type = 'work_order_backflush' AND transaction_type = 'ISSUE'"
        )
    )
    db.commit()


def test_a_real_integrity_error_is_recorded_not_swallowed(db_session: Session):
    """``duplicate_is_noop=False``, end to end: a real fault is RECORDED, never a shortage.

    ``_post_stock_movement_txn(duplicate_is_noop=True)`` swallows EVERY ``IntegrityError``
    and returns ``None``, which the caller then reads as "a concurrent completion already
    wrote this row". That was only ever correct while a partial unique index backed the
    row. Under the new shape it would turn a genuine fault into a silent under-consumption
    or a phantom shortage — the two failure modes this feature exists to make impossible.

    Four things must hold at once, and the per-component savepoint is what buys the last
    two: the failing component writes a ``BACKFLUSH_COMPONENT_FAILED`` chain row; NONE of
    its partial work survives (the first spilled row is rolled back with it, and its lot
    is not decremented); a DIFFERENT component in the same pass is unaffected; and the
    outer transaction is still committable — which is what keeps a reconcile-on-read GET
    from becoming a ``PendingRollbackError`` 500.
    """
    user = make_user(db_session)
    spilling = make_part(db_session, part_type="purchased")
    first = make_lot(db_session, spilling, qty=6, lot="FAULT-A", received_date=days(1))
    second = make_lot(db_session, spilling, qty=6, lot="FAULT-B", received_date=days(2))
    healthy = make_part(db_session, part_type="purchased")
    healthy_lot = make_lot(db_session, healthy, qty=50, lot="HEALTHY-A", received_date=days(1))

    fg = make_part(db_session, backflush=True)
    bom = make_bom(db_session, fg)
    add_bom_item(db_session, bom, spilling, quantity=2.0, item_number=10)
    add_bom_item(db_session, bom, healthy, quantity=1.0, item_number=20)
    wo = make_wo(db_session, fg, quantity_ordered=5, quantity_complete=5)
    make_op(db_session, wo, make_work_center(db_session), quantity_complete=5)

    _force_duplicate_backflush_issue_index(db_session)

    audit = AuditService(db_session, user)
    backflush_components_for_work_order(db_session, wo, user_id=user.id, company_id=COMPANY_A, audit=audit)
    db_session.commit()  # a poisoned session raises PendingRollbackError here
    db_session.expire_all()

    assert issue_rows(db_session, wo, spilling) == [], "the whole failing component rolled back, partial row included"
    assert db_session.get(InventoryItem, first.id).quantity_on_hand == 6, "and its lot was not decremented"
    assert db_session.get(InventoryItem, second.id).quantity_on_hand == 6

    assert [r.quantity for r in issue_rows(db_session, wo, healthy)] == [-5.0], "a sibling component is unaffected"
    assert db_session.get(InventoryItem, healthy_lot.id).quantity_on_hand == 45

    failed = audit_rows(db_session, BACKFLUSH_COMPONENT_FAILED_AUDIT_ACTION)
    assert len(failed) == 1, "the failure is on the tamper-evident chain, not only in a log line"
    assert failed[0].success == "false", "recorded as a FAILURE, matching the tie engine's twin"
    assert (failed[0].extra_data or {})["component_part_id"] == spilling.id
    assert (failed[0].extra_data or {})["work_order_id"] == wo.id
    assert failed[0].integrity_hash and failed[0].sequence_number is not None
    assert (
        audit_rows(db_session, BACKFLUSH_SHORTAGE_AUDIT_ACTION) == []
    ), "a real fault must NEVER be recorded as a shortage — that is the whole bug"


def test_reconcile_on_read_survives_a_component_failure(client: TestClient, db_session: Session):
    """The GET still returns 200, the reconcile still commits, and the chain row is there.

    ``_post_stock_movement_txn`` moves ``quantity_on_hand`` OUTSIDE
    ``_insert_txn_with_savepoint``'s guard, so without the per-component savepoint a
    failing draw would poison the session and turn a plain work-order detail read into a
    500 — on a path that exists to be quietly self-healing.
    """
    admin = make_user(db_session)
    spilling = make_part(db_session, part_type="purchased")
    make_lot(db_session, spilling, qty=6, lot="RECON-A", received_date=days(1))
    make_lot(db_session, spilling, qty=6, lot="RECON-B", received_date=days(2))

    fg = make_part(db_session, backflush=True)
    add_bom_item(db_session, make_bom(db_session, fg), spilling, quantity=2.0)
    wo = make_wo(db_session, fg, quantity_ordered=5, status_=WorkOrderStatus.IN_PROGRESS)
    op = make_op(db_session, wo, make_work_center(db_session), status_=OperationStatus.IN_PROGRESS)
    # Durable evidence the reconcile will act on: a closed run produced the full quantity.
    db_session.add(
        TimeEntry(
            user_id=admin.id,
            work_order_id=wo.id,
            operation_id=op.id,
            entry_type=TimeEntryType.RUN,
            clock_in=datetime.utcnow() - timedelta(hours=2),
            clock_out=datetime.utcnow() - timedelta(hours=1),
            duration_hours=1.0,
            quantity_produced=5,
            quantity_scrapped=0,
            company_id=COMPANY_A,
        )
    )
    db_session.commit()

    _force_duplicate_backflush_issue_index(db_session)

    resp = client.get(f"/api/v1/work-orders/{wo.id}", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.rollback()
    db_session.expire_all()
    assert db_session.get(WorkOrder, wo.id).status == WorkOrderStatus.COMPLETE, "the reconcile still committed"
    assert issue_rows(db_session, wo, spilling) == [], "the failing component moved no stock"
    assert len(audit_rows(db_session, BACKFLUSH_COMPONENT_FAILED_AUDIT_ACTION)) == 1


def test_the_versioned_update_is_flushed_before_any_savepoint_opens(db_session: Session):
    """An optimistic-lock conflict must ABORT the request, not degrade into a chain row.

    The per-component savepoint catches everything except ``StaleDataError``-with-propagate
    — and this seam deliberately passes ``propagate_lock_conflict=False``, because it runs
    from reconcile-on-read GETs where there is no 409 to return and no actor to attribute
    one to. That makes the ORDER load-bearing: an AUTOFLUSH ``StaleDataError`` from the
    work order's own pending versioned UPDATE, raised INSIDE a savepoint, would be
    swallowed into a ``BACKFLUSH_COMPONENT_FAILED`` row and the completion would silently
    proceed on a stale read. Flushing first puts that UPDATE outside every savepoint.

    Asserted both ways: structurally (the ``db.flush()`` precedes the first savepoint
    call in the source) and behaviourally (a genuinely stale version propagates).
    """
    source = inspect.getsource(cis.backflush_components_for_work_order)
    assert source.index("db.flush()") < source.index(
        "_issue_component_under_savepoint"
    ), "the versioned UPDATE must be flushed BEFORE the first per-component savepoint"

    user = make_user(db_session)
    component = make_part(db_session, part_type="purchased")
    make_lot(db_session, component, qty=100, lot="STALE-A", received_date=days(1))
    wo = backflush_job(db_session, component=component, qty_per_unit=2.0, quantity_complete=5.0)

    # A concurrent writer bumped the row's version behind this session's back.
    db_session.commit()
    wo = db_session.get(WorkOrder, wo.id)
    db_session.execute(
        text("UPDATE work_orders SET version = version + 1 WHERE id = :id"),
        {"id": wo.id},
    )
    wo.notes = "edited against a stale version"  # makes the UPDATE (and its version check) pending

    with pytest.raises(StaleDataError):
        backflush_components_for_work_order(
            db_session, wo, user_id=user.id, company_id=COMPANY_A, audit=AuditService(db_session, user)
        )

    db_session.rollback()
    assert (
        audit_rows(db_session, BACKFLUSH_COMPONENT_FAILED_AUDIT_ACTION) == []
    ), "a version conflict is NOT a component failure and must never be recorded as one"
    assert issue_rows(db_session, wo, component) == []


# ===========================================================================
# 4. Readers -- the new shape flows through the one shared predicate
# ===========================================================================


def _spilled_job(db: Session, user: User) -> tuple[WorkOrder, Part, list[InventoryItem]]:
    """A backflushed job whose one component draw spilled across two lots."""
    component = make_part(db, part_type="purchased", standard_cost=10.0)
    lots = [
        make_lot(db, component, qty=6, lot="READ-A", received_date=days(1), unit_cost=10.0),
        make_lot(db, component, qty=6, lot="READ-B", received_date=days(2), unit_cost=10.0),
    ]
    wo = backflush_job(db, component=component, qty_per_unit=2.0, quantity_complete=5.0)
    run_effects(db, wo, user)
    db.expire_all()
    return wo, component, lots


def test_job_cost_includes_backflush_rows(db_session: Session):
    """``WorkOrder.actual_cost`` / ``JobCost`` must not lose the material leg to a rename.

    Both cost implementations go through ``work_order_ledger_filter``, whose first arm was
    widened to ``reference_type IN ('work_order', 'work_order_backflush')`` — the ONLY
    downstream reader edit PR 4.4 needed. A reader still filtering on ``work_order``
    alone would silently drop every reconciled component row out of a compliance-facing
    number, which is exactly the defect PR 2 fixed for the operation shape.
    """
    user = make_user(db_session)
    wo, component, _ = _spilled_job(db_session, user)

    assert sum(r.quantity for r in issue_rows(db_session, wo, component)) == -10.0
    assert _issued_material_cost(db_session, wo, COMPANY_A) == 100.0, "10 units at $10, across two lots"


def test_analytics_cogs_includes_backflush_rows(db_session: Session):
    """The analytics leg shares the predicate, so the two can never drift apart."""
    user = make_user(db_session)
    wo, _, _ = _spilled_job(db_session, user)

    assert AnalyticsService(db_session, COMPANY_A)._issued_material_cost(wo.id) == 100.0
    assert AnalyticsService(db_session, COMPANY_A)._issued_material_cost(wo.id) == _issued_material_cost(
        db_session, wo, COMPANY_A
    )


def test_inventory_transactions_work_order_filter_includes_backflush_rows(client: TestClient, db_session: Session):
    """``GET /inventory/transactions?work_order_id=`` lists what the job actually burned."""
    user = make_user(db_session)
    wo, component, _ = _spilled_job(db_session, user)

    resp = client.get(f"/api/v1/inventory/transactions?work_order_id={wo.id}", headers=headers_for(user))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    shapes = {row["reference_type"] for row in body}
    assert BACKFLUSH_REFERENCE_TYPE in shapes, "the reconciled component rows must appear"
    assert WORK_ORDER_REFERENCE_TYPE in shapes, "and the finished-good receipt still does"
    component_rows = [r for r in body if r["reference_type"] == BACKFLUSH_REFERENCE_TYPE]
    assert len(component_rows) == 2, "both spilled rows, not a collapsed one"
    assert sum(r["quantity"] for r in component_rows) == -10.0


def test_traceability_resolves_the_backflush_shape(client: TestClient, db_session: Session):
    """As-built genealogy: N spilled rows collapse to one line PER LOT, not one per row.

    ``traceability`` needed no code change — its ``else`` branch already treats a
    non-operation reference id as the work order — but "no change needed" is a claim, and
    an as-built record missing the material a job actually burned is an AS9100D hole. Both
    directions are asserted: the FG lot's trace enumerates the consumed component lots,
    and each consumed lot's own trace names the work order that used it.
    """
    admin = make_user(db_session)
    wo, component, lots = _spilled_job(db_session, admin)
    wo = db_session.get(WorkOrder, wo.id)

    resp = client.get(f"/api/v1/traceability/lot/{wo.lot_number}", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    consumed = resp.json()["consumed_components"]
    lines = sorted(
        ((c["lot_number"], c["quantity"]) for c in consumed if c["component_part_id"] == component.id),
    )
    assert lines == [("READ-A", 6.0), ("READ-B", 4.0)], "one line per (work order, part, LOT), quantities positive"

    for item in lots:
        trace = client.get(f"/api/v1/traceability/lot/{item.lot_number}", headers=headers_for(admin))
        assert trace.status_code == status.HTTP_200_OK, trace.text
        assert wo.work_order_number in trace.json()["work_orders_used"], f"{item.lot_number} must name its work order"


# ===========================================================================
# 5. The notification the outbox has been dropping since Batch 6
# ===========================================================================


def test_backflush_shortage_event_resolves_to_a_catalog_entry(db_session: Session):
    """``backflush_shortage`` has been EMITTED with no catalog row, so it went nowhere.

    The outbox deliberately ignores an event type with no entry (visible future decisions,
    not silent drops) — which meant a shortage that drove a source lot negative wrote its
    audit row and then notified nobody. The catalog entry is the whole wiring; the emit
    site is unchanged, which is why this test drives a REAL shortage and then looks the
    emitted type up rather than asserting against the registry alone.
    """
    user = make_user(db_session)
    component = make_part(db_session, part_type="purchased")
    make_lot(db_session, component, qty=1, lot="NOTIFY-A", received_date=days(1))
    wo = backflush_job(db_session, component=component, qty_per_unit=2.0, quantity_complete=5.0)

    run_effects(db_session, wo, user)
    db_session.expire_all()

    events = (
        db_session.query(OperationalEvent)
        .filter(
            OperationalEvent.company_id == COMPANY_A,
            OperationalEvent.event_type == BACKFLUSH_SHORTAGE_EVENT_TYPE,
        )
        .all()
    )
    assert len(events) == 1, "the shortage really emitted, or the lookup below proves nothing"

    entry = entry_for_event_type(events[0].event_type)
    assert entry is not None, "the emitted type must now resolve to a catalog entry"
    assert entry.event_key == "material.backflush_shortage"
    assert entry.severity == "warning"
    assert (
        entry.event_key != "material.allocation_shortage"
    ), "distinct from the tie engine's shortage so the settings matrix can gate them apart"


# ===========================================================================
# 6. Shortage DISCLOSURE -- what a shortage row may TRUTHFULLY say about the
#    stock the draw did not take
# ===========================================================================
#
# A shortage row is read by a human deciding what to do next, and the two draw modes come
# up short for entirely different reasons:
#
#   * UNPINNED -- every consumable lot WAS walked, so anything left on the rack is there
#     because the predicate skipped it. Disclosing it turns a bare purchasing signal into
#     an MRB signal.
#   * PINNED   -- the reason no other lot was drawn is THE PIN. Disclosing held stock here
#     is false by implication twice over: it tells an MRB reviewer that releasing the
#     quarantined material clears the shortage (it does not -- the pin still excludes it),
#     and it says nothing about the freely-available stock in other heats that the pin is
#     what excluded.
#
# The two tests below run over an IDENTICAL stock picture so the only variable is the pin.


def _pin_scenario_stock(db: Session, tag: str) -> tuple[Part, InventoryItem, InventoryItem, InventoryItem]:
    """One stock picture, built twice: 6 certified, 60 quarantined, 200 in another heat.

    Deliberately identical between the pinned and unpinned tests below, so the two
    shortage rows can differ ONLY because one draw was pinned. That is the whole claim.
    """
    part = make_part(db, uom="sheets", part_type="raw_material")
    certified = make_lot(db, part, qty=6, lot=f"{tag}-HEAT-A", received_date=days(50))
    quarantined = make_lot(db, part, qty=60, lot=f"{tag}-MRB", received_date=days(1), status_="quarantine")
    other_heat = make_lot(db, part, qty=200, lot=f"{tag}-HEAT-B", received_date=days(2))
    return part, certified, quarantined, other_heat


def test_a_pinned_shortage_names_the_pin_and_never_the_segregated_stock(db_session: Session):
    """A PINNED shortage must not present held stock as the constraint. It isn't.

    This is the nest case: a tie pinned to a certified heat with 6 on hand against a
    demand of 10, while the part also carries 60 in quarantine and 200 freely available in
    a different heat. Disclosing the 60 here — which the shortage path used to do
    unconditionally, because ``held_stock_summary`` was called on both branches — sends an
    MRB reviewer to release material that changes nothing, since the pin still excludes it,
    and stays silent about the 200 that the pin is actually what excluded.

    The row now names the RESTRICTION instead, and the held query is not even run. Both
    halves are asserted: the payload carries ``pinned_lot`` with a zero held quantity, and
    the prose names neither segregated stock nor the other heat.
    """
    user = make_user(db_session)
    fg = make_part(db_session)
    sheet, certified, quarantined, other_heat = _pin_scenario_stock(db_session, "PIN")

    wo = make_wo(db_session, fg, quantity_ordered=10, quantity_complete=10)
    make_op(db_session, wo, make_work_center(db_session), quantity_complete=10)
    tie(db_session, wo, sheet, operation=None, qty_planned=10.0, pinned=certified)

    run_effects(db_session, wo, user)
    db_session.expire_all()

    # The draw itself is unchanged: pinned lot only, driven negative, no spill.
    assert [(r.lot_number, r.quantity) for r in issue_rows(db_session, wo, sheet)] == [
        ("PIN-HEAT-A", -6.0),
        ("PIN-HEAT-A", -4.0),
    ]
    assert db_session.get(InventoryItem, quarantined.id).quantity_on_hand == 60, "the pin excluded it"
    assert db_session.get(InventoryItem, other_heat.id).quantity_on_hand == 200, "…and it excluded this too"

    [row] = audit_rows(db_session, BACKFLUSH_SHORTAGE_AUDIT_ACTION)
    extra = row.extra_data or {}
    assert (extra["required_quantity"], extra["available_quantity"], extra["shortfall"]) == (10.0, 6.0, 4.0)
    assert extra["pinned_lot"] == "PIN-HEAT-A", "the pin is the disclosed reason nothing else was drawn"
    assert extra["held_quantity_skipped"] == 0.0, "the held query is not even RUN on a pinned draw"
    assert extra["held_lot_numbers"] == []

    description = row.description or ""
    assert "draw restricted to pinned lot PIN-HEAT-A" in description
    assert "segregated status" not in description, "held stock is NOT the constraint here"
    assert quarantined.lot_number not in description, "naming the quarantined lot sends MRB to a false remedy"
    assert other_heat.lot_number not in description


def test_an_unpinned_shortage_still_discloses_the_segregated_stock_it_skipped(db_session: Session):
    """…and the UNPINNED half of the same rule, on the SAME stock picture.

    Here every consumable lot really was walked, so the 60 in quarantine is the only thing
    left on the rack and "release it" is a genuine remedy. Removing the disclosure to fix
    the pinned case would have re-opened the lie-by-omission it was added for, so both
    directions are locked, not just the one that changed.
    """
    user = make_user(db_session)
    fg = make_part(db_session)
    sheet, certified, quarantined, other_heat = _pin_scenario_stock(db_session, "UNPIN")

    wo = make_wo(db_session, fg, quantity_ordered=300, quantity_complete=300)
    make_op(db_session, wo, make_work_center(db_session), quantity_complete=300)
    tie(db_session, wo, sheet, operation=None, qty_planned=300.0)

    run_effects(db_session, wo, user)
    db_session.expire_all()

    # FIFO over the CONSUMABLE lots only: HEAT-B (day 2) then HEAT-A (day 50); the
    # quarantined day-1 lot is skipped despite being the oldest.
    assert [(r.lot_number, r.quantity) for r in issue_rows(db_session, wo, sheet)] == [
        ("UNPIN-HEAT-B", -200.0),
        ("UNPIN-HEAT-A", -6.0),
        ("UNPIN-HEAT-A", -94.0),
    ]
    assert db_session.get(InventoryItem, quarantined.id).quantity_on_hand == 60, "skipped, never consumed"

    [row] = audit_rows(db_session, BACKFLUSH_SHORTAGE_AUDIT_ACTION)
    extra = row.extra_data or {}
    assert (extra["required_quantity"], extra["available_quantity"], extra["shortfall"]) == (300.0, 206.0, 94.0)
    assert extra["pinned_lot"] is None, "nothing restricted this draw"
    assert extra["held_quantity_skipped"] == 60.0
    assert extra["held_lot_numbers"] == ["UNPIN-MRB"]

    description = row.description or ""
    assert "60" in description and "segregated status" in description
    assert "UNPIN-MRB" in description, "the reader must be able to go and look at the lot"
    assert "restricted to pinned lot" not in description

    # Sanity: this ran over the SAME stock picture as the pinned test above. If the fixture
    # ever diverges, the pair stops being a contrast and becomes two unrelated scenarios.
    assert db_session.get(InventoryItem, certified.id).quantity_on_hand == -94, "the anchor absorbed the remainder"
    assert db_session.get(InventoryItem, other_heat.id).quantity_on_hand == 0, "…after the other heat was exhausted"


def test_the_pinned_branch_does_not_even_QUERY_held_stock(db_session: Session, monkeypatch):
    """Not "returns zero" — does not RUN. Asserted by making the query fatal.

    ``held_quantity_skipped == 0.0`` alone is satisfied by a query that happens to find
    nothing, so on its own it would pass on a database with no held stock even if the
    branch still queried. Stubbing ``held_stock_summary`` to raise proves the pinned path
    never reaches it, which is what makes the disclosure cheap as well as truthful.
    """

    def _explode(*args, **kwargs):  # pragma: no cover - the point is that it never runs
        raise AssertionError("held_stock_summary must not be queried on a pinned draw")

    monkeypatch.setattr(mcs, "held_stock_summary", _explode)

    part = make_part(db_session, part_type="purchased")
    pinned_lot = make_lot(db_session, part, qty=1, lot="NOQUERY-PIN", received_date=days(1))
    make_lot(db_session, part, qty=99, lot="NOQUERY-HELD", received_date=days(1), status_="on_hold")

    held_qty, held_lots, pinned_label = shortage_draw_disclosure(
        db_session,
        part_id=part.id,
        company_id=COMPANY_A,
        pinned_inventory_item_id=pinned_lot.id,
        pinned_item=pinned_lot,
    )
    assert (held_qty, held_lots, pinned_label) == (0.0, [], "NOQUERY-PIN")

    # The unpinned branch DOES query — otherwise the stub above proves nothing, because a
    # helper nobody ever calls also never raises.
    monkeypatch.undo()
    assert shortage_draw_disclosure(
        db_session,
        part_id=part.id,
        company_id=COMPANY_A,
        pinned_inventory_item_id=None,
        pinned_item=None,
    ) == (99.0, ["NOQUERY-HELD"], None)


def test_an_unresolvable_pin_is_still_reported_as_pin_restricted(db_session: Session):
    """A pin that resolves to nothing (deleted row, other tenant) still RESTRICTED the draw.

    Falling back to the held disclosure here would re-introduce the same false remedy on
    the one path where the reader has least context. The clause names the inventory row by
    id instead — the row IS the lot, named or not.
    """
    part = make_part(db_session, part_type="purchased")
    make_lot(db_session, part, qty=99, lot="ORPHAN-HELD", received_date=days(1), status_="quarantine")

    assert shortage_draw_disclosure(
        db_session,
        part_id=part.id,
        company_id=COMPANY_A,
        pinned_inventory_item_id=987654,
        pinned_item=None,
    ) == (0.0, [], "#987654")
    assert "draw restricted to pinned lot #987654" in held_stock_disclosure(0.0, [], "#987654")


def test_a_pinned_shortage_on_the_LIVE_tie_engine_names_the_pin_too(db_session: Session):
    """The same rule on the operation-scoped engine — the shipped path, where nests live.

    Both engines take their disclosure from ``shortage_draw_disclosure``, and this is the
    assertion that keeps them from drifting: the defect the pinned branch fixes was found
    against this leg, so pinning it only on the dark backflush would have missed it.
    """
    user = make_user(db_session)
    fg = make_part(db_session)
    sheet, certified, quarantined, other_heat = _pin_scenario_stock(db_session, "LIVEPIN")

    wo = make_wo(db_session, fg, quantity_ordered=10, quantity_complete=10)
    op = make_op(db_session, wo, make_work_center(db_session), quantity_complete=10)
    tie(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=10.0, pinned=certified)

    apply_operation_completion_inventory_effects(
        db_session, wo, op, user_id=user.id, company_id=COMPANY_A, audit=AuditService(db_session, user)
    )
    db_session.commit()
    db_session.expire_all()

    assert db_session.get(InventoryItem, quarantined.id).quantity_on_hand == 60
    assert db_session.get(InventoryItem, other_heat.id).quantity_on_hand == 200

    [row] = audit_rows(db_session, ALLOCATION_SHORTAGE_AUDIT_ACTION)
    extra = row.extra_data or {}
    assert extra["shortfall"] == 4.0
    assert extra["pinned_lot"] == "LIVEPIN-HEAT-A"
    assert extra["held_quantity_skipped"] == 0.0
    assert extra["held_lot_numbers"] == []
    assert "draw restricted to pinned lot LIVEPIN-HEAT-A" in (row.description or "")
    assert "segregated status" not in (row.description or "")


# ===========================================================================
# 7. The DEGRADED path must not be quieter than the condition it degrades from
# ===========================================================================
#
# A shortage moves stock (a lot goes negative) and notifies Purchasing. "The draw raised
# and rolled back, so nothing moved at all" is the strictly WORSE material-trail gap --
# and it used to write an audit row and then reach nobody. Where
# ``chk_inventory_items_quantity_non_negative`` is live, EVERY shortage arrives on this
# path instead, so that deployment would have got no consumption notification whatsoever.


def _force_duplicate_operation_issue_index(db: Session) -> None:
    """The tie engine's twin of ``_force_duplicate_backflush_issue_index``.

    No index covers the ``work_order_operation`` shape either, and SQLite does not enforce
    foreign keys, so a genuine ``IntegrityError`` has to be manufactured. A one-row-per-
    (company, operation, part) unique index makes a SPILLING draw violate on its second
    row, from inside the engine, exactly as a real constraint would in production.
    """
    db.commit()
    db.execute(
        text(
            "CREATE UNIQUE INDEX tmp_uq_operation_issue ON inventory_transactions "
            "(company_id, reference_id, part_id) "
            "WHERE reference_type = 'work_order_operation' AND transaction_type = 'ISSUE'"
        )
    )
    db.commit()


def test_a_rolled_back_component_still_produces_a_notifiable_event(db_session: Session):
    """``BACKFLUSH_COMPONENT_FAILED`` now emits, and the emitted type resolves to a catalog row.

    The audit row is the compliance record and it was already written; what was missing was
    any signal a human sees. Both ends are asserted — a real event row with the failure
    context in its payload, and a catalog lookup of the type that was actually emitted
    (not of the registry in isolation, which would pass even if the emit were dead).

    The last assertion is the one that matters most: the degraded path must not be
    mis-signalled as a shortage. Purchasing acting on "stock went negative" when in fact
    stock never moved is a different remedy applied to a different problem.
    """
    user = make_user(db_session)
    spilling = make_part(db_session, part_type="purchased")
    first = make_lot(db_session, spilling, qty=6, lot="EVENT-A", received_date=days(1))
    second = make_lot(db_session, spilling, qty=6, lot="EVENT-B", received_date=days(2))

    wo = backflush_job(db_session, component=spilling, qty_per_unit=2.0, quantity_complete=5.0)
    _force_duplicate_backflush_issue_index(db_session)

    backflush_components_for_work_order(
        db_session, wo, user_id=user.id, company_id=COMPANY_A, audit=AuditService(db_session, user)
    )
    db_session.commit()
    db_session.expire_all()

    assert issue_rows(db_session, wo, spilling) == [], "the component really did roll back"
    assert db_session.get(InventoryItem, first.id).quantity_on_hand == 6
    assert db_session.get(InventoryItem, second.id).quantity_on_hand == 6
    assert len(audit_rows(db_session, BACKFLUSH_COMPONENT_FAILED_AUDIT_ACTION)) == 1

    events = (
        db_session.query(OperationalEvent)
        .filter(
            OperationalEvent.company_id == COMPANY_A,
            OperationalEvent.event_type == BACKFLUSH_COMPONENT_FAILED_EVENT_TYPE,
        )
        .all()
    )
    assert len(events) == 1, "a rolled-back component must reach the outbox, not only the audit log"
    event = events[0]
    assert event.severity == "warning"
    assert event.work_order_id == wo.id
    assert (event.event_payload or {})["component_part_id"] == spilling.id
    assert "IntegrityError" in (event.event_payload or {})["error"], "the cause travels with the signal"

    entry = entry_for_event_type(event.event_type)
    assert entry is not None, "the emitted type must resolve, or the outbox drops it silently"
    assert entry.event_key == "material.backflush_failed"
    assert entry.severity == "warning"
    assert (
        db_session.query(OperationalEvent)
        .filter(
            OperationalEvent.company_id == COMPANY_A,
            OperationalEvent.event_type == BACKFLUSH_SHORTAGE_EVENT_TYPE,
        )
        .count()
        == 0
    ), "a rolled-back draw is NOT a shortage and must never be signalled as one"


def test_a_rolled_back_tie_consumption_still_produces_a_notifiable_event(db_session: Session):
    """The same guarantee on the LIVE operation-scoped engine, with its own catalog key.

    Kept separately keyed from the backflush failure so an operator can tell a tied-material
    rollback from a BOM one without opening the audit log, and so the settings matrix can
    gate them independently.
    """
    user = make_user(db_session)
    fg = make_part(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    first = make_lot(db_session, sheet, qty=6, lot="TIE-EVENT-A", received_date=days(1))
    second = make_lot(db_session, sheet, qty=6, lot="TIE-EVENT-B", received_date=days(2))

    wo = make_wo(db_session, fg, quantity_ordered=10, quantity_complete=10)
    op = make_op(db_session, wo, make_work_center(db_session), quantity_complete=10)
    allocation = tie(db_session, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=10.0)

    _force_duplicate_operation_issue_index(db_session)

    apply_operation_completion_inventory_effects(
        db_session, wo, op, user_id=user.id, company_id=COMPANY_A, audit=AuditService(db_session, user)
    )
    db_session.commit()  # a poisoned session raises PendingRollbackError here
    db_session.expire_all()

    assert db_session.get(InventoryItem, first.id).quantity_on_hand == 6, "nothing survived the rollback"
    assert db_session.get(InventoryItem, second.id).quantity_on_hand == 6
    assert db_session.get(WorkOrderMaterialAllocation, allocation.id).qty_consumed == 0.0

    [failed] = audit_rows(db_session, ALLOCATION_CONSUMPTION_FAILED_AUDIT_ACTION)
    assert failed.success == "false"
    assert failed.integrity_hash and failed.sequence_number is not None

    events = (
        db_session.query(OperationalEvent)
        .filter(
            OperationalEvent.company_id == COMPANY_A,
            OperationalEvent.event_type == ALLOCATION_CONSUMPTION_FAILED_EVENT_TYPE,
        )
        .all()
    )
    assert len(events) == 1
    event = events[0]
    assert event.severity == "warning"
    assert event.work_order_id == wo.id
    assert (event.event_payload or {})["allocation_id"] == allocation.id
    # Deliberately NOT set on the row: ``emit`` validates ``operation_id`` with an extra
    # query and this runs after a savepoint rollback, where the cheapest possible touch is
    # the right posture. The id still travels, in the payload.
    assert event.operation_id is None
    assert (event.event_payload or {})["work_order_operation_id"] == op.id

    entry = entry_for_event_type(event.event_type)
    assert entry is not None
    assert entry.event_key == "material.allocation_consumption_failed"
    assert entry.event_key != "material.backflush_failed", "one key per engine, gated independently"


# ===========================================================================
# 8. Leg 2's suppression record -- delta BEFORE fence
# ===========================================================================
#
# Leg 2 computes its delta before consulting the legacy fence, which leg 1 cannot do (its
# suppression layers PRODUCE its target). Neither consequence below is reachable while the
# leg runs once per work-order lifetime; both become live the moment a re-entry trigger is
# added, which is the whole reason to get the order right now rather than then.


def _fenced_partly_drained_tie(db: Session, user: User, *, already: float):
    """A work-order-scoped tie that has already drawn ``already``, on a LEGACY-fenced job.

    Two hand-built ledger rows, doing two different jobs: a ``work_order_backflush`` ISSUE
    carrying the tie's ``allocation_id`` (its own prior draw, which is what
    ``net_consumed_quantity_for_allocation`` reads), and a legacy ``('work_order', ISSUE)``
    row with a NULL ``allocation_id`` (which the fence reads and the tie's net cannot see).
    The finished part deliberately does NOT opt into the BOM leg, so every assertion below
    is about leg 2 alone.
    """
    component = make_part(db, part_type="purchased")
    lot = make_lot(db, component, qty=100, lot=f"FENCED-{_next()}", received_date=days(1))

    fg = make_part(db)  # backflush_components False -- leg 1 never runs
    wo = make_wo(db, fg, quantity_ordered=5, quantity_complete=5)
    make_op(db, wo, make_work_center(db), quantity_complete=5)
    allocation = tie(db, wo, component, operation=None, qty_planned=10.0)

    def _row(quantity: float, reference_type: str, allocation_id):
        return InventoryTransaction(
            company_id=COMPANY_A,
            inventory_item_id=lot.id,
            part_id=component.id,
            transaction_type=TransactionType.ISSUE,
            quantity=-quantity,
            from_location=lot.location,
            lot_number=lot.lot_number,
            reference_type=reference_type,
            reference_id=wo.id,
            reference_number=wo.work_order_number,
            allocation_id=allocation_id,
            unit_cost=2.0,
            total_cost=quantity * 2.0,
            created_by=user.id,
        )

    if already > 0:
        db.add(_row(already, BACKFLUSH_REFERENCE_TYPE, allocation.id))
    db.add(_row(3.0, WORK_ORDER_REFERENCE_TYPE, None))
    lot.quantity_on_hand = 100.0 - already - 3.0
    db.commit()
    return wo, component, allocation, lot


def test_leg_2_records_the_unmet_remainder_not_the_gross_planned_quantity(db_session: Session):
    """A partly-drained tie that gets fenced records what was DROPPED, not what was planned.

    The tie planned 10 and the ledger already holds 4 of it, so exactly 6 failed to move.
    Recording ``qty_planned`` would put a false figure on the tamper-evident chain —
    claiming material was dropped that had in fact already been issued — and the
    ``ledger_net_issued`` field is what lets a reader check the arithmetic without going
    back to the ledger.
    """
    user = make_user(db_session)
    wo, component, allocation, lot = _fenced_partly_drained_tie(db_session, user, already=4.0)

    assert net_consumed_quantity_for_allocation(db_session, allocation_id=allocation.id, company_id=COMPANY_A) == 4.0
    on_hand_before = db_session.get(InventoryItem, lot.id).quantity_on_hand
    # The seeded prior draw already carries the new shape, so "no new rows" is a DELTA
    # against it, not an empty list.
    rows_before = {r.id for r in issue_rows(db_session, wo, component)}

    run_effects(db_session, wo, user)
    db_session.expire_all()

    assert {r.id for r in issue_rows(db_session, wo, component)} == rows_before, "a fenced tie moves nothing more"
    assert db_session.get(InventoryItem, lot.id).quantity_on_hand == on_hand_before

    [blocked] = audit_rows(db_session, BACKFLUSH_DOUBLE_ISSUE_BLOCKED_AUDIT_ACTION)
    extra = blocked.extra_data or {}
    assert extra["suppressed_quantity"] == 6.0, "10 planned less the 4 the ledger already holds"
    assert extra["ledger_net_issued"] == 4.0, "…and the term it was computed from, so the row is checkable"
    assert extra["suppression_reason"] == "already_issued"
    assert extra["component_part_id"] == component.id
    assert "Backflush of 6.0 of component" in (blocked.description or ""), "the prose carries the remainder too"
    assert extra["suppressed_quantity"] != float(
        db_session.get(WorkOrderMaterialAllocation, allocation.id).qty_planned
    ), "explicitly NOT the gross qty_planned — that is the figure this ordering exists to stop recording"


def test_a_converged_leg_2_tie_writes_no_suppression_row_even_though_it_is_fenced(db_session: Session):
    """Nothing was suppressed, so nothing is recorded — the delta test runs FIRST.

    A tie whose ledger net has reached ``qty_planned`` has no outstanding demand to drop.
    Consulting the fence anyway and writing a ``BACKFLUSH_DOUBLE_ISSUE_BLOCKED`` row would
    be a record of a decision that was never taken, on the hash chain, for every replay
    forever.

    The fence is asserted to be ARMED before the run, which is what stops this from being
    a test that passes because nothing was fenced in the first place.
    """
    user = make_user(db_session)
    wo, component, allocation, _ = _fenced_partly_drained_tie(db_session, user, already=10.0)

    assert net_consumed_quantity_for_allocation(db_session, allocation_id=allocation.id, company_id=COMPANY_A) == 10.0
    assert cis._component_already_issued(
        db_session, wo.id, component.id, COMPANY_A
    ), "the fence must be ARMED, or the assertion below is trivially satisfied"
    rows_before = {r.id for r in issue_rows(db_session, wo, component)}

    run_effects(db_session, wo, user)
    db_session.expire_all()

    assert {r.id for r in issue_rows(db_session, wo, component)} == rows_before
    assert (
        audit_rows(db_session, BACKFLUSH_DOUBLE_ISSUE_BLOCKED_AUDIT_ACTION) == []
    ), "a converged tie suppressed nothing, so it records nothing"


def test_leg_1_keeps_its_fence_first_ordering_and_records_the_resolved_target(db_session: Session):
    """Leg 1 CANNOT reorder, and the asymmetry is deliberate rather than an oversight.

    ``_resolve_backflush_components`` runs BOTH suppression layers to produce leg 1's
    target, so there is no ordering in which the fence could follow the delta test and
    still describe what was dropped. Its recorded quantity is therefore the resolved target
    — which is still "the demand that did not move", because the fence drops the whole of
    it. Locked so the two legs' divergence stays a decision rather than a drift.
    """
    user = make_user(db_session)
    component = make_part(db_session, part_type="purchased")
    lot = make_lot(db_session, component, qty=100, lot="LEG1-FENCE", received_date=days(1))

    wo = backflush_job(db_session, component=component, qty_per_unit=2.0, quantity_complete=5.0)
    db_session.add(
        InventoryTransaction(
            company_id=COMPANY_A,
            inventory_item_id=lot.id,
            part_id=component.id,
            transaction_type=TransactionType.ISSUE,
            quantity=-10.0,
            from_location=lot.location,
            lot_number=lot.lot_number,
            reference_type=WORK_ORDER_REFERENCE_TYPE,
            reference_id=wo.id,
            reference_number=wo.work_order_number,
            allocation_id=None,
            unit_cost=2.0,
            total_cost=20.0,
            created_by=user.id,
        )
    )
    lot.quantity_on_hand = 90.0
    db_session.commit()

    run_effects(db_session, wo, user)
    db_session.expire_all()

    assert issue_rows(db_session, wo, component) == []
    [blocked] = audit_rows(db_session, BACKFLUSH_DOUBLE_ISSUE_BLOCKED_AUDIT_ACTION)
    extra = blocked.extra_data or {}
    assert extra["suppressed_quantity"] == 10.0, "the resolved BOM target, dropped whole"
    assert extra["ledger_net_issued"] is None, "leg 1's net is not read on the fenced path"

    source = inspect.getsource(cis.backflush_components_for_work_order)
    leg_1 = source[source.index("LEG 1: BOM / routing") :]
    assert leg_1.index("_component_already_issued") < leg_1.index(
        "delta = target"
    ), "leg 1 must keep consulting the fence BEFORE the delta test — its target depends on it"
