import asyncio
import functools
import json
import logging
import math
import os
import shutil
import tempfile
import uuid
from dataclasses import asdict as dataclass_asdict
from dataclasses import replace as dataclass_replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, List, Literal, Optional, Set

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, TypeAdapter, ValidationError
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, joinedload, selectinload
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.orm.exc import StaleDataError

from app.api.audit_guard import required_audit
from app.api.deps import get_audit_service, get_current_company_id, get_current_user, require_role

# THE tenant-scoped ``{part_id: Part}`` resolver for BOM-line components. Imported rather
# than re-implemented: this module and ``bom.py`` render the same rows, and a third private
# copy is a third place for the ``company_id`` predicate to go missing. ``bom.py`` imports
# no endpoint module, so there is no cycle.
from app.api.endpoints.bom import tenant_parts_by_id
from app.api.endpoints.user_workspaces import require_workspace_access
from app.core.cache import invalidate_work_centers_cache
from app.core.credentials import is_api_token_credential
from app.core.realtime import safe_broadcast
from app.core.time_utils import to_utc_iso
from app.core.websocket import (
    broadcast_dashboard_update,
    broadcast_shop_floor_update,
    broadcast_work_order_update,
)
from app.db.database import atomic_transaction, get_db
from app.db.locks import acquire_generator_lock
from app.db.tenant_filter import tenant_query
from app.models.bom import BOM, BOMItem
from app.models.laser_nest import LaserNest
from app.models.part import Part, PartType, UnitOfMeasure, uom_label
from app.models.routing import Routing, RoutingOperation
from app.models.time_entry import TimeEntry, TimeEntrySource
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus, WorkOrderType
from app.models.work_order_template import WorkOrderTemplate
from app.schemas.dispatch import DispatchChangeKind
from app.schemas.import_kit import WorkOrderImportResponse
from app.schemas.time_entry import ProductionReductionRequest
from app.schemas.work_order import (
    LaserNestImportRow,
    LaserNestManualCreate,
    LaserNestManualResponse,
    LaserNestPreviewRow,
    WorkOrderBrowseResponse,
    WorkOrderCreate,
    WorkOrderDuplicateRequest,
    WorkOrderDuplicateResponse,
    WorkOrderOperationCreate,
    WorkOrderOperationResponse,
    WorkOrderOperationUpdate,
    WorkOrderResponse,
    WorkOrderRestoreResponse,
    WorkOrderResumeResponse,
    WorkOrderSummary,
    WorkOrderUpdate,
)
from app.schemas.work_order_timeline import TimelineCategory, WorkOrderTimelineResponse
from app.services import dispatch_service, process_sheet_service
from app.services.audit_service import AuditService, AuditWriteError
from app.services.completion_cost_service import (
    apply_completion_cost_rollup,
    compute_and_store_estimated_cost,
    rollup_labor_hours_for_closed_entries,
    rollup_labor_hours_from_evidence,
)
from app.services.completion_inventory_service import (
    apply_completion_inventory_effects,
    apply_operation_completion_inventory_effects,
)
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
from app.services.import_service import ImportFileError, parse_import_file
from app.services.labor_cost_service import is_labor_cost_rollup_enabled
from app.services.laser_nest_extraction_service import extract_nest_fields_from_pdf, segment_nest_pdf
from app.services.laser_nest_pdf_split_service import (
    get_pdf_page_count,
    is_bare_pdf_upload,
    segment_file_name,
    split_pdf_segments,
)
from app.services.laser_nest_service import (
    LASER_PDF_PACKAGE_MAX,
    ParsedLaserNest,
    active_laser_nest,
    build_laser_nest_child_work_order,
    build_parsed_nest_from_extraction,
    copy_laser_nest_folder,
    create_manual_laser_nest,
    create_nest_material_allocation,
    extract_laser_nest_zip,
    manual_nest_response_dict,
    package_has_pdfs,
    parse_laser_nest_folder,
    sync_laser_nest_from_operation,
)
from app.services.material_consumption_service import (
    MaterialAllocationConsumedError,
    allocations_on_work_order,
    cancel_open_allocations_for_work_order,
    ledger_backed_allocation_ids,
    reopen_allocations_cancelled_by_delete,
)
from app.services.material_tie_part_gate import assert_part_is_tieable_material
from app.services.migration_import_service import import_open_work_orders
from app.services.operation_action_gates import (
    API_TOKEN_SHORT_CLOSE,
    COMPLETION_AUDIT_UNAVAILABLE,
    MSG_ALREADY_COMPLETE,
    MSG_ALREADY_IN_PROGRESS,
    MSG_DRAFT_CANNOT_BE_HELD,
    MSG_OPERATION_ON_HOLD_NO_START,
    MSG_PREDECESSORS_INCOMPLETE,
    MSG_SCRAP_REASON_OR_CODE_REQUIRED,
    MSG_WORK_ORDER_NOT_RELEASED,
    MSG_WORK_ORDER_ON_HOLD,
    ceiling_exceeded_message,
    completed_counts_locked_message,
    force_complete_exceeds_physical_message,
    force_complete_exceeds_recorded_message,
    force_complete_unaccounted_message,
    office_no_change_message,
    operation_blocked_by_predecessors,
    operation_has_labor_evidence,
    others_clocked_in_message,
    override_reason_required_message,
    quantity_ordered_floor_message,
    serialized_short_message,
    work_order_start_blocker,
)

# WHY an operation is held, WHO held it and WHEN -- the SAME batched, pure-read view the
# kiosk queue renders (shop_floor.py), so the office page and the floor cannot tell two
# different stories about one hold. Two queries for the whole held set, tenant-scoped.
from app.services.operation_hold_view import HoldContext, hold_contexts_for_operations
from app.services.operational_event_service import OperationalEventService
from app.services.production_reduction_service import (
    approved_produced_total,
    eligible_reduction_entries,
    load_operation_for_reduction_or_http,
    perform_production_reduction,
)
from app.services.quality_gate_service import (
    QualityException,
    evaluate_and_record_completion_quality_exceptions,
    evaluate_completion_quality_exceptions,
    evaluate_inspection_exception,
    record_completion_quality_exceptions,
    record_reconcile_inspection_exception,
)
from app.services.scheduling_service import SchedulingService
from app.services.scrap_reason_service import resolve_scrap_reason_code_or_http

# The sheet-stock suggestion pair. Both are PURE READS that write nothing -- no
# ledger row, no audit row, no event -- and neither creates a tie: the preview
# proposes, the planner confirms on Import. ``resolve_ambiguous_sheet_matches``
# may only re-rank and annotate an AMBIGUOUS shortlist; ``auto_fill_part_id`` is
# assigned by the deterministic gate alone and is unreachable from the resolver.
from app.services.sheet_stock_ai_resolver import resolve_ambiguous_sheet_matches
from app.services.sheet_stock_matcher import STATUS_AMBIGUOUS, match_sheet_parts
from app.services.storage_service import delete_ref
from app.services.wallboard_service import operator_display_name
from app.services.work_center_type_service import get_work_center_group
from app.services.work_order_browse_service import browse_work_orders
from app.services.work_order_duplicate_service import duplicate_work_order
from app.services.work_order_state_service import (
    QTY_EPSILON,
    TERMINAL_WO_STATUSES,
    OperationAccounting,
    StageFigures,
    StatusTransition,
    WorkOrderStateError,
    accounting_extra_data,
    active_work_order_operations,
    apply_work_order_resume,
    begin_operation_progress,
    demote_operations_for_sequencing,
    finalize_operation_completion,
    find_parent_to_advance,
    is_laser_dispatch_work_order,
    log_demotions_required,
    operation_accounting,
    operation_labor_evidence,
    operation_target_quantity,
    operations_worked_out_of_sequence,
    quantity_ordered_floor,
    reconcile_work_orders_from_completion_evidence,
    record_short_declaration,
    release_first_ready_operation,
    report_reconcile_leg_failure,
    resolve_absolute_operation_quantity,
    stage_accounting,
    sync_work_order_quantity_complete,
    validate_operation_quantity,
    work_order_completion_bounds_from_evidence,
    work_order_operation_progress,
)
from app.services.work_order_template_service import templates_pointing_at_work_order
from app.services.work_order_timeline_service import list_work_order_timeline

logger = logging.getLogger(__name__)

router = APIRouter()


def _audit_reconcile_transitions(
    db: Session,
    current_user: User,
    transitions: list[StatusTransition],
    company_id: Optional[int] = None,
) -> None:
    """Emit a tamper-evident status-change audit row per reconcile-driven transition.

    AUD-3: reconcile-on-read drives operations/WOs to COMPLETE from durable
    TimeEntry evidence; those transitions were previously unaudited and could not
    be attributed (the reconcile has no actor). We thread the requesting user in
    and write one ``log_status_change`` per transition with the contributing
    TimeEntry ids in ``extra_data``. ``AuditService.log`` already swallows its own
    failures and only flushes (never commits), and this block is additionally
    wrapped so the read stays resilient even on an unexpected error.
    """
    if not transitions:
        return
    try:
        audit = AuditService(db, current_user)
        unaudited: list[StatusTransition] = []
        for tr in transitions:
            entry = audit.log_status_change(
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
                    # Owner ruling 1 (INT3): an operation transition carries the
                    # {target, received, upstream_quantity_short} it was decided on.
                    **tr.extra_data,
                },
            )
            # ``AuditService.log`` absorbs its own failures and returns None (e.g. a
            # ``sequence_number`` collision that outlasts its savepoint retries), so a lost
            # row never raises into the ``except`` below. Collect it and report it there.
            if entry is None:
                unaudited.append(tr)
            # QG-4 (partial): a completion can happen on a GET via reconcile. Record
            # at minimum the inspection_incomplete exception (cheapest gate, no extra
            # query). NCR/FAI/blocker gates are evaluated on the live completion path,
            # not the read path -- documented partial coverage.
            if tr.resource_type == "work_order_operation":
                record_reconcile_inspection_exception(db, operation_id=tr.resource_id, audit=audit, user=current_user)
        if unaudited:
            report_reconcile_leg_failure(
                logger,
                "audit",
                AuditWriteError(f"{len(unaudited)} reconcile status-change audit row(s) could not be written"),
                company_id=company_id,
                user=current_user,
                transitions=unaudited,
                summary=(
                    "Reconcile-on-read audit leg: AuditService could not write every status-change row "
                    "(it absorbed the failure); the read continues"
                ),
            )
    except Exception as exc:  # reads must never 500 on an audit failure -- but it must be seen
        report_reconcile_leg_failure(
            logger, "audit", exc, company_id=company_id, user=current_user, transitions=transitions
        )
    # KNOWN GAP (invariant 2). Follow-up: write these rows with ``AuditService.log_required``
    # inside a savepoint and let a failure abort the whole reconcile. Until then the caller
    # commits AFTER this leg returns, so a status flip can commit with its audit row missing
    # in two ways:
    #   * ``AuditService.log`` absorbed the failure (the ``unaudited`` report above). Its
    #     savepoint keeps the session usable after a sequence collision, so the commit
    #     succeeds without the row;
    #   * a non-database exception raised in this leg (e.g. by the inspection-exception
    #     hook) ended the loop early, and the flips commit without the rows not yet written.
    # Only a failure that leaves the session unusable rolls the flips back with their audit
    # rows (the commit then fails). Each case above is reported; that report is the only trace.


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
    so AI/realtime consumers aren't blind to reconcile-driven completions. IN-PROCESS
    ONLY: we do NOT fire outbound notifications/webhooks from a GET/reconcile path
    (a read must not have outbound side-effects; rank 12 will move reconcile to a
    debounced ARQ job, at which point the outbound dispatch can move with it).
    Best-effort and tenant-scoped (``emit`` validates the WO/op belong to
    ``company_id``); wrapped so a signal failure never 500s a read.
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
        # FULLY best-effort: a parent-advance failure must never 500 a GET.
        _emit_reconcile_parent_advance(db, company_id, current_user, transitions)
    except Exception as exc:  # reads must never 500 on an event-emit failure -- but it must be seen
        report_reconcile_leg_failure(
            logger, "events", exc, company_id=company_id, user=current_user, transitions=transitions
        )


def _emit_reconcile_parent_advance(
    db: Session,
    company_id: int,
    current_user: User,
    transitions: list[StatusTransition],
) -> None:
    """Record the G1 parent-children-complete signal for reconcile-driven WO completions.

    Mirror of the shop_floor helper. For each ``work_order`` -> COMPLETE transition,
    load the WO (company-scoped, not soft-deleted, has a parent) and, if its last laser
    child just completed, leave the tamper-evident audit row + ``child_work_orders_complete``
    event. Best-effort: wrapped so it can never 500 a GET; joined to this read's unit of
    work (the caller commits); tenant-scoped via ``company_id``. Same no-double-fire
    reasoning as the live paths (all-children-terminal becomes true exactly once).
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
    except Exception as exc:  # reads must never 500 on a parent-advance failure -- but it must be seen
        report_reconcile_leg_failure(
            logger, "parent_advance", exc, company_id=company_id, user=current_user, transitions=transitions
        )


def _refresh_reconcile_scheduling(db: Session, company_id: int, transitions: list[StatusTransition]) -> None:
    """Refresh cached work-center availability for reconcile-driven WO completions (MS-2).

    A reconcile-on-read WO -> COMPLETE drops its ops out of the scheduled-load query,
    so the persisted ``work_center.availability_rate`` would otherwise stay understated.
    ``StatusTransition.work_center_ids`` carries the affected WCs for each WO transition.
    Tenant-scoped (``SchedulingService(db, company_id)``); ``commit=False`` so the
    refresh joins THIS read's unit of work and is committed/rolled back atomically by
    the caller. Best-effort: a scheduling-refresh failure must never 500 a GET.
    """
    work_center_ids = sorted({wc for tr in transitions for wc in tr.work_center_ids if wc})
    if not work_center_ids:
        return
    try:
        SchedulingService(db, company_id).update_availability_rates(
            work_center_ids=work_center_ids, horizon_days=90, commit=False
        )
    except Exception as exc:  # reads must never 500 on a scheduling refresh -- but it must be seen
        report_reconcile_leg_failure(
            logger, "scheduling", exc, company_id=company_id, user=None, transitions=transitions
        )


def _apply_reconcile_inventory_effects(
    db: Session,
    company_id: int,
    current_user: User,
    work_orders: list[WorkOrder],
    transitions: list[StatusTransition],
) -> None:
    """FG receipt + gated backflush for reconcile-driven WO completions (Batch 6 / rank 9).

    Mirror of the shop_floor helper: a WO that completes implicitly on a list/detail
    GET via reconcile must move inventory the SAME way the live paths do (INV-1/INV-2).
    READ-SAFE / best-effort (wrapped so it can never 500 a GET) and IDEMPOTENT (a prior
    WO RECEIVE / component ISSUE short-circuits it). Joined to THIS read's unit of work
    (the caller commits) and tenant-scoped via ``company_id``.
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
    except Exception as exc:  # reads must never 500 on an inventory-effect failure -- but it must be seen
        report_reconcile_leg_failure(
            logger, "inventory_effects", exc, company_id=company_id, user=current_user, transitions=transitions
        )


def _apply_reconcile_cost_rollup(
    db: Session,
    company_id: int,
    current_user: User,
    work_orders: list[WorkOrder],
    transitions: list[StatusTransition],
) -> None:
    """Labor hour + cost + JobCost rollup for reconcile-driven WO completions (Batch 7).

    Mirror of the shop_floor helper (COST-4): a WO that completes implicitly on a
    list/detail GET must roll labor hours/cost the SAME way the live paths do. ALL of the
    Batch-7 rollup -- the evidence-sourced HOUR rollup AND the cost/JobCost rollup -- is
    gated behind ``LABOR_COST_ROLLUP_ENABLED`` so the OPT-IN flag governs cost surfacing
    consistently: flag-OFF, a reconcile completion surfaces NO computed Batch-7
    hours/cost (matching the live paths, which also gate the hour rollup); flag-ON, both
    paths roll up identically. (The pre-existing clock_out hour accumulation is a separate
    mechanism and is unaffected.) READ-SAFE (wrapped) + idempotent; joined to this read's
    unit of work; tenant-scoped.
    """
    completed_wo_ids = {tr.resource_id for tr in transitions if tr.resource_type == "work_order"}
    if not completed_wo_ids:
        return
    rollup_enabled = is_labor_cost_rollup_enabled(company_id)
    try:
        audit = AuditService(db, current_user)
        for work_order in work_orders:
            if work_order.id in completed_wo_ids:
                # Batch-7 hour rollup is now flag-gated on the reconcile path too (it was
                # previously unconditional). apply_completion_cost_rollup is itself a
                # no-op when the flag is OFF, but we hoist the same guard so the NEW hour
                # rollup never runs flag-OFF either -- keeping cost/hours surfacing
                # consistent across the live and reconcile paths.
                if rollup_enabled:
                    rollup_labor_hours_from_evidence(db, work_order)
                    apply_completion_cost_rollup(
                        db, work_order, company_id=company_id, user_id=current_user.id, audit=audit
                    )
                record_reconcile_labor_data_quality(
                    db, work_order=work_order, company_id=company_id, audit=audit, user=current_user
                )
    except Exception as exc:  # reads must never 500 on a cost-rollup failure -- but it must be seen
        report_reconcile_leg_failure(
            logger, "cost_rollup", exc, company_id=company_id, user=current_user, transitions=transitions
        )


def _reconcile_and_commit(db: Session, work_orders: list[WorkOrder], current_user: User, company_id: int) -> None:
    """Reconcile operation rows from completion evidence and commit, tolerating
    ANY failure of that best-effort write on a READ/list path.

    ``reconcile_work_orders_from_completion_evidence`` mutates version-mapped
    operation rows; committing that mutation can raise ``StaleDataError`` when
    another transaction bumped the same rows' version first. On a read that
    conflict is BENIGN -- the reconcile is idempotent and the other writer
    already persisted the truth -- so we roll the reconcile back (NOT a 409) and
    serve the read against the freshest committed state.

    Reconcile-on-read is a best-effort optimization, so this intentionally
    swallows ALL of its own commit failures, not just the version race. AUD-3:
    a write in one of the side-effect legs can fail in a way that leaves the session
    unusable (a database error inside a flush that no savepoint contains), so the
    subsequent ``db.commit()`` here raises ``PendingRollbackError`` /
    ``InvalidRequestError`` / ``IntegrityError`` rather than ``StaleDataError``. We
    catch ``SQLAlchemyError`` broadly, roll back, expire, and serve the read normally
    so a poisoned session can never turn a GET into a 500. Because the reconcile
    mutation and its audit rows share one unit of work, the rollback drops BOTH
    atomically -- no orphaned state change, no unaudited transition -- and the next
    read retries.

    That atomicity covers only a failure that leaves the session UNUSABLE. Two kinds of
    failure never reach this rollback, so the commit below still runs -- with whatever
    was written and without the rest: a non-database exception inside a side-effect leg
    (absorbed by the leg itself, logged + reported to Sentry via
    ``report_reconcile_leg_failure``), and an audit row ``AuditService.log`` failed to
    write -- it writes each row under a savepoint and absorbs its own failures, so a
    ``sequence_number`` collision that outlasts its retries no longer poisons the session
    and the flip commits without its row. See the KNOWN GAP note in
    ``_audit_reconcile_transitions``.

    (The root ``sequence_number`` race is a separately-tracked follow-up; this
    guard only guarantees reads never 500.)
    """
    transitions: list[StatusTransition] = []
    try:
        if reconcile_work_orders_from_completion_evidence(db, work_orders, transitions):
            _audit_reconcile_transitions(db, current_user, transitions, company_id)
            # EVT-4: in-process completion events for the materialized transitions
            # (NO outbound notify/webhook on a read -- see helper).
            _emit_reconcile_events(db, company_id, current_user, transitions)
            # MS-2: refresh cached work-center availability for reconcile-driven WO
            # completions, joined to this read's unit of work (commit=False).
            _refresh_reconcile_scheduling(db, company_id, transitions)
            # Batch 6 / rank 9 (INV-1/INV-2): FG receipt + gated backflush for any WO
            # this reconcile drove to COMPLETE. Read-safe (best-effort) + idempotent.
            _apply_reconcile_inventory_effects(db, company_id, current_user, work_orders, transitions)
            # Batch 7 / rank 10 (COST-4): labor hour rollup (monotonic-up) + OPT-IN
            # cost/JobCost rollup + no_labor_recorded signal. Read-safe + idempotent.
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


class WorkOrderPriorityUpdate(BaseModel):
    priority: int = Field(..., ge=1, le=10, description="Priority (1=highest, 10=lowest)")
    reason: Optional[str] = Field(None, max_length=500, description="Optional reason for priority change")


class LaserNestPreviewResponse(BaseModel):
    package_name: str
    nest_count: int
    total_planned_runs: int
    # Typed rows so the PDF extras (cnc_number / confidence / source_file) are
    # part of the contract while staying backward-compatible with CNC-file rows
    # (every extra field defaults). Rows arrive as dicts from ParsedLaserNest
    # .as_dict(); Pydantic validates/coerces them on construction.
    nests: list[LaserNestPreviewRow]
    # Bare-multi-page-PDF preview extras; all default None so ZIP/CNC/folder
    # previews validate unchanged. source_page_count is the uploaded PDF's page
    # count; skipped_pages lists pages the segmentation pass classified as
    # non-nest (cover/summary) pages; segmentation_warning surfaces a degraded
    # segmentation (one-nest-per-page fallback).
    source_page_count: Optional[int] = None
    segmentation_warning: Optional[str] = None
    skipped_pages: Optional[List[int]] = None
    # Sheet-stock suggestion roll-ups for the review grid's header banner. All
    # three default to 0, so every construction that passes no ``sheet_matches``
    # (notably the IMPORT response echo) reports zeros and its rows carry a null
    # ``sheet_suggestion`` -- the import contract is unchanged in substance.
    # These count ROWS, not parts: two nests proposing the same sheet are two.
    suggested_row_count: int = Field(
        0, description="Rows the deterministic gate pre-filled (``auto_fill_part_id`` set)."
    )
    shortlist_row_count: int = Field(
        0, description="Rows offering a shortlist but pre-filling nothing (``ambiguous`` with candidates)."
    )
    short_stock_row_count: int = Field(
        0,
        description=(
            "Rows whose claimed candidate ends ``short`` or ``none`` on stock, counting this package's "
            "cumulative demand. Advisory: short stock never blocks an import and never re-ranks a match."
        ),
    )


def _emit_work_order_event(
    db: Session,
    *,
    company_id: int,
    current_user: User,
    work_order: WorkOrder,
    event_type: str,
    severity: str = "info",
    payload: Optional[dict] = None,
) -> None:
    """Emit a WO-lifecycle OperationalEvent (released/started/completed/...).

    BEST-EFFORT: these events are telemetry attached to the status change, not the
    status change itself -- an event-store failure must never fail the release/
    start/complete that triggered it, so this routes through ``emit_best_effort``
    (which logs the failure with event type / WO id / company id and continues).
    """
    OperationalEventService(db).emit_best_effort(
        company_id=company_id,
        event_type=event_type,
        source_module="work_orders",
        entity_type="work_order",
        entity_id=work_order.id,
        work_order_id=work_order.id,
        user_id=current_user.id,
        severity=severity,
        event_payload={
            "work_order_number": work_order.work_order_number,
            "status": work_order.status.value if hasattr(work_order.status, "value") else work_order.status,
            **(payload or {}),
        },
    )


def _assert_work_center_in_company(db: Session, work_center_id: int, company_id: int) -> None:
    """Refuse an operation pointed at another tenant's work center.

    ``update_operation`` has always validated its work-center reassignment
    target against ``company_id``, but the two operation-CREATE paths
    (``create_work_order``'s inline operations list and ``add_operation``) built
    the row straight from ``**model_dump()`` and never checked. That let a
    caller in company B create an operation on their own work order pointed at
    company A's work center -- a cross-tenant WRITE whose downstream effect is a
    ``TimeEntry`` carrying company B's company_id and company A's
    work_center_id, which then corrupted company A's work-center utilization
    report. Flat 404 (not 403): a foreign work center must be indistinguishable
    from a nonexistent one.

    Deliberately does NOT require ``is_active``. ``update_operation`` does,
    because re-dispatching onto a retired machine is a planner error; creation
    legitimately happens against work centers that are momentarily inactive
    (routing-generated ops, imports), so requiring it here would be a behavior
    change beyond the security fix.
    """
    exists = (
        db.query(WorkCenter.id).filter(WorkCenter.id == work_center_id, WorkCenter.company_id == company_id).first()
    )
    if not exists:
        raise HTTPException(status_code=404, detail="Work center not found")


def _get_active_bom(db: Session, part_id: int, company_id: int, *, include_deleted: bool = False) -> Optional[BOM]:
    """THE "which BOM does this part build from" lookup — ten-odd call sites across work
    orders, job costing and the backflush engine.

    ``BOM.is_deleted`` is now part of the predicate by DEFAULT (invariant 3). ``BOM``
    carries ``SoftDeleteMixin`` and ``DELETE /bom/{bom_id}`` is now a SOFT delete, so
    without it a deleted BOM keeps resolving here and the structure the shop believes it
    deleted drives work-order release readiness, material requirements, job costing and the
    sub-assembly descent inside the backflush explosion. ``is_active`` alone is not a
    substitute: it is an independent flag a user can set either way, not a tombstone.

    ``include_deleted=True`` is a NARROW opt-in with exactly two callers, both of them the
    TOP-LEVEL entry points of the backflush resolver
    (``completion_inventory_service._resolve_backflush_demand`` and
    ``backflush_readiness_for_part``). They resolve the deleted row **only in order to
    refuse it out loud**: each one calls ``_record_bom_header_diagnostics`` immediately
    afterwards, which raises the BLOCKING ``deleted_active_bom`` diagnostic, so the
    structure is named and condemned rather than silently absent. Nothing issues stock down
    that path. Do NOT pass this anywhere else, and in particular not on the child-BOM
    descent, where no header diagnostic runs and a deleted sub-assembly would be exploded
    for real.

    What the opt-in does NOT do is change the outcome for a BOM deleted through the
    product's own verb. ``is_active == True`` stays in the predicate unconditionally and
    ``DELETE /bom/{bom_id}`` clears ``is_active`` alongside the tombstone, so such a row
    misses here either way and the backflush refuses with the generic — but equally
    BLOCKING — ``no_demand_source``. The opt-in covers the ``is_deleted=True,
    is_active=True`` shape a script or a fixture can leave behind. Both paths refuse; only
    the wording differs.
    """
    query = db.query(BOM).filter(
        BOM.part_id == part_id,
        BOM.company_id == company_id,
        BOM.is_active == True,  # noqa: E712
    )
    if not include_deleted:
        query = query.filter(BOM.is_deleted == False)  # noqa: E712
    return query.first()


def _collect_bom_components(
    db: Session,
    bom: BOM,
    company_id: int,
    parent_qty: float = 1.0,
    visited_part_ids: Optional[set[int]] = None,
) -> List[tuple[BOMItem, Part, float]]:
    """Return BOM components in multi-level order with quantity per parent assembly.

    Components are resolved through ``tenant_parts_by_id`` (invariant #1), never off
    the ``BOMItem.component_part`` relationship this used to ``joinedload``: that
    relationship joins on ``component_part_id`` alone and applies no ``company_id``
    predicate, so on a mis-parented line it materialises ANOTHER COMPANY's ``Part`` -- and
    everything downstream of this helper renders it, ``GET /work-orders/preview-operations``
    printing the foreign part number and name straight back to the caller. Scoping the
    LOOKUP means the foreign object is never materialised, rather than materialised and
    then carefully not printed. A component that does not resolve is skipped, which is what
    the ``if not component`` branch already did for a hard-deleted part row.
    """
    if visited_part_ids is None:
        visited_part_ids = {bom.part_id}

    items = (
        db.query(BOMItem)
        .filter(
            BOMItem.bom_id == bom.id,
            BOMItem.company_id == company_id,
        )
        .order_by(
            BOMItem.item_number.asc(),
            BOMItem.id.asc(),
        )
        .all()
    )
    # One scoped read per BOM level, not one per line -- the walk is recursive and hot.
    components_by_id = tenant_parts_by_id(db, [item.component_part_id for item in items], company_id)

    components: List[tuple[BOMItem, Part, float]] = []
    for item in items:
        component = components_by_id.get(item.component_part_id)
        if not component or component.id in visited_part_ids:
            continue

        qty = float(item.quantity or 1)
        scrap = float(item.scrap_factor or 0)
        extended_qty = qty * parent_qty * (1 + scrap)
        components.append((item, component, extended_qty))

        item_type = (item.item_type or "").lower()
        if item_type == "buy":
            continue

        child_bom = _get_active_bom(db, component.id, company_id)
        if child_bom:
            next_visited = set(visited_part_ids)
            next_visited.add(component.id)
            components.extend(
                _collect_bom_components(
                    db,
                    child_bom,
                    company_id,
                    parent_qty=extended_qty,
                    visited_part_ids=next_visited,
                )
            )

    return components


def _bom_required_quantities_by_component(
    db: Session,
    work_order: WorkOrder,
    company_id: int,
) -> tuple[dict[int, float], dict[str, int], dict[int, Part]]:
    bom = _get_active_bom(db, work_order.part_id, company_id)
    if not bom:
        return {}, {}, {}

    component_items = _collect_bom_components(db, bom, company_id)
    quantity_by_part_id: dict[int, float] = {}
    part_by_id: dict[int, Part] = {}
    part_id_by_number: dict[str, int] = {}
    work_order_qty = float(work_order.quantity_ordered or 0)

    for _, component, qty_per_assembly in component_items:
        required_qty = float(qty_per_assembly or 0) * work_order_qty
        quantity_by_part_id[component.id] = quantity_by_part_id.get(component.id, 0.0) + required_qty
        part_by_id[component.id] = component
        part_id_by_number[component.part_number.upper()] = component.id

    return quantity_by_part_id, part_id_by_number, part_by_id


def _reconcile_operation_component_quantities(
    db: Session,
    work_order: WorkOrder,
    company_id: int,
) -> bool:
    quantity_by_part_id, part_id_by_number, part_by_id = _bom_required_quantities_by_component(
        db,
        work_order,
        company_id,
    )
    if not quantity_by_part_id:
        return False

    changed = False
    for op in work_order.operations:
        component_part_id = op.component_part_id
        if not component_part_id and op.name and " - " in op.name:
            part_number_prefix = op.name.split(" - ", 1)[0].strip().upper()
            component_part_id = part_id_by_number.get(part_number_prefix)
            if component_part_id:
                op.component_part_id = component_part_id
                changed = True

        if not component_part_id or component_part_id not in quantity_by_part_id:
            continue

        required_qty = quantity_by_part_id[component_part_id]
        if float(op.component_quantity or 0) != required_qty:
            op.component_quantity = required_qty
            changed = True

        component = part_by_id.get(component_part_id)
        if component:
            op.component_part_number = component.part_number
            op.component_part_name = component.name

    return changed


def _enrich_work_order_operations(work_order: WorkOrder, *, db: Session, company_id: int) -> None:
    """Inject the computed, non-column attributes every work-order response renders.

    ``db``/``company_id`` are KEYWORD-ONLY and required: they exist for the hold-provenance
    read below, and a new call site has to state which tenant it is reading as rather than
    inherit one. ``company_id`` must be the ACTIVE company already resolved at the call site
    (``get_current_company_id``) -- never client input, never ``current_user.company_id``.
    """
    # HOLD PROVENANCE (why an operation is held, who held it, when). Read FIRST, and under
    # ``no_autoflush``, for two reasons that are correctness rather than style:
    #
    # 1. This function normalizes MAPPED columns below (quantity_complete, actual hours, ...).
    #    A query issued after that loop would autoflush those in-memory normalizations into
    #    the open transaction -- turning a render step into a write. This addition must stay a
    #    PURE READ: no writes, no audit rows, no events. ``get_work_order`` already runs (and
    #    commits) a reconcile of its own before calling us; nothing here may add to it.
    # 2. The lookup is skipped ENTIRELY when nothing is held, so the common path costs zero
    #    extra queries. A held set costs two, batched, regardless of how many rows are held.
    #
    # ``OperationStatus`` is a ``str`` enum, so this comparison is correct whether the loaded
    # value is the enum member or the raw column string.
    held_operation_ids = [
        op.id for op in work_order.operations if op.id is not None and op.status == OperationStatus.ON_HOLD
    ]
    hold_contexts: Dict[int, HoldContext] = {}
    # Query the cancellation marker independently of the relationship: rendering
    # hides that relationship with set_committed_value, including on repeated reads.
    cancelled_nests: Dict[int, int] = {}
    if held_operation_ids:
        with db.no_autoflush:
            cancelled_nests = dict(
                db.query(LaserNest.work_order_operation_id, LaserNest.id)
                .join(WorkOrderOperation, LaserNest.work_order_operation_id == WorkOrderOperation.id)
                .filter(
                    LaserNest.company_id == company_id,
                    LaserNest.is_deleted.is_(True),
                    WorkOrderOperation.company_id == company_id,
                    WorkOrderOperation.id.in_(held_operation_ids),
                )
                .all()
            )
            hold_contexts = hold_contexts_for_operations(db, company_id=company_id, operation_ids=held_operation_ids)

    # SHORT-COUNT EVIDENCE (owner ruling 1): one grouped, tenant-scoped TimeEntry read --
    # SUM(produced) and COUNT per operation -- for the work-order completion bounds below.
    # Under ``no_autoflush`` for the reason the hold read is: a render must never flush.
    with db.no_autoflush:
        labor_evidence = operation_labor_evidence(db, company_id, [op.id for op in work_order.operations])

    for op in work_order.operations:
        op.cancelled_nest_id = cancelled_nests.get(op.id)
        # Only an ON_HOLD row carries provenance; every other row is explicitly None rather
        # than left unset, so the response shape does not depend on attribute presence.
        #
        # The blocker's free text (title/note) rides this response. That is deliberate and it
        # is NOT the crew-station rule: ``shop_floor._hold_blocker_payload`` withholds those
        # two keys from a station principal -- an unattended, PIN-unlocked tablet with no
        # operator identity and no idle logout -- and this is the office page, reached only
        # through ``get_current_user``, which is the identified audience that gate already
        # exempts. Withholding the reason on the one screen built to act on it is the bug.
        op.hold_context = hold_contexts.get(op.id) if op.status == OperationStatus.ON_HOLD else None
        op.setup_time_hours = op.setup_time_hours or 0
        op.run_time_hours = op.run_time_hours or 0
        op.run_time_per_piece = op.run_time_per_piece or 0
        op.actual_setup_hours = op.actual_setup_hours or 0
        op.actual_run_hours = op.actual_run_hours or 0
        op.quantity_complete = op.quantity_complete or 0
        op.quantity_scrapped = op.quantity_scrapped or 0
        op.estimated_hours = float(op.setup_time_hours) + float(op.run_time_hours)
        op.actual_hours = float(op.actual_setup_hours) + float(op.actual_run_hours)
        op.work_center_name = op.work_center.name if op.work_center else None
        sync_laser_nest_from_operation(op)

        # Soft-delete guard + computed-field injection for the laser nest. A
        # soft-deleted nest must NEVER surface on a WorkOrderResponse, so hide
        # the relationship-backed attribute the schema validates off. We use
        # ``set_committed_value`` (NOT ``op.laser_nest = None``) on purpose: a
        # plain assignment dirties the ``uselist=False`` relationship and
        # back-populates ``nest.operation = None``, so any flush/commit that ran
        # after enrich in the same request would NULL the soft-deleted nest's
        # ``work_order_operation_id`` FK and corrupt traceability.
        # ``set_committed_value`` overrides the loaded value as if it came from
        # the DB -- it marks nothing dirty -- so the guard is safe regardless of
        # call order. For a live nest, inject has_document / document_file_name
        # as in-memory attrs (not ORM columns), like work_center_name above.
        nest = active_laser_nest(op)
        if nest is None:
            set_committed_value(op, "laser_nest", None)
        else:
            nest.has_document = bool(nest.document_id)
            nest.document_file_name = nest.document.file_name if nest.document else None

        if op.component_part_id:
            component = op.component_part
            if component:
                op.component_part_number = component.part_number
                op.component_part_name = component.name

    metrics = work_order_operation_progress(work_order)
    work_order.operation_count = metrics["operation_count"]
    work_order.operations_complete = metrics["operations_complete"]
    work_order.operation_progress_percent = metrics["operation_progress_percent"]

    _inject_short_count_figures(work_order, labor_evidence)


def _inject_short_count_figures(work_order: WorkOrder, labor_evidence: dict) -> None:
    """Owner ruling 1 read-model figures, in memory (no query; the evidence is batched).

    Per operation: ``upstream_quantity_short`` (target units that never reached it -- an
    upstream close in its chain declared them short), ``quantity_good_ceiling`` (the most good
    the server accepts) and ``quantity_unaccounted`` (received units neither good nor
    scrapped), from ``stage_accounting`` over the ACTIVE operations -- the one implementation.
    ``quantity_short_declared`` is the column itself.

    Per work order: ``quantity_deliverable`` (the most the floor's recorded good supports) and
    ``quantity_accounted_loss`` (units the floor already accounted as lost), from the SAME
    bounds the force-complete enforces, so the office modal's SHORT readout is the server's.
    """
    active_operations = active_work_order_operations(work_order)
    figures = stage_accounting(work_order, active_operations)
    for op in work_order.operations:
        figure = figures.get(op.id, StageFigures()) if op.id is not None else StageFigures()
        acct = OperationAccounting(
            target=operation_target_quantity(op, work_order),
            good=float(op.quantity_complete or 0),
            scrapped=float(op.quantity_scrapped or 0),
            chain_upstream=figure.chain_upstream,
            feed_short=figure.feed_short,
        )
        op.upstream_quantity_short = acct.upstream_short
        op.quantity_good_ceiling = acct.ceiling
        op.quantity_unaccounted = acct.unaccounted
    bounds = work_order_completion_bounds_from_evidence(work_order, active_operations, labor_evidence)
    work_order.quantity_deliverable = bounds.deliverable
    work_order.quantity_accounted_loss = bounds.floor_loss


def generate_work_order_number(db: Session, company_id: int = None) -> str:
    """Generate next work order number (WO-YYYYMMDD-XXX)

    Holds a Postgres advisory lock for the duration of the transaction so
    two concurrent creates can't read the same "last number" and produce
    duplicate work order numbers. No-op on non-Postgres (tests).
    """
    acquire_generator_lock(db, "work_order_number", company_id)

    today = datetime.now().strftime("%Y%m%d")
    prefix = f"WO-{today}-"

    query = db.query(WorkOrder).filter(WorkOrder.work_order_number.like(f"{prefix}%"))
    if company_id is not None:
        query = query.filter(WorkOrder.company_id == company_id)
    last_wo = query.order_by(WorkOrder.work_order_number.desc()).first()

    if last_wo:
        last_num = int(last_wo.work_order_number.split("-")[-1])
        new_num = last_num + 1
    else:
        new_num = 1

    return f"{prefix}{new_num:03d}"


def _resolve_laser_upload_root() -> str:
    preferred_dir = os.getenv("UPLOAD_DIR", "/app/uploads")
    try:
        root = os.path.join(preferred_dir, "laser_nest_packages")
        os.makedirs(root, exist_ok=True)
        return root
    except OSError:
        root = os.path.abspath(os.path.join(os.getenv("UPLOAD_DIR_FALLBACK", "./uploads"), "laser_nest_packages"))
        os.makedirs(root, exist_ok=True)
        return root


def _laser_work_center_preference(work_center: WorkCenter) -> int:
    """Rank a %laser% auto-detect candidate: lower is preferred.

    The DEFAULT laser for nest dispatch is the Ermaksan fiber laser, never the
    HSG tube laser (owner decision): (0) name/code/type mentions "ermaksan" or
    "fiber", (1) any other laser match, (2) anything mentioning "tube" last.
    """
    haystack = " ".join(
        value.lower() for value in (work_center.name, work_center.code, work_center.work_center_type) if value
    )
    # "tube" is checked FIRST so it wins even when the same name also says
    # "fiber" (tube lasers ARE fiber machines — 'HSG Fiber Tube Laser' must
    # never rank top tier). Mirrors laserDispatchTier in the frontend.
    if "tube" in haystack:
        return 2
    if "ermaksan" in haystack or "fiber" in haystack:
        return 0
    return 1


def _find_laser_work_center(db: Session, company_id: int, work_center_id: Optional[int] = None) -> WorkCenter:
    query = db.query(WorkCenter).filter(WorkCenter.company_id == company_id, WorkCenter.is_active == True)
    if work_center_id:
        work_center = query.filter(WorkCenter.id == work_center_id).first()
        if not work_center:
            raise HTTPException(status_code=404, detail="Laser work center not found")
        return work_center

    # Auto-detect: prefer the Ermaksan fiber laser over other lasers, and any
    # tube laser last (see _laser_work_center_preference); id is the
    # deterministic tiebreak within a preference tier.
    #
    # The candidate set must be keyed on the SAME tokens the tiering is, or the
    # preference is decided before it runs: tier 0 is "ermaksan"/"fiber", so a
    # prefilter demanding the literal "laser" drops an Ermaksan row spelled
    # "Ermaksan 6KW Bay 2" / "ERM-6K" with a non-laser work_center_type -- while
    # a tube laser typed `laser_cutting` sails through -- and min() then returns
    # the one machine this rule exists to avoid. That is the exact inversion the
    # frontend does not have: `laserDispatchTier` in
    # frontend/src/utils/laserWorkCenters.ts accepts "ermaksan"/"fiber" with no
    # "laser" token anywhere. These two sets must stay in step; edit them together.
    candidates = query.filter(
        or_(
            WorkCenter.name.ilike("%laser%"),
            WorkCenter.work_center_type.ilike("%laser%"),
            WorkCenter.code.ilike("%laser%"),
            WorkCenter.name.ilike("%ermaksan%"),
            WorkCenter.code.ilike("%ermaksan%"),
            WorkCenter.name.ilike("%fiber%"),
            WorkCenter.code.ilike("%fiber%"),
        )
    ).all()
    if not candidates:
        raise HTTPException(status_code=400, detail="No active laser work center found")
    return min(candidates, key=lambda wc: (_laser_work_center_preference(wc), wc.id))


def _laser_work_center_for_added_nest(
    db: Session,
    *,
    laser_work_order: WorkOrder,
    company_id: int,
    work_center_id: Optional[int] = None,
) -> WorkCenter:
    """Which laser a nest ADDED to an existing package runs on.

    Precedence: the caller's explicit choice, then **the machine this work order's
    own nests already run on**, then the shop-wide auto-detect.

    The middle step is the point. ``_find_laser_work_center`` answers "which laser
    does this SHOP prefer?", which is the right question when a package is first
    imported and the wrong one when a nest is appended to a job already running
    somewhere: the manual add shipped in #55 a month before #136 taught the import
    path about work-center assignment, so it kept asking the shop-wide question and
    silently dispatched the new nest to whatever machine auto-detect happened to
    return -- a different machine from the rest of the package, splitting one job
    across two lasers with nothing on screen saying so.

    The inherited machine is the package's MODAL work center, not its last one: a
    nest package is a dispatch pool and may legitimately span two lasers (see
    ``is_laser_dispatch_work_order``), so one nest deliberately moved to the tube
    laser must not drag every later addition after it. Ties break on the earliest
    sequence, so the answer is deterministic. ``is_active`` is deliberately NOT
    filtered here: this is "the same machine as the rest of the job", and a work
    center holding live work cannot be deactivated in the first place.

    The incumbent is read off the nests themselves -- ``LaserNest`` joined to its
    operation -- and deliberately NOT off ``operation_group == "LASER"``. A nest
    operation derives its group from ITS work center via ``get_work_center_group``,
    which keys on the literal token "LASER" in the machine's type or name, so a
    nest running on an Ermaksan row spelled "Ermaksan 6KW Bay 2" / type
    ``fabrication`` carries group "OTHER". Keying on the group would therefore
    find no incumbent and fall through to auto-detect under **exactly** the data
    shape that makes auto-detect wrong -- the fix would miss the case it exists
    for. ``import_nest_package``'s wipe filter already had to be widened for this
    same reason; it is the precedent, not a new concern. Soft-deleted nests are
    excluded so a legacy ON_HOLD tombstone cannot out-vote the live nests.
    """
    if work_center_id:
        return _find_laser_work_center(db, company_id, work_center_id)

    incumbent = (
        db.query(WorkOrderOperation.work_center_id)
        .join(LaserNest, LaserNest.work_order_operation_id == WorkOrderOperation.id)
        .filter(
            WorkOrderOperation.company_id == company_id,
            WorkOrderOperation.work_order_id == laser_work_order.id,
            WorkOrderOperation.work_center_id.isnot(None),
            LaserNest.company_id == company_id,
            LaserNest.is_deleted == False,  # noqa: E712
        )
        .group_by(WorkOrderOperation.work_center_id)
        .order_by(func.count(WorkOrderOperation.id).desc(), func.min(WorkOrderOperation.sequence).asc())
        .first()
    )
    if incumbent is not None:
        work_center = (
            db.query(WorkCenter).filter(WorkCenter.id == incumbent[0], WorkCenter.company_id == company_id).first()
        )
        if work_center is not None:
            return work_center

    # No nests yet (the first one on a fresh laser WO): fall back to the shop-wide
    # preference, which is what the import path uses for the same "no incumbent" case.
    return _find_laser_work_center(db, company_id)


def _find_nest_material_part(db: Session, company_id: int, part_id: int) -> Part:
    """Resolve a nest row's ``material_part_id`` to a live, company-scoped part.

    Tenant isolation is the whole point (invariant #1): an unscoped
    ``db.query(Part).get()`` would let a planner tie -- and then deplete -- another
    company's material. A miss is a **404, never a 403**, so a part id cannot be
    probed for existence across tenants. Soft-deleted parts are excluded: a tie to a
    deleted part would advertise demand nothing can satisfy.

    Since it also resolves the part that a nest's tie will DEPLETE at completion
    (invariant 6), it carries the third predicate too: the part may not be one the shop
    PRODUCES. ``assert_part_is_tieable_material`` refuses a manufactured part or an
    assembly with **422** -- the part exists and the caller may see it, so what is refused
    is its ROLE, not its existence. The check lives here rather than at the call sites
    because this is the ONE resolver behind both nest-tie doors (the laser-package import
    and the manual nest create), and a gate on one of two doors is not a gate. It runs
    before the resolver returns, so no caller can mutate anything with a rejected part.
    """
    part = tenant_query(db, Part, company_id).filter(Part.id == part_id, Part.is_deleted == False).first()  # noqa: E712
    if part is None:
        raise HTTPException(status_code=404, detail="Material part not found")
    assert_part_is_tieable_material(part)
    return part


# Byte cap on laser-package uploads (ZIP or bare PDF), enforced while the body
# streams to the temp file -- BEFORE any pypdf or AI work touches it. Matches
# the nginx client_max_body_size posture (50M) so the app-layer guard holds on
# deployments (Railway) that have no fronting proxy limit.
LASER_UPLOAD_MAX_BYTES = 50 * 1024 * 1024


async def _save_upload_to_temp(file: UploadFile, max_bytes: Optional[int] = None) -> str:
    # Resolved at call time (not def time) so the module-level cap stays the
    # single tunable source of truth.
    limit = max_bytes if max_bytes is not None else LASER_UPLOAD_MAX_BYTES
    suffix = os.path.splitext(file.filename or "")[1] or ".zip"
    fd, temp_path = tempfile.mkstemp(suffix=suffix)
    written = 0
    try:
        with os.fdopen(fd, "wb") as handle:
            while chunk := file.file.read(1024 * 1024):
                written += len(chunk)
                if written > limit:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Upload exceeds the {limit // (1024 * 1024)} MB limit. "
                        "Split the package into smaller batches.",
                    )
                handle.write(chunk)
    except Exception:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise
    finally:
        await file.close()
    return temp_path


def _laser_package_name(file: Optional[UploadFile], source_path: Optional[str]) -> str:
    if file and file.filename:
        return file.filename
    if source_path:
        return os.path.basename(os.path.normpath(source_path)) or "Laser nest package"
    return "Laser nest package"


def _ensure_laser_child_work_order(
    db: Session,
    *,
    parent_work_order: WorkOrder,
    company_id: int,
) -> WorkOrder:
    # Serialize child-laser-WO creation per parent. Without this, two simultaneous
    # laser-nest imports -- or a manual-add racing an import -- could both miss the
    # SELECT below and each create a duplicate LASER_CUTTING child under one
    # assembly. A transaction-scoped Postgres advisory lock keyed on the (globally
    # unique) parent WO id forces the race-loser to block until the winner's
    # surrounding atomic_transaction commits; the loser's SELECT then finds the
    # committed child and returns it, so the INSERT never double-fires. Released
    # automatically on commit/rollback; no-op on SQLite (tests). This is the sole
    # creation point for laser child WOs, so locking here covers both the import
    # and manual-entry paths. (A partial unique index on
    # (company_id, parent_work_order_id) WHERE work_order_type='laser_cutting' would
    # be a DB-level backstop, but is deferred -- it needs a pre-flight de-dup audit
    # before it can be safely added to live multi-tenant data.)
    acquire_generator_lock(db, f"laser_child_work_order:{parent_work_order.id}", company_id)

    # is_deleted == False is load-bearing, not hygiene. Without it a parent-addressed
    # import or manual add resolved a SOFT-DELETED laser child and rebuilt it -- the
    # caller's very next act force-sets RELEASED and re-derives the quantities, so a
    # deleted work order silently came back to life on the shop floor with none of the
    # restore path's controls (no audited `restore` action, and the tie re-open in
    # `reopen_allocations_cancelled_by_delete` never ran, so its material demand stayed
    # cancelled while the work order ran). The direct-address route already refuses
    # this -- `_load_parent_work_order` filters soft-deleted rows and 404s -- so this
    # was the one door left open.
    child = (
        db.query(WorkOrder)
        .filter(
            WorkOrder.company_id == company_id,
            WorkOrder.parent_work_order_id == parent_work_order.id,
            WorkOrder.work_order_type == WorkOrderType.LASER_CUTTING.value,
            WorkOrder.is_deleted == False,  # noqa: E712
        )
        .first()
    )
    if child:
        return child

    # A soft-deleted child exists but no live one: REFUSE rather than silently create a
    # second laser child alongside the deleted one. Creating one would fork the parent's
    # nest history in two and leave the deleted work order's operations, nests and
    # material ties stranded; 409 (the state conflicts with the request) names the work
    # order and the one remedy that keeps that history in one place.
    deleted_child = (
        db.query(WorkOrder)
        .filter(
            WorkOrder.company_id == company_id,
            WorkOrder.parent_work_order_id == parent_work_order.id,
            WorkOrder.work_order_type == WorkOrderType.LASER_CUTTING.value,
            WorkOrder.is_deleted == True,  # noqa: E712
        )
        .order_by(WorkOrder.id.desc())
        .first()
    )
    if deleted_child is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"The laser work order for {parent_work_order.work_order_number} "
                f"({deleted_child.work_order_number}) was deleted. It must be restored before "
                "importing or adding nests, so its nests and material ties stay on one work order. "
                "Restoring requires an admin or manager (POST /work-orders/{id}/restore)."
            ),
        )

    child = WorkOrder(
        company_id=company_id,
        work_order_number=generate_work_order_number(db, company_id),
        part_id=parent_work_order.part_id,
        parent_work_order_id=parent_work_order.id,
        work_order_type=WorkOrderType.LASER_CUTTING.value,
        # A nest package is a DISPATCH POOL by construction, so pin the flag to the
        # pooled value rather than inherit the sequenced create-default. It is inert
        # either way (is_laser_dispatch_work_order short-circuits above it at every
        # seam), but a laser WO serializing sequential_operations=true would put a
        # claim on the screen that its own behavior contradicts.
        sequential_operations=False,
        quantity_ordered=1,
        status=WorkOrderStatus.RELEASED,
        priority=parent_work_order.priority,
        due_date=parent_work_order.due_date,
        customer_name=parent_work_order.customer_name,
        customer_po=parent_work_order.customer_po,
        notes=f"Laser cutting child work order for {parent_work_order.work_order_number}",
    )
    db.add(child)
    db.flush()
    return child


def _resolve_laser_target(
    db: Session,
    *,
    work_order: WorkOrder,
    company_id: int,
) -> tuple[Optional[WorkOrder], WorkOrder]:
    """Resolve the (parent, laser) WO pair for the ``{work_order_id}`` nest endpoints.

    Classic flow: the addressed WO is an assembly parent -> find-or-create its
    LASER_CUTTING child via ``_ensure_laser_child_work_order`` (which takes the
    per-parent advisory lock).

    Generalized flow: the addressed WO is ITSELF ``work_order_type='laser_cutting'``
    (a standalone nest WO, or a laser child addressed directly) -> operate on it
    directly instead of nesting another child under it. The same advisory-lock
    discipline applies: lock the laser WO's own key so a concurrent import /
    manual add targeting it serializes, and ALSO its parent's key (when it has
    one) so a parent-addressed import racing a child-addressed import serializes
    too. Lock order (parent key first, then own key) matches the classic flow's
    single parent-key acquisition, so the two flows cannot deadlock.

    Returns ``(parent_work_order_or_None, laser_work_order)`` -- parent is None
    for standalone nest WOs.
    """
    if work_order.work_order_type == WorkOrderType.LASER_CUTTING.value:
        if work_order.parent_work_order_id:
            acquire_generator_lock(db, f"laser_child_work_order:{work_order.parent_work_order_id}", company_id)
        acquire_generator_lock(db, f"laser_child_work_order:{work_order.id}", company_id)
        return work_order.parent_work_order, work_order
    return work_order, _ensure_laser_child_work_order(db, parent_work_order=work_order, company_id=company_id)


def _load_parent_work_order(db: Session, work_order_id: int, company_id: int) -> WorkOrder:
    """Load the WO addressed by a ``{work_order_id}`` laser-nest endpoint.

    In the classic flow this is the parent assembly WO; since the standalone-nest
    generalization it may also be a laser-cutting WO itself (see
    ``_resolve_laser_target``).
    """
    work_order = (
        db.query(WorkOrder)
        .options(joinedload(WorkOrder.part))
        .filter(
            WorkOrder.id == work_order_id,
            WorkOrder.company_id == company_id,
            WorkOrder.is_deleted == False,  # noqa: E712 - never rebuild/RELEASE a soft-deleted WO
        )
        .first()
    )
    if not work_order:
        raise HTTPException(status_code=404, detail="Work order not found")
    return work_order


def _laser_wo_audit_values(work_order: WorkOrder) -> dict:
    """WO-level audit snapshot for the laser-nest write paths.

    The import/manual endpoints force-set ``status`` (keeping an on-hold job on hold),
    stamp the release they perform and rewrite the quantity fields on the target laser
    WO; these are the fields whose old->new change the WO-level ``log_update`` records
    (invariant 2).
    """
    status_value = work_order.status.value if hasattr(work_order.status, "value") else work_order.status
    return {
        "status": status_value,
        "released_at": to_utc_iso(work_order.released_at),
        "released_by": work_order.released_by,
        "quantity_ordered": float(work_order.quantity_ordered or 0),
        "quantity_complete": float(work_order.quantity_complete or 0),
        "quantity_scrapped": float(work_order.quantity_scrapped or 0),
    }


def _release_laser_work_order_for_nests(work_order: WorkOrder, *, released_by: int) -> None:
    """The nest routes' force-RELEASE of their laser work order, recorded as a release.

    Import Nest Package and the manual nest add put the laser work order on the floor
    RELEASED -- that is their contract (a nest package is born released). Two rules on
    top of the plain status write:

    * an **ON_HOLD** job stays ON_HOLD. The usual reason to hold a package is bad nests,
      and the fix is a re-import; silently lifting the hold here put the corrected (or
      still-bad) nests straight back on the floor. The rebuilt nest operations are then
      refused by every start verb until someone runs ``POST /work-orders/{id}/resume``.
      ON_HOLD is non-terminal, so new material ties stay consumable.
    * moving the job INTO RELEASED stamps ``released_by`` / ``released_at`` when it was
      DRAFT or carries no stamp yet (a freshly created child or standalone job included):
      the importer IS who authorized it, and the resume verb reads the stamp as the
      release record. An existing stamp on a RELEASED / IN_PROGRESS job is preserved, so
      a corrective re-import never rewrites who first released the job.

    Callers snapshot ``_laser_wo_audit_values`` BEFORE calling this so the WO-level
    ``log_update`` records the status and the stamp.
    """
    if work_order.status == WorkOrderStatus.ON_HOLD:
        return
    if work_order.status == WorkOrderStatus.DRAFT or work_order.released_at is None:
        work_order.released_by = released_by
        work_order.released_at = datetime.utcnow()
    work_order.status = WorkOrderStatus.RELEASED


# The closed vocabulary for how each imported row's sheet part came to be chosen:
#
#   ``auto``    -- the server suggested it and the planner CONFIRMED it in the
#                  review grid's accept dialog.
#   ``planner`` -- the planner chose it themselves (per row or package-wide),
#                  whether or not a suggestion was on offer.
#   ``prefill`` -- carried forward from the tie the nest already had, on re-import.
#
# Only COMMITTED ties are described. A row the planner left untied, and a
# suggestion they never confirmed, both import without a tie -- so there is no
# decision to record and the client omits them rather than inventing a value.
# Anything else is dropped (see ``_parse_sheet_match_provenance``).
#
# Keep in lock-step with ``PROVENANCE_BY_TIE_SOURCE`` in
# ``frontend/src/components/laser/LaserNestImportWizard.tsx`` -- the client maps
# its richer internal ``TieSource`` (which also models the uncommitted states)
# down to exactly these three on the way out.
_SHEET_MATCH_PROVENANCE_VALUES = frozenset({"auto", "planner", "prefill"})

# A source_file is a relative path inside the uploaded package; LaserNestImportRow
# already caps it at 1000, but the audit log is append-only and hash-chained, so
# what lands THERE gets the tighter, realistic filename bound.
_MAX_PROVENANCE_KEY_CHARS = 255


def _parse_sheet_match_provenance(raw: Optional[str], known_source_files: Optional[Set[str]] = None) -> Dict[str, str]:
    """Decode the import's optional ``sheet_match_provenance`` form field.

    OBSERVATIONAL ONLY. This is a client-reported breadcrumb -- how the planner
    says each row's sheet was chosen -- recorded on the WO-level audit row so the
    "did the suggestions actually get used" question is answerable later. It MUST
    NEVER participate in resolution and MUST NEVER create a tie: ties come from
    the validated ``rows`` (``material_part_id``) and nothing else. Because it is
    untrusted client input that only ever lands in ``extra_data``, a malformed or
    hostile value is DISCARDED rather than 400'd -- refusing an import over a
    telemetry field would punish the planner for a wizard bug.

    THE AUDIT LOG IS APPEND-ONLY AND HASH-CHAINED, so everything that reaches it
    from here is bounded on BOTH axes, not just one:

    * VALUES are filtered to the closed vocabulary above, so no free text lands.
    * KEYS are capped in count (``LASER_PDF_PACKAGE_MAX``) *and* in length
      (``_MAX_PROVENANCE_KEY_CHARS``) *and* intersected with the ``source_file``
      values of the rows actually being imported. Count alone was not enough: 50
      keys at the old 1000-char truncation is ~50 KB of caller-chosen text
      permanently appended to the tenant's immutable quality record, per call,
      with nothing able to redact it.

    The intersection also makes the breadcrumb HONEST rather than merely bounded.
    Without it a client could assert ``{"nest_07.pdf": "auto"}`` for a row it
    imported untied, or for a file not in the package at all, and the audit row
    would record the claim as fact -- in the row someone reads when asking why
    the wrong lot was depleted.
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("Discarding malformed sheet_match_provenance on laser-nest import")
        return {}
    if not isinstance(parsed, dict):
        logger.warning("Discarding non-object sheet_match_provenance on laser-nest import")
        return {}
    provenance: Dict[str, str] = {}
    dropped = 0
    for source_file, value in parsed.items():
        if len(provenance) >= LASER_PDF_PACKAGE_MAX:
            break
        if (
            isinstance(source_file, str)
            and len(source_file) <= _MAX_PROVENANCE_KEY_CHARS
            and (known_source_files is None or source_file in known_source_files)
            and isinstance(value, str)
            and value in _SHEET_MATCH_PROVENANCE_VALUES
        ):
            provenance[source_file] = value
        else:
            dropped += 1
    if dropped:
        # Logged, not silent: a client sending a value outside the vocabulary is
        # far more likely to be a wizard/server vocabulary DRIFT than an attack,
        # and a drift that produces an empty audit field is otherwise invisible
        # until someone asks the question this breadcrumb exists to answer.
        logger.warning(
            "Dropped %s sheet_match_provenance entr%s outside the accepted vocabulary %s",
            dropped,
            "y" if dropped == 1 else "ies",
            sorted(_SHEET_MATCH_PROVENANCE_VALUES),
        )
    return provenance


def _suggestion_to_row_value(suggestion: Any) -> dict:
    """Serialize one ``SheetSuggestion`` dataclass for the wire.

    ``dataclass_asdict`` walks the nested ``CandidatePart`` / ``MatchDiagnostic``
    dataclasses too, so the only hand-work is DROPPING ``alloy_score``: it is the
    matcher's internal alloy-agreement weight behind ``score``, not part of the
    contract, and nothing downstream may start reading it. Dropping it here (not
    in the schema) means it can never reach the wire even if a caller dumps the
    dict directly.
    """
    payload = dataclass_asdict(suggestion)
    for candidate in payload.get("candidates") or []:
        candidate.pop("alloy_score", None)
    return payload


def _claimed_candidate(suggestion: Any) -> Optional[Any]:
    """The candidate a row's stock annotation was computed against.

    Mirrors ``_annotate_stock``: the pre-filled part when the deterministic gate
    named one, otherwise the top of the shortlist. Exactly one candidate per row
    is ever stock-annotated, and this is it.
    """
    candidates = suggestion.candidates or []
    if not candidates:
        return None
    if suggestion.auto_fill_part_id is not None:
        return next((c for c in candidates if c.part_id == suggestion.auto_fill_part_id), None)
    return candidates[0]


def _build_laser_preview_response(
    package_name: str,
    nests: list[dict],
    *,
    source_page_count: Optional[int] = None,
    segmentation_warning: Optional[str] = None,
    skipped_pages: Optional[List[int]] = None,
    sheet_matches: Optional[dict] = None,
) -> LaserNestPreviewResponse:
    """Assemble a preview response, optionally carrying sheet-stock suggestions.

    ``sheet_matches`` is ``{source_file: SheetSuggestion}`` from the matcher, or
    None. When it is None NOTHING changes: the row dicts are passed through by
    reference and the three roll-ups stay 0 -- which is what the IMPORT response
    echo relies on. When it is present the rows are SHALLOW-COPIED before the
    ``sheet_suggestion`` key is set, so the caller's dicts (and the
    ``ParsedLaserNest`` values behind them) are never mutated.
    """
    rows = nests
    suggested = shortlist = short_stock = 0
    if sheet_matches:
        enriched: list[dict] = []
        for nest in nests:
            row = dict(nest)
            suggestion = sheet_matches.get(row.get("source_file"))
            if suggestion is not None:
                row["sheet_suggestion"] = _suggestion_to_row_value(suggestion)
                if suggestion.auto_fill_part_id is not None:
                    suggested += 1
                elif suggestion.status == STATUS_AMBIGUOUS and suggestion.candidates:
                    shortlist += 1
                claimed = _claimed_candidate(suggestion)
                if claimed is not None and claimed.stock_state in ("short", "none"):
                    short_stock += 1
            enriched.append(row)
        rows = enriched

    return LaserNestPreviewResponse(
        package_name=package_name,
        nest_count=len(nests),
        total_planned_runs=sum(int(nest.get("planned_runs") or 0) for nest in nests),
        nests=rows,
        source_page_count=source_page_count,
        segmentation_warning=segmentation_warning,
        skipped_pages=skipped_pages,
        suggested_row_count=suggested,
        shortlist_row_count=shortlist,
        short_stock_row_count=short_stock,
    )


# Bounded fan-out for the per-PDF AI extraction. extract_nest_fields_from_pdf is
# sync/blocking (now up to TWO sequential LLM calls per file), so each call is
# dispatched to the threadpool; the semaphore caps concurrent in-flight
# extractions. Module-level -- i.e. PROCESS-GLOBAL, shared across requests --
# so concurrent previews can't multiply up to the shared anyio threadpool's
# ~40 tokens and starve every other sync endpoint for the LLM-call duration.
# (asyncio.Semaphore binds its loop lazily on 3.10+, so creating it at import
# time is safe; the app runs a single event loop.)
_LASER_PDF_EXTRACT_CONCURRENCY = 5
_laser_pdf_extract_semaphore = asyncio.Semaphore(_LASER_PDF_EXTRACT_CONCURRENCY)


async def _parse_laser_nest_pdf_package_async(
    folder: str,
    company_id: int,
    *,
    cnc_hints: Optional[dict[str, str]] = None,
    filename_is_cnc_hint: bool = True,
) -> list[ParsedLaserNest]:
    """Parallelized counterpart to ``parse_laser_nest_pdf_package``.

    Globs the PDFs here (enforcing the same cap), then runs the per-file AI
    extraction concurrently via ``run_in_threadpool`` under the process-global
    semaphore. Returns rows in stable (sorted-path) order. The sync helper is
    kept for the offline path and tests; this one is what the async endpoint
    uses for latency.

    The bare-PDF path passes ``filename_is_cnc_hint=False`` (its files carry
    synthetic ``nest-pNNN`` split names, which must not be offered to the model
    as CNC numbers) plus optional per-file ``cnc_hints`` keyed by rel path,
    sourced from the segmentation pass.
    """
    root = Path(folder).expanduser().resolve()
    pdf_paths = sorted(p for p in root.rglob("*.pdf") if p.is_file())
    if not pdf_paths:
        raise ValueError("No PDF files found in package")
    if len(pdf_paths) > LASER_PDF_PACKAGE_MAX:
        raise ValueError(
            f"Package has {len(pdf_paths)} PDFs; the limit is {LASER_PDF_PACKAGE_MAX}. "
            "Split the package into smaller batches."
        )

    async def _extract(path: Path) -> ParsedLaserNest:
        rel_path = str(path.relative_to(root))
        async with _laser_pdf_extract_semaphore:
            result = await run_in_threadpool(
                functools.partial(
                    extract_nest_fields_from_pdf,
                    str(path),
                    path.name,
                    company_id,
                    cnc_hint=(cnc_hints or {}).get(rel_path),
                    filename_is_cnc_hint=filename_is_cnc_hint,
                )
            )
        return build_parsed_nest_from_extraction(result, abs_path=str(path), rel_path=rel_path)

    # return_exceptions=True so one bad PDF can't sink the whole preview batch
    # (the documented anti-goal). extract_nest_fields_from_pdf is itself
    # never-raise, so this is belt-and-suspenders for an unexpected raise in the
    # threadpool dispatch / row assembly: a failed task degrades to a
    # filename-only ParsedLaserNest with a low confidence, in stable path order.
    results = await asyncio.gather(*(_extract(path) for path in pdf_paths), return_exceptions=True)
    nests: list[ParsedLaserNest] = []
    for path, result in zip(pdf_paths, results):
        if isinstance(result, Exception):
            logger.warning("Laser-nest preview extraction failed for %s: %s", path.name, result)
            rel_path = str(path.relative_to(root))
            nests.append(
                build_parsed_nest_from_extraction(
                    {"cnc_number": None, "extraction_confidence": "low"},
                    abs_path=str(path),
                    rel_path=rel_path,
                )
            )
        else:
            nests.append(result)
    return nests


async def _preview_nests_from_folder(folder: str, company_id: int) -> list[dict]:
    """Detect package shape and return preview rows as dicts.

    PDF package -> parallel AI extraction; otherwise the legacy CNC-file parser
    (sync; run off the event loop). Raises ``ValueError`` (empty/over-cap) for
    the caller to translate into a 400.
    """
    if package_has_pdfs(folder):
        nests = await _parse_laser_nest_pdf_package_async(folder, company_id)
    else:
        nests = await run_in_threadpool(parse_laser_nest_folder, folder)
    return [nest.as_dict() for nest in nests]


async def _read_bare_pdf_page_count_or_400(temp_path: str) -> int:
    """Page count of an uploaded bare PDF, with the shared 400 translations.

    Unreadable bytes are a client problem (not a 500); the page cap reuses the
    package cap -- one segmented multi-page PDF costs the same per-nest AI
    fan-out as a ZIP of that many PDFs, so it gets the same ceiling.
    """
    try:
        page_count = await run_in_threadpool(get_pdf_page_count, temp_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Could not read the PDF") from exc
    if page_count > LASER_PDF_PACKAGE_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"PDF has {page_count} pages; the limit is {LASER_PDF_PACKAGE_MAX}. "
            "Split the PDF into smaller batches.",
        )
    return page_count


def _confirmed_pdf_segments(rows: list[LaserNestImportRow]) -> list[list[int]]:
    """Derive the deterministic split plan for a bare-PDF import from its rows.

    The commit path re-splits the re-sent PDF by the rows' CONFIRMED page lists
    -- no AI, no re-segmentation -- so every row must carry ``source_pages``,
    and its ``source_file`` must equal the name the split will deterministically
    derive for those pages. A mismatch means the rows came from a different
    preview (or were hand-mangled): reject as a stale preview rather than
    attaching the wrong pages to a nest. Out-of-range and duplicate segments
    are rejected downstream by ``split_pdf_segments`` (ValueError -> 400).

    Rows must also be page-DISJOINT: segmentation guarantees every page lands in
    at most one nest, so overlapping-but-distinct ranges (e.g. [1,2] and [2,3])
    can only come from a hand-crafted payload -- without this check one source
    page would silently land in two nests' Documents.
    """
    segments: list[list[int]] = []
    claimed_pages: set[int] = set()
    for row in rows:
        if not row.source_pages:
            raise HTTPException(
                status_code=400,
                detail="Each nest row must include source_pages for a bare-PDF import. "
                "Preview the PDF first, then confirm the rows.",
            )
        expected_name = segment_file_name(list(row.source_pages))
        if row.source_file.strip() != expected_name:
            raise HTTPException(
                status_code=400,
                detail=f"Nest row source_file '{row.source_file}' does not match its source_pages "
                f"(expected '{expected_name}'). The preview is stale; re-run it and confirm again.",
            )
        overlap = claimed_pages.intersection(row.source_pages)
        if overlap:
            raise HTTPException(
                status_code=400,
                detail=f"Nest rows overlap on page(s) {sorted(overlap)}; each PDF page may belong to "
                "at most one nest. The preview is stale; re-run it and confirm again.",
            )
        claimed_pages.update(row.source_pages)
        segments.append(list(row.source_pages))
    return segments


def _resolve_package_pdf(package_dir: str, source_file: str) -> str:
    """Resolve a confirmed row's ``source_file`` to an absolute PDF path inside
    ``package_dir``, rejecting path traversal (mirrors ``_safe_extract_zip``).

    Raises ``ValueError`` if the path escapes the package or the file is missing.
    """
    root = Path(package_dir).resolve()
    target = (root / source_file).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Invalid nest source path: {source_file}") from exc
    if not target.is_file():
        raise ValueError(f"Nest source file not found in package: {source_file}")
    return str(target)


def _build_confirmed_pdf_nests(package_dir: str, rows: list[LaserNestImportRow]) -> list[ParsedLaserNest]:
    """Build ParsedLaserNest objects from confirmed wizard rows (no AI re-call).

    Rows are already-validated ``LaserNestImportRow`` models (the raw JSON was
    parsed through Pydantic at the endpoint, so ``planned_runs`` is a positive
    int and all strings are length-bounded). The re-sent ZIP only supplies the
    PDF bytes; the persisted field values are the planner-confirmed ones.

    Duplicate ``source_file`` values are rejected: two rows pointing at the same
    PDF would double-create nests/Documents and trip ``uq_laser_nests_package_file``
    as an uncaught 500. Raises ``ValueError`` (-> 400) on a repeat.
    """
    if not rows:
        raise ValueError("No nest rows were provided for import")

    nests: list[ParsedLaserNest] = []
    seen_source_files: set[str] = set()
    for row in rows:
        source_file = row.source_file.strip()
        if not source_file:
            raise ValueError("Each nest row must include a source_file")
        if source_file in seen_source_files:
            raise ValueError(f"Duplicate nest source file in import rows: {source_file}")
        seen_source_files.add(source_file)
        abs_path = _resolve_package_pdf(package_dir, source_file)
        cnc_number = (row.cnc_number or "").strip() or None
        nest_name = (row.nest_name or "").strip() or cnc_number or Path(source_file).stem
        nests.append(
            ParsedLaserNest(
                nest_name=nest_name,
                cnc_file_name=Path(source_file).name,
                cnc_file_path=source_file,
                planned_runs=row.planned_runs,
                material=row.material,
                thickness=row.thickness,
                sheet_size=row.sheet_size,
                cnc_number=cnc_number,
                pdf_source_path=abs_path,
                confidence=row.confidence,
                # Per-row work-center override (import-side instruction; resolved
                # and validated in _run_laser_nest_import before the atomic build).
                work_center_id=row.work_center_id,
                # Per-row material tie (import-side instruction; the part is resolved
                # and tenant-validated in _run_laser_nest_import before the atomic
                # build). No fuzzy match off row.material -- an explicit pick only.
                material_part_id=row.material_part_id,
                qty_per_run=row.qty_per_run,
            )
        )
    return nests


@router.get("/browse", response_model=WorkOrderBrowseResponse)
def browse_orders(
    response: Response,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    status: Optional[WorkOrderStatus] = None,
    search: Optional[str] = Query(None, max_length=200),
    customer: Optional[str] = Query(None, max_length=255),
    hide_cots: bool = True,
    scope: Optional[Literal["overdue", "due_today"]] = None,
    sort: Literal["work_order_number", "part", "customer", "due_date", "priority", "status"] = "priority",
    direction: Literal["asc", "desc"] = "asc",
    group: Literal["none", "customer", "part", "status"] = "none",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Server-filtered page plus exact persisted-record counts; never reconciles on read."""
    require_workspace_access(db, current_user, company_id, "work-orders")
    response.headers["Cache-Control"] = "no-store"
    return browse_work_orders(
        db,
        company_id,
        skip=skip,
        limit=limit,
        status=status,
        search=search,
        customer=customer,
        hide_cots=hide_cots,
        scope=scope,
        sort=sort,
        direction=direction,
        group=group,
    )


@router.get("/", response_model=List[WorkOrderSummary])
def list_work_orders(
    response: Response,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=5000),
    status: Optional[WorkOrderStatus] = None,
    search: Optional[str] = None,
    include_deleted: bool = Query(False, description="Include soft-deleted work orders (admin only)"),
    deleted_only: bool = Query(
        False,
        description="Return ONLY soft-deleted work orders (the restore view). Default false = live WOs only.",
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """List work orders with summary info.

    ``deleted_only=true`` inverts the soft-delete filter and returns ONLY the
    company's soft-deleted work orders, each carrying ``is_deleted`` / ``deleted_at`` /
    ``deleted_by_name`` so the caller can decide whether to restore it (via
    ``POST /work-orders/{id}/restore``, ADMIN/MANAGER). Without this a deleted work order
    is invisible to every API caller and nothing can be restored.

    UNLIKE the PO and vendor restore views, this one DOES carry its own role gate:
    ADMIN/MANAGER, mirroring exactly the population ``require_role`` admits on
    ``DELETE /work-orders/{id}`` and ``POST /work-orders/{id}/restore`` (superusers and
    platform admins included). A work-order archive is not a view of rows the same
    reader could already see -- the *live* list is role-open to every authenticated
    user, including operators, so leaving the archive ungated would put every deleted
    job in front of a population that can neither delete nor restore one. The gate is an
    explicit in-handler check rather than a router dependency because it applies to this
    ONE parameter; the endpoint dependency stays ``get_current_user``.

    ``deleted_only`` WINS over ``include_deleted`` if both are passed -- it is the
    narrower, explicit view, and the union is not a thing anybody asked for. Note that
    ``include_deleted`` itself is unchanged: an ADMIN passing it still gets live AND
    deleted rows mixed together, and those rows still report ``is_deleted: null``, i.e.
    they stay indistinguishable from live ones in the payload exactly as they are today.
    Non-null provenance means "came from the restore view", nothing else.
    """
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"

    # The archive gate. Mirrors ``deps.require_role([UserRole.ADMIN, UserRole.MANAGER])``
    # clause for clause (deps.py) rather than hard-coding the two roles: it admits
    # ``is_superuser`` whatever the role column says, and PLATFORM_ADMIN, BOTH before the
    # role test. Miss either and the same person who can DELETE a work order gets 403'd
    # from the only view that would let them undo it. Detail string kept byte-identical to
    # require_role's so an archive refusal is indistinguishable from a delete/restore one.
    #
    # NOT ``status.HTTP_403_FORBIDDEN``: this handler declares a query param named
    # ``status``, which shadows the fastapi ``status`` module for the whole function body
    # -- that expression raises AttributeError and serves 500 instead of 403, and only
    # when the gate actually fires.
    if deleted_only and not (
        current_user.is_superuser
        or current_user.role == UserRole.PLATFORM_ADMIN
        or current_user.role in (UserRole.ADMIN, UserRole.MANAGER)
    ):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # Tenancy is unconditional and unchanged -- company_id from get_current_company_id
    # scopes BOTH views. ``deleted_only`` only ever flips the is_deleted predicate; it can
    # never widen the tenant scope.
    #
    # The joined part deliberately carries NO ``is_deleted`` predicate (it never did): a
    # deleted work order can legitimately point at a soft-deleted part, and blanking
    # part_number/part_name on the archive would erase exactly the identifying information
    # a reader uses to decide whether to restore the job.
    query = (
        db.query(WorkOrder)
        .filter(WorkOrder.company_id == company_id)
        .options(
            joinedload(WorkOrder.part),
            selectinload(WorkOrder.operations).selectinload(WorkOrderOperation.laser_nest),
        )
    )

    if deleted_only:
        # The restore view. Tested FIRST so it wins over ``include_deleted``: folding it
        # into the branch below would hand an ADMIN who passed both the UNION (live +
        # deleted) instead of the archive. The one-line
        # ``is_deleted == (True if deleted_only else False)`` form the PO/vendor twins use
        # does not transplant here, because this endpoint's false branch carries the extra
        # include_deleted/ADMIN condition.
        query = query.filter(WorkOrder.is_deleted == True)  # noqa: E712
    # Filter out soft-deleted unless explicitly requested by admin
    elif not include_deleted or current_user.role != UserRole.ADMIN:
        query = query.filter(WorkOrder.is_deleted == False)

    if status:
        query = query.filter(WorkOrder.status == status)
    elif not deleted_only:
        # Default: exclude complete/closed/cancelled (only show active work orders)
        #
        # DELIBERATELY SKIPPED on the deleted view -- do not "tidy" this back into a plain
        # ``else``. A soft-deleted work order can sit in ANY status (WorkOrder.soft_delete
        # leaves ``status`` untouched), and a COMPLETE- or CANCELLED-then-deleted job is
        # among the likeliest things somebody wants back. Applying this exclusion here
        # would hide those rows from the ONLY list that can see them, so the restore
        # control could never be offered for them and nothing else in the API would reach
        # them either: an archive that hides finished jobs is empty exactly when someone
        # needs it. An explicit ``?status=`` still narrows the deleted view -- that is the
        # branch above, which both views share, as ``search`` below likewise does.
        query = query.filter(
            WorkOrder.status.not_in([WorkOrderStatus.COMPLETE, WorkOrderStatus.CLOSED, WorkOrderStatus.CANCELLED])
        )

    if search:
        search_filter = f"%{search}%"
        query = query.outerjoin(Part, WorkOrder.part_id == Part.id)
        query = query.filter(
            or_(
                WorkOrder.work_order_number.ilike(search_filter),
                WorkOrder.customer_name.ilike(search_filter),
                WorkOrder.customer_po.ilike(search_filter),
                WorkOrder.lot_number.ilike(search_filter),
                WorkOrder.unit_number.ilike(search_filter),
                Part.part_number.ilike(search_filter),
                Part.name.ilike(search_filter),
            )
        )

    # ``WorkOrder.id`` is a TIEBREAKER, not decoration -- do not tidy it away. priority and
    # due_date tie constantly in a real shop (a whole day's jobs share both), and this
    # endpoint is paged with OFFSET/LIMIT by every caller -- the frontend walks it in
    # 500-row windows, on BOTH views. Postgres guarantees no row order at all among tied
    # sort keys, so two OFFSET windows over one tied block can repeat a work order and
    # drop another silently. Pre-existing exposure on the live list, hardened here rather
    # than a bug the deleted view introduced; the archive only made it reachable from a
    # second screen.
    work_orders = query.order_by(WorkOrder.priority, WorkOrder.due_date, WorkOrder.id).offset(skip).limit(limit).all()
    # Reconcile-on-read: a concurrent-write conflict here is benign (idempotent),
    # so it must NOT 500 the list -- _reconcile_and_commit swallows StaleDataError.
    # AUD-3: terminal reconcile-driven transitions are audited to the requesting user.
    #
    # NOT RUN on the deleted view, and that carve-out is load-bearing. The reconcile
    # filters only TERMINAL_WO_STATUSES (work_order_state_service) and carries NO
    # is_deleted predicate anywhere -- it has never been handed a deleted row because this
    # list never returned one. Run it over the archive and merely OPENING the restore
    # screen would drive a soft-deleted RELEASED/IN_PROGRESS job to COMPLETE from stale
    # labor evidence, write audit rows, refresh scheduling, and fire the FG receipt +
    # gated backflush -- inventory movements against a job somebody deleted, caused by a
    # GET, with no actor intent and no reason recorded. The archive is a read of the
    # tombstones, not a resumption of the work; the reconcile resumes when the row is
    # restored and shows up on the live list again.
    if not deleted_only:
        _reconcile_and_commit(db, work_orders, current_user, company_id)

    # Resolve deleted_by -> display name in ONE batched query, and ONLY for the deleted
    # view. SoftDeleteMixin.deleted_by is a bare Integer column with no FK/relationship,
    # so there is nothing to joinedload and the alternative is a lookup per row. On the
    # default path this dict stays empty, NO query is emitted at all, and the loop below
    # reads None out of it -- that is what keeps the unset parameter inert.
    #
    # Not company-scoped, on purpose: deleted_by is whoever's session performed the
    # delete, and a platform admin acting inside this company is not a user row of it, so
    # scoping would blank exactly the name a reader most needs. The id is read off our own
    # already-tenant-scoped row and never comes from the caller, so this cannot be steered
    # into enumerating another tenant's users. It discloses nothing new either:
    # AuditService already snapshots the same actor's full_name onto this tenant's
    # audit_log row for the very same delete.
    #
    # It also deliberately applies NO ``is_active`` / ``is_deleted`` filter to User:
    # provenance must survive the deleter's own departure. Adding one would silently
    # regress the name to "Unknown" for exactly the deletes people ask about most -- the
    # ones done by someone who has since left.
    #
    # User.full_name is a plain Python property, not a mapped column, so it cannot be
    # SELECTed -- pull first_name/last_name and join them here.
    deleted_by_names: dict[int, str] = {}
    if deleted_only:
        deleter_ids = {wo.deleted_by for wo in work_orders if wo.deleted_by is not None}
        if deleter_ids:
            for user_id, first_name, last_name in db.query(User.id, User.first_name, User.last_name).filter(
                User.id.in_(deleter_ids)
            ):
                name = f"{first_name or ''} {last_name or ''}".strip()
                if name:
                    deleted_by_names[user_id] = name

    result = []
    for wo in work_orders:
        metrics = work_order_operation_progress(wo)
        # Hand-built kwarg by kwarg, and it MUST stay that way. WorkOrderSummary sets
        # from_attributes, and is_deleted/deleted_at are real SoftDeleteMixin columns on
        # WorkOrder -- so "simplifying" this into WorkOrderSummary.model_validate(wo)
        # would make every LIVE row answer ``is_deleted: false``, killing the tri-state
        # silently and putting a Restore control on jobs nobody deleted.
        summary = WorkOrderSummary(
            id=wo.id,
            # Carried so the list's inline due-date edit can PUT with the lock held.
            # Same kwarg-by-kwarg hazard as sequential_operations below: omitted, every
            # row would ship the schema default (0) and every inline edit would 409.
            version=wo.version,
            work_order_number=wo.work_order_number,
            part_id=wo.part_id,
            parent_work_order_id=wo.parent_work_order_id,
            work_order_type=wo.work_order_type,
            # Explicit: this summary is built kwarg-by-kwarg, so an omitted field silently
            # ships the schema default (False = pooled) for every sequenced work order.
            sequential_operations=wo.sequential_operations,
            unit_number=wo.unit_number,
            part_number=wo.part.part_number if wo.part else None,
            part_name=wo.part.name if wo.part else None,
            part_type=wo.part.part_type.value if wo.part and wo.part.part_type else None,
            status=wo.status,
            priority=wo.priority,
            quantity_ordered=wo.quantity_ordered,
            quantity_complete=wo.quantity_complete,
            operation_count=metrics["operation_count"],
            operations_complete=metrics["operations_complete"],
            operation_progress_percent=metrics["operation_progress_percent"],
            due_date=wo.due_date,
            customer_name=wo.customer_name,
            customer_po=wo.customer_po,
            po_line_item=wo.po_line_item,
            # Left at their None defaults on every other path -- see WorkOrderSummary.
            # Off the deleted view the ternary short-circuits on ``deleted_only``, so
            # ``.get()`` never runs and no lookup dict was ever built; that is what keeps
            # the unset parameter inert, not the emptiness of the dict. ON the archive a
            # dict MISS -- a row whose deleted_by is NULL (pre-mixin rows), or a deleter
            # whose name came back blank and was skipped when the dict was built -- yields
            # None, i.e. "somebody deleted this, we cannot say who", not an error.
            is_deleted=wo.is_deleted if deleted_only else None,
            deleted_at=wo.deleted_at if deleted_only else None,
            deleted_by_name=(deleted_by_names.get(wo.deleted_by) if deleted_only else None),
        )
        result.append(summary)

    return result


@router.get("/preview-operations/{part_id}")
def preview_work_order_operations(
    part_id: int,
    quantity: float = 1,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Preview what operations would be generated for a part (for debugging)"""
    part = db.query(Part).filter(Part.id == part_id, Part.company_id == company_id).first()
    if not part:
        raise HTTPException(status_code=404, detail="Part not found")
    bom = _get_active_bom(db, part_id, company_id)
    has_bom = bom is not None

    result = {
        "part_id": part_id,
        "part_number": part.part_number,
        "part_type": part.part_type.value,
        "is_assembly": part.part_type == PartType.ASSEMBLY or has_bom,
        "quantity": quantity,
        "bom_found": False,
        "bom_status": None,
        "bom_items_count": 0,
        "component_routings": [],
        "operations_preview": [],
    }

    if has_bom:
        # Check for BOM
        if bom:
            result["bom_found"] = True
            result["bom_status"] = bom.status

            component_items = _collect_bom_components(db, bom, company_id)
            result["bom_items_count"] = len(component_items)

            component_ids = [component.id for _, component, _ in component_items]
            routings_by_part_id = {}
            if component_ids:
                routings = (
                    db.query(Routing)
                    .options(selectinload(Routing.operations).selectinload(RoutingOperation.work_center))
                    .filter(
                        Routing.company_id == company_id,
                        Routing.part_id.in_(set(component_ids)),
                        Routing.is_active == True,
                        Routing.status == "released",
                    )
                    .all()
                )
                routings_by_part_id = {r.part_id: r for r in routings}

            quantity_by_component_id: dict[int, float] = {}
            for _, component, component_qty_per_assembly in component_items:
                quantity_by_component_id[component.id] = quantity_by_component_id.get(component.id, 0.0) + (
                    float(component_qty_per_assembly or 0) * float(quantity or 0)
                )

            previewed_component_part_ids = set()
            for item, component, component_qty_per_assembly in component_items:
                # Check for routing
                routing = routings_by_part_id.get(component.id)
                total_component_qty = quantity_by_component_id.get(
                    component.id,
                    float(component_qty_per_assembly or 0) * float(quantity or 0),
                )

                comp_info = {
                    "part_id": component.id,
                    "part_number": component.part_number,
                    "quantity_per": float(item.quantity),
                    "total_qty": total_component_qty,
                    "has_routing": routing is not None,
                    "routing_status": routing.status if routing else None,
                    "routing_operations": [],
                }

                if routing and component.id not in previewed_component_part_ids:
                    previewed_component_part_ids.add(component.id)
                    for op in sorted(routing.operations, key=lambda operation: operation.sequence):
                        if op.is_active:
                            work_center = op.work_center
                            comp_info["routing_operations"].append(
                                {"sequence": op.sequence, "name": op.name, "work_center_id": op.work_center_id}
                            )
                            result["operations_preview"].append(
                                {
                                    "name": f"{component.part_number} - {op.name}",
                                    "work_center_id": op.work_center_id,
                                    "work_center_name": work_center.name if work_center else "Unknown",
                                    "setup_hours": op.setup_hours,
                                    "run_hours_per_unit": op.run_hours_per_unit,
                                    "setup_instructions": op.setup_instructions,
                                    "run_instructions": op.work_instructions,
                                    "requires_inspection": op.is_inspection_point,
                                    "component_part_id": component.id,
                                    "component_part_number": component.part_number,
                                    "component_quantity": total_component_qty,
                                    "operation_group": get_work_center_group(work_center) if work_center else None,
                                }
                            )

                result["component_routings"].append(comp_info)

            assembly_routing = (
                db.query(Routing)
                .options(selectinload(Routing.operations).selectinload(RoutingOperation.work_center))
                .filter(
                    Routing.company_id == company_id,
                    Routing.part_id == part_id,
                    Routing.is_active == True,
                    Routing.status == "released",
                )
                .first()
            )

            if assembly_routing:
                active_assembly_ops = [
                    op for op in sorted(assembly_routing.operations, key=lambda op: op.sequence) if op.is_active
                ]
                non_inspection_ops = [op for op in active_assembly_ops if not _is_inspection_operation(op)]
                inspection_ops = [op for op in active_assembly_ops if _is_inspection_operation(op)]

                for op in non_inspection_ops + inspection_ops:
                    work_center = op.work_center
                    result["operations_preview"].append(
                        {
                            "name": op.name,
                            "work_center_id": op.work_center_id,
                            "work_center_name": work_center.name if work_center else "Unknown",
                            "setup_hours": op.setup_hours,
                            "run_hours_per_unit": op.run_hours_per_unit,
                            "setup_instructions": op.setup_instructions,
                            "run_instructions": op.work_instructions,
                            "requires_inspection": op.is_inspection_point,
                            "component_part_id": None,
                            "component_part_number": part.part_number,
                            "component_quantity": quantity,
                            "operation_group": get_work_center_group(work_center) if work_center else None,
                        }
                    )

    return result


def _is_inspection_operation(operation: RoutingOperation) -> bool:
    if operation.is_inspection_point:
        return True

    inspection_tokens = ("INSPECT", "INSPECTION", "QUALITY", "QC")
    text_fields = (
        (operation.name or "").upper(),
        (operation.description or "").upper(),
    )
    if any(token in field for field in text_fields for token in inspection_tokens):
        return True

    work_center = operation.work_center
    if not work_center:
        return False

    wc_fields = (
        (work_center.name or "").upper(),
        (work_center.work_center_type or "").upper(),
    )
    return any(token in field for field in wc_fields for token in inspection_tokens)


@router.post("/", response_model=WorkOrderResponse, status_code=status.HTTP_201_CREATED)
def create_work_order(
    work_order_in: WorkOrderCreate,
    request: Request,
    auto_routing: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
):
    """Create a new work order. If auto_routing=True, operations are auto-generated from released routing."""

    # Initialize audit service
    audit = AuditService(db, current_user, request)

    # Verify part exists
    part = db.query(Part).filter(Part.id == work_order_in.part_id, Part.company_id == company_id).first()
    if not part:
        raise HTTPException(status_code=404, detail="Part not found")

    # Generate work order number
    wo_number = generate_work_order_number(db, company_id)

    # Create work order. serial_numbers (PR 4) is validated by the schema (unique,
    # non-empty, count == quantity_ordered) and stored to the existing JSON-in-Text
    # column — the shop-floor capture endpoints then key step records per serial.
    wo_data = work_order_in.model_dump(exclude={"operations", "serial_numbers"})
    work_order = WorkOrder(**wo_data, work_order_number=wo_number, created_by=current_user.id)
    if work_order_in.serial_numbers is not None:
        work_order.serial_numbers = json.dumps(work_order_in.serial_numbers)
    work_order.company_id = company_id
    db.add(work_order)
    db.flush()  # Get the work order ID

    # Auto-generate operations from routing if enabled and no operations provided

    process_sheet_snapshot: list[dict] = []
    if auto_routing and not work_order_in.operations:
        # Copies routed operations AND snapshots attached process sheets (PR 3) in this
        # same pre-commit unit of work — a family with no released revision raises a
        # structured 409 here and the whole creation (WO included) rolls back atomically.
        process_sheet_snapshot = create_routing_operations_for_work_order(
            db, work_order, part, float(work_order_in.quantity_ordered), company_id
        )
    else:
        # Create operations from input
        for op_data in work_order_in.operations:
            # Tenancy: the work center is caller-supplied, so it must be proven to
            # belong to the active company before it is written onto the row.
            _assert_work_center_in_company(db, op_data.work_center_id, company_id)
            operation = WorkOrderOperation(work_order_id=work_order.id, company_id=company_id, **op_data.model_dump())
            db.add(operation)

    db.commit()
    work_order = (
        db.query(WorkOrder)
        .options(
            joinedload(WorkOrder.part),
            selectinload(WorkOrder.operations).selectinload(WorkOrderOperation.component_part),
            selectinload(WorkOrder.operations).selectinload(WorkOrderOperation.work_center),
            selectinload(WorkOrder.operations)
            .selectinload(WorkOrderOperation.laser_nest)
            .selectinload(LaserNest.document),
        )
        .filter(WorkOrder.id == work_order.id, WorkOrder.company_id == company_id)
        .first()
    )
    # The WORK ORDER itself is already durably committed above. The reconcile
    # below mutates version-mapped operation rows and could (in theory) hit a
    # concurrent-version conflict on its commit; guard it in its OWN commit so a
    # StaleDataError rolls back ONLY the reconcile -- it must NOT drop the
    # creation audit row, which is committed atomically in the separate terminal
    # commit below. (For a brand-new WO the completion-evidence reconcile is a
    # no-op; this guard is defensive and keeps the POST off the 500 path.)
    try:
        _reconcile_operation_component_quantities(db, work_order, company_id)
        # AUD-3 N/A here: a brand-new WO has no TimeEntry evidence, so this reconcile
        # can drive no terminal status transition -- nothing to audit. Pass no
        # transitions accumulator to keep this the documented no-op it has always been.
        reconcile_work_orders_from_completion_evidence(db, [work_order])
        db.commit()
    except StaleDataError:
        db.rollback()
        db.expire_all()
        # Re-load the freshest committed state for the audit snapshot below.
        work_order = (
            db.query(WorkOrder)
            .options(
                joinedload(WorkOrder.part),
                selectinload(WorkOrder.operations).selectinload(WorkOrderOperation.component_part),
                selectinload(WorkOrder.operations).selectinload(WorkOrderOperation.work_center),
                selectinload(WorkOrder.operations)
                .selectinload(WorkOrderOperation.laser_nest)
                .selectinload(LaserNest.document),
            )
            .filter(WorkOrder.id == work_order.id, WorkOrder.company_id == company_id)
            .first()
        )

    # Audit log for work order creation. Logged BEFORE the terminal commit so the audit
    # row commits atomically with the work order — AuditService.log() only flushes, and
    # the request session never commits on teardown, so an audit call placed after the
    # final commit would be silently discarded.
    db.flush()  # ensure work_order (and any reconciled changes) are flushed; PK is real
    audit.log_create(
        resource_type="work_order",
        resource_id=work_order.id,
        resource_identifier=work_order.work_order_number,
        new_values=work_order,
        extra_data={
            "part_number": part.part_number,
            "quantity": float(work_order.quantity_ordered),
            "auto_routing": auto_routing,
            "operation_count": len(work_order.operations),
            # PR 3 traceability: which sheet families resolved to which released
            # revisions at snapshot time (empty when no operation carries a sheet).
            "process_sheet_snapshot": process_sheet_snapshot,
        },
    )
    db.commit()
    _enrich_work_order_operations(work_order, db=db, company_id=company_id)

    safe_broadcast(
        broadcast_dashboard_update,
        {
            "event": "work_order_created",
            "work_order_id": work_order.id,
            "status": work_order.status.value if hasattr(work_order.status, "value") else work_order.status,
        },
        company_id=company_id,
    )

    return work_order


@router.post(
    "/{work_order_id}/duplicate",
    response_model=WorkOrderDuplicateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Duplicate a work order as a new DRAFT job",
)
def duplicate_work_order_endpoint(
    work_order_id: int,
    payload: WorkOrderDuplicateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Re-run a job's PLAN without re-entering it.

    The motivating case is a laser nest package: 40+ nests confirmed once through the
    AI import wizard, re-run next month without re-uploading the PDFs or re-confirming
    a single row. The new work order copies the header, every operation, the nests
    (each pointing at the SAME drawing Document — the reference is copied, the blob is
    not) and the OPEN material ties, and lands in DRAFT so a planner reviews it before
    release. Process-sheet step snapshots are RE-taken from each sheet family's
    currently-released revision, so the duplicate's traveler gates completion exactly as
    the source's did.

    What is deliberately NOT copied is the production record — quantities, actual hours
    and cost, actual timestamps, lot/serial numbers, who released it, consumed material,
    lot pins. Copying any of it would fabricate history on a job that has not run.
    ``parent_work_order_id`` is not copied either: the duplicate is an INDEPENDENT work
    order, and re-attaching it to the source's assembly parent would add a second child
    against demand the first one already satisfied. The field-by-field decisions and
    their reasons live in ``services/work_order_duplicate_service``.

    The response is an ENVELOPE, not a bare work order: ``work_order`` plus
    ``skipped_operations`` / ``skipped_material_allocations``. Both lists are normally
    empty; when they are not, the duplicate is a valid draft that is MISSING something
    the source had, and the planner has to be told — a skipped material tie that nobody
    surfaces means the job runs and stock is never deducted.

    **404** when the source work order is not in the active company or is soft-deleted —
    never 403, and never a leak of "exists elsewhere". There is no status gate: the
    headline case is duplicating a COMPLETE job, so a terminal source is expected.

    **409** when the duplicate would mint a work order the create path would have
    rejected: the source's produced part has been deleted, or one of its operations
    references a process-sheet family with no released revision (structured detail,
    ``code: PROCESS_SHEET_UNAVAILABLE`` — byte-identical to ``POST /work-orders``).
    Nothing is written in either case.
    """
    source = (
        tenant_query(db, WorkOrder, company_id)
        .filter(
            WorkOrder.id == work_order_id,
            WorkOrder.is_deleted == False,  # noqa: E712
        )
        .first()
    )
    if not source:
        raise HTTPException(status_code=404, detail="Work order not found")

    try:
        # ONE unit of work: header, operations, nest package, nests, material ties and
        # every audit row commit together or not at all. A header without its nests must
        # not survive — that is a plan the planner never approved.
        with atomic_transaction(db):
            result = duplicate_work_order(
                db,
                source=source,
                quantity_ordered=float(payload.quantity_ordered),
                due_date=payload.due_date,
                company_id=company_id,
                user_id=current_user.id,
                audit=audit,
            )
            new_work_order_id = result.work_order.id
            # Read the skips off the result INSIDE the block: the objects survive the
            # commit, but the lists are the only record of them outside the audit chain.
            skipped_operations = list(result.skipped_operations)
            skipped_material_allocations = list(result.skipped_allocations)
    except IntegrityError as exc:
        # A uniqueness/constraint fault (a work-order-number race on a non-Postgres
        # deployment, a nest key collision) must not surface as a 500 on a poisoned
        # session. Nothing was committed.
        #
        # The message deliberately does NOT tell the planner to retry. Only ONE of the
        # faults that land here is transient — the work-order-number race, where a retry
        # picks the next number and succeeds. The rest (a nest key collision, a
        # violated CHECK on the copied data) are properties of the source work order and
        # will fail identically every time, so "try again" would send the planner into a
        # loop and hide a real data problem.
        logger.warning("Work order duplicate failed on a constraint error (source %s): %s", work_order_id, exc)
        raise HTTPException(
            status_code=409,
            detail="Could not duplicate this work order; a generated record conflicts with an existing one. "
            "If duplicating it again fails the same way, the source work order has data that cannot be "
            "copied — check its nests and material ties.",
        ) from exc

    work_order = (
        db.query(WorkOrder)
        .options(
            joinedload(WorkOrder.part),
            selectinload(WorkOrder.operations).selectinload(WorkOrderOperation.component_part),
            selectinload(WorkOrder.operations).selectinload(WorkOrderOperation.work_center),
            selectinload(WorkOrder.operations)
            .selectinload(WorkOrderOperation.laser_nest)
            .selectinload(LaserNest.document),
        )
        .filter(WorkOrder.id == new_work_order_id, WorkOrder.company_id == company_id)
        .first()
    )
    _enrich_work_order_operations(work_order, db=db, company_id=company_id)

    safe_broadcast(
        broadcast_dashboard_update,
        {
            "event": "work_order_created",
            "work_order_id": work_order.id,
            "status": work_order.status.value if hasattr(work_order.status, "value") else work_order.status,
        },
        company_id=company_id,
    )

    return WorkOrderDuplicateResponse(
        work_order=WorkOrderResponse.model_validate(work_order),
        skipped_operations=skipped_operations,
        skipped_material_allocations=skipped_material_allocations,
    )


def _create_assembly_routing_operations(
    db: Session,
    work_order: WorkOrder,
    wo_quantity: float,
    company_id: int = None,
) -> list[tuple[WorkOrderOperation, Optional[int]]]:
    """Create assembly operations from BOM component routings, then assembly routing.

    Returns (wo_operation, attached process_sheet_id) pairs so the caller can snapshot
    process-sheet steps onto the new operations (PR 3).
    """

    sequence = 10
    company_id = company_id or work_order.company_id
    operation_sheet_pairs: list[tuple[WorkOrderOperation, Optional[int]]] = []
    bom = _get_active_bom(db, work_order.part_id, company_id)

    if bom:
        component_items = _collect_bom_components(db, bom, company_id)
        component_ids = [component.id for _, component, _ in component_items]

        routings_by_part_id = {}
        if component_ids:
            routings = (
                db.query(Routing)
                .options(selectinload(Routing.operations).selectinload(RoutingOperation.work_center))
                .filter(
                    Routing.company_id == company_id,
                    Routing.part_id.in_(set(component_ids)),
                    Routing.is_active == True,
                    Routing.status == "released",
                )
                .all()
            )
            routings_by_part_id = {routing.part_id: routing for routing in routings}

        quantity_by_component_id: dict[int, float] = {}
        for _, component_for_qty, qty_per_assembly in component_items:
            quantity_by_component_id[component_for_qty.id] = quantity_by_component_id.get(component_for_qty.id, 0.0) + (
                float(qty_per_assembly or 0) * float(wo_quantity or 0)
            )

        created_component_part_ids = set()
        for _, component, component_qty_per_assembly in component_items:
            if component.id in created_component_part_ids:
                continue
            created_component_part_ids.add(component.id)
            routing = routings_by_part_id.get(component.id)
            if not routing:
                continue

            component_qty = quantity_by_component_id.get(
                component.id,
                float(component_qty_per_assembly or 0) * float(wo_quantity or 0),
            )
            for rop in sorted(routing.operations, key=lambda operation: operation.sequence):
                if not rop.is_active:
                    continue

                work_center = rop.work_center
                description_parts = []
                if rop.description:
                    description_parts.append(rop.description)
                description_parts.append(f"Part: {component.name}")
                description_parts.append(f"Qty: {component_qty:g}")

                wo_op = WorkOrderOperation(
                    work_order_id=work_order.id,
                    sequence=sequence,
                    # Bare identifier, never a display label -- see the note in
                    # create_routing_operations_for_work_order below.
                    operation_number=str(sequence),
                    name=f"{component.part_number} - {rop.name}",
                    description=" | ".join(description_parts),
                    work_center_id=rop.work_center_id,
                    setup_time_hours=rop.setup_hours,
                    run_time_hours=float(rop.run_hours_per_unit or 0) * component_qty,
                    setup_instructions=rop.setup_instructions,
                    run_instructions=rop.work_instructions,
                    requires_inspection=rop.is_inspection_point,
                    inspection_type="final" if _is_inspection_operation(rop) else None,
                    status=OperationStatus.PENDING,
                    component_part_id=component.id,
                    component_quantity=component_qty,
                    operation_group=get_work_center_group(work_center) if work_center else None,
                    company_id=company_id,
                )
                db.add(wo_op)
                operation_sheet_pairs.append((wo_op, rop.process_sheet_id))
                sequence += 10

    assembly_routing = (
        db.query(Routing)
        .options(selectinload(Routing.operations).selectinload(RoutingOperation.work_center))
        .filter(
            Routing.company_id == company_id,
            Routing.part_id == work_order.part_id,
            Routing.is_active == True,
            Routing.status == "released",
        )
        .first()
    )

    if not assembly_routing:
        return operation_sheet_pairs

    active_assembly_ops = [op for op in sorted(assembly_routing.operations, key=lambda x: x.sequence) if op.is_active]
    non_inspection_ops = [op for op in active_assembly_ops if not _is_inspection_operation(op)]
    inspection_ops = [op for op in active_assembly_ops if _is_inspection_operation(op)]

    for rop in non_inspection_ops + inspection_ops:
        work_center = rop.work_center
        wo_op = WorkOrderOperation(
            work_order_id=work_order.id,
            sequence=sequence,
            # Bare identifier, never a display label -- see the note in
            # create_routing_operations_for_work_order below.
            operation_number=str(sequence),
            name=rop.name,
            description=rop.description,
            work_center_id=rop.work_center_id,
            setup_time_hours=rop.setup_hours,
            run_time_hours=float(rop.run_hours_per_unit or 0) * wo_quantity,
            setup_instructions=rop.setup_instructions,
            run_instructions=rop.work_instructions,
            requires_inspection=rop.is_inspection_point,
            inspection_type="final" if _is_inspection_operation(rop) else None,
            status=OperationStatus.PENDING,
            operation_group=get_work_center_group(work_center) if work_center else None,
            company_id=company_id,
        )
        db.add(wo_op)
        operation_sheet_pairs.append((wo_op, rop.process_sheet_id))
        sequence += 10

    return operation_sheet_pairs


def create_routing_operations_for_work_order(
    db: Session,
    work_order: WorkOrder,
    part: Part,
    quantity: float,
    company_id: int,
) -> list[dict]:
    """Generate this work order's operations from the part's released routing.

    Single source of truth shared by POST /work-orders (auto_routing=True) and
    the A0.2 Excel-migration open-WO import (``migration_import_service``), so
    imported work orders get exactly the same routed operations as hand-entered
    ones. Assembly-aware: assemblies/BOM parts expand component routings first
    (``_create_assembly_routing_operations``); simple parts copy their released
    routing operations. No-op when no released routing exists (the caller
    decides whether that is an error).

    PR 3: routing operations with an attached process sheet get the sheet family's
    currently-RELEASED revision snapshotted into ``wo_operation_steps`` (both callers,
    inside the same pre-commit unit of work — atomic with the WO). A family with no
    released revision raises ``ProcessSheetUnavailableError`` (409) and the whole
    creation rolls back. Returns the snapshot summary for the WO-creation audit row.
    """
    has_bom = _get_active_bom(db, part.id, company_id) is not None
    if part.part_type == PartType.ASSEMBLY or has_bom:
        operation_sheet_pairs = _create_assembly_routing_operations(
            db, work_order, float(quantity), company_id=company_id
        )
        return process_sheet_service.snapshot_steps_for_work_order(db, company_id, operation_sheet_pairs)

    routing = (
        db.query(Routing)
        .options(selectinload(Routing.operations).selectinload(RoutingOperation.work_center))
        .filter(
            Routing.company_id == company_id,
            Routing.part_id == work_order.part_id,
            Routing.is_active == True,
            Routing.status == "released",
        )
        .first()
    )
    if not routing:
        return []

    operation_sheet_pairs: list[tuple[WorkOrderOperation, Optional[int]]] = []
    for rop in sorted(routing.operations, key=lambda x: x.sequence):
        if not rop.is_active:
            continue
        work_center = rop.work_center
        wo_op = WorkOrderOperation(
            work_order_id=work_order.id,
            sequence=rop.sequence,
            # ``operation_number`` is an IDENTIFIER column, not a display label: store the
            # bare sequence ("10"). Every UI adds the "Op " prefix at render time
            # (frontend/src/utils/operationLabel.ts::formatOperationLabel), so minting the
            # prefix here rendered "Op Op 10" on the kiosk (WO-20260807-006). Only a
            # fallback -- a routing operation that carries its own number is copied verbatim.
            #
            # FORWARD-ONLY BY DESIGN -- do NOT write a data migration to backfill this.
            # Rows written before this change keep "Op 10" and render identically, because
            # formatOperationLabel absorbs an existing prefix and both backend parsers of
            # this column key on the digits (work_order_state_service._normalized_operation_number
            # and the SPC critical-dims match in shop_floor.py). An UPDATE over live
            # multi-tenant production rows would buy nothing and would rewrite values the
            # office typed by hand.
            operation_number=rop.operation_number or str(rop.sequence),
            name=rop.name,
            description=rop.description,
            work_center_id=rop.work_center_id,
            setup_time_hours=rop.setup_hours,
            run_time_hours=float(rop.run_hours_per_unit or 0) * float(quantity),
            setup_instructions=rop.setup_instructions,
            run_instructions=rop.work_instructions,
            requires_inspection=rop.is_inspection_point,
            inspection_type="final" if _is_inspection_operation(rop) else None,
            status=OperationStatus.PENDING,
            operation_group=get_work_center_group(work_center) if work_center else None,
            company_id=company_id,
        )
        db.add(wo_op)
        operation_sheet_pairs.append((wo_op, rop.process_sheet_id))

    return process_sheet_service.snapshot_steps_for_work_order(db, company_id, operation_sheet_pairs)


@router.post("/import", response_model=WorkOrderImportResponse, summary="Import open work orders (CSV/XLSX)")
async def import_open_work_orders_endpoint(
    file: UploadFile = File(...),
    dry_run: bool = Query(False, description="Validate and preview only; guarantees no rows are written"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Import OPEN (in-flight) work orders for the Excel go-live migration.

    Columns: ``wo_number`` (optional, generated when blank), ``part_number``
    (must exist with a released routing), ``quantity``, ``due_date`` (optional,
    past dates allowed — open WOs can be overdue), ``customer`` (optional code
    or name), ``customer_po`` (optional), ``priority`` (optional 1-10),
    ``completed_through_seq`` (optional — last routing sequence already
    finished on paper; those operations are marked complete WITHOUT fabricated
    labor evidence and the next operation becomes READY in floor queues).

    Use ``dry_run=true`` to preview: every row is fully validated (including
    routing expansion) inside a savepoint that is rolled back.
    """
    content = await file.read()
    # Parse + import are CPU/DB-bound sync work; run them in the threadpool so a
    # large upload can't stall the event loop (the request-scoped Session/audit
    # are used sequentially from one worker thread — same as a sync endpoint).
    try:
        table = await run_in_threadpool(
            parse_import_file, file.filename, content, required_columns={"part_number", "quantity"}
        )
    except ImportFileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return await run_in_threadpool(
        import_open_work_orders,
        db,
        table=table,
        current_user=current_user,
        company_id=company_id,
        audit=audit,
        dry_run=dry_run,
    )


async def _run_laser_nest_preview(
    *,
    file: Optional[UploadFile],
    source_path: Optional[str],
    company_id: int,
    db: Session,
) -> LaserNestPreviewResponse:
    """Shared preview flow for the ``{work_order_id}`` and standalone endpoints.

    Three upload shapes, auto-detected: a **bare PDF** (single- or multi-page --
    AI segmentation decides which pages form which nest, the PDF is split into
    per-segment files, and each segment runs the per-nest AI extraction), a
    ZIP/folder of nest-report **PDFs** (fields extracted by AI, one extraction
    per PDF, parallelized with bounded concurrency), or the legacy ZIP/folder of
    CNC **program files** (fields inferred from filenames). PDFs and CNC
    extensions are disjoint, so a package is treated as a PDF package iff it
    contains any ``*.pdf``.
    """
    package_name = _laser_package_name(file, source_path)
    temp_path = None
    source_page_count: Optional[int] = None
    segmentation_warning: Optional[str] = None
    skipped_pages: Optional[List[int]] = None
    try:
        if file and is_bare_pdf_upload(file.filename or "", file.content_type):
            # Bare (possibly multi-page) PDF: segment (AI pass 0, degrades to
            # one-nest-per-page) -> deterministic split -> the same bounded
            # parallel per-segment extraction the ZIP path uses. Rows carry the
            # segment's page list so import can re-split without AI.
            temp_path = await _save_upload_to_temp(file)
            page_count = await _read_bare_pdf_page_count_or_400(temp_path)
            segmentation = await run_in_threadpool(
                segment_nest_pdf, temp_path, file.filename or "nests.pdf", page_count, company_id
            )
            segments = [list(nest["pages"]) for nest in segmentation["nests"]]
            with TemporaryDirectory() as scan_dir:
                segment_names = await run_in_threadpool(split_pdf_segments, temp_path, segments, scan_dir)
                # Split names are synthetic ('nest-p001.pdf'), NOT CNC numbers:
                # extraction must not use them as hints or fallbacks. The
                # segmentation pass's per-nest cnc_number_hint (when it saw one
                # in a title block) is offered instead, keyed by split name.
                cnc_hints = {
                    name: nest["cnc_number_hint"]
                    for name, nest in zip(segment_names, segmentation["nests"])
                    if nest.get("cnc_number_hint")
                }
                parsed_nests = await _parse_laser_nest_pdf_package_async(
                    scan_dir, company_id, cnc_hints=cnc_hints, filename_is_cnc_hint=False
                )
            # Attach each segment's page list to its row. Split names are the
            # rows' rel paths, and both orders are ascending-first-page, so the
            # name->pages map realigns them even if glob order ever shifts.
            pages_by_name = dict(zip(segment_names, segments))
            nests = [
                dataclass_replace(
                    nest,
                    source_pages=(
                        tuple(pages_by_name[nest.cnc_file_path]) if nest.cnc_file_path in pages_by_name else None
                    ),
                ).as_dict()
                for nest in parsed_nests
            ]
            source_page_count = page_count
            segmentation_warning = segmentation.get("warning")
            skipped_pages = list(segmentation.get("skipped_pages") or [])
        elif file:
            temp_path = await _save_upload_to_temp(file)
            # Extract once into a temp dir so we can inspect contents (PDF vs CNC)
            # and run the AI extraction over the materialized files.
            with TemporaryDirectory() as scan_dir:
                extract_laser_nest_zip(temp_path, scan_dir)
                nests = await _preview_nests_from_folder(scan_dir, company_id)
        elif source_path:
            nests = await _preview_nests_from_folder(source_path, company_id)
        else:
            raise HTTPException(status_code=400, detail="Upload a zipped package or provide source_path")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)

    # Sheet-stock suggestion pass. ADVISORY and BEST-EFFORT: it reads the tenant's
    # material catalog, tie history and on-hand to propose a sheet per row, then
    # (for ambiguous rows only) lets the AI resolver re-rank the shortlist. It
    # writes nothing and ties nothing.
    #
    # THE MATCHER CAN NEVER FAIL A PREVIEW. A planner who uploaded a 42-nest
    # package must get their rows even if the catalog read, the LLM, or the
    # resolver falls over -- degrading to "no suggestions" costs 42 manual picks,
    # which is exactly today's behavior; raising costs the whole upload.
    # Both calls are SYNCHRONOUS and BLOCKING -- three SQLAlchemy queries, then a
    # single Anthropic request with a 20-second ceiling -- so they run off the
    # event loop, exactly like every other blocking step in this function
    # (``segment_nest_pdf``, ``split_pdf_segments``, ``parse_laser_nest_folder``).
    # Left inline they would freeze the worker's loop for the whole LLM timeout,
    # stalling the kiosk polls, the wallboard refresh and /health for everyone
    # else on that process -- a cost paid by the shop, not by the planner whose
    # spinner it is.
    def _match_sheets() -> Optional[dict]:
        matches = match_sheet_parts(db, company_id=company_id, nests=nests)
        resolve_ambiguous_sheet_matches(matches, company_id=company_id)
        return matches

    sheet_matches = None
    try:
        sheet_matches = await run_in_threadpool(_match_sheets)
    except Exception:  # noqa: BLE001 - a suggestion is never worth failing the preview for
        logger.warning("sheet-stock matching failed; preview degrades to no suggestions", exc_info=True)
        sheet_matches = None

    # Response assembly is INSIDE the guard too. The schema caps
    # (``max_length=300`` on every diagnostic / reason) are validated here, not
    # above, and the matcher quotes the nest's own AI-extracted descriptors --
    # which carry no length bound anywhere on their path. Producing those strings
    # is clamped at the source as well (``_clamp_detail``), so this is the second
    # of two independent guards; without it a single garbled thickness cell turns
    # a 42-nest upload that already burned minutes of extraction into a 500.
    try:
        return _build_laser_preview_response(
            package_name,
            nests,
            source_page_count=source_page_count,
            segmentation_warning=segmentation_warning,
            skipped_pages=skipped_pages,
            sheet_matches=sheet_matches,
        )
    except ValidationError:
        logger.warning(
            "sheet-stock suggestions failed response validation; preview degrades to no suggestions",
            exc_info=True,
        )

    # Rebuilt with NO suggestions -- passing the same ``sheet_matches`` again is
    # what just failed. The planner gets their 42 rows and picks by hand.
    return _build_laser_preview_response(
        package_name,
        nests,
        source_page_count=source_page_count,
        segmentation_warning=segmentation_warning,
        skipped_pages=skipped_pages,
    )


# NOTE: the two static /laser-nest-packages/standalone/* routes are registered
# BEFORE the parametrized /{work_order_id}/laser-nest-packages/* routes. Their
# literal paths cannot collide with the parametrized patterns today, but keeping
# the static routes first documents the intent and stays robust to path tweaks.
@router.post("/laser-nest-packages/standalone/preview", response_model=LaserNestPreviewResponse)
async def preview_standalone_laser_nest_package(
    file: Optional[UploadFile] = File(None),
    source_path: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
):
    """Preview a nest package for a STANDALONE laser-cutting work order.

    Identical parsing/extraction behavior to the ``{work_order_id}`` preview,
    just not anchored to any existing work order -- the wizard uses this before
    a standalone import that will create a fresh part-less laser WO.

    The ``db`` session is here for the sheet-stock suggestion pass only; it is
    used for tenant-scoped READS (``company_id`` from the token, never the body)
    and this endpoint still persists nothing.
    """
    return await _run_laser_nest_preview(file=file, source_path=source_path, company_id=company_id, db=db)


@router.post("/{work_order_id}/laser-nest-packages/preview", response_model=LaserNestPreviewResponse)
async def preview_laser_nest_package_import(
    work_order_id: int,
    file: Optional[UploadFile] = File(None),
    source_path: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
):
    """Preview nest operations from a zipped Ermaksan package, a bare nest-report PDF, or a server folder.

    See ``_run_laser_nest_preview`` for the package-shape detection. The addressed
    WO may be an assembly parent or (since the standalone generalization) a
    laser-cutting WO itself.
    """
    _load_parent_work_order(db, work_order_id, company_id)
    return await _run_laser_nest_preview(file=file, source_path=source_path, company_id=company_id, db=db)


async def _run_laser_nest_import(
    *,
    db: Session,
    current_user: User,
    company_id: int,
    audit: AuditService,
    target_work_order: Optional[WorkOrder],
    file: Optional[UploadFile],
    source_path: Optional[str],
    work_center_id: Optional[int],
    rows: Optional[str],
    due_date: Optional[date] = None,
    sheet_match_provenance: Optional[str] = None,
) -> dict:
    """Shared import flow for the ``{work_order_id}`` and standalone endpoints.

    ``target_work_order`` is the WO addressed in the path -- an assembly parent
    (classic child-laser-WO flow) or a laser-cutting WO itself (operated on
    directly; see ``_resolve_laser_target``). ``None`` means STANDALONE: create
    a fresh part-less RELEASED laser-cutting WO (no parent) and import into it;
    ``due_date`` (standalone only) is stamped onto that fresh WO. Past dates are
    allowed -- an open WO can already be overdue at import, matching the WO
    import loader posture. On the ``{work_order_id}`` path the WO already exists
    and its due date is edited via ``PUT /work-orders/{id}``, so ``due_date`` is
    never passed there.

    Two paths, both honoring IMPORT-REPLACES-EVERYTHING:
    - ``rows`` provided (PDF confirm-and-commit): the re-sent upload -- a ZIP of
      per-nest PDFs, or a bare (possibly multi-page) PDF that is re-split
      deterministically by each row's confirmed ``source_pages`` -- supplies the
      PDF bytes only; the persisted field values are the planner-CONFIRMED ones
      from the JSON ``rows`` (no second AI call). Each nest's PDF (segment) is
      stored as a DRAWING Document and attached.
    - ``rows`` absent (legacy CNC-program import): unchanged -- fields inferred
      from filenames, no Documents. A bare PDF without ``rows`` is rejected.

    Both paths audit (DELETE per superseded nest, CREATE per new nest): the
    import wipes ALL prior nests/operations for the laser WO, so each wipe is
    recorded before the rebuild and each created nest is recorded after. The
    standalone path additionally audits the CREATE of the fresh work order.
    """
    package_name = _laser_package_name(file, source_path)
    temp_path = None
    package_dir = os.path.join(_resolve_laser_upload_root(), str(uuid.uuid4()))

    is_pdf_import = rows is not None
    confirmed_rows: list[LaserNestImportRow] = []
    if is_pdf_import:
        try:
            parsed = json.loads(rows)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="rows must be valid JSON") from exc
        if not isinstance(parsed, list):
            raise HTTPException(status_code=400, detail="rows must be a JSON array of nest rows")
        if len(parsed) > LASER_PDF_PACKAGE_MAX:
            raise HTTPException(
                status_code=400,
                detail=f"Too many nest rows ({len(parsed)}); the limit is {LASER_PDF_PACKAGE_MAX}.",
            )
        # Validate the raw rows through Pydantic BEFORE anything is persisted, so
        # a negative/huge/non-numeric planned_runs or an over-long string is a
        # clean 400 rather than a 500 or poisoned data.
        try:
            confirmed_rows = TypeAdapter(List[LaserNestImportRow]).validate_python(parsed)
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid nest rows: {exc.errors()}") from exc

    # Decoded HERE -- before ``atomic_transaction``, alongside the rows parse and
    # deliberately nowhere near the operation wipe or the tie machinery. It feeds
    # exactly one thing: a key on the WO-level audit row's ``extra_data``. It does
    # not reach ``_build_confirmed_pdf_nests``, ``row_material_parts``, or
    # ``create_nest_material_allocation``, so an untied import stays byte-identical
    # to its pre-feature behavior (invariant 6(d)) whatever this field says.
    # Intersected with the rows actually being imported, so the audit row can only
    # describe nests this import really carried. A claim about a file that is not
    # in the package is not a breadcrumb, it is a fabrication in an append-only
    # record. (Legacy CNC-program imports send no ``rows``; there is nothing to
    # intersect against and nothing sends provenance on that path either.)
    sheet_match_provenance_map = _parse_sheet_match_provenance(
        sheet_match_provenance,
        {row.source_file for row in confirmed_rows} if confirmed_rows else set(),
    )

    # Storage blobs for nest-PDF Documents are written by storage.save() INSIDE
    # the atomic_transaction, BEFORE it commits. On rollback they must be reaped
    # (they live outside package_dir, so shutil.rmtree(package_dir) misses them).
    saved_storage_keys: list[str] = []

    def _reap_saved_blobs() -> None:
        # Idempotent: the list is drained as it is reaped, so nested handlers
        # can each call this (the DB-error branch reaps, then raises a 400 that
        # the outer HTTPException handler also cleans up after) without
        # double-deleting a ref.
        while saved_storage_keys:
            key = saved_storage_keys.pop()
            try:
                delete_ref(key)
            except Exception:  # noqa: BLE001 - cleanup must not mask the original error
                logger.warning("Failed to reap orphaned laser-nest blob on rollback: %s", key)

    try:
        if file:
            temp_path = await _save_upload_to_temp(file)
            if is_bare_pdf_upload(file.filename or "", file.content_type):
                # Bare-PDF confirm-and-commit: rows are REQUIRED (the legacy
                # no-rows import is CNC-programs-only), and the re-sent PDF is
                # re-split deterministically by the rows' confirmed page lists
                # -- no AI call and no re-segmentation on the commit path. The
                # split writes the per-segment PDFs into package_dir under the
                # exact names the preview issued, so the confirmed-rows
                # machinery below resolves them like any PDF package.
                if not is_pdf_import:
                    raise HTTPException(status_code=400, detail="Preview the PDF first, then confirm the rows")
                await _read_bare_pdf_page_count_or_400(temp_path)
                segments = _confirmed_pdf_segments(confirmed_rows)
                await run_in_threadpool(split_pdf_segments, temp_path, segments, package_dir)
            else:
                extract_laser_nest_zip(temp_path, package_dir)
        elif source_path:
            copy_laser_nest_folder(source_path, package_dir)
        else:
            raise HTTPException(status_code=400, detail="Upload a zipped package or provide source_path")

        if is_pdf_import:
            # PDF path: persist the CONFIRMED values; do NOT re-run the AI.
            nests = _build_confirmed_pdf_nests(package_dir, confirmed_rows)
        else:
            # Legacy CNC-program import path, unchanged.
            nests = parse_laser_nest_folder(package_dir)
        laser_work_center = _find_laser_work_center(db, company_id, work_center_id)

        # Resolve every DISTINCT per-row work-center override BEFORE the atomic
        # build, with the same active + company-scoped semantics (and 404) as the
        # package-level pick, so a bad override fails cleanly with nothing
        # persisted. Only the PDF confirm-and-commit path carries overrides; the
        # legacy CNC-file path has no rows and stays package-level only.
        row_work_centers: dict[int, WorkCenter] = {}
        for nest in nests:
            if nest.work_center_id and nest.work_center_id not in row_work_centers:
                row_work_centers[nest.work_center_id] = _find_laser_work_center(db, company_id, nest.work_center_id)

        # Same pre-resolution for every DISTINCT per-row MATERIAL TIE, and for the
        # same reason: a bad or cross-tenant part id must fail cleanly (404) with
        # nothing persisted, rather than mid-build where the rebuild has already
        # wiped the prior nests. Only the PDF confirm-and-commit path carries ties;
        # the legacy CNC-file path has no rows, so this loop is a no-op there and
        # those imports stay byte-identical to their pre-feature behavior.
        row_material_parts: dict[int, Part] = {}
        for nest in nests:
            if nest.material_part_id and nest.material_part_id not in row_material_parts:
                row_material_parts[nest.material_part_id] = _find_nest_material_part(
                    db, company_id, nest.material_part_id
                )

        import_source = "pdf_import" if is_pdf_import else "cnc_file_import"

        try:
            with atomic_transaction(db):
                if target_work_order is None:
                    # STANDALONE: create the fresh part-less laser WO here so it
                    # commits (and audits) atomically with the package build.
                    parent_work_order: Optional[WorkOrder] = None
                    child_work_order = WorkOrder(
                        company_id=company_id,
                        work_order_number=generate_work_order_number(db, company_id),
                        part_id=None,
                        parent_work_order_id=None,
                        work_order_type=WorkOrderType.LASER_CUTTING.value,
                        # A nest package is a DISPATCH POOL by construction: pin the flag
                        # to pooled rather than inherit the sequenced create-default. Inert
                        # either way (the laser short-circuit sits above it), but it stops
                        # the row claiming a rule its own behavior contradicts.
                        sequential_operations=False,
                        # Re-derived to the package's total planned runs by
                        # build_laser_nest_child_work_order below.
                        quantity_ordered=1,
                        status=WorkOrderStatus.RELEASED,
                        priority=5,
                        # Planner-set at import (may be in the past -- open WOs
                        # can be overdue); editable later via PUT /work-orders/{id}.
                        due_date=due_date,
                        notes=f"Standalone laser nest work order for package {package_name}",
                        created_by=current_user.id,
                    )
                    db.add(child_work_order)
                    db.flush()
                    pre_import_wo_values = None  # fresh WO: creation is audited via log_create below
                else:
                    parent_work_order, child_work_order = _resolve_laser_target(
                        db, work_order=target_work_order, company_id=company_id
                    )
                    # Snapshot BEFORE the rebuild mutates the WO: the import
                    # force-sets RELEASED and zeroes the produced quantities, and
                    # that WO-level change must be audited (invariant 2) -- the
                    # per-nest DELETE/CREATE rows alone don't record it.
                    pre_import_wo_values = _laser_wo_audit_values(child_work_order)
                    # A re-import onto a job ALREADY running on a machine keeps that
                    # machine. IMPORT-REPLACES-EVERYTHING rebuilds every operation onto
                    # `laser_work_center`, so without this the shop-wide auto-detect
                    # moves the WHOLE package -- not just the nest being added -- to
                    # whichever laser it happens to prefer. Resolved HERE because it is
                    # the first point where the target WO is known and the last before
                    # the wipe below deletes the operations it reads. Only when the
                    # caller named no machine: an explicit pick (package-level or
                    # per-row) still wins, and it was already validated above so a bad
                    # id still 404s with nothing persisted.
                    if work_center_id is None:
                        laser_work_center = _laser_work_center_for_added_nest(
                            db, laser_work_order=child_work_order, company_id=company_id
                        )
                # RELEASED (stamped as this importer's release) unless the job is on
                # hold, which a corrective re-import keeps -- see the helper.
                _release_laser_work_order_for_nests(child_work_order, released_by=current_user.id)
                child_work_order.quantity_complete = 0
                child_work_order.quantity_scrapped = 0
                parent_work_order_id = parent_work_order.id if parent_work_order is not None else None

                # IMPORT-REPLACES-EVERYTHING wipes ALL prior non-deleted nests on
                # this child WO (cascade hard-delete via build_..._child_work_order).
                # Audit each superseded nest as a DELETE BEFORE the rebuild so the
                # wipe is traceable; the audit rows only flush, so they commit
                # atomically with the rebuild (mirrors the manual endpoint's
                # audit-before-commit ordering). Runs for BOTH import shapes.
                superseded_nests = (
                    db.query(LaserNest)
                    .join(WorkOrderOperation, LaserNest.work_order_operation_id == WorkOrderOperation.id)
                    .options(joinedload(LaserNest.operation))
                    .filter(
                        LaserNest.company_id == company_id,
                        WorkOrderOperation.work_order_id == child_work_order.id,
                        LaserNest.is_deleted == False,  # noqa: E712
                    )
                    .all()
                )
                for nest in superseded_nests:
                    audit.log_delete(
                        resource_type="laser_nest",
                        resource_id=nest.id,
                        resource_identifier=nest.cnc_number or nest.nest_name,
                        old_values={
                            "nest_name": nest.nest_name,
                            "cnc_number": nest.cnc_number,
                            "planned_runs": nest.planned_runs,
                            "completed_runs": nest.completed_runs,
                            "material": nest.material,
                            "thickness": nest.thickness,
                            "sheet_size": nest.sheet_size,
                            "document_id": nest.document_id,
                            "work_order_operation_id": nest.work_order_operation_id,
                        },
                        soft_delete=False,
                        extra_data={
                            "reason": "superseded_by_reimport",
                            "parent_work_order_id": parent_work_order_id,
                            "child_work_order_id": child_work_order.id,
                        },
                    )

                package = build_laser_nest_child_work_order(
                    db,
                    parent_work_order=parent_work_order,
                    child_work_order=child_work_order,
                    package_name=package_name,
                    package_source_path=package_dir,
                    nests=nests,
                    laser_work_center=laser_work_center,
                    company_id=company_id,
                    created_by=current_user.id,
                    saved_storage_keys=saved_storage_keys,
                    row_work_centers=row_work_centers,
                    row_material_parts=row_material_parts,
                    audit=audit,
                )

                if pre_import_wo_values is not None:
                    # Audit the WO-level effect of the (re)import on an EXISTING
                    # laser WO: status forced to RELEASED (an on-hold job kept on
                    # hold), the release stamp written when it had none or was a
                    # DRAFT, quantity_complete / quantity_scrapped zeroed,
                    # quantity_ordered re-derived to the package's total planned
                    # runs. A classic child WO freshly created by this very request
                    # records its first release stamp here (it has no CREATE row of
                    # its own). log_update self-suppresses when nothing actually
                    # changed, and only flushes, so it commits atomically with the
                    # rebuild.
                    audit.log_update(
                        resource_type="work_order",
                        resource_id=child_work_order.id,
                        resource_identifier=child_work_order.work_order_number,
                        old_values=pre_import_wo_values,
                        new_values=_laser_wo_audit_values(child_work_order),
                        extra_data={
                            "reason": "laser_nest_package_import",
                            "source": import_source,
                            "parent_work_order_id": parent_work_order_id,
                            "package_name": package_name,
                            # Absent (not empty) when the wizard sent nothing, so an
                            # import from a client that never heard of suggestions
                            # writes the exact extra_data it wrote before.
                            **(
                                {"sheet_match_provenance": sheet_match_provenance_map}
                                if sheet_match_provenance_map
                                else {}
                            ),
                        },
                    )

                if target_work_order is None:
                    # Audit the standalone WO creation AFTER the build so the
                    # snapshot carries the final quantity_ordered (= total
                    # planned runs) and the laser_cutting type/RELEASED status.
                    # AuditService.log only flushes, so this commits atomically
                    # with the WO + package.
                    audit.log_create(
                        resource_type="work_order",
                        resource_id=child_work_order.id,
                        resource_identifier=child_work_order.work_order_number,
                        new_values=child_work_order,
                        extra_data={
                            "work_order_type": WorkOrderType.LASER_CUTTING.value,
                            "quantity": float(child_work_order.quantity_ordered),
                            "source": "laser_nest_standalone_import",
                            "package_name": package_name,
                            # See the log_update above: omitted entirely when empty.
                            **(
                                {"sheet_match_provenance": sheet_match_provenance_map}
                                if sheet_match_provenance_map
                                else {}
                            ),
                        },
                    )

                # Audit each CREATED nest BEFORE commit, for BOTH import shapes
                # (the legacy CNC path previously created nests with only a WO
                # event). The SELECT filters company_id + package.id, so it works
                # regardless of source. AuditService.log only flushes, so these
                # commit atomically with the nests.
                created_nests = (
                    db.query(LaserNest)
                    .filter(
                        LaserNest.company_id == company_id,
                        LaserNest.package_id == package.id,
                    )
                    .order_by(LaserNest.id)
                    .all()
                )
                for nest in created_nests:
                    audit.log_create(
                        resource_type="laser_nest",
                        resource_id=nest.id,
                        resource_identifier=nest.cnc_number or nest.nest_name,
                        new_values={
                            "nest_name": nest.nest_name,
                            "cnc_number": nest.cnc_number,
                            "planned_runs": nest.planned_runs,
                            "material": nest.material,
                            "thickness": nest.thickness,
                            "sheet_size": nest.sheet_size,
                            "document_id": nest.document_id,
                            "work_order_operation_id": nest.work_order_operation_id,
                            "package_id": nest.package_id,
                        },
                        extra_data={
                            "parent_work_order_id": parent_work_order_id,
                            "child_work_order_id": child_work_order.id,
                            "source": import_source,
                        },
                    )

                _emit_work_order_event(
                    db,
                    company_id=company_id,
                    current_user=current_user,
                    work_order=child_work_order,
                    event_type="laser_nest_package_imported",
                    payload={
                        "parent_work_order_id": parent_work_order_id,
                        "package_id": package.id,
                        "nest_count": len(nests),
                        "total_planned_runs": sum(nest.planned_runs for nest in nests),
                        "source": "pdf_import" if is_pdf_import else "cnc_files",
                    },
                )
        except (IntegrityError, SQLAlchemyError) as exc:
            # The transaction rolled back, so the just-written nest-PDF blobs are
            # now orphaned -- reap them. Translate the DB/constraint fault to a
            # clean 400 (a poisoned session must not surface as a 500).
            _reap_saved_blobs()
            logger.warning("Laser-nest import failed on a database/constraint error: %s", exc)
            raise HTTPException(
                status_code=400,
                detail="Could not import the nest package; a nest conflicts with an existing record "
                "or a value is invalid. Review the rows and try again.",
            ) from exc
    except MaterialAllocationConsumedError as exc:
        # A rebuild would destroy operations that already consumed tied material,
        # orphaning the ISSUE rows that carry their lot genealogy. Nothing was
        # deleted (the guard runs before the wipe) -- refuse with 409. The remedy is
        # a NEW work order: a material return does not un-write movement history, so
        # it does not unlock re-import.
        _reap_saved_blobs()
        if os.path.isdir(package_dir):
            shutil.rmtree(package_dir, ignore_errors=True)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        # A pre-commit validation failure (e.g. duplicate source_file, empty
        # package): no transaction committed. Reap any blobs written before the
        # raise, then clean the temp package dir.
        _reap_saved_blobs()
        if os.path.isdir(package_dir):
            shutil.rmtree(package_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        # A pre-commit HTTPException (per-row work-center 404, laser-WC lookup
        # failures, ...) raised AFTER the package was extracted would otherwise
        # orphan the extracted directory on disk forever -- same cleanup as the
        # ValueError branch, then let the response propagate unchanged.
        _reap_saved_blobs()
        if os.path.isdir(package_dir):
            shutil.rmtree(package_dir, ignore_errors=True)
        raise
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)

    # Requery by id: correct for all three flows (classic child, direct laser
    # target, standalone) -- the laser WO's id is known either way.
    child_work_order = (
        db.query(WorkOrder)
        .options(
            joinedload(WorkOrder.part),
            selectinload(WorkOrder.operations).selectinload(WorkOrderOperation.work_center),
            selectinload(WorkOrder.operations)
            .selectinload(WorkOrderOperation.laser_nest)
            .selectinload(LaserNest.document),
        )
        .filter(
            WorkOrder.company_id == company_id,
            WorkOrder.id == child_work_order.id,
        )
        .first()
    )
    _enrich_work_order_operations(child_work_order, db=db, company_id=company_id)

    safe_broadcast(
        broadcast_work_order_update,
        child_work_order.id,
        {
            "event": "laser_nest_package_imported",
            "status": child_work_order.status.value,
        },
        company_id=company_id,
    )
    safe_broadcast(
        broadcast_dashboard_update,
        {
            "event": "laser_nest_package_imported",
            "work_order_id": child_work_order.id,
            "parent_work_order_id": parent_work_order_id,
        },
        company_id=company_id,
    )

    return {
        "package": _build_laser_preview_response(package_name, [nest.as_dict() for nest in nests]).model_dump(),
        "child_work_order": WorkOrderResponse.model_validate(child_work_order).model_dump(mode="json"),
    }


@router.post("/laser-nest-packages/standalone/import")
async def import_standalone_laser_nest_package(
    file: Optional[UploadFile] = File(None),
    source_path: Optional[str] = Form(None),
    work_center_id: Optional[int] = Form(None),
    rows: Optional[str] = Form(None),
    due_date: Optional[date] = Form(None),
    sheet_match_provenance: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Import a nest package into a FRESH standalone laser-cutting work order.

    No parent work order and no part: the import creates a RELEASED
    ``work_order_type='laser_cutting'`` WO (its release recorded as the importer's:
    ``released_by`` / ``released_at``) with ``part_id`` NULL and
    ``quantity_ordered`` = total planned sheet runs, sets the package's
    ``child_work_order_id`` to it (``parent_work_order_id`` NULL), and attaches
    nest-PDF Documents to the created WO itself. Same request shape and audit
    behavior as the ``{work_order_id}`` import, minus the WO id; the response
    exposes the created WO under the same ``child_work_order`` key.

    ``due_date`` (ISO date) is the planner-set due date for the created WO; past
    dates are allowed (an open WO can already be overdue at import).

    ``sheet_match_provenance`` is an optional JSON object mapping each row's
    ``source_file`` to how its sheet part was chosen (``auto`` | ``suggested`` |
    ``planner`` | ``prefill``). OBSERVATIONAL ONLY: it is recorded on the WO-level
    audit row and never used to resolve anything or to create a tie. A malformed
    value is discarded, not rejected.
    """
    return await _run_laser_nest_import(
        db=db,
        current_user=current_user,
        company_id=company_id,
        audit=audit,
        target_work_order=None,
        file=file,
        source_path=source_path,
        work_center_id=work_center_id,
        rows=rows,
        due_date=due_date,
        sheet_match_provenance=sheet_match_provenance,
    )


@router.post("/{work_order_id}/laser-nest-packages/import")
async def import_laser_nest_package(
    work_order_id: int,
    file: Optional[UploadFile] = File(None),
    source_path: Optional[str] = Form(None),
    work_center_id: Optional[int] = Form(None),
    rows: Optional[str] = Form(None),
    sheet_match_provenance: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Create or update a laser work order from one nest package.

    Classic flow: the addressed WO is an assembly parent -> its LASER_CUTTING
    child is found-or-created and rebuilt. Generalized flow: the addressed WO is
    itself ``laser_cutting`` (e.g. a standalone nest WO) -> it is rebuilt
    directly, no child is nested under it. See ``_run_laser_nest_import`` for
    the PDF confirm-and-commit vs legacy CNC paths and the audit contract.

    The laser work order is set RELEASED, with the release recorded as the
    caller's when it was a draft or carried no release stamp (an existing stamp is
    kept). A laser work order that is ON HOLD stays on hold: its rebuilt nests are
    refused by clock-in and every start verb until the hold is lifted
    (``POST /work-orders/{id}/resume``).

    ``sheet_match_provenance`` is the same observational, audit-only breadcrumb
    documented on the standalone import: it never resolves anything and never
    creates a tie.
    """
    target_work_order = _load_parent_work_order(db, work_order_id, company_id)
    return await _run_laser_nest_import(
        db=db,
        current_user=current_user,
        company_id=company_id,
        audit=audit,
        target_work_order=target_work_order,
        file=file,
        source_path=source_path,
        work_center_id=work_center_id,
        rows=rows,
        sheet_match_provenance=sheet_match_provenance,
    )


@router.post(
    "/{work_order_id}/laser-nests/manual",
    response_model=LaserNestManualResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Manually add one laser nest to an assembly work order",
)
def create_manual_laser_nest_endpoint(
    work_order_id: int,
    payload: LaserNestManualCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Manually key one laser nest onto a work order (standalone creation path).

    Classic flow: the addressed WO is an assembly parent -- the child laser WO is
    resolved (or created). Generalized flow: the addressed WO is itself
    ``laser_cutting`` (e.g. a standalone nest WO) -- the nest is appended to it
    directly. Delegates the state change to ``create_manual_laser_nest``.
    Untouched by, and does not touch, the import flow.

    The laser work order is set RELEASED the same way the import does (release
    recorded as the caller's when it was a draft or unstamped); one that is ON
    HOLD stays on hold, so the new nest cannot be started until the hold is lifted.

    An optional ``material_part_id`` ties the created nest's operation to a stock
    material part (``qty_per_run`` defaults to 1.0), so that material is deducted when
    the nest's operation completes (the work-order completion reconcile is the
    self-heal) -- the same tie, through the same ``create_nest_material_allocation``
    seam, that the package import creates. Omitting it leaves the nest untied and
    byte-identical to its pre-feature behavior. It must name material: an unknown,
    cross-tenant or soft-deleted part is **404**, and a part the shop PRODUCES
    (MANUFACTURED / ASSEMBLY) is **422** — both raised by ``_find_nest_material_part``
    before the transaction opens, so nothing is created either way.
    """
    target_work_order = _load_parent_work_order(db, work_order_id, company_id)
    # Resolve the material part BEFORE the transaction: it is a read-only,
    # tenant-scoped lookup, and a 404 here must not have to unwind a partial build.
    material_part = (
        _find_nest_material_part(db, company_id, payload.material_part_id) if payload.material_part_id else None
    )

    with atomic_transaction(db):
        parent_work_order, child_work_order = _resolve_laser_target(
            db, work_order=target_work_order, company_id=company_id
        )
        # Snapshot BEFORE the mutations: the manual add force-sets RELEASED (stamped;
        # an on-hold job stays on hold) and re-derives quantity_ordered; that WO-level
        # change is audited below (invariant 2). log_update self-suppresses when
        # nothing changed.
        pre_add_wo_values = _laser_wo_audit_values(child_work_order)
        _release_laser_work_order_for_nests(child_work_order, released_by=current_user.id)
        # The machine the rest of this job's nests already run on; the shop-wide
        # auto-detect only when there are none. Raises 400 when neither yields a
        # laser work center, and 404 on an unknown/inactive explicit choice.
        laser_work_center = _laser_work_center_for_added_nest(
            db,
            laser_work_order=child_work_order,
            company_id=company_id,
            work_center_id=payload.work_center_id,
        )

        nest = create_manual_laser_nest(
            db,
            parent_work_order=parent_work_order,
            child_work_order=child_work_order,
            laser_work_center=laser_work_center,
            data=payload,
            company_id=company_id,
            user_id=current_user.id,
        )
        if material_part is not None:
            # Same operation-scoped tie the package import creates, through the same
            # seam, so both paths produce identical rows and identical hash-chain
            # entries. The nest's operation is already flushed by the call above.
            create_nest_material_allocation(
                db,
                work_order=child_work_order,
                operation=nest.operation,
                part=material_part,
                qty_per_run=payload.qty_per_run,
                planned_runs=nest.planned_runs,
                company_id=company_id,
                created_by=current_user.id,
                audit=audit,
            )
        # Audit BEFORE the atomic_transaction commit so the audit row commits
        # atomically with the nest (AuditService.log only flushes).
        audit.log_create(
            resource_type="laser_nest",
            resource_id=nest.id,
            resource_identifier=nest.cnc_number or nest.nest_name,
            new_values={
                "nest_name": nest.nest_name,
                "cnc_number": nest.cnc_number,
                "planned_runs": nest.planned_runs,
                "material": nest.material,
                "thickness": nest.thickness,
                "sheet_size": nest.sheet_size,
                "work_order_operation_id": nest.work_order_operation_id,
                "package_id": nest.package_id,
            },
            extra_data={
                "parent_work_order_id": parent_work_order.id if parent_work_order is not None else None,
                "child_work_order_id": child_work_order.id,
                "source": "manual",
            },
        )
        # WO-level audit for the status force-set / quantity re-derivation the
        # manual add performs on the laser WO (see the pre-mutation snapshot
        # above). Only flushes, so it commits atomically with the nest.
        audit.log_update(
            resource_type="work_order",
            resource_id=child_work_order.id,
            resource_identifier=child_work_order.work_order_number,
            old_values=pre_add_wo_values,
            new_values=_laser_wo_audit_values(child_work_order),
            extra_data={
                "reason": "manual_laser_nest_added",
                "source": "manual",
                "parent_work_order_id": parent_work_order.id if parent_work_order is not None else None,
            },
        )

    db.refresh(nest)
    return LaserNestManualResponse(**manual_nest_response_dict(nest))


@router.get("/{work_order_id}/timeline", response_model=WorkOrderTimelineResponse)
def get_work_order_timeline(
    work_order_id: int,
    category: Optional[TimelineCategory] = None,
    actor_id: Optional[int] = Query(None, gt=0),
    start_at: Optional[datetime] = None,
    end_at: Optional[datetime] = None,
    cursor: Optional[str] = Query(None, max_length=1000),
    limit: int = Query(30, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Read this job's business history; same authorization as job detail."""
    return list_work_order_timeline(
        db,
        company_id,
        work_order_id,
        category=category,
        actor_id=actor_id,
        start_at=start_at,
        end_at=end_at,
        cursor=cursor,
        limit=limit,
    )


@router.get("/{work_order_id}", response_model=WorkOrderResponse)
def get_work_order(
    work_order_id: int,
    response: Response,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get a specific work order with all operations.

    Each ``ON_HOLD`` operation additionally carries ``hold_context`` -- WHY it is
    held, WHO placed the hold and WHEN -- reconstructed by
    ``services/operation_hold_view`` (the same batched, PURE-READ view the kiosk's
    held list uses, so the office page and the floor cannot tell two different
    stories about one hold). Every other operation row carries ``hold_context:
    null``, every field inside it is nullable (``blocker`` is null for a BARE
    hold), and the blocker's free text rides this response because every caller
    here is an identified session -- see ``OperationHoldInfo`` in
    ``app/schemas/work_order.py``.
    """
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    work_order = (
        db.query(WorkOrder)
        .options(
            joinedload(WorkOrder.part),
            selectinload(WorkOrder.operations).selectinload(WorkOrderOperation.component_part),
            selectinload(WorkOrder.operations).selectinload(WorkOrderOperation.work_center),
            selectinload(WorkOrder.operations)
            .selectinload(WorkOrderOperation.laser_nest)
            .selectinload(LaserNest.document),
        )
        .filter(
            WorkOrder.id == work_order_id,
            WorkOrder.company_id == company_id,
            WorkOrder.is_deleted == False,  # noqa: E712
        )
        .first()
    )

    if not work_order:
        raise HTTPException(status_code=404, detail="Work order not found")

    # Normalize nullable numeric fields for serialization safety
    work_order.quantity_complete = work_order.quantity_complete or 0
    work_order.quantity_scrapped = work_order.quantity_scrapped or 0
    work_order.estimated_hours = work_order.estimated_hours or 0
    work_order.actual_hours = work_order.actual_hours or 0
    work_order.estimated_cost = work_order.estimated_cost or 0
    work_order.actual_cost = work_order.actual_cost or 0

    # Both reconcile-on-read commits below mutate version-mapped operation rows;
    # a concurrent-write conflict is benign on a GET (idempotent), so swallow
    # StaleDataError and serve the read against the freshest committed state
    # rather than 500'ing.
    try:
        if _reconcile_operation_component_quantities(db, work_order, company_id):
            db.commit()
    except StaleDataError:
        db.rollback()
        db.expire_all()
    # AUD-3: terminal reconcile-driven transitions are audited to the requesting user.
    _reconcile_and_commit(db, [work_order], current_user, company_id)
    _enrich_work_order_operations(work_order, db=db, company_id=company_id)

    return work_order


@router.get("/by-number/{wo_number}", response_model=WorkOrderResponse)
def get_work_order_by_number(
    wo_number: str,
    response: Response,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get a work order by work order number"""
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    work_order = (
        db.query(WorkOrder)
        .options(
            selectinload(WorkOrder.operations).selectinload(WorkOrderOperation.work_center),
            selectinload(WorkOrder.operations)
            .selectinload(WorkOrderOperation.laser_nest)
            .selectinload(LaserNest.document),
        )
        .filter(WorkOrder.work_order_number == wo_number, WorkOrder.company_id == company_id)
        .first()
    )

    if not work_order:
        raise HTTPException(status_code=404, detail="Work order not found")
    return work_order


@router.put("/{work_order_id}", response_model=WorkOrderResponse)
def update_work_order(
    work_order_id: int,
    work_order_in: WorkOrderUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
):
    """Update a work order.

    Optimistic locking: the required body ``version`` must equal the work order's
    current version (as returned on every work-order response) or the update is
    rejected with **409 Conflict** ("Work order was modified by someone else.
    Refresh and try again.") before any field is written — re-fetch and retry.
    A successful update increments ``version`` server-side. Also returns **409**
    when moving a terminal WO (COMPLETE/CLOSED/CANCELLED) back to a non-terminal
    status, and when setting ``status`` to COMPLETE/CLOSED from any status other
    than COMPLETE/CLOSED (completion must run its own endpoint's effect chain;
    a CANCELLED work order can never become complete/closed), and when putting a
    **draft** work order on hold (a draft has nothing on the floor to hold -- release
    it first; a hold is lifted with work_orders_resume_work_order).

    This is also the flip verb for ``sequential_operations``. Turning it ON returns
    **409** on a laser nest work order (its nests are a dispatch pool by type, so the
    flag would be inert), and **409** when any operation the sequenced rule would block
    has already been worked -- naming those operations, because every completion verb
    re-checks the gate at action time and they would otherwise become uncompletable.
    Both refusals run before any field is written. On success, operations the pooled
    rule had promoted but the sequenced rule forbids are returned READY -> PENDING, one
    audited status change each; operations anyone has worked are never touched. Turning
    it OFF needs no repair -- the next reconciling read re-promotes the pool.

    Lowering ``quantity_ordered`` below the good + scrap a whole-order operation has already
    accounted for is refused **409** naming the operation: lower the order only by units no
    operation has started.
    """
    work_order = db.query(WorkOrder).filter(WorkOrder.id == work_order_id, WorkOrder.company_id == company_id).first()
    if not work_order:
        raise HTTPException(status_code=404, detail="Work order not found")

    # Capture old values for audit
    audit = AuditService(db, current_user, request)
    old_values = {c.key: getattr(work_order, c.key) for c in work_order.__table__.columns}

    update_data = work_order_in.model_dump(exclude_unset=True)

    # Optimistic locking (invariant 4): the client's version must MATCH the row --
    # and must never be written through the setattr loop below, or a stale client
    # could silently overwrite a concurrent edit AND arbitrarily move the
    # version_id_col counter that SQLAlchemy's StaleDataError protection keys on.
    client_version = update_data.pop("version")
    if client_version != work_order.version:
        raise HTTPException(
            status_code=409,
            detail="Work order was modified by someone else. Refresh and try again.",
        )

    # G6-A: this generic update applies `status` via a blind setattr with no
    # transition validation. Block the one dangerous transition -- resurrecting a
    # terminal WO (CANCELLED/CLOSED/COMPLETE) back to a non-terminal status -- with a
    # 409, consistent with how the release/start endpoints gate transitions. This is
    # intentionally minimal (not a full state machine); it only stops a terminal->
    # non-terminal flip that would reopen a finished/cancelled job.
    new_status = update_data.get("status")
    if new_status is not None and work_order.status in TERMINAL_WO_STATUSES and new_status not in TERMINAL_WO_STATUSES:
        current = work_order.status.value if hasattr(work_order.status, "value") else work_order.status
        target = new_status.value if hasattr(new_status, "value") else new_status
        raise HTTPException(
            status_code=409,
            detail=f"cannot move work order out of terminal status '{current}' to '{target}'",
        )

    # The other dangerous direction: a blind setattr to COMPLETE/CLOSED would mark the
    # job finished while PERMANENTLY bypassing every completion inventory effect (FG
    # receipt, tied-material consumption, backflush, cost rollup) -- every completion
    # verb and the reconcile refuse terminal WOs afterwards, so nothing ever heals it.
    # Completion must go through its own endpoint, which runs the full effect chain.
    # The source-status exemption is deliberately COMPLETE/CLOSED only, NOT all of
    # TERMINAL_WO_STATUSES: COMPLETE -> CLOSED is an archival move between two states
    # whose completion chain already ran (and a resend of the current status is
    # idempotent), but a CANCELLED WO never ran that chain -- so CANCELLED ->
    # COMPLETE/CLOSED is the exact fabricated completion this guard exists to stop,
    # and guard 1 above cannot catch it (the target is terminal too).
    if (
        new_status is not None
        and new_status in (WorkOrderStatus.COMPLETE, WorkOrderStatus.CLOSED)
        and work_order.status not in (WorkOrderStatus.COMPLETE, WorkOrderStatus.CLOSED)
    ):
        target = new_status.value if hasattr(new_status, "value") else new_status
        if work_order.status == WorkOrderStatus.CANCELLED:
            # Pointing at POST /complete would mislead here: the completion endpoint
            # (correctly) refuses terminal WOs, so a cancelled job can NEVER become
            # complete/closed -- it stays cancelled.
            raise HTTPException(
                status_code=409,
                detail=(
                    f"cannot mark a cancelled work order '{target}': its completion effect chain never ran "
                    "and never will. A cancelled work order stays cancelled."
                ),
            )
        raise HTTPException(
            status_code=409,
            detail=(
                f"cannot set status '{target}' through this generic update: completing a work order has "
                "inventory and audit side effects that would be permanently skipped. "
                f"Use POST /work-orders/{work_order.id}/complete instead."
            ),
        )

    # Owner ruling 2026-11-05: a hold stops work on the FLOOR, and a draft has nothing
    # on the floor to stop. Refusing DRAFT -> ON_HOLD here (the only writer of a
    # work-order ON_HOLD) means every hold placed from now on is on released work, so
    # POST /work-orders/{id}/resume never has to guess whether a held job was ever
    # authorized -- and an MCP-imported DRAFT laser job can no longer be laundered
    # into RELEASED through a hold and a resume. Re-sending on_hold on a job already on
    # hold stays a no-op; ON_HOLD -> DRAFT / CANCELLED stay allowed (restrictive moves).
    if new_status == WorkOrderStatus.ON_HOLD and work_order.status == WorkOrderStatus.DRAFT:
        raise HTTPException(status_code=409, detail=MSG_DRAFT_CANNOT_BE_HELD)

    # A finished job's due date is its PROMISE date: OTD scores against
    # `coalesce(must_ship_by, due_date)`, so moving it on a COMPLETE/CLOSED/CANCELLED
    # work order silently rewrites a delivery result that is already recorded -- and
    # the rewrite is indistinguishable, after the fact, from the job having always
    # been due then. Refuse it here rather than in the UI: this used to be hidden on
    # the work-order list and permitted on the detail page and the API, which made the
    # audit trail look like a restriction was holding when two other doors were open.
    #
    # Deliberately NARROW in three ways. It refuses only the `due_date` component, not
    # the whole request -- `notes` / `special_instructions` / `unit_number` stay
    # editable at any status, which is a documented posture (a correction written after
    # the fact is exactly what those fields are for). It refuses only an actual CHANGE,
    # so re-sending the value a terminal WO already has stays idempotent. And it runs
    # before the first setattr, so a refusal leaves the row untouched.
    if "due_date" in update_data and work_order.status in TERMINAL_WO_STATUSES:
        if update_data["due_date"] != work_order.due_date:
            current = work_order.status.value if hasattr(work_order.status, "value") else work_order.status
            raise HTTPException(
                status_code=409,
                detail=(
                    f"cannot change the due date of a work order in terminal status '{current}': its due date is "
                    "the promise date its delivery performance was scored against."
                ),
            )

    # Owner ruling 1 (INT8): lowering the order below what a whole-order stage has already
    # accounted for (good + scrap) would turn that stage's surplus into a short close of
    # everything downstream, after the fact. Judged against the CURRENT order, before the
    # setattr loop writes the new one. Pieces never started are the legitimate case --
    # lowering by those is allowed. Laser nests (planned_runs) are untouched by design.
    if "quantity_ordered" in update_data and update_data["quantity_ordered"] is not None:
        new_quantity_ordered = float(update_data["quantity_ordered"])
        if new_quantity_ordered < float(work_order.quantity_ordered or 0) - QTY_EPSILON:
            ordered_floor, floor_operation = quantity_ordered_floor(
                work_order, active_work_order_operations(work_order)
            )
            if floor_operation is not None and new_quantity_ordered < ordered_floor - QTY_EPSILON:
                raise HTTPException(
                    status_code=409,
                    detail=quantity_ordered_floor_message(floor_operation, ordered_floor),
                )

    # 081: remember which way sequencing moved BEFORE the setattr loop overwrites it.
    # Only pooled -> sequenced needs repair; the other direction heals itself, because
    # promotion is forward-only and the next reconciling read re-promotes the pool.
    sequencing_turned_on = (
        "sequential_operations" in update_data
        and bool(update_data["sequential_operations"])
        and not bool(work_order.sequential_operations)
    )

    # Refuse BEFORE the first setattr, so a refused flip leaves the row untouched.
    # Every completion verb re-evaluates the predecessor gate at action time, so an
    # operation being worked ahead of its predecessors would become uncompletable the
    # instant this flag flips -- and nothing would ever heal it. Better to refuse and
    # name the operations than to brick live work on a tablet.
    if sequencing_turned_on:
        # A nest package pools by TYPE -- is_laser_dispatch_work_order short-circuits
        # above this flag at every seam, so accepting True here would persist a claim the
        # work order's own behavior contradicts and put "sequential" on a screen showing
        # every nest at once. Refuse rather than store a lie (the posture
        # POST /work-orders/{id}/operations already takes for laser WOs).
        if is_laser_dispatch_work_order(work_order):
            raise HTTPException(
                status_code=409,
                detail=(
                    "cannot switch a laser nest work order to sequential operations: its nests are a dispatch "
                    "pool and are deliberately startable in any order."
                ),
            )
        stranded = operations_worked_out_of_sequence(db, work_order)
        if stranded:
            names = ", ".join(f"OP{op.sequence} {op.name}" for op in stranded)
            raise HTTPException(
                status_code=409,
                detail=(
                    "cannot switch this work order to sequential operations: work is already under way out of "
                    f"sequence on {names}. Complete those operations (or the earlier ones they run ahead of) "
                    "first, then switch."
                ),
            )

    for field, value in update_data.items():
        setattr(work_order, field, value)

    # Turning sequencing ON must fix THIS work order, not just the next one. Every
    # promotion seam is forward-only, so operations the pooled rule already promoted
    # would otherwise sit READY on the dispatch board and the kiosk under a rule that
    # forbids them. Never touches worked operations -- see the helper's docstring.
    demoted_operations = demote_operations_for_sequencing(db, work_order) if sequencing_turned_on else []
    if demoted_operations:
        # Flush the demotions BEFORE the first audit call, and this ordering is load-bearing
        # (invariant 4). WorkOrderOperation maps version_id_col, so a concurrent clock-in or
        # hold on one of these rows makes the UPDATE stale. AuditService.log() flushes the
        # whole session inside a begin_nested() savepoint and swallows every exception, so
        # if the first log_status_change is what flushes them, the StaleDataError is eaten
        # there, the savepoint is left deactivated, and the terminal flush below raises
        # PendingRollbackError -- which no handler matches, turning the 409 this endpoint
        # promises into an opaque 500. Flushing here raises StaleDataError to the app-wide
        # handler instead, and mints audit rows only for demotions that actually landed.
        db.flush()
    for demoted_op in demoted_operations:
        # Invariant 2: a rule-driven status change still gets an audit row, and this
        # endpoint HAS an actor, so it is attributed rather than left to a read path.
        audit.log_status_change(
            resource_type="work_order_operation",
            resource_id=demoted_op.id,
            resource_identifier=f"{work_order.work_order_number} OP{demoted_op.sequence}",
            old_status=OperationStatus.READY.value,
            new_status=OperationStatus.PENDING.value,
            description=(
                f"Operation returned to pending: work order {work_order.work_order_number} was switched to "
                "sequential operations, and an earlier operation is not complete."
            ),
            extra_data={
                "transition": "sequential_operations_enabled",
                # Every other work_order_operation audit row identifies the operation by
                # operation_number; carry it so a filter on that identifier still finds
                # the demotion beside the operation's completion history.
                "operation_number": demoted_op.operation_number,
                "sequence": demoted_op.sequence,
            },
        )

    _emit_work_order_event(
        db,
        company_id=company_id,
        current_user=current_user,
        work_order=work_order,
        event_type="work_order_updated",
        payload={
            "updated_fields": list(update_data.keys()),
            "operations_returned_to_pending": len(demoted_operations),
        },
    )

    # Audit log for update. Logged BEFORE the terminal commit so the audit row commits
    # atomically with the change — AuditService.log() only flushes and the request
    # session never commits on teardown.
    db.flush()
    audit.log_update(
        resource_type="work_order",
        resource_id=work_order.id,
        resource_identifier=work_order.work_order_number,
        old_values=old_values,
        new_values=work_order,
    )
    db.commit()
    db.refresh(work_order)

    safe_broadcast(
        broadcast_work_order_update,
        work_order.id,
        {
            "event": "work_order_updated",
            "status": work_order.status.value if hasattr(work_order.status, "value") else work_order.status,
        },
        company_id=company_id,
    )
    safe_broadcast(
        broadcast_dashboard_update,
        {
            "event": "work_order_updated",
            "work_order_id": work_order.id,
            "status": work_order.status.value if hasattr(work_order.status, "value") else work_order.status,
        },
        company_id=company_id,
    )

    return work_order


@router.put("/{work_order_id}/priority")
def update_work_order_priority(
    work_order_id: int,
    priority_in: WorkOrderPriorityUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
):
    """Update only work order priority for quick dispatch changes."""
    work_order = (
        db.query(WorkOrder)
        .options(joinedload(WorkOrder.operations))
        .filter(WorkOrder.id == work_order_id, WorkOrder.company_id == company_id)
        .first()
    )
    if not work_order:
        raise HTTPException(status_code=404, detail="Work order not found")

    old_priority = work_order.priority
    reason = (priority_in.reason or "").strip() or None

    with atomic_transaction(db):
        work_order.priority = priority_in.priority
        work_order.updated_at = datetime.utcnow()
        db.flush()

        audit = AuditService(db, current_user, request)
        audit.log_update(
            resource_type="work_order",
            resource_id=work_order.id,
            resource_identifier=work_order.work_order_number,
            old_values={"priority": old_priority},
            new_values={"priority": work_order.priority},
            description=(
                f"Updated work_order priority: {work_order.work_order_number}"
                + (f" (reason: {reason})" if reason else "")
            ),
            extra_data={"priority_reason": reason} if reason else None,
        )
        _emit_work_order_event(
            db,
            company_id=company_id,
            current_user=current_user,
            work_order=work_order,
            event_type="work_order_priority_updated",
            severity="medium" if work_order.priority <= 2 else "info",
            payload={"old_priority": old_priority, "new_priority": work_order.priority, "reason": reason},
        )

    db.refresh(work_order)

    safe_broadcast(
        broadcast_work_order_update,
        work_order.id,
        {
            "event": "work_order_priority_updated",
            "priority": work_order.priority,
            "reason": reason,
        },
        company_id=company_id,
    )
    safe_broadcast(
        broadcast_dashboard_update,
        {
            "event": "work_order_priority_updated",
            "work_order_id": work_order.id,
            "priority": work_order.priority,
            "reason": reason,
        },
        company_id=company_id,
    )

    work_center_ids = list(
        {
            op.work_center_id
            for op in work_order.operations
            if op.work_center_id and op.status != OperationStatus.COMPLETE
        }
    )
    for wc_id in work_center_ids:
        safe_broadcast(
            broadcast_shop_floor_update,
            wc_id,
            {
                "event": "work_order_priority_updated",
                "work_order_id": work_order.id,
                "priority": work_order.priority,
                "reason": reason,
            },
            company_id=company_id,
        )

    return {
        "message": f"Priority updated for {work_order.work_order_number}",
        "work_order_id": work_order.id,
        "priority": work_order.priority,
        "reason": reason,
    }


@router.delete("/{work_order_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_work_order(
    work_order_id: int,
    request: Request,
    hard_delete: bool = Query(False, description="Permanently delete (only for draft/cancelled WOs)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
):
    """
    Soft delete or permanently delete a work order.

    Allowed for admins and managers.

    **Soft delete (default)**: Marks the work order as deleted but preserves data.

    **Hard delete**: Only allowed for draft or cancelled work orders.
    Permanently removes the record and associated operations. Refused **409** when
    inventory-ledger movement is tied to it, or when a work order template was saved
    from it — a template copies its plan live off the job it points at, so the row has
    to outlive the template. A soft-deleted template still counts: its foreign key is
    just as real, and only Postgres would notice. Both refusals name a remedy, and both
    leave soft delete available: a soft-deleted work order keeps every template working.
    """
    work_order = db.query(WorkOrder).filter(WorkOrder.id == work_order_id, WorkOrder.company_id == company_id).first()
    if not work_order:
        raise HTTPException(status_code=404, detail="Work order not found")

    audit = AuditService(db, current_user, request)
    wo_number = work_order.work_order_number
    wo_id = work_order.id

    if hard_delete:
        # Only draft or cancelled can be hard deleted
        if work_order.status not in [WorkOrderStatus.DRAFT, WorkOrderStatus.CANCELLED]:
            raise HTTPException(
                status_code=400,
                detail="Only draft or cancelled work orders can be hard deleted. Use soft delete instead.",
            )

        # Material ties FK-reference this WO and its operations, so they must go with it
        # -- but a tie a ledger row points at CANNOT: inventory_transactions.allocation_id
        # has to keep resolving. Ask the LEDGER, not the qty_consumed cache: the cache is
        # documented as non-authoritative (model docstring) and the FK carries no
        # ON DELETE, so any drift between the two would surface as an IntegrityError 500
        # instead of this 409.
        tie_rows = allocations_on_work_order(db, work_order_id=wo_id, company_id=company_id)
        blocked_ids = ledger_backed_allocation_ids(
            db, allocation_ids=[row.id for row in tie_rows], company_id=company_id
        )
        if blocked_ids:
            # Name a remedy that EXISTS. This used to say "Reverse consumption first",
            # and PR 3's RETURN verb is deliberately NOT that remedy: a return APPENDS a
            # compensating row carrying the same ``allocation_id``, so a fully returned
            # tie is still ledger-backed and this guard still fires -- correctly, because
            # the hard delete would remove the tie those rows resolve through. Soft
            # delete is the answer, and it keeps the material history intact.
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Material movement is on the inventory ledger for {len(blocked_ids)} tied "
                    "allocation(s) on this work order, so it cannot be permanently deleted — returning "
                    "the material does not remove that history. Soft delete instead; the work order and "
                    "its material record stay intact."
                ),
            )

        # A saved TEMPLATE points at this work order, and a template reads its plan LIVE
        # off the work order it points at -- there is no frozen copy anywhere. Since
        # templates now read THROUGH a soft-delete (owner decision: a catalog entry must
        # not stop working because somebody deleted a job), this FK is what guarantees
        # "no work order present" can only ever mean tombstoned, never physically gone.
        #
        # That guarantee was an ACCIDENT until now: work_order_templates
        # .source_work_order_id is NOT NULL with a plain FK and no ON DELETE, so this
        # branch hit a ForeignKeyViolation on Postgres and surfaced as a 500. Something
        # load-bearing should refuse legibly and name what is in the way, before the
        # first mutation. Soft delete is deliberately unaffected -- it is the whole
        # point: the templates keep working.
        #
        # The probe reads TOMBSTONED templates too (``templates_pointing_at_work_order``,
        # not ``live_templates_query``). Postgres does not consult ``is_deleted`` before
        # refusing to drop a referenced row, so a soft-deleted template holds this FK
        # exactly as hard as a live one -- filtering it out would put the 500 back for
        # that population, and the in-memory SQLite the suite runs on could never show
        # it (foreign-key enforcement is off there). Only LIVE templates are NAMED: a
        # tombstone has no name the planner can act on, and "delete those templates
        # first" is not a remedy for one that is already deleted. Soft delete is, and it
        # is always available.
        blocking_templates = (
            templates_pointing_at_work_order(db, wo_id, company_id)
            .order_by(WorkOrderTemplate.name, WorkOrderTemplate.id)
            .all()
        )
        if blocking_templates:
            live_names = [t.name for t in blocking_templates if not t.is_deleted]
            named = f" ({', '.join(repr(name) for name in live_names)})" if live_names else ""
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{len(blocking_templates)} work order template(s) were saved from this work order"
                    f"{named}, and a template copies its plan straight off the job it points at, so "
                    "permanently deleting it would leave them with nothing to run. Soft delete instead — "
                    "a soft-deleted work order keeps every template working."
                ),
            )

        for tie in tie_rows:
            audit.log_delete(
                "work_order_material_allocation",
                tie.id,
                f"WO {wo_number} / part {tie.part_id}",
                old_values={"status": tie.status.value, "qty_consumed": tie.qty_consumed},
                description=f"Removed material allocation with hard-deleted work order {wo_number}",
                extra_data={"reason": "work_order_hard_deleted", "work_order_id": wo_id},
            )
            db.delete(tie)

        # Delete operations first
        for op in work_order.operations:
            db.delete(op)

        db.delete(work_order)

        # Audit BEFORE the terminal commit so the audit row commits atomically with the
        # delete — AuditService.log() only flushes and the session never commits on teardown.
        audit.log_delete("work_order", wo_id, wo_number)
        db.commit()
        safe_broadcast(
            broadcast_dashboard_update,
            {
                "event": "work_order_deleted",
                "work_order_id": wo_id,
                "status": "deleted",
            },
            company_id=company_id,
        )
        safe_broadcast(
            broadcast_work_order_update,
            wo_id,
            {
                "event": "work_order_deleted",
                "status": "deleted",
            },
            company_id=company_id,
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # Soft delete - allowed for any status
    work_order.soft_delete(current_user.id)

    # Close out forward-looking material demand: every OPEN tie is auto-CANCELLED
    # (audited). Consumption already posted STANDS -- the material was physically used
    # and the ledger is the compliance record -- so a consumed tie never refuses the
    # delete, it just stops accruing.
    cancel_open_allocations_for_work_order(db, work_order=work_order, company_id=company_id, audit=audit)

    # Audit BEFORE the terminal commit so the audit row commits atomically with the
    # soft delete — AuditService.log() only flushes and the session never commits on teardown.
    audit.log_delete("work_order", wo_id, wo_number, soft_delete=True)
    db.commit()
    safe_broadcast(
        broadcast_dashboard_update,
        {
            "event": "work_order_deleted",
            "work_order_id": wo_id,
            "status": "deleted",
        },
        company_id=company_id,
    )
    safe_broadcast(
        broadcast_work_order_update,
        wo_id,
        {
            "event": "work_order_deleted",
            "status": "deleted",
        },
        company_id=company_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{work_order_id}/restore",
    response_model=WorkOrderRestoreResponse,
    summary="Restore a soft-deleted work order",
)
def restore_work_order(
    work_order_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
) -> WorkOrderRestoreResponse:
    """Restore a soft-deleted work order.

    Returns an ENVELOPE rather than a bare message: the tie re-open below is allowed to
    leave a tie CANCELLED (its part was reclassified into something the shop PRODUCES
    while the work order was deleted), and a dropped tie with no channel is precisely the
    failure the duplicate path's skip envelope exists to prevent -- the job runs, no
    shortage shows, and stock is never deducted until the count disagrees. ``message`` is
    unchanged and still present, so existing callers are unaffected.
    """
    work_order = db.query(WorkOrder).filter(WorkOrder.id == work_order_id, WorkOrder.company_id == company_id).first()
    if not work_order:
        raise HTTPException(status_code=404, detail="Work order not found")

    if not work_order.is_deleted:
        raise HTTPException(status_code=400, detail="Work order is not deleted")

    audit = AuditService(db, current_user, request)

    work_order.restore()

    # "A restore returns the RECORD, not the permission" (invariant 3): a work
    # order deleted while it sat in the dispatch plan comes back NOT in the plan,
    # so it cannot silently reclaim a position -- or share one with a live job and
    # interleave with it -- that the planner never gave it back. Leaving the plan
    # also clears its operations' within-plan order (Core UPDATE: no version bump,
    # updated_at preserved), exactly as the plan verb does for a work order it
    # drops, so that stale order cannot float its operations up the unplanned tail.
    restored_from_plan_rank = work_order.dispatch_rank
    if restored_from_plan_rank is not None:
        work_order.dispatch_rank = None
        dispatch_service.clear_run_order_for_work_orders(db, company_id, [work_order.id])

    # Symmetry with the soft delete, which auto-CANCELLED every OPEN tie: put back
    # exactly those, so restored work keeps depleting its tied material. Leaving them
    # cancelled means the work order completes and material silently never moves — and
    # once `backflush_components` is exposed, a consumed-then-cancelled operation tie
    # stops suppressing the BOM backflush, double-issuing the same part. Ties cancelled
    # for ANY other reason (a manual untie, a nest re-import) are deliberately left
    # alone; the discriminator is the cancel's own audit reason.
    #
    # One tie it will NOT put back: one whose part is now a MANUFACTURED part or an
    # ASSEMBLY. Re-opening that would re-arm demand that issues finished goods to build
    # the job — the state `material_tie_part_gate` refuses 422 at both tie-write doors —
    # and it is reachable in three supported verbs (delete → reclassify → restore),
    # because the conversion gate counts only OPEN ties and this delete cancelled them.
    # Those skips are reported to the planner and recorded on the audit row below.
    tie_restore = reopen_allocations_cancelled_by_delete(db, work_order=work_order, company_id=company_id, audit=audit)

    # Audit BEFORE the terminal commit so the audit row commits atomically with the
    # restore — AuditService.log() only flushes and the session never commits on teardown.
    db.flush()
    restore_old_values: dict = {"is_deleted": True}
    restore_new_values: dict = {"is_deleted": False}
    if restored_from_plan_rank is not None:
        restore_old_values["dispatch_rank"] = restored_from_plan_rank
        restore_new_values["dispatch_rank"] = None
    audit.log_update(
        "work_order",
        work_order.id,
        work_order.work_order_number,
        old_values=restore_old_values,
        new_values=restore_new_values,
        action="restore",
        # Both lists unconditionally, the duplicate path's convention: an empty
        # `skipped_material_allocations` is a positive statement that nothing was dropped,
        # which is worth more on a tamper-evident chain than an absent key. `model_dump()`
        # rather than hand-built dicts, so the chain and the response can never describe a
        # skip differently. The SKIPS are audited here, on the verb's own row, because
        # nothing changed on the allocation itself and a chain row must describe something
        # that happened (invariant 2); each RE-OPEN wrote its own row already.
        extra_data={
            "reopened_material_allocations": tie_restore.reopened,
            "skipped_material_allocations": [entry.model_dump() for entry in tie_restore.skipped],
        },
    )
    db.commit()

    return WorkOrderRestoreResponse(
        message=f"Work order {work_order.work_order_number} restored",
        skipped_material_allocations=tie_restore.skipped,
    )


@router.post("/{work_order_id}/release", response_model=WorkOrderResponse)
def release_work_order(
    work_order_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
):
    """Release a work order to production"""
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
        raise HTTPException(status_code=404, detail="Work order not found")

    if work_order.status != WorkOrderStatus.DRAFT:
        raise HTTPException(status_code=400, detail="Only draft work orders can be released")

    # Verify has at least one operation
    if not work_order.operations:
        raise HTTPException(status_code=400, detail="Work order must have at least one operation")

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
    _emit_work_order_event(
        db,
        company_id=company_id,
        current_user=current_user,
        work_order=work_order,
        event_type="work_order_released",
        payload={"old_status": old_status, "new_status": WorkOrderStatus.RELEASED.value},
    )

    # Audit log for status change. Logged BEFORE the terminal commit so the audit row
    # commits atomically with the status change — AuditService.log() only flushes and the
    # request session never commits on teardown.
    db.flush()
    audit = AuditService(db, current_user, request)
    audit.log_status_change(
        resource_type="work_order",
        resource_id=work_order.id,
        resource_identifier=work_order.work_order_number,
        old_status=old_status,
        new_status="released",
    )

    db.commit()

    work_center_ids = list({op.work_center_id for op in work_order.operations if op.work_center_id})
    SchedulingService(db).run_scheduling(
        work_center_ids=work_center_ids or None, horizon_days=90, optimize_setup=False, work_order_ids=[work_order.id]
    )

    db.refresh(work_order)
    safe_broadcast(
        broadcast_work_order_update,
        work_order.id,
        {
            "event": "work_order_released",
            "status": work_order.status.value if hasattr(work_order.status, "value") else work_order.status,
        },
        company_id=company_id,
    )
    safe_broadcast(
        broadcast_dashboard_update,
        {
            "event": "work_order_released",
            "work_order_id": work_order.id,
            "status": work_order.status.value if hasattr(work_order.status, "value") else work_order.status,
        },
        company_id=company_id,
    )
    for wc_id in work_center_ids:
        safe_broadcast(
            broadcast_shop_floor_update,
            wc_id,
            {
                "event": "work_order_released",
                "work_order_id": work_order.id,
            },
            company_id=company_id,
        )
    return work_order


@router.post("/{work_order_id}/start")
def start_work_order(
    work_order_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
):
    """Start a RELEASED work order (Admin/Manager/Supervisor): sets it IN_PROGRESS.

    Refuses **409** on a draft (release it first: release_work_order) or on-hold
    (work_orders_resume_work_order lifts a hold -- this verb no longer does) work
    order, **400** on any other non-released status, **404** when missing or
    soft-deleted.
    """
    work_order = (
        db.query(WorkOrder)
        .filter(
            WorkOrder.id == work_order_id,
            WorkOrder.company_id == company_id,
            WorkOrder.is_deleted == False,  # noqa: E712
        )
        .first()
    )
    if not work_order:
        raise HTTPException(status_code=404, detail="Work order not found")

    # Owner ruling 2026-11-05: starting is for RELEASED work only. It used to accept
    # ON_HOLD and silently lift the hold -- open to ANY authenticated user, operators
    # included. A hold is lifted by POST /work-orders/{id}/resume, which records the
    # restore decision on the required audit chain.
    if work_order.status == WorkOrderStatus.DRAFT:
        raise HTTPException(status_code=409, detail=MSG_WORK_ORDER_NOT_RELEASED)
    if work_order.status == WorkOrderStatus.ON_HOLD:
        raise HTTPException(status_code=409, detail=MSG_WORK_ORDER_ON_HOLD)
    if work_order.status != WorkOrderStatus.RELEASED:
        raise HTTPException(status_code=400, detail="Work order must be released to start")

    old_status = work_order.status.value if work_order.status else None
    work_order.status = WorkOrderStatus.IN_PROGRESS
    if not work_order.actual_start:
        work_order.actual_start = datetime.utcnow()

    _emit_work_order_event(
        db,
        company_id=company_id,
        current_user=current_user,
        work_order=work_order,
        event_type="work_order_started",
        payload={"actual_start": to_utc_iso(work_order.actual_start)},
    )

    # Audit the status transition on the tamper-evident chain. Logged BEFORE the
    # terminal commit so the audit row commits atomically with the status change.
    db.flush()
    AuditService(db, current_user, request).log_status_change(
        resource_type="work_order",
        resource_id=work_order.id,
        resource_identifier=work_order.work_order_number,
        old_status=old_status,
        new_status=WorkOrderStatus.IN_PROGRESS.value,
    )
    try:
        db.commit()
    except StaleDataError as exc:
        # A concurrent writer bumped the work order's version between read and commit
        # (version_id_col mismatch). Surface a clean 409, not a 500.
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="This work order was modified concurrently. Refresh and retry.",
        ) from exc
    safe_broadcast(
        broadcast_work_order_update,
        work_order.id,
        {
            "event": "work_order_started",
            "status": work_order.status.value if hasattr(work_order.status, "value") else work_order.status,
        },
        company_id=company_id,
    )
    safe_broadcast(
        broadcast_dashboard_update,
        {
            "event": "work_order_started",
            "work_order_id": work_order.id,
            "status": work_order.status.value if hasattr(work_order.status, "value") else work_order.status,
        },
        company_id=company_id,
    )
    return {"message": "Work order started"}


RESUME_AUDIT_UNAVAILABLE_DETAIL = (
    "The audit record for resuming this work order could not be saved; nothing was changed. Retry."
)
RESUME_MESSAGES = {
    WorkOrderStatus.IN_PROGRESS: "Work order resumed — it is back in progress.",
    WorkOrderStatus.RELEASED: "Work order resumed — it is released and back on the floor.",
    WorkOrderStatus.DRAFT: (
        "No release is on record for this work order, so it returned to DRAFT — release it to put it back on "
        "the floor."
    ),
}


@router.post("/{work_order_id}/resume", response_model=WorkOrderResumeResponse)
def resume_work_order(
    work_order_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Resume Work Order: lift a WORK-ORDER hold (Admin/Manager/Supervisor).

    The one door out of a work-order ON_HOLD back to the floor; clock-in and every start
    verb refuse 409 on a held job until it runs. Resuming never RELEASES a job -- Release
    is the authorization step -- so the target comes from the release record:

    * released (``released_at`` / ``released_by`` set) and work already done -> IN_PROGRESS;
    * released, no work yet -> RELEASED;
    * no release on record -> DRAFT, with every un-worked READY operation returned to
      PENDING (one audited status change each); release it with release_work_order.

    Operation-level holds are untouched (lift them with Resume / Clear Hold). Refuses
    **400** when the work order is not on hold, **404** when missing or soft-deleted, and
    **503** (nothing changed) when the required audit record cannot be written. Returns
    ``status``, ``restore_basis`` and ``operations_returned_to_pending``.
    """
    work_order = (
        db.query(WorkOrder)
        .filter(
            WorkOrder.id == work_order_id,
            WorkOrder.company_id == company_id,
            WorkOrder.is_deleted == False,  # noqa: E712
        )
        .first()
    )
    if not work_order:
        raise HTTPException(status_code=404, detail="Work order not found")
    if work_order.status != WorkOrderStatus.ON_HOLD:
        raise HTTPException(status_code=400, detail="Work order is not on hold")

    outcome = apply_work_order_resume(db, work_order)
    demoted = outcome.demoted_operations

    # Flush the status write and every demotion BEFORE the first audit call (invariant
    # 4): WorkOrder and WorkOrderOperation both map version_id_col, so a concurrent hold
    # / clock-in makes this flush raise StaleDataError to the app-wide 409 handler --
    # rather than being swallowed inside AuditService.log's savepoint and resurfacing as
    # an opaque 500 (the same ordering note as update_work_order's sequencing demotion).
    db.flush()

    # REQUIRED evidence: a resume that cannot be audited does not happen (503, rolled
    # back). The hold itself is still written by PUT's best-effort log_update; a resume
    # decides where a job goes back to, and that decision must be on the chain.
    with required_audit(db, RESUME_AUDIT_UNAVAILABLE_DETAIL):
        audit.log_required(
            action=AuditService.ACTIONS["STATUS_CHANGE"],
            resource_type="work_order",
            resource_id=work_order.id,
            resource_identifier=work_order.work_order_number,
            description=(
                f"Work order {work_order.work_order_number} resumed from hold to '{outcome.status.value}' "
                f"({outcome.restore_basis})"
            ),
            old_values={"status": outcome.previous_status.value},
            new_values={"status": outcome.status.value},
            extra_data={
                "transition": "work_order_hold_cleared",
                "restore_basis": outcome.restore_basis,
                "operations_returned_to_pending": len(demoted),
            },
        )
        log_demotions_required(
            audit,
            demoted,
            reason=(
                f"work order {work_order.work_order_number} was resumed with no release on record and returned "
                "to draft."
            ),
            extra={"transition": "work_order_resumed_to_draft"},
        )

    try:
        db.commit()
    except StaleDataError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="This work order was modified concurrently. Refresh and retry.",
        ) from exc

    status_value = outcome.status.value
    safe_broadcast(
        broadcast_work_order_update,
        work_order.id,
        {"event": "work_order_resumed", "status": status_value},
        company_id=company_id,
    )
    safe_broadcast(
        broadcast_dashboard_update,
        {"event": "work_order_resumed", "work_order_id": work_order.id, "status": status_value},
        company_id=company_id,
    )
    # The work centers come from the operations apply_work_order_resume already read
    # (tenant-scoped, before the commit) -- no second query.
    for wc_id in outcome.work_center_ids:
        safe_broadcast(
            broadcast_shop_floor_update,
            wc_id,
            {"event": "work_order_resumed", "work_order_id": work_order.id},
            company_id=company_id,
        )
    return WorkOrderResumeResponse(
        work_order_id=work_order.id,
        message=RESUME_MESSAGES[outcome.status],
        status=outcome.status,
        restore_basis=outcome.restore_basis,
        operations_returned_to_pending=len(demoted),
    )


@router.get("/{work_order_id}/material-requirements")
def get_material_requirements(
    work_order_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get BOM material requirements for a work order with quantities calculated"""
    # (``BOM`` / ``BOMItem`` are imported at module scope; the local re-import that used to
    # sit here went unused once the BOM lookup moved to ``_get_active_bom``.)
    work_order = db.query(WorkOrder).filter(WorkOrder.id == work_order_id, WorkOrder.company_id == company_id).first()
    if not work_order:
        raise HTTPException(status_code=404, detail="Work order not found")

    # Get BOM for the part, through THE shared lookup: this hand-rolled query filtered
    # neither ``company_id`` (invariant 1 — it could resolve another tenant's BOM for the
    # same part id) nor ``is_deleted`` (invariant 3).
    bom = _get_active_bom(db, work_order.part_id, company_id)

    if not bom:
        return {
            "work_order_id": work_order_id,
            "work_order_number": work_order.work_order_number,
            "quantity_ordered": float(work_order.quantity_ordered),
            "has_bom": False,
            "materials": [],
        }

    # Get BOM items with component parts. Both predicates are invariant #1: the line query
    # carried no ``company_id``, and the components came off ``joinedload(BOMItem.
    # component_part)`` -- a relationship with no ``company_id`` predicate, which on a
    # mis-parented line rendered ANOTHER COMPANY's part number and name into this response.
    # Resolved TENANT-SCOPED and batched instead; see ``tenant_parts_by_id``.
    items = db.query(BOMItem).filter(BOMItem.bom_id == bom.id, BOMItem.company_id == company_id).all()
    components_by_id = tenant_parts_by_id(db, [item.component_part_id for item in items], company_id)

    materials = []
    for item in items:
        component = components_by_id.get(item.component_part_id)
        if component:
            qty_per_assembly = float(item.quantity)
            qty_required = qty_per_assembly * float(work_order.quantity_ordered)
            scrap_allowance = qty_required * float(item.scrap_factor or 0)
            total_required = qty_required + scrap_allowance

            materials.append(
                {
                    "bom_item_id": item.id,
                    "item_number": item.item_number,
                    "part_id": component.id,
                    "part_number": component.part_number,
                    "part_name": component.name,
                    "part_type": (
                        component.part_type.value if hasattr(component.part_type, 'value') else component.part_type
                    ),
                    "quantity_per_assembly": qty_per_assembly,
                    "quantity_required": round(qty_required, 3),
                    "scrap_factor": float(item.scrap_factor or 0),
                    "scrap_allowance": round(scrap_allowance, 3),
                    "total_required": round(total_required, 3),
                    # The one other place a BOM line's unit falls back to its component
                    # part's. Routed through ``uom_label`` so it agrees with
                    # ``uom_disagrees`` / ``GET /bom/uom-mismatches`` on what a part's unit
                    # IS, and so a part with a NULL ``unit_of_measure`` no longer 500s here
                    # on ``.value`` — the column is nullable and always has been.
                    "unit_of_measure": (
                        item.unit_of_measure or uom_label(component.unit_of_measure) or UnitOfMeasure.EACH.value
                    ),
                    "item_type": item.item_type.value if hasattr(item.item_type, 'value') else item.item_type,
                    "is_optional": item.is_optional,
                    "notes": item.notes,
                }
            )

    return {
        "work_order_id": work_order_id,
        "work_order_number": work_order.work_order_number,
        "quantity_ordered": float(work_order.quantity_ordered),
        "has_bom": True,
        "bom_id": bom.id,
        "bom_revision": bom.revision,
        "materials": sorted(materials, key=lambda x: x["item_number"]),
    }


# S2: cap on the per-step bypass entries stamped onto the force-complete audit row /
# response — the count is always exact; only the itemized list truncates (flagged).
STEPS_BYPASSED_AUDIT_CAP = 50


@router.post("/{work_order_id}/complete")
def complete_work_order(
    work_order_id: int,
    request: Request,
    quantity_complete: float,
    quantity_scrapped: Optional[float] = None,
    scrap_reason: Optional[str] = None,
    scrap_reason_code_id: Optional[int] = None,
    quantity_override_reason: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(
        require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR, UserRole.QUALITY])
    ),
    company_id: int = Depends(get_current_company_id),
):
    """Manually complete a work order (privileged override), booking quantity_complete as its
    finished count. It may not exceed what the floor recorded without quantity_override_reason
    (409 names the binding operation), and a shortfall below the order must be covered by scrap
    the floor reported or by quantity_scrapped with a reason (409); an API token may not complete
    short (403).

    Owner ruling 1 (2026-11-05), enforced at the WORK-ORDER grain: a short is reported as scrap
    with a reason, or the work order stays open. The bounds come from the floor's own evidence
    (``work_order_state_service.work_order_completion_bounds``, fed by one grouped TimeEntry read):
    ``deliverable`` -- the most the recorded good supports at the binding operation (exceeding it
    needs ``quantity_override_reason``, written as REQUIRED audit evidence); ``physical_max`` -- the
    most that can exist once declared shorts are out (400 above it, even with a reason);
    ``floor_loss`` -- units the floor already accounted as lost. ``ordered - quantity_complete``
    must be covered by ``max(floor_loss, work-order scrap)``. The header is SET to
    ``quantity_complete`` -- never ``max()``'d with the pre-loop header, which on a routing whose
    first stage completed at the full order would re-inflate the FG receipt past a declared short.

    DUP-4: this override delegates to the SHARED rollup instead of blindly
    flipping the WO to COMPLETE. It force-completes every still-open operation
    through the shared finalizer -- each gets ``actual_end``/``completed_by``
    stamped, an audit row, and the WO ``actual_start``/qty-sync/scheduling refresh
    -- so it can no longer leave a COMPLETE WO with open operations and unreleased
    capacity. Open operations are closed WITHOUT invented per-operation counts.

    DUP-3 scrap parity (mirrors the op-level fix): ``quantity_scrapped`` is
    optional. When omitted (``None``) the WO's recorded scrap is left untouched so
    a defaulted call cannot ZERO previously-booked WO scrap; only an explicit value
    overwrites it.
    """
    # SFI-1 / LOCK-1: lock the WO row before this privileged manual read-modify
    # so two concurrent completers serialize. Then load+lock its operations so the
    # force-complete below runs against the freshest committed rows. Lock order is
    # WORK ORDER then OPERATIONS here (the manual override is WO-centric and starts
    # from a WO id); operations are locked in a deterministic id order.
    work_order = (
        db.query(WorkOrder)
        .filter(
            WorkOrder.id == work_order_id,
            WorkOrder.company_id == company_id,
            # Parity with every other completion path (invariant 3). Benign today
            # -- a soft delete cancels every open tie, so the consumption engine
            # this handler now drives finds nothing -- but that is a second-order
            # property of a different verb, not a guarantee this query should be
            # leaning on.
            WorkOrder.is_deleted == False,  # noqa: E712
        )
        .with_for_update()
        .first()
    )
    if not work_order:
        raise HTTPException(status_code=404, detail="Work order not found")

    # Idempotency guard (EVT-3 / e3): if this WO is ALREADY terminal (COMPLETE or
    # CLOSED) the completion already happened on a prior call. Re-running the body
    # would re-fire the work_order_completed event, write another COMPLETE/CLOSED
    # status-change audit row on the tamper-evident chain, and re-enqueue the outbound
    # completion signal -- a spurious duplicate per re-invoke. Return the existing
    # terminal state as a clean no-op so the signal/audit/event fire ONCE per real
    # transition. (A WO is only driven terminal AFTER every open op is force-completed
    # via the finalizer, so an already-terminal WO has no open ops to force-complete --
    # the "force-complete remaining open ops" path runs on a still-open WO below.)
    if work_order.status in (WorkOrderStatus.COMPLETE, WorkOrderStatus.CLOSED):
        return {
            "message": "Work order already completed",
            "already_completed": True,
            "status": work_order.status.value if hasattr(work_order.status, "value") else work_order.status,
            "quality_exceptions": [],
        }

    # G6-A: a CANCELLED WO is terminal and must NOT be silently completed. Unlike the
    # COMPLETE/CLOSED no-op above (the completion already happened), a CANCELLED WO was
    # deliberately taken out of production -- driving it to COMPLETE here would
    # resurrect a cancelled job, re-fire FG receipt/backflush/cost rollup, and write a
    # COMPLETE row onto the tamper-evident audit chain. Refuse with a 409 state conflict.
    if work_order.status == WorkOrderStatus.CANCELLED:
        raise HTTPException(status_code=409, detail="cannot complete a cancelled work order")

    # Bound the manager-supplied quantities (DUP-4): non-negative and not above the
    # quantity ordered. quantity_ordered is the natural cap for a finished WO.
    # quantity_complete is required; quantity_scrapped is optional (DUP-3) and only
    # bounded when explicitly provided.
    # Reject non-finite quantities (NaN/Inf) up front: a plain float query param accepts
    # "nan"/"inf", and NaN slips past every `> 0`/`< 0` guard below (including the scrap-
    # reason guard), which would persist a reasonless NaN scrap on Postgres (compliance
    # auditor). Mirrors the shop-floor /production isnan/isinf guard.
    if (quantity_complete is not None and not math.isfinite(quantity_complete)) or (
        quantity_scrapped is not None and not math.isfinite(quantity_scrapped)
    ):
        raise HTTPException(status_code=400, detail="Quantity must be a valid number")
    ordered_qty = float(work_order.quantity_ordered or 0)
    if quantity_complete is None or quantity_complete < 0:
        raise HTTPException(status_code=400, detail="quantity_complete cannot be negative")
    if quantity_scrapped is not None and quantity_scrapped < 0:
        raise HTTPException(status_code=400, detail="quantity_scrapped cannot be negative")
    # AS9100D defect-traceability invariant (same rule as ClockOut/ProductionReportRequest):
    # any positive scrap MUST carry a reason. Lean Phase 1: EITHER a structured
    # scrap_reason_code_id OR non-blank free text satisfies the rule (code preferred;
    # text-only clients keep working). Query-param path, so the guard lives in the
    # handler (no Pydantic body validator). 422 matches the scrap-reason enforcement
    # semantics established this session; blank/whitespace counts as missing.
    has_scrap_reason = bool(scrap_reason and scrap_reason.strip()) or scrap_reason_code_id is not None
    if quantity_scrapped is not None and quantity_scrapped > 0 and not has_scrap_reason:
        raise HTTPException(
            status_code=422,
            detail="scrap_reason or scrap_reason_code_id is required when quantity_scrapped is greater than 0",
        )
    # Lean Phase 1: resolve+validate the structured code BEFORE any mutation
    # (404 unknown/cross-tenant, 422 inactive). None passes through untouched.
    scrap_code = resolve_scrap_reason_code_or_http(db, company_id, scrap_reason_code_id)
    if ordered_qty > 0 and quantity_complete > ordered_qty:
        raise HTTPException(
            status_code=400,
            detail=f"quantity_complete ({quantity_complete}) cannot exceed quantity ordered ({ordered_qty})",
        )

    operations = (
        db.query(WorkOrderOperation)
        .options(selectinload(WorkOrderOperation.laser_nest))
        .filter(WorkOrderOperation.work_order_id == work_order.id, WorkOrderOperation.company_id == company_id)
        .order_by(WorkOrderOperation.id)
        .with_for_update()
        .all()
    )
    work_order.operations = operations
    # Keep cancelled-nest history attached, but do not force-complete its held
    # operation or treat it as a live hold that prevents closing this order.
    operations = active_work_order_operations(work_order)
    if is_laser_dispatch_work_order(work_order) and not operations:
        raise HTTPException(status_code=409, detail="Cannot complete a laser work order with no active nests")

    # QG-5 / BLK-1 consistency: this privileged override force-completes every open
    # op, but it must NOT silently lift a quality/material hold -- that contradicts
    # the ON_HOLD refusal the op-complete endpoints now enforce. Refuse (409) up
    # front, before mutating anything, if any open op is ON_HOLD. (Batch 4 adds an
    # audited QUALITY-role override for clearing a hold during completion.)
    held = next(
        (op for op in operations if op.status == OperationStatus.ON_HOLD),
        None,
    )
    if held is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"cannot complete work order: operation {held.operation_number or held.sequence} "
                "is on hold; resolve the hold first"
            ),
        )

    # Owner ruling 1 at the WORK-ORDER grain (INT1), before the steps-bypass read and any
    # mutation. ONE grouped, tenant-scoped TimeEntry read feeds the shared bounds -- the
    # same figures the work-order read model shows as quantity_deliverable /
    # quantity_accounted_loss, so the screen and the verb cannot disagree.
    completion_bounds = work_order_completion_bounds_from_evidence(
        work_order,
        operations,
        operation_labor_evidence(db, company_id, [op.id for op in operations]),
    )
    requested_complete = float(quantity_complete)
    override_reason = (quantity_override_reason or "").strip() or None
    asserted_above_recorded = requested_complete > completion_bounds.deliverable + QTY_EPSILON
    binding_operation = next(
        (op for op in operations if op.id == completion_bounds.binding_operation_id),
        None,
    )
    if asserted_above_recorded and override_reason is None:
        raise HTTPException(
            status_code=409,
            detail=force_complete_exceeds_recorded_message(
                requested_complete, completion_bounds.deliverable, binding_operation
            ),
        )
    if asserted_above_recorded and requested_complete > completion_bounds.physical_max + QTY_EPSILON:
        raise HTTPException(
            status_code=400,
            detail=force_complete_exceeds_physical_message(
                requested_complete,
                completion_bounds.physical_max,
                max(0.0, ordered_qty - completion_bounds.physical_max),
            ),
        )
    effective_scrap_claim = (
        float(quantity_scrapped) if quantity_scrapped is not None else float(work_order.quantity_scrapped or 0)
    )
    covered_loss = max(completion_bounds.floor_loss, effective_scrap_claim)
    unaccounted_short = ordered_qty - requested_complete - covered_loss
    if ordered_qty > 0 and unaccounted_short > QTY_EPSILON:
        raise HTTPException(
            status_code=409,
            detail=force_complete_unaccounted_message(unaccounted_short, ordered_qty, requested_complete, covered_loss),
        )
    completing_short = ordered_qty > 0 and requested_complete < ordered_qty - QTY_EPSILON
    if completing_short and is_api_token_credential(current_user):
        raise HTTPException(status_code=403, detail=API_TOKEN_SHORT_CLOSE)

    # S2 (settled, user decision): this privileged override stays UNGATED by the
    # process-sheet steps gate — it is an audited EVIDENCE-OVERRIDE. Make the bypass
    # deliberate and visible, never silent: BEFORE mutating, compute the required
    # step records the force-complete is about to bypass (still-open operations
    # only — already-COMPLETE ops passed their own gate at their own completion)
    # and stamp them on the force-complete audit row + the response below.
    steps_bypassed_all: list[dict] = []
    for operation in operations:
        if operation.status == OperationStatus.COMPLETE:
            continue
        # ONE vocabulary per collection: the fallback yields the bare sequence, exactly like
        # the ON_HOLD refusal above -- never a display label. These entries are stamped on the
        # force-complete AUDIT row (permanent, on the tamper-evident chain), so a mix of "10"
        # and "Op 10" inside one steps_bypassed list would be un-fixable after the fact.
        op_identifier = str(operation.operation_number or operation.sequence)
        for item in process_sheet_service.missing_required_steps(db, company_id, operation, work_order):
            steps_bypassed_all.append({"operation": op_identifier, **item})
    steps_bypassed_count = len(steps_bypassed_all)
    steps_bypassed_truncated = steps_bypassed_count > STEPS_BYPASSED_AUDIT_CAP
    steps_bypassed_entries = steps_bypassed_all[:STEPS_BYPASSED_AUDIT_CAP]

    old_status = work_order.status.value if work_order.status else None
    old_quantity_complete = float(work_order.quantity_complete or 0)
    old_quantity_scrapped = float(work_order.quantity_scrapped or 0)
    old_scrap_reason = work_order.scrap_reason
    old_scrap_reason_code_id = work_order.scrap_reason_code_id

    db.flush()
    audit = AuditService(db, current_user, request)

    # Force-complete each still-open operation through the shared path so each is
    # stamped + audited and the route is genuinely closed (no COMPLETE WO over open
    # ops). The last force-complete drives the WO to COMPLETE via the finalizer.
    now = datetime.utcnow()
    affected_work_centers: set[int] = set()
    # PERF-5: tracks whether the scheduling refresh ran (it runs with commit=False,
    # so the WC cache must be invalidated by us after the terminal commit succeeds).
    work_centers_refreshed = False
    for operation in operations:
        if operation.status == OperationStatus.COMPLETE:
            continue
        op_old_status = operation.status.value if operation.status else None
        if not operation.actual_start:
            operation.actual_start = now
            operation.started_by = operation.started_by or current_user.id
        operation.status = OperationStatus.COMPLETE
        operation.actual_end = now
        operation.completed_by = current_user.id
        operation.updated_at = now
        sync_laser_nest_from_operation(operation)
        affected_work_centers |= finalize_operation_completion(db, work_order, operation)

        # Material consumption (incremental), for symmetry with the three other
        # operation-completion paths. Consumes only what the operation RECORDED (good +
        # scrap) -- force-complete never writes ``operation.quantity_complete`` and never
        # stamps ``quantity_short_declared`` (the work order goes terminal, so nothing
        # downstream reads a declaration), so an unreported operation's sum-delta is
        # non-positive and posts nothing. Deliberately NOT "fixed" here -- whether a
        # privileged force-complete should book produced quantity per operation is a
        # separate product decision, and inventing one would silently deplete material for
        # runs nobody reported. The work-order bounds above are where ruling 1 is enforced.
        # The tie still flushes through the whole-WO reconcile in
        # apply_completion_inventory_effects below if the operation does carry produced
        # quantity from an earlier partial report.
        #
        # The flush keeps an autoflush StaleDataError out of the engine's per-allocation
        # savepoint (where it would degrade into an ALLOCATION_CONSUMPTION_FAILED audit
        # row instead of a 409); see the shop-floor twins.
        db.flush()
        apply_operation_completion_inventory_effects(
            db, work_order, operation, user_id=current_user.id, company_id=company_id, audit=audit
        )
        audit.log_status_change(
            resource_type="work_order_operation",
            resource_id=operation.id,
            resource_identifier=operation.operation_number,
            old_status=op_old_status,
            new_status=OperationStatus.COMPLETE.value,
            description=(
                f"Force-completed operation {operation.operation_number} via manual "
                f"completion of WO {work_order.work_order_number}"
            ),
        )
        # EVT-2: each force-completed operation gets an operation_completed event,
        # uniform with the op-level completion paths.
        emit_operation_completed_event(
            db,
            company_id=company_id,
            work_order=work_order,
            operation=operation,
            user_id=current_user.id,
            source_module="work_orders",
        )

    # Ensure the WO is COMPLETE even when it had no operations to force-complete
    # (the finalizer only runs per-operation). actual_start is stamped before the
    # COMPLETE flip to avoid an actual_end-without-actual_start row (DUP-2).
    if work_order.status not in (WorkOrderStatus.COMPLETE, WorkOrderStatus.CLOSED):
        if not work_order.actual_start:
            work_order.actual_start = now
        work_order.status = WorkOrderStatus.COMPLETE
        work_order.current_operation_id = None
    if not work_order.actual_end:
        work_order.actual_end = now

    # The finished quantity is the DECLARED one, SET for every work-order type (INT1). It
    # used to be max()'d with the header the loop above computed -- which on any routing
    # whose first stage had completed at the full order re-inflated the header, the FG
    # receipt and the backflush basis past a short the floor declared. The bounds above are
    # what keep this honest: it can never exceed what the floor recorded without a stated,
    # audited override, and a shortfall below the order is covered by reported scrap.
    work_order.quantity_complete = requested_complete
    # DUP-3: only overwrite recorded WO scrap when an explicit value was supplied;
    # a defaulted (omitted) call must not zero previously-booked scrap. The
    # scrap-reason guard above (422) has already ensured a positive scrap carries a
    # non-blank reason, so persist it alongside the quantity.
    if quantity_scrapped is not None:
        work_order.quantity_scrapped = quantity_scrapped
        work_order.scrap_reason = scrap_reason
        # Lean Phase 1: the structured code rides the same explicit-scrap-write
        # semantics -- an explicit scrap write replaces the categorization wholly.
        work_order.scrap_reason_code_id = scrap_code.id if scrap_code else None
    work_order.updated_at = now
    # The effective scrap actually persisted (the existing value when omitted), used
    # in the event + audit payloads so they reflect what was stored, not the raw arg.
    effective_quantity_scrapped = float(work_order.quantity_scrapped or 0)

    # Release capacity for every affected work center (DUP-4: this override used to
    # emit no scheduling refresh, stranding capacity for the still-open operations).
    if affected_work_centers:
        # PERF-5: commit=False joins this scheduling refresh into the handler's single
        # unit of work, so the WO/op state change is committed atomically with the
        # audit rows / FG receipt / cost rollup written below (the old default
        # commit=True committed the state change mid-handler -- a crash before the
        # terminal commit left a completed WO with no audit/inventory/cost).
        # commit=False skips the in-service WC cache invalidation, so we do it
        # ourselves after the terminal commit succeeds.
        SchedulingService(db, company_id).update_availability_rates(
            work_center_ids=[wc_id for wc_id in affected_work_centers if wc_id],
            horizon_days=90,
            commit=False,
        )
        work_centers_refreshed = True

    _emit_work_order_event(
        db,
        company_id=company_id,
        current_user=current_user,
        work_order=work_order,
        event_type="work_order_completed",
        payload={"quantity_complete": work_order.quantity_complete, "quantity_scrapped": effective_quantity_scrapped},
    )

    # Batch 6 / rank 9 (INV-1/INV-2/INV-3/TRACE-2/TRACE-3): this privileged override
    # drives the WO to COMPLETE, so it too must receive the finished good (always,
    # lot-only, idempotent) and backflush components (only if part.backflush_components).
    # Atomic with the manual completion below; a backflush shortage never fails it.
    apply_completion_inventory_effects(db, work_order, user_id=current_user.id, company_id=company_id, audit=audit)
    # Batch 7 / rank 10 (COST-1/COST-2/COST-4/COST-5): OPT-IN labor hour + actual-cost +
    # JobCost rollup for this privileged manual completion. No-op + pre-Batch-7 behavior
    # when the flag is OFF; atomic with the manual completion when ON.
    apply_completion_cost_rollup(db, work_order, company_id=company_id, user_id=current_user.id, audit=audit)

    # Audit this privileged manual completion (status change + the quantities it set)
    # on the tamper-evident chain. Logged BEFORE the terminal commit so the audit rows
    # commit atomically with the status change.
    db.flush()
    audit.log_status_change(
        resource_type="work_order",
        resource_id=work_order.id,
        resource_identifier=work_order.work_order_number,
        old_status=old_status,
        new_status=WorkOrderStatus.COMPLETE.value,
        description=f"Manually completed work order {work_order.work_order_number}",
        # S2: the evidence-override trail — which required process-sheet step records
        # this force-complete bypassed (count is exact; the itemized list is capped).
        extra_data={
            "steps_bypassed_count": steps_bypassed_count,
            "steps_bypassed": steps_bypassed_entries,
            "steps_bypassed_truncated": steps_bypassed_truncated,
        },
    )
    quantity_old_values = {
        "quantity_complete": old_quantity_complete,
        "quantity_scrapped": old_quantity_scrapped,
        "scrap_reason": old_scrap_reason,
        "scrap_reason_code_id": old_scrap_reason_code_id,
    }
    quantity_new_values = {
        "quantity_complete": work_order.quantity_complete,
        "quantity_scrapped": effective_quantity_scrapped,
        "scrap_reason": work_order.scrap_reason,
        "scrap_reason_code_id": work_order.scrap_reason_code_id,
    }
    # The evidence the quantity was decided on, on the same row (owner ruling 1).
    bounds_extra = {
        "deliverable": completion_bounds.deliverable,
        "physical_max": completion_bounds.physical_max,
        "floor_loss": completion_bounds.floor_loss,
        "binding_operation_id": completion_bounds.binding_operation_id,
        "binding_operation": (
            str(binding_operation.operation_number or binding_operation.sequence)
            if binding_operation is not None
            else None
        ),
        "override_reason": override_reason if asserted_above_recorded else None,
        "asserted_above_recorded": asserted_above_recorded,
    }
    quantity_description = f"Recorded completion quantities for work order {work_order.work_order_number}"
    if completing_short or asserted_above_recorded:
        # REQUIRED evidence (invariant 2, fail-closed): a work order completed short, or
        # above what the floor recorded, must not land without its quantity row -- the whole
        # unit rolls back and the caller gets a 503.
        with required_audit(db, COMPLETION_AUDIT_UNAVAILABLE):
            audit.log_required(
                action=audit.ACTIONS["UPDATE"],
                resource_type="work_order",
                resource_id=work_order.id,
                resource_identifier=work_order.work_order_number,
                description=quantity_description,
                old_values=quantity_old_values,
                new_values=quantity_new_values,
                extra_data={
                    "changes": {
                        key: {"old": quantity_old_values[key], "new": quantity_new_values[key]}
                        for key in quantity_new_values
                        if quantity_old_values[key] != quantity_new_values[key]
                    },
                    **bounds_extra,
                },
            )
    else:
        audit.log_update(
            resource_type="work_order",
            resource_id=work_order.id,
            resource_identifier=work_order.work_order_number,
            old_values=quantity_old_values,
            new_values=quantity_new_values,
            description=quantity_description,
            extra_data=bounds_extra,
        )

    # Batch 4 / rank 7 (QG-1/3, BLK-2): warn-and-record for the privileged manual
    # completion. This force-completes EVERY open operation, so gather the gates at
    # the WO grain (NCR / FAI / open-blocker -- evaluated once with operation=None)
    # PLUS one inspection_incomplete per operation that still requires inspection.
    # Each unsatisfied gate gets a tamper-evident audit row + warning event that
    # commit atomically below. Warn-only: completion already succeeded above.
    quality_exceptions: list[QualityException] = list(
        evaluate_completion_quality_exceptions(db, work_order, None, company_id)
    )
    for operation in operations:
        inspection_exc = evaluate_inspection_exception(operation)
        if inspection_exc is not None:
            quality_exceptions.append(inspection_exc)
    if quality_exceptions:
        record_completion_quality_exceptions(
            db,
            company_id=company_id,
            work_order=work_order,
            operation=None,
            exceptions=quality_exceptions,
            audit=audit,
            user=current_user,
            source="complete_work_order",
        )
    # Batch 7 data-quality signal (no_labor_recorded): the manual override force-completes
    # EVERY open operation, so a zero-labor op is especially likely here. Flag it on the
    # SAME quality_exceptions channel (its own audit row + warning event). Fires
    # REGARDLESS of the cost-rollup flag; warn-only, never blocks.
    quality_exceptions = quality_exceptions + evaluate_and_record_labor_data_quality(
        db,
        company_id=company_id,
        work_order=work_order,
        audit=audit,
        user=current_user,
        source="complete_work_order",
    )

    try:
        db.commit()
    except StaleDataError as exc:
        # A concurrent completer committed a newer version of this WO/op between our
        # locked read and this commit (version_id_col mismatch).
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="This work order was modified concurrently. Refresh and retry the completion.",
        ) from exc

    # PERF-5: the scheduling refresh ran with commit=False (joined to this handler's
    # unit of work), so it skipped the in-service WC cache invalidation -- do it here,
    # after the terminal commit succeeded, so the cache reflects the freed capacity.
    if work_centers_refreshed:
        invalidate_work_centers_cache()

    # EVT-3: enqueue the tenant-scoped notification + webhook dispatch in the ARQ
    # worker. After commit + best-effort so it can never fail the completion.
    enqueue_work_order_completion_signals(work_order_id=work_order.id, company_id=company_id, status="COMPLETE")

    safe_broadcast(
        broadcast_work_order_update,
        work_order.id,
        {
            "event": "work_order_completed",
            "status": work_order.status.value if hasattr(work_order.status, "value") else work_order.status,
        },
        company_id=company_id,
    )
    safe_broadcast(
        broadcast_dashboard_update,
        {
            "event": "work_order_completed",
            "work_order_id": work_order.id,
            "status": work_order.status.value if hasattr(work_order.status, "value") else work_order.status,
        },
        company_id=company_id,
    )
    return {
        "message": "Work order completed",
        # Warn-and-record (Batch 4 / rank 7): unsatisfied quality gates at completion.
        "quality_exceptions": [exc.as_dict() for exc in quality_exceptions],
        # S2: evidence-override summary — the required process-sheet step records this
        # force-complete bypassed, so the office UI can say "completed with N step
        # records bypassed". Backward-compatible: null when nothing was bypassed.
        "steps_bypassed": (
            {
                "count": steps_bypassed_count,
                "steps": steps_bypassed_entries,
                "truncated": steps_bypassed_truncated,
            }
            if steps_bypassed_count
            else None
        ),
    }


# Operation endpoints
@router.post("/{work_order_id}/operations", response_model=WorkOrderOperationResponse)
def add_operation(
    work_order_id: int,
    operation_in: WorkOrderOperationCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
):
    """Add an operation to a work order"""
    work_order = db.query(WorkOrder).filter(WorkOrder.id == work_order_id, WorkOrder.company_id == company_id).first()
    if not work_order:
        raise HTTPException(status_code=404, detail="Work order not found")

    # Laser nest WOs are DISPATCH POOLS whose every op is a nest, exempt from
    # predecessor gating and promoted all-at-once. A free-form op added here
    # would inherit that exemption/promotion without being nest-backed --
    # keep laser WOs managed exclusively by the nest import / manual-nest paths.
    if work_order.work_order_type == WorkOrderType.LASER_CUTTING.value:
        raise HTTPException(
            status_code=400,
            detail="Laser nest work orders manage operations through the nest package import "
            "and manual nest entry; add a nest instead of a free-form operation.",
        )

    # Tenancy: the work center is caller-supplied, so it must be proven to belong
    # to the active company before it is written onto the row.
    _assert_work_center_in_company(db, operation_in.work_center_id, company_id)

    operation = WorkOrderOperation(
        work_order_id=work_order_id, company_id=work_order.company_id, **operation_in.model_dump()
    )
    db.add(operation)
    db.commit()
    db.refresh(operation)
    return operation


def _differs_from_stored(requested: Any, stored: Any) -> bool:
    """Would writing ``requested`` change ``stored``? Numbers compare as floats (Decimal vs float)."""
    if isinstance(requested, (int, float, Decimal)) and isinstance(stored, (int, float, Decimal)):
        return abs(float(requested) - float(stored)) > QTY_EPSILON
    return requested != stored


@router.put("/operations/{operation_id}", response_model=WorkOrderOperationResponse)
def update_operation(
    operation_id: int,
    operation_in: WorkOrderOperationUpdate,
    request: Request,
    db: Session = Depends(get_db),
    # RBAC matrix (docs/RBAC_PERMISSIONS.md): Work Orders Edit = Admin/Manager/Supervisor.
    # This path edits operation fields incl. quantity_scrapped, so it must match the
    # sibling update_work_order's gate -- previously it was get_current_user only, letting
    # any authenticated user (incl. Operator/Viewer) edit/scrap an operation (compliance auditor).
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
):
    """Update an operation. The counts and status of a COMPLETE operation are its completion
    record and are refused 409 here; correct them with reduce_operation_production_office.

    Requires the operation's current ``version`` (stale -> **409**). Also returns
    **409** when setting ``status`` to COMPLETE on a not-yet-complete operation:
    completion has finalization and inventory side effects (operation-scoped
    tied-material consumption included) that this generic update would skip — use
    ``POST /work-orders/operations/{id}/complete`` instead.

    On a COMPLETE operation, changing ``quantity_complete``, ``quantity_scrapped``,
    ``scrap_reason`` or ``status`` is refused **409**: those are the completion record. Correct
    the good count with reduce_operation_production_office, which can reopen the operation.

    Setting ``status`` to IN_PROGRESS starts the operation, so it is refused **409**
    with the start gate's text when the work order is a draft ("release first":
    release_work_order), on hold (work_orders_resume_work_order) or
    complete / closed / cancelled; setting ``status`` to READY on a draft work order is
    refused **409** too (it would put unreleased work on the dispatch board). Both are
    raised before any field is written. Re-sending the operation's current status is
    not a transition and is accepted.
    """
    operation = (
        db.query(WorkOrderOperation)
        .filter(WorkOrderOperation.id == operation_id, WorkOrderOperation.company_id == company_id)
        .first()
    )
    if not operation:
        raise HTTPException(status_code=404, detail="Operation not found")

    # Capture old values for audit. This generic update writes domain data (including
    # quantity_scrapped/scrap_reason) via a blind setattr loop; previously it committed
    # with NO audit row at all (the compliance auditor flagged the gap). Snapshot the
    # full row up front, mirroring update_work_order, so log_update records old->new.
    audit = AuditService(db, current_user, request)
    old_values = {c.key: getattr(operation, c.key) for c in operation.__table__.columns}

    update_data = operation_in.model_dump(exclude_unset=True)

    # Optimistic locking (invariant 4): the client's version must MATCH the row --
    # and must never be written through the setattr loop below, or a stale client
    # could silently overwrite a concurrent edit AND arbitrarily move the
    # version_id_col counter that SQLAlchemy's StaleDataError protection keys on.
    client_version = update_data.pop("version")
    if client_version != operation.version:
        raise HTTPException(
            status_code=409,
            detail="Operation was modified by someone else. Refresh and try again.",
        )

    # Owner ruling 1 (INT3): a COMPLETE operation's counts and status ARE its completion
    # record -- the short it declared, what the next stage was credited and what consumed
    # material were all decided on them. This blind setattr used to rewrite them with no
    # bound at all, which could make a completed operation short after the fact. Refused
    # before any write; re-sending a value the row already holds is not a change. Counts
    # are corrected through reduce-production, which reopens the operation when it must.
    if operation.status == OperationStatus.COMPLETE:
        locked_changes = [
            field
            for field in ("quantity_complete", "quantity_scrapped", "scrap_reason", "status")
            if field in update_data and _differs_from_stored(update_data[field], getattr(operation, field))
        ]
        if locked_changes:
            raise HTTPException(status_code=409, detail=completed_counts_locked_message(operation))

    # Refuse status=COMPLETE via this generic PUT: it would be a FIFTH completion path
    # outside the four wired handlers -- no ``finalize_operation_completion``, no
    # operation-scoped tied-material consumption, no completion audit shape. Completion
    # goes through the completion endpoints (office/shop-floor complete-operation or the
    # kiosk clock-out), which run the full effect chain.
    new_op_status = update_data.get("status")
    if new_op_status == OperationStatus.COMPLETE and operation.status != OperationStatus.COMPLETE:
        raise HTTPException(
            status_code=409,
            detail=(
                "cannot mark an operation complete through this generic update: completion has "
                "finalization and inventory side effects that would be skipped. "
                f"Use POST /work-orders/operations/{operation.id}/complete instead."
            ),
        )

    # Owner ruling 2026-11-05 (ruling 2): this generic PUT must not be a way around the
    # start gate. Writing IN_PROGRESS here STARTS the operation, and the office complete
    # verb then accepts an IN_PROGRESS operation with no work-order gate (labor already
    # running on a held job must stay closable) -- so without this refusal the two calls
    # together would start AND complete work, tied-material consumption included, on a
    # job nobody released or a supervisor stopped. The shared helper decides it (no inline
    # twin); a terminal work order gets its existing "cannot start operation" text. READY
    # on a DRAFT is refused as well: Release is the authorization step and the record of
    # who put work on the floor, and resume-to-DRAFT demotes exactly those rows. Raised
    # before the dispatch lock and the first setattr, so a refusal writes nothing. A
    # re-sent CURRENT status is not a transition (the COMPLETE refusal above follows the
    # same rule), so an unrelated edit that echoes the status still lands.
    if new_op_status is not None and new_op_status != operation.status:
        status_blocked_reason: Optional[str] = None
        if new_op_status == OperationStatus.IN_PROGRESS:
            status_blocked_reason = work_order_start_blocker(operation.work_order)
        elif new_op_status == OperationStatus.READY and operation.work_order is not None:
            if operation.work_order.status == WorkOrderStatus.DRAFT:
                status_blocked_reason = MSG_WORK_ORDER_NOT_RELEASED
        if status_blocked_reason:
            raise HTTPException(status_code=409, detail=status_blocked_reason)

    # Work-center reassignment (planner action, e.g. re-dispatching a laser nest to
    # another laser -- but legitimate for any operation between compatible work
    # centers). Validated BEFORE the blind setattr loop so a bad target mutates
    # nothing. An explicit null is ignored: work_center_id is NOT NULL on the model.
    new_work_center_id = update_data.pop("work_center_id", None)
    dispatch_before = None
    dispatch_change_kind: dict = {}
    if new_work_center_id is not None and new_work_center_id != operation.work_center_id:
        # A move rewrites two machines' queue order, so it serializes with every
        # other dispatch-order writer (the plan verbs, the column rewrite) BEFORE
        # any read the notice snapshot compares against.
        dispatch_service.acquire_dispatch_order_lock(db, company_id)
        # Active time sessions are bound to the old work center's queue, so the op
        # must be idle before it moves: refuse while IN_PROGRESS or while ANY open
        # time session exists (belt and braces -- an open entry can outlive an
        # IN_PROGRESS status through manual status edits). 409: state conflict.
        open_session = (
            db.query(TimeEntry.id)
            .filter(
                TimeEntry.operation_id == operation.id,
                TimeEntry.company_id == company_id,
                TimeEntry.clock_out.is_(None),
            )
            .first()
        )
        if operation.status == OperationStatus.IN_PROGRESS or open_session is not None:
            raise HTTPException(
                status_code=409,
                detail="Clock out before moving the operation to another work center",
            )
        # A finished run's labor history belongs to the work center it actually
        # ran on -- rewriting it after completion would falsify utilization and
        # traceability records (records-integrity, not just hygiene).
        if operation.status == OperationStatus.COMPLETE:
            raise HTTPException(
                status_code=409,
                detail="Completed operations cannot be moved to another work center",
            )
        new_work_center = (
            db.query(WorkCenter)
            .filter(
                WorkCenter.id == new_work_center_id,
                WorkCenter.company_id == company_id,
                WorkCenter.is_active == True,  # noqa: E712
            )
            .first()
        )
        if not new_work_center:
            raise HTTPException(status_code=404, detail="Work center not found")
        old_work_center_id = operation.work_center_id
        # Notice "before" snapshot of both machines, taken BEFORE anything mutates.
        dispatch_before = dispatch_service.snapshot_queue_orders(
            db, company_id, [old_work_center_id, new_work_center.id]
        )
        dispatch_change_kind = {
            old_work_center_id: DispatchChangeKind.MOVED_OUT,
            new_work_center.id: DispatchChangeKind.MOVED_IN,
        }
        # The manual dispatch rank is scoped to the work center it was ranked IN,
        # so it is meaningless at the destination: the shared helper clears it and
        # the op lands unranked at the tail of the new column (a manager re-ranks
        # it there) -- under the work-order plan, at the bottom of its own work
        # order's block there. Called BEFORE the reassignment -- it compares against
        # the operation's current work center. run_order is in the full-row audit
        # snapshot above, so the clear shows up in this endpoint's old->new diff.
        dispatch_service.clear_run_order_on_move(operation, new_work_center.id)
        operation.work_center_id = new_work_center.id
        # Keep the derived grouping in step with the new work center (mirrors how
        # ops are grouped at creation) so queue/grouping views stay consistent.
        # Both fields are in the full-row audit snapshot, so the old->new diff
        # records the move on the tamper-evident chain.
        operation.operation_group = get_work_center_group(new_work_center)
        # Reserved load followed the op to the new work center: refresh BOTH
        # centers' persisted availability (matches the scheduling endpoint's
        # post-move refresh) so capacity views don't show stale load. Runs after
        # commit below via the flag -- the service commits its own updates.
        availability_refresh_wc_ids = {old_work_center_id, new_work_center.id}
    else:
        availability_refresh_wc_ids = None

    for field, value in update_data.items():
        setattr(operation, field, value)
    sync_laser_nest_from_operation(operation)

    # Audit log for update. Logged BEFORE the terminal commit so the audit row commits
    # atomically with the change (AuditService.log() only flushes; the request session
    # never commits on teardown).
    db.flush()
    audit.log_update(
        resource_type="work_order_operation",
        resource_id=operation.id,
        resource_identifier=operation.operation_number,
        old_values=old_values,
        new_values=operation,
    )
    dispatch_changed_wc_ids: list = []
    if dispatch_before is not None:
        # Notice seam: AFTER the flush above (so the after-snapshot sees the move)
        # and never with expire_all (it would discard nothing here, but the seam is
        # shared with paths that still hold un-flushed ORM writes).
        dispatch_changed_wc_ids = dispatch_service.record_dispatch_change(
            db, company_id, current_user, dispatch_before, dispatch_change_kind
        )

    db.commit()
    db.refresh(operation)
    dispatch_service.broadcast_dispatch_changes(company_id, dispatch_changed_wc_ids)

    if availability_refresh_wc_ids:
        SchedulingService(db, company_id).update_availability_rates(
            work_center_ids=list(availability_refresh_wc_ids), horizon_days=90
        )

    return operation


@router.post("/operations/{operation_id}/reduce-production")
def reduce_operation_production_office(
    operation_id: int,
    reduction_data: ProductionReductionRequest,
    db: Session = Depends(get_db),
    # RBAC matrix (docs/RBAC_PERMISSIONS.md): same gate as update_operation -- this
    # verb corrects recorded production (other operators' labor records included),
    # which is a Work Orders Edit power, not operator self-service.
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """
    Supervisor/office over-count correction -- the role-gated twin of the operator's
    ``POST /shop-floor/operations/{id}/reduce-production``. A correction that would leave a
    COMPLETE operation short is refused 409 unless reopen_operation is sent (the operation
    returns to in progress).

    Scope: walks the delta down ALL UNAPPROVED TimeEntry evidence on the operation
    (any operator), open entries first then newest-first. APPROVED entries are the
    immutability boundary (G5-A) and are excluded from the allowance -- unapprove
    them first via ``POST /shop-floor/time-entries/{id}/unapprove`` (the audited
    segregation-of-duties front door), then reduce. No open clock-in is required --
    the supervisor is correcting from the office, not working the operation.

    Scope difference from the operator's twin: a **COMPLETE operation is correctable
    here** (``allow_completed_operation=True``). It used to 409 on both verbs, on the
    rationale that a completed operation's downstream inventory / cost / FG effects had
    fired and could not be walked back -- while telling the operator to "ask a
    supervisor" whose own endpoint hit the identical refusal. PR 3's reasoned RETURN
    verb is that walk-back: tied material a completed operation consumed comes back
    through ``POST /work-orders/{id}/material-allocations/{alloc}/return``, and lowering
    the completed operation's count HERE is exactly what opens the bounded
    ``correct_over_consumption`` allowance that return is measured against. Order
    matters: reduce first (the count is the record), then return the material the lower
    count no longer accounts for. A TERMINAL work order is still 409 on both verbs.

    Everything else is identical to the shop-floor twin (one shared core, see
    ``production_reduction_service``): terminal-WO 409 re-checked under the op->WO row
    locks in the completion paths' order, tenant-scoped 404, required correction
    ``reason``, per-entry audit trail on the tamper-evident chain, best-effort
    OperationalEvent, optimistic-lock 409, and the RECOMPUTED work-order rollup (max
    over non-component siblings -- or, on a laser dispatch-pool WO, the pooled SUM of
    per-nest progress -- only ever lowered). Scrap fields are never touched.

    Owner ruling 1 (2026-11-05): a COMPLETE operation cannot be short. A correction that
    keeps it accounted (good + scrap still cover every unit that reached it) leaves it
    COMPLETE with a truthful count. One that would leave it short is refused **409** unless
    the request sends ``reopen_operation: true``: the operation then returns to
    **IN_PROGRESS** (``actual_end`` / ``completed_by`` / ``quantity_short_declared`` cleared),
    the un-worked READY operations it now blocks return to PENDING, and the reopen is
    REQUIRED audit evidence (**503**, nothing saved, when it cannot be written). The reopen
    is refused **409** while a later operation of the same chain is COMPLETE -- correct that
    one first. Consumed material is never auto-returned; use the return verb.
    """
    load_operation_for_reduction_or_http(db, operation_id, company_id, allow_completed_operation=True)

    # Same loader-channel guard as the labor writes: 'import' is reserved for the
    # bulk-migration loaders and may never be claimed by an interactive correction.
    # (Kiosk-scoped tokens are path-fenced away from /work-orders, so no kiosk forcing
    # is needed here.) Resolved before any mutation.
    if reduction_data.source == TimeEntrySource.IMPORT:
        raise HTTPException(
            status_code=422,
            detail="source 'import' cannot be set on an interactive correction; "
            "it is reserved for the bulk-import loaders",
        )
    recorded_source = reduction_data.source.value if reduction_data.source else None

    # Eligible evidence: ALL unapproved entries on the operation (any operator), open
    # first then newest-first. Approved rows are excluded -- unapprove first.
    entries = eligible_reduction_entries(db, operation_id=operation_id, company_id=company_id, user_id=None)
    allowance = sum(float(e.quantity_produced or 0) for e in entries)

    if reduction_data.quantity_delta > allowance:
        approved_qty = approved_produced_total(db, operation_id=operation_id, company_id=company_id, user_id=None)
        if approved_qty > 0:
            detail = (
                f"Only {allowance:g} piece(s) on this operation are unapproved and correctable; "
                f"{approved_qty:g} piece(s) are on approved labor -- unapprove it first."
            )
        else:
            detail = (
                f"Only {allowance:g} piece(s) are recorded on this operation's time entries; "
                "the correction cannot exceed the recorded evidence."
            )
        raise HTTPException(status_code=400, detail=detail)

    # Locks, TOCTOU re-check, walk, recomputed WO rollup, audit, event, commit/409 --
    # shared with the shop-floor twin. notes_entry=None: a supervisor's note belongs on
    # the audit row (and the event payload), not on another operator's labor record.
    outcome = perform_production_reduction(
        db,
        operation_id=operation_id,
        company_id=company_id,
        actor=current_user,
        audit=audit,
        entries=entries,
        delta=reduction_data.quantity_delta,
        reason=reduction_data.reason,
        notes=reduction_data.notes,
        recorded_source=recorded_source,
        notes_entry=None,
        event_source_module="work_orders",
        path="office",
        # Must match the pre-lock call above: the same gate runs again under the row
        # locks, so relaxing only one of the two would refuse under the lock instead.
        allow_completed_operation=True,
        reopen_operation=reduction_data.reopen_operation,
    )
    operation = outcome.operation
    work_order = outcome.work_order

    if operation.work_center_id:
        safe_broadcast(
            broadcast_shop_floor_update,
            operation.work_center_id,
            {
                "event": "operation_production_reduced",
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
            "event": "operation_production_reduced",
            "operation_id": operation.id,
        },
        company_id=company_id,
    )
    safe_broadcast(
        broadcast_dashboard_update,
        {
            "event": "operation_production_reduced",
            "work_order_id": work_order.id,
            "operation_id": operation.id,
        },
        company_id=company_id,
    )

    return {
        "message": "Production quantity corrected",
        "operation": {
            "id": operation.id,
            "status": operation.status.value,
            "quantity_complete": operation.quantity_complete,
            "quantity_scrapped": operation.quantity_scrapped,
            "quantity_ordered": outcome.target_qty,
        },
        "work_order": {
            "id": work_order.id,
            "quantity_complete": work_order.quantity_complete,
        },
        # Per-entry paper trail of the walk (whose evidence was lowered, by how much).
        "reduced_time_entries": [s.as_dict() for s in outcome.reduction.time_entry_reductions],
    }


@router.post("/operations/{operation_id}/start")
def start_operation(
    operation_id: int,
    request: Request,
    db: Session = Depends(get_db),
    # Office verb, and it was open to ANY authenticated tenant user -- VIEWER
    # included. That was always wrong (starting an operation stamps actual_start /
    # started_by, moves the work order to IN_PROGRESS and writes the tamper-evident
    # chain), but it was masked while the predecessor gate refused nearly every
    # operation a read-only user could reach. With same-work-center operations now
    # promoting together, the ops a Viewer can reach are exactly the ones the gate
    # no longer refuses, so the hole became reachable.
    #
    # The gate MATCHES its office twin ``complete_operation`` -- being able to start
    # an operation but not complete it (or vice versa) is incoherent.
    #
    # Operators are unaffected and deliberately NOT added: they start work through
    # PUT /shop-floor/operations/{id}/start and the kiosk, which stay operator-open
    # (docs/RBAC_PERMISSIONS.md).
    current_user: User = Depends(
        require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR, UserRole.QUALITY])
    ),
    company_id: int = Depends(get_current_company_id),
):
    """Start an operation (office verb).

    Stamps ``actual_start`` / ``started_by`` and lifts a RELEASED work order to
    IN_PROGRESS. Gated to **Admin / Manager / Supervisor / Quality**, matching the
    office twin ``POST /work-orders/operations/{id}/complete``; operators start work
    through ``PUT /shop-floor/operations/{id}/start`` and the kiosk, which stay open to
    any authenticated user.

    Refused before any write, in the shop-floor twin's order: unknown operation (404);
    already complete / already in progress (400 -- it never re-opens or re-stamps one);
    work order missing or soft-deleted (404); **409** when the work order is a draft
    ("release first": release_work_order), on hold (work_orders_resume_work_order), or
    complete / closed / cancelled; **409** when the operation itself is on hold --
    starting does not lift a hold.

    Predecessor gate: refused **400** ("Previous operations must be completed first")
    while a lower-sequence operation of the same work order is incomplete. Operations
    sharing the candidate's **work center** do not block it -- the same rule clock-in
    enforces -- except when such a predecessor is **ON_HOLD**, which blocks from any work
    center. Laser dispatch-pool work orders are exempt from the gate entirely.
    """
    operation = (
        db.query(WorkOrderOperation)
        .options(joinedload(WorkOrderOperation.work_order).joinedload(WorkOrder.part))
        .filter(WorkOrderOperation.id == operation_id, WorkOrderOperation.company_id == company_id)
        .first()
    )
    if not operation:
        raise HTTPException(status_code=404, detail="Operation not found")

    # Same order and text as PUT /shop-floor/operations/{id}/start. Without these two
    # the write below unconditionally set IN_PROGRESS and re-stamped actual_start /
    # started_by -- re-opening a COMPLETE operation and erasing who really started a
    # running one, with an audit row that recorded only the status.
    if operation.status == OperationStatus.COMPLETE:
        raise HTTPException(status_code=400, detail=MSG_ALREADY_COMPLETE)
    if operation.status == OperationStatus.IN_PROGRESS:
        raise HTTPException(status_code=400, detail=MSG_ALREADY_IN_PROGRESS)

    work_order = operation.work_order
    if not work_order or work_order.is_deleted:
        raise HTTPException(status_code=404, detail="Work order not found")

    # The work-order half of the start gate (terminal / draft / on hold) -- the shared
    # helper, 409. This verb had NO work-order status check: a DRAFT work order's
    # operation could be started while the work order stayed DRAFT.
    start_blocked_reason = work_order_start_blocker(work_order)
    if start_blocked_reason:
        raise HTTPException(status_code=409, detail=start_blocked_reason)

    # Starting is not how an operation's hold is lifted (Resume / Clear Hold is).
    if operation.status == OperationStatus.ON_HOLD:
        raise HTTPException(status_code=409, detail=MSG_OPERATION_ON_HOLD_NO_START)

    # One predicate, shared with the shop floor, so this gate cannot drift again.
    # It used to be an inline copy passing allow_same_work_center=False while the
    # shop-floor twin passed True, which meant the office refused an operation the
    # floor would happily start -- invisible while both surfaces only ever showed
    # one READY operation, and immediately contradictory once same-work-center
    # operations began promoting together. (The laser dispatch-pool exemption
    # lives inside the predicate too.)
    if operation_blocked_by_predecessors(db, operation):
        raise HTTPException(status_code=400, detail=MSG_PREDECESSORS_INCOMPLETE)

    old_operation_status = operation.status.value if operation.status else None
    old_work_order_status = work_order.status.value if work_order.status else None

    operation.status = OperationStatus.IN_PROGRESS
    operation.actual_start = datetime.utcnow()
    operation.started_by = current_user.id

    # Also update work order status if needed
    work_order_started = False
    if work_order.status == WorkOrderStatus.RELEASED:
        work_order.status = WorkOrderStatus.IN_PROGRESS
        work_order.actual_start = datetime.utcnow()
        work_order_started = True

    # Audit the status transitions on the tamper-evident chain. Logged BEFORE the
    # terminal commit so the audit rows commit atomically with the status change.
    db.flush()
    audit = AuditService(db, current_user, request)
    audit.log_status_change(
        resource_type="work_order_operation",
        resource_id=operation.id,
        resource_identifier=operation.operation_number,
        old_status=old_operation_status,
        new_status=OperationStatus.IN_PROGRESS.value,
        description=(f"Started operation {operation.operation_number} on WO {work_order.work_order_number}"),
    )
    if work_order_started:
        audit.log_status_change(
            resource_type="work_order",
            resource_id=work_order.id,
            resource_identifier=work_order.work_order_number,
            old_status=old_work_order_status,
            new_status=WorkOrderStatus.IN_PROGRESS.value,
        )

    try:
        db.commit()
    except StaleDataError as exc:
        # A concurrent writer bumped the operation/WO version between read and
        # commit (version_id_col mismatch). Surface a clean 409, not a 500.
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="This operation was modified concurrently. Refresh and retry.",
        ) from exc
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
    return {"message": "Operation started"}


@router.post("/operations/{operation_id}/complete")
def complete_operation(
    operation_id: int,
    request: Request,
    quantity_complete: float,
    quantity_scrapped: Optional[float] = None,
    scrap_reason: Optional[str] = None,
    scrap_reason_code_id: Optional[int] = None,
    quantity_override_reason: Optional[str] = None,
    acknowledge_open_entries: bool = False,
    db: Session = Depends(get_db),
    # Office verb. Previously open to ANY authenticated tenant user, VIEWER and
    # SHIPPING included -- already wrong, and load-bearing once operation
    # completion started moving stock: a Viewer could decrement inventory and
    # write ledger + hash-chain rows from a page they were only meant to read.
    #
    # The gate MATCHES ``complete_work_order`` (its larger sibling, which
    # completes every operation on the work order) rather than
    # ``reduce-production``. QUALITY belongs here: excluding it would let a
    # Quality user complete a whole work order but not one of its operations,
    # which is incoherent. reduce-production is stricter for a reason that does
    # not apply here -- it rewrites other operators' recorded labor.
    #
    # Operators are unaffected: they complete work through
    # /shop-floor/operations/{id}/complete and the kiosk (docs/RBAC_PERMISSIONS.md).
    current_user: User = Depends(
        require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR, UserRole.QUALITY])
    ),
    company_id: int = Depends(get_current_company_id),
):
    """Record an operation's totals from the office; it closes only when good + scrap cover
    every unit that reached it, otherwise it saves progress and stays open. quantity_complete
    is the absolute good total and quantity_scrapped an absolute scrap total that never lowers
    (a rise needs scrap_reason or scrap_reason_code_id); good above the floor's labor count
    needs quantity_override_reason; an API token may not close short (403).

    Owner ruling 1 (2026-11-05): a short count is reported as scrap with a reason, or the operation
    stays open -- never closed at the counted quantity. The decision is
    ``work_order_state_service.OperationAccounting.is_accounted`` (good + scrap cover every unit
    that reached the operation, upstream declared shorts excluded); a close stamps the short it
    declared on ``quantity_short_declared``.

    Refused before any write, in order: non-finite quantity **400**; terminal work order **409**;
    predecessors incomplete **400** (same-work-center predecessors don't block on a pooled work
    order unless **ON_HOLD**; laser pools are exempt); bad ``quantity_complete`` / missing target
    **400**; already complete **400**; operation ON_HOLD **409** (other non-startable status
    **400**); a READY operation under a **draft** / **on-hold** work order **409** (completing it
    starts it); negative scrap **400**; unknown / inactive scrap code **404** / **422**; a scrap
    rise without a reason **422**; good above what can reach the operation after upstream short
    closes **400**; good above the floor's labor evidence without ``quantity_override_reason``
    **422** (an operation nobody clocked into needs no reason -- desk cleanup stays reason-free,
    and there is deliberately no labor gate here, unlike the shop-floor twin). On a SHORT close
    only: an API token **403**; anyone still clocked in without ``acknowledge_open_entries`` **409**
    naming them; a serialized work order with missing per-serial step records **409**. Then
    required process-sheet steps **409** ``STEPS_INCOMPLETE``.

    Nothing to change (same good, no scrap rise, still short) is a **200** "No change" that writes
    nothing -- not even the auto-start. A short close with ``acknowledge_open_entries`` clocks the
    named operators out with their quantities untouched (pieces they never reported are not
    counted) and lists them in ``closed_time_entries``; a full-count close never auto-closes
    anyone. Every call writes one quantity audit row; it is REQUIRED evidence (``log_required``,
    **503** with nothing saved when it cannot be written) whenever scrap rose, the close was short
    or good exceeded the floor's evidence.
    """
    # Reject non-finite quantities (NaN/Inf) up front: a plain float query param accepts
    # "nan"/"inf", and NaN slips past every `> 0`/`< 0` guard below (including the scrap-
    # reason guard), which would persist a reasonless NaN scrap on Postgres (compliance
    # auditor). Mirrors the shop-floor /production isnan/isinf guard.
    if (quantity_complete is not None and not math.isfinite(quantity_complete)) or (
        quantity_scrapped is not None and not math.isfinite(quantity_scrapped)
    ):
        raise HTTPException(status_code=400, detail="Quantity must be a valid number")
    operation = (
        db.query(WorkOrderOperation)
        .options(joinedload(WorkOrderOperation.work_order).joinedload(WorkOrder.part))
        .filter(WorkOrderOperation.id == operation_id, WorkOrderOperation.company_id == company_id)
        .first()
    )
    if not operation:
        raise HTTPException(status_code=404, detail="Operation not found")

    # SFI-1: serialize concurrent completers on the office/admin op-complete path
    # the same way the shop_floor twin does. Re-fetch the operation and its parent
    # work order under SELECT ... FOR UPDATE (consistent lock order: OPERATION
    # first, then WORK ORDER) so the over-completion guard AND the remaining-ops
    # "WO COMPLETE" decision below run against the freshest committed rows. Both
    # re-fetches stay scoped to the active company.
    operation = (
        db.query(WorkOrderOperation)
        .filter(WorkOrderOperation.id == operation_id, WorkOrderOperation.company_id == company_id)
        .with_for_update()
        .first()
    )
    if not operation:
        raise HTTPException(status_code=404, detail="Operation not found")

    # Re-fetch the parent WO under a row lock, scoped to the active company and
    # excluding soft-deleted WOs -- matching the shop_floor complete_operation
    # twin (the safer default: never complete operations against a deleted WO).
    work_order = None
    if operation.work_order_id is not None:
        work_order = (
            db.query(WorkOrder)
            .options(selectinload(WorkOrder.operations).selectinload(WorkOrderOperation.laser_nest))
            .filter(
                WorkOrder.id == operation.work_order_id,
                WorkOrder.company_id == company_id,
                WorkOrder.is_deleted == False,  # noqa: E712
            )
            .with_for_update()
            .first()
        )
    # PR 4 (ledger, re-audit note b): a soft-deleted/missing parent WO is a 404 here,
    # exactly like the shop-floor twin — previously this path `work_order and ...`-
    # guarded every gate and would complete an orphaned operation against a deleted WO
    # with no gates evaluated.
    if not work_order:
        raise HTTPException(status_code=404, detail="Work order not found for this operation")

    # G6-A: refuse to complete an operation against a TERMINAL parent WO
    # (CANCELLED/CLOSED/COMPLETE) before any mutation -- mirrors the ON_HOLD 409 the
    # op-complete handlers already enforce. Without this, finalizing the last op of a
    # CANCELLED WO would resurrect it to COMPLETE via the shared finalizer and re-fire
    # FG receipt/backflush/cost rollup plus a COMPLETE audit row.
    if work_order.status in TERMINAL_WO_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"cannot complete operation: work order is {work_order.status.value}",
        )

    # Same shared predicate as the office start verb and the shop floor -- see the
    # note there. No `work_order` guard: the 404 above already returned for a missing
    # one, so a truthiness check here would be dead code implying a reachable case.
    if operation_blocked_by_predecessors(db, operation):
        raise HTTPException(status_code=400, detail=MSG_PREDECESSORS_INCOMPLETE)

    target_qty = operation_target_quantity(operation, work_order)
    try:
        validate_operation_quantity(quantity_complete, target_qty)
    except WorkOrderStateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Re-checked under the lock so a concurrent completer that already flipped
    # this op to COMPLETE is rejected here rather than losing its update.
    if operation.status == OperationStatus.COMPLETE:
        raise HTTPException(status_code=400, detail="Operation is already complete")

    # QG-5 / BLK-1: refuse to complete an ON_HOLD (or otherwise non-startable)
    # operation, identical to the shop_floor twin. Previously this path force-set
    # ANY non-IN_PROGRESS status (incl. ON_HOLD) to IN_PROGRESS and completed it,
    # silently lifting a quality/material hold and leaving its blocker open.
    if operation.status not in (OperationStatus.IN_PROGRESS, OperationStatus.READY):
        if operation.status == OperationStatus.ON_HOLD:
            raise HTTPException(status_code=409, detail="Operation is on hold and cannot be completed")
        raise HTTPException(status_code=400, detail=f"Cannot complete operation with status: {operation.status.value}")

    # Completing a READY operation STARTS it (the auto-start below), so it passes the
    # start gate first -- identical to the shop-floor twin: a READY operation under a
    # DRAFT or ON_HOLD work order is refused 409 with the start verbs' text, before the
    # quantity and accounting checks below, any write and any inventory effect (this path
    # used to book stock on a held job). A "No change" or progress-only submit on such an
    # operation is refused here too: a state conflict outranks a no-op, and the partial
    # branch auto-starts. An IN_PROGRESS operation is NOT gated. Terminal was refused above.
    if operation.status != OperationStatus.IN_PROGRESS:
        start_blocked_reason = work_order_start_blocker(work_order)
        if start_blocked_reason:
            raise HTTPException(status_code=409, detail=start_blocked_reason)

    # ---- Input checks (owner ruling 1). Every one of them runs before the first write;
    # this path used to set quantity_complete and only then refuse a bad scrap value. ----
    if quantity_scrapped is not None and quantity_scrapped < 0:
        raise HTTPException(status_code=400, detail="quantity_scrapped cannot be negative")
    # Lean Phase 1: resolve the structured code BEFORE any mutation (404 unknown /
    # cross-tenant, 422 inactive). None passes through untouched.
    scrap_code = resolve_scrap_reason_code_or_http(db, company_id, scrap_reason_code_id)
    # The scrap TOTAL never lowers (it used to be an overwrite that could zero scrap the
    # floor had recorded, and null its reason). A value below what is recorded is a no-op
    # for scrap; a rise is the declared delta and must carry a reason or a code.
    stored_scrap = float(operation.quantity_scrapped or 0)
    new_scrap = max(stored_scrap, float(quantity_scrapped)) if quantity_scrapped is not None else stored_scrap
    scrap_delta = new_scrap - stored_scrap
    has_reason_text = bool(scrap_reason and scrap_reason.strip())
    if scrap_delta > QTY_EPSILON and not (has_reason_text or scrap_code is not None):
        raise HTTPException(status_code=422, detail=MSG_SCRAP_REASON_OR_CODE_REQUIRED)

    # PR 4 (ledger): the good total is resolved ONCE under the lock and reused for the
    # accounting, the steps gate and the store below -- gating and storing can never
    # diverge. ABSOLUTE verb (DUP-3 / SFI-5): max(existing, requested, evidence), capped
    # at target, so the office can never lower an operation below durable evidence.
    stored_good = float(operation.quantity_complete or 0)
    resolved_quantity = resolve_absolute_operation_quantity(db, operation, quantity_complete, target_qty)
    acct = operation_accounting(work_order, operation, good=resolved_quantity, scrapped=new_scrap)
    # Good above what can reach the operation once upstream closes declared a short.
    if resolved_quantity > acct.ceiling + QTY_EPSILON:
        raise HTTPException(
            status_code=400,
            detail=ceiling_exceeded_message(operation, resolved_quantity, acct),
        )
    # INT7: recording more good than the floor counted is a desk assertion -- allowed, but
    # only with a stated reason, written as REQUIRED audit evidence below. An operation
    # nobody ever clocked into has no floor count to exceed: desk cleanup of never-clocked
    # work stays reason-free (the owner's floor-requires-labor / office-exempt asymmetry).
    recorded_good = resolve_absolute_operation_quantity(db, operation, 0.0, target_qty)
    override_reason = (quantity_override_reason or "").strip() or None
    asserted_above_recorded = (
        resolved_quantity > recorded_good + QTY_EPSILON
        and operation.id is not None
        and operation_has_labor_evidence(db, operation.id, company_id)
    )
    if asserted_above_recorded and override_reason is None:
        raise HTTPException(status_code=422, detail=override_reason_required_message(operation, recorded_good))

    # ---- The accounting decision. ----
    is_fully_complete = acct.is_accounted
    if not is_fully_complete and abs(resolved_quantity - stored_good) <= QTY_EPSILON and scrap_delta <= QTY_EPSILON:
        # SF9: nothing would change and the operation stays open. Write NOTHING -- not
        # the auto-start, not an audit row -- so an unchanged desk submit can never
        # lift a READY operation (and its work order) onto the floor.
        return {
            "message": office_no_change_message(operation),
            "is_fully_complete": False,
            "operation": _office_completion_operation_payload(operation, acct),
            "closed_time_entries": [],
            "quality_exceptions": [],
        }
    closing_short = is_fully_complete and acct.closing_short > QTY_EPSILON

    open_entries: list[TimeEntry] = []
    missing_steps: Optional[list] = None
    if closing_short:
        # Ruling 1 / this PR's own credential rule: a standing API token may close a full
        # count, but declaring where missing pieces went is a judgment a named person
        # makes. (``is_api_token_credential`` -- a crew-station badge token is a person.)
        if is_api_token_credential(current_user):
            raise HTTPException(status_code=403, detail=API_TOKEN_SHORT_CLOSE)
        # SF4: a short close must not land while anyone is still clocked in -- their
        # unreported pieces would silently become the shortfall. The office verb has no
        # caller entry of its own, so EVERY open entry counts. With the acknowledgement the
        # close clocks them out below, quantities untouched.
        open_entries = (
            db.query(TimeEntry)
            .options(joinedload(TimeEntry.user))
            .filter(
                TimeEntry.operation_id == operation.id,
                TimeEntry.company_id == company_id,
                TimeEntry.clock_out.is_(None),
            )
            .order_by(TimeEntry.clock_in.asc())
            .all()
        )
        if open_entries and not acknowledge_open_entries:
            names = [
                operator_display_name(entry.user.first_name, entry.user.last_name) if entry.user else None
                for entry in open_entries
            ]
            raise HTTPException(
                status_code=409,
                detail=others_clocked_in_message(
                    operation, [name or "Another operator" for name in names], office=True
                ),
            )
        # INT6: scrapped serials can never satisfy the per-serial step gate, and there is
        # no serial-level scrap record yet -- refuse explicitly rather than with the
        # generic STEPS_INCOMPLETE. Force-complete stays the supervisor path.
        if process_sheet_service.parse_work_order_serials(work_order):
            missing_steps = process_sheet_service.missing_required_steps(db, company_id, operation, work_order)
            if missing_steps:
                missing_serials: list[str] = []
                for step in missing_steps:
                    for serial in step.get("serials") or []:
                        if serial not in missing_serials:
                            missing_serials.append(serial)
                raise HTTPException(
                    status_code=409,
                    detail=serialized_short_message(operation, acct.closing_short, missing_serials),
                )

    # Process-sheet completion gate (PR 3): IDENTICAL to the shop-floor twin — an
    # ungated office/admin completion path would let anyone bypass the objective-
    # evidence requirement. When THIS request CLOSES the operation, every required
    # (non-INSTRUCTION) snapshot step must carry a live (non-superseded) conforming
    # record — per serial on a serialized WO. Evaluated under the same row lock;
    # progress updates and zero-step operations are unaffected.
    if is_fully_complete:
        if missing_steps is None:
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

    # ---- Every refusal is above this line; the mutation starts here. ----
    old_operation_status = operation.status.value if operation.status else None
    old_work_order_status = work_order.status.value if work_order and work_order.status else None
    old_quantity_values = {
        "quantity_complete": stored_good,
        "quantity_scrapped": stored_scrap,
        "scrap_reason": operation.scrap_reason,
        "scrap_reason_code_id": operation.scrap_reason_code_id,
        "quantity_short_declared": operation.quantity_short_declared,
    }
    work_order_completed = False
    # PERF-5: tracks whether the scheduling refresh ran (it runs with commit=False,
    # so the WC cache must be invalidated by us after the terminal commit succeeds).
    work_centers_refreshed = False

    # Auto-start a READY op (consistent with the shop_floor twin). ON_HOLD is no
    # longer reachable here -- it was refused above. This IS a start, which is why the
    # work-order start gate ran above for a READY operation.
    if operation.status != OperationStatus.IN_PROGRESS:
        operation.status = OperationStatus.IN_PROGRESS
        if not operation.actual_start:
            operation.actual_start = datetime.utcnow()
            operation.started_by = current_user.id

    operation.quantity_complete = resolved_quantity
    if scrap_delta > QTY_EPSILON:
        operation.quantity_scrapped = new_scrap
        # Never-clear semantics, like every other scrap writer: a reason or code describes
        # the scrap this call ADDED and is never nulled by one that carries none.
        if has_reason_text:
            operation.scrap_reason = scrap_reason
        if scrap_code is not None:
            operation.scrap_reason_code_id = scrap_code.id
    operation.updated_at = datetime.utcnow()
    if is_fully_complete:
        record_short_declaration(operation, acct)
    sync_laser_nest_from_operation(operation)

    if is_fully_complete:
        operation.status = OperationStatus.COMPLETE
        operation.actual_end = datetime.utcnow()
        operation.completed_by = current_user.id

    # work_order is the row already locked above; don't re-derive it from the
    # (unlocked, unscoped) relationship. The shared finalizer owns the rollup
    # (DUP-5): remaining-ops decision, COMPLETE-vs-release, actual_start/actual_end
    # stamping, qty sync, current_operation_id; it returns the WCs to refresh.
    affected_work_centers = {operation.work_center_id}
    if is_fully_complete:
        affected_work_centers |= finalize_operation_completion(db, work_order, operation)
        work_order_completed = work_order.status == WorkOrderStatus.COMPLETE
    else:
        # Partial progress: lift a RELEASED WO to IN_PROGRESS / stamp actual_start
        # and roll partial qty up without forcing a completion rollup.
        begin_operation_progress(work_order, operation)

    sync_work_order_quantity_complete(
        work_order,
        operation,
        all_operations_complete=work_order.status == WorkOrderStatus.COMPLETE,
    )
    work_order.updated_at = datetime.utcnow()

    # A short close the caller acknowledged clocks everyone still on the operation out,
    # with their quantities UNTOUCHED (nothing is credited to anyone) -- exactly the
    # shop-floor twin's auto-close. A full-count office close never auto-closes: a stale
    # clock-out later is labor-only (the clock-out verb refuses pieces on a COMPLETE op).
    closed_time_entries: list[dict] = []
    if closing_short and open_entries:
        closed_at = datetime.utcnow()
        for entry in open_entries:
            entry.clock_out = closed_at
            if entry.clock_in:
                entry.duration_hours = (closed_at - entry.clock_in).total_seconds() / 3600.0
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
        # COST-3 (Batch 7, OPT-IN): the just-closed entries' hours roll into the
        # operation and work order, summed across every operator.
        if is_labor_cost_rollup_enabled(company_id):
            rollup_labor_hours_for_closed_entries(work_order, operation, open_entries)

    if is_fully_complete:
        scheduling_service = SchedulingService(db, company_id)
        # PERF-5: commit=False joins this scheduling refresh into the handler's single
        # unit of work, so the WO/op state change is committed atomically with the
        # audit rows / FG receipt / cost rollup / quality exceptions written below (the
        # old default commit=True committed the state change mid-handler -- a crash
        # before the terminal commit left a completed WO with no audit/inventory/cost).
        # commit=False skips the in-service WC cache invalidation, so we do it
        # ourselves after the terminal commit succeeds.
        scheduling_service.update_availability_rates(
            work_center_ids=[wc_id for wc_id in affected_work_centers if wc_id],
            horizon_days=90,
            commit=False,
        )
        work_centers_refreshed = True

    # Audit on the tamper-evident chain, BEFORE the terminal commit so the rows commit
    # atomically with the change. The flush keeps a StaleDataError out of the audit
    # savepoint (invariant 4).
    db.flush()
    audit = AuditService(db, current_user, request)
    new_quantity_values = {
        "quantity_complete": float(operation.quantity_complete or 0),
        "quantity_scrapped": float(operation.quantity_scrapped or 0),
        "scrap_reason": operation.scrap_reason,
        "scrap_reason_code_id": operation.scrap_reason_code_id,
        "quantity_short_declared": operation.quantity_short_declared,
    }
    # ONE quantity row on every call (the full close used to record only a status change,
    # so the counts it was decided on were nowhere on the chain), carrying the accounting.
    quantity_row = {
        "action": audit.ACTIONS["UPDATE"],
        "resource_type": "work_order_operation",
        "resource_id": operation.id,
        "resource_identifier": operation.operation_number,
        "description": (
            f"{'Completed' if is_fully_complete else 'Updated'} operation {operation.operation_number} "
            f"from the office. Qty: {round(resolved_quantity, 6):g}/{round(target_qty, 6):g}, "
            f"scrapped {round(new_scrap, 6):g}"
            + (f". Override reason: {override_reason}" if asserted_above_recorded else "")
        ),
        "old_values": old_quantity_values,
        "new_values": new_quantity_values,
        "extra_data": {
            "changes": {
                key: {"old": old_quantity_values[key], "new": new_quantity_values[key]}
                for key in new_quantity_values
                if old_quantity_values[key] != new_quantity_values[key]
            },
            **accounting_extra_data(acct),
            "unaccounted_after": acct.unaccounted,
            "closed_short": closing_short,
            "asserted_above_recorded": asserted_above_recorded,
            "recorded": recorded_good,
            "override_reason": override_reason if asserted_above_recorded else None,
            "acknowledged_open_entries": [entry.id for entry in open_entries] if closing_short else [],
        },
    }
    # REQUIRED evidence (invariant 2, fail-closed) whenever this call declared scrap, closed
    # short, or asserted more good than the floor recorded: such a write landing without its
    # row is the outcome ruling 1 exists to prevent, so the whole unit rolls back with a 503.
    if scrap_delta > QTY_EPSILON or closing_short or asserted_above_recorded:
        with required_audit(db, COMPLETION_AUDIT_UNAVAILABLE):
            audit.log_required(**quantity_row)
    else:
        audit.log(**quantity_row)
    if is_fully_complete:
        audit.log_status_change(
            resource_type="work_order_operation",
            resource_id=operation.id,
            resource_identifier=operation.operation_number,
            old_status=old_operation_status,
            new_status=OperationStatus.COMPLETE.value,
            description=(f"Completed operation {operation.operation_number} on WO {work_order.work_order_number}"),
            extra_data=accounting_extra_data(acct),
        )

    # Material consumption (incremental): deplete THIS operation's tied material the
    # moment the operation is COMPLETE rather than at work-order completion (one laser
    # operation = one nest = one sheet). Its input is the accounted count (good + scrap),
    # never an asserted target. A terminal parent WO was refused with a 409 above, so a
    # finished / cancelled job never consumes. Writes NOTHING for an untied operation; the
    # whole-WO reconcile below stays the self-heal. Placed BEFORE the work-order-completion
    # block so apply_completion_cost_rollup picks up these ledger rows on this request. The
    # db.flush() above keeps an autoflush StaleDataError out of the engine's per-allocation
    # savepoint, where it would be recorded as an ALLOCATION_CONSUMPTION_FAILED row instead
    # of surfacing as a 409.
    if is_fully_complete:
        apply_operation_completion_inventory_effects(
            db, work_order, operation, user_id=current_user.id, company_id=company_id, audit=audit
        )

    if work_order_completed:
        audit.log_status_change(
            resource_type="work_order",
            resource_id=work_order.id,
            resource_identifier=work_order.work_order_number,
            old_status=old_work_order_status,
            new_status=WorkOrderStatus.COMPLETE.value,
        )

    # EVT-2: emit the uniform completion OperationalEvents in-process (before the
    # terminal commit). This office op-complete path previously emitted NO
    # OperationalEvent, so AI/realtime consumers never saw a completion from it.
    if is_fully_complete:
        emit_operation_completed_event(
            db,
            company_id=company_id,
            work_order=work_order,
            operation=operation,
            user_id=current_user.id,
            source_module="work_orders",
        )
    if work_order_completed:
        emit_work_order_completed_event(
            db,
            company_id=company_id,
            work_order=work_order,
            user_id=current_user.id,
            source_module="work_orders",
        )
        # Batch 6 / rank 9 (INV-1/INV-2/INV-3/TRACE-2/TRACE-3): FG receipt (always,
        # lot-only, idempotent) + gated backflush, atomic with this completion. The FG
        # quantity is the header the finalizer set -- after a declared short, what
        # actually left the last stage.
        apply_completion_inventory_effects(db, work_order, user_id=current_user.id, company_id=company_id, audit=audit)
        # Batch 7 / rank 10 (COST-1/COST-2/COST-4/COST-5): OPT-IN labor hour +
        # actual-cost + JobCost rollup, atomic with this completion. No-op + pre-Batch-7
        # behavior when the flag is OFF.
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
    # Batch 7 data-quality signal (no_labor_recorded): on WO COMPLETE, flag any
    # zero-labor operation on the SAME quality_exceptions channel. Fires REGARDLESS of
    # the cost-rollup flag; warn-only.
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
        # A concurrent completer committed a newer version of the operation/WO
        # between our locked read and this commit (version_id_col mismatch).
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="This operation was modified concurrently. Refresh and retry the completion.",
        ) from exc

    # PERF-5: the scheduling refresh ran with commit=False (joined to this handler's
    # unit of work), so it skipped the in-service WC cache invalidation -- do it here,
    # after the terminal commit succeeded, so the cache reflects the freed capacity.
    if work_centers_refreshed:
        invalidate_work_centers_cache()

    # EVT-3: on WO COMPLETE, enqueue the tenant-scoped notification + webhook
    # dispatch in the ARQ worker. After commit + best-effort.
    if work_order_completed:
        enqueue_work_order_completion_signals(work_order_id=work_order.id, company_id=company_id, status="COMPLETE")

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
    return {
        "message": "Operation completed" if is_fully_complete else "Progress updated",
        # False means "saved, still open": the units still unaccounted are in
        # operation.quantity_unaccounted, and the client says so instead of "completed".
        "is_fully_complete": is_fully_complete,
        "operation": _office_completion_operation_payload(operation, acct),
        # The open entries a SHORT close the caller acknowledged clocked out ([] otherwise).
        "closed_time_entries": closed_time_entries,
        # Warn-and-record (Batch 4 / rank 7): unsatisfied quality gates at completion.
        "quality_exceptions": [exc.as_dict() for exc in quality_exceptions],
    }


def _office_completion_operation_payload(operation: WorkOrderOperation, acct: OperationAccounting) -> dict:
    """The ``operation`` block of the office op-complete response (owner ruling 1).

    ``acct`` is the accounting the call was decided on, so ``upstream_quantity_short``
    (units of the target that never reached the operation) and ``quantity_unaccounted``
    (received units still neither good nor scrapped) are the figures the server used.
    """
    status_value = operation.status.value if hasattr(operation.status, "value") else operation.status
    return {
        "id": operation.id,
        "status": status_value,
        "quantity_complete": float(operation.quantity_complete or 0),
        "quantity_scrapped": float(operation.quantity_scrapped or 0),
        "upstream_quantity_short": acct.upstream_short,
        "quantity_unaccounted": acct.unaccounted,
        "quantity_short_declared": operation.quantity_short_declared,
    }
