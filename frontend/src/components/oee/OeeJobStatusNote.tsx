import React, { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import api from '../../services/api';
import { useAuth } from '../../context/AuthContext';
import { OEE_CRON_JOB_NAME } from '../../types/systemHealth';
import type { CronJobHealth, SystemHealth } from '../../types/systemHealth';
import { formatCentralDateTime } from '../../utils/centralTime';

const FALLBACK =
  'If those entries exist but samples remain missing, ask an administrator to check the nightly OEE job.';

/**
 * The OEE empty-state's last line. Everyone but an Admin sees the static advice
 * (GET /admin/system-health is Admin-only, so nobody else is asked for it). An
 * Admin sees the live state of the nightly OEE job instead: armed or off, and
 * how its last run went, with a link to Admin Settings > System Health. Any
 * failure, or a state the server reports as unknown, falls back to the advice.
 */
export default function OeeJobStatusNote() {
  const { user } = useAuth();
  const isAdmin = user?.role === 'admin' || user?.role === 'platform_admin' || Boolean(user?.is_superuser);
  const [health, setHealth] = useState<SystemHealth | null>(null);

  useEffect(() => {
    if (!isAdmin) return undefined;
    let active = true;
    api
      .getSystemHealth()
      .then(result => {
        if (active) setHealth(result);
      })
      .catch(() => {
        if (active) setHealth(null);
      });
    return () => {
      active = false;
    };
  }, [isAdmin]);

  const job: CronJobHealth | undefined = health?.cron_jobs.find(row => row.name === OEE_CRON_JOB_NAME);
  if (!isAdmin || !health || !job || job.armed === null) {
    return (
      <p className="text-sm text-slate-300">
        Check the selected dates and the work center links on completed time-clock and downtime entries. {FALLBACK}
      </p>
    );
  }

  const lastRun = job.last_run;
  return (
    <p className="text-sm text-slate-300">
      Check the selected dates and the work center links on completed time-clock and downtime entries. The nightly OEE
      job is <strong className="text-fd-ink">{job.armed ? 'switched on' : 'switched off'}</strong>
      {job.schedule ? ` (${job.schedule})` : ''}
      {lastRun
        ? lastRun.outcome === 'running'
          ? `; its last run started at ${formatCentralDateTime(lastRun.started_at)} and has not finished`
          : `; its last run ${lastRun.outcome === 'success' ? 'succeeded' : 'failed'} at ${formatCentralDateTime(
              lastRun.finished_at ?? lastRun.started_at
            )}`
        : '; no run has been recorded'}
      .{' '}
      <Link to="/admin/settings?tab=health" className="text-werco-400 underline">
        View System Health
      </Link>
    </p>
  );
}
