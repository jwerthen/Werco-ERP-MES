/**
 * Production-calendar fixtures shaped exactly like `GET /production-calendar`
 * (backend/app/schemas/production_calendar.py ProductionCalendarResponse).
 *
 * Werco's configuration from docs/API.md, on production day Tuesday 2026-11-10
 * (CST, UTC-6): the day starts 05:00 local = 11:00Z and ends 11:00Z on the 11th.
 *   Weld shop (WLD work centers 101-106): W1 05:00-15:30, W2 15:30-02:00 (planned 600)
 *   Rest of shop (DEFAULT):               S1 07:30-16:00, S2 15:30-00:00 (planned 480)
 * `today.occurrences` carries every active shift of every pattern, server-sorted by
 * (start_utc, sort_order, id) -- W2 (id 2) before S2 (id 4) at 21:30Z.
 */
import type { ProductionCalendar, ShiftDefinition } from '../types/productionCalendar';

const weekdays = [0, 1, 2, 3, 4];

const shift = (
  id: number,
  patternId: number,
  code: string,
  name: string,
  start: string,
  end: string,
  startMinute: number,
  endMinute: number,
  planned: number
): ShiftDefinition => ({
  id,
  pattern_id: patternId,
  code,
  name,
  start,
  end,
  start_minute: startMinute,
  end_minute: endMinute,
  ends_next_day: endMinute > 1440,
  days: weekdays,
  planned_minutes: planned,
  is_active: true,
  sort_order: 0,
});

export const WELD_WORK_CENTER_IDS = [101, 102, 103, 104, 105, 106];

export function wercoCalendar(overrides: Partial<ProductionCalendar> = {}): ProductionCalendar {
  return {
    configured: true,
    version: 3,
    timezone: 'America/Chicago',
    production_day_start: '05:00',
    production_day_start_minute: 300,
    updated_at: '2026-11-09T15:00:00Z',
    updated_by: 1,
    patterns: [
      {
        id: 1,
        name: 'Weld shop',
        is_default: false,
        sort_order: 0,
        work_center_ids: WELD_WORK_CENTER_IDS,
        shifts: [
          shift(1, 1, 'W1', 'Weld 1st', '05:00', '15:30', 300, 930, 600),
          shift(2, 1, 'W2', 'Weld 2nd', '15:30', '02:00', 930, 1560, 600),
        ],
      },
      {
        id: 2,
        name: 'Rest of shop',
        is_default: true,
        sort_order: 0,
        work_center_ids: [],
        shifts: [
          shift(3, 2, 'S1', '1st shift', '07:30', '16:00', 450, 960, 480),
          shift(4, 2, 'S2', '2nd shift', '15:30', '00:00', 930, 1440, 480),
        ],
      },
    ],
    today: {
      production_day: '2026-11-10',
      start_utc: '2026-11-10T11:00:00Z',
      end_utc: '2026-11-11T11:00:00Z',
      occurrences: [
        occurrence(1, 'W1', 'Weld 1st', 1, '2026-11-10T11:00:00Z', '2026-11-10T21:30:00Z', 600),
        occurrence(3, 'S1', '1st shift', 2, '2026-11-10T13:30:00Z', '2026-11-10T22:00:00Z', 480),
        occurrence(2, 'W2', 'Weld 2nd', 1, '2026-11-10T21:30:00Z', '2026-11-11T08:00:00Z', 600),
        occurrence(4, 'S2', '2nd shift', 2, '2026-11-10T21:30:00Z', '2026-11-11T06:00:00Z', 480),
      ],
    },
    ...overrides,
  };
}

function occurrence(
  shiftId: number,
  code: string,
  name: string,
  patternId: number,
  startUtc: string,
  endUtc: string,
  planned: number
) {
  return {
    shift_id: shiftId,
    code,
    name,
    pattern_id: patternId,
    production_day: '2026-11-10',
    start_utc: startUtc,
    end_utc: endUtc,
    planned_minutes: planned,
  };
}

/** No calendar saved: production day = local calendar day, no shifts. */
export function unconfiguredCalendar(): ProductionCalendar {
  return {
    configured: false,
    version: 0,
    timezone: 'America/Chicago',
    production_day_start: '00:00',
    production_day_start_minute: 0,
    updated_at: null,
    updated_by: null,
    patterns: [],
    today: {
      production_day: '2026-11-10',
      start_utc: '2026-11-10T06:00:00Z',
      end_utc: '2026-11-11T06:00:00Z',
      occurrences: [],
    },
  };
}
