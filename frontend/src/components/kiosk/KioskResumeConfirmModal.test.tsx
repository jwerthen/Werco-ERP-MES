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
import { BARE_HOLD, HELD_ROW, UNRECORDED_HOLD, heldRowWith } from './heldOperationFixtures';


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
