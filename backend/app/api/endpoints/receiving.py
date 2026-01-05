from typing import List, Optional
from datetime import datetime, date, timedelta
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func
from app.db.database import get_db
from app.api.deps import get_current_user, require_role
from app.models.user import User, UserRole
from app.models.purchasing import (
    Vendor, PurchaseOrder, PurchaseOrderLine, POStatus, POReceipt, 
    ReceiptStatus, InspectionStatus, DefectType, InspectionMethod
)
from app.models.part import Part
from app.models.inventory import InventoryItem, InventoryTransaction, TransactionType, InventoryLocation
from app.models.quality import NonConformanceReport, NCRStatus, NCRDisposition, NCRSource
from app.models.audit_log import AuditLog
from app.schemas.purchasing import (
    ReceiptCreate, ReceiptInspection, ReceiptResponse,
    InspectionQueueItem, InspectionResultResponse
)

router = APIRouter()


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


def generate_ncr_number(db: Session) -> str:
    today = datetime.now().strftime("%Y%m%d")
    prefix = f"NCR-{today}-"
    
    last = db.query(NonConformanceReport).filter(
        NonConformanceReport.ncr_number.like(f"{prefix}%")
    ).order_by(NonConformanceReport.ncr_number.desc()).first()
    
    if last:
        last_num = int(last.ncr_number.split("-")[-1])
        new_num = last_num + 1
    else:
        new_num = 1
    
    return f"{prefix}{new_num:03d}"


def log_audit(db: Session, user_id: int, action: str, resource_type: str, resource_id: int, details: str):
    """Create audit log entry for compliance"""
    audit = AuditLog(
        user_id=user_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        details=details,
        ip_address="system"
    )
    db.add(audit)


@router.get("/open-pos")
def get_open_purchase_orders(
    vendor_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get POs available for receiving (sent or partial status)"""
    query = db.query(PurchaseOrder).options(
        joinedload(PurchaseOrder.vendor),
        joinedload(PurchaseOrder.lines).joinedload(PurchaseOrderLine.part)
    ).filter(
        PurchaseOrder.status.in_([POStatus.SENT, POStatus.PARTIAL])
    )
    
    if vendor_id:
        query = query.filter(PurchaseOrder.vendor_id == vendor_id)
    
    pos = query.order_by(PurchaseOrder.required_date, PurchaseOrder.po_number).all()
    
    result = []
    for po in pos:
        lines_data = []
        for line in po.lines:
            remaining = line.quantity_ordered - line.quantity_received
            if remaining > 0 and not line.is_closed:
                lines_data.append({
                    "line_id": line.id,
                    "line_number": line.line_number,
                    "part_id": line.part_id,
                    "part_number": line.part.part_number if line.part else None,
                    "part_name": line.part.name if line.part else None,
                    "quantity_ordered": line.quantity_ordered,
                    "quantity_received": line.quantity_received,
                    "quantity_remaining": remaining,
                    "unit_price": line.unit_price,
                    "required_date": line.required_date
                })
        
        if lines_data:
            result.append({
                "po_id": po.id,
                "po_number": po.po_number,
                "vendor_id": po.vendor_id,
                "vendor_name": po.vendor.name if po.vendor else None,
                "vendor_code": po.vendor.code if po.vendor else None,
                "order_date": po.order_date,
                "required_date": po.required_date,
                "status": po.status.value,
                "lines": lines_data,
                "total_lines": len(lines_data)
            })
    
    return result


@router.get("/po/{po_id}")
def get_purchase_order_for_receiving(
    po_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get full PO details for receiving"""
    po = db.query(PurchaseOrder).options(
        joinedload(PurchaseOrder.vendor),
        joinedload(PurchaseOrder.lines).joinedload(PurchaseOrderLine.part),
        joinedload(PurchaseOrder.lines).joinedload(PurchaseOrderLine.receipts)
    ).filter(PurchaseOrder.id == po_id).first()
    
    if not po:
        raise HTTPException(status_code=404, detail="Purchase order not found")
    
    lines_data = []
    for line in po.lines:
        receipts_data = []
        for r in line.receipts:
            receipts_data.append({
                "receipt_id": r.id,
                "receipt_number": r.receipt_number,
                "quantity_received": r.quantity_received,
                "lot_number": r.lot_number,
                "status": r.status.value if hasattr(r.status, 'value') else r.status,
                "received_at": r.received_at
            })
        
        lines_data.append({
            "line_id": line.id,
            "line_number": line.line_number,
            "part_id": line.part_id,
            "part_number": line.part.part_number if line.part else None,
            "part_name": line.part.name if line.part else None,
            "quantity_ordered": line.quantity_ordered,
            "quantity_received": line.quantity_received,
            "quantity_remaining": line.quantity_ordered - line.quantity_received,
            "unit_price": line.unit_price,
            "required_date": line.required_date,
            "is_closed": line.is_closed,
            "receipts": receipts_data
        })
    
    return {
        "po_id": po.id,
        "po_number": po.po_number,
        "vendor_id": po.vendor_id,
        "vendor_name": po.vendor.name if po.vendor else None,
        "vendor_code": po.vendor.code if po.vendor else None,
        "is_approved_vendor": po.vendor.is_approved if po.vendor else False,
        "order_date": po.order_date,
        "required_date": po.required_date,
        "expected_date": po.expected_date,
        "status": po.status.value,
        "notes": po.notes,
        "lines": lines_data
    }


@router.post("/receive", response_model=ReceiptResponse)
def receive_material(
    receipt_in: ReceiptCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Receive material against a PO line.
    Creates receipt record, updates PO quantities, places in inspection queue if required.
    """
    po_line = db.query(PurchaseOrderLine).options(
        joinedload(PurchaseOrderLine.purchase_order).joinedload(PurchaseOrder.vendor),
        joinedload(PurchaseOrderLine.part)
    ).filter(PurchaseOrderLine.id == receipt_in.po_line_id).first()
    
    if not po_line:
        raise HTTPException(status_code=404, detail="PO line not found")
    
    po = po_line.purchase_order
    if po.status not in [POStatus.SENT, POStatus.PARTIAL]:
        raise HTTPException(status_code=400, detail="PO must be in sent or partial status to receive")
    
    if po_line.is_closed:
        raise HTTPException(status_code=400, detail="PO line is already closed")
    
    # Lot number is required for AS9100D compliance
    if not receipt_in.lot_number or not receipt_in.lot_number.strip():
        raise HTTPException(status_code=400, detail="Lot number is required for traceability (AS9100D)")
    
    # Check for over-receiving
    remaining = po_line.quantity_ordered - po_line.quantity_received
    if receipt_in.quantity_received > remaining:
        if not receipt_in.over_receive_approved:
            raise HTTPException(
                status_code=400, 
                detail=f"Quantity received ({receipt_in.quantity_received}) exceeds remaining quantity ({remaining}). Set over_receive_approved=true to override."
            )
    
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
        lot_number=receipt_in.lot_number.strip(),
        serial_numbers=receipt_in.serial_numbers,
        heat_number=receipt_in.heat_number,
        cert_number=receipt_in.cert_number,
        coc_attached=receipt_in.coc_attached,
        location_id=receipt_in.location_id,
        requires_inspection=receipt_in.requires_inspection,
        status=ReceiptStatus.PENDING_INSPECTION if receipt_in.requires_inspection else ReceiptStatus.ACCEPTED,
        inspection_status=InspectionStatus.PENDING if receipt_in.requires_inspection else InspectionStatus.PASSED,
        packing_slip_number=receipt_in.packing_slip_number,
        carrier=receipt_in.carrier,
        tracking_number=receipt_in.tracking_number,
        over_receive_approved=receipt_in.over_receive_approved,
        over_receive_approved_by=current_user.id if receipt_in.over_receive_approved else None,
        received_by=current_user.id,
        notes=receipt_in.notes
    )
    db.add(receipt)
    db.flush()
    
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
        receipt.inspection_status = InspectionStatus.PASSED
        receipt.inspection_method = InspectionMethod.VISUAL
        receipt.inspected_by = current_user.id
        receipt.inspected_at = datetime.utcnow()
        
        location_code = location.code if location else "RECV-01"
        _add_to_inventory(
            db, po_line.part_id, receipt_in.quantity_received,
            location_code, receipt_in.lot_number.strip(), 
            po_line.unit_price, current_user.id, receipt_number,
            po.vendor.name if po.vendor else None
        )
    
    # Audit log
    log_audit(
        db, current_user.id, "RECEIVE_MATERIAL", "po_receipt", receipt.id,
        f"Received {receipt_in.quantity_received} of part {po_line.part.part_number if po_line.part else 'N/A'} on PO {po.po_number}, Lot: {receipt_in.lot_number}"
    )
    
    db.commit()
    db.refresh(receipt)
    return receipt


@router.get("/inspection-queue", response_model=List[InspectionQueueItem])
def get_inspection_queue(
    days_back: int = 30,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get items pending inspection, sorted by date received (oldest first)"""
    cutoff = datetime.utcnow() - timedelta(days=days_back)
    
    receipts = db.query(POReceipt).options(
        joinedload(POReceipt.po_line).joinedload(PurchaseOrderLine.part),
        joinedload(POReceipt.po_line).joinedload(PurchaseOrderLine.purchase_order).joinedload(PurchaseOrder.vendor),
        joinedload(POReceipt.location),
        joinedload(POReceipt.receiver)
    ).filter(
        POReceipt.status == ReceiptStatus.PENDING_INSPECTION,
        POReceipt.received_at >= cutoff
    ).order_by(POReceipt.received_at).all()
    
    result = []
    now = datetime.utcnow()
    for r in receipts:
        days_pending = (now - r.received_at).days if r.received_at else 0
        result.append(InspectionQueueItem(
            receipt_id=r.id,
            receipt_number=r.receipt_number,
            po_number=r.po_line.purchase_order.po_number,
            po_id=r.po_line.purchase_order.id,
            vendor_name=r.po_line.purchase_order.vendor.name if r.po_line.purchase_order.vendor else None,
            part_id=r.po_line.part_id,
            part_number=r.po_line.part.part_number if r.po_line.part else None,
            part_name=r.po_line.part.name if r.po_line.part else None,
            quantity_received=r.quantity_received,
            lot_number=r.lot_number,
            cert_number=r.cert_number,
            coc_attached=r.coc_attached,
            received_at=r.received_at,
            received_by_name=r.receiver.full_name if r.receiver else None,
            location_code=r.location.code if r.location else None,
            days_pending=days_pending
        ))
    
    return result


@router.get("/receipt/{receipt_id}")
def get_receipt_detail(
    receipt_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get full receipt details for inspection"""
    receipt = db.query(POReceipt).options(
        joinedload(POReceipt.po_line).joinedload(PurchaseOrderLine.part),
        joinedload(POReceipt.po_line).joinedload(PurchaseOrderLine.purchase_order).joinedload(PurchaseOrder.vendor),
        joinedload(POReceipt.location),
        joinedload(POReceipt.receiver),
        joinedload(POReceipt.inspector)
    ).filter(POReceipt.id == receipt_id).first()
    
    if not receipt:
        raise HTTPException(status_code=404, detail="Receipt not found")
    
    return {
        "receipt_id": receipt.id,
        "receipt_number": receipt.receipt_number,
        "po_number": receipt.po_line.purchase_order.po_number,
        "po_id": receipt.po_line.purchase_order.id,
        "vendor_name": receipt.po_line.purchase_order.vendor.name if receipt.po_line.purchase_order.vendor else None,
        "vendor_code": receipt.po_line.purchase_order.vendor.code if receipt.po_line.purchase_order.vendor else None,
        "is_approved_vendor": receipt.po_line.purchase_order.vendor.is_approved if receipt.po_line.purchase_order.vendor else False,
        "part_id": receipt.po_line.part_id,
        "part_number": receipt.po_line.part.part_number if receipt.po_line.part else None,
        "part_name": receipt.po_line.part.name if receipt.po_line.part else None,
        "part_description": receipt.po_line.part.description if receipt.po_line.part else None,
        "quantity_received": receipt.quantity_received,
        "quantity_accepted": receipt.quantity_accepted,
        "quantity_rejected": receipt.quantity_rejected,
        "lot_number": receipt.lot_number,
        "serial_numbers": receipt.serial_numbers,
        "heat_number": receipt.heat_number,
        "cert_number": receipt.cert_number,
        "coc_attached": receipt.coc_attached,
        "status": receipt.status.value if hasattr(receipt.status, 'value') else receipt.status,
        "inspection_status": receipt.inspection_status.value if hasattr(receipt.inspection_status, 'value') else receipt.inspection_status,
        "inspection_method": receipt.inspection_method.value if receipt.inspection_method else None,
        "defect_type": receipt.defect_type.value if receipt.defect_type else None,
        "inspection_notes": receipt.inspection_notes,
        "packing_slip_number": receipt.packing_slip_number,
        "carrier": receipt.carrier,
        "tracking_number": receipt.tracking_number,
        "location_code": receipt.location.code if receipt.location else None,
        "received_at": receipt.received_at,
        "received_by_name": receipt.receiver.full_name if receipt.receiver else None,
        "inspected_at": receipt.inspected_at,
        "inspected_by_name": receipt.inspector.full_name if receipt.inspector else None,
        "notes": receipt.notes
    }


@router.post("/inspect/{receipt_id}", response_model=InspectionResultResponse)
def inspect_receipt(
    receipt_id: int,
    inspection: ReceiptInspection,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY, UserRole.SUPERVISOR]))
):
    """
    Complete inspection of a receipt.
    - If all accepted: Pass → Add to inventory
    - If partial: Partial → Add accepted to inventory, create NCR for rejected
    - If all rejected: Fail → Create NCR only
    """
    receipt = db.query(POReceipt).options(
        joinedload(POReceipt.po_line).joinedload(PurchaseOrderLine.part),
        joinedload(POReceipt.po_line).joinedload(PurchaseOrderLine.purchase_order).joinedload(PurchaseOrder.vendor),
        joinedload(POReceipt.location)
    ).filter(POReceipt.id == receipt_id).first()
    
    if not receipt:
        raise HTTPException(status_code=404, detail="Receipt not found")
    
    if receipt.status != ReceiptStatus.PENDING_INSPECTION:
        raise HTTPException(status_code=400, detail="Receipt is not pending inspection")
    
    # Validate quantities
    total = inspection.quantity_accepted + inspection.quantity_rejected
    if total > receipt.quantity_received:
        raise HTTPException(
            status_code=400, 
            detail=f"Accepted ({inspection.quantity_accepted}) + rejected ({inspection.quantity_rejected}) = {total} exceeds received ({receipt.quantity_received})"
        )
    
    # Validation: if rejected > 0, defect_type and notes are required
    if inspection.quantity_rejected > 0:
        if not inspection.defect_type:
            raise HTTPException(status_code=400, detail="Defect type is required when rejecting material")
        if not inspection.inspection_notes:
            raise HTTPException(status_code=400, detail="Inspection notes are required when rejecting material")
    
    # Update receipt
    receipt.quantity_accepted = inspection.quantity_accepted
    receipt.quantity_rejected = inspection.quantity_rejected
    receipt.inspection_method = InspectionMethod(inspection.inspection_method)
    receipt.defect_type = DefectType(inspection.defect_type) if inspection.defect_type else None
    receipt.inspection_notes = inspection.inspection_notes
    receipt.inspected_by = current_user.id
    receipt.inspected_at = datetime.utcnow()
    
    # Determine inspection result
    if inspection.quantity_accepted == receipt.quantity_received:
        receipt.inspection_status = InspectionStatus.PASSED
        receipt.status = ReceiptStatus.ACCEPTED
    elif inspection.quantity_rejected == receipt.quantity_received:
        receipt.inspection_status = InspectionStatus.FAILED
        receipt.status = ReceiptStatus.REJECTED
    else:
        receipt.inspection_status = InspectionStatus.PARTIAL
        receipt.status = ReceiptStatus.ACCEPTED  # Partial acceptance
    
    result = InspectionResultResponse(
        receipt=ReceiptResponse.model_validate(receipt),
        inventory_created=False,
        ncr_created=False
    )
    
    po = receipt.po_line.purchase_order
    part = receipt.po_line.part
    
    # Add accepted quantity to inventory
    if inspection.quantity_accepted > 0:
        location_code = receipt.location.code if receipt.location else "RECV-01"
        inv_item = _add_to_inventory(
            db, receipt.po_line.part_id, inspection.quantity_accepted,
            location_code, receipt.lot_number,
            receipt.po_line.unit_price, current_user.id, receipt.receipt_number,
            po.vendor.name if po.vendor else None
        )
        result.inventory_created = True
        result.inventory_item_id = inv_item.id if inv_item else None
    
    # Create NCR for rejected quantity
    if inspection.quantity_rejected > 0:
        ncr = _create_ncr_for_rejection(
            db, receipt, inspection, current_user.id,
            po.vendor.name if po.vendor else None,
            po.po_number,
            part
        )
        result.ncr_created = True
        result.ncr_number = ncr.ncr_number
        result.ncr_id = ncr.id
    
    # Audit log
    log_audit(
        db, current_user.id, "INSPECT_RECEIPT", "po_receipt", receipt.id,
        f"Inspected receipt {receipt.receipt_number}: {inspection.quantity_accepted} accepted, {inspection.quantity_rejected} rejected. Method: {inspection.inspection_method}"
    )
    
    db.commit()
    db.refresh(receipt)
    result.receipt = ReceiptResponse.model_validate(receipt)
    
    return result


def _add_to_inventory(
    db: Session, part_id: int, quantity: float, location: str, 
    lot_number: str, unit_cost: float, user_id: int, reference: str,
    supplier_name: Optional[str] = None
) -> InventoryItem:
    """Add received material to inventory with full traceability"""
    # Check for existing inventory at location with same lot
    existing = db.query(InventoryItem).filter(
        InventoryItem.part_id == part_id,
        InventoryItem.location == location,
        InventoryItem.lot_number == lot_number
    ).first()
    
    if existing:
        existing.quantity_on_hand += quantity
        existing.quantity_available = existing.quantity_on_hand - existing.quantity_allocated
        inv_item = existing
    else:
        inv_item = InventoryItem(
            part_id=part_id,
            location=location,
            lot_number=lot_number,
            quantity_on_hand=quantity,
            quantity_allocated=0,
            quantity_available=quantity,
            unit_cost=unit_cost,
            received_date=datetime.utcnow(),
            po_number=reference,
            status="available",
            is_active=True
        )
        db.add(inv_item)
        db.flush()
    
    # Create transaction record for audit trail
    txn = InventoryTransaction(
        inventory_item_id=inv_item.id,
        part_id=part_id,
        transaction_type=TransactionType.RECEIVE,
        quantity=quantity,
        from_location=None,
        to_location=location,
        lot_number=lot_number,
        unit_cost=unit_cost,
        total_cost=quantity * unit_cost,
        reference_type="po_receipt",
        reference_number=reference,
        notes=f"Received from {supplier_name or 'vendor'} via {reference}",
        created_by=user_id
    )
    db.add(txn)
    
    return inv_item


def _create_ncr_for_rejection(
    db: Session, receipt: POReceipt, inspection: ReceiptInspection,
    user_id: int, supplier_name: Optional[str], po_number: str, part
) -> NonConformanceReport:
    """Create NCR in draft status for rejected material"""
    ncr_number = generate_ncr_number(db)
    
    defect_descriptions = {
        "dimensional": "Material does not meet dimensional specifications",
        "cosmetic": "Visual/surface defects found",
        "material": "Material composition or properties non-conforming",
        "documentation": "Missing or incorrect documentation",
        "functional": "Functional testing failed",
        "contamination": "Material contamination detected",
        "packaging": "Packaging damage or contamination",
        "other": "Other non-conformance"
    }
    
    defect_type = inspection.defect_type or "other"
    description = f"{defect_descriptions.get(defect_type, 'Non-conformance detected')}\n\nInspector Notes: {inspection.inspection_notes or 'N/A'}"
    
    ncr = NonConformanceReport(
        ncr_number=ncr_number,
        part_id=receipt.po_line.part_id,
        receipt_id=receipt.id,
        lot_number=receipt.lot_number,
        quantity_affected=inspection.quantity_rejected,
        quantity_rejected=inspection.quantity_rejected,
        source=NCRSource.INCOMING_INSPECTION,
        status=NCRStatus.OPEN,  # Draft - needs QA review
        disposition=NCRDisposition.PENDING,
        title=f"Incoming Inspection Rejection - {part.part_number if part else 'N/A'}",
        description=description,
        specification=f"Receipt: {receipt.receipt_number}, Method: {inspection.inspection_method}",
        supplier_name=supplier_name,
        supplier_lot=receipt.lot_number,
        po_number=po_number,
        detected_by=user_id,
        detected_date=date.today()
    )
    db.add(ncr)
    db.flush()
    
    # Audit log for NCR creation
    log_audit(
        db, user_id, "CREATE_NCR", "ncr", ncr.id,
        f"Auto-created NCR {ncr_number} for incoming inspection rejection on receipt {receipt.receipt_number}"
    )
    
    return ncr


@router.get("/history")
def get_receiving_history(
    days: int = 30,
    status: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get receiving history with inspection results"""
    cutoff = datetime.utcnow() - timedelta(days=days)
    
    query = db.query(POReceipt).options(
        joinedload(POReceipt.po_line).joinedload(PurchaseOrderLine.part),
        joinedload(POReceipt.po_line).joinedload(PurchaseOrderLine.purchase_order).joinedload(PurchaseOrder.vendor),
        joinedload(POReceipt.receiver),
        joinedload(POReceipt.inspector)
    ).filter(POReceipt.received_at >= cutoff)
    
    if status:
        query = query.filter(POReceipt.status == ReceiptStatus(status))
    
    receipts = query.order_by(POReceipt.received_at.desc()).limit(200).all()
    
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
            "lot_number": r.lot_number,
            "status": r.status.value if hasattr(r.status, 'value') else r.status,
            "inspection_status": r.inspection_status.value if hasattr(r.inspection_status, 'value') else r.inspection_status,
            "inspection_method": r.inspection_method.value if r.inspection_method else None,
            "defect_type": r.defect_type.value if r.defect_type else None,
            "received_at": r.received_at,
            "received_by_name": r.receiver.full_name if r.receiver else None,
            "inspected_at": r.inspected_at,
            "inspected_by_name": r.inspector.full_name if r.inspector else None
        })
    
    return result


@router.get("/stats")
def get_receiving_stats(
    days: int = 30,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get receiving statistics for dashboard"""
    cutoff = datetime.utcnow() - timedelta(days=days)
    
    # Count pending inspections
    pending_count = db.query(func.count(POReceipt.id)).filter(
        POReceipt.status == ReceiptStatus.PENDING_INSPECTION
    ).scalar()
    
    # Count received in period
    received_count = db.query(func.count(POReceipt.id)).filter(
        POReceipt.received_at >= cutoff
    ).scalar()
    
    # Count inspected in period
    inspected_count = db.query(func.count(POReceipt.id)).filter(
        POReceipt.inspected_at >= cutoff,
        POReceipt.status != ReceiptStatus.PENDING_INSPECTION
    ).scalar()
    
    # Count rejections
    rejection_count = db.query(func.count(POReceipt.id)).filter(
        POReceipt.inspected_at >= cutoff,
        POReceipt.quantity_rejected > 0
    ).scalar()
    
    # Total quantities
    qty_stats = db.query(
        func.sum(POReceipt.quantity_received).label('total_received'),
        func.sum(POReceipt.quantity_accepted).label('total_accepted'),
        func.sum(POReceipt.quantity_rejected).label('total_rejected')
    ).filter(POReceipt.received_at >= cutoff).first()
    
    return {
        "pending_inspection": pending_count or 0,
        "receipts_in_period": received_count or 0,
        "inspected_in_period": inspected_count or 0,
        "rejections_in_period": rejection_count or 0,
        "total_qty_received": qty_stats.total_received or 0 if qty_stats else 0,
        "total_qty_accepted": qty_stats.total_accepted or 0 if qty_stats else 0,
        "total_qty_rejected": qty_stats.total_rejected or 0 if qty_stats else 0,
        "acceptance_rate": round(
            (qty_stats.total_accepted / qty_stats.total_received * 100) 
            if qty_stats and qty_stats.total_received else 100, 1
        )
    }


@router.get("/locations")
def get_receiving_locations(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get available locations for receiving"""
    locations = db.query(InventoryLocation).filter(
        InventoryLocation.is_active == True,
        InventoryLocation.is_receivable == True
    ).order_by(InventoryLocation.code).all()
    
    return [{"id": loc.id, "code": loc.code, "name": loc.name} for loc in locations]
