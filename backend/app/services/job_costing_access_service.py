"""The job-cost HTTP boundary: tenant resolution, related IDs and audited writes.

Completion cost rollups keep their existing service. HTTP writers lock the job
header before changing entries, and the caller commits the required audit evidence
with the financial change through ``job_cost_transaction``.
"""

from contextlib import contextmanager

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import inspect, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload, selectinload, with_loader_criteria

from app.db.tenant_filter import tenant_filter, tenant_query
from app.models.job_costing import CostEntry, JobCost
from app.models.part import Part
from app.models.time_entry import TimeEntry
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder, WorkOrderOperation
from app.schemas.job_costing import CostEntryCreate, JobCostCreate, JobCostUpdate
from app.services.audit_service import AuditService, AuditWriteError
from app.services.job_costing_service import recalculate_totals, recompute_from_time_entries
from app.services.labor_cost_service import is_approved_labor_required


@contextmanager
def job_cost_transaction(db: Session):
    """A refusal or missing required audit must leave no financial side effects."""
    try:
        yield
        db.commit()
    except AuditWriteError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="Unable to save audit record") from exc
    except Exception:
        db.rollback()
        raise


def job_cost_query(db: Session, company_id: int):
    """Only costs whose own tenant AND parent WO match the active company.

    FK ownership is not enforced by the database. Apply predicates to relationship
    loads too, including later refreshes inside the existing recompute service.
    populate_existing replaces previously loaded, unscoped relationship state.
    """
    query = tenant_query(db, JobCost, company_id).join(WorkOrder, JobCost.work_order_id == WorkOrder.id)
    return (
        tenant_filter(query, WorkOrder, company_id)
        .options(
            # Historical financial records retain same-company part labels,
            # including retired/deleted parts; only the tenant boundary masks them.
            joinedload(JobCost.work_order).joinedload(WorkOrder.part.and_(Part.company_id == company_id)),
            selectinload(JobCost.entries.and_(CostEntry.company_id == company_id)),
            with_loader_criteria(CostEntry, CostEntry.company_id == company_id),
        )
        .populate_existing()
    )


def resolve_job_cost(db: Session, job_cost_id: int, company_id: int, *, for_write: bool = False) -> JobCost:
    query = job_cost_query(db, company_id).filter(JobCost.id == job_cost_id)
    if for_write:
        query = query.filter(WorkOrder.is_deleted.is_(False)).with_for_update(of=JobCost)
    job_cost = query.first()
    if job_cost is None:
        raise HTTPException(status_code=404, detail="Job cost not found")
    return job_cost


def cost_entry_query(db: Session, job_cost: JobCost, company_id: int):
    return (
        tenant_query(db, CostEntry, company_id)
        .filter(CostEntry.job_cost_id == job_cost.id)
        .options(
            joinedload(
                CostEntry.operation.and_(
                    WorkOrderOperation.company_id == company_id,
                    WorkOrderOperation.work_order_id == job_cost.work_order_id,
                )
            ),
            joinedload(
                CostEntry.creator.and_(
                    or_(
                        User.company_id == company_id, User.role == UserRole.PLATFORM_ADMIN, User.is_superuser.is_(True)
                    )
                )
            ),
        )
        .populate_existing()
    )


def build_cost_entry_response(entry: CostEntry) -> dict:
    """Do not disclose foreign/mismatched operation or ordinary-user references."""
    values = snapshot(entry)
    values["work_order_operation_id"] = entry.operation.id if entry.operation else None
    values["created_by"] = entry.creator.id if entry.creator else None
    return values


def snapshot(row) -> dict:
    return jsonable_encoder({column.key: getattr(row, column.key) for column in inspect(row).mapper.column_attrs})


def _require_operation(db: Session, operation_id: int, work_order_id: int, company_id: int):
    operation = (
        tenant_query(db, WorkOrderOperation, company_id)
        .filter(WorkOrderOperation.id == operation_id, WorkOrderOperation.work_order_id == work_order_id)
        .first()
    )
    if operation is None:
        raise HTTPException(status_code=404, detail="Work order operation not found")


def create_job_cost(db: Session, data: JobCostCreate, company_id: int, audit: AuditService) -> JobCost:
    work_order = (
        tenant_query(db, WorkOrder, company_id)
        .filter(WorkOrder.id == data.work_order_id, WorkOrder.is_deleted.is_(False))
        .with_for_update()
        .first()
    )
    if work_order is None:
        raise HTTPException(status_code=404, detail="Work order not found")
    if tenant_query(db, JobCost, company_id).filter(JobCost.work_order_id == work_order.id).first():
        raise HTTPException(status_code=400, detail="Job cost already exists for this work order")

    job_cost = JobCost(company_id=company_id, **data.model_dump())
    job_cost.estimated_total_cost = (
        data.estimated_material_cost + data.estimated_labor_cost + data.estimated_overhead_cost
    )
    if data.revenue > 0:
        job_cost.margin_amount = data.revenue
        job_cost.margin_percent = 100.0
    db.add(job_cost)
    try:
        db.flush()
    except IntegrityError as exc:
        # A malformed legacy row owned by another tenant can occupy the global
        # work_order_id unique key. Reveal no foreign record; roll back at caller.
        raise HTTPException(status_code=409, detail="Job cost could not be created") from exc
    audit.log_required(
        "CREATE",
        "job_cost",
        resource_id=job_cost.id,
        resource_identifier=str(job_cost.id),
        new_values=snapshot(job_cost),
        company_id=company_id,
    )
    return job_cost


def update_job_cost(
    db: Session, job_cost_id: int, data: JobCostUpdate, company_id: int, audit: AuditService
) -> JobCost:
    job_cost = resolve_job_cost(db, job_cost_id, company_id, for_write=True)
    before = snapshot(job_cost)
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(job_cost, field, value)
    recalculate_totals(job_cost)
    audit.log_required(
        "STATUS_CHANGE" if before["status"] != job_cost.status else "UPDATE",
        "job_cost",
        resource_id=job_cost.id,
        resource_identifier=str(job_cost.id),
        old_values=before,
        new_values=snapshot(job_cost),
        company_id=company_id,
    )
    return job_cost


def add_cost_entry(
    db: Session,
    job_cost_id: int,
    data: CostEntryCreate,
    company_id: int,
    user_id: int,
    audit: AuditService,
) -> CostEntry:
    job_cost = resolve_job_cost(db, job_cost_id, company_id, for_write=True)
    if data.work_order_operation_id is not None:
        _require_operation(db, data.work_order_operation_id, job_cost.work_order_id, company_id)
    before = snapshot(job_cost)
    entry = CostEntry(
        company_id=company_id,
        job_cost_id=job_cost.id,
        created_by=user_id,
        total_cost=data.quantity * data.unit_cost,
        **data.model_dump(),
    )
    job_cost.entries.append(entry)
    db.flush()
    recalculate_totals(job_cost)
    audit.log_required(
        "CREATE",
        "cost_entry",
        resource_id=entry.id,
        resource_identifier=str(entry.id),
        new_values=snapshot(entry),
        company_id=company_id,
        extra_data={"job_cost_before": before, "job_cost_after": snapshot(job_cost)},
    )
    return entry


def delete_cost_entry(db: Session, job_cost_id: int, entry_id: int, company_id: int, audit: AuditService):
    job_cost = resolve_job_cost(db, job_cost_id, company_id, for_write=True)
    entry = cost_entry_query(db, job_cost, company_id).filter(CostEntry.id == entry_id).first()
    if entry is None:
        raise HTTPException(status_code=404, detail="Cost entry not found")
    before = snapshot(job_cost)
    entry_before = snapshot(entry)
    # CostEntry has no SoftDeleteMixin. Preserve its existing deletion contract,
    # with required audit evidence and the parent totals in the same transaction.
    job_cost.entries.remove(entry)
    db.flush()
    recalculate_totals(job_cost)
    audit.log_required(
        "DELETE",
        "cost_entry",
        resource_id=entry_id,
        resource_identifier=str(entry_id),
        old_values=entry_before,
        company_id=company_id,
        extra_data={"soft_delete": False, "job_cost_before": before, "job_cost_after": snapshot(job_cost)},
    )


def calculate_job_cost(db: Session, job_cost_id: int, company_id: int, user_id: int, audit: AuditService) -> JobCost:
    job_cost = resolve_job_cost(db, job_cost_id, company_id, for_write=True)
    # Validate source operation FKs before the shared service deletes/rebuilds
    # entries. Do not let a legacy TimeEntry mint another invalid cost reference.
    time_entries = tenant_query(db, TimeEntry, company_id).filter(
        TimeEntry.work_order_id == job_cost.work_order_id,
        TimeEntry.clock_out.isnot(None),
    )
    if is_approved_labor_required(company_id):
        time_entries = time_entries.filter(TimeEntry.approved.isnot(None))
    for (operation_id,) in time_entries.with_entities(TimeEntry.operation_id).distinct():
        if operation_id is not None:
            _require_operation(db, operation_id, job_cost.work_order_id, company_id)

    before = {"job_cost": snapshot(job_cost), "entries": [snapshot(e) for e in job_cost.entries]}
    recompute_from_time_entries(db, job_cost=job_cost, company_id=company_id, user_id=user_id)
    audit.log_required(
        "RECALCULATE",
        "job_cost",
        resource_id=job_cost.id,
        resource_identifier=str(job_cost.id),
        old_values=before,
        new_values={"job_cost": snapshot(job_cost), "entries": [snapshot(e) for e in job_cost.entries]},
        company_id=company_id,
    )
    return job_cost
