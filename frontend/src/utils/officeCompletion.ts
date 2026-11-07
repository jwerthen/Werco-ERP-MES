/**
 * Office completion helpers — owner ruling 1 (2026-11-05), the desk side.
 *
 * A short count at operation completion is reported as scrap with a reason, or the
 * operation stays open. The server is the arbiter (`POST /work-orders/operations/{id}/
 * complete`, `POST /work-orders/{id}/complete`, `POST /work-orders/operations/{id}/
 * reduce-production`); these helpers only let the office screens offer the ONE follow-up
 * each refusal names, instead of leaving the user to retype a request by hand.
 *
 * The refusal texts are built in `backend/app/services/operation_action_gates.py`
 * (`others_clocked_in_message(office=True)`, `reduce_would_leave_short_message`,
 * `override_reason_required_message`, `force_complete_exceeds_recorded_message`) and
 * pinned by the backend's short-count tests. Each detector keys on the request-parameter
 * name the refusal tells the caller to send — the one part of the text that cannot be
 * reworded without breaking every API client. The text itself is always shown verbatim:
 * rewording a gating refusal is a compliance bug.
 */

import type { WorkOrderOperation } from '../types';
import { SHORT_EPSILON, operationUnaccounted } from './shortCount';

function detailText(detail: unknown): string {
  return typeof detail === 'string' ? detail : '';
}

/**
 * The office "still clocked in" 409 on a SHORT close — the caller may resubmit with
 * `acknowledge_open_entries` to clock those operators out (their unreported pieces are
 * not counted).
 */
export function isOpenEntriesRefusal(detail: unknown): boolean {
  return detailText(detail).includes('Resubmit with acknowledge_open_entries');
}

/**
 * The office reduce-production 409 when a correction would leave a COMPLETE operation
 * short — the caller may resubmit with `reopen_operation` (it returns to in progress).
 */
export function isReopenRequiredRefusal(detail: unknown): boolean {
  return detailText(detail).includes('Resubmit with reopen_operation');
}

/**
 * The refusal for booking more good than the floor recorded without saying why: the
 * office operation 422 and the force-complete 409 both name `quantity_override_reason`.
 */
export function isOverrideReasonRefusal(detail: unknown): boolean {
  return detailText(detail).includes('quantity_override_reason');
}

/**
 * Could the floor have clocked labor into this operation?
 *
 * The server requires `quantity_override_reason` to book more good than the floor
 * recorded ONLY on an operation that has any time entry; desk cleanup of never-clocked
 * work stays reason-free (the owner's floor-requires-labor / office-exempt asymmetry).
 * The office read model does not carry the time entries, so this is a deliberately
 * GENEROUS proxy: every clock-in starts the operation (`actual_start`), so an operation
 * that was never started cannot hold labor, and anything started — by a clock-in or by
 * an earlier desk save — is treated as if it might. A false "yes" costs one typed reason;
 * a false "no" is caught by the server's 422, which the modal turns into a required field.
 */
export function operationMayHaveLabor(
  operation: Pick<
    WorkOrderOperation,
    'status' | 'actual_start' | 'started_by' | 'actual_hours' | 'actual_setup_hours' | 'actual_run_hours'
  >
): boolean {
  if (operation.actual_start || operation.started_by != null) return true;
  if (operation.status === 'in_progress' || operation.status === 'complete') return true;
  const hours =
    Number(operation.actual_hours || 0) +
    Number(operation.actual_setup_hours || 0) +
    Number(operation.actual_run_hours || 0);
  return hours > 0;
}

/**
 * The "n unaccounted" figure an office operation row shows (owner ruling 1): received
 * units still neither good nor scrapped — what a Complete must account for. Only an
 * OPEN operation that already has progress shows one (a fresh 0/10 row has nothing to
 * flag; a complete or cancelled-nest row never does), so 0 means "no chip".
 *
 * The server's `quantity_unaccounted` when the payload carries it — it alone knows the
 * chain credit — else the same per-operation arithmetic on the row's own figures.
 */
export function operationUnaccountedChip(
  operation: Pick<
    WorkOrderOperation,
    | 'status'
    | 'cancelled_nest_id'
    | 'quantity_complete'
    | 'quantity_scrapped'
    | 'quantity_unaccounted'
    | 'upstream_quantity_short'
  >,
  target: number
): number {
  if (operation.status === 'complete' || operation.cancelled_nest_id != null) return 0;
  const hasProgress = Number(operation.quantity_complete || 0) > 0 || Number(operation.quantity_scrapped || 0) > 0;
  if (!hasProgress) return 0;
  const served = operation.quantity_unaccounted;
  const figure =
    served != null && Number.isFinite(Number(served))
      ? Math.max(0, Number(served))
      : operationUnaccounted({
          target,
          upstreamShort: operation.upstream_quantity_short,
          complete: operation.quantity_complete,
          scrapped: operation.quantity_scrapped,
        });
  return figure > SHORT_EPSILON ? figure : 0;
}

/** "Dana R.", "Dana R. and Lee K.", "Dana R., Lee K. and Sam P." — unnamed rows skipped. */
export function joinOperatorNames(names: ReadonlyArray<string | null | undefined>): string {
  const present = names.map(name => (name ?? '').trim()).filter(Boolean);
  if (present.length <= 1) return present[0] ?? '';
  return `${present.slice(0, -1).join(', ')} and ${present[present.length - 1]}`;
}
