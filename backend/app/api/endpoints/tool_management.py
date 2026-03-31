from typing import List, Optional
from datetime import datetime, date, timedelta
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.db.database import get_db
from app.api.deps import get_current_user
from app.models.user import User
from app.models.tool_management import Tool, ToolCheckout, ToolUsageLog, ToolStatus, ToolType
from pydantic import BaseModel

router = APIRouter()


# ── Pydantic Schemas ──────────────────────────────────────────────────────────

class ToolCreate(BaseModel):
    tool_id: str
    name: str
    tool_type: str = "other"
    description: Optional[str] = None
    manufacturer: Optional[str] = None
    model_number: Optional[str] = None
    serial_number: Optional[str] = None
    location: Optional[str] = None
    max_life_hours: Optional[float] = None
    max_life_cycles: Optional[int] = None
    purchase_date: Optional[date] = None
    purchase_cost: float = 0
    inspection_interval_days: Optional[int] = None
    notes: Optional[str] = None


class ToolUpdate(BaseModel):
    name: Optional[str] = None
    tool_type: Optional[str] = None
    description: Optional[str] = None
    manufacturer: Optional[str] = None
    model_number: Optional[str] = None
    serial_number: Optional[str] = None
    location: Optional[str] = None
    status: Optional[str] = None
    max_life_hours: Optional[float] = None
    max_life_cycles: Optional[int] = None
    purchase_date: Optional[date] = None
    purchase_cost: Optional[float] = None
    inspection_interval_days: Optional[int] = None
    last_inspection_date: Optional[date] = None
    next_inspection_date: Optional[date] = None
    notes: Optional[str] = None
    is_active: Optional[bool] = None


class CheckoutRequest(BaseModel):
    work_center_id: Optional[int] = None
    work_order_id: Optional[int] = None
    condition_out: str = "good"
    notes: Optional[str] = None


class CheckinRequest(BaseModel):
    condition_in: str = "good"
    notes: Optional[str] = None


class UsageLogRequest(BaseModel):
    usage_hours: float = 0
    usage_cycles: int = 0
    work_order_id: Optional[int] = None
    work_center_id: Optional[int] = None
    usage_date: Optional[date] = None
    notes: Optional[str] = None


class ToolResponse(BaseModel):
    id: int
    tool_id: str
    name: str
    tool_type: str
    description: Optional[str] = None
    manufacturer: Optional[str] = None
    model_number: Optional[str] = None
    serial_number: Optional[str] = None
    status: str
    location: Optional[str] = None
    current_work_center_id: Optional[int] = None
    current_user_id: Optional[int] = None
    max_life_hours: Optional[float] = None
    current_life_hours: float = 0
    max_life_cycles: Optional[int] = None
    current_life_cycles: int = 0
    life_remaining_pct: Optional[float] = None
    purchase_date: Optional[date] = None
    purchase_cost: float = 0
    last_inspection_date: Optional[date] = None
    next_inspection_date: Optional[date] = None
    inspection_interval_days: Optional[int] = None
    notes: Optional[str] = None
    is_active: bool
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# ── Helpers ───────────────────────────────────────────────────────────────────

def _calc_life_remaining(tool: Tool) -> Optional[float]:
    """Return life remaining as a percentage 0-100, or None if no max is set."""
    pct_hours = None
    pct_cycles = None
    if tool.max_life_hours and tool.max_life_hours > 0:
        pct_hours = max(0, 100 - (tool.current_life_hours / tool.max_life_hours * 100))
    if tool.max_life_cycles and tool.max_life_cycles > 0:
        pct_cycles = max(0, 100 - (tool.current_life_cycles / tool.max_life_cycles * 100))
    # Use the worst (lowest) of the two if both are set
    if pct_hours is not None and pct_cycles is not None:
        return round(min(pct_hours, pct_cycles), 1)
    return round(pct_hours, 1) if pct_hours is not None else (round(pct_cycles, 1) if pct_cycles is not None else None)


def _tool_to_dict(tool: Tool) -> dict:
    life_pct = _calc_life_remaining(tool)
    tool.life_remaining_pct = life_pct
    return {
        "id": tool.id,
        "tool_id": tool.tool_id,
        "name": tool.name,
        "tool_type": tool.tool_type.value if hasattr(tool.tool_type, 'value') else tool.tool_type,
        "description": tool.description,
        "manufacturer": tool.manufacturer,
        "model_number": tool.model_number,
        "serial_number": tool.serial_number,
        "status": tool.status.value if hasattr(tool.status, 'value') else tool.status,
        "location": tool.location,
        "current_work_center_id": tool.current_work_center_id,
        "current_user_id": tool.current_user_id,
        "max_life_hours": tool.max_life_hours,
        "current_life_hours": tool.current_life_hours or 0,
        "max_life_cycles": tool.max_life_cycles,
        "current_life_cycles": tool.current_life_cycles or 0,
        "life_remaining_pct": life_pct,
        "purchase_date": tool.purchase_date.isoformat() if tool.purchase_date else None,
        "purchase_cost": tool.purchase_cost or 0,
        "last_inspection_date": tool.last_inspection_date.isoformat() if tool.last_inspection_date else None,
        "next_inspection_date": tool.next_inspection_date.isoformat() if tool.next_inspection_date else None,
        "inspection_interval_days": tool.inspection_interval_days,
        "notes": tool.notes,
        "is_active": tool.is_active,
        "created_at": tool.created_at.isoformat() if tool.created_at else None,
        "updated_at": tool.updated_at.isoformat() if tool.updated_at else None,
    }


def _checkout_to_dict(co: ToolCheckout) -> dict:
    return {
        "id": co.id,
        "tool_id": co.tool_id,
        "checked_out_by": co.checked_out_by,
        "checked_out_by_name": co.user.full_name if co.user and hasattr(co.user, 'full_name') else str(co.checked_out_by),
        "checked_out_at": co.checked_out_at.isoformat() if co.checked_out_at else None,
        "checked_in_at": co.checked_in_at.isoformat() if co.checked_in_at else None,
        "work_center_id": co.work_center_id,
        "work_order_id": co.work_order_id,
        "condition_out": co.condition_out,
        "condition_in": co.condition_in,
        "notes": co.notes,
        "created_at": co.created_at.isoformat() if co.created_at else None,
    }


def _usage_to_dict(ul: ToolUsageLog) -> dict:
    return {
        "id": ul.id,
        "tool_id": ul.tool_id,
        "work_order_id": ul.work_order_id,
        "work_center_id": ul.work_center_id,
        "usage_hours": ul.usage_hours or 0,
        "usage_cycles": ul.usage_cycles or 0,
        "usage_date": ul.usage_date.isoformat() if ul.usage_date else None,
        "recorded_by": ul.recorded_by,
        "notes": ul.notes,
        "created_at": ul.created_at.isoformat() if ul.created_at else None,
    }


# ── Dashboard ─────────────────────────────────────────────────────────────────

@router.get("/tools/dashboard")
def get_dashboard(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Summary stats for tools dashboard"""
    tools = db.query(Tool).filter(Tool.is_active == True).all()
    total = len(tools)
    checked_out = sum(1 for t in tools if t.status == ToolStatus.IN_USE)

    replacement_due = 0
    inspection_due = 0
    today = date.today()
    by_type: dict[str, int] = {}
    by_status: dict[str, int] = {}

    for t in tools:
        tt = t.tool_type.value if hasattr(t.tool_type, 'value') else str(t.tool_type)
        by_type[tt] = by_type.get(tt, 0) + 1
        ts = t.status.value if hasattr(t.status, 'value') else str(t.status)
        by_status[ts] = by_status.get(ts, 0) + 1
        pct = _calc_life_remaining(t)
        if pct is not None and pct <= 20:
            replacement_due += 1
        if t.next_inspection_date and t.next_inspection_date <= today:
            inspection_due += 1

    return {
        "total_tools": total,
        "checked_out": checked_out,
        "replacement_due": replacement_due,
        "inspection_due": inspection_due,
        "by_type": by_type,
        "by_status": by_status,
    }


# ── Checked-out / Replacement / Inspection lists ─────────────────────────────

@router.get("/tools/checked-out")
def list_checked_out(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all currently checked-out tools"""
    tools = db.query(Tool).filter(
        Tool.is_active == True,
        Tool.status == ToolStatus.IN_USE,
    ).all()
    return [_tool_to_dict(t) for t in tools]


@router.get("/tools/replacement-due")
def list_replacement_due(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Tools with <=20% life remaining"""
    tools = db.query(Tool).filter(Tool.is_active == True).all()
    result = []
    for t in tools:
        pct = _calc_life_remaining(t)
        if pct is not None and pct <= 20:
            result.append(_tool_to_dict(t))
    return result


@router.get("/tools/inspection-due")
def list_inspection_due(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Tools overdue or due for inspection"""
    today = date.today()
    tools = db.query(Tool).filter(
        Tool.is_active == True,
        Tool.next_inspection_date != None,
        Tool.next_inspection_date <= today,
    ).order_by(Tool.next_inspection_date).all()
    return [_tool_to_dict(t) for t in tools]


# ── CRUD ──────────────────────────────────────────────────────────────────────

@router.get("/tools/")
def list_tools(
    status: Optional[str] = None,
    tool_type: Optional[str] = None,
    search: Optional[str] = None,
    include_inactive: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List tools with optional filters"""
    query = db.query(Tool)
    if not include_inactive:
        query = query.filter(Tool.is_active == True)
    if status:
        query = query.filter(Tool.status == status)
    if tool_type:
        query = query.filter(Tool.tool_type == tool_type)
    if search:
        search_term = f"%{search}%"
        query = query.filter(
            (Tool.tool_id.ilike(search_term)) |
            (Tool.name.ilike(search_term)) |
            (Tool.manufacturer.ilike(search_term)) |
            (Tool.serial_number.ilike(search_term))
        )
    tools = query.order_by(Tool.tool_id).all()
    return [_tool_to_dict(t) for t in tools]


@router.get("/tools/{tool_id}")
def get_tool(
    tool_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get single tool by primary key"""
    tool = db.query(Tool).filter(Tool.id == tool_id).first()
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")
    return _tool_to_dict(tool)


@router.post("/tools/")
def create_tool(
    tool_in: ToolCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create a new tool"""
    if db.query(Tool).filter(Tool.tool_id == tool_in.tool_id).first():
        raise HTTPException(status_code=400, detail="Tool ID already exists")

    data = tool_in.model_dump()
    if data.get("tool_type"):
        data["tool_type"] = ToolType(data["tool_type"])

    # Calculate initial next_inspection_date
    if data.get("inspection_interval_days"):
        base = data.get("purchase_date") or date.today()
        data["next_inspection_date"] = base + timedelta(days=data["inspection_interval_days"])

    tool = Tool(**data)
    tool.life_remaining_pct = _calc_life_remaining(tool)
    db.add(tool)
    db.commit()
    db.refresh(tool)
    return _tool_to_dict(tool)


@router.put("/tools/{tool_id}")
def update_tool(
    tool_id: int,
    tool_in: ToolUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Update a tool"""
    tool = db.query(Tool).filter(Tool.id == tool_id).first()
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")

    update_data = tool_in.model_dump(exclude_unset=True)
    if "status" in update_data:
        update_data["status"] = ToolStatus(update_data["status"])
    if "tool_type" in update_data:
        update_data["tool_type"] = ToolType(update_data["tool_type"])

    for field, value in update_data.items():
        setattr(tool, field, value)

    tool.life_remaining_pct = _calc_life_remaining(tool)
    db.commit()
    db.refresh(tool)
    return _tool_to_dict(tool)


@router.delete("/tools/{tool_id}")
def retire_tool(
    tool_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Retire a tool (soft delete)"""
    tool = db.query(Tool).filter(Tool.id == tool_id).first()
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")
    tool.status = ToolStatus.RETIRED
    tool.is_active = False
    db.commit()
    return {"message": f"Tool {tool.tool_id} retired"}


# ── Check-out / Check-in ─────────────────────────────────────────────────────

@router.post("/tools/{tool_id}/checkout")
def checkout_tool(
    tool_id: int,
    req: CheckoutRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Check out a tool"""
    tool = db.query(Tool).filter(Tool.id == tool_id).first()
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")
    if tool.status == ToolStatus.IN_USE:
        raise HTTPException(status_code=400, detail="Tool is already checked out")
    if tool.status in (ToolStatus.RETIRED, ToolStatus.LOST, ToolStatus.DAMAGED):
        raise HTTPException(status_code=400, detail=f"Cannot check out a {tool.status.value} tool")

    checkout = ToolCheckout(
        tool_id=tool.id,
        checked_out_by=current_user.id,
        checked_out_at=datetime.utcnow(),
        work_center_id=req.work_center_id,
        work_order_id=req.work_order_id,
        condition_out=req.condition_out,
        notes=req.notes,
    )
    db.add(checkout)

    tool.status = ToolStatus.IN_USE
    tool.current_user_id = current_user.id
    tool.current_work_center_id = req.work_center_id

    db.commit()
    db.refresh(checkout)
    return _checkout_to_dict(checkout)


@router.post("/tools/{tool_id}/checkin")
def checkin_tool(
    tool_id: int,
    req: CheckinRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Check in a tool"""
    tool = db.query(Tool).filter(Tool.id == tool_id).first()
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")
    if tool.status != ToolStatus.IN_USE:
        raise HTTPException(status_code=400, detail="Tool is not currently checked out")

    # Find the open checkout record
    checkout = db.query(ToolCheckout).filter(
        ToolCheckout.tool_id == tool.id,
        ToolCheckout.checked_in_at == None,
    ).order_by(ToolCheckout.checked_out_at.desc()).first()

    if checkout:
        checkout.checked_in_at = datetime.utcnow()
        checkout.condition_in = req.condition_in
        if req.notes:
            checkout.notes = (checkout.notes or "") + ("\n" if checkout.notes else "") + req.notes

    # Update tool status based on condition
    if req.condition_in == "damaged":
        tool.status = ToolStatus.DAMAGED
    elif req.condition_in == "needs_maintenance":
        tool.status = ToolStatus.MAINTENANCE
    else:
        tool.status = ToolStatus.AVAILABLE

    tool.current_user_id = None
    tool.current_work_center_id = None

    db.commit()
    return {"message": "Tool checked in", "new_status": tool.status.value}


# ── Usage Logging ─────────────────────────────────────────────────────────────

@router.post("/tools/{tool_id}/log-usage")
def log_usage(
    tool_id: int,
    req: UsageLogRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Log usage hours/cycles for a tool"""
    tool = db.query(Tool).filter(Tool.id == tool_id).first()
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")

    log = ToolUsageLog(
        tool_id=tool.id,
        work_order_id=req.work_order_id,
        work_center_id=req.work_center_id,
        usage_hours=req.usage_hours,
        usage_cycles=req.usage_cycles,
        usage_date=req.usage_date or date.today(),
        recorded_by=current_user.id,
        notes=req.notes,
    )
    db.add(log)

    # Update cumulative totals
    tool.current_life_hours = (tool.current_life_hours or 0) + req.usage_hours
    tool.current_life_cycles = (tool.current_life_cycles or 0) + req.usage_cycles
    tool.life_remaining_pct = _calc_life_remaining(tool)

    db.commit()
    db.refresh(log)
    return _usage_to_dict(log)


# ── History ───────────────────────────────────────────────────────────────────

@router.get("/tools/{tool_id}/history")
def get_tool_history(
    tool_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get checkout and usage history for a tool"""
    tool = db.query(Tool).filter(Tool.id == tool_id).first()
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")

    checkouts = db.query(ToolCheckout).filter(
        ToolCheckout.tool_id == tool_id
    ).order_by(ToolCheckout.checked_out_at.desc()).all()

    usage_logs = db.query(ToolUsageLog).filter(
        ToolUsageLog.tool_id == tool_id
    ).order_by(ToolUsageLog.usage_date.desc()).all()

    return {
        "tool": _tool_to_dict(tool),
        "checkouts": [_checkout_to_dict(co) for co in checkouts],
        "usage_logs": [_usage_to_dict(ul) for ul in usage_logs],
    }
