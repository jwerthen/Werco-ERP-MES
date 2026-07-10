/**
 * Types for the A0.5 TV wallboard (GET /shop-floor/wallboard) and the
 * scoped display tokens that authenticate unattended screens
 * (POST/GET/DELETE /auth/display-token).
 */

export interface WallboardActiveJob {
  wo_number: string | null;
  part_number: string | null;
  op_name: string | null;
  /** Public-screen safe: first name + last initial ("Jon W."). */
  operator_name: string | null;
  elapsed_minutes: number;
  qty_done: number;
  qty_target: number;
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
