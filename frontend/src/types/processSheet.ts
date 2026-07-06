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
