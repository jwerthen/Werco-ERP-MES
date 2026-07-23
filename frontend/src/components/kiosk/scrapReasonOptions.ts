/**
 * Shared scrap-reason vocabulary logic (Lean Phase 1 codes-or-legacy), extracted
 * from KioskQuantityScreen so the Foundry report modal and the complete modal
 * reuse ONE implementation instead of forking it.
 *
 * Contract (locked by KioskQuantityScreen.test.tsx and the kiosk page tests):
 *  - Company codes present  -> tiles are "CODE — Name" (value = String(code.id));
 *    the emitted reason text is the OPTIONAL typed detail (null when blank) and
 *    the structured `scrap_reason_code_id` carries the reason.
 *  - No codes (legacy)      -> tiles are the hardcoded SCRAP_REASONS; the emitted
 *    reason text is the tile label verbatim and the code id is null.
 */

import { ScrapReasonCodeOption, scrapCodeLabel } from '../../types/scrapReason';
import { KioskReason, SCRAP_REASONS } from './kioskConstants';

/** Non-empty codes list, or null → legacy mode. */
export function activeScrapCodes(
  scrapCodes?: ScrapReasonCodeOption[] | null
): ScrapReasonCodeOption[] | null {
  return scrapCodes && scrapCodes.length > 0 ? scrapCodes : null;
}

/** The reason tile vocabulary for the current mode. */
export function scrapReasonTiles(codes: ScrapReasonCodeOption[] | null): KioskReason[] {
  return codes ? codes.map((code) => ({ value: String(code.id), label: scrapCodeLabel(code) })) : SCRAP_REASONS;
}

export interface ScrapReasonSelection {
  /** Free text stored in TimeEntry.scrap_reason (legacy tile value, or typed detail in codes mode). */
  reason: string | null;
  /** Structured company code id (codes mode only). */
  codeId: number | null;
}

/** Resolve the tapped tile value (+ optional typed detail) into the payload pair. */
export function resolveScrapSelection(
  codes: ScrapReasonCodeOption[] | null,
  selected: string | null,
  detail: string
): ScrapReasonSelection {
  if (codes) {
    const trimmed = detail.trim();
    return { reason: trimmed || null, codeId: selected != null ? Number(selected) : null };
  }
  return { reason: selected, codeId: null };
}

/**
 * OPEN NCR default heuristic (Kiosk Foundry redesign, decision 5).
 *
 * Deliberately CONSERVATIVE — the toggle defaults ON only when the selected
 * reason clearly describes nonconforming material/quality (the classic
 * NCR-worthy case), and OFF for everything else (process noise like setup
 * pieces, mis-keys, handling). The operator can always flip it either way:
 *  - codes mode: the company code's category is `material` or `supplier`
 *    (defective raw/incoming material);
 *  - legacy grid: the "Material defect" tile only.
 */
export function isQualityRelatedScrapSelection(
  codes: ScrapReasonCodeOption[] | null,
  selected: string | null
): boolean {
  if (!selected) return false;
  if (codes) {
    const code = codes.find((c) => String(c.id) === selected);
    return code != null && (code.category === 'material' || code.category === 'supplier');
  }
  return selected === 'Material defect';
}
