from typing import Any, Dict, List, Optional
from datetime import datetime, date, timedelta
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session, joinedload
from app.db.database import get_db
from app.api.deps import get_current_user, require_role
from app.models.user import User, UserRole
from app.models.quote import Quote, QuoteLine, QuoteStatus
from app.models.rfq_quote import QuoteEstimate, RfqPackage
from app.models.work_order import WorkOrder
from app.models.part import Part
from app.services.quote_pdf_service import build_customer_quote_pdf
from pydantic import BaseModel
from io import BytesIO

router = APIRouter()


class QuoteLineCreate(BaseModel):
    part_id: Optional[int] = None
    description: str
    quantity: float
    unit_price: float
    material_cost: float = 0
    labor_hours: float = 0
    labor_cost: float = 0
    notes: Optional[str] = None


class QuoteCreate(BaseModel):
    customer_name: str
    customer_contact: Optional[str] = None
    customer_email: Optional[str] = None
    customer_phone: Optional[str] = None
    customer_po: Optional[str] = None
    valid_days: int = 30
    lead_time_days: Optional[int] = None
    payment_terms: Optional[str] = None
    notes: Optional[str] = None
    lines: List[QuoteLineCreate] = []


class QuoteUpdate(BaseModel):
    customer_name: Optional[str] = None
    customer_contact: Optional[str] = None
    customer_email: Optional[str] = None
    customer_phone: Optional[str] = None
    customer_po: Optional[str] = None
    valid_until: Optional[date] = None
    lead_time_days: Optional[int] = None
    payment_terms: Optional[str] = None
    notes: Optional[str] = None
    status: Optional[str] = None


class QuoteLineResponse(BaseModel):
    id: int
    line_number: int
    part_id: Optional[int] = None
    part_number: Optional[str] = None
    description: str
    quantity: float
    unit_price: float
    line_total: float
    material_cost: float
    labor_hours: float
    labor_cost: float
    
    class Config:
        from_attributes = True


class AIEstimateLineSummaryResponse(BaseModel):
    part_number: Optional[str] = None
    part_name: str
    quantity: float
    material: Optional[str] = None
    thickness: Optional[str] = None
    flat_area: Optional[float] = None
    cut_length: Optional[float] = None
    bend_count: Optional[int] = None
    hole_count: Optional[int] = None
    finish: Optional[str] = None
    part_total: float = 0.0
    confidence: Dict[str, float] = {}
    sources: Dict[str, List[str]] = {}


class AIEstimateResponse(BaseModel):
    estimate_id: int
    rfq_package_id: Optional[int] = None
    rfq_reference: Optional[str] = None
    totals: Dict[str, float]
    lead_time: Dict[str, Any]
    confidence: Dict[str, Any]
    assumptions: List[Dict[str, Any]] = []
    missing_specs: List[Dict[str, Any]] = []
    source_attribution: Dict[str, List[str]] = {}
    line_summaries: List[AIEstimateLineSummaryResponse] = []


class QuoteResponse(BaseModel):
    id: int
    quote_number: str
    revision: str
    customer_name: str
    customer_contact: Optional[str] = None
    customer_email: Optional[str] = None
    status: str
    quote_date: date
    valid_until: Optional[date] = None
    subtotal: float
    total: float
    lead_time_days: Optional[int] = None
    lines: List[QuoteLineResponse] = []
    work_order_id: Optional[int] = None
    ai_estimate: Optional[AIEstimateResponse] = None
    created_at: datetime
    
    class Config:
        from_attributes = True
        use_enum_values = True


def generate_quote_number(db: Session) -> str:
    today = datetime.now().strftime("%Y%m")
    prefix = f"QTE-{today}-"
    
    last = db.query(Quote).filter(
        Quote.quote_number.like(f"{prefix}%")
    ).order_by(Quote.quote_number.desc()).first()
    
    if last:
        last_num = int(last.quote_number.split("-")[-1])
        new_num = last_num + 1
    else:
        new_num = 1
    
    return f"{prefix}{new_num:04d}"


def _format_date_for_pdf(value: Optional[date]) -> Optional[str]:
    if not value:
        return None
    return value.strftime("%m/%d/%Y")


def _load_ai_estimate(db: Session, quote_id: int) -> Optional[AIEstimateResponse]:
    estimate = (
        db.query(QuoteEstimate)
        .options(joinedload(QuoteEstimate.line_summaries))
        .filter(QuoteEstimate.quote_id == quote_id)
        .order_by(QuoteEstimate.created_at.desc())
        .first()
    )
    if not estimate:
        return None

    rfq_reference = None
    if estimate.rfq_package_id:
        package = db.query(RfqPackage).filter(RfqPackage.id == estimate.rfq_package_id).first()
        if package:
            rfq_reference = package.rfq_reference or package.rfq_number

    return AIEstimateResponse(
        estimate_id=estimate.id,
        rfq_package_id=estimate.rfq_package_id,
        rfq_reference=rfq_reference,
        totals={
            "material": float(estimate.material_total or 0),
            "hardware_consumables": float(estimate.hardware_consumables_total or 0),
            "outside_services": float(estimate.outside_services_total or 0),
            "shop_labor_oh": float(estimate.shop_labor_oh_total or 0),
            "margin": float(estimate.margin_total or 0),
            "grand_total": float(estimate.grand_total or 0),
        },
        lead_time={
            "min_days": estimate.lead_time_min_days,
            "max_days": estimate.lead_time_max_days,
            "confidence": float(estimate.lead_time_confidence or 0),
            "label": (
                f"{estimate.lead_time_min_days}-{estimate.lead_time_max_days} business days"
                if estimate.lead_time_min_days and estimate.lead_time_max_days
                else None
            ),
        },
        confidence={
            "overall": float(estimate.confidence_score or 0),
            "detail": estimate.confidence_detail or {},
        },
        assumptions=estimate.assumptions or [],
        missing_specs=estimate.missing_specs or [],
        source_attribution=estimate.source_attribution or {},
        line_summaries=[
            AIEstimateLineSummaryResponse(
                part_number=line.part_number,
                part_name=line.part_name,
                quantity=float(line.quantity or 0),
                material=line.material,
                thickness=line.thickness,
                flat_area=float(line.flat_area) if line.flat_area is not None else None,
                cut_length=float(line.cut_length) if line.cut_length is not None else None,
                bend_count=line.bend_count,
                hole_count=line.hole_count,
                finish=line.finish,
                part_total=float(line.part_total or 0),
                confidence=line.confidence or {},
                sources=line.sources or {},
            )
            for line in estimate.line_summaries
        ],
    )


@router.get("/", response_model=List[QuoteResponse])
def list_quotes(
    status: Optional[str] = None,
    customer: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    query = db.query(Quote).options(joinedload(Quote.lines))
    
    if status:
        query = query.filter(Quote.status == status)
    else:
        query = query.filter(Quote.status.not_in([QuoteStatus.CONVERTED, QuoteStatus.EXPIRED]))
    
    if customer:
        query = query.filter(Quote.customer_name.ilike(f"%{customer}%"))
    
    quotes = query.order_by(Quote.created_at.desc()).limit(100).all()
    
    result = []
    for q in quotes:
        lines = []
        for l in q.lines:
            lines.append(QuoteLineResponse(
                id=l.id,
                line_number=l.line_number,
                part_id=l.part_id,
                part_number=l.part.part_number if l.part else None,
                description=l.description,
                quantity=l.quantity,
                unit_price=l.unit_price,
                line_total=l.line_total,
                material_cost=l.material_cost,
                labor_hours=l.labor_hours,
                labor_cost=l.labor_cost
            ))
        
        result.append(QuoteResponse(
            id=q.id,
            quote_number=q.quote_number,
            revision=q.revision,
            customer_name=q.customer_name,
            customer_contact=q.customer_contact,
            customer_email=q.customer_email,
            status=q.status.value if hasattr(q.status, 'value') else q.status,
            quote_date=q.quote_date,
            valid_until=q.valid_until,
            subtotal=q.subtotal,
            total=q.total,
            lead_time_days=q.lead_time_days,
            lines=lines,
            work_order_id=q.work_order_id,
            created_at=q.created_at
        ))
    
    return result


@router.post("/", response_model=QuoteResponse)
def create_quote(
    quote_in: QuoteCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    quote_number = generate_quote_number(db)
    
    quote = Quote(
        quote_number=quote_number,
        customer_name=quote_in.customer_name,
        customer_contact=quote_in.customer_contact,
        customer_email=quote_in.customer_email,
        customer_phone=quote_in.customer_phone,
        customer_po=quote_in.customer_po,
        valid_until=date.today() + timedelta(days=quote_in.valid_days),
        lead_time_days=quote_in.lead_time_days,
        payment_terms=quote_in.payment_terms,
        notes=quote_in.notes,
        created_by=current_user.id
    )
    db.add(quote)
    db.flush()
    
    subtotal = 0.0
    for idx, line_data in enumerate(quote_in.lines, 1):
        line_total = line_data.quantity * line_data.unit_price
        line = QuoteLine(
            quote_id=quote.id,
            line_number=idx,
            part_id=line_data.part_id if line_data.part_id and line_data.part_id > 0 else None,
            description=line_data.description,
            quantity=line_data.quantity,
            unit_price=line_data.unit_price,
            line_total=line_total,
            material_cost=line_data.material_cost,
            labor_hours=line_data.labor_hours,
            labor_cost=line_data.labor_cost,
            notes=line_data.notes
        )
        db.add(line)
        subtotal += line_total
    
    quote.subtotal = subtotal
    quote.total = subtotal
    
    db.commit()
    db.refresh(quote)
    
    return quote


@router.get("/{quote_id}", response_model=QuoteResponse)
def get_quote(
    quote_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    quote = db.query(Quote).options(
        joinedload(Quote.lines).joinedload(QuoteLine.part)
    ).filter(Quote.id == quote_id).first()
    
    if not quote:
        raise HTTPException(status_code=404, detail="Quote not found")

    lines = []
    for l in sorted(quote.lines, key=lambda item: item.line_number):
        lines.append(
            QuoteLineResponse(
                id=l.id,
                line_number=l.line_number,
                part_id=l.part_id,
                part_number=l.part.part_number if l.part else None,
                description=l.description,
                quantity=l.quantity,
                unit_price=l.unit_price,
                line_total=l.line_total,
                material_cost=l.material_cost,
                labor_hours=l.labor_hours,
                labor_cost=l.labor_cost,
            )
        )

    ai_estimate = _load_ai_estimate(db, quote.id)

    return QuoteResponse(
        id=quote.id,
        quote_number=quote.quote_number,
        revision=quote.revision,
        customer_name=quote.customer_name,
        customer_contact=quote.customer_contact,
        customer_email=quote.customer_email,
        status=quote.status.value if hasattr(quote.status, "value") else quote.status,
        quote_date=quote.quote_date,
        valid_until=quote.valid_until,
        subtotal=quote.subtotal,
        total=quote.total,
        lead_time_days=quote.lead_time_days,
        lines=lines,
        work_order_id=quote.work_order_id,
        ai_estimate=ai_estimate,
        created_at=quote.created_at,
    )


@router.put("/{quote_id}", response_model=QuoteResponse)
def update_quote(
    quote_id: int,
    quote_in: QuoteUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    quote = db.query(Quote).filter(Quote.id == quote_id).first()
    if not quote:
        raise HTTPException(status_code=404, detail="Quote not found")
    
    update_data = quote_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        if field == "status":
            setattr(quote, field, QuoteStatus(value))
        else:
            setattr(quote, field, value)
    
    db.commit()
    db.refresh(quote)
    return quote


@router.post("/{quote_id}/send")
def send_quote(
    quote_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Mark quote as sent to customer"""
    quote = db.query(Quote).filter(Quote.id == quote_id).first()
    if not quote:
        raise HTTPException(status_code=404, detail="Quote not found")
    
    quote.status = QuoteStatus.SENT
    db.commit()
    
    return {"message": "Quote marked as sent", "quote_number": quote.quote_number}


@router.post("/{quote_id}/convert")
def convert_to_work_order(
    quote_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]))
):
    """Convert accepted quote to work order"""
    quote = db.query(Quote).options(
        joinedload(Quote.lines)
    ).filter(Quote.id == quote_id).first()
    
    if not quote:
        raise HTTPException(status_code=404, detail="Quote not found")
    
    if quote.status not in [QuoteStatus.SENT, QuoteStatus.ACCEPTED]:
        raise HTTPException(status_code=400, detail="Quote must be sent or accepted to convert")
    
    # Find part from first line if available
    part_id = None
    for line in quote.lines:
        if line.part_id:
            part_id = line.part_id
            break
    
    if not part_id:
        raise HTTPException(status_code=400, detail="Quote must have at least one line with a part to convert")
    
    # Generate WO number
    today = datetime.now().strftime("%Y%m%d")
    prefix = f"WO-{today}-"
    last_wo = db.query(WorkOrder).filter(
        WorkOrder.work_order_number.like(f"{prefix}%")
    ).order_by(WorkOrder.work_order_number.desc()).first()
    
    if last_wo:
        last_num = int(last_wo.work_order_number.split("-")[-1])
        new_num = last_num + 1
    else:
        new_num = 1
    wo_number = f"{prefix}{new_num:03d}"
    
    # Create work order
    wo = WorkOrder(
        work_order_number=wo_number,
        part_id=part_id,
        quantity_ordered=quote.lines[0].quantity if quote.lines else 1,
        customer_name=quote.customer_name,
        customer_po=quote.customer_po,
        notes=f"Converted from quote {quote.quote_number}",
        created_by=current_user.id
    )
    db.add(wo)
    db.flush()
    
    # Update quote
    quote.status = QuoteStatus.CONVERTED
    quote.work_order_id = wo.id
    
    db.commit()
    
    return {
        "message": "Quote converted to work order",
        "quote_number": quote.quote_number,
        "work_order_id": wo.id,
        "work_order_number": wo.work_order_number
    }


@router.post("/{quote_id}/generate-pdf")
def generate_quote_pdf(
    quote_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Generate customer-ready quote PDF (no operation-time line items)."""
    quote = db.query(Quote).options(joinedload(Quote.lines).joinedload(QuoteLine.part)).filter(Quote.id == quote_id).first()
    if not quote:
        raise HTTPException(status_code=404, detail="Quote not found")

    ai_estimate = _load_ai_estimate(db, quote.id)
    line_summaries: List[Dict[str, Any]] = []
    assumptions: List[Dict[str, Any]] = []
    rfq_reference = None
    lead_time_label = f"{quote.lead_time_days} business days" if quote.lead_time_days else None

    if ai_estimate:
        rfq_reference = ai_estimate.rfq_reference
        assumptions = ai_estimate.assumptions
        lead_time_label = ai_estimate.lead_time.get("label") or lead_time_label
        line_summaries = [
            {
                "part_display": f"{line.part_number or '-'} - {line.part_name}",
                "qty": line.quantity,
                "material": line.material,
                "thickness": line.thickness,
                "finish": line.finish,
                "part_total": line.part_total,
            }
            for line in ai_estimate.line_summaries
        ]
    else:
        for line in quote.lines:
            line_summaries.append(
                {
                    "part_display": (
                        f"{line.part.part_number} - {line.description}"
                        if line.part
                        else line.description
                    ),
                    "qty": line.quantity,
                    "material": None,
                    "thickness": None,
                    "finish": None,
                    "part_total": line.line_total,
                }
            )

    pdf_bytes = build_customer_quote_pdf(
        quote_number=quote.quote_number,
        revision=quote.revision or "A",
        customer_name=quote.customer_name,
        customer_contact=quote.customer_contact,
        customer_email=quote.customer_email,
        rfq_reference=rfq_reference,
        quote_date=_format_date_for_pdf(quote.quote_date) or "",
        valid_until=_format_date_for_pdf(quote.valid_until),
        lead_time_label=lead_time_label,
        total_amount=float(quote.total or 0),
        line_summaries=line_summaries,
        assumptions=assumptions,
        exclusions=[
            "Quote excludes taxes, freight, and duties unless stated otherwise.",
            "Subject to drawing/specification review at order entry.",
            "Operation-level cycle times are internal and not included in customer quote.",
        ],
    )

    filename = f"{quote.quote_number}.pdf"
    return StreamingResponse(
        BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/{quote_id}/lines", response_model=QuoteLineResponse)
def add_quote_line(
    quote_id: int,
    line_in: QuoteLineCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    quote = db.query(Quote).filter(Quote.id == quote_id).first()
    if not quote:
        raise HTTPException(status_code=404, detail="Quote not found")
    
    if quote.status not in [QuoteStatus.DRAFT, QuoteStatus.PENDING]:
        raise HTTPException(status_code=400, detail="Can only add lines to draft or pending quotes")
    
    # Get next line number
    from sqlalchemy import func
    max_line = db.query(func.max(QuoteLine.line_number)).filter(
        QuoteLine.quote_id == quote_id
    ).scalar() or 0
    
    line_total = line_in.quantity * line_in.unit_price
    line = QuoteLine(
        quote_id=quote_id,
        line_number=max_line + 1,
        part_id=line_in.part_id if line_in.part_id and line_in.part_id > 0 else None,
        description=line_in.description,
        quantity=line_in.quantity,
        unit_price=line_in.unit_price,
        line_total=line_total,
        material_cost=line_in.material_cost,
        labor_hours=line_in.labor_hours,
        labor_cost=line_in.labor_cost,
        notes=line_in.notes
    )
    db.add(line)
    
    # Update quote totals
    quote.subtotal += line_total
    quote.total = quote.subtotal
    
    db.commit()
    db.refresh(line)
    
    part = db.query(Part).filter(Part.id == line.part_id).first() if line.part_id else None
    
    return QuoteLineResponse(
        id=line.id,
        line_number=line.line_number,
        part_id=line.part_id,
        part_number=part.part_number if part else None,
        description=line.description,
        quantity=line.quantity,
        unit_price=line.unit_price,
        line_total=line.line_total,
        material_cost=line.material_cost,
        labor_hours=line.labor_hours,
        labor_cost=line.labor_cost
    )
