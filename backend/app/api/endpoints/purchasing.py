from datetime import date, datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, ValidationError
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload, selectinload

from app.api.deps import get_audit_service, get_current_company_id, get_current_user, require_role
from app.db.database import get_db
from app.db.locks import acquire_generator_lock
from app.models.part import Part
from app.models.purchasing import (
    POStatus,
    PurchaseOrder,
    PurchaseOrderLine,
    Vendor,
)
from app.models.user import User, UserRole
from app.schemas.import_kit import PurchaseOrderImportResponse
from app.schemas.purchasing import (
    POCreate,
    POLineCreate,
    POLineResponse,
    POListResponse,
    POResponse,
    POUpdate,
    VendorCreate,
    VendorResponse,
    VendorUpdate,
)
from app.services.audit_service import AuditService
from app.services.import_service import ImportFileError, parse_import_file
from app.services.migration_import_service import import_open_purchase_orders
from app.services.operational_event_service import OperationalEventService

router = APIRouter()


class VendorCsvImportError(BaseModel):
    row: int
    code: Optional[str] = None
    name: Optional[str] = None
    reason: str


class VendorCsvImportResponse(BaseModel):
    imported_count: int
    skipped_count: int
    total_rows: int
    created_ids: List[int]
    errors: List[VendorCsvImportError]
    dry_run: bool = False


def _parse_bool(value: str, default: bool = False) -> bool:
    if value == "":
        return default
    normalized = value.strip().lower()
    if normalized in {"true", "1", "yes", "y", "approved"}:
        return True
    if normalized in {"false", "0", "no", "n"}:
        return False
    raise ValueError(f"Invalid boolean value '{value}'")


def _parse_int(value: str, field_name: str, default: int = 0) -> int:
    if value == "":
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an integer") from exc


def _generate_vendor_code(db: Session, name: str, company_id: int) -> str:
    base = "".join(c for c in name.upper() if c.isalnum())[:3]
    if len(base) < 3:
        base = base.ljust(3, "X")
    existing = db.query(Vendor).filter(Vendor.company_id == company_id, Vendor.code.like(f"{base}%")).count()
    return f"{base}{existing + 1:03d}"


# ============ VENDORS ============


@router.get("/vendors", response_model=List[VendorResponse])
def list_vendors(
    active_only: bool = True,
    approved_only: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    query = db.query(Vendor).filter(Vendor.company_id == company_id)
    if active_only:
        query = query.filter(Vendor.is_active == True)
    if approved_only:
        query = query.filter(Vendor.is_approved == True)
    return query.order_by(Vendor.name).all()


@router.post("/vendors", response_model=VendorResponse)
def create_vendor(
    vendor_in: VendorCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
):
    existing = db.query(Vendor).filter(Vendor.code == vendor_in.code, Vendor.company_id == company_id).first()
    if existing:
        raise HTTPException(status_code=400, detail="Vendor code already exists")

    vendor = Vendor(**vendor_in.model_dump())
    vendor.company_id = company_id
    if vendor.is_approved:
        vendor.approval_date = date.today()
    db.add(vendor)
    db.commit()
    db.refresh(vendor)
    return vendor


@router.post("/vendors/import-csv", response_model=VendorCsvImportResponse)
async def import_vendors_csv(
    request: Request,
    file: UploadFile = File(...),
    dry_run: bool = Query(False, description="Validate only; no rows are written"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
):
    """Import vendor master records from CSV or XLSX with row-level errors."""
    content = await file.read()
    try:
        table = parse_import_file(file.filename, content, required_columns={"name"})
    except ImportFileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    existing_codes = {
        (value or "").strip().upper()
        for (value,) in db.query(Vendor.code).filter(Vendor.company_id == company_id).all()
    }

    audit = AuditService(db, current_user, request)
    errors: List[VendorCsvImportError] = []
    created_ids: List[int] = []
    total_rows = 0
    accepted_count = 0

    for row_number, row in table.iter_rows():
        total_rows += 1
        name = row.get("name", "")
        code = (row.get("code") or "").upper()
        code_was_provided = bool(code)

        try:
            if not name:
                raise ValueError("name is required")
            if not code:
                code = _generate_vendor_code(db, name, company_id)
            if code in existing_codes:
                raise ValueError("Vendor code already exists")

            vendor_in = VendorCreate(
                code=code,
                name=name,
                contact_name=row.get("contact_name") or None,
                email=row.get("email") or None,
                phone=row.get("phone") or None,
                address_line1=row.get("address_line1") or None,
                address_line2=row.get("address_line2") or None,
                city=row.get("city") or None,
                state=(row.get("state") or "").upper() or None,
                postal_code=row.get("postal_code") or row.get("zip_code") or None,
                country=row.get("country") or "US",
                payment_terms=row.get("payment_terms") or None,
                lead_time_days=_parse_int(row.get("lead_time_days", ""), "lead_time_days", 14),
                is_approved=_parse_bool(row.get("is_approved", ""), False),
                is_as9100_certified=_parse_bool(row.get("is_as9100_certified", ""), False),
                is_iso9001_certified=_parse_bool(row.get("is_iso9001_certified", ""), False),
                notes=row.get("notes") or None,
            )
            is_active = _parse_bool(row.get("is_active", ""), True)
        except (ValueError, ValidationError) as exc:
            errors.append(
                VendorCsvImportError(
                    row=row_number,
                    code=code or None,
                    name=name or None,
                    reason=str(exc),
                )
            )
            continue

        if dry_run:
            # Generated codes are only reserved at commit; don't let a
            # would-be-generated code trip the in-file duplicate check.
            if code_was_provided:
                existing_codes.add(code.upper())
            accepted_count += 1
            continue

        try:
            vendor = Vendor(**vendor_in.model_dump())
            vendor.company_id = company_id
            vendor.is_active = is_active
            if vendor.is_approved:
                vendor.approval_date = date.today()
            db.add(vendor)
            db.flush()
            audit.log_create("vendor", vendor.id, vendor.code, new_values=vendor, extra_data={"source": "import"})
            db.commit()
            db.refresh(vendor)
        except Exception as exc:
            db.rollback()
            errors.append(VendorCsvImportError(row=row_number, code=code, name=name, reason=str(exc)))
            continue

        existing_codes.add(vendor.code.upper())
        created_ids.append(vendor.id)
        accepted_count += 1

    return VendorCsvImportResponse(
        imported_count=accepted_count,
        skipped_count=len(errors),
        total_rows=total_rows,
        created_ids=created_ids,
        errors=errors,
        dry_run=dry_run,
    )


@router.get("/vendors/{vendor_id}", response_model=VendorResponse)
def get_vendor(
    vendor_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    vendor = db.query(Vendor).filter(Vendor.id == vendor_id, Vendor.company_id == company_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")
    return vendor


@router.put("/vendors/{vendor_id}", response_model=VendorResponse)
def update_vendor(
    vendor_id: int,
    vendor_in: VendorUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
):
    vendor = db.query(Vendor).filter(Vendor.id == vendor_id, Vendor.company_id == company_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    update_data = vendor_in.model_dump(exclude_unset=True)

    # Set approval date if being approved
    if update_data.get("is_approved") and not vendor.is_approved:
        vendor.approval_date = date.today()

    for field, value in update_data.items():
        setattr(vendor, field, value)

    db.commit()
    db.refresh(vendor)
    return vendor


# ============ PURCHASE ORDERS ============


def generate_po_number(db: Session, company_id: int = None) -> str:
    """Generate next PO number (PO-YYYYMMDD-XXX).

    Holds an advisory lock so concurrent creates can't collide.
    """
    acquire_generator_lock(db, "po_number", company_id)

    today = datetime.now().strftime("%Y%m%d")
    prefix = f"PO-{today}-"

    query = db.query(PurchaseOrder).filter(PurchaseOrder.po_number.like(f"{prefix}%"))
    if company_id is not None:
        query = query.filter(PurchaseOrder.company_id == company_id)
    last_po = query.order_by(PurchaseOrder.po_number.desc()).first()

    if last_po:
        last_num = int(last_po.po_number.split("-")[-1])
        new_num = last_num + 1
    else:
        new_num = 1

    return f"{prefix}{new_num:03d}"


@router.get("/purchase-orders", response_model=List[POListResponse])
def list_purchase_orders(
    status: Optional[str] = None,
    vendor_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    query = (
        db.query(PurchaseOrder)
        .filter(PurchaseOrder.company_id == company_id)
        .options(joinedload(PurchaseOrder.vendor), selectinload(PurchaseOrder.lines))
    )

    if status:
        query = query.filter(PurchaseOrder.status == status)
    else:
        # Default: exclude closed/cancelled
        query = query.filter(PurchaseOrder.status.not_in([POStatus.CLOSED, POStatus.CANCELLED]))

    if vendor_id:
        query = query.filter(PurchaseOrder.vendor_id == vendor_id)

    pos = query.order_by(PurchaseOrder.created_at.desc()).all()

    result = []
    for po in pos:
        result.append(
            POListResponse(
                id=po.id,
                po_number=po.po_number,
                vendor_id=po.vendor_id,
                vendor_name=po.vendor.name if po.vendor else None,
                status=po.status.value if hasattr(po.status, "value") else po.status,
                order_date=po.order_date,
                required_date=po.required_date,
                total=po.total,
                line_count=len(po.lines),
                created_at=po.created_at,
            )
        )
    return result


@router.post("/purchase-orders", response_model=POResponse)
def create_purchase_order(
    po_in: POCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
):
    # Verify vendor
    vendor = db.query(Vendor).filter(Vendor.id == po_in.vendor_id, Vendor.company_id == company_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    po_number = generate_po_number(db, company_id)

    po = PurchaseOrder(
        po_number=po_number,
        vendor_id=po_in.vendor_id,
        required_date=po_in.required_date,
        expected_date=po_in.expected_date,
        ship_to=po_in.ship_to,
        shipping_method=po_in.shipping_method,
        notes=po_in.notes,
        created_by=current_user.id,
    )
    po.company_id = company_id
    db.add(po)
    db.flush()

    # Add lines
    subtotal = 0.0
    for idx, line_data in enumerate(po_in.lines, 1):
        part = db.query(Part).filter(Part.id == line_data.part_id, Part.company_id == company_id).first()
        if not part:
            raise HTTPException(status_code=404, detail=f"Part {line_data.part_id} not found")

        line_total = line_data.quantity_ordered * line_data.unit_price
        line = PurchaseOrderLine(
            purchase_order_id=po.id,
            line_number=idx,
            part_id=line_data.part_id,
            quantity_ordered=line_data.quantity_ordered,
            unit_price=line_data.unit_price,
            line_total=line_total,
            required_date=line_data.required_date or po_in.required_date,
            notes=line_data.notes,
        )
        line.company_id = company_id
        db.add(line)
        subtotal += line_total

    po.subtotal = subtotal
    po.total = subtotal + po.tax + po.shipping

    db.flush()
    OperationalEventService(db).emit(
        company_id=company_id,
        event_type="purchase_order_created",
        source_module="purchasing",
        entity_type="purchase_order",
        entity_id=po.id,
        user_id=current_user.id,
        severity="info",
        event_payload={
            "po_number": po.po_number,
            "vendor_id": po.vendor_id,
            "vendor_name": vendor.name,
            "line_count": len(po_in.lines),
            "required_date": po.required_date.isoformat() if po.required_date else None,
            "total": float(po.total or 0),
        },
    )
    db.commit()
    db.refresh(po)
    return po


@router.post(
    "/purchase-orders/import",
    response_model=PurchaseOrderImportResponse,
    summary="Import open purchase orders (CSV/XLSX)",
)
async def import_open_purchase_orders_endpoint(
    file: UploadFile = File(...),
    dry_run: bool = Query(False, description="Validate and preview only; guarantees no rows are written"),
    db: Session = Depends(get_db),
    # ADMIN/MANAGER only: imported POs land directly in SENT (issued), and the
    # interactive /send transition is ADMIN/MANAGER-only — allowing SUPERVISOR
    # here would let them issue POs via spreadsheet that they cannot issue in
    # the UI (privilege escalation).
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Import OPEN (issued, not yet received) purchase orders for the Excel go-live migration.

    Columns: ``po_number`` (optional — rows sharing one become lines of a
    single PO; generated when blank), ``vendor_code`` (must exist),
    ``part_number`` (must exist), ``quantity``, ``unit_price``,
    ``promised_date`` (optional). POs are created in ``sent`` (issued) status
    so receiving can act on them immediately. Use ``dry_run=true`` to preview
    without writing.
    """
    content = await file.read()
    try:
        table = parse_import_file(
            file.filename, content, required_columns={"vendor_code", "part_number", "quantity", "unit_price"}
        )
    except ImportFileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return import_open_purchase_orders(
        db,
        table=table,
        current_user=current_user,
        company_id=company_id,
        audit=audit,
        dry_run=dry_run,
    )


@router.get("/purchase-orders/{po_id}", response_model=POResponse)
def get_purchase_order(
    po_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    po = (
        db.query(PurchaseOrder)
        .options(
            joinedload(PurchaseOrder.vendor),
            joinedload(PurchaseOrder.lines).joinedload(PurchaseOrderLine.part),
        )
        .filter(PurchaseOrder.id == po_id, PurchaseOrder.company_id == company_id)
        .first()
    )

    if not po:
        raise HTTPException(status_code=404, detail="Purchase order not found")
    return po


@router.put("/purchase-orders/{po_id}", response_model=POResponse)
def update_purchase_order(
    po_id: int,
    po_in: POUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
):
    po = db.query(PurchaseOrder).filter(PurchaseOrder.id == po_id, PurchaseOrder.company_id == company_id).first()
    if not po:
        raise HTTPException(status_code=404, detail="Purchase order not found")

    previous_status = po.status
    update_data = po_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        if field == "status":
            setattr(po, field, POStatus(value))
        else:
            setattr(po, field, value)

    OperationalEventService(db).emit(
        company_id=company_id,
        event_type="purchase_order_updated",
        source_module="purchasing",
        entity_type="purchase_order",
        entity_id=po.id,
        user_id=current_user.id,
        severity="info" if po.status == previous_status else "medium",
        event_payload={
            "po_number": po.po_number,
            "changed_fields": [field for field in update_data.keys() if field != "version"],
            "previous_status": (previous_status.value if hasattr(previous_status, "value") else previous_status),
            "status": po.status.value if hasattr(po.status, "value") else po.status,
            "required_date": po.required_date.isoformat() if po.required_date else None,
            "expected_date": po.expected_date.isoformat() if po.expected_date else None,
        },
    )
    db.commit()
    db.refresh(po)
    return po


@router.post("/purchase-orders/{po_id}/send")
def send_purchase_order(
    po_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
):
    po = db.query(PurchaseOrder).filter(PurchaseOrder.id == po_id, PurchaseOrder.company_id == company_id).first()
    if not po:
        raise HTTPException(status_code=404, detail="Purchase order not found")

    if po.status not in [POStatus.DRAFT, POStatus.APPROVED]:
        raise HTTPException(status_code=400, detail="Can only send draft or approved POs")

    po.status = POStatus.SENT
    po.order_date = date.today()
    OperationalEventService(db).emit(
        company_id=company_id,
        event_type="purchase_order_sent",
        source_module="purchasing",
        entity_type="purchase_order",
        entity_id=po.id,
        user_id=current_user.id,
        severity="info",
        event_payload={
            "po_number": po.po_number,
            "vendor_id": po.vendor_id,
            "order_date": po.order_date.isoformat() if po.order_date else None,
            "required_date": po.required_date.isoformat() if po.required_date else None,
        },
    )
    db.commit()

    return {"message": "PO sent", "po_number": po.po_number}


@router.post("/purchase-orders/{po_id}/lines", response_model=POLineResponse)
def add_po_line(
    po_id: int,
    line_in: POLineCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
):
    po = db.query(PurchaseOrder).filter(PurchaseOrder.id == po_id, PurchaseOrder.company_id == company_id).first()
    if not po:
        raise HTTPException(status_code=404, detail="Purchase order not found")

    if po.status not in [POStatus.DRAFT]:
        raise HTTPException(status_code=400, detail="Can only add lines to draft POs")

    part = db.query(Part).filter(Part.id == line_in.part_id, Part.company_id == company_id).first()
    if not part:
        raise HTTPException(status_code=404, detail="Part not found")

    # Get next line number
    max_line = (
        db.query(func.max(PurchaseOrderLine.line_number)).filter(PurchaseOrderLine.purchase_order_id == po_id).scalar()
        or 0
    )

    line_total = line_in.quantity_ordered * line_in.unit_price
    line = PurchaseOrderLine(
        purchase_order_id=po_id,
        line_number=max_line + 1,
        part_id=line_in.part_id,
        quantity_ordered=line_in.quantity_ordered,
        unit_price=line_in.unit_price,
        line_total=line_total,
        required_date=line_in.required_date,
        notes=line_in.notes,
    )
    line.company_id = company_id
    db.add(line)

    # Update PO totals
    po.subtotal += line_total
    po.total = po.subtotal + po.tax + po.shipping

    db.flush()
    OperationalEventService(db).emit(
        company_id=company_id,
        event_type="purchase_order_line_added",
        source_module="purchasing",
        entity_type="purchase_order_line",
        entity_id=line.id,
        user_id=current_user.id,
        severity="info",
        event_payload={
            "po_id": po.id,
            "po_number": po.po_number,
            "line_number": line.line_number,
            "part_id": line.part_id,
            "quantity_ordered": float(line.quantity_ordered or 0),
            "unit_price": float(line.unit_price or 0),
            "required_date": (line.required_date.isoformat() if line.required_date else None),
        },
    )
    db.commit()
    db.refresh(line)
    return line


# ============ RECEIVING ============
# The receiving / inspection endpoints live in app/api/endpoints/receiving.py
# (mounted at /api/v1/receiving). The duplicate copies that previously lived here
# were removed; purchasing.py now owns only vendor and purchase-order endpoints.
