"""Inventory side-effects of work-order completion (Batch 6 / rank 9).

When a work order reaches ``WorkOrderStatus.COMPLETE`` the produced quantity must
become on-hand inventory (a finished-good RECEIPT), and -- when the finished part
is configured for it -- its BOM components must be consumed from stock (a
backflush ISSUE). Both legs write the same tamper-evident ``audit_log`` chain the
manual inventory endpoints use (INV-4) and reference the work order
(``reference_type='work_order'``).

As-built genealogy (INV-3 / TRACE-2 / TRACE-5) is NOT automatic from the FG-lot
trace alone: the FG-receipt RECEIVE txn carries the *finished-good* lot while the
component ISSUE txns carry the *component* lots, so a lot-keyed ``trace_lot`` of the
FG lot surfaces only the producing work order, not the consumed component lots. The
genealogy second hop -- FG lot -> producing WO (RECEIVE ``reference_id``) -> that
WO's component ISSUE txns -> consumed component part/lot/qty -- is reconstructed in
``trace_lot`` (``api/endpoints/traceability.py``), which enumerates a
``consumed_components`` section for work-order-produced lots.

Design rules (these functions are I/O-light but DB-mutating):

* **No commit.** Every function joins the CALLER's unit of work; the completion
  handler owns ``db.commit()`` so the inventory writes land ATOMICALLY with the
  status change on the live paths. (The reconcile-on-read caller commits too, but
  best-effort -- see the read-safe wrapper there.)
* **Idempotent.** Re-entry (reconcile re-read, re-completion of an already-terminal
  WO, a retried request) must never double-receive or double-issue. The idempotency
  key is the existence of a prior work-order-referencing ``InventoryTransaction``
  for the same company:
    - FG receipt: ANY ``RECEIVE`` txn with ``reference_type='work_order',
      reference_id=work_order.id, company_id`` -> already received, no-op.
    - Backflush: per component part, ANY ``ISSUE`` txn with
      ``reference_type='work_order', reference_id=work_order.id, part_id=<component>,
      company_id`` -> that component is already issued, skip it.
* **Tenant-scoped.** ``company_id`` is stamped on every row and every lookup filters
  it (invariant #1). The caller passes the ACTIVE company.
* **Audited.** Each new ``InventoryTransaction`` is logged via ``AuditService``
  (mirrors ``receiving.py`` / ``inventory.py``) so stock movement lands on the hash
  chain, not just the AI ``OperationalEvent`` store.

Lot-only (no serialization flag exists yet): on FG receipt we assign
``work_order.lot_number`` if empty (a per-company-unique lot derived from the WO
number) and leave ``InventoryItem.serial_number`` NULL. Serial assignment is a
tracked follow-up.

**The BOM/routing backflush leg is DARK.** ``Part.backflush_components`` has no writer
anywhere in ``app/`` -- no schema field, no endpoint, no UI, ``server_default="false"`` --
so ``_resolve_backflush_components`` and everything it calls have never executed against
production data. Read that as a licence to fix them and as a warning not to assume any of
it was ever right: the demand resolution shipped treating a whole-job component quantity
as a per-unit rate, summing one component's demand once per operation that touched it,
letting one routed operation cancel an entire BOM explosion, consuming both a
sub-assembly and its raw material, and reading neither ``is_alternate`` nor
``is_optional`` nor reference lines. ``_issue_one_component`` and
``_component_already_issued`` below are the OPPOSITE case -- work-order-scoped material
ties drive them today, so their behavior (including a lot selection that genuinely
contradicts the per-run engine's FIFO) is live and is deliberately left alone.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Optional

from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.db.ledger_filter import LEDGER_QUANTITY_EPSILON, OPERATION_REFERENCE_TYPE, WORK_ORDER_REFERENCE_TYPE
from app.models.bom import BOM, BOMItem, BOMItemType, BOMLineType
from app.models.inventory import InventoryItem, InventoryTransaction, TransactionType
from app.models.operational_event import OperationalEvent  # noqa: F401  (imported for type/test discoverability)
from app.models.part import Part
from app.models.shipping import Shipment, ShipmentStatus
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation
from app.models.work_order_material import WorkOrderMaterialAllocation
from app.services.audit_service import AuditService
from app.services.operational_event_service import OperationalEventService

logger = logging.getLogger(__name__)

# Tamper-evident audit action + operational-event type for a backflush shortage
# (a component driven negative on-hand). A silent negative stock is a material-trail
# control gap in a regulated (AS9100D/CMMC-L2) system, so a shortage is recorded on
# the hash chain AND emitted as a warning OperationalEvent (item 3).
BACKFLUSH_SHORTAGE_AUDIT_ACTION = "BACKFLUSH_SHORTAGE"
BACKFLUSH_SHORTAGE_EVENT_TYPE = "backflush_shortage"

# Tamper-evident audit action for a BOM/routing backflush demand line that was SUPPRESSED
# because the ledger already shows that part leaving stock against this work order under
# the OPERATION reference shape (tied consumption). Recorded rather than left silent: the
# suppression is the system declining to issue material a planner's BOM asked for, and
# "correct but invisible" is exactly the shape of control gap an AS9100D 8.5.2 as-built
# review cannot reconstruct after the fact. See ``_drop_ledger_covered_parts``.
BACKFLUSH_DOUBLE_ISSUE_BLOCKED_AUDIT_ACTION = "BACKFLUSH_DOUBLE_ISSUE_BLOCKED"

# Tamper-evident audit actions + operational-event types for ship-side discrepancies
# (G2). A ship with no matching finished-good lot row (receipt skipped / lot changed /
# stock already moved) and a ship that drives cumulative shipped beyond what was
# produced are both regulated material-trail control gaps. Each is recorded on the hash
# chain AND emitted as a warning OperationalEvent -- but NEITHER fails the ship/close
# (mirrors the warn-and-record backflush-shortage posture).
SHIP_FG_MISSING_AUDIT_ACTION = "SHIP_FG_LOT_MISSING"
SHIP_FG_MISSING_EVENT_TYPE = "ship_fg_lot_missing"
OVER_SHIP_AUDIT_ACTION = "OVER_SHIP"
OVER_SHIP_EVENT_TYPE = "over_ship"

# ONE float-comparison epsilon for the whole completion-inventory surface (this module
# and ``material_consumption_service``, which re-exports it as ``CONSUMPTION_EPSILON``).
# Quantities are ``Float`` columns, so "is this quantity zero?" must never be an exact
# ``== 0`` / ``> 0`` test: a target computed as ``0.1 * 3`` is not ``0.3``. Previously
# three different spellings were in use -- a bare ``1e-9`` here, ``_EPSILON`` in the
# consumption engine, and plain ``<= 0`` / ``> 0`` on the tie legs -- so the same
# near-zero delta could be "zero" in one guard and "positive" in the next.
#
# The DEFINITION now lives in ``app/db/ledger_filter.py``, alongside the ledger predicate
# it is used with. This module keeps the private name so its own call sites read
# unchanged, but it is an alias, not a source: a read-only genealogy endpoint should not
# have to import the consumption engine to learn what "zero" means.
_EPSILON = LEDGER_QUANTITY_EPSILON


def _insert_txn_with_savepoint(db: Session, txn: InventoryTransaction) -> bool:
    """Insert one ``InventoryTransaction`` inside a SAVEPOINT, returning success.

    The work-order RECEIVE / ISSUE keys carry a partial UNIQUE index (added by the
    migration specialist); a concurrent second insert of the same key (the
    double-receive / double-issue race) raises ``IntegrityError`` on flush. We wrap
    the INSERT in ``db.begin_nested()`` so that on collision we roll back ONLY the
    savepoint (not the outer completion / reconcile unit of work) and treat it as a
    clean no-op -- the other transaction already wrote the row.

    Returns ``True`` when the row was actually inserted (caller may now mutate the
    on-hand quantity), ``False`` when it was a duplicate no-op (caller must NOT
    mutate on-hand, or it would double-count against the winning transaction's row).

    Crucially this keeps the OUTER transaction usable on BOTH paths: the live paths
    stay atomic with the completion, and on the reconcile path a duplicate insert
    can never abort the whole reconcile (only the savepoint is rolled back, so the
    status transition still commits).
    """
    nested = db.begin_nested()
    try:
        db.add(txn)
        db.flush()
    except IntegrityError:
        nested.rollback()
        return False
    return True


# Finished-goods receipt location. A module constant (rather than a Part field) so it
# is configurable in one place; the warehouse mirrors InventoryItem's MAIN default.
FINISHED_GOODS_WAREHOUSE = "MAIN"
FINISHED_GOODS_LOCATION = "FINISHED-GOODS"


@dataclass
class ComponentShortage:
    """A backflush ISSUE that drove (or would have driven) a source lot negative.

    Recorded but NOT fatal: a shortage must never fail a completion (negative
    on-hand is the existing system's behavior -- the manual ``/inventory/adjust``
    path also permits it). Surfaced so the caller can log / report it.
    """

    part_id: int
    part_number: Optional[str]
    required_quantity: float
    available_quantity: float
    shortfall: float


@dataclass
class WorkOrderMaterialAllocationDemand:
    """Summed one-shot demand a part carries from WORK-ORDER-scoped material ties.

    ``allocation_ids`` are every tie that contributed, so a single emitted ISSUE can
    mark all of them consumed (``uq_wo_inventory_issue`` allows only one ISSUE row per
    (company, WO, part), so the ledger row carries the FIRST tie's ``allocation_id``
    and the rest are reconciled through their ``qty_consumed``).

    ``pinned_inventory_item_id`` is the tie's LOT PIN, carried through to the ISSUE so a
    work-order-scoped tie honors its pin exactly as the operation-scoped engine does.
    Dropping it here (the shape this dataclass originally had) meant a planner could pin
    a heat-certified lot, get a 201, and watch the ledger issue from a different lot --
    the as-built genealogy naming material the operator never touched (AS9100D 8.5.2).
    ``uq_wo_material_alloc_open_wo`` permits at most ONE open work-order-scoped tie per
    (company, WO, part), and the demand builder reads only OPEN rows -- so there is never
    a second, conflicting pin to reconcile. The summing branch below is unreachable while
    that partial unique index holds; it is kept as a defensive sum (and keeps the FIRST
    pin) rather than as a path anything exercises today.
    """

    allocation_ids: list[int] = field(default_factory=list)
    quantity: float = 0.0
    pinned_inventory_item_id: Optional[int] = None


@dataclass
class IssueOutcome:
    """What one ``_issue_one_component`` call actually did.

    ``posted`` distinguishes "the ISSUE row landed" from "a concurrent completion had
    already written it, so this call inserted nothing" -- two cases the old
    ``Optional[ComponentShortage]`` return collapsed into ``None``. The caller must not
    advance a tie's ``qty_consumed`` cache on the second one: the winning transaction's
    rows own that quantity, and the cache is the exact field the untie endpoint refuses
    against (409 once ``qty_consumed > 0``). Its operation-scoped twin has always made
    this distinction (``posted_any`` in ``_consume_one_allocation``).
    """

    posted: bool = False
    shortage: Optional[ComponentShortage] = None


@dataclass
class BackflushResult:
    issued_part_ids: list[int] = field(default_factory=list)
    shortages: list[ComponentShortage] = field(default_factory=list)


def _existing_work_order_receipt(db: Session, work_order_id: int, company_id: int) -> bool:
    """True if a finished-good RECEIVE for this WO already exists (idempotency key)."""
    return (
        db.query(InventoryTransaction.id)
        .filter(
            InventoryTransaction.company_id == company_id,
            InventoryTransaction.reference_type == "work_order",
            InventoryTransaction.reference_id == work_order_id,
            InventoryTransaction.transaction_type == TransactionType.RECEIVE,
        )
        .first()
        is not None
    )


def _component_already_issued(db: Session, work_order_id: int, component_part_id: int, company_id: int) -> bool:
    """True if this component was already backflushed for this WO (idempotency key)."""
    return (
        db.query(InventoryTransaction.id)
        .filter(
            InventoryTransaction.company_id == company_id,
            InventoryTransaction.reference_type == "work_order",
            InventoryTransaction.reference_id == work_order_id,
            InventoryTransaction.transaction_type == TransactionType.ISSUE,
            InventoryTransaction.part_id == component_part_id,
        )
        .first()
        is not None
    )


def _assign_finished_good_lot(db: Session, work_order: WorkOrder, company_id: int) -> str:
    """Return ``work_order.lot_number``, assigning a per-company-unique lot if empty.

    The lot is derived from the WO number (``LOT-<wo_number>``) and de-collided with
    a ``-NN`` suffix within the company if a same-named lot already exists on an
    InventoryItem. Idempotent: an already-assigned lot is returned untouched.
    """
    if work_order.lot_number:
        return work_order.lot_number

    base = f"LOT-{work_order.work_order_number}"
    candidate = base
    suffix = 1
    while (
        db.query(InventoryItem.id)
        .filter(InventoryItem.company_id == company_id, InventoryItem.lot_number == candidate)
        .first()
        is not None
    ):
        suffix += 1
        candidate = f"{base}-{suffix}"
    work_order.lot_number = candidate
    return candidate


def receive_finished_goods_for_work_order(
    db: Session,
    work_order: WorkOrder,
    *,
    user_id: int,
    company_id: int,
    audit: AuditService,
) -> Optional[InventoryTransaction]:
    """Receive a completed WO's output into finished-goods inventory (INV-1 / TRACE-3).

    Creates or increments an ``InventoryItem`` for ``work_order.part_id`` at the FG
    location and writes a positive ``RECEIVE`` ``InventoryTransaction`` referencing
    the work order, with the assigned finished-good lot and ``standard_cost`` unit
    cost. Idempotent (skips if a WO RECEIVE already exists) so reconcile re-entry /
    re-completion can't double-receive. Does NOT commit -- the caller owns the
    transaction so the receipt is atomic with the completion.

    Returns the created transaction, or ``None`` when it was a no-op (already
    received, or nothing to receive).
    """
    if _existing_work_order_receipt(db, work_order.id, company_id):
        return None

    quantity = float(work_order.quantity_complete or 0)
    if quantity <= 0:
        # Nothing produced (e.g. fully scrapped) -- no finished good to receive.
        return None

    part = db.query(Part).filter(Part.id == work_order.part_id, Part.company_id == company_id).first()
    if part is None:
        logger.warning(
            "FG receipt skipped: part %s not found for WO %s (company %s)",
            work_order.part_id,
            work_order.id,
            company_id,
        )
        return None

    lot_number = _assign_finished_good_lot(db, work_order, company_id)
    unit_cost = float(part.standard_cost or 0)

    # Match on part + location + lot + company (create if none) so a re-run after a
    # partial completion would aggregate onto the same FG lot row.
    inv_item = (
        db.query(InventoryItem)
        .filter(
            InventoryItem.company_id == company_id,
            InventoryItem.part_id == work_order.part_id,
            InventoryItem.location == FINISHED_GOODS_LOCATION,
            InventoryItem.lot_number == lot_number,
        )
        .first()
    )

    # Order matters (item 1 / BLOCKER companion): create/flush the InventoryItem row
    # (at its EXISTING quantity -- no increment yet) so the RECEIVE txn has an
    # ``inventory_item_id`` to reference, then insert the txn FIRST under a savepoint.
    # Only if that insert actually committed to the savepoint do we increment on-hand;
    # a duplicate (already-received race the unique index catches) is a clean no-op and
    # must NOT mutate the quantity, or it would double the on-hand vs. the winner's row.
    old_quantity_on_hand = inv_item.quantity_on_hand if inv_item else None
    if inv_item is None:
        inv_item = InventoryItem(
            part_id=work_order.part_id,
            location=FINISHED_GOODS_LOCATION,
            warehouse=FINISHED_GOODS_WAREHOUSE,
            quantity_on_hand=0.0,
            quantity_allocated=0.0,
            quantity_available=0.0,
            lot_number=lot_number,
            unit_cost=unit_cost,
            received_date=datetime.utcnow(),
            status="available",
        )
        inv_item.company_id = company_id
        db.add(inv_item)
        db.flush()

    txn = InventoryTransaction(
        company_id=company_id,
        inventory_item_id=inv_item.id,
        part_id=work_order.part_id,
        transaction_type=TransactionType.RECEIVE,
        quantity=quantity,
        to_location=FINISHED_GOODS_LOCATION,
        lot_number=lot_number,
        reference_type="work_order",
        reference_id=work_order.id,
        reference_number=work_order.work_order_number,
        unit_cost=unit_cost,
        total_cost=quantity * unit_cost,
        notes=f"Finished-goods receipt from work order {work_order.work_order_number}",
        created_by=user_id,
    )
    if not _insert_txn_with_savepoint(db, txn):
        # A concurrent RECEIVE already wrote this WO's finished-good receipt (the unique
        # index fired). Treat as an idempotent no-op: do NOT increment on-hand (the
        # winning txn's row owns the quantity) and leave any freshly-created empty item
        # row at zero. The outer transaction stays usable on both live and reconcile.
        return None

    # The RECEIVE insert succeeded -> NOW apply the on-hand increment.
    inv_item.quantity_on_hand = float(inv_item.quantity_on_hand or 0) + quantity
    inv_item.quantity_available = inv_item.quantity_on_hand - float(inv_item.quantity_allocated or 0)
    if not inv_item.unit_cost:
        inv_item.unit_cost = unit_cost
    db.flush()

    # Tamper-evident audit (hash chain) for the stock movement (INV-4). Flushed inside
    # the caller's unit of work so the audit row commits with the inventory write.
    audit.log_create(
        "inventory",
        txn.id,
        str(txn.id),
        new_values=txn,
        description=(
            f"Received {quantity} of part {part.part_number} into {FINISHED_GOODS_LOCATION} "
            f"lot {lot_number} from work order {work_order.work_order_number}"
        ),
    )
    if old_quantity_on_hand is not None:
        audit.log_update(
            "inventory",
            inv_item.id,
            f"{part.part_number} @ {FINISHED_GOODS_LOCATION}",
            old_values={"quantity_on_hand": old_quantity_on_hand},
            new_values={"quantity_on_hand": inv_item.quantity_on_hand},
            description=f"FG receipt: stock for part {part.part_number} at {FINISHED_GOODS_LOCATION}",
        )

    return txn


def _open_allocations(
    db: Session,
    work_order: WorkOrder,
    company_id: int,
    allocations: Optional[list[WorkOrderMaterialAllocation]],
) -> list[WorkOrderMaterialAllocation]:
    """The OPEN ties on this work order -- the caller's pre-fetched list, or one SELECT.

    ``apply_completion_inventory_effects`` reads them ONCE and threads the list through
    the consume leg, the backflush-precedence drop and the work-order-scoped demand,
    which used to issue the same tenant-scoped SELECT up to three times per completion.
    The parameter stays optional so the individual legs remain independently callable
    (tests and any future call site) without a behavior change.
    """
    if allocations is not None:
        return allocations
    from app.services.material_consumption_service import open_allocations_for_work_order

    return open_allocations_for_work_order(db, work_order.id, company_id)


@dataclass
class _BackflushBomExplosion:
    """What the BOM explosion says about a work order's component demand.

    ``demand`` is the quantity to ISSUE per part. ``excluded_part_ids`` is every part the
    explosion walked past WITHOUT giving it demand -- an alternate/optional/reference
    line, a phantom that was exploded through, or any part inside a ``make``
    sub-assembly's own subtree. That set is not bookkeeping: the routing leg's
    ``component_part_id`` values are generated from ``_collect_bom_components``, which
    applies NONE of these rules and DOES recurse into ``make``, so without the exclusion
    set the routing would re-introduce through the back door exactly the lines the
    explosion just declined to issue.
    """

    demand: dict[int, float] = field(default_factory=dict)
    excluded_part_ids: set[int] = field(default_factory=set)


def _is_non_consumed_bom_line(item: BOMItem) -> bool:
    """True when a BOM line states demand that a backflush must NOT issue.

    Three families, all of which the backflush read NOWHERE before this and all of which
    fail in the expensive direction -- material issued into an as-built record that the
    job never consumed (AS9100D 8.5.2):

    * **``is_alternate``** -- a member of an alternate group. The group is an OR, not an
      AND: issuing every member multiplies the group's demand by its size. ``mrp_service``
      has always skipped alternates (``explode_bom_for_mrp``); the backflush now agrees,
      so planning and consumption cannot state different demand for one BOM.
    * **``is_optional``** -- present on some units and not others, with nothing on the
      work order recording which. Nothing here can know, so it issues nothing.
    * **``line_type == reference``** -- documentation and tooling. The enum's own comment
      is "Reference only - not consumed".

    Under-issuing is the safe direction and is not silent: the material is still on the
    shelf, so the operator who needs it draws it manually, and the job's cost shows the
    gap. Over-issuing writes material into a genealogy record that never contained it,
    which no downstream reader can distinguish from the truth.
    """
    if bool(item.is_alternate) or bool(item.is_optional):
        return True
    return (item.line_type or "").lower() == BOMLineType.REFERENCE.value


def _explode_backflush_bom(
    db: Session,
    bom: BOM,
    company_id: int,
    *,
    parent_qty: float,
    visited_part_ids: set[int],
    out: _BackflushBomExplosion,
    consumed: bool,
) -> None:
    """Explode a BOM with BACKFLUSH semantics: phantoms open up, ``make`` items do not.

    Deliberately NOT ``_collect_bom_components``. That helper appends a ``make`` /
    ``phantom`` sub-assembly AND recurses into its children, producing a flat list in
    which the assembly and its raw material both appear -- correct for the four callers
    that want "every part anywhere in this structure" (routing generation, cost estimate,
    MRP-ish reads), and a double-consume for a backflush. It also has three callers
    outside this module whose behavior must not change inside a backflush PR, and its
    flat output cannot express the make/phantom distinction at all (a line's parent is
    not recoverable from it). So the traversal is re-stated here, with the same
    ``item_number, id`` ordering, the same ``quantity x parent_qty x (1 + scrap_factor)``
    extension and the same visited-set cycle guard, and a different rule at each node:

    * **phantom** -- the assembly is a planning fiction that is never stocked, so it is
      EXCLUDED and its children are exploded in its place at the phantom's extended
      quantity. A phantom with no active BOM has nothing to explode into; it is treated
      as a stocked line instead (and logged), because dropping it would make the line
      vanish with no ledger row, no shortage and no signal of any kind.
    * **make** -- a stocked unit. The assembly is issued; its children are NOT, because
      they were consumed when IT was built and issuing both consumes the same material
      twice. Its subtree is still walked, in exclude-only mode, so the routing leg cannot
      issue those children either.
    * **buy** -- a stocked unit with no structure below it. Issued, never recursed into
      (matching ``_collect_bom_components``).

    ``consumed=False`` walks a subtree purely to collect ``excluded_part_ids`` and
    contributes no demand. Does not commit and writes nothing.
    """
    items = (
        db.query(BOMItem)
        .options(joinedload(BOMItem.component_part))
        .filter(BOMItem.bom_id == bom.id, BOMItem.company_id == company_id)
        .order_by(BOMItem.item_number.asc(), BOMItem.id.asc())
        .all()
    )
    for item in items:
        component = item.component_part
        if component is None or component.id in visited_part_ids:
            continue

        line_consumed = consumed and not _is_non_consumed_bom_line(item)
        extended = float(item.quantity or 1) * parent_qty * (1 + float(item.scrap_factor or 0))
        item_type = (item.item_type or "").lower()
        child_visited = visited_part_ids | {component.id}
        child_bom: Optional[BOM] = None
        if item_type != BOMItemType.BUY.value:
            # Imported lazily to avoid an import cycle with the endpoints module. One
            # definition of "the active BOM for a part" for the whole platform.
            from app.api.endpoints.work_orders import _get_active_bom

            child_bom = _get_active_bom(db, component.id, company_id)

        if item_type == BOMItemType.PHANTOM.value and child_bom is not None:
            out.excluded_part_ids.add(component.id)
            _explode_backflush_bom(
                db,
                child_bom,
                company_id,
                parent_qty=extended,
                visited_part_ids=child_visited,
                out=out,
                consumed=line_consumed,
            )
            continue
        if item_type == BOMItemType.PHANTOM.value:
            logger.warning(
                "BOM item %s on BOM %s (company %s) is a phantom with no active BOM to explode; "
                "backflushing part %s as a stocked line",
                item.id,
                bom.id,
                company_id,
                component.id,
            )

        if line_consumed:
            out.demand[component.id] = out.demand.get(component.id, 0.0) + extended
        else:
            out.excluded_part_ids.add(component.id)

        if child_bom is not None:
            # A stocked sub-assembly. Walk its subtree EXCLUDE-ONLY: its children were
            # consumed when it was built, and the walk exists so the routing leg -- which
            # was generated from a traversal that does recurse here -- cannot issue them.
            _explode_backflush_bom(
                db,
                child_bom,
                company_id,
                parent_qty=extended,
                visited_part_ids=child_visited,
                out=out,
                consumed=False,
            )


def _routing_backflush_demand(
    db: Session,
    work_order: WorkOrder,
    company_id: int,
    *,
    basis: float,
) -> dict[int, float]:
    """Component demand stated by the ROUTING (``component_part_id`` on an operation).

    Three things here are not what the previous shape of this leg assumed:

    * **``component_quantity`` is the WHOLE-JOB total, not a per-unit rate.** Both writers
      (``_create_assembly_routing_operations`` and
      ``_reconcile_operation_component_quantities``) store
      ``qty_per_assembly x work_order.quantity_ordered``. Multiplying it by the produced
      quantity again -- which is what this leg did -- squares the demand: a 100-piece job
      with 2 per unit asked for 20,000. The rate is recovered by dividing back out by
      ``quantity_ordered`` and re-scaling to ``basis``, so the routing and the BOM answer
      in the same units. A work order with no ordered quantity has no rate to recover, so
      the stated total is used verbatim.
    * **One component's demand is REPLICATED across every operation that touches it.** A
      component with a three-operation routing gets three operations all carrying the same
      whole-job ``component_quantity``; summing them tripled the demand. Reduced with
      ``max``: the stated value when the rows agree, and incapable of multiplying by the
      operation count.

      An earlier version of this comment justified ``max`` over ``min`` by claiming
      ``min`` would silently under-issue to 0 on an unreconciled row. That is **wrong**,
      and the refutation is two lines below: rows with no ``component_quantity`` are
      dropped by the ``stated <= _EPSILON`` skip BEFORE the reduction, so ``min`` could
      never be dragged to zero by one. With that argument gone, ``max`` is a bare choice
      of the OVER-issue direction on a genuine disagreement -- which sits awkwardly beside
      this module's own rule that over-issuing writes material into a genealogy record
      that never contained it. It is kept because rows only disagree when something
      upstream failed to reconcile, where the larger stated demand is the likelier truth
      and a shortage is visible while a silent under-issue is not. The disagreement is
      logged so it is answerable; if that log is ever seen in practice, REFUSING is the
      better answer than picking a side, and this leg is flag-gated so a refusal costs
      nothing today.
    * **Self-consumption is refused.** An operation naming the work order's OWN part would
      ISSUE the part the finished-goods leg just RECEIVED, netting the job's own output
      out of stock and writing the produced part into its own as-built record.

    Operation STATUS is deliberately not filtered. ``OperationStatus`` has no cancelled or
    skipped member, so no status means "this work did not happen"; the quantity is
    job-scoped and replicated, so filtering would change the JOB's demand according to
    which replica happened to survive; and at work-order completion every operation is
    terminal by construction (force-complete stamps the stragglers). A non-COMPLETE
    operation contributing demand is logged instead, because the only way it happens is a
    completion path that closed the work order without closing its operations -- a
    production-record defect worth surfacing rather than silently absorbing.
    """
    operations = (
        db.query(WorkOrderOperation)
        .filter(
            WorkOrderOperation.work_order_id == work_order.id,
            WorkOrderOperation.company_id == company_id,
            WorkOrderOperation.component_part_id.isnot(None),
        )
        .all()
    )
    ordered = float(work_order.quantity_ordered or 0)
    demand: dict[int, float] = {}
    for op in operations:
        component_part_id = op.component_part_id
        if component_part_id is None:
            continue
        if work_order.part_id is not None and component_part_id == work_order.part_id:
            logger.warning(
                "Operation %s on WO %s (company %s) names the work order's own part %s as a component; "
                "backflush self-consumption refused",
                op.id,
                work_order.id,
                company_id,
                component_part_id,
            )
            continue
        stated = float(op.component_quantity or 0)
        if stated <= _EPSILON:
            continue
        line = (stated / ordered) * basis if ordered > _EPSILON else stated
        previous = demand.get(component_part_id)
        if previous is not None and abs(previous - line) > _EPSILON:
            logger.warning(
                "WO %s (company %s) operations disagree on component %s demand (%s vs %s); " "backflushing the larger",
                work_order.id,
                company_id,
                component_part_id,
                previous,
                line,
            )
        demand[component_part_id] = line if previous is None else max(previous, line)
        if op.status != OperationStatus.COMPLETE:
            logger.warning(
                "Operation %s on WO %s (company %s) is %s but contributes backflush demand for part %s",
                op.id,
                work_order.id,
                company_id,
                getattr(op.status, "value", op.status),
                component_part_id,
            )
    return demand


def _record_backflush_demand_suppressed(
    db: Session,
    work_order: WorkOrder,
    *,
    part_id: int,
    quantity: float,
    company_id: int,
    audit: AuditService,
    reason: str,
    ledger_net: Optional[float] = None,
) -> None:
    """Record that resolved backflush demand was dropped without issuing material.

    The shop's BOM/routing asked for this component on THIS completion and it did not
    move. Whether that is correct (the material already left under a tie) or merely
    unavoidable (the one-shot index will not permit a second ISSUE) it must not be
    silent: an as-built review cannot reconstruct a decision the system never wrote down,
    and "correct but invisible" is the control gap this action exists to close.

    ``reason`` distinguishes the two, because they have opposite remedies:

    * ``ledger_consumed`` -- a tie already drew this material. Nothing is wrong; the row
      exists so the absence of a backflush ISSUE is explicable.
    * ``already_issued`` -- ``uq_wo_inventory_issue`` permits one ISSUE per (work order,
      part) forever and one already exists. If that ISSUE was later RETURNED the material
      is physically back on the shelf and this work order can never draw it again through
      the backflush; the remedy is an OPERATION-scoped tie, which posts outside the index.
    """
    part_number = (
        db.query(Part.part_number).filter(Part.company_id == company_id, Part.id == part_id).scalar()  # noqa: E501
    )
    logger.warning(
        "Backflush demand of %s for part %s on WO %s (company %s) suppressed (%s)",
        quantity,
        part_id,
        work_order.id,
        company_id,
        reason,
    )
    audit.log(
        action=BACKFLUSH_DOUBLE_ISSUE_BLOCKED_AUDIT_ACTION,
        resource_type="inventory",
        resource_id=part_id,
        resource_identifier=part_number or str(part_id),
        description=(
            f"Backflush of {quantity} of component {part_number or part_id} on work order "
            f"{work_order.work_order_number} was not issued ({reason})"
        ),
        new_values={"suppressed_quantity": quantity, "ledger_net_issued": ledger_net},
        extra_data={
            "work_order_id": work_order.id,
            "work_order_number": work_order.work_order_number,
            "component_part_id": part_id,
            "component_part_number": part_number,
            "suppressed_quantity": quantity,
            "ledger_net_issued": ledger_net,
            "suppression_reason": reason,
        },
        company_id=company_id,
    )


def _backflush_basis(work_order: WorkOrder) -> float:
    """Units of the finished part whose material this job actually drew.

    ``quantity_complete + scrapped`` -- the same shape the per-run tie engine reconciles
    to, so one shop cannot report two different consumptions for one physical event
    depending on whether the material happened to be tied. A scrapped unit consumed its
    material exactly like a good one; leaving scrap out is why a fully-scrapped work
    order used to backflush NOTHING.

    **The scrap term comes from the OPERATIONS, and getting this wrong is invisible.**
    ``WorkOrder.quantity_complete`` IS rolled up from operations
    (``sync_work_order_quantity_complete``, ``max()``-guarded and monotonic-up), so it is
    trustworthy. ``WorkOrder.quantity_scrapped`` is NOT rolled up at all -- its only
    writers are a child reset, a null-guard, force-complete's explicit override and the
    manual office edit. Reading the header column would therefore report scrap as ZERO
    for the ordinary case: an operator who scraps 3 of 10 at the kiosk leaves
    ``operation.quantity_scrapped = 3`` and the header at ``0``, so the tie engine would
    consume for 10 and this leg for 7 -- precisely the divergence including scrap is
    meant to close.

    SUMMED across operations, not maxed: a unit scrapped at operation 10 and a unit
    scrapped at operation 20 are DIFFERENT units, and both drew this job's material.

    The header column is a FALLBACK only, used when no operation carries scrap -- which
    is exactly the force-complete-with-explicit-scrap path, the one writer that sets it.
    """
    produced = float(work_order.quantity_complete or 0)
    operation_scrap = sum(float(op.quantity_scrapped or 0) for op in (work_order.operations or []))
    scrapped = operation_scrap if operation_scrap > _EPSILON else float(work_order.quantity_scrapped or 0)
    return produced + scrapped


def _resolve_backflush_components(
    db: Session,
    work_order: WorkOrder,
    company_id: int,
    allocations: Optional[list[WorkOrderMaterialAllocation]] = None,
    *,
    audit: AuditService,
) -> dict[int, float]:
    """Required quantity per component part for backflushing this WO.

    Runs ONLY when the finished part opted into ``Part.backflush_components`` -- a column
    with no writer anywhere in ``app/`` -- so everything below is dark: it has never
    executed in production and changing it changes no shipped behavior.

    **The basis is ``quantity_complete + quantity_scrapped``, not ``quantity_complete``.**
    A scrapped run physically used its material, and lot genealogy filters on ISSUE, so
    material consumed making scrap must appear as ISSUE or the parts most likely to be
    audited are the ones whose material vanished from the record. This is also the exact
    basis the per-run tie engine has always used (``qty_per_run x (complete + scrapped)``),
    which is what stops one shop from reporting two different consumptions for the same
    physical event depending on whether the material happened to be tied. Consequence: a
    fully-scrapped work order now backflushes, where before it backflushed nothing.

    **The routing no longer wins outright over the BOM.** It used to
    (``if not required:``), so ONE stray ``component_part_id`` on ONE operation silently
    disabled the entire BOM explosion for the whole work order -- and that is not a
    hypothetical: ``_create_assembly_routing_operations`` writes ``component_part_id``
    only for components that HAVE a released routing, so an assembly whose ten BOM lines
    include two routed components lost the other eight. Precedence is now PER PART, which
    is the only scope the routing actually speaks to: an operation naming a component
    states that component's demand and says nothing about the rest of the BOM. Routing
    demand wins for the parts it names; the BOM supplies every part it does not. A
    disagreement between the two on a shared part is logged.

    Suppression then runs in two layers, and BOTH are needed:

    1. ``_drop_allocation_covered_parts`` -- an OPEN operation-scoped tie owns that part's
       demand, including a tie that has not consumed yet (the material is coming).
    2. ``_drop_ledger_covered_parts`` -- the ledger already shows that material gone,
       whatever the tie's status now says.
    """
    basis = _backflush_basis(work_order)
    if basis <= _EPSILON:
        return {}

    explosion = _BackflushBomExplosion()
    if work_order.part_id is not None:
        # Imported lazily to avoid an import cycle with the endpoints module.
        from app.api.endpoints.work_orders import _get_active_bom

        bom = _get_active_bom(db, work_order.part_id, company_id)
        if bom is not None:
            _explode_backflush_bom(
                db,
                bom,
                company_id,
                parent_qty=basis,
                visited_part_ids={bom.part_id},
                out=explosion,
                consumed=True,
            )
    # A part reached as a primary line SOMEWHERE outranks its appearance as an alternate,
    # an optional, or a member of a make subtree elsewhere in the same structure.
    explosion.excluded_part_ids -= set(explosion.demand)

    required: dict[int, float] = dict(explosion.demand)
    for part_id, quantity in _routing_backflush_demand(db, work_order, company_id, basis=basis).items():
        if part_id in explosion.excluded_part_ids:
            logger.warning(
                "WO %s (company %s) operation demand for part %s is dropped: the BOM reaches that part only "
                "as an alternate/optional/reference line or inside a make sub-assembly",
                work_order.id,
                company_id,
                part_id,
            )
            continue
        bom_quantity = required.get(part_id)
        if bom_quantity is not None and abs(bom_quantity - quantity) > _EPSILON:
            logger.warning(
                "WO %s (company %s) routing and BOM disagree on component %s (routing %s, BOM %s); routing wins",
                work_order.id,
                company_id,
                part_id,
                quantity,
                bom_quantity,
            )
        required[part_id] = quantity

    # ALLOCATION PRECEDENCE. A part covered by an OPEN operation-scoped material
    # allocation on this WO is owned by the material-consumption engine, which posts
    # per-run ISSUEs with reference_type='work_order_operation'. Those rows are OUTSIDE
    # the uq_wo_inventory_issue predicate, so nothing at the DB level would stop this
    # WO-level backflush from ALSO issuing the same material -- a silent double-issue.
    # Drop those parts here; the allocation is the sole demand carrier (we deliberately
    # do NOT write op.component_part_id from a tie).
    required = _drop_allocation_covered_parts(db, work_order, company_id, required, allocations)
    return _drop_ledger_covered_parts(db, work_order, company_id, required, audit=audit)


def _drop_allocation_covered_parts(
    db: Session,
    work_order: WorkOrder,
    company_id: int,
    required: dict[int, float],
    allocations: Optional[list[WorkOrderMaterialAllocation]] = None,
) -> dict[int, float]:
    """Remove parts covered by an OPEN OPERATION-scoped allocation from backflush demand.

    No allocations -> ``required`` is returned unchanged, and with the list threaded down
    from ``apply_completion_inventory_effects`` that costs ZERO extra queries -- which is
    what keeps an UNTIED work order byte-identical to its pre-feature behavior.

    A tie is only allowed to SUPPRESS the backflush if it can actually consume, so the
    operation it points at must still exist on THIS work order -- exactly the check the
    consume path already makes (``_consume_tied_materials`` skips such a tie). Without
    the symmetry a tie pointing at a vanished operation (a nest re-import that raced the
    cancel, a hand-edited row) suppressed the backflush AND never consumed: the part was
    silently neither issued nor depleted. The operation lookup only runs when an
    operation-scoped tie exists, so an untied WO still costs nothing here.

    This layer is STATUS-keyed, and that is its whole point: an OPEN tie that has not
    consumed a thing still suppresses, because the material is coming. It is no longer the
    only layer -- ``_drop_ledger_covered_parts`` covers the ties this one cannot see.

    **Suppression is all-or-nothing and stays that way.** ``required.pop`` drops the BOM's
    entire demand for the part however little the tie covers. Subtracting instead is not
    available: the tie's target is ``qty_per_run x runs`` on ONE operation and the BOM's
    demand is per finished unit across the whole job, so a difference between them is two
    incompatible bases, not a shortfall, and netting them would post a quantity nobody
    authored. What the difference IS is worth seeing, so it is logged -- a tie whose live
    target is nowhere near the BOM demand it just cancelled is a planning error somebody
    should look at, and before this it produced no signal at all.
    """
    if not required:
        return required

    allocations = [a for a in _open_allocations(db, work_order, company_id, allocations) if a.part_id in required]
    operation_ids = {a.work_order_operation_id for a in allocations if a.work_order_operation_id is not None}
    if not operation_ids:
        return required

    live_operations = {
        row.id: row
        for row in db.query(
            WorkOrderOperation.id,
            WorkOrderOperation.quantity_complete,
            WorkOrderOperation.quantity_scrapped,
        )
        .filter(
            WorkOrderOperation.company_id == company_id,
            WorkOrderOperation.work_order_id == work_order.id,
            WorkOrderOperation.id.in_(operation_ids),
        )
        .all()
    }
    for allocation in allocations:
        operation = live_operations.get(allocation.work_order_operation_id)
        if operation is not None:
            demand = required.pop(allocation.part_id, None)
            if demand is None:
                continue
            per_run = float(allocation.qty_per_run if allocation.qty_per_run is not None else 1.0)
            tie_target = per_run * (float(operation.quantity_complete or 0) + float(operation.quantity_scrapped or 0))
            if abs(tie_target - demand) > _EPSILON:
                logger.warning(
                    "Allocation %s suppresses %s of part %s on WO %s (company %s) but its own live target is "
                    "%s; the tie's per-run basis and the BOM's per-unit demand disagree",
                    allocation.id,
                    demand,
                    allocation.part_id,
                    work_order.id,
                    company_id,
                    tie_target,
                )
        elif allocation.work_order_operation_id is not None:
            logger.warning(
                "Allocation %s points at operation %s which is not on WO %s (company %s); "
                "backflush demand for part %s is NOT suppressed",
                allocation.id,
                allocation.work_order_operation_id,
                work_order.id,
                company_id,
                allocation.part_id,
            )
    return required


def operation_scoped_net_issued_by_part(
    db: Session,
    *,
    work_order_id: int,
    company_id: int,
    part_ids: Iterable[int],
) -> dict[int, float]:
    """Signed ISSUE - RETURN per part, over this WO's OPERATION-scoped ledger rows.

    The reference shape is ``('work_order_operation', <an operation on this work order>)``
    -- the same resolution ``work_order_ledger_filter`` performs, narrowed to the one
    shape that matters here. The ``work_order`` shape is deliberately NOT included: rows
    under it are already covered by ``_component_already_issued`` and by
    ``uq_wo_inventory_issue``, and folding them in would turn an ordinary idempotent
    replay into a "double issue blocked" audit row on every re-entry.

    The SIGN is keyed on ``transaction_type``, never on the stored sign of ``quantity``
    (ISSUE stores it negative, RETURN positive) -- the rule every other reader that learned
    to net returns follows, and the one that stops a credit being counted as more
    consumption. A dataset with no RETURN rows is numerically identical to a plain
    ``SUM(ABS(quantity))``.

    Tenant-scoped on the outer predicate and the operation subquery alike (invariant #1).
    Empty input short-circuits to no query at all.
    """
    ids = [int(part_id) for part_id in part_ids]
    if not ids:
        return {}
    operation_ids = select(WorkOrderOperation.id).where(
        WorkOrderOperation.company_id == company_id,
        WorkOrderOperation.work_order_id == work_order_id,
    )
    rows = (
        db.query(
            InventoryTransaction.part_id,
            func.sum(
                case(
                    (
                        InventoryTransaction.transaction_type == TransactionType.RETURN,
                        -func.abs(InventoryTransaction.quantity),
                    ),
                    else_=func.abs(InventoryTransaction.quantity),
                )
            ),
        )
        .filter(
            InventoryTransaction.company_id == company_id,
            InventoryTransaction.part_id.in_(ids),
            InventoryTransaction.transaction_type.in_((TransactionType.ISSUE, TransactionType.RETURN)),
            InventoryTransaction.reference_type == OPERATION_REFERENCE_TYPE,
            InventoryTransaction.reference_id.in_(operation_ids),
        )
        .group_by(InventoryTransaction.part_id)
        .all()
    )
    return {part_id: float(total or 0.0) for part_id, total in rows}


def net_consumed_quantity_for_allocation(db: Session, *, allocation_id: int, company_id: int) -> float:
    """Signed ISSUE - RETURN the ledger holds against ONE material tie. Tenant-scoped.

    The authoritative answer to "is this tie still holding material?", as opposed to
    ``WorkOrderMaterialAllocation.qty_consumed``, which the plan documents as a CACHE and
    which the completion backflush writes as ``qty_planned`` rather than as the quantity
    the ISSUE actually posted. Guards whose refusal protects the ledger must read the
    ledger: hard delete has since PR 1, nest re-import since PR 3, and the manual untie
    since this PR.

    Signed, not existence-keyed. ``ledger_backed_allocation_ids`` answers a different
    question -- "would deleting this row orphan a ledger reference?" -- for which mere
    existence is correct, because a RETURN row references the tie just as durably as the
    ISSUE it compensates. Here the question is whether material is still OUT, and a tie
    that gave everything back is holding none.
    """
    total = (
        db.query(
            func.coalesce(
                func.sum(
                    case(
                        (
                            InventoryTransaction.transaction_type == TransactionType.RETURN,
                            -func.abs(InventoryTransaction.quantity),
                        ),
                        else_=func.abs(InventoryTransaction.quantity),
                    )
                ),
                0.0,
            )
        )
        .filter(
            InventoryTransaction.company_id == company_id,
            InventoryTransaction.allocation_id == allocation_id,
            InventoryTransaction.transaction_type.in_((TransactionType.ISSUE, TransactionType.RETURN)),
        )
        .scalar()
    )
    return float(total or 0.0)


def _drop_ledger_covered_parts(
    db: Session,
    work_order: WorkOrder,
    company_id: int,
    required: dict[int, float],
    *,
    audit: AuditService,
) -> dict[int, float]:
    """Suppress backflush demand for a part the LEDGER already shows leaving this job.

    The double-issue ``_drop_allocation_covered_parts`` cannot prevent on its own. That
    layer keys on tie STATUS, and three guards in a row miss the same case:

    * a tie that CONSUMED and is no longer ``OPEN`` is invisible to it;
    * ``_component_already_issued`` keys on ``reference_type='work_order'`` ISSUE rows and
      therefore cannot see tied consumption at all (it posts under
      ``work_order_operation``);
    * ``uq_wo_inventory_issue``'s partial predicate does not cover those rows either --
      that is the whole reason the reference type was split.

    So the same part leaves stock twice, and the as-built record carries two lines naming
    two DIFFERENT lots for one physical consumption (AS9100D 8.5.2). It is reachable
    through supported verbs: ``cancel_open_allocations_for_work_order`` cancels a tie
    regardless of ``qty_consumed``, and a restore only re-opens ties whose most recent
    DELETE audit row carries the delete's own reason -- so a missing or detached audit row,
    or a cancel that came from anywhere else, leaves a consumed tie CANCELLED forever.

    Keyed on the LEDGER because ``status`` and ``qty_consumed`` are both documented as
    non-authoritative planning state; every other guard of comparable consequence (hard
    delete since PR 1, nest re-import since PR 3) already reads it for the same reason.

    **A fully-returned tie nets to zero and IS permitted to re-issue. That is a decision,
    not an accident of the arithmetic.** PR 3's ``return_and_untie`` credits the material
    back to its source lots; the job then holds none of it, and the BOM's demand for that
    part is once again unmet. Suppressing on the mere EXISTENCE of ledger rows would leave
    the part permanently un-issuable on a job that genuinely gave the material back --
    refusing to consume material the shop is holding, and hiding the demand from the
    shortage machinery that would otherwise surface it.

    Suppression is RECORDED (``BACKFLUSH_DOUBLE_ISSUE_BLOCKED``) rather than silent. The
    row is bounded, not per-request: this fires on the completion paths and on a reconcile
    pass that actually applies a work-order transition, the same cardinality as
    ``BACKFLUSH_SHORTAGE``, not on every read.

    One known blind spot, inherited from ``work_order_ledger_filter`` and not introduced
    here: operation ids resolve through a LIVE subquery, so rows naming an operation a nest
    re-import deleted drop out of it. The structural fix is superseding operations instead
    of deleting them (see the plan's fourth residual gap).
    """
    if not required:
        return required

    nets = operation_scoped_net_issued_by_part(
        db, work_order_id=work_order.id, company_id=company_id, part_ids=required.keys()
    )
    blocked = {part_id: net for part_id, net in nets.items() if net > _EPSILON and part_id in required}
    if not blocked:
        return required

    part_numbers = {
        part_id: part_number
        for part_id, part_number in db.query(Part.id, Part.part_number)
        .filter(Part.company_id == company_id, Part.id.in_(blocked.keys()))
        .all()
    }
    for part_id, net in blocked.items():
        demand = required.pop(part_id)
        part_number = part_numbers.get(part_id)
        logger.warning(
            "Backflush demand of %s for part %s on WO %s (company %s) suppressed: the ledger already shows "
            "%s consumed against this work order's operations",
            demand,
            part_id,
            work_order.id,
            company_id,
            net,
        )
        audit.log(
            action=BACKFLUSH_DOUBLE_ISSUE_BLOCKED_AUDIT_ACTION,
            resource_type="inventory",
            resource_id=part_id,
            resource_identifier=part_number or str(part_id),
            description=(
                f"Backflush of {demand} of component {part_number or part_id} on work order "
                f"{work_order.work_order_number} was blocked: {net} has already been consumed against "
                "this work order's operations by a material tie"
            ),
            new_values={"suppressed_quantity": demand, "ledger_net_issued": net},
            extra_data={
                "work_order_id": work_order.id,
                "work_order_number": work_order.work_order_number,
                "component_part_id": part_id,
                "component_part_number": part_number,
                "suppressed_quantity": demand,
                "ledger_net_issued": net,
                "reference_type": OPERATION_REFERENCE_TYPE,
            },
            company_id=company_id,
        )
    return required


def _work_order_scoped_allocation_demand(
    db: Session,
    work_order: WorkOrder,
    company_id: int,
    allocations: Optional[list[WorkOrderMaterialAllocation]] = None,
) -> dict[int, WorkOrderMaterialAllocationDemand]:
    """One-shot demand contributed by WORK-ORDER-scoped (non-operation) ties.

    A work-order-scoped tie has no operation to scale against, so its demand is the
    full ``qty_planned``, consumed once at completion through the SAME one-shot
    backflush machinery (``reference_type='work_order'``). That matters because
    ``uq_wo_inventory_issue`` permits exactly ONE ISSUE per (company, WO, part): demand
    from the BOM/routing and demand from a tie must be SUMMED into that single row, not
    emitted twice (the second insert would be swallowed as a duplicate no-op and the
    tie would silently never consume).

    Unlike the BOM backflush this is NOT gated on ``part.backflush_components``: an
    explicit tie IS the opt-in. Keyed by part id; a second tie on the same part sums in.

    The tie's LOT PIN travels with the demand. It has to: this leg is the only consumer
    of a work-order-scoped tie, so a pin dropped here is a pin that never happens, and
    the ISSUE would silently pick a different lot than the one the tie names.
    """
    demand: dict[int, WorkOrderMaterialAllocationDemand] = {}
    for allocation in _open_allocations(db, work_order, company_id, allocations):
        if allocation.work_order_operation_id is not None:
            continue
        quantity = float(allocation.qty_planned or 0) - float(allocation.qty_consumed or 0)
        if quantity <= _EPSILON:
            continue
        existing = demand.get(allocation.part_id)
        if existing is None:
            demand[allocation.part_id] = WorkOrderMaterialAllocationDemand(
                allocation_ids=[allocation.id],
                quantity=quantity,
                pinned_inventory_item_id=allocation.pinned_inventory_item_id,
            )
        else:
            existing.allocation_ids.append(allocation.id)
            existing.quantity += quantity
            if existing.pinned_inventory_item_id is None:
                existing.pinned_inventory_item_id = allocation.pinned_inventory_item_id
    return demand


def _placeholder_stock_row(
    db: Session,
    *,
    part_id: int,
    company_id: int,
    unit_cost: float,
) -> InventoryItem:
    """A zero-quantity stock row to carry a consumption when no lot exists at all.

    An ``InventoryTransaction`` with a dangling ``inventory_item_id`` would be worse than
    a negative on-hand, so the negative movement is still recorded against a REAL row.
    Shared by the backflush leg and the per-run consumption engine (which had its own
    byte-for-byte copy of this).
    """
    item = InventoryItem(
        part_id=part_id,
        location=FINISHED_GOODS_LOCATION,
        warehouse=FINISHED_GOODS_WAREHOUSE,
        quantity_on_hand=0.0,
        quantity_allocated=0.0,
        quantity_available=0.0,
        unit_cost=unit_cost,
        received_date=datetime.utcnow(),
        status="available",
    )
    item.company_id = company_id
    db.add(item)
    db.flush()
    return item


def _is_placeholder_stock_row(item: InventoryItem) -> bool:
    """True when a stock row has the shape ``_placeholder_stock_row`` creates.

    Lives next to its constructor deliberately: the two definitions must move together,
    and the reader that needs this is in another module (the RETURN engine).

    A placeholder is a LOT-LESS row at the finished-goods location, minted only so a
    consumption with no stock at all still has a real ``inventory_item_id`` to point at.
    It names no heat and carries no cert, so it is a fine sink for a negative movement
    and a bad SOURCE for a positive one: crediting material back into it would create
    unlabeled, FIFO-eligible stock out of a row that exists purely as a ledger anchor
    (AS9100D 8.5.2). The RETURN engine refuses rather than guessing.

    Identification is by SHAPE, not by a flag -- there is no column marking these -- so it
    matches the constructor's FULL shape (location AND warehouse), not a prefix of it.
    Matching on location alone OVER-matches, and not hypothetically: the finished-goods
    RECEIPT path mints genuine stock at ``FINISHED-GOODS`` with
    ``lot_number = work_order.lot_number``, which is NULL for any work order carrying no
    lot. A sub-assembly that is both produced and consumed can therefore hold real,
    lot-less finished-goods stock -- and calling that a placeholder would 409 a return
    that should have been allowed. The failure direction was safe (refuse, never
    mis-credit), but a refusal a user cannot act on is still a defect.
    """
    return (
        not item.lot_number and item.location == FINISHED_GOODS_LOCATION and item.warehouse == FINISHED_GOODS_WAREHOUSE
    )


def _issue_one_component(
    db: Session,
    work_order: WorkOrder,
    *,
    component_part_id: int,
    required_qty: float,
    company_id: int,
    user_id: int,
    audit: AuditService,
    allocation_id: Optional[int] = None,
    pinned_inventory_item_id: Optional[int] = None,
) -> IssueOutcome:
    """Backflush a single component: write ONE ISSUE txn + decrement source stock.

    The work-order ISSUE unique index keys on ``(company, WO, ISSUE, part_id)``, so a
    component is consumed by EXACTLY ONE ISSUE per WO. We therefore write a single
    negative ISSUE for the FULL ``required_qty`` against the primary source lot,
    carrying that lot on the txn for genealogy. If total on-hand is insufficient, the
    primary lot is driven NEGATIVE (consumption + true demand still RECORDED, matching
    the permissive ``/inventory/adjust`` behavior) and a ``ComponentShortage`` is
    reported (never raised) -- additionally recorded tamper-evidently (item 3). The
    ISSUE INSERT is wrapped in a SAVEPOINT (item 1): a concurrent duplicate (the
    double-issue race the index catches) rolls back only the savepoint and is a clean
    no-op (no decrement, no shortage record), reported as ``posted=False``.

    **Lot selection.** ``pinned_inventory_item_id`` (set only when a WORK-ORDER-scoped
    material tie carried a lot pin) consumes from THAT lot exclusively -- pinning is a
    lot-directed instruction, so an insufficient pinned lot is driven negative rather
    than silently spilling onto a different, uncertified, wrong-heat lot. This mirrors
    the operation-scoped engine exactly. Unpinned demand keeps the historical behavior:
    the lowest-id active row with on-hand -- NOT ``received_date`` FIFO, which is what
    the per-run engine does -- else a placeholder at standard cost.

    **The two engines' lot selection genuinely contradicts each other, and this one is
    deliberately NOT changed.** Unpinned, this leg takes the LOWEST-ID active on-hand row,
    ignores ``status`` entirely, and writes ONE ISSUE against that single lot. The per-run
    engine (``_fifo_source_items``) walks ``received_date`` FIFO, filters
    ``status = 'available'``, and spills across as many lots as the delta needs. Both write
    lot genealogy, so on the same material they can name different heats for the same
    physical draw. Reconciling them is a real, wanted fix and it is not a dark one: this
    function is LIVE -- work-order-scoped material ties have driven it since PR 1, so any
    change to which lot it picks changes shipped genealogy for work orders that have
    nothing to do with the BOM backflush, and adding ``status = 'available'`` would newly
    exclude legacy NULL-status rows on top of that. It belongs in a PR that can say so and
    carry the data review, not in one whose remit is the flag-gated leg above.

    **Neither branch consumes a held lot silently.** A lot that is inactive or not
    ``available`` is still consumed -- the material is already in the part and this path
    also runs from a reconcile-on-read GET, where refusing would be unattributable --
    but the fact goes on the tamper-evident chain as ``HELD_MATERIAL_CONSUMED``
    (AS9100D 8.7). On the PINNED branch the row can only ever mean "held after it was
    pinned", because the tie endpoint refuses to pin a held lot (422). On the UNPINNED
    branch it can mean the lot was already held when it was picked: that SELECT filters
    ``is_active`` and on-hand but deliberately not ``status`` (its per-run twin
    ``_fifo_source_items`` does), and tightening it here would newly exclude legacy
    NULL-status rows and change the pre-existing BOM backflush -- so the lot choice is
    left exactly as it was and the consumption is RECORDED instead of skipped.
    """
    part = db.query(Part).filter(Part.id == component_part_id, Part.company_id == company_id).first()
    part_number = part.part_number if part else None
    unit_cost = float(part.standard_cost or 0) if part else 0.0

    # Imported lazily: ``material_consumption_service`` imports helpers from THIS module.
    from app.services.material_consumption_service import is_consumable_item, record_held_material_consumed

    held_item: Optional[InventoryItem] = None
    if pinned_inventory_item_id is not None:
        pinned = (
            db.query(InventoryItem)
            .filter(
                InventoryItem.id == pinned_inventory_item_id,
                InventoryItem.company_id == company_id,
            )
            .first()
        )
        source_items = [pinned] if pinned is not None else []
        if pinned is not None and not is_consumable_item(pinned):
            held_item = pinned
    else:
        source_items = (
            db.query(InventoryItem)
            .filter(
                InventoryItem.company_id == company_id,
                InventoryItem.part_id == component_part_id,
                InventoryItem.is_active == True,  # noqa: E712
                InventoryItem.quantity_on_hand > 0,
            )
            .order_by(InventoryItem.id)
            .all()
        )
    available_total = sum(float(i.quantity_on_hand or 0) for i in source_items)

    # Primary consumed lot: the pinned lot, else the lowest-id on-hand row, else a
    # placeholder row (so the negative consumption is still recorded against a real item).
    target = source_items[0] if source_items else None
    if target is None:
        target = _placeholder_stock_row(db, part_id=component_part_id, company_id=company_id, unit_cost=unit_cost)
    elif held_item is None and not is_consumable_item(target):
        # UNPINNED leg, and the lot it picked is held. The SELECT above filters
        # ``is_active`` and on-hand but deliberately NOT ``status`` -- adding
        # ``status = 'available'`` would newly exclude legacy NULL-status rows and change
        # the pre-existing BOM backflush, so lot SELECTION stays exactly as it was. What
        # changes is that consuming an ``on_hold`` / ``quarantine`` / ``rejected`` lot is
        # no longer SILENT: it takes the same AS9100D 8.7 chain row the pinned leg writes.
        # (``is_consumable_item`` reads a NULL status as available, so a legacy row is
        # still not treated as held.)
        held_item = target

    # ONE ISSUE for the full required quantity, inserted FIRST under a savepoint; the
    # decrement applies ONLY when the insert actually committed. A duplicate (a
    # concurrent completion already issued this component) is a clean no-op -- no
    # decrement, no shortage record -- so it can never double-consume or abort the
    # outer completion / reconcile transaction.
    txn = _write_issue_txn(
        db,
        work_order,
        inventory_item=target,
        component_part_id=component_part_id,
        quantity=required_qty,
        unit_cost=float(target.unit_cost or unit_cost),
        lot_number=target.lot_number,
        company_id=company_id,
        user_id=user_id,
        audit=audit,
        part_number=part_number,
        allocation_id=allocation_id,
    )
    if txn is None:
        return IssueOutcome(posted=False)

    outcome = IssueOutcome(posted=True)

    if held_item is not None:
        # This leg writes exactly ONE ISSUE against ONE lot (uq_wo_inventory_issue
        # permits no second row per (WO, part)), so the whole demand came off the held
        # lot -- whether through the normal take or by driving it negative -- on the
        # pinned and unpinned branches alike.
        record_held_material_consumed(
            work_order=work_order,
            operation=None,
            allocation_id=allocation_id,
            part_id=component_part_id,
            item=held_item,
            part_number=part_number,
            quantity=required_qty,
            company_id=company_id,
            audit=audit,
            pinned=pinned_inventory_item_id is not None,
        )

    shortfall = required_qty - available_total
    if shortfall > _EPSILON:
        outcome.shortage = ComponentShortage(
            part_id=component_part_id,
            part_number=part_number,
            required_quantity=required_qty,
            available_quantity=available_total,
            shortfall=shortfall,
        )
        logger.warning(
            "Backflush shortage on WO %s component %s (company %s): required %s, available %s, short %s",
            work_order.id,
            component_part_id,
            company_id,
            required_qty,
            available_total,
            shortfall,
        )
        # Item 3: a negative on-hand is a regulated material-trail control gap, so
        # record the shortage tamper-evidently (audit_log hash chain) AND emit a
        # warning OperationalEvent -- not just a log line. Atomic with the completion
        # on the live paths (both flush, never commit). The consumed source lot of
        # the driven-negative row is carried for genealogy.
        _record_backflush_shortage(
            db,
            work_order,
            shortage=outcome.shortage,
            consumed_lot=target.lot_number,
            company_id=company_id,
            user_id=user_id,
            audit=audit,
        )

    return outcome


def _record_backflush_shortage(
    db: Session,
    work_order: WorkOrder,
    *,
    shortage: ComponentShortage,
    consumed_lot: Optional[str],
    company_id: int,
    user_id: int,
    audit: AuditService,
) -> None:
    """Persist a backflush shortage as a tamper-evident audit row + OperationalEvent (item 3).

    Writes ONE ``audit_log`` row (action ``BACKFLUSH_SHORTAGE``) on the component part,
    carrying the shortfall qty + consumed lot + the producing WO in ``extra_data`` so the
    negative on-hand is on the immutable hash chain (never written directly). Then emits a
    ``backflush_shortage`` ``OperationalEvent`` (``severity="warning"``) for AI/realtime
    consumers. Tenant-scoped (``company_id`` on both). The audit ``log`` and the event
    ``emit`` both only flush (never commit), so the records land atomically with the
    completion on the live paths; the event emit is wrapped so a transient signal failure
    can never fail an in-flight completion (the audit row is the compliance record).
    """
    extra = {
        "work_order_id": work_order.id,
        "work_order_number": work_order.work_order_number,
        "component_part_id": shortage.part_id,
        "component_part_number": shortage.part_number,
        "required_quantity": shortage.required_quantity,
        "available_quantity": shortage.available_quantity,
        "shortfall": shortage.shortfall,
        "consumed_lot": consumed_lot,
    }
    audit.log(
        action=BACKFLUSH_SHORTAGE_AUDIT_ACTION,
        resource_type="inventory",
        resource_id=shortage.part_id,
        resource_identifier=shortage.part_number or str(shortage.part_id),
        description=(
            f"Backflush shortage on WO {work_order.work_order_number}: component "
            f"{shortage.part_number or shortage.part_id} short {shortage.shortfall} "
            f"(required {shortage.required_quantity}, available {shortage.available_quantity})"
            + (f", lot {consumed_lot}" if consumed_lot else "")
        ),
        new_values={"shortfall": shortage.shortfall},
        extra_data=extra,
        company_id=company_id,
    )
    try:
        OperationalEventService(db).emit(
            company_id=company_id,
            event_type=BACKFLUSH_SHORTAGE_EVENT_TYPE,
            source_module="completion_inventory",
            entity_type="inventory",
            entity_id=shortage.part_id,
            work_order_id=work_order.id,
            user_id=user_id,
            severity="warning",
            event_payload=extra,
        )
    except Exception:  # pragma: no cover - a warning signal must never fail a completion
        # The audit row above is the compliance record; the operational event is an
        # AI/realtime convenience. Swallow any emit failure so the completion the caller
        # is committing is unaffected (mirrors the quality-gate exception pattern).
        logger.exception(
            "backflush_shortage event emit failed for WO %s component %s (company %s)",
            work_order.id,
            shortage.part_id,
            company_id,
        )


def _write_issue_txn(
    db: Session,
    work_order: WorkOrder,
    *,
    inventory_item: InventoryItem,
    component_part_id: int,
    quantity: float,
    unit_cost: float,
    lot_number: Optional[str],
    company_id: int,
    user_id: int,
    audit: AuditService,
    part_number: Optional[str],
    allocation_id: Optional[int] = None,
    reference_type: str = WORK_ORDER_REFERENCE_TYPE,
    reference_id: Optional[int] = None,
    notes: Optional[str] = None,
    movement_verb: str = "Backflushed",
    movement_label: str = "Backflush",
    movement_suffix: str = "",
    extra_data: Optional[dict] = None,
) -> Optional[InventoryTransaction]:
    """Write one negative ISSUE txn (carrying the consumed lot), decrement, + audit.

    THE single way material leaves stock against a work order. The per-run consumption
    engine used to carry a near-verbatim copy of this (``_post_consumption_txn``)
    differing only in reference shape, notes and description -- while its module docstring
    claimed it "REUSES its helpers rather than reimplementing them". The variable parts
    are now parameters:

    * ``reference_type`` / ``reference_id`` -- ``('work_order', work_order.id)`` for the
      FG backflush and work-order-scoped ties, ``('work_order_operation', operation.id)``
      for per-run consumption (outside the ``uq_wo_inventory_*`` predicates by design);
    * ``notes`` -- the ledger row's own note (defaults to the backflush wording);
    * ``movement_verb`` / ``movement_label`` / ``movement_suffix`` -- the audit prose;
    * ``extra_data`` -- extra audit context (the tie + operation ids on the per-run leg).

    The insert -> decrement -> dual-audit body itself lives in
    ``_post_stock_movement_txn``, shared with the compensating ``_write_return_txn``; the
    only thing that differs between consuming and returning is the SIGN of the on-hand
    move. Read that helper for the ordering rule (insert first, under a savepoint; move
    on-hand only when the insert actually landed) and for why ``quantity_available`` is
    recomputed in the same block.

    Returns the inserted ``InventoryTransaction``, or ``None`` on a duplicate no-op.
    """
    txn = InventoryTransaction(
        company_id=company_id,
        inventory_item_id=inventory_item.id,
        part_id=component_part_id,
        transaction_type=TransactionType.ISSUE,
        quantity=-quantity,
        from_location=inventory_item.location,
        lot_number=lot_number,
        reference_type=reference_type,
        reference_id=work_order.id if reference_id is None else reference_id,
        reference_number=work_order.work_order_number,
        # NULL for a plain BOM/routing backflush; set when a material tie contributed the
        # demand, so the ledger row walks back to the tie.
        allocation_id=allocation_id,
        unit_cost=unit_cost,
        total_cost=quantity * unit_cost,
        notes=notes if notes is not None else f"Backflush consumption for work order {work_order.work_order_number}",
        created_by=user_id,
    )
    return _post_stock_movement_txn(
        db,
        txn=txn,
        inventory_item=inventory_item,
        # NEGATIVE: an ISSUE takes material off the shelf.
        on_hand_delta=-quantity,
        audit=audit,
        movement_description=(
            f"{movement_verb} {quantity} of part {part_number or component_part_id} "
            f"for work order {work_order.work_order_number}"
            + movement_suffix
            + (f" lot {lot_number}" if lot_number else "")
        ),
        stock_identifier=f"{part_number or component_part_id} @ {inventory_item.location}",
        stock_description=f"{movement_label}: stock for part {part_number or component_part_id}",
        extra_data=extra_data,
    )


def _write_return_txn(
    db: Session,
    work_order: WorkOrder,
    *,
    inventory_item: InventoryItem,
    part_id: int,
    quantity: float,
    unit_cost: float,
    lot_number: Optional[str],
    company_id: int,
    user_id: int,
    audit: AuditService,
    part_number: Optional[str],
    allocation_id: Optional[int],
    reference_type: str,
    reference_id: int,
    reason_code: str,
    notes: str,
    movement_verb: str = "Returned",
    movement_label: str = "Material return",
    movement_suffix: str = "",
    extra_data: Optional[dict] = None,
) -> InventoryTransaction:
    """Write one POSITIVE ``RETURN`` txn against a source lot, increment it, + audit.

    The compensating sibling of ``_write_issue_txn``, sharing its
    construct -> insert -> move-on-hand -> dual-audit body (``_post_stock_movement_txn``)
    rather than copying it. The insert/increment ORDER and the dual-audit shape are the
    compliance-visible parts of a stock movement; a drifted copy is exactly what the
    shared helper exists to prevent.

    Deliberate choices, each of which has a wrong-looking alternative:

    * **``TransactionType.RETURN``, positive quantity.** Not a positive ``ISSUE`` -- five
      readers ``abs()`` an ISSUE row into MORE consumption, and under
      ``reference_type='work_order'`` a second ISSUE row collides with
      ``uq_wo_inventory_issue``. Not ``ADJUST`` either: that means "a count changed", not
      "material came back off a job".
    * **``reference_type`` / ``reference_id`` MIRROR the rows being compensated**
      (``('work_order_operation', operation.id)`` for an operation-scoped tie). That is
      the single most load-bearing field here: ``work_order_ledger_filter`` matches on
      reference SHAPE only, never on ``transaction_type``, so a correctly-referenced
      RETURN is picked up by job cost, analytics, lot genealogy and
      ``GET /inventory/transactions?work_order_id=`` with no change to those readers.
    * **``unit_cost`` is the COMPENSATED ROW'S**, not the lot's current ``unit_cost`` --
      a lot revaluation between consume and return would otherwise leave residual (or
      negative) material cost stranded on the job.
    * **``to_location``** mirrors the ISSUE's ``from_location``: the material goes back
      where it came from.

    Unlike the ISSUE path this NEVER treats a duplicate as a no-op. A RETURN row sits
    outside both ``uq_wo_inventory_*`` predicates (they require ``transaction_type`` of
    ``RECEIVE`` / ``ISSUE``), so there is no idempotency index to lose a race with: an
    ``IntegrityError`` here is a real fault and must reach the write handler that asked
    for the return, not be swallowed into a silent "nothing moved". Hence the non-optional
    return type.
    """
    txn = InventoryTransaction(
        company_id=company_id,
        inventory_item_id=inventory_item.id,
        part_id=part_id,
        transaction_type=TransactionType.RETURN,
        quantity=quantity,
        to_location=inventory_item.location,
        lot_number=lot_number,
        reference_type=reference_type,
        reference_id=reference_id,
        reference_number=work_order.work_order_number,
        # The tie the material came off, so the compensating row walks back to the same
        # allocation its ISSUE rows do (and the ledger-backed guards stay armed).
        allocation_id=allocation_id,
        unit_cost=unit_cost,
        total_cost=quantity * unit_cost,
        reason_code=reason_code,
        notes=notes,
        created_by=user_id,
    )
    posted = _post_stock_movement_txn(
        db,
        txn=txn,
        inventory_item=inventory_item,
        # POSITIVE: a RETURN puts material back on the shelf. Returning INTO a negative
        # lot is expected -- a shortage-driven consumption drove it below zero and this
        # unwinds it toward zero -- so there is deliberately no guard against it.
        on_hand_delta=quantity,
        audit=audit,
        movement_description=(
            f"{movement_verb} {quantity} of part {part_number or part_id} "
            f"to stock from work order {work_order.work_order_number}"
            + movement_suffix
            + (f" lot {lot_number}" if lot_number else "")
        ),
        stock_identifier=f"{part_number or part_id} @ {inventory_item.location}",
        stock_description=f"{movement_label}: stock for part {part_number or part_id}",
        extra_data=extra_data,
        duplicate_is_noop=False,
    )
    if posted is None:  # pragma: no cover - unreachable: duplicate_is_noop=False never returns None
        raise RuntimeError(f"Material return transaction for work order {work_order.id} was not written")
    return posted


def _post_stock_movement_txn(
    db: Session,
    *,
    txn: InventoryTransaction,
    inventory_item: InventoryItem,
    on_hand_delta: float,
    audit: AuditService,
    movement_description: str,
    stock_identifier: str,
    stock_description: str,
    extra_data: Optional[dict] = None,
    duplicate_is_noop: bool = True,
) -> Optional[InventoryTransaction]:
    """Insert one ledger row, move the lot's on-hand by ``on_hand_delta``, write the audit.

    THE single implementation of the four steps every work-order material movement owes,
    in the order it owes them (item 1):

    1. INSERT the ``InventoryTransaction`` first (under a savepoint when the caller has an
       idempotency index behind it);
    2. move ``quantity_on_hand`` ONLY when that insert actually landed -- a duplicate
       inserted nothing, so decrementing/incrementing would double-count against the
       winning transaction's row;
    3. recompute ``quantity_available`` in the SAME block as the ``quantity_on_hand``
       mutation, because skipping it silently desyncs the denormalized column the
       receipt-void guard and MRP read;
    4. write the DUAL audit rows -- an ``inventory`` CREATE for the movement plus an
       ``inventory`` UPDATE for the on-hand change it produced (the canonical shape,
       shared with ``inventory.py``'s ``_audit_stock_movement``).

    The sign of ``on_hand_delta`` is the whole difference between consuming and
    returning; everything else about the two is identical, which is exactly why they
    share this body instead of each carrying a copy of it.

    ``duplicate_is_noop`` picks the insert discipline:

    * ``True`` (ISSUE / backflush) -- a partial UNIQUE index backs the row, so a
      concurrent duplicate raises ``IntegrityError``; roll back just the savepoint and
      report ``None``, leaving the outer completion / reconcile transaction usable.
    * ``False`` (RETURN) -- no index covers the row, so an ``IntegrityError`` is a real
      fault. Insert plainly and let it propagate to the write handler.
    """
    if duplicate_is_noop:
        if not _insert_txn_with_savepoint(db, txn):
            return None
    else:
        db.add(txn)
        db.flush()

    old_on_hand = inventory_item.quantity_on_hand
    inventory_item.quantity_on_hand = float(inventory_item.quantity_on_hand or 0) + on_hand_delta
    inventory_item.quantity_available = inventory_item.quantity_on_hand - float(inventory_item.quantity_allocated or 0)
    db.flush()

    audit.log_create(
        "inventory",
        txn.id,
        str(txn.id),
        new_values=txn,
        description=movement_description,
        extra_data=extra_data,
    )
    if old_on_hand is not None:
        audit.log_update(
            "inventory",
            inventory_item.id,
            stock_identifier,
            old_values={"quantity_on_hand": old_on_hand},
            new_values={"quantity_on_hand": inventory_item.quantity_on_hand},
            description=stock_description,
        )
    return txn


def backflush_components_for_work_order(
    db: Session,
    work_order: WorkOrder,
    *,
    user_id: int,
    company_id: int,
    audit: AuditService,
    allocations: Optional[list[WorkOrderMaterialAllocation]] = None,
) -> BackflushResult:
    """Consume a completed WO's BOM components from inventory (INV-2).

    GATED: the BOM/routing leg only runs when ``work_order.part.backflush_components``
    is True (opt-in per part, default False) so material a shop issued manually is never
    double-consumed. That column has NO writer anywhere in ``app/`` -- no schema field, no
    endpoint, no UI -- so this leg has never run in production and
    ``_resolve_backflush_components`` is dark code. Idempotent per component (skips a
    component that already has a WO ISSUE txn). Each consumed source lot is carried on the
    ISSUE txn for as-built genealogy. A shortage NEVER fails the completion, but is
    recorded tamper-evidently (a ``BACKFLUSH_SHORTAGE`` ``audit_log`` row + a
    ``backflush_shortage`` ``OperationalEvent``) inside ``_issue_one_component`` -- so it
    is captured on BOTH the live paths AND the reconcile path (the caller no longer needs
    to inspect the returned shortages to record them). Does NOT commit.

    **``_component_already_issued`` is EXISTENCE-keyed and stays that way**, unlike the
    signed ledger net ``_drop_ledger_covered_parts`` uses one layer up. That is the
    documented answer to a real asymmetry: a fully-returned OPERATION-scoped tie nets to
    zero and lets the backflush re-issue, while a fully-returned WORK-ORDER-scoped tie
    leaves its part un-issuable on this work order forever. The difference is not a policy
    preference -- ``uq_wo_inventory_issue`` permits exactly ONE ISSUE row per
    (company, WO, part) under ``reference_type='work_order'``, so a second issue on that
    shape is physically unavailable at any price short of a migration. Netting the guard
    here would therefore not enable a re-issue; it would attempt one, lose to the index,
    and be swallowed as a duplicate no-op -- claiming a consumption that never posted. The
    remedy for a work-order-scoped tie that needs to re-consume is an OPERATION-scoped
    tie, which posts outside that index; ``create_material_allocation``'s 409 already says
    so.

    Also drains WORK-ORDER-scoped material ties (``work_order_material_allocations``
    with no operation). Those are NOT gated on ``backflush_components`` -- an explicit
    tie is itself the opt-in -- and their demand is SUMMED with any BOM demand for the
    same part so exactly ONE ISSUE row is emitted per (WO, part), as
    ``uq_wo_inventory_issue`` requires. OPERATION-scoped ties are handled by
    ``material_consumption_service`` and are excluded from this leg entirely. A
    work-order-scoped tie's LOT PIN is honored -- the ISSUE draws from the pinned lot
    exclusively, and a pinned lot held after pinning writes ``HELD_MATERIAL_CONSUMED``.
    """
    result = BackflushResult()

    part = work_order.part
    if part is None and work_order.part_id is not None:
        part = db.query(Part).filter(Part.id == work_order.part_id, Part.company_id == company_id).first()

    allocation_demand = _work_order_scoped_allocation_demand(db, work_order, company_id, allocations)
    backflush_enabled = part is not None and bool(getattr(part, "backflush_components", False))
    if not backflush_enabled and not allocation_demand:
        # Untied + not opted in -> exactly the pre-feature no-op.
        return result

    required_by_component: dict[int, float] = (
        _resolve_backflush_components(db, work_order, company_id, allocations, audit=audit) if backflush_enabled else {}
    )
    for part_id, demand in allocation_demand.items():
        required_by_component[part_id] = required_by_component.get(part_id, 0.0) + demand.quantity

    if not required_by_component:
        return result

    for component_part_id, required_qty in required_by_component.items():
        if required_qty <= _EPSILON:
            continue
        if _component_already_issued(db, work_order.id, component_part_id, company_id):
            # Idempotency: this component was already backflushed for this WO, and
            # ``uq_wo_inventory_issue`` permits exactly one ISSUE per (work order, part)
            # on this reference shape -- forever. So this is not merely "skip a replay":
            # it is the ONLY path by which a work-order-scoped tie that was fully
            # RETURNED becomes permanently un-issuable, because the returned ISSUE row
            # still satisfies this existence test while the material is physically back
            # on the shelf.
            #
            # RECORD IT. The demand being dropped here was resolved by THIS call from a
            # live BOM/routing, so the shop asked for material and got none -- silently,
            # with no ledger row, no shortage and no event. That is the same shape of
            # control gap ``_drop_ledger_covered_parts`` writes a chain row for, and
            # recording the recoverable suppression while staying silent on the
            # permanent one would be exactly backwards.
            _record_backflush_demand_suppressed(
                db,
                work_order,
                part_id=component_part_id,
                quantity=required_qty,
                company_id=company_id,
                audit=audit,
                reason="already_issued",
            )
            continue
        demand = allocation_demand.get(component_part_id)
        outcome = _issue_one_component(
            db,
            work_order,
            component_part_id=component_part_id,
            required_qty=required_qty,
            company_id=company_id,
            user_id=user_id,
            audit=audit,
            allocation_id=demand.allocation_ids[0] if demand else None,
            pinned_inventory_item_id=demand.pinned_inventory_item_id if demand else None,
        )
        result.issued_part_ids.append(component_part_id)
        if demand is not None and outcome.posted:
            # ONLY when an ISSUE row actually landed. On a duplicate no-op nothing was
            # inserted and nothing was decremented, so the winning transaction's rows own
            # the quantity -- advancing qty_consumed here would corrupt the cache the
            # untie 409 keys on, claiming consumption this call never posted.
            _mark_work_order_ties_consumed(db, work_order, demand, company_id=company_id, audit=audit)
        if outcome.shortage is not None:
            result.shortages.append(outcome.shortage)

    return result


def _mark_work_order_ties_consumed(
    db: Session,
    work_order: WorkOrder,
    demand: WorkOrderMaterialAllocationDemand,
    *,
    company_id: int,
    audit: AuditService,
) -> None:
    """Advance ``qty_consumed`` to ``qty_planned`` on the ties a WO-level ISSUE drained.

    The ledger is authoritative (see the model docstring); this keeps the denormalized
    cache honest so the untie guard ("409 if qty_consumed > 0") and the UI agree with
    the movement that just posted. Tenant-scoped; only flushes.

    AUDITED (invariant #2), mirroring the operation-scoped twin in
    ``material_consumption_service._consume_one_allocation``. This is a state change on a
    TenantMixin row, and it writes the exact field a later verb keys on: once
    ``qty_consumed > 0`` the untie endpoint refuses with 409. An unaudited write here
    would change what the system will later refuse, with nothing on the hash chain
    saying who or what changed it. ``log_update`` self-suppresses when the value did not
    actually move, so a replay adds no row.

    ONLY called when the ISSUE actually posted (``IssueOutcome.posted``) -- see the call
    site. A duplicate no-op inserted nothing, so there is no consumption to cache.
    """
    rows = (
        db.query(WorkOrderMaterialAllocation)
        .filter(
            WorkOrderMaterialAllocation.company_id == company_id,
            WorkOrderMaterialAllocation.id.in_(demand.allocation_ids),
        )
        .all()
    )
    if not rows:
        return

    part_numbers = {
        row_id: number
        for row_id, number in db.query(Part.id, Part.part_number)
        .filter(Part.company_id == company_id, Part.id.in_({row.part_id for row in rows}))
        .all()
    }
    updates: list[tuple[WorkOrderMaterialAllocation, float]] = []
    for row in rows:
        old_consumed = float(row.qty_consumed or 0)
        row.qty_consumed = float(row.qty_planned or 0)
        updates.append((row, old_consumed))
    db.flush()

    for row, old_consumed in updates:
        part_number = part_numbers.get(row.part_id)
        audit.log_update(
            "work_order_material_allocation",
            row.id,
            f"WO {work_order.work_order_number} / part {part_number or row.part_id}",
            old_values={"qty_consumed": old_consumed},
            new_values={"qty_consumed": row.qty_consumed},
            description=(
                f"Consumed {row.qty_consumed} {row.unit_of_measure} of part "
                f"{part_number or row.part_id} against work order {work_order.work_order_number} "
                "(work-order-scoped tie, drained by the completion backflush)"
            ),
            extra_data={
                "work_order_id": work_order.id,
                "reference_type": "work_order",
                "part_id": row.part_id,
                "allocation_id": row.id,
            },
        )


def apply_completion_inventory_effects(
    db: Session,
    work_order: WorkOrder,
    *,
    user_id: int,
    company_id: int,
    audit: AuditService,
) -> BackflushResult:
    """Run the full completion inventory effect: FG receipt ALWAYS, backflush if gated.

    Single entry point for the completion handlers. The FG receipt is always
    performed (idempotent); the backflush only runs when the finished part opts in.
    Returns the backflush result (shortages) so the caller can surface / log them.
    Does NOT commit -- the caller owns the transaction so these writes are atomic
    with the completion on the live paths.

    Material consumption for OPERATION-scoped ties runs here too, so it inherits every
    existing completion call site (kiosk clock-out, shop-floor + office op complete,
    force-complete, reconcile-on-read) WITHOUT adding one. It is a no-op -- not one
    write -- on an untied work order, and it never raises (see
    ``consume_tied_materials_for_work_order``). Imported lazily: that module imports
    helpers from THIS one.

    The work order's OPEN material ties are read ONCE here and threaded into both legs.
    Three separate legs used to issue the same tenant-scoped SELECT (the consume engine,
    the backflush-precedence drop, and the work-order-scoped demand), so a completion
    cost 2 unconditional reads of ``work_order_material_allocations`` plus a third when
    the part opted into backflush. It is now exactly ONE, tied or not.
    """
    from app.services.material_consumption_service import (
        consume_tied_materials_for_work_order,
        open_allocations_for_work_order,
    )

    allocations = open_allocations_for_work_order(db, work_order.id, company_id)

    receive_finished_goods_for_work_order(db, work_order, user_id=user_id, company_id=company_id, audit=audit)
    consume_tied_materials_for_work_order(
        db, work_order, user_id=user_id, company_id=company_id, audit=audit, allocations=allocations
    )
    return backflush_components_for_work_order(
        db, work_order, user_id=user_id, company_id=company_id, audit=audit, allocations=allocations
    )


def apply_operation_completion_inventory_effects(
    db: Session,
    work_order: WorkOrder,
    operation: WorkOrderOperation,
    *,
    user_id: int,
    company_id: int,
    audit: AuditService,
) -> None:
    """Run the inventory effects of ONE OPERATION completing. Today: tied consumption.

    The operation-level analogue of ``apply_completion_inventory_effects``, and the one
    seam the four operation-completion handlers call right after
    ``finalize_operation_completion``. It exists even though it currently does a single
    thing so that the NEXT operation-scoped inventory effect has an obvious home and
    lands on all four handlers at once -- the same reason its work-order-level sibling
    exists (CLAUDE.md: add completion side-effects at the seam, not at the call sites).

    **The finished-goods RECEIVE and the BOM backflush deliberately DO NOT fire here.**
    They are correctly WORK-ORDER-scoped -- the FG receipt books the job's produced
    quantity into stock against ``work_order.lot_number``, and the backflush is a
    one-shot ISSUE per (work order, part) physically constrained to one row by
    ``uq_wo_inventory_issue``. Firing either per operation would double-receive a
    multi-operation job and collide with that index. They stay in
    ``apply_completion_inventory_effects``; only operation-scoped TIED-MATERIAL
    consumption moved earlier, because that is the leg whose unit of work genuinely is
    the operation (a nest).

    Does NOT commit -- joins the caller's unit of work, so consumption lands atomically
    with the operation status change. Writes NOTHING at all when the operation carries no
    ties.

    **Propagates ``StaleDataError`` and nothing else.** Every other failure degrades into
    a recorded ``ALLOCATION_CONSUMPTION_FAILED`` row inside the engine; an optimistic-lock
    conflict (invariant 4) is re-raised so it becomes the calling handler's documented
    409 rather than a 200 that silently skipped a material deduction. Do NOT wrap this
    call in a bare ``except`` -- that would restore exactly the failure it prevents.
    Callers must gate on the work order not being terminal.
    Imported lazily: that module imports helpers from THIS one.
    """
    from app.services.material_consumption_service import consume_tied_materials_for_operation

    consume_tied_materials_for_operation(db, work_order, operation, user_id=user_id, company_id=company_id, audit=audit)


def _existing_shipment_ship_txn(db: Session, shipment_id: int, company_id: int) -> bool:
    """True if a SHIP txn for this shipment already exists (idempotency key, G2).

    The offsetting finished-goods decrement on ship is keyed on a prior SHIP
    ``InventoryTransaction`` with ``reference_type='shipment', reference_id=shipment.id``
    so a re-submitted / concurrent double-ship can never double-decrement. (There is no
    DB partial-unique index for SHIP txns this batch -- see the follow-up note in
    ``decrement_finished_goods_for_shipment`` -- so this application check, combined with
    the caller's ``with_for_update`` row lock on the shipment, is the idempotency guard.)
    """
    return (
        db.query(InventoryTransaction.id)
        .filter(
            InventoryTransaction.company_id == company_id,
            InventoryTransaction.reference_type == "shipment",
            InventoryTransaction.reference_id == shipment_id,
            InventoryTransaction.transaction_type == TransactionType.SHIP,
        )
        .first()
        is not None
    )


def _record_ship_discrepancy(
    db: Session,
    *,
    work_order: WorkOrder,
    shipment: Shipment,
    audit_action: str,
    event_type: str,
    description: str,
    extra: dict,
    company_id: int,
    user_id: int,
    audit: AuditService,
) -> None:
    """Persist a ship-side discrepancy as a tamper-evident audit row + warning event (G2).

    Mirrors ``_record_backflush_shortage``: ONE ``audit_log`` row on the immutable hash
    chain (never written directly) + a warning ``OperationalEvent`` for AI/realtime
    consumers. Tenant-scoped. Both only flush (never commit), so the records land
    atomically with the ship/close on the caller's unit of work; the event emit is
    wrapped so a transient signal failure can never fail an in-flight ship (the audit
    row is the compliance record). Used for BOTH the FG-not-found case and the over-ship
    case -- neither fails the ship (warn-and-record posture).
    """
    audit.log(
        action=audit_action,
        resource_type="shipment",
        resource_id=shipment.id,
        resource_identifier=shipment.shipment_number,
        description=description,
        new_values={"shipment_number": shipment.shipment_number},
        extra_data=extra,
        company_id=company_id,
    )
    try:
        OperationalEventService(db).emit(
            company_id=company_id,
            event_type=event_type,
            source_module="shipping",
            entity_type="shipment",
            entity_id=shipment.id,
            work_order_id=work_order.id,
            user_id=user_id,
            severity="warning",
            event_payload=extra,
        )
    except Exception:  # pragma: no cover - a warning signal must never fail a ship
        logger.exception(
            "%s event emit failed for shipment %s WO %s (company %s)",
            event_type,
            shipment.id,
            work_order.id,
            company_id,
        )


def record_over_ship_if_needed(
    db: Session,
    *,
    work_order: WorkOrder,
    shipment: Shipment,
    company_id: int,
    user_id: int,
    audit: AuditService,
) -> Optional[float]:
    """Warn-and-record over-ship (G2): cumulative shipped beyond produced quantity.

    Ceiling = ``WorkOrder.quantity_complete`` (what was actually produced/received into
    FG; there is no SalesOrder table). Cumulative shipped = SUM(``quantity_shipped``)
    over this WO's NON-CANCELLED shipments (this shipment is already SHIPPED at the call
    site, so it is included). If cumulative shipped exceeds the produced quantity, the
    ship is ALLOWED (no 400) but a warning ``OperationalEvent`` + tamper-evident audit
    row record the over-ship (mirrors the backflush-shortage recording). Tenant-scoped;
    joins the caller's unit of work (no commit). Returns the overage when one was
    recorded, else ``None``.
    """
    produced = float(work_order.quantity_complete or 0)
    cumulative = (
        db.query(func.coalesce(func.sum(Shipment.quantity_shipped), 0.0))
        .filter(
            Shipment.company_id == company_id,
            Shipment.work_order_id == work_order.id,
            Shipment.status != ShipmentStatus.CANCELLED,
        )
        .scalar()
    )
    cumulative_shipped = float(cumulative or 0.0)
    overage = cumulative_shipped - produced
    if overage <= _EPSILON:
        return None

    extra = {
        "work_order_id": work_order.id,
        "work_order_number": work_order.work_order_number,
        "shipment_number": shipment.shipment_number,
        "quantity_shipped": float(shipment.quantity_shipped or 0),
        "cumulative_shipped": cumulative_shipped,
        "quantity_complete": produced,
        "overage": overage,
    }
    _record_ship_discrepancy(
        db,
        work_order=work_order,
        shipment=shipment,
        audit_action=OVER_SHIP_AUDIT_ACTION,
        event_type=OVER_SHIP_EVENT_TYPE,
        description=(
            f"Over-ship on WO {work_order.work_order_number}: cumulative shipped {cumulative_shipped} "
            f"exceeds produced {produced} by {overage} (shipment {shipment.shipment_number})"
        ),
        extra=extra,
        company_id=company_id,
        user_id=user_id,
        audit=audit,
    )
    logger.warning(
        "Over-ship on WO %s (company %s): cumulative %s > produced %s by %s",
        work_order.id,
        company_id,
        cumulative_shipped,
        produced,
        overage,
    )
    return overage


def decrement_finished_goods_for_shipment(
    db: Session,
    *,
    work_order: WorkOrder,
    shipment: Shipment,
    company_id: int,
    user_id: int,
    audit: AuditService,
) -> Optional[InventoryTransaction]:
    """Write the offsetting finished-goods decrement when a shipment ships (G2).

    The mirror of Batch-6's ``receive_finished_goods_for_work_order``: that RECEIVEs the
    produced quantity into the FG lot on completion; this writes the negative SHIP txn
    and decrements on-hand/available when those finished goods leave on a shipment. The
    decremented row is located by ``company_id + part_id == work_order.part_id +
    location == FINISHED_GOODS_LOCATION + lot_number == work_order.lot_number`` -- exactly
    the row the receipt created (the constants are imported, not re-declared, so the two
    sides stay in lock-step).

    Idempotency + concurrency: the caller holds a ``with_for_update`` row lock on the
    shipment when it re-checks status, serializing concurrent double-ship; here we ALSO
    short-circuit if a SHIP txn for this shipment already exists
    (``_existing_shipment_ship_txn``) and insert under a savepoint so a duplicate is a
    clean no-op rather than aborting the ship/close. (FOLLOW-UP: a DB partial-unique
    index on ``(company_id, reference_type='shipment', reference_id, SHIP)`` would harden
    this the way ``uq_wo_inventory_receipt`` hardens the receipt; deferred -- this batch
    is migration-free.)

    FG-not-found: if no matching FINISHED-GOODS lot row exists (receipt skipped, lot
    changed, stock already moved), DO NOT fail the ship -- record a discrepancy warning
    (audit row + OperationalEvent) and return ``None``, leaving the caller to proceed
    with the ship/close (mirrors ``_record_backflush_shortage``).

    Does NOT commit -- joins the caller's unit of work so the decrement + audit land
    atomically with the SHIPPED status change + WO close. Returns the SHIP txn, or
    ``None`` on a no-op (already shipped, nothing to ship, or FG row not found).
    """
    if _existing_shipment_ship_txn(db, shipment.id, company_id):
        return None

    quantity = float(shipment.quantity_shipped or 0)
    if quantity <= 0:
        # Nothing to decrement (a zero-quantity shipment); not a discrepancy.
        return None

    lot_number = work_order.lot_number

    inv_item = (
        db.query(InventoryItem)
        .filter(
            InventoryItem.company_id == company_id,
            InventoryItem.part_id == work_order.part_id,
            InventoryItem.location == FINISHED_GOODS_LOCATION,
            InventoryItem.lot_number == lot_number,
        )
        .first()
        if lot_number
        else None
    )

    if inv_item is None:
        # FG-not-found: receipt was skipped, the lot changed, or stock already moved.
        # Warn-and-record (audit + event) and proceed with the ship/close -- never fail.
        part = db.query(Part).filter(Part.id == work_order.part_id, Part.company_id == company_id).first()
        extra = {
            "work_order_id": work_order.id,
            "work_order_number": work_order.work_order_number,
            "shipment_number": shipment.shipment_number,
            "part_id": work_order.part_id,
            "part_number": part.part_number if part else None,
            "lot_number": lot_number,
            "location": FINISHED_GOODS_LOCATION,
            "quantity_shipped": quantity,
        }
        _record_ship_discrepancy(
            db,
            work_order=work_order,
            shipment=shipment,
            audit_action=SHIP_FG_MISSING_AUDIT_ACTION,
            event_type=SHIP_FG_MISSING_EVENT_TYPE,
            description=(
                f"Ship of {quantity} for WO {work_order.work_order_number} found no finished-good lot "
                f"{lot_number or '(none)'} at {FINISHED_GOODS_LOCATION}; on-hand not decremented "
                f"(shipment {shipment.shipment_number})"
            ),
            extra=extra,
            company_id=company_id,
            user_id=user_id,
            audit=audit,
        )
        logger.warning(
            "Ship FG lot missing for WO %s lot %s (company %s); on-hand not decremented",
            work_order.id,
            lot_number,
            company_id,
        )
        return None

    part = db.query(Part).filter(Part.id == work_order.part_id, Part.company_id == company_id).first()
    part_number = part.part_number if part else None
    unit_cost = float(inv_item.unit_cost or (part.standard_cost if part else 0) or 0)

    # Insert the SHIP txn FIRST under a savepoint; the decrement applies ONLY when the
    # insert actually committed (mirrors the receipt/issue order). The savepoint also keeps
    # the outer ship/close transaction usable if a future DB unique index ever fires.
    txn = InventoryTransaction(
        company_id=company_id,
        inventory_item_id=inv_item.id,
        part_id=work_order.part_id,
        transaction_type=TransactionType.SHIP,
        quantity=-quantity,
        from_location=FINISHED_GOODS_LOCATION,
        lot_number=lot_number,
        reference_type="shipment",
        reference_id=shipment.id,
        reference_number=shipment.shipment_number,
        unit_cost=unit_cost,
        total_cost=quantity * unit_cost,
        notes=(f"Finished-goods shipment {shipment.shipment_number} for work order " f"{work_order.work_order_number}"),
        created_by=user_id,
    )
    if not _insert_txn_with_savepoint(db, txn):
        # A concurrent ship already wrote this shipment's SHIP txn -> idempotent no-op
        # (do NOT decrement; the winning txn owns the movement).
        return None

    old_quantity_on_hand = inv_item.quantity_on_hand
    inv_item.quantity_on_hand = float(inv_item.quantity_on_hand or 0) - quantity
    inv_item.quantity_available = inv_item.quantity_on_hand - float(inv_item.quantity_allocated or 0)
    db.flush()

    # Tamper-evident audit (hash chain) for the outbound stock movement (mirrors the
    # FG-receipt audit). Flushed inside the caller's unit of work.
    audit.log_create(
        "inventory",
        txn.id,
        str(txn.id),
        new_values=txn,
        description=(
            f"Shipped {quantity} of part {part_number or work_order.part_id} from {FINISHED_GOODS_LOCATION} "
            f"lot {lot_number} on shipment {shipment.shipment_number} (WO {work_order.work_order_number})"
        ),
    )
    if old_quantity_on_hand is not None:
        audit.log_update(
            "inventory",
            inv_item.id,
            f"{part_number or work_order.part_id} @ {FINISHED_GOODS_LOCATION}",
            old_values={"quantity_on_hand": old_quantity_on_hand},
            new_values={"quantity_on_hand": inv_item.quantity_on_hand},
            description=f"Shipment decrement: stock for part {part_number or work_order.part_id}",
        )

    return txn
