"""Pydantic contracts for the shop-floor TV wallboard payload (A0.5).

PRIVACY: this payload may render on a public screen. Operator identity is
truncated to first name + last initial (``operator_name`` / ``crew``) by the
service — never widen it to full names / employee ids without a privacy
review. The same discipline applies to the ship / today / quality blocks:
counts, ages, WO/part numbers and dates ONLY — no ship-to addresses, no dollar
figures, no NCR titles/descriptions.

The ONE gated exception is ``WallboardJob.customer_name``: it is populated only
for a principal explicitly authorized to see customer names (an executive
display token opted in via ``display_tokens.show_customer_names``, or a
signed-in privileged office role) and stays None on every public shop-floor TV.
See ``build_wallboard_payload(..., include_customer=...)``.

Back-compat: every field added after A0.5 v1 is optional/defaulted (the
``kpi_strip`` precedent) — an old backend → new TV renders ``—`` panels; a
new backend → old TV ignores unknown fields.
"""

from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel

from app.schemas.base import UTCModel


class WallboardActiveJob(BaseModel):
    wo_number: Optional[str] = None
    part_number: Optional[str] = None
    op_name: Optional[str] = None
    # BACK-COMPAT: kept, always = crew[0] (or None). "First L." only.
    operator_name: Optional[str] = None
    # Crew-station grouping: one row per OPERATION, not per time entry.
    # Each entry is operator_display_name() output ("First L."), max 3;
    # crew_count carries the true headcount for the "+N" suffix.
    crew: list[str] = []
    crew_count: int = 0
    elapsed_minutes: int = 0  # from the EARLIEST open clock_in of the crew
    qty_done: float = 0
    qty_target: float = 0
    # Server-computed lateness (kills the capped client-side derivation):
    # True when the parent WO's promise (coalesce(must_ship_by, due_date)) is
    # before today's Central date and the WO is not complete/closed/cancelled.
    is_late: bool = False


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
    # The WO's PROMISE date (coalesce(must_ship_by, due_date) — OTD precedence),
    # kept under the original field name for wire back-compat.
    due_date: Optional[date] = None
    days_late: int = 0
    status: Optional[str] = None


class WallboardBlockedWorkOrder(BaseModel):
    wo_number: str
    category: str
    age_hours: float = 0


class WallboardShipRow(BaseModel):
    """One WO due to ship. NO customer_name, NO ship_to_*, NO cost columns —
    the display token may drive a public shop-floor TV."""

    wo_number: str
    part_number: Optional[str] = None
    promise_date: Optional[date] = None  # coalesce(must_ship_by, due_date) — OTD precedence
    qty_remaining: float = 0


class WallboardShip(BaseModel):
    """Plant-wide ship status (Central-day window). Promise = must_ship_by || due_date,
    identical to AnalyticsService OTD semantics so this panel and the OTD KPI agree."""

    # ONE population — WOs promised (must_ship_by || due_date) today — so the
    # TV fraction "shipped_today / due_today" is coherent: the denominator is
    # every promise dated today, the numerator those already fully shipped.
    due_today: int = 0  # ALL WOs promised today (shipped or not)
    shipped_today: int = 0  # of those, fully shipped (cumulative-crossing rule)
    due_this_week: int = 0  # promised today..today+6, not fully shipped
    due_today_rows: list[WallboardShipRow] = []  # top 2 worst (largest qty_remaining)
    next_due_date: Optional[date] = None  # populated when due_today == 0
    next_due_count: int = 0


class WallboardToday(BaseModel):
    """Live today-so-far pulse. Central-midnight window (CENTRAL_TIME_ZONE day
    start — never naive date.today()). Aggregate counts only — no names, no
    per-person figures, no dollars."""

    ops_completed: int = 0
    pieces_completed: int = 0  # sum(TimeEntry.quantity_produced), RUN+REWORK,
    # provenance-excluded (BASELINE_EXCLUDED_SOURCES)
    wos_completed: int = 0
    operators_on_clock: int = 0  # distinct user_id, clock_out IS NULL (any entry type)
    hours_logged: Optional[float] = None  # closed durations + open elapsed; provenance-excluded
    receipts: int = 0  # POReceipt received_at within Central day
    scrap_events: int = 0  # TimeEntry qty_scrapped>0, provenance-excluded


class WallboardQuality(BaseModel):
    """Counts and ages ONLY — never NCR titles/descriptions/supplier/cost."""

    open_ncr_count: int = 0  # status not in (CLOSED, VOID)
    newest_ncr_age_days: Optional[int] = None
    wos_on_hold: int = 0  # WorkOrderStatus.ON_HOLD, is_deleted == False


class WallboardKPIStrip(BaseModel):
    """DEPRECATED (2026-07-15 Job Wall redesign — owner dropped the 30d strip).

    The server no longer computes this block; ``WallboardResponse.kpi_strip``
    is always ``None``. The class stays only so the field keeps its wire type
    for old TV bundles (which render an em-dash panel on ``null``).
    """

    otd_ship_pct_30d: Optional[float] = None  # ship-based OTD (full qty shipped on/before promise)
    fpy_pct_30d: Optional[float] = None  # overall first-pass yield across completed ops
    scrap_pct_30d: Optional[float] = None  # scrapped / (complete + scrapped) across completed ops
    open_wip_count: int = 0  # open released WOs (released / in-progress / on-hold)
    avg_wip_age_days: Optional[float] = None  # mean days since release of open WOs


class WallboardJobOp(BaseModel):
    """Current operation of a WO on the job wall. NO customer data."""

    sequence: Optional[int] = None
    name: Optional[str] = None
    work_center_code: Optional[str] = None
    work_center_name: Optional[str] = None
    status: Optional[str] = None  # ready | in_progress | pending
    qty_done: float = 0  # operation quantity_complete
    qty_target: float = 0  # operation_target_quantity(op, wo)
    crew: list[str] = []  # "First L." via operator_display_name, max 3
    crew_count: int = 0  # true headcount of open labor entries
    elapsed_minutes: int = 0  # from earliest open labor clock_in on this op


class WallboardJob(BaseModel):
    """One open work order tile. NO dollars, NO notes.

    ``customer_name`` is the ONE deliberate exception to the "no customer data
    on a public screen" rule, and it is GATED: the payload builder only
    populates it when the request principal is authorized to see it (an
    executive display token opted in via ``show_customer_names``, or a signed-in
    privileged office role). It stays None for every public shop-floor TV.
    """

    wo_number: str
    part_number: Optional[str] = None
    # Gated — populated only for an authorized (executive) principal; None on
    # public boards. See the class docstring and build_wallboard_payload.
    customer_name: Optional[str] = None
    status: str  # released | in_progress
    qty_complete: float = 0  # WO-level
    qty_ordered: float = 0
    promise_date: Optional[date] = None  # coalesce(must_ship_by, due_date)
    is_late: bool = False  # via the shared _late_wo_filters predicate
    days_late: int = 0  # 0 when not late
    blocked: bool = False  # any OPEN/ACKNOWLEDGED blocker on the WO
    down: bool = False  # current op's WC has an open DowntimeEvent
    running: bool = False  # current op has >=1 open labor entry
    current_op: Optional[WallboardJobOp] = None  # None when all ops complete
    ops_completed: int = 0  # for "Op 3 of 5"
    ops_total: int = 0


class WallboardResponse(UTCModel):
    work_centers: list[WallboardWorkCenter]
    # Server-side ranked (late: worst-first; blocked: oldest-first), capped at
    # 12, and DEPT-SCOPED when ``dept`` is passed (as are the totals below).
    late_wos: list[WallboardLateWorkOrder]
    blocked_wos: list[WallboardBlockedWorkOrder]
    # DEPRECATED (2026-07-15 Job Wall redesign): the trailing-30d KPI strip is
    # gone from the TV. The field stays for wire back-compat but the server no
    # longer computes it — ALWAYS None (old bundles render an em-dash panel).
    kpi_strip: Optional[WallboardKPIStrip] = None
    # Job Wall (owner feedback 2026-07-15): the main wall renders WORK ORDERS
    # with their current operation. Priority-sorted server-side (blocked/down,
    # then late worst-first, then running, then promise asc), capped at 24;
    # jobs_total carries the true uncapped (dept-scoped) count for "+N more".
    # Optional -> an old backend omits it and the TV falls back to the
    # machine wall.
    jobs: Optional[list[WallboardJob]] = None
    jobs_total: Optional[int] = None
    # True uncapped totals for the rail headlines / hero sentence.
    # Dept-scoped when ?dept= is set; None (not 0) when omitted by an old backend.
    late_total: Optional[int] = None
    blocked_total: Optional[int] = None
    down_total: Optional[int] = None
    # Plant-wide blocks, each independently best-effort (try/except like
    # kpi_strip: a failed block is None, never a failed payload).
    ship: Optional[WallboardShip] = None
    today: Optional[WallboardToday] = None
    quality: Optional[WallboardQuality] = None
    generated_at: datetime
