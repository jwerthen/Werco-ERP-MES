"""Changing a work order's routing after it is released (owner decision 2026-11-25).

"I need to be able to update routing for work orders once a work order releases. Sometimes
things change on the fly." Four verbs, behind ``app/api/endpoints/work_orders.py``:

* **skip** (``POST /work-orders/operations/{id}/skip``) -- a step is never deleted from a
  released job: it is marked ``SKIPPED`` with who / when / why and stays on the traveler. A
  skipped step is FINISHED for every predecessor gate and all-complete check
  (``work_order_state_service.FINISHED_OPERATION_STATUSES``) and produced nothing (it is out of
  ``active_work_order_operations``, so it is transparent to chain credit and the header).
* **unskip** (``POST /work-orders/operations/{id}/unskip``) -- back to PENDING; the un-worked
  READY steps it now blocks go back to PENDING through the one demotion core.
* **add** (``POST /work-orders/{id}/operations``, hardened) -- a new step on a released job.
* **resequence** (``POST /work-orders/{id}/operations/resequence``) -- reorder not-started steps.
  ``operation_number`` is NEVER rewritten: it is an identifier printed travelers and SPC matching
  key on (CLAUDE.md -> Operation numbers).

Rules every verb shares, decided here once:

* the work order is live (soft-deleted 404, terminal 409) and not a laser dispatch pool (409 --
  nests are managed by hold / the nest verbs, and the predecessor gates do not apply there);
* a change may never put an un-finished step IN FRONT OF work already recorded, under the work
  order's own gate (``operation_blocked_by_incomplete_predecessors`` with the resolver's
  ``allow_same_work_center`` -- the gate itself is asked, never a copy of it): that step would
  become an uncompletable predecessor of in-flight work, because every completion verb
  re-evaluates the gate at action time and no read path heals it;
* a change may never strand another operation's short-count accounting
  (``routing_change_strands`` -- chain credit flows by sequence);
* it promotes through ``promote_ready_operations`` (the one rule; this is a seam that calls it,
  not a copy) and demotes through ``demote_ready_operations`` (the one core);
* the reason a person typed is REQUIRED audit evidence on every change to a non-DRAFT work order
  (skip / unskip / resequence always), and a standing API token is refused 403
  (``API_TOKEN_ROUTING_CHANGE``) -- routing is a person's judgment.

The service flushes but never commits and writes the audit rows itself; the router wraps the
call in ``required_audit`` (503 + rollback when a row cannot be written) and commits.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Optional, Sequence

from fastapi import HTTPException
from sqlalchemy.orm import Query, Session
from sqlalchemy.orm import object_session as object_session_or_none

from app.core.credentials import API_TOKEN_ROUTING_CHANGE, is_api_token_credential
from app.db.tenant_filter import tenant_query
from app.models.user import User
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.services.audit_service import AuditService
from app.services.operation_action_gates import accounting_strand_message, operation_ids_with_recorded_work
from app.services.work_order_state_service import (
    TERMINAL_WO_STATUSES,
    active_operation_id,
    active_work_order_operations,
    demote_ready_operations,
    is_laser_dispatch_work_order,
    is_operation_finished,
    is_operation_skipped,
    log_demotions_required,
    operation_blocked_by_incomplete_predecessors,
    operation_what_if,
    promote_ready_operations,
    ready_operations_blocked_now,
    release_operation_schedule_reservation,
    routing_change_strands,
    work_order_allows_same_work_center,
)

# The required-audit 503 text every verb answers when its evidence cannot be written.
ROUTING_AUDIT_UNAVAILABLE = "The audit record for this routing change could not be saved; nothing was changed. Retry."
OPERATION_VERSION_CONFLICT = "Operation was modified by someone else. Refresh and try again."
SKIP_WOULD_COMPLETE_HELD_WORK_ORDER = (
    "Resume the work order first: it is on hold, and skipping this step would leave no step unfinished, which "
    "completes the work order (finished goods received) without the hold ever being lifted."
)
SKIPPABLE_STATUSES = (OperationStatus.PENDING, OperationStatus.READY)
REORDERABLE_STATUSES = (OperationStatus.PENDING, OperationStatus.READY)


@dataclass
class RoutingChangeOutcome:
    """What a verb wrote (flushed, audited, not committed)."""

    operation: Optional[WorkOrderOperation] = None
    promoted: list[WorkOrderOperation] = field(default_factory=list)
    demoted: list[WorkOrderOperation] = field(default_factory=list)
    changed: list[WorkOrderOperation] = field(default_factory=list)


def _status_value(status: Any) -> str:
    return str(getattr(status, "value", status))


def operation_label(operation: Any) -> str:
    """``Op 20`` for refusal texts: the bare identifier, prefixed once (never ``Op Op 20``)."""
    number = (getattr(operation, "operation_number", None) or "").strip()
    if number.lower().startswith("op"):
        return number
    return f"Op {number or getattr(operation, 'sequence', '?')}"


def refuse_api_token(user: User) -> None:
    if is_api_token_credential(user):
        raise HTTPException(status_code=403, detail=API_TOKEN_ROUTING_CHANGE)


def assert_routing_changeable(work_order: Optional[WorkOrder]) -> WorkOrder:
    """404 a missing / soft-deleted work order, 409 a terminal one or a laser dispatch pool."""
    if work_order is None or work_order.is_deleted:
        raise HTTPException(status_code=404, detail="Work order not found")
    if work_order.status in TERMINAL_WO_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"The routing of a {_status_value(work_order.status)} work order cannot be changed.",
        )
    if is_laser_dispatch_work_order(work_order):
        raise HTTPException(
            status_code=409,
            detail="Laser nest work orders are managed through their nests (hold, remove or re-import a nest), "
            "not by changing the routing.",
        )
    return work_order


def work_order_lock_query(db: Session, work_order_id: int, company_id: int) -> Query:
    """``SELECT ... FOR UPDATE`` of one tenant-scoped work order row (taken AFTER its operation rows)."""
    return (
        tenant_query(db, WorkOrder, company_id)
        .filter(WorkOrder.id == work_order_id)
        .populate_existing()
        .with_for_update()
    )


def operation_rows_lock_query(db: Session, work_order_id: int, company_id: int) -> Query:
    """``SELECT ... ORDER BY id FOR UPDATE`` of every tenant-scoped operation of one work order (taken FIRST).

    Ascending id: Postgres locks the rows in the sorted order, so two lockers of overlapping
    operation sets always meet in the same order.
    """
    return (
        tenant_query(db, WorkOrderOperation, company_id)
        .filter(WorkOrderOperation.work_order_id == work_order_id)
        .order_by(WorkOrderOperation.id)
        .populate_existing()
        .with_for_update()
    )


def lock_work_order(db: Session, work_order_id: int, company_id: int) -> Optional[WorkOrder]:
    """Row-lock the route every routing-change verb serializes on, and return the work order (or ``None``).

    Every verb's guards read the WHOLE route (at least one step stays not skipped, no
    sequence collision, nothing lands in front of recorded work, no strand), so two
    concurrent changes judged over two unlocked snapshots could each pass and together
    break the rule -- two skips skipping every step. All four verbs therefore take this lock
    before their first read of the route: skip / unskip through ``load_operation``, add and
    resequence in their routers.

    **Lock order: every operation row of the work order (``FOR UPDATE``, ascending id), THEN
    the work order row, then the audit chain lock** (taken inside ``log_required``, always
    last). That is the OPERATION-then-WORK-ORDER order the completion and clock-out verbs
    already use, so a routing change and a completer never each hold one row and wait on the
    other. Locking the operation rows, not only the work order, is what serializes a routing
    change against the floor: clock-in and both start verbs row-lock their operation before
    they read the predecessor gate (``operation_action_gates.lock_operation_for_start``). The
    start either commits first and this verb's recorded-work guard sees it, or this verb
    commits first and the start's gate reads the changed route.

    After the work order lock, the operation rows are locked once more. A step that another
    routing change inserted while this one waited (it held the work order then) is not in
    the first pass's snapshot. Rows this transaction already holds are re-locked at no cost.
    ``populate_existing`` re-reads every locked row, so no guard judges a stale identity-map
    copy. The dispatch-order lock is not taken: no routing verb writes ``dispatch_rank`` or
    ``run_order``.
    """
    operation_rows_lock_query(db, work_order_id, company_id).all()
    work_order = work_order_lock_query(db, work_order_id, company_id).first()
    if work_order is not None:
        operation_rows_lock_query(db, work_order_id, company_id).all()
    return work_order


def load_operation(db: Session, operation_id: int, company_id: int) -> WorkOrderOperation:
    """The tenant-scoped operation, or 404 (another company's id is indistinguishable from none).

    Locks the operation's whole route first (``lock_work_order``: every operation row of its
    work order, then the work order row), so skip / unskip serialize with every other routing
    change on the same work order and with the floor's start verbs.
    """
    work_order_id = (
        tenant_query(db, WorkOrderOperation, company_id)
        .filter(WorkOrderOperation.id == operation_id)
        .with_entities(WorkOrderOperation.work_order_id)
        .scalar()
    )
    if work_order_id is None:
        raise HTTPException(status_code=404, detail="Operation not found")
    lock_work_order(db, work_order_id, company_id)
    # Already locked and re-read by the route lock; this resolves the row and re-checks it is
    # still on that work order.
    operation = (
        tenant_query(db, WorkOrderOperation, company_id)
        .filter(WorkOrderOperation.id == operation_id, WorkOrderOperation.work_order_id == work_order_id)
        .first()
    )
    if operation is None:
        raise HTTPException(status_code=404, detail="Operation not found")
    return operation


def route_operations(db: Session, work_order: WorkOrder) -> list[WorkOrderOperation]:
    """Every operation of ``work_order``, tenant-scoped off the work order's own company, by sequence."""
    return (
        tenant_query(db, WorkOrderOperation, work_order.company_id)
        .filter(WorkOrderOperation.work_order_id == work_order.id)
        .order_by(WorkOrderOperation.sequence, WorkOrderOperation.id)
        .all()
    )


def _check_version(operation: WorkOrderOperation, version: int) -> None:
    if version != operation.version:
        raise HTTPException(status_code=409, detail=OPERATION_VERSION_CONFLICT)


def _would_block_recorded_work(
    work_order: WorkOrder,
    candidates: Iterable[Any],
    route: Sequence[WorkOrderOperation],
    recorded_ids: set[int],
) -> Optional[tuple[Any, WorkOrderOperation]]:
    """``(candidate, worked)`` when an un-finished candidate step would block an operation with recorded work.

    Asks THE gate (the in-memory twin, with the work order's own ``allow_same_work_center``):
    on a sequenced routing any lower sequence blocks; on a pooled one only a lower sequence at a
    DIFFERENT work center (a new / restored / moved step is never ON_HOLD). ``candidates`` are the
    steps as the change would leave them (stand-ins carrying the new sequence); the worked
    operations are judged at their own, unchanged position.
    """
    allow_same_work_center = work_order_allows_same_work_center(work_order)
    candidate_list = [c for c in candidates if not is_operation_finished(c.status)]
    candidate_ids = {c.id for c in candidate_list}
    for worked in sorted(route, key=lambda op: (op.sequence or 0, op.id or 0)):
        if worked.id not in recorded_ids or worked.id in candidate_ids or is_operation_skipped(worked):
            continue
        for candidate in candidate_list:
            if operation_blocked_by_incomplete_predecessors(
                worked, [candidate], allow_same_work_center=allow_same_work_center
            ):
                return candidate, worked
    return None


def _refuse_blocking_recorded_work(
    work_order: WorkOrder,
    candidates: Iterable[Any],
    route: Sequence[WorkOrderOperation],
    recorded_ids: set[int],
    *,
    verb: str,
) -> None:
    hit = _would_block_recorded_work(work_order, candidates, route, recorded_ids)
    if hit is None:
        return
    candidate, worked = hit
    raise HTTPException(
        status_code=409,
        detail=(
            f"Cannot {verb} at sequence {candidate.sequence}: {operation_label(worked)} (sequence {worked.sequence}) "
            "already has recorded work, and a step in front of it that is not finished would block it from being "
            "completed. Use a sequence after the work already done."
        ),
    )


def _refuse_strand(work_order: WorkOrder, route: Sequence[WorkOrderOperation], after: Sequence[Any]) -> None:
    strand = routing_change_strands(work_order, route, after)
    if strand is not None:
        raise HTTPException(status_code=409, detail=accounting_strand_message(strand))


def _promote(db: Session, work_order: WorkOrder, user: User) -> list[WorkOrderOperation]:
    """PENDING -> READY through THE rule (``promote_ready_operations``), over a fresh tenant read."""
    db.flush()
    return promote_ready_operations(work_order, route_operations(db, work_order), db=db, user_id=user.id)


def _repoint(work_order: WorkOrder) -> None:
    """Keep ``current_operation_id`` on the step the work order is now on (RUP-1), in flight only."""
    if work_order.status in (WorkOrderStatus.RELEASED, WorkOrderStatus.IN_PROGRESS):
        db_session = object_session_or_none(work_order)
        if db_session is not None:
            db_session.expire(work_order, ["operations"])
        work_order.current_operation_id = active_operation_id(work_order)


def _identifier(work_order: WorkOrder, operation: WorkOrderOperation) -> str:
    return f"{work_order.work_order_number} OP{operation.sequence}"


# --------------------------------------------------------------------------- skip


def skip_operation(
    db: Session,
    operation: WorkOrderOperation,
    *,
    version: int,
    reason: str,
    user: User,
    audit: AuditService,
) -> RoutingChangeOutcome:
    """PENDING / READY -> SKIPPED, then promote the successors it unblocks. Required audit."""
    refuse_api_token(user)
    work_order = assert_routing_changeable(operation.work_order)
    _check_version(operation, version)
    if operation.status not in SKIPPABLE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"Only a step that has not started can be skipped; {operation_label(operation)} is "
            f"{_status_value(operation.status)}.",
        )
    route = route_operations(db, work_order)
    recorded = operation_ids_with_recorded_work(db, [operation], work_order.company_id)
    if operation.id in recorded:
        raise HTTPException(
            status_code=409,
            detail=f"{operation_label(operation)} already has recorded work (a start, pieces or labor) and cannot "
            "be skipped. Complete it, or correct its counts first.",
        )
    if not any(op.id != operation.id and not is_operation_skipped(op) for op in route):
        raise HTTPException(
            status_code=409,
            detail="A work order must keep at least one step that is not skipped. Cancel the work order instead.",
        )
    # A skip that leaves nothing unfinished completes the work order (the route then runs the
    # completion reconcile: FG receipt, backflush, cost). On a held job that would carry
    # quality- or material-held product to stock without anyone lifting the hold, so it is
    # refused; Resume Work Order is the one door back from a hold. Skipping any other step of
    # a held job stays allowed -- it completes nothing.
    if work_order.status == WorkOrderStatus.ON_HOLD and not any(
        op.id != operation.id and not is_operation_finished(op.status)
        for op in active_work_order_operations(work_order)
    ):
        raise HTTPException(status_code=409, detail=SKIP_WOULD_COMPLETE_HELD_WORK_ORDER)
    after = [operation_what_if(op, status=OperationStatus.SKIPPED) if op.id == operation.id else op for op in route]
    _refuse_strand(work_order, route, after)

    old_status = _status_value(operation.status)
    operation.status = OperationStatus.SKIPPED
    operation.skipped_at = datetime.utcnow()
    operation.skipped_by = user.id
    operation.skip_reason = reason
    # A skipped step reserves no capacity: freed by data, the way a completion frees it.
    release_operation_schedule_reservation(operation)
    db.flush()
    audit.log_required(
        action=audit.ACTIONS["STATUS_CHANGE"],
        resource_type="work_order_operation",
        resource_id=operation.id,
        resource_identifier=_identifier(work_order, operation),
        description=f"Skipped operation {operation_label(operation)}: {reason}",
        old_values={"status": old_status},
        new_values={"status": OperationStatus.SKIPPED.value, "skip_reason": reason},
        extra_data={
            "transition": "operation_skipped",
            "reason": reason,
            "operation_number": operation.operation_number,
            "sequence": operation.sequence,
            "work_order_id": work_order.id,
            "work_order_status": _status_value(work_order.status),
        },
    )
    promoted = _promote(db, work_order, user)
    _repoint(work_order)
    return RoutingChangeOutcome(operation=operation, promoted=promoted, changed=[operation])


# --------------------------------------------------------------------------- unskip


def unskip_operation(
    db: Session,
    operation: WorkOrderOperation,
    *,
    version: int,
    reason: str,
    user: User,
    audit: AuditService,
) -> RoutingChangeOutcome:
    """SKIPPED -> PENDING, demote what it now blocks, then promote. Required audit."""
    refuse_api_token(user)
    work_order = assert_routing_changeable(operation.work_order)
    _check_version(operation, version)
    if not is_operation_skipped(operation):
        raise HTTPException(
            status_code=409,
            detail=f"{operation_label(operation)} is {_status_value(operation.status)}, not skipped.",
        )
    route = route_operations(db, work_order)
    # Add and resequence treat a skipped step's sequence as free (a replacement may take it), so
    # restoring the original must not put two live steps at one sequence: on a sequenced routing
    # neither would block the other (both gates compare ``sequence <``), i.e. pooled behavior.
    taken = next(
        (
            op
            for op in route
            if op.id != operation.id and op.sequence == operation.sequence and not is_operation_skipped(op)
        ),
        None,
    )
    if taken is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Sequence {operation.sequence} is now used by {operation_label(taken)} on this work order, so "
            f"{operation_label(operation)} cannot be restored there. Move {operation_label(taken)} to an unused "
            "sequence first.",
        )
    recorded = operation_ids_with_recorded_work(db, route, work_order.company_id)
    restored = operation_what_if(operation, status=OperationStatus.PENDING)
    _refuse_blocking_recorded_work(work_order, [restored], route, recorded, verb="restore this step")
    after = [restored if op.id == operation.id else op for op in route]
    _refuse_strand(work_order, route, after)

    previous = {
        "status": OperationStatus.SKIPPED.value,
        "skipped_at": operation.skipped_at.isoformat() if operation.skipped_at else None,
        "skipped_by": operation.skipped_by,
        "skip_reason": operation.skip_reason,
    }
    operation.status = OperationStatus.PENDING
    operation.skipped_at = None
    operation.skipped_by = None
    operation.skip_reason = None
    db.flush()
    demoted = demote_ready_operations(db, work_order, scope="blocked_by", blocked_by=operation)
    # Flushed BEFORE the first audit write (WorkOrderOperation maps version_id_col): a concurrent
    # writer's StaleDataError must reach the app-wide 409, not die inside an audit savepoint.
    db.flush()
    audit.log_required(
        action=audit.ACTIONS["STATUS_CHANGE"],
        resource_type="work_order_operation",
        resource_id=operation.id,
        resource_identifier=_identifier(work_order, operation),
        description=f"Restored skipped operation {operation_label(operation)}: {reason}",
        old_values=previous,
        new_values={"status": OperationStatus.PENDING.value},
        extra_data={
            "transition": "operation_unskipped",
            "reason": reason,
            "operation_number": operation.operation_number,
            "sequence": operation.sequence,
            "work_order_id": work_order.id,
        },
    )
    log_demotions_required(
        audit,
        demoted,
        reason=f"restored step {operation_label(operation)} now comes first",
        extra={"transition": "operation_unskipped", "restored_operation_id": operation.id, "reason": reason},
    )
    promoted = _promote(db, work_order, user)
    _repoint(work_order)
    return RoutingChangeOutcome(operation=operation, promoted=promoted, demoted=demoted, changed=[operation])


# --------------------------------------------------------------------------- add


def add_operation(
    db: Session,
    work_order: Optional[WorkOrder],
    values: dict,
    *,
    change_reason: Optional[str],
    user: User,
    audit: AuditService,
) -> RoutingChangeOutcome:
    """Insert one step. The sequence / recorded-work / strand rules apply on every work order,
    DRAFT included; on a non-DRAFT one a reason is also required, a token is refused 403 and the
    audit row is REQUIRED. The caller has already row-locked the work order (``lock_work_order``)."""
    if work_order is None or work_order.is_deleted:
        raise HTTPException(status_code=404, detail="Work order not found")
    is_draft = work_order.status == WorkOrderStatus.DRAFT
    if not is_draft:
        refuse_api_token(user)
    assert_routing_changeable(work_order)
    if not is_draft and not change_reason:
        raise HTTPException(
            status_code=422,
            detail="change_reason is required to add a step to a work order that has been released.",
        )

    route = route_operations(db, work_order)
    sequence = values.get("sequence")
    taken = next((op for op in route if op.sequence == sequence and not is_operation_skipped(op)), None)
    if taken is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Sequence {sequence} is already used by {operation_label(taken)} on this work order. "
            "Pick an unused sequence.",
        )

    operation = WorkOrderOperation(work_order_id=work_order.id, company_id=work_order.company_id, **values)
    # Judged on a DRAFT too: these protect the production RECORD, not the authorization step,
    # and a DRAFT can still carry worked steps (a PUT back to draft, or a resume with no
    # release record, demotes only un-worked ones) whose running labor must stay closable.
    recorded = operation_ids_with_recorded_work(db, route, work_order.company_id)
    candidate = operation_what_if(operation, id=-1, status=OperationStatus.PENDING)
    _refuse_blocking_recorded_work(work_order, [candidate], route, recorded, verb="add a step")
    _refuse_strand(work_order, route, [*route, candidate])

    db.add(operation)
    db.flush()
    demoted: list[WorkOrderOperation] = []
    if not is_draft:
        demoted = demote_ready_operations(db, work_order, scope="blocked_by", blocked_by=operation)
        db.flush()
        audit.log_required(
            action=audit.ACTIONS["CREATE"],
            resource_type="work_order_operation",
            resource_id=operation.id,
            resource_identifier=_identifier(work_order, operation),
            description=f"Added operation {operation_label(operation)} ({operation.name}) to a released work order: "
            f"{change_reason}",
            new_values=audit._model_to_dict(operation),
            extra_data={
                "transition": "operation_added",
                "reason": change_reason,
                "operation_number": operation.operation_number,
                "sequence": operation.sequence,
                "work_order_id": work_order.id,
                "work_order_status": _status_value(work_order.status),
            },
        )
        log_demotions_required(
            audit,
            demoted,
            reason=f"new step {operation_label(operation)} now comes first",
            extra={"transition": "operation_added", "added_operation_id": operation.id, "reason": change_reason},
        )
    else:
        # A DRAFT's plan is still being built: best-effort, as every other draft edit.
        audit.log_create(
            resource_type="work_order_operation",
            resource_id=operation.id,
            resource_identifier=_identifier(work_order, operation),
            new_values=operation,
        )
    promoted = _promote(db, work_order, user)
    return RoutingChangeOutcome(operation=operation, promoted=promoted, demoted=demoted, changed=[operation])


# --------------------------------------------------------------------------- resequence


def resequence_operations(
    db: Session,
    work_order: Optional[WorkOrder],
    changes: Sequence[Any],
    *,
    reason: str,
    user: User,
    audit: AuditService,
) -> RoutingChangeOutcome:
    """Move not-started steps to new sequences. One required audit row per moved step."""
    refuse_api_token(user)
    work_order = assert_routing_changeable(work_order)
    route = route_operations(db, work_order)
    by_id = {op.id: op for op in route}

    requested: dict[int, int] = {}
    for change in changes:
        operation = by_id.get(change.id)
        if operation is None:
            # Another work order's (or another company's) operation is indistinguishable from none.
            raise HTTPException(status_code=404, detail=f"Operation {change.id} is not on this work order")
        if change.version != operation.version:
            raise HTTPException(status_code=409, detail=OPERATION_VERSION_CONFLICT)
        if operation.status not in REORDERABLE_STATUSES:
            raise HTTPException(
                status_code=409,
                detail=f"Only steps that have not started can be reordered; {operation_label(operation)} is "
                f"{_status_value(operation.status)}.",
            )
        requested[operation.id] = change.sequence

    recorded = operation_ids_with_recorded_work(db, route, work_order.company_id)
    worked_listed = next((by_id[op_id] for op_id in requested if op_id in recorded), None)
    if worked_listed is not None:
        raise HTTPException(
            status_code=409,
            detail=f"{operation_label(worked_listed)} already has recorded work and cannot be reordered.",
        )

    moved_ids = {op_id for op_id, seq in requested.items() if by_id[op_id].sequence != seq}
    final_sequence = {op.id: requested.get(op.id, op.sequence) for op in route}
    seen: dict[int, WorkOrderOperation] = {}
    for op in route:
        if is_operation_skipped(op):
            continue
        seq = final_sequence[op.id]
        if seq in seen:
            raise HTTPException(
                status_code=409,
                detail=f"Sequence {seq} would be used by both {operation_label(seen[seq])} and {operation_label(op)}. "
                "Each step needs its own sequence.",
            )
        seen[seq] = op
    if not moved_ids:
        return RoutingChangeOutcome()

    stand_ins = {op_id: operation_what_if(by_id[op_id], sequence=final_sequence[op_id]) for op_id in moved_ids}
    _refuse_blocking_recorded_work(work_order, stand_ins.values(), route, recorded, verb="move a step")
    after = [stand_ins.get(op.id, op) for op in route]
    _refuse_strand(work_order, route, after)

    already_blocked = ready_operations_blocked_now(work_order, route)
    moved = sorted((by_id[op_id] for op_id in moved_ids), key=lambda op: (final_sequence[op.id], op.id))
    old_sequences = {op.id: op.sequence for op in moved}
    for op in moved:
        op.sequence = final_sequence[op.id]
    db.flush()
    demoted = demote_ready_operations(db, work_order, scope="newly_blocked", already_blocked_ids=already_blocked)
    db.flush()
    sequence_map = {str(op.id): {"from": old_sequences[op.id], "to": op.sequence} for op in moved}
    for op in moved:
        audit.log_required(
            action=audit.ACTIONS["UPDATE"],
            resource_type="work_order_operation",
            resource_id=op.id,
            resource_identifier=_identifier(work_order, op),
            description=f"Resequenced operation {operation_label(op)} from {old_sequences[op.id]} to "
            f"{op.sequence}: {reason}",
            old_values={"sequence": old_sequences[op.id]},
            new_values={"sequence": op.sequence},
            extra_data={
                "transition": "operations_resequenced",
                "reason": reason,
                "operation_number": op.operation_number,
                "work_order_id": work_order.id,
                "sequence_changes": sequence_map,
            },
        )
    log_demotions_required(
        audit,
        demoted,
        reason="the steps were reordered and an earlier step now comes first",
        extra={"transition": "operations_resequenced", "reason": reason},
    )
    promoted = _promote(db, work_order, user)
    _repoint(work_order)
    return RoutingChangeOutcome(promoted=promoted, demoted=demoted, changed=moved)
