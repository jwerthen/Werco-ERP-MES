"""Admin verbs for long-lived, per-user API tokens (``/api/v1/api-tokens``).

An API token is the credential a non-interactive client -- the Werco Assistant
bot, an MCP client such as Cursor -- presents instead of the 15-minute access
token. It is bound to ONE real user and is exactly as powerful as that user:
``require_role`` decides on every route as it does for the SPA, tenancy is
pinned to the row's company, and a revoke takes effect on the holder's very
next request. Minting, listing and revoking are an Admin's interactive act:
an API token is path-fenced (403) from this router and from ``/auth``, so a
token can never mint, list or revoke tokens, refresh, log out or switch
company -- the direct verbs; an Admin-held token is still an Admin on
``/users``, so the bound user's role is the real boundary. No token is ever
bound to a platform admin / superuser (409 at mint, refused at use), and
deactivating a token's user revokes it. The service
(``app.services.api_token_service``) audits issuance and revocation with
metadata only -- the plaintext token is returned exactly once and never
stored -- and every write a token makes carries an audit marker naming it.
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_audit_service, get_current_company_id, require_role
from app.db.database import get_db
from app.models.user import User, UserRole
from app.schemas.api_token import (
    ApiTokenCreate,
    ApiTokenIssueResponse,
    ApiTokenListResponse,
    ApiTokenResponse,
    ApiTokenRevoke,
)
from app.services import api_token_service
from app.services.audit_service import AuditService

router = APIRouter()

_API_TOKEN_ADMIN_ROLES = [UserRole.ADMIN]


@router.post("/", response_model=ApiTokenIssueResponse, status_code=201, summary="Issue an API token")
def create_api_token(
    payload: ApiTokenCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_API_TOKEN_ADMIN_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Mint a long-lived, revocable API token for one user of this company (ADMIN only).

    The returned ``token`` is shown ONCE. It is never stored and can never be
    retrieved again -- lose it and you revoke this one and mint another. Its
    power is exactly the target user's role: it cannot do anything that user
    could not do from the SPA, it never switches company, and it is refused
    (403) on every ``/auth`` and ``/api-tokens`` route. Omit ``expires_days``
    for a token that never expires (the default for a standing bot); the
    target must belong to this company (404 otherwise), be active (409) and
    not be a platform admin / superuser (409 -- no token ever acts as one).
    Issuance is audited, every write the token makes carries an audit marker
    naming it, and deactivating its user revokes it; a dedicated bot user
    account is recommended.
    """
    record, token = api_token_service.issue_api_token(
        db,
        company_id=company_id,
        user_id=payload.user_id,
        label=payload.label,
        expires_days=payload.expires_days,
        created_by=current_user.id,
        audit=audit,
    )
    return ApiTokenIssueResponse(**ApiTokenResponse.model_validate(record).model_dump(), token=token)


@router.get("/", response_model=ApiTokenListResponse, summary="List API tokens")
def list_api_tokens(
    user_id: Optional[int] = Query(None, gt=0, description="Only tokens held by this user."),
    include_revoked: bool = Query(False, description="Include revoked tokens (the record of who held access)."),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_API_TOKEN_ADMIN_ROLES)),
    company_id: int = Depends(get_current_company_id),
):
    """List this company's API tokens, newest first (ADMIN only; metadata only, never a secret).

    Each row carries the holder (``user_id``), its ``label``, ``jti_prefix``
    (the correlation handle, not a credential), ``expires_at`` (null = never),
    the revocation trail and ``last_used_at`` (touched at most once per five
    minutes, so it is a liveness marker, not a hit counter).
    """
    records = api_token_service.list_api_tokens(
        db, company_id=company_id, user_id=user_id, include_revoked=include_revoked
    )
    return ApiTokenListResponse(api_tokens=[ApiTokenResponse.model_validate(record) for record in records])


@router.post("/{token_id}/revoke", response_model=ApiTokenResponse, summary="Revoke an API token")
def revoke_api_token(
    token_id: int,
    payload: ApiTokenRevoke,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_API_TOKEN_ADMIN_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Revoke an API token with a reason (ADMIN only, tenant-scoped, audited, one-way).

    The holder is refused (401) on its very next request -- the row is
    re-read on every call. Revocation is a status flip, never a delete: the
    row stays as the record of who held access, and a second revoke is 409
    because the first revocation's reason, actor and instant are the record.
    404 when the token is not this company's.
    """
    record = api_token_service.revoke_api_token(
        db,
        company_id=company_id,
        token_id=token_id,
        revoked_by=current_user.id,
        reason=payload.reason,
        audit=audit,
    )
    return ApiTokenResponse.model_validate(record)
