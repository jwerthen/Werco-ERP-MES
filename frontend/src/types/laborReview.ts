/**
 * Labor Review wire shapes — mirror `backend/app/schemas/labor_review.py`.
 *
 * `GET /shop-floor/time-entries` (Admin / Manager / Supervisor / Quality) and
 * `POST /shop-floor/time-entries/{id}/set-clock-out` (Admin / Manager / Supervisor).
 * Datetimes arrive as UTC ISO strings with a trailing `Z`; `production_day` is a
 * date-only `YYYY-MM-DD`.
 */
/** The closed vocabulary of review flags the server computes per entry. */
export type LaborReviewFlag =
  | 'open_past_shift_end'
  | 'long_entry'
  | 'overlap'
  | 'open_on_closed_work'
  | 'long_zero_pieces'
  | 'backfill';

export const LABOR_REVIEW_FLAGS: readonly LaborReviewFlag[] = [
  'open_past_shift_end',
  'long_entry',
  'overlap',
  'open_on_closed_work',
  'long_zero_pieces',
  'backfill',
];

export type LaborReviewStatus = 'open' | 'closed';

export interface LaborReviewEntry {
  id: number;
  version: number;
  user_id: number;
  employee_name: string | null;
  employee_id: string | null;
  work_order_id: number | null;
  work_order_number: string | null;
  work_order_status: string | null;
  operation_id: number | null;
  /** Bare identifier ("10"); render through formatOperationLabel. */
  operation_number: string | null;
  operation_name: string | null;
  operation_status: string | null;
  work_center_id: number | null;
  work_center_name: string | null;
  /** Usually a TimeEntryType value; a plain string on the wire so an unknown value still reads. */
  entry_type: string | null;
  source: string | null;
  clock_in: string;
  clock_out: string | null;
  /** Stored hours; null while the entry is open. */
  duration_hours: number | null;
  /** Closed: the stored duration. Open: clock-in to now. */
  elapsed_hours: number;
  is_open: boolean;
  quantity_produced: number;
  quantity_scrapped: number;
  approved: string | null;
  approved_by: number | null;
  approved_by_name: string | null;
  production_day: string;
  shift_name: string | null;
  /** End of the shift the entry STARTED in, when a production calendar is configured. */
  shift_end: string | null;
  flags: LaborReviewFlag[];
  can_approve: boolean;
  can_unapprove: boolean;
  can_set_clock_out: boolean;
}

export interface LaborReviewListResponse {
  items: LaborReviewEntry[];
  total: number;
  page: number;
  page_size: number;
  has_next: boolean;
  /** The first production day actually read; null = no lower bound (status=open, no dates). */
  start_date: string | null;
  end_date: string | null;
  /** Entries carrying each flag across every page of the filtered result. */
  flag_counts: Partial<Record<LaborReviewFlag, number>>;
  long_entry_hours: number;
  long_zero_pieces_hours: number;
}

export interface LaborReviewListParams {
  start_date?: string;
  end_date?: string;
  user_id?: number;
  work_center_id?: number;
  work_order_id?: number;
  status?: LaborReviewStatus;
  approved?: boolean;
  flag?: LaborReviewFlag;
  flagged_only?: boolean;
  page?: number;
  page_size?: number;
}

export interface SetClockOutRequest {
  /** UTC ISO instant (send `centralWallClockToUtcISO(...)`). */
  clock_out: string;
  reason: string;
  /** The entry's version as last read; a stale one is refused 409. */
  version: number;
}

export type HoursSkippedReason =
  | 'no_work_order'
  | 'terminal_work_order'
  | 'no_change'
  | 'operation_hours_manual'
  | 'operation_hours_none'
  | 'operation_hours_below_evidence';

export interface HoursRederivation {
  applied: boolean;
  /** Usually a HoursSkippedReason; typed open so a newer server value still reads. */
  skipped_reason: string | null;
  hours_delta: number;
  operation_bucket: 'setup' | 'run' | null;
  operation_hours_before: number | null;
  operation_hours_after: number | null;
  work_order_hours_before: number | null;
  work_order_hours_after: number | null;
  job_cost_recomputed: boolean;
  actual_cost_recomputed: boolean;
}

export interface SetClockOutResponse {
  entry: LaborReviewEntry;
  changed: boolean;
  previous_clock_out: string | null;
  previous_duration_hours: number | null;
  hours: HoursRederivation | null;
}

/**
 * The fields Labor Review reads from the approve / unapprove response
 * (`TimeEntryResponse` in `backend/app/schemas/time_entry.py`, a superset).
 */
export interface TimeEntryApprovalResult {
  id: number;
  user_id: number;
  clock_in: string;
  clock_out: string | null;
  duration_hours: number | null;
  approved: string | null;
  approved_by: number | null;
}
