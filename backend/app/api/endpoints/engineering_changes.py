from typing import List, Optional
from datetime import datetime, date
from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func, and_
import json

from app.db.database import get_db
from app.api.deps import get_current_user
from app.models.user import User
from app.models.engineering_change import (
    EngineeringChangeOrder, ECOApproval, ECOImplementationTask,
    ECOStatus, ECOPriority, ECOType,
)

router = APIRouter()


# ============== Pydantic Schemas ==============

class ECOCreate(BaseModel):
    title: str = Field(..., min_length=3, max_length=255)
    description: str = Field(..., min_length=5)
    eco_type: ECOType
    priority: ECOPriority = ECOPriority.MEDIUM
    reason_for_change: str = Field(..., min_length=5)
    proposed_solution: Optional[str] = None
    impact_analysis: Optional[str] = None
    risk_assessment: Optional[str] = None
    affected_parts: Optional[List[int]] = None
    affected_work_orders: Optional[List[int]] = None
    affected_documents: Optional[List[int]] = None
    estimated_cost: float = 0
    effectivity_type: Optional[str] = None
    effectivity_date: Optional[date] = None
    effectivity_serial: Optional[str] = None
    assigned_to: Optional[int] = None
    target_date: Optional[date] = None


class ECOUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=3, max_length=255)
    description: Optional[str] = None
    eco_type: Optional[ECOType] = None
    priority: Optional[ECOPriority] = None
    reason_for_change: Optional[str] = None
    proposed_solution: Optional[str] = None
    impact_analysis: Optional[str] = None
    risk_assessment: Optional[str] = None
    affected_parts: Optional[List[int]] = None
    affected_work_orders: Optional[List[int]] = None
    affected_documents: Optional[List[int]] = None
    estimated_cost: Optional[float] = None
    actual_cost: Optional[float] = None
    effectivity_type: Optional[str] = None
    effectivity_date: Optional[date] = None
    effectivity_serial: Optional[str] = None
    assigned_to: Optional[int] = None
    target_date: Optional[date] = None


class ApprovalCreate(BaseModel):
    approver_id: int
    role: str = Field(..., min_length=1, max_length=100)


class ApprovalDecision(BaseModel):
    status: str = Field(..., pattern="^(approved|rejected)$")
    comments: Optional[str] = None


class TaskCreate(BaseModel):
    description: str = Field(..., min_length=3)
    department: Optional[str] = None
    assigned_to: Optional[int] = None
    due_date: Optional[date] = None


class TaskUpdate(BaseModel):
    description: Optional[str] = None
    department: Optional[str] = None
    assigned_to: Optional[int] = None
    status: Optional[str] = Field(None, pattern="^(pending|in_progress|completed|skipped)$")
    due_date: Optional[date] = None
    notes: Optional[str] = None


class UserSummary(BaseModel):
    id: int
    first_name: str
    last_name: str
    email: str

    class Config:
        from_attributes = True


class ApprovalResponse(BaseModel):
    id: int
    eco_id: int
    approver_id: int
    approver: Optional[UserSummary] = None
    role: str
    status: str
    comments: Optional[str]
    decision_date: Optional[datetime]
    created_at: datetime

    class Config:
        from_attributes = True


class TaskResponse(BaseModel):
    id: int
    eco_id: int
    task_number: int
    description: str
    department: Optional[str]
    assigned_to: Optional[int]
    assignee: Optional[UserSummary] = None
    status: str
    due_date: Optional[date]
    completed_date: Optional[date]
    notes: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True


class ECOResponse(BaseModel):
    id: int
    eco_number: str
    title: str
    description: str
    eco_type: ECOType
    priority: ECOPriority
    status: ECOStatus
    reason_for_change: str
    proposed_solution: Optional[str]
    impact_analysis: Optional[str]
    risk_assessment: Optional[str]
    affected_parts: Optional[str]
    affected_work_orders: Optional[str]
    affected_documents: Optional[str]
    estimated_cost: float
    actual_cost: float
    effectivity_type: Optional[str]
    effectivity_date: Optional[date]
    effectivity_serial: Optional[str]
    requested_by: int
    requester: Optional[UserSummary] = None
    assigned_to: Optional[int]
    assignee: Optional[UserSummary] = None
    approved_by: Optional[int]
    approver: Optional[UserSummary] = None
    approved_date: Optional[datetime]
    target_date: Optional[date]
    completed_date: Optional[date]
    created_at: datetime
    updated_at: datetime
    approvals: List[ApprovalResponse] = []
    implementation_tasks: List[TaskResponse] = []

    class Config:
        from_attributes = True


class DashboardResponse(BaseModel):
    pending_review: int
    in_implementation: int
    completed_this_month: int
    total_active: int
    by_type: dict
    by_priority: dict
    avg_cycle_time_days: Optional[float]


# ============== Helper Functions ==============

def generate_eco_number(db: Session) -> str:
    today = datetime.now().strftime("%Y%m%d")
    prefix = f"ECO-{today}-"
    last = db.query(EngineeringChangeOrder).filter(
        EngineeringChangeOrder.eco_number.like(f"{prefix}%")
    ).order_by(EngineeringChangeOrder.eco_number.desc()).first()

    if last:
        num = int(last.eco_number.split("-")[-1]) + 1
    else:
        num = 1
    return f"{prefix}{num:03d}"


def get_eco_or_404(db: Session, eco_id: int) -> EngineeringChangeOrder:
    eco = db.query(EngineeringChangeOrder).options(
        joinedload(EngineeringChangeOrder.requester),
        joinedload(EngineeringChangeOrder.assignee),
        joinedload(EngineeringChangeOrder.approver),
        joinedload(EngineeringChangeOrder.approvals).joinedload(ECOApproval.approver),
        joinedload(EngineeringChangeOrder.implementation_tasks).joinedload(ECOImplementationTask.assignee),
    ).filter(EngineeringChangeOrder.id == eco_id).first()

    if not eco:
        raise HTTPException(status_code=404, detail="ECO not found")
    return eco


# ============== CRUD Endpoints ==============

@router.get("/eco/dashboard", response_model=DashboardResponse)
def get_eco_dashboard(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get ECO dashboard statistics"""
    now = datetime.utcnow()
    first_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    pending_review = db.query(func.count(EngineeringChangeOrder.id)).filter(
        EngineeringChangeOrder.status.in_([ECOStatus.SUBMITTED, ECOStatus.UNDER_REVIEW])
    ).scalar()

    in_implementation = db.query(func.count(EngineeringChangeOrder.id)).filter(
        EngineeringChangeOrder.status == ECOStatus.IN_IMPLEMENTATION
    ).scalar()

    completed_this_month = db.query(func.count(EngineeringChangeOrder.id)).filter(
        and_(
            EngineeringChangeOrder.status == ECOStatus.COMPLETED,
            EngineeringChangeOrder.completed_date >= first_of_month.date()
        )
    ).scalar()

    total_active = db.query(func.count(EngineeringChangeOrder.id)).filter(
        EngineeringChangeOrder.status.notin_([ECOStatus.COMPLETED, ECOStatus.REJECTED, ECOStatus.CANCELLED])
    ).scalar()

    # By type breakdown
    type_counts = db.query(
        EngineeringChangeOrder.eco_type, func.count(EngineeringChangeOrder.id)
    ).filter(
        EngineeringChangeOrder.status.notin_([ECOStatus.CANCELLED])
    ).group_by(EngineeringChangeOrder.eco_type).all()
    by_type = {str(t.value) if hasattr(t, 'value') else str(t): c for t, c in type_counts}

    # By priority breakdown
    priority_counts = db.query(
        EngineeringChangeOrder.priority, func.count(EngineeringChangeOrder.id)
    ).filter(
        EngineeringChangeOrder.status.notin_([ECOStatus.COMPLETED, ECOStatus.CANCELLED])
    ).group_by(EngineeringChangeOrder.priority).all()
    by_priority = {str(p.value) if hasattr(p, 'value') else str(p): c for p, c in priority_counts}

    # Average cycle time for completed ECOs
    completed_ecos = db.query(EngineeringChangeOrder).filter(
        and_(
            EngineeringChangeOrder.status == ECOStatus.COMPLETED,
            EngineeringChangeOrder.completed_date.isnot(None)
        )
    ).all()

    avg_cycle_time = None
    if completed_ecos:
        total_days = 0
        count = 0
        for eco in completed_ecos:
            if eco.completed_date and eco.created_at:
                delta = datetime.combine(eco.completed_date, datetime.min.time()) - eco.created_at
                total_days += delta.days
                count += 1
        if count > 0:
            avg_cycle_time = round(total_days / count, 1)

    return DashboardResponse(
        pending_review=pending_review or 0,
        in_implementation=in_implementation or 0,
        completed_this_month=completed_this_month or 0,
        total_active=total_active or 0,
        by_type=by_type,
        by_priority=by_priority,
        avg_cycle_time_days=avg_cycle_time,
    )


@router.get("/eco/", response_model=List[ECOResponse])
def list_ecos(
    skip: int = 0,
    limit: int = 100,
    status: Optional[ECOStatus] = None,
    eco_type: Optional[ECOType] = None,
    priority: Optional[ECOPriority] = None,
    search: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all ECOs with optional filters"""
    query = db.query(EngineeringChangeOrder).options(
        joinedload(EngineeringChangeOrder.requester),
        joinedload(EngineeringChangeOrder.assignee),
        joinedload(EngineeringChangeOrder.approver),
        joinedload(EngineeringChangeOrder.approvals).joinedload(ECOApproval.approver),
        joinedload(EngineeringChangeOrder.implementation_tasks).joinedload(ECOImplementationTask.assignee),
    )

    if status:
        query = query.filter(EngineeringChangeOrder.status == status)
    if eco_type:
        query = query.filter(EngineeringChangeOrder.eco_type == eco_type)
    if priority:
        query = query.filter(EngineeringChangeOrder.priority == priority)
    if search:
        search_term = f"%{search}%"
        query = query.filter(
            (EngineeringChangeOrder.eco_number.ilike(search_term)) |
            (EngineeringChangeOrder.title.ilike(search_term))
        )

    return query.order_by(EngineeringChangeOrder.created_at.desc()).offset(skip).limit(limit).all()


@router.get("/eco/{eco_id}", response_model=ECOResponse)
def get_eco(
    eco_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get ECO details"""
    return get_eco_or_404(db, eco_id)


@router.post("/eco/", response_model=ECOResponse)
def create_eco(
    eco_in: ECOCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a new Engineering Change Order"""
    data = eco_in.model_dump(exclude={"affected_parts", "affected_work_orders", "affected_documents"})

    eco = EngineeringChangeOrder(
        eco_number=generate_eco_number(db),
        **data,
        requested_by=current_user.id,
        affected_parts=json.dumps(eco_in.affected_parts) if eco_in.affected_parts else None,
        affected_work_orders=json.dumps(eco_in.affected_work_orders) if eco_in.affected_work_orders else None,
        affected_documents=json.dumps(eco_in.affected_documents) if eco_in.affected_documents else None,
    )
    db.add(eco)
    db.commit()
    db.refresh(eco)
    return get_eco_or_404(db, eco.id)


@router.put("/eco/{eco_id}", response_model=ECOResponse)
def update_eco(
    eco_id: int,
    eco_in: ECOUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update an ECO"""
    eco = get_eco_or_404(db, eco_id)

    if eco.status in [ECOStatus.COMPLETED, ECOStatus.REJECTED, ECOStatus.CANCELLED]:
        raise HTTPException(status_code=400, detail="Cannot update a completed, rejected, or cancelled ECO")

    update_data = eco_in.model_dump(exclude_unset=True, exclude={"affected_parts", "affected_work_orders", "affected_documents"})

    for field, value in update_data.items():
        setattr(eco, field, value)

    # Handle JSON list fields
    if eco_in.affected_parts is not None:
        eco.affected_parts = json.dumps(eco_in.affected_parts)
    if eco_in.affected_work_orders is not None:
        eco.affected_work_orders = json.dumps(eco_in.affected_work_orders)
    if eco_in.affected_documents is not None:
        eco.affected_documents = json.dumps(eco_in.affected_documents)

    db.commit()
    db.refresh(eco)
    return get_eco_or_404(db, eco.id)


# ============== Status Transition Endpoints ==============

@router.post("/eco/{eco_id}/submit", response_model=ECOResponse)
def submit_eco(
    eco_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Submit ECO for review"""
    eco = get_eco_or_404(db, eco_id)

    if eco.status != ECOStatus.DRAFT:
        raise HTTPException(status_code=400, detail="Only draft ECOs can be submitted")

    eco.status = ECOStatus.SUBMITTED
    db.commit()
    db.refresh(eco)
    return get_eco_or_404(db, eco.id)


@router.post("/eco/{eco_id}/approve", response_model=ECOResponse)
def approve_eco(
    eco_id: int,
    decision: ApprovalDecision,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Add an approval decision to an ECO"""
    eco = get_eco_or_404(db, eco_id)

    if eco.status not in [ECOStatus.SUBMITTED, ECOStatus.UNDER_REVIEW]:
        raise HTTPException(status_code=400, detail="ECO is not pending approval")

    # Find the user's pending approval record
    approval = db.query(ECOApproval).filter(
        and_(
            ECOApproval.eco_id == eco_id,
            ECOApproval.approver_id == current_user.id,
            ECOApproval.status == "pending"
        )
    ).first()

    if not approval:
        raise HTTPException(status_code=400, detail="No pending approval found for this user")

    approval.status = decision.status
    approval.comments = decision.comments
    approval.decision_date = datetime.utcnow()

    # Update ECO status to under_review if first approval action
    if eco.status == ECOStatus.SUBMITTED:
        eco.status = ECOStatus.UNDER_REVIEW

    # Check if all approvals are granted
    all_approvals = db.query(ECOApproval).filter(ECOApproval.eco_id == eco_id).all()
    pending = [a for a in all_approvals if a.status == "pending"]
    rejected = [a for a in all_approvals if a.status == "rejected"]

    if rejected:
        eco.status = ECOStatus.REJECTED
    elif not pending:
        # All approved
        eco.status = ECOStatus.APPROVED
        eco.approved_by = current_user.id
        eco.approved_date = datetime.utcnow()

    db.commit()
    return get_eco_or_404(db, eco.id)


@router.post("/eco/{eco_id}/reject", response_model=ECOResponse)
def reject_eco(
    eco_id: int,
    decision: ApprovalDecision,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Reject an ECO"""
    eco = get_eco_or_404(db, eco_id)

    if eco.status in [ECOStatus.COMPLETED, ECOStatus.CANCELLED]:
        raise HTTPException(status_code=400, detail="Cannot reject a completed or cancelled ECO")

    eco.status = ECOStatus.REJECTED

    # Record the rejection as an approval record
    approval = ECOApproval(
        eco_id=eco_id,
        approver_id=current_user.id,
        role="Rejection",
        status="rejected",
        comments=decision.comments,
        decision_date=datetime.utcnow(),
    )
    db.add(approval)
    db.commit()
    return get_eco_or_404(db, eco.id)


@router.post("/eco/{eco_id}/implement", response_model=ECOResponse)
def start_implementation(
    eco_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Start implementation of an approved ECO"""
    eco = get_eco_or_404(db, eco_id)

    if eco.status != ECOStatus.APPROVED:
        raise HTTPException(status_code=400, detail="Only approved ECOs can be implemented")

    eco.status = ECOStatus.IN_IMPLEMENTATION
    db.commit()
    return get_eco_or_404(db, eco.id)


@router.post("/eco/{eco_id}/complete", response_model=ECOResponse)
def complete_eco(
    eco_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Mark an ECO as completed"""
    eco = get_eco_or_404(db, eco_id)

    if eco.status != ECOStatus.IN_IMPLEMENTATION:
        raise HTTPException(status_code=400, detail="Only in-implementation ECOs can be completed")

    # Check if all tasks are completed or skipped
    incomplete_tasks = db.query(ECOImplementationTask).filter(
        and_(
            ECOImplementationTask.eco_id == eco_id,
            ECOImplementationTask.status.in_(["pending", "in_progress"])
        )
    ).count()

    if incomplete_tasks > 0:
        raise HTTPException(
            status_code=400,
            detail=f"{incomplete_tasks} implementation task(s) still incomplete"
        )

    eco.status = ECOStatus.COMPLETED
    eco.completed_date = date.today()
    db.commit()
    return get_eco_or_404(db, eco.id)


# ============== Approval Endpoints ==============

@router.get("/eco/{eco_id}/approvals", response_model=List[ApprovalResponse])
def list_approvals(
    eco_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all approvals for an ECO"""
    eco = get_eco_or_404(db, eco_id)
    return db.query(ECOApproval).options(
        joinedload(ECOApproval.approver)
    ).filter(ECOApproval.eco_id == eco_id).all()


@router.post("/eco/{eco_id}/approvals", response_model=ApprovalResponse)
def add_approval(
    eco_id: int,
    approval_in: ApprovalCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Add an approval requirement to an ECO"""
    eco = get_eco_or_404(db, eco_id)

    # Check that approver user exists
    approver = db.query(User).filter(User.id == approval_in.approver_id).first()
    if not approver:
        raise HTTPException(status_code=404, detail="Approver user not found")

    approval = ECOApproval(
        eco_id=eco_id,
        approver_id=approval_in.approver_id,
        role=approval_in.role,
        status="pending",
    )
    db.add(approval)
    db.commit()
    db.refresh(approval)
    return db.query(ECOApproval).options(
        joinedload(ECOApproval.approver)
    ).filter(ECOApproval.id == approval.id).first()


# ============== Implementation Task Endpoints ==============

@router.post("/eco/{eco_id}/tasks", response_model=TaskResponse)
def add_task(
    eco_id: int,
    task_in: TaskCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Add an implementation task to an ECO"""
    eco = get_eco_or_404(db, eco_id)

    # Determine next task number
    max_task = db.query(func.max(ECOImplementationTask.task_number)).filter(
        ECOImplementationTask.eco_id == eco_id
    ).scalar()
    next_num = (max_task or 0) + 1

    task = ECOImplementationTask(
        eco_id=eco_id,
        task_number=next_num,
        description=task_in.description,
        department=task_in.department,
        assigned_to=task_in.assigned_to,
        due_date=task_in.due_date,
        status="pending",
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return db.query(ECOImplementationTask).options(
        joinedload(ECOImplementationTask.assignee)
    ).filter(ECOImplementationTask.id == task.id).first()


@router.put("/eco/{eco_id}/tasks/{task_id}", response_model=TaskResponse)
def update_task(
    eco_id: int,
    task_id: int,
    task_in: TaskUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update an implementation task"""
    task = db.query(ECOImplementationTask).filter(
        and_(ECOImplementationTask.id == task_id, ECOImplementationTask.eco_id == eco_id)
    ).first()

    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    update_data = task_in.model_dump(exclude_unset=True)

    # Auto-set completed_date when marking as completed
    if "status" in update_data and update_data["status"] == "completed":
        task.completed_date = date.today()
    elif "status" in update_data and update_data["status"] in ["pending", "in_progress"]:
        task.completed_date = None

    for field, value in update_data.items():
        setattr(task, field, value)

    db.commit()
    db.refresh(task)
    return db.query(ECOImplementationTask).options(
        joinedload(ECOImplementationTask.assignee)
    ).filter(ECOImplementationTask.id == task.id).first()


# ============== Affected Items Endpoint ==============

@router.get("/eco/affected-items/{eco_id}")
def get_affected_items(
    eco_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get details of all affected parts, work orders, and documents for an ECO"""
    eco = get_eco_or_404(db, eco_id)

    result = {
        "parts": [],
        "work_orders": [],
        "documents": [],
    }

    # Parse affected parts
    if eco.affected_parts:
        try:
            part_ids = json.loads(eco.affected_parts)
            if part_ids:
                from app.models.part import Part
                parts = db.query(Part).filter(Part.id.in_(part_ids)).all()
                result["parts"] = [
                    {"id": p.id, "part_number": p.part_number, "name": p.name, "revision": getattr(p, 'revision', None)}
                    for p in parts
                ]
        except (json.JSONDecodeError, TypeError):
            pass

    # Parse affected work orders
    if eco.affected_work_orders:
        try:
            wo_ids = json.loads(eco.affected_work_orders)
            if wo_ids:
                from app.models.work_order import WorkOrder
                wos = db.query(WorkOrder).filter(WorkOrder.id.in_(wo_ids)).all()
                result["work_orders"] = [
                    {"id": w.id, "wo_number": w.wo_number, "status": str(w.status.value) if w.status else None}
                    for w in wos
                ]
        except (json.JSONDecodeError, TypeError):
            pass

    # Parse affected documents
    if eco.affected_documents:
        try:
            doc_ids = json.loads(eco.affected_documents)
            if doc_ids:
                from app.models.document import Document
                docs = db.query(Document).filter(Document.id.in_(doc_ids)).all()
                result["documents"] = [
                    {"id": d.id, "title": d.title, "document_type": str(d.document_type.value) if d.document_type else None}
                    for d in docs
                ]
        except (json.JSONDecodeError, TypeError):
            pass

    return result
