import { getCentralMinutesOfDay } from './centralTime';

/**
 * Shop-floor shift schedule.
 *
 * Shifts are defined in the shop's local (Central) wall clock, so detection
 * keys off Central time via {@link getCentralMinutesOfDay} regardless of the
 * viewer's browser timezone.
 *
 *   Shift A — 5:30 AM to 4:00 PM
 *   Shift B — 3:30 PM to 2:00 AM (wraps past midnight)
 *
 * The windows overlap 3:30–4:00 PM (shift change) by design, and there is an
 * uncovered gap 2:00–5:30 AM. {@link getCurrentShift} resolves the overlap in
 * favor of the *outgoing* shift A, so the indicator matches each shift's full
 * stated duration (A stays "A" through 4:00 PM, then B takes over). To prefer
 * the incoming shift instead, reorder {@link SHIFT_PRECEDENCE}.
 */

export type ShiftCode = 'A' | 'B';

export interface ShiftDefinition {
  code: ShiftCode;
  label: string;
  /** Human-readable schedule, e.g. "5:30 AM – 4:00 PM". */
  hours: string;
  /** Start of the window as minutes-of-day in Central time (inclusive). */
  startMinute: number;
  /**
   * End of the window as minutes-of-day in Central time (exclusive). Values
   * greater than 1440 denote a window that wraps past midnight — e.g. 1560 is
   * 2:00 AM the following day.
   */
  endMinute: number;
}

const HOUR = 60;

export const SHIFTS: Record<ShiftCode, ShiftDefinition> = {
  A: {
    code: 'A',
    label: 'Shift A',
    hours: '5:30 AM – 4:00 PM',
    startMinute: 5 * HOUR + 30, // 5:30 AM
    endMinute: 16 * HOUR, // 4:00 PM
  },
  B: {
    code: 'B',
    label: 'Shift B',
    hours: '3:30 PM – 2:00 AM',
    startMinute: 15 * HOUR + 30, // 3:30 PM
    endMinute: 26 * HOUR, // 2:00 AM next day (1440 + 120)
  },
};

/** Order in which overlapping shifts win the single "current shift" slot. */
export const SHIFT_PRECEDENCE: ShiftCode[] = ['A', 'B'];

const MINUTES_PER_DAY = 24 * HOUR;

/** Whether a Central minutes-of-day value falls inside a shift's window. */
export const isMinuteInShift = (minute: number, shift: ShiftDefinition): boolean => {
  if (!Number.isFinite(minute)) {
    return false;
  }
  if (shift.endMinute <= MINUTES_PER_DAY) {
    return minute >= shift.startMinute && minute < shift.endMinute;
  }
  // Window wraps past midnight: e.g. [930, 1560) → [930, 1440) ∪ [0, 120).
  return minute >= shift.startMinute || minute < shift.endMinute - MINUTES_PER_DAY;
};

/**
 * The shift active at the given instant (defaults to now), or `null` during the
 * uncovered gap. Overlaps resolve per {@link SHIFT_PRECEDENCE}.
 */
export const getCurrentShift = (date: Date = new Date()): ShiftDefinition | null => {
  const minute = getCentralMinutesOfDay(date);
  for (const code of SHIFT_PRECEDENCE) {
    if (isMinuteInShift(minute, SHIFTS[code])) {
      return SHIFTS[code];
    }
  }
  return null;
};

/** Convenience: just the active shift's code (A/B), or `null` off-shift. */
export const getCurrentShiftCode = (date: Date = new Date()): ShiftCode | null =>
  getCurrentShift(date)?.code ?? null;
