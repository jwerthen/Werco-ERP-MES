"""Shared canonical shipment creation and allocation arithmetic."""

from datetime import datetime
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.locks import acquire_generator_lock
from app.models.shipping import Shipment, ShipmentStatus
from app.models.work_order import WorkOrder, WorkOrderStatus
from app.services.operational_event_service import OperationalEventService


def generate_shipment_number(db: Session) -> str:
    today = datetime.now().strftime("%Y%m%d")
    prefix = f"SHP-{today}-"

    last = (
        db.query(Shipment)
        .filter(Shipment.shipment_number.like(f"{prefix}%"))
        .order_by(Shipment.shipment_number.desc())
        .first()
    )

    if last:
        last_num = int(last.shipment_number.split("-")[-1])
        new_num = last_num + 1
    else:
        new_num = 1

    return f"{prefix}{new_num:03d}"


def _allocated_quantity(db: Session, company_id: int, work_order_id: int, exclude_id: Optional[int] = None) -> float:
    query = db.query(func.coalesce(func.sum(Shipment.quantity_shipped), 0)).filter(
        Shipment.company_id == company_id,
        Shipment.work_order_id == work_order_id,
        Shipment.status != ShipmentStatus.CANCELLED,
    )
    if exclude_id is not None:
        query = query.filter(Shipment.id != exclude_id)
    return float(query.scalar() or 0)


def create_shipment_command(db, current_user, company_id, shipment_in, audit):
    """Reserve completed quantity in a pending shipment; no commit, dispatch or carrier I/O."""
    wo = (
        db.query(WorkOrder)
        .filter(
            WorkOrder.id == shipment_in.work_order_id,
            WorkOrder.company_id == company_id,
            WorkOrder.is_deleted.is_(False),
        )
        .with_for_update()
        .first()
    )
    if not wo:
        raise HTTPException(status_code=404, detail="Work order not found")

    if wo.status not in (WorkOrderStatus.COMPLETE, WorkOrderStatus.CLOSED):
        raise HTTPException(status_code=409, detail="Complete the work order before creating a shipment")
    remaining = max(0, float(wo.quantity_complete or 0) - _allocated_quantity(db, company_id, wo.id))
    if shipment_in.quantity_shipped > remaining + 1e-9:
        raise HTTPException(
            status_code=409,
            detail=f"Only {remaining:g} completed units remain available. Refresh shipping quantities and try again.",
        )

    acquire_generator_lock(db, "shipment_number")
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
        created_by=current_user.id,
    )
    shipment.company_id = company_id
    db.add(shipment)
    db.flush()
    OperationalEventService(db).emit_best_effort(
        company_id=company_id,
        event_type="shipment_created",
        source_module="shipping",
        entity_type="shipment",
        entity_id=shipment.id,
        work_order_id=shipment.work_order_id,
        user_id=current_user.id,
        severity="info",
        event_payload={
            "shipment_number": shipment.shipment_number,
            "work_order_number": wo.work_order_number,
            "quantity_shipped": shipment.quantity_shipped,
            "carrier": shipment.carrier,
            "service_type": shipment.service_type,
            "status": shipment.status.value if hasattr(shipment.status, "value") else shipment.status,
        },
    )
    audit.log_required(
        "CREATE",
        "shipment",
        resource_id=shipment.id,
        resource_identifier=shipment.shipment_number,
        new_values={
            **shipment_in.model_dump(mode='json'),
            'id': shipment.id,
            'shipment_number': shipment.shipment_number,
            'status': 'pending',
        },
    )
    return shipment
