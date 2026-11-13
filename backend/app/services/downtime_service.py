"""Opening and resolving machine downtime events -- the one implementation.

Extracted from ``app/api/endpoints/downtime.py`` (``POST /downtime/`` and
``POST /downtime/{id}/resolve``) so the maintenance work-order verbs record a stoppage the
exact same way an operator does: the row, the ``downtime_started`` / ``downtime_resolved``
OperationalEvent (best-effort -- it feeds notifications, not the record), and the audit row.
Neither helper commits; the caller owns the transaction. Request validation (who may report,
whose machine, an end time before the start) stays with the callers.

Maintenance work order -> downtime (P2.3, owner decision 2026-11-13) lives here too:

* Only a WORK-CENTER-targeted maintenance WO with ``requires_shutdown`` touches downtime. An
  equipment-asset WO never does -- assets are not in OEE, the wallboard or the downtime
  reports, by design.
* Start opens an event on the WO's work center -- preventive / predictive -> PLANNED_MAINTENANCE
  + PLANNED (outside OEE loss), corrective / emergency -> MECHANICAL + UNPLANNED -- linked by
  ``downtime_events.maintenance_work_order_id``. If the machine already has an OPEN event no
  maintenance WO owns (an operator logged the breakdown first), that event is ADOPTED instead
  of opening a second one: two open events on one machine would double the minutes in OEE and
  the downtime analytics. A WO that already owns an open event (resumed from hold) opens none.
* Complete and cancel resolve the WO's open event(s); complete also fills the WO's
  ``downtime_minutes`` from the linked events when the request left it at 0.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional

from sqlalchemy.orm import Session

from app.models.downtime import DowntimeCategory, DowntimeEvent, DowntimePlannedType
from app.models.maintenance import MaintenanceType, MaintenanceWorkOrder
from app.services.audit_service import AuditService
from app.services.operational_event_service import OperationalEventService


def event_identifier(event: DowntimeEvent) -> str:
    return f"DT-{event.id}"


def _enum_value(value: Any) -> Any:
    return value.value if hasattr(value, "value") else value


def open_downtime_event(
    db: Session,
    *,
    company_id: int,
    work_center_id: int,
    reported_by: int,
    audit: AuditService,
    start_time: Optional[datetime] = None,
    category: DowntimeCategory = DowntimeCategory.OTHER,
    planned_type: DowntimePlannedType = DowntimePlannedType.UNPLANNED,
    reason_code: Optional[str] = None,
    description: Optional[str] = None,
    work_order_id: Optional[int] = None,
    maintenance_work_order_id: Optional[int] = None,
) -> DowntimeEvent:
    """Insert an open downtime event, emit ``downtime_started`` and audit the create.

    The caller has already checked that ``work_center_id`` / ``work_order_id`` are the active
    company's. Flushes; never commits.
    """
    event = DowntimeEvent(
        work_center_id=work_center_id,
        work_order_id=work_order_id,
        maintenance_work_order_id=maintenance_work_order_id,
        start_time=start_time or datetime.utcnow(),
        category=category,
        planned_type=planned_type,
        reason_code=reason_code,
        description=description,
        reported_by=reported_by,
    )
    event.company_id = company_id
    db.add(event)
    db.flush()
    payload = {
        "work_center_id": event.work_center_id,
        "category": _enum_value(category),
        "planned_type": _enum_value(planned_type),
        "reason_code": reason_code,
    }
    if maintenance_work_order_id is not None:
        payload["maintenance_work_order_id"] = maintenance_work_order_id
    OperationalEventService(db).emit_best_effort(
        company_id=company_id,
        event_type="downtime_started",
        source_module="downtime",
        entity_type="downtime_event",
        entity_id=event.id,
        work_order_id=event.work_order_id,
        user_id=reported_by,
        severity="medium" if planned_type == DowntimePlannedType.UNPLANNED else "info",
        event_payload=payload,
    )
    audit.log_create("downtime_event", event.id, event_identifier(event), new_values=event)
    return event


def resolve_downtime_event(
    db: Session,
    event: DowntimeEvent,
    *,
    company_id: int,
    resolved_by: int,
    end_time: datetime,
    audit: AuditService,
    resolution: Optional[str] = None,
) -> DowntimeEvent:
    """Close an OPEN event at ``end_time``: duration, resolver, ``downtime_resolved``, audit.

    The caller has row-locked the event and checked it is still open. ``end_time`` earlier
    than the start is clamped to the start (a zero-length stoppage) rather than recording a
    negative duration; ``POST /downtime/{id}/resolve`` refuses that case before calling.
    Flushes nothing itself; never commits.
    """
    if end_time < event.start_time:
        end_time = event.start_time
    event.end_time = end_time
    event.duration_minutes = round((end_time - event.start_time).total_seconds() / 60.0, 2)
    event.resolved_by = resolved_by
    if resolution:
        event.resolution = resolution

    OperationalEventService(db).emit_best_effort(
        company_id=company_id,
        event_type="downtime_resolved",
        source_module="downtime",
        entity_type="downtime_event",
        entity_id=event.id,
        work_order_id=event.work_order_id,
        user_id=resolved_by,
        severity="info",
        event_payload={
            "work_center_id": event.work_center_id,
            "duration_minutes": event.duration_minutes,
            "resolution": event.resolution,
        },
    )
    audit.log_status_change(
        "downtime_event",
        event.id,
        event_identifier(event),
        old_status="active",
        new_status="resolved",
        description=f"Resolved downtime event {event_identifier(event)}",
        extra_data={
            "end_time": end_time.isoformat(),
            "duration_minutes": event.duration_minutes,
            "resolution": event.resolution,
        },
    )
    return event


# ---------------------------------------------------------------------------
# Maintenance work order <-> downtime
# ---------------------------------------------------------------------------

_PLANNED_MAINTENANCE_TYPES = {MaintenanceType.PREVENTIVE, MaintenanceType.PREDICTIVE}


def maintenance_downtime_classification(maintenance_type: Any) -> tuple:
    """``(category, planned_type)`` for a maintenance WO's stoppage.

    Preventive / predictive is planned maintenance (outside OEE availability loss);
    corrective / emergency is a breakdown (MECHANICAL, UNPLANNED -- it counts against OEE).
    """
    try:
        kind = MaintenanceType(_enum_value(maintenance_type))
    except ValueError:
        kind = MaintenanceType.CORRECTIVE
    if kind in _PLANNED_MAINTENANCE_TYPES:
        return DowntimeCategory.PLANNED_MAINTENANCE, DowntimePlannedType.PLANNED
    return DowntimeCategory.MECHANICAL, DowntimePlannedType.UNPLANNED


def maintenance_wo_causes_downtime(wo: MaintenanceWorkOrder) -> bool:
    """Only a work-center-targeted WO that shuts the machine down records downtime."""
    return bool(wo.requires_shutdown) and wo.work_center_id is not None


def _open_events_linked_to(db: Session, company_id: int, wo: MaintenanceWorkOrder) -> List[DowntimeEvent]:
    return (
        db.query(DowntimeEvent)
        .filter(
            DowntimeEvent.company_id == company_id,
            DowntimeEvent.maintenance_work_order_id == wo.id,
            DowntimeEvent.end_time.is_(None),
        )
        .order_by(DowntimeEvent.start_time, DowntimeEvent.id)
        .with_for_update()
        .all()
    )


def open_or_link_maintenance_downtime(
    db: Session,
    wo: MaintenanceWorkOrder,
    *,
    company_id: int,
    actor_id: int,
    audit: AuditService,
    started_at: datetime,
) -> Optional[DowntimeEvent]:
    """On maintenance WO start: the WO's open downtime event, opened or adopted.

    Returns None (and writes nothing) for a WO that does not cause downtime. Never opens a
    second event: an open event this WO already owns is returned as is, and an open event on
    the machine that no maintenance WO owns is adopted (linked, audited) rather than doubled.
    """
    if not maintenance_wo_causes_downtime(wo):
        return None
    owned = _open_events_linked_to(db, company_id, wo)
    if owned:
        return owned[0]

    unowned = (
        db.query(DowntimeEvent)
        .filter(
            DowntimeEvent.company_id == company_id,
            DowntimeEvent.work_center_id == wo.work_center_id,
            DowntimeEvent.end_time.is_(None),
            DowntimeEvent.maintenance_work_order_id.is_(None),
        )
        .order_by(DowntimeEvent.start_time, DowntimeEvent.id)
        .with_for_update()
        .first()
    )
    if unowned is not None:
        unowned.maintenance_work_order_id = wo.id
        db.flush()
        audit.log_update(
            "downtime_event",
            unowned.id,
            event_identifier(unowned),
            old_values={"maintenance_work_order_id": None},
            new_values={"maintenance_work_order_id": wo.id},
            description=(
                f"Linked open downtime event {event_identifier(unowned)} to maintenance work order "
                f"{wo.wo_number} (adopted, not doubled)"
            ),
            extra_data={"source": "maintenance_work_order_start", "maintenance_wo_number": wo.wo_number},
        )
        return unowned

    category, planned_type = maintenance_downtime_classification(wo.maintenance_type)
    assert wo.work_center_id is not None  # maintenance_wo_causes_downtime
    return open_downtime_event(
        db,
        company_id=company_id,
        work_center_id=wo.work_center_id,
        reported_by=actor_id,
        audit=audit,
        start_time=started_at,
        category=category,
        planned_type=planned_type,
        description=f"Maintenance WO {wo.wo_number}: {wo.title}"[:2000],
        maintenance_work_order_id=wo.id,
    )


def resolve_maintenance_downtime(
    db: Session,
    wo: MaintenanceWorkOrder,
    *,
    company_id: int,
    actor_id: int,
    audit: AuditService,
    ended_at: datetime,
    resolution: str,
) -> List[DowntimeEvent]:
    """Resolve every open downtime event the WO owns (complete / cancel). Returns them.

    Runs whatever the WO's current ``requires_shutdown`` / target say: an event it opened
    while it did shut the machine down must still close when it ends.
    """
    resolved = []
    for event in _open_events_linked_to(db, company_id, wo):
        resolve_downtime_event(
            db,
            event,
            company_id=company_id,
            resolved_by=actor_id,
            end_time=ended_at,
            audit=audit,
            resolution=resolution,
        )
        resolved.append(event)
    if resolved:
        db.flush()
    return resolved


def linked_downtime_minutes(db: Session, company_id: int, wo: MaintenanceWorkOrder) -> Optional[float]:
    """Total minutes of the WO's RESOLVED linked events, or None when it has none."""
    events = (
        db.query(DowntimeEvent)
        .filter(
            DowntimeEvent.company_id == company_id,
            DowntimeEvent.maintenance_work_order_id == wo.id,
            DowntimeEvent.end_time.isnot(None),
        )
        .all()
    )
    if not events:
        return None
    return round(sum(event.duration_minutes or 0 for event in events), 2)
