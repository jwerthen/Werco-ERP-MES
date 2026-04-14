from typing import List, Optional
from datetime import datetime, date, timedelta
from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func, extract
from app.db.database import get_db
from app.api.deps import get_current_user, require_role, get_current_company_id
from app.models.user import User, UserRole
from app.models.customer_complaint import (
    CustomerComplaint, ReturnMaterialAuthorization,
    ComplaintStatus, ComplaintSeverity, RMAStatus
)
from app.models.quality import (
    NonConformanceReport, NCRStatus, NCRSource,
    CorrectiveActionRequest, CARStatus
)

router = APIRouter()


# ============== Pydantic Schemas ==============

class PartSummary(BaseModel):
    id: int
    part_number: str
    name: str

    class Config:
        from_attributes = True


class CustomerSummary(BaseModel):
    id: int
    name: str

    class Config:
        from_attributes = True


class RMABrief(BaseModel):
    id: int
    rma_number: str
    status: RMAStatus
    quantity: float
    disposition: Optional[str] = None

    class Config:
        from_attributes = True


class ComplaintCreate(BaseModel):
    customer_id: Optional[int] = None
    customer_name: str = Field(..., min_length=1, max_length=255)
    customer_po_number: Optional[str] = Field(None, max_length=100)
    customer_contact: Optional[str] = Field(None, max_length=255)
    part_id: Optional[int] = None
    work_order_id: Optional[int] = None
    lot_number: Optional[str] = Field(None, max_length=100)
    serial_number: Optional[str] = Field(None, max_length=100)
    quantity_affected: float = Field(default=1, gt=0)
    severity: ComplaintSeverity = ComplaintSeverity.MINOR
    title: str = Field(..., min_length=3, max_length=255)
    description: str = Field(..., min_length=5)
    date_received: Optional[date] = None
    date_of_occurrence: Optional[date] = None
    assigned_to: Optional[int] = None
    estimated_cost: float = Field(default=0, ge=0)


class ComplaintUpdate(BaseModel):
    customer_name: Optional[str] = Field(None, max_length=255)
    customer_po_number: Optional[str] = Field(None, max_length=100)
    customer_contact: Optional[str] = Field(None, max_length=255)
    part_id: Optional[int] = None
    work_order_id: Optional[int] = None
    lot_number: Optional[str] = Field(None, max_length=100)
    serial_number: Optional[str] = Field(None, max_length=100)
    quantity_affected: Optional[float] = Field(None, gt=0)
    severity: Optional[ComplaintSeverity] = None
    status: Optional[ComplaintStatus] = None
    title: Optional[str] = Field(None, max_length=255)
    description: Optional[str] = None
    investigation_findings: Optional[str] = None
    root_cause: Optional[str] = None
    containment_action: Optional[str] = None
    corrective_action: Optional[str] = None
    preventive_action: Optional[str] = None
    resolution_description: Optional[str] = None
    assigned_to: Optional[int] = None
    estimated_cost: Optional[float] = Field(None, ge=0)
    actual_cost: Optional[float] = Field(None, ge=0)


class InvestigateRequest(BaseModel):
    investigation_findings: str = Field(..., min_length=5)
    root_cause: Optional[str] = None
    containment_action: Optional[str] = None


class ResolveRequest(BaseModel):
    resolution_description: str = Field(..., min_length=5)
    corrective_action: Optional[str] = None
    preventive_action: Optional[str] = None
    actual_cost: Optional[float] = Field(None, ge=0)


class CloseRequest(BaseModel):
    customer_satisfied: Optional[bool] = None
    satisfaction_notes: Optional[str] = None


class ComplaintResponse(BaseModel):
    id: int
    complaint_number: str
    customer_id: Optional[int] = None
    customer_name: str
    customer_po_number: Optional[str] = None
    customer_contact: Optional[str] = None
    part_id: Optional[int] = None
    part: Optional[PartSummary] = None
    customer: Optional[CustomerSummary] = None
    work_order_id: Optional[int] = None
    lot_number: Optional[str] = None
    serial_number: Optional[str] = None
    quantity_affected: float
    severity: ComplaintSeverity
    status: ComplaintStatus
    title: str
    description: str
    date_received: Optional[date] = None
    date_of_occurrence: Optional[date] = None
    investigation_findings: Optional[str] = None
    root_cause: Optional[str] = None
    containment_action: Optional[str] = None
    corrective_action: Optional[str] = None
    preventive_action: Optional[str] = None
    resolution_description: Optional[str] = None
    ncr_id: Optional[int] = None
    car_id: Optional[int] = None
    estimated_cost: float
    actual_cost: float
    assigned_to: Optional[int] = None
    received_by: Optional[int] = None
    resolved_date: Optional[date] = None
    closed_date: Optional[date] = None
    customer_satisfied: Optional[bool] = None
    satisfaction_notes: Optional[str] = None
    rmas: List[RMABrief] = Field(default_factory=list)
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class RMACreate(BaseModel):
    complaint_id: Optional[int] = None
    customer_id: Optional[int] = None
    customer_name: str = Field(..., min_length=1, max_length=255)
    part_id: Optional[int] = None
    quantity: float = Field(..., gt=0)
    lot_number: Optional[str] = Field(None, max_length=100)
    reason: str = Field(..., min_length=5)
    notes: Optional[str] = None


class RMAUpdate(BaseModel):
    quantity: Optional[float] = Field(None, gt=0)
    lot_number: Optional[str] = Field(None, max_length=100)
    reason: Optional[str] = None
    disposition: Optional[str] = Field(None, max_length=100)
    shipping_tracking: Optional[str] = Field(None, max_length=255)
    inspection_findings: Optional[str] = None
    credit_amount: Optional[float] = Field(None, ge=0)
    replacement_wo_id: Optional[int] = None
    notes: Optional[str] = None


class RMAResponse(BaseModel):
    id: int
    rma_number: str
    complaint_id: Optional[int] = None
    customer_id: Optional[int] = None
    customer_name: str
    part_id: Optional[int] = None
    part: Optional[PartSummary] = None
    customer: Optional[CustomerSummary] = None
    status: RMAStatus
    quantity: float
    lot_number: Optional[str] = None
    reason: str
    disposition: Optional[str] = None
    shipping_tracking: Optional[str] = None
    received_date: Optional[date] = None
    inspection_date: Optional[date] = None
    inspection_findings: Optional[str] = None
    replacement_wo_id: Optional[int] = None
    credit_amount: float
    authorized_by: Optional[int] = None
    authorized_date: Optional[date] = None
    notes: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class DashboardResponse(BaseModel):
    open_complaints: int
    avg_resolution_days: Optional[float] = None
    by_severity: dict
    by_customer: List[dict]
    satisfaction_rate: Optional[float] = None
    trend: List[dict]
    open_rmas: int


class EightDReport(BaseModel):
    complaint: ComplaintResponse
    d1_team: Optional[str] = None
    d2_problem_description: str
    d3_containment_action: Optional[str] = None
    d4_root_cause: Optional[str] = None
    d5_corrective_action: Optional[str] = None
    d6_verification: Optional[str] = None
    d7_preventive_action: Optional[str] = None
    d8_customer_satisfaction: Optional[str] = None


# ============== Number Generators ==============

def generate_complaint_number(db: Session) -> str:
    year = datetime.now().strftime("%Y")
    prefix = f"CC-{year}-"
    last = db.query(CustomerComplaint).filter(
        CustomerComplaint.complaint_number.like(f"{prefix}%")
    ).order_by(CustomerComplaint.complaint_number.desc()).first()

    if last:
        num = int(last.complaint_number.split("-")[-1]) + 1
    else:
        num = 1
    return f"{prefix}{num:04d}"


def generate_rma_number(db: Session) -> str:
    year = datetime.now().strftime("%Y")
    prefix = f"RMA-{year}-"
    last = db.query(ReturnMaterialAuthorization).filter(
        ReturnMaterialAuthorization.rma_number.like(f"{prefix}%")
    ).order_by(ReturnMaterialAuthorization.rma_number.desc()).first()

    if last:
        num = int(last.rma_number.split("-")[-1]) + 1
    else:
        num = 1
    return f"{prefix}{num:04d}"


# ============== Complaint Endpoints ==============

@router.get("/complaints/dashboard", response_model=DashboardResponse)
def get_dashboard(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get complaint dashboard statistics"""
    open_statuses = [ComplaintStatus.RECEIVED, ComplaintStatus.UNDER_INVESTIGATION, ComplaintStatus.PENDING_RESOLUTION]
    open_complaints = db.query(func.count(CustomerComplaint.id)).filter(
        CustomerComplaint.status.in_(open_statuses)
    ).scalar() or 0

    # Average resolution time
    resolved = db.query(CustomerComplaint).filter(
        CustomerComplaint.resolved_date.isnot(None),
        CustomerComplaint.date_received.isnot(None)
    ).all()
    avg_days = None
    if resolved:
        total_days = sum(
            (r.resolved_date - r.date_received).days for r in resolved if r.resolved_date and r.date_received
        )
        avg_days = round(total_days / len(resolved), 1) if resolved else None

    # By severity
    severity_counts = db.query(
        CustomerComplaint.severity, func.count(CustomerComplaint.id)
    ).filter(
        CustomerComplaint.status.in_(open_statuses)
    ).group_by(CustomerComplaint.severity).all()
    by_severity = {s.value: c for s, c in severity_counts}

    # By customer (top 10)
    by_customer_rows = db.query(
        CustomerComplaint.customer_name, func.count(CustomerComplaint.id).label("count")
    ).group_by(CustomerComplaint.customer_name).order_by(
        func.count(CustomerComplaint.id).desc()
    ).limit(10).all()
    by_customer = [{"customer": name, "count": count} for name, count in by_customer_rows]

    # Satisfaction rate
    satisfied_total = db.query(func.count(CustomerComplaint.id)).filter(
        CustomerComplaint.customer_satisfied.isnot(None)
    ).scalar() or 0
    satisfied_yes = db.query(func.count(CustomerComplaint.id)).filter(
        CustomerComplaint.customer_satisfied == True
    ).scalar() or 0
    satisfaction_rate = round((satisfied_yes / satisfied_total) * 100, 1) if satisfied_total > 0 else None

    # Monthly trend (last 12 months)
    twelve_months_ago = date.today() - timedelta(days=365)
    trend_rows = db.query(
        extract('year', CustomerComplaint.date_received).label('year'),
        extract('month', CustomerComplaint.date_received).label('month'),
        func.count(CustomerComplaint.id).label('count')
    ).filter(
        CustomerComplaint.date_received >= twelve_months_ago
    ).group_by('year', 'month').order_by('year', 'month').all()
    trend = [{"year": int(r.year), "month": int(r.month), "count": r.count} for r in trend_rows]

    # Open RMAs
    open_rma_statuses = [RMAStatus.REQUESTED, RMAStatus.APPROVED, RMAStatus.MATERIAL_RECEIVED, RMAStatus.UNDER_INSPECTION, RMAStatus.DISPOSITION_DECIDED]
    open_rmas = db.query(func.count(ReturnMaterialAuthorization.id)).filter(
        ReturnMaterialAuthorization.status.in_(open_rma_statuses)
    ).scalar() or 0

    return DashboardResponse(
        open_complaints=open_complaints,
        avg_resolution_days=avg_days,
        by_severity=by_severity,
        by_customer=by_customer,
        satisfaction_rate=satisfaction_rate,
        trend=trend,
        open_rmas=open_rmas
    )


@router.get("/complaints/8d-report/{complaint_id}", response_model=EightDReport)
def get_8d_report(
    complaint_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Generate 8D report data for a complaint"""
    complaint = db.query(CustomerComplaint).options(
        joinedload(CustomerComplaint.part),
        joinedload(CustomerComplaint.customer),
        joinedload(CustomerComplaint.rmas),
    ).filter(CustomerComplaint.id == complaint_id).first()

    if not complaint:
        raise HTTPException(status_code=404, detail="Complaint not found")

    satisfaction_text = None
    if complaint.customer_satisfied is not None:
        satisfaction_text = f"{'Satisfied' if complaint.customer_satisfied else 'Not satisfied'}"
        if complaint.satisfaction_notes:
            satisfaction_text += f" - {complaint.satisfaction_notes}"

    return EightDReport(
        complaint=complaint,
        d1_team=f"Assigned to user ID: {complaint.assigned_to}" if complaint.assigned_to else None,
        d2_problem_description=complaint.description,
        d3_containment_action=complaint.containment_action,
        d4_root_cause=complaint.root_cause,
        d5_corrective_action=complaint.corrective_action,
        d6_verification=complaint.resolution_description,
        d7_preventive_action=complaint.preventive_action,
        d8_customer_satisfaction=satisfaction_text
    )


@router.get("/complaints/", response_model=List[ComplaintResponse])
def list_complaints(
    skip: int = 0,
    limit: int = 100,
    status: Optional[ComplaintStatus] = None,
    severity: Optional[ComplaintSeverity] = None,
    customer_id: Optional[int] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id)
):
    """List all complaints with filters"""
    query = db.query(CustomerComplaint).filter(CustomerComplaint.company_id == company_id).options(
        joinedload(CustomerComplaint.part),
        joinedload(CustomerComplaint.customer),
        joinedload(CustomerComplaint.rmas),
    )

    if status:
        query = query.filter(CustomerComplaint.status == status)
    if severity:
        query = query.filter(CustomerComplaint.severity == severity)
    if customer_id:
        query = query.filter(CustomerComplaint.customer_id == customer_id)
    if date_from:
        query = query.filter(CustomerComplaint.date_received >= date_from)
    if date_to:
        query = query.filter(CustomerComplaint.date_received <= date_to)

    return query.order_by(CustomerComplaint.created_at.desc()).offset(skip).limit(limit).all()


@router.get("/complaints/{complaint_id}", response_model=ComplaintResponse)
def get_complaint(
    complaint_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get complaint details"""
    complaint = db.query(CustomerComplaint).options(
        joinedload(CustomerComplaint.part),
        joinedload(CustomerComplaint.customer),
        joinedload(CustomerComplaint.rmas),
    ).filter(CustomerComplaint.id == complaint_id).first()

    if not complaint:
        raise HTTPException(status_code=404, detail="Complaint not found")
    return complaint


@router.post("/complaints/", response_model=ComplaintResponse)
def create_complaint(
    data: ComplaintCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id)
):
    """Create a new customer complaint"""
    complaint = CustomerComplaint(
        complaint_number=generate_complaint_number(db),
        **data.model_dump(),
        received_by=current_user.id,
        date_received=data.date_received or date.today()
    )
    complaint.company_id = company_id
    db.add(complaint)
    db.commit()
    db.refresh(complaint)
    return complaint


@router.put("/complaints/{complaint_id}", response_model=ComplaintResponse)
def update_complaint(
    complaint_id: int,
    data: ComplaintUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id)
):
    """Update a complaint"""
    complaint = db.query(CustomerComplaint).filter(CustomerComplaint.id == complaint_id, CustomerComplaint.company_id == company_id).first()
    if not complaint:
        raise HTTPException(status_code=404, detail="Complaint not found")

    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(complaint, field, value)

    db.commit()
    db.refresh(complaint)
    return complaint


@router.post("/complaints/{complaint_id}/investigate", response_model=ComplaintResponse)
def investigate_complaint(
    complaint_id: int,
    data: InvestigateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update complaint to under investigation with findings"""
    complaint = db.query(CustomerComplaint).filter(CustomerComplaint.id == complaint_id).first()
    if not complaint:
        raise HTTPException(status_code=404, detail="Complaint not found")

    complaint.status = ComplaintStatus.UNDER_INVESTIGATION
    complaint.investigation_findings = data.investigation_findings
    if data.root_cause:
        complaint.root_cause = data.root_cause
    if data.containment_action:
        complaint.containment_action = data.containment_action

    db.commit()
    db.refresh(complaint)
    return complaint


@router.post("/complaints/{complaint_id}/resolve", response_model=ComplaintResponse)
def resolve_complaint(
    complaint_id: int,
    data: ResolveRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Resolve a complaint"""
    complaint = db.query(CustomerComplaint).filter(CustomerComplaint.id == complaint_id).first()
    if not complaint:
        raise HTTPException(status_code=404, detail="Complaint not found")

    complaint.status = ComplaintStatus.RESOLVED
    complaint.resolution_description = data.resolution_description
    complaint.resolved_date = date.today()
    if data.corrective_action:
        complaint.corrective_action = data.corrective_action
    if data.preventive_action:
        complaint.preventive_action = data.preventive_action
    if data.actual_cost is not None:
        complaint.actual_cost = data.actual_cost

    db.commit()
    db.refresh(complaint)
    return complaint


@router.post("/complaints/{complaint_id}/close", response_model=ComplaintResponse)
def close_complaint(
    complaint_id: int,
    data: CloseRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Close a complaint with optional satisfaction feedback"""
    complaint = db.query(CustomerComplaint).filter(CustomerComplaint.id == complaint_id).first()
    if not complaint:
        raise HTTPException(status_code=404, detail="Complaint not found")

    complaint.status = ComplaintStatus.CLOSED
    complaint.closed_date = date.today()
    if data.customer_satisfied is not None:
        complaint.customer_satisfied = data.customer_satisfied
    if data.satisfaction_notes:
        complaint.satisfaction_notes = data.satisfaction_notes

    db.commit()
    db.refresh(complaint)
    return complaint


@router.post("/complaints/{complaint_id}/create-ncr", response_model=ComplaintResponse)
def create_ncr_from_complaint(
    complaint_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY]))
):
    """Create a linked NCR from a complaint"""
    complaint = db.query(CustomerComplaint).filter(CustomerComplaint.id == complaint_id).first()
    if not complaint:
        raise HTTPException(status_code=404, detail="Complaint not found")
    if complaint.ncr_id:
        raise HTTPException(status_code=400, detail="Complaint already has a linked NCR")

    # Generate NCR number
    today = datetime.now().strftime("%Y%m%d")
    prefix = f"NCR-{today}-"
    last = db.query(NonConformanceReport).filter(
        NonConformanceReport.ncr_number.like(f"{prefix}%")
    ).order_by(NonConformanceReport.ncr_number.desc()).first()
    num = int(last.ncr_number.split("-")[-1]) + 1 if last else 1
    ncr_number = f"{prefix}{num:03d}"

    ncr = NonConformanceReport(
        ncr_number=ncr_number,
        title=f"NCR from {complaint.complaint_number}: {complaint.title}",
        description=complaint.description,
        source=NCRSource.CUSTOMER_RETURN,
        part_id=complaint.part_id,
        work_order_id=complaint.work_order_id,
        lot_number=complaint.lot_number,
        serial_number=complaint.serial_number,
        quantity_affected=complaint.quantity_affected,
        detected_by=current_user.id,
        detected_date=date.today()
    )
    db.add(ncr)
    db.flush()

    complaint.ncr_id = ncr.id
    db.commit()
    db.refresh(complaint)
    return complaint


@router.post("/complaints/{complaint_id}/create-car", response_model=ComplaintResponse)
def create_car_from_complaint(
    complaint_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY]))
):
    """Create a linked CAR from a complaint"""
    complaint = db.query(CustomerComplaint).filter(CustomerComplaint.id == complaint_id).first()
    if not complaint:
        raise HTTPException(status_code=404, detail="Complaint not found")
    if complaint.car_id:
        raise HTTPException(status_code=400, detail="Complaint already has a linked CAR")

    # Generate CAR number
    today = datetime.now().strftime("%Y%m%d")
    prefix = f"CAR-{today}-"
    last = db.query(CorrectiveActionRequest).filter(
        CorrectiveActionRequest.car_number.like(f"{prefix}%")
    ).order_by(CorrectiveActionRequest.car_number.desc()).first()
    num = int(last.car_number.split("-")[-1]) + 1 if last else 1
    car_number = f"{prefix}{num:03d}"

    car = CorrectiveActionRequest(
        car_number=car_number,
        title=f"CAR from {complaint.complaint_number}: {complaint.title}",
        problem_description=complaint.description,
        initiated_by=current_user.id
    )
    db.add(car)
    db.flush()

    complaint.car_id = car.id
    db.commit()
    db.refresh(complaint)
    return complaint


# ============== RMA Endpoints ==============

@router.get("/rma/", response_model=List[RMAResponse])
def list_rmas(
    skip: int = 0,
    limit: int = 100,
    status: Optional[RMAStatus] = None,
    customer_id: Optional[int] = None,
    complaint_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id)
):
    """List all RMAs with filters"""
    query = db.query(ReturnMaterialAuthorization).filter(ReturnMaterialAuthorization.company_id == company_id).options(
        joinedload(ReturnMaterialAuthorization.part),
        joinedload(ReturnMaterialAuthorization.customer),
    )

    if status:
        query = query.filter(ReturnMaterialAuthorization.status == status)
    if customer_id:
        query = query.filter(ReturnMaterialAuthorization.customer_id == customer_id)
    if complaint_id:
        query = query.filter(ReturnMaterialAuthorization.complaint_id == complaint_id)

    return query.order_by(ReturnMaterialAuthorization.created_at.desc()).offset(skip).limit(limit).all()


@router.get("/rma/{rma_id}", response_model=RMAResponse)
def get_rma(
    rma_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get RMA details"""
    rma = db.query(ReturnMaterialAuthorization).options(
        joinedload(ReturnMaterialAuthorization.part),
        joinedload(ReturnMaterialAuthorization.customer),
    ).filter(ReturnMaterialAuthorization.id == rma_id).first()

    if not rma:
        raise HTTPException(status_code=404, detail="RMA not found")
    return rma


@router.post("/rma/", response_model=RMAResponse)
def create_rma(
    data: RMACreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id)
):
    """Create a new RMA"""
    rma = ReturnMaterialAuthorization(
        rma_number=generate_rma_number(db),
        **data.model_dump()
    )
    rma.company_id = company_id
    db.add(rma)
    db.commit()
    db.refresh(rma)
    return rma


@router.put("/rma/{rma_id}", response_model=RMAResponse)
def update_rma(
    rma_id: int,
    data: RMAUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id)
):
    """Update an RMA"""
    rma = db.query(ReturnMaterialAuthorization).filter(ReturnMaterialAuthorization.id == rma_id, ReturnMaterialAuthorization.company_id == company_id).first()
    if not rma:
        raise HTTPException(status_code=404, detail="RMA not found")

    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(rma, field, value)

    db.commit()
    db.refresh(rma)
    return rma


@router.post("/rma/{rma_id}/approve", response_model=RMAResponse)
def approve_rma(
    rma_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY]))
):
    """Approve an RMA"""
    rma = db.query(ReturnMaterialAuthorization).filter(ReturnMaterialAuthorization.id == rma_id).first()
    if not rma:
        raise HTTPException(status_code=404, detail="RMA not found")
    if rma.status != RMAStatus.REQUESTED:
        raise HTTPException(status_code=400, detail="RMA can only be approved from REQUESTED status")

    rma.status = RMAStatus.APPROVED
    rma.authorized_by = current_user.id
    rma.authorized_date = date.today()

    db.commit()
    db.refresh(rma)
    return rma


@router.post("/rma/{rma_id}/deny", response_model=RMAResponse)
def deny_rma(
    rma_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY]))
):
    """Deny an RMA"""
    rma = db.query(ReturnMaterialAuthorization).filter(ReturnMaterialAuthorization.id == rma_id).first()
    if not rma:
        raise HTTPException(status_code=404, detail="RMA not found")
    if rma.status != RMAStatus.REQUESTED:
        raise HTTPException(status_code=400, detail="RMA can only be denied from REQUESTED status")

    rma.status = RMAStatus.DENIED
    rma.authorized_by = current_user.id
    rma.authorized_date = date.today()

    db.commit()
    db.refresh(rma)
    return rma


class ReceiveRequest(BaseModel):
    shipping_tracking: Optional[str] = Field(None, max_length=255)


@router.post("/rma/{rma_id}/receive", response_model=RMAResponse)
def receive_rma(
    rma_id: int,
    data: ReceiveRequest = ReceiveRequest(),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Mark RMA material as received"""
    rma = db.query(ReturnMaterialAuthorization).filter(ReturnMaterialAuthorization.id == rma_id).first()
    if not rma:
        raise HTTPException(status_code=404, detail="RMA not found")
    if rma.status != RMAStatus.APPROVED:
        raise HTTPException(status_code=400, detail="RMA must be approved before receiving material")

    rma.status = RMAStatus.MATERIAL_RECEIVED
    rma.received_date = date.today()
    if data.shipping_tracking:
        rma.shipping_tracking = data.shipping_tracking

    db.commit()
    db.refresh(rma)
    return rma


class InspectRequest(BaseModel):
    inspection_findings: str = Field(..., min_length=5)


@router.post("/rma/{rma_id}/inspect", response_model=RMAResponse)
def inspect_rma(
    rma_id: int,
    data: InspectRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Record RMA inspection findings"""
    rma = db.query(ReturnMaterialAuthorization).filter(ReturnMaterialAuthorization.id == rma_id).first()
    if not rma:
        raise HTTPException(status_code=404, detail="RMA not found")
    if rma.status != RMAStatus.MATERIAL_RECEIVED:
        raise HTTPException(status_code=400, detail="Material must be received before inspection")

    rma.status = RMAStatus.UNDER_INSPECTION
    rma.inspection_date = date.today()
    rma.inspection_findings = data.inspection_findings

    db.commit()
    db.refresh(rma)
    return rma


class DisposeRequest(BaseModel):
    disposition: str = Field(..., max_length=100)
    credit_amount: Optional[float] = Field(None, ge=0)
    replacement_wo_id: Optional[int] = None
    notes: Optional[str] = None


@router.post("/rma/{rma_id}/dispose", response_model=RMAResponse)
def dispose_rma(
    rma_id: int,
    data: DisposeRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY]))
):
    """Set RMA disposition"""
    rma = db.query(ReturnMaterialAuthorization).filter(ReturnMaterialAuthorization.id == rma_id).first()
    if not rma:
        raise HTTPException(status_code=404, detail="RMA not found")
    if rma.status not in [RMAStatus.UNDER_INSPECTION, RMAStatus.MATERIAL_RECEIVED]:
        raise HTTPException(status_code=400, detail="RMA must be under inspection or material received for disposition")

    valid_dispositions = ["replace", "repair", "credit", "scrap", "return_to_customer"]
    if data.disposition not in valid_dispositions:
        raise HTTPException(status_code=400, detail=f"Disposition must be one of: {', '.join(valid_dispositions)}")

    rma.status = RMAStatus.DISPOSITION_DECIDED
    rma.disposition = data.disposition
    if data.credit_amount is not None:
        rma.credit_amount = data.credit_amount
    if data.replacement_wo_id:
        rma.replacement_wo_id = data.replacement_wo_id
    if data.notes:
        rma.notes = data.notes

    db.commit()
    db.refresh(rma)
    return rma


@router.post("/rma/{rma_id}/complete", response_model=RMAResponse)
def complete_rma(
    rma_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY]))
):
    """Complete an RMA"""
    rma = db.query(ReturnMaterialAuthorization).filter(ReturnMaterialAuthorization.id == rma_id).first()
    if not rma:
        raise HTTPException(status_code=404, detail="RMA not found")
    if rma.status != RMAStatus.DISPOSITION_DECIDED:
        raise HTTPException(status_code=400, detail="RMA disposition must be decided before completing")

    rma.status = RMAStatus.COMPLETED

    db.commit()
    db.refresh(rma)
    return rma
