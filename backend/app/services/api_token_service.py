"""Issue / list / revoke / resolve long-lived, per-user API tokens.

The credential a bot or an agent (the "Werco Assistant", an MCP client such as
Cursor) presents instead of the 15-minute interactive access token. Mirrors
``display_token_service``: a ``type="api"`` JWT anchored to a tenant-scoped
``api_tokens`` row that is looked up on EVERY request; revocation is the row;
issuance and revocation are audited through ``AuditService``; the JWT is shown
once and never stored (only its ``jti`` lands in the row).

Two functions matter for security, and the split between them is deliberate:

- :func:`check_api_token` is THE row check -- signature, row by ``jti``, not
  revoked, not past the row's ``expires_at``, JWT claims equal to the row
  (the ROW is authoritative; a forged claim can never widen tenancy), user
  exists in the row's company. It writes nothing. Both consumers run it --
  ``app.api.deps.get_current_user`` and the MCP door's ``ErpTokenVerifier`` --
  so there is exactly one copy of the rule. It does NOT decide ``is_active``:
  the two callers answer that differently (403 in-app, exactly as a disabled
  user's access token answers; 401 at the door, which has no user to name),
  so each applies it at its own status code.
- :func:`resolve_api_token_user` is the auth-path wrapper: the same check, then
  the ``last_used_at`` touch -- at most once per :data:`LAST_USED_TOUCH_INTERVAL`
  and only for an active user, committed immediately because it runs before
  any route logic (the session holds nothing else), with a conditional UPDATE
  so two workers racing the same token write once.

Every function that changes state owns its unit of work (commits at the end)
and writes the audit rows BEFORE that commit so the state change and its trail
land atomically (``AuditService`` only flushes). Nothing here logs, audits or
raises the token's plaintext -- only metadata and the first eight characters
of the ``jti`` (the list response's correlation handle; the ``jti`` alone
mints nothing without ``SECRET_KEY``).
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from fastapi import HTTPException, status
from sqlalchemy import or_
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.security import create_api_token, verify_api_token
from app.core.time_utils import to_utc_iso
from app.db.tenant_filter import tenant_query
from app.models.api_token import ApiToken
from app.models.user import User
from app.services.audit_service import AuditService

logger = logging.getLogger(__name__)

API_TOKEN_RESOURCE_TYPE = "api_token"
# Auth events (``resource_type="authentication"``, the row shape
# ``app.api.endpoints.auth.log_auth_event`` writes) so the token's lifecycle
# sits beside LOGIN_SUCCESS / TOKEN_REFRESHED in an authentication query.
AUTH_EVENT_ISSUED = "API_TOKEN_ISSUED"
AUTH_EVENT_REVOKED = "API_TOKEN_REVOKED"
# ``expires_days`` upper bound (ten years); ``None`` is the owner's default -- never.
MAX_API_TOKEN_EXPIRES_DAYS = 3650
# ``last_used_at`` is a coarse liveness marker, not a hit counter.
LAST_USED_TOUCH_INTERVAL = timedelta(minutes=5)


# --------------------------------------------------------------------------- auth path


def check_api_token(db: Session, token: str, *, now: Optional[datetime] = None) -> Optional[Tuple[User, ApiToken]]:
    """The ONE row check for an API token: ``(user, row)`` when it is live, else ``None``.

    Order, each a hard stop:
      1. signature (+ ``exp`` when the JWT carries one) + ``type == "api"``
      2. an ``api_tokens`` row exists for the JWT's ``jti``
      3. the row is not revoked
      4. the row's ``expires_at`` is NULL or still in the future (the ROW's expiry,
         never the JWT's -- a token minted with a different lifetime cannot outlive it)
      5. the JWT's ``user_id`` / ``company_id`` equal the row's (the row is
         authoritative; a forged claim can never widen tenancy)
      6. the user exists and belongs to the row's company
    Pure read: no touch, no audit, no commit. ``is_active`` is the CALLER's
    decision -- see the module docstring.
    """
    claims = verify_api_token(token)
    if claims is None or not claims.get("jti"):
        return None

    record = db.query(ApiToken).filter(ApiToken.jti == claims["jti"]).first()
    if record is None or record.revoked:
        return None

    moment = now or datetime.utcnow()
    if record.expires_at is not None and record.expires_at <= moment:
        return None

    if claims.get("user_id") != record.user_id or claims.get("company_id") != record.company_id:
        return None

    user = db.query(User).filter(User.id == record.user_id).first()
    if user is None or user.company_id != record.company_id:
        return None
    return user, record


def touch_api_token_last_used(db: Session, record: ApiToken, *, now: Optional[datetime] = None) -> bool:
    """Stamp ``last_used_at`` when it is NULL or older than :data:`LAST_USED_TOUCH_INTERVAL`.

    Commits immediately (the auth path runs before any route logic, so the
    session holds nothing else) through a CONDITIONAL ``UPDATE`` -- the
    interval is re-checked in the ``WHERE`` so two workers racing the same
    token inside one interval write once. A failed touch is logged and
    swallowed: a liveness marker must never 500 the request it marks.
    Returns whether a row was written.
    """
    moment = now or datetime.utcnow()
    last = record.last_used_at
    if last is not None and moment - last < LAST_USED_TOUCH_INTERVAL:
        return False

    threshold = moment - LAST_USED_TOUCH_INTERVAL
    try:
        updated = (
            db.query(ApiToken)
            .filter(
                ApiToken.id == record.id,
                or_(ApiToken.last_used_at.is_(None), ApiToken.last_used_at < threshold),
            )
            .update({"last_used_at": moment}, synchronize_session=False)
        )
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        logger.warning("api_tokens.last_used_at touch failed for token id %s", record.id)
        return False
    return bool(updated)


def resolve_api_token_user(
    db: Session, token: str, *, now: Optional[datetime] = None
) -> Optional[Tuple[User, ApiToken]]:
    """The auth-path resolver: :func:`check_api_token`, then the throttled touch.

    Returns the ``(user, row)`` pair for a live token -- INCLUDING a disabled
    user, untouched, so ``get_current_user`` can answer its usual 403 rather
    than a 401 that would read as "bad credentials". ``None`` for everything
    the row check refuses.
    """
    moment = now or datetime.utcnow()
    resolved = check_api_token(db, token, now=moment)
    if resolved is None:
        return None
    user, record = resolved
    if user.is_active:
        touch_api_token_last_used(db, record, now=moment)
    return user, record


# --------------------------------------------------------------------------- admin verbs


def _log_auth_event(audit: AuditService, action: str, *, user: User, record: ApiToken, extra: Dict) -> None:
    """The ``authentication`` row ``auth.log_auth_event`` would write, via the request-scoped audit.

    Written through the caller's ``AuditService`` (actor = the Admin, IP and
    user agent from the request) rather than by importing the endpoint-module
    helper: a service must not import a router, and ``auth.py`` already
    imports ``deps`` which imports this module. Same ``resource_type``, same
    ``resource_id`` (the token's USER), same identifier (their email).
    """
    audit.log(
        action=action,
        resource_type="authentication",
        resource_id=user.id,
        resource_identifier=user.email,
        description=f"{action} for {user.email}",
        success=True,
        extra_data={"api_token_id": record.id, "label": record.label, "jti_prefix": record.jti_prefix, **extra},
    )


def issue_api_token(
    db: Session,
    *,
    company_id: int,
    user_id: int,
    label: str,
    expires_days: Optional[int],
    created_by: int,
    audit: AuditService,
) -> Tuple[ApiToken, str]:
    """Create an ``api_tokens`` row + the matching JWT. Returns ``(record, jwt)``.

    The target user must belong to ``company_id`` (**404** otherwise -- never a
    cross-tenant hint) and be active (**409**). ``expires_days`` ``None`` means
    the row's ``expires_at`` is NULL (never expires); otherwise 1..3650. The
    JWT is returned exactly once and is never persisted; the audit row and the
    ``API_TOKEN_ISSUED`` auth event carry metadata only.
    """
    if expires_days is not None and not 1 <= expires_days <= MAX_API_TOKEN_EXPIRES_DAYS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"expires_days must be between 1 and {MAX_API_TOKEN_EXPIRES_DAYS}, or omitted for a token that never expires",
        )

    target = tenant_query(db, User, company_id).filter(User.id == user_id).first()
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if not target.is_active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User account is disabled; reactivate the user before issuing an API token",
        )

    now = datetime.utcnow()
    expires_at = now + timedelta(days=expires_days) if expires_days is not None else None
    jti = secrets.token_urlsafe(32)

    record = ApiToken(
        user_id=target.id,
        label=label,
        jti=jti,
        expires_at=expires_at,
        revoked=False,
        created_by=created_by,
        created_at=now,
        company_id=company_id,
    )
    db.add(record)
    db.flush()  # assign the PK so the audit rows carry a real resource_id

    lifetime = "never expires" if expires_at is None else f"expires {expires_at.date().isoformat()}"
    audit.log_create(
        resource_type=API_TOKEN_RESOURCE_TYPE,
        resource_id=record.id,
        resource_identifier=label,
        new_values={
            "label": label,
            "user_id": target.id,
            "user_email": target.email,
            "company_id": company_id,
            "expires_at": to_utc_iso(expires_at) if expires_at is not None else None,
            "jti_prefix": record.jti_prefix,
        },
        description=f"Issued API token '{label}' for {target.email} ({lifetime})",
    )
    _log_auth_event(audit, AUTH_EVENT_ISSUED, user=target, record=record, extra={"expires_at": lifetime})
    db.commit()
    db.refresh(record)

    token = create_api_token(jti=jti, user_id=target.id, company_id=company_id, expires_at=expires_at)
    return record, token


def list_api_tokens(
    db: Session, *, company_id: int, user_id: Optional[int] = None, include_revoked: bool = False
) -> List[ApiToken]:
    """This company's API tokens, newest first (tenant-scoped; metadata only, the rows hold no JWT)."""
    query = tenant_query(db, ApiToken, company_id)
    if user_id is not None:
        query = query.filter(ApiToken.user_id == user_id)
    if not include_revoked:
        query = query.filter(ApiToken.revoked.is_(False))
    return query.order_by(ApiToken.created_at.desc(), ApiToken.id.desc()).all()


def revoke_api_token(
    db: Session,
    *,
    company_id: int,
    token_id: int,
    revoked_by: int,
    reason: str,
    audit: AuditService,
) -> ApiToken:
    """Revoke an API token (tenant-scoped lookup, **404**; already revoked, **409**; audited).

    Revocation is a status flip, never a delete -- the row stays as the record
    of who held access. One-way: the FIRST revocation's reason, actor and
    instant are the record, so a second call refuses rather than overwrites.
    The auth path re-reads the row on every request, so the holder is 401 on
    its very next call.
    """
    record = tenant_query(db, ApiToken, company_id).filter(ApiToken.id == token_id).first()
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API token not found")
    if record.revoked:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="API token is already revoked")

    now = datetime.utcnow()
    record.revoked = True
    record.revoked_at = now
    record.revoked_by = revoked_by
    record.revoke_reason = reason

    holder = db.query(User).filter(User.id == record.user_id).first()
    holder_email = holder.email if holder is not None else f"user {record.user_id}"
    audit.log_status_change(
        resource_type=API_TOKEN_RESOURCE_TYPE,
        resource_id=record.id,
        resource_identifier=record.label,
        old_status="active",
        new_status="revoked",
        description=f"Revoked API token '{record.label}' for {holder_email}: {reason}",
        extra_data={"reason": reason, "user_id": record.user_id, "jti_prefix": record.jti_prefix},
    )
    if holder is not None:
        _log_auth_event(audit, AUTH_EVENT_REVOKED, user=holder, record=record, extra={"reason": reason})
    db.commit()
    db.refresh(record)
    return record
