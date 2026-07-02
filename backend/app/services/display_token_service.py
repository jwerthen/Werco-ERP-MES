"""Issue / list / revoke scoped TV-display tokens (A0.5 wallboard).

Each function owns its unit of work (commits at the end) and writes the
tamper-evident audit row BEFORE the terminal commit so the state change and
its audit trail commit atomically (AuditService only flushes).

The raw JWT is returned to the caller exactly once at issuance and is never
persisted — only its ``jti`` lands in ``display_tokens`` (the revocation
anchor checked by ``app.api.deps.get_display_or_user`` on every wallboard
request).
"""

import secrets
from datetime import datetime, timedelta
from typing import Tuple

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.core.security import create_display_token
from app.core.time_utils import to_utc_iso
from app.db.tenant_filter import tenant_query
from app.models.display_token import DisplayToken
from app.services.audit_service import AuditService


def issue_display_token(
    db: Session,
    *,
    company_id: int,
    label: str,
    expires_days: int,
    created_by: int,
    audit: AuditService,
) -> Tuple[DisplayToken, str]:
    """Create a display_tokens row + matching JWT. Returns (record, jwt).

    The JWT's ``exp`` and the row's ``expires_at`` carry the same instant; the
    row is authoritative (checked on every request), so revocation/expiry hold
    even for an already-minted JWT.
    """
    expires_at = datetime.utcnow() + timedelta(days=expires_days)
    jti = secrets.token_urlsafe(32)

    record = DisplayToken(
        label=label,
        jti=jti,
        expires_at=expires_at,
        revoked=False,
        created_by=created_by,
        company_id=company_id,
    )
    db.add(record)
    db.flush()  # assign the PK so the audit row carries a real resource_id

    # Audit the issuance (CMMC AC/AU): the row, never the JWT, is logged.
    audit.log_create(
        resource_type="display_token",
        resource_id=record.id,
        resource_identifier=label,
        new_values={
            "label": label,
            "expires_at": to_utc_iso(expires_at),
            "company_id": company_id,
        },
        description=f"Issued wallboard display token '{label}' (expires {expires_at.date().isoformat()})",
    )
    db.commit()
    db.refresh(record)

    token = create_display_token(jti=jti, company_id=company_id, label=label, expires_at=expires_at)
    return record, token


def list_display_tokens(db: Session, *, company_id: int) -> list[DisplayToken]:
    """All display tokens for the active company, newest first (tenant-scoped)."""
    return tenant_query(db, DisplayToken, company_id).order_by(DisplayToken.created_at.desc()).all()


def revoke_display_token(
    db: Session,
    *,
    company_id: int,
    token_id: int,
    revoked_by: int,
    audit: AuditService,
) -> DisplayToken:
    """Revoke a display token (tenant-scoped lookup; idempotent; audited).

    Revocation is a status flip, not a delete — the row stays as the issuance
    record. Already-revoked tokens return unchanged with no second audit row.
    """
    record = tenant_query(db, DisplayToken, company_id).filter(DisplayToken.id == token_id).first()
    if record is None:
        raise HTTPException(status_code=404, detail="Display token not found")

    if record.revoked:
        return record  # idempotent no-op

    record.revoked = True
    record.revoked_at = datetime.utcnow()
    record.revoked_by = revoked_by

    audit.log_status_change(
        resource_type="display_token",
        resource_id=record.id,
        resource_identifier=record.label,
        old_status="active",
        new_status="revoked",
        description=f"Revoked wallboard display token '{record.label}'",
    )
    db.commit()
    db.refresh(record)
    return record
