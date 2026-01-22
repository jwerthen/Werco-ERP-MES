from typing import List, Optional
from datetime import datetime, date
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func, and_
from app.db.database import get_db
from app.api.deps import get_current_user, require_role
from app.models.user import User, UserRole
from app.models.inventory import (
    InventoryItem, InventoryTransaction, InventoryLocation,
    CycleCount, CycleCountItem, TransactionType, CycleCountStatus
)
from app.models.part import Part
from pydantic import BaseModel

router = APIRouter()


@router.get("/low-stock")
def get_low_stock_alerts(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get parts with inventory below reorder point"""
    # Get all parts with reorder points set
    parts_with_reorder = db.query(Part).filter(
        Part.reorder_point > 0,
        Part.is_active == True
    ).all()
    if not parts_with_reorder:
        return []

    # Aggregate inventory quantities in a single query
    totals = db.query(
        InventoryItem.part_id,
        func.sum(InventoryItem.quantity_on_hand).label("total_qty")
    ).filter(
        InventoryItem.is_active == True,
        InventoryItem.part_id.in_([p.id for p in parts_with_reorder])
    ).group_by(InventoryItem.part_id).all()

    totals_by_part_id = {row.part_id: float(row.total_qty or 0) for row in totals}

    alerts = []
    for part in parts_with_reorder:
        total_qty = totals_by_part_id.get(part.id, 0.0)

        if total_qty <= part.reorder_point:
            alerts.append({
                "part_id": part.id,
                "part_number": part.part_number,
                "part_name": part.name,
                "quantity_on_hand": total_qty,
                "reorder_point": part.reorder_point,
                "reorder_quantity": part.reorder_quantity,
                "safety_stock": part.safety_stock,
                "shortage": part.reorder_point - total_qty,
                "is_critical": total_qty <= part.safety_stock
            })
    
    # Sort by critical first, then by shortage
    alerts.sort(key=lambda x: (not x["is_critical"], -x["shortage"]))
    return alerts


# Pydantic schemas
class LocationCreate(BaseModel):
    code: str
    name: Optional[str] = None
    warehouse: str
    zone: Optional[str] = None
    aisle: Optional[str] = None
    rack: Optional[str] = None
    shelf: Optional[str] = None
    bin: Optional[str] = None
    location_type: str = "bin"
    is_pickable: bool = True
    is_receivable: bool = True


class ReceiveItemRequest(BaseModel):
    part_id: int
    quantity: float
    location_code: str
    lot_number: Optional[str] = None
    serial_number: Optional[str] = None
    po_number: Optional[str] = None
    unit_cost: float = 0.0
    cert_number: Optional[str] = None
    heat_lot: Optional[str] = None
    notes: Optional[str] = None


class IssueItemRequest(BaseModel):
    inventory_item_id: int
    quantity: float
    work_order_number: Optional[str] = None
    notes: Optional[str] = None


class TransferRequest(BaseModel):
    inventory_item_id: int
    quantity: float
    to_location_code: str
    notes: Optional[str] = None


class AdjustmentRequest(BaseModel):
    inventory_item_id: int
    new_quantity: float
    reason_code: str
    notes: Optional[str] = None


class CycleCountCreate(BaseModel):
    warehouse: Optional[str] = None
    location_code: Optional[str] = None
    part_id: Optional[int] = None
    scheduled_date: date
    notes: Optional[str] = None


class CountItemRequest(BaseModel):
    counted_quantity: float
    notes: Optional[str] = None


# Location endpoints
@router.get("/locations")
def list_locations(
    warehouse: Optional[str] = None,
    active_only: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    query = db.query(InventoryLocation)
    if warehouse:
        query = query.filter(InventoryLocation.warehouse == warehouse)
    if active_only:
        query = query.filter(InventoryLocation.is_active == True)
    return query.order_by(InventoryLocation.code).all()


@router.post("/locations")
def create_location(
    loc_in: LocationCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER]))
):
    existing = db.query(InventoryLocation).filter(InventoryLocation.code == loc_in.code).first()
    if existing:
        raise HTTPException(status_code=400, detail="Location code already exists")
    
    location = InventoryLocation(**loc_in.model_dump())
    db.add(location)
    db.commit()
    db.refresh(location)
    return location


# Inventory endpoints
@router.get("/")
def list_inventory(
    part_id: Optional[int] = None,
    warehouse: Optional[str] = None,
    location_code: Optional[str] = None,
    has_quantity: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    query = db.query(InventoryItem).options(joinedload(InventoryItem.part))
    
    if part_id:
        query = query.filter(InventoryItem.part_id == part_id)
    if warehouse:
        query = query.filter(InventoryItem.warehouse == warehouse)
    if location_code:
        query = query.filter(InventoryItem.location == location_code)
    if has_quantity:
        query = query.filter(InventoryItem.quantity_on_hand > 0)
    
    return query.order_by(InventoryItem.part_id, InventoryItem.location).all()


@router.get("/summary")
def get_inventory_summary(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get inventory summary by part with locations"""
    # Get all inventory items with quantity
    items = db.query(InventoryItem).options(
        joinedload(InventoryItem.part)
    ).filter(
        InventoryItem.is_active == True,
        InventoryItem.quantity_on_hand > 0
    ).all()
    
    # Group by part
    by_part = {}
    for item in items:
        pid = item.part_id
        if pid not in by_part:
            by_part[pid] = {
                "part_id": pid,
                "part_number": item.part.part_number if item.part else "",
                "part_name": item.part.name if item.part else "",
                "total_on_hand": 0,
                "total_allocated": 0,
                "locations": []
            }
        by_part[pid]["total_on_hand"] += item.quantity_on_hand
        by_part[pid]["total_allocated"] += item.quantity_allocated
        by_part[pid]["locations"].append({
            "location": item.location,
            "quantity": item.quantity_on_hand,
            "lot_number": item.lot_number
        })
    
    result = []
    for data in by_part.values():
        data["available"] = data["total_on_hand"] - data["total_allocated"]
        result.append(data)
    
    return result


@router.post("/receive")
def receive_inventory(
    receive_in: ReceiveItemRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Receive inventory into stock"""
    # Verify part exists
    part = db.query(Part).filter(Part.id == receive_in.part_id).first()
    if not part:
        raise HTTPException(status_code=404, detail="Part not found")
    
    # Verify location exists
    location = db.query(InventoryLocation).filter(InventoryLocation.code == receive_in.location_code).first()
    if not location:
        raise HTTPException(status_code=404, detail="Location not found")
    
    # Check for existing inventory at this location with same lot
    existing = db.query(InventoryItem).filter(
        InventoryItem.part_id == receive_in.part_id,
        InventoryItem.location == receive_in.location_code,
        InventoryItem.lot_number == receive_in.lot_number
    ).first()
    
    if existing:
        existing.quantity_on_hand += receive_in.quantity
        existing.quantity_available = existing.quantity_on_hand - existing.quantity_allocated
        inv_item = existing
    else:
        inv_item = InventoryItem(
            part_id=receive_in.part_id,
            location=receive_in.location_code,
            warehouse=location.warehouse,
            quantity_on_hand=receive_in.quantity,
            quantity_available=receive_in.quantity,
            lot_number=receive_in.lot_number,
            serial_number=receive_in.serial_number,
            po_number=receive_in.po_number,
            unit_cost=receive_in.unit_cost,
            cert_number=receive_in.cert_number,
            heat_lot=receive_in.heat_lot,
            received_date=datetime.utcnow()
        )
        db.add(inv_item)
    
    db.flush()
    
    # Create transaction
    txn = InventoryTransaction(
        inventory_item_id=inv_item.id,
        part_id=receive_in.part_id,
        transaction_type=TransactionType.RECEIVE,
        quantity=receive_in.quantity,
        to_location=receive_in.location_code,
        lot_number=receive_in.lot_number,
        serial_number=receive_in.serial_number,
        reference_type="purchase_order" if receive_in.po_number else None,
        reference_number=receive_in.po_number,
        unit_cost=receive_in.unit_cost,
        total_cost=receive_in.quantity * receive_in.unit_cost,
        notes=receive_in.notes,
        created_by=current_user.id
    )
    db.add(txn)
    db.commit()
    
    return {"message": "Inventory received", "inventory_item_id": inv_item.id, "quantity": receive_in.quantity}


@router.post("/issue")
def issue_inventory(
    issue_in: IssueItemRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Issue inventory to work order"""
    inv_item = db.query(InventoryItem).filter(InventoryItem.id == issue_in.inventory_item_id).first()
    if not inv_item:
        raise HTTPException(status_code=404, detail="Inventory item not found")
    
    if inv_item.quantity_available < issue_in.quantity:
        raise HTTPException(status_code=400, detail=f"Insufficient quantity. Available: {inv_item.quantity_available}")
    
    inv_item.quantity_on_hand -= issue_in.quantity
    inv_item.quantity_available = inv_item.quantity_on_hand - inv_item.quantity_allocated
    
    txn = InventoryTransaction(
        inventory_item_id=inv_item.id,
        part_id=inv_item.part_id,
        transaction_type=TransactionType.ISSUE,
        quantity=-issue_in.quantity,
        from_location=inv_item.location,
        lot_number=inv_item.lot_number,
        serial_number=inv_item.serial_number,
        reference_type="work_order" if issue_in.work_order_number else None,
        reference_number=issue_in.work_order_number,
        unit_cost=inv_item.unit_cost,
        total_cost=issue_in.quantity * inv_item.unit_cost,
        notes=issue_in.notes,
        created_by=current_user.id
    )
    db.add(txn)
    db.commit()
    
    return {"message": "Inventory issued", "quantity": issue_in.quantity}


@router.post("/transfer")
def transfer_inventory(
    transfer_in: TransferRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Transfer inventory between locations"""
    inv_item = db.query(InventoryItem).filter(InventoryItem.id == transfer_in.inventory_item_id).first()
    if not inv_item:
        raise HTTPException(status_code=404, detail="Inventory item not found")
    
    to_location = db.query(InventoryLocation).filter(InventoryLocation.code == transfer_in.to_location_code).first()
    if not to_location:
        raise HTTPException(status_code=404, detail="Destination location not found")
    
    if inv_item.quantity_available < transfer_in.quantity:
        raise HTTPException(status_code=400, detail="Insufficient quantity")
    
    from_location = inv_item.location
    
    # Reduce from source
    inv_item.quantity_on_hand -= transfer_in.quantity
    inv_item.quantity_available = inv_item.quantity_on_hand - inv_item.quantity_allocated
    
    # Add to destination (or create new)
    dest_inv = db.query(InventoryItem).filter(
        InventoryItem.part_id == inv_item.part_id,
        InventoryItem.location == transfer_in.to_location_code,
        InventoryItem.lot_number == inv_item.lot_number
    ).first()
    
    if dest_inv:
        dest_inv.quantity_on_hand += transfer_in.quantity
        dest_inv.quantity_available = dest_inv.quantity_on_hand - dest_inv.quantity_allocated
    else:
        dest_inv = InventoryItem(
            part_id=inv_item.part_id,
            location=transfer_in.to_location_code,
            warehouse=to_location.warehouse,
            quantity_on_hand=transfer_in.quantity,
            quantity_available=transfer_in.quantity,
            lot_number=inv_item.lot_number,
            serial_number=inv_item.serial_number,
            unit_cost=inv_item.unit_cost,
            received_date=inv_item.received_date
        )
        db.add(dest_inv)
    
    # Transaction
    txn = InventoryTransaction(
        inventory_item_id=inv_item.id,
        part_id=inv_item.part_id,
        transaction_type=TransactionType.TRANSFER,
        quantity=transfer_in.quantity,
        from_location=from_location,
        to_location=transfer_in.to_location_code,
        lot_number=inv_item.lot_number,
        notes=transfer_in.notes,
        created_by=current_user.id
    )
    db.add(txn)
    db.commit()
    
    return {"message": "Transfer complete"}


@router.post("/adjust")
def adjust_inventory(
    adjust_in: AdjustmentRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]))
):
    """Adjust inventory quantity"""
    inv_item = db.query(InventoryItem).filter(InventoryItem.id == adjust_in.inventory_item_id).first()
    if not inv_item:
        raise HTTPException(status_code=404, detail="Inventory item not found")
    
    old_qty = inv_item.quantity_on_hand
    variance = adjust_in.new_quantity - old_qty
    
    inv_item.quantity_on_hand = adjust_in.new_quantity
    inv_item.quantity_available = inv_item.quantity_on_hand - inv_item.quantity_allocated
    
    txn = InventoryTransaction(
        inventory_item_id=inv_item.id,
        part_id=inv_item.part_id,
        transaction_type=TransactionType.ADJUST,
        quantity=variance,
        from_location=inv_item.location,
        to_location=inv_item.location,
        lot_number=inv_item.lot_number,
        reason_code=adjust_in.reason_code,
        notes=f"Adjusted from {old_qty} to {adjust_in.new_quantity}. {adjust_in.notes or ''}",
        unit_cost=inv_item.unit_cost,
        total_cost=abs(variance) * inv_item.unit_cost,
        created_by=current_user.id
    )
    db.add(txn)
    db.commit()
    
    return {"message": "Adjustment complete", "old_quantity": old_qty, "new_quantity": adjust_in.new_quantity}


# Cycle Count endpoints
@router.get("/cycle-counts")
def list_cycle_counts(
    status: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    query = db.query(CycleCount).options(joinedload(CycleCount.items))
    if status:
        query = query.filter(CycleCount.status == status)
    return query.order_by(CycleCount.scheduled_date.desc()).all()


@router.post("/cycle-counts")
def create_cycle_count(
    count_in: CycleCountCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]))
):
    """Create a new cycle count"""
    # Generate count number
    today = datetime.now().strftime("%Y%m%d")
    prefix = f"CC-{today}-"
    last = db.query(CycleCount).filter(CycleCount.count_number.like(f"{prefix}%")).order_by(CycleCount.count_number.desc()).first()
    num = int(last.count_number.split("-")[-1]) + 1 if last else 1
    
    count = CycleCount(
        count_number=f"{prefix}{num:03d}",
        warehouse=count_in.warehouse,
        part_id=count_in.part_id,
        scheduled_date=count_in.scheduled_date,
        notes=count_in.notes,
        created_by=current_user.id
    )
    
    # Get location if specified
    if count_in.location_code:
        loc = db.query(InventoryLocation).filter(InventoryLocation.code == count_in.location_code).first()
        if loc:
            count.location_id = loc.id
            count.warehouse = loc.warehouse
    
    db.add(count)
    db.flush()
    
    # Add items to count
    query = db.query(InventoryItem).filter(InventoryItem.is_active == True, InventoryItem.quantity_on_hand > 0)
    
    if count.warehouse:
        query = query.filter(InventoryItem.warehouse == count.warehouse)
    if count_in.location_code:
        query = query.filter(InventoryItem.location == count_in.location_code)
    if count.part_id:
        query = query.filter(InventoryItem.part_id == count.part_id)
    
    items = query.all()
    for inv in items:
        count_item = CycleCountItem(
            cycle_count_id=count.id,
            inventory_item_id=inv.id,
            system_quantity=inv.quantity_on_hand,
            unit_cost=inv.unit_cost
        )
        db.add(count_item)
    
    count.total_items = len(items)
    db.commit()
    db.refresh(count)
    
    return count


@router.post("/cycle-counts/{count_id}/start")
def start_cycle_count(
    count_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    count = db.query(CycleCount).filter(CycleCount.id == count_id).first()
    if not count:
        raise HTTPException(status_code=404, detail="Cycle count not found")
    
    count.status = CycleCountStatus.IN_PROGRESS
    count.started_at = datetime.utcnow()
    count.assigned_to = current_user.id
    db.commit()
    
    return {"message": "Cycle count started"}


@router.post("/cycle-counts/{count_id}/items/{item_id}/count")
def record_count(
    count_id: int,
    item_id: int,
    count_in: CountItemRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Record a count for an item"""
    item = db.query(CycleCountItem).filter(
        CycleCountItem.id == item_id,
        CycleCountItem.cycle_count_id == count_id
    ).first()
    
    if not item:
        raise HTTPException(status_code=404, detail="Count item not found")
    
    item.counted_quantity = count_in.counted_quantity
    item.variance = count_in.counted_quantity - item.system_quantity
    item.variance_value = item.variance * item.unit_cost
    item.is_counted = True
    item.counted_at = datetime.utcnow()
    item.counted_by = current_user.id
    item.notes = count_in.notes
    
    # Update count progress
    count = db.query(CycleCount).filter(CycleCount.id == count_id).first()
    count.items_counted = db.query(CycleCountItem).filter(
        CycleCountItem.cycle_count_id == count_id,
        CycleCountItem.is_counted == True
    ).count()
    
    db.commit()
    
    return {"message": "Count recorded", "variance": item.variance}


@router.post("/cycle-counts/{count_id}/complete")
def complete_cycle_count(
    count_id: int,
    apply_adjustments: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]))
):
    """Complete cycle count and optionally apply adjustments"""
    count = db.query(CycleCount).options(joinedload(CycleCount.items)).filter(CycleCount.id == count_id).first()
    if not count:
        raise HTTPException(status_code=404, detail="Cycle count not found")
    
    # Calculate totals
    total_variance = 0
    items_adjusted = 0
    
    for item in count.items:
        if item.is_counted and item.variance != 0:
            total_variance += item.variance_value
            
            if apply_adjustments:
                # Update inventory
                inv = db.query(InventoryItem).filter(InventoryItem.id == item.inventory_item_id).first()
                if inv:
                    old_qty = inv.quantity_on_hand
                    inv.quantity_on_hand = item.counted_quantity
                    inv.quantity_available = inv.quantity_on_hand - inv.quantity_allocated
                    
                    # Create adjustment transaction
                    txn = InventoryTransaction(
                        inventory_item_id=inv.id,
                        part_id=inv.part_id,
                        transaction_type=TransactionType.COUNT,
                        quantity=item.variance,
                        from_location=inv.location,
                        to_location=inv.location,
                        lot_number=inv.lot_number,
                        reason_code="cycle_count",
                        notes=f"Cycle count {count.count_number}. System: {old_qty}, Counted: {item.counted_quantity}",
                        unit_cost=inv.unit_cost,
                        total_cost=abs(item.variance) * inv.unit_cost,
                        created_by=current_user.id
                    )
                    db.add(txn)
                    items_adjusted += 1
    
    count.status = CycleCountStatus.COMPLETED
    count.completed_at = datetime.utcnow()
    count.completed_by = current_user.id
    count.items_adjusted = items_adjusted
    count.total_variance_value = total_variance
    
    db.commit()
    
    return {
        "message": "Cycle count completed",
        "items_adjusted": items_adjusted,
        "total_variance_value": total_variance
    }


# Transaction history
@router.get("/transactions")
def list_transactions(
    part_id: Optional[int] = None,
    transaction_type: Optional[str] = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    query = db.query(InventoryTransaction).options(joinedload(InventoryTransaction.part))
    
    if part_id:
        query = query.filter(InventoryTransaction.part_id == part_id)
    if transaction_type:
        query = query.filter(InventoryTransaction.transaction_type == transaction_type)
    
    return query.order_by(InventoryTransaction.created_at.desc()).limit(limit).all()
