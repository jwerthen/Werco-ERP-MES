"""Issue / list / revoke scoped TV-display tokens (A0.5 wallboard).

Each function owns its unit of work (commits at the end) and writes the
tamper-evident audit row BEFORE the terminal commit so the state change and
its audit trail commit atomically (AuditService only flushes).

The raw JWT is returned to the caller exactly once at issuance and is never
persisted — only its ``jti`` lands in ``display_tokens`` (the revocation
anchor checked by ``app.api.deps.get_display_or_user`` on every wallboard
request).

TV pairing: issuance also mints a short one-time setup code (8 chars,
15-minute TTL, single-use) so a wall TV can pair by typing ``<host>/tv`` +
the code instead of a 355-char ``#token=`` URL. Only the code's SHA-256 hex
is stored; the public claim endpoint re-mints the SAME row-anchored JWT, so
revocation semantics are unchanged.
"""

import hashlib
import logging
import re
import secrets
from datetime import datetime, timedelta
from typing import Optional, Tuple

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from app.core.security import create_display_token
from app.core.time_utils import to_utc_iso
from app.db.tenant_filter import tenant_query
from app.models.display_token import DisplayToken
from app.services.audit_service import AuditService

logger = logging.getLogger(__name__)

# One-time TV setup codes pair a screen in minutes, not days — keep it tight.
SETUP_CODE_TTL_MINUTES = 15

# Unambiguous, uppercase-only alphabet (no 0/O/1/I/L) so the code survives
# being read off a screen and typed on a TV remote.
_SETUP_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_SETUP_CODE_LENGTH = 8

# ONE generic message for EVERY claim failure mode (unknown / already used /
# expired code / revoked or expired display) — the public claim endpoint must
# never act as an oracle for which of those it was.
_SETUP_CODE_NOT_RECOGNIZED = "Setup code not recognized"


def _generate_setup_code() -> str:
    """8 chars of CSPRNG output over the unambiguous alphabet (~40 bits)."""
    return "".join(secrets.choice(_SETUP_CODE_ALPHABET) for _ in range(_SETUP_CODE_LENGTH))


def _normalize_setup_code(raw: str) -> str:
    """Uppercase and strip whitespace + dash variants — 'abcd-2345', 'ABCD 2345',
    and a smart-punctuation en/em dash ('ABCD–2345') all claim the same code."""
    return re.sub(r"[\s‐-―-]", "", raw.upper())


def _hash_setup_code(code: str) -> str:
    """SHA-256 hex of the NORMALIZED code — what ``setup_code_hash`` stores.

    Plain (unsalted, fast) SHA-256 rather than bcrypt is deliberate: the code
    is high-entropy CSPRNG output (~40 bits), single-use, and expires in
    minutes, so offline brute force isn't the threat model — and a
    deterministic digest is what allows the single indexed lookup by
    ``setup_code_hash`` at claim time.
    """
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def issue_display_token(
    db: Session,
    *,
    company_id: int,
    label: str,
    expires_days: int,
    created_by: int,
    audit: AuditService,
    dept: Optional[str] = None,
) -> Tuple[DisplayToken, str, str]:
    """Create a display_tokens row + matching JWT + one-time setup code.

    Returns ``(record, jwt, setup_code)``. The JWT's ``exp`` and the row's
    ``expires_at`` carry the same instant; the row is authoritative (checked
    on every request), so revocation/expiry hold even for an already-minted
    JWT. The setup code (15-min, single-use) lets the TV pair by typing 8
    chars instead of the full JWT URL; only its SHA-256 lands in the row.
    """
    expires_at = datetime.utcnow() + timedelta(days=expires_days)
    jti = secrets.token_urlsafe(32)
    setup_code = _generate_setup_code()
    setup_code_expires_at = datetime.utcnow() + timedelta(minutes=SETUP_CODE_TTL_MINUTES)

    record = DisplayToken(
        label=label,
        jti=jti,
        expires_at=expires_at,
        revoked=False,
        created_by=created_by,
        company_id=company_id,
        dept=dept,
        setup_code_hash=_hash_setup_code(setup_code),
        setup_code_expires_at=setup_code_expires_at,
        setup_code_used_at=None,
    )
    db.add(record)
    db.flush()  # assign the PK so the audit row carries a real resource_id

    # Audit the issuance (CMMC AC/AU): the row, never the JWT, is logged —
    # and NEVER the setup code or its hash (the hash is the credential lookup
    # key; the audit trail must not become a claim oracle).
    audit.log_create(
        resource_type="display_token",
        resource_id=record.id,
        resource_identifier=label,
        new_values={
            "label": label,
            "expires_at": to_utc_iso(expires_at),
            "company_id": company_id,
            "dept": dept,
            "setup_code_expires_at": to_utc_iso(setup_code_expires_at),
        },
        description=f"Issued wallboard display token '{label}' (expires {expires_at.date().isoformat()})",
    )
    db.commit()
    db.refresh(record)

    token = create_display_token(jti=jti, company_id=company_id, label=label, expires_at=expires_at)
    return record, token, setup_code


def reissue_setup_code(
    db: Session,
    *,
    company_id: int,
    token_id: int,
    audit: AuditService,
) -> Tuple[DisplayToken, str]:
    """Rotate the one-time setup code on an existing display token.

    Tenant-scoped (ADMIN/MANAGER path). The previous code — used or not —
    stops working immediately because the stored hash is replaced;
    ``setup_code_used_at`` resets so the NEW code is claimable once, with a
    fresh 15-minute window. Refuses revoked (400) and expired (400) tokens:
    reissuing a code for a dead display would mint nothing usable. The acting
    user is carried on the audit row via the request-scoped AuditService.
    """
    record = tenant_query(db, DisplayToken, company_id).filter(DisplayToken.id == token_id).first()
    if record is None:
        raise HTTPException(status_code=404, detail="Display token not found")
    if record.revoked:
        raise HTTPException(status_code=400, detail="Display token is revoked")
    if record.expires_at <= datetime.utcnow():
        raise HTTPException(status_code=400, detail="Display token has expired")

    setup_code = _generate_setup_code()
    record.setup_code_hash = _hash_setup_code(setup_code)
    record.setup_code_expires_at = datetime.utcnow() + timedelta(minutes=SETUP_CODE_TTL_MINUTES)
    record.setup_code_used_at = None

    # The rotation is the auditable fact — the code value (and its hash)
    # NEVER lands on the tamper-evident chain.
    audit.log_update(
        resource_type="display_token",
        resource_id=record.id,
        resource_identifier=record.label,
        old_values={"setup_code": "(previous code invalidated)"},
        new_values={
            "setup_code": "(rotated)",
            "setup_code_expires_at": to_utc_iso(record.setup_code_expires_at),
        },
        description=f"Reissued TV setup code for display token '{record.label}'",
    )
    db.commit()
    db.refresh(record)
    return record, setup_code


def _audit_failed_claim(db: Session, request: Optional[Request], company_id: Optional[int] = None) -> None:
    """Best-effort failed-claim audit row — must never mask the generic 404."""
    try:
        audit = AuditService(db, user=None, request=request, company_id=company_id)
        audit.log(
            action="CLAIM",
            resource_type="display_token",
            description="Failed TV setup-code claim (unknown, used, or expired code — not disclosed to caller)",
            success=False,
        )
        db.commit()
    except Exception:  # pragma: no cover - defensive, mirrors kiosk station-login
        logger.exception("Failed to audit display-token claim failure")


def claim_display_token(
    db: Session,
    *,
    raw_code: str,
    request: Optional[Request] = None,
) -> Tuple[DisplayToken, str]:
    """Exchange a one-time setup code for the wallboard display JWT (PUBLIC path).

    Deliberately NOT tenant-scoped: the caller is an unauthenticated TV, so
    the high-entropy code hash IS the credential and the row it finds is the
    company-binding authority (same posture as the kiosk station-login). One
    indexed lookup enforces every precondition — unused, unexpired code,
    unrevoked and unexpired display — and every miss raises the same generic
    404 so the endpoint can't be probed for WHY a code failed.

    On success the claim is audited on the row's company with ``user=None``
    (a TV, not a person), the code is burned (``setup_code_used_at``), and
    the JWT is re-minted from the row (``jti``/``company_id``/``label``/
    ``expires_at``) — byte-for-byte equivalent in claims to the issuance JWT,
    so the ``display_tokens`` row remains the single revocation anchor.
    """
    now = datetime.utcnow()
    code_hash = _hash_setup_code(_normalize_setup_code(raw_code))

    record = (
        db.query(DisplayToken)
        .filter(
            DisplayToken.setup_code_hash == code_hash,
            DisplayToken.setup_code_used_at.is_(None),
            DisplayToken.setup_code_expires_at > now,
            DisplayToken.revoked.is_(False),
            DisplayToken.expires_at > now,
        )
        .first()
    )
    if record is None:
        # CMMC AU-2: failed access attempts land on the tamper-evident chain
        # (station-login precedent). No row matched, so the event is
        # platform-level (company_id=None) — and the attempted code is NEVER
        # recorded (the trail must not become a claim oracle).
        _audit_failed_claim(db, request)
        raise HTTPException(status_code=404, detail=_SETUP_CODE_NOT_RECOGNIZED)

    # Burn the code with an ATOMIC conditional UPDATE, not read-then-set: two
    # TVs racing the same code would both pass the SELECT above under READ
    # COMMITTED, so single-use is enforced by ``WHERE setup_code_used_at IS
    # NULL`` — exactly one racer updates a row; the loser sees rowcount 0 and
    # gets the same generic 404 as every other failure mode.
    burned = (
        db.query(DisplayToken)
        .filter(DisplayToken.id == record.id, DisplayToken.setup_code_used_at.is_(None))
        .update({"setup_code_used_at": now}, synchronize_session=False)
    )
    if not burned:
        db.rollback()
        # The race loser is attributable to the row's tenant.
        _audit_failed_claim(db, request, company_id=record.company_id)
        raise HTTPException(status_code=404, detail=_SETUP_CODE_NOT_RECOGNIZED)

    # Audit the successful pairing on the row's company (kiosk station-login
    # pattern: user=None, request passed through for IP/user-agent).
    audit = AuditService(db, user=None, request=request, company_id=record.company_id)
    audit.log(
        action="CLAIM",
        resource_type="display_token",
        resource_id=record.id,
        resource_identifier=record.label,
        description=f"TV claimed display token '{record.label}' via one-time setup code",
        success=True,
    )
    db.commit()
    db.refresh(record)

    token = create_display_token(
        jti=record.jti, company_id=record.company_id, label=record.label, expires_at=record.expires_at
    )
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
