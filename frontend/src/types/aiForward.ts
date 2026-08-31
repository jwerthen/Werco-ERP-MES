// `ResumeOpenBlocker` is the shop-floor resume's still-open-blocker shape.
// Imported rather than restated so the blocker-resolve response and the
// hold-resume response cannot drift into two vocabularies for one fact.
import type { ResumeOpenBlocker } from './index';

export type WorkOrderBlockerCategory =
  | 'material_missing'
  | 'machine_down'
  | 'tooling_missing'
  | 'quality_hold'
  | 'labor_unavailable'
  | 'engineering_question'
  | 'previous_operation'
  | 'other';

export type WorkOrderBlockerSeverity = 'low' | 'medium' | 'high' | 'critical';
export type WorkOrderBlockerStatus = 'open' | 'acknowledged' | 'resolved' | 'dismissed';

export interface WorkOrderBlocker {
  id: number;
  company_id: number;
  work_order_id: number;
  operation_id?: number | null;
  material_part_id?: number | null;
  category: WorkOrderBlockerCategory;
  severity: WorkOrderBlockerSeverity;
  status: WorkOrderBlockerStatus;
  title: string;
  note?: string | null;
  resolution_note?: string | null;
  reported_by?: number | null;
  assigned_to?: number | null;
  resolved_by?: number | null;
  reported_at: string;
  acknowledged_at?: string | null;
  resolved_at?: string | null;
  created_at: string;
  updated_at: string;
  work_order_number?: string | null;
  operation_name?: string | null;
  material_part_number?: string | null;
}

/**
 * Why closing a blocker did NOT take its operation off hold.
 *
 * Mirrors the backend's `BlockerResumeWithheldReason` (closed vocabulary, stable
 * strings — the docs key on them too). Only ONE of these means a resume was owed
 * and something withheld it:
 *
 * - `other_blockers_open` — another OPEN/ACKNOWLEDGED blocker still names this
 *   operation, so closing this one changed nothing on the floor.
 * - `no_operation` — the blocker named no operation; nothing was ever held.
 * - `operation_not_held` — the operation is not on hold; nothing to lift.
 * - `operation_missing` — the operation row could not be loaded.
 *
 * Do NOT warn "still held" off this field. The last three mean there was nothing
 * to resume, and saying "still held" for one of them is a new kind of dishonesty,
 * not a fix. Read `operation_still_held` for that judgement — it stays correct
 * when a fifth reason is added (the cancelled-nest tombstone).
 */
export type BlockerResumeWithheldReason =
  | 'no_operation'
  | 'other_blockers_open'
  | 'operation_not_held'
  | 'operation_missing';

/**
 * What closing a blocker did to its OPERATION — the fact a 200 could not carry.
 *
 * `POST /work-order-blockers/{id}/resolve` and `PUT /work-order-blockers/{id}`
 * both returned the blocker row and nothing about the operation, so a resolve
 * that took a job off hold was indistinguishable from one that left it exactly
 * where it was. The page fired an unconditional green toast, and an owner read
 * "Resolved blocker" over a nest that was still ON_HOLD.
 *
 * TWO things are warnable and they are different:
 *
 * 1. `operation_still_held` — a resume was OWED and WITHHELD. The job is still
 *    on hold; closing this blocker did not change that.
 * 2. `operation_resumed` with `operation_status === 'pending'` — the hold DID
 *    clear, but PENDING is off the dispatch board and off the kiosk (both
 *    surface READY only), so the job did not come back to the queue.
 *
 * Take the verdict from `resolveBlockerOutcome`, never by re-deriving either
 * rule at a call site.
 */
export interface BlockerOperationOutcome {
  operation_id?: number | null;
  /** The operation's status AFTER the call. Null when the row is gone. */
  operation_status?: string | null;
  /** True only when this call actually moved the operation off hold. */
  operation_resumed?: boolean;
  /** Null exactly when a resume happened. */
  resume_withheld_reason?: BlockerResumeWithheldReason | null;
  /** A resume was owed and withheld: the operation is STILL on hold. */
  operation_still_held?: boolean;
  /**
   * The blockers still in the way, in the `ResumeOpenBlocker` shape the
   * shop-floor resume already returns — reused verbatim, not a second vocabulary.
   * `has_note` / `free_text_withheld` are absent here: they describe the
   * crew-station free-text gate, which cannot apply on a router no kiosk token
   * can reach (see the backend `OperationOpenBlocker` docstring).
   */
  open_blockers?: ResumeOpenBlocker[] | null;
}

/**
 * What `POST /work-order-blockers/{id}/resolve` and `PUT /work-order-blockers/
 * {id}` return: the blocker row, plus what the write did to its operation.
 *
 * `operation_outcome` is **absent/null when no resume was even attempted** — the
 * call left the blocker open or acknowledged, so the operation was never a
 * candidate. Absence means not-applicable. NEVER warn on absence: that would put
 * a "still held" notice on an acknowledge, which is the same class of false
 * statement this field exists to remove.
 */
export interface WorkOrderBlockerWriteResult extends WorkOrderBlocker {
  operation_outcome?: BlockerOperationOutcome | null;
}

export interface WorkOrderBlockerInput {
  operation_id?: number;
  material_part_id?: number;
  category: WorkOrderBlockerCategory;
  severity?: WorkOrderBlockerSeverity;
  title?: string;
  note?: string;
  assigned_to?: number;
  put_operation_on_hold?: boolean;
}

export interface NaturalLanguageSearchResult {
  id: number;
  type: string;
  title: string;
  subtitle?: string;
  url: string;
  icon: string;
  explanation: string;
  matched_filters: string[];
}

export interface NaturalLanguageSearchResponse {
  query: string;
  confidence: number;
  interpreted_filters: Record<string, unknown>;
  used_fallback: boolean;
  results: NaturalLanguageSearchResult[];
}

export interface AdaptivePrompt {
  id: string;
  title: string;
  detail: string;
  href?: string;
  action_label?: string;
}
