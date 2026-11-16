"""Opening and resolving machine downtime events -- the one implementation.

Extracted from ``app/api/endpoints/downtime.py`` (``POST /downtime/`` and
``POST /downtime/{id}/resolve``) so the maintenance work-order verbs record a stoppage the
exact same way an operator does: the row, the ``downtime_started`` / ``downtime_resolved``
OperationalEvent (best-effort -- it feeds notifications, not the record), and the audit row.
Neither helper commits; the caller owns the transaction. Request validation (who may report,
whose machine, an end time before the start) stays with the callers.

Maintenance work order -> downtime (P2.3, owner decision 2026-11-13) lives here too:

* Only a WORK-CENTER-targeted maintenance WO with ``requires_shutdown`` touches downtime, and
  only on its own company's machine. An equipment-asset WO never does -- assets are not in
  OEE, the wallboard or the downtime reports, by design.
* A machine never carries two open events for one stoppage: every downtime total (OEE, the
  downtime summary and analytics, adoption metrics) sums ``duration_minutes`` without merging
  overlaps. So start opens an event on the WO's work center -- preventive / predictive ->
  PLANNED_MAINTENANCE + PLANNED (outside OEE loss), corrective / emergency -> MECHANICAL +
  UNPLANNED -- linked by ``downtime_events.maintenance_work_order_id`` ONLY when the machine
  has no open event: an operator's open event is ADOPTED (``maintenance_adopted``), and an
  event another started shutdown WO already holds is SHARED (this WO opens nothing).
* When a WO ends, each open event it owns is handed over to another shutdown WO still holding
  the machine down; failing that, a CANCELLED WO hands an adopted operator's event back to the
  machine, still open; anything else is resolved. Complete also fills the WO's
  ``downtime_minutes`` from the events it resolved when the request left it at 0.
* The other direction is closed too: ``POST /downtime/`` refuses (409) an operator's report on a
  machine a maintenance WO already holds down (``maintenance_hold_on_machine``) -- the
  stoppage is already being recorded, and a second open event would count it twice.
* A completed or cancelled maintenance WO is final (``PUT`` refuses any status change out of
  either), and a WO that was ever completed is never taken as holding a machine down.
* Start and end take a row lock on the work center first, so concurrent starts / ends on one
  machine are serialized (PostgreSQL).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.models.downtime import DowntimeCategory, DowntimeEvent, DowntimePlannedType
from app.models.maintenance import MaintenanceStatus, MaintenanceType, MaintenanceWorkOrder
from app.models.work_center import WorkCenter
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

# A shutdown maintenance WO in one of these, once started, is holding its machine down.
_HOLDING_STATUSES = (MaintenanceStatus.IN_PROGRESS, MaintenanceStatus.ON_HOLD)

LINK_OPENED = "opened"  # this WO opened the event
LINK_ADOPTED = "adopted"  # an operator's open event, linked to this WO
LINK_OWNED = "owned"  # the WO already held it (resumed from hold)
LINK_SHARED = "shared"  # the machine is already down under another maintenance WO


@dataclass
class MaintenanceDowntimeLink:
    """The open downtime event a started maintenance WO is recorded against, and how."""

    event: DowntimeEvent
    kind: str
    shared_with: Optional[MaintenanceWorkOrder] = None

    def audit_extra(self) -> Dict[str, Any]:
        extra: Dict[str, Any] = {"downtime_event_id": self.event.id, "downtime_link": self.kind}
        if self.shared_with is not None:
            extra["downtime_shared_with"] = self.shared_with.wo_number
        return extra


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
    """Only a work-center-targeted WO that shuts the machine down records downtime -- and only
    on a machine of its OWN company. A legacy row whose ``work_center_id`` points at another
    tenant's machine (written before the maintenance writes checked whose machine it was) must
    never open an event there: the downtime reads would render that machine back to this
    tenant, the very egress ``_work_center_in_company`` exists to stop."""
    if not wo.requires_shutdown or wo.work_center_id is None:
        return False
    work_center = wo.work_center
    return work_center is not None and work_center.company_id == wo.company_id


def _lock_machine(db: Session, company_id: int, work_center_id: Optional[int]) -> None:
    """Serialize every maintenance downtime decision on one machine.

    Start (open / adopt / share) and end (resolve / hand over / hand back) each read the
    machine's open events and then write; without a common lock, two concurrent starts would
    each see no open event and open two, and a completion racing a start could hand an event to
    a work order that is about to stop. The work-center row is that common lock (PostgreSQL;
    SQLite ignores it). Callers have already locked their own maintenance WO row, and no audit
    row has been written yet -- the audit chain lock is global, so it must come after."""
    if work_center_id is None:
        return
    (
        db.query(WorkCenter.id)
        .filter(WorkCenter.id == work_center_id, WorkCenter.company_id == company_id)
        .with_for_update()
        .first()
    )


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


def _holding_work_order(
    db: Session, company_id: int, work_order_id: Optional[int], *, excluding: Optional[int] = None
) -> Optional[MaintenanceWorkOrder]:
    """The maintenance WO with this id if it is still holding a machine down, else None."""
    if work_order_id is None or work_order_id == excluding:
        return None
    return (
        db.query(MaintenanceWorkOrder)
        .filter(
            MaintenanceWorkOrder.id == work_order_id,
            MaintenanceWorkOrder.company_id == company_id,
            MaintenanceWorkOrder.status.in_(_HOLDING_STATUSES),
            MaintenanceWorkOrder.started_at.isnot(None),
            # A work order that was ever completed has ended, whatever its status says now (a
            # pre-guard PUT could reopen one): it must never be taken as holding the machine.
            MaintenanceWorkOrder.completed_at.is_(None),
        )
        .first()
    )


def _successor(
    db: Session, company_id: int, wo: MaintenanceWorkOrder, work_center_id: int
) -> Optional[MaintenanceWorkOrder]:
    """Another shutdown maintenance WO still holding ``work_center_id`` down, earliest first."""
    return (
        db.query(MaintenanceWorkOrder)
        .filter(
            MaintenanceWorkOrder.company_id == company_id,
            MaintenanceWorkOrder.work_center_id == work_center_id,
            MaintenanceWorkOrder.id != wo.id,
            MaintenanceWorkOrder.requires_shutdown == True,  # noqa: E712
            MaintenanceWorkOrder.status.in_(_HOLDING_STATUSES),
            MaintenanceWorkOrder.started_at.isnot(None),
            MaintenanceWorkOrder.completed_at.is_(None),  # see _holding_work_order
        )
        .order_by(MaintenanceWorkOrder.started_at, MaintenanceWorkOrder.id)
        .first()
    )


def _relink(
    event: DowntimeEvent,
    *,
    to: Optional[MaintenanceWorkOrder],
    adopted: bool,
    audit: AuditService,
    description: str,
    source: str,
) -> None:
    old_values = {
        "maintenance_work_order_id": event.maintenance_work_order_id,
        "maintenance_adopted": event.maintenance_adopted,
    }
    event.maintenance_work_order_id = to.id if to is not None else None
    event.maintenance_adopted = adopted
    new_values = {"maintenance_work_order_id": event.maintenance_work_order_id, "maintenance_adopted": adopted}
    audit.log_update(
        "downtime_event",
        event.id,
        event_identifier(event),
        old_values=old_values,
        new_values=new_values,
        description=description,
        extra_data={"source": source, "maintenance_wo_number": to.wo_number if to is not None else None},
    )


def maintenance_hold_on_machine(
    db: Session, company_id: int, work_center_id: int
) -> Optional[Tuple[DowntimeEvent, MaintenanceWorkOrder]]:
    """The open downtime event a maintenance WO is holding ``work_center_id`` down under, and
    that WO -- or None. For ``POST /downtime/``, which must not open a second event for a
    stoppage the maintenance WO is already recording (a machine never carries two open events
    for one stoppage; every downtime total sums ``duration_minutes`` without merging overlaps).

    Takes the machine lock first -- the lock maintenance start and end take -- so a report
    racing a start cannot each see the other's absence. Writes nothing."""
    _lock_machine(db, company_id, work_center_id)
    open_on_machine = (
        db.query(DowntimeEvent)
        .filter(
            DowntimeEvent.company_id == company_id,
            DowntimeEvent.work_center_id == work_center_id,
            DowntimeEvent.end_time.is_(None),
            DowntimeEvent.maintenance_work_order_id.isnot(None),
        )
        .order_by(DowntimeEvent.start_time, DowntimeEvent.id)
        .all()
    )
    for event in open_on_machine:
        holder = _holding_work_order(db, company_id, event.maintenance_work_order_id)
        if holder is not None:
            return event, holder
    return None


def open_or_link_maintenance_downtime(
    db: Session,
    wo: MaintenanceWorkOrder,
    *,
    company_id: int,
    actor_id: int,
    audit: AuditService,
    started_at: datetime,
) -> Optional[MaintenanceDowntimeLink]:
    """On maintenance WO start: the open downtime event the WO is recorded against.

    Returns None (and writes nothing) for a WO that does not cause downtime. NEVER opens a
    second open event on a machine -- every downtime total (OEE, the downtime summary and
    analytics, adoption metrics) sums ``duration_minutes`` without merging overlaps, so two
    open events on one machine count the stoppage twice. In order:

    1. an open event this WO already owns (resumed from hold) -- ``owned``;
    2. an open event on the machine that no maintenance WO owns (an operator logged the
       breakdown first) -- linked to this WO and marked adopted, ``adopted``;
    3. an open event another maintenance WO owns while it still holds the machine down -- the
       machine is ALREADY down, so this WO opens nothing and shares it, ``shared``. When that
       WO ends first it hands the event to this one (``end_maintenance_downtime``);
    4. an open event owned by a WO that has already ended (an orphan) -- taken over, ``adopted``
       semantics unchanged;
    5. otherwise a new event, ``opened``.
    """
    if not maintenance_wo_causes_downtime(wo):
        return None
    assert wo.work_center_id is not None  # maintenance_wo_causes_downtime
    _lock_machine(db, company_id, wo.work_center_id)

    owned = _open_events_linked_to(db, company_id, wo)
    if owned:
        return MaintenanceDowntimeLink(owned[0], LINK_OWNED)

    open_on_machine = (
        db.query(DowntimeEvent)
        .filter(
            DowntimeEvent.company_id == company_id,
            DowntimeEvent.work_center_id == wo.work_center_id,
            DowntimeEvent.end_time.is_(None),
        )
        .order_by(DowntimeEvent.start_time, DowntimeEvent.id)
        .with_for_update()
        .all()
    )
    unowned = next((event for event in open_on_machine if event.maintenance_work_order_id is None), None)
    if unowned is not None:
        _relink(
            unowned,
            to=wo,
            adopted=True,
            audit=audit,
            description=(
                f"Linked open downtime event {event_identifier(unowned)} to maintenance work order "
                f"{wo.wo_number} (adopted, not doubled)"
            ),
            source="maintenance_work_order_start",
        )
        db.flush()
        return MaintenanceDowntimeLink(unowned, LINK_ADOPTED)

    for event in open_on_machine:
        holder = _holding_work_order(db, company_id, event.maintenance_work_order_id, excluding=wo.id)
        if holder is not None:
            return MaintenanceDowntimeLink(event, LINK_SHARED, shared_with=holder)
    if open_on_machine:
        orphan = open_on_machine[0]
        _relink(
            orphan,
            to=wo,
            adopted=bool(orphan.maintenance_adopted),
            audit=audit,
            description=(
                f"Linked open downtime event {event_identifier(orphan)} to maintenance work order "
                f"{wo.wo_number} (its previous work order had already ended)"
            ),
            source="maintenance_work_order_start",
        )
        db.flush()
        return MaintenanceDowntimeLink(orphan, LINK_ADOPTED)

    category, planned_type = maintenance_downtime_classification(wo.maintenance_type)
    event = open_downtime_event(
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
    return MaintenanceDowntimeLink(event, LINK_OPENED)


def end_maintenance_downtime(
    db: Session,
    wo: MaintenanceWorkOrder,
    *,
    company_id: int,
    actor_id: int,
    audit: AuditService,
    ended_at: datetime,
    resolution: str,
    cancelled: bool = False,
) -> List[DowntimeEvent]:
    """When a maintenance WO ends (complete, or ``cancelled``): settle every open event it owns.

    Runs whatever the WO's current ``requires_shutdown`` / target say: an event it opened while
    it did shut the machine down must still settle when it ends. For each owned open event, in
    this order:

    * another shutdown WO still holds the machine down (it shared this event at its start) ->
      HANDED OVER to it, still open, so the stoppage runs on, once, until the last WO ends --
      however this one ended. A completed WO's hand-over clears the adopted mark (the original
      cause has been worked); a cancelled WO's keeps it, so a later cancel of the successor
      still hands the operator's breakdown back to the machine.
    * CANCELLED and the event was ADOPTED from an operator -> handed back to the machine:
      unlinked and left OPEN. The breakdown predates the work order and cancelling the work
      order did not fix it; resolving it would show a broken machine as running.
    * otherwise -> RESOLVED at ``ended_at``.

    Returns the events this call RESOLVED (what the WO's ``downtime_minutes`` may count).
    """
    # The machine lock BEFORE the event locks -- the same order as start, or a start and an end
    # on one machine could each hold what the other waits for.
    _lock_machine(db, company_id, wo.work_center_id)
    owned = _open_events_linked_to(db, company_id, wo)
    if not owned:
        return []

    resolved: List[DowntimeEvent] = []
    for event in owned:
        # The successor FIRST: while another shutdown WO still has the machine down, the
        # stoppage goes on under it whichever way this one ended. Handing an adopted operator's
        # event back to the machine instead would leave it owned by nobody, so the sharer's
        # completion -- the repair -- would never resolve it.
        successor = _successor(db, company_id, wo, event.work_center_id)
        if successor is not None:
            _relink(
                event,
                to=successor,
                # A cancel did not work the operator's breakdown, so the adopted mark goes with
                # it: if the successor is cancelled too, the event is handed back to the machine.
                adopted=bool(event.maintenance_adopted) and cancelled,
                audit=audit,
                description=(
                    f"Maintenance work order {wo.wo_number} ended; downtime event {event_identifier(event)} "
                    f"stays open under {successor.wo_number}, which still has the machine shut down"
                ),
                source="maintenance_work_order_handover",
            )
            continue
        if cancelled and event.maintenance_adopted:
            _relink(
                event,
                to=None,
                adopted=False,
                audit=audit,
                description=(
                    f"Maintenance work order {wo.wo_number} was cancelled; downtime event "
                    f"{event_identifier(event)} stays open on the machine (it was logged before the work order)"
                ),
                source="maintenance_work_order_cancel",
            )
            continue
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
