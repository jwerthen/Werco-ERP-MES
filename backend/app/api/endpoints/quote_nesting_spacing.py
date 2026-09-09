"""Explicit governance of company quoting allowances, separate from quote approval."""

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_audit_service, get_current_company_id, require_role
from app.api.endpoints.quote_nesting_drafts import read_user
from app.db.database import atomic_transaction, get_db
from app.models.user import User, UserRole
from app.schemas.quote_nesting_spacing import (
    CreateSpacingRevision,
    PublishSpacingRevision,
    ResolveSpacingRequest,
    SpacingCommandResponse,
    SpacingResolutionResponse,
    SpacingRevisionResponse,
    SpacingStateResponse,
    WithdrawSpacingPublication,
)
from app.services import quote_nesting_spacing as service
from app.services.audit_service import AuditService, AuditWriteError

router = APIRouter()


def policy_admin(
    db: Session = Depends(get_db),
    user: User = Depends(require_role([UserRole.ADMIN])),
    company_id: int = Depends(get_current_company_id),
) -> User:
    service.require_policy_write(db, user, company_id)
    return user


def command(db: Session, function, *args):
    try:
        with atomic_transaction(db):
            return function(db, *args)
    except AuditWriteError as exc:
        raise HTTPException(
            503, "Policy decision was not saved because required audit evidence failed. Retry the same request."
        ) from exc
    except IntegrityError as exc:
        raise HTTPException(409, "Policy history changed concurrently. Refresh or retry the same request.") from exc


@router.get("", response_model=SpacingStateResponse)
def get_quote_nesting_spacing_state(
    page: int = Query(1, ge=1, le=100000),
    per_page: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(read_user),
    company_id: int = Depends(get_current_company_id),
):
    """Read current selection and bounded revision/publication history without creating defaults."""
    return service.state(db, company_id, page=page, per_page=per_page)


@router.get("/revisions/{number}", response_model=SpacingRevisionResponse)
def get_quote_nesting_spacing_revision(
    number: int = Path(..., ge=1),
    db: Session = Depends(get_db),
    user: User = Depends(read_user),
    company_id: int = Depends(get_current_company_id),
):
    """Read the immutable policy content and its exact server digest."""
    return service.get_revision(db, company_id, number)


@router.post("/revisions", response_model=SpacingCommandResponse)
def create_quote_nesting_spacing_revision(
    body: CreateSpacingRevision,
    db: Session = Depends(get_db),
    user: User = Depends(policy_admin),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Append an immutable draft and required audit, with company-version and UUID retry checks."""
    return command(db, service.create_revision, user, company_id, audit, body)


@router.post("/publications", response_model=SpacingCommandResponse)
def publish_quote_nesting_spacing_revision(
    body: PublishSpacingRevision,
    db: Session = Depends(get_db),
    user: User = Depends(policy_admin),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Explicit Admin approval binds an exact revision; immediate or future effectiveness only."""
    return command(db, service.publish, user, company_id, audit, body)


@router.post("/publications/{publication_id}/withdraw", response_model=SpacingCommandResponse)
def withdraw_quote_nesting_spacing_publication(
    body: WithdrawSpacingPublication,
    publication_id: int = Path(..., ge=1),
    db: Session = Depends(get_db),
    user: User = Depends(policy_admin),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Append a reasoned withdrawal; history stays immutable and no earlier approval reactivates."""
    return command(db, service.withdraw, user, company_id, audit, publication_id, body)


@router.post("/resolve", response_model=SpacingResolutionResponse)
def resolve_quote_nesting_spacing_policy(
    body: ResolveSpacingRequest,
    db: Session = Depends(get_db),
    user: User = Depends(read_user),
    company_id: int = Depends(get_current_company_id),
):
    """Pure read: resolve the current band using pinned decimal arithmetic; apply remains explicit."""
    try:
        return service.resolve(db, company_id, body.material, body.thickness_in)
    except ValueError as exc:
        raise HTTPException(422, "Invalid policy thickness or immutable policy content") from exc
