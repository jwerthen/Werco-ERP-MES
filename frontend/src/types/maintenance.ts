/**
 * Preventive maintenance wire shapes (backend/app/api/endpoints/maintenance.py and
 * backend/app/schemas/maintenance.py).
 *
 * A maintenance record targets EXACTLY ONE thing: a production work center or a
 * non-production equipment asset (forklift, compressor, manual mill). Every schedule /
 * work order / log row names its target through `target_type` + `target_code` +
 * `target_name`, so no screen has to branch on which id is set.
 */

export type MaintenanceTargetType = 'work_center' | 'asset';

export type MaintenanceType = 'preventive' | 'corrective' | 'predictive' | 'emergency';
export type MaintenancePriority = 'critical' | 'high' | 'medium' | 'low';
export type MaintenanceStatus = 'scheduled' | 'in_progress' | 'completed' | 'overdue' | 'cancelled' | 'on_hold';
export type MaintenanceFrequency =
  | 'daily'
  | 'weekly'
  | 'biweekly'
  | 'monthly'
  | 'quarterly'
  | 'semi_annual'
  | 'annual'
  | 'custom';

export interface MaintenanceTargetFields {
  work_center_id: number | null;
  work_center_name?: string | null;
  asset_id: number | null;
  asset_tag?: string | null;
  asset_name?: string | null;
  target_type: MaintenanceTargetType;
  target_code?: string | null;
  target_name?: string | null;
}

export interface MaintenanceSchedule extends MaintenanceTargetFields {
  id: number;
  name: string;
  description?: string | null;
  maintenance_type: MaintenanceType | string;
  frequency: MaintenanceFrequency | string;
  frequency_days?: number | null;
  estimated_duration_hours: number;
  priority: MaintenancePriority | string;
  /** Raw stored text (a JSON array of strings; legacy rows may be one item per line). */
  checklist?: string | null;
  /** The checklist parsed tolerantly server-side. */
  checklist_items?: string[];
  requires_shutdown: boolean;
  assigned_to?: number | null;
  last_completed_date?: string | null;
  next_due_date?: string | null;
  is_active: boolean;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface MaintenanceWorkOrder extends MaintenanceTargetFields {
  id: number;
  schedule_id?: number | null;
  wo_number: string;
  maintenance_type: MaintenanceType | string;
  priority: MaintenancePriority | string;
  status: MaintenanceStatus | string;
  title: string;
  description?: string | null;
  checklist_results?: string | null;
  /** The PM schedule's checklist steps, which completion also judges (absent from an older API). */
  schedule_checklist_items?: string[];
  scheduled_date?: string | null;
  due_date?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
  actual_duration_hours?: number | null;
  requires_shutdown: boolean;
  downtime_minutes?: number | null;
  parts_used?: string | null;
  labor_cost?: number | null;
  parts_cost?: number | null;
  total_cost?: number | null;
  assigned_to?: number | null;
  completed_by?: number | null;
  notes?: string | null;
  findings?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface MaintenanceWorkCenterMetric {
  work_center_id: number;
  work_center_name: string;
  work_center_code: string;
  wo_count: number;
  mtbf_hours: number;
  mttr_hours: number;
  total_downtime_minutes: number;
  total_cost: number;
}

/**
 * POST /maintenance/work-orders/{id}/complete: the completed work order, plus the PM schedule's
 * new next due date when completing it advanced one (`null` otherwise; absent from an older API).
 */
export interface MaintenanceWorkOrderCompleted extends MaintenanceWorkOrder {
  schedule_next_due_date?: string | null;
}

/** PUT /maintenance/schedules/{id}: only the fields sent change. The target never does. */
export interface MaintenanceScheduleUpdate {
  next_due_date?: string;
  frequency?: MaintenanceFrequency;
  frequency_days?: number | null;
  priority?: MaintenancePriority;
  estimated_duration_hours?: number;
  requires_shutdown?: boolean;
  checklist?: string | null;
}

// ── Maintenance log (history) ──

/** What a person (or an API token) may file as a log entry; `POST /maintenance/log` refuses anything else (422). */
export type MaintenanceLogEntryType = 'inspection' | 'repair' | 'part_replacement' | 'observation' | 'note';

/** Entries only the work order's Start and Complete verbs write. */
export type MaintenanceServerLogEventType = 'started' | 'completed';

/**
 * One maintenance log entry (`_serialize_log`). `event_type` is a filed type, a server-written
 * `started` / `completed`, or -- on a legacy row -- free text. `event_date` is when it happened (UTC `Z`).
 */
export interface MaintenanceLogEntry extends MaintenanceTargetFields {
  id: number;
  maintenance_wo_id: number | null;
  event_type: MaintenanceLogEntryType | MaintenanceServerLogEventType | string;
  description: string | null;
  performed_by: number | null;
  /** The performer's name, read inside the tenant; null for a legacy row whose user it cannot name. */
  performed_by_name: string | null;
  event_date: string | null;
  cost: number | null;
  created_at: string | null;
}

/** GET /maintenance/work-orders/{id}/logs: the work order's entries, newest first. */
export interface MaintenanceWorkOrderLogs {
  maintenance_wo_id: number;
  wo_number: string;
  logs: MaintenanceLogEntry[];
}

/** GET /maintenance/history/{work_center_id}: a work center's entries, newest first. */
export interface MaintenanceWorkCenterHistory {
  work_center_id: number;
  work_center_name: string;
  logs: MaintenanceLogEntry[];
}

/** GET /maintenance/assets/{id}/history: an equipment asset's entries, newest first. */
export interface MaintenanceAssetHistory {
  asset_id: number;
  asset_tag: string;
  asset_name: string;
  logs: MaintenanceLogEntry[];
}

/**
 * POST /maintenance/log, filed against a maintenance work order. No target is sent: the server
 * takes the work order's (a different one is 409). `event_date` (UTC) defaults to now on the
 * server and may be at most 30 days back, never in the future (422); `cost` is 0 or more.
 */
export interface MaintenanceLogCreate {
  maintenance_wo_id: number;
  event_type: MaintenanceLogEntryType;
  description: string;
  cost?: number;
  event_date?: string;
}

/** GET /maintenance/dashboard. MTBF / MTTR are work centers only (assets never count). */
export interface MaintenanceDashboard {
  /** Maintenance WORK ORDERS (scheduled / overdue) whose due date has passed. */
  overdue_count: number;
  /** Maintenance WORK ORDERS (scheduled / overdue) due today through today + 7. */
  due_this_week: number;
  /** Active PM SCHEDULES past their next due date with no open work order (absent from an older API). */
  schedules_overdue?: number;
  /** Active PM SCHEDULES due today through today + 7 with no open work order (absent from an older API). */
  schedules_due_this_week?: number;
  completed_this_month: number;
  completion_rate: number;
  total_cost_month: number;
  in_progress: number;
  work_center_metrics: MaintenanceWorkCenterMetric[];
}

export interface MaintenanceAsset {
  id: number;
  asset_tag: string;
  name: string;
  asset_type?: string | null;
  manufacturer?: string | null;
  model?: string | null;
  serial_number?: string | null;
  location?: string | null;
  notes?: string | null;
  is_active: boolean;
  version: number;
  created_at?: string | null;
  updated_at?: string | null;
  active_schedule_count: number;
  open_work_order_count: number;
}

export interface MaintenanceAssetInput {
  asset_tag: string;
  name: string;
  asset_type?: string | null;
  manufacturer?: string | null;
  model?: string | null;
  serial_number?: string | null;
  location?: string | null;
  notes?: string | null;
}

/** PUT /maintenance/assets/{id}: only sent fields change; `version` is the CAS (409 if stale). */
export interface MaintenanceAssetUpdate extends Partial<MaintenanceAssetInput> {
  version: number;
  is_active?: boolean;
}

// ── PM schedule import (template `maintenance-schedules`, one row per task) ──

export type PmImportRowOutcome = 'create' | 'update' | 'unchanged' | 'skipped' | 'error';

export interface PmImportRowResult {
  row: number;
  target_type?: MaintenanceTargetType | null;
  target_code?: string | null;
  schedule_name?: string | null;
  task?: string | null;
  frequency?: string | null;
  outcome: PmImportRowOutcome | string;
  warnings: string[];
  error?: string | null;
}

export interface PmImportScheduleResult {
  schedule_id?: number | null;
  name: string;
  target_type: MaintenanceTargetType;
  target_id?: number | null;
  target_code: string;
  target_name?: string | null;
  frequency: string;
  frequency_days?: number | null;
  tasks: string[];
  next_due_date?: string | null;
  estimated_duration_hours?: number | null;
  requires_shutdown?: boolean | null;
  priority?: string | null;
  outcome: 'create' | 'update' | 'unchanged' | 'error' | string;
  changes: Record<string, { old: unknown; new: unknown }>;
  rows: number[];
}

export interface PmImportAssetResult {
  asset_id?: number | null;
  asset_tag: string;
  name: string;
  asset_type?: string | null;
  rows: number[];
}

export interface PmImportSummary {
  total_rows: number;
  task_rows: number;
  skipped_rows: number;
  error_count: number;
  warning_count: number;
  schedules_to_create: number;
  schedules_to_update: number;
  schedules_unchanged: number;
  assets_to_create: number;
}

/** Both POST /maintenance/schedules/import/preview and /commit answer with this. */
export interface PmImportResponse {
  filename?: string | null;
  source_hash: string;
  committed: boolean;
  summary: PmImportSummary;
  rows: PmImportRowResult[];
  schedules: PmImportScheduleResult[];
  assets_to_create: PmImportAssetResult[];
}
