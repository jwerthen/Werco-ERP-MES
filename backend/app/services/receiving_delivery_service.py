"""Delivery-level atomic receipts and tenant-bound certificate storage."""

import hashlib
import json
import os
import uuid

from fastapi import HTTPException

from app.db.locks import acquire_generator_lock
from app.models.document import Document, DocumentType
from app.models.purchasing import POReceipt, PurchaseOrder, PurchaseOrderLine, ReceivingDeliveryBatch
from app.schemas.purchasing import ReceiptResponse
from app.services.document_numbering import generate_document_number
from app.services.storage_service import delete_ref, get_storage, ref_exists, resolve_upload_dir


def _check_certificate(document, line, *, check_storage=True):
    if not document or document.part_id != line.part_id or document.vendor_id != line.purchase_order.vendor_id:
        raise HTTPException(422, "Choose a certificate uploaded for this part and supplier")
    if document.document_type not in (DocumentType.CERTIFICATE, DocumentType.MATERIAL_CERT) or not document.file_path:
        raise HTTPException(422, "The linked document must be a stored certificate")
    if check_storage and not ref_exists(document.file_path):
        raise HTTPException(422, "Certificate file is unavailable. Upload it again before receiving.")


def validate_certificate(db, company_id, line, document_id, *, check_storage=True):
    """Row-lock one certificate and check it belongs to ``line``'s part and supplier.

    The caller must not have written an audit row yet: the lock has to come before the global
    audit-chain lock, the order document writers take them in. (Hank intake calls this after its
    audit row, on a document it inserted in the same transaction, which no other transaction can
    lock.) A delivery locks all its lines' certificates up front instead (``_lock_delivery``).
    """
    if not document_id:
        return
    document = (
        db.query(Document)
        .filter(Document.id == document_id, Document.company_id == company_id)
        .with_for_update()
        .first()
    )
    _check_certificate(document, line, check_storage=check_storage)


def _delivery_line_error(index, exc):
    return HTTPException(exc.status_code, f"Delivery line {index + 1}: {exc.detail}. No delivery lines were posted.")


def _lock_delivery(db, company_id, purchase_order_id, delivery_lines):
    """Take a delivery's locks before it writes anything, in the order a single receipt takes them.

    The order is the receipt-number lock, the PO row (``FOR UPDATE``), then every line's certificate
    in ascending id order: ``_receive_material``'s order for one receipt, and Hank's locked preview's.
    They are taken here, not in the receive loop, for two reasons:

    - The batch insert takes ``FOR KEY SHARE`` on the PO through its foreign key. Done before the
      receipt-number lock, it deadlocks against a single receipt on the same PO that holds that lock
      and waits for the PO's ``FOR UPDATE``.
    - Receiving line 1 writes an audit row, which takes the global audit-chain lock. Locking line 2's
      certificate after that inverts the order document writers use (the document row, then the
      chain lock: document delete, revision upload, document attach) and deadlocks against one
      holding that certificate.

    Every line's certificate is checked here, with ``validate_certificate``'s checks. Returns the ids
    of the certificates locked and checked; the receive loop hands them to ``_receive_material`` so
    it does not check (and lock) them again after the chain lock. The locks last until the delivery
    commits or rolls back.
    """
    acquire_generator_lock(db, "receipt_number", company_id)
    po = (
        db.query(PurchaseOrder)
        .filter(
            PurchaseOrder.id == purchase_order_id,
            PurchaseOrder.company_id == company_id,
            PurchaseOrder.is_deleted.is_(False),
        )
        .with_for_update()
        .populate_existing()
        .first()
    )
    if not po:
        raise HTTPException(404, "Purchase order not found")
    document_ids = sorted({line.certificate_document_id for line in delivery_lines if line.certificate_document_id})
    if not document_ids:
        return frozenset()
    documents = {
        document.id: document
        for document in db.query(Document)
        .filter(Document.company_id == company_id, Document.id.in_(document_ids))
        .order_by(Document.id)
        .with_for_update()
        .all()
    }
    certificate_line_ids = sorted({line.po_line_id for line in delivery_lines if line.certificate_document_id})
    po_lines = {
        row.id: row
        for row in db.query(PurchaseOrderLine).filter(
            PurchaseOrderLine.company_id == company_id,
            PurchaseOrderLine.purchase_order_id == po.id,
            PurchaseOrderLine.id.in_(certificate_line_ids),
        )
    }
    for index, line in enumerate(delivery_lines):
        if not line.certificate_document_id:
            continue
        po_line = po_lines.get(line.po_line_id)
        if po_line is None:
            raise HTTPException(422, "Every delivery line must belong to this purchase order")
        try:
            _check_certificate(documents.get(line.certificate_document_id), po_line)
        except HTTPException as exc:
            raise _delivery_line_error(index, exc) from exc
    return frozenset(document_ids)


def post_delivery_command(db, user, company_id, body, audit, *, return_created=False):
    from app.services.receiving_commands import _receive_material
    from app.services.supplier_followup_service import require_module_write

    require_module_write(db, user, company_id, "receiving", "create")

    digest = hashlib.sha256(json.dumps(body.model_dump(mode='json'), sort_keys=True).encode()).hexdigest()
    acquire_generator_lock(db, f"receiving_delivery:{body.idempotency_key}", company_id)
    previous = (
        db.query(ReceivingDeliveryBatch)
        .filter(
            ReceivingDeliveryBatch.company_id == company_id, ReceivingDeliveryBatch.request_key == body.idempotency_key
        )
        .first()
    )
    if previous:
        if previous.created_by != user.id or previous.payload_hash != digest:
            raise HTTPException(
                409, "This delivery key belongs to a different submission. Review the original delivery."
            )
        return (previous.response, False) if return_created else previous.response
    # Scope the entire worksheet to one real PO; never trust a line's submitted part/vendor.
    lines = (
        db.query(PurchaseOrderLine.id)
        .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderLine.purchase_order_id)
        .filter(
            PurchaseOrderLine.company_id == company_id,
            PurchaseOrderLine.purchase_order_id == body.purchase_order_id,
            PurchaseOrder.company_id == company_id,
            PurchaseOrder.is_deleted.is_(False),
        )
        .all()
    )
    valid_ids = {row.id for row in lines}
    if any(line.po_line_id not in valid_ids for line in body.lines):
        raise HTTPException(422, "Every delivery line must belong to this purchase order")
    # Before the batch insert and line 1's audit row; see the helper.
    checked_certificate_ids = _lock_delivery(db, company_id, body.purchase_order_id, body.lines)
    batch = ReceivingDeliveryBatch(
        company_id=company_id,
        purchase_order_id=body.purchase_order_id,
        request_key=body.idempotency_key,
        payload_hash=digest,
        response={},
        created_by=user.id,
    )
    db.add(batch)
    db.flush()
    receipts = []
    for index, line in enumerate(body.lines):
        try:
            receipt = _receive_material(
                line, db, user, company_id, audit, checked_certificate_ids=checked_certificate_ids
            )
        except HTTPException as exc:
            raise _delivery_line_error(index, exc) from exc
        receipt.delivery_batch_id = batch.id
        receipts.append(receipt)
    db.flush()
    result = {
        "batch_id": batch.id,
        "idempotency_key": body.idempotency_key,
        "receipts": [ReceiptResponse.model_validate(row).model_dump(mode='json') for row in receipts],
    }
    batch.response = result
    audit.log_create(
        'receiving_delivery',
        batch.id,
        body.idempotency_key,
        new_values={"purchase_order_id": body.purchase_order_id, "receipt_ids": [r.id for r in receipts]},
    )
    return (result, True) if return_created else result


def post_delivery(db, user, company_id, body, audit):
    # Printing remains explicit post-commit behavior of the receiving screen.
    from app.api.endpoints.receiving import enqueue_receipt_label

    try:
        result, created = post_delivery_command(db, user, company_id, body, audit, return_created=True)
        db.commit()
    except Exception:
        db.rollback()
        raise
    if created:
        for item in result['receipts']:
            receipt = db.get(POReceipt, item['id'])
            enqueue_receipt_label(receipt, company_id, user.id)
    return result


async def upload_certificate(db, user, company_id, line_id, file, audit, receipt_id=None):
    from app.services.supplier_followup_service import require_module_write

    require_module_write(db, user, company_id, "receiving", "create")
    line = (
        db.query(PurchaseOrderLine)
        .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderLine.purchase_order_id)
        .filter(
            PurchaseOrderLine.id == line_id,
            PurchaseOrderLine.company_id == company_id,
            PurchaseOrder.company_id == company_id,
        )
        .first()
    )
    if not line:
        raise HTTPException(404, "PO line not found")
    receipt = None
    if receipt_id:
        receipt = (
            db.query(POReceipt)
            .filter(POReceipt.id == receipt_id, POReceipt.company_id == company_id, POReceipt.po_line_id == line_id)
            .with_for_update()
            .first()
        )
        if not receipt:
            raise HTTPException(404, "Receipt not found for this PO line")
        if receipt.certificate_document_id:
            raise HTTPException(
                409,
                "A certificate is already linked. Keep the historical file; use document revisions for corrections.",
            )
    # Historical certificates remain attachable even after the vendor/PO is removed.
    content = await file.read(20 * 1024 * 1024 + 1)
    if not content or len(content) > 20 * 1024 * 1024:
        raise HTTPException(422, "Choose a nonempty certificate file up to 20 MB")
    if content.startswith(b'%PDF-'):
        extension, mime = '.pdf', 'application/pdf'
    elif content.startswith(b'\x89PNG\r\n\x1a\n'):
        extension, mime = '.png', 'image/png'
    elif content.startswith(b'\xff\xd8\xff'):
        extension, mime = '.jpg', 'image/jpeg'
    else:
        raise HTTPException(422, "Certificate must be a PDF, PNG, or JPEG file")
    storage = get_storage()
    key = (
        f"{company_id}/documents/{uuid.uuid4()}{extension}"
        if storage.is_remote
        else os.path.join(resolve_upload_dir(), f"{uuid.uuid4()}{extension}")
    )
    stored = storage.save(content, key=key)
    try:
        document = Document(
            company_id=company_id,
            document_number=generate_document_number(db, 'material_cert'),
            title=f"Certificate for {line.purchase_order.po_number} line {line.line_number}",
            revision='A',
            document_type=DocumentType.MATERIAL_CERT,
            part_id=line.part_id,
            vendor_id=line.purchase_order.vendor_id,
            file_name=os.path.basename(file.filename or f'certificate{extension}')[:255],
            file_path=stored,
            file_size=len(content),
            mime_type=mime,
            status='released',
            created_by=user.id,
        )
        db.add(document)
        db.flush()
        audit.log_create(
            'document',
            document.id,
            document.document_number,
            new_values=document,
            description=f"Uploaded receiving certificate for PO line {line.id}",
        )
        if receipt:
            receipt.certificate_document_id = document.id
            receipt.coc_attached = True
            audit.log_update(
                'receipt',
                receipt.id,
                receipt.receipt_number,
                {'certificate_document_id': None},
                {'certificate_document_id': document.id},
                description='Attached certificate received after the material; stock and receipt quantities unchanged',
            )
        result = {"id": document.id, "file_name": document.file_name, "document_number": document.document_number}
    except Exception:
        db.rollback()
        delete_ref(stored)
        raise
    # A connection can fail after COMMIT was durable. Preserve bytes when the outcome
    # is uncertain; deleting them could corrupt a committed document/receipt link.
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(
            503,
            "Certificate save outcome is unknown. Refresh the receipt certificate or check Documents "
            "before uploading again; the stored file has been preserved.",
        ) from exc
    return result
