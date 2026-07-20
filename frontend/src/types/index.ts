export interface User {
  id: number;
  version: number;  // For optimistic locking
  employee_id: string;
  email: string;
  first_name: string;
  last_name: string;
  role: UserRole;
  department?: string;
  is_active: boolean;
  is_superuser: boolean;
  company_id?: number;
  company_name?: string;
  created_at: string;
  updated_at: string;
}

export type UserRole = 'platform_admin' | 'admin' | 'manager' | 'supervisor' | 'operator' | 'quality' | 'shipping' | 'viewer';

export interface Company {
  id: number;
  name: string;
  slug: string;
  logo_url?: string;
  is_active: boolean;
  parent_company_id?: number;
  timezone?: string;
  address?: string;
  phone?: string;
  website?: string;
  user_count?: number;
  active_work_orders?: number;
  /**
   * Per-company AI egress kill switch. When false, no document content leaves
   * the system boundary to the Anthropic AI provider and AI-backed extraction /
   * copilot / NL-search features degrade gracefully. CUI/compliance control;
   * defaults OFF for new companies (existing companies grandfathered ON).
   */
  allow_ai_egress: boolean;
  created_at?: string;
  updated_at?: string;
}

export interface WorkCenter {
  id: number;
  version: number;  // For optimistic locking
  code: string;
  name: string;
  work_center_type: WorkCenterType;
  description?: string;
  hourly_rate: number;
  capacity_hours_per_day: number;
  efficiency_factor: number;
  availability_rate?: number;
  is_active: boolean;
  current_status: string;
  building?: string;
  area?: string;
  created_at: string;
  updated_at: string;
}

export type WorkCenterType = string;

export interface Part {
  id: number;
  version: number;  // For optimistic locking
  part_number: string;
  revision: string;
  name: string;
  description?: string;
  part_type: PartType;
  unit_of_measure: string;
  standard_cost: number;
  is_critical: boolean;
  requires_inspection: boolean;
  is_active: boolean;
  status: string;
  customer_name?: string;
  customer_part_number?: string;
  drawing_number?: string;
  created_at: string;
  updated_at: string;
}

export type PartType = 'manufactured' | 'purchased' | 'assembly' | 'raw_material' | 'hardware' | 'consumable';

export interface WorkOrder {
  id: number;
  version: number;  // For optimistic locking
  work_order_number: string;
  /** NULL only for standalone laser-cutting (nest package) work orders. */
  part_id: number | null;
  parent_work_order_id?: number;
  work_order_type: string;
  quantity_ordered: number;
  quantity_complete: number;
  quantity_scrapped: number;
  status: WorkOrderStatus;
  priority: number;
  scheduled_start?: string;
  scheduled_end?: string;
  actual_start?: string;
  actual_end?: string;
  due_date?: string;
  customer_name?: string;
  customer_po?: string;
  notes?: string;
  special_instructions?: string;
  estimated_hours: number;
  actual_hours: number;
  operation_count?: number;
  operations_complete?: number;
  operation_progress_percent?: number;
  created_at: string;
  updated_at: string;
  operations: WorkOrderOperation[];
}

export type WorkOrderStatus = 'draft' | 'released' | 'in_progress' | 'on_hold' | 'complete' | 'closed' | 'cancelled';

export interface WorkOrderOperation {
  id: number;
  version: number;  // For optimistic locking
  work_order_id: number;
  work_center_id: number;
  sequence: number;
  operation_number?: string;
  name: string;
  description?: string;
  setup_instructions?: string;
  run_instructions?: string;
  setup_time_hours: number;
  run_time_hours: number;
  run_time_per_piece: number;
  actual_setup_hours: number;
  actual_run_hours: number;
  estimated_hours?: number;
  actual_hours?: number;
  work_center_name?: string;
  status: OperationStatus;
  quantity_complete: number;
  quantity_scrapped: number;
  requires_inspection: boolean;
  inspection_complete: boolean;
  scheduled_start?: string;
  scheduled_end?: string;
  actual_start?: string;
  actual_end?: string;
  created_at: string;
  updated_at: string;
  // Component tracking for assembly WOs
  component_part_id?: number;
  component_part_number?: string;
  component_part_name?: string;
  component_quantity?: number;
  operation_group?: string;
  started_by?: number;
  completed_by?: number;
  laser_nest?: LaserNestInfo | null;
}

export type OperationStatus = 'pending' | 'ready' | 'in_progress' | 'complete' | 'on_hold';

export interface LaserNestInfo {
  id: number;
  nest_name: string;
  // Nullable: manually-keyed nests have no uploaded CNC file.
  cnc_file_name?: string | null;
  cnc_file_path?: string | null;
  // Operator-/machine-facing program number (manual + imported nests).
  cnc_number?: string | null;
  planned_runs: number;
  completed_runs: number;
  remaining_runs: number;
  material?: string | null;
  thickness?: string | null;
  sheet_size?: string | null;
  // Optional attached reference PDF (served inline via GET /laser-nests/{id}/document).
  document_id?: number | null;
  has_document?: boolean;
  document_file_name?: string | null;
}

/**
 * Compact response for the manual-create / patch / attach-document /
 * detach-document endpoints. Carries the backing operation id + status so the
 * UI can immediately render the nest as a clock-in-able operation.
 */
export interface LaserNestManualResponse {
  id: number;
  nest_name: string;
  cnc_number?: string | null;
  planned_runs: number;
  completed_runs: number;
  remaining_runs: number;
  material?: string | null;
  thickness?: string | null;
  sheet_size?: string | null;
  work_order_operation_id?: number | null;
  operation_status?: OperationStatus | null;
  document_id?: number | null;
  has_document?: boolean;
  document_file_name?: string | null;
}

export interface LaserNestManualInput {
  cnc_number: string;
  planned_runs: number;
  nest_name?: string;
  material?: string;
  thickness?: string;
  sheet_size?: string;
}

export type LaserNestUpdateInput = Partial<LaserNestManualInput>;

/** Per-field confidence the extraction pipeline reports for a nest PDF. */
export type LaserNestExtractionConfidence = 'high' | 'medium' | 'low';

/** The extracted fields that carry a per-field confidence from the two-pass read. */
export type LaserNestConfidenceField = 'cnc_number' | 'material' | 'thickness' | 'sheet_size' | 'planned_runs';

/** Per-field merged confidence map for one preview row (PDF uploads only). */
export type LaserNestFieldConfidence = Partial<Record<LaserNestConfidenceField, LaserNestExtractionConfidence>>;

/**
 * Result of `POST /laser-nests/extract` — AI (or filename-fallback) read of a
 * single nest report PDF. Every value is nullable: the model returns what it
 * could read and leaves the rest null. `source` distinguishes a full AI read
 * from the filename-only fallback (which only recovers the CNC number).
 */
export interface LaserNestPdfExtraction {
  cnc_number: string | null;
  material: string | null;
  thickness: string | null;
  sheet_size: string | null;
  planned_runs: number | null;
  confidence: LaserNestExtractionConfidence | null;
  source: 'ai' | 'filename';
  warning: string | null;
}

/**
 * One row of a batch `laser-nest-packages/preview`. Carries the existing
 * CNC-program fields plus the new AI-extraction fields: `source_file` (the
 * PDF/CNC file's relative path within the ZIP — the key the import step matches
 * rows back to PDFs by), `cnc_number`, and `confidence`. PDF rows populate
 * `cnc_number`; CNC-program rows populate `cnc_file_name`.
 */
export interface LaserNestPreviewRow {
  source_file: string;
  nest_name: string;
  cnc_file_name?: string | null;
  cnc_number?: string | null;
  planned_runs: number;
  material?: string | null;
  thickness?: string | null;
  sheet_size?: string | null;
  confidence?: LaserNestExtractionConfidence | null;
  /** 1-based page numbers of this nest within an uploaded PDF (null for ZIP/CNC packages). */
  source_pages?: number[] | null;
  /** Per-field merged confidence from the two-pass extraction (PDF uploads). */
  field_confidence?: LaserNestFieldConfidence | null;
  /** Per-row extraction warning (e.g. verification pass skipped). */
  warning?: string | null;
  /** How many AI passes ran for this row (1 or 2). */
  passes?: number | null;
}

export interface LaserNestPackagePreview {
  package_name: string;
  nest_count: number;
  total_planned_runs: number;
  nests: LaserNestPreviewRow[];
  /** Total page count when the upload was a bare (single/multi-page) PDF. */
  source_page_count?: number | null;
  /** Pages the AI segmentation classified as non-nest (cover/summary). */
  skipped_pages?: number[] | null;
  /** Set when segmentation degraded to one-page-per-nest. */
  segmentation_warning?: string | null;
}

/**
 * One confirmed row sent back to `laser-nest-packages/import`. `source_file`
 * lets the backend match the row to its PDF bytes in the re-sent ZIP without a
 * second AI call. `planned_runs` stays an integer >= 1.
 */
export interface LaserNestImportRow {
  source_file: string;
  cnc_number: string;
  nest_name: string;
  planned_runs: number;
  material: string | null;
  thickness: string | null;
  sheet_size: string | null;
  /**
   * For PDF uploads: MUST be echoed back verbatim from the preview row — the
   * backend re-splits the re-sent PDF by these pages and 400s on a mismatch.
   * Omit for ZIP/CNC packages.
   */
  source_pages?: number[] | null;
  /**
   * Per-nest work-center override. Omit to fall back to the package-level
   * pick (or the server's auto-detect when no package pick was made).
   */
  work_center_id?: number | null;
}

/**
 * Result of a nest-package import (parented or standalone). `child_work_order`
 * is the laser-cutting WO the nests landed on: the auto-created child under an
 * assembly WO, the target WO itself when it is already laser_cutting, or — for
 * the standalone import — a fresh RELEASED laser WO with no parent and no part
 * whose `quantity_ordered` is the total planned sheet runs.
 */
export interface LaserNestPackageImportResult {
  package?: LaserNestPackagePreview;
  child_work_order?: {
    id: number;
    work_order_number: string;
  } | null;
}

export interface WorkOrderSummary {
  id: number;
  work_order_number: string;
  /** NULL only for standalone laser-cutting (nest package) work orders. */
  part_id: number | null;
  parent_work_order_id?: number;
  work_order_type: string;
  part_number?: string | null;
  part_name?: string | null;
  part_type?: string | null;
  status: WorkOrderStatus;
  priority: number;
  quantity_ordered: number;
  quantity_complete: number;
  operation_count?: number;
  operations_complete?: number;
  operation_progress_percent?: number;
  due_date?: string;
  customer_name?: string;
  current_operation?: string;
}

export type TimeEntryType = 'setup' | 'run' | 'rework' | 'inspection' | 'downtime' | 'break';

export interface DashboardData {
  summary: {
    active_work_orders: number;
    due_today: number;
    overdue: number;
    signed_in_users: number;
    checked_in_users: number;
    idle_signed_in_users: number;
    completed_today?: number;
  };
  work_centers: WorkCenterStatus[];
  signed_in_users: SignedInUserStatus[];
  active_assignments: ActiveAssignment[];
  recent_completions: {
    work_order_number?: string;
    operation_name?: string;
    work_center_name?: string;
    operator_name?: string;
    completed_at?: string;
    quantity_complete: number;
  }[];
}

export interface WorkCenterStatus {
  id: number;
  code: string;
  name: string;
  type: WorkCenterType;
  status: string;
  active_operations: number;
  queued_operations: number;
  active_people_count: number;
  active_people: {
    user_id: number;
    name: string;
    employee_id: string;
    work_order_number: string;
    operation_name: string;
    clock_in: string;
  }[];
}

export interface SignedInUserStatus {
  id: number;
  employee_id: string;
  name: string;
  role: UserRole;
  department?: string;
  connected_since?: string;
  has_active_job: boolean;
  active_job_count: number;
  active_work_centers: string[];
  active_work_orders: string[];
}

export interface ActiveAssignment {
  time_entry_id: number;
  clock_in: string;
  entry_type: TimeEntryType;
  user: {
    id: number;
    employee_id: string;
    name: string;
    /** Short display form ("First L.") supplied by the backend; fall back to deriving from `name`. */
    display_name?: string | null;
    role: UserRole;
    department?: string;
  };
  work_order: {
    id: number;
    work_order_number: string;
    status: WorkOrderStatus;
    part_number?: string;
    part_name?: string;
    customer_name?: string;
    priority?: number;
    due_date?: string;
    quantity_ordered?: number;
    quantity_complete?: number;
  };
  operation: {
    id: number;
    operation_number?: string;
    name: string;
    status: OperationStatus;
    sequence?: number;
    quantity_complete?: number;
    quantity_scrapped?: number;
  };
  work_center: {
    id: number;
    code?: string;
    name: string;
    status?: string;
    type?: WorkCenterType;
  };
}

export interface QueueItem {
  operation_id: number;
  work_order_id: number;
  work_order_number: string;
  part_number?: string;
  part_name?: string;
  operation_number?: string;
  operation_name: string;
  status: OperationStatus;
  quantity_ordered: number;
  quantity_complete: number;
  priority: number;
  due_date?: string;
  setup_time_hours: number;
  run_time_hours: number;
}

export interface ActiveJob {
  time_entry_id: number;
  clock_in: string;
  entry_type: TimeEntryType;
  work_order_id?: number;
  operation_id?: number;
  work_center_id?: number;
  work_order_number?: string;
  part_number?: string;
  part_name?: string;
  operation_name?: string;
  operation_number?: string;
  work_center_name?: string;
  quantity_ordered?: number;
  work_order_quantity_ordered?: number;
  component_quantity?: number | null;
  quantity_complete?: number;
  laser_nest?: LaserNestInfo | null;
}
