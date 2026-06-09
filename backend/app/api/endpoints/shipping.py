from datetime import date, datetime
from io import BytesIO
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session, joinedload

from app.api.deps import get_audit_service, get_current_company_id, get_current_user, require_role
from app.core.realtime import safe_broadcast
from app.core.websocket import broadcast_dashboard_update, broadcast_work_order_update
from app.db.database import get_db
from app.models.shipping import (
    CertificateOfConformance,
    Shipment,
    ShipmentRateQuote,
    ShipmentStatus,
    ShipmentTrackingEvent,
)
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder, WorkOrderStatus
from app.schemas.shipping import (
    AddressValidationRequest,
    AddressValidationResponse,
    BuyBolRequest,
    BuyBolResponse,
    BuyLabelRequest,
    BuyLabelResponse,
    RateQuoteResponse,
    RateShopRequest,
    SchedulePickupRequest,
    SchedulePickupResponse,
    ShipmentCreate,
    ShipmentResponse,
    ShipmentTrackingResponse,
    ShipmentUpdate,
    TrackingEventResponse,
    VoidRefundResponse,
)
from app.services.audit_service import AuditService
from app.services.carriers.exceptions import (
    AddressInvalidError,
    CarrierError,
    EgressDisabledError,
    NotSupportedError,
)
from app.services.coc_service import (
    coc_required_for_shipment,
    generate_coc_for_shipment,
    render_coc_pdf,
)
from app.services.completion_inventory_service import (
    decrement_finished_goods_for_shipment,
    record_over_ship_if_needed,
)
from app.services.completion_signal_service import enqueue_work_order_completion_signals
from app.services.operational_event_service import OperationalEventService
from app.services.shipping_service import ShippingService

router = APIRouter()


# Carrier-action RBAC: rate-shop / label / void are documented under the Shipping
# role set (docs/RBAC_PERMISSIONS.md). They transmit customer data to a carrier
# (egress-gated in the service) and move money (audited), so they are restricted
# to ADMIN / MANAGER / SUPERVISOR / SHIPPING -- the same set that may complete a
# shipment. read-only views (rate quotes, tracking) stay open to any tenant user.
CARRIER_WRITE_ROLES = [UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR, UserRole.SHIPPING]


def _map_carrier_error(exc: CarrierError) -> HTTPException:
    """Translate a service-layer carrier error onto a clean HTTP response.

    No provider internals or secrets are surfaced -- only the typed error's
    message (the adapters scrub api keys before raising).
    """
    if isinstance(exc, EgressDisabledError):
        # Customer-data egress is OFF for this company (the kill switch). 409 is
        # the precondition-not-met signal the UI uses to prompt enabling egress.
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, AddressInvalidError):
        return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    if isinstance(exc, NotSupportedError):
        return HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=str(exc))
    # CarrierError "not found" maps to 404; other provider failures are 502.
    message = str(exc)
    if "not found" in message.lower():
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message)
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=message or "Carrier provider error")


class CertificateOfConformanceResponse(BaseModel):
    """Metadata for an issued Certificate of Conformance (the PDF is a separate endpoint)."""

    id: int
    coc_number: str
    shipment_id: int
    work_order_id: int
    customer_name: Optional[str] = None
    customer_po: Optional[str] = None
    part_number: Optional[str] = None
    part_name: Optional[str] = None
    revision: Optional[str] = None
    quantity: Optional[float] = None
    lot_number: Optional[str] = None
    issued_by: Optional[int] = None
    issued_at: Optional[datetime] = None

    class Config:
        from_attributes = True


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


@router.get("/", response_model=List[ShipmentResponse])
def list_shipments(
    status: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    query = db.query(Shipment).filter(Shipment.company_id == company_id).options(joinedload(Shipment.work_order))

    if status:
        query = query.filter(Shipment.status == status)

    shipments = query.order_by(Shipment.created_at.desc()).limit(100).all()

    result = []
    for s in shipments:
        result.append(
            ShipmentResponse(
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
                created_at=s.created_at,
            )
        )
    return result


@router.get("/ready-to-ship")
def get_ready_to_ship(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get completed work orders ready to ship"""
    work_orders = (
        db.query(WorkOrder)
        .filter(WorkOrder.company_id == company_id)
        .options(joinedload(WorkOrder.part))
        .filter(WorkOrder.status == WorkOrderStatus.COMPLETE)
        .order_by(WorkOrder.due_date)
        .all()
    )

    result = []
    for wo in work_orders:
        # Check if already shipped
        existing = (
            db.query(Shipment)
            .filter(Shipment.work_order_id == wo.id, Shipment.status != ShipmentStatus.CANCELLED)
            .first()
        )

        if not existing:
            result.append(
                {
                    "work_order_id": wo.id,
                    "work_order_number": wo.work_order_number,
                    "part_number": wo.part.part_number if wo.part else None,
                    "part_name": wo.part.name if wo.part else None,
                    "customer_name": wo.customer_name,
                    "quantity_complete": wo.quantity_complete,
                    "due_date": wo.due_date.isoformat() if wo.due_date else None,
                }
            )

    return result


@router.get("/{shipment_id}")
def get_shipment(
    shipment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get single shipment with full details"""
    shipment = (
        db.query(Shipment)
        .options(joinedload(Shipment.work_order).joinedload(WorkOrder.part))
        .filter(Shipment.id == shipment_id, Shipment.company_id == company_id)
        .first()
    )

    if not shipment:
        raise HTTPException(status_code=404, detail="Shipment not found")

    return {
        "id": shipment.id,
        "shipment_number": shipment.shipment_number,
        "work_order_id": shipment.work_order_id,
        "work_order_number": shipment.work_order.work_order_number if shipment.work_order else None,
        "customer_name": shipment.work_order.customer_name if shipment.work_order else None,
        "customer_po": shipment.work_order.customer_po if shipment.work_order else None,
        "part_number": (
            shipment.work_order.part.part_number if shipment.work_order and shipment.work_order.part else None
        ),
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
        "created_at": shipment.created_at.isoformat(),
    }


@router.post("/", response_model=ShipmentResponse)
def create_shipment(
    shipment_in: ShipmentCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Create a new shipment"""
    wo = (
        db.query(WorkOrder)
        .filter(WorkOrder.id == shipment_in.work_order_id, WorkOrder.company_id == company_id)
        .first()
    )
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
        created_by=current_user.id,
    )
    shipment.company_id = company_id
    db.add(shipment)
    db.flush()
    OperationalEventService(db).emit(
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
        created_at=shipment.created_at,
    )


@router.post("/{shipment_id}/ship")
def mark_shipped(
    shipment_id: int,
    tracking_number: Optional[str] = None,
    db: Session = Depends(get_db),
    # RBAC (docs/RBAC_PERMISSIONS.md -> Shipping -> "Complete"): marking a shipment shipped
    # is the terminal shipping action that CLOSES the work order, so it is gated to the
    # documented Shipping-Complete role set (ADMIN / MANAGER / SUPERVISOR / SHIPPING). This
    # is an intentional behavior change: non-privileged tenant users now get 403 (previously
    # any authenticated user could close a WO by shipping).
    current_user: User = Depends(
        require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR, UserRole.SHIPPING])
    ),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Mark shipment as shipped"""
    # G2: lock the shipment row while we re-check status + write the offsetting FG
    # decrement, so a concurrent double-ship can't both pass the idempotency guard and
    # double-decrement on-hand. (with_for_update is a no-op on SQLite used by tests.)
    shipment = (
        db.query(Shipment)
        .filter(Shipment.id == shipment_id, Shipment.company_id == company_id)
        .with_for_update()
        .first()
    )
    if not shipment:
        raise HTTPException(status_code=404, detail="Shipment not found")

    # Idempotency guard (EVT-1 / e2): a re-submitted ship of the SAME shipment must not
    # re-run. Only an already-SHIPPED shipment is a no-op here. A *distinct*, not-yet-
    # shipped shipment on a WO that an EARLIER shipment already CLOSED must still ship
    # and decrement finished goods -- partial / multi-shipment WOs are allowed (the FG
    # decrement + cumulative over-ship guard below exist precisely for that case). The
    # WO close is separately gated (below) so it fires exactly ONCE per WO; keying this
    # early return on the WO being closed (the old behavior) made the G2 decrement +
    # over-ship guard unreachable for every 2nd-or-later shipment.
    if shipment.status == ShipmentStatus.SHIPPED:
        return {
            "message": "Shipment already marked as shipped",
            "shipment_number": shipment.shipment_number,
            "already_shipped": True,
        }

    shipment.status = ShipmentStatus.SHIPPED
    shipment.ship_date = date.today()
    shipment.shipped_by = current_user.id
    if tracking_number:
        shipment.tracking_number = tracking_number

    # Close work order (terminal compliance status change) -- ONCE per WO. A later
    # shipment on an already-CLOSED WO ships its units (and decrements FG below) without
    # re-closing / re-auditing / re-emitting / re-enqueueing the closure: wo_previous_status
    # stays None when the WO is already CLOSED, so every close-once side effect (the
    # work_order_closed event, the close audit row, and the post-commit broadcast +
    # completion-signal enqueue) is guarded on `wo_previous_status is not None` and skips.
    wo = shipment.work_order
    wo_previous_status = None
    if wo and wo.status != WorkOrderStatus.CLOSED:
        wo_previous_status = wo.status.value if hasattr(wo.status, "value") else wo.status
        wo.status = WorkOrderStatus.CLOSED

    OperationalEventService(db).emit(
        company_id=company_id,
        event_type="shipment_shipped",
        source_module="shipping",
        entity_type="shipment",
        entity_id=shipment.id,
        work_order_id=shipment.work_order_id,
        user_id=current_user.id,
        severity="info",
        event_payload={
            "shipment_number": shipment.shipment_number,
            "work_order_number": wo.work_order_number if wo else None,
            "tracking_number": shipment.tracking_number,
            "carrier": shipment.carrier,
            "ship_date": shipment.ship_date.isoformat() if shipment.ship_date else None,
        },
    )

    # EVT-1: the WO CLOSED transition is a distinct, terminal completion signal --
    # emit a dedicated work_order_closed OperationalEvent (NOT just the shipment-scoped
    # event above) so AI/realtime consumers see the closure of the work order, uniform
    # with operation_completed / work_order_completed on the other completion paths.
    # Tenant-scoped (the WO belongs to company_id) and best-effort.
    if wo and wo_previous_status is not None:
        try:
            OperationalEventService(db).emit(
                company_id=company_id,
                event_type="work_order_closed",
                source_module="shipping",
                entity_type="work_order",
                entity_id=wo.id,
                work_order_id=wo.id,
                user_id=current_user.id,
                severity="info",
                event_payload={
                    "work_order_number": wo.work_order_number,
                    "status": wo.status.value if hasattr(wo.status, "value") else wo.status,
                    "shipment_number": shipment.shipment_number,
                },
            )
        except Exception:  # pragma: no cover - signal failure must not fail the close
            pass

    # Tamper-evident audit trail (hash chain) for the terminal WO closure on
    # shipment. Flushed (not committed) so the audit row commits atomically with
    # the status change via the db.commit() below.
    if wo and wo_previous_status is not None:
        new_status = wo.status.value if hasattr(wo.status, "value") else wo.status
        audit.log_status_change(
            "work_order",
            wo.id,
            wo.work_order_number,
            wo_previous_status,
            new_status,
            description=(f"Work order {wo.work_order_number} closed on shipment {shipment.shipment_number}"),
        )

    # G2: finished-goods decrement on ship + over-ship guard. Both join THIS unit of work
    # (no commit) so the SHIP InventoryTransaction + on-hand decrement and any
    # discrepancy/over-ship audit rows land ATOMICALLY with the SHIPPED status change and
    # the WO close above. Neither fails the ship (warn-and-record posture): a missing FG
    # lot row or an over-ship is recorded tamper-evidently and the ship/close proceeds.
    if wo is not None:
        decrement_finished_goods_for_shipment(
            db,
            work_order=wo,
            shipment=shipment,
            company_id=company_id,
            user_id=current_user.id,
            audit=audit,
        )
        record_over_ship_if_needed(
            db,
            work_order=wo,
            shipment=shipment,
            company_id=company_id,
            user_id=current_user.id,
            audit=audit,
        )

        # G6-B: auto-issue a Certificate of Conformance when one is required (the shipment
        # was flagged or the customer master requires it). The CoC row + its audit entry
        # join THIS unit of work so they commit atomically with the ship below. CoC
        # generation is BEST-EFFORT and idempotent (DB-enforced per shipment): a failure
        # here must never fail the ship, so it is wrapped and recorded as a warning
        # OperationalEvent (mirrors the warn-and-record posture of the FG/over-ship guards).
        if coc_required_for_shipment(db, work_order=wo, shipment=shipment, company_id=company_id):
            try:
                generate_coc_for_shipment(
                    db,
                    shipment=shipment,
                    company_id=company_id,
                    user_id=current_user.id,
                    audit=audit,
                )
            except Exception:  # pragma: no cover - CoC issuance must never fail a ship
                try:
                    OperationalEventService(db).emit(
                        company_id=company_id,
                        event_type="coc_generation_failed",
                        source_module="shipping",
                        entity_type="shipment",
                        entity_id=shipment.id,
                        work_order_id=wo.id,
                        user_id=current_user.id,
                        severity="warning",
                        event_payload={
                            "shipment_number": shipment.shipment_number,
                            "work_order_number": wo.work_order_number,
                        },
                    )
                except Exception:  # the warning signal itself must never fail the ship
                    pass

    db.commit()

    # EVT-1: realtime + outbound signals for the closure. After commit (so we never
    # signal a rolled-back close) and best-effort. Broadcasts are tenant-scoped to the
    # originating company (rank 3). The notification/webhook dispatch is enqueued to
    # the ARQ worker with status="CLOSED" -> work_order.closed webhook event.
    if wo and wo_previous_status is not None:
        wo_status = wo.status.value if hasattr(wo.status, "value") else wo.status
        safe_broadcast(
            broadcast_work_order_update,
            wo.id,
            {"event": "work_order_closed", "status": wo_status},
            company_id=company_id,
        )
        safe_broadcast(
            broadcast_dashboard_update,
            {"event": "work_order_closed", "work_order_id": wo.id, "status": wo_status},
            company_id=company_id,
        )
        enqueue_work_order_completion_signals(work_order_id=wo.id, company_id=company_id, status="CLOSED")

    return {"message": "Shipment marked as shipped", "shipment_number": shipment.shipment_number}


@router.put("/{shipment_id}", response_model=ShipmentResponse)
def update_shipment(
    shipment_id: int,
    shipment_in: ShipmentUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    shipment = (
        db.query(Shipment)
        .options(joinedload(Shipment.work_order))
        .filter(Shipment.id == shipment_id, Shipment.company_id == company_id)
        .first()
    )

    if not shipment:
        raise HTTPException(status_code=404, detail="Shipment not found")

    previous_status = shipment.status
    update_data = shipment_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        if field == "status":
            setattr(shipment, field, ShipmentStatus(value))
        else:
            setattr(shipment, field, value)

    OperationalEventService(db).emit(
        company_id=company_id,
        event_type="shipment_updated",
        source_module="shipping",
        entity_type="shipment",
        entity_id=shipment.id,
        work_order_id=shipment.work_order_id,
        user_id=current_user.id,
        severity="info" if shipment.status == previous_status else "medium",
        event_payload={
            "shipment_number": shipment.shipment_number,
            "changed_fields": list(update_data.keys()),
            "previous_status": previous_status.value if hasattr(previous_status, "value") else previous_status,
            "status": shipment.status.value if hasattr(shipment.status, "value") else shipment.status,
            "tracking_number": shipment.tracking_number,
            "carrier": shipment.carrier,
        },
    )
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
        created_at=shipment.created_at,
    )


def _coc_for_shipment(db: Session, shipment_id: int, company_id: int) -> Optional[CertificateOfConformance]:
    """Tenant-scoped lookup of the CoC issued for a shipment (None if not yet issued)."""
    return (
        db.query(CertificateOfConformance)
        .filter(
            CertificateOfConformance.company_id == company_id,
            CertificateOfConformance.shipment_id == shipment_id,
        )
        .first()
    )


@router.post("/{shipment_id}/coc", response_model=CertificateOfConformanceResponse)
def issue_certificate_of_conformance(
    shipment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Issue (or return the existing) Certificate of Conformance for a shipment.

    Idempotent: re-issuing returns the same CoC without a second audit row. RBAC is
    restricted to ADMIN / MANAGER / QUALITY (quality artifact). Tenant-scoped.
    """
    shipment = db.query(Shipment).filter(Shipment.id == shipment_id, Shipment.company_id == company_id).first()
    if not shipment:
        raise HTTPException(status_code=404, detail="Shipment not found")

    coc = generate_coc_for_shipment(
        db,
        shipment=shipment,
        company_id=company_id,
        user_id=current_user.id,
        audit=audit,
    )
    db.commit()
    db.refresh(coc)
    return CertificateOfConformanceResponse.model_validate(coc)


@router.get("/{shipment_id}/coc", response_model=CertificateOfConformanceResponse)
def get_certificate_of_conformance(
    shipment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Return the metadata for a shipment's issued CoC (404 if none has been issued)."""
    coc = _coc_for_shipment(db, shipment_id, company_id)
    if not coc:
        raise HTTPException(status_code=404, detail="Certificate of Conformance not found")
    return CertificateOfConformanceResponse.model_validate(coc)


@router.get("/{shipment_id}/coc/pdf")
def download_certificate_of_conformance_pdf(
    shipment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Render and stream the CoC PDF (deterministically, from the frozen snapshot)."""
    coc = _coc_for_shipment(db, shipment_id, company_id)
    if not coc:
        raise HTTPException(status_code=404, detail="Certificate of Conformance not found")

    pdf_bytes = render_coc_pdf(coc, db)
    filename = f"{coc.coc_number}.pdf"
    return StreamingResponse(
        BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ===========================================================================
# Multi-carrier integration endpoints.
#
# Routers stay THIN: each delegates to ``ShippingService`` (the egress kill
# switch, provider selection, audit, and idempotency all live there) and maps
# the typed carrier errors onto HTTP via ``_map_carrier_error``. company_id is
# always the ACTIVE company (get_current_company_id); the audit service is
# request-scoped (get_audit_service). Provider-calling routes are ``async def``.
# ===========================================================================


@router.post("/validate-address", response_model=AddressValidationResponse)
async def validate_address(
    payload: AddressValidationRequest,
    carrier_account_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(CARRIER_WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
):
    """Validate / normalize a postal address via the carrier (egress-gated)."""
    service = ShippingService(db)
    try:
        result = await service.validate_address(company_id, payload.address, carrier_account_id=carrier_account_id)
    except CarrierError as exc:
        raise _map_carrier_error(exc)
    return AddressValidationResponse.model_validate(result.model_dump())


@router.post("/{shipment_id}/rate-shop", response_model=List[RateQuoteResponse])
async def rate_shop(
    shipment_id: int,
    payload: RateShopRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(CARRIER_WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
):
    """Rate-shop a shipment and persist the quotes (egress-gated)."""
    service = ShippingService(db)
    try:
        quotes = await service.rate_shop(
            company_id,
            shipment_id,
            parcels=payload.parcels,
            pallets=payload.pallets,
            ship_from=payload.ship_from,
            ship_to=payload.ship_to,
            carrier_account_id=payload.carrier_account_id,
            user_id=current_user.id,
        )
    except CarrierError as exc:
        raise _map_carrier_error(exc)
    return [RateQuoteResponse.model_validate(q) for q in quotes]


@router.get("/{shipment_id}/rates", response_model=List[RateQuoteResponse])
def list_rate_quotes(
    shipment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Return the persisted rate quotes for a shipment (read-only, no egress)."""
    shipment = (
        db.query(Shipment)
        .filter(
            Shipment.id == shipment_id,
            Shipment.company_id == company_id,
            Shipment.is_deleted == False,  # noqa: E712
        )
        .first()
    )
    if not shipment:
        raise HTTPException(status_code=404, detail="Shipment not found")
    quotes = (
        db.query(ShipmentRateQuote)
        .filter(ShipmentRateQuote.shipment_id == shipment_id, ShipmentRateQuote.company_id == company_id)
        .order_by(ShipmentRateQuote.amount)
        .all()
    )
    return [RateQuoteResponse.model_validate(q) for q in quotes]


@router.post("/{shipment_id}/buy-label", response_model=BuyLabelResponse)
async def buy_label(
    shipment_id: int,
    payload: BuyLabelRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(CARRIER_WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Purchase a parcel label (egress-gated, idempotent, audited)."""
    service = ShippingService(db, audit)
    try:
        shipment, already_purchased = await service.buy_label(
            company_id,
            shipment_id,
            payload.rate_id,
            current_user.id,
            carrier_account_id=payload.carrier_account_id,
        )
    except CarrierError as exc:
        raise _map_carrier_error(exc)
    return BuyLabelResponse(
        shipment_id=shipment.id,
        shipment_number=shipment.shipment_number,
        carrier=shipment.carrier,
        service_code=shipment.service_code,
        tracking_number=shipment.tracking_number,
        actual_cost=shipment.actual_cost,
        cost_currency=shipment.cost_currency,
        label_document_id=shipment.label_document_id,
        label_purchased_at=shipment.label_purchased_at,
        already_purchased=already_purchased,
    )


@router.post("/{shipment_id}/buy-bol", response_model=BuyBolResponse)
async def buy_bol(
    shipment_id: int,
    payload: BuyBolRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(CARRIER_WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Purchase an LTL Bill of Lading (egress-gated, idempotent, audited).

    EasyPost raises ``NotSupportedError`` (freight is the future Zenkraft
    adapter's job) -> mapped to HTTP 501.
    """
    service = ShippingService(db, audit)
    try:
        shipment, already_purchased = await service.buy_freight_bol(
            company_id,
            shipment_id,
            payload.rate_id,
            current_user.id,
            carrier_account_id=payload.carrier_account_id,
        )
    except CarrierError as exc:
        raise _map_carrier_error(exc)
    return BuyBolResponse(
        shipment_id=shipment.id,
        shipment_number=shipment.shipment_number,
        carrier=shipment.carrier,
        bol_number=shipment.bol_number,
        pro_number=shipment.pro_number,
        actual_cost=shipment.actual_cost,
        cost_currency=shipment.cost_currency,
        bol_document_id=shipment.bol_document_id,
        label_purchased_at=shipment.label_purchased_at,
        already_purchased=already_purchased,
    )


@router.post("/{shipment_id}/schedule-pickup", response_model=SchedulePickupResponse)
async def schedule_pickup(
    shipment_id: int,
    payload: SchedulePickupRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(CARRIER_WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
):
    """Schedule a carrier pickup for an already-purchased shipment (egress-gated)."""
    service = ShippingService(db)
    try:
        pickup = await service.schedule_pickup(
            company_id,
            shipment_id,
            pickup_date=payload.pickup_date,
            window_start=payload.window_start,
            window_end=payload.window_end,
            carrier_account_id=payload.carrier_account_id,
            user_id=current_user.id,
        )
    except CarrierError as exc:
        raise _map_carrier_error(exc)
    return SchedulePickupResponse(
        provider_pickup_id=pickup.provider_pickup_id,
        confirmation_number=pickup.confirmation_number,
        scheduled_date=pickup.scheduled_date,
        window_start=pickup.window_start,
        window_end=pickup.window_end,
        status=pickup.status,
    )


@router.post("/{shipment_id}/void-label", response_model=VoidRefundResponse)
async def void_label(
    shipment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(CARRIER_WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Void a purchased label (egress-gated, idempotent, audited as a CANCEL)."""
    service = ShippingService(db, audit)
    try:
        shipment = await service.void_label(company_id, shipment_id, current_user.id)
    except CarrierError as exc:
        raise _map_carrier_error(exc)
    return VoidRefundResponse(
        shipment_id=shipment.id,
        voided_at=shipment.voided_at,
        refund_status=shipment.refund_status,
        message="Label voided / refund requested",
    )


@router.post("/{shipment_id}/refund", response_model=VoidRefundResponse)
async def refund_label(
    shipment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(CARRIER_WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Request a refund for a purchased label (alias of void; same money-moving CANCEL)."""
    service = ShippingService(db, audit)
    try:
        shipment = await service.refund_label(company_id, shipment_id, current_user.id)
    except CarrierError as exc:
        raise _map_carrier_error(exc)
    return VoidRefundResponse(
        shipment_id=shipment.id,
        voided_at=shipment.voided_at,
        refund_status=shipment.refund_status,
        message="Refund requested",
    )


@router.get("/{shipment_id}/tracking", response_model=ShipmentTrackingResponse)
def get_tracking(
    shipment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Return the stored tracking status + event history for a shipment.

    Read-only and NOT egress-gated: it serves data already flowed back from
    inbound webhooks. Tenant-scoped.
    """
    shipment = (
        db.query(Shipment)
        .filter(
            Shipment.id == shipment_id,
            Shipment.company_id == company_id,
            Shipment.is_deleted == False,  # noqa: E712
        )
        .first()
    )
    if not shipment:
        raise HTTPException(status_code=404, detail="Shipment not found")

    events = (
        db.query(ShipmentTrackingEvent)
        .filter(
            ShipmentTrackingEvent.shipment_id == shipment_id,
            ShipmentTrackingEvent.company_id == company_id,
        )
        .order_by(ShipmentTrackingEvent.occurred_at.desc().nullslast(), ShipmentTrackingEvent.id.desc())
        .all()
    )
    return ShipmentTrackingResponse(
        shipment_id=shipment.id,
        shipment_number=shipment.shipment_number,
        tracking_number=shipment.tracking_number,
        tracking_status=shipment.tracking_status,
        tracking_status_detail=shipment.tracking_status_detail,
        last_tracking_sync_at=shipment.last_tracking_sync_at,
        actual_delivery=shipment.actual_delivery,
        events=[TrackingEventResponse.model_validate(e) for e in events],
    )
