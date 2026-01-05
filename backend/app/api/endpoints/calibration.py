from typing import List, Optional
from datetime import datetime, date, timedelta
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.db.database import get_db
from app.api.deps import get_current_user
from app.models.user import User
from app.models.calibration import Equipment, CalibrationRecord, CalibrationStatus
from pydantic import BaseModel

router = APIRouter()


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
    calibration_interval_days: int = 365
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
    calibration_interval_days: Optional[int] = None
    calibration_provider: Optional[str] = None
    status: Optional[str] = None
    is_active: Optional[bool] = None
    notes: Optional[str] = None


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


@router.get("/equipment", response_model=List[EquipmentResponse])
def list_equipment(
    status: Optional[str] = None,
    include_inactive: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all equipment"""
    query = db.query(Equipment)
    
    if not include_inactive:
        query = query.filter(Equipment.is_active == True)
    
    if status:
        query = query.filter(Equipment.status == status)
    
    equipment_list = query.order_by(Equipment.next_calibration_date).all()
    
    result = []
    today = date.today()
    for eq in equipment_list:
        update_equipment_status(eq)
        days_until = None
        if eq.next_calibration_date:
            days_until = (eq.next_calibration_date - today).days
        
        result.append(EquipmentResponse(
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
            status=eq.status.value if hasattr(eq.status, 'value') else eq.status,
            is_active=eq.is_active,
            days_until_due=days_until
        ))
    
    db.commit()  # Save status updates
    return result


@router.get("/equipment/due-soon")
def get_equipment_due_soon(
    days: int = 30,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get equipment due for calibration within specified days"""
    cutoff = date.today() + timedelta(days=days)
    
    equipment = db.query(Equipment).filter(
        Equipment.is_active == True,
        Equipment.next_calibration_date <= cutoff
    ).order_by(Equipment.next_calibration_date).all()
    
    return [{
        "id": eq.id,
        "equipment_id": eq.equipment_id,
        "name": eq.name,
        "next_calibration_date": eq.next_calibration_date.isoformat() if eq.next_calibration_date else None,
        "days_until_due": (eq.next_calibration_date - date.today()).days if eq.next_calibration_date else None,
        "status": eq.status.value if hasattr(eq.status, 'value') else eq.status
    } for eq in equipment]


@router.post("/equipment", response_model=EquipmentResponse)
def create_equipment(
    equipment_in: EquipmentCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create new equipment"""
    if db.query(Equipment).filter(Equipment.equipment_id == equipment_in.equipment_id).first():
        raise HTTPException(status_code=400, detail="Equipment ID already exists")
    
    equipment = Equipment(**equipment_in.model_dump())
    db.add(equipment)
    db.commit()
    db.refresh(equipment)
    
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
        status=equipment.status.value,
        is_active=equipment.is_active,
        days_until_due=None
    )


@router.get("/equipment/{equipment_id}")
def get_equipment(
    equipment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get equipment with calibration history"""
    equipment = db.query(Equipment).filter(Equipment.id == equipment_id).first()
    if not equipment:
        raise HTTPException(status_code=404, detail="Equipment not found")
    
    records = db.query(CalibrationRecord).filter(
        CalibrationRecord.equipment_id == equipment_id
    ).order_by(CalibrationRecord.calibration_date.desc()).all()
    
    return {
        "equipment": equipment,
        "calibration_history": records
    }


@router.put("/equipment/{equipment_id}", response_model=EquipmentResponse)
def update_equipment(
    equipment_id: int,
    equipment_in: EquipmentUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update equipment"""
    equipment = db.query(Equipment).filter(Equipment.id == equipment_id).first()
    if not equipment:
        raise HTTPException(status_code=404, detail="Equipment not found")
    
    update_data = equipment_in.model_dump(exclude_unset=True)
    if "status" in update_data:
        update_data["status"] = CalibrationStatus(update_data["status"])
    
    for field, value in update_data.items():
        setattr(equipment, field, value)
    
    db.commit()
    db.refresh(equipment)
    
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
        status=equipment.status.value if hasattr(equipment.status, 'value') else equipment.status,
        is_active=equipment.is_active,
        days_until_due=days_until
    )


@router.post("/equipment/{equipment_id}/calibrate", response_model=CalibrationRecordResponse)
def record_calibration(
    equipment_id: int,
    record_in: CalibrationRecordCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Record a calibration for equipment"""
    equipment = db.query(Equipment).filter(Equipment.id == equipment_id).first()
    if not equipment:
        raise HTTPException(status_code=404, detail="Equipment not found")
    
    # Calculate next due date
    due_date = record_in.calibration_date + timedelta(days=equipment.calibration_interval_days)
    
    record = CalibrationRecord(
        equipment_id=equipment_id,
        calibration_date=record_in.calibration_date,
        due_date=due_date,
        performed_by=record_in.performed_by,
        calibration_provider=record_in.calibration_provider or equipment.calibration_provider,
        certificate_number=record_in.certificate_number,
        result=record_in.result,
        as_found=record_in.as_found,
        as_left=record_in.as_left,
        standards_used=record_in.standards_used,
        cost=record_in.cost,
        notes=record_in.notes,
        created_by=current_user.id
    )
    db.add(record)
    
    # Update equipment
    equipment.last_calibration_date = record_in.calibration_date
    equipment.next_calibration_date = due_date
    equipment.status = CalibrationStatus.ACTIVE
    
    db.commit()
    db.refresh(record)
    
    return record


@router.get("/equipment/{equipment_id}/history", response_model=List[CalibrationRecordResponse])
def get_calibration_history(
    equipment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get calibration history for equipment"""
    records = db.query(CalibrationRecord).filter(
        CalibrationRecord.equipment_id == equipment_id
    ).order_by(CalibrationRecord.calibration_date.desc()).all()
    
    return records
