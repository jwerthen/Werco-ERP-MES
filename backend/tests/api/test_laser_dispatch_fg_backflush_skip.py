"""Laser nest-dispatch WOs mint no phantom finished goods and run no parent backflush.

Finding B3: ``apply_completion_inventory_effects`` ran the FG receipt unconditionally,
so completing a PARENTED laser child WO -- created with ``part_id = parent part``
(``work_orders.py::_ensure_laser_child_work_order``) and ``quantity_complete`` = pooled
nest RUNS -- received phantom FINISHED_GOODS of the parent part (5 nests x 8 runs = 40
phantom units; the parent's own completion later books the real quantity). The same
wrong demand basis reached the BOM-backflush leg: an ARMED parent part would have had
its BOM exploded against the child's run count.

The gate is ``is_laser_dispatch_work_order`` (``work_order_type == 'laser_cutting'``) --
the exact predicate every other nest-dispatch exemption routes through, matching both
shapes (parented children AND part-less standalones) and nothing else. Tied-material
consumption is deliberately UNAFFECTED: ties are the nest flow's actual consumption
mechanism.

Also locked here: the skip logs at DEBUG (expected behavior, not an alarm), and the
part-less standalone no longer emits the old "part not found" WARNING.
"""

import logging
from datetime import date, timedelta

import pytest
from sqlalchemy.orm import Session

from app.models.bom import BOM, BOMItem
from app.models.company import Company
from app.models.inventory import InventoryItem, InventoryTransaction, TransactionType
from app.models.part import Part
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder, WorkOrderStatus, WorkOrderType
from app.models.work_order_material import AllocationSource, AllocationStatus, WorkOrderMaterialAllocation
from app.services.audit_service import AuditService
from app.services.completion_inventory_service import FINISHED_GOODS_LOCATION, apply_completion_inventory_effects

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
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


def make_user(db: Session) -> User:
    _ensure_company(db, COMPANY_A)
    n = _next()
    user = User(
        email=f"laserfg-{n}@co{COMPANY_A}.test",
        employee_id=f"LFG-{n:05d}",
        first_name="Laser",
        last_name="FG",
        hashed_password=TEST_PASSWORD_HASH,
        role=UserRole.ADMIN,
        is_active=True,
        is_superuser=False,
        company_id=COMPANY_A,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def make_part(db: Session, *, backflush: bool = False, standard_cost: float = 7.5) -> Part:
    _ensure_company(db, COMPANY_A)
    n = _next()
    part = Part(
        part_number=f"LFG-P-{n}",
        name=f"Part {n}",
        description="laser-fg fixture part",
        part_type="manufactured",
        unit_of_measure="each",
        standard_cost=standard_cost,
        backflush_components=backflush,
        is_active=True,
        company_id=COMPANY_A,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def make_wo(
    db: Session,
    part: Part = None,
    *,
    quantity_complete: float = 0,
    work_order_type: str = WorkOrderType.PRODUCTION.value,
    parent: WorkOrder = None,
) -> WorkOrder:
    n = _next()
    wo = WorkOrder(
        work_order_number=f"LFG-WO-{n:05d}",
        part_id=part.id if part is not None else None,
        parent_work_order_id=parent.id if parent is not None else None,
        work_order_type=work_order_type,
        quantity_ordered=10,
        quantity_complete=quantity_complete,
        status=WorkOrderStatus.IN_PROGRESS,
        priority=5,
        due_date=date.today() + timedelta(days=30),
        company_id=COMPANY_A,
    )
    db.add(wo)
    db.commit()
    db.refresh(wo)
    return wo


def make_stock(db: Session, part: Part, *, qty: float, lot: str) -> InventoryItem:
    item = InventoryItem(
        part_id=part.id,
        location="RAW-A",
        warehouse="MAIN",
        quantity_on_hand=qty,
        quantity_available=qty,
        lot_number=lot,
        unit_cost=2.0,
        status="available",
        is_active=True,
        company_id=COMPANY_A,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def make_wo_tie(db: Session, wo: WorkOrder, part: Part, *, qty_planned: float) -> WorkOrderMaterialAllocation:
    allocation = WorkOrderMaterialAllocation(
        company_id=COMPANY_A,
        work_order_id=wo.id,
        work_order_operation_id=None,
        part_id=part.id,
        source=AllocationSource.NEST,
        status=AllocationStatus.OPEN,
        qty_planned=qty_planned,
        unit_of_measure="each",
        qty_consumed=0.0,
    )
    db.add(allocation)
    db.commit()
    db.refresh(allocation)
    return allocation


def fg_receipts(db: Session, wo_id: int) -> list:
    return (
        db.query(InventoryTransaction)
        .filter(
            InventoryTransaction.company_id == COMPANY_A,
            InventoryTransaction.reference_type == "work_order",
            InventoryTransaction.reference_id == wo_id,
            InventoryTransaction.transaction_type == TransactionType.RECEIVE,
        )
        .all()
    )


def fg_on_hand(db: Session, part_id: int) -> float:
    rows = (
        db.query(InventoryItem)
        .filter(
            InventoryItem.company_id == COMPANY_A,
            InventoryItem.part_id == part_id,
            InventoryItem.location == FINISHED_GOODS_LOCATION,
        )
        .all()
    )
    return sum(float(r.quantity_on_hand or 0) for r in rows)


def backflush_issues(db: Session, wo_id: int) -> list:
    return (
        db.query(InventoryTransaction)
        .filter(
            InventoryTransaction.company_id == COMPANY_A,
            InventoryTransaction.reference_type == "work_order_backflush",
            InventoryTransaction.reference_id == wo_id,
            InventoryTransaction.transaction_type == TransactionType.ISSUE,
        )
        .all()
    )


def _apply(db: Session, wo: WorkOrder, user: User):
    audit = AuditService(db, user)
    result = apply_completion_inventory_effects(db, wo, user_id=user.id, company_id=COMPANY_A, audit=audit)
    db.commit()
    db.expire_all()
    return result


def _armed_parent_with_bom(db: Session):
    """An ARMED parent part (backflush_components=True) with a one-line BOM, plus
    component stock the backflush would draw from."""
    component = make_part(db, standard_cost=2.0)
    stock = make_stock(db, component, qty=100.0, lot="LFG-RAW-1")
    parent_part = make_part(db, backflush=True, standard_cost=5.0)
    bom = BOM(part_id=parent_part.id, revision="A", is_active=True, company_id=COMPANY_A)
    db.add(bom)
    db.flush()
    db.add(
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
    db.commit()
    return parent_part, component, stock


def test_parented_laser_child_completion_mints_no_phantom_fg(db_session: Session):
    """The headline defect: 5 nests x 8 runs booked 40 phantom units of the PARENT part."""
    user = make_user(db_session)
    parent_part = make_part(db_session)
    parent = make_wo(db_session, parent_part, quantity_complete=0)
    child = make_wo(
        db_session,
        parent_part,
        quantity_complete=40,  # pooled nest RUNS, not parent units
        work_order_type=WorkOrderType.LASER_CUTTING.value,
        parent=parent,
    )

    _apply(db_session, child, user)

    assert fg_receipts(db_session, child.id) == [], "a nest-dispatch child must not book an FG receipt"
    assert fg_on_hand(db_session, parent_part.id) == 0.0, "no phantom finished goods of the parent part"


def test_parented_laser_child_still_consumes_its_material_ties(db_session: Session):
    """Ties are the nest flow's actual consumption mechanism -- the FG/backflush skip
    must not touch them."""
    user = make_user(db_session)
    sheet = make_part(db_session, standard_cost=3.0)
    stock = make_stock(db_session, sheet, qty=50.0, lot="LFG-SHEET-1")
    parent_part = make_part(db_session)
    parent = make_wo(db_session, parent_part)
    child = make_wo(
        db_session,
        parent_part,
        quantity_complete=8,
        work_order_type=WorkOrderType.LASER_CUTTING.value,
        parent=parent,
    )
    make_wo_tie(db_session, child, sheet, qty_planned=5.0)

    _apply(db_session, child, user)

    assert fg_receipts(db_session, child.id) == []
    assert float(db_session.get(InventoryItem, stock.id).quantity_on_hand) == 45.0, "the tie must still consume"
    tie_issues = backflush_issues(db_session, child.id)
    assert len(tie_issues) == 1 and float(tie_issues[0].quantity) == -5.0


def test_armed_parent_part_does_not_backflush_through_the_child(db_session: Session):
    """The child carries the PARENT's part_id, so an armed parent would have exploded
    the parent BOM against nest-run counts. The leg is gated by the same predicate."""
    user = make_user(db_session)
    parent_part, component, stock = _armed_parent_with_bom(db_session)
    parent = make_wo(db_session, parent_part)
    child = make_wo(
        db_session,
        parent_part,
        quantity_complete=40,
        work_order_type=WorkOrderType.LASER_CUTTING.value,
        parent=parent,
    )

    _apply(db_session, child, user)

    assert backflush_issues(db_session, child.id) == [], "no parent-BOM backflush on the child"
    assert float(db_session.get(InventoryItem, stock.id).quantity_on_hand) == 100.0, "component stock untouched"
    assert fg_receipts(db_session, child.id) == []


def test_parent_completion_still_books_fg_and_backflush(db_session: Session):
    """Positive control: the PARENT's own completion keeps both effects -- the skip is
    scoped to the nest-dispatch shape, not to the laser flow's parts."""
    user = make_user(db_session)
    parent_part, component, stock = _armed_parent_with_bom(db_session)
    parent = make_wo(db_session, parent_part, quantity_complete=10)

    _apply(db_session, parent, user)

    assert len(fg_receipts(db_session, parent.id)) == 1
    assert fg_on_hand(db_session, parent_part.id) == 10.0
    issues = backflush_issues(db_session, parent.id)
    assert sum(float(t.quantity) for t in issues) == -20.0, "10 units x qty 2 component demand"
    assert float(db_session.get(InventoryItem, stock.id).quantity_on_hand) == 80.0


def test_plain_production_wo_for_a_laser_cut_part_keeps_its_receipt(db_session: Session):
    """A non-nest WO (work_order_type='production') for a real part must keep its FG
    receipt -- the predicate keys on the WO type, never on the routing."""
    user = make_user(db_session)
    part = make_part(db_session)
    wo = make_wo(db_session, part, quantity_complete=3)

    _apply(db_session, wo, user)

    assert len(fg_receipts(db_session, wo.id)) == 1
    assert fg_on_hand(db_session, part.id) == 3.0


def test_partless_standalone_skips_quietly_at_debug_not_warning(db_session: Session, caplog):
    """The old path warned 'part not found' on every part-less standalone completion --
    expected behavior is not an alarm (finding laser-nest-flow/info)."""
    user = make_user(db_session)
    standalone = make_wo(
        db_session,
        None,
        quantity_complete=6,
        work_order_type=WorkOrderType.LASER_CUTTING.value,
    )

    with caplog.at_level(logging.DEBUG, logger="app.services.completion_inventory_service"):
        _apply(db_session, standalone, user)

    assert fg_receipts(db_session, standalone.id) == []
    service_records = [r for r in caplog.records if r.name == "app.services.completion_inventory_service"]
    assert all(
        r.levelno < logging.WARNING for r in service_records
    ), "the nest-dispatch skip is expected behavior and must not warn: " + "; ".join(
        r.getMessage() for r in service_records if r.levelno >= logging.WARNING
    )
    assert any(
        "skipped for laser nest-dispatch" in r.getMessage() for r in service_records if r.levelno == logging.DEBUG
    )
