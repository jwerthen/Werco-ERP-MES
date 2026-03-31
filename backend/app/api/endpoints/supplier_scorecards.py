from typing import List, Optional
from datetime import datetime, date, timedelta
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.orm import Session, joinedload
from app.db.database import get_db
from app.api.deps import get_current_user, require_role
from app.models.user import User, UserRole
from app.models.purchasing import Vendor, PurchaseOrder, PurchaseOrderLine, POReceipt, POStatus
from app.models.quality import NonConformanceReport
from app.models.supplier_scorecard import (
    SupplierScorecard, SupplierAudit, ApprovedSupplierList, ScorecardPeriod
)

router = APIRouter()


# ============ Pydantic Schemas ============

class ScorecardCreate(BaseModel):
    vendor_id: int
    period_type: str = "quarterly"
    period_start: date
    period_end: date
    quality_score: float = 0.0
    quality_weight: float = 0.40
    delivery_score: float = 0.0
    delivery_weight: float = 0.30
    responsiveness_score: float = 0.0
    responsiveness_weight: float = 0.15
    price_score: float = 0.0
    price_weight: float = 0.15
    total_pos: int = 0
    total_lines: int = 0
    on_time_deliveries: int = 0
    late_deliveries: int = 0
    total_received_qty: float = 0.0
    rejected_qty: float = 0.0
    ncr_count: int = 0
    car_count: int = 0
    notes: Optional[str] = None
    action_items: Optional[str] = None
    responsiveness_score_manual: Optional[float] = None
    price_score_manual: Optional[float] = None


class ScorecardUpdate(BaseModel):
    quality_score: Optional[float] = None
    quality_weight: Optional[float] = None
    delivery_score: Optional[float] = None
    delivery_weight: Optional[float] = None
    responsiveness_score: Optional[float] = None
    responsiveness_weight: Optional[float] = None
    price_score: Optional[float] = None
    price_weight: Optional[float] = None
    notes: Optional[str] = None
    action_items: Optional[str] = None
    total_pos: Optional[int] = None
    total_lines: Optional[int] = None
    on_time_deliveries: Optional[int] = None
    late_deliveries: Optional[int] = None
    total_received_qty: Optional[float] = None
    rejected_qty: Optional[float] = None
    ncr_count: Optional[int] = None
    car_count: Optional[int] = None


class ScorecardResponse(BaseModel):
    id: int
    vendor_id: int
    vendor_name: Optional[str] = None
    vendor_code: Optional[str] = None
    period_type: str
    period_start: date
    period_end: date
    quality_score: float
    quality_weight: float
    delivery_score: float
    delivery_weight: float
    responsiveness_score: float
    responsiveness_weight: float
    price_score: float
    price_weight: float
    overall_score: float
    rating: Optional[str] = None
    total_pos: int
    total_lines: int
    on_time_deliveries: int
    late_deliveries: int
    total_received_qty: float
    rejected_qty: float
    ncr_count: int
    car_count: int
    notes: Optional[str] = None
    action_items: Optional[str] = None
    evaluated_by: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class CalculateRequest(BaseModel):
    period_start: date
    period_end: date
    period_type: str = "quarterly"
    responsiveness_score: float = 80.0
    price_score: float = 80.0


class AuditCreate(BaseModel):
    vendor_id: int
    audit_type: str
    audit_date: date
    next_audit_date: Optional[date] = None
    auditor: Optional[str] = None
    scope: Optional[str] = None
    findings: Optional[str] = None
    corrective_actions: Optional[str] = None
    result: Optional[str] = None
    score: Optional[float] = None
    notes: Optional[str] = None


class AuditUpdate(BaseModel):
    audit_type: Optional[str] = None
    audit_date: Optional[date] = None
    next_audit_date: Optional[date] = None
    auditor: Optional[str] = None
    scope: Optional[str] = None
    findings: Optional[str] = None
    corrective_actions: Optional[str] = None
    result: Optional[str] = None
    score: Optional[float] = None
    notes: Optional[str] = None


class AuditResponse(BaseModel):
    id: int
    vendor_id: int
    vendor_name: Optional[str] = None
    vendor_code: Optional[str] = None
    audit_type: str
    audit_date: date
    next_audit_date: Optional[date] = None
    auditor: Optional[str] = None
    scope: Optional[str] = None
    findings: Optional[str] = None
    corrective_actions: Optional[str] = None
    result: Optional[str] = None
    score: Optional[float] = None
    notes: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class ASLCreate(BaseModel):
    vendor_id: int
    approval_status: str = "approved"
    approved_date: Optional[date] = None
    scope: Optional[str] = None
    certifications_required: Optional[str] = None
    certifications_verified: bool = False
    next_review_date: Optional[date] = None
    review_frequency_months: int = 12
    notes: Optional[str] = None


class ASLUpdate(BaseModel):
    approval_status: Optional[str] = None
    approved_date: Optional[date] = None
    scope: Optional[str] = None
    certifications_required: Optional[str] = None
    certifications_verified: Optional[bool] = None
    next_review_date: Optional[date] = None
    review_frequency_months: Optional[int] = None
    notes: Optional[str] = None


class ASLResponse(BaseModel):
    id: int
    vendor_id: int
    vendor_name: Optional[str] = None
    vendor_code: Optional[str] = None
    approval_status: str
    approved_date: Optional[date] = None
    approved_by: Optional[int] = None
    scope: Optional[str] = None
    certifications_required: Optional[str] = None
    certifications_verified: bool
    last_review_date: Optional[date] = None
    next_review_date: Optional[date] = None
    review_frequency_months: int
    notes: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# ============ Helper Functions ============

def calculate_rating(score: float) -> str:
    """Determine rating from overall score."""
    if score >= 90:
        return "Excellent"
    elif score >= 80:
        return "Good"
    elif score >= 70:
        return "Acceptable"
    elif score >= 60:
        return "Probationary"
    else:
        return "Disqualified"


def calculate_overall(scorecard: SupplierScorecard) -> float:
    """Calculate weighted overall score."""
    return (
        scorecard.quality_score * scorecard.quality_weight +
        scorecard.delivery_score * scorecard.delivery_weight +
        scorecard.responsiveness_score * scorecard.responsiveness_weight +
        scorecard.price_score * scorecard.price_weight
    )


def scorecard_to_response(sc: SupplierScorecard) -> dict:
    """Convert a scorecard ORM object to a response dict with vendor info."""
    data = {
        "id": sc.id,
        "vendor_id": sc.vendor_id,
        "vendor_name": sc.vendor.name if sc.vendor else None,
        "vendor_code": sc.vendor.code if sc.vendor else None,
        "period_type": sc.period_type.value if isinstance(sc.period_type, ScorecardPeriod) else sc.period_type,
        "period_start": sc.period_start,
        "period_end": sc.period_end,
        "quality_score": sc.quality_score,
        "quality_weight": sc.quality_weight,
        "delivery_score": sc.delivery_score,
        "delivery_weight": sc.delivery_weight,
        "responsiveness_score": sc.responsiveness_score,
        "responsiveness_weight": sc.responsiveness_weight,
        "price_score": sc.price_score,
        "price_weight": sc.price_weight,
        "overall_score": sc.overall_score,
        "rating": sc.rating,
        "total_pos": sc.total_pos,
        "total_lines": sc.total_lines,
        "on_time_deliveries": sc.on_time_deliveries,
        "late_deliveries": sc.late_deliveries,
        "total_received_qty": sc.total_received_qty,
        "rejected_qty": sc.rejected_qty,
        "ncr_count": sc.ncr_count,
        "car_count": sc.car_count,
        "notes": sc.notes,
        "action_items": sc.action_items,
        "evaluated_by": sc.evaluated_by,
        "created_at": sc.created_at,
        "updated_at": sc.updated_at,
    }
    return data


def audit_to_response(a: SupplierAudit) -> dict:
    return {
        "id": a.id,
        "vendor_id": a.vendor_id,
        "vendor_name": a.vendor.name if a.vendor else None,
        "vendor_code": a.vendor.code if a.vendor else None,
        "audit_type": a.audit_type,
        "audit_date": a.audit_date,
        "next_audit_date": a.next_audit_date,
        "auditor": a.auditor,
        "scope": a.scope,
        "findings": a.findings,
        "corrective_actions": a.corrective_actions,
        "result": a.result,
        "score": a.score,
        "notes": a.notes,
        "created_at": a.created_at,
        "updated_at": a.updated_at,
    }


def asl_to_response(a: ApprovedSupplierList) -> dict:
    return {
        "id": a.id,
        "vendor_id": a.vendor_id,
        "vendor_name": a.vendor.name if a.vendor else None,
        "vendor_code": a.vendor.code if a.vendor else None,
        "approval_status": a.approval_status,
        "approved_date": a.approved_date,
        "approved_by": a.approved_by,
        "scope": a.scope,
        "certifications_required": a.certifications_required,
        "certifications_verified": a.certifications_verified,
        "last_review_date": a.last_review_date,
        "next_review_date": a.next_review_date,
        "review_frequency_months": a.review_frequency_months,
        "notes": a.notes,
        "created_at": a.created_at,
        "updated_at": a.updated_at,
    }


# ============ Scorecard Endpoints ============

@router.get("/supplier-scorecards/dashboard")
def scorecard_dashboard(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Dashboard stats: avg score, suppliers below threshold, audits due, probationary count."""
    scorecards = db.query(SupplierScorecard).all()

    # Get latest scorecard per vendor
    latest_by_vendor = {}
    for sc in scorecards:
        if sc.vendor_id not in latest_by_vendor or sc.period_end > latest_by_vendor[sc.vendor_id].period_end:
            latest_by_vendor[sc.vendor_id] = sc

    latest = list(latest_by_vendor.values())

    avg_score = sum(s.overall_score for s in latest) / len(latest) if latest else 0
    below_threshold = sum(1 for s in latest if s.overall_score < 70)
    probationary_count = sum(1 for s in latest if s.rating == "Probationary")
    disqualified_count = sum(1 for s in latest if s.rating == "Disqualified")

    # Audits due within 30 days
    thirty_days = date.today() + timedelta(days=30)
    audits_due = db.query(SupplierAudit).filter(
        SupplierAudit.next_audit_date != None,
        SupplierAudit.next_audit_date <= thirty_days
    ).count()

    # Reviews due (ASL)
    reviews_due = db.query(ApprovedSupplierList).filter(
        ApprovedSupplierList.next_review_date != None,
        ApprovedSupplierList.next_review_date <= thirty_days
    ).count()

    # Top and worst performer
    top_performer = max(latest, key=lambda s: s.overall_score) if latest else None
    worst_performer = min(latest, key=lambda s: s.overall_score) if latest else None

    return {
        "avg_score": round(avg_score, 1),
        "total_vendors_scored": len(latest),
        "below_threshold": below_threshold,
        "probationary_count": probationary_count,
        "disqualified_count": disqualified_count,
        "audits_due_30_days": audits_due,
        "reviews_due_30_days": reviews_due,
        "top_performer": {
            "vendor_id": top_performer.vendor_id,
            "vendor_name": top_performer.vendor.name if top_performer.vendor else "Unknown",
            "score": round(top_performer.overall_score, 1),
            "rating": top_performer.rating,
        } if top_performer else None,
        "worst_performer": {
            "vendor_id": worst_performer.vendor_id,
            "vendor_name": worst_performer.vendor.name if worst_performer.vendor else "Unknown",
            "score": round(worst_performer.overall_score, 1),
            "rating": worst_performer.rating,
        } if worst_performer else None,
    }


@router.get("/supplier-scorecards/ranking")
def scorecard_ranking(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """All vendors ranked by their latest overall score."""
    scorecards = db.query(SupplierScorecard).options(
        joinedload(SupplierScorecard.vendor)
    ).all()

    # Get latest scorecard per vendor
    latest_by_vendor = {}
    for sc in scorecards:
        if sc.vendor_id not in latest_by_vendor or sc.period_end > latest_by_vendor[sc.vendor_id].period_end:
            latest_by_vendor[sc.vendor_id] = sc

    ranked = sorted(latest_by_vendor.values(), key=lambda s: s.overall_score, reverse=True)
    return [scorecard_to_response(sc) for sc in ranked]


@router.get("/supplier-scorecards/vendor/{vendor_id}/history")
def vendor_scorecard_history(
    vendor_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Performance history for a specific vendor over time."""
    vendor = db.query(Vendor).filter(Vendor.id == vendor_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    scorecards = db.query(SupplierScorecard).options(
        joinedload(SupplierScorecard.vendor)
    ).filter(
        SupplierScorecard.vendor_id == vendor_id
    ).order_by(SupplierScorecard.period_start.asc()).all()

    return [scorecard_to_response(sc) for sc in scorecards]


@router.get("/supplier-scorecards/{scorecard_id}", response_model=ScorecardResponse)
def get_scorecard(
    scorecard_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    sc = db.query(SupplierScorecard).options(
        joinedload(SupplierScorecard.vendor)
    ).filter(SupplierScorecard.id == scorecard_id).first()
    if not sc:
        raise HTTPException(status_code=404, detail="Scorecard not found")
    return scorecard_to_response(sc)


@router.get("/supplier-scorecards/", response_model=List[ScorecardResponse])
def list_scorecards(
    vendor_id: Optional[int] = None,
    period_type: Optional[str] = None,
    rating: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List scorecards with filters."""
    query = db.query(SupplierScorecard).options(
        joinedload(SupplierScorecard.vendor)
    )
    if vendor_id:
        query = query.filter(SupplierScorecard.vendor_id == vendor_id)
    if period_type:
        query = query.filter(SupplierScorecard.period_type == period_type)
    if rating:
        query = query.filter(SupplierScorecard.rating == rating)

    scorecards = query.order_by(
        SupplierScorecard.period_end.desc()
    ).offset(skip).limit(limit).all()
    return [scorecard_to_response(sc) for sc in scorecards]


@router.post("/supplier-scorecards/", response_model=ScorecardResponse)
def create_scorecard(
    data: ScorecardCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER]))
):
    """Create a new scorecard."""
    vendor = db.query(Vendor).filter(Vendor.id == data.vendor_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    sc = SupplierScorecard(
        vendor_id=data.vendor_id,
        period_type=data.period_type,
        period_start=data.period_start,
        period_end=data.period_end,
        quality_score=data.quality_score,
        quality_weight=data.quality_weight,
        delivery_score=data.delivery_score,
        delivery_weight=data.delivery_weight,
        responsiveness_score=data.responsiveness_score,
        responsiveness_weight=data.responsiveness_weight,
        price_score=data.price_score,
        price_weight=data.price_weight,
        total_pos=data.total_pos,
        total_lines=data.total_lines,
        on_time_deliveries=data.on_time_deliveries,
        late_deliveries=data.late_deliveries,
        total_received_qty=data.total_received_qty,
        rejected_qty=data.rejected_qty,
        ncr_count=data.ncr_count,
        car_count=data.car_count,
        notes=data.notes,
        action_items=data.action_items,
        evaluated_by=current_user.id,
    )
    sc.overall_score = calculate_overall(sc)
    sc.rating = calculate_rating(sc.overall_score)

    db.add(sc)
    db.commit()
    db.refresh(sc)
    return scorecard_to_response(sc)


@router.put("/supplier-scorecards/{scorecard_id}", response_model=ScorecardResponse)
def update_scorecard(
    scorecard_id: int,
    data: ScorecardUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER]))
):
    """Update an existing scorecard."""
    sc = db.query(SupplierScorecard).options(
        joinedload(SupplierScorecard.vendor)
    ).filter(SupplierScorecard.id == scorecard_id).first()
    if not sc:
        raise HTTPException(status_code=404, detail="Scorecard not found")

    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(sc, field, value)

    sc.overall_score = calculate_overall(sc)
    sc.rating = calculate_rating(sc.overall_score)
    sc.evaluated_by = current_user.id

    db.commit()
    db.refresh(sc)
    return scorecard_to_response(sc)


@router.post("/supplier-scorecards/calculate/{vendor_id}", response_model=ScorecardResponse)
def auto_calculate_scorecard(
    vendor_id: int,
    data: CalculateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER]))
):
    """Auto-calculate scorecard from PO/receipt/NCR data for a vendor and date range."""
    vendor = db.query(Vendor).filter(Vendor.id == vendor_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    # Get POs in the period
    pos = db.query(PurchaseOrder).filter(
        PurchaseOrder.vendor_id == vendor_id,
        PurchaseOrder.order_date >= data.period_start,
        PurchaseOrder.order_date <= data.period_end,
        PurchaseOrder.status != POStatus.CANCELLED,
        PurchaseOrder.status != POStatus.DRAFT,
    ).all()

    total_pos = len(pos)
    po_ids = [po.id for po in pos]

    # Get PO lines
    lines = []
    if po_ids:
        lines = db.query(PurchaseOrderLine).filter(
            PurchaseOrderLine.purchase_order_id.in_(po_ids)
        ).all()
    total_lines = len(lines)
    line_ids = [l.id for l in lines]

    # Get receipts
    receipts = []
    if line_ids:
        receipts = db.query(POReceipt).filter(
            POReceipt.po_line_id.in_(line_ids)
        ).all()

    total_received_qty = sum(r.quantity_received for r in receipts)
    rejected_qty = sum(r.quantity_rejected for r in receipts)

    # Delivery: count on-time vs late based on PO required_date vs receipt date
    on_time = 0
    late = 0
    for receipt in receipts:
        po_line = None
        for l in lines:
            if l.id == receipt.po_line_id:
                po_line = l
                break
        if po_line:
            required = po_line.required_date
            # Fall back to the PO-level required_date
            if not required:
                for po in pos:
                    if po.id == po_line.purchase_order_id:
                        required = po.required_date
                        break
            if required and receipt.received_at:
                if receipt.received_at.date() <= required:
                    on_time += 1
                else:
                    late += 1
            else:
                # If no required date, count as on-time
                on_time += 1

    total_deliveries = on_time + late

    # Quality score: (1 - rejected/received) * 100
    if total_received_qty > 0:
        quality_score = (1 - rejected_qty / total_received_qty) * 100
    else:
        quality_score = 100.0

    # Delivery score
    if total_deliveries > 0:
        delivery_score = (on_time / total_deliveries) * 100
    else:
        delivery_score = 100.0

    # NCR count for this vendor in period (based on receipt linkage or supplier_name)
    ncr_count = 0
    if line_ids:
        receipt_ids = [r.id for r in receipts]
        if receipt_ids:
            ncr_count = db.query(NonConformanceReport).filter(
                NonConformanceReport.receipt_id.in_(receipt_ids),
                NonConformanceReport.detected_date >= data.period_start,
                NonConformanceReport.detected_date <= data.period_end,
            ).count()

    # CAR count linked to NCRs
    car_count = 0
    if ncr_count > 0 and line_ids:
        receipt_ids = [r.id for r in receipts]
        ncrs_with_car = db.query(NonConformanceReport).filter(
            NonConformanceReport.receipt_id.in_(receipt_ids),
            NonConformanceReport.car_id != None,
            NonConformanceReport.detected_date >= data.period_start,
            NonConformanceReport.detected_date <= data.period_end,
        ).all()
        car_ids = set(n.car_id for n in ncrs_with_car if n.car_id)
        car_count = len(car_ids)

    # Responsiveness and price are manual inputs (passed from frontend)
    responsiveness_score = data.responsiveness_score
    price_score = data.price_score

    # Create the scorecard
    sc = SupplierScorecard(
        vendor_id=vendor_id,
        period_type=data.period_type,
        period_start=data.period_start,
        period_end=data.period_end,
        quality_score=round(quality_score, 1),
        delivery_score=round(delivery_score, 1),
        responsiveness_score=responsiveness_score,
        price_score=price_score,
        total_pos=total_pos,
        total_lines=total_lines,
        on_time_deliveries=on_time,
        late_deliveries=late,
        total_received_qty=total_received_qty,
        rejected_qty=rejected_qty,
        ncr_count=ncr_count,
        car_count=car_count,
        evaluated_by=current_user.id,
    )
    sc.overall_score = round(calculate_overall(sc), 1)
    sc.rating = calculate_rating(sc.overall_score)

    db.add(sc)
    db.commit()
    db.refresh(sc)
    return scorecard_to_response(sc)


# ============ Audit Endpoints ============

@router.get("/supplier-audits/due-soon")
def audits_due_soon(
    days: int = 30,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get audits due within N days."""
    cutoff = date.today() + timedelta(days=days)
    audits = db.query(SupplierAudit).options(
        joinedload(SupplierAudit.vendor)
    ).filter(
        SupplierAudit.next_audit_date != None,
        SupplierAudit.next_audit_date <= cutoff
    ).order_by(SupplierAudit.next_audit_date.asc()).all()
    return [audit_to_response(a) for a in audits]


@router.get("/supplier-audits/", response_model=List[AuditResponse])
def list_audits(
    vendor_id: Optional[int] = None,
    result: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    query = db.query(SupplierAudit).options(joinedload(SupplierAudit.vendor))
    if vendor_id:
        query = query.filter(SupplierAudit.vendor_id == vendor_id)
    if result:
        query = query.filter(SupplierAudit.result == result)
    audits = query.order_by(SupplierAudit.audit_date.desc()).offset(skip).limit(limit).all()
    return [audit_to_response(a) for a in audits]


@router.post("/supplier-audits/", response_model=AuditResponse)
def create_audit(
    data: AuditCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER]))
):
    vendor = db.query(Vendor).filter(Vendor.id == data.vendor_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    audit = SupplierAudit(**data.model_dump())
    db.add(audit)
    db.commit()
    db.refresh(audit)
    return audit_to_response(audit)


@router.put("/supplier-audits/{audit_id}", response_model=AuditResponse)
def update_audit(
    audit_id: int,
    data: AuditUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER]))
):
    audit = db.query(SupplierAudit).options(
        joinedload(SupplierAudit.vendor)
    ).filter(SupplierAudit.id == audit_id).first()
    if not audit:
        raise HTTPException(status_code=404, detail="Audit not found")

    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(audit, field, value)

    db.commit()
    db.refresh(audit)
    return audit_to_response(audit)


# ============ Approved Supplier List Endpoints ============

@router.get("/approved-suppliers/", response_model=List[ASLResponse])
def list_approved_suppliers(
    status: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    query = db.query(ApprovedSupplierList).options(joinedload(ApprovedSupplierList.vendor))
    if status:
        query = query.filter(ApprovedSupplierList.approval_status == status)
    entries = query.order_by(ApprovedSupplierList.id.desc()).offset(skip).limit(limit).all()
    return [asl_to_response(a) for a in entries]


@router.get("/approved-suppliers/{asl_id}", response_model=ASLResponse)
def get_approved_supplier(
    asl_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    entry = db.query(ApprovedSupplierList).options(
        joinedload(ApprovedSupplierList.vendor)
    ).filter(ApprovedSupplierList.id == asl_id).first()
    if not entry:
        raise HTTPException(status_code=404, detail="ASL entry not found")
    return asl_to_response(entry)


@router.post("/approved-suppliers/", response_model=ASLResponse)
def create_approved_supplier(
    data: ASLCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER]))
):
    vendor = db.query(Vendor).filter(Vendor.id == data.vendor_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    existing = db.query(ApprovedSupplierList).filter(
        ApprovedSupplierList.vendor_id == data.vendor_id
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="Vendor already has an ASL entry")

    entry = ApprovedSupplierList(
        **data.model_dump(),
        approved_by=current_user.id,
        last_review_date=date.today(),
    )
    if not entry.approved_date:
        entry.approved_date = date.today()

    db.add(entry)
    db.commit()
    db.refresh(entry)
    return asl_to_response(entry)


@router.put("/approved-suppliers/{asl_id}", response_model=ASLResponse)
def update_approved_supplier(
    asl_id: int,
    data: ASLUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER]))
):
    entry = db.query(ApprovedSupplierList).options(
        joinedload(ApprovedSupplierList.vendor)
    ).filter(ApprovedSupplierList.id == asl_id).first()
    if not entry:
        raise HTTPException(status_code=404, detail="ASL entry not found")

    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(entry, field, value)

    entry.last_review_date = date.today()

    db.commit()
    db.refresh(entry)
    return asl_to_response(entry)
