from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ValidationError
from sqlalchemy import or_
from sqlalchemy.orm import Session, selectinload

from app.api.deps import get_current_company_id, get_current_user, require_role
from app.api.endpoints.parts import (
    PartCsvImportError,
    _parse_bool,
    _parse_float,
    _parse_int,
    _part_to_response,
)
from app.db.database import get_db
from app.models.bom import BOM, BOMItem
from app.models.part import MATERIAL_SUPPLY_PART_TYPES, Part, PartType, UnitOfMeasure, is_material_supply_part_type
from app.models.user import User, UserRole
from app.schemas.part import PartCreate, PartResponse, PartUpdate
from app.services.audit_service import AuditService
from app.services.import_service import ImportFileError, parse_import_file

router = APIRouter()


class MaterialCsvImportResponse(BaseModel):
    imported_count: int
    skipped_count: int
    total_rows: int
    created_ids: List[int]
    errors: List[PartCsvImportError]
    dry_run: bool = False


def _require_material_type(part_type) -> None:
    if not is_material_supply_part_type(part_type):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Materials must be purchased, raw_material, hardware, or consumable.",
        )


@router.get("/", response_model=List[PartResponse], summary="List materials and supplies")
def list_materials(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    search: Optional[str] = Query(
        None, description="Search in item number, name, description, or supplier/customer number"
    ),
    part_type: Optional[PartType] = Query(None, description="Filter by material/supply type"),
    active_only: bool = Query(True),
    include_deleted: bool = Query(False, description="Include soft-deleted items (admin only)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    query = (
        db.query(Part)
        .filter(Part.company_id == company_id, Part.part_type.in_(MATERIAL_SUPPLY_PART_TYPES))
        .options(selectinload(Part.inventory_items))
    )

    if not (include_deleted and current_user.role == UserRole.ADMIN):
        query = query.filter(Part.is_deleted == False)
    if active_only:
        query = query.filter(Part.is_active == True)
    if part_type:
        _require_material_type(part_type)
        query = query.filter(Part.part_type == part_type)
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

    parts = query.order_by(Part.part_number).offset(skip).limit(limit).all()
    return [response for part in parts if (response := _part_to_response(part))]


@router.post("/", response_model=PartResponse, status_code=status.HTTP_201_CREATED, summary="Create material or supply")
def create_material(
    material_in: PartCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
):
    _require_material_type(material_in.part_type)
    if db.query(Part).filter(Part.part_number == material_in.part_number, Part.company_id == company_id).first():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Material number already exists")

    data = material_in.model_dump()
    part_type_val = data.get("part_type")
    data["part_type"] = part_type_val.value if hasattr(part_type_val, "value") else str(part_type_val).strip().lower()
    uom_val = data.get("unit_of_measure")
    data["unit_of_measure"] = uom_val.value if hasattr(uom_val, "value") else str(uom_val).strip().lower()

    material = Part(**data, created_by=current_user.id)
    material.company_id = company_id
    db.add(material)
    db.flush()  # assign PK without committing so the audit row carries resource_id

    AuditService(db, current_user, request).log_create(
        "material", material.id, material.part_number, new_values=material
    )
    db.commit()
    db.refresh(material)
    return material


@router.post("/import-csv", response_model=MaterialCsvImportResponse)
async def import_materials_csv(
    request: Request,
    file: UploadFile = File(...),
    dry_run: bool = Query(False, description="Validate only; no rows are written"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
):
    """Import material/supply master records from CSV or XLSX with row-level errors."""
    content = await file.read()
    # Parse + import are CPU/DB-bound sync work; run them in the threadpool so a
    # large upload can't stall the event loop (the request-scoped Session/audit
    # are used sequentially from one worker thread — same as a sync endpoint).
    try:
        table = await run_in_threadpool(
            parse_import_file, file.filename, content, required_columns={"part_number", "name", "part_type"}
        )
    except ImportFileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    def _run_import() -> MaterialCsvImportResponse:
        existing_part_numbers = {
            (value or "").strip().upper()
            for (value,) in db.query(Part.part_number).filter(Part.company_id == company_id).all()
        }

        audit = AuditService(db, current_user, request)
        errors: List[PartCsvImportError] = []
        created_ids: List[int] = []
        total_rows = 0
        accepted_count = 0

        for row_number, row in table.iter_rows():
            total_rows += 1
            part_number = row.get("part_number", "").upper()
            part_type = (row.get("part_type", "") or "").strip().lower()

            try:
                if not part_number:
                    raise ValueError("part_number is required")
                if part_number in existing_part_numbers:
                    raise ValueError("Material number already exists")
                if not is_material_supply_part_type(part_type):
                    raise ValueError("part_type must be purchased, raw_material, hardware, or consumable")

                material_in = PartCreate(
                    part_number=part_number,
                    revision=row.get("revision") or "A",
                    name=row.get("name", ""),
                    description=row.get("description") or None,
                    part_type=part_type,
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
                is_active = _parse_bool(row.get("is_active", ""), True)
            except (ValueError, ValidationError) as exc:
                errors.append(PartCsvImportError(row=row_number, part_number=part_number or None, reason=str(exc)))
                continue

            if dry_run:
                existing_part_numbers.add(part_number)
                accepted_count += 1
                continue

            try:
                material = Part(**material_in.model_dump(), created_by=current_user.id)
                material.company_id = company_id
                material.is_active = is_active
                material.status = row.get("status") or "active"
                db.add(material)
                db.flush()
                audit.log_create(
                    "material", material.id, material.part_number, new_values=material, extra_data={"source": "import"}
                )
                db.commit()
                db.refresh(material)
            except Exception as exc:
                db.rollback()
                errors.append(PartCsvImportError(row=row_number, part_number=part_number, reason=str(exc)))
                continue

            existing_part_numbers.add(material.part_number.upper())
            created_ids.append(material.id)
            accepted_count += 1

        return MaterialCsvImportResponse(
            imported_count=accepted_count,
            skipped_count=len(errors),
            total_rows=total_rows,
            created_ids=created_ids,
            errors=errors,
            dry_run=dry_run,
        )

    return await run_in_threadpool(_run_import)


@router.get("/{material_id}", response_model=PartResponse)
def get_material(
    material_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    material = (
        db.query(Part)
        .filter(Part.id == material_id, Part.company_id == company_id, Part.part_type.in_(MATERIAL_SUPPLY_PART_TYPES))
        .first()
    )
    if not material:
        raise HTTPException(status_code=404, detail="Material not found")
    return material


@router.put("/{material_id}", response_model=PartResponse)
def update_material(
    material_id: int,
    material_in: PartUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
):
    material = (
        db.query(Part)
        .filter(Part.id == material_id, Part.company_id == company_id, Part.part_type.in_(MATERIAL_SUPPLY_PART_TYPES))
        .first()
    )
    if not material:
        raise HTTPException(status_code=404, detail="Material not found")

    audit = AuditService(db, current_user, request)
    old_values = {c.key: getattr(material, c.key) for c in material.__table__.columns}

    update_data = material_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        if field == "part_type":
            if hasattr(value, "value"):
                value = str(value.value).strip().lower()
            elif isinstance(value, str):
                value = value.strip().lower()
            _require_material_type(value)
        if field == "unit_of_measure":
            if hasattr(value, "value"):
                value = str(value.value).strip().lower()
            elif isinstance(value, str):
                value = value.strip().lower()
        setattr(material, field, value)

    audit.log_update("material", material.id, material.part_number, old_values=old_values, new_values=material)
    db.commit()
    db.refresh(material)
    return material


@router.delete("/{material_id}")
def delete_material(
    material_id: int,
    request: Request,
    hard_delete: bool = Query(False, description="Permanently delete the record (admin only, use with caution)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN])),
    company_id: int = Depends(get_current_company_id),
):
    material = (
        db.query(Part)
        .filter(Part.id == material_id, Part.company_id == company_id, Part.part_type.in_(MATERIAL_SUPPLY_PART_TYPES))
        .first()
    )
    if not material:
        raise HTTPException(status_code=404, detail="Material not found")

    audit = AuditService(db, current_user, request)

    if hard_delete:
        from app.models.work_order import WorkOrder

        wo_count = db.query(WorkOrder).filter(WorkOrder.part_id == material_id).count()
        bom_count = db.query(BOM).filter(BOM.part_id == material_id).count()
        bom_item_count = db.query(BOMItem).filter(BOMItem.component_part_id == material_id).count()

        if wo_count > 0 or bom_count > 0 or bom_item_count > 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot hard delete: Material has {wo_count} work orders, {bom_count} BOMs, {bom_item_count} BOM references",
            )

        audit.log_delete("material", material.id, material.part_number)
        db.delete(material)
        db.commit()
        return {"message": "Material permanently deleted"}

    material.soft_delete(current_user.id)
    material.is_active = False
    material.status = "obsolete"
    audit.log_delete("material", material.id, material.part_number, soft_delete=True)
    db.commit()
    return {"message": "Material marked as deleted (soft delete)", "can_restore": True}
