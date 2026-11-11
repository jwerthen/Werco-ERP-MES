/**
 * Banner-zone core widgets. Banners toggle on and off but always render in registry
 * order. SetupNudge comes from the cockpit BARREL on purpose (the page suites stub it there).
 */
import React from 'react';
import { Link } from 'react-router-dom';
import { ExclamationTriangleIcon } from '@heroicons/react/24/outline';
import { SetupNudge } from '../../../cockpit';
import { MorningBriefBanner } from '../../../ai/MorningBriefBanner';
import { useDashboardWidgetEnv } from '../../DashboardWidgetContext';
import { useCoreDashboard } from './CoreDashboardContext';
import { useDashboardAlerts } from './alerts';

/** "Needs attention": the first exception, with a review link when the viewer can open it. */
export function AttentionBanner() {
  const alerts = useDashboardAlerts();
  const { shopError } = useCoreDashboard();
  const { canOpen } = useDashboardWidgetEnv();
  const first = alerts[0];
  if (!first) return null;
  return (
    <section
      aria-label="First exception to review"
      className="flex flex-wrap items-center justify-between gap-3 rounded border border-red-500/40 bg-red-500/10 p-4"
    >
      <div>
        <p className="text-sm font-medium text-red-200">Needs attention</p>
        <p className="font-semibold text-fd-ink">{first.message}</p>
        <p className="text-xs text-fd-mute">
          {shopError ? 'Based on last verified shop data' : 'Review the affected records and choose the next action.'}
        </p>
      </div>
      {canOpen(first.link) && (
        <Link className="btn-primary" to={first.link}>
          Review affected records
        </Link>
      )}
    </section>
  );
}

/** Setup nudge — admins only, dismissible, deep-links to /setup. */
export function SetupNudgeBanner() {
  return <SetupNudge />;
}

export function MorningBriefWidget() {
  return <MorningBriefBanner />;
}

/** Alerts — compact chip row. A chip whose target the viewer cannot open is plain text. */
export function AlertChipsBanner() {
  const alerts = useDashboardAlerts();
  const { canOpen } = useDashboardWidgetEnv();
  if (alerts.length === 0) return null;
  return (
    <div className="flex flex-wrap items-center gap-2">
      {alerts.map((alert, idx) => {
        const Icon = alert.icon || ExclamationTriangleIcon;
        const className = `inline-flex items-center gap-1.5 rounded-sm border px-2.5 py-1 text-xs font-medium transition-colors ${
          alert.type === 'error'
            ? 'bg-red-500/10 border-red-500/30 text-red-300 hover:bg-fd-red/20'
            : alert.type === 'warning'
              ? 'bg-amber-500/10 border-amber-500/30 text-amber-300 hover:bg-fd-amber/20'
              : 'bg-blue-500/10 border-blue-500/30 text-blue-300 hover:bg-blue-500/20'
        }`;
        const body = (
          <>
            <Icon className="h-4 w-4 flex-shrink-0" />
            <span>{alert.message}</span>
          </>
        );
        return canOpen(alert.link) ? (
          <Link key={idx} to={alert.link} className={className}>
            {body}
          </Link>
        ) : (
          <span key={idx} className={className}>
            {body}
          </span>
        );
      })}
    </div>
  );
}
