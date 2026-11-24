/**
 * GET /admin/system-health — mirrors backend/app/schemas/system_health.py
 * (SystemHealthResponse). Admin only, a pure read. Datetimes are UTC ISO strings
 * ending in Z; render them with utils/centralTime.
 */

/**
 * alive    — a worker heartbeat exists.
 * not_seen — Redis answered and holds no heartbeat (no worker running).
 * unknown  — Redis could not be read.
 */
export type WorkerStatus = 'alive' | 'not_seen' | 'unknown';

export interface WorkerHealth {
  status: WorkerStatus;
  heartbeat_age_seconds: number | null;
  last_heartbeat_at: string | null;
  release: string | null;
  instance_count: number;
}

export interface CronRunResult {
  started_at: string | null;
  finished_at: string | null;
  /** 'running' = started and not finished normally; long after its start it means the run was cut off. */
  outcome: 'success' | 'failed' | 'running';
  /** Exception class name only on a failure. */
  error_type: string | null;
  /** Flat numeric/boolean values the job returned. */
  counts: Record<string, number | boolean>;
}

export interface CronJobHealth {
  name: string;
  /** null = unknown (no live worker, or Redis could not be read). */
  armed: boolean | null;
  schedule: string | null;
  next_run_at: string | null;
  last_run: CronRunResult | null;
}

export interface CompanyBacklog {
  unnotified_events: number;
  unnotified_events_stranded: number;
  queued_digest_items: number;
  failed_emails: number;
  failed_email_window_days: number;
}

export interface SystemHealth {
  generated_at: string;
  redis_available: boolean;
  worker: WorkerHealth;
  schedule_timezone: string | null;
  schedule_published_at: string | null;
  /** The worker's WORKER_CRON_JOBS value; null = unset (arms every cron). */
  cron_spec: string | null;
  cron_jobs: CronJobHealth[];
  backlog: CompanyBacklog;
}

export const OEE_CRON_JOB_NAME = 'run_oee_auto_calc_job';
