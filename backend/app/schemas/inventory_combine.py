"""Request/response contracts for folding two SKUs into one.

Two verbs, and the difference between them is the point of this file.

``POST /inventory/combine/preview`` answers "what would this do, and would it be
refused?" It is a PURE READ (the service function it calls takes no
``AuditService`` and no actor id, so it could not write even by accident), and
its ``eligible`` flag is a SNAPSHOT, never authorisation -- every input it reads
is mutable by other people, so the write re-runs every probe server-side.

``POST /inventory/combine`` performs it. Its request carries three things the
preview has no way to know: the compare-and-swap part numbers, the explicit
acknowledgement of any flagged part, and the reason.
"""

from typing import List, Optional

from pydantic import BaseModel, Field, field_validator

from app.db.ledger_filter import LEDGER_QUANTITY_EPSILON
from app.schemas.base import UTCModel

# The smallest quantity a combine will accept, and it is NOT an arbitrary bound: it
# is the SAME epsilon the ledger and the drain loop compare quantities with.
#
# THE BUG THIS CLOSES: the field used to be ``Field(gt=0)`` while the drain loop broke
# on ``remaining <= LEDGER_QUANTITY_EPSILON``. A request for ``1e-10`` therefore passed
# validation, moved NOTHING (the loop's first ``break`` fired immediately), and still
# wrote an ``inventory_combines`` header, an operational event and an audit row -- an
# immutable, un-deletable record asserting a combine that did not happen, on a table
# whose own docstring says "a combine happened or it did not". With
# ``deactivate_source: true`` it would additionally have retired the source part off a
# zero-move request. Do not relax this back to ``gt=0``; the schema bound and the
# ledger epsilon have to agree or that gap reopens.
#
# The service carries the same refusal as ``quantity_below_minimum`` for callers that
# reach it without going through this schema.
MINIMUM_COMBINE_QUANTITY = LEDGER_QUANTITY_EPSILON


class CombineDiagnosticSchema(UTCModel):
    """One reason a combine is refused (``blockers``), or one thing worth
    disclosing that does not refuse it (``advisories``).

    ``code`` is the stable machine token (``no_available_stock``,
    ``unit_of_measure_mismatch``, ...) and is the SAME token the write's 409
    detail is built from, so the dialog can never explain a refusal in words that
    disagree with the refusal the operator is about to get.
    """

    code: str
    detail: str


class FlaggedPartSchema(UTCModel):
    """A part whose number or name contains a word the owner asked to be careful of.

    NOT a ban. "Housing" is a legitimate manufacturing word (the Miratech housing
    is a real production part), so refusing outright would be wrong. The request
    must instead name this ``part_id`` in ``acknowledge_flagged_part_ids``, which
    is what turns the caution into a decision somebody made on purpose.
    """

    part_id: int
    part_number: str
    matched_token: str
    # Which field the token was found in: ``part_number`` or ``name``.
    field: str


class OpenReservationSchema(UTCModel):
    """One open work-order material tie standing against the SOURCE part.

    ``outstanding_quantity`` is what that tie still expects to draw
    (``qty_planned - qty_consumed``, floored at zero). Listed by work order so the
    operator knows exactly which job to untie or re-tie before combining, instead
    of being told only that "something" is reserved.
    """

    work_order_id: int
    work_order_number: str
    work_order_status: str
    outstanding_quantity: float


class StockLineSchema(UTCModel):
    """One stock row (part / location / lot / serial) in a combine preview.

    ``eligible`` is what decides whether this row's material moves. An ineligible
    row is listed anyway, with ``ineligible_reason`` saying why -- material on
    hold, in quarantine, rejected or deactivated is NEVER silently folded into
    another SKU, and it is never silently hidden either.

    THREE quantity figures, and the difference between them is the point:

    * ``quantity_available`` = ``on_hand - allocated``. Unchanged meaning; this is
      what the rest of the app calls "available".
    * ``quantity_pinned`` = how much of that this row holds back because an OPEN
      work-order tie is PINNED to this exact lot (``pinned_inventory_item_id``).
      A pin is a per-row reservation, so it is withheld the same way
      ``quantity_allocated`` is -- see ``quantity_combinable``.
    * ``quantity_combinable`` = ``quantity_available - quantity_pinned``, floored at
      zero. THIS is what the combine may take off this row, and the sum of it over
      the eligible rows is ``eligible_available``.
    """

    inventory_item_id: int
    location: Optional[str] = None
    warehouse: Optional[str] = None
    lot_number: Optional[str] = None
    serial_number: Optional[str] = None
    quantity_on_hand: float = 0.0
    quantity_allocated: float = 0.0
    quantity_available: float = 0.0
    # NEW (both default to a backward-compatible value): a client that does not know
    # about pins sees exactly what it saw before.
    quantity_pinned: float = 0.0
    quantity_combinable: float = 0.0
    unit_cost: float = 0.0
    status: Optional[str] = None
    eligible: bool = False
    ineligible_reason: Optional[str] = None


class PartStockSummarySchema(UTCModel):
    """Everything the confirm dialog needs to show about one side of a combine.

    ``total_available`` is ``on_hand - allocated`` across every row.
    ``eligible_available`` is the COMBINABLE figure across the ELIGIBLE rows only,
    and it is the number that actually bounds the move -- the two differ by exactly
    the stock that is on hold, quarantined, rejected, on a deactivated row, or
    withheld because an open work-order tie is pinned to that lot
    (``total_pinned``).

    Rendering ``total_available`` alone on the TARGET panel understates nothing but
    OVERSTATES usability: a quarantined target row reads "Available 10" when no
    combine and no consumption may touch it. Show ``eligible_available`` on both
    sides.
    """

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
    # NEW, backward-compatible default: total withheld by lot-directed (pinned)
    # open work-order ties across every row. ``eligible_available`` is already net
    # of it; this is here so a dialog can EXPLAIN a cap that would otherwise look
    # arbitrary.
    total_pinned: float = 0.0
    eligible_available: float = 0.0
    lines: List[StockLineSchema] = []


class CombineCostSchema(UTCModel):
    """What each side's stock is currently carried at, and whether that changes.

    NOTHING here is a reblend. A combine never recomputes a weighted average: a
    NEWLY CREATED target lot carries the source lot's ``unit_cost`` verbatim (the
    material and its cost move together), and an EXISTING target lot keeps its own
    ``unit_cost`` untouched. This block exists so the operator can see the
    difference BEFORE approving, rather than discover it in a valuation report
    afterwards.

    A side with no stock reports ``0.0`` -- and ``differs`` is then ``False``,
    because "nothing to compare" is not a disagreement.
    """

    source_weighted_unit_cost: float = 0.0
    target_weighted_unit_cost: float = 0.0
    differs: bool = False
    note: str = ""


class InventoryCombinePreviewRequest(BaseModel):
    """Ask what a combine would do. Writes nothing.

    ``quantity`` is optional: omit it and the preview answers for the full
    eligible available quantity (which it also returns as ``default_quantity``),
    which is what the dialog pre-fills.

    The lower bound is ``MINIMUM_COMBINE_QUANTITY``, not ``0`` -- the preview and
    the write have to refuse the same inputs or the preview stops being a truthful
    rehearsal of the write.
    """

    source_part_id: int
    target_part_id: int
    quantity: Optional[float] = Field(
        default=None,
        gt=MINIMUM_COMBINE_QUANTITY,
        description="Quantity to model. Omit for the source's full eligible available quantity.",
    )


class InventoryCombinePreviewResponse(UTCModel):
    """What the combine would do, and every reason it might be refused.

    ``eligible`` is ``blockers == []`` and is **not a durable verdict**. Stock
    moves, ties are opened, parts are renumbered; the write re-runs every probe
    against the state at write time. A client must never treat a stale
    ``eligible: true`` as authorization. (Same contract as
    ``PartRenumberImpactResponse`` and ``PartBackflushReadinessResponse``,
    deliberately.)

    Two of the write's refusal codes cannot appear here, because the preview
    request carries neither input: ``expected_part_number_mismatch`` (there are no
    expected numbers to compare) and ``source_still_has_stock`` (there is no
    ``deactivate_source`` flag). Everything else the write can refuse is listed.

    Three codes joined that list and a client must render them as hard refusals:

    * ``target_row_not_available`` -- the material would land on a TARGET row that
      is on hold, quarantined, rejected or deactivated. This is the one blocker
      that says the operator's stock is about to become UNUSABLE: available source
      material folded onto a held target row is still counted and no longer
      drawable, and it used to return **200** with an empty ``blockers`` list.
    * ``target_serial_mismatch`` -- a serialized lot would merge onto a row
      carrying a different serial number, so one serial would claim two units.
    * ``quantity_below_minimum`` -- a sub-epsilon quantity that would move nothing.
      In practice the request schema's ``gt`` bound refuses this first, with a 422.
    """

    source: PartStockSummarySchema
    target: PartStockSummarySchema

    # ``uom_label(source) == uom_label(target)``, with a BLANK on either side
    # counting as a match: a part that states no stocking unit makes no claim to
    # contradict. A blank still raises an advisory, because folding an
    # unit-less SKU into a unit-ed one is worth a human glance.
    unit_of_measure_match: bool = True

    # The source's full eligible available quantity -- what the dialog pre-fills.
    default_quantity: float = 0.0
    # The largest quantity that would NOT trip the open-tie reservation check.
    # Offered so a dialog can propose the safe number instead of a dead end.
    max_combinable_quantity: float = 0.0
    # Total outstanding demand from open work-order ties naming the source part.
    reserved_quantity: float = 0.0

    eligible: bool = False
    blockers: List[CombineDiagnosticSchema] = []
    advisories: List[CombineDiagnosticSchema] = []
    flagged_parts: List[FlaggedPartSchema] = []
    open_source_reservations: List[OpenReservationSchema] = []
    cost: CombineCostSchema = CombineCostSchema()


class InventoryCombineRequest(BaseModel):
    """Perform the combine.

    ``expected_source_part_number`` / ``expected_target_part_number`` are the
    compare-and-swap precondition, not decoration. ``Part`` maps no
    optimistic-lock version column, so the part-number strings are the ONLY
    concurrency control available: if somebody renumbered either part while this
    dialog was open, the request is refused **409** rather than folding stock into
    a SKU the operator never saw. They are plain ``str`` (not the strict
    ``PartNumber`` annotated type) for the same reason
    ``PartRenumberRequest.expected_part_number`` is: production holds numbers the
    pattern rejects (``1/4" PLATE 48 X 96``), and those legacy numbers are exactly
    the ones a recut leaves duplicated.

    ``quantity`` is ``gt=MINIMUM_COMBINE_QUANTITY``, so a literal ``0`` -- and any
    sub-epsilon value the drain loop would decline to move -- is a **422** before
    the handler runs, distinct from the **409** ``no_available_stock`` a second
    combine of an already-drained SKU gets. It was ``gt=0``; see
    ``MINIMUM_COMBINE_QUANTITY`` for the phantom-record bug that closed.
    """

    source_part_id: int
    target_part_id: int
    quantity: float = Field(
        gt=MINIMUM_COMBINE_QUANTITY,
        description="How much to move from the source SKU onto the target SKU.",
    )
    reason: str = Field(
        ...,
        min_length=5,
        max_length=500,
        description="Why these two numbers describe the same article. Recorded on the combine and the audit row.",
    )
    expected_source_part_number: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="The source part's number as the client last read it. Compare-and-swap precondition.",
    )
    expected_target_part_number: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="The target part's number as the client last read it. Compare-and-swap precondition.",
    )
    acknowledge_flagged_part_ids: List[int] = Field(
        default_factory=list,
        description="Part ids from the preview's flagged_parts that the operator has explicitly confirmed.",
    )
    deactivate_source: bool = Field(
        default=False,
        description=(
            "Also mark the source part inactive/obsolete. Permitted only when it lands at exactly 0 on hand "
            "across ALL its stock rows, including ones this combine could not move."
        ),
    )

    @field_validator("reason")
    @classmethod
    def _reason_not_blank(cls, value: str) -> str:
        """``min_length=5`` alone passes ``"     "``. Copied from ``ReceiptVoidRequest``."""
        cleaned = (value or "").strip()
        if len(cleaned) < 5:
            raise ValueError("reason must be at least 5 characters")
        return cleaned


class CombineLineSchema(UTCModel):
    """One (location, lot) line that actually moved.

    ``target_row_created`` distinguishes the two cases the cost rule turns on: a
    NEW target row carries the source row's ``unit_cost``; an EXISTING one kept
    its own.
    """

    location: Optional[str] = None
    lot_number: Optional[str] = None
    quantity: float = 0.0
    unit_cost: float = 0.0
    source_inventory_item_id: int
    target_inventory_item_id: int
    target_row_created: bool = False


class InventoryCombineResponse(UTCModel):
    """The result of a combine.

    The four before/after figures are what makes the acceptance check answerable
    on the response alone: source 92 -> 0, target 141 -> 233, total unchanged.
    ``transaction_ids`` lists every ledger row the combine wrote -- 2 per line,
    summing to exactly zero.
    """

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
    lines: List[CombineLineSchema] = []
    transaction_ids: List[int] = []
