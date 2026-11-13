/**
 * Types for the production calendar (`/production-calendar`, migration 120):
 * the production-day start, shift patterns with their work centers, and shift
 * definitions. Mirrors `backend/app/schemas/production_calendar.py` exactly.
 *
 * Wall times (`start`, `end`, `production_day_start`) are `HH:MM` on the
 * company's local clock. A shift whose `end` is at or before its `start` ends
 * the next day, and a shift always belongs to the production day it STARTED on.
 * `days` are 0 = Monday ... 6 = Sunday and name the day a shift starts on.
 * Instants (`*_utc`, `updated_at`, `at`) are UTC ISO-8601 with a trailing `Z`;
 * `production_day` values are plain `YYYY-MM-DD` dates.
 */

/** 0 = Monday ... 6 = Sunday (Python `date.weekday()`, the `weekly_hours` order). */
export type Weekday = 0 | 1 | 2 | 3 | 4 | 5 | 6;

export interface ShiftDefinition {
  id: number;
  pattern_id: number;
  code: string;
  name: string;
  /** Local wall time `HH:MM`. */
  start: string;
  /** Local wall time `HH:MM`; at or before `start` means the next day. */
  end: string;
  /** Minutes after local midnight of the start day. */
  start_minute: number;
  /** Minutes after local midnight of the start day; above 1440 = the next day. */
  end_minute: number;
  ends_next_day: boolean;
  days: number[];
  /** Planned production minutes (breaks and lunch excluded). */
  planned_minutes: number;
  /** false = retired: kept for history, no longer attributed. */
  is_active: boolean;
  sort_order: number;
}

export interface ShiftPattern {
  id: number;
  name: string;
  /** The pattern every work center in no other pattern uses (one at most). */
  is_default: boolean;
  sort_order: number;
  work_center_ids: number[];
  shifts: ShiftDefinition[];
}

/** One shift on one production day, as server-computed UTC instants. */
export interface ShiftOccurrence {
  shift_id: number;
  code: string;
  name: string;
  pattern_id: number;
  production_day: string;
  start_utc: string;
  end_utc: string;
  planned_minutes: number;
}

export interface ProductionDay {
  production_day: string;
  start_utc: string;
  end_utc: string;
  /** Every ACTIVE shift of every pattern on this production day, by start. */
  occurrences: ShiftOccurrence[];
}

export interface ProductionCalendar {
  /** false = no calendar saved: production day = company-local calendar day, no shifts. */
  configured: boolean;
  /** 0 before the first save; send it back as `expected_version`. */
  version: number;
  timezone: string;
  production_day_start: string;
  production_day_start_minute: number;
  updated_at?: string | null;
  updated_by?: number | null;
  patterns: ShiftPattern[];
  /** The CURRENT production day and its shifts. */
  today: ProductionDay;
}

export interface ShiftDefinitionInput {
  /** An existing shift's id to keep it; omit for a new shift. */
  id?: number | null;
  code: string;
  name: string;
  start: string;
  end: string;
  days: number[];
  planned_minutes: number;
  is_active: boolean;
  sort_order: number;
}

export interface ShiftPatternInput {
  /** An existing pattern's id to keep it; omit for a new pattern. */
  id?: number | null;
  name: string;
  is_default: boolean;
  sort_order: number;
  work_center_ids: number[];
  shifts: ShiftDefinitionInput[];
}

/** `PUT /production-calendar` — the WHOLE document; anything omitted is removed. */
export interface ProductionCalendarUpdate {
  expected_version: number;
  production_day_start: string;
  patterns: ShiftPatternInput[];
}

/** `GET /production-calendar/resolve`. */
export interface ProductionMoment {
  at: string;
  work_center_id?: number | null;
  pattern_id?: number | null;
  production_day: string;
  production_day_start_utc: string;
  production_day_end_utc: string;
  /** In an overlap, the OUTGOING (earlier-start) shift. */
  shift?: ShiftOccurrence | null;
  overlapping: ShiftOccurrence[];
  configured: boolean;
  calendar_version: number;
}

export interface WorkingCalendarSyncRow {
  work_center_id: number;
  work_center_code: string;
  work_center_name: string;
  pattern_id: number;
  pattern_name: string;
  /** 0 = the work center has no saved working calendar yet. */
  current_version: number;
  current_weekly_hours: number[];
  new_weekly_hours: number[];
  overrides_kept: number;
  changed: boolean;
}

/** `POST /production-calendar/working-calendars/sync`. */
export interface WorkingCalendarSyncResult {
  dry_run: boolean;
  configured: boolean;
  calendar_version: number;
  changed_count: number;
  rows: WorkingCalendarSyncRow[];
}
