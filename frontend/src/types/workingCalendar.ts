export interface WorkingCalendarOverride {
  date: string;
  hours: number;
  reason: string;
}
export interface WorkingCalendar {
  work_center_id: number;
  version: number;
  weekly_hours: number[];
  overrides: WorkingCalendarOverride[];
}
export interface WorkingCalendarUpdate {
  expected_version: number;
  weekly_hours: number[];
  overrides: WorkingCalendarOverride[];
}
