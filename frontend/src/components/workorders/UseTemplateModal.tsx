/**
 * Use a work order template — run the saved plan again as a new DRAFT.
 *
 * The server side of this is `duplicate_work_order` against the work order the
 * template points at: the SAME copy engine the Duplicate dialog drives, and the
 * SAME response envelope. So this dialog is deliberately the Duplicate dialog's
 * twin, and the properties below are copied because they were decisions, not
 * because the file was.
 *
 * ---------------------------------------------------------------------------
 * THE DUE DATE STARTS BLANK
 * ---------------------------------------------------------------------------
 * A template's whole purpose is re-running a job that already ran, so inheriting
 * a date would be maximally wrong here: the new job would be born overdue — red
 * on the dispatch board, counted against OTD — for a promise nobody made. Blank
 * is a missing date, which reads as "unscheduled" everywhere; a stale date reads
 * as "late". The hint under the field says so, because a planner who does not
 * know why the field is empty will helpfully type the old date back in.
 *
 * ---------------------------------------------------------------------------
 * A NEST-BEARING TEMPLATE'S QUANTITY IS DERIVED, NOT TYPED
 * ---------------------------------------------------------------------------
 * `plan.nest_count > 0` means the new work order's `quantity_ordered` is DEFINED
 * as the sum of the copied nests' planned runs. The server derives it and
 * overrules whatever this form sends. So the field is DISABLED, not hidden, with
 * the reason on it, and the success toast quotes the quantity off the RESPONSE —
 * the planner is never shown a number the server did not store.
 *
 * ---------------------------------------------------------------------------
 * BOTH FIELDS ARE OPTIONAL, WHICH IS WHAT MAKES THIS ONE CLICK
 * ---------------------------------------------------------------------------
 * Quantity is sent only when the field is live and filled in. Left blank, the
 * server resolves the first positive of (template default, source work order
 * quantity) and refuses 422 rather than fabricating a 1 — a quantity of one on a
 * job that should have run fifty is a plan nobody approved. The due date is sent
 * as an explicit `null` when blank, because unscheduled is a decision.
 *
 * ---------------------------------------------------------------------------
 * AN UNUSABLE TEMPLATE IS SHOWN, NOT HIDDEN
 * ---------------------------------------------------------------------------
 * When the source work order has been deleted the catalog still lists the
 * template (hiding it tells the planner nothing) and the server refuses the use
 * with a 409. This dialog refuses first, in words, with no submit control at
 * all: the fix is to restore the work order or delete the template, and both
 * start with reading the reason.
 *
 * ---------------------------------------------------------------------------
 * A PARTIAL COPY STOPS THE FLOW; A CLEAN ONE STAYS ONE CLICK
 * ---------------------------------------------------------------------------
 * The envelope's skip lists say what the server could NOT carry across. A
 * skipped material tie means the new job has no demand for that material: no
 * shortage is raised, the nests run, and stock is never deducted. A toast cannot
 * carry that news — it self-dismisses on a timer and fires while the caller is
 * navigating away — so a partial copy renders the shared `<CopyPlanSkipReport>`
 * instead: no toast, no hand-off, no auto-close, and the planner chooses "go to
 * the copy" or "dismiss".
 */

import React, { useEffect, useId, useRef, useState } from 'react';
import api from '../../services/api';
import { Button, FormField, LoadingButton, Modal, useToast } from '../ui';
import { CopyPlanSkipReport, hasSkips, serverErrorDetail, storedQuantityNote } from './copyPlanSkips';
import type { WorkOrderDuplicateResult, WorkOrderTemplate, WorkOrderTemplateUsePayload } from '../../types';

/**
 * Machine-readable `plan.unavailable_reason` → the sentence a planner reads.
 *
 * The server owns this vocabulary and says to treat the set as OPEN, so an
 * unrecognized token is shown verbatim inside a generic sentence rather than
 * being dropped or guessed at — the same rule the copy skip reasons follow.
 */
const TEMPLATE_UNAVAILABLE_REASONS: Record<string, string> = {
  source_work_order_deleted:
    'The work order this template was saved from has been deleted, so there is no plan left to copy. ' +
    'Restore that work order, or delete this template and save a new one from a live job.',
};

/** The full sentence for an unusable template, never an empty string. */
export function templateUnavailableSentence(reason?: string | null): string {
  const token = (reason ?? '').trim();
  if (!token) return 'This template cannot be used right now.';
  return TEMPLATE_UNAVAILABLE_REASONS[token] ?? `This template cannot be used right now (${token}).`;
}

export interface UseTemplateModalProps {
  open: boolean;
  /** `null` while closed; the template being run. */
  template: WorkOrderTemplate | null;
  onClose: () => void;
  /**
   * The caller's hand-off — it navigates to the new draft. Fired only after the
   * server has created the work order, with the WHOLE envelope, and on a PARTIAL
   * copy only once the planner has read the result view and chosen to go there.
   * Dismissing that view does not fire it: nothing should navigate out from
   * under a list of omissions the planner just declined to follow.
   */
  onUsed: (result: WorkOrderDuplicateResult) => void;
}

export default function UseTemplateModal({ open, template, onClose, onUsed }: UseTemplateModalProps) {
  const { showToast } = useToast();
  const titleId = useId();
  const [quantity, setQuantity] = useState('');
  const [dueDate, setDueDate] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  /**
   * Set ONLY for a partial copy — the envelope the result view renders. A clean
   * copy never lands here: it toasts, hands over, and closes.
   */
  const [partialResult, setPartialResult] = useState<WorkOrderDuplicateResult | null>(null);
  const goToCopyRef = useRef<HTMLButtonElement | null>(null);

  const templateId = template?.id ?? null;
  const plan = template?.plan;
  const available = plan?.available !== false;
  // Nest-bearing: the copied nests decide the quantity, so the field is locked.
  const quantityLocked = (plan?.nest_count ?? 0) > 0;
  // The prefill the planner saved, falling back to what the source job ran.
  const prefillQuantity = template?.default_quantity ?? plan?.source_quantity_ordered ?? null;

  // Reset on every open. Quantity prefills; the due date stays blank on purpose.
  useEffect(() => {
    if (!open || templateId == null) return;
    setQuantity(prefillQuantity != null ? String(prefillQuantity) : '');
    setDueDate('');
    setError('');
    setPartialResult(null);
  }, [open, templateId, prefillQuantity]);

  // Move focus onto the go-to control when the result view replaces the form:
  // the button that had focus just unmounted, and a keyboard user must not be
  // left on <body> with a dialog full of unread omissions in front of them.
  useEffect(() => {
    if (partialResult) goToCopyRef.current?.focus();
  }, [partialResult]);

  const close = () => {
    // Never dismiss mid-request: the work order may already exist server-side,
    // and this dialog must reflect only what the server actually did.
    if (submitting) return;
    onClose();
  };

  /** The result view's primary action: hand the envelope over, then close. */
  const goToCopy = () => {
    if (!partialResult) return;
    onUsed(partialResult);
    onClose();
  };

  const handleSubmit = async () => {
    if (submitting || !template || !available) return;

    const payload: WorkOrderTemplateUsePayload = {
      // Blank means "no promise yet", which the server stores as no due date.
      due_date: dueDate.trim() === '' ? null : dueDate,
    };

    // Sent only when the field is live AND filled in. Omitting it is not a
    // fallback to zero — it is what lets the server resolve the template's
    // default and then the source work order's own quantity.
    if (!quantityLocked && quantity.trim() !== '') {
      const parsed = Number(quantity);
      if (!Number.isFinite(parsed) || parsed <= 0) {
        setError('Quantity must be greater than zero, or leave it blank to use the saved default.');
        return;
      }
      payload.quantity_ordered = parsed;
    }

    setSubmitting(true);
    setError('');
    try {
      const result = await api.useWorkOrderTemplate(template.id, payload);
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
      // template the server stores the derived sum, not what was submitted.
      showToast(
        'success',
        `${created.work_order_number} created as a draft${storedQuantityNote(result)}, ` +
          `from template "${template.name}". Review it, then release.`
      );
      onUsed(result);
      onClose();
    } catch (err) {
      setError(serverErrorDetail(err, 'Failed to create a work order from this template'));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      open={open && template !== null}
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
      {template && (
        <>
          <div className="modal-header">
            <h3 id={titleId} className="text-lg font-semibold">
              {partialResult
                ? // Not "Use template" any more — the work order exists. The
                  // heading names it so the planner reads this as a result, not
                  // as a form that failed to submit.
                  `Created with omissions — ${partialResult.work_order.work_order_number}`
                : `Use template — ${template.name}`}
            </h3>
          </div>

          {partialResult ? (
            <div className="modal-body max-h-[70vh] space-y-4 overflow-y-auto">
              <CopyPlanSkipReport
                result={partialResult}
                origin={
                  <>
                    from template <span className="font-mono">{template.name}</span>.
                  </>
                }
              />
            </div>
          ) : (
            <div className="modal-body max-h-[70vh] space-y-4 overflow-y-auto">
              {!available ? (
                // No form and no submit control: the only fixes are to restore
                // the work order or delete the template, and both start here.
                <div
                  role="alert"
                  data-testid="use-template-unavailable"
                  className="rounded-sm border border-red-500/60 bg-red-500/10 px-4 py-3 text-sm text-red-200"
                >
                  {templateUnavailableSentence(plan?.unavailable_reason)}
                </div>
              ) : (
                <>
                  <div className="rounded-sm border border-fd-line bg-fd-sunken/40 px-3 py-2 text-xs text-slate-400">
                    Copies the plan from{' '}
                    <span className="font-mono text-slate-300">
                      {plan?.source_work_order_number ?? `work order #${template.source_work_order_id}`}
                    </span>{' '}
                    as it stands right now: operations and their setup/run instructions, any laser nests and any
                    open material ties. What that job actually did stays with it — quantities, actual hours,
                    lot/serial. The new work order starts as a <strong className="text-slate-300">draft</strong>{' '}
                    under a new number, so nothing reaches the floor until somebody releases it.
                  </div>

                  {template.notes?.trim() ? (
                    <p data-testid="use-template-notes" className="text-sm text-surface-500">
                      <span className="font-semibold text-surface-700">Note</span> — {template.notes.trim()}
                    </p>
                  ) : null}

                  <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                    <FormField
                      label="Quantity"
                      help={
                        quantityLocked
                          ? 'Derived, not typed: this job’s quantity is the sum of its nests’ sheet runs, and the copy carries those runs across unchanged. Add or remove nests on the new draft to change it.'
                          : prefillQuantity != null
                            ? 'Prefilled from this template. Change it if this run is a different size, or clear it to fall back to the source work order’s quantity.'
                            : 'Optional. Left blank, the quantity comes from the source work order.'
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
                          // Disabled, never hidden: a missing field is a
                          // mystery, a disabled one with a reason is not.
                          disabled={submitting || quantityLocked}
                          value={quantityLocked ? '' : quantity}
                          onChange={(e) => setQuantity(e.target.value)}
                        />
                      )}
                    </FormField>

                    <FormField
                      label="Due date"
                      help="Left blank on purpose — a template re-runs a job that already ran, so carrying a date over would make the new one overdue the moment it exists, on the dispatch board and in OTD. Set the date this run is actually promised for, or leave it unscheduled."
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
                      data-testid="use-template-error"
                      className="rounded-sm border border-red-500/60 bg-red-500/10 px-4 py-3 text-sm font-semibold text-red-300"
                    >
                      {error}
                    </div>
                  )}
                </>
              )}
            </div>
          )}

          <div className="modal-footer">
            {partialResult ? (
              <>
                {/* Dismissing does NOT navigate: nothing should move the planner
                    out from under a list of omissions they just declined to
                    follow. The work order still exists either way. */}
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
                  {available ? 'Cancel' : 'Close'}
                </Button>
                {available && (
                  <LoadingButton loading={submitting} loadingText="Creating…" onClick={handleSubmit}>
                    Create draft work order
                  </LoadingButton>
                )}
              </>
            )}
          </div>
        </>
      )}
    </Modal>
  );
}
