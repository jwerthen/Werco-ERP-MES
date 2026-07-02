import React, { useState } from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { useBadgeCapture } from './useBadgeCapture';

/** Minimal consumer mirroring how the kiosk screens wire the hook. */
function Probe({
  enabled = true,
  maxLength,
  onSubmit,
}: {
  enabled?: boolean;
  maxLength?: number;
  onSubmit: (value: string) => void;
}) {
  const [value, setValue] = useState('');
  useBadgeCapture({ enabled, value, onValueChange: setValue, onSubmit, maxLength });
  return <output data-testid="buffer">{value}</output>;
}

describe('useBadgeCapture', () => {
  it('buffers scanner keystrokes at window level and submits the buffer on Enter', () => {
    const onSubmit = jest.fn();
    render(<Probe onSubmit={onSubmit} />);

    fireEvent.keyDown(window, { key: 'E' });
    fireEvent.keyDown(window, { key: 'M' });
    fireEvent.keyDown(window, { key: 'P' });
    fireEvent.keyDown(window, { key: '-' });
    fireEvent.keyDown(window, { key: '7' });
    expect(screen.getByTestId('buffer')).toHaveTextContent('EMP-7');

    fireEvent.keyDown(window, { key: 'Enter' });
    expect(onSubmit).toHaveBeenCalledWith('EMP-7');
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it('supports Backspace edits to the buffer', () => {
    const onSubmit = jest.fn();
    render(<Probe onSubmit={onSubmit} />);

    fireEvent.keyDown(window, { key: '4' });
    fireEvent.keyDown(window, { key: '2' });
    fireEvent.keyDown(window, { key: 'Backspace' });
    expect(screen.getByTestId('buffer')).toHaveTextContent('4');
  });

  it('ignores chords, IME composition, and non-badge keys', () => {
    const onSubmit = jest.fn();
    render(<Probe onSubmit={onSubmit} />);

    fireEvent.keyDown(window, { key: 'r', ctrlKey: true });
    fireEvent.keyDown(window, { key: 'l', metaKey: true });
    fireEvent.keyDown(window, { key: '4', altKey: true });
    fireEvent.keyDown(window, { key: 'a', isComposing: true });
    fireEvent.keyDown(window, { key: 'Escape' });
    fireEvent.keyDown(window, { key: ' ' });
    expect(screen.getByTestId('buffer')).toHaveTextContent('');

    // A modified Enter is a shortcut, not a scan terminator.
    fireEvent.keyDown(window, { key: '7' });
    fireEvent.keyDown(window, { key: 'Enter', ctrlKey: true });
    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByTestId('buffer')).toHaveTextContent('7');
  });

  it('caps the buffer at maxLength', () => {
    const onSubmit = jest.fn();
    render(<Probe onSubmit={onSubmit} maxLength={3} />);

    ['1', '2', '3', '4'].forEach((key) => fireEvent.keyDown(window, { key }));
    expect(screen.getByTestId('buffer')).toHaveTextContent('123');
  });

  it('captures nothing while disabled (exactly one enabled consumer owns the scanner)', () => {
    const onSubmit = jest.fn();
    render(<Probe onSubmit={onSubmit} enabled={false} />);

    fireEvent.keyDown(window, { key: '9' });
    fireEvent.keyDown(window, { key: 'Enter' });
    expect(screen.getByTestId('buffer')).toHaveTextContent('');
    expect(onSubmit).not.toHaveBeenCalled();
  });
});
