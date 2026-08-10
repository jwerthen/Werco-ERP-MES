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
  /**
   * Opt-in to AUTOMATIC BOM/routing component backflush at work-order completion.
   *
   * Read-mostly and deliberately asymmetric: the server exposes it on every part
   * read (`PartResponse`) and on `PartUpdate`, but NOT on `PartCreate`/`PartBase`
   * — so `POST /parts`, `POST /materials` and both CSV importers cannot set it,
   * and a part is always born with it off. That asymmetry is mirrored here: this
   * field is absent from `PartCreate`/`PartUpdate` in `types/api.ts` on purpose;
   * the only client that flips it is `api.setPartBackflush`.
   *
   * Turning it ON is server-GATED (409 while the part's backflush readiness check
   * reports blockers) — so any UI for it must stay NON-optimistic.
   */
  backflush_components: boolean;
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
 * One reason the sheet-stock matcher gives for what it did with a candidate.
 *
 * `severity` is the difference between "this candidate was ruled out" (`gate`)
 * and "this candidate stands, but read this first" (`advisory`) — a truncated
 * catalog, a sheet size the nest never stated, stock that will not cover the
 * demand. `code` is the server's vocabulary and deliberately a bare `string`,
 * not a union: the matcher may add codes, and a client that narrows the type
 * would either stop compiling against a truthful response or silently drop a
 * diagnostic it has never seen. Render `detail`, never a hard-coded sentence
 * keyed off `code`.
 */
export interface SheetMatchDiagnostic {
  code: string;
  severity: 'gate' | 'advisory';
  detail: string;
}

/**
 * One stock part the matcher offers for a nest.
 *
 * Every candidate has already cleared the HARD exact-thickness gate server-side
 * (±0.002"); `score` ranks what survived on alloy and sheet size. It is a
 * PROPOSAL — see `SheetPartSuggestion`.
 *
 * The stock fields annotate, they never rank: `on_hand_known` false means the
 * stock read did not land, and `on_hand` must not be rendered as a real zero in
 * that case (a fabricated 0 reads as "we are out", which is a different and
 * actionable claim). `demand` is what this package would draw, and
 * `projected_on_hand` is `on_hand - demand`, so `stock_state` can be shown
 * without the client re-deriving it.
 */
export interface SheetPartCandidate {
  part_id: number;
  part_number: string;
  part_name: string;
  unit_of_measure?: string | null;
  score: number;
  on_hand: number;
  on_hand_known: boolean;
  demand: number;
  projected_on_hand: number;
  stock_state: 'covered' | 'short' | 'none' | 'unknown';
  /** Thickness read off the candidate's own part number, for a side-by-side read. */
  spec_thickness?: string | null;
  spec_sheet_size?: string | null;
  is_sheet_like: boolean;
  /** How many nests have been tied to this part before — corroboration, not proof. */
  prior_tie_count: number;
  reason: string;
  basis: 'deterministic' | 'history' | 'ai_disambiguated';
  diagnostics: SheetMatchDiagnostic[];
}

/**
 * The server's answer for one nest row: which sheet part it thinks this nest is
 * cut from.
 *
 * A SUGGESTION IS ADVISORY. `auto_fill_part_id` is assigned only by the
 * deterministic gate (exact thickness, agreeing alloy, a clear margin over the
 * runner-up) and the wizard pre-fills its picker from it — but a pre-filled
 * picker is a proposal the planner confirms, never a tie. The tie is what makes
 * stock leave inventory when the nest's operation completes, into an AS9100D
 * as-built record that never auto-reverses, so nothing commits one without a
 * deliberate human act.
 *
 *  - `matched`   — one part, confidently: pre-fill it, and ask.
 *  - `ambiguous` — the data does not identify ONE sheet. `auto_fill_part_id` is
 *    null and `candidates` is the shortlist the planner picks from (2 rows, not
 *    500).
 *  - `unmatched` — nothing cleared the thickness gate. `diagnostic` says why.
 */
export interface SheetPartSuggestion {
  status: 'matched' | 'ambiguous' | 'unmatched';
  auto_fill_part_id?: number | null;
  candidates: SheetPartCandidate[];
  diagnostic?: string | null;
}

/**
 * How each imported nest's sheet tie came to be, keyed by `source_file`.
 *
 * Sent alongside the confirmed rows so the server can record WHERE a tie came
 * from, which the row payload itself cannot express — a `material_part_id` looks
 * identical whether a planner searched for it, stamped it package-wide, or
 * accepted a machine suggestion.
 *
 * COMMITTED TIES ONLY, over the closed three-value vocabulary the server accepts
 * (`_SHEET_MATCH_PROVENANCE_VALUES` in `work_orders.py`):
 *
 *  - `auto`    — the server suggested it and the planner CONFIRMED it in the
 *    accept dialog.
 *  - `planner` — the planner chose it themselves, per row or package-wide.
 *  - `prefill` — carried over from the tie the nest already had on a re-import.
 *
 * A row the planner left untied, and a suggestion they never confirmed, are both
 * OMITTED rather than sent as a fourth value: neither serializes a tie, so there
 * is no decision to describe, and an invented entry in an append-only audit row
 * is worse than an absent one. An entirely untied package sends `{}`.
 *
 * The wizard's internal `TieSource` is richer — it also models the uncommitted
 * states — and maps down to these three at the boundary; see
 * `PROVENANCE_BY_TIE_SOURCE` in `components/laser/LaserNestImportWizard.tsx`.
 *
 * A bare `Record<string, string>` rather than a narrowed union, because the
 * server filters the vocabulary itself and drops what it does not recognize; a
 * union here would only duplicate that check in the weaker of the two places.
 */
export type SheetMatchProvenance = Record<string, string>;

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
  /**
   * Server-computed sheet-part suggestion for this nest. Absent/null on an
   * older server, and absent is not "unmatched" — the wizard renders nothing at
   * all rather than a machine opinion it never received.
   */
  sheet_suggestion?: SheetPartSuggestion | null;
}

/**
 * One source operation the duplicate did NOT copy.
 *
 * Today the only reason is `laser_nest_deleted`: the operation's nest was
 * soft-deleted, which parks the operation ON_HOLD without cancelling its tie.
 * Copying it would put a nest task with no nest, no CNC number and no drawing
 * on the kiosk queue, because releasing a laser work order promotes every
 * pending operation to READY.
 *
 * `reason` is deliberately a bare `string`, not a union: the server owns the
 * vocabulary and may add to it, and a client that narrows it would either stop
 * compiling against a truthful response or silently mis-label a reason it has
 * never seen. Render it through a lookup with a default, never a hard-coded
 * sentence.
 */
export interface WorkOrderDuplicateSkippedOperation {
  source_operation_id: number;
  operation_number?: string | null;
  sequence?: number | null;
  reason: string;
}

/**
 * One source material tie the duplicate did NOT copy.
 *
 * `source_work_order_operation_id` joins a skipped tie back to the skipped
 * operation that explains it (null for a work-order-scoped tie).
 *
 * Reasons, and treat this list as open — render an unrecognized one verbatim
 * rather than dropping the row:
 *  - `part_not_available` — the tie's part has since been soft-deleted.
 *  - `operation_not_copied` — the operation the tie hung off was itself skipped.
 *  - `nest_runs_unavailable` — SERVER-SIDE DEFENCE, not currently producible: an
 *    operation that is nest-backed with no run count is already skipped upstream,
 *    so its tie reports `operation_not_copied` first.
 *
 * `part_id` is non-null: `work_order_material_allocations.part_id` is
 * `nullable=False`, so a skipped tie always names the part it would have drawn.
 */
export interface WorkOrderDuplicateSkippedAllocation {
  source_allocation_id: number;
  part_id: number;
  source_work_order_operation_id?: number | null;
  reason: string;
}

/**
 * `POST /work-orders/{id}/duplicate`.
 *
 * An ENVELOPE, not a bare work order — the skip lists are the point. A skipped
 * tie means the new job carries no demand for that material: no shortage shows,
 * the nests run, and stock is never deducted. Both lists empty is the
 * "nothing was lost" signal, so a caller must read them rather than assume.
 *
 * `work_order.quantity_ordered` is authoritative and can differ from what was
 * requested: on a nest-bearing work order the server derives it from the copied
 * nests' planned runs.
 */
export interface WorkOrderDuplicateResult {
  work_order: WorkOrder;
  skipped_operations: WorkOrderDuplicateSkippedOperation[];
  skipped_material_allocations: WorkOrderDuplicateSkippedAllocation[];
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
  /**
   * Package-level rollups of the sheet-part match, as the SERVER saw it at
   * preview time: rows with a pre-fillable match, rows with only a shortlist,
   * and rows whose suggested sheet will not cover this package's demand.
   *
   * A SNAPSHOT, not live state. The wizard's own chips are derived from the
   * rows, because the planner accepts, re-picks and clears them as they review
   * — a count frozen at preview time would keep claiming work that is already
   * done. These stay on the type because they are what the response carries.
   */
  suggested_row_count?: number | null;
  shortlist_row_count?: number | null;
  short_stock_row_count?: number | null;
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
//
// Consumption fires when an OPERATION completes (that operation's ties, and only
// those) — corrected here in PR 2.5; this block previously said "at WORK-ORDER
// completion", which under-stated it once the per-operation seam landed. It is
// still NOT per run: reporting 3 of 6 runs on a nest that is still open deducts
// nothing. `utils/materialTie.ts` is the single home for the client-side
// arithmetic and the copy — anchor every string on THIS OPERATION completing.

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

// --- Material RETURN (PR 3) --------------------------------------------------
// Consumption NEVER auto-reverses (invariant 6b): a negative delta is a no-op,
// so every "reverse consumption first" refusal needs an ACTOR to perform it,
// with a reason, on the record. That verb is the RETURN below. It appends a
// signed compensating `RETURN` inventory transaction per credited lot — it never
// mutates the historical ISSUE rows — mirroring the receiving correct/void
// pattern.
//
// What it does NOT unlock: nest package re-import stays refused after a full
// return. The original ISSUE rows and the new RETURN rows both still reference
// the operation a rebuild would delete, so the ledger — not the `qty_consumed`
// cache — is what the re-import guard reads. The remedy for a job whose nests
// must change is still a NEW work order.

/**
 * Which of the two named intents the actor is performing. There is deliberately
 * nothing in between: a return that would leave `qty_consumed` below the tie's
 * LIVE target with the tie still open is refused **422**, because the sum-delta
 * engine would simply re-consume it on the next completion — or on a
 * reconcile-on-read GET, which re-runs FIFO and can credit a DIFFERENT lot than
 * the material came from, fabricating heat/cert linkage in an as-built record.
 *
 * - `correct_over_consumption` — BOUNDED by `qty_consumed - target`, where
 *   `target` is recomputed live from operation state at return time (never from
 *   `qty_planned`). The tie stays OPEN and live. Afterwards `qty_consumed >=
 *   target`, so the engine's delta stays `<= 0` and it no-ops forever, on every
 *   path including GETs. This is exactly the negative delta the engine already
 *   computes and refuses to execute — now performed by a human, with a reason.
 * - `return_and_untie` — UNBOUNDED (return everything consumed) and sets the
 *   tie `status = 'cancelled'` in the SAME transaction, so the engine can never
 *   re-draw it.
 *
 * The 422 detail names which intent the caller wants; render it verbatim.
 */
export type MaterialReturnIntent = 'correct_over_consumption' | 'return_and_untie';

/**
 * POST body for the return.
 *
 * `reason` is REQUIRED and non-blank — validated at the Pydantic boundary
 * exactly like `ReceiptCorrection.reason` (min_length 1, max 500, plus a
 * strip-and-reject-whitespace validator), so a blank one comes back **422**
 * from FastAPI's own validation rather than a hand-rolled 400. It lands in the
 * ledger `notes` AND the audit `description` AND `extra_data.reason`.
 */
export interface MaterialReturnRequest {
  /** Total material to credit back, in the tie's `unit_of_measure`. Must be > 0. */
  quantity: number;
  intent: MaterialReturnIntent;
  /** Required, non-blank, <= 500 chars. Whitespace-only is a 422. */
  reason: string;
}

/**
 * One lot credited by a return.
 *
 * Mirrors the server's `MaterialReturnLot` (`schemas/work_order_material.py`)
 * FIELD FOR FIELD. It is deliberately NOT shared with `MaterialConsumptionLine`
 * below: the pre-confirm read answers "what has this lot issued and given back"
 * (`issued`/`returned`/`net`) while this answers "what did THIS return credit"
 * (`quantity`/`unit_cost`/the two transaction ids). Collapsing them would make
 * one of the two silently wrong.
 *
 * Material is ALWAYS returned to its SOURCE lots: the server walks the tie's own
 * ISSUE rows NEWEST-FIRST and credits each lot back, capped per lot at
 * issued-minus-already-returned. A single consumption can spill across several
 * FIFO lots, so one logical return is N of these rows. Crediting any other lot
 * would invent heat/cert linkage (AS9100D 8.5.2), which is why there is no
 * "return to a lot of your choosing" option.
 */
export interface MaterialReturnLotLine {
  inventory_item_id: number;
  /** The lot the material goes back to. `null` for a lot-less stock row. */
  lot_number: string | null;
  /** Quantity credited to THIS lot (positive). */
  quantity: number;
  /**
   * Copied from the ISSUE ROW being compensated, NOT the lot's current
   * `unit_cost` — a revaluation between consume and return would otherwise leave
   * residual (or negative) material cost on the job.
   */
  unit_cost: number;
  /** The appended `RETURN` inventory transaction this line created. */
  transaction_id: number;
  /** The ISSUE row it compensates. Historical rows are never mutated. */
  compensated_transaction_id: number;
}

/**
 * 200 response from the return — the server's `MaterialReturnResponse`, field
 * for field.
 *
 * This interface previously declared `lines` and an `allocation` object, neither
 * of which the server has ever sent. Because the only consumer read
 * `result.lines`, the per-lot count silently degraded to zero and the
 * confirmation toast dropped the "…to 2 lots" disclosure with no error anywhere.
 * Keep this shape pinned to the schema; `MaterialTiesPanel.test.tsx` fixtures
 * are built from the real response for exactly that reason.
 */
export interface MaterialReturnResult {
  allocation_id: number;
  work_order_id: number;
  part_id: number;
  part_number: string | null;
  intent: MaterialReturnIntent;
  /** The tie's unit of measure, echoed so a toast need not look it up. */
  unit_of_measure: string;
  /** Total credited back across every lot — the sum of `returned_lots[].quantity`. */
  quantity_returned: number;
  /** The tie's `qty_consumed` BEFORE this return. */
  qty_consumed_before: number;
  /**
   * The tie's `qty_consumed` AFTER the return. Still a CACHE: the authoritative
   * total is the sum of `inventory_transactions` carrying this `allocation_id`.
   */
  qty_consumed: number;
  /**
   * The tie's status AFTER the return: `'open'` for `correct_over_consumption`,
   * `'cancelled'` for `return_and_untie`.
   */
  status: MaterialAllocationStatus;
  /** Per-lot breakdown of what was actually credited, newest source lot first. */
  returned_lots: MaterialReturnLotLine[];
}

/**
 * One lot's consumption ledger for a tie — the pre-confirm read that answers
 * "where will this material land?" BEFORE anything moves.
 *
 * Rows are the tie's `inventory_transactions` grouped per lot. `net` is what is
 * still out on the job for that lot, and therefore the per-lot CAP on any
 * further return; the array is ordered newest source lot first, which is the
 * order the return itself credits in.
 */
export interface MaterialConsumptionLine {
  inventory_item_id: number;
  lot_number: string | null;
  /** Total ISSUEd from this lot against the tie (positive). */
  issued: number;
  /** Total already RETURNed to this lot against the tie (positive). */
  returned: number;
  /** `issued - returned` — still consumed, and the cap on a further return. */
  net: number;
}

// --- BOM/routing backflush: dry-run preview + opt-in readiness (PR 4.5) ------
//
// A SECOND, independent consumption path from the ties above. A tie is the
// explicit "this job eats that stock" link a planner draws; backflush is the
// automatic one, driven off the finished part's BOM and routing and gated on
// `Part.backflush_components`. Both legs post to the same ledger and both
// surface here, which is why the preview's lines carry `source` and
// `requires_opt_in` rather than assuming one origin.
//
// Both shapes below back PURE READS — `GET /work-orders/{id}/backflush-preview`
// and `GET /parts/{id}/backflush-readiness`. They write nothing: no ledger row,
// no audit row, no event. Polling them is free and records no reason.

/**
 * One thing the demand resolver could not answer cleanly about a BOM/routing.
 *
 * `severity` is what matters:
 *  - `blocking` — the resolved demand is wrong or absent. These are exactly the
 *    sentences the server joins into its 409 when a flip is refused, so they
 *    read as complete sentences on their own.
 *  - `advisory` — usable, but worth a human's attention, OR work-order-scoped
 *    and therefore not gateable at part opt-in at all.
 *
 * `code` is the stable machine key; `detail` is the operator-facing sentence.
 * Render `detail` — never a prettified `code` — so the UI cannot drift from the
 * refusal the server would actually produce.
 */
export interface BackflushDiagnostic {
  code: string;
  severity: 'blocking' | 'advisory' | string;
  detail: string;
  bom_item_id: number | null;
  component_part_id: number | null;
  component_part_number: string | null;
  operation_id: number | null;
}

/**
 * One ISSUE row a completion would post, in the order the draw walks them.
 *
 * `is_shortfall` marks the row for the part of the demand no permitted lot could
 * cover: the writer posts it as a SEPARATE issue against the last lot it drew
 * (or the first candidate lot if it drew nothing), driving that lot negative and
 * putting its lot number on the as-built record. A line can therefore list the
 * same `inventory_item_id` twice — the covered take, then the remainder.
 */
export interface BackflushPreviewLot {
  inventory_item_id: number;
  lot_number: string | null;
  location: string | null;
  quantity: number;
  unit_cost: number;
  is_shortfall: boolean;
}

/**
 * Which rule stopped a preview line from moving material. `null` when nothing did.
 *
 * Kept as a union of the server's own vocabulary (widened with `string` so a new
 * server value degrades to "suppressed, reason shown raw" rather than a type error).
 */
export type BackflushSuppressionReason =
  | 'converged'
  | 'already_issued'
  | 'ledger_consumed'
  | 'open_operation_tie'
  /** A blocking diagnostic stands, so the completion refuses this component and
   *  records a `BACKFLUSH_DEMAND_REFUSED` audit row instead of issuing it. */
  | 'blocking_diagnostic'
  | string;

/**
 * One component's whole decision: target, what already posted, and the lots it hits.
 *
 * `delta_quantity` (`required_quantity - already_issued`) is what would actually
 * post now. The leg reconciles to target and NEVER auto-reverses, so a
 * non-positive delta is a no-op rather than a credit — which is why a suppressed
 * line is normal rather than an error.
 *
 * `requires_opt_in` is true on BOM/routing lines (they move only once the part's
 * `backflush_components` is on) and false on work-order-scoped tie lines, where
 * the tie itself IS the opt-in and consumes regardless.
 */
export interface BackflushPreviewLine {
  component_part_id: number;
  component_part_number: string | null;
  component_part_name: string | null;
  unit_of_measure: string | null;
  /** `'bom_routing'` (needs the flag) or `'work_order_tie'` (its own opt-in). */
  source: 'bom_routing' | 'work_order_tie' | string;
  requires_opt_in: boolean;
  allocation_id: number | null;
  /** The reconcile TARGET for this component. */
  required_quantity: number;
  /** Signed ledger net already posted against it (ISSUE − RETURN). */
  already_issued: number;
  /** What would post now. `0` when suppressed. */
  delta_quantity: number;
  suppressed: boolean;
  suppression_reason: BackflushSuppressionReason | null;
  available_quantity: number;
  shortfall: number;
  would_go_negative: boolean;
  /**
   * Stock that IS on hand but segregated (hold / quarantine / rejected /
   * inactive) and therefore skipped. The difference between a purchasing signal
   * and an MRB signal. Always 0 on a PINNED line — there, the pin is why nothing
   * else was drawn.
   */
  held_quantity_skipped: number;
  held_lot_numbers: string[];
  pinned_inventory_item_id: number | null;
  pinned_lot_number: string | null;
  /**
   * The PINNED lot went on hold / quarantine / rejected / inactive after it was
   * pinned, and the completion will consume it anyway (recording
   * `HELD_MATERIAL_CONSUMED`). That draw is not short, so the `held_*` fields
   * above stay empty — this is the only warning for it.
   */
  pinned_lot_is_held: boolean;
  /**
   * No stock row for this part exists at all, so rather than driving a lot
   * negative the completion mints a lot-less placeholder row and posts against
   * it. Mutually exclusive with a trailing `is_shortfall` lot.
   */
  shortfall_creates_placeholder: boolean;
  lots: BackflushPreviewLot[];
}

/**
 * A dry run of one work order's component consumption. Nothing was written.
 *
 * `backflush_components` is the FINISHED part's current flag. BOM/routing lines
 * are reported whether or not it is set — the operator reading this is deciding
 * whether to set it — so read it together with each line's `requires_opt_in`.
 *
 * `basis` is `quantity_complete + operation scrap`. A work order that has
 * produced nothing has a basis of 0 and therefore no BOM lines at all; that is
 * the resolver's real behaviour, not a preview artefact.
 */
export interface BackflushPreviewResponse {
  work_order_id: number;
  work_order_number: string | null;
  part_id: number | null;
  part_number: string | null;
  backflush_components: boolean;
  basis: number;
  lines: BackflushPreviewLine[];
  blockers: BackflushDiagnostic[];
  advisories: BackflushDiagnostic[];
}

/**
 * Whether a part may opt into automatic backflush, and what refuses it if not.
 *
 * `eligible === (blockers.length === 0)`. It is **not authorisation and not
 * durable**: every input it reads (BOM lines, `is_alternate`/`is_optional`/
 * `item_type`/`quantity`, the routing's `component_part_id`) is mutable
 * afterwards by other people, so the identical check re-runs server-side on the
 * write that sets the flag. Never treat a stale `eligible: true` as permission —
 * make the call and render what comes back.
 *
 * Only the BOM half is answerable at part scope; routing conditions need a work
 * order and appear on the backflush preview instead.
 */
export interface PartBackflushReadiness {
  part_id: number;
  part_number: string | null;
  /** The part's CURRENT flag, so state and eligibility render together. */
  backflush_components: boolean;
  eligible: boolean;
  blockers: BackflushDiagnostic[];
  advisories: BackflushDiagnostic[];
}

/**
 * One BOM line whose STATED unit of measure contradicts its component part's.
 *
 * This is the shape behind the `unit_of_measure_mismatch` BLOCKING diagnostic:
 * nothing in the platform converts units, so a line stating `each` against a
 * part stocked in `sheets` would issue the wrong quantity of the right
 * material. Until every such line is corrected, `Part.backflush_components`
 * cannot be armed on the assembly.
 *
 * `line_unit_of_measure` / `component_unit_of_measure` are the NORMALISED
 * labels the server's comparison actually used, not the raw column text — what
 * the row shows is what the gate compared. In particular `ea` does not satisfy
 * `each`: that is a stored value a human should normalise, not a synonym the
 * report should paper over.
 */
export interface BOMLineUomMismatch {
  bom_id: number;
  bom_revision: string | null;
  bom_status: string | null;
  bom_is_active: boolean;
  /** The ASSEMBLY the BOM belongs to (the part someone is trying to arm). */
  part_id: number;
  part_number: string;
  bom_item_id: number;
  item_number: number | null;
  component_part_id: number;
  component_part_number: string;
  component_part_name: string | null;
  /**
   * Disclosed, not filtered. The readiness explosion resolves soft-deleted
   * components of this company on purpose (they raise their own blocking
   * diagnostic), so hiding them here would hide a row that still blocks.
   */
  component_is_deleted: boolean;
  /** What the BOM LINE says. */
  line_unit_of_measure: string;
  /** What the component part is actually STOCKED in. */
  component_unit_of_measure: string;
  /**
   * Whether the backflush would ever issue THIS LINE — false on alternate /
   * optional / reference lines, which raise no diagnostic and refuse nothing.
   *
   * **It answers the LINE, not the tree.** A line inside a `make`
   * sub-assembly reports `true` here and still refuses nothing when the parent
   * assembly is armed. The authoritative per-part answer is `blockers` on
   * `PartBackflushReadiness` (`GET /parts/{id}/backflush-readiness`).
   */
  blocks_backflush: boolean;
}

/**
 * The pre-arming remediation worklist for `Part.backflush_components`.
 *
 * `truncated` is load-bearing: the scan has a candidate ceiling, and when it is
 * hit `total` is a **FLOOR, not a count**. A UI that renders it as a plain
 * total lies about how much work is left — say so on screen and tell the user
 * to narrow the filters.
 */
export interface BOMUomMismatchReport {
  /** Every disagreeing line found under the requested filters — a FLOOR when `truncated`. */
  total: number;
  /** How many rows this page actually carries. */
  returned: number;
  truncated: boolean;
  items: BOMLineUomMismatch[];
}

/** Query params accepted by `GET /bom/uom-mismatches`. */
export interface BOMUomMismatchParams {
  /**
   * Lines on this assembly part's OWN BOM only. It does NOT follow nested
   * sub-assembly BOMs, which a readiness check for that part does reach — so
   * the UNFILTERED report is the authoritative pre-arming worklist and this is
   * for working one assembly at a time.
   */
  part_id?: number;
  bom_id?: number;
  component_part_id?: number;
  /** Default true server-side — the BOMs a backflush actually reads. */
  active_only?: boolean;
  skip?: number;
  /** Server default 100, max 500. */
  limit?: number;
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
 * Consumption posts when THIS OPERATION completes — a laser child WO carries
 * one operation per nest, so finishing nest 1 of 3 deducts nest 1's sheets
 * right then. It is still never per individual run WITHIN an operation:
 * reporting runs on a still-open operation posts nothing. `utils/materialTie.ts`
 * owns the operator-facing copy and the timing rules behind it — write and
 * change wording there, not here. `qty_remaining`, `on_hand` and `short_by` are
 * server-derived (the queue is a READ path; it must not compute stock, and it
 * must not write).
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
