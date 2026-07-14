import hashlib
import json
import logging
import math
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, Request, Response, UploadFile
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import and_, func, or_
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, joinedload, selectinload
from sqlalchemy.orm.exc import StaleDataError

from app.api.deps import (
    KioskReadPrincipal,
    WallboardPrincipal,
    get_audit_service,
    get_current_company_id,
    get_current_user,
    get_display_or_user,
    get_kiosk_or_user,
    require_role,
)
from app.core.cache import invalidate_work_centers_cache
from app.core.config import settings
from app.core.realtime import safe_broadcast
from app.core.time_utils import CENTRAL_TIME_ZONE, to_utc_iso
from app.core.websocket import (
    broadcast_dashboard_update,
    broadcast_shop_floor_update,
    broadcast_work_order_update,
    manager,
)
from app.db.database import get_db
from app.db.tenant_filter import tenant_query
from app.models.audit_log import AuditLog
from app.models.laser_nest import LaserNest
from app.models.scrap_reason import ScrapReasonCode
from app.models.time_entry import TimeEntry, TimeEntrySource, TimeEntryType
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.models.work_order_blocker import (
    WorkOrderBlocker,
    WorkOrderBlockerCategory,
    WorkOrderBlockerSeverity,
    WorkOrderBlockerStatus,
)
from app.schemas.kiosk_station import (
    KioskStationCreate,
    KioskStationInfo,
    KioskStationListResponse,
    KioskStationLoginRequest,
    KioskStationLoginResponse,
    KioskStationResetPinRequest,
    KioskStationResponse,
)
from app.schemas.process_sheet import (
    OperationStepRecordCreate,
    OperationStepRecordResponse,
    OperationStepRecordSupersede,
    OperationStepsViewResponse,
    QualityHoldRequest,
    QualityHoldResponse,
    StepAttachmentResponse,
)
from app.schemas.time_entry import ClockIn, ClockOut, TimeEntryResponse
from app.schemas.wallboard import WallboardResponse
from app.schemas.work_order_blocker import WorkOrderBlockerCreate
from app.services import kiosk_station_service, process_sheet_service
from app.services.audit_service import AuditService
from app.services.completion_cost_service import (
    apply_completion_cost_rollup,
    rollup_labor_hours_for_closed_entries,
    rollup_labor_hours_from_evidence,
)
from app.services.completion_inventory_service import apply_completion_inventory_effects
from app.services.completion_quality_service import (
    evaluate_and_record_labor_data_quality,
    record_reconcile_labor_data_quality,
)
from app.services.completion_signal_service import (
    emit_operation_completed_event,
    emit_work_order_completed_event,
    enqueue_work_order_completion_signals,
    record_parent_children_complete,
)
from app.services.labor_cost_service import is_labor_cost_rollup_enabled
from app.services.laser_nest_service import active_laser_nest, sync_laser_nest_from_operation
from app.services.operation_action_gates import (
    CLOCK_IN_ALLOWED_STATUSES,
    MSG_WRONG_WORK_CENTER,
    get_open_time_entry,
    operation_blocked_by_predecessors,
)
from app.services.operational_event_service import OperationalEventService
from app.services.operator_qualification_service import evaluate_and_record_operator_qualification
from app.services.quality_gate_service import (
    QualityException,
    evaluate_and_record_completion_quality_exceptions,
    record_reconcile_inspection_exception,
)
from app.services.scheduling_service import SchedulingService
from app.services.scrap_reason_service import resolve_scrap_reason_code_or_http
from app.services.wallboard_service import (
    LABOR_ENTRY_TYPES,
    build_wallboard_payload,
    operation_counts_by_work_center,
    operator_display_name,
)
from app.services.work_order_blocker_service import WorkOrderBlockerService
from app.services.work_order_state_service import (
    TERMINAL_WO_STATUSES,
    StatusTransition,
    WorkOrderStateError,
    begin_operation_progress,
    finalize_operation_completion,
    find_parent_to_advance,
    floor_operation_quantity_at_evidence,
    has_incomplete_predecessors,
    operation_target_quantity,
    reconcile_work_orders_from_completion_evidence,
    resolve_absolute_operation_quantity,
    sync_work_order_quantity_complete,
    validate_operation_quantity,
)


class OperationCompleteRequest(BaseModel):
    quantity_complete: float
    notes: Optional[str] = None
    # A0.1 adoption telemetry: client channel (kiosk/desktop/scanner/import/backfill).
    # Optional -- omitted means unknown; unknown values are rejected with a 422.
    source: Optional[TimeEntrySource] = Field(
        None,
        description="Adoption-telemetry channel of this completion (kiosk | desktop | scanner | "
        "backfill). Also fills the channel on auto-closed open entries that have none; never overwrites "
        "a recorded channel. Omit when unknown. 'import' is rejected (422) here (reserved for the "
        "bulk-migration loaders); a kiosk-scoped operator token forces 'kiosk' regardless of this hint.",
    )


class ProductionReportRequest(BaseModel):
    quantity_complete_delta: float = 0.0
    quantity_scrapped_delta: float = 0.0
    notes: Optional[str] = None
    # A0.3: structured scrap reason, same shape as ClockOut.scrap_reason (the
    # TimeEntry.scrap_reason column is String(255), hence the max_length).
    scrap_reason: Optional[str] = Field(
        None,
        max_length=255,
        description="Reason for scrapped parts; stored only when quantity_scrapped_delta > 0 "
        "and never cleared by a later reason-less report.",
    )
    # Lean Phase 1: structured scrap categorization (validated: exists, active,
    # belongs to the company). Either the code or free text satisfies the
    # scrap-requires-a-reason rule; the code is preferred, text stays narrative.
    scrap_reason_code_id: Optional[int] = Field(
        None,
        description="Id of a predefined scrap reason code (see /quality/scrap-reason-codes). "
        "Applied only when quantity_scrapped_delta > 0; never cleared by a code-less report.",
    )
    # A0.1 adoption telemetry: client channel (kiosk/desktop/scanner/import/backfill).
    source: Optional[TimeEntrySource] = Field(
        None,
        description="Adoption-telemetry channel of this production report (kiosk | desktop | scanner | "
        "backfill). Omit to keep the active entry's existing channel. 'import' is rejected (422) here "
        "(reserved for the bulk-migration loaders); a kiosk-scoped operator token forces 'kiosk' "
        "regardless of this hint.",
    )

    @model_validator(mode="after")
    def _require_scrap_reason(self) -> "ProductionReportRequest":
        # AS9100D defect-traceability invariant (same rule as ClockOut): a scrap delta MUST
        # carry a reason. Enforced at the data boundary so a scripted/API client can't post
        # reasonless scrap that the UIs already block. Lean Phase 1: EITHER a structured
        # scrap_reason_code_id OR non-blank free text satisfies the rule (old text-only
        # clients keep working). Blank/whitespace counts as missing; raised as a Pydantic
        # ValueError -> 422. A zero scrap delta with no reason stays valid; negatives/NaN
        # fall through to the handler's existing numeric guards.
        has_reason = (self.scrap_reason and self.scrap_reason.strip()) or self.scrap_reason_code_id is not None
        if (self.quantity_scrapped_delta or 0) > 0 and not has_reason:
            raise ValueError(
                "scrap_reason or scrap_reason_code_id is required when quantity_scrapped_delta is greater than 0"
            )
        return self


class OperationHoldRequest(BaseModel):
    category: WorkOrderBlockerCategory = WorkOrderBlockerCategory.OTHER
    severity: WorkOrderBlockerSeverity = WorkOrderBlockerSeverity.MEDIUM
    note: Optional[str] = None
    # A0.1 adoption telemetry: client channel (kiosk/desktop/scanner/import/backfill).
    source: Optional[TimeEntrySource] = Field(
        None,
        description="Adoption-telemetry channel of this hold (kiosk | desktop | scanner | backfill). "
        "Also fills the channel on the open entries the hold auto-closes when they have none; never "
        "overwrites a recorded channel. Omit when unknown. 'import' is rejected (422) here (reserved "
        "for the bulk-migration loaders); a kiosk-scoped operator token forces 'kiosk' regardless of "
        "this hint.",
    )


class OperationInspectionRequest(BaseModel):
    """Record an operation as inspected (QG-2 writer).

    ``inspection_type`` optionally records WHICH inspection cleared the gate
    (first_article / in_process / final); ``notes`` carries inspector commentary.
    Both land in the audit row so the inspection sign-off is traceable.
    """

    inspection_type: Optional[str] = Field(default=None, max_length=100)
    notes: Optional[str] = Field(default=None, max_length=2000)


router = APIRouter()

logger = logging.getLogger(__name__)


def _emit_reconcile_events(
    db: Session,
    company_id: int,
    current_user: User,
    transitions: list[StatusTransition],
) -> None:
    """Emit the in-process completion OperationalEvent for each reconcile transition.

    EVT-4: reconcile-on-read materializes operation/WO completions from durable
    TimeEntry evidence. Those transitions must produce the SAME in-process signal as
    the live completion paths -- ``operation_completed`` / ``work_order_completed`` --
    so AI/realtime consumers aren't blind to reconcile-driven completions. This is
    intentionally IN-PROCESS ONLY: we do NOT fire outbound notifications/webhooks from
    a GET/reconcile path (a read must not have outbound side-effects; rank 12 will move
    reconcile to a debounced ARQ job, at which point the outbound dispatch can move with
    it). Best-effort and tenant-scoped (``emit`` validates the WO/op belong to
    ``company_id``); a signal failure must never 500 a read, so this is wrapped.
    """
    if not transitions:
        return
    try:
        event_service = OperationalEventService(db)
        for tr in transitions:
            event_type = "operation_completed" if tr.resource_type == "work_order_operation" else "work_order_completed"
            try:
                event_service.emit(
                    company_id=company_id,
                    event_type=event_type,
                    source_module="reconcile_on_read",
                    entity_type=tr.resource_type,
                    entity_id=tr.resource_id,
                    work_order_id=tr.work_order_id,
                    operation_id=tr.resource_id if tr.resource_type == "work_order_operation" else None,
                    user_id=current_user.id,
                    severity="info",
                    event_payload={
                        "work_order_number": tr.work_order_number,
                        "source": "reconcile_on_read",
                        "time_entry_ids": tr.time_entry_ids,
                    },
                )
            except ValueError:
                # emit() raises ValueError if the WO/op isn't in this company; a
                # reconcile transition for another tenant must be skipped, not 500.
                continue
        # G1 ADVANCE on the reconcile path: for any WO this reconcile drove to COMPLETE
        # that is a laser child, surface a signal on its parent iff every laser child is
        # now terminal. Attributed to the requesting user, source="reconcile_on_read".
        # FULLY best-effort: a parent-advance failure must never 500 a GET. Same
        # no-double-fire reasoning as the live paths -- all-children-terminal becomes
        # true exactly once, and a terminal child is never re-flipped on read.
        _emit_reconcile_parent_advance(db, company_id, current_user, transitions)
    except Exception:  # pragma: no cover - reads must never 500 on event-emit failure
        pass


def _emit_reconcile_parent_advance(
    db: Session,
    company_id: int,
    current_user: User,
    transitions: list[StatusTransition],
) -> None:
    """Record the G1 parent-children-complete signal for reconcile-driven WO completions.

    Mirrors the live-completion-path advance, on the read/reconcile path. For each
    ``work_order`` -> COMPLETE transition, load the WO (company-scoped, not
    soft-deleted) and, if it has a parent whose last laser child just completed, leave
    the tamper-evident audit row + ``child_work_orders_complete`` event. Best-effort:
    the whole block is wrapped so it can never 500 a GET, joined to this read's unit of
    work (the caller commits) and tenant-scoped via ``company_id``.
    """
    completed_wo_ids = {tr.resource_id for tr in transitions if tr.resource_type == "work_order"}
    if not completed_wo_ids:
        return
    try:
        audit = AuditService(db, current_user)
        completed_work_orders = (
            db.query(WorkOrder)
            .filter(
                WorkOrder.id.in_(completed_wo_ids),
                WorkOrder.company_id == company_id,
                WorkOrder.is_deleted == False,  # noqa: E712
                WorkOrder.parent_work_order_id.isnot(None),
            )
            .all()
        )
        for child in completed_work_orders:
            parent = find_parent_to_advance(db, child, company_id)
            if parent is not None:
                record_parent_children_complete(
                    db,
                    parent_work_order=parent,
                    child_work_order=child,
                    company_id=company_id,
                    user_id=current_user.id,
                    audit=audit,
                    source="reconcile_on_read",
                )
    except Exception:  # pragma: no cover - reads must never 500 on parent-advance failure
        pass


def _refresh_reconcile_scheduling(db: Session, company_id: int, transitions: list[StatusTransition]) -> None:
    """Refresh cached work-center availability for reconcile-driven WO completions (MS-2).

    A reconcile-on-read WO -> COMPLETE drops its ops out of the scheduled-load query,
    so the persisted ``work_center.availability_rate`` would otherwise stay understated.
    ``StatusTransition.work_center_ids`` carries the affected WCs for each WO transition.
    Tenant-scoped (``SchedulingService(db, company_id)``); ``commit=False`` so the
    refresh joins THIS read's unit of work and is committed/rolled back atomically by
    the caller (it must not commit independently on a read path). Best-effort: a
    scheduling-refresh failure must never 500 a GET.
    """
    work_center_ids = sorted({wc for tr in transitions for wc in tr.work_center_ids if wc})
    if not work_center_ids:
        return
    try:
        SchedulingService(db, company_id).update_availability_rates(
            work_center_ids=work_center_ids, horizon_days=90, commit=False
        )
    except Exception:  # pragma: no cover - reads must never 500 on scheduling refresh
        pass


def _apply_reconcile_inventory_effects(
    db: Session,
    company_id: int,
    current_user: User,
    work_orders: list[WorkOrder],
    transitions: list[StatusTransition],
) -> None:
    """FG receipt + gated backflush for reconcile-driven WO completions (Batch 6 / rank 9).

    A reconcile-on-read WO -> COMPLETE must move inventory the SAME way the live
    completion paths do (INV-1/INV-2), otherwise a WO that completes implicitly on a
    GET would never produce finished-good stock or backflush components. This is
    deliberately READ-SAFE / best-effort: the whole block is wrapped so an inventory
    or audit-write failure can never 500 a dashboard/list/detail GET, and the writes
    are IDEMPOTENT (a prior WO RECEIVE / component ISSUE short-circuits them) so a
    later live completion or another read can't double-receive / double-issue. Joined
    to THIS read's unit of work (the caller commits) and tenant-scoped via
    ``company_id``. (rank 12 will move reconcile off the read path; the same audit/
    attribution caveat as the events applies -- attributed here to the requesting
    user.)
    """
    completed_wo_ids = {tr.resource_id for tr in transitions if tr.resource_type == "work_order"}
    if not completed_wo_ids:
        return
    try:
        audit = AuditService(db, current_user)
        for work_order in work_orders:
            if work_order.id in completed_wo_ids:
                # The returned BackflushResult is intentionally not inspected here: a
                # backflush shortage is now recorded tamper-evidently INSIDE the service
                # (a BACKFLUSH_SHORTAGE audit_log row + a backflush_shortage
                # OperationalEvent), so it is captured on this read path too -- atomic
                # with the reconcile's unit of work and inside this read-safe guard.
                apply_completion_inventory_effects(
                    db, work_order, user_id=current_user.id, company_id=company_id, audit=audit
                )
    except Exception:  # pragma: no cover - reads must never 500 on inventory-effect failure
        pass


def _apply_reconcile_cost_rollup(
    db: Session,
    company_id: int,
    current_user: User,
    work_orders: list[WorkOrder],
    transitions: list[StatusTransition],
) -> None:
    """Labor hour + cost + JobCost rollup for reconcile-driven WO completions (Batch 7).

    COST-4: a WO that completes implicitly on a GET (reconcile-on-read) must roll labor
    hours/cost the SAME way the live completion paths do. ALL of the Batch-7 rollup -- the
    evidence-sourced HOUR rollup AND the cost/JobCost rollup -- is gated behind
    ``LABOR_COST_ROLLUP_ENABLED`` so the OPT-IN flag governs cost/hours surfacing
    consistently across the live and reconcile paths: flag-OFF, a reconcile completion
    surfaces NO computed Batch-7 hours/cost (the live paths also gate their hour rollup);
    flag-ON, both roll up identically and monotonic-up. (The pre-existing clock_out hour
    accumulation is a separate mechanism and is unaffected.) READ-SAFE: the whole block is
    wrapped so an error can never 500 a GET; joined to this read's unit of work (the caller
    commits) and tenant-scoped. (rank 12 will move reconcile off the read path;
    re-attribute to a system actor then -- same caveat as the events.)
    """
    completed_wo_ids = {tr.resource_id for tr in transitions if tr.resource_type == "work_order"}
    if not completed_wo_ids:
        return
    rollup_enabled = is_labor_cost_rollup_enabled(company_id)
    try:
        audit = AuditService(db, current_user)
        for work_order in work_orders:
            if work_order.id in completed_wo_ids:
                # The Batch-7 hour rollup is now flag-gated on the reconcile path too (it
                # was previously unconditional). apply_completion_cost_rollup is itself a
                # no-op when the flag is OFF, but we hoist the same guard so the NEW hour
                # rollup never runs flag-OFF either -- keeping cost/hours surfacing
                # consistent between the live and reconcile paths.
                if rollup_enabled:
                    rollup_labor_hours_from_evidence(db, work_order)
                    apply_completion_cost_rollup(
                        db, work_order, company_id=company_id, user_id=current_user.id, audit=audit
                    )
                # no_labor_recorded data-quality signal on the read path (read-safe,
                # flag-independent).
                record_reconcile_labor_data_quality(
                    db, work_order=work_order, company_id=company_id, audit=audit, user=current_user
                )
    except Exception:  # pragma: no cover - reads must never 500 on cost-rollup failure
        pass


def _audit_reconcile_transitions(
    db: Session,
    current_user: User,
    transitions: list[StatusTransition],
) -> None:
    """Emit a tamper-evident status-change audit row per reconcile-driven transition.

    AUD-3: reconcile-on-read drives operations/WOs to COMPLETE from durable
    TimeEntry evidence; those transitions were previously unaudited and could not
    even be attributed (the reconcile has no actor). We now thread the requesting
    user in and write one ``log_status_change`` per transition, attributed to that
    user, with the contributing TimeEntry ids in ``extra_data``. ``AuditService.log``
    already swallows its own failures and only flushes (never commits), so a single
    bad audit write cannot 500 the read; this whole block is additionally wrapped so
    the GET stays resilient even on an unexpected error.
    """
    if not transitions:
        return
    try:
        audit = AuditService(db, current_user)
        for tr in transitions:
            audit.log_status_change(
                resource_type=tr.resource_type,
                resource_id=tr.resource_id,
                resource_identifier=tr.resource_identifier or str(tr.resource_id),
                old_status=tr.old_status or "",
                new_status=tr.new_status,
                description=(
                    f"Reconciled {tr.resource_type} "
                    f"{tr.resource_identifier or tr.resource_id} to {tr.new_status} "
                    "from durable completion evidence"
                ),
                extra_data={
                    "source": "reconcile_on_read",
                    "work_order_number": tr.work_order_number,
                    "time_entry_ids": tr.time_entry_ids,
                },
            )
            # QG-4: a completion can happen on a GET via reconcile. The same
            # warn-and-record gate must flag it. To keep the read path cheap and
            # 500-safe we record at MINIMUM the inspection_incomplete exception
            # (no extra query -- the operation row is already loaded in-session).
            # PARTIAL COVERAGE by design: the NCR/FAI/open-blocker gates (which
            # need extra queries) are NOT evaluated on the read path; they are
            # caught on the next live completion / WO-complete. Flagged in the
            # report and the module docstring.
            if tr.resource_type == "work_order_operation":
                record_reconcile_inspection_exception(db, operation_id=tr.resource_id, audit=audit, user=current_user)
    except Exception:  # pragma: no cover - reads must never 500 on audit failure
        # Reads must still succeed even if the audit chain write fails. The
        # status mutation itself is already committed by the caller; losing the
        # audit row degrades traceability but must not break the GET.
        pass


def _reconcile_and_commit(db: Session, work_orders: list[WorkOrder], current_user: User, company_id: int) -> None:
    """Reconcile operation rows from completion evidence and commit, tolerating
    ANY failure of that best-effort write on a READ/dashboard path.

    ``reconcile_work_orders_from_completion_evidence`` mutates version-mapped
    operation rows; committing that mutation can raise ``StaleDataError`` when
    another transaction bumped the same rows' version first. On a read that
    conflict is BENIGN -- the reconcile is idempotent and the other writer
    already persisted the truth -- so we roll the reconcile back (NOT a 409) and
    serve the read against the freshest committed state.

    Reconcile-on-read is a best-effort optimization, so this intentionally
    swallows ALL of its own commit failures, not just the version race. AUD-3:
    the audit INSERT can itself fail (e.g. an ``audit_log.sequence_number``
    unique collision under concurrency); ``AuditService.log`` absorbs that
    without rolling back, which POISONS the session, so the subsequent
    ``db.commit()`` here raises ``PendingRollbackError`` / ``InvalidRequestError``
    / ``IntegrityError`` rather than ``StaleDataError``. We catch ``SQLAlchemyError``
    broadly, roll back, expire, and serve the read normally so a poisoned session
    can never turn a GET into a 500. Because the reconcile mutation and its audit
    rows share one unit of work, the rollback drops BOTH atomically -- no orphaned
    state change, no unaudited transition -- and the next read retries.

    (The root ``sequence_number`` race is a separately-tracked follow-up; this
    guard only guarantees reads never 500.)
    """
    transitions: list[StatusTransition] = []
    try:
        if reconcile_work_orders_from_completion_evidence(db, work_orders, transitions):
            _audit_reconcile_transitions(db, current_user, transitions)
            # EVT-4: emit the in-process completion events for the materialized
            # transitions (NO outbound notify/webhook on a read -- see helper).
            _emit_reconcile_events(db, company_id, current_user, transitions)
            # MS-2: refresh cached work-center availability for reconcile-driven WO
            # completions, joined to this read's unit of work (commit=False).
            _refresh_reconcile_scheduling(db, company_id, transitions)
            # Batch 6 / rank 9 (INV-1/INV-2): FG receipt + gated backflush for any WO
            # this reconcile drove to COMPLETE, joined to this read's unit of work.
            # Read-safe (best-effort) + idempotent -- see helper.
            _apply_reconcile_inventory_effects(db, company_id, current_user, work_orders, transitions)
            # Batch 7 / rank 10 (COST-4): labor hour rollup (monotonic-up, always) +
            # OPT-IN cost/JobCost rollup + no_labor_recorded signal for reconcile-driven
            # completions. Read-safe + idempotent -- see helper.
            _apply_reconcile_cost_rollup(db, company_id, current_user, work_orders, transitions)
            db.commit()
            # PERF-5: _refresh_reconcile_scheduling ran with commit=False (joined to
            # this read's unit of work), so it SKIPPED the in-service WC cache
            # invalidation -- without this the cache would serve a stale
            # availability_rate after a reconcile-driven WO completion. Invalidate
            # only when scheduling was actually refreshed (a WO->COMPLETE transition
            # carried a non-falsy work_center_id) and only on the post-commit success
            # path (never in the rollback branch). This matches _refresh_reconcile_scheduling's
            # own refresh condition exactly. A cache invalidate cannot 500 a read.
            if any(wc for tr in transitions for wc in tr.work_center_ids):
                invalidate_work_centers_cache()
    except SQLAlchemyError:
        # Best-effort reconcile lost a version race OR its commit failed on a
        # poisoned session (audit INSERT collision). Either way, drop our
        # redundant mutation + its audit rows and serve the read with the
        # freshest data; expire so subsequent reads reload from the DB.
        db.rollback()
        db.expire_all()


def _is_open_time_entry_violation(exc: IntegrityError) -> bool:
    """True only when an IntegrityError is the open-time-entry unique violation.

    The partial unique index ``uq_open_time_entry`` (one open clock-in per
    user/operation) is what backs the "already clocked in" 400. Other integrity
    failures (FK violations, a different unique constraint, NOT NULL) must NOT be
    masked as a duplicate clock-in -- they should surface as their own error.
    Inspect the driver-level original exception text for the constraint name.
    """
    orig = getattr(exc, "orig", None)
    haystack = str(orig) if orig is not None else str(exc)
    return "uq_open_time_entry" in haystack


# Adoption-telemetry channels a client may NOT self-assert on an interactive labor
# write. IMPORT is reserved for the bulk-migration loaders, which write TimeEntry
# rows directly (never through these HTTP endpoints); an operator/desktop request
# claiming it would corrupt import provenance -- so the loaders' channel can only
# ever come from the loaders. Rejected with 422. (KIOSK/DESKTOP/SCANNER/BACKFILL
# stay client-settable: A0.1's contract is "store the declared channel, never
# guess", and the shipped /kiosk OperatorKiosk station self-reports "kiosk" on a
# normal session -- see the A0.1 source-tagging tests.)
_FORBIDDEN_CLIENT_LABOR_SOURCES = frozenset({TimeEntrySource.IMPORT})


def _resolve_labor_source(current_user: User, client_source: Optional[TimeEntrySource]) -> Optional[str]:
    """Resolve the adoption-telemetry channel to record on a labor write.

    Trust model (labor twin of ``_record_source`` for process-sheet records):

    * A kiosk-scoped credential (a badge-minted crew-station operator token,
      ``_token_scope == "kiosk"``) is AUTHORITATIVE -- it always records KIOSK
      regardless of any client hint, so a crew station can never be tricked into
      stamping ``backfill``/``import`` (or anything else) onto its labor.
    * Otherwise the client's declared channel is stored verbatim, EXCEPT the
      loader-reserved channels (``IMPORT``), which a normal request may never
      claim -- rejected with HTTP 422 so the write mutates nothing.
    * Omitted -> ``None`` (NULL): the server never guesses a channel.
    """
    if getattr(current_user, "_token_scope", None) == "kiosk":
        return TimeEntrySource.KIOSK.value
    if client_source in _FORBIDDEN_CLIENT_LABOR_SOURCES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"source '{client_source.value}' cannot be set on an interactive labor entry; "
                "it is reserved for the bulk-import loaders"
            ),
        )
    return client_source.value if client_source else None


def _operation_check_in_state(db: Session, operation: WorkOrderOperation) -> dict:
    blocked = operation_blocked_by_predecessors(db, operation)
    can_check_in = (
        operation.status
        in [
            OperationStatus.PENDING,
            OperationStatus.READY,
            OperationStatus.IN_PROGRESS,
            OperationStatus.ON_HOLD,
        ]
        and not blocked
    )
    return {
        "can_check_in": can_check_in,
        "blocked_by_previous_operations": blocked,
    }


def _laser_nest_payload(operation: WorkOrderOperation) -> Optional[dict]:
    sync_laser_nest_from_operation(operation)
    # Soft-delete guard: a soft-deleted manual nest must never surface in the
    # operator queue / kiosk payloads (active_laser_nest returns None for it).
    nest = active_laser_nest(operation)
    if not nest:
        return None
    return {
        "id": nest.id,
        "nest_name": nest.nest_name,
        "cnc_file_name": nest.cnc_file_name,
        "cnc_file_path": nest.cnc_file_path,
        "planned_runs": nest.planned_runs,
        "completed_runs": nest.completed_runs,
        "remaining_runs": nest.remaining_runs,
        "material": nest.material,
        "thickness": nest.thickness,
        "sheet_size": nest.sheet_size,
        # Manual-entry + per-nest PDF fields (mirrors manual_nest_response_dict's
        # document access pattern; .document is the same lazy-load on LaserNest).
        "cnc_number": nest.cnc_number,
        "document_id": nest.document_id,
        "has_document": bool(nest.document_id),
        "document_file_name": nest.document.file_name if nest.document else None,
    }


@router.get("/my-active-job")
def get_my_active_job(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get the current user's active time entries (clocked in jobs)"""
    # Eager-load operation, work_order, and work_order.part in a single
    # query so we don't issue 2*N extra SELECTs iterating active entries.
    active_entries = (
        db.query(TimeEntry)
        .options(
            joinedload(TimeEntry.operation),
            joinedload(TimeEntry.work_center),
            joinedload(TimeEntry.work_order).joinedload(WorkOrder.part),
        )
        .filter(
            and_(
                TimeEntry.user_id == current_user.id,
                TimeEntry.clock_out.is_(None),
            )
        )
        .all()
    )

    if not active_entries:
        return {"active_jobs": [], "active_job": None}

    jobs = []
    for entry in active_entries:
        operation = entry.operation
        work_order = entry.work_order

        jobs.append(
            {
                "time_entry_id": entry.id,
                "clock_in": to_utc_iso(entry.clock_in),
                "entry_type": entry.entry_type,
                "work_order_id": entry.work_order_id,
                "operation_id": entry.operation_id,
                "work_center_id": entry.work_center_id,
                "work_order_number": work_order.work_order_number if work_order else None,
                "part_number": work_order.part.part_number if work_order and work_order.part else None,
                "part_name": work_order.part.name if work_order and work_order.part else None,
                "operation_name": operation.name if operation else None,
                "operation_number": operation.operation_number if operation else None,
                "work_center_name": entry.work_center.name if entry.work_center else None,
                "quantity_ordered": operation_target_quantity(operation, work_order),
                "work_order_quantity_ordered": (
                    float(work_order.quantity_ordered) if work_order and work_order.quantity_ordered else 0
                ),
                "component_quantity": (
                    float(operation.component_quantity) if operation and operation.component_quantity else None
                ),
                "quantity_complete": (
                    float(operation.quantity_complete) if operation and operation.quantity_complete else 0
                ),
                # G5-A: surface approval state on the active-job list serializer (these
                # are open entries so typically null, but kept uniform with TimeEntryResponse).
                "approved": to_utc_iso(entry.approved) if entry.approved else None,
                "approved_by": entry.approved_by,
                "laser_nest": _laser_nest_payload(operation) if operation else None,
            }
        )

    return {
        "active_jobs": jobs,
        "active_job": jobs[0] if jobs else None,
    }


@router.post("/clock-in", response_model=TimeEntryResponse)
def clock_in(
    clock_in_data: ClockIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Clock in to a work order operation"""
    # Prevent duplicate clock-ins for the same operation (A0.4: shared gate helper --
    # the scanner resolve-action endpoint consults the same predicate).
    existing = get_open_time_entry(db, current_user.id, clock_in_data.operation_id)

    if existing:
        raise HTTPException(status_code=400, detail="You are already clocked in to this operation.")

    # Verify work order and operation (scoped to the active company so a guessed
    # foreign operation_id cannot drive another tenant's operation/WO IN_PROGRESS)
    operation = (
        db.query(WorkOrderOperation)
        .options(joinedload(WorkOrderOperation.work_order).joinedload(WorkOrder.part))
        .filter(
            WorkOrderOperation.id == clock_in_data.operation_id,
            WorkOrderOperation.company_id == company_id,
        )
        .first()
    )

    if not operation:
        raise HTTPException(status_code=404, detail="Operation not found")

    if operation.work_order_id != clock_in_data.work_order_id:
        raise HTTPException(status_code=400, detail="Operation does not belong to this work order")

    if operation.work_center_id != clock_in_data.work_center_id:
        # Shared constant so the scanner resolver's blocker text can never drift.
        raise HTTPException(status_code=400, detail=MSG_WRONG_WORK_CENTER)

    if operation.status not in CLOCK_IN_ALLOWED_STATUSES:
        raise HTTPException(status_code=400, detail="Operation is not ready to start")

    # Prevent out-of-sequence starts
    work_order = operation.work_order
    if not work_order:
        raise HTTPException(status_code=404, detail="Work order not found")
    if operation_blocked_by_predecessors(db, operation):
        raise HTTPException(status_code=400, detail="Previous operations must be completed first")

    # A0.1 adoption-telemetry channel for this labor write, with the kiosk-token
    # forcing + import guard applied centrally. Resolved before any mutation so a
    # disallowed 'import' 422s without touching the operation/WO.
    recorded_source = _resolve_labor_source(current_user, clock_in_data.source)

    # Update operation status
    if operation.status in [OperationStatus.PENDING, OperationStatus.READY]:
        operation.status = OperationStatus.IN_PROGRESS
        if not operation.actual_start:
            operation.actual_start = datetime.utcnow()
        operation.started_by = current_user.id

    # Update work order status
    if work_order.status == WorkOrderStatus.RELEASED:
        work_order.status = WorkOrderStatus.IN_PROGRESS
        work_order.actual_start = datetime.utcnow()

    # Create time entry
    time_entry = TimeEntry(
        user_id=current_user.id,
        work_order_id=clock_in_data.work_order_id,
        operation_id=clock_in_data.operation_id,
        work_center_id=clock_in_data.work_center_id,
        entry_type=clock_in_data.entry_type,
        clock_in=datetime.utcnow(),
        notes=clock_in_data.notes,
        # A0.1 adoption telemetry: resolved channel (client value, kiosk-forced for a
        # kiosk token, or NULL -- never guessed).
        source=recorded_source,
        company_id=company_id,
    )

    db.add(time_entry)

    # G5-B: warn-and-record operator-qualification gate. The operation/WC/predecessor
    # validation passed and the TimeEntry is in this unit of work; evaluate the
    # (read-only, tenant-scoped) skill + certification gates and, for each unsatisfied
    # one, leave a tamper-evident audit row + a warning OperationalEvent that commit
    # ATOMICALLY with this clock-in below. NEVER blocks the clock-in.
    qualification_exceptions = evaluate_and_record_operator_qualification(
        db,
        company_id=company_id,
        user=current_user,
        operation=operation,
        work_center_id=operation.work_center_id,
        audit=audit,
        source="clock_in",
    )

    OperationalEventService(db).emit_best_effort(
        company_id=company_id,
        event_type="labor_clock_in",
        source_module="shop_floor",
        entity_type="time_entry",
        work_order_id=work_order.id,
        operation_id=operation.id,
        user_id=current_user.id,
        severity="info",
        event_payload={
            "work_order_number": work_order.work_order_number,
            "operation_name": operation.name,
            "work_center_id": operation.work_center_id,
            "entry_type": (
                clock_in_data.entry_type.value
                if hasattr(clock_in_data.entry_type, "value")
                else clock_in_data.entry_type
            ),
            # A0.1 adoption telemetry: resolved channel (None = not reported).
            "source": recorded_source,
        },
    )
    # AS9100D traceability: a paper back-entry (source=backfill) is a manual,
    # after-the-fact labor record, so put it on the tamper-evident audit chain like
    # any other state change (a live clock-in is self-evidenced by the labor_clock_in
    # OperationalEvent above; a back-fill needs an explicit who/when audit row). The
    # flush assigns time_entry.id for the audit row, and both commit ATOMICALLY with
    # the clock-in inside the existing try so a duplicate-open-entry IntegrityError is
    # still translated to the 400 below and the audit row rolls back with it.
    try:
        if recorded_source == TimeEntrySource.BACKFILL.value:
            db.flush()
            audit.log_create(
                resource_type="time_entry",
                resource_id=time_entry.id,
                resource_identifier=f"WO {work_order.work_order_number} / OP {operation.operation_number}",
                description=(
                    f"Back-filled labor clock-in on operation {operation.operation_number} "
                    f"of work order {work_order.work_order_number} (source=backfill)"
                ),
                extra_data={"source": TimeEntrySource.BACKFILL.value},
            )
        db.commit()
    except IntegrityError as exc:
        # The pre-check above handles the common case, but a concurrent clock-in
        # for the same (user, operation) can slip past it under READ COMMITTED.
        # The partial unique index uq_open_time_entry rejects the duplicate open
        # row at the DB; translate ONLY that violation to the same 400 the
        # pre-check returns. Any OTHER integrity failure is a genuinely different
        # bug and must not be masked -- roll back and re-raise it.
        db.rollback()
        if _is_open_time_entry_violation(exc):
            raise HTTPException(status_code=400, detail="You are already clocked in to this operation.") from exc
        raise
    except StaleDataError as exc:
        # A concurrent writer bumped the operation/WO version between read and
        # commit (version_id_col mismatch). Surface a clean 409, not a 500.
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="This operation was modified concurrently. Refresh and retry.",
        ) from exc
    db.refresh(time_entry)

    safe_broadcast(
        broadcast_shop_floor_update,
        clock_in_data.work_center_id,
        {
            "event": "clock_in",
            "work_order_id": clock_in_data.work_order_id,
            "operation_id": clock_in_data.operation_id,
            "user_id": current_user.id,
        },
        company_id=company_id,
    )
    safe_broadcast(
        broadcast_work_order_update,
        work_order.id,
        {
            "event": "clock_in",
            "operation_id": clock_in_data.operation_id,
            "status": work_order.status.value if hasattr(work_order.status, "value") else work_order.status,
        },
        company_id=company_id,
    )
    safe_broadcast(
        broadcast_dashboard_update,
        {
            "event": "clock_in",
            "work_order_id": work_order.id,
            "operation_id": clock_in_data.operation_id,
        },
        company_id=company_id,
    )

    # G5-B: surface any unsatisfied qualification gate on the response (set AFTER the
    # refresh so it survives -- it is a transient, non-column attribute the
    # TimeEntryResponse schema reads; defaults to empty list otherwise). Warn-only.
    time_entry.qualification_exceptions = [exc.as_dict() for exc in qualification_exceptions]

    return time_entry


@router.post("/clock-out/{time_entry_id}", response_model=TimeEntryResponse)
def clock_out(
    time_entry_id: int,
    clock_out_data: ClockOut,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Clock out from a work order operation"""
    time_entry = (
        db.query(TimeEntry)
        .filter(
            and_(
                TimeEntry.id == time_entry_id,
                TimeEntry.user_id == current_user.id,
                TimeEntry.company_id == company_id,
            )
        )
        .first()
    )

    if not time_entry:
        raise HTTPException(status_code=404, detail="Time entry not found")

    if time_entry.clock_out:
        raise HTTPException(status_code=400, detail="Already clocked out")

    if not time_entry.clock_in:
        # Defensive: every row should have clock_in because the insert
        # happens at clock-in time, but don't 500 if a bad row slipped in.
        raise HTTPException(status_code=500, detail="Time entry is missing clock_in")

    if clock_out_data.quantity_produced < 0 or clock_out_data.quantity_scrapped < 0:
        raise HTTPException(status_code=400, detail="Quantities cannot be negative")

    # A0.1 adoption-telemetry channel for this write (kiosk-token forcing + import
    # guard). Resolved before any mutation so a disallowed 'import' 422s untouched.
    recorded_source = _resolve_labor_source(current_user, clock_out_data.source)

    # Lean Phase 1: resolve the structured scrap reason code BEFORE any mutation so
    # an invalid id (404 unknown/cross-tenant, 422 inactive) leaves no half-updated
    # row. None passes through untouched.
    scrap_code = resolve_scrap_reason_code_or_http(db, company_id, clock_out_data.scrap_reason_code_id)

    # Look up related work order / operation BEFORE mutating the time entry
    # so we can return a clean 404 instead of leaving a half-updated row
    # in the session if the referenced work order was deleted.
    #
    # SFI-1: lock the operation row (and, when present, the parent work order)
    # with SELECT ... FOR UPDATE before the over-completion read-modify-write so
    # two concurrent completers serialize instead of losing updates / both
    # flipping the WO to COMPLETE. Consistent lock order across every completion
    # path: OPERATION first, then WORK ORDER. The lock re-fetch stays scoped to
    # the active company and re-reads the freshest committed row.
    operation = None
    if time_entry.operation_id is not None:
        operation = (
            db.query(WorkOrderOperation)
            .filter(
                WorkOrderOperation.id == time_entry.operation_id,
                WorkOrderOperation.company_id == company_id,
            )
            .with_for_update()
            .first()
        )
        if not operation:
            raise HTTPException(
                status_code=404,
                detail="Operation for this time entry no longer exists",
            )

    work_order = (
        db.query(WorkOrder)
        .filter(
            WorkOrder.id == time_entry.work_order_id,
            WorkOrder.company_id == company_id,
            WorkOrder.is_deleted == False,  # noqa: E712
        )
        .with_for_update()
        .first()
    )
    if not work_order:
        raise HTTPException(
            status_code=404,
            detail="Work order for this time entry no longer exists",
        )

    # G6-A: an operator can be clocked into a WO that gets CANCELLED/CLOSED/COMPLETE
    # mid-operation. We must NEVER trap their open TimeEntry, so clock_out ALWAYS
    # closes the entry (the durable, auditable labor record). But when the parent WO
    # is terminal we MUST NOT drive its operations toward completion or accrue cost
    # onto a finished/cancelled job, so every production-rollup + completion side
    # effect below is gated on `not wo_is_terminal`. A plain 409 here would be wrong:
    # it would strand the open time entry forever.
    wo_is_terminal = work_order.status in TERMINAL_WO_STATUSES

    if operation is not None and not wo_is_terminal:
        # Re-evaluate the over-completion guard against the freshly locked row so
        # a concurrent producer's committed quantity is seen here. Skipped for a
        # terminal WO -- we don't roll the produced qty up onto it anyway, and the
        # operator must still be allowed to close out regardless of quantity.
        target_qty = operation_target_quantity(operation, work_order)
        additional_good_qty = float(clock_out_data.quantity_produced or 0)
        if (float(operation.quantity_complete or 0) + additional_good_qty) > target_qty:
            raise HTTPException(status_code=400, detail="Quantity produced exceeds quantity ordered")

    # Update time entry
    time_entry.clock_out = datetime.utcnow()
    time_entry.duration_hours = (time_entry.clock_out - time_entry.clock_in).total_seconds() / 3600
    time_entry.quantity_produced = float(time_entry.quantity_produced or 0) + float(
        clock_out_data.quantity_produced or 0
    )
    time_entry.quantity_scrapped = float(time_entry.quantity_scrapped or 0) + float(
        clock_out_data.quantity_scrapped or 0
    )
    # A0.3: only overwrite when the clock-out actually carries a reason -- the kiosk
    # COMPLETE flow clocks out with zero scrap and no reason, which must not null a
    # reason recorded by an in-shift /production report.
    if clock_out_data.scrap_reason:
        time_entry.scrap_reason = clock_out_data.scrap_reason
    # Lean Phase 1: same never-clear semantics for the structured code -- persisted
    # whenever the clock-out carries one, never nulled by a code-less clock-out.
    if scrap_code is not None:
        time_entry.scrap_reason_code_id = scrap_code.id
    time_entry.notes = clock_out_data.notes or time_entry.notes
    # A0.1 adoption telemetry: record the clock-out channel when this write carries one
    # (a kiosk-scoped token always resolves to KIOSK); omitted on a normal session ->
    # keep whatever channel clock-in recorded (NULL stays NULL, never guessed).
    if recorded_source:
        time_entry.source = recorded_source

    # G6-A: terminal WO -> close the labor entry (above) but never roll its hours /
    # produced / scrapped quantities up onto the operation. The TimeEntry remains the
    # durable labor record; we simply don't drive a finished/cancelled job's op state.
    if operation and not wo_is_terminal:
        if time_entry.entry_type == TimeEntryType.SETUP:
            operation.actual_setup_hours += time_entry.duration_hours
        else:
            operation.actual_run_hours += time_entry.duration_hours

        # clock_out is an ADDITIVE verb: the operation total grows by the produced
        # delta. The stored result is floored at durable TimeEntry evidence and
        # capped at the operation target so additive and absolute writes converge
        # on the same invariant (DUP-3 / SFI-5). The over-completion guard above
        # already rejected a delta that would exceed target.
        additive_complete = float(operation.quantity_complete or 0) + float(clock_out_data.quantity_produced or 0)
        operation.quantity_complete = floor_operation_quantity_at_evidence(
            db, operation, additive_complete, operation_target_quantity(operation, work_order)
        )
        operation.quantity_scrapped = float(operation.quantity_scrapped or 0) + float(
            clock_out_data.quantity_scrapped or 0
        )
        # Lean Phase 1: categorize the operation's scrap when THIS write carries both
        # scrap and a code (a code-less write never clears a recorded one).
        if scrap_code is not None and float(clock_out_data.quantity_scrapped or 0) > 0:
            operation.scrap_reason_code_id = scrap_code.id
        # Lean Phase 1 (FPY): a REWORK entry booking produced quantity is re-processed
        # work -- track it on the operation so FPY/RTY can subtract it from first-pass.
        if time_entry.entry_type == TimeEntryType.REWORK and float(clock_out_data.quantity_produced or 0) > 0:
            operation.quantity_reworked = float(operation.quantity_reworked or 0) + float(
                clock_out_data.quantity_produced or 0
            )
        sync_laser_nest_from_operation(operation)

    # G6-A: never accrue cost/hours onto a terminal WO.
    if not wo_is_terminal:
        work_order.actual_hours += time_entry.duration_hours
    else:
        logger.warning(
            "clock_out closed time entry %s against terminal WO %s (%s) without rollup: "
            "labor recorded on the TimeEntry only, no op/cost accrual",
            time_entry.id,
            work_order.work_order_number,
            work_order.status.value if hasattr(work_order.status, "value") else work_order.status,
        )
    OperationalEventService(db).emit_best_effort(
        company_id=company_id,
        event_type="labor_clock_out",
        source_module="shop_floor",
        entity_type="time_entry",
        entity_id=time_entry.id,
        work_order_id=work_order.id,
        operation_id=operation.id if operation else None,
        user_id=current_user.id,
        severity="info",
        event_payload={
            "work_order_number": work_order.work_order_number,
            "duration_hours": round(time_entry.duration_hours or 0, 4),
            "quantity_produced": clock_out_data.quantity_produced,
            "quantity_scrapped": clock_out_data.quantity_scrapped,
            "scrap_reason": clock_out_data.scrap_reason,
            "scrap_reason_code_id": scrap_code.id if scrap_code else None,
            # G6-A: flag so AI/realtime consumers know this labor closed against a
            # terminal WO and was deliberately NOT rolled up into op/cost.
            "wo_terminal": wo_is_terminal,
            # A0.1 adoption telemetry: resolved channel (None = not reported).
            "source": recorded_source,
        },
    )

    # Capture pre-mutation statuses so completion transitions can be audited below.
    old_operation_status = operation.status.value if operation and operation.status else None
    old_work_order_status = work_order.status.value if work_order.status else None
    operation_completed = False
    work_order_completed = False
    # PERF-5: tracks whether the scheduling refresh ran (it runs with commit=False,
    # so the WC cache must be invalidated by us after the terminal commit succeeds).
    work_centers_refreshed = False
    # Process-sheet gate (PR 3): set when this clock-out reached the operation target
    # but required steps lack records — surfaced on the response so the kiosk can
    # tell the operator why the op did not complete.
    steps_incomplete: Optional[dict] = None

    # Update statuses if operation complete. The shared finalizer owns the rollup
    # (remaining-ops decision, COMPLETE-vs-release branch, actual_start/actual_end
    # stamping, qty sync, current_operation_id) so this path can never drift from
    # the office / scan twins (DUP-5). The caller still owns the row locks (held),
    # the audit rows below, and the scheduling refresh of the returned WCs.
    #
    # G6-A: SKIP the entire finalize/advance block for a terminal WO. We never drive a
    # finished/cancelled job's operation toward COMPLETE nor lift it to IN_PROGRESS.
    # operation_completed / work_order_completed therefore stay False, so no spurious
    # completion audit row, completion OperationalEvent, FG receipt/backflush/cost
    # rollup, or scheduling refresh fires below for a terminal WO.
    if operation and not wo_is_terminal:
        target_qty = operation_target_quantity(operation, work_order)
        is_fully_complete = operation.quantity_complete >= target_qty

        # Process-sheet completion gate (PR 3): reaching target via clock-out must not
        # auto-complete an operation whose required (non-INSTRUCTION) snapshot steps
        # lack live conforming records — the same predicate as the /complete twins.
        # Follows the G6-A never-trap-an-open-TimeEntry precedent: the entry above
        # ALWAYS closed normally with its full quantities (labor truth is separate
        # from completion); the operation simply stays IN_PROGRESS at/near target and
        # the response carries a ``steps_incomplete`` warning so the kiosk can tell
        # the operator. Completion then happens via /complete once records exist
        # (or supersede corrections land).
        if is_fully_complete:
            missing_steps = process_sheet_service.missing_required_steps(db, company_id, operation, work_order)
            if missing_steps:
                steps_incomplete = {"code": "STEPS_INCOMPLETE", "missing": missing_steps}
                is_fully_complete = False

        if is_fully_complete:
            operation.status = OperationStatus.COMPLETE
            operation.actual_end = datetime.utcnow()
            operation.completed_by = current_user.id
            # DUP-2: the clock_out path historically stamped only actual_end /
            # completed_by, leaving actual_start NULL on a one-shot completion. The
            # finalizer would then fall back to now() for the WO actual_start AFTER
            # actual_end was set, yielding a negative cycle time. Stamp the op's
            # actual_start from its earliest TimeEntry clock_in (the entry being
            # clocked out, or any earlier one), falling back to actual_end so the
            # op never carries actual_end without a sane (<=) actual_start.
            if not operation.actual_start:
                earliest_clock_in = (
                    db.query(func.min(TimeEntry.clock_in)).filter(TimeEntry.operation_id == operation.id).scalar()
                )
                operation.actual_start = earliest_clock_in or time_entry.clock_in or operation.actual_end
                if not operation.started_by:
                    operation.started_by = current_user.id
            operation_completed = True

            affected_work_centers = finalize_operation_completion(db, work_order, operation)
            work_order_completed = work_order.status == WorkOrderStatus.COMPLETE
            # PERF-5: commit=False joins this scheduling refresh into the handler's
            # single unit of work, so the WO/op state change is committed atomically
            # with the audit rows / cost rollup / quality exceptions written below
            # (the old default commit=True committed the state change mid-handler --
            # a crash before the terminal commit left a completed WO with no audit).
            # commit=False skips the in-service WC cache invalidation, so we do it
            # ourselves after the terminal commit succeeds.
            SchedulingService(db, company_id).update_availability_rates(
                work_center_ids=[wc_id for wc_id in affected_work_centers if wc_id],
                horizon_days=90,
                commit=False,
            )
            work_centers_refreshed = True
        else:
            # Partial production on an unfinished operation: still lift a RELEASED
            # WO to IN_PROGRESS / stamp actual_start and roll partial qty up (DUP-2,
            # RUP-6) without forcing a completion rollup.
            begin_operation_progress(work_order, operation)
            sync_work_order_quantity_complete(work_order, operation, all_operations_complete=False)
        work_order.updated_at = datetime.utcnow()

    # Audit completion transitions on the tamper-evident chain. clock_out is the
    # primary floor completion path; the OperationalEvent above is an AI/realtime
    # signal, not the audit_log hash chain. Logged BEFORE the terminal commit so
    # the audit rows commit atomically with the status change.
    quality_exceptions: list[QualityException] = []
    if operation_completed or work_order_completed:
        db.flush()
        audit = AuditService(db, current_user)
        if operation_completed and operation:
            audit.log_status_change(
                resource_type="work_order_operation",
                resource_id=operation.id,
                resource_identifier=operation.operation_number,
                old_status=old_operation_status,
                new_status=OperationStatus.COMPLETE.value,
                description=(
                    f"Completed operation {operation.operation_number} on WO "
                    f"{work_order.work_order_number} via clock-out"
                ),
            )
        if work_order_completed:
            audit.log_status_change(
                resource_type="work_order",
                resource_id=work_order.id,
                resource_identifier=work_order.work_order_number,
                old_status=old_work_order_status,
                new_status=WorkOrderStatus.COMPLETE.value,
                description=f"Completed work order {work_order.work_order_number} via clock-out",
            )
        # EVT-2: emit the uniform completion OperationalEvents in-process (before the
        # terminal commit so they land atomically with the status change). The
        # labor_clock_out event above is a labor signal, NOT a completion signal --
        # AI/realtime consumers need operation_completed/work_order_completed too.
        if operation_completed and operation:
            emit_operation_completed_event(
                db,
                company_id=company_id,
                work_order=work_order,
                operation=operation,
                user_id=current_user.id,
                source_module="shop_floor",
                source=recorded_source,
            )
        if work_order_completed:
            emit_work_order_completed_event(
                db,
                company_id=company_id,
                work_order=work_order,
                user_id=current_user.id,
                source_module="shop_floor",
                source=recorded_source,
            )
            # Batch 6 / rank 9 (INV-1/INV-2/INV-3/TRACE-2/TRACE-3): on WO COMPLETE,
            # receive the finished good into inventory (ALWAYS, lot-only, idempotent)
            # and backflush BOM components (ONLY if part.backflush_components). Routed
            # through the same locked unit of work so the inventory writes + their
            # tamper-evident audit rows commit ATOMICALLY with the completion. A
            # backflush shortage is recorded but never fails the completion.
            apply_completion_inventory_effects(
                db, work_order, user_id=current_user.id, company_id=company_id, audit=audit
            )
            # Batch 7 / rank 10 (COST-1/COST-2/COST-4/COST-5): OPT-IN labor hour +
            # actual-cost + JobCost rollup, atomic with this clock-out completion. No-op
            # + pre-Batch-7 behavior when the flag is OFF. (The single-entry hour rollup
            # for the entry being clocked out is applied above, flag-independent, as it
            # always was; this evidence-sourced rollup reconciles the full WO totals.)
            apply_completion_cost_rollup(db, work_order, company_id=company_id, user_id=current_user.id, audit=audit)
        # Batch 4 / rank 7 (QG-1/3, BLK-2): warn-and-record. Completion has already
        # mutated state and is about to commit -- evaluate the (read-only, locked-row)
        # quality gates and, for each unsatisfied one, leave a tamper-evident audit
        # row + a warning OperationalEvent that commit ATOMICALLY with this clock-out.
        # Never blocks the completion. Runs against the already-loaded/locked op + WO.
        if operation:
            quality_exceptions = evaluate_and_record_completion_quality_exceptions(
                db,
                company_id=company_id,
                work_order=work_order,
                operation=operation,
                audit=audit,
                user=current_user,
                source="clock_out",
            )
        # Batch 7 data-quality signal (no_labor_recorded): fires REGARDLESS of the
        # cost-rollup flag when the WO completes with any zero-labor operation. Same
        # quality_exceptions channel; warn-only.
        if work_order_completed:
            quality_exceptions = quality_exceptions + evaluate_and_record_labor_data_quality(
                db,
                company_id=company_id,
                work_order=work_order,
                audit=audit,
                user=current_user,
                source="clock_out",
            )
        # G1 ADVANCE: when THIS WO (a laser child) just completed, surface a signal on
        # its parent iff every laser child is now terminal. Signal-only -- we do NOT
        # auto-complete the parent (parent/child WOs are not operation-coupled). The
        # advance fires only when ALL children are terminal, which becomes true exactly
        # once (when the last child flips); completion handlers are idempotent and
        # reconcile won't re-flip a terminal child, so this records at most once.
        if work_order_completed:
            parent = find_parent_to_advance(db, work_order, company_id)
            if parent is not None:
                record_parent_children_complete(
                    db,
                    parent_work_order=parent,
                    child_work_order=work_order,
                    company_id=company_id,
                    user_id=current_user.id,
                    audit=audit,
                    source="completion",
                )

    # AS9100D traceability: a paper back-entry clock-out (source=backfill) is a manual,
    # after-the-fact labor record -- audit it on the tamper-evident chain, atomic with
    # this commit, via the request-scoped AuditService so the row captures ip_address /
    # user_agent (matching the clock-in backfill path). (The labor_clock_out
    # OperationalEvent above is an AI/realtime signal, not the audit hash chain; any
    # completion status change is audited separately.)
    if recorded_source == TimeEntrySource.BACKFILL.value:
        audit.log(
            action="UPDATE",
            resource_type="time_entry",
            resource_id=time_entry.id,
            resource_identifier=(
                f"WO {work_order.work_order_number} / OP {operation.operation_number if operation else '-'}"
            ),
            description=f"Back-filled labor clock-out on work order {work_order.work_order_number} (source=backfill)",
            extra_data={"source": TimeEntrySource.BACKFILL.value},
        )

    try:
        db.commit()
    except StaleDataError as exc:
        # A concurrent completer committed a newer version of the operation/WO
        # between our locked read and this commit (version_id_col mismatch).
        # Surface a clean 409 instead of a 500.
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="This operation was modified concurrently. Refresh and retry the clock-out.",
        ) from exc
    db.refresh(time_entry)

    # PERF-5: the scheduling refresh ran with commit=False (joined to this handler's
    # unit of work), so it skipped the in-service WC cache invalidation -- do it here,
    # after the terminal commit succeeded, so the cache reflects the freed capacity.
    if work_centers_refreshed:
        invalidate_work_centers_cache()

    if operation and operation.work_center_id:
        safe_broadcast(
            broadcast_shop_floor_update,
            operation.work_center_id,
            {
                "event": "clock_out",
                "work_order_id": time_entry.work_order_id,
                "operation_id": operation.id,
                "user_id": current_user.id,
            },
            company_id=company_id,
        )
    safe_broadcast(
        broadcast_work_order_update,
        work_order.id,
        {
            "event": "clock_out",
            "operation_id": operation.id if operation else None,
            "status": work_order.status.value if hasattr(work_order.status, "value") else work_order.status,
        },
        company_id=company_id,
    )
    safe_broadcast(
        broadcast_dashboard_update,
        {
            "event": "clock_out",
            "work_order_id": work_order.id,
            "operation_id": operation.id if operation else None,
        },
        company_id=company_id,
    )

    # EVT-3: on WO COMPLETE, enqueue the outbound notification + webhook dispatch
    # (tenant-scoped, in the ARQ worker). Enqueued AFTER the commit so we never fire
    # signals for a completion that rolled back, and best-effort so a Redis/enqueue
    # failure can't fail an already-committed completion.
    if work_order_completed:
        enqueue_work_order_completion_signals(work_order_id=work_order.id, company_id=company_id, status="COMPLETE")

    # Surface the warn-and-record quality exceptions on the response (QG-4 / schema).
    # Attached AFTER db.refresh(time_entry) so the ORM refresh can't clobber it; the
    # TimeEntryResponse schema reads this attribute (default empty list otherwise).
    time_entry.quality_exceptions = [exc.as_dict() for exc in quality_exceptions]
    # Process-sheet gate (PR 3): tell the kiosk WHY the op did not auto-complete
    # (same transient-attribute channel as quality_exceptions; null when N/A).
    time_entry.steps_incomplete = steps_incomplete

    return time_entry


# Roles allowed to approve/unapprove shop-floor labor (G5-A). Admin + manager +
# supervisor + quality -- matches the documented approver set used elsewhere (the
# shop-floor inspection sign-off and the Quality/ECO approvals all grant MANAGER), so a
# Manager isn't anomalously locked out of labor sign-off. Reads stay broad; only the
# approve/unapprove writes gate. All four names verified in app/models/user.py UserRole.
_TIME_ENTRY_APPROVAL_ROLES = [UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR, UserRole.QUALITY]


def _set_time_entry_approval(
    time_entry_id: int,
    *,
    approve: bool,
    db: Session,
    current_user: User,
    company_id: int,
    audit: AuditService,
) -> TimeEntry:
    """Shared approve/unapprove body (G5-A).

    Tenant-scoped lookup (``company_id``). Forbids self-approval (a user cannot approve
    their OWN TimeEntry -> 403; segregation of duties for the labor-cost gate). Sets/clears
    ``approved`` (timestamp) + ``approved_by`` and writes ONE tamper-evident
    ``AuditService.log_update`` row. Respects the model's optimistic-lock ``version`` column
    -- a concurrent stale write raises ``StaleDataError`` on flush, surfaced as HTTP 409.
    Joins the request's unit of work; commits at the end (this IS the unit of work).
    """
    time_entry = db.query(TimeEntry).filter(TimeEntry.id == time_entry_id, TimeEntry.company_id == company_id).first()
    if not time_entry:
        raise HTTPException(status_code=404, detail="Time entry not found")

    # Segregation of duties: you cannot approve your own labor (even if you hold an
    # approver role). Unapprove is gated the same way for symmetry.
    if time_entry.user_id == current_user.id:
        raise HTTPException(status_code=403, detail="You cannot approve or unapprove your own time entry")

    old_approved = time_entry.approved
    old_approved_by = time_entry.approved_by

    if approve:
        if time_entry.approved is not None:
            # Idempotent: already approved -> return current state (no second audit row).
            return time_entry
        time_entry.approved = datetime.utcnow()
        time_entry.approved_by = current_user.id
        action = "time_entry_approve"
        description = f"Approved time entry {time_entry.id} (user {time_entry.user_id})"
    else:
        if time_entry.approved is None:
            # Idempotent: already un-approved -> no-op.
            return time_entry
        time_entry.approved = None
        time_entry.approved_by = None
        action = "time_entry_unapprove"
        description = f"Unapproved time entry {time_entry.id} (user {time_entry.user_id})"

    # Tamper-evident audit (hash chain), flushed inside this unit of work so it commits
    # atomically with the approval flip. log_update computes the field-level diff.
    audit.log_update(
        resource_type="time_entry",
        resource_id=time_entry.id,
        resource_identifier=str(time_entry.id),
        old_values={
            "approved": to_utc_iso(old_approved),
            "approved_by": old_approved_by,
        },
        new_values={
            "approved": to_utc_iso(time_entry.approved),
            "approved_by": time_entry.approved_by,
        },
        description=description,
        action=action,
        extra_data={"work_order_id": time_entry.work_order_id, "operation_id": time_entry.operation_id},
    )

    try:
        db.commit()
    except StaleDataError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="This time entry was modified concurrently. Refresh and retry.",
        ) from exc
    db.refresh(time_entry)
    return time_entry


@router.post("/time-entries/{time_entry_id}/approve", response_model=TimeEntryResponse)
def approve_time_entry(
    time_entry_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_TIME_ENTRY_APPROVAL_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Approve a shop-floor TimeEntry (G5-A).

    Sets ``approved`` (now) + ``approved_by`` (the approver). Role-gated to
    ADMIN / MANAGER / SUPERVISOR / QUALITY; forbids self-approval; tenant-scoped; respects the
    optimistic-lock ``version`` column; audited. Approval is what the opt-in
    ``REQUIRE_APPROVED_LABOR_FOR_COST`` flag keys on for the labor-cost legs.
    """
    return _set_time_entry_approval(
        time_entry_id,
        approve=True,
        db=db,
        current_user=current_user,
        company_id=company_id,
        audit=audit,
    )


@router.post("/time-entries/{time_entry_id}/unapprove", response_model=TimeEntryResponse)
def unapprove_time_entry(
    time_entry_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_TIME_ENTRY_APPROVAL_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Clear approval on a shop-floor TimeEntry (G5-A).

    Clears ``approved`` + ``approved_by``. Same role gate / self-approval ban /
    tenant scope / optimistic-lock handling / audit as approve.
    """
    return _set_time_entry_approval(
        time_entry_id,
        approve=False,
        db=db,
        current_user=current_user,
        company_id=company_id,
        audit=audit,
    )


@router.get("/work-center-queue/{work_center_id}")
def get_work_center_queue(
    work_center_id: int,
    db: Session = Depends(get_db),
    principal: KioskReadPrincipal = Depends(get_kiosk_or_user),
):
    """Get operations queued at a work center, with the live crew roster per item.

    Auth accepts EITHER a normal user access token OR a crew-station kiosk
    token (``get_kiosk_or_user``). A station principal may only read ITS OWN
    work center's queue (403 otherwise); users read any queue in their company,
    as before. Each queued item carries a ``roster`` of the open (labor)
    TimeEntries on that operation so the crew kiosk can render per-person
    timers; ``server_time`` lets the client correct clock skew.
    """
    company_id = principal.company_id
    if principal.kind == "station" and principal.work_center_id != work_center_id:
        # The station is physically bound to one work center (from the DB row,
        # never the client) — it can never read another work center's queue.
        raise HTTPException(status_code=403, detail="Kiosk station may only read its own work center queue")

    operations = (
        db.query(WorkOrderOperation)
        .options(
            joinedload(WorkOrderOperation.work_order).joinedload(WorkOrder.part),
            # Eager-load the nest + its reference PDF so _laser_nest_payload below
            # doesn't issue per-row SELECTs (N+1) for each queued laser operation.
            joinedload(WorkOrderOperation.laser_nest).joinedload(LaserNest.document),
        )
        .join(WorkOrder)
        .filter(
            and_(
                WorkOrder.company_id == company_id,
                WorkOrder.is_deleted == False,  # noqa: E712
                WorkOrder.status.not_in([WorkOrderStatus.COMPLETE, WorkOrderStatus.CLOSED, WorkOrderStatus.CANCELLED]),
                WorkOrderOperation.work_center_id == work_center_id,
                WorkOrderOperation.status.in_([OperationStatus.READY, OperationStatus.IN_PROGRESS]),
            )
        )
        .order_by(WorkOrderOperation.scheduled_start)
        .all()
    )

    # Crew roster: open labor TimeEntries for the queued operations, one bucket
    # per operation (the wallboard open-entry query shape — company-scoped,
    # clock_out IS NULL, labor entry types only so an open BREAK/DOWNTIME row
    # never renders as a crew member). joinedload(User) avoids N+1 name lookups.
    roster_by_operation: dict[int, list[dict]] = defaultdict(list)
    operation_ids = [op.id for op in operations]
    if operation_ids:
        open_entries = (
            db.query(TimeEntry)
            .options(joinedload(TimeEntry.user))
            .filter(
                TimeEntry.company_id == company_id,
                TimeEntry.operation_id.in_(operation_ids),
                TimeEntry.clock_out.is_(None),
                TimeEntry.entry_type.in_(LABOR_ENTRY_TYPES),
            )
            .order_by(TimeEntry.clock_in.asc())
            .all()
        )
        for entry in open_entries:
            roster_by_operation[entry.operation_id].append(
                {
                    "time_entry_id": entry.id,
                    "user_id": entry.user_id,
                    "operator_name": (
                        operator_display_name(entry.user.first_name, entry.user.last_name) if entry.user else None
                    ),
                    "employee_id": entry.user.employee_id if entry.user else None,
                    "entry_type": entry.entry_type.value if hasattr(entry.entry_type, "value") else entry.entry_type,
                    "clock_in": to_utc_iso(entry.clock_in),
                }
            )

    # Steps chip (PR 3): required-process-step counts per queued operation so the
    # kiosk job card can render "Steps 2/6" without an extra round-trip. A step only
    # counts as recorded when its live conforming records cover every WO serial.
    step_counts = process_sheet_service.step_counts_for_operations(db, company_id, operations)

    queue = []
    for op in operations:
        wo = op.work_order
        target_qty = operation_target_quantity(op, wo)
        op_step_counts = step_counts.get(op.id, {"steps_total": 0, "steps_recorded": 0})
        queue.append(
            {
                "operation_id": op.id,
                "work_order_id": wo.id,
                "work_order_number": wo.work_order_number,
                "part_number": wo.part.part_number if wo.part else None,
                "part_name": wo.part.name if wo.part else None,
                "operation_number": op.operation_number,
                "operation_name": op.name,
                "work_center_id": op.work_center_id,
                "status": op.status,
                "quantity_ordered": target_qty,
                "work_order_quantity_ordered": wo.quantity_ordered,
                "component_quantity": op.component_quantity,
                "quantity_complete": op.quantity_complete,
                # Crew tally: scrap surfaces next to quantity_complete so the
                # kiosk tally block ("37 of 50 · 2 scrap") is server-derived.
                "quantity_scrapped": op.quantity_scrapped,
                "priority": wo.priority,
                "due_date": wo.due_date,
                "setup_time_hours": op.setup_time_hours,
                "run_time_hours": op.run_time_hours,
                "laser_nest": _laser_nest_payload(op),
                "roster": roster_by_operation.get(op.id, []),
                "steps_total": op_step_counts["steps_total"],
                "steps_recorded": op_step_counts["steps_recorded"],
            }
        )

    # Lean Phase 1 (issue #88): active scrap reason codes ride the queue payload
    # so the crew station's scrap picker works WITHOUT widening any token scope —
    # the station token is honored only by this read + badge mint, and the 5-min
    # badge tokens are path-fenced to /shop-floor, so the kiosk cannot call
    # GET /quality/scrap-reason-codes. This is tenant config data on an
    # already-authorized, already-tenant-scoped read (the station's company from
    # the DB row, never the client). One indexed query (company_id + code are
    # indexed; per-tenant code lists are tiny) — no caching needed at the poll
    # cadence. Optional field: old clients simply ignore the extra key.
    scrap_reason_codes = [
        {
            "id": code.id,
            "code": code.code,
            "name": code.name,
            "category": code.category,
            "display_order": code.display_order,
        }
        for code in tenant_query(db, ScrapReasonCode, company_id)
        .filter(ScrapReasonCode.is_active == True)  # noqa: E712
        .order_by(ScrapReasonCode.display_order, ScrapReasonCode.code)
        .all()
    ]

    return {
        "queue": queue,
        # Timer skew correction: honest per-person timers are computed against
        # the server clock, not the tablet's.
        "server_time": to_utc_iso(datetime.utcnow()),
        # Station identity for the kiosk header; null for normal user callers.
        "station": (
            {"id": principal.station_id, "label": principal.station_label} if principal.kind == "station" else None
        ),
        # Active scrap reason codes for the crew-station scrap picker (see above).
        "scrap_reason_codes": scrap_reason_codes,
    }


def _dashboard_state_fingerprint(
    db: Session,
    company_id: int,
    connected_user_ids: set[int],
    connected_since_by_id: dict[int, Optional[str]],
) -> str:
    """Cheap state fingerprint for the dashboard ETag (PERF-2).

    REPLACES the old ETag that md5-hashed the FULLY-BUILT payload, which forced
    every poll -- even an unchanged one that 304s -- to pay for the write-amplifying
    reconcile AND the whole payload build before it could short-circuit. This
    fingerprint is computed from a handful of cheap aggregate queries instead, so an
    unchanged dashboard can 304 having touched only these aggregates.

    For every source table the payload reads, we hash ``(count, max(updated_at))``.
    All six models carry ``updated_at`` (``onupdate=datetime.utcnow``), so an INSERT
    bumps ``count`` and any in-place UPDATE/soft-delete bumps ``max(updated_at)`` --
    together they faithfully DOMINATE every payload field derived from those rows
    (counts, statuses, quantities, timestamps). ``Part`` is included because
    ``active_assignments`` surfaces ``part_number``/``part_name`` (dereferenced via the
    WO), so a part rename MUST move the ETag -- a stale floor display of a part identity
    is an AS9100D traceability hazard. ``today`` (UTC) is folded in for the
    ``due_today``/``overdue`` rollups; ``central_today`` is folded in SEPARATELY for
    ``completed_today``, which is a Central-Time rolling window that ages a completion
    OUT at Central midnight with NO row change -- and Central midnight is hours after the
    UTC date already rolled over, so UTC ``today`` alone would miss that boundary and
    serve a stale 304. ``presence`` mirrors the (company-scoped) websocket presence the
    payload's ``signed_in_users`` depends on, so the fingerprint moves when presence does
    even though it is not a DB row. Tenant-scoped via ``company_id`` on every aggregate.
    """
    from app.models.part import Part

    wo_count, wo_max = (
        db.query(func.count(WorkOrder.id), func.max(WorkOrder.updated_at))
        .filter(WorkOrder.company_id == company_id, WorkOrder.is_deleted == False)  # noqa: E712
        .one()
    )
    op_count, op_max = (
        db.query(func.count(WorkOrderOperation.id), func.max(WorkOrderOperation.updated_at))
        .filter(WorkOrderOperation.company_id == company_id)
        .one()
    )
    te_count, te_max = (
        db.query(func.count(TimeEntry.id), func.max(TimeEntry.updated_at))
        .filter(TimeEntry.company_id == company_id)
        .one()
    )
    wc_count, wc_max = (
        db.query(func.count(WorkCenter.id), func.max(WorkCenter.updated_at))
        .filter(WorkCenter.company_id == company_id)
        .one()
    )
    user_count, user_max = (
        db.query(func.count(User.id), func.max(User.updated_at)).filter(User.company_id == company_id).one()
    )
    part_count, part_max = (
        db.query(func.count(Part.id), func.max(Part.updated_at))
        .filter(Part.company_id == company_id, Part.is_deleted == False)  # noqa: E712
        .one()
    )
    fingerprint = {
        "today": date.today().isoformat(),
        "central_today": datetime.now(CENTRAL_TIME_ZONE).date().isoformat(),
        "work_orders": (int(wo_count or 0), wo_max.isoformat() if wo_max else None),
        "operations": (int(op_count or 0), op_max.isoformat() if op_max else None),
        "time_entries": (int(te_count or 0), te_max.isoformat() if te_max else None),
        "work_centers": (int(wc_count or 0), wc_max.isoformat() if wc_max else None),
        "users": (int(user_count or 0), user_max.isoformat() if user_max else None),
        "parts": (int(part_count or 0), part_max.isoformat() if part_max else None),
        "presence": sorted([user_id, connected_since_by_id.get(user_id)] for user_id in connected_user_ids),
    }
    return hashlib.md5(json.dumps(fingerprint, sort_keys=True, default=str).encode(), usedforsecurity=False).hexdigest()


@router.get("/dashboard")
def shop_floor_dashboard(
    response: Response,
    if_none_match: Optional[str] = Header(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """
    Get shop floor dashboard data with ETag support for conditional requests.

    Supports If-None-Match header for cache validation.
    Returns 304 Not Modified if data hasn't changed, saving bandwidth.

    PERF-2: the ETag is a CHEAP state fingerprint computed BEFORE the reconcile (see
    ``_dashboard_state_fingerprint``), so an unchanged dashboard returns 304 having
    paid only for the fingerprint aggregates -- it skips both the write-amplifying
    reconcile and the entire payload build. On a changed dashboard we run the reconcile
    + build the payload, then RECOMPUTE the fingerprint (the reconcile may have mutated
    rows / bumped ``updated_at``) so the next poll over the now-stable state 304s with
    no extra round-trip.

    PERF-3: the reconcile scan is bounded to the most-recently-touched
    ``SHOP_FLOOR_DASHBOARD_RECONCILE_LIMIT`` open WOs; anything beyond the cap is still
    reconciled when opened in its detail/operations-list views (the durable fix is the
    deferred ARQ reconcile job).

    OPTIMIZATION: Uses aggregation queries to avoid N+1 query problem.
    Before: 1 query for work centers + 2 queries per work center (N+1 pattern)
            For 25 work centers = 51 queries
    After:  3 queries total (work centers + aggregated operation counts + summary stats)
    """
    # PERF-2 + tenant isolation (invariant #1): capture websocket presence ONCE up front
    # and reuse it for BOTH the fingerprint and the served ``signed_in_users`` payload
    # below, so the returned ETag is always consistent with the body it describes. The
    # websocket manager's presence set is GLOBAL across tenants, so SCOPE it to this
    # company first -- otherwise (a) another tenant's connect/disconnect would churn this
    # dashboard's ETag (spurious 200s, defeating the 304), and (b) ``signed_in_users``
    # could surface another tenant's connected users (a pre-existing cross-tenant leak,
    # closed here).
    global_connected_ids = {int(uid) for uid in manager.get_connected_user_ids() if str(uid).isdigit()}
    connected_user_ids: set[int] = set()
    if global_connected_ids:
        connected_user_ids = {
            row[0]
            for row in db.query(User.id).filter(User.id.in_(global_connected_ids), User.company_id == company_id).all()
        }
    connected_since_by_id = {uid: manager.get_connected_since(str(uid)) for uid in connected_user_ids}

    # PERF-2: cheap pre-reconcile fingerprint -> fast 304 short-circuit. An unchanged
    # dashboard returns here WITHOUT running the reconcile or building the payload.
    etag = _dashboard_state_fingerprint(db, company_id, connected_user_ids, connected_since_by_id)
    if if_none_match and if_none_match.strip('"') == etag:
        return Response(status_code=304)

    # PERF-3: ONLY the dashboard reconcile is unbounded (the list reconcile is
    # page-bounded, detail is a single WO). Bound it to the most-recently-touched open
    # WOs -- those are the most likely to carry new completion evidence. Reconcile is
    # best-effort and idempotent, so any WO beyond the cap is still reconciled when
    # viewed in its detail/operations-list (both reconcile); nothing is permanently
    # stranded. The full fix is the deferred ARQ reconcile job.
    work_orders_to_reconcile = (
        db.query(WorkOrder)
        .options(selectinload(WorkOrder.operations))
        .filter(
            WorkOrder.company_id == company_id,
            WorkOrder.is_deleted == False,  # noqa: E712
            WorkOrder.status.in_([WorkOrderStatus.RELEASED, WorkOrderStatus.IN_PROGRESS, WorkOrderStatus.ON_HOLD]),
        )
        # PERF-3: id.desc() is a stable secondary tiebreak so two WOs with equal
        # updated_at don't swap across the cap boundary between polls.
        .order_by(WorkOrder.updated_at.desc(), WorkOrder.id.desc())
        .limit(settings.SHOP_FLOOR_DASHBOARD_RECONCILE_LIMIT)
        .all()
    )
    # PERF-3: no silent cap. When the scan fills the cap exactly, the open-WO set has
    # outgrown read-path reconcile -- warn so we know to switch to the deferred ARQ
    # reconcile job (rank 12 / PERF-3).
    if len(work_orders_to_reconcile) == settings.SHOP_FLOOR_DASHBOARD_RECONCILE_LIMIT:
        logger.warning(
            "Shop-floor dashboard reconcile truncated to the cap of %d open work orders for company %d; "
            "the shop has outgrown read-path reconcile -- switch to the deferred ARQ reconcile job "
            "(rank 12 / PERF-3).",
            settings.SHOP_FLOOR_DASHBOARD_RECONCILE_LIMIT,
            company_id,
        )
    # Reconcile-on-read: a concurrent-write conflict here is benign (idempotent),
    # so it must NOT 500 the dashboard -- _reconcile_and_commit swallows StaleDataError.
    # AUD-3: terminal reconcile-driven transitions are audited, attributed to the
    # requesting user, without failing the read if the audit write fails.
    _reconcile_and_commit(db, work_orders_to_reconcile, current_user, company_id)

    # PERF-2: compute the served ETag from the post-reconcile committed state HERE --
    # BEFORE building the payload -- so it describes the same snapshot the body is built
    # from. Computing it AFTER the build would open a TOCTOU window: a concurrent
    # same-tenant commit during the build would land in the ETag but not in the body, so
    # the client would store a newer ETag than its body and 304 (stale) on the next poll.
    # Pre-build is the SAFE direction -- a concurrent write merely makes the next poll's
    # fingerprint differ -> a 200 that re-serves the fresh body. The reconcile (the only
    # in-request mutator) has already committed, so this fingerprint is stable for the
    # next poll's fast-304 match.
    etag = _dashboard_state_fingerprint(db, company_id, connected_user_ids, connected_since_by_id)

    # Active work orders
    active_wos = (
        db.query(WorkOrder)
        .filter(WorkOrder.company_id == company_id, WorkOrder.status == WorkOrderStatus.IN_PROGRESS)
        .count()
    )

    # Work orders due today
    due_today = (
        db.query(WorkOrder)
        .filter(
            and_(
                WorkOrder.company_id == company_id,
                WorkOrder.due_date == date.today(),
                WorkOrder.status.not_in([WorkOrderStatus.COMPLETE, WorkOrderStatus.CLOSED]),
            )
        )
        .count()
    )

    # Overdue work orders
    overdue = (
        db.query(WorkOrder)
        .filter(
            and_(
                WorkOrder.company_id == company_id,
                WorkOrder.due_date < date.today(),
                WorkOrder.status.not_in([WorkOrderStatus.COMPLETE, WorkOrderStatus.CLOSED, WorkOrderStatus.CANCELLED]),
            )
        )
        .count()
    )

    # OPTIMIZATION: Single aggregation query for operation counts by work center
    # (conditional SUM/CASE, not N COUNT queries). Shared with the wallboard so
    # the two surfaces can't drift on what "active/queued" means (A0.5).
    op_counts_by_wc = operation_counts_by_work_center(db, company_id)

    # Get work centers (single query)
    work_centers = db.query(WorkCenter).filter(WorkCenter.company_id == company_id, WorkCenter.is_active == True).all()

    active_entries = (
        db.query(TimeEntry)
        .options(
            joinedload(TimeEntry.user),
            joinedload(TimeEntry.work_order).joinedload(WorkOrder.part),
            joinedload(TimeEntry.operation),
            joinedload(TimeEntry.work_center),
        )
        .filter(TimeEntry.company_id == company_id, TimeEntry.clock_out.is_(None))
        .all()
    )

    assignments_by_user: dict[int, list[dict]] = defaultdict(list)
    assignments_by_work_center: dict[int, list[dict]] = defaultdict(list)
    active_assignments: list[dict] = []

    for entry in active_entries:
        assignment = {
            "time_entry_id": entry.id,
            "clock_in": to_utc_iso(entry.clock_in),
            "entry_type": entry.entry_type.value if hasattr(entry.entry_type, "value") else entry.entry_type,
            "user": {
                "id": entry.user.id if entry.user else None,
                "employee_id": entry.user.employee_id if entry.user else None,
                "name": entry.user.full_name if entry.user else None,
                "role": (
                    entry.user.role.value
                    if entry.user and hasattr(entry.user.role, "value")
                    else (entry.user.role if entry.user else None)
                ),
                "department": entry.user.department if entry.user else None,
            },
            "work_order": {
                "id": entry.work_order.id if entry.work_order else None,
                "work_order_number": entry.work_order.work_order_number if entry.work_order else None,
                "status": (
                    entry.work_order.status.value
                    if entry.work_order and hasattr(entry.work_order.status, "value")
                    else (entry.work_order.status if entry.work_order else None)
                ),
                "part_number": (
                    entry.work_order.part.part_number if entry.work_order and entry.work_order.part else None
                ),
                "part_name": entry.work_order.part.name if entry.work_order and entry.work_order.part else None,
                "customer_name": entry.work_order.customer_name if entry.work_order else None,
                "priority": entry.work_order.priority if entry.work_order else None,
                "due_date": (
                    entry.work_order.due_date.isoformat() if entry.work_order and entry.work_order.due_date else None
                ),
                "quantity_ordered": entry.work_order.quantity_ordered if entry.work_order else None,
                "quantity_complete": entry.work_order.quantity_complete if entry.work_order else None,
            },
            "operation": {
                "id": entry.operation.id if entry.operation else None,
                "operation_number": entry.operation.operation_number if entry.operation else None,
                "name": entry.operation.name if entry.operation else None,
                "status": (
                    entry.operation.status.value
                    if entry.operation and hasattr(entry.operation.status, "value")
                    else (entry.operation.status if entry.operation else None)
                ),
                "sequence": entry.operation.sequence if entry.operation else None,
                "quantity_complete": entry.operation.quantity_complete if entry.operation else None,
                "quantity_scrapped": entry.operation.quantity_scrapped if entry.operation else None,
            },
            "work_center": {
                "id": entry.work_center.id if entry.work_center else None,
                "code": entry.work_center.code if entry.work_center else None,
                "name": entry.work_center.name if entry.work_center else None,
                "status": entry.work_center.current_status if entry.work_center else None,
                "type": (
                    entry.work_center.work_center_type.value
                    if entry.work_center and hasattr(entry.work_center.work_center_type, "value")
                    else (entry.work_center.work_center_type if entry.work_center else None)
                ),
            },
        }
        active_assignments.append(assignment)
        if entry.user_id:
            assignments_by_user[entry.user_id].append(assignment)
        if entry.work_center_id:
            assignments_by_work_center[entry.work_center_id].append(assignment)

    # Build response using pre-computed counts
    wc_status = []
    for wc in work_centers:
        counts = op_counts_by_wc.get(wc.id, {'active': 0, 'queued': 0})
        active_people = assignments_by_work_center.get(wc.id, [])
        wc_status.append(
            {
                "id": wc.id,
                "code": wc.code,
                "name": wc.name,
                "type": wc.work_center_type.value if hasattr(wc.work_center_type, 'value') else wc.work_center_type,
                "status": wc.current_status,
                "active_operations": counts['active'],
                "queued_operations": counts['queued'],
                "active_people_count": len(active_people),
                "active_people": [
                    {
                        "user_id": assignment["user"]["id"],
                        "name": assignment["user"]["name"],
                        "employee_id": assignment["user"]["employee_id"],
                        "work_order_number": assignment["work_order"]["work_order_number"],
                        "operation_name": assignment["operation"]["name"],
                        "clock_in": assignment["clock_in"],
                    }
                    for assignment in active_people
                ],
            }
        )

    # PERF-2: reuse the presence captured at the top of the handler (NOT a fresh
    # manager.get_* call) so the served payload matches the fingerprint/ETag exactly.
    # connected_user_ids is already company-scoped above; the company_id filter here is
    # belt-and-suspenders tenant isolation (invariant #1).
    signed_in_users: list[dict] = []
    if connected_user_ids:
        connected_users = db.query(User).filter(User.id.in_(connected_user_ids), User.company_id == company_id).all()
        signed_in_users = [
            {
                "id": user.id,
                "employee_id": user.employee_id,
                "name": user.full_name,
                "role": user.role.value if hasattr(user.role, "value") else user.role,
                "department": user.department,
                "connected_since": connected_since_by_id.get(user.id),
                "has_active_job": bool(assignments_by_user.get(user.id)),
                "active_job_count": len(assignments_by_user.get(user.id, [])),
                "active_work_centers": sorted(
                    {
                        assignment["work_center"]["name"]
                        for assignment in assignments_by_user.get(user.id, [])
                        if assignment["work_center"]["name"]
                    }
                ),
                "active_work_orders": sorted(
                    {
                        assignment["work_order"]["work_order_number"]
                        for assignment in assignments_by_user.get(user.id, [])
                        if assignment["work_order"]["work_order_number"]
                    }
                ),
            }
            for user in connected_users
        ]
        signed_in_users.sort(key=lambda user: (not user["has_active_job"], user["name"] or ""))

    # Recent operation completions. Work orders can remain open while operators
    # finish individual ops, so use operation completions for the live feed.
    recent_operations = (
        db.query(WorkOrderOperation)
        .options(
            joinedload(WorkOrderOperation.work_order),
            joinedload(WorkOrderOperation.work_center),
        )
        .filter(
            WorkOrderOperation.company_id == company_id,
            WorkOrderOperation.status == OperationStatus.COMPLETE,
            WorkOrderOperation.actual_end.isnot(None),
        )
        .order_by(WorkOrderOperation.actual_end.desc())
        .limit(10)
        .all()
    )
    completed_user_ids = {op.completed_by for op in recent_operations if op.completed_by}
    completed_users_by_id = {}
    if completed_user_ids:
        completed_users_by_id = {
            user.id: user
            for user in db.query(User)
            .filter(
                User.company_id == company_id,
                User.id.in_(completed_user_ids),
            )
            .all()
        }

    checked_in_user_ids = {entry.user_id for entry in active_entries if entry.user_id}
    central_now = datetime.now(CENTRAL_TIME_ZONE)
    central_day_start = central_now.replace(hour=0, minute=0, second=0, microsecond=0)
    completed_today_start = central_day_start.astimezone(timezone.utc).replace(tzinfo=None)
    completed_today_end = central_now.astimezone(timezone.utc).replace(tzinfo=None)
    completed_today = (
        db.query(WorkOrderOperation)
        .filter(
            WorkOrderOperation.company_id == company_id,
            WorkOrderOperation.status == OperationStatus.COMPLETE,
            WorkOrderOperation.actual_end >= completed_today_start,
            WorkOrderOperation.actual_end <= completed_today_end,
        )
        .count()
    )

    data = {
        "summary": {
            "active_work_orders": active_wos,
            "due_today": due_today,
            "overdue": overdue,
            "signed_in_users": len(signed_in_users),
            "checked_in_users": len(checked_in_user_ids),
            "idle_signed_in_users": max(len(signed_in_users) - len(checked_in_user_ids), 0),
            "completed_today": completed_today,
        },
        "work_centers": wc_status,
        "signed_in_users": signed_in_users,
        "active_assignments": active_assignments,
        "recent_completions": [
            {
                "work_order_number": op.work_order.work_order_number if op.work_order else None,
                "operation_name": op.name,
                "work_center_name": op.work_center.name if op.work_center else None,
                "operator_name": (
                    completed_users_by_id[op.completed_by].full_name
                    if op.completed_by in completed_users_by_id
                    else None
                ),
                "completed_at": to_utc_iso(op.actual_end),
                "quantity_complete": op.quantity_complete,
            }
            for op in recent_operations
        ],
    }

    # PERF-2: serve the ETag computed from the post-reconcile snapshot above (the same
    # snapshot the body was built from). The fast-304 short-circuit already happened
    # before the reconcile/payload build.
    response.headers["ETag"] = f'"{etag}"'
    response.headers["Cache-Control"] = "private, max-age=10"

    return data


@router.get("/wallboard", response_model=WallboardResponse)
def shop_floor_wallboard(
    dept: Optional[str] = Query(
        None,
        max_length=50,
        description=(
            "Scope to one work-center type (case-insensitive): filters the work centers "
            "AND the late/blocked lists + totals; ship/today/quality/kpi_strip stay plant-wide"
        ),
    ),
    db: Session = Depends(get_db),
    principal: WallboardPrincipal = Depends(get_display_or_user),
):
    """Read-only TV wallboard snapshot (A0.5).

    AUTH: accepts a normal user token OR a scoped display token — this is the
    ONLY endpoint display tokens can authenticate (``get_display_or_user``);
    everywhere else they 401 via ``verify_token``'s type check.

    Tenant-scoped to the principal's company (user's active company, or the
    display token's ``display_tokens.company_id`` — never client input).

    DELIBERATELY side-effect free: no reconcile-on-read, no audit rows, no
    events — an unattended TV polling every 30s must never mutate state, and
    a display token has no user identity to attribute writes to.
    """
    return build_wallboard_payload(db, principal.company_id, dept=dept)


@router.get("/active-users")
def get_active_shop_users(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get list of users currently clocked in"""
    active_entries = (
        db.query(TimeEntry)
        .options(
            joinedload(TimeEntry.user),
            joinedload(TimeEntry.work_order),
            joinedload(TimeEntry.operation),
            joinedload(TimeEntry.work_center),
        )
        .filter(TimeEntry.company_id == company_id, TimeEntry.clock_out.is_(None))
        .all()
    )

    users = []
    for entry in active_entries:
        users.append(
            {
                "user_id": entry.user_id,
                "user_name": entry.user.full_name if entry.user else None,
                "work_order_number": entry.work_order.work_order_number if entry.work_order else None,
                "operation": entry.operation.name if entry.operation else None,
                "work_center": entry.work_center.name if entry.work_center else None,
                "clock_in": to_utc_iso(entry.clock_in),
                "entry_type": entry.entry_type,
            }
        )

    return {"active_users": users}


# ============ SIMPLIFIED OPERATION WORKFLOW ============


@router.get("/operations")
def get_all_operations(
    work_center_id: Optional[int] = Query(None, description="Filter by work center"),
    status: Optional[str] = Query(None, description="Filter by status: pending, ready, in_progress, complete, on_hold"),
    search: Optional[str] = Query(None, description="Search by WO number or part number"),
    due_today: bool = Query(False, description="Filter operations due today"),
    page: int = Query(1, ge=1, description="Page number (1-indexed)"),
    page_size: int = Query(50, ge=1, le=200, description="Items per page (max 200)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """
    Get operations with filters and pagination for the shop floor view.

    Returns paginated operations that are not complete or cancelled.
    Default: 50 items per page, max 200.

    Response includes pagination metadata for building UI controls.
    """
    from app.core.pagination import paginate_query

    query = (
        db.query(WorkOrderOperation)
        .options(
            joinedload(WorkOrderOperation.work_order).joinedload(WorkOrder.part),
            joinedload(WorkOrderOperation.work_center),
            selectinload(WorkOrderOperation.laser_nest).selectinload(LaserNest.document),
        )
        .join(WorkOrder)
    )

    # Scope to company and exclude completed/cancelled and soft-deleted work orders
    query = query.filter(
        WorkOrder.company_id == company_id,
        WorkOrder.is_deleted == False,  # noqa: E712
        WorkOrder.status.not_in([WorkOrderStatus.COMPLETE, WorkOrderStatus.CLOSED, WorkOrderStatus.CANCELLED]),
    )

    # Filter by work center
    if work_center_id:
        query = query.filter(WorkOrderOperation.work_center_id == work_center_id)

    # Filter by operation status
    if status:
        try:
            op_status = OperationStatus(status)
            query = query.filter(WorkOrderOperation.status == op_status)
        except ValueError:
            pass  # Invalid status, ignore filter
    else:
        # Default: exclude completed operations
        query = query.filter(WorkOrderOperation.status != OperationStatus.COMPLETE)

    # Search by WO number or part number
    if search:
        search_term = f"%{search}%"
        from app.models.part import Part

        query = query.join(Part, WorkOrder.part_id == Part.id).filter(
            or_(WorkOrder.work_order_number.ilike(search_term), Part.part_number.ilike(search_term))
        )

    if due_today:
        query = query.filter(WorkOrder.due_date == date.today())

    # Order by priority, then due date
    query = query.order_by(WorkOrder.priority, WorkOrder.due_date, WorkOrderOperation.sequence)

    # Apply pagination
    paginated_query, pagination_meta = paginate_query(query, page, page_size)
    operations = paginated_query.all()
    work_orders_by_id = {op.work_order.id: op.work_order for op in operations if op.work_order}
    work_orders = list(work_orders_by_id.values())
    # Reconcile-on-read: a concurrent-write conflict here is benign (idempotent),
    # so it must NOT 500 the list -- _reconcile_and_commit swallows StaleDataError.
    # AUD-3: terminal reconcile-driven transitions are audited to the requesting user.
    _reconcile_and_commit(db, work_orders, current_user, company_id)
    if not status:
        operations = [op for op in operations if op.status != OperationStatus.COMPLETE]

    # Build response data
    result = []
    for op in operations:
        wo = op.work_order
        wc = op.work_center
        target_qty = operation_target_quantity(op, wo)
        check_in_state = _operation_check_in_state(db, op)
        result.append(
            {
                "id": op.id,
                "work_order_id": wo.id,
                "work_order_number": wo.work_order_number,
                "part_number": wo.part.part_number if wo.part else None,
                "part_name": wo.part.name if wo.part else None,
                "operation_number": op.operation_number,
                "operation_name": op.name,
                "description": op.description,
                "work_center_id": wc.id if wc else None,
                "work_center_name": wc.name if wc else None,
                "status": op.status.value,
                "quantity_ordered": target_qty,
                "work_order_quantity_ordered": wo.quantity_ordered,
                "component_quantity": op.component_quantity,
                "quantity_complete": op.quantity_complete,
                "quantity_scrapped": op.quantity_scrapped,
                "laser_nest": _laser_nest_payload(op),
                "priority": wo.priority,
                "due_date": wo.due_date.isoformat() if wo.due_date else None,
                "customer_name": wo.customer_name,
                "customer_po": wo.customer_po,
                "actual_start": to_utc_iso(op.actual_start),
                "setup_instructions": op.setup_instructions,
                "run_instructions": op.run_instructions,
                "requires_inspection": op.requires_inspection,
                **check_in_state,
            }
        )

    return {
        "operations": result,
        "total": pagination_meta.total_count,  # Backward compatibility
        "pagination": {
            "page": pagination_meta.page,
            "page_size": pagination_meta.page_size,
            "total_count": pagination_meta.total_count,
            "total_pages": pagination_meta.total_pages,
            "has_next": pagination_meta.has_next,
            "has_previous": pagination_meta.has_previous,
        },
    }


@router.put("/operations/{operation_id}/start")
def start_operation(
    operation_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """
    Mark operation as in progress and create a time entry.
    - Sets status to IN_PROGRESS
    - Records actual_start_time
    - Creates a TimeEntry so the dashboard shows the operator on this job
    - Updates work order status if needed
    """
    operation = (
        db.query(WorkOrderOperation)
        .options(joinedload(WorkOrderOperation.work_order).joinedload(WorkOrder.part))
        .filter(
            WorkOrderOperation.id == operation_id,
            WorkOrderOperation.company_id == company_id,
        )
        .first()
    )

    if not operation:
        raise HTTPException(status_code=404, detail="Operation not found")

    # Validate operation can be started
    if operation.status == OperationStatus.COMPLETE:
        raise HTTPException(status_code=400, detail="Operation is already complete")

    if operation.status == OperationStatus.IN_PROGRESS:
        raise HTTPException(status_code=400, detail="Operation is already in progress")

    work_order = operation.work_order
    if not work_order:
        raise HTTPException(status_code=404, detail="Work order not found")

    # G6-A: refuse to START new work on a TERMINAL parent WO (CANCELLED/CLOSED/COMPLETE)
    # before any mutation -- mirrors the guard in complete_operation. You can never
    # legitimately begin a new operation on a finished/cancelled job, and refusing it
    # traps nothing (no open time entry is created yet).
    if work_order.status in TERMINAL_WO_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"cannot start operation: work order is {work_order.status.value}",
        )

    if has_incomplete_predecessors(
        db,
        operation.work_order_id,
        operation.sequence,
        operation.id,
        operation.work_center_id,
        allow_same_work_center=True,
    ):
        raise HTTPException(status_code=400, detail="Previous operations must be completed first")

    # Update operation
    operation.status = OperationStatus.IN_PROGRESS
    operation.actual_start = datetime.utcnow()
    operation.started_by = current_user.id
    operation.updated_at = datetime.utcnow()

    # Update work order status if needed
    if work_order.status in [WorkOrderStatus.DRAFT, WorkOrderStatus.RELEASED]:
        work_order.status = WorkOrderStatus.IN_PROGRESS
        if not work_order.actual_start:
            work_order.actual_start = datetime.utcnow()

    # Create a time entry so the dashboard shows this operator on this job
    existing_entry = (
        db.query(TimeEntry)
        .filter(
            and_(
                TimeEntry.user_id == current_user.id,
                TimeEntry.operation_id == operation_id,
                TimeEntry.clock_out.is_(None),
                TimeEntry.company_id == company_id,
            )
        )
        .first()
    )

    time_entry = None
    if not existing_entry:
        time_entry = TimeEntry(
            user_id=current_user.id,
            work_order_id=work_order.id,
            operation_id=operation_id,
            work_center_id=operation.work_center_id,
            entry_type=TimeEntryType.RUN,
            clock_in=datetime.utcnow(),
            company_id=company_id,
        )
        db.add(time_entry)

    # Create audit log
    audit.log(
        action="START_OPERATION",
        resource_type="work_order_operation",
        resource_id=operation_id,
        description=f"Started operation {operation.operation_number} on WO {work_order.work_order_number}",
    )

    # G5-B: warn-and-record operator-qualification gate. Validation passed and the
    # TimeEntry (if any) is in this unit of work; evaluate the (read-only,
    # tenant-scoped) skill + certification gates and, for each unsatisfied one, leave a
    # tamper-evident audit row + a warning OperationalEvent that commit ATOMICALLY with
    # this start below. NEVER blocks the start.
    qualification_exceptions = evaluate_and_record_operator_qualification(
        db,
        company_id=company_id,
        user=current_user,
        operation=operation,
        work_center_id=operation.work_center_id,
        audit=audit,
        source="start_operation",
    )

    try:
        db.commit()
    except IntegrityError as exc:
        # The existing_entry pre-check handles the common case, but a concurrent
        # start/clock-in for the same (user, operation) can race past it. The
        # partial unique index uq_open_time_entry rejects the duplicate open row
        # at the DB; surface a clean 400 for ONLY that violation. Any OTHER
        # integrity failure must not be mislabeled -- roll back and re-raise it.
        db.rollback()
        if _is_open_time_entry_violation(exc):
            raise HTTPException(
                status_code=400,
                detail="This operation has already been started by you.",
            ) from exc
        raise
    except StaleDataError as exc:
        # A concurrent writer bumped the operation/WO version between read and
        # commit (version_id_col mismatch). Surface a clean 409, not a 500.
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="This operation was modified concurrently. Refresh and retry.",
        ) from exc
    db.refresh(operation)

    if operation.work_center_id:
        safe_broadcast(
            broadcast_shop_floor_update,
            operation.work_center_id,
            {
                "event": "operation_started",
                "work_order_id": work_order.id,
                "operation_id": operation.id,
            },
            company_id=company_id,
        )
    safe_broadcast(
        broadcast_work_order_update,
        work_order.id,
        {
            "event": "operation_started",
            "operation_id": operation.id,
            "status": work_order.status.value if hasattr(work_order.status, "value") else work_order.status,
        },
        company_id=company_id,
    )
    safe_broadcast(
        broadcast_dashboard_update,
        {
            "event": "operation_started",
            "work_order_id": work_order.id,
            "operation_id": operation.id,
        },
        company_id=company_id,
    )

    return {
        "message": "Operation started successfully",
        "operation": {
            "id": operation.id,
            "status": operation.status.value,
            "actual_start": to_utc_iso(operation.actual_start),
        },
        # G5-B: surface any unsatisfied qualification gate (warn-only; defaults to []).
        "qualification_exceptions": [exc.as_dict() for exc in qualification_exceptions],
    }


@router.post("/operations/{operation_id}/production")
def report_operation_production(
    operation_id: int,
    production_data: ProductionReportRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """
    Add produced/scrapped quantity while keeping the operator clocked in.

    This is intentionally separate from checkout: it updates progress and the
    active time entry production counters, but does not close time or complete
    the operation automatically when the target quantity is reached.
    """
    operation = (
        db.query(WorkOrderOperation)
        .options(joinedload(WorkOrderOperation.work_order).joinedload(WorkOrder.part))
        .filter(
            WorkOrderOperation.id == operation_id,
            WorkOrderOperation.company_id == company_id,
        )
        .first()
    )

    if not operation:
        raise HTTPException(status_code=404, detail="Operation not found")

    work_order = operation.work_order
    if not work_order:
        raise HTTPException(status_code=404, detail="Work order not found for this operation")

    if operation.status != OperationStatus.IN_PROGRESS:
        raise HTTPException(status_code=400, detail="Operation must be in progress to add completed quantity")

    active_entry = (
        db.query(TimeEntry)
        .filter(
            and_(
                TimeEntry.user_id == current_user.id,
                TimeEntry.operation_id == operation_id,
                TimeEntry.clock_out.is_(None),
                TimeEntry.company_id == company_id,
            )
        )
        .first()
    )
    if not active_entry:
        raise HTTPException(status_code=400, detail="You must be clocked in to add completed quantity")

    good_delta = production_data.quantity_complete_delta
    scrap_delta = production_data.quantity_scrapped_delta
    if math.isnan(good_delta) or math.isinf(good_delta) or math.isnan(scrap_delta) or math.isinf(scrap_delta):
        raise HTTPException(status_code=400, detail="Quantity must be a valid number")
    if good_delta < 0 or scrap_delta < 0:
        raise HTTPException(status_code=400, detail="Quantity cannot be negative")
    if good_delta == 0 and scrap_delta == 0:
        raise HTTPException(status_code=400, detail="Enter a completed or scrap quantity")

    # A0.1 adoption-telemetry channel (kiosk-token forcing + import guard) resolved
    # before any mutation so a disallowed 'import' 422s without touching the entry.
    recorded_source = _resolve_labor_source(current_user, production_data.source)

    # Lean Phase 1: resolve the structured scrap reason code BEFORE any mutation
    # (404 unknown/cross-tenant, 422 inactive). None passes through untouched.
    scrap_code = resolve_scrap_reason_code_or_http(db, company_id, production_data.scrap_reason_code_id)

    # SFI-1: lock the operation row before the over-completion read-modify-write
    # so concurrent producers serialize on quantity_complete instead of losing
    # updates. Re-read the freshest committed quantity off the locked row rather
    # than reusing the stale in-session value for the guard.
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

    # SFI-1: ALSO re-fetch the parent WO under a row lock (consistent lock order:
    # OPERATION first, then WORK ORDER -- same as complete_operation). The rollup
    # write below sets work_order.quantity_complete; two producers on different
    # operations of the same WO would otherwise race last-writer-wins on that
    # column. Locking the parent serializes the rollup. Tenant-scoped and
    # soft-delete-aware, against the freshest committed row.
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

    target_qty = operation_target_quantity(operation, work_order)
    next_complete_qty = float(operation.quantity_complete or 0) + good_delta
    if target_qty > 0 and next_complete_qty > target_qty:
        raise HTTPException(
            status_code=400, detail=f"Quantity ({next_complete_qty}) cannot exceed quantity ordered ({target_qty})"
        )

    # /production is an ADDITIVE verb: floor the incremented total at durable
    # TimeEntry evidence and cap at target so additive and absolute writes converge
    # on the same invariant (DUP-3 / SFI-5). The over-completion guard above already
    # rejected a delta that would exceed target.
    operation.quantity_complete = floor_operation_quantity_at_evidence(db, operation, next_complete_qty, target_qty)
    operation.quantity_scrapped = float(operation.quantity_scrapped or 0) + scrap_delta
    # Lean Phase 1: categorize the operation's scrap when THIS report carries both
    # scrap and a code (a code-less report never clears a recorded one).
    if scrap_code is not None and scrap_delta > 0:
        operation.scrap_reason_code_id = scrap_code.id
    # Lean Phase 1 (FPY): produced quantity reported while clocked into a REWORK
    # entry is re-processed work -- track it for first-pass yield.
    if active_entry.entry_type == TimeEntryType.REWORK and good_delta > 0:
        operation.quantity_reworked = float(operation.quantity_reworked or 0) + good_delta
    operation.updated_at = datetime.utcnow()
    sync_laser_nest_from_operation(operation)

    active_entry.quantity_produced = float(active_entry.quantity_produced or 0) + good_delta
    active_entry.quantity_scrapped = float(active_entry.quantity_scrapped or 0) + scrap_delta
    if production_data.notes:
        active_entry.notes = (
            f"{active_entry.notes}\n{production_data.notes}" if active_entry.notes else production_data.notes
        )
    # A0.3: structured scrap reason -- persisted onto the active entry like clock-out's,
    # but only when this report actually carries scrap; an omitted/None reason never
    # clobbers a reason recorded by an earlier in-shift report.
    scrap_reason = production_data.scrap_reason if (production_data.scrap_reason and scrap_delta > 0) else None
    if scrap_reason:
        active_entry.scrap_reason = scrap_reason
    # Lean Phase 1: same semantics for the structured code.
    if scrap_code is not None and scrap_delta > 0:
        active_entry.scrap_reason_code_id = scrap_code.id
    # A0.1 adoption telemetry: record the reporting channel when this write carries one
    # (a kiosk-scoped token always resolves to KIOSK); omitted on a normal session ->
    # keep whatever channel the entry already carries (never guessed).
    if recorded_source:
        active_entry.source = recorded_source
    active_entry.updated_at = datetime.utcnow()

    sync_work_order_quantity_complete(work_order, operation, all_operations_complete=False)
    work_order.updated_at = datetime.utcnow()

    AuditService(db, current_user).log(
        action="REPORT_OPERATION_PRODUCTION",
        resource_type="work_order_operation",
        resource_id=operation_id,
        description=(
            f"Reported production on operation {operation.operation_number} for WO {work_order.work_order_number}. "
            f"Added good: {good_delta}, scrap: {scrap_delta}. "
            f"Qty: {operation.quantity_complete}/{target_qty}"
            + (f". Scrap reason: {scrap_reason}" if scrap_reason else "")
            + (f". Scrap reason code: {scrap_code.code}" if (scrap_code and scrap_delta > 0) else "")
            + (f". Notes: {production_data.notes}" if production_data.notes else "")
        ),
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
    db.refresh(active_entry)

    if operation.work_center_id:
        safe_broadcast(
            broadcast_shop_floor_update,
            operation.work_center_id,
            {
                "event": "operation_production_reported",
                "work_order_id": work_order.id,
                "operation_id": operation.id,
                "quantity_complete": operation.quantity_complete,
                "quantity_scrapped": operation.quantity_scrapped,
            },
            company_id=company_id,
        )
    safe_broadcast(
        broadcast_work_order_update,
        work_order.id,
        {
            "event": "operation_production_reported",
            "operation_id": operation.id,
        },
        company_id=company_id,
    )
    safe_broadcast(
        broadcast_dashboard_update,
        {
            "event": "operation_production_reported",
            "work_order_id": work_order.id,
            "operation_id": operation.id,
        },
        company_id=company_id,
    )

    return {
        "message": "Production quantity added",
        "operation": {
            "id": operation.id,
            "status": operation.status.value,
            "quantity_complete": operation.quantity_complete,
            "quantity_scrapped": operation.quantity_scrapped,
            "quantity_ordered": target_qty,
        },
        "active_time_entry": {
            "id": active_entry.id,
            "quantity_produced": active_entry.quantity_produced,
            "quantity_scrapped": active_entry.quantity_scrapped,
            "clock_out": to_utc_iso(active_entry.clock_out),
        },
    }


@router.post("/operations/{operation_id}/complete")
def complete_operation(
    operation_id: int,
    completion_data: OperationCompleteRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """
    Mark operation as complete (full or partial).
    - Updates quantity_complete (max-guarded, floored at durable TimeEntry
      evidence and capped at the operation target via the shared finalizer)
    - If qty_complete >= qty_ordered: status = COMPLETE, record actual_end_time
    - If qty_complete < qty_ordered: status remains IN_PROGRESS
    - Optionally record notes
    - Auto-closes every open TimeEntry on the operation (all clocked-in
      operators) and names them in the response's ``closed_time_entries``
      (time_entry_id / user_id / operator_name) so the crew kiosk can say
      who was clocked out

    ON_HOLD policy (QG-5 / BLK-1): completing an ON_HOLD operation is REFUSED
    with **409 Conflict** ("Operation is on hold and cannot be completed"),
    matching the office `/work-orders/operations/{id}/complete` twin -- so the
    completion path can no longer silently lift a quality / material hold.
    """
    operation = (
        db.query(WorkOrderOperation)
        .options(joinedload(WorkOrderOperation.work_order).joinedload(WorkOrder.part))
        .filter(
            WorkOrderOperation.id == operation_id,
            WorkOrderOperation.company_id == company_id,
        )
        .first()
    )

    if not operation:
        raise HTTPException(status_code=404, detail="Operation not found")

    work_order = operation.work_order
    if not work_order:
        raise HTTPException(status_code=404, detail="Work order not found for this operation")

    # SFI-1: serialize concurrent completers. Re-fetch the operation and its
    # parent work order under SELECT ... FOR UPDATE (consistent lock order:
    # OPERATION first, then WORK ORDER) so the over-completion guard AND the
    # "remaining ops == 0 -> WO COMPLETE" decision below are evaluated against
    # the freshest committed rows, not a stale in-session snapshot. Both
    # re-fetches stay scoped to the active company.
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

    # G6-A: refuse to complete an operation against a TERMINAL parent WO
    # (CANCELLED/CLOSED/COMPLETE) before any mutation -- mirrors the ON_HOLD 409 this
    # handler already enforces. Without this, finalizing the last op of a CANCELLED WO
    # would resurrect it to COMPLETE via the shared finalizer and re-fire FG
    # receipt/backflush/cost rollup plus a COMPLETE audit row.
    if work_order.status in TERMINAL_WO_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"cannot complete operation: work order is {work_order.status.value}",
        )

    # Validate operation state (re-checked under the lock so a concurrent
    # completer that already flipped this op to COMPLETE is rejected here).
    if operation.status == OperationStatus.COMPLETE:
        raise HTTPException(status_code=400, detail="Operation is already complete")

    # QG-5 / BLK-1: refuse to complete an ON_HOLD (or otherwise non-startable)
    # operation -- identical to the office twin. An ON_HOLD op is a STATE conflict
    # (409), not bad input (400): completing it would silently lift a quality /
    # material hold. Any other non-startable status stays a 400.
    if operation.status not in [OperationStatus.IN_PROGRESS, OperationStatus.READY]:
        if operation.status == OperationStatus.ON_HOLD:
            raise HTTPException(status_code=409, detail="Operation is on hold and cannot be completed")
        raise HTTPException(status_code=400, detail=f"Cannot complete operation with status: {operation.status.value}")

    ordered_qty = operation_target_quantity(operation, work_order)
    try:
        validate_operation_quantity(completion_data.quantity_complete, ordered_qty)
    except WorkOrderStateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if has_incomplete_predecessors(
        db,
        operation.work_order_id,
        operation.sequence,
        operation.id,
        operation.work_center_id,
        allow_same_work_center=True,
    ):
        raise HTTPException(status_code=400, detail="Previous operations must be completed first")

    # A0.1 adoption-telemetry channel of THIS completing write (kiosk-token forcing +
    # import guard). Resolved before any mutation so a disallowed 'import' 422s without
    # advancing the operation or auto-closing any open entry.
    recorded_source = _resolve_labor_source(current_user, completion_data.source)

    # Process-sheet completion gate (PR 3): when THIS request would FULLY complete the
    # operation, every required (non-INSTRUCTION) snapshot step must carry a live
    # (non-superseded) conforming record — per serial on a serialized WO. Evaluated
    # under the row lock against the RESOLVED quantity, so a concurrent completer
    # re-runs the same check on the freshest rows. Partial progress updates and
    # operations with no snapshot steps are unaffected (zero-step operations complete
    # exactly as before — regression-sensitive). Non-optimistic by design.
    #
    # PR 4 (ledger): resolved ONCE under the lock and reused for both the gate and the
    # store below — gating and storing can no longer diverge (TOCTOU closed), and the
    # duplicate evidence query is gone.
    resolved_quantity = resolve_absolute_operation_quantity(
        db, operation, completion_data.quantity_complete, ordered_qty
    )
    if resolved_quantity >= ordered_qty:
        missing_steps = process_sheet_service.missing_required_steps(db, company_id, operation, work_order)
        if missing_steps:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "STEPS_INCOMPLETE",
                    "detail": "Required process-sheet steps are missing conforming records for this operation",
                    "missing": missing_steps,
                },
            )

    # Auto-start if not already in progress
    if operation.status != OperationStatus.IN_PROGRESS:
        operation.status = OperationStatus.IN_PROGRESS
        if not operation.actual_start:
            operation.actual_start = datetime.utcnow()
            operation.started_by = current_user.id

    previous_quantity_complete = float(operation.quantity_complete or 0)

    # Update quantity. /complete is an ABSOLUTE verb: clamp to
    # max(existing, requested, produced-evidence) capped at target so it can never
    # regress below durable TimeEntry evidence (SFI-5) and a later read-time
    # reconcile cannot silently re-raise and mask the write. ``resolved_quantity``
    # was computed ONCE above (PR 4) — the gate and this store see the same value.
    operation.quantity_complete = resolved_quantity
    operation.updated_at = datetime.utcnow()
    sync_laser_nest_from_operation(operation)

    # Check if fully complete (against the resolved, evidence-floored quantity).
    is_fully_complete = resolved_quantity >= ordered_qty
    work_order_completed = False
    # PERF-5: tracks whether the scheduling refresh ran (it runs with commit=False,
    # so the WC cache must be invalidated by us after the terminal commit succeeds).
    work_centers_refreshed = False

    if is_fully_complete:
        operation.status = OperationStatus.COMPLETE
        operation.actual_end = datetime.utcnow()
        operation.completed_by = current_user.id

        # Shared finalizer owns the rollup (DUP-5): remaining-ops decision,
        # COMPLETE-vs-release, actual_start/actual_end stamping, qty sync,
        # current_operation_id. Returns the WCs whose capacity to refresh.
        affected_work_centers = finalize_operation_completion(db, work_order, operation)
        work_order_completed = work_order.status == WorkOrderStatus.COMPLETE
        # PERF-5: commit=False joins this scheduling refresh into the handler's single
        # unit of work, so the WO/op state change is committed atomically with the
        # audit rows / cost rollup / quality exceptions written below (the old default
        # commit=True committed the state change mid-handler -- a crash before the
        # terminal commit left a completed WO with no audit). commit=False skips the
        # in-service WC cache invalidation, so we do it after the terminal commit.
        SchedulingService(db, company_id).update_availability_rates(
            work_center_ids=[wc_id for wc_id in affected_work_centers if wc_id],
            horizon_days=90,
            commit=False,
        )
        work_centers_refreshed = True
    else:
        # Partial completion: lift a RELEASED WO to IN_PROGRESS / stamp actual_start
        # and roll partial qty up without forcing a completion rollup.
        begin_operation_progress(work_order, operation)
        sync_work_order_quantity_complete(work_order, operation, all_operations_complete=False)
    work_order.updated_at = datetime.utcnow()

    # Close any open time entries for this operation when fully complete
    closed_time_entries: list[dict] = []
    if is_fully_complete:
        open_entries = (
            db.query(TimeEntry)
            .options(joinedload(TimeEntry.user))
            .filter(
                and_(
                    TimeEntry.operation_id == operation_id,
                    TimeEntry.company_id == company_id,
                    TimeEntry.clock_out.is_(None),
                )
            )
            .order_by((TimeEntry.user_id == current_user.id).desc(), TimeEntry.clock_in.asc())
            .all()
        )
        now = datetime.utcnow()
        # Credit the resolved (evidence-floored) quantity, not the raw request, so
        # the closed TimeEntry total and the operation total stay consistent.
        completion_delta = max(0.0, resolved_quantity - previous_quantity_complete)
        for entry in open_entries:
            entry.clock_out = now
            if entry.clock_in:
                entry.duration_hours = (now - entry.clock_in).total_seconds() / 3600.0
            # A0.1 adoption telemetry: this completion auto-closes OTHER operators'
            # open entries too, so only FILL a missing channel -- never overwrite an
            # entry's own recorded clock-in channel with the completer's channel.
            if recorded_source and entry.source is None:
                entry.source = recorded_source
        if open_entries and completion_delta > 0:
            open_entries[0].quantity_produced = float(open_entries[0].quantity_produced or 0) + completion_delta
        # Crew-station kiosk: surface WHO this completion auto-clocked-out so the
        # client can toast it ("also clocked out: Bob T, Charlie M"). Captured
        # BEFORE the terminal commit while the rows are loaded; read-only —
        # the auto-close mutation above is unchanged.
        closed_time_entries = [
            {
                "time_entry_id": entry.id,
                "user_id": entry.user_id,
                "operator_name": (
                    operator_display_name(entry.user.first_name, entry.user.last_name) if entry.user else None
                ),
            }
            for entry in open_entries
        ]

        # COST-3 (Batch 7, OPT-IN): this path auto-closes the open TimeEntries above by
        # writing clock_out + duration_hours, but historically dropped those hours from
        # the rollups (data loss vs. an explicit clock-out). When the rollup flag is ON,
        # accumulate each just-closed entry's duration into operation setup/run hours and
        # work_order.actual_hours -- summed across ALL operators (each operator has its
        # own entry; they are summed, not deduped). Atomic with the completion; the
        # evidence-sourced WO rollup below reconciles totals monotonic-up.
        if is_labor_cost_rollup_enabled(company_id):
            rollup_labor_hours_for_closed_entries(work_order, operation, open_entries)

    # Create audit log
    audit = AuditService(db, current_user)
    audit.log(
        action="COMPLETE_OPERATION" if is_fully_complete else "UPDATE_OPERATION_PROGRESS",
        resource_type="work_order_operation",
        resource_id=operation_id,
        description=(
            f"{'Completed' if is_fully_complete else 'Updated'} operation {operation.operation_number} on WO {work_order.work_order_number}. "
            f"Qty: {resolved_quantity}/{work_order.quantity_ordered}"
            + (f". Notes: {completion_data.notes}" if completion_data.notes else "")
        ),
    )

    # EVT-2: emit the uniform completion OperationalEvents in-process (before the
    # terminal commit so they land atomically with the status change). This scan/
    # shop-floor complete_operation path previously emitted NO OperationalEvent.
    if is_fully_complete:
        emit_operation_completed_event(
            db,
            company_id=company_id,
            work_order=work_order,
            operation=operation,
            user_id=current_user.id,
            source_module="shop_floor",
            source=recorded_source,
        )
    if work_order_completed:
        emit_work_order_completed_event(
            db,
            company_id=company_id,
            work_order=work_order,
            user_id=current_user.id,
            source_module="shop_floor",
            source=recorded_source,
        )
        # Batch 6 / rank 9 (INV-1/INV-2/INV-3/TRACE-2/TRACE-3): FG receipt (always,
        # lot-only, idempotent) + gated backflush, atomic with this completion.
        apply_completion_inventory_effects(db, work_order, user_id=current_user.id, company_id=company_id, audit=audit)
        # Batch 7 / rank 10 (COST-1/COST-2/COST-4/COST-5): OPT-IN (default OFF) labor
        # hour + actual-cost + JobCost rollup. No-op + pre-Batch-7 behavior when the flag
        # is OFF; when ON, rolls op/WO hours monotonic-up from durable evidence, computes
        # actual_cost, syncs the linked JobCost (-> COMPLETED), all atomic with completion.
        apply_completion_cost_rollup(db, work_order, company_id=company_id, user_id=current_user.id, audit=audit)

    # Batch 4 / rank 7 (QG-1/3, BLK-2): warn-and-record on a true completion only.
    # Read-only evaluation against the locked op + WO; each unsatisfied gate gets a
    # tamper-evident audit row + warning event committed atomically below. Never blocks.
    quality_exceptions: list[QualityException] = []
    if is_fully_complete:
        quality_exceptions = evaluate_and_record_completion_quality_exceptions(
            db,
            company_id=company_id,
            work_order=work_order,
            operation=operation,
            audit=audit,
            user=current_user,
            source="complete_operation",
        )
    # Batch 7 data-quality signal (no_labor_recorded): when the WO completes with one or
    # more operations that recorded ZERO labor, surface it on the SAME quality_exceptions
    # channel (audit + warning event). Fires REGARDLESS of the cost-rollup flag (a
    # process signal, not a cost figure). Warn-only, never blocks.
    if work_order_completed:
        quality_exceptions = quality_exceptions + evaluate_and_record_labor_data_quality(
            db,
            company_id=company_id,
            work_order=work_order,
            audit=audit,
            user=current_user,
            source="complete_operation",
        )
    # G1 ADVANCE: when THIS WO (a laser child) just completed, surface a signal on its
    # parent iff every laser child is now terminal. Signal-only -- we do NOT
    # auto-complete the parent (parent/child WOs are not operation-coupled). Fires only
    # when ALL children are terminal, which becomes true exactly once (last child
    # flips); idempotent completion + non-reopening reconcile => records at most once.
    if work_order_completed:
        parent = find_parent_to_advance(db, work_order, company_id)
        if parent is not None:
            record_parent_children_complete(
                db,
                parent_work_order=parent,
                child_work_order=work_order,
                company_id=company_id,
                user_id=current_user.id,
                audit=audit,
                source="completion",
            )

    try:
        db.commit()
    except StaleDataError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="This operation was modified concurrently. Refresh and retry the completion.",
        ) from exc
    db.refresh(operation)

    # PERF-5: the scheduling refresh ran with commit=False (joined to this handler's
    # unit of work), so it skipped the in-service WC cache invalidation -- do it here,
    # after the terminal commit succeeded, so the cache reflects the freed capacity.
    if work_centers_refreshed:
        invalidate_work_centers_cache()

    # EVT-3: on WO COMPLETE, enqueue the tenant-scoped notification + webhook
    # dispatch in the ARQ worker. After commit + best-effort so it can never fail
    # the already-committed completion.
    if work_order_completed:
        enqueue_work_order_completion_signals(work_order_id=work_order.id, company_id=company_id, status="COMPLETE")

    if operation.work_center_id:
        safe_broadcast(
            broadcast_shop_floor_update,
            operation.work_center_id,
            {
                "event": "operation_completed",
                "work_order_id": work_order.id,
                "operation_id": operation.id,
                "is_fully_complete": is_fully_complete,
            },
            company_id=company_id,
        )
    safe_broadcast(
        broadcast_work_order_update,
        work_order.id,
        {
            "event": "operation_completed",
            "operation_id": operation.id,
            "status": work_order.status.value if hasattr(work_order.status, "value") else work_order.status,
            "is_fully_complete": is_fully_complete,
        },
        company_id=company_id,
    )
    safe_broadcast(
        broadcast_dashboard_update,
        {
            "event": "operation_completed",
            "work_order_id": work_order.id,
            "operation_id": operation.id,
            "is_fully_complete": is_fully_complete,
        },
        company_id=company_id,
    )

    return {
        "message": "Operation completed" if is_fully_complete else "Progress updated",
        "operation": {
            "id": operation.id,
            "status": operation.status.value,
            "quantity_complete": operation.quantity_complete,
            "actual_start": to_utc_iso(operation.actual_start),
            "actual_end": to_utc_iso(operation.actual_end),
        },
        "work_order": {
            "id": work_order.id,
            "status": work_order.status.value,
            "quantity_complete": work_order.quantity_complete,
        },
        "is_fully_complete": is_fully_complete,
        # Warn-and-record (Batch 4 / rank 7): unsatisfied quality gates at completion.
        "quality_exceptions": [exc.as_dict() for exc in quality_exceptions],
        # Crew-station kiosk: the open entries this completion auto-closed
        # (empty on a partial/progress update).
        "closed_time_entries": closed_time_entries,
    }


@router.get("/operations/{operation_id}")
def get_operation_details(
    operation_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get detailed information about a specific operation"""
    operation = (
        db.query(WorkOrderOperation)
        .options(
            joinedload(WorkOrderOperation.work_order).joinedload(WorkOrder.part),
            joinedload(WorkOrderOperation.work_center),
            selectinload(WorkOrderOperation.laser_nest).selectinload(LaserNest.document),
        )
        .filter(
            WorkOrderOperation.id == operation_id,
            WorkOrderOperation.company_id == company_id,
        )
        .first()
    )

    if not operation:
        raise HTTPException(status_code=404, detail="Operation not found")

    wo = operation.work_order
    wc = operation.work_center

    # Get all operations for this work order
    all_ops = (
        db.query(WorkOrderOperation)
        .options(selectinload(WorkOrderOperation.laser_nest).selectinload(LaserNest.document))
        .filter(
            WorkOrderOperation.work_order_id == wo.id,
            WorkOrderOperation.company_id == company_id,
        )
        .order_by(WorkOrderOperation.sequence)
        .all()
    )
    wo.operations = all_ops
    # Reconcile-on-read: a concurrent-write conflict here is benign (idempotent),
    # so it must NOT 500 the details read -- _reconcile_and_commit swallows StaleDataError.
    # AUD-3: terminal reconcile-driven transitions are audited to the requesting user.
    _reconcile_and_commit(db, [wo], current_user, company_id)

    # Get recent history (audit logs). The operation is already tenant-validated
    # above; the company_id filter is defense-in-depth now that audit rows are
    # tenant-tagged.
    history = (
        db.query(AuditLog)
        .filter(
            and_(
                AuditLog.resource_type == "work_order_operation",
                AuditLog.resource_id == operation_id,
                AuditLog.company_id == company_id,
            )
        )
        .order_by(AuditLog.timestamp.desc())
        .limit(10)
        .all()
    )

    return {
        "operation": {
            "id": operation.id,
            "operation_number": operation.operation_number,
            "name": operation.name,
            "description": operation.description,
            "status": operation.status.value,
            "quantity_ordered": operation_target_quantity(operation, wo),
            "work_order_quantity_ordered": wo.quantity_ordered,
            "component_quantity": operation.component_quantity,
            "quantity_complete": operation.quantity_complete,
            "quantity_scrapped": operation.quantity_scrapped,
            "laser_nest": _laser_nest_payload(operation),
            "setup_instructions": operation.setup_instructions,
            "run_instructions": operation.run_instructions,
            "setup_time_hours": operation.setup_time_hours,
            "run_time_hours": operation.run_time_hours,
            "actual_setup_hours": operation.actual_setup_hours,
            "actual_run_hours": operation.actual_run_hours,
            "actual_start": to_utc_iso(operation.actual_start),
            "actual_end": to_utc_iso(operation.actual_end),
            "requires_inspection": operation.requires_inspection,
            "inspection_type": operation.inspection_type,
            "inspection_complete": operation.inspection_complete,
            **_operation_check_in_state(db, operation),
        },
        "work_order": {
            "id": wo.id,
            "work_order_number": wo.work_order_number,
            "status": wo.status.value,
            "quantity_ordered": wo.quantity_ordered,
            "quantity_complete": wo.quantity_complete,
            "due_date": wo.due_date.isoformat() if wo.due_date else None,
            "customer_name": wo.customer_name,
            "customer_po": wo.customer_po,
            "notes": wo.notes,
            "special_instructions": wo.special_instructions,
            "part": {
                "part_number": wo.part.part_number if wo.part else None,
                "name": wo.part.name if wo.part else None,
                "description": wo.part.description if wo.part else None,
            },
        },
        "work_center": {
            "id": wc.id if wc else None,
            "name": wc.name if wc else None,
            "code": wc.code if wc else None,
        },
        "all_operations": [
            {
                "id": op.id,
                "sequence": op.sequence,
                "operation_number": op.operation_number,
                "name": op.name,
                "status": op.status.value,
                "quantity_ordered": operation_target_quantity(op, wo),
                "work_order_quantity_ordered": wo.quantity_ordered,
                "component_quantity": op.component_quantity,
                "quantity_complete": op.quantity_complete,
                "quantity_scrapped": op.quantity_scrapped,
                "laser_nest": _laser_nest_payload(op),
                "actual_setup_hours": op.actual_setup_hours,
                "actual_run_hours": op.actual_run_hours,
                "actual_start": to_utc_iso(op.actual_start),
                "actual_end": to_utc_iso(op.actual_end),
                "started_by": op.started_by,
                "completed_by": op.completed_by,
                "is_current": op.id == operation_id,
                **_operation_check_in_state(db, op),
            }
            for op in all_ops
        ],
        "history": [
            {
                "action": h.action,
                "details": h.description,
                "created_at": to_utc_iso(h.timestamp),
            }
            for h in history
        ],
    }


@router.put("/operations/{operation_id}/hold")
def put_operation_on_hold(
    operation_id: int,
    hold_data: Optional[OperationHoldRequest] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Put an operation on hold.

    The body is optional and backward-compatible. When present it carries the
    structured hold details -- ``category`` / ``severity`` / ``note`` (a note or a
    non-OTHER category also files a WorkOrderBlocker) -- plus the optional
    ``source`` adoption-telemetry channel (kiosk | desktop | scanner | backfill;
    ``import`` is rejected with 422 -- reserved for the bulk-migration loaders -- and a
    kiosk-scoped operator token forces ``kiosk``) that tags the emitted event and fills
    the channel on any open time entries the hold auto-closes (never overwriting a
    recorded one).
    """
    operation = (
        db.query(WorkOrderOperation)
        .options(joinedload(WorkOrderOperation.work_order))
        .filter(WorkOrderOperation.id == operation_id, WorkOrderOperation.company_id == company_id)
        .first()
    )

    if not operation:
        raise HTTPException(status_code=404, detail="Operation not found")

    if operation.status == OperationStatus.COMPLETE:
        raise HTTPException(status_code=400, detail="Cannot put completed operation on hold")

    # A0.1 adoption-telemetry channel of THIS hold write (kiosk-token forcing + import
    # guard). Resolved before any mutation so a disallowed 'import' 422s without changing
    # operation state or closing any entry.
    hold_source = _resolve_labor_source(current_user, hold_data.source if hold_data else None)

    operation.status = OperationStatus.ON_HOLD
    operation.updated_at = datetime.utcnow()

    # Close any open time entries for this operation
    open_entries = (
        db.query(TimeEntry)
        .filter(
            and_(
                TimeEntry.operation_id == operation_id,
                TimeEntry.company_id == company_id,
                TimeEntry.clock_out.is_(None),
            )
        )
        .all()
    )
    now = datetime.utcnow()
    for entry in open_entries:
        entry.clock_out = now
        if entry.clock_in:
            entry.duration_hours = (now - entry.clock_in).total_seconds() / 3600.0
        # A0.1 adoption telemetry: a hold auto-closes OTHER operators' open entries
        # too, so only FILL a missing channel -- never overwrite an entry's own
        # recorded clock-in channel with the holder's channel (same as /complete).
        if hold_source and entry.source is None:
            entry.source = hold_source

    # Create audit log
    AuditService(db, current_user).log(
        action="HOLD_OPERATION",
        resource_type="work_order_operation",
        resource_id=operation_id,
        description=f"Put operation {operation.operation_number} on hold",
    )
    if work_order := operation.work_order:
        if hold_data and (hold_data.note or hold_data.category != WorkOrderBlockerCategory.OTHER):
            WorkOrderBlockerService(db).create_blocker(
                company_id=company_id,
                user=current_user,
                work_order_id=work_order.id,
                data=WorkOrderBlockerCreate(
                    operation_id=operation.id,
                    category=hold_data.category,
                    severity=hold_data.severity,
                    note=hold_data.note,
                    put_operation_on_hold=False,
                ),
                source=hold_source,
            )
        else:
            OperationalEventService(db).emit_best_effort(
                company_id=company_id,
                event_type="operation_hold",
                source_module="shop_floor",
                entity_type="work_order_operation",
                entity_id=operation.id,
                work_order_id=work_order.id,
                operation_id=operation.id,
                user_id=current_user.id,
                severity="medium",
                event_payload={
                    "work_order_number": work_order.work_order_number,
                    "operation_name": operation.name,
                    # A0.1 adoption telemetry: client channel (None = not reported).
                    "source": hold_source,
                },
            )

    db.commit()

    work_order = operation.work_order
    if operation.work_center_id:
        safe_broadcast(
            broadcast_shop_floor_update,
            operation.work_center_id,
            {
                "event": "operation_hold",
                "work_order_id": work_order.id if work_order else None,
                "operation_id": operation.id,
            },
            company_id=company_id,
        )
    if work_order:
        safe_broadcast(
            broadcast_work_order_update,
            work_order.id,
            {
                "event": "operation_hold",
                "operation_id": operation.id,
                "status": work_order.status.value if hasattr(work_order.status, "value") else work_order.status,
            },
            company_id=company_id,
        )
        safe_broadcast(
            broadcast_dashboard_update,
            {
                "event": "operation_hold",
                "work_order_id": work_order.id,
                "operation_id": operation.id,
            },
            company_id=company_id,
        )

    return {"message": "Operation placed on hold", "status": operation.status.value}


@router.put("/operations/{operation_id}/resume")
def resume_operation(
    operation_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Resume an operation that was on hold"""
    operation = (
        db.query(WorkOrderOperation)
        .options(joinedload(WorkOrderOperation.work_order))
        .filter(WorkOrderOperation.id == operation_id, WorkOrderOperation.company_id == company_id)
        .first()
    )

    if not operation:
        raise HTTPException(status_code=404, detail="Operation not found")

    if operation.status != OperationStatus.ON_HOLD:
        raise HTTPException(status_code=400, detail="Operation is not on hold")

    # BLK-4 (warn-and-record): resuming an op does NOT resolve the blocker(s) that
    # put it on hold (resolution stays owned by the blocker resolve/dismiss flow), so
    # surface any still-open blocker on the response so the operator/dashboard is
    # warned that operation status and blocker status are about to diverge. Read-only.
    open_blockers = (
        db.query(WorkOrderBlocker)
        .filter(
            WorkOrderBlocker.company_id == company_id,
            WorkOrderBlocker.operation_id == operation.id,
            WorkOrderBlocker.status.in_([WorkOrderBlockerStatus.OPEN.value, WorkOrderBlockerStatus.ACKNOWLEDGED.value]),
        )
        .all()
    )

    # Resume to previous state
    operation.status = OperationStatus.IN_PROGRESS if operation.actual_start else OperationStatus.READY
    operation.updated_at = datetime.utcnow()

    # Create audit log. BLK-4: note any still-open blocker so the audit row records
    # that the op was resumed while its blocker(s) remained open.
    AuditService(db, current_user).log(
        action="RESUME_OPERATION",
        resource_type="work_order_operation",
        resource_id=operation_id,
        description=f"Resumed operation {operation.operation_number}",
        extra_data=({"open_blocker_ids": [b.id for b in open_blockers]} if open_blockers else None),
    )
    if operation.work_order:
        OperationalEventService(db).emit_best_effort(
            company_id=company_id,
            event_type="operation_resumed",
            source_module="shop_floor",
            entity_type="work_order_operation",
            entity_id=operation.id,
            work_order_id=operation.work_order.id,
            operation_id=operation.id,
            user_id=current_user.id,
            severity="info",
            event_payload={
                "work_order_number": operation.work_order.work_order_number,
                "operation_name": operation.name,
            },
        )

    db.commit()

    work_order = operation.work_order
    if operation.work_center_id:
        safe_broadcast(
            broadcast_shop_floor_update,
            operation.work_center_id,
            {
                "event": "operation_resumed",
                "work_order_id": work_order.id if work_order else None,
                "operation_id": operation.id,
            },
            company_id=company_id,
        )
    if work_order:
        safe_broadcast(
            broadcast_work_order_update,
            work_order.id,
            {
                "event": "operation_resumed",
                "operation_id": operation.id,
                "status": work_order.status.value if hasattr(work_order.status, "value") else work_order.status,
            },
            company_id=company_id,
        )
        safe_broadcast(
            broadcast_dashboard_update,
            {
                "event": "operation_resumed",
                "work_order_id": work_order.id,
                "operation_id": operation.id,
            },
            company_id=company_id,
        )

    return {
        "message": "Operation resumed",
        "status": operation.status.value,
        # BLK-4: warn that these blockers are still open even though the op resumed.
        "open_blockers": [
            {
                "id": b.id,
                "title": b.title,
                "category": b.category,
                "severity": b.severity,
                "status": b.status,
            }
            for b in open_blockers
        ],
    }


@router.post("/operations/{operation_id}/inspection")
def mark_operation_inspected(
    operation_id: int,
    inspection_data: OperationInspectionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(
        require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR, UserRole.QUALITY])
    ),
    company_id: int = Depends(get_current_company_id),
):
    """Record an operation's inspection as complete (QG-2 writer).

    Before Batch 4 NOTHING set ``WorkOrderOperation.inspection_complete = True``, so
    the inspection quality gate (QG-1) could warn but never CLEAR -- the warning was
    not actionable. This audited writer closes that loop: it flips
    ``inspection_complete`` true, records who/when, and writes a tamper-evident
    ``audit_log`` row, so a subsequent completion stops raising the
    ``inspection_incomplete`` exception.

    Tenant-scoped (operation must belong to the active company) and RBAC-gated to
    ADMIN / MANAGER / SUPERVISOR / QUALITY -- the roles that perform / sign off
    inspection in this repo's role model (there is no separate INSPECTOR role).

    FAI-pass auto-wire is DEFERRED: the FAI/NCR tables carry no ``operation_id`` FK,
    so an FAI passing cannot be linked back to a specific operation without a schema
    change (database-migration-specialist). In warn-and-record mode the manual writer
    is sufficient (nothing is blocked); the auto-wire is flagged as a follow-up.
    """
    operation = (
        db.query(WorkOrderOperation)
        .options(joinedload(WorkOrderOperation.work_order))
        .filter(WorkOrderOperation.id == operation_id, WorkOrderOperation.company_id == company_id)
        .first()
    )
    if not operation:
        raise HTTPException(status_code=404, detail="Operation not found")

    work_order = operation.work_order
    was_complete = bool(operation.inspection_complete)
    operation.inspection_complete = True
    if inspection_data.inspection_type:
        operation.inspection_type = inspection_data.inspection_type
    operation.updated_at = datetime.utcnow()

    # Tamper-evident audit row for the inspection sign-off (who/when/what). Flushed
    # (not committed) by AuditService.log so it commits atomically with the flag below.
    note_suffix = f". Notes: {inspection_data.notes}" if inspection_data.notes else ""
    type_suffix = f" ({inspection_data.inspection_type})" if inspection_data.inspection_type else ""
    AuditService(db, current_user).log(
        action="MARK_OPERATION_INSPECTED",
        resource_type="work_order_operation",
        resource_id=operation.id,
        resource_identifier=operation.operation_number,
        description=(
            f"Recorded inspection complete{type_suffix} for operation {operation.operation_number}"
            + (f" on WO {work_order.work_order_number}" if work_order else "")
            + note_suffix
        ),
        new_values={"inspection_complete": True},
        old_values={"inspection_complete": was_complete},
    )

    if work_order:
        try:
            OperationalEventService(db).emit(
                company_id=company_id,
                event_type="operation_inspected",
                source_module="shop_floor",
                entity_type="work_order_operation",
                entity_id=operation.id,
                work_order_id=work_order.id,
                operation_id=operation.id,
                user_id=current_user.id,
                severity="info",
                event_payload={
                    "work_order_number": work_order.work_order_number,
                    "operation_name": operation.name,
                    "inspection_type": inspection_data.inspection_type,
                },
            )
        except Exception:  # pragma: no cover - signal emission must not break the write
            pass

    db.commit()
    db.refresh(operation)

    if operation.work_center_id:
        safe_broadcast(
            broadcast_shop_floor_update,
            operation.work_center_id,
            {
                "event": "operation_inspected",
                "work_order_id": work_order.id if work_order else None,
                "operation_id": operation.id,
            },
            company_id=company_id,
        )

    return {
        "message": "Operation inspection recorded",
        "operation_id": operation.id,
        "inspection_complete": operation.inspection_complete,
        "inspection_type": operation.inspection_type,
    }


# ============================================================================
# Process-sheet step execution (PR 3 of docs/PROCESS_SHEETS_SCOPE.md).
#
# Snapshot steps live on wo_operation_steps (copied at WO creation); captured
# evidence is APPEND-ONLY operation_step_records (corrections supersede, never
# mutate). These paths sit under /shop-floor on purpose: badge-minted
# kiosk-scoped operator tokens are path-fenced to this prefix (deps.py), so
# crew-station operators can read steps, record values and upload PHOTO/FILE
# evidence with ZERO fence changes — including the attachment endpoint below,
# which exists precisely because the fence blocks /documents/upload.
# ============================================================================


def _get_operation_and_work_order(
    db: Session, operation_id: int, company_id: int
) -> tuple[WorkOrderOperation, WorkOrder]:
    """Tenant-scoped operation + parent WO fetch shared by the step endpoints."""
    operation = (
        db.query(WorkOrderOperation)
        .options(joinedload(WorkOrderOperation.work_order))
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
    return operation, work_order


def _require_step_recordable(operation: WorkOrderOperation, work_order: WorkOrder) -> None:
    """Rung 1 of the capture ladder, mirroring the sibling predicates exactly:
    a TERMINAL parent WO is a state conflict (409, same shape as complete_operation's
    G6-A guard); a non-IN_PROGRESS operation is bad input (400, same rule as
    report_operation_production — step data is captured while running the op)."""
    if work_order.status in TERMINAL_WO_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"cannot record step data: work order is {work_order.status.value}",
        )
    if operation.status != OperationStatus.IN_PROGRESS:
        raise HTTPException(status_code=400, detail="Operation must be in progress to record step data")


def _record_source(current_user: User, client_source: Optional[TimeEntrySource]) -> Optional[str]:
    """Adoption-telemetry channel for step records — TimeEntry's exact trust model (PR 4).

    Mirrors clock-in: the client-reported channel is stored verbatim, or NULL when
    omitted (the server never GUESSES a channel), EXCEPT where the credential is
    authoritative — a badge-minted crew-station operator token (scope == "kiosk")
    ALWAYS records KIOSK regardless of any hint. Values are fenced to the
    TimeEntrySource vocabulary by the request schema (422 on anything else)."""
    if getattr(current_user, "_token_scope", None) == "kiosk":
        return TimeEntrySource.KIOSK.value
    return client_source.value if client_source else None


@router.get("/operations/{operation_id}/steps", response_model=OperationStepsViewResponse)
def get_operation_steps(
    operation_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Snapshot steps (ordered by sequence) + live records + per-serial completeness.

    Read-only; available in any operation/WO state so the kiosk can show the trail
    on held or completed jobs too. ``completeness`` maps step_id -> serial -> satisfied
    for serialized WOs; ``steps_total``/``steps_recorded`` are the required-step chip
    numbers (the work-center queue carries the same pair per item).
    """
    operation, work_order = _get_operation_and_work_order(db, operation_id, company_id)
    return process_sheet_service.build_steps_view(db, company_id, operation, work_order)


@router.post(
    "/operations/{operation_id}/steps/{step_id}/records",
    response_model=OperationStepRecordResponse,
    status_code=201,
)
def record_operation_step(
    operation_id: int,
    step_id: int,
    data: OperationStepRecordCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Capture one step record (AS9100D objective evidence; append-only).

    Validation ladder (service): WO not terminal (409) -> operation IN_PROGRESS (400)
    -> step belongs to operation (404) / INSTRUCTION takes no records (400) -> serial
    required+valid on serialized WOs (400) -> gauge (PR 4: the reference resolves from
    ``equipment_id`` OR ``equipment_code`` — the gauge's MARKED identifier, the kiosk
    scan/type path since operator tokens can't list /equipment; both -> 400, unknown
    code -> 404. ``requires_gauge`` measurement steps demand a MANDATORY
    calibration-current gauge — 400 missing / 409 ``GAUGE_OUT_OF_CAL``; other steps
    keep the optional tenant-valid passthrough) -> type-shaped value (400) ->
    MEASUREMENT out-of-tolerance REFUSED (409 ``OUT_OF_TOLERANCE``, no row). PR 4 also
    freezes the warn-and-record operator-qualification snapshot onto the record, feeds
    the SPC point for conforming measurements wired to a characteristic, and echoes
    the resolved gauge as ``gauge: {equipment_id, equipment_code, name}``. Audited
    before commit.
    """
    operation, work_order = _get_operation_and_work_order(db, operation_id, company_id)
    _require_step_recordable(operation, work_order)
    step = process_sheet_service.get_wo_step_or_404(db, company_id, operation.id, step_id)
    return process_sheet_service.create_step_record(
        db,
        company_id,
        work_order=work_order,
        operation=operation,
        step=step,
        data=data,
        user=current_user,
        audit=audit,
        source=_record_source(current_user, data.source),
    )


@router.post(
    "/operations/{operation_id}/steps/{step_id}/records/{record_id}/supersede",
    response_model=OperationStepRecordResponse,
    status_code=201,
)
def supersede_operation_step_record(
    operation_id: int,
    step_id: int,
    record_id: int,
    data: OperationStepRecordSupersede,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Correction path: a NEW record replaces ``record_id`` (append-only chain).

    Requires ``reason`` + the replacement value fields; the replacement runs the FULL
    capture ladder (including the out-of-tolerance refusal) and inherits the superseded
    record's serial. The old row is stamped ``superseded_by_id``/``supersede_reason``
    exactly once — an already-superseded record 409s. Audited before commit.
    """
    operation, work_order = _get_operation_and_work_order(db, operation_id, company_id)
    _require_step_recordable(operation, work_order)
    step = process_sheet_service.get_wo_step_or_404(db, company_id, operation.id, step_id)
    return process_sheet_service.supersede_step_record(
        db,
        company_id,
        work_order=work_order,
        operation=operation,
        step=step,
        record_id=record_id,
        data=data,
        user=current_user,
        audit=audit,
        source=_record_source(current_user, data.source),
    )


@router.post(
    "/operations/{operation_id}/steps/{step_id}/attachment",
    response_model=StepAttachmentResponse,
    status_code=201,
)
async def upload_operation_step_attachment(
    operation_id: int,
    step_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Upload PHOTO/FILE step evidence as a QUALITY_RECORD Document.

    Kiosk-scoped operator tokens CANNOT reach ``/documents/upload`` (the deps.py path
    fence stops at /shop-floor), so this in-fence endpoint wraps the same
    StorageBackend/Document persistence (receiving-label precedent) with image/PDF MIME
    and size validation. Returns the document id to pass as ``attachment_document_id``
    on the subsequent record create.
    """
    operation, work_order = _get_operation_and_work_order(db, operation_id, company_id)
    _require_step_recordable(operation, work_order)
    step = process_sheet_service.get_wo_step_or_404(db, company_id, operation.id, step_id)
    content = await file.read()
    document = process_sheet_service.store_step_attachment(
        db,
        company_id,
        work_order=work_order,
        operation=operation,
        step=step,
        content=content,
        filename=file.filename,
        content_type=file.content_type,
        user=current_user,
        audit=audit,
    )
    return {
        "document_id": document.id,
        "document_number": document.document_number,
        "file_name": document.file_name,
        "file_size": document.file_size,
        "mime_type": document.mime_type,
    }


@router.post(
    "/operations/{operation_id}/steps/{step_id}/quality-hold",
    response_model=QualityHoldResponse,
    status_code=201,
)
def raise_step_quality_hold(
    operation_id: int,
    step_id: int,
    data: QualityHoldRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """One-tap NCR + quality hold for a REFUSED out-of-tolerance measurement (PR 4).

    An OOT value is never stored as a record (409 ``OUT_OF_TOLERANCE``); this is the
    sanctioned path forward. Atomically: creates an ``IN_PROCESS`` NCR pre-filled from
    the snapshot step config (specification/required from lsl/nominal/usl, actual =
    the refused measurement), files a QUALITY_HOLD ``WorkOrderBlocker`` carrying the
    ``ncr_id``, flips the operation ON_HOLD through the existing blocker hold pathway,
    and closes open time entries (same as ``PUT .../hold``). All audited.

    Same all-authenticated posture as the other shop-floor writes, and in-fence for
    badge-minted kiosk operator tokens (this lives under ``/shop-floor``): kiosk
    operators file these.
    """
    operation, work_order = _get_operation_and_work_order(db, operation_id, company_id)
    _require_step_recordable(operation, work_order)
    step = process_sheet_service.get_wo_step_or_404(db, company_id, operation.id, step_id)
    result = process_sheet_service.create_quality_hold(
        db,
        company_id,
        work_order=work_order,
        operation=operation,
        step=step,
        data=data,
        user=current_user,
        audit=audit,
        source=_record_source(current_user, data.source),
    )

    # Same post-commit broadcasts as PUT .../hold so queues/dashboards refresh.
    if operation.work_center_id:
        safe_broadcast(
            broadcast_shop_floor_update,
            operation.work_center_id,
            {"event": "operation_hold", "work_order_id": work_order.id, "operation_id": operation.id},
            company_id=company_id,
        )
    safe_broadcast(
        broadcast_work_order_update,
        work_order.id,
        {
            "event": "operation_hold",
            "operation_id": operation.id,
            "status": work_order.status.value if hasattr(work_order.status, "value") else work_order.status,
        },
        company_id=company_id,
    )
    safe_broadcast(
        broadcast_dashboard_update,
        {"event": "operation_hold", "work_order_id": work_order.id, "operation_id": operation.id},
        company_id=company_id,
    )
    return result


# ============================================================================
# Crew-station kiosk stations (A0 crew kiosk).
#
# A KioskStation is the PIN-unlocked, work-center-bound, revocable auth anchor
# for the shared crew tablet (twin of the visitor SigninStation). Admin
# lifecycle is ADMIN/MANAGER-gated and mirrors visitor_logs.py; station-login
# is PUBLIC + rate-limited (see /api/v1/shop-floor/kiosk-stations/station-login
# in main.py AUTH_RATE_LIMITS) and mints the scoped type="kiosk" JWT honored
# ONLY by get_kiosk_or_user (queue read) and POST /auth/kiosk-badge-token.
# ============================================================================

# Staff roles allowed to manage kiosk stations (same set as visitor stations).
_KIOSK_STATION_MANAGE_ROLES = [UserRole.ADMIN, UserRole.MANAGER]


def _kiosk_station_response(station) -> KioskStationResponse:
    """Serialize a KioskStation row + its bound work center identity (no PIN/hash)."""
    return KioskStationResponse(
        id=station.id,
        label=station.label,
        work_center_id=station.work_center_id,
        work_center_code=station.work_center.code if station.work_center else None,
        work_center_name=station.work_center.name if station.work_center else None,
        revoked=station.revoked,
        revoked_at=station.revoked_at,
        revoked_by=station.revoked_by,
        last_used_at=station.last_used_at,
        created_by=station.created_by,
        created_at=station.created_at,
    )


@router.post("/kiosk-stations/station-login", response_model=KioskStationLoginResponse)
def kiosk_station_login(
    payload: KioskStationLoginRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """Unlock a crew tablet with the shared station PIN → scoped type='kiosk' JWT.

    PUBLIC + rate-limited (5/minute per IP). The DB row is the company-binding
    authority; the minted token's ``cid`` comes from it, never from the client.
    The response carries the station identity (label + bound work center) the
    tablet needs to render its header and scope its queue polling. Failed PIN
    attempts are recorded as an operational audit event.
    """
    try:
        station, token, expires_in = kiosk_station_service.authenticate_station(
            db, station_id=payload.station_id, pin=payload.pin
        )
    except Exception:
        # Audit the failed attempt against the station's company when the station
        # exists (so the trail stays tenant-attributed); swallow lookup issues so
        # we never leak whether the station id or the PIN was wrong.
        try:
            from app.models.kiosk_station import KioskStation

            existing = db.query(KioskStation).filter(KioskStation.id == payload.station_id).first()
            if existing is not None:
                audit = AuditService(db, user=None, request=request, company_id=existing.company_id)
                audit.log(
                    action="LOGIN_FAILED",
                    resource_type="kiosk_station",
                    resource_id=existing.id,
                    resource_identifier=existing.label,
                    description=f"Failed PIN attempt for crew-station kiosk '{existing.label}'",
                    success=False,
                )
                db.commit()
        except Exception:  # pragma: no cover - defensive
            logger.exception("Failed to audit kiosk station-login failure")
        raise

    return KioskStationLoginResponse(
        access_token=token,
        token_type="bearer",
        expires_in=expires_in,
        station=KioskStationInfo(
            id=station.id,
            label=station.label,
            work_center_id=station.work_center_id,
            work_center_code=station.work_center.code if station.work_center else None,
            work_center_name=station.work_center.name if station.work_center else None,
        ),
    )


@router.post("/kiosk-stations", response_model=KioskStationResponse, status_code=201)
def create_kiosk_station(
    payload: KioskStationCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_KIOSK_STATION_MANAGE_ROLES)),
    company_id: int = Depends(get_current_company_id),
):
    """Create a PIN-protected crew-station kiosk bound to a work center.

    ADMIN/MANAGER. The PIN is bcrypt-hashed and never echoed; the work center
    must belong to the active company (404 otherwise).
    """
    audit = AuditService(db, user=current_user, request=request, company_id=company_id)
    station = kiosk_station_service.create_station(
        db,
        company_id=company_id,
        label=payload.label,
        work_center_id=payload.work_center_id,
        pin=payload.pin,
        created_by=current_user.id,
        audit=audit,
    )
    return _kiosk_station_response(station)


@router.get("/kiosk-stations", response_model=KioskStationListResponse)
def list_kiosk_stations(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_KIOSK_STATION_MANAGE_ROLES)),
    company_id: int = Depends(get_current_company_id),
):
    """List this company's crew-station kiosks (no PIN/pin_hash exposed)."""
    stations = kiosk_station_service.list_stations(db, company_id=company_id)
    return KioskStationListResponse(stations=[_kiosk_station_response(s) for s in stations])


@router.post("/kiosk-stations/{station_id}/revoke", response_model=KioskStationResponse)
def revoke_kiosk_station(
    station_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_KIOSK_STATION_MANAGE_ROLES)),
    company_id: int = Depends(get_current_company_id),
):
    """Revoke a kiosk station (idempotent, audited). The tablet loses access next request."""
    audit = AuditService(db, user=current_user, request=request, company_id=company_id)
    station = kiosk_station_service.revoke_station(
        db,
        company_id=company_id,
        station_id=station_id,
        revoked_by=current_user.id,
        audit=audit,
    )
    return _kiosk_station_response(station)


@router.post("/kiosk-stations/{station_id}/reset-pin", response_model=KioskStationResponse)
def reset_kiosk_station_pin(
    station_id: int,
    payload: KioskStationResetPinRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_KIOSK_STATION_MANAGE_ROLES)),
    company_id: int = Depends(get_current_company_id),
):
    """Reset a kiosk station's shared PIN (re-hashed, audited; PIN never logged)."""
    audit = AuditService(db, user=current_user, request=request, company_id=company_id)
    station = kiosk_station_service.reset_pin(
        db,
        company_id=company_id,
        station_id=station_id,
        pin=payload.pin,
        audit=audit,
    )
    return _kiosk_station_response(station)
