/**
 * Pure helpers over the production calendar (`GET /production-calendar`).
 *
 * The server owns every time-zone and production-day decision: `today.occurrences`
 * are UTC instants it computed on the company's clock, so nothing here does zone
 * math. These helpers only pick from what the server sent (which shift is running
 * now, which pattern a work center uses) and shape the editor's draft into the
 * whole-document `PUT` body.
 *
 * The overlap rule mirrors the server's `shift_for`: an instant inside two shifts
 * (the 15:30-16:00 hand-off) belongs to the OUTGOING, earlier-start shift.
 */
import type {
  ProductionCalendar,
  ProductionCalendarUpdate,
  ShiftDefinitionInput,
  ShiftOccurrence,
  ShiftPattern,
  ShiftPatternInput,
} from '../types/productionCalendar';

export const MINUTES_PER_DAY = 24 * 60;

/** Day labels indexed by the server's weekday (0 = Monday ... 6 = Sunday). */
export const WEEKDAY_SHORT = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'] as const;

const WALL_TIME = /^([01]\d|2[0-3]):([0-5]\d)$/;

/** `"05:30"` -> `330`; anything but a 00:00-23:59 wall time -> `null`. */
export function wallTimeToMinute(value: string | null | undefined): number | null {
  const match = WALL_TIME.exec((value ?? '').trim());
  if (!match) return null;
  return Number(match[1]) * 60 + Number(match[2]);
}

/**
 * A shift's span in minutes after local midnight of its start day, the way the
 * server stores it: an end at or before the start ends the next day
 * (`"15:30"`-`"02:00"` -> `[930, 1560]`). `null` when either time is malformed.
 */
export function shiftSpan(start: string, end: string): { startMinute: number; endMinute: number } | null {
  const startMinute = wallTimeToMinute(start);
  const endWall = wallTimeToMinute(end);
  if (startMinute == null || endWall == null) return null;
  return { startMinute, endMinute: endWall > startMinute ? endWall : endWall + MINUTES_PER_DAY };
}

/** Length of a shift in minutes (`null` when a time is malformed). */
export function shiftLengthMinutes(start: string, end: string): number | null {
  const span = shiftSpan(start, end);
  return span ? span.endMinute - span.startMinute : null;
}

/** `[0,1,2,3,4]` -> `"Mon–Fri"`, `[0,2,4]` -> `"Mon, Wed, Fri"`, all seven -> `"Every day"`. */
export function formatShiftDays(days: readonly number[]): string {
  const sorted = Array.from(new Set(days))
    .filter(d => d >= 0 && d <= 6)
    .sort((a, b) => a - b);
  if (sorted.length === 0) return '—';
  if (sorted.length === 7) return 'Every day';
  const runs: string[] = [];
  let runStart = sorted[0];
  let previous = sorted[0];
  const close = () => {
    if (previous - runStart >= 2) runs.push(`${WEEKDAY_SHORT[runStart]}–${WEEKDAY_SHORT[previous]}`);
    else if (previous === runStart) runs.push(WEEKDAY_SHORT[runStart]);
    else runs.push(WEEKDAY_SHORT[runStart], WEEKDAY_SHORT[previous]);
  };
  for (const day of sorted.slice(1)) {
    if (day === previous + 1) {
      previous = day;
      continue;
    }
    close();
    runStart = day;
    previous = day;
  }
  close();
  return runs.join(', ');
}

/** The company's single default pattern, or `null`. */
export function defaultPattern(calendar: ProductionCalendar | null | undefined): ShiftPattern | null {
  return calendar?.patterns.find(pattern => pattern.is_default) ?? null;
}

/**
 * The pattern whose shifts apply at a work center: the pattern it is a member of,
 * else the default (the server's `pattern_id_for_work_center`).
 */
export function patternForWorkCenter(
  calendar: ProductionCalendar | null | undefined,
  workCenterId: number | null | undefined
): ShiftPattern | null {
  if (!calendar) return null;
  if (workCenterId != null) {
    const member = calendar.patterns.find(pattern => pattern.work_center_ids.includes(workCenterId));
    if (member) return member;
  }
  return defaultPattern(calendar);
}

/** Every shift running at `nowMs` on the current production day, outgoing (earlier start) first. */
export function runningOccurrences(
  calendar: ProductionCalendar | null | undefined,
  nowMs: number,
  patternId: number | null | undefined
): ShiftOccurrence[] {
  if (!calendar || patternId == null) return [];
  return (
    calendar.today.occurrences
      .filter(occ => occ.pattern_id === patternId)
      .filter(occ => {
        const start = Date.parse(occ.start_utc);
        const end = Date.parse(occ.end_utc);
        return Number.isFinite(start) && Number.isFinite(end) && start <= nowMs && nowMs < end;
      })
      .map((occ, index) => ({ occ, index }))
      // Stable: equal starts keep the server's order (start, sort_order, id).
      .sort((a, b) => Date.parse(a.occ.start_utc) - Date.parse(b.occ.start_utc) || a.index - b.index)
      .map(({ occ }) => occ)
  );
}

/**
 * The default pattern's shift running at `nowMs` -- in an overlap the OUTGOING one --
 * or `null` (no calendar, no default pattern, or between shifts).
 */
export function currentDefaultShift(
  calendar: ProductionCalendar | null | undefined,
  nowMs: number
): { current: ShiftOccurrence | null; overlapping: ShiftOccurrence[] } {
  const running = runningOccurrences(calendar, nowMs, defaultPattern(calendar)?.id);
  return { current: running[0] ?? null, overlapping: running };
}

/** Whether the calendar's `today` has ended (time to refetch for the next production day). */
export function productionDayEnded(calendar: ProductionCalendar | null | undefined, nowMs: number): boolean {
  if (!calendar) return true;
  const end = Date.parse(calendar.today.end_utc);
  return !Number.isFinite(end) || nowMs >= end;
}

// --------------------------------------------------------------------------------------
// Editor draft <-> PUT body
// --------------------------------------------------------------------------------------

export interface ShiftDraft extends ShiftDefinitionInput {
  /** Stable React key; never sent. */
  key: string;
}

export interface PatternDraft extends Omit<ShiftPatternInput, 'shifts'> {
  key: string;
  shifts: ShiftDraft[];
}

export interface CalendarDraft {
  production_day_start: string;
  patterns: PatternDraft[];
}

let draftKeySeq = 0;
/** A key for a row the editor just added (existing rows key off their id). */
export const newDraftKey = (prefix: 'p' | 's') => `${prefix}-new-${++draftKeySeq}`;

export function draftFromCalendar(calendar: ProductionCalendar): CalendarDraft {
  return {
    production_day_start: calendar.production_day_start,
    patterns: calendar.patterns.map(pattern => ({
      key: `p-${pattern.id}`,
      id: pattern.id,
      name: pattern.name,
      is_default: pattern.is_default,
      sort_order: pattern.sort_order,
      work_center_ids: [...pattern.work_center_ids].sort((a, b) => a - b),
      shifts: pattern.shifts.map(shift => ({
        key: `s-${shift.id}`,
        id: shift.id,
        code: shift.code,
        name: shift.name,
        start: shift.start,
        end: shift.end,
        days: [...shift.days].sort((a, b) => a - b),
        planned_minutes: shift.planned_minutes,
        is_active: shift.is_active,
        sort_order: shift.sort_order,
      })),
    })),
  };
}

/** The whole-document `PUT /production-calendar` body for a draft. */
export function buildCalendarUpdate(draft: CalendarDraft, expectedVersion: number): ProductionCalendarUpdate {
  return {
    expected_version: expectedVersion,
    production_day_start: draft.production_day_start,
    patterns: draft.patterns.map(pattern => ({
      ...(pattern.id != null ? { id: pattern.id } : {}),
      name: pattern.name.trim(),
      is_default: pattern.is_default,
      // Existing rows keep theirs; a new row carries the API default (0), so the server
      // orders it after older patterns (by id) and new shifts by their start time.
      sort_order: pattern.sort_order,
      work_center_ids: Array.from(new Set(pattern.work_center_ids)).sort((a, b) => a - b),
      shifts: pattern.shifts.map(shift => ({
        ...(shift.id != null ? { id: shift.id } : {}),
        code: shift.code.trim(),
        name: shift.name.trim(),
        start: shift.start,
        end: shift.end,
        days: Array.from(new Set(shift.days)).sort((a, b) => a - b),
        planned_minutes: shift.planned_minutes,
        is_active: shift.is_active,
        sort_order: shift.sort_order,
      })),
    })),
  };
}

/**
 * Client-side mirror of the server's whole-document validation, so the editor can say
 * what is wrong before a round trip. The server re-validates everything (422) and stays
 * the authority; each rule here is one the server enforces with the same meaning.
 */
export function draftIssues(draft: CalendarDraft): string[] {
  const issues: string[] = [];
  const dayStart = wallTimeToMinute(draft.production_day_start);
  if (dayStart == null) issues.push('Production day start must be a time (HH:MM).');
  const defaults = draft.patterns.filter(pattern => pattern.is_default);
  if (defaults.length > 1) issues.push('Only one shift pattern can be the default.');
  const patternNames = new Set<string>();
  const codes = new Set<string>();
  const owner = new Map<number, string>();
  draft.patterns.forEach((pattern, index) => {
    const label = pattern.name.trim() || `Pattern ${index + 1}`;
    if (!pattern.name.trim()) issues.push(`Pattern ${index + 1} needs a name.`);
    else if (patternNames.has(pattern.name.trim().toLowerCase())) {
      issues.push(`Shift pattern name '${pattern.name.trim()}' is used twice.`);
    }
    patternNames.add(pattern.name.trim().toLowerCase());
    pattern.work_center_ids.forEach(wcId => {
      const other = owner.get(wcId);
      if (other !== undefined && other !== pattern.key) {
        issues.push(`A work center is in more than one pattern (${label}).`);
      }
      owner.set(wcId, pattern.key);
    });
    pattern.shifts.forEach((shift, shiftIndex) => {
      const code = shift.code.trim();
      const shiftLabel = code || `${label} shift ${shiftIndex + 1}`;
      if (!code) issues.push(`${shiftLabel} needs a code.`);
      else if (codes.has(code.toLowerCase())) issues.push(`Shift code '${code}' is used twice.`);
      if (code) codes.add(code.toLowerCase());
      if (!shift.name.trim()) issues.push(`Shift ${shiftLabel} needs a name.`);
      if (shift.days.length === 0) issues.push(`Shift ${shiftLabel} needs at least one day.`);
      const span = shiftSpan(shift.start, shift.end);
      if (!span) {
        issues.push(`Shift ${shiftLabel} needs a start and end time.`);
        return;
      }
      const length = span.endMinute - span.startMinute;
      if (!Number.isFinite(shift.planned_minutes) || shift.planned_minutes < 1) {
        issues.push(`Shift ${shiftLabel} needs planned minutes of at least 1.`);
      } else if (shift.planned_minutes > length) {
        issues.push(`Shift ${shiftLabel} plans ${shift.planned_minutes} minutes but is only ${length} minutes long.`);
      }
      if (dayStart != null && (span.startMinute < dayStart || span.endMinute > dayStart + MINUTES_PER_DAY)) {
        issues.push(
          `Shift ${shiftLabel} (${shift.start}–${shift.end}) must start at or after the production day start ` +
            `(${draft.production_day_start}) and end by the next day's start.`
        );
      }
    });
    // The sync writes these sums as the working calendar's hours, which hold 0-24 a day.
    WEEKDAY_SHORT.forEach((dayLabel, day) => {
      const planned = pattern.shifts
        .filter(shift => shift.is_active && shift.days.includes(day) && Number.isFinite(shift.planned_minutes))
        .reduce((sum, shift) => sum + shift.planned_minutes, 0);
      if (planned > MINUTES_PER_DAY) {
        issues.push(
          `${label} plans ${planned} minutes of active shifts starting on ${dayLabel}; ` +
            `the shifts starting on one day can plan at most ${MINUTES_PER_DAY} minutes (24 hours).`
        );
      }
    });
  });
  return issues;
}

/** Hours to one decimal, trimming a trailing `.0` (`16` -> `"16"`, `7.5` -> `"7.5"`). */
export const formatHours = (hours: number): string =>
  Number.isFinite(hours) ? String(Math.round(hours * 10) / 10) : '—';
