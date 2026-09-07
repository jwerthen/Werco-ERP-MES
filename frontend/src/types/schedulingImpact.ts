export interface SchedulingImpactRequest {
  action: 'earliest' | 'shift';
  work_order_ids: number[];
  shift_days?: number;
  horizon_days?: number;
}
export interface SchedulingImpactOperation {
  operation_id: number;
  operation_number: string | null;
  operation_name: string;
  work_center_id: number;
  work_center_code: string;
  before_start: string | null;
  before_end: string | null;
  after_start: string;
  after_end: string;
  before_status: string;
  after_status: string;
}
export interface SchedulingImpactJob {
  work_order_id: number;
  work_order_number: string;
  due_date: string | null;
  before_finish: string | null;
  after_finish: string | null;
  before_late_days: number | null;
  late_days: number | null;
  outcome: 'changed' | 'skipped' | 'blocked';
  reason: string | null;
  operations: SchedulingImpactOperation[];
}
export interface SchedulingImpactResponse {
  action: 'earliest' | 'shift';
  shift_days: number;
  plan_token: string | null;
  expires_at: string;
  summary: {
    selected_jobs: number;
    changed_jobs: number;
    changed_operations: number;
    skipped_jobs: number;
    blocked_jobs: number;
    late_jobs: number;
    overloaded_days: number;
  };
  jobs: SchedulingImpactJob[];
  capacity: Array<{
    work_center_id: number;
    work_center_code: string;
    date: string;
    capacity_hours: number;
    before_hours: number;
    after_hours: number;
    overload_hours: number;
    affected_jobs: Array<{ work_order_id: number; work_order_number: string }>;
  }>;
}
export interface SchedulingImpactApplyResponse {
  message: string;
  already_applied: boolean;
  applied_work_order_ids: number[];
  changed_operations: number;
}
