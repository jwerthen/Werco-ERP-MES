/**
 * Duplicate a work order — copy the PLAN onto a new draft.
 *
 * The motivating case is a laser nest package: re-running one meant
 * re-uploading the PDF and re-confirming 40+ nests by hand. Everything the
 * planner already confirmed (operations, nests, sheet-part ties) is exactly the
 * thing worth copying; nothing about the previous run is.
 *
 * ---------------------------------------------------------------------------
 * WHY THE DUE DATE STARTS BLANK
 * ---------------------------------------------------------------------------
 * Inheriting the source's due date is the obvious default and the wrong one: a
 * re-run of a job that shipped last month would be born overdue — red on the
 * dispatch board, counted against OTD — for a promise nobody made. Blank is a
 * missing date, which reads as "unscheduled" everywhere; a stale date reads as
 * "late". The hint under the field says so, because a planner who does not know
 * why the field is empty will helpfully copy the old date back in.
 *
 * ---------------------------------------------------------------------------
 * A NEST-BEARING WORK ORDER'S QUANTITY IS DERIVED, NOT TYPED
 * ---------------------------------------------------------------------------
 * `quantity_ordered` on a laser work order is DEFINED as the sum of its nests'
 * planned runs, and every nest path in the backend re-asserts it. The duplicate
 * endpoint therefore ignores the quantity sent for a nest-bearing source and
 * stores the derived sum. So the field is DISABLED, not hidden, with the reason
 * on it: a missing field is a mystery, a disabled one with a reason is an
 * explanation. The quantity still rides along in the request (the schema
 * requires it, > 0) — the server simply overrules it.
 *
 * The success toast quotes the quantity off the RESPONSE for the same reason:
 * the planner is never shown a number the server did not store.
 *
 * ---------------------------------------------------------------------------
 * SERVER-GATED, THEREFORE NON-OPTIMISTIC
 * ---------------------------------------------------------------------------
 * The endpoint is role-gated (admin/manager/supervisor) and may refuse for
 * reasons this form cannot know. So nothing is painted before the server
 * answers: `pending` holds the spinner + double-click guard, disables Cancel,
 * and refuses backdrop/Escape dismissal, and a refusal leaves the dialog open
 * with the server's `detail` rendered verbatim rather than closing over it.
 *
 * ---------------------------------------------------------------------------
 * A PARTIAL COPY STOPS THE FLOW; A CLEAN ONE STAYS ONE CLICK
 * ---------------------------------------------------------------------------
 * The response is an envelope whose two skip lists say what the server could
 * NOT carry across. A skipped material tie means the new job has no demand for
 * that material: no shortage is raised, the nests run, and stock is never
 * deducted. That omission reaches the audit chain either way — the whole reason
 * the envelope exists is to put it in front of the person who pressed the
 * button, while they can still act on it.
 *
 * A toast cannot do that job here. It self-dismisses after 4s and it fires
 * while the caller is navigating to the new work order, so the one surface that
 * named the omission is gone before the destination has painted, and nothing on
 * the destination re-states it. So the two paths deliberately DIVERGE:
 *
 *   clean copy    → success toast, hand the envelope to the caller, close.
 *                   One click, exactly as before.
 *   partial copy  → no toast, no navigation, no auto-close. The dialog switches
 *                   to a RESULT view that itemizes what was not copied and
 *                   makes the planner choose "go to the copy" or "dismiss".
 *
 * The result view is a RESULT, not an error: the work order exists and is a
 * valid draft, so it is amber and it names the new work order number. Rendering
 * it as a failure would send someone hunting for a job that is already there.
 */

import React, { useEffect, useId, useRef, useState } from 'react';
import api from '../../services/api';
import { Button, FormField, LoadingButton, Modal, useToast } from '../ui';
import { toDisplayString } from '../../utils/apiError';
import { formatOperationLabel, hasOperationNumber } from '../../utils/operationLabel';
import type {
  WorkOrder,
  WorkOrderDuplicateResult,
  WorkOrderDuplicateSkippedAllocation,
  WorkOrderDuplicateSkippedOperation,
} from '../../types';

/**
 * The fields this dialog needs off the source work order. Deliberately narrow
 * so BOTH callers fit: the detail page holds a full `WorkOrder`, the list page
 * a `WorkOrderSummary`.
 */
export interface DuplicateWorkOrderSource {
  id: number;
  work_order_number: string;
  quantity_ordered: number;
}

export interface DuplicateWorkOrderModalProps {
  open: boolean;
  /** `null` while closed; the work order being copied. */
  workOrder: DuplicateWorkOrderSource | null;
  /**
   * Whether the source carries laser nests — which makes its quantity derived
   * rather than typed (see the header). Pass it whenever the caller can answer
   * it: the detail page has the operations loaded, and the list page can rule
   * it out for any row that is not `laser_cutting`.
   *
   * Leave it undefined and the dialog resolves it with one `getWorkOrder`. That
   * fallback is deliberately the LAST resort, not the default: the detail
   * endpoint reconciles operation quantities and can COMMIT writes against the
   * source work order, which is far more than a dialog should do to learn one
   * boolean.
   */
  hasLaserNests?: boolean;
  onClose: () => void;
  /**
   * The caller's hand-off — both callers navigate to the new work order. Fired
   * only after the server has created the copy, with the WHOLE envelope, and on
   * a PARTIAL copy only once the planner has read the result view and chosen to
   * go there. Dismissing that view does not fire it: nothing should navigate
   * out from under a list of omissions the planner just declined to follow.
   */
  onDuplicated: (result: WorkOrderDuplicateResult) => void;
}

/**
 * Machine-readable skip reason → the phrase shown to a planner.
 *
 * The server owns this vocabulary and can add to it, so every lookup falls back
 * to the raw token rather than asserting a reason. `skipSummary` used to state
 * "(its laser nest was deleted)" for ANY skipped operation, which is true only
 * while `laser_nest_deleted` is the only reason there is — a sentence that
 * becomes a falsehood the moment the server grows a second one.
 */
const OPERATION_SKIP_REASONS: Record<string, string> = {
  laser_nest_deleted: 'its laser nest was deleted',
};

const ALLOCATION_SKIP_REASONS: Record<string, string> = {
  part_not_available: 'the tied part is no longer available',
  operation_not_copied: 'its operation was not copied',
  nest_runs_unavailable: 'no nest run count to plan against',
};

/** The phrase for one reason, falling back to the server's own token. */
function reasonLabel(reason: string, labels: Record<string, string>): string {
  return labels[reason] ?? reason;
}

/**
 * A parenthetical naming the reason — but ONLY when every entry shares one, and
 * only when we have a phrase for it. Mixed reasons get no parenthetical rather
 * than one that describes a subset.
 */
function sharedReasonNote(entries: Array<{ reason: string }>, labels: Record<string, string>): string {
  const distinct = Array.from(new Set(entries.map((entry) => entry.reason)));
  if (distinct.length !== 1) return '';
  const label = labels[distinct[0]];
  return label ? ` (${label})` : '';
}

/** Did the server leave anything behind? Both lists empty is the "clean copy" signal. */
function hasSkips(result: WorkOrderDuplicateResult): boolean {
  return (result.skipped_operations?.length ?? 0) > 0 || (result.skipped_material_allocations?.length ?? 0) > 0;
}

/**
 * A sentence for what the server refused to carry across, or `null` when the
 * copy was clean.
 *
 * This is not decoration. A skipped material tie means the new job carries NO
 * demand for that material: no shortage is raised, the nests run, and stock is
 * never deducted. That omission is on the audit chain either way — this is what
 * puts it in front of the person who pressed the button, while they can still
 * act on it.
 *
 * Counts only. Ties carry a per-row reason in the itemized list below it, where
 * a mixed set can be shown honestly; collapsing three different tie reasons into
 * one parenthetical here could not be.
 */
function skipSummary(result: WorkOrderDuplicateResult): string | null {
  const operations = result.skipped_operations ?? [];
  const ties = result.skipped_material_allocations ?? [];
  if (operations.length === 0 && ties.length === 0) return null;
  const parts: string[] = [];
  if (operations.length > 0) {
    parts.push(
      `${operations.length} operation${operations.length === 1 ? '' : 's'}` +
        sharedReasonNote(operations, OPERATION_SKIP_REASONS)
    );
  }
  if (ties.length > 0) parts.push(`${ties.length} material tie${ties.length === 1 ? '' : 's'}`);
  return `Not copied: ${parts.join(' and ')}. Check the new work order before releasing it.`;
}

/** How a skipped operation is named. Falls back through the ids the envelope carries. */
function operationLabel(operation: WorkOrderDuplicateSkippedOperation): string {
  // The FULL label, matching the `Seq 20` / `Operation #72` fallbacks below --
  // these three are one vocabulary, and a bare "20" among them names nothing.
  if (hasOperationNumber(operation.operation_number)) {
    return formatOperationLabel(operation.operation_number);
  }
  if (operation.sequence != null) return `Seq ${operation.sequence}`;
  return `Operation #${operation.source_operation_id}`;
}

/**
 * How a skipped tie is named. The envelope carries `part_id`, not a part
 * number — an internal id is still enough to look the part up, and far more
 * than a bare count.
 */
function allocationLabel(allocation: WorkOrderDuplicateSkippedAllocation): string {
  return allocation.part_id != null ? `Part #${allocation.part_id}` : `Tie #${allocation.source_allocation_id}`;
}

/** Pull a displayable `detail` off any error shape, incl. a structured 409 body. */
function duplicateErrorDetail(err: unknown, fallback: string): string {
  const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  const rendered = toDisplayString(detail);
  if (rendered.trim()) return rendered;
  const message = (err as { message?: unknown })?.message;
  if (typeof message === 'string' && message.trim()) return message;
  return fallback;
}

/** Does this work order carry live laser nests? Nests hang off operations. */
function carriesLaserNests(wo: Pick<WorkOrder, 'operations'> | null | undefined): boolean {
  return (wo?.operations ?? []).some((operation) => Boolean(operation.laser_nest));
}

export default function DuplicateWorkOrderModal({
  open,
  workOrder,
  hasLaserNests,
  onClose,
  onDuplicated,
}: DuplicateWorkOrderModalProps) {
  const { showToast } = useToast();
  const titleId = useId();
  const [quantity, setQuantity] = useState('');
  const [dueDate, setDueDate] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  /** `null` = not resolved yet (a read is in flight). */
  const [nestBearing, setNestBearing] = useState<boolean | null>(null);
  /**
   * Set ONLY for a partial copy — the envelope the result view renders. A clean
   * copy never lands here: it toasts, hands over, and closes.
   */
  const [partialResult, setPartialResult] = useState<WorkOrderDuplicateResult | null>(null);
  const goToCopyRef = useRef<HTMLButtonElement | null>(null);

  const sourceId = workOrder?.id ?? null;
  const sourceQuantity = workOrder?.quantity_ordered ?? null;

  // Reset on every open. Quantity prefills from the source (the common case is
  // the same run again); the due date stays blank on purpose — see the header.
  useEffect(() => {
    if (!open || sourceId == null) return;
    setQuantity(sourceQuantity != null ? String(sourceQuantity) : '');
    setDueDate('');
    setError('');
    setPartialResult(null);

    // The caller already knows (detail page) — believe it, read nothing.
    if (hasLaserNests !== undefined) {
      setNestBearing(hasLaserNests);
      return;
    }

    let cancelled = false;
    setNestBearing(null);
    api
      .getWorkOrder(sourceId)
      .then((full: WorkOrder) => {
        if (!cancelled) setNestBearing(carriesLaserNests(full));
      })
      .catch(() => {
        // Could not tell. Fall back to the ordinary editable field rather than
        // leaving a planner staring at a locked one: for a nest-bearing source
        // the server overrules the number anyway, and the success toast quotes
        // what was actually stored.
        if (!cancelled) setNestBearing(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, sourceId, sourceQuantity, hasLaserNests]);

  // Locked while a nest-bearing source is confirmed, and while we do not yet
  // know — never invite a number that is about to become un-typeable.
  const quantityLocked = nestBearing !== false;
  const resolvingNests = nestBearing === null;

  // Move focus onto the go-to control when the result view replaces the form:
  // the button that had focus just unmounted, and a keyboard user must not be
  // left on <body> with a dialog full of unread omissions in front of them.
  useEffect(() => {
    if (partialResult) goToCopyRef.current?.focus();
  }, [partialResult]);

  const close = () => {
    // Never dismiss mid-request: the copy may already exist server-side, and
    // this dialog must reflect only what the server actually did.
    if (submitting) return;
    onClose();
  };

  /** The result view's primary action: hand the envelope over, then close. */
  const goToCopy = () => {
    if (!partialResult) return;
    onDuplicated(partialResult);
    onClose();
  };

  const handleSubmit = async () => {
    if (submitting || !workOrder) return;

    const parsed = Number(quantity);
    if (!quantityLocked && (quantity.trim() === '' || !Number.isFinite(parsed) || parsed <= 0)) {
      setError('Quantity must be greater than zero.');
      return;
    }

    // Locked: the server derives the real number from the copied nests and
    // ignores this one, but the schema still requires a positive value, so send
    // the source's own quantity (already the sum of the runs being copied).
    const requestedQuantity = quantityLocked
      ? Math.max(1, Number(sourceQuantity) || 0)
      : parsed;

    setSubmitting(true);
    setError('');
    try {
      const result = await api.duplicateWorkOrder(workOrder.id, {
        quantity_ordered: requestedQuantity,
        // Blank means "no promise yet", which the server stores as no due date.
        due_date: dueDate.trim() === '' ? null : dueDate,
      });
      // A PARTIAL copy stops here. No toast (the panel says it, and saying it
      // twice trains people to read neither), no hand-off, no close: the skip
      // lists are the only surface that will ever name what did not come
      // across, so they do not get to scroll past during a route change.
      if (hasSkips(result)) {
        setPartialResult(result);
        return;
      }

      const created = result.work_order;
      // Quantity comes off the RESPONSE, never off the form: on a nest-bearing
      // work order the server stores the derived sum, not what was submitted.
      const storedQuantity = Number(created.quantity_ordered);
      const quantityNote = Number.isFinite(storedQuantity) ? ` — qty ${storedQuantity}` : '';
      // A clean copy stays exactly one click — that contrast is the point.
      showToast(
        'success',
        `${created.work_order_number} created as a draft${quantityNote}, copied from ${workOrder.work_order_number}.` +
          ' Review it, then release.'
      );
      onDuplicated(result);
      onClose();
    } catch (err) {
      setError(duplicateErrorDetail(err, 'Failed to duplicate this work order'));
    } finally {
      setSubmitting(false);
    }
  };

  const createdQuantity = partialResult ? Number(partialResult.work_order.quantity_ordered) : NaN;
  const createdQuantityNote = Number.isFinite(createdQuantity) ? ` — qty ${createdQuantity}` : '';
  const skippedOperations = partialResult?.skipped_operations ?? [];
  const skippedTies = partialResult?.skipped_material_allocations ?? [];

  return (
    <Modal
      open={open && workOrder !== null}
      onClose={close}
      size="lg"
      padded={false}
      scroll={false}
      // Escape still dismisses the result view — that is a deliberate keypress.
      // A stray backdrop click is not, and it must not be what makes the only
      // record of an un-copied material tie disappear.
      closeOnBackdrop={!submitting && partialResult === null}
      closeOnEscape={!submitting}
      ariaLabelledBy={titleId}
    >
      {workOrder && (
        <>
          <div className="modal-header">
            <h3 id={titleId} className="text-lg font-semibold">
              {partialResult
                ? // Not "Duplicate work order" any more — the copy exists. The
                  // heading names the NEW work order so the planner reads this
                  // as a result, not as a form that failed to submit.
                  `Copied with omissions — ${partialResult.work_order.work_order_number}`
                : `Duplicate work order — ${workOrder.work_order_number}`}
            </h3>
          </div>

          {partialResult ? (
            <div className="modal-body max-h-[70vh] space-y-4 overflow-y-auto">
              {/* Amber, not red: the work order WAS created and is a valid
                  draft. Calling this a failure sends someone hunting for a job
                  that is already there. */}
              <div
                role="status"
                data-testid="duplicate-wo-skips"
                className="rounded-sm border border-amber-500/50 bg-amber-500/5 px-4 py-3 text-sm text-slate-300"
              >
                <p className="font-semibold text-amber-200">
                  <span className="font-mono">{partialResult.work_order.work_order_number}</span> created as a draft
                  {createdQuantityNote}, copied from{' '}
                  <span className="font-mono">{workOrder.work_order_number}</span>.
                </p>
                <p className="mt-1">{skipSummary(partialResult)}</p>
              </div>

              {skippedOperations.length > 0 && (
                <section>
                  <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-400">
                    Operations not copied
                  </h4>
                  <ul
                    data-testid="duplicate-wo-skipped-operations"
                    className="mt-2 divide-y divide-fd-line border border-fd-line"
                  >
                    {skippedOperations.map((operation) => (
                      <li
                        key={operation.source_operation_id}
                        className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1 px-3 py-2"
                      >
                        <span className="font-mono text-sm text-slate-200">{operationLabel(operation)}</span>
                        <span className="text-xs text-slate-400">
                          {reasonLabel(operation.reason, OPERATION_SKIP_REASONS)}
                        </span>
                      </li>
                    ))}
                  </ul>
                </section>
              )}

              {skippedTies.length > 0 && (
                <section>
                  <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-400">
                    Material ties not copied
                  </h4>
                  <ul
                    data-testid="duplicate-wo-skipped-ties"
                    className="mt-2 divide-y divide-fd-line border border-fd-line"
                  >
                    {skippedTies.map((tie) => (
                      <li
                        key={tie.source_allocation_id}
                        className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1 px-3 py-2"
                      >
                        <span className="font-mono text-sm text-slate-200">{allocationLabel(tie)}</span>
                        <span className="text-xs text-slate-400">
                          {reasonLabel(tie.reason, ALLOCATION_SKIP_REASONS)}
                        </span>
                      </li>
                    ))}
                  </ul>
                  <p className="mt-2 text-xs text-slate-400">
                    Nothing on the new draft records these. Re-tie the material by hand before releasing it: a job
                    with no tie carries no demand, so no shortage shows, the nests run, and stock is never deducted.
                  </p>
                </section>
              )}
            </div>
          ) : (
          <div className="modal-body max-h-[70vh] space-y-4 overflow-y-auto">
            <div className="rounded-sm border border-fd-line bg-fd-sunken/40 px-3 py-2 text-xs text-slate-400">
              Copies the plan: operations and their setup/run instructions, any laser nests (CNC number, material,
              thickness, sheet size, planned runs, work center) and any open material ties. What the last run
              actually did stays with{' '}
              <span className="font-mono text-slate-300">{workOrder.work_order_number}</span>: quantities, actual
              hours, lot/serial. The copy starts as a <strong className="text-slate-300">draft</strong> under a new
              work order number.
            </div>

            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <FormField
                label="Quantity"
                required={!quantityLocked}
                help={
                  resolvingNests
                    ? 'Checking whether this job’s quantity comes from its nests…'
                    : quantityLocked
                      ? 'Derived, not typed: this job’s quantity is the sum of its nests’ sheet runs, and the copy carries those runs across unchanged. Add or remove nests on the new draft to change it.'
                      : `Prefilled from ${workOrder.work_order_number}. Change it if this run is a different size.`
                }
              >
                {(field) => (
                  <input
                    {...field}
                    type="number"
                    inputMode="numeric"
                    min={1}
                    step={1}
                    className="input"
                    // Disabled, never hidden: a missing field is a mystery, a
                    // disabled one carrying its reason is an explanation.
                    disabled={submitting || quantityLocked}
                    value={quantity}
                    onChange={(e) => setQuantity(e.target.value)}
                  />
                )}
              </FormField>

              <FormField
                label="Due date"
                help="Left blank on purpose — carrying over the old date would make the copy overdue the moment it exists, on the dispatch board and in OTD. Set the date this run is actually promised for, or leave it unscheduled."
              >
                {(field) => (
                  <input
                    {...field}
                    type="date"
                    className="input"
                    disabled={submitting}
                    value={dueDate}
                    onChange={(e) => setDueDate(e.target.value)}
                  />
                )}
              </FormField>
            </div>

            {/* Verbatim server refusal — the primary display for a gated write. */}
            {error && (
              <div
                role="alert"
                data-testid="duplicate-wo-error"
                className="rounded-sm border border-red-500/60 bg-red-500/10 px-4 py-3 text-sm font-semibold text-red-300"
              >
                {error}
              </div>
            )}
          </div>
          )}

          <div className="modal-footer">
            {partialResult ? (
              <>
                {/* Dismissing does NOT navigate: nothing should move the planner
                    out from under a list of omissions they just declined to
                    follow. The copy still exists either way. */}
                <Button variant="secondary" onClick={close}>
                  Dismiss
                </Button>
                <Button ref={goToCopyRef} onClick={goToCopy}>
                  Go to {partialResult.work_order.work_order_number}
                </Button>
              </>
            ) : (
              <>
                <Button variant="secondary" onClick={close} disabled={submitting}>
                  Cancel
                </Button>
                {/* Deliberately NOT disabled on an empty quantity — a dead button
                    says nothing about why; the handler's guard names the reason. */}
                <LoadingButton loading={submitting} loadingText="Duplicating…" onClick={handleSubmit}>
                  Duplicate
                </LoadingButton>
              </>
            )}
          </div>
        </>
      )}
    </Modal>
  );
}
