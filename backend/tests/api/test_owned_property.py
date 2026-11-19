"""Customer- and supplier-owned property (AS9100D 8.5.3, migration 129) -- the API surface.

A stock row or a tool is owned by the company (the default, and what every pre-129 row is),
by one customer, or by one supplier. This file pins everything about that EXCEPT what
automatic consumption does with owned stock (``test_owned_property_consumption.py``):

* **Merge keys.** Every write that adds into an existing stock row matched on part,
  location and lot also matches the owner, or it creates a second row: ``/inventory/receive``,
  the PO receipt (single line and delivery batch), the transfer destination and the
  finished-goods receipt. Owned stock never lands in a company row, nor the reverse.
* **Owner resolution.** An owner reference resolves tenant-scoped BEFORE the first write: a
  live customer / a non-deleted vendor (an inactive vendor is fine) of THIS company, 404
  otherwise; a kind/owner mismatch is 422. Every refusal leaves nothing written.
* **The change-of-ownership verb** (``POST /inventory/items/{id}/ownership``): Admin /
  Manager / Supervisor, a required reason, 409 on an inactive / empty row or a no-op, ONE
  required audit row carrying old -> new and the reason (503 and nothing saved when it
  cannot be written), and no ledger row.
* **Reads.** List filters and ``owner_name`` (tenant-scoped, through tombstones), the
  summary's ``owned_by_others_on_hand``, valuation that counts company stock only, and the
  ownership column on the export and the report builder.
* **Tools**, the **customer hard-delete** refusal, and **Hank's receive_delivery**, which
  stays company-only because its line schema has no ownership fields at all.

Every customer, vendor and part name here is invented.
"""

import csv
import hashlib
import io
import json
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Dict, Iterator, List, Optional, Tuple
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import Insert, event, update
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.customer import Customer
from app.models.inventory import InventoryItem, InventoryLocation, InventoryTransaction, TransactionType
from app.models.operational_event import OperationalEvent
from app.models.part import Part
from app.models.purchasing import (
    POReceipt,
    POStatus,
    PurchaseOrder,
    PurchaseOrderLine,
    ReceivingDeliveryBatch,
    Vendor,
)
from app.models.shipping import Shipment
from app.models.tool_management import Tool
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder, WorkOrderStatus
from app.models.work_order_material import AllocationSource, AllocationStatus, WorkOrderMaterialAllocation
from app.schemas.hank_operations import HankReceiptLine, ReceiveDeliveryInput
from app.schemas.purchasing import ReceiptLineFields
from app.services.analytics_service import AnalyticsService
from app.services.audit_service import AuditService, AuditWriteError
from app.services.completion_inventory_service import (
    FINISHED_GOODS_LOCATION,
    SHIP_FG_MISSING_AUDIT_ACTION,
    decrement_finished_goods_for_shipment,
    receive_finished_goods_for_work_order,
)
from app.services.owned_property_service import OWNERSHIP_AUDIT_UNAVAILABLE_DETAIL
from tests.api.test_hank_tasks import execute as hank_execute
from tests.api.test_hank_tasks import propose as hank_propose

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2
FIXTURE_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"  # tokens are minted directly; never a login

RECEIVE_URL = "/api/v1/inventory/receive"
INVENTORY_URL = "/api/v1/inventory/"
TRANSFER_URL = "/api/v1/inventory/transfer"
PO_RECEIVE_URL = "/api/v1/receiving/receive"
DELIVERY_URL = "/api/v1/receiving/deliveries"
TOOLS_URL = "/api/v1/tool-management/tools/"
RECEIVING_DEFAULT_LOCATION = "RECV-01"

_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


# ---------------------------------------------------------------------------
# Fixtures (local, invented names only)
# ---------------------------------------------------------------------------


def _ensure_company(db: Session, company_id: int) -> Company:
    company = db.query(Company).filter(Company.id == company_id).first()
    if company is None:
        company = Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True)
        db.add(company)
        db.commit()
    return company


def _user(db: Session, *, role: UserRole = UserRole.ADMIN, company_id: int = COMPANY_A) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"owned-{n}@co{company_id}.test",
        employee_id=f"OWN-{n:05d}",
        first_name="Owned",
        last_name="Property",
        hashed_password=FIXTURE_PASSWORD_HASH,
        role=role,
        is_active=True,
        is_superuser=False,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _headers(user: User) -> dict:
    token = create_access_token(subject=user.id, company_id=user.company_id)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


@pytest.fixture
def admin(db_session: Session) -> User:
    return _user(db_session)


@pytest.fixture
def hdrs(admin: User) -> dict:
    return _headers(admin)


@pytest.fixture(autouse=True)
def _no_label_printing(monkeypatch):
    """PO receipts queue a label print job after commit; nothing here wants a worker."""
    monkeypatch.setattr("app.api.endpoints.receiving.enqueue_job_best_effort", Mock())


def _part(
    db: Session,
    *,
    company_id: int = COMPANY_A,
    standard_cost: float = 10.0,
    part_type: str = "purchased",
) -> Part:
    _ensure_company(db, company_id)
    n = _next()
    part = Part(
        part_number=f"OWN-P-{n:04d}",
        name=f"Owned property fixture part {n}",
        part_type=part_type,
        unit_of_measure="each",
        standard_cost=standard_cost,
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def _location(db: Session, *, company_id: int = COMPANY_A) -> InventoryLocation:
    _ensure_company(db, company_id)
    n = _next()
    location = InventoryLocation(
        code=f"OWN-LOC-{n:04d}", name=f"Rack {n}", warehouse="MAIN", is_active=True, company_id=company_id
    )
    db.add(location)
    db.commit()
    db.refresh(location)
    return location


def _customer(db: Session, *, company_id: int = COMPANY_A, deleted: bool = False) -> Customer:
    _ensure_company(db, company_id)
    n = _next()
    customer = Customer(name=f"Invented Aero Works {n}", code=f"IAW{n:04d}", company_id=company_id)
    db.add(customer)
    db.commit()
    if deleted:
        customer.soft_delete(None)
        customer.is_active = False
        db.commit()
    db.refresh(customer)
    return customer


def _vendor(db: Session, *, company_id: int = COMPANY_A, active: bool = True, deleted: bool = False) -> Vendor:
    _ensure_company(db, company_id)
    n = _next()
    vendor = Vendor(name=f"Invented Plate Supply {n}", code=f"IPS{n:04d}", is_active=active, company_id=company_id)
    db.add(vendor)
    db.commit()
    if deleted:
        vendor.soft_delete(None)
        db.commit()
    db.refresh(vendor)
    return vendor


def _owner(customer: Optional[Customer] = None, vendor: Optional[Vendor] = None) -> Dict[str, object]:
    """The three ownership columns / request fields for an owner (none = company)."""
    if customer is not None:
        return {"ownership": "customer", "owner_customer_id": customer.id, "owner_vendor_id": None}
    if vendor is not None:
        return {"ownership": "supplier", "owner_customer_id": None, "owner_vendor_id": vendor.id}
    return {"ownership": "company", "owner_customer_id": None, "owner_vendor_id": None}


def _stock(
    db: Session,
    part: Part,
    *,
    qty: float,
    lot: Optional[str],
    location: str = "RAW-A",
    customer: Optional[Customer] = None,
    vendor: Optional[Vendor] = None,
    unit_cost: float = 2.0,
    is_active: bool = True,
    company_id: Optional[int] = None,
) -> InventoryItem:
    item = InventoryItem(
        part_id=part.id,
        location=location,
        warehouse="MAIN",
        quantity_on_hand=qty,
        quantity_allocated=0.0,
        quantity_available=qty,
        lot_number=lot,
        unit_cost=unit_cost,
        received_date=datetime(2026, 3, 1, 12, 0, 0),
        status="available",
        is_active=is_active,
        company_id=company_id if company_id is not None else part.company_id,
        **_owner(customer, vendor),
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


OwnerKey = Tuple[str, Optional[int], Optional[int]]


def _key(item: InventoryItem) -> OwnerKey:
    return (item.ownership, item.owner_customer_id, item.owner_vendor_id)


def _rows_by_owner(db: Session, part: Part, *, location: Optional[str] = None) -> Dict[OwnerKey, float]:
    """``{(ownership, customer id, vendor id): on hand}`` for a part (optionally one location).

    A dict keyed by owner, so a merge across owners (one row instead of two) and a split
    within one owner (two rows instead of one) both fail the equality check.
    """
    db.expire_all()
    query = db.query(InventoryItem).filter(InventoryItem.part_id == part.id)
    if location is not None:
        query = query.filter(InventoryItem.location == location)
    rows = query.order_by(InventoryItem.id).all()
    out: Dict[OwnerKey, float] = {}
    for row in rows:
        assert _key(row) not in out, f"two rows for one owner at one slot: {_key(row)}"
        out[_key(row)] = float(row.quantity_on_hand)
    return out


def _inventory_audit_rows(db: Session, item_id: Optional[int] = None, action: Optional[str] = None) -> list:
    query = db.query(AuditLog).filter(AuditLog.resource_type == "inventory")
    if item_id is not None:
        query = query.filter(AuditLog.resource_id == item_id)
    if action is not None:
        query = query.filter(AuditLog.action == action)
    return query.order_by(AuditLog.id).all()


def _receive(client: TestClient, headers: dict, part: Part, location: InventoryLocation, **body):
    payload = {"part_id": part.id, "location_code": location.code, **body}
    return client.post(RECEIVE_URL, json=payload, headers=headers)


def _po_line(db: Session, *, company_id: int = COMPANY_A, quantity_ordered: float = 20.0) -> PurchaseOrderLine:
    """Vendor + PO (SENT) + one open line on a fresh purchased part."""
    vendor = _vendor(db, company_id=company_id)
    part = _part(db, company_id=company_id)
    n = _next()
    po = PurchaseOrder(
        po_number=f"OWN-PO-{n:04d}",
        vendor_id=vendor.id,
        status=POStatus.SENT,
        order_date=date(2026, 3, 1),
        company_id=company_id,
    )
    db.add(po)
    db.flush()
    line = PurchaseOrderLine(
        purchase_order_id=po.id,
        line_number=1,
        part_id=part.id,
        quantity_ordered=quantity_ordered,
        quantity_received=0.0,
        unit_price=5.0,
        is_closed=False,
        company_id=company_id,
    )
    db.add(line)
    db.commit()
    db.refresh(line)
    return line


def _second_line(db: Session, line: PurchaseOrderLine, *, quantity_ordered: float = 20.0) -> PurchaseOrderLine:
    second = PurchaseOrderLine(
        purchase_order_id=line.purchase_order_id,
        line_number=2,
        part_id=line.part_id,
        quantity_ordered=quantity_ordered,
        quantity_received=0.0,
        unit_price=5.0,
        is_closed=False,
        company_id=line.company_id,
    )
    db.add(second)
    db.commit()
    db.refresh(second)
    return second


def _po_receive(client: TestClient, headers: dict, line: PurchaseOrderLine, **body):
    payload = {"po_line_id": line.id, "requires_inspection": False, **body}
    return client.post(PO_RECEIVE_URL, json=payload, headers=headers)


@contextmanager
def _inserted_tables(db: Session) -> Iterator[List[str]]:
    """Every table an INSERT was issued against while the block ran, in order.

    A refused write is rolled back either way, so "nothing was saved" cannot tell a refusal
    raised BEFORE the first write from one raised after it. The statement log can: an owner
    is resolved before the first INSERT, so a refusal issues none.
    """
    tables: List[str] = []

    def before_execute(conn, clauseelement, multiparams, params, execution_options):  # noqa: ARG001
        if isinstance(clauseelement, Insert):
            tables.append(clauseelement.table.name)

    engine = db.get_bind()
    event.listen(engine, "before_execute", before_execute)
    try:
        yield tables
    finally:
        event.remove(engine, "before_execute", before_execute)


def _nothing_received(db: Session, part: Part) -> None:
    db.expire_all()
    assert db.query(InventoryItem).filter(InventoryItem.part_id == part.id).count() == 0
    assert db.query(InventoryTransaction).filter(InventoryTransaction.part_id == part.id).count() == 0


# ===========================================================================
# 1. Merge keys: /inventory/receive
# ===========================================================================


@pytest.mark.parametrize("lot", ["HEAT-4471", None], ids=["lot", "lot-less"])
def test_receive_adds_only_into_a_row_with_the_same_owner(db_session: Session, client: TestClient, hdrs, lot):
    """Lot L as customer- or supplier-owned beside the company's lot L is a SECOND row each.

    Five receives into one slot, alternating owners: a merge key without the owner would
    collapse them into one row of 15; the right key keeps three rows and merges each
    owner's repeat receive into its own. The lot-less case runs the NULL-safe lot branch.
    """
    part = _part(db_session)
    location = _location(db_session)
    customer = _customer(db_session)
    vendor = _vendor(db_session)
    base = {"lot_number": lot}

    company_1 = _receive(client, hdrs, part, location, quantity=5, **base)
    owned_1 = _receive(client, hdrs, part, location, quantity=4, **base, **_owner(customer))
    supplier_1 = _receive(client, hdrs, part, location, quantity=3, **base, **_owner(vendor=vendor))
    company_2 = _receive(client, hdrs, part, location, quantity=2, **base)
    owned_2 = _receive(client, hdrs, part, location, quantity=1, **base, **_owner(customer))
    responses = [company_1, owned_1, supplier_1, company_2, owned_2]
    for response in responses:
        assert response.status_code == 200, response.text
    ids = [response.json()["inventory_item_id"] for response in responses]

    assert len({ids[0], ids[1], ids[2]}) == 3, "each owner gets its own row"
    assert ids[3] == ids[0], "a company receive merges into the company row"
    assert ids[4] == ids[1], "a customer receive merges into that customer's row"
    assert _rows_by_owner(db_session, part) == {
        ("company", None, None): 7.0,
        ("customer", customer.id, None): 5.0,
        ("supplier", None, vendor.id): 3.0,
    }
    # Each RECEIVE ledger row names the row it actually added to.
    ledger = (
        db_session.query(InventoryTransaction.inventory_item_id, InventoryTransaction.quantity)
        .filter(
            InventoryTransaction.part_id == part.id,
            InventoryTransaction.transaction_type == TransactionType.RECEIVE,
        )
        .order_by(InventoryTransaction.id)
        .all()
    )
    assert [tuple(row) for row in ledger] == [(ids[0], 5.0), (ids[1], 4.0), (ids[2], 3.0), (ids[0], 2.0), (ids[1], 1.0)]


def test_receive_audit_wording_is_unchanged_for_company_stock_and_names_the_owner_otherwise(
    db_session: Session, client: TestClient, hdrs
):
    part = _part(db_session)
    location = _location(db_session)
    customer = _customer(db_session)
    vendor = _vendor(db_session)

    for body in ({}, _owner(customer), _owner(vendor=vendor)):
        response = _receive(client, hdrs, part, location, quantity=5, lot_number="AUD-1", **body)
        assert response.status_code == 200, response.text

    received = [
        row.description
        for row in _inventory_audit_rows(db_session, action="CREATE")
        if (row.description or "").startswith("Received")
    ]
    stem = f"Received 5.0 of part {part.part_number} into {location.code} lot AUD-1"
    assert received == [
        stem,  # the pre-129 sentence, byte for byte
        f"{stem} as customer-owned ({customer.name}) property",
        f"{stem} as supplier-owned ({vendor.name}) property",
    ]


@pytest.mark.parametrize(
    "scenario, status_code, detail",
    [
        ("customer_without_id", 422, None),
        ("customer_with_vendor_id", 422, None),
        ("company_with_customer_id", 422, None),
        ("supplier_without_id", 422, None),
        ("unknown_kind", 422, None),
        ("foreign_customer", 404, "Owning customer not found"),
        ("deleted_customer", 404, "Owning customer not found"),
        ("missing_customer", 404, "Owning customer not found"),
        ("foreign_vendor", 404, "Owning vendor not found"),
        ("deleted_vendor", 404, "Owning vendor not found"),
    ],
)
def test_receive_refuses_an_unresolvable_owner_before_anything_is_written(
    db_session: Session, client: TestClient, hdrs, scenario, status_code, detail
):
    """404 for an owner that is not a live customer / non-deleted vendor of THIS company
    (the same words whether it is another tenant's, deleted or missing -- no oracle), 422
    for a kind/owner mismatch. Either way no row, no ledger row, no audit row."""
    part = _part(db_session)
    location = _location(db_session)
    customer = _customer(db_session)
    vendor = _vendor(db_session)
    bodies = {
        "customer_without_id": {"ownership": "customer"},
        "customer_with_vendor_id": {
            "ownership": "customer",
            "owner_customer_id": customer.id,
            "owner_vendor_id": vendor.id,
        },
        "company_with_customer_id": {"ownership": "company", "owner_customer_id": customer.id},
        "supplier_without_id": {"ownership": "supplier"},
        "unknown_kind": {"ownership": "partner", "owner_customer_id": customer.id},
        "foreign_customer": _owner(_customer(db_session, company_id=COMPANY_B)),
        "deleted_customer": _owner(_customer(db_session, deleted=True)),
        "missing_customer": {"ownership": "customer", "owner_customer_id": 987654},
        "foreign_vendor": _owner(vendor=_vendor(db_session, company_id=COMPANY_B)),
        "deleted_vendor": _owner(vendor=_vendor(db_session, deleted=True)),
    }
    audit_before = len(_inventory_audit_rows(db_session))

    with _inserted_tables(db_session) as inserted:
        response = _receive(client, hdrs, part, location, quantity=5, lot_number="REF-1", **bodies[scenario])

    assert response.status_code == status_code, response.text
    if detail is not None:
        assert response.json()["detail"] == detail
    assert inserted == []
    _nothing_received(db_session, part)
    assert len(_inventory_audit_rows(db_session)) == audit_before


def test_receive_accepts_an_inactive_vendor_and_a_mixed_case_kind(db_session: Session, client: TestClient, hdrs):
    """An INACTIVE vendor still owns the consigned stock it left on the rack (only a deleted
    one does not), and the kind is case-insensitive like the other request enums."""
    part = _part(db_session)
    location = _location(db_session)
    vendor = _vendor(db_session, active=False)
    customer = _customer(db_session)

    supplier = _receive(client, hdrs, part, location, quantity=2, lot_number="INACT", **_owner(vendor=vendor))
    mixed = _receive(
        client,
        hdrs,
        part,
        location,
        quantity=3,
        lot_number="MIXED",
        ownership=" Customer ",
        owner_customer_id=customer.id,
    )

    assert supplier.status_code == 200, supplier.text
    assert mixed.status_code == 200, mixed.text
    assert _rows_by_owner(db_session, part) == {
        ("supplier", None, vendor.id): 2.0,
        ("customer", customer.id, None): 3.0,
    }


# ===========================================================================
# 2. Merge keys: the PO receipt (single line and delivery batch)
# ===========================================================================


def test_po_receipt_owned_line_lands_in_its_own_row(db_session: Session, client: TestClient, hdrs):
    line = _po_line(db_session)
    part = db_session.get(Part, line.part_id)
    customer = _customer(db_session)

    first = _po_receive(client, hdrs, line, quantity_received=5, lot_number="MILL-HEAT-9")
    owned = _po_receive(client, hdrs, line, quantity_received=4, lot_number="MILL-HEAT-9", **_owner(customer))
    again = _po_receive(client, hdrs, line, quantity_received=2, lot_number="MILL-HEAT-9")
    for response in (first, owned, again):
        assert response.status_code == 200, response.text

    assert _rows_by_owner(db_session, part, location=RECEIVING_DEFAULT_LOCATION) == {
        ("company", None, None): 7.0,
        ("customer", customer.id, None): 4.0,
    }
    owned_row = (
        db_session.query(InventoryItem)
        .filter(InventoryItem.part_id == part.id, InventoryItem.ownership == "customer")
        .one()
    )
    creates = [row.description for row in _inventory_audit_rows(db_session, item_id=owned_row.id, action="CREATE")]
    assert creates == [
        f"Received 4.0 of part {part.id} lot MILL-HEAT-9 at {RECEIVING_DEFAULT_LOCATION} via "
        f"{owned.json()['receipt_number']} as customer-owned ({customer.name}) property"
    ]
    receives = (
        db_session.query(InventoryTransaction.inventory_item_id)
        .filter(
            InventoryTransaction.part_id == part.id,
            InventoryTransaction.transaction_type == TransactionType.RECEIVE,
        )
        .order_by(InventoryTransaction.id)
        .all()
    )
    company_row_id = next(
        row.id
        for row in db_session.query(InventoryItem).filter(InventoryItem.part_id == part.id)
        if row.ownership == "company"
    )
    assert [row[0] for row in receives] == [company_row_id, owned_row.id, company_row_id]


def _receipt_owner(db: Session, receipt_id: int) -> OwnerKey:
    db.expire_all()
    receipt = db.get(POReceipt, receipt_id)
    return (receipt.ownership, receipt.owner_customer_id, receipt.owner_vendor_id)


def _place_held_receipt(client: TestClient, headers: dict, receipt_id: int, path: str):
    """Put a receipt held for inspection into stock: an inspection accept, or a cleared hold."""
    if path == "inspect":
        return client.post(
            f"/api/v1/receiving/inspect/{receipt_id}",
            json={"quantity_accepted": 4, "quantity_rejected": 0, "inspection_method": "visual"},
            headers=headers,
        )
    return client.post(
        f"/api/v1/receiving/receipt/{receipt_id}/clear-inspection",
        json={"reason": "Inspection box ticked in error"},
        headers=headers,
    )


@pytest.mark.parametrize("path", ["inspect", "clear"])
@pytest.mark.parametrize("kind", ["customer", "supplier"])
def test_po_receipt_owned_line_held_for_inspection_reaches_stock_under_its_owner(
    db_session: Session, client: TestClient, hdrs, kind, path
):
    """The receipt RECORDS its owner (migration 129), so material held for inspection lands on
    a stock row of that owner when it is accepted or its hold is cleared -- never merged into
    the company's lot L already sitting in the receiving slot."""
    line = _po_line(db_session)
    part = db_session.get(Part, line.part_id)
    owner = _customer(db_session) if kind == "customer" else _vendor(db_session)
    owner_fields = _owner(owner) if kind == "customer" else _owner(vendor=owner)
    expected_key = (kind, owner.id, None) if kind == "customer" else (kind, None, owner.id)
    company = _po_receive(client, hdrs, line, quantity_received=5, lot_number="HELD-HEAT")
    assert company.status_code == 200, company.text

    held = _po_receive(
        client, hdrs, line, quantity_received=4, lot_number="HELD-HEAT", requires_inspection=True, **owner_fields
    )

    assert held.status_code == 200, held.text
    body = held.json()
    assert (body["ownership"], body["owner_customer_id"], body["owner_vendor_id"]) == expected_key
    assert _receipt_owner(db_session, body["id"]) == expected_key
    assert _rows_by_owner(db_session, part) == {("company", None, None): 5.0}, "held: nothing placed yet"
    assert company.json()["ownership"] == "company"

    placed = _place_held_receipt(client, hdrs, body["id"], path)

    assert placed.status_code == 200, placed.text
    assert _rows_by_owner(db_session, part, location=RECEIVING_DEFAULT_LOCATION) == {
        ("company", None, None): 5.0,
        expected_key: 4.0,
    }


def test_held_receipt_of_a_customer_deleted_since_still_lands_as_that_customers_stock(
    db_session: Session, client: TestClient, hdrs
):
    """Carrying the recorded owner forward is a record, not a new selection: a customer
    soft-deleted between the receipt and the inspection still owns what it sent."""
    line = _po_line(db_session)
    part = db_session.get(Part, line.part_id)
    customer = _customer(db_session)
    held = _po_receive(
        client, hdrs, line, quantity_received=4, lot_number="LATE-INSP", requires_inspection=True, **_owner(customer)
    )
    assert held.status_code == 200, held.text
    customer.soft_delete(None)
    db_session.commit()

    placed = _place_held_receipt(client, hdrs, held.json()["id"], "inspect")

    assert placed.status_code == 200, placed.text
    assert _rows_by_owner(db_session, part) == {("customer", customer.id, None): 4.0}


def test_po_receipt_company_line_held_for_inspection_still_works(db_session: Session, client: TestClient, hdrs):
    line = _po_line(db_session)

    response = _po_receive(client, hdrs, line, quantity_received=5, requires_inspection=True)

    assert response.status_code == 200, response.text
    assert response.json()["ownership"] == "company"
    db_session.expire_all()
    receipt = db_session.query(POReceipt).one()
    assert (receipt.requires_inspection, receipt.ownership, receipt.owner_customer_id) == (True, "company", None)
    assert db_session.query(InventoryItem).filter(InventoryItem.part_id == line.part_id).count() == 0


@pytest.mark.parametrize("scenario", ["foreign_customer", "deleted_customer", "foreign_vendor", "deleted_vendor"])
def test_po_receipt_unresolvable_owner_is_404_before_the_receipt_exists(
    db_session: Session, client: TestClient, hdrs, scenario
):
    line = _po_line(db_session)
    owner = {
        "foreign_customer": lambda: _owner(_customer(db_session, company_id=COMPANY_B)),
        "deleted_customer": lambda: _owner(_customer(db_session, deleted=True)),
        "foreign_vendor": lambda: _owner(vendor=_vendor(db_session, company_id=COMPANY_B)),
        "deleted_vendor": lambda: _owner(vendor=_vendor(db_session, deleted=True)),
    }[scenario]()

    with _inserted_tables(db_session) as inserted:
        response = _po_receive(client, hdrs, line, quantity_received=5, **owner)

    assert response.status_code == 404, response.text
    assert inserted == [], "the owner is resolved before the receipt row, the first write"
    db_session.expire_all()
    assert db_session.query(POReceipt).count() == 0
    assert db_session.get(PurchaseOrderLine, line.id).quantity_received == 0
    _nothing_received(db_session, db_session.get(Part, line.part_id))


def test_delivery_batch_carries_ownership_per_line(db_session: Session, client: TestClient, hdrs):
    line = _po_line(db_session)
    second = _second_line(db_session, line)
    customer = _customer(db_session)
    body = {
        "idempotency_key": "owned-delivery-001",
        "purchase_order_id": line.purchase_order_id,
        "lines": [
            {"po_line_id": line.id, "quantity_received": 3, "lot_number": "DLV-A"},
            {"po_line_id": second.id, "quantity_received": 4, "lot_number": "DLV-A", **_owner(customer)},
        ],
    }

    response = client.post(DELIVERY_URL, json=body, headers=hdrs)

    assert response.status_code == 200, response.text
    assert len(response.json()["receipts"]) == 2
    assert _rows_by_owner(db_session, db_session.get(Part, line.part_id), location=RECEIVING_DEFAULT_LOCATION) == {
        ("company", None, None): 3.0,
        ("customer", customer.id, None): 4.0,
    }


def test_delivery_batch_accepts_an_owned_line_held_for_inspection_and_records_its_owner(
    db_session: Session, client: TestClient, hdrs
):
    line = _po_line(db_session)
    second = _second_line(db_session, line)
    customer = _customer(db_session)
    body = {
        "idempotency_key": "owned-delivery-held",
        "purchase_order_id": line.purchase_order_id,
        "lines": [
            {"po_line_id": line.id, "quantity_received": 3, "lot_number": "DLV-A"},
            {
                "po_line_id": second.id,
                "quantity_received": 4,
                "lot_number": "DLV-A",
                "requires_inspection": True,
                **_owner(customer),
            },
        ],
    }

    response = client.post(DELIVERY_URL, json=body, headers=hdrs)

    assert response.status_code == 200, response.text
    receipts = {row["po_line_id"]: row for row in response.json()["receipts"]}
    assert _receipt_owner(db_session, receipts[second.id]["id"]) == ("customer", customer.id, None)
    assert _receipt_owner(db_session, receipts[line.id]["id"]) == ("company", None, None)
    part = db_session.get(Part, line.part_id)
    assert _rows_by_owner(db_session, part) == {("company", None, None): 3.0}, "the held line places nothing yet"

    placed = _place_held_receipt(client, hdrs, receipts[second.id]["id"], "inspect")
    assert placed.status_code == 200, placed.text
    assert _rows_by_owner(db_session, part, location=RECEIVING_DEFAULT_LOCATION) == {
        ("company", None, None): 3.0,
        ("customer", customer.id, None): 4.0,
    }


def test_delivery_batch_refuses_an_unresolvable_owner_on_any_line_before_the_first_write(
    db_session: Session, client: TestClient, hdrs
):
    """Line 2's owner is resolved before the batch row -- the delivery's first write -- so
    line 1 (a perfectly good company line) is not posted either."""
    line = _po_line(db_session)
    second = _second_line(db_session, line)
    foreign = _customer(db_session, company_id=COMPANY_B)
    body = {
        "idempotency_key": "owned-delivery-foreign",
        "purchase_order_id": line.purchase_order_id,
        "lines": [
            {"po_line_id": line.id, "quantity_received": 3, "lot_number": "DLV-A"},
            {"po_line_id": second.id, "quantity_received": 4, "lot_number": "DLV-B", **_owner(foreign)},
        ],
    }

    with _inserted_tables(db_session) as inserted:
        response = client.post(DELIVERY_URL, json=body, headers=hdrs)

    assert (response.status_code, response.json()["detail"]) == (
        404,
        "Delivery line 2: Owning customer not found. No delivery lines were posted.",
    )
    assert inserted == [], "line 2's owner is resolved before the batch row and line 1's receipt"
    db_session.expire_all()
    for model in (POReceipt, InventoryItem, InventoryTransaction, ReceivingDeliveryBatch):
        assert db_session.query(model).count() == 0, model.__name__
    assert (
        db_session.query(AuditLog)
        .filter(AuditLog.resource_type.in_(["receipt", "receiving_delivery", "inventory"]))
        .count()
        == 0
    )


def _pre_129_digest(body: dict) -> str:
    """The idempotency digest a delivery hashed to BEFORE migration 129: the request dumped
    with the receipt fields only -- no ownership keys at all."""
    payload = {
        "idempotency_key": body["idempotency_key"],
        "purchase_order_id": body["purchase_order_id"],
        "lines": [
            ReceiptLineFields.model_validate(
                {k: v for k, v in line.items() if not k.startswith(("ownership", "owner_"))}
            ).model_dump(mode="json")
            for line in body["lines"]
        ],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


@pytest.mark.parametrize("explicit_defaults", [False, True], ids=["keys-omitted", "default-keys-sent"])
def test_company_delivery_digest_is_byte_identical_to_the_pre_129_digest(
    db_session: Session, client: TestClient, hdrs, explicit_defaults
):
    """A delivery submitted before the deploy and retried after it must REPLAY, not answer 409
    "different submission": a company line hashes exactly as it did, whether the client omits
    the ownership keys or sends their defaults."""
    line = _po_line(db_session)
    company_line = {"po_line_id": line.id, "quantity_received": 3, "lot_number": "DIG-1"}
    defaults = {"ownership": "company", "owner_customer_id": None, "owner_vendor_id": None}
    body = {
        "idempotency_key": f"digest-{'explicit' if explicit_defaults else 'omitted'}",
        "purchase_order_id": line.purchase_order_id,
        "lines": [{**company_line, **(defaults if explicit_defaults else {})}],
    }

    first = client.post(DELIVERY_URL, json=body, headers=hdrs)

    assert first.status_code == 200, first.text
    db_session.expire_all()
    batch = db_session.query(ReceivingDeliveryBatch).one()
    assert batch.payload_hash == _pre_129_digest(body)
    # The other spelling of the same company line is the same submission: it replays.
    other = {**body, "lines": [{**company_line, **({} if explicit_defaults else defaults)}]}
    replay = client.post(DELIVERY_URL, json=other, headers=hdrs)
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    assert db_session.query(POReceipt).count() == 1
    # Changing the line's owner IS a different submission.
    owned = {**body, "lines": [{**company_line, **_owner(_customer(db_session))}]}
    assert client.post(DELIVERY_URL, json=owned, headers=hdrs).status_code == 409


def test_voiding_an_owned_po_receipt_unwinds_the_owned_row(db_session: Session, client: TestClient, hdrs):
    """Void finds the receipt's row through its own RECEIVE ledger row, so with a company lot
    and an owned lot in one slot it unwinds the receipt's row and leaves the other alone."""
    line = _po_line(db_session)
    part = db_session.get(Part, line.part_id)
    customer = _customer(db_session)
    assert _po_receive(client, hdrs, line, quantity_received=5, lot_number="SHARED-HEAT").status_code == 200
    owned = _po_receive(client, hdrs, line, quantity_received=4, lot_number="SHARED-HEAT", **_owner(customer))
    assert owned.status_code == 200, owned.text

    void = client.post(
        f"/api/v1/receiving/receipt/{owned.json()['id']}/void",
        json={"reason": "Customer-furnished bar arrived on the wrong PO"},
        headers=hdrs,
    )

    assert void.status_code == 200, void.text
    assert _rows_by_owner(db_session, part, location=RECEIVING_DEFAULT_LOCATION) == {
        ("company", None, None): 5.0,
        ("customer", customer.id, None): 0.0,
    }


def test_correcting_a_company_po_receipt_adjusts_the_company_row_beside_an_owned_lot(
    db_session: Session, client: TestClient, hdrs
):
    """The mirror, through the correction verb: an owned row with the LOWER id in the slot
    must not be the one a company receipt's correction unwinds."""
    line = _po_line(db_session)
    part = db_session.get(Part, line.part_id)
    customer = _customer(db_session)
    assert (
        _po_receive(client, hdrs, line, quantity_received=4, lot_number="SHARED-2", **_owner(customer)).status_code
        == 200
    )
    company = _po_receive(client, hdrs, line, quantity_received=5, lot_number="SHARED-2")
    assert company.status_code == 200, company.text

    corrected = client.patch(
        f"/api/v1/receiving/receipt/{company.json()['id']}",
        json={"quantity_received": 3, "reason": "Packing slip over-counted"},
        headers=hdrs,
    )

    assert corrected.status_code == 200, corrected.text
    assert _rows_by_owner(db_session, part, location=RECEIVING_DEFAULT_LOCATION) == {
        ("customer", customer.id, None): 4.0,
        ("company", None, None): 3.0,
    }


def _unlink_receipt_ledger(db: Session, receipt_number: str) -> None:
    """Make a receipt look like it predates the RECEIVE row's stock-row link."""
    db.execute(
        update(InventoryTransaction)
        .where(
            InventoryTransaction.reference_type == "po_receipt",
            InventoryTransaction.reference_number == receipt_number,
        )
        .values(inventory_item_id=None)
    )
    db.commit()


def test_voiding_a_legacy_receipt_with_two_matching_rows_is_refused_not_guessed(
    db_session: Session, client: TestClient, hdrs
):
    """A receipt whose RECEIVE row carries no stock-row link falls back to part + location + lot
    -- but only when exactly ONE row matches. Two (a company and an owned lot L) is a 409 for
    a person to resolve, with nothing changed."""
    line = _po_line(db_session)
    part = db_session.get(Part, line.part_id)
    legacy = _po_receive(client, hdrs, line, quantity_received=5, lot_number="LEGACY-L")
    assert legacy.status_code == 200, legacy.text
    _unlink_receipt_ledger(db_session, legacy.json()["receipt_number"])
    customer = _customer(db_session)
    _stock(db_session, part, qty=2, lot="LEGACY-L", location=RECEIVING_DEFAULT_LOCATION, customer=customer)
    ledger_before = db_session.query(InventoryTransaction).count()

    void = client.post(
        f"/api/v1/receiving/receipt/{legacy.json()['id']}/void", json={"reason": "Duplicate entry"}, headers=hdrs
    )

    assert void.status_code == 409, void.text
    assert void.json()["detail"] == (
        "Cannot reconcile inventory for this receipt automatically; make a manual inventory adjustment."
    )
    assert _rows_by_owner(db_session, part) == {("company", None, None): 5.0, ("customer", customer.id, None): 2.0}
    assert db_session.query(InventoryTransaction).count() == ledger_before
    assert db_session.get(POReceipt, legacy.json()["id"]).is_deleted is False


def test_voiding_a_legacy_receipt_with_exactly_one_matching_row_still_works(
    db_session: Session, client: TestClient, hdrs
):
    line = _po_line(db_session)
    part = db_session.get(Part, line.part_id)
    legacy = _po_receive(client, hdrs, line, quantity_received=5, lot_number="LEGACY-ONE")
    assert legacy.status_code == 200, legacy.text
    _unlink_receipt_ledger(db_session, legacy.json()["receipt_number"])

    void = client.post(
        f"/api/v1/receiving/receipt/{legacy.json()['id']}/void", json={"reason": "Duplicate entry"}, headers=hdrs
    )

    assert void.status_code == 200, void.text
    assert _rows_by_owner(db_session, part) == {("company", None, None): 0.0}


# ===========================================================================
# 3. Merge keys: transfer and finished-goods receipt
# ===========================================================================


def test_transfer_carries_the_owner_and_merges_only_with_the_same_owner(db_session: Session, client: TestClient, hdrs):
    part = _part(db_session)
    origin = _location(db_session)
    destination = _location(db_session)
    customer = _customer(db_session)
    owned = _stock(db_session, part, qty=6, lot="TR-1", location=origin.code, customer=customer)
    company_src = _stock(db_session, part, qty=5, lot="TR-1", location=origin.code)
    _stock(db_session, part, qty=2, lot="TR-1", location=destination.code)  # the company's row at the destination

    def transfer(item: InventoryItem, quantity: float):
        response = client.post(
            TRANSFER_URL,
            json={"inventory_item_id": item.id, "quantity": quantity, "to_location_code": destination.code},
            headers=hdrs,
        )
        assert response.status_code == 200, response.text

    transfer(owned, 4)
    assert _rows_by_owner(db_session, part, location=destination.code) == {
        ("company", None, None): 2.0,
        ("customer", customer.id, None): 4.0,
    }, "the customer's stock must not land in the company's row at the destination"

    transfer(owned, 1)
    transfer(company_src, 3)
    assert _rows_by_owner(db_session, part, location=destination.code) == {
        ("company", None, None): 5.0,
        ("customer", customer.id, None): 5.0,
    }
    assert _rows_by_owner(db_session, part, location=origin.code) == {
        ("customer", customer.id, None): 1.0,
        ("company", None, None): 2.0,
    }


def _work_order(db: Session, part: Part, *, quantity_complete: float, lot_number: str) -> WorkOrder:
    n = _next()
    wo = WorkOrder(
        work_order_number=f"OWN-WO-{n:04d}",
        part_id=part.id,
        quantity_ordered=quantity_complete,
        quantity_complete=quantity_complete,
        status=WorkOrderStatus.IN_PROGRESS,
        lot_number=lot_number,
        due_date=date(2026, 12, 1),
        company_id=part.company_id,
    )
    db.add(wo)
    db.commit()
    db.refresh(wo)
    return wo


def test_finished_goods_receipt_never_adds_into_an_owned_row(db_session: Session, admin: User):
    """Finished goods the shop made are company stock: with only a customer-owned row in the
    FG slot, the receipt creates the company row beside it and leaves the owned row alone."""
    part = _part(db_session, part_type="manufactured", standard_cost=5.0)
    customer = _customer(db_session)
    owned = _stock(db_session, part, qty=3, lot="FG-OWN-LOT", location=FINISHED_GOODS_LOCATION, customer=customer)
    wo = _work_order(db_session, part, quantity_complete=4, lot_number="FG-OWN-LOT")

    txn = receive_finished_goods_for_work_order(
        db_session, wo, user_id=admin.id, company_id=COMPANY_A, audit=AuditService(db_session, admin)
    )
    db_session.commit()

    assert _rows_by_owner(db_session, part, location=FINISHED_GOODS_LOCATION) == {
        ("customer", customer.id, None): 3.0,
        ("company", None, None): 4.0,
    }
    assert txn is not None and txn.inventory_item_id != owned.id


def test_finished_goods_receipt_still_merges_into_the_company_row(db_session: Session, admin: User):
    part = _part(db_session, part_type="manufactured", standard_cost=5.0)
    customer = _customer(db_session)
    _stock(db_session, part, qty=2, lot="FG-MIX-LOT", location=FINISHED_GOODS_LOCATION, customer=customer)
    company_row = _stock(db_session, part, qty=3, lot="FG-MIX-LOT", location=FINISHED_GOODS_LOCATION)
    wo = _work_order(db_session, part, quantity_complete=4, lot_number="FG-MIX-LOT")

    txn = receive_finished_goods_for_work_order(
        db_session, wo, user_id=admin.id, company_id=COMPANY_A, audit=AuditService(db_session, admin)
    )
    db_session.commit()

    assert txn.inventory_item_id == company_row.id
    assert _rows_by_owner(db_session, part, location=FINISHED_GOODS_LOCATION) == {
        ("customer", customer.id, None): 2.0,
        ("company", None, None): 7.0,
    }


def _shipment(db: Session, wo: WorkOrder, quantity: float) -> Shipment:
    shipment = Shipment(
        company_id=wo.company_id,
        shipment_number=f"OWN-SHP-{_next():04d}",
        work_order_id=wo.id,
        quantity_shipped=quantity,
    )
    db.add(shipment)
    db.commit()
    db.refresh(shipment)
    return shipment


def test_shipment_decrements_the_company_fg_row_never_a_re_owned_one(db_session: Session, admin: User):
    """The shop ships its own finished goods. With a re-owned (customer) row holding the
    LOWER id in the FG slot, the SHIP decrement still lands on the company row."""
    part = _part(db_session, part_type="manufactured", standard_cost=5.0)
    customer = _customer(db_session)
    owned = _stock(db_session, part, qty=3, lot="FG-SHIP-1", location=FINISHED_GOODS_LOCATION, customer=customer)
    company_row = _stock(db_session, part, qty=5, lot="FG-SHIP-1", location=FINISHED_GOODS_LOCATION)
    wo = _work_order(db_session, part, quantity_complete=5, lot_number="FG-SHIP-1")

    txn = decrement_finished_goods_for_shipment(
        db_session,
        work_order=wo,
        shipment=_shipment(db_session, wo, 2),
        company_id=COMPANY_A,
        user_id=admin.id,
        audit=AuditService(db_session, admin),
    )
    db_session.commit()

    assert txn is not None and txn.inventory_item_id == company_row.id
    assert (txn.transaction_type, txn.quantity) == (TransactionType.SHIP, -2.0)
    assert _rows_by_owner(db_session, part, location=FINISHED_GOODS_LOCATION) == {
        ("customer", customer.id, None): 3.0,
        ("company", None, None): 3.0,
    }
    assert db_session.get(InventoryItem, owned.id).quantity_on_hand == 3.0


def test_shipment_with_only_a_re_owned_fg_row_records_the_missing_lot_and_leaves_it_alone(
    db_session: Session, client: TestClient, hdrs, admin: User
):
    """Re-owning the whole FG lot to a customer leaves no COMPANY row to ship from: the
    decrement records its usual "FG lot missing" discrepancy rather than touch the owned row."""
    part = _part(db_session, part_type="manufactured", standard_cost=5.0)
    customer = _customer(db_session)
    wo = _work_order(db_session, part, quantity_complete=4, lot_number="FG-SHIP-2")
    receive_finished_goods_for_work_order(
        db_session, wo, user_id=admin.id, company_id=COMPANY_A, audit=AuditService(db_session, admin)
    )
    db_session.commit()
    fg_row = db_session.query(InventoryItem).filter(InventoryItem.part_id == part.id).one()
    reowned = client.post(
        f"/api/v1/inventory/items/{fg_row.id}/ownership",
        json={**_owner(customer), "reason": "Built from customer-furnished kits"},
        headers=hdrs,
    )
    assert reowned.status_code == 200, reowned.text

    txn = decrement_finished_goods_for_shipment(
        db_session,
        work_order=db_session.get(WorkOrder, wo.id),
        shipment=_shipment(db_session, wo, 2),
        company_id=COMPANY_A,
        user_id=admin.id,
        audit=AuditService(db_session, admin),
    )
    db_session.commit()

    assert txn is None
    assert _rows_by_owner(db_session, part) == {("customer", customer.id, None): 4.0}
    assert db_session.query(AuditLog).filter(AuditLog.action == SHIP_FG_MISSING_AUDIT_ACTION).count() == 1
    assert (
        db_session.query(InventoryTransaction)
        .filter(InventoryTransaction.transaction_type == TransactionType.SHIP)
        .count()
        == 0
    )


# ===========================================================================
# 4. Reads: list filters, owner_name, summary
# ===========================================================================


def _listed(client: TestClient, headers: dict, **params) -> Dict[str, tuple]:
    response = client.get(INVENTORY_URL, params=params, headers=headers)
    assert response.status_code == 200, response.text
    return {
        row["lot_number"]: (row["ownership"], row["owner_customer_id"], row["owner_vendor_id"], row["owner_name"])
        for row in response.json()
    }


def test_inventory_list_carries_the_owner_and_filters_by_it(db_session: Session, client: TestClient, hdrs):
    part = _part(db_session)
    since_deleted = _customer(db_session)
    other_customer = _customer(db_session)
    vendor = _vendor(db_session)
    _stock(db_session, part, qty=5, lot="L-CO")
    _stock(db_session, part, qty=4, lot="L-C1", customer=since_deleted)
    _stock(db_session, part, qty=3, lot="L-C2", customer=other_customer)
    _stock(db_session, part, qty=2, lot="L-SU", vendor=vendor)
    # Who owns the material is a historical record: a soft-deleted customer keeps being named.
    since_deleted.soft_delete(None)
    db_session.commit()

    assert _listed(client, hdrs, part_id=part.id) == {
        "L-CO": ("company", None, None, None),
        "L-C1": ("customer", since_deleted.id, None, since_deleted.name),
        "L-C2": ("customer", other_customer.id, None, other_customer.name),
        "L-SU": ("supplier", None, vendor.id, vendor.name),
    }
    assert set(_listed(client, hdrs, part_id=part.id, ownership="customer")) == {"L-C1", "L-C2"}
    assert set(_listed(client, hdrs, part_id=part.id, ownership="supplier")) == {"L-SU"}
    assert set(_listed(client, hdrs, part_id=part.id, ownership="company")) == {"L-CO"}
    assert set(_listed(client, hdrs, part_id=part.id, owner_customer_id=other_customer.id)) == {"L-C2"}
    assert set(_listed(client, hdrs, part_id=part.id, owner_vendor_id=vendor.id)) == {"L-SU"}
    assert client.get(INVENTORY_URL, params={"ownership": "partner"}, headers=hdrs).status_code == 422


def test_inventory_list_never_names_another_tenants_owner(db_session: Session, client: TestClient, hdrs):
    """Owner names resolve tenant-scoped. A row that (by corruption or a bad import) points at
    ANOTHER company's customer must not print that company's customer name."""
    part = _part(db_session)
    foreign = _customer(db_session, company_id=COMPANY_B)
    _stock(db_session, part, qty=1, lot="LEAK", customer=foreign)
    foreign_part = _part(db_session, company_id=COMPANY_B)
    _stock(db_session, foreign_part, qty=9, lot="B-ONLY", customer=foreign)

    assert _listed(client, hdrs, part_id=part.id) == {"LEAK": ("customer", foreign.id, None, None)}
    assert _listed(client, hdrs, owner_customer_id=foreign.id) == {"LEAK": ("customer", foreign.id, None, None)}
    foreign_admin = _headers(_user(db_session, company_id=COMPANY_B))
    assert _listed(client, foreign_admin) == {"B-ONLY": ("customer", foreign.id, None, foreign.name)}


def test_inventory_summary_keeps_physical_on_hand_and_reports_owned_by_others(
    db_session: Session, client: TestClient, hdrs
):
    mixed = _part(db_session)
    company_only = _part(db_session)
    customer = _customer(db_session)
    vendor = _vendor(db_session)
    _stock(db_session, mixed, qty=5, lot="S-CO")
    _stock(db_session, mixed, qty=4, lot="S-CU", customer=customer)
    _stock(db_session, mixed, qty=2, lot="S-SU", vendor=vendor)
    _stock(db_session, company_only, qty=3, lot="S-ONLY")

    response = client.get("/api/v1/inventory/summary", headers=hdrs)

    assert response.status_code == 200, response.text
    by_part = {row["part_id"]: row for row in response.json()}
    assert by_part[mixed.id]["total_on_hand"] == 11.0, "on hand stays PHYSICAL, whoever owns it"
    assert by_part[mixed.id]["owned_by_others_on_hand"] == 6.0
    assert sorted((loc["lot_number"], loc["ownership"]) for loc in by_part[mixed.id]["locations"]) == [
        ("S-CO", "company"),
        ("S-CU", "customer"),
        ("S-SU", "supplier"),
    ]
    assert by_part[company_only.id]["owned_by_others_on_hand"] == 0


# ===========================================================================
# 5. The change-of-ownership verb
# ===========================================================================


def _ownership_url(item_id: int) -> str:
    return f"/api/v1/inventory/items/{item_id}/ownership"


def _ownership_updates(db: Session, item_id: int) -> list:
    db.expire_all()
    return [
        row
        for row in _inventory_audit_rows(db, item_id=item_id, action="UPDATE")
        if (row.extra_data or {}).get("change") == "ownership"
    ]


def _assert_untouched(db: Session, item: InventoryItem, expected: OwnerKey) -> None:
    db.expire_all()
    row = db.get(InventoryItem, item.id)
    assert _key(row) == expected
    assert _ownership_updates(db, item.id) == []


@pytest.mark.parametrize(
    "role, allowed",
    [
        (UserRole.ADMIN, True),
        (UserRole.MANAGER, True),
        (UserRole.SUPERVISOR, True),
        (UserRole.OPERATOR, False),
        (UserRole.QUALITY, False),
        (UserRole.SHIPPING, False),
        (UserRole.VIEWER, False),
    ],
)
def test_ownership_change_is_admin_manager_supervisor_only(db_session: Session, client: TestClient, role, allowed):
    part = _part(db_session)
    item = _stock(db_session, part, qty=8, lot="ROLE-1")
    customer = _customer(db_session)
    headers = _headers(_user(db_session, role=role))

    response = client.post(
        _ownership_url(item.id),
        json={**_owner(customer), "reason": "Customer-furnished material"},
        headers=headers,
    )

    if allowed:
        assert response.status_code == 200, response.text
        db_session.expire_all()
        assert _key(db_session.get(InventoryItem, item.id)) == ("customer", customer.id, None)
    else:
        assert response.status_code == 403, response.text
        _assert_untouched(db_session, item, ("company", None, None))


@pytest.mark.parametrize("reason", [None, "", "   "], ids=["missing", "empty", "blank"])
def test_ownership_change_requires_a_reason(db_session: Session, client: TestClient, hdrs, reason):
    part = _part(db_session)
    item = _stock(db_session, part, qty=8, lot="RSN-1")
    body = _owner(_customer(db_session))
    if reason is not None:
        body["reason"] = reason

    response = client.post(_ownership_url(item.id), json=body, headers=hdrs)

    assert response.status_code == 422, response.text
    _assert_untouched(db_session, item, ("company", None, None))


def test_ownership_change_writes_one_audit_row_with_old_new_and_reason_and_no_ledger_row(
    db_session: Session, client: TestClient, hdrs
):
    part = _part(db_session)
    item = _stock(db_session, part, qty=8, lot="V1", location="RAW-A")
    customer = _customer(db_session)
    events_before = db_session.query(OperationalEvent).count()

    response = client.post(
        _ownership_url(item.id),
        json={**_owner(customer), "reason": "  Customer-furnished bar stock  "},
        headers=hdrs,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["id"], body["ownership"], body["owner_customer_id"], body["owner_vendor_id"]) == (
        item.id,
        "customer",
        customer.id,
        None,
    )
    assert body["owner_name"] == customer.name
    assert body["quantity_on_hand"] == 8.0

    [row] = _ownership_updates(db_session, item.id)
    assert row.old_values == {
        "ownership": "company",
        "owner_customer_id": None,
        "owner_vendor_id": None,
        "owner_name": None,
    }
    assert row.new_values == {
        "ownership": "customer",
        "owner_customer_id": customer.id,
        "owner_vendor_id": None,
        "owner_name": customer.name,
    }
    expected_extra = {
        "change": "ownership",
        "reason": "Customer-furnished bar stock",  # stripped
        "part_id": part.id,
        "lot_number": "V1",
        "location": "RAW-A",
        "quantity_on_hand": 8.0,
    }
    assert {key: row.extra_data.get(key) for key in expected_extra} == expected_extra
    assert row.description.endswith(
        f"from company-owned to customer-owned ({customer.name}). Reason: Customer-furnished bar stock"
    )

    # Ownership is not a quantity movement: no ledger row, no quantity change, no event.
    db_session.expire_all()
    assert db_session.query(InventoryTransaction).filter(InventoryTransaction.inventory_item_id == item.id).count() == 0
    assert db_session.get(InventoryItem, item.id).quantity_on_hand == 8.0
    assert db_session.query(OperationalEvent).count() == events_before


def _job(db: Session, part: Part, status: WorkOrderStatus = WorkOrderStatus.RELEASED) -> WorkOrder:
    wo = WorkOrder(
        work_order_number=f"OWN-JOB-{_next():04d}",
        part_id=part.id,
        quantity_ordered=10,
        status=status,
        company_id=part.company_id,
    )
    db.add(wo)
    db.commit()
    db.refresh(wo)
    return wo


def _open_tie(
    db: Session,
    wo: WorkOrder,
    part: Part,
    *,
    planned: float,
    consumed: float = 0.0,
    pinned: Optional[InventoryItem] = None,
) -> WorkOrderMaterialAllocation:
    allocation = WorkOrderMaterialAllocation(
        company_id=wo.company_id,
        work_order_id=wo.id,
        part_id=part.id,
        source=AllocationSource.MANUAL,
        status=AllocationStatus.OPEN,
        qty_planned=planned,
        qty_consumed=consumed,
        unit_of_measure="each",
        pinned_inventory_item_id=pinned.id if pinned is not None else None,
        pinned_lot_number=pinned.lot_number if pinned is not None else None,
    )
    db.add(allocation)
    db.commit()
    db.refresh(allocation)
    return allocation


def test_ownership_change_discloses_pinned_ties_and_the_unpinned_shortfall_it_creates(
    db_session: Session, client: TestClient, hdrs
):
    """Disclosure, never a refusal: re-owning company stock away tells the person which jobs
    are PINNED to the row (they still draw it) and how short the part's open UNPINNED demand
    now is against the company stock left. Terminal jobs and fully drawn ties do not count."""
    part = _part(db_session)
    reowned = _stock(db_session, part, qty=8, lot="DISC-A")
    _stock(db_session, part, qty=3, lot="DISC-B")  # the company stock left
    unpinned_job = _job(db_session, part)
    _open_tie(db_session, unpinned_job, part, planned=6, consumed=1)  # 5 outstanding
    pinned_job = _job(db_session, part)
    pinned_tie = _open_tie(db_session, pinned_job, part, planned=4, pinned=reowned)
    _open_tie(db_session, _job(db_session, part, WorkOrderStatus.COMPLETE), part, planned=50)  # terminal
    _open_tie(db_session, _job(db_session, part), part, planned=2, consumed=2)  # nothing left to draw
    customer = _customer(db_session)

    response = client.post(
        _ownership_url(reowned.id), json={**_owner(customer), "reason": "Customer-furnished"}, headers=hdrs
    )

    assert response.status_code == 200, response.text
    expected_pinned = [
        {
            "allocation_id": pinned_tie.id,
            "work_order_id": pinned_job.id,
            "work_order_number": pinned_job.work_order_number,
            "outstanding_quantity": 4.0,
        }
    ]
    expected_unpinned = {
        "open_unpinned_demand": 5.0,
        "company_stock_remaining": 3.0,
        "short_by": 2.0,
        "work_order_numbers": [unpinned_job.work_order_number],
    }
    assert response.json()["open_pinned_ties"] == expected_pinned
    assert response.json()["unpinned_demand"] == expected_unpinned
    assert response.json()["created_at"].endswith("Z"), "the hand-built row serves UTC"
    [row] = _ownership_updates(db_session, reowned.id)
    assert row.extra_data["open_pinned_ties"] == expected_pinned
    assert row.extra_data["unpinned_demand"] == expected_unpinned
    _assert_owner(db_session, reowned, ("customer", customer.id, None))


@pytest.mark.parametrize("new_kind", ["company", "supplier"])
def test_ownership_change_away_from_an_owner_discloses_no_unpinned_demand(
    db_session: Session, client: TestClient, hdrs, new_kind
):
    """``unpinned_demand`` is only about COMPANY stock being taken out of the automatic draw;
    re-owning customer stock (to the company or to a supplier) reports null. Pinned ties are
    still listed."""
    part = _part(db_session)
    customer = _customer(db_session)
    item = _stock(db_session, part, qty=8, lot="DISC-C", customer=customer)
    _open_tie(db_session, _job(db_session, part), part, planned=6)
    pinned_job = _job(db_session, part)
    pinned_tie = _open_tie(db_session, pinned_job, part, planned=2, pinned=item)
    body = {"ownership": "company"} if new_kind == "company" else _owner(vendor=_vendor(db_session))

    response = client.post(_ownership_url(item.id), json={**body, "reason": "Re-owned"}, headers=hdrs)

    assert response.status_code == 200, response.text
    assert response.json()["unpinned_demand"] is None
    assert [tie["allocation_id"] for tie in response.json()["open_pinned_ties"]] == [pinned_tie.id]
    [row] = _ownership_updates(db_session, item.id)
    assert row.extra_data["unpinned_demand"] is None


def _assert_owner(db: Session, item: InventoryItem, expected: OwnerKey) -> None:
    db.expire_all()
    assert _key(db.get(InventoryItem, item.id)) == expected


def test_ownership_change_round_trip_writes_one_row_per_change(db_session: Session, client: TestClient, hdrs):
    part = _part(db_session)
    item = _stock(db_session, part, qty=5, lot="RT-1")
    customer = _customer(db_session)
    vendor = _vendor(db_session, active=False)  # an inactive vendor is a valid new owner

    steps = [
        (_owner(customer), "Customer-furnished"),
        (_owner(vendor=vendor), "Actually consigned by the supplier"),
        ({"ownership": "company"}, "Bought outright"),
    ]
    for body, reason in steps:
        response = client.post(_ownership_url(item.id), json={**body, "reason": reason}, headers=hdrs)
        assert response.status_code == 200, response.text

    rows = _ownership_updates(db_session, item.id)
    assert [(r.old_values["ownership"], r.new_values["ownership"], r.extra_data["reason"]) for r in rows] == [
        ("company", "customer", "Customer-furnished"),
        ("customer", "supplier", "Actually consigned by the supplier"),
        ("supplier", "company", "Bought outright"),
    ]
    assert rows[1].old_values["owner_name"] == customer.name
    assert rows[2].old_values["owner_name"] == vendor.name
    assert _key(db_session.get(InventoryItem, item.id)) == ("company", None, None)


@pytest.mark.parametrize(
    "scenario, status_code, detail_fragment",
    [
        ("inactive_row", 409, "is deactivated"),
        ("zero_quantity", 409, "has no stock on hand (0)"),
        ("negative_quantity", 409, "has no stock on hand (-2)"),
        ("already_company", 409, "is already company-owned; nothing to change."),
        ("already_same_customer", 409, "is already customer-owned ("),
        ("kind_mismatch", 422, None),
        ("foreign_customer", 404, "Owning customer not found"),
        ("deleted_customer", 404, "Owning customer not found"),
        ("deleted_vendor", 404, "Owning vendor not found"),
        ("foreign_row", 404, "Inventory item not found"),
        ("missing_row", 404, "Inventory item not found"),
    ],
)
def test_ownership_change_refusals_write_nothing(
    db_session: Session, client: TestClient, hdrs, scenario, status_code, detail_fragment
):
    part = _part(db_session)
    customer = _customer(db_session)
    item_kwargs = {"qty": 8.0, "lot": "REF-OWN"}
    if scenario == "inactive_row":
        item_kwargs["is_active"] = False
    if scenario == "zero_quantity":
        item_kwargs["qty"] = 0.0
    if scenario == "negative_quantity":
        item_kwargs["qty"] = -2.0
    if scenario == "already_same_customer":
        item_kwargs["customer"] = customer
    if scenario == "foreign_row":
        item = _stock(db_session, _part(db_session, company_id=COMPANY_B), **item_kwargs)
    else:
        item = _stock(db_session, part, **item_kwargs)
    starting_key = _key(item)
    item_id = 987654 if scenario == "missing_row" else item.id
    body = {
        "already_company": {"ownership": "company"},
        "kind_mismatch": {"ownership": "customer"},
        "foreign_customer": _owner(_customer(db_session, company_id=COMPANY_B)),
        "deleted_customer": _owner(_customer(db_session, deleted=True)),
        "deleted_vendor": _owner(vendor=_vendor(db_session, deleted=True)),
    }.get(scenario, _owner(customer))

    response = client.post(_ownership_url(item_id), json={**body, "reason": "Recount"}, headers=hdrs)

    assert response.status_code == status_code, response.text
    if detail_fragment is not None:
        assert detail_fragment in response.json()["detail"]
    _assert_untouched(db_session, item, starting_key)


def test_ownership_change_audit_loss_answers_503_and_saves_nothing(
    db_session: Session, client: TestClient, hdrs, monkeypatch
):
    part = _part(db_session)
    item = _stock(db_session, part, qty=8, lot="AUD-LOSS")
    customer = _customer(db_session)
    original = AuditService.log_required

    def unavailable(self, *args, **kwargs):
        raise AuditWriteError("simulated audit outage")

    monkeypatch.setattr(AuditService, "log_required", unavailable)
    lost = client.post(_ownership_url(item.id), json={**_owner(customer), "reason": "Recount"}, headers=hdrs)

    assert lost.status_code == 503, lost.text
    assert lost.json()["detail"] == OWNERSHIP_AUDIT_UNAVAILABLE_DETAIL
    _assert_untouched(db_session, item, ("company", None, None))

    # The refusal is transient: the identical request succeeds once the audit trail is back.
    monkeypatch.setattr(AuditService, "log_required", original)
    retried = client.post(_ownership_url(item.id), json={**_owner(customer), "reason": "Recount"}, headers=hdrs)
    assert retried.status_code == 200, retried.text
    assert len(_ownership_updates(db_session, item.id)) == 1


def test_ownership_change_cannot_reach_across_tenants(db_session: Session, client: TestClient, hdrs):
    """Company B's admin cannot re-own company A's row (404, not 403: no oracle), and the
    row A's admin re-owns stays A's with an A customer."""
    part = _part(db_session)
    item = _stock(db_session, part, qty=8, lot="TEN-1")
    a_customer = _customer(db_session)
    b_customer = _customer(db_session, company_id=COMPANY_B)
    b_headers = _headers(_user(db_session, company_id=COMPANY_B))

    from_b = client.post(_ownership_url(item.id), json={**_owner(b_customer), "reason": "Mine now"}, headers=b_headers)
    assert from_b.status_code == 404, from_b.text
    _assert_untouched(db_session, item, ("company", None, None))

    from_a = client.post(_ownership_url(item.id), json={**_owner(a_customer), "reason": "Ours"}, headers=hdrs)
    assert from_a.status_code == 200, from_a.text
    [row] = _ownership_updates(db_session, item.id)
    assert row.company_id == COMPANY_A


# ===========================================================================
# 6. Valuation leaves owned stock out; exports and reports carry the column
# ===========================================================================


def test_inventory_value_report_counts_company_stock_only(db_session: Session, client: TestClient, hdrs):
    valued = _part(db_session, standard_cost=10.0)
    owned_only = _part(db_session, standard_cost=3.0)
    customer = _customer(db_session)
    vendor = _vendor(db_session)
    _stock(db_session, valued, qty=5, lot="VAL-CO")
    _stock(db_session, valued, qty=100, lot="VAL-CU", customer=customer)
    _stock(db_session, valued, qty=7, lot="VAL-SU", vendor=vendor)
    _stock(db_session, owned_only, qty=50, lot="VAL-ONLY", customer=customer)

    response = client.get("/api/v1/reports/inventory-value", headers=hdrs)

    assert response.status_code == 200, response.text
    assert response.json() == {"total_value": 50.0, "total_quantity": 5.0, "unique_parts": 1}


def test_inventory_turnover_analytics_value_company_stock_only(db_session: Session, client: TestClient, hdrs):
    valued = _part(db_session)
    owned_only = _part(db_session)
    customer = _customer(db_session)
    vendor = _vendor(db_session)
    _stock(db_session, valued, qty=5, lot="AN-CO", unit_cost=2.0)  # 10 of the company's value
    _stock(db_session, valued, qty=100, lot="AN-CU", unit_cost=2.0, customer=customer)
    _stock(db_session, owned_only, qty=50, lot="AN-SU", unit_cost=3.0, vendor=vendor)

    response = client.get("/api/v1/analytics/inventory-turnover", headers=hdrs)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["summary"]["total_inventory_value"] == 10.0
    per_part = {row["part_id"]: row["avg_inventory_value"] for row in body["low_turnover_items"]}
    assert per_part[valued.id] == 10.0, "the part's average is over its company rows only"
    assert per_part[owned_only.id] == 1, "a part with no company stock falls back to the divide-by-zero guard"


def test_turnover_kpi_averages_company_stock_only(db_session: Session, admin: User):
    part = _part(db_session)
    customer = _customer(db_session)
    company_row = _stock(db_session, part, qty=10, lot="KPI-CO", unit_cost=2.0)  # value 20
    _stock(db_session, part, qty=100, lot="KPI-CU", unit_cost=2.0, customer=customer)  # value 200, excluded
    db_session.add(
        InventoryTransaction(
            company_id=COMPANY_A,
            inventory_item_id=company_row.id,
            part_id=part.id,
            transaction_type=TransactionType.ISSUE,
            quantity=-20,
            unit_cost=2.0,
            total_cost=40.0,
            created_by=admin.id,
        )
    )
    db_session.commit()

    # A window longer than a year, so COGS is not annualized and the arithmetic is exact
    # (and no "today" boundary can move the ledger row out of it).
    today = date.today()
    turnover = AnalyticsService(db_session, COMPANY_A)._get_turnover_value(
        today - timedelta(days=400), today + timedelta(days=2)
    )

    assert turnover == pytest.approx(40.0 / 20.0), "average inventory is the company row alone"


def test_inventory_export_carries_ownership_and_owner_name(db_session: Session, client: TestClient, hdrs):
    part = _part(db_session)
    customer = _customer(db_session)
    vendor = _vendor(db_session)
    _stock(db_session, part, qty=5, lot="EXP-CO", unit_cost=2.0)
    _stock(db_session, part, qty=4, lot="EXP-CU", unit_cost=3.0, customer=customer)
    _stock(db_session, part, qty=2, lot="EXP-SU", unit_cost=1.5, vendor=vendor)

    response = client.get("/api/v1/exports/inventory/export", params={"format": "csv"}, headers=hdrs)

    assert response.status_code == 200, response.text
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert {"ownership", "owner_name", "total_value"} <= set(rows[0])
    by_lot = {row["lot_number"]: (row["ownership"], row["owner_name"], float(row["total_value"])) for row in rows}
    assert by_lot == {
        "EXP-CO": ("company", "", 10.0),
        "EXP-CU": ("customer", customer.name, 12.0),  # per-row value kept, so a reader can filter
        "EXP-SU": ("supplier", vendor.name, 3.0),
    }


def test_report_builder_reads_and_filters_inventory_ownership(db_session: Session, client: TestClient, hdrs):
    part = _part(db_session)
    customer = _customer(db_session)
    _stock(db_session, part, qty=5, lot="RB-CO")
    _stock(db_session, part, qty=4, lot="RB-CU", customer=customer)
    body = {
        "data_source": "inventory",
        "columns": [{"field": "lot_number"}, {"field": "ownership"}],
        "sort": [{"field": "lot_number", "direction": "asc"}],
    }

    every = client.post("/api/v1/analytics/custom-report", json=body, headers=hdrs)
    customer_only = client.post(
        "/api/v1/analytics/custom-report",
        json={**body, "filters": [{"field": "ownership", "operator": "eq", "value": "customer"}]},
        headers=hdrs,
    )

    assert every.status_code == 200, every.text
    assert [(r["lot_number"], r["ownership"]) for r in every.json()["rows"]] == [
        ("RB-CO", "company"),
        ("RB-CU", "customer"),
    ]
    assert customer_only.status_code == 200, customer_only.text
    assert [r["lot_number"] for r in customer_only.json()["rows"]] == ["RB-CU"]


# ===========================================================================
# 7. Tools
# ===========================================================================


def _create_tool(client: TestClient, headers: dict, **body):
    n = _next()
    payload = {"tool_id": f"FIX-OWN-{n:04d}", "name": f"Invented fixture {n}", **body}
    return client.post(TOOLS_URL, json=payload, headers=headers)


def _tool_owner(body: dict) -> tuple:
    return (body["ownership"], body["owner_customer_id"], body["owner_vendor_id"], body["owner_name"])


def test_tool_create_records_and_audits_its_owner(db_session: Session, client: TestClient, hdrs):
    customer = _customer(db_session)
    inactive_vendor = _vendor(db_session, active=False)

    owned = _create_tool(client, hdrs, **_owner(customer))
    consigned = _create_tool(client, hdrs, **_owner(vendor=inactive_vendor))
    ours = _create_tool(client, hdrs)

    for response in (owned, consigned, ours):
        assert response.status_code == 200, response.text
    assert _tool_owner(owned.json()) == ("customer", customer.id, None, customer.name)
    assert _tool_owner(consigned.json()) == ("supplier", None, inactive_vendor.id, inactive_vendor.name)
    assert _tool_owner(ours.json()) == ("company", None, None, None)

    db_session.expire_all()
    tool = db_session.get(Tool, owned.json()["id"])
    assert (tool.ownership, tool.owner_customer_id, tool.owner_vendor_id) == ("customer", customer.id, None)
    [create] = (
        db_session.query(AuditLog)
        .filter(AuditLog.resource_type == "tool", AuditLog.resource_id == tool.id, AuditLog.action == "CREATE")
        .all()
    )
    assert (create.new_values["ownership"], create.new_values["owner_customer_id"]) == ("customer", customer.id)


@pytest.mark.parametrize(
    "scenario, status_code",
    [
        ("supplier_without_vendor", 422),
        ("customer_with_vendor_id", 422),
        ("foreign_customer", 404),
        ("deleted_customer", 404),
        ("deleted_vendor", 404),
    ],
)
def test_tool_create_refuses_an_unresolvable_owner(
    db_session: Session, client: TestClient, hdrs, scenario, status_code
):
    customer = _customer(db_session)
    vendor = _vendor(db_session)
    body = {
        "supplier_without_vendor": {"ownership": "supplier"},
        "customer_with_vendor_id": {
            "ownership": "customer",
            "owner_customer_id": customer.id,
            "owner_vendor_id": vendor.id,
        },
        "foreign_customer": _owner(_customer(db_session, company_id=COMPANY_B)),
        "deleted_customer": _owner(_customer(db_session, deleted=True)),
        "deleted_vendor": _owner(vendor=_vendor(db_session, deleted=True)),
    }[scenario]

    with _inserted_tables(db_session) as inserted:
        response = _create_tool(client, hdrs, **body)

    assert response.status_code == status_code, response.text
    assert inserted == []
    db_session.expire_all()
    assert db_session.query(Tool).count() == 0
    assert db_session.query(AuditLog).filter(AuditLog.resource_type == "tool").count() == 0


def test_tool_list_filters_by_ownership_and_every_read_names_the_owner(db_session: Session, client: TestClient, hdrs):
    customer = _customer(db_session)
    vendor = _vendor(db_session)
    owned = _create_tool(client, hdrs, **_owner(customer)).json()
    consigned = _create_tool(client, hdrs, **_owner(vendor=vendor)).json()
    ours = _create_tool(client, hdrs).json()

    def listed(**params) -> Dict[str, tuple]:
        response = client.get(TOOLS_URL, params=params, headers=hdrs)
        assert response.status_code == 200, response.text
        return {row["tool_id"]: _tool_owner(row) for row in response.json()}

    assert listed() == {
        owned["tool_id"]: ("customer", customer.id, None, customer.name),
        consigned["tool_id"]: ("supplier", None, vendor.id, vendor.name),
        ours["tool_id"]: ("company", None, None, None),
    }
    assert set(listed(ownership="customer")) == {owned["tool_id"]}
    assert set(listed(ownership="supplier")) == {consigned["tool_id"]}
    assert set(listed(ownership="company")) == {ours["tool_id"]}
    assert client.get(TOOLS_URL, params={"ownership": "partner"}, headers=hdrs).status_code == 422

    single = client.get(f"{TOOLS_URL}{owned['id']}", headers=hdrs)
    history = client.get(f"{TOOLS_URL}{consigned['id']}/history", headers=hdrs)
    assert single.status_code == 200 and single.json()["owner_name"] == customer.name
    assert history.status_code == 200 and history.json()["tool"]["owner_name"] == vendor.name


def _put_tool(client: TestClient, headers: dict, tool_id: int, **body):
    return client.put(f"{TOOLS_URL}{tool_id}", json=body, headers=headers)


def test_tool_update_with_only_an_owner_id_keeps_the_kind(db_session: Session, client: TestClient, hdrs):
    first = _customer(db_session)
    second = _customer(db_session)
    vendor_one = _vendor(db_session)
    vendor_two = _vendor(db_session)
    customer_tool = _create_tool(client, hdrs, **_owner(first)).json()
    supplier_tool = _create_tool(client, hdrs, **_owner(vendor=vendor_one)).json()
    company_tool = _create_tool(client, hdrs).json()

    repointed = _put_tool(client, hdrs, customer_tool["id"], owner_customer_id=second.id)
    assert repointed.status_code == 200, repointed.text
    assert _tool_owner(repointed.json()) == ("customer", second.id, None, second.name)
    update = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.resource_type == "tool", AuditLog.resource_id == customer_tool["id"], AuditLog.action == "UPDATE"
        )
        .one()
    )
    assert update.extra_data["changes"]["owner_customer_id"] == {"old": first.id, "new": second.id}

    resupplied = _put_tool(client, hdrs, supplier_tool["id"], owner_vendor_id=vendor_two.id)
    assert resupplied.status_code == 200, resupplied.text
    assert _tool_owner(resupplied.json()) == ("supplier", None, vendor_two.id, vendor_two.name)

    # The kind is kept, so an owner id that does not belong to it is a mismatch, not a switch.
    assert _put_tool(client, hdrs, company_tool["id"], owner_customer_id=first.id).status_code == 422
    assert _put_tool(client, hdrs, repointed.json()["id"], owner_vendor_id=vendor_one.id).status_code == 422
    db_session.expire_all()
    assert db_session.get(Tool, company_tool["id"]).ownership == "company"
    assert db_session.get(Tool, customer_tool["id"]).owner_customer_id == second.id


def test_tool_update_with_ownership_states_the_whole_triple(db_session: Session, client: TestClient, hdrs):
    customer = _customer(db_session)
    vendor = _vendor(db_session)
    tool = _create_tool(client, hdrs, **_owner(customer)).json()

    back = _put_tool(client, hdrs, tool["id"], ownership="company")
    assert back.status_code == 200, back.text
    assert _tool_owner(back.json()) == ("company", None, None, None)

    consigned = _put_tool(client, hdrs, tool["id"], ownership="supplier", owner_vendor_id=vendor.id)
    assert consigned.status_code == 200, consigned.text
    assert _tool_owner(consigned.json()) == ("supplier", None, vendor.id, vendor.name)

    assert _put_tool(client, hdrs, tool["id"], ownership="customer").status_code == 422, "the id is not inherited"
    assert _put_tool(client, hdrs, tool["id"], ownership=None).status_code == 422
    db_session.expire_all()
    assert (db_session.get(Tool, tool["id"]).ownership, db_session.get(Tool, tool["id"]).owner_vendor_id) == (
        "supplier",
        vendor.id,
    )


def test_tool_update_keeps_an_unchanged_soft_deleted_owner(db_session: Session, client: TestClient, hdrs):
    """Re-sending a tool's CURRENT owner -- a customer deleted since -- must not refuse an
    unrelated edit; only a CHANGE of owner is resolved. A change to a deleted owner is 404."""
    owner = _customer(db_session)
    tool = _create_tool(client, hdrs, **_owner(owner)).json()
    owner.soft_delete(None)
    db_session.commit()

    renamed = _put_tool(client, hdrs, tool["id"], name="Renamed fixture", **_owner(owner))
    relocated = _put_tool(client, hdrs, tool["id"], location="CRIB-2", owner_customer_id=owner.id)
    plain = _put_tool(client, hdrs, tool["id"], notes="Calibrated")

    for response in (renamed, relocated, plain):
        assert response.status_code == 200, response.text
    assert renamed.json()["name"] == "Renamed fixture"
    assert _tool_owner(plain.json()) == ("customer", owner.id, None, owner.name), "the name reads through the tombstone"

    gone = _customer(db_session, deleted=True)
    moved = _put_tool(client, hdrs, tool["id"], name="Should not save", owner_customer_id=gone.id)
    assert moved.status_code == 404, moved.text
    db_session.expire_all()
    stored = db_session.get(Tool, tool["id"])
    assert (stored.name, stored.owner_customer_id) == ("Renamed fixture", owner.id)


# ===========================================================================
# 8. Customer hard delete
# ===========================================================================


def _hard_delete(client: TestClient, headers: dict, customer: Customer):
    return client.delete(f"/api/v1/customers/{customer.id}", params={"hard_delete": "true"}, headers=headers)


# The two remedies the refusal ends with -- the code's exact sentences.
REOWN_REMEDY = (
    "Change their ownership first (POST /inventory/items/{id}/ownership for stock, "
    "PUT /tool-management/tools/{id} for tools), or soft delete the customer."
)
SOFT_DELETE_REMEDY = (
    "Some of these are records whose owner cannot be changed (PO receipts, or stock rows "
    "that are deactivated or hold no stock), so soft delete the customer instead; it keeps "
    "them naming their owner."
)


@pytest.mark.parametrize(
    "stock, tools, words, remedy",
    [
        ([(3.0, True)], 0, "1 inventory stock row", REOWN_REMEDY),
        ([(3.0, True), (4.0, True)], 0, "2 inventory stock rows", REOWN_REMEDY),
        ([(0.0, True)], 0, "1 inventory stock row", SOFT_DELETE_REMEDY),  # empty: the verb refuses it
        ([(3.0, False)], 0, "1 inventory stock row", SOFT_DELETE_REMEDY),  # deactivated: likewise
        ([], 1, "1 tool", REOWN_REMEDY),
        ([(3.0, True)], 2, "1 inventory stock row and 2 tools", REOWN_REMEDY),
        ([(0.0, True), (3.0, True)], 1, "2 inventory stock rows and 1 tool", SOFT_DELETE_REMEDY),
    ],
    ids=["one-row", "two-rows", "empty-row", "deactivated-row", "tool", "row-and-tools", "mixed"],
)
def test_customer_hard_delete_is_refused_while_it_owns_stock_or_tools(
    db_session: Session, client: TestClient, hdrs, stock, tools, words, remedy
):
    """Any owned row counts, an EMPTY or deactivated one included: the record of whose
    property the shop held must keep naming its owner, and each row holds an FK to the
    customer. The remedy names re-owning only when every blocker CAN be re-owned."""
    customer = _customer(db_session)
    part = _part(db_session)
    for index, (qty, is_active) in enumerate(stock):
        _stock(db_session, part, qty=qty, lot=f"HD-{index}", customer=customer, is_active=is_active)
    for _ in range(tools):
        assert _create_tool(client, hdrs, **_owner(customer)).status_code == 200

    response = _hard_delete(client, hdrs, customer)

    assert response.status_code == 400, response.text
    assert (
        response.json()["detail"]
        == f"Cannot hard delete: Customer owns {words} recorded as customer property. {remedy}"
    )
    db_session.expire_all()
    assert db_session.get(Customer, customer.id) is not None
    # Soft delete stays available, and keeps the owner rows pointing at a real customer.
    assert client.delete(f"/api/v1/customers/{customer.id}", headers=hdrs).status_code == 200


@pytest.mark.parametrize("held_for_inspection", [False, True], ids=["dock-to-stock", "held"])
def test_customer_hard_delete_is_refused_while_a_po_receipt_names_it_as_owner(
    db_session: Session, client: TestClient, hdrs, held_for_inspection
):
    """A receipt is a historical record whose owner cannot change: it blocks a hard delete
    and the only remedy offered is the soft delete. A dock-to-stock receipt also placed an
    owned stock row; a held one placed nothing yet."""
    customer = _customer(db_session)
    line = _po_line(db_session)
    received = _po_receive(
        client, hdrs, line, quantity_received=3, requires_inspection=held_for_inspection, **_owner(customer)
    )
    assert received.status_code == 200, received.text

    response = _hard_delete(client, hdrs, customer)

    words = "1 PO receipt" if held_for_inspection else "1 inventory stock row and 1 PO receipt"
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == (
        f"Cannot hard delete: Customer owns {words} recorded as customer property. {SOFT_DELETE_REMEDY}"
    )


def test_customer_hard_delete_names_every_kind_of_owned_record(db_session: Session, client: TestClient, hdrs):
    """A voided receipt still counts, and three kinds join as "a, b and c"."""
    customer = _customer(db_session)
    line = _po_line(db_session)
    received = _po_receive(client, hdrs, line, quantity_received=3, **_owner(customer))
    assert received.status_code == 200, received.text
    assert (
        client.post(
            f"/api/v1/receiving/receipt/{received.json()['id']}/void", json={"reason": "Wrong PO"}, headers=hdrs
        ).status_code
        == 200
    )
    assert _create_tool(client, hdrs, **_owner(customer)).status_code == 200

    response = _hard_delete(client, hdrs, customer)

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == (
        "Cannot hard delete: Customer owns 1 inventory stock row, 1 tool and 1 PO receipt recorded as "
        f"customer property. {SOFT_DELETE_REMEDY}"
    )


def test_customer_hard_delete_goes_through_once_its_property_is_re_owned(db_session: Session, client: TestClient, hdrs):
    customer = _customer(db_session)
    part = _part(db_session)
    item = _stock(db_session, part, qty=3, lot="HD-BACK", customer=customer)
    tool = _create_tool(client, hdrs, **_owner(customer)).json()

    assert (
        client.post(
            _ownership_url(item.id), json={"ownership": "company", "reason": "Bought"}, headers=hdrs
        ).status_code
        == 200
    )
    assert _put_tool(client, hdrs, tool["id"], ownership="company").status_code == 200
    response = _hard_delete(client, hdrs, customer)

    assert response.status_code == 200, response.text
    db_session.expire_all()
    assert db_session.get(Customer, customer.id) is None


def test_customer_hard_delete_counts_only_this_companys_property(db_session: Session, client: TestClient, hdrs):
    """A foreign row carrying this customer's id (a corrupt reference) neither blocks the
    delete nor is disclosed by it -- the counts are company-scoped."""
    customer = _customer(db_session)
    _stock(db_session, _part(db_session, company_id=COMPANY_B), qty=4, lot="HD-FOREIGN", customer=customer)

    response = _hard_delete(client, hdrs, customer)

    assert response.status_code == 200, response.text


# ===========================================================================
# 9. Hank's receive_delivery stays company-only
# ===========================================================================


def test_hank_receipt_line_schema_has_no_ownership_fields():
    """Recording someone else's property is a person's call on the receiving screen: the
    AI-proposed line inherits the receipt fields WITHOUT ownership and forbids extras."""
    assert {"ownership", "owner_customer_id", "owner_vendor_id"} & set(HankReceiptLine.model_fields) == set()
    with pytest.raises(ValidationError) as excinfo:
        ReceiveDeliveryInput.model_validate(
            {
                "purchase_order_id": 1,
                "lines": [
                    {
                        "po_line_id": 1,
                        "quantity_received": 3,
                        "requires_inspection": False,
                        "ownership": "customer",
                        "owner_customer_id": 4,
                    }
                ],
            }
        )
    assert {error["type"] for error in excinfo.value.errors()} == {"extra_forbidden"}


def test_hank_receive_delivery_posts_company_stock_beside_an_owned_lot(
    db_session: Session, client: TestClient, auth_headers
):
    line = _po_line(db_session)
    part = db_session.get(Part, line.part_id)
    customer = _customer(db_session)
    _stock(db_session, part, qty=6, lot="HANK-LOT", location=RECEIVING_DEFAULT_LOCATION, customer=customer)
    owned_line = {
        "po_line_id": line.id,
        "quantity_received": 3,
        "requires_inspection": False,
        "lot_number": "HANK-LOT",
    }

    smuggled = hank_propose(
        client,
        auth_headers,
        "receive_delivery",
        {"purchase_order_id": line.purchase_order_id, "lines": [{**owned_line, **_owner(customer)}]},
    )
    assert smuggled.status_code == 422, smuggled.text

    task = hank_propose(
        client, auth_headers, "receive_delivery", {"purchase_order_id": line.purchase_order_id, "lines": [owned_line]}
    )
    assert task.status_code == 200, task.text
    executed = hank_execute(client, auth_headers, task.json())
    assert executed.status_code == 200, executed.text

    assert _rows_by_owner(db_session, part, location=RECEIVING_DEFAULT_LOCATION) == {
        ("customer", customer.id, None): 6.0,
        ("company", None, None): 3.0,
    }
