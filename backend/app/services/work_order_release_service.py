"""Releasing a DRAFT work order to production -- one implementation behind both release doors.

Release is the authorization step: it is the record of who put a job on the floor, and
nothing but Release moves a DRAFT work order's operations onto the dispatch board and the
kiosk (``promote_ready_operations`` refuses a draft at every other seam). Two doors reach
it, and they must never disagree about what a release IS:

* ``POST /work-orders/{id}/release`` -- :func:`release_work_order`, one job;
* ``POST /work-orders/bulk-release`` -- :func:`bulk_release_work_orders`, up to
  :data:`BULK_RELEASE_MAX` jobs in one transaction, each judged and released exactly as
  the single door would, with a per-item result instead of a per-request error.

The release itself is :func:`apply_work_order_release` (the DRAFT check, the
at-least-one-operation check, the status / ``released_by`` / ``released_at`` stamp, the
opt-in estimated-cost rollup, the READY promotion and the ``work_order_released`` event)
and its audit row is :func:`audit_work_order_release`. Both doors call both; neither
re-implements either. The readiness check is deliberately just "has an operation" -- no
material or blocker gate -- because that is the single door's contract.

Neither door commits here. The caller commits once, then runs scheduling and the realtime
broadcasts (they need committed rows), exactly as the single route always has.

LOCK ORDER (bulk)
-----------------
Every audit row takes the ONE global audit hash-chain advisory lock and holds it until the
outer transaction ends; a per-item savepoint does not release it. The order every floor and
office writer takes is SFI-1's: operation rows -> work-order row -> audit chain
(``work_orders._lock_laser_import_wipe_operations`` documents it). A naive bulk loop that
audits work order #1 and then flushes UPDATEs to work order #2 would hold the all-tenant
chain lock while it queues on #2's rows -- a lock cycle with any concurrent writer that took
#2's row first and now wants the chain (Postgres breaks it with 40P01). So the bulk door:

1. locks every target row BEFORE its first write (:func:`lock_release_targets`): every
   operation of every live target, ascending id, then the work orders, ascending id --
   ``SELECT ... FOR UPDATE``, tenant-scoped, ``is_deleted`` filtered, ``populate_existing()``
   so the release judges exactly the locked rows;
2. releases each item in ascending id order (two overlapping bulk releases then lock and
   write in the same order), each inside its own SAVEPOINT, writing NO audit row;
3. writes the buffered audit rows only after the loop, one ``log_status_change`` per
   released work order -- so the chain lock is the LAST lock the request takes.

``AuditService`` keeps no chain state in memory (it re-reads the chain tail from the
database under the lock on every row), so a savepoint rolled back mid-loop could not leave
it holding a stale previous hash or sequence even if an audit row had been written inside
one -- and with the audits deferred past the loop, none is.

Events emitted inside a per-item savepoint follow the notification outbox's rule: they are
enqueued only when the OUTERMOST transaction commits, and a rolled-back savepoint drops
exactly the event ids emitted inside it (``services/notification_outbox.py``). A failed item
therefore notifies nobody. Each released work order's ``work_order_released`` event fans out
the ``wo.released`` notification to supervisors, so N releases send N notifications.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Iterable, List, Literal, Optional, Tuple

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Query, Session, joinedload
from sqlalchemy.orm.exc import StaleDataError

from app.core.observability import report_absorbed_exception
from app.db.locks import is_lost_lock_race
from app.db.mixins import STALE_WRITE_CONFLICT_DETAIL
from app.db.tenant_filter import tenant_query
from app.models.user import User
from app.models.work_order import WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.services.audit_service import AuditService
from app.services.completion_cost_service import compute_and_store_estimated_cost
from app.services.completion_signal_service import emit_work_order_lifecycle_event
from app.services.labor_cost_service import is_labor_cost_rollup_enabled
from app.services.work_order_state_service import release_first_ready_operation

logger = logging.getLogger(__name__)

# The single route's texts, verbatim. The bulk door reports the same words per item.
WORK_ORDER_NOT_FOUND_DETAIL = "Work order not found"
NOT_DRAFT_DETAIL = "Only draft work orders can be released"
NO_OPERATIONS_DETAIL = "Work order must have at least one operation"
# A per-item database failure in the bulk door. Never the driver's text: it can carry SQL.
BULK_RELEASE_RETRY_DETAIL = "Work order could not be released. Refresh and try again."

# How many work orders one bulk request may name (before de-duplication).
BULK_RELEASE_MAX = 50

BulkReleaseItemStatus = Literal["released", "error"]


def load_work_order_for_release(db: Session, *, work_order_id: int, company_id: int) -> WorkOrder:
    """The single door's target: the live work order in the active company, or 404.

    Tenant-scoped (invariant 1) and soft-delete filtered (invariant 3): releasing a
    tombstone would put its operations on the floor's board. A foreign, deleted and absent
    id are the same 404.
    """
    work_order = (
        db.query(WorkOrder)
        .options(joinedload(WorkOrder.part))
        .filter(
            WorkOrder.id == work_order_id,
            WorkOrder.company_id == company_id,
            # Invariant 3: a soft-deleted work order is gone -- releasing one would put
            # a tombstone's operations on the floor's board.
            WorkOrder.is_deleted == False,  # noqa: E712
        )
        .first()
    )
    if not work_order:
        raise HTTPException(status_code=404, detail=WORK_ORDER_NOT_FOUND_DETAIL)
    return work_order


def apply_work_order_release(db: Session, *, work_order: WorkOrder, company_id: int, current_user: User) -> str:
    """Release one resolved work order: judge it, stamp it, promote its operations, emit the event.

    Raises ``HTTPException(400)`` -- before any write -- when the work order is not DRAFT or
    has no operation. Otherwise sets RELEASED with ``released_by`` / ``released_at``, runs the
    opt-in estimated-cost rollup (best-effort: an estimate never blocks a release), promotes
    every startable PENDING operation to READY (``release_first_ready_operation``, which emits
    the attributed ``operation_ready`` events) and emits ``work_order_released``
    (best-effort). Writes NO audit row -- :func:`audit_work_order_release` does, and the bulk
    door defers it past every row write. Flushes only through the event emits; never commits.

    Returns the old status value for the audit row.
    """
    if work_order.status != WorkOrderStatus.DRAFT:
        raise HTTPException(status_code=400, detail=NOT_DRAFT_DETAIL)

    # Verify has at least one operation
    if not work_order.operations:
        raise HTTPException(status_code=400, detail=NO_OPERATIONS_DETAIL)

    old_status = work_order.status.value
    work_order.status = WorkOrderStatus.RELEASED
    work_order.released_by = current_user.id
    work_order.released_at = datetime.utcnow()

    # COST-1/COST-5 (Batch 7): when the labor-cost rollup is enabled, populate
    # estimated_cost at release from routing standard hours x shared WC rate + BOM
    # material (best-effort). Gated behind the same OPT-IN flag so a flag-OFF shop sees
    # the pre-Batch-7 behavior (estimated_cost stays at its default). Best-effort: an
    # estimate failure must never block a release.
    if is_labor_cost_rollup_enabled(company_id):
        try:
            compute_and_store_estimated_cost(db, work_order, company_id)
        except Exception:  # pragma: no cover - an estimate must never fail a release
            logger.exception("estimated_cost compute failed on release of WO %s", work_order.id)

    # Lean Phase 1: pass db/user so the PENDING->READY flip emits operation_ready.
    release_first_ready_operation(work_order, db=db, user_id=current_user.id)
    emit_work_order_lifecycle_event(
        db,
        company_id=company_id,
        current_user=current_user,
        work_order=work_order,
        event_type="work_order_released",
        payload={"old_status": old_status, "new_status": WorkOrderStatus.RELEASED.value},
    )
    return old_status


def audit_work_order_release(audit: AuditService, *, work_order: WorkOrder, old_status: str) -> None:
    """The release's tamper-evident STATUS_CHANGE row -- one shape for both doors.

    Best-effort ``log_status_change`` (not ``log_required``), as the single route has always
    written it; a database error that no savepoint contained is still re-raised by
    ``AuditService.log`` and fails the whole request.
    """
    audit.log_status_change(
        resource_type="work_order",
        resource_id=work_order.id,
        resource_identifier=work_order.work_order_number,
        old_status=old_status,
        new_status="released",
    )


def release_work_order(
    db: Session,
    *,
    work_order_id: int,
    company_id: int,
    current_user: User,
    request: Optional[Request] = None,
) -> WorkOrder:
    """``POST /work-orders/{id}/release`` up to (not including) its commit.

    Resolve (404), judge (400), release, then flush and write the audit row in the caller's
    transaction, so the row commits atomically with the status change. The caller commits,
    then schedules and broadcasts.
    """
    work_order = load_work_order_for_release(db, work_order_id=work_order_id, company_id=company_id)
    old_status = apply_work_order_release(db, work_order=work_order, company_id=company_id, current_user=current_user)

    # Audit log for status change. Logged BEFORE the terminal commit so the audit row
    # commits atomically with the status change — AuditService.log() only flushes and the
    # request session never commits on teardown.
    db.flush()
    audit = AuditService(db, current_user, request)
    audit_work_order_release(audit, work_order=work_order, old_status=old_status)
    return work_order


# --------------------------------------------------------------------------------------
# Bulk release
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class BulkReleaseItem:
    """One requested id's outcome. ``work_order_number`` is None exactly when the id was not found."""

    work_order_id: int
    work_order_number: Optional[str]
    status: BulkReleaseItemStatus
    detail: Optional[str] = None


@dataclass
class BulkReleaseOutcome:
    """What the bulk door did, for its caller to commit, schedule and broadcast.

    ``results`` is in the de-duplicated REQUEST order; ``released_work_order_ids`` in the
    ascending order the releases were performed. ``work_center_ids`` and
    ``work_order_ids_by_work_center`` are the released jobs' operations' work centers,
    captured before the commit expires the rows.
    """

    results: List[BulkReleaseItem]
    released_work_order_ids: List[int] = field(default_factory=list)
    work_center_ids: List[int] = field(default_factory=list)
    work_order_ids_by_work_center: Dict[int, List[int]] = field(default_factory=dict)

    @property
    def released_count(self) -> int:
        return sum(1 for item in self.results if item.status == "released")

    @property
    def error_count(self) -> int:
        return sum(1 for item in self.results if item.status == "error")


def dedupe_work_order_ids(work_order_ids: Iterable[int]) -> List[int]:
    """Drop repeated ids, keeping each one's FIRST position."""
    return list(dict.fromkeys(work_order_ids))


def operation_lock_query(db: Session, *, work_order_ids: List[int], company_id: int) -> Query:
    """``SELECT ... FOR UPDATE`` over every operation of the live targets, ascending id.

    The live-target set is a sub-SELECT (tenant, id, ``is_deleted == False``), which Postgres
    does not lock -- only the operations are locked here; the work orders come next, in their
    own statement, so the two tables are locked in SFI-1 order rather than interleaved.
    """
    live_targets = select(WorkOrder.id).where(
        WorkOrder.company_id == company_id,
        WorkOrder.id.in_(work_order_ids),
        WorkOrder.is_deleted == False,  # noqa: E712
    )
    return (
        tenant_query(db, WorkOrderOperation, company_id)
        .filter(WorkOrderOperation.work_order_id.in_(live_targets))
        .order_by(WorkOrderOperation.id.asc())
        .with_for_update()
        .populate_existing()
    )


def work_order_lock_query(db: Session, *, work_order_ids: List[int], company_id: int) -> Query:
    """``SELECT ... FOR UPDATE`` over the live targets themselves, ascending id.

    No eager join (``joinedload(part)`` would be a LEFT OUTER JOIN, and Postgres refuses
    ``FOR UPDATE`` on the nullable side of one -- a laser work order has no part).
    """
    return (
        tenant_query(db, WorkOrder, company_id)
        .filter(
            WorkOrder.id.in_(work_order_ids),
            WorkOrder.is_deleted == False,  # noqa: E712
        )
        .order_by(WorkOrder.id.asc())
        .with_for_update()
        .populate_existing()
    )


def lock_release_targets(db: Session, *, work_order_ids: List[int], company_id: int) -> Dict[int, WorkOrder]:
    """Row-lock every bulk target before the first write: operations, then work orders.

    Returns the locked live work orders by id. An id missing from the result is foreign,
    soft-deleted or absent -- the caller reports all three the same way. Writes nothing.
    """
    if not work_order_ids:
        return {}
    operation_lock_query(db, work_order_ids=work_order_ids, company_id=company_id).all()
    work_orders = work_order_lock_query(db, work_order_ids=work_order_ids, company_id=company_id).all()
    return {work_order.id: work_order for work_order in work_orders}


def _error(work_order_id: int, work_order_number: Optional[str], detail: str) -> BulkReleaseItem:
    return BulkReleaseItem(
        work_order_id=work_order_id, work_order_number=work_order_number, status="error", detail=detail
    )


def bulk_release_work_orders(
    db: Session,
    *,
    work_order_ids: Iterable[int],
    company_id: int,
    current_user: User,
    request: Optional[Request] = None,
) -> BulkReleaseOutcome:
    """``POST /work-orders/bulk-release`` up to (not including) its commit.

    De-duplicates the ids (first occurrence wins), locks every target (see the module
    docstring for the order), then releases each live target in ascending id order inside
    its own SAVEPOINT through :func:`apply_work_order_release` -- the single door's rule,
    texts and side effects. A per-item refusal (not DRAFT, no operation) or database failure
    rolls back THAT item only and is reported on its result; the others proceed. Only after
    the loop are the audit rows written, one per released work order, in the caller's
    transaction. The caller commits once.

    A foreign, soft-deleted and absent id are one result, ``"Work order not found"`` with no
    number -- no existence oracle. A database failure reports a fixed retry text, never the
    driver's message.
    """
    ordered_ids = dedupe_work_order_ids(work_order_ids)
    locked = lock_release_targets(db, work_order_ids=ordered_ids, company_id=company_id)

    by_id: Dict[int, BulkReleaseItem] = {}
    released: List[Tuple[WorkOrder, str]] = []

    for work_order_id in sorted(ordered_ids):
        work_order = locked.get(work_order_id)
        if work_order is None:
            by_id[work_order_id] = _error(work_order_id, None, WORK_ORDER_NOT_FOUND_DETAIL)
            continue
        # Read before the savepoint: a rolled-back savepoint expires what it modified.
        work_order_number = work_order.work_order_number
        try:
            with db.begin_nested():
                old_status = apply_work_order_release(
                    db, work_order=work_order, company_id=company_id, current_user=current_user
                )
                # Send this item's writes inside its own savepoint, so a version conflict or a
                # database refusal rolls back this item and nothing else.
                db.flush()
        except HTTPException as exc:
            by_id[work_order_id] = _error(work_order_id, work_order_number, str(exc.detail))
            continue
        except StaleDataError:
            logger.info("Bulk release: work order %s changed underneath the release; skipped", work_order_id)
            by_id[work_order_id] = _error(work_order_id, work_order_number, STALE_WRITE_CONFLICT_DETAIL)
            continue
        except SQLAlchemyError as exc:
            if is_lost_lock_race(exc):
                logger.warning("Bulk release: work order %s lost a lock race; skipped", work_order_id)
            else:
                report_absorbed_exception(
                    logger,
                    "Bulk release: one work order's release failed at the database; the rest proceed",
                    exc,
                    context={"work_order_id": work_order_id, "company_id": company_id},
                )
            by_id[work_order_id] = _error(work_order_id, work_order_number, BULK_RELEASE_RETRY_DETAIL)
            continue
        released.append((work_order, old_status))
        by_id[work_order_id] = BulkReleaseItem(
            work_order_id=work_order_id, work_order_number=work_order_number, status="released"
        )

    # Every row lock is held and every row write is done: only now take the global audit
    # chain lock, one STATUS_CHANGE row per released work order, identical to the single door's.
    work_order_ids_by_work_center: Dict[int, List[int]] = {}
    if released:
        db.flush()
        audit = AuditService(db, current_user, request)
        for work_order, old_status in released:
            audit_work_order_release(audit, work_order=work_order, old_status=old_status)
            for operation in work_order.operations or []:
                if operation.work_center_id:
                    ids_here = work_order_ids_by_work_center.setdefault(operation.work_center_id, [])
                    if work_order.id not in ids_here:
                        ids_here.append(work_order.id)

    return BulkReleaseOutcome(
        results=[by_id[work_order_id] for work_order_id in ordered_ids],
        released_work_order_ids=[work_order.id for work_order, _old in released],
        work_center_ids=sorted(work_order_ids_by_work_center),
        work_order_ids_by_work_center=work_order_ids_by_work_center,
    )
