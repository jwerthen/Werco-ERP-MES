from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from app.db.database import get_db
from app.api.deps import get_current_user, require_role
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.schemas.work_center import WorkCenterCreate, WorkCenterUpdate, WorkCenterResponse

router = APIRouter()


@router.get("/", response_model=List[WorkCenterResponse])
def list_work_centers(
    skip: int = 0,
    limit: int = 100,
    active_only: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all work centers"""
    query = db.query(WorkCenter)
    if active_only:
        query = query.filter(WorkCenter.is_active == True)
    return query.offset(skip).limit(limit).all()


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
