/**
 * SPC (Statistical Process Control) API contract types.
 *
 * These mirror `backend/app/api/endpoints/spc.py` EXACTLY. They exist because the
 * SPC page previously carried hand-written interfaces that disagreed with the
 * server on almost every field name (`nominal` vs `specification_nominal`, `cl`
 * vs `center_line`, `value` vs `measurement_value`, a flat measurement body vs
 * the `{measurements: [...]}` wrapper). With real return types on the client
 * methods, that class of drift becomes a compile error instead of a silent `[]`.
 *
 * ⚠️ Timestamps: the Pydantic-schema routes (`CharacteristicResponse`,
 * `MeasurementResponse`, `ControlLimitResponse`, `CapabilityResponse`) inherit bare
 * `BaseModel`, NOT the app's `UTCModel`, so their datetimes arrive WITHOUT a trailing
 * `Z`. Only the two hand-built dict routes (`chart-data.chart_points[].measured_at`
 * and `out-of-control.last_ooc`) run through `to_utc_iso`. Always parse/format SPC
 * timestamps with `utils/centralTime.ts` — its zone-less branch treats them as UTC;
 * a bare `new Date(x)` would read them as local time.
 */

/** The 7 values of the backend `ChartType` enum. An off-list value is a 500, not a 422. */
export const SPC_CHART_TYPES = [
  'xbar_r',
  'xbar_s',
  'individual_mr',
  'p_chart',
  'np_chart',
  'c_chart',
  'u_chart',
] as const;

export type SPCChartType = (typeof SPC_CHART_TYPES)[number];

export const SPC_CHART_TYPE_LABELS: Record<SPCChartType, string> = {
  xbar_r: 'X-bar & R',
  xbar_s: 'X-bar & S',
  individual_mr: 'Individual & MR',
  p_chart: 'P Chart',
  np_chart: 'NP Chart',
  c_chart: 'C Chart',
  u_chart: 'U Chart',
};

/**
 * `characteristic_type` is `String(50)` server-side with NO enum and NO validation,
 * yet the value is rendered verbatim into AS9100D compliance-evidence summaries
 * (`auto_evidence_service.py`). The client select is therefore the ONLY control that
 * exists — keep it closed, with no free-text fallback.
 */
export const SPC_CHARACTERISTIC_TYPES = [
  'dimensional',
  'weight',
  'force',
  'temperature',
  'visual',
] as const;

export type SPCCharacteristicType = (typeof SPC_CHARACTERISTIC_TYPES)[number];

/** GET /spc/characteristics[/{id}] — one row. Bare array on the list route (no envelope). */
export interface SPCCharacteristic {
  id: number;
  name: string;
  part_id: number;
  characteristic_type: string;
  unit_of_measure: string | null;
  /** NOT `nominal`. */
  specification_nominal: number | null;
  /** NOT `usl`. */
  specification_usl: number | null;
  /** NOT `lsl`. */
  specification_lsl: number | null;
  chart_type: SPCChartType;
  subgroup_size: number;
  work_center_id: number | null;
  operation_number: number | null;
  is_active: boolean;
  is_critical: boolean;
  notes: string | null;
  /** No trailing `Z` — parse via centralTime `toDate`. */
  created_at: string;
  /** No trailing `Z` — parse via centralTime `toDate`. */
  updated_at: string | null;
}

/** POST /spc/characteristics body. Unknown keys are SILENTLY DROPPED (pydantic extra='ignore'). */
export interface SPCCharacteristicCreate {
  name: string;
  part_id: number;
  characteristic_type: string;
  unit_of_measure?: string | null;
  specification_nominal?: number | null;
  specification_usl?: number | null;
  specification_lsl?: number | null;
  /** Always send a valid value — an off-list string OR an explicit null both 500. */
  chart_type?: SPCChartType;
  subgroup_size?: number;
  work_center_id?: number | null;
  operation_number?: number | null;
  is_critical?: boolean;
  notes?: string | null;
}

/** PUT /spc/characteristics/{id} body — every create field EXCEPT `part_id`, plus `is_active`. */
export type SPCCharacteristicUpdate = Partial<Omit<SPCCharacteristicCreate, 'part_id'>> & {
  is_active?: boolean;
};

/** One item inside the POST /spc/measurements wrapper. */
export interface SPCMeasurementCreate {
  characteristic_id: number;
  /** Rational-subgroup identifier. Ordinal; gaps are allowed. */
  subgroup_number: number;
  /** NOT `value`. */
  measurement_value: number;
  /** Position within the subgroup, 1..subgroup_size. */
  sample_number: number;
  work_order_id?: number | null;
  lot_number?: string | null;
  serial_number?: string | null;
  notes?: string | null;
}

/**
 * POST /spc/measurements body — a WRAPPER. A flat `MeasurementCreate` 422s with
 * `{"loc":["measurements"],"msg":"Field required"}`.
 *
 * `measured_by` is deliberately absent: the server stamps `measured_by=current_user.id`
 * and silently discards any client-sent value.
 */
export interface SPCMeasurementBatchCreate {
  measurements: SPCMeasurementCreate[];
}

/** GET /spc/measurements/{id} and the POST /spc/measurements response. Bare array. */
export interface SPCMeasurement {
  id: number;
  characteristic_id: number;
  subgroup_number: number;
  /** NOT `value`. */
  measurement_value: number;
  sample_number: number;
  /** No trailing `Z`. */
  measured_at: string;
  /** A USER ID, not a name. No SPC route joins `users`; do not attempt a lookup. */
  measured_by: number | null;
  work_order_id: number | null;
  lot_number: string | null;
  serial_number: string | null;
  /** As of the LAST control-limit recalculation — not recomputed on read. */
  is_out_of_control: boolean;
  /** Comma-joined, e.g. "Rule1,Rule2". */
  violation_rules: string | null;
  notes: string | null;
}

/** GET /spc/control-limits/{id} (nullable) and POST .../calculate (non-null). */
export interface SPCControlLimit {
  id: number;
  characteristic_id: number;
  /** No trailing `Z`. */
  calculation_date: string;
  ucl: number;
  lcl: number;
  /** NOT `cl`. */
  center_line: number;
  ucl_range: number | null;
  lcl_range: number | null;
  center_line_range: number | null;
  /** Number of SUBGROUPS used. */
  sample_count: number;
  is_current: boolean;
  notes: string | null;
}

/** GET /spc/capability/{id} (nullable) and POST /spc/capability-study/{id} (non-null). */
export interface SPCCapability {
  id: number;
  characteristic_id: number;
  /** No trailing `Z`. */
  study_date: string;
  /** Individual measurements, not subgroups. */
  sample_count: number;
  mean: number;
  std_dev: number;
  /** All four indices are nullable — `null >= 1.33` is false, so guard before styling. */
  cp: number | null;
  cpk: number | null;
  /** Backend computes this as `cp` (not a true overall-sigma Pp). */
  pp: number | null;
  /** Backend computes this as `cpk`. */
  ppk: number | null;
  within_spec_pct: number | null;
  is_capable: boolean;
  notes: string | null;
}

/** One plotted subgroup from GET /spc/chart-data/{id}. */
export interface SPCChartPoint {
  /** X axis. */
  subgroup_number: number;
  /** X-bar for the subgroup (the individual reading when `sample_count === 1`). */
  mean: number;
  /** R for the subgroup; 0 when `sample_count === 1`. */
  range: number;
  sample_count: number;
  /** OR across the subgroup's stored flags. */
  is_out_of_control: boolean;
  /** Deduped rule codes; UNORDERED (built from a set). */
  violations: string[];
  /** ISO WITH trailing `Z` — first sample's timestamp. */
  measured_at: string | null;
}

/** GET /spc/chart-data/{id} — an OBJECT, not an array. */
export interface SPCChartData {
  characteristic: {
    id: number;
    name: string;
    chart_type: SPCChartType;
    subgroup_size: number;
    specification_nominal: number | null;
    specification_usl: number | null;
    specification_lsl: number | null;
    unit_of_measure: string | null;
  };
  /** Ascending by `subgroup_number`; windowed to the numerically-highest N. */
  chart_points: SPCChartPoint[];
  /** The current `is_current` limits, or null until limits have been calculated. */
  control_limits: {
    ucl: number;
    lcl: number;
    center_line: number;
    ucl_range: number | null;
    lcl_range: number | null;
    center_line_range: number | null;
  } | null;
}

/** GET /spc/out-of-control — bare array. */
export interface SPCOutOfControlAlert {
  characteristic_id: number;
  characteristic_name: string;
  part_id: number;
  is_critical: boolean;
  /** OOC MEASUREMENT rows, not subgroups. */
  ooc_count: number;
  /** ISO WITH trailing `Z`. */
  last_ooc: string;
}

/** One violating subgroup. No `id`, no timestamp, no per-measurement value. */
export interface SPCViolation {
  subgroup_number: number | null;
  subgroup_mean: number | null;
  /** e.g. ["Rule1","Rule2"] */
  rules_violated: string[];
}

/**
 * GET /spc/violations/{id} — an OBJECT with two shapes. When no current control
 * limits exist the server returns ONLY `{violations: [], message: "..."}`, so every
 * other key is optional here.
 */
export interface SPCViolationsResponse {
  violations: SPCViolation[];
  /** Present ONLY on the no-control-limits shape. */
  message?: string;
  characteristic_id?: number;
  characteristic_name?: string;
  control_limits?: { ucl: number; lcl: number; center_line: number };
  total_subgroups?: number;
  total_violations?: number;
}

/** Western Electric rules as implemented in `check_western_electric_rules`. */
export const SPC_VIOLATION_RULE_LABELS: Record<string, string> = {
  Rule1: 'One point beyond 3σ (outside a control limit)',
  Rule2: '2 of 3 consecutive points beyond 2σ on the same side',
  Rule3: '4 of 5 consecutive points beyond 1σ on the same side',
  Rule4: '8 consecutive points on the same side of the center line',
};

/** One row of the dashboard's "needs attention" list. */
export interface SPCDashboardAttentionItem {
  /** Characteristic id. */
  id: number;
  name: string;
  part_id: number;
  is_critical: boolean;
  cpk: number | null;
  has_ooc: boolean;
}

/**
 * GET /spc/dashboard. There is NO `characteristics_monitored`, NO `avg_cpk`, and
 * NO `measurements_today` — the page's old interface was wrong on 3 of 4 fields.
 */
export interface SPCDashboard {
  /** Active characteristics only. */
  total_characteristics: number;
  /** DISTINCT characteristics with any OOC measurement. */
  out_of_control_count: number;
  /** Null when no capability studies exist. */
  average_cpk: number | null;
  /** Latest-study cpk < 1.33. */
  characteristics_below_cpk_threshold: number;
  /** Unstable ordering (iterates a set) — sort client-side if order matters. */
  attention_needed: SPCDashboardAttentionItem[];
}
