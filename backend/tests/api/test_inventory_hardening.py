"""Tenant-isolation / RBAC / ledger-filter coverage for the inventory endpoints.

Locks in the PR-0 hardening of ``app/api/endpoints/inventory.py``. Before it,
several lookups on the stock-mutating paths were keyed on natural keys that are
only unique *per company* -- a location ``code``, a ``(part_id, location,
lot_number)`` triple, a warehouse name, a ``cycle_count_items`` row id -- with no
``company_id`` predicate at all. The consequences ranged from "receive silently
placed stock at a foreign warehouse" to outright cross-tenant WRITES:

- ``POST /inventory/receive``   -- location code + existing-lot row resolved
  unscoped, so another tenant's stock row could be the row we incremented.
- ``POST /inventory/transfer``  -- same for the destination location and the
  destination stock row.
- ``GET  /inventory/low-stock`` -- the on-hand subquery summed *every* company's
  rows for a part, so a foreign quantity could mask a real shortage.
- ``POST /inventory/cycle-counts`` -- enrolled every company's inventory rows
  that matched the warehouse/location/part scope, and never stamped
  ``CycleCountItem.company_id`` (a NOT NULL TenantMixin column).
- ``POST /inventory/cycle-counts/{id}/items/{item_id}/count`` -- had **no
  company dependency whatsoever**, so in code any authenticated user could write
  counted quantities onto another company's count. Not reachable in the field:
  the only writer of ``cycle_count_items`` is ``POST /inventory/cycle-counts``,
  which always failed on the NOT NULL ``company_id`` (below), so no foreign count
  item existed to be written onto.
- ``POST /inventory/cycle-counts/{id}/complete`` -- wrote the counted quantity
  onto whatever ``inventory_item_id`` the count item pointed at, and built the
  COUNT ``InventoryTransaction`` without a ``company_id``.

Also covered here: ``POST /inventory/issue``, ``/inventory/receive`` and
``/inventory/transfer`` are now role-gated to ADMIN/MANAGER/SUPERVISOR (matching
the sibling ``/inventory/adjust`` stock mutator and ``POST /receiving/receive``,
the PO path into the same tables), and ``GET /inventory/transactions`` gained the
reference/work-order/lot/date filters plus bounded ``limit`` + ``offset`` paging
with a deterministic newest-first order.

The second hardening pass adds the cycle-count **lifecycle and audit** coverage.
Fixing the missing ``company_id`` stamps made the enroll/complete paths work for
the first time (both columns are NOT NULL, so every insert used to raise
IntegrityError and 500), which exposed that those paths had neither audit rows
nor terminal-state guards:

- ``complete`` now writes the ``/inventory/adjust`` dual-row audit convention per
  adjusted item (``inventory`` CREATE for the COUNT movement + ``inventory``
  UPDATE for the stock level) plus a ``cycle_count`` STATUS_CHANGE, and refuses a
  terminal count with **409** so a double-click cannot double-post the same
  physical variance to the ledger.
- ``start`` refuses a terminal count, preserves ``started_at`` across a
  re-assignment, and writes its own STATUS_CHANGE; ``record_count`` refuses anything
  but an IN_PROGRESS parent, so a closed quality record can't be overwritten, and
  audits every counted quantity it writes (including what a re-count overwrote).
- ``start`` and ``record_count`` are gated by **exclusion** -- every working role
  keeps them (counting is the documented operator task) and only the read-only
  VIEWER, which the frontend grants ``inventory:view`` and nothing else, is refused.
- ``create`` 404s on an unknown/foreign ``location_code`` instead of silently
  ignoring it, writes a CREATE audit row recording the scope it enrolled, and
  ``CycleCount.total_variance_value`` now stores the variance that actually
  **posted**, priced on the ledger row's own cost basis (the measured total, priced
  on the enrollment-time snapshot, is returned alongside it).

Fixture conventions follow ``test_completion_tenant_isolation.py`` /
``test_receipt_correction_void.py``: company A is the seeded default (id=1),
company B is a second tenant (id=2), rows are created directly on the shared
``db_session``, requests are made with directly-minted tokens, and a cross-tenant
call must answer **404 (not 403)** *and* leave the foreign rows byte-identical.

A note on the deliberately "poisoned" rows below: ``InventoryItem`` rows are not
naturally shared across tenants, so a few tests seed a company-B stock row that
carries company A's ``part_id`` (or a company-A ``CycleCountItem`` pointing at a
company-B stock row -- the shape the unscoped ``create_cycle_count`` enrollment
scan *selected*, though it never persisted one: ``company_id`` is NOT NULL on
``cycle_count_items``, so every such insert raised IntegrityError and the whole
create 500'd and rolled back). That is the point: the pre-fix filters keyed only on
``(part_id, location, lot_number)`` / ``inventory_item_id``, so *any* row
matching those was a candidate. Constructing that row is what proves the
``company_id`` predicate -- and nothing else -- is what excludes it.
"""

from datetime import date, datetime, timedelta
from typing import Optional

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.inventory import (
    CycleCount,
    CycleCountItem,
    CycleCountStatus,
    InventoryItem,
    InventoryLocation,
    InventoryTransaction,
    TransactionType,
)
from app.models.part import Part
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder, WorkOrderStatus

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2

TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"  # tokens are minted directly; never used for login

# Fixed clock for the ledger-ordering / date-range tests so nothing depends on
# wall time (and so `-n auto` workers can't interleave into an ambiguous order).
LEDGER_EPOCH = datetime(2026, 3, 1, 12, 0, 0)

# Module-level counter so every fixture row gets a globally unique natural key,
# even across tests sharing a worker DB under `-n auto`.
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


def make_user(db: Session, *, company_id: int, role: UserRole = UserRole.ADMIN) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"inv-hard-{n}@co{company_id}.test",
        employee_id=f"INVH-{n:05d}",
        first_name="Inv",
        last_name=f"C{company_id}",
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


def make_part(db: Session, *, company_id: int, reorder_point: float = 0.0, safety_stock: float = 0.0) -> Part:
    _ensure_company(db, company_id)
    n = _next()
    part = Part(
        part_number=f"INVH-P-{n:05d}",
        name=f"Part {n}",
        description="inventory-hardening fixture part",
        part_type="purchased",
        unit_of_measure="each",
        reorder_point=reorder_point,
        reorder_quantity=50.0,
        safety_stock=safety_stock,
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def make_location(
    db: Session,
    *,
    company_id: int,
    code: Optional[str] = None,
    warehouse: str = "MAIN",
) -> InventoryLocation:
    """A stock location. ``code`` is unique per company, so the same code may be
    passed for two companies on purpose (that collision is the whole defect)."""
    _ensure_company(db, company_id)
    n = _next()
    loc = InventoryLocation(
        code=code or f"INVH-L-{n:05d}",
        name=f"Location {n}",
        warehouse=warehouse,
        is_active=True,
        is_pickable=True,
        is_receivable=True,
        company_id=company_id,
    )
    db.add(loc)
    db.commit()
    db.refresh(loc)
    return loc


def make_inventory_item(
    db: Session,
    *,
    company_id: int,
    part: Part,
    location: str,
    warehouse: str = "MAIN",
    qty: float = 10.0,
    lot_number: Optional[str] = None,
    unit_cost: float = 2.0,
) -> InventoryItem:
    n = _next()
    item = InventoryItem(
        part_id=part.id,
        location=location,
        warehouse=warehouse,
        quantity_on_hand=qty,
        quantity_allocated=0.0,
        quantity_available=qty,
        lot_number=lot_number if lot_number is not None else f"INVH-LOT-{n:05d}",
        unit_cost=unit_cost,
        is_active=True,
        company_id=company_id,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def make_work_order(db: Session, *, company_id: int, part: Part) -> WorkOrder:
    n = _next()
    wo = WorkOrder(
        work_order_number=f"INVH-WO-{n:05d}",
        part_id=part.id,
        quantity_ordered=10,
        status=WorkOrderStatus.RELEASED,
        company_id=company_id,
    )
    db.add(wo)
    db.commit()
    db.refresh(wo)
    return wo


def make_txn(
    db: Session,
    *,
    company_id: int,
    part: Part,
    user: User,
    transaction_type: TransactionType = TransactionType.ADJUST,
    quantity: float = 1.0,
    reference_type: Optional[str] = None,
    reference_id: Optional[int] = None,
    reference_number: Optional[str] = None,
    lot_number: Optional[str] = None,
    created_at: Optional[datetime] = None,
) -> InventoryTransaction:
    """Seed a ledger row directly.

    NOTE on the two partial UNIQUE indexes declared on ``InventoryTransaction``
    (``uq_wo_inventory_receipt`` / ``uq_wo_inventory_issue``): since migration
    ``076_uq_wo_inv_sqlite_parity`` they declare BOTH ``postgresql_where`` and
    ``sqlite_where``, so they are genuinely PARTIAL under the SQLite test engine --
    scoped to ``reference_type = 'work_order'`` with RECEIVE/ISSUE, exactly as in
    production. Rows seeded with any OTHER ``reference_type`` are outside both
    predicates and can repeat freely.

    (Before 076 the SQLite engine ignored the ``postgresql_where`` and materialized
    these as *unconditional* unique indexes over
    ``(company_id, reference_type, reference_id, transaction_type[, part_id])``,
    which forced fixtures here to keep reference-bearing rows artificially distinct.
    That constraint is gone; the existing fixtures are simply still valid.)
    """
    txn = InventoryTransaction(
        company_id=company_id,
        part_id=part.id,
        transaction_type=transaction_type,
        quantity=quantity,
        reference_type=reference_type,
        reference_id=reference_id,
        reference_number=reference_number,
        lot_number=lot_number,
        created_by=user.id,
        created_at=created_at or LEDGER_EPOCH,
    )
    db.add(txn)
    db.commit()
    db.refresh(txn)
    return txn


def _reload(db: Session, model, pk: int):
    """Re-read a row fresh from the DB so we observe the committed state."""
    db.expire_all()
    return db.query(model).filter(model.id == pk).first()


def _status_label(count_status: CycleCountStatus) -> str:
    return count_status.value


def _audit_rows(db: Session, *, resource_type: str, action: Optional[str] = None) -> list:
    db.expire_all()
    query = db.query(AuditLog).filter(AuditLog.resource_type == resource_type)
    if action:
        query = query.filter(AuditLog.action == action)
    return query.order_by(AuditLog.sequence_number).all()


def _assert_hash_chain_intact(db: Session) -> None:
    """The audit log is tamper-evident: contiguous sequence, linked hashes."""
    logs = db.query(AuditLog).order_by(AuditLog.sequence_number).all()
    seqs = [log.sequence_number for log in logs]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)
    assert all(log.integrity_hash for log in logs)
    for prev, curr in zip(logs, logs[1:]):
        assert curr.previous_hash == prev.integrity_hash


# ---------------------------------------------------------------------------
# POST /inventory/receive -- tenant isolation
# ---------------------------------------------------------------------------


def test_receive_into_other_companys_location_is_404(client: TestClient, db_session: Session):
    """A location code that exists only in company B must be "not found" for A.

    Pre-fix the location lookup had no company predicate, so this receive
    succeeded: it created a company-A stock row at a location code company A does
    not own, stamped with company B's warehouse.
    """
    a_user = make_user(db_session, company_id=COMPANY_A)
    a_part = make_part(db_session, company_id=COMPANY_A)
    b_loc = make_location(db_session, company_id=COMPANY_B, code="INVH-B-ONLY", warehouse="B-WAREHOUSE")

    resp = client.post(
        "/api/v1/inventory/receive",
        headers=headers_for(a_user),
        json={
            "part_id": a_part.id,
            "quantity": 25,
            "location_code": b_loc.code,
            "lot_number": "INVH-XT-LOT",
            "unit_cost": 3.0,
        },
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
    assert resp.json()["detail"] == "Location not found"

    # No stock and no ledger row was produced anywhere for the foreign location.
    db_session.expire_all()
    assert db_session.query(InventoryItem).filter(InventoryItem.location == b_loc.code).count() == 0
    assert db_session.query(InventoryTransaction).filter(InventoryTransaction.part_id == a_part.id).count() == 0
    # Company B's location row itself is untouched.
    loc_after = _reload(db_session, InventoryLocation, b_loc.id)
    assert loc_after.company_id == COMPANY_B
    assert loc_after.warehouse == "B-WAREHOUSE"


def test_receive_does_not_increment_another_companys_lot_row(client: TestClient, db_session: Session):
    """The existing-lot lookup must never resolve to another tenant's stock row.

    Both companies own a location with the same code (legal: the unique key is
    ``(company_id, code)``). Company B holds a row matching the exact
    ``(part_id, location, lot_number)`` triple the pre-fix query keyed on -- so
    pre-fix, company A's receive incremented **company B's** stock. Post-fix, A
    gets its own row and B's is byte-identical.
    """
    a_user = make_user(db_session, company_id=COMPANY_A)
    a_part = make_part(db_session, company_id=COMPANY_A)
    shared_code = "INVH-SHARED-01"
    lot = "INVH-SHARED-LOT"

    # A's location is created first so the pre-fix unscoped `.first()` still
    # resolved A's location -- isolating this test to the *stock row* lookup.
    make_location(db_session, company_id=COMPANY_A, code=shared_code, warehouse="MAIN")
    make_location(db_session, company_id=COMPANY_B, code=shared_code, warehouse="MAIN")
    b_item = make_inventory_item(
        db_session,
        company_id=COMPANY_B,
        part=a_part,  # poisoned: the pre-fix filter keyed on part_id alone
        location=shared_code,
        qty=5.0,
        lot_number=lot,
    )

    resp = client.post(
        "/api/v1/inventory/receive",
        headers=headers_for(a_user),
        json={"part_id": a_part.id, "quantity": 12, "location_code": shared_code, "lot_number": lot, "unit_cost": 1.5},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    new_item_id = resp.json()["inventory_item_id"]

    # Company B's row is untouched.
    b_after = _reload(db_session, InventoryItem, b_item.id)
    assert float(b_after.quantity_on_hand) == 5.0, "company B's stock must not be incremented by company A's receive"
    assert float(b_after.quantity_available) == 5.0
    assert new_item_id != b_item.id

    # Company A got its own, correctly-stamped row.
    a_after = _reload(db_session, InventoryItem, new_item_id)
    assert a_after.company_id == COMPANY_A
    assert float(a_after.quantity_on_hand) == 12.0
    assert a_after.location == shared_code
    assert a_after.lot_number == lot


def test_receive_against_a_soft_deleted_part_is_400(client: TestClient, db_session: Session):
    """``Part`` is the one SoftDeleteMixin model this router touches.

    The lookup resolved the part by ``(id, company_id)`` with no ``is_deleted``
    predicate -- 20+ call sites elsewhere (``materials.py``, ``po_upload.py``,
    ``shop_floor.py``) carry it -- so a Manager could create brand-new stock *and* a
    ledger row against a part the business had deleted. The refusal matches the
    repo-wide deleted-part policy: **400**, "restore it or use a different part
    number" (``po_upload.py``), not a 404 that hides why.
    """
    user = make_user(db_session, company_id=COMPANY_A, role=UserRole.MANAGER)
    part = make_part(db_session, company_id=COMPANY_A)
    loc = make_location(db_session, company_id=COMPANY_A)
    part.soft_delete(user.id)
    db_session.commit()

    resp = client.post(
        "/api/v1/inventory/receive",
        headers=headers_for(user),
        json={"part_id": part.id, "quantity": 5, "location_code": loc.code, "unit_cost": 1.0},
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
    detail = resp.json()["detail"]
    assert part.part_number in detail
    assert "restore it or use a different part number" in detail

    # No stock, no ledger row, and the deleted part is not resurrected.
    db_session.expire_all()
    assert db_session.query(InventoryItem).count() == 0
    assert db_session.query(InventoryTransaction).count() == 0
    assert _reload(db_session, Part, part.id).is_deleted is True


def test_receive_unknown_part_is_still_404(client: TestClient, db_session: Session):
    """The deleted-part branch must not swallow the plain "no such part" case."""
    user = make_user(db_session, company_id=COMPANY_A, role=UserRole.MANAGER)
    loc = make_location(db_session, company_id=COMPANY_A)

    resp = client.post(
        "/api/v1/inventory/receive",
        headers=headers_for(user),
        json={"part_id": 999_999, "quantity": 5, "location_code": loc.code, "unit_cost": 1.0},
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
    assert resp.json()["detail"] == "Part not found"


# ---------------------------------------------------------------------------
# POST /inventory/transfer -- tenant isolation
# ---------------------------------------------------------------------------


def test_transfer_to_other_companys_location_is_404_and_source_untouched(client: TestClient, db_session: Session):
    """A destination code owned only by company B is "not found", and the 404
    happens before the source decrement (pre-fix this moved stock to a foreign
    location code and returned 200)."""
    a_user = make_user(db_session, company_id=COMPANY_A)
    a_part = make_part(db_session, company_id=COMPANY_A)
    a_loc = make_location(db_session, company_id=COMPANY_A)
    a_item = make_inventory_item(db_session, company_id=COMPANY_A, part=a_part, location=a_loc.code, qty=10.0)
    b_loc = make_location(db_session, company_id=COMPANY_B, code="INVH-B-DEST", warehouse="B-WAREHOUSE")

    resp = client.post(
        "/api/v1/inventory/transfer",
        headers=headers_for(a_user),
        json={"inventory_item_id": a_item.id, "quantity": 4, "to_location_code": b_loc.code},
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
    assert resp.json()["detail"] == "Destination location not found"

    # Source stock intact, no destination row conjured, no ledger row.
    src_after = _reload(db_session, InventoryItem, a_item.id)
    assert float(src_after.quantity_on_hand) == 10.0
    assert float(src_after.quantity_available) == 10.0
    assert db_session.query(InventoryItem).filter(InventoryItem.location == b_loc.code).count() == 0
    assert db_session.query(InventoryTransaction).count() == 0


def test_transfer_does_not_increment_another_companys_destination_row(client: TestClient, db_session: Session):
    """The destination stock-row lookup must be tenant-scoped.

    Company B holds a row matching the ``(part_id, location, lot_number)`` triple
    the pre-fix destination query keyed on, so pre-fix company A's transfer
    credited **company B's** stock (while debiting A's).
    """
    a_user = make_user(db_session, company_id=COMPANY_A)
    a_part = make_part(db_session, company_id=COMPANY_A)
    src_loc = make_location(db_session, company_id=COMPANY_A)
    dest_code = "INVH-SHARED-DEST"
    lot = "INVH-XFER-LOT"

    make_location(db_session, company_id=COMPANY_A, code=dest_code, warehouse="MAIN")
    make_location(db_session, company_id=COMPANY_B, code=dest_code, warehouse="MAIN")
    a_item = make_inventory_item(
        db_session, company_id=COMPANY_A, part=a_part, location=src_loc.code, qty=10.0, lot_number=lot
    )
    b_dest = make_inventory_item(
        db_session,
        company_id=COMPANY_B,
        part=a_part,  # poisoned: pre-fix the destination filter keyed on part_id alone
        location=dest_code,
        qty=7.0,
        lot_number=lot,
    )

    resp = client.post(
        "/api/v1/inventory/transfer",
        headers=headers_for(a_user),
        json={"inventory_item_id": a_item.id, "quantity": 4, "to_location_code": dest_code},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    b_after = _reload(db_session, InventoryItem, b_dest.id)
    assert float(b_after.quantity_on_hand) == 7.0, "company B's destination stock must not absorb company A's transfer"

    src_after = _reload(db_session, InventoryItem, a_item.id)
    assert float(src_after.quantity_on_hand) == 6.0

    # A brand-new company-A destination row holds the transferred quantity.
    a_dest = (
        db_session.query(InventoryItem)
        .filter(
            InventoryItem.company_id == COMPANY_A,
            InventoryItem.location == dest_code,
            InventoryItem.lot_number == lot,
        )
        .one()
    )
    assert float(a_dest.quantity_on_hand) == 4.0


# ---------------------------------------------------------------------------
# POST /inventory/receive and /inventory/transfer -- RBAC
# ---------------------------------------------------------------------------
#
# Both endpoints were bare-``get_current_user``: any authenticated user, VIEWER
# included, could create stock and write the ledger. docs/RBAC_PERMISSIONS.md
# already documented Transfer as Admin/Manager/Supervisor, and POST
# /receiving/receive -- the PO path into the SAME tables -- was already gated that
# way, so the server was under-enforcing its own documented policy.

UNAUTHORIZED_STOCK_ROLES = [UserRole.OPERATOR, UserRole.VIEWER, UserRole.QUALITY]
AUTHORIZED_STOCK_ROLES = [UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]


@pytest.mark.parametrize("role", UNAUTHORIZED_STOCK_ROLES)
def test_receive_forbidden_for_unauthorized_roles(client: TestClient, db_session: Session, role: UserRole):
    user = make_user(db_session, company_id=COMPANY_A, role=role)
    part = make_part(db_session, company_id=COMPANY_A)
    loc = make_location(db_session, company_id=COMPANY_A)

    resp = client.post(
        "/api/v1/inventory/receive",
        headers=headers_for(user),
        json={"part_id": part.id, "quantity": 5, "location_code": loc.code, "unit_cost": 1.0},
    )
    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text
    assert resp.json()["detail"] == "Insufficient permissions"

    # A refused receive conjures neither stock nor a ledger row.
    db_session.expire_all()
    assert db_session.query(InventoryItem).count() == 0
    assert db_session.query(InventoryTransaction).count() == 0


@pytest.mark.parametrize("role", AUTHORIZED_STOCK_ROLES)
def test_receive_allowed_for_authorized_roles(client: TestClient, db_session: Session, role: UserRole):
    user = make_user(db_session, company_id=COMPANY_A, role=role)
    part = make_part(db_session, company_id=COMPANY_A)
    loc = make_location(db_session, company_id=COMPANY_A)

    resp = client.post(
        "/api/v1/inventory/receive",
        headers=headers_for(user),
        json={"part_id": part.id, "quantity": 5, "location_code": loc.code, "unit_cost": 1.0},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert float(_reload(db_session, InventoryItem, resp.json()["inventory_item_id"]).quantity_on_hand) == 5.0


def _transfer_fixture(db: Session, role: UserRole):
    user = make_user(db, company_id=COMPANY_A, role=role)
    part = make_part(db, company_id=COMPANY_A)
    src = make_location(db, company_id=COMPANY_A)
    dst = make_location(db, company_id=COMPANY_A)
    item = make_inventory_item(db, company_id=COMPANY_A, part=part, location=src.code, qty=10.0)
    return user, item, dst


@pytest.mark.parametrize("role", UNAUTHORIZED_STOCK_ROLES)
def test_transfer_forbidden_for_unauthorized_roles(client: TestClient, db_session: Session, role: UserRole):
    user, item, dst = _transfer_fixture(db_session, role)

    resp = client.post(
        "/api/v1/inventory/transfer",
        headers=headers_for(user),
        json={"inventory_item_id": item.id, "quantity": 4, "to_location_code": dst.code},
    )
    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text
    assert resp.json()["detail"] == "Insufficient permissions"

    after = _reload(db_session, InventoryItem, item.id)
    assert float(after.quantity_on_hand) == 10.0, "a refused transfer must not move stock"
    assert db_session.query(InventoryTransaction).count() == 0


@pytest.mark.parametrize("role", AUTHORIZED_STOCK_ROLES)
def test_transfer_allowed_for_authorized_roles(client: TestClient, db_session: Session, role: UserRole):
    user, item, dst = _transfer_fixture(db_session, role)

    resp = client.post(
        "/api/v1/inventory/transfer",
        headers=headers_for(user),
        json={"inventory_item_id": item.id, "quantity": 4, "to_location_code": dst.code},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert float(_reload(db_session, InventoryItem, item.id).quantity_on_hand) == 6.0


# ---------------------------------------------------------------------------
# GET /inventory/low-stock -- the on-hand aggregate is tenant-scoped
# ---------------------------------------------------------------------------


def test_low_stock_aggregate_ignores_other_companys_quantity(client: TestClient, db_session: Session):
    """The per-part on-hand SUM must not include another company's rows.

    Pre-fix the subquery grouped by ``part_id`` with no company predicate, so a
    foreign row carrying this part_id inflated the total above the reorder point
    and the shortage silently disappeared from the alert list.
    """
    a_user = make_user(db_session, company_id=COMPANY_A)
    a_part = make_part(db_session, company_id=COMPANY_A, reorder_point=100.0, safety_stock=20.0)
    a_loc = make_location(db_session, company_id=COMPANY_A)
    make_inventory_item(db_session, company_id=COMPANY_A, part=a_part, location=a_loc.code, qty=10.0)
    # Foreign row for the same part_id -- large enough to mask the shortage.
    make_inventory_item(db_session, company_id=COMPANY_B, part=a_part, location="B-LOC", qty=500.0)

    resp = client.get("/api/v1/inventory/low-stock", headers=headers_for(a_user))
    assert resp.status_code == status.HTTP_200_OK, resp.text

    alerts = {a["part_id"]: a for a in resp.json()}
    assert a_part.id in alerts, "company A's shortage must not be masked by company B's stock"
    alert = alerts[a_part.id]
    assert float(alert["quantity_on_hand"]) == 10.0
    assert float(alert["shortage"]) == 90.0
    assert alert["is_critical"] is True  # 10 <= safety_stock 20


def test_low_stock_excludes_soft_deleted_parts(client: TestClient, db_session: Session):
    """A deleted part must not raise a purchasing signal.

    The query filtered ``is_active`` only. Deleting a part also clears ``is_active``,
    so this was incidentally covered -- but ``is_active`` is an operational flag
    anyone can toggle back, while ``is_deleted`` is the deletion record. The part
    below is deliberately left ACTIVE **and** deleted, which is the state only the
    ``is_deleted`` predicate excludes.
    """
    user = make_user(db_session, company_id=COMPANY_A, role=UserRole.MANAGER)
    live = make_part(db_session, company_id=COMPANY_A, reorder_point=100.0)
    deleted = make_part(db_session, company_id=COMPANY_A, reorder_point=100.0)
    loc = make_location(db_session, company_id=COMPANY_A)
    make_inventory_item(db_session, company_id=COMPANY_A, part=live, location=loc.code, qty=1.0)
    make_inventory_item(db_session, company_id=COMPANY_A, part=deleted, location=loc.code, qty=1.0)
    deleted.soft_delete(user.id)
    deleted.is_active = True  # deleted but still flagged active -- only is_deleted excludes it
    db_session.commit()

    resp = client.get("/api/v1/inventory/low-stock", headers=headers_for(user))
    assert resp.status_code == status.HTTP_200_OK, resp.text

    part_ids = {row["part_id"] for row in resp.json()}
    assert live.id in part_ids
    assert deleted.id not in part_ids, "a soft-deleted part must not raise a low-stock alert"


def test_low_stock_rejects_non_positive_limit(client: TestClient, db_session: Session):
    """``limit`` was ``Query(500, le=2000)`` with no lower bound, so ``?limit=-1``
    reached ``.limit(-1)`` -- which PostgreSQL rejects outright (500) and SQLite
    silently reads as "unbounded". It is now ``ge=1``."""
    user = make_user(db_session, company_id=COMPANY_A)

    for bad in (0, -1):
        resp = client.get("/api/v1/inventory/low-stock", headers=headers_for(user), params={"limit": bad})
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, resp.text

    ok = client.get("/api/v1/inventory/low-stock", headers=headers_for(user), params={"limit": 1})
    assert ok.status_code == status.HTTP_200_OK, ok.text


# ---------------------------------------------------------------------------
# POST /inventory/cycle-counts -- enrollment is tenant-scoped and stamped
# ---------------------------------------------------------------------------


def _make_two_company_stock(db: Session, warehouse: str, location_code: str):
    """Matching warehouse/location shapes in BOTH companies.

    Warehouse names and location codes are per-company natural keys, so this is
    ordinary data -- and exactly what the pre-fix unscoped enrollment scan swept
    up. Returns (company-A item ids, company-B item ids).
    """
    a_part = make_part(db, company_id=COMPANY_A)
    b_part = make_part(db, company_id=COMPANY_B)
    make_location(db, company_id=COMPANY_A, code=location_code, warehouse=warehouse)
    make_location(db, company_id=COMPANY_B, code=location_code, warehouse=warehouse)

    a_ids = [
        make_inventory_item(
            db, company_id=COMPANY_A, part=a_part, location=location_code, warehouse=warehouse, qty=qty
        ).id
        for qty in (10.0, 4.0)
    ]
    b_ids = [
        make_inventory_item(
            db, company_id=COMPANY_B, part=b_part, location=location_code, warehouse=warehouse, qty=qty
        ).id
        for qty in (33.0, 44.0)
    ]
    return a_ids, b_ids


def test_create_cycle_count_enrolls_only_active_company_items(client: TestClient, db_session: Session):
    """Both tenants have stock in warehouse ``INVH-WH`` at location ``INVH-CC``.

    Pre-fix, the enrollment scan filtered only on warehouse/location/part, so
    company A's count enrolled company B's rows too (and ``complete`` would then
    have written the counted quantity onto them).
    """
    a_user = make_user(db_session, company_id=COMPANY_A, role=UserRole.MANAGER)
    a_ids, b_ids = _make_two_company_stock(db_session, warehouse="INVH-WH", location_code="INVH-CC")

    resp = client.post(
        "/api/v1/inventory/cycle-counts",
        headers=headers_for(a_user),
        json={"warehouse": "INVH-WH", "scheduled_date": date.today().isoformat()},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["total_items"] == len(a_ids), "only the active company's stock rows may be enrolled"

    enrolled = db_session.query(CycleCountItem).filter(CycleCountItem.cycle_count_id == body["id"]).all()
    enrolled_inventory_ids = {ci.inventory_item_id for ci in enrolled}
    assert enrolled_inventory_ids == set(a_ids)
    assert enrolled_inventory_ids.isdisjoint(set(b_ids)), "company B's stock rows must not be enrolled"


def test_create_cycle_count_stamps_company_id_on_every_count_item(client: TestClient, db_session: Session):
    """``CycleCountItem`` is a TenantMixin table (``company_id`` NOT NULL).

    Pre-fix the endpoint never set it, so the INSERT violated the constraint --
    a latent 500 on every cycle count that enrolled at least one row.
    """
    a_user = make_user(db_session, company_id=COMPANY_A, role=UserRole.SUPERVISOR)
    a_ids, _b_ids = _make_two_company_stock(db_session, warehouse="INVH-WH2", location_code="INVH-CC2")

    resp = client.post(
        "/api/v1/inventory/cycle-counts",
        headers=headers_for(a_user),
        json={"location_code": "INVH-CC2", "scheduled_date": date.today().isoformat()},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["company_id"] == COMPANY_A

    enrolled = db_session.query(CycleCountItem).filter(CycleCountItem.cycle_count_id == body["id"]).all()
    assert len(enrolled) == len(a_ids)
    assert all(ci.company_id == COMPANY_A for ci in enrolled)
    assert not any(ci.company_id is None for ci in enrolled)


@pytest.mark.parametrize("owner", ["nobody", COMPANY_B])
def test_create_cycle_count_unknown_or_foreign_location_is_404(client: TestClient, db_session: Session, owner):
    """A ``location_code`` that does not resolve for the active company is a 404.

    It used to be silently ignored (``if loc:`` with no ``else``), so the count was
    created with the caller's declared scope dropped: no ``location_id``, and the
    enrollment scan still filtered on the raw code string -- a count whose stated
    scope did not match the rows it enrolled.
    """
    a_user = make_user(db_session, company_id=COMPANY_A, role=UserRole.MANAGER)
    if owner == COMPANY_B:
        code = make_location(db_session, company_id=COMPANY_B, code="INVH-CC-FOREIGN").code
    else:
        code = "INVH-CC-NO-SUCH-LOC"

    resp = client.post(
        "/api/v1/inventory/cycle-counts",
        headers=headers_for(a_user),
        json={"location_code": code, "scheduled_date": date.today().isoformat()},
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
    assert resp.json()["detail"] == "Location not found"

    db_session.expire_all()
    assert db_session.query(CycleCount).count() == 0, "a refused create must not leave a half-scoped count"


def test_create_cycle_count_writes_a_create_audit_row(client: TestClient, db_session: Session):
    """The step that DEFINES a count's scope must be on the hash chain.

    ``create_cycle_count`` took no ``AuditService`` at all, so "who scoped this count,
    and which stock rows did it enroll" was unanswerable -- even though those are
    exactly the rows ``complete`` later adjusts. Both ``cycle_counts`` and
    ``cycle_count_items`` are TenantMixin tables and this endpoint is their only
    writer.
    """
    user = make_user(db_session, company_id=COMPANY_A, role=UserRole.MANAGER)
    a_ids, _b_ids = _make_two_company_stock(db_session, warehouse="INVH-WH3", location_code="INVH-CC3")

    resp = client.post(
        "/api/v1/inventory/cycle-counts",
        headers=headers_for(user),
        json={"warehouse": "INVH-WH3", "scheduled_date": date.today().isoformat(), "notes": "quarterly"},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()

    creates = _audit_rows(db_session, resource_type="cycle_count", action="CREATE")
    assert len(creates) == 1, "the enrollment must be audited"
    row = creates[0]
    assert row.company_id == COMPANY_A
    assert row.user_id == user.id
    assert row.resource_id == body["id"]
    assert row.resource_identifier == body["count_number"]
    # The declared scope and the enrolled row count are both recoverable from the row.
    assert row.extra_data["warehouse"] == "INVH-WH3"
    assert row.extra_data["location_code"] is None
    assert row.extra_data["part_id"] is None
    assert row.extra_data["total_items"] == len(a_ids)
    assert row.new_values["count_number"] == body["count_number"]

    _assert_hash_chain_intact(db_session)


def test_create_cycle_count_audit_records_the_location_scope(client: TestClient, db_session: Session):
    """A location-scoped count records the code it was scoped by."""
    user = make_user(db_session, company_id=COMPANY_A, role=UserRole.SUPERVISOR)
    a_ids, _b_ids = _make_two_company_stock(db_session, warehouse="INVH-WH4", location_code="INVH-CC4")

    resp = client.post(
        "/api/v1/inventory/cycle-counts",
        headers=headers_for(user),
        json={"location_code": "INVH-CC4", "scheduled_date": date.today().isoformat()},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    row = _audit_rows(db_session, resource_type="cycle_count", action="CREATE")[0]
    assert row.extra_data["location_code"] == "INVH-CC4"
    assert row.extra_data["total_items"] == len(a_ids)


# ---------------------------------------------------------------------------
# POST /inventory/cycle-counts/{id}/items/{item_id}/count -- tenant isolation
# ---------------------------------------------------------------------------


def _seed_cycle_count(
    db: Session,
    *,
    company_id: int,
    inventory_items: list,
    status_: CycleCountStatus = CycleCountStatus.IN_PROGRESS,
) -> CycleCount:
    """A cycle count plus one CycleCountItem per supplied inventory item."""
    n = _next()
    count = CycleCount(
        count_number=f"CC-INVH-{n:05d}",
        warehouse="MAIN",
        scheduled_date=date.today(),
        status=status_,
        total_items=len(inventory_items),
        company_id=company_id,
    )
    db.add(count)
    db.flush()
    for inv in inventory_items:
        db.add(
            CycleCountItem(
                cycle_count_id=count.id,
                inventory_item_id=inv.id,
                system_quantity=inv.quantity_on_hand,
                unit_cost=inv.unit_cost,
                company_id=company_id,
            )
        )
    db.commit()
    db.refresh(count)
    return count


def test_record_count_cross_tenant_is_404_and_no_mutation(client: TestClient, db_session: Session):
    """A company-A user cannot record a count against company B's count item.

    This endpoint previously took **no company dependency at all** -- the lookup
    was ``id == item_id AND cycle_count_id == count_id``, both guessable
    integers -- so any authenticated user could write a counted quantity (and
    therefore, via ``complete``, an inventory adjustment) into another tenant.
    """
    a_user = make_user(db_session, company_id=COMPANY_A)
    b_part = make_part(db_session, company_id=COMPANY_B)
    b_loc = make_location(db_session, company_id=COMPANY_B)
    b_item = make_inventory_item(db_session, company_id=COMPANY_B, part=b_part, location=b_loc.code, qty=20.0)
    b_count = _seed_cycle_count(db_session, company_id=COMPANY_B, inventory_items=[b_item])
    b_count_item = db_session.query(CycleCountItem).filter(CycleCountItem.cycle_count_id == b_count.id).one()

    resp = client.post(
        f"/api/v1/inventory/cycle-counts/{b_count.id}/items/{b_count_item.id}/count",
        headers=headers_for(a_user),
        json={"counted_quantity": 999, "notes": "cross tenant"},
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
    assert resp.json()["detail"] == "Cycle count not found"

    item_after = _reload(db_session, CycleCountItem, b_count_item.id)
    assert item_after.is_counted is False, "company B's count item must not be written"
    assert item_after.counted_quantity is None
    assert item_after.variance is None
    assert item_after.counted_by is None
    count_after = _reload(db_session, CycleCount, b_count.id)
    assert count_after.items_counted == 0


def test_record_count_own_company_succeeds(client: TestClient, db_session: Session):
    """Positive control: the scoping change must not break the normal path."""
    a_user = make_user(db_session, company_id=COMPANY_A, role=UserRole.OPERATOR)
    a_part = make_part(db_session, company_id=COMPANY_A)
    a_loc = make_location(db_session, company_id=COMPANY_A)
    a_item = make_inventory_item(
        db_session, company_id=COMPANY_A, part=a_part, location=a_loc.code, qty=10.0, unit_cost=3.0
    )
    a_count = _seed_cycle_count(db_session, company_id=COMPANY_A, inventory_items=[a_item])
    a_count_item = db_session.query(CycleCountItem).filter(CycleCountItem.cycle_count_id == a_count.id).one()

    resp = client.post(
        f"/api/v1/inventory/cycle-counts/{a_count.id}/items/{a_count_item.id}/count",
        headers=headers_for(a_user),
        json={"counted_quantity": 7, "notes": "recount ok"},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert float(resp.json()["variance"]) == -3.0

    item_after = _reload(db_session, CycleCountItem, a_count_item.id)
    assert item_after.is_counted is True
    assert float(item_after.counted_quantity) == 7.0
    assert float(item_after.variance_value) == -9.0
    assert item_after.counted_by == a_user.id
    # NOTE: ``CycleCount.items_counted`` is NOT asserted here -- it is off by one.
    # See test_record_count_progress_counter_is_stale below.


@pytest.mark.xfail(
    strict=True,
    reason=(
        "PRE-EXISTING DEFECT (not introduced by the tenant-scoping fix, and deliberately "
        "not fixed here): record_count recomputes CycleCount.items_counted with a "
        "db.query(...).count() while `item.is_counted = True` is still pending in the "
        "session. Both app.db.database.SessionLocal and the test sessionmaker are "
        "autoflush=False, so the just-counted row is invisible to that COUNT and the "
        "progress counter lags by exactly one -- it can never reach total_items. The "
        "same query shape exists on main; only the company_id predicate was added."
    ),
)
def test_record_count_progress_counter_is_stale(client: TestClient, db_session: Session):
    """Counting N items should leave ``items_counted == N``; it lands on N-1."""
    a_user = make_user(db_session, company_id=COMPANY_A, role=UserRole.OPERATOR)
    a_part = make_part(db_session, company_id=COMPANY_A)
    a_loc = make_location(db_session, company_id=COMPANY_A)
    items = [
        make_inventory_item(db_session, company_id=COMPANY_A, part=a_part, location=a_loc.code, qty=qty)
        for qty in (10.0, 20.0)
    ]
    a_count = _seed_cycle_count(db_session, company_id=COMPANY_A, inventory_items=items)
    count_items = db_session.query(CycleCountItem).filter(CycleCountItem.cycle_count_id == a_count.id).all()

    for ci in count_items:
        resp = client.post(
            f"/api/v1/inventory/cycle-counts/{a_count.id}/items/{ci.id}/count",
            headers=headers_for(a_user),
            json={"counted_quantity": 1},
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text

    # Both rows really are counted...
    assert db_session.query(CycleCountItem).filter(CycleCountItem.is_counted == True).count() == 2
    # ...but the denormalized progress counter says otherwise.
    count_after = _reload(db_session, CycleCount, a_count.id)
    assert count_after.items_counted == 2


@pytest.mark.parametrize(
    "count_status", [CycleCountStatus.SCHEDULED, CycleCountStatus.COMPLETED, CycleCountStatus.CANCELLED]
)
def test_record_count_requires_an_in_progress_parent(client: TestClient, db_session: Session, count_status):
    """A counted quantity is the quality record the variance adjustment derives from.

    Once the parent count is closed that record is evidence and must not be
    overwritten; before it is opened there is nothing to count against. Only an
    IN_PROGRESS parent accepts writes -- 409 otherwise.
    """
    a_user = make_user(db_session, company_id=COMPANY_A, role=UserRole.OPERATOR)
    a_part = make_part(db_session, company_id=COMPANY_A)
    a_loc = make_location(db_session, company_id=COMPANY_A)
    a_item = make_inventory_item(db_session, company_id=COMPANY_A, part=a_part, location=a_loc.code, qty=10.0)
    a_count = _seed_cycle_count(db_session, company_id=COMPANY_A, inventory_items=[a_item], status_=count_status)
    a_count_item = db_session.query(CycleCountItem).filter(CycleCountItem.cycle_count_id == a_count.id).one()

    resp = client.post(
        f"/api/v1/inventory/cycle-counts/{a_count.id}/items/{a_count_item.id}/count",
        headers=headers_for(a_user),
        json={"counted_quantity": 999},
    )
    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
    assert _status_label(count_status) in resp.json()["detail"]

    item_after = _reload(db_session, CycleCountItem, a_count_item.id)
    assert item_after.is_counted is False
    assert item_after.counted_quantity is None


def _counted_count(db: Session, *, role: UserRole = UserRole.OPERATOR, qty: float = 10.0, unit_cost: float = 3.0):
    """An IN_PROGRESS count over one stock row, plus a user who may count into it."""
    user = make_user(db, company_id=COMPANY_A, role=role)
    part = make_part(db, company_id=COMPANY_A)
    loc = make_location(db, company_id=COMPANY_A)
    item = make_inventory_item(db, company_id=COMPANY_A, part=part, location=loc.code, qty=qty, unit_cost=unit_cost)
    count = _seed_cycle_count(db, company_id=COMPANY_A, inventory_items=[item])
    count_item = db.query(CycleCountItem).filter(CycleCountItem.cycle_count_id == count.id).one()
    return user, count, count_item


def test_record_count_writes_an_update_audit_row(client: TestClient, db_session: Session):
    """``record_count`` took no ``AuditService``, so the counted quantity -- the
    quality record the ledger-posting adjustment is derived from -- was written with
    nothing on the hash chain.

    Audited as an UPDATE on every write (the ``cycle_count_item`` row already exists;
    enrollment created it), so the first count carries null old values.
    """
    user, count, count_item = _counted_count(db_session)

    resp = client.post(
        f"/api/v1/inventory/cycle-counts/{count.id}/items/{count_item.id}/count",
        headers=headers_for(user),
        json={"counted_quantity": 7, "notes": "floor count"},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    rows = _audit_rows(db_session, resource_type="cycle_count_item", action="UPDATE")
    assert len(rows) == 1
    row = rows[0]
    assert row.company_id == COMPANY_A
    assert row.user_id == user.id
    assert row.resource_id == count_item.id
    assert row.resource_identifier == f"{count.count_number} item {count_item.id}"
    changes = row.extra_data["changes"]
    assert changes["counted_quantity"] == {"old": None, "new": 7}
    assert changes["is_counted"] == {"old": False, "new": True}
    assert changes["counted_by"] == {"old": None, "new": user.id}
    assert row.extra_data["cycle_count_id"] == count.id

    _assert_hash_chain_intact(db_session)


def test_re_recording_a_count_audits_the_overwritten_value(client: TestClient, db_session: Session):
    """A re-POST while the count is IN_PROGRESS silently overwrites
    ``counted_quantity`` / ``variance`` / ``counted_by``.

    That is the destruction of an evidence value whose only other record is the row
    being overwritten, so the audit row has to carry what it replaced.
    """
    first, count, count_item = _counted_count(db_session, unit_cost=3.0)
    second = make_user(db_session, company_id=COMPANY_A, role=UserRole.SUPERVISOR)
    url = f"/api/v1/inventory/cycle-counts/{count.id}/items/{count_item.id}/count"

    assert client.post(url, headers=headers_for(first), json={"counted_quantity": 7}).status_code == 200
    assert client.post(url, headers=headers_for(second), json={"counted_quantity": 4}).status_code == 200

    rows = _audit_rows(db_session, resource_type="cycle_count_item", action="UPDATE")
    assert len(rows) == 2, "both the original count and the overwrite are on the chain"
    overwrite = rows[-1]
    assert overwrite.user_id == second.id
    changes = overwrite.extra_data["changes"]
    assert changes["counted_quantity"] == {"old": 7, "new": 4}
    assert changes["variance"] == {"old": -3, "new": -6}
    assert changes["counted_by"] == {"old": first.id, "new": second.id}
    assert "Re-counted" in overwrite.description

    # The live row holds only the latest value -- which is exactly why the audit row
    # above is the sole surviving record of the 7.
    item_after = _reload(db_session, CycleCountItem, count_item.id)
    assert float(item_after.counted_quantity) == 4.0
    assert item_after.counted_by == second.id

    _assert_hash_chain_intact(db_session)


# ---------------------------------------------------------------------------
# POST /inventory/cycle-counts/{id}/start + .../count -- RBAC (VIEWER excluded)
# ---------------------------------------------------------------------------
#
# Both verbs were bare-``get_current_user``. VIEWER is read-only by definition
# (``frontend/src/utils/permissions.ts`` grants it ``inventory:view`` and nothing
# else), yet it could write the counted quantities a manager's ledger-posting
# adjustment is derived from. The gate is by EXCLUSION so the whole shop-floor path
# is preserved and only the read-only role loses write access.

COUNT_WRITE_ROLES = [
    UserRole.ADMIN,
    UserRole.MANAGER,
    UserRole.SUPERVISOR,
    UserRole.OPERATOR,
    UserRole.QUALITY,
    UserRole.SHIPPING,
]


@pytest.mark.parametrize("role", COUNT_WRITE_ROLES)
def test_start_allowed_for_every_working_role(client: TestClient, db_session: Session, role: UserRole):
    user = make_user(db_session, company_id=COMPANY_A, role=role)
    part = make_part(db_session, company_id=COMPANY_A)
    loc = make_location(db_session, company_id=COMPANY_A)
    item = make_inventory_item(db_session, company_id=COMPANY_A, part=part, location=loc.code, qty=10.0)
    count = _seed_cycle_count(
        db_session, company_id=COMPANY_A, inventory_items=[item], status_=CycleCountStatus.SCHEDULED
    )

    assert _start(client, user, count).status_code == status.HTTP_200_OK
    assert _reload(db_session, CycleCount, count.id).status == CycleCountStatus.IN_PROGRESS


def test_start_forbidden_for_viewer(client: TestClient, db_session: Session):
    """VIEWER is read-only; opening a count is a write to the count's lifecycle."""
    user = make_user(db_session, company_id=COMPANY_A, role=UserRole.VIEWER)
    part = make_part(db_session, company_id=COMPANY_A)
    loc = make_location(db_session, company_id=COMPANY_A)
    item = make_inventory_item(db_session, company_id=COMPANY_A, part=part, location=loc.code, qty=10.0)
    count = _seed_cycle_count(
        db_session, company_id=COMPANY_A, inventory_items=[item], status_=CycleCountStatus.SCHEDULED
    )

    resp = _start(client, user, count)
    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text
    assert resp.json()["detail"] == "Insufficient permissions"

    after = _reload(db_session, CycleCount, count.id)
    assert after.status == CycleCountStatus.SCHEDULED, "a refused start must not open the count"
    assert after.started_at is None
    assert after.assigned_to is None
    assert _audit_rows(db_session, resource_type="cycle_count") == []


@pytest.mark.parametrize("role", COUNT_WRITE_ROLES)
def test_record_count_allowed_for_every_working_role(client: TestClient, db_session: Session, role: UserRole):
    user, count, count_item = _counted_count(db_session, role=role)

    resp = client.post(
        f"/api/v1/inventory/cycle-counts/{count.id}/items/{count_item.id}/count",
        headers=headers_for(user),
        json={"counted_quantity": 7},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert _reload(db_session, CycleCountItem, count_item.id).is_counted is True


def test_record_count_forbidden_for_viewer(client: TestClient, db_session: Session):
    """The counted quantity is the quality record the variance adjustment derives
    from -- writing it is not a read, so the read-only role is refused."""
    user, count, count_item = _counted_count(db_session, role=UserRole.VIEWER)

    resp = client.post(
        f"/api/v1/inventory/cycle-counts/{count.id}/items/{count_item.id}/count",
        headers=headers_for(user),
        json={"counted_quantity": 999},
    )
    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text
    assert resp.json()["detail"] == "Insufficient permissions"

    item_after = _reload(db_session, CycleCountItem, count_item.id)
    assert item_after.is_counted is False
    assert item_after.counted_quantity is None
    assert item_after.counted_by is None
    assert _reload(db_session, CycleCount, count.id).items_counted == 0
    assert _audit_rows(db_session, resource_type="cycle_count_item") == []


# ---------------------------------------------------------------------------
# POST /inventory/cycle-counts/{id}/start -- terminal guard, audit
# ---------------------------------------------------------------------------


def _start(client: TestClient, user: User, count: CycleCount):
    return client.post(f"/api/v1/inventory/cycle-counts/{count.id}/start", headers=headers_for(user))


def test_operator_can_start_a_scheduled_count_and_record_into_it(client: TestClient, db_session: Session):
    """The shop-floor counting path, end to end, pinned as a capability.

    ``start`` and ``record_count`` are gated by EXCLUSION
    (``require_role(COUNT_WRITE_ROLES)``): every working role keeps them and only the
    read-only VIEWER is refused. Counting is an operator task by documented policy
    (``docs/RBAC_PERMISSIONS.md`` -> Inventory); the privileged steps are creating the
    count and completing it (posting the variance to the ledger). Because
    ``record_count`` 409s unless the parent is IN_PROGRESS, narrowing ``start`` to the
    stock-mutator set would leave an operator unable to work a SCHEDULED count at
    all -- so this test exists to fail loudly if a future pass adds that gate.
    """
    operator = make_user(db_session, company_id=COMPANY_A, role=UserRole.OPERATOR)
    part = make_part(db_session, company_id=COMPANY_A)
    loc = make_location(db_session, company_id=COMPANY_A)
    item = make_inventory_item(db_session, company_id=COMPANY_A, part=part, location=loc.code, qty=10.0, unit_cost=3.0)
    count = _seed_cycle_count(
        db_session, company_id=COMPANY_A, inventory_items=[item], status_=CycleCountStatus.SCHEDULED
    )
    count_item = db_session.query(CycleCountItem).filter(CycleCountItem.cycle_count_id == count.id).one()

    # 1. The operator opens the scheduled count.
    resp = _start(client, operator, count)
    assert resp.status_code == status.HTTP_200_OK, resp.text

    after = _reload(db_session, CycleCount, count.id)
    assert after.status == CycleCountStatus.IN_PROGRESS
    assert after.started_at is not None
    assert after.assigned_to == operator.id

    # The transition is audited regardless of who made it.
    logs = _audit_rows(db_session, resource_type="cycle_count", action="STATUS_CHANGE")
    assert len(logs) == 1
    assert logs[0].new_values == {"status": "in_progress"}
    _assert_hash_chain_intact(db_session)

    # 2. ...and counts into it, with no further gate in the way.
    resp = client.post(
        f"/api/v1/inventory/cycle-counts/{count.id}/items/{count_item.id}/count",
        headers=headers_for(operator),
        json={"counted_quantity": 7, "notes": "floor count"},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert float(resp.json()["variance"]) == -3.0

    item_after = _reload(db_session, CycleCountItem, count_item.id)
    assert item_after.is_counted is True
    assert float(item_after.counted_quantity) == 7.0
    assert item_after.counted_by == operator.id


def test_start_cycle_count_writes_status_change_audit(client: TestClient, db_session: Session):
    user = make_user(db_session, company_id=COMPANY_A, role=UserRole.SUPERVISOR)
    part = make_part(db_session, company_id=COMPANY_A)
    loc = make_location(db_session, company_id=COMPANY_A)
    item = make_inventory_item(db_session, company_id=COMPANY_A, part=part, location=loc.code, qty=10.0)
    count = _seed_cycle_count(
        db_session, company_id=COMPANY_A, inventory_items=[item], status_=CycleCountStatus.SCHEDULED
    )

    assert _start(client, user, count).status_code == status.HTTP_200_OK

    after = _reload(db_session, CycleCount, count.id)
    assert after.status == CycleCountStatus.IN_PROGRESS
    assert after.started_at is not None
    assert after.assigned_to == user.id

    logs = _audit_rows(db_session, resource_type="cycle_count", action="STATUS_CHANGE")
    assert len(logs) == 1
    assert logs[0].company_id == COMPANY_A
    assert logs[0].resource_id == count.id
    assert logs[0].old_values == {"status": "scheduled"}
    assert logs[0].new_values == {"status": "in_progress"}
    _assert_hash_chain_intact(db_session)


@pytest.mark.parametrize("count_status", [CycleCountStatus.COMPLETED, CycleCountStatus.CANCELLED])
def test_start_cycle_count_on_terminal_count_is_409(client: TestClient, db_session: Session, count_status):
    """Re-opening a COMPLETED count would let a second ``complete`` double-post the
    same physical variance; a CANCELLED one was deliberately abandoned."""
    user = make_user(db_session, company_id=COMPANY_A, role=UserRole.MANAGER)
    part = make_part(db_session, company_id=COMPANY_A)
    loc = make_location(db_session, company_id=COMPANY_A)
    item = make_inventory_item(db_session, company_id=COMPANY_A, part=part, location=loc.code, qty=10.0)
    count = _seed_cycle_count(db_session, company_id=COMPANY_A, inventory_items=[item], status_=count_status)

    resp = _start(client, user, count)
    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
    assert _status_label(count_status) in resp.json()["detail"]

    after = _reload(db_session, CycleCount, count.id)
    assert after.status == count_status
    assert after.started_at is None


def test_restarting_an_in_progress_count_preserves_started_at(client: TestClient, db_session: Session):
    """``started_at`` is the traceability record of when counting began; a
    reassignment must not overwrite it (and is audited as an UPDATE, not a
    fabricated SCHEDULED -> IN_PROGRESS transition)."""
    first = make_user(db_session, company_id=COMPANY_A, role=UserRole.SUPERVISOR)
    second = make_user(db_session, company_id=COMPANY_A, role=UserRole.MANAGER)
    part = make_part(db_session, company_id=COMPANY_A)
    loc = make_location(db_session, company_id=COMPANY_A)
    item = make_inventory_item(db_session, company_id=COMPANY_A, part=part, location=loc.code, qty=10.0)
    count = _seed_cycle_count(
        db_session, company_id=COMPANY_A, inventory_items=[item], status_=CycleCountStatus.SCHEDULED
    )

    assert _start(client, first, count).status_code == status.HTTP_200_OK
    original_started_at = _reload(db_session, CycleCount, count.id).started_at

    assert _start(client, second, count).status_code == status.HTTP_200_OK
    after = _reload(db_session, CycleCount, count.id)
    assert after.started_at == original_started_at
    assert after.assigned_to == second.id

    assert len(_audit_rows(db_session, resource_type="cycle_count", action="STATUS_CHANGE")) == 1
    updates = _audit_rows(db_session, resource_type="cycle_count", action="UPDATE")
    assert len(updates) == 1
    assert updates[0].extra_data["changes"]["assigned_to"] == {"old": first.id, "new": second.id}
    _assert_hash_chain_intact(db_session)


# ---------------------------------------------------------------------------
# POST /inventory/cycle-counts/{id}/complete -- adjustments stay in-tenant
# ---------------------------------------------------------------------------


def test_complete_cycle_count_adjusts_only_tenant_rows_and_stamps_txn(client: TestClient, db_session: Session):
    """``complete`` must refuse to write through a count item that points at a
    foreign stock row, and the COUNT ledger row must carry ``company_id``.

    The count seeded here has the shape the pre-fix unscoped enrollment scan
    selected for: a company-A count covering one company-A stock row *and* one
    company-B stock row. Pre-fix, ``complete`` resolved the inventory row by
    primary key alone and overwrote company B's on-hand; and it built the
    ``InventoryTransaction`` with no ``company_id`` (a NOT NULL TenantMixin
    column), so the write also violated the constraint.

    ``create`` is fixed too (see the enrollment tests above), so this pairing is
    no longer reachable through the API -- but ``complete`` must refuse it on its
    own, both as defense in depth and because any count rows already persisted
    with that shape get replayed through exactly this code path.
    """
    a_user = make_user(db_session, company_id=COMPANY_A, role=UserRole.MANAGER)
    a_part = make_part(db_session, company_id=COMPANY_A)
    b_part = make_part(db_session, company_id=COMPANY_B)
    a_loc = make_location(db_session, company_id=COMPANY_A)
    b_loc = make_location(db_session, company_id=COMPANY_B)
    a_item = make_inventory_item(
        db_session, company_id=COMPANY_A, part=a_part, location=a_loc.code, qty=10.0, unit_cost=2.0
    )
    b_item = make_inventory_item(
        db_session, company_id=COMPANY_B, part=b_part, location=b_loc.code, qty=20.0, unit_cost=5.0
    )

    a_count = _seed_cycle_count(db_session, company_id=COMPANY_A, inventory_items=[a_item, b_item])
    by_inv = {
        ci.inventory_item_id: ci
        for ci in db_session.query(CycleCountItem).filter(CycleCountItem.cycle_count_id == a_count.id).all()
    }
    for inv_id, counted in ((a_item.id, 7.0), (b_item.id, 1.0)):
        ci = by_inv[inv_id]
        ci.counted_quantity = counted
        ci.variance = counted - ci.system_quantity
        ci.variance_value = ci.variance * ci.unit_cost
        ci.is_counted = True
    db_session.commit()

    resp = client.post(
        f"/api/v1/inventory/cycle-counts/{a_count.id}/complete?apply_adjustments=true",
        headers=headers_for(a_user),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["items_adjusted"] == 1, "only the in-tenant stock row may be adjusted"

    a_after = _reload(db_session, InventoryItem, a_item.id)
    assert float(a_after.quantity_on_hand) == 7.0
    assert float(a_after.quantity_available) == 7.0

    b_after = _reload(db_session, InventoryItem, b_item.id)
    assert float(b_after.quantity_on_hand) == 20.0, "company B's stock must survive company A's cycle count"
    assert float(b_after.quantity_available) == 20.0

    txns = db_session.query(InventoryTransaction).filter(InventoryTransaction.transaction_type == "COUNT").all()
    assert len(txns) == 1
    assert txns[0].company_id == COMPANY_A
    assert txns[0].inventory_item_id == a_item.id
    assert float(txns[0].quantity) == -3.0
    assert txns[0].reason_code == "cycle_count"

    count_after = _reload(db_session, CycleCount, a_count.id)
    assert count_after.status == CycleCountStatus.COMPLETED
    assert count_after.items_adjusted == 1


def _mark_counted(db: Session, count: CycleCount, counted_by_inventory_id: dict) -> None:
    """Fill in counted quantities directly, the way ``record_count`` would."""
    for ci in db.query(CycleCountItem).filter(CycleCountItem.cycle_count_id == count.id).all():
        if ci.inventory_item_id not in counted_by_inventory_id:
            continue
        counted = counted_by_inventory_id[ci.inventory_item_id]
        ci.counted_quantity = counted
        ci.variance = counted - ci.system_quantity
        ci.variance_value = ci.variance * ci.unit_cost
        ci.is_counted = True
    db.commit()


def _one_item_count(db: Session, *, qty: float = 10.0, unit_cost: float = 2.0, counted: float = 7.0):
    """A MANAGER, one in-tenant stock row, and an IN_PROGRESS count already counted."""
    user = make_user(db, company_id=COMPANY_A, role=UserRole.MANAGER)
    part = make_part(db, company_id=COMPANY_A)
    loc = make_location(db, company_id=COMPANY_A)
    item = make_inventory_item(db, company_id=COMPANY_A, part=part, location=loc.code, qty=qty, unit_cost=unit_cost)
    count = _seed_cycle_count(db, company_id=COMPANY_A, inventory_items=[item])
    _mark_counted(db, count, {item.id: counted})
    return user, item, count


def test_complete_cycle_count_writes_audit_rows_for_every_adjustment(client: TestClient, db_session: Session):
    """BLOCKER: ``complete`` mutated ``quantity_on_hand`` and appended a COUNT ledger
    row with **no audit calls at all** -- the endpoint took no ``AuditService``
    dependency. It now writes the same dual-row convention as ``/inventory/adjust``
    (an ``inventory`` CREATE for the movement + an ``inventory`` UPDATE for the stock
    level) plus a ``cycle_count`` STATUS_CHANGE for the completion itself.
    """
    user, item, count = _one_item_count(db_session, qty=10.0, unit_cost=2.0, counted=7.0)

    resp = client.post(
        f"/api/v1/inventory/cycle-counts/{count.id}/complete?apply_adjustments=true",
        headers=headers_for(user),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    creates = _audit_rows(db_session, resource_type="inventory", action="CREATE")
    updates = _audit_rows(db_session, resource_type="inventory", action="UPDATE")
    assert len(creates) == 1, "the COUNT movement must be audited"
    assert len(updates) == 1, "the stock-level change must be audited"
    assert creates[0].company_id == COMPANY_A
    txn = db_session.query(InventoryTransaction).filter(InventoryTransaction.transaction_type == "COUNT").one()
    assert creates[0].resource_id == txn.id
    assert updates[0].resource_id == item.id
    assert updates[0].extra_data["changes"]["quantity_on_hand"] == {"old": 10, "new": 7}

    status_changes = _audit_rows(db_session, resource_type="cycle_count", action="STATUS_CHANGE")
    assert len(status_changes) == 1
    assert status_changes[0].resource_identifier == count.count_number
    assert status_changes[0].new_values == {"status": "completed"}
    assert status_changes[0].extra_data["items_adjusted"] == 1

    _assert_hash_chain_intact(db_session)


def test_complete_cycle_count_twice_is_409_and_does_not_double_post(client: TestClient, db_session: Session):
    """BLOCKER: no terminal-state guard meant a double-click appended a SECOND COUNT
    transaction for the same physical variance.

    On-hand is idempotent (it is *set* to the counted quantity), so the divergence is
    silent: stock reads 7 while the ledger claims two -3 movements.
    """
    user, item, count = _one_item_count(db_session, qty=10.0, unit_cost=2.0, counted=7.0)
    url = f"/api/v1/inventory/cycle-counts/{count.id}/complete?apply_adjustments=true"

    assert client.post(url, headers=headers_for(user)).status_code == status.HTTP_200_OK

    second = client.post(url, headers=headers_for(user))
    assert second.status_code == status.HTTP_409_CONFLICT, second.text
    assert "completed" in second.json()["detail"]

    txns = db_session.query(InventoryTransaction).filter(InventoryTransaction.transaction_type == "COUNT").all()
    assert len(txns) == 1, "the ledger must not gain a phantom second COUNT row"
    assert float(_reload(db_session, InventoryItem, item.id).quantity_on_hand) == 7.0
    # ...and no second audit trail either.
    assert len(_audit_rows(db_session, resource_type="inventory", action="CREATE")) == 1
    assert len(_audit_rows(db_session, resource_type="cycle_count", action="STATUS_CHANGE")) == 1


def test_complete_stores_posted_variance_and_returns_measured(client: TestClient, db_session: Session):
    """``total_variance_value`` must reconcile with the COUNT rows this completion wrote.

    The count below carries one in-tenant row (variance_value -6, posts) and one
    foreign row (variance_value -95, refused by the tenant guard). The stored figure
    used to include BOTH, so the header claimed a variance the ledger never carried.
    """
    a_user = make_user(db_session, company_id=COMPANY_A, role=UserRole.MANAGER)
    a_part = make_part(db_session, company_id=COMPANY_A)
    b_part = make_part(db_session, company_id=COMPANY_B)
    a_loc = make_location(db_session, company_id=COMPANY_A)
    b_loc = make_location(db_session, company_id=COMPANY_B)
    a_item = make_inventory_item(
        db_session, company_id=COMPANY_A, part=a_part, location=a_loc.code, qty=10.0, unit_cost=2.0
    )
    b_item = make_inventory_item(
        db_session, company_id=COMPANY_B, part=b_part, location=b_loc.code, qty=20.0, unit_cost=5.0
    )
    count = _seed_cycle_count(db_session, company_id=COMPANY_A, inventory_items=[a_item, b_item])
    _mark_counted(db_session, count, {a_item.id: 7.0, b_item.id: 1.0})

    resp = client.post(
        f"/api/v1/inventory/cycle-counts/{count.id}/complete?apply_adjustments=true",
        headers=headers_for(a_user),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["items_adjusted"] == 1
    assert float(body["total_variance_value"]) == -6.0, "only the variance that posted"
    assert float(body["measured_variance_value"]) == -101.0, "what the counters found, unfiltered"

    after = _reload(db_session, CycleCount, count.id)
    assert float(after.total_variance_value) == -6.0
    posted = db_session.query(InventoryTransaction).filter(InventoryTransaction.transaction_type == "COUNT").all()
    assert sum(float(t.quantity) * float(t.unit_cost) for t in posted) == float(after.total_variance_value)


def test_complete_another_companys_count_is_404_and_posts_nothing(client: TestClient, db_session: Session):
    """The row pre-lock that serializes the terminal-state guard is itself
    tenant-scoped, so a foreign count id is "not found" -- it must not lock, read or
    complete another tenant's count.

    (The ``FOR UPDATE`` clause itself is a no-op under the SQLite test backend, as at
    every other ``with_for_update`` site in this codebase; what is asserted here is
    the scoping and the 404 the pre-lock produces.)
    """
    a_user = make_user(db_session, company_id=COMPANY_A, role=UserRole.MANAGER)
    b_part = make_part(db_session, company_id=COMPANY_B)
    b_loc = make_location(db_session, company_id=COMPANY_B)
    b_item = make_inventory_item(db_session, company_id=COMPANY_B, part=b_part, location=b_loc.code, qty=20.0)
    b_count = _seed_cycle_count(db_session, company_id=COMPANY_B, inventory_items=[b_item])
    _mark_counted(db_session, b_count, {b_item.id: 5.0})

    resp = client.post(
        f"/api/v1/inventory/cycle-counts/{b_count.id}/complete?apply_adjustments=true",
        headers=headers_for(a_user),
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
    assert resp.json()["detail"] == "Cycle count not found"

    after = _reload(db_session, CycleCount, b_count.id)
    assert after.status == CycleCountStatus.IN_PROGRESS, "company B's count must not be completed by company A"
    assert after.completed_at is None
    assert float(_reload(db_session, InventoryItem, b_item.id).quantity_on_hand) == 20.0
    assert db_session.query(InventoryTransaction).count() == 0


def test_complete_unknown_count_is_404(client: TestClient, db_session: Session):
    user = make_user(db_session, company_id=COMPANY_A, role=UserRole.MANAGER)

    resp = client.post("/api/v1/inventory/cycle-counts/999999/complete", headers=headers_for(user))
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
    assert resp.json()["detail"] == "Cycle count not found"


def test_complete_prices_posted_variance_on_the_ledger_rows_own_cost_basis(client: TestClient, db_session: Session):
    """``total_variance_value`` must reconcile with the rows the completion WROTE.

    ``posted_variance`` accumulated ``item.variance_value`` -- the ENROLLMENT-time
    ``unit_cost`` snapshotted onto ``CycleCountItem`` -- while the COUNT ledger row it
    was supposed to reconcile with is priced at the CURRENT ``InventoryItem.unit_cost``.
    When the unit cost moves between enrollment and completion (a re-cost, a new
    receipt at a different price) the header claimed a variance the ledger never
    carried. The existing coverage only passed because its fixtures hold cost constant.

    Enrolled at 2.00/ea, completed at 5.00/ea, variance -3 ea:
      measured (enrollment basis) = -3 * 2.00 = -6.00
      posted   (ledger basis)     = -3 * 5.00 = -15.00
    """
    user, item, count = _one_item_count(db_session, qty=10.0, unit_cost=2.0, counted=7.0)
    count_item = db_session.query(CycleCountItem).filter(CycleCountItem.cycle_count_id == count.id).one()
    assert float(count_item.unit_cost) == 2.0, "the count item snapshots the enrollment-time cost"

    # The part is re-costed after enrollment but before the count is completed.
    item.unit_cost = 5.0
    db_session.commit()

    resp = client.post(
        f"/api/v1/inventory/cycle-counts/{count.id}/complete?apply_adjustments=true",
        headers=headers_for(user),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["items_adjusted"] == 1
    assert float(body["total_variance_value"]) == -15.0, "posted variance is priced like the ledger row"
    assert float(body["measured_variance_value"]) == -6.0, "measured variance keeps the enrollment snapshot"

    after = _reload(db_session, CycleCount, count.id)
    assert float(after.total_variance_value) == -15.0

    # The header reconciles exactly against the COUNT rows this completion wrote.
    posted = db_session.query(InventoryTransaction).filter(InventoryTransaction.transaction_type == "COUNT").all()
    assert len(posted) == 1
    assert float(posted[0].unit_cost) == 5.0
    assert sum(float(t.quantity) * float(t.unit_cost) for t in posted) == float(after.total_variance_value)
    # ``total_cost`` on the ledger row is the unsigned magnitude of the same figure.
    assert sum(float(t.total_cost) for t in posted) == abs(float(after.total_variance_value))

    # The per-item measured figure is untouched -- no information was lost.
    assert float(_reload(db_session, CycleCountItem, count_item.id).variance_value) == -6.0


def test_complete_reconciles_across_a_mix_of_shifted_and_unchanged_costs(client: TestClient, db_session: Session):
    """Multi-row control: the stored total equals the summed ledger rows even when
    only some of the enrolled parts were re-costed."""
    user = make_user(db_session, company_id=COMPANY_A, role=UserRole.MANAGER)
    part = make_part(db_session, company_id=COMPANY_A)
    loc = make_location(db_session, company_id=COMPANY_A)
    steady = make_inventory_item(
        db_session, company_id=COMPANY_A, part=part, location=loc.code, qty=10.0, unit_cost=2.0
    )
    shifted = make_inventory_item(
        db_session, company_id=COMPANY_A, part=part, location=loc.code, qty=20.0, unit_cost=4.0
    )
    count = _seed_cycle_count(db_session, company_id=COMPANY_A, inventory_items=[steady, shifted])
    _mark_counted(db_session, count, {steady.id: 12.0, shifted.id: 15.0})

    shifted.unit_cost = 10.0  # re-costed after enrollment
    db_session.commit()

    resp = client.post(
        f"/api/v1/inventory/cycle-counts/{count.id}/complete?apply_adjustments=true",
        headers=headers_for(user),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["items_adjusted"] == 2
    # steady: +2 @ 2.00 = +4 ; shifted: -5 @ 10.00 = -50
    assert float(body["total_variance_value"]) == -46.0
    # measured keeps the enrollment basis: +2 @ 2.00 = +4 ; -5 @ 4.00 = -20
    assert float(body["measured_variance_value"]) == -16.0

    after = _reload(db_session, CycleCount, count.id)
    posted = db_session.query(InventoryTransaction).filter(InventoryTransaction.transaction_type == "COUNT").all()
    assert len(posted) == 2
    assert sum(float(t.quantity) * float(t.unit_cost) for t in posted) == float(after.total_variance_value)


def test_complete_without_adjustments_posts_nothing(client: TestClient, db_session: Session):
    """``apply_adjustments=false`` closes the count without touching stock: nothing
    posted, so the stored variance is 0 and the measured figure is returned instead.
    The per-item ``variance_value`` rows survive, so no information is lost."""
    user, item, count = _one_item_count(db_session, qty=10.0, unit_cost=2.0, counted=7.0)

    resp = client.post(
        f"/api/v1/inventory/cycle-counts/{count.id}/complete?apply_adjustments=false",
        headers=headers_for(user),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["items_adjusted"] == 0
    assert float(body["total_variance_value"]) == 0.0
    assert float(body["measured_variance_value"]) == -6.0

    assert float(_reload(db_session, InventoryItem, item.id).quantity_on_hand) == 10.0
    assert db_session.query(InventoryTransaction).count() == 0
    assert _audit_rows(db_session, resource_type="inventory") == []

    after = _reload(db_session, CycleCount, count.id)
    assert after.status == CycleCountStatus.COMPLETED
    assert float(after.total_variance_value) == 0.0
    count_item = db_session.query(CycleCountItem).filter(CycleCountItem.cycle_count_id == count.id).one()
    assert float(count_item.variance_value) == -6.0, "the measured variance stays on the item row"
    # The completion itself is still audited even though nothing posted.
    assert len(_audit_rows(db_session, resource_type="cycle_count", action="STATUS_CHANGE")) == 1


# ---------------------------------------------------------------------------
# POST /inventory/issue -- RBAC
# ---------------------------------------------------------------------------


def _issue_fixture(db: Session, role: UserRole):
    user = make_user(db, company_id=COMPANY_A, role=role)
    part = make_part(db, company_id=COMPANY_A)
    loc = make_location(db, company_id=COMPANY_A)
    item = make_inventory_item(db, company_id=COMPANY_A, part=part, location=loc.code, qty=10.0)
    return user, item


@pytest.mark.parametrize("role", [UserRole.OPERATOR, UserRole.VIEWER, UserRole.QUALITY])
def test_issue_forbidden_for_unauthorized_roles(client: TestClient, db_session: Session, role: UserRole):
    """``/inventory/issue`` was ungated; it now matches the sibling
    ``/inventory/adjust`` stock mutator (ADMIN/MANAGER/SUPERVISOR)."""
    user, item = _issue_fixture(db_session, role)

    resp = client.post(
        "/api/v1/inventory/issue",
        headers=headers_for(user),
        json={"inventory_item_id": item.id, "quantity": 3, "work_order_number": "WO-RBAC"},
    )
    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text
    assert resp.json()["detail"] == "Insufficient permissions"

    after = _reload(db_session, InventoryItem, item.id)
    assert float(after.quantity_on_hand) == 10.0, "a refused issue must not move stock"
    assert db_session.query(InventoryTransaction).count() == 0


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])
def test_issue_allowed_for_authorized_roles(client: TestClient, db_session: Session, role: UserRole):
    user, item = _issue_fixture(db_session, role)

    resp = client.post(
        "/api/v1/inventory/issue",
        headers=headers_for(user),
        json={"inventory_item_id": item.id, "quantity": 3},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    after = _reload(db_session, InventoryItem, item.id)
    assert float(after.quantity_on_hand) == 7.0
    assert float(after.quantity_available) == 7.0


def test_issue_refuses_work_order_attribution_with_400(client: TestClient, db_session: Session):
    """A ``work_order_number`` on the deprecated manual issue writes a
    reference_number-only row that ``work_order_ledger_filter`` (job costing, lot
    genealogy, the backflush nets) can never see — so it is refused outright, with no
    stock movement and no ledger row."""
    user, item = _issue_fixture(db_session, UserRole.MANAGER)

    resp = client.post(
        "/api/v1/inventory/issue",
        headers=headers_for(user),
        json={"inventory_item_id": item.id, "quantity": 3, "work_order_number": "WO-0001"},
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
    assert "material allocations" in resp.json()["detail"]

    after = _reload(db_session, InventoryItem, item.id)
    assert float(after.quantity_on_hand) == 10.0, "a refused issue must not move stock"
    assert db_session.query(InventoryTransaction).count() == 0


def test_plain_issue_writes_no_work_order_reference(client: TestClient, db_session: Session):
    """The surviving manual issue is reference-less: nothing this endpoint writes may
    masquerade as work-order-attributed."""
    user, item = _issue_fixture(db_session, UserRole.MANAGER)

    resp = client.post(
        "/api/v1/inventory/issue",
        headers=headers_for(user),
        json={"inventory_item_id": item.id, "quantity": 2},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    txn = db_session.query(InventoryTransaction).one()
    assert txn.reference_type is None
    assert txn.reference_number is None
    assert txn.reference_id is None


# ---------------------------------------------------------------------------
# GET /inventory/transactions -- filters, paging, ordering, tenant scope
# ---------------------------------------------------------------------------


@pytest.fixture
def ledger(db_session: Session):
    """A small two-tenant ledger with every filterable shape represented."""
    a_user = make_user(db_session, company_id=COMPANY_A)
    b_user = make_user(db_session, company_id=COMPANY_B)
    a_part = make_part(db_session, company_id=COMPANY_A)
    a_part2 = make_part(db_session, company_id=COMPANY_A)
    b_part = make_part(db_session, company_id=COMPANY_B)
    wo = make_work_order(db_session, company_id=COMPANY_A, part=a_part)
    other_wo = make_work_order(db_session, company_id=COMPANY_A, part=a_part2)

    rows = {
        # reference_id shape (completion_inventory_service writes this)
        "wo_by_id": make_txn(
            db_session,
            company_id=COMPANY_A,
            part=a_part,
            user=a_user,
            transaction_type=TransactionType.RECEIVE,
            reference_type="work_order",
            reference_id=wo.id,
            lot_number="LOT-AAA",
            created_at=LEDGER_EPOCH,
        ),
        # reference_number shape (the legacy POST /inventory/issue path leaves
        # reference_id NULL)
        "wo_by_number": make_txn(
            db_session,
            company_id=COMPANY_A,
            part=a_part,
            user=a_user,
            transaction_type=TransactionType.ISSUE,
            quantity=-2.0,
            reference_type="work_order",
            reference_number=wo.work_order_number,
            lot_number="LOT-BBB",
            created_at=LEDGER_EPOCH + timedelta(hours=1),
        ),
        # a different work order -- must never match
        "other_wo": make_txn(
            db_session,
            company_id=COMPANY_A,
            part=a_part2,
            user=a_user,
            transaction_type=TransactionType.RECEIVE,
            reference_type="work_order",
            reference_id=other_wo.id,
            lot_number="LOT-AAA",
            created_at=LEDGER_EPOCH + timedelta(hours=2),
        ),
        # same reference_id, different reference_type -- must never match
        "po": make_txn(
            db_session,
            company_id=COMPANY_A,
            part=a_part,
            user=a_user,
            transaction_type=TransactionType.RECEIVE,
            reference_type="purchase_order",
            reference_id=wo.id,
            reference_number="PO-9",
            lot_number="LOT-CCC",
            created_at=LEDGER_EPOCH + timedelta(hours=3),
        ),
        # unreferenced adjustment
        "plain": make_txn(
            db_session,
            company_id=COMPANY_A,
            part=a_part,
            user=a_user,
            transaction_type=TransactionType.ADJUST,
            lot_number="LOT-CCC",
            created_at=LEDGER_EPOCH + timedelta(hours=4),
        ),
        # company B row carrying the SAME work-order id and number
        "foreign": make_txn(
            db_session,
            company_id=COMPANY_B,
            part=b_part,
            user=b_user,
            transaction_type=TransactionType.RECEIVE,
            reference_type="work_order",
            reference_id=wo.id,
            reference_number=wo.work_order_number,
            lot_number="LOT-AAA",
            created_at=LEDGER_EPOCH + timedelta(hours=5),
        ),
    }
    return {"a_user": a_user, "a_part": a_part, "a_part2": a_part2, "wo": wo, "other_wo": other_wo, "rows": rows}


def _get_txns(client: TestClient, ledger, **params):
    resp = client.get("/api/v1/inventory/transactions", headers=headers_for(ledger["a_user"]), params=params)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    return resp.json()


def test_transactions_are_tenant_scoped(client: TestClient, ledger):
    body = _get_txns(client, ledger)
    ids = {row["id"] for row in body}
    assert ledger["rows"]["foreign"].id not in ids, "company B's ledger rows must never surface"
    assert all(row["company_id"] == COMPANY_A for row in body)
    assert len(body) == 5


def test_transactions_filter_by_reference_type(client: TestClient, ledger):
    body = _get_txns(client, ledger, reference_type="purchase_order")
    assert [row["id"] for row in body] == [ledger["rows"]["po"].id]


def test_transactions_filter_by_reference_id(client: TestClient, ledger):
    """``reference_id`` alone is type-agnostic: both the work-order row and the
    purchase-order row carry this id."""
    body = _get_txns(client, ledger, reference_id=ledger["wo"].id)
    assert {row["id"] for row in body} == {ledger["rows"]["wo_by_id"].id, ledger["rows"]["po"].id}
    # Narrowed by type it resolves to exactly one row -- and never company B's.
    body = _get_txns(client, ledger, reference_id=ledger["wo"].id, reference_type="work_order")
    assert [row["id"] for row in body] == [ledger["rows"]["wo_by_id"].id]


def test_transactions_filter_by_lot_number(client: TestClient, ledger):
    body = _get_txns(client, ledger, lot_number="LOT-CCC")
    assert {row["id"] for row in body} == {ledger["rows"]["po"].id, ledger["rows"]["plain"].id}
    # LOT-AAA also exists in company B -- that row must stay invisible.
    body = _get_txns(client, ledger, lot_number="LOT-AAA")
    assert {row["id"] for row in body} == {ledger["rows"]["wo_by_id"].id, ledger["rows"]["other_wo"].id}


def test_transactions_filter_by_part_and_transaction_type(client: TestClient, ledger):
    body = _get_txns(client, ledger, part_id=ledger["a_part2"].id)
    assert [row["id"] for row in body] == [ledger["rows"]["other_wo"].id]
    body = _get_txns(client, ledger, transaction_type="ISSUE")
    assert [row["id"] for row in body] == [ledger["rows"]["wo_by_number"].id]


def test_transactions_filter_by_date_range(client: TestClient, ledger):
    """``start_date``/``end_date`` are inclusive bounds on ``created_at``."""
    body = _get_txns(
        client,
        ledger,
        start_date=(LEDGER_EPOCH + timedelta(hours=1)).isoformat(),
        end_date=(LEDGER_EPOCH + timedelta(hours=3)).isoformat(),
    )
    assert {row["id"] for row in body} == {
        ledger["rows"]["wo_by_number"].id,
        ledger["rows"]["other_wo"].id,
        ledger["rows"]["po"].id,
    }
    # Bounds are inclusive on both ends.
    body = _get_txns(client, ledger, start_date=LEDGER_EPOCH.isoformat(), end_date=LEDGER_EPOCH.isoformat())
    assert [row["id"] for row in body] == [ledger["rows"]["wo_by_id"].id]
    # A window after every seeded row is empty.
    assert _get_txns(client, ledger, start_date=(LEDGER_EPOCH + timedelta(days=1)).isoformat()) == []


def test_transactions_work_order_id_matches_both_reference_shapes(client: TestClient, ledger):
    """``work_order_id`` unions the ``reference_id`` shape and the legacy
    ``reference_number`` shape, and excludes everything else."""
    body = _get_txns(client, ledger, work_order_id=ledger["wo"].id)
    assert {row["id"] for row in body} == {
        ledger["rows"]["wo_by_id"].id,
        ledger["rows"]["wo_by_number"].id,
    }
    # Not the other work order, not the same-id purchase-order row, not company B's.
    excluded = {ledger["rows"][k].id for k in ("other_wo", "po", "plain", "foreign")}
    assert {row["id"] for row in body}.isdisjoint(excluded)


def test_transactions_work_order_id_from_other_tenant_returns_nothing(client: TestClient, db_session: Session, ledger):
    """A work-order id belonging to company B resolves no number (tenant-scoped
    lookup) and matches no reference_id in the company-scoped ledger."""
    b_part = make_part(db_session, company_id=COMPANY_B)
    b_wo = make_work_order(db_session, company_id=COMPANY_B, part=b_part)

    assert _get_txns(client, ledger, work_order_id=b_wo.id) == []


def test_transactions_ordering_is_newest_first_with_id_tiebreak(client: TestClient, db_session: Session, ledger):
    body = _get_txns(client, ledger)
    assert [row["id"] for row in body] == [
        ledger["rows"]["plain"].id,
        ledger["rows"]["po"].id,
        ledger["rows"]["other_wo"].id,
        ledger["rows"]["wo_by_number"].id,
        ledger["rows"]["wo_by_id"].id,
    ]

    # Same-instant rows fall back to id DESC, so paging can't duplicate or skip.
    tie_a = make_txn(
        db_session,
        company_id=COMPANY_A,
        part=ledger["a_part"],
        user=ledger["a_user"],
        created_at=LEDGER_EPOCH + timedelta(hours=9),
    )
    tie_b = make_txn(
        db_session,
        company_id=COMPANY_A,
        part=ledger["a_part"],
        user=ledger["a_user"],
        created_at=LEDGER_EPOCH + timedelta(hours=9),
    )
    body = _get_txns(client, ledger)
    assert [row["id"] for row in body][:2] == [tie_b.id, tie_a.id]


def test_transactions_offset_pages_without_overlap(client: TestClient, ledger):
    page1 = _get_txns(client, ledger, limit=2, offset=0)
    page2 = _get_txns(client, ledger, limit=2, offset=2)
    page3 = _get_txns(client, ledger, limit=2, offset=4)

    ids1 = [row["id"] for row in page1]
    ids2 = [row["id"] for row in page2]
    ids3 = [row["id"] for row in page3]
    assert len(ids1) == 2 and len(ids2) == 2 and len(ids3) == 1
    assert set(ids1).isdisjoint(ids2)
    assert set(ids1 + ids2).isdisjoint(ids3)
    # The pages concatenate to the unpaged, newest-first result exactly once each.
    assert ids1 + ids2 + ids3 == [row["id"] for row in _get_txns(client, ledger)]
    # Past the end is empty, not a wrap-around.
    assert _get_txns(client, ledger, limit=2, offset=10) == []


def test_transactions_limit_is_capped_at_500(client: TestClient, ledger):
    """``limit`` is declared ``Query(default=100, le=500)`` -- an over-cap value
    is REJECTED with 422, not silently clamped."""
    resp = client.get(
        "/api/v1/inventory/transactions",
        headers=headers_for(ledger["a_user"]),
        params={"limit": 501},
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, resp.text

    # The cap itself is a valid value.
    ok = client.get(
        "/api/v1/inventory/transactions",
        headers=headers_for(ledger["a_user"]),
        params={"limit": 500},
    )
    assert ok.status_code == status.HTTP_200_OK, ok.text
    assert len(ok.json()) == 5


def test_transactions_serialize_created_at_as_utc_with_z(client: TestClient, ledger):
    """``store UTC, serve UTC (Z), display Central``.

    The endpoint returned raw ORM rows with **no** ``response_model``, so FastAPI's
    ``jsonable_encoder`` emitted a zone-less ``2026-03-01T12:00:00``. The frontend's
    ``toDate`` treats a zone-less string as UTC, but nothing else guarantees that, and
    the invariant is that the wire format carries the ``Z``. It is now typed by
    ``InventoryTransactionResponse``, a ``UTCModel``.
    """
    body = _get_txns(client, ledger)
    assert body, "fixture must produce rows"
    for row in body:
        assert row["created_at"].endswith("Z"), row["created_at"]
    # The value itself is unchanged -- only its rendering gained the zone marker.
    oldest = body[-1]
    assert oldest["created_at"] == LEDGER_EPOCH.isoformat() + "Z"


def test_transactions_response_shape_is_preserved_and_part_is_nested(client: TestClient, ledger):
    """Formalizing the schema must not change the shape callers already receive.

    Every ledger column stays top-level and ``part`` stays a nested object; it is
    narrowed to the identifying fields, since a ledger read has no business
    publishing the part's standard/material/labor/overhead cost (the raw ORM dump
    did).
    """
    row = _get_txns(client, ledger)[0]

    assert set(row) == {
        "id",
        "company_id",
        "inventory_item_id",
        "part_id",
        "transaction_type",
        "quantity",
        "reference_type",
        "reference_id",
        "reference_number",
        "from_location",
        "to_location",
        "lot_number",
        "serial_number",
        "unit_cost",
        "total_cost",
        "notes",
        "reason_code",
        "created_at",
        "created_by",
        "part",
    }
    # transaction_type still serializes as the enum VALUE, not its member NAME.
    assert row["transaction_type"] == "adjust"

    assert set(row["part"]) == {"id", "part_number", "name", "description", "revision", "unit_of_measure"}
    assert row["part"]["id"] == ledger["a_part"].id
    assert row["part"]["part_number"] == ledger["a_part"].part_number
    assert not any(key.endswith("_cost") for key in row["part"]), "cost fields must not ride along on a ledger read"


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": -1}, {"offset": -1}])
def test_transactions_rejects_out_of_range_paging(client: TestClient, ledger, params):
    """``limit`` is ``ge=1, le=500`` and ``offset`` is ``ge=0``.

    Unbounded, a negative value reached the database: ``LIMIT must not be negative``
    is a 500 on Postgres, and SQLite silently treats it as "no limit".
    """
    resp = client.get(
        "/api/v1/inventory/transactions",
        headers=headers_for(ledger["a_user"]),
        params=params,
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, resp.text


# ---------------------------------------------------------------------------
# Movement signs are validated at the schema (finding B5)
# ---------------------------------------------------------------------------


def _sign_fixture(db: Session):
    user = make_user(db, company_id=COMPANY_A, role=UserRole.MANAGER)
    part = make_part(db, company_id=COMPANY_A)
    loc = make_location(db, company_id=COMPANY_A)
    item = make_inventory_item(db, company_id=COMPANY_A, part=part, location=loc.code, qty=10.0)
    return user, part, loc, item


@pytest.mark.parametrize("qty", [0, -5])
def test_receive_rejects_non_positive_quantity(client: TestClient, db_session: Session, qty):
    """A negative RECEIVE removed stock while writing a positive-looking receipt."""
    user, part, loc, item = _sign_fixture(db_session)
    resp = client.post(
        "/api/v1/inventory/receive",
        headers=headers_for(user),
        json={"part_id": part.id, "quantity": qty, "location_code": loc.code},
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, resp.text
    assert float(_reload(db_session, InventoryItem, item.id).quantity_on_hand) == 10.0


@pytest.mark.parametrize("qty", [0, -3])
def test_issue_rejects_non_positive_quantity(client: TestClient, db_session: Session, qty):
    """A negative ISSUE MINTED stock while writing a positive-quantity ISSUE ledger
    row with a negative total_cost."""
    user, part, loc, item = _sign_fixture(db_session)
    resp = client.post(
        "/api/v1/inventory/issue",
        headers=headers_for(user),
        json={"inventory_item_id": item.id, "quantity": qty},
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, resp.text
    assert float(_reload(db_session, InventoryItem, item.id).quantity_on_hand) == 10.0
    assert db_session.query(InventoryTransaction).count() == 0


@pytest.mark.parametrize("qty", [0, -2])
def test_transfer_rejects_non_positive_quantity(client: TestClient, db_session: Session, qty):
    """A negative TRANSFER moved stock dest->source, against locations the response
    never named."""
    user, part, loc, item = _sign_fixture(db_session)
    dest = make_location(db_session, company_id=COMPANY_A)
    resp = client.post(
        "/api/v1/inventory/transfer",
        headers=headers_for(user),
        json={"inventory_item_id": item.id, "quantity": qty, "to_location_code": dest.code},
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, resp.text
    assert float(_reload(db_session, InventoryItem, item.id).quantity_on_hand) == 10.0


def test_adjust_rejects_negative_target_but_allows_zero(client: TestClient, db_session: Session):
    """``new_quantity`` is an absolute target: zero is a legitimate write-off,
    negative is not a state a manual adjustment may dictate."""
    user, part, loc, item = _sign_fixture(db_session)

    refused = client.post(
        "/api/v1/inventory/adjust",
        headers=headers_for(user),
        json={"inventory_item_id": item.id, "new_quantity": -1, "reason_code": "damage"},
    )
    assert refused.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, refused.text
    assert float(_reload(db_session, InventoryItem, item.id).quantity_on_hand) == 10.0

    zeroed = client.post(
        "/api/v1/inventory/adjust",
        headers=headers_for(user),
        json={"inventory_item_id": item.id, "new_quantity": 0, "reason_code": "damage"},
    )
    assert zeroed.status_code == status.HTTP_200_OK, zeroed.text
    assert float(_reload(db_session, InventoryItem, item.id).quantity_on_hand) == 0.0


def test_record_count_rejects_negative_counted_quantity(client: TestClient, db_session: Session):
    """A physical count observation can be zero but never negative -- a negative
    counted quantity would drive on-hand negative at completion."""
    user, part, loc, item = _sign_fixture(db_session)
    count = _seed_cycle_count(db_session, company_id=COMPANY_A, inventory_items=[item])
    count_item = db_session.query(CycleCountItem).filter(CycleCountItem.cycle_count_id == count.id).one()

    resp = client.post(
        f"/api/v1/inventory/cycle-counts/{count.id}/items/{count_item.id}/count",
        headers=headers_for(user),
        json={"counted_quantity": -4},
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, resp.text
    assert _reload(db_session, CycleCountItem, count_item.id).is_counted is False


# ---------------------------------------------------------------------------
# Lot-less receive/transfer merge onto one row (finding B10)
# ---------------------------------------------------------------------------


def test_two_lotless_receives_increment_one_row(client: TestClient, db_session: Session):
    """``lot_number == None`` compiles to ``= NULL`` which never matches, so every
    lot-less receive minted a brand-new fragment row. The IS NULL branch merges them."""
    user = make_user(db_session, company_id=COMPANY_A, role=UserRole.MANAGER)
    part = make_part(db_session, company_id=COMPANY_A)
    loc = make_location(db_session, company_id=COMPANY_A)

    for qty in (4.0, 6.0):
        resp = client.post(
            "/api/v1/inventory/receive",
            headers=headers_for(user),
            json={"part_id": part.id, "quantity": qty, "location_code": loc.code},
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    rows = db_session.query(InventoryItem).filter(InventoryItem.part_id == part.id).all()
    assert len(rows) == 1, "two lot-less receives to the same part+location must share one stock row"
    assert rows[0].lot_number is None
    assert float(rows[0].quantity_on_hand) == 10.0
    assert float(rows[0].quantity_available) == 10.0


def test_lotless_transfer_merges_into_existing_lotless_destination_row(client: TestClient, db_session: Session):
    user = make_user(db_session, company_id=COMPANY_A, role=UserRole.MANAGER)
    part = make_part(db_session, company_id=COMPANY_A)
    src = make_location(db_session, company_id=COMPANY_A)
    dst = make_location(db_session, company_id=COMPANY_A)
    src_item = make_inventory_item(
        db_session, company_id=COMPANY_A, part=part, location=src.code, qty=10.0, lot_number=""
    )
    # Explicit NULL lots on both rows (make_inventory_item defaults to a generated lot).
    src_item.lot_number = None
    dst_item = make_inventory_item(db_session, company_id=COMPANY_A, part=part, location=dst.code, qty=3.0)
    dst_item.lot_number = None
    db_session.commit()

    resp = client.post(
        "/api/v1/inventory/transfer",
        headers=headers_for(user),
        json={"inventory_item_id": src_item.id, "quantity": 4.0, "to_location_code": dst.code},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    db_session.expire_all()
    rows = db_session.query(InventoryItem).filter(InventoryItem.part_id == part.id).all()
    assert len(rows) == 2, "the transfer must merge into the existing lot-less destination row, not mint a third"
    assert float(_reload(db_session, InventoryItem, src_item.id).quantity_on_hand) == 6.0
    assert float(_reload(db_session, InventoryItem, dst_item.id).quantity_on_hand) == 7.0


# ---------------------------------------------------------------------------
# Driven-negative lots stay visible and countable (finding B11)
# ---------------------------------------------------------------------------


def test_list_inventory_default_filter_includes_negative_lots(client: TestClient, db_session: Session):
    """The shortage posture deliberately drives a lot negative; ``has_quantity=True``
    filtered ``> 0`` so the discrepancy was invisible to the one list view."""
    user = make_user(db_session, company_id=COMPANY_A)
    part = make_part(db_session, company_id=COMPANY_A)
    loc = make_location(db_session, company_id=COMPANY_A)
    negative = make_inventory_item(db_session, company_id=COMPANY_A, part=part, location=loc.code, qty=-5.0)
    make_inventory_item(db_session, company_id=COMPANY_A, part=part, location=loc.code, qty=0.0)

    resp = client.get("/api/v1/inventory/", headers=headers_for(user), params={"part_id": part.id})
    assert resp.status_code == status.HTTP_200_OK, resp.text
    ids = {row["id"] for row in resp.json()}
    assert negative.id in ids, "a driven-negative lot must be visible"
    assert len(ids) == 1, "zero rows stay hidden under has_quantity=True"


def test_cycle_count_enrolls_negative_lots(client: TestClient, db_session: Session):
    """A lot the shortage engine drove negative is exactly the row a cycle count
    exists to reconcile; enrolling only ``> 0`` made it permanently uncountable."""
    user = make_user(db_session, company_id=COMPANY_A, role=UserRole.MANAGER)
    part = make_part(db_session, company_id=COMPANY_A)
    loc = make_location(db_session, company_id=COMPANY_A)
    negative = make_inventory_item(db_session, company_id=COMPANY_A, part=part, location=loc.code, qty=-5.0)
    make_inventory_item(db_session, company_id=COMPANY_A, part=part, location=loc.code, qty=0.0)

    resp = client.post(
        "/api/v1/inventory/cycle-counts",
        headers=headers_for(user),
        json={"part_id": part.id, "scheduled_date": str(date.today())},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    count_id = resp.json()["id"]
    enrolled = {
        ci.inventory_item_id
        for ci in db_session.query(CycleCountItem).filter(CycleCountItem.cycle_count_id == count_id).all()
    }
    assert negative.id in enrolled, "a driven-negative lot must be countable"


def test_inventory_export_default_filter_includes_negative_lots(client: TestClient, db_session: Session):
    """B11, export leg: the exported spreadsheet is what a manager reconciles from, so
    ``/exports/inventory/export`` must apply the same ``!= 0`` predicate as the list
    view -- a driven-negative lot visible on screen but absent from the export would
    hide the discrepancy exactly where it gets acted on."""
    user = make_user(db_session, company_id=COMPANY_A)
    part = make_part(db_session, company_id=COMPANY_A)
    loc = make_location(db_session, company_id=COMPANY_A)
    make_inventory_item(db_session, company_id=COMPANY_A, part=part, location=loc.code, qty=-5.0)
    make_inventory_item(db_session, company_id=COMPANY_A, part=part, location=loc.code, qty=0.0)

    resp = client.get(
        "/api/v1/exports/inventory/export",
        headers=headers_for(user),
        params={"format": "csv"},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    rows = [line for line in resp.text.strip().splitlines() if part.part_number in line]
    assert len(rows) == 1, "the negative lot must export; the zero row stays hidden"
    assert "-5" in rows[0], "the exported row carries the negative on-hand"


# ---------------------------------------------------------------------------
# Cycle-count completion posts the CURRENT-basis delta (finding B4)
# ---------------------------------------------------------------------------


def test_complete_posts_current_basis_delta_after_mid_window_movement(client: TestClient, db_session: Session):
    """Enroll at 100, count 98, engine issues 5 mid-window (on-hand 95). Completion
    must land on-hand at the counted 98 and post a COUNT row of +3 (the CURRENT-basis
    delta), so SUM(ledger) == on-hand. Posting the enrollment variance (-2) while
    setting on-hand absolutely silently resurrected the consumed 5 in the ledger."""
    user = make_user(db_session, company_id=COMPANY_A, role=UserRole.MANAGER)
    part = make_part(db_session, company_id=COMPANY_A)
    loc = make_location(db_session, company_id=COMPANY_A)
    item = make_inventory_item(db_session, company_id=COMPANY_A, part=part, location=loc.code, qty=100.0, unit_cost=2.0)
    count = _seed_cycle_count(db_session, company_id=COMPANY_A, inventory_items=[item])
    _mark_counted(db_session, count, {item.id: 98.0})

    # Ledger before the count: the receive that put the 100 on hand...
    receive = InventoryTransaction(
        company_id=COMPANY_A,
        inventory_item_id=item.id,
        part_id=part.id,
        transaction_type=TransactionType.RECEIVE,
        quantity=100.0,
        created_by=user.id,
    )
    # ...and the operation-completion consumption that moved 5 between enrollment
    # and completion (the ledger row plus the on-hand decrement the engine writes).
    issue = InventoryTransaction(
        company_id=COMPANY_A,
        inventory_item_id=item.id,
        part_id=part.id,
        transaction_type=TransactionType.ISSUE,
        quantity=-5.0,
        created_by=user.id,
    )
    db_session.add_all([receive, issue])
    item.quantity_on_hand = 95.0
    item.quantity_available = 95.0
    db_session.commit()

    resp = client.post(
        f"/api/v1/inventory/cycle-counts/{count.id}/complete?apply_adjustments=true",
        headers=headers_for(user),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["items_adjusted"] == 1

    after = _reload(db_session, InventoryItem, item.id)
    assert float(after.quantity_on_hand) == 98.0
    assert float(after.quantity_available) == 98.0

    count_txn = db_session.query(InventoryTransaction).filter(InventoryTransaction.transaction_type == "COUNT").one()
    assert float(count_txn.quantity) == 3.0, "the ledger row carries the CURRENT-basis delta"
    # Both bases are stated on the row.
    assert "on-hand at completion: 95.0" in count_txn.notes
    assert "system at enrollment: 100.0" in count_txn.notes

    ledger_sum = sum(
        float(t.quantity)
        for t in db_session.query(InventoryTransaction).filter(InventoryTransaction.inventory_item_id == item.id).all()
    )
    assert ledger_sum == float(after.quantity_on_hand), "SUM(ledger) must equal on-hand"

    # The enrollment-basis variance stays as the recorded quality figure.
    ci = db_session.query(CycleCountItem).filter(CycleCountItem.cycle_count_id == count.id).one()
    assert float(ci.variance) == -2.0
    # And the posted figure prices what actually hit the ledger: +3 @ 2.0.
    assert float(_reload(db_session, CycleCount, count.id).total_variance_value) == 6.0


def test_complete_posts_no_ledger_row_when_current_delta_is_zero(client: TestClient, db_session: Session):
    """Enrollment variance -5, but the engine already moved exactly 5: on-hand equals
    the counted figure, so there is no stock movement to record -- no COUNT row, no
    on-hand change, count still completes."""
    user = make_user(db_session, company_id=COMPANY_A, role=UserRole.MANAGER)
    part = make_part(db_session, company_id=COMPANY_A)
    loc = make_location(db_session, company_id=COMPANY_A)
    item = make_inventory_item(db_session, company_id=COMPANY_A, part=part, location=loc.code, qty=100.0)
    count = _seed_cycle_count(db_session, company_id=COMPANY_A, inventory_items=[item])
    _mark_counted(db_session, count, {item.id: 95.0})
    item.quantity_on_hand = 95.0
    item.quantity_available = 95.0
    db_session.commit()

    resp = client.post(
        f"/api/v1/inventory/cycle-counts/{count.id}/complete?apply_adjustments=true",
        headers=headers_for(user),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["items_adjusted"] == 0
    assert float(body["total_variance_value"]) == 0.0
    assert float(body["measured_variance_value"]) == -10.0, "enrollment-basis quality figure is preserved"

    assert db_session.query(InventoryTransaction).filter(InventoryTransaction.transaction_type == "COUNT").count() == 0
    assert float(_reload(db_session, InventoryItem, item.id).quantity_on_hand) == 95.0
    assert _reload(db_session, CycleCount, count.id).status == CycleCountStatus.COMPLETED


def test_complete_skips_ledger_row_on_sub_epsilon_float_residue(client: TestClient, db_session: Session):
    """The zero-delta skip uses the shared ledger epsilon, not exact float equality:
    after fractional consumption on-hand can sit a ~1e-15 residue away from the counted
    figure, and posting a COUNT row for that residue would be pure noise. On-hand is
    still snapped to the counted figure."""
    user = make_user(db_session, company_id=COMPANY_A, role=UserRole.MANAGER)
    part = make_part(db_session, company_id=COMPANY_A)
    loc = make_location(db_session, company_id=COMPANY_A)
    item = make_inventory_item(db_session, company_id=COMPANY_A, part=part, location=loc.code, qty=10.0)
    count = _seed_cycle_count(db_session, company_id=COMPANY_A, inventory_items=[item])
    _mark_counted(db_session, count, {item.id: 9.7})
    # The classic binary-float residue: 10.0 - 0.1 - 0.2 != 9.7 exactly.
    item.quantity_on_hand = 10.0 - 0.1 - 0.2
    item.quantity_available = item.quantity_on_hand
    db_session.commit()
    assert item.quantity_on_hand != 9.7, "precondition: the fixture must carry a real float residue"

    resp = client.post(
        f"/api/v1/inventory/cycle-counts/{count.id}/complete?apply_adjustments=true",
        headers=headers_for(user),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["items_adjusted"] == 0

    assert db_session.query(InventoryTransaction).filter(InventoryTransaction.transaction_type == "COUNT").count() == 0
    assert float(_reload(db_session, InventoryItem, item.id).quantity_on_hand) == 9.7, "on-hand snaps to the count"


# ---------------------------------------------------------------------------
# POST /inventory/locations writes a CREATE audit row (finding B9)
# ---------------------------------------------------------------------------


def test_create_location_writes_a_create_audit_row(client: TestClient, db_session: Session):
    """Invariant #2: a location is the scoping anchor for receives, transfers and
    cycle counts -- its creation is a state change the hash chain must record."""
    user = make_user(db_session, company_id=COMPANY_A, role=UserRole.MANAGER)

    resp = client.post(
        "/api/v1/inventory/locations",
        headers=headers_for(user),
        json={"code": "AUDIT-LOC-1", "warehouse": "MAIN"},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    location_id = resp.json()["id"]

    rows = _audit_rows(db_session, resource_type="inventory_location", action="CREATE")
    assert len(rows) == 1
    assert rows[0].resource_id == location_id
    assert rows[0].resource_identifier == "AUDIT-LOC-1"
    assert rows[0].company_id == COMPANY_A
    assert rows[0].user_id == user.id
    _assert_hash_chain_intact(db_session)
