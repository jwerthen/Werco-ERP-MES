"""Shared work-order state rules used by office and shop-floor flows."""

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from app.models.time_entry import TimeEntry
from app.models.work_order import (
    OperationStatus,
    WorkOrder,
    WorkOrderOperation,
    WorkOrderStatus,
    WorkOrderType,
)

# G6-A: the set of work-order statuses that are *terminal* -- a WO in any of
# these states has finished its lifecycle and must never be reopened or
# resurrected (esp. CANCELLED, which previously slipped through guards that only
# checked COMPLETE/CLOSED, letting a cancelled job be driven to COMPLETE and
# re-fire FG receipt / backflush / cost rollup and write a COMPLETE row onto the
# tamper-evident audit chain). Reuse this set everywhere a completion guard or a
# reconcile-on-read needs to refuse touching a finished WO.
TERMINAL_WO_STATUSES = {
    WorkOrderStatus.COMPLETE,
    WorkOrderStatus.CLOSED,
    WorkOrderStatus.CANCELLED,
}


class WorkOrderStateError(ValueError):
    """Raised when a requested work-order transition is not valid."""


@dataclass
class StatusTransition:
    """One reconcile-driven status change, returned so a read handler can audit it.

    ``reconcile_work_orders_from_completion_evidence`` mutates persistent state
    from durable shop-floor evidence (TimeEntry sums) on the read path. It has no
    actor, so it cannot write the tamper-evident ``audit_log`` itself. Instead it
    records each terminal transition here and hands them back to the caller, which
    *does* hold ``current_user`` and can emit ``AuditService.log_status_change``
    before its commit (AUD-3).
    """

    resource_type: str  # "work_order" | "work_order_operation"
    resource_id: int
    resource_identifier: Optional[str]
    old_status: Optional[str]
    new_status: str
    work_order_number: Optional[str] = None
    # EVT-4: the owning work order's id, so a read handler can emit the reconcile
    # OperationalEvent with the same ``work_order_id`` the live completion paths use
    # (an operation_completed event must be queryable by work order). For a
    # work_order transition this equals ``resource_id``.
    work_order_id: Optional[int] = None
    time_entry_ids: list[int] = field(default_factory=list)
    # MS-2: for a work_order -> COMPLETE transition, the work_center_ids whose
    # capacity the read handler must refresh (a COMPLETE op drops out of the
    # scheduled-load query, so the persisted availability_rate would otherwise stay
    # understated). Empty for operation transitions and non-completion transitions.
    work_center_ids: list[int] = field(default_factory=list)


def incomplete_child_work_orders(
    db: Session,
    work_order: WorkOrder,
    company_id: int,
) -> list[WorkOrder]:
    """Laser-nest child WOs of ``work_order`` that have NOT reached a terminal state.

    G1 gate scope (chosen): only ``LASER_CUTTING`` children are tracked for the
    parent rollup (a parent WO with a laser nest spawns one child per nest; those
    must finish before the parent is treated as legitimately complete). Read-only
    and tenant-scoped -- always filters ``company_id`` and ``is_deleted == False`` so
    it can never surface another tenant's or a soft-deleted child. A child counts as
    "incomplete" while its status is NOT in ``TERMINAL_WO_STATUSES``
    (COMPLETE/CLOSED/CANCELLED) -- a CANCELLED child is intentionally treated as
    resolved, not as a blocker.
    """
    if work_order is None or work_order.id is None:
        return []
    return (
        db.query(WorkOrder)
        .filter(
            WorkOrder.parent_work_order_id == work_order.id,
            WorkOrder.company_id == company_id,
            WorkOrder.is_deleted == False,  # noqa: E712
            WorkOrder.work_order_type == WorkOrderType.LASER_CUTTING.value,
            WorkOrder.status.notin_(TERMINAL_WO_STATUSES),
        )
        .all()
    )


def find_parent_to_advance(
    db: Session,
    completed_work_order: WorkOrder,
    company_id: int,
) -> Optional[WorkOrder]:
    """Return the parent WO whose LAST laser child just completed, else ``None`` (G1).

    Advance signal (NOT an auto-complete): parent and child work orders are NOT
    operation-coupled in the data model, so we never mutate the parent's route here.
    The caller uses the returned parent only to record a surfacing flag + audit/event.

    Tenant-scoped and read-only on the parent lookup (``company_id`` +
    ``is_deleted == False``). Flushes first so the just-completed child's terminal
    status is visible to the ``incomplete_child_work_orders`` query (the session runs
    autoflush=False on the completion paths). Returns the parent ONLY when, after this
    completion, NO laser child remains non-terminal -- which becomes true exactly once
    (when the last child flips), so the advance fires at most once per parent.
    """
    if completed_work_order is None or completed_work_order.parent_work_order_id is None:
        return None
    db.flush()
    parent = (
        db.query(WorkOrder)
        .filter(
            WorkOrder.id == completed_work_order.parent_work_order_id,
            WorkOrder.company_id == company_id,
            WorkOrder.is_deleted == False,  # noqa: E712
        )
        .first()
    )
    if parent is None:
        return None
    if incomplete_child_work_orders(db, parent, company_id):
        return None
    return parent


def operation_target_quantity(
    operation: Optional[WorkOrderOperation],
    work_order: Optional[WorkOrder] = None,
) -> float:
    """Quantity required for an operation, including component operation targets."""
    if operation and operation.component_quantity and float(operation.component_quantity) > 0:
        return float(operation.component_quantity)
    if work_order and work_order.quantity_ordered:
        return float(work_order.quantity_ordered)
    if operation and operation.work_order and operation.work_order.quantity_ordered:
        return float(operation.work_order.quantity_ordered)
    return 0.0


def has_incomplete_predecessors(
    db: Session,
    work_order_id: int,
    sequence: int,
    current_operation_id: Optional[int] = None,
    current_work_center_id: Optional[int] = None,
    allow_same_work_center: bool = False,
) -> bool:
    query = db.query(WorkOrderOperation).filter(
        and_(
            WorkOrderOperation.work_order_id == work_order_id,
            WorkOrderOperation.sequence < sequence,
            WorkOrderOperation.status != OperationStatus.COMPLETE,
        )
    )
    if current_operation_id is not None:
        query = query.filter(WorkOrderOperation.id != current_operation_id)
    if allow_same_work_center and current_work_center_id is not None:
        query = query.filter(WorkOrderOperation.work_center_id != current_work_center_id)
    return query.count() > 0


def release_first_ready_operation(
    work_order: WorkOrder,
) -> Optional[WorkOrderOperation]:
    if not work_order.operations:
        return None

    first_pending = min(
        (op for op in work_order.operations if op.status == OperationStatus.PENDING),
        key=lambda op: op.sequence,
        default=None,
    )
    if first_pending:
        first_pending.status = OperationStatus.READY
    return first_pending


def release_next_ready_operation(
    db: Session,
    work_order: WorkOrder,
    completed_op: WorkOrderOperation,
) -> Optional[WorkOrderOperation]:
    """Promote the lowest-sequence PENDING op whose predecessors are all complete.

    RUP-4: this is intentionally self-healing rather than strictly forward-only.
    The shop-floor scan/complete path completes ops out of sequence within the same
    work center (``allow_same_work_center=True``), which used to strand an
    earlier-sequence PENDING op in PENDING forever (only ``release_first_ready_operation``
    at WO release would have promoted it). We now scan every PENDING op on the WO in
    sequence order and promote the first one whose predecessor gate is satisfied,
    so single-op completions advance the route without depending on a read-time
    reconcile or a manual clock-in. Reuses ``has_incomplete_predecessors`` as the
    order gate so the same rule governs release and the start/complete guards.

    The session runs with ``autoflush=False``, so flush the just-completed
    operation's status first -- otherwise the predecessor gate below would query
    its stale (pre-COMPLETE) row and refuse to release the successor.

    PERF-4: load every operation of this WO ONCE and run the predecessor gate
    in memory, instead of calling ``has_incomplete_predecessors`` (one COUNT(*)
    query) per PENDING candidate. The old shape was an N+1 that turned quadratic
    inside ``complete_work_order``'s force-complete loop (each force-completed op
    re-walks the route). The in-memory ``blocked`` test below replicates
    ``has_incomplete_predecessors(db, work_order.id, candidate.sequence,
    current_operation_id=candidate.id)`` EXACTLY -- "exists an op of THIS work
    order with ``sequence < candidate.sequence`` AND ``status != COMPLETE`` AND
    ``id != candidate.id``" -- so release/start/complete keep the same order gate.
    """
    db.flush()
    all_ops = (
        db.query(WorkOrderOperation)
        .filter(WorkOrderOperation.work_order_id == work_order.id)
        .order_by(WorkOrderOperation.sequence)
        .all()
    )
    incomplete = [op for op in all_ops if op.status != OperationStatus.COMPLETE]
    pending_ops = [op for op in all_ops if op.status == OperationStatus.PENDING]
    for candidate in pending_ops:
        blocked = any(op.sequence < candidate.sequence and op.id != candidate.id for op in incomplete)
        if not blocked:
            candidate.status = OperationStatus.READY
            return candidate
    return None


def _operation_produced_evidence(db: Session, operation: WorkOrderOperation) -> float:
    """Durable produced-good total recorded against an operation's TimeEntry rows.

    SFI-5 / DUP-3: an absolute completion verb must never write
    ``quantity_complete`` below what the operator already booked on durable
    TimeEntry evidence, otherwise a later read-time reconcile silently bumps it
    back up with no audit of the discrepancy. Returns 0 for an unsaved operation.
    """
    if operation.id is None:
        return 0.0
    total = (
        db.query(func.coalesce(func.sum(TimeEntry.quantity_produced), 0.0))
        .filter(TimeEntry.operation_id == operation.id)
        .scalar()
    )
    return float(total or 0.0)


def resolve_absolute_operation_quantity(
    db: Session,
    operation: WorkOrderOperation,
    requested_absolute: float,
    target_qty: float,
) -> float:
    """Quantity an *absolute* completion verb should store for an operation.

    ``complete_operation`` (absolute verb) contract:
        clamp(max(existing, requested_absolute, produced_evidence_sum), 0, target)

    Never regresses below the currently stored value, never below durable
    TimeEntry evidence (SFI-5), never above the operation target (SFI-5). The
    additive verbs (``clock_out`` / ``report_operation_production``) keep ``+=``
    in the endpoint; they pass the already-incremented value through this floor by
    calling ``floor_operation_quantity_at_evidence`` instead.
    """
    floor = max(
        float(operation.quantity_complete or 0),
        float(requested_absolute or 0),
        _operation_produced_evidence(db, operation),
    )
    if target_qty > 0:
        floor = min(floor, target_qty)
    return max(0.0, floor)


def floor_operation_quantity_at_evidence(
    db: Session,
    operation: WorkOrderOperation,
    proposed: float,
    target_qty: float,
) -> float:
    """Floor an additive verb's already-incremented quantity at durable evidence.

    The additive paths compute ``existing + delta`` themselves; this guarantees
    the stored result is never below recorded TimeEntry evidence (DUP-3) and never
    above the operation target (SFI-5), keeping additive and absolute writes
    converging on the same invariant.
    """
    floored = max(float(proposed or 0), _operation_produced_evidence(db, operation))
    if target_qty > 0:
        floored = min(floored, target_qty)
    return max(0.0, floored)


def sync_work_order_quantity_complete(
    work_order: WorkOrder,
    operation: WorkOrderOperation,
    all_operations_complete: bool,
) -> None:
    """Roll an operation's progress up into ``work_order.quantity_complete``.

    RUP-6: always guarded with ``max()`` against the WO's current value so an
    earlier-stage / out-of-sequence operation can never pull finished quantity
    *backward* across completion events. Component operations do not contribute to
    finished WO quantity until the whole route is complete.
    """
    existing = float(work_order.quantity_complete or 0)
    target = float(work_order.quantity_ordered or 0)
    if all_operations_complete:
        work_order.quantity_complete = max(existing, target)
    elif not operation.component_part_id:
        candidate = float(operation.quantity_complete or 0)
        if target > 0:
            candidate = min(candidate, target)
        work_order.quantity_complete = max(existing, candidate)


def _active_operation_id(work_order: WorkOrder) -> Optional[int]:
    """The operation the WO is 'currently on', for ``current_operation_id`` (RUP-1).

    Preference order, by ascending sequence: the first IN_PROGRESS op, else the
    first READY op, else the first not-yet-COMPLETE op. Returns ``None`` when the
    whole route is complete (the WO is no longer on any operation).
    """
    operations = sorted(
        (op for op in (work_order.operations or []) if op.id is not None),
        key=lambda op: (op.sequence if op.sequence is not None else 0),
    )
    for wanted in (OperationStatus.IN_PROGRESS, OperationStatus.READY):
        for op in operations:
            if op.status == wanted:
                return op.id
    for op in operations:
        if op.status != OperationStatus.COMPLETE:
            return op.id
    return None


def release_operation_schedule_reservation(operation: WorkOrderOperation) -> bool:
    """Free a completed operation's capacity reservation by clearing its schedule (MS-5).

    Scheduling capacity is recomputed from operations where ``status !=
    OperationStatus.COMPLETE`` (``scheduling_service._initialize_capacity`` /
    ``_get_scheduled_hours_by_work_center``). A completed op that still carries
    ``scheduled_start``/``scheduled_end`` is correct ONLY as long as every reader
    remembers that status predicate; any future/third-party query over scheduled
    operations that omits it would double-count finished work as still-reserved
    capacity. Nulling the schedule on completion frees the reservation by DATA rather
    than by every consumer remembering the filter, so the in-tree status filters and
    any new reader agree. Returns True if it changed anything (for the reconcile
    change-tracking). No-op if the op is not COMPLETE or already cleared.
    """
    if operation.status != OperationStatus.COMPLETE:
        return False
    changed = False
    if operation.scheduled_start is not None:
        operation.scheduled_start = None
        changed = True
    if operation.scheduled_end is not None:
        operation.scheduled_end = None
        changed = True
    return changed


def _remaining_incomplete_operation_ids(
    work_order: WorkOrder,
    completed_operation: WorkOrderOperation,
) -> list[int]:
    """Ids of other operations on the WO that are not COMPLETE.

    DUP-5: reuse the already-loaded ``work_order.operations`` relationship instead
    of issuing a redundant COUNT query. ``completed_operation`` is treated as
    complete even if the caller has not flushed its status yet (it is being
    completed in the same unit of work).
    """
    remaining: list[int] = []
    for op in work_order.operations or []:
        if op.id == completed_operation.id:
            continue
        if op.status != OperationStatus.COMPLETE:
            remaining.append(op.id)
    return remaining


def finalize_operation_completion(
    db: Session,
    work_order: WorkOrder,
    operation: WorkOrderOperation,
    *,
    all_operations_complete_hint: Optional[bool] = None,
) -> set[int]:
    """Roll a just-completed operation up into its work order. Returns affected WCs.

    This is the single shared rollup all completion paths delegate to (DUP-5). The
    caller owns auth, tenant lookup, row locks, audit, scheduling refresh and
    broadcasts; this function owns ONLY the state transition and returns the set of
    ``work_center_id``s whose capacity the caller must refresh. It does not commit
    and it does not flush the audit chain.

    Contract (one implementation, so the three former inline copies cannot drift):

    * Remaining-ops decision reuses the loaded ``work_order.operations`` (DUP-5);
      ``operation`` must already be flipped to COMPLETE by the caller (it stamps
      ``actual_end``/``completed_by`` with the acting user before calling).
    * COMPLETE branch: ALWAYS stamp ``work_order.actual_start`` (min op actual_start,
      falling back to now) BEFORE flipping the WO to COMPLETE (DUP-2 — fixes the
      ``actual_end``-without-``actual_start`` rows), set ``actual_end`` = max op
      actual_end (falling back to now), sync finished qty via the ``max()`` guard
      (RUP-6) and CLEAR ``current_operation_id`` (RUP-1).
    * RELEASED→IN_PROGRESS branch: lift a RELEASED WO to IN_PROGRESS and stamp
      ``actual_start`` on first progress, self-heal the next READY op via
      ``has_incomplete_predecessors`` (RUP-4), and populate ``current_operation_id``
      with the now-active/next op (RUP-1).
    """
    affected_work_centers: set[int] = set()
    if operation.work_center_id:
        affected_work_centers.add(operation.work_center_id)

    # MS-5: the just-completed operation no longer reserves capacity -- free it by data
    # (clear its schedule) so it cannot be double-counted by any reader that forgets the
    # ``status != COMPLETE`` predicate. The caller refreshes availability for the WCs we
    # return, so the persisted availability_rate stays in step with the freed reservation.
    release_operation_schedule_reservation(operation)

    if all_operations_complete_hint is not None:
        remaining_ids: list[int] = [] if all_operations_complete_hint else [-1]
    else:
        remaining_ids = _remaining_incomplete_operation_ids(work_order, operation)

    if not remaining_ids:
        # All operations complete -> the work order is finished.
        now = datetime.utcnow()
        end_dates = [op.actual_end for op in (work_order.operations or []) if op.actual_end]
        work_order.actual_end = max(end_dates) if end_dates else now
        start_dates = [op.actual_start for op in (work_order.operations or []) if op.actual_start]
        # DUP-2: stamp actual_start BEFORE flipping to COMPLETE so no terminal WO
        # is left with actual_end but a NULL actual_start (corrupts cycle-time).
        # When no op carries an actual_start, the `now` fallback is captured AFTER
        # the endpoints already stamped operation.actual_end, so a bare `now` would
        # land AFTER actual_end and yield a NEGATIVE cycle time. Clamp the fallback
        # at actual_end so actual_start <= actual_end always holds.
        if not work_order.actual_start:
            work_order.actual_start = min(start_dates) if start_dates else min(now, work_order.actual_end)
        # G6-A: refuse to (re)flip a terminal WO. CANCELLED/CLOSED/COMPLETE are all
        # final -- a CANCELLED WO must not be resurrected to COMPLETE from operation
        # evidence (that would re-fire FG receipt/backflush/cost rollup and write a
        # COMPLETE audit row). Was COMPLETE/CLOSED only, which let CANCELLED through.
        if work_order.status not in TERMINAL_WO_STATUSES:
            work_order.status = WorkOrderStatus.COMPLETE
        sync_work_order_quantity_complete(work_order, operation, all_operations_complete=True)
        # RUP-1: the WO is no longer sitting on any operation.
        work_order.current_operation_id = None
    elif work_order.status not in TERMINAL_WO_STATUSES:
        # More operations remain: lift the WO to IN_PROGRESS on first progress and
        # self-heal the next READY operation. G6-A: gated so a terminal (esp.
        # CANCELLED) WO can never be reopened to IN_PROGRESS from operation evidence.
        if work_order.status == WorkOrderStatus.RELEASED:
            work_order.status = WorkOrderStatus.IN_PROGRESS
            if not work_order.actual_start:
                work_order.actual_start = operation.actual_start or datetime.utcnow()
        release_next_ready_operation(db, work_order, operation)
        for op in work_order.operations or []:
            if op.status == OperationStatus.READY and op.work_center_id:
                affected_work_centers.add(op.work_center_id)
        sync_work_order_quantity_complete(work_order, operation, all_operations_complete=False)
        # RUP-1: point the WO at the operation it is now on (active/next).
        work_order.current_operation_id = _active_operation_id(work_order)

    return affected_work_centers


def begin_operation_progress(work_order: WorkOrder, operation: WorkOrderOperation) -> None:
    """Lift a RELEASED WO to IN_PROGRESS and stamp ``actual_start`` on first progress.

    Used by the additive verbs (clock_out / production) and the partial-complete
    branch where the operation does NOT finish but the WO should reflect that work
    has started. Keeps ``actual_start`` stamping (DUP-2) and ``current_operation_id``
    population (RUP-1) consistent with the finalizer without forcing a rollup.
    """
    if work_order.status == WorkOrderStatus.RELEASED:
        work_order.status = WorkOrderStatus.IN_PROGRESS
        if not work_order.actual_start:
            work_order.actual_start = operation.actual_start or datetime.utcnow()
    if work_order.status in (WorkOrderStatus.RELEASED, WorkOrderStatus.IN_PROGRESS):
        work_order.current_operation_id = _active_operation_id(work_order)


def work_order_operation_progress(work_order: WorkOrder) -> dict:
    """Return route-progress metrics without changing finished WO quantity.

    Component operations can complete before the parent assembly is finished.
    Those completions should move the progress bar, but they should not be
    counted as finished work-order quantity for shipping or closeout.

    Operation rows can also be regenerated while preserving the same human job
    identity. In that case, count one progress slot per natural operation and
    let an older completed row satisfy the matching current row.
    """
    operations = list(work_order.operations or [])
    if not operations:
        quantity_ordered = float(work_order.quantity_ordered or 0)
        quantity_complete = float(work_order.quantity_complete or 0)
        progress_percent = (
            min(100.0, max(0.0, (quantity_complete / quantity_ordered) * 100.0)) if quantity_ordered > 0 else 0.0
        )
        return {
            "operation_count": 0,
            "operations_complete": 0,
            "operation_progress_percent": round(progress_percent, 1),
        }

    progress_by_key: dict[tuple, float] = {}
    completed_by_key: dict[tuple, bool] = {}
    for operation in operations:
        key = _operation_progress_key(operation)
        target_qty = operation_target_quantity(operation, work_order)
        complete_qty = float(operation.quantity_complete or 0)
        has_completion_evidence = _operation_has_completion_evidence(operation)

        if has_completion_evidence:
            ratio = 1.0
        elif target_qty > 0:
            ratio = min(1.0, max(0.0, complete_qty / target_qty))
        else:
            ratio = 0.0

        progress_by_key[key] = max(progress_by_key.get(key, 0.0), ratio)
        completed_by_key[key] = completed_by_key.get(key, False) or has_completion_evidence

    total_operations = len(progress_by_key)
    operations_complete = sum(1 for is_complete in completed_by_key.values() if is_complete)
    progress_total = sum(progress_by_key.values())
    return {
        "operation_count": total_operations,
        "operations_complete": operations_complete,
        "operation_progress_percent": round(
            (progress_total / total_operations) * 100.0,
            1,
        ),
    }


def reconcile_work_orders_from_completion_evidence(
    db: Session,
    work_orders: list[WorkOrder],
    transitions: Optional[list[StatusTransition]] = None,
) -> bool:
    """Repair operation rows from durable shop-floor completion evidence.

    AUD-3: when ``transitions`` is provided, every terminal status change this
    reconcile drives (operation→COMPLETE, work_order→COMPLETE) is appended to it,
    tagged with the contributing TimeEntry ids, so the read handler that owns
    ``current_user`` can emit a tamper-evident ``audit_log`` status-change row per
    transition before its commit. The reconcile itself has no actor and never
    writes the audit chain. Passing ``None`` preserves the legacy unaudited
    behavior for callers that have no actor (e.g. a brand-new WO POST where this is
    a documented no-op).
    """
    # G6-A: skip the OPERATION-level reconcile for any TERMINAL parent WO
    # (CANCELLED/CLOSED/COMPLETE). The WO-level _sync_work_order_status_from_operations
    # already early-returns for terminal WOs, but this op-level loop ran over ALL
    # operations regardless -- so a CANCELLED WO with closed TimeEntry evidence could
    # still have operation.quantity_complete bumped or an op flipped to COMPLETE during
    # a read-path reconcile. Excluding terminal WOs' operations from the candidate set
    # leaves their committed op state untouched. Read-safe: never raises.
    non_terminal_work_orders = [wo for wo in work_orders if wo.status not in TERMINAL_WO_STATUSES]
    operations = [op for wo in non_terminal_work_orders for op in (wo.operations or [])]
    operation_ids = [op.id for op in operations if op.id is not None]
    if not operation_ids:
        return False

    changed = False
    # AUD-3: the contributing TimeEntry ids are only needed to enrich audit rows,
    # so this extra lookup runs ONLY when a caller wants the transitions audited.
    # Read paths that pass ``transitions=None`` (or have no actor) skip it entirely,
    # keeping the reconcile-on-read query count unchanged.
    entry_ids_by_operation: dict[int, list[int]] = {}
    if transitions is not None:
        for op_id, entry_id in (
            db.query(TimeEntry.operation_id, TimeEntry.id).filter(TimeEntry.operation_id.in_(operation_ids)).all()
        ):
            if op_id is not None and entry_id is not None:
                entry_ids_by_operation.setdefault(op_id, []).append(entry_id)

    produced_by_operation: dict[int, tuple[float, float]] = {}
    for row in (
        db.query(
            TimeEntry.operation_id,
            func.coalesce(func.sum(TimeEntry.quantity_produced), 0).label("quantity_produced"),
            func.coalesce(func.sum(TimeEntry.quantity_scrapped), 0).label("quantity_scrapped"),
        )
        .filter(TimeEntry.operation_id.in_(operation_ids))
        .group_by(TimeEntry.operation_id)
        .all()
    ):
        if row.operation_id is not None:
            produced_by_operation[row.operation_id] = (
                float(row.quantity_produced or 0),
                float(row.quantity_scrapped or 0),
            )

    closed_produced_by_operation: dict[int, float] = {}
    for row in (
        db.query(
            TimeEntry.operation_id,
            func.coalesce(func.sum(TimeEntry.quantity_produced), 0).label("quantity_produced"),
        )
        .filter(TimeEntry.operation_id.in_(operation_ids), TimeEntry.clock_out.isnot(None))
        .group_by(TimeEntry.operation_id)
        .all()
    ):
        if row.operation_id is not None:
            closed_produced_by_operation[row.operation_id] = float(row.quantity_produced or 0)

    latest_entry_by_operation: dict[int, TimeEntry] = {}
    latest_entries = (
        db.query(TimeEntry)
        .filter(TimeEntry.operation_id.in_(operation_ids), TimeEntry.clock_out.isnot(None))
        .order_by(TimeEntry.operation_id, TimeEntry.clock_out.desc())
        .all()
    )
    for entry in latest_entries:
        if entry.operation_id is not None and entry.operation_id not in latest_entry_by_operation:
            latest_entry_by_operation[entry.operation_id] = entry

    # Process-sheet completion gate (PR 3): evidence-at-target must not auto-complete
    # an operation whose required snapshot steps lack live conforming records — the
    # same predicate the /complete endpoints and the clock-out path enforce; without
    # it, this read-time reconcile would undo their refusals on the next page load.
    # Quantities still reconcile below; only the COMPLETE flip is withheld. Function-
    # local import: this module is a low-level dependency of many services (several of
    # which import process_sheet_service themselves), so keeping the reverse edge out
    # of the module import graph avoids ever creating a cycle as either side grows.
    from app.services.process_sheet_service import gated_operation_ids

    step_gated_operation_ids = gated_operation_ids(db, operations)

    for operation in operations:
        produced_qty, scrapped_qty = produced_by_operation.get(operation.id, (0.0, 0.0))
        if produced_qty > float(operation.quantity_complete or 0):
            operation.quantity_complete = produced_qty
            changed = True
        if scrapped_qty > float(operation.quantity_scrapped or 0):
            operation.quantity_scrapped = scrapped_qty
            changed = True
        old_op_status = operation.status.value if operation.status else None
        op_changed = _sync_operation_status_from_quantity(
            operation,
            latest_entry_by_operation.get(operation.id),
            closed_produced_by_operation.get(operation.id, 0.0) >= operation_target_quantity(operation),
            completion_gated=operation.id in step_gated_operation_ids,
        )
        if (
            op_changed
            and operation.status == OperationStatus.COMPLETE
            and old_op_status != OperationStatus.COMPLETE.value
        ):
            _record_transition(
                transitions,
                resource_type="work_order_operation",
                resource_id=operation.id,
                resource_identifier=operation.operation_number,
                old_status=old_op_status,
                new_status=OperationStatus.COMPLETE.value,
                work_order_number=operation.work_order.work_order_number if operation.work_order else None,
                work_order_id=operation.work_order_id,
                time_entry_ids=entry_ids_by_operation.get(operation.id, []),
            )
        changed = op_changed or changed

    for work_order in work_orders:
        # G6-A: a terminal WO is done -- never copy slot completion evidence onto its
        # operations (which would flip ops to COMPLETE / bump quantity_complete) nor
        # re-derive its WO status. _sync_work_order_status_from_operations already
        # self-guards for terminal WOs; _copy_slot_completion_evidence did NOT, so
        # skipping the whole pair here closes that op-level hole. Read-safe.
        if work_order.status in TERMINAL_WO_STATUSES:
            continue
        changed = _copy_slot_completion_evidence(work_order, transitions, entry_ids_by_operation) or changed
        changed = _sync_work_order_status_from_operations(work_order, transitions, entry_ids_by_operation) or changed

    return changed


def _record_transition(
    transitions: Optional[list[StatusTransition]],
    *,
    resource_type: str,
    resource_id: Optional[int],
    resource_identifier: Optional[str],
    old_status: Optional[str],
    new_status: str,
    work_order_number: Optional[str] = None,
    work_order_id: Optional[int] = None,
    time_entry_ids: Optional[list[int]] = None,
) -> None:
    if transitions is None or resource_id is None:
        return
    transitions.append(
        StatusTransition(
            resource_type=resource_type,
            resource_id=resource_id,
            resource_identifier=resource_identifier,
            old_status=old_status,
            new_status=new_status,
            work_order_number=work_order_number,
            work_order_id=work_order_id,
            time_entry_ids=list(time_entry_ids or []),
        )
    )


def _operation_progress_key(operation: WorkOrderOperation) -> tuple:
    if operation.sequence is not None:
        return ("sequence", int(operation.sequence))
    operation_number = _normalized_operation_number(operation.operation_number)
    if operation_number:
        return ("operation_number", operation_number)
    name = " ".join((operation.name or "").strip().lower().split())
    return (
        operation.work_center_id,
        operation.component_part_id,
        operation.operation_group,
        name or operation.operation_number or operation.sequence or operation.id,
    )


def _operation_has_completion_evidence(operation: WorkOrderOperation) -> bool:
    return operation.status == OperationStatus.COMPLETE or (
        operation.actual_end is not None and operation.completed_by is not None
    )


def _normalized_operation_number(operation_number: Optional[str]) -> Optional[str]:
    if not operation_number:
        return None
    digits = "".join(ch for ch in str(operation_number) if ch.isdigit())
    return digits or " ".join(str(operation_number).strip().lower().split()) or None


def _sync_operation_status_from_quantity(
    operation: WorkOrderOperation,
    latest_entry: Optional[TimeEntry] = None,
    has_closed_completion_evidence: bool = False,
    completion_gated: bool = False,
) -> bool:
    """Reconcile an operation's status from its quantities + closed labor evidence.

    ``completion_gated`` (PR 3): True when the operation's required process-sheet
    steps are missing conforming records — the evidence-driven COMPLETE flip is then
    withheld (quantities and the PENDING/READY -> IN_PROGRESS lift still apply), so
    the read-time reconcile can never complete an operation the /complete endpoints
    and the clock-out path would refuse.
    """
    target_qty = operation_target_quantity(operation)
    if target_qty <= 0:
        return False

    quantity_complete = float(operation.quantity_complete or 0)
    changed = False
    if operation.status == OperationStatus.COMPLETE:
        if not operation.actual_end and latest_entry:
            operation.actual_end = latest_entry.clock_out
            changed = True
        if not operation.completed_by and latest_entry:
            operation.completed_by = latest_entry.user_id
            changed = True
        if not operation.actual_start and latest_entry:
            operation.actual_start = latest_entry.clock_in
            changed = True
        if not operation.started_by and latest_entry:
            operation.started_by = latest_entry.user_id
            changed = True
    elif quantity_complete >= target_qty and has_closed_completion_evidence and not completion_gated:
        operation.status = OperationStatus.COMPLETE
        operation.actual_end = operation.actual_end or (latest_entry.clock_out if latest_entry else None)
        operation.completed_by = operation.completed_by or (latest_entry.user_id if latest_entry else None)
        operation.actual_start = operation.actual_start or (latest_entry.clock_in if latest_entry else None)
        operation.started_by = operation.started_by or (latest_entry.user_id if latest_entry else None)
        # MS-5: free the schedule reservation for a reconcile-driven completion too.
        release_operation_schedule_reservation(operation)
        changed = True
    elif quantity_complete > 0 and operation.status in (OperationStatus.PENDING, OperationStatus.READY):
        operation.status = OperationStatus.IN_PROGRESS
        operation.actual_start = operation.actual_start or (latest_entry.clock_in if latest_entry else None)
        operation.started_by = operation.started_by or (latest_entry.user_id if latest_entry else None)
        changed = True

    return changed


def _copy_slot_completion_evidence(
    work_order: WorkOrder,
    transitions: Optional[list[StatusTransition]] = None,
    entry_ids_by_operation: Optional[dict[int, list[int]]] = None,
) -> bool:
    changed = False
    operations_by_key: dict[tuple, list[WorkOrderOperation]] = {}
    for operation in work_order.operations or []:
        operations_by_key.setdefault(_operation_progress_key(operation), []).append(operation)

    for slot_operations in operations_by_key.values():
        completed_source = next((op for op in slot_operations if _operation_has_completion_evidence(op)), None)
        if not completed_source:
            continue

        for operation in slot_operations:
            target_qty = operation_target_quantity(operation, work_order)
            if target_qty > 0 and float(operation.quantity_complete or 0) < target_qty:
                operation.quantity_complete = target_qty
                changed = True
            if operation.quantity_scrapped is None and completed_source.quantity_scrapped is not None:
                operation.quantity_scrapped = completed_source.quantity_scrapped
                changed = True
            if operation.status != OperationStatus.COMPLETE:
                old_op_status = operation.status.value if operation.status else None
                operation.status = OperationStatus.COMPLETE
                release_operation_schedule_reservation(operation)  # MS-5
                changed = True
                _record_transition(
                    transitions,
                    resource_type="work_order_operation",
                    resource_id=operation.id,
                    resource_identifier=operation.operation_number,
                    old_status=old_op_status,
                    new_status=OperationStatus.COMPLETE.value,
                    work_order_number=work_order.work_order_number,
                    work_order_id=work_order.id,
                    time_entry_ids=(entry_ids_by_operation or {}).get(operation.id, []),
                )
            if not operation.actual_end and completed_source.actual_end:
                operation.actual_end = completed_source.actual_end
                changed = True
            if not operation.completed_by and completed_source.completed_by:
                operation.completed_by = completed_source.completed_by
                changed = True
            if not operation.actual_start and completed_source.actual_start:
                operation.actual_start = completed_source.actual_start
                changed = True
            if not operation.started_by and completed_source.started_by:
                operation.started_by = completed_source.started_by
                changed = True

    return changed


def _sync_work_order_status_from_operations(
    work_order: WorkOrder,
    transitions: Optional[list[StatusTransition]] = None,
    entry_ids_by_operation: Optional[dict[int, list[int]]] = None,
) -> bool:
    operations = list(work_order.operations or [])
    if not operations:
        return False

    # G6-A: reconcile-on-read has no actor and runs on every WO GET. A terminal WO
    # (CANCELLED/CLOSED/COMPLETE) has finished its lifecycle; never let operation
    # evidence reopen it to IN_PROGRESS or resurrect a CANCELLED job to COMPLETE
    # (which would re-fire FG receipt/backflush/cost rollup and write a COMPLETE row
    # onto the tamper-evident audit chain). Leave a terminal WO's status and
    # current_operation_id exactly as committed.
    if work_order.status in TERMINAL_WO_STATUSES:
        return False

    changed = False
    all_operations_complete = all(operation.status == OperationStatus.COMPLETE for operation in operations)
    any_operation_progress = any(
        operation.status in (OperationStatus.IN_PROGRESS, OperationStatus.COMPLETE)
        or float(operation.quantity_complete or 0) > 0
        for operation in operations
    )

    if all_operations_complete:
        # DUP-2: stamp actual_end first, then actual_start clamped at actual_end,
        # BEFORE the COMPLETE flip -- IDENTICAL to finalize_operation_completion so
        # reconcile-on-read and the live finalizer agree. This guarantees a terminal
        # WO never carries actual_end with a NULL or LATER actual_start (negative
        # cycle time). When no op date exists, fall back to now; the actual_start
        # fallback is clamped at actual_end so it can never land after it.
        now = datetime.utcnow()
        if not work_order.actual_end:
            completed_dates = [operation.actual_end for operation in operations if operation.actual_end]
            work_order.actual_end = max(completed_dates) if completed_dates else now
            changed = True
        if not work_order.actual_start:
            started_dates = [operation.actual_start for operation in operations if operation.actual_start]
            work_order.actual_start = min(started_dates) if started_dates else min(now, work_order.actual_end)
            changed = True
        # G6-A: TERMINAL_WO_STATUSES (not just COMPLETE/CLOSED) -- the early terminal
        # guard above already returns before here, so this is defense-in-depth that
        # keeps this check identical to finalize_operation_completion's COMPLETE flip.
        if work_order.status not in TERMINAL_WO_STATUSES:
            old_wo_status = work_order.status.value if work_order.status else None
            work_order.status = WorkOrderStatus.COMPLETE
            changed = True
            _record_wo_complete_transition(work_order, operations, transitions, entry_ids_by_operation, old_wo_status)
        target_qty = float(work_order.quantity_ordered or 0)
        if target_qty > 0 and float(work_order.quantity_complete or 0) < target_qty:
            work_order.quantity_complete = target_qty
            changed = True
        # RUP-1: a completed WO is no longer sitting on any operation.
        if work_order.current_operation_id is not None:
            work_order.current_operation_id = None
            changed = True
    elif any_operation_progress and work_order.status == WorkOrderStatus.RELEASED:
        work_order.status = WorkOrderStatus.IN_PROGRESS
        changed = True
        started_dates = [operation.actual_start for operation in operations if operation.actual_start]
        if started_dates and not work_order.actual_start:
            work_order.actual_start = min(started_dates)
            changed = True

    # RUP-1: keep current_operation_id pointing at the active/next op while the WO
    # is still in flight, so reconcile-on-read repairs the historically-dead column
    # the same way the live finalizer populates it.
    if work_order.status in (WorkOrderStatus.RELEASED, WorkOrderStatus.IN_PROGRESS):
        active_op_id = _active_operation_id(work_order)
        if work_order.current_operation_id != active_op_id:
            work_order.current_operation_id = active_op_id
            changed = True

    return changed


def _record_wo_complete_transition(
    work_order: WorkOrder,
    operations: list[WorkOrderOperation],
    transitions: Optional[list[StatusTransition]],
    entry_ids_by_operation: Optional[dict[int, list[int]]],
    old_status: Optional[str],
) -> None:
    entry_ids: list[int] = []
    for operation in operations:
        entry_ids.extend((entry_ids_by_operation or {}).get(operation.id, []))
    # MS-2: capture the affected work centers so the read handler can refresh their
    # cached availability_rate (a reconcile-driven WO completion otherwise leaves
    # capacity looking consumed).
    work_center_ids = sorted({op.work_center_id for op in operations if op.work_center_id})
    if transitions is None or work_order.id is None:
        return
    transitions.append(
        StatusTransition(
            resource_type="work_order",
            resource_id=work_order.id,
            resource_identifier=work_order.work_order_number,
            old_status=old_status,
            new_status=WorkOrderStatus.COMPLETE.value,
            work_order_number=work_order.work_order_number,
            work_order_id=work_order.id,
            time_entry_ids=entry_ids,
            work_center_ids=work_center_ids,
        )
    )


def validate_operation_quantity(quantity_complete: float, target_qty: float) -> None:
    if math.isnan(quantity_complete) or math.isinf(quantity_complete):
        raise WorkOrderStateError("Quantity must be a valid number")
    if quantity_complete < 0:
        raise WorkOrderStateError("Quantity cannot be negative")
    if target_qty <= 0:
        raise WorkOrderStateError("Operation quantity ordered is missing or invalid")
    if quantity_complete > target_qty:
        raise WorkOrderStateError(f"Quantity ({quantity_complete}) cannot exceed quantity ordered ({target_qty})")
