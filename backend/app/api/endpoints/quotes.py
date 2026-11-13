from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload, selectinload

from app.api.deps import get_audit_service, get_current_company_id, get_current_user, require_role
from app.db.database import get_db
from app.models.fabrication_quote import FabricationQuote
from app.models.quote import Quote, QuoteLine, QuoteStatus
from app.models.rfq_quote import QuoteEstimate, RfqPackage
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder
from app.services.audit_service import AuditService
from app.services.quote_send_gate import (
    assert_quote_printable,
    assert_quote_sendable,
    normalize_line_revision,
    refuse_na_on_drawn_parts,
)

router = APIRouter()

# Every quote WRITE verb on this router (edit, send, convert) shares one role set. It is the
# set that holds ``purchasing:create`` by default -- the permission Fabrication Quoting
# requires to approve an estimate and hand it off as the ERP quote these verbs act on -- and
# the set ``convert`` was already gated to. Reads stay tenant-scoped and role-open (the
# read-broad / write-restricted model in docs/RBAC_PERMISSIONS.md), so a Viewer who can open
# the Quotes page still cannot accept, reject, expire, re-word or mark sent a customer quote.
QUOTE_WRITE_ROLES = [UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]

# Columns written into a quote's UPDATE audit diff. ``status`` is deliberately absent: every
# status move gets its own STATUS_CHANGE row (``log_status_change``), so a reviewer searching
# for "who accepted / rejected / expired this quote" finds exactly one row per transition
# instead of a status buried in a content diff as well.
_QUOTE_AUDIT_EXCLUDED_COLUMNS = frozenset({"status"})


def _status_value(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _quote_audit_snapshot(quote: Quote) -> Dict[str, Any]:
    return {
        column.key: getattr(quote, column.key)
        for column in quote.__table__.columns
        if column.key not in _QUOTE_AUDIT_EXCLUDED_COLUMNS
    }


def _line_work_order_links(quote: Quote) -> Dict[str, Optional[int]]:
    # String keys: audit values are JSON and feed the hash chain, so keep them unambiguous.
    return {str(line.id): line.work_order_id for line in quote.lines}


def _line_revisions(quote: Quote) -> Dict[str, Optional[str]]:
    # Each line's drawing revision, keyed like ``_line_work_order_links``.
    return {str(line.id): line.part_revision for line in quote.lines}


def _part_current_revision(line: QuoteLine, company_id: int) -> Optional[str]:
    """The line's part's revision today (what the editor pre-fills), tenant-checked."""
    if line.part is None or line.part.company_id != company_id:
        return None
    return line.part.revision


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


class QuoteLineRevisionEdit(BaseModel):
    """One line's drawing revision, entered on a draft or pending quote."""

    line_id: int = Field(..., gt=0, description="A line of THIS quote")
    part_revision: str = Field(
        ...,
        description=(
            "Drawing revision: 1-20 letters/digits, upper-cased on save, or N/A for a line with no drawing "
            "(setup charge, freight, a packaged line)."
        ),
    )

    @field_validator("part_revision")
    @classmethod
    def _normalized(cls, value: str) -> str:
        return normalize_line_revision(value)


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
    # The only line content this verb edits: each line's drawing revision. A CONTENT edit --
    # draft/pending quotes only, the same CAS, the reset to DRAFT and the approval clear as any
    # other content field -- audited per line on the quote's UPDATE row.
    line_revisions: Optional[List[QuoteLineRevisionEdit]] = None


class QuoteConvertRequest(BaseModel):
    line_ids: Optional[List[int]] = None
    acknowledge_unlinked: bool = False


class QuoteLineResponse(BaseModel):
    id: int
    line_number: int
    part_id: Optional[int] = None
    part_number: Optional[str] = None
    part_revision: Optional[str] = Field(
        None, description="The drawing revision this line quotes (required on every line before sending)"
    )
    part_current_revision: Optional[str] = Field(
        None, description="The linked part's current revision -- what the editor pre-fills when part_revision is empty"
    )
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
    fabrication_quote_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
        use_enum_values = True


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


def _quote_response(quote: Quote, ai_estimate=None, fabrication_quote_id=None) -> QuoteResponse:
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
        fabrication_quote_id=fabrication_quote_id,
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
                        "part_revision",
                    )
                },
                part_current_revision=_part_current_revision(line, quote.company_id),
                # Tenant-checked like the revision beside it: a legacy line whose part_id
                # points at another company's part names no part here.
                part_number=(line.part.part_number if line.part and line.part.company_id == quote.company_id else None),
                work_order_number=line.work_order.work_order_number if line.work_order else None,
            )
            for line in sorted(quote.lines, key=lambda line: (line.line_number, line.id))
        ],
    )


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
    fabrication_links = (
        dict(
            db.query(FabricationQuote.erp_quote_id, FabricationQuote.id)
            .filter(
                FabricationQuote.company_id == company_id, FabricationQuote.erp_quote_id.in_([q.id for q in quotes])
            )
            .all()
        )
        if quotes
        else {}
    )
    return [_quote_response(quote, fabrication_quote_id=fabrication_links.get(quote.id)) for quote in quotes]


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

    fabrication = (
        db.query(FabricationQuote.id)
        .filter(FabricationQuote.company_id == company_id, FabricationQuote.erp_quote_id == quote.id)
        .first()
    )
    return _quote_response(quote, _load_ai_estimate(db, quote.id), fabrication.id if fabrication else None)


@router.put("/{quote_id}", response_model=QuoteResponse)
def update_quote(
    quote_id: int,
    quote_in: QuoteUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(QUOTE_WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Edit a draft/pending quote's commercial terms, or move its status (accept / reject / expire).

    Role-gated to ``QUOTE_WRITE_ROLES``. Audited on the tamper-evident chain: a content edit
    writes one UPDATE row (field diff, ``status`` excluded) and any status move -- an explicit
    one, or the reset to DRAFT that a content edit forces -- writes one STATUS_CHANGE row. Both
    are written before the terminal commit, so they commit atomically with the change. Every
    refusal below is raised before the first ``setattr`` and writes nothing.

    ``line_revisions`` enters each line's drawing revision (letters/digits, or ``N/A`` for a
    line with no drawing). It is a content edit: draft/pending quotes only, and it resets the
    quote to DRAFT and clears its approval stamp like any other; a ``line_id`` that is not a
    line of this quote (or is repeated) is a 422, and so is ``N/A`` on a line naming a
    manufactured or assembly part (it has a drawing). A value equal to the stored one edits
    nothing -- no reset, no audit row. Moving a quote to ``sent`` or ``accepted``
    here from any status other than those two (draft, pending, and also rejected or expired,
    which a quote can reach without ever being sent) is refused (409) while any line lacks
    its drawing revision, exactly as ``POST /quotes/{id}/send`` is.
    """
    quote = db.query(Quote).filter(Quote.id == quote_id, Quote.company_id == company_id).with_for_update().first()
    if not quote:
        raise HTTPException(status_code=404, detail="Quote not found")

    previous_status = _status_value(quote.status)
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
    revision_edits = update_data.pop("line_revisions", None) or []  # an empty list edits nothing
    content_keys = set(update_data) - {"status"}
    lines_by_id = {line.id: line for line in quote.lines}
    # Only a revision that CHANGES is a content edit. Re-sending the stored values (an editor
    # saving unchanged rows) must not reset a pending quote to DRAFT or clear its approval. An
    # unknown line id counts as a change, so it still reaches the 422 below.
    if any(
        edit["line_id"] not in lines_by_id or lines_by_id[edit["line_id"]].part_revision != edit["part_revision"]
        for edit in revision_edits
    ):
        content_keys.add("line_revisions")
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
        raise HTTPException(
            status_code=409,
            detail="Customer quote prices and lines are frozen. Revise and approve the estimate in Fabrication Quoting.",
        )
    edited_line_ids = [edit["line_id"] for edit in revision_edits]
    unknown = sorted({line_id for line_id in edited_line_ids if line_id not in lines_by_id})
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"line_revisions names line id(s) that are not on this quote: {', '.join(map(str, unknown))}",
        )
    if len(set(edited_line_ids)) != len(edited_line_ids):
        raise HTTPException(status_code=422, detail="line_revisions names the same line more than once")
    refuse_na_on_drawn_parts(
        db,
        company_id,
        [
            (lines_by_id[edit["line_id"]], edit["part_revision"])
            for edit in revision_edits
            if lines_by_id[edit["line_id"]].part_revision != edit["part_revision"]
        ],
    )
    # Every door that puts the quote in front of the customer holds the revision gate. Only a
    # real move INTO a customer status counts (SENT -> ACCEPTED is not one: the customer
    # already holds that document), and it is judged on the move, not the starting status:
    # REJECTED / EXPIRED are reachable from DRAFT by a plain status update, so gating only
    # draft/pending would let draft -> rejected -> sent carry a blank revision out. With a
    # content edit in the same request the quote lands at DRAFT, so there is no move.
    if (
        update_data.get("status") in (QuoteStatus.SENT, QuoteStatus.ACCEPTED)
        and quote.status not in (QuoteStatus.SENT, QuoteStatus.ACCEPTED)
        and not (content_keys or line_data is not None)
    ):
        assert_quote_sendable(quote)
    old_snapshot = _quote_audit_snapshot(quote)
    old_revisions = _line_revisions(quote)
    for field, value in update_data.items():
        setattr(quote, field, value)
    revision_changes = []
    for edit in revision_edits:
        line = lines_by_id[edit["line_id"]]
        if line.part_revision != edit["part_revision"]:
            revision_changes.append(
                {
                    "line_id": line.id,
                    "line_number": line.line_number,
                    "old": line.part_revision,
                    "new": edit["part_revision"],
                }
            )
            line.part_revision = edit["part_revision"]
    if content_keys or line_data is not None:
        # Line-only changes also advance the document snapshot even when totals match.
        quote.updated_at = datetime.utcnow()
        quote.approved_at = None
        quote.approved_by = None
        quote.status = QuoteStatus.DRAFT

    db.flush()
    new_status = _status_value(quote.status)
    if content_keys:
        new_snapshot = _quote_audit_snapshot(quote)
        extra_data: Dict[str, Any] = {"edited_fields": sorted(content_keys)}
        if "line_revisions" in content_keys:
            # Per line, old -> new: the drawing revision is customer-document content.
            old_snapshot["line_revisions"] = old_revisions
            new_snapshot["line_revisions"] = _line_revisions(quote)
            extra_data["line_revision_changes"] = revision_changes
        audit.log_update(
            "quote",
            quote.id,
            quote.quote_number,
            old_values=old_snapshot,
            new_values=new_snapshot,
            extra_data=extra_data,
        )
    if new_status != previous_status:
        audit.log_status_change(
            "quote",
            quote.id,
            quote.quote_number,
            old_status=previous_status,
            new_status=new_status,
            extra_data={
                # "edit": a content edit reset an issued-for-review quote to DRAFT and cleared
                # its approval stamp. "status_update": the caller asked for this status.
                "source": "edit" if content_keys else "status_update",
            },
        )

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
    fabrication = (
        db.query(FabricationQuote.id)
        .filter(FabricationQuote.company_id == company_id, FabricationQuote.erp_quote_id == quote.id)
        .first()
    )
    return _quote_response(quote, fabrication_quote_id=fabrication.id if fabrication else None)


@router.post("/{quote_id}/send")
def send_quote(
    quote_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(QUOTE_WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Mark quote as sent to customer (records the status only; no message is dispatched).

    Role-gated to ``QUOTE_WRITE_ROLES``. Writes one STATUS_CHANGE audit row when the status
    actually moves; re-sending an already-SENT quote stays an idempotent 200 that writes none.
    A draft/pending quote is refused (409, nothing written) while any line lacks its drawing
    revision -- enter them with ``PUT /quotes/{id}`` ``line_revisions`` (``N/A`` for a line
    with no drawing).
    """
    quote = db.query(Quote).filter(Quote.id == quote_id, Quote.company_id == company_id).with_for_update().first()
    if not quote:
        raise HTTPException(status_code=404, detail="Quote not found")

    if quote.status not in [QuoteStatus.DRAFT, QuoteStatus.PENDING, QuoteStatus.SENT]:
        raise HTTPException(status_code=409, detail="Only draft or pending quotes can be marked as sent")
    if quote.status != QuoteStatus.SENT:
        assert_quote_sendable(quote)
    previous_status = _status_value(quote.status)
    quote.status = QuoteStatus.SENT
    db.flush()
    if previous_status != QuoteStatus.SENT.value:
        audit.log_status_change(
            "quote",
            quote.id,
            quote.quote_number,
            old_status=previous_status,
            new_status=QuoteStatus.SENT.value,
            extra_data={"source": "send", "customer_message_dispatched": False},
        )
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
    current_user: User = Depends(require_role(QUOTE_WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Convert explicitly reviewed lines atomically; each line keeps its own WO.

    Audit, all before the single terminal commit: one CREATE row per work order minted (the
    same ``log_create("work_order", ...)`` shape ``POST /work-orders/`` writes, with the quote
    and line it came from in ``extra_data``), plus one row on the quote -- a STATUS_CHANGE
    when the quote moves (to ACCEPTED or CONVERTED), otherwise an UPDATE carrying the new
    line -> work-order links (a second partial conversion of an already-ACCEPTED quote).
    """
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

    previous_links = _line_work_order_links(quote)
    previous_work_order_id = quote.work_order_id
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
        audit.log_create(
            resource_type="work_order",
            resource_id=wo.id,
            resource_identifier=wo.work_order_number,
            new_values=wo,
            extra_data={
                "source": "quote_conversion",
                "quote_id": quote.id,
                "quote_number": quote.quote_number,
                "quote_line_id": line.id,
                "quote_line_number": line.line_number,
                "part_id": line.part_id,
                "quantity": float(line.quantity),
            },
        )
        created.append(
            {
                "line_id": line.id,
                "quantity": line.quantity,
                "work_order_id": wo.id,
                "work_order_number": wo.work_order_number,
            }
        )
    previous_status = _status_value(quote.status)
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
    db.flush()
    new_status = _status_value(quote.status)
    conversion_detail = {
        "source": "conversion",
        "work_orders": created,
        "remaining_line_ids": remaining,
    }
    if new_status != previous_status:
        audit.log_status_change(
            "quote",
            quote.id,
            quote.quote_number,
            old_status=previous_status,
            new_status=new_status,
            description=(
                f"Converted quote {quote.quote_number} to {len(created)} work order(s); "
                f"status '{previous_status}' -> '{new_status}'"
            ),
            extra_data=conversion_detail,
        )
    else:
        audit.log_update(
            "quote",
            quote.id,
            quote.quote_number,
            old_values={"line_work_orders": previous_links, "work_order_id": previous_work_order_id},
            new_values={"line_work_orders": _line_work_order_links(quote), "work_order_id": quote.work_order_id},
            description=f"Converted {len(created)} more line(s) of quote {quote.quote_number} to work orders",
            extra_data=conversion_detail,
        )
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
    """Generate customer-ready quote PDF (no operation-time line items).

    Until the quote is issued this is a new customer document, so it holds the same gate as
    sending: a draft, pending, rejected or expired quote is refused (409) while any line lacks
    its drawing revision. A sent, accepted or converted quote reprints the document the
    customer already holds and is never refused for it (a quote issued before revisions were
    recorded prints "-" in the Rev column). The PDF shows each line's Rev, the quote's own
    revision as "Quote Rev", Valid Until (the quote's date, else quote date + 30 days) and
    the standard Terms block (FOB from your company's address; the validity term names the
    Valid Until date when the quote carries one).
    """
    quote = (
        db.query(Quote)
        .options(joinedload(Quote.lines).joinedload(QuoteLine.part))
        .filter(Quote.id == quote_id, Quote.company_id == company_id)
        .first()
    )
    if not quote:
        raise HTTPException(status_code=404, detail="Quote not found")
    assert_quote_printable(quote)

    from app.services.document_pdf_service import build_quote_document

    pdf_bytes = build_quote_document(db, quote, company_id)

    filename = f"{quote.quote_number}.pdf"
    return StreamingResponse(
        BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
