/**
 * InputDialog — the shared single-field text-capture dialog that replaces the
 * app's native prompt() calls.
 *
 * Covers the contract its three call sites depend on: the FormField label is
 * programmatically associated with the input, `defaultValue` pre-fills (and
 * re-seeds on reopen), submit passes the TRIMMED value and never an empty one
 * (submit disables while the trimmed value is empty — the chosen empty-value
 * behavior), Enter inside the field submits, cancel never calls onSubmit, and
 * `pending` disables the footer and blocks Escape/backdrop dismissal exactly
 * like ConfirmDialog.
 */

import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { InputDialog } from './InputDialog';

function renderDialog(overrides: Partial<React.ComponentProps<typeof InputDialog>> = {}) {
  const onSubmit = jest.fn();
  const onCancel = jest.fn();
  const utils = render(
    <InputDialog
      open
      title="Resolve Blocker"
      message='Resolve blocker "Material missing"?'
      label="Resolution note"
      defaultValue="Resolved"
      submitLabel="Resolve"
      onSubmit={onSubmit}
      onCancel={onCancel}
      {...overrides}
    />,
  );
  return { onSubmit, onCancel, ...utils };
}

describe('InputDialog', () => {
  it('associates the label with the input and pre-fills the default value', () => {
    renderDialog();

    expect(screen.getByText('Resolve Blocker')).toBeInTheDocument();
    expect(screen.getByText('Resolve blocker "Material missing"?')).toBeInTheDocument();

    // FormField's render-prop wiring: the label resolves the control.
    const input = screen.getByLabelText(/resolution note/i);
    expect(input).toHaveValue('Resolved');
  });

  it('submit passes the trimmed value', () => {
    const { onSubmit } = renderDialog();

    const input = screen.getByLabelText(/resolution note/i);
    fireEvent.change(input, { target: { value: '  Vendor delivered  ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Resolve' }));

    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(onSubmit).toHaveBeenCalledWith('Vendor delivered');
  });

  it('pressing Enter inside the field submits', async () => {
    const user = userEvent.setup();
    const { onSubmit } = renderDialog();

    const input = screen.getByLabelText(/resolution note/i);
    await user.clear(input);
    await user.type(input, 'Fixed at the saw{Enter}');

    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(onSubmit).toHaveBeenCalledWith('Fixed at the saw');
  });

  it('disables submit while the trimmed value is empty — onSubmit never sees an empty string', () => {
    const { onSubmit } = renderDialog();

    const input = screen.getByLabelText(/resolution note/i);
    const submit = screen.getByRole('button', { name: 'Resolve' });

    fireEvent.change(input, { target: { value: '   ' } });
    expect(submit).toBeDisabled();

    // Even a direct form submit (Enter's code path) is guarded.
    fireEvent.submit(input.closest('form')!);
    expect(onSubmit).not.toHaveBeenCalled();

    // Typing a real value re-enables submit.
    fireEvent.change(input, { target: { value: 'ok' } });
    expect(submit).toBeEnabled();
  });

  it('cancel calls onCancel and never onSubmit', () => {
    const { onSubmit, onCancel } = renderDialog();

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('re-seeds the field from defaultValue when reopened', () => {
    const { rerender } = render(
      <InputDialog
        open
        title="t"
        label="Name"
        defaultValue="Initial"
        onSubmit={jest.fn()}
        onCancel={jest.fn()}
      />,
    );
    fireEvent.change(screen.getByLabelText(/name/i), { target: { value: 'edited away' } });

    rerender(
      <InputDialog open={false} title="t" label="Name" defaultValue="Initial" onSubmit={jest.fn()} onCancel={jest.fn()} />,
    );
    rerender(
      <InputDialog open title="t" label="Name" defaultValue="Initial" onSubmit={jest.fn()} onCancel={jest.fn()} />,
    );
    expect(screen.getByLabelText(/name/i)).toHaveValue('Initial');
  });

  it('pending disables the field and footer and shows the loading spinner', () => {
    const { onSubmit } = renderDialog({ pending: true });

    expect(screen.getByLabelText(/resolution note/i)).toBeDisabled();
    const submit = screen.getByRole('button', { name: /resolve/i });
    expect(submit).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeDisabled();
    expect(screen.getByRole('status', { name: 'Loading' })).toBeInTheDocument();

    fireEvent.submit(screen.getByRole('dialog').querySelector('form')!);
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('blocks Escape and backdrop dismissal while pending', () => {
    const { onCancel } = renderDialog({ pending: true });

    fireEvent.keyDown(window, { key: 'Escape' });
    fireEvent.click(screen.getByRole('dialog').parentElement!);
    expect(onCancel).not.toHaveBeenCalled();
  });

  it('renders nothing when closed', () => {
    renderDialog({ open: false });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });
});
