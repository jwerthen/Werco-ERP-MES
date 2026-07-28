"""PR 4.5 behavior locks: EXPOSING ``Part.backflush_components``.

Until this PR the flag had no writer anywhere in ``app/`` — no schema field, no
endpoint, no UI — so the BOM/routing backflush leg had never executed against
production data and every test that drove it set the model column through a local
factory. PR 4.5 gives it a door, a dry run and a refusal gate. That changes what has
to be proved, in order of consequence:

1. **The door is the ONLY door** (§1). The flag lives on ``PartUpdate`` and
   ``PartResponse`` and deliberately NOT on ``PartBase``/``PartCreate``, because
   ``PartCreate`` is a bare subclass of ``PartBase`` and both create endpoints and both
   CSV importers splat ``Part(**data)``. One field in the wrong class would make a
   shop-wide "consume this part's BOM automatically, forever" policy settable by a
   spreadsheet column. All four of those paths are asserted shut.
   The list-vs-detail agreement test is in the same section for a related reason:
   ``_part_to_response`` hand-builds every kwarg inside ``except Exception: return
   None`` and the callers filter the ``None``s, so a field omitted there does not
   raise — it makes the LIST report a stale default while ``GET /parts/{id}`` reports
   the truth, or makes the part vanish from the list entirely.

2. **The SECOND door runs the SAME gate** (§2). ``PUT /materials/{id}`` is a
   byte-identical ``setattr`` loop over the same ``PartUpdate`` schema writing the same
   ``parts`` rows. A gate implemented in one of the two files is not a gate, so the
   proof here is that ONE defective part refused through ``/parts`` and through
   ``/materials`` returns the SAME sentence.

3. **The refusal gate, one test per blocking condition** (§3). Each asserts the 409
   AND that the column stayed ``False`` — a gate that returns 409 after writing the
   row would be worse than no gate. The ``code`` is pinned through the readiness GET
   rather than by matching prose, so re-wording a sentence does not silently retarget
   a test.

4. **DRY-RUN PURITY** (§4) — the most important property in the PR. The preview shares
   the completion path's resolver, and that resolver used to write a
   ``BACKFLUSH_DOUBLE_ISSUE_BLOCKED`` hash-chain row from inside the suppression layer.
   A preview taking that path would pollute ``audit_log`` with rows describing nothing
   that happened. The assertion is made against a work order that WOULD produce such a
   row, with a positive control proving the real completion still does — otherwise the
   test would pass just as well if the preview had stopped resolving anything.

5. **PREVIEW FIDELITY** (§5). The lots the preview names are the lots the completion
   actually decrements — including the cross-line depletion a single work order with
   both a tie and BOM demand for one part produces. This is the test that catches a
   preview built on its own predicate instead of the writer's.

6. **FLAG-ON BREADTH** (§6) — the mirror of ``test_backflush_breadth``'s structural
   unreachability proof. That file proves the changed functions are never CALLED with
   the flag off; nothing proved what happens when they are, across the same BOM
   shapes. Now that a supervisor can turn the flag on from a form, that gap is the
   live one.

7. **The synthetic basis** (§7). ``_backflush_basis`` is ``quantity_complete +
   operation scrap`` and the resolver short-circuits below epsilon, so a readiness
   check that used the real basis would walk no BOM at all and pronounce EVERY part at
   opt-in time clean. Pinned by driving the real resolver and the readiness check over
   the same broken BOM and asserting they disagree.

8. **What the gating review changed** (§8), and each item is a live-consequence fix
   rather than a polish pass:

   * a diagnostic must never name a component outside this company — the readiness GET
     is open to every authenticated tenant user and the 409 echoes it, so an unscoped
     joinedload became a DISCLOSURE the moment this PR rendered it;
   * a blocking diagnostic REFUSES the demand it describes at completion and writes a
     ``BACKFLUSH_DEMAND_REFUSED`` chain row. It used to be computed on the write path
     and discarded, which was defensible only while the leg was dark;
   * a soft-deleted part cannot be armed, through EITHER door;
   * the preview reports both of the above, because a preview that disagrees with the
     outcome is worse than no preview — including the shortfall ROW, which it used to
     report as a scalar while the writer posted it against a named heat, and the
     placeholder stock row the writer mints when there is no lot at all;
   * severity is scoped to what it describes: a diagnostic on a line the leg never
     issues cannot refuse an opt-in, and ``no_demand_source`` is advisory on a job while
     staying blocking at opt-in — an over-broad blocker is not a safe default, it is
     either a permanently un-armable part or a red banner over a healthy one;
   * the pinned lot that went on hold AFTER it was pinned is flagged, the one thing the
     writer's own shortage disclosure structurally cannot express;
   * one ``parts`` row's control-change trail is ONE audit query, from both doors;
   * a refusal is attributed ONCE PER REFUSED SCOPE and NOTIFIED once. Two conditions on
     one BOM line are two things to fix (two rows) but one quantity that did not move and
     one signal — charging each row the full demand would put a figure on the hash chain
     that is double what happened, and a refusal with no ``OperationalEvent`` would be
     strictly quieter than the shortage it is worse than.

===========================================================================
Traps that shaped these fixtures
===========================================================================

* **THE FIFO TRAP.** ``make_lot`` here takes an explicit ``received_date`` and every
  lot-ordering test passes one, arranged so insertion order and FIFO order DISAGREE
  (the oldest lot gets the HIGHEST id). With every date NULL — the default in every
  sibling fixture — ``ORDER BY id`` and the FIFO ordering return the same rows in the
  same order, and a lot test proves nothing. See ``test_backflush_lot_policy``'s
  module docstring, which states this at length.
* **SQLite does not enforce foreign keys.** That is what makes the
  ``missing_component_part`` fixture possible at all (a ``BOMItem`` pointing at a part
  id that does not exist). On Postgres the same state arrives via a hard delete or an
  FK that was never enforced; the resolver has to survive it either way.
* **``PartUpdate.version`` is required but cosmetic.** ``Part`` maps no ``version``
  column, so every request here sends ``0`` and concurrent flips do not 409. That is a
  recorded residual of the owner's choice of the ordinary part-edit field, not an
  oversight, and it is asserted rather than worked around.
"""

from datetime import date, datetime, timedelta
from io import BytesIO

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import app.services.completion_inventory_service as cis
import app.services.notification_outbox as notification_outbox
from app.core.security import create_access_token
from app.db.ledger_filter import BACKFLUSH_REFERENCE_TYPE, OPERATION_REFERENCE_TYPE
from app.models.audit_log import AuditLog
from app.models.bom import BOM, BOMItem
from app.models.company import Company
from app.models.inventory import InventoryItem, InventoryTransaction, TransactionType
from app.models.operational_event import OperationalEvent
from app.models.part import Part
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.models.work_order_material import AllocationSource, AllocationStatus, WorkOrderMaterialAllocation
from app.services.audit_service import AuditService, compute_audit_hash
from app.services.completion_inventory_service import (
    BACKFLUSH_BLOCKING,
    BACKFLUSH_COMPONENT_FAILED_EVENT_TYPE,
    BACKFLUSH_DEMAND_REFUSED_AUDIT_ACTION,
    BACKFLUSH_DEMAND_REFUSED_EVENT_TYPE,
    BACKFLUSH_DOUBLE_ISSUE_BLOCKED_AUDIT_ACTION,
    BACKFLUSH_SHORTAGE_EVENT_TYPE,
    _resolve_backflush_demand,
    apply_completion_inventory_effects,
    backflush_readiness_for_part,
)
from app.services.material_consumption_service import (
    HELD_MATERIAL_CONSUMED_AUDIT_ACTION,
    cancel_open_allocations_for_work_order,
)
from app.services.notification_catalog import (
    CHANNEL_EMAIL,
    CHANNEL_IN_APP,
    SOURCE_EVENT_TYPE_TO_KEY,
    entry_for_event_type,
    should_fire,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2
TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"
_seq = {"n": 0}

# A fixed clock so every ``received_date`` in this file is written by hand. See THE
# FIFO TRAP in the module docstring: a default of ``None`` collapses FIFO onto id order.
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
        email=f"bfx-{n}@co{company_id}.test",
        employee_id=f"BFX-{n:05d}",
        first_name="Back",
        last_name="Expose",
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
    is_deleted: bool = False,
    company_id: int = COMPANY_A,
) -> Part:
    _ensure_company(db, company_id)
    n = _next()
    part = Part(
        part_number=f"BFX-P-{n}",
        name=f"Part {n}",
        description="backflush-exposure fixture part",
        part_type=part_type,
        unit_of_measure=uom,
        standard_cost=standard_cost,
        backflush_components=backflush,
        is_active=True,
        is_deleted=is_deleted,
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
        name=f"BFX-WC-{n}",
        code=f"BFX-WC-{n}",
        work_center_type="laser",
        description="backflush-exposure fixture work center",
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
        work_order_number=f"BFX-WO-{n:05d}",
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


def make_lot(
    db: Session,
    part: Part,
    *,
    qty: float = 500.0,
    lot: str = None,
    received_date: datetime = None,
    unit_cost: float = 2.0,
    status_: str = "available",
    is_active: bool = True,
    location: str = "RAW-A",
    company_id: int = COMPANY_A,
) -> InventoryItem:
    """One stock lot.

    ``received_date`` defaults to ``None`` exactly like the sibling fixtures, but every
    ORDERING test in this file passes it explicitly — see THE FIFO TRAP.
    """
    item = InventoryItem(
        part_id=part.id,
        location=location,
        warehouse="MAIN",
        quantity_on_hand=qty,
        quantity_allocated=0.0,
        quantity_available=qty,
        lot_number=lot if lot is not None else f"BFX-LOT-{_next():05d}",
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


def make_bom(db: Session, part: Part, *, is_deleted: bool = False, company_id: int = COMPANY_A) -> BOM:
    bom = BOM(part_id=part.id, revision="A", is_active=True, is_deleted=is_deleted, company_id=company_id)
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
    unit_of_measure: str = None,
    is_alternate: bool = False,
    is_optional: bool = False,
    alternate_group: str = None,
    component_part_id: int = None,
    company_id: int = COMPANY_A,
) -> BOMItem:
    """One BOM line.

    ``component_part_id`` overrides the resolved component so a line can be pointed at a
    part id that does not exist — the ``missing_component_part`` fixture. SQLite does not
    enforce foreign keys, which is the only reason that state is constructible here; on
    Postgres it arrives through a hard delete or an FK that was never enforced.
    """
    item = BOMItem(
        bom_id=bom.id,
        component_part_id=component_part_id if component_part_id is not None else component.id,
        item_number=item_number,
        quantity=quantity,
        item_type=item_type,
        line_type=line_type,
        scrap_factor=scrap_factor,
        unit_of_measure=unit_of_measure,
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


def _csv_file(text: str):
    return {"file": ("import.csv", BytesIO(text.encode("utf-8")), "text/csv")}


# ---------------------------------------------------------------------------
# Drivers / observation helpers
# ---------------------------------------------------------------------------


def run_effects(db: Session, wo: WorkOrder, user: User, *, company_id: int = COMPANY_A) -> None:
    """The real completion entry point the work-order-completion call sites share."""
    apply_completion_inventory_effects(db, wo, user_id=user.id, company_id=company_id, audit=AuditService(db, user))
    db.commit()


def enable_backflush(client: TestClient, user: User, part_id: int, *, enabled: bool = True, path: str = "parts"):
    """The flip, as a client makes it: ``PUT`` carrying only ``version`` and the flag.

    ``version`` is always ``0`` because ``Part`` maps no version column and
    ``_part_to_response`` hard-codes it — see the module docstring's third trap.
    """
    return client.put(
        f"/api/v1/{path}/{part_id}",
        headers=headers_for(user),
        json={"version": 0, "backflush_components": enabled},
    )


def readiness_codes(client: TestClient, user: User, part_id: int) -> tuple[list[str], list[str]]:
    """``(blocker codes, advisory codes)`` from the readiness GET.

    Tests pin the machine ``code`` rather than the operator sentence: the sentences are
    written to be re-worded, and a test that matched prose would either break on a copy
    edit or (worse) keep passing while pointing at a different condition.
    """
    response = client.get(f"/api/v1/parts/{part_id}/backflush-readiness", headers=headers_for(user))
    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    return ([d["code"] for d in body["blockers"]], [d["code"] for d in body["advisories"]])


def audit_count(db: Session, *, company_id: int = COMPANY_A) -> int:
    return db.query(AuditLog).filter(AuditLog.company_id == company_id).count()


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


def refused_rows(db: Session, *, company_id: int = COMPANY_A) -> list[AuditLog]:
    """Every ``BACKFLUSH_DEMAND_REFUSED`` row — one per blocking diagnostic the leg met."""
    return (
        db.query(AuditLog)
        .filter(
            AuditLog.company_id == company_id,
            AuditLog.action == BACKFLUSH_DEMAND_REFUSED_AUDIT_ACTION,
        )
        .order_by(AuditLog.id)
        .all()
    )


def assert_on_the_tamper_evident_chain(db: Session, row: AuditLog) -> None:
    """The row is a real link in the hash chain, not merely a row in ``audit_logs``.

    ``audit_log`` is tamper-evident by construction (invariant 2): every row carries a
    globally-ordered ``sequence_number``, a SHA-256 ``integrity_hash`` over its own
    content, and a ``previous_hash`` naming its predecessor. A refusal recorded through
    any path that skipped that — a hand-built ``AuditLog(...)``, a bulk insert, a
    backfill — would still satisfy "an audit row exists" while being exactly the kind of
    record an auditor may not rely on.

    So this recomputes the hash from the stored fields rather than asserting the column is
    non-empty, and walks the link back to the row before it. Both halves are needed: the
    recomputation catches content that does not match its own digest, the link catches a
    row spliced into the sequence.
    """
    assert row.sequence_number is not None, "no sequence number: the row is not in the global chain"
    assert row.integrity_hash, "no integrity hash: the row is not tamper-evident"
    assert row.integrity_hash == compute_audit_hash(
        sequence_number=row.sequence_number,
        timestamp=row.timestamp,
        user_id=row.user_id,
        user_email=row.user_email,
        action=row.action,
        resource_type=row.resource_type,
        resource_id=row.resource_id,
        resource_identifier=row.resource_identifier,
        description=row.description,
        old_values=row.old_values,
        new_values=row.new_values,
        ip_address=row.ip_address,
        session_id=row.session_id,
        success=row.success,
        previous_hash=row.previous_hash,
    ), "the stored hash does not cover the stored content"

    predecessor = (
        db.query(AuditLog)
        .filter(AuditLog.sequence_number < row.sequence_number)
        .order_by(AuditLog.sequence_number.desc())
        .first()
    )
    if predecessor is not None:
        assert row.previous_hash == predecessor.integrity_hash, "the row does not link to the row before it"


def ledger_fingerprint(db: Session, *, company_id: int = COMPANY_A) -> list[tuple]:
    """Every inventory movement in the tenant, as comparable tuples.

    Deliberately NOT scoped to one work order: a dry run must not write ANYWHERE, and a
    fingerprint that only looked where a row was expected would go blind to one written
    somewhere else.
    """
    return sorted(
        (
            row.id,
            row.part_id,
            row.transaction_type.value,
            float(row.quantity or 0),
            row.reference_type,
            row.reference_id,
            row.lot_number,
            row.allocation_id,
        )
        for row in db.query(InventoryTransaction).filter(InventoryTransaction.company_id == company_id).all()
    )


def stock_fingerprint(db: Session, *, company_id: int = COMPANY_A) -> list[tuple]:
    return sorted(
        (row.id, float(row.quantity_on_hand or 0))
        for row in db.query(InventoryItem).filter(InventoryItem.company_id == company_id).all()
    )


def backflush_issue_rows(db: Session, wo: WorkOrder, part: Part = None, *, company_id: int = COMPANY_A):
    """``work_order_backflush`` ISSUE rows for a work order, in DRAW order (id ASC)."""
    query = db.query(InventoryTransaction).filter(
        InventoryTransaction.company_id == company_id,
        InventoryTransaction.reference_type == BACKFLUSH_REFERENCE_TYPE,
        InventoryTransaction.reference_id == wo.id,
        InventoryTransaction.transaction_type == TransactionType.ISSUE,
    )
    if part is not None:
        query = query.filter(InventoryTransaction.part_id == part.id)
    return query.order_by(InventoryTransaction.id).all()


def op_scoped_rows(db: Session, op: WorkOrderOperation, *, company_id: int = COMPANY_A):
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


def clean_backflush_part(db: Session, *, part_type: str = "manufactured", uom: str = "each") -> tuple[Part, Part]:
    """A finished part whose BOM resolves with NO blocking diagnostic, and its component.

    The BOM line's ``unit_of_measure`` is set EXPLICITLY to the component's. It is not
    ceremony: ``BOMItem.unit_of_measure`` defaults to ``"each"`` at the column, so a
    "clean" fixture built for a component stocked in sheets would trip
    ``unit_of_measure_mismatch`` and quietly stop being clean.
    """
    fg = make_part(db, part_type=part_type, uom=uom)
    component = make_part(db, part_type="purchased", uom=uom)
    add_bom_item(db, make_bom(db, fg), component, quantity=2.0, unit_of_measure=uom)
    return fg, component


# ===========================================================================
# 1. THE FLAG'S ONLY DOOR — exposure without four accidental writers
# ===========================================================================


def test_list_and_detail_agree_about_backflush_components(client: TestClient, db_session: Session):
    """``GET /parts/`` and ``GET /parts/{id}`` must report the SAME flag for one part.

    ``_part_to_response`` hand-builds ~20 explicit kwargs and is wrapped in
    ``except Exception: return None``, with the list callers filtering the ``None``s out.
    So a field forgotten there does not raise — it either makes the list report a stale
    default while the detail read (which serialises the ORM object directly) reports the
    truth, or makes the part disappear from the list altogether. Two endpoints
    disagreeing about whether a part auto-consumes its BOM is the kind of divergence
    nobody notices until material has moved, so BOTH failure modes are asserted: the
    part is PRESENT, and the values match, in both directions.
    """
    user = make_user(db_session)
    on_part = make_part(db_session, backflush=True)
    off_part = make_part(db_session, backflush=False)

    listing = client.get("/api/v1/parts/?limit=500", headers=headers_for(user))
    assert listing.status_code == status.HTTP_200_OK, listing.text
    by_id = {row["id"]: row for row in listing.json()}

    for part, expected in ((on_part, True), (off_part, False)):
        assert part.id in by_id, f"{part.part_number} vanished from the list — the swallowed-exception mode"
        detail = client.get(f"/api/v1/parts/{part.id}", headers=headers_for(user))
        assert detail.status_code == status.HTTP_200_OK, detail.text
        assert detail.json()["backflush_components"] is expected
        assert by_id[part.id]["backflush_components"] is expected, "the list must not report a stale default"


def test_create_part_cannot_set_backflush_components(client: TestClient, db_session: Session):
    """``POST /parts/`` ignores the flag — it is absent from ``PartBase``/``PartCreate``.

    ``create_part`` splats ``Part(**data)``, so the field's ABSENCE from the create
    schema is the whole control. A part is always born with automatic consumption off
    and can only be switched on through the gated update path.
    """
    user = make_user(db_session)
    response = client.post(
        "/api/v1/parts/",
        headers=headers_for(user),
        json={
            "part_number": "BFX-CREATE-1",
            "name": "Created part",
            "part_type": "manufactured",
            "unit_of_measure": "each",
            "backflush_components": True,
        },
    )
    assert response.status_code == status.HTTP_201_CREATED, response.text
    assert response.json()["backflush_components"] is False
    created = db_session.query(Part).filter(Part.part_number == "BFX-CREATE-1").one()
    assert created.backflush_components is False, "the create path must not be able to opt a part in"


def test_create_material_cannot_set_backflush_components(client: TestClient, db_session: Session):
    """``POST /materials/`` is the same splat over the same schema — same answer."""
    user = make_user(db_session)
    response = client.post(
        "/api/v1/materials/",
        headers=headers_for(user),
        json={
            "part_number": "BFX-CREATE-M1",
            "name": "Created material",
            "part_type": "raw_material",
            "unit_of_measure": "sheets",
            "backflush_components": True,
        },
    )
    assert response.status_code == status.HTTP_201_CREATED, response.text
    assert response.json()["backflush_components"] is False
    created = db_session.query(Part).filter(Part.part_number == "BFX-CREATE-M1").one()
    assert created.backflush_components is False


def test_parts_csv_import_cannot_set_backflush_components(client: TestClient, db_session: Session):
    """A spreadsheet column must not be able to switch on automatic consumption.

    The importer builds a ``PartCreate`` from named row keys and then splats it, so an
    unrecognised ``backflush_components`` column is simply never read. That is exactly
    the guarantee worth pinning: the failure mode this prevents is a migration workbook
    quietly opting a hundred parts into moving stock by themselves.
    """
    user = make_user(db_session)
    response = client.post(
        "/api/v1/parts/import-csv",
        headers=headers_for(user),
        files=_csv_file("part_number,name,part_type,backflush_components\nBFX-IMP-1,Imported part,manufactured,true\n"),
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["imported_count"] == 1
    imported = db_session.query(Part).filter(Part.part_number == "BFX-IMP-1").one()
    assert imported.backflush_components is False


def test_materials_csv_import_cannot_set_backflush_components(client: TestClient, db_session: Session):
    """The fourth splat, closed the same way."""
    user = make_user(db_session)
    response = client.post(
        "/api/v1/materials/import-csv",
        headers=headers_for(user),
        files=_csv_file(
            "part_number,name,part_type,backflush_components\nBFX-IMP-M1,Imported material,raw_material,true\n"
        ),
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["imported_count"] == 1
    imported = db_session.query(Part).filter(Part.part_number == "BFX-IMP-M1").one()
    assert imported.backflush_components is False


def test_enabling_a_clean_part_succeeds_and_records_the_readiness_verdict(client: TestClient, db_session: Session):
    """The happy path, plus the audit content that makes the flip reconstructable.

    The flag itself lands in the row's ``changes`` map for free (both sides of the diff
    enumerate model columns). What ``extra_data`` adds is the READINESS VERDICT that
    authorised the flip — which is NOT reconstructable later, because the BOM the check
    read is mutable by other people the moment the request ends.
    """
    user = make_user(db_session)
    fg, _component = clean_backflush_part(db_session)
    assert readiness_codes(client, user, fg.id)[0] == [], "the fixture must actually resolve cleanly"

    response = enable_backflush(client, user, fg.id)
    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["backflush_components"] is True
    db_session.expire_all()
    assert db_session.get(Part, fg.id).backflush_components is True

    row = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.company_id == COMPANY_A,
            AuditLog.resource_type == "part",
            AuditLog.resource_id == fg.id,
        )
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert row is not None, "turning on automatic material consumption is a state change and must be audited"
    extra = row.extra_data or {}
    assert extra["backflush_components"] is True
    assert extra["backflush_readiness"] == "clean"
    assert extra["backflush_readiness_checked_at"], "the verdict must carry when it was taken"
    assert extra["backflush_readiness_advisories"] == []
    assert extra["changes"]["backflush_components"] == {"old": False, "new": True}


def test_explicit_null_is_a_422_not_a_null_write(client: TestClient, db_session: Session):
    """``None`` is this schema's "omitted" sentinel, but the column is ``NOT NULL``.

    Every other optional field on ``PartUpdate`` treats ``None`` as "not sent", and the
    generic ``setattr`` loop writes whatever survives ``exclude_unset=True``. An explicit
    ``null`` would therefore reach ``setattr`` and raise ``IntegrityError`` on Postgres —
    and, worse, store ``NULL`` silently on SQLite, where a subsequent read of
    ``bool(None)`` is False and the part quietly reverts.
    """
    user = make_user(db_session)
    fg, _component = clean_backflush_part(db_session)

    response = client.put(
        f"/api/v1/parts/{fg.id}",
        headers=headers_for(user),
        json={"version": 0, "backflush_components": None},
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, response.text
    db_session.expire_all()
    assert db_session.get(Part, fg.id).backflush_components is False


def test_disabling_is_always_allowed_even_with_blockers(client: TestClient, db_session: Session):
    """Turning it OFF can never issue wrong material, so the gate must not stand in the way.

    A part whose BOM has since been broken is precisely the part somebody urgently needs
    to switch off. Refusing that would trap the shop in the state the gate exists to
    prevent.
    """
    user = make_user(db_session)
    fg = make_part(db_session, backflush=True)
    add_bom_item(db_session, make_bom(db_session, fg), make_part(db_session, part_type="purchased"), quantity=0)
    assert "zero_bom_quantity" in readiness_codes(client, user, fg.id)[0]

    response = enable_backflush(client, user, fg.id, enabled=False)
    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["backflush_components"] is False
    db_session.expire_all()
    assert db_session.get(Part, fg.id).backflush_components is False

    row = (
        db_session.query(AuditLog)
        .filter(AuditLog.resource_type == "part", AuditLog.resource_id == fg.id)
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert (row.extra_data or {})["backflush_readiness"] == "not_evaluated_disable"


def test_restating_the_current_value_is_not_gated(client: TestClient, db_session: Session):
    """A whole-form PUT that re-sends ``false`` on an already-``false`` part must succeed.

    The gate fires on a real ``False -> True`` transition and nothing else. Gating on the
    mere PRESENCE of the key would refuse an unrelated edit (a description change on a
    form that round-trips every field) because of a BOM defect the request is not
    introducing.
    """
    user = make_user(db_session)
    fg = make_part(db_session)
    add_bom_item(db_session, make_bom(db_session, fg), make_part(db_session, part_type="purchased"), quantity=0)
    assert "zero_bom_quantity" in readiness_codes(client, user, fg.id)[0]

    response = client.put(
        f"/api/v1/parts/{fg.id}",
        headers=headers_for(user),
        json={"version": 0, "backflush_components": False, "description": "renamed while the BOM is broken"},
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["description"] == "renamed while the BOM is broken"
    assert response.json()["backflush_components"] is False


# ===========================================================================
# 2. THE SECOND DOOR — /materials writes the same rows, so it runs the same gate
# ===========================================================================


def test_materials_put_runs_the_identical_refusal_gate_as_parts_put(client: TestClient, db_session: Session):
    """ONE defective part, refused through BOTH doors, with the SAME sentence.

    ``PUT /materials/{id}`` is a byte-identical ``setattr`` loop importing the same
    ``PartUpdate`` and writing the same ``parts`` rows; ``PUT /parts/{id}`` does not
    filter on part type, so the very same row is reachable from both. Comparing the two
    ``detail`` strings is what proves the gate is SHARED rather than duplicated — a
    second copy is how the next gap gets created.
    """
    user = make_user(db_session)
    material = make_part(db_session, part_type="raw_material", uom="sheets")

    via_materials = enable_backflush(client, user, material.id, path="materials")
    via_parts = enable_backflush(client, user, material.id, path="parts")

    assert via_materials.status_code == status.HTTP_409_CONFLICT, via_materials.text
    assert via_parts.status_code == status.HTTP_409_CONFLICT, via_parts.text
    assert isinstance(via_materials.json()["detail"], str), "detail is a PLAIN STRING so clients render it verbatim"
    assert via_materials.json()["detail"] == via_parts.json()["detail"], "one gate, not two"
    assert material.part_number in via_materials.json()["detail"]

    db_session.expire_all()
    assert db_session.get(Part, material.id).backflush_components is False


def test_materials_put_can_still_enable_a_clean_part(client: TestClient, db_session: Session):
    """The negative control for the test above: the materials door is gated, not welded.

    Without this, a ``PUT /materials`` that had simply lost the ability to write the
    field at all would satisfy the refusal assertion just as well.
    """
    user = make_user(db_session)
    material, _component = clean_backflush_part(db_session, part_type="raw_material", uom="sheets")

    response = enable_backflush(client, user, material.id, path="materials")
    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["backflush_components"] is True
    db_session.expire_all()
    assert db_session.get(Part, material.id).backflush_components is True


# ===========================================================================
# 3. THE REFUSAL GATE — one test per blocking condition
# ===========================================================================


def assert_refused(client: TestClient, db: Session, user: User, part: Part, *, code: str) -> str:
    """409, the named blocker, and — the half that matters — the row is UNCHANGED.

    A gate that returned 409 after the ``setattr`` loop had already run would be worse
    than no gate: the caller is told it failed while the part quietly starts consuming
    its BOM on the next completion. The gate therefore runs BEFORE the first ``setattr``,
    and every case below re-reads the flag through a FRESH REQUEST — the statement a
    client actually depends on — as well as off the session.

    The audit-row count is checked too. A refusal is not a state change, so it must not
    extend the tamper-evident chain with a row describing an update that did not happen.
    """
    blockers, _advisories = readiness_codes(client, user, part.id)
    assert code in blockers, f"expected {code} among the readiness blockers, got {blockers}"

    before_audit = audit_count(db)
    response = enable_backflush(client, user, part.id)
    assert response.status_code == status.HTTP_409_CONFLICT, response.text
    detail = response.json()["detail"]
    assert isinstance(detail, str), "detail is a PLAIN STRING so every client renders it verbatim"
    assert detail.startswith(f"Part {part.part_number} cannot enable automatic backflush:")

    reread = client.get(f"/api/v1/parts/{part.id}", headers=headers_for(user))
    assert reread.status_code == status.HTTP_200_OK, reread.text
    assert reread.json()["backflush_components"] is False, "a refusal must leave the row untouched"
    db.expire_all()
    assert db.get(Part, part.id).backflush_components is False
    assert audit_count(db) == before_audit, "a refused flip is not a state change and must not be audited"
    return detail


def test_refuses_a_phantom_with_no_bom_to_explode(client: TestClient, db_session: Session):
    """A phantom with nothing to explode into is ISSUED AS IF STOCKED.

    ``_issue_one_component`` then finds no stock for a part that is never stocked by
    definition and mints a placeholder row, driving a fictitious part negative. The
    permissive answer is pinned elsewhere as the leg's behaviour; the gate's job is to
    stop a part reaching that state through an opt-in.
    """
    user = make_user(db_session)
    fg = make_part(db_session)
    phantom = make_part(db_session)
    add_bom_item(db_session, make_bom(db_session, fg), phantom, quantity=1, item_type="phantom")

    assert_refused(client, db_session, user, fg, code="phantom_without_bom")


def test_refuses_an_alternate_group_with_no_primary(client: TestClient, db_session: Session):
    """An alternate group is an OR. Every member alternate means NOTHING is ever issued.

    Before the diagnostic this produced no log line of any kind — the worst shape a
    wrong answer can take, because it is indistinguishable from a BOM with genuinely
    nothing to issue. The fixture carries a normal ungrouped line as well, so demand
    exists and ``no_demand_source`` cannot mask the condition under test.
    """
    user = make_user(db_session)
    fg = make_part(db_session)
    bom = make_bom(db_session, fg)
    add_bom_item(db_session, bom, make_part(db_session, part_type="purchased"), quantity=1, item_number=10)
    alt_a = make_part(db_session, part_type="purchased")
    alt_b = make_part(db_session, part_type="purchased")
    add_bom_item(db_session, bom, alt_a, quantity=1, item_number=20, is_alternate=True, alternate_group="G")
    add_bom_item(db_session, bom, alt_b, quantity=1, item_number=30, is_alternate=True, alternate_group="G")

    assert_refused(client, db_session, user, fg, code="alternate_group_without_primary")
    assert readiness_codes(client, user, fg.id)[0] == [
        "alternate_group_without_primary"
    ], "an ordinary line in the ungrouped pool must not be flagged too"


def test_refuses_a_zero_quantity_bom_line(client: TestClient, db_session: Session):
    """``float(item.quantity or 1)`` coerces a stored ``0.0`` to ONE PER PARENT UNIT.

    ``BOMItem.quantity`` carries no CHECK behind the schema's ``gt=0``, so an importer or
    a hand-written row can hold it, and the leg would then issue a quantity nobody wrote.
    """
    user = make_user(db_session)
    fg = make_part(db_session)
    add_bom_item(db_session, make_bom(db_session, fg), make_part(db_session, part_type="purchased"), quantity=0)

    assert_refused(client, db_session, user, fg, code="zero_bom_quantity")


def test_refuses_a_negative_bom_quantity(client: TestClient, db_session: Session):
    """A negative extension is NETTED against positive demand for the same part.

    Two lines naming one component, one of them negative, quietly under-issue rather
    than failing — and under-issuing is invisible, because the material is still on the
    shelf and nothing says it should not be.
    """
    user = make_user(db_session)
    fg = make_part(db_session)
    add_bom_item(db_session, make_bom(db_session, fg), make_part(db_session, part_type="purchased"), quantity=-2)

    assert_refused(client, db_session, user, fg, code="negative_bom_quantity")


def test_refuses_a_unit_of_measure_mismatch(client: TestClient, db_session: Session):
    """``BOMItem.unit_of_measure`` is documented as "may differ" and read NOWHERE here.

    Demand is a bare float and nothing in the platform converts units, so a line stating
    ``each`` against a part stocked in ``sheets`` issues the wrong quantity of the RIGHT
    material — which is far harder to spot than issuing the wrong part.
    """
    user = make_user(db_session)
    fg = make_part(db_session)
    component = make_part(db_session, part_type="raw_material", uom="sheets")
    add_bom_item(db_session, make_bom(db_session, fg), component, quantity=2, unit_of_measure="each")

    detail = assert_refused(client, db_session, user, fg, code="unit_of_measure_mismatch")
    assert "sheets" in detail and "each" in detail, "the sentence must name both units or it cannot be acted on"


def test_refuses_a_bom_line_with_no_resolvable_component(client: TestClient, db_session: Session):
    """Half of what used to be one unlogged ``continue``, now separable from a cycle.

    A line pointing at nothing and a line cut by the cycle guard are opposite conditions
    with opposite remedies, and merging them meant neither could be reported. Only
    constructible here because SQLite does not enforce foreign keys; on Postgres the
    same state arrives via a hard delete.
    """
    user = make_user(db_session)
    fg = make_part(db_session)
    ghost = make_part(db_session, part_type="purchased")
    add_bom_item(
        db_session,
        make_bom(db_session, fg),
        ghost,
        quantity=1,
        component_part_id=ghost.id + 900_000,
    )

    assert_refused(client, db_session, user, fg, code="missing_component_part")


def test_refuses_a_circular_bom(client: TestClient, db_session: Session):
    """The other half of that ``continue``: a branch cut by the visited-set guard.

    The demand below the cut is dropped without a trace. A phantom line is used because
    a ``make`` sub-assembly's subtree is walked EXCLUDE-ONLY (its children were consumed
    when it was built), and diagnostics are deliberately not collected on a walk that
    cannot move material.
    """
    user = make_user(db_session)
    fg = make_part(db_session)
    sub = make_part(db_session)
    fg_bom = make_bom(db_session, fg)
    add_bom_item(db_session, fg_bom, make_part(db_session, part_type="purchased"), quantity=1, item_number=10)
    add_bom_item(db_session, fg_bom, sub, quantity=1, item_number=20, item_type="phantom")
    add_bom_item(db_session, make_bom(db_session, sub), fg, quantity=1)

    assert_refused(client, db_session, user, fg, code="circular_bom")


def test_refuses_a_soft_deleted_component_part(client: TestClient, db_session: Session):
    """``BOMItem.component_part`` is joinedloaded with NO tenant or soft-delete filter.

    At issue time a tenant-scoped miss proceeds with ``part_number=None, unit_cost=0.0``
    — material issued onto the as-built record with no identity and no cost. Recorded
    rather than silently costed at zero.
    """
    user = make_user(db_session)
    fg = make_part(db_session)
    deleted = make_part(db_session, part_type="purchased", is_deleted=True)
    add_bom_item(db_session, make_bom(db_session, fg), deleted, quantity=1)

    assert_refused(client, db_session, user, fg, code="foreign_component_part")


def test_refuses_a_soft_deleted_but_still_active_bom(client: TestClient, db_session: Session):
    """``_get_active_bom`` filters ``is_active`` only, while ``BOM`` carries the soft-delete mixin.

    So the structure the shop believes it deleted is the structure that moves stock —
    an invariant-3 violation with a material consequence rather than a bookkeeping one.
    Not fixed here (four other callers share that helper); recorded, so a part sitting on
    one cannot opt in.
    """
    user = make_user(db_session)
    fg = make_part(db_session)
    bom = make_bom(db_session, fg, is_deleted=True)
    add_bom_item(db_session, bom, make_part(db_session, part_type="purchased"), quantity=1)

    assert_refused(client, db_session, user, fg, code="deleted_active_bom")


def test_refuses_a_bom_deeper_than_the_recursion_cap(client: TestClient, db_session: Session):
    """The depth cap degrades to "this part cannot opt in" instead of a ``RecursionError``.

    A ``RecursionError`` raised inside the explosion would be swallowed whole by the
    ``except Exception: pass`` at the two reconcile-on-read call sites, silently losing
    the ENTIRE completion's inventory effects — finished-goods receipt included — with
    nothing but a log line. The visited-set cycle guard does not bound DEPTH; it bounds
    repetition of a part, and this chain repeats nothing.
    """
    user = make_user(db_session)
    chain = [make_part(db_session) for _ in range(22)]
    for parent, child in zip(chain, chain[1:]):
        add_bom_item(db_session, make_bom(db_session, parent), child, quantity=1, item_type="phantom")

    assert_refused(client, db_session, user, chain[0], code="bom_depth_exceeded")


def test_refuses_a_part_with_no_demand_source_at_all(client: TestClient, db_session: Session):
    """No active BOM and no routing component: enabling would consume NOTHING.

    Previously an empty result, completely silent and indistinguishable from a part that
    never opted in — so a shop could turn this on, see nothing happen, and have no way to
    tell a broken opt-in from a working one.
    """
    user = make_user(db_session)
    fg = make_part(db_session)

    assert_refused(client, db_session, user, fg, code="no_demand_source")


def test_readiness_is_a_pure_read_and_is_tenant_scoped(client: TestClient, db_session: Session):
    """The readiness GET writes nothing and cannot be pointed at another company's part.

    Invariant 1 plus the rule that governs this whole feature: a poll is not an actor and
    records no reason. Polled here in a loop precisely because a per-call write would
    accumulate.
    """
    user = make_user(db_session)
    fg, _component = clean_backflush_part(db_session)
    foreign = make_part(db_session, company_id=COMPANY_B)

    before_audit = audit_count(db_session)
    before_ledger = ledger_fingerprint(db_session)
    for _ in range(3):
        assert readiness_codes(client, user, fg.id) == ([], [])
    db_session.expire_all()
    assert audit_count(db_session) == before_audit, "a readiness read must not touch the hash chain"
    assert ledger_fingerprint(db_session) == before_ledger

    cross_tenant = client.get(f"/api/v1/parts/{foreign.id}/backflush-readiness", headers=headers_for(user))
    assert cross_tenant.status_code == status.HTTP_404_NOT_FOUND


# ===========================================================================
# 3b. WHO MAY FLIP IT — the owner's decision, pinned rather than assumed
# ===========================================================================


def test_the_reads_are_open_but_the_flip_is_supervisor_tier(client: TestClient, db_session: Session):
    """Reads open to any authenticated tenant user; the WRITE is ADMIN/MANAGER/SUPERVISOR.

    **This asymmetry is a decision, and the SUPERVISOR half is a recorded residual
    rather than an oversight.** The owner chose the ordinary part-edit field over a
    dedicated reasoned verb, so turning on a permanent, shop-wide "consume this part's
    BOM automatically, forever" policy carries the same permission as editing a
    description, and no reason is captured — the readiness verdict on the audit row
    stands in for one.

    Pinned in both directions so nobody can drift it silently: tightening the gate to
    ADMIN/MANAGER, or loosening the READ (which discloses this company's own BOM
    structure and would leave a UI unable to explain a refusal), both have to change a
    test that says why.
    """
    fg, _component = clean_backflush_part(db_session)
    operator = make_user(db_session, role=UserRole.OPERATOR)
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)

    # READ: open. An operator can see the diagnosis, which is what makes a refusal
    # explicable to the person standing in front of the material.
    assert readiness_codes(client, operator, fg.id) == ([], [])
    preview_wo = make_wo(db_session, fg, quantity_ordered=4, quantity_complete=4)
    preview = client.get(f"/api/v1/work-orders/{preview_wo.id}/backflush-preview", headers=headers_for(operator))
    assert preview.status_code == status.HTTP_200_OK, preview.text

    # WRITE: refused for an operator, and the row does not move.
    refused = enable_backflush(client, operator, fg.id)
    assert refused.status_code == status.HTTP_403_FORBIDDEN, refused.text
    db_session.expire_all()
    assert db_session.get(Part, fg.id).backflush_components is False

    # WRITE: a SUPERVISOR can. Recorded residual — see the docstring.
    allowed = enable_backflush(client, supervisor, fg.id)
    assert allowed.status_code == status.HTTP_200_OK, allowed.text
    db_session.expire_all()
    assert db_session.get(Part, fg.id).backflush_components is True


# ===========================================================================
# 4. DRY-RUN PURITY — the property this PR could most easily have lost
# ===========================================================================


def _consumed_then_cancelled_tie(db: Session, user: User) -> tuple[WorkOrder, WorkOrderOperation, Part, Part]:
    """A work order whose ledger shows a tie already drew the BOM's component.

    Reachable through supported verbs, not hypothetical:
    ``cancel_open_allocations_for_work_order`` (what a work-order soft delete calls)
    cancels an OPEN tie regardless of ``qty_consumed``, and the restore path only
    resurrects ties whose most recent DELETE audit row carries the delete's own reason.
    Once CANCELLED, the status-keyed suppression layer cannot see it — which is exactly
    the state where the LEDGER layer fires and writes
    ``BACKFLUSH_DOUBLE_ISSUE_BLOCKED``.
    """
    wc = make_work_center(db)
    fg = make_part(db, backflush=True)
    sheet = make_part(db, uom="sheets", part_type="raw_material")
    make_lot(db, sheet, qty=50, lot="BFX-DBL", received_date=days(1))
    # ``unit_of_measure`` stated to match the part: the column defaults to "each", and an
    # accidental mismatch diagnostic here would muddy a fixture that is about suppression.
    add_bom_item(db, make_bom(db, fg), sheet, quantity=1.0, unit_of_measure="sheets")

    wo = make_wo(db, fg, quantity_ordered=4, quantity_complete=4)
    op = make_op(db, wo, wc, quantity_complete=4)
    tie(db, wo, sheet, operation=op, qty_per_run=1.0, qty_planned=4)

    run_effects(db, wo, user)
    db.expire_all()
    assert [t.quantity for t in op_scoped_rows(db, op)] == [-4], "the tie must really have consumed"

    cancel_open_allocations_for_work_order(db, work_order=wo, company_id=COMPANY_A, audit=AuditService(db, user))
    db.commit()
    db.expire_all()
    return db.get(WorkOrder, wo.id), op, sheet, fg


def test_the_preview_writes_absolutely_nothing_where_a_completion_would_audit(client: TestClient, db_session: Session):
    """**THE test.** A dry run over the exact shape that makes the write path audit.

    The resolver used to write a ``BACKFLUSH_DOUBLE_ISSUE_BLOCKED`` hash-chain row from
    INSIDE its suppression layer, so learning what a completion would do required running
    the thing that records that it HAD. PR 4.5 splits the decision from the record; this
    asserts the split holds where it matters, over a work order whose ledger already
    shows the component gone.

    Three assertions and a control:
      * the preview REACHED that layer — the line comes back ``ledger_consumed``, so the
        purity below is not the purity of a function that bailed out early;
      * nothing was written — audit-row count, full tenant ledger fingerprint and every
        lot's on-hand are identical across FOUR preview calls, and the Session has no
        pending insert/update/delete;
      * specifically NO ``BACKFLUSH_DOUBLE_ISSUE_BLOCKED`` row exists;
      * **positive control**: the real completion afterwards DOES write exactly one, over
        the same data. Without it this test would pass just as well against a preview
        endpoint that had been broken into returning nothing.
    """
    user = make_user(db_session)
    wo, _op, sheet, _fg = _consumed_then_cancelled_tie(db_session, user)

    before_audit = audit_count(db_session)
    before_ledger = ledger_fingerprint(db_session)
    before_stock = stock_fingerprint(db_session)
    assert blocked_audit_rows(db_session) == []

    for _ in range(4):
        response = client.get(f"/api/v1/work-orders/{wo.id}/backflush-preview", headers=headers_for(user))
        assert response.status_code == status.HTTP_200_OK, response.text
        body = response.json()
        lines = [line for line in body["lines"] if line["component_part_id"] == sheet.id]
        assert len(lines) == 1, "the suppressed component must be REPORTED, not omitted"
        assert lines[0]["suppressed"] is True
        assert (
            lines[0]["suppression_reason"] == "ledger_consumed"
        ), "the preview must have reached the ledger-suppression layer — the one that used to write"
        assert lines[0]["already_issued"] == 4.0

    db_session.expire_all()
    assert audit_count(db_session) == before_audit, "a dry run must not extend the tamper-evident chain"
    assert blocked_audit_rows(db_session) == [], "in particular: no BACKFLUSH_DOUBLE_ISSUE_BLOCKED"
    assert ledger_fingerprint(db_session) == before_ledger
    assert stock_fingerprint(db_session) == before_stock
    assert not db_session.new and not db_session.dirty and not db_session.deleted

    # POSITIVE CONTROL — the completion path over the same data still records it.
    run_effects(db_session, db_session.get(WorkOrder, wo.id), user)
    db_session.expire_all()
    rows = blocked_audit_rows(db_session)
    assert len(rows) == 1, "the write path must still audit what the read path only reports"
    assert (rows[0].extra_data or {})["component_part_id"] == sheet.id
    assert on_hand(db_session, sheet) == 46.0, "50 - 4, consumed exactly once"


def test_the_preview_is_tenant_scoped(client: TestClient, db_session: Session):
    """Invariant 1. The preview discloses BOM structure, stock and lot numbers."""
    user = make_user(db_session)
    foreign_part = make_part(db_session, company_id=COMPANY_B)
    foreign_wo = make_wo(db_session, foreign_part, company_id=COMPANY_B)

    response = client.get(f"/api/v1/work-orders/{foreign_wo.id}/backflush-preview", headers=headers_for(user))
    assert response.status_code == status.HTTP_404_NOT_FOUND


# ===========================================================================
# 5. PREVIEW FIDELITY — the lots shown are the lots consumed
# ===========================================================================


def test_preview_names_the_same_lots_the_completion_actually_decrements(client: TestClient, db_session: Session):
    """**THE FIFO TRAP TEST for the preview.** Id order is the exact REVERSE of date order.

    The lot number the draw lands on is written onto the as-built genealogy record, so a
    preview built on its own predicate — rather than on the writer's
    ``consumable_source_items`` + ``plan_stock_draw`` — would promise a heat the engine
    never touches. That failure is invisible in any fixture whose ``received_date`` is
    NULL, because FIFO and lowest-id then agree; here the newest lot is inserted first
    and holds the LOWEST id, and the fixture's own inversion is asserted so it cannot
    decay if someone "tidies" the setup.
    """
    user = make_user(db_session)
    fg = make_part(db_session, backflush=True)
    component = make_part(db_session, part_type="purchased")
    newest = make_lot(db_session, component, qty=10, lot="FID-NEWEST", received_date=days(30))
    middle = make_lot(db_session, component, qty=10, lot="FID-MIDDLE", received_date=days(20))
    oldest = make_lot(db_session, component, qty=10, lot="FID-OLDEST", received_date=days(10))
    assert newest.id < middle.id < oldest.id, "the fixture must give the OLDEST lot the HIGHEST id"

    add_bom_item(db_session, make_bom(db_session, fg), component, quantity=5.0)
    wo = make_wo(db_session, fg, quantity_ordered=5, quantity_complete=5)
    make_op(db_session, wo, make_work_center(db_session), quantity_complete=5)

    response = client.get(f"/api/v1/work-orders/{wo.id}/backflush-preview", headers=headers_for(user))
    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert body["backflush_components"] is True
    assert body["basis"] == 5.0
    line = next(row for row in body["lines"] if row["component_part_id"] == component.id)
    assert line["required_quantity"] == 25.0
    assert line["delta_quantity"] == 25.0
    predicted = [(lot["lot_number"], lot["quantity"]) for lot in line["lots"]]
    assert predicted == [("FID-OLDEST", 10.0), ("FID-MIDDLE", 10.0), ("FID-NEWEST", 5.0)]
    assert line["would_go_negative"] is False
    assert line["available_quantity"] == 30.0

    run_effects(db_session, db_session.get(WorkOrder, wo.id), user)
    db_session.expire_all()

    actual = [(row.lot_number, -float(row.quantity)) for row in backflush_issue_rows(db_session, wo, component)]
    assert actual == predicted, "the preview and the outcome must not be able to disagree about the heat"


def test_preview_models_cross_line_lot_depletion_between_a_tie_and_the_bom(client: TestClient, db_session: Session):
    """Two demand sources for ONE part, in the real leg order, off shared stock.

    The engine runs work-order-scoped ties FIRST (explicit planner intent outranks
    derived demand, and the tie's pin gets first claim), and each draw sees the previous
    draw's decrement. A preview that planned every line against the committed on-hand
    would name the same lot twice — a state the completion cannot produce. The lot
    simulation therefore runs on DETACHED copies, and this is the test that proves it,
    because mutating a Session-tracked ``InventoryItem`` instead would have been written
    out by the next autoflush and would look right here while corrupting stock.
    """
    user = make_user(db_session)
    fg = make_part(db_session, backflush=True)
    sheet = make_part(db_session, uom="sheets", part_type="raw_material")
    first = make_lot(db_session, sheet, qty=6, lot="XLOT-S1", received_date=days(5))
    second = make_lot(db_session, sheet, qty=6, lot="XLOT-S2", received_date=days(9))

    add_bom_item(db_session, make_bom(db_session, fg), sheet, quantity=1.0, unit_of_measure="sheets")
    wo = make_wo(db_session, fg, quantity_ordered=4, quantity_complete=4)
    make_op(db_session, wo, make_work_center(db_session), quantity_complete=4)
    allocation = tie(db_session, wo, sheet, operation=None, qty_planned=5)

    response = client.get(f"/api/v1/work-orders/{wo.id}/backflush-preview", headers=headers_for(user))
    assert response.status_code == status.HTTP_200_OK, response.text
    lines = response.json()["lines"]
    assert [line["source"] for line in lines] == ["work_order_tie", "bom_routing"], "ties are previewed FIRST"

    tie_line, bom_line = lines
    assert tie_line["allocation_id"] == allocation.id
    assert tie_line["requires_opt_in"] is False, "a tie IS its own opt-in"
    assert bom_line["requires_opt_in"] is True
    tie_lots = [(lot["lot_number"], lot["quantity"]) for lot in tie_line["lots"]]
    bom_lots = [(lot["lot_number"], lot["quantity"]) for lot in bom_line["lots"]]
    assert tie_lots == [("XLOT-S1", 5.0)]
    assert bom_lots == [("XLOT-S1", 1.0), ("XLOT-S2", 3.0)], "the BOM draw must see the tie's decrement"

    run_effects(db_session, db_session.get(WorkOrder, wo.id), user)
    db_session.expire_all()

    rows = backflush_issue_rows(db_session, wo, sheet)
    actual_tie = [(r.lot_number, -float(r.quantity)) for r in rows if r.allocation_id == allocation.id]
    actual_bom = [(r.lot_number, -float(r.quantity)) for r in rows if r.allocation_id is None]
    assert actual_tie == tie_lots
    assert actual_bom == bom_lots
    assert db_session.get(InventoryItem, first.id).quantity_on_hand == 0
    assert db_session.get(InventoryItem, second.id).quantity_on_hand == 3


def test_preview_reports_routing_conditions_that_part_readiness_cannot_see(client: TestClient, db_session: Session):
    """The documented split between the two checks, asserted in both directions.

    Routing conditions need a work order to resolve against — there is no routing for a
    job that does not exist yet — so the part-level gate genuinely cannot answer them.
    Stating that is only honest if the preview really does answer them, and if the
    readiness check really is silent about them.
    """
    user = make_user(db_session)
    fg = make_part(db_session)
    component = make_part(db_session, part_type="purchased")
    add_bom_item(db_session, make_bom(db_session, fg), component, quantity=1.0)

    wo = make_wo(db_session, fg, quantity_ordered=4, quantity_complete=4)
    wc = make_work_center(db_session)
    make_op(db_session, wo, wc, quantity_complete=4, component_part=fg, component_quantity=4)

    response = client.get(f"/api/v1/work-orders/{wo.id}/backflush-preview", headers=headers_for(user))
    assert response.status_code == status.HTTP_200_OK, response.text
    preview_codes = [d["code"] for d in response.json()["blockers"]]
    assert (
        "operation_names_own_part" in preview_codes
    ), "an operation naming the job's own part would ISSUE what the FG receipt just received"

    blockers, _advisories = readiness_codes(client, user, fg.id)
    assert "operation_names_own_part" not in blockers, "part scope must not pretend to answer a routing question"


# ===========================================================================
# 6. FLAG-ON BREADTH — the mirror of the flag-off unreachability proof
# ===========================================================================


def test_flag_on_reaches_the_resolver_and_issues_exactly_the_consumable_lines(db_session: Session, monkeypatch):
    """The mirror of ``test_flag_off_never_reaches_any_code_this_pr_changed``.

    That test proves PR 4's demand resolution is UNREACHABLE while every part has the
    flag off. It was a complete answer only while nothing could turn the flag on; now
    that a supervisor can do it from a form, the untested half is the one that moves
    material. Same work order shape as its behavioural twin — alternates, an optional, a
    reference line, a phantom, a ``make`` sub-assembly, a routing operation naming a
    component, and reported scrap — with the flag ON:

      * the resolver IS called (the literal inverse of ``calls == []``);
      * the three CONSUMABLE lines are issued at exactly the right quantities;
      * the four NON-consumable ones are untouched, including ``sub_child``, whose
        subtree is walked exclude-only so the routing leg cannot re-introduce it.
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
    subject_part = make_part(db_session, backflush=True, standard_cost=9.0)
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
        make_lot(db_session, component, qty=500.0, received_date=days(1))

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

    assert "_resolve_backflush_components" in calls, "flag ON must reach the demand resolver"
    assert "_drop_ledger_covered_parts" in calls

    # basis = quantity_complete + operation scrap = 4 + 2 = 6 (a scrapped unit still ate
    # its material). Routing: 12 stated / 6 ordered = 2 per unit x 6 = 12, which AGREES
    # with the BOM's 2 x 6 — so precedence changes nothing and no disagreement is raised.
    issued: dict[int, float] = {}
    for row in backflush_issue_rows(db_session, subject):
        issued[row.part_id] = issued.get(row.part_id, 0.0) + -float(row.quantity)
    assert issued == {primary.id: 12.0, phantom_child.id: 18.0, sub_assembly.id: 6.0}
    assert on_hand(db_session, primary) == 488.0
    assert on_hand(db_session, phantom_child) == 482.0
    assert on_hand(db_session, sub_assembly) == 494.0
    for untouched in (alternate, optional, reference, sub_child):
        assert on_hand(db_session, untouched) == 500.0, f"{untouched.part_number} must never be issued"


# ===========================================================================
# 7. THE SYNTHETIC BASIS — why readiness cannot reuse the real one
# ===========================================================================


def test_readiness_evaluates_a_part_whose_work_orders_have_produced_nothing(client: TestClient, db_session: Session):
    """At opt-in time NOTHING has been produced, and the real resolver answers ``{}``.

    ``_backflush_basis`` is ``quantity_complete + operation scrap`` and
    ``_resolve_backflush_demand`` short-circuits below epsilon, so a readiness check
    reusing the real basis would walk no BOM at all and pronounce EVERY part clean —
    vacuously, and exactly at the moment the answer is acted on. The synthetic basis of
    1.0 exercises every line of the structure without needing a job.

    Both halves are asserted over the SAME broken BOM, so the test states a disagreement
    between two functions rather than a property of one.
    """
    user = make_user(db_session)
    fg = make_part(db_session)
    add_bom_item(db_session, make_bom(db_session, fg), make_part(db_session, part_type="purchased"), quantity=0)
    wo = make_wo(db_session, fg, quantity_ordered=10, quantity_complete=0)
    make_op(db_session, wo, make_work_center(db_session), quantity_complete=0, status_=OperationStatus.PENDING)

    resolution = _resolve_backflush_demand(db_session, wo, COMPANY_A)
    assert resolution.basis == 0.0
    assert resolution.demand == {}
    assert resolution.diagnostics == [], "the real resolver is silent on an unproduced job — that is the trap"

    diagnostics = backflush_readiness_for_part(db_session, fg, company_id=COMPANY_A)
    assert [d.code for d in diagnostics if d.severity == BACKFLUSH_BLOCKING] == ["zero_bom_quantity"]
    assert_refused(client, db_session, user, fg, code="zero_bom_quantity")

    # And the preview says so plainly rather than showing an empty table that reads as a
    # bug: basis 0 is the engine's real answer for a job that has produced nothing.
    response = client.get(f"/api/v1/work-orders/{wo.id}/backflush-preview", headers=headers_for(user))
    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["basis"] == 0.0
    assert response.json()["lines"] == []


def test_readiness_flags_a_routing_only_part_as_an_advisory_not_a_blocker(client: TestClient, db_session: Session):
    """A part with no BOM but a routing that names a component may still opt in.

    ``no_demand_source`` must not fire on it — the routing IS a demand source, just not
    one the part-scoped check can validate — so the honest answer is an advisory saying
    where the demand will come from, and a flip that succeeds.
    """
    user = make_user(db_session)
    fg = make_part(db_session)
    component = make_part(db_session, part_type="purchased")
    wo = make_wo(db_session, fg, quantity_ordered=4, quantity_complete=4)
    make_op(
        db_session,
        wo,
        make_work_center(db_session),
        quantity_complete=4,
        component_part=component,
        component_quantity=8,
    )

    blockers, advisories = readiness_codes(client, user, fg.id)
    assert blockers == []
    assert "routing_only_no_bom" in advisories

    response = enable_backflush(client, user, fg.id)
    assert response.status_code == status.HTTP_200_OK, response.text
    row = (
        db_session.query(AuditLog)
        .filter(AuditLog.resource_type == "part", AuditLog.resource_id == fg.id)
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert (row.extra_data or {})["backflush_readiness_advisories"] == [
        "routing_only_no_bom"
    ], "the advisories the flip was accepted despite must be on the chain row"


# ===========================================================================
# 8. THE GATING FIXES — cross-tenant silence, the refusal, and the deleted part
# ===========================================================================


def assert_names_no_foreign_part(raw_body: str, diagnostics: list[dict], foreign: Part) -> None:
    """No identity of the foreign part anywhere in a rendered surface.

    Asserted over the WHOLE serialized body rather than over the one diagnostic under
    test: the disclosure this guards against is "the object was materialised and then
    printed somewhere", and a check aimed at a single field would go blind to a second
    render site added later. The id is checked structurally (``component_part_id`` on
    every diagnostic) rather than as a substring, because a bare integer collides with
    unrelated ids in the same document and would make the assertion lie in both
    directions.
    """
    assert foreign.part_number not in raw_body, "another company's part number must not leave the server"
    for diagnostic in diagnostics:
        assert diagnostic["component_part_id"] != foreign.id, "not even the id — it is enumerable, and it is not ours"
        assert diagnostic["component_part_number"] != foreign.part_number


def test_a_diagnostic_never_names_a_component_outside_this_company(client: TestClient, db_session: Session):
    """**SECURITY REGRESSION TEST — invariant 1, one hop out.**

    A BOM line can point at ANOTHER tenant's part: ``bom.py``'s add-line validator
    resolves ``component_part_id`` with no ``company_id`` filter, so a sequential id
    belonging to company B is reachable through a supported verb. Until PR 4.5 that only
    mattered inside the server — ``BOMItem.component_part`` was joinedloaded unscoped and
    nothing rendered the object. This PR renders diagnostics, so the unscoped joinedload
    became a DISCLOSURE, and it became one on three surfaces at once:

      1. ``GET /parts/{id}/backflush-readiness`` — open to EVERY authenticated user of
         company A, ``OPERATOR`` included;
      2. the ``409`` refusal detail, which echoes the same sentences verbatim;
      3. ``GET /work-orders/{id}/backflush-preview`` — same resolver, same auth tier.

    All three are asserted here, because the fix is at the LOOKUP (``_tenant_components``
    never materialises the foreign row) and a fix at the lookup is exactly the kind that
    a later "helpful" ``joinedload`` re-opens on whichever surface the test forgot.

    What the operator keeps is the honest, actionable half: WHICH line to repoint.
    """
    user = make_user(db_session)
    fg = make_part(db_session)
    foreign = make_part(db_session, part_type="purchased", company_id=COMPANY_B)
    line = add_bom_item(db_session, make_bom(db_session, fg), foreign, quantity=1, component_part_id=foreign.id)
    wo = make_wo(db_session, fg, quantity_ordered=5, quantity_complete=5)
    make_op(db_session, wo, make_work_center(db_session), quantity_complete=5)
    foreign_number = foreign.part_number

    # --- surface 1: the readiness GET
    response = client.get(f"/api/v1/parts/{fg.id}/backflush-readiness", headers=headers_for(user))
    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    # ``no_demand_source`` rides along — the line contributed nothing, which is the whole
    # complaint — so the diagnostic under test is selected by code rather than by position.
    diagnostic = next(d for d in body["blockers"] if d["code"] == "missing_component_part")
    assert diagnostic["bom_item_id"] == line.id, "the actionable half stays: WHICH line to repoint"
    assert diagnostic["component_part_number"] is None
    assert diagnostic["component_part_id"] is None
    assert foreign_number not in diagnostic["detail"]
    assert_names_no_foreign_part(response.text, body["blockers"] + body["advisories"], foreign)

    # --- surface 2: the 409 that echoes the same sentences
    refusal = enable_backflush(client, user, fg.id)
    assert refusal.status_code == status.HTTP_409_CONFLICT, refusal.text
    assert foreign_number not in refusal.json()["detail"]
    assert foreign_number not in refusal.text

    # --- surface 3: the dry-run preview, which shares the resolver
    preview = client.get(f"/api/v1/work-orders/{wo.id}/backflush-preview", headers=headers_for(user))
    assert preview.status_code == status.HTTP_200_OK, preview.text
    preview_body = preview.json()
    assert_names_no_foreign_part(preview.text, preview_body["blockers"] + preview_body["advisories"], foreign)
    assert all(row["component_part_id"] != foreign.id for row in preview_body["lines"])


def test_a_soft_deleted_component_still_names_itself(client: TestClient, db_session: Session):
    """The other half of the split branch — and it must NOT be silenced.

    A soft-deleted component is this company's own part. Naming it is what makes the
    refusal actionable, and withholding the number to be uniformly cautious would leave an
    operator with a sentence they cannot act on. The two cases share a ``code`` and differ
    only in what they may disclose.
    """
    user = make_user(db_session)
    fg = make_part(db_session)
    deleted = make_part(db_session, part_type="purchased", is_deleted=True)
    add_bom_item(db_session, make_bom(db_session, fg), deleted, quantity=1)

    blockers = client.get(f"/api/v1/parts/{fg.id}/backflush-readiness", headers=headers_for(user)).json()["blockers"]
    diagnostic = next(d for d in blockers if d["code"] == "foreign_component_part")
    assert diagnostic["component_part_number"] == deleted.part_number
    assert deleted.part_number in diagnostic["detail"]


def test_a_soft_deleted_part_cannot_be_armed(client: TestClient, db_session: Session):
    """``delete_part`` checks dependencies only on a HARD delete, so a soft-deleted part
    keeps its in-flight work orders — and the PUT lookup filters ``company_id`` only, so
    it stays reachable by id from a bookmark or a work-order page.

    Arming it would move component stock on behalf of a part the shop believes is gone.
    Closed with a blocking diagnostic rather than a lookup change, because that closes
    BOTH doors (the gate calls readiness, and so does the GET) without touching a query
    four other handlers share. The BOM is deliberately CLEAN, so the refusal can only be
    coming from the part's own deleted state.

    **Both doors are asserted, not one and an argument.** "One diagnostic closes both" is
    a claim about where the check lives, and the cheapest way for it to stop being true is
    a future lookup change on one router only. The part is a ``raw_material`` so the SAME
    row is reachable through ``PUT /materials/{id}``, which filters on part type.
    """
    user = make_user(db_session)
    fg, _component = clean_backflush_part(db_session, part_type="raw_material", uom="sheets")
    fg.is_deleted = True
    db_session.commit()

    detail = assert_refused(client, db_session, user, fg, code="deleted_part")

    via_materials = enable_backflush(client, user, fg.id, path="materials")
    assert via_materials.status_code == status.HTTP_409_CONFLICT, via_materials.text
    assert via_materials.json()["detail"] == detail, "one gate, both doors — not a second copy of the check"
    db_session.expire_all()
    assert db_session.get(Part, fg.id).backflush_components is False


def test_a_blocking_diagnostic_refuses_that_component_at_completion_and_records_it(db_session: Session):
    """**The completion path no longer computes "this BOM is wrong" and discards it.**

    The opt-in gate is a one-time check and every input it reads is mutable afterwards by
    anyone with ``boms:edit``. So the reachable state is: arm a clean part on Monday, edit
    a BOM line's unit of measure on Tuesday, complete a work order on Wednesday. Through
    PR 4.4 the resolver detected that on Wednesday, dropped the finding on the floor, and
    issued the untrusted figure — no log line, no audit row, no event, and a ledger row
    that reads like any other consumption.

    Now: the component is REFUSED (under-issuing is this module's stated safe direction —
    the material is still on the shelf, where the operator who needs it draws it manually)
    and the refusal is on the tamper-evident chain, naming the BOM line to fix. The
    healthy component on the same BOM is untouched, which is what makes the refusal
    per-component rather than a job-wide outage.
    """
    user = make_user(db_session)
    fg = make_part(db_session, backflush=True)
    drifted = make_part(db_session, part_type="purchased", uom="sheets")
    healthy = make_part(db_session, part_type="purchased")
    make_lot(db_session, drifted, qty=100, received_date=days(1))
    make_lot(db_session, healthy, qty=100, received_date=days(1))

    bom = make_bom(db_session, fg)
    # Tuesday's edit: the line now states a unit the part is not stocked in, and nothing
    # in the platform converts units.
    add_bom_item(db_session, bom, drifted, quantity=2, item_number=10, unit_of_measure="each")
    add_bom_item(db_session, bom, healthy, quantity=1, item_number=20, unit_of_measure="each")

    wo = make_wo(db_session, fg, quantity_ordered=5, quantity_complete=5)
    make_op(db_session, wo, make_work_center(db_session), quantity_complete=5)

    run_effects(db_session, wo, user)
    db_session.expire_all()

    issued = {row.part_id: -float(row.quantity) for row in backflush_issue_rows(db_session, wo)}
    assert issued == {healthy.id: 5.0}, "the trustworthy line still consumes; the untrusted one does not"
    assert on_hand(db_session, drifted) == 100.0

    rows = refused_rows(db_session)
    assert len(rows) == 1, "one chain row per blocking diagnostic — a silent refusal is the same gap as a silent issue"
    extra = rows[0].extra_data or {}
    assert extra["diagnostic_code"] == "unit_of_measure_mismatch"
    assert extra["component_part_id"] == drifted.id
    assert extra["refused_quantity"] == 10.0, "2 per unit x 5 produced — the quantity that did NOT move"
    assert extra["refused_whole_leg"] is False
    assert extra["work_order_id"] == wo.id
    assert extra["bom_item_id"] is not None, "the row must name the line to fix, not merely that something was wrong"
    assert_on_the_tamper_evident_chain(db_session, rows[0])


def test_a_structural_blocker_refuses_the_whole_leg(db_session: Session):
    """A diagnostic that names no component means the resolved demand is incomplete in a
    way no component owns — a soft-deleted active BOM, a structure deeper than the cap, a
    line whose component does not resolve. There is no per-component refusal available,
    and issuing "the parts we did manage to resolve" would write a partial consumption
    that reads exactly like a complete one.

    ``_get_active_bom`` filters ``is_active`` only, so the structure the shop deleted is
    the structure that would have moved stock.
    """
    user = make_user(db_session)
    fg = make_part(db_session, backflush=True)
    component = make_part(db_session, part_type="purchased")
    make_lot(db_session, component, qty=100, received_date=days(1))
    add_bom_item(db_session, make_bom(db_session, fg, is_deleted=True), component, quantity=2)

    wo = make_wo(db_session, fg, quantity_ordered=5, quantity_complete=5)
    make_op(db_session, wo, make_work_center(db_session), quantity_complete=5)

    run_effects(db_session, wo, user)
    db_session.expire_all()

    assert backflush_issue_rows(db_session, wo) == []
    assert on_hand(db_session, component) == 100.0

    rows = refused_rows(db_session)
    assert len(rows) == 1
    extra = rows[0].extra_data or {}
    assert extra["diagnostic_code"] == "deleted_active_bom"
    assert extra["refused_whole_leg"] is True
    assert extra["refused_quantity"] == 10.0, "the whole leg's surviving demand, not one component's share"
    assert rows[0].resource_type == "work_order", "a structural blocker is a fact about the JOB, not a component"
    assert_on_the_tamper_evident_chain(db_session, rows[0])


def test_the_preview_reports_a_refusal_the_completion_will_make(client: TestClient, db_session: Session):
    """The preview must state the SAME answer the completion acts on.

    ``blocked_demand_refusal`` is the pure half of ``_refuse_blocked_demand`` precisely so
    this cannot drift: a panel that showed "would post now: 10" for a component the engine
    is about to refuse would be worse than showing nothing, because it would be read as a
    commitment.
    """
    user = make_user(db_session)
    fg = make_part(db_session, backflush=True)
    drifted = make_part(db_session, part_type="purchased", uom="sheets")
    make_lot(db_session, drifted, qty=100, received_date=days(1))
    add_bom_item(db_session, make_bom(db_session, fg), drifted, quantity=2, unit_of_measure="each")

    wo = make_wo(db_session, fg, quantity_ordered=5, quantity_complete=5)
    make_op(db_session, wo, make_work_center(db_session), quantity_complete=5)

    body = client.get(f"/api/v1/work-orders/{wo.id}/backflush-preview", headers=headers_for(user)).json()
    line = next(row for row in body["lines"] if row["component_part_id"] == drifted.id)
    assert line["suppressed"] is True
    assert line["suppression_reason"] == "blocking_diagnostic"
    assert line["delta_quantity"] == 0.0, "nothing would post, and the panel must not imply otherwise"
    assert "unit_of_measure_mismatch" in [d["code"] for d in body["blockers"]]


def test_the_preview_names_the_lot_the_shortfall_row_drives_negative(client: TestClient, db_session: Session):
    """**The preview's whole promise, in the case that used to break it.**

    ``plan_stock_draw`` returns only the COVERED takes. The writer does not stop there: on
    a shortfall it posts a SECOND issue for the remainder against the last lot it drew,
    and that lot number goes onto the as-built genealogy. The preview used to report the
    remainder as a bare scalar, so for 25 over lots of 10 and 5 it said "A:10, B:5, short
    10" while the completion posted A:10, B:5 and **B:10** — lot B contributing 15 and
    ending at -10. A planner asking "which heats went into this job, and how much of
    each" was given a wrong number for B.

    Both paths now go through ``_shortfall_anchor``, and this asserts the preview's lot
    list against the LEDGER, not against a restatement of the same arithmetic.

    The two lots are inserted NEWEST FIRST — see THE FIFO TRAP in the module docstring.
    With ``received_date`` ascending in insertion order, ``ORDER BY id`` and the FIFO
    ordering return the same rows in the same order, and both the draw order and the
    identity of the shortfall anchor (the LAST lot drawn) would be satisfied by an
    implementation that never looked at a date.
    """
    user = make_user(db_session)
    fg = make_part(db_session, backflush=True)
    component = make_part(db_session, part_type="purchased")
    make_lot(db_session, component, qty=5, lot="SHORT-B", received_date=days(20))
    make_lot(db_session, component, qty=10, lot="SHORT-A", received_date=days(10))

    add_bom_item(db_session, make_bom(db_session, fg), component, quantity=5.0)
    wo = make_wo(db_session, fg, quantity_ordered=5, quantity_complete=5)
    make_op(db_session, wo, make_work_center(db_session), quantity_complete=5)

    body = client.get(f"/api/v1/work-orders/{wo.id}/backflush-preview", headers=headers_for(user)).json()
    line = next(row for row in body["lines"] if row["component_part_id"] == component.id)
    assert line["required_quantity"] == 25.0
    assert line["shortfall"] == 10.0
    assert line["would_go_negative"] is True
    assert line["shortfall_creates_placeholder"] is False
    predicted = [(lot["lot_number"], lot["quantity"], lot["is_shortfall"]) for lot in line["lots"]]
    assert predicted == [
        ("SHORT-A", 10.0, False),
        ("SHORT-B", 5.0, False),
        ("SHORT-B", 10.0, True),
    ], "the remainder is a ROW against a NAMED heat, not a scalar the panel prints beside the list"

    run_effects(db_session, db_session.get(WorkOrder, wo.id), user)
    db_session.expire_all()

    actual = [(row.lot_number, -float(row.quantity)) for row in backflush_issue_rows(db_session, wo, component)]
    assert actual == [(lot, qty) for lot, qty, _short in predicted], "preview and ledger must agree, row for row"
    assert on_hand(db_session, component) == -10.0, "the shortfall really does drive that lot negative"


def test_the_preview_says_so_when_a_shortfall_would_mint_a_placeholder_stock_row(
    client: TestClient, db_session: Session
):
    """The empty-stock half of the same promise, and the worse half.

    With no lot at all the writer does not merely go negative — it MINTS an
    ``InventoryItem`` (``_placeholder_stock_row``: lot-less, at the finished-goods
    location) and names it on the ISSUE row, because a transaction with a dangling
    ``inventory_item_id`` would be worse than a negative on-hand. A dry run may not create
    that row, and inventing a lot number for a row that does not exist would be a lie, so
    it reports the FACT instead: no lots, and ``shortfall_creates_placeholder``.

    Asserted against the ledger like its sibling: the preview names no heat because there
    is no heat to name, and the row the completion writes names none either.
    """
    user = make_user(db_session)
    fg = make_part(db_session, backflush=True)
    component = make_part(db_session, part_type="purchased", standard_cost=4.0)
    add_bom_item(db_session, make_bom(db_session, fg), component, quantity=5.0)
    wo = make_wo(db_session, fg, quantity_ordered=5, quantity_complete=5)
    make_op(db_session, wo, make_work_center(db_session), quantity_complete=5)
    assert on_hand(db_session, component) == 0.0, "the fixture's whole point: this part has no stock row at all"

    body = client.get(f"/api/v1/work-orders/{wo.id}/backflush-preview", headers=headers_for(user)).json()
    line = next(row for row in body["lines"] if row["component_part_id"] == component.id)
    assert line["required_quantity"] == 25.0
    assert line["available_quantity"] == 0.0
    assert line["shortfall"] == 25.0
    assert line["would_go_negative"] is True
    assert line["shortfall_creates_placeholder"] is True
    assert line["lots"] == [], "a dry run must not invent a lot for a row that does not exist yet"

    run_effects(db_session, db_session.get(WorkOrder, wo.id), user)
    db_session.expire_all()

    rows = backflush_issue_rows(db_session, wo, component)
    assert [(row.lot_number, -float(row.quantity)) for row in rows] == [(None, 25.0)]
    minted = db_session.get(InventoryItem, rows[0].inventory_item_id)
    assert minted is not None, "the ISSUE row must point at a REAL stock row, not a dangling id"
    assert minted.lot_number is None, "a placeholder names no heat — that is why the preview names none either"
    assert on_hand(db_session, component) == -25.0


def test_a_non_consumed_bom_line_cannot_block_an_opt_in_over_demand_it_never_states(
    client: TestClient, db_session: Session
):
    """``missing_component_part`` / ``circular_bom`` are gated on the LINE, not the level.

    ``_explode_backflush_bom``'s own rule is that a diagnostic is collected only for a
    line this walk would actually ISSUE, and its stated reason is that "flagging it would
    refuse an opt-in over a line the leg never reads". Both of these fired before that
    rule was applied to them — they were gated on ``consumed`` (the level) rather than on
    ``line_consumed`` (the line).

    Failure scenario the gating closes: a BOM carries a ``reference`` line — the enum's
    own comment is "Reference only - not consumed" — pointing at a retired fixture whose
    ``Part`` row is gone. The part could never be armed, permanently, over a line that
    contributes zero demand either way and that the operator would have to delete for a
    reason unrelated to backflush.

    The blocking form of both codes is pinned in §3 (``test_refuses_a_bom_line_with_no_
    resolvable_component`` / ``test_refuses_a_circular_bom``), so this test cannot pass by
    the diagnostics having been dropped altogether.
    """
    user = make_user(db_session)
    fg = make_part(db_session)
    component = make_part(db_session, part_type="purchased")
    bom = make_bom(db_session, fg)
    add_bom_item(db_session, bom, component, quantity=2, item_number=10, unit_of_measure="each")
    # A reference line pointing at nothing: would be ``missing_component_part`` if issued.
    ghost = make_part(db_session, part_type="purchased")
    add_bom_item(
        db_session,
        bom,
        ghost,
        quantity=1,
        item_number=20,
        line_type="reference",
        component_part_id=ghost.id + 900_000,
    )
    # An OPTIONAL line closing a cycle back onto the assembly: would be ``circular_bom``.
    add_bom_item(db_session, bom, fg, quantity=1, item_number=30, item_type="phantom", is_optional=True)

    blockers, _advisories = readiness_codes(client, user, fg.id)
    assert blockers == [], "a line the leg never issues cannot make a part permanently un-armable"

    response = enable_backflush(client, user, fg.id)
    assert response.status_code == status.HTTP_200_OK, response.text
    db_session.expire_all()
    assert db_session.get(Part, fg.id).backflush_components is True


def test_no_demand_source_is_advisory_on_a_job_and_blocking_only_at_opt_in(client: TestClient, db_session: Session):
    """One condition, two scopes, two severities — and the severity is the whole point.

    "This has no component demand" is a DEFECT at opt-in (arming a part whose completions
    consume nothing is a shop believing an automation runs when it does not) and the
    ORDINARY case on a work order (a single-op turned part, a purchased item, a part-less
    standalone nest package). Emitting it as blocking at both scopes painted a red *"1
    problem resolving this demand"* banner over perfectly healthy jobs — the alarm fatigue
    the whole severity vocabulary exists to avoid — and, worse, fed it to
    ``_refuse_blocked_demand``'s STRUCTURAL tier, where a blocker naming no component
    refuses the entire leg. There is nothing there to refuse, and "no BOM" is not a reason
    to distrust a BOM.

    The armed half of the fixture is a reachable state, not a contrivance: arm a part
    while its BOM is good, then retire the BOM.
    """
    user = make_user(db_session)
    fg = make_part(db_session)
    wo = make_wo(db_session, fg, quantity_ordered=4, quantity_complete=4)
    make_op(db_session, wo, make_work_center(db_session), quantity_complete=4)

    body = client.get(f"/api/v1/work-orders/{wo.id}/backflush-preview", headers=headers_for(user)).json()
    assert body["blockers"] == [], "a healthy job must not be painted as a problem"
    assert [d["code"] for d in body["advisories"]] == ["no_demand_source"]
    assert body["lines"] == []

    # ...and at opt-in scope the SAME condition really does refuse.
    assert_refused(client, db_session, user, fg, code="no_demand_source")

    # The completion path must not treat it as structural either: nothing refused, nothing
    # recorded, nothing issued.
    armed = make_part(db_session, backflush=True)
    armed_wo = make_wo(db_session, armed, quantity_ordered=4, quantity_complete=4)
    make_op(db_session, armed_wo, make_work_center(db_session), quantity_complete=4)
    run_effects(db_session, armed_wo, user)
    db_session.expire_all()

    assert refused_rows(db_session) == [], "an advisory must never reach the structural refusal tier"
    assert backflush_issue_rows(db_session, armed_wo) == []


def test_the_preview_flags_a_pinned_lot_that_went_on_hold_after_it_was_pinned(client: TestClient, db_session: Session):
    """The single most consequential thing a pre-completion dry run can say.

    The tie endpoint refuses to PIN a held lot, so the only way this state arises is QA
    quarantining a heat AFTER the pin — and at that point the writer consumes it anyway
    (refusing from a reconcile-on-read GET would be unattributable) and records
    ``HELD_MATERIAL_CONSUMED``. The draw is not SHORT, so no shortage disclosure runs and
    ``held_quantity_skipped`` stays zero by design; without a dedicated flag the dry run
    showed a clean pinned line over quarantined material about to go into product.

    A second, available pin on the same request is the negative control: without it a flag
    hard-wired to ``True`` would satisfy every assertion here.
    """
    user = make_user(db_session)
    fg = make_part(db_session)
    quarantined = make_part(db_session, part_type="purchased")
    heat = make_lot(db_session, quarantined, qty=100, lot="HEAT-Q", received_date=days(1))
    healthy = make_part(db_session, part_type="purchased")
    good_heat = make_lot(db_session, healthy, qty=100, lot="HEAT-OK", received_date=days(1))

    wo = make_wo(db_session, fg, quantity_ordered=5, quantity_complete=5)
    make_op(db_session, wo, make_work_center(db_session), quantity_complete=5)
    tie(db_session, wo, quarantined, qty_planned=5.0, pinned=heat)
    tie(db_session, wo, healthy, qty_planned=3.0, pinned=good_heat)

    heat.status = "on_hold"
    db_session.commit()

    body = client.get(f"/api/v1/work-orders/{wo.id}/backflush-preview", headers=headers_for(user)).json()
    held_line = next(row for row in body["lines"] if row["component_part_id"] == quarantined.id)
    assert held_line["pinned_lot_is_held"] is True
    assert held_line["pinned_lot_number"] == "HEAT-Q"
    assert [(lot["lot_number"], lot["quantity"]) for lot in held_line["lots"]] == [("HEAT-Q", 5.0)]
    assert held_line["held_quantity_skipped"] == 0.0, "not skipped — DRAWN, which is exactly why it needs its own flag"
    assert held_line["shortfall"] == 0.0

    clean_line = next(row for row in body["lines"] if row["component_part_id"] == healthy.id)
    assert clean_line["pinned_lot_is_held"] is False, "the flag must describe the lot, not every pinned line"

    run_effects(db_session, db_session.get(WorkOrder, wo.id), user)
    db_session.expire_all()

    drawn = [(row.lot_number, -float(row.quantity)) for row in backflush_issue_rows(db_session, wo, quarantined)]
    assert drawn == [("HEAT-Q", 5.0)], "the warning describes something that really happens"
    held_rows = (
        db_session.query(AuditLog)
        .filter(AuditLog.company_id == COMPANY_A, AuditLog.action == HELD_MATERIAL_CONSUMED_AUDIT_ACTION)
        .all()
    )
    assert len(held_rows) == 1
    assert_on_the_tamper_evident_chain(db_session, held_rows[0])


def test_arming_through_the_materials_door_is_recorded_under_resource_type_part(
    client: TestClient, db_session: Session
):
    """One table's control-change trail must be ONE query.

    ``PUT /materials/{id}`` and ``PUT /parts/{id}`` write the same ``parts`` row through
    the same schema and the same shared gate. Logging the flip under
    ``resource_type='material'`` on one of them meant an auditor asking "who armed
    automatic stock movement, and when" — filtering ``resource_type='part'``, which is
    what the table is — silently missed every flip made through the materials URL. Not a
    cosmetic split: the query returns fewer rows and says nothing about it.

    The recipe the compliance doc now publishes is asserted end-to-end, and the OLD
    resource type is asserted EMPTY, because a fix that merely added a second row would
    also satisfy "the recipe finds it".
    """
    user = make_user(db_session)
    material, _component = clean_backflush_part(db_session, part_type="raw_material", uom="sheets")

    response = enable_backflush(client, user, material.id, path="materials")
    assert response.status_code == status.HTTP_200_OK, response.text

    assert (
        db_session.query(AuditLog)
        .filter(
            AuditLog.company_id == COMPANY_A,
            AuditLog.resource_type == "material",
            AuditLog.resource_id == material.id,
            AuditLog.action == "UPDATE",
        )
        .count()
        == 0
    ), "an arming row left under the old type is a row the auditor's single query never sees"

    # The published recipe: resource_type='part' AND action='UPDATE' AND a readiness verdict.
    armed = [
        row
        for row in db_session.query(AuditLog)
        .filter(
            AuditLog.company_id == COMPANY_A,
            AuditLog.resource_type == "part",
            AuditLog.action == "UPDATE",
        )
        .all()
        if (row.extra_data or {}).get("backflush_readiness")
    ]
    assert len(armed) == 1
    assert armed[0].resource_id == material.id
    assert (armed[0].extra_data or {})["backflush_components"] is True
    assert (armed[0].extra_data or {})["backflush_readiness"] == "clean"
    assert_on_the_tamper_evident_chain(db_session, armed[0])


def refused_events(db: Session, *, company_id: int = COMPANY_A) -> list[OperationalEvent]:
    """Every ``backflush_demand_refused`` event — one per refused SCOPE, not per diagnostic."""
    return (
        db.query(OperationalEvent)
        .filter(
            OperationalEvent.company_id == company_id,
            OperationalEvent.event_type == BACKFLUSH_DEMAND_REFUSED_EVENT_TYPE,
        )
        .order_by(OperationalEvent.id)
        .all()
    )


def two_blocking_conditions_on_one_line(db: Session) -> tuple[WorkOrder, Part]:
    """One BOM line that raises TWO blocking diagnostics naming ONE component.

    ``quantity=0`` raises ``zero_bom_quantity`` and the line's ``unit_of_measure`` raises
    ``unit_of_measure_mismatch`` against a part stocked in ``sheets`` — the exact pair
    ``_record_bom_line_diagnostics`` can emit for a single line, and the pair that made the
    attribution bug reachable.

    The quantity is the subtle half: ``_explode_backflush_bom`` extends by
    ``float(item.quantity or 1)``, so a stored ``0`` is coerced to ONE PER PARENT UNIT.
    The line therefore carries REAL demand (1 x 5 produced) while being doubly defective,
    which is what makes "how much did not move" a number that can be got wrong.
    """
    fg = make_part(db, backflush=True)
    drifted = make_part(db, part_type="purchased", uom="sheets")
    make_lot(db, drifted, qty=100, received_date=days(1))
    add_bom_item(db, make_bom(db, fg), drifted, quantity=0, item_number=10, unit_of_measure="each")

    wo = make_wo(db, fg, quantity_ordered=5, quantity_complete=5)
    make_op(db, wo, make_work_center(db), quantity_complete=5)
    return wo, drifted


def test_two_blocking_diagnostics_on_one_component_refuse_one_quantity_not_two(db_session: Session):
    """**A refusal is attributed once per refused SCOPE, never once per row.**

    ``_record_bom_line_diagnostics`` can raise several blocking conditions for a single
    BOM line, and two different lines can name one component. Each condition is a separate
    thing to fix, so each earns its own ``BACKFLUSH_DEMAND_REFUSED`` row — but they
    describe ONE quantity that did not move. Charging every row the component's full
    demand puts a FALSE FIGURE on the tamper-evident chain: an auditor summing the action
    would read double what actually failed to move, and the hash chain would be internally
    consistent while saying something untrue about the inventory it exists to explain.

    This module already refuses to do that elsewhere — ``_record_backflush_demand_suppressed``
    records a tie's UNMET REMAINDER rather than its gross ``qty_planned`` for the same
    reason — and the structural tier had the guard from the start (``structural[0]`` owns
    the total). The per-component tier did not.

    The figure is asserted against the PURE resolver rather than against a literal, so the
    test states the real relationship: what the chain says was refused is what the engine
    would otherwise have issued.
    """
    user = make_user(db_session)
    wo, drifted = two_blocking_conditions_on_one_line(db_session)

    resolution = _resolve_backflush_demand(db_session, wo, COMPANY_A)
    assert resolution.demand == {drifted.id: 5.0}, "one per parent unit x 5 produced — the demand under refusal"
    assert (
        len([d for d in resolution.diagnostics if d.severity == BACKFLUSH_BLOCKING]) == 2
    ), "the fixture must really raise TWO blocking diagnostics, or this test proves nothing"

    run_effects(db_session, wo, user)
    db_session.expire_all()

    assert backflush_issue_rows(db_session, wo) == [], "the component is refused, so nothing posts"
    assert on_hand(db_session, drifted) == 100.0, "5.0 is exactly what did not move"

    rows = refused_rows(db_session)
    assert len(rows) == 2, "one row per condition — each names a different line-level defect to fix"
    assert {(row.extra_data or {})["diagnostic_code"] for row in rows} == {
        "zero_bom_quantity",
        "unit_of_measure_mismatch",
    }
    quantities = sorted(float((row.extra_data or {})["refused_quantity"]) for row in rows)
    assert quantities == [0.0, 5.0], "the FIRST row naming the component owns the quantity; the rest carry 0"
    assert sum(quantities) == 5.0, "the summed action must equal the stock that did not move, not double it"

    for row in rows:
        extra = row.extra_data or {}
        assert extra["component_part_id"] == drifted.id, "both rows still name the component — only the figure moves"
        assert extra["refused_whole_leg"] is False
        assert extra["bom_item_id"] is not None
        assert row.resource_type == "inventory"
        assert float((row.new_values or {})["refused_quantity"]) == float(extra["refused_quantity"])
        assert_on_the_tamper_evident_chain(db_session, row)

    assert (
        len(refused_events(db_session)) == 1
    ), "one refused component is ONE signal — a line violating two rules must not notify twice"


def test_a_refusal_reaches_the_notification_outbox_under_its_own_catalog_key(db_session: Session, monkeypatch):
    """**The refusal must not be quieter than the conditions it is worse than.**

    A shortage (``material.backflush_shortage``) still moves stock and notifies; a
    rolled-back draw (``material.backflush_failed``) moves none and notifies. A REFUSAL
    also moves none — and it is the least self-correcting of the three: it fires at
    completion on a part that is ALREADY armed, nothing disarms it, and so the same
    component silently under-issues on every subsequent job until somebody fixes the BOM
    line the diagnostic names. An audit row is a record; it is not a signal.

    Both ends are asserted, and neither alone would do. A registry-only assertion passes
    even if the emit is dead; an event-only assertion passes even if the outbox drops the
    type (which is exactly how ``backflush_shortage`` went nowhere from Batch 6 until PR
    4.4). So this drives a REAL refusal, looks up the type that was actually emitted, and
    asserts the transactional outbox actually routed a ``dispatch_notification_job`` for
    it — ``OperationalEventService.emit`` marks ONLY catalog-mapped types on
    ``Session.info["pending_notification_event_ids"]``, so the enqueue is the
    machine-checkable form of "this event will reach a human".

    The enqueue is observed at ``notification_outbox._enqueue_dispatch`` rather than by
    reading the pending list, and that is not a stylistic choice. SQLAlchemy fires
    ``after_commit`` on a SAVEPOINT release as well as on a real commit, and the emit runs
    inside its own ``begin_nested`` (see ``_emit_demand_refused_event``), so the tee drains
    the pending list at ``savepoint.commit()`` — before the caller's commit. Any assertion
    against the list would read empty at every point a test can look. Spying the routing
    function is also the stronger assertion: it is the thing the worker acts on.
    """
    enqueued: list[int] = []
    monkeypatch.setattr(notification_outbox, "_enqueue_dispatch", lambda event_id: enqueued.append(event_id))

    user = make_user(db_session)
    fg = make_part(db_session, backflush=True)
    drifted = make_part(db_session, part_type="purchased", uom="sheets")
    make_lot(db_session, drifted, qty=100, received_date=days(1))
    add_bom_item(db_session, make_bom(db_session, fg), drifted, quantity=2, unit_of_measure="each")

    wo = make_wo(db_session, fg, quantity_ordered=5, quantity_complete=5)
    make_op(db_session, wo, make_work_center(db_session), quantity_complete=5)

    run_effects(db_session, wo, user)
    db_session.expire_all()

    assert backflush_issue_rows(db_session, wo) == [], "the refusal really happened"
    assert len(refused_rows(db_session)) == 1

    events = refused_events(db_session)
    assert len(events) == 1, "a refusal must reach the outbox, not only the audit log"
    event = events[0]
    assert event.severity == "warning"
    assert event.work_order_id == wo.id
    assert event.entity_type == "inventory", "keyed like the chain row: the component, when one is named"
    assert event.entity_id == drifted.id
    assert event.user_id == user.id
    payload = event.event_payload or {}
    assert payload["component_part_id"] == drifted.id
    assert payload["refused_quantity"] == 10.0, "the quantity that did not move travels with the signal"
    assert payload["diagnostic_code"] == "unit_of_measure_mismatch"
    assert payload["refused_whole_leg"] is False
    assert payload["bom_item_id"] is not None, "the signal names the line to fix"

    assert event.id in enqueued, "the outbox tee never routed it, so dispatch_notification_job never runs"

    entry = entry_for_event_type(event.event_type)
    assert entry is not None, "an uncataloged type is silently ignored by the outbox"
    assert entry.event_key == "material.backflush_demand_refused"
    assert entry.severity == "warning"
    assert {CHANNEL_IN_APP, CHANNEL_EMAIL} <= set(entry.default_channels)
    assert should_fire(entry, event) is True, "no transition gate stands between this event and its recipients"

    # One key per condition, so the settings matrix can gate them apart and Purchasing can
    # tell "stock went negative" from "stock never moved" from "we declined to move it".
    assert SOURCE_EVENT_TYPE_TO_KEY[BACKFLUSH_DEMAND_REFUSED_EVENT_TYPE] == "material.backflush_demand_refused"
    assert SOURCE_EVENT_TYPE_TO_KEY[BACKFLUSH_SHORTAGE_EVENT_TYPE] != entry.event_key
    assert SOURCE_EVENT_TYPE_TO_KEY[BACKFLUSH_COMPONENT_FAILED_EVENT_TYPE] != entry.event_key
    assert (
        db_session.query(OperationalEvent)
        .filter(
            OperationalEvent.company_id == COMPANY_A,
            OperationalEvent.event_type.in_([BACKFLUSH_SHORTAGE_EVENT_TYPE, BACKFLUSH_COMPONENT_FAILED_EVENT_TYPE]),
        )
        .count()
        == 0
    ), "a refusal is not a shortage and not a rollback — mis-signalling it sends the wrong remedy"


def test_a_structural_refusal_notifies_once_for_the_whole_leg(db_session: Session):
    """The structural tier's half of the same rule.

    A whole-leg refusal already attributed its quantity to the first structural row; the
    event follows the same gate, so a job whose BOM raises several structural blockers
    produces several chain rows (each a distinct defect) and ONE notification keyed to the
    JOB rather than to any component — there is no component to key it to, which is the
    definition of the structural tier.
    """
    user = make_user(db_session)
    fg = make_part(db_session, backflush=True)
    component = make_part(db_session, part_type="purchased")
    make_lot(db_session, component, qty=100, received_date=days(1))
    add_bom_item(db_session, make_bom(db_session, fg, is_deleted=True), component, quantity=2)

    wo = make_wo(db_session, fg, quantity_ordered=5, quantity_complete=5)
    make_op(db_session, wo, make_work_center(db_session), quantity_complete=5)

    run_effects(db_session, wo, user)
    db_session.expire_all()

    events = refused_events(db_session)
    assert len(events) == 1
    event = events[0]
    assert event.entity_type == "work_order", "a structural blocker is a fact about the JOB"
    assert event.entity_id == wo.id
    payload = event.event_payload or {}
    assert payload["refused_whole_leg"] is True
    assert payload["refused_quantity"] == 10.0
    assert payload["component_part_id"] is None
    assert entry_for_event_type(event.event_type).event_key == "material.backflush_demand_refused"
