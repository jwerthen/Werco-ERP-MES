"""Dispatch v2: the work-order PLAN -- its two order verbs and the work-order-first read.

Owner decisions (2026-11-04) this module implements:

1. WORK-ORDER ORDER WINS. The planner sets one shop-wide order of work orders
   (``WorkOrder.dispatch_rank``, migration 114); every machine's queue follows it
   (``dispatch_service.queue_order_by``); inside a planned work order the planner
   orders that work order's operations (``WorkOrderOperation.run_order``).
2. LIVE. Every save lands immediately; the floor picks it up on its next poll.

Both stay ADVISORY: nothing here decides whether an operation may start, clock in
or complete -- that remains ``operation_action_gates`` and the predecessor rule.

WRITE DISCIPLINE (shared by both verbs, spec 3a)
------------------------------------------------
lock (``dispatch_service.acquire_dispatch_order_lock``) -> compare-and-swap read as
COLUMN TUPLES -> notice "before" snapshot -> Core rank UPDATEs (only rows whose
value changes, ``version`` untouched, ``updated_at`` preserved) -> flush ->
``expire_all`` (safe: these paths hold no pending ORM writes) -> ONE audit row ->
notice seam (``dispatch_service.record_dispatch_change``) -> the caller commits.
The rank writes go BEFORE the audit row because the audit chain lock is global:
taking it first could deadlock against a completion holding a work-order row lock.

THE READ (spec 4a)
------------------
:func:`build_work_order_board` projects every LIVE work order (DRAFT included)
as a lane with ALL its operations. It is a pure read -- no reconcile, no promotion,
no nest sync, no audit, no events -- and its query count is fixed: it does not
grow with work orders or operations.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from fastapi import HTTPException
from sqlalchemy import case, func, or_, update
from sqlalchemy.orm import Session, contains_eager, joinedload

from app.models.time_entry import TimeEntry
from app.models.user import User
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.models.work_order_blocker import WorkOrderBlocker, WorkOrderBlockerStatus
from app.schemas.dispatch import (
    DispatchChangeKind,
    DispatchCrewMember,
    DispatchLaneOperation,
    DispatchWorkCenterOption,
    DispatchWorkOrderBoardResponse,
    DispatchWorkOrderLane,
)
from app.services import dispatch_service
from app.services.audit_service import AuditService
from app.services.material_tie_view import tie_views_for_operations
from app.services.wallboard_service import LABOR_ENTRY_TYPES, operator_display_name
from app.services.work_order_state_service import (
    TERMINAL_WO_STATUSES,
    is_laser_dispatch_work_order,
    operation_blocked_by_incomplete_predecessors,
    operation_target_quantity,
    work_order_allows_same_work_center,
)

# Unresolved blocker statuses -- the same set the wallboard's blocked rail counts.
_UNRESOLVED_BLOCKER_STATUSES = [WorkOrderBlockerStatus.OPEN.value, WorkOrderBlockerStatus.ACKNOWLEDGED.value]

ROUTING_SEQUENCE_DETAIL = "This work order runs in routing sequence."
NOT_IN_PLAN_DETAIL = "Add this work order to the plan first."
WORK_ORDER_CLOSED_DETAIL = "This work order is closed."
WORK_ORDER_ON_HOLD_DETAIL = "This work order is on hold. Clear the hold before ordering its operations."


@dataclass
class PlanWriteResult:
    """What a plan verb did, for the router.

    ``changed`` is False for a no-op save (nothing written, no audit row, no
    notice). ``changed_work_center_ids`` is what the notice seam reported, for the
    after-commit broadcast seam.
    """

    changed: bool
    changed_work_center_ids: List[int] = field(default_factory=list)


def _status_value(status) -> Optional[str]:
    if status is None:
        return None
    return status.value if hasattr(status, "value") else str(status)


def _is_live(is_deleted: bool, status) -> bool:
    """Python form of ``dispatch_service.live_work_order_criteria`` (NULL status is not live)."""
    return not is_deleted and status is not None and status not in TERMINAL_WO_STATUSES


def _check_id_list(ids: Sequence[int], label: str) -> List[int]:
    values = list(ids)
    if len(values) > dispatch_service.MAX_RUN_ORDER_IDS:
        raise HTTPException(
            status_code=400,
            detail=f"Too many {label} ids (max {dispatch_service.MAX_RUN_ORDER_IDS})",
        )
    seen: set = set()
    for item_id in values:
        if item_id in seen:
            raise HTTPException(status_code=400, detail=f"Duplicate {label} id: {item_id}")
        seen.add(item_id)
    return values


def work_order_rank_update_statements(company_id: int, to_rank: Mapping[int, int], to_clear: Sequence[int]) -> List:
    """The Core UPDATEs that write the plan. Exposed so a test can dialect-compile them.

    WHERE+CASE form, ONLY the rows whose rank changes. Deliberately NOT the ORM and
    NOT the bulk-by-primary-key ``execute(update(WorkOrder), [dicts])`` form: the
    latter puts ``version`` into its search keys, and an ORM write bumps the
    optimistic-lock ``version`` -- which would 409 every office edit form open on a
    re-ranked job. ``updated_at`` is set to ITSELF because a Core UPDATE still fires
    the column's Python ``onupdate``, and ``coalesce(actual_end, updated_at)`` is
    the OTD completion anchor and the Tier-3 yield anchor: a plan edit must not move
    a job's dates.
    """
    statements = []
    if to_rank:
        statements.append(
            update(WorkOrder)
            .where(WorkOrder.company_id == company_id, WorkOrder.id.in_(sorted(to_rank)))
            .values(
                dispatch_rank=case(dict(to_rank), value=WorkOrder.id),
                updated_at=WorkOrder.updated_at,
            )
            .execution_options(synchronize_session=False)
        )
    if to_clear:
        statements.append(
            update(WorkOrder)
            .where(WorkOrder.company_id == company_id, WorkOrder.id.in_(sorted(to_clear)))
            .values(dispatch_rank=None, updated_at=WorkOrder.updated_at)
            .execution_options(synchronize_session=False)
        )
    return statements


def operation_run_order_update_statement(company_id: int, to_write: Mapping[int, int]):
    """The Core UPDATE for the operation-order verb (same write form as above)."""
    return (
        update(WorkOrderOperation)
        .where(WorkOrderOperation.company_id == company_id, WorkOrderOperation.id.in_(sorted(to_write)))
        .values(
            run_order=case(dict(to_write), value=WorkOrderOperation.id),
            updated_at=WorkOrderOperation.updated_at,
        )
        .execution_options(synchronize_session=False)
    )


# --------------------------------------------------------------------------
# PUT /shop-floor/dispatch-board/work-order-order
# --------------------------------------------------------------------------


def set_work_order_order(
    db: Session,
    company_id: int,
    actor: User,
    audit: AuditService,
    work_order_ids: Sequence[int],
    expected_work_order_ids: Sequence[int],
) -> PlanWriteResult:
    """Rewrite the shop-wide work-order plan to exactly ``work_order_ids``.

    WHOLE-LIST AUTHORITY: the listed LIVE work orders get dense ranks 1..N in order,
    and EVERY other work order of the company that still carries a rank -- any
    status, terminal and soft-deleted included -- leaves the plan (rank NULL). A
    work order that LEAVES the plan also has its operations' ``run_order`` cleared,
    so its stale within-block order cannot compete in the unplanned tail.

    COMPARE-AND-SWAP: ``expected_work_order_ids`` is the plan the client rendered.
    Ids that are no longer live (completed, closed, cancelled or deleted since the
    render) are dropped from BOTH lists before comparing -- a job finishing is not a
    conflict -- and the remainder must equal the live plan in ``(rank, id)`` order,
    else 409 :data:`dispatch_service.DISPATCH_PLAN_CONFLICT_DETAIL`. An id that is
    not a work order of this company at all is a 400 naming it (a foreign id is
    indistinguishable from a missing one).

    A no-op save writes nothing: no rank UPDATE, no audit row, no notice.
    """
    dispatch_service.acquire_dispatch_order_lock(db, company_id)

    desired = _check_id_list(work_order_ids, "work order")
    expected = _check_id_list(expected_work_order_ids, "expected work order")
    submitted = sorted(set(desired) | set(expected))

    # CAS state as COLUMN TUPLES: every work order of the company that holds a rank
    # (any status -- whole-list authority needs them) plus every submitted id.
    rows = (
        db.query(
            WorkOrder.id,
            WorkOrder.work_order_number,
            WorkOrder.dispatch_rank,
            WorkOrder.status,
            WorkOrder.is_deleted,
        )
        .filter(
            WorkOrder.company_id == company_id,
            or_(WorkOrder.dispatch_rank.is_not(None), WorkOrder.id.in_(submitted)),
        )
        .all()
    )
    by_id = {row.id: row for row in rows}
    for work_order_id in desired + expected:
        if work_order_id not in by_id:
            raise HTTPException(status_code=400, detail=f"Unknown work order id: {work_order_id}")

    def live(work_order_id: int) -> bool:
        row = by_id[work_order_id]
        return _is_live(bool(row.is_deleted), row.status)

    held_rank = sorted((row for row in rows if row.dispatch_rank is not None), key=lambda r: (r.dispatch_rank, r.id))
    current_plan = [row.id for row in held_rank if live(row.id)]
    if [work_order_id for work_order_id in expected if live(work_order_id)] != current_plan:
        raise HTTPException(status_code=409, detail=dispatch_service.DISPATCH_PLAN_CONFLICT_DETAIL)

    new_plan = [work_order_id for work_order_id in desired if live(work_order_id)]
    new_rank = {work_order_id: rank for rank, work_order_id in enumerate(new_plan, start=1)}
    to_rank = {
        work_order_id: rank for work_order_id, rank in new_rank.items() if by_id[work_order_id].dispatch_rank != rank
    }
    to_clear = [row.id for row in held_rank if row.id not in new_rank]
    if not to_rank and not to_clear:
        return PlanWriteResult(changed=False)

    # Notice "before" snapshot, under the lock and before any write: the machines
    # holding queued work of a LIVE work order whose rank is about to change.
    changed_live = [wo_id for wo_id in list(to_rank) + to_clear if live(wo_id)]
    before = dispatch_service.snapshot_queue_orders(
        db,
        company_id,
        dispatch_service.queued_work_center_ids_for_work_orders(db, company_id, changed_live),
    )

    for statement in work_order_rank_update_statements(company_id, to_rank, to_clear):
        db.execute(statement)
    dispatch_service.clear_run_order_for_work_orders(db, company_id, to_clear)
    db.flush()
    # The Core UPDATEs bypassed the identity map; nothing on this path holds a
    # pending ORM write, so expiring is safe (and required before re-reading).
    db.expire_all()

    changed = [
        {
            "work_order_id": wo_id,
            "work_order_number": by_id[wo_id].work_order_number,
            "from": by_id[wo_id].dispatch_rank,
            "to": new_rank.get(wo_id),
        }
        for wo_id in [wo_id for wo_id in new_plan if wo_id in to_rank] + to_clear
    ]
    audit.log_update(
        resource_type="dispatch_board",
        resource_id=company_id,
        resource_identifier="work-order-order",
        # ``old`` is every work order that HELD a rank, in plan order -- terminal and
        # deleted ones included -- because whole-list authority clears those too and
        # the chain row must describe what actually changed. The stored ranks ride
        # along so a pure renumber (a gap left by a hard-deleted planned job, same
        # visible order) still differs old -> new: log_update suppresses a row whose
        # values do not change, and every rank write must leave a chain row.
        old_values={
            "work_order_ids": [row.id for row in held_rank],
            "dispatch_ranks": {str(row.id): row.dispatch_rank for row in held_rank},
        },
        new_values={
            "work_order_ids": new_plan,
            "dispatch_ranks": {str(wo_id): rank for wo_id, rank in new_rank.items()},
        },
        description=f"Set dispatch work-order plan ({len(new_plan)} planned)",
        extra_data={"changed": changed},
    )

    changed_work_center_ids = dispatch_service.record_dispatch_change(
        db, company_id, actor, before, DispatchChangeKind.WORK_ORDER_ORDER
    )
    return PlanWriteResult(changed=True, changed_work_center_ids=changed_work_center_ids)


# --------------------------------------------------------------------------
# PUT /shop-floor/dispatch-board/work-orders/{id}/operation-order
# --------------------------------------------------------------------------


def _lane_ordered_queue_rows(
    db: Session, company_id: int, work_order_id: int
) -> Tuple[List[dispatch_service.QueueOrderRow], List[dispatch_service.QueueOrderRow]]:
    """``(all rows at the work order's machines, this work order's rows in LANE order)``.

    Lane order for a pool/laser lane's queued operations is (machine code, position in
    that machine's live queue) -- the order :func:`build_work_order_board` renders.
    """
    work_center_ids = dispatch_service.queued_work_center_ids_for_work_orders(db, company_id, [work_order_id])
    if not work_center_ids:
        return [], []
    rows = dispatch_service.queue_order_rows(db, company_id, work_center_ids)
    codes = {
        wc_id: code
        for wc_id, code in db.query(WorkCenter.id, WorkCenter.code)
        .filter(WorkCenter.company_id == company_id, WorkCenter.id.in_(work_center_ids))
        .all()
    }
    index = {row.operation_id: position for position, row in enumerate(rows)}
    own = [row for row in rows if row.work_order_id == work_order_id]
    own.sort(key=lambda row: (codes.get(row.work_center_id) or "", row.work_center_id, index[row.operation_id]))
    return rows, own


def set_operation_order(
    db: Session,
    company_id: int,
    actor: User,
    audit: AuditService,
    work_order_id: int,
    operation_ids: Sequence[int],
    expected_operation_ids: Sequence[int],
) -> PlanWriteResult:
    """Set the order of ONE planned work order's queued operations at each machine.

    For each work center, this work order's queued (READY/IN_PROGRESS) operations
    there get ``run_order`` 1..k in the requested relative order -- their block
    order at that machine. NOTHING ELSE is touched: no column reset, so other work
    orders' rows, held operations and unplanned work keep their values.

    Refusals, before any write:

    * 404 -- not a live work order of this company (foreign or soft-deleted);
    * 409 -- the work order is terminal (:data:`WORK_ORDER_CLOSED_DETAIL`);
    * 409 -- the work order is ON_HOLD (:data:`WORK_ORDER_ON_HOLD_DETAIL`): a held work
      order's operations carry no effective rank, so ordering them would only push
      them up the unplanned tail of every machine while the hold stands;
    * 409 -- a sequenced routing (:data:`ROUTING_SEQUENCE_DETAIL`): the routing IS
      the order there, and re-sequencing a released routing is a different, gated
      verb that v1 does not ship;
    * 409 -- the work order is not in the plan (:data:`NOT_IN_PLAN_DETAIL`): an
      unplanned work order's ``run_order`` would compete in the unplanned tail of
      every machine it touches, which is not what "order this job's operations" means;
    * 400 -- an id that is not an operation of this work order, or a list that does
      not name every queued operation exactly once;
    * 409 -- compare-and-swap mismatch (same drop-the-departed rule as the plan verb:
      an operation of this work order that left the queue since the render is
      dropped from both lists).
    """
    dispatch_service.acquire_dispatch_order_lock(db, company_id)

    desired = _check_id_list(operation_ids, "operation")
    expected = _check_id_list(expected_operation_ids, "expected operation")

    work_order = (
        db.query(WorkOrder)
        .filter(
            WorkOrder.id == work_order_id,
            WorkOrder.company_id == company_id,
            WorkOrder.is_deleted == False,  # noqa: E712
        )
        .first()
    )
    if work_order is None:
        raise HTTPException(status_code=404, detail="Work order not found")
    if not _is_live(False, work_order.status):
        raise HTTPException(status_code=409, detail=WORK_ORDER_CLOSED_DETAIL)
    if work_order.status == WorkOrderStatus.ON_HOLD:
        raise HTTPException(status_code=409, detail=WORK_ORDER_ON_HOLD_DETAIL)
    if not is_laser_dispatch_work_order(work_order) and not work_order_allows_same_work_center(work_order):
        raise HTTPException(status_code=409, detail=ROUTING_SEQUENCE_DETAIL)
    if work_order.dispatch_rank is None:
        raise HTTPException(status_code=409, detail=NOT_IN_PLAN_DETAIL)
    work_order_number = work_order.work_order_number

    rows, own = _lane_ordered_queue_rows(db, company_id, work_order.id)
    current = [row.operation_id for row in own]
    current_set = set(current)

    outsiders = sorted({op_id for op_id in desired + expected if op_id not in current_set})
    if outsiders:
        mine = {
            row[0]
            for row in db.query(WorkOrderOperation.id)
            .filter(
                WorkOrderOperation.company_id == company_id,
                WorkOrderOperation.work_order_id == work_order.id,
                WorkOrderOperation.id.in_(outsiders),
            )
            .all()
        }
        for op_id in desired + expected:
            if op_id not in current_set and op_id not in mine:
                raise HTTPException(
                    status_code=400,
                    detail=f"Operation {op_id} is not an operation of work order {work_order_number}",
                )

    if [op_id for op_id in expected if op_id in current_set] != current:
        raise HTTPException(status_code=409, detail=dispatch_service.DISPATCH_PLAN_CONFLICT_DETAIL)

    wanted = [op_id for op_id in desired if op_id in current_set]
    if sorted(wanted) != sorted(current):
        raise HTTPException(
            status_code=400,
            detail=(
                "operation_ids must list every ready or in-progress operation of work order "
                f"{work_order_number} exactly once"
            ),
        )

    row_by_id = {row.operation_id: row for row in own}
    next_rank: Dict[int, int] = {}
    new_run_order: Dict[int, int] = {}
    for op_id in wanted:
        wc_id = row_by_id[op_id].work_center_id
        next_rank[wc_id] = next_rank.get(wc_id, 0) + 1
        new_run_order[op_id] = next_rank[wc_id]
    to_write = {op_id: rank for op_id, rank in new_run_order.items() if row_by_id[op_id].run_order != rank}
    if not to_write:
        return PlanWriteResult(changed=False)

    # The resulting lane order (machine groups in lane order, the new order within
    # each), so old/new on the audit row compare like for like.
    wc_order: List[int] = []
    for row in own:
        if row.work_center_id not in wc_order:
            wc_order.append(row.work_center_id)
    new_lane_order = [op_id for wc_id in wc_order for op_id in wanted if row_by_id[op_id].work_center_id == wc_id]

    before: dispatch_service.QueueSnapshot = {wc_id: [] for wc_id in wc_order}
    for row in rows:
        before.setdefault(row.work_center_id, []).append(row.operation_id)

    db.execute(operation_run_order_update_statement(company_id, to_write))
    db.flush()
    db.expire_all()

    audit.log_update(
        resource_type="work_order",
        resource_id=work_order_id,
        resource_identifier=work_order_number,
        # The stored values ride along with the visible order so a pure renumber
        # (same order, sparse values made dense) still differs old -> new and is not
        # suppressed by log_update: every write leaves a chain row.
        old_values={
            "dispatch_operation_order": current,
            "run_order": {str(op_id): row_by_id[op_id].run_order for op_id in current},
        },
        new_values={
            "dispatch_operation_order": new_lane_order,
            "run_order": {str(op_id): new_run_order[op_id] for op_id in current},
        },
        description="Set dispatch operation order",
    )

    changed_work_center_ids = dispatch_service.record_dispatch_change(
        db, company_id, actor, before, DispatchChangeKind.OPERATION_ORDER
    )
    return PlanWriteResult(changed=True, changed_work_center_ids=changed_work_center_ids)


# --------------------------------------------------------------------------
# GET /shop-floor/dispatch-board/work-orders -- the lanes
# --------------------------------------------------------------------------


def _lane_mode(work_order: WorkOrder) -> str:
    if is_laser_dispatch_work_order(work_order):
        return "laser"
    # THE single reader of ``sequential_operations`` (081).
    return "pool" if work_order_allows_same_work_center(work_order) else "routing"


def _promise_date(work_order: WorkOrder) -> Optional[date]:
    return work_order.must_ship_by or work_order.due_date


def _unranked_lane_key(work_order: WorkOrder) -> Tuple:
    promise = _promise_date(work_order)
    return (
        work_order.priority if work_order.priority is not None else 99,
        # Portable NULLS LAST: (False, date) before (True, min).
        promise is None,
        promise or date.min,
        work_order.work_order_number or "",
        work_order.id,
    )


def _pool_lane_key(
    operation: WorkOrderOperation, wc_by_id: Mapping[int, WorkCenter], queue_index: Mapping[int, int]
) -> Tuple:
    """Pool/laser lane order: machine (by code), then the live queue's own order there.

    Queued operations first within a machine, in the position the shared queue sort
    gives them (never re-sorted here); off-queue operations after, by routing step.
    The operation-order verb's compare-and-swap uses the same order.
    """
    wc = wc_by_id.get(operation.work_center_id)
    queued = operation.id in queue_index
    return (
        wc.code if wc else "",
        operation.work_center_id or 0,
        0 if queued else 1,
        queue_index.get(operation.id, 0),
        operation.sequence,
        operation.id,
    )


def _crew_by_operation(db: Session, company_id: int) -> Dict[int, List[DispatchCrewMember]]:
    """Every open LABOR time entry of the company, bucketed by operation -- ONE query.

    Company-wide rather than an IN list over every lane operation: bounded by
    headcount, not by the size of the plan. Same LABOR set as the kiosk roster, so
    an open break/downtime row never renders as crew.
    """
    rows = (
        db.query(TimeEntry.operation_id, TimeEntry.user_id, User.first_name, User.last_name)
        # Onclause pinned: time_entries -> users has two FK paths (approved_by).
        .join(User, TimeEntry.user_id == User.id)
        .filter(
            TimeEntry.company_id == company_id,
            TimeEntry.clock_out.is_(None),
            TimeEntry.entry_type.in_(LABOR_ENTRY_TYPES),
            TimeEntry.operation_id.is_not(None),
        )
        .order_by(TimeEntry.clock_in.asc(), TimeEntry.id.asc())
        .all()
    )
    crew: Dict[int, List[DispatchCrewMember]] = {}
    for operation_id, user_id, first_name, last_name in rows:
        members = crew.setdefault(operation_id, [])
        if any(member.user_id == user_id for member in members):
            continue
        members.append(DispatchCrewMember(user_id=user_id, display_name=operator_display_name(first_name, last_name)))
    return crew


def _open_blockers_by_work_order(
    db: Session, company_id: int, work_order_ids: Optional[Sequence[int]]
) -> Dict[int, int]:
    query = (
        db.query(WorkOrderBlocker.work_order_id, func.count(WorkOrderBlocker.id))
        .join(WorkOrder, WorkOrderBlocker.work_order_id == WorkOrder.id)
        .filter(
            WorkOrderBlocker.company_id == company_id,
            WorkOrderBlocker.status.in_(_UNRESOLVED_BLOCKER_STATUSES),
            *dispatch_service.live_work_order_criteria(company_id),
        )
    )
    if work_order_ids is not None:
        query = query.filter(WorkOrder.id.in_(list(work_order_ids)))
    return {wo_id: int(count) for wo_id, count in query.group_by(WorkOrderBlocker.work_order_id).all()}


def _build_lanes(
    db: Session, company_id: int, work_order_ids: Optional[Sequence[int]] = None
) -> Tuple[List[DispatchWorkOrderLane], int, List[DispatchWorkCenterOption]]:
    """``(lanes in board order, ranked_count, work-center options)`` -- a fixed number of queries.

    1. live work orders (+ part); 2. their operations (+ nest, component part, and
    the cancelled-nest tombstone flag); 3. the plan positions; 4. the live queue as
    tuples (RUN positions and in-queue order); 5. the crew roster; 6. open blocker
    counts; 7. material ties (one batch, a bounded handful of SELECTs); 8. work
    centers. None of it grows with the number of work orders or operations.
    """
    wo_query = (
        db.query(WorkOrder)
        .options(joinedload(WorkOrder.part))
        .filter(*dispatch_service.live_work_order_criteria(company_id))
    )
    if work_order_ids is not None:
        wo_query = wo_query.filter(WorkOrder.id.in_(list(work_order_ids)))
    work_orders = wo_query.all()

    tombstone = dispatch_service.cancelled_nest_exists(company_id).label("cancelled_nest")
    op_query = (
        db.query(WorkOrderOperation, tombstone)
        # Onclause pinned -- two FK paths to work_orders since migration 080.
        .join(WorkOrder, WorkOrderOperation.work_order_id == WorkOrder.id)
        .options(
            contains_eager(WorkOrderOperation.work_order),
            joinedload(WorkOrderOperation.laser_nest),
            joinedload(WorkOrderOperation.component_part),
        )
        .filter(
            WorkOrderOperation.company_id == company_id,
            *dispatch_service.live_work_order_criteria(company_id),
        )
    )
    if work_order_ids is not None:
        op_query = op_query.filter(WorkOrder.id.in_(list(work_order_ids)))
    op_rows = op_query.order_by(
        WorkOrderOperation.work_order_id, WorkOrderOperation.sequence, WorkOrderOperation.id
    ).all()

    # Every operation (tombstones included) feeds the predecessor snapshot, exactly
    # as the SQL twin of the gate counts them; tombstones are then left off the lane.
    all_ops_by_wo: Dict[int, List[WorkOrderOperation]] = {}
    shown_ops_by_wo: Dict[int, List[WorkOrderOperation]] = {}
    for operation, is_tombstone in op_rows:
        all_ops_by_wo.setdefault(operation.work_order_id, []).append(operation)
        if not is_tombstone:
            shown_ops_by_wo.setdefault(operation.work_order_id, []).append(operation)

    plan_position = dispatch_service.plan_positions(db, company_id)

    queued_wc_ids = sorted(
        {
            op.work_center_id
            for ops in shown_ops_by_wo.values()
            for op in ops
            if op.status in dispatch_service.QUEUE_OPERATION_STATUSES and op.work_center_id is not None
        }
    )
    if work_order_ids is None:
        queue_rows = dispatch_service.queue_order_rows(db, company_id)
    elif queued_wc_ids:
        queue_rows = dispatch_service.queue_order_rows(db, company_id, queued_wc_ids)
    else:
        queue_rows = []
    run_position = dispatch_service.queue_row_positions(queue_rows)
    queue_index = {row.operation_id: position for position, row in enumerate(queue_rows)}

    crew = _crew_by_operation(db, company_id)
    blockers = _open_blockers_by_work_order(db, company_id, work_order_ids)

    tie_operation_ids = [
        op.id for ops in shown_ops_by_wo.values() for op in ops if op.status != OperationStatus.COMPLETE
    ]
    ties = tie_views_for_operations(db, company_id=company_id, operation_ids=tie_operation_ids)

    all_work_centers = db.query(WorkCenter).filter(WorkCenter.company_id == company_id).order_by(WorkCenter.code).all()
    wc_by_id = {wc.id: wc for wc in all_work_centers}

    lanes: List[Tuple[Tuple, DispatchWorkOrderLane]] = []
    referenced_wc_ids: set = set()
    for work_order in work_orders:
        all_ops = all_ops_by_wo.get(work_order.id, [])
        shown = shown_ops_by_wo.get(work_order.id, [])
        mode = _lane_mode(work_order)
        is_laser = mode == "laser"
        allow_same_wc = work_order_allows_same_work_center(work_order)
        incomplete = [op for op in all_ops if op.status != OperationStatus.COMPLETE]

        if mode == "routing":
            ordered = sorted(shown, key=lambda op: (op.sequence, op.id))
        else:
            ordered = sorted(shown, key=lambda op: _pool_lane_key(op, wc_by_id, queue_index))

        lane_ops: List[DispatchLaneOperation] = []
        any_short = False
        for op in ordered:
            wc = wc_by_id.get(op.work_center_id)
            if op.work_center_id is not None:
                referenced_wc_ids.add(op.work_center_id)
            op_ties = ties.get(op.id)
            tie_chip = dispatch_service.dispatch_material_tie(op_ties)
            if tie_chip is not None and tie_chip.any_short:
                any_short = True
            blocked = False
            if op.status == OperationStatus.PENDING and not is_laser:
                # THE shared predecessor rule (the promotion/clock-in gate), never a copy.
                blocked = operation_blocked_by_incomplete_predecessors(
                    op, incomplete, allow_same_work_center=allow_same_wc
                )
            lane_ops.append(
                DispatchLaneOperation(
                    operation_id=op.id,
                    version=op.version,
                    sequence=op.sequence,
                    operation_number=op.operation_number,
                    operation_name=op.name,
                    status=_status_value(op.status) or "",
                    work_center_id=op.work_center_id,
                    work_center_code=wc.code if wc else None,
                    run_position=run_position.get(op.id),
                    blocked_by_predecessor=blocked,
                    on_hold=op.status == OperationStatus.ON_HOLD,
                    crew=crew.get(op.id, []),
                    quantity_complete=float(op.quantity_complete or 0.0),
                    quantity_target=operation_target_quantity(op, work_order),
                    setup_time_hours=float(op.setup_time_hours or 0.0),
                    run_time_hours=float(op.run_time_hours or 0.0),
                    component_part_number=op.component_part.part_number if op.component_part else None,
                    laser_nest=dispatch_service.dispatch_nest_info(op),
                    material_tie=tie_chip,
                )
            )

        part = work_order.part
        position = plan_position.get(work_order.id)
        lane = DispatchWorkOrderLane(
            work_order_id=work_order.id,
            work_order_number=work_order.work_order_number,
            version=work_order.version,
            unit_number=work_order.unit_number,
            status=_status_value(work_order.status) or "",
            work_order_type=work_order.work_order_type,
            sequential_operations=not allow_same_wc,
            is_laser=is_laser,
            mode=mode,
            dispatch_position=position,
            priority=work_order.priority,
            due_date=work_order.due_date,
            must_ship_by=work_order.must_ship_by,
            promise_date=_promise_date(work_order),
            customer_name=work_order.customer_name,
            part_number=part.part_number if part else None,
            part_name=part.name if part else None,
            quantity_ordered=float(work_order.quantity_ordered or 0.0),
            quantity_complete=float(work_order.quantity_complete or 0.0),
            ops_total=len(shown),
            ops_complete=sum(1 for op in shown if op.status == OperationStatus.COMPLETE),
            open_blocker_count=blockers.get(work_order.id, 0),
            any_material_short=any_short,
            operations=lane_ops,
        )
        sort_key = (0, position, 0) if position is not None else (1, 0, _unranked_lane_key(work_order))
        lanes.append((sort_key, lane))

    lanes.sort(key=lambda item: item[0])
    ranked_count = sum(1 for key, _ in lanes if key[0] == 0)

    options = [
        DispatchWorkCenterOption(
            id=wc.id,
            code=wc.code,
            name=wc.name,
            work_center_type=wc.work_center_type,
            is_active=bool(wc.is_active),
            current_status=wc.current_status,
        )
        for wc in all_work_centers
        # Active machines are move targets; an inactive one is listed only so a
        # lane operation still sitting on it can be labelled.
        if wc.is_active or wc.id in referenced_wc_ids
    ]
    return [lane for _, lane in lanes], ranked_count, options


def build_work_order_board(db: Session, company_id: int) -> DispatchWorkOrderBoardResponse:
    """Every live work order as a lane: the plan first, in order, then the rest.

    Pure read -- shared with the plan verb's response, which commits, so nothing
    here may write.
    """
    lanes, ranked_count, options = _build_lanes(db, company_id)
    return DispatchWorkOrderBoardResponse(
        generated_at=datetime.utcnow(),
        work_orders=lanes,
        ranked_count=ranked_count,
        unranked_count=len(lanes) - ranked_count,
        work_centers=options,
    )


def build_work_order_lane(db: Session, company_id: int, work_order_id: int) -> DispatchWorkOrderLane:
    """One live work order's lane (the operation-order verb's response). 404 when not live."""
    lanes, _, _ = _build_lanes(db, company_id, [work_order_id])
    if not lanes:
        raise HTTPException(status_code=404, detail="Work order not found")
    return lanes[0]
