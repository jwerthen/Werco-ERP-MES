/**
 * The copy-plan skip report — shared by every dialog that copies a work order's plan.
 *
 * `POST /work-orders/{id}/duplicate` and `POST /work-order-templates/{id}/use` return
 * the SAME envelope (`WorkOrderDuplicateResponse`), and they return it for the same
 * reason: the two skip lists say what the server could NOT carry across. A skipped
 * material tie means the new job has no demand for that material — no shortage is
 * raised, the nests run, and stock is never deducted. That omission reaches the audit
 * chain either way; this is what puts it in front of the person who pressed the
 * button, while they can still act on it.
 *
 * It lives here, in one module, because two renderings of that news would drift: the
 * reason vocabulary would grow on one side only, and the second dialog would quietly
 * become the one that counts skips without naming them. The Duplicate dialog and the
 * Use-template dialog render the identical view, down to the `data-testid`s, which is
 * also what lets one suite's assertions describe both.
 *
 * WHAT IS DELIBERATE IN HERE
 * --------------------------
 * * **Every reason falls back to the server's own token.** The server owns this
 *   vocabulary and can add to it. `skipSummary` used to state "(its laser nest was
 *   deleted)" for ANY skipped operation — true only while that is the only reason
 *   there is, and a confident wrong explanation sends a planner to check the nests
 *   when the cause was something else. So an unknown reason renders as itself.
 * * **The summary states a shared reason only when there IS one.** Mixed reasons get
 *   a bare count; three different tie reasons cannot honestly collapse into one
 *   parenthetical, and each row carries its own reason in the itemized list.
 * * **The report is amber, not red.** The work order EXISTS and is a valid draft.
 *   Rendering it as a failure sends someone hunting for a job that is already there.
 */

import React from 'react';
import { toDisplayString } from '../../utils/apiError';
import { formatOperationLabel, hasOperationNumber } from '../../utils/operationLabel';
import type {
  WorkOrderDuplicateResult,
  WorkOrderDuplicateSkippedAllocation,
  WorkOrderDuplicateSkippedOperation,
} from '../../types';

/**
 * Machine-readable operation-skip reason → the phrase shown to a planner.
 *
 * Today the only reason the server emits is `laser_nest_deleted`. Treat the set as
 * open; every lookup falls back to the raw token rather than asserting a reason.
 */
export const OPERATION_SKIP_REASONS: Record<string, string> = {
  laser_nest_deleted: 'its laser nest was deleted',
};

export const ALLOCATION_SKIP_REASONS: Record<string, string> = {
  part_not_available: 'the tied part is no longer available',
  // The whole argument for SKIPPING this row rather than refusing the copy
  // outright is that the planner is told legibly which tie to re-make by hand.
  // Falling through to the raw `part_not_tieable` token would spend the refusal
  // and deliver none of the explanation that justified it.
  part_not_tieable: 'the tied part is one the shop produces, not stock material',
  operation_not_copied: 'its operation was not copied',
  nest_runs_unavailable: 'no nest run count to plan against',
};

/** The phrase for one reason, falling back to the server's own token. */
export function reasonLabel(reason: string, labels: Record<string, string>): string {
  return labels[reason] ?? reason;
}

/**
 * A parenthetical naming the reason — but ONLY when every entry shares one, and
 * only when we have a phrase for it. Mixed reasons get no parenthetical rather
 * than one that describes a subset.
 */
export function sharedReasonNote(entries: Array<{ reason: string }>, labels: Record<string, string>): string {
  const distinct = Array.from(new Set(entries.map((entry) => entry.reason)));
  if (distinct.length !== 1) return '';
  const label = labels[distinct[0]];
  return label ? ` (${label})` : '';
}

/** Did the server leave anything behind? Both lists empty is the "clean copy" signal. */
export function hasSkips(result: WorkOrderDuplicateResult): boolean {
  return (result.skipped_operations?.length ?? 0) > 0 || (result.skipped_material_allocations?.length ?? 0) > 0;
}

/**
 * A sentence for what the server refused to carry across, or `null` when the
 * copy was clean.
 *
 * This is not decoration. A skipped material tie means the new job carries NO
 * demand for that material: no shortage is raised, the nests run, and stock is
 * never deducted.
 *
 * Counts only. Ties carry a per-row reason in the itemized list below it, where
 * a mixed set can be shown honestly; collapsing three different tie reasons into
 * one parenthetical here could not be.
 */
export function skipSummary(result: WorkOrderDuplicateResult): string | null {
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
export function operationLabel(operation: WorkOrderDuplicateSkippedOperation): string {
  // The FULL label, matching the `Seq 20` / `Operation #72` fallbacks below --
  // these three are one vocabulary, and a bare "20" among them names nothing.
  //
  // `formatOperationLabel` and not the bare text, even though this notice is
  // emitted ONLY for nest-backed operations (`laser_nest_deleted`), whose stored
  // number is `Nest 3`: the helper leaves a self-labeled value alone, so a nest
  // reads `Nest 3` and a legacy office row still reads `Op 20`. Both spellings
  // reach here -- a nest task can carry either -- and only the label names both.
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
export function allocationLabel(allocation: WorkOrderDuplicateSkippedAllocation): string {
  return allocation.part_id != null ? `Part #${allocation.part_id}` : `Tie #${allocation.source_allocation_id}`;
}

/**
 * ` — qty 18` for the quantity the server actually STORED, or `''` when it sent
 * something unreadable.
 *
 * Always off the RESPONSE, never off the form: on a nest-bearing work order the
 * stored quantity is the sum of the copied nests' planned runs, not the number
 * that was submitted, so quoting the typed value shows a planner a quantity the
 * server did not keep.
 */
export function storedQuantityNote(result: WorkOrderDuplicateResult): string {
  const stored = Number(result.work_order.quantity_ordered);
  return Number.isFinite(stored) ? ` — qty ${stored}` : '';
}

/**
 * Pull a displayable `detail` off any error shape, incl. a structured 409 body.
 *
 * Shared by all three plan-copy dialogs: each renders the server's refusal
 * VERBATIM (they are server-gated writes, so the refusal is the primary display),
 * and a per-dialog copy of this is how one of them ends up showing "[object
 * Object]" for the structured `PROCESS_SHEET_UNAVAILABLE` 409.
 */
export function serverErrorDetail(err: unknown, fallback: string): string {
  const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  const rendered = toDisplayString(detail);
  if (rendered.trim()) return rendered;
  const message = (err as { message?: unknown })?.message;
  if (typeof message === 'string' && message.trim()) return message;
  return fallback;
}

export interface CopyPlanSkipReportProps {
  /** The envelope the server returned. Rendered only when it carries skips. */
  result: WorkOrderDuplicateResult;
  /**
   * The trailing clause of the headline sentence, naming WHERE the copy came
   * from — "copied from WO-1234." for a duplicate, "from the template …" for a
   * template run. A node rather than a string so the caller keeps its own
   * `font-mono` treatment of identifiers.
   */
  origin: React.ReactNode;
}

/**
 * The result view for a PARTIAL copy: the amber headline, then one itemized
 * section per skip list.
 *
 * Renders as a fragment, so the caller's own scroll container and vertical
 * rhythm (`modal-body … space-y-4`) apply unchanged.
 */
export function CopyPlanSkipReport({ result, origin }: CopyPlanSkipReportProps) {
  const skippedOperations = result.skipped_operations ?? [];
  const skippedTies = result.skipped_material_allocations ?? [];

  return (
    <>
      {/* Amber, not red: the work order WAS created and is a valid draft.
          Calling this a failure sends someone hunting for a job that is already
          there. */}
      <div
        role="status"
        data-testid="duplicate-wo-skips"
        className="rounded-sm border border-amber-500/50 bg-amber-500/5 px-4 py-3 text-sm text-slate-300"
      >
        <p className="font-semibold text-amber-200">
          <span className="font-mono">{result.work_order.work_order_number}</span> created as a draft
          {storedQuantityNote(result)}, {origin}
        </p>
        <p className="mt-1">{skipSummary(result)}</p>
      </div>

      {skippedOperations.length > 0 && (
        <section>
          <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-400">Operations not copied</h4>
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
          <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-400">Material ties not copied</h4>
          <ul data-testid="duplicate-wo-skipped-ties" className="mt-2 divide-y divide-fd-line border border-fd-line">
            {skippedTies.map((tie) => (
              <li
                key={tie.source_allocation_id}
                className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1 px-3 py-2"
              >
                <span className="font-mono text-sm text-slate-200">{allocationLabel(tie)}</span>
                <span className="text-xs text-slate-400">{reasonLabel(tie.reason, ALLOCATION_SKIP_REASONS)}</span>
              </li>
            ))}
          </ul>
          <p className="mt-2 text-xs text-slate-400">
            Nothing on the new draft records these. Re-tie the material by hand before releasing it: a job with no
            tie carries no demand, so no shortage shows, the nests run, and stock is never deducted.
          </p>
        </section>
      )}
    </>
  );
}
