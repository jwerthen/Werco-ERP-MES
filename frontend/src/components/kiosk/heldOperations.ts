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
 * 4. **The blocker's free text does not reach a crew station — on BOTH the
 *    read and the write.** `title` and `note` are absent from a station-token
 *    queue response, and `title` is absent from the resume response a
 *    badge-minted station token gets back. An unattended,
 *    PIN-unlocked tablet with no idle logout is a public screen, and this system
 *    already withholds NCR titles and descriptions from that audience on the
 *    wallboard. Category, severity and the attribution stay, which is what tells
 *    a deliberate hold from a mis-tap. `holdFreeTextWithheld` (the card) and
 *    `openBlockersFreeTextWithheld` (the post-resume screen) are how each surface
 *    says "a note exists, you just cannot read it here" rather than implying none
 *    was given. The single-operator kiosk runs on the operator's OWN session and
 *    keeps the full text on both.
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
import type { BlockerResumeWithheldReason, WorkOrderBlockerWriteResult } from '../../types/aiForward';

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
 * The blocker's `title`, or null when it only restates the category chip.
 *
 * WHY THIS EXISTS: `title` is `nullable=False` server-side but only SOMETIMES
 * server-composed — `POST /work-order-blockers` takes a caller-supplied one, and
 * per `_blocker_free_text_recorded` an office-created blocker "routinely puts its
 * free text there with an empty note". Dropping `title` therefore hides the ONLY
 * reason text such a hold has, on the screens built to show the reason before the
 * Clear Hold button. `hasHoldReason` counts a title as a reason, so the "No reason
 * given" fallback does not fire either: the panel would render a bare category and
 * nothing under it, which is the exact "silence reads as no reason given" mis-read
 * this module already guards against for a withheld note.
 *
 * The echo suppression is the other half: a title identical to the category (or to
 * "category · severity") printed under that same chip reads as a rendering bug on
 * the one panel that has to be believed.
 *
 * Null when the text was withheld (a station payload sends no `title` key) — that
 * case is `holdFreeTextWithheld`'s to state, not this one's to guess at.
 */
export function holdTitleText(hold: OperationHold | null | undefined): string | null {
  const blocker = hold?.blocker;
  const raw = (blocker?.title || '').trim();
  if (!raw) return null;
  const category = holdReasonLabel(blocker?.category);
  const severity = holdSeverityLabel(blocker?.severity);
  const headline = [category, severity].filter(Boolean).join(' · ');
  const lowered = raw.toLowerCase();
  if (category && lowered === category.toLowerCase()) return null;
  if (headline && lowered === headline.toLowerCase()) return null;
  return raw;
}

/**
 * True when a written reason EXISTS but this response deliberately did not carry
 * it — a crew-station (shared, unattended, PIN-unlocked) payload.
 *
 * The server withholds `title` / `note` from a station principal, for the same
 * reason the wallboard withholds NCR titles and descriptions: an unattended
 * tablet is a public screen. Without this the card would show a categorized hold
 * with nothing under it, and an operator could read "Machine Down · Held by Dana
 * R." as a mis-tap when Dana actually wrote "spindle bearing failed — do not
 * run". So the card says the note exists and where to get it, rather than
 * implying none was given.
 *
 * False on a user session (the single-operator kiosk, the desktop): the text is
 * there and gets rendered.
 */
export function holdFreeTextWithheld(hold: OperationHold | null | undefined): boolean {
  const blocker = hold?.blocker;
  if (!blocker) return false;
  return Boolean(blocker.free_text_withheld && blocker.has_note);
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
 * The toast a successful resume earns — and it is not always "success".
 *
 * Resume RESTORES, it does not release. An operation with no labor evidence is
 * floored at PENDING and lifted to READY only by the server's own promotion
 * rule, so resuming a hold that was placed on a PENDING operation — or one whose
 * work order is still DRAFT, or whose predecessor is incomplete — lands PENDING
 * and the job does NOT come back to the queue. A green "resumed" toast there
 * would send the operator looking for a card that is not going to appear.
 *
 * `info` rather than the shared Toast's `warning` variant on purpose: these two
 * station screens carry their own local success/error/info toast, and widening
 * that vocabulary is a styling change this does not need. What matters is that
 * the copy states the shortfall instead of hiding it.
 */
export function resumeToast(
  result: ResumeOperationResult | null | undefined,
  workOrderNumber: string | null | undefined
): { type: 'success' | 'info'; message: string } {
  const wo = (workOrderNumber || 'Operation').trim() || 'Operation';
  if ((result?.status || '').trim() === 'pending') {
    return { type: 'info', message: `${wo} hold lifted — still waiting on release or an earlier step.` };
  }
  return { type: 'success', message: `${wo} resumed` };
}

/**
 * THE ONE PLACE that decides whether a Clear Hold fell short — read off the
 * SERVER's answer, never off "the call did not throw".
 *
 * Two shortfalls, and either alone is enough:
 *
 * 1. **`status === 'pending'`** — the hold came off but the job did NOT come
 *    back to the board (unreleased parent, or an incomplete predecessor).
 * 2. **`open_blockers` is non-empty** — resume RESTORES the operation and
 *    deliberately does not resolve the blocker.
 *
 * Extracted because THREE screens clear a hold — the Time Clock page,
 * ShopFloorSimple and the Work Order page — and they word the outcome
 * differently on purpose (a phone-sized toast vs. an office sentence) while the
 * JUDGEMENT has to be identical. A drifted copy of this rule is how one screen
 * ends up reporting a live quality stop as cleared. Compose the words at the
 * call site; take the verdict from here.
 */
export function clearHoldOutcome(result: ResumeOperationResult | null | undefined): {
  landedPending: boolean;
  openBlockers: ResumeOpenBlocker[];
  fellShort: boolean;
} {
  const openBlockers = stillOpenBlockers(result);
  const landedPending = (result?.status || '').trim() === 'pending';
  return { landedPending, openBlockers, fellShort: landedPending || openBlockers.length > 0 };
}

/**
 * The toast a CLEAR HOLD earns on the desk screens — ShopFloor (Time Clock) and
 * ShopFloorSimple — where the shared `<Toast>` carries a `warning` variant the
 * two station screens do not.
 *
 * Same two shortfalls `resumeToast` reasons about, plus the one it cannot say:
 *
 * 1. **`status === 'pending'`** — the hold came off but the job did NOT come
 *    back to the board (unreleased parent, or an incomplete predecessor). A
 *    green "cleared" there sends somebody looking for a card that will not
 *    appear.
 * 2. **`open_blockers` is non-empty** — resume RESTORES the operation and
 *    deliberately does not resolve the blocker, so the record stays open for
 *    somebody to close. Green would report a quality stop as handled.
 *
 * Both can hold at once, and they compose into ONE warning rather than two
 * toasts: two toasts for one tap read as two things happening, and the second
 * would push the first off the stack before it was read.
 *
 * `warning` and not `error`: the write SUCCEEDED. Claiming a failure would send
 * the operator to look for an operation that is, in fact, no longer held.
 *
 * Deliberately NOT merged with `resumeToast`: that one serves the kiosk and
 * crew station, whose local toast vocabulary is success/error/info, and
 * widening it there is a styling change those screens do not need. The two
 * share every fact they read (`stillOpenBlockers`, `openBlockerLine`) so they
 * cannot disagree about what happened — only about which chrome says it.
 */
export function clearHoldToast(
  result: ResumeOperationResult | null | undefined,
  workOrderNumber: string | null | undefined
): { type: 'success' | 'warning'; message: string } {
  const wo = (workOrderNumber || 'Operation').trim() || 'Operation';
  const { landedPending, openBlockers: blockers, fellShort } = clearHoldOutcome(result);

  if (!fellShort) {
    return { type: 'success', message: `${wo} hold cleared` };
  }

  const shortfalls: string[] = [];
  if (landedPending) {
    shortfalls.push('the job did not return to the queue (still waiting on release or an earlier operation)');
  }
  if (blockers.length > 0) {
    shortfalls.push(
      `${blockers.length} blocker${blockers.length === 1 ? '' : 's'} still open: ${namedOpenBlockers(blockers)}`
    );
  }
  return { type: 'warning', message: `${wo} hold cleared — ${shortfalls.join('; ')}.` };
}

/**
 * THE ONE PLACE that decides whether RESOLVING A BLOCKER fell short.
 *
 * Twin of `clearHoldOutcome` for the other verb, and it exists for the same
 * defect wearing a different hat: a shop owner resolved a blocker on a held nest,
 * got a green "Resolved blocker" toast, and the operation was still ON_HOLD.
 * Closing a blocker resumes its operation only SOMETIMES, and the 200 now says
 * which way it went (`operation_outcome`).
 *
 * Three states, and conflating any two of them is a bug:
 *
 * 1. **Not attempted** (`operation_outcome` absent/null) — the call left the
 *    blocker open or acknowledged, so the operation was never a candidate.
 *    `fellShort` is false. Warning here would put a "still held" notice on an
 *    acknowledge, which is a NEW false statement, not a fix.
 * 2. **Owed and withheld** (`operation_still_held`) — the operation IS still on
 *    hold. Usually because another blocker still names it; `openBlockers` says
 *    which. This is the reported defect.
 * 3. **Cleared but off the board** (`operation_resumed` and
 *    `operation_status === 'pending'`) — the hold really did lift, but PENDING is
 *    off the dispatch board and off the kiosk (both surface READY only), so the
 *    job did not come back to the queue. Not a withheld resume, and it carries no
 *    withheld reason: read the STATUS, not just the boolean.
 *
 * Read off the SERVER's answer, never off "the call did not throw" — and never
 * off `resume_withheld_reason` alone, which is true for three reasons that mean
 * *there was nothing to resume*. Compose the words at the call site; take the
 * verdict from here, exactly as the three Clear Hold screens do.
 */
export function resolveBlockerOutcome(result: WorkOrderBlockerWriteResult | null | undefined): {
  attempted: boolean;
  stillHeld: boolean;
  landedPending: boolean;
  openBlockers: ResumeOpenBlocker[];
  withheldReason: BlockerResumeWithheldReason | null;
  fellShort: boolean;
} {
  const outcome = result?.operation_outcome;
  if (!outcome) {
    return {
      attempted: false,
      stillHeld: false,
      landedPending: false,
      openBlockers: [],
      withheldReason: null,
      fellShort: false,
    };
  }
  const stillHeld = outcome.operation_still_held === true;
  const landedPending = outcome.operation_resumed === true && (outcome.operation_status || '').trim() === 'pending';
  const openBlockers = Array.isArray(outcome.open_blockers) ? outcome.open_blockers : [];
  return {
    attempted: true,
    stillHeld,
    landedPending,
    openBlockers,
    withheldReason: outcome.resume_withheld_reason ?? null,
    fellShort: stillHeld || landedPending,
  };
}

/**
 * The still-open blockers NAMED — "Machine Down: OP20 Deburr; Awaiting MTR".
 *
 * One line per blocker from the server's own text (`openBlockerLine`), joined
 * the one way. Three toasts print this list — the desk Clear Hold
 * (`clearHoldToast`), the Work Order page's Clear Hold, and its resolve-a-blocker
 * — and a separator that drifts between them is a small thing that makes two
 * screens look like two systems. Empty string for an empty list: every caller
 * already tests `length` before deciding to say anything at all.
 */
export function namedOpenBlockers(blockers: ResumeOpenBlocker[]): string {
  return blockers.map(openBlockerLine).join('; ');
}

/**
 * THE ONE EXPLANATION of a resume that landed at PENDING — the office-length
 * sentence, shared by the two verbs on the Work Order page that can produce it.
 *
 * Both Clear Hold and Resolve-a-blocker end in the same server behavior: resume
 * RESTORES, it does not release, so an operation with no labor evidence is
 * floored at PENDING, and PENDING is off the dispatch board and off the kiosk
 * (both surface READY only). The job did not come back. That is ONE fact about
 * the server, and it was written out twice — a copy each in `handleConfirmClearHold`
 * and `handleResolveBlocker` — which is how two buttons on one page end up
 * describing one outcome in two ways, one of them eventually stale.
 *
 * `subject` is the only thing the two call sites legitimately differ on: Clear
 * Hold has already named the operation ("...: hold cleared. It did NOT..."),
 * while the resolve toast has been talking about a BLOCKER and has to hand the
 * subject back to the job ("...The hold cleared, but the job did NOT..."). The
 * explanation after it is identical, and that is the part that must not drift.
 *
 * Deliberately NOT folded into `clearHoldToast`: that one is the phone-sized
 * station/desk toast and words the same fact short on purpose ("still waiting on
 * release or an earlier operation"). Two audiences, one fact — the fact is what
 * is shared.
 */
export function offTheBoardSentence(subject: string = 'It'): string {
  return (
    `${subject} did NOT go back on the board — it is Pending again, so it will not show on the dispatch ` +
    'board or at the kiosk until the work order is released and any earlier operations are finished.'
  );
}

/**
 * Why the operation is STILL on hold after a blocker was closed, and the two
 * ways off it — the sentence the reported defect's green toast replaced with
 * silence.
 *
 * EXHAUSTIVE over `BlockerResumeWithheldReason` on purpose. The backend's
 * vocabulary is closed and documented as growing (the cancelled-nest tombstone
 * guard on the unmerged nest-removal branch is a known fifth member), and the
 * failure mode of a `default:` clause here is precisely the one this whole change
 * exists to remove: a new server reason silently rendering a vague line that
 * happens to still be grammatical. The `never` binding below turns that into a
 * COMPILE ERROR, so whoever adds the fifth reason has to decide what the shop
 * reads.
 *
 * Called ONLY when `resolveBlockerOutcome` reports `stillHeld` — read off the
 * operation's status, never off the reason name (three of the four reasons mean
 * there was nothing to resume). The reason only picks the WORDS; it never decides
 * whether to warn. The nothing-to-resume reasons are handled here anyway, and
 * defensively: a server that ever pairs one of them with a held operation gets a
 * true, if unspecific, sentence rather than a crash or an empty string.
 *
 * Clear Hold is named as the second remedy because Release 1 put that control on
 * the operation's own row on this very page — the person reading this toast can
 * act on it without leaving the screen. Naming only "resolve the others" would
 * describe half the exits.
 */
export function stillHeldSentence(
  reason: BlockerResumeWithheldReason | null | undefined,
  blockers: ResumeOpenBlocker[]
): string {
  const clearHold = "Use Clear Hold on the operation's row to lift it.";
  switch (reason) {
    case 'other_blockers_open': {
      // The count is read off the LIST, not off the reason: the list is what the
      // reader has to go and close, so "2 other blockers" must never be printed
      // beside one name.
      if (blockers.length === 0) {
        return (
          'The operation is STILL on hold — another blocker is still open on it. Resolve it too, or ' +
          lowerFirst(clearHold)
        );
      }
      const many = blockers.length > 1;
      return (
        `The operation is STILL on hold — ${many ? `${blockers.length} other blockers are` : 'another blocker is'} ` +
        `still open on it (${namedOpenBlockers(blockers)}). Resolve ${many ? 'those' : 'that one'} too, or ` +
        lowerFirst(clearHold)
      );
    }
    case 'no_operation':
    case 'operation_not_held':
    case 'operation_missing':
    case null:
    case undefined:
      return `The operation is STILL on hold — closing this blocker did not lift it. ${clearHold}`;
    default: {
      // A reason this build has never heard of. It cannot be worded honestly, so
      // it is a compile error instead — see the docstring.
      const unhandled: never = reason;
      return `The operation is STILL on hold (${String(unhandled)}). ${clearHold}`;
    }
  }
}

/** "Use Clear Hold..." -> "use Clear Hold...", for mid-sentence reuse. */
function lowerFirst(text: string): string {
  return text.replace(/^./, (c) => c.toLowerCase());
}

/**
 * One line per still-open blocker, built from the server's own `title` VERBATIM
 * (e.g. "Machine Down: OP20 Deburr") — the kiosk never rewords what the system
 * recorded about a quality record.
 *
 * The title is ABSENT on a crew-station response (it is caller-supplied free
 * text; see `ResumeOpenBlocker`), so the category label is the fallback and
 * "Open blocker" the floor under that. `openBlockerMeta` below is what keeps the
 * fallback from reading as a stutter.
 */
export function openBlockerLine(blocker: ResumeOpenBlocker): string {
  const title = (blocker.title || '').trim();
  if (title) return title;
  return holdReasonLabel(blocker.category) || 'Open blocker';
}

/**
 * The category · severity line UNDER `openBlockerLine`, and the reason it is a
 * function rather than a template.
 *
 * With a title present the two lines say different things ("Machine Down: OP20
 * Deburr" / "Machine down · High"). With the title WITHHELD — every crew-station
 * response — `openBlockerLine` already IS the category, so repeating it below
 * renders "Machine down / Machine down · High": a stutter that reads like a
 * rendering bug on the one screen that has to be believed. So the category drops
 * out of the meta line whenever it is doing duty as the headline.
 *
 * Null when there is nothing left to say (no severity on a title-less blocker).
 */
export function openBlockerMeta(blocker: ResumeOpenBlocker): string | null {
  const titled = Boolean((blocker.title || '').trim());
  const category = titled ? holdReasonLabel(blocker.category) : null;
  const severity = holdSeverityLabel(blocker.severity);
  const parts = [category, severity].filter(Boolean) as string[];
  return parts.length ? parts.join(' · ') : null;
}

/**
 * True when ANY of these blockers had free text the server deliberately did not
 * send — i.e. this is a crew station and somebody wrote a reason.
 *
 * Same job as `holdFreeTextWithheld` on the held card, one screen later: without
 * it the resumed-with-holds screen shows a bare category, and silence there
 * reads as "nobody wrote anything", which is the one way withholding could
 * actively mislead. Aggregated over the list because the copy is one line for
 * the panel, not one per row — the operator's action (ask a supervisor) is the
 * same either way.
 */
export function openBlockersFreeTextWithheld(blockers: ResumeOpenBlocker[]): boolean {
  return blockers.some((blocker) => Boolean(blocker.free_text_withheld && blocker.has_note));
}
