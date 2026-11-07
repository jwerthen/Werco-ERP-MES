"""Shared core for the over-count correction ("reduce production") endpoints.

Two routers expose the correction verb and MUST share one implementation of the
eligibility rules, the lock discipline, the walk math, and the audit shape so the
operator and office paths can never drift:

* ``POST /shop-floor/operations/{id}/reduce-production`` -- operator self-service,
  bounded to the CALLER'S OWN unapproved evidence (open clock-in first, then their
  own closed unapproved sessions newest-first).
* ``POST /work-orders/operations/{id}/reduce-production`` -- supervisor/office,
  bounded to ALL unapproved evidence on the operation (any operator).

APPROVAL is the immutability boundary (G5-A): approved TimeEntry rows are excluded
from every allowance -- the existing unapprove endpoint is the front door for
correcting signed-off labor. Clock-out is NOT a boundary: closed-but-unapproved
evidence is correctable (the real-world "noticed after check-out" case).

Operation COMPLETION is not a boundary for the OFFICE verb either (PR 3). It refused
both verbs on the rationale that a completed operation's downstream inventory / cost /
FG effects had fired and could not be walked back; the reasoned RETURN verb
(``material_consumption_service.return_tied_material``) is now that walk-back, so the
office path passes ``allow_completed_operation=True`` and the operator path does not.
The terminal-WORK-ORDER refusal is unchanged on BOTH verbs.

Owner ruling 1 (2026-11-05) narrows that: a COMPLETE operation cannot be SHORT. When the
corrected count would leave received units unaccounted (``OperationAccounting.unaccounted``),
the office correction is refused 409 unless the same request says ``reopen_operation`` --
then the operation returns to IN_PROGRESS through ``reopen_completed_operation`` (refused
while a later stage of the same chain is COMPLETE), and the reopen is REQUIRED audit
evidence. A correction that keeps the operation accounted (scrap covers it) leaves it
COMPLETE; when it raises the short the operation now closes with, its declaration is
RE-STAMPED (REQUIRED audit) so the later stages are credited with the real loss -- refused
409 when a later stage already holds more good than that lets through. Every reopen and
re-stamp is judged by ``accounting_change_strands`` too, so a change can never strand a
COMPLETE operation in another chain (a component chain feeds the assembly chain).

The quantity math itself lives in
``work_order_state_service.reduce_operation_produced_quantity`` (the walk across
eligible entries + the recomputed WO rollup); this module owns eligibility,
the op->WO lock ordering, the TOCTOU re-check, the tamper-evident audit row, the
best-effort OperationalEvent, and the commit/409 translation. HTTP-flavored
(raises ``HTTPException``), following the ``resolve_scrap_reason_code_or_http``
precedent for service helpers that ARE the endpoint's business logic.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.api.audit_guard import required_audit
from app.models.time_entry import TimeEntry
from app.models.user import User
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation
from app.services.audit_service import AuditService
from app.services.laser_nest_service import sync_laser_nest_from_operation
from app.services.operation_action_gates import (
    CORRECTION_AUDIT_UNAVAILABLE,
    accounting_strand_message,
    reduce_would_leave_short_message,
    reopen_blocked_by_later_message,
)
from app.services.operational_event_service import OperationalEventService
from app.services.work_order_state_service import (
    QTY_EPSILON,
    TERMINAL_WO_STATUSES,
    OperationQuantityReduction,
    accounting_change_strands,
    accounting_extra_data,
    active_work_order_operations,
    later_completed_chain_operation,
    log_demotions_required,
    operation_accounting,
    operation_target_quantity,
    reduce_operation_produced_quantity,
    reopen_completed_operation,
)

# The OPERATOR's refusal, for both the pre-lock gate and the TOCTOU re-check. It is
# accurate only because the office verb now ACCEPTS a completed operation
# (``allow_completed_operation``): before that, both endpoints hit this identical 409, so
# the operator was told to ask a supervisor whose own front door refused the same thing.
MSG_COMPLETED_WORK = "Completed work can't be corrected here -- ask a supervisor"

# The terminal-WORK-ORDER refusal, deliberately SPLIT from the message above. A terminal
# work order is refused on BOTH verbs -- the office relaxation is scoped to a completed
# OPERATION on a live work order -- so pointing the caller at a supervisor here would be
# the same false referral in the other direction.
MSG_TERMINAL_WORK_ORDER = (
    "This work order is complete, closed or cancelled -- its recorded production can no longer be corrected"
)


def _assert_reduction_in_scope(
    work_order: WorkOrder,
    operation: WorkOrderOperation,
    *,
    allow_completed_operation: bool,
) -> None:
    """The before-completion scope gate. ONE implementation, two call sites, two verbs.

    Both the pre-lock read and the post-lock TOCTOU re-check run this, so the unlocked
    fast-fail and the authoritative under-lock check can never drift (the whole reason
    this module exists).

    Two independent refusals, and only one of them is relaxable:

    * **Terminal work order** -- always 409, on both verbs. The job's record is closed.
    * **COMPLETE operation** -- 409 on the operator's self-service verb; ALLOWED on the
      office verb (``allow_completed_operation=True``). The original rationale for
      refusing was that "downstream inventory / cost / FG effects have fired" and could
      not be walked back. Since PR 3 that is no longer true: tied material that a
      completed operation consumed is unwound by the reasoned RETURN verb
      (``material_consumption_service.return_tied_material``), and a supervisor lowering
      a completed operation's count is precisely what opens the
      ``correct_over_consumption`` allowance that verb is bounded by. The operator's
      verb keeps the refusal -- correcting finished work is a supervised act, and
      MSG_COMPLETED_WORK's referral is only honest because the office door is now open.
    """
    if work_order.status in TERMINAL_WO_STATUSES:
        raise HTTPException(status_code=409, detail=MSG_TERMINAL_WORK_ORDER)
    if operation.status == OperationStatus.COMPLETE and not allow_completed_operation:
        raise HTTPException(status_code=409, detail=MSG_COMPLETED_WORK)


def load_operation_for_reduction_or_http(
    db: Session, operation_id: int, company_id: int, *, allow_completed_operation: bool = False
) -> tuple[WorkOrderOperation, WorkOrder]:
    """Pre-lock phase shared by both routers: tenant-scoped 404s + the before-completion 409.

    The same terminal/complete gate is re-asserted under the row locks in
    ``perform_production_reduction`` (TOCTOU); this unlocked read exists to fail fast
    with a clear 4xx before any query the caller runs for eligibility.

    ``allow_completed_operation`` is the OFFICE verb's opt-in to correcting a COMPLETE
    operation (see ``_assert_reduction_in_scope``). It defaults to ``False``, so the
    operator's shop-floor path keeps its refusal without changing a line, and the
    relaxation can only ever be reached by a caller that asked for it explicitly. Pass
    the SAME value to ``perform_production_reduction`` -- a caller that relaxes only the
    pre-lock gate would sail past it and be refused under the lock instead.
    """
    operation = (
        db.query(WorkOrderOperation)
        .filter(
            WorkOrderOperation.id == operation_id,
            WorkOrderOperation.company_id == company_id,
        )
        .first()
    )
    if not operation:
        raise HTTPException(status_code=404, detail="Operation not found")

    work_order = operation.work_order
    if not work_order or work_order.is_deleted:
        raise HTTPException(status_code=404, detail="Work order not found for this operation")

    # Before-completion scope gate (non-optimistic), shared verbatim with the post-lock
    # re-check below. 409 Conflict.
    _assert_reduction_in_scope(work_order, operation, allow_completed_operation=allow_completed_operation)

    return operation, work_order


def eligible_reduction_entries(
    db: Session,
    *,
    operation_id: int,
    company_id: int,
    user_id: Optional[int] = None,
) -> list[TimeEntry]:
    """The ordered evidence rows a correction may walk down.

    Eligibility = UNAPPROVED (``approved IS NULL``) entries on the operation;
    ``user_id`` narrows to one operator's own evidence (the self-service path) or,
    when ``None``, spans every operator (the supervisor path). Order = open entries
    first (newest clock-in first), then closed entries newest-first (``clock_out``
    desc) -- corrections walk back the most recent counts first. Tenant-scoped.
    """
    query = db.query(TimeEntry).filter(
        TimeEntry.operation_id == operation_id,
        TimeEntry.company_id == company_id,
        TimeEntry.approved.is_(None),
    )
    if user_id is not None:
        query = query.filter(TimeEntry.user_id == user_id)
    # Open entries (clock_out IS NULL) first, then closed newest-first. NULLs-first
    # emulation that works on both Postgres and SQLite: sort key 0 for open, 1 for
    # closed; within each group newest first (open by clock_in, closed by clock_out).
    # Final tie-break: newest ROW first (-id) so two entries sharing a clock_out (e.g.
    # a crew-wide auto-close stamping one timestamp) walk in a reproducible order.
    entries = query.all()
    return sorted(
        entries,
        key=lambda e: (
            0 if e.clock_out is None else 1,
            -(e.clock_in.timestamp() if e.clock_out is None and e.clock_in else 0),
            -(e.clock_out.timestamp() if e.clock_out else 0),
            -(e.id or 0),
        ),
    )


def approved_produced_total(
    db: Session,
    *,
    operation_id: int,
    company_id: int,
    user_id: Optional[int] = None,
) -> float:
    """SUM(quantity_produced) over APPROVED entries on the operation (for 400 messages).

    Lets the bound error say WHY the allowance is short ("approved labor needs a
    supervisor" / "unapprove it first") when signed-off evidence exists.
    """
    query = db.query(func.coalesce(func.sum(TimeEntry.quantity_produced), 0.0)).filter(
        TimeEntry.operation_id == operation_id,
        TimeEntry.company_id == company_id,
        TimeEntry.approved.isnot(None),
    )
    if user_id is not None:
        query = query.filter(TimeEntry.user_id == user_id)
    return float(query.scalar() or 0.0)


@dataclass
class ProductionReductionOutcome:
    """What ``perform_production_reduction`` hands back for the response/broadcasts."""

    operation: WorkOrderOperation
    work_order: WorkOrder
    reduction: OperationQuantityReduction
    target_qty: float


def perform_production_reduction(
    db: Session,
    *,
    operation_id: int,
    company_id: int,
    actor: User,
    audit: AuditService,
    entries: list[TimeEntry],
    delta: float,
    reason: str,
    notes: Optional[str],
    recorded_source: Optional[str],
    notes_entry: Optional[TimeEntry],
    event_source_module: str,
    path: str,
    allow_completed_operation: bool = False,
    reopen_operation: bool = False,
) -> ProductionReductionOutcome:
    """The shared transactional body of both reduce-production endpoints.

    Caller has already: resolved eligibility (``entries``, ordered) and validated the
    bound ``delta <= sum(entries' quantity_produced)``, plus any path-specific gates
    (open clock-in for self-service; role for office). This function owns everything
    that must be identical on both paths:

    * Row locks in the completion paths' order -- OPERATION then WORK ORDER
      (``with_for_update``), tenant-scoped, soft-delete-aware (SFI-1).
    * The TOCTOU re-check of the before-completion gate under those locks (a
      concurrent WO-cancel doesn't bump any row version this write touches), run
      through the same ``_assert_reduction_in_scope`` the pre-lock read used and with
      the same ``allow_completed_operation`` the caller passed there.
    * The walk + recomputed WO rollup via ``reduce_operation_produced_quantity``.
    * ``notes``/``source`` applied to ``notes_entry`` when given AND unapproved (the
      self-service path passes the caller's open entry only when ``approved IS
      NULL``; the office path passes ``None`` -- a supervisor's note belongs on the
      audit row, not scribbled onto another operator's labor record). Dirty-write
      discipline: ONLY entries actually walked (their quantity changed) or a
      ``notes_entry`` that actually receives notes/source may be mutated -- nothing
      else gets a gratuitous ``updated_at``/version bump, so an APPROVED row can
      never be touched outside the audited diff.
    * The best-effort OperationalEvent (never fails the request) and the
      tamper-evident audit rows: ONE ``work_order_operation`` row (the aggregate,
      tagged ``extra_data.path`` = ``"shop_floor" | "office"``) plus one
      ``time_entry`` row PER WALKED ENTRY, so an auditor sampling a specific
      TimeEntry surfaces the correction by a resource-keyed lookup. All in this
      unit of work -- atomic with the mutation.
    * Commit, translating an optimistic-lock ``StaleDataError`` to HTTP 409.

    Returns the refreshed rows + snapshot; broadcasts and response shaping stay in
    the routers.

    ``notes_entry`` MUST be an unapproved row (callers enforce this); ``path``
    disambiguates the two verbs on the audit chain.

    ``allow_completed_operation`` MUST match what the caller passed to
    ``load_operation_for_reduction_or_http`` -- it is the office verb's opt-in to
    correcting a COMPLETE operation, and the two gates are the same predicate evaluated
    before and under the locks.

    ``reopen_operation`` (office only) is the explicit yes for a correction that would
    leave a COMPLETE operation short (owner ruling 1); without it that correction is
    refused 409 before anything is written. With it, the operation returns to IN_PROGRESS
    and the un-worked READY operations it now blocks to PENDING, with REQUIRED audit rows
    (503 and nothing saved when they cannot be written).
    """
    # SFI-1: same locks, same order as report_operation_production / complete_operation.
    operation = (
        db.query(WorkOrderOperation)
        .filter(
            WorkOrderOperation.id == operation_id,
            WorkOrderOperation.company_id == company_id,
        )
        .with_for_update()
        .first()
    )
    if not operation:
        raise HTTPException(status_code=404, detail="Operation not found")

    work_order = (
        db.query(WorkOrder)
        .filter(
            WorkOrder.id == operation.work_order_id,
            WorkOrder.company_id == company_id,
            WorkOrder.is_deleted == False,  # noqa: E712
        )
        .with_for_update()
        .first()
    )
    if not work_order:
        raise HTTPException(status_code=404, detail="Work order not found for this operation")

    # TOCTOU re-check under the lock (mirrors complete_operation's post-lock terminal
    # guard): the op-COMPLETE race is also caught by the operation version bump, but a
    # concurrent WO-cancel does NOT bump any version this write touches -- without this
    # a reduction could commit against a just-CANCELLED work order. 409 Conflict.
    # (On the office verb the op-COMPLETE half is relaxed; the terminal-WO half -- the
    # half this re-check exists for -- is not.)
    _assert_reduction_in_scope(work_order, operation, allow_completed_operation=allow_completed_operation)

    target_qty = operation_target_quantity(operation, work_order)

    # Siblings loaded under the WO row lock so the service can RECOMPUTE the rollup
    # (they are only written while holding this WO lock, so they are stable + fresh).
    # Includes the corrected operation itself (identity-mapped -> it will reflect the
    # lowered count the walk is about to set).
    work_order_operations = list(work_order.operations)

    # Owner ruling 1: a COMPLETE operation cannot be short. Decided BEFORE the walk, so a
    # refusal writes nothing. Only reachable on the office verb (the scope gate above
    # refuses a COMPLETE operation on the operator's).
    reopening = False
    reopen_accounting = None
    # A correction that keeps a COMPLETE operation accounted (scrap still covers it) but
    # raises the short it now closes with RE-STAMPS its declaration -- otherwise the later
    # stages would keep being credited with the old figure and have to cover real loss
    # with scrap that never happened there.
    restamp: Optional[tuple[Optional[float], Optional[float]]] = None
    if operation.status == OperationStatus.COMPLETE:
        corrected_good = max(0.0, float(operation.quantity_complete or 0) - delta)
        active_operations = active_work_order_operations(work_order)
        reopen_accounting = operation_accounting(work_order, operation, active_operations, good=corrected_good)
        if reopen_accounting.unaccounted > QTY_EPSILON:
            if not reopen_operation:
                raise HTTPException(
                    status_code=409,
                    detail=reduce_would_leave_short_message(operation, delta, reopen_accounting.unaccounted),
                )
            later = later_completed_chain_operation(work_order, operation, active_operations)
            if later is not None:
                raise HTTPException(status_code=409, detail=reopen_blocked_by_later_message(later))
            # Chain identity is not the whole story: a component chain's declaration feeds the
            # assembly chain, so reopening it (its declaration cleared) can strand a COMPLETE
            # assembly stage in ANOTHER chain. Judged on the accounting itself.
            strand = accounting_change_strands(
                work_order,
                active_operations,
                changed=operation,
                status=OperationStatus.IN_PROGRESS,
                quantity_short_declared=None,
                quantity_complete=corrected_good,
            )
            if strand is not None:
                raise HTTPException(status_code=409, detail=accounting_strand_message(strand))
            reopening = True
        else:
            new_declared = reopen_accounting.closing_short if reopen_accounting.closing_short > QTY_EPSILON else None
            old_declared = operation.quantity_short_declared
            if abs(float(new_declared or 0) - float(old_declared or 0)) > QTY_EPSILON:
                strand = accounting_change_strands(
                    work_order,
                    active_operations,
                    changed=operation,
                    status=OperationStatus.COMPLETE,
                    quantity_short_declared=new_declared,
                    quantity_complete=corrected_good,
                )
                if strand is not None:
                    raise HTTPException(status_code=409, detail=accounting_strand_message(strand))
                restamp = (
                    float(old_declared) if old_declared is not None else None,
                    new_declared,
                )

    # Walk the delta across the eligible evidence + lower the op total + recompute the
    # WO rollup (see the service docstring for why this stays reconcile-safe).
    reduction = reduce_operation_produced_quantity(operation, work_order, entries, delta, work_order_operations)

    reopened_from = operation.status.value if reopening and operation.status else None
    if restamp is not None:
        operation.quantity_short_declared = restamp[1]

    operation.updated_at = datetime.utcnow()
    sync_laser_nest_from_operation(operation)

    # Dirty-write discipline (G5-A): only write notes/source -- and only bump
    # updated_at/version -- when there is actually something to record on an eligible
    # (unapproved) entry. A notes_entry with nothing to write stays byte-for-byte
    # untouched; an APPROVED open entry never reaches here (callers pass None).
    if notes_entry is not None and (notes or recorded_source):
        if notes:
            notes_entry.notes = f"{notes_entry.notes}\n{notes}" if notes_entry.notes else notes
        # A0.1 adoption telemetry: record the channel when this write carries one (a
        # kiosk-scoped token always resolves to KIOSK); omitted -> keep the entry's channel.
        if recorded_source:
            notes_entry.source = recorded_source
        notes_entry.updated_at = datetime.utcnow()
    work_order.updated_at = datetime.utcnow()

    # Surface a REAL optimistic-lock conflict as the documented 409, not a 500: the
    # walked entries' versioned UPDATEs would otherwise first flush INSIDE
    # emit_best_effort below, which swallows failures by design -- a genuine concurrent
    # version bump (approve / clock-out / report on a selected entry between the
    # eligibility SELECT and this flush) would be eaten there and the next flush would
    # surface as PendingRollbackError (500). Flush the mutations here, under our own
    # guard, so StaleDataError is caught and translated while the transaction is still
    # cleanly rollback-able. The commit-time guard below stays as a backstop.
    try:
        db.flush()
    except StaleDataError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="This operation was modified concurrently. Refresh and retry.",
        ) from exc

    # The reopen, AFTER the walk landed under the guard above (we hold this operation's row
    # lock and just wrote it, so its second write in this transaction cannot be stale):
    # back to IN_PROGRESS, current_operation_id, and the demotions through the ONE demotion
    # core (scope "blocked_by"). Flushed under the same guard BEFORE any audit write --
    # ``log_demotions_required`` requires it (invariant 4).
    demoted_operations: list[WorkOrderOperation] = []
    if reopening:
        demoted_operations = reopen_completed_operation(db, work_order, operation)
        try:
            db.flush()
        except StaleDataError as exc:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail="This operation was modified concurrently. Refresh and retry.",
            ) from exc

    primary_entry_id = reduction.time_entry_reductions[0].time_entry_id if reduction.time_entry_reductions else None

    # Best-effort telemetry (never fails the request); emitted BEFORE commit because
    # emit_best_effort only flushes.
    OperationalEventService(db).emit_best_effort(
        company_id=company_id,
        event_type="operation_production_reduced",
        source_module=event_source_module,
        entity_type="work_order_operation",
        entity_id=operation.id,
        work_order_id=work_order.id,
        operation_id=operation.id,
        user_id=actor.id,
        severity="info",
        event_payload={
            "work_order_number": work_order.work_order_number,
            "operation_number": operation.operation_number,
            "quantity_delta": delta,
            "quantity_complete_before": reduction.operation_quantity_complete_before,
            "quantity_complete_after": reduction.operation_quantity_complete_after,
            "time_entry_id": primary_entry_id,
            "time_entries": [s.as_dict() for s in reduction.time_entry_reductions],
            "reason": reason,
            "source": recorded_source,
        },
    )

    # Tamper-evident audit (hash chain): old->new quantity_complete + the affected time
    # entries, carrying the caller-supplied correction reason. Flushed inside this unit
    # of work so it commits atomically with the reduction. The audited diff also carries
    # the SUM of the walked entries' quantity_produced before->after: that sum is ALWAYS
    # lowered by a positive delta, so log_update's empty-diff skip can never fire and a
    # correction can never commit unaudited (defense-in-depth). Per-entry before/after
    # slices ride extra_data so a reviewer can reconstruct exactly which evidence rows
    # were walked down, by how much, and in what order.
    audit.log_update(
        resource_type="work_order_operation",
        resource_id=operation.id,
        resource_identifier=f"WO {work_order.work_order_number} / OP {operation.operation_number}",
        old_values={
            "quantity_complete": reduction.operation_quantity_complete_before,
            "time_entry_quantity_produced": reduction.time_entry_quantity_produced_before,
        },
        new_values={
            "quantity_complete": reduction.operation_quantity_complete_after,
            "time_entry_quantity_produced": reduction.time_entry_quantity_produced_after,
        },
        action="reduce_operation_production",
        description=(
            f"Corrected over-reported production on operation {operation.operation_number} "
            f"for WO {work_order.work_order_number}. Removed {delta:g}. "
            f"Qty: {reduction.operation_quantity_complete_after:g}/{target_qty:g}. "
            f"Reason: {reason}" + (f". Notes: {notes}" if notes else "")
        ),
        extra_data={
            "reason": reason,
            "quantity_delta": delta,
            "time_entry_id": primary_entry_id,
            "time_entry_quantity_produced_before": reduction.time_entry_quantity_produced_before,
            "time_entry_quantity_produced_after": reduction.time_entry_quantity_produced_after,
            "time_entries": [s.as_dict() for s in reduction.time_entry_reductions],
            "rework_delta": reduction.rework_delta,
            "work_order_id": work_order.id,
            "work_order_quantity_complete_before": reduction.work_order_quantity_complete_before,
            "work_order_quantity_complete_after": reduction.work_order_quantity_complete_after,
            # F5: disambiguate the two correction verbs on the audit chain.
            "path": path,
            "reopened_operation": reopening,
        },
    )

    # The reopen is REQUIRED evidence (invariant 2, fail-closed): a completed operation
    # moved back to the floor, and the operations it demoted, must not land without their
    # rows -- otherwise the whole correction rolls back and the caller gets a 503.
    if reopening and reopen_accounting is not None:
        with required_audit(db, CORRECTION_AUDIT_UNAVAILABLE):
            audit.log_required(
                action=audit.ACTIONS["STATUS_CHANGE"],
                resource_type="work_order_operation",
                resource_id=operation.id,
                resource_identifier=operation.operation_number,
                description=(
                    f"Reopened operation {operation.operation_number} on WO {work_order.work_order_number}: "
                    f"lowering it by {delta:g} would have left a completed operation short. Reason: {reason}"
                ),
                old_values={"status": reopened_from},
                new_values={"status": OperationStatus.IN_PROGRESS.value},
                extra_data={
                    **accounting_extra_data(reopen_accounting),
                    "reason": reason,
                    "quantity_delta": delta,
                    "quantity_unaccounted": reopen_accounting.unaccounted,
                    "transition": "reopened_by_correction",
                    "operation_number": operation.operation_number,
                    "sequence": operation.sequence,
                    "demoted_operation_ids": [op.id for op in demoted_operations],
                    "path": path,
                },
            )
            log_demotions_required(
                audit,
                demoted_operations,
                reason=(
                    f"operation {operation.operation_number} was reopened by an over-count correction and is no "
                    "longer complete"
                ),
                extra={"transition": "predecessor_reopened", "reopened_operation_id": operation.id},
            )

    # The re-stamped declaration moves what the later stages are credited with: REQUIRED
    # evidence (invariant 2, fail-closed), like the reopen.
    if restamp is not None and reopen_accounting is not None:
        with required_audit(db, CORRECTION_AUDIT_UNAVAILABLE):
            audit.log_required(
                action=audit.ACTIONS["UPDATE"],
                resource_type="work_order_operation",
                resource_id=operation.id,
                resource_identifier=operation.operation_number,
                description=(
                    f"Re-declared the short on completed operation {operation.operation_number} for WO "
                    f"{work_order.work_order_number}: lowering it by {delta:g} leaves "
                    f"{float(restamp[1] or 0):g} of what reached it scrapped. Reason: {reason}"
                ),
                old_values={"quantity_short_declared": restamp[0]},
                new_values={"quantity_short_declared": restamp[1]},
                extra_data={
                    **accounting_extra_data(reopen_accounting),
                    "reason": reason,
                    "quantity_delta": delta,
                    "transition": "short_redeclared_by_correction",
                    "path": path,
                },
            )

    # F3 (per-entry audit discoverability): the operation-level row above is the
    # aggregate, but an auditor sampling a SPECIFIC TimeEntry needs a resource-keyed
    # lookup (resource_type="time_entry", resource_id=<entry id>) to surface an
    # administrative reduction of that entry's evidence. Emit one secondary row per
    # walked entry -- same AuditService, same unit of work, atomic with the mutation.
    # extra_data links back to the operation/WO, the actor's reason, the path, and the
    # ORIGINAL operator's user_id (which differs from the audit row's actor on a
    # cross-operator office walk).
    entry_by_id = {entry.id: entry for entry in entries}
    for entry_slice in reduction.time_entry_reductions:
        walked_entry = entry_by_id.get(entry_slice.time_entry_id)
        audit.log_update(
            resource_type="time_entry",
            resource_id=entry_slice.time_entry_id,
            resource_identifier=str(entry_slice.time_entry_id),
            old_values={"quantity_produced": entry_slice.quantity_produced_before},
            new_values={"quantity_produced": entry_slice.quantity_produced_after},
            action="reduce_operation_production",
            description=(
                f"Over-count correction walked down time entry {entry_slice.time_entry_id}"
                + (f" (user {walked_entry.user_id})" if walked_entry is not None else "")
                + f" on operation {operation.operation_number} for WO {work_order.work_order_number}: "
                f"{entry_slice.quantity_produced_before:g} -> {entry_slice.quantity_produced_after:g}. "
                f"Reason: {reason}"
            ),
            extra_data={
                "operation_id": operation.id,
                "work_order_id": work_order.id,
                "entry_user_id": walked_entry.user_id if walked_entry is not None else None,
                "entry_type": entry_slice.entry_type,
                "reason": reason,
                "quantity_delta": delta,
                "path": path,
            },
        )

    try:
        db.commit()
    except StaleDataError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="This operation was modified concurrently. Refresh and retry.",
        ) from exc
    db.refresh(operation)
    db.refresh(work_order)

    return ProductionReductionOutcome(
        operation=operation,
        work_order=work_order,
        reduction=reduction,
        target_qty=target_qty,
    )
