from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from app.db.database import get_db
from app.api.deps import get_current_user, require_role
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.schemas.work_center import WorkCenterCreate, WorkCenterUpdate, WorkCenterResponse
from app.core.cache import (
    cache, CacheKeys, CacheTTL,
    get_cached_work_centers_list, cache_work_centers_list, invalidate_work_centers_cache
)

router = APIRouter()


@router.get("/", response_model=List[WorkCenterResponse])
def list_work_centers(
    skip: int = 0,
    limit: int = 100,
    active_only: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all work centers (cached for 15 minutes)"""
    # Try cache first (only for default parameters)
    if skip == 0 and limit == 100 and active_only:
        cached = get_cached_work_centers_list()
        if cached is not None:
            return cached
    
    query = db.query(WorkCenter)
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
                "work_center_type": wc.work_center_type.value if wc.work_center_type else "fabrication",
                "description": wc.description or "",
                "hourly_rate": float(wc.hourly_rate) if wc.hourly_rate else 0.0,
                "capacity_hours_per_day": float(wc.capacity_hours_per_day) if wc.capacity_hours_per_day else 8.0,
                "efficiency_factor": float(wc.efficiency_factor) if wc.efficiency_factor else 1.0,
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
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER]))
):
    """Create a new work center"""
    # Check if code already exists
    if db.query(WorkCenter).filter(WorkCenter.code == work_center_in.code).first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Work center code already exists"
        )
    
    work_center = WorkCenter(**work_center_in.model_dump())
    db.add(work_center)
    db.commit()
    db.refresh(work_center)
    
    # Invalidate cache
    invalidate_work_centers_cache()
    
    return work_center


@router.get("/{work_center_id}", response_model=WorkCenterResponse)
def get_work_center(
    work_center_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get a specific work center"""
    work_center = db.query(WorkCenter).filter(WorkCenter.id == work_center_id).first()
    if not work_center:
        raise HTTPException(status_code=404, detail="Work center not found")
    return work_center


@router.put("/{work_center_id}", response_model=WorkCenterResponse)
def update_work_center(
    work_center_id: int,
    work_center_in: WorkCenterUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER]))
):
    """Update a work center"""
    work_center = db.query(WorkCenter).filter(WorkCenter.id == work_center_id).first()
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
    current_user: User = Depends(require_role([UserRole.ADMIN]))
):
    """Soft delete a work center"""
    work_center = db.query(WorkCenter).filter(WorkCenter.id == work_center_id).first()
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
    current_user: User = Depends(get_current_user)
):
    """Update work center status (available, in_use, maintenance, offline)"""
    valid_statuses = ["available", "in_use", "maintenance", "offline"]
    if status not in valid_statuses:
        raise HTTPException(
            status_code=400, 
            detail=f"Invalid status. Must be one of: {valid_statuses}"
        )
    
    work_center = db.query(WorkCenter).filter(WorkCenter.id == work_center_id).first()
    if not work_center:
        raise HTTPException(status_code=404, detail="Work center not found")
    
    work_center.current_status = status
    db.commit()
    return {"message": f"Work center status updated to {status}"}
