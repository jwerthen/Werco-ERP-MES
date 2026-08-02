/**
 * ConfirmDialog — the shared confirm/cancel dialog primitive.
 *
 * Covers the `pending` in-flight state added for server-gated confirms: both
 * footer buttons disable, the confirm button shows the LoadingButton spinner,
 * and Escape/backdrop dismissal is refused so an action already on the wire
 * can't be visually "cancelled". Also pins the variant → confirm-button
 * styling mapping (danger/info ride the LoadingButton variants; warning keeps
 * its amber utility override) and the fully-backward-compatible render without
 * `pending` that every pre-existing caller relies on.
 */

import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { ConfirmDialog } from './ConfirmDialog';

function renderDialog(overrides: Partial<React.ComponentProps<typeof ConfirmDialog>> = {}) {
  const onConfirm = jest.fn();
  const onCancel = jest.fn();
  const utils = render(
    <ConfirmDialog
      open
      title="Delete Purchase Order"
      message="Delete purchase order PO-001?"
      confirmLabel="Delete"
      onConfirm={onConfirm}
      onCancel={onCancel}
      {...overrides}
    />,
  );
  return { onConfirm, onCancel, ...utils };
}

describe('ConfirmDialog', () => {
  it('backward-compat: renders title/message and fires the callbacks without pending', () => {
    const { onConfirm, onCancel } = renderDialog();

    expect(screen.getByText('Delete Purchase Order')).toBeInTheDocument();
    expect(screen.getByText('Delete purchase order PO-001?')).toBeInTheDocument();

    const confirm = screen.getByRole('button', { name: 'Delete' });
    const cancel = screen.getByRole('button', { name: 'Cancel' });
    expect(confirm).toBeEnabled();
    expect(cancel).toBeEnabled();

    fireEvent.click(confirm);
    expect(onConfirm).toHaveBeenCalledTimes(1);
    fireEvent.click(cancel);
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it('closes via Escape and backdrop when not pending (Modal defaults preserved)', () => {
    const { onCancel } = renderDialog();

    fireEvent.keyDown(window, { key: 'Escape' });
    expect(onCancel).toHaveBeenCalledTimes(1);

    // The overlay is the dialog panel's parent.
    fireEvent.click(screen.getByRole('dialog').parentElement!);
    expect(onCancel).toHaveBeenCalledTimes(2);
  });

  it('pending disables both buttons and shows the loading spinner on confirm', () => {
    const { onConfirm } = renderDialog({ pending: true });

    const confirm = screen.getByRole('button', { name: /delete/i });
    const cancel = screen.getByRole('button', { name: 'Cancel' });
    expect(confirm).toBeDisabled();
    expect(cancel).toBeDisabled();

    // LoadingButton renders its Spinner (role="status") while loading.
    expect(screen.getByRole('status', { name: 'Loading' })).toBeInTheDocument();

    // A disabled confirm can't re-fire the action.
    fireEvent.click(confirm);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it('blocks Escape and backdrop dismissal while pending', () => {
    const { onCancel } = renderDialog({ pending: true });

    fireEvent.keyDown(window, { key: 'Escape' });
    expect(onCancel).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('dialog').parentElement!);
    expect(onCancel).not.toHaveBeenCalled();
  });

  it('maps variants onto the confirm button: danger → LoadingButton danger classes', () => {
    renderDialog({ variant: 'danger' });
    expect(screen.getByRole('button', { name: 'Delete' })).toHaveClass('bg-red-600');
  });

  it('maps variants onto the confirm button: info → btn-primary', () => {
    renderDialog({ variant: 'info', confirmLabel: 'Confirm' });
    expect(screen.getByRole('button', { name: 'Confirm' })).toHaveClass('btn-primary');
  });

  it('maps variants onto the confirm button: warning keeps the amber override', () => {
    renderDialog({ variant: 'warning', confirmLabel: 'Obsolete' });
    const confirm = screen.getByRole('button', { name: 'Obsolete' });
    // Amber utilities override the underlying btn-primary chrome (utilities
    // layer beats the components layer, so this is deterministic), including
    // suppressing .btn-primary:hover's blue ring/glow box-shadow.
    expect(confirm).toHaveClass('bg-amber-500', 'hover:bg-amber-600', 'hover:shadow-none', 'text-white');
  });

  it('renders nothing when closed', () => {
    renderDialog({ open: false });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });
});
