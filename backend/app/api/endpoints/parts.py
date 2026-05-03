import csv
import io
from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from pydantic import BaseModel, ValidationError
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, selectinload

from app.api.deps import get_current_company_id, get_current_user, require_role
from app.db.database import get_db
from app.models.bom import BOM, BOMItem
from app.models.part import Part, PartType, UnitOfMeasure
from app.models.user import User, UserRole
from app.schemas.part import PartCreate, PartResponse, PartUpdate
from app.services.audit_service import AuditService
from app.services.part_number_service import generate_werco_part_number, normalize_description

router = APIRouter()


class PartCsvImportError(BaseModel):
    row: int
    part_number: Optional[str] = None
    reason: str


class PartCsvImportResponse(BaseModel):
    imported_count: int
    skipped_count: int
    total_rows: int
    created_ids: List[int]
    errors: List[PartCsvImportError]


def _normalize_csv_header(value: str) -> str:
    return value.strip().lower().replace(" ", "_").replace("-", "_")


def _csv_row(raw_row: dict, header_map: dict) -> dict:
    row = {}
    for raw_key, raw_value in raw_row.items():
        if not raw_key:
            continue
        row[header_map.get(raw_key, _normalize_csv_header(raw_key))] = (raw_value or "").strip()
    return row


def _parse_bool(value: str, default: bool = False) -> bool:
    if value == "":
        return default
    normalized = value.strip().lower()
    if normalized in {"true", "1", "yes", "y", "active"}:
        return True
    if normalized in {"false", "0", "no", "n", "inactive"}:
        return False
    raise ValueError(f"Invalid boolean value '{value}'")


def _parse_float(value: str, field_name: str, default: float = 0.0) -> float:
    if value == "":
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a number") from exc


def _parse_int(value: str, field_name: str, default: int = 0) -> int:
    if value == "":
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an integer") from exc


@router.get("/", response_model=List[PartResponse], summary="List all parts")
def list_parts(
    skip: int = Query(0, ge=0, description="Number of records to skip for pagination"),
    limit: int = Query(100, ge=1, le=500, description="Maximum number of records to return"),
    search: Optional[str] = Query(
        None, description="Search in part number, name, description, or customer part number"
    ),
    part_type: Optional[PartType] = Query(
        None, description="Filter by part type (manufactured, purchased, assembly, raw_material)"
    ),
    active_only: bool = Query(True, description="Only return active parts"),
    include_bom_components: bool = Query(True, description="Include parts used as active BOM components"),
    include_deleted: bool = Query(False, description="Include soft-deleted parts (admin only)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """
    Retrieve a list of parts with optional filtering and pagination.

    - **skip**: Number of records to skip (for pagination)
    - **limit**: Maximum number of records to return (max 500)
    - **search**: Text search across part number, name, description, and customer part number
    - **part_type**: Filter by type (manufactured, purchased, assembly, raw_material)
    - **active_only**: When true, only returns active parts (default: true)
    - **include_deleted**: Include soft-deleted parts (admin only, default: false)

    Returns parts ordered by part number.
    """
    query = (
        db.query(Part)
        .filter(Part.company_id == company_id)
        .options(selectinload(Part.bom), selectinload(Part.inventory_items))
    )

    # Filter out soft-deleted unless explicitly requested by admin
    if not (include_deleted and current_user.role == UserRole.ADMIN):
        query = query.filter(Part.is_deleted == False)

    if active_only:
        query = query.filter(Part.is_active == True)

    if part_type:
        query = query.filter(Part.part_type == part_type)

    if not include_bom_components:
        component_part_ids = (
            db.query(BOMItem.component_part_id)
            .join(BOM, BOM.id == BOMItem.bom_id)
            .filter(
                BOM.company_id == company_id,
                BOM.is_active == True,
            )
        )
        query = query.filter(~Part.id.in_(component_part_ids))

    if search:
        search_filter = f"%{search}%"
        query = query.filter(
            or_(
                Part.part_number.ilike(search_filter),
                Part.name.ilike(search_filter),
                Part.description.ilike(search_filter),
                Part.customer_part_number.ilike(search_filter),
            )
        )

    # Also filter out parts with NULL part_type to prevent serialization errors
    query = query.filter(Part.part_type.isnot(None))

    parts = query.order_by(Part.part_number).offset(skip).limit(limit).all()

    def normalize_enum(value, fallback):
        if hasattr(value, "value"):
            return str(value.value).strip().lower()
        if isinstance(value, str):
            return value.strip().lower()
        return fallback

    # Build response manually to handle any edge cases
    result = []
    for part in parts:
        try:
            part_type_val = normalize_enum(part.part_type, PartType.MANUFACTURED.value)
            uom_val = normalize_enum(part.unit_of_measure, UnitOfMeasure.EACH.value)
            result.append(
                PartResponse(
                    id=part.id,
                    part_number=part.part_number or "",
                    revision=part.revision or "A",
                    name=part.name or "",
                    description=part.description,
                    part_type=PartType(part_type_val),
                    unit_of_measure=UnitOfMeasure(uom_val),
                    standard_cost=part.standard_cost or 0.0,
                    material_cost=part.material_cost or 0.0,
                    labor_cost=part.labor_cost or 0.0,
                    overhead_cost=part.overhead_cost or 0.0,
                    lead_time_days=part.lead_time_days or 0,
                    safety_stock=part.safety_stock or 0.0,
                    reorder_point=part.reorder_point or 0.0,
                    reorder_quantity=part.reorder_quantity or 0.0,
                    is_critical=part.is_critical or False,
                    requires_inspection=part.requires_inspection if part.requires_inspection is not None else True,
                    inspection_requirements=part.inspection_requirements,
                    customer_name=part.customer_name,
                    customer_part_number=part.customer_part_number,
                    drawing_number=part.drawing_number,
                    is_active=part.is_active if part.is_active is not None else True,
                    status=part.status or "active",
                    created_at=part.created_at,
                    updated_at=part.updated_at,
                    version=0,
                )
            )
        except Exception:
            pass  # Skip parts that fail to serialize

    return result


@router.post("/", response_model=PartResponse, status_code=status.HTTP_201_CREATED, summary="Create a new part")
def create_part(
    part_in: PartCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
):
    """
    Create a new part in the system.

    **Required roles**: Admin, Manager, or Supervisor

    The part number must be unique and will be automatically converted to uppercase.

    **Returns**: The created part with system-generated ID and timestamps.

    **Raises**:
    - 400: Part number already exists
    """
    if db.query(Part).filter(Part.part_number == part_in.part_number, Part.company_id == company_id).first():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Part number already exists")

    data = part_in.model_dump()
    # Normalize enum inputs in case clients send uppercase values or enum objects.
    part_type_val = data.get("part_type")
    if hasattr(part_type_val, "value"):
        data["part_type"] = str(part_type_val.value).strip().lower()
    elif isinstance(part_type_val, str):
        data["part_type"] = part_type_val.strip().lower()

    uom_val = data.get("unit_of_measure")
    if hasattr(uom_val, "value"):
        data["unit_of_measure"] = str(uom_val.value).strip().lower()
    elif isinstance(uom_val, str):
        data["unit_of_measure"] = uom_val.strip().lower()

    part = Part(**data, created_by=current_user.id)
    part.company_id = company_id
    db.add(part)
    db.commit()
    db.refresh(part)

    # Audit log
    audit = AuditService(db, current_user, request)
    audit.log_create("part", part.id, part.part_number, new_values=part)

    return part


@router.post("/import-csv", response_model=PartCsvImportResponse)
async def import_parts_csv(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
):
    """Import part master records from CSV with row-level errors."""
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Please upload a CSV file")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded CSV file is empty")

    try:
        decoded_content = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="CSV must be UTF-8 encoded")

    reader = csv.DictReader(io.StringIO(decoded_content))
    if not reader.fieldnames:
        raise HTTPException(status_code=400, detail="CSV must include a header row")

    header_map = {raw: _normalize_csv_header(raw) for raw in reader.fieldnames if raw}
    required_headers = {"part_number", "name", "part_type"}
    missing_headers = sorted(required_headers - set(header_map.values()))
    if missing_headers:
        raise HTTPException(status_code=400, detail=f"Missing required CSV columns: {', '.join(missing_headers)}")

    existing_part_numbers = {
        (value or "").strip().upper()
        for (value,) in db.query(Part.part_number).filter(Part.company_id == company_id).all()
    }

    errors: List[PartCsvImportError] = []
    created_ids: List[int] = []
    total_rows = 0

    for row_number, raw_row in enumerate(reader, start=2):
        row = _csv_row(raw_row, header_map)
        if not any(row.values()):
            continue

        total_rows += 1
        part_number = row.get("part_number", "").upper()

        try:
            if not part_number:
                raise ValueError("part_number is required")
            if part_number in existing_part_numbers:
                raise ValueError("Part number already exists")

            part_in = PartCreate(
                part_number=part_number,
                revision=row.get("revision") or "A",
                name=row.get("name", ""),
                description=row.get("description") or None,
                part_type=row.get("part_type", ""),
                unit_of_measure=row.get("unit_of_measure") or row.get("uom") or UnitOfMeasure.EACH.value,
                standard_cost=_parse_float(row.get("standard_cost", ""), "standard_cost"),
                material_cost=_parse_float(row.get("material_cost", ""), "material_cost"),
                labor_cost=_parse_float(row.get("labor_cost", ""), "labor_cost"),
                overhead_cost=_parse_float(row.get("overhead_cost", ""), "overhead_cost"),
                lead_time_days=_parse_int(row.get("lead_time_days", ""), "lead_time_days"),
                safety_stock=_parse_float(row.get("safety_stock", ""), "safety_stock"),
                reorder_point=_parse_float(row.get("reorder_point", ""), "reorder_point"),
                reorder_quantity=_parse_float(row.get("reorder_quantity", ""), "reorder_quantity"),
                is_critical=_parse_bool(row.get("is_critical", ""), False),
                requires_inspection=_parse_bool(row.get("requires_inspection", ""), True),
                inspection_requirements=row.get("inspection_requirements") or None,
                customer_name=row.get("customer_name") or None,
                customer_part_number=row.get("customer_part_number") or None,
                drawing_number=row.get("drawing_number") or None,
            )
        except (ValueError, ValidationError) as exc:
            errors.append(PartCsvImportError(row=row_number, part_number=part_number or None, reason=str(exc)))
            continue

        try:
            part = Part(**part_in.model_dump(), created_by=current_user.id)
            part.company_id = company_id
            part.is_active = _parse_bool(row.get("is_active", ""), True)
            part.status = row.get("status") or "active"
            db.add(part)
            db.commit()
            db.refresh(part)
        except Exception as exc:
            db.rollback()
            errors.append(PartCsvImportError(row=row_number, part_number=part_number, reason=str(exc)))
            continue

        existing_part_numbers.add(part.part_number.upper())
        created_ids.append(part.id)

    return PartCsvImportResponse(
        imported_count=len(created_ids),
        skipped_count=len(errors),
        total_rows=total_rows,
        created_ids=created_ids,
        errors=errors,
    )


@router.get("/{part_id}", response_model=PartResponse)
def get_part(
    part_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get a specific part"""
    part = db.query(Part).filter(Part.id == part_id, Part.company_id == company_id).first()
    if not part:
        raise HTTPException(status_code=404, detail="Part not found")
    return part


@router.get("/by-number/{part_number}", response_model=PartResponse)
def get_part_by_number(
    part_number: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get a part by part number"""
    part = db.query(Part).filter(Part.part_number == part_number, Part.company_id == company_id).first()
    if not part:
        raise HTTPException(status_code=404, detail="Part not found")
    return part


@router.put("/{part_id}", response_model=PartResponse)
def update_part(
    part_id: int,
    part_in: PartUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
):
    """Update a part"""
    part = db.query(Part).filter(Part.id == part_id, Part.company_id == company_id).first()
    if not part:
        raise HTTPException(status_code=404, detail="Part not found")

    # Capture old values for audit
    audit = AuditService(db, current_user, request)
    old_values = {c.key: getattr(part, c.key) for c in part.__table__.columns}

    update_data = part_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        if field == "part_type":
            if hasattr(value, "value"):
                value = str(value.value).strip().lower()
            elif isinstance(value, str):
                value = value.strip().lower()
        if field == "unit_of_measure":
            if hasattr(value, "value"):
                value = str(value.value).strip().lower()
            elif isinstance(value, str):
                value = value.strip().lower()
        setattr(part, field, value)

    db.commit()
    db.refresh(part)

    # Audit log
    audit.log_update("part", part.id, part.part_number, old_values=old_values, new_values=part)

    return part


@router.post("/{part_id}/revision")
def create_new_revision(
    part_id: int,
    new_revision: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
):
    """Create a new revision of a part (for AS9100D revision control)"""
    part = db.query(Part).filter(Part.id == part_id, Part.company_id == company_id).first()
    if not part:
        raise HTTPException(status_code=404, detail="Part not found")

    old_revision = part.revision
    part.revision = new_revision
    db.commit()

    return {
        "message": f"Part revision updated from {old_revision} to {new_revision}",
        "part_number": part.part_number,
        "new_revision": new_revision,
    }


@router.delete("/{part_id}")
def delete_part(
    part_id: int,
    request: Request,
    hard_delete: bool = Query(False, description="Permanently delete the record (admin only, use with caution)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN])),
    company_id: int = Depends(get_current_company_id),
):
    """
    Soft delete a part (default) or permanently delete (hard_delete=true).

    **Soft delete**: Marks the part as deleted but preserves data for recovery and audit trail.
    The part will be excluded from normal queries but can be restored.

    **Hard delete**: Permanently removes the record. Use with extreme caution.
    Only available if no dependencies exist (work orders, BOMs, etc.).
    """
    part = db.query(Part).filter(Part.id == part_id, Part.company_id == company_id).first()
    if not part:
        raise HTTPException(status_code=404, detail="Part not found")

    audit = AuditService(db, current_user, request)

    if hard_delete:
        # Check for dependencies before hard delete
        from app.models.bom import BOM, BOMItem
        from app.models.work_order import WorkOrder

        wo_count = db.query(WorkOrder).filter(WorkOrder.part_id == part_id).count()
        bom_count = db.query(BOM).filter(BOM.part_id == part_id).count()
        bom_item_count = db.query(BOMItem).filter(BOMItem.component_part_id == part_id).count()

        if wo_count > 0 or bom_count > 0 or bom_item_count > 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot hard delete: Part has {wo_count} work orders, {bom_count} BOMs, {bom_item_count} BOM references",
            )

        audit.log_delete("part", part.id, part.part_number)
        db.delete(part)
        db.commit()
        return {"message": "Part permanently deleted"}

    # Soft delete
    part.soft_delete(current_user.id)
    part.is_active = False
    part.status = "obsolete"
    db.commit()

    audit.log_delete("part", part.id, part.part_number, soft_delete=True)

    return {"message": "Part marked as deleted (soft delete)", "can_restore": True}


@router.post("/{part_id}/restore", summary="Restore a soft-deleted part")
def restore_part(
    part_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
):
    """
    Restore a soft-deleted part.

    **Required roles**: Admin or Manager

    Returns the part to active status and clears deletion metadata.
    """
    part = db.query(Part).filter(Part.id == part_id, Part.company_id == company_id).first()
    if not part:
        raise HTTPException(status_code=404, detail="Part not found")

    if not part.is_deleted:
        raise HTTPException(status_code=400, detail="Part is not deleted")

    audit = AuditService(db, current_user, request)

    part.restore()
    part.is_active = True
    part.status = "active"
    db.commit()

    audit.log_update(
        "part",
        part.id,
        part.part_number,
        old_values={"is_deleted": True, "status": "obsolete"},
        new_values={"is_deleted": False, "status": "active"},
        action="restore",
    )

    return {"message": "Part restored successfully", "part_id": part.id}


@router.get("/generate-number", summary="Generate Werco part number for raw material or hardware")
def generate_part_number(
    description: str = Query(..., min_length=3, description="Part description"),
    part_type: PartType = Query(..., description="Part type"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    if part_type not in [PartType.RAW_MATERIAL, PartType.HARDWARE, PartType.CONSUMABLE]:
        return {"suggested_part_number": None, "existing": False}

    normalized = " ".join(normalize_description(description).lower().split())
    existing = (
        db.query(Part)
        .filter(
            Part.company_id == company_id,
            Part.part_type == part_type,
            func.lower(func.trim(Part.description)) == normalized,
        )
        .first()
    )
    if existing:
        return {"suggested_part_number": existing.part_number, "existing": True}

    suggested = generate_werco_part_number(description, part_type.value)
    return {"suggested_part_number": suggested, "existing": False}
