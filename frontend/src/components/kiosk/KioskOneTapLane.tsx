import React from 'react';
import { CheckCircleIcon, ExclamationTriangleIcon } from '@heroicons/react/24/solid';
import type { OneTapPieces } from './useOneTapPieces';

/**
 * The one-tap `+1 PIECE` lane — tap once per finished part.
 *
 * This is a DIFFERENT CONTROL from the `+5 / +25 / Full nest` quick-add row it
 * sits above, and the difference is not decoration. The two commit differently:
 *
 *   +1 PIECE  is a COMMIT with a grace period. The tap is the decision; the
 *             window only buys the operator a way out. It will be recorded —
 *             on the countdown, on RECORD (the screen's confirm), on leaving
 *             the screen, on the idle flow-reset, on page unload.
 *   +5 / +25  are an ENTRY. They fill the GOOD field and are recorded only when
 *   / keypad   the operator taps the screen's confirm, and are discarded on
 *             cancel or idle exactly as they always have been.
 *
 * Because those two fates differ, the two numbers may not share a display: one
 * merged field would either auto-post a keyed 25 nobody confirmed, or discard
 * tapped pieces the operator already committed to. So the lane owns its own
 * count, its own chrome, and its own vocabulary, and `+1` is REMOVED from the
 * quick-add row wherever this lane renders — two controls reading `+1` with
 * different commit semantics on one screen is exactly the ambiguity that makes
 * an operator unsure whether their part was counted.
 *
 * The three states an operator must never confuse, and how they are told apart
 * without reading a word:
 *   PENDING  — amber, DASHED border, a depleting bar, an UNDO control.
 *   RECORDED — green, SOLID border, a check, NO undo control anywhere (the
 *              kiosk has no undo for a posted report; implying one would be a
 *              lie the correction screen has to clean up).
 *   NOT SAVED — red, solid, `role="alert"`, the server's words verbatim.
 */

interface KioskOneTapLaneProps {
  oneTap: OneTapPieces;
  /**
   * Who every tap on this lane is being recorded as, named ON the control.
   *
   * The screen heading says it too, but the heading is not where an operator
   * looks while tapping, and the failure it guards is quiet: one person scans,
   * walks away, and the next person taps twenty parts that all record under the
   * first name — with every tap resetting the 90-second idle timer, so
   * inactivity never bounds it either. The name sits on the lane so the wrong
   * one is visible at the moment of the tap.
   */
  operatorName: string;
  /**
   * False once `posted + unbanked` reaches the operation target. The server
   * refuses an over-target report before any mutation, so the tap goes away
   * rather than keying a guaranteed 400 (the repo's non-optimistic rule).
   */
  atCeiling: boolean;
  /** busy || offline — the kiosk's standard mutation gate. */
  blocked: boolean;
  /** id of the offline banner, for aria-describedby, when offline. */
  offlineHintId?: string;
  online: boolean;
}

const LABEL_CLASSES = 'font-mono text-[11px] font-bold uppercase tracking-[0.18em]';

export default function KioskOneTapLane({
  oneTap,
  operatorName,
  atCeiling,
  blocked,
  offlineHintId,
  online,
}: KioskOneTapLaneProps) {
  const { phase, pending, inFlight, lastRecorded, pendingLabel, remainingMs, windowMs, error } = oneTap;
  const secondsLeft = Math.ceil(remainingMs / 1000);
  const barPct = windowMs > 0 ? Math.max(0, Math.min(100, (remainingMs / windowMs) * 100)) : 0;

  // A tap is refused while a post is in flight only by the ceiling, never by
  // `busy`: the whole point is that a run of parts keeps counting while the
  // previous batch is still on the wire. Offline is the exception — every other
  // mutation control on this kiosk goes dark offline, and a tap that cannot
  // reach the server must not look like one that did.
  // An orphaned delta belongs to somebody else. Taking more taps would either
  // merge two operators' pieces into one report or bury the notice, so the lane
  // stops until it is resolved.
  const tapDisabled = atCeiling || !online || blocked || phase === 'orphaned';

  const statusTone =
    phase === 'orphaned'
      ? 'border-fd-amber bg-fd-amber/10'
      : phase === 'failed'
      ? 'border-fd-red bg-fd-red/10'
      : phase === 'recorded'
        ? 'border-fd-green bg-fd-green/10'
        : phase === 'pending'
          ? 'border-dashed border-fd-amber bg-fd-amber/10'
          : phase === 'saving'
            ? 'border-fd-amber bg-fd-amber/10'
            : 'border-fd-line bg-fd-sunken';

  return (
    <section
      data-testid="kiosk-onetap"
      data-phase={phase}
      aria-label="One-tap piece count"
      // No outer margin: the two hosts space this differently (a grid column on
      // the crew screen, a flex gap in the report overlay) and a margin baked in
      // here fights both.
      className="rounded-[4px] border border-fd-line-bright bg-fd-raised p-3"
    >
      {/* Status block — fixed height so the tap target below never moves under
          a gloved thumb as the state changes. */}
      <div
        data-testid="kiosk-onetap-status"
        role={phase === 'failed' ? 'alert' : 'status'}
        className={`flex min-h-[78px] flex-col justify-center rounded-[3px] border px-4 py-2.5 ${statusTone}`}
      >
        {phase === 'pending' && (
          <>
            <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
              <p className="min-w-0">
                <span className="font-mono text-4xl font-bold tabular-nums text-fd-amber">{pending}</span>
                <span className={`${LABEL_CLASSES} ml-2 text-fd-amber`}>pcs · not yet recorded</span>
              </p>
              <p data-testid="kiosk-onetap-countdown" className={`${LABEL_CLASSES} shrink-0 text-fd-amber`}>
                recording in {secondsLeft}s
              </p>
            </div>
            <div className="mt-2 h-1.5 overflow-hidden rounded-[2px] bg-fd-canvas">
              <div className="h-full bg-fd-amber transition-[width] duration-100 ease-linear" style={{ width: `${barPct}%` }} />
            </div>
          </>
        )}

        {phase === 'saving' && (
          <p className={`${LABEL_CLASSES} text-fd-amber`}>
            <span className="font-mono text-3xl font-bold tabular-nums">{inFlight}</span>
            <span className="ml-2">pcs · recording…</span>
            {pending > 0 && <span className="ml-2 text-fd-mute">+{pending} more pending</span>}
          </p>
        )}

        {phase === 'recorded' && (
          <p className={`${LABEL_CLASSES} flex items-center gap-3 text-fd-green`}>
            <CheckCircleIcon className="h-8 w-8 shrink-0" aria-hidden="true" />
            <span className="font-mono text-3xl font-bold tabular-nums">{lastRecorded}</span>
            <span>pcs recorded</span>
          </p>
        )}

        {phase === 'failed' && (
          <div className="flex items-start gap-3">
            <ExclamationTriangleIcon className="h-8 w-8 shrink-0 text-fd-red" aria-hidden="true" />
            <div className="min-w-0">
              <p className={`${LABEL_CLASSES} text-fd-red`}>
                <span className="font-mono text-3xl font-bold tabular-nums">{pending}</span>
                <span className="ml-2">pcs not saved</span>
              </p>
              <p className="mt-1 text-sm text-fd-body">{error}</p>
            </div>
            {/* RETRY lives INSIDE the status block, never in the button row —
                see the fixed-geometry note on that row. */}
            <button
              type="button"
              data-testid="kiosk-onetap-retry"
              disabled={!online || blocked}
              aria-describedby={!online ? offlineHintId : undefined}
              onClick={oneTap.retry}
              className="ml-auto min-h-[44px] shrink-0 self-center rounded-[3px] border border-fd-red bg-fd-red/15 px-5 font-mono text-sm font-bold uppercase tracking-[0.08em] text-fd-red transition-transform duration-150 ease-out active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-40"
            >
              Retry
            </button>
          </div>
        )}

        {/* ORPHANED — the delta outlived the pair that made it. It names whose
            pieces they are and where they came from, because nobody standing at
            the kiosk can consent on that operator's behalf: the ONLY thing that
            banks these is that same operator scanning back onto that same job.
            There is deliberately no "save anyway" here. */}
        {phase === 'orphaned' && (
          <div className="flex items-start gap-3">
            <ExclamationTriangleIcon className="h-8 w-8 shrink-0 text-fd-amber" aria-hidden="true" />
            <div className="min-w-0">
              <p className={`${LABEL_CLASSES} text-fd-amber`}>
                <span className="font-mono text-3xl font-bold tabular-nums">{pending}</span>
                <span className="ml-2">pcs not saved · another operator&apos;s count</span>
              </p>
              <p className="mt-1 text-sm text-fd-body">
                Tapped by {pendingLabel ?? 'another operator'}. Only they can save these, on that job — ask them to
                scan, or have a supervisor record the pieces in the office.
              </p>
            </div>
          </div>
        )}

        {phase === 'idle' && (
          <p className={`${LABEL_CLASSES} text-fd-mute`}>
            {atCeiling
              ? 'operation is already at its target'
              : `tap once per finished piece · ${Math.round(windowMs / 1000)}s to undo`}
          </p>
        )}
      </div>

      {/* Who this records as, on the control itself. */}
      <p className={`${LABEL_CLASSES} mt-2 text-fd-mute`} data-testid="kiosk-onetap-operator">
        recording as <span className="text-fd-body">{operatorName}</span>
      </p>

      {/* FIXED GEOMETRY, and it is a safety property rather than tidiness.
          Measured on the tablet: when UNDO was rendered only while something was
          pending, `+1 PIECE` grew back to full width the instant the window
          closed — so a thumb already travelling toward UNDO landed on `+1` and
          recorded a piece instead of removing one, which is the precise accident
          this window exists to prevent. Both controls are therefore always
          present at the same size; UNDO simply goes dim when there is nothing to
          take back, and RETRY is kept out of this row entirely.

          `flex-wrap` + a min width on the tap target is what keeps that true in
          the single-operator kiosk's REPORT overlay, whose entry column is only
          ~250px: rather than squeezing `+1 PIECE` until the label wraps inside a
          76px button, UNDO drops to its own line at the same size. Both controls
          keep their full touch target on every surface — which is the point. */}
      <div className="mt-2.5 flex flex-wrap gap-2.5">
        <button
          type="button"
          data-testid="kiosk-onetap-add"
          disabled={tapDisabled}
          aria-describedby={!online ? offlineHintId : undefined}
          onClick={oneTap.tap}
          className="min-h-[76px] min-w-[150px] flex-1 whitespace-nowrap rounded-[4px] border-2 border-fd-green bg-fd-green/15 font-mono text-2xl font-bold uppercase tracking-[0.08em] text-fd-green transition-transform duration-150 ease-out active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-40"
        >
          +1 piece
        </button>

        {/* UNDO removes ONE tap, mirroring the button it undoes — the label says
            so rather than leaving the scope to be guessed. Disabled, not
            removed, once nothing is left to take back: in the RECORDED state
            there is genuinely no undo (the correction screen is the only path
            after a post), and a dim control says that more plainly than a
            control that vanishes. */}
        <button
          type="button"
          data-testid="kiosk-onetap-undo"
          aria-label="Undo one piece"
          disabled={pending <= 0 || phase === 'orphaned'}
          onClick={oneTap.undoOne}
          className="min-h-[76px] w-[150px] shrink-0 whitespace-nowrap rounded-[4px] border border-fd-line-bright bg-fd-sunken font-mono text-lg font-bold uppercase tracking-[0.08em] text-fd-body transition-transform duration-150 ease-out active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-30"
        >
          Undo −1
        </button>
      </div>
    </section>
  );
}
