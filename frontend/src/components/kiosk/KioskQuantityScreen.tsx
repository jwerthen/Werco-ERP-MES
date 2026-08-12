import React, { useEffect, useMemo, useState } from 'react';
import KioskKeypad from './KioskKeypad';
import KioskReasonGrid from './KioskReasonGrid';
import { activeScrapCodes, resolveScrapSelection, scrapReasonTiles } from './scrapReasonOptions';
import { applyQuickAdd, kioskQuickAdds, QUICK_ADD_BUTTON_CLASSES } from './quantityQuickAdds';
import { ScrapReasonCodeOption } from '../../types/scrapReason';

interface KioskQuantityScreenProps {
  title: string;
  /** e.g. "WO-2026-0142 · Op 20 Deburr" */
  jobLabel: string;
  confirmLabel: string;
  /** Prefill for the GOOD field (e.g. remaining qty on COMPLETE). */
  initialGood?: number;
  /** When true (production report) the backend rejects 0/0, so block it client-side too. */
  requireTotalPositive: boolean;
  /**
   * Crew-station double-count guard: the operation-level tally, e.g.
   * "CREW TOTAL SO FAR: 37 of 50 · 2 scrap — enter only NEW pieces".
   */
  tallyBanner?: string;
  /**
   * Company-managed scrap reason codes (Lean Phase 1). Non-empty -> the scrap
   * reason grid is built from these ("CODE — Name" tiles, required) plus an
   * OPTIONAL free-text detail line. Empty/null -> legacy hardcoded
   * SCRAP_REASONS grid (companies without codes keep the old flow). The crew
   * station feeds this from the queue payload's `scrap_reason_codes`; the
   * operator kiosk from GET /quality/scrap-reason-codes.
   */
  scrapCodes?: ScrapReasonCodeOption[] | null;
  /**
   * OPT-IN quick-add row (`+1 / +5 / +25`, plus `Full nest n`) over the GOOD
   * field — omit it and the screen renders exactly as before.
   *
   * The opt-in IS the ceiling, deliberately: this number is the largest good
   * quantity the SERVER will take from this screen, so a caller that cannot work
   * one out cannot accidentally ship an unbounded row. Both writers behind this
   * screen refuse over-target good quantity before any mutation —
   * `POST /shop-floor/operations/{id}/production` with 400 "Quantity (N) cannot
   * exceed quantity ordered (T)" and `POST /shop-floor/clock-out/{id}` with 400
   * "Quantity produced exceeds quantity ordered" — both measured as
   * `operation.quantity_complete + delta > operation_target_quantity(...)`, so
   * the ceiling is the operation target less what is already recorded. Per the
   * repo's non-optimistic convention for server-gated actions, the row clamps
   * there and goes disabled rather than keying a guaranteed refusal.
   *
   * The keypad is never bounded by this — an operator can still key any figure
   * and take the server's answer. This bounds the CONVENIENCE only.
   */
  quickAddCeiling?: number | null;
  /**
   * The operation's per-item target (`component_quantity`) — adds a `Full nest n`
   * tap to the row. Ignored unless `quickAddCeiling` is given.
   */
  fullNestQuantity?: number | null;
  busy: boolean;
  /**
   * scrapReason: free text stored in TimeEntry.scrap_reason (legacy tile value,
   * or the typed detail in codes mode). scrapReasonCodeId: the structured code
   * (codes mode only). Both null when scrap is 0.
   */
  onConfirm: (good: number, scrap: number, scrapReason: string | null, scrapReasonCodeId: number | null) => void;
  onCancel: () => void;
}

/**
 * Shared quantity-entry screen for REPORT PRODUCTION and COMPLETE.
 * Numbers come from the big keypad, plus — when the caller passes a
 * `quickAddCeiling` — a GOOD-only quick-add row (no native inputs/spinners).
 * Any scrap quantity REQUIRES an explicit reason — the confirm button stays
 * disabled until one is chosen. With company scrap codes the choice is a code
 * tile (+ optional typed detail); without them it is the legacy reason tile.
 */
export default function KioskQuantityScreen({
  title,
  jobLabel,
  confirmLabel,
  initialGood,
  requireTotalPositive,
  tallyBanner,
  scrapCodes,
  quickAddCeiling,
  fullNestQuantity,
  busy,
  onConfirm,
  onCancel,
}: KioskQuantityScreenProps) {
  const [good, setGood] = useState(initialGood != null && initialGood > 0 ? String(initialGood) : '');
  const [scrap, setScrap] = useState('');
  const [activeField, setActiveField] = useState<'good' | 'scrap'>('good');
  // Legacy mode: the chosen SCRAP_REASONS value. Codes mode: the chosen code id (as string).
  const [scrapReason, setScrapReason] = useState<string | null>(null);
  const [scrapDetail, setScrapDetail] = useState('');

  const codes = activeScrapCodes(scrapCodes);
  const codesMode = codes != null;
  const reasonTiles = useMemo(() => scrapReasonTiles(codes), [codes]);

  // If the codes list settles AFTER a reason tile was already tapped (rare —
  // codes are fetched at page mount), the stored value belongs to the other
  // vocabulary; clear it rather than submit a mismatched reason/id.
  useEffect(() => {
    setScrapReason(null);
  }, [codesMode]);

  const goodQty = Number(good || 0);
  const scrapQty = Number(scrap || 0);
  const needsReason = scrapQty > 0 && !scrapReason;
  const totalInvalid = requireTotalPositive && goodQty <= 0 && scrapQty <= 0;
  const confirmDisabled = busy || needsReason || totalInvalid;

  // Quick adds: the same row, off the same definition (quantityQuickAdds.ts), as
  // the single-operator kiosk's REPORT and COMPLETE overlays — an operator who
  // learns `+25` on one station must find it in the same place, in the same
  // order, meaning the same thing, on the other.
  const quickAddsEnabled = quickAddCeiling != null && Number.isFinite(Number(quickAddCeiling));
  const quickAddCap = Math.max(0, Number(quickAddCeiling ?? 0));
  const quickAdds = kioskQuickAdds(fullNestQuantity);
  const quickAddsExhausted = goodQty >= quickAddCap;

  const handleQuickAdd = (amount: number) => {
    setGood(String(applyQuickAdd(goodQty, amount, quickAddCap)));
    // A quick add is unambiguously a GOOD entry, so point the shared keypad at
    // GOOD — otherwise a scrap-bound keypad would take the operator's next digit
    // into the field they just steered away from. Both quantity fields are on
    // screen at once here, so that is a real mis-post, not a theoretical one.
    setActiveField('good');
  };

  const handleConfirm = () => {
    if (scrapQty <= 0) {
      onConfirm(goodQty, scrapQty, null, null);
      return;
    }
    const { reason, codeId } = resolveScrapSelection(codes, scrapReason, scrapDetail);
    onConfirm(goodQty, scrapQty, reason, codeId);
  };

  const fieldClasses = (field: 'good' | 'scrap', tone: 'green' | 'red') => {
    const active = activeField === field;
    if (tone === 'green') {
      return active ? 'border-fd-green bg-fd-green/10 text-fd-green' : 'border-fd-line bg-fd-sunken text-fd-body';
    }
    return active ? 'border-fd-red bg-fd-red/10 text-fd-red' : 'border-fd-line bg-fd-sunken text-fd-body';
  };

  return (
    <section aria-label={title} className="mx-auto w-full max-w-2xl">
      <h2 className="text-3xl font-bold text-fd-ink">{title}</h2>
      <p className="mt-1 font-mono text-lg text-fd-mute">{jobLabel}</p>

      {tallyBanner && (
        <p
          data-testid="kiosk-tally-banner"
          className="mt-4 rounded border border-fd-blue/50 bg-fd-blue/10 px-4 py-3 text-center font-mono text-xl font-bold text-fd-blue"
        >
          {tallyBanner}
        </p>
      )}

      <div className="mt-5 grid grid-cols-2 gap-3">
        <button
          type="button"
          data-testid="kiosk-qty-good"
          aria-pressed={activeField === 'good'}
          onClick={() => setActiveField('good')}
          className={`min-h-24 rounded border px-4 py-3 text-left transition-colors ${fieldClasses('good', 'green')}`}
        >
          <span className="block text-sm font-bold uppercase tracking-[0.2em]">Good</span>
          <span className="mt-1 block font-mono text-5xl font-bold tabular-nums">{good || '0'}</span>
        </button>
        <button
          type="button"
          data-testid="kiosk-qty-scrap"
          aria-pressed={activeField === 'scrap'}
          onClick={() => setActiveField('scrap')}
          className={`min-h-24 rounded border px-4 py-3 text-left transition-colors ${fieldClasses('scrap', 'red')}`}
        >
          <span className="block text-sm font-bold uppercase tracking-[0.2em]">Scrap</span>
          <span className="mt-1 block font-mono text-5xl font-bold tabular-nums">{scrap || '0'}</span>
        </button>
      </div>

      <div className="mt-4">
        <KioskKeypad
          value={activeField === 'good' ? good : scrap}
          onChange={activeField === 'good' ? setGood : setScrap}
          maxLength={5}
          disabled={busy}
        />
      </div>

      {/* Quick adds — GOOD only. Two quantity fields sit side by side on this
          screen, so the row is captioned and every button names its target: a
          quick add must never be mistaken for (or silently write to) scrap.
          There is deliberately no scrap quick-add — scrap takes a reason and a
          deliberate entry.

          BELOW the keypad, unlike the narrow modal overlays that stack it above
          one. This screen is a full page on a shared iPad and its stack is
          taller: measured at 1024x768 (landscape iPad), the row above the keypad
          pushed the keypad's bottom row — CLEAR / 0 / backspace — 49px under the
          fold, and a convenience that hides part of the primary input is a bad
          trade. Here the keypad stays whole at both tablet orientations and the
          quick adds are what falls to a scroll in landscape, on a screen whose
          CONFIRM already sat below the fold. Anything added above the keypad on
          this screen owes the same measurement. */}
      {quickAddsEnabled && (
        // `mt-2.5` is the keypad's own inter-key gap, so the row reads as one
        // more pad row rather than as part of the CANCEL/CONFIRM action band it
        // now sits above — a gloved thumb reaching for CONFIRM must not land on
        // a quick add on the way.
        <div className="mt-2.5" data-testid="kiosk-qty-quickadds">
          <p
            id="kiosk-qty-quickadd-label"
            data-testid="kiosk-qty-quickadd-label"
            className="mb-1.5 font-mono text-xs font-bold uppercase tracking-[0.16em] text-fd-mute"
          >
            {quickAddCap > 0
              ? `Quick add to good · max ${quickAddCap}`
              : 'Quick add to good · operation is already at its target'}
          </p>
          <div className="flex gap-2" role="group" aria-labelledby="kiosk-qty-quickadd-label">
            {quickAdds.map((qa) => (
              <button
                key={qa.label}
                type="button"
                aria-label={`Add ${qa.label} to good`}
                disabled={busy || quickAddsExhausted}
                onClick={() => handleQuickAdd(qa.amount)}
                className={QUICK_ADD_BUTTON_CLASSES}
              >
                {qa.label}
              </button>
            ))}
          </div>
        </div>
      )}

      {scrapQty > 0 && (
        <div className="mt-5">
          <p className="mb-2 text-lg font-semibold text-fd-red">Scrap reason — required</p>
          <KioskReasonGrid reasons={reasonTiles} selected={scrapReason} onSelect={setScrapReason} disabled={busy} tone="red" />
          {codes && (
            <div className="mt-3">
              <label htmlFor="kiosk-scrap-detail" className="mb-1 block text-base font-semibold text-fd-mute">
                Detail — optional
              </label>
              <input
                id="kiosk-scrap-detail"
                data-testid="kiosk-scrap-detail"
                type="text"
                maxLength={255}
                disabled={busy}
                value={scrapDetail}
                onChange={(e) => setScrapDetail(e.target.value)}
                placeholder="What happened?"
                className="w-full rounded border border-fd-line bg-fd-sunken px-4 py-3 text-xl text-fd-ink placeholder:text-fd-mute focus:border-fd-red focus:outline-none disabled:opacity-40"
              />
            </div>
          )}
        </div>
      )}

      <div className="mt-6 grid grid-cols-2 gap-3">
        <button
          type="button"
          onClick={onCancel}
          disabled={busy}
          className="min-h-20 rounded border border-fd-line bg-fd-sunken text-xl font-bold uppercase tracking-wide text-fd-body transition-colors hover:border-fd-line-bright disabled:opacity-40"
        >
          Cancel
        </button>
        <button
          type="button"
          data-testid="kiosk-qty-confirm"
          onClick={handleConfirm}
          disabled={confirmDisabled}
          className="min-h-20 rounded border border-fd-green bg-fd-green/15 text-xl font-bold uppercase tracking-wide text-fd-green transition-colors hover:bg-fd-green/25 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {busy ? 'Saving…' : confirmLabel}
        </button>
      </div>
      {needsReason && <p className="mt-3 text-center text-base text-fd-red">Choose a scrap reason to continue.</p>}
    </section>
  );
}
