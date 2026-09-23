"""Private document analysis followed by explicit, audited employee actions."""

import subprocess
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_audit_service, get_current_company_id, require_role
from app.db.database import get_db
from app.models.user import User
from app.schemas.hank_intake import (
    IntakeBatchList,
    IntakeBatchResponse,
    IntakeCommand,
    IntakeFileResponse,
    IntakePlanCommand,
    IntakeReceivingDraft,
    IntakeSourcePreview,
)
from app.schemas.hank_purchase_order import IntakePurchaseOrderDraft
from app.services.audit_service import AuditService, AuditWriteError
from app.services.hank_intake_purchase_order_service import HankIntakePurchaseOrderService
from app.services.hank_intake_receiving_service import HankIntakeReceivingService
from app.services.hank_intake_service import (
    MAX_BATCH_BYTES,
    MAX_FILE_BYTES,
    MAX_FILES,
    WRITE_ROLES,
    HankIntakeService,
    enqueue_intake,
    read_intake_document,
    read_verified_source,
)
from app.services.hank_office_reader import MIME_BY_FORMAT, detect_intake_format

router = APIRouter()


def _change(db, service, file_id, payload, audit, action):
    try:
        row = getattr(service, action)(file_id, payload, audit)
        response = service.response_file(row)
        db.commit()
    except AuditWriteError as exc:
        db.rollback()
        raise HTTPException(503, 'Required audit evidence could not be saved. Refresh intake before retrying.') from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, 'This intake changed. Refresh it before continuing.') from exc
    except Exception:
        db.rollback()
        raise
    if action == 'retry':
        enqueue_intake(response.id, response.version)
    return response


@router.post('/intake', response_model=IntakeBatchResponse)
def upload_intake(
    expected_company_id: int = Form(...),
    request_key: str = Form(...),
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_role(list(WRITE_ROLES))),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Save 1–5 documents durably, then enqueue bounded analysis; retry the UUID to recover."""
    service = HankIntakeService(db, user, company_id)
    service.authority(write=True)
    if expected_company_id != company_id:
        raise HTTPException(409, 'Active company changed. Reopen intake in the intended company.')
    if not 1 <= len(files) <= MAX_FILES:
        raise HTTPException(413, 'Upload between 1 and 5 documents.')
    db.rollback()
    buffered = []
    for file in files:
        content = file.file.read(MAX_FILE_BYTES + 1)
        if len(content) > MAX_FILE_BYTES:
            raise HTTPException(413, 'Each document must be no larger than 10 MB.')
        buffered.append((file.filename, content))
        if sum(len(content) for _, content in buffered) > MAX_BATCH_BYTES:
            raise HTTPException(413, 'The document batch must be no larger than 25 MB.')
    try:
        return service.upload(expected_company_id, request_key, buffered, audit)
    except AuditWriteError as exc:
        db.rollback()
        raise HTTPException(
            503, 'The required intake audit could not be saved. Retry with the same upload key.'
        ) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, 'This intake submission changed. Retry with the same upload key.') from exc
    except Exception:
        db.rollback()
        raise


@router.get('/intake', response_model=IntakeBatchList)
def list_intake(
    limit: int = Query(20, ge=1, le=50),
    before_id: int | None = Query(None, gt=0),
    db: Session = Depends(get_db),
    user: User = Depends(require_role(list(WRITE_ROLES))),
    company_id: int = Depends(get_current_company_id),
):
    return HankIntakeService(db, user, company_id).list(limit, before_id)


@router.get('/intake/files/{file_id}', response_model=IntakeFileResponse)
def get_intake_file(
    file_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(list(WRITE_ROLES))),
    company_id: int = Depends(get_current_company_id),
):
    """Recover one owned file directly from a saved work-queue link."""
    service = HankIntakeService(db, user, company_id)
    return service.response_file(service.file(file_id))


@router.get('/intake/{batch_id}', response_model=IntakeBatchResponse)
def get_intake(
    batch_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(list(WRITE_ROLES))),
    company_id: int = Depends(get_current_company_id),
):
    service = HankIntakeService(db, user, company_id)
    return service.response_batch(service.batch(batch_id))


@router.get('/intake/files/{file_id}/receiving-draft', response_model=IntakeReceivingDraft)
def intake_receiving_draft(
    file_id: int,
    purchase_order_id: int | None = Query(None, gt=0),
    db: Session = Depends(get_db),
    user: User = Depends(require_role(list(WRITE_ROLES))),
    company_id: int = Depends(get_current_company_id),
):
    """Match saved document evidence to open order lines without creating receipts."""
    return HankIntakeReceivingService(db, user, company_id).draft(file_id, purchase_order_id)


@router.get('/intake/files/{file_id}/source')
def intake_source(
    file_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(list(WRITE_ROLES))),
    company_id: int = Depends(get_current_company_id),
):
    """Return verified source bytes; only PDFs can be displayed inline."""
    row = HankIntakeService(db, user, company_id).file(file_id)
    ref, filename, digest, size = row.storage_ref, row.filename, row.content_sha256, row.file_size
    db.rollback()
    content = read_verified_source(ref, digest, size)
    try:
        source_format = detect_intake_format(content, filename)
    except ValueError as exc:
        raise HTTPException(409, 'The saved source format could not be verified.') from exc
    disposition = 'inline' if source_format == 'pdf' else 'attachment'
    return Response(
        content,
        media_type=MIME_BY_FORMAT[source_format],
        headers={
            'Content-Disposition': f"{disposition}; filename*=UTF-8''{quote(filename, safe='')}",
            'Cache-Control': 'private, no-store',
            'X-Content-Type-Options': 'nosniff',
        },
    )


@router.get('/intake/files/{file_id}/source-preview', response_model=IntakeSourcePreview)
def intake_source_preview(
    file_id: int,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(list(WRITE_ROLES))),
    company_id: int = Depends(get_current_company_id),
):
    """Read bounded native text without running Office content or another model call."""
    row = HankIntakeService(db, user, company_id).file(file_id)
    ref, filename, digest, size = row.storage_ref, row.filename, row.content_sha256, row.file_size
    db.rollback()
    content = read_verified_source(ref, digest, size)
    try:
        preview = read_intake_document(content, filename)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        raise HTTPException(422, 'This document cannot be safely previewed. Download and review its source.') from exc
    response.headers['Cache-Control'] = 'private, no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return IntakeSourcePreview.model_validate(preview)


@router.get('/intake/files/{file_id}/purchase-order-draft', response_model=IntakePurchaseOrderDraft)
def intake_purchase_order_draft(
    file_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(list(WRITE_ROLES))),
    company_id: int = Depends(get_current_company_id),
):
    """Resolve uploaded PO evidence for a reviewed import; creates no ERP records."""
    return HankIntakePurchaseOrderService(db, user, company_id).draft(file_id)


@router.post('/intake/files/{file_id}/plan', response_model=IntakeFileResponse)
def plan_intake(
    file_id: int,
    payload: IntakePlanCommand,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(list(WRITE_ROLES))),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    return _change(db, HankIntakeService(db, user, company_id), file_id, payload, audit, 'prepare')


@router.post('/intake/files/{file_id}/execute', response_model=IntakeFileResponse)
def execute_intake(
    file_id: int,
    payload: IntakeCommand,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(list(WRITE_ROLES))),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    return _change(db, HankIntakeService(db, user, company_id), file_id, payload, audit, 'execute')


@router.post('/intake/files/{file_id}/retry', response_model=IntakeFileResponse)
def retry_intake(
    file_id: int,
    payload: IntakeCommand,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(list(WRITE_ROLES))),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    return _change(db, HankIntakeService(db, user, company_id), file_id, payload, audit, 'retry')


@router.post('/intake/files/{file_id}/cancel', response_model=IntakeFileResponse)
def cancel_intake(
    file_id: int,
    payload: IntakeCommand,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(list(WRITE_ROLES))),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    return _change(db, HankIntakeService(db, user, company_id), file_id, payload, audit, 'cancel')
