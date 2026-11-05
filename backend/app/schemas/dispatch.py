"""Pydantic contracts for the manager dispatch board and the run-order rewrite.

``run_order`` is an ADVISORY manual rank: a dense 1..N ordering of the live
queue AT ONE WORK CENTER, dictated by a manager on the dispatch board. It sorts
and displays the queue and it NEVER gates a start (same posture as the laser
dispatch pool). NULL means "unranked" and sorts after every ranked row.

It is NOT ``sequence``: ``sequence`` is routing-step precedence WITHIN one work
order and DOES gate (predecessor rules); ``run_order`` is cross-work-order,
scoped to a work center, and gates nothing.
"""

import enum
from datetime import date, datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from app.schemas.base import UTCModel

# Upper bound on every dispatch-order list in a request body. Keep in lock-step
# with ``dispatch_service.MAX_RUN_ORDER_IDS`` (the services re-check it; they
# cannot import this constant back without a cycle).
MAX_DISPATCH_ORDER_IDS = 500


class DispatchChangeKind(str, enum.Enum):
    """What kind of planner action changed a machine's queue order. CLOSED VOCABULARY.

    Carried on the ``dispatch_order_changed`` notice payload (:class:`DispatchNotice`)
    so the floor can say WHY its queue moved. A str Enum on purpose: every value is one
    whitespace-free lower-case token, which is what lets it ride an SMS/email
    classifier fence later without a new vocabulary review. Add members rather
    than renaming -- clients key on these strings.
    """

    # The planner reordered the shop-wide work-order plan.
    WORK_ORDER_ORDER = "work_order_order"
    # The planner reordered operations WITHIN one work order (pool/laser WOs).
    OPERATION_ORDER = "operation_order"
    # The planner rewrote one machine's column order directly.
    MACHINE_ORDER = "machine_order"
    # An operation was moved onto / off this machine.
    MOVED_IN = "moved_in"
    MOVED_OUT = "moved_out"


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
    drains through the completion backflush's own tie leg, so hanging it on a card
    would fan one tie across every card of that work order and read as N separate
    ties.

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

    Consumption fires when the OPERATION completes, never per run. A dispatch
    card IS an operation row, so copy built on these numbers should say "deducts
    N when this operation completes". Both neighbouring phrasings are wrong:
    "when WO-#### finishes" UNDERSTATES (a laser WO carries one operation per
    nest and each nest now deducts as it closes), while "deducting now" or
    anything per-run OVERSTATES -- reporting runs on a still-open operation
    posts nothing, because an ``IN_PROGRESS`` operation is still reducible and
    consumption never auto-reverses.
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
    # 083. Build identity, mirroring the kiosk row's key of the same name.
    unit_number: Optional[str] = None
    operation_number: Optional[str] = None
    operation_name: str
    part_number: Optional[str] = None
    # The COMPONENT's own number, read LIVE from the part.
    #
    # On an assembly work order the operation builds a component, not the work
    # order's produced part -- so ``part_number`` above is the ASSEMBLY's. Until now
    # the component's number reached this board only through the prefix baked into
    # ``operation_name`` at mint time, which is a SNAPSHOT of whatever the number was
    # the day the work order was raised.
    #
    # Once parts became renumberable that snapshot could go stale, and the renumber
    # deliberately does not rewrite operation names (they are part of the released
    # quality plan). Deriving this live is what keeps the board honest -- for that
    # rename and every future one -- without mutating a released record.
    #
    # NULL on the ordinary case: most operations carry no component.
    component_part_number: Optional[str] = None
    component_part_name: Optional[str] = None
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
    # Dispatch v2 (migration 114). The work order's gap-free position in the
    # shop-wide PLAN (1..N over every live ranked work order), or null when the
    # work order is not in the plan -- AND null when this row's EFFECTIVE rank is
    # null (an ON_HOLD work order sinks out of the plan block, so its row must not
    # claim a plan position it is not sorted by). The board draws its work-order
    # bands from this. Optional/defaulted so a pre-v2 client is unaffected.
    dispatch_position: Optional[int] = None
    # Routing step of this operation within its work order (OP10 before OP20).
    sequence: Optional[int] = None
    # The parent work order's status ("released", "on_hold", ...).
    work_order_status: Optional[str] = None


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
    # Sum of (setup_time_hours + run_time_hours) over the rows in ``queue`` -- the
    # planned hours waiting at this machine. Deliberately simple (planned, not
    # remaining): a load cue for the planner, not a capacity calculation.
    queued_hours: float = 0.0


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
        max_length=MAX_DISPATCH_ORDER_IDS,
        description="Operation ids in the desired run order (rank 1 first). Must all be live queued "
        "operations at this work center; duplicates are rejected. Omitted operations become unranked. "
        "Must respect work-order plan order: an operation of a work order later in the plan (or not in "
        "the plan) may not be placed before an operation of an earlier planned work order (409).",
    )
    # Compare-and-swap (Dispatch v2). The column's queue as the client RENDERED it
    # (operation ids, top to bottom). When present, the server compares it with the
    # live queue -- after dropping ids that have since left the queue (completed,
    # held, moved) from both sides -- and refuses 409 on any other difference, so two
    # planners editing one column can no longer overwrite each other silently.
    # Absent = the pre-v2 contract, unchanged (last writer wins).
    expected_operation_ids: Optional[List[int]] = Field(
        default=None,
        max_length=MAX_DISPATCH_ORDER_IDS,
        description="Optional compare-and-swap guard: the column's queued operation ids as the client "
        "last rendered them, top to bottom. A mismatch with the live queue is refused with 409.",
    )


# --------------------------------------------------------------------------
# Dispatch v2: the work-order-first board (lanes) and its two order verbs.
# --------------------------------------------------------------------------


class DispatchCrewMember(UTCModel):
    """One person clocked into an operation right now (an open LABOR time entry)."""

    user_id: int
    # Public-screen-safe form ("Jon W."), the same one the kiosk roster uses.
    display_name: Optional[str] = None


class DispatchLaneOperation(UTCModel):
    """One operation of a work-order lane -- EVERY live status, not just queued work.

    The machine view shows only READY/IN_PROGRESS rows; a lane shows the whole
    routing (PENDING, ON_HOLD and COMPLETE too) so a planner can see where the job
    is. Read-only facts; the only things a client may act on are ``version`` (for a
    machine move) and the queued rows' order (the operation-order verb).
    """

    operation_id: int
    # The operation's optimistic-lock counter -- required by the machine-move PUT.
    version: int
    sequence: int
    operation_number: Optional[str] = None
    operation_name: str
    status: str
    work_center_id: Optional[int] = None
    work_center_code: Optional[str] = None
    # "RUN n" at its machine: the gap-free position in that machine's LIVE queue,
    # identical to the kiosk RUN chip. Null for an off-queue (pending/held/complete)
    # or unranked operation.
    run_position: Optional[int] = None
    # PENDING only: a lower-sequence operation still has to finish first. Computed by
    # the SAME predecessor rule promotion and clock-in use (never a copy of it);
    # always false on a laser work order (a dispatch pool with no ordering).
    blocked_by_predecessor: bool = False
    on_hold: bool = False
    crew: List[DispatchCrewMember] = []
    quantity_complete: float = 0.0
    quantity_target: float = 0.0
    setup_time_hours: float = 0.0
    run_time_hours: float = 0.0
    component_part_number: Optional[str] = None
    laser_nest: Optional[DispatchNestInfo] = None
    material_tie: Optional[DispatchMaterialTie] = None


class DispatchWorkOrderLane(UTCModel):
    """One live work order on the work-order-first board.

    ``mode`` tells the client what the planner may do with the operation order:
    ``routing`` (sequenced -- the routing IS the order, read-only), ``pool``
    (operations at a machine are mutually startable; their order is a dispatch
    preference the planner may set) or ``laser`` (a nest package: a pool with no
    predecessor gating at all).
    """

    work_order_id: int
    work_order_number: str
    # The work order's optimistic-lock counter (the plan verbs do NOT bump it).
    version: int
    unit_number: Optional[str] = None
    status: str
    work_order_type: Optional[str] = None
    sequential_operations: bool = True
    is_laser: bool = False
    mode: Literal["routing", "pool", "laser"]
    # Gap-free 1..N over every live ranked work order; null = not in the plan.
    dispatch_position: Optional[int] = None
    priority: Optional[int] = None
    due_date: Optional[date] = None
    must_ship_by: Optional[date] = None
    # The promise the shop is held to: ``must_ship_by`` when set, else ``due_date``.
    promise_date: Optional[date] = None
    customer_name: Optional[str] = None
    part_number: Optional[str] = None
    part_name: Optional[str] = None
    quantity_ordered: float = 0.0
    quantity_complete: float = 0.0
    ops_total: int = 0
    ops_complete: int = 0
    # Unresolved (open or acknowledged) blockers on this work order.
    open_blocker_count: int = 0
    # True when any operation's open material tie is short.
    any_material_short: bool = False
    operations: List[DispatchLaneOperation] = []


class DispatchWorkCenterOption(UTCModel):
    """A work center the client may label a lane operation with or move one to."""

    id: int
    code: str
    name: str
    work_center_type: Optional[str] = None
    # False = deactivated; listed only because a live operation still sits there.
    # Never a valid move target.
    is_active: bool = True
    current_status: Optional[str] = None


class DispatchWorkOrderBoardResponse(UTCModel):
    """``GET /shop-floor/dispatch-board/work-orders`` -- the plan, lane by lane.

    ``work_orders`` lists the plan first (ranked lanes in plan order) and then every
    other live work order ("not in plan"), so ``ranked_count`` is also the index
    where the unranked section starts.
    """

    generated_at: datetime
    work_orders: List[DispatchWorkOrderLane] = []
    ranked_count: int = 0
    unranked_count: int = 0
    work_centers: List[DispatchWorkCenterOption] = []


class WorkOrderOrderUpdateRequest(BaseModel):
    """The FULL desired plan, front to back, plus the plan the client rendered.

    Whole-list authority: every live work order listed gets dense ranks 1..N in the
    given order, and EVERY other work order of the company that still carries a rank
    leaves the plan. An empty list clears the plan.
    """

    work_order_ids: List[int] = Field(
        ...,
        max_length=MAX_DISPATCH_ORDER_IDS,
        description="Work order ids in the desired plan order (position 1 first). Omitted work orders "
        "leave the plan. Duplicates are rejected.",
    )
    expected_work_order_ids: List[int] = Field(
        ...,
        max_length=MAX_DISPATCH_ORDER_IDS,
        description="Compare-and-swap guard: the planned work order ids exactly as the client last "
        "rendered them. A mismatch with the live plan is refused with 409.",
    )


class OperationOrderUpdateRequest(BaseModel):
    """The desired order of ONE work order's queued operations, plus what was rendered.

    Lists every READY/IN_PROGRESS operation of the work order exactly once. Only the
    RELATIVE order of the operations that share a machine matters: at each machine
    they become that work order's block order there.
    """

    operation_ids: List[int] = Field(
        ...,
        max_length=MAX_DISPATCH_ORDER_IDS,
        description="The work order's queued (ready/in-progress) operation ids in the desired order.",
    )
    expected_operation_ids: List[int] = Field(
        ...,
        max_length=MAX_DISPATCH_ORDER_IDS,
        description="Compare-and-swap guard: the work order's queued operation ids exactly as the client "
        "last rendered them (lane order). A mismatch is refused with 409.",
    )


# --------------------------------------------------------------------------
# Dispatch v2: the floor notice ("the planner changed this machine's order").
# --------------------------------------------------------------------------


class DispatchNoticeNextUp(UTCModel):
    """The job a machine's queue said to run next, frozen when the notice was written.

    "Next up" is the first READY operation of the queue (the head may be jobs already
    running), else the first row. Built from the queue at write time and never
    re-queried, so a later edit cannot change what a notice said. Every field is
    office-authored work-order/operation identity the same station already renders
    on its queue cards -- no new category of data.
    """

    operation_id: int
    work_order_number: Optional[str] = None
    unit_number: Optional[str] = None
    operation_number: Optional[str] = None
    operation_name: Optional[str] = None
    part_number: Optional[str] = None


class DispatchNotice(UTCModel):
    """The latest ``dispatch_order_changed`` notice for one work center.

    Rides the floor's existing polls (``GET /shop-floor/work-center-queue/{id}``,
    ``GET /shop-floor/my-active-job``, and ``GET /shop-floor/operations`` when it is
    filtered to one work center) so a changed run order reaches the operator on the
    screen they already look at. ``id`` increases with every notice, so a client
    acknowledges by remembering the last id it showed.

    EXACTLY these keys -- the work-center-queue read is open to an unattended crew
    station, and a test pins the set. ``actor_display`` is the "First L." form the
    station roster already shows; nothing here is operator-typed free text.
    """

    id: int
    occurred_at: datetime
    work_center_id: int
    work_center_code: Optional[str] = None
    # A DispatchChangeKind value: work_order_order | operation_order | machine_order |
    # moved_in | moved_out.
    change_kind: Optional[str] = None
    actor_display: Optional[str] = None
    next_up_before: Optional[DispatchNoticeNextUp] = None
    next_up_after: Optional[DispatchNoticeNextUp] = None
    # Operation ids AT THIS WORK CENTER that moved ahead of work they used to follow
    # (at most 10, queue order). An arrival or a departure alone moves nobody "up".
    moved_up: List[int] = []
    # How many dispatch notices this work center received inside the window -- lets
    # the client say "3 updates" instead of only showing the newest.
    updates_in_window: int = 1
