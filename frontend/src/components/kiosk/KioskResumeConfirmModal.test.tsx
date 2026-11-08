/**
 * KioskResumeConfirmModal — the pause before lifting somebody's hold.
 *
 * Pinned here: the overlay restates WHICH job is being resumed (so nobody lifts
 * the wrong hold from a board of similar rows), and it states the consequence
 * the backend actually produces — the operation restarts while the blocker
 * record stays OPEN. That second half is why the modal exists; resuming does
 * not resolve the blocker, and the kiosk cannot resolve it either (both the
 * role gate and the kiosk path fence refuse), so the operator has to be told
 * the record does not clean itself up.
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import KioskResumeConfirmModal from './KioskResumeConfirmModal';
import {
  BARE_HOLD,
  HELD_ROW,
  HELD_ROW_ON_HELD_WORK_ORDER,
  HOLD_WITH_BLOCKER_STATION,
  UNRECORDED_HOLD,
  WORK_ORDER_ON_HOLD_REASON,
  heldRowWith,
} from './heldOperationFixtures';


function renderModal(overrides: Partial<React.ComponentProps<typeof KioskResumeConfirmModal>> = {}) {
  const props = {
    item: HELD_ROW,
    busy: false,
    online: true,
    offlineHintId: 'offline-hint',
    onCancel: jest.fn(),
    onConfirm: jest.fn(),
    ...overrides,
  };
  render(<KioskResumeConfirmModal {...props} />);
  return props;
}

describe('KioskResumeConfirmModal', () => {
  it('clears an ordinary noted hold without sending the operator to resolve a blocker', () => {
    renderModal({ item: heldRowWith({
      ...BARE_HOLD, category: 'material_shortage', severity: 'medium', note: 'Waiting for next sheet',
    }) });

    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveTextContent('Material shortage');
    expect(dialog).toHaveTextContent('Waiting for next sheet');
    expect(dialog).toHaveTextContent('Resuming clears this hold. There is no blocker left to resolve.');
    expect(dialog).not.toHaveTextContent(/supervisor|stays recorded|will not clear itself/i);
  });

  it('restates exactly which job is being resumed', () => {
    renderModal();

    expect(screen.getByTestId('kiosk-resume-wo')).toHaveTextContent('WO-HELD-0001');
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveTextContent('PN-1');
    expect(dialog).toHaveTextContent('Deburr');
    expect(dialog).toHaveTextContent('Op 20');
  });

  it('says the hold stays recorded, and names the reason it is leaving open', () => {
    renderModal();

    const warning = screen.getByTestId('kiosk-resume-blocker-warning');
    expect(warning).toHaveTextContent(/stays recorded/i);
    // Read out of the NESTED hold.blocker the server sends, not a flat field.
    expect(warning).toHaveTextContent('Machine down');
    expect(warning).toHaveTextContent('Z-axis alarm 4012');
    expect(warning).toHaveTextContent('Held by Dana R.');
    // The consequence, in floor language: it lands on someone's list.
    expect(warning).toHaveTextContent(/supervisor/i);
  });

  it('withholds the note at the decision point too, and says one was recorded', () => {
    // The crew station reaches this modal BEFORE the badge scan, on a shared,
    // unattended tablet — so the server never sent the text and the modal must
    // not imply the hold was reasonless.
    renderModal({ item: heldRowWith(HOLD_WITH_BLOCKER_STATION) });

    const warning = screen.getByTestId('kiosk-resume-blocker-warning');
    expect(warning).not.toHaveTextContent('Z-axis alarm 4012');
    expect(warning).not.toHaveTextContent('Machine Down: Deburr');
    expect(screen.getByTestId('kiosk-resume-note-withheld')).toHaveTextContent(/written note was recorded/i);
    // Still enough to tell a deliberate hold from a mis-tap.
    expect(warning).toHaveTextContent('Machine down');
    expect(warning).toHaveTextContent('Held by Dana R.');
  });

  it('tells a BARE-hold operator that nothing is left open, and does NOT send them to a supervisor', () => {
    // A bare hold files no blocker, so resuming leaves nothing behind. The
    // "ask a supervisor to clear it" copy would send them after a record that
    // does not exist.
    renderModal({ item: heldRowWith(BARE_HOLD) });

    const warning = screen.getByTestId('kiosk-resume-blocker-warning');
    expect(warning).toHaveTextContent('Held by Dana R.');
    expect(screen.getByTestId('kiosk-resume-bare-hold')).toHaveTextContent(/nothing left open/i);
    expect(warning).not.toHaveTextContent(/will not clear itself/i);
  });

  it('tells an operator who mis-tapped how the record actually gets cleared', () => {
    renderModal();
    // The kiosk cannot resolve a blocker (role gate + path fence), so the only
    // honest instruction is to hand it to someone who can.
    expect(screen.getByTestId('kiosk-resume-blocker-warning')).toHaveTextContent(/will not clear itself/i);
  });

  it('does not claim a hold reason when the payload recorded neither reason nor holder', () => {
    renderModal({ item: heldRowWith(UNRECORDED_HOLD) });

    expect(screen.queryByTestId('kiosk-resume-blocker-warning')).not.toBeInTheDocument();
    expect(screen.getByTestId('kiosk-resume-no-reason')).toHaveTextContent(/no hold reason was recorded/i);
  });

  it('confirms and cancels through the host — it never calls the API itself', async () => {
    const props = renderModal();

    await userEvent.click(screen.getByTestId('kiosk-resume-confirm'));
    expect(props.onConfirm).toHaveBeenCalledTimes(1);

    await userEvent.click(screen.getByTestId('kiosk-resume-cancel'));
    expect(props.onCancel).toHaveBeenCalledTimes(1);
  });

  it('shows an in-flight state and blocks a double tap while busy', async () => {
    const props = renderModal({ busy: true });

    const confirm = screen.getByTestId('kiosk-resume-confirm');
    expect(confirm).toBeDisabled();
    expect(confirm).toHaveTextContent(/resuming/i);
    await userEvent.click(confirm);
    expect(props.onConfirm).not.toHaveBeenCalled();
    // Cancel is disabled too: an in-flight server-gated verb must not have its
    // overlay yanked out from under the response.
    expect(screen.getByTestId('kiosk-resume-cancel')).toBeDisabled();
  });

  it('refuses to resume while offline, and points at the offline banner', () => {
    renderModal({ online: false });

    const confirm = screen.getByTestId('kiosk-resume-confirm');
    expect(confirm).toBeDisabled();
    expect(confirm).toHaveTextContent(/offline/i);
    expect(confirm).toHaveAttribute('aria-describedby', 'offline-hint');
  });

  it('lets the crew station relabel the CTA for its badge-signature hand-off', () => {
    renderModal({ confirmLabel: 'Continue — scan badge' });
    expect(screen.getByTestId('kiosk-resume-confirm')).toHaveTextContent('Continue — scan badge');
  });
});

/**
 * Resume lifts the OPERATION's hold. When the row carries the WORK ORDER's start
 * refusal, the job will not run after the tap either, so the overlay says so
 * BEFORE it — and stops promising that the job "starts again".
 */
describe('KioskResumeConfirmModal — the work order still refuses a start', () => {
  const onHeldWorkOrder = (hold: Parameters<typeof heldRowWith>[0]) => ({
    ...heldRowWith(hold),
    work_order_status: 'on_hold',
    start_blocked_reason: WORK_ORDER_ON_HOLD_REASON,
  });

  it('shows the WO ON HOLD chip and the server reason verbatim, and points the CTA at it', () => {
    renderModal({ item: HELD_ROW_ON_HELD_WORK_ORDER });

    const panel = screen.getByTestId('kiosk-resume-start-blocked');
    expect(panel).toHaveTextContent('Lifts the operation hold only');
    expect(panel).toHaveTextContent(WORK_ORDER_ON_HOLD_REASON);
    expect(screen.getByTestId('kiosk-start-blocked-chip')).toHaveTextContent('WO on hold');
    // The tap is still allowed — lifting the operation hold is not refused.
    const confirm = screen.getByTestId('kiosk-resume-confirm');
    expect(confirm).toBeEnabled();
    expect(confirm).toHaveAttribute('aria-describedby', 'kiosk-resume-start-blocked');
  });

  it('never promises the job "starts running again" under a held work order', () => {
    renderModal({ item: HELD_ROW_ON_HELD_WORK_ORDER });
    const dialog = screen.getByRole('dialog');
    expect(dialog).not.toHaveTextContent(/starts running again|starts the job again/i);
    // The blocker half of the warning is unchanged: the record stays open.
    expect(screen.getByTestId('kiosk-resume-blocker-warning')).toHaveTextContent(
      "The operation hold lifts, but this stays on the supervisor's list until someone clears it."
    );
  });

  it('drops the "starts again" sentence from the bare and unrecorded cases too', () => {
    const { unmount } = render(
      <KioskResumeConfirmModal
        item={onHeldWorkOrder({ ...BARE_HOLD, held_by_name: 'Dana R.' })}
        busy={false}
        online
        onCancel={jest.fn()}
        onConfirm={jest.fn()}
      />
    );
    expect(screen.getByTestId('kiosk-resume-bare-hold')).toHaveTextContent(
      /^No reason was filed with this hold, so there is nothing left open\.$/
    );
    unmount();

    renderModal({ item: onHeldWorkOrder(UNRECORDED_HOLD) });
    expect(screen.getByTestId('kiosk-resume-no-reason')).toHaveTextContent(
      /^No hold reason was recorded for this operation\.$/
    );
  });

  it('renders exactly as before on a released job', () => {
    renderModal({ item: { ...HELD_ROW, work_order_status: 'released', start_blocked_reason: null } });
    expect(screen.queryByTestId('kiosk-resume-start-blocked')).not.toBeInTheDocument();
    expect(screen.queryByTestId('kiosk-start-blocked-chip')).not.toBeInTheDocument();
    expect(screen.getByTestId('kiosk-resume-blocker-warning')).toHaveTextContent(/starts running again/i);
    expect(screen.getByTestId('kiosk-resume-confirm')).not.toHaveAttribute('aria-describedby');
  });
});
