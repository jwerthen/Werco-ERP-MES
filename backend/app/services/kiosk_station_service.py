"""Create / list / revoke / reset shared-PIN crew-station kiosks, and
authenticate a station PIN into a scoped kiosk token.

Twin of ``signin_station_service`` (the visitor sign-in tablet): each function
owns its unit of work (commits at the end) and writes the tamper-evident audit
row BEFORE the terminal commit so the state change and its audit trail commit
atomically (AuditService only flushes). The PIN is bcrypt-hashed at rest and
never returned; the minted JWT is returned exactly once at
``authenticate_station``.

The kiosk's current work center (non-null ``work_center_id``) is tenant-scoped.
An unlocked station may choose another active work center in its own company;
queue reads always enforce the current selection from the station row.
"""

from datetime import datetime
from typing import Tuple

from fastapi import HTTPException
from sqlalchemy.orm import Session, joinedload

from app.core.security import create_kiosk_token, get_password_hash, verify_password
from app.db.tenant_filter import tenant_query
from app.models.kiosk_station import KioskStation
from app.models.work_center import WorkCenter
from app.services.audit_service import AuditService

# Lifetime of a minted station kiosk token (hours).
KIOSK_TOKEN_TTL_HOURS = 24


def create_station(
    db: Session,
    *,
    company_id: int,
    label: str,
    work_center_id: int,
    pin: str,
    created_by: int,
    audit: AuditService,
) -> KioskStation:
    """Create a kiosk_stations row with a bcrypt-hashed PIN (tenant-scoped, audited).

    The bound work center must exist in the active company (404 otherwise) —
    a guessed foreign work_center_id can never bind a station across tenants.
    """
    work_center = tenant_query(db, WorkCenter, company_id).filter(WorkCenter.id == work_center_id).first()
    if work_center is None:
        raise HTTPException(status_code=404, detail="Work center not found")

    record = KioskStation(
        label=label,
        work_center_id=work_center.id,
        pin_hash=get_password_hash(pin),
        revoked=False,
        created_by=created_by,
        company_id=company_id,
    )
    db.add(record)
    db.flush()  # assign the PK so the audit row carries a real resource_id

    audit.log_create(
        resource_type="kiosk_station",
        resource_id=record.id,
        resource_identifier=label,
        new_values={"label": label, "work_center_id": work_center.id, "company_id": company_id},
        description=f"Created crew-station kiosk '{label}' bound to work center '{work_center.code}'",
    )
    db.commit()
    db.refresh(record)
    return record


def list_stations(db: Session, *, company_id: int) -> list[KioskStation]:
    """All kiosk stations for the active company, newest first (no pin_hash exposure)."""
    return (
        tenant_query(db, KioskStation, company_id)
        .options(joinedload(KioskStation.work_center))
        .order_by(KioskStation.created_at.desc())
        .all()
    )


def list_work_centers(db: Session, *, company_id: int) -> list[WorkCenter]:
    """Return only active workstation choices within the station's company."""
    return (
        tenant_query(db, WorkCenter, company_id)
        .filter(WorkCenter.is_active.is_(True))
        .order_by(WorkCenter.code, WorkCenter.id)
        .all()
    )


def select_work_center(
    db: Session,
    *,
    station: KioskStation,
    work_center_id: int,
    audit: AuditService,
) -> KioskStation:
    """Persist an authenticated station's own workstation choice with its audit."""
    work_center = (
        tenant_query(db, WorkCenter, station.company_id)
        .filter(WorkCenter.id == work_center_id, WorkCenter.is_active.is_(True))
        .first()
    )
    if work_center is None:
        raise HTTPException(status_code=404, detail="Active work center not found")

    old_work_center_id = station.work_center_id
    if old_work_center_id == work_center.id:
        return station

    station.work_center_id = work_center.id
    station.work_center = work_center
    audit.log_update(
        resource_type="kiosk_station",
        resource_id=station.id,
        resource_identifier=station.label,
        old_values={"work_center_id": old_work_center_id},
        new_values={"work_center_id": work_center.id},
        description=f"Crew-station kiosk '{station.label}' selected work center '{work_center.code}'",
    )
    db.commit()
    db.refresh(station)
    return station


def revoke_station(
    db: Session,
    *,
    company_id: int,
    station_id: int,
    revoked_by: int,
    audit: AuditService,
) -> KioskStation:
    """Revoke a station (tenant-scoped lookup; idempotent; audited).

    Revocation is a status flip, not a delete — the row stays as the issuance
    record so the trail survives. ``get_kiosk_or_user`` and the badge-token
    mint re-check ``revoked`` on every request, so the tablet loses access on
    its next call.
    """
    record = tenant_query(db, KioskStation, company_id).filter(KioskStation.id == station_id).first()
    if record is None:
        raise HTTPException(status_code=404, detail="Kiosk station not found")

    if record.revoked:
        return record  # idempotent no-op

    record.revoked = True
    record.revoked_at = datetime.utcnow()
    record.revoked_by = revoked_by

    audit.log_status_change(
        resource_type="kiosk_station",
        resource_id=record.id,
        resource_identifier=record.label,
        old_status="active",
        new_status="revoked",
        description=f"Revoked crew-station kiosk '{record.label}'",
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
) -> KioskStation:
    """Re-hash the station PIN (tenant-scoped, audited). The PIN itself is never logged."""
    record = tenant_query(db, KioskStation, company_id).filter(KioskStation.id == station_id).first()
    if record is None:
        raise HTTPException(status_code=404, detail="Kiosk station not found")

    record.pin_hash = get_password_hash(pin)

    audit.log_update(
        resource_type="kiosk_station",
        resource_id=record.id,
        resource_identifier=record.label,
        old_values={"pin": "***"},
        new_values={"pin": "*** (reset)"},
        description=f"Reset PIN for crew-station kiosk '{record.label}'",
    )
    db.commit()
    db.refresh(record)
    return record


def authenticate_station(
    db: Session,
    *,
    station_id: int,
    pin: str,
) -> Tuple[KioskStation, str, int]:
    """Verify a station PIN and mint a scoped kiosk token.

    NOT tenant-scoped by an external company_id (the tablet has no session yet);
    the station row IS the company-binding authority. Returns
    ``(station, token, expires_in_seconds)``. Raises 401 on a missing/revoked
    station or a bad PIN (deliberately indistinguishable to the caller). The
    company_id for the minted token comes from the DB row, never the client.
    """
    record = (
        db.query(KioskStation)
        .options(joinedload(KioskStation.work_center))
        .filter(KioskStation.id == station_id)
        .first()
    )
    if record is None or record.revoked or not verify_password(pin, record.pin_hash):
        raise HTTPException(status_code=401, detail="Invalid station or PIN")

    record.last_used_at = datetime.utcnow()
    db.commit()
    db.refresh(record)

    token = create_kiosk_token(
        station_id=record.id,
        company_id=record.company_id,
        label=record.label,
        ttl_hours=KIOSK_TOKEN_TTL_HOURS,
    )
    return record, token, KIOSK_TOKEN_TTL_HOURS * 3600
