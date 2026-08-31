import React from 'react';
import { PauseCircleIcon } from '@heroicons/react/24/solid';
import {
  formatHoldAttribution,
  hasHoldReason,
  holdFreeTextWithheld,
  holdIsUnexplained,
  holdReasonLabel,
  holdSeverityLabel,
  holdTitleText,
} from '../kiosk/heldOperations';
import type { OperationHold } from '../../types';

interface OperationHoldReasonProps {
  /** The `hold` block the server sends on a held row. Renders nothing when absent. */
  hold?: OperationHold | null;
  /** 'sm' = a table row or a dense card; 'md' = a stand-alone panel. */
  size?: 'sm' | 'md';
  className?: string;
  /** Test hook, so a page can name its own instance. */
  testId?: string;
}

/**
 * WHY an operation is held — the disclosure that has to be on screen BEFORE the
 * Clear Hold button, on the DESK screens (Time Clock, Shop Floor).
 *
 * The kiosk has had this since `KioskHeldCard`; the desk did not, so an office
 * user looked at a held nest and saw "on hold" and nothing else. Clearing a hold
 * you cannot see the reason for is how somebody else's genuine quality stop gets
 * silently lifted — which is exactly what this control would otherwise make one
 * tap away.
 *
 * The rules here are `KioskHeldCard`'s, deliberately unchanged, so the office
 * and the floor cannot tell two different stories about one hold:
 *
 * - **Reason and attribution render INDEPENDENTLY.** A BARE hold (no note,
 *   category OTHER — the accidental fat-finger case) files no blocker at all, so
 *   it has no reason text but usually still names who pressed it. Gating the
 *   attribution on the blocker would leave exactly that case anonymous AND
 *   reasonless — the one case that most needs to read as an accident.
 * - **Only when BOTH are absent** does it say the reason was not recorded. That
 *   is a REAL state (a hold placed before either record was written); the server
 *   never infers a holder from `operation.updated_at`.
 * - **A withheld note is stated, not hidden.** `free_text_withheld` is `false`
 *   on every payload this component sees today — both desk endpoints are served
 *   to identified user sessions, never to a station principal — but the branch
 *   is kept because silence where a note exists reads as "no reason given",
 *   which is the one way withholding could actively mislead. If a future payload
 *   ever routes through here withheld, it says so instead of going quiet.
 *
 * ONE rule is deliberately WIDER than `KioskHeldCard`'s: this renders the
 * blocker's `title` (echo-suppressed via the shared `holdTitleText`). A kiosk
 * hold's title is server-composed, so the card loses nothing by dropping it — but
 * an OFFICE-created blocker routinely carries its free text in the title with an
 * empty note, and those are the blockers the desk screens show. Dropping it there
 * rendered a bare category over a hold that had a written reason, while the Work
 * Order page showed that same reason: two screens, one hold, two stories.
 *
 * Amber throughout, matching the hold controls and the kiosk's held card, so
 * stopped work never reads as startable.
 */
export default function OperationHoldReason({
  hold,
  size = 'sm',
  className = '',
  testId = 'operation-hold-reason',
}: OperationHoldReasonProps) {
  if (!hold) return null;

  const blocker = hold.blocker;
  const reason = holdReasonLabel(blocker?.category);
  const severity = holdSeverityLabel(blocker?.severity);
  const note = (blocker?.note || '').trim();
  // The blocker's own title, echo-suppressed. An office-created blocker routinely
  // carries its free text HERE with an empty note (see `holdTitleText`), so dropping
  // it would render a bare category on the desk screens for exactly the holds an
  // office user filed -- while the Work Order page showed the text, i.e. two screens
  // telling different stories about one hold.
  const title = holdTitleText(hold);
  const attribution = formatHoldAttribution(hold);
  const reasonKnown = hasHoldReason(hold);
  const unexplained = holdIsUnexplained(hold);
  const noteWithheld = holdFreeTextWithheld(hold);
  const roomy = size === 'md';

  return (
    <div
      data-testid={testId}
      className={`rounded-sm border border-amber-500/30 bg-amber-500/10 ${roomy ? 'px-3 py-2.5' : 'px-2.5 py-2'} ${className}`}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span
          className={`inline-flex items-center gap-1.5 font-semibold uppercase tracking-wide text-amber-300 ${
            roomy ? 'text-xs' : 'text-[11px]'
          }`}
        >
          <PauseCircleIcon className="h-3.5 w-3.5" aria-hidden="true" />
          On hold
        </span>
        {reasonKnown && reason && (
          <span
            data-testid={`${testId}-category`}
            className={`font-semibold text-amber-200 ${roomy ? 'text-sm' : 'text-xs'}`}
          >
            {reason}
          </span>
        )}
        {severity && (
          <span
            data-testid={`${testId}-severity`}
            className="rounded-sm border border-amber-500/40 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-amber-200/90"
          >
            {severity}
          </span>
        )}
      </div>

      {reasonKnown && title && (
        <p
          data-testid={`${testId}-title`}
          className={`mt-1 font-medium text-amber-100 ${roomy ? 'text-sm' : 'text-xs'}`}
        >
          {title}
        </p>
      )}

      {reasonKnown && note && (
        <p data-testid={`${testId}-note`} className={`mt-1 text-amber-100/90 ${roomy ? 'text-sm' : 'text-xs'}`}>
          {note}
        </p>
      )}

      {/* A note WAS written but this response deliberately did not carry it.
          Saying so is the point: silence would read as "no reason given". */}
      {reasonKnown && noteWithheld && (
        <p
          data-testid={`${testId}-note-withheld`}
          className={`mt-1 text-amber-200/80 ${roomy ? 'text-sm' : 'text-xs'}`}
        >
          A written note was recorded but is not shown here. Ask a supervisor before clearing the hold.
        </p>
      )}

      {/* Attribution stands on its own — a bare hold has WHO without WHY. */}
      {!reasonKnown && !unexplained && (
        <p data-testid={`${testId}-no-blocker`} className={`mt-1 text-amber-200/80 ${roomy ? 'text-sm' : 'text-xs'}`}>
          No reason given
        </p>
      )}

      {attribution && (
        <p data-testid={`${testId}-attribution`} className={`mt-1 text-amber-200/70 ${roomy ? 'text-sm' : 'text-xs'}`}>
          {attribution}
        </p>
      )}

      {unexplained && (
        <p data-testid={`${testId}-unrecorded`} className={`mt-1 text-amber-200/80 ${roomy ? 'text-sm' : 'text-xs'}`}>
          No hold reason recorded
        </p>
      )}
    </div>
  );
}
