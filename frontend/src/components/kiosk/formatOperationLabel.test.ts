/**
 * formatOperationLabel — the one operation-number label for every kiosk surface.
 *
 * The bug it exists to close: `WorkOrderOperation.operation_number` is free text
 * the office types, and on WO-20260807-006 it is stored as "Op 10". Every kiosk
 * surface hard-coded a literal `Op ` prefix around it, so a crew station read
 * "Op Op 10 · Skid Fit". The stored value is NOT rewritten — this is display
 * normalization only.
 */
import { formatOperationLabel } from './kioskConstants';

describe('formatOperationLabel', () => {
  it.each([
    ['a bare number string', '10', 'Op 10'],
    ['a bare number', 10, 'Op 10'],
    ['a tight uppercase prefix', 'OP10', 'Op 10'],
    ['the already-prefixed form that caused the doubling', 'Op 10', 'Op 10'],
    ['a hyphenated lowercase prefix', 'op-10', 'Op 10'],
    ['a lowercase spaced prefix', 'op 10', 'Op 10'],
    ['a dotted prefix', 'OP.10', 'Op 10'],
    ['a hash-separated prefix', 'Op #10', 'Op 10'],
    ['the spelled-out prefix', 'Operation 10', 'Op 10'],
    ['surrounding whitespace', '  Op 10  ', 'Op 10'],
    ['a multi-digit sequence', '110', 'Op 110'],
    ['a decimal sequence', '10.5', 'Op 10.5'],
  ])('renders %s as "%s" → %s', (_label, input, expected) => {
    expect(formatOperationLabel(input as string | number)).toBe(expected);
  });

  it('never produces "Op Op", whatever the stored spelling', () => {
    ['10', 'OP10', 'Op 10', 'op-10', 'op 10', 'OPERATION 10', 'op#10'].forEach((stored) => {
      expect(formatOperationLabel(stored)).not.toMatch(/Op\s+Op/i);
      expect(formatOperationLabel(stored)).toBe('Op 10');
    });
  });

  it.each([
    ['null', null],
    ['undefined', undefined],
    ['an empty string', ''],
    ['whitespace only', '   '],
  ])('is null-safe for %s — keeps the em-dash the kiosk has always shown', (_label, input) => {
    expect(formatOperationLabel(input as string | null | undefined)).toBe('Op —');
  });

  it.each([
    ['an alphanumeric sequence', 'A10', 'Op A10'],
    ['a word that merely starts with "op"', 'OPTICAL', 'Op OPTICAL'],
    ['a non-numeric label', 'FINAL', 'Op FINAL'],
  ])('passes %s through untouched — no prefix to absorb', (_label, input, expected) => {
    expect(formatOperationLabel(input)).toBe(expected);
  });

  it.each([
    ['an alphanumeric sequence', 'Op A10', 'Op A10'],
    ['a spelled-out prefix on one', 'Operation A10', 'Op A10'],
    ['a word-valued sequence', 'Op FINAL', 'Op FINAL'],
  ])('absorbs the prefix on %s too — a separator is enough, a digit is not required', (_l, input, expected) => {
    expect(formatOperationLabel(input)).toBe(expected);
  });

  it('is idempotent — formatting its own output changes nothing', () => {
    ['10', 'OP10', 'op-10', 'A10', 'FINAL', 'OPTICAL', null].forEach((stored) => {
      const once = formatOperationLabel(stored as string | null);
      expect(formatOperationLabel(once)).toBe(once);
    });
  });
});
