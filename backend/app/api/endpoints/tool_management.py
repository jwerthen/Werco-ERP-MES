from datetime import date, datetime, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import AliasChoices, BaseModel, Field, field_validator, model_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_audit_service, get_current_company_id, get_current_user, require_role
from app.core.time_utils import to_utc_iso
from app.db.database import get_db
from app.db.tenant_filter import tenant_query
from app.models.tool_management import Tool, ToolCheckout, ToolStatus, ToolType, ToolUsageLog
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import WorkOrder
from app.services.audit_service import AuditService

router = APIRouter()

# Who may change the tool crib. Until this change every handler here was a bare
# ``get_current_user``, so a VIEWER could create, edit and retire tools.
#
# Planning verbs (create / edit / retire a tool record) match the inventory stock
# mutators (``inventory.STOCK_MUTATOR_ROLES``: Admin / Manager / Supervisor) -- a tool
# is crib inventory. Using a tool (check out / check in / log usage) stays open to
# every role that works on the floor; only the read-only Viewer loses it, so no
# operator, inspector or shipper who checks tools out today is cut off.
# ``require_role`` always admits PLATFORM_ADMIN and superusers. Reads stay open.
TOOL_PLANNING_ROLES = [UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]
TOOL_USE_ROLES = [
    UserRole.ADMIN,
    UserRole.MANAGER,
    UserRole.SUPERVISOR,
    UserRole.OPERATOR,
    UserRole.QUALITY,
    UserRole.SHIPPING,
]

# Fields ``PUT /tools/{id}`` may write, applied explicitly -- never a blind setattr
# over the request body. ``status`` is handled separately (see ``update_tool``).
_TOOL_EDITABLE_FIELDS = (
    "name",
    "tool_type",
    "description",
    "manufacturer",
    "model_number",
    "serial_number",
    "location",
    "max_life_hours",
    "max_life_cycles",
    "purchase_date",
    "purchase_cost",
    "inspection_interval_days",
    "last_inspection_date",
    "next_inspection_date",
    "notes",
    "is_active",
)
_TOOL_NON_NULLABLE_UPDATE_FIELDS = ("name", "tool_type", "status", "is_active")

# What a check-in condition does to the tool. A closed vocabulary: an unknown word is a
# 422, never silently read as "good". It covers both the API's own words and the ones the
# Tool Management page offers (``worn`` / ``needs_repair``), so the condition a person
# picked is the condition recorded -- on the checkout row and in the audit row.
CHECKIN_CONDITION_STATUS = {
    "good": ToolStatus.AVAILABLE,
    "worn": ToolStatus.AVAILABLE,
    "damaged": ToolStatus.DAMAGED,
    "needs_maintenance": ToolStatus.MAINTENANCE,
    "needs_repair": ToolStatus.MAINTENANCE,
}


def _normalize_lower(value: Any) -> Any:
    return value.strip().lower() if isinstance(value, str) else value


# ── Pydantic Schemas ──────────────────────────────────────────────────────────


class ToolCreate(BaseModel):
    tool_id: str
    name: str
    tool_type: ToolType = ToolType.OTHER
    description: Optional[str] = None
    manufacturer: Optional[str] = None
    model_number: Optional[str] = None
    serial_number: Optional[str] = None
    location: Optional[str] = None
    max_life_hours: Optional[float] = None
    max_life_cycles: Optional[int] = None
    purchase_date: Optional[date] = None
    purchase_cost: float = 0
    inspection_interval_days: Optional[int] = Field(None, ge=1, le=3650)
    notes: Optional[str] = None

    @field_validator("tool_type", mode="before")
    @classmethod
    def _normalize_tool_type(cls, value: Any) -> Any:
        return _normalize_lower(value)


class ToolUpdate(BaseModel):
    name: Optional[str] = None
    tool_type: Optional[ToolType] = None
    description: Optional[str] = None
    manufacturer: Optional[str] = None
    model_number: Optional[str] = None
    serial_number: Optional[str] = None
    location: Optional[str] = None
    status: Optional[ToolStatus] = None
    max_life_hours: Optional[float] = None
    max_life_cycles: Optional[int] = None
    purchase_date: Optional[date] = None
    purchase_cost: Optional[float] = None
    inspection_interval_days: Optional[int] = Field(None, ge=1, le=3650)
    last_inspection_date: Optional[date] = None
    next_inspection_date: Optional[date] = None
    notes: Optional[str] = None
    is_active: Optional[bool] = None

    @field_validator("tool_type", "status", mode="before")
    @classmethod
    def _normalize_enums(cls, value: Any) -> Any:
        return _normalize_lower(value)

    @model_validator(mode="after")
    def _refuse_explicit_nulls(self) -> "ToolUpdate":
        nulled = [
            field
            for field in _TOOL_NON_NULLABLE_UPDATE_FIELDS
            if field in self.model_fields_set and getattr(self, field) is None
        ]
        if nulled:
            raise ValueError(f"{', '.join(nulled)} cannot be null")
        return self


class CheckoutRequest(BaseModel):
    work_center_id: Optional[int] = None
    work_order_id: Optional[int] = None
    condition_out: str = "good"
    notes: Optional[str] = None


class CheckinRequest(BaseModel):
    # ``condition`` is accepted as an alias because that is the key the Tool Management page
    # sends. Before, the page's choice was dropped as an unknown key and every check-in --
    # "damaged" included -- was recorded as ``good`` and returned the tool to AVAILABLE.
    condition_in: str = Field("good", validation_alias=AliasChoices("condition_in", "condition"))
    notes: Optional[str] = None

    @field_validator("condition_in", mode="before")
    @classmethod
    def _validate_condition(cls, value: Any) -> Any:
        normalized = _normalize_lower(value)
        if normalized not in CHECKIN_CONDITION_STATUS:
            raise ValueError(f"condition_in must be one of: {', '.join(CHECKIN_CONDITION_STATUS)}")
        return normalized


class UsageLogRequest(BaseModel):
    # Non-negative: a negative entry would silently rewind a tool's accumulated life
    # and hide it from the replacement-due list.
    usage_hours: float = Field(0, ge=0)
    usage_cycles: int = Field(0, ge=0)
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


def _status_value(status: Any) -> Optional[str]:
    return status.value if hasattr(status, "value") else status


def _tool_or_404(db: Session, tool_id: int, company_id: int, *, for_update: bool = False) -> Tool:
    """Resolve a tool inside the caller's tenant, or a flat 404 (never 403).

    A foreign tool must be indistinguishable from a nonexistent one, so the status
    code cannot be used as an existence oracle over another tenant's crib.

    ``for_update`` row-locks the tool (``FOR UPDATE OF tools`` on Postgres; a no-op on
    SQLite) for the write verbs. ``Tool`` has no ``version`` column: without the lock two
    simultaneous checkouts both read AVAILABLE and leave two open ledger rows, and two usage
    logs lose one of their increments to ``current_life_hours``.
    """
    query = tenant_query(db, Tool, company_id).filter(Tool.id == tool_id)
    if for_update:
        query = query.with_for_update(of=Tool)
    tool = query.first()
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")
    return tool


def _assert_refs_in_company(
    db: Session, company_id: int, *, work_center_id: Optional[int], work_order_id: Optional[int]
) -> None:
    """Refuse a work center / work order that is not the caller's (flat 404).

    These ids are stored on the checkout / usage row and drive ``current_work_center_id``;
    unvalidated, a caller could tie its tool record to another tenant's machine or job.
    A new link is a SELECTION, so a soft-deleted work order is refused too (invariant 3).
    """
    if work_center_id is not None:
        if not tenant_query(db, WorkCenter, company_id).filter(WorkCenter.id == work_center_id).first():
            raise HTTPException(status_code=404, detail="Work center not found")
    if work_order_id is not None:
        live = (
            tenant_query(db, WorkOrder, company_id)
            .filter(WorkOrder.id == work_order_id, WorkOrder.is_deleted == False)  # noqa: E712
            .first()
        )
        if not live:
            raise HTTPException(status_code=404, detail="Work order not found")


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
        "created_at": to_utc_iso(tool.created_at),
        "updated_at": to_utc_iso(tool.updated_at),
    }


def _checkout_to_dict(co: ToolCheckout) -> dict:
    return {
        "id": co.id,
        "tool_id": co.tool_id,
        "checked_out_by": co.checked_out_by,
        "checked_out_by_name": (
            co.user.full_name if co.user and hasattr(co.user, 'full_name') else str(co.checked_out_by)
        ),
        "checked_out_at": to_utc_iso(co.checked_out_at),
        "checked_in_at": to_utc_iso(co.checked_in_at),
        "work_center_id": co.work_center_id,
        "work_order_id": co.work_order_id,
        "condition_out": co.condition_out,
        "condition_in": co.condition_in,
        "notes": co.notes,
        "created_at": to_utc_iso(co.created_at),
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
        "created_at": to_utc_iso(ul.created_at),
    }


# ── Dashboard ─────────────────────────────────────────────────────────────────


@router.get("/tools/dashboard")
def get_dashboard(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Summary stats for tools dashboard"""
    tools = tenant_query(db, Tool, company_id).filter(Tool.is_active == True).all()
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
    company_id: int = Depends(get_current_company_id),
):
    """List all currently checked-out tools"""
    tools = (
        tenant_query(db, Tool, company_id)
        .filter(
            Tool.is_active == True,
            Tool.status == ToolStatus.IN_USE,
        )
        .all()
    )
    return [_tool_to_dict(t) for t in tools]


@router.get("/tools/replacement-due")
def list_replacement_due(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Tools with <=20% life remaining"""
    tools = tenant_query(db, Tool, company_id).filter(Tool.is_active == True).all()
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
    company_id: int = Depends(get_current_company_id),
):
    """Tools overdue or due for inspection"""
    today = date.today()
    tools = (
        tenant_query(db, Tool, company_id)
        .filter(
            Tool.is_active == True,
            Tool.next_inspection_date != None,
            Tool.next_inspection_date <= today,
        )
        .order_by(Tool.next_inspection_date)
        .all()
    )
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
    company_id: int = Depends(get_current_company_id),
):
    """List tools with optional filters"""
    query = tenant_query(db, Tool, company_id)
    if not include_inactive:
        query = query.filter(Tool.is_active == True)
    if status:
        query = query.filter(Tool.status == status)
    if tool_type:
        query = query.filter(Tool.tool_type == tool_type)
    if search:
        search_term = f"%{search}%"
        query = query.filter(
            (Tool.tool_id.ilike(search_term))
            | (Tool.name.ilike(search_term))
            | (Tool.manufacturer.ilike(search_term))
            | (Tool.serial_number.ilike(search_term))
        )
    tools = query.order_by(Tool.tool_id).all()
    return [_tool_to_dict(t) for t in tools]


@router.get("/tools/{tool_id}")
def get_tool(
    tool_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get single tool by primary key"""
    return _tool_to_dict(_tool_or_404(db, tool_id, company_id))


@router.post("/tools/")
def create_tool(
    tool_in: ToolCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(TOOL_PLANNING_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Create a new tool. **Role: ADMIN / MANAGER / SUPERVISOR.**"""
    if tenant_query(db, Tool, company_id).filter(Tool.tool_id == tool_in.tool_id).first():
        raise HTTPException(status_code=400, detail="Tool ID already exists")

    data = tool_in.model_dump()

    # Calculate initial next_inspection_date
    if data.get("inspection_interval_days"):
        base = data.get("purchase_date") or date.today()
        data["next_inspection_date"] = base + timedelta(days=data["inspection_interval_days"])

    tool = Tool(**data)
    tool.company_id = company_id
    tool.life_remaining_pct = _calc_life_remaining(tool)
    db.add(tool)
    try:
        db.flush()
    except IntegrityError:
        # ``tools.tool_id`` still carries a GLOBAL unique constraint (no per-company
        # compound yet), so a code another tenant already uses fails here rather than
        # in the tenant-scoped probe above. Same answer as the probe.
        db.rollback()
        raise HTTPException(status_code=400, detail="Tool ID already exists")
    audit.log_create("tool", tool.id, tool.tool_id, new_values=tool)
    db.commit()
    db.refresh(tool)
    return _tool_to_dict(tool)


@router.put("/tools/{tool_id}")
def update_tool(
    tool_id: int,
    tool_in: ToolUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(TOOL_PLANNING_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Update a tool. **Role: ADMIN / MANAGER / SUPERVISOR.**

    ``in_use`` is entered only by ``POST /tools/{id}/checkout`` and left only by
    ``POST /tools/{id}/checkin``, so the checkout ledger and the tool's status cannot
    drift apart: setting ``status: in_use`` here, or changing the status or ``is_active``
    of a tool that is checked out, is **409**. Retirement has one door too:
    ``status: retired`` here is **409** naming ``DELETE /tools/{id}``, which also clears
    ``is_active`` and refuses a checked-out tool.
    """
    tool = _tool_or_404(db, tool_id, company_id, for_update=True)

    update_data = tool_in.model_dump(exclude_unset=True)
    old_status = tool.status
    new_status = update_data.pop("status", None)
    status_changes = new_status is not None and new_status != old_status
    if status_changes and new_status == ToolStatus.IN_USE:
        raise HTTPException(
            status_code=409, detail=f"A tool is marked in use only by checking it out: POST /tools/{tool.id}/checkout"
        )
    if status_changes and new_status == ToolStatus.RETIRED:
        raise HTTPException(status_code=409, detail=f"Retire a tool with DELETE /tools/{tool.id}")
    active_changes = "is_active" in update_data and update_data["is_active"] != tool.is_active
    if (status_changes or active_changes) and old_status == ToolStatus.IN_USE:
        # Hiding a checked-out tool (``is_active: false``) drops it off ``/tools/checked-out``
        # and strands its open checkout row -- the same drift the status rule prevents.
        raise HTTPException(
            status_code=409,
            detail=f"Tool {tool.tool_id} is checked out. Check it in first: POST /tools/{tool.id}/checkin",
        )

    old_values = {field: getattr(tool, field) for field in _TOOL_EDITABLE_FIELDS}
    for field in _TOOL_EDITABLE_FIELDS:
        if field in update_data:
            setattr(tool, field, update_data[field])
    if status_changes:
        tool.status = new_status

    tool.life_remaining_pct = _calc_life_remaining(tool)
    db.flush()
    audit.log_update(
        "tool",
        tool.id,
        tool.tool_id,
        old_values=old_values,
        new_values={field: getattr(tool, field) for field in _TOOL_EDITABLE_FIELDS},
    )
    if status_changes:
        audit.log_status_change(
            "tool", tool.id, tool.tool_id, old_status=_status_value(old_status), new_status=_status_value(new_status)
        )
    db.commit()
    db.refresh(tool)
    return _tool_to_dict(tool)


@router.delete("/tools/{tool_id}")
def retire_tool(
    tool_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(TOOL_PLANNING_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Retire a tool. **Role: ADMIN / MANAGER / SUPERVISOR.**

    Not a delete: the row, its checkout ledger and usage history are kept; the tool
    moves to RETIRED and drops off the active lists. A checked-out tool is **409** --
    check it in first, so no open checkout is left behind. Retiring a tool that is
    already retired (and inactive) is a no-op: no second audit row.
    """
    tool = _tool_or_404(db, tool_id, company_id, for_update=True)
    if tool.status == ToolStatus.IN_USE:
        raise HTTPException(
            status_code=409,
            detail=f"Tool {tool.tool_id} is checked out. Check it in first: POST /tools/{tool.id}/checkin",
        )
    if tool.status == ToolStatus.RETIRED and not tool.is_active:
        return {"message": f"Tool {tool.tool_id} is already retired"}
    old_status = tool.status
    old_active = tool.is_active
    tool.status = ToolStatus.RETIRED
    tool.is_active = False
    db.flush()
    audit.log_status_change(
        "tool",
        tool.id,
        tool.tool_id,
        old_status=_status_value(old_status),
        new_status=_status_value(ToolStatus.RETIRED),
        description=f"Retired tool {tool.tool_id}",
        extra_data={"is_active": {"old": old_active, "new": False}},
    )
    db.commit()
    return {"message": f"Tool {tool.tool_id} retired"}


# ── Check-out / Check-in ─────────────────────────────────────────────────────


@router.post("/tools/{tool_id}/checkout")
def checkout_tool(
    tool_id: int,
    req: CheckoutRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(TOOL_USE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Check out a tool. **Role: any tenant role except VIEWER.**"""
    tool = _tool_or_404(db, tool_id, company_id, for_update=True)
    if tool.status == ToolStatus.IN_USE:
        raise HTTPException(status_code=400, detail="Tool is already checked out")
    if tool.status in (ToolStatus.RETIRED, ToolStatus.LOST, ToolStatus.DAMAGED):
        raise HTTPException(status_code=400, detail=f"Cannot check out a {tool.status.value} tool")
    _assert_refs_in_company(db, company_id, work_center_id=req.work_center_id, work_order_id=req.work_order_id)

    old_status = tool.status
    checkout = ToolCheckout(
        tool_id=tool.id,
        checked_out_by=current_user.id,
        checked_out_at=datetime.utcnow(),
        work_center_id=req.work_center_id,
        work_order_id=req.work_order_id,
        condition_out=req.condition_out,
        notes=req.notes,
    )
    checkout.company_id = tool.company_id
    db.add(checkout)

    tool.status = ToolStatus.IN_USE
    tool.current_user_id = current_user.id
    tool.current_work_center_id = req.work_center_id

    db.flush()
    audit.log_status_change(
        "tool",
        tool.id,
        tool.tool_id,
        old_status=_status_value(old_status),
        new_status=_status_value(ToolStatus.IN_USE),
        description=f"Checked out tool {tool.tool_id}",
        extra_data={
            "checkout_id": checkout.id,
            "work_center_id": req.work_center_id,
            "work_order_id": req.work_order_id,
            "condition_out": req.condition_out,
        },
    )
    db.commit()
    db.refresh(checkout)
    return _checkout_to_dict(checkout)


@router.post("/tools/{tool_id}/checkin")
def checkin_tool(
    tool_id: int,
    req: CheckinRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(TOOL_USE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Check in a tool. **Role: any tenant role except VIEWER.**

    ``condition_in`` (alias ``condition``) decides the tool's next status: ``good`` / ``worn``
    → available, ``damaged`` → damaged, ``needs_maintenance`` / ``needs_repair`` →
    maintenance. Any other word is **422**.
    """
    tool = _tool_or_404(db, tool_id, company_id, for_update=True)
    if tool.status != ToolStatus.IN_USE:
        raise HTTPException(status_code=400, detail="Tool is not currently checked out")

    # Find the open checkout record
    checkout = (
        tenant_query(db, ToolCheckout, company_id)
        .filter(
            ToolCheckout.tool_id == tool.id,
            ToolCheckout.checked_in_at == None,
        )
        .order_by(ToolCheckout.checked_out_at.desc())
        .first()
    )

    if checkout:
        checkout.checked_in_at = datetime.utcnow()
        checkout.condition_in = req.condition_in
        if req.notes:
            checkout.notes = (checkout.notes or "") + ("\n" if checkout.notes else "") + req.notes

    old_status = tool.status
    tool.status = CHECKIN_CONDITION_STATUS[req.condition_in]

    tool.current_user_id = None
    tool.current_work_center_id = None

    db.flush()
    audit.log_status_change(
        "tool",
        tool.id,
        tool.tool_id,
        old_status=_status_value(old_status),
        new_status=_status_value(tool.status),
        description=f"Checked in tool {tool.tool_id}",
        extra_data={"checkout_id": checkout.id if checkout else None, "condition_in": req.condition_in},
    )
    db.commit()
    return {"message": "Tool checked in", "new_status": tool.status.value}


# ── Usage Logging ─────────────────────────────────────────────────────────────


@router.post("/tools/{tool_id}/log-usage")
def log_usage(
    tool_id: int,
    req: UsageLogRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(TOOL_USE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Log usage hours/cycles for a tool. **Role: any tenant role except VIEWER.**"""
    tool = _tool_or_404(db, tool_id, company_id, for_update=True)
    _assert_refs_in_company(db, company_id, work_center_id=req.work_center_id, work_order_id=req.work_order_id)

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
    log.company_id = tool.company_id
    db.add(log)

    # Update cumulative totals
    tool.current_life_hours = (tool.current_life_hours or 0) + req.usage_hours
    tool.current_life_cycles = (tool.current_life_cycles or 0) + req.usage_cycles
    tool.life_remaining_pct = _calc_life_remaining(tool)

    db.flush()
    audit.log_create(
        "tool_usage_log",
        log.id,
        tool.tool_id,
        new_values=log,
        extra_data={
            "tool_id": tool.id,
            "current_life_hours": tool.current_life_hours,
            "current_life_cycles": tool.current_life_cycles,
        },
    )
    db.commit()
    db.refresh(log)
    return _usage_to_dict(log)


# ── History ───────────────────────────────────────────────────────────────────


@router.get("/tools/{tool_id}/history")
def get_tool_history(
    tool_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get checkout and usage history for a tool (404 for another tenant's tool)."""
    tool = _tool_or_404(db, tool_id, company_id)

    checkouts = (
        tenant_query(db, ToolCheckout, company_id)
        .filter(ToolCheckout.tool_id == tool.id)
        .order_by(ToolCheckout.checked_out_at.desc())
        .all()
    )

    usage_logs = (
        tenant_query(db, ToolUsageLog, company_id)
        .filter(ToolUsageLog.tool_id == tool.id)
        .order_by(ToolUsageLog.usage_date.desc())
        .all()
    )

    return {
        "tool": _tool_to_dict(tool),
        "checkouts": [_checkout_to_dict(co) for co in checkouts],
        "usage_logs": [_usage_to_dict(ul) for ul in usage_logs],
    }
