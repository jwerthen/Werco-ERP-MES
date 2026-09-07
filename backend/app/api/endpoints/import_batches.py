"""Reviewed import batches: persist receipts, correct failed rows, safely resume.

The router's Import Batches tag is excluded from generic MCP tool generation.
Committing the legacy WO/PO cutover formats creates open jobs / issued orders;
that explicit reviewed UI workflow must not become a generic assistant write tool.
"""

from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.api.deps import get_current_company_id, require_role
from app.db.database import get_db
from app.db.tenant_filter import tenant_query
from app.models.import_batch import ImportBatch, ImportBatchRow
from app.models.user import User, UserRole
from app.schemas.import_batch import ImportBatchHistory, ImportBatchResponse
from app.services import import_batch_service as service
from app.services.audit_service import AuditService
from app.services.import_service import MAX_IMPORT_FILE_BYTES

router = APIRouter()
import_user = require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])


async def _read_bounded(file):
    content = await file.read(MAX_IMPORT_FILE_BYTES + 1)
    if len(content) > MAX_IMPORT_FILE_BYTES:
        raise HTTPException(413, 'Import files are limited to 10 MB')
    return content


@router.get('', response_model=ImportBatchHistory)
def list_import_batches(
    entity: Optional[str] = None,
    offset: int = Query(0, ge=0, le=100000),
    db: Session = Depends(get_db),
    user: User = Depends(import_user),
    company_id: int = Depends(get_current_company_id),
):
    """List the latest 25 permitted batches, including durable partial-success receipts."""
    allowed = service.visible_entities(user)
    if entity:
        service.assert_import_role(user, entity)
        allowed = [entity]
    rows = (
        tenant_query(db, ImportBatch, company_id)
        .filter(ImportBatch.entity.in_(allowed))
        .order_by(ImportBatch.created_at.desc(), ImportBatch.id.desc())
        .offset(offset)
        .limit(26)
        .all()
    )
    return {
        'batches': [service.batch_response(db, company_id, user, row, include_rows=False) for row in rows[:25]],
        'has_more': len(rows) > 25,
    }


@router.post('/prepare', response_model=ImportBatchResponse)
async def prepare_import_batch(
    request: Request,
    entity: str = Form(...),
    request_key: str = Form(..., min_length=16, max_length=100),
    file: UploadFile = File(...),
    default_password: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(import_user),
    company_id: int = Depends(get_current_company_id),
):
    """Validate input and save its review; no business records are created."""
    return await service.prepare_batch(
        db, user, company_id, request, entity, file.filename, await _read_bounded(file), request_key, default_password
    )


@router.get('/{batch_id}', response_model=ImportBatchResponse)
def get_import_batch(
    batch_id: int,
    row_offset: int = Query(0, ge=0, le=100000),
    db: Session = Depends(get_db),
    user: User = Depends(import_user),
    company_id: int = Depends(get_current_company_id),
):
    """Read the durable receipt with 200 input rows per page; this never resumes writes."""
    batch = service.load_batch(db, company_id, user, batch_id)
    return service.batch_response(db, company_id, user, batch, row_offset)


@router.post('/{batch_id}/commit', response_model=ImportBatchResponse)
async def commit_import_batch(
    batch_id: int,
    request: Request,
    expected_version: int = Form(..., ge=1),
    credentials_file: Optional[UploadFile] = File(None),
    default_password: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(import_user),
    company_id: int = Depends(get_current_company_id),
):
    """Commit up to 25 reviewed groups. Retry after reading the receipt; created rows never repeat."""
    table = (
        await run_in_threadpool(service._parse, credentials_file.filename, await _read_bounded(credentials_file))
        if credentials_file
        else None
    )
    return await service.commit_batch(
        db, user, company_id, request, batch_id, expected_version, service._credential_rows(table), default_password
    )


@router.get('/{batch_id}/failed-rows.csv')
def download_failed_import_rows(
    batch_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(import_user),
    company_id: int = Depends(get_current_company_id),
):
    """Export only failed input rows with stable IDs for correction; passwords are never exported."""
    batch = service.load_batch(db, company_id, user, batch_id)
    content = service.failed_rows_csv(db, company_id, batch)
    count = (
        tenant_query(db, ImportBatchRow, company_id)
        .filter(ImportBatchRow.batch_id == batch.id, ImportBatchRow.status.in_(['invalid', 'failed']))
        .count()
    )
    AuditService(db, user, request).log(
        action='EXPORT',
        resource_type='import_batch',
        resource_id=batch.id,
        description='Downloaded failed import rows for correction',
        extra_data={'entity': batch.entity, 'failed_row_count': count},
    )
    db.commit()
    return Response(
        content,
        media_type='text/csv',
        headers={'Content-Disposition': f'attachment; filename="import-{batch.id}-failed-rows.csv"'},
    )


@router.post('/{batch_id}/corrections', response_model=ImportBatchResponse)
async def correct_import_batch(
    batch_id: int,
    request: Request,
    expected_version: int = Form(..., ge=1),
    file: UploadFile = File(...),
    default_password: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(import_user),
    company_id: int = Depends(get_current_company_id),
):
    """Validate corrections to failed rows only; a created record and its receipt are immutable."""
    return await service.correct_batch(
        db,
        user,
        company_id,
        request,
        batch_id,
        expected_version,
        file.filename,
        await _read_bounded(file),
        default_password,
    )
