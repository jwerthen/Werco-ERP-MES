export interface User {
  id: number;
  version: number;  // For optimistic locking
  employee_id: string;
  email: string;
  first_name: string;
  last_name: string;
  role: UserRole;
  department?: string;
  /**
   * E.164 mobile number used for SMS notifications. FIELD-MINIMIZED server-side:
   * it is serialized only on the self-profile response and admin user-management
   * responses — never on general user lists or mention search — so it is optional
   * here and absent on most payloads.
   */
  phone?: string | null;
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
  /**
   * Per-company SMS egress kill switch (Twilio). When false, no SMS body leaves
   * the system boundary to the commercial carrier and every SMS notification is
   * suppressed server-side (fail-closed). CUI/compliance control; defaults OFF.
   * Optional on the type because older cached CompanyResponse payloads predate
   * the column — treat a missing value as OFF.
   */
  allow_sms_egress?: boolean;
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
  /**
   * Per-nest sheet-part tie. Omit to leave the nest untied.
   *
   * Always an EXPLICIT pick — never fuzzy-matched from the AI-extracted
   * `material` / `thickness` free text. A wrong auto-tie depletes the wrong
   * heat lot into an as-built record.
   */
  material_part_id?: number | null;
  /**
   * Sheets consumed per completed run on the tied nest operation (defaults to
   * 1.0 server-side). Meaningless without `material_part_id`.
   */
  qty_per_run?: number | null;
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
    /**
     * The created/updated laser operations, one per nest. The payload is a full
     * `WorkOrderResponse` and has always carried these; the type simply never
     * exposed them. Optional so callers must null-check — an older server, or a
     * response shape change, must not crash the wizard.
     */
    operations?: Array<{
      id: number;
      operation_number?: string | null;
      laser_nest?: LaserNestInfo | null;
    }>;
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

// --- Material ties (work-order material allocations) ------------------------
// The OPTIONAL tie between a work order (or one of its operations) and stock
// material. Mirrors backend/app/schemas/work_order_material.py exactly.
// Consumption fires at WORK-ORDER completion, never per run — copy must say
// "deducts N when WO-#### finishes", never "deducting now".

/** How the tie was created. Mirrors backend `AllocationSource`. */
export type MaterialAllocationSource = 'nest' | 'bom' | 'manual';

/**
 * Lifecycle of a tie — this IS the tombstone (rows are never physically
 * deleted, so the ledger's `allocation_id` back-reference always resolves).
 *
 * `closed` is RESERVED and never written by any code: a fully consumed tie
 * stays `open`. Derive "fully consumed" from `qty_consumed >= qty_planned`,
 * never from status. Readers that want the LIVE ties must filter `'open'`
 * explicitly.
 */
export type MaterialAllocationStatus = 'open' | 'closed' | 'cancelled';

/** One material tie on a work order (GET/POST/PATCH/DELETE responses). */
export interface MaterialAllocation {
  id: number;
  work_order_id: number;
  /**
   * Set => the tie is OPERATION-scoped and depletes per completed run (the
   * laser-nest case). `null` => work-order-scoped, one-shot at completion.
   */
  work_order_operation_id: number | null;
  operation_number: string | null;
  /**
   * The operation this tie pointed at before a nest re-import superseded it and
   * cleared the link. `null` for every tie that was never detached — without it
   * a detached tie is indistinguishable from one that was always
   * work-order-scoped (both read `work_order_operation_id: null`). Reporting
   * only; the audit chain remains the record of record.
   */
  detached_from_operation_id: number | null;
  /** The MATERIAL part consumed — never the part being produced. */
  part_id: number;
  part_number: string | null;
  part_name: string | null;

  source: MaterialAllocationSource;
  status: MaterialAllocationStatus;

  /** Material per completed run. Operation-scoped ties only; `null` otherwise. */
  qty_per_run: number | null;
  qty_planned: number;
  /** Snapshot of the part's UoM at tie time. Nothing converts units. */
  unit_of_measure: string;
  /**
   * CACHE, not a compliance figure. The authoritative consumed total is the sum
   * of `inventory_transactions` carrying this allocation's `allocation_id`.
   * Label it as such wherever it is shown.
   */
  qty_consumed: number;

  /** Consume from THIS lot. `null` => FIFO picks the lot at consume time. */
  pinned_inventory_item_id: number | null;
  pinned_lot_number: string | null;

  notes: string | null;
  created_by: number | null;
  /** UTC ISO (`Z`) — render via centralTime, never `new Date().toLocaleString()`. */
  created_at: string;
  updated_at: string;
}

/**
 * POST body. `part_id`, `work_order_operation_id` and `source` are fixed at
 * creation — changing what a tie points at after consumption posted would
 * rewrite genealogy, so untie and re-tie instead.
 */
export interface MaterialAllocationCreatePayload {
  /** The MATERIAL part consumed — never the part being produced. */
  part_id: number;
  /**
   * Set => OPERATION-scoped (per-run). Omit for a work-order-scoped, one-shot
   * tie.
   */
  work_order_operation_id?: number | null;
  /** Defaults to `'manual'` server-side. */
  source?: MaterialAllocationSource;
  /**
   * Operation-scoped ties ONLY (defaults to 1.0 when omitted). Sending it
   * without `work_order_operation_id` is a 422 — there are no runs to scale by.
   */
  qty_per_run?: number | null;
  /** Total material planned for this tie. Must be > 0. */
  qty_planned: number;
  /** Omit to leave the tie UNPINNED (FIFO at consume time). A held lot is 422. */
  pinned_inventory_item_id?: number | null;
  notes?: string | null;
}

/**
 * PATCH body — an OPEN tie only (409 otherwise). Omitted fields are untouched.
 *
 * `clear_pinned_inventory_item` is required-with-default on the backend: send
 * `true` to drop the pin and fall back to FIFO. Sending it together with a
 * `pinned_inventory_item_id` is a 422 (they ask for opposite things), as is
 * lowering `qty_planned` below `qty_consumed`.
 */
export interface MaterialAllocationUpdatePayload {
  qty_per_run?: number | null;
  qty_planned?: number | null;
  pinned_inventory_item_id?: number | null;
  clear_pinned_inventory_item?: boolean;
  notes?: string | null;
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
  /**
   * Manager-dictated run order (Dispatch Board), 1..N per work center.
   * `null`/absent = unranked; the server already sorts ranked work first, so
   * the kiosks render server order and only DISPLAY this rank.
   */
  run_order?: number | null;
}

/**
 * Laser-nest details carried on a Dispatch Board row.
 *
 * Deliberately mirrors the kiosk queue's `laser_nest` block (shape + naming) so
 * the two payloads stay recognisably the same thing; the board only needs the
 * sequencing-relevant subset, so this is NOT `LaserNestInfo` (no id, file, or
 * document fields). Every field is optional — a non-laser operation has
 * `laser_nest: null` rather than an empty object.
 */
export interface DispatchNestInfo {
  cnc_number?: string | null;
  material?: string | null;
  thickness?: string | null;
  sheet_size?: string | null;
  planned_runs?: number | null;
  completed_runs?: number | null;
  remaining_runs?: number | null;
}

/**
 * The material tie carried on a Dispatch Board row.
 *
 * OPERATION-SCOPED ties only. A work-order-scoped tie would fan out across every
 * card of that work order and read as N separate ties, so the board never shows
 * one. `null`/absent for an untied operation — render nothing at all, no
 * placeholder and no "not tied" nag.
 *
 * `qty_remaining`, `on_hand` and `short_by` are SERVER-DERIVED (the board is a
 * read path and must not recompute stock client-side). `short_by` is 0 when
 * stock covers the remainder; > 0 is the shortage chip — advisory only, a
 * shortage never blocks production.
 */
export interface DispatchMaterialTie {
  allocation_id: number;
  part_id: number;
  part_number: string | null;
  /** Snapshot of the part's UoM at tie time. Nothing converts units. */
  unit_of_measure: string;
  /** Material per completed run; `null` on a tie that never set one (=> 1.0). */
  qty_per_run: number | null;
  qty_planned: number;
  /** CACHE — the ledger (`inventory_transactions.allocation_id`) is authoritative. */
  qty_consumed: number;
  /** `qty_planned - qty_consumed`, floored at 0 server-side. */
  qty_remaining: number;
  /** On-hand stock of the tied part, company-scoped. */
  on_hand: number;
  /** `max(0, qty_remaining - on_hand)`. 0 = covered. */
  short_by: number;
  /**
   * How many open ties the operation carries. The card renders ONE chip, so
   * without this a second tied part is invisible on the board. Optional: a
   * pre-feature cached payload has no value, which reads as a single tie.
   */
  tie_count?: number;
  /**
   * True when ANY of the operation's ties is short — not only the one this chip
   * names. Chip tone and the column's "N short" rollup read this, so a shortage
   * on a tie the card had no room to draw still surfaces.
   */
  any_short?: boolean;
  pinned_inventory_item_id: number | null;
  pinned_lot_number: string | null;
}

/**
 * One queued operation on the Dispatch Board (GET /shop-floor/dispatch-board).
 *
 * Rows arrive server-sorted: ranked work first by `run_order`, then unranked by
 * priority -> due date -> sequence. `version` is the operation's optimistic-lock
 * version, required by the cross-machine move (PUT /work-orders/operations/{id}).
 */
export interface DispatchBoardRow {
  operation_id: number;
  run_order: number | null;
  version: number;
  work_order_id: number;
  work_order_number: string;
  operation_number: string | number | null;
  operation_name: string | null;
  part_number: string | null;
  part_name: string | null;
  status: string;
  priority: number | null;
  /** Date-only ISO string (YYYY-MM-DD) — format via centralTime, never `new Date()`. */
  due_date: string | null;
  quantity_ordered: number;
  quantity_complete: number;
  setup_time_hours: number | null;
  run_time_hours: number | null;
  /**
   * Laser-nest details for a nest operation; `null`/absent for every other
   * operation. This is what a planner sequences nests by (material + thickness
   * drive sheet swaps, assist-gas and nozzle/lens changes).
   */
  laser_nest?: DispatchNestInfo | null;
  /**
   * Operation-scoped material tie for this operation; `null`/absent when the
   * operation is untied (the byte-identical pre-feature case — render nothing).
   * Optional so pre-feature payloads and existing fixtures still typecheck.
   */
  material_tie?: DispatchMaterialTie | null;
}

/** One work center column on the Dispatch Board. */
export interface DispatchBoardColumn {
  id: number;
  name: string;
  code: string;
  work_center_type: WorkCenterType;
  /** false = deactivated WC that still has queued work; read-only on the board. */
  is_active: boolean;
  queue: DispatchBoardRow[];
}

export interface DispatchBoardResponse {
  work_centers: DispatchBoardColumn[];
}

/**
 * PUT /shop-floor/work-centers/{id}/run-order returns the refreshed queue for
 * that work center. Accepts the bare row array or a wrapped column/queue object
 * so the client survives either envelope; `extractDispatchQueue` normalizes it.
 */
export type RunOrderUpdateResponse =
  | DispatchBoardRow[]
  | { queue: DispatchBoardRow[] }
  | DispatchBoardColumn;

// ---------------------------------------------------------------------------
// Kiosk shop-floor payload blocks (Kiosk Foundry Redesign, backend B1–B8).
// All additive + optional so pre-redesign backend payloads (and existing
// tests) still typecheck.
// ---------------------------------------------------------------------------

/**
 * A material tie carried on a kiosk queue row — the shop-floor-facing twin of
 * `DispatchMaterialTie` (types/index.ts), with the part NAME the operator reads
 * and without the pinned inventory id the office uses.
 *
 * Consumption fires at WORK-ORDER completion, never per run: an operator
 * finishing nest 1 of 3 deducts NOTHING. Copy must say "deducts N when WO-####
 * finishes" — never "this will deduct now". `qty_remaining`, `on_hand` and
 * `short_by` are server-derived (the queue is a READ path; it must not compute
 * stock, and it must not write).
 */
export interface KioskMaterialTie {
  allocation_id: number;
  part_id: number;
  part_number: string | null;
  part_name: string | null;
  /** Snapshot of the part's UoM at tie time. Nothing converts units. */
  unit_of_measure: string;
  /** Material per completed run; `null` on a tie that never set one (=> 1.0). */
  qty_per_run: number | null;
  qty_planned: number;
  /** CACHE — the ledger (`inventory_transactions.allocation_id`) is authoritative. */
  qty_consumed: number;
  /** `qty_planned - qty_consumed`, floored at 0 server-side. */
  qty_remaining: number;
  /** On-hand stock of the tied part, company-scoped. */
  on_hand: number;
  /** `max(0, qty_remaining - on_hand)`. 0 = covered. Advisory: never blocks work. */
  short_by: number;
  /** Named lot the tie is pinned to; `null` = FIFO picks at consume time. */
  pinned_lot_number: string | null;
}

/**
 * Last production-evidence telemetry for an operation ("LAST REPORT" tile).
 * Rides work-center-queue rows AND the my-active-job job dict. `at` is UTC
 * ISO — render via utils/centralTime.
 */
export interface KioskLastReport {
  at: string;
  good: number;
  scrap: number;
}

/**
 * The next operation in the WO routing after the current one ("ROUTES TO"
 * row). Rides the my-active-job job dict and the /complete response; null
 * when the current op is the last.
 */
export interface KioskNextOperation {
  operation_number: string | number | null;
  name: string | null;
  status: string;
  work_center: {
    id: number;
    code: string | null;
    name: string | null;
  } | null;
}

/**
 * Top-level `work_center` block on the work-center-queue response — feeds the
 * kiosk top bar (machine code + `name · description` line).
 */
export interface KioskQueueWorkCenter {
  id: number;
  code: string | null;
  name: string | null;
  description?: string | null;
  current_status?: string | null;
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
  /** Part.revision — the REV chip next to the part line (backend B1). */
  part_revision?: string | null;
  /** Last production-evidence telemetry for this operation (backend B4). */
  last_report?: KioskLastReport | null;
  /** Next op in the WO routing after this one; null when last (backend B5). */
  next_operation?: KioskNextOperation | null;
  /** Σ blocker downtime for this operation, minutes (backend B8). */
  downtime_minutes?: number;
  /** THIS open entry's session good count (backend B7) — AVG PER PC tile. */
  quantity_produced?: number;
  /** THIS open entry's session scrap count (backend B7). */
  quantity_scrapped?: number;
  /**
   * The OPERATION's running scrap total — deliberately a DIFFERENT key from
   * `quantity_scrapped` above, which is only THIS time entry's session count.
   * The consumption target is computed from the operation total, so feeding the
   * session figure into the deduction estimate would under-state any operation
   * worked across more than one sitting (or by a crew).
   */
  operation_quantity_scrapped?: number | null;
  /**
   * Open material ties on this operation. Present so the deduction notice still
   * renders when the running job is NOT in the queue the kiosk is displaying —
   * `activeQueueItem` is matched against the kiosk's SELECTED machine, so an
   * operator clocked onto a job at another work center resolves it `undefined`
   * and would otherwise lose the notice silently.
   */
  material_ties?: KioskMaterialTie[] | null;
}

/**
 * GET /shop-floor/my-active-job envelope. `server_time` (backend B2) anchors
 * the skew-corrected cycle timer/clock — compute skew once per poll.
 */
export interface MyActiveJobResponse {
  active_jobs?: ActiveJob[];
  /** Legacy single-job shape some callers still normalize from. */
  active_job?: ActiveJob | null;
  /** UTC ISO server clock at response time. */
  server_time?: string;
}

// ---------------------------------------------------------------------------
// Kiosk doc viewer discovery (backend A1) — GET /shop-floor/operations/{id}/documents
// ---------------------------------------------------------------------------

export interface OperationDocumentsPart {
  id: number;
  part_number: string;
  name: string | null;
  revision: string | null;
}

/** Newest approved/released DRAWING Document for the operation's part. */
export interface OperationDocumentsDrawing {
  document_id: number;
  revision: string | null;
  title: string | null;
  status: string;
  /** UTC ISO, when present — the RELEASED right-rail row. */
  released_at: string | null;
  file_name: string | null;
}

/** The operation's active laser nest document reference, when one exists. */
export interface OperationDocumentsNest {
  laser_nest_id: number;
  nest_name: string | null;
  cnc_number: string | null;
  /** Null when the nest has no attached PDF. */
  document_id: number | null;
  file_name: string | null;
}

/** One critical SPC characteristic for the CRITICAL DIMS right-rail list. */
export interface OperationCriticalDim {
  id: number;
  name: string;
  nominal: number | null;
  usl: number | null;
  lsl: number | null;
  unit_of_measure: string | null;
}

/** Response of GET /shop-floor/operations/{id}/documents (viewer discovery). */
export interface OperationDocumentsResponse {
  part: OperationDocumentsPart | null;
  drawing: OperationDocumentsDrawing | null;
  nest: OperationDocumentsNest | null;
  /** LaserNest.material when a nest is present — the MATERIAL rail row. */
  material: string | null;
  critical_dims: OperationCriticalDim[];
}
