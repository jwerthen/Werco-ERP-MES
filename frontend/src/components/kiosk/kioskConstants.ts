/**
 * Operator kiosk (A0.3) shared constants.
 *
 * Compliance notes:
 * - Every mutating call from the kiosk reports `source: "kiosk"` — the A0.1
 *   adoption-telemetry channel. Use KIOSK_SOURCE, never a string literal.
 * - Scrap ALWAYS requires an explicit reason chosen from SCRAP_REASONS
 *   (no default, no free text). The chosen label is stored verbatim in the
 *   TimeEntry.scrap_reason column (free string, 255 max) on clock-out, and is
 *   prefixed into `notes` for in-shift production reports (that endpoint has
 *   no structured scrap_reason field yet).
 * - Hold reasons mirror the backend WorkOrderBlockerCategory enum
 *   (backend/app/models/work_order_blocker.py) so a kiosk hold files the same
 *   structured blocker a supervisor would.
 */

export const KIOSK_SOURCE = 'kiosk';

export interface KioskReason {
  value: string;
  label: string;
}

/** Shop-standard scrap reasons. Stored verbatim as TimeEntry.scrap_reason. */
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
