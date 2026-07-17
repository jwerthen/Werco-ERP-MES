from datetime import date, datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.api.deps import (
    get_admin_user,
    get_audit_service,
    get_current_company_id,
    get_current_user,
    require_role,
)
from app.core.queue import enqueue_job_best_effort
from app.db.database import get_db
from app.db.locks import acquire_generator_lock
from app.models.inventory import (
    InventoryItem,
    InventoryLocation,
    InventoryTransaction,
    TransactionType,
)
from app.models.part import Part
from app.models.print_profile import CompanyPrintProfile
from app.models.purchasing import (
    DefectType,
    InspectionMethod,
    InspectionStatus,
    POReceipt,
    POStatus,
    PurchaseOrder,
    PurchaseOrderLine,
    ReceiptStatus,
)
from app.models.quality import (
    NCRDisposition,
    NCRSource,
    NCRStatus,
    NonConformanceReport,
)
from app.models.user import User, UserRole
from app.schemas.print_profile import (
    PrintLabelRequest,
    PrintLabelResponse,
    PrintProfileRead,
    PrintProfileUpdate,
)
from app.schemas.purchasing import (
    InspectionQueueItem,
    InspectionResultResponse,
    ReceiptCreate,
    ReceiptInspection,
    ReceiptResponse,
)
from app.services.audit_service import AuditService
from app.services.operational_event_service import OperationalEventService
from app.services.print_service import PrintEgressDisabledError, PrintService
from app.services.proxybox_client import ProxyBoxError

router = APIRouter()


def generate_receipt_number(db: Session, company_id: int) -> str:
    """Generate next receipt number (RCV-YYYYMMDD-XXX), scoped to the company.

    Holds an advisory lock so concurrent creates can't collide.
    """
    acquire_generator_lock(db, "receipt_number", company_id)

    today = datetime.now().strftime("%Y%m%d")
    prefix = f"RCV-{today}-"

    last = (
        db.query(POReceipt)
        .filter(
            POReceipt.company_id == company_id,
            POReceipt.receipt_number.like(f"{prefix}%"),
        )
        .order_by(POReceipt.receipt_number.desc())
        .first()
    )

    if last:
        last_num = int(last.receipt_number.split("-")[-1])
        new_num = last_num + 1
    else:
        new_num = 1

    return f"{prefix}{new_num:03d}"


def generate_ncr_number(db: Session, company_id: int) -> str:
    today = datetime.now().strftime("%Y%m%d")
    prefix = f"NCR-{today}-"

    last = (
        db.query(NonConformanceReport)
        .filter(
            NonConformanceReport.company_id == company_id,
            NonConformanceReport.ncr_number.like(f"{prefix}%"),
        )
        .order_by(NonConformanceReport.ncr_number.desc())
        .first()
    )

    if last:
        last_num = int(last.ncr_number.split("-")[-1])
        new_num = last_num + 1
    else:
        new_num = 1

    return f"{prefix}{new_num:03d}"


@router.get("/open-pos")
def get_open_purchase_orders(
    vendor_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """
    Get POs available for receiving (sent or partial status).

    Each PO in the payload carries ``order_date``, ``required_date``, and
    ``expected_date`` alongside its open (unclosed, quantity-remaining) lines.
    """
    query = (
        db.query(PurchaseOrder)
        .filter(PurchaseOrder.company_id == company_id)
        .options(
            joinedload(PurchaseOrder.vendor),
            joinedload(PurchaseOrder.lines).joinedload(PurchaseOrderLine.part),
        )
        .filter(PurchaseOrder.status.in_([POStatus.SENT, POStatus.PARTIAL]))
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
                lines_data.append(
                    {
                        "line_id": line.id,
                        "line_number": line.line_number,
                        "part_id": line.part_id,
                        "part_number": line.part.part_number if line.part else None,
                        "part_name": line.part.name if line.part else None,
                        "quantity_ordered": line.quantity_ordered,
                        "quantity_received": line.quantity_received,
                        "quantity_remaining": remaining,
                        "unit_price": line.unit_price,
                        "required_date": line.required_date,
                        # Part-master incoming-inspection flag — shown as an
                        # advisory hint next to the Receive form's checkbox
                        # (the checkbox itself always starts unchecked).
                        "requires_inspection": bool(line.part.requires_inspection) if line.part else False,
                    }
                )

        if lines_data:
            result.append(
                {
                    "po_id": po.id,
                    "po_number": po.po_number,
                    "vendor_id": po.vendor_id,
                    "vendor_name": po.vendor.name if po.vendor else None,
                    "vendor_code": po.vendor.code if po.vendor else None,
                    "order_date": po.order_date,
                    "required_date": po.required_date,
                    "expected_date": po.expected_date,
                    "status": po.status.value,
                    "lines": lines_data,
                    "total_lines": len(lines_data),
                }
            )

    return result


@router.get("/po/{po_id}")
def get_purchase_order_for_receiving(
    po_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get full PO details for receiving"""
    po = (
        db.query(PurchaseOrder)
        .options(
            joinedload(PurchaseOrder.vendor),
            joinedload(PurchaseOrder.lines).joinedload(PurchaseOrderLine.part),
            joinedload(PurchaseOrder.lines).joinedload(PurchaseOrderLine.receipts),
        )
        .filter(PurchaseOrder.id == po_id, PurchaseOrder.company_id == company_id)
        .first()
    )

    if not po:
        raise HTTPException(status_code=404, detail="Purchase order not found")

    lines_data = []
    for line in po.lines:
        receipts_data = []
        for r in line.receipts:
            receipts_data.append(
                {
                    "receipt_id": r.id,
                    "receipt_number": r.receipt_number,
                    "quantity_received": r.quantity_received,
                    "lot_number": r.lot_number,
                    "status": (r.status.value if hasattr(r.status, "value") else r.status),
                    "received_at": r.received_at,
                }
            )

        lines_data.append(
            {
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
                # Part-master incoming-inspection flag — shown as an advisory
                # hint next to the Receive form's checkbox (the checkbox itself
                # always starts unchecked).
                "requires_inspection": bool(line.part.requires_inspection) if line.part else False,
                "receipts": receipts_data,
            }
        )

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
        "lines": lines_data,
    }


@router.post("/receive", response_model=ReceiptResponse)
def receive_material(
    receipt_in: ReceiptCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """
    Receive material against a PO line.

    Creates the receipt record and updates PO quantities. ``lot_number`` is
    optional: when blank or omitted the server auto-assigns the receipt number
    as the lot, so every receipt still stores a non-null lot and AS9100D lot
    traceability is preserved. ``requires_inspection``
    defaults to **false** when omitted (owner-requested receiving default): the
    receipt is auto-accepted straight into inventory (dock-to-stock) and recorded
    with ``inspection_status = not_required``. No inspection is performed on this
    path, so ``inspection_method`` / ``inspected_by`` / ``inspected_at`` stay null —
    the record is never stamped ``passed`` for an inspection that did not happen
    (AS9100D records integrity). Pass ``true`` to hold the lot in the inspection
    queue, where it resolves to ``passed`` / ``failed`` / ``partial`` with a real
    inspector, method, and timestamp. The part master's ``Part.requires_inspection``
    flag is NOT applied automatically — it is exposed on the /receiving/open-pos and
    /receiving/po/{id} line payloads as an advisory hint the receiving UI shows next
    to the checkbox.
    """
    po_line = (
        db.query(PurchaseOrderLine)
        .options(
            joinedload(PurchaseOrderLine.purchase_order).joinedload(PurchaseOrder.vendor),
            joinedload(PurchaseOrderLine.part),
        )
        .filter(
            PurchaseOrderLine.id == receipt_in.po_line_id,
            PurchaseOrderLine.company_id == company_id,
        )
        .first()
    )

    if not po_line:
        raise HTTPException(status_code=404, detail="PO line not found")

    po = po_line.purchase_order
    if po.status not in [POStatus.SENT, POStatus.PARTIAL]:
        raise HTTPException(status_code=400, detail="PO must be in sent or partial status to receive")

    if po_line.is_closed:
        raise HTTPException(status_code=400, detail="PO line is already closed")

    # Lot number is optional at receiving; when blank it is auto-assigned from
    # the receipt number below, so AS9100D lot traceability is preserved.
    lot_number = (receipt_in.lot_number or "").strip()

    qty_received = float(receipt_in.quantity_received)
    # Check for over-receiving
    remaining = po_line.quantity_ordered - po_line.quantity_received
    if qty_received > remaining:
        if not receipt_in.over_receive_approved:
            raise HTTPException(
                status_code=400,
                detail=f"Quantity received ({qty_received}) exceeds remaining quantity ({remaining}). Set over_receive_approved=true to override.",
            )

    # Validate location if provided
    location = None
    if receipt_in.location_id:
        location = (
            db.query(InventoryLocation)
            .filter(
                InventoryLocation.id == receipt_in.location_id,
                InventoryLocation.company_id == company_id,
            )
            .first()
        )
        if not location:
            raise HTTPException(status_code=404, detail="Location not found")

    # Owner-requested receiving default: an omitted flag means "no inspection
    # required" (schema default False = dock-to-stock). No part-master deferral
    # here — the part flag is only an advisory hint in the receiving UI.
    requires_inspection = receipt_in.requires_inspection

    receipt_number = generate_receipt_number(db, company_id)
    # Auto-assign the lot from the (unique, company-scoped) receipt number when
    # the receiver left it blank — every receipt still gets a real lot value.
    if not lot_number:
        lot_number = receipt_number

    receipt = POReceipt(
        receipt_number=receipt_number,
        po_line_id=po_line.id,
        quantity_received=qty_received,
        lot_number=lot_number,
        serial_numbers=receipt_in.serial_numbers,
        heat_number=receipt_in.heat_number,
        cert_number=receipt_in.cert_number,
        coc_attached=receipt_in.coc_attached,
        location_id=receipt_in.location_id,
        requires_inspection=requires_inspection,
        status=(ReceiptStatus.PENDING_INSPECTION if requires_inspection else ReceiptStatus.ACCEPTED),
        # NOT_REQUIRED (not PASSED) for dock-to-stock: no incoming inspection was
        # performed, so the record must not assert a passed inspection.
        inspection_status=(InspectionStatus.PENDING if requires_inspection else InspectionStatus.NOT_REQUIRED),
        packing_slip_number=receipt_in.packing_slip_number,
        carrier=receipt_in.carrier,
        tracking_number=receipt_in.tracking_number,
        over_receive_approved=receipt_in.over_receive_approved,
        over_receive_approved_by=(current_user.id if receipt_in.over_receive_approved else None),
        received_by=current_user.id,
        notes=receipt_in.notes,
    )
    receipt.company_id = company_id
    db.add(receipt)
    db.flush()

    # Update PO line quantity received
    po_line.quantity_received += qty_received
    if po_line.quantity_received >= po_line.quantity_ordered:
        po_line.is_closed = True

    # Update PO status (capture old status before mutating for the audit trail)
    old_po_status = po.status
    all_lines = (
        db.query(PurchaseOrderLine)
        .filter(
            PurchaseOrderLine.purchase_order_id == po.id,
            PurchaseOrderLine.company_id == company_id,
        )
        .all()
    )
    all_closed = all(line.is_closed for line in all_lines)
    any_received = any(line.quantity_received > 0 for line in all_lines)

    if all_closed:
        po.status = POStatus.RECEIVED
    elif any_received:
        po.status = POStatus.PARTIAL

    # If not requiring inspection, auto-accept and add to inventory (dock-to-stock).
    if not requires_inspection:
        receipt.quantity_accepted = qty_received
        # Records integrity (AS9100D): no incoming inspection occurred, so DO NOT
        # stamp an inspection result/method/inspector/time. inspection_status stays
        # NOT_REQUIRED (set on construction) and inspection_method / inspected_by /
        # inspected_at stay NULL. The receiver + receipt time are already captured
        # by received_by / received_at. Fabricating a VISUAL inspection by the
        # receiver here was the records-integrity defect flagged on PR #127.
        location_code = location.code if location else "RECV-01"
        _add_to_inventory(
            db,
            company_id,
            po_line.part_id,
            qty_received,
            location_code,
            lot_number,
            po_line.unit_price,
            current_user.id,
            receipt_number,
            audit,
            po.vendor.name if po.vendor else None,
        )

    # Audit log (tamper-evident hash chain via the request-scoped AuditService)
    part_number = po_line.part.part_number if po_line.part else "N/A"
    audit.log_create(
        "receipt",
        receipt.id,
        receipt.receipt_number,
        new_values=receipt,
        description=(f"Received {qty_received} of part {part_number} on PO {po.po_number} lot {lot_number}"),
    )
    if po.status != old_po_status:
        audit.log_status_change(
            "purchase_order",
            po.id,
            po.po_number,
            old_po_status.value if hasattr(old_po_status, "value") else old_po_status,
            po.status.value if hasattr(po.status, "value") else po.status,
        )

    # Operational-event parity with the (now-removed) purchasing.py receive path so
    # existing AI/real-time consumers keep working. Emit before commit.
    OperationalEventService(db).emit_best_effort(
        company_id=company_id,
        event_type="purchase_order_received",
        source_module="purchasing",
        entity_type="po_receipt",
        entity_id=receipt.id,
        user_id=current_user.id,
        severity="info" if receipt.status == ReceiptStatus.ACCEPTED else "medium",
        event_payload={
            "receipt_number": receipt.receipt_number,
            "po_id": po.id,
            "po_number": po.po_number,
            "po_line_id": po_line.id,
            "part_id": po_line.part_id,
            "quantity_received": qty_received,
            "requires_inspection": receipt.requires_inspection,
            "status": (receipt.status.value if hasattr(receipt.status, "value") else receipt.status),
        },
    )

    db.commit()
    db.refresh(receipt)

    # Best-effort auto-print of the 4x6 receiving label. The job ITSELF decides
    # whether to print (gated on the per-company auto_print_on_receipt + egress
    # toggles), so this enqueue is unconditional and minimal. enqueue_job_best_effort
    # swallows any Redis/enqueue error so a printer/tunnel problem can NEVER fail or
    # block an already-committed receipt.
    enqueue_job_best_effort(
        "print_receiving_label_job",
        company_id=company_id,
        receipt_id=receipt.id,
        user_id=current_user.id,
    )

    return receipt


@router.get("/inspection-queue", response_model=List[InspectionQueueItem])
def get_inspection_queue(
    # Bounded so a negative value can't yield a future cutoff (empty queue) and
    # a huge value can't OverflowError timedelta into a 500.
    days_back: Optional[int] = Query(None, ge=1, le=3650),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get items pending inspection, sorted by date received (oldest first).

    By default there is NO date cutoff: a pending inspection must never age out
    of the queue (the /stats "pending_inspection" badge counts all of them, and
    the list must match). Pass ``days_back`` to explicitly narrow to recent
    receipts.
    """
    query = (
        db.query(POReceipt)
        .options(
            joinedload(POReceipt.po_line).joinedload(PurchaseOrderLine.part),
            joinedload(POReceipt.po_line).joinedload(PurchaseOrderLine.purchase_order).joinedload(PurchaseOrder.vendor),
            joinedload(POReceipt.location),
            joinedload(POReceipt.receiver),
        )
        .filter(
            POReceipt.company_id == company_id,
            POReceipt.status == ReceiptStatus.PENDING_INSPECTION,
        )
    )
    if days_back is not None:
        cutoff = datetime.utcnow() - timedelta(days=days_back)
        query = query.filter(POReceipt.received_at >= cutoff)

    receipts = query.order_by(POReceipt.received_at).all()

    result = []
    now = datetime.utcnow()
    for r in receipts:
        # Degrade per-row instead of 500ing the whole list: an orphaned receipt
        # (missing PO line / purchase order / part) still shows up with None
        # context fields so it can be found and fixed — skip nothing.
        po_line = r.po_line
        po = po_line.purchase_order if po_line else None
        part = po_line.part if po_line else None
        days_pending = (now - r.received_at).days if r.received_at else 0
        result.append(
            InspectionQueueItem(
                receipt_id=r.id,
                receipt_number=r.receipt_number,
                po_number=po.po_number if po else None,
                po_id=po.id if po else None,
                vendor_name=po.vendor.name if po and po.vendor else None,
                part_id=po_line.part_id if po_line else None,
                part_number=part.part_number if part else None,
                part_name=part.name if part else None,
                quantity_received=r.quantity_received,
                lot_number=r.lot_number,
                cert_number=r.cert_number,
                coc_attached=r.coc_attached,
                received_at=r.received_at,
                received_by_name=r.receiver.full_name if r.receiver else None,
                location_code=r.location.code if r.location else None,
                days_pending=days_pending,
            )
        )

    return result


@router.get("/receipt/{receipt_id}")
def get_receipt_detail(
    receipt_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get full receipt details for inspection"""
    receipt = (
        db.query(POReceipt)
        .options(
            joinedload(POReceipt.po_line).joinedload(PurchaseOrderLine.part),
            joinedload(POReceipt.po_line).joinedload(PurchaseOrderLine.purchase_order).joinedload(PurchaseOrder.vendor),
            joinedload(POReceipt.location),
            joinedload(POReceipt.receiver),
            joinedload(POReceipt.inspector),
        )
        .filter(POReceipt.id == receipt_id, POReceipt.company_id == company_id)
        .first()
    )

    if not receipt:
        raise HTTPException(status_code=404, detail="Receipt not found")

    # Degrade instead of 500ing: an orphaned receipt (missing PO line /
    # purchase order / part) returns None context fields — same posture as the
    # hardened inspection queue, which deliberately surfaces such rows.
    po_line = receipt.po_line
    po = po_line.purchase_order if po_line else None
    part = po_line.part if po_line else None
    vendor = po.vendor if po else None

    return {
        "receipt_id": receipt.id,
        "receipt_number": receipt.receipt_number,
        "po_number": po.po_number if po else None,
        "po_id": po.id if po else None,
        "vendor_name": vendor.name if vendor else None,
        "vendor_code": vendor.code if vendor else None,
        "is_approved_vendor": vendor.is_approved if vendor else False,
        "part_id": po_line.part_id if po_line else None,
        "part_number": part.part_number if part else None,
        "part_name": part.name if part else None,
        "part_description": part.description if part else None,
        "quantity_received": receipt.quantity_received,
        "quantity_accepted": receipt.quantity_accepted,
        "quantity_rejected": receipt.quantity_rejected,
        "lot_number": receipt.lot_number,
        "serial_numbers": receipt.serial_numbers,
        "heat_number": receipt.heat_number,
        "cert_number": receipt.cert_number,
        "coc_attached": receipt.coc_attached,
        "status": (receipt.status.value if hasattr(receipt.status, "value") else receipt.status),
        "inspection_status": (
            receipt.inspection_status.value
            if hasattr(receipt.inspection_status, "value")
            else receipt.inspection_status
        ),
        "inspection_method": (receipt.inspection_method.value if receipt.inspection_method else None),
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
        "notes": receipt.notes,
    }


@router.post("/inspect/{receipt_id}", response_model=InspectionResultResponse)
def inspect_receipt(
    receipt_id: int,
    inspection: ReceiptInspection,
    db: Session = Depends(get_db),
    # SUPERVISOR added deliberately (owner-approved): the same roles that can
    # receive material may complete incoming inspection, plus QUALITY.
    current_user: User = Depends(
        require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY, UserRole.SUPERVISOR])
    ),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """
    Complete inspection of a receipt.
    - If all accepted: Pass → Add to inventory
    - If partial: Partial → Add accepted to inventory, create NCR for rejected
    - If all rejected: Fail → Create NCR only
    """
    receipt = (
        db.query(POReceipt)
        .options(
            joinedload(POReceipt.po_line).joinedload(PurchaseOrderLine.part),
            joinedload(POReceipt.po_line).joinedload(PurchaseOrderLine.purchase_order).joinedload(PurchaseOrder.vendor),
            joinedload(POReceipt.location),
        )
        .filter(POReceipt.id == receipt_id, POReceipt.company_id == company_id)
        .first()
    )

    if not receipt:
        raise HTTPException(status_code=404, detail="Receipt not found")

    if receipt.status != ReceiptStatus.PENDING_INSPECTION:
        raise HTTPException(status_code=400, detail="Receipt is not pending inspection")

    # An orphaned receipt (dangling PO line) cannot complete inspection — there
    # is no part/price context to post accepted material into inventory. Fail
    # with a clear 400 instead of a 500; the hardened inspection queue
    # deliberately surfaces such rows so they can be found and repaired.
    if receipt.po_line is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "Receipt's PO line no longer exists, so this inspection cannot be completed. "
                "Contact an administrator to repair the receipt record."
            ),
        )

    # Quantities arrive as Decimal (Pydantic MoneySmall); the inventory/Float columns
    # below work in float, so normalize once here (mirrors the receive path which does
    # the same with float(receipt_in.quantity_received)).
    qty_accepted = float(inspection.quantity_accepted)
    qty_rejected = float(inspection.quantity_rejected)

    # Validate quantities
    total = qty_accepted + qty_rejected
    if total > receipt.quantity_received:
        raise HTTPException(
            status_code=400,
            detail=f"Accepted ({qty_accepted}) + rejected ({qty_rejected}) = {total} exceeds received ({receipt.quantity_received})",
        )

    # Validation: if rejected > 0, defect_type and notes are required
    if qty_rejected > 0:
        if not inspection.defect_type:
            raise HTTPException(
                status_code=400,
                detail="Defect type is required when rejecting material",
            )
        if not inspection.inspection_notes:
            raise HTTPException(
                status_code=400,
                detail="Inspection notes are required when rejecting material",
            )

    # Update receipt (capture old status before mutating for the audit trail)
    old_status = receipt.status
    receipt.quantity_accepted = qty_accepted
    receipt.quantity_rejected = qty_rejected
    receipt.inspection_method = InspectionMethod(inspection.inspection_method)
    receipt.defect_type = DefectType(inspection.defect_type) if inspection.defect_type else None
    receipt.inspection_notes = inspection.inspection_notes
    receipt.inspected_by = current_user.id
    receipt.inspected_at = datetime.utcnow()

    # Determine inspection result
    if qty_accepted == receipt.quantity_received:
        receipt.inspection_status = InspectionStatus.PASSED
        receipt.status = ReceiptStatus.ACCEPTED
    elif qty_rejected == receipt.quantity_received:
        receipt.inspection_status = InspectionStatus.FAILED
        receipt.status = ReceiptStatus.REJECTED
    else:
        receipt.inspection_status = InspectionStatus.PARTIAL
        receipt.status = ReceiptStatus.ACCEPTED  # Partial acceptance

    result = InspectionResultResponse(
        receipt=ReceiptResponse.model_validate(receipt),
        inventory_created=False,
        ncr_created=False,
    )

    # The PO line is guaranteed above; the purchase order / part may still be
    # missing on an orphaned row — degrade those to None context.
    po = receipt.po_line.purchase_order
    part = receipt.po_line.part
    vendor_name = po.vendor.name if po and po.vendor else None

    # Add accepted quantity to inventory
    if qty_accepted > 0:
        location_code = receipt.location.code if receipt.location else "RECV-01"
        inv_item = _add_to_inventory(
            db,
            company_id,
            receipt.po_line.part_id,
            qty_accepted,
            location_code,
            receipt.lot_number,
            receipt.po_line.unit_price,
            current_user.id,
            receipt.receipt_number,
            audit,
            vendor_name,
        )
        result.inventory_created = True
        result.inventory_item_id = inv_item.id if inv_item else None

    # Create NCR for rejected quantity
    if qty_rejected > 0:
        ncr = _create_ncr_for_rejection(
            db,
            company_id,
            receipt,
            inspection,
            current_user,
            vendor_name,
            po.po_number if po else None,
            part,
            audit,
        )
        result.ncr_created = True
        result.ncr_number = ncr.ncr_number
        result.ncr_id = ncr.id

    # Audit log (tamper-evident hash chain via the request-scoped AuditService)
    audit.log_status_change(
        "receipt",
        receipt.id,
        receipt.receipt_number,
        old_status.value if hasattr(old_status, "value") else old_status,
        receipt.status.value if hasattr(receipt.status, "value") else receipt.status,
        description=(
            f"Inspected receipt {receipt.receipt_number}: {inspection.quantity_accepted} accepted, "
            f"{inspection.quantity_rejected} rejected. Method: {inspection.inspection_method}"
        ),
    )

    # Operational-event parity with the (now-removed) purchasing.py inspect path so
    # existing AI/real-time consumers keep working. Emit before commit.
    OperationalEventService(db).emit_best_effort(
        company_id=company_id,
        event_type="purchase_receipt_inspected",
        source_module="purchasing",
        entity_type="po_receipt",
        entity_id=receipt.id,
        user_id=current_user.id,
        severity="high" if inspection.quantity_rejected > 0 else "info",
        event_payload={
            "receipt_number": receipt.receipt_number,
            "po_line_id": receipt.po_line_id,
            "part_id": receipt.po_line.part_id if receipt.po_line else None,
            "quantity_received": float(receipt.quantity_received),
            "quantity_accepted": float(inspection.quantity_accepted),
            "quantity_rejected": float(inspection.quantity_rejected),
            "status": (receipt.status.value if hasattr(receipt.status, "value") else receipt.status),
            "inspection_method": inspection.inspection_method,
            "defect_type": inspection.defect_type,
        },
    )

    db.commit()
    db.refresh(receipt)
    result.receipt = ReceiptResponse.model_validate(receipt)

    return result


def _add_to_inventory(
    db: Session,
    company_id: int,
    part_id: int,
    quantity: float,
    location: str,
    lot_number: str,
    unit_cost: float,
    user_id: int,
    reference: str,
    audit: AuditService,
    supplier_name: Optional[str] = None,
) -> InventoryItem:
    """Add received material to inventory with full traceability"""
    # Check for existing inventory at location with same lot (scoped to the company)
    existing = (
        db.query(InventoryItem)
        .filter(
            InventoryItem.company_id == company_id,
            InventoryItem.part_id == part_id,
            InventoryItem.location == location,
            InventoryItem.lot_number == lot_number,
        )
        .first()
    )

    if existing:
        old_values = {
            "quantity_on_hand": existing.quantity_on_hand,
            "quantity_available": existing.quantity_available,
        }
        existing.quantity_on_hand += quantity
        existing.quantity_available = existing.quantity_on_hand - existing.quantity_allocated
        inv_item = existing
        audit.log_update(
            "inventory",
            inv_item.id,
            f"{part_id}/{lot_number}@{location}",
            old_values=old_values,
            new_values={
                "quantity_on_hand": inv_item.quantity_on_hand,
                "quantity_available": inv_item.quantity_available,
            },
            description=(
                f"Added {quantity} to inventory part {part_id} lot {lot_number} at {location} via {reference}"
            ),
        )
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
            is_active=True,
        )
        inv_item.company_id = company_id
        db.add(inv_item)
        db.flush()
        audit.log_create(
            "inventory",
            inv_item.id,
            f"{part_id}/{lot_number}@{location}",
            new_values=inv_item,
            description=(f"Received {quantity} of part {part_id} lot {lot_number} at {location} via {reference}"),
        )

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
        created_by=user_id,
    )
    txn.company_id = company_id
    db.add(txn)

    return inv_item


def _create_ncr_for_rejection(
    db: Session,
    company_id: int,
    receipt: POReceipt,
    inspection: ReceiptInspection,
    current_user: User,
    supplier_name: Optional[str],
    po_number: Optional[str],
    part: Optional[Part],
    audit: AuditService,
) -> NonConformanceReport:
    """Create NCR in draft status for rejected material"""
    ncr_number = generate_ncr_number(db, company_id)

    defect_descriptions = {
        "dimensional": "Material does not meet dimensional specifications",
        "cosmetic": "Visual/surface defects found",
        "material": "Material composition or properties non-conforming",
        "documentation": "Missing or incorrect documentation",
        "functional": "Functional testing failed",
        "contamination": "Material contamination detected",
        "packaging": "Packaging damage or contamination",
        "other": "Other non-conformance",
    }

    defect_type = inspection.defect_type or "other"
    description = f"{defect_descriptions.get(defect_type, 'Non-conformance detected')}\n\nInspector Notes: {inspection.inspection_notes or 'N/A'}"

    ncr = NonConformanceReport(
        ncr_number=ncr_number,
        part_id=receipt.po_line.part_id,
        receipt_id=receipt.id,
        lot_number=receipt.lot_number,
        quantity_affected=float(inspection.quantity_rejected),
        quantity_rejected=float(inspection.quantity_rejected),
        source=NCRSource.INCOMING_INSPECTION,
        status=NCRStatus.OPEN,  # Draft - needs QA review
        disposition=NCRDisposition.PENDING,
        title=f"Incoming Inspection Rejection - {part.part_number if part else 'N/A'}",
        description=description,
        specification=f"Receipt: {receipt.receipt_number}, Method: {inspection.inspection_method}",
        supplier_name=supplier_name,
        supplier_lot=receipt.lot_number,
        po_number=po_number,
        detected_by=current_user.id,
        detected_date=date.today(),
    )
    ncr.company_id = company_id
    db.add(ncr)
    db.flush()

    # Audit log for NCR creation (tamper-evident hash chain via the request-scoped AuditService)
    audit.log_create(
        "ncr",
        ncr.id,
        ncr.ncr_number,
        new_values=ncr,
        description=(
            f"Auto-created NCR {ncr_number} for incoming inspection rejection on receipt {receipt.receipt_number}"
        ),
    )

    return ncr


@router.get("/history")
def get_receiving_history(
    days: int = 30,
    status: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get receiving history with inspection results"""
    cutoff = datetime.utcnow() - timedelta(days=days)

    query = (
        db.query(POReceipt)
        .options(
            joinedload(POReceipt.po_line).joinedload(PurchaseOrderLine.part),
            joinedload(POReceipt.po_line).joinedload(PurchaseOrderLine.purchase_order).joinedload(PurchaseOrder.vendor),
            joinedload(POReceipt.receiver),
            joinedload(POReceipt.inspector),
        )
        .filter(POReceipt.company_id == company_id, POReceipt.received_at >= cutoff)
    )

    if status:
        query = query.filter(POReceipt.status == ReceiptStatus(status))

    receipts = query.order_by(POReceipt.received_at.desc()).limit(200).all()

    result = []
    for r in receipts:
        # Degrade per-row instead of 500ing the whole list — same posture as
        # the hardened inspection queue: an orphaned receipt (missing PO line /
        # purchase order / part) still shows with None context fields.
        po_line = r.po_line
        po = po_line.purchase_order if po_line else None
        part = po_line.part if po_line else None
        result.append(
            {
                "receipt_id": r.id,
                "receipt_number": r.receipt_number,
                "po_number": po.po_number if po else None,
                "vendor_name": po.vendor.name if po and po.vendor else None,
                "part_number": part.part_number if part else None,
                "part_name": part.name if part else None,
                "quantity_received": r.quantity_received,
                "quantity_accepted": r.quantity_accepted,
                "quantity_rejected": r.quantity_rejected,
                "lot_number": r.lot_number,
                "status": r.status.value if hasattr(r.status, "value") else r.status,
                "inspection_status": (
                    r.inspection_status.value if hasattr(r.inspection_status, "value") else r.inspection_status
                ),
                "inspection_method": (r.inspection_method.value if r.inspection_method else None),
                "defect_type": r.defect_type.value if r.defect_type else None,
                "received_at": r.received_at,
                "received_by_name": r.receiver.full_name if r.receiver else None,
                "inspected_at": r.inspected_at,
                "inspected_by_name": r.inspector.full_name if r.inspector else None,
            }
        )

    return result


@router.get("/stats")
def get_receiving_stats(
    days: int = 30,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get receiving statistics for dashboard"""
    cutoff = datetime.utcnow() - timedelta(days=days)

    # Count pending inspections
    pending_count = (
        db.query(func.count(POReceipt.id))
        .filter(
            POReceipt.company_id == company_id,
            POReceipt.status == ReceiptStatus.PENDING_INSPECTION,
        )
        .scalar()
    )

    # Count received in period
    received_count = (
        db.query(func.count(POReceipt.id))
        .filter(POReceipt.company_id == company_id, POReceipt.received_at >= cutoff)
        .scalar()
    )

    # Count inspected in period
    inspected_count = (
        db.query(func.count(POReceipt.id))
        .filter(
            POReceipt.company_id == company_id,
            POReceipt.inspected_at >= cutoff,
            POReceipt.status != ReceiptStatus.PENDING_INSPECTION,
        )
        .scalar()
    )

    # Count rejections
    rejection_count = (
        db.query(func.count(POReceipt.id))
        .filter(
            POReceipt.company_id == company_id,
            POReceipt.inspected_at >= cutoff,
            POReceipt.quantity_rejected > 0,
        )
        .scalar()
    )

    # Total quantities
    qty_stats = (
        db.query(
            func.sum(POReceipt.quantity_received).label("total_received"),
            func.sum(POReceipt.quantity_accepted).label("total_accepted"),
            func.sum(POReceipt.quantity_rejected).label("total_rejected"),
        )
        .filter(POReceipt.company_id == company_id, POReceipt.received_at >= cutoff)
        .first()
    )

    return {
        "pending_inspection": pending_count or 0,
        "receipts_in_period": received_count or 0,
        "inspected_in_period": inspected_count or 0,
        "rejections_in_period": rejection_count or 0,
        "total_qty_received": qty_stats.total_received or 0 if qty_stats else 0,
        "total_qty_accepted": qty_stats.total_accepted or 0 if qty_stats else 0,
        "total_qty_rejected": qty_stats.total_rejected or 0 if qty_stats else 0,
        "acceptance_rate": round(
            (
                (qty_stats.total_accepted / qty_stats.total_received * 100)
                if qty_stats and qty_stats.total_received
                else 100
            ),
            1,
        ),
    }


@router.get("/locations")
def get_receiving_locations(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get available locations for receiving"""
    locations = (
        db.query(InventoryLocation)
        .filter(
            InventoryLocation.company_id == company_id,
            InventoryLocation.is_active == True,
            InventoryLocation.is_receivable == True,
        )
        .order_by(InventoryLocation.code)
        .all()
    )

    return [{"id": loc.id, "code": loc.code, "name": loc.name} for loc in locations]


# ===========================================================================
# Thermal receiving-label printing (ProxyBox / WHTP203e).
#
# The router stays THIN: it delegates to ``PrintService`` (the egress kill switch,
# the ProxyBox round-trip, Document persistence, and audit all live there). The
# manual reprint route is ``async def`` so it can await the print and return a real
# success/failure. The per-company profile routes are admin-only so the printer can
# actually be configured (there is otherwise no way to enter the API key).
# ===========================================================================


def _print_profile_to_read(profile: CompanyPrintProfile) -> PrintProfileRead:
    """Safe read shape -- the plaintext key is never exposed (last4 only)."""
    return PrintProfileRead(
        id=profile.id,
        proxybox_base_url=profile.proxybox_base_url,
        proxybox_target=profile.proxybox_target,
        api_key_last4=profile.api_key_last4,
        has_api_key=bool(profile.encrypted_api_key),
        default_paper_size=profile.default_paper_size,
        default_copies=profile.default_copies,
        auto_print_on_receipt=bool(profile.auto_print_on_receipt),
        allow_print_egress=bool(profile.allow_print_egress),
        is_active=bool(profile.is_active),
        created_at=profile.created_at,
    )


@router.post("/receipt/{receipt_id}/print-label", response_model=PrintLabelResponse)
async def print_receiving_label(
    receipt_id: int,
    payload: Optional[PrintLabelRequest] = None,
    db: Session = Depends(get_db),
    # Same role gate as receive_material (ADMIN / MANAGER / SUPERVISOR).
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Manually (re)print the 4x6 receiving label for a receipt.

    Egress-gated (409 when ``allow_print_egress`` is OFF), tenant-scoped (404 for a
    missing/cross-tenant receipt), and audited. A ProxyBox/printer failure maps to
    502 -- the rendered label Document is still persisted, so a later reprint works.
    """
    copies = payload.copies if payload else None
    service = PrintService(db, audit)
    try:
        document, printed = await service.print_receipt_label(company_id, receipt_id, current_user.id, copies=copies)
    except PrintEgressDisabledError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except LookupError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Receipt not found")
    except ProxyBoxError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))

    # Resolve the receipt number for the response (tenant-scoped, lightweight).
    receipt = db.query(POReceipt).filter(POReceipt.id == receipt_id, POReceipt.company_id == company_id).first()
    return PrintLabelResponse(
        receipt_id=receipt_id,
        receipt_number=receipt.receipt_number if receipt else None,
        label_document_id=document.id,
        printed=printed,
        message="Label sent to printer" if printed else "Label generated",
    )


@router.get("/print-profile", response_model=PrintProfileRead)
def get_print_profile(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_admin_user),
    company_id: int = Depends(get_current_company_id),
):
    """Return the company print profile (admin-only; 404 until one is created)."""
    profile = db.query(CompanyPrintProfile).filter(CompanyPrintProfile.company_id == company_id).first()
    if not profile:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Print profile not configured")
    return _print_profile_to_read(profile)


@router.put("/print-profile", response_model=PrintProfileRead)
def upsert_print_profile(
    data: PrintProfileUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_admin_user),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Create or update the company print profile (admin-only).

    SAFETY: ``allow_print_egress`` is the outbound-egress kill switch. It is created
    OFF (model + this upsert default) and only flips when an admin sets it here; the
    toggle is audited as a status change so enabling/disabling print egress is on the
    tamper-evident trail. ``api_key`` is write-only: encrypted at rest, never returned.
    """
    profile = db.query(CompanyPrintProfile).filter(CompanyPrintProfile.company_id == company_id).first()
    update = data.model_dump(exclude_unset=True)
    is_create = profile is None
    previous_egress: Optional[bool] = None
    api_key_rotated = False

    if profile is None:
        profile = CompanyPrintProfile(allow_print_egress=False)
        profile.company_id = company_id
        profile.created_by = current_user.id
        db.add(profile)
    else:
        previous_egress = profile.allow_print_egress

    # api_key is write-only -- rotate via the model helper, never store/return raw.
    if "api_key" in update:
        api_key_value = update.pop("api_key")
        if api_key_value:
            profile.set_api_key(api_key_value)
            api_key_rotated = True

    for field, value in update.items():
        # Don't null out the kill switch / toggles when omitted (exclude_unset
        # already drops omitted fields; this guards an explicit null).
        if value is None and field in ("allow_print_egress", "auto_print_on_receipt", "is_active"):
            continue
        setattr(profile, field, value)

    db.flush()

    if is_create:
        audit.log_create(
            resource_type="company_print_profile",
            resource_id=profile.id,
            resource_identifier=f"company:{company_id}",
            new_values={
                "allow_print_egress": profile.allow_print_egress,
                "auto_print_on_receipt": profile.auto_print_on_receipt,
                "proxybox_target": profile.proxybox_target,
            },
            description="Company print profile created",
            extra_data={"api_key_rotated": api_key_rotated},
        )
    else:
        audit.log_update(
            resource_type="company_print_profile",
            resource_id=profile.id,
            resource_identifier=f"company:{company_id}",
            old_values={"allow_print_egress": previous_egress},
            new_values={"allow_print_egress": profile.allow_print_egress},
            description="Company print profile updated",
            extra_data={"api_key_rotated": api_key_rotated},
        )

    # Flipping the print-egress kill switch is a security-relevant status change --
    # record it explicitly on the tamper-evident trail whenever it actually changed.
    if previous_egress is not None and previous_egress != profile.allow_print_egress:
        audit.log_status_change(
            "company_print_profile",
            profile.id,
            f"company:{company_id}",
            "egress_enabled" if previous_egress else "egress_disabled",
            "egress_enabled" if profile.allow_print_egress else "egress_disabled",
            description=(
                "Label-print egress "
                f"{'ENABLED' if profile.allow_print_egress else 'DISABLED'} for company {company_id}"
            ),
        )

    db.commit()
    db.refresh(profile)
    return _print_profile_to_read(profile)
