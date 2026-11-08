/**
 * CompleteWorkModal — the office "Complete" dialog for a work order or one of
 * its operations.
 *
 * Owner ruling 1 (2026-11-05): a short count is reported as scrap with a
 * required reason, or the work stays open — never closed at the counted
 * quantity. The server is the arbiter; this dialog shows the user, before they
 * submit, what the server will decide, from the SERVER's own figures:
 *
 *  - Both fields are TOTALS, prefilled with what is RECORDED (never the
 *    target). Good is capped at the server's good ceiling; the scrap total
 *    never lowers (the server keeps the larger of the two), and a scrap reason
 *    is required only when the total RISES.
 *  - OPERATION mode: the operation closes only when good + scrap account for
 *    every unit that reached it (`received = target − upstream short`). Short,
 *    the submit becomes "Save progress · leave open" — the server saves the
 *    counts and keeps the operation open — and an unchanged short submit is
 *    disabled (the server would write nothing). Booking more good than the
 *    floor recorded reveals "Why record more than the floor counted?", REQUIRED
 *    when the floor may have clocked labor (`laborRecorded`); desk cleanup of
 *    never-clocked work stays reason-free, the owner's office-exempt asymmetry.
 *  - WORK-ORDER mode (force-complete): a shortfall below the order must be
 *    covered by max(the floor's accounted loss, the scrap total), else the
 *    submit is disabled; good above the floor's `deliverable` needs the
 *    override reason. When the caller cannot supply those figures (the list's
 *    quick-complete has only a summary row) they are UNKNOWN, never zero: the
 *    readout turns advisory and the server decides.
 *
 * A server refusal stays inline. Two of them name their own follow-up and the
 * dialog offers it: the office "still clocked in" 409 gets "Clock them out and
 * close" (resubmits with `acknowledgeOpenEntries`), and a refusal asking for
 * `quantity_override_reason` turns the reason field on and makes it required.
 *
 * Scrap reason capture is unchanged (Lean Phase 1): with ACTIVE company scrap
 * codes the reason is a REQUIRED code pick (+ optional detail) and the submit
 * carries scrapReasonCodeId; with zero codes the legacy SCRAP_REASONS picker.
 * `scrapReason` is always non-blank when the scrap total rises.
 *
 * This is a SERVER-GATED completion: intentionally NON-optimistic. The caller
 * awaits the server, reflects only what comes back, and passes the server's
 * verbatim refusal back in through `error`.
 */

import React, { useEffect, useMemo, useRef, useState } from 'react';
import { CheckCircleIcon, ExclamationTriangleIcon } from '@heroicons/react/24/outline';
import { Modal } from '../ui/Modal';
import { FormField } from '../ui/FormField';
import { Button } from '../ui/Button';
import { LoadingButton } from '../ui/LoadingButton';
import {
  EMPTY_SCRAP_SELECTION,
  ScrapReasonFields,
  isScrapSelectionComplete,
  scrapSelectionText,
} from '../quality/ScrapReasonFields';
import { useScrapReasonCodes } from '../../hooks/useScrapReasonCodes';
import {
  SHORT_EPSILON,
  formatShortQuantity,
  goodCeiling as resolveGoodCeiling,
  operationReceived,
  operationUnaccounted,
  shortUnitLabel,
} from '../../utils/shortCount';
import { isOpenEntriesRefusal, isOverrideReasonRefusal } from '../../utils/officeCompletion';

export interface CompleteWorkSubmit {
  quantityComplete: number;
  /**
   * The scrap TOTAL when it rose above what is recorded; `null` leaves the
   * recorded scrap untouched (the request omits the param).
   */
  quantityScrapped: number | null;
  /** Non-blank only when the scrap total rises; undefined otherwise. */
  scrapReason?: string;
  /** Company scrap-code id, when one was chosen (codes mode only). */
  scrapReasonCodeId?: number;
  /** Why the desk books more good than the floor counted (`quantity_override_reason`). */
  overrideReason?: string;
  /** The user confirmed the "still clocked in" refusal: clock them out and close. */
  acknowledgeOpenEntries?: boolean;
}

export type CompleteWorkMode = 'operation' | 'work_order';

interface CompleteWorkModalProps {
  open: boolean;
  onClose: () => void;
  /** Awaited; throw to surface a server error. Caller closes on success. */
  onSubmit: (values: CompleteWorkSubmit) => Promise<void>;
  /** In-flight flag — disables inputs and spins the submit button. */
  submitting: boolean;
  /** Dialog title, e.g. "Complete work order WO-0042" or an operation name. */
  title: string;
  /** Optional context line under the title (e.g. "Ordered: 10"). */
  subtitle?: string;
  /** Server refusal for the current attempt; keep quantities editable for retry. */
  error?: string | null;
  /** Explain how this caller handles existing scrap totals. */
  scrapHint?: string;
  /** `operation` (the office op verb) or `work_order` (force-complete). */
  mode: CompleteWorkMode;
  /** The operation target, or the work order's quantity ordered. */
  target?: number;
  /** @deprecated Alias of `target`, kept for older call sites. */
  defaultQuantityComplete?: number;
  /** Good already recorded — the prefill (an operation's count, or the work-order header). */
  recordedComplete: number;
  /**
   * Operation mode: the good the FLOOR's labor record supports — the server's
   * `quantity_recorded_good` (evidence-floored). Booking more than this on an operation with
   * labor needs `quantity_override_reason`. Defaults to `recordedComplete`.
   */
  floorRecorded?: number | null;
  /**
   * Scrap already recorded — the prefill and the floor the total never drops below.
   * `null` = unknown (a list summary): the field starts at 0 and 0 keeps whatever is
   * recorded.
   */
  recordedScrapped?: number | null;
  /** Operation mode: target units that never reached it (closed short upstream). */
  upstreamShort?: number | null;
  /** Operation mode: the most good the server accepts (`quantity_good_ceiling`). */
  goodCeiling?: number | null;
  /** Work-order mode: units the floor already accounted as lost. Unknown when absent. */
  accountedLoss?: number | null;
  /** Work-order mode: the most the floor's recorded good supports. Unknown when absent. */
  deliverable?: number | null;
  /**
   * Operation mode: the floor may have clocked labor into this operation, so booking
   * more good than it recorded needs a reason. Defaults to true (the safe reading).
   */
  laborRecorded?: boolean;
  /** A laser nest: the target is sheet runs. */
  isNest?: boolean;
  /** How messages name the subject, e.g. "Op 10" (default "this operation"). */
  subjectLabel?: string;
}

const fmt = formatShortQuantity;

function finiteOrNull(value: number | null | undefined): number | null {
  if (value == null) return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

export function CompleteWorkModal({
  open,
  onClose,
  onSubmit,
  submitting,
  title,
  subtitle,
  error,
  scrapHint,
  mode,
  target,
  defaultQuantityComplete,
  recordedComplete,
  floorRecorded,
  recordedScrapped,
  upstreamShort,
  goodCeiling,
  accountedLoss,
  deliverable,
  laborRecorded = true,
  isNest = false,
  subjectLabel,
}: CompleteWorkModalProps) {
  const isOperation = mode === 'operation';
  const subject = subjectLabel || (isOperation ? 'this operation' : 'this work order');
  const targetQty = Math.max(0, finiteOrNull(target ?? defaultQuantityComplete) ?? 0);
  const recordedGood = Math.max(0, finiteOrNull(recordedComplete) ?? 0);
  // What the server judges an override against: the floor's evidence-floored good.
  const floorGood = Math.max(recordedGood, finiteOrNull(floorRecorded) ?? recordedGood);
  const recordedScrapKnown = finiteOrNull(recordedScrapped);
  const recordedScrap = Math.max(0, recordedScrapKnown ?? 0);
  const upstream = Math.max(0, finiteOrNull(upstreamShort) ?? 0);
  // Units that reached the work: an operation's target less what an upstream close
  // declared short; a work order's quantity ordered.
  const received = isOperation ? operationReceived({ target: targetQty, upstreamShort: upstream }) : targetQty;
  // The server's hard cap on good: the operation's good ceiling, or the order.
  const cap = isOperation ? resolveGoodCeiling(targetQty, goodCeiling) : targetQty;
  const loss = finiteOrNull(accountedLoss);
  const deliverableQty = finiteOrNull(deliverable);

  const [qtyComplete, setQtyComplete] = useState<string>(fmt(recordedGood));
  const [qtyScrapped, setQtyScrapped] = useState<string>(fmt(recordedScrap));
  const [scrap, setScrap] = useState(EMPTY_SCRAP_SELECTION);
  const [overrideReason, setOverrideReason] = useState('');
  // Sticky for the dialog session: once the server has asked for an override
  // reason, the field stays up and required until the dialog closes.
  const [overrideDemanded, setOverrideDemanded] = useState(false);
  // Company scrap codes ([] -> legacy SCRAP_REASONS fallback, fail-soft).
  const { codes: scrapCodes } = useScrapReasonCodes();

  // Reset to the RECORDED totals each time the dialog opens, so a prior
  // session's entries never leak into the next completion. Only on the
  // closed → open transition: a background refresh while the dialog is up must
  // not wipe what the user typed (the server re-checks every figure anyway).
  const wasOpen = useRef(false);
  useEffect(() => {
    if (open && !wasOpen.current) {
      setQtyComplete(fmt(recordedGood));
      setQtyScrapped(fmt(recordedScrap));
      setScrap(EMPTY_SCRAP_SELECTION);
      setOverrideReason('');
      setOverrideDemanded(false);
    }
    wasOpen.current = open;
  }, [open, recordedGood, recordedScrap]);

  useEffect(() => {
    if (open && isOverrideReasonRefusal(error)) setOverrideDemanded(true);
  }, [open, error]);

  const completeNum = Number(qtyComplete);
  const scrappedNum = qtyScrapped.trim() === '' ? 0 : Number(qtyScrapped);
  const goodValid = qtyComplete.trim() !== '' && Number.isFinite(completeNum) && completeNum >= 0;
  const scrapValid = Number.isFinite(scrappedNum) && scrappedNum >= 0;
  const good = goodValid ? completeNum : recordedGood;
  // The server keeps the larger of the recorded and the entered scrap total, so the
  // accounting reads it the same way (a lowered entry is refused below anyway).
  const scrapTotal = scrapValid ? Math.max(scrappedNum, recordedScrap) : recordedScrap;

  const completeError = useMemo(() => {
    if (qtyComplete.trim() === '') return 'Required';
    if (!Number.isFinite(completeNum) || completeNum < 0) return 'Must be a non-negative number';
    if (completeNum > cap + SHORT_EPSILON) {
      return cap < targetQty - SHORT_EPSILON
        ? `Cannot exceed ${fmt(cap)} — ${fmt(targetQty - cap)} never reached ${subject} (closed short upstream)`
        : `Cannot exceed ${fmt(cap)}`;
    }
    return null;
  }, [qtyComplete, completeNum, cap, targetQty, subject]);

  const scrappedError = useMemo(() => {
    if (!scrapValid) return 'Must be a non-negative number';
    if (recordedScrapKnown != null && scrappedNum < recordedScrap - SHORT_EPSILON) {
      return `Cannot be less than the ${fmt(recordedScrap)} already recorded — scrap is never lowered here`;
    }
    return null;
  }, [scrapValid, scrappedNum, recordedScrap, recordedScrapKnown]);

  // A scrap reason is required only for scrap this submit ADDS. The recorded scrap
  // already carries its own reason, and the server never lowers the total.
  const scrapRose = scrapValid && scrappedNum > recordedScrap + SHORT_EPSILON;
  const needsReason = scrapRose;
  const reasonMissing = needsReason && !isScrapSelectionComplete(scrapCodes, scrap);

  const goodRose = goodValid && completeNum > floorGood + SHORT_EPSILON;
  const exceedsDeliverable =
    !isOperation && goodValid && deliverableQty != null && completeNum > deliverableQty + SHORT_EPSILON;
  // The refusal on screen asks for the reason now; the sticky flag keeps asking
  // after the next submit clears the error.
  const serverAsksOverride = overrideDemanded || isOverrideReasonRefusal(error);
  const overrideShown = isOperation ? goodRose || serverAsksOverride : exceedsDeliverable || serverAsksOverride;
  const overrideRequired = isOperation
    ? (goodRose && laborRecorded) || serverAsksOverride
    : exceedsDeliverable || serverAsksOverride;
  const overrideMissing = overrideShown && overrideRequired && overrideReason.trim() === '';

  // ---- The accounting the server will apply (its figures, its arithmetic). ----
  const operationShort = isOperation
    ? operationUnaccounted({ target: targetQty, upstreamShort: upstream, complete: good, scrapped: scrapTotal })
    : 0;
  const workOrderCovered = Math.max(loss ?? 0, scrapTotal);
  const workOrderShortRaw = isOperation ? 0 : targetQty - good - workOrderCovered;
  const workOrderShort = workOrderShortRaw > SHORT_EPSILON ? workOrderShortRaw : 0;
  // Unknown accounted loss (a list summary) is never read as zero: the floor may
  // already have reported the shortfall, so the readout is advisory there.
  const workOrderShortBlocks = !isOperation && loss != null && workOrderShort > 0;

  const leavesOpen = isOperation && operationShort > 0;
  const changed = (goodValid && Math.abs(completeNum - recordedGood) > SHORT_EPSILON) || scrapRose;
  const nothingToSave = leavesOpen && !changed;

  // "Book the rest as good": every received unit not already scrapped (an
  // operation), or every ordered unit not already accounted lost (the order).
  const fullCount = isOperation
    ? Math.max(0, Math.min(cap, received - scrapTotal))
    : Math.max(0, targetQty - workOrderCovered);
  const offerFullCount = goodValid && completeNum < fullCount - SHORT_EPSILON;
  // What the floor's own records support — offered FIRST: the honest shortcut is to book
  // what was counted and report the rest as scrap. Booking the full count above it is an
  // assertion the server asks a reason for, and the shortcut says so.
  const floorCount = isOperation ? Math.min(cap, floorGood) : deliverableQty;
  const offerFloorCount =
    goodValid &&
    floorCount != null &&
    Math.abs(completeNum - floorCount) > SHORT_EPSILON &&
    floorCount < fullCount - SHORT_EPSILON;
  const fullCountNeedsReason = isOperation
    ? laborRecorded && fullCount > floorGood + SHORT_EPSILON
    : deliverableQty != null && fullCount > deliverableQty + SHORT_EPSILON;
  // What "report as scrap" sets the scrap TOTAL to so the readout clears.
  const scrapToCover = isOperation ? scrapTotal + operationShort : Math.max(scrapTotal, targetQty - good);

  const canSubmit =
    !submitting &&
    !completeError &&
    !scrappedError &&
    !reasonMissing &&
    !overrideMissing &&
    !nothingToSave &&
    !workOrderShortBlocks;

  const openEntriesRefused = isOperation && isOpenEntriesRefusal(error);

  const buildValues = (acknowledge: boolean): CompleteWorkSubmit => ({
    quantityComplete: completeNum,
    quantityScrapped: scrapRose ? scrappedNum : null,
    scrapReason: needsReason ? scrapSelectionText(scrapCodes, scrap) : undefined,
    scrapReasonCodeId: needsReason && scrapCodes.length > 0 && scrap.codeId != null ? scrap.codeId : undefined,
    overrideReason: overrideShown && overrideReason.trim() ? overrideReason.trim() : undefined,
    ...(acknowledge ? { acknowledgeOpenEntries: true } : {}),
  });

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!canSubmit) return;
    await onSubmit(buildValues(false));
  };

  const handleAcknowledge = async () => {
    if (!canSubmit) return;
    await onSubmit(buildValues(true));
  };

  const reportAsScrap = () => setQtyScrapped(fmt(scrapToCover));

  const overrideHelp = !overrideRequired
    ? 'No one has clocked in to this operation, so a reason is optional. Saved on the audit trail.'
    : isOperation
      ? `The floor recorded ${fmt(recordedGood)}. Required — saved on the audit trail with your name.`
      : deliverableQty != null
        ? `The floor's records support ${fmt(deliverableQty)}. Required — saved on the audit trail with your name.`
        : 'Required — saved on the audit trail with your name.';

  const scrapHelp =
    scrapHint ??
    (recordedScrapKnown != null
      ? `Total scrapped, including the ${fmt(recordedScrap)} already recorded. It is never lowered here.`
      : undefined);

  const showReadouts = !completeError && !scrappedError;

  return (
    <Modal open={open} onClose={onClose} size="md" padded={false} ariaLabelledBy="complete-work-title">
      <form onSubmit={handleSubmit}>
        <div className="modal-header">
          <div>
            <h2 id="complete-work-title" className="text-lg font-semibold text-slate-100">
              {title}
            </h2>
            {subtitle && <p className="mt-0.5 text-xs text-slate-400">{subtitle}</p>}
          </div>
        </div>

        <div className="modal-body space-y-4">
          <FormField
            label="Quantity completed"
            required
            error={completeError}
            help={`Total good, including the ${fmt(recordedGood)} already recorded.`}
          >
            {field => (
              <input
                {...field}
                type="number"
                min={0}
                max={cap}
                step="any"
                autoFocus
                disabled={submitting}
                className={completeError ? 'input-error' : 'input'}
                value={qtyComplete}
                onChange={e => setQtyComplete(e.target.value)}
              />
            )}
          </FormField>
          {(offerFloorCount || offerFullCount) && (
            <div className="flex flex-wrap gap-2">
              {offerFloorCount && floorCount != null && (
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  disabled={submitting}
                  onClick={() => setQtyComplete(fmt(floorCount))}
                >
                  Use floor count ({fmt(floorCount)})
                </Button>
              )}
              {offerFullCount && (
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  disabled={submitting}
                  onClick={() => setQtyComplete(fmt(fullCount))}
                >
                  {fullCountNeedsReason
                    ? `Book ${fmt(fullCount)} good (needs a reason)`
                    : `Use full count (${fmt(fullCount)})`}
                </Button>
              )}
            </div>
          )}

          {overrideShown && (
            <FormField label="Why record more than the floor counted?" required={overrideRequired} help={overrideHelp}>
              {field => (
                <input
                  {...field}
                  type="text"
                  maxLength={255}
                  disabled={submitting}
                  className="input"
                  value={overrideReason}
                  onChange={e => setOverrideReason(e.target.value)}
                  placeholder="e.g. the operator finished the last pieces but never reported them"
                />
              )}
            </FormField>
          )}

          <FormField label="Quantity scrapped" error={scrappedError} help={scrapHelp}>
            {field => (
              <input
                {...field}
                type="number"
                min={recordedScrapKnown != null ? recordedScrap : 0}
                step="any"
                disabled={submitting}
                className={scrappedError ? 'input-error' : 'input'}
                value={qtyScrapped}
                onChange={e => setQtyScrapped(e.target.value)}
              />
            )}
          </FormField>

          {/* Scrap reason is required for defect traceability whenever the scrap
              total RISES — company scrap codes when defined, legacy SCRAP_REASONS
              fallback otherwise (shared ScrapReasonFields fragment). */}
          {needsReason && (
            <ScrapReasonFields codes={scrapCodes} value={scrap} onChange={setScrap} disabled={submitting} />
          )}

          {/* SHORT — the server's accounting, shown before the user submits. */}
          {leavesOpen && showReadouts && (
            <div
              role="status"
              data-testid="complete-work-short"
              className="rounded-sm border border-fd-amber/50 bg-fd-amber/10 p-3 text-sm text-fd-body"
            >
              <p className="flex items-center gap-1.5 font-semibold text-fd-amber">
                <ExclamationTriangleIcon className="h-4 w-4 flex-shrink-0" aria-hidden="true" />
                Short {fmt(operationShort)} {shortUnitLabel(isNest, operationShort)}
              </p>
              <p className="mt-1">
                {fmt(operationShort)} of {fmt(received)} not yet accounted for ({fmt(good)} good, {fmt(scrapTotal)}{' '}
                scrapped). Saving keeps {subject} open. To close it, record the finished pieces or report the lost ones
                as scrap with a reason. Pieces never started: lower the order quantity.
                {upstream > SHORT_EPSILON && ` (${fmt(upstream)} never reached it — closed short upstream.)`}
              </p>
              {nothingToSave && (
                <p className="mt-1 text-fd-mute">Nothing to save yet — change a count, or Cancel to leave it open.</p>
              )}
              <Button
                type="button"
                variant="secondary"
                size="sm"
                className="mt-2"
                disabled={submitting}
                onClick={reportAsScrap}
              >
                Report {fmt(operationShort)} as scrap
              </Button>
            </div>
          )}
          {workOrderShort > 0 && showReadouts && (
            <div
              role="status"
              data-testid="complete-work-short"
              className="rounded-sm border border-fd-amber/50 bg-fd-amber/10 p-3 text-sm text-fd-body"
            >
              <p className="flex items-center gap-1.5 font-semibold text-fd-amber">
                <ExclamationTriangleIcon className="h-4 w-4 flex-shrink-0" aria-hidden="true" />
                {fmt(workOrderShort)} unaccounted
              </p>
              <p className="mt-1">
                {workOrderShortBlocks
                  ? `${fmt(workOrderShort)} of ${fmt(targetQty)} unaccounted for (${fmt(good)} complete, ${fmt(workOrderCovered)} scrapped) — report it as scrap with a reason, or leave the work order open.`
                  : `${fmt(workOrderShort)} below the order with no scrap entered here. The server accepts this only if the floor already reported that many as scrap; otherwise report it as scrap with a reason, or leave the work order open.`}
              </p>
              <Button
                type="button"
                variant="secondary"
                size="sm"
                className="mt-2"
                disabled={submitting}
                onClick={reportAsScrap}
              >
                Set scrap to {fmt(scrapToCover)}
              </Button>
            </div>
          )}

          {error && (
            <div role="alert" className="rounded border border-fd-red/40 bg-fd-red/10 p-3 text-sm text-fd-red">
              <p>{error}</p>
              {openEntriesRefused && (
                <LoadingButton
                  type="button"
                  variant="danger"
                  size="sm"
                  className="mt-2"
                  loading={submitting}
                  loadingText="Closing..."
                  disabled={!canSubmit}
                  onClick={handleAcknowledge}
                >
                  Clock them out and close
                </LoadingButton>
              )}
            </div>
          )}
        </div>

        <div className="modal-footer">
          <Button type="button" variant="secondary" onClick={onClose} disabled={submitting}>
            Cancel
          </Button>
          <LoadingButton
            type="submit"
            loading={submitting}
            loadingText={leavesOpen ? 'Saving...' : 'Completing...'}
            disabled={!canSubmit}
          >
            <CheckCircleIcon className="mr-2 h-5 w-5" />
            {leavesOpen ? 'Save progress · leave open' : 'Complete'}
          </LoadingButton>
        </div>
      </form>
    </Modal>
  );
}

export default CompleteWorkModal;
