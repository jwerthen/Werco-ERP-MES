"""Explicit Save/Open of unapproved nesting inputs, separate from operational nests."""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile

from app.api.deps import get_audit_service, get_current_company_id, require_role
from app.db.database import atomic_transaction, get_db
from app.models.user import User, UserRole
from app.schemas.quote_nesting_drafts import MAX_ESTIMATE_BYTES, DraftHistoryResponse, DraftRevisionResponse
from app.services import quote_nesting_drafts as service
from app.services.audit_service import AuditService, AuditWriteError

router = APIRouter()


def read_user(
    db: Session = Depends(get_db),
    user: User = Depends(require_role(list(UserRole))),
    company_id: int = Depends(get_current_company_id),
) -> User:
    service.require_access(db, user, company_id)
    return user


def write_user(
    db: Session = Depends(get_db),
    user: User = Depends(require_role(list(UserRole))),
    company_id: int = Depends(get_current_company_id),
) -> User:
    service.require_access(db, user, company_id, write=True)
    return user


def upload_schema(append: bool) -> dict:
    properties = {
        "estimate": {"type": "string", "format": "binary"},
        "request_key": {"type": "string", "format": "uuid"},
        "expected_company_id": {"type": "integer", "minimum": 1},
    }
    if append:
        properties["expected_version"] = {"type": "integer", "minimum": 1}
    return {
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "properties": properties,
                        "required": list(properties),
                        "additionalProperties": False,
                    }
                }
            },
        }
    }


async def read_upload(request: Request, *, append: bool) -> dict:
    if not request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
        raise HTTPException(415, "Upload the estimate as multipart/form-data")
    # This runs after authentication; the ASGI middleware already capped the raw
    # request before its parser can spool files. The file-specific cap remains exact.
    async with request.form(max_files=1, max_fields=4 if append else 3, max_part_size=4096) as form:
        expected = {"estimate", "request_key", "expected_company_id"}
        if append:
            expected.add("expected_version")
        if len(form.multi_items()) != len(expected) or set(form) != expected:
            raise HTTPException(422, "Provide exactly the documented upload fields, without duplicates")
        estimate = form["estimate"]
        if not isinstance(estimate, UploadFile):
            raise HTTPException(422, "estimate must be a JSON file")
        content = await estimate.read(MAX_ESTIMATE_BYTES + 1)
        if len(content) > MAX_ESTIMATE_BYTES:
            raise HTTPException(413, "Saved estimates are limited to 5 MiB")
        result = {"content": content, "request_key": form["request_key"]}
        if not isinstance(result["request_key"], str) or len(result["request_key"]) > 36:
            raise HTTPException(422, "request_key must be a UUID")
        for field in ["expected_company_id"] + (["expected_version"] if append else []):
            value = form[field]
            if not isinstance(value, str) or not value.isascii() or not value.isdigit() or len(value) > 10:
                raise HTTPException(422, field + " must be a positive integer")
            result[field] = int(value)
            if result[field] < 1 or result[field] > 2147483647:
                raise HTTPException(422, field + " must be a positive 32-bit integer")
        return result


def save(db: Session, user: User, company_id: int, audit: AuditService, values: dict) -> dict:
    try:
        with atomic_transaction(db):
            return service.save_revision(db, user, company_id, audit, **values)
    except AuditWriteError as exc:
        raise HTTPException(
            503, "Draft was not saved because its audit record could not be recorded. Retry the same save."
        ) from exc
    except IntegrityError as exc:
        raise HTTPException(
            409, "A concurrent save changed this draft. Retry the same request to recover its result."
        ) from exc


@router.get("", response_model=DraftHistoryResponse)
def list_quote_nesting_drafts(
    page: int = Query(1, ge=1, le=100000),
    per_page: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(read_user),
    company_id: int = Depends(get_current_company_id),
):
    """Read latest draft summaries in stable order; no geometry, audit, or business writes."""
    return service.list_drafts(db, company_id, page=page, per_page=per_page)


@router.get("/{draft_id}/revisions", response_model=DraftHistoryResponse)
def list_quote_nesting_draft_revisions(
    draft_id: int,
    page: int = Query(1, ge=1, le=100000),
    per_page: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(read_user),
    company_id: int = Depends(get_current_company_id),
):
    """Read immutable revision summaries; opening history never changes the current draft."""
    return service.list_drafts(db, company_id, draft_id=draft_id, page=page, per_page=per_page)


@router.get("/{draft_id}/revisions/{number}", response_model=DraftRevisionResponse)
def get_quote_nesting_draft_revision(
    draft_id: int,
    number: int,
    db: Session = Depends(get_db),
    user: User = Depends(read_user),
    company_id: int = Depends(get_current_company_id),
):
    """Open exact saved client inputs, retaining that revision's version for conflict checks."""
    return service.get_revision(db, company_id, draft_id, number)


@router.post("", response_model=DraftRevisionResponse, openapi_extra=upload_schema(False))
async def create_quote_nesting_draft(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(write_user),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Atomically save a new DRAFT and audit record; identical UUID retries return the original revision."""
    values = await read_upload(request, append=False)
    return await run_in_threadpool(save, db, user, company_id, audit, values)


@router.post("/{draft_id}/revisions", response_model=DraftRevisionResponse, openapi_extra=upload_schema(True))
async def append_quote_nesting_draft_revision(
    draft_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(write_user),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Append immutable inputs after expected-version validation; stale edits return 409 without writes."""
    values = await read_upload(request, append=True)
    values["draft_id"] = draft_id
    return await run_in_threadpool(save, db, user, company_id, audit, values)
