import hashlib
import logging
import mimetypes
import os
import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session, joinedload, with_loader_criteria

from app.api.deps import get_audit_service, get_current_company_id, get_current_user, require_role
from app.db.database import get_db
from app.db.tenant_filter import tenant_query
from app.models.document import Document, DocumentType
from app.models.hank_intake import HankIntakeFile
from app.models.part import Part
from app.models.purchasing import POReceipt, Vendor
from app.models.qms_standard import QMSClause, QMSClauseEvidence, QMSStandard
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder
from app.schemas.base import UTCModel
from app.services.audit_service import AuditService, AuditWriteError
from app.services.document_numbering import generate_document_number as generate_shared_document_number
from app.services.erp_draft_commands import _audit_document, _audit_values, attach_document_command
from app.services.storage_service import (
    delete_ref,
    get_storage,
    is_s3_ref,
    open_ref_stream,
    ref_exists,
    resolve_upload_dir,
    sanitize_ext,
)

router = APIRouter()
logger = logging.getLogger(__name__)
# Manual uploads publish immediately. These are release-capable office roles;
# generated receipt/shipping artifacts retain their own business-route gates.
DOCUMENT_WRITE_ROLES = [UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY]

UPLOAD_DIR = resolve_upload_dir()


def _document_query(db: Session, company_id: int):
    # Historical documents still name a removed part, but never another tenant's
    # part. Refresh loaded relationships too, including after a write/commit.
    return (
        db.query(Document)
        .options(joinedload(Document.part), with_loader_criteria(Part, Part.company_id == company_id))
        .filter(Document.company_id == company_id)
        .populate_existing()
    )


def _commit_audited_document(db, audit, document, *, action, old_values=None, extra_data=None):
    try:
        _audit_document(db, audit, document, action=action, old_values=old_values, extra_data=extra_data)
        db.commit()
    except AuditWriteError as exc:
        db.rollback()
        raise HTTPException(503, "Unable to save audit record") from exc
    except Exception:
        db.rollback()
        raise


def _qms_link_refusal(db: Session, company_id: int, document_id: int) -> Optional[str]:
    """Say why the active company's QMS records keep this document, or return None.

    ``qms_standards.document_id`` and ``qms_clause_evidence.document_id`` are plain
    foreign keys with no ``ON DELETE``. On Postgres a physical delete of a referenced
    document fails at flush, after the document row lock and the global audit-chain
    lock are already held: a 500, or a deadlock (40P01, answered 409 or 503 depending
    on where the delete loses) against an evidence edit that has locked the referencing
    row. Neither says why, so the delete route refuses here, before the audit write and
    before any mutation.

    Removed rows count. A soft-deleted standard, clause or evidence link is still the
    record of which document a clause was assessed against, and nulling its link would
    rewrite that record. Its document stays retained, as a voided receipt's certificate
    does. Evidence under a removed clause or standard counts as removed even when its
    own flag is clear, because every QMS read hides it with its parent, and the evidence
    ``PUT`` / ``DELETE`` refuse it (404) as they refuse a removed link
    (``qms_standards._lock_live_evidence_or_404``), so its link cannot be cleared either.

    The document row lock the caller already holds orders this against a concurrent
    link. Every QMS write that sets ``document_id`` flushes it before its audit row, so
    it takes its key-share lock on the document row before the global audit-chain lock,
    the order this route takes them in. A writer that got there first commits before the
    row lock is granted, and these reads see its link. A writer that comes later waits
    for the delete and then fails its own foreign-key check.

    Scoped to the active company. A link held by another company's QMS row (the QMS
    create / update routes have accepted any existing document id, another company's
    included) is not reported, since naming it would disclose the other tenant's record.
    On Postgres that delete still fails at the foreign key.
    """
    standards = (
        db.query(QMSStandard.name, QMSStandard.is_deleted)
        .filter(QMSStandard.company_id == company_id, QMSStandard.document_id == document_id)
        .order_by(QMSStandard.id)
        .all()
    )
    evidence = (
        db.query(
            QMSClauseEvidence.is_deleted.label("evidence_deleted"),
            QMSClause.clause_number,
            QMSClause.is_deleted.label("clause_deleted"),
            QMSStandard.name.label("standard_name"),
            QMSStandard.is_deleted.label("standard_deleted"),
        )
        .select_from(QMSClauseEvidence)
        .outerjoin(QMSClause, and_(QMSClause.id == QMSClauseEvidence.clause_id, QMSClause.company_id == company_id))
        .outerjoin(QMSStandard, and_(QMSStandard.id == QMSClause.standard_id, QMSStandard.company_id == company_id))
        .filter(QMSClauseEvidence.company_id == company_id, QMSClauseEvidence.document_id == document_id)
        .order_by(QMSClauseEvidence.id)
        .all()
    )
    if not standards and not evidence:
        return None

    live_links = [f"to QMS standard {row.name}" for row in standards if not row.is_deleted]
    live_links += [
        f"as QMS evidence on {row.standard_name} clause {row.clause_number}"
        for row in evidence
        # A NULL flag means the clause or standard did not join (it is not this
        # company's), so there is nothing the user can open: not a live link.
        if all(
            flag is not None and not flag for flag in (row.evidence_deleted, row.clause_deleted, row.standard_deleted)
        )
    ]
    if not live_links:
        return (
            "This document is named by a removed QMS standard, clause or evidence link, which keeps the record "
            "of what was assessed. It must remain available and cannot be deleted."
        )
    others = len(live_links) - 1
    also = f" and {others} other QMS link{'s' if others > 1 else ''}" if others else ""
    return (
        f"This document is linked {live_links[0]}{also}. Unlink it first by setting document_id to null on that "
        "QMS standard or evidence link. Removing the link instead keeps it on record, and the document with it."
    )


class DocumentPartResponse(BaseModel):
    part_number: str
    name: str

    class Config:
        from_attributes = True


class DocumentResponse(UTCModel):
    part: Optional[DocumentPartResponse] = None
    id: int
    document_number: str
    revision: str
    previous_revision_id: Optional[int] = None
    revision_notes: Optional[str] = None
    title: str
    document_type: str
    description: Optional[str] = None
    part_id: Optional[int] = None
    work_order_id: Optional[int] = None
    vendor_id: Optional[int] = None
    file_name: Optional[str] = None
    file_size: Optional[int] = None
    mime_type: Optional[str] = None
    status: str
    created_at: datetime

    class Config:
        from_attributes = True
        use_enum_values = True


class WorkOrderDocumentAttachRequest(BaseModel):
    work_order_id: int


def _content_disposition(file_name: Optional[str]) -> str:
    """Attachment Content-Disposition matching Starlette's FileResponse filename handling."""
    from urllib.parse import quote

    if not file_name:
        return "attachment"
    quoted = quote(file_name)
    if quoted != file_name:
        return f"attachment; filename*=utf-8''{quoted}"
    return f'attachment; filename="{file_name}"'


def generate_document_number(db: Session, doc_type: str) -> str:
    """Delegates to the shared generator (PR 4 dedupe) — kept as a re-export because
    callers (this router, laser_nest_service) import it from here."""
    return generate_shared_document_number(db, doc_type)


@router.get("/", response_model=List[DocumentResponse])
def list_documents(
    part_id: Optional[int] = None,
    work_order_id: Optional[int] = None,
    vendor_id: Optional[int] = None,
    document_type: Optional[str] = None,
    search: Optional[str] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=5000),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    query = _document_query(db, company_id)

    if part_id:
        query = query.filter(Document.part_id == part_id)
    if work_order_id:
        query = query.filter(Document.work_order_id == work_order_id)
    if vendor_id:
        query = query.filter(Document.vendor_id == vendor_id)
    if document_type:
        query = query.filter(Document.document_type == document_type)
    if search:
        search_filter = f"%{search}%"
        query = query.filter(
            or_(
                Document.document_number.ilike(search_filter),
                Document.title.ilike(search_filter),
                Document.description.ilike(search_filter),
            )
        )

    return query.order_by(Document.created_at.desc()).offset(skip).limit(limit).all()


@router.post("/upload", response_model=DocumentResponse)
async def upload_document(
    file: UploadFile = File(...),
    title: str = Form(...),
    document_type: str = Form(...),
    description: str = Form(None),
    part_id: int = Form(None),
    work_order_id: int = Form(None),
    vendor_id: int = Form(None),
    revision: str = Form("A"),
    previous_revision_id: Optional[int] = Form(None),
    revision_notes: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(DOCUMENT_WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Upload and release a document as Admin/Manager/Quality, with atomic audit.

    Revisions retain the previous document's type and linked records; existing
    files remain available. Failure to save the required audit refuses publication.
    """
    try:
        parsed_document_type = DocumentType(document_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid document type") from exc

    normalized_part_id = part_id if part_id and part_id > 0 else None
    normalized_work_order_id = work_order_id if work_order_id and work_order_id > 0 else None
    normalized_vendor_id = vendor_id if vendor_id and vendor_id > 0 else None

    if normalized_part_id:
        part_query = db.query(Part).filter(Part.id == normalized_part_id, Part.company_id == company_id)
        # A revision keeps its historical links, validated against the locked
        # predecessor below. A fresh upload cannot select a removed record.
        if not previous_revision_id:
            part_query = part_query.filter(Part.is_deleted == False)
        part = part_query.first()
        if not part:
            raise HTTPException(status_code=404, detail="Part not found")

    if normalized_work_order_id:
        work_order_query = db.query(WorkOrder).filter(
            WorkOrder.id == normalized_work_order_id,
            WorkOrder.company_id == company_id,
        )
        if not previous_revision_id:
            work_order_query = work_order_query.filter(WorkOrder.is_deleted == False)
        work_order = work_order_query.first()
        if not work_order:
            raise HTTPException(status_code=404, detail="Work order not found")

    if normalized_vendor_id:
        # Vendor DELIBERATELY not filtered on soft delete -- do not add
        # ``is_deleted == False`` here. A vendor-attached document is a quality RECORD about
        # material that already arrived (certificate of conformance, material test report,
        # corrected packing slip), and those routinely turn up AFTER the supplier
        # relationship ends -- ``Document.vendor_id`` is the only field that links such a
        # record to the supplier that certified the material, so refusing the attachment
        # does not prevent the upload, it just strips the supplier off an AS9100D 8.4
        # record. Same posture as the PO vendor block, the receipt, the lot trace and the
        # thermal label, all of which name a deleted vendor on purpose. It would also be
        # asymmetric: ``GET /documents?vendor_id=`` above has no vendor predicate at all, so
        # gating the write alone yields a path you can read but not write to.
        vendor = db.query(Vendor).filter(Vendor.id == normalized_vendor_id, Vendor.company_id == company_id).first()
        if not vendor:
            raise HTTPException(status_code=404, detail="Vendor not found")

    previous = None
    if previous_revision_id:
        previous = (
            db.query(Document)
            .filter(Document.id == previous_revision_id, Document.company_id == company_id)
            .with_for_update()
            .first()
        )
        if not previous:
            raise HTTPException(status_code=404, detail="Previous document revision not found")
        # Lock the selected predecessor so concurrent uploads cannot create sibling revisions.
        if (
            db.query(Document.id)
            .filter(Document.company_id == company_id, Document.previous_revision_id == previous.id)
            .first()
        ):
            raise HTTPException(
                status_code=409, detail="A newer revision exists. Open the latest revision before uploading."
            )
        ancestor = previous
        seen = set()
        while ancestor and ancestor.id not in seen:
            seen.add(ancestor.id)
            if revision.strip().casefold() == ancestor.revision.strip().casefold():
                raise HTTPException(
                    status_code=422, detail="This revision label already exists in the document history"
                )
            ancestor = (
                db.query(Document)
                .filter(Document.id == ancestor.previous_revision_id, Document.company_id == company_id)
                .first()
                if ancestor.previous_revision_id
                else None
            )
        if not revision_notes or not revision_notes.strip():
            raise HTTPException(status_code=422, detail="Describe what changed in this revision")
        if (
            previous.document_type != parsed_document_type
            or previous.part_id != normalized_part_id
            or previous.work_order_id != normalized_work_order_id
            or previous.vendor_id != normalized_vendor_id
        ):
            raise HTTPException(
                status_code=422,
                detail="A revision must retain the document type and linked records of the previous revision",
            )
    if not revision.strip() or len(revision.strip()) > 20:
        raise HTTPException(status_code=422, detail="Revision must contain 1–20 characters")

    # Allocate before storing bytes so a numbering failure cannot leave an orphan.
    doc_number = generate_document_number(db, document_type)

    # Generate unique filename and persist through the configured storage backend.
    content = await file.read()
    storage = get_storage()
    if storage.is_remote:
        # Tenant-prefixed, never-user-controlled object key (extension sanitized).
        key = f"{company_id}/documents/{uuid.uuid4()}{sanitize_ext(file.filename)}"
    else:
        # Legacy local layout, byte-for-byte: UPLOAD_DIR/{uuid}{ext}.
        file_ext = os.path.splitext(file.filename)[1] if file.filename else ""
        key = os.path.join(UPLOAD_DIR, f"{uuid.uuid4()}{file_ext}")
    file_path = storage.save(content, key=key)

    document = Document(
        document_number=doc_number,
        revision=revision.strip(),
        previous_revision_id=previous_revision_id,
        revision_notes=revision_notes,
        title=title,
        document_type=parsed_document_type,
        description=description,
        part_id=normalized_part_id,
        work_order_id=normalized_work_order_id,
        vendor_id=normalized_vendor_id,
        file_name=file.filename,
        file_path=file_path,
        file_size=len(content),
        mime_type=file.content_type,
        status="released",
        released_by=current_user.id,
        released_at=datetime.utcnow(),
        created_by=current_user.id,
        company_id=company_id,
    )

    try:
        db.add(document)
        _commit_audited_document(
            db,
            audit,
            document,
            action="CREATE",
            extra_data={"release": True, "content_sha256": hashlib.sha256(content).hexdigest()},
        )
    except Exception:
        # A refused publication must not retain newly uploaded bytes. Earlier
        # revision files are never touched by this compensation.
        try:
            storage.delete(file_path)
        except Exception:
            logger.error("Could not remove an unpublished document upload")
        raise
    return _document_query(db, company_id).filter(Document.id == document.id).one()


@router.get("/types/list")
def list_document_types(current_user: User = Depends(get_current_user)):
    return [{"value": t.value, "label": t.value.replace("_", " ").title()} for t in DocumentType]


@router.get("/{document_id}/revisions", response_model=List[DocumentResponse])
def get_document_revisions(
    document_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    documents = _document_query(db, company_id).all()
    by_id = {doc.id: doc for doc in documents}
    if document_id not in by_id:
        raise HTTPException(status_code=404, detail="Document not found")
    root_id = document_id
    seen = set()
    while root_id not in seen and by_id[root_id].previous_revision_id in by_id:
        seen.add(root_id)
        root_id = by_id[root_id].previous_revision_id
    linked = {root_id}
    while True:
        children = {doc.id for doc in documents if doc.previous_revision_id in linked}
        if children.issubset(linked):
            break
        linked.update(children)
    return sorted((by_id[id] for id in linked), key=lambda doc: doc.created_at, reverse=True)


@router.get("/{document_id}", response_model=DocumentResponse)
def get_document(
    document_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    document = _document_query(db, company_id).filter(Document.id == document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    return document


@router.get("/{document_id}/download")
def download_document(
    document_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    from fastapi.responses import FileResponse, StreamingResponse

    document = db.query(Document).filter(Document.id == document_id, Document.company_id == company_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    # Per-row dispatch on the stored ref: s3:// rows stream from object storage,
    # legacy/local rows keep the exact FileResponse behavior.
    if is_s3_ref(document.file_path):
        if not ref_exists(document.file_path):
            raise HTTPException(status_code=404, detail="File not found")
        # NULL mime_type rows still get a sensible Content-Type (FileResponse used
        # to guess from the filename on the local path; mirror that here).
        media_type = (
            document.mime_type or mimetypes.guess_type(document.file_name or "")[0] or "application/octet-stream"
        )
        return StreamingResponse(
            open_ref_stream(document.file_path),
            media_type=media_type,
            headers={"Content-Disposition": _content_disposition(document.file_name)},
        )

    if not document.file_path or not os.path.exists(document.file_path):
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(document.file_path, filename=document.file_name, media_type=document.mime_type)


@router.post("/{document_id}/attach-work-order", response_model=DocumentResponse)
def attach_document_to_work_order(
    document_id: int,
    payload: WorkOrderDocumentAttachRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(DOCUMENT_WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Attach an unlinked PDF to a live tenant work order; existing history stays bound."""
    try:
        attach_document_command(db, document_id, payload.work_order_id, company_id, audit)
        db.commit()
        return _document_query(db, company_id).filter(Document.id == document_id).one()
    except AuditWriteError as exc:
        db.rollback()
        raise HTTPException(503, "Unable to save audit record") from exc
    except Exception:
        db.rollback()
        raise


@router.delete("/{document_id}")
def delete_document(
    document_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Delete an unretained document as Admin/Manager, preserving audited metadata.

    Refused 409 while the document is retained: part of a revision history, a receipt's
    certificate, Hank intake evidence, or linked from a QMS standard or QMS clause
    evidence (a removed QMS link keeps it too).
    """
    document = (
        db.query(Document)
        .filter(Document.id == document_id, Document.company_id == company_id)
        .with_for_update()
        .first()
    )
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    if (
        document.previous_revision_id
        or db.query(Document.id)
        .filter(Document.company_id == company_id, Document.previous_revision_id == document.id)
        .first()
    ):
        raise HTTPException(
            status_code=409,
            detail="This document belongs to a revision history and cannot be deleted. Its prior files must remain available.",
        )

    if (
        db.query(POReceipt.id)
        .filter(POReceipt.company_id == company_id, POReceipt.certificate_document_id == document.id)
        .first()
    ):
        raise HTTPException(
            409, "This certificate belongs to a receipt and must remain available, including after a receipt is voided."
        )

    if (
        tenant_query(db, HankIntakeFile, company_id)
        .filter(
            or_(
                HankIntakeFile.storage_ref == document.file_path,
                HankIntakeFile.result_json['document_id'].as_integer() == document.id,
            )
        )
        .first()
    ):
        raise HTTPException(
            409, 'This document is retained intake evidence. Its metadata and source file must remain available.'
        )

    qms_refusal = _qms_link_refusal(db, company_id, document.id)
    if qms_refusal:
        raise HTTPException(409, qms_refusal)

    file_path = document.file_path
    _commit_audited_document(db, audit, document, action="DELETE", old_values=_audit_values(document))
    # Only remove bytes after metadata and required audit commit together. An
    # object-store failure can leave an orphan, never a live row with lost bytes.
    if file_path:
        try:
            if ref_exists(file_path):
                delete_ref(file_path)
        except Exception:
            logger.error("Document %s deleted; stored-file cleanup requires retry", document_id)

    return {"message": "Document deleted"}
