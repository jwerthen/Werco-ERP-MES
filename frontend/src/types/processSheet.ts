/**
 * Process Sheets (engineering library) — typed contracts mirroring
 * backend/app/schemas/process_sheet.py EXACTLY. Do not invent fields.
 *
 * Lifecycle: draft -> released -> obsolete. Only DRAFT sheets are mutable
 * (header fields + step CRUD); anything else 409s server-side. Revisions are
 * separate rows sharing `sheet_number` (same pattern as routing revisions).
 */

export type ProcessSheetStatus = 'draft' | 'released' | 'obsolete';

/** StepType str-enum values (backend app/models/process_sheet.py). */
export type ProcessSheetStepType =
  | 'measurement'
  | 'checkbox'
  | 'list'
  | 'value'
  | 'photo'
  | 'file'
  | 'instruction';

/**
 * Per-type `config` JSON shape (validated in the backend service):
 *   measurement: { nominal, lsl, usl, unit, decimals }
 *   list:        { options: [] }
 *   photo/file:  { hint }
 * Kept as one optional-field bag since the wire type is a plain JSON object.
 */
export interface ProcessSheetStepConfig {
  nominal?: number;
  lsl?: number;
  usl?: number;
  unit?: string;
  decimals?: number;
  options?: string[];
  hint?: string;
}

export interface ProcessSheetStep {
  id: number;
  process_sheet_id: number;
  sequence: number;
  label: string;
  instruction_text: string | null;
  step_type: string; // ProcessSheetStepType values
  is_required: boolean;
  config: ProcessSheetStepConfig | null;
  requires_gauge: boolean;
  spc_characteristic_id: number | null;
  created_at: string;
  updated_at: string;
}

export interface ProcessSheet {
  id: number;
  sheet_number: string;
  title: string;
  description: string | null;
  revision: string;
  status: string; // ProcessSheetStatus values
  effective_date: string | null;
  obsolete_date: string | null;
  is_active: boolean;
  version: number;
  created_by: number | null;
  updated_by: number | null;
  created_at: string;
  updated_at: string;
  steps: ProcessSheetStep[];
}

/** GET /process-sheets/ row (ProcessSheetListResponse). */
export interface ProcessSheetListItem {
  id: number;
  sheet_number: string;
  title: string;
  revision: string;
  status: string;
  is_active: boolean;
  effective_date: string | null;
  step_count: number;
  created_at: string;
  updated_at: string;
}

export interface ProcessSheetListParams {
  status?: ProcessSheetStatus;
  search?: string;
  skip?: number;
  limit?: number;
}

export interface ProcessSheetCreateInput {
  title: string;
  description?: string | null;
}

export interface ProcessSheetUpdateInput {
  title?: string;
  description?: string | null;
}

/**
 * Step create payload (ProcessSheetStepCreate). The same shape is sent on
 * update — every field explicitly set — which is safe against the backend's
 * explicit-null guard because null is only ever sent for the NULLABLE columns
 * (instruction_text / config / spc_characteristic_id).
 */
export interface ProcessSheetStepInput {
  sequence: number;
  label: string;
  instruction_text?: string | null;
  step_type: ProcessSheetStepType;
  is_required: boolean;
  config?: ProcessSheetStepConfig | null;
  requires_gauge: boolean;
  spc_characteristic_id?: number | null;
}

// ---------------------------------------------------------------------------
// Shop-floor capture (PR 3) — mirrors the shop-floor step endpoints in
// backend/app/api/endpoints/shop_floor.py + schemas/process_sheet.py EXACTLY.
// ---------------------------------------------------------------------------

/**
 * Resolved gauge identity echoed on capture responses (GaugeRef, PR 4).
 * `equipment_code` is the gauge's MARKED identifier (Equipment.equipment_id —
 * the human-readable/barcode code the kiosk scans); `equipment_id` is the PK.
 */
export interface GaugeRef {
  equipment_id: number;
  equipment_code: string;
  name: string;
}

/** One warn-and-record qualification exception frozen on a record. */
export interface QualificationException {
  message?: string;
  [key: string]: unknown;
}

/**
 * Warn-and-record operator-qualification result frozen at capture time (PR 4).
 * `qualified === false` NEVER blocked the record — it is a supervision signal.
 */
export interface QualificationSnapshot {
  evaluated_at?: string;
  user_id?: number;
  work_center_id?: number;
  qualified: boolean;
  exceptions: QualificationException[];
}

/** One live (non-superseded) captured record (OperationStepRecordResponse). */
export interface OperationStepRecord {
  id: number;
  wo_operation_step_id: number;
  work_order_operation_id: number;
  serial_number: string | null;
  value_text: string | null;
  value_numeric: number | null;
  value_bool: boolean | null;
  /** Server-computed. NULL for types without a tolerance; false = honest "not done"/non-conforming. */
  is_conforming: boolean | null;
  recorded_by: number;
  recorded_by_name: string | null;
  /** UTC ISO — display via formatCentralDateTime. */
  recorded_at: string;
  source: string | null;
  equipment_id: number | null;
  /** PR 4: the resolved gauge used on this record; null when none was recorded. */
  gauge: GaugeRef | null;
  /** PR 4: warn-and-record qualification result at capture; null when not evaluable. */
  qualification_snapshot: QualificationSnapshot | null;
  attachment_document_id: number | null;
  superseded_by_id: number | null;
  supersede_reason: string | null;
  created_at: string;
}

/** Snapshot step + its live records and completeness state (OperationStepWithState). */
export interface OperationStepWithState {
  id: number;
  work_order_operation_id: number;
  source_sheet_id: number;
  source_sheet_revision: string;
  sequence: number;
  label: string;
  instruction_text: string | null;
  step_type: string; // ProcessSheetStepType values
  is_required: boolean;
  config: ProcessSheetStepConfig | null;
  requires_gauge: boolean;
  spc_characteristic_id: number | null;
  created_at: string;
  records: OperationStepRecord[];
  complete: boolean;
  missing_serials: string[];
}

/**
 * GET /shop-floor/operations/{id}/steps (OperationStepsViewResponse).
 * `completeness` is keyed by STRINGIFIED step id (JSON object keys), then by
 * serial; empty for non-serialized WOs (each step's `complete` flag carries
 * the state). `steps_total`/`steps_recorded` count REQUIRED steps only.
 */
export interface OperationStepsView {
  operation_id: number;
  work_order_id: number;
  work_order_number: string;
  operation_status: string;
  is_serialized: boolean;
  serial_numbers: string[];
  steps: OperationStepWithState[];
  steps_total: number;
  steps_recorded: number;
  completeness: Record<string, Record<string, boolean>>;
}

/**
 * POST .../steps/{step_id}/records payload. Send EXACTLY ONE type-shaped value
 * field, plus `serial_number` when the WO is serialized.
 *
 * `source` (PR 4) is the optional adoption-telemetry channel hint (TimeEntry
 * trust model): stored verbatim, NULL when omitted — EXCEPT a kiosk-scoped
 * badge token always records "kiosk" regardless of the hint. The logged-in
 * OperatorKiosk sends "kiosk" (exactly like clock-in); the crew station sends
 * nothing (its badge credential is authoritative).
 *
 * `equipment_code` (PR 4 addendum) is the gauge's SCANNED identifier — the
 * kiosk-preferred alternative to `equipment_id` (operators cannot browse
 * /equipment from the kiosk). Provide one or the other, never both (400);
 * unknown code 404s; a stale gauge 409s GAUGE_OUT_OF_CAL with no record row.
 */
export interface OperationStepRecordInput {
  serial_number?: string;
  value_numeric?: number;
  value_bool?: boolean;
  value_text?: string;
  equipment_id?: number;
  equipment_code?: string;
  attachment_document_id?: number;
  source?: string;
}

/** POST .../records/{record_id}/supersede payload — reason + the replacement value. */
export interface OperationStepSupersedeInput extends Omit<OperationStepRecordInput, 'serial_number'> {
  reason: string;
}

/**
 * POST /shop-floor/operations/{id}/steps/{step_id}/quality-hold body (PR 4).
 * The one-tap OOT escape hatch: `measured_value` is the REFUSED measurement
 * (never stored as a record — it lands on the NCR's actual_value). The server
 * VERIFIES the claim: an in-band value 409s `VALUE_IN_TOLERANCE` and a
 * snapshot config without numeric limits 400s — no NCR either way.
 *
 * The gauge used goes as `equipment_id` OR `equipment_code` (never both) —
 * resolved tenant-scoped WITHOUT calibration gating (the escape hatch must
 * never trap the operator behind a stale gauge). The server itself writes the
 * resolved gauge identity into the NCR description and the audit trail, so
 * `notes` stays pure operator notes.
 */
export interface QualityHoldInput {
  measured_value: number;
  serial_number?: string;
  notes?: string;
  equipment_id?: number;
  equipment_code?: string;
  source?: string;
}

/** 201 result of the quality-hold one-tap: NCR + blocker filed, op ON_HOLD. */
export interface QualityHoldResult {
  message: string;
  ncr_id: number;
  ncr_number: string;
  blocker_id: number;
  operation_id: number;
  operation_status: string;
  /** Open time entries the hold closed server-side (same as PUT .../hold). */
  closed_time_entry_ids: number[];
}

/** POST .../steps/{step_id}/attachment response (StepAttachmentResponse). */
export interface StepAttachmentResult {
  document_id: number;
  document_number: string;
  file_name: string | null;
  file_size: number;
  mime_type: string | null;
}

// ---------------------------------------------------------------------------
// FAI prefill (PR 4) — POST /quality/fai/{fai_id}/prefill-from-steps. Desktop
// only: the kiosk token fence keeps shop-floor tokens out of /quality.
// ---------------------------------------------------------------------------

/** One characteristic populated from a conforming measurement step record. */
export interface FAIPrefillEntry {
  char_number: number;
  characteristic: string;
  actual_value: string | null;
  measuring_device: string | null;
  wo_operation_step_id: number;
  record_id: number;
  serial_number: string | null;
}

/** One characteristic the prefill would not populate, and why. */
export interface FAIPrefillUnmatched {
  char_number: number;
  characteristic: string;
  reason: string;
}

/** FAIPrefillResponse — what was filled vs. reported. */
export interface FAIPrefillResult {
  fai_id: number;
  fai_number: string;
  work_order_id: number;
  prefilled: FAIPrefillEntry[];
  unmatched: FAIPrefillUnmatched[];
  prefilled_count: number;
  unmatched_count: number;
}

/** One entry of the 409 STEPS_INCOMPLETE `detail.missing` array. */
export interface MissingStepInfo {
  step_id: number;
  label: string;
  serials: string[];
}

/** One bypassed step on a force-completed WO (steps_bypassed.steps[] entry). */
export interface BypassedStepInfo {
  /** Operation identifier string (e.g. "OP10") the bypassed step belonged to. */
  operation: string;
  step_id: number;
  label: string;
  serials: string[];
}

/**
 * `steps_bypassed` on a successful POST /work-orders/{id}/complete response:
 * an authorized user force-completed the WO with required step records still
 * missing — a deliberate, audited override (the action SUCCEEDED by design).
 * Null / absent when nothing was bypassed. `truncated` means the steps list
 * was capped server-side and `count` exceeds the entries shown.
 */
export interface StepsBypassedInfo {
  count: number;
  steps: BypassedStepInfo[];
  truncated: boolean;
}
