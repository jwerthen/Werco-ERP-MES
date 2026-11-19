/**
 * FloorPinPad — the floor-PIN entry shown when a badge sign-on answers
 * `floor_pin_required` (AS9100D 8.5.2). One component for both surfaces:
 * 'kiosk' (touch keypad) and 'desk' (keyboard field on the Login page).
 *
 * Behavior pinned: a PIN is a CODE (a leading zero is a digit, "0912" stays four
 * digits), 4–6 digits only (no letters, no 7th digit, nothing submitted short),
 * masked, held while the server checks it, cleared on every new ask; and the
 * accessibility contract: a labelled field, the refusal announced (role=alert)
 * and wired to the field via aria-invalid / aria-describedby. A badge SCAN is never a PIN:
 * a keystroke burst faster than a person types is thrown away, its Enter included, so a
 * colleague's badge scanned at the station can never be charged as a wrong PIN against the
 * person waiting at the pad.
 */

import React from 'react';
import { fireEvent, render, screen, within } from '@testing-library/react';
import FloorPinPad, { FloorPinPadProps, SCAN_DISCARDED_HINT } from './FloorPinPad';

function renderPad(overrides: Partial<FloorPinPadProps> = {}) {
  const props: FloorPinPadProps = {
    variant: 'kiosk',
    error: null,
    verifying: false,
    attempt: 1,
    onSubmit: jest.fn(),
    onCancel: jest.fn(),
    ...overrides,
  };
  const utils = render(<FloorPinPad {...props} />);
  return { ...utils, props };
}

const input = () => screen.getByTestId('floor-pin-input') as HTMLInputElement;
const key = (name: string) => screen.getByTestId(`floor-pin-key-${name}`);
const press = (...keys: string[]) => keys.forEach(k => fireEvent.click(key(k)));

describe('FloorPinPad (kiosk)', () => {
  it('keeps a leading zero and submits the four digits as typed on the keypad', () => {
    const { props } = renderPad();

    press('0', '9', '1', '2');

    expect(input().value).toBe('0912');
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    expect(props.onSubmit).toHaveBeenCalledWith('0912');
  });

  it('accepts up to six digits and ignores a seventh', () => {
    const { props } = renderPad();

    press('8', '3', '0', '4', '1', '6', '9');

    expect(input().value).toBe('830416');
    fireEvent.submit(input().closest('form') as HTMLFormElement);
    expect(props.onSubmit).toHaveBeenCalledWith('830416');
  });

  it('holds Sign in until four digits, and a short submit is refused on the pad, not sent', () => {
    const { props } = renderPad();

    press('1', '2', '3');
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeDisabled();
    fireEvent.submit(input().closest('form') as HTMLFormElement);

    expect(props.onSubmit).not.toHaveBeenCalled();
    expect(screen.getByRole('alert')).toHaveTextContent('Enter your PIN (4–6 digits).');
    expect(input()).toHaveAttribute('aria-invalid', 'true');

    press('4');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeEnabled();
  });

  it('a hardware keyboard into the field keeps digits only', () => {
    renderPad();

    fireEvent.change(input(), { target: { value: '0a9-1 2x' } });

    expect(input().value).toBe('0912');
  });

  it('Clear empties the PIN and Backspace removes the last digit', () => {
    renderPad();

    press('5', '2', '9', '0');
    press('back');
    expect(input().value).toBe('529');
    press('clear');
    expect(input().value).toBe('');
  });

  it('is masked, suppresses the soft keyboard and is labelled PIN', () => {
    renderPad();

    expect(input()).toHaveAttribute('type', 'password');
    expect(input()).toHaveAttribute('inputMode', 'none');
    expect(input()).toHaveAttribute('autoComplete', 'off');
    expect(screen.getByLabelText('PIN')).toBe(input());
    expect(screen.getByRole('heading', { name: 'Enter your PIN' })).toBeInTheDocument();
    expect(screen.getByRole('group', { name: 'Number pad' })).toBeInTheDocument();
  });

  it('announces the refusal and wires it to the field', () => {
    renderPad({ error: 'Invalid badge or PIN', badgeLabel: '4217' });

    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent('Invalid badge or PIN');
    expect(input()).toHaveAttribute('aria-invalid', 'true');
    expect(input().getAttribute('aria-describedby')?.split(' ')).toContain(alert.id);
    expect(screen.getByText('4217')).toBeInTheDocument();
  });

  it('while verifying, every key, Cancel and Sign in are held and the field is read-only', () => {
    const { props } = renderPad({ verifying: true });

    expect(input()).toHaveAttribute('readonly');
    for (const name of ['0', '1', '9', 'clear', 'back']) expect(key(name)).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Checking…' })).toBeDisabled();
    expect(screen.getByText('Checking PIN…')).toBeInTheDocument();
    expect(input().closest('form')).toHaveAttribute('aria-busy', 'true');

    fireEvent.change(input(), { target: { value: '1234' } });
    fireEvent.submit(input().closest('form') as HTMLFormElement);
    expect(input().value).toBe('');
    expect(props.onSubmit).not.toHaveBeenCalled();
  });

  it('a new ask clears the digits', () => {
    const { rerender, props } = renderPad();
    press('2', '5', '8', '0');

    rerender(<FloorPinPad {...props} attempt={2} error="Invalid badge or PIN" />);

    expect(input().value).toBe('');
    expect(screen.getByRole('alert')).toHaveTextContent('Invalid badge or PIN');
  });

  it('Cancel calls onCancel and never onSubmit', () => {
    const { props } = renderPad();
    press('2', '5', '8', '0');

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(props.onCancel).toHaveBeenCalledTimes(1);
    expect(props.onSubmit).not.toHaveBeenCalled();
  });
});

describe('FloorPinPad (desk)', () => {
  it('takes keyboard entry, keeps a leading zero and submits on Enter', () => {
    const { props } = renderPad({ variant: 'desk' });

    fireEvent.change(input(), { target: { value: '0912' } });
    fireEvent.submit(input().closest('form') as HTMLFormElement);

    expect(props.onSubmit).toHaveBeenCalledWith('0912');
    expect(input()).toHaveAttribute('inputMode', 'numeric');
    expect(input()).toHaveAttribute('type', 'password');
  });

  it('has no touch keypad, a labelled field and a Back button', () => {
    const { props } = renderPad({ variant: 'desk', badgeLabel: '0042' });

    expect(screen.queryByTestId('floor-pin-key-1')).not.toBeInTheDocument();
    expect(screen.getByLabelText('PIN')).toBe(input());
    expect(screen.getByText('0042')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Back' }));
    expect(props.onCancel).toHaveBeenCalledTimes(1);
  });

  it('refuses a short or long PIN client-side and truncates past six', () => {
    const { props } = renderPad({ variant: 'desk' });
    const form = input().closest('form') as HTMLFormElement;

    fireEvent.change(input(), { target: { value: '12' } });
    fireEvent.submit(form);
    expect(props.onSubmit).not.toHaveBeenCalled();
    expect(within(form).getByRole('alert')).toHaveTextContent('Enter your PIN (4–6 digits).');

    fireEvent.change(input(), { target: { value: '12345678' } });
    expect(input().value).toBe('123456');
  });

  it('announces the server refusal', () => {
    renderPad({ variant: 'desk', error: 'Invalid badge or PIN' });
    expect(screen.getByRole('alert')).toHaveTextContent('Invalid badge or PIN');
    expect(input()).toHaveAttribute('aria-invalid', 'true');
  });
});

describe('FloorPinPad scan-burst guard', () => {
  let now = 0;
  beforeEach(() => {
    now = 1_000_000;
    jest.spyOn(Date, 'now').mockImplementation(() => now);
  });
  afterEach(() => jest.restoreAllMocks());

  /** One keystroke on the field `gapMs` after the previous one; true when the pad let it through. */
  const keyAt = (key: string, gapMs: number) => {
    now += gapMs;
    return fireEvent.keyDown(input(), { key });
  };

  it.each(['kiosk', 'desk'] as const)(
    'a wedge-scanner burst into the %s field is discarded, Enter included',
    variant => {
      const { props } = renderPad({ variant });
      fireEvent.change(input(), { target: { value: '42' } });

      const passed = ['4', '2', '1', '7'].map(k => keyAt(k, 5));
      const enterPassed = keyAt('Enter', 5);

      expect(passed.slice(0, 3)).toEqual([true, true, true]);
      expect(passed[3]).toBe(false);
      expect(enterPassed).toBe(false);
      expect(input().value).toBe('');
      expect(screen.getByRole('alert')).toHaveTextContent(SCAN_DISCARDED_HINT[variant]);
      expect(props.onSubmit).not.toHaveBeenCalled();
    }
  );

  it('a person typing at human speed is never discarded', () => {
    renderPad();

    const passed = ['0', '9', '1', '2', 'Enter'].map(k => keyAt(k, 180));

    expect(passed).toEqual([true, true, true, true, true]);
    expect(screen.queryByText(SCAN_DISCARDED_HINT.kiosk)).not.toBeInTheDocument();
  });

  it('after a discarded burst, the person types the PIN and it is submitted', () => {
    const { props } = renderPad();
    ['4', '2', '1', '7', 'Enter'].forEach(k => keyAt(k, 5));
    expect(screen.getByRole('alert')).toHaveTextContent(SCAN_DISCARDED_HINT.kiosk);

    now += 1_000;
    ['0', '9', '1', '2'].forEach(k => fireEvent.click(key(k)));

    expect(screen.queryByText(SCAN_DISCARDED_HINT.kiosk)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    expect(props.onSubmit).toHaveBeenCalledWith('0912');
  });
});

describe('FloorPinPad — a sign-on the server ended (final, the 429 PIN throttle)', () => {
  const THROTTLED = 'Too many failed sign-in attempts — wait a few minutes';

  it.each(['kiosk', 'desk'] as const)('%s: shows the refusal, takes no PIN, and offers only Back', variant => {
    const { props } = renderPad({ variant, final: true, error: THROTTLED });

    expect(screen.getByRole('alert')).toHaveTextContent(THROTTLED);
    expect(input()).toHaveAttribute('readonly');
    fireEvent.change(input(), { target: { value: '0912' } });
    expect(input().value).toBe('');
    expect(screen.queryByRole('button', { name: /sign in/i })).not.toBeInTheDocument();
    fireEvent.submit(input().closest('form') as HTMLFormElement);
    expect(props.onSubmit).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Back' }));
    expect(props.onCancel).toHaveBeenCalledTimes(1);
  });

  it('kiosk: the keypad is held too', () => {
    renderPad({ final: true, error: THROTTLED });
    expect(key('5')).toBeDisabled();
  });
});
