"""Canonical commit-free receiving, stock and numbering commands."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session, joinedload

from app.db.locks import acquire_generator_lock
from app.models.inventory import InventoryItem, InventoryLocation, InventoryTransaction, TransactionType
from app.models.purchasing import InspectionStatus, POReceipt, POStatus, PurchaseOrder, PurchaseOrderLine, ReceiptStatus
from app.services.audit_service import AuditService
from app.services.operational_event_service import OperationalEventService
from app.services.owned_property_service import (
    COMPANY_OWNER,
    ResolvedOwner,
    ownership_label,
    ownership_match_clauses,
    resolve_owner,
)


def blank_to_none(value: Optional[str]) -> Optional[str]:
    """A traceability string as a stock row stores it: stripped, and ``None`` when blank."""
    if value is None:
        return None
    stripped = str(value).strip()
    return stripped or None


@dataclass(frozen=True)
class StockTraceability:
    """The mill heat, cert number and supplier a PO receipt hands to the stock row it creates.

    The receipt is the record of what arrived (``po_receipts.heat_number`` / ``cert_number``,
    the PO's vendor); before this the stock row it created carried only the lot, cost and
    receipt number, so the heat and cert stopped at the dock. ``supplier_id`` is the PO's
    vendor id even when that vendor is soft-deleted: the supplier of record is history
    (invariant 3's records-vs-selection exception), not a selection.
    """

    heat_lot: Optional[str] = None
    cert_number: Optional[str] = None
    supplier_id: Optional[int] = None

    def stock_columns(self) -> Dict[str, Any]:
        return {"heat_lot": self.heat_lot, "cert_number": self.cert_number, "supplier_id": self.supplier_id}


NO_TRACEABILITY = StockTraceability()


def receipt_traceability(receipt: POReceipt, po: Optional[PurchaseOrder]) -> StockTraceability:
    """The traceability a receipt carries to stock: its heat and cert, and the PO's vendor."""
    return StockTraceability(
        heat_lot=blank_to_none(receipt.heat_number),
        cert_number=blank_to_none(receipt.cert_number),
        supplier_id=po.vendor_id if po is not None else None,
    )


def resolve_receipt_owner(db: Session, company_id: int, receipt_in) -> ResolvedOwner:
    """The owner a receipt line names, resolved in this company. Raises before any write.

    422 for a kind/owner mismatch; 404 for an owner that is not a live customer / non-deleted
    vendor of this company. A company line (every line before migration 129, and every Hank
    line, whose schema carries no ownership) costs no query. The owner is recorded on the
    receipt (``po_receipts.ownership`` / ``owner_*``), so a line held for inspection carries it
    to stock when ``inspect_receipt`` accepts it or ``clear_receipt_inspection`` releases it.
    """
    return resolve_owner(
        db,
        company_id,
        getattr(receipt_in, "ownership", None) or COMPANY_OWNER.ownership,
        getattr(receipt_in, "owner_customer_id", None),
        getattr(receipt_in, "owner_vendor_id", None),
    )


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


def _receive_material(receipt_in, db, current_user, company_id, audit, *, checked_certificate_ids=None):
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
    # Same lock order for a single receipt and a delivery: number generator,
    # PO header, then stock rows. Locks live until the caller commits the whole unit.
    acquire_generator_lock(db, "receipt_number", company_id)
    line_po = (
        db.query(PurchaseOrderLine.purchase_order_id)
        .filter(PurchaseOrderLine.id == receipt_in.po_line_id, PurchaseOrderLine.company_id == company_id)
        .first()
    )
    if not line_po:
        raise HTTPException(404, "PO line not found")
    locked_po = (
        db.query(PurchaseOrder)
        .filter(
            PurchaseOrder.id == line_po[0],
            PurchaseOrder.company_id == company_id,
            PurchaseOrder.is_deleted.is_(False),
        )
        .with_for_update()
        .populate_existing()
        .first()
    )
    if not locked_po:
        raise HTTPException(404, "Purchase order not found")
    po_line = (
        db.query(PurchaseOrderLine)
        .options(
            # Vendor deliberately unfiltered on soft delete -- see module docstring.
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
    certificate_id = receipt_in.certificate_document_id
    if checked_certificate_ids is None or (certificate_id and certificate_id not in checked_certificate_ids):
        # A delivery passes the certificates it locked and checked before its first line wrote the
        # audit row that takes the chain lock (receiving_delivery_service._lock_delivery); only those
        # skip the check here.
        from app.services.receiving_delivery_service import validate_certificate

        validate_certificate(db, company_id, po_line, certificate_id)
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

    # Who owns the material (AS9100D 8.5.3), resolved in this company before the receipt
    # row -- the first write -- exists.
    owner = resolve_receipt_owner(db, company_id, receipt_in)

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
        coc_attached=bool(receipt_in.certificate_document_id) or receipt_in.coc_attached,
        certificate_document_id=receipt_in.certificate_document_id,
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
        # Who owns the material (AS9100D 8.5.3) -- recorded on the receipt so an inspection
        # accept or a cleared hold later places it on a stock row of the same owner.
        **owner.columns(),
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
            owner=owner,
            traceability=receipt_traceability(receipt, po),
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
            # Snapshot the received line for email; delayed workers must not read
            # a subsequently edited part master or cumulative PO quantities.
            "part_number": po_line.part.part_number if po_line.part else None,
            "part_name": po_line.part.name if po_line.part else None,
            "unit_of_measure": (
                getattr(po_line.part.unit_of_measure, "value", po_line.part.unit_of_measure) if po_line.part else None
            ),
            "lot_number": receipt.lot_number,
            "quantity_received": qty_received,
            "requires_inspection": receipt.requires_inspection,
            "status": (receipt.status.value if hasattr(receipt.status, "value") else receipt.status),
        },
    )

    db.flush()
    return receipt


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
    reason_code: Optional[str] = None,
    notes: Optional[str] = None,
    *,
    owner: ResolvedOwner = COMPANY_OWNER,
    traceability: StockTraceability = NO_TRACEABILITY,
) -> InventoryItem:
    """Add received material to inventory with full traceability.

    ``owner`` (already resolved in this company) is part of the merge key: the material
    is added into an existing row only when that row has the same part, location, lot
    AND owner, and a new row carries it (AS9100D 8.5.3). Every caller passes the owner the
    receipt records (``po_receipts.ownership``): dock-to-stock here, and the inspection accept
    (``inspect_receipt``) and the cleared hold (``clear_receipt_inspection``) through
    ``owned_property_service.recorded_owner``. It defaults to company.

    ``reason_code`` / ``notes`` are optional overrides for the RECEIVE ledger row.
    Both default to None so the two original callers (receive_material's
    dock-to-stock leg and inspect_receipt) are byte-identical to before: no
    reason_code, and the standard "Received from {vendor} via {reference}" note.
    clear_receipt_inspection passes them so the movement itself says WHY the
    material entered stock -- the Stock Movements tab is the only place the ledger
    is rendered, and an unstamped row there is indistinguishable from a normal
    receive. Same stamping the correct/void reconciler does with RECEIPT_VOID /
    RECEIPT_CORRECTION.

    ``traceability`` (``receipt_traceability``) puts the receipt's heat, cert and the PO's
    vendor on the stock row. A NEW row takes all three. When the material merges into an
    EXISTING row, the first value wins: a field the row already carries is never
    overwritten, and only a blank one is filled (and the fill rides the merge's audit row).
    Every PO-receipt caller passes it -- dock-to-stock, the inspection accept and the
    cleared hold. The default is no traceability, so any other caller is unchanged.
    """
    # Check for existing inventory at location with same lot and owner (scoped to the company)
    existing = (
        db.query(InventoryItem)
        .filter(
            InventoryItem.company_id == company_id,
            InventoryItem.part_id == part_id,
            InventoryItem.location == location,
            InventoryItem.lot_number == lot_number,
            *ownership_match_clauses(owner.key),
        )
        .with_for_update()
        .first()
    )
    # Company stock keeps its pre-129 audit wording; owned property says whose it is.
    owned_suffix = "" if owner.is_company else f" as {ownership_label(owner.key, owner.owner_name)} property"

    if existing:
        old_values: Dict[str, Any] = {
            "quantity_on_hand": existing.quantity_on_hand,
            "quantity_available": existing.quantity_available,
        }
        existing.quantity_on_hand += quantity
        existing.quantity_available = existing.quantity_on_hand - existing.quantity_allocated
        new_values: Dict[str, Any] = {
            "quantity_on_hand": existing.quantity_on_hand,
            "quantity_available": existing.quantity_available,
        }
        # First value wins on a merge: fill only what the row is missing.
        for column, value in traceability.stock_columns().items():
            current = getattr(existing, column)
            if value is not None and (current is None or (isinstance(current, str) and not current.strip())):
                old_values[column] = current
                setattr(existing, column, value)
                new_values[column] = value
        inv_item = existing
        audit.log_update(
            "inventory",
            inv_item.id,
            f"{part_id}/{lot_number}@{location}",
            old_values=old_values,
            new_values=new_values,
            description=(
                f"Added {quantity} to inventory part {part_id} lot {lot_number} at {location} via {reference}"
                f"{owned_suffix}"
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
            **traceability.stock_columns(),
            **owner.columns(),
        )
        inv_item.company_id = company_id
        db.add(inv_item)
        db.flush()
        audit.log_create(
            "inventory",
            inv_item.id,
            f"{part_id}/{lot_number}@{location}",
            new_values=inv_item,
            description=(
                f"Received {quantity} of part {part_id} lot {lot_number} at {location} via {reference}{owned_suffix}"
            ),
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
        reason_code=reason_code,
        notes=(notes or f"Received from {supplier_name or 'vendor'} via {reference}"),
        created_by=user_id,
    )
    txn.company_id = company_id
    db.add(txn)

    return inv_item
