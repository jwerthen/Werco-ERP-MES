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
 * A DELETED SOURCE JOB DOES NOT MAKE A TEMPLATE UNUSABLE
 * ---------------------------------------------------------------------------
 * It used to: the plan came back `available = false` and this dialog refused it.
 * It no longer does. A soft-deleted work order — the only kind reachable, since
 * `source_work_order_id` is NOT NULL with no `ON DELETE` — keeps every operation,
 * nest and tie it had, so the plan is read straight through it and the copy runs
 * normally. The deletion is disclosed as a muted line above the form, because a
 * planner is entitled to know the job they are copying is in the archive; it is
 * not a warning and it gates nothing.
 *
 * ---------------------------------------------------------------------------
 * AN UNUSABLE TEMPLATE IS SHOWN, NOT HIDDEN
 * ---------------------------------------------------------------------------
 * A source the server genuinely cannot resolve still comes back
 * `available = false`, and `POST .../use` refuses it 409. This dialog refuses
 * first, in words, with no submit control at all. The branch is reachable only
 * from a stale list (the modal reads `template.plan` off the prop rather than
 * re-fetching), which is exactly why it stays: a stale row must not submit a
 * write the server will refuse.
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
 *
 * ---------------------------------------------------------------------------
 * SEVERAL WORK ORDERS, NOT ONE WORK ORDER WITH A BIGGER QUANTITY
 * ---------------------------------------------------------------------------
 * A weld assembly is built ONE UNIT PER WORK ORDER: each unit carries its own
 * Unit #, its own travelers, its own labor and its own quality record. "Five of
 * them" is therefore five work orders, not one with a quantity of five — those
 * are different plans, and only the first can be reported against per unit. So
 * the count field creates N separate DRAFTS, each a full copy of the plan under
 * its own number.
 *
 * Two consequences are wired in rather than left to the planner to remember:
 *
 * * **Raising the count re-prefills the quantity to 1** — but ONLY while the
 *   planner has not touched the quantity themselves. A template saved from a
 *   qty-8 job would otherwise turn "make 5" into 5 × 8 = 40 pieces in silence.
 *   Touch the field and it is yours: the auto-set never overwrites a typed value,
 *   and dropping the count back to 1 restores the template's own prefill.
 * * **The batch line states the OUTCOME before the click.** Auto-setting a field
 *   nobody asked for is only honest if the planner can see what it did, in their
 *   own units — "5 draft work orders, quantity 1 each — 5 pieces in total".
 * * **The cap is refused in the dialog's own words.** `max` on a number input is
 *   not a gate outside a native form submit, and this dialog submits from a click
 *   handler — so without a pre-submit check a planner who types 25 earns a raw
 *   pydantic sentence naming a request field, instead of one that says what the
 *   limit is and what to do about it.
 *
 * ---------------------------------------------------------------------------
 * A NEST-BEARING TEMPLATE IS ONE AT A TIME
 * ---------------------------------------------------------------------------
 * The count field is DISABLED for exactly the reason the quantity field is: a
 * laser job's quantity IS the sum of its nests' planned runs, so "five of it" is
 * the wrong shape — running more means more runs on the nests, not five work
 * orders each claiming the same sheets. The server refuses `count > 1` there
 * with a 409 before it mutates anything; the field carries the reason so nobody
 * has to earn that refusal to learn it.
 *
 * ---------------------------------------------------------------------------
 * UNIT NUMBERS ARE A LIST THE PLANNER PASTES, NOT A PATTERN WE GUESS
 * ---------------------------------------------------------------------------
 * There is deliberately NO generator, NO auto-increment and NO fill-down. Unit
 * numbers are not a trailing digit that steps by one — they come off the
 * customer's own scheme — and a control that invented them would mint a
 * plausible WRONG number onto a physical build, which is the single failure a
 * Unit # exists to prevent. So: one per line, in order, pasted straight out of
 * the spreadsheet column the planner already has. A blank line is legal and
 * means "no unit yet"; it is sent as `null`, because NULL and "" are not the
 * same claim about a build.
 *
 * Both pre-submit gates on the LIST mirror the server's own 422s (the third is
 * the count cap above), and exist so a mis-pasted column costs no round trip: the list must be the same length as the
 * count, and no non-blank value may repeat. NEITHER is checked against existing
 * work orders — `work_orders.unit_number` carries no unique constraint, because
 * a rework work order legitimately re-uses the unit it is reworking.
 *
 * ---------------------------------------------------------------------------
 * A BATCH HAS NOWHERE SINGLE TO NAVIGATE, SO IT DOES NOT
 * ---------------------------------------------------------------------------
 * One copy hands off and the caller lands on the new draft. Five copies have no
 * single destination, and the planner's next physical act is writing work order
 * numbers onto travelers — so a clean batch STAYS in the dialog and renders the
 * number → Unit # table, with a copy-to-clipboard for whatever the shop tracks
 * units in. "Done" hands the envelope over and closes.
 *
 * A PARTIAL batch keeps the partial behaviour above unchanged and adds one line
 * saying the omissions apply to EVERY draft: the skip lists are the union across
 * the copies (they ran one plan, so an omission is a property of the plan), and
 * read without that line they describe only the one work order the report names.
 * Its primary control is "Done" rather than "Go to WO-…", because for a batch
 * the hand-off is a list refresh and there is no one copy to go to — a button
 * promising to go somewhere that then does not is worse than no button.
 */

import React, { useEffect, useId, useMemo, useRef, useState } from 'react';
import api from '../../services/api';
import { Button, FormField, LoadingButton, Modal, useToast } from '../ui';
import { CopyPlanSkipReport, hasSkips, serverErrorDetail, storedQuantityNote } from './copyPlanSkips';
import type {
  WorkOrder,
  WorkOrderTemplate,
  WorkOrderTemplateUsePayload,
  WorkOrderTemplateUseResult,
} from '../../types';

/**
 * Machine-readable `plan.unavailable_reason` → the sentence a planner reads.
 *
 * The server owns this vocabulary and says to treat the set as OPEN, so an
 * unrecognized token is shown verbatim inside a generic sentence rather than
 * being dropped or guessed at — the same rule the copy skip reasons follow.
 */
const TEMPLATE_UNAVAILABLE_REASONS: Record<string, string> = {
  // The only reason the CURRENT server sends. It does not mean "deleted" — a deleted
  // source is read straight through and reported as `plan.source_work_order_deleted`
  // — it means the source row could not be resolved at all, which the NOT NULL FK on
  // `work_order_templates.source_work_order_id` makes near-unreachable. So the sentence
  // deliberately does not name restoring a work order as the remedy: there is no
  // tombstone to restore, and sending someone to the Deleted tab to look for one is
  // worse than admitting the template needs re-saving.
  source_work_order_missing:
    'The work order this template was saved from can no longer be found, so there is no plan to ' +
    'copy. Save a new template from a current job.',
  // Retained for a PRE-CHANGE server, or a list cached from one. That server DID refuse
  // a deleted source; this one never does. Kept because the sentence has to be true of
  // the server that sent the token, not of the one we happen to be running.
  source_work_order_deleted:
    'The work order this template was saved from has been deleted, and this copy was refused. ' +
    'Restoring that work order clears it.',
};

/** The full sentence for an unusable template, never an empty string. */
export function templateUnavailableSentence(reason?: string | null): string {
  const token = (reason ?? '').trim();
  if (!token) return 'This template cannot be used right now.';
  return TEMPLATE_UNAVAILABLE_REASONS[token] ?? `This template cannot be used right now (${token}).`;
}

/**
 * The most work orders one click may create. Matches the server's own cap,
 * which refuses 422 above it: every copy writes `2 + nests + ties` audit rows
 * under a GLOBAL, cross-tenant advisory lock, so a batch is a cost the other
 * tenants pay too. Duplicated here rather than fetched — a hard number in a
 * `max` attribute cannot come from a response the form does not make.
 */
export const MAX_TEMPLATE_USE_COUNT = 20;

/**
 * The refusal for a count above the cap, or `null` when it can be sent.
 *
 * The third of the server's three 422s, and the one the form does NOT already
 * stop: `max` on a `type="number"` input is enforced only by a native form
 * submit, and this dialog submits from a click handler, so 25 can be typed and
 * sent. Left to the server it comes back as a raw pydantic sentence about a
 * request field ("Input should be less than or equal to 20") — true, but it
 * names neither the limit's reason nor the way out of it.
 */
export function templateUseCountIssue(count: number): string | null {
  if (count <= MAX_TEMPLATE_USE_COUNT) return null;
  return (
    `You asked for ${count} work orders, and ${MAX_TEMPLATE_USE_COUNT} is the most one click may create. ` +
    'Run the template again for the rest.'
  );
}

/**
 * The pasted unit-number column → one trimmed entry per line, in order.
 *
 * Exactly ONE trailing newline is dropped, and that is the whole subtlety here.
 * A column pasted out of a spreadsheet ends with a newline, and the empty string
 * after it is an artifact of the paste, not a sixth work order with no unit —
 * counting it would refuse a correctly pasted list of five. Dropping only one
 * keeps a genuinely blank LAST entry expressible: leave the line blank and press
 * Enter again.
 *
 * A wholly blank box is NO list at all (`[]`), which is what "leave the box empty
 * to add them later" has to mean — not N blank units.
 */
export function parseUnitNumberLines(text: string): string[] {
  if (text.trim() === '') return [];
  const normalized = text.replace(/\r\n?/g, '\n');
  const body = normalized.endsWith('\n') ? normalized.slice(0, -1) : normalized;
  return body.split('\n').map((line) => line.trim());
}

/**
 * The refusal for a unit-number list that cannot be sent, or `null` when it can.
 *
 * These mirror the server's two 422s so a mis-pasted column is caught before a
 * round trip — the server stays the enforcement, this is only the fast path.
 * Duplicates are compared case-insensitively and on the trimmed value, because
 * `2410048 ` and `2410048` name the same physical build; blanks never collide,
 * since two work orders with no unit yet is an ordinary state.
 */
export function unitNumberListIssue(lines: string[], count: number): string | null {
  if (lines.length !== count) {
    return (
      `You listed ${lines.length} unit number${lines.length === 1 ? '' : 's'} for ${count} work orders. ` +
      'Make the two match, or clear the unit numbers and add them later.'
    );
  }
  const seen = new Set<string>();
  for (const line of lines) {
    if (line === '') continue;
    const key = line.toLowerCase();
    if (seen.has(key)) {
      return (
        `Two work orders would carry the same unit number ("${line}"). ` +
        'A unit number identifies one physical build — fix the list.'
      );
    }
    seen.add(key);
  }
  return null;
}

/**
 * Every work order the server says it created, in creation order.
 *
 * `work_orders[0]` IS `work_order`, so the fallback is not a second shape — it
 * is what a truncated or hand-built envelope degrades to, and one work order is
 * the honest reading of an envelope that names exactly one.
 */
export function createdWorkOrders(result: WorkOrderTemplateUseResult): WorkOrder[] {
  return result.work_orders?.length ? result.work_orders : [result.work_order];
}

export interface UseTemplateModalProps {
  open: boolean;
  /** `null` while closed; the template being run. */
  template: WorkOrderTemplate | null;
  onClose: () => void;
  /**
   * The caller's hand-off, fired only after the server has created the work
   * order(s), with the WHOLE envelope.
   *
   * For ONE copy it navigates to the new draft, and on a partial copy it fires
   * only once the planner has read the result view and chosen to go there —
   * dismissing does not fire it, because nothing should navigate out from under
   * a list of omissions the planner just declined to follow.
   *
   * For a BATCH there is no single destination, so the caller refreshes its list
   * instead. That makes the hand-off harmless, so it fires on ANY close of a
   * batch result view: the drafts exist either way, and a caller that never
   * hears about them shows a stale list.
   */
  onUsed: (result: WorkOrderTemplateUseResult) => void;
}

export default function UseTemplateModal({ open, template, onClose, onUsed }: UseTemplateModalProps) {
  const { showToast } = useToast();
  const titleId = useId();
  const [quantity, setQuantity] = useState('');
  const [dueDate, setDueDate] = useState('');
  const [count, setCount] = useState('1');
  const [unitNumbersText, setUnitNumbersText] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  /**
   * Set ONLY for a partial copy — the envelope the result view renders. A clean
   * SINGLE copy never lands here: it toasts, hands over, and closes.
   */
  const [partialResult, setPartialResult] = useState<WorkOrderTemplateUseResult | null>(null);
  /**
   * Set ONLY for a clean BATCH — the number → Unit # table. It is the one place
   * that mapping is ever shown together, and the planner is about to write it on
   * paper, so it stays up instead of self-dismissing behind a route change.
   */
  const [batchResult, setBatchResult] = useState<WorkOrderTemplateUseResult | null>(null);
  const resultPrimaryRef = useRef<HTMLButtonElement | null>(null);
  /**
   * Has the planner typed in the quantity field? Raising the count re-prefills
   * that field, and this is what keeps the auto-set from ever overwriting a
   * number somebody chose. A ref, not state: nothing renders from it.
   */
  const quantityTouchedRef = useRef(false);

  const templateId = template?.id ?? null;
  const plan = template?.plan;
  const available = plan?.available !== false;
  /**
   * Nest-bearing: the copied nests decide BOTH the quantity and how many work
   * orders this can be. One flag, because they are one rule — the quantity is
   * the sum of the nests' planned runs, which is also why "five of it" means
   * five times the runs rather than five work orders.
   */
  const nestBearing = (plan?.nest_count ?? 0) > 0;
  const quantityLocked = nestBearing;
  // The prefill the planner saved, falling back to what the source job ran.
  const prefillQuantity = template?.default_quantity ?? plan?.source_quantity_ordered ?? null;
  const prefillQuantityText = prefillQuantity != null ? String(prefillQuantity) : '';

  /**
   * How many drafts the form is asking for. An empty or unreadable box reads as
   * ONE — the field starts at 1 and a half-typed value must not make the dialog
   * claim a batch it is not about to create.
   */
  const requestedCount = useMemo(() => {
    const parsed = Number.parseInt(count, 10);
    return Number.isFinite(parsed) && parsed >= 1 ? parsed : 1;
  }, [count]);
  const isBatch = requestedCount > 1 && !nestBearing;

  const unitNumberLines = useMemo(() => parseUnitNumberLines(unitNumbersText), [unitNumbersText]);
  const filledUnitNumbers = unitNumberLines.filter((line) => line !== '').length;

  // Reset on every open. Quantity prefills; the due date stays blank on purpose.
  useEffect(() => {
    if (!open || templateId == null) return;
    setQuantity(prefillQuantityText);
    setDueDate('');
    setCount('1');
    setUnitNumbersText('');
    quantityTouchedRef.current = false;
    setError('');
    setPartialResult(null);
    setBatchResult(null);
  }, [open, templateId, prefillQuantityText]);

  // Move focus onto the result view's primary control when it replaces the form:
  // the button that had focus just unmounted, and a keyboard user must not be
  // left on <body> with a dialog full of unread omissions in front of them.
  useEffect(() => {
    if (partialResult || batchResult) resultPrimaryRef.current?.focus();
  }, [partialResult, batchResult]);

  /**
   * Raising the count past one re-prefills the quantity to 1, and dropping it
   * back restores the template's own prefill — but only while the planner has
   * not typed in that field themselves.
   *
   * Without this, a template saved from a qty-8 job turns "make 5" into 5 × 8 =
   * 40 pieces across five work orders, silently. The batch line below the grid
   * is the other half: it states the outcome in pieces before the click, so an
   * auto-set the planner did not ask for is never invisible.
   */
  const handleCountChange = (raw: string) => {
    setCount(raw);
    if (quantityTouchedRef.current) return;
    const parsed = Number.parseInt(raw, 10);
    setQuantity(Number.isFinite(parsed) && parsed > 1 ? '1' : prefillQuantityText);
  };

  const handleQuantityChange = (raw: string) => {
    quantityTouchedRef.current = true;
    setQuantity(raw);
  };

  /** The envelope currently on screen, whichever result view is up. */
  const activeResult = partialResult ?? batchResult;
  const activeCreated = activeResult ? createdWorkOrders(activeResult) : [];
  const activeIsBatch = activeCreated.length > 1;

  const close = () => {
    // Never dismiss mid-request: the work order may already exist server-side,
    // and this dialog must reflect only what the server actually did.
    if (submitting) return;
    // A batch hand-off is a LIST REFRESH, not a navigation, so dismissing a
    // batch result still fires it — the drafts exist either way and the caller's
    // list is stale until it hears. The single-copy rule is untouched: nothing
    // navigates out from under omissions the planner just declined to follow.
    if (activeResult && activeIsBatch) onUsed(activeResult);
    onClose();
  };

  /** The result view's primary action: hand the envelope over, then close. */
  const finishResult = () => {
    if (!activeResult) return;
    onUsed(activeResult);
    onClose();
  };

  /**
   * The number → Unit # mapping as tab-separated lines, ready to paste back into
   * whatever the shop tracks units in. A no-op where the clipboard API is absent
   * or refused (an insecure origin, a locked-down browser): the table is on
   * screen either way, so this is a convenience and never the only copy.
   */
  const copyBatchList = async () => {
    if (!batchResult) return;
    const lines = createdWorkOrders(batchResult)
      .map((workOrder) => `${workOrder.work_order_number}\t${workOrder.unit_number ?? ''}`)
      .join('\n');
    const unavailable = 'This browser would not let the page copy. The list is on screen — select it by hand.';
    // Checked, not optional-chained: `await undefined` resolves, so chaining
    // would report a successful copy on a browser that has no clipboard at all.
    if (!navigator.clipboard?.writeText) {
      showToast('info', unavailable);
      return;
    }
    try {
      await navigator.clipboard.writeText(lines);
      showToast('success', 'Work order numbers and unit numbers copied.');
    } catch {
      showToast('info', unavailable);
    }
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

    if (isBatch) {
      // Before the list gates, because a count over the cap makes the length
      // check meaningless: a correctly pasted column of 25 would clear it and
      // still be refused by the server.
      const countIssue = templateUseCountIssue(requestedCount);
      if (countIssue) {
        setError(countIssue);
        return;
      }
      payload.count = requestedCount;
      // Only when the box has something in it: an empty list is "add the units
      // later", which is a legal way to run a batch, not an incomplete form. The
      // gates run only in this branch too — the textarea is not on screen for a
      // single use, and refusing a submit over a field nobody can see is worse
      // than dropping a stale paste the planner cannot read back.
      if (unitNumberLines.length > 0) {
        const issue = unitNumberListIssue(unitNumberLines, requestedCount);
        if (issue) {
          setError(issue);
          return;
        }
        // Blank → null, never "": the column is nullable, and NULL is the shape
        // that means "no unit yet" rather than "the unit is the empty string".
        payload.unit_numbers = unitNumberLines.map((line) => (line === '' ? null : line));
      }
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

      const created = createdWorkOrders(result);
      // A clean BATCH stops here too, for a different reason: there is no single
      // work order to hand off to, and the number → Unit # mapping the planner
      // is about to write on travelers exists nowhere else in the app.
      if (created.length > 1) {
        setBatchResult(result);
        return;
      }

      // Quantity comes off the RESPONSE, never off the form: on a nest-bearing
      // template the server stores the derived sum, not what was submitted.
      showToast(
        'success',
        `${created[0].work_order_number} created as a draft${storedQuantityNote(result)}, ` +
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

  /**
   * The outcome of the current form, in the planner's own units, stated before
   * the click. This is the CHECK on the quantity auto-set above: a number the
   * form changed on its own is only acceptable if its consequence is on screen.
   *
   * When the quantity is blank the server resolves it (template default, then
   * the source work order), so the sentence says that rather than multiplying by
   * a number nobody has.
   */
  const quantityNumber = Number(quantity);
  const quantityKnown =
    !quantityLocked && quantity.trim() !== '' && Number.isFinite(quantityNumber) && quantityNumber > 0;
  const batchTotalSentence = quantityKnown
    ? `${requestedCount} draft work orders, quantity ${quantityNumber} each — ` +
      `${requestedCount * quantityNumber} pieces in total, one work order number per unit.`
    : `${requestedCount} draft work orders, one work order number per unit — ` +
      'the quantity of each comes from the saved template.';

  const firstCreated = activeCreated[0];
  const lastCreated = activeCreated[activeCreated.length - 1];

  return (
    <Modal
      open={open && template !== null}
      onClose={close}
      // Wider than the `lg` this shipped at, because the dialog gained a third
      // number field and a result TABLE: at `lg` the three labels wrap and the
      // work order → Unit # rows crush together, which is the one view a planner
      // reads off the screen while writing on paper.
      size="2xl"
      padded={false}
      scroll={false}
      // Escape still dismisses a result view — that is a deliberate keypress. A
      // stray backdrop click is not, and it must not be what makes the only
      // record of an un-copied material tie, or the only rendering of the
      // number → Unit # mapping, disappear.
      closeOnBackdrop={!submitting && activeResult === null}
      closeOnEscape={!submitting}
      ariaLabelledBy={titleId}
    >
      {template && (
        <>
          <div className="modal-header">
            <h3 id={titleId} className="text-lg font-semibold">
              {/* Not "Use template" any more once the work orders exist — the
                  heading names the result so this reads as an outcome, not as a
                  form that failed to submit. A batch is named by its COUNT: one
                  of several numbers in the heading would claim the news below
                  belongs to that one copy. */}
              {partialResult
                ? activeIsBatch
                  ? `Created with omissions — ${activeCreated.length} work orders`
                  : `Created with omissions — ${partialResult.work_order.work_order_number}`
                : batchResult
                  ? `${activeCreated.length} draft work orders created`
                  : `Use template — ${template.name}`}
            </h3>
          </div>

          {partialResult ? (
            <div className="modal-body max-h-[70vh] space-y-4 overflow-y-auto">
              {/* The skip lists are the UNION across the copies — they ran one
                  plan, so an omission belongs to the plan and lands on all of
                  them. Read without this line they describe only the single work
                  order the report happens to name. */}
              {activeIsBatch && firstCreated && lastCreated ? (
                <p data-testid="use-template-batch-skip-scope" className="text-sm text-slate-300">
                  All {activeCreated.length} drafts were created (
                  <span className="font-mono">{firstCreated.work_order_number}</span> through{' '}
                  <span className="font-mono">{lastCreated.work_order_number}</span>). The omissions below apply to
                  every one of them.
                </p>
              ) : null}

              <CopyPlanSkipReport
                result={partialResult}
                origin={
                  <>
                    from template <span className="font-mono">{template.name}</span>.
                  </>
                }
              />
            </div>
          ) : batchResult ? (
            <div className="modal-body max-h-[70vh] space-y-4 overflow-y-auto">
              {/* Quantity off the RESPONSE, never off the form — the same rule
                  the single-copy toast follows. */}
              <p className="text-sm text-slate-300">
                <span className="font-semibold text-slate-100">{activeCreated.length} draft work orders</span> created
                {storedQuantityNote(batchResult)} each, from template{' '}
                <span className="font-mono">{template.name}</span>. Nothing reaches the dispatch board or the kiosk
                until somebody releases them.
              </p>

              <div className="overflow-x-auto border border-fd-line">
                <table data-testid="use-template-batch-table" className="min-w-full text-sm">
                  <thead>
                    <tr className="border-b border-fd-line bg-fd-sunken/40 text-left">
                      <th scope="col" className="px-3 py-2 text-xs font-semibold uppercase tracking-wide text-slate-400">
                        Work order
                      </th>
                      <th scope="col" className="px-3 py-2 text-xs font-semibold uppercase tracking-wide text-slate-400">
                        Unit #
                      </th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-fd-line">
                    {activeCreated.map((workOrder) => (
                      <tr key={workOrder.id}>
                        <td className="px-3 py-2 font-mono text-slate-200">{workOrder.work_order_number}</td>
                        <td className="px-3 py-2 font-mono text-slate-200">
                          {workOrder.unit_number?.trim() ? (
                            workOrder.unit_number
                          ) : (
                            // Not blank: a planner scanning this column has to be
                            // able to tell "no unit yet" from a row that failed
                            // to render one.
                            <span className="font-sans text-xs text-slate-500">No unit yet</span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              <p className="text-xs text-slate-400">
                Write these onto the travelers before the drafts are released — the work order number is what the
                floor books labor against, and the unit number is what the customer asks about.
              </p>
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

                  {/* Muted, never an alert: the source job being in the archive
                      changes nothing about this copy — a soft-deleted work order
                      keeps its whole plan — but the planner should not learn it
                      afterwards from a work order number that leads nowhere. */}
                  {plan?.source_work_order_deleted === true ? (
                    <p data-testid="use-template-source-deleted" className="text-xs text-slate-400">
                      That work order has been deleted. Its plan is still intact and copies exactly as it
                      stands; only the job itself is in the archive.
                    </p>
                  ) : null}

                  {template.notes?.trim() ? (
                    <p data-testid="use-template-notes" className="text-sm text-surface-500">
                      <span className="font-semibold text-surface-700">Note</span> — {template.notes.trim()}
                    </p>
                  ) : null}

                  <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
                    <FormField
                      label="Work orders to create"
                      help={
                        nestBearing
                          ? // Disabled with the reason on it, exactly like the
                            // quantity field beside it: the server refuses
                            // count > 1 on a nest-bearing template with a 409,
                            // and nobody should have to earn that refusal.
                            'One at a time. This job’s quantity is the sum of its sheet runs, so more of it means more runs on the nests, not more work orders.'
                          : 'Each one gets its own work order number and its own plan. Use this for serialized units — one unit per work order — rather than one work order with a quantity of 5.'
                      }
                    >
                      {(field) => (
                        <input
                          {...field}
                          type="number"
                          inputMode="numeric"
                          min={1}
                          max={MAX_TEMPLATE_USE_COUNT}
                          step={1}
                          className="input"
                          disabled={submitting || nestBearing}
                          value={nestBearing ? '1' : count}
                          onChange={(e) => handleCountChange(e.target.value)}
                        />
                      )}
                    </FormField>

                    <FormField
                      // Named for what it now is: with a count above one this is
                      // the size of EACH work order, not a total to divide.
                      label={isBatch ? 'Quantity per work order' : 'Quantity'}
                      help={
                        quantityLocked
                          ? 'Derived, not typed: this job’s quantity is the sum of its nests’ sheet runs, and the copy carries those runs across unchanged. Add or remove nests on the new draft to change it.'
                          : isBatch
                            ? 'How many pieces EACH of these work orders builds — one, for a serialized unit. Not a total to split between them.'
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
                          onChange={(e) => handleQuantityChange(e.target.value)}
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

                  {/* The check on the quantity auto-set: the outcome in pieces,
                      in the planner's own units, before they click. */}
                  {isBatch ? (
                    <p
                      data-testid="use-template-batch-total"
                      className="rounded-sm border border-fd-line bg-fd-sunken/40 px-3 py-2 text-sm text-slate-300"
                    >
                      {batchTotalSentence}
                    </p>
                  ) : null}

                  {/* One per line, pasted. No generator, no auto-increment, no
                      fill-down: unit numbers come off the customer's scheme, and
                      a control that invented them would mint a plausible WRONG
                      number onto a physical build. */}
                  {isBatch ? (
                    <FormField
                      label="Unit numbers (optional)"
                      help={
                        <>
                          One per line, in order — paste a column straight from a spreadsheet. Leave a line blank for
                          a work order that has no unit yet, or leave the box empty to add them later. Up to 50
                          characters each.
                          <span data-testid="use-template-unit-count" className="mt-1 block text-slate-300">
                            {filledUnitNumbers} of {requestedCount} unit numbers entered
                          </span>
                        </>
                      }
                    >
                      {(field) => (
                        <textarea
                          {...field}
                          // Tall enough to show the whole list up to a point;
                          // past that it scrolls rather than pushing the submit
                          // control off a laptop screen.
                          rows={Math.min(requestedCount, 8)}
                          className="input min-h-0 font-mono text-sm"
                          disabled={submitting}
                          value={unitNumbersText}
                          onChange={(e) => setUnitNumbersText(e.target.value)}
                        />
                      )}
                    </FormField>
                  ) : null}

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
                {/* A batch has no one copy to go to, and a button that promises
                    a destination and then delivers none is worse than no button.
                    It hands off (a list refresh) and closes instead. */}
                <Button ref={resultPrimaryRef} onClick={finishResult}>
                  {activeIsBatch ? 'Done' : `Go to ${partialResult.work_order.work_order_number}`}
                </Button>
              </>
            ) : batchResult ? (
              <>
                <Button variant="secondary" onClick={copyBatchList}>
                  Copy list
                </Button>
                <Button ref={resultPrimaryRef} onClick={finishResult}>
                  Done
                </Button>
              </>
            ) : (
              <>
                <Button variant="secondary" onClick={close} disabled={submitting}>
                  {available ? 'Cancel' : 'Close'}
                </Button>
                {available && (
                  <LoadingButton loading={submitting} loadingText="Creating…" onClick={handleSubmit}>
                    {isBatch ? `Create ${requestedCount} draft work orders` : 'Create draft work order'}
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
