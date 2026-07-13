"""Visitor sign-in tablet + admin visitor-log API.

Thin router (mirrors ``qms_standards`` thinness): validate, delegate to a
service, return a Pydantic schema. Two write endpoints accept EITHER a staff
access token OR a PIN-minted station signin token via ``get_signin_principal``;
everything else is staff-only RBAC.

Compliance: tenant isolation everywhere (staff via ``get_current_company_id``;
station via the authoritative ``SigninStation`` row), AuditService on every
state change (station writes pass ``user=None`` + explicit ``company_id`` and
record the station label as actor), soft-delete only, CUI names never egress.
"""

import csv
import io
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.deps import (
    SigninPrincipal,
    get_current_company_id,
    get_signin_principal,
    require_role,
)
from app.core.time_utils import to_utc_iso
from app.db.database import get_db
from app.models.user import User, UserRole
from app.models.visitor_log import VisitorStatus
from app.schemas.signin_station import (
    SigninStationCreate,
    SigninStationListResponse,
    SigninStationResponse,
    StationLoginRequest,
    StationLoginResponse,
    StationResetPinRequest,
)
from app.schemas.visitor_log import (
    VisitorLogListResponse,
    VisitorLogResponse,
    VisitorManualEntryRequest,
    VisitorSignInRequest,
    VisitorSignOutRequest,
)
from app.services import signin_station_service, visitor_log_service
from app.services.audit_service import AuditService

logger = logging.getLogger(__name__)
router = APIRouter()

# Staff roles allowed to view the visitor log.
_VIEW_ROLES = [UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]
# Staff roles allowed to manage stations, export, and delete.
_MANAGE_ROLES = [UserRole.ADMIN, UserRole.MANAGER]


def _audit_for_principal(db: Session, principal: SigninPrincipal, request: Request) -> AuditService:
    """Build an AuditService for a write that may come from a station OR a user.

    Station path: ``user=None`` + explicit ``company_id`` (from the authoritative
    DB row); the station label is woven into the audit description by the service
    so the tamper-evident row attributes the action to the station, not a person.
    """
    return AuditService(db, user=principal.user, request=request, company_id=principal.company_id)


# ============== Station login (PUBLIC, PIN-gated, rate-limited) ==============


@router.post("/station-login", response_model=StationLoginResponse)
def station_login(
    payload: StationLoginRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """Unlock a tablet with the shared station PIN → scoped type='signin' JWT.

    PUBLIC + rate-limited (see ``/api/v1/visitor-logs/station-login`` in
    ``main.py`` AUTH_RATE_LIMITS). The DB row is the company-binding authority;
    the minted token's ``cid`` comes from it, never from the client. Failed PIN
    attempts are recorded as an operational audit event.
    """
    try:
        station, token, expires_in = signin_station_service.authenticate_station(
            db, station_id=payload.station_id, pin=payload.pin
        )
    except Exception:
        # Audit the failed attempt against the station's company when the station
        # exists (so the trail stays tenant-attributed); swallow lookup issues so
        # we never leak whether the station id or the PIN was wrong.
        try:
            from app.models.signin_station import SigninStation

            existing = db.query(SigninStation).filter(SigninStation.id == payload.station_id).first()
            if existing is not None:
                audit = AuditService(db, user=None, request=request, company_id=existing.company_id)
                audit.log(
                    action="LOGIN_FAILED",
                    resource_type="signin_station",
                    resource_id=existing.id,
                    resource_identifier=existing.label,
                    description=f"Failed PIN attempt for sign-in station '{existing.label}'",
                    success=False,
                )
                db.commit()
        except Exception:  # pragma: no cover - defensive
            logger.exception("Failed to audit station-login failure")
        raise

    return StationLoginResponse(token=token, station_label=station.label, expires_in=expires_in)


# ============== Visitor writes (station OR staff) ==============


@router.post("/sign-in", response_model=VisitorLogResponse, status_code=status.HTTP_201_CREATED)
def sign_in(
    payload: VisitorSignInRequest,
    request: Request,
    db: Session = Depends(get_db),
    principal: SigninPrincipal = Depends(get_signin_principal),
):
    """Record a visitor sign-in (station tablet OR staff). Best-effort host email."""
    audit = _audit_for_principal(db, principal, request)
    row = visitor_log_service.sign_in(
        db,
        company_id=principal.company_id,
        payload=payload,
        signin_station_id=principal.station_id,
        station_label=principal.station_label,
        audit=audit,
    )
    return row


@router.post("/sign-out", response_model=VisitorLogResponse)
def sign_out(
    payload: VisitorSignOutRequest,
    request: Request,
    db: Session = Depends(get_db),
    principal: SigninPrincipal = Depends(get_signin_principal),
):
    """Record a visitor sign-out by visitor_log_id or name (409 on name ambiguity)."""
    audit = _audit_for_principal(db, principal, request)
    row = visitor_log_service.sign_out(
        db,
        company_id=principal.company_id,
        name=payload.name,
        visitor_log_id=payload.visitor_log_id,
        audit=audit,
    )
    return row


# ============== Staff back-entry (staff-only, NOT the station token) ==============


@router.post("/manual", response_model=VisitorLogResponse, status_code=status.HTTP_201_CREATED)
def manual_entry(
    payload: VisitorManualEntryRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_MANAGE_ROLES)),
    company_id: int = Depends(get_current_company_id),
):
    """Staff back-entry of an offline visit with its ACTUAL times (ADMIN/MANAGER).

    For recording a paper-logged visit after a lobby-tablet outage. Unlike
    ``/sign-in`` (station OR staff, stamps ``utcnow()``), this is staff-only RBAC
    — the station token is NOT accepted — and takes the real past sign-in/out
    times. The row is marked staff-entered (``signin_station_id`` NULL +
    ``entered_by_user_id`` set), tenant-scoped, and audited.
    """
    audit = AuditService(db, user=current_user, request=request, company_id=company_id)
    row = visitor_log_service.manual_entry(
        db,
        company_id=company_id,
        payload=payload,
        entered_by_user_id=current_user.id,
        audit=audit,
    )
    return row


# ============== Admin visitor-log views (staff-only) ==============


@router.get("/", response_model=VisitorLogListResponse)
def list_visitors(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_VIEW_ROLES)),
    company_id: int = Depends(get_current_company_id),
    status_filter: Optional[VisitorStatus] = Query(None, alias="status"),
    q: Optional[str] = Query(None, max_length=120),
    date_from: Optional[datetime] = Query(None),
    date_to: Optional[datetime] = Query(None),
    on_site_only: bool = Query(False),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
):
    """List visitor records for the active company (filters + offset paging)."""
    items, total = visitor_log_service.list_visitors(
        db,
        company_id=company_id,
        status=status_filter,
        q=q,
        date_from=date_from,
        date_to=date_to,
        on_site_only=on_site_only,
        skip=skip,
        limit=limit,
    )
    return VisitorLogListResponse(
        items=[VisitorLogResponse.model_validate(i) for i in items],
        total=total,
    )


@router.get("/export.csv")
def export_visitors_csv(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_MANAGE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    status_filter: Optional[VisitorStatus] = Query(None, alias="status"),
    date_from: Optional[datetime] = Query(None),
    date_to: Optional[datetime] = Query(None),
):
    """Export the visitor log as CSV (ADMIN/MANAGER). The export is audited."""
    items, total = visitor_log_service.list_visitors(
        db,
        company_id=company_id,
        status=status_filter,
        date_from=date_from,
        date_to=date_to,
        skip=0,
        limit=100000,
    )

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "id",
            "visitor_name",
            "visitor_company",
            "visitor_phone",
            "host_name",
            "purpose",
            "purpose_note",
            "safety_acknowledged",
            "status",
            "signed_in_at",
            "signed_out_at",
            "station_label",
            "entry_type",
        ]
    )
    for r in items:
        if r.entered_by_user_id is not None:
            entry_type = "staff_back_entry"
        elif r.signin_station_id is not None:
            entry_type = "station"
        else:
            entry_type = "staff_live"
        writer.writerow(
            [
                r.id,
                r.visitor_name,
                r.visitor_company or "",
                r.visitor_phone or "",
                r.host_name or "",
                r.purpose.value if r.purpose else "",
                r.purpose_note or "",
                r.safety_acknowledged,
                r.status.value if r.status else "",
                to_utc_iso(r.signed_in_at) or "",
                to_utc_iso(r.signed_out_at) or "",
                r.station_label or "",
                entry_type,
            ]
        )

    audit = AuditService(db, user=current_user, request=request, company_id=company_id)
    audit.log(
        action="EXPORT",
        resource_type="visitor_log",
        description=f"Exported {total} visitor record(s) to CSV",
    )
    db.commit()

    buffer.seek(0)
    return StreamingResponse(
        buffer,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=visitor_log.csv"},
    )


@router.delete("/{visitor_log_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_visitor(
    visitor_log_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_MANAGE_ROLES)),
    company_id: int = Depends(get_current_company_id),
):
    """Soft-delete a visitor record (ADMIN/MANAGER, tenant-scoped, audited)."""
    audit = AuditService(db, user=current_user, request=request, company_id=company_id)
    visitor_log_service.soft_delete_visitor(
        db,
        company_id=company_id,
        visitor_log_id=visitor_log_id,
        user=current_user,
        audit=audit,
    )


# ============== Station management (staff-only) ==============


@router.post("/stations", response_model=SigninStationResponse, status_code=status.HTTP_201_CREATED)
def create_station(
    payload: SigninStationCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_MANAGE_ROLES)),
    company_id: int = Depends(get_current_company_id),
):
    """Create a PIN-protected sign-in station (PIN is hashed, never echoed)."""
    audit = AuditService(db, user=current_user, request=request, company_id=company_id)
    station = signin_station_service.create_station(
        db,
        company_id=company_id,
        label=payload.label,
        pin=payload.pin,
        created_by=current_user.id,
        audit=audit,
    )
    return SigninStationResponse.model_validate(station)


@router.get("/stations", response_model=SigninStationListResponse)
def list_stations(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_MANAGE_ROLES)),
    company_id: int = Depends(get_current_company_id),
):
    """List this company's sign-in stations (no PIN/pin_hash exposed)."""
    stations = signin_station_service.list_stations(db, company_id=company_id)
    return SigninStationListResponse(stations=[SigninStationResponse.model_validate(s) for s in stations])


@router.post("/stations/{station_id}/revoke", response_model=SigninStationResponse)
def revoke_station(
    station_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_MANAGE_ROLES)),
    company_id: int = Depends(get_current_company_id),
):
    """Revoke a station (idempotent, audited). The tablet loses access next request."""
    audit = AuditService(db, user=current_user, request=request, company_id=company_id)
    station = signin_station_service.revoke_station(
        db,
        company_id=company_id,
        station_id=station_id,
        revoked_by=current_user.id,
        audit=audit,
    )
    return SigninStationResponse.model_validate(station)


@router.post("/stations/{station_id}/reset-pin", response_model=SigninStationResponse)
def reset_station_pin(
    station_id: int,
    payload: StationResetPinRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(_MANAGE_ROLES)),
    company_id: int = Depends(get_current_company_id),
):
    """Reset a station's shared PIN (re-hashed, audited; PIN never logged)."""
    audit = AuditService(db, user=current_user, request=request, company_id=company_id)
    station = signin_station_service.reset_pin(
        db,
        company_id=company_id,
        station_id=station_id,
        pin=payload.pin,
        audit=audit,
    )
    return SigninStationResponse.model_validate(station)
