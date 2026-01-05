from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import or_
from app.db.database import get_db
from app.api.deps import get_current_user
from app.models.user import User
from app.models.part import Part
from app.models.work_order import WorkOrder
from app.models.supplier_part import SupplierPartMapping
from app.models.purchasing import Vendor
from app.models.inventory import InventoryLocation
from pydantic import BaseModel
from datetime import datetime

router = APIRouter()


class SupplierMappingCreate(BaseModel):
    supplier_part_number: str
    part_id: int
    vendor_id: Optional[int] = None
    supplier_description: Optional[str] = None
    supplier_uom: Optional[str] = None
    conversion_factor: float = 1.0
    default_location_id: Optional[int] = None
    notes: Optional[str] = None


class SupplierMappingResponse(BaseModel):
    id: int
    supplier_part_number: str
    part_id: int
    part_number: str
    part_name: str
    part_description: Optional[str] = None
    vendor_id: Optional[int] = None
    vendor_name: Optional[str] = None
    supplier_description: Optional[str] = None
    supplier_uom: Optional[str] = None
    conversion_factor: float
    is_active: bool
    
    class Config:
        from_attributes = True


class ScanLookupResponse(BaseModel):
    found: bool
    match_type: Optional[str] = None  # 'supplier_mapping', 'part_number', 'work_order'
    
    # Part info (if found)
    part_id: Optional[int] = None
    part_number: Optional[str] = None
    part_name: Optional[str] = None
    part_description: Optional[str] = None
    part_type: Optional[str] = None
    unit_of_measure: Optional[str] = None
    
    # Supplier mapping info
    supplier_part_number: Optional[str] = None
    vendor_name: Optional[str] = None
    supplier_description: Optional[str] = None
    
    # Work order info (if WO scan)
    work_order_id: Optional[int] = None
    work_order_number: Optional[str] = None
    work_order_status: Optional[str] = None
    quantity_ordered: Optional[float] = None
    customer_name: Optional[str] = None
    
    # For manual entry
    scanned_code: str


@router.post("/lookup", response_model=ScanLookupResponse)
def lookup_barcode(
    code: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Look up a scanned barcode/code.
    Searches in order:
    1. Supplier part number mappings
    2. Internal part numbers
    3. Work order numbers
    """
    code = code.strip()
    code_upper = code.upper()
    
    # 1. Check supplier part mappings first
    mapping = db.query(SupplierPartMapping).options(
        joinedload(SupplierPartMapping.part),
        joinedload(SupplierPartMapping.vendor)
    ).filter(
        SupplierPartMapping.supplier_part_number == code,
        SupplierPartMapping.is_active == True
    ).first()
    
    if not mapping:
        # Try case-insensitive
        mapping = db.query(SupplierPartMapping).options(
            joinedload(SupplierPartMapping.part),
            joinedload(SupplierPartMapping.vendor)
        ).filter(
            SupplierPartMapping.supplier_part_number.ilike(code),
            SupplierPartMapping.is_active == True
        ).first()
    
    if mapping:
        return ScanLookupResponse(
            found=True,
            match_type='supplier_mapping',
            part_id=mapping.part.id,
            part_number=mapping.part.part_number,
            part_name=mapping.part.name,
            part_description=mapping.part.description,
            part_type=mapping.part.part_type.value if mapping.part.part_type else None,
            unit_of_measure=mapping.part.unit_of_measure.value if mapping.part.unit_of_measure else None,
            supplier_part_number=mapping.supplier_part_number,
            vendor_name=mapping.vendor.name if mapping.vendor else None,
            supplier_description=mapping.supplier_description,
            scanned_code=code
        )
    
    # 2. Check internal part numbers
    part = db.query(Part).filter(
        or_(
            Part.part_number == code,
            Part.part_number.ilike(code)
        ),
        Part.is_active == True
    ).first()
    
    if part:
        return ScanLookupResponse(
            found=True,
            match_type='part_number',
            part_id=part.id,
            part_number=part.part_number,
            part_name=part.name,
            part_description=part.description,
            part_type=part.part_type.value if part.part_type else None,
            unit_of_measure=part.unit_of_measure.value if part.unit_of_measure else None,
            scanned_code=code
        )
    
    # 3. Check work order numbers
    if code_upper.startswith('WO') or code_upper.startswith('W0'):
        wo = db.query(WorkOrder).options(
            joinedload(WorkOrder.part)
        ).filter(
            WorkOrder.work_order_number.ilike(f"%{code}%")
        ).first()
        
        if wo:
            return ScanLookupResponse(
                found=True,
                match_type='work_order',
                work_order_id=wo.id,
                work_order_number=wo.work_order_number,
                work_order_status=wo.status.value if wo.status else None,
                quantity_ordered=wo.quantity_ordered,
                customer_name=wo.customer_name,
                part_id=wo.part.id if wo.part else None,
                part_number=wo.part.part_number if wo.part else None,
                part_name=wo.part.name if wo.part else None,
                scanned_code=code
            )
    
    # Not found - return for manual entry
    return ScanLookupResponse(
        found=False,
        scanned_code=code
    )


@router.get("/mappings", response_model=List[SupplierMappingResponse])
def list_supplier_mappings(
    search: Optional[str] = None,
    part_id: Optional[int] = None,
    vendor_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List supplier part mappings"""
    query = db.query(SupplierPartMapping).options(
        joinedload(SupplierPartMapping.part),
        joinedload(SupplierPartMapping.vendor)
    ).filter(SupplierPartMapping.is_active == True)
    
    if search:
        query = query.filter(
            or_(
                SupplierPartMapping.supplier_part_number.ilike(f"%{search}%"),
                SupplierPartMapping.supplier_description.ilike(f"%{search}%")
            )
        )
    
    if part_id:
        query = query.filter(SupplierPartMapping.part_id == part_id)
    
    if vendor_id:
        query = query.filter(SupplierPartMapping.vendor_id == vendor_id)
    
    mappings = query.order_by(SupplierPartMapping.supplier_part_number).limit(200).all()
    
    return [
        SupplierMappingResponse(
            id=m.id,
            supplier_part_number=m.supplier_part_number,
            part_id=m.part_id,
            part_number=m.part.part_number,
            part_name=m.part.name,
            part_description=m.part.description,
            vendor_id=m.vendor_id,
            vendor_name=m.vendor.name if m.vendor else None,
            supplier_description=m.supplier_description,
            supplier_uom=m.supplier_uom,
            conversion_factor=m.conversion_factor,
            is_active=m.is_active
        )
        for m in mappings
    ]


@router.post("/mappings", response_model=SupplierMappingResponse)
def create_supplier_mapping(
    mapping_in: SupplierMappingCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a new supplier part mapping"""
    # Verify part exists
    part = db.query(Part).filter(Part.id == mapping_in.part_id).first()
    if not part:
        raise HTTPException(status_code=404, detail="Part not found")
    
    # Check if mapping already exists
    existing = db.query(SupplierPartMapping).filter(
        SupplierPartMapping.supplier_part_number == mapping_in.supplier_part_number,
        SupplierPartMapping.vendor_id == mapping_in.vendor_id,
        SupplierPartMapping.is_active == True
    ).first()
    
    if existing:
        raise HTTPException(status_code=400, detail="Mapping already exists for this supplier part number")
    
    vendor = None
    if mapping_in.vendor_id:
        vendor = db.query(Vendor).filter(Vendor.id == mapping_in.vendor_id).first()
    
    mapping = SupplierPartMapping(
        supplier_part_number=mapping_in.supplier_part_number,
        part_id=mapping_in.part_id,
        vendor_id=mapping_in.vendor_id,
        supplier_description=mapping_in.supplier_description,
        supplier_uom=mapping_in.supplier_uom,
        conversion_factor=mapping_in.conversion_factor,
        default_location_id=mapping_in.default_location_id,
        notes=mapping_in.notes
    )
    
    db.add(mapping)
    db.commit()
    db.refresh(mapping)
    
    return SupplierMappingResponse(
        id=mapping.id,
        supplier_part_number=mapping.supplier_part_number,
        part_id=mapping.part_id,
        part_number=part.part_number,
        part_name=part.name,
        part_description=part.description,
        vendor_id=mapping.vendor_id,
        vendor_name=vendor.name if vendor else None,
        supplier_description=mapping.supplier_description,
        supplier_uom=mapping.supplier_uom,
        conversion_factor=mapping.conversion_factor,
        is_active=mapping.is_active
    )


@router.delete("/mappings/{mapping_id}")
def delete_supplier_mapping(
    mapping_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Deactivate a supplier mapping"""
    mapping = db.query(SupplierPartMapping).filter(SupplierPartMapping.id == mapping_id).first()
    if not mapping:
        raise HTTPException(status_code=404, detail="Mapping not found")
    
    mapping.is_active = False
    db.commit()
    
    return {"message": "Mapping deleted"}
