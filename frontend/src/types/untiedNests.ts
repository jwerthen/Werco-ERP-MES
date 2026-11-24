/**
 * Contracts for the untied-laser-nest reads under `/laser-nests` — transcribed
 * from `backend/app/schemas/untied_nests.py`. Every one of these is a PURE READ
 * (no ledger row, audit row or event). The one action the worklist offers, "Tie
 * this sheet", is the EXISTING `POST /work-orders/{id}/material-allocations`;
 * `proposed_tie` is the body for it, minus `work_order_id` (the path parameter).
 *
 * Datetimes arrive as UTC ISO strings with a trailing `Z`; render them in Central
 * through `utils/centralTime`.
 */

export type UntiedNestStatusFilter = 'open' | 'completed' | 'all';

/** One stock part the sheet matcher proposes for a nest (advisory). */
export interface SheetCandidate {
  part_id: number;
  part_number: string;
  part_name: string;
  unit_of_measure: string | null;
  /** 100 is exact thickness + grade + size. */
  score: number;
  /** The matcher's one-sentence evidence for this part. */
  reason: string;
  /** 'deterministic' (spec) or 'history' (past ties). */
  basis: string;
  /** Company-owned, drawable on-hand of this part. */
  on_hand: number;
  /** False when the stock read failed; `on_hand` is then 0. */
  on_hand_known: boolean;
}

export type SheetSuggestionStatus = 'matched' | 'ambiguous' | 'unmatched';

export interface SheetSuggestionView {
  status: SheetSuggestionStatus;
  /** Set only when status is 'matched'. */
  candidate: SheetCandidate | null;
  /** Shortlist, best first (max 5). */
  candidates: SheetCandidate[];
  diagnostic: string | null;
}

/** The body for `POST /work-orders/{work_order_id}/material-allocations`. */
export interface ProposedNestTie {
  /** Path parameter for the tie endpoint (the nest's laser job) — not part of the body. */
  work_order_id: number;
  part_id: number;
  work_order_operation_id: number;
  source: 'nest';
  qty_per_run: number;
  qty_planned: number;
}

export interface UntiedNestRow {
  laser_nest_id: number;
  work_order_operation_id: number;
  operation_number: string | null;
  operation_name: string | null;
  operation_status: string;
  work_order_id: number;
  work_order_number: string;
  work_order_status: string;
  parent_work_order_id: number | null;
  parent_work_order_number: string | null;
  work_center_id: number | null;
  work_center_name: string | null;
  nest_name: string;
  /** The program number keyed on the laser. */
  cnc_number: string | null;
  material: string | null;
  thickness: string | null;
  sheet_size: string | null;
  /** material / thickness / sheet size, normalized, for display ('?' for a blank). */
  sheet_descriptor: string;
  planned_runs: number;
  /** The operation's good count (one run = one sheet). */
  runs_complete: number;
  runs_scrapped: number;
  /** When the operation completed; null while open. */
  completed_at: string | null;
  is_completed: boolean;
  /**
   * OPEN work-order-scoped ties on this nest's laser job. A job-level sheet tie
   * already consumes at work-order completion, so tying the nest as well could
   * draw the sheet twice.
   */
  job_level_tie_count: number;
  /** False when the tie endpoint would refuse (the laser job is finished). */
  tieable: boolean;
  tie_blocked_reason: string | null;
  /** Null only when the matcher could not run. */
  suggestion: SheetSuggestionView | null;
  /** Set only when the matcher identified one part AND the nest is tieable. */
  proposed_tie: ProposedNestTie | null;
  /**
   * Candidate parts with a cycle count COMPLETED after this nest completed: that
   * count already corrected on-hand for its sheets, so tying the nest to one of
   * them deducts the sheets again when the job completes. A warning, never a
   * refusal. Optional only so an older API without the field still renders.
   */
  counted_since_completion_part_ids?: number[];
}

export interface UntiedNestListResponse {
  items: UntiedNestRow[];
  /** Rows matching the filters, before limit/offset. */
  total: number;
  limit: number;
  offset: number;
  completed_from: string | null;
  completed_to: string | null;
  matcher_available: boolean;
}

export interface UntiedNestListParams {
  status?: UntiedNestStatusFilter;
  work_order_id?: number;
  work_center_id?: number;
  /** Company-local YYYY-MM-DD. */
  completed_from?: string;
  /** Company-local YYYY-MM-DD (inclusive day). */
  completed_to?: string;
  limit?: number;
  offset?: number;
}

export interface NestTieCoverageMonth {
  /** Company-local calendar month, YYYY-MM. */
  month: string;
  completed_nests: number;
  tied_nests: number;
  untied_nests: number;
  /** tied / completed x 100, one decimal; null with none completed. */
  tied_percent: number | null;
}

export interface NestTieCoverageResponse {
  /** Oldest first, one entry per month, zeros included. */
  months: NestTieCoverageMonth[];
  completed_nests: number;
  tied_nests: number;
  tied_percent: number | null;
}

export interface OpenCycleCountRef {
  id: number;
  count_number: string;
  status: string;
  scheduled_date: string | null;
}

export interface UntiedSheetGroup {
  /** Read-time grouping key (grade|thickness|size); approximate. */
  spec_key: string;
  material: string | null;
  thickness: string | null;
  sheet_size: string | null;
  sheet_descriptor: string;
  nest_count: number;
  /** Runs completed + scrapped across the group, one sheet per run. */
  sheets_burned: number;
  /** Nests in the group whose laser job is still open (still tieable from the worklist). */
  tieable_nest_count?: number;
  /**
   * Of `sheets_burned`, the sheets on still-open jobs. A tie corrects these when
   * the job completes; counting them as well deducts them twice.
   */
  tieable_sheets_burned?: number;
  work_order_count: number;
  /** Up to 10, most recent first. */
  work_order_numbers: string[];
  first_completed_at: string | null;
  last_completed_at: string | null;
  suggestion: SheetSuggestionView | null;
  /** Scheduled / in-progress cycle counts already scoped to the matched part. */
  open_cycle_counts: OpenCycleCountRef[];
}

export interface UntiedSheetReportResponse {
  /** Most sheets burned first. */
  groups: UntiedSheetGroup[];
  nest_count: number;
  sheets_burned: number;
  /** Of `sheets_burned`, the sheets on still-open (still tieable) jobs. */
  tieable_sheets_burned?: number;
  completed_from: string | null;
  completed_to: string | null;
  /** True when more completed untied nests existed than were read. */
  truncated: boolean;
  matcher_available: boolean;
}

export interface WorkOrderNestTieRow {
  laser_nest_id: number;
  work_order_operation_id: number;
  work_order_id: number;
  work_order_number: string;
  operation_number: string | null;
  cnc_number: string | null;
  nest_name: string;
  operation_status: string;
}

/** How many of a job's laser nests have no sheet tied (its own nests plus its laser child jobs'). */
export interface WorkOrderNestTieSummary {
  work_order_id: number;
  work_order_number: string;
  nest_count: number;
  tied_count: number;
  untied_count: number;
  job_level_tie_count: number;
  /** 'N of M nests have no sheet tied', or null when none. */
  message: string | null;
  untied: WorkOrderNestTieRow[];
}
