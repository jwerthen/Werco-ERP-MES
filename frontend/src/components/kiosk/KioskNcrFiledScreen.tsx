import React from 'react';
import { ShieldExclamationIcon } from '@heroicons/react/24/solid';
import type { QualityHoldResult } from '../../types/processSheet';

interface KioskNcrFiledScreenProps {
  result: QualityHoldResult;
  /** e.g. "WO-2026-0142 · Op 20 Deburr" */
  jobLabel: string;
  /** "Back to queue" (operator kiosk) / "Back to board" (crew station). */
  doneLabel: string;
  onDone: () => void;
}

/**
 * Full-screen confirmation after the one-tap OOT quality hold (PR 4): the NCR
 * number must stay readable long enough for the operator to tag the part, so
 * this is a dedicated host view (NOT a 3-second toast) with a single exit that
 * lands exactly where the existing HOLD flow lands. Both kiosks share it.
 */
export default function KioskNcrFiledScreen({ result, jobLabel, doneLabel, onDone }: KioskNcrFiledScreenProps) {
  const closedCount = result.closed_time_entry_ids.length;
  return (
    <section aria-label="NCR filed" className="mx-auto w-full max-w-2xl text-center">
      <ShieldExclamationIcon className="mx-auto h-16 w-16 text-fd-amber" aria-hidden="true" />
      <h2 className="mt-3 text-3xl font-bold text-fd-ink">Operation on hold</h2>
      <p className="mt-1 font-mono text-lg text-fd-mute">{jobLabel}</p>

      <div className="mt-5 rounded border-2 border-fd-amber bg-fd-amber/10 px-5 py-6">
        <p className="font-mono text-xs font-bold uppercase tracking-[0.25em] text-fd-amber">
          Non-conformance report filed
        </p>
        <p data-testid="kiosk-ncr-number" className="mt-2 font-mono text-5xl font-bold tracking-tight text-fd-ink">
          {result.ncr_number}
        </p>
        <p className="mt-3 text-xl text-fd-body">
          {result.ncr_number} filed — this operation is on hold for quality review.
        </p>
        <p className="mt-2 text-lg text-fd-mute">
          Tag the part with the NCR number and set it aside.
          {closedCount > 0 &&
            ` ${closedCount === 1 ? 'The open labor entry was' : `All ${closedCount} open labor entries were`} clocked out automatically.`}
        </p>
      </div>

      <button
        type="button"
        data-testid="kiosk-ncr-done"
        onClick={onDone}
        className="mt-6 min-h-20 w-full rounded border border-fd-line bg-fd-sunken text-2xl font-bold uppercase tracking-wide text-fd-body transition-colors hover:border-fd-line-bright"
      >
        {doneLabel}
      </button>
    </section>
  );
}
