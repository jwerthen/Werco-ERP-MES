"""Request/response contracts for work-order material allocations (the material tie).

Response schemas inherit ``UTCModel`` so ``datetime`` fields serialize as UTC ISO-8601
with a trailing ``Z`` (invariant: store UTC, serve UTC, display Central).
"""

import enum
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from app.models.work_order_material import AllocationSource, AllocationStatus
from app.schemas.base import UTCModel


class MaterialAllocationCreate(BaseModel):
    """Tie a material part to a work order (or to one of its operations)."""

    part_id: int = Field(..., description="The MATERIAL part consumed — never the part being produced")
    work_order_operation_id: Optional[int] = Field(
        None,
        description=(
            "Set => the tie is OPERATION-scoped and depletes per completed run "
            "(the laser-nest case). Omit for a work-order-scoped, one-shot tie."
        ),
    )
    source: AllocationSource = Field(AllocationSource.MANUAL, description="How the tie was created")
    qty_per_run: Optional[float] = Field(
        None,
        gt=0,
        description=(
            "Material consumed per completed run. Applies to OPERATION-scoped ties only, where it "
            "defaults to 1.0 when omitted. Supplying it on a work-order-scoped tie (no "
            "``work_order_operation_id``) is a 422 — there are no runs to scale by."
        ),
    )
    qty_planned: float = Field(..., gt=0, description="Total material planned for this tie")
    pinned_inventory_item_id: Optional[int] = Field(
        None, description="Consume from THIS lot. Omit to pick FIFO at consume time."
    )
    notes: Optional[str] = None


class MaterialAllocationUpdate(BaseModel):
    """Edit an OPEN tie's quantities, lot pin, or notes.

    Every field is optional; omitted fields are left untouched. ``part_id``,
    ``work_order_operation_id`` and ``source`` are deliberately NOT editable — changing
    what a tie points at after consumption has posted would rewrite genealogy. Untie and
    re-tie instead.
    """

    qty_per_run: Optional[float] = Field(None, gt=0)
    qty_planned: Optional[float] = Field(None, gt=0)
    pinned_inventory_item_id: Optional[int] = None
    clear_pinned_inventory_item: bool = Field(False, description="Set true to drop the lot pin and fall back to FIFO")
    notes: Optional[str] = None


class MaterialAllocationResponse(UTCModel):
    """One material tie, with the display fields the tie table needs."""

    id: int
    work_order_id: int
    work_order_operation_id: Optional[int] = None
    operation_number: Optional[str] = None
    # Set only on a tie a nest re-import DETACHED: the operation it used to name, read
    # back off the audit chain. Without it a detached tie is indistinguishable from one
    # that was always work-order-scoped -- both read ``work_order_operation_id: null``.
    # Reporting only; the chain row remains the record of record.
    detached_from_operation_id: Optional[int] = Field(
        None,
        description=(
            "The operation this tie was tied to before a nest re-import superseded it and "
            "cleared the link. NULL for every tie that was never detached."
        ),
    )
    part_id: int
    part_number: Optional[str] = None
    part_name: Optional[str] = None

    source: AllocationSource
    status: AllocationStatus

    qty_per_run: Optional[float] = None
    qty_planned: float
    unit_of_measure: str
    # CACHE. The ledger (inventory_transactions.allocation_id) is authoritative.
    qty_consumed: float

    pinned_inventory_item_id: Optional[int] = None
    pinned_lot_number: Optional[str] = None

    notes: Optional[str] = None
    created_by: Optional[int] = None
    created_at: datetime
    updated_at: datetime


class MaterialReturnIntent(str, enum.Enum):
    """WHICH return the caller means. There are exactly two, and nothing in between.

    Consumption is sum-delta: the engine posts ``target - qty_consumed`` whenever that is
    positive, and ``target`` is recomputed from LIVE operation state on every call --
    including from a reconcile-on-read GET. So a return that leaves ``qty_consumed``
    BELOW the live target on a still-OPEN tie is not "a smaller return"; it is material
    the engine will silently draw again on the very next completion or page load, and
    that re-draw re-runs FIFO and can credit a DIFFERENT lot than the material came from,
    fabricating heat/cert linkage in an as-built record (AS9100D 8.5.2).

    Each intent closes that door a different way, and the API makes the caller say which:

    * ``CORRECT_OVER_CONSUMPTION`` -- the tie stays OPEN and live, so the return is
      BOUNDED by ``qty_consumed - target``. That bound is not a safety hack bolted on
      top: it is precisely the negative delta the engine already computes and refuses to
      execute (invariant 6b), now performed by an actor with a reason. After it,
      ``qty_consumed >= target``, so the engine no-ops forever on every path.
    * ``RETURN_AND_UNTIE`` -- give back everything consumed AND cancel the tie in the same
      transaction, so there is no OPEN row left for the engine to re-draw against. This is
      the unbounded one, and the only remedy for a tie that must give all its material
      back.

    A return that would leave ``qty_consumed < target`` with the tie still OPEN is refused
    with 422 naming ``return_and_untie`` as the alternative.
    """

    CORRECT_OVER_CONSUMPTION = "correct_over_consumption"
    RETURN_AND_UNTIE = "return_and_untie"


class MaterialReturnRequest(BaseModel):
    """Return consumed material to stock — the reasoned, audited compensating verb.

    ``reason`` is mandatory and non-blank, validated HERE at the Pydantic boundary
    exactly like ``ReceiptCorrection.reason`` (``schemas/purchasing.py``), so a blank
    reason is FastAPI's own 422 rather than a hand-rolled 400 somewhere downstream. It
    lands on the ledger row's ``notes`` AND in the audit ``description`` AND in
    ``extra_data.reason`` — the receiving void path only did the last of those, and a
    reason that is not on the row an auditor pulls is a reason nobody reads.
    """

    quantity: float = Field(
        ...,
        gt=0,
        description=(
            "How much material to return, in the tie's unit of measure. Must equal the "
            "tie's full qty_consumed when intent is return_and_untie."
        ),
    )
    intent: MaterialReturnIntent = Field(
        ...,
        description="Which of the two named returns this is — see MaterialReturnIntent.",
    )
    reason: str = Field(..., min_length=1, max_length=500, description="Why the material is being returned (required)")

    @field_validator("reason")
    @classmethod
    def reason_not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("reason must not be blank")
        return v.strip()


class MaterialReturnLot(UTCModel):
    """One lot credited back by a return — one row of the per-lot breakdown.

    A single logical return becomes N of these when the consumption it compensates
    spilled across several FIFO lots (or posted in several passes against one lot).
    Material always goes back to the lot it came from, so this is the shape the UI must
    render: "3 sheets to lot HT-4471, 1 sheet to lot HT-4470", never one anonymous total.
    """

    inventory_item_id: int
    lot_number: Optional[str] = None
    quantity: float
    # Copied from the ISSUE row being compensated, NOT the lot's current cost — a
    # revaluation between consume and return would otherwise strand material cost on the job.
    unit_cost: float
    transaction_id: int
    compensated_transaction_id: int


class MaterialConsumptionLine(UTCModel):
    """One source lot's ledger position on a tie: what it issued, gave back, and still holds.

    The read behind the return dialog — it answers "where would this material go back
    to?" BEFORE anything is confirmed, from the ledger rather than from the tie's
    ``qty_consumed`` cache. ``net`` is the per-lot CAP a return can credit against that
    lot, and the array is ordered newest source lot first: the exact order
    ``_plan_material_return`` walks, so the preview and the outcome cannot disagree
    about which lot gets what.

    There is deliberately no lot to CHOOSE here. Material goes back to the lots it came
    off or the return is refused (AS9100D 8.5.2), so this is a disclosure, not a picker.
    """

    inventory_item_id: int
    lot_number: Optional[str] = None
    issued: float
    returned: float
    # issued - returned, float dust clamped to zero. Zero means this lot is fully
    # squared up; the line is still listed because it is part of the tie's history.
    net: float


class MaterialReturnResponse(UTCModel):
    """What a return actually did: the per-lot breakdown and the tie's resulting state."""

    allocation_id: int
    work_order_id: int
    part_id: int
    part_number: Optional[str] = None
    intent: MaterialReturnIntent
    unit_of_measure: str
    quantity_returned: float
    qty_consumed_before: float
    # The tie's qty_consumed AFTER the return. Still a CACHE — the ledger
    # (inventory_transactions.allocation_id) remains authoritative.
    qty_consumed: float
    # OPEN after a correction, CANCELLED after a return-and-untie.
    status: AllocationStatus
    returned_lots: list[MaterialReturnLot]
