import csv
import io
from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
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
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
):
    """Import work centers from CSV with row-level errors."""
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
    required_headers = {"code", "name", "work_center_type"}
    missing_headers = sorted(required_headers - set(header_map.values()))
    if missing_headers:
        raise HTTPException(status_code=400, detail=f"Missing required CSV columns: {', '.join(missing_headers)}")

    existing_codes = {
        (value or "").strip().upper()
        for (value,) in db.query(WorkCenter.code).filter(WorkCenter.company_id == company_id).all()
    }
    valid_types = set(get_work_center_types(db, company_id=company_id))
    valid_statuses = {"available", "in_use", "maintenance", "offline"}

    errors: List[WorkCenterCsvImportError] = []
    created_ids: List[int] = []
    total_rows = 0

    for row_number, raw_row in enumerate(reader, start=2):
        row = _csv_row(raw_row, header_map)
        if not any(row.values()):
            continue

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
        except (ValueError, ValidationError) as exc:
            errors.append(WorkCenterCsvImportError(row=row_number, code=code or None, reason=str(exc)))
            continue

        try:
            work_center = WorkCenter(**work_center_in.model_dump())
            work_center.company_id = company_id
            work_center.current_status = current_status
            work_center.is_active = _parse_bool(row.get("is_active", ""), True)
            work_center.availability_rate = _parse_float(row.get("availability_rate", ""), "availability_rate", 100.0)
            db.add(work_center)
            db.commit()
            db.refresh(work_center)
        except Exception as exc:
            db.rollback()
            errors.append(WorkCenterCsvImportError(row=row_number, code=code, reason=str(exc)))
            continue

        existing_codes.add(work_center.code.upper())
        created_ids.append(work_center.id)

    if created_ids:
        invalidate_work_centers_cache()

    return WorkCenterCsvImportResponse(
        imported_count=len(created_ids),
        skipped_count=len(errors),
        total_rows=total_rows,
        created_ids=created_ids,
        errors=errors,
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
