import React from 'react';
import { PauseCircleIcon } from '@heroicons/react/24/solid';
import { KioskQueueItem, formatOperationLabel, operationNumberText } from './kioskConstants';
import { KioskRunOrderChip } from './KioskQueueCard';
import {
  formatHoldAttribution,
  hasHoldReason,
  holdFreeTextWithheld,
  holdIsUnexplained,
  holdReasonLabel,
  holdSeverityLabel,
} from './heldOperations';

interface KioskHeldCardProps {
  item: KioskQueueItem;
  /** Resume tapped — the host opens the confirm overlay (never resumes here). */
  onResume: (item: KioskQueueItem) => void;
  /** busy || offline. Disables the ONE verb; the card still reads. */
  disabled?: boolean;
  /** id of the offline banner, for aria-describedby when offline. */
  offlineHintId?: string;
  /**
   * 'kiosk' = the compact single-operator queue column; 'crew' = the crew
   * station's large-format board. Same information, two densities — the crew
   * station is read from further back.
   */
  size?: 'kiosk' | 'crew';
}

/**
 * One ON_HOLD operation on the kiosk queue.
 *
 * Before this card a held operation was simply absent from the board, so an
 * accidental hold looked like the job had disappeared and only a desktop could
 * undo it. Three things about the card are deliberate:
 *
 * - **The card is not a tap target.** Unlike KioskQueueCard (a role="button"
 *   div that clocks you in), this is inert markup with exactly one interactive
 *   element: RESUME. A held job must not be startable — the operator has to
 *   lift the hold first, which is also what the server enforces.
 * - **The reason is shown before the verb, not after.** Category, severity, who
 *   placed it when, and — on an identified session — the operator's note
 *   verbatim. Without that, Resume is a control that silently clears somebody
 *   else's genuine quality stop; with it, an operator can tell their own mis-tap
 *   from a real one. On a CREW STATION the server withholds the free text (a
 *   shared, unattended tablet is a public screen), so the card says a note
 *   exists instead of showing it — see `holdFreeTextWithheld`.
 * - **Reason and attribution render INDEPENDENTLY.** A bare hold (no note,
 *   category OTHER — the accidental case) files no blocker at all, so it has no
 *   reason text but usually still names who pressed it. Gating the attribution
 *   on the blocker would leave exactly that case anonymous AND reasonless, which
 *   is the one case that most needs to read as an accident. Only when BOTH are
 *   absent does the card say the reason was not recorded — a real state, since
 *   the server never infers a holder it wasn't given.
 * - **Amber, matching PLACE ON HOLD.** The same colour the hold overlay uses,
 *   so held work reads as stopped at a glance and never as startable blue/green.
 */
export default function KioskHeldCard({
  item,
  onResume,
  disabled = false,
  offlineHintId,
  size = 'kiosk',
}: KioskHeldCardProps) {
  const hold = item.hold;
  const blocker = hold?.blocker;
  const reason = holdReasonLabel(blocker?.category);
  const severity = holdSeverityLabel(blocker?.severity);
  const note = (blocker?.note || '').trim();
  const attribution = formatHoldAttribution(hold);
  const reasonKnown = hasHoldReason(hold);
  const unexplained = holdIsUnexplained(hold);
  const noteWithheld = holdFreeTextWithheld(hold);
  const crew = size === 'crew';

  const done = Number(item.quantity_complete || 0);
  const ordered = Number(item.quantity_ordered || 0);

  return (
    <div
      data-testid="kiosk-held-card"
      className={`w-full rounded-[4px] border border-fd-amber/60 border-l-2 border-l-fd-amber bg-fd-amber/5 ${
        crew ? 'px-5 py-5' : 'px-4 py-3.5'
      }`}
    >
      <div className="flex flex-wrap items-center gap-2.5">
        <KioskRunOrderChip item={item} size={crew ? 'kiosk' : 'sm'} />
        <span className={`font-mono font-bold text-fd-ink ${crew ? 'text-3xl tracking-tight' : 'text-lg'}`}>
          {item.work_order_number}
        </span>
        <span
          data-testid="kiosk-held-badge"
          className={`inline-flex items-center gap-1.5 rounded-[3px] border border-fd-amber bg-fd-amber/15 font-mono font-bold uppercase tracking-[0.08em] text-fd-amber ${
            crew ? 'px-2 py-1 text-xs' : 'px-1.5 py-0.5 text-[10px]'
          }`}
        >
          <PauseCircleIcon className={crew ? 'h-4 w-4' : 'h-3 w-3'} aria-hidden="true" />
          On hold
        </span>
        {severity && (
          <span
            data-testid="kiosk-held-severity"
            className={`rounded-[3px] border border-fd-line font-mono font-semibold uppercase tracking-[0.08em] text-fd-mute ${
              crew ? 'px-2 py-1 text-xs' : 'px-1.5 py-0.5 text-[10px]'
            }`}
          >
            {severity}
          </span>
        )}
        <div className="flex-1" />
        <span className={`font-mono font-bold tabular-nums text-fd-ink ${crew ? 'text-2xl' : 'text-[15px]'}`}>
          {done}
          <span className="font-normal text-fd-mute">/{ordered}</span>
        </span>
      </div>

      <div className={`mt-2 min-w-0 truncate text-fd-body ${crew ? 'text-xl' : 'text-[13px]'}`}>
        <span className="font-mono font-semibold text-fd-body-2">{item.part_number || '—'}</span>
        {item.part_name ? ` ${item.part_name}` : ''} · {formatOperationLabel(item.operation_number)}
        {item.operation_name ? ` ${item.operation_name}` : ''}
      </div>

      {/* Why it stopped — the whole point of the card. */}
      <div
        data-testid="kiosk-held-reason"
        className={`mt-2.5 rounded-[3px] border border-fd-amber/30 bg-fd-sunken ${crew ? 'px-4 py-3' : 'px-2.5 py-2'}`}
      >
        {/* Reason (from the blocker) and attribution (from the hold event) are
            rendered on their OWN terms — a bare hold has the second without the
            first, and that is the accidental case. */}
        {reasonKnown && (
          <>
            {reason && (
              <p
                className={`font-mono font-bold uppercase tracking-[0.06em] text-fd-amber ${
                  crew ? 'text-base' : 'text-[11px]'
                }`}
              >
                {reason}
              </p>
            )}
            {note && (
              <p data-testid="kiosk-held-note" className={`mt-1 text-fd-body ${crew ? 'text-lg' : 'text-[13px]'}`}>
                {note}
              </p>
            )}
            {/* A note was written but this screen is shared and unattended, so
                the server did not send it. Saying so is the point: silence here
                would read as "no reason given" and invite a Resume over
                somebody's real stop. */}
            {noteWithheld && (
              <p
                data-testid="kiosk-held-note-withheld"
                className={`mt-1 text-fd-mute ${crew ? 'text-lg' : 'text-[13px]'}`}
              >
                A written note was recorded — not shown on a shared station. Ask a supervisor before resuming.
              </p>
            )}
          </>
        )}

        {!reasonKnown && !unexplained && (
          <p
            data-testid="kiosk-held-no-blocker"
            className={`font-mono uppercase tracking-[0.06em] text-fd-mute ${crew ? 'text-base' : 'text-[11px]'}`}
          >
            No reason given
          </p>
        )}

        {attribution && (
          <p
            data-testid="kiosk-held-attribution"
            className={`font-mono text-fd-mute ${reasonKnown || !unexplained ? 'mt-1' : ''} ${
              crew ? 'text-sm' : 'text-[11px]'
            }`}
          >
            {attribution}
          </p>
        )}

        {unexplained && (
          <p
            data-testid="kiosk-held-no-reason"
            className={`font-mono uppercase tracking-[0.06em] text-fd-mute ${crew ? 'text-base' : 'text-[11px]'}`}
          >
            No hold reason recorded
          </p>
        )}
      </div>

      <button
        type="button"
        data-testid="kiosk-held-resume"
        disabled={disabled}
        aria-describedby={offlineHintId}
        // Normalized for the same reason the visible label above is: raw, a legacy
        // row announced "operation Op 10" beside a new row's "operation 10".
        aria-label={`Resume work order ${item.work_order_number}, operation ${
          item.operation_name || operationNumberText(item.operation_number) || ''
        }`}
        onClick={() => onResume(item)}
        className={`mt-2.5 w-full rounded-[4px] border border-fd-amber bg-fd-amber/15 font-mono font-bold uppercase tracking-[0.1em] text-fd-amber transition-transform duration-150 ease-out active:scale-[0.99] disabled:cursor-not-allowed disabled:opacity-40 ${
          crew ? 'min-h-16 text-xl' : 'min-h-11 text-[13px]'
        }`}
      >
        Resume
      </button>
    </div>
  );
}
