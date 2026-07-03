import React, { useState } from 'react';
import KioskKeypad from './KioskKeypad';
import KioskReasonGrid from './KioskReasonGrid';
import { SCRAP_REASONS } from './kioskConstants';

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
  busy: boolean;
  onConfirm: (good: number, scrap: number, scrapReason: string | null) => void;
  onCancel: () => void;
}

/**
 * Shared quantity-entry screen for REPORT PRODUCTION and COMPLETE.
 * Numbers come ONLY from the big keypad (no native inputs/spinners).
 * Any scrap quantity REQUIRES an explicit reason — the confirm button stays
 * disabled until one is chosen; there is no default and no free text.
 */
export default function KioskQuantityScreen({
  title,
  jobLabel,
  confirmLabel,
  initialGood,
  requireTotalPositive,
  tallyBanner,
  busy,
  onConfirm,
  onCancel,
}: KioskQuantityScreenProps) {
  const [good, setGood] = useState(initialGood != null && initialGood > 0 ? String(initialGood) : '');
  const [scrap, setScrap] = useState('');
  const [activeField, setActiveField] = useState<'good' | 'scrap'>('good');
  const [scrapReason, setScrapReason] = useState<string | null>(null);

  const goodQty = Number(good || 0);
  const scrapQty = Number(scrap || 0);
  const needsReason = scrapQty > 0 && !scrapReason;
  const totalInvalid = requireTotalPositive && goodQty <= 0 && scrapQty <= 0;
  const confirmDisabled = busy || needsReason || totalInvalid;

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

      {scrapQty > 0 && (
        <div className="mt-5">
          <p className="mb-2 text-lg font-semibold text-fd-red">Scrap reason — required</p>
          <KioskReasonGrid reasons={SCRAP_REASONS} selected={scrapReason} onSelect={setScrapReason} disabled={busy} tone="red" />
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
          onClick={() => onConfirm(goodQty, scrapQty, scrapQty > 0 ? scrapReason : null)}
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
