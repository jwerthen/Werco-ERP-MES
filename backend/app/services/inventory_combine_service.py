"""Folding two SKUs that describe the same physical article into one.

THE PROBLEM THIS EXISTS FOR
---------------------------
A materials-numbering recut left two part numbers naming the SAME sheet on the
SAME rack: 92 on one number and 141 on the other, 233 sheets in total. Neither
"receive 92 onto the good number" (which mints 92 sheets that do not exist) nor
"issue 92 against a fake work order" (which destroys 92 that do) is acceptable:
the first overstates inventory, the second launders a real quantity through a
production record that never happened. The combine moves the stock across
instead, and the two ledger rows it writes per moved lot line SUM TO EXACTLY
ZERO -- that identity is the whole safety argument and is pinned by a test.

WHAT MOVES, AND WHAT DELIBERATELY DOES NOT
------------------------------------------
* **Quantity moves.** The source lands lower by exactly what the target gains.
* **Traceability moves with it.** When a target stock row has to be created, it
  carries the source row's ``lot_number``, ``serial_number``, ``cert_number``,
  ``heat_lot``, ``supplier_id``, ``po_number``, ``received_date`` and
  ``expiration_date``. The MTR and heat lot follow the physical material, because
  that is the entire point of AS9100D 8.5.2 lot traceability -- a merge that
  dropped them would launder the material's provenance while looking tidy.
* **Cost does NOT get reblended.** A NEWLY CREATED target row copies the source
  row's ``unit_cost`` (the cost travels with the material). An EXISTING target row
  keeps its own ``unit_cost``, untouched. There is no weighted-average recompute
  anywhere in this module; the preview surfaces the delta so a human decides
  whether it matters, which is a decision this code is not entitled to make.
* **The source part is NEVER deleted.** It stays in the catalog at qty 0 so every
  traveler, MTR, PO and spreadsheet naming it keeps resolving. ``deactivate_source``
  can take it out of USE (``is_active=False`` + ``status='obsolete'``); it never
  touches ``is_deleted``.
* **Nothing here renames anything.** ``POST /parts/{id}/renumber`` is a separate
  verb and is untouched: a rename does not imply a merge, and a merge does not
  imply a rename.

REFUSALS COME BEFORE MUTATIONS, ALWAYS
--------------------------------------
Every probe runs before the first ``setattr``/``add``, so a refused combine leaves
every row byte-identical -- the same discipline ``parts.assert_backflush_change_allowed``
and ``part_renumber_service.renumber_part`` follow. ``_combine_blockers`` is the
ONE list of those probes: ``build_combine_preview`` renders it as structured
``blockers`` and the write raises the first entry as an HTTP error built from the
SAME ``detail`` string, so the dialog can never explain a refusal in words that
disagree with the refusal the operator is about to receive.

**The preview is a snapshot, never authorisation.** ``build_combine_preview``
takes no ``AuditService`` and no actor id, so it is STRUCTURALLY unable to write
an audit row, a ledger row or an event -- the same rule ``build_renumber_impact``
and the backflush-readiness companions follow ("a poll is not an actor and
records no reason"). Every input it reads is mutable by other people, so the
write re-runs every probe server-side against the state at write time.

THREE INDEPENDENT RESERVATION RULES, ALL ENFORCED
-------------------------------------------------
1. **Row level, hard, always.** A line can move at most ``quantity_on_hand -
   quantity_allocated``. Allocated material is spoken for at the lot; it never
   crosses.
2. **Row level, lot-directed (pins).** An OPEN tie carrying
   ``pinned_inventory_item_id`` is a reservation against THAT lot and nothing else
   -- the consumption engine locks the pinned row and takes what is there, so
   material drawn off it by anything else becomes a shortage at completion. It is
   therefore withheld exactly the way ``quantity_allocated`` is, per row, and shows
   up on the preview as ``quantity_pinned`` so the operator can see why the cap is
   lower. THE BUG THIS CLOSES: the part-level rule below aggregates per WORK ORDER
   and never read the pin, while the drain is ascending-id, so a 50-unit pin on the
   OLDEST lot was satisfied on paper (``100 - 50 >= 50``) and then drained out of
   the pinned lot first -- the engine later found 10 where 60 had been and recorded
   a 40 shortage while 50 sheets sat on the shelf under the target number.
3. **Part level.** OPEN ``work_order_material_allocations`` naming the source part
   on non-terminal work orders still expect to draw from it. If what the source
   would be left with cannot cover that outstanding demand, the combine is refused
   and the work orders are NAMED, so the operator knows what to untie or re-tie
   first rather than being told only that "something" is reserved.
   **The basis is ``eligible_available``, not ``total_on_hand``.** Those differ by
   the held / quarantined / deactivated rows, and the consumption engine cannot
   draw those any more than this verb can move them: computing the remainder from
   ``total_on_hand`` let a source with "92 free + 92 on hold" satisfy a 92-unit tie
   after moving all 92 free ones away, turning a satisfiable job into an unfillable
   one. Only demand NOT already withheld by rule 2 is counted here, so a pin is
   never charged twice.

WHERE THE MATERIAL LANDS IS CHECKED TOO
---------------------------------------
``_find_stock_row`` resolves the target row by ``(company, part, location, lot)``
and nothing else -- no ``is_active``, no ``status``. Folding available material
onto a target row that is ON HOLD would leave the quantity counted and nobody able
to draw it: 92 usable sheets become 0 usable sheets behind a success response.
``target_row_not_available`` refuses that, and ``target_serial_mismatch`` refuses
merging a serialized lot onto a row carrying a different serial (one serial cannot
name two units -- invariant 5). Both are computed inside ``_combine_blockers``, so
the PREVIEW discloses them rather than the operator discovering them at submit.

DEFERRED IMPORTS FROM ``app.api.endpoints.inventory``
-----------------------------------------------------
``_find_stock_row``, ``_load_inventory_items`` and ``_audit_stock_movement`` live
in the inventory ROUTER module, and that module imports this service, so a
module-level import here would be circular. They are imported inside the
functions that need them -- the same shape ``part_renumber_service`` uses for
``_reconcile_operation_component_quantities``. Re-implementing them here would be
worse than the awkwardness: ``_find_stock_row``'s ``lot_number IS NULL`` branch is
load-bearing (the naive ``lot_number == None`` compiles to ``= NULL`` and never
matches, which is how lot-less receives used to mint duplicate fragment rows), and
``_load_inventory_items`` is what acquires locks in ascending-id order.

LOCK ORDERING, INCLUDING THE AUDIT CHAIN LOCK
---------------------------------------------
Order is: **every source row (ascending id) -> every target landing row -> the
audit hash-chain lock**. The last leg is not decoration.
``AuditService._acquire_chain_lock`` takes a ``pg_advisory_xact_lock`` on ONE
GLOBAL key and holds it to transaction end, so anything that asks for a NEW row
lock after its first audit write is holding a lock the whole system contends on
while waiting for a row lock somebody else may hold. Every sibling stock mutator
(``/receive``, ``/transfer``, ``/adjust``) takes all its row locks BEFORE its first
audit write; this one used to write line 1's audit rows inside the drain loop and
then ask for line 2's target row, which deadlocks against a ``/receive`` onto that
row that already holds it and is about to write its own audit row. Postgres aborts
a victim, and there is no DBAPI deadlock handler, so it surfaced as a 500 on a
stock verb. Two things now prevent it, deliberately belt-and-braces: every target
landing row is resolved and locked BEFORE the first write, and the per-line audit
calls are BUFFERED and emitted after the drain loop. Do not move an audit write
back inside the loop.
"""

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple, cast

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.ledger_filter import LEDGER_QUANTITY_EPSILON
from app.models.inventory import InventoryItem, InventoryTransaction, TransactionType
from app.models.inventory_combine import (
    COMBINE_IN_REASON_CODE,
    COMBINE_OUT_REASON_CODE,
    COMBINE_REFERENCE_TYPE,
    InventoryCombine,
    format_combine_number,
)
from app.models.part import Part, uom_label
from app.models.part_number_alias import normalize_alias_key
from app.models.work_order import WorkOrder
from app.models.work_order_material import AllocationStatus, WorkOrderMaterialAllocation
from app.services.audit_service import AuditService
from app.services.material_consumption_service import AVAILABLE_ITEM_STATUS, is_consumable_item
from app.services.operational_event_service import OperationalEventService
from app.services.sheet_stock_spec import (
    canonical_alloy,
    derive_sheet_spec,
    dims_inches,
    is_sheet_like,
    thickness_inches,
)
from app.services.work_order_state_service import TERMINAL_WO_STATUSES

logger = logging.getLogger(__name__)

# The state a deactivated part is parked in. ``is_active`` and ``status`` are
# written TOGETHER everywhere in this feature so the two can never disagree --
# ``delete_part`` / ``restore_part`` already pair them, and a row where one says
# "in use" and the other says "obsolete" is unreadable by anything.
OBSOLETE_PART_STATUS = "obsolete"
ACTIVE_PART_STATUS = "active"

# Words the owner asked to be careful of before folding a SKU away: the CNC job
# and the housing/test parts that were mid-flight when this feature was specified.
#
# This is an ACKNOWLEDGEMENT GATE, NOT A BAN, and that distinction is the whole
# design. "Housing" is an ordinary manufacturing word -- the Miratech housing is a
# real production part -- so a ban would refuse exactly the legitimate work this
# shop does. Instead the request must name the flagged part's id in
# ``acknowledge_flagged_part_ids``, which turns a caution into a decision somebody
# made on purpose and which the audit row records.
FLAGGED_PART_TOKENS = ("test", "housing")

# Word-boundary matching, hand-rolled rather than ``\b`` on purpose. ``\b`` treats
# ``_`` as a word character, so ``\btest\b`` does NOT match ``TEST_FIXTURE`` -- a
# part that is obviously a test fixture. This lookaround treats any non-alphanumeric
# as a separator, so ``TEST_FIXTURE``, ``TEST FIXTURE`` and ``HOUSING-A`` all flag,
# while ``TESTA-500`` and ``WAREHOUSING`` (alphanumeric on one side) do not.
_FLAGGED_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = tuple(
    (token, re.compile(rf"(?<![0-9A-Za-z]){re.escape(token)}(?![0-9A-Za-z])", re.IGNORECASE))
    for token in FLAGGED_PART_TOKENS
)

# The HTTP status each refusal carries. Kept as data beside the codes so the
# preview's ``blockers`` list and the write's exception can never drift: one code,
# one sentence, one status.
#
# ``same_part`` / ``part_deleted`` are 400 (the request names something that cannot
# be combined at all); everything else is 409 (the request is well formed but the
# current state refuses it), matching ``/inventory/receive``'s deleted-part 400 and
# the renumber verb's 409s.
_BLOCKER_STATUS: Dict[str, int] = {
    "same_part": 400,
    "part_not_found": 404,
    "part_deleted": 400,
    # 400, like ``same_part``: the request names a quantity that cannot be moved at
    # all. In practice the router never reaches it -- ``InventoryCombineRequest``'s
    # ``gt=MINIMUM_COMBINE_QUANTITY`` bound refuses the same input with a 422 first
    # -- it is the backstop for callers that reach the service directly.
    "quantity_below_minimum": 400,
    "unit_of_measure_mismatch": 409,
    "no_available_stock": 409,
    "quantity_exceeds_available": 409,
    "open_work_order_reservation": 409,
    "target_row_not_available": 409,
    "target_serial_mismatch": 409,
    "flagged_part_not_acknowledged": 409,
    "expected_part_number_mismatch": 409,
    "source_still_has_stock": 409,
}

# The key ``_find_stock_row`` resolves a target row by: ``(location, lot_number)``.
# Kept as one alias because THREE things have to agree on it -- the drain plan, the
# ``target_row_not_available`` probe, and the row the write actually increments --
# and a fourth spelling of the same tuple is how they would drift apart.
_LandingKey = Tuple[Optional[str], Optional[str]]

# A transient value for ``combine_number`` that lives only between the header
# INSERT and the UPDATE that stamps the real number, because the number is minted
# from the primary key the INSERT produces. It must be UNIQUE per request:
# ``uq_inventory_combines_company_number`` is checked per statement, so two
# concurrent combines inserting the same literal placeholder would collide on
# Postgres. It never survives the transaction.
_PENDING_NUMBER_PREFIX = "PENDING-"


@dataclass
class CombineDiagnostic:
    """One refusal reason, or one disclosure that does not refuse."""

    code: str
    detail: str


@dataclass
class FlaggedPart:
    part_id: int
    part_number: str
    matched_token: str
    field: str


@dataclass
class OpenReservation:
    work_order_id: int
    work_order_number: str
    work_order_status: str
    outstanding_quantity: float


@dataclass
class SourceReservations:
    """Every open claim on the source part, split by the SHAPE of the claim.

    Two shapes, because they bind differently and mixing them double-counts:

    * ``by_work_order`` -- outstanding demand aggregated per job, for the message
      the operator reads and for the part-level cap.
    * ``pinned_by_item`` -- outstanding demand that names ONE lot
      (``pinned_inventory_item_id``). The consumption engine locks that row and
      draws from it alone, so this binds per row, exactly like
      ``quantity_allocated``, and is withheld at the row rather than counted in the
      part-level total.

    ``total`` is every open claim regardless of shape -- what the preview reports as
    ``reserved_quantity``. The part-level rule uses ``unpinned_total`` instead, so a
    pin is never charged both at the row and again against the remainder.
    """

    by_work_order: List[OpenReservation] = field(default_factory=list)
    pinned_by_item: Dict[int, float] = field(default_factory=dict)
    pinned_work_orders: Dict[int, List[str]] = field(default_factory=dict)

    @property
    def total(self) -> float:
        return sum(r.outstanding_quantity for r in self.by_work_order)

    def unpinned_total(self, withheld: float) -> float:
        """Demand NOT already protected by a per-row withholding.

        ``withheld`` is what the source picture actually held back at the pinned
        rows, which can be less than the pinned demand when the pinned lot does not
        hold enough. The floor at zero matters: a pin larger than its lot is already
        a shortage, and letting the subtraction go negative would hand the combine
        credit for demand nobody can satisfy.
        """
        return max(0.0, self.total - max(0.0, withheld))


@dataclass
class PlannedLine:
    """One source row this combine intends to draw from, and how much.

    Computed BEFORE any write, from the same per-row caps the drain loop obeys, so
    the refusal probes and the write can never model different moves. It is also
    what tells the ``target_row_not_available`` probe which target rows are actually
    in play -- a held target row at a location this combine never touches is not a
    reason to refuse anything.
    """

    inventory_item_id: int
    location: Optional[str] = None
    lot_number: Optional[str] = None
    serial_number: Optional[str] = None
    quantity: float = 0.0

    @property
    def landing_key(self) -> _LandingKey:
        return (self.location, self.lot_number)


@dataclass
class CombineStockLine:
    """One stock row, and how much of it this combine may move.

    ``quantity_available`` keeps its platform-wide meaning (``on_hand -
    allocated``). ``quantity_combinable`` is the figure this verb actually draws
    against: available MINUS anything a lot-directed (pinned) open tie is holding on
    this exact row. ``unavailable_reason`` is the STATUS half of eligibility only --
    "this row is on hold / deactivated" -- with no availability clause, because a
    TARGET row is a perfectly good landing site at zero on hand but is not one while
    it is quarantined. Splitting the two is what lets one predicate serve both sides.
    """

    inventory_item_id: int
    location: Optional[str] = None
    warehouse: Optional[str] = None
    lot_number: Optional[str] = None
    serial_number: Optional[str] = None
    quantity_on_hand: float = 0.0
    quantity_allocated: float = 0.0
    quantity_available: float = 0.0
    quantity_pinned: float = 0.0
    quantity_combinable: float = 0.0
    unit_cost: float = 0.0
    status: Optional[str] = None
    is_active: bool = True
    eligible: bool = False
    ineligible_reason: Optional[str] = None
    # Not part of the API contract: the status-only verdict, reused for the target
    # landing check. ``model_validate(..., from_attributes=True)`` only reads the
    # fields the schema declares, so carrying it here costs the wire nothing.
    unavailable_reason: Optional[str] = None
    pinned_work_orders: List[str] = field(default_factory=list)


@dataclass
class PartStockPicture:
    """One side of a combine: the part, its stock rows, and the derived totals."""

    part_id: int
    part_number: str
    name: str
    part_type: Optional[str] = None
    unit_of_measure: Optional[str] = None
    is_active: bool = True
    status: Optional[str] = None
    is_deleted: bool = False
    total_on_hand: float = 0.0
    total_allocated: float = 0.0
    total_available: float = 0.0
    total_pinned: float = 0.0
    eligible_available: float = 0.0
    lines: List[CombineStockLine] = field(default_factory=list)


@dataclass
class CombineCost:
    source_weighted_unit_cost: float = 0.0
    target_weighted_unit_cost: float = 0.0
    differs: bool = False
    note: str = ""


@dataclass
class CombinePreview:
    """What a combine would do. Produced by a PURE READ -- see the module docstring."""

    source: PartStockPicture
    target: PartStockPicture
    unit_of_measure_match: bool = True
    default_quantity: float = 0.0
    max_combinable_quantity: float = 0.0
    reserved_quantity: float = 0.0
    eligible: bool = False
    blockers: List[CombineDiagnostic] = field(default_factory=list)
    advisories: List[CombineDiagnostic] = field(default_factory=list)
    flagged_parts: List[FlaggedPart] = field(default_factory=list)
    open_source_reservations: List[OpenReservation] = field(default_factory=list)
    cost: CombineCost = field(default_factory=CombineCost)


@dataclass
class CombineLine:
    """One (location, lot) line that actually moved."""

    location: Optional[str]
    lot_number: Optional[str]
    quantity: float
    unit_cost: float
    source_inventory_item_id: int
    target_inventory_item_id: int
    target_row_created: bool


@dataclass
class CombineResult:
    combine_id: int
    combine_number: str
    source_part_id: int
    source_part_number: str
    target_part_id: int
    target_part_number: str
    quantity_moved: float
    lines_moved: int
    source_quantity_before: float
    source_quantity_after: float
    target_quantity_before: float
    target_quantity_after: float
    source_deactivated: bool = False
    lines: List[CombineLine] = field(default_factory=list)
    transaction_ids: List[int] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #


def _resolve_part(db: Session, company_id: int, part_id: int) -> Part:
    """A part in the ACTIVE company, or 404. Deliberately does NOT filter ``is_deleted``.

    A soft-deleted part is refused, but as its own diagnostic (``part_deleted``,
    **400**, "restore it or use a different part number" -- the wording
    ``/inventory/receive`` already uses), not as a 404. That matters twice: the
    preview has to be able to SHOW the operator that the number they picked is
    deleted (``PartStockSummary`` carries ``is_deleted`` for exactly that), and a
    404 would send someone hunting for a typo in a number that is spelled
    correctly.

    Another company's part id behaves as 404 here, unconditionally -- invariant 1.
    """
    part = db.query(Part).filter(Part.id == part_id, Part.company_id == company_id).first()
    if not part:
        raise HTTPException(status_code=404, detail="Part not found")
    return part


def _stock_rows(db: Session, company_id: int, part_id: int) -> List[InventoryItem]:
    """Every stock row for a part, tenant-scoped, in ASCENDING ID ORDER.

    Ascending id is not cosmetic: it is simultaneously the drain order (oldest row
    first, approximately FIFO) and the lock-acquisition order, so a combine cannot
    deadlock against itself and two concurrent combines over the same part take the
    same rows in the same sequence.

    Ineligible rows are returned too. Deciding eligibility is
    ``_picture_from_rows``'s job, and reporting the excluded quantities is the
    preview's -- material on hold or in quarantine is never silently folded into
    another SKU, and never silently hidden either.
    """
    return (
        db.query(InventoryItem)
        .filter(InventoryItem.company_id == company_id, InventoryItem.part_id == part_id)
        .order_by(InventoryItem.id.asc())
        .all()
    )


def _unavailable_reason(item: InventoryItem) -> Optional[str]:
    """Why this row's material is not usable at all, or ``None`` when it is.

    THE gate is ``is_consumable_item`` -- the same predicate the
    material-consumption engine draws lots with. Two rules for "may this lot be
    used" would eventually disagree, and it matters that this one is not
    accidentally STRICTER either: a NULL ``status`` reads as available there (the
    column has a Python-side default and no server default, so legacy rows carry
    NULL), and reading it as held here would hide real stock from the operator.

    The branches below only DECOMPOSE that verdict into a sentence; they never
    decide it.

    Deliberately STATUS-ONLY -- no availability clause. This is the half that
    applies to BOTH sides: a source row with nothing free cannot contribute, but a
    TARGET row with nothing on hand is a perfectly good place to land material,
    while a quarantined target row is not. ``_ineligible_reason`` adds the source
    side's availability clause on top.
    """
    if is_consumable_item(item):
        return None
    if not item.is_active:
        return "this stock row is deactivated"
    status = (item.status or AVAILABLE_ITEM_STATUS).strip().lower()
    return f"material is {status.replace('_', ' ')}, not available"


def _ineligible_reason(item: InventoryItem, combinable: float, pinned: float) -> Optional[str]:
    """Why this SOURCE row cannot be moved, in the operator's words, or ``None``.

    ``combinable`` is already net of both per-row reservations (``quantity_allocated``
    and any lot-directed pin), so the "nothing free" branch reports the cap that
    actually bit -- naming the pin when the pin is what emptied it, because
    "nothing free on this row" against a row visibly holding 60 sheets reads as a bug.
    """
    reason = _unavailable_reason(item)
    if reason is not None:
        return reason
    if combinable <= LEDGER_QUANTITY_EPSILON:
        if pinned > LEDGER_QUANTITY_EPSILON:
            return (
                f"reserved to a specific lot by open work orders (on hand "
                f"{item.quantity_on_hand or 0:g}, allocated {item.quantity_allocated or 0:g}, "
                f"pinned {pinned:g})"
            )
        return (
            f"nothing free on this row (on hand {item.quantity_on_hand or 0:g}, "
            f"allocated {item.quantity_allocated or 0:g})"
        )
    return None


def _picture_from_rows(
    part: Part,
    rows: Sequence[InventoryItem],
    reservations: Optional[SourceReservations] = None,
) -> PartStockPicture:
    """Totals and per-row eligibility for one side of a combine.

    ``eligible_available`` -- the figure that actually bounds the move -- is the
    sum of ``quantity_combinable`` over the ELIGIBLE rows only. It differs from
    ``total_available`` by exactly the stock that is held, quarantined, rejected,
    sitting on a deactivated row, or withheld by a lot-directed pin, which is why
    all of them are reported: an operator seeing "141 available" but a 92 cap
    deserves to see where the gap is.

    ``reservations`` is passed for the SOURCE side only. Pins on the TARGET part
    restrict nothing about adding material to it, so passing them there would
    invent a cap that does not exist.
    """
    pinned_by_item = reservations.pinned_by_item if reservations is not None else {}
    pinned_work_orders = reservations.pinned_work_orders if reservations is not None else {}

    lines: List[CombineStockLine] = []
    total_on_hand = 0.0
    total_allocated = 0.0
    total_pinned = 0.0
    eligible_available = 0.0

    for item in rows:
        on_hand = float(item.quantity_on_hand or 0.0)
        allocated = float(item.quantity_allocated or 0.0)
        available = on_hand - allocated
        total_on_hand += on_hand
        total_allocated += allocated

        # A pin can only withhold material that is actually on the row. A pin larger
        # than its lot is already a shortage; letting it withhold more than exists
        # would push ``combinable`` negative and understate every other row via the
        # part-level subtraction.
        pinned_demand = float(pinned_by_item.get(item.id, 0.0))
        withheld = min(max(0.0, pinned_demand), max(0.0, available))
        combinable = max(0.0, available - withheld)
        total_pinned += withheld

        reason = _ineligible_reason(item, combinable, withheld)
        eligible = reason is None
        if eligible:
            eligible_available += combinable

        lines.append(
            CombineStockLine(
                inventory_item_id=item.id,
                location=item.location,
                warehouse=item.warehouse,
                lot_number=item.lot_number,
                serial_number=item.serial_number,
                quantity_on_hand=on_hand,
                quantity_allocated=allocated,
                quantity_available=available,
                quantity_pinned=withheld,
                quantity_combinable=combinable,
                unit_cost=float(item.unit_cost or 0.0),
                status=item.status,
                is_active=bool(item.is_active),
                eligible=eligible,
                ineligible_reason=reason,
                unavailable_reason=_unavailable_reason(item),
                pinned_work_orders=list(pinned_work_orders.get(item.id, ())),
            )
        )

    return PartStockPicture(
        part_id=part.id,
        part_number=part.part_number or "",
        name=part.name or "",
        part_type=str(getattr(part.part_type, "value", part.part_type) or "") or None,
        unit_of_measure=str(getattr(part.unit_of_measure, "value", part.unit_of_measure) or "") or None,
        is_active=bool(part.is_active),
        status=part.status,
        is_deleted=bool(part.is_deleted),
        total_on_hand=total_on_hand,
        total_allocated=total_allocated,
        total_available=total_on_hand - total_allocated,
        total_pinned=total_pinned,
        eligible_available=eligible_available,
        lines=lines,
    )


def _stock_line_snapshot(item: InventoryItem) -> CombineStockLine:
    """One stock row as a plain value object, with NO source-side caps applied.

    Used for the TARGET landing rows, where the only questions are "is this row
    usable?" and "what serial does it carry?" -- so it deliberately carries no
    eligibility verdict and no combinable figure. Handing the probes a value object
    rather than the ORM row keeps ``_combine_blockers`` identical between the
    preview (which never loads locked rows) and the write (which only loads locked
    ones).
    """
    on_hand = float(item.quantity_on_hand or 0.0)
    allocated = float(item.quantity_allocated or 0.0)
    return CombineStockLine(
        inventory_item_id=item.id,
        location=item.location,
        warehouse=item.warehouse,
        lot_number=item.lot_number,
        serial_number=item.serial_number,
        quantity_on_hand=on_hand,
        quantity_allocated=allocated,
        quantity_available=on_hand - allocated,
        unit_cost=float(item.unit_cost or 0.0),
        status=item.status,
        is_active=bool(item.is_active),
        unavailable_reason=_unavailable_reason(item),
    )


def part_total_on_hand(db: Session, company_id: int, part_id: int) -> float:
    """Total on-hand across EVERY stock row for a part, tenant-scoped.

    Every row, deliberately: held, quarantined and deactivated rows are still
    stock on a shelf. This is what "does this part still have material?" means to
    the two callers that ask -- the ``source_still_has_stock`` refusal here, and
    the ``part_still_has_stock`` refusal on ``POST /parts/{id}/deactivate``, which
    imports it rather than growing a second definition.
    """
    total = (
        db.query(func.coalesce(func.sum(InventoryItem.quantity_on_hand), 0.0))
        .filter(InventoryItem.company_id == company_id, InventoryItem.part_id == part_id)
        .scalar()
    )
    return float(total or 0.0)


def _flag_for_part(part: Part) -> Optional[FlaggedPart]:
    """The first flagged token in this part's number or name, if any.

    ONE entry per part, not one per matching field: the acknowledgement is
    per-part (the request carries ``acknowledge_flagged_part_ids``), so a dialog
    rendering two checkboxes for one part would be asking the same question twice.
    ``part_number`` is checked before ``name`` because that is the identifier the
    operator typed.
    """
    for source_field, value in (("part_number", part.part_number), ("name", part.name)):
        text = value or ""
        for token, pattern in _FLAGGED_PATTERNS:
            if pattern.search(text):
                return FlaggedPart(
                    part_id=part.id,
                    part_number=part.part_number or "",
                    matched_token=token,
                    field=source_field,
                )
    return None


def _flagged_parts(source: Part, target: Part) -> List[FlaggedPart]:
    """Flagged parts on either side, de-duplicated by part id.

    De-duplication matters for the ``same_part`` request, which is refused anyway
    but is still previewed: one part must not produce two identical checkboxes.
    """
    flags: Dict[int, FlaggedPart] = {}
    for part in (source, target):
        flag = _flag_for_part(part)
        if flag is not None:
            flags.setdefault(flag.part_id, flag)
    return list(flags.values())


def source_reservations(db: Session, company_id: int, part_id: int) -> SourceReservations:
    """Open work-order material ties still expecting to draw this part.

    **The basis is the PLAN (``qty_planned``), not the live consumption target.**
    ``material_consumption_service._live_consumption_target`` computes
    ``qty_per_run x (complete + scrapped)`` for an operation-scoped tie, which is
    the right basis for bounding a RETURN (you cannot give back what was never
    drawn) and the WRONG basis for a reservation: a released job that has not run
    a single part yet would report zero demand, and this check exists precisely to
    stop a combine stranding that job. ``qty_planned`` is what the tie was raised
    for (``laser_nest_service`` writes ``qty_per_run x planned_runs`` into it), so
    ``qty_planned - qty_consumed`` is what the job still expects to pull off the
    shelf. Do not "align" this with the return bound; they answer different
    questions.

    Terminal work orders are excluded via the shared ``TERMINAL_WO_STATUSES`` --
    a COMPLETE/CLOSED/CANCELLED job cannot draw anything, and since nothing
    currently closes a tie at completion, counting them would block this verb
    forever on any part the shop has ever consumed. Soft-deleted work orders are
    excluded for the same reason: this is a SELECTION read (what can still
    happen), not a historical one, so invariant 3's tombstone filter applies.

    Aggregated per work order so the refusal can name jobs rather than tie ids --
    the operator's next action is to open a work order and untie or re-tie it.

    **``pinned_inventory_item_id`` is read here, and that is not optional.** A pinned
    tie is a claim on ONE lot: the consumption engine locks that row and draws from
    it alone. The per-work-order aggregate above cannot express that, and a combine
    that satisfied only the aggregate would happily drain the pinned lot first (the
    drain is ascending-id, i.e. oldest lot first) and leave the pin looking at an
    empty shelf. So the pinned demand is ALSO returned per inventory item, and the
    picture withholds it at the row -- see ``SourceReservations``. Do not collapse
    the two maps back into one number.
    """
    rows = (
        db.query(WorkOrderMaterialAllocation, WorkOrder)
        .join(WorkOrder, WorkOrder.id == WorkOrderMaterialAllocation.work_order_id)
        .filter(
            WorkOrderMaterialAllocation.company_id == company_id,
            WorkOrderMaterialAllocation.part_id == part_id,
            WorkOrderMaterialAllocation.status == AllocationStatus.OPEN,
            WorkOrder.company_id == company_id,
            WorkOrder.is_deleted == False,  # noqa: E712
            WorkOrder.status.notin_(TERMINAL_WO_STATUSES),
        )
        .order_by(WorkOrderMaterialAllocation.id.asc())
        .all()
    )

    by_work_order: Dict[int, OpenReservation] = {}
    pinned_by_item: Dict[int, float] = {}
    pinned_work_orders: Dict[int, List[str]] = {}
    for allocation, work_order in rows:
        outstanding = float(allocation.qty_planned or 0.0) - float(allocation.qty_consumed or 0.0)
        if outstanding <= LEDGER_QUANTITY_EPSILON:
            continue
        label = work_order.work_order_number or f"work order {work_order.id}"
        existing = by_work_order.get(work_order.id)
        if existing is None:
            by_work_order[work_order.id] = OpenReservation(
                work_order_id=work_order.id,
                work_order_number=label,
                work_order_status=str(getattr(work_order.status, "value", work_order.status) or ""),
                outstanding_quantity=outstanding,
            )
        else:
            existing.outstanding_quantity += outstanding

        pinned_item_id = allocation.pinned_inventory_item_id
        if pinned_item_id is not None:
            pinned_by_item[pinned_item_id] = pinned_by_item.get(pinned_item_id, 0.0) + outstanding
            jobs = pinned_work_orders.setdefault(pinned_item_id, [])
            if label not in jobs:
                jobs.append(label)

    return SourceReservations(
        by_work_order=sorted(by_work_order.values(), key=lambda r: r.work_order_number),
        pinned_by_item=pinned_by_item,
        pinned_work_orders=pinned_work_orders,
    )


def open_source_reservations(db: Session, company_id: int, part_id: int) -> List[OpenReservation]:
    """The per-work-order half of ``source_reservations``. Kept as the public name.

    Thin on purpose: everything inside this module wants the pinned map too, and one
    query has to produce both or the two halves can be read a moment apart and
    disagree about what is open.
    """
    return source_reservations(db, company_id, part_id).by_work_order


def _weighted_unit_cost(picture: PartStockPicture) -> Tuple[float, bool]:
    """(quantity-weighted unit cost, whether there was any stock to weight).

    Rows with non-positive on-hand are skipped: a driven-negative lot (the
    shortage posture deliberately allows one) would otherwise pull the average in
    a direction that means nothing.
    """
    value = 0.0
    quantity = 0.0
    for line in picture.lines:
        if line.quantity_on_hand <= 0:
            continue
        value += line.quantity_on_hand * line.unit_cost
        quantity += line.quantity_on_hand
    if quantity <= LEDGER_QUANTITY_EPSILON:
        return 0.0, False
    return value / quantity, True


def _cost_summary(source: PartStockPicture, target: PartStockPicture) -> CombineCost:
    """What each side is carried at, and whether the operator should look twice.

    ``differs`` requires BOTH sides to actually have stock: comparing a real cost
    against the ``0.0`` that means "nothing on hand" would raise an alarm about a
    difference that does not exist.
    """
    source_cost, source_has_stock = _weighted_unit_cost(source)
    target_cost, target_has_stock = _weighted_unit_cost(target)
    differs = source_has_stock and target_has_stock and abs(source_cost - target_cost) > 0.005

    note = (
        "Costs are never reblended by a combine. A target lot created by this move carries the "
        "source lot's unit cost; a target lot that already exists keeps its own."
    )
    if differs:
        note += (
            f" The two sides are currently carried at different unit costs "
            f"({source_cost:,.4f} vs {target_cost:,.4f}) — the material moves, the target's cost basis does not."
        )
    return CombineCost(
        source_weighted_unit_cost=source_cost,
        target_weighted_unit_cost=target_cost,
        differs=differs,
        note=note,
    )


def _join_words(words: Sequence[str]) -> str:
    """``["a"]`` -> ``"a"``; ``["a", "b"]`` -> ``"a and b"``; ``["a", "b", "c"]`` -> ``"a, b and c"``."""
    words = list(words)
    if len(words) <= 1:
        return "".join(words)
    return f"{', '.join(words[:-1])} and {words[-1]}"


def _sheet_reading_agrees(
    source_text: Optional[str],
    target_text: Optional[str],
    normalizer,
) -> Optional[bool]:
    """Do the two sides agree on one spec field? ``None`` when it is not comparable.

    THREE answers, not two, and the third is the important one. A field stated on
    only ONE side is NOT a disagreement -- it is a field the other number does not
    talk about. That is the same rule ``uom_disagrees`` follows for units and
    ``canonical_alloy``'s docstring states for grades ("no grade stated here" is a
    DIFFERENT answer from a wrong one), and it is what makes the flagship case
    behave: ``.0625-60X144-304SS`` parses as an anchored triple, while
    ``SH-A240-304-0.0625-60X144-2B`` does not (the triple grammar is anchored to
    the start of the string and that number leads with a spec designation), so the
    target states only its GRADE. Treating "not stated" as "different" would raise
    a wrong-material alarm on exactly the pair this feature was built for.

    Comparison goes through the spec module's own normalizers -- ``thickness_inches``
    for a thickness, ``dims_inches`` for a size (which sorts the pair ascending, so
    ``60X144`` and ``144X60`` are one sheet) -- so a formatting difference is not
    reported as a material difference. When a normalizer cannot read one side, the
    raw text is compared case-insensitively rather than the field being dropped.
    """
    if not source_text or not target_text:
        return None
    if normalizer is not None:
        source_value = normalizer(source_text)
        target_value = normalizer(target_text)
        if source_value is not None and target_value is not None:
            return source_value == target_value
    return source_text.strip().upper() == target_text.strip().upper()


def _sheet_advisories(source: Part, target: Part) -> List[CombineDiagnostic]:
    """What the laser-nest matcher reads out of the two part numbers.

    For sheet and plate the part NUMBER **is** the material spec -- thickness, size
    and grade are parsed out of the string because ``Part`` carries no such columns.
    So this is the only automatic guard against folding a 304 SKU into a 316 one,
    and it is worth having.

    **Advisory, never a blocker**, for exactly the reason ``build_renumber_impact``
    reports rather than refuses: if the CURRENT string is wrong then the matcher is
    ALREADY mis-matching, and refusing would block the very case the feature exists
    for -- ``.0625-60X144-304SS`` -> ``SH-A240-304-0.0625-60X144-2B`` must be
    allowed. It is also unenforceable: part numbers are free text, so a refusal only
    pushes the operator to a spelling that defeats the check.

    Only fields BOTH numbers state are compared -- see ``_sheet_reading_agrees``.

    The alias table is deliberately NOT consulted. ``part_number_alias``'s module
    docstring forbids alias reads in the sheet matcher: an alias is a STALE spec
    string, and a part presenting two material specs at once becomes a pre-fill
    candidate for two incompatible nests simultaneously.
    """
    out: List[CombineDiagnostic] = []
    source_sheet = is_sheet_like(source.part_number, source.name, source.description)
    target_sheet = is_sheet_like(target.part_number, target.name, target.description)
    if not (source_sheet or target_sheet):
        return out

    source_spec = derive_sheet_spec(source.part_number, source.name)
    target_spec = derive_sheet_spec(target.part_number, target.name)
    # ``canonical_alloy`` is tried on the number first and the name second, exactly
    # as ``part_renumber_service._sheet_delta`` does -- the number is the shop's
    # canonical identifier and the name is prose that drifts.
    source_alloy = canonical_alloy(source.part_number) or canonical_alloy(source.name)
    target_alloy = canonical_alloy(target.part_number) or canonical_alloy(target.name)

    readings: List[Tuple[str, Optional[str], Optional[str], Optional[bool]]] = [
        (label, src, tgt, _sheet_reading_agrees(src, tgt, normalizer))
        for label, src, tgt, normalizer in (
            ("thickness", source_spec.thickness, target_spec.thickness, thickness_inches),
            ("size", source_spec.sheet_size, target_spec.sheet_size, dims_inches),
            ("grade", source_alloy, target_alloy, None),
        )
    ]

    disagreements = [r for r in readings if r[3] is False]
    agreements = [r for r in readings if r[3] is True]

    if disagreements:
        detail = "; ".join(f"{label} {src} vs {tgt}" for label, src, tgt, _ in disagreements)
        out.append(
            CombineDiagnostic(
                code="sheet_spec_mismatch",
                detail=(
                    f"These part numbers describe DIFFERENT material ({detail}). Combining them would "
                    "put one grade or size of sheet onto the other's number, and the nest screen would "
                    "then offer it for nests it does not fit. Make sure this really is one sheet."
                ),
            )
        )
    elif agreements:
        detail = ", ".join(f"{label} {src}" for label, src, _tgt, _ in agreements)
        unstated = [label for label, _src, _tgt, verdict in readings if verdict is None]
        note = f" Only one of them states {_join_words(unstated)}, so that could not be checked." if unstated else ""
        out.append(
            CombineDiagnostic(
                code="sheet_spec_match",
                detail=(f"Both part numbers agree on {detail}, read from the numbers themselves.{note}"),
            )
        )
    else:
        out.append(
            CombineDiagnostic(
                code="sheet_spec_unreadable",
                detail=(
                    "These numbers state no material spec in common, so nothing about the thickness, "
                    "size or grade could be checked automatically. The nest screen reads all three out "
                    "of the part number — confirm by hand that this is one sheet."
                ),
            )
        )
    return out


def _excluded_stock_advisories(picture: PartStockPicture) -> List[CombineDiagnostic]:
    """One advisory per source row this combine will NOT move, with its quantity.

    Held, quarantined, rejected and deactivated stock is excluded from the move and
    said so explicitly. Silently folding it would move material somebody put a hold
    on; silently omitting it would leave the operator wondering why the numbers do
    not add up.
    """
    out: List[CombineDiagnostic] = []
    for line in picture.lines:
        if line.eligible or abs(line.quantity_on_hand) <= LEDGER_QUANTITY_EPSILON:
            continue
        where = line.location or "(no location)"
        lot = f" lot {line.lot_number}" if line.lot_number else ""
        out.append(
            CombineDiagnostic(
                code="non_available_stock_excluded",
                detail=(
                    f"{line.quantity_on_hand:g} at {where}{lot} will NOT move: "
                    f"{line.ineligible_reason or 'not available'}."
                ),
            )
        )
    return out


def _pinned_stock_advisories(picture: PartStockPicture) -> List[CombineDiagnostic]:
    """One advisory per source row a lot-directed tie is holding material on.

    Without this the cap simply looks wrong: the dialog shows a row with 60 on hand,
    nothing allocated and an available figure of 60, and then refuses to move more
    than 10 of it. Naming the pinned quantity AND the jobs holding it turns an
    inexplicable number into an action -- go to that work order and re-point or
    release the pin.
    """
    out: List[CombineDiagnostic] = []
    for line in picture.lines:
        if line.quantity_pinned <= LEDGER_QUANTITY_EPSILON:
            continue
        where = line.location or "(no location)"
        lot = f" lot {line.lot_number}" if line.lot_number else ""
        jobs = _join_words(line.pinned_work_orders) if line.pinned_work_orders else "an open work order"
        out.append(
            CombineDiagnostic(
                code="pinned_lot_reserved",
                detail=(
                    f"{line.quantity_pinned:g} at {where}{lot} is pinned to {jobs} and will NOT move: "
                    "that job draws from this lot specifically, so moving it would strand the job even "
                    "though the total looks sufficient."
                ),
            )
        )
    return out


# --------------------------------------------------------------------------- #
# The drain plan, and where each line would land
# --------------------------------------------------------------------------- #


def _plan_lines(source_picture: PartStockPicture, quantity: float) -> List[PlannedLine]:
    """Which source rows this combine would draw from, and how much off each.

    ONE definition of the draw, computed before any write and reused by the write,
    so a refusal probe can never be modelling a different move than the loop
    performs. Ascending id (the order ``_stock_rows`` returns) is simultaneously
    the FIFO drain order and the lock-acquisition order.

    Every cap is already baked into ``quantity_combinable``: ``quantity_allocated``
    and any lot-directed pin. Nothing here may re-derive them.
    """
    remaining = float(quantity)
    planned: List[PlannedLine] = []
    for line in source_picture.lines:
        if remaining <= LEDGER_QUANTITY_EPSILON:
            break
        if not line.eligible or line.quantity_combinable <= LEDGER_QUANTITY_EPSILON:
            continue
        take = min(remaining, line.quantity_combinable)
        planned.append(
            PlannedLine(
                inventory_item_id=line.inventory_item_id,
                location=line.location,
                lot_number=line.lot_number,
                serial_number=line.serial_number,
                quantity=take,
            )
        )
        remaining -= take
    return planned


def _landing_keys(planned: Sequence[PlannedLine]) -> List[_LandingKey]:
    """The distinct ``(location, lot)`` keys the plan would land on, in plan order.

    De-duplicated because two source rows can share a ``(location, lot)`` -- a
    legacy fragmented set does exactly that -- and both would land on the SAME
    target row.
    """
    keys: List[_LandingKey] = []
    for line in planned:
        if line.landing_key not in keys:
            keys.append(line.landing_key)
    return keys


def _landing_rows_from_picture(target_picture: PartStockPicture) -> Dict[_LandingKey, CombineStockLine]:
    """The target row each ``(location, lot)`` would resolve to, from a read-only picture.

    Mirrors ``_find_stock_row``: same key, and FIRST match wins because that query
    orders by ascending id and takes ``.first()``. ``_stock_rows`` hands the picture
    back in ascending-id order, so "first occurrence" and "lowest id" are the same
    row -- if that ordering is ever relaxed, this resolver silently starts naming a
    different row than the write increments.
    """
    landing: Dict[_LandingKey, CombineStockLine] = {}
    for line in target_picture.lines:
        landing.setdefault((line.location, line.lot_number), line)
    return landing


# --------------------------------------------------------------------------- #
# The refusal probes -- ONE list, shared by the preview and the write
# --------------------------------------------------------------------------- #


def _combine_blockers(
    *,
    source: Part,
    target: Part,
    source_picture: PartStockPicture,
    quantity: float,
    flagged: Sequence[FlaggedPart],
    acknowledged_ids: Sequence[int],
    reservations: SourceReservations,
    planned: Sequence[PlannedLine],
    target_landing: Dict[_LandingKey, CombineStockLine],
) -> List[CombineDiagnostic]:
    """Every condition that refuses a combine, in the order the write checks them.

    Each entry's ``detail`` is verbatim the message the write raises, so the
    preview dialog can never disagree with the refusal the operator is about to
    get. The write raises the FIRST entry; the preview renders all of them.

    Two of the write's refusals are absent here because the preview request
    carries neither input: ``expected_part_number_mismatch`` (the compare-and-swap,
    checked by ``_assert_expected_part_numbers``) and ``source_still_has_stock``
    (the ``deactivate_source`` guard). Both are checked by the write before its
    first mutation, alongside these.

    ``planned`` and ``target_landing`` are what let the TARGET-side probes live
    here rather than in the write. They MUST be computed from this same
    ``source_picture`` (via ``_plan_lines``) -- probes modelling a different draw
    than the loop performs are how a refusal ends up disagreeing with the write it
    is supposed to rehearse.
    """
    out: List[CombineDiagnostic] = []

    if source.id == target.id:
        out.append(
            CombineDiagnostic(
                code="same_part",
                detail="Source and target are the same part. Pick two different part numbers.",
            )
        )
        # Everything below compares two sides; with one part there is nothing to
        # compare and the derived numbers would be nonsense.
        return out

    for part, label in ((source, "Source"), (target, "Target")):
        if part.is_deleted:
            out.append(
                CombineDiagnostic(
                    code="part_deleted",
                    detail=(
                        f"{label} part '{part.part_number}' is deleted - restore it or use a " "different part number"
                    ),
                )
            )

    # REUSES ``uom_label`` (app.models.part) rather than comparing raw column
    # values: one side is a native enum column and the other may arrive as a
    # string, and this is the one normalizer the whole platform compares units
    # with. Blank on either side is NOT a disagreement -- a part stating no unit
    # makes no claim to contradict -- exactly the rule ``uom_disagrees`` follows.
    source_uom = uom_label(source.unit_of_measure)
    target_uom = uom_label(target.unit_of_measure)
    if source_uom and target_uom and source_uom != target_uom:
        out.append(
            CombineDiagnostic(
                code="unit_of_measure_mismatch",
                detail=(
                    f"These parts are stocked in different units ('{source_uom}' vs '{target_uom}'). "
                    "Combining them would add quantities that do not mean the same thing."
                ),
            )
        )

    if source_picture.eligible_available <= LEDGER_QUANTITY_EPSILON:
        out.append(
            CombineDiagnostic(
                code="no_available_stock",
                detail=(
                    f"'{source.part_number}' has no available stock to combine. "
                    "Anything it still holds is allocated, pinned to an open job, on hold, quarantined "
                    "or on a deactivated row."
                ),
            )
        )
    elif quantity <= LEDGER_QUANTITY_EPSILON:
        # THE BUG THIS CLOSES: the schema bound was ``gt=0`` while the drain loop
        # broke on ``remaining <= LEDGER_QUANTITY_EPSILON``, so ``quantity: 1e-10``
        # returned 200 having moved nothing and still wrote an immutable combine
        # header, an operational event and an audit row -- a record asserting a
        # combine that did not happen. Ordered AFTER the availability probe so a
        # drained SKU still reports ``no_available_stock`` first, which is the
        # sentence that actually helps.
        out.append(
            CombineDiagnostic(
                code="quantity_below_minimum",
                detail=(
                    f"{quantity:g} is too small to move -- it would round to nothing and record a combine "
                    "that never happened. Enter the quantity you actually want to fold across."
                ),
            )
        )
    elif quantity - source_picture.eligible_available > LEDGER_QUANTITY_EPSILON:
        out.append(
            CombineDiagnostic(
                code="quantity_exceeds_available",
                detail=(
                    f"Only {source_picture.eligible_available:g} of '{source.part_number}' is available to "
                    f"combine; {quantity:g} was requested."
                ),
            )
        )

    # Part-level reservation (rule 3 of three -- the row-level caps are enforced by
    # the draw itself and can never be waived). What the source would be LEFT WITH
    # has to cover what open jobs still expect to pull from it.
    #
    # BOTH SIDES ARE ELIGIBLE-BASED, and that is the fix for a real defect. This was
    # ``source_picture.total_on_hand - quantity`` compared against every open tie:
    # ``total_on_hand`` sums held, quarantined and deactivated rows, which the
    # consumption engine can no more draw than this verb can move. A source with 92
    # free and 92 on hold "satisfied" a 92-unit tie (``184 - 92 >= 92``) and was then
    # left with nothing drawable -- the combine turned a satisfiable job into an
    # unfillable one, and ``_max_combinable`` inherited the same wrong basis and
    # ACTIVELY OFFERED the unsafe number. Refusing too much costs one visible
    # conversation; refusing too little strands material silently.
    #
    # Pinned demand is subtracted out because ``source_picture`` already withheld it
    # at the row -- counting it here as well would charge the same reservation twice.
    unpinned_reserved = reservations.unpinned_total(source_picture.total_pinned)
    remaining_after = source_picture.eligible_available - quantity
    if unpinned_reserved > LEDGER_QUANTITY_EPSILON and remaining_after < unpinned_reserved - LEDGER_QUANTITY_EPSILON:
        named = ", ".join(f"{r.work_order_number} ({r.outstanding_quantity:g})" for r in reservations.by_work_order)
        pinned_note = (
            f" ({source_picture.total_pinned:g} more is pinned to specific lots and is already held back.)"
            if source_picture.total_pinned > LEDGER_QUANTITY_EPSILON
            else ""
        )
        out.append(
            CombineDiagnostic(
                code="open_work_order_reservation",
                detail=(
                    f"Open work orders still expect {unpinned_reserved:g} of '{source.part_number}' to be "
                    f"available and this combine would leave {remaining_after:g} available. Untie or "
                    f"re-tie them first: {named}.{pinned_note}"
                ),
            )
        )

    out.extend(_target_landing_blockers(target, planned, target_landing))

    acknowledged = set(acknowledged_ids or ())
    unacknowledged = [flag for flag in flagged if flag.part_id not in acknowledged]
    if unacknowledged:
        named = ", ".join(f"'{flag.part_number}' (matched '{flag.matched_token}')" for flag in unacknowledged)
        out.append(
            CombineDiagnostic(
                code="flagged_part_not_acknowledged",
                detail=(
                    f"These parts look like test or housing parts and need an explicit confirmation "
                    f"before they can be combined: {named}."
                ),
            )
        )

    return out


def _target_landing_blockers(
    target: Part,
    planned: Sequence[PlannedLine],
    target_landing: Dict[_LandingKey, CombineStockLine],
) -> List[CombineDiagnostic]:
    """Refusals about the TARGET rows this combine's material would land on.

    ``target_row_not_available`` -- THE BUG THIS CLOSES, and it is the one that made
    this feature actively dangerous. ``_find_stock_row`` resolves a landing row by
    ``(company, part, location, lot)`` and nothing else: no ``is_active``, no
    ``status``. So 92 AVAILABLE sheets folded onto a target row somebody had put
    ``on_hold`` produced a **200**, an empty ``blockers`` list, a row at 102 still
    ``on_hold`` -- and 92 usable sheets that nothing in the app could draw any more.
    Counted, present, unusable, behind a success toast. Eligibility is decided by
    ``_unavailable_reason`` (i.e. ``is_consumable_item``), the SAME predicate the
    source side and the consumption engine use, so the two halves cannot drift.

    ``target_serial_mismatch`` -- a serial number names ONE physical unit. Merging a
    lot carrying ``SN-SOURCE`` onto a row carrying ``SN-TARGET`` produced a single
    row at quantity 2 claiming ``SN-TARGET``: two units under one serial, which
    invariant 5 exists to prevent. The comparison is SYMMETRIC (either side blank
    while the other is set is refused too) deliberately: merging a serialized lot
    into an anonymous row loses the serial, and merging an anonymous lot into a
    serialized row inflates it. Both misrepresent, so both are refused. A combine
    that genuinely needs to move serialized units to a new number lands them on a
    NEW row, which carries the serial across intact and is unaffected by this.

    Only rows a MOVING line would actually land on are considered -- a held target
    row at a location this combine never touches refuses nothing. That is why this
    takes the plan rather than the whole target picture.
    """
    out: List[CombineDiagnostic] = []
    reported: set = set()

    for line in planned:
        key = line.landing_key
        if key in reported:
            continue
        existing = target_landing.get(key)
        if existing is None:
            # No row there yet: ``_resolve_target_row`` will CREATE one, available
            # and carrying the source lot's traceability. Nothing to refuse.
            continue

        where = existing.location or "(no location)"
        lot = f" lot {existing.lot_number}" if existing.lot_number else ""

        if existing.unavailable_reason is not None:
            reported.add(key)
            out.append(
                CombineDiagnostic(
                    code="target_row_not_available",
                    detail=(
                        f"'{target.part_number}' already has stock at {where}{lot} that is not available "
                        f"({existing.unavailable_reason}, status '{existing.status or 'available'}'). "
                        f"{line.quantity:g} of usable material would be folded onto it and become unusable "
                        "too. Release that hold, or move this material to a different location or lot first."
                    ),
                )
            )
            continue

        source_serial = (line.serial_number or "").strip()
        target_serial = (existing.serial_number or "").strip()
        if (source_serial or target_serial) and source_serial != target_serial:
            reported.add(key)
            out.append(
                CombineDiagnostic(
                    code="target_serial_mismatch",
                    detail=(
                        f"The material at {where}{lot} carries serial "
                        f"'{source_serial or '(none)'}' and '{target.part_number}' already has a row there "
                        f"carrying serial '{target_serial or '(none)'}'. Merging them would put more than "
                        "one unit under a single serial number. Combine serialized stock onto a location "
                        "or lot the target does not already use."
                    ),
                )
            )

    return out


def _refuse(diagnostic: CombineDiagnostic) -> HTTPException:
    """The HTTP form of a blocker. One code, one sentence, one status."""
    return HTTPException(status_code=_BLOCKER_STATUS.get(diagnostic.code, 409), detail=diagnostic.detail)


def _assert_expected_part_numbers(
    source: Part,
    target: Part,
    expected_source_part_number: str,
    expected_target_part_number: str,
) -> None:
    """Compare-and-swap on the two part numbers, before anything is written.

    ``Part`` maps NO optimistic-lock version column (migration 004 versioned the
    table; the model never mapped it, and ``_part_to_response`` returns a
    hard-coded ``0``), so the number strings are the ONLY concurrency control
    available -- the same conclusion ``renumber_part`` reached. Without this, a
    combine fired against a part somebody renumbered while the dialog was open
    would fold stock into a SKU the operator never saw.

    Compared through ``normalize_alias_key`` (strip + upper), THE part-number
    comparison normalizer for this codebase, so a case or whitespace difference in
    what the client echoes back is not treated as somebody else's edit.
    """
    for part, expected, label in (
        (source, expected_source_part_number, "source"),
        (target, expected_target_part_number, "target"),
    ):
        if normalize_alias_key(expected) != normalize_alias_key(part.part_number):
            raise _refuse(
                CombineDiagnostic(
                    code="expected_part_number_mismatch",
                    detail=(
                        f"The {label} part's number changed while you were working (it is now "
                        f"'{part.part_number}'). Reload and try again."
                    ),
                )
            )


# --------------------------------------------------------------------------- #
# The preview -- PURE READ
# --------------------------------------------------------------------------- #


def build_combine_preview(
    db: Session,
    company_id: int,
    source_part_id: int,
    target_part_id: int,
    quantity: Optional[float] = None,
) -> CombinePreview:
    """What a combine would do. PURE READ -- writes nothing, structurally.

    Takes no ``AuditService`` and no actor id, so it CANNOT write an audit row, a
    ledger row or an operational event even by accident. Keeping that structural
    rather than conventional is the rule ``build_renumber_impact`` and the
    backflush-readiness companions established: a poll is not an actor and records
    no reason.

    A stale ``eligible: true`` is NOT authorization. Every input here is mutable by
    other people -- stock moves, ties open, parts get renumbered -- so
    ``combine_inventory`` re-runs every one of these probes against the state at
    write time.

    ``quantity=None`` means "model the whole thing": the probes run against the
    source's full eligible available quantity, which is also returned as
    ``default_quantity`` for the dialog to pre-fill.
    """
    source = _resolve_part(db, company_id, source_part_id)
    target = _resolve_part(db, company_id, target_part_id)

    reservations = source_reservations(db, company_id, source.id)
    reserved_quantity = reservations.total

    # Pins are a SOURCE-side cap only: a lot-directed tie on the target part
    # restricts nothing about adding material to it.
    source_picture = _picture_from_rows(source, _stock_rows(db, company_id, source.id), reservations)
    target_picture = _picture_from_rows(target, _stock_rows(db, company_id, target.id))

    default_quantity = source_picture.eligible_available
    requested = default_quantity if quantity is None else float(quantity)

    flagged = _flagged_parts(source, target)
    planned = _plan_lines(source_picture, requested)

    blockers = _combine_blockers(
        source=source,
        target=target,
        source_picture=source_picture,
        quantity=requested,
        flagged=flagged,
        # The preview request carries no acknowledgements, so it answers "what
        # happens if you submit this as it stands" -- which is a refusal while a
        # flagged part is unconfirmed. That is deliberate: the dialog needs the
        # blocker to know it must render the confirmation checkbox.
        acknowledged_ids=(),
        reservations=reservations,
        planned=planned,
        target_landing=_landing_rows_from_picture(target_picture),
    )

    advisories: List[CombineDiagnostic] = []
    source_uom = uom_label(source.unit_of_measure)
    target_uom = uom_label(target.unit_of_measure)
    if not source_uom or not target_uom:
        advisories.append(
            CombineDiagnostic(
                code="unit_of_measure_unstated",
                detail=(
                    "One of these parts does not state a stocking unit, so the units cannot be checked "
                    "against each other. Confirm by hand that the quantities mean the same thing."
                ),
            )
        )
    advisories.extend(_excluded_stock_advisories(source_picture))
    advisories.extend(_pinned_stock_advisories(source_picture))
    advisories.extend(_sheet_advisories(source, target))

    return CombinePreview(
        source=source_picture,
        target=target_picture,
        # Blank on either side counts as a match (no claim to contradict) and is
        # reported as an advisory instead -- same rule ``uom_disagrees`` follows.
        unit_of_measure_match=not (source_uom and target_uom and source_uom != target_uom),
        default_quantity=default_quantity,
        max_combinable_quantity=_max_combinable(source_picture, reservations),
        reserved_quantity=reserved_quantity,
        eligible=not blockers,
        blockers=blockers,
        advisories=advisories,
        flagged_parts=flagged,
        open_source_reservations=reservations.by_work_order,
        cost=_cost_summary(source_picture, target_picture),
    )


def _max_combinable(source_picture: PartStockPicture, reservations: SourceReservations) -> float:
    """The largest quantity that would NOT trip the open-tie reservation check.

    Offered so the dialog can propose the safe number instead of presenting a dead
    end -- which means it has to be computed on the SAME basis as the check itself,
    or the dialog offers a number the server then refuses. It used to be
    ``total_on_hand - reserved``, while the check compared against every open tie:
    on a source with held stock that offered a quantity which passed the check and
    left the open jobs with nothing drawable. Both are ``eligible_available``-based
    now; if one moves, the other moves with it.

    Bounded by the eligible available quantity as well, because that cap is the hard
    one, and the subtraction skips pinned demand for the same reason
    ``_combine_blockers`` does -- ``eligible_available`` is already net of it.
    """
    reservation_cap = source_picture.eligible_available - reservations.unpinned_total(source_picture.total_pinned)
    return max(0.0, min(source_picture.eligible_available, reservation_cap))


# --------------------------------------------------------------------------- #
# The write
# --------------------------------------------------------------------------- #


def _lock_source_stock_rows(db: Session, company_id: int, part_id: int) -> List[InventoryItem]:
    """Every source stock row, locked ``FOR UPDATE`` in ASCENDING ID ORDER.

    The id read and the locking read are separate on purpose: selecting ids first
    means the rows are not yet in the session's identity map, so the locked SELECT
    genuinely loads their current values rather than handing back attributes that
    were read before the lock existed.

    Ascending id is one order for two jobs -- the drain order (oldest lot first,
    approximately FIFO) and the lock-acquisition order -- so a combine can never
    deadlock against itself, and reuses ``_load_inventory_items``, which is where
    that ordering rule already lives.

    LOCK-ORDERING CAVEAT, carried verbatim from the ``/transfer`` handler because
    this verb has the same shape: source rows are locked by id first, then each
    target row by (part, location, lot), and only then the global audit hash-chain
    lock (``_lock_target_landing_rows`` runs before the first audit write precisely
    so that leg comes last -- see the module docstring). The source/target pair is
    NOT id-ordered relative to each other, so a combine racing an opposing
    transfer/consumption over the same lots can deadlock. Postgres detects it and
    aborts one victim with an error (never a hang), and nothing partial commits --
    the whole verb runs inside one ``atomic_transaction``. A two-phase ascending-id
    acquisition would close the window if this ever shows up in practice.

    ``with_for_update`` is a no-op on the SQLite test backend, as everywhere else
    in this codebase.
    """
    from app.api.endpoints.inventory import _load_inventory_items  # deferred: see module docstring

    ids = [
        row[0]
        for row in db.query(InventoryItem.id)
        .filter(InventoryItem.company_id == company_id, InventoryItem.part_id == part_id)
        .order_by(InventoryItem.id.asc())
        .all()
    ]
    locked = _load_inventory_items(db, company_id, ids, for_update=True)
    return [locked[item_id] for item_id in sorted(locked)]


def _lock_target_landing_rows(
    db: Session,
    company_id: int,
    *,
    target_part_id: int,
    planned: Sequence[PlannedLine],
) -> Dict[_LandingKey, InventoryItem]:
    """Resolve and LOCK every existing target row the plan would land on.

    Run BEFORE the first write, for three reasons that are all load-bearing:

    1. The ``target_row_not_available`` / ``target_serial_mismatch`` probes have to
       read the row as LOCKED, not as some other transaction last left it -- a
       refusal computed off a stale copy is not a refusal.
    2. Every row lock this verb needs is then held before the first audit write, so
       the global audit hash-chain lock is the LAST lock taken. Taking a new row
       lock while holding it is what deadlocked this verb against ``/receive``.
    3. It replaces the unlocked ``_stock_rows(target)`` read that used to run here.
       **THE BUG THAT READ CAUSED:** it seeded the Session identity map with every
       target row, and SQLAlchemy's default behaviour on a later ``SELECT ... FOR
       UPDATE`` of the same row is to return the ALREADY-PRESENT instance and
       DISCARD the freshly-read column values. Measured: the DB row held 150, the
       locked SELECT handed back the cached 100, and the write landed 110 instead of
       160 -- 50 received units silently gone, with a ledger that still nets to zero
       over an understated on-hand. ``_stock_row_query`` now calls
       ``.populate_existing()`` on the ``for_update`` path as the structural guard
       (mirroring ``material_consumption_service``'s locked operation read), and this
       function is the reason nothing re-seeds the map beforehand.

    Resolution goes through ``_find_stock_row``, which is NULL-safe on a lot-less
    row: the naive ``lot_number == None`` compiles to ``lot_number = NULL`` and
    never matches, which is how lot-less receives used to mint a duplicate fragment
    row every time instead of incrementing the one already there.

    ``with_for_update`` is a no-op on the SQLite test backend, as everywhere else in
    this codebase.
    """
    from app.api.endpoints.inventory import _find_stock_row  # deferred: see module docstring

    landing: Dict[_LandingKey, InventoryItem] = {}
    for key in _landing_keys(planned):
        location, lot_number = key
        row = _find_stock_row(
            db,
            company_id=company_id,
            part_id=target_part_id,
            # ``InventoryItem.location`` is NOT NULL, so this is a ``str`` in every
            # real row; the annotation on the dataclass is Optional only because the
            # ORM attribute is untyped. Cast rather than widen ``_find_stock_row``,
            # which is shared with /receive and /transfer.
            location_code=cast(str, location),
            lot_number=lot_number,
            for_update=True,
        )
        if row is not None:
            landing[key] = row
    return landing


def _apply_to_target_row(
    db: Session,
    company_id: int,
    *,
    target_part: Part,
    source_row: InventoryItem,
    quantity: float,
    existing: Optional[InventoryItem],
) -> Tuple[InventoryItem, bool]:
    """Land one line's material on the target. Returns (row, created).

    ``existing`` is the row ``_lock_target_landing_rows`` already resolved and
    LOCKED for this line's ``(location, lot)``, or ``None`` when there is none. It is
    passed in rather than re-queried so the row the probes vetted is provably the row
    that gets incremented.

    WHEN A ROW MUST BE CREATED it carries the source row's traceability wholesale
    -- lot, serial, cert, heat lot, supplier, PO, received and expiration dates --
    and its ``unit_cost``. The MTR and heat lot follow the physical material
    because that is what AS9100D 8.5.2 lot traceability IS; a merge that dropped
    them would launder the material's provenance.

    WHEN THE ROW ALREADY EXISTS only ``quantity_on_hand`` (and the derived
    ``quantity_available``) change. Every other column, ``unit_cost`` INCLUDED,
    is left untouched: a combine never reblends a cost basis, and the preview
    surfaced the delta so a human could decide before approving.
    """
    if existing is not None:
        _assert_target_row_still_landable(target_part, source_row, existing)
        existing.quantity_on_hand = float(existing.quantity_on_hand or 0.0) + quantity
        existing.quantity_available = existing.quantity_on_hand - float(existing.quantity_allocated or 0.0)
        return existing, False

    created = InventoryItem(
        part_id=target_part.id,
        location=source_row.location,
        warehouse=source_row.warehouse,
        quantity_on_hand=quantity,
        quantity_allocated=0.0,
        quantity_available=quantity,
        lot_number=source_row.lot_number,
        serial_number=source_row.serial_number,
        unit_cost=float(source_row.unit_cost or 0.0),
        cert_number=source_row.cert_number,
        heat_lot=source_row.heat_lot,
        supplier_id=source_row.supplier_id,
        po_number=source_row.po_number,
        received_date=source_row.received_date,
        expiration_date=source_row.expiration_date,
        status=AVAILABLE_ITEM_STATUS,
        is_active=True,
    )
    created.company_id = company_id
    db.add(created)
    # Flushed immediately so the row has an id for the ledger row's
    # ``inventory_item_id``. Two source rows CAN share a (location, lot); the caller
    # records the created row back into its landing map so the second line
    # increments it rather than minting a duplicate fragment beside it.
    db.flush()
    return created, True


def _assert_target_row_still_landable(
    target_part: Part,
    source_row: InventoryItem,
    existing: InventoryItem,
) -> None:
    """Backstop for the two TARGET-row refusals, evaluated on the row as locked.

    ``_combine_blockers`` already refused both conditions before anything was
    written, and it did so against these same locked rows -- so in normal operation
    this never fires. It exists because the alternative to a cheap assertion here is
    trusting that the plan, the probe and the loop can never drift: the probe reads a
    ``CombineStockLine`` copy while the loop holds the ORM row, and "those two always
    agree" is the kind of claim that stops being true a year later. Raising is safe
    at any point in the loop -- the endpoint's ``atomic_transaction`` rolls the whole
    verb back, so a refusal here still leaves every row byte-identical.
    """
    reason = _unavailable_reason(existing)
    if reason is not None:
        raise _refuse(
            CombineDiagnostic(
                code="target_row_not_available",
                detail=(
                    f"'{target_part.part_number}' already has stock at "
                    f"{existing.location or '(no location)'} that is not available ({reason}). Folding "
                    "usable material onto it would make that material unusable too."
                ),
            )
        )

    source_serial = (source_row.serial_number or "").strip()
    target_serial = (existing.serial_number or "").strip()
    if (source_serial or target_serial) and source_serial != target_serial:
        raise _refuse(
            CombineDiagnostic(
                code="target_serial_mismatch",
                detail=(
                    f"Serial '{source_serial or '(none)'}' cannot be merged onto a "
                    f"'{target_part.part_number}' row carrying serial '{target_serial or '(none)'}' -- "
                    "one serial number cannot name two units."
                ),
            )
        )


def combine_inventory(
    db: Session,
    company_id: int,
    *,
    source_part_id: int,
    target_part_id: int,
    quantity: float,
    reason: str,
    expected_source_part_number: str,
    expected_target_part_number: str,
    acknowledge_flagged_part_ids: Sequence[int],
    deactivate_source: bool,
    actor_user_id: int,
    audit: AuditService,
) -> CombineResult:
    """Move stock from one SKU onto another. The caller wraps the transaction.

    Flushes, never commits -- the endpoint wraps this in ``atomic_transaction`` so
    the header, every stock write, all 2N ledger rows and every audit row land as
    one unit or not at all. That is what makes acceptance item 5 true: a failure
    part-way through leaves NO partial stock change and NO ledger row.

    **Every refusal is raised before the first mutation.** The order below is the
    order of ``_combine_blockers`` plus the two probes the preview cannot run (the
    compare-and-swap, and the ``deactivate_source`` guard). Nothing is written
    until all of them have passed.

    **The preview is not authorization.** Every probe re-runs here, server-side,
    against state read under the row locks this function takes.

    **Lock order is source rows (ascending id) -> target landing rows -> the audit
    hash-chain lock**, and the last leg is why the per-line audit calls are buffered
    and emitted after the drain loop instead of inside it. See the module docstring.
    """
    # --- Probes. Nothing below this block writes; nothing above it does either. ---
    if source_part_id == target_part_id:
        # Checked before the part loads so the message is about the request, not
        # about a part the caller never asked to compare with itself.
        raise _refuse(
            CombineDiagnostic(
                code="same_part",
                detail="Source and target are the same part. Pick two different part numbers.",
            )
        )

    source = _resolve_part(db, company_id, source_part_id)
    target = _resolve_part(db, company_id, target_part_id)

    _assert_expected_part_numbers(source, target, expected_source_part_number, expected_target_part_number)

    # Locks are taken here, before the remaining probes, so those probes read the
    # quantities the draw below will actually see. A lock is not a mutation: a
    # refusal past this point still leaves every row byte-identical, because the
    # endpoint's ``atomic_transaction`` rolls back and releases.
    source_rows = _lock_source_stock_rows(db, company_id, source.id)
    reservations = source_reservations(db, company_id, source.id)
    source_picture = _picture_from_rows(source, source_rows, reservations)
    flagged = _flagged_parts(source, target)

    # The plan comes first because it decides WHICH target rows are in play, and
    # those are the only ones worth locking or refusing over.
    planned = _plan_lines(source_picture, quantity)
    target_rows_by_key = _lock_target_landing_rows(
        db,
        company_id,
        target_part_id=target.id,
        planned=planned,
    )

    blockers = _combine_blockers(
        source=source,
        target=target,
        source_picture=source_picture,
        quantity=quantity,
        flagged=flagged,
        acknowledged_ids=acknowledge_flagged_part_ids,
        reservations=reservations,
        planned=planned,
        target_landing={key: _stock_line_snapshot(row) for key, row in target_rows_by_key.items()},
    )
    if blockers:
        raise _refuse(blockers[0])

    source_quantity_before = source_picture.total_on_hand
    # An AGGREGATE, deliberately, not ``_picture_from_rows(target, _stock_rows(...))``.
    # That read was only ever used for this one number, and it seeded the Session
    # identity map with every target row -- which is what made the later locked
    # SELECT hand back stale cached quantities and silently lose concurrently
    # received stock. See ``_lock_target_landing_rows`` for the measured failure.
    # ``func.sum`` returns no ORM instances, so it cannot re-open that hole.
    target_quantity_before = part_total_on_hand(db, company_id, target.id)

    if deactivate_source and abs(source_quantity_before - quantity) > LEDGER_QUANTITY_EPSILON:
        # "Lands at exactly 0" is measured across ALL the source's stock rows,
        # including the ineligible ones this combine cannot move. Deactivating a
        # part that still has material on a held or quarantined row would hide that
        # material from every list in the app while it is still physically there.
        raise _refuse(
            CombineDiagnostic(
                code="source_still_has_stock",
                detail=(
                    f"'{source.part_number}' would still hold "
                    f"{source_quantity_before - quantity:g} after this combine, so it cannot be deactivated. "
                    "Combine the rest first, or leave it active."
                ),
            )
        )

    # --- Everything from here writes. All refusals are behind us. ---
    combine = InventoryCombine(
        # Transient: the real number is minted from the primary key this INSERT
        # produces (``format_combine_number``), so it cannot be known until after
        # the flush. Unique per request because the per-company unique index is
        # checked per statement — see ``_PENDING_NUMBER_PREFIX``.
        combine_number=f"{_PENDING_NUMBER_PREFIX}{uuid.uuid4().hex}",
        source_part_id=source.id,
        target_part_id=target.id,
        quantity=quantity,
        reason=reason,
        lines_moved=0,
        source_quantity_before=source_quantity_before,
        target_quantity_before=target_quantity_before,
        source_deactivated=False,
        created_by=actor_user_id,
        created_at=datetime.utcnow(),
    )
    combine.company_id = company_id
    db.add(combine)
    db.flush()
    combine.combine_number = format_combine_number(combine.id)
    db.flush()

    lines: List[CombineLine] = []
    transaction_ids: List[int] = []
    # BUFFERED, not written inside the loop. ``AuditService._acquire_chain_lock``
    # takes a global ``pg_advisory_xact_lock`` held to transaction end, so writing
    # line 1's audit rows and THEN asking for line 2's target row lock deadlocks
    # against any ``/receive`` that already holds that row and is about to write its
    # own audit row -- surfacing as a 500 on a stock verb, because there is no DBAPI
    # deadlock handler. Every row lock this verb needs is taken above; the chain lock
    # is taken last, once, after the loop. Do not move these calls back inside it.
    pending_audit: List[Dict[str, object]] = []
    rows_by_id = {row.id: row for row in source_rows}

    for plan in planned:
        source_row = rows_by_id[plan.inventory_item_id]
        on_hand = float(source_row.quantity_on_hand or 0.0)
        allocated = float(source_row.quantity_allocated or 0.0)

        # RULE 1 and RULE 2 of the three reservation rules, and the hard ones: never
        # move into ``quantity_allocated``, and never move material a lot-directed
        # open tie is pinned to. Both are already netted into the plan's ``quantity``
        # by ``_plan_lines``, which is also what the refusal probes measured -- one
        # computation, so the probe and the draw cannot disagree.
        take = plan.quantity
        unit_cost = float(source_row.unit_cost or 0.0)
        lot_label = f" lot {source_row.lot_number}" if source_row.lot_number else ""

        source_before = on_hand
        source_row.quantity_on_hand = on_hand - take
        source_row.quantity_available = source_row.quantity_on_hand - allocated
        source_after = float(source_row.quantity_on_hand or 0.0)

        target_row, target_created = _apply_to_target_row(
            db,
            company_id,
            target_part=target,
            source_row=source_row,
            quantity=take,
            existing=target_rows_by_key.get(plan.landing_key),
        )
        # Two source rows can share a (location, lot). Recording the row back means
        # the second line increments the one the first line created instead of
        # minting a duplicate fragment beside it.
        target_rows_by_key[plan.landing_key] = target_row
        target_after = float(target_row.quantity_on_hand or 0.0)
        target_before = target_after - take

        # THE LEDGER SHAPE: two linked ADJUST rows, net zero, grouped by this
        # combine. Not a new TransactionType -- adding a value to ``transactiontype``
        # means an ALTER TYPE on live Postgres plus teaching every consumer
        # (analytics, exports, traceability, job costing, frontend labels) a value
        # they cannot currently see. ADJUST is already this repo's compensating /
        # reconciliation shape (invariant 3), and it is the one type whose SIGN
        # carries direction.
        #
        # ``from_location == to_location`` on BOTH rows because the material does
        # not physically move; only its SKU changes.
        out_txn = InventoryTransaction(
            company_id=company_id,
            inventory_item_id=source_row.id,
            part_id=source.id,
            transaction_type=TransactionType.ADJUST,
            quantity=-take,
            from_location=source_row.location,
            to_location=source_row.location,
            lot_number=source_row.lot_number,
            serial_number=source_row.serial_number,
            reference_type=COMBINE_REFERENCE_TYPE,
            reference_id=combine.id,
            reference_number=combine.combine_number,
            reason_code=COMBINE_OUT_REASON_CODE,
            unit_cost=unit_cost,
            total_cost=abs(take) * unit_cost,
            notes=(
                f"Combine {combine.combine_number}: {take:g} of {source.part_number} "
                f"at {source_row.location or '(no location)'}{lot_label} moved onto {target.part_number}"
            ),
            created_by=actor_user_id,
        )
        in_txn = InventoryTransaction(
            company_id=company_id,
            inventory_item_id=target_row.id,
            part_id=target.id,
            transaction_type=TransactionType.ADJUST,
            quantity=take,
            from_location=source_row.location,
            to_location=source_row.location,
            lot_number=source_row.lot_number,
            serial_number=source_row.serial_number,
            reference_type=COMBINE_REFERENCE_TYPE,
            reference_id=combine.id,
            reference_number=combine.combine_number,
            reason_code=COMBINE_IN_REASON_CODE,
            # The SOURCE row's unit cost on both halves: the cost travels with the
            # material, and the pair has to price identically or the net-zero
            # identity holds on quantity but not on value.
            unit_cost=unit_cost,
            total_cost=abs(take) * unit_cost,
            notes=(
                f"Combine {combine.combine_number}: {take:g} received onto {target.part_number} "
                f"at {source_row.location or '(no location)'}{lot_label} from {source.part_number}"
            ),
            created_by=actor_user_id,
        )
        db.add(out_txn)
        db.add(in_txn)
        db.flush()

        # Tamper-evident audit trail (invariant 2), the dual-row convention every
        # stock mutator in this codebase follows: the movement, plus the stock-level
        # change it produced. QUEUED here and emitted after the loop -- see
        # ``pending_audit`` above for why that ordering is load-bearing.
        #
        # The before/after quantities are captured NOW, not re-read at emit time:
        # two source rows can land on the SAME target row, and reading
        # ``target_row.quantity_on_hand`` after the loop would report the cumulative
        # figure on both lines' audit rows.
        pending_audit.append(
            {
                "out_txn": out_txn,
                "in_txn": in_txn,
                "source_row": source_row,
                "source_before": source_before,
                "source_after": source_after,
                "target_row": target_row,
                "target_before": target_before,
                "target_after": target_after,
                "target_created": target_created,
                "combine_number": combine.combine_number,
                "source_part_number": source.part_number,
                "target_part_number": target.part_number,
                "quantity": take,
            }
        )

        lines.append(
            CombineLine(
                location=source_row.location,
                lot_number=source_row.lot_number,
                quantity=take,
                unit_cost=unit_cost,
                source_inventory_item_id=source_row.id,
                target_inventory_item_id=target_row.id,
                target_row_created=target_created,
            )
        )
        transaction_ids.extend([out_txn.id, in_txn.id])

    for entry in pending_audit:
        # Called through the module global on purpose: the atomicity test
        # monkeypatches ``inventory_combine_service._audit_combine_line`` to prove a
        # mid-flight failure unwinds every earlier write.
        _audit_combine_line(audit, **entry)  # type: ignore[arg-type]

    # SUMMED FROM WHAT WAS ACTUALLY POSTED, never re-derived as ``quantity -
    # remaining``. In the multi-line case that subtraction is the same sum plus the
    # accumulated float error of the running remainder, so the header's ``quantity``
    # could differ in the last bits from the ledger rows it claims to summarize.
    # This value is the sum of the same floats the ledger holds, by construction.
    moved = sum((line.quantity for line in lines), 0.0)
    if quantity - moved > LEDGER_QUANTITY_EPSILON:
        # Unreachable while the probes above hold: ``quantity_exceeds_available``
        # already refused anything larger than the eligible available total, and the
        # rows were locked before that probe read them. Logged rather than silently
        # written, because a short draw means the two disagreed and the recorded
        # ``quantity`` would otherwise overstate what moved.
        logger.warning(
            "combine %s drew only %s of a requested %s for part_id=%s company_id=%s",
            combine.combine_number,
            moved,
            quantity,
            source.id,
            company_id,
        )

    source_quantity_after = part_total_on_hand(db, company_id, source.id)
    target_quantity_after = part_total_on_hand(db, company_id, target.id)

    combine.quantity = moved
    combine.lines_moved = len(lines)
    combine.source_quantity_after = source_quantity_after
    combine.target_quantity_after = target_quantity_after

    source_deactivated = False
    if deactivate_source:
        # RE-CHECKED against the POST-move total, not just the pre-move probe above.
        # ``FOR UPDATE`` locks ROWS, not the predicate: a ``/receive`` onto this part
        # that commits after ``_lock_source_stock_rows`` ran INSERTS a row the
        # ``source_still_has_stock`` probe never saw. Deactivating on the strength of
        # that probe alone would retire a part with material physically on the shelf,
        # and would write a header row whose ``source_quantity_after`` is > 0 while
        # its ``source_deactivated`` is True -- a record that contradicts itself.
        #
        # It DECLINES rather than raising: the fold itself is correct and already
        # posted, and throwing away a correct net-zero move because somebody received
        # stock a second earlier helps nobody. The response and the header both carry
        # ``source_deactivated: false``, so the caller can see the request was only
        # partly honoured (a client should surface that as a WARNING, not a success)
        # and can retire the part explicitly via ``POST /parts/{id}/deactivate``.
        if source_quantity_after > LEDGER_QUANTITY_EPSILON:
            logger.warning(
                "combine %s did not deactivate part_id=%s (company_id=%s): %s on hand after the move, "
                "so stock arrived between the lock and the write",
                combine.combine_number,
                source.id,
                company_id,
                source_quantity_after,
            )
        else:
            source_deactivated = _deactivate_source_part(
                audit,
                part=source,
                combine_number=combine.combine_number,
                reason=reason,
            )
    combine.source_deactivated = source_deactivated
    db.flush()

    _audit_combine_header(
        audit,
        combine=combine,
        source=source,
        target=target,
        lines=lines,
        reason=reason,
        flagged=flagged,
        acknowledged_ids=acknowledge_flagged_part_ids,
        deactivate_source=deactivate_source,
        source_deactivated=source_deactivated,
    )

    OperationalEventService(db).emit_best_effort(
        company_id=company_id,
        event_type="inventory_combined",
        source_module="inventory",
        entity_type="inventory_combine",
        entity_id=combine.id,
        user_id=actor_user_id,
        severity="medium",
        event_payload={
            "combine_number": combine.combine_number,
            "source_part_id": source.id,
            "source_part_number": source.part_number,
            "target_part_id": target.id,
            "target_part_number": target.part_number,
            "quantity": moved,
            "lines_moved": len(lines),
            "source_quantity_after": source_quantity_after,
            "target_quantity_after": target_quantity_after,
            "source_deactivated": source_deactivated,
        },
    )

    return CombineResult(
        combine_id=combine.id,
        combine_number=combine.combine_number,
        source_part_id=source.id,
        source_part_number=source.part_number,
        target_part_id=target.id,
        target_part_number=target.part_number,
        quantity_moved=moved,
        lines_moved=len(lines),
        source_quantity_before=source_quantity_before,
        source_quantity_after=source_quantity_after,
        target_quantity_before=target_quantity_before,
        target_quantity_after=target_quantity_after,
        source_deactivated=source_deactivated,
        lines=lines,
        transaction_ids=transaction_ids,
    )


def _audit_combine_line(
    audit: AuditService,
    *,
    out_txn: InventoryTransaction,
    in_txn: InventoryTransaction,
    source_row: InventoryItem,
    source_before: float,
    source_after: float,
    target_row: InventoryItem,
    target_before: float,
    target_after: float,
    target_created: bool,
    combine_number: str,
    source_part_number: str,
    target_part_number: str,
    quantity: float,
) -> None:
    """The audit rows for one moved lot line.

    The OUT half fits ``_audit_stock_movement``'s two-row shape exactly (one
    ledger CREATE + one stock UPDATE), so it reuses it.

    The IN half is written explicitly, for the same reason ``/receive`` writes its
    own block: when the target row was CREATED by this line there is no "old"
    quantity to diff, so only the ledger CREATE is written -- an UPDATE row
    claiming ``0 -> n`` on a row that did not exist a moment ago would be a
    fabricated fact on a tamper-evident chain. When the target row already existed,
    ``_audit_stock_movement`` handles it and the pair is symmetric.

    ``source_after`` / ``target_after`` are PASSED IN rather than read off the rows.
    These calls are deliberately deferred until after the drain loop (see the
    module docstring's lock-ordering section), and by then the rows carry their
    FINAL quantities -- two source lines landing on one target row would otherwise
    both record the cumulative figure, so line 1's audit row would claim a change
    that never happened at that moment.
    """
    from app.api.endpoints.inventory import _audit_stock_movement  # deferred: see module docstring

    _audit_stock_movement(
        audit,
        out_txn,
        source_row,
        source_before,
        source_after,
        movement_description=(
            f"Combine {combine_number}: moved {quantity:g} of {source_part_number} out of "
            f"{source_row.location or '(no location)'} onto {target_part_number}"
        ),
        stock_label="Combine out",
    )

    if target_created:
        audit.log_create(
            "inventory",
            in_txn.id,
            str(in_txn.id),
            new_values=in_txn,
            description=(
                f"Combine {combine_number}: received {quantity:g} onto {target_part_number} at "
                f"{target_row.location or '(no location)'} (new stock row {target_row.id})"
            ),
        )
        return

    _audit_stock_movement(
        audit,
        in_txn,
        target_row,
        target_before,
        target_after,
        movement_description=(
            f"Combine {combine_number}: received {quantity:g} onto {target_part_number} at "
            f"{target_row.location or '(no location)'}"
        ),
        stock_label="Combine in",
    )


def _audit_combine_header(
    audit: AuditService,
    *,
    combine: InventoryCombine,
    source: Part,
    target: Part,
    lines: Sequence[CombineLine],
    reason: str,
    flagged: Sequence[FlaggedPart],
    acknowledged_ids: Sequence[int],
    deactivate_source: bool,
    source_deactivated: bool,
) -> None:
    """The one audit row that carries the whole decision.

    ``extra_data`` records what the generic column diff cannot: the per-line
    breakdown (which lot moved from where, at what cost), the reason, and which
    flagged parts were acknowledged by whom. None of that is reconstructable
    afterwards once later movements have posted against the same lots -- the
    ledger says what moved, but not what the operator was told and confirmed at the
    moment they approved it.
    """
    audit.log_create(
        "inventory_combine",
        combine.id,
        combine.combine_number,
        new_values=combine,
        description=(
            f"Combined {combine.quantity:g} of {source.part_number} into {target.part_number} "
            f"across {len(lines)} lot line(s) (reason: {reason})"
        ),
        extra_data={
            "combine_number": combine.combine_number,
            "source_part_id": source.id,
            "source_part_number": source.part_number,
            "target_part_id": target.id,
            "target_part_number": target.part_number,
            "quantity": combine.quantity,
            "reason": reason,
            "lines": [
                {
                    "location": line.location,
                    "lot_number": line.lot_number,
                    "quantity": line.quantity,
                    "unit_cost": line.unit_cost,
                }
                for line in lines
            ],
            "flagged_parts": [
                {"part_id": flag.part_id, "part_number": flag.part_number, "matched_token": flag.matched_token}
                for flag in flagged
            ],
            "acknowledged_flagged_part_ids": list(acknowledged_ids or ()),
            "deactivate_source": deactivate_source,
            "source_deactivated": source_deactivated,
        },
    )


def _deactivate_source_part(
    audit: AuditService,
    *,
    part: Part,
    combine_number: str,
    reason: str,
) -> bool:
    """Take the drained source part out of use. Returns whether anything changed.

    ``is_active`` and ``status`` are written TOGETHER so the two can never
    disagree, and ``is_deleted`` is NEVER touched. That last point is the important
    one: ``is_active`` doubles as the soft-delete MASK (``delete_part`` sets
    ``is_deleted`` AND ``is_active=False`` AND ``status='obsolete'``), so a verb
    that wrote ``is_deleted`` here would be conflating "no longer ordered" with
    "deleted" -- and the part must stay in the catalog at qty 0 so every historical
    document naming it keeps resolving.
    """
    if not part.is_active and (part.status or "") == OBSOLETE_PART_STATUS:
        return False

    old_values = {"is_active": bool(part.is_active), "status": part.status}
    part.is_active = False
    part.status = OBSOLETE_PART_STATUS
    audit.log_update(
        "part",
        part.id,
        part.part_number,
        old_values=old_values,
        new_values={"is_active": False, "status": OBSOLETE_PART_STATUS},
        description=(
            f"Deactivated part {part.part_number} as part of combine {combine_number} "
            f"(reason: {reason}). Not deleted — it stays in the catalog at qty 0."
        ),
        extra_data={"combine_number": combine_number, "reason": reason},
    )
    return True


__all__ = [
    "ACTIVE_PART_STATUS",
    "FLAGGED_PART_TOKENS",
    "OBSOLETE_PART_STATUS",
    "CombineCost",
    "CombineDiagnostic",
    "CombineLine",
    "CombinePreview",
    "CombineResult",
    "CombineStockLine",
    "FlaggedPart",
    "OpenReservation",
    "PartStockPicture",
    "PlannedLine",
    "SourceReservations",
    "build_combine_preview",
    "combine_inventory",
    "open_source_reservations",
    "part_total_on_hand",
    "source_reservations",
]
