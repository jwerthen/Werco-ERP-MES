from datetime import date, datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.api.deps import get_current_company_id, get_current_user, require_role
from app.db.database import get_db
from app.db.locks import acquire_generator_lock
from app.models.quality import (
    CARStatus,
    CorrectiveActionRequest,
    FAICharacteristic,
    FAIStatus,
    FirstArticleInspection,
    NCRSource,
    NCRStatus,
    NonConformanceReport,
)
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder
from app.schemas.quality import (
    CARCreate,
    CARResponse,
    CARUpdate,
    FAICharacteristicCreate,
    FAICharacteristicResponse,
    FAICharacteristicUpdate,
    FAICreate,
    FAIResponse,
    FAIUpdate,
    NCRCreate,
    NCRResponse,
    NCRUpdate,
)
from app.services.operational_event_service import OperationalEventService

router = APIRouter()


# ============== NCR Endpoints ==============


def _enum_value(value):
    return value.value if hasattr(value, "value") else value


def _validate_work_order_reference(db: Session, company_id: int, work_order_id: Optional[int]) -> None:
    if work_order_id is None:
        return
    exists = db.query(WorkOrder.id).filter(WorkOrder.id == work_order_id, WorkOrder.company_id == company_id).first()
    if not exists:
        raise HTTPException(status_code=404, detail="Work order not found")


def generate_ncr_number(db: Session, company_id: int = None) -> str:
    acquire_generator_lock(db, "ncr_number", company_id)
    today = datetime.now().strftime("%Y%m%d")
    prefix = f"NCR-{today}-"
    query = db.query(NonConformanceReport).filter(NonConformanceReport.ncr_number.like(f"{prefix}%"))
    if company_id is not None:
        query = query.filter(NonConformanceReport.company_id == company_id)
    last = query.order_by(NonConformanceReport.ncr_number.desc()).first()

    if last:
        num = int(last.ncr_number.split("-")[-1]) + 1
    else:
        num = 1
    return f"{prefix}{num:03d}"


@router.get("/ncr", response_model=List[NCRResponse])
def list_ncrs(
    skip: int = 0,
    limit: int = 100,
    status: Optional[NCRStatus] = None,
    part_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """List all NCRs"""
    query = (
        db.query(NonConformanceReport)
        .filter(NonConformanceReport.company_id == company_id)
        .options(joinedload(NonConformanceReport.part))
    )

    if status:
        query = query.filter(NonConformanceReport.status == status)
    if part_id:
        query = query.filter(NonConformanceReport.part_id == part_id)

    return query.order_by(NonConformanceReport.created_at.desc()).offset(skip).limit(limit).all()


@router.post("/ncr", response_model=NCRResponse)
def create_ncr(
    ncr_in: NCRCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Create a new NCR"""
    _validate_work_order_reference(db, company_id, ncr_in.work_order_id)
    ncr = NonConformanceReport(
        ncr_number=generate_ncr_number(db, company_id),
        **ncr_in.model_dump(),
        detected_by=current_user.id,
        detected_date=ncr_in.detected_date or date.today(),
    )
    ncr.company_id = company_id
    db.add(ncr)
    db.flush()
    OperationalEventService(db).emit(
        company_id=company_id,
        event_type="ncr_created",
        source_module="quality",
        entity_type="ncr",
        entity_id=ncr.id,
        work_order_id=ncr.work_order_id,
        user_id=current_user.id,
        severity="high" if ncr.source == NCRSource.CUSTOMER_RETURN else "medium",
        event_payload={
            "ncr_number": ncr.ncr_number,
            "title": ncr.title,
            "source": _enum_value(ncr.source),
            "status": _enum_value(ncr.status),
            "disposition": _enum_value(ncr.disposition),
            "part_id": ncr.part_id,
            "quantity_affected": float(ncr.quantity_affected or 0),
        },
    )
    db.commit()
    db.refresh(ncr)
    return ncr


@router.get("/ncr/{ncr_id}", response_model=NCRResponse)
def get_ncr(
    ncr_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get NCR details"""
    ncr = (
        db.query(NonConformanceReport)
        .options(joinedload(NonConformanceReport.part))
        .filter(NonConformanceReport.id == ncr_id, NonConformanceReport.company_id == company_id)
        .first()
    )

    if not ncr:
        raise HTTPException(status_code=404, detail="NCR not found")
    return ncr


@router.put("/ncr/{ncr_id}", response_model=NCRResponse)
def update_ncr(
    ncr_id: int,
    ncr_in: NCRUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Update an NCR"""
    ncr = (
        db.query(NonConformanceReport)
        .filter(NonConformanceReport.id == ncr_id, NonConformanceReport.company_id == company_id)
        .first()
    )
    if not ncr:
        raise HTTPException(status_code=404, detail="NCR not found")

    previous_status = ncr.status
    previous_disposition = ncr.disposition
    update_data = ncr_in.model_dump(exclude_unset=True)

    # Handle status transitions
    if "status" in update_data and update_data["status"] == NCRStatus.CLOSED:
        ncr.closed_date = date.today()
        ncr.closed_by = current_user.id

    for field, value in update_data.items():
        setattr(ncr, field, value)

    OperationalEventService(db).emit(
        company_id=company_id,
        event_type="ncr_updated",
        source_module="quality",
        entity_type="ncr",
        entity_id=ncr.id,
        work_order_id=ncr.work_order_id,
        user_id=current_user.id,
        severity="info" if ncr.status == previous_status else "medium",
        event_payload={
            "ncr_number": ncr.ncr_number,
            "changed_fields": [field for field in update_data.keys() if field != "version"],
            "previous_status": _enum_value(previous_status),
            "status": _enum_value(ncr.status),
            "previous_disposition": _enum_value(previous_disposition),
            "disposition": _enum_value(ncr.disposition),
        },
    )
    db.commit()
    db.refresh(ncr)
    return ncr


@router.post("/ncr/{ncr_id}/create-car", response_model=CARResponse)
def create_car_from_ncr(
    ncr_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY])),
    company_id: int = Depends(get_current_company_id),
):
    """Create a CAR from an NCR"""
    ncr = (
        db.query(NonConformanceReport)
        .filter(NonConformanceReport.id == ncr_id, NonConformanceReport.company_id == company_id)
        .first()
    )
    if not ncr:
        raise HTTPException(status_code=404, detail="NCR not found")

    car = CorrectiveActionRequest(
        car_number=generate_car_number(db, company_id),
        title=f"CAR for {ncr.ncr_number}: {ncr.title}",
        problem_description=ncr.description,
        initiated_by=current_user.id,
    )
    car.company_id = company_id
    db.add(car)
    db.flush()

    ncr.car_required = True
    ncr.car_id = car.id

    OperationalEventService(db).emit(
        company_id=company_id,
        event_type="car_created_from_ncr",
        source_module="quality",
        entity_type="car",
        entity_id=car.id,
        work_order_id=ncr.work_order_id,
        user_id=current_user.id,
        severity="medium",
        event_payload={
            "car_number": car.car_number,
            "ncr_id": ncr.id,
            "ncr_number": ncr.ncr_number,
            "title": car.title,
            "priority": car.priority,
        },
    )
    db.commit()
    db.refresh(car)
    return car


# ============== CAR Endpoints ==============


def generate_car_number(db: Session, company_id: int = None) -> str:
    acquire_generator_lock(db, "car_number", company_id)
    today = datetime.now().strftime("%Y%m%d")
    prefix = f"CAR-{today}-"
    query = db.query(CorrectiveActionRequest).filter(CorrectiveActionRequest.car_number.like(f"{prefix}%"))
    if company_id is not None:
        query = query.filter(CorrectiveActionRequest.company_id == company_id)
    last = query.order_by(CorrectiveActionRequest.car_number.desc()).first()

    if last:
        num = int(last.car_number.split("-")[-1]) + 1
    else:
        num = 1
    return f"{prefix}{num:03d}"


@router.get("/car", response_model=List[CARResponse])
def list_cars(
    skip: int = 0,
    limit: int = 100,
    status: Optional[CARStatus] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """List all CARs"""
    query = db.query(CorrectiveActionRequest).filter(CorrectiveActionRequest.company_id == company_id)

    if status:
        query = query.filter(CorrectiveActionRequest.status == status)

    return query.order_by(CorrectiveActionRequest.created_at.desc()).offset(skip).limit(limit).all()


@router.post("/car", response_model=CARResponse)
def create_car(
    car_in: CARCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Create a new CAR"""
    car = CorrectiveActionRequest(
        car_number=generate_car_number(db, company_id), **car_in.model_dump(), initiated_by=current_user.id
    )
    car.company_id = company_id
    db.add(car)
    db.flush()
    OperationalEventService(db).emit(
        company_id=company_id,
        event_type="car_created",
        source_module="quality",
        entity_type="car",
        entity_id=car.id,
        user_id=current_user.id,
        severity="high" if car.priority <= 2 else "medium",
        event_payload={
            "car_number": car.car_number,
            "title": car.title,
            "car_type": _enum_value(car.car_type),
            "status": _enum_value(car.status),
            "priority": car.priority,
            "due_date": car.due_date.isoformat() if car.due_date else None,
        },
    )
    db.commit()
    db.refresh(car)
    return car


@router.get("/car/{car_id}", response_model=CARResponse)
def get_car(
    car_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get CAR details"""
    car = (
        db.query(CorrectiveActionRequest)
        .filter(CorrectiveActionRequest.id == car_id, CorrectiveActionRequest.company_id == company_id)
        .first()
    )
    if not car:
        raise HTTPException(status_code=404, detail="CAR not found")
    return car


@router.put("/car/{car_id}", response_model=CARResponse)
def update_car(
    car_id: int,
    car_in: CARUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Update a CAR"""
    car = (
        db.query(CorrectiveActionRequest)
        .filter(CorrectiveActionRequest.id == car_id, CorrectiveActionRequest.company_id == company_id)
        .first()
    )
    if not car:
        raise HTTPException(status_code=404, detail="CAR not found")

    previous_status = car.status
    update_data = car_in.model_dump(exclude_unset=True)

    if "status" in update_data:
        if update_data["status"] == CARStatus.VERIFICATION:
            car.verified_by = current_user.id
        elif update_data["status"] == CARStatus.CLOSED:
            car.closed_date = date.today()
            car.closed_by = current_user.id

    for field, value in update_data.items():
        setattr(car, field, value)

    OperationalEventService(db).emit(
        company_id=company_id,
        event_type="car_updated",
        source_module="quality",
        entity_type="car",
        entity_id=car.id,
        user_id=current_user.id,
        severity="info" if car.status == previous_status else "medium",
        event_payload={
            "car_number": car.car_number,
            "changed_fields": [field for field in update_data.keys() if field != "version"],
            "previous_status": _enum_value(previous_status),
            "status": _enum_value(car.status),
            "priority": car.priority,
        },
    )
    db.commit()
    db.refresh(car)
    return car


# ============== FAI Endpoints ==============


def generate_fai_number(db: Session, company_id: int = None) -> str:
    acquire_generator_lock(db, "fai_number", company_id)
    today = datetime.now().strftime("%Y%m%d")
    prefix = f"FAI-{today}-"
    query = db.query(FirstArticleInspection).filter(FirstArticleInspection.fai_number.like(f"{prefix}%"))
    if company_id is not None:
        query = query.filter(FirstArticleInspection.company_id == company_id)
    last = query.order_by(FirstArticleInspection.fai_number.desc()).first()

    if last:
        num = int(last.fai_number.split("-")[-1]) + 1
    else:
        num = 1
    return f"{prefix}{num:03d}"


@router.get("/fai", response_model=List[FAIResponse])
def list_fais(
    skip: int = 0,
    limit: int = 100,
    status: Optional[FAIStatus] = None,
    part_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """List all FAIs"""
    query = (
        db.query(FirstArticleInspection)
        .filter(FirstArticleInspection.company_id == company_id)
        .options(joinedload(FirstArticleInspection.part), joinedload(FirstArticleInspection.characteristics))
    )

    if status:
        query = query.filter(FirstArticleInspection.status == status)
    if part_id:
        query = query.filter(FirstArticleInspection.part_id == part_id)

    return query.order_by(FirstArticleInspection.created_at.desc()).offset(skip).limit(limit).all()


@router.post("/fai", response_model=FAIResponse)
def create_fai(
    fai_in: FAICreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Create a new FAI"""
    _validate_work_order_reference(db, company_id, fai_in.work_order_id)
    fai = FirstArticleInspection(fai_number=generate_fai_number(db, company_id), **fai_in.model_dump())
    fai.company_id = company_id
    db.add(fai)
    db.flush()
    OperationalEventService(db).emit(
        company_id=company_id,
        event_type="fai_created",
        source_module="quality",
        entity_type="fai",
        entity_id=fai.id,
        work_order_id=fai.work_order_id,
        user_id=current_user.id,
        severity="medium",
        event_payload={
            "fai_number": fai.fai_number,
            "part_id": fai.part_id,
            "status": _enum_value(fai.status),
            "fai_type": fai.fai_type,
            "reason": fai.reason,
            "due_date": fai.due_date.isoformat() if fai.due_date else None,
        },
    )
    db.commit()
    db.refresh(fai)
    return fai


@router.get("/fai/{fai_id}", response_model=FAIResponse)
def get_fai(
    fai_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get FAI details"""
    fai = (
        db.query(FirstArticleInspection)
        .options(joinedload(FirstArticleInspection.part), joinedload(FirstArticleInspection.characteristics))
        .filter(FirstArticleInspection.id == fai_id, FirstArticleInspection.company_id == company_id)
        .first()
    )

    if not fai:
        raise HTTPException(status_code=404, detail="FAI not found")
    return fai


@router.put("/fai/{fai_id}", response_model=FAIResponse)
def update_fai(
    fai_id: int,
    fai_in: FAIUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Update a FAI"""
    fai = (
        db.query(FirstArticleInspection)
        .filter(FirstArticleInspection.id == fai_id, FirstArticleInspection.company_id == company_id)
        .first()
    )
    if not fai:
        raise HTTPException(status_code=404, detail="FAI not found")

    previous_status = fai.status
    update_data = fai_in.model_dump(exclude_unset=True)

    if "status" in update_data:
        if update_data["status"] in [FAIStatus.PASSED, FAIStatus.FAILED, FAIStatus.CONDITIONAL]:
            fai.completed_date = date.today()
            fai.approved_by = current_user.id

    for field, value in update_data.items():
        setattr(fai, field, value)

    OperationalEventService(db).emit(
        company_id=company_id,
        event_type="fai_updated",
        source_module="quality",
        entity_type="fai",
        entity_id=fai.id,
        work_order_id=fai.work_order_id,
        user_id=current_user.id,
        severity="high" if fai.status == FAIStatus.FAILED else "info",
        event_payload={
            "fai_number": fai.fai_number,
            "changed_fields": [field for field in update_data.keys() if field != "version"],
            "previous_status": _enum_value(previous_status),
            "status": _enum_value(fai.status),
            "characteristics_passed": fai.characteristics_passed,
            "characteristics_failed": fai.characteristics_failed,
        },
    )
    db.commit()
    db.refresh(fai)
    return fai


@router.post("/fai/{fai_id}/characteristics", response_model=FAICharacteristicResponse)
def add_fai_characteristic(
    fai_id: int,
    char_in: FAICharacteristicCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Add a characteristic to FAI"""
    fai = (
        db.query(FirstArticleInspection)
        .filter(FirstArticleInspection.id == fai_id, FirstArticleInspection.company_id == company_id)
        .first()
    )
    if not fai:
        raise HTTPException(status_code=404, detail="FAI not found")

    char = FAICharacteristic(fai_id=fai_id, **char_in.model_dump())
    db.add(char)

    fai.total_characteristics += 1

    db.commit()
    db.refresh(char)
    return char


@router.put("/fai/{fai_id}/characteristics/{char_id}", response_model=FAICharacteristicResponse)
def update_fai_characteristic(
    fai_id: int,
    char_id: int,
    char_in: FAICharacteristicUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Update/record measurement for a characteristic"""
    fai = (
        db.query(FirstArticleInspection)
        .filter(FirstArticleInspection.id == fai_id, FirstArticleInspection.company_id == company_id)
        .first()
    )
    if not fai:
        raise HTTPException(status_code=404, detail="FAI not found")

    char = (
        db.query(FAICharacteristic).filter(FAICharacteristic.id == char_id, FAICharacteristic.fai_id == fai_id).first()
    )

    if not char:
        raise HTTPException(status_code=404, detail="Characteristic not found")

    # Track pass/fail changes
    was_conforming = char.is_conforming

    update_data = char_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(char, field, value)

    # Update FAI pass/fail counts
    if "is_conforming" in update_data:
        if was_conforming is None:
            # First time recording
            if char.is_conforming:
                fai.characteristics_passed += 1
            else:
                fai.characteristics_failed += 1
        elif was_conforming != char.is_conforming:
            # Changed
            if char.is_conforming:
                fai.characteristics_passed += 1
                fai.characteristics_failed -= 1
            else:
                fai.characteristics_passed -= 1
                fai.characteristics_failed += 1

    if "is_conforming" in update_data:
        OperationalEventService(db).emit(
            company_id=company_id,
            event_type="fai_characteristic_recorded",
            source_module="quality",
            entity_type="fai_characteristic",
            entity_id=char.id,
            work_order_id=fai.work_order_id,
            user_id=current_user.id,
            severity="high" if char.is_conforming is False else "info",
            event_payload={
                "fai_id": fai.id,
                "fai_number": fai.fai_number,
                "char_number": char.char_number,
                "is_conforming": char.is_conforming,
                "characteristics_passed": fai.characteristics_passed,
                "characteristics_failed": fai.characteristics_failed,
            },
        )
    db.commit()
    db.refresh(char)
    return char


@router.delete("/fai/{fai_id}/characteristics/{char_id}")
def delete_fai_characteristic(
    fai_id: int,
    char_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Delete a characteristic"""
    char = (
        db.query(FAICharacteristic).filter(FAICharacteristic.id == char_id, FAICharacteristic.fai_id == fai_id).first()
    )

    if not char:
        raise HTTPException(status_code=404, detail="Characteristic not found")

    fai = db.query(FirstArticleInspection).filter(FirstArticleInspection.id == fai_id).first()
    fai.total_characteristics -= 1
    if char.is_conforming is True:
        fai.characteristics_passed -= 1
    elif char.is_conforming is False:
        fai.characteristics_failed -= 1

    db.delete(char)
    db.commit()

    return {"message": "Characteristic deleted"}


# ============== Dashboard/Summary ==============


@router.get("/summary")
def get_quality_summary(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get quality metrics summary"""
    open_ncrs = (
        db.query(func.count(NonConformanceReport.id))
        .filter(
            NonConformanceReport.company_id == company_id,
            NonConformanceReport.status.in_([NCRStatus.OPEN, NCRStatus.UNDER_REVIEW, NCRStatus.PENDING_DISPOSITION]),
        )
        .scalar()
    )

    open_cars = (
        db.query(func.count(CorrectiveActionRequest.id))
        .filter(
            CorrectiveActionRequest.company_id == company_id,
            CorrectiveActionRequest.status.in_(
                [CARStatus.OPEN, CARStatus.ROOT_CAUSE_ANALYSIS, CARStatus.CORRECTIVE_ACTION, CARStatus.VERIFICATION]
            ),
        )
        .scalar()
    )

    pending_fais = (
        db.query(func.count(FirstArticleInspection.id))
        .filter(
            FirstArticleInspection.company_id == company_id,
            FirstArticleInspection.status.in_([FAIStatus.PENDING, FAIStatus.IN_PROGRESS]),
        )
        .scalar()
    )

    # NCRs by disposition this month
    month_start = date.today().replace(day=1)
    ncr_dispositions = (
        db.query(NonConformanceReport.disposition, func.count(NonConformanceReport.id))
        .filter(NonConformanceReport.company_id == company_id, NonConformanceReport.created_at >= month_start)
        .group_by(NonConformanceReport.disposition)
        .all()
    )

    return {
        "open_ncrs": open_ncrs,
        "open_cars": open_cars,
        "pending_fais": pending_fais,
        "ncr_dispositions": {d.value: c for d, c in ncr_dispositions},
    }
