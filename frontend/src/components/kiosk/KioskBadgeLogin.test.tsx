import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import KioskBadgeLogin from './KioskBadgeLogin';

describe('KioskBadgeLogin', () => {
  it('captures wedge-scanner keystrokes at window level and submits on Enter', async () => {
    const onLogin = jest.fn().mockResolvedValue(undefined);
    render(<KioskBadgeLogin stationLabel="LASER1" onLogin={onLogin} />);

    // A keyboard-wedge badge scanner "types" the id followed by Enter,
    // with no input focused.
    fireEvent.keyDown(window, { key: '4' });
    fireEvent.keyDown(window, { key: '2' });
    fireEvent.keyDown(window, { key: '1' });
    fireEvent.keyDown(window, { key: '7' });
    expect(screen.getByTestId('kiosk-badge-display')).toHaveTextContent('4217');

    fireEvent.keyDown(window, { key: 'Enter' });
    await waitFor(() => expect(onLogin).toHaveBeenCalledWith('4217'));
    expect(onLogin).toHaveBeenCalledTimes(1);
  });

  it('supports manual entry via the on-screen number pad', async () => {
    const onLogin = jest.fn().mockResolvedValue(undefined);
    render(<KioskBadgeLogin stationLabel="LASER1" onLogin={onLogin} />);

    fireEvent.click(screen.getByTestId('kiosk-badge-key-9'));
    fireEvent.click(screen.getByTestId('kiosk-badge-key-9'));
    fireEvent.click(screen.getByTestId('kiosk-badge-key-1'));
    fireEvent.click(screen.getByRole('button', { name: /log in/i }));

    await waitFor(() => expect(onLogin).toHaveBeenCalledWith('991'));
  });

  it('shows the backend rejection verbatim and clears for a re-scan', async () => {
    const onLogin = jest.fn().mockRejectedValue({ response: { data: { detail: 'Invalid employee ID' } } });
    render(<KioskBadgeLogin stationLabel="LASER1" onLogin={onLogin} />);

    fireEvent.keyDown(window, { key: '1' });
    fireEvent.keyDown(window, { key: 'Enter' });

    expect(await screen.findByRole('alert')).toHaveTextContent('Invalid employee ID');
    expect(screen.getByTestId('kiosk-badge-display')).toHaveTextContent('Waiting for badge…');
  });

  it('ignores keyboard-shortcut chords and IME composition keystrokes', () => {
    const onLogin = jest.fn();
    render(<KioskBadgeLogin stationLabel="LASER1" onLogin={onLogin} />);

    // Ctrl/Cmd/Alt chords (browser/OS shortcuts) must not pollute the buffer.
    fireEvent.keyDown(window, { key: 'r', ctrlKey: true });
    fireEvent.keyDown(window, { key: 'l', metaKey: true });
    fireEvent.keyDown(window, { key: '4', altKey: true });
    // Nor must keystrokes replayed during IME composition.
    fireEvent.keyDown(window, { key: 'a', isComposing: true });
    expect(screen.getByTestId('kiosk-badge-display')).toHaveTextContent('Waiting for badge…');

    // A modified Enter is a shortcut too — it must not submit the buffer.
    fireEvent.keyDown(window, { key: '7' });
    fireEvent.keyDown(window, { key: 'Enter', ctrlKey: true });
    expect(onLogin).not.toHaveBeenCalled();
    expect(screen.getByTestId('kiosk-badge-display')).toHaveTextContent('7');
  });

  it('does not submit an empty badge', () => {
    const onLogin = jest.fn();
    render(<KioskBadgeLogin stationLabel="LASER1" onLogin={onLogin} />);

    fireEvent.keyDown(window, { key: 'Enter' });
    expect(onLogin).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: /log in/i })).toBeDisabled();
  });
});
