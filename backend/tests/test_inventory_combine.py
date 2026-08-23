"""Folding two SKUs that describe the same physical article into one.

THE RECORD THIS FILE IS
-----------------------
A materials-numbering recut left 92 sheets on ``.0625-60X144-304SS`` and 141 on
``SH-A240-304-0.0625-60X144-2B`` -- 233 sheets, one rack, two numbers. The five
ACCEPTANCE tests below are named after what the owner asked for and use those
literal strings and quantities on purpose: they are the record that the feature
does what was asked, not generic fixtures that happen to exercise the code.

  A1 ``test_combine_recut_sheet_leaves_zero_and_233``
  A2 ``test_combine_ledger_rows_net_to_zero``
  A3 ``test_second_combine_of_zero_is_blocked``
  A4 ``test_combine_blocked_by_open_work_order_reservation``
     + ``test_combine_never_moves_allocated_quantity``
  A5 ``test_combine_is_atomic_on_failure``

WHY EACH OF THEM IS WRITTEN THE WAY IT IS
-----------------------------------------
* **A1 asserts the TOTAL, not just the two halves.** "Source 0, target 233" is
  also true of a combine that invented 92 sheets on the way across; only
  "source + target is still exactly what it was before" rules that out.
* **A2 sums the ledger.** The two ``ADJUST`` rows per moved lot line netting to
  exactly zero IS the safety argument -- it is what makes a combine provably
  neither a receipt (which would mint stock) nor an issue against a fabricated
  work order (which would destroy it). A test that only counted rows would pass
  with both signs positive.
* **A3 pins the two DIFFERENT refusals a "zero" request gets**: a drained SKU is
  **409** ``no_available_stock`` (state refuses a well-formed request) and a
  literal ``quantity: 0`` body is **422** (the field is ``gt=0``, so the handler
  never runs). Conflating them would hide a whole class of regression.
* **A4 covers BOTH reservation sources**, because they are independent and only
  one of them is waivable-looking: the part-level open-work-order tie (refused
  with the work orders NAMED, and a ``max_combinable_quantity`` that is offered
  and then proven to work), and the row-level ``quantity_allocated`` cap enforced
  at the draw itself.
* **A5 forces a failure part-way through a MULTI-line combine.** A single-line
  failure proves nothing about atomicity -- the interesting claim is that line 1's
  stock change, ledger rows and header row all disappear when line 2 raises.

Everything else here pins a decision from the spec that a plausible "improvement"
would break: the preview is a structurally pure read, a blank unit of measure is
NOT a mismatch, the flagged-part gate matches on word boundaries (so ``TESTA-500``
and ``WAREHOUSING`` must not flag), traceability follows the material onto a new
target row, an existing target row's cost basis is never reblended, held stock is
never folded, and the ledger rows sit outside both work-order idempotency
predicates and outside ``work_order_ledger_filter``.

THE SECOND HALF OF THIS FILE IS THE REVIEW FINDINGS (B1-B9)
-----------------------------------------------------------
Everything from "THE REVIEW FINDINGS" downwards reproduces a defect that SHIPPED
in the first cut of this verb and was caught by review rather than by a test.
Each of those tests states what the bug WAS, because several of them would have
passed against the broken code with slightly different fixtures -- the sentence
describing the failure is what stops a later "simplification" reintroducing it:

  B1 a stale identity-map instance silently ate a concurrently received 50 units
  B2 usable stock folded onto an on-hold target row and became unusable, **200**
  B3 ``quantity: 1e-10`` wrote an immutable header for a combine that never happened
  B4 the open-tie check counted held stock the consumption engine cannot draw
  B5 the check was blind to ``pinned_inventory_item_id`` and drained the pinned lot
  B6 two units ended up under one serial number
  B7 ``deactivate_source`` retired a part with material on the shelf
  B8 the header quantity was re-derived by subtraction and drifted from the ledger
  B9 the audit chain lock was taken mid-loop, ahead of later target-row locks

Two of them, B1 and B9, are ORDERING claims that SQLite cannot observe directly
(it ignores ``FOR UPDATE``), so they are pinned on what IS observable and is the
actual property: the value the write lands on, and the order the calls happen in.

Fixture conventions follow ``tests/api/test_inventory_hardening.py``: company A is
the seeded default (id=1), company B is a second tenant, rows are created on the
shared ``db_session``, tokens are minted directly, and a cross-tenant call must
answer **404** *and* leave the foreign rows byte-identical.

That shared session is also what makes B1 testable at all: ``conftest``'s
``override_get_db`` hands the endpoint the very Session the test holds, so seeding
the identity map here is the same act the deleted unlocked read performed.
"""

from datetime import datetime
from typing import Optional, Sequence

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.db.ledger_filter import WORK_ORDER_REFERENCE_TYPES, work_order_ledger_filter
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.inventory import (
    WO_ISSUE_INDEX_PREDICATE,
    WO_RECEIPT_INDEX_PREDICATE,
    InventoryItem,
    InventoryTransaction,
    TransactionType,
)
from app.models.inventory_combine import (
    COMBINE_IN_REASON_CODE,
    COMBINE_OUT_REASON_CODE,
    COMBINE_REFERENCE_TYPE,
    InventoryCombine,
)
from app.models.operational_event import OperationalEvent
from app.models.part import Part
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.models.work_order_material import (
    AllocationSource,
    AllocationStatus,
    WorkOrderMaterialAllocation,
)
from app.services import inventory_combine_service

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2

PREVIEW_URL = "/api/v1/inventory/combine/preview"
COMBINE_URL = "/api/v1/inventory/combine"

# The owner's literal case. These strings are the acceptance record — do not
# "tidy" them into fixture-generated numbers.
SOURCE_NUMBER = ".0625-60X144-304SS"
TARGET_NUMBER = "SH-A240-304-0.0625-60X144-2B"

REASON = "Materials recut left one sheet on two numbers"

# Tokens are minted directly; this hash is never used for a login.
FIXTURE_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"

_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


def _ensure_company(db: Session, company_id: int) -> Company:
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        company = Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True)
        db.add(company)
        db.commit()
    return company


def _user(db: Session, *, role: UserRole = UserRole.ADMIN, company_id: int = COMPANY_A) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"combine-{n}@co{company_id}.test",
        employee_id=f"COMB-{n:05d}",
        first_name="Combine",
        last_name=f"C{company_id}",
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


def _part(
    db: Session,
    *,
    number: str,
    name: str = "Sheet stock plate",
    uom: Optional[str] = "each",
    part_type: str = "raw_material",
    company_id: int = COMPANY_A,
    is_deleted: bool = False,
) -> Part:
    """A part. ``uom=None`` lands a genuinely NULL ``unit_of_measure``.

    The column carries a PYTHON-side default (``UnitOfMeasure.EACH``) and no
    server default, so an explicit ``None`` on the INSERT is simply omitted and
    the default applies. A follow-up UPDATE is the only way to reach the NULL a
    legacy row can genuinely hold — which is exactly the state the "blank is not
    a mismatch" rule is about.
    """
    _ensure_company(db, company_id)
    part = Part(
        part_number=number,
        name=name,
        description=name,
        part_type=part_type,
        unit_of_measure=uom or "each",
        is_active=True,
        is_deleted=is_deleted,
        company_id=company_id,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    if uom is None:
        db.execute(text("UPDATE parts SET unit_of_measure = NULL WHERE id = :pid"), {"pid": part.id})
        db.commit()
        db.refresh(part)
    return part


def _stock(
    db: Session,
    part: Part,
    *,
    location: str,
    qty: float,
    lot: Optional[str] = None,
    allocated: float = 0.0,
    status: str = "available",
    is_active: bool = True,
    unit_cost: float = 1.0,
    warehouse: str = "MAIN",
    serial_number: Optional[str] = None,
    cert_number: Optional[str] = None,
    heat_lot: Optional[str] = None,
    supplier_id: Optional[int] = None,
    po_number: Optional[str] = None,
    received_date: Optional[datetime] = None,
    expiration_date: Optional[datetime] = None,
    company_id: Optional[int] = None,
) -> InventoryItem:
    item = InventoryItem(
        part_id=part.id,
        location=location,
        warehouse=warehouse,
        quantity_on_hand=qty,
        quantity_allocated=allocated,
        quantity_available=qty - allocated,
        lot_number=lot,
        serial_number=serial_number,
        unit_cost=unit_cost,
        cert_number=cert_number,
        heat_lot=heat_lot,
        supplier_id=supplier_id,
        po_number=po_number,
        received_date=received_date,
        expiration_date=expiration_date,
        status=status,
        is_active=is_active,
        company_id=company_id if company_id is not None else part.company_id,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _on_hand(db: Session, part_id: int, company_id: int = COMPANY_A) -> float:
    db.expire_all()
    rows = (
        db.query(InventoryItem).filter(InventoryItem.company_id == company_id, InventoryItem.part_id == part_id).all()
    )
    return float(sum(float(r.quantity_on_hand or 0.0) for r in rows))


def _preview(
    client: TestClient,
    headers: dict,
    source: Part,
    target: Part,
    quantity: Optional[float] = None,
):
    body = {"source_part_id": source.id, "target_part_id": target.id}
    if quantity is not None:
        body["quantity"] = quantity
    return client.post(PREVIEW_URL, json=body, headers=headers)


def _combine(
    client: TestClient,
    headers: dict,
    source: Part,
    target: Part,
    quantity: float,
    *,
    reason: str = REASON,
    expected_source: Optional[str] = None,
    expected_target: Optional[str] = None,
    acknowledge: Optional[Sequence[int]] = None,
    deactivate_source: bool = False,
):
    payload = {
        "source_part_id": source.id,
        "target_part_id": target.id,
        "quantity": quantity,
        "reason": reason,
        "expected_source_part_number": (expected_source if expected_source is not None else source.part_number),
        "expected_target_part_number": (expected_target if expected_target is not None else target.part_number),
        "acknowledge_flagged_part_ids": list(acknowledge or []),
        "deactivate_source": deactivate_source,
    }
    return client.post(COMBINE_URL, json=payload, headers=headers)


def _codes(diagnostics) -> set:
    return {d["code"] for d in diagnostics}


def _combine_txns(db: Session, combine_id: int) -> list:
    db.expire_all()
    return (
        db.query(InventoryTransaction)
        .filter(
            InventoryTransaction.reference_type == COMBINE_REFERENCE_TYPE,
            InventoryTransaction.reference_id == combine_id,
        )
        .order_by(InventoryTransaction.id.asc())
        .all()
    )


def _counts(db: Session) -> dict:
    """Row counts of everything a combine can write. Used for 'nothing happened'."""
    db.expire_all()
    return {
        "audit": db.query(AuditLog).count(),
        "txn": db.query(InventoryTransaction).count(),
        "item": db.query(InventoryItem).count(),
        "combine": db.query(InventoryCombine).count(),
        "event": db.query(OperationalEvent).count(),
    }


@pytest.fixture
def admin(db_session: Session) -> User:
    return _user(db_session, role=UserRole.ADMIN)


@pytest.fixture
def admin_hdrs(admin: User) -> dict:
    return _headers(admin)


@pytest.fixture
def recut_pair(db_session: Session):
    """The owner's literal case: 92 on the old number, 141 on the new one."""
    source = _part(db_session, number=SOURCE_NUMBER, name="Sheet 16GA 304 stainless 60 x 144")
    target = _part(db_session, number=TARGET_NUMBER, name="Sheet A240 304 2B 0.0625 60 x 144")
    _stock(db_session, source, location="RECV-01", qty=92.0, lot="RCV-20260813-005", unit_cost=118.5)
    _stock(db_session, target, location="RACK-12", qty=141.0, lot="LOT-20260701-001", unit_cost=121.0)
    return source, target


# --------------------------------------------------------------------------- #
# A1 — the acceptance case
# --------------------------------------------------------------------------- #


def test_combine_recut_sheet_leaves_zero_and_233(db_session: Session, client: TestClient, admin_hdrs, recut_pair):
    """92 from '.0625-60X144-304SS' onto 'SH-A240-304-0.0625-60X144-2B' -> 0 and 233.

    ACCEPTANCE 1. The TOTAL assertion is the load-bearing one: "source 0, target
    233" is equally true of a combine that minted 92 sheets on the way across.
    Only "the two together are still exactly 233" rules that out.
    """
    source, target = recut_pair
    total_before = _on_hand(db_session, source.id) + _on_hand(db_session, target.id)
    assert total_before == 233.0

    response = _combine(client, admin_hdrs, source, target, 92.0)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["quantity_moved"] == 92.0
    assert body["lines_moved"] == 1
    assert body["source_part_number"] == SOURCE_NUMBER
    assert body["target_part_number"] == TARGET_NUMBER
    assert body["source_quantity_before"] == 92.0
    assert body["source_quantity_after"] == 0.0
    assert body["target_quantity_before"] == 141.0
    assert body["target_quantity_after"] == 233.0
    assert body["combine_number"].startswith("COMB-")

    assert _on_hand(db_session, source.id) == 0.0
    assert _on_hand(db_session, target.id) == 233.0
    # Nothing minted, nothing destroyed.
    assert _on_hand(db_session, source.id) + _on_hand(db_session, target.id) == total_before

    # The source part is NEVER deleted — it stays in the catalog at qty 0 so every
    # traveler, MTR and PO naming it keeps resolving.
    db_session.expire_all()
    reloaded = db_session.query(Part).filter(Part.id == source.id).first()
    assert reloaded is not None
    assert reloaded.is_deleted is False
    assert reloaded.is_active is True

    # The lot travelled: the target's new row carries the source's lot number.
    moved_row = (
        db_session.query(InventoryItem)
        .filter(InventoryItem.part_id == target.id, InventoryItem.lot_number == "RCV-20260813-005")
        .first()
    )
    assert moved_row is not None
    assert moved_row.quantity_on_hand == 92.0


# --------------------------------------------------------------------------- #
# A2 — the ledger identity
# --------------------------------------------------------------------------- #


def test_combine_ledger_rows_net_to_zero(db_session: Session, client: TestClient, admin_hdrs):
    """The 2N ADJUST rows one combine writes sum to EXACTLY zero.

    ACCEPTANCE 2. This identity is the whole safety argument: a combine is
    provably neither a receipt (which mints stock) nor an issue against a
    fabricated work order (which destroys it). Also pins the pairing — same
    location, same lot, OUT row on the source part, IN row on the target.
    """
    source = _part(db_session, number=f"OLD-{_next():04d}", name="Plate stock")
    target = _part(db_session, number=f"NEW-{_next():04d}", name="Plate stock")
    _stock(db_session, source, location="A-1", qty=5.0, lot="L1", unit_cost=10.0)
    _stock(db_session, source, location="A-2", qty=7.0, lot="L2", unit_cost=11.0)
    _stock(db_session, target, location="B-1", qty=3.0, lot="L9", unit_cost=12.0)

    response = _combine(client, admin_hdrs, source, target, 12.0)
    assert response.status_code == 200, response.text
    body = response.json()
    combine_id = body["combine_id"]
    assert body["lines_moved"] == 2

    txns = _combine_txns(db_session, combine_id)
    assert len(txns) == 4, "two ledger rows per moved lot line"
    assert [t.id for t in txns] == sorted(body["transaction_ids"])

    total = sum(float(t.quantity) for t in txns)
    assert total == pytest.approx(0.0, abs=1e-9)

    outs = [t for t in txns if t.reason_code == COMBINE_OUT_REASON_CODE]
    ins = [t for t in txns if t.reason_code == COMBINE_IN_REASON_CODE]
    assert len(outs) == 2 and len(ins) == 2

    for txn in txns:
        assert txn.transaction_type == TransactionType.ADJUST
        assert txn.reference_type == COMBINE_REFERENCE_TYPE
        assert txn.reference_number == body["combine_number"]
        assert txn.company_id == COMPANY_A
        # The material does not physically move; only its SKU changes.
        assert txn.from_location == txn.to_location

    for out_txn in outs:
        assert out_txn.part_id == source.id
        assert out_txn.quantity < 0
    for in_txn in ins:
        assert in_txn.part_id == target.id
        assert in_txn.quantity > 0

    # Each pair shares its location and lot, and cancels exactly.
    by_lot = {}
    for txn in txns:
        by_lot.setdefault((txn.from_location, txn.lot_number), []).append(txn)
    assert set(by_lot) == {("A-1", "L1"), ("A-2", "L2")}
    for pair in by_lot.values():
        assert len(pair) == 2
        assert sum(float(t.quantity) for t in pair) == pytest.approx(0.0, abs=1e-9)


# --------------------------------------------------------------------------- #
# A3 — a second combine of a drained SKU
# --------------------------------------------------------------------------- #


def test_second_combine_of_zero_is_blocked(db_session: Session, client: TestClient, admin_hdrs, recut_pair):
    """Once drained, a second combine is 409 — and a literal quantity 0 is 422.

    ACCEPTANCE 3. Two DIFFERENT refusals, deliberately not conflated: a drained
    SKU is a **409** ``no_available_stock`` (a well-formed request the current
    state refuses) while ``quantity: 0`` is a **422** rejected by ``Field(gt=0)``
    before the handler ever runs. Both must leave the database untouched.
    """
    source, target = recut_pair
    first = _combine(client, admin_hdrs, source, target, 92.0)
    assert first.status_code == 200, first.text

    before = _counts(db_session)
    source_on_hand = _on_hand(db_session, source.id)
    target_on_hand = _on_hand(db_session, target.id)

    # The preview says so, in the same words the write will use.
    preview = _preview(client, admin_hdrs, source, target)
    assert preview.status_code == 200, preview.text
    pv = preview.json()
    assert pv["eligible"] is False
    assert "no_available_stock" in _codes(pv["blockers"])
    assert pv["default_quantity"] == 0.0
    assert pv["source"]["eligible_available"] == 0.0

    # ORDERING IS DELIBERATE and is asserted, not assumed. A drained SKU previews
    # with ``quantity = default_quantity = 0``, which ALSO trips the newer
    # ``quantity_below_minimum`` probe. That probe is ordered AFTER the availability
    # one precisely so the operator reads "there is nothing left to move" rather
    # than "0 is too small a number to type" — the second sentence is true and
    # useless. Both may appear; ``no_available_stock`` must come first.
    codes = [b["code"] for b in pv["blockers"]]
    assert codes.index("no_available_stock") == 0

    # The write refuses with 409, not 422 — the request is well formed.
    refused = _combine(client, admin_hdrs, source, target, 1.0)
    assert refused.status_code == 409, refused.text
    assert "no available stock" in refused.json()["detail"].lower()

    # A literal 0 never reaches the handler.
    zero = _combine(client, admin_hdrs, source, target, 0.0)
    assert zero.status_code == 422, zero.text

    assert _counts(db_session) == before
    assert _on_hand(db_session, source.id) == source_on_hand
    assert _on_hand(db_session, target.id) == target_on_hand


# --------------------------------------------------------------------------- #
# A4 — the two reservation rules
# --------------------------------------------------------------------------- #


def _work_order(db: Session, part: Part, *, status=WorkOrderStatus.RELEASED, company_id: int = COMPANY_A) -> WorkOrder:
    n = _next()
    wo = WorkOrder(
        work_order_number=f"WO-COMB-{n:05d}",
        part_id=part.id,
        quantity_ordered=10,
        status=status,
        company_id=company_id,
    )
    db.add(wo)
    db.commit()
    db.refresh(wo)
    return wo


def _tie(
    db: Session,
    wo: WorkOrder,
    part: Part,
    *,
    qty_planned: float,
    qty_consumed: float = 0.0,
    status=AllocationStatus.OPEN,
    company_id: int = COMPANY_A,
    pinned_item: Optional[InventoryItem] = None,
) -> WorkOrderMaterialAllocation:
    """An open work-order material tie on ``part``.

    ``pinned_item`` sets ``pinned_inventory_item_id`` — a LOT-DIRECTED tie. That is
    not a variation on the same claim, it is a different SHAPE of claim: the
    consumption engine locks that one row and draws from it alone, so the demand
    binds per row (like ``quantity_allocated``) rather than against the part's
    total. The combine has to withhold it at the row it names, which is what
    ``test_combine_withholds_a_pinned_lot_from_the_drain`` pins.
    """
    allocation = WorkOrderMaterialAllocation(
        company_id=company_id,
        work_order_id=wo.id,
        work_order_operation_id=None,
        part_id=part.id,
        source=AllocationSource.MANUAL,
        status=status,
        qty_per_run=None,
        qty_planned=qty_planned,
        unit_of_measure="each",
        qty_consumed=qty_consumed,
        pinned_inventory_item_id=(pinned_item.id if pinned_item is not None else None),
    )
    db.add(allocation)
    db.commit()
    db.refresh(allocation)
    return allocation


def test_combine_blocked_by_open_work_order_reservation(db_session: Session, client: TestClient, admin_hdrs):
    """An open tie that the fold would strand refuses the combine, and names the job.

    ACCEPTANCE 4 (part-level). The basis is the PLAN, not live consumption: a
    released job that has not run a single part yet still expects to draw, and
    stranding it is precisely what this check exists to prevent. The preview must
    also offer ``max_combinable_quantity`` — and that number has to actually work,
    or the dialog is proposing a second dead end.

    THE BASIS IS ``eligible_available``, NOT ``total_on_hand``, and this fixture
    is deliberately arranged so the two coincide (one clean 100-unit row, nothing
    held, nothing pinned) — which means the ``70.0`` below would pass against the
    OLD, wrong arithmetic too. The formula is therefore asserted here in its own
    right, and the case where the two bases DISAGREE has its own test:
    ``test_reservation_check_counts_only_stock_the_engine_can_draw``.
    """
    source = _part(db_session, number=f"RSV-SRC-{_next():04d}", name="Bar stock")
    target = _part(db_session, number=f"RSV-TGT-{_next():04d}", name="Bar stock")
    _stock(db_session, source, location="A-1", qty=100.0, lot="L1", unit_cost=4.0)
    _stock(db_session, target, location="B-1", qty=1.0, lot="L2", unit_cost=4.0)

    wo = _work_order(db_session, source)
    _tie(db_session, wo, source, qty_planned=30.0, qty_consumed=0.0)

    preview = _preview(client, admin_hdrs, source, target)
    assert preview.status_code == 200, preview.text
    pv = preview.json()
    assert pv["reserved_quantity"] == 30.0
    assert "open_work_order_reservation" in _codes(pv["blockers"])
    assert [r["work_order_number"] for r in pv["open_source_reservations"]] == [wo.work_order_number]
    assert pv["open_source_reservations"][0]["outstanding_quantity"] == 30.0
    # Strictly less than the full available quantity, and the safe number.
    assert pv["default_quantity"] == 100.0
    assert pv["max_combinable_quantity"] == 70.0
    assert pv["max_combinable_quantity"] < pv["default_quantity"]
    # Stated as the FORMULA, not as a literal that happens to match: the cap is
    # eligible-available minus unpinned open demand. Nothing here is pinned, so
    # the whole 30 is charged against the remainder.
    assert pv["source"]["eligible_available"] == 100.0
    assert pv["source"]["total_pinned"] == 0.0
    assert pv["max_combinable_quantity"] == pv["source"]["eligible_available"] - pv["reserved_quantity"]
    # And the refusal quotes the number the RULE used, in the words the operator
    # reads — "available", never "on hand".
    assert "available" in next(b["detail"] for b in pv["blockers"] if b["code"] == "open_work_order_reservation")

    refused = _combine(client, admin_hdrs, source, target, 92.0)
    assert refused.status_code == 409, refused.text
    detail = refused.json()["detail"]
    assert wo.work_order_number in detail
    assert _on_hand(db_session, source.id) == 100.0
    assert db_session.query(InventoryCombine).count() == 0

    # The offered number is not a dead end.
    ok = _combine(client, admin_hdrs, source, target, pv["max_combinable_quantity"])
    assert ok.status_code == 200, ok.text
    assert _on_hand(db_session, source.id) == 30.0
    assert _on_hand(db_session, target.id) == 71.0


def test_combine_reservation_ignores_terminal_and_cancelled_ties(db_session: Session, client: TestClient, admin_hdrs):
    """A COMPLETE job and a CANCELLED tie reserve nothing.

    Nothing currently closes a tie at work-order completion, so counting terminal
    jobs would block this verb forever on any part the shop has ever consumed.
    """
    source = _part(db_session, number=f"RSV2-SRC-{_next():04d}", name="Bar stock")
    target = _part(db_session, number=f"RSV2-TGT-{_next():04d}", name="Bar stock")
    _stock(db_session, source, location="A-1", qty=10.0, lot="L1")
    _stock(db_session, target, location="B-1", qty=0.0, lot="L2")

    done = _work_order(db_session, source, status=WorkOrderStatus.COMPLETE)
    _tie(db_session, done, source, qty_planned=99.0)
    live = _work_order(db_session, source)
    _tie(db_session, live, source, qty_planned=99.0, status=AllocationStatus.CANCELLED)
    # A fully-consumed OPEN tie has no outstanding demand either.
    running = _work_order(db_session, source)
    _tie(db_session, running, source, qty_planned=5.0, qty_consumed=5.0)

    preview = _preview(client, admin_hdrs, source, target)
    assert preview.status_code == 200, preview.text
    pv = preview.json()
    assert pv["reserved_quantity"] == 0.0
    assert pv["open_source_reservations"] == []
    assert "open_work_order_reservation" not in _codes(pv["blockers"])

    ok = _combine(client, admin_hdrs, source, target, 10.0)
    assert ok.status_code == 200, ok.text


def test_combine_never_moves_allocated_quantity(db_session: Session, client: TestClient, admin_hdrs):
    """The row-level ``quantity_allocated`` cap is hard and can never be moved into.

    ACCEPTANCE 4 (row-level). Enforced at the draw itself, so no caller and no
    future flag can waive it: 10 on hand with 4 allocated can move at most 6, and
    what is left behind still carries its allocation.
    """
    source = _part(db_session, number=f"ALLOC-SRC-{_next():04d}", name="Bar stock")
    target = _part(db_session, number=f"ALLOC-TGT-{_next():04d}", name="Bar stock")
    row = _stock(db_session, source, location="A-1", qty=10.0, allocated=4.0, lot="L1", unit_cost=3.0)
    _stock(db_session, target, location="B-1", qty=0.0, lot="L2")

    preview = _preview(client, admin_hdrs, source, target)
    pv = preview.json()
    assert pv["default_quantity"] == 6.0
    assert pv["source"]["total_on_hand"] == 10.0
    assert pv["source"]["total_allocated"] == 4.0
    assert pv["source"]["eligible_available"] == 6.0

    refused = _combine(client, admin_hdrs, source, target, 10.0)
    assert refused.status_code == 409, refused.text
    assert "available" in refused.json()["detail"].lower()

    ok = _combine(client, admin_hdrs, source, target, 6.0)
    assert ok.status_code == 200, ok.text

    db_session.expire_all()
    reloaded = db_session.query(InventoryItem).filter(InventoryItem.id == row.id).first()
    assert reloaded.quantity_on_hand == 4.0
    assert reloaded.quantity_allocated == 4.0
    assert reloaded.quantity_available == 0.0
    assert _on_hand(db_session, target.id) == 6.0


# --------------------------------------------------------------------------- #
# A5 — atomicity
# --------------------------------------------------------------------------- #


def test_combine_is_atomic_on_failure(db_session: Session, client: TestClient, admin_hdrs, monkeypatch):
    """A failure part-way through a MULTI-line combine leaves nothing behind.

    ACCEPTANCE 5. The audit writer for the SECOND line is made to raise, so line
    one's stock decrement, its target row, its two ledger rows and the header row
    all already exist in the session when the failure lands. Everything must
    disappear — which is only true if the endpoint's ``atomic_transaction``
    genuinely covers every write, not merely the last one.
    """
    source = _part(db_session, number=f"ATOM-SRC-{_next():04d}", name="Plate stock")
    target = _part(db_session, number=f"ATOM-TGT-{_next():04d}", name="Plate stock")
    row_a = _stock(db_session, source, location="A-1", qty=10.0, lot="L1", unit_cost=8.0)
    row_b = _stock(db_session, source, location="A-2", qty=10.0, lot="L2", unit_cost=8.0)
    target_row = _stock(db_session, target, location="B-1", qty=4.0, lot="L9", unit_cost=8.0)

    before = _counts(db_session)
    source_before = _on_hand(db_session, source.id)
    target_before = _on_hand(db_session, target.id)
    assert source_before == 20.0 and target_before == 4.0

    original = inventory_combine_service._audit_combine_line
    calls = {"n": 0}

    def _boom(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("forced failure after the first line was written")
        return original(*args, **kwargs)

    monkeypatch.setattr(inventory_combine_service, "_audit_combine_line", _boom)

    with pytest.raises(RuntimeError):
        _combine(client, admin_hdrs, source, target, 15.0)

    # The first line really was written before the failure — otherwise this test
    # would pass against a combine that never got started.
    assert calls["n"] == 2

    # NOTE: the test does NOT roll back the session itself. Line one's writes were
    # FLUSHED before the failure, so they are visible to any SELECT inside the same
    # open transaction — reading them back unrolled is what proves the endpoint's
    # ``atomic_transaction`` really rolled back, rather than the test tidying up
    # after it.
    assert _on_hand(db_session, source.id) == source_before
    assert _on_hand(db_session, target.id) == target_before

    db_session.expire_all()
    assert db_session.query(InventoryItem).filter(InventoryItem.id == row_a.id).first().quantity_on_hand == 10.0
    assert db_session.query(InventoryItem).filter(InventoryItem.id == row_b.id).first().quantity_on_hand == 10.0
    assert db_session.query(InventoryItem).filter(InventoryItem.id == target_row.id).first().quantity_on_hand == 4.0

    assert (
        db_session.query(InventoryTransaction)
        .filter(InventoryTransaction.reference_type == COMBINE_REFERENCE_TYPE)
        .count()
        == 0
    )
    assert db_session.query(InventoryCombine).count() == 0
    assert _counts(db_session) == before


# --------------------------------------------------------------------------- #
# The preview is a pure read
# --------------------------------------------------------------------------- #


def test_combine_preview_writes_nothing(db_session: Session, client: TestClient, admin_hdrs, recut_pair):
    """Spec D11: the preview cannot write, structurally.

    ``build_combine_preview`` takes no ``AuditService`` and no actor id, so it
    could not write an audit row, a ledger row or an event by accident. A poll is
    not an actor and records no reason.
    """
    source, target = recut_pair
    before = _counts(db_session)

    response = _preview(client, admin_hdrs, source, target)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["eligible"] is True
    assert body["default_quantity"] == 92.0
    assert body["source"]["total_on_hand"] == 92.0
    assert body["target"]["total_on_hand"] == 141.0

    assert _counts(db_session) == before


def test_combine_preview_reports_cost_delta_without_reblending(db_session: Session, client: TestClient, admin_hdrs):
    """The two sides' carried costs are DISCLOSED, never averaged together."""
    source = _part(db_session, number=f"COST-SRC-{_next():04d}", name="Plate stock")
    target = _part(db_session, number=f"COST-TGT-{_next():04d}", name="Plate stock")
    _stock(db_session, source, location="A-1", qty=10.0, lot="L1", unit_cost=4.0)
    _stock(db_session, target, location="B-1", qty=10.0, lot="L2", unit_cost=9.0)

    pv = _preview(client, admin_hdrs, source, target).json()
    assert pv["cost"]["source_weighted_unit_cost"] == pytest.approx(4.0)
    assert pv["cost"]["target_weighted_unit_cost"] == pytest.approx(9.0)
    assert pv["cost"]["differs"] is True
    assert "never reblended" in pv["cost"]["note"]


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #


def test_combine_refuses_same_part(db_session: Session, client: TestClient, admin_hdrs):
    part = _part(db_session, number=f"SAME-{_next():04d}", name="Plate stock")
    _stock(db_session, part, location="A-1", qty=5.0, lot="L1")

    pv = _preview(client, admin_hdrs, part, part).json()
    assert "same_part" in _codes(pv["blockers"])

    response = _combine(client, admin_hdrs, part, part, 1.0)
    assert response.status_code == 400, response.text
    assert "same part" in response.json()["detail"].lower()
    assert db_session.query(InventoryCombine).count() == 0


def test_combine_refuses_unit_of_measure_mismatch(db_session: Session, client: TestClient, admin_hdrs):
    """Different stocking units add quantities that do not mean the same thing."""
    source = _part(db_session, number=f"UOM-SRC-{_next():04d}", name="Plate stock", uom="each")
    target = _part(db_session, number=f"UOM-TGT-{_next():04d}", name="Plate stock", uom="pounds")
    _stock(db_session, source, location="A-1", qty=5.0, lot="L1")
    _stock(db_session, target, location="B-1", qty=1.0, lot="L2")

    pv = _preview(client, admin_hdrs, source, target).json()
    assert pv["unit_of_measure_match"] is False
    assert "unit_of_measure_mismatch" in _codes(pv["blockers"])

    response = _combine(client, admin_hdrs, source, target, 5.0)
    assert response.status_code == 409, response.text
    assert "different units" in response.json()["detail"].lower()
    assert _on_hand(db_session, source.id) == 5.0
    assert db_session.query(InventoryCombine).count() == 0


def test_combine_blank_unit_of_measure_is_not_a_mismatch(db_session: Session, client: TestClient, admin_hdrs):
    """A part stating NO unit makes no claim to contradict — advisory, not blocker.

    The same rule ``uom_disagrees`` follows for BOM lines: a MISSING unit is not
    the same claim as a WRONG one. It still earns a disclosure, because folding an
    unit-less SKU into a unit-ed one is worth a human glance.
    """
    source = _part(db_session, number=f"BLANK-SRC-{_next():04d}", name="Plate stock", uom=None)
    target = _part(db_session, number=f"BLANK-TGT-{_next():04d}", name="Plate stock", uom="pounds")
    _stock(db_session, source, location="A-1", qty=5.0, lot="L1")
    _stock(db_session, target, location="B-1", qty=1.0, lot="L2")

    db_session.expire_all()
    assert db_session.query(Part).filter(Part.id == source.id).first().unit_of_measure is None

    pv = _preview(client, admin_hdrs, source, target).json()
    assert pv["unit_of_measure_match"] is True
    assert "unit_of_measure_mismatch" not in _codes(pv["blockers"])
    assert "unit_of_measure_unstated" in _codes(pv["advisories"])

    response = _combine(client, admin_hdrs, source, target, 5.0)
    assert response.status_code == 200, response.text
    assert _on_hand(db_session, target.id) == 6.0


@pytest.mark.parametrize("deleted_side", ["source", "target"])
def test_combine_refuses_soft_deleted_part(db_session: Session, client: TestClient, admin_hdrs, deleted_side: str):
    """A soft-deleted part is a 400 that says 'restore it', not a 404 typo hunt."""
    source = _part(
        db_session,
        number=f"DEL-SRC-{_next():04d}",
        name="Plate stock",
        is_deleted=deleted_side == "source",
    )
    target = _part(
        db_session,
        number=f"DEL-TGT-{_next():04d}",
        name="Plate stock",
        is_deleted=deleted_side == "target",
    )
    _stock(db_session, source, location="A-1", qty=5.0, lot="L1")
    _stock(db_session, target, location="B-1", qty=1.0, lot="L2")

    pv = _preview(client, admin_hdrs, source, target).json()
    assert "part_deleted" in _codes(pv["blockers"])
    # The preview SHOWS the deleted side rather than hiding it.
    assert pv[deleted_side]["is_deleted"] is True

    response = _combine(client, admin_hdrs, source, target, 5.0)
    assert response.status_code == 400, response.text
    assert "deleted" in response.json()["detail"].lower()
    assert db_session.query(InventoryCombine).count() == 0


def test_combine_expected_part_number_mismatch(db_session: Session, client: TestClient, admin_hdrs, recut_pair):
    """The compare-and-swap: somebody renumbered a part while the dialog was open.

    ``Part`` maps no optimistic-lock version column, so the number strings ARE the
    concurrency control. Without this, the combine would fold stock into a SKU the
    operator never saw.
    """
    source, target = recut_pair
    stale_number = target.part_number

    target.part_number = "SH-A240-304-0.0625-60X144-2B-R2"
    db_session.commit()

    response = _combine(client, admin_hdrs, source, target, 92.0, expected_target=stale_number)
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert "changed while you were working" in detail
    assert "SH-A240-304-0.0625-60X144-2B-R2" in detail
    assert _on_hand(db_session, source.id) == 92.0
    assert db_session.query(InventoryCombine).count() == 0

    # Case and surrounding whitespace are NOT somebody else's edit.
    ok = _combine(client, admin_hdrs, source, target, 92.0, expected_target="  sh-a240-304-0.0625-60x144-2b-r2 ")
    assert ok.status_code == 200, ok.text


# --------------------------------------------------------------------------- #
# The flagged-part acknowledgement gate
# --------------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.parametrize(
    "number,expected_token",
    [
        ("HOUSING-A", "housing"),
        ("TEST FIXTURE", "test"),
        ("TEST_FIXTURE", "test"),
        ("06463-TEST", "test"),
        # THE NEAR MISSES. A naive substring match flags both of these, and
        # ``\b`` alone would MISS ``TEST_FIXTURE`` above (``_`` is a word char).
        ("TESTA-500", None),
        ("WAREHOUSING", None),
        ("PROTEST", None),
    ],
)
def test_flagged_part_matching_is_word_boundary(db_session: Session, number: str, expected_token):
    """'housing' is a legitimate manufacturing word — the match must be exact.

    ``WAREHOUSING`` is a warehouse label and ``TESTA-500`` is a real part family;
    flagging either would train operators to click through the acknowledgement,
    which is the one failure mode that makes the whole gate worthless.
    """
    part = _part(db_session, number=number, name="Widget bracket")
    flag = inventory_combine_service._flag_for_part(part)
    if expected_token is None:
        assert flag is None
    else:
        assert flag is not None
        assert flag.matched_token == expected_token
        assert flag.field == "part_number"


@pytest.mark.unit
def test_flagged_part_matches_on_name_too(db_session: Session):
    part = _part(db_session, number=f"PN-{_next():04d}", name="Miratech housing weldment")
    flag = inventory_combine_service._flag_for_part(part)
    assert flag is not None
    assert flag.matched_token == "housing"
    assert flag.field == "name"


def test_combine_flagged_part_requires_acknowledgement(db_session: Session, client: TestClient, admin_hdrs):
    """A flagged part is an acknowledgement GATE, not a ban.

    "Housing" is an ordinary manufacturing word — the Miratech housing is a real
    production part — so a ban would refuse exactly the legitimate work this shop
    does. The request must name the part id instead, which turns a caution into a
    decision somebody made on purpose and which the audit row records.
    """
    source = _part(db_session, number=f"HOUSING-{_next():04d}", name="Blower housing")
    target = _part(db_session, number=f"HSG-TGT-{_next():04d}", name="Blower assembly shell")
    _stock(db_session, source, location="A-1", qty=8.0, lot="L1")
    _stock(db_session, target, location="B-1", qty=2.0, lot="L2")

    pv = _preview(client, admin_hdrs, source, target).json()
    assert "flagged_part_not_acknowledged" in _codes(pv["blockers"])
    assert [f["part_id"] for f in pv["flagged_parts"]] == [source.id]
    assert pv["flagged_parts"][0]["matched_token"] == "housing"

    refused = _combine(client, admin_hdrs, source, target, 8.0)
    assert refused.status_code == 409, refused.text
    assert "explicit confirmation" in refused.json()["detail"]
    assert db_session.query(InventoryCombine).count() == 0

    ok = _combine(client, admin_hdrs, source, target, 8.0, acknowledge=[source.id])
    assert ok.status_code == 200, ok.text
    assert _on_hand(db_session, target.id) == 10.0


def test_combine_near_miss_part_names_do_not_require_acknowledgement(
    db_session: Session, client: TestClient, admin_hdrs
):
    """'TESTA-500' and 'WAREHOUSING' must combine with no acknowledgement at all."""
    source = _part(db_session, number="TESTA-500", name="Bracket A")
    target = _part(db_session, number="WAREHOUSING", name="Bracket B")
    _stock(db_session, source, location="A-1", qty=4.0, lot="L1")
    _stock(db_session, target, location="B-1", qty=1.0, lot="L2")

    pv = _preview(client, admin_hdrs, source, target).json()
    assert pv["flagged_parts"] == []
    assert "flagged_part_not_acknowledged" not in _codes(pv["blockers"])
    assert pv["eligible"] is True

    ok = _combine(client, admin_hdrs, source, target, 4.0)
    assert ok.status_code == 200, ok.text
    assert _on_hand(db_session, target.id) == 5.0


# --------------------------------------------------------------------------- #
# Which rows move, in what order, carrying what
# --------------------------------------------------------------------------- #


def test_combine_splits_across_multiple_lots_and_locations(db_session: Session, client: TestClient, admin_hdrs):
    """Three source rows produce three linked pairs, drained oldest row first.

    Ascending id is one order doing two jobs — the drain order (roughly FIFO) and
    the lock-acquisition order — so a combine cannot deadlock against itself. The
    partial draw proves the ordering: the third row must be untouched.
    """
    source = _part(db_session, number=f"MULTI-SRC-{_next():04d}", name="Plate stock")
    target = _part(db_session, number=f"MULTI-TGT-{_next():04d}", name="Plate stock")
    row1 = _stock(db_session, source, location="A-1", qty=5.0, lot="LOT-1", unit_cost=2.0)
    row2 = _stock(db_session, source, location="A-2", qty=7.0, lot="LOT-2", unit_cost=3.0)
    row3 = _stock(db_session, source, location="A-3", qty=9.0, lot="LOT-3", unit_cost=4.0)
    assert row1.id < row2.id < row3.id

    # Draw 8: all of row1, 3 of row2, nothing from row3.
    response = _combine(client, admin_hdrs, source, target, 8.0)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["lines_moved"] == 2
    assert [line["source_inventory_item_id"] for line in body["lines"]] == [row1.id, row2.id]
    assert [line["quantity"] for line in body["lines"]] == [5.0, 3.0]
    assert all(line["target_row_created"] for line in body["lines"])

    db_session.expire_all()
    assert db_session.query(InventoryItem).filter(InventoryItem.id == row1.id).first().quantity_on_hand == 0.0
    assert db_session.query(InventoryItem).filter(InventoryItem.id == row2.id).first().quantity_on_hand == 4.0
    assert db_session.query(InventoryItem).filter(InventoryItem.id == row3.id).first().quantity_on_hand == 9.0

    # Lots are CARRIED onto the target rows, not collapsed into one anonymous pile.
    target_rows = {
        r.lot_number: r for r in db_session.query(InventoryItem).filter(InventoryItem.part_id == target.id).all()
    }
    assert set(target_rows) == {"LOT-1", "LOT-2"}
    assert target_rows["LOT-1"].quantity_on_hand == 5.0
    assert target_rows["LOT-1"].location == "A-1"
    assert target_rows["LOT-2"].quantity_on_hand == 3.0
    assert target_rows["LOT-2"].location == "A-2"

    txns = _combine_txns(db_session, body["combine_id"])
    assert len(txns) == 4
    assert sum(float(t.quantity) for t in txns) == pytest.approx(0.0, abs=1e-9)


def test_combine_carries_traceability_onto_new_target_row(db_session: Session, client: TestClient, admin_hdrs):
    """Invariant 5: the MTR and heat lot follow the physical material.

    A merge that dropped them would launder the material's provenance while
    looking tidy — which is the exact opposite of what AS9100D 8.5.2 lot
    traceability is for.
    """
    from app.models.purchasing import Vendor

    vendor = Vendor(name="Acme Metals", code=f"V-{_next():04d}", is_active=True, company_id=COMPANY_A)
    db_session.add(vendor)
    db_session.commit()
    db_session.refresh(vendor)

    received = datetime(2026, 8, 13, 9, 30, 0)
    expires = datetime(2027, 8, 13, 9, 30, 0)

    source = _part(db_session, number=f"TRACE-SRC-{_next():04d}", name="Plate stock")
    target = _part(db_session, number=f"TRACE-TGT-{_next():04d}", name="Plate stock")
    _stock(
        db_session,
        source,
        location="RECV-01",
        qty=6.0,
        lot="RCV-20260813-005",
        unit_cost=118.5,
        warehouse="WEST",
        serial_number="SN-0001",
        cert_number="CERT-8891",
        heat_lot="HEAT-44231",
        supplier_id=vendor.id,
        po_number="PO-10022",
        received_date=received,
        expiration_date=expires,
    )

    response = _combine(client, admin_hdrs, source, target, 6.0)
    assert response.status_code == 200, response.text
    assert response.json()["lines"][0]["target_row_created"] is True

    db_session.expire_all()
    new_row = db_session.query(InventoryItem).filter(InventoryItem.part_id == target.id).one()
    assert new_row.location == "RECV-01"
    assert new_row.warehouse == "WEST"
    assert new_row.lot_number == "RCV-20260813-005"
    assert new_row.serial_number == "SN-0001"
    assert new_row.cert_number == "CERT-8891"
    assert new_row.heat_lot == "HEAT-44231"
    assert new_row.supplier_id == vendor.id
    assert new_row.po_number == "PO-10022"
    assert new_row.received_date == received
    assert new_row.expiration_date == expires
    assert new_row.quantity_on_hand == 6.0
    assert new_row.quantity_allocated == 0.0


def test_combine_does_not_change_existing_target_unit_cost(db_session: Session, client: TestClient, admin_hdrs):
    """Both halves of the cost rule, in one combine.

    A NEWLY CREATED target row carries the source lot's ``unit_cost`` (the cost
    travels with the material). An EXISTING target row keeps its own, untouched —
    there is no weighted-average reblend anywhere, because that is a decision this
    code is not entitled to make on the operator's behalf.
    """
    source = _part(db_session, number=f"UC-SRC-{_next():04d}", name="Plate stock")
    target = _part(db_session, number=f"UC-TGT-{_next():04d}", name="Plate stock")
    _stock(db_session, source, location="A-1", qty=5.0, lot="SHARED", unit_cost=4.0)
    _stock(db_session, source, location="A-2", qty=5.0, lot="FRESH", unit_cost=4.0)
    existing = _stock(db_session, target, location="A-1", qty=2.0, lot="SHARED", unit_cost=9.0)

    response = _combine(client, admin_hdrs, source, target, 10.0)
    assert response.status_code == 200, response.text
    lines = {line["lot_number"]: line for line in response.json()["lines"]}
    assert lines["SHARED"]["target_row_created"] is False
    assert lines["FRESH"]["target_row_created"] is True

    db_session.expire_all()
    reloaded = db_session.query(InventoryItem).filter(InventoryItem.id == existing.id).first()
    assert reloaded.quantity_on_hand == 7.0
    assert reloaded.unit_cost == 9.0, "an existing target row's cost basis is never reblended"

    created = (
        db_session.query(InventoryItem)
        .filter(InventoryItem.part_id == target.id, InventoryItem.lot_number == "FRESH")
        .one()
    )
    assert created.unit_cost == 4.0, "a new target row carries the source lot's cost"

    # Both halves of every ledger pair price on the SOURCE row's cost, or the
    # net-zero identity would hold on quantity but not on value.
    txns = _combine_txns(db_session, response.json()["combine_id"])
    assert {float(t.unit_cost) for t in txns} == {4.0}
    assert sum(float(t.quantity) for t in txns) == pytest.approx(0.0, abs=1e-9)


def test_combine_excludes_non_available_stock(db_session: Session, client: TestClient, admin_hdrs):
    """Held, quarantined and deactivated stock is never folded — and never hidden.

    Silently folding it would move material somebody put a hold on; silently
    omitting it would leave the operator wondering why the numbers do not add up.
    """
    source = _part(db_session, number=f"HOLD-SRC-{_next():04d}", name="Plate stock")
    target = _part(db_session, number=f"HOLD-TGT-{_next():04d}", name="Plate stock")
    free = _stock(db_session, source, location="A-1", qty=10.0, lot="FREE")
    held = _stock(db_session, source, location="A-2", qty=5.0, lot="HELD", status="on_hold")
    quarantined = _stock(db_session, source, location="A-3", qty=3.0, lot="QUAR", status="quarantine")
    dead = _stock(db_session, source, location="A-4", qty=4.0, lot="DEAD", is_active=False)

    pv = _preview(client, admin_hdrs, source, target).json()
    assert pv["source"]["total_on_hand"] == 22.0
    assert pv["source"]["eligible_available"] == 10.0
    excluded = [a for a in pv["advisories"] if a["code"] == "non_available_stock_excluded"]
    assert len(excluded) == 3
    joined = " ".join(a["detail"] for a in excluded)
    assert "HELD" in joined and "QUAR" in joined and "DEAD" in joined

    refused = _combine(client, admin_hdrs, source, target, 11.0)
    assert refused.status_code == 409, refused.text

    ok = _combine(client, admin_hdrs, source, target, 10.0)
    assert ok.status_code == 200, ok.text

    db_session.expire_all()
    assert db_session.query(InventoryItem).filter(InventoryItem.id == free.id).first().quantity_on_hand == 0.0
    assert db_session.query(InventoryItem).filter(InventoryItem.id == held.id).first().quantity_on_hand == 5.0
    assert db_session.query(InventoryItem).filter(InventoryItem.id == quarantined.id).first().quantity_on_hand == 3.0
    assert db_session.query(InventoryItem).filter(InventoryItem.id == dead.id).first().quantity_on_hand == 4.0
    assert _on_hand(db_session, source.id) == 12.0


# --------------------------------------------------------------------------- #
# Audit, events and the ledger's reference shape
# --------------------------------------------------------------------------- #


def test_combine_writes_audit_rows(db_session: Session, client: TestClient, admin_hdrs, recut_pair):
    """Invariant 2: every state change reaches ``audit_log`` through ``AuditService``.

    The header row carries what the generic column diff cannot — which lot moved
    from where at what cost, the written reason, and what the operator was shown.
    None of that is reconstructable once later movements post against the same
    lots.
    """
    source, target = recut_pair
    response = _combine(client, admin_hdrs, source, target, 92.0)
    assert response.status_code == 200, response.text
    body = response.json()

    db_session.expire_all()
    headers = (
        db_session.query(AuditLog)
        .filter(AuditLog.resource_type == "inventory_combine", AuditLog.resource_id == body["combine_id"])
        .all()
    )
    assert len(headers) == 1
    header = headers[0]
    assert header.action == "CREATE"
    assert header.company_id == COMPANY_A
    assert header.resource_identifier == body["combine_number"]

    extra = header.extra_data
    assert extra["source_part_id"] == source.id
    assert extra["source_part_number"] == SOURCE_NUMBER
    assert extra["target_part_id"] == target.id
    assert extra["target_part_number"] == TARGET_NUMBER
    assert extra["quantity"] == 92.0
    assert extra["reason"] == REASON
    assert extra["deactivate_source"] is False
    assert extra["lines"] == [
        {"location": "RECV-01", "lot_number": "RCV-20260813-005", "quantity": 92.0, "unit_cost": 118.5}
    ]

    # One CREATE per ledger row (the movement) plus the stock-level UPDATE rows.
    ledger_ids = {str(i) for i in body["transaction_ids"]}
    ledger_audits = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.resource_type == "inventory",
            AuditLog.action == "CREATE",
            AuditLog.resource_identifier.in_(ledger_ids),
        )
        .all()
    )
    assert len(ledger_audits) == 2

    stock_updates = (
        db_session.query(AuditLog).filter(AuditLog.resource_type == "inventory", AuditLog.action == "UPDATE").all()
    )
    # The source decrement is audited. The target row was CREATED by this line, so
    # no "0 -> n" UPDATE is fabricated for a row that did not exist a moment ago.
    assert len(stock_updates) == 1
    assert stock_updates[0].old_values["quantity_on_hand"] == 92.0
    assert stock_updates[0].new_values["quantity_on_hand"] == 0.0


def test_combine_emits_operational_event(db_session: Session, client: TestClient, admin_hdrs, recut_pair):
    source, target = recut_pair
    response = _combine(client, admin_hdrs, source, target, 92.0)
    assert response.status_code == 200, response.text

    db_session.expire_all()
    events = db_session.query(OperationalEvent).filter(OperationalEvent.event_type == "inventory_combined").all()
    assert len(events) == 1
    event = events[0]
    assert event.company_id == COMPANY_A
    assert event.entity_type == "inventory_combine"
    assert event.entity_id == response.json()["combine_id"]
    assert event.event_payload["quantity"] == 92.0
    assert event.event_payload["source_part_number"] == SOURCE_NUMBER


def test_combine_reference_shape_is_outside_wo_idempotency_predicates(
    db_session: Session, client: TestClient, admin_hdrs
):
    """A combine is not work-order material and must never reach job costing.

    Two independent claims, both structural:

    1. ``(reference_type='inventory_combine', ADJUST)`` cannot satisfy either
       partial unique predicate (both key on ``reference_type = 'work_order'``
       with RECEIVE/ISSUE), so a combine can never collide with — or be mistaken
       for — a work-order backflush idempotency guard. Proven twice: by the
       predicate literals, and by combining the SAME pair twice, which a
       degenerate unconditional unique index would reject on the second insert.
    2. ``work_order_ledger_filter`` does not select the rows, so they cannot land
       in a job's cost roll-up or its as-built material record.
    """
    assert COMBINE_REFERENCE_TYPE not in WORK_ORDER_REFERENCE_TYPES
    assert "'work_order'" in WO_RECEIPT_INDEX_PREDICATE
    assert "'work_order'" in WO_ISSUE_INDEX_PREDICATE
    assert "RECEIVE" in WO_RECEIPT_INDEX_PREDICATE
    assert "ISSUE" in WO_ISSUE_INDEX_PREDICATE

    # The predicate is read off the DECLARED index on BOTH dialects rather than
    # inferred from what SQLite happened to do, per the house rule: engine-specific
    # behaviour is asserted by inspecting/compiling the declaration, never by
    # executing it and trusting the result.
    declared = {idx.name: idx for idx in InventoryTransaction.__table__.indexes}
    for name in ("uq_wo_inventory_receipt", "uq_wo_inventory_issue"):
        index = declared[name]
        assert index.unique is True
        for dialect in ("postgresql", "sqlite"):
            predicate = str(index.dialect_options[dialect]["where"])
            assert "reference_type = 'work_order'" in predicate
            assert COMBINE_REFERENCE_TYPE not in predicate

    source = _part(db_session, number=f"REF-SRC-{_next():04d}", name="Plate stock")
    target = _part(db_session, number=f"REF-TGT-{_next():04d}", name="Plate stock")
    _stock(db_session, source, location="A-1", qty=10.0, lot="L1")
    _stock(db_session, target, location="B-1", qty=0.0, lot="L2")

    first = _combine(client, admin_hdrs, source, target, 4.0)
    assert first.status_code == 200, first.text
    second = _combine(client, admin_hdrs, source, target, 6.0)
    assert second.status_code == 200, second.text

    all_rows = (
        db_session.query(InventoryTransaction)
        .filter(InventoryTransaction.reference_type == COMBINE_REFERENCE_TYPE)
        .all()
    )
    assert len(all_rows) == 4, "two combines over one pair must both post; neither predicate covers them"
    for txn in all_rows:
        assert txn.transaction_type == TransactionType.ADJUST
        assert txn.reference_type != "work_order"
        assert not (txn.reference_type == "work_order" and txn.transaction_type == TransactionType.RECEIVE)
        assert not (txn.reference_type == "work_order" and txn.transaction_type == TransactionType.ISSUE)

    # And no work order can claim them as its material.
    wc = WorkCenter(
        name=f"Laser {_next()}",
        code=f"WC-{_next():04d}",
        work_center_type="laser_cutting",
        is_active=True,
        company_id=COMPANY_A,
    )
    db_session.add(wc)
    db_session.commit()
    wo = _work_order(db_session, target)
    op = WorkOrderOperation(
        work_order_id=wo.id,
        work_center_id=wc.id,
        sequence=10,
        name="Cut",
        company_id=COMPANY_A,
    )
    db_session.add(op)
    db_session.commit()

    selected = (
        db_session.query(InventoryTransaction)
        .filter(
            InventoryTransaction.company_id == COMPANY_A,
            work_order_ledger_filter([wo.id], COMPANY_A),
        )
        .all()
    )
    combine_ids = {t.id for t in all_rows}
    assert combine_ids.isdisjoint({t.id for t in selected})


# --------------------------------------------------------------------------- #
# The sheet-spec advisory — the regression guard for the headline case
# --------------------------------------------------------------------------- #


def test_sheet_advisory_matches_on_the_owners_recut_pair(db_session: Session, client: TestClient, admin_hdrs):
    """'.0625-60X144-304SS' -> 'SH-A240-304-0.0625-60X144-2B' reads as a MATCH.

    THE REGRESSION GUARD. The new numbering scheme leads with a spec designation,
    so the anchored triple grammar cannot read a thickness or a size out of it —
    only the grade. A field stated on ONE side only is NOT a disagreement, it is a
    field the other number does not talk about. Treating "not stated" as
    "different" would cry wolf on exactly the pair this feature was built for, and
    an operator who is warned about the correct combine learns to ignore the
    warning that matters.
    """
    source, target = (
        _part(db_session, number=SOURCE_NUMBER, name="Sheet 16GA 304 stainless 60 x 144"),
        _part(db_session, number=TARGET_NUMBER, name="Sheet A240 304 2B 0.0625 60 x 144"),
    )
    _stock(db_session, source, location="RECV-01", qty=92.0, lot="RCV-20260813-005")
    _stock(db_session, target, location="RACK-12", qty=141.0, lot="LOT-1")

    pv = _preview(client, admin_hdrs, source, target).json()
    codes = _codes(pv["advisories"])
    assert "sheet_spec_match" in codes
    assert "sheet_spec_mismatch" not in codes
    detail = next(a["detail"] for a in pv["advisories"] if a["code"] == "sheet_spec_match")
    assert "grade 304" in detail
    assert pv["eligible"] is True


@pytest.mark.parametrize(
    "source_number,target_number,expected_in_detail",
    [
        # A grade change is the alarm this advisory exists for.
        (".0625-60X144-304SS", "SH-A240-316-0.0625-60X144-2B", "grade"),
        # Both numbers state a size here, so the sizes are genuinely comparable.
        (".0625-60X144-304SS", ".0625-48X120-304SS", "size"),
    ],
)
def test_sheet_advisory_flags_a_real_material_difference(
    db_session: Session,
    client: TestClient,
    admin_hdrs,
    source_number: str,
    target_number: str,
    expected_in_detail: str,
):
    """304 -> 316 and 60X144 -> 48X120 are genuine disagreements, and are reported.

    Advisory, never a blocker: part numbers are free text, so a refusal only
    pushes the operator to a spelling that defeats the check — and if the CURRENT
    string is wrong then the nest matcher is already mis-matching.
    """
    source = _part(db_session, number=source_number, name="Sheet stock 304")
    target = _part(db_session, number=target_number, name="Sheet stock")
    _stock(db_session, source, location="A-1", qty=5.0, lot="L1")
    _stock(db_session, target, location="B-1", qty=1.0, lot="L2")

    pv = _preview(client, admin_hdrs, source, target).json()
    codes = _codes(pv["advisories"])
    assert "sheet_spec_mismatch" in codes
    assert "sheet_spec_match" not in codes
    detail = next(a["detail"] for a in pv["advisories"] if a["code"] == "sheet_spec_mismatch")
    assert expected_in_detail in detail
    # It warns; it does not refuse.
    assert "sheet_spec_mismatch" not in _codes(pv["blockers"])
    assert pv["eligible"] is True


# --------------------------------------------------------------------------- #
# deactivate_source
# --------------------------------------------------------------------------- #


def test_combine_deactivate_source(db_session: Session, client: TestClient, admin_hdrs, recut_pair):
    """The drained source is retired, never deleted.

    ``is_active`` and ``status`` are written TOGETHER so the two can never
    disagree, and ``is_deleted`` is never touched: the part stays in the catalog
    at qty 0 so every historical document naming it keeps resolving.
    """
    source, target = recut_pair
    response = _combine(client, admin_hdrs, source, target, 92.0, deactivate_source=True)
    assert response.status_code == 200, response.text
    assert response.json()["source_deactivated"] is True

    db_session.expire_all()
    reloaded = db_session.query(Part).filter(Part.id == source.id).first()
    assert reloaded.is_active is False
    assert reloaded.status == "obsolete"
    assert reloaded.is_deleted is False
    assert reloaded.deleted_at is None

    part_audits = (
        db_session.query(AuditLog).filter(AuditLog.resource_type == "part", AuditLog.resource_id == source.id).all()
    )
    assert len(part_audits) == 1
    assert part_audits[0].new_values["is_active"] is False
    assert part_audits[0].new_values["status"] == "obsolete"


def test_combine_deactivate_source_refuses_when_stock_remains(db_session: Session, client: TestClient, admin_hdrs):
    """ "Lands at exactly 0" counts the rows this combine CANNOT move, too.

    Deactivating a part that still has material on a held or quarantined row would
    hide that material from every list in the app while it is still physically on
    the shelf.
    """
    source = _part(db_session, number=f"DEACT-SRC-{_next():04d}", name="Plate stock")
    target = _part(db_session, number=f"DEACT-TGT-{_next():04d}", name="Plate stock")
    _stock(db_session, source, location="A-1", qty=10.0, lot="FREE")
    _stock(db_session, source, location="A-2", qty=2.0, lot="HELD", status="on_hold")

    refused = _combine(client, admin_hdrs, source, target, 10.0, deactivate_source=True)
    assert refused.status_code == 409, refused.text
    assert "cannot be deactivated" in refused.json()["detail"]

    db_session.expire_all()
    assert db_session.query(Part).filter(Part.id == source.id).first().is_active is True
    assert _on_hand(db_session, source.id) == 12.0
    assert db_session.query(InventoryCombine).count() == 0


# --------------------------------------------------------------------------- #
# Tenancy and roles
# --------------------------------------------------------------------------- #


def test_combine_is_tenant_scoped(db_session: Session, client: TestClient, admin_hdrs):
    """Invariant 1: another company's part is a 404, and its stock is unreachable.

    The "poisoned" row below is the point: a company-B stock row carrying company
    A's ``part_id``. Nothing creates one naturally, but the pre-scoping shape of
    these lookups keyed only on ``part_id`` — so constructing it is what proves the
    ``company_id`` predicate, and nothing else, is what excludes it.
    """
    source = _part(db_session, number=f"TEN-SRC-{_next():04d}", name="Plate stock")
    target = _part(db_session, number=f"TEN-TGT-{_next():04d}", name="Plate stock")
    _stock(db_session, source, location="A-1", qty=10.0, lot="L1")
    _stock(db_session, target, location="B-1", qty=0.0, lot="L2")

    foreign_part = _part(db_session, number=f"TEN-B-{_next():04d}", name="Plate stock", company_id=COMPANY_B)
    poisoned = _stock(db_session, source, location="B-RACK", qty=50.0, lot="B-LOT", company_id=COMPANY_B)

    # A's preview never sees B's rows, even the one naming A's part.
    pv = _preview(client, admin_hdrs, source, target).json()
    assert pv["source"]["total_on_hand"] == 10.0
    assert poisoned.id not in [line["inventory_item_id"] for line in pv["source"]["lines"]]
    assert pv["default_quantity"] == 10.0

    # Another tenant's part id is a 404 on BOTH verbs, in both positions.
    assert _preview(client, admin_hdrs, foreign_part, target).status_code == 404
    assert _preview(client, admin_hdrs, source, foreign_part).status_code == 404
    assert _combine(client, admin_hdrs, foreign_part, target, 1.0).status_code == 404
    assert _combine(client, admin_hdrs, source, foreign_part, 1.0).status_code == 404

    # And a company-B admin cannot reach company A's parts either.
    b_headers = _headers(_user(db_session, role=UserRole.ADMIN, company_id=COMPANY_B))
    assert _preview(client, b_headers, source, target).status_code == 404
    assert _combine(client, b_headers, source, target, 1.0).status_code == 404

    ok = _combine(client, admin_hdrs, source, target, 10.0)
    assert ok.status_code == 200, ok.text

    db_session.expire_all()
    assert db_session.query(InventoryItem).filter(InventoryItem.id == poisoned.id).first().quantity_on_hand == 50.0
    assert db_session.query(InventoryCombine).count() == 1
    assert db_session.query(InventoryCombine).one().company_id == COMPANY_A


@pytest.mark.parametrize("role", [UserRole.SUPERVISOR, UserRole.OPERATOR, UserRole.VIEWER, UserRole.QUALITY])
def test_combine_write_is_refused_below_manager(db_session: Session, client: TestClient, role: UserRole):
    """Folding two SKUs is an identity change: ADMIN/MANAGER only.

    Deliberately NARROWER than ``inventory:adjust``, which reaches Supervisor.
    Adjusting one lot corrects a count; folding two SKUs together is a controlled
    change to article identity under AS9100D 8.5.2.
    """
    source = _part(db_session, number=f"ROLE-SRC-{_next():04d}", name="Plate stock")
    target = _part(db_session, number=f"ROLE-TGT-{_next():04d}", name="Plate stock")
    _stock(db_session, source, location="A-1", qty=5.0, lot="L1")
    _stock(db_session, target, location="B-1", qty=1.0, lot="L2")

    headers = _headers(_user(db_session, role=role))
    response = _combine(client, headers, source, target, 5.0)
    assert response.status_code == 403, response.text
    assert _on_hand(db_session, source.id) == 5.0
    assert db_session.query(InventoryCombine).count() == 0


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.MANAGER])
def test_combine_write_allowed_for_admin_and_manager(db_session: Session, client: TestClient, role: UserRole):
    source = _part(db_session, number=f"ROLEOK-SRC-{_next():04d}", name="Plate stock")
    target = _part(db_session, number=f"ROLEOK-TGT-{_next():04d}", name="Plate stock")
    _stock(db_session, source, location="A-1", qty=5.0, lot="L1")
    _stock(db_session, target, location="B-1", qty=1.0, lot="L2")

    headers = _headers(_user(db_session, role=role))
    response = _combine(client, headers, source, target, 5.0)
    assert response.status_code == 200, response.text
    assert _on_hand(db_session, target.id) == 6.0


def test_combine_preview_reachable_by_supervisor(db_session: Session, client: TestClient):
    """A supervisor investigating "why is this sheet on two numbers?" may look.

    Only folding them together is restricted — the preview writes nothing.
    """
    source = _part(db_session, number=f"SUPV-SRC-{_next():04d}", name="Plate stock")
    target = _part(db_session, number=f"SUPV-TGT-{_next():04d}", name="Plate stock")
    _stock(db_session, source, location="A-1", qty=5.0, lot="L1")
    _stock(db_session, target, location="B-1", qty=1.0, lot="L2")

    headers = _headers(_user(db_session, role=UserRole.SUPERVISOR))
    before = _counts(db_session)
    response = _preview(client, headers, source, target)
    assert response.status_code == 200, response.text
    assert response.json()["default_quantity"] == 5.0
    assert _counts(db_session) == before


@pytest.mark.parametrize("role", [UserRole.OPERATOR, UserRole.VIEWER])
def test_combine_preview_refused_below_supervisor(db_session: Session, client: TestClient, role: UserRole):
    source = _part(db_session, number=f"PVROLE-SRC-{_next():04d}", name="Plate stock")
    target = _part(db_session, number=f"PVROLE-TGT-{_next():04d}", name="Plate stock")
    _stock(db_session, source, location="A-1", qty=5.0, lot="L1")

    headers = _headers(_user(db_session, role=role))
    assert _preview(client, headers, source, target).status_code == 403


# =========================================================================== #
# THE REVIEW FINDINGS (B1-B9). One section per defect.
#
# Every test below reproduces a bug that SHIPPED in the first cut of this verb
# and was found by review rather than by a test — so each one states what the
# bug WAS, not merely what the code now does. Several of them passed against the
# broken code with different numbers, which is the whole reason the failure mode
# is written out: a future "simplification" that reintroduces the bug has to get
# past a sentence describing exactly what it costs.
# =========================================================================== #


# --------------------------------------------------------------------------- #
# B1 — the stale identity map
# --------------------------------------------------------------------------- #


def test_combine_lands_on_the_target_row_as_locked_not_a_cached_copy(
    db_session: Session, client: TestClient, admin_hdrs
):
    """A movement that lands on the target between the read and the lock is NOT lost.

    THE BUG (a blocker, and the one that silently destroyed stock): the write used
    to compute ``target_quantity_before`` from
    ``_picture_from_rows(target, _stock_rows(...))`` — an UNLOCKED read of every
    target row, a dozen statements before the drain loop. That read seeded the
    Session identity map. SQLAlchemy's default behaviour when a later
    ``SELECT ... FOR UPDATE`` hits a row the identity map already holds is to return
    the ALREADY-PRESENT instance and DISCARD the freshly-read column values — so the
    lock was taken, the fresh row was fetched, and the caller was handed the stale
    copy anyway.

    Measured: the DB row held 150, the locked SELECT handed back the cached 100, and
    the read-modify-write landed 110 instead of 160. Fifty received units gone, with
    a ledger that still nets to exactly zero over an understated on-hand — which is
    why acceptance test A2 could not have caught it.

    THE SIMULATION. The endpoint runs on this very Session (see ``conftest``'s
    ``override_get_db``), so seeding the identity map here is the same thing the
    unlocked read did. The raw ``UPDATE`` is the concurrent ``/receive``: Core DML
    does not expire ORM instances, so the cached copy genuinely still reads 100
    while the database reads 150 — asserted below, because a simulation that failed
    to produce the stale copy would make this test pass vacuously.

    TWO fixes hold this shut and both are load-bearing: the unlocked read is gone
    (``target_quantity_before`` is now a ``func.sum`` aggregate, which returns no ORM
    instances), and ``_stock_row_query`` calls ``.populate_existing()`` on the
    ``for_update`` path as the structural guard. Verified distinguishing: with the
    guard stripped, this same scenario lands 192 instead of 242.
    """
    source = _part(db_session, number=f"STALE-SRC-{_next():04d}", name="Plate stock")
    target = _part(db_session, number=f"STALE-TGT-{_next():04d}", name="Plate stock")
    _stock(db_session, source, location="RECV-01", qty=92.0, lot="LOTX", unit_cost=5.0)
    target_row = _stock(db_session, target, location="RECV-01", qty=100.0, lot="LOTX", unit_cost=5.0)

    # Seed the identity map exactly as the deleted unlocked read did.
    cached = db_session.query(InventoryItem).filter(InventoryItem.id == target_row.id).one()
    assert cached.quantity_on_hand == 100.0

    # The concurrent receipt: +50 onto that row, committed by somebody else.
    db_session.execute(
        text("UPDATE inventory_items SET quantity_on_hand = 150, quantity_available = 150 WHERE id = :item_id"),
        {"item_id": target_row.id},
    )
    assert cached.quantity_on_hand == 100.0, "the cached instance must still be stale, or this proves nothing"

    response = _combine(client, admin_hdrs, source, target, 92.0)
    assert response.status_code == 200, response.text
    body = response.json()

    # 150 + 92, not 100 + 92. The received units are still there.
    assert body["target_quantity_before"] == 150.0
    assert body["target_quantity_after"] == 242.0
    # The header cannot contradict itself either: before + moved == after.
    assert body["target_quantity_before"] + body["quantity_moved"] == body["target_quantity_after"]

    db_session.expire_all()
    reloaded = db_session.query(InventoryItem).filter(InventoryItem.id == target_row.id).one()
    assert reloaded.quantity_on_hand == 242.0, "the concurrently received 50 were folded away by a stale read"
    assert _on_hand(db_session, target.id) == 242.0


def test_combine_target_quantity_before_is_an_aggregate_not_an_orm_read(
    db_session: Session, client: TestClient, admin_hdrs
):
    """The structural half of B1: nothing re-seeds the identity map before the locks.

    ``part_total_on_hand`` is a ``func.sum`` — it returns a scalar and therefore
    cannot put a single ``InventoryItem`` instance into the Session. That is the
    property, not the implementation detail: reinstating any eager ORM read of the
    target's rows before ``_lock_target_landing_rows`` reopens the exact hole above,
    and it would do so silently.

    Asserted behaviourally rather than by grepping source: run the write against a
    target with several rows and confirm the Session holds NO target stock instance
    beyond the single landing row the lock legitimately loaded.
    """
    source = _part(db_session, number=f"AGG-SRC-{_next():04d}", name="Plate stock")
    target = _part(db_session, number=f"AGG-TGT-{_next():04d}", name="Plate stock")
    _stock(db_session, source, location="A-1", qty=5.0, lot="L1", unit_cost=2.0)
    far_row_1 = _stock(db_session, target, location="Z-9", qty=11.0, lot="OTHER-1", unit_cost=2.0)
    far_row_2 = _stock(db_session, target, location="Z-8", qty=13.0, lot="OTHER-2", unit_cost=2.0)

    # Detach the two far rows so anything found afterwards was loaded by the verb
    # itself. (Only the stock rows — expunging the Parts would detach the objects
    # the request body is built from.)
    far_ids = (far_row_1.id, far_row_2.id)
    db_session.expunge(far_row_1)
    db_session.expunge(far_row_2)

    response = _combine(client, admin_hdrs, source, target, 5.0)
    assert response.status_code == 200, response.text
    # The aggregate still has to be RIGHT — it counts every row, held ones included.
    assert response.json()["target_quantity_before"] == 24.0

    loaded_ids = {instance.id for instance in db_session.identity_map.values() if isinstance(instance, InventoryItem)}
    for far_id in far_ids:
        assert far_id not in loaded_ids, "an eager ORM read of the target's rows is back — see B1"


# --------------------------------------------------------------------------- #
# B2 — folding usable stock onto an unusable target row
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "row_kwargs,expected_words",
    [
        ({"status": "on_hold"}, "on hold"),
        ({"status": "quarantine"}, "quarantine"),
        ({"is_active": False}, "deactivated"),
    ],
    ids=["on_hold", "quarantine", "deactivated_row"],
)
def test_combine_refuses_an_unavailable_target_row(
    db_session: Session, client: TestClient, admin_hdrs, row_kwargs: dict, expected_words: str
):
    """Usable stock is never folded onto a row that would make it unusable.

    THE BUG (a blocker, and the one that made this verb actively dangerous):
    ``_find_stock_row`` resolves a landing row by ``(company, part, location, lot)``
    and NOTHING else — no ``is_active``, no ``status``. Measured end-to-end: source
    92 available at RECV-01/LOTX, target already holding a row at RECV-01/LOTX with
    ``status='on_hold'``. The preview returned ``blockers: []``, the write returned
    **200**, and the held row went to 102 — still ``on_hold``. Ninety-two usable
    sheets became zero usable sheets, behind a green success toast, with nothing in
    ``blockers`` and nothing in ``advisories``. The totals still added up, so no
    other screen in the app would ever have said what happened.

    Eligibility is decided by ``_unavailable_reason`` — i.e. ``is_consumable_item``,
    the same predicate the source side and the material-consumption engine use — so
    the two halves cannot drift into disagreeing about what "usable" means.

    The refusal is raised in ``_combine_blockers``, so the PREVIEW discloses it
    before the operator ever clicks: refusals come before mutations, and the preview
    exists to disclose.
    """
    source = _part(db_session, number=f"TGTBAD-SRC-{_next():04d}", name="Plate stock")
    target = _part(db_session, number=f"TGTBAD-TGT-{_next():04d}", name="Plate stock")
    _stock(db_session, source, location="RECV-01", qty=92.0, lot="LOTX", unit_cost=5.0)
    held = _stock(db_session, target, location="RECV-01", qty=10.0, lot="LOTX", unit_cost=5.0, **row_kwargs)

    pv = _preview(client, admin_hdrs, source, target).json()
    assert "target_row_not_available" in _codes(pv["blockers"])
    assert pv["eligible"] is False
    detail = next(b["detail"] for b in pv["blockers"] if b["code"] == "target_row_not_available")
    # It NAMES the row, so the operator knows which hold to release.
    assert "RECV-01" in detail and "LOTX" in detail
    assert expected_words in detail

    before = _counts(db_session)
    refused = _combine(client, admin_hdrs, source, target, 92.0)
    assert refused.status_code == 409, refused.text
    assert "RECV-01" in refused.json()["detail"]

    # Refused BEFORE the first mutation: every row byte-identical, no header row.
    db_session.expire_all()
    assert db_session.query(InventoryItem).filter(InventoryItem.id == held.id).first().quantity_on_hand == 10.0
    assert _on_hand(db_session, source.id) == 92.0
    assert _counts(db_session) == before

    # …and releasing the row is all it takes. The happy path is unchanged.
    db_session.query(InventoryItem).filter(InventoryItem.id == held.id).update(
        {"status": "available", "is_active": True}
    )
    db_session.commit()
    ok = _combine(client, admin_hdrs, source, target, 92.0)
    assert ok.status_code == 200, ok.text
    assert _on_hand(db_session, target.id) == 102.0


def test_combine_ignores_an_unavailable_target_row_the_fold_never_touches(
    db_session: Session, client: TestClient, admin_hdrs
):
    """A held target row somewhere else is not a reason to refuse anything.

    The probe takes the PLAN, not the whole target picture, precisely so it refuses
    only what it must. A target part with a quarantined pallet in another aisle is
    an ordinary state; refusing every combine into that part would make the check a
    nuisance, and a nuisance check is one people learn to route around.
    """
    source = _part(db_session, number=f"TGTFAR-SRC-{_next():04d}", name="Plate stock")
    target = _part(db_session, number=f"TGTFAR-TGT-{_next():04d}", name="Plate stock")
    _stock(db_session, source, location="A-1", qty=6.0, lot="L1", unit_cost=5.0)
    elsewhere = _stock(db_session, target, location="QUAR-01", qty=3.0, lot="OTHER", status="quarantine")

    pv = _preview(client, admin_hdrs, source, target).json()
    assert "target_row_not_available" not in _codes(pv["blockers"])
    assert pv["eligible"] is True

    ok = _combine(client, admin_hdrs, source, target, 6.0)
    assert ok.status_code == 200, ok.text
    assert ok.json()["lines"][0]["target_row_created"] is True

    db_session.expire_all()
    # The quarantined pallet is exactly where it was, and still quarantined.
    row = db_session.query(InventoryItem).filter(InventoryItem.id == elsewhere.id).first()
    assert row.quantity_on_hand == 3.0
    assert row.status == "quarantine"


# --------------------------------------------------------------------------- #
# B3 — the phantom combine record
# --------------------------------------------------------------------------- #


def test_combine_refuses_a_sub_epsilon_quantity_and_writes_absolutely_nothing(
    db_session: Session, client: TestClient, admin_hdrs, recut_pair
):
    """``quantity: 1e-10`` is a 422, and the database is untouched.

    THE BUG: the schema bound was ``Field(gt=0)`` while the drain loop breaks on
    ``remaining <= LEDGER_QUANTITY_EPSILON`` (1e-9). A request for 1e-10 therefore
    passed validation, moved NOTHING (the loop's first ``break`` fired immediately)
    and returned **200** with ``quantity_moved: 0.0, lines_moved: 0`` — while still
    writing an ``inventory_combines`` header, an operational event and an audit row,
    with ZERO ledger rows and zero stock change. That is an immutable, un-deletable
    record asserting a combine that did not happen, on a table whose own docstring
    says "a combine happened or it did not" and which carries no soft-delete and no
    status to walk it back with. The short-draw warning did not fire either, because
    1e-10 < 1e-9.

    With ``deactivate_source: true`` it would additionally have RETIRED the source
    part off a request that moved nothing.

    The fix makes the schema bound and the ledger epsilon the same number
    (``MINIMUM_COMBINE_QUANTITY = LEDGER_QUANTITY_EPSILON``), which is why this is a
    **422** and not a 409: the field refuses it before the handler runs. Do not
    relax it back to ``gt=0``.
    """
    from app.db.ledger_filter import LEDGER_QUANTITY_EPSILON as LEDGER_EPS
    from app.schemas.inventory_combine import MINIMUM_COMBINE_QUANTITY

    # The two bounds are the SAME number by construction, not by coincidence.
    assert MINIMUM_COMBINE_QUANTITY == LEDGER_EPS

    source, target = recut_pair
    before = _counts(db_session)

    for tiny in (1e-10, 1e-12, LEDGER_EPS):
        refused = _combine(client, admin_hdrs, source, target, tiny)
        assert refused.status_code == 422, f"{tiny} -> {refused.status_code}: {refused.text}"
        # And with deactivate_source, which is what made this more than a tidiness bug.
        refused = _combine(client, admin_hdrs, source, target, tiny, deactivate_source=True)
        assert refused.status_code == 422, refused.text

    # The preview refuses it too — same bound, so the dialog cannot walk into it.
    tiny_preview = _preview(client, admin_hdrs, source, target, quantity=1e-10)
    assert tiny_preview.status_code == 422, tiny_preview.text

    # NOTHING was written: no audit row, no ledger row, no stock row, no header,
    # no operational event.
    assert _counts(db_session) == before
    assert db_session.query(InventoryCombine).count() == 0
    assert _on_hand(db_session, source.id) == 92.0
    assert _on_hand(db_session, target.id) == 141.0

    # Just above the bound still works, so the refusal is a floor and not a ban.
    ok = _combine(client, admin_hdrs, source, target, 1.0)
    assert ok.status_code == 200, ok.text


def test_service_backstops_a_sub_epsilon_quantity_with_quantity_below_minimum(db_session: Session):
    """The service carries the same refusal for callers that skip the schema.

    The 422 above is the real gate, and it is the one an HTTP caller meets. This is
    the backstop for anything reaching ``combine_inventory`` /
    ``build_combine_preview`` directly — a job, a script, a future service — where
    there is no Pydantic field to bounce it. One code, ``quantity_below_minimum``,
    chosen over reusing ``quantity_exceeds_available`` because "too small to move"
    and "more than you have" send an operator in opposite directions.
    """
    source = _part(db_session, number=f"TINY-SRC-{_next():04d}", name="Plate stock")
    target = _part(db_session, number=f"TINY-TGT-{_next():04d}", name="Plate stock")
    _stock(db_session, source, location="A-1", qty=10.0, lot="L1")
    _stock(db_session, target, location="B-1", qty=1.0, lot="L2")

    preview = inventory_combine_service.build_combine_preview(
        db_session, COMPANY_A, source.id, target.id, quantity=1e-10
    )
    codes = [b.code for b in preview.blockers]
    assert "quantity_below_minimum" in codes
    assert preview.eligible is False
    # 400, like ``same_part``: the request names a quantity that cannot be moved at
    # all, rather than a state that happens to refuse it.
    assert inventory_combine_service._BLOCKER_STATUS["quantity_below_minimum"] == 400

    # And it does NOT fire for an ordinary quantity, so it cannot become a
    # background refusal nobody can explain.
    healthy = inventory_combine_service.build_combine_preview(db_session, COMPANY_A, source.id, target.id, quantity=5.0)
    assert "quantity_below_minimum" not in [b.code for b in healthy.blockers]


# --------------------------------------------------------------------------- #
# B4 — the reservation check counted stock nothing can draw
# --------------------------------------------------------------------------- #


def test_reservation_check_counts_only_stock_the_engine_can_draw(db_session: Session, client: TestClient, admin_hdrs):
    """92 free + 92 on hold does NOT satisfy a 92-unit open tie.

    THE BUG: the check was ``source_picture.total_on_hand - quantity >= reserved``
    and ``_max_combinable`` was ``total_on_hand - reserved``. ``total_on_hand`` sums
    EVERY row — held, quarantined, rejected, deactivated — none of which the
    consumption engine can draw and none of which this verb can move. So a source
    with 92 available and 92 on hold "satisfied" a 92-unit tie (``184 - 92 >= 92``)
    and was left with **zero drawable**. The combine turned a satisfiable job into
    an unfillable one, and ``_max_combinable`` inherited the same wrong basis, so
    the dialog ACTIVELY OFFERED the unsafe number as "Use 92".

    Both sides are ``eligible_available``-based now. Refusing too much costs one
    visible conversation; refusing too little strands material silently — the same
    asymmetry invariant 3 uses for the restrictive vendor restore.
    """
    source = _part(db_session, number=f"ELIG-SRC-{_next():04d}", name="Sheet stock")
    target = _part(db_session, number=f"ELIG-TGT-{_next():04d}", name="Sheet stock")
    _stock(db_session, source, location="A-1", qty=92.0, lot="FREE", unit_cost=5.0)
    _stock(db_session, source, location="A-2", qty=92.0, lot="HELD", status="on_hold", unit_cost=5.0)
    _stock(db_session, target, location="B-1", qty=1.0, lot="L2", unit_cost=5.0)

    wo = _work_order(db_session, source)
    _tie(db_session, wo, source, qty_planned=92.0)

    pv = _preview(client, admin_hdrs, source, target).json()
    # The two bases genuinely disagree here — that is the point of the fixture.
    assert pv["source"]["total_on_hand"] == 184.0
    assert pv["source"]["eligible_available"] == 92.0
    assert pv["reserved_quantity"] == 92.0

    assert "open_work_order_reservation" in _codes(pv["blockers"])
    # The old arithmetic offered 92 ("184 - 92"). The right answer is that NOTHING
    # can move without stranding the job.
    assert pv["max_combinable_quantity"] == 0.0

    refused = _combine(client, admin_hdrs, source, target, 92.0)
    assert refused.status_code == 409, refused.text
    detail = refused.json()["detail"]
    assert wo.work_order_number in detail
    # The sentence quotes the number the rule used, so the operator is not left
    # comparing "92" against a visible on-hand of 184.
    assert "available" in detail

    db_session.expire_all()
    assert _on_hand(db_session, source.id) == 184.0
    assert db_session.query(InventoryCombine).count() == 0


def test_reservation_check_does_not_over_refuse_when_nothing_is_tied(
    db_session: Session, client: TestClient, admin_hdrs
):
    """Held stock alone never triggers the reservation refusal.

    The eligible-based rule is STRICTER than the old one, so it is worth pinning
    that it did not become a blanket "any held stock refuses the combine". With no
    open tie, ``reserved`` is 0 and the branch cannot fire at all.
    """
    source = _part(db_session, number=f"NOTIE-SRC-{_next():04d}", name="Sheet stock")
    target = _part(db_session, number=f"NOTIE-TGT-{_next():04d}", name="Sheet stock")
    _stock(db_session, source, location="A-1", qty=92.0, lot="FREE")
    _stock(db_session, source, location="A-2", qty=92.0, lot="HELD", status="on_hold")
    _stock(db_session, target, location="B-1", qty=0.0, lot="L2")

    pv = _preview(client, admin_hdrs, source, target).json()
    assert pv["reserved_quantity"] == 0.0
    assert "open_work_order_reservation" not in _codes(pv["blockers"])
    assert pv["max_combinable_quantity"] == 92.0

    ok = _combine(client, admin_hdrs, source, target, 92.0)
    assert ok.status_code == 200, ok.text
    assert _on_hand(db_session, target.id) == 92.0


# --------------------------------------------------------------------------- #
# B5 — a lot-directed (pinned) tie
# --------------------------------------------------------------------------- #


def test_combine_withholds_a_pinned_lot_from_the_drain(db_session: Session, client: TestClient, admin_hdrs):
    """A tie pinned to one lot protects THAT lot, not just the part-level total.

    THE BUG: ``open_source_reservations`` aggregated demand per work order and never
    read ``pinned_inventory_item_id``, while the drain is ascending-id — oldest lot
    first. Measured failure: item #10 (lot A, 60 on hand) carries an OPEN tie pinned
    to #10 for 50; item #20 (lot B, 40 on hand). The part-level rule saw
    ``100 - 50 = 50 >= 50`` and allowed it, and the drain then took all 50 off lot A
    because lot A is older — leaving lot A with 10. At completion the consumption
    engine locks the PINNED row, finds 10, and records a shortage of 40, while 50
    sheets of lot A sit on the shelf under the target's number.

    A pin binds per ROW, exactly like ``quantity_allocated``, so it is withheld at
    the row: ``quantity_combinable = available - pinned``. The chosen fix is
    per-row withholding rather than an outright refusal, because refusing would
    block the legitimate case this fixture shows — 50 units CAN safely move, they
    just have to come off the unpinned lot.

    Pinned demand is subtracted out of the part-level rule as well
    (``SourceReservations.unpinned_total``), or the same reservation would be
    charged twice and the cap would collapse to zero.
    """
    source = _part(db_session, number=f"PIN-SRC-{_next():04d}", name="Sheet stock")
    target = _part(db_session, number=f"PIN-TGT-{_next():04d}", name="Sheet stock")
    lot_a = _stock(db_session, source, location="A-1", qty=60.0, lot="LOT-A", unit_cost=5.0)
    lot_b = _stock(db_session, source, location="A-2", qty=40.0, lot="LOT-B", unit_cost=5.0)
    assert lot_a.id < lot_b.id, "the drain is ascending-id; the pinned lot must be the OLDER one"
    _stock(db_session, target, location="B-1", qty=0.0, lot="L9", unit_cost=5.0)

    wo = _work_order(db_session, source)
    _tie(db_session, wo, source, qty_planned=50.0, pinned_item=lot_a)

    pv = _preview(client, admin_hdrs, source, target).json()

    # The part-level picture: 100 available, 50 of it pinned to one lot.
    assert pv["source"]["total_available"] == 100.0
    assert pv["source"]["total_pinned"] == 50.0
    assert pv["source"]["eligible_available"] == 50.0
    assert pv["max_combinable_quantity"] == 50.0

    # Per row, on the wire, so the dialog can show WHERE the cap comes from.
    by_item = {ln["inventory_item_id"]: ln for ln in pv["source"]["lines"]}
    assert by_item[lot_a.id]["quantity_available"] == 60.0
    assert by_item[lot_a.id]["quantity_pinned"] == 50.0
    assert by_item[lot_a.id]["quantity_combinable"] == 10.0
    assert by_item[lot_b.id]["quantity_pinned"] == 0.0
    assert by_item[lot_b.id]["quantity_combinable"] == 40.0

    # …and it is DISCLOSED, naming the job. Without this the cap simply looks like
    # a bug: a row showing 60 on hand and 0 allocated that refuses to move 60.
    pinned_advisories = [a for a in pv["advisories"] if a["code"] == "pinned_lot_reserved"]
    assert len(pinned_advisories) == 1
    assert wo.work_order_number in pinned_advisories[0]["detail"]
    assert "LOT-A" in pinned_advisories[0]["detail"]

    ok = _combine(client, admin_hdrs, source, target, 50.0)
    assert ok.status_code == 200, ok.text
    lines = {line["lot_number"]: line["quantity"] for line in ok.json()["lines"]}
    # 10 off the pinned lot (all that is free on it) and 40 off the other — NOT 50
    # off the pinned lot, which is what the ascending-id drain did before.
    assert lines == {"LOT-A": 10.0, "LOT-B": 40.0}

    db_session.expire_all()
    assert db_session.query(InventoryItem).filter(InventoryItem.id == lot_a.id).first().quantity_on_hand == 50.0
    assert db_session.query(InventoryItem).filter(InventoryItem.id == lot_b.id).first().quantity_on_hand == 0.0
    # The pin is still satisfiable: the job needs 50 off lot A and lot A holds 50.
    assert _on_hand(db_session, source.id) == 50.0


def test_combine_refuses_to_move_more_than_the_unpinned_remainder(db_session: Session, client: TestClient, admin_hdrs):
    """One unit past the pinned cap is refused, and the pinned lot is untouched."""
    source = _part(db_session, number=f"PIN2-SRC-{_next():04d}", name="Sheet stock")
    target = _part(db_session, number=f"PIN2-TGT-{_next():04d}", name="Sheet stock")
    lot_a = _stock(db_session, source, location="A-1", qty=60.0, lot="LOT-A")
    _stock(db_session, source, location="A-2", qty=40.0, lot="LOT-B")
    _stock(db_session, target, location="B-1", qty=0.0, lot="L9")

    wo = _work_order(db_session, source)
    _tie(db_session, wo, source, qty_planned=50.0, pinned_item=lot_a)

    refused = _combine(client, admin_hdrs, source, target, 51.0)
    assert refused.status_code == 409, refused.text
    assert "available" in refused.json()["detail"].lower()

    db_session.expire_all()
    assert db_session.query(InventoryItem).filter(InventoryItem.id == lot_a.id).first().quantity_on_hand == 60.0
    assert db_session.query(InventoryCombine).count() == 0


def test_a_pin_larger_than_its_lot_never_withholds_more_than_exists(
    db_session: Session, client: TestClient, admin_hdrs
):
    """An over-pinned lot is already a shortage; it must not go on to poison the rest.

    A pin of 90 against a 60-unit lot is a state the shop can genuinely reach (the
    job was tied, then stock was consumed elsewhere). Withholding the full 90 would
    push that row's ``combinable`` negative, and because the part-level rule
    subtracts what was actually withheld, a negative would silently understate every
    OTHER row's cap too. Both the per-row withholding and the subtraction are
    floored at zero for that reason.
    """
    source = _part(db_session, number=f"PIN3-SRC-{_next():04d}", name="Sheet stock")
    target = _part(db_session, number=f"PIN3-TGT-{_next():04d}", name="Sheet stock")
    lot_a = _stock(db_session, source, location="A-1", qty=60.0, lot="LOT-A")
    lot_b = _stock(db_session, source, location="A-2", qty=40.0, lot="LOT-B")
    _stock(db_session, target, location="B-1", qty=0.0, lot="L9")

    wo = _work_order(db_session, source)
    _tie(db_session, wo, source, qty_planned=90.0, pinned_item=lot_a)

    pv = _preview(client, admin_hdrs, source, target).json()
    by_item = {ln["inventory_item_id"]: ln for ln in pv["source"]["lines"]}
    # Withheld 60, not 90 — a pin can only hold back material that is on the row.
    assert by_item[lot_a.id]["quantity_pinned"] == 60.0
    assert by_item[lot_a.id]["quantity_combinable"] == 0.0
    assert by_item[lot_a.id]["eligible"] is False
    # …and the other lot still moves. 90 - 60 = 30 of unpinned demand remains
    # charged against it, so the cap is 40 - 30 = 10.
    assert by_item[lot_b.id]["quantity_combinable"] == 40.0
    assert pv["source"]["eligible_available"] == 40.0
    assert pv["max_combinable_quantity"] == 10.0

    ok = _combine(client, admin_hdrs, source, target, 10.0)
    assert ok.status_code == 200, ok.text
    assert [line["lot_number"] for line in ok.json()["lines"]] == ["LOT-B"]

    db_session.expire_all()
    assert db_session.query(InventoryItem).filter(InventoryItem.id == lot_a.id).first().quantity_on_hand == 60.0


# --------------------------------------------------------------------------- #
# B6 — serial numbers
# --------------------------------------------------------------------------- #


def test_combine_refuses_a_serial_conflict_on_the_merge_path(db_session: Session, client: TestClient, admin_hdrs):
    """One serial number cannot come to name two units.

    THE BUG: ``_find_stock_row`` ignores ``serial_number`` entirely. Measured:
    source (BIN-1, lot NULL, serial ``SN-SOURCE``, qty 1) folded onto an existing
    target row (BIN-1, lot NULL, serial ``SN-TARGET``, qty 1) produced ONE row at
    quantity 2.0 carrying ``SN-TARGET``. A serialized stock row claiming two units
    under one serial is an invariant-5 problem: the whole point of a serial is that
    it identifies one physical article, and the source's serial simply vanished.

    Lot-less rows are used deliberately — that is the shape where the collision is
    easiest to reach, because ``(location, NULL lot)`` matches broadly.
    """
    source = _part(db_session, number=f"SER-SRC-{_next():04d}", name="Weldment")
    target = _part(db_session, number=f"SER-TGT-{_next():04d}", name="Weldment")
    _stock(db_session, source, location="BIN-1", qty=1.0, lot=None, serial_number="SN-SOURCE", unit_cost=99.0)
    target_row = _stock(
        db_session, target, location="BIN-1", qty=1.0, lot=None, serial_number="SN-TARGET", unit_cost=99.0
    )

    pv = _preview(client, admin_hdrs, source, target).json()
    assert "target_serial_mismatch" in _codes(pv["blockers"])
    detail = next(b["detail"] for b in pv["blockers"] if b["code"] == "target_serial_mismatch")
    assert "SN-SOURCE" in detail and "SN-TARGET" in detail

    before = _counts(db_session)
    refused = _combine(client, admin_hdrs, source, target, 1.0)
    assert refused.status_code == 409, refused.text

    db_session.expire_all()
    row = db_session.query(InventoryItem).filter(InventoryItem.id == target_row.id).first()
    assert row.quantity_on_hand == 1.0
    assert row.serial_number == "SN-TARGET"
    assert _on_hand(db_session, source.id) == 1.0
    assert _counts(db_session) == before


@pytest.mark.parametrize(
    "source_serial,target_serial",
    [("SN-ONLY-SOURCE", None), (None, "SN-ONLY-TARGET")],
    ids=["source_serialized_target_not", "target_serialized_source_not"],
)
def test_combine_refuses_a_serial_mismatch_symmetrically(
    db_session: Session, client: TestClient, admin_hdrs, source_serial, target_serial
):
    """Blank on ONE side is a mismatch too — deliberately wider than "both set".

    Losing a serial and inflating one are both misrepresentations of a controlled
    record, so both are refused. Merging a serialized lot into an anonymous row
    drops the serial; merging an anonymous lot into a serialized row makes that
    serial name two units. Neither is a thing this verb is entitled to decide, and
    the operator's escape hatch is the same in both cases: land the material on a
    location or lot the target does not already hold.

    (This is deliberately BROADER than the review's wording, which described only
    the both-set case. Stated here so nobody "corrects" it back.)
    """
    source = _part(db_session, number=f"SERSYM-SRC-{_next():04d}", name="Weldment")
    target = _part(db_session, number=f"SERSYM-TGT-{_next():04d}", name="Weldment")
    _stock(db_session, source, location="BIN-9", qty=1.0, lot="L1", serial_number=source_serial)
    _stock(db_session, target, location="BIN-9", qty=1.0, lot="L1", serial_number=target_serial)

    pv = _preview(client, admin_hdrs, source, target).json()
    assert "target_serial_mismatch" in _codes(pv["blockers"])

    refused = _combine(client, admin_hdrs, source, target, 1.0)
    assert refused.status_code == 409, refused.text
    assert db_session.query(InventoryCombine).count() == 0


def test_combine_allows_a_matching_serial_and_carries_one_onto_a_new_row(
    db_session: Session, client: TestClient, admin_hdrs
):
    """The two cases the refusal must NOT catch.

    Identical serials on both sides are the same article under two numbers — the
    exact thing this verb exists to fold. And a NEW target row carries the serial
    across intact, so serialized stock is not locked out of the feature; it simply
    has to land somewhere the target does not already hold.
    """
    # Same serial on both sides: nothing is misrepresented by merging them.
    source = _part(db_session, number=f"SEROK-SRC-{_next():04d}", name="Weldment")
    target = _part(db_session, number=f"SEROK-TGT-{_next():04d}", name="Weldment")
    _stock(db_session, source, location="BIN-2", qty=1.0, lot="L1", serial_number="SN-SAME")
    existing = _stock(db_session, target, location="BIN-2", qty=1.0, lot="L1", serial_number="SN-SAME")

    pv = _preview(client, admin_hdrs, source, target).json()
    assert "target_serial_mismatch" not in _codes(pv["blockers"])
    ok = _combine(client, admin_hdrs, source, target, 1.0)
    assert ok.status_code == 200, ok.text
    db_session.expire_all()
    assert db_session.query(InventoryItem).filter(InventoryItem.id == existing.id).first().quantity_on_hand == 2.0

    # A new row: the serial travels with the material.
    fresh_source = _part(db_session, number=f"SERNEW-SRC-{_next():04d}", name="Weldment")
    fresh_target = _part(db_session, number=f"SERNEW-TGT-{_next():04d}", name="Weldment")
    _stock(db_session, fresh_source, location="BIN-3", qty=1.0, lot="L7", serial_number="SN-TRAVELS")

    created = _combine(client, admin_hdrs, fresh_source, fresh_target, 1.0)
    assert created.status_code == 200, created.text
    assert created.json()["lines"][0]["target_row_created"] is True
    db_session.expire_all()
    new_row = db_session.query(InventoryItem).filter(InventoryItem.part_id == fresh_target.id).one()
    assert new_row.serial_number == "SN-TRAVELS"


# --------------------------------------------------------------------------- #
# B7 — the deactivate_source race
# --------------------------------------------------------------------------- #


def test_combine_declines_to_deactivate_when_stock_arrives_after_the_lock(
    db_session: Session, client: TestClient, admin_hdrs, monkeypatch
):
    """A receipt landing mid-combine stops the source being retired — and does not
    throw the correct fold away.

    THE BUG: ``FOR UPDATE`` locks ROWS, not the PREDICATE. A ``/receive`` onto the
    source that commits after ``_lock_source_stock_rows`` ran INSERTS a row the
    ``source_still_has_stock`` probe never saw. Acting on that probe alone
    deactivated a part with material physically on the shelf, and wrote a header row
    whose ``source_quantity_after`` was > 0 while its ``source_deactivated`` was
    True — a record contradicting itself on its own two columns.

    THE CHOSEN BEHAVIOUR IS DECLINE, NOT RAISE, and that is a decision rather than
    an accident: the fold itself is correct and already posted, and throwing away a
    correct net-zero move because somebody received stock a second earlier helps
    nobody. The response and the header both report ``source_deactivated: false``,
    which a client must surface as a WARNING rather than a success, and the operator
    retires the part deliberately via ``POST /parts/{id}/deactivate``.

    The racing receipt is injected inside the drain loop — after every lock is held
    and before the post-move total is read — which is precisely the window the
    ``FOR UPDATE`` cannot cover.
    """
    source, target = (
        _part(db_session, number=f"RACE-SRC-{_next():04d}", name="Plate stock"),
        _part(db_session, number=f"RACE-TGT-{_next():04d}", name="Plate stock"),
    )
    _stock(db_session, source, location="A-1", qty=10.0, lot="L1", unit_cost=3.0)
    _stock(db_session, target, location="B-1", qty=1.0, lot="L2", unit_cost=3.0)

    original = inventory_combine_service._apply_to_target_row
    injected = {"done": False}

    def _receive_mid_flight(db, company_id, **kwargs):
        result = original(db, company_id, **kwargs)
        if not injected["done"]:
            injected["done"] = True
            # A dock receipt against the source part: a row the source lock never saw.
            db.add(
                InventoryItem(
                    part_id=source.id,
                    location="RECV-01",
                    warehouse="MAIN",
                    quantity_on_hand=4.0,
                    quantity_allocated=0.0,
                    quantity_available=4.0,
                    lot_number="RCV-LATE",
                    unit_cost=3.0,
                    status="available",
                    is_active=True,
                    company_id=company_id,
                )
            )
            db.flush()
        return result

    monkeypatch.setattr(inventory_combine_service, "_apply_to_target_row", _receive_mid_flight)

    response = _combine(client, admin_hdrs, source, target, 10.0, deactivate_source=True)
    assert response.status_code == 200, response.text
    body = response.json()
    assert injected["done"] is True, "the racing receipt never fired; this test proves nothing"

    # The fold itself stands, in full.
    assert body["quantity_moved"] == 10.0
    assert body["target_quantity_after"] == 11.0

    # The part was NOT retired, and the record says so consistently.
    assert body["source_deactivated"] is False
    assert body["source_quantity_after"] == 4.0

    db_session.expire_all()
    reloaded = db_session.query(Part).filter(Part.id == source.id).first()
    assert reloaded.is_active is True
    assert reloaded.is_deleted is False

    header = db_session.query(InventoryCombine).one()
    assert header.source_deactivated is False
    # The self-contradiction the guard exists to prevent: never both at once.
    assert not (header.source_deactivated and (header.source_quantity_after or 0.0) > 0.0)
    # No part-level audit row was written, because no part-level change happened.
    assert (
        db_session.query(AuditLog).filter(AuditLog.resource_type == "part", AuditLog.resource_id == source.id).count()
        == 0
    )


# --------------------------------------------------------------------------- #
# B8 — the header quantity
# --------------------------------------------------------------------------- #


def test_header_quantity_is_the_sum_of_the_posted_lines_not_a_subtraction(
    db_session: Session, client: TestClient, admin_hdrs
):
    """``inventory_combines.quantity`` equals the ledger EXACTLY, to the last bit.

    THE BUG: the header recorded ``moved = quantity - remaining``. In the multi-line
    case that is the same sum PLUS the accumulated float error of the running
    remainder, so the header could differ from the ledger rows it claims to
    summarize. These three lots are chosen because the two formulas genuinely
    disagree on them: drawing 0.6 from rows of 0.1 / 0.2 / 0.3 gives
    ``sum(lines) == 0.6000000000000001`` while ``quantity - remaining == 0.6``.

    The assertion is exact equality on purpose — ``pytest.approx`` would pass
    against the defect and is what would have let it ship.
    """
    source = _part(db_session, number=f"SUM-SRC-{_next():04d}", name="Coil stock", uom="pounds")
    target = _part(db_session, number=f"SUM-TGT-{_next():04d}", name="Coil stock", uom="pounds")
    _stock(db_session, source, location="A-1", qty=0.1, lot="L1", unit_cost=1.0)
    _stock(db_session, source, location="A-2", qty=0.2, lot="L2", unit_cost=1.0)
    _stock(db_session, source, location="A-3", qty=0.3, lot="L3", unit_cost=1.0)
    _stock(db_session, target, location="B-1", qty=0.0, lot="L9", unit_cost=1.0)

    response = _combine(client, admin_hdrs, source, target, 0.6)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["lines_moved"] == 3

    line_sum = sum(line["quantity"] for line in body["lines"])
    assert body["quantity_moved"] == line_sum

    db_session.expire_all()
    header = db_session.query(InventoryCombine).one()
    assert header.lines_moved == 3
    assert float(header.quantity) == line_sum

    # …and the ledger the header summarizes agrees bit-for-bit on both halves.
    txns = _combine_txns(db_session, body["combine_id"])
    ins = sum(float(t.quantity) for t in txns if t.reason_code == COMBINE_IN_REASON_CODE)
    outs = sum(float(t.quantity) for t in txns if t.reason_code == COMBINE_OUT_REASON_CODE)
    assert float(header.quantity) == ins
    assert ins + outs == pytest.approx(0.0, abs=1e-12)
    # The defect's value, spelled out, so a regression is unmistakable in the diff.
    assert float(header.quantity) != 0.6


# --------------------------------------------------------------------------- #
# B9 — lock ordering, and per-line audit values
# --------------------------------------------------------------------------- #


def test_every_row_lock_is_taken_before_the_first_audit_write(
    db_session: Session, client: TestClient, admin_hdrs, monkeypatch
):
    """No new row lock is ever requested while the global audit chain lock is held.

    THE BUG: ``AuditService._acquire_chain_lock`` takes a ``pg_advisory_xact_lock``
    on ONE GLOBAL key, held to transaction end. Every sibling stock mutator
    (``/receive``, ``/transfer``, ``/adjust``) takes ALL its row locks before its
    first audit write; the combine called ``_audit_combine_line`` INSIDE the drain
    loop, so line 1's audit grabbed the global chain lock and line 2 then asked for
    a NEW target row lock. Against a ``/receive`` that already held that row and was
    about to write its own audit row, that is a textbook deadlock — and there is no
    DBAPI deadlock handler in this app, so Postgres aborting the victim surfaces as
    a **500 on a stock verb**.

    SQLite ignores ``FOR UPDATE``, so the lock itself cannot be observed here. What
    IS observable, and is the actual property, is the ORDER OF OPERATIONS: every
    target-row resolution (``_find_stock_row``, the only thing that takes a target
    lock) must happen before the first ``_audit_combine_line``. Two lots landing on
    two DIFFERENT existing target rows is the shape that used to interleave.
    """
    from app.api.endpoints import inventory as inventory_endpoints

    source = _part(db_session, number=f"LOCK-SRC-{_next():04d}", name="Plate stock")
    target = _part(db_session, number=f"LOCK-TGT-{_next():04d}", name="Plate stock")
    _stock(db_session, source, location="A-1", qty=5.0, lot="L1", unit_cost=2.0)
    _stock(db_session, source, location="A-2", qty=7.0, lot="L2", unit_cost=2.0)
    _stock(db_session, target, location="A-1", qty=1.0, lot="L1", unit_cost=2.0)
    _stock(db_session, target, location="A-2", qty=1.0, lot="L2", unit_cost=2.0)

    events: list = []
    original_find = inventory_endpoints._find_stock_row
    original_audit = inventory_combine_service._audit_combine_line

    def _record_find(*args, **kwargs):
        if kwargs.get("for_update"):
            events.append("lock_target_row")
        return original_find(*args, **kwargs)

    def _record_audit(*args, **kwargs):
        events.append("audit_line")
        return original_audit(*args, **kwargs)

    monkeypatch.setattr(inventory_endpoints, "_find_stock_row", _record_find)
    monkeypatch.setattr(inventory_combine_service, "_audit_combine_line", _record_audit)

    response = _combine(client, admin_hdrs, source, target, 12.0)
    assert response.status_code == 200, response.text
    assert response.json()["lines_moved"] == 2

    assert events.count("lock_target_row") == 2, "both existing target rows must be locked"
    assert events.count("audit_line") == 2
    first_audit = events.index("audit_line")
    last_lock = len(events) - 1 - events[::-1].index("lock_target_row")
    assert last_lock < first_audit, f"a target row was locked after an audit write: {events}"


def test_two_lines_landing_on_one_target_row_audit_their_own_before_and_after(
    db_session: Session, client: TestClient, admin_hdrs
):
    """Deferring the audit writes must not make both lines report the FINAL total.

    The consequence of B9's fix, and the reason ``_audit_combine_line`` now takes
    ``source_after`` / ``target_after`` explicitly instead of reading them off the
    rows. By the time the buffered calls run, the ORM rows carry their final
    quantities — so two source rows landing on ONE target row would both have
    recorded ``4 -> 12`` on the tamper-evident trail, and neither audit row would
    describe a change that actually happened at that moment.

    Two source rows sharing a ``(location, lot)`` is not contrived: a legacy
    fragmented set is exactly that, and it is also what makes the landing map
    reuse one row rather than minting a duplicate beside it.
    """
    source = _part(db_session, number=f"FRAG-SRC-{_next():04d}", name="Plate stock")
    target = _part(db_session, number=f"FRAG-TGT-{_next():04d}", name="Plate stock")
    frag_a = _stock(db_session, source, location="A-1", qty=3.0, lot="SHARED", unit_cost=2.0)
    frag_b = _stock(db_session, source, location="A-1", qty=5.0, lot="SHARED", unit_cost=2.0)
    landing = _stock(db_session, target, location="A-1", qty=4.0, lot="SHARED", unit_cost=9.0)

    response = _combine(client, admin_hdrs, source, target, 8.0)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["lines_moved"] == 2
    # Both lines landed on the SAME row — no duplicate fragment was minted.
    assert {line["target_inventory_item_id"] for line in body["lines"]} == {landing.id}
    assert [line["target_row_created"] for line in body["lines"]] == [False, False]

    db_session.expire_all()
    assert db_session.query(InventoryItem).filter(InventoryItem.id == landing.id).first().quantity_on_hand == 12.0
    assert db_session.query(InventoryItem).filter(InventoryItem.part_id == target.id).count() == 1

    updates = (
        db_session.query(AuditLog)
        .filter(AuditLog.resource_type == "inventory", AuditLog.action == "UPDATE")
        .order_by(AuditLog.id.asc())
        .all()
    )
    steps = [(row.old_values["quantity_on_hand"], row.new_values["quantity_on_hand"]) for row in updates]

    # The TARGET row's two increments are recorded as the steps they were.
    assert (4.0, 7.0) in steps, f"line 1's target step is missing or cumulative: {steps}"
    assert (7.0, 12.0) in steps, f"line 2's target step is missing or cumulative: {steps}"
    assert (4.0, 12.0) not in steps, "both lines recorded the cumulative figure — the B9 regression"

    # And each SOURCE row's own decrement, likewise per line.
    assert (3.0, 0.0) in steps
    assert (5.0, 0.0) in steps
    assert len(steps) == 4

    # Every step is contiguous with the next on that row: a chain with a gap in it
    # is a chain that lost a movement.
    for old, new in steps:
        assert old != new
    assert sum(new - old for old, new in steps) == pytest.approx(0.0, abs=1e-9)
    assert frag_a.id and frag_b.id  # (rows referenced above; keeps the fixture honest)


def test_no_source_stock_row_is_in_the_session_before_the_lock(
    db_session: Session, client: TestClient, admin_hdrs, monkeypatch
):
    """The precondition the SOURCE half of B1 currently rests on, pinned.

    B1's structural guard, ``populate_existing()``, was added to ``_stock_row_query``
    — the TARGET-row resolver. Its sibling ``_load_inventory_items``, which is what
    ``_lock_source_stock_rows`` locks the SOURCE rows with, does NOT carry it. So the
    source half is safe today for a CONVENTION rather than a guard: nothing in this
    request touches a source stock row before the lock, so the identity map is empty
    when the locked SELECT runs and SQLAlchemy has no cached instance to prefer.
    ``_lock_source_stock_rows``'s docstring states that convention ("the rows are not
    yet in the session's identity map"); this test is what makes it check itself.

    IT IS NOT HYPOTHETICAL. Measured against this build with a source row cached at
    10 while the database held 50: the draw computed 10 - 10 and wrote **0** where a
    fresh read gives 40 — forty units destroyed rather than merely mislaid, because
    the source side writes a DECREASE. Any future edit that reads the source's stock
    rows before the lock (a preview inlined into the write, an eligibility pre-check,
    an eager relationship load) opens exactly that, and it would do so silently. This
    test goes red at that moment, which is the point.

    The durable fix is ``populate_existing()`` on ``_load_inventory_items``'s
    ``for_update`` path — reported to the owner, not made here (this file may not
    edit application code).
    """
    source = _part(db_session, number=f"PRELOCK-SRC-{_next():04d}", name="Plate stock")
    target = _part(db_session, number=f"PRELOCK-TGT-{_next():04d}", name="Plate stock")
    row_a = _stock(db_session, source, location="A-1", qty=10.0, lot="L1", unit_cost=2.0)
    row_b = _stock(db_session, source, location="A-2", qty=6.0, lot="L2", unit_cost=2.0)
    _stock(db_session, target, location="B-1", qty=0.0, lot="L9", unit_cost=2.0)

    # The fixtures themselves seeded the map; detach them so what is observed below
    # is what the REQUEST loaded, not what the test set up.
    source_row_ids = {row_a.id, row_b.id}
    db_session.expunge(row_a)
    db_session.expunge(row_b)

    original = inventory_combine_service._lock_source_stock_rows
    seen: dict = {}

    def _inspect_then_lock(db, company_id, part_id):
        seen["cached"] = {
            instance.id
            for instance in db.identity_map.values()
            if isinstance(instance, InventoryItem) and instance.id in source_row_ids
        }
        return original(db, company_id, part_id)

    monkeypatch.setattr(inventory_combine_service, "_lock_source_stock_rows", _inspect_then_lock)

    response = _combine(client, admin_hdrs, source, target, 16.0)
    assert response.status_code == 200, response.text

    assert "cached" in seen, "the lock was never taken; this test proves nothing"
    assert seen["cached"] == set(), (
        "a source stock row was already in the Session when the FOR UPDATE ran. "
        "_load_inventory_items has no populate_existing(), so the lock now hands back "
        "pre-lock values — see B1."
    )
