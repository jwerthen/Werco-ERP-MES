from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from app.api.deps import get_current_company_id, get_current_user, require_role
from app.core.cache import (
    cache_work_centers_list,
    get_cached_work_centers_list,
    invalidate_work_centers_cache,
)
from app.core.realtime import safe_broadcast
from app.core.websocket import broadcast_dashboard_update, broadcast_shop_floor_update
from app.db.database import get_db
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.schemas.work_center import WorkCenterCreate, WorkCenterResponse, WorkCenterUpdate
from app.services.audit_service import AuditService
from app.services.import_service import ImportFileError, parse_import_file
from app.services.work_center_type_service import get_work_center_types, normalize_work_center_type

router = APIRouter()


class WorkCenterCsvImportError(BaseModel):
    row: int
    code: Optional[str] = None
    reason: str


class WorkCenterCsvImportResponse(BaseModel):
    imported_count: int
    skipped_count: int
    total_rows: int
    created_ids: List[int]
    errors: List[WorkCenterCsvImportError]
    dry_run: bool = False


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


@router.get("/types")
def list_work_center_types(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """List available work center types"""
    return {"types": get_work_center_types(db, company_id=company_id)}


@router.get("/", response_model=List[WorkCenterResponse])
def list_work_centers(
    skip: int = 0,
    limit: int = 100,
    active_only: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """List all work centers (cached for 15 minutes)"""
    # Try cache first (only for default parameters)
    if skip == 0 and limit == 100 and active_only:
        cached = get_cached_work_centers_list()
        if cached is not None:
            return cached

    query = db.query(WorkCenter).filter(WorkCenter.company_id == company_id)
    if active_only:
        query = query.filter(WorkCenter.is_active == True)
    result = query.offset(skip).limit(limit).all()

    # Cache the result for default parameters
    if skip == 0 and limit == 100 and active_only:
        # Convert to dict for caching - must include ALL fields required by WorkCenterResponse
        cache_data = [
            {
                "id": wc.id,
                "code": wc.code,
                "name": wc.name,
                "work_center_type": wc.work_center_type or "fabrication",
                "description": wc.description or "",
                "hourly_rate": float(wc.hourly_rate) if wc.hourly_rate else 0.0,
                "capacity_hours_per_day": float(wc.capacity_hours_per_day) if wc.capacity_hours_per_day else 8.0,
                "efficiency_factor": float(wc.efficiency_factor) if wc.efficiency_factor else 1.0,
                "availability_rate": float(wc.availability_rate) if wc.availability_rate is not None else 100.0,
                "building": wc.building,
                "area": wc.area,
                "is_active": wc.is_active,
                "current_status": wc.current_status or "available",
                "version": getattr(wc, 'version', 0),
                "created_at": wc.created_at.isoformat() if wc.created_at else None,
                "updated_at": wc.updated_at.isoformat() if wc.updated_at else None,
            }
            for wc in result
        ]
        cache_work_centers_list(cache_data)

    return result


@router.post("/", response_model=WorkCenterResponse)
def create_work_center(
    work_center_in: WorkCenterCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
):
    """Create a new work center"""
    # Check if code already exists
    if db.query(WorkCenter).filter(WorkCenter.code == work_center_in.code, WorkCenter.company_id == company_id).first():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Work center code already exists")

    work_center = WorkCenter(**work_center_in.model_dump())
    work_center.company_id = company_id
    db.add(work_center)
    db.commit()
    db.refresh(work_center)

    # Invalidate cache
    invalidate_work_centers_cache()

    return work_center


@router.post("/import-csv", response_model=WorkCenterCsvImportResponse)
async def import_work_centers_csv(
    request: Request,
    file: UploadFile = File(...),
    dry_run: bool = Query(False, description="Validate only; no rows are written"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
):
    """Import work centers from CSV or XLSX with row-level errors."""
    content = await file.read()
    try:
        table = parse_import_file(file.filename, content, required_columns={"code", "name", "work_center_type"})
    except ImportFileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    existing_codes = {
        (value or "").strip().upper()
        for (value,) in db.query(WorkCenter.code).filter(WorkCenter.company_id == company_id).all()
    }
    valid_types = set(get_work_center_types(db, company_id=company_id))
    valid_statuses = {"available", "in_use", "maintenance", "offline"}

    audit = AuditService(db, current_user, request)
    errors: List[WorkCenterCsvImportError] = []
    created_ids: List[int] = []
    total_rows = 0
    accepted_count = 0

    for row_number, row in table.iter_rows():
        total_rows += 1
        code = row.get("code", "").upper()
        work_center_type = normalize_work_center_type(row.get("work_center_type", ""))
        current_status = row.get("current_status") or "available"

        try:
            if not code:
                raise ValueError("code is required")
            if code in existing_codes:
                raise ValueError("Work center code already exists")
            if work_center_type not in valid_types:
                raise ValueError(f"Invalid work_center_type '{work_center_type}'")
            if current_status not in valid_statuses:
                raise ValueError(f"Invalid current_status '{current_status}'")

            work_center_in = WorkCenterCreate(
                code=code,
                name=row.get("name", ""),
                work_center_type=work_center_type,
                description=row.get("description") or None,
                hourly_rate=_parse_float(row.get("hourly_rate", ""), "hourly_rate"),
                capacity_hours_per_day=_parse_float(
                    row.get("capacity_hours_per_day", ""), "capacity_hours_per_day", 8.0
                ),
                efficiency_factor=_parse_float(row.get("efficiency_factor", ""), "efficiency_factor", 1.0),
                building=row.get("building") or None,
                area=row.get("area") or None,
            )
            is_active = _parse_bool(row.get("is_active", ""), True)
            availability_rate = _parse_float(row.get("availability_rate", ""), "availability_rate", 100.0)
        except (ValueError, ValidationError) as exc:
            errors.append(WorkCenterCsvImportError(row=row_number, code=code or None, reason=str(exc)))
            continue

        if dry_run:
            existing_codes.add(code)
            accepted_count += 1
            continue

        try:
            work_center = WorkCenter(**work_center_in.model_dump())
            work_center.company_id = company_id
            work_center.current_status = current_status
            work_center.is_active = is_active
            work_center.availability_rate = availability_rate
            db.add(work_center)
            db.flush()
            audit.log_create(
                "work_center", work_center.id, work_center.code, new_values=work_center, extra_data={"source": "import"}
            )
            db.commit()
            db.refresh(work_center)
        except Exception as exc:
            db.rollback()
            errors.append(WorkCenterCsvImportError(row=row_number, code=code, reason=str(exc)))
            continue

        existing_codes.add(work_center.code.upper())
        created_ids.append(work_center.id)
        accepted_count += 1

    if created_ids:
        invalidate_work_centers_cache()

    return WorkCenterCsvImportResponse(
        imported_count=accepted_count,
        skipped_count=len(errors),
        total_rows=total_rows,
        created_ids=created_ids,
        errors=errors,
        dry_run=dry_run,
    )


@router.get("/{work_center_id}", response_model=WorkCenterResponse)
def get_work_center(
    work_center_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get a specific work center"""
    work_center = (
        db.query(WorkCenter).filter(WorkCenter.id == work_center_id, WorkCenter.company_id == company_id).first()
    )
    if not work_center:
        raise HTTPException(status_code=404, detail="Work center not found")
    return work_center


@router.put("/{work_center_id}", response_model=WorkCenterResponse)
def update_work_center(
    work_center_id: int,
    work_center_in: WorkCenterUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
):
    """Update a work center"""
    work_center = (
        db.query(WorkCenter).filter(WorkCenter.id == work_center_id, WorkCenter.company_id == company_id).first()
    )
    if not work_center:
        raise HTTPException(status_code=404, detail="Work center not found")

    update_data = work_center_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(work_center, field, value)

    db.commit()
    db.refresh(work_center)

    # Invalidate cache
    invalidate_work_centers_cache(work_center_id)

    return work_center


@router.delete("/{work_center_id}")
def delete_work_center(
    work_center_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN])),
    company_id: int = Depends(get_current_company_id),
):
    """Soft delete a work center"""
    work_center = (
        db.query(WorkCenter).filter(WorkCenter.id == work_center_id, WorkCenter.company_id == company_id).first()
    )
    if not work_center:
        raise HTTPException(status_code=404, detail="Work center not found")

    work_center.is_active = False
    db.commit()

    # Invalidate cache
    invalidate_work_centers_cache(work_center_id)

    return {"message": "Work center deactivated"}


@router.post("/{work_center_id}/status")
def update_work_center_status(
    work_center_id: int,
    status: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Update work center status (available, in_use, maintenance, offline)"""
    valid_statuses = ["available", "in_use", "maintenance", "offline"]
    if status not in valid_statuses:
        raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of: {valid_statuses}")

    work_center = (
        db.query(WorkCenter).filter(WorkCenter.id == work_center_id, WorkCenter.company_id == company_id).first()
    )
    if not work_center:
        raise HTTPException(status_code=404, detail="Work center not found")

    work_center.current_status = status
    db.commit()
    safe_broadcast(
        broadcast_shop_floor_update,
        work_center_id,
        {
            "event": "work_center_status",
            "work_center_id": work_center_id,
            "status": status,
        },
    )
    safe_broadcast(
        broadcast_dashboard_update,
        {
            "event": "work_center_status",
            "work_center_id": work_center_id,
            "status": status,
        },
    )
    return {"message": f"Work center status updated to {status}"}
