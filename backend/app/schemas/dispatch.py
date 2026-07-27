"""Pydantic contracts for the manager dispatch board and the run-order rewrite.

``run_order`` is an ADVISORY manual rank: a dense 1..N ordering of the live
queue AT ONE WORK CENTER, dictated by a manager on the dispatch board. It sorts
and displays the queue and it NEVER gates a start (same posture as the laser
dispatch pool). NULL means "unranked" and sorts after every ranked row.

It is NOT ``sequence``: ``sequence`` is routing-step precedence WITHIN one work
order and DOES gate (predecessor rules); ``run_order`` is cross-work-order,
scoped to a work center, and gates nothing.
"""

from datetime import date, datetime
from typing import List, Optional

from pydantic import BaseModel, Field

from app.schemas.base import UTCModel


class DispatchNestInfo(UTCModel):
    """The laser-nest facts a planner sequences a nest by.

    A deliberate SUBSET of the kiosk queue's ``laser_nest`` block (see
    ``_laser_nest_payload`` in ``app/api/endpoints/shop_floor.py``), using the
    same field names and the same meanings so the two payloads stay
    recognisably the same thing. The kiosk-only bits -- the nest id, the CNC
    file name/path, and the reference-PDF document fields -- are omitted: the
    board sequences work, it does not open programs or drawings.

    Every field is optional/defaulted because a nest may be keyed manually with
    only a CNC number, and the board must render what it has.
    """

    # Operator-/machine-facing program number keyed on the laser.
    cnc_number: Optional[str] = None
    # The three changeover drivers: swapping any of these costs a setup.
    material: Optional[str] = None
    thickness: Optional[str] = None
    sheet_size: Optional[str] = None
    # Sheet counts. ``completed_runs`` is the operation's completed quantity and
    # ``remaining_runs`` is ``max(0, planned - completed)`` -- exactly the
    # kiosk's numbers (see ``dispatch_service.dispatch_nest_info``).
    planned_runs: int = 0
    completed_runs: float = 0.0
    remaining_runs: float = 0.0


class DispatchMaterialTie(UTCModel):
    """The material tied to ONE queued operation, priced against real stock.

    Projected from ``material_tie_view.MaterialTieView`` -- the same read the
    kiosk queue serializes -- so the manager's chip and the operator's line can
    never disagree about what a tie means or how short it is.

    OPERATION-SCOPED ONLY. A work-order-scoped tie belongs to the whole job and
    drains through the one-shot backflush, so hanging it on a card would fan one
    tie across every card of that work order and read as N separate ties.

    Absent (``null``) on an untied operation -- and that is the WHOLE rendering
    contract: the client draws nothing, no placeholder and no "not tied" nag, so
    an untied work order looks byte-identical to its pre-feature self.

    ``qty_remaining`` / ``on_hand`` / ``short_by`` are SERVER-derived because the
    board is a read path that must not recompute stock (or write). ``short_by``
    is advisory: a shortage never blocks production, it warns.

    ``qty_remaining`` and ``short_by`` are **PLAN-based** (``qty_planned -
    qty_consumed``) and deliberately EXCLUDE scrap. That is the forward-looking
    question a planner asks of a queue -- *will this job have material when it
    runs?* -- and it is NOT the engine's consumption target, which is
    ``per_run x (quantity_complete + quantity_scrapped)``. Two consequences a
    reader must not mistake for bugs: an un-started operation still shows its
    full planned demand here (scoring it the engine's way would read ``0`` on a
    nest about to eat five sheets), and scrap can push the real draw ABOVE
    ``qty_planned``, so this chip can read "covered" while the kiosk's
    completion-time estimate flags short. The kiosk answers the other question
    on purpose; see ``material_tie_view``'s module docstring.

    Consumption fires at WORK-ORDER completion, never per run -- copy built on
    these numbers must say "deducts N when WO-#### finishes", never "deducting
    now".
    """

    allocation_id: int
    part_id: int
    part_number: Optional[str] = None
    # Snapshot of the part's UoM taken at tie time; nothing converts units.
    unit_of_measure: str
    # Material per completed run. NULL means "not run-scaled" and reads as 1.0;
    # carried raw so the tie editor can tell that apart from an explicit 1.
    qty_per_run: Optional[float] = None
    qty_planned: float = 0.0
    # CACHE, not the compliance figure: the ledger rows carrying this tie's
    # ``allocation_id`` are authoritative (invariant 6).
    qty_consumed: float = 0.0
    # max(0, planned - consumed), floored server-side.
    qty_remaining: float = 0.0
    # Pinned tie: that lot's own on-hand. Unpinned: the FIFO-eligible total.
    on_hand: float = 0.0
    # max(0, remaining - on_hand) for THIS tie. 0 = covered.
    short_by: float = 0.0
    # How many open ties the operation carries. The card renders ONE chip (this
    # one, the lowest allocation id), so without a count a second tied part is
    # invisible on the board.
    tie_count: int = 1
    # True when ANY of the operation's ties is short -- not just this one. The
    # chip tone and the column's "N short" rollup read THIS, so a shortage on a
    # tie the card had no room to draw still shows up.
    any_short: bool = False
    pinned_inventory_item_id: Optional[int] = None
    pinned_lot_number: Optional[str] = None


class DispatchQueueRow(UTCModel):
    """One live queued operation as the dispatch board / kiosk sees it.

    Mirrors the ``GET /shop-floor/work-center-queue/{id}`` row (minus the
    kiosk-only roster / process-step blocks) and adds the two fields the board
    needs to reorder safely: ``run_order`` (current rank) and ``version`` (the
    operation's optimistic-lock counter, so a client can tell a stale card from
    a fresh one).

    ``laser_nest`` is populated only for a laser-nest operation whose nest is
    live (not soft-deleted); every other row carries ``null``.

    ``material_tie`` follows the same "null unless it applies" rule (see
    :class:`DispatchMaterialTie`), and defaults to ``None`` so a pre-feature
    client is unaffected by its arrival -- exactly like ``laser_nest`` did.
    """

    operation_id: int
    # NULL = unranked; the queue sorts these after every ranked row.
    run_order: Optional[int] = None
    version: int
    work_order_id: int
    work_order_number: str
    operation_number: Optional[str] = None
    operation_name: str
    part_number: Optional[str] = None
    part_name: Optional[str] = None
    status: str
    priority: Optional[int] = None
    # date-only: stays YYYY-MM-DD (UTCModel only rewrites datetimes).
    due_date: Optional[date] = None
    quantity_ordered: float = 0.0
    quantity_complete: float = 0.0
    setup_time_hours: float = 0.0
    run_time_hours: float = 0.0
    # Nest details for laser rows; null for every non-laser operation.
    laser_nest: Optional[DispatchNestInfo] = None
    # The operation's open material tie; null for every untied operation.
    material_tie: Optional[DispatchMaterialTie] = None


class DispatchBoardColumn(UTCModel):
    """One work center and its live queue -- a column on the board.

    Emitted for every ACTIVE work center -- including ones with an empty queue,
    so a manager can drag work onto an idle machine -- PLUS any DEACTIVATED work
    center that still has queued work, flagged ``is_active=false`` so the client
    renders it read-only (drain-only: work can be moved OFF it, but it is not a
    drop target and its run order is not editable -- the run-order rewrite 404s
    inactive work centers). The work-center identity fields use the repo's
    ``id`` / ``code`` / ``name`` shape (matching ``WallboardWorkCenter``), not a
    ``work_center_*`` prefix.
    """

    id: int
    code: str
    name: str
    work_center_type: Optional[str] = None
    current_status: Optional[str] = None
    # False = a deactivated work center still holding queued work; render the
    # column flagged and read-only until its queue drains.
    is_active: bool = True
    queue: List[DispatchQueueRow] = []


class DispatchBoardResponse(UTCModel):
    work_centers: List[DispatchBoardColumn] = []
    generated_at: datetime


class RunOrderUpdateRequest(BaseModel):
    """The FULL desired order for one work center's column, front to back.

    Operations at that work center omitted from the list are unranked
    (``run_order = NULL``) so the column ends up exactly as submitted, with no
    drift. An empty list is valid and clears every rank in the column.
    """

    # Keep max_length in lock-step with dispatch_service.MAX_RUN_ORDER_IDS (the
    # service re-checks it; it cannot import this constant back without a cycle).
    operation_ids: List[int] = Field(
        default_factory=list,
        max_length=500,
        description="Operation ids in the desired run order (rank 1 first). Must all be live queued "
        "operations at this work center; duplicates are rejected. Omitted operations become unranked.",
    )
