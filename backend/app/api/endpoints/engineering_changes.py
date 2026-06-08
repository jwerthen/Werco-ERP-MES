import json
from datetime import date, datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import and_, func
from sqlalchemy.orm import Session, joinedload

from app.api.deps import get_audit_service, get_current_company_id, get_current_user, require_role
from app.db.database import get_db
from app.models.engineering_change import (
    ECOApproval,
    ECOImplementationTask,
    ECOPriority,
    ECOStatus,
    ECOType,
    EngineeringChangeOrder,
)
from app.models.user import User, UserRole
from app.services.audit_service import AuditService

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
    last = (
        db.query(EngineeringChangeOrder)
        .filter(EngineeringChangeOrder.eco_number.like(f"{prefix}%"))
        .order_by(EngineeringChangeOrder.eco_number.desc())
        .first()
    )

    if last:
        num = int(last.eco_number.split("-")[-1]) + 1
    else:
        num = 1
    return f"{prefix}{num:03d}"


def get_eco_or_404(db: Session, eco_id: int, company_id: int) -> EngineeringChangeOrder:
    """Load an ECO by id, scoped to the active company.

    Tenant isolation (G4-Fix1): the lookup MUST filter ``company_id`` -- previously
    it matched on ``id`` alone, making every by-id ECO endpoint cross-tenant
    readable/mutable. A mismatch returns 404 (never reveal that another tenant's ECO
    exists).
    """
    eco = (
        db.query(EngineeringChangeOrder)
        .options(
            joinedload(EngineeringChangeOrder.requester),
            joinedload(EngineeringChangeOrder.assignee),
            joinedload(EngineeringChangeOrder.approver),
            joinedload(EngineeringChangeOrder.approvals).joinedload(ECOApproval.approver),
            joinedload(EngineeringChangeOrder.implementation_tasks).joinedload(ECOImplementationTask.assignee),
        )
        .filter(
            EngineeringChangeOrder.id == eco_id,
            EngineeringChangeOrder.company_id == company_id,
        )
        .first()
    )

    if not eco:
        raise HTTPException(status_code=404, detail="ECO not found")
    return eco


def _validate_affected_ids_in_company(db: Session, company_id: int, eco_in) -> None:
    """Reject any affected part/work-order/document id that is not in the active company.

    Tenant isolation (G4-Fix1): ``affected_parts``/``affected_work_orders``/
    ``affected_documents`` are free-form id lists persisted as JSON. Without this
    check a crafted payload could store another tenant's ids, which the
    affected-items endpoint would then resolve. Each referenced id must resolve to a
    live row WITHIN ``company_id`` (Part/WorkOrder are soft-deletable, so exclude
    deleted rows). Raises 422 on the first foreign/nonexistent id.
    """
    from app.models.document import Document
    from app.models.part import Part
    from app.models.work_order import WorkOrder

    def _check(model, ids, label, soft_delete):
        if not ids:
            return
        unique_ids = list({int(i) for i in ids})
        query = db.query(model.id).filter(model.id.in_(unique_ids), model.company_id == company_id)
        if soft_delete:
            query = query.filter(model.is_deleted == False)  # noqa: E712
        found = {row[0] for row in query.all()}
        missing = [i for i in unique_ids if i not in found]
        if missing:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown or cross-tenant {label} id(s): {sorted(missing)}",
            )

    _check(Part, eco_in.affected_parts, "part", soft_delete=True)
    _check(WorkOrder, eco_in.affected_work_orders, "work order", soft_delete=True)
    _check(Document, eco_in.affected_documents, "document", soft_delete=False)


# ============== CRUD Endpoints ==============


@router.get("/eco/dashboard", response_model=DashboardResponse)
def get_eco_dashboard(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get ECO dashboard statistics"""
    now = datetime.utcnow()
    first_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    pending_review = (
        db.query(func.count(EngineeringChangeOrder.id))
        .filter(
            EngineeringChangeOrder.company_id == company_id,
            EngineeringChangeOrder.status.in_([ECOStatus.SUBMITTED, ECOStatus.UNDER_REVIEW]),
        )
        .scalar()
    )

    in_implementation = (
        db.query(func.count(EngineeringChangeOrder.id))
        .filter(
            EngineeringChangeOrder.company_id == company_id,
            EngineeringChangeOrder.status == ECOStatus.IN_IMPLEMENTATION,
        )
        .scalar()
    )

    completed_this_month = (
        db.query(func.count(EngineeringChangeOrder.id))
        .filter(
            and_(
                EngineeringChangeOrder.company_id == company_id,
                EngineeringChangeOrder.status == ECOStatus.COMPLETED,
                EngineeringChangeOrder.completed_date >= first_of_month.date(),
            )
        )
        .scalar()
    )

    total_active = (
        db.query(func.count(EngineeringChangeOrder.id))
        .filter(
            EngineeringChangeOrder.company_id == company_id,
            EngineeringChangeOrder.status.notin_([ECOStatus.COMPLETED, ECOStatus.REJECTED, ECOStatus.CANCELLED]),
        )
        .scalar()
    )

    # By type breakdown
    type_counts = (
        db.query(EngineeringChangeOrder.eco_type, func.count(EngineeringChangeOrder.id))
        .filter(
            EngineeringChangeOrder.company_id == company_id,
            EngineeringChangeOrder.status.notin_([ECOStatus.CANCELLED]),
        )
        .group_by(EngineeringChangeOrder.eco_type)
        .all()
    )
    by_type = {str(t.value) if hasattr(t, 'value') else str(t): c for t, c in type_counts}

    # By priority breakdown
    priority_counts = (
        db.query(EngineeringChangeOrder.priority, func.count(EngineeringChangeOrder.id))
        .filter(
            EngineeringChangeOrder.company_id == company_id,
            EngineeringChangeOrder.status.notin_([ECOStatus.COMPLETED, ECOStatus.CANCELLED]),
        )
        .group_by(EngineeringChangeOrder.priority)
        .all()
    )
    by_priority = {str(p.value) if hasattr(p, 'value') else str(p): c for p, c in priority_counts}

    # Average cycle time for completed ECOs
    completed_ecos = (
        db.query(EngineeringChangeOrder)
        .filter(
            and_(
                EngineeringChangeOrder.company_id == company_id,
                EngineeringChangeOrder.status == ECOStatus.COMPLETED,
                EngineeringChangeOrder.completed_date.isnot(None),
            )
        )
        .all()
    )

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
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """List all ECOs with optional filters"""
    query = (
        db.query(EngineeringChangeOrder)
        .filter(EngineeringChangeOrder.company_id == company_id)
        .options(
            joinedload(EngineeringChangeOrder.requester),
            joinedload(EngineeringChangeOrder.assignee),
            joinedload(EngineeringChangeOrder.approver),
            joinedload(EngineeringChangeOrder.approvals).joinedload(ECOApproval.approver),
            joinedload(EngineeringChangeOrder.implementation_tasks).joinedload(ECOImplementationTask.assignee),
        )
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
            (EngineeringChangeOrder.eco_number.ilike(search_term)) | (EngineeringChangeOrder.title.ilike(search_term))
        )

    return query.order_by(EngineeringChangeOrder.created_at.desc()).offset(skip).limit(limit).all()


@router.get("/eco/{eco_id}", response_model=ECOResponse)
def get_eco(
    eco_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get ECO details"""
    return get_eco_or_404(db, eco_id, company_id)


@router.post("/eco/", response_model=ECOResponse)
def create_eco(
    eco_in: ECOCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Create a new Engineering Change Order"""
    # G4-Fix1: every affected id must resolve within the active company before persist,
    # so a crafted payload cannot store cross-tenant references.
    _validate_affected_ids_in_company(db, company_id, eco_in)

    data = eco_in.model_dump(exclude={"affected_parts", "affected_work_orders", "affected_documents"})

    eco = EngineeringChangeOrder(
        eco_number=generate_eco_number(db),
        **data,
        requested_by=current_user.id,
        affected_parts=json.dumps(eco_in.affected_parts) if eco_in.affected_parts else None,
        affected_work_orders=json.dumps(eco_in.affected_work_orders) if eco_in.affected_work_orders else None,
        affected_documents=json.dumps(eco_in.affected_documents) if eco_in.affected_documents else None,
    )
    eco.company_id = company_id
    db.add(eco)
    db.flush()  # assign PK without committing so the audit row carries resource_id

    audit.log_create("engineering_change_order", eco.id, eco.eco_number, new_values=eco)

    db.commit()
    db.refresh(eco)
    return get_eco_or_404(db, eco.id, company_id)


@router.put("/eco/{eco_id}", response_model=ECOResponse)
def update_eco(
    eco_id: int,
    eco_in: ECOUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Update an ECO"""
    eco = get_eco_or_404(db, eco_id, company_id)

    if eco.status in [ECOStatus.COMPLETED, ECOStatus.REJECTED, ECOStatus.CANCELLED]:
        raise HTTPException(status_code=400, detail="Cannot update a completed, rejected, or cancelled ECO")

    # G4-Fix1: validate affected ids against the active company before persisting.
    _validate_affected_ids_in_company(db, company_id, eco_in)

    old_values = {c.key: getattr(eco, c.key) for c in eco.__table__.columns}

    update_data = eco_in.model_dump(
        exclude_unset=True, exclude={"affected_parts", "affected_work_orders", "affected_documents"}
    )

    for field, value in update_data.items():
        setattr(eco, field, value)

    # Handle JSON list fields
    if eco_in.affected_parts is not None:
        eco.affected_parts = json.dumps(eco_in.affected_parts)
    if eco_in.affected_work_orders is not None:
        eco.affected_work_orders = json.dumps(eco_in.affected_work_orders)
    if eco_in.affected_documents is not None:
        eco.affected_documents = json.dumps(eco_in.affected_documents)

    db.flush()
    audit.log_update(
        resource_type="engineering_change_order",
        resource_id=eco.id,
        resource_identifier=eco.eco_number,
        old_values=old_values,
        new_values=eco,
    )

    db.commit()
    db.refresh(eco)
    return get_eco_or_404(db, eco.id, company_id)


# ============== Status Transition Endpoints ==============


@router.post("/eco/{eco_id}/submit", response_model=ECOResponse)
def submit_eco(
    eco_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Submit ECO for review"""
    eco = get_eco_or_404(db, eco_id, company_id)

    if eco.status != ECOStatus.DRAFT:
        raise HTTPException(status_code=400, detail="Only draft ECOs can be submitted")

    old_status = eco.status.value if eco.status else None
    eco.status = ECOStatus.SUBMITTED
    db.flush()
    audit.log_status_change(
        resource_type="engineering_change_order",
        resource_id=eco.id,
        resource_identifier=eco.eco_number,
        old_status=old_status,
        new_status=ECOStatus.SUBMITTED.value,
    )
    db.commit()
    db.refresh(eco)
    return get_eco_or_404(db, eco.id, company_id)


@router.post("/eco/{eco_id}/approve", response_model=ECOResponse)
def approve_eco(
    eco_id: int,
    decision: ApprovalDecision,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Add an approval decision to an ECO"""
    eco = get_eco_or_404(db, eco_id, company_id)

    if eco.status not in [ECOStatus.SUBMITTED, ECOStatus.UNDER_REVIEW]:
        raise HTTPException(status_code=400, detail="ECO is not pending approval")

    old_status = eco.status.value if eco.status else None

    # Find the user's pending approval record (the ECO is already company-verified,
    # so its child approvals are implicitly tenant-scoped via eco_id).
    approval = (
        db.query(ECOApproval)
        .filter(
            and_(
                ECOApproval.eco_id == eco_id,
                ECOApproval.approver_id == current_user.id,
                ECOApproval.status == "pending",
            )
        )
        .first()
    )

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

    db.flush()
    new_status = eco.status.value if eco.status else None
    if new_status != old_status:
        audit.log_status_change(
            resource_type="engineering_change_order",
            resource_id=eco.id,
            resource_identifier=eco.eco_number,
            old_status=old_status,
            new_status=new_status,
            description=f"ECO {eco.eco_number} approval decision '{decision.status}' by user {current_user.id}",
        )
    db.commit()
    return get_eco_or_404(db, eco.id, company_id)


@router.post("/eco/{eco_id}/reject", response_model=ECOResponse)
def reject_eco(
    eco_id: int,
    decision: ApprovalDecision,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Reject an ECO"""
    eco = get_eco_or_404(db, eco_id, company_id)

    if eco.status in [ECOStatus.COMPLETED, ECOStatus.CANCELLED]:
        raise HTTPException(status_code=400, detail="Cannot reject a completed or cancelled ECO")

    old_status = eco.status.value if eco.status else None
    eco.status = ECOStatus.REJECTED

    # Record the rejection as an approval record (tenant-tag it -- ECOApproval is a
    # TenantMixin model with a NOT NULL company_id).
    approval = ECOApproval(
        eco_id=eco_id,
        approver_id=current_user.id,
        role="Rejection",
        status="rejected",
        comments=decision.comments,
        decision_date=datetime.utcnow(),
    )
    approval.company_id = company_id
    db.add(approval)
    db.flush()
    audit.log_status_change(
        resource_type="engineering_change_order",
        resource_id=eco.id,
        resource_identifier=eco.eco_number,
        old_status=old_status,
        new_status=ECOStatus.REJECTED.value,
    )
    db.commit()
    return get_eco_or_404(db, eco.id, company_id)


@router.post("/eco/{eco_id}/implement", response_model=ECOResponse)
def start_implementation(
    eco_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Start implementation of an approved ECO"""
    eco = get_eco_or_404(db, eco_id, company_id)

    if eco.status != ECOStatus.APPROVED:
        raise HTTPException(status_code=400, detail="Only approved ECOs can be implemented")

    old_status = eco.status.value if eco.status else None
    eco.status = ECOStatus.IN_IMPLEMENTATION
    db.flush()
    audit.log_status_change(
        resource_type="engineering_change_order",
        resource_id=eco.id,
        resource_identifier=eco.eco_number,
        old_status=old_status,
        new_status=ECOStatus.IN_IMPLEMENTATION.value,
    )
    db.commit()
    return get_eco_or_404(db, eco.id, company_id)


@router.post("/eco/{eco_id}/complete", response_model=ECOResponse)
def complete_eco(
    eco_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Mark an ECO as completed"""
    eco = get_eco_or_404(db, eco_id, company_id)

    if eco.status != ECOStatus.IN_IMPLEMENTATION:
        raise HTTPException(status_code=400, detail="Only in-implementation ECOs can be completed")

    # Check if all tasks are completed or skipped (the ECO is company-verified, so its
    # child tasks are implicitly tenant-scoped via eco_id).
    incomplete_tasks = (
        db.query(ECOImplementationTask)
        .filter(
            and_(ECOImplementationTask.eco_id == eco_id, ECOImplementationTask.status.in_(["pending", "in_progress"]))
        )
        .count()
    )

    if incomplete_tasks > 0:
        raise HTTPException(status_code=400, detail=f"{incomplete_tasks} implementation task(s) still incomplete")

    old_status = eco.status.value if eco.status else None
    eco.status = ECOStatus.COMPLETED
    eco.completed_date = date.today()
    db.flush()
    audit.log_status_change(
        resource_type="engineering_change_order",
        resource_id=eco.id,
        resource_identifier=eco.eco_number,
        old_status=old_status,
        new_status=ECOStatus.COMPLETED.value,
    )
    db.commit()
    return get_eco_or_404(db, eco.id, company_id)


# ============== Approval Endpoints ==============


@router.get("/eco/{eco_id}/approvals", response_model=List[ApprovalResponse])
def list_approvals(
    eco_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """List all approvals for an ECO"""
    get_eco_or_404(db, eco_id, company_id)
    return db.query(ECOApproval).options(joinedload(ECOApproval.approver)).filter(ECOApproval.eco_id == eco_id).all()


@router.post("/eco/{eco_id}/approvals", response_model=ApprovalResponse)
def add_approval(
    eco_id: int,
    approval_in: ApprovalCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Add an approval requirement to an ECO"""
    get_eco_or_404(db, eco_id, company_id)

    # Check that the approver user exists WITHIN the active company (G4-Fix1: an
    # unscoped lookup let another tenant's user be attached as an approver).
    approver = db.query(User).filter(User.id == approval_in.approver_id, User.company_id == company_id).first()
    if not approver:
        raise HTTPException(status_code=404, detail="Approver user not found")

    approval = ECOApproval(
        eco_id=eco_id,
        approver_id=approval_in.approver_id,
        role=approval_in.role,
        status="pending",
    )
    approval.company_id = company_id
    db.add(approval)
    db.flush()
    audit.log_create(
        "eco_approval",
        approval.id,
        f"ECO {eco_id} approval ({approval_in.role})",
        new_values=approval,
    )
    db.commit()
    db.refresh(approval)
    return db.query(ECOApproval).options(joinedload(ECOApproval.approver)).filter(ECOApproval.id == approval.id).first()


# ============== Implementation Task Endpoints ==============


@router.post("/eco/{eco_id}/tasks", response_model=TaskResponse)
def add_task(
    eco_id: int,
    task_in: TaskCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Add an implementation task to an ECO"""
    get_eco_or_404(db, eco_id, company_id)

    # Determine next task number
    max_task = (
        db.query(func.max(ECOImplementationTask.task_number)).filter(ECOImplementationTask.eco_id == eco_id).scalar()
    )
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
    task.company_id = company_id
    db.add(task)
    db.flush()
    audit.log_create(
        "eco_implementation_task",
        task.id,
        f"ECO {eco_id} task #{next_num}",
        new_values=task,
    )
    db.commit()
    db.refresh(task)
    return (
        db.query(ECOImplementationTask)
        .options(joinedload(ECOImplementationTask.assignee))
        .filter(ECOImplementationTask.id == task.id)
        .first()
    )


@router.put("/eco/{eco_id}/tasks/{task_id}", response_model=TaskResponse)
def update_task(
    eco_id: int,
    task_id: int,
    task_in: TaskUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Update an implementation task"""
    # G4-Fix1: verify the parent ECO belongs to the active company (404 otherwise),
    # then scope the task query by company too -- previously this query filtered only
    # on id + eco_id, making any tenant's task mutable by id.
    get_eco_or_404(db, eco_id, company_id)
    task = (
        db.query(ECOImplementationTask)
        .filter(
            and_(
                ECOImplementationTask.id == task_id,
                ECOImplementationTask.eco_id == eco_id,
                ECOImplementationTask.company_id == company_id,
            )
        )
        .first()
    )

    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    old_values = {c.key: getattr(task, c.key) for c in task.__table__.columns}

    update_data = task_in.model_dump(exclude_unset=True)

    # Auto-set completed_date when marking as completed
    if "status" in update_data and update_data["status"] == "completed":
        task.completed_date = date.today()
    elif "status" in update_data and update_data["status"] in ["pending", "in_progress"]:
        task.completed_date = None

    for field, value in update_data.items():
        setattr(task, field, value)

    db.flush()
    audit.log_update(
        resource_type="eco_implementation_task",
        resource_id=task.id,
        resource_identifier=f"ECO {eco_id} task #{task.task_number}",
        old_values=old_values,
        new_values=task,
    )
    db.commit()
    db.refresh(task)
    return (
        db.query(ECOImplementationTask)
        .options(joinedload(ECOImplementationTask.assignee))
        .filter(ECOImplementationTask.id == task.id)
        .first()
    )


# ============== Affected Items Endpoint ==============


@router.get("/eco/affected-items/{eco_id}")
def get_affected_items(
    eco_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get details of all affected parts, work orders, and documents for an ECO"""
    eco = get_eco_or_404(db, eco_id, company_id)

    result = {
        "parts": [],
        "work_orders": [],
        "documents": [],
    }

    # Parse affected parts (G4-Fix1: scope to the active company and exclude
    # soft-deleted rows; previously this resolved ids across ALL tenants).
    if eco.affected_parts:
        try:
            part_ids = json.loads(eco.affected_parts)
            if part_ids:
                from app.models.part import Part

                parts = (
                    db.query(Part)
                    .filter(
                        Part.id.in_(part_ids),
                        Part.company_id == company_id,
                        Part.is_deleted == False,  # noqa: E712
                    )
                    .all()
                )
                result["parts"] = [
                    {"id": p.id, "part_number": p.part_number, "name": p.name, "revision": getattr(p, 'revision', None)}
                    for p in parts
                ]
        except (json.JSONDecodeError, TypeError):
            pass

    # Parse affected work orders (scoped + soft-delete filtered).
    if eco.affected_work_orders:
        try:
            wo_ids = json.loads(eco.affected_work_orders)
            if wo_ids:
                from app.models.work_order import WorkOrder

                wos = (
                    db.query(WorkOrder)
                    .filter(
                        WorkOrder.id.in_(wo_ids),
                        WorkOrder.company_id == company_id,
                        WorkOrder.is_deleted == False,  # noqa: E712
                    )
                    .all()
                )
                result["work_orders"] = [
                    {"id": w.id, "wo_number": w.work_order_number, "status": str(w.status.value) if w.status else None}
                    for w in wos
                ]
        except (json.JSONDecodeError, TypeError):
            pass

    # Parse affected documents (Document is TenantMixin only -- scope by company_id;
    # it has no is_deleted column, so no soft-delete filter applies).
    if eco.affected_documents:
        try:
            doc_ids = json.loads(eco.affected_documents)
            if doc_ids:
                from app.models.document import Document

                docs = db.query(Document).filter(Document.id.in_(doc_ids), Document.company_id == company_id).all()
                result["documents"] = [
                    {
                        "id": d.id,
                        "title": d.title,
                        "document_type": str(d.document_type.value) if d.document_type else None,
                    }
                    for d in docs
                ]
        except (json.JSONDecodeError, TypeError):
            pass

    return result
