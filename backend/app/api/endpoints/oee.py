from datetime import date, datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.api.deps import get_current_company_id, get_current_user, require_role
from app.db.database import get_db
from app.models.downtime import DowntimeEvent, DowntimePlannedType
from app.models.oee import OEERecord, OEETarget
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import WorkOrderOperation

# Production-bearing time-entry types (OEE-5): pieces/scrap counted from these
# uniformly across the auto-calc, matching ``analytics_service`` so a quantity logged
# on a REWORK clock-out is never silently dropped.
PRODUCTION_BEARING_ENTRY_TYPES = [TimeEntryType.RUN, TimeEntryType.REWORK]
# RUN + SETUP are the productive-run portion of clocked time (availability numerator).
PRODUCTIVE_RUN_ENTRY_TYPES = [TimeEntryType.RUN, TimeEntryType.SETUP]

# RBAC: OEE WRITE/mutation endpoints (records, targets, auto-calculate) are gated to the
# same role set as the sibling Analytics router — Operators/Viewers can VIEW dashboards
# but must not create/overwrite OEE records or targets. READ endpoints stay on
# ``get_current_user`` so the shop floor can still see OEE dashboards.
OEE_WRITE_ROLES = [UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]

router = APIRouter()


# ============== Pydantic Schemas ==============


class OEERecordCreate(BaseModel):
    work_center_id: int
    record_date: date
    shift: Optional[str] = None
    planned_production_time_minutes: float = 0.0
    actual_run_time_minutes: float = 0.0
    downtime_minutes: float = 0.0
    total_parts_produced: int = 0
    ideal_cycle_time_seconds: float = 0.0
    actual_operating_time_minutes: float = 0.0
    good_parts: int = 0
    total_parts: int = 0
    defect_parts: int = 0
    rework_parts: int = 0
    unplanned_stop_minutes: float = 0.0
    planned_stop_minutes: float = 0.0
    small_stop_minutes: float = 0.0
    slow_cycle_minutes: float = 0.0
    production_reject_count: int = 0
    startup_reject_count: int = 0
    notes: Optional[str] = None


class OEERecordUpdate(BaseModel):
    shift: Optional[str] = None
    planned_production_time_minutes: Optional[float] = None
    actual_run_time_minutes: Optional[float] = None
    downtime_minutes: Optional[float] = None
    total_parts_produced: Optional[int] = None
    ideal_cycle_time_seconds: Optional[float] = None
    actual_operating_time_minutes: Optional[float] = None
    good_parts: Optional[int] = None
    total_parts: Optional[int] = None
    defect_parts: Optional[int] = None
    rework_parts: Optional[int] = None
    unplanned_stop_minutes: Optional[float] = None
    planned_stop_minutes: Optional[float] = None
    small_stop_minutes: Optional[float] = None
    slow_cycle_minutes: Optional[float] = None
    production_reject_count: Optional[int] = None
    startup_reject_count: Optional[int] = None
    notes: Optional[str] = None


class OEERecordResponse(BaseModel):
    id: int
    work_center_id: int
    work_center_name: Optional[str] = None
    record_date: date
    shift: Optional[str] = None
    planned_production_time_minutes: float
    actual_run_time_minutes: float
    downtime_minutes: float
    total_parts_produced: int
    ideal_cycle_time_seconds: float
    actual_operating_time_minutes: float
    good_parts: int
    total_parts: int
    defect_parts: int
    rework_parts: int
    availability_pct: float
    performance_pct: float
    quality_pct: float
    oee_pct: float
    unplanned_stop_minutes: float
    planned_stop_minutes: float
    small_stop_minutes: float
    slow_cycle_minutes: float
    production_reject_count: int
    startup_reject_count: int
    notes: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    created_by: Optional[int] = None

    model_config = {"from_attributes": True}


class OEETargetCreate(BaseModel):
    work_center_id: int
    target_oee_pct: float = 85.0
    target_availability_pct: float = 90.0
    target_performance_pct: float = 95.0
    target_quality_pct: float = 99.0


class OEETargetUpdate(BaseModel):
    target_oee_pct: Optional[float] = None
    target_availability_pct: Optional[float] = None
    target_performance_pct: Optional[float] = None
    target_quality_pct: Optional[float] = None


class OEETargetResponse(BaseModel):
    id: int
    work_center_id: int
    work_center_name: Optional[str] = None
    target_oee_pct: float
    target_availability_pct: float
    target_performance_pct: float
    target_quality_pct: float
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


# ============== Helper Functions ==============


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


def _record_to_response(record: OEERecord) -> dict:
    """Convert OEERecord to response dict with work_center_name."""
    data = {
        "id": record.id,
        "work_center_id": record.work_center_id,
        "work_center_name": record.work_center.name if record.work_center else None,
        "record_date": record.record_date,
        "shift": record.shift,
        "planned_production_time_minutes": record.planned_production_time_minutes,
        "actual_run_time_minutes": record.actual_run_time_minutes,
        "downtime_minutes": record.downtime_minutes,
        "total_parts_produced": record.total_parts_produced,
        "ideal_cycle_time_seconds": record.ideal_cycle_time_seconds,
        "actual_operating_time_minutes": record.actual_operating_time_minutes,
        "good_parts": record.good_parts,
        "total_parts": record.total_parts,
        "defect_parts": record.defect_parts,
        "rework_parts": record.rework_parts,
        "availability_pct": record.availability_pct,
        "performance_pct": record.performance_pct,
        "quality_pct": record.quality_pct,
        "oee_pct": record.oee_pct,
        "unplanned_stop_minutes": record.unplanned_stop_minutes or 0.0,
        "planned_stop_minutes": record.planned_stop_minutes or 0.0,
        "small_stop_minutes": record.small_stop_minutes or 0.0,
        "slow_cycle_minutes": record.slow_cycle_minutes or 0.0,
        "production_reject_count": record.production_reject_count or 0,
        "startup_reject_count": record.startup_reject_count or 0,
        "notes": record.notes,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "created_by": record.created_by,
    }
    return data


def _parse_period(period: str) -> date:
    """Convert period string like '7d', '30d', '90d' to a start date."""
    today = date.today()
    if period == "7d":
        return today - timedelta(days=7)
    elif period == "30d":
        return today - timedelta(days=30)
    elif period == "90d":
        return today - timedelta(days=90)
    elif period == "365d":
        return today - timedelta(days=365)
    else:
        return today - timedelta(days=30)


# ============== OEE Record Endpoints ==============


@router.get("/records", response_model=List[OEERecordResponse])
def list_oee_records(
    work_center_id: Optional[int] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    shift: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """List OEE records with optional filters."""
    query = db.query(OEERecord).filter(OEERecord.company_id == company_id).options(joinedload(OEERecord.work_center))

    if work_center_id:
        query = query.filter(OEERecord.work_center_id == work_center_id)
    if date_from:
        query = query.filter(OEERecord.record_date >= date_from)
    if date_to:
        query = query.filter(OEERecord.record_date <= date_to)
    if shift:
        query = query.filter(OEERecord.shift == shift)

    records = query.order_by(OEERecord.record_date.desc()).offset(skip).limit(limit).all()
    return [_record_to_response(r) for r in records]


@router.get("/records/{record_id}", response_model=OEERecordResponse)
def get_oee_record(
    record_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get a single OEE record."""
    record = (
        db.query(OEERecord)
        .options(joinedload(OEERecord.work_center))
        .filter(OEERecord.id == record_id, OEERecord.company_id == company_id)
        .first()
    )

    if not record:
        raise HTTPException(status_code=404, detail="OEE record not found")
    return _record_to_response(record)


@router.post("/records", response_model=OEERecordResponse)
def create_oee_record(
    record_in: OEERecordCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(OEE_WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
):
    """Create a new OEE record with auto-calculated OEE metrics."""
    # Verify work center exists
    wc = (
        db.query(WorkCenter)
        .filter(WorkCenter.id == record_in.work_center_id, WorkCenter.company_id == company_id)
        .first()
    )
    if not wc:
        raise HTTPException(status_code=404, detail="Work center not found")

    # Calculate OEE
    oee_calcs = calculate_oee(
        planned_production_time_minutes=record_in.planned_production_time_minutes,
        actual_run_time_minutes=record_in.actual_run_time_minutes,
        total_parts_produced=record_in.total_parts_produced,
        ideal_cycle_time_seconds=record_in.ideal_cycle_time_seconds,
        actual_operating_time_minutes=record_in.actual_operating_time_minutes,
        good_parts=record_in.good_parts,
        total_parts=record_in.total_parts,
    )

    record = OEERecord(
        **record_in.model_dump(),
        **oee_calcs,
        created_by=current_user.id,
    )
    record.company_id = company_id
    db.add(record)
    db.commit()
    db.refresh(record)

    # Reload with relationship
    record = db.query(OEERecord).options(joinedload(OEERecord.work_center)).filter(OEERecord.id == record.id).first()

    return _record_to_response(record)


@router.put("/records/{record_id}", response_model=OEERecordResponse)
def update_oee_record(
    record_id: int,
    record_in: OEERecordUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(OEE_WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
):
    """Update an OEE record and recalculate OEE metrics."""
    record = (
        db.query(OEERecord)
        .options(joinedload(OEERecord.work_center))
        .filter(OEERecord.id == record_id, OEERecord.company_id == company_id)
        .first()
    )

    if not record:
        raise HTTPException(status_code=404, detail="OEE record not found")

    update_data = record_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(record, field, value)

    # Recalculate OEE
    oee_calcs = calculate_oee(
        planned_production_time_minutes=record.planned_production_time_minutes,
        actual_run_time_minutes=record.actual_run_time_minutes,
        total_parts_produced=record.total_parts_produced,
        ideal_cycle_time_seconds=record.ideal_cycle_time_seconds,
        actual_operating_time_minutes=record.actual_operating_time_minutes,
        good_parts=record.good_parts,
        total_parts=record.total_parts,
    )
    for field, value in oee_calcs.items():
        setattr(record, field, value)

    db.commit()
    db.refresh(record)
    return _record_to_response(record)


@router.delete("/records/{record_id}")
def delete_oee_record(
    record_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(OEE_WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
):
    """Delete an OEE record."""
    record = db.query(OEERecord).filter(OEERecord.id == record_id, OEERecord.company_id == company_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="OEE record not found")

    db.delete(record)
    db.commit()
    return {"message": "OEE record deleted"}


# ============== Auto-Calculate Endpoint ==============


@router.post("/calculate/{work_center_id}")
def auto_calculate_oee(
    work_center_id: int,
    record_date: date = None,
    shift: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(OEE_WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
):
    """Auto-calculate OEE for a work center on a given date from existing data.

    Computes a real OEERecord from the day's clocked TimeEntries, the routing standard
    cycle time, and reported DowntimeEvents — on the STAFFED-time convention (Batch 8 /
    rank 11). Previously this endpoint referenced ``TimeEntry.start_time``/``end_time``
    (which DO NOT EXIST) and 500'd on every call (OEE-1); it also hardcoded a 60 s ideal
    cycle and assumed all parts good (OEE-7).

    Convention (mirrors ``analytics_service``):
      * Availability = productive run (clocked RUN+SETUP minus reported DowntimeEvent
        time) ÷ STAFFED (clocked) minutes at the WC that day (OEE-4) — NOT the plant
        calendar, so idle/un-clocked time is excluded and availability is not pinned ~1.
      * Performance = ideal cycle (routing ``run_time_per_piece``) × pieces ÷ productive
        run time, ideal cycle DERIVED, not assumed (OEE-7).
      * Quality = good ÷ total, where scrap comes from ``TimeEntry.quantity_scrapped``
        on the production-bearing entry types (OEE-7), not assumed all-good.
    """
    if record_date is None:
        record_date = date.today()

    wc = db.query(WorkCenter).filter(WorkCenter.id == work_center_id, WorkCenter.company_id == company_id).first()
    if not wc:
        raise HTTPException(status_code=404, detail="Work center not found")

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

    # Reported machine downtime for this WC/day (OEE-7) — DowntimeEvent was never read
    # by this endpoint before despite the docstring's claim.
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
    existing = (
        db.query(OEERecord)
        .filter(
            OEERecord.company_id == company_id,
            OEERecord.work_center_id == work_center_id,
            OEERecord.record_date == record_date,
            OEERecord.shift == shift,
        )
        .first()
    )

    if existing:
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
        for field, value in oee_calcs.items():
            setattr(existing, field, value)
        db.commit()
        db.refresh(existing)
        record = existing
    else:
        record = OEERecord(
            work_center_id=work_center_id,
            record_date=record_date,
            shift=shift,
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
            created_by=current_user.id,
        )
        record.company_id = company_id
        db.add(record)
        db.commit()
        db.refresh(record)

    # Reload with relationship
    record = (
        db.query(OEERecord)
        .options(joinedload(OEERecord.work_center))
        .filter(OEERecord.id == record.id, OEERecord.company_id == company_id)
        .first()
    )

    return _record_to_response(record)


# ============== Dashboard Endpoint ==============


@router.get("/dashboard")
def get_oee_dashboard(
    period: str = "30d",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get OEE dashboard: current OEE per work center, plant-wide OEE, and targets."""
    start_date = _parse_period(period)

    # Get all active work centers
    work_centers = db.query(WorkCenter).filter(WorkCenter.company_id == company_id, WorkCenter.is_active == True).all()

    # Get latest OEE record per work center within period
    work_center_oee = []
    all_oee_values = []

    for wc in work_centers:
        latest = (
            db.query(OEERecord)
            .filter(
                OEERecord.company_id == company_id,
                OEERecord.work_center_id == wc.id,
                OEERecord.record_date >= start_date,
            )
            .order_by(OEERecord.record_date.desc())
            .first()
        )

        # Get target for this work center
        target = (
            db.query(OEETarget).filter(OEETarget.company_id == company_id, OEETarget.work_center_id == wc.id).first()
        )

        oee_data = {
            "work_center_id": wc.id,
            "work_center_name": wc.name,
            "work_center_code": wc.code,
            "current_oee_pct": latest.oee_pct if latest else None,
            "availability_pct": latest.availability_pct if latest else None,
            "performance_pct": latest.performance_pct if latest else None,
            "quality_pct": latest.quality_pct if latest else None,
            "record_date": latest.record_date.isoformat() if latest else None,
            "target_oee_pct": target.target_oee_pct if target else 85.0,
            "target_availability_pct": target.target_availability_pct if target else 90.0,
            "target_performance_pct": target.target_performance_pct if target else 95.0,
            "target_quality_pct": target.target_quality_pct if target else 99.0,
        }
        work_center_oee.append(oee_data)
        if latest:
            all_oee_values.append(latest.oee_pct)

    # Calculate plant-wide OEE (average of all work centers)
    plant_oee = round(sum(all_oee_values) / len(all_oee_values), 2) if all_oee_values else 0.0

    # Get average OEE per work center over the period for comparison chart
    comparison = []
    for wc in work_centers:
        avg = (
            db.query(func.avg(OEERecord.oee_pct))
            .filter(
                OEERecord.company_id == company_id,
                OEERecord.work_center_id == wc.id,
                OEERecord.record_date >= start_date,
            )
            .scalar()
        )

        target = (
            db.query(OEETarget).filter(OEETarget.company_id == company_id, OEETarget.work_center_id == wc.id).first()
        )

        comparison.append(
            {
                "work_center_id": wc.id,
                "work_center_name": wc.name,
                "avg_oee_pct": round(float(avg), 2) if avg else 0.0,
                "target_oee_pct": target.target_oee_pct if target else 85.0,
            }
        )

    return {
        "plant_oee_pct": plant_oee,
        "work_centers": work_center_oee,
        "comparison": comparison,
        "period": period,
    }


# ============== Trends Endpoint ==============


@router.get("/trends")
def get_oee_trends(
    work_center_id: Optional[int] = None,
    period: str = "30d",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get OEE trend data over time for charts."""
    start_date = _parse_period(period)

    query = (
        db.query(OEERecord)
        .options(joinedload(OEERecord.work_center))
        .filter(OEERecord.company_id == company_id, OEERecord.record_date >= start_date)
    )

    if work_center_id:
        query = query.filter(OEERecord.work_center_id == work_center_id)

    records = query.order_by(OEERecord.record_date.asc()).all()

    # Get target
    target = None
    if work_center_id:
        target = (
            db.query(OEETarget)
            .filter(OEETarget.company_id == company_id, OEETarget.work_center_id == work_center_id)
            .first()
        )

    time_series = []
    for r in records:
        time_series.append(
            {
                "date": r.record_date.isoformat(),
                "work_center_id": r.work_center_id,
                "work_center_name": r.work_center.name if r.work_center else None,
                "oee_pct": r.oee_pct,
                "availability_pct": r.availability_pct,
                "performance_pct": r.performance_pct,
                "quality_pct": r.quality_pct,
                "total_parts": r.total_parts,
                "good_parts": r.good_parts,
                "defect_parts": r.defect_parts,
            }
        )

    return {
        "time_series": time_series,
        "target_oee_pct": target.target_oee_pct if target else 85.0,
        "target_availability_pct": target.target_availability_pct if target else 90.0,
        "target_performance_pct": target.target_performance_pct if target else 95.0,
        "target_quality_pct": target.target_quality_pct if target else 99.0,
        "period": period,
    }


# ============== Six Big Losses Endpoint ==============


@router.get("/six-big-losses/{work_center_id}")
def get_six_big_losses(
    work_center_id: int,
    period: str = "30d",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get breakdown of the six big losses for a work center."""
    start_date = _parse_period(period)

    wc = db.query(WorkCenter).filter(WorkCenter.id == work_center_id, WorkCenter.company_id == company_id).first()
    if not wc:
        raise HTTPException(status_code=404, detail="Work center not found")

    records = (
        db.query(OEERecord)
        .filter(
            OEERecord.company_id == company_id,
            OEERecord.work_center_id == work_center_id,
            OEERecord.record_date >= start_date,
        )
        .all()
    )

    # Aggregate six big losses
    unplanned_stops = sum(r.unplanned_stop_minutes or 0 for r in records)
    planned_stops = sum(r.planned_stop_minutes or 0 for r in records)
    small_stops = sum(r.small_stop_minutes or 0 for r in records)
    slow_cycles = sum(r.slow_cycle_minutes or 0 for r in records)
    production_rejects = sum(r.production_reject_count or 0 for r in records)
    startup_rejects = sum(r.startup_reject_count or 0 for r in records)

    total_loss = unplanned_stops + planned_stops + small_stops + slow_cycles

    losses = [
        {
            "name": "Unplanned Stops",
            "category": "availability",
            "value": round(unplanned_stops, 1),
            "unit": "minutes",
            "percentage": round((unplanned_stops / total_loss * 100) if total_loss > 0 else 0, 1),
        },
        {
            "name": "Planned Stops",
            "category": "availability",
            "value": round(planned_stops, 1),
            "unit": "minutes",
            "percentage": round((planned_stops / total_loss * 100) if total_loss > 0 else 0, 1),
        },
        {
            "name": "Small Stops",
            "category": "performance",
            "value": round(small_stops, 1),
            "unit": "minutes",
            "percentage": round((small_stops / total_loss * 100) if total_loss > 0 else 0, 1),
        },
        {
            "name": "Slow Cycles",
            "category": "performance",
            "value": round(slow_cycles, 1),
            "unit": "minutes",
            "percentage": round((slow_cycles / total_loss * 100) if total_loss > 0 else 0, 1),
        },
        {
            "name": "Production Rejects",
            "category": "quality",
            "value": production_rejects,
            "unit": "parts",
            "percentage": 0,
        },
        {
            "name": "Startup Rejects",
            "category": "quality",
            "value": startup_rejects,
            "unit": "parts",
            "percentage": 0,
        },
    ]

    return {
        "work_center_id": work_center_id,
        "work_center_name": wc.name,
        "losses": losses,
        "period": period,
        "total_downtime_minutes": round(total_loss, 1),
    }


# ============== Target Endpoints ==============


@router.get("/targets", response_model=List[OEETargetResponse])
def list_oee_targets(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """List all OEE targets."""
    targets = (
        db.query(OEETarget).filter(OEETarget.company_id == company_id).options(joinedload(OEETarget.work_center)).all()
    )

    result = []
    for t in targets:
        result.append(
            {
                "id": t.id,
                "work_center_id": t.work_center_id,
                "work_center_name": t.work_center.name if t.work_center else None,
                "target_oee_pct": t.target_oee_pct,
                "target_availability_pct": t.target_availability_pct,
                "target_performance_pct": t.target_performance_pct,
                "target_quality_pct": t.target_quality_pct,
                "created_at": t.created_at,
                "updated_at": t.updated_at,
            }
        )
    return result


@router.post("/targets", response_model=OEETargetResponse)
def create_oee_target(
    target_in: OEETargetCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(OEE_WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
):
    """Create or update an OEE target for a work center."""
    wc = (
        db.query(WorkCenter)
        .filter(WorkCenter.id == target_in.work_center_id, WorkCenter.company_id == company_id)
        .first()
    )
    if not wc:
        raise HTTPException(status_code=404, detail="Work center not found")

    # Check if target already exists for this work center
    existing = (
        db.query(OEETarget)
        .filter(OEETarget.company_id == company_id, OEETarget.work_center_id == target_in.work_center_id)
        .first()
    )

    if existing:
        # Update existing
        update_data = target_in.model_dump(exclude={"work_center_id"})
        for field, value in update_data.items():
            setattr(existing, field, value)
        db.commit()
        db.refresh(existing)
        target = existing
    else:
        target = OEETarget(**target_in.model_dump())
        target.company_id = company_id
        db.add(target)
        db.commit()
        db.refresh(target)

    # Reload with relationship
    target = db.query(OEETarget).options(joinedload(OEETarget.work_center)).filter(OEETarget.id == target.id).first()

    return {
        "id": target.id,
        "work_center_id": target.work_center_id,
        "work_center_name": target.work_center.name if target.work_center else None,
        "target_oee_pct": target.target_oee_pct,
        "target_availability_pct": target.target_availability_pct,
        "target_performance_pct": target.target_performance_pct,
        "target_quality_pct": target.target_quality_pct,
        "created_at": target.created_at,
        "updated_at": target.updated_at,
    }


@router.put("/targets/{target_id}", response_model=OEETargetResponse)
def update_oee_target(
    target_id: int,
    target_in: OEETargetUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(OEE_WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
):
    """Update an OEE target."""
    target = db.query(OEETarget).filter(OEETarget.id == target_id, OEETarget.company_id == company_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="OEE target not found")

    update_data = target_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(target, field, value)

    db.commit()
    db.refresh(target)

    # Reload with relationship
    target = (
        db.query(OEETarget)
        .options(joinedload(OEETarget.work_center))
        .filter(OEETarget.id == target.id, OEETarget.company_id == company_id)
        .first()
    )

    return {
        "id": target.id,
        "work_center_id": target.work_center_id,
        "work_center_name": target.work_center.name if target.work_center else None,
        "target_oee_pct": target.target_oee_pct,
        "target_availability_pct": target.target_availability_pct,
        "target_performance_pct": target.target_performance_pct,
        "target_quality_pct": target.target_quality_pct,
        "created_at": target.created_at,
        "updated_at": target.updated_at,
    }


@router.delete("/targets/{target_id}")
def delete_oee_target(
    target_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(OEE_WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
):
    """Delete an OEE target."""
    target = db.query(OEETarget).filter(OEETarget.id == target_id, OEETarget.company_id == company_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="OEE target not found")

    db.delete(target)
    db.commit()
    return {"message": "OEE target deleted"}
