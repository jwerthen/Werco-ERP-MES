from typing import List, Optional
from datetime import datetime, date
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func
from app.db.database import get_db
from app.api.deps import get_current_user, get_current_company_id
from app.models.user import User
from app.models.job_costing import JobCost, CostEntry, JobCostStatus, CostEntryType, CostEntrySource
from app.models.work_order import WorkOrder, WorkOrderOperation
from app.models.time_entry import TimeEntry
from pydantic import BaseModel

router = APIRouter()


# ── Pydantic Schemas ──────────────────────────────────────────────

class JobCostCreate(BaseModel):
    work_order_id: int
    estimated_material_cost: float = 0.0
    estimated_labor_cost: float = 0.0
    estimated_overhead_cost: float = 0.0
    revenue: float = 0.0
    notes: Optional[str] = None


class JobCostUpdate(BaseModel):
    estimated_material_cost: Optional[float] = None
    estimated_labor_cost: Optional[float] = None
    estimated_overhead_cost: Optional[float] = None
    revenue: Optional[float] = None
    status: Optional[str] = None
    notes: Optional[str] = None


class CostEntryCreate(BaseModel):
    entry_type: str  # material, labor, overhead, other
    description: str
    quantity: float = 1.0
    unit_cost: float = 0.0
    work_order_operation_id: Optional[int] = None
    source: str = "manual"
    reference: Optional[str] = None
    entry_date: date


class CostEntryResponse(BaseModel):
    id: int
    job_cost_id: int
    entry_type: str
    description: str
    quantity: float
    unit_cost: float
    total_cost: float
    work_order_operation_id: Optional[int] = None
    source: str
    reference: Optional[str] = None
    entry_date: date
    created_by: Optional[int] = None
    created_at: datetime

    class Config:
        from_attributes = True


class JobCostResponse(BaseModel):
    id: int
    work_order_id: int
    estimated_material_cost: float
    estimated_labor_cost: float
    estimated_overhead_cost: float
    estimated_total_cost: float
    actual_material_cost: float
    actual_labor_cost: float
    actual_overhead_cost: float
    actual_total_cost: float
    material_variance: float
    labor_variance: float
    overhead_variance: float
    total_variance: float
    margin_amount: float
    margin_percent: float
    revenue: float
    status: str
    notes: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    # Enriched fields from work order
    work_order_number: Optional[str] = None
    part_number: Optional[str] = None
    part_name: Optional[str] = None
    customer_name: Optional[str] = None

    class Config:
        from_attributes = True


# ── Helper Functions ──────────────────────────────────────────────

def recalculate_job_cost(job_cost: JobCost):
    """Recalculate totals, variances, and margin from entries and estimates."""
    # Sum actual costs from entries
    material_total = 0.0
    labor_total = 0.0
    overhead_total = 0.0
    other_total = 0.0

    for entry in job_cost.entries:
        if entry.entry_type == CostEntryType.MATERIAL or entry.entry_type == "material":
            material_total += entry.total_cost
        elif entry.entry_type == CostEntryType.LABOR or entry.entry_type == "labor":
            labor_total += entry.total_cost
        elif entry.entry_type == CostEntryType.OVERHEAD or entry.entry_type == "overhead":
            overhead_total += entry.total_cost
        else:
            other_total += entry.total_cost

    job_cost.actual_material_cost = material_total
    job_cost.actual_labor_cost = labor_total
    job_cost.actual_overhead_cost = overhead_total + other_total
    job_cost.actual_total_cost = material_total + labor_total + overhead_total + other_total

    # Estimated total
    job_cost.estimated_total_cost = (
        job_cost.estimated_material_cost
        + job_cost.estimated_labor_cost
        + job_cost.estimated_overhead_cost
    )

    # Variances
    job_cost.material_variance = job_cost.actual_material_cost - job_cost.estimated_material_cost
    job_cost.labor_variance = job_cost.actual_labor_cost - job_cost.estimated_labor_cost
    job_cost.overhead_variance = job_cost.actual_overhead_cost - job_cost.estimated_overhead_cost
    job_cost.total_variance = job_cost.actual_total_cost - job_cost.estimated_total_cost

    # Margin
    if job_cost.revenue and job_cost.revenue > 0:
        job_cost.margin_amount = job_cost.revenue - job_cost.actual_total_cost
        job_cost.margin_percent = (job_cost.margin_amount / job_cost.revenue) * 100
    else:
        job_cost.margin_amount = 0.0
        job_cost.margin_percent = 0.0


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
    query = db.query(JobCost).filter(JobCost.company_id == company_id).options(
        joinedload(JobCost.work_order).joinedload(WorkOrder.part),
        joinedload(JobCost.entries),
    )

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
    all_jobs = db.query(JobCost).filter(JobCost.company_id == company_id).options(
        joinedload(JobCost.entries),
    ).all()

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
    completed_this_month = len([
        j for j in (completed + reviewed)
        if j.updated_at and j.updated_at >= first_of_month
    ])

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
    job_cost = db.query(JobCost).options(
        joinedload(JobCost.work_order).joinedload(WorkOrder.part),
        joinedload(JobCost.entries),
    ).filter(JobCost.id == job_cost_id, JobCost.company_id == company_id).first()

    if not job_cost:
        raise HTTPException(status_code=404, detail="Job cost not found")

    return build_job_cost_response(job_cost)


@router.post("/", response_model=JobCostResponse)
def create_job_cost(
    data: JobCostCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Create a new job cost record for a work order."""
    # Verify work order exists
    wo = db.query(WorkOrder).filter(WorkOrder.id == data.work_order_id).first()
    if not wo:
        raise HTTPException(status_code=404, detail="Work order not found")

    # Check for existing job cost on this work order
    existing = db.query(JobCost).filter(JobCost.work_order_id == data.work_order_id).first()
    if existing:
        raise HTTPException(status_code=400, detail="Job cost already exists for this work order")

    estimated_total = data.estimated_material_cost + data.estimated_labor_cost + data.estimated_overhead_cost

    job_cost = JobCost(
        work_order_id=data.work_order_id,
        estimated_material_cost=data.estimated_material_cost,
        estimated_labor_cost=data.estimated_labor_cost,
        estimated_overhead_cost=data.estimated_overhead_cost,
        estimated_total_cost=estimated_total,
        revenue=data.revenue,
        notes=data.notes,
    )

    # Calculate initial margin if revenue provided
    if data.revenue and data.revenue > 0:
        job_cost.margin_amount = data.revenue
        job_cost.margin_percent = 100.0

    job_cost.company_id = company_id
    db.add(job_cost)
    db.commit()
    db.refresh(job_cost)

    # Reload with relationships
    job_cost = db.query(JobCost).options(
        joinedload(JobCost.work_order).joinedload(WorkOrder.part),
        joinedload(JobCost.entries),
    ).filter(JobCost.id == job_cost.id).first()

    return build_job_cost_response(job_cost)


@router.put("/{job_cost_id}", response_model=JobCostResponse)
def update_job_cost(
    job_cost_id: int,
    data: JobCostUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Update a job cost record."""
    job_cost = db.query(JobCost).options(
        joinedload(JobCost.work_order).joinedload(WorkOrder.part),
        joinedload(JobCost.entries),
    ).filter(JobCost.id == job_cost_id).first()

    if not job_cost:
        raise HTTPException(status_code=404, detail="Job cost not found")

    update_data = data.model_dump(exclude_unset=True)
    if "status" in update_data:
        update_data["status"] = JobCostStatus(update_data["status"])

    for field, value in update_data.items():
        setattr(job_cost, field, value)

    recalculate_job_cost(job_cost)

    db.commit()
    db.refresh(job_cost)

    # Reload with relationships
    job_cost = db.query(JobCost).options(
        joinedload(JobCost.work_order).joinedload(WorkOrder.part),
        joinedload(JobCost.entries),
    ).filter(JobCost.id == job_cost.id).first()

    return build_job_cost_response(job_cost)


@router.get("/{job_cost_id}/entries", response_model=List[CostEntryResponse])
def list_entries(
    job_cost_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all cost entries for a job cost."""
    job_cost = db.query(JobCost).filter(JobCost.id == job_cost_id).first()
    if not job_cost:
        raise HTTPException(status_code=404, detail="Job cost not found")

    entries = db.query(CostEntry).filter(
        CostEntry.job_cost_id == job_cost_id
    ).order_by(CostEntry.entry_date.desc(), CostEntry.created_at.desc()).all()

    result = []
    for e in entries:
        result.append({
            "id": e.id,
            "job_cost_id": e.job_cost_id,
            "entry_type": e.entry_type.value if hasattr(e.entry_type, 'value') else e.entry_type,
            "description": e.description,
            "quantity": e.quantity,
            "unit_cost": e.unit_cost,
            "total_cost": e.total_cost,
            "work_order_operation_id": e.work_order_operation_id,
            "source": e.source.value if hasattr(e.source, 'value') else e.source,
            "reference": e.reference,
            "entry_date": e.entry_date,
            "created_by": e.created_by,
            "created_at": e.created_at,
        })
    return result


@router.post("/{job_cost_id}/entries", response_model=CostEntryResponse)
def add_entry(
    job_cost_id: int,
    data: CostEntryCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Add a cost entry to a job cost."""
    job_cost = db.query(JobCost).options(
        joinedload(JobCost.entries),
        joinedload(JobCost.work_order).joinedload(WorkOrder.part),
    ).filter(JobCost.id == job_cost_id).first()
    if not job_cost:
        raise HTTPException(status_code=404, detail="Job cost not found")

    total_cost = data.quantity * data.unit_cost

    entry = CostEntry(
        job_cost_id=job_cost_id,
        entry_type=CostEntryType(data.entry_type),
        description=data.description,
        quantity=data.quantity,
        unit_cost=data.unit_cost,
        total_cost=total_cost,
        work_order_operation_id=data.work_order_operation_id,
        source=CostEntrySource(data.source),
        reference=data.reference,
        entry_date=data.entry_date,
        created_by=current_user.id,
    )

    db.add(entry)
    db.flush()

    # Recalculate job cost totals
    job_cost.entries.append(entry)
    recalculate_job_cost(job_cost)

    db.commit()
    db.refresh(entry)

    return {
        "id": entry.id,
        "job_cost_id": entry.job_cost_id,
        "entry_type": entry.entry_type.value if hasattr(entry.entry_type, 'value') else entry.entry_type,
        "description": entry.description,
        "quantity": entry.quantity,
        "unit_cost": entry.unit_cost,
        "total_cost": entry.total_cost,
        "work_order_operation_id": entry.work_order_operation_id,
        "source": entry.source.value if hasattr(entry.source, 'value') else entry.source,
        "reference": entry.reference,
        "entry_date": entry.entry_date,
        "created_by": entry.created_by,
        "created_at": entry.created_at,
    }


@router.delete("/{job_cost_id}/entries/{entry_id}")
def delete_entry(
    job_cost_id: int,
    entry_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete a cost entry."""
    entry = db.query(CostEntry).filter(
        CostEntry.id == entry_id,
        CostEntry.job_cost_id == job_cost_id,
    ).first()
    if not entry:
        raise HTTPException(status_code=404, detail="Cost entry not found")

    db.delete(entry)
    db.flush()

    # Recalculate job cost totals
    job_cost = db.query(JobCost).options(
        joinedload(JobCost.entries),
    ).filter(JobCost.id == job_cost_id).first()
    if job_cost:
        recalculate_job_cost(job_cost)

    db.commit()
    return {"detail": "Cost entry deleted"}


@router.post("/{job_cost_id}/calculate", response_model=JobCostResponse)
def calculate_costs(
    job_cost_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Recalculate actual costs from time entries and existing cost entries."""
    job_cost = db.query(JobCost).options(
        joinedload(JobCost.work_order).joinedload(WorkOrder.part),
        joinedload(JobCost.entries),
    ).filter(JobCost.id == job_cost_id).first()

    if not job_cost:
        raise HTTPException(status_code=404, detail="Job cost not found")

    # Pull time entries for this work order and create labor cost entries
    time_entries = db.query(TimeEntry).filter(
        TimeEntry.work_order_id == job_cost.work_order_id,
        TimeEntry.clock_out.isnot(None),
    ).all()

    # Remove existing auto-generated labor entries (from time entries)
    existing_auto = db.query(CostEntry).filter(
        CostEntry.job_cost_id == job_cost_id,
        CostEntry.source == CostEntrySource.TIME_ENTRY,
    ).all()
    for e in existing_auto:
        db.delete(e)
    db.flush()

    # Default labor rate (could be made configurable)
    labor_rate = 45.0

    for te in time_entries:
        if te.duration_hours and te.duration_hours > 0:
            entry = CostEntry(
                job_cost_id=job_cost_id,
                entry_type=CostEntryType.LABOR,
                description=f"Labor - {te.entry_type.value if hasattr(te.entry_type, 'value') else te.entry_type}",
                quantity=te.duration_hours,
                unit_cost=labor_rate,
                total_cost=te.duration_hours * labor_rate,
                work_order_operation_id=te.operation_id,
                source=CostEntrySource.TIME_ENTRY,
                reference=f"TE-{te.id}",
                entry_date=te.clock_in.date() if te.clock_in else date.today(),
                created_by=current_user.id,
            )
            db.add(entry)

    db.flush()

    # Reload entries and recalculate
    job_cost = db.query(JobCost).options(
        joinedload(JobCost.work_order).joinedload(WorkOrder.part),
        joinedload(JobCost.entries),
    ).filter(JobCost.id == job_cost_id).first()

    recalculate_job_cost(job_cost)
    db.commit()

    return build_job_cost_response(job_cost)


@router.get("/{job_cost_id}/variance-report")
def variance_report(
    job_cost_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get detailed variance breakdown for a job cost."""
    job_cost = db.query(JobCost).options(
        joinedload(JobCost.work_order).joinedload(WorkOrder.part),
        joinedload(JobCost.entries),
    ).filter(JobCost.id == job_cost_id).first()

    if not job_cost:
        raise HTTPException(status_code=404, detail="Job cost not found")

    wo = job_cost.work_order

    # Group entries by type
    entries_by_type = {}
    for entry in job_cost.entries:
        entry_type = entry.entry_type.value if hasattr(entry.entry_type, 'value') else entry.entry_type
        if entry_type not in entries_by_type:
            entries_by_type[entry_type] = []
        entries_by_type[entry_type].append({
            "id": entry.id,
            "description": entry.description,
            "quantity": entry.quantity,
            "unit_cost": entry.unit_cost,
            "total_cost": entry.total_cost,
            "source": entry.source.value if hasattr(entry.source, 'value') else entry.source,
            "reference": entry.reference,
            "entry_date": entry.entry_date.isoformat() if entry.entry_date else None,
        })

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
                    (job_cost.material_variance / job_cost.estimated_material_cost * 100)
                    if job_cost.estimated_material_cost else 0, 2
                ),
            },
            "labor": {
                "estimated": job_cost.estimated_labor_cost,
                "actual": job_cost.actual_labor_cost,
                "variance": job_cost.labor_variance,
                "variance_percent": round(
                    (job_cost.labor_variance / job_cost.estimated_labor_cost * 100)
                    if job_cost.estimated_labor_cost else 0, 2
                ),
            },
            "overhead": {
                "estimated": job_cost.estimated_overhead_cost,
                "actual": job_cost.actual_overhead_cost,
                "variance": job_cost.overhead_variance,
                "variance_percent": round(
                    (job_cost.overhead_variance / job_cost.estimated_overhead_cost * 100)
                    if job_cost.estimated_overhead_cost else 0, 2
                ),
            },
            "total": {
                "estimated": job_cost.estimated_total_cost,
                "actual": job_cost.actual_total_cost,
                "variance": job_cost.total_variance,
                "variance_percent": round(
                    (job_cost.total_variance / job_cost.estimated_total_cost * 100)
                    if job_cost.estimated_total_cost else 0, 2
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
