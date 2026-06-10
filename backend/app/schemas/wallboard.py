"""Pydantic contracts for the shop-floor TV wallboard payload (A0.5).

PRIVACY: this payload renders on a public screen. Operator identity is
truncated to first name + last initial (``operator_name``) by the service —
never widen it to full names / employee ids without a privacy review.
"""

from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel


class WallboardActiveJob(BaseModel):
    wo_number: Optional[str] = None
    part_number: Optional[str] = None
    op_name: Optional[str] = None
    operator_name: Optional[str] = None  # "First L." — public-screen safe
    elapsed_minutes: int = 0
    qty_done: float = 0
    qty_target: float = 0


class WallboardDowntime(BaseModel):
    category: str
    since: Optional[datetime] = None
    minutes: int = 0


class WallboardWorkCenter(BaseModel):
    id: int
    code: Optional[str] = None
    name: str
    status: Optional[str] = None
    active_jobs: list[WallboardActiveJob] = []
    queued_count: int = 0
    blocked_count: int = 0
    down: Optional[WallboardDowntime] = None


class WallboardLateWorkOrder(BaseModel):
    wo_number: str
    part_number: Optional[str] = None
    due_date: Optional[date] = None
    days_late: int = 0
    status: Optional[str] = None


class WallboardBlockedWorkOrder(BaseModel):
    wo_number: str
    category: str
    age_hours: float = 0


class WallboardResponse(BaseModel):
    work_centers: list[WallboardWorkCenter]
    late_wos: list[WallboardLateWorkOrder]
    blocked_wos: list[WallboardBlockedWorkOrder]
    generated_at: datetime
