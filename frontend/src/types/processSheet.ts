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
 * field, plus `serial_number` when the WO is serialized. Never send `source` —
 * the server derives the channel from the credential.
 */
export interface OperationStepRecordInput {
  serial_number?: string;
  value_numeric?: number;
  value_bool?: boolean;
  value_text?: string;
  attachment_document_id?: number;
}

/** POST .../records/{record_id}/supersede payload — reason + the replacement value. */
export interface OperationStepSupersedeInput extends Omit<OperationStepRecordInput, 'serial_number'> {
  reason: string;
}

/** POST .../steps/{step_id}/attachment response (StepAttachmentResponse). */
export interface StepAttachmentResult {
  document_id: number;
  document_number: string;
  file_name: string | null;
  file_size: number;
  mime_type: string | null;
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
