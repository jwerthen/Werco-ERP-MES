"""Material consumption — depleting stock tied to a work order as work completes.

The headline case: a laser nest operation tied to a sheet part consumes
``qty_per_run`` sheets for every completed (or scrapped) run. The tie itself is the
``WorkOrderMaterialAllocation`` row (``models/work_order_material.py``); this module
is the engine that turns a tie into ledger movement.

It deliberately matches the contract of its sibling ``completion_inventory_service``
(read that module's docstring first) and REUSES its helpers rather than
reimplementing them -- ``_insert_txn_with_savepoint``, ``_write_issue_txn`` and its
compensating twin ``_write_return_txn`` above all (the ledger write, the on-hand move
and the dual audit rows are that shared helper's, parameterized by reference shape,
sign and prose, not a copy of it):

* **No commit.** Every function joins the CALLER's unit of work; the completion
  handler owns ``db.commit()`` so consumption lands ATOMICALLY with the status
  change on the live paths.
* **Tenant-scoped.** Every read filters ``company_id`` (invariant #1); the caller
  passes the ACTIVE company.
* **Audited.** Every ``InventoryTransaction`` is logged through ``AuditService``
  (invariant #2) -- never a direct ``audit_log`` write.
* **Idempotent under REPLAY** (sequential re-entry). See below, including what that
  does and does not cover.

Sum-delta / reconcile-to-target
-------------------------------
For each OPEN operation-scoped allocation on the work order::

    target = qty_per_run * (op.quantity_complete + op.quantity_scrapped)
    delta  = target - allocation.qty_consumed
    delta > 0  -> post ISSUE for -delta, set qty_consumed = target
    delta <= 0 -> NO-OP

Because ``target`` is RECOMPUTED from live operation state on every call, a replay
converges instead of double-issuing: the second call sees ``delta == 0``. That is
why this path needs no ``uq_*`` index of its own (and why it deliberately posts with
``reference_type='work_order_operation'``, OUTSIDE the ``uq_wo_inventory_issue``
predicate). Since PR 4.4 the work-order backflush reconciles the same way, under its
own ``work_order_backflush`` shape -- so the reason for staying outside that index is no
longer "do not collide with the one-shot guard" but the plainer one both legs share: a
one-row-per-(company, WO, ISSUE, part) index cannot express spill-across-lots plus a
later top-up row. The two nets never overlap (this leg's rows key on an OPERATION id,
that leg's on the WORK ORDER id).

Two entry points, one engine
----------------------------
* ``consume_tied_materials_for_operation`` -- ONE just-completed operation's ties.
  This is where stock actually moves on the floor: a laser child work order carries one
  operation per nest, so nest 1 of 3 must deplete its sheet when nest 1 closes. Its
  scope is deliberately narrow for a SAFETY reason (a still-``IN_PROGRESS`` operation
  is still reducible, and consumption never auto-reverses) -- read that function's
  docstring before widening it.
* ``consume_tied_materials_for_work_order`` -- EVERY open operation-scoped tie on the
  work order, run from ``apply_completion_inventory_effects`` at work-order completion.
  It is now the SELF-HEAL rather than the only depletion moment: sum-delta means it
  recomputes ``target`` from live operation state and no-ops on whatever the
  per-operation call already posted.

Both funnel into ``_consume_allocation_under_savepoint`` -> ``_consume_one_allocation``,
so the savepoint boundary, the ``ALLOCATION_CONSUMPTION_FAILED`` record and the
arithmetic itself exist exactly once.

**What carries concurrency is the WORK-ORDER LOCK, not this engine.** The convergence
argument above is a SEQUENTIAL one: it holds for a re-entry that observes the previous
call's committed ``qty_consumed``. It does not, on its own, make two *simultaneous*
completions of one operation safe -- these rows sit outside ``uq_wo_inventory_issue``
by design and ``WorkOrderMaterialAllocation`` carries no ``version`` column, so nothing
here would stop both racers from computing the same positive delta and both posting.
What actually serializes them is invariant #4: ``WorkOrder`` and ``WorkOrderOperation``
map ``version_id_col`` directly, so every call site that drives a completion takes the
optimistic lock and a stale concurrent writer raises ``StaleDataError`` (-> HTTP 409)
before it can reach here. Treat that lock as load-bearing for this engine: a future
call site that mutates neither row would step outside the protection, and the
per-allocation savepoint is damage control, not a substitute.

**Scrap consumes.** A scrapped run physically used the sheet, so scrap is inside
``target``. It is posted as ``TransactionType.ISSUE``, NOT ``SCRAP``: lot genealogy
(``api/endpoints/traceability.py``) reconstructs consumed components by filtering
``transaction_type == ISSUE``, so a ``SCRAP`` row would make audited scrap material
VANISH from the as-built record -- an AS9100D traceability hole. The good/scrap
split is recorded in the transaction ``notes`` instead.

**Never auto-reverse.** A negative delta is a no-op, never a RETURN. The supervisor
reduce-over-count verb lowers ``quantity_complete`` after the sheet is already cut;
un-consuming it would be a lie. This matters because consumption also runs from a GET
(reconcile-on-read, ``shop_floor.py``) where there is no reason and the actor is whoever
happens to be reading -- exactly the context in which an automatic inventory reversal
would be unattributable.

Reversal is instead an explicit, reasoned, audited verb: ``return_tied_material`` below.

The RETURN verb
---------------
``return_tied_material`` is that same negative delta, performed by an ACTOR with a
REASON -- the "compensating transaction + required reason + audit" pattern the receiving
corrections established. It appends positive ``TransactionType.RETURN`` rows (never
mutating an ISSUE row), credits them back to the SOURCE lots the material came off, and
copies each row's ``unit_cost`` from the ISSUE row it compensates. It raises rather than
degrades: unlike everything above it, it only ever runs from a write handler.

There are exactly TWO intents and nothing between them, because a return that leaves
``qty_consumed`` below the live ``target`` on a still-OPEN tie is not a smaller return --
it is material the next completion (or the next reconcile-on-read GET) draws again,
re-running FIFO and possibly crediting a different lot into an as-built record. So
``correct_over_consumption`` is BOUNDED by ``qty_consumed - target`` and leaves the tie
live, while ``return_and_untie`` gives everything back and CANCELS the tie in the same
transaction. See ``schemas/work_order_material.MaterialReturnIntent``.

Read-safety
-----------
The reconcile-on-read callers wrap their inventory effects in a bare
``except: pass``. Swallowing an exception AFTER a failed flush would leave the
session poisoned and the caller's commit would raise ``PendingRollbackError``, so
every allocation's work runs inside its OWN ``begin_nested()`` savepoint: a failure
rolls back just that allocation and leaves the outer transaction usable. The
top-level entry point additionally never propagates. Degrade and record, never
break a GET.

Shortage posture
----------------
A shortage NEVER fails the completion. Production truth outranks inventory
bookkeeping -- refusing would train operators to untie material. Mirroring
``_record_backflush_shortage``: drive the source lot negative, write a tamper-evident
``ALLOCATION_SHORTAGE`` audit row, and emit a warning ``OperationalEvent``
(``material_allocation_shortage`` -> notification catalog ``material.allocation_shortage``).

The shortage row DISCLOSES why the draw could not reach further, and the sentence differs
by cause (``shortage_draw_disclosure`` picks; both engines share it). On an UNPINNED draw
it names the stock the predicate passed over (``held_stock_summary`` ->
``held_quantity_skipped`` / ``held_lot_numbers``): both engines skip held / inactive lots,
so without it a part whose stock is entirely segregated reports a bare shortage against
material physically on the rack, and the reader cannot tell a purchasing problem from an
MRB problem. On a PINNED draw it names the PIN instead (``pinned_lot``) -- there, held
stock is not the constraint, and saying it was would send MRB to release material whose
release changes nothing while never mentioning the available stock the pin excluded.

The same warn-and-record posture covers the other two things that can go wrong here,
because BOTH would otherwise be silent:

* **Held material consumed** (``HELD_MATERIAL_CONSUMED``). ``consumable_source_items``
  excludes lots that are inactive or not ``available``, but a PINNED lot bypasses FIFO
  entirely, and a lot can be quarantined/held AFTER it was pinned. Consumption still
  proceeds -- the sheet was physically cut, and refusing from a GET would be
  unattributable -- but consuming nonconforming/held material into an as-built record
  is an AS9100D 8.7 event, so it goes on the hash chain. The tie-time control is the
  other half: ``work_order_materials.py`` REFUSES a pin of a held/inactive lot (422),
  so this row can only ever mean "held after it was pinned". ``record_held_material_consumed``
  is exported because the WORK-ORDER-scoped leg in ``completion_inventory_service``
  honors pins too and owes the identical record.

  **Disclosure -- nothing in ``app/`` ever writes a held ``InventoryItem.status``.**
  No endpoint, schema or service in this codebase sets ``on_hold`` / ``quarantine`` /
  ``rejected`` on a stock row (``status`` is written only as ``"available"``, on
  creation), and there is no deactivation verb for a lot. So both halves of this control
  -- the 422 pin refusal and this audit row -- can only fire on data set OUTSIDE the
  application (a direct DB write, an import, or a future hold verb). They are built and
  tested ahead of the feature that will produce that state; do not read them as evidence
  that lot-hold state is live in-system today.
* **Consumption failed** (``ALLOCATION_CONSUMPTION_FAILED``). A per-allocation
  savepoint rollback used to leave nothing but a log line, so material that should
  have depleted and did not was invisible -- weaker treatment than the LESSER shortage
  condition gets. ``AuditService.log`` inserts under its OWN savepoint and never
  propagates, so it is safe to call on the outer transaction after that rollback.
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.db.ledger_filter import BACKFLUSH_REFERENCE_TYPE, OPERATION_REFERENCE_TYPE
from app.models.audit_log import AuditLog
from app.models.inventory import InventoryItem, InventoryTransaction, TransactionType
from app.models.part import Part
from app.models.work_order import WorkOrder, WorkOrderOperation
from app.models.work_order_material import AllocationStatus, WorkOrderMaterialAllocation
from app.schemas.work_order_material import MaterialReturnIntent
from app.services.audit_service import AuditService
from app.services.completion_inventory_service import (
    _EPSILON,
    _component_already_issued,
    _is_placeholder_stock_row,
    _placeholder_stock_row,
    _write_issue_txn,
    _write_return_txn,
    require_posted_issue,
)
from app.services.operational_event_service import OperationalEventService

logger = logging.getLogger(__name__)

# ``OPERATION_REFERENCE_TYPE`` / ``BACKFLUSH_REFERENCE_TYPE`` / ``work_order_ledger_filter``
# live in ``app.db.ledger_filter`` -- the predicate is a GENERIC ledger question ("which
# rows belong to this work order?") that job costing, analytics, genealogy and the ledger
# list endpoint all ask, and homing it here forced every one of them to import this whole
# engine (transitively ``completion_inventory_service`` + ``operational_event_service``)
# just to get a WHERE clause. Import them from there, NOT through this module: a re-export
# here would rebuild exactly the coupling the move removed.

# Tamper-evident audit action + operational-event type for a consumption that drove a
# source lot negative. Distinct from ``BACKFLUSH_SHORTAGE`` so the two mechanisms stay
# separable in the audit trail and in the notification catalog.
ALLOCATION_SHORTAGE_AUDIT_ACTION = "ALLOCATION_SHORTAGE"
ALLOCATION_SHORTAGE_EVENT_TYPE = "material_allocation_shortage"

# Tamper-evident audit action for consuming a PINNED lot that is on hold / quarantined /
# rejected / inactive. See the module docstring: the pin is refused at tie time, so this
# only fires when the lot was held AFTER it was pinned. Consumption still proceeds.
HELD_MATERIAL_CONSUMED_AUDIT_ACTION = "HELD_MATERIAL_CONSUMED"

# Tamper-evident audit action for an allocation whose consumption raised and was rolled
# back to its savepoint. Recorded so a FAILED depletion is at least as visible as a
# shortage (which already writes a chain row).
ALLOCATION_CONSUMPTION_FAILED_AUDIT_ACTION = "ALLOCATION_CONSUMPTION_FAILED"

# ...and its operational-event type, so the FAILED case is at least as loud as the lesser
# SHORTAGE case. A shortage emits ``material_allocation_shortage`` and reaches Purchasing;
# without this, "nothing depleted at all" reached nobody -- and on a database where
# ``chk_inventory_items_quantity_non_negative`` is live, every shortage becomes this.
ALLOCATION_CONSUMPTION_FAILED_EVENT_TYPE = "material_allocation_consumption_failed"

# The only ``InventoryItem.status`` a consumption may draw from without comment. Anything
# else (``on_hold`` / ``quarantine`` / ``rejected`` -- see ``models/inventory.py``) is
# nonconforming or held material under AS9100D 8.7.
AVAILABLE_ITEM_STATUS = "available"

# Public alias of the shared float-comparison epsilon (defined once in
# ``completion_inventory_service``). Exported so callers OUTSIDE the services layer --
# the tie endpoint's untie guard above all -- test "has anything been consumed?" exactly
# the way the engine does. These are ``Float`` columns; a bare ``> 0`` at the API
# boundary and an ``> _EPSILON`` inside the engine can disagree about the same row.
CONSUMPTION_EPSILON = _EPSILON

# The audit ``extra_data.reason`` a work-order SOFT DELETE stamps on the ties it
# auto-cancels. It is the ONLY marker distinguishing "cancelled because the work order
# was deleted" (which restore must undo) from every other cancellation -- a manual untie,
# or a nest re-import superseding the operation -- which restore must NOT resurrect.
WORK_ORDER_DELETED_CANCEL_REASON = "work_order_deleted"

# The same, for the nest-re-import wipe.
REIMPORT_CANCEL_REASON = "superseded_by_reimport"


@dataclass
class AllocationShortage:
    """A consumption that drove (or would have driven) a source lot negative.

    Recorded but NOT fatal -- see the module docstring's shortage posture.
    """

    allocation_id: int
    part_id: int
    part_number: Optional[str]
    required_quantity: float
    available_quantity: float
    shortfall: float


@dataclass
class MaterialConsumptionResult:
    """What one ``consume_tied_materials_for_work_order`` call actually did."""

    consumed_allocation_ids: list[int] = field(default_factory=list)
    transactions: list[InventoryTransaction] = field(default_factory=list)
    shortages: list[AllocationShortage] = field(default_factory=list)
    failed_allocation_ids: list[int] = field(default_factory=list)


def open_allocations_for_work_order(
    db: Session,
    work_order_id: int,
    company_id: int,
) -> list[WorkOrderMaterialAllocation]:
    """Every OPEN allocation on a work order, tenant-scoped (invariant #1).

    Index-backed by ``ix_wo_material_alloc_company_wo``. An UNTIED work order returns
    ``[]`` here and every caller then short-circuits with ZERO writes -- that is what
    keeps untied work orders byte-identical to their pre-feature behavior.

    Called ONCE per completion, by ``apply_completion_inventory_effects``, which threads
    the result into the consume engine, the backflush-precedence drop and the backflush's
    work-order-scoped tie leg. Call it directly only outside that seam.
    """
    return (
        db.query(WorkOrderMaterialAllocation)
        .filter(
            WorkOrderMaterialAllocation.company_id == company_id,
            WorkOrderMaterialAllocation.work_order_id == work_order_id,
            WorkOrderMaterialAllocation.status == AllocationStatus.OPEN,
        )
        .order_by(WorkOrderMaterialAllocation.id)
        .all()
    )


def _open_allocations_for_operation(
    db: Session,
    *,
    work_order_id: int,
    operation_id: int,
    company_id: int,
) -> list[WorkOrderMaterialAllocation]:
    """The OPEN ties scoped to ONE operation, tenant-scoped (invariant #1).

    The narrow sibling of ``open_allocations_for_work_order``, read by the
    operation-completion entry point. Index-backed by ``uq_wo_material_alloc_open_op``
    (``company_id, work_order_operation_id, part_id`` WHERE the row is operation-scoped
    and OPEN), which is exactly this predicate.

    ``work_order_id`` is filtered too, redundantly with the operation id: it is the same
    "the tie must actually belong to THIS work order" check the whole-work-order path
    makes by looking the operation up on the work order, so a tie whose two scopes have
    drifted apart can never consume against a work order it does not name.

    An UNTIED operation returns ``[]`` and the caller short-circuits with ZERO writes.
    """
    return (
        db.query(WorkOrderMaterialAllocation)
        .filter(
            WorkOrderMaterialAllocation.company_id == company_id,
            WorkOrderMaterialAllocation.work_order_id == work_order_id,
            WorkOrderMaterialAllocation.work_order_operation_id == operation_id,
            WorkOrderMaterialAllocation.status == AllocationStatus.OPEN,
        )
        .order_by(WorkOrderMaterialAllocation.id)
        .all()
    )


def is_consumable_item(item: InventoryItem) -> bool:
    """True when a lot may be consumed into product without a compliance comment.

    The PYTHON half of one policy; ``CONSUMABLE_ITEM_CLAUSES`` is the SQL half and the
    two are locked together by a parity test. Exposed so the tie endpoint can refuse a
    PIN of a held lot up front (a 422 at tie time, where a human is present to answer)
    instead of leaving the divergence to be discovered at consume time -- which runs from
    a GET, where refusing is not an option.

    A NULL ``status`` reads as ``available`` (the column's own default), so a legacy row
    written outside the ORM is not treated as held. The SQL agrees, via
    ``COALESCE(status, 'available')`` -- it did NOT before PR 4.4, and the disagreement
    was not benign: ``status`` has a Python-side ``default`` with no ``server_default``
    and no backfill, so a bare ``status = 'available'`` hid legacy stock from the engine,
    which then recorded a false shortage against material sitting on the rack.
    """
    return bool(item.is_active) and (item.status or AVAILABLE_ITEM_STATUS) == AVAILABLE_ITEM_STATUS


# THE consumable-stock predicate, in SQL. One tuple, splatted into every query that asks
# "which lots may this engine draw from?" -- the FIFO source read below, and the on-hand
# hint ``material_tie_view`` paints on the dispatch board and the kiosk queue. Sharing
# the clauses is the point; re-declaring them is the bug (a display that promises stock
# the engine refuses to touch, or hides stock it will).
#
# ``COALESCE(status, 'available')`` rather than ``status = 'available'``: see
# ``is_consumable_item``. It is a strict WIDENING -- it can never make a held lot
# consumable -- and it compiles identically on Postgres and SQLite, unlike the
# ``OR status IS NULL`` spelling it replaces conceptually.
#
# ``is_active == True`` is deliberately the SQL ``= TRUE`` comparison: it excludes NULL,
# and ``bool(None)`` is False in ``is_consumable_item``, so the two already agree.
CONSUMABLE_ITEM_CLAUSES = (
    InventoryItem.is_active == True,  # noqa: E712
    func.coalesce(InventoryItem.status, AVAILABLE_ITEM_STATUS) == AVAILABLE_ITEM_STATUS,
)


def consumable_source_items(db: Session, part_id: int, company_id: int) -> list[InventoryItem]:
    """FIFO-ordered consumable stock for a part (tenant-scoped).

    THE one lot-selection policy, shared by BOTH engines -- the operation-scoped tie
    engine here and the work-order backflush in ``completion_inventory_service``. Before
    PR 4.4 they contradicted each other: this walked ``received_date`` FIFO filtered on
    ``status = 'available'`` and spilled across lots, while the backflush took the
    LOWEST-ID active row, ignored ``status`` entirely and wrote one row for the whole
    demand. Two engines naming different heats for the same physical draw is an AS9100D
    8.5.2 problem, not a stylistic one.

    ``received_date ASC NULLS LAST, id ASC``: oldest receipt first, rows with no
    received date last (they cannot be ordered by age, so they are the fallback), and
    ``id`` as the deterministic tie-break.
    """
    return (
        db.query(InventoryItem)
        .filter(
            InventoryItem.company_id == company_id,
            InventoryItem.part_id == part_id,
            *CONSUMABLE_ITEM_CLAUSES,
            InventoryItem.quantity_on_hand > 0,
        )
        .order_by(
            InventoryItem.received_date.is_(None),
            InventoryItem.received_date.asc(),
            InventoryItem.id.asc(),
        )
        .all()
    )


def plan_stock_draw(
    source_items: list[InventoryItem], quantity: float
) -> tuple[list[tuple[InventoryItem, float]], float]:
    """Split a demand across candidate lots, in the order given. PURE -- no DB, no writes.

    Returns ``([(lot, take), ...], unmet_remainder)``. Takes of ``<= _EPSILON`` are
    skipped entirely (a lot at zero or negative on-hand contributes nothing and must not
    produce a zero-quantity ledger row), so the returned pairs are exactly the rows the
    caller should post.

    Extracted so the two engines share the SPILL ARITHMETIC while each keeps its own
    posting and recording prose. The remainder is the honest shortfall -- computed
    against the lots actually walked, not against a total that includes stock the
    predicate excluded.
    """
    draws: list[tuple[InventoryItem, float]] = []
    remaining = float(quantity)
    for item in source_items:
        if remaining <= _EPSILON:
            break
        take = min(remaining, float(item.quantity_on_hand or 0))
        if take <= _EPSILON:
            continue
        draws.append((item, take))
        remaining -= take
    return draws, remaining


def held_stock_summary(db: Session, part_id: int, company_id: int) -> tuple[float, list[str]]:
    """On-hand quantity + lot numbers of this part's stock that is NOT consumable.

    ONE extra query, run ONLY when a shortage is about to be recorded. It is what stops a
    shortage row lying by omission: both engines now SKIP held / inactive lots rather
    than drawing from them (AS9100D 8.7), so a part whose stock is entirely
    ``on_hold`` / ``quarantine`` / ``rejected`` would otherwise report a bare shortage
    against material physically sitting on the rack. The chain row instead reads "short
    40; 60 on hand in segregated status, lots ...", which is the difference between a
    purchasing signal and an MRB signal.

    Tenant-scoped (invariant #1). Only rows with POSITIVE on-hand are counted -- a held
    lot at zero discloses nothing.
    """
    rows = (
        db.query(InventoryItem.lot_number, InventoryItem.quantity_on_hand)
        .filter(
            InventoryItem.company_id == company_id,
            InventoryItem.part_id == part_id,
            InventoryItem.quantity_on_hand > 0,
            # The NEGATION of ``CONSUMABLE_ITEM_CLAUSES``, written NULL-safely rather
            # than as ``~and_(*CONSUMABLE_ITEM_CLAUSES)``. SQL three-valued logic makes
            # the bare negation wrong: ``is_active = TRUE`` evaluates to NULL (not FALSE)
            # on a row whose ``is_active`` is NULL, and ``NOT NULL`` is NULL -- so such a
            # row would fall out of BOTH sets: not consumable (correct, ``bool(None)`` is
            # False) and not held either, vanishing from the disclosure this helper
            # exists to write. ``is_active`` has a Python-side default and no
            # ``server_default``, exactly like ``status``.
            or_(
                func.coalesce(InventoryItem.is_active, False) == False,  # noqa: E712
                func.coalesce(InventoryItem.status, AVAILABLE_ITEM_STATUS) != AVAILABLE_ITEM_STATUS,
            ),
        )
        .order_by(InventoryItem.id)
        .all()
    )
    total = sum(float(quantity or 0) for _, quantity in rows)
    lots = [lot for lot, _ in rows if lot]
    return total, lots


def shortage_draw_disclosure(
    db: Session,
    *,
    part_id: int,
    company_id: int,
    pinned_inventory_item_id: Optional[int],
    pinned_item: Optional[InventoryItem],
) -> tuple[float, list[str], Optional[str]]:
    """What a shortage row may TRUTHFULLY say about the stock the draw did not take.

    Returns ``(held_quantity_skipped, held_lot_numbers, pinned_lot)``, of which at most
    one half is ever populated -- because an unpinned draw and a pinned draw come up short
    for entirely different reasons, and saying the wrong one sends the reader to the wrong
    remedy:

    * **UNPINNED** -- every consumable lot WAS walked, so anything still on the rack is
      there because the predicate skipped it (on hold / quarantine / rejected / inactive).
      ``held_stock_summary`` is queried and disclosed: "short 40; 60 on hand in segregated
      status" is the difference between a purchasing signal and an MRB signal.
    * **PINNED** -- the reason no other lot was drawn is THE PIN, not any lot's status.
      Disclosing held stock here would be false by implication twice over: it tells an MRB
      reviewer that releasing the quarantined material clears the shortage (it does not --
      the pin still excludes it), and it says nothing about the freely-available stock in
      other heats that the pin is what excluded. So the held query is not even RUN, and
      the clause names the restriction instead.

    Shared by both engines' shortage paths from this one helper so the two can never
    drift, exactly as ``held_stock_summary`` / ``held_stock_disclosure`` are.
    """
    if pinned_inventory_item_id is None:
        held_quantity_skipped, held_lot_numbers = held_stock_summary(db, part_id, company_id)
        return held_quantity_skipped, held_lot_numbers, None
    # A pin that does not resolve (deleted row, other tenant) still RESTRICTED the draw,
    # so it is still the honest explanation; label it by the item id when there is no lot
    # number to name -- the inventory row IS the lot, named or not.
    if pinned_item is not None and pinned_item.lot_number:
        return 0.0, [], pinned_item.lot_number
    return 0.0, [], f"#{pinned_inventory_item_id}"


def held_stock_disclosure(
    held_quantity_skipped: float,
    held_lot_numbers: list[str],
    pinned_lot: Optional[str] = None,
) -> str:
    """The shortage-description clause explaining what the draw did NOT take. ``""`` if none.

    The PROSE half of what ``shortage_draw_disclosure`` computes, shared by both engines'
    shortage recorders for the same reason the summary itself is: an auditor comparing two
    chain rows should not have to work out whether two different sentences mean the same
    thing.

    ``pinned_lot`` and the held pair are mutually exclusive by construction (see
    ``shortage_draw_disclosure``); the pinned clause wins if both are somehow supplied,
    because on a pinned draw the held quantity is not the constraint.
    """
    if pinned_lot is not None:
        return f"; draw restricted to pinned lot {pinned_lot}, other stock not eligible"
    if held_quantity_skipped <= _EPSILON:
        return ""
    clause = f"; {held_quantity_skipped} on hand in segregated status (not drawn)"
    if held_lot_numbers:
        clause += f", lots {', '.join(held_lot_numbers)}"
    return clause


def _post_consumption_txn(
    db: Session,
    *,
    work_order: WorkOrder,
    operation: WorkOrderOperation,
    allocation: WorkOrderMaterialAllocation,
    inventory_item: InventoryItem,
    quantity: float,
    company_id: int,
    user_id: int,
    audit: AuditService,
    part_number: Optional[str],
    notes: str,
) -> InventoryTransaction:
    """Write ONE negative ISSUE against a source lot, decrement it, and audit.

    A THIN adapter over the shared ``_write_issue_txn`` -- it supplies only what differs
    on this leg (the operation reference shape, the run-scaled note, the audit prose and
    the tie/operation ids in ``extra_data``). The construct -> savepoint -> decrement ->
    dual-audit sequence itself, including recomputing ``quantity_available``, belongs to
    the shared helper; this used to be a near-verbatim second copy of it.

    **``duplicate_is_noop=False``, and that is a bug fix, not a preference.** These rows
    post under ``work_order_operation``, which NO unique index has ever covered -- so an
    ``IntegrityError`` here is a real fault (an FK, a NOT NULL, or
    ``chk_inventory_items_quantity_non_negative`` if it is live), never the
    concurrent-duplicate the savepoint discipline exists for. The default (``True``)
    swallowed it into "a concurrent completion already wrote this row" and returned
    ``None``, which this leg then treated as a lot that satisfied nothing -- a real fault
    silently degraded into a shortage. It has been doing that on a LIVE path since PR 1.
    ``_write_return_txn``'s docstring already states the rule this now follows.

    Consequently it NEVER returns ``None``: a fault raises into the caller's
    per-allocation savepoint and becomes an ``ALLOCATION_CONSUMPTION_FAILED`` chain row.
    The narrowing goes through the SHARED ``require_posted_issue`` so both component legs
    state that invariant the same way -- this one used to raise a hand-rolled
    ``RuntimeError`` while the backflush discarded the return entirely.
    """
    return require_posted_issue(
        _write_issue_txn(
            db,
            work_order,
            inventory_item=inventory_item,
            component_part_id=allocation.part_id,
            quantity=quantity,
            unit_cost=float(inventory_item.unit_cost or 0),
            lot_number=inventory_item.lot_number,
            company_id=company_id,
            user_id=user_id,
            audit=audit,
            part_number=part_number,
            allocation_id=allocation.id,
            reference_type=OPERATION_REFERENCE_TYPE,
            reference_id=operation.id,
            notes=notes,
            movement_verb="Consumed",
            movement_label="Material consumption",
            movement_suffix=f" operation {operation.operation_number or operation.id}",
            extra_data={"allocation_id": allocation.id, "work_order_operation_id": operation.id},
            duplicate_is_noop=False,
        ),
        what=(f"Material consumption transaction for allocation {allocation.id} on work order {work_order.id}"),
    )


def _record_allocation_shortage(
    db: Session,
    *,
    work_order: WorkOrder,
    operation: WorkOrderOperation,
    shortage: AllocationShortage,
    consumed_lot: Optional[str],
    company_id: int,
    user_id: int,
    audit: AuditService,
    held_quantity_skipped: float = 0.0,
    held_lot_numbers: Optional[list[str]] = None,
    pinned_lot: Optional[str] = None,
) -> None:
    """Persist a consumption shortage as a tamper-evident audit row + warning event.

    Mirrors ``_record_backflush_shortage``: ONE ``audit_log`` row (action
    ``ALLOCATION_SHORTAGE``) on the immutable hash chain -- never a direct table write --
    plus a ``material_allocation_shortage`` ``OperationalEvent`` (severity ``warning``),
    which the notification catalog maps to ``material.allocation_shortage``. Both only
    flush, so they land atomically with the completion; the emit is best-effort so a
    signal failure can never fail an in-flight completion (the audit row is the
    compliance record).

    ``held_quantity_skipped`` / ``held_lot_numbers`` disclose stock the lot-selection
    predicate PASSED OVER (``held_stock_summary``). Without them a part whose stock is
    entirely ``on_hold`` / ``quarantine`` / ``rejected`` reports a bare shortage against
    material physically on the rack, and the reader cannot tell a purchasing problem from
    an MRB problem. ``pinned_lot`` is the mutually exclusive alternative, populated on a
    PINNED draw where the reason no other lot was drawn is the pin rather than any lot's
    status -- see ``shortage_draw_disclosure``, which decides between them. Written from
    the same helper as the backflush leg's so the two cannot drift.
    """
    held_lot_numbers = held_lot_numbers or []
    extra = {
        "work_order_id": work_order.id,
        "work_order_number": work_order.work_order_number,
        "work_order_operation_id": operation.id,
        "operation_number": operation.operation_number,
        "allocation_id": shortage.allocation_id,
        "part_id": shortage.part_id,
        "part_number": shortage.part_number,
        "required_quantity": shortage.required_quantity,
        "available_quantity": shortage.available_quantity,
        "shortfall": shortage.shortfall,
        "consumed_lot": consumed_lot,
        "held_quantity_skipped": held_quantity_skipped,
        "held_lot_numbers": held_lot_numbers,
        "pinned_lot": pinned_lot,
    }
    audit.log(
        action=ALLOCATION_SHORTAGE_AUDIT_ACTION,
        resource_type="inventory",
        resource_id=shortage.part_id,
        resource_identifier=shortage.part_number or str(shortage.part_id),
        description=(
            f"Material shortage on WO {work_order.work_order_number} operation "
            f"{operation.operation_number or operation.id}: part "
            f"{shortage.part_number or shortage.part_id} short {shortage.shortfall} "
            f"(required {shortage.required_quantity}, available {shortage.available_quantity})"
            + (f", lot {consumed_lot}" if consumed_lot else "")
            + held_stock_disclosure(held_quantity_skipped, held_lot_numbers, pinned_lot)
        ),
        new_values={"shortfall": shortage.shortfall},
        extra_data=extra,
        company_id=company_id,
    )
    OperationalEventService(db).emit_best_effort(
        company_id=company_id,
        event_type=ALLOCATION_SHORTAGE_EVENT_TYPE,
        source_module="material_consumption",
        entity_type="inventory",
        entity_id=shortage.part_id,
        work_order_id=work_order.id,
        operation_id=operation.id,
        user_id=user_id,
        severity="warning",
        event_payload=extra,
    )


def record_held_material_consumed(
    *,
    work_order: WorkOrder,
    operation: Optional[WorkOrderOperation],
    allocation_id: Optional[int],
    part_id: Optional[int],
    item: InventoryItem,
    part_number: Optional[str],
    quantity: float,
    company_id: int,
    audit: AuditService,
    pinned: bool = True,
) -> None:
    """Record that a held/inactive lot was consumed anyway (AS9100D 8.7).

    Consumption is NOT refused: this path also runs from a reconcile-on-read GET, the
    material is already physically in the part, and refusing would leave the ledger
    lying about what was burned. What is NOT acceptable is doing it silently -- so the
    fact goes on the tamper-evident hash chain, naming the lot, its status and the tie,
    for the MRB/segregation review that has to follow.

    PUBLIC (no leading underscore) and ``operation``-optional because TWO call sites owe
    this row: the per-run engine below, and the work-order backflush's pinned branch in
    ``completion_inventory_service._issue_one_component``. A work-order-scoped tie has no
    operation, so the row simply omits it.

    ``pinned`` distinguishes the two, and since PR 4.4 only ``True`` is reachable:

    * ``True`` (both PINNED branches) -- only reachable when the lot was held AFTER it
      was pinned, since the tie endpoint refuses to pin a non-``available`` or inactive
      lot in the first place.
    * ``False`` -- the historical UNPINNED work-order-scoped branch, which selected the
      lowest-id active on-hand lot with NO ``status`` predicate and could therefore pick
      an already-held lot. That branch is gone: both engines now share
      ``consumable_source_items``, which SKIPS held lots (the AS9100D 8.7-correct
      behaviour) and discloses the skipped quantity on the shortage row instead
      (``held_stock_summary``). The parameter is kept because the flag is a
      records-integrity discriminator on rows already written under the old rule; do not
      re-point an unpinned selection at it.
    """
    scope = f" operation {operation.operation_number or operation.id}" if operation is not None else ""
    audit.log(
        action=HELD_MATERIAL_CONSUMED_AUDIT_ACTION,
        resource_type="inventory",
        resource_id=item.id,
        resource_identifier=item.lot_number or str(item.id),
        description=(
            f"Consumed {quantity} of part {part_number or part_id} from "
            f"{'PINNED lot' if pinned else 'lot'} "
            f"{item.lot_number or item.id} on WO {work_order.work_order_number}{scope} "
            f"while that lot was {'inactive' if not item.is_active else item.status}"
        ),
        new_values={"status": item.status, "is_active": bool(item.is_active)},
        extra_data={
            "work_order_id": work_order.id,
            "work_order_number": work_order.work_order_number,
            "work_order_operation_id": operation.id if operation is not None else None,
            "allocation_id": allocation_id,
            "part_id": part_id,
            "part_number": part_number,
            "inventory_item_id": item.id,
            "lot_number": item.lot_number,
            "item_status": item.status,
            "item_is_active": bool(item.is_active),
            "quantity": quantity,
            "pin_directed": pinned,
        },
        company_id=company_id,
    )
    logger.warning(
        "Held material consumed: WO %s op %s allocation %s %s lot %s (status=%s active=%s, company %s)",
        work_order.id,
        operation.id if operation is not None else None,
        allocation_id,
        "pinned" if pinned else "unpinned",
        item.lot_number or item.id,
        item.status,
        item.is_active,
        company_id,
    )


def _consume_one_allocation(
    db: Session,
    *,
    work_order: WorkOrder,
    allocation: WorkOrderMaterialAllocation,
    operation: WorkOrderOperation,
    company_id: int,
    user_id: int,
    audit: AuditService,
    result: MaterialConsumptionResult,
) -> None:
    """Reconcile ONE operation-scoped allocation to its target (the sum-delta rule).

    Lot selection: a PINNED allocation consumes from that lot only -- pinning is a
    lot-directed instruction, so an insufficient pinned lot is driven negative rather
    than silently spilling onto a different (uncertified, wrong-heat) lot. An UNPINNED
    allocation walks ``consumable_source_items`` -- THE shared FIFO/consumable policy,
    now used by the work-order backflush too -- spilling across lots via
    ``plan_stock_draw`` when the head lot cannot cover the delta.

    The pin bypasses FIFO's ordering, NOT its hold check. A pinned lot that is inactive
    or not ``available`` (on hold / quarantined / rejected) is still consumed -- see
    ``record_held_material_consumed`` for why refusing here would be worse -- but the
    fact is written to the tamper-evident chain instead of passing silently.
    """
    good = float(operation.quantity_complete or 0)
    scrapped = float(operation.quantity_scrapped or 0)
    # COALESCE(qty_per_run, 1.0) -- a NULL on an operation-scoped row means
    # "not run-scaled" per the model docstring.
    per_run = float(allocation.qty_per_run if allocation.qty_per_run is not None else 1.0)
    target = per_run * (good + scrapped)
    delta = target - float(allocation.qty_consumed or 0)
    if delta <= _EPSILON:
        # Includes the reduce-over-count case (target fell below what was consumed):
        # a NO-OP, never an auto-reversal. See the module docstring.
        return

    part = db.query(Part).filter(Part.id == allocation.part_id, Part.company_id == company_id).first()
    part_number = part.part_number if part else None
    notes = (
        f"Material consumption for work order {work_order.work_order_number} operation "
        f"{operation.operation_number or operation.id}: {good} complete + {scrapped} scrapped runs "
        f"@ {per_run} {allocation.unit_of_measure}/run"
    )

    held_item: Optional[InventoryItem] = None
    if allocation.pinned_inventory_item_id is not None:
        pinned = (
            db.query(InventoryItem)
            .filter(
                InventoryItem.id == allocation.pinned_inventory_item_id,
                InventoryItem.company_id == company_id,
            )
            .first()
        )
        source_items = [pinned] if pinned is not None else []
        if pinned is not None and not is_consumable_item(pinned):
            # Held AFTER it was pinned (the tie endpoint refuses to pin a held lot).
            # Consume anyway, but never silently -- recorded below, once the quantity
            # actually taken from this lot is known.
            held_item = pinned
    else:
        source_items = consumable_source_items(db, allocation.part_id, company_id)

    available_total = sum(float(i.quantity_on_hand or 0) for i in source_items)

    draws, remaining = plan_stock_draw(source_items, delta)
    posted_any = False
    last_item: Optional[InventoryItem] = None
    for item, take in draws:
        # ``_post_consumption_txn`` posts with ``duplicate_is_noop=False``: it either
        # returns the row or raises into this allocation's savepoint. There is no
        # "duplicate no-op" branch to handle any more, and there never should have been
        # -- no unique index has ever covered this reference shape, so the swallowed
        # IntegrityError it used to absorb was always a real fault.
        result.transactions.append(
            _post_consumption_txn(
                db,
                work_order=work_order,
                operation=operation,
                allocation=allocation,
                inventory_item=item,
                quantity=take,
                company_id=company_id,
                user_id=user_id,
                audit=audit,
                part_number=part_number,
                notes=notes,
            )
        )
        last_item = item
        posted_any = True

    if remaining > _EPSILON:
        # SHORTAGE: never fail the completion. Drive a lot negative so the true demand
        # is still on the ledger, then record it tamper-evidently + emit the warning.
        unit_cost = float(part.standard_cost or 0) if part else 0.0
        # The anchor guard stays in THIS form. ``source_items`` is empty on exactly the
        # no-stock-at-all path -- which IS the shortage path -- so a bare
        # ``source_items[0]`` would raise ``IndexError`` precisely when this branch runs.
        target_item = last_item or (source_items[0] if source_items else None)
        if target_item is None:
            target_item = _placeholder_stock_row(
                db, part_id=allocation.part_id, company_id=company_id, unit_cost=unit_cost
            )
        result.transactions.append(
            _post_consumption_txn(
                db,
                work_order=work_order,
                operation=operation,
                allocation=allocation,
                inventory_item=target_item,
                quantity=remaining,
                company_id=company_id,
                user_id=user_id,
                audit=audit,
                part_number=part_number,
                notes=f"{notes} (SHORT {remaining})",
            )
        )
        posted_any = True
        # AT MOST one extra query, only on this branch: on an UNPINNED draw, the stock the
        # predicate passed over, so the chain row cannot report a bare shortage against
        # segregated material that is physically on the rack. On a PINNED draw the pin is
        # the constraint, not any lot's status -- no query, and the clause says so.
        held_quantity_skipped, held_lot_numbers, pinned_lot = shortage_draw_disclosure(
            db,
            part_id=allocation.part_id,
            company_id=company_id,
            pinned_inventory_item_id=allocation.pinned_inventory_item_id,
            pinned_item=source_items[0] if allocation.pinned_inventory_item_id is not None and source_items else None,
        )
        shortage = AllocationShortage(
            allocation_id=allocation.id,
            part_id=allocation.part_id,
            part_number=part_number,
            required_quantity=delta,
            available_quantity=available_total,
            shortfall=remaining,
        )
        result.shortages.append(shortage)
        logger.warning(
            "Material shortage on WO %s op %s allocation %s (company %s): required %s, available %s, short %s",
            work_order.id,
            operation.id,
            allocation.id,
            company_id,
            delta,
            available_total,
            remaining,
        )
        _record_allocation_shortage(
            db,
            work_order=work_order,
            operation=operation,
            shortage=shortage,
            consumed_lot=target_item.lot_number,
            company_id=company_id,
            user_id=user_id,
            audit=audit,
            held_quantity_skipped=held_quantity_skipped,
            held_lot_numbers=held_lot_numbers,
            pinned_lot=pinned_lot,
        )

    if not posted_any:
        # DEFENSIVE, and honestly unreachable as written: ``delta > _EPSILON`` is already
        # guaranteed above, and ``plan_stock_draw`` returns the demand UNCHANGED as its
        # remainder when it draws nothing, so either a take posted or the shortage branch
        # did. Kept as a bare guard on the one thing that must never happen -- advancing
        # ``qty_consumed`` against zero movement -- not as a description of a live case.
        return

    if held_item is not None:
        # A pin has exactly ONE source lot, so the whole delta came off the held lot
        # (whether through the normal take or by driving it negative). Recorded only
        # now, once we know a row actually posted.
        record_held_material_consumed(
            work_order=work_order,
            operation=operation,
            allocation_id=allocation.id,
            part_id=allocation.part_id,
            item=held_item,
            part_number=part_number,
            quantity=delta,
            company_id=company_id,
            audit=audit,
        )

    old_consumed = float(allocation.qty_consumed or 0)
    # The TARGET, not the ledger net -- and deliberately NOT the ledger-backed form
    # ``completion_inventory_service._advance_tie_consumed`` adopted for work-order-scoped
    # ties in PR 4.4. This engine's own ``delta`` is ``target - qty_consumed``, so the
    # cache is an INPUT to the next pass here, where there it is only an output. Closing
    # residual 4 on this leg therefore changes the arithmetic, not just the stored value,
    # and is out of PR 4.4's scope. The consequence to know: the two engines now mean
    # different things by ``qty_consumed`` (see that function's docstring).
    allocation.qty_consumed = target
    db.flush()
    audit.log_update(
        "work_order_material_allocation",
        allocation.id,
        f"WO {work_order.work_order_number} / part {part_number or allocation.part_id}",
        old_values={"qty_consumed": old_consumed},
        new_values={"qty_consumed": allocation.qty_consumed},
        description=(
            f"Consumed {delta} {allocation.unit_of_measure} of part "
            f"{part_number or allocation.part_id} against work order "
            f"{work_order.work_order_number} operation {operation.operation_number or operation.id}"
        ),
        extra_data={"work_order_id": work_order.id, "work_order_operation_id": operation.id},
    )
    result.consumed_allocation_ids.append(allocation.id)


def _record_consumption_failed(
    db: Session,
    *,
    work_order: WorkOrder,
    allocation_id: int,
    part_id: Optional[int],
    operation_id: Optional[int],
    error: BaseException,
    company_id: int,
    user_id: Optional[int],
    audit: AuditService,
) -> None:
    """Record a rolled-back consumption on the tamper-evident chain AND notify.

    Material that SHOULD have depleted and did not is a material-trail control gap --
    strictly worse than the shortage case, which already writes a chain row. Without
    this the only trace was a log line the compliance record never sees.

    **The warning ``OperationalEvent`` is the other half of that argument.** The audit row
    alone left the degraded path quieter than the lesser condition it degrades from: a
    shortage emits ``material_allocation_shortage`` and reaches Purchasing's inbox, while
    "nothing was consumed at all" reached nobody. On a database where
    ``chk_inventory_items_quantity_non_negative`` is live, every shortage arrives here
    instead -- so without this the notification would be missing on exactly the deployment
    that needs it. ``material.allocation_consumption_failed`` carries it.

    Safe on the post-``nested.rollback()`` outer transaction: ``AuditService.log`` opens
    its OWN savepoint around the INSERT and swallows every failure (returns ``None``),
    so it can neither propagate nor re-poison the session, and the emit is
    ``best_effort``. Still wrapped defensively -- a reconcile-on-read GET must never 500
    because the failure record failed.
    """
    extra = {
        "work_order_id": work_order.id,
        "work_order_number": work_order.work_order_number,
        "work_order_operation_id": operation_id,
        "allocation_id": allocation_id,
        "part_id": part_id,
        "error": f"{type(error).__name__}: {error}"[:500],
    }
    try:
        audit.log(
            action=ALLOCATION_CONSUMPTION_FAILED_AUDIT_ACTION,
            resource_type="work_order_material_allocation",
            resource_id=allocation_id,
            resource_identifier=f"WO {work_order.work_order_number} / allocation {allocation_id}",
            description=(
                f"Material consumption FAILED and was rolled back for allocation {allocation_id} on work "
                f"order {work_order.work_order_number}; tied material was NOT depleted"
            ),
            success=False,
            error_message=f"{type(error).__name__}: {error}"[:500],
            extra_data=extra,
            company_id=company_id,
        )
    except Exception:  # pragma: no cover - the record must never break the caller
        logger.exception(
            "Failed to record ALLOCATION_CONSUMPTION_FAILED for allocation %s on WO %s (company %s)",
            allocation_id,
            work_order.id,
            company_id,
        )
    # Emitted under its OWN savepoint, for the reason spelled out at the twin recorder in
    # ``completion_inventory_service._record_backflush_component_failed``: a ``flush()``
    # that fails at the DB deactivates the outer transaction even though
    # ``emit_best_effort`` swallows the exception, and this recorder runs inside a
    # per-allocation loop that must survive to the next allocation. A ``None`` return is
    # the documented failure signal.
    event_savepoint = db.begin_nested()
    if (
        OperationalEventService(db).emit_best_effort(
            company_id=company_id,
            event_type=ALLOCATION_CONSUMPTION_FAILED_EVENT_TYPE,
            source_module="material_consumption",
            entity_type="work_order_material_allocation",
            entity_id=allocation_id,
            work_order_id=work_order.id,
            # Deliberately NOT ``operation_id``: ``emit`` validates it against the tenant with
            # a query, and this path runs after a savepoint rollback where the cheapest
            # possible touch is the right posture. The operation id is in the payload.
            user_id=user_id,
            severity="warning",
            event_payload=extra,
        )
        is None
    ):
        event_savepoint.rollback()
    else:
        event_savepoint.commit()


def consume_tied_materials_for_work_order(
    db: Session,
    work_order: WorkOrder,
    *,
    user_id: int,
    company_id: int,
    audit: AuditService,
    allocations: Optional[list[WorkOrderMaterialAllocation]] = None,
) -> MaterialConsumptionResult:
    """Reconcile every OPEN operation-scoped allocation on a WO to its target.

    The single entry point wired into ``apply_completion_inventory_effects`` so it
    inherits every existing completion call site without adding one.

    UNTIED WORK ORDERS ARE UNTOUCHED: with no allocation rows this returns immediately --
    no inventory row, no ledger row, no audit row, no event. That equivalence is the
    single most important property of this feature and is locked by a test. The OPEN ties
    are read ONCE by ``apply_completion_inventory_effects`` and passed in as
    ``allocations``; omit the argument and this reads them itself.

    Does NOT commit (joins the caller's unit of work) and NEVER raises: each allocation
    runs inside its own SAVEPOINT so a failure rolls back only that allocation and
    leaves the outer transaction usable, and the ENTIRE body -- the operation lookup and
    the ``begin_nested()`` included, both of which sat outside the guard and could 500 a
    live completion -- runs under the wrapper below.
    """
    result = MaterialConsumptionResult()
    try:
        _consume_tied_materials(
            db,
            work_order,
            user_id=user_id,
            company_id=company_id,
            audit=audit,
            result=result,
            allocations=allocations,
        )
    except Exception:  # pragma: no cover - degrade, never break a completion or a GET
        logger.exception(
            "Material consumption aborted for WO %s (company %s)",
            work_order.id,
            company_id,
        )
    return result


def _consume_tied_materials(
    db: Session,
    work_order: WorkOrder,
    *,
    user_id: int,
    company_id: int,
    audit: AuditService,
    result: MaterialConsumptionResult,
    allocations: Optional[list[WorkOrderMaterialAllocation]] = None,
) -> None:
    """The body of ``consume_tied_materials_for_work_order``; may raise (wrapped there)."""
    if allocations is None:
        allocations = open_allocations_for_work_order(db, work_order.id, company_id)
    if not allocations:
        return

    operation_ids = [a.work_order_operation_id for a in allocations if a.work_order_operation_id is not None]
    if not operation_ids:
        # Work-order-scoped ties only; those drain through leg 2 of
        # ``backflush_components_for_work_order`` (completion_inventory_service), which
        # reconciles them against ``qty_planned``, not this per-run engine.
        return

    operations = {
        op.id: op
        for op in db.query(WorkOrderOperation)
        .filter(
            WorkOrderOperation.company_id == company_id,
            WorkOrderOperation.work_order_id == work_order.id,
            WorkOrderOperation.id.in_(operation_ids),
        )
        .all()
    }

    for allocation in allocations:
        if allocation.work_order_operation_id is None:
            continue
        operation = operations.get(allocation.work_order_operation_id)
        if operation is None:
            # The tie points at an operation that is no longer on this WO (or another
            # tenant's). Never consume against it.
            logger.warning(
                "Skipping allocation %s: operation %s not on WO %s (company %s)",
                allocation.id,
                allocation.work_order_operation_id,
                work_order.id,
                company_id,
            )
            continue
        _consume_allocation_under_savepoint(
            db,
            work_order=work_order,
            allocation=allocation,
            operation=operation,
            company_id=company_id,
            user_id=user_id,
            audit=audit,
            result=result,
            # This reconcile also runs from a GET (reconcile-on-read), where
            # there is no 409 to return -- degrade and record instead.
            propagate_lock_conflict=False,
        )


def _consume_allocation_under_savepoint(
    db: Session,
    *,
    work_order: WorkOrder,
    allocation: WorkOrderMaterialAllocation,
    operation: WorkOrderOperation,
    company_id: int,
    user_id: int,
    audit: AuditService,
    result: MaterialConsumptionResult,
    propagate_lock_conflict: bool,
) -> None:
    """Reconcile ONE allocation inside its OWN savepoint; degrade all but a lock conflict.

    The per-allocation damage-control boundary shared by BOTH entry points -- the
    whole-work-order reconcile (``_consume_tied_materials``) and the per-operation
    post (``consume_tied_materials_for_operation``). It is a shared helper rather
    than a second copy precisely because the savepoint/rollback/failure-record
    sequence is the compliance-visible part: a copy that drifts would mean one entry
    point silently swallowing a failed depletion the other records on the hash chain.

    A failure rolls back only this allocation, leaves the outer transaction usable
    (a reconcile-on-read GET is about to commit on it), and writes the
    ``ALLOCATION_CONSUMPTION_FAILED`` chain row.
    """
    # Captured BEFORE the savepoint: a rollback expires the instance, and the
    # failure record must not depend on re-loading a row the rollback disturbed.
    allocation_id = allocation.id
    allocation_part_id = allocation.part_id
    operation_id = operation.id
    nested = db.begin_nested()
    try:
        _consume_one_allocation(
            db,
            work_order=work_order,
            allocation=allocation,
            operation=operation,
            company_id=company_id,
            user_id=user_id,
            audit=audit,
            result=result,
        )
        nested.commit()
    except StaleDataError:
        # INVARIANT 4, and the ONE failure the caller gets to decide about. Every
        # other failure here is a per-allocation problem worth recording and
        # stepping over; an optimistic-lock conflict is a statement about the
        # whole request -- another transaction moved this work order underneath
        # us, so the quantities this consumption was computed from are stale.
        #
        # Whether that should propagate depends on WHO is calling, which is why
        # it is a parameter rather than a fixed policy:
        #   * ``True`` (the per-operation post, called only from write handlers):
        #     degrading it would turn the handler's documented 409 into a 200
        #     that silently skipped a material deduction -- operator sees
        #     success, stock never moves, and the only trace is an audit row
        #     nobody is watching. Re-raise so the app-wide StaleDataError -> 409
        #     handler (``app/main.py``) does its job.
        #   * ``False`` (the whole-work-order reconcile): that path also runs
        #     from reconcile-on-read GETs, where there is no 409 to return and no
        #     actor to attribute one to. Propagating would abort the remaining
        #     allocations AND lose the ``ALLOCATION_CONSUMPTION_FAILED`` row --
        #     and material that should have depleted and did not is a worse
        #     control gap than the shortage case, which at least writes a chain
        #     row. So it degrades exactly as every other failure does.
        nested.rollback()
        if propagate_lock_conflict:
            raise
        result.failed_allocation_ids.append(allocation_id)
        logger.exception(
            "Material consumption hit a lock conflict for allocation %s on WO %s (company %s)",
            allocation_id,
            work_order.id,
            company_id,
        )
        _record_consumption_failed(
            db,
            work_order=work_order,
            allocation_id=allocation_id,
            part_id=allocation_part_id,
            operation_id=operation_id,
            error=StaleDataError("optimistic lock conflict during consumption"),
            company_id=company_id,
            user_id=user_id,
            audit=audit,
        )
    except Exception as exc:  # degrade per-allocation, never break a GET
        nested.rollback()
        result.failed_allocation_ids.append(allocation_id)
        logger.exception(
            "Material consumption failed for allocation %s on WO %s (company %s)",
            allocation_id,
            work_order.id,
            company_id,
        )
        _record_consumption_failed(
            db,
            work_order=work_order,
            allocation_id=allocation_id,
            part_id=allocation_part_id,
            operation_id=operation_id,
            error=exc,
            company_id=company_id,
            user_id=user_id,
            audit=audit,
        )


def consume_tied_materials_for_operation(
    db: Session,
    work_order: WorkOrder,
    operation: WorkOrderOperation,
    *,
    user_id: int,
    company_id: int,
    audit: AuditService,
) -> MaterialConsumptionResult:
    """Reconcile the OPEN ties on ONE just-completed operation to their target.

    The incremental entry point: a laser child work order carries one operation per
    nest, so finishing nest 1 of 3 has to deplete nest 1's sheet NOW rather than at the
    end of the job. Wired into ``apply_operation_completion_inventory_effects``, which
    the four operation-completion handlers call right after
    ``finalize_operation_completion``.

    **The scope is deliberately THIS OPERATION'S ties only -- never the whole work
    order -- and that is a safety property, not a performance one.** A whole-work-order
    reconcile from an operation completion would also post against operations that are
    still ``IN_PROGRESS`` with partial production, and an in-progress operation is still
    REDUCIBLE: ``production_reduction_service`` refuses a walk-back only once the
    operation is ``COMPLETE`` (or the work order is terminal). So a supervisor correcting
    an over-count on a *different*, still-open operation would strand material this call
    had already consumed against it -- and consumption NEVER auto-reverses (a negative
    delta is a no-op), with no RETURN verb yet. Scoping to the one operation makes that
    unreachable: the operation being consumed for is ``COMPLETE`` at the instant the
    ISSUE posts, hence reduce-immune, so its target can only ever move UP.

    The whole-work-order reconcile in ``apply_completion_inventory_effects`` is unchanged
    and remains the SELF-HEAL: anything an operation-level post missed (a failed
    savepoint, a tie created after the operation completed, an operation completed before
    this call site existed) still flushes when the work order finishes. Sum-delta makes
    the two converge -- the later call recomputes ``target`` from live operation state
    and sees ``delta == 0`` for whatever already posted.

    UNTIED OPERATIONS ARE UNTOUCHED: with no tie rows this returns immediately -- no
    inventory row, no ledger row, no audit row, no event -- so an untied work order stays
    byte-identical to its pre-feature behavior (invariant 6(d)).

    Does NOT commit (joins the caller's unit of work). It raises exactly ONE exception --
    ``StaleDataError`` -- and swallows every other: each allocation runs in its own
    SAVEPOINT via the shared ``_consume_allocation_under_savepoint``, and the whole body,
    the tie read included, is under the wrapper below.

    **The one thing that propagates is invariant 4.** An optimistic-lock conflict means
    another transaction moved this work order underneath us, so the quantities this
    consumption was computed from are stale. This entry point is called ONLY from write
    handlers, every one of which documents a 409 on a concurrent write, so degrading it
    would turn that 409 into a 200 that silently skipped a material deduction. Callers
    must therefore be prepared for it -- ``app/main.py``'s app-wide handler translates it
    -- and must NOT wrap this in a bare ``except``. The whole-work-order twin
    (``consume_tied_materials_for_work_order``) makes the opposite choice on purpose: it
    also runs from GETs, where there is no 409 to return.

    Callers must still gate on the work order NOT being terminal; this engine consumes
    whatever ties it is handed.
    """
    result = MaterialConsumptionResult()
    try:
        allocations = _open_allocations_for_operation(
            db,
            work_order_id=work_order.id,
            operation_id=operation.id,
            company_id=company_id,
        )
        if not allocations:
            return result
        for allocation in allocations:
            _consume_allocation_under_savepoint(
                db,
                work_order=work_order,
                allocation=allocation,
                operation=operation,
                company_id=company_id,
                user_id=user_id,
                audit=audit,
                result=result,
                # Write handlers only: a lock conflict is this request's 409.
                propagate_lock_conflict=True,
            )
    except StaleDataError:
        # INVARIANT 4. This entry point is called ONLY from write handlers, all of
        # which document a 409 on a concurrent-write conflict, so the lock
        # conflict must reach them. It is re-raised rather than degraded for a
        # sharper reason than the savepoint helper's: a conflict raised by the
        # autoflush of the tie READ above never reaches a savepoint at all, so it
        # would be caught here and produce NO audit row whatsoever -- just a log
        # line. A silently skipped material deduction with no record is strictly
        # worse than the shortage case, which at least writes a chain row.
        #
        # This is also what keeps the guarantee STRUCTURAL rather than
        # conventional: the four call sites each precede this with an explicit
        # ``db.flush()`` so the conflict surfaces there instead, but a fifth call
        # site that forgets would otherwise turn a 409 into an invisible no-op.
        raise
    except Exception:  # pragma: no cover - degrade, never break a completion
        logger.exception(
            "Material consumption aborted for WO %s operation %s (company %s)",
            work_order.id,
            operation.id,
            company_id,
        )
    return result


# ---------------------------------------------------------------------------
# The reasoned RETURN verb -- the only way consumed material ever comes back
# ---------------------------------------------------------------------------

# ``InventoryTransaction.reason_code`` on every RETURN row written here. Symmetric with
# receiving's ``RECEIPT_CORRECTION`` / ``RECEIPT_VOID``: a compensating movement must be
# classifiable from the ledger row alone, without parsing its free text.
MATERIAL_RETURN_REASON_CODE = "MATERIAL_RETURN"

# The audit ``extra_data.reason`` a ``RETURN_AND_UNTIE`` stamps on the tie it cancels.
# Deliberately NOT ``WORK_ORDER_DELETED_CANCEL_REASON``:
# ``reopen_allocations_cancelled_by_delete`` resurrects exactly those ties whose most
# recent DELETE row carries the delete reason, so a mis-stamped cancel would be brought
# back to life by a work-order delete/restore round trip -- re-arming demand for material
# somebody explicitly gave back, and re-opening a tie whose whole point was to be closed.
MATERIAL_RETURNED_CANCEL_REASON = "material_returned"


class MaterialReturnRefused(Exception):
    """The engine refuses a return, carrying the HTTP status the router should use.

    Raising is correct HERE, unlike everywhere else in this module: the consume engine
    also runs from reconcile-on-read GETs and therefore degrades rather than propagates,
    but a return only ever runs from a write handler with an actor and a reason attached.
    A refusal is an answer to that actor, not a failure to swallow.

    ``status_code`` is carried on the exception rather than decided by the router so the
    distinction between the two refusal families cannot drift: **422** for "you asked for
    the wrong thing" (a bound the caller can satisfy by naming the other intent or a
    smaller quantity) and **409** for "the ledger cannot express this" (a source lot that
    is gone or is a placeholder, or a cache/ledger disagreement) -- receiving's
    "409 rather than guess" posture.
    """

    def __init__(self, detail: str, *, status_code: int = 422) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


@dataclass
class ReturnedLot:
    """One lot credited back by a return, mirroring exactly one compensated ISSUE row."""

    inventory_item_id: int
    lot_number: Optional[str]
    quantity: float
    unit_cost: float
    transaction_id: int
    compensated_transaction_id: int


@dataclass
class MaterialReturnResult:
    """What one ``return_tied_material`` call actually did."""

    allocation_id: int
    work_order_id: int
    part_id: int
    part_number: Optional[str]
    intent: MaterialReturnIntent
    unit_of_measure: str
    quantity_returned: float
    qty_consumed_before: float
    qty_consumed: float
    status: AllocationStatus
    returned_lots: list[ReturnedLot] = field(default_factory=list)
    transactions: list[InventoryTransaction] = field(default_factory=list)


@dataclass
class _ReturnStep:
    """One planned credit: how much goes back against which ISSUE row, at what cost."""

    issue_txn: InventoryTransaction
    inventory_item_id: int
    quantity: float


def _lock_return_scope(
    db: Session,
    *,
    work_order: WorkOrder,
    allocation: WorkOrderMaterialAllocation,
    company_id: int,
) -> Optional[WorkOrderOperation]:
    """Row-lock the operation then the work order, and hand back the fresh operation.

    **The lock is load-bearing, and the optimistic lock is not enough here.** Invariant 4
    serializes concurrent COMPLETIONS because each of them writes the ``version_id_col``
    -mapped ``WorkOrder`` / ``WorkOrderOperation`` rows. A return writes NEITHER, so it
    would take no version conflict at all: a completion landing between this function's
    bound check and its ledger post would raise ``target`` underneath it, and the
    ``correct_over_consumption`` bound -- whose entire purpose is to leave
    ``qty_consumed >= target`` so the engine can never re-draw -- would silently no
    longer hold. ``SELECT ... FOR UPDATE`` on both rows is what makes the check and the
    post see the same production quantities.

    **This covers ``target`` only.** The bound's OTHER operand, the tie's
    ``qty_consumed``, lives on a row this function deliberately does not lock -- the
    caller must ``db.refresh(allocation)`` immediately after this returns, or it compares
    a fresh target against a cache read before the lock. See the comment at that call
    site for the silent partial-return that follows if it is skipped.

    Order is OPERATION then WORK ORDER, matching the completion paths and
    ``production_reduction_service`` (SFI-1). Taking two locks in two different orders
    across two verbs is how deadlocks are built.

    ``populate_existing()`` is deliberate: the caller's session may already hold a stale
    copy of the operation, and ``target`` MUST be computed from the row as locked, not
    from whatever the identity map happened to be carrying.

    Returns the locked operation, or ``None`` for a work-order-scoped tie (nothing to
    lock) or an operation-scoped tie whose operation is no longer on this work order.
    (``with_for_update`` is a no-op on the SQLite test backend, as elsewhere in this
    codebase.)
    """
    operation: Optional[WorkOrderOperation] = None
    if allocation.work_order_operation_id is not None:
        operation = (
            db.query(WorkOrderOperation)
            .filter(
                WorkOrderOperation.company_id == company_id,
                WorkOrderOperation.work_order_id == work_order.id,
                WorkOrderOperation.id == allocation.work_order_operation_id,
            )
            .populate_existing()
            .with_for_update()
            .first()
        )
    db.query(WorkOrder).filter(
        WorkOrder.company_id == company_id,
        WorkOrder.id == work_order.id,
    ).with_for_update().first()
    return operation


def _live_consumption_target(
    allocation: WorkOrderMaterialAllocation,
    operation: Optional[WorkOrderOperation],
) -> float:
    """The quantity the consume engine would drive this tie's ``qty_consumed`` to RIGHT NOW.

    Recomputed from live state, NEVER from ``qty_planned`` on an operation-scoped tie --
    that is the plan, and the engine has never consumed against the plan.

    Three cases, each the exact basis its own consumption leg uses, so "the engine can no
    longer draw" means the same thing in this function as it does there:

    * **Operation-scoped, operation present** -- ``qty_per_run x (complete + scrapped)``,
      byte-identical to ``_consume_one_allocation``'s ``target``.
    * **Work-order-scoped** -- ``qty_planned``, which is EXACTLY the target the backflush
      leg reconciles such a tie to (``backflush_components_for_work_order``'s leg 2:
      ``delta = qty_planned - net_consumed_quantity_for_allocation(...)``). So leaving
      ``qty_consumed >= qty_planned`` is what makes that delta non-positive and the leg
      skip it. (Consequence worth knowing: a work-order-scoped tie drained by the
      backflush sits at exactly ``qty_consumed == qty_planned``, so its correction
      allowance is ZERO and the only return available to it is ``return_and_untie`` --
      correct, because such a tie consumed precisely what it planned. An allowance opens
      only if the plan was later edited down.)
    * **Operation-scoped, operation GONE** (detached by a nest re-import, or never on this
      work order) -- ``0.0``. Neither leg can reach such a tie: the per-operation read is
      keyed by operation id, the whole-work-order reconcile skips a tie whose operation is
      not on the work order, and the backflush's tie leg reads only ties with
      ``work_order_operation_id IS NULL``. Nothing can re-draw it, so nothing is bounded.
    """
    if allocation.work_order_operation_id is None:
        return float(allocation.qty_planned or 0)
    if operation is None:
        return 0.0
    per_run = float(allocation.qty_per_run if allocation.qty_per_run is not None else 1.0)
    return per_run * (float(operation.quantity_complete or 0) + float(operation.quantity_scrapped or 0))


def _plan_material_return(
    db: Session,
    *,
    allocation: WorkOrderMaterialAllocation,
    quantity: float,
    company_id: int,
) -> list[_ReturnStep]:
    """Decide which ISSUE rows a return compensates, NEWEST-FIRST, without writing anything.

    **Material goes back to the lots it came off, or not at all.** A consumption can spill
    across several FIFO lots, so one logical return is N ledger rows; crediting any other
    lot would invent heat/cert linkage in an as-built record (AS9100D 8.5.2). This mirrors
    receiving's "always the ORIGINAL row, or refuse" posture rather than inventing a
    convenient sink.

    The walk is newest-first because that is the reverse of how consumption posted: the
    most recent ISSUE is the one an over-count correction is compensating.

    **The cap is the idempotency story.** There is no unique index on RETURN rows and
    nothing links a RETURN to a specific ISSUE, so double-crediting is prevented
    arithmetically: capacity per ``(allocation_id, inventory_item_id)`` is
    ``issued - already returned``, and prior returns are charged against the NEWEST issue
    rows first -- the same LIFO order this walk credits in, and this engine is the only
    writer of these rows, so the residual capacity lands on exactly the rows a prior
    return did not reach. A second return therefore cannot over-credit a lot no matter how
    many times it is replayed.

    Planning is a separate pass from posting on purpose: every refusal must fire before
    the first row is written, so a refused return leaves the ledger untouched rather than
    half-credited.
    """
    rows = (
        db.query(InventoryTransaction)
        .filter(
            InventoryTransaction.company_id == company_id,
            InventoryTransaction.allocation_id == allocation.id,
        )
        .order_by(InventoryTransaction.id.desc())
        .all()
    )

    issue_rows: list[InventoryTransaction] = []
    returned_by_item: dict[int, float] = {}
    for txn in rows:
        item_id = txn.inventory_item_id
        if item_id is None:
            # A ledger row with no stock row cannot be credited back to a lot. It also
            # cannot exist on this path (both writers always resolve or mint a row), so
            # it is skipped rather than guessed at.
            continue
        if txn.transaction_type == TransactionType.ISSUE:
            issue_rows.append(txn)
        elif txn.transaction_type == TransactionType.RETURN:
            returned_by_item[item_id] = returned_by_item.get(item_id, 0.0) + abs(float(txn.quantity or 0))

    # Charge prior returns against the newest issue rows first (see the docstring), so
    # each row's remaining capacity is what a further return may still credit against it.
    capacity: dict[int, float] = {}
    for txn in issue_rows:  # already newest-first
        item_id = txn.inventory_item_id
        row_qty = abs(float(txn.quantity or 0))
        already = returned_by_item.get(item_id, 0.0)
        if already > _EPSILON:
            consumed_from_row = min(row_qty, already)
            returned_by_item[item_id] = already - consumed_from_row
            row_qty -= consumed_from_row
        capacity[txn.id] = row_qty

    steps: list[_ReturnStep] = []
    remaining = quantity
    for txn in issue_rows:
        if remaining <= _EPSILON:
            break
        available = capacity.get(txn.id, 0.0)
        if available <= _EPSILON:
            continue
        take = min(remaining, available)
        steps.append(
            _ReturnStep(
                issue_txn=txn,
                inventory_item_id=txn.inventory_item_id,
                quantity=take,
            )
        )
        remaining -= take

    if remaining > _EPSILON:
        # The LEDGER, not the cache, decides what may come back. ``qty_consumed`` is a
        # documented cache (see the model docstring), so a disagreement between the two
        # is exactly the case where trusting the cache would credit stock no ISSUE row
        # ever took. 409: the caller cannot fix this by asking differently.
        raise MaterialReturnRefused(
            f"Cannot return {quantity} {allocation.unit_of_measure}: the ledger shows only "
            f"{round(quantity - remaining, 6)} still returnable against this tie. The inventory "
            "ledger is authoritative and the tie's consumed quantity is only a cache; make a "
            "manual inventory adjustment if stock genuinely needs to move.",
            status_code=409,
        )
    return steps


def _resolve_return_source_lots(
    db: Session,
    *,
    steps: list[_ReturnStep],
    company_id: int,
) -> dict[int, InventoryItem]:
    """Load (tenant-scoped) every lot a plan credits, refusing rather than guessing.

    Two rows a return must NOT invent stock against, both refused with 409 naming the
    offending row -- receiving's "409 rather than guess" posture:

    * **A source lot that no longer exists.** Nothing in ``app/`` deletes stock rows, so
      this means the row was removed outside the application. Crediting the quantity
      somewhere else would put material on a lot that never held it.
    * **A ``_placeholder_stock_row``** -- the lot-less, finished-goods-located anchor the
      consume engine mints when a part has NO stock row at all and a shortage still has to
      be recorded. Crediting it back would turn a ledger anchor into unlabeled,
      FIFO-eligible stock with no heat and no cert. (This is rare: the shortage leg
      prefers any real lot and only mints a placeholder when the part has none.)

    Note what is deliberately NOT refused: a lot whose ``quantity_on_hand`` is NEGATIVE.
    That is the expected shape after a shortage-driven consumption, and the return
    unwinding it toward zero is the whole point.
    """
    item_ids = {step.inventory_item_id for step in steps}
    if not item_ids:
        return {}
    items = {
        item.id: item
        for item in db.query(InventoryItem)
        .filter(
            InventoryItem.company_id == company_id,
            InventoryItem.id.in_(item_ids),
        )
        .all()
    }
    for item_id in sorted(item_ids):
        item = items.get(item_id)
        if item is None:
            raise MaterialReturnRefused(
                f"Cannot return this material: the source stock row (inventory item {item_id}) the "
                "consumption came from no longer exists, and returning it to any other lot would "
                "misstate lot traceability. Make a manual inventory adjustment instead.",
                status_code=409,
            )
        if _is_placeholder_stock_row(item):
            raise MaterialReturnRefused(
                f"Cannot return this material: the consumption was recorded against placeholder stock "
                f"row {item_id}, which names no lot (the part had no stock at all when it was "
                "consumed). Crediting it would create unlabeled stock; make a manual inventory "
                "adjustment instead.",
                status_code=409,
            )
    return items


def return_tied_material(
    db: Session,
    work_order: WorkOrder,
    allocation: WorkOrderMaterialAllocation,
    *,
    quantity: float,
    intent: MaterialReturnIntent,
    reason: str,
    user_id: int,
    company_id: int,
    audit: AuditService,
) -> MaterialReturnResult:
    """Return consumed material to its source lots -- the compensating twin of the engine.

    Consumption never auto-reverses (invariant 6b): a negative delta is a no-op, because
    the consume path also runs from a GET where there is no actor, no intent and no reason
    to record. This is that same reversal performed by an ACTOR, with a REASON, on the
    hash chain -- the "compensating transaction + required reason + audit" pattern the
    receiving corrections established, and the self-service path every "reverse
    consumption first" refusal previously lacked.

    **Nothing historical is mutated.** The original ISSUE rows stand exactly as written;
    a return is an APPENDED positive ``RETURN`` row per compensated ISSUE, carrying the
    same reference shape (so job cost, analytics, genealogy and the ledger list endpoint
    pick it up with no change to any of them), the same ``allocation_id``, and the
    compensated row's ``unit_cost``.

    Two named intents and nothing in between -- see ``MaterialReturnIntent`` for WHY the
    middle is closed rather than merely discouraged.

    Refusals (each carried on ``MaterialReturnRefused.status_code``):

    * **404** -- the tie does not belong to this work order / company (never 403, so an
      id cannot be probed).
    * **422** -- blank reason, non-positive quantity, nothing consumed to return, more
      than ``qty_consumed``, a ``correct_over_consumption`` past the live bound (the
      detail names ``return_and_untie``), or a ``return_and_untie`` that is not the full
      consumed quantity.
    * **409** -- the ledger has less returnable than asked, a source lot is gone, or a
      source lot is a placeholder.

    Does NOT commit -- joins the caller's unit of work, so the ledger rows, the tie
    update and the audit rows land atomically with the request. Unlike the consume engine
    this DOES raise: it only ever runs from a write handler, so a refusal must reach the
    caller rather than degrade into an audit row nobody is watching.
    """
    reason = (reason or "").strip()
    if not reason:
        # The Pydantic boundary already enforces this; repeated here because the service
        # is callable without it and an unreasoned compensating movement is exactly what
        # the audit chain must never contain.
        raise MaterialReturnRefused("A reason is required to return material.", status_code=422)
    if allocation.company_id != company_id or allocation.work_order_id != work_order.id:
        raise MaterialReturnRefused(
            "Material tie not found on this work order.",
            status_code=404,
        )
    if quantity is None or quantity <= _EPSILON:
        raise MaterialReturnRefused("Return quantity must be greater than zero.", status_code=422)

    operation = _lock_return_scope(db, work_order=work_order, allocation=allocation, company_id=company_id)

    # BOTH operands of the bound must be read under the lock, not just ``target``.
    # ``_lock_return_scope`` refreshes the OPERATION (that is where ``target`` comes
    # from) but never touches this row, and ``allocation`` was put in the identity map
    # by the endpoint's load BEFORE any lock was taken -- SQLAlchemy will not overwrite
    # a live instance's attributes, so without this refresh ``qty_consumed`` is stale by
    # exactly the window the lock exists to close.
    #
    # The failure it prevents is silent and user-visible: a completion committing in that
    # window advances ``qty_consumed`` 5 -> 8, we would read a FRESH target of 8 against a
    # STALE consumed of 5, and a ``return_and_untie`` for 5 would pass every check, credit
    # 5 of the 8 the ledger holds, zero the cache and CANCEL the tie -- an operator asking
    # for everything back, getting a success toast, and silently keeping 3. (The ledger
    # itself stays truthful, which is exactly why nothing downstream would flag it.)
    #
    # Safe without a new lock and with no deadlock-order question: any competing completion
    # or return must have committed to release the operation/work-order locks we now hold,
    # so its ``qty_consumed`` write is already visible.
    db.refresh(allocation)

    consumed_before = float(allocation.qty_consumed or 0)
    if consumed_before <= _EPSILON:
        raise MaterialReturnRefused(
            "Nothing has been consumed against this material tie, so there is nothing to return. "
            "Untie it instead if the material is no longer needed.",
            status_code=422,
        )
    if quantity > consumed_before + _EPSILON:
        raise MaterialReturnRefused(
            f"Cannot return {quantity} {allocation.unit_of_measure}: only {consumed_before} has been "
            "consumed against this tie.",
            status_code=422,
        )

    target = _live_consumption_target(allocation, operation)
    if intent == MaterialReturnIntent.CORRECT_OVER_CONSUMPTION:
        allowance = consumed_before - target
        if quantity > allowance + _EPSILON:
            # The bound IS the engine's own arithmetic: below it, ``target - qty_consumed``
            # turns positive again and the next completion -- or the next reconcile-on-read
            # GET, where there is no actor at all -- re-consumes what was just returned,
            # re-running FIFO and possibly crediting a different lot.
            raise MaterialReturnRefused(
                f"Cannot return {quantity} {allocation.unit_of_measure} while this tie stays open: "
                f"the work still accounts for {round(target, 6)} and only "
                f"{round(max(allowance, 0.0), 6)} is over-consumed. Returning more would be "
                "re-consumed automatically the next time this work order is completed or read. "
                "Use return_and_untie to give all the material back and close the tie.",
                status_code=422,
            )
    elif intent == MaterialReturnIntent.RETURN_AND_UNTIE:
        if abs(quantity - consumed_before) > _EPSILON:
            # return_and_untie gives EVERYTHING back; the quantity is a CONFIRMATION of
            # what the caller believes was consumed. Refusing a mismatch catches the stale
            # client (a completion landed between the page load and the submit) instead of
            # returning a different amount than the operator was looking at.
            raise MaterialReturnRefused(
                f"return_and_untie returns everything consumed against this tie, which is currently "
                f"{consumed_before} {allocation.unit_of_measure}, not {quantity}. Re-read the tie and "
                "confirm that quantity.",
                status_code=422,
            )
    else:  # pragma: no cover - unreachable while MaterialReturnIntent has two members
        # Deliberately exhaustive rather than a permissive fall-through: a third intent
        # added without a bound of its own would otherwise post an UNBOUNDED return
        # against a live tie -- exactly the middle ground the two-intent rule closes.
        raise MaterialReturnRefused(f"Unsupported material return intent: {intent}", status_code=422)

    # PLAN AND VALIDATE BEFORE POSTING: every refusal above and below fires before the
    # first ledger row is written, so a refused return leaves the ledger untouched.
    steps = _plan_material_return(db, allocation=allocation, quantity=quantity, company_id=company_id)
    items = _resolve_return_source_lots(db, steps=steps, company_id=company_id)

    part = db.query(Part).filter(Part.id == allocation.part_id, Part.company_id == company_id).first()
    part_number = part.part_number if part else None
    scope = f" operation {operation.operation_number or operation.id}" if operation is not None else ""
    # FALLBACK only -- the primary path mirrors ``step.issue_txn.reference_type``. A
    # work-order-scoped tie's ISSUE rows now post under ``work_order_backflush``, so that
    # is the shape a fallback must derive; ``work_order`` would put the compensating row
    # under the one-shot legacy shape (and inside the ``uq_wo_inventory_issue`` predicate's
    # neighbourhood) for a tie that never wrote one.
    scope_reference_type = (
        OPERATION_REFERENCE_TYPE if allocation.work_order_operation_id is not None else BACKFLUSH_REFERENCE_TYPE
    )
    notes = f"Material return ({intent.value}) for work order {work_order.work_order_number}{scope}: {reason}"

    result = MaterialReturnResult(
        allocation_id=allocation.id,
        work_order_id=work_order.id,
        part_id=allocation.part_id,
        part_number=part_number,
        intent=intent,
        unit_of_measure=allocation.unit_of_measure,
        quantity_returned=0.0,
        qty_consumed_before=consumed_before,
        qty_consumed=consumed_before,
        status=allocation.status,
    )

    for step in steps:
        item = items[step.inventory_item_id]
        txn = _write_return_txn(
            db,
            work_order,
            inventory_item=item,
            part_id=allocation.part_id,
            quantity=step.quantity,
            # The COMPENSATED row's cost, never the lot's current one -- see
            # ``_write_return_txn``.
            unit_cost=float(step.issue_txn.unit_cost or 0),
            lot_number=item.lot_number,
            company_id=company_id,
            user_id=user_id,
            audit=audit,
            part_number=part_number,
            allocation_id=allocation.id,
            # MIRROR the row being compensated. ``work_order_ledger_filter`` matches on
            # reference shape only, so this is what keeps the return inside job cost,
            # analytics and lot genealogy without touching any of those readers. (Both
            # writers always stamp a reference type; the fallback derives it from the
            # tie's own scope rather than assuming one, so a hand-written legacy row
            # cannot produce an operation reference pointing at a work-order id.)
            reference_type=step.issue_txn.reference_type or scope_reference_type,
            reference_id=step.issue_txn.reference_id,
            reason_code=MATERIAL_RETURN_REASON_CODE,
            notes=notes,
            movement_suffix=scope,
            extra_data={
                "allocation_id": allocation.id,
                "work_order_id": work_order.id,
                "work_order_operation_id": operation.id if operation is not None else None,
                "intent": intent.value,
                # On the row AND in extra_data AND in the description below: receiving put
                # the reason in only one of the three, and a reason an auditor cannot find
                # from the record they pulled is a reason nobody reads.
                "reason": reason,
                "compensated_transaction_id": step.issue_txn.id,
            },
        )
        result.transactions.append(txn)
        result.returned_lots.append(
            ReturnedLot(
                inventory_item_id=item.id,
                lot_number=item.lot_number,
                quantity=step.quantity,
                unit_cost=float(step.issue_txn.unit_cost or 0),
                transaction_id=txn.id,
                compensated_transaction_id=step.issue_txn.id,
            )
        )
        result.quantity_returned += step.quantity

    # Clamp float dust to exactly zero (the receiving-correction precedent): a FULL return
    # must leave the cache reading 0, not 4e-16, or the untie / hard-delete guards read as
    # "material was consumed" against a tie that gave everything back.
    new_consumed = max(consumed_before - result.quantity_returned, 0.0)
    allocation.qty_consumed = 0.0 if new_consumed <= _EPSILON else new_consumed
    db.flush()
    audit.log_update(
        "work_order_material_allocation",
        allocation.id,
        f"WO {work_order.work_order_number} / part {part_number or allocation.part_id}",
        old_values={"qty_consumed": consumed_before},
        new_values={"qty_consumed": allocation.qty_consumed},
        description=(
            f"Returned {result.quantity_returned} {allocation.unit_of_measure} of part "
            f"{part_number or allocation.part_id} to stock from work order "
            f"{work_order.work_order_number}{scope} ({intent.value}): {reason}"
        ),
        extra_data={
            "work_order_id": work_order.id,
            "work_order_operation_id": operation.id if operation is not None else None,
            "part_id": allocation.part_id,
            "intent": intent.value,
            "reason": reason,
            "quantity_returned": result.quantity_returned,
            "live_target": target,
            "returned_lots": [
                {
                    "inventory_item_id": lot.inventory_item_id,
                    "lot_number": lot.lot_number,
                    "quantity": lot.quantity,
                    "transaction_id": lot.transaction_id,
                    "compensated_transaction_id": lot.compensated_transaction_id,
                }
                for lot in result.returned_lots
            ],
        },
    )

    if intent == MaterialReturnIntent.RETURN_AND_UNTIE and allocation.status != AllocationStatus.CANCELLED:
        # CANCELLED in the SAME transaction as the credit, so there is never a window in
        # which an open tie sits at qty_consumed 0 with its target still positive -- the
        # engine would re-consume the whole thing. Deliberately NOT ``CLOSED``: nothing in
        # ``app/`` writes that status, and a CLOSED tie would vanish from
        # ``_drop_allocation_covered_parts`` and let the BOM backflush double-issue the
        # same part once ``backflush_components`` is exposed (PR 4).
        old_status = allocation.status
        allocation.status = AllocationStatus.CANCELLED
        db.flush()
        audit.log_delete(
            "work_order_material_allocation",
            allocation.id,
            f"WO {work_order.work_order_number} / part {part_number or allocation.part_id}",
            old_values={"status": old_status.value, "qty_consumed": consumed_before},
            description=(
                f"Cancelled material allocation on WO {work_order.work_order_number}: "
                f"{result.quantity_returned} {allocation.unit_of_measure} of material was returned "
                f"to stock ({reason})"
            ),
            soft_delete=True,
            extra_data={
                "reason": MATERIAL_RETURNED_CANCEL_REASON,
                "return_reason": reason,
                "work_order_id": work_order.id,
                "work_order_operation_id": allocation.work_order_operation_id,
                "part_id": allocation.part_id,
                "quantity_returned": result.quantity_returned,
                "new_status": AllocationStatus.CANCELLED.value,
            },
        )
    result.qty_consumed = float(allocation.qty_consumed or 0)
    result.status = allocation.status
    logger.info(
        "Material returned: WO %s allocation %s intent %s quantity %s across %s lot(s) (company %s)",
        work_order.id,
        allocation.id,
        intent.value,
        result.quantity_returned,
        len(result.returned_lots),
        company_id,
    )
    return result


# ---------------------------------------------------------------------------
# Lifecycle helpers (nest re-import, work-order delete)
# ---------------------------------------------------------------------------


class MaterialAllocationConsumedError(Exception):
    """A lifecycle action would orphan CONSUMED material. Routers map this to HTTP 409.

    Raised when an operation (nest re-import) or a work order (hard delete) that
    already has posted consumption is about to be destroyed. Deleting the operation
    out from under its ISSUE rows would orphan lot genealogy -- the ledger row's
    ``reference_id`` would point at nothing -- so the caller must reverse the
    consumption explicitly first (an explicit, reasoned verb; a later PR).
    """


def cancel_allocations_for_operations(
    db: Session,
    *,
    work_order: WorkOrder,
    operation_ids: list[int],
    company_id: int,
    audit: AuditService,
) -> list[int]:
    """Guard + DETACH every allocation scoped to operations that are about to be WIPED.

    ``build_laser_nest_child_work_order`` rebuilds a laser WO's operations wholesale
    (import replaces everything). Any allocation on a wiped operation that the LEDGER
    references raises ``MaterialAllocationConsumedError`` -> HTTP 409; the OPEN ones are
    CANCELLED (status is the tombstone -- these rows are never deleted) with an audit row
    so the untie is traceable. Returns the ids of the ties this call actually CANCELLED
    (already-cancelled rows are detached, not re-cancelled, so they are not in the list).
    Tenant-scoped; does not commit.

    **The guard reads the LEDGER, not the ``qty_consumed`` cache** -- the same basis the
    hard-delete guard uses (``ledger_backed_allocation_ids``), and the difference is not
    cosmetic now that PR 3's RETURN verb exists. A full ``return_and_untie`` drives
    ``qty_consumed`` back to 0, so a cache-keyed guard would wave the re-import through
    on a tie whose ISSUE **and** RETURN rows both still carry
    ``reference_type='work_order_operation'`` with ``reference_id`` = an operation the
    caller's very next ``db.delete(operation)`` destroys. Two things follow, and both are
    worse than a refusal: (1) ``work_order_ledger_filter`` resolves operation ids through
    a LIVE SUBQUERY over ``work_order_operations``, so those rows do not merely lose a
    label -- they silently drop out of job cost, analytics and lot genealogy while
    remaining in the ledger, and an as-built record would then disagree with the ledger
    it is supposed to summarize (AS9100D 8.5.2); (2) the ``allocation_id`` FK and the
    operation FK carry no ``ON DELETE``, so on Postgres the delete raises
    ``IntegrityError`` and the import endpoint turns it into a misleading 400 -- the
    exact bug class that already shipped through one review because SQLite does not
    enforce foreign keys.

    So the answer after a return is still "raise a new work order for the corrected
    package". Returning the material makes the tie's forward-looking demand go away; it
    does not, and cannot, un-write the movement history that names these operations.

    **The tie's ``work_order_operation_id`` is CLEARED here, and that is load-bearing.**
    The caller's very next act is ``db.delete(operation)`` on exactly these operations,
    and ``work_order_material_allocations.work_order_operation_id`` is a plain FK with
    NO ``ON DELETE`` (migration 074) declared many-to-one with no parent backref -- so
    SQLAlchemy does not null it and Postgres raises ``IntegrityError`` on the DELETE,
    which the import endpoint turns into a misleading 400 ("a nest conflicts with an
    existing record"). The documented cancel-and-rebuild path was therefore unreachable
    in production on any laser WO that had a material tie -- the feature's headline flow.
    The suite could not see it: SQLite does not enforce foreign keys unless
    ``PRAGMA foreign_keys=ON``, which this project never sets.

    **The selection is deliberately status-BLIND, and that is the round-3 fix.** This
    used to filter ``status != CANCELLED``, which made the fix above incomplete in two
    ways, because an ALREADY-cancelled tie keeps pointing at its operation:

    * ``work_order_materials.delete_material_allocation`` (a manual untie) and
      ``cancel_open_allocations_for_work_order`` (the work-order soft delete) both set
      ``CANCELLED`` and deliberately RETAIN ``work_order_operation_id``. Such a row was
      neither guarded nor detached, so the FK violation -- and the misleading 400 --
      survived on any WO where a tie had been untied before the re-import. That made the
      rebuild permanently impossible for that work order through supported verbs alone.
    * Worse, the consumed-material guard was BYPASSED: the soft delete cancels every
      OPEN tie regardless of ``qty_consumed``, so a tie carrying real consumption could
      reach the wipe as a ``CANCELLED`` row the ``qty_consumed`` check never saw. The
      409 that exists precisely to stop ``work_order_operation``-referenced ISSUE rows
      being orphaned did not fire. Reading EVERY status widens the guard correctly.

    Nulling is safe and correct for these rows specifically: after this call every one
    of them is ``CANCELLED``, so NEITHER partial unique index applies
    (``uq_wo_material_alloc_open_op`` requires ``work_order_operation_id IS NOT NULL AND
    status='OPEN'``; ``uq_wo_material_alloc_open_wo`` requires ``IS NULL AND
    status='OPEN'``), and no ledger row references any of them at all (guarded above,
    against the ledger itself), so nothing points at the operation id being dropped. The ORIGINAL scope is preserved on the
    hash chain -- in the audit row's ``old_values`` *and*
    ``extra_data.work_order_operation_id`` -- so the tie's history still says which
    operation it was tied to.

    A detach of an already-cancelled tie is itself a state change on a tenant row, so it
    gets its OWN chain row (invariant #2): a ``log_update`` recording
    ``work_order_operation_id: <old> -> None`` with ``reason=superseded_by_reimport``.
    It is deliberately an UPDATE, not a second DELETE -- the row was already untied, and
    ``reopen_allocations_cancelled_by_delete`` reads the tie's most recent DELETE row to
    decide what a restore may resurrect, so a second DELETE here would rewrite that
    history. That reader has its own detach check; keep the two in lock-step.

    ``audit`` is REQUIRED, not optional. Cancelling a tie is a state change on a tenant
    table (invariant #2), and an optional-with-``None``-default audit made an unaudited
    CANCEL a one-line mistake for the next caller.
    """
    if not operation_ids:
        return []
    allocations = (
        db.query(WorkOrderMaterialAllocation)
        .filter(
            WorkOrderMaterialAllocation.company_id == company_id,
            WorkOrderMaterialAllocation.work_order_operation_id.in_(operation_ids),
        )
        .all()
    )
    if not allocations:
        return []

    # LEDGER-keyed, deliberately not cache-keyed -- see the docstring. A returned tie
    # reads ``qty_consumed == 0`` and is still ledger-backed, and its rows still name the
    # operations this rebuild is about to delete.
    ledger_backed = ledger_backed_allocation_ids(db, allocation_ids=[a.id for a in allocations], company_id=company_id)
    if ledger_backed:
        # Name a remedy that EXISTS. This message used to end "Reverse consumption
        # first", pointing at a RETURN verb that did not exist yet -- so the one
        # person most likely to read it, a planner re-importing a corrected nest
        # package, was told to do something impossible and would reasonably read
        # it as a system fault. The RETURN verb exists now and is STILL not the
        # remedy here, so the message must not imply that it is: material coming
        # back does not un-write the movement history, and it is the history --
        # ISSUE and RETURN rows alike, keyed to these operations -- that the wipe
        # would orphan.
        raise MaterialAllocationConsumedError(
            "Cannot rebuild this work order's operations: this work order's material movement is "
            f"already on the inventory ledger for {len(ledger_backed)} tied allocation(s), and the "
            "rebuild would delete the operations those ledger rows are recorded against — dropping "
            "them out of job cost, analytics and lot traceability. Returning the material does not "
            "change that. Raise a new work order for the corrected nest package; this one keeps its "
            "material history intact."
        )

    cancelled: list[int] = []
    for allocation in allocations:
        old_operation_id = allocation.work_order_operation_id
        old_status = allocation.status
        # Detach from the operation that is about to be physically deleted (see above).
        # Done for EVERY status: a row that is already cancelled still holds the FK.
        allocation.work_order_operation_id = None
        if old_status == AllocationStatus.OPEN:
            allocation.status = AllocationStatus.CANCELLED
            cancelled.append(allocation.id)
            audit.log_delete(
                "work_order_material_allocation",
                allocation.id,
                f"WO {work_order.work_order_number} / part {allocation.part_id}",
                old_values={
                    "status": AllocationStatus.OPEN.value,
                    "qty_consumed": allocation.qty_consumed,
                    "work_order_operation_id": old_operation_id,
                },
                description=(
                    f"Cancelled material allocation on WO {work_order.work_order_number}: "
                    f"the tied operation ({old_operation_id}) was superseded by a nest re-import"
                ),
                soft_delete=True,
                extra_data={
                    "reason": REIMPORT_CANCEL_REASON,
                    "work_order_id": work_order.id,
                    # The scope the tie HAD. The column is cleared so the operation delete
                    # below cannot FK-violate; this is where that fact survives.
                    "work_order_operation_id": old_operation_id,
                    "work_order_operation_id_cleared": True,
                    "part_id": allocation.part_id,
                    "new_status": AllocationStatus.CANCELLED.value,
                },
            )
        else:
            # Already CANCELLED (a manual untie, or a work-order soft delete) -- there is
            # no status change to record, but the DETACH is one, and without this row the
            # tie's original scope would exist nowhere: the column is about to read NULL,
            # which is indistinguishable from a tie that was always work-order-scoped.
            audit.log_update(
                "work_order_material_allocation",
                allocation.id,
                f"WO {work_order.work_order_number} / part {allocation.part_id}",
                old_values={"work_order_operation_id": old_operation_id},
                new_values={"work_order_operation_id": None},
                description=(
                    f"Detached {old_status.value} material allocation from operation "
                    f"({old_operation_id}) on WO {work_order.work_order_number}: the operation was "
                    "superseded by a nest re-import"
                ),
                extra_data={
                    "reason": REIMPORT_CANCEL_REASON,
                    "work_order_id": work_order.id,
                    "work_order_operation_id": old_operation_id,
                    "work_order_operation_id_cleared": True,
                    "part_id": allocation.part_id,
                    "status": old_status.value,
                },
            )
    db.flush()
    return cancelled


def cancel_open_allocations_for_work_order(
    db: Session,
    *,
    work_order: WorkOrder,
    company_id: int,
    audit: AuditService,
    reason: str = WORK_ORDER_DELETED_CANCEL_REASON,
) -> list[int]:
    """Auto-CANCEL every OPEN allocation when a work order is soft-deleted.

    Consumption already posted STANDS -- the material was physically used, and the
    ledger is the compliance record. The delete is never refused for a tie; only the
    forward-looking demand is closed out. Audited, tenant-scoped, does not commit.

    ``reason`` lands in the audit row's ``extra_data`` and is the ONLY thing that later
    tells ``reopen_allocations_cancelled_by_delete`` which ties this delete closed --
    keep the two in lock-step. Unlike the nest-re-import cancel, ``work_order_operation_id``
    is deliberately LEFT INTACT: a soft delete removes no operation rows, so there is no
    FK to detach from, and the tie must come back pointing at the same operation if the
    work order is restored.

    ``audit`` is REQUIRED (invariant #2): every CANCEL written here is a state change on
    a tenant table, so there is no caller for whom skipping the chain row is correct.
    """
    allocations = open_allocations_for_work_order(db, work_order.id, company_id)
    cancelled: list[int] = []
    for allocation in allocations:
        allocation.status = AllocationStatus.CANCELLED
        cancelled.append(allocation.id)
        audit.log_delete(
            "work_order_material_allocation",
            allocation.id,
            f"WO {work_order.work_order_number} / part {allocation.part_id}",
            old_values={"status": AllocationStatus.OPEN.value, "qty_consumed": allocation.qty_consumed},
            description=(
                f"Cancelled material allocation on WO {work_order.work_order_number}: "
                "the work order was deleted (consumption already posted stands)"
            ),
            soft_delete=True,
            extra_data={
                "reason": reason,
                "work_order_id": work_order.id,
                "work_order_operation_id": allocation.work_order_operation_id,
                "part_id": allocation.part_id,
                "new_status": AllocationStatus.CANCELLED.value,
            },
        )
    if cancelled:
        db.flush()
    return cancelled


def reopen_allocations_cancelled_by_delete(
    db: Session,
    *,
    work_order: WorkOrder,
    company_id: int,
    audit: AuditService,
) -> list[int]:
    """Re-OPEN the ties that this work order's own soft delete auto-cancelled.

    The delete/restore pair has to be symmetric. Soft delete cancels every OPEN tie to
    close out forward-looking demand; leaving them cancelled on restore means the work
    order runs to completion and its tied material SILENTLY never depletes -- and with no
    UI in PR 1, the only signal is a ``cancelled`` row nobody looks at. It gets worse
    once ``backflush_components`` is exposed (PR 4): an operation-scoped tie that has
    already consumed and was then cancelled by a delete/restore round trip no longer
    suppresses the BOM backflush in ``_drop_allocation_covered_parts``, so the same part
    is issued TWICE.

    **Only the ties THIS delete cancelled are resurrected.** A tie the planner untied by
    hand, or one a nest re-import superseded, was cancelled for a reason that a restore
    does not undo. The discriminator is the cancel's own audit row: every cancel path
    stamps ``extra_data.reason`` (``work_order_deleted`` here, ``superseded_by_reimport``
    for the re-import, absent for a manual untie), and the tie's MOST RECENT DELETE row
    is what its current CANCELLED state means. Reading the chain is a read -- the row is
    never written or altered here.

    **A tie DETACHED after that delete is not resurrected either.** A nest re-import
    clears ``work_order_operation_id`` on every tie scoped to an operation it wipes,
    including ones that were already cancelled -- and it records the detach as an UPDATE
    so it does not rewrite the DELETE history this function reads. Reopening such a tie
    would bring it back as a WORK-ORDER-scoped tie (its operation is NULL and the
    operation itself no longer exists), re-arming one-shot demand the planner never
    asked for and risking a collision with ``uq_wo_material_alloc_open_wo``. So a
    candidate whose cancel row named an operation while the row now holds NULL is left
    alone; the cancel's ``extra_data.work_order_operation_id`` is what makes that
    detectable without a second query.

    Degrades safely: if a cancel's audit row is missing (``AuditService.log`` swallows its
    own failures), that tie simply is not reopened -- the conservative direction, since
    the alternative is resurrecting a tie whose provenance we cannot establish.

    Audited (``action="restore"``), tenant-scoped, does not commit.
    """
    candidates = (
        db.query(WorkOrderMaterialAllocation)
        .filter(
            WorkOrderMaterialAllocation.company_id == company_id,
            WorkOrderMaterialAllocation.work_order_id == work_order.id,
            WorkOrderMaterialAllocation.status == AllocationStatus.CANCELLED,
        )
        .order_by(WorkOrderMaterialAllocation.id)
        .all()
    )
    if not candidates:
        return []

    by_id = {allocation.id: allocation for allocation in candidates}
    # Ascending id => the LAST row seen per allocation is its most recent cancel.
    latest_cancel: dict[int, dict] = {}
    for resource_id, extra_data in (
        db.query(AuditLog.resource_id, AuditLog.extra_data)
        .filter(
            AuditLog.company_id == company_id,
            AuditLog.resource_type == "work_order_material_allocation",
            AuditLog.action == "DELETE",
            AuditLog.resource_id.in_(list(by_id.keys())),
        )
        .order_by(AuditLog.id)
        .all()
    ):
        latest_cancel[resource_id] = extra_data or {}

    reopened: list[int] = []
    for allocation in candidates:
        cancel_extra = latest_cancel.get(allocation.id, {})
        if cancel_extra.get("reason") != WORK_ORDER_DELETED_CANCEL_REASON:
            continue
        if cancel_extra.get("work_order_operation_id") is not None and allocation.work_order_operation_id is None:
            # DETACHED after this delete cancelled it -- a nest re-import wiped the
            # operation it named. Reopening it would re-arm it as a work-order-scoped
            # tie the planner never created. See the docstring.
            continue
        allocation.status = AllocationStatus.OPEN
        reopened.append(allocation.id)
    if not reopened:
        return []

    db.flush()
    for allocation_id in reopened:
        allocation = by_id[allocation_id]
        audit.log_update(
            "work_order_material_allocation",
            allocation.id,
            f"WO {work_order.work_order_number} / part {allocation.part_id}",
            old_values={"status": AllocationStatus.CANCELLED.value},
            new_values={"status": AllocationStatus.OPEN.value},
            description=(
                f"Re-opened material allocation on WO {work_order.work_order_number}: " "the work order was restored"
            ),
            action="restore",
            extra_data={
                "reason": "work_order_restored",
                "work_order_id": work_order.id,
                "work_order_operation_id": allocation.work_order_operation_id,
                "part_id": allocation.part_id,
            },
        )
    return reopened


def allocations_on_work_order(
    db: Session,
    *,
    work_order_id: int,
    company_id: int,
) -> list[WorkOrderMaterialAllocation]:
    """EVERY allocation row on a work order, whatever its status. Tenant-scoped.

    Used by the HARD-delete path, which physically removes the work order and its
    operations: every allocation row pointing at them must go too, and one carrying
    consumption cannot, because the ledger's ``allocation_id`` back-reference must always
    resolve. The caller pairs this with ``ledger_backed_allocation_ids`` -- 409 when the
    LEDGER references any of them, physically remove the rest alongside the operations.

    (Named for what it returns. It was previously ``allocations_blocking_hard_delete``,
    which read as "the ones that block" -- it is every tie on the work order, and the
    blocking subset is whatever the ledger check hands back.)
    """
    return (
        db.query(WorkOrderMaterialAllocation)
        .filter(
            WorkOrderMaterialAllocation.company_id == company_id,
            WorkOrderMaterialAllocation.work_order_id == work_order_id,
        )
        .all()
    )


def ledger_backed_allocation_ids(
    db: Session,
    *,
    allocation_ids: list[int],
    company_id: int,
) -> set[int]:
    """Which of these allocations actually have ledger rows pointing at them.

    The LOAD-BEARING check for the hard-delete guard. ``qty_consumed`` is a documented
    CACHE (see the model docstring) and the ``inventory_transactions.allocation_id`` FK
    carries no ``ON DELETE``, so keying the guard on the cache means any drift between
    cache and ledger surfaces as an FK ``IntegrityError`` -- a 500 -- instead of the
    intended 409. Ask the ledger directly. Tenant-scoped; empty input short-circuits.
    """
    if not allocation_ids:
        return set()
    rows = (
        db.query(InventoryTransaction.allocation_id)
        .filter(
            InventoryTransaction.company_id == company_id,
            InventoryTransaction.allocation_id.in_(allocation_ids),
        )
        .distinct()
        .all()
    )
    return {row[0] for row in rows if row[0] is not None}


def work_order_tie_is_already_issued(
    db: Session,
    *,
    work_order_id: int,
    part_id: int,
    company_id: int,
) -> bool:
    """True when a LEGACY (pre-PR-4.4) one-shot ISSUE row exists for this (WO, part).

    ``_component_already_issued`` is kept keyed VERBATIM on
    ``reference_type='work_order' AND transaction_type='ISSUE'``, and since PR 4.4 that
    predicate matches ONLY rows written before this change: the backflush leg now posts
    ``work_order_backflush``. So this is the LEGACY FENCE -- a work order carrying one of
    those summed one-shot rows is fenced out of the new reconciling engine entirely,
    which is what makes PR 4.4 correct-forward with no backfill and no re-interpretation
    of a single historical ledger row.

    A work-order-scoped tie on such a part still could never consume: the fence drops it,
    and ``uq_wo_inventory_issue`` forbids a second ISSUE under that shape at any price.
    It would stay OPEN at ``qty_consumed`` 0 forever, indistinguishable in the API from a
    tie that simply has not consumed yet -- so the tie endpoint 409s instead of creating
    it.

    **That 409 is UNREACHABLE today, and it is kept anyway.** Creating a tie requires a
    NON-terminal work order; a ``work_order``-shaped component ISSUE requires the
    backflush, which only runs at COMPLETE; and COMPLETE -> non-terminal is blocked. No
    operator can produce the state. The guard costs one existence query, fails in the
    safe direction, and is the correct legacy fence if that reachability argument ever
    stops holding.

    Operation-scoped ties are unaffected -- they post under ``work_order_operation``,
    outside that unique index -- which is exactly the remedy the 409 names.
    """
    return _component_already_issued(db, work_order_id, part_id, company_id)
