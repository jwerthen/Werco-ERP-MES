import React from 'react';
import { ExclamationTriangleIcon } from '@heroicons/react/24/solid';
import { openBlockerLine, openBlockerMeta, openBlockersFreeTextWithheld } from './heldOperations';
import type { ResumeOpenBlocker } from '../../types';

interface KioskBlockerStillOpenScreenProps {
  blockers: ResumeOpenBlocker[];
  /** e.g. "WO-2026-0142 · Op 20 Deburr" */
  jobLabel: string;
  /** "Back to queue" (operator kiosk) / "Back to board" (crew station). */
  doneLabel: string;
  onDone: () => void;
}

/**
 * Shown after a resume that left blockers OPEN.
 *
 * A dedicated host view rather than a toast, for the same reason
 * KioskNcrFiledScreen is one: a 3-second notice that a quality stop is still
 * recorded is a notice nobody reads, and the 15s queue poll would yank it off
 * the screen mid-sentence. The operator has just restarted a machine whose
 * recorded problem is unresolved — that has to survive long enough to be acted
 * on, and it takes an explicit tap to leave.
 *
 * The blocker text is the SERVER's (`title`, e.g. "Machine Down: OP20 Deburr"),
 * rendered verbatim. The kiosk does not reword what the system recorded about a
 * quality hold.
 *
 * On a CREW STATION the server withholds that title — it is caller-supplied free
 * text, and this view persists until somebody taps it away on a shared,
 * unattended tablet — so each row falls back to its category and the panel says a
 * written reason exists rather than leaving a bare category to read as "nobody
 * gave one". Same split, same reason, as the held card one screen earlier.
 */
export default function KioskBlockerStillOpenScreen({
  blockers,
  jobLabel,
  doneLabel,
  onDone,
}: KioskBlockerStillOpenScreenProps) {
  const many = blockers.length > 1;
  const textWithheld = openBlockersFreeTextWithheld(blockers);
  return (
    <section aria-label="Hold still open" className="mx-auto w-full max-w-2xl text-center">
      <ExclamationTriangleIcon className="mx-auto h-16 w-16 text-fd-amber" aria-hidden="true" />
      <h2 className="mt-3 text-3xl font-bold text-fd-ink">Job resumed</h2>
      <p className="mt-1 font-mono text-lg text-fd-mute">{jobLabel}</p>

      <div className="mt-5 rounded border-2 border-fd-amber bg-fd-amber/10 px-5 py-6 text-left">
        <p className="text-center font-mono text-xs font-bold uppercase tracking-[0.25em] text-fd-amber">
          {many ? `${blockers.length} holds still open` : 'Hold still open'}
        </p>

        <ul data-testid="kiosk-open-blockers" className="mt-4 space-y-2.5">
          {blockers.map((blocker) => {
            const meta = openBlockerMeta(blocker);
            return (
              <li
                key={blocker.id}
                className="rounded border border-fd-amber/40 bg-fd-sunken px-4 py-3"
              >
                <p className="text-xl font-semibold text-fd-ink">{openBlockerLine(blocker)}</p>
                {meta && (
                  <p className="mt-1 font-mono text-sm uppercase tracking-wide text-fd-mute">{meta}</p>
                )}
              </li>
            );
          })}
        </ul>

        {/* A written reason was recorded but this screen is shared and
            unattended, so the server did not send it. Saying so is the point:
            silence here would read as "no reason was given" for a hold that is
            still open. */}
        {textWithheld && (
          <p data-testid="kiosk-blocker-open-withheld" className="mt-4 text-lg text-fd-mute">
            A written reason was recorded — not shown on a shared station. Ask a supervisor.
          </p>
        )}

        <p className="mt-4 text-lg text-fd-body">
          The job is running again, but {many ? 'these problems are' : 'this problem is'} still recorded and open.
        </p>
        <p data-testid="kiosk-blocker-open-followup" className="mt-2 text-lg text-fd-body">
          If {many ? 'they were' : 'it was'} a mistake, tell a supervisor to clear {many ? 'them' : 'it'} — resuming
          does not.
        </p>
      </div>

      <button
        type="button"
        data-testid="kiosk-blocker-open-done"
        onClick={onDone}
        className="mt-6 min-h-20 w-full rounded border border-fd-line bg-fd-sunken text-2xl font-bold uppercase tracking-wide text-fd-body transition-colors hover:border-fd-line-bright"
      >
        {doneLabel}
      </button>
    </section>
  );
}
