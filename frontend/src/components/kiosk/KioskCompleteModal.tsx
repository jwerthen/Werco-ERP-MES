import React, { useMemo, useState } from 'react';
import { CheckIcon } from '@heroicons/react/24/outline';
import KioskKeypad from './KioskKeypad';
import KioskModal, { KioskModalClose } from './KioskModal';
import { activeScrapCodes, resolveScrapSelection, scrapReasonTiles } from './scrapReasonOptions';
import type { ActiveJob, LaserNestInfo } from '../../types';
import type { ScrapReasonCodeOption } from '../../types/scrapReason';
import { KioskQueueItem } from './kioskConstants';

interface KioskCompleteModalProps {
  job: ActiveJob;
  /** Skew-corrected now — drives the RUN TIME H:M tile. */
  nowMs: number;
  /** Required-step counts from the queue payload (undefined/0 total = no banner). */
  stepsTotal?: number | null;
  stepsRecorded?: number | null;
  /** The next queued (non-active) item on this machine, when one exists (decision 6). */
  nextQueueItem?: KioskQueueItem | null;
  /** Machine code for the "NEXT ON {machine}" label. */
  machineCode?: string | null;
  /** NCR number filed from a scrap report THIS session, when one exists. */
  sessionNcrNumber?: string | null;
  scrapCodes?: ScrapReasonCodeOption[] | null;
  busy: boolean;
  online: boolean;
  offlineHintId?: string;
  onCancel: () => void;
  /** Tap-through on the amber steps banner → the steps view. */
  onSteps?: () => void;
  /** Final entry (clock-out quantities) — the existing complete semantics. */
  onConfirm: (good: number, scrap: number, reason: string | null, codeId: number | null) => void;
}

function formatRunTime(clockInIso: string, nowMs: number): string {
  const startMs = Date.parse(clockInIso);
  if (!Number.isFinite(startMs)) return '—';
  const totalMinutes = Math.max(0, Math.floor((nowMs - startMs) / 60_000));
  const h = Math.floor(totalMinutes / 60);
  const m = totalMinutes % 60;
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`;
}

/**
 * COMPLETE OPERATION overlay (Foundry 1g): summary chrome over the EXISTING
 * complete semantics — the final entry defaults to good = remaining / scrap = 0
 * and rides the clock-out, then the host asserts completion at the target
 * quantity (decision 6). Scrap still requires a reason (codes-or-legacy). When
 * the queue holds a next job the CTA reads `COMPLETE OP n · START {WO}` and the
 * host chains a NON-optimistic clock-in after a successful complete.
 */
export default function KioskCompleteModal({
  job,
  nowMs,
  stepsTotal,
  stepsRecorded,
  nextQueueItem,
  machineCode,
  sessionNcrNumber,
  scrapCodes,
  busy,
  online,
  offlineHintId,
  onCancel,
  onSteps,
  onConfirm,
}: KioskCompleteModalProps) {
  const ordered = Number(job.quantity_ordered || 0);
  const completeSoFar = Number(job.quantity_complete || 0);
  const remaining = Math.max(0, ordered - completeSoFar);

  const [good, setGood] = useState(remaining > 0 ? String(remaining) : '');
  const [scrap, setScrap] = useState('');
  const [activeField, setActiveField] = useState<'good' | 'scrap' | null>(null);
  const [scrapReason, setScrapReason] = useState<string | null>(null);
  const [scrapDetail, setScrapDetail] = useState('');

  const codes = activeScrapCodes(scrapCodes);
  const reasonTiles = useMemo(() => scrapReasonTiles(codes), [codes]);

  const goodQty = Number(good || 0);
  const scrapQty = Number(scrap || 0);
  const needsReason = scrapQty > 0 && !scrapReason;
  const confirmDisabled = busy || needsReason;

  const nest: LaserNestInfo | null | undefined = job.laser_nest;
  const total = Number(stepsTotal || 0);
  const recorded = Number(stepsRecorded || 0);
  const stepsOutstanding = Math.max(0, total - recorded);

  const sessionScrap = Number(job.quantity_scrapped || 0);
  const scrapTileValue = sessionScrap + scrapQty;
  const opNumber = job.operation_number ?? '—';

  const handleConfirm = () => {
    if (confirmDisabled) return;
    if (scrapQty <= 0) {
      onConfirm(goodQty, scrapQty, null, null);
      return;
    }
    const { reason, codeId } = resolveScrapSelection(codes, scrapReason, scrapDetail);
    onConfirm(goodQty, scrapQty, reason, codeId);
  };

  const wellClasses = (field: 'good' | 'scrap', tone: 'green' | 'red') => {
    const active = activeField === field;
    if (tone === 'green') {
      return active ? 'border-fd-green bg-fd-green/10 text-fd-green' : 'border-fd-line bg-fd-sunken text-fd-green';
    }
    return active ? 'border-fd-red bg-fd-red/10 text-fd-red' : 'border-fd-line bg-fd-sunken text-fd-red';
  };

  return (
    <KioskModal
      onClose={onCancel}
      widthClassName="max-w-[640px]"
      topEdgeClassName="border-t-2 border-t-fd-green"
      ariaLabelledBy="kiosk-complete-title"
    >
      {/* Header */}
      <div className="flex items-center gap-3 border-b border-fd-line px-5 py-4">
        <h2
          id="kiosk-complete-title"
          className="font-mono text-[13px] font-bold uppercase tracking-[0.1em] text-fd-green"
        >
          Complete operation
        </h2>
        <span className="font-mono text-[11px] uppercase text-fd-mute">
          {job.work_order_number || '—'} · Op {opNumber} {job.operation_name || ''}
        </span>
        <div className="flex-1" />
        <KioskModalClose onClose={onCancel} disabled={busy} />
      </div>

      <div className="flex flex-col gap-3.5 p-5">
        {/* Summary tiles */}
        <div className={`grid gap-2.5 ${nest ? 'grid-cols-2 sm:grid-cols-4' : 'grid-cols-3'}`}>
          <div className="rounded-[4px] border border-fd-line bg-fd-sunken p-3.5 text-center">
            <div className="font-mono text-[28px] font-bold tabular-nums text-fd-green">
              {completeSoFar + goodQty}
            </div>
            <div className="mt-1 font-mono text-[9.5px] uppercase tracking-[0.14em] text-fd-mute">Good pcs</div>
          </div>
          <div className="rounded-[4px] border border-fd-line bg-fd-sunken p-3.5 text-center">
            <div className="font-mono text-[28px] font-bold tabular-nums text-fd-red">{scrapTileValue}</div>
            <div className="mt-1 font-mono text-[9.5px] uppercase tracking-[0.14em] text-fd-mute">
              Scrap{sessionNcrNumber ? ` · ${sessionNcrNumber}` : ''}
            </div>
          </div>
          {nest && (
            <div className="rounded-[4px] border border-fd-line bg-fd-sunken p-3.5 text-center">
              <div className="font-mono text-[28px] font-bold tabular-nums text-fd-ink">
                {Number(nest.completed_runs)}/{Number(nest.planned_runs)}
              </div>
              <div className="mt-1 font-mono text-[9.5px] uppercase tracking-[0.14em] text-fd-mute">Sheet runs</div>
            </div>
          )}
          <div className="rounded-[4px] border border-fd-line bg-fd-sunken p-3.5 text-center">
            <div className="font-mono text-[28px] font-bold tabular-nums text-fd-ink">
              {formatRunTime(job.clock_in, nowMs)}
            </div>
            <div className="mt-1 font-mono text-[9.5px] uppercase tracking-[0.14em] text-fd-mute">Run time h:m</div>
          </div>
        </div>

        {/* Steps banner */}
        {total > 0 &&
          (stepsOutstanding === 0 ? (
            <div className="flex items-center gap-3 rounded-[4px] border border-fd-green/35 bg-fd-green/5 px-3.5 py-3">
              <span
                aria-hidden="true"
                className="flex h-[22px] w-[22px] items-center justify-center rounded-[3px] border border-fd-green/50 bg-fd-green/15 text-fd-green"
              >
                <CheckIcon className="h-[13px] w-[13px]" strokeWidth={2} />
              </span>
              <span className="font-mono text-xs uppercase tracking-[0.06em] text-fd-green">
                All {total} process steps recorded · traceability complete
              </span>
            </div>
          ) : (
            <button
              type="button"
              data-testid="kiosk-complete-steps-banner"
              disabled={busy}
              onClick={onSteps}
              className="flex min-h-11 w-full items-center gap-3 rounded-[4px] border border-fd-amber/45 bg-fd-amber/8 px-3.5 py-3 text-left transition-transform duration-150 ease-out active:scale-[0.98] disabled:opacity-40"
            >
              <span className="font-mono text-xs font-bold uppercase tracking-[0.06em] text-fd-amber">
                {stepsOutstanding} step record{stepsOutstanding === 1 ? '' : 's'} still needed — tap to review
              </span>
            </button>
          ))}

        {/* Routing row */}
        {(job.next_operation || nextQueueItem) && (
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-[4px] border border-fd-line bg-fd-sunken px-3.5 py-3">
            {job.next_operation && (
              <>
                <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-fd-mute">Routes to</span>
                <span className="font-mono text-[13px] font-semibold uppercase text-fd-ink">
                  Op {job.next_operation.operation_number ?? '—'} · {job.next_operation.name || 'Next operation'}
                  {job.next_operation.work_center?.code || job.next_operation.work_center?.name
                    ? ` · ${job.next_operation.work_center?.code || job.next_operation.work_center?.name}`
                    : ''}
                </span>
              </>
            )}
            <div className="flex-1" />
            {nextQueueItem && (
              <>
                <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-fd-mute">
                  Next on {machineCode || 'this machine'}
                </span>
                <span className="font-mono text-[13px] font-semibold uppercase text-fd-blue">
                  {nextQueueItem.work_order_number}
                  {nextQueueItem.part_number ? ` · ${nextQueueItem.part_number}` : ''}
                </span>
              </>
            )}
          </div>
        )}

        {/* Final entry — rides the clock-out (good defaults to remaining). */}
        <div>
          <p className="mb-2 font-mono text-[10px] uppercase tracking-[0.16em] text-fd-mute">
            Final entry — new pieces recorded at clock-out
          </p>
          <div className="grid grid-cols-2 gap-2.5">
            <button
              type="button"
              data-testid="kiosk-qty-good"
              aria-pressed={activeField === 'good'}
              disabled={busy}
              onClick={() => setActiveField('good')}
              className={`min-h-16 rounded-[4px] border px-3.5 py-2 text-left transition-colors duration-150 ease-out disabled:opacity-40 ${wellClasses('good', 'green')}`}
            >
              <span className="block font-mono text-[10px] font-bold uppercase tracking-[0.16em]">Good</span>
              <span className="mt-0.5 block font-mono text-3xl font-bold tabular-nums">{good || '0'}</span>
            </button>
            <button
              type="button"
              data-testid="kiosk-qty-scrap"
              aria-pressed={activeField === 'scrap'}
              disabled={busy}
              onClick={() => setActiveField('scrap')}
              className={`min-h-16 rounded-[4px] border px-3.5 py-2 text-left transition-colors duration-150 ease-out disabled:opacity-40 ${wellClasses('scrap', 'red')}`}
            >
              <span className="block font-mono text-[10px] font-bold uppercase tracking-[0.16em]">Scrap</span>
              <span className="mt-0.5 block font-mono text-3xl font-bold tabular-nums">{scrap || '0'}</span>
            </button>
          </div>

          {activeField != null && (
            <div className="mx-auto mt-3 max-w-[340px]">
              <KioskKeypad
                value={activeField === 'good' ? good : scrap}
                onChange={activeField === 'good' ? setGood : setScrap}
                maxLength={5}
                disabled={busy}
                size="sm"
              />
            </div>
          )}

          {scrapQty > 0 && (
            <div className="mt-3">
              <p className="mb-2 font-mono text-[10px] uppercase tracking-[0.16em] text-fd-red">
                Scrap reason — required
              </p>
              <div className="grid grid-cols-2 gap-2" role="group" aria-label="Scrap reason">
                {reasonTiles.map((reason) => {
                  const selected = scrapReason === reason.value;
                  return (
                    <button
                      key={reason.value}
                      type="button"
                      aria-pressed={selected}
                      disabled={busy}
                      onClick={() => setScrapReason(reason.value)}
                      className={`min-h-12 rounded-[3px] border px-2 py-1 font-mono text-xs uppercase tracking-[0.06em] transition-transform duration-150 ease-out active:scale-[0.98] disabled:opacity-40 ${
                        selected
                          ? 'border-fd-red bg-fd-red/10 font-bold text-fd-red'
                          : 'border-fd-line bg-fd-raised font-semibold text-fd-body'
                      }`}
                    >
                      {reason.label}
                    </button>
                  );
                })}
              </div>
              {codes && (
                <div className="mt-2.5">
                  <label
                    htmlFor="kiosk-scrap-detail"
                    className="mb-1 block font-mono text-[10px] uppercase tracking-[0.16em] text-fd-mute"
                  >
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
                    className="w-full rounded-[3px] border border-fd-line bg-fd-sunken px-3 py-2.5 font-mono text-base text-fd-ink placeholder:text-fd-mute focus:border-fd-red focus:outline-none disabled:opacity-40"
                  />
                </div>
              )}
            </div>
          )}
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
          Back
        </button>
        <button
          type="button"
          data-testid="kiosk-qty-confirm"
          onClick={handleConfirm}
          disabled={confirmDisabled}
          aria-describedby={!online ? offlineHintId : undefined}
          className="h-[60px] flex-1 rounded-[4px] bg-fd-green font-mono text-[15px] font-bold uppercase tracking-[0.1em] text-[#04140b] transition-transform duration-150 ease-out active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-40"
        >
          {!online
            ? 'Offline'
            : busy
              ? 'Completing…'
              : nextQueueItem
                ? `Complete op ${opNumber} · Start ${nextQueueItem.work_order_number}`
                : `Complete op ${opNumber}`}
        </button>
      </div>
      {needsReason && <p className="px-5 pb-4 text-center text-sm text-fd-red">Choose a scrap reason to continue.</p>}
    </KioskModal>
  );
}
