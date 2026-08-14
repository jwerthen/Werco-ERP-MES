import React, { useMemo, useState } from 'react';
import { CheckIcon } from '@heroicons/react/24/outline';
import KioskKeypad from './KioskKeypad';
import KioskModal, { KioskModalClose } from './KioskModal';
import { activeScrapCodes, resolveScrapSelection, scrapReasonTiles } from './scrapReasonOptions';
import type { ActiveJob, LaserNestInfo } from '../../types';
import type { ScrapReasonCodeOption } from '../../types/scrapReason';
import { KioskMaterialTie, KioskQueueItem, formatOperationLabel } from './kioskConstants';
import { applyQuickAdd, kioskQuickAdds, QUICK_ADD_BUTTON_CLASSES } from './quantityQuickAdds';
import {
  DEDUCTION_TIMING_NOTE,
  deductionHeadline,
  deductionLineText,
  predictMaterialConsumption,
  scrapNoteText,
  shortageNoteText,
} from '../../utils/materialTie';

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
  /**
   * Material ties on THIS operation, straight off the queue payload (a read
   * path — the kiosk never calls the office tie API). Absent/empty on an untied
   * operation, which renders nothing at all.
   */
  materialTies?: KioskMaterialTie[] | null;
  /**
   * The OPERATION's scrap total already recorded. NOT this time entry's session
   * count (`job.quantity_scrapped`) — feeding that in would under-state the
   * prediction on a multi-session operation.
   */
  operationScrapped?: number | null;
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
  materialTies,
  operationScrapped,
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
  // The whole label ('Op 20'), not the bare number: both call sites below used
  // to hard-code their own prefix, which doubled on a stored 'Op 20'.
  const opLabel = formatOperationLabel(job.operation_number);

  // Predicted material draw. Recomputed from `scrapQty` on every keystroke so
  // the number moves with the scrap keypad — the one input that DOES move it.
  // `job.quantity_ordered` (not `goodQty`) because /complete asserts
  // `quantity_complete = quantity_ordered` regardless of what was keyed as good.
  const prediction = useMemo(
    () =>
      predictMaterialConsumption({
        ties: materialTies,
        quantityOrdered: job.quantity_ordered,
        operationScrapped,
        scrapEntered: scrapQty,
      }),
    [materialTies, job.quantity_ordered, operationScrapped, scrapQty]
  );
  const scrapNote = prediction ? scrapNoteText(prediction, scrapQty) : null;
  const shortageNote = prediction ? shortageNoteText(prediction) : null;

  // Same row the REPORT modal shows on its GOOD tab, off the same definition
  // (quantityQuickAdds.ts) and off the same `component_quantity` the page hands
  // Report as `fullNestQuantity` — it rides in on `job`, so nothing new is
  // threaded through.
  const quickAdds = kioskQuickAdds(job.component_quantity);

  // The ceiling is the SERVER's, not a style choice: clock-out refuses 400
  // "Quantity produced exceeds quantity ordered" once
  // `operation.quantity_complete + produced` clears the operation target, and
  // this field pre-fills at exactly `remaining` — the ceiling itself. Unbounded,
  // every quick add from the default state would be a guaranteed refusal that
  // takes the whole completion down with it. So the row tops out at what the
  // server will take and goes disabled once the field is there, which is also
  // the truth an operator needs: COMPLETE closes this operation at its target,
  // and recording MORE than the target is an office over-count, not a tap here.
  const quickAddCeiling = remaining;
  const quickAddsExhausted = goodQty >= quickAddCeiling;

  const handleQuickAdd = (amount: number) => {
    setGood(String(applyQuickAdd(goodQty, amount, quickAddCeiling)));
    // A quick add is unambiguously a GOOD entry, so point the keypad at GOOD —
    // otherwise a scrap-bound keypad would take the operator's next digit into
    // the field they just steered away from. The keypad is always already open
    // by the time this runs (the row only enables once good is under its
    // ceiling, which takes a keypad edit), so this re-points, never opens: the
    // COMPLETE button can't grow away from a gloved finger mid-tap.
    setActiveField('good');
  };

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
          {job.work_order_number || '—'} · {opLabel} {job.operation_name || ''}
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
                  {formatOperationLabel(job.next_operation.operation_number)} ·{' '}
                  {job.next_operation.name || 'Next operation'}
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

        {/* Material deduction notice — INFORMATIONAL ONLY. It never gates the
            CTA below: a shortage does not block production, it drives the lot
            negative and writes ALLOCATION_SHORTAGE.

            Consumption fires when THIS OPERATION completes, so on a laser child
            WO (one operation per nest) the sheets for this nest leave stock on
            this very tap — which is why the copy here is allowed the present
            tense the board chip is not. What it must NOT drift into is "per
            run": reporting runs on a still-open operation posts nothing (an
            in-progress operation is still reducible and consumption never
            auto-reverses). Untied operations render nothing at all. */}
        {prediction && prediction.lines.length > 0 && (
          <div
            data-testid="kiosk-complete-material"
            className="rounded-[4px] border border-fd-line bg-fd-sunken px-3.5 py-3"
          >
            <p className="font-mono text-[10px] uppercase tracking-[0.14em] text-fd-mute">
              {deductionHeadline(job.work_order_number)}
            </p>
            <ul className="mt-1.5 space-y-0.5">
              {prediction.lines.map((line) => (
                <li key={line.key} className="font-mono text-[15px] font-semibold uppercase text-fd-ink">
                  {deductionLineText(line)}
                </li>
              ))}
            </ul>
            {scrapNote && (
              <p data-testid="kiosk-complete-material-scrap" className="mt-1.5 text-[12px] text-fd-body">
                {scrapNote}
              </p>
            )}
            <p className="mt-1.5 text-[12px] text-fd-mute">{DEDUCTION_TIMING_NOTE}</p>
            {shortageNote && (
              <p
                data-testid="kiosk-complete-material-short"
                className="mt-2 rounded-[3px] border border-fd-amber/45 bg-fd-amber/8 px-2.5 py-2 font-mono text-[12px] font-bold uppercase tracking-[0.06em] text-fd-amber"
              >
                {shortageNote}
              </p>
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

          {/* Quick adds — GOOD only. Two fields are on screen, so the row is
              captioned and each button names its target: a quick add must never
              be mistaken for (or silently write to) scrap. */}
          <div className="mt-2.5">
            <p
              id="kiosk-complete-quickadd-label"
              data-testid="kiosk-complete-quickadd-label"
              className="mb-1.5 font-mono text-[10px] uppercase tracking-[0.16em] text-fd-mute"
            >
              {quickAddCeiling > 0
                ? `Quick add to good · max ${quickAddCeiling}`
                : 'Quick add to good · operation is already at its target'}
            </p>
            <div className="flex gap-2" role="group" aria-labelledby="kiosk-complete-quickadd-label">
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
                ? `Complete ${opLabel} · Start ${nextQueueItem.work_order_number}`
                : `Complete ${opLabel}`}
        </button>
      </div>
      {needsReason && <p className="px-5 pb-4 text-center text-sm text-fd-red">Choose a scrap reason to continue.</p>}
    </KioskModal>
  );
}
