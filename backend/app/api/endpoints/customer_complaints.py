"""Customer Complaints & RMA router (mounted at ``/complaints``).

Thin by design: every lookup, reference check, transition guard and audit row lives in
``app/services/customer_complaint_service.py``, whose module docstring holds the
tenancy / audit / role contracts. Each route scopes to the active company through
``get_current_company_id``; a foreign id is a 404. Writes are role-gated:

* ``AUTHOR_ROLES`` (Admin / Manager / Supervisor / Quality, the ``quality:inspect``
  tier): create and edit complaints and RMAs, record an investigation, receive and
  inspect returned material.
* ``APPROVER_ROLES`` (Admin / Manager / Quality, the ``quality:approve`` tier):
  resolve and close a complaint, raise an NCR or CAR from it, and approve, deny,
  dispose and complete an RMA.

Reads (dashboard, 8D report, lists, details) are open to any authenticated user of
the tenant, like every other operational module.
"""

from datetime import date, datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.deps import get_audit_service, get_current_company_id, get_current_user, require_role
from app.db.database import get_db
from app.models.customer_complaint import (
    ComplaintSeverity,
    ComplaintStatus,
    CustomerComplaint,
    ReturnMaterialAuthorization,
    RMAStatus,
)
from app.models.user import User
from app.services import customer_complaint_service as complaints
from app.services.audit_service import AuditService
from app.services.customer_complaint_service import APPROVER_ROLES, AUTHOR_ROLES, complaint_transaction

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
    # ``status`` IS DELIBERATELY ABSENT, and unknown fields are refused (422) so a client
    # still sending it learns that instead of being silently ignored. The blind setattr
    # this schema feeds used to let any caller set any status -- CLOSED straight from
    # RECEIVED, with no investigation, no resolution, no resolved/closed date and no
    # audit evidence. Status now moves only through the workflow verbs
    # (investigate / resolve / close), each role-gated, state-guarded and audited.
    # Consequence, accepted: PENDING_RESOLUTION and VOID have no writer until a
    # dedicated, reasoned and audited verb ships for them.
    model_config = ConfigDict(extra="forbid")

    customer_name: Optional[str] = Field(None, max_length=255)
    customer_po_number: Optional[str] = Field(None, max_length=100)
    customer_contact: Optional[str] = Field(None, max_length=255)
    part_id: Optional[int] = None
    work_order_id: Optional[int] = None
    lot_number: Optional[str] = Field(None, max_length=100)
    serial_number: Optional[str] = Field(None, max_length=100)
    quantity_affected: Optional[float] = Field(None, gt=0)
    severity: Optional[ComplaintSeverity] = None
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
    # ``disposition`` IS DELIBERATELY ABSENT (unknown fields are refused, 422): it was a
    # free-text field here that bypassed ``POST /rma/{id}/dispose`` -- its allow-list, its
    # Admin/Manager/Quality gate, its from-state guard and its required audit row.
    model_config = ConfigDict(extra="forbid")

    quantity: Optional[float] = Field(None, gt=0)
    lot_number: Optional[str] = Field(None, max_length=100)
    reason: Optional[str] = None
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


class ReceiveRequest(BaseModel):
    shipping_tracking: Optional[str] = Field(None, max_length=255)


class InspectRequest(BaseModel):
    inspection_findings: str = Field(..., min_length=5)


class DisposeRequest(BaseModel):
    disposition: str = Field(..., max_length=100)
    credit_amount: Optional[float] = Field(None, ge=0)
    replacement_wo_id: Optional[int] = None
    notes: Optional[str] = None


# ============== Complaint Endpoints ==============


@router.get("/complaints/dashboard", response_model=DashboardResponse)
def get_dashboard(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Complaint and RMA statistics for the active company: open complaints, average
    resolution days, open complaints by severity, top customers, satisfaction rate,
    12-month trend and open RMAs."""
    return DashboardResponse(**complaints.complaint_dashboard(db, company_id))


@router.get("/complaints/8d-report/{complaint_id}", response_model=EightDReport)
def get_8d_report(
    complaint_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """8D report data (D1-D8) for one complaint of the active company; 404 if it is not ours."""
    complaint = complaints.get_complaint_or_404(db, company_id, complaint_id)

    satisfaction_text = None
    if complaint.customer_satisfied is not None:
        satisfaction_text = f"{'Satisfied' if complaint.customer_satisfied else 'Not satisfied'}"
        if complaint.satisfaction_notes:
            satisfaction_text += f" - {complaint.satisfaction_notes}"

    return EightDReport(
        complaint=ComplaintResponse.model_validate(complaint),
        d1_team=f"Assigned to user ID: {complaint.assigned_to}" if complaint.assigned_to else None,
        d2_problem_description=complaint.description,
        d3_containment_action=complaint.containment_action,
        d4_root_cause=complaint.root_cause,
        d5_corrective_action=complaint.corrective_action,
        d6_verification=complaint.resolution_description,
        d7_preventive_action=complaint.preventive_action,
        d8_customer_satisfaction=satisfaction_text,
    )


@router.get("/complaints/", response_model=List[ComplaintResponse])
def list_complaints(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=5000),
    status: Optional[ComplaintStatus] = None,
    severity: Optional[ComplaintSeverity] = None,
    customer_id: Optional[int] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """List the active company's complaints, newest first, with optional filters."""
    query = complaints.complaint_query(db, company_id)

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
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """One complaint of the active company, with its part, customer and RMAs; 404 if it is not ours."""
    return complaints.get_complaint_or_404(db, company_id, complaint_id)


@router.post("/complaints/", response_model=ComplaintResponse)
def create_complaint(
    data: ComplaintCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(AUTHOR_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Record a customer complaint (status RECEIVED). Admin / Manager / Supervisor / Quality.

    ``customer_id``, ``part_id``, ``work_order_id`` and ``assigned_to`` must name live
    records of the active company (422 otherwise)."""
    with complaint_transaction(db):
        complaint_id = complaints.create_complaint(db, company_id, data.model_dump(), current_user, audit).id
    return complaints.get_complaint_or_404(db, company_id, complaint_id)


@router.put("/complaints/{complaint_id}", response_model=ComplaintResponse)
def update_complaint(
    complaint_id: int,
    data: ComplaintUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(AUTHOR_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Edit a complaint's details. Admin / Manager / Supervisor / Quality.

    Status is not editable here: use investigate / resolve / close. A changed reference
    must name a live record of the active company (422 otherwise)."""
    with complaint_transaction(db):
        complaints.update_complaint(db, company_id, complaint_id, data.model_dump(exclude_unset=True), audit)
    return complaints.get_complaint_or_404(db, company_id, complaint_id)


@router.post("/complaints/{complaint_id}/investigate", response_model=ComplaintResponse)
def investigate_complaint(
    complaint_id: int,
    data: InvestigateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(AUTHOR_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Record investigation findings and move an open complaint to UNDER_INVESTIGATION.
    Admin / Manager / Supervisor / Quality. 400 once the complaint is resolved or closed."""
    with complaint_transaction(db):
        complaints.investigate_complaint(db, company_id, complaint_id, audit, **data.model_dump())
    return complaints.get_complaint_or_404(db, company_id, complaint_id)


@router.post("/complaints/{complaint_id}/resolve", response_model=ComplaintResponse)
def resolve_complaint(
    complaint_id: int,
    data: ResolveRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(APPROVER_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Resolve an investigated complaint (UNDER_INVESTIGATION or PENDING_RESOLUTION ->
    RESOLVED). Admin / Manager / Quality. The audit row is required: 503 and nothing
    saved if it cannot be written."""
    with complaint_transaction(db):
        complaints.resolve_complaint(db, company_id, complaint_id, audit, **data.model_dump())
    return complaints.get_complaint_or_404(db, company_id, complaint_id)


@router.post("/complaints/{complaint_id}/close", response_model=ComplaintResponse)
def close_complaint(
    complaint_id: int,
    data: CloseRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(APPROVER_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Close a RESOLVED complaint, optionally recording customer satisfaction.
    Admin / Manager / Quality. The audit row is required: 503 and nothing saved if it
    cannot be written."""
    with complaint_transaction(db):
        complaints.close_complaint(db, company_id, complaint_id, audit, **data.model_dump())
    return complaints.get_complaint_or_404(db, company_id, complaint_id)


@router.post("/complaints/{complaint_id}/create-ncr", response_model=ComplaintResponse)
def create_ncr_from_complaint(
    complaint_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(APPROVER_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Raise a customer-return NCR from the complaint and link it. Admin / Manager / Quality.

    Numbered, evented (``ncr_created``) and tenant-tagged like ``POST /quality/ncr``.
    400 if the complaint already has an NCR."""
    with complaint_transaction(db):
        complaints.create_ncr_from_complaint(db, company_id, complaint_id, current_user, audit)
    return complaints.get_complaint_or_404(db, company_id, complaint_id)


@router.post("/complaints/{complaint_id}/create-car", response_model=ComplaintResponse)
def create_car_from_complaint(
    complaint_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(APPROVER_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Raise a corrective action request from the complaint and link it. Admin / Manager / Quality.

    Numbered, evented (``car_created``) and tenant-tagged like ``POST /quality/car``.
    400 if the complaint already has a CAR."""
    with complaint_transaction(db):
        complaints.create_car_from_complaint(db, company_id, complaint_id, current_user, audit)
    return complaints.get_complaint_or_404(db, company_id, complaint_id)


# ============== RMA Endpoints ==============


@router.get("/rma/", response_model=List[RMAResponse])
def list_rmas(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=5000),
    status: Optional[RMAStatus] = None,
    customer_id: Optional[int] = None,
    complaint_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """List the active company's RMAs, newest first, with optional filters."""
    query = complaints.rma_query(db, company_id)

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
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """One RMA of the active company; 404 if it is not ours."""
    return complaints.get_rma_or_404(db, company_id, rma_id)


@router.post("/rma/", response_model=RMAResponse)
def create_rma(
    data: RMACreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(AUTHOR_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Open a return material authorization (status REQUESTED). Admin / Manager / Supervisor / Quality.

    ``complaint_id``, ``customer_id`` and ``part_id`` must name records of the active
    company (422 otherwise)."""
    with complaint_transaction(db):
        rma_id = complaints.create_rma(db, company_id, data.model_dump(), audit).id
    return complaints.get_rma_or_404(db, company_id, rma_id)


@router.put("/rma/{rma_id}", response_model=RMAResponse)
def update_rma(
    rma_id: int,
    data: RMAUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(AUTHOR_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Edit an RMA's details. Admin / Manager / Supervisor / Quality.

    Status and disposition are not editable here: use the RMA workflow verbs."""
    with complaint_transaction(db):
        complaints.update_rma(db, company_id, rma_id, data.model_dump(exclude_unset=True), audit)
    return complaints.get_rma_or_404(db, company_id, rma_id)


@router.post("/rma/{rma_id}/approve", response_model=RMAResponse)
def approve_rma(
    rma_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(APPROVER_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Approve a REQUESTED RMA, recording who authorized it. Admin / Manager / Quality."""
    with complaint_transaction(db):
        complaints.decide_rma(db, company_id, rma_id, current_user, audit, approve=True)
    return complaints.get_rma_or_404(db, company_id, rma_id)


@router.post("/rma/{rma_id}/deny", response_model=RMAResponse)
def deny_rma(
    rma_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(APPROVER_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Deny a REQUESTED RMA, recording who decided. Admin / Manager / Quality."""
    with complaint_transaction(db):
        complaints.decide_rma(db, company_id, rma_id, current_user, audit, approve=False)
    return complaints.get_rma_or_404(db, company_id, rma_id)


@router.post("/rma/{rma_id}/receive", response_model=RMAResponse)
def receive_rma(
    rma_id: int,
    data: ReceiveRequest = ReceiveRequest(),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(AUTHOR_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Mark an APPROVED RMA's material as received. Admin / Manager / Supervisor / Quality."""
    with complaint_transaction(db):
        complaints.receive_rma(db, company_id, rma_id, audit, shipping_tracking=data.shipping_tracking)
    return complaints.get_rma_or_404(db, company_id, rma_id)


@router.post("/rma/{rma_id}/inspect", response_model=RMAResponse)
def inspect_rma(
    rma_id: int,
    data: InspectRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(AUTHOR_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Record inspection findings on received RMA material. Admin / Manager / Supervisor / Quality."""
    with complaint_transaction(db):
        complaints.inspect_rma(db, company_id, rma_id, audit, inspection_findings=data.inspection_findings)
    return complaints.get_rma_or_404(db, company_id, rma_id)


@router.post("/rma/{rma_id}/dispose", response_model=RMAResponse)
def dispose_rma(
    rma_id: int,
    data: DisposeRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(APPROVER_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Decide an RMA's disposition: replace, repair, credit, scrap or return_to_customer.
    Admin / Manager / Quality. ``replacement_wo_id`` must be a live work order of the
    active company. The audit row is required: 503 and nothing saved if it cannot be
    written."""
    with complaint_transaction(db):
        complaints.dispose_rma(db, company_id, rma_id, audit, **data.model_dump())
    return complaints.get_rma_or_404(db, company_id, rma_id)


@router.post("/rma/{rma_id}/complete", response_model=RMAResponse)
def complete_rma(
    rma_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(APPROVER_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Complete an RMA whose disposition is decided. Admin / Manager / Quality. The
    audit row is required: 503 and nothing saved if it cannot be written."""
    with complaint_transaction(db):
        complaints.complete_rma(db, company_id, rma_id, audit)
    return complaints.get_rma_or_404(db, company_id, rma_id)
