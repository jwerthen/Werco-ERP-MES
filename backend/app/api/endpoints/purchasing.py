from typing import List, Optional
from datetime import datetime, date
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func
from app.db.database import get_db
from app.api.deps import get_current_user, require_role
from app.models.user import User, UserRole
from app.models.purchasing import Vendor, PurchaseOrder, PurchaseOrderLine, POStatus, POReceipt, ReceiptStatus
from app.models.part import Part
from app.models.inventory import InventoryItem, InventoryTransaction, TransactionType, InventoryLocation
from app.schemas.purchasing import (
    VendorCreate, VendorUpdate, VendorResponse,
    POCreate, POUpdate, POResponse, POListResponse,
    POLineCreate, POLineResponse,
    ReceiptCreate, ReceiptInspection, ReceiptResponse
)

router = APIRouter()


# ============ VENDORS ============

@router.get("/vendors", response_model=List[VendorResponse])
def list_vendors(
    active_only: bool = True,
    approved_only: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    query = db.query(Vendor)
    if active_only:
        query = query.filter(Vendor.is_active == True)
    if approved_only:
        query = query.filter(Vendor.is_approved == True)
    return query.order_by(Vendor.name).all()


@router.post("/vendors", response_model=VendorResponse)
def create_vendor(
    vendor_in: VendorCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER]))
):
    existing = db.query(Vendor).filter(Vendor.code == vendor_in.code).first()
    if existing:
        raise HTTPException(status_code=400, detail="Vendor code already exists")
    
    vendor = Vendor(**vendor_in.model_dump())
    if vendor.is_approved:
        vendor.approval_date = date.today()
    db.add(vendor)
    db.commit()
    db.refresh(vendor)
    return vendor


@router.get("/vendors/{vendor_id}", response_model=VendorResponse)
def get_vendor(
    vendor_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    vendor = db.query(Vendor).filter(Vendor.id == vendor_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")
    return vendor


@router.put("/vendors/{vendor_id}", response_model=VendorResponse)
def update_vendor(
    vendor_id: int,
    vendor_in: VendorUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER]))
):
    vendor = db.query(Vendor).filter(Vendor.id == vendor_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")
    
    update_data = vendor_in.model_dump(exclude_unset=True)
    
    # Set approval date if being approved
    if update_data.get("is_approved") and not vendor.is_approved:
        vendor.approval_date = date.today()
    
    for field, value in update_data.items():
        setattr(vendor, field, value)
    
    db.commit()
    db.refresh(vendor)
    return vendor


# ============ PURCHASE ORDERS ============

def generate_po_number(db: Session) -> str:
    today = datetime.now().strftime("%Y%m%d")
    prefix = f"PO-{today}-"
    
    last_po = db.query(PurchaseOrder).filter(
        PurchaseOrder.po_number.like(f"{prefix}%")
    ).order_by(PurchaseOrder.po_number.desc()).first()
    
    if last_po:
        last_num = int(last_po.po_number.split("-")[-1])
        new_num = last_num + 1
    else:
        new_num = 1
    
    return f"{prefix}{new_num:03d}"


@router.get("/purchase-orders", response_model=List[POListResponse])
def list_purchase_orders(
    status: Optional[str] = None,
    vendor_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    query = db.query(PurchaseOrder).options(joinedload(PurchaseOrder.vendor))
    
    if status:
        query = query.filter(PurchaseOrder.status == status)
    else:
        # Default: exclude closed/cancelled
        query = query.filter(PurchaseOrder.status.not_in([POStatus.CLOSED, POStatus.CANCELLED]))
    
    if vendor_id:
        query = query.filter(PurchaseOrder.vendor_id == vendor_id)
    
    pos = query.order_by(PurchaseOrder.created_at.desc()).all()
    
    result = []
    for po in pos:
        result.append(POListResponse(
            id=po.id,
            po_number=po.po_number,
            vendor_id=po.vendor_id,
            vendor_name=po.vendor.name if po.vendor else None,
            status=po.status.value if hasattr(po.status, 'value') else po.status,
            order_date=po.order_date,
            required_date=po.required_date,
            total=po.total,
            line_count=len(po.lines),
            created_at=po.created_at
        ))
    return result


@router.post("/purchase-orders", response_model=POResponse)
def create_purchase_order(
    po_in: POCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]))
):
    # Verify vendor
    vendor = db.query(Vendor).filter(Vendor.id == po_in.vendor_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")
    
    po_number = generate_po_number(db)
    
    po = PurchaseOrder(
        po_number=po_number,
        vendor_id=po_in.vendor_id,
        required_date=po_in.required_date,
        expected_date=po_in.expected_date,
        ship_to=po_in.ship_to,
        shipping_method=po_in.shipping_method,
        notes=po_in.notes,
        created_by=current_user.id
    )
    db.add(po)
    db.flush()
    
    # Add lines
    subtotal = 0.0
    for idx, line_data in enumerate(po_in.lines, 1):
        part = db.query(Part).filter(Part.id == line_data.part_id).first()
        if not part:
            raise HTTPException(status_code=404, detail=f"Part {line_data.part_id} not found")
        
        line_total = line_data.quantity_ordered * line_data.unit_price
        line = PurchaseOrderLine(
            purchase_order_id=po.id,
            line_number=idx,
            part_id=line_data.part_id,
            quantity_ordered=line_data.quantity_ordered,
            unit_price=line_data.unit_price,
            line_total=line_total,
            required_date=line_data.required_date or po_in.required_date,
            notes=line_data.notes
        )
        db.add(line)
        subtotal += line_total
    
    po.subtotal = subtotal
    po.total = subtotal + po.tax + po.shipping
    
    db.commit()
    db.refresh(po)
    return po


@router.get("/purchase-orders/{po_id}", response_model=POResponse)
def get_purchase_order(
    po_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    po = db.query(PurchaseOrder).options(
        joinedload(PurchaseOrder.vendor),
        joinedload(PurchaseOrder.lines).joinedload(PurchaseOrderLine.part)
    ).filter(PurchaseOrder.id == po_id).first()
    
    if not po:
        raise HTTPException(status_code=404, detail="Purchase order not found")
    return po


@router.put("/purchase-orders/{po_id}", response_model=POResponse)
def update_purchase_order(
    po_id: int,
    po_in: POUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]))
):
    po = db.query(PurchaseOrder).filter(PurchaseOrder.id == po_id).first()
    if not po:
        raise HTTPException(status_code=404, detail="Purchase order not found")
    
    update_data = po_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        if field == "status":
            setattr(po, field, POStatus(value))
        else:
            setattr(po, field, value)
    
    db.commit()
    db.refresh(po)
    return po


@router.post("/purchase-orders/{po_id}/send")
def send_purchase_order(
    po_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER]))
):
    po = db.query(PurchaseOrder).filter(PurchaseOrder.id == po_id).first()
    if not po:
        raise HTTPException(status_code=404, detail="Purchase order not found")
    
    if po.status not in [POStatus.DRAFT, POStatus.APPROVED]:
        raise HTTPException(status_code=400, detail="Can only send draft or approved POs")
    
    po.status = POStatus.SENT
    po.order_date = date.today()
    db.commit()
    
    return {"message": "PO sent", "po_number": po.po_number}


@router.post("/purchase-orders/{po_id}/lines", response_model=POLineResponse)
def add_po_line(
    po_id: int,
    line_in: POLineCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]))
):
    po = db.query(PurchaseOrder).filter(PurchaseOrder.id == po_id).first()
    if not po:
        raise HTTPException(status_code=404, detail="Purchase order not found")
    
    if po.status not in [POStatus.DRAFT]:
        raise HTTPException(status_code=400, detail="Can only add lines to draft POs")
    
    part = db.query(Part).filter(Part.id == line_in.part_id).first()
    if not part:
        raise HTTPException(status_code=404, detail="Part not found")
    
    # Get next line number
    max_line = db.query(func.max(PurchaseOrderLine.line_number)).filter(
        PurchaseOrderLine.purchase_order_id == po_id
    ).scalar() or 0
    
    line_total = line_in.quantity_ordered * line_in.unit_price
    line = PurchaseOrderLine(
        purchase_order_id=po_id,
        line_number=max_line + 1,
        part_id=line_in.part_id,
        quantity_ordered=line_in.quantity_ordered,
        unit_price=line_in.unit_price,
        line_total=line_total,
        required_date=line_in.required_date,
        notes=line_in.notes
    )
    db.add(line)
    
    # Update PO totals
    po.subtotal += line_total
    po.total = po.subtotal + po.tax + po.shipping
    
    db.commit()
    db.refresh(line)
    return line


# ============ RECEIVING ============

def generate_receipt_number(db: Session) -> str:
    today = datetime.now().strftime("%Y%m%d")
    prefix = f"RCV-{today}-"
    
    last = db.query(POReceipt).filter(
        POReceipt.receipt_number.like(f"{prefix}%")
    ).order_by(POReceipt.receipt_number.desc()).first()
    
    if last:
        last_num = int(last.receipt_number.split("-")[-1])
        new_num = last_num + 1
    else:
        new_num = 1
    
    return f"{prefix}{new_num:03d}"


@router.get("/receiving/queue")
def get_receiving_queue(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get PO lines awaiting receipt"""
    lines = db.query(PurchaseOrderLine).options(
        joinedload(PurchaseOrderLine.purchase_order).joinedload(PurchaseOrder.vendor),
        joinedload(PurchaseOrderLine.part)
    ).join(PurchaseOrder).filter(
        PurchaseOrder.status.in_([POStatus.SENT, POStatus.PARTIAL]),
        PurchaseOrderLine.is_closed == False,
        PurchaseOrderLine.quantity_received < PurchaseOrderLine.quantity_ordered
    ).order_by(PurchaseOrderLine.required_date).all()
    
    result = []
    for line in lines:
        result.append({
            "po_line_id": line.id,
            "po_number": line.purchase_order.po_number,
            "po_id": line.purchase_order.id,
            "vendor_name": line.purchase_order.vendor.name if line.purchase_order.vendor else None,
            "part_number": line.part.part_number if line.part else None,
            "part_name": line.part.name if line.part else None,
            "quantity_ordered": line.quantity_ordered,
            "quantity_received": line.quantity_received,
            "quantity_remaining": line.quantity_ordered - line.quantity_received,
            "required_date": line.required_date,
            "line_number": line.line_number
        })
    return result


@router.post("/receiving", response_model=ReceiptResponse)
def receive_material(
    receipt_in: ReceiptCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Receive material against a PO line"""
    po_line = db.query(PurchaseOrderLine).options(
        joinedload(PurchaseOrderLine.purchase_order)
    ).filter(PurchaseOrderLine.id == receipt_in.po_line_id).first()
    
    if not po_line:
        raise HTTPException(status_code=404, detail="PO line not found")
    
    po = po_line.purchase_order
    if po.status not in [POStatus.SENT, POStatus.PARTIAL]:
        raise HTTPException(status_code=400, detail="PO must be in sent or partial status to receive")
    
    # Validate location if provided
    location = None
    if receipt_in.location_id:
        location = db.query(InventoryLocation).filter(InventoryLocation.id == receipt_in.location_id).first()
        if not location:
            raise HTTPException(status_code=404, detail="Location not found")
    
    receipt_number = generate_receipt_number(db)
    
    receipt = POReceipt(
        receipt_number=receipt_number,
        po_line_id=po_line.id,
        quantity_received=receipt_in.quantity_received,
        lot_number=receipt_in.lot_number,
        serial_numbers=receipt_in.serial_numbers,
        heat_number=receipt_in.heat_number,
        cert_number=receipt_in.cert_number,
        location_id=receipt_in.location_id,
        requires_inspection=receipt_in.requires_inspection,
        status=ReceiptStatus.PENDING_INSPECTION if receipt_in.requires_inspection else ReceiptStatus.ACCEPTED,
        packing_slip_number=receipt_in.packing_slip_number,
        carrier=receipt_in.carrier,
        tracking_number=receipt_in.tracking_number,
        received_by=current_user.id,
        notes=receipt_in.notes
    )
    db.add(receipt)
    
    # Update PO line quantity received
    po_line.quantity_received += receipt_in.quantity_received
    if po_line.quantity_received >= po_line.quantity_ordered:
        po_line.is_closed = True
    
    # Update PO status
    all_lines = db.query(PurchaseOrderLine).filter(PurchaseOrderLine.purchase_order_id == po.id).all()
    all_closed = all(l.is_closed for l in all_lines)
    any_received = any(l.quantity_received > 0 for l in all_lines)
    
    if all_closed:
        po.status = POStatus.RECEIVED
    elif any_received:
        po.status = POStatus.PARTIAL
    
    # If not requiring inspection, auto-accept and add to inventory
    if not receipt_in.requires_inspection:
        receipt.quantity_accepted = receipt_in.quantity_received
        receipt.status = ReceiptStatus.ACCEPTED
        
        # Add to inventory
        _add_to_inventory(
            db, po_line.part_id, receipt_in.quantity_received,
            location.code if location else "RECV-01",
            receipt_in.lot_number, current_user.id, receipt_number
        )
    
    db.commit()
    db.refresh(receipt)
    return receipt


@router.get("/receiving/pending-inspection")
def get_pending_inspection(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get receipts pending inspection"""
    receipts = db.query(POReceipt).options(
        joinedload(POReceipt.po_line).joinedload(PurchaseOrderLine.part),
        joinedload(POReceipt.po_line).joinedload(PurchaseOrderLine.purchase_order).joinedload(PurchaseOrder.vendor),
        joinedload(POReceipt.location)
    ).filter(
        POReceipt.status == ReceiptStatus.PENDING_INSPECTION
    ).order_by(POReceipt.received_at).all()
    
    result = []
    for r in receipts:
        result.append({
            "receipt_id": r.id,
            "receipt_number": r.receipt_number,
            "po_number": r.po_line.purchase_order.po_number,
            "vendor_name": r.po_line.purchase_order.vendor.name if r.po_line.purchase_order.vendor else None,
            "part_number": r.po_line.part.part_number if r.po_line.part else None,
            "part_name": r.po_line.part.name if r.po_line.part else None,
            "quantity_received": r.quantity_received,
            "lot_number": r.lot_number,
            "cert_number": r.cert_number,
            "received_at": r.received_at,
            "location": r.location.code if r.location else None
        })
    return result


@router.post("/receiving/{receipt_id}/inspect", response_model=ReceiptResponse)
def inspect_receipt(
    receipt_id: int,
    inspection: ReceiptInspection,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY]))
):
    """Complete inspection of a receipt"""
    receipt = db.query(POReceipt).options(
        joinedload(POReceipt.po_line),
        joinedload(POReceipt.location)
    ).filter(POReceipt.id == receipt_id).first()
    
    if not receipt:
        raise HTTPException(status_code=404, detail="Receipt not found")
    
    if receipt.status != ReceiptStatus.PENDING_INSPECTION:
        raise HTTPException(status_code=400, detail="Receipt is not pending inspection")
    
    # Validate quantities
    if inspection.quantity_accepted + inspection.quantity_rejected > receipt.quantity_received:
        raise HTTPException(status_code=400, detail="Accepted + rejected cannot exceed received quantity")
    
    receipt.quantity_accepted = inspection.quantity_accepted
    receipt.quantity_rejected = inspection.quantity_rejected
    receipt.status = ReceiptStatus(inspection.status)
    receipt.inspected_by = current_user.id
    receipt.inspected_at = datetime.utcnow()
    receipt.inspection_notes = inspection.inspection_notes
    
    # If accepted, add to inventory
    if inspection.quantity_accepted > 0 and inspection.status == "accepted":
        location_code = receipt.location.code if receipt.location else "RECV-01"
        _add_to_inventory(
            db, receipt.po_line.part_id, inspection.quantity_accepted,
            location_code, receipt.lot_number, current_user.id, receipt.receipt_number
        )
    
    db.commit()
    db.refresh(receipt)
    return receipt


def _add_to_inventory(db: Session, part_id: int, quantity: float, location: str, lot_number: str, user_id: int, reference: str):
    """Helper to add received material to inventory"""
    # Check for existing inventory at location with same lot
    existing = db.query(InventoryItem).filter(
        InventoryItem.part_id == part_id,
        InventoryItem.location == location,
        InventoryItem.lot_number == lot_number
    ).first()
    
    if existing:
        existing.quantity_on_hand += quantity
    else:
        inv_item = InventoryItem(
            part_id=part_id,
            location=location,
            lot_number=lot_number,
            quantity_on_hand=quantity,
            quantity_allocated=0,
            is_active=True
        )
        db.add(inv_item)
    
    # Create transaction record
    txn = InventoryTransaction(
        part_id=part_id,
        transaction_type=TransactionType.RECEIPT,
        quantity=quantity,
        location_to=location,
        lot_number=lot_number,
        reference_type="po_receipt",
        reference_id=reference,
        performed_by=user_id,
        notes=f"Received from {reference}"
    )
    db.add(txn)


@router.get("/receiving/history")
def get_receiving_history(
    days: int = 30,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get recent receiving history"""
    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(days=days)
    
    receipts = db.query(POReceipt).options(
        joinedload(POReceipt.po_line).joinedload(PurchaseOrderLine.part),
        joinedload(POReceipt.po_line).joinedload(PurchaseOrderLine.purchase_order).joinedload(PurchaseOrder.vendor)
    ).filter(
        POReceipt.received_at >= cutoff
    ).order_by(POReceipt.received_at.desc()).limit(100).all()
    
    result = []
    for r in receipts:
        result.append({
            "receipt_id": r.id,
            "receipt_number": r.receipt_number,
            "po_number": r.po_line.purchase_order.po_number,
            "vendor_name": r.po_line.purchase_order.vendor.name if r.po_line.purchase_order.vendor else None,
            "part_number": r.po_line.part.part_number if r.po_line.part else None,
            "part_name": r.po_line.part.name if r.po_line.part else None,
            "quantity_received": r.quantity_received,
            "quantity_accepted": r.quantity_accepted,
            "quantity_rejected": r.quantity_rejected,
            "status": r.status.value if hasattr(r.status, 'value') else r.status,
            "lot_number": r.lot_number,
            "received_at": r.received_at
        })
    return result
