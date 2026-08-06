/**
 * Toast — the app-wide notification provider.
 *
 * Covers the accessibility contract a screen reader depends on: the container is
 * a polite, non-atomic live region; error AND warning toasts announce assertively
 * via role="alert" while success/info announce politely via role="status"; and the
 * dismiss control is labelled for assistive tech.
 *
 * `warning` is the newest variant, for an action that SUCCEEDED but did not do
 * everything asked — a partial result the user has to act on. It earns the
 * assertive announcement for the same reason an error does. Its first caller is a
 * duplicated work order that could not carry a material tie across, where
 * believing the copy was complete means releasing a job whose stock is never
 * deducted. The full type -> role mapping is pinned in one table below so adding a
 * fifth variant cannot quietly land as a silent `status`.
 */

import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { ToastProvider, useToast } from './Toast';

type ToastType = 'success' | 'error' | 'warning' | 'info';

// A tiny consumer that fires a toast of a given type/message on click, so each
// test can drive the provider through its real public API.
function ToastHarness({ type, message }: { type: ToastType; message: string }) {
  const { showToast } = useToast();
  return (
    <button type="button" onClick={() => showToast(type, message)}>
      fire
    </button>
  );
}

function fireToast(type: ToastType, message: string) {
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

  it('gives a warning toast role="alert" so a partial result is not announced quietly', () => {
    // The variant exists because the alternatives mislead: `success` hides the
    // shortfall, `error` claims a failure that did not happen. A warning that
    // waited politely for a pause would be back to hiding it.
    fireToast('warning', 'Not copied: 1 material tie');
    const toast = screen.getByText('Not copied: 1 material tie').closest('[role]');
    expect(toast).toHaveAttribute('role', 'alert');
  });

  it.each([
    ['success', 'status'],
    ['info', 'status'],
    ['error', 'alert'],
    ['warning', 'alert'],
  ] as const)('maps a %s toast to role="%s"', (type, role) => {
    // The whole table in one place: adding the warning variant must not have
    // moved any of the other three, and a fifth variant cannot land as a silent
    // `status` without this failing.
    fireToast(type, `${type} message`);
    expect(screen.getByText(`${type} message`).closest('[role]')).toHaveAttribute('role', role);
  });

  it('distinguishes a warning from an error visually, not only by role', () => {
    // Both are role="alert", so colour is the ONLY thing separating "this failed"
    // from "this worked but not completely" for a sighted user. Amber, not red.
    fireToast('warning', 'Partial');
    const warning = screen.getByText('Partial').closest('[role]');
    expect(warning).toHaveClass('bg-amber-600');
    expect(warning).not.toHaveClass('bg-red-600');
  });

  it('labels the dismiss button for assistive tech and dismisses on click', () => {
    fireToast('info', 'Dismiss me');
    const dismiss = screen.getByRole('button', { name: 'Dismiss notification' });
    expect(dismiss).toBeInTheDocument();

    fireEvent.click(dismiss);
    expect(screen.queryByText('Dismiss me')).not.toBeInTheDocument();
  });
});

// Regression: a FastAPI 422 `detail` array must never reach the DOM as a raw object.
// The toast list mounts above the router's error boundary, so rendering an array of
// {loc,msg} objects threw "Objects are not valid as a React child" and blanked the
// whole SPA. showToast now coerces via toDisplayString, so it can't.
describe('Toast crash-safety on non-string messages', () => {
  // Harness that intentionally passes a non-string (what a mis-normalized 422 handler did).
  function BadHarness({ message }: { message: unknown }) {
    const { showToast } = useToast();
    return (
      <button type="button" onClick={() => showToast('error', message as string)}>
        fire
      </button>
    );
  }

  function fireRaw(message: unknown) {
    render(
      <ToastProvider>
        <BadHarness message={message} />
      </ToastProvider>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'fire' }));
  }

  it('renders a raw 422 detail ARRAY as a readable joined string, without unmounting', () => {
    const detail = [
      { loc: ['body', 'required_date'], msg: 'Input should be a valid date' },
      { loc: ['body', 'lines', 0, 'part_id'], msg: 'Input should be greater than 0' },
    ];

    expect(() => fireRaw(detail)).not.toThrow();

    // The joined message renders (not "[object Object]" / not a crash)...
    expect(
      screen.getByText('required_date: Input should be a valid date; lines.0.part_id: Input should be greater than 0'),
    ).toBeInTheDocument();
    // ...and the harness (the app tree) is still mounted — no white-screen unmount.
    expect(screen.getByRole('button', { name: 'fire' })).toBeInTheDocument();
  });

  it('renders a bare error object by preferring its message field', () => {
    expect(() => fireRaw({ message: 'Something broke' })).not.toThrow();
    expect(screen.getByText('Something broke')).toBeInTheDocument();
  });
});
