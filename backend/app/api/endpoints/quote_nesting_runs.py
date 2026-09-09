"""Explicit unapproved server calculations; HTTP requests never execute geometry."""

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_audit_service, get_current_company_id
from app.api.endpoints.quote_nesting_drafts import read_user, write_user
from app.db.database import atomic_transaction, get_db
from app.models.user import User
from app.schemas.quote_nesting_runs import (
    CancelRunRequest,
    RunCheckpointResponse,
    RunDetail,
    RunPage,
    RunReport,
    RuntimeReadiness,
    StartRunRequest,
)
from app.services import quote_nesting_runs as service
from app.services.audit_service import AuditService, AuditWriteError
from app.services.nesting_runtime import runtime_status

router = APIRouter()


@router.post("", response_model=RunDetail)
async def create_quote_nesting_run(
    body: StartRunRequest,
    db: Session = Depends(get_db),
    user: User = Depends(write_user),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Queue the exact immutable input revision; UUID retry returns the same unapproved run."""
    readiness = await runtime_status()
    identity = readiness["identity"]
    runtime = (
        {key: identity[key] for key in ("release", "protocol", "solver_version", "bundle_sha256", "node_version")}
        if readiness["available"]
        else None
    )

    def save():
        with atomic_transaction(db):
            return service.start_run(db, user, company_id, audit, body, runtime=runtime)

    try:
        return await run_in_threadpool(save)
    except AuditWriteError as exc:
        raise HTTPException(
            503, "Calculation was not queued because its audit could not be recorded. Retry the same request."
        ) from exc
    except IntegrityError as exc:
        raise HTTPException(
            409, "A concurrent calculation request changed this company. Refresh or retry the same request."
        ) from exc


@router.get("", response_model=RunPage)
def list_quote_nesting_runs(
    page: int = Query(1, ge=1, le=100000),
    per_page: int = Query(20, ge=1, le=100),
    draft_id: int | None = Query(None, ge=1),
    revision_number: int | None = Query(None, ge=1),
    db: Session = Depends(get_db),
    user: User = Depends(read_user),
    company_id: int = Depends(get_current_company_id),
):
    """Read company calculation summaries without geometry, audit or other writes."""
    return service.list_runs(
        db,
        company_id,
        page=page,
        per_page=per_page,
        draft_id=draft_id,
        revision_number=revision_number,
        user=user,
    )


@router.get("/runtime", response_model=RuntimeReadiness)
async def get_quote_nesting_runtime(
    user: User = Depends(read_user),
):
    """Read fresh worker release identity only; missing or stale readiness blocks new runs."""
    return await runtime_status()


@router.get("/{run_id}", response_model=RunDetail)
def get_quote_nesting_run(
    run_id: int = Path(..., ge=1),
    db: Session = Depends(get_db),
    user: User = Depends(read_user),
    company_id: int = Depends(get_current_company_id),
):
    """Read status and checkpoint metadata for one exact saved calculation."""
    return service.run_detail(db, service.get_run(db, company_id, run_id), user=user, authorize=True)


@router.get("/{run_id}/checkpoints/{sequence}", response_model=RunCheckpointResponse)
def get_quote_nesting_run_checkpoint(
    run_id: int = Path(..., ge=1),
    sequence: int = Path(..., ge=1, le=36),
    db: Session = Depends(get_db),
    user: User = Depends(read_user),
    company_id: int = Depends(get_current_company_id),
):
    """Read one immutable internal-mm result with its server content hash."""
    return service.get_checkpoint(db, company_id, run_id, sequence, user=user)


@router.post("/{run_id}/cancel", response_model=RunDetail)
def cancel_quote_nesting_run(
    body: CancelRunRequest,
    run_id: int = Path(..., ge=1),
    db: Session = Depends(get_db),
    user: User = Depends(write_user),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Request cancellation with version/context checks; completed checkpoints are retained."""
    try:
        with atomic_transaction(db):
            return service.cancel_run(
                db, user, company_id, audit, run_id, body.expected_company_id, body.expected_version
            )
    except AuditWriteError as exc:
        raise HTTPException(503, "Cancellation was not recorded because its audit failed. Refresh and retry.") from exc
    except IntegrityError as exc:
        raise HTTPException(409, "Calculation changed concurrently. Refresh before cancelling.") from exc


@router.get("/{run_id}/report", response_model=RunReport)
def export_quote_nesting_run_report(
    run_id: int = Path(..., ge=1),
    db: Session = Depends(get_db),
    user: User = Depends(read_user),
    company_id: int = Depends(get_current_company_id),
):
    """Export exact saved inputs and available checked results as unapproved draft evidence."""
    return service.export_report(db, company_id, run_id, user=user)
