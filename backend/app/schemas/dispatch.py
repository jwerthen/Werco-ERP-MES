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


class DispatchQueueRow(UTCModel):
    """One live queued operation as the dispatch board / kiosk sees it.

    Mirrors the ``GET /shop-floor/work-center-queue/{id}`` row (minus the
    kiosk-only roster / process-step blocks) and adds the two fields the board
    needs to reorder safely: ``run_order`` (current rank) and ``version`` (the
    operation's optimistic-lock counter, so a client can tell a stale card from
    a fresh one).

    ``laser_nest`` is populated only for a laser-nest operation whose nest is
    live (not soft-deleted); every other row carries ``null``.
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


class DispatchBoardColumn(UTCModel):
    """One work center and its live queue -- a column on the board.

    Emitted for EVERY active work center, including ones with an empty queue,
    so a manager can drag work onto an idle machine. The work-center identity
    fields use the repo's ``id`` / ``code`` / ``name`` shape (matching
    ``WallboardWorkCenter``), not a ``work_center_*`` prefix.
    """

    id: int
    code: str
    name: str
    work_center_type: Optional[str] = None
    current_status: Optional[str] = None
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
