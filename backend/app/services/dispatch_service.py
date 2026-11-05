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

WORK-ORDER PLAN ORDER WINS (Dispatch v2, migration 114)
-------------------------------------------------------
``WorkOrder.dispatch_rank`` is the planner's shop-wide order of WORK ORDERS.
Owner decision (2026-11-04): work-order order wins -- every machine's queue
follows the plan, and ``run_order`` now orders operations WITHIN a planned work
order's block at a machine (and, as before, across the unplanned tail). There is
no per-machine override of the plan in v1.

The rank only counts where it is EFFECTIVE: on a queued (READY/IN_PROGRESS)
operation of a work order that is not ON_HOLD (:func:`effective_dispatch_rank_expr`
and its Python twin :func:`effective_dispatch_rank`). Without that gate
``GET /shop-floor/operations`` -- which also lists PENDING and ON_HOLD rows under
this same sort -- would float blocked work of planned jobs above runnable work,
and a held job would read "RUN 1" everywhere. ``run_order`` carries the same hold
gate (:func:`effective_run_order_expr` and its twin :func:`effective_run_order`):
a held work order's stored operation order is kept, but neither sorts nor labels
until the hold lifts.

Like ``run_order`` the plan is ADVISORY: it orders and labels, it never gates.
With every ``dispatch_rank`` NULL (every pre-114 row) the queue order is
byte-identical to what it was before the column existed.

WRITERS SERIALIZE ON ONE LOCK
-----------------------------
Every writer of a machine's queue ORDER -- the work-order plan verb, the
operation-order verb, the column rewrite and the four machine-move paths -- takes
:func:`acquire_dispatch_order_lock` first, before any read it compares against.
A transaction-scoped Postgres advisory lock, never ``SELECT ... FOR UPDATE`` on
work-order rows (that cannot see a newly ranked row and would contend with the
completion path's work-order writes).
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Mapping, NamedTuple, Optional, Sequence, Tuple, Union

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import and_, case, func, select, update
from sqlalchemy.orm import Query, Session, contains_eager, defaultload, joinedload
from sqlalchemy.orm.exc import StaleDataError

from app.db.locks import acquire_generator_lock
from app.models.laser_nest import LaserNest
from app.models.operational_event import OperationalEvent
from app.models.part import Part
from app.models.user import User
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.schemas.dispatch import (
    DispatchBoardColumn,
    DispatchChangeKind,
    DispatchMaterialTie,
    DispatchNestInfo,
    DispatchNotice,
    DispatchNoticeNextUp,
    DispatchQueueRow,
)
from app.services.laser_nest_service import active_laser_nest
from app.services.material_tie_view import MaterialTieView, tie_views_for_operations
from app.services.notification_outbox import _PENDING_KEY as NOTIFICATION_PENDING_KEY
from app.services.operational_event_service import OperationalEventService
from app.services.wallboard_service import operator_display_name
from app.services.work_order_state_service import TERMINAL_WO_STATUSES, operation_target_quantity

logger = logging.getLogger(__name__)

# Operation statuses that count as "on the machine's queue" -- work an operator
# could pick up right now. PENDING (predecessors outstanding) and COMPLETE /
# ON_HOLD are deliberately excluded.
QUEUE_OPERATION_STATUSES = (OperationStatus.READY, OperationStatus.IN_PROGRESS)

# The HELD set, and it is NOT the queue set. Held work is deliberately absent
# from QUEUE_OPERATION_STATUSES above and must stay absent: a hold means stop,
# so an ON_HOLD operation is not capacity, is not ranked by ``run_order``, never
# enters :func:`display_positions`, and never counts toward the wallboard's
# queued_count. The two tuples must never be concatenated or widened into each
# other -- if you are here to "just add ON_HOLD to the queue", that is the
# change this comment exists to refuse.
#
# What held work IS: something an operator has to be able to SEE. A hold placed
# by accident used to make the operation vanish from every shop-floor screen,
# recoverable only from a desk (``ShopFloorSimple`` is the app's only caller of
# resume), so the kiosk had a one-way door on its own terminal.
# :func:`held_operations` is the bounded, read-only feed that closes it; its one
# consumer is the ``held`` list on ``GET /shop-floor/work-center-queue/{id}``.
HELD_OPERATION_STATUSES = (OperationStatus.ON_HOLD,)

# Upper bound on the held list for one work-center-queue read. The kiosk polls
# that read every 10-15s per station, shop-wide, so the held feed has to be
# bounded like everything else on it. Real queues hold 0-2 operations at a time;
# the cap exists so a pathological tenant cannot turn a poll into an unbounded
# scan, not to hide routine work. Most-recently-held first, so the job an
# operator just lost is always the first row -- the cap can only ever drop the
# oldest holds, never the one being looked for.
MAX_HELD_OPERATIONS = 25

# Upper bound on a single run-order payload, so a runaway client cannot submit
# an unbounded list. Far above any real work center's live queue depth.
MAX_RUN_ORDER_IDS = 500

# The 409 body for a stale-write conflict on the rewrite. Shared so the service
# and the endpoint cannot drift from each other (or from docs/API.md).
RUN_ORDER_CONFLICT_DETAIL = "The queue changed while you were reordering. Refresh the dispatch board and try again."

# The 409 body for EVERY dispatch compare-and-swap mismatch (the work-order plan
# verb, the operation-order verb, and the column rewrite when the client sends
# ``expected_operation_ids``). One string so the board can recognise it and the
# docs can quote it.
DISPATCH_PLAN_CONFLICT_DETAIL = (
    "The dispatch board changed while you were editing. It has been refreshed \u2014 please try again."
)

# Namespace of the advisory lock every dispatch-order writer takes (see the
# module docstring). One constant so no writer can lock a different key.
DISPATCH_ORDER_LOCK_NAMESPACE = "dispatch_order"


def acquire_dispatch_order_lock(db: Session, company_id: int) -> None:
    """Serialize every writer of a machine's queue ORDER within one company.

    Transaction-scoped (released at commit/rollback) and a no-op on SQLite, via
    ``app.db.locks.acquire_generator_lock``. Callers take it BEFORE any read they
    compare against (the CAS state, the notice "before" snapshot), so two planners
    -- or a planner and a machine move -- can never interleave a read and a write.
    """
    acquire_generator_lock(db, DISPATCH_ORDER_LOCK_NAMESPACE, company_id)


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

    ``work_order`` itself is NOT given a loader strategy here: the shared queue
    queries populate it from the join they already make (``contains_eager``, see
    :func:`queued_operations_query`), and a second strategy on the same path is an
    ``InvalidRequestError``. ``defaultload`` only reaches THROUGH it to the part.
    """
    return (
        defaultload(WorkOrderOperation.work_order).joinedload(WorkOrder.part),
        joinedload(WorkOrderOperation.laser_nest),
        # The component part, for the LIVE component_part_number on each card.
        # Without it, every assembly card costs its own SELECT on every board poll.
        joinedload(WorkOrderOperation.component_part),
    )


def effective_dispatch_rank_expr():
    """SQL: the work order's plan rank AS IT COUNTS FOR THIS OPERATION ROW, else NULL.

    ``WorkOrder.dispatch_rank`` only when the operation is QUEUED (READY /
    IN_PROGRESS) AND its work order is not ON_HOLD. A PENDING or held operation of
    a planned work order -- and every operation of a held work order -- carries no
    effective rank, so it sorts in the unplanned tail instead of floating above
    runnable work, and it never reads "RUN 1".

    THE SQL TWIN of :func:`effective_dispatch_rank`. The two MUST stay identical
    (including NULL handling: a NULL operation or work-order status yields NULL on
    both sides). Edit them together; ``tests/api/test_dispatch_effective_rank.py``
    holds them to each other over the full status matrix.

    Requires ``WorkOrder`` to be joined into the query.
    """
    return case(
        (
            and_(
                WorkOrderOperation.status.in_(QUEUE_OPERATION_STATUSES),
                WorkOrder.status != WorkOrderStatus.ON_HOLD,
            ),
            WorkOrder.dispatch_rank,
        ),
        else_=None,
    )


def effective_dispatch_rank(operation: WorkOrderOperation, work_order: Optional[WorkOrder] = None) -> Optional[int]:
    """Python twin of :func:`effective_dispatch_rank_expr` -- see it for the rule.

    ``work_order`` defaults to ``operation.work_order``; the shared queue queries
    populate that relationship from their own join, so this costs no query on
    their rows. Must stay byte-for-byte the same rule as the SQL.
    """
    wo = work_order if work_order is not None else operation.work_order
    if wo is None:
        return None
    if operation.status is None or operation.status not in QUEUE_OPERATION_STATUSES:
        return None
    # ``status != ON_HOLD`` in SQL is NULL (not true) for a NULL status, so the
    # CASE falls through to NULL there too.
    if wo.status is None or wo.status == WorkOrderStatus.ON_HOLD:
        return None
    return wo.dispatch_rank


def effective_run_order_expr():
    """SQL: the operation's stored ``run_order`` AS IT COUNTS FOR ORDER AND LABEL, else NULL.

    The stored ``run_order`` only while the work order is not ON_HOLD. A held work
    order's operations sort and label exactly like never-ranked work -- no RUN chip,
    no float to the head of the unplanned tail -- the same gate
    :func:`effective_dispatch_rank_expr` puts on the plan rank. Holding a work order
    leaves its operations READY and touches neither value, so without this gate the
    planner's operation order (or a machine-column rank) kept a held job reading
    "RUN 1" ahead of runnable work.

    READ-SIDE ONLY: the stored value is never modified by a hold, so lifting the hold
    restores the planner's order and its RUN chips with no write. Unlike the plan rank
    this does NOT gate on the operation's status -- a PENDING or ON_HOLD operation that
    still carries a stored rank orders on ``/shop-floor/operations`` exactly as it did
    before Dispatch v2; only the work order's hold is new here.

    THE SQL TWIN of :func:`effective_run_order`. The two MUST stay identical
    (including NULL handling: a NULL work-order status yields NULL on both sides, as
    ``status != 'ON_HOLD'`` is NULL, not true, in SQL). Edit them together;
    ``tests/api/test_dispatch_effective_rank.py`` holds them to each other over the
    full status matrix.

    Requires ``WorkOrder`` to be joined into the query.
    """
    return case(
        (WorkOrder.status != WorkOrderStatus.ON_HOLD, WorkOrderOperation.run_order),
        else_=None,
    )


def effective_run_order(operation: WorkOrderOperation, work_order: Optional[WorkOrder] = None) -> Optional[int]:
    """Python twin of :func:`effective_run_order_expr` -- see it for the rule.

    ``work_order`` defaults to ``operation.work_order``; the shared queue queries
    populate that relationship from their own join, so this costs no query on their
    rows. Must stay byte-for-byte the same rule as the SQL.
    """
    wo = work_order if work_order is not None else operation.work_order
    if wo is None:
        return None
    if wo.status is None or wo.status == WorkOrderStatus.ON_HOLD:
        return None
    return operation.run_order


def queue_order_by() -> List:
    """The canonical dispatch-queue sort, as a list of ORDER BY expressions.

    1. The work order's EFFECTIVE plan rank (:func:`effective_dispatch_rank_expr`)
       ascending with NULLs LAST -- the planner's work-order order wins.
    2. Tiebreak inside a rank value: the work order id, so two work orders that
       transiently share a rank (a restore, a mid-rewrite read) can never
       interleave their operations. A constant NULL for unplanned rows, so the
       unplanned region is untouched by this key.
    3. The EFFECTIVE ``run_order`` (:func:`effective_run_order_expr`) ascending with
       NULLs LAST: within a planned work order's block at this machine, the
       planner's operation order; across the unplanned tail, the machine order a
       manager dictated (today's behavior there). A held work order's stored
       ``run_order`` does not count, exactly as its plan rank does not.
    4. The repo's canonical fallback for unranked work: ``WorkOrder.priority``,
       ``WorkOrder.due_date``, ``WorkOrderOperation.sequence``.
    5. ``WorkOrderOperation.id`` as a final deterministic tiebreak, so two rows
       that tie on everything above still come back in a stable order.

    Every NULLS-LAST key is an explicit ``IS NULL`` boolean (see the module
    docstring). With every ``dispatch_rank`` NULL, keys 1 and 2 are constant and,
    when no held work order carries a stored ``run_order``, the order is
    byte-identical to the pre-114 sort.

    Requires ``WorkOrder`` to be joined into the query.
    """
    effective_rank = effective_dispatch_rank_expr()
    effective_run = effective_run_order_expr()
    return [
        effective_rank.is_(None).asc(),
        effective_rank.asc(),
        case((effective_rank.is_(None), None), else_=WorkOrderOperation.work_order_id).asc(),
        effective_run.is_(None).asc(),
        effective_run.asc(),
        WorkOrder.priority.asc(),
        WorkOrder.due_date.asc(),
        WorkOrderOperation.sequence.asc(),
        WorkOrderOperation.id.asc(),
    ]


def _queue_criteria(company_id: int) -> List:
    """THE live-queue filter set, shared by the entity query and the tuple query.

    Tenancy from the joined ``WorkOrder.company_id`` (never client input); parent
    WO not soft-deleted and not terminal; operation READY/IN_PROGRESS.
    """
    return [
        WorkOrder.company_id == company_id,
        WorkOrder.is_deleted == False,  # noqa: E712
        WorkOrder.status.not_in(TERMINAL_WO_STATUSES),
        WorkOrderOperation.status.in_(QUEUE_OPERATION_STATUSES),
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

    ``operation.work_order`` is POPULATED FROM THE JOIN (``contains_eager``):
    :func:`display_positions` reads the work order's plan rank on every row, and
    the positions callers (``/shop-floor/operations``, ``/scheduling/work-orders``)
    pass no load options -- a lazy load there would be one SELECT per queued row.
    Callers that want deeper relationships reach through it with ``defaultload``
    (see :func:`queue_row_load_options`), never a second strategy on the path.
    """
    query = db.query(WorkOrderOperation)
    # Onclause pinned on purpose: work_order_operations and work_orders carry TWO
    # FK paths since migration 080 restored fk_work_orders_current_operation
    # (work_orders.current_operation_id), so an implicit ``join(WorkOrder)`` can
    # no longer infer one and raises AmbiguousForeignKeysError.
    query = query.join(WorkOrder, WorkOrderOperation.work_order_id == WorkOrder.id).options(
        contains_eager(WorkOrderOperation.work_order)
    )
    if load_options:
        query = query.options(*load_options)
    query = query.filter(and_(*_queue_criteria(company_id)))
    if work_center_ids is not None:
        query = query.filter(WorkOrderOperation.work_center_id.in_(list(work_center_ids)))
    return query.order_by(*queue_order_by())


class QueueOrderRow(NamedTuple):
    """One live-queue row as plain column values, in canonical queue order.

    The CAS reads, the notice snapshot and the lanes' RUN positions read the queue
    through this rather than as entities: column tuples cannot be served stale by
    the identity map after a Core UPDATE, and they cost no hydration.

    ``run_order`` is the STORED value (the operation-order verb compares against it
    to write only changed rows, and audits it); ``effective_run_order`` is what the
    sort and the RUN positions read (:func:`effective_run_order_expr`). They differ
    only on a held work order's rows.
    """

    operation_id: int
    work_center_id: int
    work_order_id: int
    work_order_number: str
    effective_rank: Optional[int]
    run_order: Optional[int]
    status: OperationStatus
    effective_run_order: Optional[int]


def queue_order_rows(
    db: Session, company_id: int, work_center_ids: Optional[Sequence[int]] = None
) -> List[QueueOrderRow]:
    """The live queue as :class:`QueueOrderRow` tuples, SAME filter and SAME sort.

    The column-tuple twin of :func:`queued_operations_query` -- one filter set
    (:func:`_queue_criteria`) and one sort (:func:`queue_order_by`), so it can
    never disagree with what the kiosk shows. Rows of several work centers come
    back interleaved; bucket them per work center (stable) before reading
    positions -- the sort keys never involve the work center.
    """
    query = db.query(
        WorkOrderOperation.id,
        WorkOrderOperation.work_center_id,
        WorkOrderOperation.work_order_id,
        WorkOrder.work_order_number,
        effective_dispatch_rank_expr(),
        WorkOrderOperation.run_order,
        WorkOrderOperation.status,
        effective_run_order_expr(),
    ).join(WorkOrder, WorkOrderOperation.work_order_id == WorkOrder.id)
    query = query.filter(and_(*_queue_criteria(company_id)))
    if work_center_ids is not None:
        query = query.filter(WorkOrderOperation.work_center_id.in_(list(work_center_ids)))
    return [QueueOrderRow(*row) for row in query.order_by(*queue_order_by()).all()]


def queued_operations(
    db: Session,
    company_id: int,
    work_center_ids: Optional[Sequence[int]] = None,
    *,
    load_options: Sequence = (),
) -> List[WorkOrderOperation]:
    """Materialize :func:`queued_operations_query` (already in queue order)."""
    return queued_operations_query(db, company_id, work_center_ids=work_center_ids, load_options=load_options).all()


def cancelled_nest_exists(company_id: int, operation_id_expr=None):
    """SQL predicate: this operation is a CANCELLED-nest tombstone, not held work.

    ``ON_HOLD`` is overloaded. ``laser_nest_service.soft_delete_laser_nest`` uses
    it as the tombstone for a CANCELLED nest, because ``OperationStatus`` has no
    operation-level CANCELLED and the hold status is what drops the row off the
    queue. The nest itself is never hard-deleted -- it is soft-deleted precisely
    so traceability and the package's run history survive -- and the child work
    order's ``quantity_ordered`` is recomputed to exclude it on the way out.

    So resuming one is not "lifting a hold": it undoes a soft delete from the
    front end. The operation would go back to READY/IN_PROGRESS, re-enter
    :func:`queued_operations_query` (which has no nest filter) and reach the
    dispatch board and the kiosk as a laser operation with ``laser_nest: null``,
    no CNC file, and a quantity its parent no longer counts. Invariant 3 breach
    with an invariant 5 consequence.

    THIS PREDICATE IS THE GUARD, AND IT BELONGS ON THE WRITE. It started life
    inline in :func:`held_operations_query`, where it protected one read while
    ``PUT /shop-floor/operations/{id}/resume`` -- reachable from ``ShopFloorSimple``
    and now from both kiosks -- checked only ``status != ON_HOLD``. Named and
    shared so every caller inherits the same rule:

    * :func:`held_operations_query` -- keep the tombstone off the kiosk's held list.
    * ``shop_floor.resume_operation`` -- REFUSE (409) rather than resurrect.
    * ``shop_floor.get_all_operations`` -- keep it off the desktop list that
      offers the Resume button in the first place.

    ``operation_id_expr`` defaults to ``WorkOrderOperation.id``, which makes the
    subquery CORRELATED for use inside a query over operations. Pass a literal id
    for a standalone check (see :func:`operation_has_cancelled_nest`). Tenant
    scope is the caller's active company on both sides of the join, so a
    cross-tenant nest can neither hide nor condemn another company's operation.
    """
    if operation_id_expr is None:
        operation_id_expr = WorkOrderOperation.id
    return (
        select(LaserNest.id)
        .where(
            LaserNest.work_order_operation_id == operation_id_expr,
            LaserNest.company_id == company_id,
            LaserNest.is_deleted == True,  # noqa: E712
        )
        .exists()
    )


def operation_has_cancelled_nest(db: Session, company_id: int, operation_id: int) -> bool:
    """Scalar form of :func:`cancelled_nest_exists`, for the WRITE paths.

    One boolean round trip, tenant-scoped. Deliberately queried rather than read
    off ``operation.laser_nest``: that relationship carries no ``is_deleted``
    filter and no company filter, so a caller that already has the operation
    loaded could just as easily read a stale or mis-parented row through it.
    """
    return bool(db.query(cancelled_nest_exists(company_id, operation_id)).scalar())


def held_operations_query(
    db: Session,
    company_id: int,
    *,
    work_center_ids: Optional[Sequence[int]] = None,
    load_options: Sequence = (),
) -> Query:
    """ON_HOLD operations at one or more work centers -- the SEE-IT feed, not the queue.

    Same tenancy and same live-work-order filters as
    :func:`queued_operations_query` (company from the joined ``WorkOrder``, never
    client input; parent WO not terminal and not soft-deleted), and one
    deliberate difference: it selects ``HELD_OPERATION_STATUSES`` instead of
    ``QUEUE_OPERATION_STATUSES``. Nothing else in this module may call it. It
    must never feed the dispatch board, the run-order rewrite,
    :func:`display_positions`, capacity, or the wallboard -- held work is not
    queued work, and the whole point of surfacing it is that it is STOPPED.

    THE CANCELLED-NEST CARVE-OUT. An operation carrying a SOFT-DELETED laser
    nest is a tombstone, not held work, and is excluded here via
    :func:`cancelled_nest_exists` -- the SAME predicate the resume WRITE and the
    desktop operations list now apply, so this read is no longer the only place
    the rule holds. A live nest on a genuinely held operation is untouched and
    still surfaces.

    Sorted most-recently-updated first so the operation an operator just held is
    the first row. ``updated_at`` is nullable, and ``ORDER BY col DESC`` alone is
    not deterministic across dialects for NULLs (Postgres sorts them first on
    DESC, SQLite last), so the sort leads with an explicit ``IS NULL`` boolean
    key -- the same portability trick :func:`queue_order_by` uses.
    """
    query = db.query(WorkOrderOperation)
    # A cancelled (soft-deleted) nest's backing operation is a tombstone, not a
    # hold -- see the docstring. Correlated against the outer operation row.
    cancelled_nest = cancelled_nest_exists(company_id)
    # Onclause pinned for the same reason as queued_operations_query: two FK
    # paths exist between the two tables since migration 080. ``work_order`` is
    # populated from this join exactly as in queued_operations_query, because the
    # kiosk hands BOTH queries one load-option tuple (which therefore reaches the
    # work order's part with ``defaultload``, not ``joinedload``).
    query = query.join(WorkOrder, WorkOrderOperation.work_order_id == WorkOrder.id).options(
        contains_eager(WorkOrderOperation.work_order)
    )
    if load_options:
        query = query.options(*load_options)
    query = query.filter(
        and_(
            WorkOrder.company_id == company_id,
            WorkOrder.is_deleted == False,  # noqa: E712
            WorkOrder.status.not_in(TERMINAL_WO_STATUSES),
            WorkOrderOperation.status.in_(HELD_OPERATION_STATUSES),
            ~cancelled_nest,
        )
    )
    if work_center_ids is not None:
        query = query.filter(WorkOrderOperation.work_center_id.in_(list(work_center_ids)))
    return query.order_by(
        WorkOrderOperation.updated_at.is_(None).asc(),
        WorkOrderOperation.updated_at.desc(),
        WorkOrderOperation.id.desc(),
    )


def held_operations(
    db: Session,
    company_id: int,
    work_center_ids: Optional[Sequence[int]] = None,
    *,
    load_options: Sequence = (),
    limit: Optional[int] = None,
) -> Tuple[List[WorkOrderOperation], bool]:
    """Materialize :func:`held_operations_query`, capped. Returns ``(rows, truncated)``.

    Fetches ``limit + 1`` rows so truncation is a fact rather than a guess: the
    caller can tell the operator "showing the 25 most recent holds" instead of
    silently dropping the 26th.

    ``limit`` defaults to :data:`MAX_HELD_OPERATIONS` resolved at CALL time, not
    bound into the signature -- a default evaluated at import would freeze the
    cap past any later reassignment of the constant.
    """
    effective_limit = MAX_HELD_OPERATIONS if limit is None else limit
    rows = held_operations_query(db, company_id, work_center_ids=work_center_ids, load_options=load_options).limit(
        effective_limit + 1
    )
    materialized = rows.all()
    if len(materialized) > effective_limit:
        return materialized[:effective_limit], True
    return materialized, False


def _dense_positions(rows: Iterable[Tuple[int, bool]]) -> Dict[int, Optional[int]]:
    """``(operation_id, is_ranked)`` pairs, in queue order -> gap-free 1..N, None for unranked."""
    positions: Dict[int, Optional[int]] = {}
    position = 0
    for operation_id, is_ranked in rows:
        if not is_ranked:
            positions[operation_id] = None
            continue
        position += 1
        positions[operation_id] = position
    return positions


def display_positions(operations: Iterable[WorkOrderOperation]) -> Dict[int, Optional[int]]:
    """Map operation id -> the rank the SHOP should see, or None when unranked.

    The stored ``run_order`` is dense 1..N only at the moment a manager writes
    it. It goes sparse in normal use -- completing a job or moving it to another
    work center takes its rank out of the column, leaving e.g. 1, 2, 4 behind.
    Showing those raw values to an operator reads as "job 3 is missing", so the
    number on screen is the position within the (already ordered) queue instead:
    always 1..N with no gaps, whatever the stored values drifted to.

    A row is RANKED (gets "RUN n") when its work order's EFFECTIVE plan rank is set
    (:func:`effective_dispatch_rank`) OR it carries an EFFECTIVE ``run_order``
    (:func:`effective_run_order` -- a held work order's stored rank does not count).
    Under the queue sort those rows are always a PREFIX of the queue -- planned
    work-order blocks first, then the manager-ordered part of the unplanned tail --
    so the numbering stays gap-free. Unranked rows map to None and stay chip-less at the
    tail. Duplicate stored ranks (transient, and allowed by design -- no unique
    constraint) resolve deterministically here too, because position follows the
    query's total ordering rather than the raw value.

    Reads ``operation.work_order``: the shared queue queries populate it from their
    join, so this issues no query on their rows.

    ``operations`` MUST already be in queue order (see :func:`queue_order_by`) and
    belong to ONE work center.
    """
    return _dense_positions(
        (
            operation.id,
            effective_dispatch_rank(operation) is not None or effective_run_order(operation) is not None,
        )
        for operation in operations
    )


def queue_row_positions(rows: Iterable[QueueOrderRow]) -> Dict[int, Optional[int]]:
    """:func:`display_positions` for :class:`QueueOrderRow` tuples -- the same rule.

    Rows may span several work centers (as :func:`queue_order_rows` returns them);
    they are bucketed per work center here, stably, so each machine is numbered
    over its own queue.
    """
    by_work_center: Dict[int, List[QueueOrderRow]] = {}
    for row in rows:
        by_work_center.setdefault(row.work_center_id, []).append(row)
    positions: Dict[int, Optional[int]] = {}
    for bucket in by_work_center.values():
        positions.update(
            _dense_positions(
                (row.operation_id, row.effective_rank is not None or row.effective_run_order is not None)
                for row in bucket
            )
        )
    return positions


def live_work_order_criteria(company_id: int) -> List:
    """A work order that can be PLANNED: same company, not deleted, not terminal.

    DRAFT is live (and rankable -- its operations simply stay PENDING; Release is
    still the step that authorizes production). ``not_in`` excludes a NULL status,
    exactly as :func:`queued_operations_query` does.
    """
    return [
        WorkOrder.company_id == company_id,
        WorkOrder.is_deleted == False,  # noqa: E712
        WorkOrder.status.not_in(TERMINAL_WO_STATUSES),
    ]


def plan_positions(db: Session, company_id: int) -> Dict[int, int]:
    """``{work_order_id: plan position}``: gap-free 1..N over every LIVE ranked work order.

    Ordered by ``(dispatch_rank, id)`` -- the same order the queue sort and the
    plan verb's compare-and-swap use -- so a stored rank that went sparse (a planned
    job completed) still numbers 1..N with no gaps. One column-tuple query.
    """
    rows = (
        db.query(WorkOrder.id)
        .filter(*live_work_order_criteria(company_id), WorkOrder.dispatch_rank.is_not(None))
        .order_by(WorkOrder.dispatch_rank.asc(), WorkOrder.id.asc())
        .all()
    )
    return {row[0]: position for position, row in enumerate(rows, start=1)}


def material_tie_info(
    db: Session, company_id: int, operations: Iterable[WorkOrderOperation]
) -> Dict[int, List[MaterialTieView]]:
    """Open, operation-scoped material ties for a whole column/board, in ONE batch.

    The board's cost has to stay flat in the number of cards, so this is called
    ONCE per response with every operation on it -- never per column and never
    per row (modelled on ``process_sheet_service.step_counts_for_operations``).
    Untied operations get no key at all, so :func:`board_column` renders nothing
    for them.

    Read-only, like everything else the board projects: this map feeds both the
    GET board and the response of the run-order PUT, and that PUT commits.

    Tenancy comes from the caller's ACTIVE company (``get_current_company_id``
    for a user, the station's DB row for a kiosk), never from client input.
    """
    return tie_views_for_operations(db, company_id=company_id, operation_ids=[op.id for op in operations])


def dispatch_material_tie(ties: Optional[Sequence[MaterialTieView]]) -> Optional[DispatchMaterialTie]:
    """Project an operation's ties onto the single board chip, or None when untied.

    ONE chip per card. A second tie on the same operation is possible (nothing
    forbids tying two different parts to one operation) and the card has room
    for one, so the FIRST is shown -- ``tie_views_for_operations`` orders by
    allocation id, making "first" deterministic and identical to what the kiosk
    lists first for the same operation. Summing them is NOT an option: they are
    different parts with different units, and a merged number would be fiction.

    Showing only the first would make a SHORTAGE on a second tie invisible, so
    the shortage signal is carried for the whole operation separately:
    ``tie_count`` says how many there are and ``any_short`` is true when ANY of
    them is short. The chip names the first part; the tone and the column's
    "N short" rollup read ``any_short``, so a second tie can never go unflagged
    just because the card had room for one line.

    Takes an already-materialized list; it must never issue a query (see
    :func:`dispatch_queue_row`).
    """
    if not ties:
        return None
    tie = ties[0]
    return DispatchMaterialTie(
        tie_count=len(ties),
        any_short=any(t.short_by > 0 for t in ties),
        allocation_id=tie.allocation_id,
        part_id=tie.part_id,
        part_number=tie.part_number,
        unit_of_measure=tie.unit_of_measure,
        qty_per_run=tie.qty_per_run,
        qty_planned=tie.qty_planned,
        qty_consumed=tie.qty_consumed,
        qty_remaining=tie.qty_remaining,
        on_hand=tie.on_hand,
        short_by=tie.short_by,
        pinned_inventory_item_id=tie.pinned_inventory_item_id,
        pinned_lot_number=tie.pinned_lot_number,
    )


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


def dispatch_queue_row(
    operation: WorkOrderOperation,
    display_position: Optional[int] = None,
    *,
    ties: Optional[Sequence[MaterialTieView]] = None,
    plan_position_by_work_order: Optional[Mapping[int, int]] = None,
) -> DispatchQueueRow:
    """Project one queued operation onto the board/kiosk row shape.

    ``display_position`` is the gap-free rank from :func:`display_positions`;
    callers that have the whole ordered queue should pass it. Falling back to
    the EFFECTIVE stored rank (:func:`effective_run_order`, never the raw column)
    keeps single-row callers correct-ish rather than blank -- and keeps an
    unranked row's ``None`` from :func:`display_positions` from being replaced by
    a held work order's stored rank, which would put a RUN chip back on held work.

    ``ties`` is this operation's already-fetched material ties (from
    :func:`material_tie_info`), passed IN rather than looked up here.

    ``plan_position_by_work_order`` is :func:`plan_positions`, passed IN for the
    same reason. The row's ``dispatch_position`` is the work order's plan position
    only when this row's EFFECTIVE rank is set -- a held work order's row sorts in
    the unplanned tail and must not carry a plan number it is not sorted by.

    THIS FUNCTION MUST STAY DB-FREE. It runs once per card across the whole
    shop's queue and is asserted to issue ZERO SELECTs
    (``tests/api/test_dispatch_nest_details.py::test_nest_is_eager_loaded_by_the_shared_load_options``);
    a lazy relationship access or a per-row query here is an N+1 multiplied by
    the board. Never give it a ``Session``.
    """
    work_order = operation.work_order
    part = work_order.part if work_order else None
    status = operation.status
    dispatch_position = None
    if plan_position_by_work_order and effective_dispatch_rank(operation, work_order) is not None:
        dispatch_position = plan_position_by_work_order.get(operation.work_order_id)
    work_order_status = work_order.status if work_order else None
    return DispatchQueueRow(
        operation_id=operation.id,
        run_order=display_position if display_position is not None else effective_run_order(operation, work_order),
        version=operation.version,
        work_order_id=operation.work_order_id,
        work_order_number=work_order.work_order_number if work_order else "",
        unit_number=work_order.unit_number if work_order else None,
        operation_number=operation.operation_number,
        operation_name=operation.name,
        part_number=part.part_number if part else None,
        part_name=part.name if part else None,
        # Live, never the snapshot baked into operation_name. Costs no query --
        # component_part is in queue_load_options below.
        component_part_number=(operation.component_part.part_number if operation.component_part else None),
        component_part_name=(operation.component_part.name if operation.component_part else None),
        status=status.value if hasattr(status, "value") else str(status),
        priority=work_order.priority if work_order else None,
        due_date=work_order.due_date if work_order else None,
        quantity_ordered=operation_target_quantity(operation, work_order),
        quantity_complete=float(operation.quantity_complete or 0.0),
        setup_time_hours=float(operation.setup_time_hours or 0.0),
        run_time_hours=float(operation.run_time_hours or 0.0),
        laser_nest=dispatch_nest_info(operation),
        material_tie=dispatch_material_tie(ties),
        dispatch_position=dispatch_position,
        sequence=operation.sequence,
        work_order_status=(work_order_status.value if hasattr(work_order_status, "value") else work_order_status),
    )


def board_column(
    work_center: WorkCenter,
    operations: Iterable[WorkOrderOperation],
    *,
    tie_info: Optional[Dict[int, List[MaterialTieView]]] = None,
    plan_position_by_work_order: Optional[Mapping[int, int]] = None,
) -> DispatchBoardColumn:
    """One board column: a work center plus its (already-ordered) queue rows.

    ``is_active`` is read straight off the work center: a deactivated work
    center that still holds queued work renders as a flagged read-only column
    (see :func:`inactive_work_centers_with_work`).

    ``tie_info`` is the batched material-tie map (:func:`material_tie_info`).
    EVERY caller that builds a column a client will render must pass it --
    including the run-order PUT, whose response REPLACES the whole column on the
    board: omitting it there would strip every tie chip on a drag-reorder and
    read as "reordering untied my material". Omitting it is a no-tie column, not
    an error, so nothing fails loudly; that is why this note is here.

    ``plan_position_by_work_order`` (:func:`plan_positions_for`) feeds each row's
    ``dispatch_position`` -- the work-order bands the board draws. The same rule
    applies: every caller whose column a client renders passes it.
    """
    ordered = list(operations)
    positions = display_positions(ordered)
    ties = tie_info or {}
    queue = [
        dispatch_queue_row(
            op,
            positions.get(op.id),
            ties=ties.get(op.id),
            plan_position_by_work_order=plan_position_by_work_order,
        )
        for op in ordered
    ]
    return DispatchBoardColumn(
        id=work_center.id,
        code=work_center.code,
        name=work_center.name,
        work_center_type=work_center.work_center_type,
        current_status=work_center.current_status,
        is_active=bool(work_center.is_active),
        queue=queue,
        queued_hours=round(sum(row.setup_time_hours + row.run_time_hours for row in queue), 4),
    )


def plan_positions_for(db: Session, company_id: int, operations: Iterable[WorkOrderOperation]) -> Dict[int, int]:
    """:func:`plan_positions`, but only paid for when some row is actually planned.

    A board on which no operation carries an effective plan rank needs no plan
    positions, so it issues no query -- which keeps a shop that has never used the
    plan at exactly its pre-114 query cost. ``operations`` must have
    ``work_order`` loaded (the shared queue queries do).
    """
    if any(effective_dispatch_rank(op) is not None for op in operations):
        return plan_positions(db, company_id)
    return {}


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


def inactive_work_centers_with_work(db: Session, company_id: int) -> List[WorkCenter]:
    """Deactivated work centers that still hold queued work, code-ordered.

    A work center deactivated while operations were still queued must not
    vanish from the board: the kiosk keeps serving that queue (deliberately --
    operators finish stranded work there), so the board renders it as a flagged
    read-only column (``is_active=false``) until the queue drains. Deactivation
    now REFUSES while live work references the machine (see
    ``work_centers._live_work_refusal``), so this surfaces pre-guard strays and
    any state that slips past the guard, rather than being the normal path.

    The subquery mirrors :func:`queued_operations_query`'s filter set --
    READY/IN_PROGRESS operations on non-deleted, non-terminal work orders --
    WITHOUT touching that shared query: its work-center seam stays the caller's
    id list, and this helper decides only WHICH inactive work centers matter.
    """
    queued_wc_ids = (
        select(WorkOrderOperation.work_center_id)
        # Onclause pinned -- two FK paths to work_orders since migration 080.
        .join(WorkOrder, WorkOrderOperation.work_order_id == WorkOrder.id)
        .where(
            and_(
                WorkOrder.company_id == company_id,
                WorkOrder.is_deleted == False,  # noqa: E712
                WorkOrder.status.not_in(TERMINAL_WO_STATUSES),
                WorkOrderOperation.status.in_(QUEUE_OPERATION_STATUSES),
            )
        )
        .distinct()
    )
    return (
        db.query(WorkCenter)
        .filter(
            WorkCenter.company_id == company_id,
            # isnot(True) rather than == False: the column is nullable, and a
            # legacy NULL row must surface flagged here rather than vanish from
            # both board halves (the update endpoint drops explicit nulls, so
            # NULL is only ever pre-existing data).
            WorkCenter.is_active.isnot(True),
            WorkCenter.id.in_(queued_wc_ids),
        )
        .order_by(WorkCenter.code)
        .all()
    )


def build_dispatch_board(db: Session, company_id: int) -> Tuple[List[DispatchBoardColumn], datetime]:
    """Every board-worthy work center with its live queue, in ONE operations query.

    Columns are every ACTIVE work center (empty queue included, so a manager can
    drag work onto an idle machine) PLUS any DEACTIVATED work center that still
    has queued work (:func:`inactive_work_centers_with_work`), flagged
    ``is_active=false`` for read-only rendering. One list, code-ordered --
    the same stable contract as before; the client decides prominence.

    Avoids N+1: the operations for ALL columns are fetched once and bucketed in
    Python, preserving the query's canonical order within each bucket, and every
    relationship the row builder touches is eager-loaded via
    :func:`queue_row_load_options`. The board's cost must stay flat in the number
    of cards -- a per-row SELECT here is multiplied by the whole shop's queue.
    Material ties are batched the same way (:func:`material_tie_info`): they add
    a small FIXED number of queries to the response, never one per card.

    Read-only. This projection is shared with ``PUT .../run-order``, which
    COMMITS -- so anything that writes here would be silently persisted by a
    manager's drag-reorder.
    """
    work_centers = sorted(
        active_work_centers(db, company_id) + inactive_work_centers_with_work(db, company_id),
        key=lambda wc: wc.code,
    )
    by_work_center: Dict[int, List[WorkOrderOperation]] = {wc.id: [] for wc in work_centers}
    tie_info: Dict[int, List[MaterialTieView]] = {}
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
        # ONE tie batch for the WHOLE board, not one per column: material ties
        # are a bounded per-page cost (a handful of SELECTs regardless of card
        # count), and the flat-in-cards property above is what makes the board
        # usable on a 24-machine shop.
        tie_info = material_tie_info(db, company_id, operations)
        # Plan positions for the work-order bands: ONE query for the whole board,
        # and none at all when nothing on it is planned.
        plan_position_by_work_order = plan_positions_for(db, company_id, operations)
    else:
        plan_position_by_work_order = {}
    columns = [
        board_column(
            wc,
            by_work_center[wc.id],
            tie_info=tie_info,
            plan_position_by_work_order=plan_position_by_work_order,
        )
        for wc in work_centers
    ]
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

    ``updated_at`` IS PRESERVED (Dispatch v2): a Core UPDATE still fires the
    column's Python ``onupdate``, so each statement sets ``updated_at`` to itself.
    A display rank is not a change to the operation -- bumping it moved the held
    list's most-recently-held order (:func:`held_operations_query`) and the period
    rollups that anchor on ``updated_at`` (reports, the Tier-3 yield anchor).

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
    db.execute(
        reset.values(run_order=None, updated_at=WorkOrderOperation.updated_at).execution_options(
            synchronize_session=False
        )
    )

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
        .values(run_order=ranks, updated_at=WorkOrderOperation.updated_at)
        .execution_options(synchronize_session=False)
    )


def _reject_duplicates(ids: Sequence[int], label: str) -> None:
    seen: set = set()
    for item_id in ids:
        if item_id in seen:
            raise HTTPException(status_code=400, detail=f"Duplicate {label} id: {item_id}")
        seen.add(item_id)


def _precedence_key(row: QueueOrderRow) -> Tuple[int, int, int]:
    """Sortable form of the queue's first three keys: plan rank (NULLS LAST), then WO id."""
    if row.effective_rank is None:
        return (1, 0, 0)
    return (0, int(row.effective_rank), int(row.work_order_id))


def assert_respects_work_order_precedence_or_http(ids: Sequence[int], rows_by_id: Mapping[int, QueueOrderRow]) -> None:
    """409 when a submitted machine order contradicts the work-order plan.

    Work-order order wins (owner decision, 2026-11-04): an operation of a work order
    later in the plan -- or not in the plan at all -- may not be placed ahead of an
    operation of an earlier planned work order. The submitted ids must therefore be
    non-decreasing in ``(effective rank IS NULL, effective rank, work order id)``,
    exactly the queue sort's leading keys. Without this the server would silently
    re-sort the column and the response would not be what the planner submitted.

    HELD WORK ORDERS. The key reads only the EFFECTIVE plan rank, which is already
    NULL for a held work order's rows, so they belong to the unplanned region here
    and are accepted anywhere in it -- deliberately not refused. Their stored
    ``run_order`` is ineffective while the hold stands (:func:`effective_run_order`),
    so the shared sort puts them among the never-ranked rows by priority, and the
    rendered column can therefore show a held row ABOVE a never-ranked runnable one:
    refusing "held before runnable" would make the column the board just rendered
    unsaveable. What the planner submits for a held row is stored as usual and takes
    effect when the hold lifts; until then the response lists it in the unranked part
    of the tail, the one place the response may differ from the submission.
    """
    previous: Optional[QueueOrderRow] = None
    for operation_id in ids:
        row = rows_by_id[operation_id]
        if previous is not None and _precedence_key(row) < _precedence_key(previous):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Operation {previous.operation_id} ({previous.work_order_number}) cannot run before operation "
                    f"{row.operation_id} ({row.work_order_number}): work orders run in plan order. "
                    "Reorder the work orders to change this."
                ),
            )
        previous = row


def apply_run_order_or_http(
    db: Session,
    company_id: int,
    work_center: WorkCenter,
    operation_ids: Sequence[int],
    expected_operation_ids: Optional[Sequence[int]] = None,
) -> Tuple[List[int], List[int], List[WorkOrderOperation]]:
    """Rewrite a work center's manual run order to exactly ``operation_ids``.

    ADVISORY ONLY -- this changes queue *presentation*, never start eligibility.

    Contract:
    - every id must be a LIVE QUEUED operation at this work center (the same
      filter set the kiosk queue uses); anything else is a 400 naming the id,
      because it means the manager's board is stale and should be refreshed;
    - duplicate ids are a 400 (an ambiguous rank request);
    - the order must respect the work-order plan (409 naming the first offending
      pair, :func:`assert_respects_work_order_precedence_or_http`);
    - the listed ids get dense ranks ``1..N`` in the given order, and every
      OTHER operation in the column is set back to unranked (NULL) so the
      column ends up exactly as submitted, with no leftover drift;
    - an empty list is valid and clears the whole column.

    COMPARE-AND-SWAP (optional). ``expected_operation_ids`` is the column as the
    client rendered it. When given, it is compared with the LIVE queue (not with
    :func:`ranked_operation_ids`, which includes off-queue rows the client never
    sees) after ids that have since left the queue are dropped from both sides --
    a job completing between render and save is not a conflict. Any remaining
    difference is a 409 (:data:`DISPATCH_PLAN_CONFLICT_DETAIL`). With CAS on, a
    submitted id that was on the rendered column but has since left the queue is
    dropped rather than refused; any other non-live id is still a 400. An expected
    id that is not an operation of this company at all is a 400. Absent = the
    pre-v2 contract, unchanged.

    A REWRITE IS AUTHORITATIVE FOR THE WHOLE COLUMN, NOT JUST ITS LIVE ROWS.
    The submitted ids must be live queued operations (a manager can only rank
    what is on the board), but the unranking half reaches EVERY operation at the
    work center whatever its status -- otherwise an ON_HOLD row would keep a
    stale rank and silently outrank, on resume, jobs the manager ranked after it.

    Writes through Core ``update()`` (see :func:`_write_run_order`): the
    optimistic-lock ``version`` of the ranked rows is intentionally NOT bumped,
    because a display-only reorder must never invalidate an operator's in-flight
    edit on a running job.

    The caller holds :func:`acquire_dispatch_order_lock`. Flushes (does not
    commit) so the caller can commit the change and its audit row atomically, in
    one transaction. Transient duplicate ranks mid-rewrite are harmless --
    ``run_order`` carries no unique constraint by design.

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

    # The live column as COLUMN TUPLES in queue order: the CAS state, the
    # validation set and the precedence keys, all from one read.
    rows = queue_order_rows(db, company_id, [work_center.id])
    rows_by_id = {row.operation_id: row for row in rows}

    if expected_operation_ids is not None:
        expected = list(expected_operation_ids)
        if len(expected) > MAX_RUN_ORDER_IDS:
            raise HTTPException(
                status_code=400,
                detail=f"Too many operations in expected run order (max {MAX_RUN_ORDER_IDS})",
            )
        _reject_duplicates(expected, "expected operation")
        dead_expected = [operation_id for operation_id in expected if operation_id not in rows_by_id]
        if dead_expected:
            known = {
                row[0]
                for row in db.query(WorkOrderOperation.id)
                .filter(WorkOrderOperation.company_id == company_id, WorkOrderOperation.id.in_(dead_expected))
                .all()
            }
            for operation_id in dead_expected:
                if operation_id not in known:
                    raise HTTPException(status_code=400, detail=f"Unknown operation id: {operation_id}")
        live_expected = [operation_id for operation_id in expected if operation_id in rows_by_id]
        if live_expected != [row.operation_id for row in rows]:
            raise HTTPException(status_code=409, detail=DISPATCH_PLAN_CONFLICT_DETAIL)
        rendered = set(expected)
        # Rendered on this column but gone since: not a conflict, just drop it.
        ids = [operation_id for operation_id in ids if operation_id in rows_by_id or operation_id not in rendered]

    for operation_id in ids:
        if operation_id not in rows_by_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Operation {operation_id} is not a live queued operation at work center "
                    f"{work_center.code}. Refresh the dispatch board and try again."
                ),
            )

    assert_respects_work_order_precedence_or_http(ids, rows_by_id)

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


def clear_run_order_for_work_orders(db: Session, company_id: int, work_order_ids: Sequence[int]) -> None:
    """Unrank (``run_order = NULL``) every operation of these work orders. Core UPDATE.

    Used when a work order LEAVES the plan (the plan verb, a restore): its
    operations' ``run_order`` values were its within-block order, and left behind
    they would compete in the unplanned tail -- where ``run_order`` still sorts
    ahead of the priority fallback -- as if a manager had ranked them there.

    Same write form as :func:`_write_run_order`: no ``version`` bump, ``updated_at``
    preserved, only rows that actually carry a rank, tenant-scoped.
    """
    if not work_order_ids:
        return
    db.execute(
        update(WorkOrderOperation)
        .where(
            WorkOrderOperation.company_id == company_id,
            WorkOrderOperation.work_order_id.in_(list(work_order_ids)),
            WorkOrderOperation.run_order.is_not(None),
        )
        .values(run_order=None, updated_at=WorkOrderOperation.updated_at)
        .execution_options(synchronize_session=False)
    )


# --------------------------------------------------------------------------
# Floor notices (spec 3g / 4c): "the planner changed this machine's order".
#
# Every dispatch-order writer takes a "before" snapshot under the dispatch lock,
# writes, flushes, writes its audit row, then calls :func:`record_dispatch_change`
# BEFORE commit, and :func:`broadcast_dispatch_changes` AFTER it. The notice is an
# OperationalEvent per work center whose ordered queue actually changed: the
# kiosk / shop-floor polls read the latest one back (:func:`latest_dispatch_notices`)
# and the notification outbox fans it out to the operators clocked in there
# (catalog ``dispatch.changed``). Telemetry, not audit: the audit row is the
# record of the change, and a notice failure never fails the write.
# --------------------------------------------------------------------------

QueueSnapshot = Dict[int, List[int]]
ChangeKindArg = Union[DispatchChangeKind, Mapping[int, DispatchChangeKind]]

DISPATCH_NOTICE_EVENT_TYPE = "dispatch_order_changed"
DISPATCH_NOTICE_SOURCE_MODULE = "dispatch"
DISPATCH_NOTICE_ENTITY_TYPE = "work_center"
# How far back a floor read looks for a notice. A shift and a half: long enough
# that the operator who starts the next shift still sees a change made late in
# the last one, short enough that the probe stays a narrow index range scan.
DISPATCH_NOTICE_WINDOW_HOURS = 12
# Cap on the ``moved_up`` ids one notice carries (a whole-plan rewrite can move
# every card at a machine; the banner only needs to badge the first few).
MAX_NOTICE_MOVED_UP = 10
# How many of a machine's newest in-window notices one floor read looks at. The
# newest row that builds a valid notice wins; a malformed newer row is skipped (and
# logged) instead of failing the poll. A fixed depth keeps the read ONE bounded
# query however many machines are asked for.
DISPATCH_NOTICE_READ_DEPTH = 5


def snapshot_queue_orders(db: Session, company_id: int, work_center_ids: Iterable[Optional[int]]) -> QueueSnapshot:
    """``{work_center_id: [operation_id, ...]}`` -- each machine's live queue, in order.

    ONE bucketed :func:`queue_order_rows` query however many work centers are
    asked for. Every requested work center gets a key, an empty queue included,
    so a before/after diff sees a queue that appeared or drained.

    Column tuples, never entities, and never ``expire_all()``: the machine-move
    paths call this AFTER flushing un-committed ORM writes (the move itself), and
    expiring would silently discard them. Flush first; this then sees the move.
    """
    wanted = sorted({int(wc_id) for wc_id in work_center_ids if wc_id is not None})
    snapshot: QueueSnapshot = {wc_id: [] for wc_id in wanted}
    if not wanted:
        return snapshot
    for row in queue_order_rows(db, company_id, wanted):
        snapshot[row.work_center_id].append(row.operation_id)
    return snapshot


def queued_work_center_ids_for_work_orders(db: Session, company_id: int, work_order_ids: Sequence[int]) -> List[int]:
    """Work centers holding a QUEUED operation of any of these (live) work orders.

    The notice candidates for a plan change: only machines where one of the
    re-ranked work orders actually has queued work can have changed order.
    """
    if not work_order_ids:
        return []
    rows = (
        db.query(WorkOrderOperation.work_center_id)
        .join(WorkOrder, WorkOrderOperation.work_order_id == WorkOrder.id)
        .filter(
            and_(*_queue_criteria(company_id)),
            WorkOrderOperation.work_order_id.in_(list(work_order_ids)),
        )
        .distinct()
        .all()
    )
    return sorted(row[0] for row in rows if row[0] is not None)


def _kind_for(change_kind: ChangeKindArg, work_center_id: int) -> DispatchChangeKind:
    if isinstance(change_kind, DispatchChangeKind):
        return change_kind
    # A map names every machine a move touched; anything else is a column rewrite.
    return change_kind.get(work_center_id, DispatchChangeKind.MACHINE_ORDER)


def _moved_up(before: Sequence[int], after: Sequence[int]) -> List[int]:
    """Operations that moved AHEAD of work they used to follow, in queue order (capped).

    Positions are compared over the operations present in BOTH snapshots only, so an
    arrival or a departure on its own -- which shifts every card below it by one --
    marks nobody as "moved up": only a change in RELATIVE order does.
    """
    common = set(before) & set(after)
    before_position = {op_id: index for index, op_id in enumerate(op for op in before if op in common)}
    moved: List[int] = []
    for index, op_id in enumerate(op for op in after if op in common):
        if index < before_position[op_id]:
            moved.append(op_id)
            if len(moved) >= MAX_NOTICE_MOVED_UP:
                break
    return moved


def _next_up_id(queue: Sequence[int], statuses: Mapping[int, OperationStatus]) -> Optional[int]:
    """The job the queue says to run next: the first READY row, else the first row.

    The head of a queue is often work already running (IN_PROGRESS); "what do I run
    next" is the first job nobody has started.
    """
    for op_id in queue:
        if statuses.get(op_id) == OperationStatus.READY:
            return op_id
    return queue[0] if queue else None


def _dispatch_notice_payloads(
    db: Session,
    company_id: int,
    actor_user: Optional[User],
    before: QueueSnapshot,
    after: QueueSnapshot,
    change_kind: ChangeKindArg,
    changed: Sequence[int],
) -> Dict[int, dict]:
    """Build every changed machine's notice payload NOW -- two queries, never re-read later.

    One query for the machines' codes and one for the identity of every operation on
    either snapshot of a changed machine (status, numbers, names, part). The payload is
    frozen at write time on purpose: the notification dispatcher and the floor reads
    render it as written and never re-query, so a later edit cannot rewrite history.

    No key may contain a ``redact_event_payload`` substring (``token``, ``cui``, ...):
    ``tests/api/test_dispatch_notices.py`` holds the payload to that.
    """
    codes = {
        wc_id: code
        for wc_id, code in db.query(WorkCenter.id, WorkCenter.code)
        .filter(WorkCenter.company_id == company_id, WorkCenter.id.in_(list(changed)))
        .all()
    }
    op_ids = sorted({op_id for wc_id in changed for op_id in (*before.get(wc_id, []), *after.get(wc_id, []))})
    details: Dict[int, dict] = {}
    statuses: Dict[int, OperationStatus] = {}
    if op_ids:
        rows = (
            db.query(
                WorkOrderOperation.id,
                WorkOrderOperation.status,
                WorkOrderOperation.operation_number,
                WorkOrderOperation.name,
                WorkOrder.work_order_number,
                WorkOrder.unit_number,
                Part.part_number,
            )
            .join(WorkOrder, WorkOrderOperation.work_order_id == WorkOrder.id)
            .outerjoin(Part, and_(WorkOrder.part_id == Part.id, Part.company_id == company_id))
            .filter(
                WorkOrderOperation.company_id == company_id,
                WorkOrder.company_id == company_id,
                WorkOrderOperation.id.in_(op_ids),
            )
            .all()
        )
        for op_id, status, operation_number, name, wo_number, unit_number, part_number in rows:
            statuses[op_id] = status
            details[op_id] = {
                "operation_id": op_id,
                "work_order_number": wo_number,
                "unit_number": unit_number,
                "operation_number": operation_number,
                "operation_name": name,
                "part_number": part_number,
            }

    def next_up(queue: Sequence[int]) -> Optional[dict]:
        op_id = _next_up_id(queue, statuses)
        return details.get(op_id) if op_id is not None else None

    actor_display = operator_display_name(actor_user.first_name, actor_user.last_name) if actor_user else None
    payloads: Dict[int, dict] = {}
    for wc_id in changed:
        old, new = before.get(wc_id, []), after.get(wc_id, [])
        payloads[wc_id] = {
            "work_center_id": wc_id,
            "work_center_code": codes.get(wc_id),
            "change_kind": _kind_for(change_kind, wc_id).value,
            "actor_display": actor_display,
            "next_up_before": next_up(old),
            "next_up_after": next_up(new),
            "moved_up": _moved_up(old, new),
            "queue_length": len(new),
        }
    return payloads


def emit_dispatch_notices(
    db: Session,
    company_id: int,
    actor_user: Optional[User],
    before: QueueSnapshot,
    after: QueueSnapshot,
    change_kind: ChangeKindArg,
) -> List[int]:
    """One ``dispatch_order_changed`` event per machine whose ordered queue changed.

    Returns the changed work-center ids (sorted) -- empty for a no-op write, which
    emits nothing and issues no query.

    BEST-EFFORT, IN ONE SAVEPOINT. The payload reads and every emit run inside
    ``db.begin_nested()``; any failure -- a query, the event INSERT's flush, a bug --
    rolls back only the savepoint and returns ``[]``, so a notice can never 500 (or,
    via a poisoned outer transaction, silently undo) the reorder it describes. This is
    the residue ``OperationalEventService.emit_best_effort``'s docstring says the
    savepoint closes.

    THE OUTBOX LIST IS SHIELDED FROM THE SAVEPOINT, BOTH WAYS. SQLAlchemy fires the
    session's ``after_commit`` on a SAVEPOINT release and its rollback events on a
    savepoint rollback, and the notification outbox listens to exactly those
    (``services/notification_outbox.py``). Unshielded, releasing this savepoint would
    enqueue the notices' fan-out BEFORE the request commits -- the worker could load an
    event that is not committed yet (and lose it), or one whose transaction later rolls
    back -- and a rollback would drop ids the still-live outer transaction owns. So the
    pending list is lifted off the session across the release and put back after it
    (the outer commit enqueues it, as the outbox intends), and on failure the ids that
    were pending BEFORE the savepoint are restored.

    NEVER ``expire_all()``: the machine-move callers hold flushed-but-uncommitted ORM
    state. Callers flush before calling (:func:`record_dispatch_change` does), so
    ``begin_nested``'s own autoflush has nothing to send.

    The events are flushed, not committed: the caller's commit makes them durable
    together with the reorder (and only then does the outbox enqueue fan-out), and a
    rollback of the caller's transaction discards them -- a write that never happened
    never notifies.
    """
    changed = sorted(
        wc_id for wc_id in set(before) | set(after) if list(before.get(wc_id, [])) != list(after.get(wc_id, []))
    )
    if not changed:
        return []

    pending_before = list(db.info.get(NOTIFICATION_PENDING_KEY) or [])
    savepoint = db.begin_nested()
    try:
        payloads = _dispatch_notice_payloads(db, company_id, actor_user, before, after, change_kind, changed)
        events = OperationalEventService(db)
        for wc_id in changed:
            event = events.emit_best_effort(
                company_id=company_id,
                event_type=DISPATCH_NOTICE_EVENT_TYPE,
                source_module=DISPATCH_NOTICE_SOURCE_MODULE,
                entity_type=DISPATCH_NOTICE_ENTITY_TYPE,
                entity_id=wc_id,
                # Deliberately no work_order_id / operation_id: the notice is about a
                # MACHINE's queue (and work_order_id would hijack the notification link).
                user_id=actor_user.id if actor_user is not None else None,
                severity="info",
                event_payload=payloads[wc_id],
            )
            if event is None:
                raise RuntimeError(f"dispatch notice emit failed for work center {wc_id}")
        # Lift the outbox list off the session across the release: the release fires
        # ``after_commit``, which would enqueue these events before the real commit.
        pending_now = db.info.pop(NOTIFICATION_PENDING_KEY, None)
        try:
            savepoint.commit()
        finally:
            if pending_now:
                db.info[NOTIFICATION_PENDING_KEY] = pending_now
        return changed
    except Exception:  # noqa: BLE001 - a notice must never fail the reorder it describes
        logger.warning(
            "dispatch notice skipped (best-effort): company_id=%s work_centers=%s",
            company_id,
            changed,
            exc_info=True,
        )
        try:
            savepoint.rollback()
        except Exception:  # noqa: BLE001 - already failing; the outer transaction decides
            logger.warning("dispatch notice savepoint rollback failed", exc_info=True)
        if pending_before:
            db.info[NOTIFICATION_PENDING_KEY] = pending_before
        else:
            db.info.pop(NOTIFICATION_PENDING_KEY, None)
        return []


def record_dispatch_change(
    db: Session,
    company_id: int,
    actor_user: Optional[User],
    before: QueueSnapshot,
    change_kind: ChangeKindArg,
) -> List[int]:
    """Emit the floor notices for a dispatch-order write; return the machines that changed.

    Called by every dispatch-order writer AFTER its rank/move writes are flushed
    and its audit row is written, BEFORE commit, with the ``before`` snapshot
    (:func:`snapshot_queue_orders`) it took under :func:`acquire_dispatch_order_lock`
    before mutating anything. ``change_kind`` is one :class:`DispatchChangeKind`
    for the whole write, or a ``{work_center_id: kind}`` map (a machine move is
    ``moved_out`` at the old machine and ``moved_in`` at the new one).

    The "after" order is re-read over the SAME machines with the same column-tuple
    query -- never by re-sorting in Python (``due_date`` is nullable and sorts by
    dialect) -- and never through ``expire_all()`` (the move paths hold
    flushed-but-uncommitted ORM writes). The flush here is deliberately OUTSIDE the
    notice's best-effort guard: a real conflict in the caller's own pending writes
    (``StaleDataError``) must surface exactly as it would at commit, not be
    swallowed as a notice failure.

    Returns the work-center ids whose order actually changed (``[]`` when nothing
    did, or when the best-effort notice failed); the caller passes them to
    :func:`broadcast_dispatch_changes` AFTER its commit.
    """
    if not before:
        return []
    db.flush()
    after = snapshot_queue_orders(db, company_id, before.keys())
    return emit_dispatch_notices(db, company_id, actor_user, before, after, change_kind)


def broadcast_dispatch_changes(company_id: int, work_center_ids: Sequence[int]) -> None:
    """After-commit websocket nudge, one per changed work center. Best-effort.

    ``company_id`` is passed EXPLICITLY: ``broadcast_shop_floor_update`` without it
    fans out to every tenant's sockets. An accelerator only -- broadcasts reach the
    sockets of the one API process that handled the request, and crew-station
    tokens cannot open a socket at all -- so the floor's poll of the notice
    (:func:`latest_dispatch_notices`) is the delivery guarantee. Never raises.
    """
    if not work_center_ids:
        return
    # Imported here: the realtime layer is an API-process concern this service
    # module should not drag into every importer (the worker, scripts).
    from app.core.realtime import safe_broadcast
    from app.core.websocket import broadcast_shop_floor_update

    for wc_id in work_center_ids:
        safe_broadcast(
            broadcast_shop_floor_update,
            wc_id,
            {"type": DISPATCH_NOTICE_EVENT_TYPE, "event": DISPATCH_NOTICE_EVENT_TYPE},
            company_id=company_id,
        )


def _notice_next_up(value) -> Optional[DispatchNoticeNextUp]:
    if not isinstance(value, dict) or value.get("operation_id") is None:
        return None
    try:
        return DispatchNoticeNextUp.model_validate(value)
    except ValueError:
        return None


def _dispatch_notice_from_row(row) -> DispatchNotice:
    """Build one :class:`DispatchNotice` from a notice-query row. RAISES on a malformed row.

    Only the board writes these rows, but the reader must not depend on that: a payload
    that is not a JSON object, or whose values fail the schema (a non-string
    ``work_center_code``, say), raises ``TypeError`` / ``ValidationError`` here and the
    caller skips the row. Optional nested ``next_up_*`` values degrade to ``None``
    rather than condemn the whole notice, and ``moved_up`` keeps only plain integers,
    capped at :data:`MAX_NOTICE_MOVED_UP`.
    """
    payload = row.event_payload
    if not isinstance(payload, dict):
        raise TypeError("dispatch notice payload is not a JSON object")
    moved_up = payload.get("moved_up")
    moved_ids = (
        [op_id for op_id in moved_up if isinstance(op_id, int) and not isinstance(op_id, bool)]
        if isinstance(moved_up, list)
        else []
    )
    return DispatchNotice(
        id=row.id,
        occurred_at=row.occurred_at,
        work_center_id=row.work_center_id,
        work_center_code=payload.get("work_center_code"),
        change_kind=payload.get("change_kind"),
        actor_display=payload.get("actor_display"),
        next_up_before=_notice_next_up(payload.get("next_up_before")),
        next_up_after=_notice_next_up(payload.get("next_up_after")),
        moved_up=moved_ids[:MAX_NOTICE_MOVED_UP],
        updates_in_window=int(row.updates_in_window or 1),
    )


def latest_dispatch_notices(
    db: Session,
    company_id: int,
    work_center_ids: Iterable[Optional[int]],
    since_hours: int = DISPATCH_NOTICE_WINDOW_HOURS,
) -> Dict[int, dict]:
    """``{work_center_id: notice}`` -- the newest VALID dispatch notice per machine in the window.

    ONE query, whatever the number of machines: a window function ranks each machine's
    rows newest first (``occurred_at`` then ``id``, descending), keeps the top
    :data:`DISPATCH_NOTICE_READ_DEPTH` of them, and counts the machine's rows in the
    window (``updates_in_window``). It probes
    ``ix_operational_events_company_entity_time (company_id, entity_type, entity_id,
    occurred_at)`` (migration 114) -- a narrow range per machine, polled by every
    station every 10-15 s. The bounds are tz-aware UTC (``occurred_at`` is timestamptz).

    THE WINDOW IS BOUNDED ON BOTH SIDES: ``now - since_hours <= occurred_at <= now``. A
    row dated in the future is not "the newest notice forever" -- without the upper
    bound it would win every poll and pin the machine's banner until its date passed,
    hiding every real notice. ``updates_in_window`` counts only rows inside both bounds.

    ONE BAD ROW NEVER FAILS A FLOOR READ. This feeds the kiosk / crew-station queue,
    ``my-active-job`` (the clock-out flow) and the filtered ``/shop-floor/operations``,
    so a row whose payload does not build a :class:`DispatchNotice`
    (:func:`_dispatch_notice_from_row` raises ``ValidationError`` / ``TypeError`` /
    ``ValueError``) is skipped and logged at warning, and that machine falls back to
    its next-newest row in the window -- the first of the top
    :data:`DISPATCH_NOTICE_READ_DEPTH` that validates. If none does, the machine gets no
    key. The depth keeps the read a bounded, fixed single query however many machines
    are asked for. ``POST /operational-events/`` refuses this event type
    (``operational_events.SERVER_ONLY_EVENT_TYPES``), so only the board writes these
    rows; the reader is robust regardless of who wrote one.

    Tenant-scoped on ``company_id`` (never client input): a machine id from another
    company finds nothing. Machines with no notice in the window get no key.

    Each value is the :class:`DispatchNotice` wire shape (``model_dump(mode="json")``,
    so ``occurred_at`` is UTC ``Z``). Built ONLY from the event row and its frozen
    payload, through the schema -- so a payload key added later can never reach a crew
    station without a schema change (and the test that pins the station's keys).
    """
    wanted = sorted({int(wc_id) for wc_id in work_center_ids if wc_id is not None})
    if not wanted:
        return {}
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=since_hours)
    newest_first = (OperationalEvent.occurred_at.desc(), OperationalEvent.id.desc())
    ranked = (
        select(
            OperationalEvent.id.label("id"),
            OperationalEvent.entity_id.label("work_center_id"),
            OperationalEvent.occurred_at.label("occurred_at"),
            OperationalEvent.event_payload.label("event_payload"),
            func.row_number().over(partition_by=OperationalEvent.entity_id, order_by=newest_first).label("newest_rank"),
            func.count(OperationalEvent.id).over(partition_by=OperationalEvent.entity_id).label("updates_in_window"),
        )
        .where(
            OperationalEvent.company_id == company_id,
            OperationalEvent.entity_type == DISPATCH_NOTICE_ENTITY_TYPE,
            OperationalEvent.entity_id.in_(wanted),
            OperationalEvent.occurred_at >= since,
            OperationalEvent.occurred_at <= now,
            OperationalEvent.event_type == DISPATCH_NOTICE_EVENT_TYPE,
        )
        .subquery()
    )
    rows = db.execute(
        select(ranked)
        .where(ranked.c.newest_rank <= DISPATCH_NOTICE_READ_DEPTH)
        .order_by(ranked.c.work_center_id, ranked.c.newest_rank)
    ).all()
    notices: Dict[int, dict] = {}
    for row in rows:
        if row.work_center_id in notices:
            continue  # this machine already has its newest valid notice
        try:
            notice = _dispatch_notice_from_row(row)
        except (ValidationError, TypeError, ValueError) as exc:
            logger.warning(
                "dispatch notice row skipped (malformed payload): company_id=%s work_center_id=%s "
                "event_id=%s error=%s",
                company_id,
                row.work_center_id,
                row.id,
                type(exc).__name__,
            )
            continue
        notices[row.work_center_id] = notice.model_dump(mode="json")
    return notices


def notices_newest_first(notices: Mapping[int, dict]) -> List[dict]:
    """The values of :func:`latest_dispatch_notices` as a list, newest notice first."""
    return sorted(notices.values(), key=lambda notice: notice["id"], reverse=True)
