import hashlib
import json
import logging
import mimetypes
import os
import uuid
from datetime import datetime
from typing import List, Optional, Set, Tuple

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session, joinedload, with_loader_criteria

from app.api.audit_guard import REQUIRED_AUDIT_UNAVAILABLE_DETAIL, required_audit
from app.api.deps import get_audit_service, get_current_company_id, get_current_user, require_role
from app.core.credentials import API_TOKEN_DOCUMENT_ACCESS_LEVEL, is_api_token_credential
from app.db.database import get_db
from app.db.tenant_filter import tenant_query
from app.models.document import Document, DocumentAccessLevel, DocumentType
from app.models.engineering_change import ECOStatus, EngineeringChangeOrder
from app.models.governance import DocumentFile
from app.models.hank_intake import HankIntakeFile
from app.models.laser_nest import LaserNest
from app.models.part import Part
from app.models.process_sheet import OperationStepRecord
from app.models.purchasing import POReceipt, Vendor
from app.models.qms_standard import QMSClause, QMSClauseEvidence, QMSStandard
from app.models.shipping import Shipment
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder, WorkOrderOperation
from app.schemas.base import UTCModel
from app.schemas.document_access import (
    DocumentAccessLevelChange,
    DocumentAccessReviewResponse,
    DocumentAccessReviewRow,
)
from app.services.audit_service import AuditService, AuditWriteError
from app.services.document_access import (
    ACCESS_LEVEL_ORDER,
    export_authorized_user_count,
    visible_document_or_404,
    visible_documents_clause,
)
from app.services.document_access_service import (
    change_document_access_level,
    document_access_review,
    resolve_upload_access_level,
)
from app.services.document_numbering import generate_document_number as generate_shared_document_number
from app.services.erp_draft_commands import _audit_document, _audit_values, attach_document_command
from app.services.operation_action_gates import operation_has_recorded_work
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
# Changing who may open a document, and reviewing which documents should be export controlled
# (AS9100D 7.5.3.1 / 7.5.3.2): Quality owns document control, the Admin owns access.
DOCUMENT_ACCESS_ROLES = [UserRole.ADMIN, UserRole.QUALITY]

UPLOAD_DIR = resolve_upload_dir()


def _written_document_query(db: Session, company_id: int):
    """The active company's documents with their part, WITHOUT the access rule.

    Only for echoing back a row this request itself just wrote (an upload, an access-level
    change), whose metadata the caller supplied or already saw; every read goes through
    :func:`_document_query`. Historical documents still name a removed part, but never
    another tenant's part. Refresh loaded relationships too, including after a write/commit.
    """
    return (
        db.query(Document)
        .options(joinedload(Document.part), with_loader_criteria(Part, Part.company_id == company_id))
        .filter(Document.company_id == company_id)
        .populate_existing()
    )


def _document_query(db: Session, company_id: int, user: User):
    """The active company's documents that ``user`` may open (``app.services.document_access``)."""
    return _written_document_query(db, company_id).filter(visible_documents_clause(user))


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


def _qms_link_blockers(db: Session, company_id: int, document_id: int) -> Tuple[Optional[str], Optional[str]]:
    """Say why the active company's QMS records keep this document, as ``(permanent, release)``.

    ``permanent`` is set when a removed QMS row names the document: no verb releases it.
    ``release`` is set when a live link names it, and says how to unlink it. Both can be
    set; the route reports ``permanent`` first, so nobody unlinks a live row only to be
    refused again for a removed one.

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

    Scoped to the active company. A link held by another company's QMS row is not
    reported, since naming it would disclose the other tenant's record. On Postgres that
    delete still fails at the foreign key. Since 2026-11-17 (#348) the QMS create / update routes
    refuse another company's document (``qms_standards._assert_company_document``), so such a link
    can only be one written before that check, when they accepted any existing document id.
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
        return None, None

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
    permanent = None
    if len(standards) + len(evidence) > len(live_links):
        permanent = (
            "This document is named by a removed QMS standard, clause or evidence link, which keeps the record "
            "of what was assessed. It must remain available and cannot be deleted."
        )
    release = None
    if live_links:
        others = len(live_links) - 1
        also = f" and {others} other QMS link{'s' if others > 1 else ''}" if others else ""
        release = (
            f"This document is linked {live_links[0]}{also}. Unlink it first by setting document_id to null on "
            "that QMS standard or evidence link. Removing the link instead keeps it on record, and the document "
            "with it."
        )
    return permanent, release


def _retained_record_refusal(db: Session, company_id: int, document_id: int) -> Optional[str]:
    """Say which of the active company's records keeps this document for good, or return None.

    Each column below is a plain foreign key to ``documents.id`` with no ``ON DELETE``,
    so on Postgres a physical delete of a referenced document fails at flush, after the
    document row lock and the global audit-chain lock are held: a 500 that says nothing.
    None of these records has a verb that lets go of its document, and each is evidence
    in its own right, so the document stays for as long as the record does, removed or
    voided included (the stance a receipt's certificate already takes):

    - ``shipments.label_document_id`` / ``bol_document_id``: the purchased carrier
      label and bill of lading. A voided label keeps its document, as does a removed
      shipment.
    - ``po_receipts.label_document_id``: the receiving label last printed for the
      receipt. A reprint stores a new document and points the receipt at it.
    - ``operation_step_records.attachment_document_id``: the photo or file an operator
      captured at a process-sheet step. Step records are append-only, and a superseded
      record keeps its evidence.
    - ``document_files.document_id``: a governed file record, which can carry a
      retention policy or a legal hold.

    Every writer of these columns either creates the document in the same transaction
    or flushes the link before its audit row, so it takes its key-share lock on the
    document row before the audit-chain lock, the order this route takes them in.

    Scoped to the active company. Every writer checks that the document is the
    company's own, and another company's row is never read, so nothing is disclosed.
    """
    shipment = (
        db.query(Shipment.shipment_number, Shipment.label_document_id)
        .filter(
            Shipment.company_id == company_id,
            or_(Shipment.label_document_id == document_id, Shipment.bol_document_id == document_id),
        )
        .order_by(Shipment.id)
        .first()
    )
    if shipment:
        artifact = "carrier label" if shipment.label_document_id == document_id else "bill of lading"
        return (
            f"This document is the {artifact} for shipment {shipment.shipment_number}. Carrier labels and bills of "
            "lading are retained shipping records and must remain available, including after a label is voided or "
            "the shipment is removed."
        )

    receipt = (
        db.query(POReceipt.receipt_number)
        .filter(POReceipt.company_id == company_id, POReceipt.label_document_id == document_id)
        .order_by(POReceipt.id)
        .first()
    )
    if receipt:
        return (
            f"This document is the receiving label for receipt {receipt.receipt_number} and must remain available, "
            "including after the receipt is voided."
        )

    step_record = (
        db.query(OperationStepRecord.id, WorkOrder.work_order_number)
        .select_from(OperationStepRecord)
        .outerjoin(
            WorkOrderOperation,
            and_(
                WorkOrderOperation.id == OperationStepRecord.work_order_operation_id,
                WorkOrderOperation.company_id == company_id,
            ),
        )
        .outerjoin(
            WorkOrder, and_(WorkOrder.id == WorkOrderOperation.work_order_id, WorkOrder.company_id == company_id)
        )
        .filter(OperationStepRecord.company_id == company_id, OperationStepRecord.attachment_document_id == document_id)
        .order_by(OperationStepRecord.id)
        .first()
    )
    if step_record:
        where = f" on work order {step_record.work_order_number}" if step_record.work_order_number else ""
        return (
            f"This document is evidence captured at a process-sheet step{where}. Step records are append-only "
            "quality evidence, so it must remain available, including after the record is superseded."
        )

    if (
        db.query(DocumentFile.id)
        .filter(DocumentFile.company_id == company_id, DocumentFile.document_id == document_id)
        .first()
    ):
        return (
            "This document is held by a governed file record, which can carry a retention policy or a legal hold. "
            "It must remain available and cannot be deleted."
        )
    return None


def _laser_nest_blockers(db: Session, company_id: int, document_id: int) -> Tuple[Optional[str], Optional[str]]:
    """Say which of the active company's laser nests keep this document as their PDF, as ``(permanent, release)``.

    ``laser_nests.document_id`` is a plain foreign key with no ``ON DELETE`` (the same
    500 at flush as the referrers above). A nest's PDF is a reference drawing with an
    audited way to let go of it, ``DELETE /laser-nests/{id}/document`` (the Detach
    button on the work order's nest row), and ``release`` sends the user there for each
    live nest that has nothing recorded against its operation. A duplicated work order
    copies the reference, not the file, so one document can sit on several nests.

    ``permanent`` is set, and the route reports it ahead of any ``release``, when:

    - a live nest's operation has recorded work (``operation_has_recorded_work``: good,
      scrap, a start or labor). The PDF is then the layout the sheet was cut from, the
      record of which parts came off it, so the refusal does not suggest Detach. (Detach
      itself is not gated on recorded work; that is a separate owner decision.)
    - a removed (soft-deleted) nest names it. ``POST /laser-nests/{id}/restore`` brings
      the nest back with its PDF, and Detach answers 404 for a removed nest.

    Scoped to the active company, like every read here. The attach verb accepts only
    the company's own document.
    """
    nests = (
        db.query(
            LaserNest.id,
            LaserNest.cnc_number,
            LaserNest.nest_name,
            LaserNest.is_deleted,
            LaserNest.work_order_operation_id,
            WorkOrder.work_order_number,
        )
        .select_from(LaserNest)
        .outerjoin(
            WorkOrderOperation,
            and_(
                WorkOrderOperation.id == LaserNest.work_order_operation_id,
                WorkOrderOperation.company_id == company_id,
            ),
        )
        .outerjoin(
            WorkOrder, and_(WorkOrder.id == WorkOrderOperation.work_order_id, WorkOrder.company_id == company_id)
        )
        .filter(LaserNest.company_id == company_id, LaserNest.document_id == document_id)
        .order_by(LaserNest.id)
        .all()
    )
    if not nests:
        return None, None

    live = [nest for nest in nests if not nest.is_deleted]
    operation_ids = [nest.work_order_operation_id for nest in live if nest.work_order_operation_id is not None]
    operations = {
        operation.id: operation
        for operation in (
            tenant_query(db, WorkOrderOperation, company_id).filter(WorkOrderOperation.id.in_(operation_ids)).all()
            if operation_ids
            else []
        )
    }
    worked = [
        nest
        for nest in live
        if operation_has_recorded_work(db, operations.get(nest.work_order_operation_id), company_id)
    ]
    worked_ids = {nest.id for nest in worked}
    releasable = [nest for nest in live if nest.id not in worked_ids]

    def _named(nest) -> str:
        where = f" on work order {nest.work_order_number}" if nest.work_order_number else ""
        return f"laser nest {nest.cnc_number or nest.nest_name}{where}"

    permanent = None
    if worked:
        permanent = (
            f"This document is the PDF of {_named(worked[0])}, which has recorded work, so it is the record of "
            "what was cut. It must remain available and cannot be deleted."
        )
    elif len(live) < len(nests):
        permanent = (
            "This document is the PDF of a removed laser nest, which keeps it so that restoring the nest brings its "
            "drawing back. It must remain available and cannot be deleted."
        )
    release = None
    if releasable:
        others = len(releasable) - 1
        also = f" and {others} other laser nest{'s' if others > 1 else ''}" if others else ""
        release = (
            f"This document is the PDF attached to {_named(releasable[0])}{also}. Detach it from the nest first "
            "(Detach on the work order's laser nest row). Removing the nest instead keeps the PDF with it."
        )
    return permanent, release


# An ECO's affected items are the scope of the change. Until anyone has decided on it the
# scope is still a proposal its owner may edit. From the first approval decision on (under
# review means at least one approver has signed and others are pending), and for a rejected
# or cancelled change, it is the record of what was authorized or decided.
_ECO_SCOPE_EDITABLE_STATUSES = (ECOStatus.DRAFT, ECOStatus.SUBMITTED)


def _eco_document_ids(raw: Optional[str]) -> Set[int]:
    """The document ids an ECO's ``affected_documents`` JSON names; empty when unreadable.

    ``GET /eco/eco/affected-items/{id}`` skips a value it cannot parse, so it names no
    document there either, and one malformed row must not block every delete.
    """
    try:
        values = json.loads(raw) if raw else []
    except (json.JSONDecodeError, TypeError):
        return set()
    if not isinstance(values, list):
        return set()
    ids: Set[int] = set()
    for value in values:
        try:
            ids.add(int(value))
        except (TypeError, ValueError):
            continue
    return ids


def _eco_blockers(db: Session, company_id: int, document_id: int) -> Tuple[Optional[str], Optional[str]]:
    """Say which of the active company's engineering changes list this document as affected,
    as ``(permanent, release)``.

    ``engineering_change_orders.affected_documents`` is a JSON list of ids, not a foreign
    key, so nothing stops the physical delete; ``GET /eco/eco/affected-items/{id}`` then drops
    the id it can no longer resolve, and the change record loses an affected item with no
    trace on it (AS9100D 8.5.6). ECOs have no delete verb, so the reference lasts as long
    as the company does.

    - ``release``: an ECO no approver has decided on yet (draft, submitted). Its owner may
      still edit its scope, so the refusal lists each one with its edit path and says to take
      the document out of
      ``affected_documents`` with ``PUT /eco/eco/{id}`` (Admin / Manager, audited).
    - ``permanent``: an ECO under review (an approver has already signed its scope),
      approved or later, rejected or cancelled. The list is then the record of what the
      change covered, so the refusal does not suggest editing it. (``PUT /eco/eco/{id}``
      still accepts an under-review, approved or in-implementation ECO; that is a separate
      owner decision.) A status outside the known ones also counts as permanent.

    The text column is narrowed with a substring match on the id's digits, a superset of
    the rows that name it, and each candidate's JSON is then parsed, so id 1 never matches
    12. ``ECO create / update`` take a key-share lock on every document they name before
    their audit row, the lock a foreign key would take, so a list written concurrently
    with this delete is either seen here or fails its own validation afterwards. That
    relies on READ COMMITTED (the Postgres default, which the app does not change): this read
    starts after the document row lock is granted and so sees an ECO that committed while
    the delete waited. Under REPEATABLE READ it would not.

    Scoped to the active company. ECO validation accepts only the company's own document
    ids, and another company's ECO is never read.
    """
    candidates = (
        db.query(
            EngineeringChangeOrder.id,
            EngineeringChangeOrder.eco_number,
            EngineeringChangeOrder.status,
            EngineeringChangeOrder.affected_documents,
        )
        .filter(
            EngineeringChangeOrder.company_id == company_id,
            EngineeringChangeOrder.affected_documents.isnot(None),
            EngineeringChangeOrder.affected_documents.contains(str(document_id)),
        )
        .order_by(EngineeringChangeOrder.id)
        .all()
    )
    listing = [eco for eco in candidates if document_id in _eco_document_ids(eco.affected_documents)]
    if not listing:
        return None, None

    editable = [eco for eco in listing if eco.status in _ECO_SCOPE_EDITABLE_STATUSES]
    recorded = [eco for eco in listing if eco.status not in _ECO_SCOPE_EDITABLE_STATUSES]
    permanent = None
    if recorded:
        status = recorded[0].status.value.replace("_", " ") if recorded[0].status else "unknown status"
        permanent = (
            f"This document is listed as affected by engineering change {recorded[0].eco_number} ({status}), which "
            "keeps the record of what the change covered. It must remain available and cannot be deleted."
        )
    release = None
    if editable:
        # Every one is named with its edit path: the Engineering Changes page has no document
        # field and the ECO list cannot be searched by document, so "and N others" would leave
        # the user no way to find them.
        named = [f"{eco.eco_number} (PUT /eco/eco/{eco.id})" for eco in editable]
        if len(named) == 1:
            which, scope = f"engineering change {named[0]}", "that change's"
        else:
            which = f"engineering changes {', '.join(named[:-1])} and {named[-1]}"
            scope = "each of those changes'"
        release = (
            f"This document is listed as affected by {which}. Take it out of {scope} affected_documents first, "
            "while no approver has decided on it yet."
        )
    return permanent, release


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
    # Who may open it: standard / restricted / export_controlled (app.services.document_access).
    access_level: str = DocumentAccessLevel.STANDARD.value
    created_at: datetime

    class Config:
        from_attributes = True
        use_enum_values = True


class DocumentUploadResponse(DocumentResponse):
    """The uploaded document, plus whether the server stored it above the level that was asked for."""

    access_level_requested: str = DocumentAccessLevel.STANDARD.value
    access_level_raised: bool = False
    # Why, in order: "predecessor_revision" (a revision is never easier to open than the one it
    # supersedes), "export_controlled_customer" (the linked part's customer is export-controlled).
    access_level_raised_because: List[str] = Field(default_factory=list)


class DocumentAccessLevelChangeResponse(DocumentResponse):
    previous_access_level: str
    # Active users of the company who may open an export_controlled document. 0 with an
    # export_controlled level means nobody can open it until someone is authorized.
    authorized_user_count: int


class WorkOrderDocumentAttachRequest(BaseModel):
    work_order_id: int


def _refuse_api_token_access_level(current_user: User) -> None:
    """403 for ANY API token on the access-level verb, before anything is read or written."""
    if is_api_token_credential(current_user):
        raise HTTPException(status_code=403, detail=API_TOKEN_DOCUMENT_ACCESS_LEVEL)


def _access_level_or_422(value: Optional[str]) -> Optional[str]:
    if value is None or not value.strip():
        return None
    level = value.strip()
    if level not in ACCESS_LEVEL_ORDER:
        raise HTTPException(status_code=422, detail=f"access_level must be one of {', '.join(ACCESS_LEVEL_ORDER)}")
    return level


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
    access_level: Optional[str] = Query(None, description="standard, restricted or export_controlled"),
    search: Optional[str] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=5000),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """List the active company's documents the caller may open, newest first.

    Only documents whose ``access_level`` the caller may open are listed: ``restricted`` needs
    Admin, Manager, Quality or Supervisor; ``export_controlled`` needs the caller's own export
    authorization, whatever their role. A document the caller may not open is simply absent.
    Filters: part, work order, vendor, document type, access level, and a free-text search over
    number, title and description.
    """
    query = _document_query(db, company_id, current_user)
    level_filter = _access_level_or_422(access_level)
    if level_filter:
        query = query.filter(Document.access_level == level_filter)

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


@router.post("/upload", response_model=DocumentUploadResponse)
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
    access_level: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(DOCUMENT_WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Upload and release a document as Admin/Manager/Quality, with atomic audit.

    Revisions retain the previous document's type and linked records; existing
    files remain available. Failure to save the required audit refuses publication.

    ``access_level`` (standard, restricted or export_controlled; default standard) says who
    may open it. The server only ever RAISES it: a revision is stored at least at its
    predecessor's level, and a document whose linked part's customer is export-controlled is
    stored ``export_controlled``. The response says what was asked for and why it was raised
    (``access_level_requested`` / ``access_level_raised`` / ``access_level_raised_because``).
    A predecessor the caller may not open is the same 404 as a missing one.
    """
    requested_access_level = _access_level_or_422(access_level)
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
        # A predecessor the uploader may not open is the same 404 as a missing one.
        previous = (
            db.query(Document)
            .filter(
                Document.id == previous_revision_id,
                Document.company_id == company_id,
                visible_documents_clause(current_user),
            )
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

    stored_level = resolve_upload_access_level(
        db, company_id, requested=requested_access_level, part_id=normalized_part_id, previous=previous
    )

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
        access_level=stored_level.access_level,
        released_by=current_user.id,
        released_at=datetime.utcnow(),
        created_by=current_user.id,
        company_id=company_id,
    )

    extra_data = {"release": True, "content_sha256": hashlib.sha256(content).hexdigest()}
    if stored_level.raised:
        extra_data["access_level_requested"] = stored_level.requested
        extra_data["access_level_raised_because"] = list(stored_level.raised_because)
    try:
        db.add(document)
        _commit_audited_document(db, audit, document, action="CREATE", extra_data=extra_data)
    except Exception:
        # A refused publication must not retain newly uploaded bytes. Earlier
        # revision files are never touched by this compensation.
        try:
            storage.delete(file_path)
        except Exception:
            logger.error("Could not remove an unpublished document upload")
        raise
    # Echoed back even when the stored level is one the uploader cannot open (they supplied it).
    written = _written_document_query(db, company_id).filter(Document.id == document.id).one()
    response = DocumentUploadResponse.model_validate(written)
    response.access_level_requested = stored_level.requested
    response.access_level_raised = stored_level.raised
    response.access_level_raised_because = list(stored_level.raised_because)
    return response


@router.get("/types/list")
def list_document_types(current_user: User = Depends(get_current_user)):
    return [{"value": t.value, "label": t.value.replace("_", " ").title()} for t in DocumentType]


@router.get("/access-review", response_model=DocumentAccessReviewResponse)
def get_document_access_review(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(DOCUMENT_ACCESS_ROLES)),
    company_id: int = Depends(get_current_company_id),
):
    """Documents that probably should be export_controlled and are not (Admin or Quality).

    Lists every document of the active company whose linked part's customer is
    export-controlled (a controlling customer contract or handling instruction, the same
    finding that redacts an AI-external connector) but whose ``access_level`` is still
    ``standard`` or ``restricted``: id, number, revision, title, type, level, part, customer
    and the customer's classification. ``unlinked_document_count`` counts the documents below
    ``export_controlled`` that have no part link, which the server cannot judge. Pure read; fix
    a row with ``POST /documents/{document_id}/access-level``.
    """
    review = document_access_review(db, company_id, current_user)
    return DocumentAccessReviewResponse(
        documents=[DocumentAccessReviewRow(**row.__dict__) for row in review.documents],
        document_count=len(review.documents),
        unlinked_document_count=review.unlinked_document_count,
    )


@router.get("/{document_id}/revisions", response_model=List[DocumentResponse])
def get_document_revisions(
    document_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """The revision history the document belongs to, newest first.

    404 when the caller may not open the document itself. The history is traced through
    every revision, but a revision the caller may not open is left out of the answer.
    """
    visible_document_or_404(db, company_id, current_user, document_id)
    chain = tenant_query(db, Document, company_id).with_entities(Document.id, Document.previous_revision_id).all()
    previous_of = {row.id: row.previous_revision_id for row in chain}
    root_id = document_id
    seen = set()
    while root_id not in seen and previous_of.get(root_id) in previous_of:
        seen.add(root_id)
        root_id = previous_of[root_id]
    linked = {root_id}
    while True:
        children = {doc_id for doc_id, previous_id in previous_of.items() if previous_id in linked}
        if children.issubset(linked):
            break
        linked.update(children)
    documents = _document_query(db, company_id, current_user).filter(Document.id.in_(sorted(linked))).all()
    return sorted(documents, key=lambda doc: doc.created_at, reverse=True)


@router.get("/{document_id}", response_model=DocumentResponse)
def get_document(
    document_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """One document's metadata. 404 when it does not exist or the caller may not open it."""
    document = _document_query(db, company_id, current_user).filter(Document.id == document_id).first()
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
    """The document's file, as an attachment. 404 when it does not exist or the caller may not open it."""
    from fastapi.responses import FileResponse, StreamingResponse

    document = visible_document_or_404(db, company_id, current_user, document_id)

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
    """Attach an unlinked PDF to a live tenant work order; existing history stays bound.

    A document the caller may not open is the same 404 as a missing one, before any other check.
    """
    try:
        attach_document_command(db, document_id, payload.work_order_id, company_id, audit, viewer=current_user)
        db.commit()
        return _document_query(db, company_id, current_user).filter(Document.id == document_id).one()
    except AuditWriteError as exc:
        db.rollback()
        raise HTTPException(503, "Unable to save audit record") from exc
    except Exception:
        db.rollback()
        raise


@router.post("/{document_id}/access-level", response_model=DocumentAccessLevelChangeResponse)
def change_access_level(
    document_id: int,
    payload: DocumentAccessLevelChange,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(DOCUMENT_ACCESS_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Change who may open a document (Admin or Quality), with a required reason.

    Levels: ``standard`` (anyone who can read documents), ``restricted`` (Admin, Manager,
    Quality, Supervisor), ``export_controlled`` (ONLY users marked export-authorized; no role
    bypasses it). The caller must be able to open the document now (**404** otherwise, the same
    as a missing one), so lowering an export_controlled document takes an export-authorized
    person. **409** when the document is already at that level. Any API token is refused
    **403** before anything is read. Recorded as a required ``ACCESS_LEVEL_CHANGE`` audit row
    with the old and new level and the reason, in the same transaction (**503**, nothing saved,
    if it cannot be written). The response carries ``authorized_user_count``, the company's
    active export-authorized users: 0 with ``export_controlled`` means nobody can open it yet.
    """
    _refuse_api_token_access_level(current_user)
    new_level = payload.access_level.value
    authorized = export_authorized_user_count(db, company_id)
    with required_audit(db, REQUIRED_AUDIT_UNAVAILABLE_DETAIL):
        document, previous_level = change_document_access_level(
            db,
            company_id=company_id,
            user=current_user,
            document_id=document_id,
            access_level=new_level,
            reason=payload.reason,
            audit=audit,
            authorized_user_count=authorized,
        )
        db.commit()
    written = _written_document_query(db, company_id).filter(Document.id == document.id).one()
    response = DocumentAccessLevelChangeResponse.model_validate(
        {
            **DocumentResponse.model_validate(written).model_dump(),
            "previous_access_level": previous_level,
            "authorized_user_count": authorized,
        }
    )
    return response


@router.delete("/{document_id}")
def delete_document(
    document_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Delete an unretained document as Admin/Manager, preserving audited metadata.

    Refused 409, before the audit write and before any mutation, while the document is
    retained: part of a revision history, a receipt's certificate or receiving label, a
    shipment's carrier label or bill of lading, captured process-sheet step evidence, a
    governed file record, Hank intake evidence, or linked from a QMS standard, QMS clause
    evidence, a laser nest or an engineering change's affected documents (a removed QMS
    link or nest, a nest with recorded work, or an ECO approved or later keeps it too;
    permanent reasons are reported before any unlink instruction). Every table that
    holds a foreign key to ``documents.id`` is checked here: on Postgres none of them has
    an ``ON DELETE``, so an unchecked one fails at flush as a 500 that gives no reason.

    A document the caller may not open is the same 404 as a missing one, before any of these.
    """
    document = visible_document_or_404(db, company_id, current_user, document_id, for_update=True)
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

    # Every reason that keeps the document for good is reported before any instruction
    # to unlink a QMS row or detach a nest, so nobody follows an instruction (an audited
    # change, and a nest losing its drawing on the floor) only to be refused again. The
    # instructions themselves are given together, so one round of unlinking is enough.
    retained = _retained_record_refusal(db, company_id, document.id)
    if retained:
        raise HTTPException(409, retained)
    qms_permanent, qms_release = _qms_link_blockers(db, company_id, document.id)
    nest_permanent, nest_release = _laser_nest_blockers(db, company_id, document.id)
    eco_permanent, eco_release = _eco_blockers(db, company_id, document.id)
    permanent = qms_permanent or nest_permanent or eco_permanent
    if permanent:
        raise HTTPException(409, permanent)
    release = " ".join(text for text in (qms_release, nest_release, eco_release) if text)
    if release:
        raise HTTPException(409, release)

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
