/**
 * KioskFloorPinOverlay — the floor-PIN pad over a kiosk screen (solo kiosk sign-in and
 * every crew-station scan), and the solo kiosk's use of it through KioskBadgeLogin.
 *
 * The overlay can open ON TOP of a KioskModal (the crew station's COMPLETE signature is a
 * badge scan inside one), so it owns Escape and Tab in the window CAPTURE phase: they must
 * never reach a listener underneath. Pinned here: it is a labelled modal dialog portaled to
 * <body>; Escape / scrim tap cancel, except while a PIN is with the server; Tab is trapped;
 * digits pass through; focus returns where it was.
 *
 * KioskBadgeLogin: a badge with a PIN draws the overlay, the PIN is resent with the same
 * badge, Cancel is "back to the scan" (no error), and a pad left alone for a minute cancels
 * itself so an abandoned pad cannot hold the station (the badge reader is off under it).
 * The crew-station twin (its 90 s idle flow reset) is in pages/CrewStationKiosk.floorPin.test.tsx.
 */

import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import KioskFloorPinOverlay from './KioskFloorPinOverlay';
import KioskBadgeLogin from './KioskBadgeLogin';
import type { FloorPinPromptState } from '../../hooks/useFloorPinPrompt';

const OPEN: FloorPinPromptState = { error: null, verifying: false, attempt: 1 };

function renderOverlay(prompt: FloorPinPromptState | null = OPEN, badgeLabel: string | null = '4217') {
  const onSubmit = jest.fn();
  const onCancel = jest.fn();
  const utils = render(
    <KioskFloorPinOverlay prompt={prompt} onSubmit={onSubmit} onCancel={onCancel} badgeLabel={badgeLabel} />
  );
  return { ...utils, onSubmit, onCancel };
}

const dialog = () => screen.getByTestId('kiosk-floor-pin-dialog');
const pinInput = () => screen.getByTestId('floor-pin-input') as HTMLInputElement;

describe('KioskFloorPinOverlay', () => {
  it('renders nothing without an open challenge', () => {
    renderOverlay(null);
    expect(screen.queryByTestId('kiosk-floor-pin-dialog')).not.toBeInTheDocument();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('is a labelled modal dialog portaled to the body, in kiosk scope', () => {
    const { container } = renderOverlay();

    const shown = screen.getByRole('dialog', { name: 'Enter your PIN' });
    expect(shown).toBe(dialog());
    expect(shown).toHaveAttribute('aria-modal', 'true');
    expect(container).not.toContainElement(shown);
    const scrim = shown.parentElement as HTMLElement;
    expect(scrim).toHaveClass('fd-scope-kiosk');
    expect(scrim.parentElement).toBe(document.body);
    expect(screen.getByText('4217')).toBeInTheDocument();
  });

  it('submits the keypad PIN with its leading zero', () => {
    const { onSubmit } = renderOverlay();

    ['0', '9', '1', '2'].forEach(k => fireEvent.click(screen.getByTestId(`floor-pin-key-${k}`)));
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));

    expect(onSubmit).toHaveBeenCalledWith('0912');
  });

  it('Escape cancels and never reaches a listener underneath (a KioskModal below)', () => {
    const underneath = jest.fn();
    window.addEventListener('keydown', underneath);
    try {
      const { onCancel } = renderOverlay();

      fireEvent.keyDown(pinInput(), { key: 'Escape' });

      expect(onCancel).toHaveBeenCalledTimes(1);
      expect(underneath).not.toHaveBeenCalled();
    } finally {
      window.removeEventListener('keydown', underneath);
    }
  });

  it('Escape while a PIN is with the server does nothing -- and still does not leak', () => {
    const underneath = jest.fn();
    window.addEventListener('keydown', underneath);
    try {
      const { onCancel } = renderOverlay({ ...OPEN, verifying: true });

      fireEvent.keyDown(document.body, { key: 'Escape' });

      expect(onCancel).not.toHaveBeenCalled();
      expect(underneath).not.toHaveBeenCalled();
    } finally {
      window.removeEventListener('keydown', underneath);
    }
  });

  it('digits and Enter pass through (the idle timer and the field still see them)', () => {
    const underneath = jest.fn();
    window.addEventListener('keydown', underneath);
    try {
      renderOverlay();
      fireEvent.keyDown(pinInput(), { key: '5' });
      fireEvent.keyDown(pinInput(), { key: 'Enter' });
      expect(underneath).toHaveBeenCalledTimes(2);
    } finally {
      window.removeEventListener('keydown', underneath);
    }
  });

  it('a scrim tap cancels; a tap inside the panel does not; nothing dismisses it while verifying', () => {
    const { onCancel, rerender, onSubmit } = renderOverlay();
    const scrim = dialog().parentElement as HTMLElement;

    fireEvent.click(dialog());
    expect(onCancel).not.toHaveBeenCalled();
    fireEvent.click(scrim);
    expect(onCancel).toHaveBeenCalledTimes(1);

    rerender(
      <KioskFloorPinOverlay
        prompt={{ ...OPEN, verifying: true }}
        onSubmit={onSubmit}
        onCancel={onCancel}
        badgeLabel="4217"
      />
    );
    fireEvent.click(dialog().parentElement as HTMLElement);
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it('traps Tab inside the panel in both directions', () => {
    renderOverlay();
    const cancel = screen.getByRole('button', { name: 'Cancel' });

    cancel.focus();
    fireEvent.keyDown(cancel, { key: 'Tab' });
    expect(document.activeElement).toBe(pinInput());

    fireEvent.keyDown(pinInput(), { key: 'Tab', shiftKey: true });
    expect(document.activeElement).toBe(cancel);
  });

  // The overlay records focus in a layout effect, before the pad's own mount effect
  // focuses the PIN field, so closing returns focus to the element the operator was on.
  it('returns focus to where it was when the challenge closes', () => {
    const outside = document.createElement('button');
    outside.textContent = 'Scan';
    document.body.appendChild(outside);
    try {
      outside.focus();
      const { rerender, onSubmit, onCancel } = renderOverlay(null);
      rerender(<KioskFloorPinOverlay prompt={OPEN} onSubmit={onSubmit} onCancel={onCancel} />);
      expect(document.activeElement).toBe(pinInput());

      rerender(<KioskFloorPinOverlay prompt={null} onSubmit={onSubmit} onCancel={onCancel} />);

      expect(document.activeElement).toBe(outside);
    } finally {
      outside.remove();
    }
  });

  it('shows the refusal from the previous attempt', () => {
    renderOverlay({ error: 'Invalid badge or PIN', verifying: false, attempt: 2 });
    expect(screen.getByRole('alert')).toHaveTextContent('Invalid badge or PIN');
  });
});

// ---------------------------------------------------------------------------
// The solo kiosk (KioskBadgeLogin) through the overlay
// ---------------------------------------------------------------------------
const CHALLENGE = {
  response: { status: 401, data: { detail: { code: 'floor_pin_required', message: 'Enter your PIN.' } } },
};
const WRONG = { response: { status: 401, data: { detail: 'Invalid badge or PIN' } } };
const NOT_SET = {
  response: {
    status: 403,
    data: {
      detail: { code: 'floor_pin_not_set', message: 'No PIN is set for this badge. Ask a supervisor to set one.' },
    },
  },
};

function scan(badge: string) {
  badge.split('').forEach(k => fireEvent.keyDown(window, { key: k }));
  fireEvent.keyDown(window, { key: 'Enter' });
}

function typePin(pin: string) {
  pin.split('').forEach(k => fireEvent.click(screen.getByTestId(`floor-pin-key-${k}`)));
  fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
}

describe('KioskBadgeLogin with a floor PIN', () => {
  afterEach(() => {
    jest.useRealTimers();
  });

  it('draws the pad for a badge with a PIN and resends the same badge with it', async () => {
    const onLogin = jest.fn().mockRejectedValueOnce(CHALLENGE).mockResolvedValueOnce(undefined);
    render(<KioskBadgeLogin stationLabel="LASER1" onLogin={onLogin} />);

    scan('4217');
    expect(await screen.findByTestId('kiosk-floor-pin-dialog')).toBeInTheDocument();
    expect(onLogin.mock.calls[0]).toEqual(['4217']);

    typePin('0912');

    await waitFor(() => expect(onLogin).toHaveBeenCalledTimes(2));
    expect(onLogin.mock.calls[1]).toEqual(['4217', '0912']);
    await waitFor(() => expect(screen.queryByTestId('kiosk-floor-pin-dialog')).not.toBeInTheDocument());
  });

  it('a wrong PIN keeps the pad with the refusal and an empty field', async () => {
    const onLogin = jest
      .fn()
      .mockRejectedValueOnce(CHALLENGE)
      .mockRejectedValueOnce(WRONG)
      .mockResolvedValueOnce(undefined);
    render(<KioskBadgeLogin stationLabel="LASER1" onLogin={onLogin} />);

    scan('4217');
    await screen.findByTestId('kiosk-floor-pin-dialog');
    typePin('1357');

    expect(await screen.findByText('Invalid badge or PIN')).toBeInTheDocument();
    await waitFor(() => expect(pinInput().value).toBe(''));
    typePin('0912');
    await waitFor(() => expect(onLogin).toHaveBeenLastCalledWith('4217', '0912'));
  });

  it('Cancel is back to the badge scan, with no error shown', async () => {
    const onLogin = jest.fn().mockRejectedValueOnce(CHALLENGE);
    render(<KioskBadgeLogin stationLabel="LASER1" onLogin={onLogin} />);

    scan('4217');
    await screen.findByTestId('kiosk-floor-pin-dialog');
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));

    await waitFor(() => expect(screen.queryByTestId('kiosk-floor-pin-dialog')).not.toBeInTheDocument());
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(onLogin).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('kiosk-badge-display')).toHaveTextContent('Waiting for badge…');
  });

  it('a pad left alone for a minute cancels itself and frees the badge reader', async () => {
    jest.useFakeTimers();
    const onLogin = jest.fn().mockRejectedValueOnce(CHALLENGE).mockResolvedValueOnce(undefined);
    render(<KioskBadgeLogin stationLabel="LASER1" onLogin={onLogin} />);

    scan('4217');
    await screen.findByTestId('kiosk-floor-pin-dialog');

    await act(async () => {
      jest.advanceTimersByTime(59_000);
    });
    expect(screen.getByTestId('kiosk-floor-pin-dialog')).toBeInTheDocument();

    await act(async () => {
      jest.advanceTimersByTime(2_000);
    });

    expect(screen.queryByTestId('kiosk-floor-pin-dialog')).not.toBeInTheDocument();
    expect(onLogin).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();

    // The reader is live again: the next scan signs in.
    scan('5310');
    await waitFor(() => expect(onLogin).toHaveBeenLastCalledWith('5310'));
  });

  it('shows the not-set message, never the raw object', async () => {
    const onLogin = jest.fn().mockRejectedValueOnce(NOT_SET);
    render(<KioskBadgeLogin stationLabel="LASER1" onLogin={onLogin} />);

    scan('4217');

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'No PIN is set for this badge. Ask a supervisor to set one.'
    );
    expect(screen.queryByTestId('kiosk-floor-pin-dialog')).not.toBeInTheDocument();
    expect(screen.queryByText(/\[object/)).not.toBeInTheDocument();
  });
});
