import React, { useMemo, useState } from 'react';
import { CheckIcon } from '@heroicons/react/24/outline';
import KioskKeypad from './KioskKeypad';
import KioskModal, { KioskModalClose } from './KioskModal';
import { activeScrapCodes, resolveScrapSelection, scrapReasonTiles } from './scrapReasonOptions';
import type { ActiveJob, LaserNestInfo } from '../../types';
import type { ScrapReasonCodeOption } from '../../types/scrapReason';
import {
  KioskMaterialTie,
  KioskQueueItem,
  KioskRosterEntry,
  UNKNOWN_OPERATOR_LABEL,
  formatOperationLabel,
} from './kioskConstants';
import { applyQuickAdd, kioskQuickAdds, QUICK_ADD_BUTTON_CLASSES } from './quantityQuickAdds';
import {
  DEDUCTION_TIMING_NOTE,
  deductionHeadline,
  deductionLineText,
  predictMaterialConsumption,
  scrapNoteText,
  shortageNoteText,
} from '../../utils/materialTie';
import {
  closingShortAfterEntry,
  formatShortQuantity,
  goodCeiling as resolveGoodCeiling,
  operationReceived,
  operationUnaccounted,
  shortAfterEntry,
  shortUnitLabel,
} from '../../utils/shortCount';

/**
 * - `finish` — COMPLETE: record the last pieces and close the operation.
 * - `leave_open` — CLOCK OUT · KEEP OPEN: record this session's pieces and walk away;
 *   the operation stays open for the next shift (owner ruling 1's "or leave it open").
 */
export type KioskCompleteMode = 'finish' | 'leave_open';

/** The four figures both confirm callbacks carry (the clock-out quantities). */
export type KioskCompleteEntryHandler = (
  good: number,
  scrap: number,
  reason: string | null,
  codeId: number | null
) => void;

interface KioskCompleteModalProps {
  job: ActiveJob;
  /** Which verb opened the overlay. The operator can switch inside it. Default `finish`. */
  mode?: KioskCompleteMode;
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
   * count (`job.quantity_scrapped`) — feeding that in would under-state both the
   * material prediction and what is already accounted for on a multi-session operation.
   */
  operationScrapped?: number | null;
  /**
   * Who is clocked in to this operation (the queue row's roster). A SHORT close is
   * refused while anyone OTHER than this operator is still clocked in — the server
   * would 409 naming them — so the overlay says so up front and steers to leave-open.
   */
  roster?: KioskRosterEntry[] | null;
  /** The signed-in operator, to tell "me" from "someone else" on the roster. */
  currentUserId?: number | null;
  /**
   * `upstream_quantity_short` — target units that never reached this operation
   * (an upstream close declared them short). The server's figure; the overlay
   * never re-derives chain credit. Absent = nothing lost upstream.
   */
  upstreamShort?: number | null;
  /** `quantity_good_ceiling` — the most good the server will accept. Absent = the target. */
  goodCeiling?: number | null;
  /**
   * A refusal from the clock-out (nothing landed yet), shown INLINE and verbatim so
   * the operator can fix the entry without losing it.
   */
  error?: string | null;
  scrapCodes?: ScrapReasonCodeOption[] | null;
  busy: boolean;
  online: boolean;
  offlineHintId?: string;
  onCancel: () => void;
  /** Tap-through on the amber steps banner → the steps view. */
  onSteps?: () => void;
  /** COMPLETE: the final entry (clock-out quantities), then the host closes the operation. */
  onConfirm: KioskCompleteEntryHandler;
  /**
   * CLOCK OUT · KEEP OPEN: the session's entry rides the clock-out and nothing else.
   * Optional only so a host without the verb still renders; every kiosk passes it.
   */
  onLeaveOpen?: KioskCompleteEntryHandler;
}

function formatRunTime(clockInIso: string, nowMs: number): string {
  const startMs = Date.parse(clockInIso);
  if (!Number.isFinite(startMs)) return '—';
  const totalMinutes = Math.max(0, Math.floor((nowMs - startMs) / 60_000));
  const h = Math.floor(totalMinutes / 60);
  const m = totalMinutes % 60;
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`;
}

/** "Ana", "Ana and Bo", "Ana, Bo and Cy" — the same joining the server's 409 uses. */
function joinNames(names: string[]): string {
  if (names.length <= 1) return names[0] ?? '';
  return `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`;
}

const TILE_GRID: Record<number, string> = {
  3: 'grid-cols-3',
  4: 'grid-cols-2 sm:grid-cols-4',
  5: 'grid-cols-3 sm:grid-cols-5',
};

/**
 * COMPLETE OPERATION / CLOCK OUT · KEEP OPEN overlay (Foundry 1g).
 *
 * Owner ruling 1 (2026-11-05): a short count at completion is reported as scrap with a
 * reason, or the operation stays open — never closed short at the counted quantity. The
 * server enforces it (`/complete` refuses 409 and writes nothing); this overlay makes the
 * same arithmetic visible BEFORE the tap, off `utils/shortCount.ts`:
 *
 * - FINISH mode pre-fills GOOD at what is still unaccounted for (a one-tap honest finish
 *   for an operator who reports at the end). When the entry would leave units unaccounted
 *   it shows SHORT n, offers "Report n as scrap" (which opens the required reason grid),
 *   and disables COMPLETE — the operator records the pieces, reports the loss, or leaves
 *   the operation open.
 * - LEAVE-OPEN mode pre-fills GOOD at 0: the operator enters only what they finished this
 *   session, so walking away can never invent good pieces. The leave-open verb is ALWAYS
 *   one tap away, short or not.
 * - A SHORT close is refused while anyone else is still clocked in, so with a crewmate on
 *   the roster the overlay disables that close and promotes leave-open instead.
 *
 * Scrap still requires a reason (codes-or-legacy). When the queue holds a next job the
 * FINISH CTA reads `COMPLETE OP n · START {WO}` and the host chains a NON-optimistic
 * clock-in after the operation actually closes.
 */
export default function KioskCompleteModal({
  job,
  mode: initialMode = 'finish',
  nowMs,
  stepsTotal,
  stepsRecorded,
  nextQueueItem,
  machineCode,
  sessionNcrNumber,
  materialTies,
  operationScrapped,
  roster,
  currentUserId,
  upstreamShort,
  goodCeiling,
  error,
  scrapCodes,
  busy,
  online,
  offlineHintId,
  onCancel,
  onSteps,
  onConfirm,
  onLeaveOpen,
}: KioskCompleteModalProps) {
  const target = Number(job.quantity_ordered || 0);
  const completeSoFar = Number(job.quantity_complete || 0);
  const scrappedSoFar = Math.max(0, Number(operationScrapped ?? job.operation_quantity_scrapped ?? 0) || 0);
  const counts = { target, upstreamShort, complete: completeSoFar, scrapped: scrappedSoFar };
  const received = operationReceived(counts);
  const unaccountedNow = operationUnaccounted(counts);

  const [mode, setMode] = useState<KioskCompleteMode>(onLeaveOpen ? initialMode : 'finish');
  const finishPrefill = unaccountedNow > 0 ? formatShortQuantity(unaccountedNow) : '';
  const [good, setGood] = useState(mode === 'finish' ? finishPrefill : '');
  // An UNTOUCHED prefill follows the mode; once the operator keys a count it is theirs.
  const [goodTouched, setGoodTouched] = useState(false);
  const [prefillReset, setPrefillReset] = useState(false);
  const [scrap, setScrap] = useState('');
  const [activeField, setActiveField] = useState<'good' | 'scrap' | null>(null);
  const [scrapReason, setScrapReason] = useState<string | null>(null);
  const [scrapDetail, setScrapDetail] = useState('');

  const codes = activeScrapCodes(scrapCodes);
  const reasonTiles = useMemo(() => scrapReasonTiles(codes), [codes]);

  const goodQty = Number(good || 0);
  const scrapQty = Number(scrap || 0);
  const needsReason = scrapQty > 0 && !scrapReason;
  const finishing = mode === 'finish';

  const nest: LaserNestInfo | null | undefined = job.laser_nest;
  const isNest = nest != null;
  const total = Number(stepsTotal || 0);
  const recorded = Number(stepsRecorded || 0);
  const stepsOutstanding = Math.max(0, total - recorded);

  const sessionScrap = Number(job.quantity_scrapped || 0);
  const scrapTileValue = sessionScrap + scrapQty;
  // The whole label ('Op 20'), not the bare number: both call sites below used
  // to hard-code their own prefix, which doubled on a stored 'Op 20'.
  const opLabel = formatOperationLabel(job.operation_number);

  const entry = { ...counts, good: goodQty, scrap: scrapQty };
  const short = shortAfterEntry(entry);
  const closingShort = closingShortAfterEntry(entry);
  const fullCount = received > 0 && completeSoFar + goodQty >= received;
  const unit = (n: number) => shortUnitLabel(isNest, n);

  // Anyone on the roster who is not this operator. Matched on user id, falling back to
  // this job's own entry when the operator id is unknown, so "me" is never "someone else".
  const others = (roster || []).filter((r) =>
    currentUserId != null ? r.user_id !== currentUserId : r.time_entry_id !== job.time_entry_id
  );
  const otherNames = joinNames(others.map((r) => r.operator_name ?? UNKNOWN_OPERATOR_LABEL));
  const othersBlockShortClose = finishing && closingShort > 0 && others.length > 0;

  const finishDisabled = busy || needsReason || short > 0 || othersBlockShortClose;
  const leaveOpenDisabled = busy || needsReason;
  // Leave-open is the PRIMARY whenever a finish is not possible because of a crewmate.
  const leaveOpenIsPrimary = !finishing || othersBlockShortClose;

  // Predicted material draw. Consumption reconciles to `per_run × (good + scrapped)` at
  // the moment THIS operation closes, and the close books the good the floor RECORDED —
  // so the number follows the final good (complete so far + this entry) and the scrap
  // keypad alike. Leave-open closes nothing, so it predicts nothing (see the notice below).
  const prediction = useMemo(
    () =>
      predictMaterialConsumption({
        ties: materialTies,
        quantityOrdered: completeSoFar + goodQty,
        operationScrapped: scrappedSoFar,
        scrapEntered: scrapQty,
      }),
    [materialTies, completeSoFar, goodQty, scrappedSoFar, scrapQty]
  );
  const scrapNote = prediction ? scrapNoteText(prediction, scrapQty) : null;
  const shortageNote = prediction ? shortageNoteText(prediction) : null;

  // Same row the REPORT modal shows on its GOOD tab, off the same definition
  // (quantityQuickAdds.ts) and off the same `component_quantity` the page hands
  // Report as `fullNestQuantity` — it rides in on `job`, so nothing new is
  // threaded through.
  const quickAdds = kioskQuickAdds(job.component_quantity);

  // The ceiling is the SERVER's, not a style choice: clock-out refuses 400 once
  // `operation.quantity_complete + produced` clears `quantity_good_ceiling` (the target,
  // less anything an upstream close declared short). Unbounded, a quick add from the
  // finish prefill would be a guaranteed refusal that takes the whole entry down with
  // it. So the row tops out at what the server will take and goes disabled once the
  // field is there; recording MORE than that is an office over-count, not a tap here.
  const quickAddCeiling = Math.max(0, resolveGoodCeiling(target, goodCeiling) - completeSoFar);
  const quickAddsExhausted = goodQty >= quickAddCeiling;

  const setGoodByOperator = (value: string) => {
    setGood(value);
    setGoodTouched(true);
    setPrefillReset(false);
  };

  const handleQuickAdd = (amount: number) => {
    setGoodByOperator(String(applyQuickAdd(goodQty, amount, quickAddCeiling)));
    // A quick add is unambiguously a GOOD entry, so point the keypad at GOOD —
    // otherwise a scrap-bound keypad would take the operator's next digit into
    // the field they just steered away from. This re-points, never opens a
    // closed keypad mid-tap.
    setActiveField('good');
  };

  const switchMode = (next: KioskCompleteMode) => {
    if (next === mode) return;
    setMode(next);
    if (!goodTouched) {
      // An untouched FINISH prefill is "everything still unaccounted" — exactly the
      // phantom good a walk-away must not record. Leave-open starts from 0 and says so.
      setGood(next === 'finish' ? finishPrefill : '');
      setPrefillReset(next === 'leave_open' && finishPrefill !== '');
    }
  };

  const reportShortAsScrap = () => {
    setScrap(formatShortQuantity(scrapQty + short));
    // Point the keypad at SCRAP so the figure is editable, and the reason grid below
    // (required whenever scrap > 0) is now on screen.
    setActiveField('scrap');
  };

  const submit = (handler: KioskCompleteEntryHandler) => {
    if (scrapQty <= 0) {
      handler(goodQty, scrapQty, null, null);
      return;
    }
    const { reason, codeId } = resolveScrapSelection(codes, scrapReason, scrapDetail);
    handler(goodQty, scrapQty, reason, codeId);
  };

  const handleFinish = () => {
    if (finishDisabled) return;
    submit(onConfirm);
  };

  const handleLeaveOpen = () => {
    if (leaveOpenDisabled || !onLeaveOpen) return;
    submit(onLeaveOpen);
  };

  const wellClasses = (field: 'good' | 'scrap', tone: 'green' | 'red') => {
    const active = activeField === field;
    if (tone === 'green') {
      return active ? 'border-fd-green bg-fd-green/10 text-fd-green' : 'border-fd-line bg-fd-sunken text-fd-green';
    }
    return active ? 'border-fd-red bg-fd-red/10 text-fd-red' : 'border-fd-line bg-fd-sunken text-fd-red';
  };

  const showShort = finishing && short > 0;
  const tileCount = 3 + (nest ? 1 : 0) + (showShort ? 1 : 0);
  const upstream = Math.max(0, target - received);

  const finishLabel = !online
    ? 'Offline'
    : busy
      ? 'Completing…'
      : nextQueueItem
        ? `Complete ${opLabel} · Start ${nextQueueItem.work_order_number}`
        : `Complete ${opLabel}`;
  const leaveOpenLabel = !online ? 'Offline' : busy ? 'Clocking out…' : `Clock out · leave ${opLabel} open`;

  const ctaBase =
    'h-[60px] flex-1 rounded-[4px] font-mono text-[15px] font-bold uppercase tracking-[0.1em] transition-transform duration-150 ease-out active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-40';
  const rowBase =
    'min-h-[52px] w-full rounded-[4px] border px-3 font-mono text-[13px] font-semibold uppercase tracking-[0.1em] transition-transform duration-150 ease-out active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-40';
  // When a crewmate blocks the short close, the finish CTA drops to an outline and the
  // leave-open row takes the filled, primary look — the eye goes to what will work.
  const finishClasses = leaveOpenIsPrimary
    ? `${ctaBase} border border-fd-green/45 text-fd-green`
    : `${ctaBase} bg-fd-green text-[#04140b]`;
  const leaveOpenRowClasses = leaveOpenIsPrimary
    ? `${rowBase} border-fd-blue bg-fd-blue/15 text-fd-blue`
    : `${rowBase} border-fd-line text-fd-body`;

  return (
    <KioskModal
      onClose={onCancel}
      widthClassName="max-w-[640px]"
      topEdgeClassName={finishing ? 'border-t-2 border-t-fd-green' : 'border-t-2 border-t-fd-blue'}
      ariaLabelledBy="kiosk-complete-title"
    >
      {/* Header */}
      <div className="flex items-center gap-3 border-b border-fd-line px-5 py-4">
        <h2
          id="kiosk-complete-title"
          className={`font-mono text-[13px] font-bold uppercase tracking-[0.1em] ${
            finishing ? 'text-fd-green' : 'text-fd-blue'
          }`}
        >
          {finishing ? 'Complete operation' : 'Clock out · keep open'}
        </h2>
        <span className="font-mono text-[11px] uppercase text-fd-mute">
          {job.work_order_number || '—'} · {opLabel} {job.operation_name || ''}
        </span>
        <div className="flex-1" />
        <KioskModalClose onClose={onCancel} disabled={busy} />
      </div>

      <div className="flex flex-col gap-3.5 p-5">
        {/* Summary tiles */}
        <div className={`grid gap-2.5 ${TILE_GRID[tileCount] ?? 'grid-cols-3'}`}>
          <div className="rounded-[4px] border border-fd-line bg-fd-sunken p-3.5 text-center">
            <div className="font-mono text-[28px] font-bold tabular-nums text-fd-green">
              {formatShortQuantity(completeSoFar + goodQty)}
            </div>
            <div className="mt-1 font-mono text-[9.5px] uppercase tracking-[0.14em] text-fd-mute">Good pcs</div>
          </div>
          <div className="rounded-[4px] border border-fd-line bg-fd-sunken p-3.5 text-center">
            <div className="font-mono text-[28px] font-bold tabular-nums text-fd-red">{scrapTileValue}</div>
            <div className="mt-1 font-mono text-[9.5px] uppercase tracking-[0.14em] text-fd-mute">
              Scrap{sessionNcrNumber ? ` · ${sessionNcrNumber}` : ''}
            </div>
          </div>
          {showShort && (
            <div
              data-testid="kiosk-complete-short-tile"
              className="rounded-[4px] border border-fd-amber/55 bg-fd-amber/8 p-3.5 text-center"
            >
              <div className="font-mono text-[28px] font-bold tabular-nums text-fd-amber">
                {formatShortQuantity(short)}
              </div>
              <div className="mt-1 font-mono text-[9.5px] uppercase tracking-[0.14em] text-fd-amber">
                Short{isNest ? ` · ${unit(short)}` : ''}
              </div>
            </div>
          )}
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

        {/* SHORT panel (finish mode). Owner ruling 1: the operator records the pieces,
            reports the loss as scrap WITH a reason, or leaves the operation open. It
            never files an NCR or does anything on its own (ruling 3) — "Report as scrap"
            only fills the scrap field the operator then confirms. */}
        {showShort && (
          <div
            role="status"
            data-testid="kiosk-complete-short"
            className="rounded-[4px] border border-fd-amber/45 bg-fd-amber/8 px-3.5 py-3"
          >
            <p className="font-mono text-xs font-bold uppercase tracking-[0.06em] text-fd-amber">
              {formatShortQuantity(short)} of {formatShortQuantity(received)} not yet accounted for — record
              finished pieces, report lost pieces as scrap, or leave {opLabel} open
            </p>
            {upstream > 0 && (
              <p className="mt-1 text-[12px] text-fd-body">
                {formatShortQuantity(upstream)} {unit(upstream)} never reached it — closed short upstream.
              </p>
            )}
            <button
              type="button"
              data-testid="kiosk-complete-report-scrap"
              disabled={busy}
              onClick={reportShortAsScrap}
              className="mt-2.5 min-h-11 rounded-[3px] border border-fd-red/55 bg-fd-red/10 px-3 font-mono text-xs font-bold uppercase tracking-[0.08em] text-fd-red transition-transform duration-150 ease-out active:scale-[0.98] disabled:opacity-40"
            >
              Report {formatShortQuantity(short)} as scrap
            </button>
          </div>
        )}

        {/* A SHORT close is refused while a crewmate is still clocked in: they must
            clock out with their count first, or the work stays open. */}
        {othersBlockShortClose && (
          <p
            role="status"
            data-testid="kiosk-complete-others"
            className="rounded-[4px] border border-fd-amber/45 bg-fd-amber/8 px-3.5 py-3 font-mono text-xs font-bold uppercase tracking-[0.06em] text-fd-amber"
          >
            {otherNames} still clocked in — they must clock out with their count first
          </p>
        )}

        {/* Leave-open hints: the reset prefill, and the honest note that a FULL count
            completes the operation on clock-out no matter which verb was chosen. */}
        {!finishing && prefillReset && (
          <p data-testid="kiosk-complete-leave-open-hint" className="text-[13px] text-fd-body">
            Enter the pieces you finished this session
          </p>
        )}
        {!finishing && fullCount && (
          <p role="status" data-testid="kiosk-complete-leave-open-full" className="text-[13px] text-fd-body">
            {stepsOutstanding > 0
              ? `That is the full count — clocking out completes ${opLabel} once its step records are in.`
              : `That is the full count — clocking out completes ${opLabel}.`}
          </p>
        )}

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
        {(job.next_operation || (finishing && nextQueueItem)) && (
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
            {finishing && nextQueueItem && (
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
            auto-reverses). For the same reason LEAVE-OPEN shows no notice — it
            closes nothing, so it deducts nothing. Untied operations render
            nothing at all. */}
        {finishing && prediction && prediction.lines.length > 0 && (
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

        {/* The entry — rides the clock-out in both modes. */}
        <div>
          <p className="mb-2 font-mono text-[10px] uppercase tracking-[0.16em] text-fd-mute">
            {finishing
              ? 'Final entry — new pieces recorded at clock-out'
              : 'This session — new pieces recorded at clock-out'}
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
                ? `Quick add to good · max ${formatShortQuantity(quickAddCeiling)}`
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
                onChange={activeField === 'good' ? setGoodByOperator : setScrap}
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

        {/* Server refusal, INLINE and verbatim (the clock-out was refused, so nothing
            landed and the entry above is still the operator's to fix). */}
        {error && (
          <p
            role="alert"
            data-testid="kiosk-complete-error"
            className="rounded-[4px] border border-fd-red bg-fd-red/10 px-3.5 py-3 text-center font-mono text-sm font-bold text-fd-red"
          >
            {error}
          </p>
        )}
      </div>

      {/* Footer: Back + the primary verb, with the other verb always one tap below. */}
      <div className="flex gap-2.5 px-5 pb-3">
        <button
          type="button"
          onClick={onCancel}
          disabled={busy}
          className="h-[60px] w-[140px] rounded-[4px] border border-fd-line font-mono text-[13px] font-semibold uppercase tracking-[0.1em] text-fd-body transition-transform duration-150 ease-out active:scale-[0.98] disabled:opacity-40 sm:w-[170px]"
        >
          Back
        </button>
        {finishing ? (
          <button
            type="button"
            data-testid="kiosk-qty-confirm"
            onClick={handleFinish}
            disabled={finishDisabled}
            aria-describedby={!online ? offlineHintId : undefined}
            className={finishClasses}
          >
            {finishLabel}
          </button>
        ) : (
          <button
            type="button"
            data-testid="kiosk-complete-leave-open-confirm"
            onClick={handleLeaveOpen}
            disabled={leaveOpenDisabled}
            aria-describedby={!online ? offlineHintId : undefined}
            className={`${ctaBase} bg-fd-blue text-[#04101f]`}
          >
            {leaveOpenLabel}
          </button>
        )}
      </div>
      {onLeaveOpen && (
        <div className="px-5 pb-5">
          {finishing ? (
            <button
              type="button"
              data-testid="kiosk-complete-leave-open"
              onClick={() => switchMode('leave_open')}
              disabled={busy}
              className={leaveOpenRowClasses}
            >
              Clock out · leave {opLabel} open
            </button>
          ) : (
            <button
              type="button"
              data-testid="kiosk-complete-switch-finish"
              onClick={() => switchMode('finish')}
              disabled={busy}
              className={`${rowBase} border-fd-line text-fd-body`}
            >
              Complete {opLabel} instead
            </button>
          )}
        </div>
      )}
      {needsReason && <p className="px-5 pb-4 text-center text-sm text-fd-red">Choose a scrap reason to continue.</p>}
    </KioskModal>
  );
}
