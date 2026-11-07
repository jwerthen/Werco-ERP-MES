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

import re
from typing import Iterable, List, Optional, Sequence

from sqlalchemy import and_
from sqlalchemy.orm import Session

from app.models.time_entry import TimeEntry
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.services.work_order_state_service import (
    TERMINAL_WO_STATUSES,
    OperationAccounting,
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


# --------------------------------------------------------------------------- #
# Short count at completion (owner ruling 1, 2026-11-05): a short count is reported as
# scrap with a reason, or the operation stays open -- never short-closed at the counted
# quantity. The refusals below are PLAIN STRINGS from one module, so every kiosk,
# desktop and MCP surface renders them verbatim. Each names the operation by its
# ``operation_label`` and says what to do next. The arithmetic behind them is
# ``work_order_state_service.OperationAccounting`` -- never re-derive it here.
# --------------------------------------------------------------------------- #

# A standing machine credential (``app.core.credentials.is_api_token_credential``) may
# close a full count but never closes work short: declaring where missing pieces went is
# a judgment a named person makes. A crew-station badge token is a person and may.
API_TOKEN_SHORT_CLOSE = "Closing work short must be done by a person signed in to the app, not an API token."
# The floor completion audit row is REQUIRED evidence on a short close or a scrap
# declaration (``AuditService.log_required``); when it cannot be written nothing is saved.
COMPLETION_AUDIT_UNAVAILABLE = "The completion audit could not be saved; nothing was changed. Please retry."

# An existing "Op"/"Operation" prefix (followed by a separator or a digit) or a
# self-labeled "Nest N" identifier is NOT prefixed again -- the server-side twin of the
# display rule in ``frontend/src/utils/operationLabel.ts``, so a legacy "Op 10" row or a
# laser nest never reads "Op Op 10" / "Op Nest 3". The stored value is never rewritten.
_ALREADY_LABELED = re.compile(r"^\s*(?:op(?:eration)?(?=[\s\-_.:#]|\d)|nest\s)", re.IGNORECASE)


def operation_label(operation: WorkOrderOperation) -> str:
    """``"Op 10"`` for messages: ``operation_number``, else the sequence."""
    ident = str(operation.operation_number or operation.sequence or operation.id or "").strip()
    if _ALREADY_LABELED.match(ident):
        return ident
    return f"Op {ident}"


def _qty_text(value: float) -> str:
    """``8`` / ``0.5`` -- float noise rounded off, no trailing ``.0``."""
    return f"{round(float(value or 0), 6):g}"


def _join_names(names: Sequence[str]) -> str:
    names = [name for name in names if name]
    if len(names) <= 1:
        return names[0] if names else "Another operator"
    return ", ".join(names[:-1]) + " and " + names[-1]


def short_count_message(operation: WorkOrderOperation, acct: OperationAccounting) -> str:
    """409: the close would leave received units unaccounted -- nothing was written."""
    message = (
        f"{operation_label(operation)}: {_qty_text(acct.unaccounted)} of {_qty_text(acct.received)} not yet "
        f"accounted for ({_qty_text(acct.good)} good, {_qty_text(acct.scrapped)} scrapped). Record finished pieces "
        "(clock out with your count, or Report production), report lost pieces as scrap with a reason, or leave "
        "the operation open. Pieces never started: ask a supervisor to lower the quantity."
    )
    if acct.upstream_short > 1e-9:
        message += f" ({_qty_text(acct.upstream_short)} never reached it — closed short upstream.)"
    return message


def others_clocked_in_message(operation: WorkOrderOperation, names: Sequence[str], *, office: bool = False) -> str:
    """409: a short close would clock out other operators before they reported their count."""
    verb = "is" if len([name for name in names if name]) <= 1 else "are"
    message = (
        f"{_join_names(names)} {verb} still clocked in to {operation_label(operation)} — have them clock out with "
        "their count first, or leave the operation open."
    )
    if office:
        message += (
            " Resubmit with acknowledge_open_entries to clock them out; pieces they have not reported will not be "
            "counted."
        )
    return message


def serialized_short_message(operation: WorkOrderOperation, short: float, serials: Iterable[str]) -> str:
    """409: a short close on a serialized work order whose per-serial step records are missing."""
    serial_list = [str(serial) for serial in serials if serial]
    missing_for = ", ".join(serial_list) if serial_list else "this operation"
    return (
        f"{operation_label(operation)} is {_qty_text(short)} short on a serialized work order and required step "
        f"records are missing for {missing_for}. Scrapped serials can't be recorded yet — leave the operation open "
        "and ask a supervisor. Never record steps for a part that was scrapped."
    )


def ceiling_exceeded_message(operation: WorkOrderOperation, quantity: float, acct: OperationAccounting) -> str:
    """400: more good than can reach this operation once upstream closes declared shorts."""
    return (
        f"Quantity ({_qty_text(quantity)}) exceeds the {_qty_text(acct.ceiling)} units that can reach "
        f"{operation_label(operation)} — {_qty_text(acct.chain_upstream)} were closed short upstream."
    )


def exceeds_recorded_message(operation: WorkOrderOperation, quantity: float, recorded_good: float) -> str:
    """409: the floor close asserted more good than the floor recorded (it records none itself)."""
    return (
        f"quantity_complete ({_qty_text(quantity)}) exceeds the {_qty_text(recorded_good)} good recorded on "
        f"{operation_label(operation)}; record the pieces through clock-out or Report production, or report the "
        "shortfall as scrap with a reason."
    )


def scrap_overdeclare_message(operation: WorkOrderOperation, unaccounted: float, declared: float) -> str:
    """409: the scrap declared at close is more than is still unaccounted (a stale screen)."""
    return (
        f"{operation_label(operation)} has only {_qty_text(unaccounted)} unaccounted; declaring "
        f"{_qty_text(declared)} scrap would over-state it. Refresh and re-enter; extra scrap belongs on clock-out "
        "or Report production."
    )


def completed_operation_clock_out_message(operation: WorkOrderOperation) -> str:
    """409: a quantity-carrying clock-out on an operation someone already completed."""
    return (
        f"{operation_label(operation)} is already complete; clock out with 0 pieces and tell a supervisor about any "
        "extra pieces."
    )


# The office twins of the floor refusals above (owner ruling 1). Same module, same
# plain-string contract, same ``operation_label`` naming.

# 422 on the office op-complete when the scrap TOTAL rises without a reason or code --
# the same sentence the floor schema and the force-complete use.
MSG_SCRAP_REASON_OR_CODE_REQUIRED = (
    "scrap_reason or scrap_reason_code_id is required when quantity_scrapped is greater than 0"
)
# 422 on the OPERATOR's reduce-production verb: re-opening a completed operation is a
# supervised correction, never operator self-service.
MSG_REOPEN_IS_OFFICE_ONLY = "reopen_operation is an office correction"
# 503 when the reopen's REQUIRED audit rows cannot be written (nothing was changed).
CORRECTION_AUDIT_UNAVAILABLE = "The correction audit could not be saved; nothing was changed. Please retry."


def office_no_change_message(operation: WorkOrderOperation) -> str:
    """200 on the office op-complete when nothing would change: nothing was written."""
    return f"No change — {operation_label(operation)} left open"


def override_reason_required_message(operation: WorkOrderOperation, recorded: float) -> str:
    """422: the desk recorded more good than the floor counted without saying why."""
    return (
        f"quantity_override_reason is required to record more good than the {_qty_text(recorded)} counted on the "
        f"floor for {operation_label(operation)}"
    )


def completed_counts_locked_message(operation: WorkOrderOperation) -> str:
    """409: the generic operation PUT editing a COMPLETE operation's counts or status."""
    return (
        f"{operation_label(operation)} is complete; its counts are the completion record. Correct the good count "
        "with reduce-production (it can reopen the operation)."
    )


def reduce_would_leave_short_message(operation: WorkOrderOperation, delta: float, short: float) -> str:
    """409: an office over-count correction would leave a COMPLETE operation short."""
    return (
        f"Lowering {operation_label(operation)} by {_qty_text(delta)} would leave it {_qty_text(short)} short, and a "
        "completed operation cannot be short. Resubmit with reopen_operation to reopen it (it returns to in "
        "progress)."
    )


def reopen_blocked_by_later_message(later: WorkOrderOperation) -> str:
    """409: a later stage of the same chain already completed with these units."""
    label = operation_label(later)
    return f"{label} is already complete with these units; correct {label} first."


def quantity_ordered_floor_message(operation: WorkOrderOperation, floor: float) -> str:
    """409: ``PUT /work-orders/{id}`` lowering the order below what a stage accounted for."""
    return (
        f"{operation_label(operation)} has already accounted for {_qty_text(floor)} units; lower the order only by "
        "units no operation has started."
    )


def force_complete_exceeds_recorded_message(
    quantity: float, deliverable: float, binding: Optional[WorkOrderOperation]
) -> str:
    """409: a force-complete books more than the floor recorded, with no override reason."""
    where = f" at {operation_label(binding)}" if binding is not None else ""
    return (
        f"quantity_complete ({_qty_text(quantity)}) exceeds the {_qty_text(deliverable)} the floor recorded{where}; "
        "record the pieces there, or give quantity_override_reason"
    )


def force_complete_exceeds_physical_message(quantity: float, physical_max: float, lost: float) -> str:
    """400: even with an override, more than can exist once declared shorts are out."""
    return (
        f"quantity_complete ({_qty_text(quantity)}) exceeds the {_qty_text(physical_max)} that can still be "
        f"delivered; {_qty_text(lost)} were reported scrapped short"
    )


def force_complete_unaccounted_message(short: float, ordered: float, quantity: float, scrapped: float) -> str:
    """409: a force-complete would close the work order short with the shortfall unaccounted."""
    return (
        f"cannot complete work order: {_qty_text(short)} of {_qty_text(ordered)} unaccounted for "
        f"({_qty_text(quantity)} complete, {_qty_text(scrapped)} scrapped); report the short as scrap with a "
        "reason, or leave the work order open"
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
    * ON_HOLD -> :data:`MSG_WORK_ORDER_ON_HOLD` -- put back on the floor by
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
    it first), the generic ``PUT /work-orders/operations/{id}`` when it writes ``status``
    IN_PROGRESS (a hand-set start; READY on a DRAFT is refused there with
    :data:`MSG_WORK_ORDER_NOT_RELEASED` as well), :func:`clock_in_blockers` and
    :func:`complete_blockers` (the scanner), and the floor read payloads'
    ``start_blocked_reason`` (``/shop-floor/operations`` rows,
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
