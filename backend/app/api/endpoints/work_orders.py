import asyncio
import json
import logging
import math
import os
import shutil
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, TypeAdapter, ValidationError
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, joinedload, selectinload
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.orm.exc import StaleDataError

from app.api.deps import get_audit_service, get_current_company_id, get_current_user, require_role
from app.core.cache import invalidate_work_centers_cache
from app.core.realtime import safe_broadcast
from app.core.websocket import (
    broadcast_dashboard_update,
    broadcast_shop_floor_update,
    broadcast_work_order_update,
)
from app.db.database import atomic_transaction, get_db
from app.db.locks import acquire_generator_lock
from app.models.bom import BOM, BOMItem
from app.models.laser_nest import LaserNest
from app.models.part import Part, PartType
from app.models.routing import Routing, RoutingOperation
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus, WorkOrderType
from app.schemas.import_kit import WorkOrderImportResponse
from app.schemas.work_order import (
    LaserNestImportRow,
    LaserNestManualCreate,
    LaserNestManualResponse,
    LaserNestPreviewRow,
    WorkOrderCreate,
    WorkOrderOperationCreate,
    WorkOrderOperationResponse,
    WorkOrderOperationUpdate,
    WorkOrderResponse,
    WorkOrderSummary,
    WorkOrderUpdate,
)
from app.services.audit_service import AuditService
from app.services.completion_cost_service import (
    apply_completion_cost_rollup,
    compute_and_store_estimated_cost,
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
from app.services.import_service import ImportFileError, parse_import_file
from app.services.labor_cost_service import is_labor_cost_rollup_enabled
from app.services.laser_nest_extraction_service import extract_nest_fields_from_pdf
from app.services.laser_nest_service import (
    LASER_PDF_PACKAGE_MAX,
    ParsedLaserNest,
    active_laser_nest,
    build_laser_nest_child_work_order,
    build_parsed_nest_from_extraction,
    copy_laser_nest_folder,
    create_manual_laser_nest,
    extract_laser_nest_zip,
    manual_nest_response_dict,
    package_has_pdfs,
    parse_laser_nest_folder,
    sync_laser_nest_from_operation,
)
from app.services.migration_import_service import import_open_work_orders
from app.services.operational_event_service import OperationalEventService
from app.services.quality_gate_service import (
    QualityException,
    evaluate_and_record_completion_quality_exceptions,
    evaluate_completion_quality_exceptions,
    evaluate_inspection_exception,
    record_completion_quality_exceptions,
    record_reconcile_inspection_exception,
)
from app.services.scheduling_service import SchedulingService
from app.services.storage_service import delete_ref
from app.services.work_order_state_service import (
    TERMINAL_WO_STATUSES,
    StatusTransition,
    WorkOrderStateError,
    begin_operation_progress,
    finalize_operation_completion,
    find_parent_to_advance,
    has_incomplete_predecessors,
    operation_target_quantity,
    reconcile_work_orders_from_completion_evidence,
    release_first_ready_operation,
    resolve_absolute_operation_quantity,
    sync_work_order_quantity_complete,
    validate_operation_quantity,
    work_order_operation_progress,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _audit_reconcile_transitions(
    db: Session,
    current_user: User,
    transitions: list[StatusTransition],
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
            # QG-4 (partial): a completion can happen on a GET via reconcile. Record
            # at minimum the inspection_incomplete exception (cheapest gate, no extra
            # query). NCR/FAI/blocker gates are evaluated on the live completion path,
            # not the read path -- documented partial coverage.
            if tr.resource_type == "work_order_operation":
                record_reconcile_inspection_exception(db, operation_id=tr.resource_id, audit=audit, user=current_user)
    except Exception:  # pragma: no cover - reads must never 500 on audit failure
        pass


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
    except Exception:  # pragma: no cover - reads must never 500 on event-emit failure
        pass


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
    except Exception:  # pragma: no cover - reads must never 500 on parent-advance failure
        pass


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
    except Exception:  # pragma: no cover - reads must never 500 on cost-rollup failure
        pass


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
    OperationalEventService(db).emit(
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


def _get_active_bom(db: Session, part_id: int, company_id: int) -> Optional[BOM]:
    return (
        db.query(BOM)
        .filter(
            BOM.part_id == part_id,
            BOM.company_id == company_id,
            BOM.is_active == True,
        )
        .first()
    )


def _collect_bom_components(
    db: Session,
    bom: BOM,
    company_id: int,
    parent_qty: float = 1.0,
    visited_part_ids: Optional[set[int]] = None,
) -> List[tuple[BOMItem, Part, float]]:
    """Return BOM components in multi-level order with quantity per parent assembly."""
    if visited_part_ids is None:
        visited_part_ids = {bom.part_id}

    items = (
        db.query(BOMItem)
        .options(joinedload(BOMItem.component_part))
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

    components: List[tuple[BOMItem, Part, float]] = []
    for item in items:
        component = item.component_part
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


def _enrich_work_order_operations(work_order: WorkOrder) -> None:
    for op in work_order.operations:
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


def _find_laser_work_center(db: Session, company_id: int, work_center_id: Optional[int] = None) -> WorkCenter:
    query = db.query(WorkCenter).filter(WorkCenter.company_id == company_id, WorkCenter.is_active == True)
    if work_center_id:
        work_center = query.filter(WorkCenter.id == work_center_id).first()
        if not work_center:
            raise HTTPException(status_code=404, detail="Laser work center not found")
        return work_center

    work_center = (
        query.filter(
            or_(
                WorkCenter.name.ilike("%laser%"),
                WorkCenter.work_center_type.ilike("%laser%"),
                WorkCenter.code.ilike("%laser%"),
            )
        )
        .order_by(WorkCenter.id)
        .first()
    )
    if not work_center:
        raise HTTPException(status_code=400, detail="No active laser work center found")
    return work_center


async def _save_upload_to_temp(file: UploadFile) -> str:
    suffix = os.path.splitext(file.filename or "")[1] or ".zip"
    fd, temp_path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "wb") as handle:
        shutil.copyfileobj(file.file, handle)
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

    child = (
        db.query(WorkOrder)
        .filter(
            WorkOrder.company_id == company_id,
            WorkOrder.parent_work_order_id == parent_work_order.id,
            WorkOrder.work_order_type == WorkOrderType.LASER_CUTTING.value,
        )
        .first()
    )
    if child:
        return child

    child = WorkOrder(
        company_id=company_id,
        work_order_number=generate_work_order_number(db, company_id),
        part_id=parent_work_order.part_id,
        parent_work_order_id=parent_work_order.id,
        work_order_type=WorkOrderType.LASER_CUTTING.value,
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


def _load_parent_work_order(db: Session, work_order_id: int, company_id: int) -> WorkOrder:
    work_order = (
        db.query(WorkOrder)
        .options(joinedload(WorkOrder.part))
        .filter(WorkOrder.id == work_order_id, WorkOrder.company_id == company_id)
        .first()
    )
    if not work_order:
        raise HTTPException(status_code=404, detail="Work order not found")
    return work_order


def _build_laser_preview_response(package_name: str, nests: list[dict]) -> LaserNestPreviewResponse:
    return LaserNestPreviewResponse(
        package_name=package_name,
        nest_count=len(nests),
        total_planned_runs=sum(int(nest.get("planned_runs") or 0) for nest in nests),
        nests=nests,
    )


# Bounded fan-out for the per-PDF AI extraction. extract_nest_fields_from_pdf is
# sync/blocking, so each call is dispatched to the threadpool; the semaphore caps
# concurrent in-flight LLM calls (latency vs. provider pressure tradeoff).
_LASER_PDF_EXTRACT_CONCURRENCY = 5


async def _parse_laser_nest_pdf_package_async(folder: str, company_id: int) -> list[ParsedLaserNest]:
    """Parallelized counterpart to ``parse_laser_nest_pdf_package``.

    Globs the PDFs here (enforcing the same cap), then runs the per-file AI
    extraction concurrently via ``run_in_threadpool`` under a semaphore. Returns
    rows in stable (sorted-path) order. The sync helper is kept for the offline
    path and tests; this one is what the async endpoint uses for latency.
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

    semaphore = asyncio.Semaphore(_LASER_PDF_EXTRACT_CONCURRENCY)

    async def _extract(path: Path) -> ParsedLaserNest:
        rel_path = str(path.relative_to(root))
        async with semaphore:
            result = await run_in_threadpool(extract_nest_fields_from_pdf, str(path), path.name, company_id)
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
            )
        )
    return nests


@router.get("/", response_model=List[WorkOrderSummary])
def list_work_orders(
    response: Response,
    skip: int = 0,
    limit: int = 100,
    status: Optional[WorkOrderStatus] = None,
    search: Optional[str] = None,
    include_deleted: bool = Query(False, description="Include soft-deleted work orders (admin only)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """List work orders with summary info"""
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    query = (
        db.query(WorkOrder)
        .filter(WorkOrder.company_id == company_id)
        .options(
            joinedload(WorkOrder.part),
            selectinload(WorkOrder.operations),
        )
    )

    # Filter out soft-deleted unless explicitly requested by admin
    if not include_deleted or current_user.role != UserRole.ADMIN:
        query = query.filter(WorkOrder.is_deleted == False)

    if status:
        query = query.filter(WorkOrder.status == status)
    else:
        # Default: exclude complete/closed/cancelled (only show active work orders)
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
                Part.part_number.ilike(search_filter),
                Part.name.ilike(search_filter),
            )
        )

    work_orders = query.order_by(WorkOrder.priority, WorkOrder.due_date).offset(skip).limit(limit).all()
    # Reconcile-on-read: a concurrent-write conflict here is benign (idempotent),
    # so it must NOT 500 the list -- _reconcile_and_commit swallows StaleDataError.
    # AUD-3: terminal reconcile-driven transitions are audited to the requesting user.
    _reconcile_and_commit(db, work_orders, current_user, company_id)

    result = []
    for wo in work_orders:
        metrics = work_order_operation_progress(wo)
        summary = WorkOrderSummary(
            id=wo.id,
            work_order_number=wo.work_order_number,
            part_id=wo.part_id,
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


def get_work_center_group(work_center: WorkCenter) -> str:
    """Get operation group name from work center type"""
    if not work_center:
        return "OTHER"
    wc_type = work_center.work_center_type.upper() if work_center.work_center_type else ""
    wc_name = work_center.name.upper() if work_center.name else ""

    # Map work center types to groups
    if "LASER" in wc_type or "LASER" in wc_name:
        return "LASER"
    elif "PRESS" in wc_type or "BRAKE" in wc_type or "BEND" in wc_name:
        return "BEND"
    elif "WELD" in wc_type or "WELD" in wc_name:
        return "WELD"
    elif "PAINT" in wc_type or "POWDER" in wc_type or "COAT" in wc_name:
        return "FINISH"
    elif "MACHINE" in wc_type or "CNC" in wc_type or "MILL" in wc_name or "LATHE" in wc_name:
        return "MACHINE"
    elif "ASSEMBLY" in wc_type or "ASSEM" in wc_name:
        return "ASSEMBLY"
    elif "INSPECT" in wc_type or "QC" in wc_name or "QUALITY" in wc_name:
        return "INSPECT"
    else:
        return wc_type or "OTHER"


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

    # Create work order
    wo_data = work_order_in.model_dump(exclude={"operations"})
    work_order = WorkOrder(**wo_data, work_order_number=wo_number, created_by=current_user.id)
    work_order.company_id = company_id
    db.add(work_order)
    db.flush()  # Get the work order ID

    # Auto-generate operations from routing if enabled and no operations provided

    if auto_routing and not work_order_in.operations:
        create_routing_operations_for_work_order(
            db, work_order, part, float(work_order_in.quantity_ordered), company_id
        )
    else:
        # Create operations from input
        for op_data in work_order_in.operations:
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
        },
    )
    db.commit()
    _enrich_work_order_operations(work_order)

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


def _create_assembly_routing_operations(
    db: Session,
    work_order: WorkOrder,
    wo_quantity: float,
    company_id: int = None,
):
    """Create assembly operations from BOM component routings, then assembly routing."""

    sequence = 10
    company_id = company_id or work_order.company_id
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
                    operation_number=f"Op {sequence}",
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
        return

    active_assembly_ops = [op for op in sorted(assembly_routing.operations, key=lambda x: x.sequence) if op.is_active]
    non_inspection_ops = [op for op in active_assembly_ops if not _is_inspection_operation(op)]
    inspection_ops = [op for op in active_assembly_ops if _is_inspection_operation(op)]

    for rop in non_inspection_ops + inspection_ops:
        work_center = rop.work_center
        wo_op = WorkOrderOperation(
            work_order_id=work_order.id,
            sequence=sequence,
            operation_number=f"Op {sequence}",
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
        sequence += 10


def create_routing_operations_for_work_order(
    db: Session,
    work_order: WorkOrder,
    part: Part,
    quantity: float,
    company_id: int,
) -> None:
    """Generate this work order's operations from the part's released routing.

    Single source of truth shared by POST /work-orders (auto_routing=True) and
    the A0.2 Excel-migration open-WO import (``migration_import_service``), so
    imported work orders get exactly the same routed operations as hand-entered
    ones. Assembly-aware: assemblies/BOM parts expand component routings first
    (``_create_assembly_routing_operations``); simple parts copy their released
    routing operations. No-op when no released routing exists (the caller
    decides whether that is an error).
    """
    has_bom = _get_active_bom(db, part.id, company_id) is not None
    if part.part_type == PartType.ASSEMBLY or has_bom:
        _create_assembly_routing_operations(db, work_order, float(quantity), company_id=company_id)
        return

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
        return

    for rop in sorted(routing.operations, key=lambda x: x.sequence):
        if not rop.is_active:
            continue
        work_center = rop.work_center
        wo_op = WorkOrderOperation(
            work_order_id=work_order.id,
            sequence=rop.sequence,
            operation_number=rop.operation_number or f"Op {rop.sequence}",
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


@router.post("/{work_order_id}/laser-nest-packages/preview", response_model=LaserNestPreviewResponse)
async def preview_laser_nest_package_import(
    work_order_id: int,
    file: Optional[UploadFile] = File(None),
    source_path: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
):
    """Preview nest operations detected from a zipped Ermaksan package or server folder.

    Two package shapes, auto-detected: a ZIP/folder of nest-report **PDFs** (the
    new path -- fields extracted by AI, one LLM call per PDF, parallelized with
    bounded concurrency) or the legacy ZIP/folder of CNC **program files** (fields
    inferred from filenames). PDFs and CNC extensions are disjoint, so a package
    is treated as a PDF package iff it contains any ``*.pdf``.
    """
    _load_parent_work_order(db, work_order_id, company_id)
    package_name = _laser_package_name(file, source_path)
    temp_path = None
    try:
        if file:
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

    return _build_laser_preview_response(package_name, nests)


@router.post("/{work_order_id}/laser-nest-packages/import")
async def import_laser_nest_package(
    work_order_id: int,
    file: Optional[UploadFile] = File(None),
    source_path: Optional[str] = Form(None),
    work_center_id: Optional[int] = Form(None),
    rows: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Create or update a child laser work order from one nest package.

    Two paths, both honoring IMPORT-REPLACES-EVERYTHING:
    - ``rows`` provided (PDF confirm-and-commit): the re-sent ZIP supplies PDF
      bytes; the persisted field values are the planner-CONFIRMED ones from the
      JSON ``rows`` (no second AI call). Each nest's PDF is stored as a DRAWING
      Document and attached.
    - ``rows`` absent (legacy CNC-program import): unchanged -- fields inferred
      from filenames, no Documents.

    Both paths audit (DELETE per superseded nest, CREATE per new nest): the
    import wipes ALL prior nests/operations for this child WO, so each wipe is
    recorded before the rebuild and each created nest is recorded after.
    """
    parent_work_order = _load_parent_work_order(db, work_order_id, company_id)
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

    # Storage blobs for nest-PDF Documents are written by storage.save() INSIDE
    # the atomic_transaction, BEFORE it commits. On rollback they must be reaped
    # (they live outside package_dir, so shutil.rmtree(package_dir) misses them).
    saved_storage_keys: list[str] = []

    def _reap_saved_blobs() -> None:
        for key in saved_storage_keys:
            try:
                delete_ref(key)
            except Exception:  # noqa: BLE001 - cleanup must not mask the original error
                logger.warning("Failed to reap orphaned laser-nest blob on rollback: %s", key)

    try:
        if file:
            temp_path = await _save_upload_to_temp(file)
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

        import_source = "pdf_import" if is_pdf_import else "cnc_file_import"

        try:
            with atomic_transaction(db):
                child_work_order = _ensure_laser_child_work_order(
                    db,
                    parent_work_order=parent_work_order,
                    company_id=company_id,
                )
                child_work_order.status = WorkOrderStatus.RELEASED
                child_work_order.quantity_complete = 0
                child_work_order.quantity_scrapped = 0

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
                            "parent_work_order_id": parent_work_order.id,
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
                            "parent_work_order_id": parent_work_order.id,
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
                        "parent_work_order_id": parent_work_order.id,
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
    except ValueError as exc:
        # A pre-commit validation failure (e.g. duplicate source_file, empty
        # package): no transaction committed. Reap any blobs written before the
        # raise, then clean the temp package dir.
        _reap_saved_blobs()
        if os.path.isdir(package_dir):
            shutil.rmtree(package_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)

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
            WorkOrder.parent_work_order_id == parent_work_order.id,
            WorkOrder.work_order_type == WorkOrderType.LASER_CUTTING.value,
        )
        .first()
    )
    _enrich_work_order_operations(child_work_order)

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
            "parent_work_order_id": parent_work_order.id,
        },
        company_id=company_id,
    )

    return {
        "package": _build_laser_preview_response(package_name, [nest.as_dict() for nest in nests]).model_dump(),
        "child_work_order": WorkOrderResponse.model_validate(child_work_order).model_dump(mode="json"),
    }


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
    """Manually key one laser nest onto an assembly WO (standalone creation path).

    Resolves (or creates) the child laser WO and an active laser work center via
    the existing endpoint helpers, then delegates the state change to
    ``create_manual_laser_nest``. Untouched by, and does not touch, the import flow.
    """
    parent_work_order = _load_parent_work_order(db, work_order_id, company_id)

    with atomic_transaction(db):
        child_work_order = _ensure_laser_child_work_order(
            db,
            parent_work_order=parent_work_order,
            company_id=company_id,
        )
        child_work_order.status = WorkOrderStatus.RELEASED
        # _find_laser_work_center raises 400 when no active laser work center exists.
        laser_work_center = _find_laser_work_center(db, company_id)

        nest = create_manual_laser_nest(
            db,
            parent_work_order=parent_work_order,
            child_work_order=child_work_order,
            laser_work_center=laser_work_center,
            data=payload,
            company_id=company_id,
            user_id=current_user.id,
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
                "parent_work_order_id": parent_work_order.id,
                "child_work_order_id": child_work_order.id,
                "source": "manual",
            },
        )

    db.refresh(nest)
    return LaserNestManualResponse(**manual_nest_response_dict(nest))


@router.get("/{work_order_id}", response_model=WorkOrderResponse)
def get_work_order(
    work_order_id: int,
    response: Response,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get a specific work order with all operations"""
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
    _enrich_work_order_operations(work_order)

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
    """Update a work order"""
    work_order = db.query(WorkOrder).filter(WorkOrder.id == work_order_id, WorkOrder.company_id == company_id).first()
    if not work_order:
        raise HTTPException(status_code=404, detail="Work order not found")

    # Capture old values for audit
    audit = AuditService(db, current_user, request)
    old_values = {c.key: getattr(work_order, c.key) for c in work_order.__table__.columns}

    update_data = work_order_in.model_dump(exclude_unset=True)

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

    for field, value in update_data.items():
        setattr(work_order, field, value)

    _emit_work_order_event(
        db,
        company_id=company_id,
        current_user=current_user,
        work_order=work_order,
        event_type="work_order_updated",
        payload={"updated_fields": list(update_data.keys())},
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
    current_user: User = Depends(require_role([UserRole.ADMIN])),
    company_id: int = Depends(get_current_company_id),
):
    """
    Soft delete or permanently delete a work order.

    **Soft delete (default)**: Marks the work order as deleted but preserves data.

    **Hard delete**: Only allowed for draft or cancelled work orders.
    Permanently removes the record and associated operations.
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


@router.post("/{work_order_id}/restore", summary="Restore a soft-deleted work order")
def restore_work_order(
    work_order_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
):
    """Restore a soft-deleted work order."""
    work_order = db.query(WorkOrder).filter(WorkOrder.id == work_order_id, WorkOrder.company_id == company_id).first()
    if not work_order:
        raise HTTPException(status_code=404, detail="Work order not found")

    if not work_order.is_deleted:
        raise HTTPException(status_code=400, detail="Work order is not deleted")

    audit = AuditService(db, current_user, request)

    work_order.restore()

    # Audit BEFORE the terminal commit so the audit row commits atomically with the
    # restore — AuditService.log() only flushes and the session never commits on teardown.
    db.flush()
    audit.log_update(
        "work_order",
        work_order.id,
        work_order.work_order_number,
        old_values={"is_deleted": True},
        new_values={"is_deleted": False},
        action="restore",
    )
    db.commit()

    return {"message": f"Work order {work_order.work_order_number} restored"}


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
        .filter(WorkOrder.id == work_order_id, WorkOrder.company_id == company_id)
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

    release_first_ready_operation(work_order)
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
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Start a work order (set to in-progress)"""
    work_order = db.query(WorkOrder).filter(WorkOrder.id == work_order_id, WorkOrder.company_id == company_id).first()
    if not work_order:
        raise HTTPException(status_code=404, detail="Work order not found")

    if work_order.status not in [WorkOrderStatus.RELEASED, WorkOrderStatus.ON_HOLD]:
        raise HTTPException(status_code=400, detail="Work order must be released or on-hold to start")

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
        payload={"actual_start": work_order.actual_start.isoformat() if work_order.actual_start else None},
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
    db.commit()
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


@router.get("/{work_order_id}/material-requirements")
def get_material_requirements(
    work_order_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get BOM material requirements for a work order with quantities calculated"""
    from app.models.bom import BOM, BOMItem

    work_order = db.query(WorkOrder).filter(WorkOrder.id == work_order_id, WorkOrder.company_id == company_id).first()
    if not work_order:
        raise HTTPException(status_code=404, detail="Work order not found")

    # Get BOM for the part
    bom = db.query(BOM).filter(BOM.part_id == work_order.part_id, BOM.is_active == True).first()

    if not bom:
        return {
            "work_order_id": work_order_id,
            "work_order_number": work_order.work_order_number,
            "quantity_ordered": float(work_order.quantity_ordered),
            "has_bom": False,
            "materials": [],
        }

    # Get BOM items with component parts
    items = db.query(BOMItem).options(joinedload(BOMItem.component_part)).filter(BOMItem.bom_id == bom.id).all()

    materials = []
    for item in items:
        component = item.component_part
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
                    "unit_of_measure": item.unit_of_measure or component.unit_of_measure.value,
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


@router.post("/{work_order_id}/complete")
def complete_work_order(
    work_order_id: int,
    request: Request,
    quantity_complete: float,
    quantity_scrapped: Optional[float] = None,
    scrap_reason: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(
        require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR, UserRole.QUALITY])
    ),
    company_id: int = Depends(get_current_company_id),
):
    """Manually complete a work order (privileged override).

    DUP-4: this override now delegates to the SHARED rollup instead of blindly
    flipping the WO to COMPLETE. It force-completes every still-open operation
    through the shared finalizer -- each gets ``actual_end``/``completed_by``
    stamped, an audit row, and the WO ``actual_start``/qty-sync/scheduling refresh
    -- so it can no longer leave a COMPLETE WO with open operations and unreleased
    capacity. The manager-supplied ``quantity_complete`` is bounded
    (validate_operation_quantity-style) and applied as a max-guarded override on
    top of the computed finished quantity.

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
        .filter(WorkOrder.id == work_order_id, WorkOrder.company_id == company_id)
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
    # any positive scrap MUST carry a non-blank reason. Query-param path, so the guard lives
    # in the handler (no Pydantic body validator). 422 matches the scrap-reason enforcement
    # semantics established this session; blank/whitespace counts as missing.
    if quantity_scrapped is not None and quantity_scrapped > 0 and not (scrap_reason and scrap_reason.strip()):
        raise HTTPException(
            status_code=422,
            detail="scrap_reason is required when quantity_scrapped is greater than 0",
        )
    if ordered_qty > 0 and quantity_complete > ordered_qty:
        raise HTTPException(
            status_code=400,
            detail=f"quantity_complete ({quantity_complete}) cannot exceed quantity ordered ({ordered_qty})",
        )

    operations = (
        db.query(WorkOrderOperation)
        .filter(WorkOrderOperation.work_order_id == work_order.id, WorkOrderOperation.company_id == company_id)
        .order_by(WorkOrderOperation.id)
        .with_for_update()
        .all()
    )
    work_order.operations = operations

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

    old_status = work_order.status.value if work_order.status else None
    old_quantity_complete = float(work_order.quantity_complete or 0)
    old_quantity_scrapped = float(work_order.quantity_scrapped or 0)
    old_scrap_reason = work_order.scrap_reason

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

    # Apply the manager-supplied finished quantities as a max-guarded override on
    # top of what the rollup computed -- never regress finished quantity (RUP-6).
    work_order.quantity_complete = max(float(work_order.quantity_complete or 0), float(quantity_complete))
    # DUP-3: only overwrite recorded WO scrap when an explicit value was supplied;
    # a defaulted (omitted) call must not zero previously-booked scrap. The
    # scrap-reason guard above (422) has already ensured a positive scrap carries a
    # non-blank reason, so persist it alongside the quantity.
    if quantity_scrapped is not None:
        work_order.quantity_scrapped = quantity_scrapped
        work_order.scrap_reason = scrap_reason
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
    )
    audit.log_update(
        resource_type="work_order",
        resource_id=work_order.id,
        resource_identifier=work_order.work_order_number,
        old_values={
            "quantity_complete": old_quantity_complete,
            "quantity_scrapped": old_quantity_scrapped,
            "scrap_reason": old_scrap_reason,
        },
        new_values={
            "quantity_complete": work_order.quantity_complete,
            "quantity_scrapped": effective_quantity_scrapped,
            "scrap_reason": work_order.scrap_reason,
        },
        description=f"Recorded completion quantities for work order {work_order.work_order_number}",
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

    operation = WorkOrderOperation(
        work_order_id=work_order_id, company_id=work_order.company_id, **operation_in.model_dump()
    )
    db.add(operation)
    db.commit()
    db.refresh(operation)
    return operation


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
    """Update an operation"""
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

    db.commit()
    db.refresh(operation)
    return operation


@router.post("/operations/{operation_id}/start")
def start_operation(
    operation_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Start an operation"""
    operation = (
        db.query(WorkOrderOperation)
        .options(joinedload(WorkOrderOperation.work_order).joinedload(WorkOrder.part))
        .filter(WorkOrderOperation.id == operation_id, WorkOrderOperation.company_id == company_id)
        .first()
    )
    if not operation:
        raise HTTPException(status_code=404, detail="Operation not found")

    work_order = operation.work_order
    if not work_order:
        raise HTTPException(status_code=404, detail="Work order not found")

    if has_incomplete_predecessors(
        db,
        operation.work_order_id,
        operation.sequence,
        operation.id,
        operation.work_center_id,
        allow_same_work_center=False,
    ):
        raise HTTPException(status_code=400, detail="Previous operations must be completed first")

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
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Complete an operation.

    DUP-3 scrap contract: ``quantity_scrapped`` is now optional. When omitted it
    is NOT written, so this office path can no longer silently zero accumulated
    operation scrap with a defaulted-0 query param. Pass an explicit value
    (including 0) to update scrap.

    ON_HOLD policy (QG-5 / BLK-1): an ON_HOLD operation is REFUSED here, matching
    the shop-floor twin. This path no longer force-lifts a held op to IN_PROGRESS
    and silently completes it (leaving its blocker open). The quality-gate/blocker
    enforcement that decides what may complete is Batch 4 (rank 7); here the two
    endpoints are only made consistent.
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
            .filter(
                WorkOrder.id == operation.work_order_id,
                WorkOrder.company_id == company_id,
                WorkOrder.is_deleted == False,  # noqa: E712
            )
            .with_for_update()
            .first()
        )

    # G6-A: refuse to complete an operation against a TERMINAL parent WO
    # (CANCELLED/CLOSED/COMPLETE) before any mutation -- mirrors the ON_HOLD 409 the
    # op-complete handlers already enforce. Without this, finalizing the last op of a
    # CANCELLED WO would resurrect it to COMPLETE via the shared finalizer and re-fire
    # FG receipt/backflush/cost rollup plus a COMPLETE audit row.
    if work_order and work_order.status in TERMINAL_WO_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"cannot complete operation: work order is {work_order.status.value}",
        )

    if work_order and has_incomplete_predecessors(
        db,
        operation.work_order_id,
        operation.sequence,
        operation.id,
        operation.work_center_id,
        allow_same_work_center=False,
    ):
        raise HTTPException(status_code=400, detail="Previous operations must be completed first")

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

    # Capture pre-mutation statuses/quantities so transitions can be audited below.
    old_operation_status = operation.status.value if operation.status else None
    old_work_order_status = work_order.status.value if work_order and work_order.status else None
    old_quantity_complete = float(operation.quantity_complete or 0)
    work_order_completed = False
    # PERF-5: tracks whether the scheduling refresh ran (it runs with commit=False,
    # so the WC cache must be invalidated by us after the terminal commit succeeds).
    work_centers_refreshed = False

    # Auto-start a READY op (consistent with the shop_floor twin). ON_HOLD is no
    # longer reachable here -- it was refused above.
    if operation.status != OperationStatus.IN_PROGRESS:
        operation.status = OperationStatus.IN_PROGRESS
        if not operation.actual_start:
            operation.actual_start = datetime.utcnow()
            operation.started_by = current_user.id

    # ABSOLUTE verb (DUP-3 / SFI-5): clamp to max(existing, requested, evidence)
    # capped at target so the office /complete can never lower the operation below
    # durable TimeEntry evidence (which a later reconcile would silently re-raise).
    resolved_quantity = resolve_absolute_operation_quantity(db, operation, quantity_complete, target_qty)
    operation.quantity_complete = resolved_quantity
    # DUP-3 scrap: only overwrite when an explicit value was provided.
    if quantity_scrapped is not None:
        # Small correctness fix (compliance auditor): this office path had no non-negative
        # guard on scrap (unlike complete_work_order). Reject a negative scrap with a 400.
        if quantity_scrapped < 0:
            raise HTTPException(status_code=400, detail="quantity_scrapped cannot be negative")
        # AS9100D defect-traceability invariant (same rule as ClockOut/ProductionReportRequest):
        # any positive scrap MUST carry a non-blank reason. Query-param path, so the guard lives
        # in the handler (no Pydantic body validator). 422 matches the scrap-reason enforcement
        # semantics established this session; blank/whitespace counts as missing.
        if quantity_scrapped > 0 and not (scrap_reason and scrap_reason.strip()):
            raise HTTPException(
                status_code=422,
                detail="scrap_reason is required when quantity_scrapped is greater than 0",
            )
        operation.quantity_scrapped = quantity_scrapped
        operation.scrap_reason = scrap_reason
    operation.updated_at = datetime.utcnow()
    sync_laser_nest_from_operation(operation)

    is_fully_complete = resolved_quantity >= target_qty
    if is_fully_complete:
        operation.status = OperationStatus.COMPLETE
        operation.actual_end = datetime.utcnow()
        operation.completed_by = current_user.id

    # work_order is the row already locked above; don't re-derive it from the
    # (unlocked, unscoped) relationship. The shared finalizer owns the rollup
    # (DUP-5): remaining-ops decision, COMPLETE-vs-release, actual_start/actual_end
    # stamping, qty sync, current_operation_id; it returns the WCs to refresh.
    affected_work_centers = {operation.work_center_id}
    if work_order and is_fully_complete:
        affected_work_centers |= finalize_operation_completion(db, work_order, operation)
        work_order_completed = work_order.status == WorkOrderStatus.COMPLETE
    elif work_order and not is_fully_complete:
        # Partial progress: lift a RELEASED WO to IN_PROGRESS / stamp actual_start
        # and roll partial qty up without forcing a completion rollup.
        begin_operation_progress(work_order, operation)

    if work_order:
        sync_work_order_quantity_complete(
            work_order,
            operation,
            all_operations_complete=work_order.status == WorkOrderStatus.COMPLETE,
        )
        work_order.updated_at = datetime.utcnow()

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

    # Audit completion transitions on the tamper-evident chain. This office/admin
    # op-complete path previously emitted neither an OperationalEvent nor an audit
    # row, unlike its shop_floor twin. Logged BEFORE the terminal commit so the
    # audit rows commit atomically with the status change.
    db.flush()
    audit = AuditService(db, current_user, request)
    if is_fully_complete:
        audit.log_status_change(
            resource_type="work_order_operation",
            resource_id=operation.id,
            resource_identifier=operation.operation_number,
            old_status=old_operation_status,
            new_status=OperationStatus.COMPLETE.value,
            description=(
                f"Completed operation {operation.operation_number}"
                + (f" on WO {work_order.work_order_number}" if work_order else "")
            ),
        )
    else:
        # Record the RESOLVED (evidence-floored) quantity actually stored, and only
        # include scrap in the diff when it was explicitly provided (DUP-3 scrap).
        new_values: dict = {"quantity_complete": resolved_quantity}
        if quantity_scrapped is not None:
            new_values["quantity_scrapped"] = quantity_scrapped
            new_values["scrap_reason"] = scrap_reason
        audit.log_update(
            resource_type="work_order_operation",
            resource_id=operation.id,
            resource_identifier=operation.operation_number,
            old_values={"quantity_complete": old_quantity_complete},
            new_values=new_values,
            description=f"Updated operation {operation.operation_number} progress",
        )
    if work_order_completed and work_order:
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
    if is_fully_complete and work_order:
        emit_operation_completed_event(
            db,
            company_id=company_id,
            work_order=work_order,
            operation=operation,
            user_id=current_user.id,
            source_module="work_orders",
        )
    if work_order_completed and work_order:
        emit_work_order_completed_event(
            db,
            company_id=company_id,
            work_order=work_order,
            user_id=current_user.id,
            source_module="work_orders",
        )
        # Batch 6 / rank 9 (INV-1/INV-2/INV-3/TRACE-2/TRACE-3): FG receipt (always,
        # lot-only, idempotent) + gated backflush, atomic with this completion.
        apply_completion_inventory_effects(db, work_order, user_id=current_user.id, company_id=company_id, audit=audit)
        # Batch 7 / rank 10 (COST-1/COST-2/COST-4/COST-5): OPT-IN labor hour +
        # actual-cost + JobCost rollup, atomic with this completion. No-op + pre-Batch-7
        # behavior when the flag is OFF. (This office path does NOT auto-close open
        # TimeEntries -- they are rolled up by a later clock_out -- so the rollup here is
        # purely evidence-sourced from already-closed entries.)
        apply_completion_cost_rollup(db, work_order, company_id=company_id, user_id=current_user.id, audit=audit)

    # Batch 4 / rank 7 (QG-1/3, BLK-2): warn-and-record on a true completion only.
    # Read-only evaluation against the locked op + WO; each unsatisfied gate gets a
    # tamper-evident audit row + warning event committed atomically below. Never blocks.
    quality_exceptions: list[QualityException] = []
    if is_fully_complete and work_order:
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
    if work_order_completed and work_order:
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
    if work_order_completed and work_order:
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
    if work_order_completed and work_order:
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
        # Warn-and-record (Batch 4 / rank 7): unsatisfied quality gates at completion.
        "quality_exceptions": [exc.as_dict() for exc in quality_exceptions],
    }
