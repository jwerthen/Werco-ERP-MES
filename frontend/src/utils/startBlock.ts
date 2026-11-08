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

/**
 * WHICH work-order refusal a floor row carries, for choosing the floor's NEXT-STEP
 * advice — never for rewording the refusal itself (that stays the server's
 * `start_blocked_reason`, verbatim):
 *
 *  - `'draft'` — not released (`MSG_WORK_ORDER_NOT_RELEASED`). Neither office surface
 *    offers Complete on a draft (the Work Order page hides it, the operations popup
 *    says "Release this work order before completing operations"), so advice that
 *    ends at "complete it from the work order page" dead-ends: the supervisor
 *    RELEASES first. Whether a draft job may be completed some other way is an open
 *    owner question, so no advice may point at a door the office does not offer.
 *  - `'on_hold'` — held (`MSG_WORK_ORDER_ON_HOLD`). An operation already running is
 *    closable as it stands — at the server and on the Work Order page alike (always-
 *    closable labor) — so the advice is the work order page, never "resume it": a
 *    hold is not lifted just to close running work.
 *  - `'other'` — a refusal this does not recognise (terminal, or a payload without the
 *    status and with unfamiliar text). Callers fall back to generic supervisor advice
 *    that names no button.
 *  - `null` — no refusal (`start_blocked_reason` empty). Never "startable".
 *
 * The row's `work_order_status` decides when present — it is the structured twin of
 * the reason and comes out of the same row builder. Only a payload without it falls
 * back to the reason TEXT, matched loosely on the phrases the two server constants
 * open with (`operation_action_gates.py`), so a rewording of the sentence around them
 * still classifies. One copy of this judgment, so the floor screens cannot drift.
 */
export type StartBlockKind = 'draft' | 'on_hold' | 'other';

const NOT_RELEASED_TEXT = /\b(?:not(?: yet)?(?: been)? released|release first|unreleased)\b/i;
const ON_HOLD_TEXT = /\bon hold\b/i;

export function startBlockKind(
  workOrderStatus: string | null | undefined,
  startBlockedReason: string | null | undefined
): StartBlockKind | null {
  const reason = String(startBlockedReason ?? '').trim();
  if (!reason) return null;
  const status = String(workOrderStatus ?? '')
    .trim()
    .toLowerCase();
  if (status === 'draft') return 'draft';
  if (status === 'on_hold') return 'on_hold';
  // A status the row DID carry (terminal, or anything else) is authoritative: the
  // reason text is consulted only when the payload has no status at all.
  if (status) return 'other';
  // Draft first: the not-released sentence never says "on hold", but test it first
  // anyway so a future sentence that mentions both cannot read as a hold.
  if (NOT_RELEASED_TEXT.test(reason)) return 'draft';
  if (ON_HOLD_TEXT.test(reason)) return 'on_hold';
  return 'other';
}
