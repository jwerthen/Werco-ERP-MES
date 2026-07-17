import React, { useState } from 'react';
import KioskKeypad from './KioskKeypad';
import KioskReasonGrid from './KioskReasonGrid';
import { CORRECTION_REASONS } from './kioskConstants';

interface KioskCorrectionScreenProps {
  /** e.g. "WO-2026-0142 · Op 20 Deburr" */
  jobLabel: string;
  /**
   * Crew-station tally, e.g. "CREW TOTAL SO FAR: 37 of 50" — a reminder of the
   * operation total the walk-back applies against.
   */
  tallyBanner?: string;
  busy: boolean;
  /**
   * Emits the positive quantity to REMOVE plus the required correction reason
   * (verbatim label). The caller submits to reduce-production and stays
   * non-optimistic — nothing here mutates the count.
   */
  onConfirm: (quantity: number, reason: string) => void;
  onCancel: () => void;
}

/**
 * Over-count correction screen (reduce-production) — remove good pieces the
 * operator OVER-reported on the job they are actively working. This is a
 * miscount fix, NOT scrap: a single amber "remove" quantity from the digits-only
 * keypad (no minus key) plus a REQUIRED reason tile. Confirm stays disabled until
 * a positive quantity AND a reason are chosen; the server enforces the real
 * bound (≤ what this operator recorded on their own open clock-in).
 */
export default function KioskCorrectionScreen({
  jobLabel,
  tallyBanner,
  busy,
  onConfirm,
  onCancel,
}: KioskCorrectionScreenProps) {
  const [qty, setQty] = useState('');
  const [reason, setReason] = useState<string | null>(null);

  const removeQty = Number(qty || 0);
  const needsReason = !reason;
  const confirmDisabled = busy || removeQty <= 0 || needsReason;

  const handleConfirm = () => {
    if (removeQty <= 0 || !reason) return;
    onConfirm(removeQty, reason);
  };

  return (
    <section aria-label="Correct over-count" className="mx-auto w-full max-w-2xl">
      <h2 className="text-3xl font-bold text-fd-ink">Correct over-count</h2>
      <p className="mt-1 font-mono text-lg text-fd-mute">{jobLabel}</p>
      <p className="mt-3 text-base text-fd-body">
        Remove good pieces you over-reported on this job. This is a miscount correction, not scrap.
      </p>

      {tallyBanner && (
        <p
          data-testid="kiosk-correct-tally-banner"
          className="mt-4 rounded border border-fd-blue/50 bg-fd-blue/10 px-4 py-3 text-center font-mono text-xl font-bold text-fd-blue"
        >
          {tallyBanner}
        </p>
      )}

      <div className="mt-5">
        <div
          data-testid="kiosk-correct-remove"
          className="min-h-24 rounded border border-fd-amber bg-fd-amber/10 px-4 py-3 text-left text-fd-amber"
        >
          <span className="block text-sm font-bold uppercase tracking-[0.2em]">Remove</span>
          <span className="mt-1 block font-mono text-5xl font-bold tabular-nums">{qty || '0'}</span>
        </div>
      </div>

      <div className="mt-4">
        <KioskKeypad value={qty} onChange={setQty} maxLength={5} disabled={busy} idPrefix="kiosk-correct-key" />
      </div>

      <div className="mt-5">
        <p className="mb-2 text-lg font-semibold text-fd-amber">Reason — required</p>
        <KioskReasonGrid reasons={CORRECTION_REASONS} selected={reason} onSelect={setReason} disabled={busy} tone="amber" />
      </div>

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
          data-testid="kiosk-correct-confirm"
          onClick={handleConfirm}
          disabled={confirmDisabled}
          className="min-h-20 rounded border border-fd-amber bg-fd-amber/15 text-xl font-bold uppercase tracking-wide text-fd-amber transition-colors hover:bg-fd-amber/25 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {busy ? 'Saving…' : 'Remove'}
        </button>
      </div>
      {removeQty > 0 && needsReason && (
        <p className="mt-3 text-center text-base text-fd-amber">Choose a reason to continue.</p>
      )}
    </section>
  );
}
