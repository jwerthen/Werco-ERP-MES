"""Read-only contracts for the BOM/routing backflush: dry-run preview + opt-in readiness.

Two GETs share these shapes, and both are **pure reads that write nothing** -- see
``completion_inventory_service``'s dry-run section for why that is a structural property
of this feature rather than a convention:

* ``GET /work-orders/{id}/backflush-preview`` -- what a completion of THIS work order
  would consume, per component, down to the lots it would draw from.
* ``GET /parts/{id}/backflush-readiness`` -- whether this part can safely opt into
  ``backflush_components`` at all, and what refuses it if not.

Everything here inherits ``UTCModel`` rather than ``BaseModel`` (``app/schemas/base.py``):
these are RESPONSE schemas, and the platform invariant is store UTC, serve UTC (``Z``),
display Central. None of them carries a datetime today; inheriting anyway is what keeps
that true when one is added.
"""

from typing import List, Optional

from app.schemas.base import UTCModel


class BackflushDiagnostic(UTCModel):
    """One thing the demand resolver could not answer cleanly about this BOM/routing.

    ``severity`` is the field that matters:

    * ``blocking`` -- the resolved demand is wrong or absent. These are exactly the
      sentences the part-edit refusal gate joins into its 409 ``detail``, so ``detail``
      reads correctly after ``"Part {part_number} cannot enable automatic backflush: "``.
    * ``advisory`` -- usable, but worth a human's attention, OR work-order-scoped and
      therefore not gateable at part opt-in (a zero ordered quantity, a non-COMPLETE
      operation contributing demand, a tie whose basis disagrees with the BOM's).

    ``code`` is the stable machine key; ``detail`` is the operator-facing sentence. The
    remaining fields are optional context for linking back to the offending row.
    """

    code: str
    severity: str
    detail: str
    bom_item_id: Optional[int] = None
    component_part_id: Optional[int] = None
    component_part_number: Optional[str] = None
    operation_id: Optional[int] = None


class BackflushPreviewLot(UTCModel):
    """One ISSUE row the completion would post: which lot, and how much of it.

    Ordered as the draw would take them: ``received_date`` FIFO over active, consumable,
    positive-on-hand lots -- the one policy both consumption engines share since PR 4.4.
    A PINNED tie shows exactly one lot, because a pin is a lot-directed instruction that
    is driven negative rather than spilling onto a different heat.

    ``is_shortfall`` marks the row for the part of the demand no permitted lot could
    cover. The writer posts it as a SEPARATE ISSUE against the last lot it drew (or the
    first candidate lot if it drew nothing), driving that lot negative and carrying its
    lot number onto the as-built record -- so a line can legitimately list the same
    ``inventory_item_id`` twice: the covered take, then the remainder.
    """

    inventory_item_id: int
    lot_number: Optional[str] = None
    location: Optional[str] = None
    quantity: float
    unit_cost: float = 0.0
    is_shortfall: bool = False


class BackflushPreviewLine(UTCModel):
    """One component's full decision: target, what already posted, and the lots it hits.

    ``delta_quantity`` -- ``required_quantity - already_issued`` -- is what would actually
    post now; the leg reconciles to target and never auto-reverses, so a non-positive
    delta is a no-op rather than a credit.

    ``suppressed`` marks a line that will NOT move material, with ``suppression_reason``
    naming which rule stopped it:

    * ``converged`` -- the ledger already holds the whole target. Nothing is wrong.
    * ``already_issued`` -- a LEGACY (pre-PR-4.4) one-shot ``('work_order', ISSUE)`` row
      fences this work order out of the reconciling engine for this part, permanently.
    * ``ledger_consumed`` -- an operation-scoped tie already drew this part against this
      job; the BOM's demand for it is dropped so the material cannot leave twice.
    * ``open_operation_tie`` -- an OPEN operation-scoped tie owns this part's demand. The
      material still moves, just on the per-run engine rather than on this leg.
    * ``blocking_diagnostic`` -- a BLOCKING diagnostic stands against this component (or
      against the whole leg, when the condition is structural), so the completion will
      REFUSE it: no material moves and a ``BACKFLUSH_DEMAND_REFUSED`` audit row is written
      instead. The only one of the five that means something is WRONG rather than already
      handled; the response-level ``blockers`` say what, and the remedy is to fix the BOM
      line / operation they name. Mirrored here from the pure ``blocked_demand_refusal`` so
      the dry run and the outcome cannot disagree.

    ``requires_opt_in`` is True on BOM/routing lines, which move only once
    ``Part.backflush_components`` is on, and False on work-order-scoped tie lines, where
    the tie itself IS the opt-in and consumes regardless.

    ``would_go_negative`` means the planned draw cannot be covered by the lots the policy
    permits; ``held_quantity_skipped`` / ``held_lot_numbers`` then disclose stock that IS
    on hand but segregated (on hold / quarantine / rejected / inactive), which is the
    difference between a purchasing signal and an MRB signal. On a PINNED line those stay
    zero/empty by design -- the pin, not any lot's status, is why nothing else was drawn.
    ``pinned_lot_is_held`` is the pinned line's equivalent and a stronger warning: the
    pinned lot itself went on hold / quarantine / rejected AFTER it was pinned, and the
    completion will consume it anyway (recording ``HELD_MATERIAL_CONSUMED``). That draw is
    not short, so no ``held_*`` disclosure runs for it.

    ``shortfall_creates_placeholder`` means this part has NO stock row at all, so rather
    than driving an existing lot negative the completion would mint a lot-less placeholder
    row and post against that. It is mutually exclusive with a trailing ``is_shortfall``
    lot.
    """

    component_part_id: int
    component_part_number: Optional[str] = None
    component_part_name: Optional[str] = None
    unit_of_measure: Optional[str] = None
    # "bom_routing" | "work_order_tie"
    source: str
    requires_opt_in: bool = True
    allocation_id: Optional[int] = None
    required_quantity: float
    already_issued: float = 0.0
    delta_quantity: float = 0.0
    suppressed: bool = False
    suppression_reason: Optional[str] = None
    available_quantity: float = 0.0
    shortfall: float = 0.0
    would_go_negative: bool = False
    held_quantity_skipped: float = 0.0
    held_lot_numbers: List[str] = []
    pinned_inventory_item_id: Optional[int] = None
    pinned_lot_number: Optional[str] = None
    pinned_lot_is_held: bool = False
    shortfall_creates_placeholder: bool = False
    lots: List[BackflushPreviewLot] = []


class BackflushPreviewResponse(UTCModel):
    """A dry run of this work order's component consumption. Nothing was written.

    ``backflush_components`` is the finished part's CURRENT flag. BOM/routing lines are
    reported whether or not it is set -- the operator is deciding whether to set it, and a
    preview that showed nothing until afterwards could not inform that decision -- so read
    it together with each line's ``requires_opt_in``.

    ``basis`` is the demand basis the BOM was exploded against:
    ``quantity_complete + operation scrap``, the same basis the per-run tie engine uses. A
    work order that has produced nothing has a basis of 0 and therefore no BOM lines at
    all; that is the resolver's real behaviour, not a preview artifact.
    """

    work_order_id: int
    work_order_number: Optional[str] = None
    part_id: Optional[int] = None
    part_number: Optional[str] = None
    backflush_components: bool = False
    basis: float = 0.0
    lines: List[BackflushPreviewLine] = []
    blockers: List[BackflushDiagnostic] = []
    advisories: List[BackflushDiagnostic] = []


class PartBackflushReadinessResponse(UTCModel):
    """Whether a part may opt into automatic backflush, and what refuses it if not.

    ``eligible`` is simply ``blockers == []``. It is **not a durable verdict**: every
    input it reads -- BOM lines, ``is_alternate`` / ``is_optional`` / ``item_type`` /
    ``quantity``, the routing's ``component_part_id`` -- is mutable afterwards by other
    people, so the same check re-runs server-side on the write that turns the flag on.
    A client must never treat a stale ``eligible: true`` as authorisation.

    Only the BOM half is answerable here: routing conditions are work-order-scoped and
    surface on ``GET /work-orders/{id}/backflush-preview`` instead.
    """

    part_id: int
    part_number: Optional[str] = None
    # The part's CURRENT flag, so a client can render state and eligibility together.
    backflush_components: bool = False
    eligible: bool = False
    blockers: List[BackflushDiagnostic] = []
    advisories: List[BackflushDiagnostic] = []
