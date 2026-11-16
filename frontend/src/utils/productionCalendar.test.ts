/**
 * Pure helpers over GET /production-calendar. The server owns every zone and
 * production-day decision; these only pick from its UTC occurrences and shape the
 * editor's whole-document PUT body.
 */
import * as fs from 'fs';
import * as path from 'path';
import {
  buildCalendarUpdate,
  currentDefaultShift,
  defaultPattern,
  draftFromCalendar,
  draftIssues,
  formatShiftDays,
  patternForWorkCenter,
  productionDayEnded,
  runningOccurrences,
  shiftLengthMinutes,
  shiftSpan,
  wallTimeToMinute,
} from './productionCalendar';
import { unconfiguredCalendar, wercoCalendar } from '../test-utils/productionCalendarFixtures';

const at = (iso: string) => Date.parse(iso);

describe('the hard-coded shift table is gone', () => {
  // The old utils/shifts.ts hard-coded Shift A 05:30-16:00 / B 15:30-02:00, which
  // never matched Werco's confirmed rules. Shifts are configuration now.
  test('utils/shifts.ts no longer exists', () => {
    expect(fs.existsSync(path.join(__dirname, 'shifts.ts'))).toBe(false);
  });

  test('the HUD no longer imports it', () => {
    const layout = fs.readFileSync(path.join(__dirname, '..', 'components', 'Layout.tsx'), 'utf8');
    expect(layout).not.toMatch(/utils\/shifts/);
  });
});

describe('wall times and spans', () => {
  test('parses HH:MM and rejects anything else', () => {
    expect(wallTimeToMinute('05:30')).toBe(330);
    expect(wallTimeToMinute('00:00')).toBe(0);
    expect(wallTimeToMinute('23:59')).toBe(1439);
    expect(wallTimeToMinute('24:00')).toBeNull();
    expect(wallTimeToMinute('5:30')).toBeNull();
    expect(wallTimeToMinute('')).toBeNull();
  });

  test('an end at or before the start ends the next day, like the server', () => {
    expect(shiftSpan('15:30', '02:00')).toEqual({ startMinute: 930, endMinute: 1560 });
    expect(shiftSpan('15:30', '00:00')).toEqual({ startMinute: 930, endMinute: 1440 });
    expect(shiftSpan('05:00', '05:00')).toEqual({ startMinute: 300, endMinute: 1740 });
    expect(shiftLengthMinutes('05:00', '15:30')).toBe(630);
    expect(shiftLengthMinutes('15:30', '02:00')).toBe(630);
    expect(shiftLengthMinutes('bad', '02:00')).toBeNull();
  });

  test('formats day lists compactly', () => {
    expect(formatShiftDays([0, 1, 2, 3, 4])).toBe('Mon–Fri');
    expect(formatShiftDays([0, 2, 4])).toBe('Mon, Wed, Fri');
    expect(formatShiftDays([5, 6])).toBe('Sat, Sun');
    expect(formatShiftDays([0, 1, 2, 3, 4, 5, 6])).toBe('Every day');
    expect(formatShiftDays([])).toBe('—');
  });
});

describe('patterns', () => {
  test('a work center uses its own pattern, else the default', () => {
    const calendar = wercoCalendar();
    expect(defaultPattern(calendar)?.name).toBe('Rest of shop');
    expect(patternForWorkCenter(calendar, 103)?.name).toBe('Weld shop');
    expect(patternForWorkCenter(calendar, 7)?.name).toBe('Rest of shop');
    expect(patternForWorkCenter(calendar, null)?.name).toBe('Rest of shop');
    expect(patternForWorkCenter(unconfiguredCalendar(), 103)).toBeNull();
  });
});

describe('the current shift (default pattern)', () => {
  const calendar = wercoCalendar();

  test('mid-shift', () => {
    // 10:00 CST = 16:00Z: only S1 of the default pattern is running.
    expect(currentDefaultShift(calendar, at('2026-11-10T16:00:00Z')).current?.code).toBe('S1');
  });

  test('the 15:30-16:00 hand-off belongs to the OUTGOING shift', () => {
    // 15:45 CST = 21:45Z: S1 (to 22:00Z) and S2 (from 21:30Z) both run.
    const { current, overlapping } = currentDefaultShift(calendar, at('2026-11-10T21:45:00Z'));
    expect(current?.code).toBe('S1');
    expect(overlapping.map(o => o.code)).toEqual(['S1', 'S2']);
  });

  test('a shift end is exclusive and the incoming shift takes over', () => {
    expect(currentDefaultShift(calendar, at('2026-11-10T22:00:00Z')).current?.code).toBe('S2');
  });

  test('between shifts there is none', () => {
    // 02:00 CST on the 11th = 08:00Z: still Tuesday's production day, S2 ended at midnight.
    expect(currentDefaultShift(calendar, at('2026-11-11T08:00:00Z')).current).toBeNull();
  });

  test('a work center pattern sees its own shifts: 01:30 Wednesday is still Tuesday W2', () => {
    // 01:30 CST Wed = 07:30Z: inside Tuesday's production day and W2.
    expect(runningOccurrences(calendar, at('2026-11-11T07:30:00Z'), 1).map(o => o.code)).toEqual(['W2']);
  });

  test('no default pattern or no calendar reads as no shift', () => {
    const noDefault = wercoCalendar({
      patterns: wercoCalendar().patterns.map(p => ({ ...p, is_default: false })),
    });
    expect(currentDefaultShift(noDefault, at('2026-11-10T16:00:00Z')).current).toBeNull();
    expect(currentDefaultShift(null, at('2026-11-10T16:00:00Z')).current).toBeNull();
    expect(currentDefaultShift(unconfiguredCalendar(), at('2026-11-10T16:00:00Z')).current).toBeNull();
  });

  test('the production day ends at its end_utc', () => {
    expect(productionDayEnded(calendar, at('2026-11-11T10:59:59Z'))).toBe(false);
    expect(productionDayEnded(calendar, at('2026-11-11T11:00:00Z'))).toBe(true);
    expect(productionDayEnded(null, at('2026-11-11T10:00:00Z'))).toBe(true);
  });
});

describe('editor draft -> PUT body', () => {
  test('round-trips the saved calendar into the whole-document body', () => {
    const body = buildCalendarUpdate(draftFromCalendar(wercoCalendar()), 3);
    expect(body).toEqual({
      expected_version: 3,
      production_day_start: '05:00',
      patterns: [
        {
          id: 1,
          name: 'Weld shop',
          is_default: false,
          sort_order: 0,
          work_center_ids: [101, 102, 103, 104, 105, 106],
          shifts: [
            {
              id: 1,
              code: 'W1',
              name: 'Weld 1st',
              start: '05:00',
              end: '15:30',
              days: [0, 1, 2, 3, 4],
              planned_minutes: 600,
              is_active: true,
              sort_order: 0,
            },
            {
              id: 2,
              code: 'W2',
              name: 'Weld 2nd',
              start: '15:30',
              end: '02:00',
              days: [0, 1, 2, 3, 4],
              planned_minutes: 600,
              is_active: true,
              sort_order: 0,
            },
          ],
        },
        {
          id: 2,
          name: 'Rest of shop',
          is_default: true,
          sort_order: 0,
          work_center_ids: [],
          shifts: [
            {
              id: 3,
              code: 'S1',
              name: '1st shift',
              start: '07:30',
              end: '16:00',
              days: [0, 1, 2, 3, 4],
              planned_minutes: 480,
              is_active: true,
              sort_order: 0,
            },
            {
              id: 4,
              code: 'S2',
              name: '2nd shift',
              start: '15:30',
              end: '00:00',
              days: [0, 1, 2, 3, 4],
              planned_minutes: 480,
              is_active: true,
              sort_order: 0,
            },
          ],
        },
      ],
    });
  });

  test('a new row is sent without an id; text is trimmed and lists de-duplicated', () => {
    const draft = draftFromCalendar(wercoCalendar());
    draft.patterns[0].work_center_ids = [102, 101, 101];
    draft.patterns[0].shifts.push({
      key: 's-new',
      code: ' W3 ',
      name: ' Weld 3rd ',
      start: '02:00',
      end: '04:00',
      days: [4, 0, 0],
      planned_minutes: 100,
      is_active: true,
      sort_order: 0,
    });
    const body = buildCalendarUpdate(draft, 3);
    const added = body.patterns[0].shifts[2];
    expect(added).not.toHaveProperty('id');
    expect(added).not.toHaveProperty('key');
    expect(added).toMatchObject({ code: 'W3', name: 'Weld 3rd', days: [0, 4] });
    expect(body.patterns[0].work_center_ids).toEqual([101, 102]);
  });
});

describe('client-side validation mirrors the server rules', () => {
  test('the saved Werco calendar is valid', () => {
    expect(draftIssues(draftFromCalendar(wercoCalendar()))).toEqual([]);
  });

  test('flags each rule the server enforces', () => {
    const draft = draftFromCalendar(wercoCalendar());
    draft.patterns[0].is_default = true; // two defaults
    draft.patterns[0].shifts[1].code = 's1'; // duplicate code (case-insensitive)
    draft.patterns[0].shifts[0].planned_minutes = 700; // longer than 05:00-15:30
    draft.patterns[1].shifts[0].start = '02:00'; // before the 05:00 day start
    draft.patterns[1].shifts[1].days = [];
    const issues = draftIssues(draft);
    expect(issues).toEqual(
      expect.arrayContaining([
        'Only one shift pattern can be the default.',
        "Shift code 'S1' is used twice.",
        'Shift W1 plans 700 minutes but is only 630 minutes long.',
        expect.stringMatching(/Shift S1 \(02:00–16:00\) must start at or after the production day start/),
        'Shift S2 needs at least one day.',
      ])
    );
  });

  test('a shift may run to exactly the next day start', () => {
    const draft = draftFromCalendar(wercoCalendar());
    draft.patterns[0].shifts[1].end = '05:00'; // W2 15:30 -> 05:00 next day = day start + 1440
    draft.patterns[0].shifts[1].planned_minutes = 600;
    expect(draftIssues(draft)).toEqual([]);
    draft.patterns[0].shifts[1].end = '05:30';
    expect(draftIssues(draft).join(' ')).toMatch(/end by the next day's start/);
  });

  test('active shifts starting on one day may plan at most 24 hours (the working-calendar range)', () => {
    const draft = draftFromCalendar(wercoCalendar());
    const weld = draft.patterns[0];
    // A parallel crew: W1 + W2 + B1 = 600 + 600 + 600 minutes on each weekday.
    weld.shifts.push({ ...weld.shifts[0], key: 'b1', id: undefined, code: 'B1', name: 'B 1st' });
    expect(draftIssues(draft)).toEqual(
      expect.arrayContaining([
        'Weld shop plans 1800 minutes of active shifts starting on Mon; the shifts starting on one day can plan at most 1440 minutes (24 hours).',
      ])
    );
    // Exactly a day is fine, and a retired shift plans nothing.
    weld.shifts[2].planned_minutes = 240;
    expect(draftIssues(draft)).toEqual([]);
    weld.shifts[2].planned_minutes = 600;
    weld.shifts[2].is_active = false;
    expect(draftIssues(draft)).toEqual([]);
  });

  test('no default pattern at all is a valid calendar', () => {
    const draft = draftFromCalendar(wercoCalendar());
    draft.patterns[1].is_default = false;
    expect(draftIssues(draft)).toEqual([]);
    expect(buildCalendarUpdate(draft, 3).patterns.map(p => p.is_default)).toEqual([false, false]);
  });
});
