from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
from sqlalchemy import or_
from app.db.database import get_db
from app.api.deps import get_current_user, require_role
from app.models.user import User, UserRole
from app.models.part import Part, PartType
from app.schemas.part import PartCreate, PartUpdate, PartResponse

router = APIRouter()


@router.get("/", response_model=List[PartResponse])
def list_parts(
    skip: int = 0,
    limit: int = 100,
    search: Optional[str] = None,
    part_type: Optional[PartType] = None,
    active_only: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List parts with optional filtering"""
    query = db.query(Part)
    
    if active_only:
        query = query.filter(Part.is_active == True)
    
    if part_type:
        query = query.filter(Part.part_type == part_type)
    
    if search:
        search_filter = f"%{search}%"
        query = query.filter(
            or_(
                Part.part_number.ilike(search_filter),
                Part.name.ilike(search_filter),
                Part.description.ilike(search_filter),
                Part.customer_part_number.ilike(search_filter)
            )
        )
    
    return query.order_by(Part.part_number).offset(skip).limit(limit).all()


@router.post("/", response_model=PartResponse)
def create_part(
    part_in: PartCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]))
):
    """Create a new part"""
    if db.query(Part).filter(Part.part_number == part_in.part_number).first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Part number already exists"
        )
    
    part = Part(**part_in.model_dump(), created_by=current_user.id)
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


@router.get("/{part_id}", response_model=PartResponse)
def get_part(
    part_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get a specific part"""
    part = db.query(Part).filter(Part.id == part_id).first()
    if not part:
        raise HTTPException(status_code=404, detail="Part not found")
    return part


@router.get("/by-number/{part_number}", response_model=PartResponse)
def get_part_by_number(
    part_number: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get a part by part number"""
    part = db.query(Part).filter(Part.part_number == part_number).first()
    if not part:
        raise HTTPException(status_code=404, detail="Part not found")
    return part


@router.put("/{part_id}", response_model=PartResponse)
def update_part(
    part_id: int,
    part_in: PartUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]))
):
    """Update a part"""
    part = db.query(Part).filter(Part.id == part_id).first()
    if not part:
        raise HTTPException(status_code=404, detail="Part not found")
    
    update_data = part_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(part, field, value)
    
    db.commit()
    db.refresh(part)
    return part


@router.post("/{part_id}/revision")
def create_new_revision(
    part_id: int,
    new_revision: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER]))
):
    """Create a new revision of a part (for AS9100D revision control)"""
    part = db.query(Part).filter(Part.id == part_id).first()
    if not part:
        raise HTTPException(status_code=404, detail="Part not found")
    
    old_revision = part.revision
    part.revision = new_revision
    db.commit()
    
    return {
        "message": f"Part revision updated from {old_revision} to {new_revision}",
        "part_number": part.part_number,
        "new_revision": new_revision
    }


@router.delete("/{part_id}")
def delete_part(
    part_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN]))
):
    """Soft delete a part (mark as inactive/obsolete)"""
    part = db.query(Part).filter(Part.id == part_id).first()
    if not part:
        raise HTTPException(status_code=404, detail="Part not found")
    
    part.is_active = False
    part.status = "obsolete"
    db.commit()
    return {"message": "Part marked as obsolete"}
