import React, { useState } from 'react';
import { IdentificationIcon } from '@heroicons/react/24/solid';
import { Modal } from '../ui/Modal';
import KioskKeypad from './KioskKeypad';
import { useBadgeCapture } from './useBadgeCapture';
import { KioskRosterEntry, UNKNOWN_OPERATOR_LABEL, formatElapsedShort } from './kioskConstants';

interface KioskCompleteConfirmModalProps {
  open: boolean;
  /** e.g. "WO-2026-0142 · Op 20 Deburr" */
  jobLabel: string;
  /** Live roster from queue state — re-derived on every poll while open. */
  roster: KioskRosterEntry[];
  /** Skew-corrected "now" for the per-person elapsed durations. */
  nowMs: number;
  /** Final NEW pieces entered on the quantity screen (reported before complete). */
  pendingGood: number;
  pendingScrap: number;
  busy: boolean;
  /** Server rejection (bad badge, gating detail) — shown verbatim, modal stays open. */
  error: string | null;
  onCancel: () => void;
  /** Badge scanned/typed inside the modal — the signature that fires the verb. */
  onBadge: (badgeId: string) => void;
}

/**
 * COMPLETE is a crew-wide verb: the server closes EVERY open time entry on the
 * operation. This dialog makes that explicit — it names who else gets
 * auto-clocked-out (with their running durations) and takes the badge scan
 * that signs the completion. While it is open it OWNS the scanner (the page
 * behind it must disable its own badge capture).
 */
export default function KioskCompleteConfirmModal({
  open,
  jobLabel,
  roster,
  nowMs,
  pendingGood,
  pendingScrap,
  busy,
  error,
  onCancel,
  onBadge,
}: KioskCompleteConfirmModalProps) {
  const [badge, setBadge] = useState('');

  useBadgeCapture({
    enabled: open && !busy,
    value: badge,
    onValueChange: setBadge,
    onSubmit: (raw) => {
      const id = raw.trim();
      if (!id) return;
      setBadge('');
      onBadge(id);
    },
  });

  return (
    <Modal
      open={open}
      onClose={onCancel}
      size="xl"
      closeOnBackdrop={false}
      ariaLabelledBy="kiosk-complete-title"
      // The shared Modal portals to document.body — outside the page's
      // .fd-scope-kiosk wrapper — so the scope class rides the panel itself.
      className="fd-scope-kiosk"
    >
      <h2 id="kiosk-complete-title" className="text-3xl font-bold text-fd-ink">
        Complete job?
      </h2>
      <p className="mt-1 font-mono text-lg text-fd-mute">{jobLabel}</p>

      {(pendingGood > 0 || pendingScrap > 0) && (
        <p className="mt-4 rounded border border-fd-blue/50 bg-fd-blue/10 px-4 py-3 font-mono text-xl font-bold text-fd-blue">
          Final pieces to record: {pendingGood} good{pendingScrap > 0 ? ` · ${pendingScrap} scrap` : ''}
        </p>
      )}

      <div className="mt-4 rounded border border-fd-amber/50 bg-fd-amber/10 p-4">
        {roster.length > 0 ? (
          <>
            <p className="text-xl font-semibold text-fd-amber">
              Everyone currently clocked in will be clocked out:
            </p>
            <ul aria-label="Will be clocked out" className="mt-2 space-y-1">
              {roster.map((entry) => (
                <li key={entry.time_entry_id} className="flex items-baseline gap-2 text-xl text-fd-ink">
                  <span className="font-semibold">{entry.operator_name ?? UNKNOWN_OPERATOR_LABEL}</span>
                  <span className="font-mono tabular-nums text-fd-body">
                    ({formatElapsedShort(entry.clock_in, nowMs)})
                  </span>
                </li>
              ))}
            </ul>
          </>
        ) : (
          <p className="text-xl text-fd-body">No one is currently clocked in to this operation.</p>
        )}
      </div>

      <p className="mt-5 text-center font-mono text-sm font-bold uppercase tracking-[0.25em] text-fd-mute">
        Scan badge to complete — or type ID
      </p>

      <div
        data-testid="kiosk-complete-badge-display"
        className="mt-2 flex min-h-16 items-center justify-center rounded border border-fd-line-bright bg-fd-sunken px-4"
      >
        {badge ? (
          <span className="font-mono text-3xl font-semibold tracking-[0.2em] text-fd-ink">{badge}</span>
        ) : (
          <span className="flex items-center gap-3 text-fd-faint">
            <IdentificationIcon className="h-7 w-7" aria-hidden="true" />
            <span className="text-lg">{busy ? 'Completing…' : 'Waiting for badge…'}</span>
          </span>
        )}
      </div>

      {error && (
        <div
          role="alert"
          className="mt-3 w-full rounded border border-fd-red bg-fd-red/10 px-4 py-3 text-center text-xl font-semibold text-fd-red"
        >
          {error}
        </div>
      )}

      <div className="mt-3">
        <KioskKeypad value={badge} onChange={setBadge} maxLength={32} disabled={busy} idPrefix="kiosk-complete-key" />
      </div>

      <div className="mt-4 grid grid-cols-2 gap-3">
        <button
          type="button"
          onClick={onCancel}
          disabled={busy}
          className="min-h-16 rounded border border-fd-line bg-fd-sunken text-xl font-bold uppercase tracking-wide text-fd-body transition-colors hover:border-fd-line-bright disabled:opacity-40"
        >
          Cancel
        </button>
        <button
          type="button"
          onClick={() => {
            const id = badge.trim();
            if (!id) return;
            setBadge('');
            onBadge(id);
          }}
          disabled={busy || !badge.trim()}
          className="min-h-16 rounded border border-fd-green bg-fd-green/15 text-xl font-bold uppercase tracking-wide text-fd-green transition-colors hover:bg-fd-green/25 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {busy ? 'Completing…' : 'Complete'}
        </button>
      </div>
    </Modal>
  );
}
