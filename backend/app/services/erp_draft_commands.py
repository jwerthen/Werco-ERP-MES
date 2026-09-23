"""Commit-free existing draft/attachment commands shared by ERP routes and Hank.

Callers own authorization and the sole transaction commit. Every source lookup
retains the existing tenant and business-state guards.
"""

from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.locks import acquire_generator_lock
from app.models.document import Document
from app.models.part import Part
from app.models.purchasing import POStatus, PurchaseOrder, PurchaseOrderLine, Vendor
from app.models.work_order import WorkOrder
from app.services.operational_event_service import OperationalEventService


class PurchaseOrderNumberConflict(IntegrityError):
    """Preserve the failed header's number while callers own rollback and HTTP mapping."""

    def __init__(self, po_number: str, error: IntegrityError):
        super().__init__(error.statement, error.params, error.orig, hide_parameters=error.hide_parameters)
        self.po_number = po_number


def generate_po_number(db: Session, company_id: int = None) -> str:
    """Generate next PO number (PO-YYYYMMDD-XXX).

    Holds an advisory lock so concurrent creates can't collide.
    """
    acquire_generator_lock(db, "po_number", company_id)

    today = datetime.now().strftime("%Y%m%d")
    prefix = f"PO-{today}-"

    normalized = func.lower(func.trim(PurchaseOrder.po_number))
    suffix = func.substr(normalized, len(prefix) + 1)
    remainder = suffix
    for digit in '0123456789':
        remainder = func.replace(remainder, digit, '')
    numeric_key = func.ltrim(suffix, '0')
    query = db.query(PurchaseOrder).filter(normalized.like(f"{prefix.lower()}%"), suffix != '', remainder == '')
    if company_id is not None:
        query = query.filter(PurchaseOrder.company_id == company_id)
    # An imported existing PO can share the date prefix with a nonnumeric suffix
    # or leading zeroes. Sort only valid integers without casting unbounded
    # printed identifiers to a database integer (which can overflow).
    last_po = query.order_by(func.length(numeric_key).desc(), numeric_key.desc()).first()

    if last_po:
        last_num = int(last_po.po_number.strip().split("-")[-1])
        new_num = last_num + 1
    else:
        new_num = 1

    candidate = f"{prefix}{new_num:03d}"
    if len(candidate) > 50:
        # A very long imported numeric identifier may occupy the upper bound of
        # the column. Find an ordinary short gap rather than let it poison future
        # auto-numbered orders. One bounded query; never fetch the whole tenant.
        normal_numbers = (
            query.with_entities(numeric_key.label('number'), func.length(numeric_key).label('digits'))
            .filter(func.length(numeric_key) <= 5)
            .distinct()
            .order_by(func.length(numeric_key), numeric_key)
            .limit(10001)
            .all()
        )
        occupied = {int(row.number or '0') for row in normal_numbers}
        gap_number = next((number for number in range(1, 10001) if number not in occupied), None)
        if gap_number is None:
            raise HTTPException(409, 'Automatic purchase order numbers for today require review in Purchasing.')
        candidate = f"{prefix}{gap_number:03d}"
    return candidate


def create_purchase_order_command(
    db,
    po_in,
    current_user,
    company_id,
    audit,
    *,
    imported_po_number=None,
    imported_order_date=None,
    source_document_path=None,
    ready_for_receiving=False,
):
    """Create the existing draft PO workflow without committing; caller owns atomicity."""
    # Verify vendor -- must be a live, active vendor (can't open a PO against a
    # deleted or deactivated supplier).
    vendor = (
        db.query(Vendor)
        .filter(
            Vendor.id == po_in.vendor_id,
            Vendor.company_id == company_id,
            Vendor.is_deleted == False,  # noqa: E712
            Vendor.is_active == True,  # noqa: E712
        )
        .first()
    )
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    po_number = imported_po_number or generate_po_number(db, company_id)

    po = PurchaseOrder(
        po_number=po_number,
        vendor_id=po_in.vendor_id,
        required_date=po_in.required_date,
        expected_date=po_in.expected_date,
        ship_to=po_in.ship_to,
        shipping_method=po_in.shipping_method,
        notes=po_in.notes,
        created_by=current_user.id,
        order_date=imported_order_date,
        source_document_path=source_document_path,
    )
    po.company_id = company_id
    db.add(po)
    try:
        db.flush()
    except IntegrityError as exc:
        # The generator lock serializes Postgres creates; preserve the existing
        # duplicate-number backstop for SQLite and any residual header conflict.
        raise PurchaseOrderNumberConflict(po_number, exc) from exc

    # Add lines
    subtotal = 0.0
    for idx, line_data in enumerate(po_in.lines, 1):
        part = db.query(Part).filter(Part.id == line_data.part_id, Part.company_id == company_id).first()
        if not part:
            raise HTTPException(status_code=404, detail=f"Part {line_data.part_id} not found")

        # quantity_ordered/unit_price parse as Decimal (Money schema types) but the
        # PO money columns are Float — coerce so `subtotal += line_total` and the
        # `subtotal + po.tax + po.shipping` total below don't mix Decimal with float.
        line_total = float(line_data.quantity_ordered) * float(line_data.unit_price)
        line = PurchaseOrderLine(
            purchase_order_id=po.id,
            line_number=idx,
            part_id=line_data.part_id,
            quantity_ordered=line_data.quantity_ordered,
            unit_price=line_data.unit_price,
            line_total=line_total,
            required_date=line_data.required_date or po_in.required_date,
            notes=line_data.notes,
        )
        line.company_id = company_id
        db.add(line)
        subtotal += line_total

    po.subtotal = subtotal
    po.total = subtotal + po.tax + po.shipping

    db.flush()
    audit.log_create(
        "purchase_order",
        po.id,
        po.po_number,
        new_values=po,
        extra_data={"vendor_code": vendor.code, "line_count": len(po_in.lines)},
    )
    if ready_for_receiving:
        # Employee attests that this uploaded existing PO belongs in Receiving.
        # This records workflow state, without supplier dispatch or receipt rows.
        po.status = POStatus.SENT
        db.flush()
        audit.log_status_change(
            "purchase_order",
            po.id,
            po.po_number,
            old_status=POStatus.DRAFT.value,
            new_status=POStatus.SENT.value,
            extra_data={
                "action": "reviewed_document_import",
                "order_date": po.order_date.isoformat() if po.order_date else None,
                "delivery_status": "not_dispatched",
            },
        )
    OperationalEventService(db).emit_best_effort(
        company_id=company_id,
        event_type="purchase_order_created",
        source_module="purchasing",
        entity_type="purchase_order",
        entity_id=po.id,
        user_id=current_user.id,
        severity="info",
        event_payload={
            "po_number": po.po_number,
            "vendor_id": po.vendor_id,
            "vendor_name": vendor.name,
            "line_count": len(po_in.lines),
            "required_date": po.required_date.isoformat() if po.required_date else None,
            "total": float(po.total or 0),
        },
    )
    return po


def _audit_values(document: Document) -> dict:
    return {
        "document_number": document.document_number,
        "revision": document.revision,
        "previous_revision_id": document.previous_revision_id,
        "revision_notes": document.revision_notes,
        "title": document.title,
        "document_type": document.document_type.value,
        "description": document.description,
        "part_id": document.part_id,
        "work_order_id": document.work_order_id,
        "vendor_id": document.vendor_id,
        "file_name": document.file_name,
        "file_size": document.file_size,
        "status": document.status,
        "released_by": document.released_by,
        "released_at": document.released_at.isoformat() if document.released_at else None,
    }


def _audit_document(db, audit, document, *, action, old_values=None, extra_data=None):
    """Flush document evidence without committing the caller's transaction."""
    db.flush()
    audit.log_required(
        action=action,
        resource_type="document",
        resource_id=document.id,
        resource_identifier=document.document_number,
        old_values=old_values,
        new_values=_audit_values(document) if action != "DELETE" else None,
        extra_data=extra_data,
    )
    if action == "DELETE":
        db.delete(document)


def attach_document_command(db, document_id, work_order_id, company_id, audit):
    """Apply the guarded existing attachment workflow; caller commits evidence atomically."""
    document = (
        db.query(Document)
        .filter(Document.id == document_id, Document.company_id == company_id)
        .with_for_update()
        .first()
    )
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    work_order = (
        db.query(WorkOrder)
        .filter(WorkOrder.id == work_order_id, WorkOrder.company_id == company_id, WorkOrder.is_deleted == False)
        .first()
    )
    if not work_order:
        raise HTTPException(status_code=404, detail="Work order not found")

    file_name = document.file_name or ""
    is_pdf = document.mime_type == "application/pdf" or file_name.lower().endswith(".pdf")
    if not is_pdf:
        raise HTTPException(status_code=400, detail="Only PDF documents can be attached as work order drawings")

    if document.work_order_id == work_order_id:
        return document
    if (
        document.work_order_id is not None
        or document.previous_revision_id
        or db.query(Document.id)
        .filter(Document.company_id == company_id, Document.previous_revision_id == document.id)
        .first()
    ):
        raise HTTPException(409, "An existing work-order or revision-history attachment cannot be reassigned")

    old_values = _audit_values(document)
    document.work_order_id = work_order_id
    _audit_document(db, audit, document, action="UPDATE", old_values=old_values)
    return document
