"""Dispatch run order: the ONE work-center queue query, and the rank rewrite.

Every surface that shows "what is queued at this machine" -- the crew/operator
kiosk (``GET /shop-floor/work-center-queue/{id}``), the manager dispatch board
(``GET /shop-floor/dispatch-board``), and the response of the run-order rewrite
(``PUT /shop-floor/work-centers/{id}/run-order``) -- builds its rows from
``queued_operations_query`` here, so the filter set and the sort can never
drift between the manager's board and the operator's tablet.

ADVISORY, NOT GATING
--------------------
``run_order`` is a manager-dictated dense 1..N rank WITHIN one work center. It
sorts and displays the queue and it NEVER decides whether an operation may
start -- start/clock-in eligibility stays entirely with
``operation_action_gates`` and the predecessor rules. Nothing in this module is
allowed to become an enforcement point.

``run_order`` vs ``sequence``
-----------------------------
``sequence`` is routing-step precedence WITHIN one work order (OP10 before
OP20) and DOES gate via ``has_incomplete_predecessors``. ``run_order`` is
cross-work-order, scoped to the operation's CURRENT work center, and gates
nothing. A future reader who conflates the two will build a gate by accident.

NULLS LAST, portably
--------------------
The queue sorts ranked work first, unranked (NULL) work last. ``ORDER BY col``
alone is NOT deterministic here: Postgres sorts NULLs last by default while
SQLite (the test backend) sorts them first. The sort therefore leads with the
explicit boolean key ``run_order IS NULL ASC`` -- false (0, ranked) before true
(1, unranked) -- which both dialects render natively without a ``NULLS LAST``
clause or a CASE expression. This replaced a single-key
``ORDER BY scheduled_start`` (nullable, no tiebreak) that made the operator's
queue order effectively arbitrary and dialect-dependent.
"""

from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from fastapi import HTTPException
from sqlalchemy import and_, case, update
from sqlalchemy.orm import Query, Session, joinedload
from sqlalchemy.orm.exc import StaleDataError

from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation
from app.schemas.dispatch import DispatchBoardColumn, DispatchNestInfo, DispatchQueueRow
from app.services.laser_nest_service import active_laser_nest
from app.services.work_order_state_service import TERMINAL_WO_STATUSES, operation_target_quantity

# Operation statuses that count as "on the machine's queue" -- work an operator
# could pick up right now. PENDING (predecessors outstanding) and COMPLETE /
# ON_HOLD are deliberately excluded.
QUEUE_OPERATION_STATUSES = (OperationStatus.READY, OperationStatus.IN_PROGRESS)

# Upper bound on a single run-order payload, so a runaway client cannot submit
# an unbounded list. Far above any real work center's live queue depth.
MAX_RUN_ORDER_IDS = 500

# The 409 body for a stale-write conflict on the rewrite. Shared so the service
# and the endpoint cannot drift from each other (or from docs/API.md).
RUN_ORDER_CONFLICT_DETAIL = "The queue changed while you were reordering. Refresh the dispatch board and try again."


def queue_row_load_options() -> Tuple:
    """Eager loads every :func:`dispatch_queue_row` needs -- ONE query, no N+1.

    The board renders EVERY active work center's queue at once, so a lazy load
    per row is not a minor cost: ``work_order`` -> ``part`` (part number/name)
    and ``laser_nest`` (the nest details a planner sequences by) would each cost
    one SELECT per card. Every caller that serializes rows must pass these.

    Returns a fresh tuple per call so a caller can extend it without mutating
    shared state. ``laser_nest`` is loaded WITHOUT an is-deleted filter on
    purpose: the soft-delete decision lives in :func:`dispatch_nest_info`, which
    is the same accessor the kiosk uses (``active_laser_nest``).
    """
    return (
        joinedload(WorkOrderOperation.work_order).joinedload(WorkOrder.part),
        joinedload(WorkOrderOperation.laser_nest),
    )


def queue_order_by() -> List:
    """The canonical dispatch-queue sort, as a list of ORDER BY expressions.

    1. ``run_order`` ascending with NULLs LAST (the manager's dictated order
       first) -- expressed as an explicit ``IS NULL`` boolean key so Postgres
       and SQLite agree (see the module docstring).
    2. The repo's canonical fallback for unranked work: ``WorkOrder.priority``,
       ``WorkOrder.due_date``, ``WorkOrderOperation.sequence``.
    3. ``WorkOrderOperation.id`` as a final deterministic tiebreak, so two rows
       that tie on everything above still come back in a stable order.

    Requires ``WorkOrder`` to be joined into the query.
    """
    return [
        WorkOrderOperation.run_order.is_(None).asc(),
        WorkOrderOperation.run_order.asc(),
        WorkOrder.priority.asc(),
        WorkOrder.due_date.asc(),
        WorkOrderOperation.sequence.asc(),
        WorkOrderOperation.id.asc(),
    ]


def queued_operations_query(
    db: Session,
    company_id: int,
    *,
    work_center_ids: Optional[Sequence[int]] = None,
    load_options: Sequence = (),
) -> Query:
    """Live queue at one or more work centers, tenant-scoped and canonically sorted.

    Tenancy comes from the joined ``WorkOrder.company_id`` (never client input).
    Filters: operation READY/IN_PROGRESS, parent WO not terminal
    (COMPLETE/CLOSED/CANCELLED) and not soft-deleted.

    ``work_center_ids=None`` means "every work center" (the caller narrows it);
    an EMPTY sequence is honoured literally and matches nothing.
    """
    query = db.query(WorkOrderOperation)
    if load_options:
        query = query.options(*load_options)
    query = query.join(WorkOrder).filter(
        and_(
            WorkOrder.company_id == company_id,
            WorkOrder.is_deleted == False,  # noqa: E712
            WorkOrder.status.not_in(TERMINAL_WO_STATUSES),
            WorkOrderOperation.status.in_(QUEUE_OPERATION_STATUSES),
        )
    )
    if work_center_ids is not None:
        query = query.filter(WorkOrderOperation.work_center_id.in_(list(work_center_ids)))
    return query.order_by(*queue_order_by())


def queued_operations(
    db: Session,
    company_id: int,
    work_center_ids: Optional[Sequence[int]] = None,
    *,
    load_options: Sequence = (),
) -> List[WorkOrderOperation]:
    """Materialize :func:`queued_operations_query` (already in queue order)."""
    return queued_operations_query(db, company_id, work_center_ids=work_center_ids, load_options=load_options).all()


def display_positions(operations: Iterable[WorkOrderOperation]) -> Dict[int, Optional[int]]:
    """Map operation id -> the rank the SHOP should see, or None when unranked.

    The stored ``run_order`` is dense 1..N only at the moment a manager writes
    it. It goes sparse in normal use -- completing a job or moving it to another
    work center takes its rank out of the column, leaving e.g. 1, 2, 4 behind.
    Showing those raw values to an operator reads as "job 3 is missing", so the
    number on screen is the position within the (already ordered) queue instead:
    always 1..N with no gaps, whatever the stored values drifted to.

    Positions count ranked operations only; unranked ones map to None and stay
    chip-less at the tail. Duplicate stored ranks (transient, and allowed by
    design -- no unique constraint) resolve deterministically here too, because
    position follows the query's total ordering rather than the raw value.

    ``operations`` MUST already be in queue order (see :func:`queue_order_by`).
    """
    positions: Dict[int, Optional[int]] = {}
    position = 0
    for operation in operations:
        if operation.run_order is None:
            positions[operation.id] = None
            continue
        position += 1
        positions[operation.id] = position
    return positions


def dispatch_nest_info(operation: WorkOrderOperation) -> Optional[DispatchNestInfo]:
    """The operation's live laser-nest details, or None when it has none.

    WHICH NEST IS LIVE is decided by ``laser_nest_service.active_laser_nest`` --
    the same accessor the kiosk queue uses -- so a SOFT-DELETED nest surfaces on
    neither surface. ``WorkOrderOperation.laser_nest`` loads whatever row points
    at the operation, deleted or not, so reading the relationship directly here
    would leak a deleted nest onto the board.

    READ-ONLY, deliberately. The kiosk's ``_laser_nest_payload`` first calls
    ``sync_laser_nest_from_operation``, which WRITES ``nest.completed_runs`` from
    the operation. This projection must not: ``GET /shop-floor/dispatch-board``
    documents itself as "no reconcile, no writes", and the same row builder also
    serves ``PUT .../run-order``, which DOES commit -- a reorder would silently
    persist a nest reconcile as a side effect.

    Instead it computes the same numbers the sync would have produced:
    ``completed_runs`` is the operation's completed quantity and
    ``remaining_runs`` is ``max(0, planned - completed)`` (the model's
    ``LaserNest.remaining_runs`` formula). So the board and the kiosk always show
    identical counts, without the board writing anything.
    """
    nest = active_laser_nest(operation)
    if nest is None:
        return None
    planned = int(nest.planned_runs or 0)
    completed = float(operation.quantity_complete or 0.0)
    return DispatchNestInfo(
        cnc_number=nest.cnc_number,
        material=nest.material,
        thickness=nest.thickness,
        sheet_size=nest.sheet_size,
        planned_runs=planned,
        completed_runs=completed,
        remaining_runs=max(0.0, float(planned) - completed),
    )


def dispatch_queue_row(operation: WorkOrderOperation, display_position: Optional[int] = None) -> DispatchQueueRow:
    """Project one queued operation onto the board/kiosk row shape.

    ``display_position`` is the gap-free rank from :func:`display_positions`;
    callers that have the whole ordered queue should pass it. Falling back to
    the raw stored rank keeps single-row callers correct-ish rather than blank.
    """
    work_order = operation.work_order
    part = work_order.part if work_order else None
    status = operation.status
    return DispatchQueueRow(
        operation_id=operation.id,
        run_order=display_position if display_position is not None else operation.run_order,
        version=operation.version,
        work_order_id=operation.work_order_id,
        work_order_number=work_order.work_order_number if work_order else "",
        operation_number=operation.operation_number,
        operation_name=operation.name,
        part_number=part.part_number if part else None,
        part_name=part.name if part else None,
        status=status.value if hasattr(status, "value") else str(status),
        priority=work_order.priority if work_order else None,
        due_date=work_order.due_date if work_order else None,
        quantity_ordered=operation_target_quantity(operation, work_order),
        quantity_complete=float(operation.quantity_complete or 0.0),
        setup_time_hours=float(operation.setup_time_hours or 0.0),
        run_time_hours=float(operation.run_time_hours or 0.0),
        laser_nest=dispatch_nest_info(operation),
    )


def board_column(work_center: WorkCenter, operations: Iterable[WorkOrderOperation]) -> DispatchBoardColumn:
    """One board column: a work center plus its (already-ordered) queue rows."""
    ordered = list(operations)
    positions = display_positions(ordered)
    return DispatchBoardColumn(
        id=work_center.id,
        code=work_center.code,
        name=work_center.name,
        work_center_type=work_center.work_center_type,
        current_status=work_center.current_status,
        queue=[dispatch_queue_row(op, positions.get(op.id)) for op in ordered],
    )


def active_work_centers(db: Session, company_id: int) -> List[WorkCenter]:
    """Every active work center in the company, code-ordered (board column order).

    ``WorkCenter`` has no soft-delete mixin, so "non-deleted" is ``is_active``.
    """
    return (
        db.query(WorkCenter)
        .filter(
            WorkCenter.company_id == company_id,
            WorkCenter.is_active == True,  # noqa: E712
        )
        .order_by(WorkCenter.code)
        .all()
    )


def build_dispatch_board(db: Session, company_id: int) -> Tuple[List[DispatchBoardColumn], datetime]:
    """Every active work center with its live queue, in ONE operations query.

    Avoids N+1: the operations for ALL columns are fetched once and bucketed in
    Python, preserving the query's canonical order within each bucket, and every
    relationship the row builder touches is eager-loaded via
    :func:`queue_row_load_options`. The board's cost must stay flat in the number
    of cards -- a per-row SELECT here is multiplied by the whole shop's queue.
    """
    work_centers = active_work_centers(db, company_id)
    by_work_center: Dict[int, List[WorkOrderOperation]] = {wc.id: [] for wc in work_centers}
    if work_centers:
        operations = queued_operations(
            db,
            company_id,
            list(by_work_center.keys()),
            load_options=queue_row_load_options(),
        )
        for operation in operations:
            # Defensive: the query already restricts to these ids.
            bucket = by_work_center.get(operation.work_center_id)
            if bucket is not None:
                bucket.append(operation)
    columns = [board_column(wc, by_work_center[wc.id]) for wc in work_centers]
    return columns, datetime.utcnow()


def resolve_active_work_center_or_http(db: Session, company_id: int, work_center_id: int) -> WorkCenter:
    """Tenant-scoped active work center, or 404.

    404 (not 403) for a foreign-tenant id: an id from another company must be
    indistinguishable from an id that does not exist.
    """
    work_center = (
        db.query(WorkCenter)
        .filter(
            WorkCenter.id == work_center_id,
            WorkCenter.company_id == company_id,
            WorkCenter.is_active == True,  # noqa: E712
        )
        .first()
    )
    if not work_center:
        raise HTTPException(status_code=404, detail="Work center not found or inactive")
    return work_center


def clear_run_order_on_move(operation: WorkOrderOperation, new_work_center_id: Optional[int]) -> bool:
    """Drop the manager's rank when an operation changes work center.

    A rank is meaningful only within the column it was dictated in; carried
    across it would outrank jobs the manager actually ordered at the
    destination. Returns True when the move actually changes the work center
    (callers use it to decide whether to record the clear in their audit diff).

    Call this BEFORE writing ``new_work_center_id`` onto the operation -- the
    comparison is against the operation's CURRENT work center. A ``None`` target
    or a no-op re-send of the current work center leaves the rank alone.

    This is the ONE implementation: every reassignment path calls it (the two
    operation-move endpoints and both scheduling reschedule paths), so a new
    call site cannot accidentally carry a rank across columns.
    """
    if new_work_center_id is None or new_work_center_id == operation.work_center_id:
        return False
    operation.run_order = None
    return True


def ranked_operation_ids(db: Session, company_id: int, work_center_id: int) -> List[int]:
    """Every operation at this work center that carries a rank, in rank order.

    Covers the WHOLE column, not just its live rows: an ON_HOLD or PENDING
    operation keeps its rank until a rewrite clears it, and the rewrite's audit
    diff should show those ranks going away too.

    Ties on ``run_order`` (allowed -- no unique constraint) break on id so the
    audit's "old order" is deterministic.
    """
    rows = (
        db.query(WorkOrderOperation.id)
        .filter(
            WorkOrderOperation.company_id == company_id,
            WorkOrderOperation.work_center_id == work_center_id,
            WorkOrderOperation.run_order.is_not(None),
        )
        .order_by(WorkOrderOperation.run_order.asc(), WorkOrderOperation.id.asc())
        .all()
    )
    return [row[0] for row in rows]


def _write_run_order(db: Session, company_id: int, work_center_id: int, ids: Sequence[int]) -> None:
    """Persist a column's ranks with CORE UPDATEs -- deliberately NOT via the ORM.

    Two reasons ``operation.run_order = rank`` is wrong here:

    1. ``WorkOrderOperation`` maps ``version_id_col``, so an ORM write bumps the
       optimistic-lock ``version`` of every row it touches. A rank is DISPLAY
       metadata: bumping it would 409 an operator's concurrent production post or
       clock-out on a job that is running RIGHT NOW, and would stale every card
       version the board just handed the client. A Core ``update()`` leaves
       ``version`` alone.
    2. The reset half has to reach rows the ORM has not loaded (below).

    The reset is AUTHORITATIVE FOR THE WHOLE COLUMN: every operation at this work
    center that is not in ``ids`` is unranked regardless of status, not just the
    live queued ones. An off-queue row (ON_HOLD, PENDING) that kept a stale rank
    would re-enter the column on resume ahead of jobs the manager ranked later.

    Tenant scoping is doubly explicit -- the caller resolved ``work_center_id``
    inside the company, and every statement also filters ``company_id``.
    """
    reset = update(WorkOrderOperation).where(
        WorkOrderOperation.company_id == company_id,
        WorkOrderOperation.work_center_id == work_center_id,
        WorkOrderOperation.run_order.is_not(None),
    )
    if ids:
        reset = reset.where(WorkOrderOperation.id.not_in(list(ids)))
    db.execute(reset.values(run_order=None).execution_options(synchronize_session=False))

    if not ids:
        return
    # One statement for the ranks: id -> rank as a CASE, so N rows cost one round
    # trip and the whole column lands atomically.
    ranks = case({operation_id: rank for rank, operation_id in enumerate(ids, start=1)}, value=WorkOrderOperation.id)
    db.execute(
        update(WorkOrderOperation)
        .where(
            WorkOrderOperation.company_id == company_id,
            WorkOrderOperation.work_center_id == work_center_id,
            WorkOrderOperation.id.in_(list(ids)),
        )
        .values(run_order=ranks)
        .execution_options(synchronize_session=False)
    )


def apply_run_order_or_http(
    db: Session,
    company_id: int,
    work_center: WorkCenter,
    operation_ids: Sequence[int],
) -> Tuple[List[int], List[int], List[WorkOrderOperation]]:
    """Rewrite a work center's manual run order to exactly ``operation_ids``.

    ADVISORY ONLY -- this changes queue *presentation*, never start eligibility.

    Contract:
    - every id must be a LIVE QUEUED operation at this work center (the same
      filter set the kiosk queue uses); anything else is a 400 naming the id,
      because it means the manager's board is stale and should be refreshed;
    - duplicate ids are a 400 (an ambiguous rank request);
    - the listed ids get dense ranks ``1..N`` in the given order, and every
      OTHER operation in the column is set back to unranked (NULL) so the
      column ends up exactly as submitted, with no leftover drift;
    - an empty list is valid and clears the whole column.

    A REWRITE IS AUTHORITATIVE FOR THE WHOLE COLUMN, NOT JUST ITS LIVE ROWS.
    The submitted ids must be live queued operations (a manager can only rank
    what is on the board), but the unranking half reaches EVERY operation at the
    work center whatever its status -- otherwise an ON_HOLD row would keep a
    stale rank and silently outrank, on resume, jobs the manager ranked after it.

    Writes through Core ``update()`` (see :func:`_write_run_order`): the
    optimistic-lock ``version`` of the ranked rows is intentionally NOT bumped,
    because a display-only reorder must never invalidate an operator's in-flight
    edit on a running job.

    Flushes (does not commit) so the caller can commit the change and its audit
    row atomically, in one transaction. Transient duplicate ranks mid-rewrite
    are harmless -- ``run_order`` carries no unique constraint by design.

    Returns ``(old_order, new_order, refreshed_queue)``.
    """
    ids = list(operation_ids)
    if len(ids) > MAX_RUN_ORDER_IDS:
        raise HTTPException(
            status_code=400,
            detail=f"Too many operations in run order (max {MAX_RUN_ORDER_IDS})",
        )
    seen: set = set()
    for operation_id in ids:
        if operation_id in seen:
            raise HTTPException(status_code=400, detail=f"Duplicate operation id in run order: {operation_id}")
        seen.add(operation_id)

    queue = queued_operations(db, company_id, [work_center.id])
    by_id = {op.id: op for op in queue}
    for operation_id in ids:
        if operation_id not in by_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Operation {operation_id} is not a live queued operation at work center "
                    f"{work_center.code}. Refresh the dispatch board and try again."
                ),
            )

    old_order = ranked_operation_ids(db, company_id, work_center.id)

    try:
        _write_run_order(db, company_id, work_center.id, ids)
        db.flush()
    except StaleDataError:
        # Belt and braces. The Core UPDATEs above do not touch ``version_id_col``,
        # so the rank write itself can no longer raise this -- but the flush also
        # flushes whatever else the request session had pending, and the 409 is a
        # documented contract of the endpoint (docs/API.md). Rolling back here
        # keeps the failure from escaping as a 500 with a dirty session.
        db.rollback()
        raise HTTPException(status_code=409, detail=RUN_ORDER_CONFLICT_DETAIL)

    # The Core UPDATEs bypassed the identity map, so every operation already
    # loaded in this session still holds its PRE-rewrite ``run_order``. Expire
    # before re-reading, or the response column (and anything the caller reads
    # afterwards) would serve the old ranks back.
    db.expire_all()
    refreshed = queued_operations(
        db,
        company_id,
        [work_center.id],
        load_options=queue_row_load_options(),
    )
    return old_order, ids, refreshed
