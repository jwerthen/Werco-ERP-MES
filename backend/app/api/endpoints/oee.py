from datetime import date, datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.api.deps import get_audit_service, get_current_company_id, get_current_user, require_role
from app.db.database import get_db
from app.models.oee import CalculationSource, OEERecord, OEETarget
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.services.audit_service import AuditService

# Lean Phase 1: the ~220-line auto-calculation moved verbatim to
# ``services/oee_service.py`` so the nightly cron shares the exact math; these
# re-exports keep the long-standing import surface of this module intact
# (tests and other modules import calculate_oee / the entry-type constants here).
from app.services.oee_service import (  # noqa: F401  (re-exported)
    PRODUCTION_BEARING_ENTRY_TYPES,
    PRODUCTIVE_RUN_ENTRY_TYPES,
    OEERecordConflictError,
    calculate_oee,
    compute_oee_for_work_center,
)

# Clear 409 detail shared by every writer that can trip the
# uq_oee_company_wc_date_shift unique index (migration 063).
_OEE_DUPLICATE_DETAIL = (
    "An OEE record already exists for this work center, date, and shift. "
    "Update the existing record instead (a blank shift and no shift are the same record)."
)

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
    # Lean Phase 1: 'manual' (hand-entered / on-demand trigger) vs 'auto' (nightly
    # cron). Plain str on the read path so an unknown future token reads fine.
    calculation_source: str = CalculationSource.MANUAL.value
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
# (calculate_oee lives in app/services/oee_service.py and is re-exported above.)


def _record_to_response(record: OEERecord) -> dict:
    """Convert OEERecord to response dict with work_center_name."""
    data = {
        "id": record.id,
        "work_center_id": record.work_center_id,
        "work_center_name": record.work_center.name if record.work_center else None,
        "record_date": record.record_date,
        "shift": record.shift,
        "calculation_source": record.calculation_source or CalculationSource.MANUAL.value,
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
    audit: AuditService = Depends(get_audit_service),
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
        calculation_source=CalculationSource.MANUAL.value,
        created_by=current_user.id,
    )
    record.company_id = company_id
    db.add(record)

    # Audit (tamper-evident). Flush so the PK is populated, log BEFORE the terminal commit
    # so the audit row commits atomically with the OEE record (log() only flushes).
    # Lean Phase 1: the uq_oee_company_wc_date_shift unique index (migration 063) makes a
    # second record for the same (company, WC, date, shift) an IntegrityError -> clean 409.
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=_OEE_DUPLICATE_DETAIL) from exc
    audit.log_create(
        resource_type="oee_record",
        resource_id=record.id,
        resource_identifier=str(record.id),
        new_values=record,
        description=f"Created OEE record {record.id} for work center {wc.name}",
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=_OEE_DUPLICATE_DETAIL) from exc
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
    audit: AuditService = Depends(get_audit_service),
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

    # Snapshot pre-mutation values for the audit diff (the live model is mutated in place).
    old_values = {c.key: getattr(record, c.key) for c in record.__table__.columns}

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

    # Audit (tamper-evident) BEFORE the terminal commit so it commits atomically.
    # Lean Phase 1: a shift change can collide with an existing (WC, date, shift)
    # record under uq_oee_company_wc_date_shift -> clean 409, not a 500.
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=_OEE_DUPLICATE_DETAIL) from exc
    audit.log_update(
        resource_type="oee_record",
        resource_id=record.id,
        resource_identifier=str(record.id),
        old_values=old_values,
        new_values=record,
        description=f"Updated OEE record {record.id}",
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=_OEE_DUPLICATE_DETAIL) from exc
    db.refresh(record)
    return _record_to_response(record)


@router.delete("/records/{record_id}")
def delete_oee_record(
    record_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(OEE_WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Delete an OEE record."""
    record = db.query(OEERecord).filter(OEERecord.id == record_id, OEERecord.company_id == company_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="OEE record not found")

    # Snapshot for the audit row before the row is removed.
    old_values = {c.key: getattr(record, c.key) for c in record.__table__.columns}
    deleted_id = record.id

    db.delete(record)
    # Audit (tamper-evident) BEFORE the terminal commit so it commits atomically.
    audit.log_delete(
        resource_type="oee_record",
        resource_id=deleted_id,
        resource_identifier=str(deleted_id),
        old_values=old_values,
        description=f"Deleted OEE record {deleted_id}",
    )
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
    audit: AuditService = Depends(get_audit_service),
):
    """Auto-calculate OEE for a work center on a given date from existing data.

    Thin delegate (Lean Phase 1): the calculation lives in
    ``services/oee_service.compute_oee_for_work_center`` -- staffed-time
    availability convention, derived ideal cycle, real scrap (OEE-1/4/5/7); the
    nightly cron runs the same code. A manual trigger stamps
    ``calculation_source='manual'`` and overwrites whatever record exists for the
    (WC, date, shift) key; a lost create race surfaces as 409.
    """
    if record_date is None:
        record_date = date.today()

    wc = db.query(WorkCenter).filter(WorkCenter.id == work_center_id, WorkCenter.company_id == company_id).first()
    if not wc:
        raise HTTPException(status_code=404, detail="Work center not found")

    try:
        record = compute_oee_for_work_center(
            db,
            company_id,
            wc,
            record_date,
            shift,
            calculation_source=CalculationSource.MANUAL,
            created_by_user_id=current_user.id,
            audit=audit,
        )
    except OEERecordConflictError as exc:
        raise HTTPException(status_code=409, detail=_OEE_DUPLICATE_DETAIL) from exc

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
    audit: AuditService = Depends(get_audit_service),
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
        old_values = {c.key: getattr(existing, c.key) for c in existing.__table__.columns}
        update_data = target_in.model_dump(exclude={"work_center_id"})
        for field, value in update_data.items():
            setattr(existing, field, value)
        # Audit (tamper-evident) BEFORE the terminal commit so it commits atomically.
        db.flush()
        audit.log_update(
            resource_type="oee_target",
            resource_id=existing.id,
            resource_identifier=str(existing.id),
            old_values=old_values,
            new_values=existing,
            description=f"Updated OEE target for work center {wc.name}",
        )
        db.commit()
        db.refresh(existing)
        target = existing
    else:
        target = OEETarget(**target_in.model_dump())
        target.company_id = company_id
        db.add(target)
        # Audit (tamper-evident) BEFORE the terminal commit; flush so the PK is populated.
        db.flush()
        audit.log_create(
            resource_type="oee_target",
            resource_id=target.id,
            resource_identifier=str(target.id),
            new_values=target,
            description=f"Created OEE target for work center {wc.name}",
        )
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
    audit: AuditService = Depends(get_audit_service),
):
    """Update an OEE target."""
    target = db.query(OEETarget).filter(OEETarget.id == target_id, OEETarget.company_id == company_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="OEE target not found")

    old_values = {c.key: getattr(target, c.key) for c in target.__table__.columns}
    update_data = target_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(target, field, value)

    # Audit (tamper-evident) BEFORE the terminal commit so it commits atomically.
    db.flush()
    audit.log_update(
        resource_type="oee_target",
        resource_id=target.id,
        resource_identifier=str(target.id),
        old_values=old_values,
        new_values=target,
        description=f"Updated OEE target {target.id}",
    )
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
    audit: AuditService = Depends(get_audit_service),
):
    """Delete an OEE target."""
    target = db.query(OEETarget).filter(OEETarget.id == target_id, OEETarget.company_id == company_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="OEE target not found")

    # Snapshot for the audit row before the row is removed.
    old_values = {c.key: getattr(target, c.key) for c in target.__table__.columns}
    deleted_id = target.id

    db.delete(target)
    # Audit (tamper-evident) BEFORE the terminal commit so it commits atomically.
    audit.log_delete(
        resource_type="oee_target",
        resource_id=deleted_id,
        resource_identifier=str(deleted_id),
        old_values=old_values,
        description=f"Deleted OEE target {deleted_id}",
    )
    db.commit()
    return {"message": "OEE target deleted"}
