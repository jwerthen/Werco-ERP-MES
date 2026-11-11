/**
 * Delivery & Yield KPIs — `GET /analytics/kpis?period=30d` (the `deliveryKpis` source).
 * ADMIN/MANAGER/SUPERVISOR only on the server, so the widget is offered to exactly those
 * roles. The call recomputes ten KPIs on every hit, so the source is cached for ten minutes
 * with a one-minute hard minimum; the panel shows the server's `generated_at` so a cached
 * value is honest about its age.
 *
 * A KPI the server cannot compute arrives as `value: null` and reads "n/a" — never 0.
 */
import React from 'react';
import { ArrowTrendingDownIcon, ArrowTrendingUpIcon, ChartBarIcon, MinusIcon } from '@heroicons/react/24/outline';
import { CockpitPanel } from '../../../cockpit';
import { EmptyState } from '../../../ui';
import { getMetricTrend, METRIC_POLARITY, type MetricKey } from '../../../analytics/metricPolarity';
import { formatCentralDateTime } from '../../../../utils/centralTime';
import type { KpiDashboardResponse, KpiValue } from '../../../../types/dashboardWidgetData';
import { useDashboardSource } from '../../DashboardWidgetContext';
import { PanelAction, SourceBody, panelSubtitle } from './shared';

type KpiFormat = 'percent' | 'hours';

const KPI_ROWS: { metric: MetricKey & keyof KpiDashboardResponse; label: string; format: KpiFormat }[] = [
  { metric: 'on_time_delivery_ship', label: 'On-time delivery (shipped)', format: 'percent' },
  { metric: 'otif', label: 'OTIF', format: 'percent' },
  { metric: 'first_pass_yield', label: 'First-pass yield', format: 'percent' },
  { metric: 'scrap_rate', label: 'Scrap rate', format: 'percent' },
  { metric: 'backlog_hours', label: 'Backlog', format: 'hours' },
];

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
  const trend = kpi && value != null ? getMetricTrend(metric, kpi) : null;
  const TrendIcon =
    trend?.direction === 'up' ? ArrowTrendingUpIcon : trend?.direction === 'down' ? ArrowTrendingDownIcon : MinusIcon;
  const trendWord = trend?.direction === 'up' ? 'up' : trend?.direction === 'down' ? 'down' : 'unchanged';

  return (
    <li className="flex items-center gap-2 py-1.5 first:pt-0">
      <span className="min-w-0 flex-1 truncate text-xs text-slate-300">{label}</span>
      {target != null && (
        <span
          className={`flex-shrink-0 rounded-sm px-1.5 py-0.5 text-[10px] tabular-nums ${
            onTarget ? 'bg-green-500/20 text-emerald-400' : 'bg-amber-500/20 text-amber-400'
          }`}
        >
          tgt {formatKpi(target, format)}
        </span>
      )}
      {trend && (
        <span role="img" aria-label={`${label} ${trendWord} from the prior period`} className="flex-shrink-0">
          <TrendIcon className={`h-4 w-4 ${trend.color}`} aria-hidden="true" />
        </span>
      )}
      <span
        className={`w-14 flex-shrink-0 text-right text-sm font-bold tabular-nums ${value == null ? 'text-slate-500' : 'text-white'}`}
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
          if (KPI_ROWS.every(row => kpis[row.metric] == null || (kpis[row.metric] as KpiValue).value == null)) {
            return (
              <EmptyState
                icon={ChartBarIcon}
                title="Not enough activity yet"
                description="Delivery and yield KPIs appear once work has shipped or completed in the last 30 days."
              />
            );
          }
          return (
            <ul className="divide-y divide-fd-line">
              {KPI_ROWS.map(row => (
                <KpiRow
                  key={row.metric}
                  metric={row.metric}
                  label={row.label}
                  format={row.format}
                  kpi={kpis[row.metric] as KpiValue | null | undefined}
                />
              ))}
            </ul>
          );
        }}
      </SourceBody>
    </CockpitPanel>
  );
}
