from typing import List, Optional
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import or_
from app.db.database import get_db
from app.api.deps import get_current_user
from app.models.user import User
from app.models.inventory import InventoryItem, InventoryTransaction, TransactionType
from app.models.work_order import WorkOrder, WorkOrderOperation
from app.models.purchasing import POReceipt, PurchaseOrderLine, PurchaseOrder
from app.models.quality import NonConformanceReport
from app.models.shipping import Shipment
from app.models.part import Part
from pydantic import BaseModel

router = APIRouter()


class LotHistoryItem(BaseModel):
    timestamp: datetime
    event_type: str
    description: str
    quantity: Optional[float] = None
    location: Optional[str] = None
    reference: Optional[str] = None
    user: Optional[str] = None
    details: Optional[dict] = None


class LotTraceResponse(BaseModel):
    lot_number: str
    part_id: Optional[int] = None
    part_number: Optional[str] = None
    part_name: Optional[str] = None
    
    # Source info
    supplier_name: Optional[str] = None
    po_number: Optional[str] = None
    received_date: Optional[datetime] = None
    cert_number: Optional[str] = None
    heat_lot: Optional[str] = None
    
    # Current status
    current_quantity: float = 0
    current_location: Optional[str] = None
    status: str = "unknown"
    
    # Usage
    work_orders_used: List[str] = []
    shipments: List[str] = []
    ncrs: List[str] = []
    
    # Full history
    history: List[LotHistoryItem] = []


@router.get("/lot/{lot_number}", response_model=LotTraceResponse)
def trace_lot(
    lot_number: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get full traceability for a lot number.
    Shows source, usage, and current status for AS9100D compliance.
    """
    history: List[LotHistoryItem] = []
    work_orders_used = set()
    shipments_list = set()
    ncrs_list = set()
    
    # Find inventory items with this lot
    inv_items = db.query(InventoryItem).options(
        joinedload(InventoryItem.part)
    ).filter(
        InventoryItem.lot_number == lot_number
    ).all()
    
    part = None
    current_qty = 0
    current_loc = None
    status = "unknown"
    cert_number = None
    heat_lot = None
    received_date = None
    po_number = None
    supplier_name = None
    
    if inv_items:
        item = inv_items[0]
        part = item.part
        current_qty = sum(i.quantity_on_hand for i in inv_items)
        current_loc = inv_items[0].location
        status = inv_items[0].status
        cert_number = item.cert_number
        heat_lot = item.heat_lot
        received_date = item.received_date
        po_number = item.po_number
    
    # Find transactions with this lot
    transactions = db.query(InventoryTransaction).options(
        joinedload(InventoryTransaction.user),
        joinedload(InventoryTransaction.part)
    ).filter(
        InventoryTransaction.lot_number == lot_number
    ).order_by(InventoryTransaction.created_at).all()
    
    for txn in transactions:
        if not part and txn.part:
            part = txn.part
        
        event_type = txn.transaction_type.value
        desc = f"{event_type.upper()}: {abs(txn.quantity)} units"
        
        if txn.to_location:
            desc += f" to {txn.to_location}"
        if txn.from_location:
            desc += f" from {txn.from_location}"
        
        if txn.reference_type == "work_order" and txn.reference_number:
            work_orders_used.add(txn.reference_number)
        
        history.append(LotHistoryItem(
            timestamp=txn.created_at,
            event_type=event_type,
            description=desc,
            quantity=txn.quantity,
            location=txn.to_location or txn.from_location,
            reference=txn.reference_number,
            user=txn.user.full_name if txn.user else None,
            details={
                "reference_type": txn.reference_type,
                "reason": txn.reason_code,
                "notes": txn.notes
            }
        ))
    
    # Check PO receipts
    receipts = db.query(POReceipt).options(
        joinedload(POReceipt.po_line).joinedload(PurchaseOrderLine.purchase_order).joinedload(PurchaseOrder.vendor)
    ).filter(
        POReceipt.lot_number == lot_number
    ).all()
    
    for r in receipts:
        if r.po_line and r.po_line.purchase_order:
            po = r.po_line.purchase_order
            po_number = po.po_number
            if po.vendor:
                supplier_name = po.vendor.name
        
        history.append(LotHistoryItem(
            timestamp=r.received_at,
            event_type="received",
            description=f"Received {r.quantity_received} units - Receipt #{r.receipt_number}",
            quantity=r.quantity_received,
            location=None,
            reference=r.receipt_number,
            details={
                "cert_number": r.cert_number,
                "packing_slip": r.packing_slip_number,
                "inspection_status": r.status.value if r.status else None
            }
        ))
    
    # Check NCRs with this lot
    ncrs = db.query(NonConformanceReport).filter(
        NonConformanceReport.lot_number == lot_number
    ).all()
    
    for ncr in ncrs:
        ncrs_list.add(ncr.ncr_number)
        history.append(LotHistoryItem(
            timestamp=ncr.created_at,
            event_type="ncr",
            description=f"NCR #{ncr.ncr_number} - {ncr.disposition or 'Open'}",
            reference=ncr.ncr_number,
            details={
                "description": ncr.description,
                "disposition": ncr.disposition
            }
        ))
    
    # Check shipments (if lot tracking added to shipments)
    # This would require joining through work orders
    
    # Sort history by timestamp
    history.sort(key=lambda x: x.timestamp)
    
    return LotTraceResponse(
        lot_number=lot_number,
        part_id=part.id if part else None,
        part_number=part.part_number if part else None,
        part_name=part.name if part else None,
        supplier_name=supplier_name,
        po_number=po_number,
        received_date=received_date,
        cert_number=cert_number,
        heat_lot=heat_lot,
        current_quantity=current_qty,
        current_location=current_loc,
        status=status,
        work_orders_used=list(work_orders_used),
        shipments=list(shipments_list),
        ncrs=list(ncrs_list),
        history=history
    )


@router.get("/serial/{serial_number}")
def trace_serial(
    serial_number: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get traceability for a serial number"""
    # Similar to lot tracing but for individual serial numbers
    inv_item = db.query(InventoryItem).options(
        joinedload(InventoryItem.part)
    ).filter(
        InventoryItem.serial_number == serial_number
    ).first()
    
    if not inv_item:
        raise HTTPException(status_code=404, detail="Serial number not found")
    
    transactions = db.query(InventoryTransaction).options(
        joinedload(InventoryTransaction.user)
    ).filter(
        InventoryTransaction.serial_number == serial_number
    ).order_by(InventoryTransaction.created_at).all()
    
    history = []
    for txn in transactions:
        history.append({
            "timestamp": txn.created_at,
            "event_type": txn.transaction_type.value,
            "quantity": txn.quantity,
            "location": txn.to_location or txn.from_location,
            "reference": txn.reference_number,
            "user": txn.user.full_name if txn.user else None
        })
    
    return {
        "serial_number": serial_number,
        "part_number": inv_item.part.part_number if inv_item.part else None,
        "part_name": inv_item.part.name if inv_item.part else None,
        "lot_number": inv_item.lot_number,
        "current_location": inv_item.location,
        "status": inv_item.status,
        "received_date": inv_item.received_date,
        "cert_number": inv_item.cert_number,
        "history": history
    }


@router.get("/search")
def search_lots(
    q: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Search for lot/serial numbers"""
    results = []
    
    # Search inventory items
    items = db.query(InventoryItem).options(
        joinedload(InventoryItem.part)
    ).filter(
        or_(
            InventoryItem.lot_number.ilike(f"%{q}%"),
            InventoryItem.serial_number.ilike(f"%{q}%"),
            InventoryItem.cert_number.ilike(f"%{q}%"),
            InventoryItem.heat_lot.ilike(f"%{q}%")
        )
    ).limit(50).all()
    
    seen = set()
    for item in items:
        if item.lot_number and item.lot_number not in seen:
            seen.add(item.lot_number)
            results.append({
                "type": "lot",
                "number": item.lot_number,
                "part_number": item.part.part_number if item.part else None,
                "part_name": item.part.name if item.part else None,
                "quantity": item.quantity_on_hand,
                "location": item.location
            })
        if item.serial_number and item.serial_number not in seen:
            seen.add(item.serial_number)
            results.append({
                "type": "serial",
                "number": item.serial_number,
                "part_number": item.part.part_number if item.part else None,
                "part_name": item.part.name if item.part else None,
                "location": item.location
            })
    
    return results
