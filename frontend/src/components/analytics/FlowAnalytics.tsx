/**
 * FlowAnalytics — the Analytics "Flow" view (Lean Phase 1 / issue #88).
 *
 * Renders the measured-flow picture for the period:
 *   1. Flow KPI strip — avg/median lead time, Little's Law throughput days,
 *      PCE, queue hours, average WIP.
 *   2. WIP aging table (oldest first) + queue time by work center.
 *   3. FPY / RTY overall + per-part and per-work-center tables.
 *   4. Scrap Pareto — quantity-share bars + cumulative % line on ONE 0–100%
 *      axis (quantity and cost in the tooltip), top 10 buckets.
 *   5. Adoption — digital completion / clock-in coverage / backfill rate with
 *      the weekly trend, plus hidden-factory (rework, maintenance mix,
 *      MTBF/MTTR).
 *
 * RBAC mirrors the backend: flow/wip-aging/adoption are ADMIN/MANAGER/
 * SUPERVISOR (work_orders:release); fpy/scrap-pareto additionally allow
 * QUALITY (quality:approve). Sections a role cannot load are not fetched and
 * not rendered.
 *
 * Chart palette (validated against the dark panel surface with the dataviz
 * six-checks script): Pareto bars #C8352B (the app-wide scrap red) +
 * cumulative line #D97706; adoption lines #059669 / #3B82F6 / #EF4444.
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  ArrowPathIcon,
  BoltIcon,
  ClockIcon,
  CubeIcon,
  QueueListIcon,
  ScaleIcon,
  TagIcon,
  WrenchScrewdriverIcon,
} from '@heroicons/react/24/outline';
import {
  Bar,
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import api from '../../services/api';
import { usePermissions } from '../../hooks/usePermissions';
import { MiniStat, MiniStatStrip, CockpitPanel } from '../cockpit';
import {
  DataTable,
  DataTableColumn,
  EmptyState,
  ErrorState,
  StatusBadge,
  statusVariantClass,
} from '../ui';
import { formatCentralDate } from '../../utils/centralTime';
import type {
  AdoptionMetricsResponse,
  FlowMetricsResponse,
  FpyGroup,
  FpyResponse,
  QueueTimeByWorkCenter,
  ScrapParetoResponse,
  WipAgingItem,
  WipAgingResponse,
  WorkCenterReliability,
} from '../../types/leanAnalytics';

const PARETO_BAR = '#C8352B';
const PARETO_LINE = '#D97706';
const ADOPTION_DIGITAL = '#059669';
const ADOPTION_COVERAGE = '#3B82F6';
const ADOPTION_BACKFILL = '#EF4444';

const CHART_GRID = '#334155';
const CHART_TICK = { fontSize: 11, fill: '#94a3b8' } as const;
const CHART_TOOLTIP_STYLE = {
  backgroundColor: '#1a1f2e',
  border: '1px solid #334155',
  borderRadius: '3px',
  color: '#e2e8f0',
} as const;

/** "—" for the null/undefined metrics the backend deliberately can't compute. */
function fmt(value: number | null | undefined, digits = 1, suffix = ''): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return `${value.toFixed(digits)}${suffix}`;
}

/** Aging chip class — statusColors semantics: amber past 7 days, red past 14. */
function agingClass(days: number | null | undefined): string {
  if (days === null || days === undefined) return statusVariantClass.slate;
  if (days > 14) return statusVariantClass.red;
  if (days > 7) return statusVariantClass.amber;
  return statusVariantClass.green;
}

function DaysBadge({ days }: { days: number | null }) {
  return (
    <span className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium tabular-nums ${agingClass(days)}`}>
      {days === null ? '—' : `${days.toFixed(1)}d`}
    </span>
  );
}

const formatWeekLabel = (value: string) =>
  formatCentralDate(value, { month: 'short', day: 'numeric', year: undefined });

interface SectionState<T> {
  data: T | null;
  error: boolean;
}

interface FlowAnalyticsProps {
  period: string;
}

export default function FlowAnalytics({ period }: FlowAnalyticsProps) {
  const navigate = useNavigate();
  const { can, canAny } = usePermissions();
  // Backend role gates: flow/wip-aging/adoption -> ADMIN/MANAGER/SUPERVISOR
  // (= work_orders:release); fpy/scrap-pareto add QUALITY (= quality:approve).
  const canViewFlow = can('work_orders:release');
  const canViewYield = canAny(['work_orders:release', 'quality:approve']);

  const [loading, setLoading] = useState(true);
  const [flow, setFlow] = useState<SectionState<FlowMetricsResponse>>({ data: null, error: false });
  const [wip, setWip] = useState<SectionState<WipAgingResponse>>({ data: null, error: false });
  const [fpy, setFpy] = useState<SectionState<FpyResponse>>({ data: null, error: false });
  const [pareto, setPareto] = useState<SectionState<ScrapParetoResponse>>({ data: null, error: false });
  const [adoption, setAdoption] = useState<SectionState<AdoptionMetricsResponse>>({ data: null, error: false });

  const loadAll = useCallback(async () => {
    if (!canViewFlow && !canViewYield) {
      setLoading(false);
      return;
    }
    setLoading(true);
    const settle = <T,>(result: PromiseSettledResult<T>, set: (next: SectionState<T>) => void) => {
      if (result.status === 'fulfilled') set({ data: result.value, error: false });
      else set({ data: null, error: true });
    };
    // Sections load independently — one failing endpoint must not blank the rest.
    const [flowRes, wipRes, adoptionRes, fpyRes, paretoRes] = await Promise.allSettled([
      canViewFlow ? api.getFlowMetrics(period) : Promise.reject(new Error('forbidden')),
      canViewFlow ? api.getWipAging() : Promise.reject(new Error('forbidden')),
      canViewFlow ? api.getAdoptionMetrics(period) : Promise.reject(new Error('forbidden')),
      canViewYield ? api.getFpyAnalytics({ period }) : Promise.reject(new Error('forbidden')),
      canViewYield ? api.getScrapPareto({ period }) : Promise.reject(new Error('forbidden')),
    ]);
    settle(flowRes, setFlow);
    settle(wipRes, setWip);
    settle(adoptionRes, setAdoption);
    settle(fpyRes, setFpy);
    settle(paretoRes, setPareto);
    setLoading(false);
  }, [period, canViewFlow, canViewYield]);

  useEffect(() => {
    void loadAll();
  }, [loadAll]);

  // ---- WIP aging table ----
  const wipColumns = useMemo<Array<DataTableColumn<WipAgingItem>>>(
    () => [
      {
        key: 'work_order_number',
        header: 'Work Order',
        sortable: true,
        className: 'font-medium',
        accessor: (item) => item.work_order_number,
      },
      {
        key: 'part_number',
        header: 'Part',
        sortable: true,
        accessor: (item) => item.part_number ?? '',
        render: (item) => item.part_number || '-',
      },
      {
        key: 'customer_name',
        header: 'Customer',
        sortable: true,
        className: 'text-sm text-slate-400 max-w-[160px] truncate',
        accessor: (item) => item.customer_name ?? '',
        render: (item) => item.customer_name || '-',
      },
      {
        key: 'status',
        header: 'Status',
        sortable: true,
        accessor: (item) => item.status,
        csv: (item) => item.status.replace(/_/g, ' '),
        render: (item) => <StatusBadge status={item.status} />,
      },
      {
        key: 'current_operation',
        header: 'Current Op',
        className: 'text-sm',
        accessor: (item) => item.current_operation_number ?? '',
        csv: (item) =>
          item.current_operation_number
            ? `${item.current_operation_number} ${item.current_operation_name ?? ''}`.trim()
            : '',
        render: (item) =>
          item.current_operation_number ? (
            <span>
              <span className="font-mono">{item.current_operation_number}</span>{' '}
              {item.current_operation_name || ''}
              {item.current_work_center_name && (
                <span className="text-slate-500"> · {item.current_work_center_name}</span>
              )}
            </span>
          ) : (
            <span className="text-slate-500">—</span>
          ),
      },
      {
        key: 'days_in_current_operation',
        header: 'Days in Op',
        sortable: true,
        align: 'right',
        headerClassName: 'text-right',
        accessor: (item) => item.days_in_current_operation ?? -1,
        csv: (item) => (item.days_in_current_operation === null ? '' : item.days_in_current_operation.toFixed(1)),
        render: (item) => <DaysBadge days={item.days_in_current_operation} />,
      },
      {
        key: 'days_since_release',
        header: 'Days Since Release',
        sortable: true,
        align: 'right',
        headerClassName: 'text-right',
        accessor: (item) => item.days_since_release ?? -1,
        csv: (item) => (item.days_since_release === null ? '' : item.days_since_release.toFixed(1)),
        render: (item) => <DaysBadge days={item.days_since_release} />,
      },
      {
        key: 'due_date',
        header: 'Due',
        sortable: true,
        accessor: (item) => item.due_date ?? '',
        csv: (item) => (item.due_date ? formatCentralDate(item.due_date) : ''),
        render: (item) =>
          item.due_date ? (
            <span className="text-sm tabular-nums">
              {formatCentralDate(item.due_date, { month: 'short', day: 'numeric' })}
              {item.days_to_due !== null && (
                <span className={item.days_to_due < 0 ? 'ml-1 font-semibold text-red-400' : 'ml-1 text-slate-500'}>
                  {item.days_to_due < 0 ? `${Math.abs(item.days_to_due)}d late` : `${item.days_to_due}d`}
                </span>
              )}
            </span>
          ) : (
            <span className="text-slate-500">—</span>
          ),
      },
    ],
    []
  );

  // ---- FPY tables ----
  const fpyColumns = useCallback(
    (label: string, withRty: boolean): Array<DataTableColumn<FpyGroup>> => [
      {
        key: 'key',
        header: label,
        sortable: true,
        className: 'font-medium',
        accessor: (row) => row.key,
        render: (row) => (
          <span>
            {row.key}
            {row.name && <span className="text-slate-500"> · {row.name}</span>}
          </span>
        ),
      },
      {
        key: 'operations',
        header: 'Ops',
        sortable: true,
        align: 'right',
        headerClassName: 'text-right',
        className: 'tabular-nums',
        accessor: (row) => row.operations,
      },
      {
        key: 'units_attempted',
        header: 'Attempted',
        sortable: true,
        align: 'right',
        headerClassName: 'text-right',
        className: 'tabular-nums',
        accessor: (row) => row.units_attempted,
      },
      {
        key: 'first_pass_units',
        header: 'First Pass',
        sortable: true,
        align: 'right',
        headerClassName: 'text-right',
        className: 'tabular-nums',
        accessor: (row) => row.first_pass_units,
      },
      {
        key: 'fpy_pct',
        header: 'FPY',
        sortable: true,
        align: 'right',
        headerClassName: 'text-right',
        accessor: (row) => row.fpy_pct ?? -1,
        csv: (row) => (row.fpy_pct === null ? '' : row.fpy_pct.toFixed(1)),
        render: (row) => (
          <span
            className={`font-semibold tabular-nums ${
              row.fpy_pct === null
                ? 'text-slate-500'
                : row.fpy_pct >= 95
                  ? 'text-fd-green'
                  : row.fpy_pct >= 85
                    ? 'text-fd-amber'
                    : 'text-fd-red'
            }`}
          >
            {fmt(row.fpy_pct, 1, '%')}
          </span>
        ),
      },
      ...(withRty
        ? [
            {
              key: 'rty_pct',
              header: 'RTY',
              sortable: true,
              align: 'right',
              headerClassName: 'text-right',
              className: 'tabular-nums',
              accessor: (row) => row.rty_pct ?? -1,
              csv: (row) => (row.rty_pct === null ? '' : row.rty_pct.toFixed(1)),
              render: (row) => <span className="tabular-nums">{fmt(row.rty_pct, 1, '%')}</span>,
            } as DataTableColumn<FpyGroup>,
          ]
        : []),
      {
        key: 'work_orders',
        header: 'WOs',
        sortable: true,
        align: 'right',
        headerClassName: 'text-right',
        className: 'tabular-nums',
        accessor: (row) => row.work_orders,
      },
    ],
    []
  );

  const fpyPartColumns = useMemo(() => fpyColumns('Part', true), [fpyColumns]);
  const fpyWorkCenterColumns = useMemo(() => fpyColumns('Work Center', false), [fpyColumns]);

  const queueColumns = useMemo<Array<DataTableColumn<QueueTimeByWorkCenter>>>(
    () => [
      {
        key: 'work_center',
        header: 'Work Center',
        sortable: true,
        className: 'font-medium',
        accessor: (row) => row.work_center_code ?? row.work_center_name ?? '',
        render: (row) => (
          <span>
            {row.work_center_code || row.work_center_name || `WC-${row.work_center_id}`}
            {row.work_center_code && row.work_center_name && (
              <span className="text-slate-500"> · {row.work_center_name}</span>
            )}
          </span>
        ),
      },
      {
        key: 'avg_queue_hours',
        header: 'Avg Queue',
        sortable: true,
        align: 'right',
        headerClassName: 'text-right',
        className: 'tabular-nums',
        accessor: (row) => row.avg_queue_hours ?? -1,
        csv: (row) => (row.avg_queue_hours === null ? '' : row.avg_queue_hours.toFixed(1)),
        render: (row) => fmt(row.avg_queue_hours, 1, 'h'),
      },
      {
        key: 'max_queue_hours',
        header: 'Max',
        sortable: true,
        align: 'right',
        headerClassName: 'text-right',
        className: 'tabular-nums',
        accessor: (row) => row.max_queue_hours ?? -1,
        csv: (row) => (row.max_queue_hours === null ? '' : row.max_queue_hours.toFixed(1)),
        render: (row) => fmt(row.max_queue_hours, 1, 'h'),
      },
      {
        key: 'samples',
        header: 'Samples',
        align: 'right',
        headerClassName: 'text-right',
        className: 'tabular-nums text-slate-400',
        accessor: (row) => row.samples,
        csv: (row) => `${row.from_ready_events}/${row.samples}`,
        // "from ready events / total" — how much of this row is event-measured.
        render: (row) => `${row.from_ready_events}/${row.samples}`,
      },
    ],
    []
  );

  const reliabilityColumns = useMemo<Array<DataTableColumn<WorkCenterReliability>>>(
    () => [
      {
        key: 'work_center',
        header: 'Work Center',
        sortable: true,
        className: 'font-medium',
        accessor: (row) => row.work_center_code ?? row.work_center_name ?? '',
        render: (row) => row.work_center_code || row.work_center_name || `WC-${row.work_center_id}`,
      },
      {
        key: 'unplanned_downtime_events',
        header: 'Unplanned Events',
        sortable: true,
        align: 'right',
        headerClassName: 'text-right',
        className: 'tabular-nums',
        accessor: (row) => row.unplanned_downtime_events,
      },
      {
        key: 'unplanned_downtime_hours',
        header: 'Down Hours',
        sortable: true,
        align: 'right',
        headerClassName: 'text-right',
        className: 'tabular-nums',
        accessor: (row) => row.unplanned_downtime_hours,
        csv: (row) => row.unplanned_downtime_hours.toFixed(1),
        render: (row) => fmt(row.unplanned_downtime_hours, 1, 'h'),
      },
      {
        key: 'mtbf_hours',
        header: 'MTBF',
        sortable: true,
        align: 'right',
        headerClassName: 'text-right',
        className: 'tabular-nums',
        accessor: (row) => row.mtbf_hours ?? -1,
        csv: (row) => (row.mtbf_hours === null ? '' : row.mtbf_hours.toFixed(1)),
        render: (row) => fmt(row.mtbf_hours, 1, 'h'),
      },
      {
        key: 'mttr_hours',
        header: 'MTTR',
        sortable: true,
        align: 'right',
        headerClassName: 'text-right',
        className: 'tabular-nums',
        accessor: (row) => row.mttr_hours ?? -1,
        csv: (row) => (row.mttr_hours === null ? '' : row.mttr_hours.toFixed(1)),
        render: (row) => fmt(row.mttr_hours, 1, 'h'),
      },
    ],
    []
  );

  // ---- Pareto chart data (top 10 buckets, single 0–100% axis) ----
  const paretoData = useMemo(() => {
    const buckets = pareto.data?.buckets ?? [];
    return buckets.slice(0, 10).map((bucket) => ({
      label: bucket.code === 'unspecified' ? 'unspecified' : bucket.code,
      name: bucket.name ?? (bucket.code === 'unspecified' ? 'No code recorded' : bucket.code),
      share: bucket.percentage,
      cumulative: bucket.cumulative_pct,
      quantity: bucket.quantity,
      cost: bucket.cost,
    }));
  }, [pareto.data]);

  const paretoCostlessBuckets = useMemo(
    () => (pareto.data?.buckets ?? []).filter((b) => b.quantity > 0 && b.cost === 0).length,
    [pareto.data]
  );

  if (!canViewFlow && !canViewYield) {
    return (
      <div className="card">
        <p className="text-sm text-slate-400">Flow analytics are not available for your role.</p>
      </div>
    );
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-48">
        <ArrowPathIcon className="h-8 w-8 animate-spin text-werco-primary" />
      </div>
    );
  }

  const summary = flow.data?.summary ?? null;

  return (
    <div className="space-y-4">
      {/* 1 — Flow KPI strip */}
      {canViewFlow &&
        (flow.error ? (
          <ErrorState message="Could not load flow metrics." onRetry={loadAll} />
        ) : (
          summary && (
            <>
              <MiniStatStrip className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-2">
                <MiniStat
                  icon={ClockIcon}
                  iconBg="bg-fd-blue/15"
                  iconColor="text-fd-blue"
                  label="Avg Lead Time"
                  value={fmt(summary.avg_lead_time_days, 1, 'd')}
                  subtitle={`${summary.work_orders_completed} WOs completed`}
                />
                <MiniStat
                  icon={ScaleIcon}
                  iconBg="bg-fd-blue/15"
                  iconColor="text-fd-blue"
                  label="Median Lead Time"
                  value={fmt(summary.median_lead_time_days, 1, 'd')}
                  subtitle={
                    summary.avg_release_to_last_ship_days !== null
                      ? `release→ship ${fmt(summary.avg_release_to_last_ship_days, 1, 'd')}`
                      : undefined
                  }
                />
                <MiniStat
                  icon={ArrowPathIcon}
                  iconBg="bg-fd-cyan/15"
                  iconColor="text-fd-cyan"
                  label="Little's Law"
                  value={fmt(summary.littles_law_throughput_days, 1, 'd')}
                  subtitle={
                    summary.daily_completion_rate !== null
                      ? `${fmt(summary.daily_completion_rate, 2)}/day`
                      : undefined
                  }
                />
                <MiniStat
                  icon={BoltIcon}
                  iconBg="bg-fd-green/15"
                  iconColor="text-fd-green"
                  label="Avg PCE"
                  value={fmt(summary.avg_pce_pct, 1, '%')}
                  subtitle="value-add / lead time"
                />
                <MiniStat
                  icon={QueueListIcon}
                  iconBg="bg-fd-amber/15"
                  iconColor="text-fd-amber"
                  label="Avg Queue"
                  value={fmt(summary.avg_queue_hours, 1, 'h')}
                />
                <MiniStat
                  icon={CubeIcon}
                  iconBg="bg-fd-cyan/15"
                  iconColor="text-fd-cyan"
                  label="Avg WIP"
                  value={fmt(summary.avg_wip, 1)}
                  subtitle={wip.data ? `${wip.data.total_open} open now` : undefined}
                />
              </MiniStatStrip>
              {summary.excluded_backfill_import_hours > 0 && (
                <p className="text-[11px] text-slate-500">
                  Provenance: {summary.excluded_backfill_import_hours.toFixed(1)}h of backfill/import labor excluded
                  from value-add.
                </p>
              )}
            </>
          )
        ))}

      {/* 2 — WIP aging + queue time */}
      {canViewFlow && (
        <div className="grid grid-cols-1 xl:grid-cols-12 gap-4 items-start">
          <CockpitPanel
            title="WIP Aging"
            subtitle="Open work orders, oldest first"
            className="xl:col-span-7"
            bodyClassName="lg:max-h-none"
            headerExtra={
              wip.data ? (
                <span className="text-xs tabular-nums text-slate-400">{wip.data.total_open} open</span>
              ) : undefined
            }
          >
            {wip.error ? (
              <ErrorState message="Could not load WIP aging." onRetry={loadAll} />
            ) : (
              <DataTable
                columns={wipColumns}
                data={wip.data?.items ?? []}
                rowKey={(item) => item.work_order_id}
                defaultSort={{ key: 'days_since_release', dir: 'desc' }}
                pageSize={10}
                csvExport={{ filename: 'wip-aging' }}
                onRowClick={(item) => navigate(`/work-orders/${item.work_order_id}`)}
                empty={{
                  icon: CubeIcon,
                  title: 'No open WIP',
                  description: 'Released work orders appear here with their age and current operation.',
                }}
              />
            )}
          </CockpitPanel>

          <CockpitPanel
            title="Queue Time by Work Center"
            subtitle="Hours waiting before an operation starts"
            className="xl:col-span-5"
            bodyClassName="lg:max-h-none"
          >
            {flow.error ? (
              <ErrorState message="Could not load queue times." onRetry={loadAll} />
            ) : (flow.data?.queue_by_work_center?.length ?? 0) === 0 ? (
              <EmptyState
                icon={QueueListIcon}
                title="No queue samples"
                description="Queue time appears once operations start (or become ready) in the period."
              />
            ) : (
              <>
                <DataTable
                  columns={queueColumns}
                  data={flow.data?.queue_by_work_center ?? []}
                  rowKey={(row) => row.work_center_id}
                  defaultSort={{ key: 'avg_queue_hours', dir: 'desc' }}
                  pageSize={10}
                />
                {(() => {
                  const rows = flow.data?.queue_by_work_center ?? [];
                  const total = rows.reduce((sum, row) => sum + row.samples, 0);
                  const fromReady = rows.reduce((sum, row) => sum + row.from_ready_events, 0);
                  return (
                    <p className="mt-2 text-[11px] text-slate-500">
                      Measured from ready events: {fromReady}/{total} samples (rest use the
                      predecessor-finish fallback).
                    </p>
                  );
                })()}
              </>
            )}
          </CockpitPanel>
        </div>
      )}

      {/* 3 — FPY / RTY */}
      {canViewYield && (
        <div className="grid grid-cols-1 xl:grid-cols-12 gap-4 items-start">
          <CockpitPanel
            title="First Pass Yield by Part"
            subtitle={`Overall FPY ${fmt(fpy.data?.overall_fpy_pct, 1, '%')} · RTY ${fmt(
              fpy.data?.overall_rty_pct,
              1,
              '%'
            )}`}
            className="xl:col-span-7"
            bodyClassName="lg:max-h-none"
          >
            {fpy.error ? (
              <ErrorState message="Could not load first pass yield." onRetry={loadAll} />
            ) : (
              <DataTable
                columns={fpyPartColumns}
                data={fpy.data?.by_part ?? []}
                rowKey={(row) => row.key}
                defaultSort={{ key: 'fpy_pct', dir: 'asc' }}
                pageSize={10}
                csvExport={{ filename: 'fpy-by-part' }}
                empty={{
                  icon: ScaleIcon,
                  title: 'No yield data',
                  description: 'FPY appears once operations report quantities in the period.',
                }}
              />
            )}
          </CockpitPanel>

          <CockpitPanel
            title="FPY by Work Center"
            subtitle="Quantity-weighted first-pass share"
            className="xl:col-span-5"
            bodyClassName="lg:max-h-none"
          >
            {fpy.error ? (
              <ErrorState message="Could not load first pass yield." onRetry={loadAll} />
            ) : (
              <DataTable
                columns={fpyWorkCenterColumns}
                data={fpy.data?.by_work_center ?? []}
                rowKey={(row) => row.key}
                defaultSort={{ key: 'fpy_pct', dir: 'asc' }}
                pageSize={10}
                csvExport={{ filename: 'fpy-by-work-center' }}
                empty={{
                  icon: ScaleIcon,
                  title: 'No yield data',
                  description: 'FPY appears once operations report quantities in the period.',
                }}
              />
            )}
          </CockpitPanel>
        </div>
      )}

      {/* 4 — Scrap Pareto */}
      {canViewYield && (
        <CockpitPanel
          title="Scrap Pareto"
          subtitle="Share of scrap quantity by reason code, with cumulative %"
          bodyClassName="lg:max-h-none"
          headerExtra={
            pareto.data ? (
              <span className="text-xs tabular-nums text-slate-400">
                {pareto.data.total_quantity.toLocaleString()} pcs · $
                {pareto.data.total_cost.toLocaleString(undefined, { maximumFractionDigits: 0 })}
              </span>
            ) : undefined
          }
        >
          {pareto.error ? (
            <ErrorState message="Could not load the scrap Pareto." onRetry={loadAll} />
          ) : paretoData.length === 0 ? (
            <EmptyState
              icon={TagIcon}
              title="No scrap in this period"
              description="Coded scrap builds the Pareto. Uncoded scrap lands in an 'unspecified' bucket."
            />
          ) : (
            <>
              <ResponsiveContainer width="100%" height={300}>
                <ComposedChart data={paretoData} margin={{ top: 8, right: 16, left: 0, bottom: 24 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke={CHART_GRID} />
                  <XAxis
                    dataKey="label"
                    angle={-30}
                    textAnchor="end"
                    interval={0}
                    height={56}
                    tick={CHART_TICK}
                    stroke={CHART_GRID}
                  />
                  <YAxis
                    domain={[0, 100]}
                    tick={CHART_TICK}
                    stroke={CHART_GRID}
                    tickFormatter={(v: number) => `${v}%`}
                  />
                  <Tooltip
                    contentStyle={CHART_TOOLTIP_STYLE}
                    formatter={(value: number | undefined, name: string | undefined, entry: any) => {
                      if (name === 'Share of scrap') {
                        const { quantity, cost } = entry?.payload ?? {};
                        return [
                          `${(value ?? 0).toFixed(1)}% — ${Number(quantity ?? 0).toLocaleString()} pcs, $${Number(
                            cost ?? 0
                          ).toLocaleString(undefined, { maximumFractionDigits: 0 })}`,
                          'Share of scrap',
                        ];
                      }
                      return [`${(value ?? 0).toFixed(1)}%`, 'Cumulative'];
                    }}
                    labelFormatter={(label, payload) => {
                      const name = payload?.[0]?.payload?.name;
                      return name && name !== label ? `${label} — ${name}` : String(label);
                    }}
                  />
                  <Legend wrapperStyle={{ fontSize: 12 }} />
                  <Bar dataKey="share" name="Share of scrap" fill={PARETO_BAR} radius={[4, 4, 0, 0]} maxBarSize={44} />
                  <Line
                    type="monotone"
                    dataKey="cumulative"
                    name="Cumulative"
                    stroke={PARETO_LINE}
                    strokeWidth={2}
                    dot={{ r: 3, fill: PARETO_LINE }}
                  />
                </ComposedChart>
              </ResponsiveContainer>
              <div className="mt-1 space-y-0.5">
                {paretoCostlessBuckets > 0 && (
                  <p className="text-[11px] text-slate-500">
                    Cost covers buckets with a known standard cost — {paretoCostlessBuckets} bucket
                    {paretoCostlessBuckets === 1 ? '' : 's'} carry quantity but no cost.
                  </p>
                )}
                {(pareto.data?.excluded_backfill_import_quantity ?? 0) > 0 && (
                  <p className="text-[11px] text-slate-500">
                    Provenance: {pareto.data?.excluded_backfill_import_quantity.toLocaleString()} pcs of
                    backfill/import scrap excluded.
                  </p>
                )}
              </div>
            </>
          )}
        </CockpitPanel>
      )}

      {/* 5 — Adoption + hidden factory */}
      {canViewFlow && (
        <div className="space-y-4">
          {adoption.error ? (
            <ErrorState message="Could not load adoption metrics." onRetry={loadAll} />
          ) : (
            adoption.data && (
              <>
                <MiniStatStrip className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-2">
                  <MiniStat
                    icon={BoltIcon}
                    iconBg="bg-fd-green/15"
                    iconColor="text-fd-green"
                    label="Digital Completion"
                    value={fmt(adoption.data.digital_completion_pct, 1, '%')}
                    subtitle={`${adoption.data.live_completions} live / ${adoption.data.backfill_completions} backfill / ${adoption.data.unknown_completions} unknown`}
                  />
                  <MiniStat
                    icon={ClockIcon}
                    iconBg="bg-fd-blue/15"
                    iconColor="text-fd-blue"
                    label="Clock-in Coverage"
                    value={fmt(adoption.data.clock_in_coverage_pct, 1, '%')}
                    subtitle="completed ops with live labor"
                  />
                  <MiniStat
                    icon={ArrowPathIcon}
                    iconBg="bg-fd-red/15"
                    iconColor="text-fd-red"
                    label="Backfill Rate"
                    value={fmt(adoption.data.backfill_rate_pct, 1, '%')}
                    subtitle="of time entries"
                  />
                  <MiniStat
                    icon={WrenchScrewdriverIcon}
                    iconBg="bg-fd-amber/15"
                    iconColor="text-fd-amber"
                    label="Rework Hours"
                    value={fmt(adoption.data.hidden_factory.rework_hours, 1, 'h')}
                    subtitle={`${fmt(adoption.data.hidden_factory.rework_hours_pct, 1, '%')} of labor`}
                  />
                  <MiniStat
                    icon={WrenchScrewdriverIcon}
                    iconBg="bg-fd-cyan/15"
                    iconColor="text-fd-cyan"
                    label="Planned Maintenance"
                    value={fmt(adoption.data.hidden_factory.maintenance.planned_pct, 1, '%')}
                    subtitle={`${adoption.data.hidden_factory.maintenance.planned_count} planned / ${adoption.data.hidden_factory.maintenance.reactive_count} reactive`}
                  />
                </MiniStatStrip>

                <div className="grid grid-cols-1 xl:grid-cols-12 gap-4 items-start">
                  <CockpitPanel
                    title="Adoption Trend"
                    subtitle="Weekly digital completion, clock-in coverage, backfill rate"
                    className="xl:col-span-7"
                    bodyClassName="lg:max-h-none"
                  >
                    {adoption.data.weekly.length === 0 ? (
                      <EmptyState
                        icon={BoltIcon}
                        title="No weekly data"
                        description="Weekly adoption appears once operations complete in the period."
                      />
                    ) : (
                      <ResponsiveContainer width="100%" height={260}>
                        <LineChart data={adoption.data.weekly} margin={{ top: 8, right: 16, left: 0, bottom: 4 }}>
                          <CartesianGrid strokeDasharray="3 3" stroke={CHART_GRID} />
                          <XAxis
                            dataKey="week_start"
                            tick={CHART_TICK}
                            stroke={CHART_GRID}
                            tickFormatter={formatWeekLabel}
                          />
                          <YAxis
                            domain={[0, 100]}
                            tick={CHART_TICK}
                            stroke={CHART_GRID}
                            tickFormatter={(v: number) => `${v}%`}
                          />
                          <Tooltip
                            contentStyle={CHART_TOOLTIP_STYLE}
                            formatter={(value: number | undefined) => [`${(value ?? 0).toFixed(1)}%`, '']}
                            labelFormatter={(label) => `Week of ${formatCentralDate(String(label))}`}
                          />
                          <Legend wrapperStyle={{ fontSize: 12 }} />
                          <Line
                            type="monotone"
                            dataKey="digital_completion_pct"
                            name="Digital completion"
                            stroke={ADOPTION_DIGITAL}
                            strokeWidth={2}
                            dot={false}
                          />
                          <Line
                            type="monotone"
                            dataKey="clock_in_coverage_pct"
                            name="Clock-in coverage"
                            stroke={ADOPTION_COVERAGE}
                            strokeWidth={2}
                            dot={false}
                          />
                          <Line
                            type="monotone"
                            dataKey="backfill_rate_pct"
                            name="Backfill rate"
                            stroke={ADOPTION_BACKFILL}
                            strokeWidth={2}
                            dot={false}
                          />
                        </LineChart>
                      </ResponsiveContainer>
                    )}
                  </CockpitPanel>

                  <CockpitPanel
                    title="Reliability (MTBF / MTTR)"
                    subtitle="Unplanned downtime per work center"
                    className="xl:col-span-5"
                    bodyClassName="lg:max-h-none"
                    footer={
                      adoption.data.hidden_factory.excluded_backfill_import_hours > 0
                        ? `${adoption.data.hidden_factory.excluded_backfill_import_hours.toFixed(1)}h backfill/import excluded`
                        : undefined
                    }
                  >
                    <DataTable
                      columns={reliabilityColumns}
                      data={adoption.data.hidden_factory.reliability_by_work_center}
                      rowKey={(row) => row.work_center_id}
                      defaultSort={{ key: 'mtbf_hours', dir: 'asc' }}
                      pageSize={10}
                      empty={{
                        icon: WrenchScrewdriverIcon,
                        title: 'No downtime recorded',
                        description: 'MTBF/MTTR appear once unplanned downtime events are logged.',
                      }}
                    />
                  </CockpitPanel>
                </div>
              </>
            )
          )}
        </div>
      )}
    </div>
  );
}
