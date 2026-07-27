"""Material ties as the SHOP FLOOR sees them -- one batched, read-only view builder.

Two surfaces ask about a page full of operations: *what material is tied to this work,
and how much of it is actually on the shelf?* The manager dispatch board asks for every
card on the board; the kiosk work-center queue asks every 10-15 seconds, per station, all
day. Both are answered here so the two can never disagree about the TIE FACTS -- which
part, which lot, how much is planned, how much has been drawn, what is on the shelf --
the same reason ``dispatch_service`` owns the one queue query both build their rows from.

**They do NOT share a shortage number, and that is deliberate.** ``qty_remaining`` and
``short_by`` here are PLAN-based (``qty_planned - qty_consumed``): the forward-looking
question a planner asks of a queue -- *will this job have material when it runs?* The
kiosk asks a different question at a different moment -- *what does THIS completion
draw?* -- and computes it from live operation state, ``per_run x (quantity_complete +
quantity_scrapped) - qty_consumed``, the engine's actual target
(``_consume_one_allocation``). The two legitimately differ:

* On an un-started operation the engine's target is ``per_run x 0 = 0``. Scoring a board
  chip that way would render "no material needed" on a nest about to eat five sheets,
  which is worse than useless -- so the board must NOT use the operation-state formula.
* Scrap raises the engine's real draw above ``qty_planned`` (a scrapped run consumed its
  sheet), so a board chip can read "covered" while the operator's screen flags short.

Both are advisory -- shortage never gates production, it drives the lot negative and
writes ``ALLOCATION_SHORTAGE``. Present either as an ESTIMATE, never a guarantee, and do
not "reconcile" them into one number: that would break whichever surface lost.

**This module is a PURE READ. It has no write path and must never grow one.**
``consume_tied_materials_for_work_order`` posts ISSUE rows, audit rows and shortage
events; it is deliberately NOT imported here. Reads must not write (a queue poll is not
an actor, has no intent and gives no reason), and the two constants this module *does*
take from the engine -- ``AVAILABLE_ITEM_STATUS`` and ``CONSUMPTION_EPSILON`` -- are
imported precisely so the numbers shown here cannot drift from the numbers the engine
acts on. Sharing the literal is the point; re-declaring it is the bug.

Scope: OPEN and OPERATION-scoped only
-------------------------------------
* **OPEN only, filtered explicitly.** ``AllocationStatus.CLOSED`` is never written by any
  code in ``app/`` (see the model docstring), so a fully consumed tie stays ``OPEN``
  forever. "Fully consumed" is therefore derived from the quantities
  (``qty_consumed >= qty_planned``, exposed here as ``qty_remaining == 0``) and NEVER
  from status -- while "live" is the opposite: a ``CANCELLED`` tie must not paint a chip
  on an operator's screen, so the status filter is spelled out rather than assumed.
* **Operation-scoped only.** A WORK-ORDER-scoped tie belongs to the whole job, so
  attaching it to operations would fan the same one tie across every card of that work
  order and read as N separate ties. Those ties also drain through a different mechanism
  (the one-shot backflush), so per-operation numbers would be meaningless for them.
* **An untied operation gets NO KEY in the returned dict** -- not an empty list. Callers
  render nothing at all for it. That is the surface-level half of the feature's central
  invariant: a work order with no ties behaves exactly as it did before this feature
  existed, down to the pixel.

``on_hand`` -- two different questions, and the difference matters
-----------------------------------------------------------------
* **A PINNED tie is scored against THAT LOT ALONE.** Pinning is a lot-directed
  instruction and ``_consume_one_allocation`` honors it absolutely: an insufficient
  pinned lot is driven NEGATIVE rather than spilled onto a different (uncertified,
  wrong-heat) lot. Scoring a pinned tie against the part's total on-hand would therefore
  paint it green off stock consumption will never touch, and the operator would find out
  when the lot went negative. The pinned lot's own ``quantity_on_hand`` is read even when
  the lot is held or inactive: consumption proceeds regardless (writing a
  ``HELD_MATERIAL_CONSUMED`` row), so this view reports what is physically there rather
  than zeroing it and inventing a shortage that will not happen.
* **An UNPINNED tie is scored against the SAME predicate FIFO uses** --
  ``is_active AND status = 'available' AND quantity_on_hand > 0``, mirroring
  ``_fifo_source_items``. Anything looser promises stock FIFO will refuse to touch.
  In particular a **NULL ``status`` deliberately does NOT match** ``= 'available'`` in
  SQL, and that asymmetry is intentional, not a bug to fix here: ``is_consumable_item``
  reads a NULL as available (the column default) while the FIFO SQL skips it, which is
  the safe direction -- such a lot is passed over rather than silently consumed. This
  view must show what the engine will actually draw from, so it follows the SQL.
* A pinned lot that is missing, or that belongs to ANOTHER TENANT, yields ``0.0``. The
  aggregate is company-scoped (invariant #1): an on-hand rollup that sums another
  company's lots is a security defect wearing a display bug's clothes, and the failure
  direction here -- reading as fully short -- is the conservative one.

Query budget
------------
At most **three** SELECTs for the whole page, regardless of how many operations or ties
come back: the allocations, the part labels, and ONE inventory read. Nothing here may
touch ``allocation.part`` or ``allocation.pinned_inventory_item``; those relationships
lazy-load, so a single attribute access inside the loop turns a poll into an N+1 against
the queue's row count. The inventory read is a UNION ALL of two aggregate legs because
the pinned and unpinned questions genuinely differ (per-lot vs per-part) and a union
answers both in one round trip with a bounded result -- one row per pinned lot plus one
per unpinned part, never one per lot in stock. Extend it by adding a leg, not by adding
a query.

``qty_consumed`` is a CACHE
---------------------------
It is carried through verbatim for display, and the ledger (``inventory_transactions``
rows carrying the ``allocation_id``) remains authoritative. Label it as progress in the
UI; never present it as the compliance answer to "how much of this lot was consumed?".
"""

from dataclasses import dataclass
from typing import Optional, Sequence

from sqlalchemy import func, literal, select
from sqlalchemy.orm import Session

from app.db.tenant_filter import tenant_query
from app.models.inventory import InventoryItem
from app.models.part import Part
from app.models.work_order_material import AllocationStatus, WorkOrderMaterialAllocation

# CONSTANTS ONLY -- never the engine's verbs. See the module docstring: this import is
# what keeps the FIFO predicate and the float-comparison threshold shown to an operator
# identical to the ones consumption acts on.
from app.services.material_consumption_service import AVAILABLE_ITEM_STATUS, CONSUMPTION_EPSILON

# Discriminators for the two legs of the one inventory query. Needed because the legs
# key on different id spaces -- an ``inventory_items.id`` and a ``parts.id`` of the same
# numeric value would otherwise collide in a single result set.
_LOT_SCOPE = "lot"
_PART_SCOPE = "part"


@dataclass(frozen=True)
class MaterialTieView:
    """One OPEN, operation-scoped material tie, priced against real stock.

    Frozen: this is a read projection handed to display code, and a caller that could
    mutate it would be editing a compliance-adjacent number in place, far from the row
    it came from.

    ``qty_per_run`` is stored RAW, NULL and all. A NULL on an operation-scoped row means
    "not run-scaled" and readers treat it as ``1.0`` (``COALESCE(qty_per_run, 1.0)``, the
    model's rule and the engine's) -- but coalescing here would erase the difference
    between "1 per run" and "nobody set this", which the tie editor needs to show.
    Coalescing is the CALLER's job.
    """

    allocation_id: int
    work_order_id: int
    work_order_operation_id: int
    part_id: int
    part_number: Optional[str]
    part_name: Optional[str]
    unit_of_measure: str
    # Raw -- see the class docstring; COALESCE to 1.0 belongs to the caller.
    qty_per_run: Optional[float]
    qty_planned: float
    # CACHE, not the compliance record: the ledger rows carrying this tie's
    # ``allocation_id`` are authoritative (model docstring, invariant #6).
    qty_consumed: float
    pinned_inventory_item_id: Optional[int]
    pinned_lot_number: Optional[str]
    # Pinned tie: that lot's own on-hand. Unpinned: the FIFO-eligible total for the part.
    on_hand: float
    qty_remaining: float
    short_by: float


def tie_views_for_operations(
    db: Session, *, company_id: int, operation_ids: Sequence[int]
) -> dict[int, list[MaterialTieView]]:
    """OPEN material ties for these operations, with on-hand and shortage. Read-only.

    Returns ``{work_order_operation_id: [MaterialTieView, ...]}`` containing ONLY the
    operations that actually have an open tie -- an untied operation is absent, so
    ``views.get(op.id)`` is falsy and the caller renders nothing (no placeholder, no
    "not tied" nag). Ties are ordered by allocation id within an operation, matching
    ``open_allocations_for_work_order``, so the dispatch chip order and the kiosk line
    order for the same operation are identical and stable across polls.

    Empty ``operation_ids`` short-circuits without touching the database: the dispatch
    board renders an empty column often, and a page of untied work should cost nothing.

    Tenant-scoped throughout (invariant #1) -- the caller passes the ACTIVE company from
    ``get_current_company_id``, never ``current_user.company_id``.
    """
    if not operation_ids:
        return {}
    # De-duplicated and ordered: the same operation can appear twice on a board (two
    # cards of one job), and a stable IN list keeps the query plan and any log line
    # comparable between polls.
    scoped_operation_ids = sorted({int(op_id) for op_id in operation_ids if op_id is not None})
    if not scoped_operation_ids:
        return {}

    allocations = (
        tenant_query(db, WorkOrderMaterialAllocation, company_id)
        .filter(
            WorkOrderMaterialAllocation.work_order_operation_id.in_(scoped_operation_ids),
            # Explicit: CANCELLED ties must never paint a chip, and CLOSED is unreachable
            # today, so "not cancelled" would silently become wrong the day it is written.
            WorkOrderMaterialAllocation.status == AllocationStatus.OPEN,
        )
        .order_by(WorkOrderMaterialAllocation.id)
        .all()
    )
    if not allocations:
        return {}

    part_labels = _part_labels(db, allocations, company_id)
    on_hand_by_allocation = _on_hand_by_allocation(db, allocations, company_id)

    views: dict[int, list[MaterialTieView]] = {}
    for allocation in allocations:
        operation_id = allocation.work_order_operation_id
        if operation_id is None:  # pragma: no cover - excluded by the IN filter above
            continue
        part_number, part_name = part_labels.get(allocation.part_id, (None, None))
        planned = float(allocation.qty_planned or 0.0)
        consumed = float(allocation.qty_consumed or 0.0)
        on_hand = on_hand_by_allocation.get(allocation.id, 0.0)
        remaining = _floor_at_zero(planned - consumed)
        views.setdefault(operation_id, []).append(
            MaterialTieView(
                allocation_id=allocation.id,
                work_order_id=allocation.work_order_id,
                work_order_operation_id=operation_id,
                part_id=allocation.part_id,
                part_number=part_number,
                part_name=part_name,
                unit_of_measure=allocation.unit_of_measure,
                qty_per_run=(float(allocation.qty_per_run) if allocation.qty_per_run is not None else None),
                qty_planned=planned,
                qty_consumed=consumed,
                pinned_inventory_item_id=allocation.pinned_inventory_item_id,
                pinned_lot_number=allocation.pinned_lot_number,
                on_hand=on_hand,
                qty_remaining=remaining,
                short_by=_floor_at_zero(remaining - on_hand),
            )
        )
    return views


def _floor_at_zero(value: float) -> float:
    """Clamp to ``0.0``, treating sub-epsilon residue as exactly zero.

    These are ``Float`` columns, so a tie whose planned quantity has been fully covered
    can land at ``4e-16`` rather than ``0``. A bare ``max(0.0, ...)`` would let that
    residue through and paint a SHORTAGE chip on a fully-stocked tie -- a false alarm on
    a shop-floor screen, which is how operators learn to ignore the indicator.
    ``CONSUMPTION_EPSILON`` is the engine's own threshold, imported rather than
    re-guessed so the display and the ledger agree about what "nothing left" means.
    """
    return value if value > CONSUMPTION_EPSILON else 0.0


def _part_labels(
    db: Session, allocations: list[WorkOrderMaterialAllocation], company_id: int
) -> dict[int, tuple[Optional[str], Optional[str]]]:
    """Part id -> ``(part_number, name)`` for every tied part, in ONE tenant-scoped SELECT.

    Columns only -- loading whole ``Part`` entities (or worse, reading
    ``allocation.part``) would drag the ORM identity map and a lazy load into a read that
    runs on every kiosk poll.

    Deliberately NOT filtered on ``is_deleted``: this is a label lookup for a row the tie
    already references, not a listing of parts. Dropping a soft-deleted part here would
    not hide anything -- the tie still exists and still consumes -- it would just render
    the chip with a blank part number, which is strictly worse for the operator holding
    the material. Matches ``work_order_materials._display_maps``.
    """
    part_ids = sorted({a.part_id for a in allocations if a.part_id is not None})
    if not part_ids:
        return {}
    rows = (
        tenant_query(db, Part, company_id)
        .with_entities(Part.id, Part.part_number, Part.name)
        .filter(Part.id.in_(part_ids))
        .all()
    )
    return {row[0]: (row[1], row[2]) for row in rows}


def _on_hand_by_allocation(
    db: Session, allocations: list[WorkOrderMaterialAllocation], company_id: int
) -> dict[int, float]:
    """Allocation id -> the stock that tie can actually draw on. ONE query, both rules.

    The two rules are genuinely different questions (module docstring): a PINNED tie is
    scored against its own lot's ``quantity_on_hand`` -- held or inactive included, since
    consumption will draw from it anyway -- while an UNPINNED tie is scored against the
    sum over lots matching the FIFO predicate exactly, NULL ``status`` included in the
    exclusion.

    They are asked as two aggregate legs of one ``UNION ALL`` rather than two round
    trips, because this runs on a queue that polls every 10-15 seconds per station across
    the shop and on the dispatch board. The result is bounded by the number of TIES (one
    row per pinned lot, one per unpinned part), never by the number of lots in stock, so
    it stays flat as inventory grows. A leg with no ids is omitted entirely -- an empty
    ``IN ()`` is both pointless and, on some dialects, invalid.

    A pinned lot's part is deliberately NOT added to the part-total leg: the pin means
    that part's other lots are unavailable to this tie, so counting them would be the
    green-chip-over-negative-lot bug the pin rule exists to prevent.

    Both legs are company-scoped (invariant #1); an unmatched key yields ``0.0``.
    """
    pinned_item_ids = sorted(
        {a.pinned_inventory_item_id for a in allocations if a.pinned_inventory_item_id is not None}
    )
    unpinned_part_ids = sorted(
        {a.part_id for a in allocations if a.pinned_inventory_item_id is None and a.part_id is not None}
    )

    legs = []
    if pinned_item_ids:
        legs.append(
            select(
                literal(_LOT_SCOPE).label("scope"),
                InventoryItem.id.label("key"),
                func.coalesce(InventoryItem.quantity_on_hand, 0.0).label("on_hand"),
            ).where(
                InventoryItem.company_id == company_id,
                InventoryItem.id.in_(pinned_item_ids),
            )
        )
    if unpinned_part_ids:
        legs.append(
            select(
                literal(_PART_SCOPE).label("scope"),
                InventoryItem.part_id.label("key"),
                func.coalesce(func.sum(InventoryItem.quantity_on_hand), 0.0).label("on_hand"),
            )
            .where(
                InventoryItem.company_id == company_id,
                InventoryItem.part_id.in_(unpinned_part_ids),
                # The FIFO predicate, verbatim from ``_fifo_source_items``. A NULL
                # ``status`` does not match ``= 'available'`` and is therefore excluded --
                # kept, not "fixed": FIFO will not draw from such a lot either, and
                # promising stock the engine refuses to touch is the worse error.
                InventoryItem.is_active == True,  # noqa: E712
                InventoryItem.status == AVAILABLE_ITEM_STATUS,
                InventoryItem.quantity_on_hand > 0,
            )
            .group_by(InventoryItem.part_id)
        )
    if not legs:  # pragma: no cover - every allocation is either pinned or not
        return {}

    statement = legs[0] if len(legs) == 1 else legs[0].union_all(legs[1])
    lot_on_hand: dict[int, float] = {}
    part_on_hand: dict[int, float] = {}
    for scope, key, quantity in db.execute(statement).all():
        bucket = lot_on_hand if scope == _LOT_SCOPE else part_on_hand
        bucket[int(key)] = float(quantity or 0.0)

    return {
        a.id: (
            lot_on_hand.get(a.pinned_inventory_item_id, 0.0)
            if a.pinned_inventory_item_id is not None
            else part_on_hand.get(a.part_id, 0.0)
        )
        for a in allocations
    }
