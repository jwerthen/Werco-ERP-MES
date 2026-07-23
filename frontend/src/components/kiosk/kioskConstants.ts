/**
 * Operator kiosk (A0.3) shared constants.
 *
 * Compliance notes:
 * - Every mutating call from the kiosk reports `source: "kiosk"` — the A0.1
 *   adoption-telemetry channel. Use KIOSK_SOURCE, never a string literal.
 * - Scrap ALWAYS requires an explicit reason. Lean Phase 1: when the company
 *   has ACTIVE scrap reason codes (GET /quality/scrap-reason-codes) the kiosk
 *   picker is built from those codes and sends `scrap_reason_code_id`
 *   (+ optional free-text detail as `scrap_reason`). SCRAP_REASONS below is
 *   the FALLBACK vocabulary for companies with zero active codes — the chosen
 *   label is stored verbatim in the TimeEntry.scrap_reason column (free
 *   string, 255 max) on clock-out and in-shift production reports.
 * - Hold reasons mirror the backend WorkOrderBlockerCategory enum
 *   (backend/app/models/work_order_blocker.py) so a kiosk hold files the same
 *   structured blocker a supervisor would. The backend only files a blocker
 *   when the hold carries a note OR a non-OTHER category, so the kiosk sends a
 *   stub note ("Other (reported at kiosk)") with the "Other" tile — every
 *   kiosk hold files a blocker.
 */

import { KioskLastReport, KioskQueueWorkCenter, LaserNestInfo } from '../../types';

export const KIOSK_SOURCE = 'kiosk';

export interface KioskReason {
  value: string;
  label: string;
}

/**
 * Shop-standard FALLBACK scrap reasons, stored verbatim as
 * TimeEntry.scrap_reason. Used only when the company has no active scrap
 * reason codes (Lean Phase 1) — codes take precedence everywhere.
 */
export const SCRAP_REASONS: KioskReason[] = [
  { value: 'Setup / first article', label: 'Setup / First article' },
  { value: 'Out of tolerance', label: 'Out of tolerance' },
  { value: 'Surface finish', label: 'Surface finish' },
  { value: 'Material defect', label: 'Material defect' },
  { value: 'Tooling damage', label: 'Tooling damage' },
  { value: 'Machine fault', label: 'Machine fault' },
  { value: 'Program error', label: 'Program error' },
  { value: 'Handling damage', label: 'Handling damage' },
];

/**
 * Over-count CORRECTION reasons (self-service reduce-production). These are NOT
 * scrap — no scrap move happens — but the backend requires a non-blank reason on
 * every walk-back for the tamper-evident audit trail. The chosen label is stored
 * verbatim as the correction reason (255 max). Touch-friendly tiles keep the
 * digits-only keypad free for the quantity.
 */
export const CORRECTION_REASONS: KioskReason[] = [
  { value: 'Double-counted', label: 'Double-counted' },
  { value: 'Scanned twice', label: 'Scanned twice' },
  { value: 'Wrong quantity entered', label: 'Wrong qty entered' },
  { value: 'Mis-key / typo', label: 'Mis-key / typo' },
  { value: 'Counted wrong job', label: 'Wrong job' },
  { value: 'Other', label: 'Other' },
];

/** Hold reasons — values are WorkOrderBlockerCategory enum values. */
export const HOLD_REASONS: KioskReason[] = [
  { value: 'material_missing', label: 'Material missing' },
  { value: 'machine_down', label: 'Machine down' },
  { value: 'tooling_missing', label: 'Tooling missing' },
  { value: 'quality_hold', label: 'Quality hold' },
  { value: 'labor_unavailable', label: 'Labor unavailable' },
  { value: 'engineering_question', label: 'Engineering question' },
  { value: 'previous_operation', label: 'Previous operation' },
  { value: 'other', label: 'Other' },
];

/** Work-center queue row as returned by GET /shop-floor/work-center-queue/{id}. */
export interface KioskQueueItem {
  operation_id: number;
  work_order_id: number;
  work_order_number: string;
  part_number: string | null;
  part_name: string | null;
  operation_number: string | number | null;
  operation_name: string | null;
  work_center_id: number;
  status: string;
  quantity_ordered: number;
  quantity_complete: number | null;
  priority: number | null;
  due_date: string | null;
  // Laser cutting: the active nest for this operation (CNC#, runs, optional PDF)
  // so operators can confirm the right nest before cutting.
  laser_nest?: LaserNestInfo | null;
  // Process Sheets chip (PR 3): REQUIRED snapshot-step counts, server-derived.
  // 0/0 means the operation has no gating steps — the chip is hidden.
  steps_total?: number | null;
  steps_recorded?: number | null;
  // Manager-dictated run order (Dispatch Board), 1..N per work center; null when
  // unranked. ADVISORY: the server already returns the queue in this order and any
  // job may still be started, so the kiosk only DISPLAYS it (no client-side sort).
  run_order?: number | null;
  // Part.revision — the REV chip (Kiosk Foundry Redesign, backend B1).
  part_revision?: string | null;
  // Last production-evidence telemetry for this operation (backend B4).
  last_report?: KioskLastReport | null;
}

/**
 * GET /shop-floor/work-center-queue/{id} envelope for the single-operator
 * kiosk (JWT-authed api client). The crew-station twin is
 * `KioskCrewQueueResponse` (types/kioskStation.ts). All non-queue fields are
 * optional so pre-redesign backend payloads still typecheck.
 */
export interface KioskWorkCenterQueueResponse {
  queue: KioskQueueItem[];
  /** UTC ISO server clock at response time — timer skew anchor. */
  server_time?: string;
  /** The queue's work center (backend B3) — feeds the kiosk top bar. */
  work_center?: KioskQueueWorkCenter | null;
}

/** "Steps 2/6" — the process-steps chip label (call only when steps_total > 0). */
export function formatStepsChip(item: Pick<KioskQueueItem, 'steps_total' | 'steps_recorded'>): string {
  return `Steps ${Number(item.steps_recorded || 0)}/${Number(item.steps_total || 0)}`;
}

/** One operator's open TimeEntry on a queued operation (crew-station roster). */
export interface KioskRosterEntry {
  time_entry_id: number;
  user_id: number;
  /** Server emits null when the entry's user record is missing. */
  operator_name: string | null;
  employee_id: string | null;
  entry_type: string;
  /** UTC ISO clock-in — feed through formatElapsed with the skew-corrected now. */
  clock_in: string;
}

/** Display fallback for a roster/closed entry whose user record is missing. */
export const UNKNOWN_OPERATOR_LABEL = 'Operator';

/**
 * Crew-station queue row: the standard queue item plus the live roster of
 * operators clocked into the operation. `quantity_scrapped` feeds the
 * operation-level tally ("37 of 50 · 2 scrap") that guards double counting.
 */
export interface KioskCrewQueueItem extends KioskQueueItem {
  roster: KioskRosterEntry[];
  quantity_scrapped?: number | null;
}

/** "37 of 50 · 2 scrap" — the operation-level crew tally line. */
export function formatCrewTally(item: Pick<KioskCrewQueueItem, 'quantity_complete' | 'quantity_ordered' | 'quantity_scrapped'>): string {
  const done = Number(item.quantity_complete || 0);
  const ordered = Number(item.quantity_ordered || 0);
  const scrap = Number(item.quantity_scrapped || 0);
  return scrap > 0 ? `${done} of ${ordered} · ${scrap} scrap` : `${done} of ${ordered}`;
}

/** hh:mm:ss elapsed since a UTC clock-in, for the live per-person timers. */
export function formatElapsed(clockInIso: string, nowMs: number): string {
  const startMs = Date.parse(clockInIso);
  if (!Number.isFinite(startMs)) return '--:--:--';
  const totalSeconds = Math.max(0, Math.floor((nowMs - startMs) / 1000));
  const h = Math.floor(totalSeconds / 3600);
  const m = Math.floor((totalSeconds % 3600) / 60);
  const s = totalSeconds % 60;
  return [h, m, s].map((n) => String(n).padStart(2, '0')).join(':');
}

/** h:mm elapsed (e.g. "2:10") — the compact form the COMPLETE confirm dialog uses. */
export function formatElapsedShort(clockInIso: string, nowMs: number): string {
  const startMs = Date.parse(clockInIso);
  if (!Number.isFinite(startMs)) return '-:--';
  const totalMinutes = Math.max(0, Math.floor((nowMs - startMs) / 60_000));
  const h = Math.floor(totalMinutes / 60);
  const m = totalMinutes % 60;
  return `${h}:${String(m).padStart(2, '0')}`;
}

/**
 * Surface a backend error VERBATIM (sequence/predecessor gating, holds, locks).
 * Suppressing or rewording these is a compliance bug — the operator must see
 * exactly why the system refused.
 */
export function kioskErrorMessage(err: unknown, fallback: string): string {
  const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof detail === 'string' && detail.trim()) return detail;
  if (detail != null) {
    try {
      return JSON.stringify(detail);
    } catch {
      // fall through to message/fallback
    }
  }
  const message = (err as { message?: unknown })?.message;
  if (typeof message === 'string' && message.trim()) return message;
  return fallback;
}
