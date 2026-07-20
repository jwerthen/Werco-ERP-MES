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
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation
from app.services.work_order_state_service import (
    TERMINAL_WO_STATUSES,
    has_incomplete_predecessors,
    is_laser_dispatch_work_order,
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
    """
    if is_laser_dispatch_work_order(operation.work_order):
        return False
    return has_incomplete_predecessors(
        db,
        operation.work_order_id,
        operation.sequence,
        operation.id,
        operation.work_center_id,
        allow_same_work_center=True,
    )


def clock_in_blockers(
    db: Session, operation: WorkOrderOperation, user_id: int, work_center_id: Optional[int] = None
) -> List[str]:
    """Reasons POST /shop-floor/clock-in would refuse this user on this operation.

    Mirrors the handler's gate order: duplicate open entry -> work center ->
    status -> predecessors. Empty list == the clock-in gates pass.

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


def complete_blockers(db: Session, operation: WorkOrderOperation, work_order: WorkOrder) -> List[str]:
    """Reasons POST /shop-floor/operations/{id}/complete would refuse right now.

    State gates only -- quantity validation is input-shaped and cannot be
    evaluated without a requested quantity.
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
    if operation_blocked_by_predecessors(db, operation):
        reasons.append(MSG_PREDECESSORS_INCOMPLETE)
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
