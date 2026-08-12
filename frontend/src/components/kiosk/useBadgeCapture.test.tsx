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

/**
 * A consumer that carries a LIVE capture and a real text field at once — the
 * crew report screen's scrap-detail line during a re-scan prompt.
 */
function ProbeWithField({ onSubmit }: { onSubmit: (value: string) => void }) {
  const [value, setValue] = useState('');
  useBadgeCapture({ enabled: true, value, onValueChange: setValue, onSubmit });
  return (
    <>
      <output data-testid="buffer">{value}</output>
      <label htmlFor="detail">Detail</label>
      <input id="detail" data-testid="detail" type="text" />
      <textarea aria-label="Notes" data-testid="notes" />
      <select aria-label="Reason" data-testid="reason">
        <option>Porosity</option>
      </select>
      <div contentEditable data-testid="rich" suppressContentEditableWarning role="textbox" tabIndex={0} aria-label="Rich" />
    </>
  );
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

  describe('typing into a real field is not badge input', () => {
    // Screens that carry BOTH a live capture and a text field exist (the crew
    // report screen's scrap-detail line during a re-scan prompt). Without this,
    // a typed character lands in the badge buffer while Enter fires a mint
    // against whatever had accumulated — a badge nobody scanned.
    it.each([
      ['an input', 'detail'],
      ['a textarea', 'notes'],
      ['a select', 'reason'],
      ['a contenteditable', 'rich'],
    ])('ignores keystrokes from %s', (_label, testId) => {
      const onSubmit = jest.fn();
      render(<ProbeWithField onSubmit={onSubmit} />);
      const field = screen.getByTestId(testId);
      // jsdom does not implement `contenteditable`, so `isContentEditable` is
      // undefined on the element no matter what the attribute says. Supply the
      // browser's value rather than drop the case — the guard the hook actually
      // reads is this property, and it is a real field on a real tablet.
      if (testId === 'rich') Object.defineProperty(field, 'isContentEditable', { value: true });

      fireEvent.keyDown(field, { key: '4' });
      fireEvent.keyDown(field, { key: '2' });
      expect(screen.getByTestId('buffer')).toBeEmptyDOMElement();

      fireEvent.keyDown(field, { key: 'Enter' });
      expect(onSubmit).not.toHaveBeenCalled();
    });

    it('still captures from the window itself, so the wedge scanner keeps working', () => {
      const onSubmit = jest.fn();
      render(<ProbeWithField onSubmit={onSubmit} />);

      fireEvent.keyDown(window, { key: 'E' });
      fireEvent.keyDown(window, { key: '1' });
      expect(screen.getByTestId('buffer')).toHaveTextContent('E1');

      fireEvent.keyDown(window, { key: 'Enter' });
      expect(onSubmit).toHaveBeenCalledWith('E1');
    });
  });
});
