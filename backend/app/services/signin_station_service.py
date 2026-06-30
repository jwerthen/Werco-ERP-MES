"""Create / list / revoke / reset shared-PIN visitor sign-in stations, and
authenticate a station PIN into a scoped signin token.

Twin of ``display_token_service``: each function owns its unit of work (commits
at the end) and writes the tamper-evident audit row BEFORE the terminal commit
so the state change and its audit trail commit atomically (AuditService only
flushes). The PIN is bcrypt-hashed at rest and never returned; the minted JWT is
returned exactly once at ``authenticate_station``.
"""

from datetime import datetime
from typing import Tuple

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.core.security import create_signin_token, get_password_hash, verify_password
from app.db.tenant_filter import tenant_query
from app.models.signin_station import SigninStation
from app.services.audit_service import AuditService

# Lifetime of a minted station signin token (hours).
SIGNIN_TOKEN_TTL_HOURS = 24


def create_station(
    db: Session,
    *,
    company_id: int,
    label: str,
    pin: str,
    created_by: int,
    audit: AuditService,
) -> SigninStation:
    """Create a signin_stations row with a bcrypt-hashed PIN (tenant-scoped, audited)."""
    record = SigninStation(
        label=label,
        pin_hash=get_password_hash(pin),
        revoked=False,
        created_by=created_by,
        company_id=company_id,
    )
    db.add(record)
    db.flush()  # assign the PK so the audit row carries a real resource_id

    audit.log_create(
        resource_type="signin_station",
        resource_id=record.id,
        resource_identifier=label,
        new_values={"label": label, "company_id": company_id},
        description=f"Created visitor sign-in station '{label}'",
    )
    db.commit()
    db.refresh(record)
    return record


def list_stations(db: Session, *, company_id: int) -> list[SigninStation]:
    """All sign-in stations for the active company, newest first (no pin_hash exposure)."""
    return tenant_query(db, SigninStation, company_id).order_by(SigninStation.created_at.desc()).all()


def revoke_station(
    db: Session,
    *,
    company_id: int,
    station_id: int,
    revoked_by: int,
    audit: AuditService,
) -> SigninStation:
    """Revoke a station (tenant-scoped lookup; idempotent; audited).

    Revocation is a status flip, not a delete — the row stays as the issuance
    record so the trail survives. The signin-auth dependency re-checks ``revoked``
    on every request, so the tablet loses access on its next call.
    """
    record = tenant_query(db, SigninStation, company_id).filter(SigninStation.id == station_id).first()
    if record is None:
        raise HTTPException(status_code=404, detail="Sign-in station not found")

    if record.revoked:
        return record  # idempotent no-op

    record.revoked = True
    record.revoked_at = datetime.utcnow()
    record.revoked_by = revoked_by

    audit.log_status_change(
        resource_type="signin_station",
        resource_id=record.id,
        resource_identifier=record.label,
        old_status="active",
        new_status="revoked",
        description=f"Revoked visitor sign-in station '{record.label}'",
    )
    db.commit()
    db.refresh(record)
    return record


def reset_pin(
    db: Session,
    *,
    company_id: int,
    station_id: int,
    pin: str,
    audit: AuditService,
) -> SigninStation:
    """Re-hash the station PIN (tenant-scoped, audited). The PIN itself is never logged."""
    record = tenant_query(db, SigninStation, company_id).filter(SigninStation.id == station_id).first()
    if record is None:
        raise HTTPException(status_code=404, detail="Sign-in station not found")

    record.pin_hash = get_password_hash(pin)

    audit.log_update(
        resource_type="signin_station",
        resource_id=record.id,
        resource_identifier=record.label,
        old_values={"pin": "***"},
        new_values={"pin": "*** (reset)"},
        description=f"Reset PIN for visitor sign-in station '{record.label}'",
    )
    db.commit()
    db.refresh(record)
    return record


def authenticate_station(
    db: Session,
    *,
    station_id: int,
    pin: str,
) -> Tuple[SigninStation, str, int]:
    """Verify a station PIN and mint a scoped signin token.

    NOT tenant-scoped by an external company_id (the tablet has no session yet);
    the station row IS the company-binding authority. Returns
    ``(station, token, expires_in_seconds)``. Raises 401 on a missing/revoked
    station or a bad PIN (deliberately indistinguishable to the caller). The
    company_id for the minted token comes from the DB row, never the client.
    """
    record = db.query(SigninStation).filter(SigninStation.id == station_id).first()
    if record is None or record.revoked or not verify_password(pin, record.pin_hash):
        raise HTTPException(status_code=401, detail="Invalid station or PIN")

    record.last_used_at = datetime.utcnow()
    db.commit()
    db.refresh(record)

    token = create_signin_token(
        station_id=record.id,
        company_id=record.company_id,
        label=record.label,
        ttl_hours=SIGNIN_TOKEN_TTL_HOURS,
    )
    return record, token, SIGNIN_TOKEN_TTL_HOURS * 3600
