import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import api from '../../services/api';
import type { CronJobHealth, SystemHealth, WorkerStatus } from '../../types/systemHealth';
import { formatCentralDateTime } from '../../utils/centralTime';
import { variantClass } from '../../utils/statusColors';
import { DataTable, EmptyState, ErrorState, LoadingButton, StatusBadge } from '../ui';
import type { DataTableColumn } from '../ui';

/**
 * Admin > System Health — a READ-ONLY view of GET /admin/system-health.
 *
 * It shows whether the background worker is alive, every scheduled job with
 * whether it is armed, its schedule (shop time), next run and last result, and
 * the active company's notification backlog. Nothing here switches a job on:
 * that stays the worker's WORKER_CRON_JOBS setting. When Redis cannot be read
 * the server still answers 200 with `redis_available: false`, which renders as
 * "unknown" — only a failed request renders the error state.
 */

const WORKER_COLORS: Record<string, string> = {
  alive: variantClass.green,
  not_seen: variantClass.red,
  unknown: variantClass.slate,
};

const ARMED_COLORS: Record<string, string> = {
  on: variantClass.green,
  off: variantClass.amber,
  unknown: variantClass.slate,
};

const RESULT_COLORS: Record<string, string> = {
  success: variantClass.green,
  failed: variantClass.red,
};

const WORKER_LABEL: Record<WorkerStatus, string> = {
  alive: 'Running',
  not_seen: 'No worker seen',
  unknown: 'Unknown',
};

export function armedState(armed: boolean | null): 'on' | 'off' | 'unknown' {
  if (armed === null) return 'unknown';
  return armed ? 'on' : 'off';
}

/** `run_oee_auto_calc_job` -> `Run oee auto calc`. The raw name stays visible beside it. */
export function humanizeJobName(name: string): string {
  const base = name.replace(/_job$/, '').replace(/_/g, ' ').trim();
  return base ? base.charAt(0).toUpperCase() + base.slice(1) : name;
}

export function formatAge(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds)) return '—';
  if (seconds < 60) return `${Math.max(0, Math.round(seconds))} s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  return `${Math.round(minutes / 60)} h ago`;
}

function formatCounts(counts: Record<string, number | boolean>): string {
  const entries = Object.entries(counts);
  if (entries.length === 0) return '';
  return entries.map(([key, value]) => `${key.replace(/_/g, ' ')}: ${String(value)}`).join(', ');
}

function describeCronSpec(spec: string | null, known: boolean): string {
  if (!known) return 'Unknown';
  if (spec === null) return 'Not set (every job armed)';
  return spec;
}

const columns: Array<DataTableColumn<CronJobHealth>> = [
  {
    key: 'name',
    header: 'Job',
    sortable: true,
    accessor: row => row.name,
    render: row => (
      <div>
        <div className="font-medium text-fd-ink">{humanizeJobName(row.name)}</div>
        <div className="font-mono text-xs text-fd-mute">{row.name}</div>
      </div>
    ),
  },
  {
    key: 'armed',
    header: 'Armed',
    sortable: true,
    accessor: row => armedState(row.armed),
    render: row => <StatusBadge status={armedState(row.armed)} colorMap={ARMED_COLORS} />,
  },
  {
    key: 'schedule',
    header: 'Schedule',
    accessor: row => row.schedule,
    render: row => row.schedule ?? '—',
  },
  {
    key: 'next_run_at',
    header: 'Next run',
    sortable: true,
    accessor: row => row.next_run_at,
    render: row => (row.next_run_at ? formatCentralDateTime(row.next_run_at) : '—'),
    className: 'tabular-nums',
  },
  {
    key: 'last_run',
    header: 'Last run',
    sortable: true,
    accessor: row => row.last_run?.finished_at ?? row.last_run?.started_at ?? null,
    render: row =>
      row.last_run ? formatCentralDateTime(row.last_run.finished_at ?? row.last_run.started_at) : 'Never recorded',
    className: 'tabular-nums',
  },
  {
    key: 'result',
    header: 'Result',
    accessor: row => row.last_run?.outcome ?? null,
    render: row => {
      if (!row.last_run) return '—';
      const counts = formatCounts(row.last_run.counts);
      return (
        <div className="space-y-1">
          <StatusBadge status={row.last_run.outcome} colorMap={RESULT_COLORS} />
          {row.last_run.error_type && <div className="font-mono text-xs text-fd-red">{row.last_run.error_type}</div>}
          {counts && <div className="text-xs text-fd-mute">{counts}</div>}
        </div>
      );
    },
  },
];

function Stat({ label, value, children }: { label: string; value: React.ReactNode; children?: React.ReactNode }) {
  return (
    <div className="border border-fd-line bg-fd-sunken p-3">
      <dt className="text-xs font-medium uppercase tracking-wide text-fd-mute">{label}</dt>
      <dd className="mt-1 text-lg font-semibold tabular-nums text-fd-ink">{value}</dd>
      {children && <dd className="mt-1 text-xs text-fd-mute">{children}</dd>}
    </div>
  );
}

export default function SystemHealthTab() {
  const [data, setData] = useState<SystemHealth | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const requestVersion = useRef(0);

  const load = useCallback(async () => {
    const version = ++requestVersion.current;
    setLoading(true);
    setError(false);
    try {
      const result = await api.getSystemHealth();
      if (version === requestVersion.current) setData(result);
    } catch {
      if (version === requestVersion.current) {
        setData(null);
        setError(true);
      }
    } finally {
      if (version === requestVersion.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    // A company switch or sign-in change re-reads the active company's backlog.
    const reload = () => void load();
    window.addEventListener('werco:auth-token-changed', reload);
    return () => {
      requestVersion.current += 1;
      window.removeEventListener('werco:auth-token-changed', reload);
    };
  }, [load]);

  const header = (
    <div className="flex flex-wrap items-start justify-between gap-4">
      <div className="max-w-3xl">
        <h2 id="system-health-title" className="text-lg font-semibold text-fd-ink">
          System health
        </h2>
        <p className="mt-1 text-sm text-fd-mute">
          Whether the background worker is running, which scheduled jobs are switched on and how each one last ran. This
          page only reads; scheduled jobs are switched on by the worker&apos;s WORKER_CRON_JOBS setting.
        </p>
      </div>
      <LoadingButton variant="secondary" size="sm" loading={loading} onClick={() => void load()}>
        Refresh
      </LoadingButton>
    </div>
  );

  if (error && !data) {
    return (
      <section aria-labelledby="system-health-title" className="space-y-5">
        {header}
        <ErrorState message="System health could not be loaded." onRetry={() => void load()} />
      </section>
    );
  }

  if (!data) {
    return (
      <section aria-labelledby="system-health-title" className="space-y-5">
        {header}
        <div className="flex items-center justify-center py-12">
          <div className="spinner h-8 w-8" role="status" aria-label="Loading system health" />
        </div>
      </section>
    );
  }

  const { worker, backlog } = data;
  const workerKnown = data.redis_available && worker.status !== 'unknown';

  return (
    <section aria-labelledby="system-health-title" className="space-y-5">
      {header}

      {!data.redis_available && (
        <div role="status" className="border border-fd-amber/40 bg-fd-amber/10 p-3 text-sm text-fd-ink">
          Redis could not be read, so the worker and scheduled-job state is unknown. The notification backlog below is
          read from the database and is current.
        </div>
      )}

      <div>
        <h3 className="mb-2 text-sm font-semibold uppercase tracking-wide text-fd-mute">Background worker</h3>
        <dl className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-4">
          <Stat
            label="Status"
            value={<StatusBadge status={worker.status} colorMap={WORKER_COLORS} className="!text-sm" />}
          >
            {WORKER_LABEL[worker.status]}
            {worker.instance_count > 1 ? ` · ${worker.instance_count} instances` : ''}
          </Stat>
          <Stat label="Last heartbeat" value={formatAge(worker.heartbeat_age_seconds)}>
            {worker.last_heartbeat_at ? formatCentralDateTime(worker.last_heartbeat_at) : 'None recorded'}
          </Stat>
          <Stat
            label="Release"
            value={<span className="font-mono">{worker.release ? worker.release.slice(0, 8) : '—'}</span>}
          />
          <Stat
            label="WORKER_CRON_JOBS"
            value={<span className="font-mono text-base">{describeCronSpec(data.cron_spec, workerKnown)}</span>}
          >
            {data.schedule_timezone ? `Times in ${data.schedule_timezone}` : 'Schedule zone unknown'}
            {data.schedule_published_at ? ` · published ${formatCentralDateTime(data.schedule_published_at)}` : ''}
          </Stat>
        </dl>
      </div>

      <div>
        <h3 className="mb-2 text-sm font-semibold uppercase tracking-wide text-fd-mute">Scheduled jobs</h3>
        {data.cron_jobs.length === 0 ? (
          <EmptyState
            title={data.redis_available ? 'No scheduled jobs published' : 'Scheduled jobs unknown'}
            description={
              data.redis_available
                ? 'The worker has not published its scheduled jobs. Either no worker is deployed, or it runs a release older than this page.'
                : 'Redis could not be read, so the scheduled jobs cannot be listed.'
            }
          />
        ) : (
          <DataTable
            columns={columns}
            data={data.cron_jobs}
            rowKey={row => row.name}
            defaultSort={{ key: 'name', dir: 'asc' }}
            dense
            mobileCards={row => (
              <div className="space-y-1 text-sm">
                <div className="flex items-center justify-between gap-2">
                  <span className="font-medium text-fd-ink">{humanizeJobName(row.name)}</span>
                  <StatusBadge status={armedState(row.armed)} colorMap={ARMED_COLORS} />
                </div>
                <div className="text-fd-mute">{row.schedule ?? '—'}</div>
                <div className="text-fd-mute">
                  Next: {row.next_run_at ? formatCentralDateTime(row.next_run_at) : '—'}
                </div>
                <div className="text-fd-mute">
                  Last:{' '}
                  {row.last_run
                    ? `${formatCentralDateTime(row.last_run.finished_at ?? row.last_run.started_at)} · ${row.last_run.outcome}`
                    : 'Never recorded'}
                </div>
              </div>
            )}
          />
        )}
        <p className="mt-2 text-xs text-fd-mute">
          &quot;Unknown&quot; means no worker is running or Redis could not be read. Next run is shown only for armed
          jobs while the worker is running. Only scheduled runs are recorded.
        </p>
      </div>

      <div>
        <h3 className="mb-2 text-sm font-semibold uppercase tracking-wide text-fd-mute">Notification backlog</h3>
        <dl className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-4">
          <Stat label="Waiting to notify" value={backlog.unnotified_events}>
            Events the re-send sweeper will still pick up
          </Stat>
          <Stat label="Never notified" value={backlog.unnotified_events_stranded}>
            Older than 24 h, last 30 days
          </Stat>
          <Stat label="Queued digest items" value={backlog.queued_digest_items}>
            Waiting for the daily digest
          </Stat>
          <Stat label="Failed emails" value={backlog.failed_emails}>
            Last {backlog.failed_email_window_days} days ·{' '}
            <Link to="/notifications" className="text-werco-600 underline">
              View email activity
            </Link>
          </Stat>
        </dl>
      </div>

      <p className="text-xs text-fd-mute">Read {formatCentralDateTime(data.generated_at)}</p>
    </section>
  );
}
