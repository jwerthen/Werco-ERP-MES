import os
import shutil
import tempfile
import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, joinedload, selectinload
from sqlalchemy.orm.exc import StaleDataError

from app.api.deps import get_current_company_id, get_current_user, require_role
from app.core.realtime import safe_broadcast
from app.core.websocket import (
    broadcast_dashboard_update,
    broadcast_shop_floor_update,
    broadcast_work_order_update,
)
from app.db.database import atomic_transaction, get_db
from app.db.locks import acquire_generator_lock
from app.models.bom import BOM, BOMItem
from app.models.part import Part, PartType
from app.models.routing import Routing, RoutingOperation
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus, WorkOrderType
from app.schemas.work_order import (
    WorkOrderCreate,
    WorkOrderOperationCreate,
    WorkOrderOperationResponse,
    WorkOrderOperationUpdate,
    WorkOrderResponse,
    WorkOrderSummary,
    WorkOrderUpdate,
)
from app.services.audit_service import AuditService
from app.services.completion_signal_service import (
    emit_operation_completed_event,
    emit_work_order_completed_event,
    enqueue_work_order_completion_signals,
)
from app.services.laser_nest_service import (
    build_laser_nest_child_work_order,
    copy_laser_nest_folder,
    extract_laser_nest_zip,
    parse_laser_nest_folder,
    parse_laser_nest_zip,
    sync_laser_nest_from_operation,
)
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
from app.services.work_order_state_service import (
    StatusTransition,
    WorkOrderStateError,
    begin_operation_progress,
    finalize_operation_completion,
    has_incomplete_predecessors,
    operation_target_quantity,
    reconcile_work_orders_from_completion_evidence,
    release_first_ready_operation,
    resolve_absolute_operation_quantity,
    sync_work_order_quantity_complete,
    validate_operation_quantity,
    work_order_operation_progress,
)

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
    except Exception:  # pragma: no cover - reads must never 500 on event-emit failure
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
            db.commit()
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
    nests: list[dict]


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
    has_bom = _get_active_bom(db, part.id, company_id) is not None

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
        if part.part_type == PartType.ASSEMBLY or has_bom:
            _create_assembly_routing_operations(
                db,
                work_order,
                float(work_order_in.quantity_ordered),
                company_id=company_id,
            )
        else:
            routing = (
                db.query(Routing)
                .options(selectinload(Routing.operations).selectinload(RoutingOperation.work_center))
                .filter(
                    Routing.company_id == company_id,
                    Routing.part_id == work_order_in.part_id,
                    Routing.is_active == True,
                    Routing.status == "released",
                )
                .first()
            )

            if routing:
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
                        run_time_hours=float(rop.run_hours_per_unit or 0) * float(work_order_in.quantity_ordered),
                        setup_instructions=rop.setup_instructions,
                        run_instructions=rop.work_instructions,
                        requires_inspection=rop.is_inspection_point,
                        inspection_type="final" if _is_inspection_operation(rop) else None,
                        status=OperationStatus.PENDING,
                        operation_group=get_work_center_group(work_center) if work_center else None,
                        company_id=company_id,
                    )
                    db.add(wo_op)
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
            selectinload(WorkOrder.operations).selectinload(WorkOrderOperation.laser_nest),
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
                selectinload(WorkOrder.operations).selectinload(WorkOrderOperation.laser_nest),
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


@router.post("/{work_order_id}/laser-nest-packages/preview", response_model=LaserNestPreviewResponse)
async def preview_laser_nest_package_import(
    work_order_id: int,
    file: Optional[UploadFile] = File(None),
    source_path: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
):
    """Preview nest operations detected from a zipped Ermaksan package or server folder."""
    _load_parent_work_order(db, work_order_id, company_id)
    package_name = _laser_package_name(file, source_path)
    temp_path = None
    try:
        if file:
            temp_path = await _save_upload_to_temp(file)
            nests = [nest.as_dict() for nest in parse_laser_nest_zip(temp_path)]
        elif source_path:
            nests = [nest.as_dict() for nest in parse_laser_nest_folder(source_path)]
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
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
):
    """Create or update a child laser work order from one nest package."""
    parent_work_order = _load_parent_work_order(db, work_order_id, company_id)
    package_name = _laser_package_name(file, source_path)
    temp_path = None
    package_dir = os.path.join(_resolve_laser_upload_root(), str(uuid.uuid4()))

    try:
        if file:
            temp_path = await _save_upload_to_temp(file)
            extract_laser_nest_zip(temp_path, package_dir)
        elif source_path:
            copy_laser_nest_folder(source_path, package_dir)
        else:
            raise HTTPException(status_code=400, detail="Upload a zipped package or provide source_path")

        nests = parse_laser_nest_folder(package_dir)
        laser_work_center = _find_laser_work_center(db, company_id, work_center_id)

        with atomic_transaction(db):
            child_work_order = _ensure_laser_child_work_order(
                db,
                parent_work_order=parent_work_order,
                company_id=company_id,
            )
            child_work_order.status = WorkOrderStatus.RELEASED
            child_work_order.quantity_complete = 0
            child_work_order.quantity_scrapped = 0

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
                },
            )
    except ValueError as exc:
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
            selectinload(WorkOrder.operations).selectinload(WorkOrderOperation.laser_nest),
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
            selectinload(WorkOrder.operations).selectinload(WorkOrderOperation.laser_nest),
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
            selectinload(WorkOrder.operations).selectinload(WorkOrderOperation.laser_nest),
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

    # Bound the manager-supplied quantities (DUP-4): non-negative and not above the
    # quantity ordered. quantity_ordered is the natural cap for a finished WO.
    # quantity_complete is required; quantity_scrapped is optional (DUP-3) and only
    # bounded when explicitly provided.
    ordered_qty = float(work_order.quantity_ordered or 0)
    if quantity_complete is None or quantity_complete < 0:
        raise HTTPException(status_code=400, detail="quantity_complete cannot be negative")
    if quantity_scrapped is not None and quantity_scrapped < 0:
        raise HTTPException(status_code=400, detail="quantity_scrapped cannot be negative")
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

    db.flush()
    audit = AuditService(db, current_user, request)

    # Force-complete each still-open operation through the shared path so each is
    # stamped + audited and the route is genuinely closed (no COMPLETE WO over open
    # ops). The last force-complete drives the WO to COMPLETE via the finalizer.
    now = datetime.utcnow()
    affected_work_centers: set[int] = set()
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
    # a defaulted (omitted) call must not zero previously-booked scrap.
    if quantity_scrapped is not None:
        work_order.quantity_scrapped = quantity_scrapped
    work_order.updated_at = now
    # The effective scrap actually persisted (the existing value when omitted), used
    # in the event + audit payloads so they reflect what was stored, not the raw arg.
    effective_quantity_scrapped = float(work_order.quantity_scrapped or 0)

    # Release capacity for every affected work center (DUP-4: this override used to
    # emit no scheduling refresh, stranding capacity for the still-open operations).
    if affected_work_centers:
        SchedulingService(db, company_id).update_availability_rates(
            work_center_ids=[wc_id for wc_id in affected_work_centers if wc_id], horizon_days=90
        )

    _emit_work_order_event(
        db,
        company_id=company_id,
        current_user=current_user,
        work_order=work_order,
        event_type="work_order_completed",
        payload={"quantity_complete": work_order.quantity_complete, "quantity_scrapped": effective_quantity_scrapped},
    )

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
        old_values={"quantity_complete": old_quantity_complete, "quantity_scrapped": old_quantity_scrapped},
        new_values={
            "quantity_complete": work_order.quantity_complete,
            "quantity_scrapped": effective_quantity_scrapped,
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
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
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

    update_data = operation_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(operation, field, value)
    sync_laser_nest_from_operation(operation)

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
        operation.quantity_scrapped = quantity_scrapped
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
        scheduling_service.update_availability_rates(
            work_center_ids=[wc_id for wc_id in affected_work_centers if wc_id], horizon_days=90
        )

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
