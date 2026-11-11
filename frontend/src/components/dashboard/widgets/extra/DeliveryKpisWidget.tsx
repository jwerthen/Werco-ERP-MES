/**
 * Delivery & Yield KPIs — `GET /analytics/kpis?period=30d` (the `deliveryKpis` source).
 * ADMIN/MANAGER/SUPERVISOR only on the server, so the widget is offered to exactly those
 * roles. The call recomputes ten KPIs on every hit, so the source is cached for ten minutes
 * with a one-minute hard minimum; the panel shows the server's `generated_at` so a cached
 * value is honest about its age.
 *
 * A KPI the server cannot compute arrives as `value: null` and reads "n/a" — never 0.
 *
 * Four rows are measured over the 30-day period. Backlog is not: `_calculate_backlog_kpi`
 * is the open released/in-progress hours right now, always a number (0 when nothing is
 * open), with no prior value. So it is labelled as a snapshot, never decides the "not enough
 * activity" state (and stays visible under it), and — like any KPI without a prior value —
 * shows no trend arrow rather than an "unchanged from the prior period" claim.
 *
 * Target status is carried by an icon and screen-reader text as well as color (WCAG 1.4.1).
 */
import React from 'react';
import {
  ArrowTrendingDownIcon,
  ArrowTrendingUpIcon,
  ChartBarIcon,
  CheckIcon,
  ExclamationTriangleIcon,
  MinusIcon,
} from '@heroicons/react/24/outline';
import { CockpitPanel } from '../../../cockpit';
import { EmptyState } from '../../../ui';
import { getMetricTrend, METRIC_POLARITY, type MetricKey } from '../../../analytics/metricPolarity';
import { formatCentralDateTime } from '../../../../utils/centralTime';
import { variantClass } from '../../../../utils/statusColors';
import type { KpiDashboardResponse, KpiValue } from '../../../../types/dashboardWidgetData';
import { useDashboardSource } from '../../DashboardWidgetContext';
import { PanelAction, SourceBody, panelSubtitle } from './shared';

type KpiFormat = 'percent' | 'hours';

interface KpiRowSpec {
  metric: MetricKey & keyof KpiDashboardResponse;
  label: string;
  format: KpiFormat;
  /** A current snapshot, not a value measured over the period. */
  snapshot?: boolean;
}

const KPI_ROWS: KpiRowSpec[] = [
  { metric: 'on_time_delivery_ship', label: 'On-time delivery (shipped)', format: 'percent' },
  { metric: 'otif', label: 'OTIF', format: 'percent' },
  { metric: 'first_pass_yield', label: 'First-pass yield', format: 'percent' },
  { metric: 'scrap_rate', label: 'Scrap rate', format: 'percent' },
  { metric: 'backlog_hours', label: 'Backlog (now)', format: 'hours', snapshot: true },
];
const PERIOD_ROWS = KPI_ROWS.filter(row => !row.snapshot);
const SNAPSHOT_ROWS = KPI_ROWS.filter(row => row.snapshot);

const TARGET_STATUS = {
  met: { text: 'meets target', Icon: CheckIcon, className: 'bg-green-500/20 text-emerald-400' },
  missed: { text: 'misses target', Icon: ExclamationTriangleIcon, className: 'bg-amber-500/20 text-amber-400' },
} as const;

function formatKpi(value: number | null | undefined, format: KpiFormat): string {
  if (value == null || !Number.isFinite(value)) return 'n/a';
  return format === 'percent' ? `${value.toFixed(1)}%` : `${value.toFixed(0)}h`;
}

function KpiRow({
  metric,
  label,
  format,
  kpi,
}: {
  metric: MetricKey;
  label: string;
  format: KpiFormat;
  kpi: KpiValue | null | undefined;
}) {
  const value = kpi?.value ?? null;
  const target = kpi?.target ?? null;
  const higherIsBetter = METRIC_POLARITY[metric] === 'higher';
  const onTarget = value != null && target != null ? (higherIsBetter ? value >= target : value <= target) : null;
  // n/a against a target is neither on nor off target: neutral chip, no status.
  const status = onTarget == null ? null : onTarget ? TARGET_STATUS.met : TARGET_STATUS.missed;
  const StatusIcon = status?.Icon;
  // A trend needs a real comparison (as on the Analytics page): no prior value → no arrow.
  const trend =
    kpi && value != null && kpi.prior_value != null && kpi.change_pct != null ? getMetricTrend(metric, kpi) : null;
  const TrendIcon =
    trend?.direction === 'up' ? ArrowTrendingUpIcon : trend?.direction === 'down' ? ArrowTrendingDownIcon : MinusIcon;
  const trendWord = trend?.direction === 'up' ? 'up' : trend?.direction === 'down' ? 'down' : 'unchanged';

  return (
    <li className="flex items-center gap-2 py-1.5 first:pt-0">
      <span className="min-w-0 flex-1 truncate text-xs text-slate-300">{label}</span>
      {target != null && (
        <span
          className={`inline-flex flex-shrink-0 items-center gap-0.5 rounded-sm px-1.5 py-0.5 text-[10px] tabular-nums ${
            status ? status.className : variantClass.slate
          }`}
        >
          {StatusIcon && <StatusIcon className="h-3 w-3" aria-hidden="true" />}
          {status && <span className="sr-only">{status.text}, </span>}
          tgt {formatKpi(target, format)}
        </span>
      )}
      {trend && (
        <span role="img" aria-label={`${label} ${trendWord} from the prior period`} className="flex-shrink-0">
          <TrendIcon className={`h-4 w-4 ${trend.color}`} aria-hidden="true" />
        </span>
      )}
      <span
        className={`w-14 flex-shrink-0 text-right text-sm font-bold tabular-nums ${value == null ? 'text-slate-400' : 'text-white'}`}
      >
        {formatKpi(value, format)}
      </span>
    </li>
  );
}

export function DeliveryKpisWidget() {
  const state = useDashboardSource<KpiDashboardResponse>('deliveryKpis');
  const generatedAt = !state.forbidden && state.data ? state.data.generated_at : null;

  return (
    <CockpitPanel
      title="Delivery & Yield KPIs"
      subtitle={panelSubtitle(
        [state],
        generatedAt ? `Last 30 days · as of ${formatCentralDateTime(generatedAt, { year: undefined })}` : 'Last 30 days'
      )}
      headerExtra={
        state.forbidden ? undefined : <PanelAction href="/analytics" label="Analytics" ariaLabel="Open analytics" />
      }
    >
      <SourceBody state={state} loadingText="Loading delivery KPIs…" errorTitle="Delivery KPIs are unavailable">
        {kpis => {
          const kpiFor = (row: KpiRowSpec) => kpis[row.metric] as KpiValue | null | undefined;
          const renderRow = (row: KpiRowSpec) => (
            <KpiRow key={row.metric} metric={row.metric} label={row.label} format={row.format} kpi={kpiFor(row)} />
          );
          // Only the period KPIs decide "not enough activity": the backlog snapshot is always a
          // number, so it would keep this state from ever appearing — and it stays visible.
          if (PERIOD_ROWS.every(row => kpiFor(row)?.value == null)) {
            return (
              <>
                <EmptyState
                  icon={ChartBarIcon}
                  title="Not enough activity yet"
                  description="Delivery and yield KPIs appear once work has shipped or completed in the last 30 days."
                />
                <ul className="mt-2 divide-y divide-fd-line">{SNAPSHOT_ROWS.map(renderRow)}</ul>
              </>
            );
          }
          return <ul className="divide-y divide-fd-line">{KPI_ROWS.map(renderRow)}</ul>;
        }}
      </SourceBody>
    </CockpitPanel>
  );
}
