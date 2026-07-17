/**
 * KioskCorrectionScreen — over-count correction (reduce-production) entry.
 *
 * The screen must:
 *  - keep confirm DISABLED until a positive quantity AND a reason are chosen
 *    (the digits-only keypad drives the quantity; there is no minus key);
 *  - emit (quantity, reasonLabel) verbatim so the caller can submit the
 *    reduce-production correction;
 *  - stay non-optimistic — nothing here mutates a count;
 *  - render a caller-supplied server refusal INLINE as a prominent role="alert"
 *    (production feedback: a toast alone was unreadable on shop displays).
 */
import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import KioskCorrectionScreen from './KioskCorrectionScreen';

function renderScreen(onConfirm = jest.fn(), busy = false, error: string | null = null) {
  render(
    <KioskCorrectionScreen
      jobLabel="WO-1 · Op 10 Deburr"
      busy={busy}
      error={error}
      onConfirm={onConfirm}
      onCancel={jest.fn()}
    />
  );
  return onConfirm;
}

it('keeps confirm disabled until both a quantity and a reason are entered', () => {
  renderScreen();

  // Nothing entered → disabled.
  expect(screen.getByTestId('kiosk-correct-confirm')).toBeDisabled();

  // Quantity only (2) → still disabled, and the reason prompt shows.
  fireEvent.click(screen.getByTestId('kiosk-correct-key-2'));
  expect(screen.getByTestId('kiosk-correct-confirm')).toBeDisabled();
  expect(screen.getByText(/choose a reason to continue/i)).toBeInTheDocument();

  // Add a reason → enabled.
  fireEvent.click(screen.getByRole('button', { name: 'Double-counted' }));
  expect(screen.getByTestId('kiosk-correct-confirm')).toBeEnabled();
});

it('emits the entered quantity and the chosen reason label', () => {
  const onConfirm = renderScreen();

  fireEvent.click(screen.getByTestId('kiosk-correct-key-3'));
  fireEvent.click(screen.getByRole('button', { name: 'Scanned twice' }));
  fireEvent.click(screen.getByTestId('kiosk-correct-confirm'));

  expect(onConfirm).toHaveBeenCalledWith(3, 'Scanned twice');
});

it('has no minus key on the keypad (removal quantity is always positive)', () => {
  renderScreen();
  expect(screen.queryByRole('button', { name: '-' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'minus' })).not.toBeInTheDocument();
});

it('hard-disables the keypad and confirm while busy', () => {
  const onConfirm = renderScreen(jest.fn(), true);
  // Keypad taps are ignored while busy, so no quantity accrues and confirm stays off.
  fireEvent.click(screen.getByTestId('kiosk-correct-key-5'));
  expect(screen.getByTestId('kiosk-correct-confirm')).toBeDisabled();
  fireEvent.click(screen.getByTestId('kiosk-correct-confirm'));
  expect(onConfirm).not.toHaveBeenCalled();
});

it('renders a server refusal INLINE as a prominent role="alert", verbatim', () => {
  const refusal = "Completed work can't be corrected here -- ask a supervisor";
  renderScreen(jest.fn(), false, refusal);

  const alert = screen.getByTestId('kiosk-correct-error');
  expect(alert).toHaveTextContent(refusal);
  expect(alert).toHaveAttribute('role', 'alert');
});

it('renders no error region when there is no refusal', () => {
  renderScreen();
  expect(screen.queryByTestId('kiosk-correct-error')).not.toBeInTheDocument();
});
