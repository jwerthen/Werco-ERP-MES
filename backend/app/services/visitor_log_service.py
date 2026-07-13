"""Visitor sign-in / sign-out / list / soft-delete business logic.

Every query is tenant-scoped to the active company and filters
``is_deleted == False`` on reads (compliance invariants #1 / #3). State changes
are audited via ``AuditService`` AFTER the flush that assigns the PK and BEFORE
the terminal commit, so the row and its tamper-evident audit entry commit
atomically. Visitor / host names are CUI and never cross an external boundary;
the only outbound signal is an internal best-effort host email (§5).
"""

import logging
from datetime import datetime
from typing import List, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.queue import enqueue_job_best_effort
from app.core.time_utils import to_utc_iso
from app.models.notification import NotificationPreference
from app.models.user import User
from app.models.visitor_log import VisitorLog, VisitorStatus
from app.schemas.visitor_log import VisitorManualEntryRequest, VisitorSignInRequest
from app.services.audit_service import AuditService
from app.services.notification_service import NotificationEvent

logger = logging.getLogger(__name__)


def _match_host_user(db: Session, *, company_id: int, host_name: Optional[str]) -> Optional[User]:
    """Best-effort host match: an active user IN THIS COMPANY whose full name
    case-insensitively equals ``host_name``. Returns the user only on EXACTLY
    one match (0 or >1 → None). Scoped by company — never cross-tenant (CUI)."""
    if not host_name or not host_name.strip():
        return None
    target = host_name.strip().lower()
    candidates = db.query(User).filter(User.company_id == company_id, User.is_active == True).all()  # noqa: E712
    matches = [u for u in candidates if (u.full_name or "").strip().lower() == target]
    if len(matches) == 1:
        return matches[0]
    return None


def _notify_host_best_effort(db: Session, *, host: User, row: VisitorLog) -> None:
    """Enqueue an internal best-effort check-in email to the matched host.

    Respects the host's notification preference for ``VISITOR_CHECK_IN`` and
    requires a host email. Names are CUI: this is internal SMTP to the company's
    own employee only. NEVER blocks or raises — a notification failure must not
    fail the sign-in (compliance: outbound signal is best-effort)."""
    try:
        if not host.email:
            return
        pref = db.query(NotificationPreference).filter(NotificationPreference.user_id == host.id).first()
        prefs = pref.preferences if (pref and pref.preferences) else {}
        event_pref = prefs.get(NotificationEvent.VISITOR_CHECK_IN, {"email": True})
        if not event_pref.get("email", True):
            return

        enqueue_job_best_effort(
            "send_email_job",
            to=host.email,
            subject=f"Visitor arrived: {row.visitor_name}",
            body=None,
            template="visitor_check_in",
            context={
                "visitor_name": row.visitor_name,
                "visitor_company": row.visitor_company,
                "purpose": row.purpose.value if row.purpose else None,
                "signed_in_at": to_utc_iso(row.signed_in_at),
                "station_label": row.station_label,
            },
        )
    except Exception:  # pragma: no cover - defensive: never fail the sign-in
        logger.exception("Best-effort host check-in notification failed for visitor_log %s", getattr(row, "id", None))


def sign_in(
    db: Session,
    *,
    company_id: int,
    payload: VisitorSignInRequest,
    signin_station_id: Optional[int],
    station_label: Optional[str],
    audit: AuditService,
) -> VisitorLog:
    """Create a SIGNED_IN visitor row (tenant-scoped, audited), best-effort host email."""
    host = _match_host_user(db, company_id=company_id, host_name=payload.host_name)

    row = VisitorLog(
        company_id=company_id,
        visitor_name=payload.visitor_name,
        visitor_company=payload.visitor_company,
        visitor_phone=payload.visitor_phone,
        host_name=payload.host_name,
        host_user_id=host.id if host else None,
        purpose=payload.purpose,
        purpose_note=payload.purpose_note,
        safety_acknowledged=payload.safety_acknowledged,
        status=VisitorStatus.SIGNED_IN,
        signed_in_at=datetime.utcnow(),
        signin_station_id=signin_station_id,
        station_label=station_label,
    )
    db.add(row)
    db.flush()  # assign PK so the audit row carries resource_id

    audit.log_create(
        resource_type="visitor_log",
        resource_id=row.id,
        resource_identifier=row.visitor_name,
        new_values=row,
        description=f"Visitor signed in: {row.visitor_name}"
        + (f" (station '{station_label}')" if station_label else ""),
    )
    db.commit()
    db.refresh(row)

    if host:
        _notify_host_best_effort(db, host=host, row=row)

    return row


def manual_entry(
    db: Session,
    *,
    company_id: int,
    payload: VisitorManualEntryRequest,
    entered_by_user_id: int,
    audit: AuditService,
) -> VisitorLog:
    """Staff back-entry of an offline visit with its ACTUAL times (tenant-scoped, audited).

    Unlike ``sign_in`` — which stamps ``signed_in_at = utcnow()`` from a live
    tablet/station — this records the real, past sign-in/out times an
    ADMIN/MANAGER supplies for a paper-logged visit after a lobby-tablet outage.
    The row is positively marked staff-entered: ``signin_station_id`` /
    ``station_label`` stay NULL and ``entered_by_user_id`` attributes the
    creating staff member, so it never masquerades as a live lobby capture. A
    supplied ``signed_out_at`` closes the visit (status SIGNED_OUT); otherwise the
    visitor is still on-site (SIGNED_IN).

    No host check-in email is sent: the visit already happened, so a live
    "visitor arrived" notification would be misleading (contrast ``sign_in``).
    """
    host = _match_host_user(db, company_id=company_id, host_name=payload.host_name)

    closed = payload.signed_out_at is not None
    row = VisitorLog(
        company_id=company_id,
        visitor_name=payload.visitor_name,
        visitor_company=payload.visitor_company,
        visitor_phone=payload.visitor_phone,
        host_name=payload.host_name,
        host_user_id=host.id if host else None,
        purpose=payload.purpose,
        purpose_note=payload.purpose_note,
        safety_acknowledged=payload.safety_acknowledged,
        status=VisitorStatus.SIGNED_OUT if closed else VisitorStatus.SIGNED_IN,
        signed_in_at=payload.signed_in_at,
        signed_out_at=payload.signed_out_at,
        signin_station_id=None,  # staff back-entry — never a station capture
        station_label=None,
        entered_by_user_id=entered_by_user_id,
    )
    db.add(row)
    db.flush()  # assign PK so the audit row carries resource_id

    audit.log_create(
        resource_type="visitor_log",
        resource_id=row.id,
        resource_identifier=row.visitor_name,
        new_values=row,
        description=f"Visitor visit back-entered by staff: {row.visitor_name}",
    )
    db.commit()
    db.refresh(row)
    return row


def sign_out(
    db: Session,
    *,
    company_id: int,
    name: Optional[str] = None,
    visitor_log_id: Optional[int] = None,
    audit: AuditService,
) -> VisitorLog:
    """Sign out an OPEN (SIGNED_IN) visitor row (tenant-scoped, audited).

    By ``visitor_log_id``: exact open row or 404. By ``name``: 1 open match →
    sign out; >1 → 409 with a minimal disambiguation list; 0 → 404.
    """
    base = db.query(VisitorLog).filter(
        VisitorLog.company_id == company_id,
        VisitorLog.is_deleted == False,  # noqa: E712
        VisitorLog.status == VisitorStatus.SIGNED_IN,
    )

    if visitor_log_id is not None:
        row = base.filter(VisitorLog.id == visitor_log_id).first()
        if row is None:
            raise HTTPException(status_code=404, detail="No open visitor record found")
    else:
        matches = (
            base.filter(func.lower(VisitorLog.visitor_name) == (name or "").strip().lower())
            .order_by(VisitorLog.signed_in_at.desc())
            .all()
        )
        if len(matches) == 0:
            raise HTTPException(status_code=404, detail="No open visitor record found for that name")
        if len(matches) > 1:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Multiple visitors signed in under that name — choose one to sign out",
                    "matches": [
                        {
                            "id": m.id,
                            "visitor_company": m.visitor_company,
                            "signed_in_at": to_utc_iso(m.signed_in_at),
                        }
                        for m in matches
                    ],
                },
            )
        row = matches[0]

    row.status = VisitorStatus.SIGNED_OUT
    row.signed_out_at = datetime.utcnow()

    audit.log_status_change(
        resource_type="visitor_log",
        resource_id=row.id,
        resource_identifier=row.visitor_name,
        old_status=VisitorStatus.SIGNED_IN.value,
        new_status=VisitorStatus.SIGNED_OUT.value,
        description=f"Visitor signed out: {row.visitor_name}",
    )
    db.commit()
    db.refresh(row)
    return row


def list_visitors(
    db: Session,
    *,
    company_id: int,
    status: Optional[VisitorStatus] = None,
    q: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    on_site_only: bool = False,
    skip: int = 0,
    limit: int = 50,
) -> Tuple[List[VisitorLog], int]:
    """Tenant-scoped, soft-delete-filtered visitor list, newest first. Returns (items, total)."""
    query = db.query(VisitorLog).filter(
        VisitorLog.company_id == company_id,
        VisitorLog.is_deleted == False,  # noqa: E712
    )

    if on_site_only:
        query = query.filter(VisitorLog.status == VisitorStatus.SIGNED_IN)
    elif status is not None:
        query = query.filter(VisitorLog.status == status)

    if q:
        like = f"%{q.strip()}%"
        query = query.filter(
            func.lower(VisitorLog.visitor_name).like(func.lower(like))
            | func.lower(func.coalesce(VisitorLog.visitor_company, "")).like(func.lower(like))
            | func.lower(func.coalesce(VisitorLog.host_name, "")).like(func.lower(like))
        )

    if date_from is not None:
        query = query.filter(VisitorLog.signed_in_at >= date_from)
    if date_to is not None:
        query = query.filter(VisitorLog.signed_in_at <= date_to)

    total = query.count()
    items = query.order_by(VisitorLog.signed_in_at.desc()).offset(skip).limit(limit).all()
    return items, total


def get_visitor(db: Session, *, company_id: int, visitor_log_id: int) -> VisitorLog:
    """Fetch a single non-deleted visitor row, tenant-scoped, or 404."""
    row = (
        db.query(VisitorLog)
        .filter(
            VisitorLog.id == visitor_log_id,
            VisitorLog.company_id == company_id,
            VisitorLog.is_deleted == False,  # noqa: E712
        )
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Visitor record not found")
    return row


def soft_delete_visitor(
    db: Session,
    *,
    company_id: int,
    visitor_log_id: int,
    user: User,
    audit: AuditService,
) -> None:
    """Soft-delete a visitor row (tenant-scoped, audited). No physical delete."""
    row = get_visitor(db, company_id=company_id, visitor_log_id=visitor_log_id)

    audit.log_delete(
        resource_type="visitor_log",
        resource_id=row.id,
        resource_identifier=row.visitor_name,
        old_values=row,
        soft_delete=True,
    )
    row.soft_delete(user.id)
    db.commit()
