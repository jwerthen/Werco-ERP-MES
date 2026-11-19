"""Owned property and automatic consumption (AS9100D 8.5.3 x invariant 6).

Customer- and supplier-owned stock is physically on the rack but is not the shop's to spend.
The rule (migration 129) is narrow, and this file pins each half of it:

* **An UNPINNED draw never takes owned stock.** The tie engine's FIFO and the BOM backflush
  both walk past a customer's or supplier's lot exactly as they walk past a held one, and a
  part whose only stock is owned draws NOTHING from it and records the usual shortage
  (the shortfall lands on the usual company placeholder row, never on the owned lot).
* **A PIN still consumes it.** A tie pinned to an owned lot is a person's lot-directed
  decision, and it draws that lot.
* **Every display agrees with the draw** -- the tie view's on-hand, the backflush preview,
  material readiness, the sheet-stock matcher and Hank's same-part stock read the same
  automatic-draw predicate, so none promises owned stock the engine skips.
* **Company stock is byte-identical.** An untied work order, a company-only tie spilling
  into a shortage and an armed backflush over company stock are pinned row for row: exact
  ledger rows, exact audit action sequences, exact event counts.
* **Combine** refuses to fold one owner's material onto another owner's target row in the
  same slot -- with the same words in the preview and the 409 -- and its open-tie cover
  counts only the stock an unpinned tie could actually draw.

Lot-ordering tests here follow THE FIFO TRAP rule in ``test_backflush_lot_policy.py``:
explicit ``received_date`` values, with the older lot inserted second, so an id-ordered draw
and a FIFO draw disagree. Every name is invented.
"""

from datetime import date
from typing import Dict, List, Optional, Tuple

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.ledger_filter import BACKFLUSH_REFERENCE_TYPE, OPERATION_REFERENCE_TYPE, WORK_ORDER_REFERENCE_TYPE
from app.models.customer import Customer
from app.models.inventory import InventoryItem, InventoryTransaction, TransactionType
from app.models.operational_event import OperationalEvent
from app.models.part import Part
from app.models.purchasing import POStatus, PurchaseOrder, PurchaseOrderLine, Vendor
from app.models.user import User
from app.models.work_order import WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.models.work_order_material import AllocationSource, AllocationStatus, WorkOrderMaterialAllocation
from app.services.audit_service import AuditService
from app.services.completion_inventory_service import (
    BACKFLUSH_SHORTAGE_AUDIT_ACTION,
    BACKFLUSH_SHORTAGE_EVENT_TYPE,
    FINISHED_GOODS_LOCATION,
    apply_operation_completion_inventory_effects,
    preview_backflush_for_work_order,
)
from app.services.material_consumption_service import (
    ALLOCATION_SHORTAGE_AUDIT_ACTION,
    ALLOCATION_SHORTAGE_EVENT_TYPE,
    HELD_MATERIAL_CONSUMED_AUDIT_ACTION,
)
from app.services.material_readiness_service import material_readiness
from app.services.material_tie_view import tie_views_for_operations
from app.services.sheet_stock_matcher import _load_on_hand
from tests.api.test_backflush_lot_policy import (
    COMPANY_A,
    add_bom_item,
    audit_rows,
    backflush_job,
    days,
    headers_for,
    make_bom,
    make_lot,
    make_op,
    make_part,
    make_user,
    make_wo,
    make_work_center,
    run_effects,
    tie,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _customer(db: Session) -> Customer:
    n = _next()
    customer = Customer(name=f"Invented Rotor Labs {n}", code=f"IRL{n:04d}", company_id=COMPANY_A)
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return customer


def _vendor(db: Session) -> Vendor:
    n = _next()
    vendor = Vendor(name=f"Invented Consignment Metals {n}", code=f"ICM{n:04d}", company_id=COMPANY_A)
    db.add(vendor)
    db.commit()
    db.refresh(vendor)
    return vendor


def _own(db: Session, item: InventoryItem, kind: str) -> InventoryItem:
    """Re-own ``item`` as ``kind`` ('customer' / 'supplier'), creating the owner."""
    if kind == "customer":
        item.ownership, item.owner_customer_id = "customer", _customer(db).id
    elif kind == "supplier":
        item.ownership, item.owner_vendor_id = "supplier", _vendor(db).id
    else:
        raise AssertionError(kind)
    db.commit()
    db.refresh(item)
    return item


def _owned_lot(db: Session, part: Part, kind: str, *, qty: float = 10.0, lot: str = "OWNED-1", received=None):
    return _own(db, make_lot(db, part, qty=qty, lot=lot, received_date=received or days(1)), kind)


def _complete_operation(db: Session, wo: WorkOrder, op: WorkOrderOperation, user: User) -> None:
    apply_operation_completion_inventory_effects(
        db, wo, op, user_id=user.id, company_id=COMPANY_A, audit=AuditService(db, user)
    )
    db.commit()


def _txns(db: Session) -> List[InventoryTransaction]:
    db.expire_all()
    return db.query(InventoryTransaction).order_by(InventoryTransaction.id).all()


def _ledger(rows: List[InventoryTransaction]) -> List[tuple]:
    """The parts of a ledger row the invariants are about."""
    return [
        (
            row.transaction_type,
            row.inventory_item_id,
            float(row.quantity),
            row.reference_type,
            row.allocation_id,
            row.lot_number,
        )
        for row in rows
    ]


def _audit_actions(db: Session) -> List[Tuple[str, str]]:
    from app.models.audit_log import AuditLog

    db.expire_all()
    return [(row.action, row.resource_type) for row in db.query(AuditLog).order_by(AuditLog.id)]


def _event_types(db: Session) -> List[str]:
    db.expire_all()
    return [row.event_type for row in db.query(OperationalEvent).order_by(OperationalEvent.id)]


def _qty(db: Session, item: InventoryItem) -> float:
    db.expire_all()
    return float(db.get(InventoryItem, item.id).quantity_on_hand)


def _placeholder(db: Session, part: Part, *, exclude: Tuple[int, ...]) -> InventoryItem:
    db.expire_all()
    return db.query(InventoryItem).filter(InventoryItem.part_id == part.id, InventoryItem.id.notin_(exclude)).one()


def _tie_job(db: Session, material: Part, *, demand: float, pinned: Optional[InventoryItem] = None):
    """A work order with one completed operation carrying an operation-scoped tie."""
    fg = make_part(db)
    wo = make_wo(db, fg, quantity_ordered=demand, quantity_complete=demand)
    op = make_op(db, wo, make_work_center(db), quantity_complete=demand)
    allocation = tie(db, wo, material, operation=op, qty_per_run=1.0, qty_planned=demand, pinned=pinned)
    return wo, op, allocation


# ===========================================================================
# 1. The unpinned draw skips owned stock and records the usual shortage
# ===========================================================================


@pytest.mark.parametrize("kind", ["customer", "supplier"])
def test_unpinned_tie_over_only_owned_stock_draws_nothing_from_it(db_session: Session, kind):
    user = make_user(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    owned = _owned_lot(db_session, sheet, kind)
    wo, op, allocation = _tie_job(db_session, sheet, demand=3)

    _complete_operation(db_session, wo, op, user)

    assert _qty(db_session, owned) == 10.0, "the owned lot is untouched"
    placeholder = _placeholder(db_session, sheet, exclude=(owned.id,))
    assert (placeholder.ownership, placeholder.owner_customer_id, placeholder.owner_vendor_id) == (
        "company",
        None,
        None,
    ), "the shortfall anchors on a COMPANY placeholder, never the owned lot"
    assert (placeholder.lot_number, placeholder.location, float(placeholder.quantity_on_hand)) == (
        None,
        FINISHED_GOODS_LOCATION,
        -3.0,
    )
    assert _ledger(_txns(db_session)) == [
        (TransactionType.ISSUE, placeholder.id, -3.0, OPERATION_REFERENCE_TYPE, allocation.id, None),
    ]
    assert _txns(db_session)[0].notes.endswith("(SHORT 3.0)")
    [shortage] = audit_rows(db_session, ALLOCATION_SHORTAGE_AUDIT_ACTION)
    assert (shortage.extra_data["available_quantity"], shortage.extra_data["shortfall"]) == (0.0, 3.0)
    assert _event_types(db_session) == [ALLOCATION_SHORTAGE_EVENT_TYPE]
    assert db_session.get(WorkOrderMaterialAllocation, allocation.id).qty_consumed == 3.0


@pytest.mark.parametrize("kind", ["customer", "supplier"])
def test_unpinned_tie_walks_past_an_older_owned_lot_to_company_stock(db_session: Session, kind):
    """FIFO would take the owned lot first (it is the OLDEST); the automatic draw must skip
    it, take the company lot, and anchor the shortfall on that company lot."""
    user = make_user(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    company_lot = make_lot(db_session, sheet, qty=2, lot="CO-NEWER", received_date=days(5))
    owned = _owned_lot(db_session, sheet, kind, qty=10, lot="OWNED-OLDEST", received=days(1))
    assert owned.id > company_lot.id, "fixture: the older lot carries the higher id (THE FIFO TRAP)"
    wo, op, allocation = _tie_job(db_session, sheet, demand=3)

    _complete_operation(db_session, wo, op, user)

    assert _ledger(_txns(db_session)) == [
        (TransactionType.ISSUE, company_lot.id, -2.0, OPERATION_REFERENCE_TYPE, allocation.id, "CO-NEWER"),
        (TransactionType.ISSUE, company_lot.id, -1.0, OPERATION_REFERENCE_TYPE, allocation.id, "CO-NEWER"),
    ]
    assert _qty(db_session, owned) == 10.0
    assert _qty(db_session, company_lot) == -1.0
    [shortage] = audit_rows(db_session, ALLOCATION_SHORTAGE_AUDIT_ACTION)
    assert (shortage.extra_data["available_quantity"], shortage.extra_data["shortfall"]) == (2.0, 1.0)


@pytest.mark.parametrize("kind", ["customer", "supplier"])
def test_unpinned_backflush_over_only_owned_stock_draws_nothing_from_it(db_session: Session, kind):
    user = make_user(db_session)
    component = make_part(db_session, part_type="purchased")
    owned = _owned_lot(db_session, component, kind)
    wo = backflush_job(db_session, component=component, qty_per_unit=1.0, quantity_complete=5.0)

    run_effects(db_session, wo, user)

    assert _qty(db_session, owned) == 10.0
    placeholder = _placeholder(db_session, component, exclude=(owned.id,))
    assert placeholder.ownership == "company" and placeholder.lot_number is None
    backflush = [row for row in _txns(db_session) if row.reference_type == BACKFLUSH_REFERENCE_TYPE]
    assert _ledger(backflush) == [(TransactionType.ISSUE, placeholder.id, -5.0, BACKFLUSH_REFERENCE_TYPE, None, None)]
    assert not [row for row in _txns(db_session) if row.inventory_item_id == owned.id]
    [shortage] = audit_rows(db_session, BACKFLUSH_SHORTAGE_AUDIT_ACTION)
    assert (shortage.extra_data["available_quantity"], shortage.extra_data["shortfall"]) == (0.0, 5.0)
    assert _event_types(db_session) == [BACKFLUSH_SHORTAGE_EVENT_TYPE]


# ===========================================================================
# 2. A pin still consumes owned stock
# ===========================================================================


@pytest.mark.parametrize("kind", ["customer", "supplier"])
def test_tie_pinned_to_an_owned_lot_consumes_it_over_older_company_stock(db_session: Session, kind):
    user = make_user(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    company_lot = make_lot(db_session, sheet, qty=50, lot="CO-OLDEST", received_date=days(1))
    owned = _owned_lot(db_session, sheet, kind, qty=10, lot="OWNED-PIN", received=days(6))
    wo, op, allocation = _tie_job(db_session, sheet, demand=3, pinned=owned)

    _complete_operation(db_session, wo, op, user)

    assert _ledger(_txns(db_session)) == [
        (TransactionType.ISSUE, owned.id, -3.0, OPERATION_REFERENCE_TYPE, allocation.id, "OWNED-PIN"),
    ]
    assert _qty(db_session, owned) == 7.0
    assert _qty(db_session, company_lot) == 50.0
    assert audit_rows(db_session, ALLOCATION_SHORTAGE_AUDIT_ACTION) == []
    assert audit_rows(db_session, HELD_MATERIAL_CONSUMED_AUDIT_ACTION) == [], "owned is not held"
    assert _event_types(db_session) == []


@pytest.mark.parametrize("kind", ["customer", "supplier"])
def test_work_order_tie_pinned_to_an_owned_lot_consumes_it_through_the_backflush(db_session: Session, kind):
    """A WORK-ORDER-scoped tie is the backflush's leg 2 and its own opt-in; pinned to an
    owned lot it draws that lot at work-order completion."""
    user = make_user(db_session)
    component = make_part(db_session, part_type="purchased")
    make_lot(db_session, component, qty=50, lot="CO-OLDEST", received_date=days(1))
    owned = _owned_lot(db_session, component, kind, qty=10, lot="OWNED-WO-PIN", received=days(6))
    fg = make_part(db_session)
    wo = make_wo(db_session, fg, quantity_ordered=3, quantity_complete=3)
    make_op(db_session, wo, make_work_center(db_session), quantity_complete=3)
    allocation = tie(db_session, wo, component, qty_planned=3, pinned=owned)

    run_effects(db_session, wo, user)

    backflush = [row for row in _txns(db_session) if row.reference_type == BACKFLUSH_REFERENCE_TYPE]
    assert _ledger(backflush) == [
        (TransactionType.ISSUE, owned.id, -3.0, BACKFLUSH_REFERENCE_TYPE, allocation.id, "OWNED-WO-PIN"),
    ]
    assert _qty(db_session, owned) == 7.0
    assert audit_rows(db_session, BACKFLUSH_SHORTAGE_AUDIT_ACTION) == []


# ===========================================================================
# 3. Every display agrees with the draw
# ===========================================================================


def _readiness_line(db: Session, wo: WorkOrder, part: Part) -> dict:
    db.expire_all()
    wo = db.get(WorkOrder, wo.id)
    job = material_readiness(db, COMPANY_A, [wo], date(2026, 11, 19))["jobs"][wo.id]
    [line] = [row for row in job["lines"] if row["part_id"] == part.id]
    return line


def _sources(line: dict) -> List[Tuple[int, float]]:
    return [(source["id"], source["quantity"]) for source in line["sources"] if source["kind"] == "stock"]


@pytest.mark.parametrize("kind", ["customer", "supplier"])
def test_unpinned_tie_displays_promise_only_what_the_engine_then_draws(db_session: Session, kind):
    user = make_user(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    company_lot = make_lot(db_session, sheet, qty=2, lot="CO-NEWER", received_date=days(5))
    owned = _owned_lot(db_session, sheet, kind, qty=10, lot="OWNED-OLDEST", received=days(1))
    wo, op, allocation = _tie_job(db_session, sheet, demand=3)

    [view] = tie_views_for_operations(db_session, company_id=COMPANY_A, operation_ids=[op.id])[op.id]
    readiness = _readiness_line(db_session, wo, sheet)
    matcher_on_hand, landed = _load_on_hand(db_session, COMPANY_A, [sheet.id])

    assert (view.on_hand, view.short_by) == (2.0, 1.0)
    assert (readiness["covered_quantity"], readiness["shortage_quantity"]) == (2.0, 1.0)
    assert _sources(readiness) == [(company_lot.id, 2.0)]
    assert (matcher_on_hand, landed) == ({sheet.id: 2.0}, True)

    _complete_operation(db_session, wo, op, user)
    drawn = [row for row in _txns(db_session) if row.allocation_id == allocation.id]
    assert {row.inventory_item_id for row in drawn} == {company_lot.id}
    assert -sum(row.quantity for row in drawn if not row.notes.endswith("(SHORT 1.0)")) == view.on_hand
    assert _qty(db_session, owned) == 10.0


@pytest.mark.parametrize("kind", ["customer", "supplier"])
def test_pinned_tie_displays_score_the_owned_lot_the_engine_then_draws(db_session: Session, kind):
    user = make_user(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    make_lot(db_session, sheet, qty=50, lot="CO-OLDEST", received_date=days(1))
    owned = _owned_lot(db_session, sheet, kind, qty=10, lot="OWNED-PIN", received=days(6))
    wo, op, allocation = _tie_job(db_session, sheet, demand=3, pinned=owned)

    [view] = tie_views_for_operations(db_session, company_id=COMPANY_A, operation_ids=[op.id])[op.id]
    readiness = _readiness_line(db_session, wo, sheet)

    assert (view.on_hand, view.short_by) == (10.0, 0.0)
    assert (readiness["covered_quantity"], readiness["shortage_quantity"]) == (3.0, 0.0)
    assert _sources(readiness) == [(owned.id, 3.0)]

    _complete_operation(db_session, wo, op, user)
    assert [(row.inventory_item_id, row.quantity) for row in _txns(db_session)] == [(owned.id, -3.0)]


@pytest.mark.parametrize("kind", ["customer", "supplier"])
def test_backflush_preview_and_readiness_match_the_backflush_draw(db_session: Session, kind):
    """The dry run names the same lots and quantities -- shortfall row included -- that the
    completion then posts; owned stock appears in neither."""
    user = make_user(db_session)
    component = make_part(db_session, part_type="purchased")
    company_lot = make_lot(db_session, component, qty=2, lot="CO-NEWER", received_date=days(5))
    owned = _owned_lot(db_session, component, kind, qty=10, lot="OWNED-OLDEST", received=days(1))
    wo = backflush_job(db_session, component=component, qty_per_unit=1.0, quantity_complete=5.0)

    preview = preview_backflush_for_work_order(db_session, wo, company_id=COMPANY_A)
    [line] = [row for row in preview.lines if row.component_part_id == component.id]
    readiness = _readiness_line(db_session, wo, component)

    assert (line.available_quantity, line.shortfall, line.shortfall_creates_placeholder) == (2.0, 3.0, False)
    previewed = [(lot.inventory_item_id, lot.quantity, lot.is_shortfall) for lot in line.lots]
    assert previewed == [(company_lot.id, 2.0, False), (company_lot.id, 3.0, True)]
    assert _sources(readiness) == [(company_lot.id, 2.0)]
    assert readiness["shortage_quantity"] == 3.0

    run_effects(db_session, wo, user)
    posted = [
        (row.inventory_item_id, -row.quantity)
        for row in _txns(db_session)
        if row.reference_type == BACKFLUSH_REFERENCE_TYPE
    ]
    assert posted == [(lot_id, quantity) for lot_id, quantity, _ in previewed]
    assert _qty(db_session, owned) == 10.0


@pytest.mark.parametrize("kind", ["customer", "supplier"])
def test_backflush_preview_over_only_owned_stock_reports_the_placeholder(db_session: Session, kind):
    component = make_part(db_session, part_type="purchased")
    _owned_lot(db_session, component, kind)
    wo = backflush_job(db_session, component=component, qty_per_unit=1.0, quantity_complete=5.0)

    preview = preview_backflush_for_work_order(db_session, wo, company_id=COMPANY_A)
    [line] = [row for row in preview.lines if row.component_part_id == component.id]

    assert (line.available_quantity, line.shortfall, line.shortfall_creates_placeholder) == (0.0, 5.0, True)
    assert line.lots == []


def test_hank_purchase_order_impact_lists_only_company_stock_as_an_alternative(
    db_session: Session, client: TestClient, auth_headers
):
    """Hank's 'same-part stock to review' must not offer a customer's lot as the shop's
    alternative stock to spend."""
    part = make_part(db_session, part_type="purchased")
    company_lot = make_lot(db_session, part, qty=4, lot="CO-ALT", received_date=days(2))
    _owned_lot(db_session, part, "customer", qty=20, lot="OWNED-ALT")
    _owned_lot(db_session, part, "supplier", qty=30, lot="CONSIGNED-ALT")
    vendor = _vendor(db_session)
    po = PurchaseOrder(company_id=COMPANY_A, po_number="OWN-IMPACT-PO", vendor_id=vendor.id, status=POStatus.SENT)
    db_session.add(po)
    db_session.flush()
    db_session.add(
        PurchaseOrderLine(
            company_id=COMPANY_A,
            purchase_order_id=po.id,
            part_id=part.id,
            line_number=1,
            quantity_ordered=10,
            quantity_received=0,
            unit_price=2,
        )
    )
    db_session.commit()

    report = client.get(f"/api/v1/hank/purchase-orders/{po.id}/impact", headers=auth_headers)

    assert report.status_code == 200, report.text
    alternatives = [row["key"] for row in report.json()["checks"] if row["key"].startswith("alternative_stock:")]
    assert alternatives == [f"alternative_stock:{company_lot.id}"]


# ===========================================================================
# 4. Company-only completions are byte-identical (invariant 6 d and the rest)
# ===========================================================================


@pytest.mark.parametrize("owned_stock_on_the_shelf", [False, True])
def test_untied_work_order_posts_only_its_finished_goods_receipt(db_session: Session, owned_stock_on_the_shelf):
    """An untied work order (BOM present, backflush OFF) moves no component stock at all --
    with or without owned stock of that component on the shelf. One RECEIVE, one audit row,
    no event."""
    user = make_user(db_session)
    component = make_part(db_session, part_type="purchased")
    company_lot = make_lot(db_session, component, qty=50, lot="A-STOCK", received_date=days(3))
    owned = _owned_lot(db_session, component, "customer") if owned_stock_on_the_shelf else None
    fg = make_part(db_session, backflush=False)
    add_bom_item(db_session, make_bom(db_session, fg), component, quantity=2.0)
    wo = make_wo(db_session, fg, quantity_ordered=5, quantity_complete=5)
    op = make_op(db_session, wo, make_work_center(db_session), quantity_complete=5)

    _complete_operation(db_session, wo, op, user)
    run_effects(db_session, wo, user)

    [receipt] = _txns(db_session)
    fg_item = db_session.get(InventoryItem, receipt.inventory_item_id)
    lot = f"LOT-{wo.work_order_number}"
    assert _ledger([receipt]) == [(TransactionType.RECEIVE, fg_item.id, 5.0, WORK_ORDER_REFERENCE_TYPE, None, lot)]
    assert (receipt.reference_id, receipt.unit_cost, receipt.total_cost, receipt.created_by) == (
        wo.id,
        5.0,
        25.0,
        user.id,
    )
    assert (fg_item.location, fg_item.lot_number, fg_item.quantity_on_hand, fg_item.ownership) == (
        FINISHED_GOODS_LOCATION,
        lot,
        5.0,
        "company",
    )
    assert _audit_actions(db_session) == [("CREATE", "inventory")]
    assert _event_types(db_session) == []
    assert _qty(db_session, company_lot) == 50.0
    if owned is not None:
        assert _qty(db_session, owned) == 10.0


def test_company_only_tie_spilling_into_a_shortage_is_unchanged(db_session: Session):
    """FIFO over two company lots into a shortfall: the exact rows the engine has always posted."""
    user = make_user(db_session)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    newer = make_lot(db_session, sheet, qty=3, lot="B-NEWER", received_date=days(5))
    older = make_lot(db_session, sheet, qty=4, lot="B-OLDER", received_date=days(1))
    wo, op, allocation = _tie_job(db_session, sheet, demand=9)

    _complete_operation(db_session, wo, op, user)
    run_effects(db_session, wo, user)

    txns = _txns(db_session)
    fg_receipt = txns[-1]
    assert _ledger(txns) == [
        (TransactionType.ISSUE, older.id, -4.0, OPERATION_REFERENCE_TYPE, allocation.id, "B-OLDER"),
        (TransactionType.ISSUE, newer.id, -3.0, OPERATION_REFERENCE_TYPE, allocation.id, "B-NEWER"),
        (TransactionType.ISSUE, newer.id, -2.0, OPERATION_REFERENCE_TYPE, allocation.id, "B-NEWER"),
        (
            TransactionType.RECEIVE,
            fg_receipt.inventory_item_id,
            9.0,
            WORK_ORDER_REFERENCE_TYPE,
            None,
            f"LOT-{wo.work_order_number}",
        ),
    ]
    stem = (
        f"Material consumption for work order {wo.work_order_number} operation OP10: "
        "9.0 complete + 0.0 scrapped runs @ 1.0 sheets/run"
    )
    assert [row.notes for row in txns[:3]] == [stem, stem, f"{stem} (SHORT 2.0)"]
    assert {(row.reference_id, row.reference_number, row.from_location) for row in txns[:3]} == {
        (op.id, wo.work_order_number, "RAW-A")
    }
    assert (_qty(db_session, older), _qty(db_session, newer)) == (0.0, -2.0)
    assert _audit_actions(db_session) == [
        ("CREATE", "inventory"),
        ("UPDATE", "inventory"),
        ("CREATE", "inventory"),
        ("UPDATE", "inventory"),
        ("CREATE", "inventory"),
        ("UPDATE", "inventory"),
        (ALLOCATION_SHORTAGE_AUDIT_ACTION, "inventory"),
        ("UPDATE", "work_order_material_allocation"),
        ("CREATE", "inventory"),
    ]
    [shortage] = audit_rows(db_session, ALLOCATION_SHORTAGE_AUDIT_ACTION)
    expected = {
        "allocation_id": allocation.id,
        "available_quantity": 7.0,
        "consumed_lot": "B-NEWER",
        "held_lot_numbers": [],
        "held_quantity_skipped": 0,
        "pinned_lot": None,
        "required_quantity": 9.0,
        "shortfall": 2.0,
    }
    assert {key: shortage.extra_data.get(key) for key in expected} == expected
    assert _event_types(db_session) == [ALLOCATION_SHORTAGE_EVENT_TYPE]
    assert db_session.get(WorkOrderMaterialAllocation, allocation.id).qty_consumed == 9.0


def test_company_only_armed_backflush_into_a_shortage_is_unchanged(db_session: Session):
    user = make_user(db_session)
    component = make_part(db_session, part_type="purchased")
    stock = make_lot(db_session, component, qty=2, lot="C-1", received_date=days(2))
    wo = backflush_job(db_session, component=component, qty_per_unit=1.0, quantity_complete=5.0)

    run_effects(db_session, wo, user)

    txns = _txns(db_session)
    assert _ledger(txns) == [
        (
            TransactionType.RECEIVE,
            txns[0].inventory_item_id,
            5.0,
            WORK_ORDER_REFERENCE_TYPE,
            None,
            f"LOT-{wo.work_order_number}",
        ),
        (TransactionType.ISSUE, stock.id, -2.0, BACKFLUSH_REFERENCE_TYPE, None, "C-1"),
        (TransactionType.ISSUE, stock.id, -3.0, BACKFLUSH_REFERENCE_TYPE, None, "C-1"),
    ]
    assert [row.notes for row in txns[1:]] == [
        f"Backflush consumption for work order {wo.work_order_number}",
        f"Backflush consumption for work order {wo.work_order_number} (SHORT 3.0)",
    ]
    assert [(row.total_cost, row.reference_id) for row in txns[1:]] == [(4.0, wo.id), (6.0, wo.id)]
    assert _qty(db_session, stock) == -3.0
    assert _audit_actions(db_session) == [
        ("CREATE", "inventory"),
        ("CREATE", "inventory"),
        ("UPDATE", "inventory"),
        ("CREATE", "inventory"),
        ("UPDATE", "inventory"),
        (BACKFLUSH_SHORTAGE_AUDIT_ACTION, "inventory"),
    ]
    [shortage] = audit_rows(db_session, BACKFLUSH_SHORTAGE_AUDIT_ACTION)
    expected = {
        "available_quantity": 2.0,
        "consumed_lot": "C-1",
        "required_quantity": 5.0,
        "shortfall": 3.0,
        "held_quantity_skipped": 0,
        "pinned_lot": None,
    }
    assert {key: shortage.extra_data.get(key) for key in expected} == expected
    assert _event_types(db_session) == [BACKFLUSH_SHORTAGE_EVENT_TYPE]


# ===========================================================================
# 5. Combine: ownership in the landing key, and the open-tie cover
# ===========================================================================

PREVIEW_URL = "/api/v1/inventory/combine/preview"
COMBINE_URL = "/api/v1/inventory/combine"


def _combine_part(db: Session) -> Part:
    n = _next()
    part = Part(
        part_number=f"OWN-CMB-{n:04d}",
        name=f"Invented bar stock {n}",
        part_type="raw_material",
        unit_of_measure="each",
        standard_cost=1.0,
        is_active=True,
        company_id=COMPANY_A,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def _preview(client: TestClient, headers: dict, source: Part, target: Part, quantity: Optional[float] = None):
    body = {"source_part_id": source.id, "target_part_id": target.id}
    if quantity is not None:
        body["quantity"] = quantity
    response = client.post(PREVIEW_URL, json=body, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _combine(client: TestClient, headers: dict, source: Part, target: Part, quantity: float):
    return client.post(
        COMBINE_URL,
        json={
            "source_part_id": source.id,
            "target_part_id": target.id,
            "quantity": quantity,
            "reason": "Numbering recut put one bar on two part numbers",
            "expected_source_part_number": source.part_number,
            "expected_target_part_number": target.part_number,
            "acknowledge_flagged_part_ids": [],
            "deactivate_source": False,
        },
        headers=headers,
    )


def _blockers(preview: dict) -> Dict[str, str]:
    return {blocker["code"]: blocker["detail"] for blocker in preview["blockers"]}


def _owner_rows(db: Session, part: Part) -> Dict[Tuple[str, Optional[str], str], float]:
    db.expire_all()
    return {
        (row.location, row.lot_number, row.ownership): float(row.quantity_on_hand)
        for row in db.query(InventoryItem).filter(InventoryItem.part_id == part.id)
    }


def _snapshot(db: Session) -> Dict[str, int]:
    db.expire_all()
    from app.models.audit_log import AuditLog
    from app.models.inventory_combine import InventoryCombine

    return {
        "items": db.query(InventoryItem).count(),
        "txns": db.query(InventoryTransaction).count(),
        "audit": db.query(AuditLog).count(),
        "combines": db.query(InventoryCombine).count(),
        "events": db.query(OperationalEvent).count(),
    }


@pytest.mark.parametrize("direction", ["owned_onto_company", "company_onto_owned"])
def test_combine_refusal_across_owners_has_the_same_words_in_preview_and_write(
    db_session: Session, client: TestClient, direction
):
    headers = headers_for(make_user(db_session))
    source, target = _combine_part(db_session), _combine_part(db_session)
    source_lot = make_lot(db_session, source, qty=6, lot="L9", received_date=days(2), location="RAW-A")
    target_lot = make_lot(db_session, target, qty=3, lot="L9", received_date=days(3), location="RAW-A")
    _own(db_session, source_lot if direction == "owned_onto_company" else target_lot, "customer")
    before = _snapshot(db_session)

    preview = _preview(client, headers, source, target, 6)
    write = _combine(client, headers, source, target, 6)

    blockers = _blockers(preview)
    assert "target_ownership_mismatch" in blockers, blockers
    assert write.status_code == 409, write.text
    assert write.json()["detail"] == blockers["target_ownership_mismatch"]
    detail = blockers["target_ownership_mismatch"]
    assert "RAW-A lot L9" in detail and "customer-owned" in detail and "company-owned" in detail
    assert _snapshot(db_session) == before, "a refused combine writes nothing"


def test_combine_carries_the_owner_onto_a_new_target_row_and_merges_with_the_same_owner(
    db_session: Session, client: TestClient
):
    """The refusal fires ONLY when the slot holds a target row of a DIFFERENT owner and none of
    the material's own. An empty slot gets a new row carrying the source's owner; a slot
    with a same-owner row merges into it, even when a different-owner row sits there too;
    a different-owner row in ANOTHER slot is irrelevant."""
    headers = headers_for(make_user(db_session))
    source, target = _combine_part(db_session), _combine_part(db_session)
    customer = _customer(db_session)

    def owned_source(qty: float, lot: str, location: str) -> InventoryItem:
        item = make_lot(db_session, source, qty=qty, lot=lot, received_date=days(1), location=location)
        item.ownership, item.owner_customer_id = "customer", customer.id
        db_session.commit()
        return item

    owned_source(4, "NEW-SLOT", "RAW-A")  # target has nothing at (RAW-A, NEW-SLOT)
    owned_source(3, "SAME", "RAW-A")  # target holds a same-owner AND a company row there
    company_elsewhere = make_lot(db_session, target, qty=9, lot="NEW-SLOT", received_date=days(1), location="RAW-B")
    same_owner = make_lot(db_session, target, qty=2, lot="SAME", received_date=days(1), location="RAW-A")
    same_owner.ownership, same_owner.owner_customer_id = "customer", customer.id
    db_session.commit()
    company_same_slot = make_lot(db_session, target, qty=5, lot="SAME", received_date=days(1), location="RAW-A")

    preview = _preview(client, headers, source, target, 7)
    write = _combine(client, headers, source, target, 7)

    assert "target_ownership_mismatch" not in _blockers(preview)
    assert write.status_code == 200, write.text
    assert _owner_rows(db_session, target) == {
        ("RAW-A", "NEW-SLOT", "customer"): 4.0,  # created, carrying the owner
        ("RAW-A", "SAME", "customer"): 5.0,  # merged into the same-owner row
        ("RAW-A", "SAME", "company"): 5.0,  # untouched
        ("RAW-B", "NEW-SLOT", "company"): 9.0,  # another slot: irrelevant
    }
    db_session.expire_all()
    created = (
        db_session.query(InventoryItem)
        .filter(
            InventoryItem.part_id == target.id,
            InventoryItem.lot_number == "NEW-SLOT",
            InventoryItem.location == "RAW-A",
        )
        .one()
    )
    assert (created.ownership, created.owner_customer_id, created.owner_vendor_id) == ("customer", customer.id, None)
    assert _qty(db_session, company_elsewhere) == 9.0 and _qty(db_session, company_same_slot) == 5.0


def _open_tie(db: Session, part: Part, qty_planned: float) -> WorkOrderMaterialAllocation:
    n = _next()
    wo = WorkOrder(
        work_order_number=f"OWN-CMB-WO-{n:04d}",
        part_id=part.id,
        quantity_ordered=10,
        status=WorkOrderStatus.RELEASED,
        company_id=COMPANY_A,
    )
    db.add(wo)
    db.commit()
    allocation = WorkOrderMaterialAllocation(
        company_id=COMPANY_A,
        work_order_id=wo.id,
        work_order_operation_id=None,
        part_id=part.id,
        source=AllocationSource.MANUAL,
        status=AllocationStatus.OPEN,
        qty_per_run=None,
        qty_planned=qty_planned,
        unit_of_measure="each",
        qty_consumed=0.0,
    )
    db.add(allocation)
    db.commit()
    return allocation


def test_combine_open_tie_cover_counts_only_company_stock(db_session: Session, client: TestClient):
    """An open unpinned tie draws company stock only, so owned stock left behind covers no job.

    Company 10 then customer-owned 50 (drain order = ascending id); a job needs 8. Moving 5
    leaves 55 on hand but only 5 the job could draw: refused. Pre-129 arithmetic
    (60 - 5 >= 8) would have stranded the job. Moving 2 leaves exactly 8: allowed.
    """
    headers = headers_for(make_user(db_session))
    source, target = _combine_part(db_session), _combine_part(db_session)
    company_lot = make_lot(db_session, source, qty=10, lot="CO-COVER", received_date=days(1))
    owned = _owned_lot(db_session, source, "customer", qty=50, lot="OWNED-COVER", received=days(2))
    assert company_lot.id < owned.id, "fixture: the company row drains first"
    allocation = _open_tie(db_session, source, 8)

    preview = _preview(client, headers, source, target)
    assert preview["source"]["eligible_available"] == 60.0
    assert preview["reserved_quantity"] == 8.0
    assert preview["max_combinable_quantity"] == 2.0, "only 10 - 8 of the company row may move"

    refused = _combine(client, headers, source, target, 5)
    assert refused.status_code == 409, refused.text
    work_order_number = db_session.get(WorkOrder, allocation.work_order_id).work_order_number
    assert work_order_number in refused.json()["detail"]
    assert _qty(db_session, company_lot) == 10.0

    allowed = _combine(client, headers, source, target, preview["max_combinable_quantity"])
    assert allowed.status_code == 200, allowed.text
    assert (_qty(db_session, company_lot), _qty(db_session, owned)) == (8.0, 50.0)


def test_combine_open_tie_cover_lets_owned_stock_move_freely(db_session: Session, client: TestClient):
    """The mirror: owned rows draining FIRST move freely (they never covered the job), and
    the company remainder still has to cover it."""
    headers = headers_for(make_user(db_session))
    source, target = _combine_part(db_session), _combine_part(db_session)
    owned = _owned_lot(db_session, source, "supplier", qty=50, lot="OWNED-FIRST", received=days(1))
    company_lot = make_lot(db_session, source, qty=10, lot="CO-SECOND", received_date=days(2))
    assert owned.id < company_lot.id, "fixture: the owned row drains first"
    _open_tie(db_session, source, 8)

    preview = _preview(client, headers, source, target)
    assert preview["max_combinable_quantity"] == 52.0, "all 50 owned plus 10 - 8 of the company row"

    assert _combine(client, headers, source, target, 53).status_code == 409
    allowed = _combine(client, headers, source, target, 52)
    assert allowed.status_code == 200, allowed.text
    assert (_qty(db_session, owned), _qty(db_session, company_lot)) == (0.0, 8.0)
    target_owners = {
        (row.ownership, float(row.quantity_on_hand))
        for row in db_session.query(InventoryItem).filter(InventoryItem.part_id == target.id)
    }
    assert target_owners == {("supplier", 50.0), ("company", 2.0)}


@pytest.mark.parametrize("drain_order", ["company_first", "owned_first"])
def test_combine_offers_nothing_when_company_stock_is_already_short_of_open_ties(
    db_session: Session, client: TestClient, drain_order
):
    """Company 5 against an open unpinned need of 8: the job is already short, so EVERY
    combine leaves the company remainder below the demand and the write refuses it -- even
    one that moves only owned stock. The preview must offer 0, the number the write accepts,
    not the owned rows it would then refuse."""
    headers = headers_for(make_user(db_session))
    source, target = _combine_part(db_session), _combine_part(db_session)
    if drain_order == "company_first":
        make_lot(db_session, source, qty=5, lot="CO-SHORT", received_date=days(1))
        _owned_lot(db_session, source, "customer", qty=50, lot="OWNED-SHORT", received=days(2))
    else:
        _owned_lot(db_session, source, "customer", qty=50, lot="OWNED-SHORT", received=days(1))
        make_lot(db_session, source, qty=5, lot="CO-SHORT", received_date=days(2))
    _open_tie(db_session, source, 8)
    before = _snapshot(db_session)

    preview = _preview(client, headers, source, target)

    assert preview["max_combinable_quantity"] == 0.0
    refused = _combine(client, headers, source, target, 1)
    assert refused.status_code == 409, refused.text
    assert _snapshot(db_session) == before


# ===========================================================================
# 6. Consumable checkout: owned rows never cover open unpinned ties
# ===========================================================================


def _crib_stock(db: Session) -> Tuple[Part, InventoryItem, InventoryItem]:
    from app.models.inventory import InventoryLocation

    n = _next()
    part = Part(
        company_id=COMPANY_A,
        part_number=f"OWN-DISC-{n:04d}",
        name=f"Invented cutoff wheel {n}",
        part_type="consumable",
        unit_of_measure="each",
        reorder_point=0,
        is_active=True,
    )
    location = InventoryLocation(
        company_id=COMPANY_A, code=f"CRIB-{n:04d}", warehouse="MAIN", is_active=True, is_pickable=True
    )
    db.add_all([part, location])
    db.commit()
    company_row = make_lot(db, part, qty=10, lot="CRIB-CO", received_date=days(1), location=location.code)
    owned = _owned_lot(db, part, "customer", qty=20, lot="CRIB-OWNED", received=days(2))
    owned.location = location.code
    db.commit()
    return part, company_row, owned


def _crib_checkout(client: TestClient, headers: dict, item: InventoryItem, employee: User, quantity: float):
    from uuid import uuid4

    return client.post(
        "/api/v1/consumables/checkouts",
        json={
            "inventory_item_id": item.id,
            "employee_id": employee.id,
            "quantity": quantity,
            "notes": "Weld bay",
            "request_id": str(uuid4()),
        },
        headers=headers,
    )


def test_consumable_checkout_caps_company_stock_by_open_ties_and_owned_stock_by_itself(
    db_session: Session, client: TestClient, auth_headers, operator_user
):
    """Company 10 + customer-owned 20 against an open UNPINNED need of 8. Only company stock can
    cover that need (the unpinned draw never takes an owned lot), so the crib may take just
    10 - 8 = 2 of the company row -- pre-129 it would have offered all 10 and stranded the job.
    The owned row is capped by its own free quantity alone."""
    part, company_row, owned = _crib_stock(db_session)
    _open_tie(db_session, part, 8)

    listed = client.get("/api/v1/consumables/stock", headers=auth_headers)
    assert listed.status_code == 200, listed.text
    available = {row["inventory_item_id"]: row["quantity_available"] for row in listed.json()["items"]}
    assert (available[company_row.id], available[owned.id]) == (2.0, 20.0)

    too_much = _crib_checkout(client, auth_headers, company_row, operator_user, 3)
    assert too_much.status_code == 409, too_much.text
    assert too_much.json()["detail"] == "Insufficient unreserved stock. Available: 2"
    assert _crib_checkout(client, auth_headers, company_row, operator_user, 2).status_code == 201
    assert _crib_checkout(client, auth_headers, owned, operator_user, 20).status_code == 201
    assert (_qty(db_session, company_row), _qty(db_session, owned)) == (8.0, 0.0)


def test_consumable_checkout_without_owned_stock_keeps_the_pre_129_cap(
    db_session: Session, client: TestClient, auth_headers, operator_user
):
    part, company_row, owned = _crib_stock(db_session)
    owned.ownership, owned.owner_customer_id = "company", None  # all company stock now
    db_session.commit()
    _open_tie(db_session, part, 8)

    listed = client.get("/api/v1/consumables/stock", headers=auth_headers).json()["items"]
    available = {row["inventory_item_id"]: row["quantity_available"] for row in listed}

    assert (available[company_row.id], available[owned.id]) == (10.0, 20.0), "30 - 8 = 22 caps neither row"
