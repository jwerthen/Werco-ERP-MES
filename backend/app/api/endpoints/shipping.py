from typing import List, Optional
from datetime import datetime, date
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, joinedload
from app.db.database import get_db
from app.api.deps import get_current_user, require_role
from app.models.user import User, UserRole
from app.models.shipping import Shipment, ShipmentStatus
from app.models.work_order import WorkOrder, WorkOrderStatus
from pydantic import BaseModel

router = APIRouter()


class ShipmentCreate(BaseModel):
    work_order_id: int
    ship_to_name: Optional[str] = None
    ship_to_address: Optional[str] = None
    ship_to_city: Optional[str] = None
    ship_to_state: Optional[str] = None
    ship_to_zip: Optional[str] = None
    carrier: Optional[str] = None
    service_type: Optional[str] = None
    quantity_shipped: float
    weight_lbs: Optional[float] = None
    num_packages: int = 1
    packing_notes: Optional[str] = None
    cert_of_conformance: bool = False


class ShipmentUpdate(BaseModel):
    carrier: Optional[str] = None
    service_type: Optional[str] = None
    tracking_number: Optional[str] = None
    ship_date: Optional[date] = None
    estimated_delivery: Optional[date] = None
    status: Optional[str] = None


class ShipmentResponse(BaseModel):
    id: int
    shipment_number: str
    work_order_id: int
    work_order_number: Optional[str] = None
    customer_name: Optional[str] = None
    part_number: Optional[str] = None
    status: str
    ship_to_name: Optional[str] = None
    carrier: Optional[str] = None
    tracking_number: Optional[str] = None
    quantity_shipped: float
    ship_date: Optional[date] = None
    created_at: datetime
    
    class Config:
        from_attributes = True
        use_enum_values = True


def generate_shipment_number(db: Session) -> str:
    today = datetime.now().strftime("%Y%m%d")
    prefix = f"SHP-{today}-"
    
    last = db.query(Shipment).filter(
        Shipment.shipment_number.like(f"{prefix}%")
    ).order_by(Shipment.shipment_number.desc()).first()
    
    if last:
        last_num = int(last.shipment_number.split("-")[-1])
        new_num = last_num + 1
    else:
        new_num = 1
    
    return f"{prefix}{new_num:03d}"


@router.get("/", response_model=List[ShipmentResponse])
def list_shipments(
    status: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    query = db.query(Shipment).options(
        joinedload(Shipment.work_order)
    )
    
    if status:
        query = query.filter(Shipment.status == status)
    
    shipments = query.order_by(Shipment.created_at.desc()).limit(100).all()
    
    result = []
    for s in shipments:
        result.append(ShipmentResponse(
            id=s.id,
            shipment_number=s.shipment_number,
            work_order_id=s.work_order_id,
            work_order_number=s.work_order.work_order_number if s.work_order else None,
            customer_name=s.work_order.customer_name if s.work_order else None,
            part_number=s.work_order.part.part_number if s.work_order and s.work_order.part else None,
            status=s.status.value if hasattr(s.status, 'value') else s.status,
            ship_to_name=s.ship_to_name,
            carrier=s.carrier,
            tracking_number=s.tracking_number,
            quantity_shipped=s.quantity_shipped,
            ship_date=s.ship_date,
            created_at=s.created_at
        ))
    return result


@router.get("/{shipment_id}")
def get_shipment(
    shipment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get single shipment with full details"""
    shipment = db.query(Shipment).options(
        joinedload(Shipment.work_order).joinedload(WorkOrder.part)
    ).filter(Shipment.id == shipment_id).first()
    
    if not shipment:
        raise HTTPException(status_code=404, detail="Shipment not found")
    
    return {
        "id": shipment.id,
        "shipment_number": shipment.shipment_number,
        "work_order_id": shipment.work_order_id,
        "work_order_number": shipment.work_order.work_order_number if shipment.work_order else None,
        "customer_name": shipment.work_order.customer_name if shipment.work_order else None,
        "customer_po": shipment.work_order.customer_po if shipment.work_order else None,
        "part_number": shipment.work_order.part.part_number if shipment.work_order and shipment.work_order.part else None,
        "part_name": shipment.work_order.part.name if shipment.work_order and shipment.work_order.part else None,
        "lot_number": shipment.work_order.lot_number if shipment.work_order else None,
        "status": shipment.status.value if hasattr(shipment.status, 'value') else shipment.status,
        "ship_to_name": shipment.ship_to_name,
        "ship_to_address": shipment.ship_to_address,
        "carrier": shipment.carrier,
        "tracking_number": shipment.tracking_number,
        "quantity_shipped": shipment.quantity_shipped,
        "weight_lbs": shipment.weight_lbs,
        "num_packages": shipment.num_packages,
        "ship_date": shipment.ship_date.isoformat() if shipment.ship_date else None,
        "cert_of_conformance": shipment.cert_of_conformance,
        "packing_notes": shipment.packing_notes,
        "created_at": shipment.created_at.isoformat()
    }


@router.get("/ready-to-ship")
def get_ready_to_ship(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get completed work orders ready to ship"""
    work_orders = db.query(WorkOrder).options(
        joinedload(WorkOrder.part)
    ).filter(
        WorkOrder.status == WorkOrderStatus.COMPLETE
    ).order_by(WorkOrder.due_date).all()
    
    result = []
    for wo in work_orders:
        # Check if already shipped
        existing = db.query(Shipment).filter(
            Shipment.work_order_id == wo.id,
            Shipment.status != ShipmentStatus.CANCELLED
        ).first()
        
        if not existing:
            result.append({
                "work_order_id": wo.id,
                "work_order_number": wo.work_order_number,
                "part_number": wo.part.part_number if wo.part else None,
                "part_name": wo.part.name if wo.part else None,
                "customer_name": wo.customer_name,
                "quantity_complete": wo.quantity_complete,
                "due_date": wo.due_date.isoformat() if wo.due_date else None
            })
    
    return result


@router.post("/", response_model=ShipmentResponse)
def create_shipment(
    shipment_in: ShipmentCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a new shipment"""
    wo = db.query(WorkOrder).filter(WorkOrder.id == shipment_in.work_order_id).first()
    if not wo:
        raise HTTPException(status_code=404, detail="Work order not found")
    
    shipment_number = generate_shipment_number(db)
    
    shipment = Shipment(
        shipment_number=shipment_number,
        work_order_id=shipment_in.work_order_id,
        ship_to_name=shipment_in.ship_to_name or wo.customer_name,
        ship_to_address=shipment_in.ship_to_address,
        ship_to_city=shipment_in.ship_to_city,
        ship_to_state=shipment_in.ship_to_state,
        ship_to_zip=shipment_in.ship_to_zip,
        carrier=shipment_in.carrier,
        service_type=shipment_in.service_type,
        quantity_shipped=shipment_in.quantity_shipped,
        weight_lbs=shipment_in.weight_lbs,
        num_packages=shipment_in.num_packages,
        packing_notes=shipment_in.packing_notes,
        cert_of_conformance=shipment_in.cert_of_conformance,
        packing_slip_number=shipment_number,
        created_by=current_user.id
    )
    
    db.add(shipment)
    db.commit()
    db.refresh(shipment)
    
    return ShipmentResponse(
        id=shipment.id,
        shipment_number=shipment.shipment_number,
        work_order_id=shipment.work_order_id,
        work_order_number=wo.work_order_number,
        customer_name=wo.customer_name,
        part_number=wo.part.part_number if wo.part else None,
        status=shipment.status.value,
        ship_to_name=shipment.ship_to_name,
        carrier=shipment.carrier,
        tracking_number=shipment.tracking_number,
        quantity_shipped=shipment.quantity_shipped,
        ship_date=shipment.ship_date,
        created_at=shipment.created_at
    )


@router.post("/{shipment_id}/ship")
def mark_shipped(
    shipment_id: int,
    tracking_number: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Mark shipment as shipped"""
    shipment = db.query(Shipment).filter(Shipment.id == shipment_id).first()
    if not shipment:
        raise HTTPException(status_code=404, detail="Shipment not found")
    
    shipment.status = ShipmentStatus.SHIPPED
    shipment.ship_date = date.today()
    shipment.shipped_by = current_user.id
    if tracking_number:
        shipment.tracking_number = tracking_number
    
    # Close work order
    wo = shipment.work_order
    if wo:
        wo.status = WorkOrderStatus.CLOSED
    
    db.commit()
    
    return {"message": "Shipment marked as shipped", "shipment_number": shipment.shipment_number}


@router.put("/{shipment_id}", response_model=ShipmentResponse)
def update_shipment(
    shipment_id: int,
    shipment_in: ShipmentUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    shipment = db.query(Shipment).options(
        joinedload(Shipment.work_order)
    ).filter(Shipment.id == shipment_id).first()
    
    if not shipment:
        raise HTTPException(status_code=404, detail="Shipment not found")
    
    update_data = shipment_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        if field == "status":
            setattr(shipment, field, ShipmentStatus(value))
        else:
            setattr(shipment, field, value)
    
    db.commit()
    db.refresh(shipment)
    
    wo = shipment.work_order
    return ShipmentResponse(
        id=shipment.id,
        shipment_number=shipment.shipment_number,
        work_order_id=shipment.work_order_id,
        work_order_number=wo.work_order_number if wo else None,
        customer_name=wo.customer_name if wo else None,
        part_number=wo.part.part_number if wo and wo.part else None,
        status=shipment.status.value if hasattr(shipment.status, 'value') else shipment.status,
        ship_to_name=shipment.ship_to_name,
        carrier=shipment.carrier,
        tracking_number=shipment.tracking_number,
        quantity_shipped=shipment.quantity_shipped,
        ship_date=shipment.ship_date,
        created_at=shipment.created_at
    )
