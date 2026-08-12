/**
 * Held-operation vocabulary shared by both kiosks (operator + crew station).
 *
 * The defect this exists for: an operator mis-taps HOLD and the operation
 * vanishes from every screen the kiosk offers, because the queue surfaces only
 * READY/IN_PROGRESS work. Recovery needed a desktop, where `resumeOperation` had
 * its one and only call site. A held job now stays on the board and carries its
 * own Resume control.
 *
 * Three rules here are correctness, not copy:
 *
 * 1. **Held work arrives on its own list.** `GET /shop-floor/work-center-queue`
 *    returns `held` beside `queue`, and `queue` stays byte-identical to what it
 *    always carried. The LIST BOUNDARY is the safety property — not a status
 *    flag inside the rows — so nothing here re-merges them, and no client
 *    iterating `queue` can render a held operation as startable by accident.
 *
 * 2. **A held operation is never startable.** It renders as its own card with
 *    exactly one verb (Resume) — not a disabled queue card, and never a tap
 *    target that clocks somebody in. The server refuses a clock-in on a held op
 *    anyway; the point is that the operator sees WHY instead of a refusal toast.
 *
 * 3. **Resume does not resolve the blocker, and the UI must say so.** The
 *    backend lifts the operation status and returns whatever blockers are still
 *    open, deliberately leaving the two decoupled. If the kiosk swallowed that,
 *    a genuine quality stop would look cleared to the next person who walked up.
 *    So the reason is on the card BEFORE the tap (whose stop is this — mine, or
 *    somebody else's?) and the still-open list is a screen AFTER it, not a toast
 *    that ages out.
 *
 * **Reason and attribution are INDEPENDENT.** `hold.blocker` carries the reason
 * and is null for a BARE hold (no note, category OTHER) — which is exactly the
 * accidental fat-finger case this feature exists for — while `hold.held_by_name`
 * / `hold.held_at` carry who and when from the `operation_hold` event. Gating
 * one on the other makes the mis-tap case render as both anonymous and
 * reasonless, the single case that most needs to read as an accident.
 *
 * **Known gap — the kiosk cannot clear a blocker, only resume past one.** For an
 * ACCIDENTAL hold the right outcome is resolving the blocker: that resumes the
 * operation as a side effect (`_resume_operation_if_no_open_blockers`) AND closes
 * the record, leaving nothing diverging. Resuming alone leaves a phantom blocker
 * for somebody to chase on the dashboard and the WO Blockers panel. The kiosk
 * still ships resume-only because `POST /work-order-blockers/{id}/resolve` is
 * unreachable from BOTH kiosks behind two independent gates: it requires
 * ADMIN/MANAGER/SUPERVISOR (an OPERATOR on the single-operator kiosk, which uses
 * their own session, gets 403), and /api/v1/work-order-blockers sits outside
 * `KIOSK_TOKEN_PATH_PREFIXES`, so a badge-minted crew-station token is 403 there
 * no matter whose badge it is. Widening either is a security/RBAC decision, not
 * a frontend one. Until then the copy tells the operator the record stays open
 * and who closes it — see KioskResumeConfirmModal and KioskBlockerStillOpenScreen.
 */

import { HOLD_REASONS } from './kioskConstants';
import { formatCentralDateTime } from '../../utils/centralTime';
import type { OperationHold, ResumeOpenBlocker, ResumeOperationResult } from '../../types';

/**
 * "Machine down" for `machine_down`. Reuses the EXISTING kiosk hold vocabulary
 * (HOLD_REASONS) so the label an operator reads back is the same one the hold
 * tile showed when it was placed. An unrecognized category is humanized rather
 * than dropped — a category the kiosk cannot name is still information.
 */
export function holdReasonLabel(category: string | null | undefined): string | null {
  const raw = (category || '').trim();
  if (!raw) return null;
  const known = HOLD_REASONS.find((reason) => reason.value === raw);
  if (known) return known.label;
  return raw.replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase());
}

/** Severity chip text, e.g. "critical" -> "Critical". Null when unset. */
export function holdSeverityLabel(severity: string | null | undefined): string | null {
  const raw = (severity || '').trim();
  if (!raw) return null;
  return raw.replace(/^./, (c) => c.toUpperCase());
}

/**
 * "Held by Dana R. · Aug 11, 2026, 2:14 PM" — who stopped the job and when.
 *
 * Reads `held_by_name` / `held_at`, which the server resolves with most-recent
 * -wins across the blocker and the `operation_hold` event. It does NOT fall back
 * to the blocker's own reporter: if the blocker were the newest record the
 * server would already have used it, so a fallback here could only contradict
 * that resolution.
 *
 * Returns null when the payload carries neither — the card must not print
 * "Held by —", which reads like an answer. Partial data yields the half we have.
 */
export function formatHoldAttribution(hold: OperationHold | null | undefined): string | null {
  if (!hold) return null;
  const who = (hold.held_by_name || '').trim();
  const at = (hold.held_at || '').trim();
  const when = at ? formatCentralDateTime(at) : '';
  if (who && when) return `Held by ${who} · ${when}`;
  if (who) return `Held by ${who}`;
  if (when) return `Held ${when}`;
  return null;
}

/**
 * True when a blocker explains the hold — i.e. there is reason TEXT to show.
 *
 * Deliberately independent of attribution: a bare hold has no blocker but may
 * still name who pressed it, and both halves render on their own terms.
 */
export function hasHoldReason(hold: OperationHold | null | undefined): boolean {
  const blocker = hold?.blocker;
  if (!blocker) return false;
  return Boolean(
    (blocker.category || '').trim() || (blocker.note || '').trim() || (blocker.title || '').trim()
  );
}

/**
 * True when the payload says nothing at all about the hold — no reason AND no
 * attribution. This is a REAL state (a hold placed before either record was
 * written; the server never infers a holder from `operation.updated_at`), so it
 * renders as "reason not recorded" rather than an error or an empty panel.
 */
export function holdIsUnexplained(hold: OperationHold | null | undefined): boolean {
  return !hasHoldReason(hold) && formatHoldAttribution(hold) === null;
}

/**
 * The blockers a resume left open. Tolerates a null/absent list (an older
 * backend, or a payload that dropped the key) by reporting none — the caller
 * then shows the plain success path rather than an empty scare screen.
 */
export function stillOpenBlockers(result: ResumeOperationResult | null | undefined): ResumeOpenBlocker[] {
  const blockers = result?.open_blockers;
  return Array.isArray(blockers) ? blockers : [];
}

/**
 * One line per still-open blocker, built from the server's own `title` VERBATIM
 * (it is server-composed, e.g. "Machine Down: OP20 Deburr"). The category is
 * used only when the title is missing, so the kiosk never rewords what the
 * server said about a quality record.
 */
export function openBlockerLine(blocker: ResumeOpenBlocker): string {
  const title = (blocker.title || '').trim();
  if (title) return title;
  return holdReasonLabel(blocker.category) || 'Open blocker';
}
