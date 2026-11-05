from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_audit_service, get_current_company_id, get_current_user, require_role
from app.db.database import get_db
from app.db.tenant_filter import tenant_query
from app.models.calibration import CalibrationRecord, CalibrationStatus, Equipment
from app.models.user import User, UserRole
from app.services.audit_service import AuditService, AuditWriteError

router = APIRouter()

# Who may change the calibration system: the RBAC "Calibration" row
# (docs/RBAC_PERMISSIONS.md -> Quality) and the frontend's ``quality:calibration``
# permission are both Admin / Manager / Quality. Until this change every write here
# was a bare ``get_current_user``, so a Viewer could flip a gauge to ACTIVE.
# ``require_role`` always admits PLATFORM_ADMIN and superusers. Reads stay open to
# every role in the tenant.
CALIBRATION_WRITE_ROLES = [UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY]

# The result vocabulary the Calibration page offers. ``adjusted`` means the gauge was
# brought back into tolerance and certified as-left, so it puts the gauge in service
# exactly like ``pass``; ``fail`` takes it out of service. Anything else is a 422 --
# an unrecognised word must never be read as either outcome.
CALIBRATION_RESULTS = ("pass", "adjusted", "fail")
PASSING_RESULTS = frozenset({"pass", "adjusted"})

# The only statuses ``PUT /equipment/{id}`` may set. They take a gauge OUT of
# service. ACTIVE is reachable only through a passing calibration record, and
# DUE / OVERDUE are derived from dates, so hand-setting any of the three is refused.
MANUAL_EQUIPMENT_STATUSES = frozenset({CalibrationStatus.OUT_OF_SERVICE, CalibrationStatus.RETIRED})

# Fields ``PUT /equipment/{id}`` may write, applied explicitly (never a blind setattr
# over the request body).
_EQUIPMENT_EDITABLE_FIELDS = (
    "name",
    "description",
    "equipment_type",
    "location",
    "assigned_to",
    "calibration_interval_days",
    "calibration_provider",
    "is_active",
    "notes",
)
_EQUIPMENT_NON_NULLABLE_UPDATE_FIELDS = ("name", "calibration_interval_days", "is_active", "status")

_AUDIT_UNAVAILABLE = "Unable to save audit record. No changes were committed."


def _normalize_lower(value: Any) -> Any:
    return value.strip().lower() if isinstance(value, str) else value


class EquipmentCreate(BaseModel):
    equipment_id: str
    name: str
    description: Optional[str] = None
    equipment_type: Optional[str] = None
    manufacturer: Optional[str] = None
    model: Optional[str] = None
    serial_number: Optional[str] = None
    location: Optional[str] = None
    assigned_to: Optional[str] = None
    calibration_interval_days: int = Field(365, ge=1, le=3650)
    calibration_provider: Optional[str] = None
    range_min: Optional[str] = None
    range_max: Optional[str] = None
    accuracy: Optional[str] = None
    resolution: Optional[str] = None
    notes: Optional[str] = None


class EquipmentUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    equipment_type: Optional[str] = None
    location: Optional[str] = None
    assigned_to: Optional[str] = None
    calibration_interval_days: Optional[int] = Field(None, ge=1, le=3650)
    calibration_provider: Optional[str] = None
    # Validated against the enum (422 on an unknown word); which of its values the
    # handler accepts is a separate, explicit rule -- see MANUAL_EQUIPMENT_STATUSES.
    status: Optional[CalibrationStatus] = None
    is_active: Optional[bool] = None
    notes: Optional[str] = None

    @field_validator("status", mode="before")
    @classmethod
    def _normalize_status(cls, value: Any) -> Any:
        return _normalize_lower(value)

    @model_validator(mode="after")
    def _refuse_explicit_nulls(self) -> "EquipmentUpdate":
        nulled = [
            field
            for field in _EQUIPMENT_NON_NULLABLE_UPDATE_FIELDS
            if field in self.model_fields_set and getattr(self, field) is None
        ]
        if nulled:
            raise ValueError(f"{', '.join(nulled)} cannot be null")
        return self


class CalibrationRecordCreate(BaseModel):
    calibration_date: date
    performed_by: Optional[str] = None
    calibration_provider: Optional[str] = None
    certificate_number: Optional[str] = None
    result: str = "pass"
    as_found: Optional[str] = None
    as_left: Optional[str] = None
    standards_used: Optional[str] = None
    cost: float = 0.0
    notes: Optional[str] = None

    @field_validator("result", mode="before")
    @classmethod
    def _validate_result(cls, value: Any) -> Any:
        normalized = _normalize_lower(value)
        if normalized not in CALIBRATION_RESULTS:
            raise ValueError(f"result must be one of: {', '.join(CALIBRATION_RESULTS)}")
        return normalized


class EquipmentResponse(BaseModel):
    id: int
    equipment_id: str
    name: str
    description: Optional[str] = None
    equipment_type: Optional[str] = None
    manufacturer: Optional[str] = None
    model: Optional[str] = None
    serial_number: Optional[str] = None
    location: Optional[str] = None
    assigned_to: Optional[str] = None
    calibration_interval_days: int
    last_calibration_date: Optional[date] = None
    next_calibration_date: Optional[date] = None
    calibration_provider: Optional[str] = None
    status: str
    is_active: bool
    days_until_due: Optional[int] = None

    class Config:
        from_attributes = True


class CalibrationRecordResponse(BaseModel):
    id: int
    equipment_id: int
    calibration_date: date
    due_date: date
    performed_by: Optional[str] = None
    calibration_provider: Optional[str] = None
    certificate_number: Optional[str] = None
    result: Optional[str] = None
    cost: float
    created_at: datetime

    class Config:
        from_attributes = True


class CalibrationRecordedResponse(CalibrationRecordResponse):
    """``POST /equipment/{id}/calibrate`` response: the record plus what it did to the gauge.

    ``applied_to_equipment`` is False when the record was filed as history only -- the
    gauge is RETIRED, or the record is dated before a calibration already on file -- so
    a caller can tell "filed" from "filed and the gauge's state moved".
    """

    equipment_status: str
    applied_to_equipment: bool


def update_equipment_status(equipment: Equipment):
    """Update equipment status based on calibration dates"""
    if not equipment.next_calibration_date:
        return

    today = date.today()
    days_until = (equipment.next_calibration_date - today).days

    if days_until < 0:
        equipment.status = CalibrationStatus.OVERDUE
    elif days_until <= 30:
        equipment.status = CalibrationStatus.DUE
    else:
        equipment.status = CalibrationStatus.ACTIVE


def _status_value(status: Any) -> Optional[str]:
    return status.value if hasattr(status, "value") else status


def _equipment_or_404(db: Session, equipment_id: int, company_id: int) -> Equipment:
    """Resolve a gauge inside the caller's tenant, or a flat 404.

    A foreign gauge must be indistinguishable from a nonexistent one, so the status
    code cannot be used as an existence oracle over another tenant's equipment.
    """
    equipment = tenant_query(db, Equipment, company_id).filter(Equipment.id == equipment_id).first()
    if not equipment:
        raise HTTPException(status_code=404, detail="Equipment not found")
    return equipment


def _equipment_response(equipment: Equipment) -> EquipmentResponse:
    days_until = None
    if equipment.next_calibration_date:
        days_until = (equipment.next_calibration_date - date.today()).days
    return EquipmentResponse(
        id=equipment.id,
        equipment_id=equipment.equipment_id,
        name=equipment.name,
        description=equipment.description,
        equipment_type=equipment.equipment_type,
        manufacturer=equipment.manufacturer,
        model=equipment.model,
        serial_number=equipment.serial_number,
        location=equipment.location,
        assigned_to=equipment.assigned_to,
        calibration_interval_days=equipment.calibration_interval_days,
        last_calibration_date=equipment.last_calibration_date,
        next_calibration_date=equipment.next_calibration_date,
        calibration_provider=equipment.calibration_provider,
        status=_status_value(equipment.status),
        is_active=equipment.is_active,
        days_until_due=days_until,
    )


@router.get("/equipment", response_model=List[EquipmentResponse])
def list_equipment(
    status: Optional[str] = None,
    include_inactive: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """List all equipment"""
    query = tenant_query(db, Equipment, company_id)

    if not include_inactive:
        query = query.filter(Equipment.is_active == True)

    equipment_list = query.order_by(Equipment.next_calibration_date).all()

    result = []
    today = date.today()
    for eq in equipment_list:
        effective_status = eq.status.value if hasattr(eq.status, "value") else eq.status
        # Out-of-service and retired are deliberate states set by a person (a failed
        # calibration, or the PUT verb); only the date-driven states are recomputed.
        if eq.next_calibration_date and effective_status not in ("out_of_service", "retired"):
            days = (eq.next_calibration_date - today).days
            effective_status = "overdue" if days < 0 else "due" if days <= 30 else "active"
        if status == "due_soon":
            if not eq.next_calibration_date or (eq.next_calibration_date - today).days > 30:
                continue
        elif status and effective_status != status:
            continue
        days_until = None
        if eq.next_calibration_date:
            days_until = (eq.next_calibration_date - today).days

        result.append(
            EquipmentResponse(
                id=eq.id,
                equipment_id=eq.equipment_id,
                name=eq.name,
                description=eq.description,
                equipment_type=eq.equipment_type,
                manufacturer=eq.manufacturer,
                model=eq.model,
                serial_number=eq.serial_number,
                location=eq.location,
                assigned_to=eq.assigned_to,
                calibration_interval_days=eq.calibration_interval_days,
                last_calibration_date=eq.last_calibration_date,
                next_calibration_date=eq.next_calibration_date,
                calibration_provider=eq.calibration_provider,
                status=effective_status,
                is_active=eq.is_active,
                days_until_due=days_until,
            )
        )

    return result


@router.get("/equipment/due-soon")
def get_equipment_due_soon(
    days: int = Query(30, ge=1, le=365),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get equipment due for calibration within specified days"""
    cutoff = date.today() + timedelta(days=days)

    equipment = (
        tenant_query(db, Equipment, company_id)
        .filter(Equipment.is_active == True, Equipment.next_calibration_date <= cutoff)
        .order_by(Equipment.next_calibration_date)
        .all()
    )

    return [
        {
            "id": eq.id,
            "equipment_id": eq.equipment_id,
            "name": eq.name,
            "next_calibration_date": eq.next_calibration_date.isoformat() if eq.next_calibration_date else None,
            "days_until_due": (eq.next_calibration_date - date.today()).days if eq.next_calibration_date else None,
            "status": eq.status.value if hasattr(eq.status, 'value') else eq.status,
        }
        for eq in equipment
    ]


@router.post("/equipment", response_model=EquipmentResponse)
def create_equipment(
    equipment_in: EquipmentCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(CALIBRATION_WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Create new equipment. **Role: ADMIN / MANAGER / QUALITY.**

    A new gauge starts with no due date, so the kiosk gauge gate refuses it until its
    first passing calibration is recorded.
    """
    if tenant_query(db, Equipment, company_id).filter(Equipment.equipment_id == equipment_in.equipment_id).first():
        raise HTTPException(status_code=400, detail="Equipment ID already exists")

    equipment = Equipment(**equipment_in.model_dump())
    equipment.company_id = company_id
    db.add(equipment)
    try:
        db.flush()
    except IntegrityError:
        # ``equipment.equipment_id`` still carries a GLOBAL unique constraint (no
        # per-company compound yet), so a code another tenant already uses fails here
        # rather than in the tenant-scoped probe above. Same answer as the probe.
        db.rollback()
        raise HTTPException(status_code=400, detail="Equipment ID already exists")
    audit.log_create("equipment", equipment.id, equipment.equipment_id, new_values=equipment)
    db.commit()
    db.refresh(equipment)

    return _equipment_response(equipment)


@router.get("/equipment/{equipment_id}")
def get_equipment(
    equipment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get equipment with calibration history"""
    equipment = _equipment_or_404(db, equipment_id, company_id)

    records = (
        tenant_query(db, CalibrationRecord, company_id)
        .filter(CalibrationRecord.equipment_id == equipment.id)
        .order_by(CalibrationRecord.calibration_date.desc())
        .all()
    )

    return {"equipment": equipment, "calibration_history": records}


@router.put("/equipment/{equipment_id}", response_model=EquipmentResponse)
def update_equipment(
    equipment_id: int,
    equipment_in: EquipmentUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(CALIBRATION_WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Update equipment. **Role: ADMIN / MANAGER / QUALITY.**

    ``status`` may only take a gauge OUT of service (``out_of_service`` / ``retired``).
    Putting one INTO service is **409**: ACTIVE is reached only through a passing
    ``POST /equipment/{id}/calibrate``, so a gauge can never be re-certified without a
    calibration record. Re-sending the gauge's current status is a no-op, not a refusal.
    """
    equipment = _equipment_or_404(db, equipment_id, company_id)

    update_data = equipment_in.model_dump(exclude_unset=True)
    old_status = equipment.status
    new_status = update_data.pop("status", None)
    status_changes = new_status is not None and new_status != old_status
    if status_changes and new_status not in MANUAL_EQUIPMENT_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Equipment status cannot be set to '{_status_value(new_status)}' here. A gauge returns to service "
                f"only through a passing calibration record: POST /calibration/equipment/{equipment.id}/calibrate"
            ),
        )

    old_values = {field: getattr(equipment, field) for field in _EQUIPMENT_EDITABLE_FIELDS}
    for field in _EQUIPMENT_EDITABLE_FIELDS:
        if field in update_data:
            setattr(equipment, field, update_data[field])
    if status_changes:
        equipment.status = new_status

    db.flush()
    audit.log_update(
        "equipment",
        equipment.id,
        equipment.equipment_id,
        old_values=old_values,
        new_values={field: getattr(equipment, field) for field in _EQUIPMENT_EDITABLE_FIELDS},
    )
    if status_changes:
        audit.log_status_change(
            "equipment",
            equipment.id,
            equipment.equipment_id,
            old_status=_status_value(old_status),
            new_status=_status_value(new_status),
        )
    db.commit()
    db.refresh(equipment)

    return _equipment_response(equipment)


@router.post("/equipment/{equipment_id}/calibrate", response_model=CalibrationRecordedResponse)
def record_calibration(
    equipment_id: int,
    record_in: CalibrationRecordCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(CALIBRATION_WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Record a calibration for equipment. **Role: ADMIN / MANAGER / QUALITY.**

    The result decides what happens to the gauge:

    * ``pass`` / ``adjusted`` -- status ACTIVE, ``last_calibration_date`` = the
      calibration date, ``next_calibration_date`` = that date + the interval.
    * ``fail`` -- status OUT_OF_SERVICE; neither date moves, so a failed calibration
      never extends the gauge's certification. The record's own ``due_date`` is the
      calibration date: the gauge is due for recalibration now.

    The record is ALWAYS filed, but it moves the gauge only when it is the newest
    calibration on file (a backdated certificate is history: entering an old pass
    must not re-certify a gauge that has failed since) and the gauge is not RETIRED.

    The record and its audit rows commit atomically; if the audit evidence cannot be
    written nothing is saved (**503**). This is the AS9100D 7.1.5.2 quality record.
    """
    equipment = _equipment_or_404(db, equipment_id, company_id)
    result = record_in.result
    passed = result in PASSING_RESULTS

    if passed:
        due_date = record_in.calibration_date + timedelta(days=equipment.calibration_interval_days or 365)
    else:
        due_date = record_in.calibration_date

    newest_on_file = (
        tenant_query(db, CalibrationRecord, company_id)
        .filter(CalibrationRecord.equipment_id == equipment.id)
        .with_entities(func.max(CalibrationRecord.calibration_date))
        .scalar()
    )
    is_newest = newest_on_file is None or record_in.calibration_date >= newest_on_file
    applies = is_newest and equipment.status != CalibrationStatus.RETIRED

    old_status = equipment.status
    record = CalibrationRecord(
        equipment_id=equipment.id,
        calibration_date=record_in.calibration_date,
        due_date=due_date,
        performed_by=record_in.performed_by,
        calibration_provider=record_in.calibration_provider or equipment.calibration_provider,
        certificate_number=record_in.certificate_number,
        result=result,
        as_found=record_in.as_found,
        as_left=record_in.as_left,
        standards_used=record_in.standards_used,
        cost=record_in.cost,
        notes=record_in.notes,
        created_by=current_user.id,
    )
    record.company_id = equipment.company_id

    try:
        db.add(record)
        if applies:
            if passed:
                equipment.last_calibration_date = record_in.calibration_date
                equipment.next_calibration_date = due_date
                equipment.status = CalibrationStatus.ACTIVE
            else:
                equipment.status = CalibrationStatus.OUT_OF_SERVICE
        db.flush()

        new_values: Dict[str, Any] = {
            "equipment_id": equipment.id,
            "equipment_code": equipment.equipment_id,
            "calibration_date": record.calibration_date.isoformat(),
            "due_date": record.due_date.isoformat(),
            "result": result,
            "certificate_number": record.certificate_number,
            "performed_by": record.performed_by,
            "calibration_provider": record.calibration_provider,
            "as_found": record.as_found,
            "as_left": record.as_left,
            "applied_to_equipment": applies,
            "equipment_next_calibration_date": (
                equipment.next_calibration_date.isoformat() if equipment.next_calibration_date else None
            ),
        }
        audit.log_required(
            "CREATE",
            "calibration",
            resource_id=record.id,
            resource_identifier=f"{equipment.equipment_id} {record.calibration_date.isoformat()}",
            description=(
                f"Recorded calibration ({result}) for {equipment.equipment_id}"
                + ("" if applies else " -- filed as history; gauge state unchanged")
            ),
            new_values=new_values,
        )
        if equipment.status != old_status:
            audit.log_required(
                "STATUS_CHANGE",
                "equipment",
                resource_id=equipment.id,
                resource_identifier=equipment.equipment_id,
                description=(
                    f"Changed equipment status: {equipment.equipment_id} from '{_status_value(old_status)}' "
                    f"to '{_status_value(equipment.status)}' by calibration result '{result}'"
                ),
                old_values={"status": _status_value(old_status)},
                new_values={"status": _status_value(equipment.status)},
                extra_data={"calibration_record_id": record.id, "result": result},
            )
        db.commit()
    except AuditWriteError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail=_AUDIT_UNAVAILABLE) from exc
    except Exception:
        db.rollback()
        raise

    db.refresh(record)
    db.refresh(equipment)
    return CalibrationRecordedResponse(
        id=record.id,
        equipment_id=record.equipment_id,
        calibration_date=record.calibration_date,
        due_date=record.due_date,
        performed_by=record.performed_by,
        calibration_provider=record.calibration_provider,
        certificate_number=record.certificate_number,
        result=record.result,
        cost=record.cost or 0.0,
        created_at=record.created_at,
        equipment_status=_status_value(equipment.status),
        applied_to_equipment=applies,
    )


@router.get("/equipment/{equipment_id}/history", response_model=List[CalibrationRecordResponse])
def get_calibration_history(
    equipment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get calibration history for equipment (404 for another tenant's gauge)."""
    equipment = _equipment_or_404(db, equipment_id, company_id)
    records = (
        tenant_query(db, CalibrationRecord, company_id)
        .filter(CalibrationRecord.equipment_id == equipment.id)
        .order_by(CalibrationRecord.calibration_date.desc())
        .all()
    )

    return records
