import React from 'react';
import KioskModal, { KioskModalClose } from './KioskModal';
import { KioskQueueItem } from './kioskConstants';
import { formatHoldAttribution, hasHoldReason, holdReasonLabel } from './heldOperations';

interface KioskResumeConfirmModalProps {
  item: KioskQueueItem;
  busy: boolean;
  online: boolean;
  offlineHintId?: string;
  onCancel: () => void;
  onConfirm: () => void;
  /**
   * Crew station: the confirm hands off to a badge-signature screen rather than
   * firing the PUT, so the CTA says so. The station token cannot mutate — only a
   * badge-minted operator token can — which makes the signature the audit actor.
   */
  confirmLabel?: string;
}

/**
 * RESUME confirm overlay — the deliberate pause between a tap and lifting a
 * hold somebody may have placed for a real reason.
 *
 * Built on KioskModal, NOT the shared <ConfirmDialog>: that one portals outside
 * `.fd-scope-kiosk` and would paint office-palette chrome onto a shop tablet.
 *
 * **Why there is no "this was a mistake — clear it" button here.** That is the
 * outcome an accidental hold actually wants (resolving the blocker resumes the
 * operation AND closes the record, leaving nothing diverging), and the copy
 * below exists because the kiosk cannot offer it. `POST
 * /work-order-blockers/{id}/resolve` is blocked from both kiosks by two
 * independent server gates: it requires ADMIN/MANAGER/SUPERVISOR (an OPERATOR
 * gets 403 on the single-operator kiosk, which runs on their own session), and
 * /api/v1/work-order-blockers sits outside KIOSK_TOKEN_PATH_PREFIXES, so a
 * badge-minted crew-station token is 403 there whatever role the badge holds.
 * A button that always 403s would be worse than none, so the kiosk resumes and
 * tells the operator plainly that the record is still open and who closes it.
 * Revisit this if a shop-floor-fenced resolve ever lands.
 */
export default function KioskResumeConfirmModal({
  item,
  busy,
  online,
  offlineHintId,
  onCancel,
  onConfirm,
  confirmLabel = 'Resume job',
}: KioskResumeConfirmModalProps) {
  const hold = item.hold;
  const blocker = hold?.blocker;
  const reason = holdReasonLabel(blocker?.category);
  const note = (blocker?.note || '').trim();
  const attribution = formatHoldAttribution(hold);
  const reasonKnown = hasHoldReason(hold);
  // A bare hold carries an attribution but no blocker, and that still earns the
  // warning panel: WHO stopped this job is exactly what separates a mis-tap from
  // a real stop somebody else placed.
  const showWarning = reasonKnown || attribution !== null;

  return (
    <KioskModal
      onClose={busy ? () => undefined : onCancel}
      widthClassName="max-w-[620px]"
      topEdgeClassName="border-t-2 border-t-fd-amber"
      ariaLabelledBy="kiosk-resume-title"
    >
      <div className="flex items-center gap-3 border-b border-fd-line px-5 py-4">
        <h2 id="kiosk-resume-title" className="font-mono text-[13px] font-bold uppercase tracking-[0.1em] text-fd-amber">
          Resume job?
        </h2>
        <div className="flex-1" />
        <KioskModalClose onClose={onCancel} disabled={busy} />
      </div>

      <div className="flex flex-col gap-3.5 p-5">
        {/* What is being resumed — restated, so nobody lifts the wrong hold. */}
        <div className="rounded-[4px] border border-fd-line bg-fd-sunken px-4 py-3.5">
          <p data-testid="kiosk-resume-wo" className="font-mono text-2xl font-bold text-fd-ink">
            {item.work_order_number}
          </p>
          <p className="mt-1 text-lg text-fd-body">
            <span className="font-mono font-semibold text-fd-ink">{item.part_number || '—'}</span>
            {item.part_name ? <span className="text-fd-mute"> · {item.part_name}</span> : null}
          </p>
          <p className="mt-0.5 text-base text-fd-mute">
            Op {item.operation_number ?? '—'}
            {item.operation_name ? ` · ${item.operation_name}` : ''}
          </p>
        </div>

        {/* Three cases, and the copy differs because the CONSEQUENCE differs.
            With a blocker, resuming leaves it open and somebody has to clear it.
            A BARE hold files no blocker at all, so nothing is left behind —
            telling that operator to chase a supervisor would send them after a
            record that does not exist. */}
        {showWarning ? (
          <div
            data-testid="kiosk-resume-blocker-warning"
            className="rounded-[4px] border border-fd-amber/40 bg-fd-amber/5 px-4 py-3.5"
          >
            <p className="font-mono text-[11px] font-bold uppercase tracking-[0.12em] text-fd-amber">
              {reasonKnown ? 'This hold stays recorded' : 'Who stopped this job'}
            </p>
            {reason && <p className="mt-1.5 text-lg font-semibold text-fd-ink">{reason}</p>}
            {note && <p className="mt-1 text-base text-fd-body">{note}</p>}
            {attribution && <p className="mt-1 font-mono text-sm text-fd-mute">{attribution}</p>}
            {reasonKnown ? (
              <>
                <p className="mt-2.5 text-base text-fd-body">
                  The job starts running again, but this stays on the supervisor&apos;s list until someone clears it.
                </p>
                <p className="mt-1.5 text-base text-fd-body">
                  Tapped hold by mistake? Resume it, then ask a supervisor to clear the hold — it will not clear
                  itself.
                </p>
              </>
            ) : (
              <p data-testid="kiosk-resume-bare-hold" className="mt-2.5 text-base text-fd-body">
                No reason was filed with this hold, so there is nothing left open. Resuming starts the job again.
              </p>
            )}
          </div>
        ) : (
          <p data-testid="kiosk-resume-no-reason" className="text-base text-fd-body">
            No hold reason was recorded for this operation. Resuming starts the job again.
          </p>
        )}
      </div>

      <div className="flex gap-2.5 px-5 pb-5">
        <button
          type="button"
          data-testid="kiosk-resume-cancel"
          onClick={onCancel}
          disabled={busy}
          className="h-[60px] w-[140px] rounded-[4px] border border-fd-line font-mono text-[13px] font-semibold uppercase tracking-[0.1em] text-fd-body transition-transform duration-150 ease-out active:scale-[0.98] disabled:opacity-40 sm:w-[170px]"
        >
          Cancel
        </button>
        <button
          type="button"
          data-testid="kiosk-resume-confirm"
          disabled={busy || !online}
          aria-describedby={!online ? offlineHintId : undefined}
          onClick={onConfirm}
          className="h-[60px] flex-1 rounded-[4px] bg-fd-amber font-mono text-[15px] font-bold uppercase tracking-[0.1em] text-[#171003] transition-transform duration-150 ease-out active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-40"
        >
          {!online ? 'Offline' : busy ? 'Resuming…' : confirmLabel}
        </button>
      </div>
    </KioskModal>
  );
}
