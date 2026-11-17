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
outer transaction ends; a per-item savepoint does not release it. A bulk loop that audited
work order #1 and then wrote #2 would hold that all-tenant lock while it queued on #2's rows.
So the bulk door:

1. reads the live targets once, column-only and UNLOCKED (:func:`live_work_order_numbers`:
   id and number, tenant-scoped, ``is_deleted`` filtered), so a foreign, deleted or absent id
   is answered without locking anything;
2. releases each live target in ascending work-order id order, each inside its OWN
   SAVEPOINT, which takes that item's row locks first (:func:`lock_release_target`): its
   operations, ascending id, then the work order -- ``SELECT ... FOR NO KEY UPDATE``,
   tenant-scoped, ``is_deleted`` filtered, ``populate_existing()`` so the release judges
   exactly the locked rows -- and then writes it, with NO audit row. A released item's
   savepoint is released into the outer transaction, so its row locks are held to the commit;
3. writes the buffered audit rows only after the loop, one ``log_status_change`` per
   released work order -- so the audit chain is the LAST lock the request takes.

Per item, then: operations -> work order; per request: every item, ascending -> audit chain.
Operations before the work order is the floor's SFI-1 order (completion, production report,
clock-out, reduce-production all lock the operation, then the work order), so an SFI-1 verb
and a bulk item queue rather than cross; and two bulk requests lock in the same ascending
order, so they cannot cross either.

That order does NOT rule out every cycle. Some office writers take the WORK ORDER first: the
single release route (it locks nothing explicitly; its first flush writes the work order's
``UPDATE`` before its operations', parent before child), ``PUT /work-orders/{id}``,
force-complete and the scheduling routes (``work_orders._lock_laser_import_rows`` lists
them). One of those can meet a bulk item holding the job's operations and waiting for its work
order while the writer holds the work order and waits for an operation. Postgres breaks that
cycle by rolling one side back (40P01). When the writer loses, it answers its own 409. When the
bulk item loses, the error is raised inside the item's savepoint: ROLLBACK TO SAVEPOINT undoes
the item, releases the row locks it took (the writer goes on at once) and leaves the outer
transaction usable, so THAT item alone fails with the lost-race retry text
(:data:`~app.db.mixins.STALE_WRITE_CONFLICT_DETAIL`, the 409 text the single route answers for
the same race) and every other item, before or after it, is released and audited as usual.
Retrying the item is safe: nothing of it was saved. Earlier items' locks stay held to the
commit, so a writer queued on one of them simply waits; it is no longer in a cycle once the
losing item has stopped waiting.

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

from app.core.observability import capture_exception_to_sentry, report_absorbed_exception
from app.db.locks import is_lost_lock_race
from app.db.mixins import STALE_WRITE_CONFLICT_DETAIL
from app.db.tenant_filter import tenant_filter, tenant_query
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
# A per-item database failure in the bulk door other than a lost lock race or version conflict
# (those answer ``STALE_WRITE_CONFLICT_DETAIL``, the single route's 409 text for the same race).
# Never the driver's text: it can carry SQL.
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


def live_work_order_numbers(db: Session, *, work_order_ids: List[int], company_id: int) -> Dict[int, str]:
    """The live targets' numbers by id: tenant-scoped, ``is_deleted`` filtered, column-only, UNLOCKED.

    An id missing from the result is foreign, soft-deleted or absent -- the caller reports all
    three the same way, without locking anything for them. Loads no entity (so it refreshes
    nothing in the session) and takes no lock: each item locks its own rows later, inside its
    own savepoint (:func:`lock_release_target`), which re-checks ``is_deleted`` under the lock.
    """
    if not work_order_ids:
        return {}
    rows = (
        tenant_filter(db.query(WorkOrder.id, WorkOrder.work_order_number), WorkOrder, company_id)
        .filter(
            WorkOrder.id.in_(work_order_ids),
            WorkOrder.is_deleted == False,  # noqa: E712
        )
        .all()
    )
    return {row.id: row.work_order_number for row in rows}


# Lock strength, both statements: FOR NO KEY UPDATE (``with_for_update(key_share=True)``), the
# strength the release's own UPDATEs take. A release changes no key column of either row (a work
# order's status, released_by / released_at, estimated cost and version; an operation's status),
# so this is no stronger than the writes it guards and is never upgraded at the flush. It still
# conflicts with every other row lock and every UPDATE or DELETE, so nobody can change the rows
# the release judged before it commits. What it does NOT conflict with is the FOR KEY SHARE a
# concurrent INSERT's foreign-key check takes (a time entry, event or blocker naming this job or
# operation): a plain FOR UPDATE would queue behind those, a wait -- and a cycle -- the release
# has no use for. It is the house choice for a write that changes no key: the package import's
# work-order lock (``work_orders._try_lock_laser_work_order`` / ``_check_laser_header_before_audit``)
# is NO KEY UPDATE for the same reason, and that import locks its OPERATIONS FOR UPDATE only
# because it deletes them.


def operation_lock_query(db: Session, *, work_order_id: int, company_id: int) -> Query:
    """``SELECT ... FOR NO KEY UPDATE`` over one live target's operations, ascending id.

    The live-target check is a sub-SELECT (tenant, id, ``is_deleted == False``), which Postgres
    does not lock -- only the operations are locked here (a work order deleted since
    :func:`live_work_order_numbers` read it locks nothing); the work order comes next, in its own
    statement, so the two tables are locked in SFI-1 order rather than interleaved.
    """
    live_target = select(WorkOrder.id).where(
        WorkOrder.company_id == company_id,
        WorkOrder.id == work_order_id,
        WorkOrder.is_deleted == False,  # noqa: E712
    )
    return (
        tenant_query(db, WorkOrderOperation, company_id)
        .filter(WorkOrderOperation.work_order_id.in_(live_target))
        .order_by(WorkOrderOperation.id.asc())
        .with_for_update(key_share=True)
        .populate_existing()
    )


def work_order_lock_query(db: Session, *, work_order_id: int, company_id: int) -> Query:
    """``SELECT ... FOR NO KEY UPDATE`` over one live target itself.

    No eager join (``joinedload(part)`` would be a LEFT OUTER JOIN, and Postgres refuses a
    row lock on the nullable side of one -- a laser work order has no part).
    """
    return (
        tenant_query(db, WorkOrder, company_id)
        .filter(
            WorkOrder.id == work_order_id,
            WorkOrder.is_deleted == False,  # noqa: E712
        )
        .with_for_update(key_share=True)
        .populate_existing()
    )


def lock_release_target(db: Session, *, work_order_id: int, company_id: int) -> Optional[WorkOrder]:
    """Row-lock ONE bulk target before its first write: its operations, then the work order.

    Call it inside the item's own savepoint (``db.begin_nested()``), never before the loop: a
    lost lock race raised here must roll back with that item alone (see the module docstring).
    Returns the locked live work order, or None when it was deleted since
    :func:`live_work_order_numbers` read it. Writes nothing.
    """
    operation_lock_query(db, work_order_id=work_order_id, company_id=company_id).all()
    return work_order_lock_query(db, work_order_id=work_order_id, company_id=company_id).one_or_none()


class _ReleaseTargetGone(Exception):
    """Internal to :func:`bulk_release_work_orders`: the target was soft-deleted before its lock."""


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

    De-duplicates the ids (first occurrence wins), resolves which are live, then releases each
    live target in ascending id order inside its own SAVEPOINT, which first locks that item's
    rows (see the module docstring for the order) and then applies
    :func:`apply_work_order_release` -- the single door's rule, texts and side effects. A
    per-item refusal (not DRAFT, no operation), lost lock race or other database failure rolls
    back THAT item only, with its row locks, and is reported on its result; the others proceed.
    Only after the loop are the audit rows written, one per released work order, in the
    caller's transaction. The caller commits once.

    A foreign, soft-deleted and absent id are one result, ``"Work order not found"`` with no
    number -- no existence oracle. A database failure reports a fixed retry text, never the
    driver's message: a lost lock race or version conflict the stale-write 409 text, anything
    else :data:`BULK_RELEASE_RETRY_DETAIL`.
    """
    ordered_ids = dedupe_work_order_ids(work_order_ids)
    live_numbers = live_work_order_numbers(db, work_order_ids=ordered_ids, company_id=company_id)

    by_id: Dict[int, BulkReleaseItem] = {}
    released: List[Tuple[WorkOrder, str]] = []

    for work_order_id in sorted(ordered_ids):
        if work_order_id not in live_numbers:
            by_id[work_order_id] = _error(work_order_id, None, WORK_ORDER_NOT_FOUND_DETAIL)
            continue
        work_order_number = live_numbers[work_order_id]
        try:
            with db.begin_nested():
                # This item's row locks, inside its savepoint: a lost lock race (40P01 / 40001)
                # raised by either statement rolls back to the savepoint -- which releases the
                # locks it took and leaves the outer transaction usable -- and fails this item only.
                work_order = lock_release_target(db, work_order_id=work_order_id, company_id=company_id)
                if work_order is None:
                    raise _ReleaseTargetGone()
                old_status = apply_work_order_release(
                    db, work_order=work_order, company_id=company_id, current_user=current_user
                )
                # Send this item's writes inside its own savepoint, so a version conflict or a
                # database refusal rolls back this item and nothing else.
                db.flush()
        except _ReleaseTargetGone:
            by_id[work_order_id] = _error(work_order_id, None, WORK_ORDER_NOT_FOUND_DETAIL)
            continue
        except HTTPException as exc:
            by_id[work_order_id] = _error(work_order_id, work_order_number, str(exc.detail))
            continue
        except StaleDataError:
            logger.info("Bulk release: work order %s changed underneath the release; skipped", work_order_id)
            by_id[work_order_id] = _error(work_order_id, work_order_number, STALE_WRITE_CONFLICT_DETAIL)
            continue
        except SQLAlchemyError as exc:
            if is_lost_lock_race(exc):
                # The same race answers the single route's 409 (``app/main.py``), with the same
                # text -- and, like there, it is reported: a lost race is a lock-order symptom.
                pgcode = getattr(getattr(exc, "orig", None), "pgcode", None)
                logger.warning(
                    "Bulk release: work order %s lost a lock race (SQLSTATE %s); skipped", work_order_id, pgcode
                )
                capture_exception_to_sentry(
                    exc,
                    context={
                        "lost_lock_race": True,
                        "pgcode": str(pgcode),
                        "work_order_id": work_order_id,
                        "company_id": company_id,
                    },
                    fingerprint=["lost-lock-race", "bulk-release-item", str(pgcode)],
                )
                detail = STALE_WRITE_CONFLICT_DETAIL
            else:
                report_absorbed_exception(
                    logger,
                    "Bulk release: one work order's release failed at the database; the rest proceed",
                    exc,
                    context={"work_order_id": work_order_id, "company_id": company_id},
                )
                detail = BULK_RELEASE_RETRY_DETAIL
            by_id[work_order_id] = _error(work_order_id, work_order_number, detail)
            continue
        released.append((work_order, old_status))
        by_id[work_order_id] = BulkReleaseItem(
            work_order_id=work_order_id, work_order_number=work_order_number, status="released"
        )

    # Every released item's row locks are held and every row write is done: only now take the
    # global audit chain lock, one STATUS_CHANGE row per released work order, identical to the
    # single door's.
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
