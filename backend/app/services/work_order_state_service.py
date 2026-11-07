"""Shared work-order state rules used by office and shop-floor flows."""

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Iterable, Literal, Optional, Sequence, Union

from sqlalchemy import and_, func, or_
from sqlalchemy.exc import PendingRollbackError
from sqlalchemy.orm import Session

from app.core.observability import report_absorbed_exception
from app.db.tenant_filter import tenant_query
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.work_order import (
    OperationStatus,
    WorkOrder,
    WorkOrderOperation,
    WorkOrderStatus,
    WorkOrderType,
)

if TYPE_CHECKING:  # pragma: no cover - typing only; this module writes no audit rows itself
    from app.services.audit_service import AuditService

logger = logging.getLogger(__name__)

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


def is_laser_dispatch_work_order(work_order: Optional[WorkOrder]) -> bool:
    """True when ``work_order`` is a laser-cutting nest WO -- a DISPATCH POOL, not a routing.

    Laser-nest work orders hold one operation per nest sheet, and the nests have
    no process ordering: operators bump around between nests in any order, and
    management may spread a package's nests across multiple laser work centers.
    The sequence numbers on nest ops exist only for stable labels/ordering, so
    the predecessor gates and the one-at-a-time READY promotion that protect a
    real routing must NOT apply to these WOs. Every site that exempts laser WOs
    from sequence rules routes through this predicate so the rule cannot drift.

    ``work_order_type`` is a plain string column; compare against the enum's
    ``.value`` (defensively unwrapping in case an enum member is ever assigned
    in memory), matching the existing comparisons elsewhere.
    """
    if work_order is None:
        return False
    wo_type = work_order.work_order_type
    wo_type_value = wo_type.value if hasattr(wo_type, "value") else wo_type
    return wo_type_value == WorkOrderType.LASER_CUTTING.value


def work_order_allows_same_work_center(work_order: Optional[WorkOrder]) -> bool:
    """Is this work order a DISPATCH POOL (True) or a sequenced ROUTING (False)?

    THE one resolver for ``WorkOrder.sequential_operations`` (migration 081). Both
    predecessor gates read it and nothing else interprets the column, because the
    promotion rule and the clock-in gate are deliberately ONE rule: an operation that
    is invisible on the dispatch board must also be refused by a badge scan, and an
    operation a badge can start must be visible. Two readings of this flag is exactly
    how those two rules would drift apart.

    Returns the value the gates want as ``allow_same_work_center``:

    * ``sequential_operations=True`` -> ``False``: operations sharing a work center DO
      block each other, so a 4-operation weld assembly on one cell unlocks one step at
      a time (the reported WO-20260807-006 behavior).
    * ``sequential_operations=False`` -> ``True``: the pooled rule, unchanged -- the
      18-item press-brake / weld-subassembly batch WOs and imported laser packages show
      every item at once.

    Defaults to POOLED (``True``) whenever the flag cannot be read -- a ``None`` work
    order, or a row/stub predating the column. That is the pre-081 behavior every caller
    had hard-coded, so an unresolvable work order can never silently TIGHTEN a gate and
    strand work the floor was already allowed to start.

    Laser WOs never reach a question this resolver can answer: ``is_laser_dispatch_work_order``
    short-circuits above it at every seam and is strictly fuller (it drops predecessor
    gating entirely, across work centers). The column is inert on those rows.
    """
    if work_order is None:
        return True
    return not bool(getattr(work_order, "sequential_operations", False))


def operation_blocked_by_incomplete_predecessors(
    candidate: WorkOrderOperation,
    incomplete_operations: Iterable[WorkOrderOperation],
    allow_same_work_center: bool = True,
) -> bool:
    """THE promotion gate, in memory: the same rule CLOCK-IN uses.

    Replicates ``has_incomplete_predecessors(db, wo_id, candidate.sequence,
    current_operation_id=candidate.id, current_work_center_id=candidate.work_center_id,
    allow_same_work_center=<same value>)`` EXACTLY -- "exists an op of THIS work order
    with ``sequence < candidate.sequence`` AND ``status != COMPLETE`` AND ``id !=
    candidate.id``" and, WHEN ``allow_same_work_center`` is True, additionally AND
    (``work_center_id != candidate.work_center_id`` OR ``status == ON_HOLD``).

    That last disjunct is the HOLD CARVE-OUT and it is load-bearing: a held predecessor
    blocks from any work center, its own included, so a quality/material stop takes the
    whole pool off the board instead of leaving 17 siblings startable around it. Keep
    this branch and the SQL edited together -- "replicates EXACTLY" is a claim the
    promotion rule and every clock-in gate both depend on.

    ``allow_same_work_center`` is NO LONGER A CONSTANT (081). Both this predicate and the
    SQL twin are handed the value resolved by ``work_order_allows_same_work_center`` from
    the work order's ``sequential_operations`` column -- by ``promote_ready_operations``
    on the promotion side and by ``operation_action_gates.operation_blocked_by_predecessors``
    on the clock-in/start/complete side. Both readings come from the SAME resolver and the
    SAME work order, which is what keeps promotion and the action gates one rule; passing
    a literal at a new call site is how they would drift apart again.

    True (a DISPATCH POOL) is the historical behavior and stays the default here:
    operations sharing a work center have always been legal to work in any order, and
    promotion once used a STRICTER rule with no such allowance, so an operation could be
    legal to clock into and yet never reach READY -- invisible, because the dispatch board
    and kiosk surface READY work only (``dispatch_service.QUEUE_OPERATION_STATUSES``).
    False (a sequenced ROUTING) is what a real route wants: a 4-operation weld assembly on
    one cell unlocks one step at a time instead of offering all four at once.

    The same-work-center allowance applies ONLY when the candidate actually has a work
    center, because the query it mirrors applies its ``work_center_id !=
    current_work_center_id`` filter only when ``current_work_center_id is not None``. A
    work-center-less operation therefore keeps the strict gate rather than silently
    exempting itself from every predecessor. In the mirror image -- a PREDECESSOR with no
    work center -- this is deliberately one notch STRICTER than the SQL: three-valued
    logic drops a ``NULL != :wc`` row from the COUNT, so the query would not let it block,
    while here it does. ``WorkOrderOperation.work_center_id`` is ``nullable=False``, so no
    persisted row can reach that difference; over-blocking (an op stays PENDING) is the
    safe side of it if one ever does.

    ``incomplete_operations`` must be every not-COMPLETE operation of the work order,
    materialized ONCE by the caller -- this predicate issues no query, which is what keeps
    ``release_next_ready_operation`` free of the PERF-4 N+1.
    """
    for other in incomplete_operations:
        if other.id == candidate.id:
            continue
        if other.sequence >= candidate.sequence:
            continue
        if (
            # 081: the waiver itself is now conditional. False == this WO is a sequenced
            # routing, so a same-work-center predecessor blocks like any other and the
            # rule collapses back to plain "every lower sequence must be COMPLETE".
            allow_same_work_center
            and candidate.work_center_id is not None
            and other.work_center_id == candidate.work_center_id
            # The hold carve-out: a held sibling is NOT waived, so it still blocks.
            and other.status != OperationStatus.ON_HOLD
        ):
            continue
        return True
    return False


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
    # Owner ruling 1 (INT3): for an operation -> COMPLETE transition, the accounting the
    # flip was decided on -- ``{target, received, upstream_quantity_short}`` -- merged into
    # the audit row's extra_data by both reconcile audit emitters, so a stage completed at
    # fewer than its target because an upstream close was declared short says so.
    extra_data: dict = field(default_factory=dict)


def report_reconcile_leg_failure(
    log: Union[logging.Logger, logging.LoggerAdapter],
    step: str,
    exc: BaseException,
    *,
    company_id: Optional[int],
    user: Optional[Any],
    transitions: Sequence[StatusTransition],
    summary: Optional[str] = None,
) -> None:
    """Log + report a reconcile-on-read side-effect leg that failed and was absorbed.

    The read handlers' reconcile legs (audit, events, parent advance, scheduling,
    inventory effects, cost rollup) each swallow their own failure so a GET can never
    500 on one. They used to swallow it SILENTLY -- a bare ``except Exception: pass`` --
    so a reconcile that committed status flips without their audit rows, or without
    the FG receipt / backflush the completion owed, left no trace anywhere. This makes
    each one an ERROR log line (traceback + the work orders, operations and company it
    was reconciling) and a Sentry event tagged ``reconcile_step``.

    Reporting only: the caller's control flow and transaction are unchanged, and this
    never raises. ``log`` is the CALLER's logger, so the record is attributed to the
    router whose read triggered the reconcile. ``user`` is read defensively: the leg may
    have failed on a database error, and an expired ORM instance would try to reload
    from that failed session. ``summary`` replaces the default message when the leg did
    not stop (e.g. the audit leg reporting rows ``AuditService`` absorbed).

    A ``PendingRollbackError`` is NOT a new failure and is logged at WARNING with no
    Sentry event: an earlier step's database error already left the session needing a
    rollback, so every later leg that touches the session raises it, and the handler's
    commit will fail and roll the whole reconcile back. The cause was already reported
    where it happened (that leg's own report, ``AuditService``'s "Failed to create audit
    log" line, or the READY-promotion heal's), and the exception text quotes it. Reporting
    each echo as its own ERROR + Sentry event would spend up to five events per read of
    the same work order -- on every read, since the rollback makes the next one retry --
    and bury the cause under them.
    """
    context: dict[str, Any] = {"reconcile_step": step, "company_id": company_id}
    try:
        context["user_id"] = getattr(user, "id", None)
    except Exception:  # an expired instance on a failed session cannot reload -- skip it
        context["user_id"] = None
    try:
        context["work_order_ids"] = sorted(
            {
                tr.work_order_id if tr.work_order_id is not None else tr.resource_id
                for tr in transitions
                if tr.work_order_id is not None or tr.resource_type == "work_order"
            }
        )
        context["operation_ids"] = sorted(
            {tr.resource_id for tr in transitions if tr.resource_type == "work_order_operation"}
        )
    except Exception:  # pragma: no cover - context is best-effort; the report must still go out
        pass
    if isinstance(exc, PendingRollbackError):
        rendered = " ".join(f"{key}={value}" for key, value in context.items())
        try:
            log.warning(
                "Reconcile-on-read %s leg skipped: the session already failed earlier in this reconcile, "
                "and the handler's commit will roll it back [%s]: %s",
                step,
                rendered,
                exc,
                extra={"extra_data": dict(context)},
            )
        except Exception:  # pragma: no cover - logging must never be fatal
            pass
        return
    report_absorbed_exception(
        log,
        summary or f"Reconcile-on-read {step} leg failed; its remaining writes were skipped and the read continues",
        exc,
        context=context,
    )


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
    """True when a lower-sequence operation of this WO is not COMPLETE (and so blocks).

    ``allow_same_work_center`` waives the block for predecessors sitting at the SAME work
    center as the candidate -- operations on one machine carry no process ordering among
    themselves, so they are mutually startable. Since 081 it is resolved per work order by
    ``work_order_allows_same_work_center`` (False on a sequenced routing) rather than
    hard-coded True at the call site; the in-memory twin
    ``operation_blocked_by_incomplete_predecessors`` takes the identical parameter and the
    two must keep replicating each other exactly.

    THE HOLD CARVE-OUT: that waiver does NOT extend to an ``ON_HOLD`` predecessor. A hold
    is a quality or material stop placed to keep the shop from building past a known
    problem, so it must outrank the pooling convenience: while item 3 of a batch is held,
    its 17 siblings stop being startable rather than staying on the board. An ON_HOLD op
    therefore blocks from ANY work center, its own included.

    That deliberately TIGHTENS clock-in as well, because this predicate is shared --
    ``operation_action_gates.operation_blocked_by_predecessors`` (clock-in, shop-floor
    start/complete, the scanner resolver) and the READY-promotion rule all read it, and a
    hold that stopped the board but not the badge scanner would be no stop at all. It is
    the intended behavior of the carve-out, not a side effect of it.
    """
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
        # Waive the same-work-center block, EXCEPT for a held op -- see the docstring.
        query = query.filter(
            or_(
                WorkOrderOperation.work_center_id != current_work_center_id,
                WorkOrderOperation.status == OperationStatus.ON_HOLD,
            )
        )
    return query.count() > 0


def promote_ready_operations(
    work_order: WorkOrder,
    operations: Iterable[WorkOrderOperation],
    db: Optional[Session] = None,
    user_id: Optional[int] = None,
) -> list[WorkOrderOperation]:
    """THE PENDING -> READY promotion. One implementation; four seams call it.

    Flips every startable PENDING operation of ``work_order`` to READY and returns
    them in ascending sequence order (empty list when nothing was startable). The
    four seams that promote -- WO release (``release_first_ready_operation``),
    operation completion (``release_next_ready_operation``), the read-path heal
    (``_promote_stranded_ready_operations``, from
    ``reconcile_work_orders_from_completion_evidence``) and the kiosk/desktop
    resume (``shop_floor.resume_operation``, which floors at PENDING and delegates
    the lift here precisely so a resume can never perform a release) -- differ ONLY
    in how they obtain ``operations``; the rule itself lives here exactly once.
    Four copies of this rule is how the office and floor gates drifted apart in the
    first place, so resist re-inlining it.

    "Startable" is ``operation_blocked_by_incomplete_predecessors`` -- the SAME rule
    clock-in enforces. Whether operations sharing a work center block each other is now
    the work order's own ``sequential_operations`` setting, resolved ONCE per call through
    ``work_order_allows_same_work_center`` (081) and handed to the gate:

    * POOLED (the pre-081 rule, and what every pre-081 row backfilled to): they do not
      block each other, so a work order carrying N unordered items at ONE work center
      promotes all N instead of stranding N-1 in PENDING where no dispatch surface can
      show them. Cross-work-center ordering still holds.
    * SEQUENCED (the default for new work orders): they do block, so the route unlocks one
      step at a time -- completing an operation is what promotes the next.

    Either way a routing whose steps each sit at a DIFFERENT work center promotes exactly
    one op, which is why the two modes are indistinguishable on such a route.

    Laser-nest WOs (``is_laser_dispatch_work_order``) keep their own, STRICTLY FULLER
    exemption: they drop predecessor gating ENTIRELY, so every PENDING nest op promotes
    even across work centers (a package's nests get spread over two lasers, which the
    same-work-center allowance above would not cover), and a HELD nest does not block its
    siblings. It also heals pre-existing laser WOs (imported before whole-package-ready)
    whose nests 2+ are still PENDING. The two rules must not be collapsed into one.

    ``operations`` MUST be every operation of the work order (not just the PENDING
    ones): it supplies both the write set and the predecessor snapshot. It is
    materialized ONCE by the caller and the gate runs in memory over it, which is what
    keeps promotion free of the PERF-4 N+1 -- promoting N ops costs the caller the same
    single query as promoting one. The snapshot stays correct as promotions land: an op
    flipped to READY is still not COMPLETE, so it keeps blocking its cross-work-center
    successors exactly as the query-backed gate would.

    Invariant 1: rows whose ``company_id`` disagrees with the work order's are dropped
    up front, from the write set AND the predecessor snapshot -- the in-memory
    equivalent of the ``tenant_query`` scoping ``release_next_ready_operation`` applies
    to its own load, so a caller that hands over a relationship rather than a scoped
    query cannot widen a mis-parented row into a write.

    Lean Phase 1 (flow metrics): when ``db`` is provided, each flip also emits a
    best-effort ``operation_ready`` OperationalEvent so queue time can be measured as
    ready -> actual_start. ``db`` stays optional so this remains usable as a pure
    in-memory rule (existing call sites/tests without a session keep working); the emit
    helper lives in ``completion_signal_service`` (the side-effects module) and is
    imported locally, once per call, to preserve that layering. All four production
    seams pass ``db``.

    "One ``operation_ready`` per operation" is NOT an invariant. The other three
    seams only ever see a given operation flip once, but resume can put an already
    -READY operation back through PENDING, so a hold/resume cycle adds a second
    event. That is harmless by construction and must stay so: every consumer
    anchors on the EARLIEST event (``flow_metrics_service._ready_times_by_operation``
    is ``func.min(occurred_at)``). Do not rewrite a consumer to last-wins without
    dealing with this -- it would silently erase queue time on every held job.
    """
    company_id = work_order.company_id
    scoped_ops = [
        op
        for op in operations
        if op is not None and (company_id is None or op.company_id is None or op.company_id == company_id)
    ]
    pending_ops = sorted(
        (op for op in scoped_ops if op.status == OperationStatus.PENDING),
        key=lambda op: op.sequence,
    )
    if not pending_ops:
        return []

    if is_laser_dispatch_work_order(work_order):
        to_promote = pending_ops
    else:
        # 081: resolved ONCE per call off the work order, never per candidate -- this is
        # the same bool operation_action_gates hands the SQL twin, so promotion and
        # clock-in cannot disagree about whether this WO is a pool or a routing.
        allow_same_work_center = work_order_allows_same_work_center(work_order)
        incomplete = [op for op in scoped_ops if op.status != OperationStatus.COMPLETE]
        to_promote = [
            op
            for op in pending_ops
            if not operation_blocked_by_incomplete_predecessors(
                op, incomplete, allow_same_work_center=allow_same_work_center
            )
        ]
    if not to_promote:
        # The promotion list can legitimately be EMPTY (every PENDING op still blocked by
        # an incomplete lower-sequence op at another work center).
        return []

    if db is None:
        # Pure in-memory rule: flip the statuses, emit nothing (no session to emit on).
        for op in to_promote:
            op.status = OperationStatus.READY
        return to_promote

    # Function-local import (the side-effects module imports back into this layer),
    # hoisted out of the loop so it is bound once per call rather than per promoted op.
    from app.services.completion_signal_service import emit_operation_ready_event

    for op in to_promote:
        op.status = OperationStatus.READY
        if op.id is not None:
            emit_operation_ready_event(
                db,
                company_id=work_order.company_id,
                work_order=work_order,
                operation=op,
                user_id=user_id,
            )
    return to_promote


def _worked_operation_ids(db: Session, company_id: int, operations: list[WorkOrderOperation]) -> set[int]:
    """Ids of the given operations that carry ANY TimeEntry -- open or closed, in ONE query.

    Existence only, tenant-scoped: the same evidence test the floor's completion gate uses
    (``operation_action_gates.operation_has_labor_evidence``), batched so a route of N
    operations costs one query instead of N.
    """
    ids = [op.id for op in operations if op.id is not None]
    if not ids:
        return set()
    return {
        row[0]
        for row in db.query(TimeEntry.operation_id)
        .filter(TimeEntry.company_id == company_id, TimeEntry.operation_id.in_(ids))
        .distinct()
        .all()
    }


def _operation_has_been_worked(operation: WorkOrderOperation, worked_operation_ids: set[int]) -> bool:
    """Has anyone actually touched this operation? Any of five independent signals."""
    return (
        operation.status == OperationStatus.IN_PROGRESS
        or operation.actual_start is not None
        or operation.id in worked_operation_ids
        or (operation.quantity_complete or 0) > 0
        or (operation.quantity_scrapped or 0) > 0
    )


def operations_worked_out_of_sequence(db: Session, work_order: WorkOrder) -> list[WorkOrderOperation]:
    """Operations a sequenced routing would block that someone has ALREADY worked.

    The pre-flight for turning ``sequential_operations`` ON, and the reason that flip can
    be REFUSED rather than merely applied. Every completion verb re-evaluates the
    predecessor gate at action time -- the shop-floor complete, the office complete,
    clock-in, the scanner resolver -- so an operation that is IN_PROGRESS ahead of its
    predecessors becomes UNCOMPLETABLE the moment the flag flips: the floor cannot finish
    it, and no read path heals it, because promotion only ever moves forward. The job
    would sit on a tablet with every button refusing it.

    So the flip asks this question first and refuses with a 409 that NAMES the operations,
    instead of quietly bricking live work. Evaluated STRICTLY and unconditionally
    (``allow_same_work_center=False``, never through ``work_order_allows_same_work_center``)
    because it runs BEFORE the column is written: it asks what the NEW rule would do, not
    what the current one does.

    Complements ``demote_operations_for_sequencing`` exactly -- this returns the blocked
    operations that HAVE been worked (refuse the flip), that one demotes the blocked
    operations that have NOT (safe to take off the board). Laser dispatch pools are exempt;
    the flag is inert on them.
    """
    if is_laser_dispatch_work_order(work_order):
        return []
    # Invariant 1: scoped to the work order's own company_id, off the tenant-resolved row.
    all_ops = (
        tenant_query(db, WorkOrderOperation, work_order.company_id)
        .filter(WorkOrderOperation.work_order_id == work_order.id)
        .order_by(WorkOrderOperation.sequence)
        .all()
    )
    incomplete = [op for op in all_ops if op.status != OperationStatus.COMPLETE]
    if not incomplete:
        return []
    worked_operation_ids = _worked_operation_ids(db, work_order.company_id, incomplete)
    return [
        op
        for op in incomplete
        if _operation_has_been_worked(op, worked_operation_ids)
        and operation_blocked_by_incomplete_predecessors(op, incomplete, allow_same_work_center=False)
    ]


# The rule-driven ways an operation can move BACKWARDS (READY -> PENDING). One core,
# ``demote_ready_operations``, with an explicit scope per caller -- never a second copy of
# the "has anyone worked it?" test or of the tenant-scoped candidate read.
#
#   "sequencing"   -- PUT /work-orders/{id} turning ``sequential_operations`` ON: demote
#                     only what the strict routing rule now blocks (via the thin wrapper
#                     ``demote_operations_for_sequencing``, whose call site is unchanged).
#   "all_unworked" -- POST /work-orders/{id}/resume landing a held job back at DRAFT (no
#                     release on record): every un-worked READY operation, laser nests
#                     included (``exempt_laser=False``), because a DRAFT work order must
#                     not leave cards on the dispatch board and the kiosk.
#   "blocked_by"   -- POST /work-orders/operations/{id}/reduce-production with
#                     ``reopen_operation`` (owner ruling 1): a COMPLETE operation an
#                     over-count correction would leave short goes back to IN_PROGRESS, and
#                     the un-worked READY operations it NOW blocks -- under the work order's
#                     own rule (``work_order_allows_same_work_center``), and only those whose
#                     block is that operation (``blocked_by``) -- return to PENDING. Reached
#                     through ``reopen_completed_operation``.
#
# A later scope adds one ``DemotionScope`` literal and one branch in
# ``_selected_for_demotion`` -- nothing else. Scheduling's
# ``PUT /scheduling/work-orders/{id}/unschedule`` also writes READY -> PENDING, but that is
# a transient scheduling reset the next reconciling read re-promotes, not a gate decision;
# it deliberately does not go through this core.
DemotionScope = Literal["sequencing", "all_unworked", "blocked_by"]
DEMOTION_SCOPES: tuple[str, ...] = ("sequencing", "all_unworked", "blocked_by")


def _selected_for_demotion(
    scope: DemotionScope,
    operation: WorkOrderOperation,
    incomplete: list[WorkOrderOperation],
    *,
    allow_same_work_center: bool = True,
    blocked_by: Optional[WorkOrderOperation] = None,
) -> bool:
    """Does ``scope`` take this (READY, un-worked) operation off the board?"""
    if scope == "sequencing":
        # The strict rule, unconditionally (allow_same_work_center=False): the lowest
        # startable operation of the route stays READY, so the floor is never left with a
        # work order it cannot start at all.
        return operation_blocked_by_incomplete_predecessors(operation, incomplete, allow_same_work_center=False)
    if scope == "all_unworked":
        return True
    if scope == "blocked_by":
        # Blocked now, under the work order's OWN rule, and only because of the re-opened
        # operation: without it in the incomplete set the gate would pass. An operation
        # some other incomplete predecessor already blocks is not this caller's business.
        assert blocked_by is not None  # guarded by demote_ready_operations
        if not operation_blocked_by_incomplete_predecessors(
            operation, incomplete, allow_same_work_center=allow_same_work_center
        ):
            return False
        without = [other for other in incomplete if other.id != blocked_by.id]
        return not operation_blocked_by_incomplete_predecessors(
            operation, without, allow_same_work_center=allow_same_work_center
        )
    raise ValueError(f"unknown demotion scope {scope!r}")  # pragma: no cover - guarded by the caller


def demote_ready_operations(
    db: Session,
    work_order: WorkOrder,
    *,
    scope: DemotionScope,
    exempt_laser: bool = True,
    blocked_by: Optional[WorkOrderOperation] = None,
) -> list[WorkOrderOperation]:
    """READY -> PENDING for un-worked operations ``scope`` selects. THE demotion core.

    The only rule-driven way anything in this system moves an operation BACKWARDS (holds
    and op-level resume are actor decisions about one specific operation; this is a rule
    applied to a route). It exists because promotion is strictly forward-only: every
    promotion seam only ever writes PENDING -> READY, so a change that makes READY wrong
    -- sequencing switched on, a held job resumed to DRAFT, a completed operation reopened
    by an office correction -- would otherwise leave those
    cards on the dispatch board and the kiosk forever. Callers and scopes are listed above
    ``DemotionScope``.

    Refuses to demote anything that has been WORKED, in every scope. An operation is left
    alone when it is not READY (IN_PROGRESS / ON_HOLD / COMPLETE / already PENDING are all
    somebody else's state to own), when it carries an ``actual_start``, when it has booked
    good or scrapped quantity, or when ANY TimeEntry exists against it -- open or closed,
    the same existence-only evidence test the floor's completion gate uses. Demoting
    worked material would strand real labor behind a gate the operator already passed.

    ``exempt_laser`` (default True) makes a laser dispatch pool a no-op -- the sequencing
    flag is inert there. The resume-to-DRAFT caller passes ``False``: a DRAFT nest package
    must not leave READY nests on the floor's board either.

    The ``"sequencing"`` scope is also a no-op on a POOLED work order: switching
    sequencing OFF needs no demotion, promotion heals that direction by itself on the next
    reconciling read.

    The ``"blocked_by"`` scope needs ``blocked_by`` (the re-opened operation, already
    written back to a not-COMPLETE status) and evaluates the gate with the work order's
    own ``work_order_allows_same_work_center`` value, resolved once here.

    Returns the demoted operations WITH THEIR STATUS ALREADY WRITTEN, so the caller (which
    holds ``current_user`` and an ``AuditService``) can log a status change per row before
    its commit -- invariant 2 (``log_demotions_required`` is the fail-closed emitter). This
    function writes no audit row itself, the same division of labor
    ``reconcile_work_orders_from_completion_evidence`` uses via StatusTransition.
    """
    if scope not in DEMOTION_SCOPES:
        raise ValueError(f"unknown demotion scope {scope!r}")
    if scope == "blocked_by" and blocked_by is None:
        raise ValueError("the 'blocked_by' demotion scope needs the re-opened operation")
    if exempt_laser and is_laser_dispatch_work_order(work_order):
        return []
    if scope == "sequencing" and work_order_allows_same_work_center(work_order):
        return []
    allow_same_work_center = work_order_allows_same_work_center(work_order) if scope == "blocked_by" else True

    # Invariant 1: scoped to the work order's OWN company_id, off the WO row the caller
    # already resolved tenant-scoped -- this both selects the rows to WRITE and supplies
    # the predecessor snapshot.
    all_ops = (
        tenant_query(db, WorkOrderOperation, work_order.company_id)
        .filter(WorkOrderOperation.work_order_id == work_order.id)
        .order_by(WorkOrderOperation.sequence)
        .all()
    )
    candidates = [op for op in all_ops if op.status == OperationStatus.READY and op.id is not None]
    if not candidates:
        return []

    worked_operation_ids = _worked_operation_ids(db, work_order.company_id, candidates)

    # The snapshot stays correct as demotions land: a demoted op is still not COMPLETE,
    # so it keeps blocking its successors exactly as it did while READY.
    incomplete = [op for op in all_ops if op.status != OperationStatus.COMPLETE]
    demoted: list[WorkOrderOperation] = []
    for op in candidates:
        if _operation_has_been_worked(op, worked_operation_ids):
            continue
        if not _selected_for_demotion(
            scope, op, incomplete, allow_same_work_center=allow_same_work_center, blocked_by=blocked_by
        ):
            continue
        op.status = OperationStatus.PENDING
        demoted.append(op)
    return demoted


def demote_operations_for_sequencing(db: Session, work_order: WorkOrder) -> list[WorkOrderOperation]:
    """READY -> PENDING for work the pooled rule promoted that a sequenced routing forbids.

    A thin wrapper over :func:`demote_ready_operations` (scope ``"sequencing"``, laser
    pools exempt), kept so the ``PUT /work-orders/{id}`` flip's call site and every test
    pinning it stay byte-identical. Turning the flag on without this fixes only the NEXT
    work order, never the one being looked at.

    Only demotes what the NEW rule actually blocks: the gate runs with
    ``allow_same_work_center=False``, so the lowest startable operation of the route stays
    READY and the floor is never left with a work order it cannot start at all. A pooled
    work order is a no-op. Never touches worked operations -- see the core's docstring.
    """
    return demote_ready_operations(db, work_order, scope="sequencing")


def log_demotions_required(
    audit: "AuditService",
    operations: Sequence[WorkOrderOperation],
    *,
    reason: str,
    extra: Optional[dict[str, Any]] = None,
) -> None:
    """One REQUIRED ``STATUS_CHANGE`` ready -> pending audit row per demoted operation.

    The fail-closed emitter for :func:`demote_ready_operations`: ``log_required`` raises
    ``AuditWriteError`` when a row cannot be written, and the caller must roll the WHOLE
    unit back (``app.api.audit_guard.required_audit`` turns that into a 503). ``reason``
    is the human sentence recorded as the row's description; ``extra`` is merged into
    ``extra_data`` beside ``operation_number`` / ``sequence`` (every other
    work_order_operation audit row identifies the operation by operation_number, so a
    filter on that identifier still finds the demotion beside its history).

    CALLERS FLUSH FIRST. ``WorkOrderOperation`` maps ``version_id_col``; if the first
    audit write is what flushes the demotions, a concurrent writer's StaleDataError is
    swallowed inside ``AuditService.log``'s savepoint instead of reaching the app-wide 409
    handler (the invariant-4 note in ``work_orders.update_work_order``).

    The sequencing flip keeps its own BEST-EFFORT rows (``log_status_change``) on purpose;
    this emitter is for callers whose demotion must not land without evidence.
    """
    for op in operations:
        work_order = op.work_order
        work_order_number = work_order.work_order_number if work_order is not None else str(op.work_order_id)
        audit.log_required(
            action=audit.ACTIONS["STATUS_CHANGE"],
            resource_type="work_order_operation",
            resource_id=op.id,
            resource_identifier=f"{work_order_number} OP{op.sequence}",
            description=f"Operation returned to pending: {reason}",
            old_values={"status": OperationStatus.READY.value},
            new_values={"status": OperationStatus.PENDING.value},
            extra_data={
                **(extra or {}),
                "operation_number": op.operation_number,
                "sequence": op.sequence,
            },
        )


# Where POST /work-orders/{id}/resume sends a held work order, and why.
HoldRestoreBasis = Literal["release_record_with_work", "release_record", "no_release_record"]


def work_order_hold_restore_target(
    work_order: WorkOrder,
    operations: Sequence[WorkOrderOperation],
    worked_operation_ids: set[int],
) -> tuple[WorkOrderStatus, HoldRestoreBasis]:
    """The status an ON_HOLD work order resumes to, and the basis it was decided on. Pure.

    Resume must never RELEASE a job: Release is the authorization step and the record of
    who put work on the floor. So a non-DRAFT target requires a RELEASE RECORD
    (``released_at`` or ``released_by``):

    * release record + work evidence -> IN_PROGRESS (``"release_record_with_work"``);
    * release record, no work evidence -> RELEASED (``"release_record"``);
    * no release record -> DRAFT (``"no_release_record"``) -- the caller demotes its
      un-worked READY operations and the job goes back through Release.

    Work evidence is any of: ``work_order.actual_start``, any COMPLETE operation, or any
    operation :func:`_operation_has_been_worked` reports (IN_PROGRESS, ``actual_start``,
    booked good/scrap quantity, any TimeEntry -- ``worked_operation_ids`` from
    :func:`_worked_operation_ids`). ``actual_start`` alone would be wrong: the read heal
    lifts RELEASED -> IN_PROGRESS without stamping it.

    No laser carve-out and no audit-row lookup: a best-effort audit row is not a state
    source (it can be missing), and since ``PUT /work-orders/{id}`` refuses DRAFT ->
    ON_HOLD every hold placed now is on released work. A legacy unstamped hold resumes to
    DRAFT -- the restrictive answer, one Release click to fix and immediately visible.
    """
    has_release_record = work_order.released_at is not None or work_order.released_by is not None
    if not has_release_record:
        return WorkOrderStatus.DRAFT, "no_release_record"
    has_work = work_order.actual_start is not None or any(
        op.status == OperationStatus.COMPLETE or _operation_has_been_worked(op, worked_operation_ids)
        for op in operations
    )
    if has_work:
        return WorkOrderStatus.IN_PROGRESS, "release_record_with_work"
    return WorkOrderStatus.RELEASED, "release_record"


@dataclass(frozen=True)
class WorkOrderResumeOutcome:
    """What :func:`apply_work_order_resume` wrote (nothing audited, nothing committed)."""

    previous_status: WorkOrderStatus
    status: WorkOrderStatus
    restore_basis: HoldRestoreBasis
    demoted_operations: list[WorkOrderOperation]
    # Every work center the work order's operations sit at (sorted, de-duplicated) -- the
    # shop-floor channels the caller broadcasts ``work_order_resumed`` on, taken from the
    # operations this function already read so the route issues no second query.
    work_center_ids: tuple[int, ...] = ()


def apply_work_order_resume(db: Session, work_order: WorkOrder) -> WorkOrderResumeOutcome:
    """Lift a work-order hold: write the restore target and, for DRAFT, the demotions.

    The caller (``POST /work-orders/{id}/resume``) has already refused a work order that
    is not ON_HOLD and holds the actor: it flushes, writes the REQUIRED audit rows and
    commits. This function writes statuses only -- no audit row, no commit, no event.

    Reads the operations tenant-scoped off the work order's own ``company_id`` (invariant
    1) and the TimeEntry evidence in ONE query. A DRAFT target demotes every un-worked
    READY operation, laser nests included, through the one demotion core.
    """
    operations = (
        tenant_query(db, WorkOrderOperation, work_order.company_id)
        .filter(WorkOrderOperation.work_order_id == work_order.id)
        .all()
    )
    worked_operation_ids = _worked_operation_ids(db, work_order.company_id, operations)
    target, basis = work_order_hold_restore_target(work_order, operations, worked_operation_ids)
    previous_status = WorkOrderStatus(getattr(work_order.status, "value", work_order.status))
    work_order.status = target
    demoted: list[WorkOrderOperation] = []
    if target == WorkOrderStatus.DRAFT:
        demoted = demote_ready_operations(db, work_order, scope="all_unworked", exempt_laser=False)
    return WorkOrderResumeOutcome(
        previous_status=previous_status,
        status=target,
        restore_basis=basis,
        demoted_operations=demoted,
        work_center_ids=tuple(sorted({op.work_center_id for op in operations if op.work_center_id})),
    )


def release_first_ready_operation(
    work_order: WorkOrder,
    db: Optional[Session] = None,
    user_id: Optional[int] = None,
) -> Optional[WorkOrderOperation]:
    """Promote every startable PENDING op to READY at WO release. Returns the lowest one.

    Thin seam over ``promote_ready_operations`` (which owns the rule and documents it):
    the operation set is the already-loaded ``work_order.operations`` relationship, so
    this issues no query of its own.

    ``user_id`` is the releasing user -- this is the one promotion seam with a human
    actor behind it, so its ``operation_ready`` events carry attribution; the completion
    and reconcile seams are rule-driven and pass None.
    """
    promoted = promote_ready_operations(work_order, work_order.operations or [], db=db, user_id=user_id)
    return promoted[0] if promoted else None


def release_next_ready_operation(
    db: Session,
    work_order: WorkOrder,
    completed_op: WorkOrderOperation,
) -> Optional[WorkOrderOperation]:
    """Promote EVERY PENDING op that is now startable to READY. Returns the lowest one.

    RUP-4: this is intentionally self-healing rather than strictly forward-only.
    The shop-floor scan/complete path completes ops out of sequence within the same
    work center (``allow_same_work_center=True``), which used to strand an
    earlier-sequence PENDING op in PENDING forever (only ``release_first_ready_operation``
    at WO release would have promoted it). We scan every PENDING op on the WO in
    sequence order and promote the ones whose predecessor gate is satisfied,
    so single-op completions advance the route without depending on a read-time
    reconcile or a manual clock-in.

    The rule itself lives in ``promote_ready_operations`` (one implementation, three
    seams) -- including the same-work-center allowance, the ON_HOLD carve-out and the
    strictly-fuller laser exemption. This function owns only the LOAD.

    The session runs with ``autoflush=False``, so flush the just-completed
    operation's status first -- otherwise the load below would read its stale
    (pre-COMPLETE) row and the predecessor gate would refuse to release the successor.

    PERF-4: load every operation of this WO ONCE and let the gate run in memory over
    that snapshot, instead of calling ``has_incomplete_predecessors`` (one COUNT(*)
    query) per PENDING candidate. The old shape was an N+1 that turned quadratic
    inside ``complete_work_order``'s force-complete loop (each force-completed op
    re-walks the route). Promoting N ops costs the same one query as promoting one.

    ``user_id`` is None: this promotion is rule-driven, not a user action.
    """
    db.flush()
    # Invariant 1: scoped to the work order's OWN company_id -- taken off the WO row that
    # the caller already resolved tenant-scoped, never from client input. This query both
    # selects the rows to WRITE and supplies the predecessor snapshot, and the rule
    # promotes N operations rather than one, so an unscoped read here would widen from a
    # single mis-written row to a whole route.
    all_ops = (
        tenant_query(db, WorkOrderOperation, work_order.company_id)
        .filter(WorkOrderOperation.work_order_id == work_order.id)
        .order_by(WorkOrderOperation.sequence)
        .all()
    )
    promoted = promote_ready_operations(work_order, all_ops, db=db, user_id=None)
    return promoted[0] if promoted else None


def _operation_produced_evidence(db: Session, operation: WorkOrderOperation) -> float:
    """Durable produced-good total recorded against an operation's TimeEntry rows.

    SFI-5 / DUP-3: an absolute completion verb must never write
    ``quantity_complete`` below what the operator already booked on durable
    TimeEntry evidence, otherwise a later read-time reconcile silently bumps it
    back up with no audit of the discrepancy. Returns 0 for an unsaved operation.

    Invariant 1: scoped to the operation's own company, exactly like
    ``operation_labor_evidence`` -- the floor close books this figure and the force-complete
    bounds read the batched twin, so a mis-tenanted entry on a colliding operation id must be
    ignored by both or the two would disagree on the same data.
    """
    if operation.id is None:
        return 0.0
    query = db.query(func.coalesce(func.sum(TimeEntry.quantity_produced), 0.0)).filter(
        TimeEntry.operation_id == operation.id
    )
    if operation.company_id is not None:
        query = query.filter(TimeEntry.company_id == operation.company_id)
    total = query.scalar()
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


def pooled_quantity_complete(
    work_order: WorkOrder,
    operations: Iterable[WorkOrderOperation],
) -> float:
    """SUM of per-nest progress on a laser dispatch pool WO (the pool rollup rule).

    On a laser dispatch pool (``is_laser_dispatch_work_order``) each operation is a
    sibling NEST whose ``quantity_complete`` caps at its OWN ``component_quantity``
    (= that nest's planned_runs, via ``operation_target_quantity``), and
    ``work_order.quantity_ordered`` is the SUM of planned_runs over all nests. The
    pool total is therefore the SUM over non-component ops of
    ``min(op.quantity_complete, per-op target)`` -- the sequential routing rule
    (every op processes the whole order, so WO qty = one op's count) structurally
    freezes the header at the LARGEST SINGLE NEST's sheet count instead. Capped at
    ``quantity_ordered`` when it is positive; each per-op cap applies only when that
    op's target is positive (mirrors the sequential candidate cap, so a target-less
    op is never zeroed). Pure/read-only -- callers own the monotonic-up ``max()`` /
    walk-down ``min()`` guard against the WO's current value.
    """
    total = 0.0
    for op in operations or []:
        if op.component_part_id or _operation_nest_is_deleted(op):
            continue
        candidate = float(op.quantity_complete or 0)
        op_target = operation_target_quantity(op, work_order)
        if op_target > 0:
            candidate = min(candidate, op_target)
        total += candidate
    ordered = float(work_order.quantity_ordered or 0)
    if ordered > 0:
        total = min(total, ordered)
    return total


def per_item_operation_totals(
    work_order: Optional[WorkOrder],
    operations: Iterable[WorkOrderOperation],
    *,
    part_has_bom: bool,
) -> Optional[tuple[float, float]]:
    """(ordered, complete) summed across a POOL WO's per-item operations -- or None.

    A pool work order's operations are independent LINE ITEMS, each declaring its own
    quantity target in ``component_quantity``:

      * laser dispatch pools -- one op per nest, target = that nest's planned_runs;
      * batch/pool work orders -- one op per fabricated line item (the Miratech brake
        and weld-subassembly WOs), target = that item's piece count.

    A component-only fabrication batch is positively identifiable when sequencing is
    disabled, every operation shares one sequence, and every operation has a positive
    target and a DISTINCT component part. Sum those pieces even when the parent has
    a BOM: its header counts finished assemblies and can remain 0/8 throughout forming
    (WO-20260923-003). Mixed assembly/component work, repeated component stages, and
    sequenced routes are excluded. This changes display totals only, never inventory.

    The remaining pool shapes have ``component_part_id`` NULL and use the guards below.
    ``None`` means "do not sum -- the WORK-ORDER header is the truth here". Callers
    fall back to the header on None; they must not treat it as zero.

    THE HARD PART IS TELLING A POOL FROM A ROUTING, because ``component_quantity``
    with a NULL ``component_part_id`` is OVERLOADED. ``GET /work-orders/preview-
    operations`` (api/endpoints/work_orders.py) emits, for the assembly's OWN routing
    operations, ``component_part_id: None`` with ``component_quantity`` = THE WHOLE
    ORDER QUANTITY, restated once per operation; the New Work Order wizard posts that
    verbatim and ``POST /work-orders/`` persists it raw. Those ops are stages of one
    order, not line items, and summing them multiplies the order by its operation
    count -- the same trap ``completion_inventory_service`` documents having fallen
    into ("tripled the demand"). Four guards separate the two, in order:

      1. PART HAS A BOM -> never sum anonymous line items. The impostor shape above is emitted only under
         the preview endpoint's ``if has_bom:`` branch, so a BOM'd part's operations
         are a routing by construction. It cuts the other way too: a pool WO's
         hand-set per-item targets only SURVIVE on a part with no BOM, because
         ``_reconcile_operation_component_quantities`` runs on every work-order GET
         and force-overwrites ``component_quantity`` from the BOM. The caller decides
         this (one batched query); the rule stays DB-free here.
      2. DEAD NEST TOMBSTONES are dropped. ``soft_delete_laser_nest`` parks a
         cancelled nest's op at ON_HOLD and leaves its ``component_quantity`` intact,
         while the WO header is recomputed over LIVE nests only -- counting the
         tombstone would put sheets on the board that nobody will cut.
      3. ALL OR NOTHING. Every remaining operation must be a line item; one op
         without a target sends the whole WO back to the header. This fails SAFE in
         both directions: it drops mixed assembly/component routings, and it refuses
         the batch WO whose 3 untargeted lines would otherwise render "120/120 --
         100%" with 15 of 18 items done. (``WorkOrderOperationUpdate`` exposes
         neither field, so a missing target cannot be repaired in-app -- the shortfall
         would be permanent and invisible.)
      4. NOT ALL EQUAL TO THE HEADER. When every line's target is exactly
         ``quantity_ordered``, that is the restated-order-quantity signature, not a
         pool -- the backstop for an impostor whose part LOST its BOM (delete the BOM
         and guard 1 switches off, while ``_reconcile_operation_component_quantities``
         stops rewriting the ops, so the restated targets sit there forever).

    Guards 3 and 4 both fail toward the header, and each has a KNOWN COST -- state
    them plainly rather than calling the rule complete:

      * a pool whose every line happens to carry the SAME target as the header (8
        sets x 1 piece per line, or a 3-line WO at ``quantity_ordered = 1``) is
        indistinguishable from the impostor without a positive pool marker, so it
        keeps reading "8/8 -- 100%". One line with a different count is enough to
        make the rule work.
      * adding a whole-order operation to a pool WO (a final QC, a PACK step) leaves
        it untargeted, which trips guard 3 and reverts the whole card to the header.

    Both disappear the day a pool WORK-ORDER TYPE exists: the marker replaces all
    four guards with one positive test. Until then this rule refuses rather than
    guesses, in the direction that never invents a bigger order than the header.

    ONE OPERATION PER LINE ITEM is assumed, as on a laser pool: two operations
    carrying the same item's piece count (a second brake setup for one item) count it
    twice, exactly as ``pooled_quantity_complete`` would.

    Each line's completion is capped at its own target before summing (the per-op cap
    in ``pooled_quantity_complete``), so complete can never exceed ordered and needs
    no second cap. Targets come from ``operation_target_quantity`` so the per-op
    target rule stays ONE rule. The unit is whatever the line declares -- sheet runs
    on a nest, pieces on a batch line.

    NO HEADER CAP, deliberately -- unlike ``pooled_quantity_complete``, which caps its
    sum at ``work_order.quantity_ordered`` before storing it. The two agree wherever
    the header was derived from the lines (every imported nest WO); where they drift
    -- a header hand-edited below the sum of its lines, or a stored rollup that has
    not caught up -- the lines are what the floor still has to make, so the summed
    number is the honest one. Callers must not re-apply the header cap.

    PURE READ: no writes, no header mutation. This is a display rollup for surfaces
    that must show pool progress honestly; the stored header rollup stays
    ``sync_work_order_quantity_complete``'s business. It issues no queries OF ITS OWN,
    but guard 2 reads ``operation.laser_nest`` -- so it is query-free only for a caller
    that eager-loads that relationship (the wallboard does). A new adopter reading
    operations off a plain query gets one SELECT per operation.
    """
    live_ops = [op for op in (operations or []) if not _operation_nest_is_deleted(op)]
    if not live_ops:
        return None

    component_ids = {op.component_part_id for op in live_ops}
    sequences = {op.sequence for op in live_ops}
    component_batch = (
        work_order is not None
        and getattr(work_order, "sequential_operations", True) is False
        and None not in component_ids
        and len(component_ids) == len(live_ops)
        and None not in sequences
        and len(sequences) == 1
        and all(float(op.component_quantity or 0) > 0 for op in live_ops)
    )
    if component_batch:
        targets = [operation_target_quantity(op, work_order) for op in live_ops]
        return sum(targets), sum(min(float(op.quantity_complete or 0), target) for op, target in zip(live_ops, targets))

    if part_has_bom:
        return None

    line_ops = [op for op in live_ops if not op.component_part_id and float(op.component_quantity or 0) > 0]
    if len(line_ops) != len(live_ops):
        return None

    targets = [operation_target_quantity(op, work_order) for op in line_ops]
    header_ordered = float(work_order.quantity_ordered or 0) if work_order is not None else 0.0
    if header_ordered > 0 and all(target == header_ordered for target in targets):
        return None

    ordered = 0.0
    complete = 0.0
    for op, target in zip(line_ops, targets):
        ordered += target
        complete += min(float(op.quantity_complete or 0), target)
    return ordered, complete


def _operation_nest_is_deleted(operation: WorkOrderOperation) -> bool:
    """True when this op backs a SOFT-DELETED laser nest (a cancelled-nest tombstone).

    Mirrors ``laser_nest_service.active_laser_nest``'s predicate without importing it
    (that module imports this one). Reads the already-loaded relationship attribute --
    callers that pass operations from a bulk query should ``joinedload`` ``laser_nest``
    or this walks into an N+1.
    """
    nest = getattr(operation, "laser_nest", None)
    if nest is not None:
        return bool(getattr(nest, "is_deleted", False))
    # Office responses hide the deleted relationship while retaining this marker.
    return getattr(operation, "cancelled_nest_id", None) is not None


def active_work_order_operations(work_order: WorkOrder) -> list[WorkOrderOperation]:
    """Production obligations, excluding retained operations of cancelled nests.

    Cancellation parks the operation ON_HOLD for traceability; it is no longer
    work to finish. A live nest on hold remains an obligation. Never remove rows
    from the ORM relationship: their history and nest linkage must survive.
    """
    operations = list(work_order.operations or [])
    if not is_laser_dispatch_work_order(work_order):
        return operations
    return [op for op in operations if not _operation_nest_is_deleted(op)]


# --------------------------------------------------------------------------- #
# Short-count accounting (owner ruling 1, 2026-11-05).
#
# A short count at operation completion is never closed at the counted quantity:
# every piece that reached an operation is accounted for as good or as scrap with a
# reason, or the operation stays open. THIS is the one implementation of that
# arithmetic. Every verb, read model and seam calls ``operation_accounting`` /
# ``stage_accounting`` / ``record_short_declaration`` /
# ``finished_quantity_at_all_complete`` -- never an inline ``good + scrap >= target``.
#
# Two completion rules, deliberately different:
#
# * IMPLICIT auto-complete (clock-out, reconcile-on-read) fires on
#   ``OperationAccounting.is_full_count`` -- a full GOOD count of the units that
#   reached the operation. Scrap never counts there, so an operator who scrapped two
#   and is re-running replacements is never closed out from under them, and a read
#   (no actor, no reason) never closes a short.
# * EXPLICIT close (the floor and office complete verbs) requires
#   ``OperationAccounting.is_accounted`` -- every received unit is good or scrapped --
#   and stamps the short it closed with on ``quantity_short_declared`` via
#   ``record_short_declaration``. That column is the ONLY input to upstream credit:
#   nothing re-derives a short from the counts later, so a later edit cannot change
#   what the next stage was credited with, and every pre-117 row (NULL) reads as zero.
#
# Credit flows along CHAINS, by sequence (``operation_chain_key``): all operations of
# one component part (always credited), and the work order's own whole-order
# operations (credited only on a SEQUENCED work order -- on a pooled one they cannot
# be told apart from same-target line items, so they get none). Laser dispatch pools
# and line items are singletons and never receive credit.
# --------------------------------------------------------------------------- #

QTY_EPSILON = 1e-9
_QTY_EPSILON = QTY_EPSILON

MAIN_CHAIN_KEY: tuple = ("main",)


def _qty(value: Any) -> float:
    return float(value or 0)


def _declared_short(operation: WorkOrderOperation) -> float:
    """The short this operation was explicitly closed with (0 when never declared)."""
    return max(0.0, _qty(getattr(operation, "quantity_short_declared", None)))


def _loaded_nest_is_deleted(operation: WorkOrderOperation) -> bool:
    """``_operation_nest_is_deleted`` without ever triggering a relationship load."""
    nest = operation.__dict__.get("laser_nest")
    if nest is not None:
        return bool(getattr(nest, "is_deleted", False))
    return getattr(operation, "cancelled_nest_id", None) is not None


def operation_chain_key(work_order: Optional[WorkOrder], operation: WorkOrderOperation) -> Optional[tuple]:
    """Which unit-flow chain an operation belongs to, or ``None`` for a singleton.

    * ``None`` for every operation of a laser dispatch pool (each nest is its own
      dispatch item) and for a cancelled-nest tombstone;
    * ``("component", component_part_id)`` for a component operation -- all operations
      making one component part form one chain;
    * ``MAIN_CHAIN_KEY`` (``("main",)``) for a whole-order operation: no component part
      and a target equal to ``quantity_ordered``;
    * ``None`` otherwise -- a line item carrying its own piece target.

    Pure; reads only already-loaded attributes (never ``operation.laser_nest`` lazily).
    """
    if work_order is None or is_laser_dispatch_work_order(work_order):
        return None
    if _loaded_nest_is_deleted(operation):
        return None
    if operation.component_part_id:
        return ("component", operation.component_part_id)
    ordered = _qty(work_order.quantity_ordered)
    if ordered > QTY_EPSILON and abs(operation_target_quantity(operation, work_order) - ordered) <= QTY_EPSILON:
        return MAIN_CHAIN_KEY
    return None


def chain_credit_enabled(work_order: Optional[WorkOrder], key: Optional[tuple]) -> bool:
    """Does a short declared upstream in this chain reduce what reaches the next stage?

    Component chains: always (a component part is a positive marker of one unit flow).
    The main chain: only on a SEQUENCED work order, read through THE one resolver
    (``work_order_allows_same_work_center`` -> False). Singletons: never.
    """
    if key is None:
        return False
    if key[0] == "component":
        return True
    return not work_order_allows_same_work_center(work_order)


@dataclass(frozen=True)
class StageFigures:
    """Upstream figures for one operation, from ``stage_accounting``."""

    chain_upstream: float = 0.0  # HARD: declared shorts earlier in the same chain
    feed_short: float = 0.0  # SOFT: component losses that cannot reach this assembly stage


def _ceil_units(value: float) -> float:
    return float(math.ceil(value - QTY_EPSILON)) if value > QTY_EPSILON else 0.0


def stage_accounting(
    work_order: WorkOrder,
    operations: Iterable[Any],
    *,
    main_credit: Optional[bool] = None,
) -> dict[int, StageFigures]:
    """``{operation_id: StageFigures}`` for every operation that receives credit. Pure.

    ``operations`` is the work order's ACTIVE operations (``active_work_order_operations``);
    an id missing from the result has zero figures. ``main_credit`` overrides whether the
    whole-order chain is credited (``None`` = the work order's own setting) -- only the
    what-if check behind a sequencing flip (``accounting_change_strands``) passes it.

    * ``chain_upstream(i)`` = SUM of ``quantity_short_declared`` over COMPLETE operations
      of the same chain with a LOWER sequence, when the chain is credited. There is
      deliberately no same-work-center waiver: a chain is a unit flow.
    * ``feed_short(i)`` (main-chain operations of a credited main chain only): the units
      a component loss keeps from this assembly stage beyond its hard ceiling. A component
      chain's declared loss counts in assemblies as ``ceil(lost / qty_per)`` with
      ``qty_per = component target / quantity_ordered``. It is walked along the main
      chain so a loss declared at an earlier main stage and a component loss are not
      counted twice: what reaches a stage is
      ``min(what left the previous main stage, ordered - component loss so far)``, and
      ``feed_short`` is that figure's distance below the hard ceiling. SOFT: it changes
      the accounting (how much a short close must cover) and never caps recorded good.
      A main stage whose every operation is COMPLETE passes on what it actually made when
      that is MORE than the walk expected (its components were replenished), and the
      component losses before it stop counting for the stages after it -- a completed
      stage's output already embodies whatever was fed into it.
    """
    ops = [op for op in (operations or []) if op.id is not None]

    def credited(key: tuple) -> bool:
        if main_credit is not None and key == MAIN_CHAIN_KEY:
            return main_credit
        return chain_credit_enabled(work_order, key)

    chains: dict[tuple, list[Any]] = {}
    for op in ops:
        key = operation_chain_key(work_order, op)
        if key is not None and credited(key):
            chains.setdefault(key, []).append(op)
    if not chains:
        return {}

    figures: dict[int, StageFigures] = {}
    for chain in chains.values():
        for op in chain:
            seq = op.sequence or 0
            upstream = sum(
                _declared_short(other)
                for other in chain
                if other.status == OperationStatus.COMPLETE and (other.sequence or 0) < seq
            )
            figures[op.id] = StageFigures(chain_upstream=upstream)

    main_chain = chains.get(MAIN_CHAIN_KEY)
    ordered = _qty(work_order.quantity_ordered)
    if not main_chain or ordered <= QTY_EPSILON:
        return figures

    # Component losses in ASSEMBLY units, per component chain: (sequence, units).
    losses: dict[tuple, list[tuple[int, float]]] = {}
    for key, chain in chains.items():
        if key[0] != "component":
            continue
        for op in chain:
            declared = _declared_short(op)
            target = operation_target_quantity(op, work_order)
            if op.status != OperationStatus.COMPLETE or declared <= QTY_EPSILON or target <= QTY_EPSILON:
                continue
            losses.setdefault(key, []).append((op.sequence or 0, declared * ordered / target))
    if not losses:
        return figures

    def component_loss_between(after: Optional[int], sequence: int) -> float:
        return max(
            (
                _ceil_units(sum(units for seq, units in items if seq < sequence and (after is None or seq > after)))
                for items in losses.values()
            ),
            default=0.0,
        )

    # Walk the main chain by sequence: ``available`` is what left the previous stage, and
    # ``absorbed`` the last fully-COMPLETE main stage (component losses up to it are already
    # inside ``available``).
    available = ordered
    absorbed: Optional[int] = None
    for sequence in sorted({op.sequence or 0 for op in main_chain}):
        group = [op for op in main_chain if (op.sequence or 0) == sequence]
        reaching = max(0.0, min(available, ordered - component_loss_between(absorbed, sequence)))
        for op in group:
            current = figures[op.id]
            ceiling = max(0.0, operation_target_quantity(op, work_order) - current.chain_upstream)
            figures[op.id] = StageFigures(
                chain_upstream=current.chain_upstream,
                feed_short=max(0.0, ceiling - reaching),
            )
        available = reaching - sum(_declared_short(op) for op in group if op.status == OperationStatus.COMPLETE)
        if all(op.status == OperationStatus.COMPLETE for op in group):
            # A replenished stage made more than the walk expected: that is what moves on.
            available = max(available, min(_qty(op.quantity_complete) for op in group))
            absorbed = sequence
    return figures


def uncredited_upstream_by_operation(work_order: WorkOrder, operations: Iterable[Any]) -> dict[int, float]:
    """``{operation_id: units}`` an earlier whole-order operation of a POOLED work order declared short.

    The pooled main chain gets NO credit (owner decision: its same-target operations cannot
    be told apart from line items). These figures change no accounting -- they exist so the
    floor is told NOT to report those units as scrap at a later stage it never received them
    at (that would double-count the loss and consume tied material for runs that never
    happened), and to ask a supervisor to turn on Sequential operations instead. Pure; empty
    on a sequenced or laser work order.
    """
    if work_order is None or not work_order_allows_same_work_center(work_order):
        return {}
    main_ops = [
        op for op in (operations or []) if op.id is not None and operation_chain_key(work_order, op) == MAIN_CHAIN_KEY
    ]
    result: dict[int, float] = {}
    for op in main_ops:
        seq = op.sequence or 0
        declared = sum(
            _declared_short(other)
            for other in main_ops
            if other.status == OperationStatus.COMPLETE and (other.sequence or 0) < seq
        )
        if declared > QTY_EPSILON:
            result[op.id] = declared
    return result


@dataclass(frozen=True)
class OperationAccounting:
    """Where every unit that reached one operation went. Pure value object.

    ``target`` is ``operation_target_quantity``; ``good`` / ``scrapped`` the counts being
    evaluated (the stored ones, or a proposed write); ``chain_upstream`` / ``feed_short``
    come from ``stage_accounting``. The frontend twin (``utils/shortCount.ts``) transcribes
    these properties -- change both together.
    """

    target: float
    good: float
    scrapped: float
    chain_upstream: float = 0.0
    feed_short: float = 0.0
    # INFORMATIONAL only (no property reads it): units an earlier whole-order operation of a
    # POOLED work order declared short. They never reached this operation either, but the
    # pooled main chain gets no credit (owner decision), so the floor must be told NOT to
    # report them as scrap here. Filled by the read models and the close refusals, never by
    # ``operation_accounting`` (which reads no siblings on an uncredited chain).
    uncredited_upstream: float = 0.0

    @property
    def ceiling(self) -> float:
        """The server's HARD cap on recorded good: units an upstream close did not lose."""
        return max(0.0, self.target - self.chain_upstream)

    @property
    def received(self) -> float:
        """Units that reached this operation (the ceiling less soft component feed loss)."""
        return max(0.0, self.ceiling - self.feed_short)

    @property
    def upstream_short(self) -> float:
        """Units of the target that never reached this operation."""
        return self.target - self.received

    @property
    def unaccounted(self) -> float:
        """Received units recorded neither good nor scrapped."""
        return max(0.0, self.received - self.good - self.scrapped)

    @property
    def closing_short(self) -> float:
        """What an explicit close right now would declare short (received - good)."""
        return max(0.0, self.received - self.good)

    @property
    def exceeds_ceiling(self) -> bool:
        """More good is recorded than can have reached the operation after an upstream short.

        Only meaningful when the ceiling is BELOW the target (a credited chain carrying a
        declared short): an uncredited operation keeps its pre-117 target comparisons, so a
        legacy count above the target is not re-judged by this rule.
        """
        return self.ceiling < self.target - QTY_EPSILON and self.good > self.ceiling + QTY_EPSILON

    @property
    def is_full_count(self) -> bool:
        """IMPLICIT auto-complete: every unit that can reach the operation is recorded GOOD.

        Judged against the HARD ``ceiling``, never the soft ``received``: soft component feed
        loss lowers only what an EXPLICIT close must account for, so an assembly whose
        components were replenished is never closed out from under the crew at the
        feed-reduced count (a closed stage could never record the replenished units).
        Scrap never counts here, and a count above the ceiling (``exceeds_ceiling``) is never
        auto-completed -- it is an error to correct, not a finish.
        """
        return self.ceiling > QTY_EPSILON and self.good + QTY_EPSILON >= self.ceiling and not self.exceeds_ceiling

    @property
    def is_accounted(self) -> bool:
        """EXPLICIT close: every received unit is recorded good or scrapped."""
        return self.target > QTY_EPSILON and self.unaccounted <= QTY_EPSILON


def operation_accounting(
    work_order: WorkOrder,
    operation: WorkOrderOperation,
    operations: Optional[Iterable[WorkOrderOperation]] = None,
    *,
    good: Optional[float] = None,
    scrapped: Optional[float] = None,
) -> OperationAccounting:
    """``OperationAccounting`` for one operation, optionally at a proposed good/scrap.

    ``operations`` defaults to ``active_work_order_operations(work_order)``. A singleton or
    an uncredited chain never reads the sibling operations at all, so a laser pool or a
    pooled work order costs no relationship load here.
    """
    target = operation_target_quantity(operation, work_order)
    good_value = _qty(operation.quantity_complete) if good is None else float(good)
    scrapped_value = _qty(operation.quantity_scrapped) if scrapped is None else float(scrapped)
    key = operation_chain_key(work_order, operation)
    if key is None or not chain_credit_enabled(work_order, key):
        return OperationAccounting(target=target, good=good_value, scrapped=scrapped_value)
    siblings = active_work_order_operations(work_order) if operations is None else list(operations)
    figures = stage_accounting(work_order, siblings).get(operation.id, StageFigures())
    return OperationAccounting(
        target=target,
        good=good_value,
        scrapped=scrapped_value,
        chain_upstream=figures.chain_upstream,
        feed_short=figures.feed_short,
    )


def accounting_extra_data(acct: OperationAccounting) -> dict[str, float]:
    """The ``{target, received, upstream_quantity_short}`` audit stamp (INT3)."""
    return {
        "target": acct.target,
        "received": acct.received,
        "upstream_quantity_short": acct.upstream_short,
    }


def record_short_declaration(operation: WorkOrderOperation, acct: OperationAccounting) -> None:
    """Persist the short an EXPLICIT close declares (``acct.closing_short``), else NULL.

    Called ONLY by the floor and office explicit close verbs, with the accounting they
    just verified ``is_accounted``. Never by clock-out, the reconcile, or a correction.
    """
    operation.quantity_short_declared = acct.closing_short if acct.closing_short > QTY_EPSILON else None


def finished_quantity_at_all_complete(
    work_order: WorkOrder,
    operations: Iterable[WorkOrderOperation],
    existing: float,
) -> float:
    """The work-order header when every active operation is COMPLETE (a SET, not a max).

    ONE implementation for the live finalizer (``sync_work_order_quantity_complete``) and
    the reconcile twin (``_sync_work_order_status_from_operations``). Laser pools never
    reach it -- their header is the pooled sum.

    * No active operation carries a declared short -> ``quantity_ordered`` (today's snap,
      now clamped to the order; ``existing`` when nothing is ordered).
    * A main chain exists -> what actually came out of the last whole-order stage: the
      LOWEST good across the operations sharing its highest sequence (a parallel final
      stage that lost units binds), each capped at its own ceiling (defense in depth: a
      count above what can have reached a stage is never booked), and at the order.
    * Line items only, whose targets sum to the order -> ``min(ordered, SUM min(good,
      target))``.
    * Otherwise -- a routing made only of component operations (the batch / assembly
      shapes with no whole-order stage) or lines that do not split the order -> the order
      less the worst chain's declared loss in finished units (``ceil(declared x ordered /
      target)`` summed along each chain). Never ``existing``: the partial rollup never
      raises the header from a component operation, so that would book 0.
    """
    ordered = _qty(work_order.quantity_ordered)
    ops = list(operations or [])
    if ordered <= QTY_EPSILON:
        return existing
    if not any(_declared_short(op) > QTY_EPSILON for op in ops):
        return ordered
    main_ops = [op for op in ops if operation_chain_key(work_order, op) == MAIN_CHAIN_KEY]
    if main_ops:
        figures = stage_accounting(work_order, ops)
        final_sequence = max(op.sequence or 0 for op in main_ops)
        finals = [op for op in main_ops if (op.sequence or 0) == final_sequence]
        produced = min(
            min(
                _qty(op.quantity_complete),
                max(
                    0.0,
                    operation_target_quantity(op, work_order) - figures.get(op.id, StageFigures()).chain_upstream,
                ),
            )
            for op in finals
        )
        return max(0.0, min(ordered, produced))
    lines = [op for op in ops if not op.component_part_id]
    if lines:
        targets = [operation_target_quantity(op, work_order) for op in lines]
        if abs(sum(targets) - ordered) <= QTY_EPSILON:
            return min(ordered, sum(min(_qty(op.quantity_complete), target) for op, target in zip(lines, targets)))
    return max(0.0, ordered - _worst_chain_loss_in_finished_units(work_order, ops))


def _worst_chain_loss_in_finished_units(work_order: WorkOrder, operations: Iterable[Any]) -> float:
    """The largest declared loss any one chain carries, in finished units (``ceil``).

    Component chains group by part; every other non-main operation is its own chain. A
    chain's declarations along its stages are successive (each was declared against what
    reached it), so they SUM; ``ordered / target`` converts component pieces to finished
    units (``qty_per`` = target / ordered).
    """
    ordered = _qty(work_order.quantity_ordered)
    per_chain: dict[Any, float] = {}
    for op in operations or []:
        declared = _declared_short(op)
        target = operation_target_quantity(op, work_order)
        if declared <= QTY_EPSILON or target <= QTY_EPSILON or op.status != OperationStatus.COMPLETE:
            continue
        key = ("component", op.component_part_id) if op.component_part_id else ("op", op.id)
        per_chain[key] = per_chain.get(key, 0.0) + declared * ordered / target
    return max((_ceil_units(units) for units in per_chain.values()), default=0.0)


@dataclass(frozen=True)
class WorkOrderCompletionBounds:
    """What a force-complete may book, from floor evidence (``work_order_completion_bounds``).

    ``deliverable`` -- the most the floor's recorded evidence supports (above it needs an
    override reason); ``physical_max`` -- the most that can exist at all once declared
    shorts are taken out (never exceeded, even with a reason); ``floor_loss`` -- units the
    floor already accounted as lost; ``binding_operation_id`` -- the operation that sets
    ``deliverable`` (``None`` when nothing binds).
    """

    deliverable: float
    physical_max: float
    floor_loss: float
    binding_operation_id: Optional[int] = None
    # The HARD part of ``floor_loss`` (declared shorts and covered scrap, never the soft
    # component feed, which replenished components can make up): ``quantity_complete +
    # hard_loss`` may never exceed the order, override reason or not.
    hard_loss: float = 0.0


def work_order_completion_bounds(
    work_order: WorkOrder,
    operations: Iterable[WorkOrderOperation],
    recorded_good_by_op: dict[int, float],
    has_evidence_by_op: dict[int, bool],
) -> WorkOrderCompletionBounds:
    """Evidence bounds for ``POST /work-orders/{id}/complete`` (the force-complete). Pure.

    ``recorded_good_by_op`` is each operation's evidence-floored good (``min(target,
    max(stored, TimeEntry sum))``); ``has_evidence_by_op`` is True when the operation is
    COMPLETE, carries any TimeEntry, or has good/scrap > 0. An operation WITHOUT evidence
    caps at what reaches it (it was simply never reported, so it does not bind).

    * Main chain present: ``deliverable`` = min over main-chain caps;
      ``physical_max`` = what can still leave the last stage once declared shorts (and
      component feed) are taken out; ``floor_loss`` = covered loss along the chain --
      a COMPLETE operation's declared short, an open operation's ``min(scrap,
      closing_short)``, plus feed into the last stage. On a credited (sequenced) main
      chain the stages' losses are successive and SUM; on an uncredited (pooled) one each
      stage processes every unit independently, so the worst stage's loss is taken
      (summing would invent loss and let a smaller quantity through unscrapped).
    * Laser pools and line items whose targets sum to the order: ``deliverable`` =
      SUM of caps, ``floor_loss`` = SUM ``min(scrap, target - good)``.
    * Otherwise (component-only routings): the worst chain's declared loss in finished
      units comes off the order (``deliverable = physical_max = ordered - loss``); with
      nothing declared nothing binds.

    ``hard_loss`` is ``floor_loss`` without the soft component feed; the force-complete
    refuses ``quantity_complete + hard_loss > ordered`` with or without a reason.
    """
    ordered = _qty(work_order.quantity_ordered)
    ops = [op for op in (operations or []) if op.id is not None]

    def cap_for(op: WorkOrderOperation, acct: OperationAccounting) -> float:
        if has_evidence_by_op.get(op.id, False):
            return min(acct.target, _qty(recorded_good_by_op.get(op.id, op.quantity_complete)))
        return acct.received

    main_ops = sorted(
        (op for op in ops if operation_chain_key(work_order, op) == MAIN_CHAIN_KEY),
        key=lambda op: (op.sequence or 0, op.id),
    )
    if main_ops:
        figures = stage_accounting(work_order, ops)
        credited = chain_credit_enabled(work_order, MAIN_CHAIN_KEY)
        deliverable = ordered
        binding: Optional[int] = None
        losses: list[float] = []
        for op in main_ops:
            fig = figures.get(op.id, StageFigures())
            good = _qty(recorded_good_by_op.get(op.id, op.quantity_complete))
            acct = OperationAccounting(
                target=operation_target_quantity(op, work_order),
                good=good,
                scrapped=_qty(op.quantity_scrapped),
                chain_upstream=fig.chain_upstream,
                feed_short=fig.feed_short,
            )
            cap = cap_for(op, acct)
            if cap < deliverable - QTY_EPSILON:
                # The FIRST (lowest-sequence) operation at the minimum binds.
                deliverable = cap
                binding = op.id
            if op.status == OperationStatus.COMPLETE:
                losses.append(_declared_short(op))
            elif has_evidence_by_op.get(op.id, False):
                losses.append(min(acct.scrapped, acct.closing_short))
            else:
                losses.append(0.0)
        # The final stage is every operation at the highest sequence: a parallel final stage
        # that lost units binds the whole group.
        final_sequence = main_ops[-1].sequence or 0
        finals = [op for op in main_ops if (op.sequence or 0) == final_sequence]
        final_feed = max(figures.get(op.id, StageFigures()).feed_short for op in finals)
        if credited:
            hard_loss = sum(losses)
            floor_loss = hard_loss + final_feed
            physical_max = min(
                max(
                    0.0,
                    operation_target_quantity(op, work_order)
                    - figures.get(op.id, StageFigures()).chain_upstream
                    - figures.get(op.id, StageFigures()).feed_short
                    - (_declared_short(op) if op.status == OperationStatus.COMPLETE else 0.0),
                )
                for op in finals
            )
        else:
            hard_loss = max(losses, default=0.0)
            floor_loss = hard_loss
            physical_max = max(0.0, ordered - max((_declared_short(op) for op in main_ops), default=0.0))
        return WorkOrderCompletionBounds(
            deliverable=max(0.0, min(deliverable, ordered)),
            physical_max=min(ordered, physical_max),
            floor_loss=floor_loss,
            binding_operation_id=binding,
            hard_loss=hard_loss,
        )

    if is_laser_dispatch_work_order(work_order):
        lines = [op for op in ops if not op.component_part_id and not _loaded_nest_is_deleted(op)]
    else:
        lines = [op for op in ops if not op.component_part_id]
    targets = [operation_target_quantity(op, work_order) for op in lines]
    if lines and abs(sum(targets) - ordered) <= QTY_EPSILON:
        caps = []
        floor_loss = 0.0
        for op, target in zip(lines, targets):
            acct = OperationAccounting(
                target=target,
                good=_qty(recorded_good_by_op.get(op.id, op.quantity_complete)),
                scrapped=_qty(op.quantity_scrapped),
            )
            caps.append(cap_for(op, acct))
            floor_loss += min(acct.scrapped, max(0.0, target - acct.good))
        return WorkOrderCompletionBounds(
            deliverable=min(ordered, sum(caps)),
            physical_max=ordered,
            floor_loss=floor_loss,
            binding_operation_id=None,
            hard_loss=floor_loss,
        )
    # No whole-order stage and no line split (component-only routings): what the worst chain
    # declared lost, in finished units -- the same figure the all-complete header books.
    if is_laser_dispatch_work_order(work_order) or ordered <= QTY_EPSILON:
        return WorkOrderCompletionBounds(deliverable=ordered, physical_max=ordered, floor_loss=0.0)
    chain_loss = min(ordered, _worst_chain_loss_in_finished_units(work_order, ops))
    return WorkOrderCompletionBounds(
        deliverable=ordered - chain_loss,
        physical_max=ordered - chain_loss,
        floor_loss=chain_loss,
        hard_loss=chain_loss,
    )


def upstream_figures_by_operation(
    db: Session,
    company_id: int,
    operations: Sequence[WorkOrderOperation],
) -> dict[int, OperationAccounting]:
    """``{operation_id: OperationAccounting}`` for a page of operations, in ONE query.

    For the read models (kiosk queue, ``/shop-floor/operations``): one tenant-scoped
    ``load_only`` read of every operation on the rows' work orders, then
    ``stage_accounting`` per work order, in memory. Callers should already have each
    row's ``work_order`` loaded (joined / contains-eager) -- an unloaded one is lazy-loaded
    here, which is correct but costs a query per work order. Invariant 1: scoped to
    ``company_id``, so a foreign row sharing a ``work_order_id`` never contributes.
    """
    from sqlalchemy.orm import load_only

    rows = [op for op in operations if op is not None and op.id is not None]
    work_orders: dict[int, WorkOrder] = {}
    for op in rows:
        wo = op.__dict__.get("work_order") or op.work_order
        if wo is not None and wo.id is not None:
            work_orders[wo.id] = wo
    if not work_orders:
        return {}
    siblings = (
        tenant_query(db, WorkOrderOperation, company_id)
        .options(
            load_only(
                WorkOrderOperation.id,
                WorkOrderOperation.work_order_id,
                WorkOrderOperation.sequence,
                WorkOrderOperation.component_part_id,
                WorkOrderOperation.component_quantity,
                WorkOrderOperation.quantity_complete,
                WorkOrderOperation.quantity_scrapped,
                WorkOrderOperation.quantity_short_declared,
                WorkOrderOperation.status,
            )
        )
        .filter(WorkOrderOperation.work_order_id.in_(list(work_orders)))
        .all()
    )
    by_work_order: dict[int, list[WorkOrderOperation]] = {}
    for sibling in siblings:
        by_work_order.setdefault(sibling.work_order_id, []).append(sibling)
    figures_by_work_order = {
        wo_id: stage_accounting(work_orders[wo_id], ops) for wo_id, ops in by_work_order.items() if wo_id in work_orders
    }
    uncredited_by_work_order = {
        wo_id: uncredited_upstream_by_operation(work_orders[wo_id], ops)
        for wo_id, ops in by_work_order.items()
        if wo_id in work_orders
    }
    result: dict[int, OperationAccounting] = {}
    for op in rows:
        wo = work_orders.get(op.work_order_id)
        if wo is None:
            continue
        figures = figures_by_work_order.get(wo.id, {}).get(op.id, StageFigures())
        result[op.id] = OperationAccounting(
            target=operation_target_quantity(op, wo),
            good=_qty(op.quantity_complete),
            scrapped=_qty(op.quantity_scrapped),
            chain_upstream=figures.chain_upstream,
            feed_short=figures.feed_short,
            uncredited_upstream=uncredited_by_work_order.get(wo.id, {}).get(op.id, 0.0),
        )
    return result


@dataclass(frozen=True)
class OperationLaborEvidence:
    """One operation's durable labor record: SUM(quantity_produced) and how many entries."""

    produced: float = 0.0
    entries: int = 0


def operation_labor_evidence(
    db: Session,
    company_id: int,
    operation_ids: Iterable[Optional[int]],
) -> dict[int, OperationLaborEvidence]:
    """``{operation_id: OperationLaborEvidence}`` in ONE grouped, tenant-scoped query.

    Any TimeEntry counts, open or closed -- the same existence test the floor's labor gate
    uses. An operation with no entries is absent from the result. Invariant 1: a foreign
    company's entry on a colliding operation id never contributes.
    """
    ids = sorted({op_id for op_id in operation_ids if op_id is not None})
    if not ids:
        return {}
    rows = (
        db.query(
            TimeEntry.operation_id,
            func.coalesce(func.sum(TimeEntry.quantity_produced), 0.0),
            func.count(TimeEntry.id),
        )
        .filter(TimeEntry.company_id == company_id, TimeEntry.operation_id.in_(ids))
        .group_by(TimeEntry.operation_id)
        .all()
    )
    return {
        row[0]: OperationLaborEvidence(produced=float(row[1] or 0.0), entries=int(row[2] or 0))
        for row in rows
        if row[0] is not None
    }


def recorded_good_from_evidence(
    work_order: WorkOrder, operation: WorkOrderOperation, evidence: Optional[OperationLaborEvidence]
) -> float:
    """The evidence-floored good an operation has recorded: ``min(target, max(stored, SUM))``.

    The same clamp ``resolve_absolute_operation_quantity`` applies with nothing requested,
    computed from an already-batched evidence read instead of one query per operation.
    """
    floor = max(_qty(operation.quantity_complete), evidence.produced if evidence is not None else 0.0)
    target = operation_target_quantity(operation, work_order)
    if target > 0:
        floor = min(floor, target)
    return max(0.0, floor)


def work_order_completion_bounds_from_evidence(
    work_order: WorkOrder,
    operations: Sequence[WorkOrderOperation],
    evidence: dict[int, OperationLaborEvidence],
) -> WorkOrderCompletionBounds:
    """``work_order_completion_bounds`` fed from one ``operation_labor_evidence`` read.

    Shared by the force-complete (``POST /work-orders/{id}/complete``) and the work-order
    read model (``quantity_accounted_loss`` / ``quantity_deliverable``), so the figure a
    supervisor is shown is the figure the verb enforces. An operation has evidence when it
    is COMPLETE, carries any TimeEntry, or has good / scrap above zero.
    """
    recorded: dict[int, float] = {}
    has_evidence: dict[int, bool] = {}
    for op in operations:
        if op.id is None:
            continue
        op_evidence = evidence.get(op.id)
        recorded[op.id] = recorded_good_from_evidence(work_order, op, op_evidence)
        has_evidence[op.id] = (
            op.status == OperationStatus.COMPLETE
            or (op_evidence is not None and op_evidence.entries > 0)
            or _qty(op.quantity_complete) > QTY_EPSILON
            or _qty(op.quantity_scrapped) > QTY_EPSILON
        )
    return work_order_completion_bounds(work_order, operations, recorded, has_evidence)


def quantity_ordered_floor(
    work_order: WorkOrder, operations: Iterable[WorkOrderOperation]
) -> tuple[float, Optional[WorkOrderOperation]]:
    """The lowest ``quantity_ordered`` a ``PUT /work-orders/{id}`` may set, and the stage that sets it.

    The largest ``good + scrapped`` any whole-order (main-chain) operation has already
    accounted for, judged against the CURRENT order (call it before the new value is
    written). Lowering the order below it would leave that stage holding more units than
    were ordered -- an after-the-fact short close of everything downstream. Laser pools
    and line items carry their own targets and set no floor. ``(0.0, None)`` when nothing
    binds.
    """
    floor = 0.0
    binding: Optional[WorkOrderOperation] = None
    for op in operations or []:
        if operation_chain_key(work_order, op) != MAIN_CHAIN_KEY:
            continue
        accounted = _qty(op.quantity_complete) + _qty(op.quantity_scrapped)
        if accounted > floor + QTY_EPSILON:
            floor = accounted
            binding = op
    return floor, binding


def later_completed_chain_operation(
    work_order: WorkOrder,
    operation: WorkOrderOperation,
    operations: Iterable[WorkOrderOperation],
) -> Optional[WorkOrderOperation]:
    """The lowest-sequence COMPLETE operation AFTER ``operation`` in the same chain, if any.

    The refusal behind re-opening a completed operation: a later stage of the same unit
    flow already completed with units that passed through it, so it has to be corrected
    first. Chain membership is ``operation_chain_key`` whether or not the chain is
    credited -- a pooled whole-order chain still moves the same units. A singleton (line
    item, laser nest) has no later chain stage.
    """
    key = operation_chain_key(work_order, operation)
    if key is None:
        return None
    sequence = operation.sequence or 0
    later = [
        other
        for other in operations or []
        if other.id != operation.id
        and other.status == OperationStatus.COMPLETE
        and (other.sequence or 0) > sequence
        and operation_chain_key(work_order, other) == key
    ]
    return min(later, key=lambda other: ((other.sequence or 0), other.id or 0)) if later else None


_WHAT_IF_ATTRS = (
    "id",
    "work_order_id",
    "sequence",
    "operation_number",
    "status",
    "quantity_short_declared",
    "component_part_id",
    "component_quantity",
    "quantity_complete",
    "quantity_scrapped",
    "cancelled_nest_id",
)


class _OperationWhatIf:
    """A plain stand-in for one operation with some attributes changed -- never an ORM row,
    so a what-if computation can never dirty the session it reads from."""

    def __init__(self, operation: WorkOrderOperation, **overrides: Any) -> None:
        for name in _WHAT_IF_ATTRS:
            setattr(self, name, getattr(operation, name, None))
        nest = operation.__dict__.get("laser_nest")
        if nest is not None:
            self.laser_nest = nest
        for name, value in overrides.items():
            setattr(self, name, value)


@dataclass(frozen=True)
class AccountingStrand:
    """Another operation a change would strand (``accounting_change_strands``).

    ``reason`` is ``"unaccounted"`` (a COMPLETE operation that adds up today would not --
    it would become a completed operation that is short) or ``"over_ceiling"`` (its
    recorded good fits what can reach it today and would not).
    """

    operation: WorkOrderOperation
    reason: Literal["unaccounted", "over_ceiling"]
    before: OperationAccounting
    after: OperationAccounting


def _accounting_from_figures(
    work_order: WorkOrder, operation: Any, figures: dict[int, StageFigures]
) -> OperationAccounting:
    figure = figures.get(operation.id, StageFigures())
    return OperationAccounting(
        target=operation_target_quantity(operation, work_order),
        good=_qty(operation.quantity_complete),
        scrapped=_qty(operation.quantity_scrapped),
        chain_upstream=figure.chain_upstream,
        feed_short=figure.feed_short,
    )


def accounting_change_strands(
    work_order: WorkOrder,
    operations: Iterable[WorkOrderOperation],
    *,
    changed: Optional[WorkOrderOperation] = None,
    main_credit: Optional[bool] = None,
    **overrides: Any,
) -> Optional[AccountingStrand]:
    """Would this change strand ANOTHER operation's accounting? The first one it would, or ``None``.

    THE what-if behind every write that moves upstream credit after the fact (owner ruling 1:
    a completed operation can never be short, and no stage may hold more good than can
    have reached it):

    * a close that DECLARES a short (``changed`` + ``status=COMPLETE`` and the new
      ``quantity_short_declared`` / counts) -- a later stage already holding more good than
      the declaration lets reach it would be over its ceiling;
    * an office correction that re-stamps a COMPLETE operation's declaration, or REOPENS it
      (``status=IN_PROGRESS``, ``quantity_short_declared=None``) -- across chains too: a
      component chain's declaration feeds the assembly chain, so reopening it can strand a
      completed assembly stage that is not in its own chain;
    * a sequencing flip (``main_credit``), which turns the whole-order chain's credit on or off.

    Only a NEW strand counts: an operation already short or over its ceiling before the
    change (legacy data) never blocks it. Pure -- the changed operation is evaluated through
    a plain stand-in, so the session is never dirtied by asking.
    """
    ops = [op for op in (operations or []) if op.id is not None]
    before = stage_accounting(work_order, ops)
    if changed is not None:
        proxied: list[Any] = [_OperationWhatIf(op, **overrides) if op.id == changed.id else op for op in ops]
    else:
        proxied = list(ops)
    after = stage_accounting(work_order, proxied, main_credit=main_credit)
    for op in sorted(ops, key=lambda item: (item.sequence or 0, item.id or 0)):
        if changed is not None and op.id == changed.id:
            continue
        acct_before = _accounting_from_figures(work_order, op, before)
        acct_after = _accounting_from_figures(work_order, op, after)
        if op.status == OperationStatus.COMPLETE and acct_before.is_accounted and not acct_after.is_accounted:
            return AccountingStrand(op, "unaccounted", acct_before, acct_after)
        if acct_before.good <= acct_before.ceiling + QTY_EPSILON and acct_after.good > acct_after.ceiling + QTY_EPSILON:
            return AccountingStrand(op, "over_ceiling", acct_before, acct_after)
    return None


def sequencing_flip_conflict(
    work_order: WorkOrder,
    operations: Iterable[WorkOrderOperation],
    *,
    turning_on: bool,
) -> Optional[tuple[WorkOrderOperation, WorkOrderOperation]]:
    """``(declaring, later)`` when a ``sequential_operations`` flip would change what a declared short meant.

    Whole-order credit is read at the work order's CURRENT setting, so a flip after a stage
    was closed short re-interprets that declaration:

    * turning sequencing OFF withdraws the credit every later whole-order stage was given
      (an open one would have to account for units that never reached it; a completed one
      would become short) -- refused while any COMPLETE whole-order stage that has a later
      stage carries a declaration;
    * turning it ON credits declarations stamped under the pooled rule, where a later stage
      had to cover the same loss itself -- refused once any later whole-order stage has
      already COMPLETED after the declaring one. Before that (the floor refused a stage that
      never received the units, and a supervisor turns sequencing on so it is credited) the
      flip is exactly the remedy.

    ``None`` when nothing was declared, which is every work order flipped today.
    """
    main_ops = sorted(
        (op for op in (operations or []) if operation_chain_key(work_order, op) == MAIN_CHAIN_KEY),
        key=lambda op: (op.sequence or 0, op.id or 0),
    )
    for declaring in main_ops:
        if declaring.status != OperationStatus.COMPLETE or _declared_short(declaring) <= QTY_EPSILON:
            continue
        later = [op for op in main_ops if (op.sequence or 0) > (declaring.sequence or 0)]
        if not later:
            continue
        if not turning_on:
            return declaring, later[0]
        completed_later = next((op for op in later if op.status == OperationStatus.COMPLETE), None)
        if completed_later is not None:
            return declaring, completed_later
    return None


def completed_chain_operation(
    work_order: WorkOrder, operations: Iterable[WorkOrderOperation]
) -> Optional[WorkOrderOperation]:
    """The lowest-sequence COMPLETE operation whose target follows the order, if any.

    The refusal behind RAISING ``quantity_ordered``: a whole-order operation's target is the
    order and a component operation's is re-derived from it, so a raise would turn a stage
    that already completed at the old order into a completed operation that is short.
    Line items and laser nests carry their own targets and never bind.
    """
    completed = [
        op
        for op in (operations or [])
        if op.status == OperationStatus.COMPLETE and operation_chain_key(work_order, op) is not None
    ]
    return min(completed, key=lambda op: (op.sequence or 0, op.id or 0)) if completed else None


def rescale_restated_operation_targets(
    work_order: WorkOrder, operations: Iterable[WorkOrderOperation], old_ordered: float, new_ordered: float
) -> list[tuple[WorkOrderOperation, float]]:
    """Move whole-order targets that were RESTATED as ``component_quantity`` along with the order.

    The new-work-order wizard persists an assembly's own routing operations with
    ``component_quantity`` = the order (no component part). ``operation_target_quantity``
    prefers that column, so after ``PUT /work-orders/{id}`` changed ``quantity_ordered`` those
    operations would keep demanding the OLD count -- after owner ruling 1 an uncloseable
    target, because units never started are never scrap. Only a non-component operation
    whose stored value equals the old order is moved (a deliberately different line target
    is not); laser nests (``component_quantity`` = planned runs) never are. Returns
    ``[(operation, old_value)]`` for the caller's audit rows; writes the column, nothing else.
    """
    if is_laser_dispatch_work_order(work_order) or old_ordered <= QTY_EPSILON:
        return []
    moved: list[tuple[WorkOrderOperation, float]] = []
    for op in operations or []:
        stored = _qty(op.component_quantity)
        if op.component_part_id or stored <= QTY_EPSILON or abs(stored - old_ordered) > QTY_EPSILON:
            continue
        op.component_quantity = new_ordered
        moved.append((op, stored))
    return moved


def reopen_completed_operation(
    db: Session, work_order: WorkOrder, operation: WorkOrderOperation
) -> list[WorkOrderOperation]:
    """COMPLETE -> IN_PROGRESS for an office over-count correction that would leave it short.

    Owner ruling 1: a completed operation cannot be short, so a correction that lowers its
    good count below what reached it (``POST /work-orders/operations/{id}/reduce-production``
    with ``reopen_operation``) returns it to the floor instead of leaving a short close
    behind. Clears ``actual_end`` / ``completed_by`` and the declared short (the next
    explicit close declares again), points ``current_operation_id`` back at the route, and
    demotes the un-worked READY operations it now blocks through the ONE demotion core
    (scope ``"blocked_by"``). Never touches consumed material: consumption never
    auto-reverses (invariant 6); the reasoned return verb is the walk-back.

    The caller holds the op -> WO locks, has already refused when
    ``later_completed_chain_operation`` names a later completed stage, and owns the
    REQUIRED audit rows (``log_required`` status change + ``log_demotions_required``).
    Writes statuses only -- no audit row, no commit.
    """
    operation.status = OperationStatus.IN_PROGRESS
    operation.actual_end = None
    operation.completed_by = None
    operation.quantity_short_declared = None
    work_order.current_operation_id = _active_operation_id(work_order)
    return demote_ready_operations(db, work_order, scope="blocked_by", blocked_by=operation)


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

    Laser dispatch pool WOs (``is_laser_dispatch_work_order``) roll up as the SUM
    of per-nest progress (``pooled_quantity_complete``) in BOTH branches -- the
    single-op rule would freeze the header at the largest nest, and the
    all-complete branch must NOT snap to ``quantity_ordered``: a nest completed
    short would then assert sheets that were never cut (AS9100D production-records
    honesty). Still ``max()``-guarded (monotonic-up).

    The all-complete branch of a non-pool work order is a SET through
    ``finished_quantity_at_all_complete`` (owner ruling 1): with no declared short
    anywhere it is today's snap to ``quantity_ordered`` (clamped to the order); once an
    operation was closed short it is what actually left the last stage, so the header --
    and the FG receipt and backflush basis read from it -- never asserts units that were
    declared scrapped. It is the one place this rollup can LOWER the header, and only
    when a declaration exists.
    """
    existing = float(work_order.quantity_complete or 0)
    target = float(work_order.quantity_ordered or 0)
    if is_laser_dispatch_work_order(work_order):
        pooled = pooled_quantity_complete(work_order, work_order.operations or [])
        work_order.quantity_complete = max(existing, pooled)
    elif all_operations_complete:
        work_order.quantity_complete = finished_quantity_at_all_complete(
            work_order, active_work_order_operations(work_order), existing
        )
    elif not operation.component_part_id:
        candidate = float(operation.quantity_complete or 0)
        if target > 0:
            candidate = min(candidate, target)
        work_order.quantity_complete = max(existing, candidate)


@dataclass
class TimeEntryReduction:
    """Per-entry slice of a walked-down correction (for the audit row's paper trail)."""

    time_entry_id: Optional[int]
    entry_type: Optional[str]
    quantity_produced_before: float
    quantity_produced_after: float

    def as_dict(self) -> dict:
        return {
            "time_entry_id": self.time_entry_id,
            "entry_type": self.entry_type,
            "quantity_produced_before": self.quantity_produced_before,
            "quantity_produced_after": self.quantity_produced_after,
        }


@dataclass
class OperationQuantityReduction:
    """Before/after snapshot of a good-count over-report correction (for the audit row).

    ``time_entry_quantity_produced_before/after`` are the SUMS over the walked
    entries (a single-entry walk degenerates to that entry's before/after);
    ``time_entry_reductions`` carries the per-entry slices, in walk order, for
    the audit row's ``extra_data``. ``rework_delta`` is the portion of the delta
    walked off REWORK-typed entries (the FPY decrement actually applied).
    """

    delta: float
    operation_quantity_complete_before: float
    operation_quantity_complete_after: float
    work_order_quantity_complete_before: float
    work_order_quantity_complete_after: float
    time_entry_quantity_produced_before: float
    time_entry_quantity_produced_after: float
    time_entry_reductions: list[TimeEntryReduction] = field(default_factory=list)
    rework_delta: float = 0.0


def reduce_operation_produced_quantity(
    operation: WorkOrderOperation,
    work_order: WorkOrder,
    entries: Sequence[TimeEntry],
    delta: float,
    work_order_operations: Iterable[WorkOrderOperation],
) -> OperationQuantityReduction:
    """Inverse of the monotonic-up completion helpers: correct an over-report DOWN.

    The system's produced quantity is deliberately monotonic-up and re-derived from
    durable evidence: ``reconcile_work_orders_from_completion_evidence`` (which runs
    on every work-order read) RAISES ``operation.quantity_complete`` to
    ``SUM(TimeEntry.quantity_produced)`` for the operation and never lowers it, and
    ``sync_work_order_quantity_complete`` rolls op -> WO with ``max()``. So lowering
    ``operation.quantity_complete`` ALONE is self-undoing -- the next read re-raises
    it from the un-reduced TimeEntry evidence.

    A correct reduction lowers the operation total and its BACKING EVIDENCE in
    lock-step by the same delta, then RECOMPUTES the WO rollup from the siblings:

    * ``entries`` -- the ordered ELIGIBLE evidence rows (the caller decides
      eligibility: the operator path passes the caller's own unapproved entries,
      open first then closed newest-first; the office path passes ALL unapproved
      entries on the operation). The delta is WALKED across them in the given
      order: each entry gives up ``min(its quantity_produced, remaining)`` until
      the whole delta is consumed, so no entry ever goes below zero and the total
      removed equals ``delta`` exactly. The reconcile ``SUM`` therefore drops by
      exactly ``delta`` (this is what makes the correction stick). The caller MUST
      have validated ``delta <= sum(entries' quantity_produced)``.
    * ``operation.quantity_complete`` -- the derived op total, lowered by ``delta``.
    * ``work_order.quantity_complete`` -- the WO rollup, RECOMPUTED (not blindly
      subtracted) from ``work_order_operations``. On a multi-op WO another op may hold
      an equal-or-higher count (e.g. an earlier COMPLETE op), so a blind ``- delta``
      would pull the WO below ``max(op)`` with no read-time repair
      (``_sync_work_order_status_from_operations`` only re-derives WO qty when ALL ops
      are complete). Instead mirror ``sync_work_order_quantity_complete``'s invariant --
      ``WO qty == max`` over NON-component ops of ``min(op.quantity_complete, target)``
      (or, on a laser dispatch pool WO, ``pooled_quantity_complete``'s SUM over sibling
      nests) -- but only ever LOWER: ``work_order.quantity_complete = min(wo_before,
      recomputed_rollup)``. This naturally handles every case: reducing a non-max op
      leaves the WO unchanged; reducing the max op drops it to the next-highest (on a
      pool WO, any nest's reduction lowers the sum by the same delta); reducing a
      COMPONENT op leaves it unchanged (component-op production never rolled
      into the WO rollup, so component ops are excluded from the max). The ``min``
      guards against ever raising the WO (a reduction must not increase it) and against
      a stale-low prior WO value.

    Reconcile-safety invariant preserved: before the call
    ``operation.quantity_complete >= SUM(quantity_produced)`` (the floor/reconcile
    invariant); removing the same ``delta`` from both sides keeps
    ``operation.quantity_complete >= SUM`` afterward, so the read-time reconcile has
    nothing to re-raise. ``delta`` MUST already be validated by the caller (``> 0``,
    finite, and ``<= sum(entries' quantity_produced)``); the ``max(0, ...)`` floors
    are defensive so the persisted values can never go negative. NEVER touches scrap
    fields or status -- this is a good-count miscount correction, not a scrap move,
    and the operation / work order stay in progress.

    ``work_order_operations`` must be the WO's operations loaded under the same WO row
    lock the caller holds (siblings are only written while holding that lock, so their
    counts are stable here); it must include ``operation`` itself. Every mutated
    ``entries`` row carries ``version_id_col`` -- a concurrent stale write surfaces as
    ``StaleDataError`` at the caller's commit.

    True-inverse rework accounting (FPY): the additive twin
    ``report_operation_production`` INCREMENTS ``operation.quantity_reworked`` when
    good quantity is reported on a REWORK clock-in; so this reduction symmetrically
    DECREMENTS it -- PER ENTRY -- by exactly the portion of the delta walked off
    REWORK-typed entries, floored at 0. Quantity walked off RUN entries never touches
    ``quantity_reworked``.

    Mutates the affected rows in place and returns the before/after snapshot (with
    per-entry slices) for the caller's tamper-evident audit row.
    """
    op_before = float(operation.quantity_complete or 0)
    wo_before = float(work_order.quantity_complete or 0)

    # Walk the delta across the eligible entries in the caller's order. Each entry
    # gives up min(available, remaining): entries never go negative, and the final
    # take equals the exact remaining value, so the total removed == delta exactly
    # (no float drift from repeated subtraction of derived values).
    remaining = delta
    slices: list[TimeEntryReduction] = []
    rework_delta = 0.0
    entries_before_sum = 0.0
    entries_after_sum = 0.0
    for entry in entries:
        if remaining <= 0:
            break
        available = float(entry.quantity_produced or 0)
        if available <= 0:
            continue
        take = min(available, remaining)
        entry.quantity_produced = available - take
        entry.updated_at = datetime.utcnow()
        remaining -= take
        if entry.entry_type == TimeEntryType.REWORK:
            rework_delta += take
        entries_before_sum += available
        entries_after_sum += available - take
        slices.append(
            TimeEntryReduction(
                time_entry_id=entry.id,
                entry_type=(entry.entry_type.value if hasattr(entry.entry_type, "value") else entry.entry_type),
                quantity_produced_before=available,
                quantity_produced_after=available - take,
            )
        )

    op_after = max(0.0, op_before - delta)
    operation.quantity_complete = op_after

    # Recompute the WO rollup from the (now-updated) siblings rather than subtracting
    # delta -- see the docstring. max over NON-component ops of min(op qty, target),
    # capped only when target > 0 (mirrors sync_work_order_quantity_complete), then only
    # LOWER via min(wo_before, ...). Empty non-component set defaults to 0.0.
    # Laser dispatch pool WOs mirror ``pooled_quantity_complete`` instead: the rollup is
    # the SUM over sibling nests of min(nest qty, that nest's own target), capped at the
    # WO ordered total -- lowering one nest's evidence lowers the pool sum by the same
    # delta, whereas the sequential max() rule would leave the header pinned at the
    # largest untouched nest.
    is_pool = is_laser_dispatch_work_order(work_order)
    target = float(work_order.quantity_ordered or 0)
    non_component_caps: list[float] = []
    for sibling in work_order_operations:
        if sibling.component_part_id or (is_pool and _operation_nest_is_deleted(sibling)):
            continue
        # Use the freshly-lowered value for the corrected op (it may not be flushed yet).
        candidate = op_after if sibling.id == operation.id else float(sibling.quantity_complete or 0)
        if is_pool:
            sibling_target = operation_target_quantity(sibling, work_order)
            if sibling_target > 0:
                candidate = min(candidate, sibling_target)
        elif target > 0:
            candidate = min(candidate, target)
        non_component_caps.append(candidate)
    if is_pool:
        recomputed_rollup = sum(non_component_caps)
        if target > 0:
            recomputed_rollup = min(recomputed_rollup, target)
    else:
        recomputed_rollup = max(non_component_caps) if non_component_caps else 0.0
    wo_after = min(wo_before, recomputed_rollup)
    work_order.quantity_complete = wo_after

    # FPY true-inverse, portioned PER ENTRY: only the quantity walked off REWORK-typed
    # entries decrements quantity_reworked (the twin only increments it for REWORK
    # clock-ins), bounded at 0. RUN-entry portions leave it unchanged.
    if rework_delta > 0:
        operation.quantity_reworked = max(0.0, float(operation.quantity_reworked or 0) - rework_delta)

    return OperationQuantityReduction(
        delta=delta,
        operation_quantity_complete_before=op_before,
        operation_quantity_complete_after=op_after,
        work_order_quantity_complete_before=wo_before,
        work_order_quantity_complete_after=wo_after,
        time_entry_quantity_produced_before=entries_before_sum,
        time_entry_quantity_produced_after=entries_after_sum,
        time_entry_reductions=slices,
        rework_delta=rework_delta,
    )


def _active_operation_id(work_order: WorkOrder) -> Optional[int]:
    """The operation the WO is 'currently on', for ``current_operation_id`` (RUP-1).

    Preference order, by ascending sequence: the first IN_PROGRESS op, else the
    first READY op, else the first not-yet-COMPLETE op. Returns ``None`` when the
    whole route is complete (the WO is no longer on any operation).
    """
    operations = sorted(
        (op for op in active_work_order_operations(work_order) if op.id is not None),
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
    for op in active_work_order_operations(work_order):
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
      falling back to now) BEFORE flipping the WO to COMPLETE (DUP-2 -- fixes the
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

    active_operations = active_work_order_operations(work_order)
    if active_operations and not remaining_ids:
        # All operations complete -> the work order is finished.
        now = datetime.utcnow()
        end_dates = [op.actual_end for op in active_operations if op.actual_end]
        work_order.actual_end = max(end_dates) if end_dates else now
        start_dates = [op.actual_start for op in active_operations if op.actual_start]
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

    Each operation row is a separate production obligation. Sequence numbers
    describe routing order, and names/numbers can repeat; none identify shared
    completion evidence, including rows that look like regenerated operations.
    """
    operations = active_work_order_operations(work_order)
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

    progress_total = 0.0
    operations_complete = 0
    for operation in operations:
        target_qty = operation_target_quantity(operation, work_order)
        complete_qty = float(operation.quantity_complete or 0)
        has_completion_evidence = _operation_has_completion_evidence(operation)

        if has_completion_evidence:
            ratio = 1.0
        elif target_qty > 0:
            ratio = min(1.0, max(0.0, complete_qty / target_qty))
        else:
            ratio = 0.0

        progress_total += ratio
        operations_complete += int(has_completion_evidence)

    total_operations = len(operations)
    return {
        "operation_count": total_operations,
        "operations_complete": operations_complete,
        "operation_progress_percent": round(
            (progress_total / total_operations) * 100.0,
            1,
        ),
    }


def _promote_stranded_ready_operations(db: Session, work_orders: Iterable[WorkOrder]) -> bool:
    """Read-path heal: re-run the READY promotion over already-released work orders.

    THE GAP THIS CLOSES. Promotion runs at WO release (``release_first_ready_operation``)
    and at operation completion (``release_next_ready_operation``) and nowhere else, and
    ``POST /work-orders/{id}/release`` refuses anything that is not DRAFT. So a work order
    RELEASED under an older, stricter promotion rule has no way to be re-promoted: its
    operations 2..N sit PENDING forever, invisible to the kiosk and the dispatch board
    (both surface READY work ONLY -- ``dispatch_service.QUEUE_OPERATION_STATUSES``). An
    18-item press-brake batch showed 1 of 18. Running the same rule from the read path
    means every stranded work order repairs itself the next time anyone loads a board,
    with no new endpoint, no data migration and no owner action.

    NEVER PROMOTES A DRAFT WORK ORDER. Standing owner decision: Release is the
    authorization step and the record of WHO authorized production, so a read must not be
    able to put unreleased work on the floor's board. The completion-evidence reconcile
    around this one still processes DRAFT WOs exactly as before -- this carve-out is
    scoped to the promotion, and it is the reason this loop re-filters instead of reusing
    the caller's ``non_terminal_work_orders`` list as-is.

    Terminal WOs are likewise skipped, and the status is re-read HERE rather than trusted
    from the caller's earlier filter: the WO-level loop above may have just driven this
    work order to COMPLETE from completion evidence, and a WO that finished one statement
    ago must not have operations promoted onto the board. Every other non-terminal status
    promotes, which is exactly the set ``release_next_ready_operation`` already promotes
    on at operation completion (ON_HOLD included -- a held work order is not a terminal
    one, and the operation-level ON_HOLD carve-out inside the gate still stops a held op's
    successors from going anywhere).

    READ-SAFE. This runs from GET handlers whose reconcile guard catches only
    ``SQLAlchemyError``, so a stray exception here would 500 a board load. Each work order
    is promoted inside its own guard: a failure is logged and skipped, the remaining work
    orders still heal, and a partially-promoted work order is harmless because every op
    that did flip had independently passed the gate. IDEMPOTENT: the promotion only reads
    and writes PENDING rows, so a second read finds nothing PENDING, promotes nothing and
    emits no duplicate ``operation_ready`` event. The ``any(... PENDING ...)`` pre-check
    makes a fully-promoted work order -- the common case -- cost one in-memory scan and no
    allocation.

    Returns True when anything was promoted, so the caller's ``changed`` flag reaches the
    read handler and the flips are actually COMMITTED (``_reconcile_and_commit`` commits
    only on a truthy reconcile).

    No query: ``work_order.operations`` is already materialized by the reconcile above,
    so the promotion's in-memory gate adds no load per work order (PERF-4).
    """
    changed = False
    for work_order in work_orders:
        try:
            status = work_order.status
            if status in TERMINAL_WO_STATUSES or status == WorkOrderStatus.DRAFT:
                continue
            operations = work_order.operations or []
            if not any(op.status == OperationStatus.PENDING for op in operations):
                continue
            if promote_ready_operations(work_order, operations, db=db, user_id=None):
                changed = True
        except Exception:  # pragma: no cover - a read must never 500 on the promotion heal
            logger.exception(
                "READY-promotion heal failed for work order %s (company %s)",
                getattr(work_order, "id", None),
                getattr(work_order, "company_id", None),
            )
    return changed


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

    It ALSO re-runs the PENDING -> READY promotion (``_promote_stranded_ready_operations``,
    last -- after the completion evidence has landed, so a freshly-completed operation
    unblocks its successors in the same pass). That heals work orders released before the
    current promotion rule existed, which no other seam can reach. Deliberately NOT
    reported through ``transitions``: PENDING -> READY is unaudited at all three promotion
    seams (READY is queue state, not a production record), and ``transitions`` carries
    completion semantics its consumers act on -- audit rows, completion events, FG
    receipt/backflush, cost rollup, scheduling refresh. A READY entry there would be a
    fourth policy AND a wrong signal to five consumers.
    """
    # G6-A: skip the OPERATION-level reconcile for any TERMINAL parent WO
    # (CANCELLED/CLOSED/COMPLETE). The WO-level _sync_work_order_status_from_operations
    # already early-returns for terminal WOs, but this op-level loop ran over ALL
    # operations regardless -- so a CANCELLED WO with closed TimeEntry evidence could
    # still have operation.quantity_complete bumped or an op flipped to COMPLETE during
    # a read-path reconcile. Excluding terminal WOs' operations from the candidate set
    # leaves their committed op state untouched. Read-safe: never raises.
    non_terminal_work_orders = [wo for wo in work_orders if wo.status not in TERMINAL_WO_STATUSES]
    operations = [op for wo in non_terminal_work_orders for op in active_work_order_operations(wo)]
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
    # an operation whose required snapshot steps lack live conforming records -- the
    # same predicate the /complete endpoints and the clock-out path enforce; without
    # it, this read-time reconcile would undo their refusals on the next page load.
    # Quantities still reconcile below; only the COMPLETE flip is withheld. Function-
    # local import: this module is a low-level dependency of many services (several of
    # which import process_sheet_service themselves), so keeping the reverse edge out
    # of the module import graph avoids ever creating a cycle as either side grows.
    from app.services.process_sheet_service import gated_operation_ids

    step_gated_operation_ids = gated_operation_ids(db, operations)

    # Owner ruling 1: what can REACH each operation, from the already-collected rows (no
    # query). An operation whose upstream stage was declared short auto-completes at its
    # hard ceiling (``is_full_count`` -- never the soft feed figure, never above the
    # ceiling); scrap still never counts here, so a read never closes a short. Without
    # credit the figures are empty and every check below is the pre-117 one.
    stage_figures: dict[int, StageFigures] = {}
    for work_order in non_terminal_work_orders:
        stage_figures.update(stage_accounting(work_order, active_work_order_operations(work_order)))

    for operation in operations:
        produced_qty, scrapped_qty = produced_by_operation.get(operation.id, (0.0, 0.0))
        if produced_qty > float(operation.quantity_complete or 0):
            operation.quantity_complete = produced_qty
            changed = True
        if scrapped_qty > float(operation.quantity_scrapped or 0):
            operation.quantity_scrapped = scrapped_qty
            changed = True
        old_op_status = operation.status.value if operation.status else None
        target_qty = operation_target_quantity(operation)
        figures = stage_figures.get(operation.id)
        acct = OperationAccounting(
            target=target_qty,
            good=float(operation.quantity_complete or 0),
            scrapped=float(operation.quantity_scrapped or 0),
            chain_upstream=figures.chain_upstream if figures else 0.0,
            feed_short=figures.feed_short if figures else 0.0,
        )
        credited = acct.ceiling < target_qty - QTY_EPSILON
        closed_produced = closed_produced_by_operation.get(operation.id, 0.0)
        op_changed = _sync_operation_status_from_quantity(
            operation,
            latest_entry_by_operation.get(operation.id),
            (closed_produced + QTY_EPSILON >= acct.ceiling) if credited else closed_produced >= target_qty,
            completion_gated=operation.id in step_gated_operation_ids,
            accounting=acct if credited else None,
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
                extra_data=accounting_extra_data(acct),
            )
        changed = op_changed or changed

    for work_order in non_terminal_work_orders:
        # Evidence is scoped to the operation_id on each TimeEntry above. Never
        # infer completion for another row from matching sequence, labels, or parts:
        # independent parallel jobs legitimately share all of those fields.
        changed = _sync_work_order_status_from_operations(work_order, transitions, entry_ids_by_operation) or changed

    # LAST: re-run the READY promotion over the same non-terminal work orders (minus
    # DRAFT -- see the helper). It runs after the loops above so it sees the operations
    # this reconcile just drove to COMPLETE and can release their successors in the same
    # pass, and after the WO-level sync so a work order that just went COMPLETE is
    # skipped. Read-safe and idempotent; never touches ``transitions``.
    changed = _promote_stranded_ready_operations(db, non_terminal_work_orders) or changed

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
    extra_data: Optional[dict] = None,
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
            extra_data=dict(extra_data or {}),
        )
    )


def _operation_has_completion_evidence(operation: WorkOrderOperation) -> bool:
    return operation.status == OperationStatus.COMPLETE or (
        operation.actual_end is not None and operation.completed_by is not None
    )


def _sync_operation_status_from_quantity(
    operation: WorkOrderOperation,
    latest_entry: Optional[TimeEntry] = None,
    has_closed_completion_evidence: bool = False,
    completion_gated: bool = False,
    accounting: Optional[OperationAccounting] = None,
) -> bool:
    """Reconcile an operation's status from its quantities + closed labor evidence.

    ``completion_gated`` (PR 3): True when the operation's required process-sheet
    steps are missing conforming records -- the evidence-driven COMPLETE flip is then
    withheld (quantities and the PENDING/READY -> IN_PROGRESS lift still apply), so
    the read-time reconcile can never complete an operation the /complete endpoints
    and the clock-out path would refuse.

    ``accounting`` (owner ruling 1): passed when an upstream close in the operation's
    chain declared a short, so its ceiling is below the target. The COMPLETE flip is
    then THE implicit rule the clock-out uses, ``OperationAccounting.is_full_count`` (a
    full GOOD count of the units that can reach it, never above that), re-evaluated on
    the reconciled counts. ``None`` keeps the pre-117 target comparison exactly.
    """
    target_qty = operation_target_quantity(operation)
    if target_qty <= 0:
        return False

    quantity_complete = float(operation.quantity_complete or 0)
    if accounting is None:
        reached_full_count = quantity_complete >= target_qty
    else:
        reached_full_count = OperationAccounting(
            target=accounting.target,
            good=quantity_complete,
            scrapped=float(operation.quantity_scrapped or 0),
            chain_upstream=accounting.chain_upstream,
            feed_short=accounting.feed_short,
        ).is_full_count
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
    elif (
        operation.status != OperationStatus.ON_HOLD
        and reached_full_count
        and has_closed_completion_evidence
        and not completion_gated
    ):
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


def _sync_work_order_status_from_operations(
    work_order: WorkOrder,
    transitions: Optional[list[StatusTransition]] = None,
    entry_ids_by_operation: Optional[dict[int, list[int]]] = None,
) -> bool:
    operations = active_work_order_operations(work_order)
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
    is_pool = is_laser_dispatch_work_order(work_order)

    # Laser dispatch pool WOs: heal the header to the SUM of per-nest progress on
    # every read, raise-only (never lower), whether or not all nests are complete.
    # The sequential rules below only re-derive WO quantity when ALL ops are
    # COMPLETE, so a pool WO whose header froze at the largest single nest (the
    # pre-fix max() rollup) would otherwise stay stuck until the next production
    # post. Applies in the all-complete case too, INSTEAD of the snap-to-ordered
    # below -- a nest completed short must not assert sheets that were never cut
    # (AS9100D production-records honesty). Best-effort/read-safe: never raises.
    if is_pool:
        pooled = pooled_quantity_complete(work_order, operations)
        if pooled > float(work_order.quantity_complete or 0):
            work_order.quantity_complete = pooled
            changed = True

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
        # Pool WOs already healed to the pooled SUM above and must NOT snap to
        # quantity_ordered (no-snap honesty rule -- see the is_pool block). Everything
        # else takes THE all-complete header rule the live finalizer uses (owner ruling
        # 1): the snap to ordered with no declared short, otherwise what left the last
        # stage. One helper, so a read and a live completion can never disagree.
        if not is_pool:
            existing_qty = float(work_order.quantity_complete or 0)
            finished_qty = finished_quantity_at_all_complete(work_order, operations, existing_qty)
            if abs(finished_qty - existing_qty) > QTY_EPSILON:
                work_order.quantity_complete = finished_qty
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
