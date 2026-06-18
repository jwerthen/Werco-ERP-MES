/**
 * Shift-detection logic keys off the shop's local (Central) wall clock, so the
 * cases below pin each instant to a concrete UTC moment and assert how it is
 * interpreted in America/Chicago — that keeps the expectations deterministic
 * regardless of the machine running the tests.
 *
 * America/Chicago is UTC−5 in June (CDT) and UTC−6 in winter (CST). Most cases
 * use June instants (Central = UTC − 5h); a CST case is included to exercise the
 * DST offset explicitly.
 */

import { getCentralMinutesOfDay } from './centralTime';
import {
  SHIFTS,
  SHIFT_PRECEDENCE,
  getCurrentShift,
  getCurrentShiftCode,
  isMinuteInShift,
} from './shifts';

describe('getCurrentShift / getCurrentShiftCode (Central time)', () => {
  // June → CDT (UTC−5): Central wall-clock = UTC − 5h.
  const cases: Array<{ label: string; utc: string; expected: 'A' | 'B' | null }> = [
    { label: '5:29 AM Central (just before A starts)', utc: '2026-06-17T10:29:00Z', expected: null },
    { label: '5:30 AM Central (A start, inclusive)', utc: '2026-06-17T10:30:00Z', expected: 'A' },
    { label: '10:00 AM Central', utc: '2026-06-17T15:00:00Z', expected: 'A' },
    { label: '3:45 PM Central (inside A/B overlap, A wins)', utc: '2026-06-17T20:45:00Z', expected: 'A' },
    { label: '4:00 PM Central (A end exclusive, B takes over)', utc: '2026-06-17T21:00:00Z', expected: 'B' },
    { label: '9:00 PM Central', utc: '2026-06-18T02:00:00Z', expected: 'B' },
    { label: '1:59 AM Central (B wraps past midnight)', utc: '2026-06-17T06:59:00Z', expected: 'B' },
    { label: '2:00 AM Central (B end exclusive)', utc: '2026-06-17T07:00:00Z', expected: null },
    { label: '3:00 AM Central (uncovered gap)', utc: '2026-06-17T08:00:00Z', expected: null },
  ];

  it.each(cases)('$label → $expected', ({ utc, expected }) => {
    expect(getCurrentShiftCode(new Date(utc))).toBe(expected);
    expect(getCurrentShift(new Date(utc))?.code ?? null).toBe(expected);
  });

  it('resolves the 3:30–4:00 PM overlap to the outgoing shift A per SHIFT_PRECEDENCE', () => {
    // 3:30 PM Central is inside BOTH A (…–4:00 PM) and B (3:30 PM–…); A must win.
    expect(getCurrentShiftCode(new Date('2026-06-17T20:30:00Z'))).toBe('A');
    expect(SHIFT_PRECEDENCE).toEqual(['A', 'B']);
  });

  it('honors Central Standard Time (UTC−6) in winter, not the test machine timezone', () => {
    // January → CST (UTC−6): Central wall-clock = UTC − 6h.
    // 16:30 UTC = 10:30 AM Central → A.
    expect(getCurrentShiftCode(new Date('2026-01-15T16:30:00Z'))).toBe('A');
    // 21:30 UTC = 3:30 PM Central → overlap, A wins.
    expect(getCurrentShiftCode(new Date('2026-01-15T21:30:00Z'))).toBe('A');
    // 22:00 UTC = 4:00 PM Central → B.
    expect(getCurrentShiftCode(new Date('2026-01-15T22:00:00Z'))).toBe('B');
    // 11:30 UTC = 5:30 AM Central → A start, inclusive.
    expect(getCurrentShiftCode(new Date('2026-01-15T11:30:00Z'))).toBe('A');
    // 11:29 UTC = 5:29 AM Central → still in the gap.
    expect(getCurrentShiftCode(new Date('2026-01-15T11:29:00Z'))).toBeNull();
  });

  it('returns the full shift definition, not just the code', () => {
    const shift = getCurrentShift(new Date('2026-06-17T15:00:00Z')); // 10:00 AM Central
    expect(shift).toEqual(SHIFTS.A);
    expect(shift?.label).toBe('Shift A');
    expect(shift?.hours).toBe('5:30 AM – 4:00 PM');
  });
});

describe('getCentralMinutesOfDay', () => {
  it('returns minutes since Central midnight for a known instant', () => {
    // 15:00 UTC in June (CDT) = 10:00 AM Central = 10 * 60 = 600.
    expect(getCentralMinutesOfDay(new Date('2026-06-17T15:00:00Z'))).toBe(600);
    // 10:30 UTC in June (CDT) = 5:30 AM Central = 5 * 60 + 30 = 330.
    expect(getCentralMinutesOfDay(new Date('2026-06-17T10:30:00Z'))).toBe(330);
  });

  it('normalizes Central midnight to 0', () => {
    // 05:00 UTC in June (CDT) = 12:00 AM Central → 0, not 1440.
    expect(getCentralMinutesOfDay(new Date('2026-06-17T05:00:00Z'))).toBe(0);
  });

  it('honors the CST offset in winter', () => {
    // 16:00 UTC in January (CST, UTC−6) = 10:00 AM Central = 600.
    expect(getCentralMinutesOfDay(new Date('2026-01-15T16:00:00Z'))).toBe(600);
  });

  it('returns NaN for an unparseable input', () => {
    expect(Number.isNaN(getCentralMinutesOfDay('not-a-date'))).toBe(true);
    expect(Number.isNaN(getCentralMinutesOfDay(new Date('invalid')))).toBe(true);
    expect(Number.isNaN(getCentralMinutesOfDay(null))).toBe(true);
  });
});

describe('isMinuteInShift', () => {
  it('treats the start as inclusive and the end as exclusive for a same-day window (A)', () => {
    expect(isMinuteInShift(SHIFTS.A.startMinute, SHIFTS.A)).toBe(true); // 5:30 AM
    expect(isMinuteInShift(SHIFTS.A.startMinute - 1, SHIFTS.A)).toBe(false); // 5:29 AM
    expect(isMinuteInShift(10 * 60, SHIFTS.A)).toBe(true); // 10:00 AM
    expect(isMinuteInShift(SHIFTS.A.endMinute - 1, SHIFTS.A)).toBe(true); // 3:59 PM
    expect(isMinuteInShift(SHIFTS.A.endMinute, SHIFTS.A)).toBe(false); // 4:00 PM exclusive
  });

  it('handles the midnight-wrapping B window on both sides of midnight', () => {
    expect(isMinuteInShift(15 * 60 + 30, SHIFTS.B)).toBe(true); // 3:30 PM start, inclusive
    expect(isMinuteInShift(15 * 60 + 29, SHIFTS.B)).toBe(false); // 3:29 PM, before start
    expect(isMinuteInShift(23 * 60, SHIFTS.B)).toBe(true); // 11:00 PM, before midnight
    expect(isMinuteInShift(0, SHIFTS.B)).toBe(true); // 12:00 AM, after wrap
    expect(isMinuteInShift(60, SHIFTS.B)).toBe(true); // 1:00 AM
    expect(isMinuteInShift(2 * 60 - 1, SHIFTS.B)).toBe(true); // 1:59 AM, last covered minute
    expect(isMinuteInShift(2 * 60, SHIFTS.B)).toBe(false); // 2:00 AM end, exclusive
    expect(isMinuteInShift(3 * 60, SHIFTS.B)).toBe(false); // 3:00 AM, in the gap
  });

  it('returns false for a non-finite minute value', () => {
    expect(isMinuteInShift(Number.NaN, SHIFTS.A)).toBe(false);
    expect(isMinuteInShift(Number.NaN, SHIFTS.B)).toBe(false);
  });
});
