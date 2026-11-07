/**
 * The short button label for a floor action the WORK ORDER refuses.
 *
 * Owner ruling 2026-11-05: the floor cannot start work on a job a supervisor has
 * not released (DRAFT) or has put on hold (ON_HOLD). Every floor read payload
 * carries the server's refusal text as `start_blocked_reason` and the work
 * order's `work_order_status`; every start verb refuses 409 with that same text.
 *
 * This helper picks ONLY the two-word label that fits on a button
 * ("Not released", "WO on hold"). The explanation shown in a tooltip, reason
 * line or toast is ALWAYS the server's `start_blocked_reason`, verbatim — never
 * re-derived here. Rewording a gating refusal is a compliance bug: the operator
 * must read exactly why the system refused.
 *
 * Callers render this only when `start_blocked_reason` is set. A null reason
 * means "no work-order refusal", never "startable" — whether a job may actually
 * start is decided by the server at the moment of the action, so nothing here
 * enables a button.
 */
export function startBlockedLabel(workOrderStatus?: string | null): string {
  const status = String(workOrderStatus ?? '').toLowerCase();
  if (status === 'draft') return 'Not released';
  if (status === 'on_hold') return 'WO on hold';
  // Terminal (complete / closed / cancelled), or a payload without the status:
  // the reason text still says which, so the label stays neutral.
  return 'Unavailable';
}
