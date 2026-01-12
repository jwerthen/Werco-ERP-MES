from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query, Request
from sqlalchemy.orm import Session
from sqlalchemy import or_
from app.db.database import get_db
from app.api.deps import get_current_user, require_role
from app.models.user import User, UserRole
from app.models.part import Part, PartType
from app.schemas.part import PartCreate, PartUpdate, PartResponse
from app.services.audit_service import AuditService

router = APIRouter()


@router.get("/", response_model=List[PartResponse], summary="List all parts")
def list_parts(
    skip: int = Query(0, ge=0, description="Number of records to skip for pagination"),
    limit: int = Query(100, ge=1, le=500, description="Maximum number of records to return"),
    search: Optional[str] = Query(None, description="Search in part number, name, description, or customer part number"),
    part_type: Optional[PartType] = Query(None, description="Filter by part type (manufactured, purchased, assembly, raw_material)"),
    active_only: bool = Query(True, description="Only return active parts"),
    include_deleted: bool = Query(False, description="Include soft-deleted parts (admin only)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
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
    query = db.query(Part)
    
    # Filter out soft-deleted unless explicitly requested by admin
    if not include_deleted or current_user.role != UserRole.ADMIN:
        query = query.filter(Part.is_deleted == False)
    
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


@router.post("/", response_model=PartResponse, status_code=status.HTTP_201_CREATED, summary="Create a new part")
def create_part(
    part_in: PartCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]))
):
    """
    Create a new part in the system.
    
    **Required roles**: Admin, Manager, or Supervisor
    
    The part number must be unique and will be automatically converted to uppercase.
    
    **Returns**: The created part with system-generated ID and timestamps.
    
    **Raises**:
    - 400: Part number already exists
    """
    if db.query(Part).filter(Part.part_number == part_in.part_number).first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Part number already exists"
        )
    
    part = Part(**part_in.model_dump(), created_by=current_user.id)
    db.add(part)
    db.commit()
    db.refresh(part)
    
    # Audit log
    audit = AuditService(db, current_user, request)
    audit.log_create("part", part.id, part.part_number, new_values=part)
    
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
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]))
):
    """Update a part"""
    part = db.query(Part).filter(Part.id == part_id).first()
    if not part:
        raise HTTPException(status_code=404, detail="Part not found")
    
    # Capture old values for audit
    audit = AuditService(db, current_user, request)
    old_values = {c.key: getattr(part, c.key) for c in part.__table__.columns}
    
    update_data = part_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
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
    request: Request,
    hard_delete: bool = Query(False, description="Permanently delete the record (admin only, use with caution)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN]))
):
    """
    Soft delete a part (default) or permanently delete (hard_delete=true).
    
    **Soft delete**: Marks the part as deleted but preserves data for recovery and audit trail.
    The part will be excluded from normal queries but can be restored.
    
    **Hard delete**: Permanently removes the record. Use with extreme caution.
    Only available if no dependencies exist (work orders, BOMs, etc.).
    """
    part = db.query(Part).filter(Part.id == part_id).first()
    if not part:
        raise HTTPException(status_code=404, detail="Part not found")
    
    audit = AuditService(db, current_user, request)
    
    if hard_delete:
        # Check for dependencies before hard delete
        from app.models.work_order import WorkOrder
        from app.models.bom import BOM, BOMItem
        
        wo_count = db.query(WorkOrder).filter(WorkOrder.part_id == part_id).count()
        bom_count = db.query(BOM).filter(BOM.part_id == part_id).count()
        bom_item_count = db.query(BOMItem).filter(BOMItem.component_part_id == part_id).count()
        
        if wo_count > 0 or bom_count > 0 or bom_item_count > 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot hard delete: Part has {wo_count} work orders, {bom_count} BOMs, {bom_item_count} BOM references"
            )
        
        audit.log_delete("part", part.id, part.part_number)
        db.delete(part)
        db.commit()
        return {"message": "Part permanently deleted"}
    
    # Soft delete
    old_values = {"is_deleted": part.is_deleted, "status": part.status}
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
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER]))
):
    """
    Restore a soft-deleted part.
    
    **Required roles**: Admin or Manager
    
    Returns the part to active status and clears deletion metadata.
    """
    part = db.query(Part).filter(Part.id == part_id).first()
    if not part:
        raise HTTPException(status_code=404, detail="Part not found")
    
    if not part.is_deleted:
        raise HTTPException(status_code=400, detail="Part is not deleted")
    
    audit = AuditService(db, current_user, request)
    
    part.restore()
    part.is_active = True
    part.status = "active"
    db.commit()
    
    audit.log_update("part", part.id, part.part_number, 
                    old_values={"is_deleted": True, "status": "obsolete"},
                    new_values={"is_deleted": False, "status": "active"},
                    action="restore")
    
    return {"message": "Part restored successfully", "part_id": part.id}
