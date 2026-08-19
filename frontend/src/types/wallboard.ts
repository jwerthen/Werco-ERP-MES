/**
 * Types for the A0.5 TV wallboard (GET /shop-floor/wallboard) and the
 * scoped display tokens that authenticate unattended screens
 * (POST/GET/DELETE /auth/display-token).
 */

export interface WallboardActiveJob {
  wo_number: string | null;
  part_number: string | null;
  op_name: string | null;
  /** Public-screen safe: "First L.". BACK-COMPAT alias of crew[0]. */
  operator_name: string | null;
  /** Crew-station grouping: all operators on this operation, "First L.", max 3. */
  crew?: string[];
  crew_count?: number;
  elapsed_minutes: number;
  qty_done: number;
  qty_target: number;
  /** Server-computed; replaces the capped lateWoNumbers client derivation. */
  is_late?: boolean;
}

export interface WallboardShipRow {
  wo_number: string;
  part_number: string | null;
  promise_date: string | null;
  qty_remaining: number;
}

export interface WallboardShip {
  due_today: number;
  shipped_today: number;
  due_this_week: number;
  due_today_rows: WallboardShipRow[];
  next_due_date: string | null;
  next_due_count: number;
}

export interface WallboardToday {
  ops_completed: number;
  pieces_completed: number;
  wos_completed: number;
  operators_on_clock: number;
  hours_logged: number | null;
  receipts: number;
  scrap_events: number;
}

export interface WallboardQuality {
  open_ncr_count: number;
  newest_ncr_age_days: number | null;
  wos_on_hold: number;
}

export interface WallboardDowntime {
  category: string;
  since: string | null;
  minutes: number;
}

export interface WallboardWorkCenter {
  id: number;
  code: string | null;
  name: string;
  status: string | null;
  active_jobs: WallboardActiveJob[];
  queued_count: number;
  blocked_count: number;
  down: WallboardDowntime | null;
}

export interface WallboardLateWorkOrder {
  wo_number: string;
  part_number: string | null;
  due_date: string | null;
  days_late: number;
  status: string | null;
}

export interface WallboardBlockedWorkOrder {
  wo_number: string;
  category: string;
  age_hours: number;
}

/**
 * Current operation of a work order on the job wall. NO customer data.
 * Every field is optional-safe: a sparse payload must never crash the board.
 */
export interface WallboardJobOp {
  sequence?: number | null;
  name?: string | null;
  work_center_code?: string | null;
  work_center_name?: string | null;
  /** ready | in_progress | pending */
  status?: string | null;
  /** Operation quantity_complete. */
  qty_done?: number;
  qty_target?: number;
  /** Public-screen safe crew names, "First L.", max 3. */
  crew?: string[];
  /** True headcount of open labor entries. */
  crew_count?: number;
  /** From earliest open labor clock_in on this op — ticks client-side. */
  elapsed_minutes?: number;
}

/**
 * One open work order tile on the job wall (owner feedback 2026-07-15: the
 * main wall shows WORK ORDERS with their CURRENT OPERATION, not machines).
 * NO dollars, NO notes.
 *
 * `customer_name` is GATED server-side: it is only populated for a principal
 * authorized to see it (an executive display token opted in via
 * `show_customer_names`, or a signed-in privileged office role). It is
 * null/undefined on every public shop-floor TV — the card falls back to the
 * op line in that case.
 */
export interface WallboardJob {
  wo_number: string;
  /** Unit # this work order builds — UNGATED (a bounded build number, not customer data). */
  unit_number?: string | null;
  part_number?: string | null;
  /** Gated — present only for authorized (executive) displays; else absent. */
  customer_name?: string | null;
  /**
   * The raw `WorkOrderStatus` value: `released` | `in_progress` | `on_hold`.
   *
   * LOAD-BEARING, not decorative. ON_HOLD joined the wall population on
   * 2026-08-19 and the backend added NO new wire key for it — the tile carries
   * the hold on THIS field, and `classifyJob` tests `status === 'on_hold'` to
   * pick the grey, non-pulsing HELD card state. Do not repopulate this with a
   * display label, and do not add a parallel `held` boolean expecting the
   * server to send one.
   */
  status?: string;
  /** WO-level quantities (the tile progress bar). */
  qty_complete?: number;
  qty_ordered?: number;
  /** coalesce(must_ship_by, due_date). */
  promise_date?: string | null;
  is_late?: boolean;
  /** 0 when not late. */
  days_late?: number;
  /** Any OPEN/ACKNOWLEDGED blocker on the WO. */
  blocked?: boolean;
  /** Current op's work center has an open DowntimeEvent. */
  down?: boolean;
  /** Current op has >=1 open labor entry. */
  running?: boolean;
  current_op?: WallboardJobOp | null;
  /** For "Op 3 of 5". */
  ops_completed?: number;
  ops_total?: number;
}

/**
 * @deprecated The PLANT 30d strip was removed from the board (owner feedback
 * 2026-07-15). The field stays on the schema as Optional for old bundles; new
 * backends always send null and the frontend no longer renders it.
 */
export interface WallboardKpiStrip {
  otd_ship_pct_30d: number | null;
  fpy_pct_30d: number | null;
  scrap_pct_30d: number | null;
  open_wip_count: number | null;
  avg_wip_age_days: number | null;
}

export interface WallboardResponse {
  work_centers: WallboardWorkCenter[];
  late_wos: WallboardLateWorkOrder[];
  blocked_wos: WallboardBlockedWorkOrder[];
  kpi_strip?: WallboardKpiStrip | null;
  /** True uncapped totals (dept-scoped under ?dept=). undefined = old backend
   *  → hero/rail fall back to list lengths (degraded but rendering). */
  late_total?: number | null;
  blocked_total?: number | null;
  down_total?: number | null;
  ship?: WallboardShip | null;
  today?: WallboardToday | null;
  quality?: WallboardQuality | null;
  /**
   * Priority-sorted open WOs (cap 24), server order — RELEASED / IN_PROGRESS /
   * ON_HOLD, with held work sorted to the BACK so it can never take an anchor
   * slot. The client NEVER re-sorts. Zone 2 pins ranks 1-4 to grid row 1 and
   * rotates the rest through grid rows 2-3 on a 22s dwell, so every delivered
   * job reaches the wall rather than the first 12 only.
   * undefined/null = old backend → the board renders its "BOARD DATA
   * UNAVAILABLE" grid zone.
   */
  jobs?: WallboardJob[] | null;
  /**
   * Uncapped open-WO count. It is the residue against `jobs` that the strip
   * reports, and the WORDING differs by state on purpose: a static board still
   * says "+N MORE WORK ORDERS IN QUEUE" (12 cells, the rest permanently hidden),
   * while a cycling board says "+N NOT ON BOARD" for the genuinely truncated
   * tail beyond the delivered cap — the "+N MORE ... IN QUEUE" phrase is never
   * emitted while cycling, so it keeps exactly one meaning across the screen.
   */
  jobs_total?: number | null;
  generated_at: string;
}

export interface DisplayToken {
  id: number;
  label: string;
  expires_at: string;
  revoked: boolean;
  revoked_at: string | null;
  created_by: number;
  created_at: string;
  /** Department the display is pinned to (wallboard ?dept=). Optional so a
   *  list payload from an older backend can't break the tab. */
  dept?: string | null;
  /** Whether this display reveals work-order customer names on the wallboard
   *  (default false = public-safe). Optional for old-backend back-compat. */
  show_customer_names?: boolean;
}

/**
 * POST /auth/display-token response — `token` AND `setup_code` are shown
 * exactly once. The setup code is the TV-friendly pairing path: enter it at
 * /tv within 15 minutes (single use) instead of typing the full #token= URL.
 */
export interface DisplayTokenIssued extends DisplayToken {
  token: string;
  setup_code: string;
  setup_code_expires_at: string;
  dept: string | null;
}

/** POST /auth/display-token/{id}/setup-code — re-issued pairing code (shown once). */
export interface SetupCodeResponse {
  id: number;
  label: string;
  dept: string | null;
  setup_code: string;
  setup_code_expires_at: string;
}

/**
 * POST /auth/display-token/claim response (PUBLIC endpoint — consumed by
 * services/wallboardClient, never the axios client). Any failure is a generic
 * 404: expired / used / unknown codes are indistinguishable by design.
 */
export interface DisplayCodeClaim {
  token: string;
  dept: string | null;
  label: string;
  expires_at: string;
}

export interface DisplayTokenCreateInput {
  label: string;
  expires_days?: number;
  dept?: string;
  /** Opt this display in to showing customer names on the wallboard. Default
   *  false — set true ONLY for a trusted executive-office TV. */
  show_customer_names?: boolean;
}
