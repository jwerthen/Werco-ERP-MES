"""Pydantic contracts for the shop-floor TV wallboard payload (A0.5).

PRIVACY: this payload renders on a public screen. Operator identity is
truncated to first name + last initial (``operator_name``) by the service —
never widen it to full names / employee ids without a privacy review.
"""

from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel

from app.schemas.base import UTCModel


class WallboardActiveJob(BaseModel):
    wo_number: Optional[str] = None
    part_number: Optional[str] = None
    op_name: Optional[str] = None
    operator_name: Optional[str] = None  # "First L." — public-screen safe
    elapsed_minutes: int = 0
    qty_done: float = 0
    qty_target: float = 0


class WallboardDowntime(UTCModel):
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


class WallboardLateWorkOrder(UTCModel):
    wo_number: str
    part_number: Optional[str] = None
    due_date: Optional[date] = None
    days_late: int = 0
    status: Optional[str] = None


class WallboardBlockedWorkOrder(BaseModel):
    wo_number: str
    category: str
    age_hours: float = 0


class WallboardKPIStrip(BaseModel):
    """Floor-visible trailing-30-day KPI strip (Lean Phase 1 / issue #88).

    Aggregate numbers only -- nothing operator-identifying (public screen).
    Percentages are 0-100; ``null`` = insufficient data in the window (the TV
    renders an em dash), never a fake 0/100. Values may be up to ~5 minutes
    stale (server-side TTL cache so the 30s poll doesn't recompute analytics).
    """

    otd_ship_pct_30d: Optional[float] = None  # ship-based OTD (full qty shipped on/before promise)
    fpy_pct_30d: Optional[float] = None  # overall first-pass yield across completed ops
    scrap_pct_30d: Optional[float] = None  # scrapped / (complete + scrapped) across completed ops
    open_wip_count: int = 0  # open released WOs (released / in-progress / on-hold)
    avg_wip_age_days: Optional[float] = None  # mean days since release of open WOs


class WallboardResponse(UTCModel):
    work_centers: list[WallboardWorkCenter]
    late_wos: list[WallboardLateWorkOrder]
    blocked_wos: list[WallboardBlockedWorkOrder]
    # Optional so pre-existing consumers/fixtures are unaffected; the live
    # builder populates it (null only if the KPI computation itself failed).
    kpi_strip: Optional[WallboardKPIStrip] = None
    generated_at: datetime
