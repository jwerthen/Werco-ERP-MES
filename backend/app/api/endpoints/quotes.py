import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload, selectinload

from app.api.deps import get_current_company_id, get_current_user, require_role
from app.db.database import get_db
from app.db.locks import acquire_generator_lock
from app.models.part import Part
from app.models.quote import Quote, QuoteLine, QuoteStatus
from app.models.rfq_quote import QuoteEstimate, RfqPackage
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder

router = APIRouter()


class QuoteLineCreate(BaseModel):
    # quantity/unit_price are bounded to match the DB CHECKs migration 080
    # restored (chk_quote_lines_quantity_positive / _unit_price_non_negative), so
    # a typo'd 0 quantity is a 422 at the boundary rather than an IntegrityError
    # 500 at flush. Same gt/ge precedent as PurchaseOrderLineCreate.
    id: Optional[int] = None
    part_id: Optional[int] = None
    description: str
    quantity: float = Field(..., gt=0, allow_inf_nan=False)
    unit_price: float = Field(..., ge=0, allow_inf_nan=False)
    material_cost: float = Field(default=0.0, allow_inf_nan=False)
    labor_hours: float = Field(default=0.0, allow_inf_nan=False)
    labor_cost: float = Field(default=0.0, allow_inf_nan=False)
    notes: Optional[str] = None


class QuoteCreate(BaseModel):
    request_key: Optional[str] = Field(default=None, min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    customer_name: str
    customer_contact: Optional[str] = None
    customer_email: Optional[str] = None
    customer_phone: Optional[str] = None
    customer_po: Optional[str] = None
    valid_days: int = 30
    lead_time_days: Optional[int] = None
    payment_terms: Optional[str] = None
    notes: Optional[str] = None
    internal_notes: Optional[str] = None
    lines: List[QuoteLineCreate] = Field(default_factory=list)


class QuoteUpdate(BaseModel):
    # The editor echoes the timestamp it reviewed. Omitted for legacy API callers.
    expected_updated_at: Optional[datetime] = None
    customer_name: Optional[str] = None
    customer_contact: Optional[str] = None
    customer_email: Optional[str] = None
    customer_phone: Optional[str] = None
    customer_po: Optional[str] = None
    valid_until: Optional[date] = None
    lead_time_days: Optional[int] = None
    payment_terms: Optional[str] = None
    notes: Optional[str] = None
    status: Optional[QuoteStatus] = None
    internal_notes: Optional[str] = None
    lines: Optional[List[QuoteLineCreate]] = None


class QuoteConvertRequest(BaseModel):
    line_ids: Optional[List[int]] = None
    acknowledge_unlinked: bool = False


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
    notes: Optional[str] = None
    work_order_id: Optional[int] = None
    work_order_number: Optional[str] = None

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
    customer_phone: Optional[str] = None
    customer_po: Optional[str] = None
    payment_terms: Optional[str] = None
    notes: Optional[str] = None
    internal_notes: Optional[str] = None
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
    updated_at: datetime

    class Config:
        from_attributes = True
        use_enum_values = True


def generate_quote_number(db: Session) -> str:
    acquire_generator_lock(db, "quote_number")
    today = datetime.now().strftime("%Y%m")
    prefix = f"QTE-{today}-"

    last = db.query(Quote).filter(Quote.quote_number.like(f"{prefix}%")).order_by(Quote.quote_number.desc()).first()

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


def _quote_response(quote: Quote, ai_estimate=None) -> QuoteResponse:
    return QuoteResponse(
        **{
            key: getattr(quote, key)
            for key in (
                "id",
                "quote_number",
                "revision",
                "customer_name",
                "customer_contact",
                "customer_email",
                "customer_phone",
                "customer_po",
                "payment_terms",
                "notes",
                "internal_notes",
                "quote_date",
                "valid_until",
                "subtotal",
                "total",
                "lead_time_days",
                "work_order_id",
                "created_at",
            )
        },
        status=quote.status.value if hasattr(quote.status, "value") else quote.status,
        ai_estimate=ai_estimate,
        updated_at=quote.updated_at or quote.created_at,
        lines=[
            QuoteLineResponse(
                **{
                    key: getattr(line, key)
                    for key in (
                        "id",
                        "line_number",
                        "part_id",
                        "description",
                        "quantity",
                        "unit_price",
                        "line_total",
                        "material_cost",
                        "labor_hours",
                        "labor_cost",
                        "notes",
                        "work_order_id",
                    )
                },
                part_number=line.part.part_number if line.part else None,
                work_order_number=line.work_order.work_order_number if line.work_order else None,
            )
            for line in sorted(quote.lines, key=lambda line: (line.line_number, line.id))
        ],
    )


def _validate_quote_parts(db: Session, lines: List[QuoteLineCreate], company_id: int) -> None:
    ids = {line.part_id for line in lines if line.part_id and line.part_id > 0}
    found = (
        {part.id for part in db.query(Part).filter(Part.id.in_(ids), Part.company_id == company_id)} if ids else set()
    )
    if found != ids:
        raise HTTPException(status_code=422, detail="One or more quote parts are unavailable in this company")


@router.get("/", response_model=List[QuoteResponse])
def list_quotes(
    status: Optional[str] = None,
    customer: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = Query(100, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Page quotes in stable order; status=all explicitly includes full history."""
    query = (
        db.query(Quote)
        .options(
            selectinload(Quote.lines).joinedload(QuoteLine.part),
            selectinload(Quote.lines).joinedload(QuoteLine.work_order),
        )
        .filter(Quote.company_id == company_id)
    )
    if status and status != "all":
        if status not in {item.value for item in QuoteStatus}:
            raise HTTPException(status_code=422, detail="Unknown quote status")
        query = query.filter(Quote.status == status)
    elif not status:
        query = query.filter(Quote.status.not_in([QuoteStatus.CONVERTED, QuoteStatus.EXPIRED]))
    if customer:
        query = query.filter(Quote.customer_name.ilike(f"%{customer}%"))
    if search:
        query = query.filter(or_(Quote.quote_number.ilike(f"%{search}%"), Quote.customer_name.ilike(f"%{search}%")))
    quotes = query.order_by(Quote.created_at.desc(), Quote.id.desc()).offset(offset).limit(limit).all()
    return [_quote_response(quote) for quote in quotes]


@router.post("/", response_model=QuoteResponse)
def create_quote(
    quote_in: QuoteCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    request_hash = None
    if quote_in.request_key:
        # Use the existing global quote-number transaction lock so a retried create
        # observes the first committed outcome before it can allocate another number.
        acquire_generator_lock(db, "quote_number")
        canonical = quote_in.model_dump(mode="json", exclude={"request_key"})
        for line in canonical["lines"]:
            line.pop("id", None)  # IDs are not used when a new line is created.
            line["part_id"] = line["part_id"] if line["part_id"] and line["part_id"] > 0 else None
        request_hash = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode(
                "utf-8"
            )
        ).hexdigest()
        existing = (
            db.query(Quote).filter(Quote.company_id == company_id, Quote.request_key == quote_in.request_key).first()
        )
        if existing:
            if existing.request_hash != request_hash:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "This create request already saved a different quote. Open the existing quote to review it before making changes.",
                        "quote_id": existing.id,
                        "quote_number": existing.quote_number,
                    },
                )
            return _quote_response(existing)
    _validate_quote_parts(db, quote_in.lines, company_id)
    quote_number = generate_quote_number(db)

    quote = Quote(
        request_key=quote_in.request_key,
        request_hash=request_hash,
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
        internal_notes=quote_in.internal_notes,
        created_by=current_user.id,
    )
    quote.company_id = company_id
    db.add(quote)
    db.flush()

    subtotal = 0.0
    for idx, line_data in enumerate(quote_in.lines, 1):
        line_total = line_data.quantity * line_data.unit_price
        line = QuoteLine(
            quote_id=quote.id,
            company_id=company_id,
            line_number=idx,
            part_id=line_data.part_id if line_data.part_id and line_data.part_id > 0 else None,
            description=line_data.description,
            quantity=line_data.quantity,
            unit_price=line_data.unit_price,
            line_total=line_total,
            material_cost=line_data.material_cost,
            labor_hours=line_data.labor_hours,
            labor_cost=line_data.labor_cost,
            notes=line_data.notes,
        )
        db.add(line)
        subtotal += line_total

    quote.subtotal = subtotal
    quote.total = subtotal

    db.commit()
    db.refresh(quote)

    return _quote_response(quote)


@router.get("/{quote_id}", response_model=QuoteResponse)
def get_quote(
    quote_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    quote = (
        db.query(Quote)
        .options(
            joinedload(Quote.lines).joinedload(QuoteLine.part),
            joinedload(Quote.lines).joinedload(QuoteLine.work_order),
        )
        .filter(Quote.id == quote_id, Quote.company_id == company_id)
        .first()
    )

    if not quote:
        raise HTTPException(status_code=404, detail="Quote not found")

    return _quote_response(quote, _load_ai_estimate(db, quote.id))


@router.put("/{quote_id}", response_model=QuoteResponse)
def update_quote(
    quote_id: int,
    quote_in: QuoteUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    quote = db.query(Quote).filter(Quote.id == quote_id, Quote.company_id == company_id).with_for_update().first()
    if not quote:
        raise HTTPException(status_code=404, detail="Quote not found")

    previous_status = quote.status.value if hasattr(quote.status, "value") else str(quote.status)
    update_data = quote_in.model_dump(exclude_unset=True)
    expected_updated_at = update_data.pop("expected_updated_at", None)
    if expected_updated_at is not None:
        expected = (
            expected_updated_at.astimezone(timezone.utc).replace(tzinfo=None)
            if expected_updated_at.tzinfo
            else expected_updated_at
        )
        current = quote.updated_at or quote.created_at
        if current.tzinfo:
            current = current.astimezone(timezone.utc).replace(tzinfo=None)
        if expected != current:
            raise HTTPException(
                status_code=409,
                detail="Quote changed since this editor was opened. Your entries are kept; reopen the latest quote before applying them.",
            )
    line_data = update_data.pop("lines", None)
    content_keys = set(update_data) - {"status"}
    has_production_links = bool(quote.work_order_id or any(line.work_order_id for line in quote.lines))
    if has_production_links and (
        content_keys or line_data is not None or update_data.get("status") in [QuoteStatus.DRAFT, QuoteStatus.PENDING]
    ):
        raise HTTPException(status_code=409, detail="Quotes with linked work orders cannot be reopened for editing")
    if (content_keys or line_data is not None) and quote.status not in [QuoteStatus.DRAFT, QuoteStatus.PENDING]:
        raise HTTPException(
            status_code=409,
            detail="Only draft or pending quotes can be edited; issued quotes retain their reviewed contents",
        )
    if update_data.get("status") == QuoteStatus.CONVERTED:
        raise HTTPException(status_code=422, detail="Use the conversion review to create and link work orders")
    if quote.status == QuoteStatus.CONVERTED and "status" in update_data:
        raise HTTPException(status_code=409, detail="Converted quotes retain their production links")
    if line_data is not None:
        incoming = [QuoteLineCreate(**line) for line in line_data]
        if not incoming:
            raise HTTPException(status_code=422, detail="Add at least one quote line")
        _validate_quote_parts(db, incoming, company_id)
        existing = {line.id: line for line in quote.lines}
        supplied = [line.id for line in incoming if line.id is not None]
        if len(supplied) != len(set(supplied)) or not set(supplied).issubset(existing):
            raise HTTPException(status_code=422, detail="Quote line selection is invalid")
        next_lines = []
        for index, value in enumerate(incoming, 1):
            line = existing.get(value.id) or QuoteLine(company_id=company_id)
            fields = value.model_dump(exclude={"id"})
            fields["part_id"] = value.part_id if value.part_id and value.part_id > 0 else None
            for key, item in fields.items():
                setattr(line, key, item)
            line.line_number = index
            line.line_total = value.quantity * value.unit_price
            next_lines.append(line)
        quote.lines = next_lines
        quote.subtotal = sum(line.line_total for line in next_lines)
        quote.total = quote.subtotal + (quote.tax or 0)
    for field, value in update_data.items():
        setattr(quote, field, value)
    if content_keys or line_data is not None:
        # Line-only changes also advance the document snapshot even when totals match.
        quote.updated_at = datetime.utcnow()
        quote.approved_at = None
        quote.approved_by = None
        quote.status = QuoteStatus.DRAFT

    # Phase 0 always-on AI: capture win/loss/expired outcomes on status change.
    if "status" in update_data:
        from app.services.ai_outcome_capture_service import record_quote_status_outcome

        record_quote_status_outcome(
            db,
            company_id=company_id,
            quote=quote,
            previous_status=previous_status,
            user_id=current_user.id,
        )

    db.commit()
    db.refresh(quote)
    return _quote_response(quote)


@router.post("/{quote_id}/send")
def send_quote(
    quote_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Mark quote as sent to customer"""
    quote = db.query(Quote).filter(Quote.id == quote_id, Quote.company_id == company_id).with_for_update().first()
    if not quote:
        raise HTTPException(status_code=404, detail="Quote not found")

    if quote.status not in [QuoteStatus.DRAFT, QuoteStatus.PENDING, QuoteStatus.SENT]:
        raise HTTPException(status_code=409, detail="Only draft or pending quotes can be marked as sent")
    quote.status = QuoteStatus.SENT
    db.commit()

    return {"message": "Quote marked as sent", "quote_number": quote.quote_number}


def _conversion_outcome(line: QuoteLine, company_id: int) -> tuple[bool, str]:
    if line.work_order_id:
        return False, "Already converted"
    if not line.part_id:
        return False, "Quote-only item; no work order"
    if not line.part or line.part.company_id != company_id:
        return False, "Part is unavailable for this company"
    if not line.part.is_active:
        return False, "Part is inactive"
    if str(getattr(line.part.part_type, "value", line.part.part_type)) not in ["manufactured", "assembly"]:
        return False, "Purchased/material part; no production work order"
    return True, "Create work order"


def _conversion_plan(quote: Quote) -> dict:
    lines = []
    for line in sorted(quote.lines, key=lambda line: (line.line_number, line.id)):
        eligible, outcome = _conversion_outcome(line, quote.company_id)
        lines.append(
            {
                "line_id": line.id,
                "line_number": line.line_number,
                "part_id": line.part_id,
                "part_number": (
                    line.part.part_number if line.part and line.part.company_id == quote.company_id else None
                ),
                "description": line.description,
                "quantity": line.quantity,
                "work_order_id": line.work_order_id,
                "eligible": eligible,
                "outcome": outcome,
            }
        )
    return {"quote_id": quote.id, "quote_number": quote.quote_number, "lines": lines}


@router.get("/{quote_id}/conversion-plan")
def get_conversion_plan(
    quote_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    quote = (
        db.query(Quote)
        .options(selectinload(Quote.lines).joinedload(QuoteLine.part))
        .filter(Quote.id == quote_id, Quote.company_id == company_id)
        .first()
    )
    if not quote:
        raise HTTPException(status_code=404, detail="Quote not found")
    return _conversion_plan(quote)


@router.post("/{quote_id}/convert")
def convert_to_work_order(
    quote_id: int,
    conversion: Optional[QuoteConvertRequest] = Body(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
):
    """Convert explicitly reviewed lines atomically; each line keeps its own WO."""
    quote = db.query(Quote).filter(Quote.id == quote_id, Quote.company_id == company_id).with_for_update().first()
    if not quote:
        raise HTTPException(status_code=404, detail="Quote not found")
    if quote.status not in [QuoteStatus.SENT, QuoteStatus.ACCEPTED]:
        raise HTTPException(status_code=400, detail="Quote must be sent or accepted to convert")
    request = conversion or QuoteConvertRequest()
    plan = _conversion_plan(quote)
    eligible = {line["line_id"] for line in plan["lines"] if line["eligible"]}
    ids = set(request.line_ids) if request.line_ids is not None else eligible
    if not ids or not ids.issubset(eligible):
        raise HTTPException(status_code=422, detail="Select unconverted, active manufactured or assembly part lines")
    unlinked = [line for line in quote.lines if not line.part_id]
    if unlinked and not request.acknowledge_unlinked:
        raise HTTPException(status_code=422, detail="Review quote-only items: they do not create work orders")
    from app.api.endpoints.work_orders import generate_work_order_number

    created = []
    for line in sorted(quote.lines, key=lambda line: (line.line_number, line.id)):
        if line.id not in ids:
            continue
        wo = WorkOrder(
            company_id=company_id,
            work_order_number=generate_work_order_number(db, company_id),
            part_id=line.part_id,
            quantity_ordered=line.quantity,
            customer_name=quote.customer_name,
            customer_po=quote.customer_po,
            due_date=date.today() + timedelta(days=quote.lead_time_days) if quote.lead_time_days else None,
            notes=f"Converted from quote {quote.quote_number}, line {line.line_number}: {line.description}",
            created_by=current_user.id,
        )
        db.add(wo)
        db.flush()  # Makes this number visible to the next line in this transaction.
        line.work_order_id = wo.id
        created.append(
            {
                "line_id": line.id,
                "quantity": line.quantity,
                "work_order_id": wo.id,
                "work_order_number": wo.work_order_number,
            }
        )
    previous_status = quote.status.value if hasattr(quote.status, "value") else str(quote.status)
    # Purchased/material lines do not create production orders. Inactive production
    # parts and unresolved references still need resolution before completion.
    remaining = [
        line.id
        for line in quote.lines
        if line.part_id
        and not line.work_order_id
        and (
            not line.part
            or line.part.company_id != company_id
            or str(getattr(line.part.part_type, "value", line.part.part_type)) in ["manufactured", "assembly"]
        )
    ]
    quote.status = QuoteStatus.ACCEPTED if remaining else QuoteStatus.CONVERTED
    quote.work_order_id = quote.work_order_id or created[0]["work_order_id"]
    from app.services.ai_outcome_capture_service import record_quote_status_outcome

    record_quote_status_outcome(
        db, company_id=company_id, quote=quote, previous_status=previous_status, user_id=current_user.id
    )
    db.commit()
    return {
        "message": f"Created {len(created)} work order(s)",
        "quote_number": quote.quote_number,
        "work_orders": created,
        "remaining_line_ids": remaining,
        "work_order_id": created[0]["work_order_id"],
        "work_order_number": created[0]["work_order_number"],
    }


@router.post("/{quote_id}/generate-pdf")
def generate_quote_pdf(
    quote_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Generate customer-ready quote PDF (no operation-time line items)."""
    quote = (
        db.query(Quote)
        .options(joinedload(Quote.lines).joinedload(QuoteLine.part))
        .filter(Quote.id == quote_id, Quote.company_id == company_id)
        .first()
    )
    if not quote:
        raise HTTPException(status_code=404, detail="Quote not found")

    from app.services.document_pdf_service import build_quote_document

    pdf_bytes = build_quote_document(db, quote, company_id)

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
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    quote = db.query(Quote).filter(Quote.id == quote_id, Quote.company_id == company_id).first()
    if not quote:
        raise HTTPException(status_code=404, detail="Quote not found")

    if quote.status not in [QuoteStatus.DRAFT, QuoteStatus.PENDING]:
        raise HTTPException(status_code=400, detail="Can only add lines to draft or pending quotes")

    # Get next line number
    from sqlalchemy import func

    max_line = db.query(func.max(QuoteLine.line_number)).filter(QuoteLine.quote_id == quote_id).scalar() or 0

    line_total = line_in.quantity * line_in.unit_price
    line = QuoteLine(
        quote_id=quote_id,
        company_id=company_id,
        line_number=max_line + 1,
        part_id=line_in.part_id if line_in.part_id and line_in.part_id > 0 else None,
        description=line_in.description,
        quantity=line_in.quantity,
        unit_price=line_in.unit_price,
        line_total=line_total,
        material_cost=line_in.material_cost,
        labor_hours=line_in.labor_hours,
        labor_cost=line_in.labor_cost,
        notes=line_in.notes,
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
        labor_cost=line.labor_cost,
    )
