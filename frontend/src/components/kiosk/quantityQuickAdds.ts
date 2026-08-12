/**
 * The kiosk quantity QUICK-ADD row — one definition, every kiosk screen.
 *
 * REPORT (in-shift good pieces) and COMPLETE (the clock-out final entry) sit one
 * tap apart on the same station, so an operator who learns `+25` on one has to
 * find it in the same order, meaning the same thing, on the other — and the crew
 * station (`KioskQuantityScreen`, which serves LEAVE / REPORT / COMPLETE there)
 * is the same operators on a shared iPad. Hand-rolled copies are exactly how
 * that stops being true, so the amounts, the labels, the clamp and the button
 * chrome all live here and every caller imports them.
 *
 * What is NOT fixed here is where the row sits on the page: the two narrow modal
 * overlays stack it above their keypad, while the full-page crew screen puts it
 * below, because measured at 1024x768 (landscape iPad) an 82px row above that
 * screen's taller stack pushed the keypad's CLEAR / 0 / backspace row under the
 * fold. Position follows the measurement; the row itself does not vary.
 *
 * Every caller applies these to its GOOD quantity only. There is deliberately no
 * scrap quick-add: scrap requires an explicit reason and a deliberate entry.
 */

export interface KioskQuickAdd {
  label: string;
  amount: number;
}

/**
 * Ceiling for a quick-added quantity. Matches `KioskKeypad`'s `maxLength={5}` at
 * both call sites — a quick add must not be able to reach a number the keypad
 * itself would refuse to key.
 */
export const QUICK_ADD_MAX = 99999;

/**
 * `+1 / +5 / +25`, plus a `Full nest {n}` tap when the active operation carries
 * a per-item target worth one (`component_quantity`). Only when it is > 1 — a
 * target of 1 is already the `+1` button (Foundry decision 4).
 */
export function kioskQuickAdds(fullNestQuantity?: number | null): KioskQuickAdd[] {
  const full = Number(fullNestQuantity);
  return [
    { label: '+1', amount: 1 },
    { label: '+5', amount: 5 },
    { label: '+25', amount: 25 },
    ...(fullNestQuantity != null && full > 1 ? [{ label: `Full nest ${full}`, amount: full }] : []),
  ];
}

/**
 * A quick add applied to the current quantity, clamped. Never yields NaN.
 *
 * `ceiling` is the largest value the SERVER will accept for this field, when the
 * caller knows it. BOTH writers behind these screens refuse an over-target good
 * quantity before any mutation, measured the same way —
 * `operation.quantity_complete + delta > operation_target_quantity(op, wo)`:
 * clock-out with 400 "Quantity produced exceeds quantity ordered" and
 * `POST /operations/{id}/production` with 400 "Quantity (N) cannot exceed
 * quantity ordered (T)" (backend/app/api/endpoints/shop_floor.py). So the
 * ceiling is the operation target less what is already recorded, and per the
 * repo's non-optimistic convention a server-gated field must never be put into a
 * state the server would refuse.
 *
 * The COMPLETE modal passes it (its good field pre-fills at exactly that
 * ceiling, so an unbounded `+1` would be a guaranteed refusal that aborts the
 * whole completion). `KioskQuantityScreen` passes it on all three crew-station
 * screens, and treats its absence as "no row at all" — a caller that cannot work
 * out the ceiling gets no quick adds rather than unbounded ones. The REPORT
 * modal passes none: its good field counts up from 0 against a ceiling the modal
 * is not given, and its own gating is unchanged by this row.
 */
export function applyQuickAdd(current: number, amount: number, ceiling?: number | null): number {
  const base = Number.isFinite(current) ? current : 0;
  const cap = ceiling != null && Number.isFinite(ceiling) ? Math.min(QUICK_ADD_MAX, ceiling) : QUICK_ADD_MAX;
  return Math.min(cap, base + amount);
}

/**
 * Quick-add button chrome: 44px touch target (gloves), kiosk `fd-*` tokens only.
 * Kept here so the two rows can't drift visually either.
 */
export const QUICK_ADD_BUTTON_CLASSES =
  'font-mono h-11 min-w-0 flex-1 rounded-[3px] border border-fd-line bg-fd-raised px-1 ' +
  'text-[13px] font-semibold uppercase tracking-[0.04em] text-fd-body transition-transform ' +
  'duration-150 ease-out active:scale-[0.98] disabled:opacity-40';
