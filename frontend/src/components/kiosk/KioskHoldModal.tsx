import React, { useState } from 'react';
import KioskModal, { KioskModalClose } from './KioskModal';
import { HOLD_REASONS } from './kioskConstants';

/**
 * Two-line tile copy per the Foundry handoff (1f), keyed by the EXISTING
 * WorkOrderBlockerCategory values — the vocabulary is unchanged (decision 7);
 * subtitles are written for the categories the design doesn't name. Accessible
 * names stay the pre-redesign labels ("Machine down", …) so behavior-pinning
 * tests and screen-reader output are stable.
 */
const HOLD_TILE_COPY: Record<string, { title: string; subtitle: string }> = {
  material_missing: { title: 'Material missing', subtitle: 'Short / wrong lot' },
  machine_down: { title: 'Machine down', subtitle: 'Alarm / downtime' },
  tooling_missing: { title: 'Tooling missing', subtitle: 'Change / damage' },
  quality_hold: { title: 'Quality hold', subtitle: 'Awaiting disposition' },
  labor_unavailable: { title: 'Labor unavailable', subtitle: 'No operator free' },
  engineering_question: { title: 'Engineering question', subtitle: 'Needs an answer first' },
  previous_operation: { title: 'Previous operation', subtitle: 'Upstream not complete' },
  other: { title: 'Other', subtitle: 'Add a note' },
};

interface KioskHoldModalProps {
  workOrderNumber: string;
  operationNumber: string | number | null;
  busy: boolean;
  online: boolean;
  offlineHintId?: string;
  onCancel: () => void;
  /** category = WorkOrderBlockerCategory value; note = optional operator note (may be empty). */
  onConfirm: (category: string, note: string) => void;
}

/**
 * PLACE ON HOLD overlay (Foundry 1f): required reason as two-line tiles, an
 * optional note (SENT whenever non-empty — any category), the amber notice row,
 * and a confirm CTA that echoes the selected reason. The host owns the PUT and
 * keeps the 'other' stub-note fallback so every kiosk hold files a blocker.
 */
export default function KioskHoldModal({
  workOrderNumber,
  operationNumber,
  busy,
  online,
  offlineHintId,
  onCancel,
  onConfirm,
}: KioskHoldModalProps) {
  const [category, setCategory] = useState<string | null>(null);
  const [note, setNote] = useState('');

  const selectedTitle = category ? (HOLD_TILE_COPY[category]?.title ?? category) : null;
  const confirmDisabled = busy || !category;

  return (
    <KioskModal
      onClose={onCancel}
      widthClassName="max-w-[660px]"
      topEdgeClassName="border-t-2 border-t-fd-amber"
      ariaLabelledBy="kiosk-hold-title"
    >
      {/* Header */}
      <div className="flex items-center gap-3 border-b border-fd-line px-5 py-4">
        <h2 id="kiosk-hold-title" className="font-mono text-[13px] font-bold uppercase tracking-[0.1em] text-fd-amber">
          Place on hold
        </h2>
        <span className="font-mono text-[11px] uppercase text-fd-mute">
          {workOrderNumber} · Op {operationNumber ?? '—'} · Timer will pause
        </span>
        <div className="flex-1" />
        <KioskModalClose onClose={onCancel} disabled={busy} />
      </div>

      <div className="flex flex-col gap-3.5 p-5">
        <p className="font-mono text-[10px] uppercase tracking-[0.16em] text-fd-mute">Hold reason — required</p>
        <div className="grid grid-cols-2 gap-2.5 sm:grid-cols-3" role="group" aria-label="Hold reason">
          {HOLD_REASONS.map((reason) => {
            const copy = HOLD_TILE_COPY[reason.value] ?? { title: reason.label, subtitle: '' };
            const selected = category === reason.value;
            return (
              <button
                key={reason.value}
                type="button"
                aria-pressed={selected}
                aria-label={reason.label}
                disabled={busy}
                onClick={() => setCategory(reason.value)}
                className={`flex h-[72px] flex-col items-center justify-center gap-1 rounded-[4px] border px-2 transition-transform duration-150 ease-out active:scale-[0.98] disabled:opacity-40 ${
                  selected
                    ? 'border-fd-amber bg-fd-amber/10 shadow-[0_0_16px_rgba(210,153,34,0.15)]'
                    : 'border-fd-line bg-fd-raised'
                }`}
              >
                <span
                  className={`font-mono text-xs uppercase tracking-[0.06em] ${
                    selected ? 'font-bold text-fd-amber' : 'font-semibold text-fd-body'
                  }`}
                >
                  {copy.title}
                </span>
                {copy.subtitle && (
                  <span
                    className={`font-mono text-[10px] uppercase tracking-[0.04em] ${
                      selected ? 'text-fd-amber/70' : 'text-fd-mute'
                    }`}
                  >
                    {copy.subtitle}
                  </span>
                )}
              </button>
            );
          })}
        </div>

        <label htmlFor="kiosk-hold-note" className="sr-only">
          Note — optional
        </label>
        <textarea
          id="kiosk-hold-note"
          data-testid="kiosk-hold-note"
          rows={2}
          maxLength={500}
          disabled={busy}
          value={note}
          onChange={(e) => setNote(e.target.value)}
          placeholder={'Add note (optional) — e.g. "Z-axis alarm 4012"…'}
          className="h-16 w-full resize-none rounded-[4px] border border-fd-line bg-fd-sunken px-3.5 py-3 font-mono text-[13px] text-fd-ink placeholder:text-fd-mute focus:border-fd-amber focus:outline-none disabled:opacity-40"
        />

        <div className="flex items-center gap-2.5 rounded-[4px] border border-fd-amber/30 bg-fd-amber/5 px-3.5 py-2.5">
          <span
            aria-hidden="true"
            className="h-2 w-2 animate-pulse rounded-full bg-fd-amber shadow-[0_0_8px_rgba(210,153,34,0.6)]"
          />
          <span className="font-mono text-[11px] uppercase tracking-[0.06em] text-fd-amber">
            Supervisor will see this hold · downtime clock starts
          </span>
        </div>
      </div>

      {/* Footer */}
      <div className="flex gap-2.5 px-5 pb-5">
        <button
          type="button"
          onClick={onCancel}
          disabled={busy}
          className="h-[60px] w-[140px] rounded-[4px] border border-fd-line font-mono text-[13px] font-semibold uppercase tracking-[0.1em] text-fd-body transition-transform duration-150 ease-out active:scale-[0.98] disabled:opacity-40 sm:w-[170px]"
        >
          Cancel
        </button>
        <button
          type="button"
          data-testid="kiosk-hold-confirm"
          disabled={confirmDisabled}
          aria-describedby={!online ? offlineHintId : undefined}
          onClick={() => category && onConfirm(category, note.trim())}
          className="h-[60px] flex-1 rounded-[4px] bg-fd-amber font-mono text-[15px] font-bold uppercase tracking-[0.1em] text-[#171003] transition-transform duration-150 ease-out active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-40"
        >
          {!online
            ? 'Offline'
            : busy
              ? 'Holding…'
              : selectedTitle
                ? `Place on hold — ${selectedTitle}`
                : 'Place on hold'}
        </button>
      </div>
    </KioskModal>
  );
}
