"""Scan-code resolution for POST /api/v1/scanner/resolve-action (A0.4).

Read-only by design: resolving a scan writes NO audit rows and emits NO
OperationalEvents -- it has GET semantics in a POST body (the POST is only so
raw scanner text never lands in a URL/access log). All lookups are scoped to
the active company; a code that exists in another tenant resolves to
``kind="unknown"`` exactly like a code that exists nowhere.

Legal-action derivation REUSES the shop-floor gate predicates
(``app/services/operation_action_gates.py``) -- the same code paths the write
endpoints call -- so the resolver can never disagree with what
``/shop-floor/clock-in`` etc. would actually allow.
"""

from datetime import datetime
from typing import List, Optional, Tuple, Union

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload, selectinload

from app.db.tenant_filter import tenant_query
from app.models.routing import Routing
from app.models.user import User
from app.models.work_order import WorkOrder, WorkOrderOperation
from app.schemas.scanner import (
    EmployeeScanResult,
    OperationScanResult,
    OperationScanSummary,
    RoutingRevisionCheck,
    ScanAction,
    UnknownScanResult,
    WorkOrderOperationBrief,
    WorkOrderScanResult,
    WorkOrderScanSummary,
)
from app.services.operation_action_gates import (
    clock_in_blockers,
    complete_blockers,
    hold_blockers,
    report_production_blockers,
    resume_blockers,
)

# _active_operation_id is module-private by convention but is the canonical
# "which operation is the WO on" rule (first IN_PROGRESS, else first READY,
# else first non-COMPLETE, by sequence); reuse it rather than fork the logic.
from app.services.work_order_state_service import _active_operation_id, operation_target_quantity

OP_PREFIX = "OP:"
WO_PREFIX = "WO:"

ROUTING_CHECK_NOTE = (
    "Work orders do not snapshot the routing revision their operations were generated from, "
    "and traveler prints are not recorded server-side; this compares the part's current "
    "released routing's release timestamps against the work order release/creation time as "
    "a proxy for the traveler print baseline."
)

ScanResult = Union[OperationScanResult, WorkOrderScanResult, EmployeeScanResult, UnknownScanResult]


def resolve_scan_code(
    db: Session,
    *,
    company_id: int,
    user: User,
    code: str,
    work_center_id: Optional[int] = None,
) -> ScanResult:
    """Resolve raw scanned text into a typed, tenant-scoped scan result."""
    raw = code.strip()
    if not raw:
        return UnknownScanResult(code=code, reason="Empty code")

    upper = raw.upper()
    if upper.startswith(OP_PREFIX):
        return _resolve_operation(db, company_id=company_id, user=user, raw=raw, work_center_id=work_center_id)
    if upper.startswith(WO_PREFIX):
        return _resolve_work_order(db, company_id=company_id, raw=raw)
    return _resolve_employee(db, company_id=company_id, raw=raw)


def _resolve_operation(
    db: Session,
    *,
    company_id: int,
    user: User,
    raw: str,
    work_center_id: Optional[int],
) -> ScanResult:
    id_text = raw[len(OP_PREFIX) :].strip()
    # str.isdigit() alone is NOT a safe int() guard: it accepts non-ASCII digit forms
    # like "OP:²" that int() rejects (500), and an unbounded digit string overflows
    # the DB integer range. 18 digits stays inside a signed 64-bit integer.
    if not (id_text.isascii() and id_text.isdigit()) or len(id_text) > 18:
        return UnknownScanResult(code=raw, reason="Malformed operation code (expected OP:<id>)")
    operation_id = int(id_text)

    operation = (
        tenant_query(db, WorkOrderOperation, company_id)
        .options(
            joinedload(WorkOrderOperation.work_order).joinedload(WorkOrder.part),
            joinedload(WorkOrderOperation.work_center),
        )
        .filter(WorkOrderOperation.id == operation_id)
        .first()
    )
    if operation is None:
        return UnknownScanResult(code=raw, reason="No operation matches this code")

    work_order = operation.work_order
    # Soft-delete awareness: an operation whose WO is deleted must not resolve.
    if work_order is None or work_order.is_deleted:
        return UnknownScanResult(code=raw, reason="No operation matches this code")

    legal_actions, blockers = _evaluate_actions(db, operation, work_order, user, company_id, work_center_id)
    revision_check, warning = _routing_revision_check(db, company_id, work_order)

    part = work_order.part
    summary = OperationScanSummary(
        id=operation.id,
        sequence=operation.sequence,
        operation_number=operation.operation_number,
        name=operation.name,
        status=operation.status.value if hasattr(operation.status, "value") else str(operation.status),
        work_order_id=work_order.id,
        work_order_number=work_order.work_order_number,
        work_order_status=work_order.status.value if hasattr(work_order.status, "value") else str(work_order.status),
        part_number=part.part_number if part else None,
        part_name=part.name if part else None,
        work_center_id=operation.work_center_id,
        work_center_name=operation.work_center.name if operation.work_center else None,
        work_center_match=(operation.work_center_id == work_center_id) if work_center_id is not None else None,
        quantity_complete=float(operation.quantity_complete or 0),
        target_quantity=float(operation_target_quantity(operation, work_order) or 0),
    )
    return OperationScanResult(
        code=raw,
        operation=summary,
        legal_actions=legal_actions,
        blockers=blockers,
        warning=warning,
        routing_revision_check=revision_check,
    )


def _evaluate_actions(
    db: Session,
    operation: WorkOrderOperation,
    work_order: WorkOrder,
    user: User,
    company_id: int,
    work_center_id: Optional[int] = None,
) -> Tuple[List[ScanAction], dict]:
    """Run every shop-floor gate for the calling user; split legal vs blocked.

    ``work_center_id`` is the scanning station, when the request carried one --
    clock-in is then gated to that station exactly like the real endpoint
    (``POST /shop-floor/clock-in`` 400s on a work-center mismatch). With no
    station id the evaluation is unchanged.
    """
    gate_results: List[Tuple[ScanAction, List[str]]] = [
        ("clock_in", clock_in_blockers(db, operation, user.id, work_center_id=work_center_id)),
        ("report_production", report_production_blockers(db, operation, user.id, company_id)),
        ("complete", complete_blockers(db, operation, work_order)),
        ("hold", hold_blockers(operation)),
        ("resume", resume_blockers(operation)),
    ]
    legal: List[ScanAction] = [action for action, reasons in gate_results if not reasons]
    blocked = {action: reasons for action, reasons in gate_results if reasons}
    return legal, blocked


def _routing_revision_check(
    db: Session, company_id: int, work_order: WorkOrder
) -> Tuple[Optional[RoutingRevisionCheck], Optional[str]]:
    """Best-supported routing staleness signal (see RoutingRevisionCheck docstring).

    Returns (check, warning) where warning is "routing_revision_changed" when the
    part's current released routing was released AFTER the work order's release/
    creation baseline -- i.e. any traveler printed from this WO predates the
    routing now in force.
    """
    routing = (
        tenant_query(db, Routing, company_id)
        .filter(
            Routing.part_id == work_order.part_id,
            Routing.is_active == True,  # noqa: E712
            Routing.status == "released",
            Routing.is_deleted == False,  # noqa: E712
        )
        # Latest RELEASE wins, not latest row: approved_at is stamped at release
        # time, so order by it (id only as a tiebreak / for legacy NULLs).
        .order_by(Routing.approved_at.desc().nullslast(), Routing.id.desc())
        .first()
    )
    if routing is None:
        return None, None

    baseline: Optional[datetime] = work_order.released_at or work_order.created_at
    # The release endpoint stamps approved_at + effective_date at release time.
    routing_released_at: Optional[datetime] = routing.approved_at or routing.effective_date or routing.created_at

    changed: Optional[bool] = None
    if baseline is not None and routing_released_at is not None:
        changed = routing_released_at > baseline

    check = RoutingRevisionCheck(
        current_released_revision=routing.revision,
        released_routing_changed_after_wo_creation=changed,
        checked_against=baseline.isoformat() if baseline else None,
        note=ROUTING_CHECK_NOTE,
    )
    return check, ("routing_revision_changed" if changed else None)


def _resolve_work_order(db: Session, *, company_id: int, raw: str) -> ScanResult:
    number = raw[len(WO_PREFIX) :].strip()
    if not number:
        return UnknownScanResult(code=raw, reason="Malformed work order code (expected WO:<number>)")

    query = (
        tenant_query(db, WorkOrder, company_id)
        .options(joinedload(WorkOrder.part), selectinload(WorkOrder.operations))
        .filter(WorkOrder.is_deleted == False)  # noqa: E712
    )
    work_order = query.filter(WorkOrder.work_order_number == number).first()
    if work_order is None:
        # Case-insensitive EXACT fallback (scanners/keyboards can mangle case).
        # NOT ilike(): % and _ in scanned text act as SQL wildcards there, so a
        # stray "WO:%" scan would resolve to an arbitrary work order.
        work_order = query.filter(func.lower(WorkOrder.work_order_number) == number.lower()).first()
    if work_order is None:
        return UnknownScanResult(code=raw, reason="No work order matches this code")

    operations = sorted(work_order.operations or [], key=lambda op: op.sequence)
    briefs = [
        WorkOrderOperationBrief(
            id=op.id,
            sequence=op.sequence,
            operation_number=op.operation_number,
            name=op.name,
            status=op.status.value if hasattr(op.status, "value") else str(op.status),
        )
        for op in operations
    ]
    # Canonical "operation the WO is currently on" -- the same helper that
    # maintains work_order.current_operation_id (first IN_PROGRESS by sequence,
    # else first READY, else first non-COMPLETE), so a WO scan can never
    # disagree with the rest of the system about where the job is.
    current_operation_id = _active_operation_id(work_order)
    summary = WorkOrderScanSummary(
        id=work_order.id,
        work_order_number=work_order.work_order_number,
        status=work_order.status.value if hasattr(work_order.status, "value") else str(work_order.status),
        quantity_ordered=float(work_order.quantity_ordered or 0),
        quantity_complete=float(work_order.quantity_complete or 0),
        part_number=work_order.part.part_number if work_order.part else None,
        part_name=work_order.part.name if work_order.part else None,
        current_operation_id=current_operation_id,
    )
    return WorkOrderScanResult(code=raw, work_order=summary, operations=briefs)


def _resolve_employee(db: Session, *, company_id: int, raw: str) -> ScanResult:
    """Badge lookup only -- never authenticates (login stays on /auth/employee-login).

    The A0.4 badge sheets encode ``users.employee_id`` verbatim, so we probe an
    EXACT, company-scoped, active-user match for any unprefixed code. Digits-only
    codes are the documented badge shape; alphanumeric legacy ids (e.g.
    "EMP-00339") resolve too because the printed payload is the stored id.
    """
    user = (
        tenant_query(db, User, company_id).filter(User.employee_id == raw, User.is_active == True).first()  # noqa: E712
    )
    if user is None:
        if raw.isdigit():
            return UnknownScanResult(code=raw, reason="No employee badge matches this id")
        return UnknownScanResult(code=raw, reason="Unrecognized code")
    return EmployeeScanResult(
        code=raw,
        employee_id=user.employee_id,
        first_name=user.first_name,
        last_initial=(user.last_name[:1].upper() if user.last_name else ""),
    )
