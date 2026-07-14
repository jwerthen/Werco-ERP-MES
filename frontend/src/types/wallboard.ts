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
 * Lean Phase 1 (issue #88) KPI strip. Every figure is nullable (empty
 * denominator → null → render "—"), and the whole block is optional so a
 * board pointed at an older backend payload must not crash.
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
}

/** POST /auth/display-token response — `token` is shown exactly once. */
export interface DisplayTokenIssued extends DisplayToken {
  token: string;
}

export interface DisplayTokenCreateInput {
  label: string;
  expires_days?: number;
}
