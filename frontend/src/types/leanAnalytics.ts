/**
 * Lean Phase 1 analytics contracts (issue #88) — typed mirrors of
 * backend/app/schemas/analytics.py:
 *
 *  - GET /analytics/flow         → FlowMetricsResponse
 *  - GET /analytics/wip-aging    → WipAgingResponse
 *  - GET /analytics/fpy          → FpyResponse
 *  - GET /analytics/scrap-pareto → ScrapParetoResponse
 *  - GET /analytics/adoption     → AdoptionMetricsResponse
 *  - GET /reports/ship-otd       → ShipOtdReportResponse
 *
 * Numeric fields are `number | null` where the backend deliberately returns
 * null for an uncomputable metric (empty denominator) — render "—"/"n/a",
 * never a fake 0 or 100. Datetimes arrive as UTC ISO strings; date-only
 * fields as YYYY-MM-DD (both safe through utils/centralTime.toDate).
 */

// ============ Flow (lead time / queue time / Little's Law / PCE) ============

export interface FlowWorkOrderDetail {
  work_order_id: number;
  work_order_number: string;
  part_number: string | null;
  customer_name: string | null;
  released_at: string | null;
  actual_end: string | null;
  first_ship_date: string | null;
  last_ship_date: string | null;
  lead_time_days: number | null;
  release_to_first_ship_days: number | null;
  release_to_last_ship_days: number | null;
  value_add_hours: number;
  pce_pct: number | null;
}

export interface QueueTimeByWorkCenter {
  work_center_id: number;
  work_center_code: string | null;
  work_center_name: string | null;
  avg_queue_hours: number | null;
  max_queue_hours: number | null;
  samples: number;
  /** Samples measured from operation_ready events (vs predecessor-end fallback). */
  from_ready_events: number;
}

export interface FlowSummary {
  work_orders_completed: number;
  avg_lead_time_days: number | null;
  median_lead_time_days: number | null;
  avg_release_to_last_ship_days: number | null;
  avg_queue_hours: number | null;
  avg_wip: number | null;
  daily_completion_rate: number | null;
  littles_law_throughput_days: number | null;
  avg_pce_pct: number | null;
  excluded_backfill_import_hours: number;
}

export interface FlowMetricsResponse {
  period_start: string;
  period_end: string;
  summary: FlowSummary;
  work_orders: FlowWorkOrderDetail[];
  queue_by_work_center: QueueTimeByWorkCenter[];
  generated_at: string;
}

// ============ WIP aging ============

export interface WipAgingItem {
  work_order_id: number;
  work_order_number: string;
  part_number: string | null;
  customer_name: string | null;
  status: string;
  priority: number | null;
  quantity_ordered: number;
  quantity_complete: number;
  released_at: string | null;
  days_since_release: number | null;
  current_operation_id: number | null;
  current_operation_number: string | null;
  current_operation_name: string | null;
  current_work_center_name: string | null;
  days_in_current_operation: number | null;
  due_date: string | null;
  /** Negative = past due. */
  days_to_due: number | null;
}

export interface WipAgingResponse {
  items: WipAgingItem[];
  total_open: number;
  generated_at: string;
}

// ============ FPY / RTY ============

export interface FpyGroup {
  /** part_number or work-center code. */
  key: string;
  name: string | null;
  operations: number;
  units_attempted: number;
  first_pass_units: number;
  fpy_pct: number | null;
  /** Per-part only; null on work-center rows (RTY is a full-route metric). */
  rty_pct: number | null;
  work_orders: number;
}

export interface FpyResponse {
  period_start: string;
  period_end: string;
  overall_fpy_pct: number | null;
  overall_rty_pct: number | null;
  by_part: FpyGroup[];
  by_work_center: FpyGroup[];
  generated_at: string;
}

// ============ Scrap Pareto ============

export interface ScrapParetoBucket {
  /** null = the 'unspecified' (uncoded) bucket. */
  scrap_reason_code_id: number | null;
  code: string;
  name: string | null;
  category: string | null;
  quantity: number;
  /** quantity × part.standard_cost where available — 0 when no cost is known. */
  cost: number;
  percentage: number;
  cumulative_pct: number;
}

export interface ScrapParetoResponse {
  period_start: string;
  period_end: string;
  total_quantity: number;
  total_cost: number;
  buckets: ScrapParetoBucket[];
  excluded_backfill_import_quantity: number;
  generated_at: string;
}

// ============ Adoption + hidden factory ============

export interface AdoptionWeek {
  week_start: string;
  operation_completions: number;
  live_completions: number;
  backfill_completions: number;
  unknown_completions: number;
  digital_completion_pct: number | null;
  clock_in_coverage_pct: number | null;
  time_entries: number;
  backfill_entries: number;
  backfill_rate_pct: number | null;
}

export interface MaintenanceMixMetrics {
  planned_count: number;
  reactive_count: number;
  planned_pct: number | null;
}

export interface WorkCenterReliability {
  work_center_id: number;
  work_center_code: string | null;
  work_center_name: string | null;
  unplanned_downtime_events: number;
  unplanned_downtime_hours: number;
  staffed_run_hours: number;
  mtbf_hours: number | null;
  mttr_hours: number | null;
}

export interface HiddenFactoryMetrics {
  rework_hours: number;
  total_labor_hours: number;
  rework_hours_pct: number | null;
  rework_quantity: number;
  total_quantity: number;
  rework_quantity_pct: number | null;
  maintenance: MaintenanceMixMetrics;
  reliability_by_work_center: WorkCenterReliability[];
  excluded_backfill_import_hours: number;
}

export interface AdoptionMetricsResponse {
  period_start: string;
  period_end: string;
  digital_completion_pct: number | null;
  clock_in_coverage_pct: number | null;
  backfill_rate_pct: number | null;
  live_completions: number;
  backfill_completions: number;
  unknown_completions: number;
  weekly: AdoptionWeek[];
  hidden_factory: HiddenFactoryMetrics;
  generated_at: string;
}

// ============ Ship-based OTD / OTIF report ============

export interface ShipOtdRow {
  work_order_id: number;
  work_order_number: string;
  customer_name: string | null;
  part_number: string | null;
  status: string;
  quantity_ordered: number;
  quantity_shipped: number;
  promise_source: 'must_ship_by' | 'due_date' | null;
  promise_date: string | null;
  first_ship_date: string | null;
  last_ship_date: string | null;
  full_ship_date: string | null;
  fully_shipped: boolean;
  /** null while open with the promise still in the future. */
  on_time: boolean | null;
  days_late: number | null;
}

export interface ShipOtdCustomerRollup {
  customer_name: string;
  work_orders: number;
  on_time: number;
  late: number;
  otd_pct: number | null;
  avg_days_late: number | null;
}

export interface PromiseHygieneRow {
  work_order_id: number;
  work_order_number: string;
  customer_name: string | null;
  status: string;
  quantity_ordered: number;
  quantity_shipped: number;
  last_ship_date: string | null;
}

export interface ShipOtdReportResponse {
  period_start: string;
  period_end: string;
  otd_ship_pct: number | null;
  otif_pct: number | null;
  rows: ShipOtdRow[];
  by_customer: ShipOtdCustomerRollup[];
  promise_hygiene: PromiseHygieneRow[];
  generated_at: string;
}

/** Shared period vocabulary for the Lean Phase 1 analytics endpoints. */
export type LeanAnalyticsPeriod = '7d' | '30d' | '90d' | 'ytd';
