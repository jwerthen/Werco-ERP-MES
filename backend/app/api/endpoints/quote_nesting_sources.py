"""Explicit original DXF evidence attached to an immutable saved revision."""

import json
from typing import Callable, TypeVar
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_current_company_id, get_current_user, oauth2_scheme, require_role
from app.db.database import get_db
from app.models.user import User, UserRole
from app.schemas.quote_nesting_sources import (
    MAX_SOURCE_BYTES,
    CreateSourceIntent,
    FinalizeSource,
    SourceIntentResponse,
    SourcePageResponse,
)
from app.services import quote_nesting_sources as service
from app.services.audit_service import AuditService, AuditWriteError

router = APIRouter()
T = TypeVar('T')


def source_context(
    request: Request,
    draft_id: int = Path(ge=1, le=2147483647),
    number: int = Path(ge=1, le=2147483647),
    db: Session = Depends(get_db),
    user: User = Depends(require_role(list(UserRole))),
    company_id: int = Depends(get_current_company_id),
    token: str = Depends(oauth2_scheme),
) -> service.SourceContext:
    # Each later mutation phase rechecks current auth/role state. A captured
    # ORM user may be expired after commit or have been disabled during I/O.
    return service.SourceContext(
        db,
        company_id,
        draft_id,
        number,
        lambda: get_current_user(request=request, db=db, token=token),
        lambda actor: AuditService(db, actor, request),
    )


def _execute(ctx: service.SourceContext, action: Callable[[], T]) -> T:
    try:
        return action()
    except AuditWriteError as exc:
        ctx.db.rollback()
        raise HTTPException(503, 'Required source audit could not be recorded. Recover using the same intent.') from exc
    except IntegrityError as exc:
        ctx.db.rollback()
        raise HTTPException(
            409, 'Source attachment changed concurrently. Reload this saved revision before retrying.'
        ) from exc


async def _unique_json(request: Request) -> None:
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate')
            result[key] = value
        return result

    try:
        json.loads(await request.body(), object_pairs_hook=object_pairs)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise HTTPException(422, 'Provide a JSON command without duplicate fields') from exc


def _expected_company(ctx: service.SourceContext, value: int) -> None:
    if value != ctx.company_id:
        raise HTTPException(409, 'Active company changed; reopen this saved revision')


@router.get('/{draft_id}/revisions/{number}/sources', response_model=SourcePageResponse)
def list_quote_nesting_sources(
    page: int = Query(default=1, ge=1, le=100000),
    per_page: int = Query(default=10, ge=1, le=10),
    ctx: service.SourceContext = Depends(source_context),
):
    """List historical source intents/receipts without reading storage or changing data."""
    return service.list_sources(ctx, page, per_page)


@router.post('/{draft_id}/revisions/{number}/sources', response_model=SourceIntentResponse)
async def create_quote_nesting_source(
    command: CreateSourceIntent,
    request: Request,
    ctx: service.SourceContext = Depends(source_context),
):
    """Commit an exact actor/credential-bound intent and its required audit before bytes are stored."""
    await _unique_json(request)
    return await run_in_threadpool(_execute, ctx, lambda: service.create_intent(ctx, command))


@router.post('/{draft_id}/revisions/{number}/sources/{intent_id}/content', response_model=SourceIntentResponse)
async def upload_quote_nesting_source(
    request: Request,
    intent_id: int = Path(ge=1, le=2147483647),
    expected_company_id: int = Query(ge=1, le=2147483647),
    ctx: service.SourceContext = Depends(source_context),
):
    """Verify raw bytes, recover earlier attempts, or audit one fresh write before immutable completion."""
    _expected_company(ctx, expected_company_id)
    if request.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'application/octet-stream':
        raise HTTPException(415, 'Send one original DXF as application/octet-stream')
    ctx.db.rollback()
    content = await request.body()
    if not content or len(content) > MAX_SOURCE_BYTES:
        raise HTTPException(413, 'Original DXF must contain fewer than 5,000,000 bytes')
    return await run_in_threadpool(_execute, ctx, lambda: service.upload_source(ctx, intent_id, content))


@router.post('/{draft_id}/revisions/{number}/sources/{intent_id}/finalize', response_model=SourceIntentResponse)
async def finalize_quote_nesting_source(
    command: FinalizeSource,
    request: Request,
    intent_id: int = Path(ge=1, le=2147483647),
    ctx: service.SourceContext = Depends(source_context),
):
    """Recover a matching tracked object without uploading, repairing or deleting any bytes."""
    await _unique_json(request)
    _expected_company(ctx, command.expected_company_id)
    return await run_in_threadpool(_execute, ctx, lambda: service.finalize_source(ctx, intent_id))


@router.get('/{draft_id}/revisions/{number}/sources/{intent_id}/download')
def download_quote_nesting_source(
    intent_id: int = Path(ge=1, le=2147483647),
    ctx: service.SourceContext = Depends(source_context),
):
    """Release an authenticated attachment only after bounded current byte/hash verification."""
    metadata, content = _execute(ctx, lambda: service.download_source(ctx, intent_id))
    return Response(
        content,
        media_type='application/octet-stream',
        headers={
            'Content-Disposition': "attachment; filename*=UTF-8''" + quote(metadata['source_name'], safe=''),
            'Cache-Control': 'private, no-store',
            'X-Content-Type-Options': 'nosniff',
        },
    )
