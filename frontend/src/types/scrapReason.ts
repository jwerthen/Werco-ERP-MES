/**
 * Scrap reason codes (Lean Phase 1 / issue #88).
 *
 * Company-managed vocabulary for categorizing scrap, replacing the hardcoded
 * SCRAP_REASONS list wherever the company has defined codes. Managed at
 * Quality → Scrap Codes (writes are ADMIN/MANAGER/QUALITY); consumed by every
 * scrap-entry surface (kiosks, desktop shop floor, WO complete dialogs).
 *
 * Retirement is a flag (`is_active=false`), never a delete — historical scrap
 * rows reference these ids for traceability.
 */

export interface ScrapReasonCode {
  id: number;
  code: string;
  name: string;
  category: string;
  description?: string | null;
  is_active: boolean;
  display_order: number;
}

/** Backend ScrapCategory enum values (models/scrap_reason.py). */
export const SCRAP_CATEGORIES = [
  'material',
  'machine',
  'tooling',
  'operator',
  'setup',
  'programming',
  'engineering',
  'supplier',
  'handling',
  'other',
] as const;

export type ScrapCategory = (typeof SCRAP_CATEGORIES)[number];

/**
 * The picker-facing subset of a scrap reason code. This is exactly what the
 * crew-station queue payload carries (GET /shop-floor/work-center-queue/{id}
 * → `scrap_reason_codes`: active-only, display_order-then-code sorted, so
 * `is_active`/`description` are omitted server-side). The full ScrapReasonCode
 * is structurally assignable, so session-authed surfaces pass their fetched
 * codes straight through.
 */
export type ScrapReasonCodeOption = Pick<ScrapReasonCode, 'id' | 'code' | 'name' | 'category' | 'display_order'>;

/** "CODE — Name" — the canonical option label for scrap-code pickers. */
export function scrapCodeLabel(code: Pick<ScrapReasonCode, 'code' | 'name'>): string {
  return `${code.code} — ${code.name}`;
}
