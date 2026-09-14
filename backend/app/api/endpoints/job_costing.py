from datetime import date, datetime
from typing import List, Optional

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_audit_service, get_current_company_id, get_current_user, require_role
from app.db.database import get_db
from app.models.job_costing import CostEntry, JobCost, JobCostStatus
from app.models.user import User, UserRole
from app.schemas.job_costing import CostEntryCreate, CostEntryResponse, JobCostCreate, JobCostResponse, JobCostUpdate
from app.services import job_costing_access_service as access
from app.services.audit_service import AuditService

router = APIRouter()
WRITE_ROLES = [UserRole.ADMIN, UserRole.MANAGER]


def build_job_cost_response(job_cost: JobCost) -> dict:
    """Build enriched response dict with work order info."""
    wo = job_cost.work_order
    part_number = None
    part_name = None
    if wo and hasattr(wo, 'part') and wo.part:
        part_number = wo.part.part_number if hasattr(wo.part, 'part_number') else None
        part_name = wo.part.name if hasattr(wo.part, 'name') else None

    return {
        "id": job_cost.id,
        "work_order_id": job_cost.work_order_id,
        "estimated_material_cost": job_cost.estimated_material_cost,
        "estimated_labor_cost": job_cost.estimated_labor_cost,
        "estimated_overhead_cost": job_cost.estimated_overhead_cost,
        "estimated_total_cost": job_cost.estimated_total_cost,
        "actual_material_cost": job_cost.actual_material_cost,
        "actual_labor_cost": job_cost.actual_labor_cost,
        "actual_overhead_cost": job_cost.actual_overhead_cost,
        "actual_total_cost": job_cost.actual_total_cost,
        "material_variance": job_cost.material_variance,
        "labor_variance": job_cost.labor_variance,
        "overhead_variance": job_cost.overhead_variance,
        "total_variance": job_cost.total_variance,
        "margin_amount": job_cost.margin_amount,
        "margin_percent": job_cost.margin_percent,
        "revenue": job_cost.revenue,
        "status": job_cost.status.value if hasattr(job_cost.status, 'value') else job_cost.status,
        "notes": job_cost.notes,
        "created_at": job_cost.created_at,
        "updated_at": job_cost.updated_at,
        "work_order_number": wo.work_order_number if wo else None,
        "part_number": part_number,
        "part_name": part_name,
        "customer_name": wo.customer_name if wo else None,
    }


# ── Endpoints ─────────────────────────────────────────────────────


@router.get("/", response_model=List[JobCostResponse])
def list_job_costs(
    status: Optional[str] = None,
    work_order_id: Optional[int] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """List all job costs with optional filtering."""
    query = access.job_cost_query(db, company_id)

    if status:
        query = query.filter(JobCost.status == status)
    if work_order_id:
        query = query.filter(JobCost.work_order_id == work_order_id)
    if date_from:
        query = query.filter(JobCost.created_at >= datetime.combine(date_from, datetime.min.time()))
    if date_to:
        query = query.filter(JobCost.created_at <= datetime.combine(date_to, datetime.max.time()))

    job_costs = query.order_by(JobCost.updated_at.desc()).all()
    return [build_job_cost_response(jc) for jc in job_costs]


@router.get("/summary")
def get_summary(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get summary statistics for job costing dashboard."""
    all_jobs = access.job_cost_query(db, company_id).all()

    in_progress = [j for j in all_jobs if j.status == JobCostStatus.IN_PROGRESS or j.status == "in_progress"]
    completed = [j for j in all_jobs if j.status == JobCostStatus.COMPLETED or j.status == "completed"]
    reviewed = [j for j in all_jobs if j.status == JobCostStatus.REVIEWED or j.status == "reviewed"]

    # Total WIP value (actual cost of in-progress jobs)
    total_wip = sum(j.actual_total_cost for j in in_progress)

    # Average margin % across all jobs with revenue
    jobs_with_margin = [j for j in all_jobs if j.revenue and j.revenue > 0]
    avg_margin = 0.0
    if jobs_with_margin:
        avg_margin = sum(j.margin_percent for j in jobs_with_margin) / len(jobs_with_margin)

    # Jobs over budget (positive total_variance means over budget)
    over_budget = len([j for j in all_jobs if j.total_variance > 0])

    # Jobs completed this month
    now = datetime.utcnow()
    first_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    completed_this_month = len([j for j in (completed + reviewed) if j.updated_at and j.updated_at >= first_of_month])

    # Total actual cost across all jobs
    total_actual = sum(j.actual_total_cost for j in all_jobs)
    total_estimated = sum(j.estimated_total_cost for j in all_jobs)

    return {
        "total_wip_value": round(total_wip, 2),
        "average_margin_percent": round(avg_margin, 2),
        "jobs_over_budget": over_budget,
        "jobs_completed_this_month": completed_this_month,
        "total_jobs": len(all_jobs),
        "in_progress_count": len(in_progress),
        "completed_count": len(completed) + len(reviewed),
        "total_actual_cost": round(total_actual, 2),
        "total_estimated_cost": round(total_estimated, 2),
    }


@router.get("/{job_cost_id}", response_model=JobCostResponse)
def get_job_cost(
    job_cost_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get a single job cost with all details."""
    job_cost = access.resolve_job_cost(db, job_cost_id, company_id)

    return build_job_cost_response(job_cost)


@router.post("/", response_model=JobCostResponse)
def create_job_cost(
    data: JobCostCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Create a cost record for a live work order in the active company."""
    with access.job_cost_transaction(db):
        job_cost = access.create_job_cost(db, data, company_id, audit)
        job_cost_id = job_cost.id
    return build_job_cost_response(access.resolve_job_cost(db, job_cost_id, company_id))


@router.put("/{job_cost_id}", response_model=JobCostResponse)
def update_job_cost(
    job_cost_id: int,
    data: JobCostUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Update estimates, revenue, notes or status with required audit evidence."""
    with access.job_cost_transaction(db):
        access.update_job_cost(db, job_cost_id, data, company_id, audit)
    return build_job_cost_response(access.resolve_job_cost(db, job_cost_id, company_id))


@router.get("/{job_cost_id}/entries", response_model=List[CostEntryResponse])
def list_entries(
    job_cost_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """List active-company entries under an authorized job-cost parent."""
    job_cost = access.resolve_job_cost(db, job_cost_id, company_id)
    entries = (
        access.cost_entry_query(db, job_cost, company_id)
        .order_by(CostEntry.entry_date.desc(), CostEntry.created_at.desc())
        .all()
    )
    return [access.build_cost_entry_response(entry) for entry in entries]


@router.post("/{job_cost_id}/entries", response_model=CostEntryResponse)
def add_entry(
    job_cost_id: int,
    data: CostEntryCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Add an audited entry; operation references must belong to this work order."""
    with access.job_cost_transaction(db):
        entry_id = access.add_cost_entry(db, job_cost_id, data, company_id, current_user.id, audit).id
    job_cost = access.resolve_job_cost(db, job_cost_id, company_id)
    entry = access.cost_entry_query(db, job_cost, company_id).filter(CostEntry.id == entry_id).one()
    return access.build_cost_entry_response(entry)


@router.delete("/{job_cost_id}/entries/{entry_id}")
def delete_entry(
    job_cost_id: int,
    entry_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Delete one authorized entry and audit the corresponding total changes."""
    with access.job_cost_transaction(db):
        access.delete_cost_entry(db, job_cost_id, entry_id, company_id, audit)
    return {"detail": "Cost entry deleted"}


@router.post("/{job_cost_id}/calculate", response_model=JobCostResponse)
def calculate_costs(
    job_cost_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Regenerate labor using the shared configurable-rate cost service, atomically audited."""
    with access.job_cost_transaction(db):
        access.calculate_job_cost(db, job_cost_id, company_id, current_user.id, audit)
    return build_job_cost_response(access.resolve_job_cost(db, job_cost_id, company_id))


@router.get("/{job_cost_id}/variance-report")
def variance_report(
    job_cost_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get detailed variance breakdown within the active company."""
    job_cost = access.resolve_job_cost(db, job_cost_id, company_id)

    wo = job_cost.work_order

    # Group entries by type
    entries_by_type = {}
    for entry in job_cost.entries:
        entry_type = entry.entry_type.value if hasattr(entry.entry_type, 'value') else entry.entry_type
        if entry_type not in entries_by_type:
            entries_by_type[entry_type] = []
        entries_by_type[entry_type].append(
            {
                "id": entry.id,
                "description": entry.description,
                "quantity": entry.quantity,
                "unit_cost": entry.unit_cost,
                "total_cost": entry.total_cost,
                "source": entry.source.value if hasattr(entry.source, 'value') else entry.source,
                "reference": entry.reference,
                "entry_date": entry.entry_date.isoformat() if entry.entry_date else None,
            }
        )

    return {
        "job_cost_id": job_cost.id,
        "work_order_number": wo.work_order_number if wo else None,
        "customer_name": wo.customer_name if wo else None,
        "variance_summary": {
            "material": {
                "estimated": job_cost.estimated_material_cost,
                "actual": job_cost.actual_material_cost,
                "variance": job_cost.material_variance,
                "variance_percent": round(
                    (
                        (job_cost.material_variance / job_cost.estimated_material_cost * 100)
                        if job_cost.estimated_material_cost
                        else 0
                    ),
                    2,
                ),
            },
            "labor": {
                "estimated": job_cost.estimated_labor_cost,
                "actual": job_cost.actual_labor_cost,
                "variance": job_cost.labor_variance,
                "variance_percent": round(
                    (
                        (job_cost.labor_variance / job_cost.estimated_labor_cost * 100)
                        if job_cost.estimated_labor_cost
                        else 0
                    ),
                    2,
                ),
            },
            "overhead": {
                "estimated": job_cost.estimated_overhead_cost,
                "actual": job_cost.actual_overhead_cost,
                "variance": job_cost.overhead_variance,
                "variance_percent": round(
                    (
                        (job_cost.overhead_variance / job_cost.estimated_overhead_cost * 100)
                        if job_cost.estimated_overhead_cost
                        else 0
                    ),
                    2,
                ),
            },
            "total": {
                "estimated": job_cost.estimated_total_cost,
                "actual": job_cost.actual_total_cost,
                "variance": job_cost.total_variance,
                "variance_percent": round(
                    (
                        (job_cost.total_variance / job_cost.estimated_total_cost * 100)
                        if job_cost.estimated_total_cost
                        else 0
                    ),
                    2,
                ),
            },
        },
        "margin": {
            "revenue": job_cost.revenue,
            "total_cost": job_cost.actual_total_cost,
            "margin_amount": job_cost.margin_amount,
            "margin_percent": job_cost.margin_percent,
        },
        "entries_by_type": entries_by_type,
    }
