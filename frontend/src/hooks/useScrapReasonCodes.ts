/**
 * useScrapReasonCodes — fetch the company's ACTIVE scrap reason codes once per
 * mount for the scrap-entry pickers (Lean Phase 1 / issue #88).
 *
 * Deliberately fail-soft: any load failure resolves to an empty list, which
 * every consumer treats as "this company has no codes" and falls back to the
 * legacy hardcoded SCRAP_REASONS behavior — a codes outage must never brick a
 * scrap-entry flow on the shop floor. No toast on failure for the same reason.
 */

import { useEffect, useState } from 'react';
import api from '../services/api';
import type { ScrapReasonCode } from '../types/scrapReason';

export interface UseScrapReasonCodesResult {
  /** Active codes in display_order (empty = fallback to legacy behavior). */
  codes: ScrapReasonCode[];
  /** True once the fetch settled (either way). */
  loaded: boolean;
}

export function useScrapReasonCodes(enabled = true): UseScrapReasonCodesResult {
  const [codes, setCodes] = useState<ScrapReasonCode[]>([]);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    if (!enabled) return undefined;
    let cancelled = false;
    (async () => {
      try {
        const result = await api.getScrapReasonCodes();
        if (!cancelled) setCodes(Array.isArray(result) ? result.filter((c) => c.is_active) : []);
      } catch {
        // Fail-soft: empty list -> legacy scrap-reason behavior.
        if (!cancelled) setCodes([]);
      } finally {
        if (!cancelled) setLoaded(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [enabled]);

  return { codes, loaded };
}

export default useScrapReasonCodes;
