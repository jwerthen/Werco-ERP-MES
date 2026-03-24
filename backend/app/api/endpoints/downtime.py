from typing import List, Optional
from datetime import datetime, date
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func, case
from app.db.database import get_db
from app.api.deps import get_current_user
from app.models.user import User
from app.models.downtime import DowntimeEvent, DowntimeReasonCode, DowntimeCategory, DowntimePlannedType

router = APIRouter()


# ============== Pydantic Schemas ==============

class DowntimeEventCreate(BaseModel):
    work_center_id: int
    work_order_id: Optional[int] = None
    start_time: Optional[datetime] = None
    category: DowntimeCategory = DowntimeCategory.OTHER
    planned_type: DowntimePlannedType = DowntimePlannedType.UNPLANNED
    reason_code: Optional[str] = None
    description: Optional[str] = None


class DowntimeEventUpdate(BaseModel):
    work_center_id: Optional[int] = None
    work_order_id: Optional[int] = None
    category: Optional[DowntimeCategory] = None
    planned_type: Optional[DowntimePlannedType] = None
    reason_code: Optional[str] = None
    description: Optional[str] = None
    resolution: Optional[str] = None


class DowntimeResolve(BaseModel):
    end_time: Optional[datetime] = None
    resolution: Optional[str] = None


class ReasonCodeCreate(BaseModel):
    code: str = Field(..., max_length=50)
    name: str = Field(..., max_length=255)
    category: DowntimeCategory
    description: Optional[str] = None
    is_active: bool = True
    display_order: int = 0


class ReasonCodeUpdate(BaseModel):
    code: Optional[str] = None
    name: Optional[str] = None
    category: Optional[DowntimeCategory] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None
    display_order: Optional[int] = None


class WorkCenterInfo(BaseModel):
    id: int
    code: str
    name: str

    class Config:
        from_attributes = True


class UserInfo(BaseModel):
    id: int
    username: str
    full_name: Optional[str] = None

    class Config:
        from_attributes = True


class WorkOrderInfo(BaseModel):
    id: int
    wo_number: Optional[str] = None

    class Config:
        from_attributes = True


class DowntimeEventResponse(BaseModel):
    id: int
    work_center_id: int
    work_order_id: Optional[int] = None
    start_time: datetime
    end_time: Optional[datetime] = None
    duration_minutes: Optional[float] = None
    category: str
    planned_type: str
    reason_code: Optional[str] = None
    description: Optional[str] = None
    resolution: Optional[str] = None
    reported_by: int
    resolved_by: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    work_center: Optional[WorkCenterInfo] = None
    reporter: Optional[UserInfo] = None
    resolver: Optional[UserInfo] = None
    work_order: Optional[WorkOrderInfo] = None

    class Config:
        from_attributes = True


class ReasonCodeResponse(BaseModel):
    id: int
    code: str
    name: str
    category: str
    description: Optional[str] = None
    is_active: bool
    display_order: int
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class DowntimeSummary(BaseModel):
    total_downtime_hours: float
    planned_hours: float
    unplanned_hours: float
    planned_percentage: float
    unplanned_percentage: float
    by_category: list
    top_reasons: list
    event_count: int


class WorkCenterDowntime(BaseModel):
    work_center_id: int
    work_center_code: str
    work_center_name: str
    total_hours: float
    event_count: int


# ============== Downtime Event Endpoints ==============

@router.get("/active", response_model=List[DowntimeEventResponse])
def get_active_downtime(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get all currently active (ongoing) downtime events"""
    events = db.query(DowntimeEvent).options(
        joinedload(DowntimeEvent.work_center),
        joinedload(DowntimeEvent.reporter),
        joinedload(DowntimeEvent.resolver),
        joinedload(DowntimeEvent.work_order),
    ).filter(
        DowntimeEvent.end_time.is_(None)
    ).order_by(DowntimeEvent.start_time.desc()).all()
    return events


@router.get("/summary")
def get_downtime_summary(
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    work_center_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get summary statistics for downtime events"""
    query = db.query(DowntimeEvent).filter(DowntimeEvent.end_time.isnot(None))

    if date_from:
        query = query.filter(DowntimeEvent.start_time >= datetime.combine(date_from, datetime.min.time()))
    if date_to:
        query = query.filter(DowntimeEvent.start_time <= datetime.combine(date_to, datetime.max.time()))
    if work_center_id:
        query = query.filter(DowntimeEvent.work_center_id == work_center_id)

    events = query.all()

    total_minutes = sum(e.duration_minutes or 0 for e in events)
    planned_minutes = sum(e.duration_minutes or 0 for e in events if e.planned_type == DowntimePlannedType.PLANNED)
    unplanned_minutes = total_minutes - planned_minutes

    # By category breakdown
    category_map: dict = {}
    for e in events:
        cat = e.category.value if e.category else "other"
        if cat not in category_map:
            category_map[cat] = 0.0
        category_map[cat] += (e.duration_minutes or 0) / 60.0

    by_category = [
        {"category": cat, "hours": round(hrs, 2)}
        for cat, hrs in sorted(category_map.items(), key=lambda x: -x[1])
    ]

    # Top reasons (Pareto)
    reason_map: dict = {}
    for e in events:
        reason = e.reason_code or e.description or "Unspecified"
        if reason not in reason_map:
            reason_map[reason] = 0.0
        reason_map[reason] += (e.duration_minutes or 0) / 60.0

    top_reasons = [
        {"reason": reason, "hours": round(hrs, 2)}
        for reason, hrs in sorted(reason_map.items(), key=lambda x: -x[1])
    ][:15]

    return {
        "total_downtime_hours": round(total_minutes / 60.0, 2),
        "planned_hours": round(planned_minutes / 60.0, 2),
        "unplanned_hours": round(unplanned_minutes / 60.0, 2),
        "planned_percentage": round((planned_minutes / total_minutes * 100) if total_minutes > 0 else 0, 1),
        "unplanned_percentage": round((unplanned_minutes / total_minutes * 100) if total_minutes > 0 else 0, 1),
        "by_category": by_category,
        "top_reasons": top_reasons,
        "event_count": len(events),
    }


@router.get("/by-work-center")
def get_downtime_by_work_center(
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get downtime hours grouped by work center"""
    from app.models.work_center import WorkCenter

    query = db.query(
        DowntimeEvent.work_center_id,
        WorkCenter.code,
        WorkCenter.name,
        func.sum(DowntimeEvent.duration_minutes).label("total_minutes"),
        func.count(DowntimeEvent.id).label("event_count"),
    ).join(
        WorkCenter, DowntimeEvent.work_center_id == WorkCenter.id
    ).filter(
        DowntimeEvent.end_time.isnot(None)
    )

    if date_from:
        query = query.filter(DowntimeEvent.start_time >= datetime.combine(date_from, datetime.min.time()))
    if date_to:
        query = query.filter(DowntimeEvent.start_time <= datetime.combine(date_to, datetime.max.time()))

    results = query.group_by(
        DowntimeEvent.work_center_id, WorkCenter.code, WorkCenter.name
    ).order_by(func.sum(DowntimeEvent.duration_minutes).desc()).all()

    return [
        {
            "work_center_id": r.work_center_id,
            "work_center_code": r.code,
            "work_center_name": r.name,
            "total_hours": round((r.total_minutes or 0) / 60.0, 2),
            "event_count": r.event_count,
        }
        for r in results
    ]


@router.get("/reason-codes", response_model=List[ReasonCodeResponse])
def list_reason_codes(
    category: Optional[DowntimeCategory] = None,
    active_only: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all downtime reason codes"""
    query = db.query(DowntimeReasonCode)
    if category:
        query = query.filter(DowntimeReasonCode.category == category)
    if active_only:
        query = query.filter(DowntimeReasonCode.is_active == True)
    return query.order_by(DowntimeReasonCode.display_order, DowntimeReasonCode.code).all()


@router.post("/reason-codes", response_model=ReasonCodeResponse)
def create_reason_code(
    data: ReasonCodeCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a new downtime reason code"""
    existing = db.query(DowntimeReasonCode).filter(DowntimeReasonCode.code == data.code).first()
    if existing:
        raise HTTPException(status_code=400, detail="Reason code already exists")

    reason_code = DowntimeReasonCode(**data.model_dump())
    db.add(reason_code)
    db.commit()
    db.refresh(reason_code)
    return reason_code


@router.put("/reason-codes/{reason_code_id}", response_model=ReasonCodeResponse)
def update_reason_code(
    reason_code_id: int,
    data: ReasonCodeUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update a downtime reason code"""
    reason_code = db.query(DowntimeReasonCode).filter(DowntimeReasonCode.id == reason_code_id).first()
    if not reason_code:
        raise HTTPException(status_code=404, detail="Reason code not found")

    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(reason_code, field, value)

    db.commit()
    db.refresh(reason_code)
    return reason_code


@router.get("/", response_model=List[DowntimeEventResponse])
def list_downtime_events(
    skip: int = 0,
    limit: int = 100,
    work_center_id: Optional[int] = None,
    category: Optional[DowntimeCategory] = None,
    planned_type: Optional[DowntimePlannedType] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    active_only: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List downtime events with optional filters"""
    query = db.query(DowntimeEvent).options(
        joinedload(DowntimeEvent.work_center),
        joinedload(DowntimeEvent.reporter),
        joinedload(DowntimeEvent.resolver),
        joinedload(DowntimeEvent.work_order),
    )

    if work_center_id:
        query = query.filter(DowntimeEvent.work_center_id == work_center_id)
    if category:
        query = query.filter(DowntimeEvent.category == category)
    if planned_type:
        query = query.filter(DowntimeEvent.planned_type == planned_type)
    if date_from:
        query = query.filter(DowntimeEvent.start_time >= datetime.combine(date_from, datetime.min.time()))
    if date_to:
        query = query.filter(DowntimeEvent.start_time <= datetime.combine(date_to, datetime.max.time()))
    if active_only:
        query = query.filter(DowntimeEvent.end_time.is_(None))

    return query.order_by(DowntimeEvent.start_time.desc()).offset(skip).limit(limit).all()


@router.get("/{event_id}", response_model=DowntimeEventResponse)
def get_downtime_event(
    event_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get a single downtime event by ID"""
    event = db.query(DowntimeEvent).options(
        joinedload(DowntimeEvent.work_center),
        joinedload(DowntimeEvent.reporter),
        joinedload(DowntimeEvent.resolver),
        joinedload(DowntimeEvent.work_order),
    ).filter(DowntimeEvent.id == event_id).first()

    if not event:
        raise HTTPException(status_code=404, detail="Downtime event not found")
    return event


@router.post("/", response_model=DowntimeEventResponse)
def create_downtime_event(
    data: DowntimeEventCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Start a new downtime event"""
    event = DowntimeEvent(
        work_center_id=data.work_center_id,
        work_order_id=data.work_order_id,
        start_time=data.start_time or datetime.utcnow(),
        category=data.category,
        planned_type=data.planned_type,
        reason_code=data.reason_code,
        description=data.description,
        reported_by=current_user.id,
    )
    db.add(event)
    db.commit()
    db.refresh(event)

    # Reload with relationships
    event = db.query(DowntimeEvent).options(
        joinedload(DowntimeEvent.work_center),
        joinedload(DowntimeEvent.reporter),
        joinedload(DowntimeEvent.resolver),
        joinedload(DowntimeEvent.work_order),
    ).filter(DowntimeEvent.id == event.id).first()

    return event


@router.put("/{event_id}", response_model=DowntimeEventResponse)
def update_downtime_event(
    event_id: int,
    data: DowntimeEventUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update a downtime event"""
    event = db.query(DowntimeEvent).filter(DowntimeEvent.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Downtime event not found")

    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(event, field, value)

    db.commit()
    db.refresh(event)

    event = db.query(DowntimeEvent).options(
        joinedload(DowntimeEvent.work_center),
        joinedload(DowntimeEvent.reporter),
        joinedload(DowntimeEvent.resolver),
        joinedload(DowntimeEvent.work_order),
    ).filter(DowntimeEvent.id == event.id).first()

    return event


@router.post("/{event_id}/resolve", response_model=DowntimeEventResponse)
def resolve_downtime_event(
    event_id: int,
    data: DowntimeResolve,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """End/resolve a downtime event - sets end_time and calculates duration"""
    event = db.query(DowntimeEvent).filter(DowntimeEvent.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Downtime event not found")

    if event.end_time is not None:
        raise HTTPException(status_code=400, detail="Downtime event is already resolved")

    end_time = data.end_time or datetime.utcnow()
    if end_time <= event.start_time:
        raise HTTPException(status_code=400, detail="End time must be after start time")

    event.end_time = end_time
    event.duration_minutes = round((end_time - event.start_time).total_seconds() / 60.0, 2)
    event.resolved_by = current_user.id
    if data.resolution:
        event.resolution = data.resolution

    db.commit()
    db.refresh(event)

    event = db.query(DowntimeEvent).options(
        joinedload(DowntimeEvent.work_center),
        joinedload(DowntimeEvent.reporter),
        joinedload(DowntimeEvent.resolver),
        joinedload(DowntimeEvent.work_order),
    ).filter(DowntimeEvent.id == event.id).first()

    return event
