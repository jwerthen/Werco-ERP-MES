from typing import List, Optional, Set
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import and_
from app.db.database import get_db
from app.api.deps import get_current_user, require_role
from app.models.user import User, UserRole
from app.models.bom import BOM, BOMItem, BOMItemType, BOMLineType
from app.models.part import Part
from app.schemas.bom import (
    BOMCreate, BOMUpdate, BOMResponse,
    BOMItemCreate, BOMItemUpdate, BOMItemResponse,
    BOMExploded, BOMItemWithChildren, BOMFlattened, BOMFlatItem,
    ComponentPartInfo, PartInfo
)

router = APIRouter()


def get_component_part_info(part: Part, db: Session) -> ComponentPartInfo:
    """Build component part info with has_bom flag - handles NULL values defensively"""
    has_bom = db.query(BOM).filter(BOM.part_id == part.id, BOM.is_active == True).first() is not None
    return ComponentPartInfo(
        id=part.id,
        part_number=part.part_number or "",
        name=part.name or "",
        revision=part.revision or "A",
        part_type=part.part_type.value if part.part_type else "manufactured",
        has_bom=has_bom
    )


def build_bom_item_response(item: BOMItem, db: Session) -> BOMItemResponse:
    """Build BOM item response with part info - handles NULL values defensively"""
    # Handle component_part safely - it might be None if the part was deleted
    component_info = None
    if item.component_part:
        try:
            component_info = get_component_part_info(item.component_part, db)
        except Exception:
            pass  # Silently handle any errors getting component info
    
    return BOMItemResponse(
        id=item.id,
        bom_id=item.bom_id,
        component_part_id=item.component_part_id,
        item_number=item.item_number if item.item_number is not None else 10,
        quantity=item.quantity if item.quantity is not None else 1.0,
        item_type=item.item_type if item.item_type else BOMItemType.MAKE,
        line_type=item.line_type if item.line_type else BOMLineType.COMPONENT,
        unit_of_measure=item.unit_of_measure or "each",
        reference_designator=item.reference_designator,
        find_number=item.find_number,
        notes=item.notes,
        torque_spec=item.torque_spec,
        installation_notes=item.installation_notes,
        work_center_id=item.work_center_id,
        operation_sequence=item.operation_sequence if item.operation_sequence is not None else 10,
        scrap_factor=item.scrap_factor if item.scrap_factor is not None else 0.0,
        lead_time_offset=item.lead_time_offset if item.lead_time_offset is not None else 0,
        is_optional=item.is_optional if item.is_optional is not None else False,
        is_alternate=item.is_alternate if item.is_alternate is not None else False,
        alternate_group=item.alternate_group,
        component_part=component_info,
        created_at=item.created_at,
        updated_at=item.updated_at
    )


@router.get("/", response_model=List[BOMResponse])
def list_boms(
    skip: int = 0,
    limit: int = 100,
    status: Optional[str] = None,
    active_only: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all BOMs"""
    # Simple query without eager loading to avoid join issues
    query = db.query(BOM)
    
    if active_only:
        query = query.filter(BOM.is_active == True)
    
    if status:
        query = query.filter(BOM.status == status)
    
    boms = query.offset(skip).limit(limit).all()
    
    result = []
    for bom in boms:
        try:
            # Load part separately
            part = db.query(Part).filter(Part.id == bom.part_id).first()
            
            # Build part info safely
            part_info = None
            if part:
                part_info = PartInfo(
                    id=part.id,
                    part_number=part.part_number or "",
                    name=part.name or "",
                    revision=part.revision or "A",
                    part_type=part.part_type.value if part.part_type else "manufactured"
                )
            
            # Load items separately
            items = db.query(BOMItem).filter(BOMItem.bom_id == bom.id).all()
            items_list = []
            for item in items:
                try:
                    # Load component part for this item
                    component = db.query(Part).filter(Part.id == item.component_part_id).first()
                    
                    component_info = None
                    if component:
                        has_bom = db.query(BOM).filter(BOM.part_id == component.id, BOM.is_active == True).first() is not None
                        component_info = ComponentPartInfo(
                            id=component.id,
                            part_number=component.part_number or "",
                            name=component.name or "",
                            revision=component.revision or "A",
                            part_type=component.part_type.value if component.part_type else "manufactured",
                            has_bom=has_bom
                        )
                    
                    items_list.append(BOMItemResponse(
                        id=item.id,
                        bom_id=item.bom_id,
                        component_part_id=item.component_part_id,
                        item_number=item.item_number if item.item_number is not None else 10,
                        quantity=item.quantity if item.quantity is not None else 1.0,
                        item_type=item.item_type if item.item_type else BOMItemType.MAKE,
                        line_type=item.line_type if item.line_type else BOMLineType.COMPONENT,
                        unit_of_measure=item.unit_of_measure or "each",
                        reference_designator=item.reference_designator,
                        find_number=item.find_number,
                        notes=item.notes,
                        torque_spec=item.torque_spec,
                        installation_notes=item.installation_notes,
                        work_center_id=item.work_center_id,
                        operation_sequence=item.operation_sequence if item.operation_sequence is not None else 10,
                        scrap_factor=item.scrap_factor if item.scrap_factor is not None else 0.0,
                        lead_time_offset=item.lead_time_offset if item.lead_time_offset is not None else 0,
                        is_optional=item.is_optional if item.is_optional is not None else False,
                        is_alternate=item.is_alternate if item.is_alternate is not None else False,
                        alternate_group=item.alternate_group,
                        component_part=component_info,
                        created_at=item.created_at,
                        updated_at=item.updated_at
                    ))
                except Exception:
                    pass  # Skip items that fail
            
            bom_response = BOMResponse(
                id=bom.id,
                part_id=bom.part_id,
                revision=bom.revision or "A",
                description=bom.description or "",
                bom_type=bom.bom_type or "standard",
                status=bom.status or "draft",
                is_active=bom.is_active if bom.is_active is not None else True,
                effective_date=bom.effective_date,
                created_at=bom.created_at,
                updated_at=bom.updated_at,
                part=part_info,
                items=items_list
            )
            result.append(bom_response)
        except Exception:
            pass  # Skip BOMs that fail
    
    return result


@router.post("/", response_model=BOMResponse)
def create_bom(
    bom_in: BOMCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]))
):
    """Create a new BOM for a part"""
    # Check if part exists
    part = db.query(Part).filter(Part.id == bom_in.part_id).first()
    if not part:
        raise HTTPException(status_code=404, detail="Part not found")
    
    # Check if BOM already exists for this part
    existing = db.query(BOM).filter(BOM.part_id == bom_in.part_id, BOM.is_active == True).first()
    if existing:
        raise HTTPException(
            status_code=400,
            detail=f"Active BOM already exists for part {part.part_number}. Deactivate it first or update the existing BOM."
        )
    
    # Create BOM
    bom = BOM(
        part_id=bom_in.part_id,
        revision=bom_in.revision,
        description=bom_in.description,
        bom_type=bom_in.bom_type,
        created_by=current_user.id
    )
    db.add(bom)
    db.flush()
    
    # Add items
    for item_data in bom_in.items:
        # Validate component part exists
        component = db.query(Part).filter(Part.id == item_data.component_part_id).first()
        if not component:
            raise HTTPException(status_code=400, detail=f"Component part ID {item_data.component_part_id} not found")
        
        # Check for circular reference
        if item_data.component_part_id == bom_in.part_id:
            raise HTTPException(status_code=400, detail="BOM cannot contain itself as a component")
        
        item = BOMItem(
            bom_id=bom.id,
            **item_data.model_dump()
        )
        db.add(item)
    
    db.commit()
    db.refresh(bom)
    
    # Return with full response
    return get_bom(bom.id, db, current_user)


@router.get("/{bom_id}", response_model=BOMResponse)
def get_bom(
    bom_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get a specific BOM with all items"""
    bom = db.query(BOM).options(
        joinedload(BOM.part),
        joinedload(BOM.items).joinedload(BOMItem.component_part)
    ).filter(BOM.id == bom_id).first()
    
    if not bom:
        raise HTTPException(status_code=404, detail="BOM not found")
    
    return BOMResponse(
        id=bom.id,
        part_id=bom.part_id,
        revision=bom.revision,
        description=bom.description,
        bom_type=bom.bom_type or "standard",
        status=bom.status,
        is_active=bom.is_active,
        effective_date=bom.effective_date,
        created_at=bom.created_at,
        updated_at=bom.updated_at,
        part=PartInfo(
            id=bom.part.id,
            part_number=bom.part.part_number or "",
            name=bom.part.name or "",
            revision=bom.part.revision or "A",
            part_type=bom.part.part_type.value if bom.part.part_type else "manufactured"
        ) if bom.part else None,
        items=[build_bom_item_response(item, db) for item in bom.items]
    )


@router.get("/by-part/{part_id}", response_model=BOMResponse)
def get_bom_by_part(
    part_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get the active BOM for a part"""
    bom = db.query(BOM).options(
        joinedload(BOM.part),
        joinedload(BOM.items).joinedload(BOMItem.component_part)
    ).filter(BOM.part_id == part_id, BOM.is_active == True).first()
    
    if not bom:
        raise HTTPException(status_code=404, detail="No active BOM found for this part")
    
    return get_bom(bom.id, db, current_user)


@router.put("/{bom_id}", response_model=BOMResponse)
def update_bom(
    bom_id: int,
    bom_in: BOMUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]))
):
    """Update a BOM"""
    bom = db.query(BOM).filter(BOM.id == bom_id).first()
    if not bom:
        raise HTTPException(status_code=404, detail="BOM not found")
    
    update_data = bom_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(bom, field, value)
    
    db.commit()
    db.refresh(bom)
    return get_bom(bom.id, db, current_user)


@router.post("/{bom_id}/release")
def release_bom(
    bom_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER]))
):
    """Release a BOM for production use"""
    bom = db.query(BOM).filter(BOM.id == bom_id).first()
    if not bom:
        raise HTTPException(status_code=404, detail="BOM not found")
    
    if bom.status == "released":
        raise HTTPException(status_code=400, detail="BOM is already released")
    
    if not bom.items:
        raise HTTPException(status_code=400, detail="Cannot release BOM with no items")
    
    bom.status = "released"
    bom.approved_by = current_user.id
    bom.approved_at = datetime.utcnow()
    bom.effective_date = datetime.utcnow()
    
    db.commit()
    return {"message": "BOM released", "bom_id": bom.id}


@router.delete("/{bom_id}")
def delete_bom(
    bom_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER]))
):
    """Delete a BOM (only draft BOMs can be deleted)"""
    bom = db.query(BOM).filter(BOM.id == bom_id).first()
    if not bom:
        raise HTTPException(status_code=404, detail="BOM not found")
    
    if bom.status != "draft":
        raise HTTPException(status_code=400, detail="Only draft BOMs can be deleted")
    
    # Delete all items first
    db.query(BOMItem).filter(BOMItem.bom_id == bom_id).delete()
    
    db.delete(bom)
    db.commit()
    return {"message": "BOM deleted"}


# BOM Item operations
@router.post("/{bom_id}/items", response_model=BOMItemResponse)
def add_bom_item(
    bom_id: int,
    item_in: BOMItemCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]))
):
    """Add an item to a BOM"""
    try:
        bom = db.query(BOM).filter(BOM.id == bom_id).first()
        if not bom:
            raise HTTPException(status_code=404, detail="BOM not found")
        
        # Validate component exists
        component = db.query(Part).filter(Part.id == item_in.component_part_id).first()
        if not component:
            raise HTTPException(status_code=404, detail="Component part not found")
        
        # Check for circular reference
        if item_in.component_part_id == bom.part_id:
            raise HTTPException(status_code=400, detail="BOM cannot contain itself")
        
        # Check for deeper circular references
        if would_create_circular_reference(db, bom.part_id, item_in.component_part_id):
            raise HTTPException(
                status_code=400, 
                detail="Adding this component would create a circular reference in the BOM structure"
            )
        
        # Inherit customer_name from parent assembly if component doesn't have one
        parent_part = db.query(Part).filter(Part.id == bom.part_id).first()
        if parent_part and parent_part.customer_name and not component.customer_name:
            component.customer_name = parent_part.customer_name
        
        # Get item data and ensure enum values are lowercase for PostgreSQL
        item_data = item_in.model_dump()
        if 'item_type' in item_data and item_data['item_type']:
            item_data['item_type'] = item_data['item_type'].lower() if isinstance(item_data['item_type'], str) else item_data['item_type'].value
        if 'line_type' in item_data and item_data['line_type']:
            item_data['line_type'] = item_data['line_type'].lower() if isinstance(item_data['line_type'], str) else item_data['line_type'].value
        
        item = BOMItem(bom_id=bom_id, **item_data)
        db.add(item)
        db.commit()
        db.refresh(item)
        
        # Build response manually to avoid joinedload issues
        component_info = None
        if component:
            has_bom = db.query(BOM).filter(BOM.part_id == component.id, BOM.is_active == True).first() is not None
            component_info = ComponentPartInfo(
                id=component.id,
                part_number=component.part_number or "",
                name=component.name or "",
                revision=component.revision or "A",
                part_type=component.part_type.value if component.part_type else "manufactured",
                has_bom=has_bom
            )
        
        return BOMItemResponse(
            id=item.id,
            bom_id=item.bom_id,
            component_part_id=item.component_part_id,
            item_number=item.item_number,
            quantity=item.quantity,
            item_type=item.item_type,
            line_type=item.line_type,
            unit_of_measure=item.unit_of_measure or "each",
            reference_designator=item.reference_designator,
            find_number=item.find_number,
            notes=item.notes,
            torque_spec=item.torque_spec,
            installation_notes=item.installation_notes,
            work_center_id=item.work_center_id,
            operation_sequence=item.operation_sequence or 10,
            scrap_factor=item.scrap_factor or 0.0,
            lead_time_offset=item.lead_time_offset or 0,
            is_optional=item.is_optional or False,
            is_alternate=item.is_alternate or False,
            alternate_group=item.alternate_group,
            component_part=component_info,
            created_at=item.created_at,
            updated_at=item.updated_at
        )
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        error_detail = f"Error adding BOM item: {str(e)}\n{traceback.format_exc()}"
        raise HTTPException(status_code=500, detail=error_detail)


@router.put("/items/{item_id}", response_model=BOMItemResponse)
def update_bom_item(
    item_id: int,
    item_in: BOMItemUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]))
):
    """Update a BOM item"""
    item = db.query(BOMItem).options(joinedload(BOMItem.component_part)).filter(BOMItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="BOM item not found")
    
    update_data = item_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(item, field, value)
    
    db.commit()
    db.refresh(item)
    return build_bom_item_response(item, db)


@router.delete("/items/{item_id}")
def delete_bom_item(
    item_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER]))
):
    """Delete a BOM item"""
    item = db.query(BOMItem).filter(BOMItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="BOM item not found")
    
    db.delete(item)
    db.commit()
    return {"message": "BOM item deleted"}


# Multi-level BOM operations
def would_create_circular_reference(db: Session, parent_part_id: int, component_part_id: int, visited: Set[int] = None) -> bool:
    """Check if adding component would create a circular reference"""
    if visited is None:
        visited = set()
    
    if component_part_id in visited:
        return True
    
    if component_part_id == parent_part_id:
        return True
    
    visited.add(component_part_id)
    
    # Get the component's BOM
    component_bom = db.query(BOM).filter(
        BOM.part_id == component_part_id, 
        BOM.is_active == True
    ).first()
    
    if not component_bom:
        return False
    
    # Check each child
    for item in component_bom.items:
        if would_create_circular_reference(db, parent_part_id, item.component_part_id, visited.copy()):
            return True
    
    return False


def explode_bom_recursive(
    db: Session, 
    bom_id: int, 
    parent_qty: float = 1.0, 
    level: int = 0, 
    max_levels: int = 20,
    visited: Set[int] = None
) -> List[BOMItemWithChildren]:
    """Recursively explode a BOM to get all levels"""
    if visited is None:
        visited = set()
    
    if level >= max_levels:
        return []
    
    bom = db.query(BOM).options(
        joinedload(BOM.items).joinedload(BOMItem.component_part)
    ).filter(BOM.id == bom_id).first()
    
    if not bom:
        return []
    
    result = []
    for item in bom.items:
        if item.component_part_id in visited:
            continue  # Skip to prevent infinite loops
        
        # Handle NULL values defensively
        qty = item.quantity or 1.0
        scrap = item.scrap_factor if item.scrap_factor is not None else 0.0
        extended_qty = qty * parent_qty * (1 + scrap)
        
        # Check if component has its own BOM
        component_bom = db.query(BOM).filter(
            BOM.part_id == item.component_part_id,
            BOM.is_active == True
        ).first()
        
        children = []
        item_type = item.item_type or BOMItemType.MAKE
        if component_bom and item_type != BOMItemType.BUY:
            new_visited = visited.copy()
            new_visited.add(item.component_part_id)
            children = explode_bom_recursive(
                db, 
                component_bom.id, 
                extended_qty, 
                level + 1, 
                max_levels,
                new_visited
            )
        
        item_response = BOMItemWithChildren(
            id=item.id,
            bom_id=item.bom_id,
            component_part_id=item.component_part_id,
            item_number=item.item_number,
            quantity=qty,
            item_type=item_type,
            line_type=item.line_type if item.line_type else BOMLineType.COMPONENT,
            unit_of_measure=item.unit_of_measure or "each",
            reference_designator=item.reference_designator,
            find_number=item.find_number,
            notes=item.notes,
            torque_spec=item.torque_spec,
            installation_notes=item.installation_notes,
            work_center_id=item.work_center_id,
            operation_sequence=item.operation_sequence if item.operation_sequence is not None else 10,
            scrap_factor=scrap,
            lead_time_offset=item.lead_time_offset if item.lead_time_offset is not None else 0,
            is_optional=item.is_optional or False,
            is_alternate=item.is_alternate or False,
            alternate_group=item.alternate_group,
            component_part=get_component_part_info(item.component_part, db) if item.component_part else None,
            created_at=item.created_at,
            updated_at=item.updated_at,
            children=children,
            level=level,
            extended_quantity=extended_qty
        )
        result.append(item_response)
    
    return result


def get_max_level(items: List[BOMItemWithChildren], current_max: int = 0) -> int:
    """Get the maximum nesting level in exploded BOM"""
    for item in items:
        current_max = max(current_max, item.level)
        if item.children:
            current_max = get_max_level(item.children, current_max)
    return current_max


@router.get("/{bom_id}/explode", response_model=BOMExploded)
def explode_bom(
    bom_id: int,
    max_levels: int = Query(default=10, le=20, description="Maximum levels to explode"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Explode a BOM to show all levels (multi-level BOM)"""
    bom = db.query(BOM).options(joinedload(BOM.part)).filter(BOM.id == bom_id).first()
    if not bom:
        raise HTTPException(status_code=404, detail="BOM not found")
    
    items = explode_bom_recursive(db, bom_id, 1.0, 0, max_levels)
    total_levels = get_max_level(items) + 1 if items else 0
    
    return BOMExploded(
        bom_id=bom.id,
        part_id=bom.part_id,
        part_number=bom.part.part_number,
        part_name=bom.part.name,
        revision=bom.revision,
        total_levels=total_levels,
        items=items
    )


def flatten_bom_items(
    items: List[BOMItemWithChildren], 
    flat_list: List[BOMFlatItem],
    parent_qty: float = 1.0
):
    """Flatten nested BOM items into a single list"""
    for item in items:
        flat_item = BOMFlatItem(
            level=item.level,
            item_number=item.item_number,
            find_number=item.find_number,
            part_id=item.component_part_id,
            part_number=item.component_part.part_number if item.component_part else "",
            part_name=item.component_part.name if item.component_part else "",
            part_type=item.component_part.part_type.value if item.component_part else "",
            item_type=item.item_type,
            line_type=item.line_type if item.line_type else BOMLineType.COMPONENT,
            quantity_per=item.quantity,
            extended_quantity=item.extended_quantity,
            unit_of_measure=item.unit_of_measure,
            scrap_factor=item.scrap_factor,
            lead_time_offset=item.lead_time_offset,
            is_optional=item.is_optional,
            is_alternate=item.is_alternate,
            has_children=len(item.children) > 0,
            torque_spec=item.torque_spec,
            installation_notes=item.installation_notes
        )
        flat_list.append(flat_item)
        
        if item.children:
            flatten_bom_items(item.children, flat_list, item.extended_quantity)


@router.get("/{bom_id}/flatten", response_model=BOMFlattened)
def flatten_bom(
    bom_id: int,
    max_levels: int = Query(default=10, le=20),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get a flattened view of a multi-level BOM (for reports/MRP)"""
    bom = db.query(BOM).options(joinedload(BOM.part)).filter(BOM.id == bom_id).first()
    if not bom:
        raise HTTPException(status_code=404, detail="BOM not found")
    
    exploded = explode_bom_recursive(db, bom_id, 1.0, 0, max_levels)
    
    flat_items: List[BOMFlatItem] = []
    flatten_bom_items(exploded, flat_items)
    
    unique_parts = set(item.part_id for item in flat_items)
    
    return BOMFlattened(
        bom_id=bom.id,
        part_number=bom.part.part_number,
        part_name=bom.part.name,
        revision=bom.revision,
        total_items=len(flat_items),
        total_unique_parts=len(unique_parts),
        items=flat_items
    )


@router.get("/{bom_id}/where-used")
def where_used(
    bom_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Find all parent assemblies that use this BOM's part"""
    bom = db.query(BOM).options(joinedload(BOM.part)).filter(BOM.id == bom_id).first()
    if not bom:
        raise HTTPException(status_code=404, detail="BOM not found")
    
    # Find all BOM items that reference this part
    usages = db.query(BOMItem).options(
        joinedload(BOMItem.bom).joinedload(BOM.part)
    ).filter(BOMItem.component_part_id == bom.part_id).all()
    
    result = []
    for usage in usages:
        if usage.bom and usage.bom.part:
            result.append({
                "parent_part_id": usage.bom.part_id,
                "parent_part_number": usage.bom.part.part_number,
                "parent_part_name": usage.bom.part.name,
                "bom_id": usage.bom_id,
                "quantity_used": usage.quantity,
                "item_type": usage.item_type.value
            })
    
    return {
        "part_id": bom.part_id,
        "part_number": bom.part.part_number,
        "used_in": result
    }
