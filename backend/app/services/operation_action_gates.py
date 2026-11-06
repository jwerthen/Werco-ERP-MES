"""Shared shop-floor action gating (A0.4 QR/scan plumbing).

These predicates are EXTRACTED from the ``/shop-floor`` write handlers so the
scanner ``resolve-action`` endpoint can answer "which actions could the calling
user perform RIGHT NOW on this operation?" with the SAME logic the write
endpoints enforce. The write handlers keep their own raise-ordering and HTTP
semantics; they now call these helpers for the *decision* so the gating can
never drift between the resolver and the real endpoints.

Byte-identical contract: ``clock_in`` in ``app/api/endpoints/shop_floor.py``
must behave exactly as before this extraction -- the existing shop-floor /
adoption-telemetry / completion-matrix test suites lock that.

The blocker MESSAGES are kept verbatim from the endpoints so a kiosk that shows
a resolver blocker and a kiosk that gets the endpoint's 400 show the same text.
"""

from typing import List, Optional

from sqlalchemy import and_
from sqlalchemy.orm import Session

from app.models.time_entry import TimeEntry
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.services.work_order_state_service import (
    TERMINAL_WO_STATUSES,
    has_incomplete_predecessors,
    is_laser_dispatch_work_order,
    work_order_allows_same_work_center,
)

# Status lists used by the shop-floor handlers (verbatim).
CLOCK_IN_ALLOWED_STATUSES = [
    OperationStatus.PENDING,
    OperationStatus.READY,
    OperationStatus.IN_PROGRESS,
]
COMPLETE_ALLOWED_STATUSES = [OperationStatus.IN_PROGRESS, OperationStatus.READY]

# Blocker messages, verbatim from the shop-floor handlers.
MSG_ALREADY_CLOCKED_IN = "You are already clocked in to this operation."
MSG_WRONG_WORK_CENTER = "Operation does not belong to this work center"
MSG_NOT_READY_TO_START = "Operation is not ready to start"
MSG_PREDECESSORS_INCOMPLETE = "Previous operations must be completed first"
MSG_MUST_BE_IN_PROGRESS = "Operation must be in progress to add completed quantity"
MSG_MUST_BE_CLOCKED_IN = "You must be clocked in to add completed quantity"
MSG_ALREADY_COMPLETE = "Operation is already complete"
MSG_ON_HOLD_CANNOT_COMPLETE = "Operation is on hold and cannot be completed"
MSG_CANNOT_HOLD_COMPLETE = "Cannot put completed operation on hold"
MSG_NOT_ON_HOLD = "Operation is not on hold"
# Deliberately NOT MSG_MUST_BE_CLOCKED_IN, which means something else: that verb needs
# the CALLER clocked in right now. This one fires only when NOBODY has ever clocked in
# to the operation, and the flow that hits it most is a screen where the operator just
# clocked out -- telling them "you must be clocked in" there would send them looking for
# a state they correctly left. Worded as the action to take, not the row that is missing.
MSG_NO_LABOR_RECORDED = "Clock in to this operation before completing it — no one has clocked in to it yet."
# Verbatim from PUT /shop-floor/operations/{id}/start, now shared with its office twin.
MSG_ALREADY_IN_PROGRESS = "Operation is already in progress"

# Work-order status gates (owner ruling 2026-11-05: the floor cannot start work on a job a
# supervisor has not released, or on one a supervisor has put on hold). Static strings on
# purpose -- no work-order number interpolated -- so the scanner resolver's blocker text,
# the read payloads' ``start_blocked_reason`` and every write verb's 409 are byte-identical
# and a doc can quote them exactly. Every surface renders a plain-string ``detail``
# verbatim, so these are what the operator reads.
MSG_WORK_ORDER_NOT_RELEASED = (
    "This work order has not been released — release first. Work cannot start until a supervisor releases it."
)
MSG_WORK_ORDER_ON_HOLD = (
    "This work order is on hold — work cannot start until a supervisor resumes it (Resume Work Order on the "
    "work order). Resuming or clearing an operation's hold does not lift it."
)
# Both start verbs used to flip an ON_HOLD operation straight to IN_PROGRESS, silently
# lifting the hold (and leaving its blocker open). Starting is not how a hold is lifted.
MSG_OPERATION_ON_HOLD_NO_START = (
    "This operation is on hold — lift it with Resume (kiosk) or Clear Hold (desk) first. "
    "Starting it does not lift a hold."
)
# PUT /work-orders/{id} {status: on_hold} on a DRAFT. Every hold placed from now on is on
# RELEASED work, which is what lets POST /work-orders/{id}/resume decide where a held job
# goes back to without guessing whether it was ever authorized for the floor.
MSG_DRAFT_CANNOT_BE_HELD = (
    "A draft work order has not been released, so there is nothing on the floor to hold. "
    "Leave it in draft until it is ready, then release it."
)


def _status_value(status: object) -> str:
    """Wire value of a status that may be an enum member or an already-plain string."""
    value = getattr(status, "value", status)
    return str(value)


def work_order_start_blocker(work_order: Optional[WorkOrder]) -> Optional[str]:
    """Why NO operation of this work order may be STARTED right now, or ``None``.

    THE one copy of the work-order-status half of the start gate. Add no inline twins:
    copies of a gate are how the office and floor rules drifted apart before.

    * terminal (COMPLETE / CLOSED / CANCELLED) -> ``"cannot start operation: work order
      is <status>"``, byte-identical to the f-string the shop-floor start verb has always
      raised;
    * DRAFT -> :data:`MSG_WORK_ORDER_NOT_RELEASED` -- Release is the authorization step
      and the record of who put the job on the floor, so the floor never starts work the
      release has not authorized (and never releases it as a side effect);
    * ON_HOLD -> :data:`MSG_WORK_ORDER_ON_HOLD` -- lifted only by
      ``POST /work-orders/{id}/resume`` ("Resume Work Order"), never by starting.

    ``None`` means "no WORK-ORDER refusal", never "startable": the operation's own
    status, the predecessor gate, the work center and open entries are decided by the
    write verb at the moment of the action. A soft-deleted work order is the RESOLVER's
    404, not this helper's business. ``work_order=None`` returns ``None`` for the same
    reason -- the caller's resolver owns that 404.

    Callers (every one of them, so a reader can check the rule is one rule):
    ``POST /shop-floor/clock-in`` (kiosk, crew JOIN, desk Check In, Time Clock, paper
    back-entry -- no exemption), ``PUT /shop-floor/operations/{id}/start``,
    ``POST /work-orders/operations/{id}/start``, the READY auto-start branch of BOTH
    complete verbs (``POST /shop-floor/operations/{id}/complete`` and
    ``POST /work-orders/operations/{id}/complete`` -- completing a READY operation STARTS
    it first), :func:`clock_in_blockers` and :func:`complete_blockers` (the scanner), and
    the floor read payloads' ``start_blocked_reason`` (``/shop-floor/operations`` rows,
    ``/shop-floor/operations/{id}``, the kiosk queue and held rows).

    Deliberately NOT called by: op-level resume, blocker resolve/dismiss, production
    reporting, completing an operation that is already IN_PROGRESS, and clock-out.
    Labor already running when a job is held must always be closable, and the kiosk's
    finish flow completes an IN_PROGRESS operation after clocking out.

    It reads only THIS operation's work order: a laser child does NOT inherit its
    assembly parent's DRAFT or ON_HOLD (the child is born RELEASED by the nest routes).
    Hold the laser work order itself to stop its nests.
    """
    if work_order is None:
        return None
    status = work_order.status
    if status in TERMINAL_WO_STATUSES:
        return f"cannot start operation: work order is {_status_value(status)}"
    if status == WorkOrderStatus.DRAFT:
        return MSG_WORK_ORDER_NOT_RELEASED
    if status == WorkOrderStatus.ON_HOLD:
        return MSG_WORK_ORDER_ON_HOLD
    return None


def get_open_time_entry(db: Session, user_id: int, operation_id: int) -> Optional[TimeEntry]:
    """The clock-in duplicate pre-check query, verbatim.

    Deliberately NOT company-filtered -- it mirrors the original inline check in
    ``clock_in`` (user id + operation id + open). Callers resolve the operation
    tenant-scoped before consulting this, and the partial unique index
    ``uq_open_time_entry`` is the authoritative DB-level guard.
    """
    return (
        db.query(TimeEntry)
        .filter(
            and_(
                TimeEntry.user_id == user_id,
                TimeEntry.operation_id == operation_id,
                TimeEntry.clock_out.is_(None),
            )
        )
        .first()
    )


def operation_has_labor_evidence(db: Session, operation_id: int, company_id: int) -> bool:
    """True when ANY labor has been recorded against this operation -- open or closed.

    The floor's completion verb refuses an operation with no ``TimeEntry`` at all, which
    is the one thing it can check without breaking how the floor actually works. It is
    deliberately NOT "the caller is clocked in right now":

    * the ``/kiosk`` finish-job button clocks the operator OUT first (quantities and the
      scrap reason land on the TimeEntry) and completes second, so at the moment of the
      call the caller's entry is CLOSED -- an open-entry check would refuse every kiosk
      completion;
    * the crew station authorizes completion with whichever badge is scanned, and the
      endpoint's job is to auto-close the OTHER operators' open entries -- so the
      completing badge often holds no entry of its own.

    Both keep working here, because both leave labor on the operation. What it refuses is
    an operation booked COMPLETE at full quantity that nobody ever worked.

    Tenant-scoped on ``company_id``: another tenant's TimeEntry must never satisfy a
    completion gate. Existence only -- no aggregate, no quantity threshold; a clock-in
    with zero pieces still counts as someone having worked it.
    """
    return (
        db.query(TimeEntry.id)
        .filter(
            TimeEntry.operation_id == operation_id,
            TimeEntry.company_id == company_id,
        )
        .first()
        is not None
    )


def get_company_open_time_entry(db: Session, user_id: int, operation_id: int, company_id: int) -> Optional[TimeEntry]:
    """The production-report active-entry check, verbatim (company-scoped)."""
    return (
        db.query(TimeEntry)
        .filter(
            and_(
                TimeEntry.user_id == user_id,
                TimeEntry.operation_id == operation_id,
                TimeEntry.clock_out.is_(None),
                TimeEntry.company_id == company_id,
            )
        )
        .first()
    )


def operation_blocked_by_predecessors(db: Session, operation: WorkOrderOperation) -> bool:
    """The out-of-sequence guard, verbatim (same args as every shop-floor call site).

    Laser-nest WOs are DISPATCH POOLS, not routings (see
    ``is_laser_dispatch_work_order``): their nest ops never predecessor-block each
    other, even across work centers -- the same-work-center exemption below stops
    helping the moment a package's nests are spread across two lasers. The
    ``operation.work_order`` access is the relationship (already loaded at most
    call sites; otherwise one cheap lazy load).

    ``allow_same_work_center`` is what lets operations on ONE machine be worked in
    any order, and it is now also the READY-promotion rule, so the two cannot drift. It
    is no longer a constant: ``work_order_allows_same_work_center`` resolves it per work
    order from ``sequential_operations`` (081), so a sequenced routing blocks a
    same-work-center predecessor here exactly as promotion refuses to promote past it. It
    carries one carve-out that matters here: an **ON_HOLD** predecessor still blocks, its
    own work center included (see ``has_incomplete_predecessors``). A held operation is a
    quality/material stop, and it would be no stop at all if it took the pool off the
    dispatch board while a badge scan could still start the siblings around it.
    """
    if is_laser_dispatch_work_order(operation.work_order):
        return False
    return has_incomplete_predecessors(
        db,
        operation.work_order_id,
        operation.sequence,
        operation.id,
        operation.work_center_id,
        # 081: no longer hard-coded True. The SAME resolver promote_ready_operations
        # uses, off the SAME work order, so a sequenced routing refuses the badge scan
        # for exactly the operations it also keeps off the dispatch board.
        allow_same_work_center=work_order_allows_same_work_center(operation.work_order),
    )


def clock_in_blockers(
    db: Session, operation: WorkOrderOperation, user_id: int, work_center_id: Optional[int] = None
) -> List[str]:
    """Reasons POST /shop-floor/clock-in would refuse this user on this operation.

    Mirrors the handler's gate order: duplicate open entry -> work center ->
    work-order status (:func:`work_order_start_blocker`: terminal / draft / on hold) ->
    operation status -> predecessors. Empty list == the clock-in gates pass.

    ``work_center_id`` is the station the caller would clock in FROM (the
    handler's ``clock_in_data.work_center_id``); when provided and it differs
    from the operation's work center, clock-in is blocked with the handler's
    exact 400 text. ``None`` (no station known) skips the gate -- the resolver
    must not invent a station the request never carried.
    """
    reasons: List[str] = []
    if get_open_time_entry(db, user_id, operation.id) is not None:
        reasons.append(MSG_ALREADY_CLOCKED_IN)
    if work_center_id is not None and operation.work_center_id != work_center_id:
        reasons.append(MSG_WRONG_WORK_CENTER)
    work_order_reason = work_order_start_blocker(operation.work_order)
    if work_order_reason:
        reasons.append(work_order_reason)
    if operation.status not in CLOCK_IN_ALLOWED_STATUSES:
        reasons.append(MSG_NOT_READY_TO_START)
    if operation_blocked_by_predecessors(db, operation):
        reasons.append(MSG_PREDECESSORS_INCOMPLETE)
    return reasons


def report_production_blockers(db: Session, operation: WorkOrderOperation, user_id: int, company_id: int) -> List[str]:
    """Reasons POST /shop-floor/operations/{id}/production would refuse right now."""
    reasons: List[str] = []
    if operation.status != OperationStatus.IN_PROGRESS:
        reasons.append(MSG_MUST_BE_IN_PROGRESS)
    if get_company_open_time_entry(db, user_id, operation.id, company_id) is None:
        reasons.append(MSG_MUST_BE_CLOCKED_IN)
    return reasons


def complete_blockers(db: Session, operation: WorkOrderOperation, work_order: WorkOrder, company_id: int) -> List[str]:
    """Reasons POST /shop-floor/operations/{id}/complete would refuse right now.

    State gates only -- quantity validation is input-shaped and cannot be
    evaluated without a requested quantity.

    SCOPE: this mirrors the FLOOR verb only. The office twin
    (``POST /work-orders/operations/{id}/complete``) deliberately does NOT carry the
    labor-evidence gate below -- a supervisor or quality user closing an operation from
    the desk, including cleanup of work that was never clocked, is a decision the owner
    kept. Do not "align" the two; the asymmetry is the point and a test pins it.

    Completing a READY operation STARTS it first (both complete verbs auto-start it), so a
    READY operation under a DRAFT or ON_HOLD work order reports the start refusal
    (:func:`work_order_start_blocker`) -- the same 409 both verbs raise. An IN_PROGRESS
    operation does not: work already running when a job is held stays completable. A
    terminal work order is reported once, by its own completion text above it.
    """
    reasons: List[str] = []
    if work_order.status in TERMINAL_WO_STATUSES:
        status_value = work_order.status.value if hasattr(work_order.status, "value") else str(work_order.status)
        reasons.append(f"cannot complete operation: work order is {status_value}")
    if operation.status == OperationStatus.COMPLETE:
        reasons.append(MSG_ALREADY_COMPLETE)
    elif operation.status not in COMPLETE_ALLOWED_STATUSES:
        if operation.status == OperationStatus.ON_HOLD:
            reasons.append(MSG_ON_HOLD_CANNOT_COMPLETE)
        else:
            status_value = operation.status.value if hasattr(operation.status, "value") else str(operation.status)
            reasons.append(f"Cannot complete operation with status: {status_value}")
    if operation.status == OperationStatus.READY and work_order.status not in TERMINAL_WO_STATUSES:
        start_reason = work_order_start_blocker(work_order)
        if start_reason:
            reasons.append(start_reason)
    if operation_blocked_by_predecessors(db, operation):
        reasons.append(MSG_PREDECESSORS_INCOMPLETE)
    if operation.id is not None and not operation_has_labor_evidence(db, operation.id, company_id):
        reasons.append(MSG_NO_LABOR_RECORDED)
    return reasons


def hold_blockers(operation: WorkOrderOperation) -> List[str]:
    """Reasons PUT /shop-floor/operations/{id}/hold would refuse right now."""
    if operation.status == OperationStatus.COMPLETE:
        return [MSG_CANNOT_HOLD_COMPLETE]
    return []


def resume_blockers(operation: WorkOrderOperation) -> List[str]:
    """Reasons PUT /shop-floor/operations/{id}/resume would refuse right now."""
    if operation.status != OperationStatus.ON_HOLD:
        return [MSG_NOT_ON_HOLD]
    return []
