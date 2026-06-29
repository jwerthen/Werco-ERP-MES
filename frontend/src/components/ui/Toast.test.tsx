/**
 * Toast — the app-wide notification provider.
 *
 * Covers the accessibility contract a screen reader depends on: the container is
 * a polite, non-atomic live region; error toasts announce assertively via
 * role="alert" while success/info announce politely via role="status"; and the
 * dismiss control is labelled for assistive tech.
 */

import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { ToastProvider, useToast } from './Toast';

// A tiny consumer that fires a toast of a given type/message on click, so each
// test can drive the provider through its real public API.
function ToastHarness({ type, message }: { type: 'success' | 'error' | 'info'; message: string }) {
  const { showToast } = useToast();
  return (
    <button type="button" onClick={() => showToast(type, message)}>
      fire
    </button>
  );
}

function fireToast(type: 'success' | 'error' | 'info', message: string) {
  render(
    <ToastProvider>
      <ToastHarness type={type} message={message} />
    </ToastProvider>,
  );
  fireEvent.click(screen.getByRole('button', { name: 'fire' }));
}

describe('Toast a11y', () => {
  it('renders the toast container as a polite, non-atomic live region', () => {
    fireToast('info', 'Heads up');
    const region = document.querySelector('[aria-live]');
    expect(region).not.toBeNull();
    expect(region).toHaveAttribute('aria-live', 'polite');
    expect(region).toHaveAttribute('aria-atomic', 'false');
  });

  it('gives an error toast role="alert" so it is announced assertively', () => {
    fireToast('error', 'Save failed');
    const toast = screen.getByText('Save failed').closest('[role]');
    expect(toast).toHaveAttribute('role', 'alert');
  });

  it('gives a success toast role="status" so it is announced politely', () => {
    fireToast('success', 'Saved');
    const toast = screen.getByText('Saved').closest('[role]');
    expect(toast).toHaveAttribute('role', 'status');
  });

  it('gives an info toast role="status" so it is announced politely', () => {
    fireToast('info', 'FYI');
    const toast = screen.getByText('FYI').closest('[role]');
    expect(toast).toHaveAttribute('role', 'status');
  });

  it('labels the dismiss button for assistive tech and dismisses on click', () => {
    fireToast('info', 'Dismiss me');
    const dismiss = screen.getByRole('button', { name: 'Dismiss notification' });
    expect(dismiss).toBeInTheDocument();

    fireEvent.click(dismiss);
    expect(screen.queryByText('Dismiss me')).not.toBeInTheDocument();
  });
});
