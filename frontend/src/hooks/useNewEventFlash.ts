/**
 * useNewEventFlash — the ONLY flashing on the wallboard (spec motion budget
 * item 5): a tile newly entering DOWN/BLOCKED, or an exception-rail row newly
 * appearing, flashes for ~10s (CSS: 1.2s steps ×8) then settles to steady.
 *
 * Diffs the current poll against the previous poll strictly by STABLE ids
 * (work-center id, WO number, blocker identity) — never by list index — and
 * SUPPRESSES on first paint and on ?dept= change (reset via `resetKey`), so a
 * fresh page/token re-mint or a dept switch never lights up the whole board.
 */

import { useRef } from 'react';
import type { WallboardResponse } from '../types/wallboard';

export function collectEventKeys(payload: WallboardResponse): Set<string> {
  const keys = new Set<string>();
  for (const wc of payload.work_centers) {
    if (wc.down !== null) keys.add(`wc-down:${wc.id}`);
    if (wc.blocked_count > 0) keys.add(`wc-blocked:${wc.id}`);
  }
  for (const wo of payload.late_wos) keys.add(`late:${wo.wo_number}`);
  for (const wo of payload.blocked_wos) keys.add(`blocked:${wo.wo_number}`);
  for (const wc of payload.work_centers) {
    if (wc.down !== null) keys.add(`down:${wc.id}:${wc.down.category}`);
  }
  return keys;
}

const EMPTY = new Set<string>();

interface FlashRef {
  resetKey: string;
  payload: WallboardResponse | null;
  prevKeys: Set<string> | null;
  newKeys: Set<string>;
}

/**
 * Returns the set of event keys that are NEW as of the current payload.
 * Render-time ref mutation is guarded by payload identity, so StrictMode
 * double-renders and unrelated re-renders (clock ticks) can't re-diff.
 */
export function useNewEventFlash(payload: WallboardResponse | null, resetKey: string): Set<string> {
  const ref = useRef<FlashRef>({
    resetKey,
    payload: null,
    prevKeys: null,
    newKeys: EMPTY,
  });

  if (ref.current.resetKey !== resetKey) {
    // ?dept= change (or any remount-equivalent reset): suppress — the next
    // payload establishes a fresh baseline instead of flashing everything.
    ref.current = { resetKey, payload: null, prevKeys: null, newKeys: EMPTY };
  }

  if (payload !== null && payload !== ref.current.payload) {
    const currentKeys = collectEventKeys(payload);
    const prev = ref.current.prevKeys;
    let newKeys: Set<string>;
    if (prev === null) {
      // First paint after mount/reset: everything is baseline, nothing flashes.
      newKeys = EMPTY;
    } else {
      const added = new Set<string>();
      currentKeys.forEach(key => {
        if (!prev.has(key)) added.add(key);
      });
      newKeys = added;
    }
    ref.current = { resetKey, payload, prevKeys: currentKeys, newKeys };
  }

  return ref.current.newKeys;
}
