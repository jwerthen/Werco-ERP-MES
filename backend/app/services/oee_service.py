"""OEE auto-calculation service (Lean Phase 1 / issue #88).

Extraction of the ~220-line calculation that lived inline in
``POST /oee/calculate/{work_center_id}`` (endpoints/oee.py), verbatim-equivalent
on the STAFFED-time availability convention (Batch 8 / rank 11 -- see the
docstring on :func:`compute_oee_for_work_center`), so the endpoint becomes a
thin delegate and the nightly ARQ cron (``app/jobs/oee_jobs.py``) can reuse the
exact same math. The only additions over the extracted code are:

* ``calculation_source`` stamping ('manual' for the endpoint trigger, 'auto'
  for the cron -- ``CalculationSource`` in app/models/oee.py), and
* the ``uq_oee_company_wc_date_shift`` IntegrityError (migration 063) surfacing
  as :class:`OEERecordConflictError` so callers map it to a clean 409 instead
  of a 500.
"""

import logging
from datetime import date
from typing import Optional

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.models.downtime import DowntimeEvent, DowntimePlannedType
from app.models.oee import CalculationSource, OEERecord
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.work_center import WorkCenter
from app.models.work_order import WorkOrderOperation
from app.services.audit_service import AuditService

logger = logging.getLogger(__name__)

# Production-bearing time-entry types (OEE-5): pieces/scrap counted from these
# uniformly across the auto-calc, matching ``analytics_service`` so a quantity logged
# on a REWORK clock-out is never silently dropped.
PRODUCTION_BEARING_ENTRY_TYPES = [TimeEntryType.RUN, TimeEntryType.REWORK]
# RUN + SETUP are the productive-run portion of clocked time (availability numerator).
PRODUCTIVE_RUN_ENTRY_TYPES = [TimeEntryType.RUN, TimeEntryType.SETUP]


class OEERecordConflictError(Exception):
    """A concurrent writer already created the (company, WC, date, shift) record.

    Raised when the ``uq_oee_company_wc_date_shift`` unique index (migration 063)
    rejects the insert; callers translate to HTTP 409 (endpoints) or a logged
    skip (cron).
    """


def calculate_oee(
    planned_production_time_minutes: float,
    actual_run_time_minutes: float,
    total_parts_produced: int,
    ideal_cycle_time_seconds: float,
    actual_operating_time_minutes: float,
    good_parts: int,
    total_parts: int,
) -> dict:
    """Calculate OEE = Availability x Performance x Quality"""
    # Availability = actual_run_time / planned_production_time
    if planned_production_time_minutes > 0:
        availability = (actual_run_time_minutes / planned_production_time_minutes) * 100
    else:
        availability = 0.0

    # Performance = (total_parts x ideal_cycle_time) / actual_operating_time
    if actual_operating_time_minutes > 0:
        ideal_run_time_minutes = (total_parts_produced * ideal_cycle_time_seconds) / 60.0
        performance = (ideal_run_time_minutes / actual_operating_time_minutes) * 100
    else:
        performance = 0.0

    # Quality = good_parts / total_parts
    if total_parts > 0:
        quality = (good_parts / total_parts) * 100
    else:
        quality = 0.0

    # Cap at 100%
    availability = min(availability, 100.0)
    performance = min(performance, 100.0)
    quality = min(quality, 100.0)

    # OEE = A x P x Q (as percentages: divide by 100^2 to get the right result)
    oee = (availability * performance * quality) / 10000.0

    return {
        "availability_pct": round(availability, 2),
        "performance_pct": round(performance, 2),
        "quality_pct": round(quality, 2),
        "oee_pct": round(oee, 2),
    }


def work_center_has_activity(db: Session, company_id: int, work_center_id: int, record_date: date) -> bool:
    """True when the WC has any closed clocked entry OR unplanned downtime that day.

    Used by the nightly cron to skip idle work centers -- writing an all-zero
    OEERecord for every idle WC daily would be noise, and a window with no
    staffed time is genuinely uncomputable (OEE-4), not a measured zero.
    """
    has_entry = (
        db.query(TimeEntry.id)
        .filter(
            TimeEntry.company_id == company_id,
            TimeEntry.work_center_id == work_center_id,
            func.date(TimeEntry.clock_in) == record_date,
            TimeEntry.clock_out.isnot(None),
        )
        .first()
    )
    if has_entry:
        return True
    has_downtime = (
        db.query(DowntimeEvent.id)
        .filter(
            DowntimeEvent.company_id == company_id,
            DowntimeEvent.work_center_id == work_center_id,
            func.date(DowntimeEvent.start_time) == record_date,
            DowntimeEvent.planned_type == DowntimePlannedType.UNPLANNED,
        )
        .first()
    )
    return has_downtime is not None


def find_existing_record(
    db: Session, company_id: int, work_center_id: int, record_date: date, shift: Optional[str]
) -> Optional[OEERecord]:
    """Tenant-scoped lookup of the (WC, date, shift) record this calc would target."""
    return (
        db.query(OEERecord)
        .filter(
            OEERecord.company_id == company_id,
            OEERecord.work_center_id == work_center_id,
            OEERecord.record_date == record_date,
            OEERecord.shift == shift,
        )
        .first()
    )


def compute_oee_for_work_center(
    db: Session,
    company_id: int,
    work_center: WorkCenter,
    record_date: date,
    shift: Optional[str],
    *,
    calculation_source: CalculationSource,
    created_by_user_id: Optional[int],
    audit: AuditService,
) -> OEERecord:
    """Compute and persist a real OEERecord for a work center/date from existing data.

    Computes from the day's clocked TimeEntries, the routing standard cycle time,
    and reported DowntimeEvents -- on the STAFFED-time convention (Batch 8 / rank
    11), verbatim-equivalent to the former inline endpoint calculation:

      * Availability = productive run (clocked RUN+SETUP minus reported
        DowntimeEvent time) / STAFFED (clocked) minutes at the WC that day (OEE-4)
        -- NOT the plant calendar, so idle/un-clocked time is excluded and
        availability is not pinned ~1.
      * Performance = ideal cycle (routing ``run_time_per_piece``) x pieces /
        productive run time, ideal cycle DERIVED, not assumed (OEE-7).
      * Quality = good / total, where scrap comes from ``TimeEntry.quantity_scrapped``
        on the production-bearing entry types (OEE-7), not assumed all-good.

    Overwrites an existing (WC, date, shift) record in place (audited as an
    update); creates + audits otherwise. COMMITS the unit of work (the audit rows
    land atomically with the record, matching the former endpoint behavior).
    ``calculation_source`` is stamped on both branches. The caller decides the
    skip policy (the cron never overwrites a 'manual' row; the manual endpoint
    overwrites anything). Raises :class:`OEERecordConflictError` when a
    concurrent writer wins the create race on ``uq_oee_company_wc_date_shift``.

    ``work_center`` must already be resolved tenant-scoped by the caller.
    """
    work_center_id = work_center.id

    # Gather the day's CLOSED clocked entries for this WC (tenant-scoped). Use clock_in/
    # clock_out (OEE-1 fix: there is no start_time/end_time on TimeEntry).
    time_entries = (
        db.query(TimeEntry)
        .filter(
            TimeEntry.company_id == company_id,
            TimeEntry.work_center_id == work_center_id,
            func.date(TimeEntry.clock_in) == record_date,
            TimeEntry.clock_out.isnot(None),
        )
        .all()
    )

    def _entry_minutes(te: TimeEntry) -> float:
        # Prefer the stored duration_hours; fall back to the clock span.
        if te.duration_hours is not None:
            return float(te.duration_hours) * 60.0
        if te.clock_in and te.clock_out:
            return (te.clock_out - te.clock_in).total_seconds() / 60.0
        return 0.0

    staffed_minutes = 0.0  # ALL clocked entries -> availability denominator (OEE-4)
    run_minutes = 0.0  # RUN+SETUP -> productive run (availability numerator)
    good_count = 0  # production-bearing good pieces (quantity_produced, OEE-5)
    scrap_count = 0  # production-bearing scrapped pieces (OEE-7)
    for te in time_entries:
        minutes = _entry_minutes(te)
        staffed_minutes += minutes
        if te.entry_type in PRODUCTIVE_RUN_ENTRY_TYPES:
            run_minutes += minutes
        if te.entry_type in PRODUCTION_BEARING_ENTRY_TYPES:
            good_count += int(te.quantity_produced or 0)
            scrap_count += int(te.quantity_scrapped or 0)

    # quantity_produced is the GOOD count (it increments quantity_complete on clock-out),
    # so total pieces cycled = good + scrap. Quality = good / (good + scrap) (OEE-7).
    total_parts = good_count + scrap_count  # all pieces cycled (perf + quality denom)

    # Reported machine downtime for this WC/day (OEE-7).
    downtime_minutes = float(
        db.query(func.coalesce(func.sum(DowntimeEvent.duration_minutes), 0.0))
        .filter(
            DowntimeEvent.company_id == company_id,
            DowntimeEvent.work_center_id == work_center_id,
            func.date(DowntimeEvent.start_time) == record_date,
            DowntimeEvent.planned_type == DowntimePlannedType.UNPLANNED,
        )
        .scalar()
        or 0.0
    )

    # Productive run = clocked RUN+SETUP minus reported downtime.
    productive_run_minutes = max(0.0, run_minutes - downtime_minutes)

    # Ideal cycle DERIVED from routing run_time_per_piece (OEE-7), quantity-weighted
    # over the production-bearing pieces (good + scrap) cycled at this WC today; every
    # piece run consumes a standard cycle, so weight by (produced + scrapped).
    # run_time_per_piece is stored in hours alongside run_time_hours.
    ideal_run_hours = float(
        db.query(
            func.coalesce(
                func.sum(
                    (TimeEntry.quantity_produced + TimeEntry.quantity_scrapped) * WorkOrderOperation.run_time_per_piece
                ),
                0.0,
            )
        )
        .select_from(TimeEntry)
        .join(WorkOrderOperation, TimeEntry.operation_id == WorkOrderOperation.id)
        .filter(
            TimeEntry.company_id == company_id,
            # Defense-in-depth: also scope the JOINED side. The FK makes a
            # cross-tenant row unlikely, but this code now runs in the nightly
            # cron across every tenant -- both sides of the join stay pinned.
            WorkOrderOperation.company_id == company_id,
            TimeEntry.work_center_id == work_center_id,
            func.date(TimeEntry.clock_in) == record_date,
            TimeEntry.clock_out.isnot(None),
            TimeEntry.entry_type.in_(PRODUCTION_BEARING_ENTRY_TYPES),
        )
        .scalar()
        or 0.0
    )
    # Per-piece ideal cycle in seconds for the stored OEERecord (0 when no standard/no
    # parts -> performance leg degrades to 0, not a misleading 60 s assumption).
    ideal_cycle_time_seconds = (ideal_run_hours * 3600.0 / total_parts) if total_parts > 0 else 0.0

    # Quality from real scrap (OEE-7): good = produced (good count); defect = scrapped.
    good_parts = good_count
    defect_parts = scrap_count
    rework_parts = 0

    # Availability basis = STAFFED minutes (OEE-4): feed calculate_oee planned=staffed,
    # actual_run=productive_run so availability = productive_run / staffed.
    planned_time = staffed_minutes

    # Calculate OEE on the staffed-time basis. Performance basis = productive run.
    oee_calcs = calculate_oee(
        planned_production_time_minutes=planned_time,
        actual_run_time_minutes=productive_run_minutes,
        total_parts_produced=total_parts,
        ideal_cycle_time_seconds=ideal_cycle_time_seconds,
        actual_operating_time_minutes=productive_run_minutes,
        good_parts=good_parts,
        total_parts=total_parts,
    )
    # Stored on the record as the availability denominator / numerator and the loss split.
    actual_run_minutes = productive_run_minutes
    downtime = downtime_minutes

    # Check for existing record
    existing = find_existing_record(db, company_id, work_center_id, record_date, shift)

    if existing:
        # Snapshot pre-mutation values for the audit diff (the live model is mutated below).
        old_values = {c.key: getattr(existing, c.key) for c in existing.__table__.columns}
        existing.planned_production_time_minutes = planned_time
        existing.actual_run_time_minutes = actual_run_minutes
        existing.downtime_minutes = downtime
        existing.total_parts_produced = total_parts
        existing.ideal_cycle_time_seconds = ideal_cycle_time_seconds
        existing.actual_operating_time_minutes = actual_run_minutes
        existing.good_parts = good_parts
        existing.total_parts = total_parts
        existing.defect_parts = defect_parts
        existing.rework_parts = rework_parts
        # Six-big-losses: reported machine downtime is an unplanned-stop loss; scrap is a
        # production reject (so the loss/dashboard breakdown reflects real data, OEE-7).
        existing.unplanned_stop_minutes = downtime
        existing.production_reject_count = defect_parts
        existing.calculation_source = calculation_source.value
        for field, value in oee_calcs.items():
            setattr(existing, field, value)
        # Audit (tamper-evident) the recomputed overwrite BEFORE the terminal commit so it
        # commits atomically with the record. A single representative row per call.
        db.flush()
        audit.log_update(
            resource_type="oee_record",
            resource_id=existing.id,
            resource_identifier=str(existing.id),
            old_values=old_values,
            new_values=existing,
            description=f"Auto-calculated OEE record {existing.id} for work center {work_center.name}",
        )
        db.commit()
        db.refresh(existing)
        record = existing
    else:
        record = OEERecord(
            work_center_id=work_center_id,
            record_date=record_date,
            shift=shift,
            calculation_source=calculation_source.value,
            planned_production_time_minutes=planned_time,
            actual_run_time_minutes=actual_run_minutes,
            downtime_minutes=downtime,
            total_parts_produced=total_parts,
            ideal_cycle_time_seconds=ideal_cycle_time_seconds,
            actual_operating_time_minutes=actual_run_minutes,
            good_parts=good_parts,
            total_parts=total_parts,
            defect_parts=defect_parts,
            rework_parts=rework_parts,
            unplanned_stop_minutes=downtime,
            production_reject_count=defect_parts,
            **oee_calcs,
            created_by=created_by_user_id,
        )
        record.company_id = company_id
        db.add(record)
        # Audit (tamper-evident) the freshly created record BEFORE the terminal commit so it
        # commits atomically. Flush so the PK is populated. A single representative row.
        try:
            db.flush()
        except IntegrityError as exc:
            # uq_oee_company_wc_date_shift (migration 063): a concurrent writer created
            # the (company, WC, date, shift) record between our lookup and this insert
            # (the COALESCE key also makes NULL and '' shift the same record).
            db.rollback()
            raise OEERecordConflictError(
                f"An OEE record already exists for work center {work_center_id} on {record_date} " f"(shift={shift!r})"
            ) from exc
        audit.log_create(
            resource_type="oee_record",
            resource_id=record.id,
            resource_identifier=str(record.id),
            new_values=record,
            description=f"Auto-calculated OEE record {record.id} for work center {work_center.name}",
        )
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise OEERecordConflictError(
                f"An OEE record already exists for work center {work_center_id} on {record_date} " f"(shift={shift!r})"
            ) from exc
        db.refresh(record)

    # Reload with relationship
    record = (
        db.query(OEERecord)
        .options(joinedload(OEERecord.work_center))
        .filter(OEERecord.id == record.id, OEERecord.company_id == company_id)
        .first()
    )
    return record
