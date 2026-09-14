import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import {
  ArrowDownTrayIcon,
  ArrowTrendingDownIcon,
  ArrowTrendingUpIcon,
  ChartBarSquareIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  MagnifyingGlassIcon,
} from '@heroicons/react/24/outline';
import { CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import api from '../services/api';
import { Button, EmptyState, ErrorState } from '../components/ui';
import { PageHeader } from '../components/ui/PageHeader';
import { useDebouncedValue } from '../hooks/useDebouncedValue';
import { formatCentralDate, formatInCentralTime } from '../utils/centralTime';
import type {
  MaterialPriceHistoryDetail,
  MaterialPriceHistoryResponse,
  MaterialPricePurchase,
  MaterialPriceSummary,
  PriceHistorySort,
  PriceHistoryTrend,
} from '../types/materialPriceHistory';

const money = (value: number | null | undefined) =>
  value == null ? '—' : `$${value.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 4 })}`;
const wholeMoney = (value: number) =>
  `$${value.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const quantity = (value: number) => value.toLocaleString('en-US', { maximumFractionDigits: 4 });
const dateLabel = (value: string) => formatCentralDate(value, { month: 'short', day: 'numeric', year: 'numeric' });
const shortDate = (value: string) => formatCentralDate(value, { month: 'short', day: 'numeric', year: '2-digit' });
const typeLabel = (value: string) => value.replace(/_/g, ' ');
const tone = (change: number | null) =>
  change == null || change === 0 ? 'text-slate-400' : change > 0 ? 'text-rose-400' : 'text-emerald-400';
const PAGE_SIZE = 15;
const HISTORY_PAGE_SIZE = 20;
type Period = 'all' | '12m' | '90d';

function PriceChange({
  change,
  percent,
  previous,
}: {
  change: number | null;
  percent: number | null;
  previous: number | null;
}) {
  if (previous == null) return <span className="text-xs text-slate-400">First purchase</span>;
  if (change == null) return <span className="text-xs text-slate-400">—</span>;
  if (change === 0) return <span className="text-xs text-slate-400">No change</span>;
  const Icon = change > 0 ? ArrowTrendingUpIcon : ArrowTrendingDownIcon;
  return (
    <span className={`relative inline-flex items-center gap-1 text-xs font-medium tabular-nums ${tone(change)}`}>
      <Icon className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
      {percent == null
        ? `${change > 0 ? '+' : '−'}${money(Math.abs(change))}`
        : `${percent > 0 ? '+' : ''}${percent.toLocaleString('en-US', { maximumFractionDigits: 2 })}%`}
      <span className="sr-only">
        {change > 0 ? ' price increase' : ' price decrease'}
        {percent == null ? ' (previous cost was zero)' : ''}
      </span>
    </span>
  );
}

function Sparkline({ item }: { item: MaterialPriceSummary }) {
  const values = item.sparkline.map(point => point.unit_price);
  if (!values.length) return null;
  const low = Math.min(...values);
  const high = Math.max(...values);
  const points = values.map(
    (value, i) =>
      `${values.length === 1 ? 40 : 3 + (i / (values.length - 1)) * 74},${high === low ? 15 : 26 - ((value - low) / (high - low)) * 22}`
  );
  return (
    <svg viewBox="0 0 80 30" className={`h-8 w-20 shrink-0 ${tone(item.price_change)}`} aria-hidden="true">
      {values.length > 1 && (
        <polyline
          points={points.join(' ')}
          fill="none"
          stroke="currentColor"
          strokeWidth="1.7"
          strokeLinejoin="round"
        />
      )}
      <circle
        cx={points[points.length - 1].split(',')[0]}
        cy={points[points.length - 1].split(',')[1]}
        r="2.5"
        fill="currentColor"
      />
    </svg>
  );
}

function LoadingPanel({ label }: { label: string }) {
  return (
    <div role="status" className="space-y-4 p-6">
      <p className="text-sm text-slate-400">{label}</p>
      <div className="h-20 animate-pulse rounded bg-slate-700/20" />
      <div className="h-40 animate-pulse rounded bg-slate-700/20" />
    </div>
  );
}

function Metric({ label, value, children }: { label: string; value: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="min-w-0 border border-fd-line bg-fd-panel p-3">
      <dt className="text-xs text-slate-400">{label}</dt>
      <dd className="mt-1 text-xl font-semibold tracking-tight text-white tabular-nums [overflow-wrap:anywhere]">
        {value}
      </dd>
      <dd className="mt-1 text-xs text-slate-400">{children}</dd>
    </div>
  );
}

function periodStart(period: Period): string | undefined {
  if (period === 'all') return undefined;
  const today = formatInCentralTime(new Date(), { year: 'numeric', month: '2-digit', day: '2-digit' });
  const [month, day, year] = today.split('/').map(Number);
  const start = new Date(Date.UTC(year, month - 1, day));
  if (period === '90d') start.setUTCDate(start.getUTCDate() - 90);
  else start.setUTCFullYear(start.getUTCFullYear() - 1);
  return start.toISOString().slice(0, 10);
}

function csvCell(value: string | number | null): string {
  let text = value == null ? '' : String(value);
  if (typeof value === 'string' && /^[\s]*[=+\-@]/.test(text)) text = `'${text}`;
  return `"${text.replace(/"/g, '""')}"`;
}

export default function MaterialPriceHistory() {
  const [searchParams, setSearchParams] = useSearchParams();
  const selectedParam = searchParams.get('part');
  const explicitPartId =
    selectedParam &&
    /^\d+$/.test(selectedParam) &&
    Number.isSafeInteger(Number(selectedParam)) &&
    Number(selectedParam) > 0
      ? Number(selectedParam)
      : null;
  const [search, setSearch] = useState('');
  const debouncedSearch = useDebouncedValue(search.trim(), 250);
  const [partType, setPartType] = useState('');
  const [trend, setTrend] = useState<PriceHistoryTrend>('all');
  const [sort, setSort] = useState<PriceHistorySort>('recent');
  const [page, setPage] = useState(1);
  const [refresh, setRefresh] = useState(0);
  const [overviewState, setOverviewState] = useState<{
    key: string;
    data?: MaterialPriceHistoryResponse;
    error?: boolean;
  }>({ key: '' });
  const overviewParams = useMemo(
    () => ({
      search: debouncedSearch || undefined,
      part_type: partType || undefined,
      trend,
      sort,
      page,
      page_size: PAGE_SIZE,
    }),
    [debouncedSearch, partType, trend, sort, page]
  );
  const overviewKey = JSON.stringify([overviewParams, refresh]);
  const overview = overviewState.key === overviewKey ? overviewState.data : undefined;
  const overviewError = overviewState.key === overviewKey && overviewState.error;
  const partId = explicitPartId ?? overviewState.data?.items[0]?.part_id ?? null;
  const [detailFilters, setDetailFilters] = useState<{
    partId: number | null;
    vendor: string;
    period: Period;
    page: number;
  }>({ partId: null, vendor: '', period: 'all', page: 1 });
  // Scope filters to the selection, including browser Back/Forward navigation.
  const vendor = detailFilters.partId === partId ? detailFilters.vendor : '';
  const period = detailFilters.partId === partId ? detailFilters.period : 'all';
  const historyPage = detailFilters.partId === partId ? detailFilters.page : 1;
  const startDate = useMemo(() => periodStart(period), [period]);
  const detailParams = useMemo(
    () => ({
      vendor_id: vendor ? Number(vendor) : undefined,
      start_date: startDate,
      page: historyPage,
      page_size: HISTORY_PAGE_SIZE,
    }),
    [vendor, startDate, historyPage]
  );
  const [detailRefresh, setDetailRefresh] = useState(0);
  const detailKey = JSON.stringify([partId, detailParams, detailRefresh]);
  const [detailState, setDetailState] = useState<{
    key: string;
    data?: MaterialPriceHistoryDetail;
    error?: boolean;
    empty?: boolean;
  }>({
    key: '',
  });
  const detail = detailState.key === detailKey && !detailState.error ? detailState.data : undefined;
  const detailError = detailState.key === detailKey && detailState.error;
  const detailEmpty = detailState.key === detailKey && detailState.empty;
  const detailShell = detail ?? (detailState.data?.part.part_id === partId ? detailState.data : undefined);
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState(false);
  const exportBusy = useRef(false);
  const detailRegion = useRef<HTMLElement>(null);

  useEffect(() => {
    let active = true;
    api
      .getMaterialPriceHistory(overviewParams)
      .then(data => {
        if (active) setOverviewState({ key: overviewKey, data });
      })
      .catch(() => {
        if (active) setOverviewState({ key: overviewKey, error: true });
      });
    return () => {
      active = false;
    };
  }, [overviewKey, overviewParams]);

  useEffect(() => {
    if (partId == null) return;
    let active = true;
    api
      .getMaterialPriceHistoryDetail(partId, detailParams)
      .then(data => {
        if (active) setDetailState({ key: detailKey, data });
      })
      .catch((error: unknown) => {
        const notFound = (error as { response?: { status?: number } } | null)?.response?.status === 404;
        if (active) setDetailState(previous => ({ key: detailKey, error: true, empty: notFound, data: previous.data }));
      });
    return () => {
      active = false;
    };
  }, [partId, detailKey, detailParams]);

  const updateDetailFilters = (next: Partial<typeof detailFilters>) => {
    setDetailFilters({ partId, vendor, period, page: 1, ...next });
  };

  const selectPart = (id: number) => {
    const next = new URLSearchParams(searchParams);
    next.set('part', String(id));
    setSearchParams(next);
    setExportError(false);
    if (window.matchMedia?.('(max-width: 1023px)').matches) {
      detailRegion.current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
      detailRegion.current?.focus({ preventScroll: true });
    }
  };

  const exportHistory = async () => {
    if (!detail || partId == null || exportBusy.current) return;
    exportBusy.current = true;
    setExporting(true);
    setExportError(false);
    try {
      const rows: MaterialPricePurchase[] = [];
      let exportPage = 1;
      let total = 0;
      do {
        const result = await api.getMaterialPriceHistoryDetail(partId, {
          ...detailParams,
          page: exportPage,
          page_size: 100,
        });
        total = result.total;
        rows.push(...result.history);
        if (!result.history.length && rows.length < total) throw new Error('Incomplete export');
        exportPage += 1;
      } while (rows.length < total);
      const header = [
        'Part number',
        'Item',
        'PO',
        'Order date',
        'Supplier',
        'Status',
        'Quantity ordered',
        'Unit (current catalog)',
        'Unit price',
        'Previous unit price',
        'Change',
        'Change %',
        'Ordered value (excludes tax/freight)',
        'Currency (not recorded)',
      ];
      const content = [
        header,
        ...rows.map(row => [
          detail.part.part_number,
          detail.part.part_name,
          row.po_number,
          row.order_date,
          row.vendor_name,
          row.status,
          row.quantity_ordered,
          row.unit_of_measure,
          row.unit_price,
          row.previous_unit_price,
          row.price_change,
          row.price_change_percent,
          row.extended_price,
          row.currency,
        ]),
      ]
        .map(row => row.map(csvCell).join(','))
        .join('\r\n');
      const url = URL.createObjectURL(new Blob(['\uFEFF', content], { type: 'text/csv;charset=utf-8;' }));
      const link = document.createElement('a');
      link.href = url;
      link.download = `${detail.part.part_number.replace(/[^a-zA-Z0-9._-]/g, '_')}-price-history.csv`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch {
      setExportError(true);
    } finally {
      exportBusy.current = false;
      setExporting(false);
    }
  };

  const trendOptions: { value: PriceHistoryTrend; label: string; count: number | undefined }[] = [
    { value: 'all', label: 'All items', count: overview?.summary.tracked_parts },
    { value: 'up', label: 'Price increases', count: overview?.summary.price_increases },
    { value: 'down', label: 'Price decreases', count: overview?.summary.price_decreases },
    { value: 'unchanged', label: 'Unchanged', count: overview?.summary.unchanged_parts },
    { value: 'new', label: 'First purchase', count: overview?.summary.new_parts },
  ];

  return (
    <div className="space-y-5">
      <PageHeader
        title="Material price history"
        description="See what you paid, spot cost changes, and trace every price back to its purchase order."
        actions={
          <Link className="btn-secondary" to="/purchasing">
            Purchase orders
          </Link>
        }
      />
      <div className="flex flex-wrap items-center gap-2" aria-label="Price movement filters">
        {trendOptions.map(option => (
          <button
            key={option.value}
            type="button"
            aria-pressed={trend === option.value}
            onClick={() => {
              setTrend(option.value);
              setPage(1);
            }}
            className={`inline-flex min-h-10 items-center gap-2 rounded-sm border px-3 py-2 text-xs font-medium transition-colors ${trend === option.value ? 'border-sky-400/60 bg-sky-400/10 text-sky-300' : 'border-fd-line bg-fd-panel text-slate-300 hover:border-slate-500'}`}
          >
            {option.value === 'up' && <ArrowTrendingUpIcon aria-hidden="true" className="h-4 w-4 text-rose-400" />}
            {option.value === 'down' && (
              <ArrowTrendingDownIcon aria-hidden="true" className="h-4 w-4 text-emerald-400" />
            )}
            {option.label}
            <span className="rounded-sm bg-slate-500/10 px-1.5 py-0.5 tabular-nums">{option.count ?? '—'}</span>
          </button>
        ))}
      </div>

      <div className="grid min-w-0 items-start gap-5 lg:grid-cols-[minmax(280px,340px)_minmax(0,1fr)]">
        <aside
          className="min-w-0 overflow-hidden rounded-sm border border-fd-line bg-fd-panel"
          aria-label="Purchased inventory"
        >
          <div className="space-y-3 border-b border-fd-line p-4">
            <div className="flex items-center justify-between gap-2">
              <h2 className="text-sm font-semibold text-white">Purchased inventory</h2>
              <span className="text-xs text-slate-400" aria-live="polite">
                {overview ? `${overview.total} items` : 'Loading…'}
              </span>
            </div>
            <div className="relative">
              <MagnifyingGlassIcon
                className="pointer-events-none absolute left-3 top-3 h-4 w-4 text-slate-400"
                aria-hidden="true"
              />
              <input
                type="search"
                aria-label="Search inventory"
                placeholder="Search item name or part number"
                value={search}
                onChange={event => {
                  setSearch(event.target.value);
                  setPage(1);
                }}
                className="input w-full !pl-9 text-sm"
              />
            </div>
            <div className="grid grid-cols-2 gap-2">
              <label className="min-w-0 text-xs text-slate-400">
                Inventory type
                <select
                  className="input mt-1 w-full text-xs"
                  value={partType}
                  onChange={event => {
                    setPartType(event.target.value);
                    setPage(1);
                  }}
                >
                  <option value="">All inventory</option>
                  <option value="raw_material">Raw materials</option>
                  <option value="purchased">Purchased parts</option>
                  <option value="hardware">Hardware</option>
                  <option value="consumable">Consumables</option>
                  <option value="manufactured">Manufactured parts</option>
                  <option value="assembly">Assemblies</option>
                </select>
              </label>
              <label className="min-w-0 text-xs text-slate-400">
                Sort inventory
                <select
                  className="input mt-1 w-full text-xs"
                  value={sort}
                  onChange={event => {
                    setSort(event.target.value as PriceHistorySort);
                    setPage(1);
                  }}
                >
                  <option value="recent">Latest purchase</option>
                  <option value="increase">Biggest increase</option>
                  <option value="decrease">Biggest decrease</option>
                  <option value="name">Part number</option>
                </select>
              </label>
            </div>
          </div>
          {overviewError ? (
            <ErrorState
              title="Could not load price history"
              message="Your purchase data could not be loaded."
              onRetry={() => setRefresh(value => value + 1)}
              className="m-3"
            />
          ) : !overview ? (
            <LoadingPanel label="Loading purchased inventory…" />
          ) : overview.items.length === 0 ? (
            <EmptyState
              title="No matching purchases"
              description="History appears automatically for approved, sent, partially received, received, and closed POs. Try clearing your filters."
              action={
                search || partType || trend !== 'all' ? (
                  {
                    label: 'Clear filters',
                    onClick: () => {
                      setSearch('');
                      setPartType('');
                      setTrend('all');
                      setPage(1);
                    },
                  }
                ) : (
                  <Link className="btn-secondary" to="/purchasing">
                    View purchase orders
                  </Link>
                )
              }
            />
          ) : (
            <>
              <ul className="max-h-[450px] divide-y divide-fd-line overflow-y-auto lg:max-h-[780px]">
                {overview.items.map(item => (
                  <li key={item.part_id}>
                    <button
                      type="button"
                      aria-pressed={partId === item.part_id}
                      onClick={() => selectPart(item.part_id)}
                      className={`w-full border-l-2 p-4 text-left transition-colors ${partId === item.part_id ? 'border-l-sky-400 bg-sky-400/[0.07]' : 'border-l-transparent hover:bg-slate-700/20'}`}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="min-w-0 truncate font-mono text-xs text-sky-300" title={item.part_number}>
                          {item.part_number}
                        </span>
                        <ChevronRightIcon aria-hidden="true" className="h-3.5 w-3.5 shrink-0 text-slate-400" />
                      </div>
                      <p className="mt-1 truncate text-sm font-medium text-slate-100" title={item.part_name}>
                        {item.part_name}
                      </p>
                      <div className="mt-2 flex items-center justify-between gap-3">
                        <div>
                          <p className="font-semibold text-white tabular-nums">
                            {money(item.latest_unit_price)}{' '}
                            <span className="text-xs font-normal text-slate-400">
                              / {item.unit_of_measure || 'unknown unit'}
                            </span>
                          </p>
                          <PriceChange
                            change={item.price_change}
                            percent={item.price_change_percent}
                            previous={item.previous_unit_price}
                          />
                        </div>
                        <Sparkline item={item} />
                      </div>
                      <p className="mt-2 truncate text-[11px] text-slate-400">
                        {dateLabel(item.last_order_date)} · {item.latest_vendor_name}
                      </p>
                    </button>
                  </li>
                ))}
              </ul>
              <div className="flex items-center justify-between gap-2 border-t border-fd-line p-3">
                <Button
                  variant="ghost"
                  size="sm"
                  aria-label="Previous inventory page"
                  disabled={page === 1}
                  onClick={() => setPage(value => value - 1)}
                >
                  <ChevronLeftIcon className="h-4 w-4" />
                </Button>
                <span className="text-xs text-slate-400">
                  {(page - 1) * PAGE_SIZE + 1}–{Math.min(page * PAGE_SIZE, overview.total)} of {overview.total}
                </span>
                <Button
                  variant="ghost"
                  size="sm"
                  aria-label="Next inventory page"
                  disabled={page * PAGE_SIZE >= overview.total}
                  onClick={() => setPage(value => value + 1)}
                >
                  <ChevronRightIcon className="h-4 w-4" />
                </Button>
              </div>
            </>
          )}
          <p className="border-t border-fd-line px-4 py-3 text-[11px] leading-relaxed text-slate-400">
            Changes compare each item’s two most recent POs. Mini charts show recent purchases in order.
          </p>
        </aside>

        <section
          ref={detailRegion}
          tabIndex={-1}
          aria-label="Item price details"
          className="min-w-0 scroll-mt-20 space-y-4 outline-none"
        >
          {partId == null ? (
            overview ? (
              <EmptyState
                icon={ChartBarSquareIcon}
                title="Your purchasing costs, over time"
                description="Choose an inventory item to explore its unit costs, suppliers, and purchase orders."
              />
            ) : (
              <LoadingPanel label="Loading price details…" />
            )
          ) : detailEmpty ? (
            <EmptyState
              icon={ChartBarSquareIcon}
              title="No purchase history available"
              description="This item has no qualifying purchase orders or is no longer available. History appears automatically when a PO is approved."
              action={
                <Link className="btn-secondary" to="/purchasing">
                  View purchase orders
                </Link>
              }
            />
          ) : !detailShell ? (
            detailError ? (
              <ErrorState
                title="Could not load item history"
                message="The item may be unavailable, or the request failed. Select another item or try again."
                onRetry={() => setDetailRefresh(value => value + 1)}
              />
            ) : (
              <LoadingPanel label="Loading price details…" />
            )
          ) : (
            <>
              <div className="flex min-w-0 flex-wrap items-start justify-between gap-3">
                <div className="min-w-0 flex-1">
                  <p className="font-mono text-xs text-sky-300 [overflow-wrap:anywhere]">
                    {detailShell.part.part_number}
                  </p>
                  <h2 className="mt-1 text-xl font-semibold tracking-tight text-white [overflow-wrap:anywhere]">
                    {detailShell.part.part_name}
                  </h2>
                  <p className="mt-1 text-xs capitalize text-slate-400">
                    {typeLabel(detailShell.part.part_type)} <span aria-hidden="true">·</span>{' '}
                    <span className="normal-case">Priced per {detailShell.part.unit_of_measure || 'unknown unit'}</span>
                  </p>
                </div>
                <Button variant="secondary" size="sm" disabled={exporting || !detail?.total} onClick={exportHistory}>
                  <ArrowDownTrayIcon aria-hidden="true" className="mr-1.5 h-4 w-4" />
                  {exporting ? 'Exporting…' : 'Export CSV'}
                </Button>
              </div>
              {exportError && <ErrorState title="Could not export history" message="Please try the export again." />}
              <div className="flex flex-wrap items-end gap-3 rounded-sm border border-fd-line bg-fd-panel p-3">
                <label className="min-w-0 flex-1 text-xs text-slate-400">
                  Supplier
                  <select
                    value={vendor}
                    onChange={event => updateDetailFilters({ vendor: event.target.value })}
                    className="input mt-1 w-full text-sm"
                  >
                    <option value="">All suppliers</option>
                    {detailShell.vendor_options.map(option => (
                      <option key={option.id} value={option.id}>
                        {option.name}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="min-w-[145px] flex-1 text-xs text-slate-400">
                  Purchase period
                  <select
                    value={period}
                    onChange={event => updateDetailFilters({ period: event.target.value as Period })}
                    className="input mt-1 w-full text-sm"
                  >
                    <option value="all">All time</option>
                    <option value="12m">Last 12 months</option>
                    <option value="90d">Last 90 days</option>
                  </select>
                </label>
                {(vendor || period !== 'all') && (
                  <Button variant="ghost" size="sm" onClick={() => updateDetailFilters({ vendor: '', period: 'all' })}>
                    Reset
                  </Button>
                )}
              </div>
              {detailError ? (
                <ErrorState
                  title="Could not load item history"
                  message="The request failed. Adjust your filters or try again."
                  onRetry={() => setDetailRefresh(value => value + 1)}
                />
              ) : !detail ? (
                <LoadingPanel label="Loading price details…" />
              ) : (
                <>
                  <dl className="grid grid-cols-2 gap-2 xl:grid-cols-4">
                    <Metric label="Latest unit cost" value={money(detail.stats.latest_unit_price)}>
                      {detail.stats.order_count
                        ? `per ${detail.part.unit_of_measure || 'unknown unit'}`
                        : 'No purchases in this view'}
                    </Metric>
                    <Metric
                      label="Change from previous PO"
                      value={
                        <span className={tone(detail.stats.price_change)}>
                          {detail.stats.price_change == null
                            ? '—'
                            : `${detail.stats.price_change > 0 ? '+' : detail.stats.price_change < 0 ? '−' : ''}${money(Math.abs(detail.stats.price_change))}`}
                        </span>
                      }
                    >
                      {detail.stats.order_count > 0 ? (
                        <PriceChange
                          change={detail.stats.price_change}
                          percent={detail.stats.price_change_percent}
                          previous={detail.stats.previous_unit_price}
                        />
                      ) : (
                        'No comparison yet'
                      )}
                    </Metric>
                    <Metric label="Lowest unit cost" value={money(detail.stats.lowest_unit_price)}>
                      High {money(detail.stats.highest_unit_price)}
                    </Metric>
                    <Metric label="Ordered value" value={wholeMoney(detail.stats.total_spend)}>
                      {quantity(detail.stats.total_quantity)} {detail.part.unit_of_measure || 'unknown unit'} ·{' '}
                      {detail.stats.order_count} POs
                    </Metric>
                  </dl>
                  <div className="overflow-hidden rounded-sm border border-fd-line bg-fd-panel">
                    <div className="flex flex-wrap items-start justify-between gap-2 border-b border-fd-line px-4 py-3">
                      <div>
                        <h3 className="text-sm font-semibold text-white">Unit cost over time</h3>
                        <p className="mt-1 text-xs text-slate-400">One point per PO · oldest to newest</p>
                      </div>
                      <span className="text-xs text-slate-400">
                        <span className="mr-1 inline-block w-4 border-t border-dashed border-slate-400 align-middle" />
                        Weighted average {money(detail.stats.weighted_average_unit_price)}
                      </span>
                    </div>
                    {detail.chart.length ? (
                      <div
                        role="img"
                        aria-label={`Unit cost history for ${detail.part.part_name}. ${detail.chart.length} purchases plotted. Exact prices are in the purchase history table below.`}
                        className="h-64 px-1 pb-1 pt-5 sm:h-72 sm:px-3"
                      >
                        <ResponsiveContainer width="100%" height="100%" minWidth={0}>
                          <LineChart
                            data={detail.chart}
                            margin={{ top: 8, right: 18, left: 0, bottom: 5 }}
                            accessibilityLayer
                          >
                            <CartesianGrid stroke="#273344" strokeDasharray="3 5" vertical={false} />
                            <XAxis
                              dataKey="purchase_order_id"
                              tickFormatter={id => {
                                const point = detail.chart.find(row => row.purchase_order_id === Number(id));
                                return point ? shortDate(point.order_date) : '';
                              }}
                              tick={{ fill: '#94a3b8', fontSize: 10 }}
                              axisLine={false}
                              tickLine={false}
                              minTickGap={35}
                              tickMargin={10}
                            />
                            <YAxis
                              domain={['auto', 'auto']}
                              tickFormatter={value =>
                                `$${Number(value).toLocaleString('en-US', { maximumFractionDigits: 2, notation: Math.abs(Number(value)) >= 10000 ? 'compact' : 'standard' })}`
                              }
                              width={76}
                              tick={{ fill: '#94a3b8', fontSize: 11 }}
                              axisLine={false}
                              tickLine={false}
                            />
                            <Tooltip
                              content={({ active, payload }) => {
                                const point = payload?.[0]?.payload as
                                  | MaterialPriceHistoryDetail['chart'][number]
                                  | undefined;
                                return active && point ? (
                                  <div className="max-w-64 rounded-sm border border-slate-600 bg-[#141b26] p-3 text-xs shadow-xl">
                                    <p className="font-semibold text-white">
                                      {point.po_number} · {dateLabel(point.order_date)}
                                    </p>
                                    <p className="mt-1 text-slate-300">{point.vendor_name}</p>
                                    <p className="mt-2 text-base font-semibold text-sky-300">
                                      {money(point.unit_price)}{' '}
                                      <span className="text-xs text-slate-400">
                                        / {detail.part.unit_of_measure || 'unknown unit'}
                                      </span>
                                    </p>
                                    <p className="mt-1 text-slate-400">
                                      {quantity(point.quantity_ordered)} {detail.part.unit_of_measure || 'unknown unit'}{' '}
                                      ordered
                                    </p>
                                  </div>
                                ) : null;
                              }}
                            />
                            {detail.stats.weighted_average_unit_price != null && (
                              <ReferenceLine
                                y={detail.stats.weighted_average_unit_price}
                                stroke="#94a3b8"
                                strokeDasharray="4 5"
                              />
                            )}
                            <Line
                              type="linear"
                              dataKey="unit_price"
                              name="Unit cost"
                              stroke="#38bdf8"
                              strokeWidth={2.5}
                              dot={{ r: detail.chart.length > 70 ? 0 : 3.5, fill: '#141b26', strokeWidth: 2 }}
                              activeDot={{ r: 6, fill: '#38bdf8', stroke: '#141b26', strokeWidth: 2 }}
                              isAnimationActive={false}
                            />
                          </LineChart>
                        </ResponsiveContainer>
                      </div>
                    ) : (
                      <EmptyState
                        title="No purchases in this view"
                        description="Try all suppliers or a longer purchase period to see more history."
                      />
                    )}
                    <p className="px-4 pb-3 text-[11px] text-slate-400">
                      {detail.chart_truncated
                        ? 'Showing the latest 500 POs. The average and totals use all matching purchases; the table and CSV include the full history. '
                        : ''}
                      Average is weighted by quantity ordered. Purchase points are evenly spaced.
                    </p>
                  </div>
                  <div className="min-w-0 overflow-hidden rounded-sm border border-fd-line bg-fd-panel">
                    <div className="flex flex-wrap items-center justify-between gap-2 border-b border-fd-line px-4 py-3">
                      <h3 className="text-sm font-semibold text-white">
                        Purchase history <span className="ml-1 font-normal text-slate-400">({detail.total})</span>
                      </h3>
                      <span className="text-xs text-slate-400">Newest first · changes within this view</span>
                    </div>
                    {detail.history.length > 0 && (
                      <p className="border-b border-fd-line px-4 py-2 text-[11px] text-slate-400 sm:hidden">
                        Scroll the table sideways for suppliers, quantities, and ordered values.
                      </p>
                    )}
                    {detail.history.length > 0 && (
                      /* eslint-disable jsx-a11y/no-noninteractive-tabindex -- Keyboard focus enables horizontal table scrolling. */
                      <div
                        className="relative overflow-x-auto"
                        tabIndex={0}
                        role="region"
                        aria-label="Purchase history table"
                      >
                        <table className="w-full min-w-[670px] text-left text-xs">
                          <caption className="sr-only">
                            PO-by-PO unit costs for {detail.part.part_name}; prices exclude tax and freight
                          </caption>
                          <thead className="border-b border-fd-line bg-slate-800/20 text-slate-400">
                            <tr>
                              {['Purchase order', 'Unit cost', 'Change', 'Supplier', 'Quantity', 'Ordered value'].map(
                                (label, index) => (
                                  <th
                                    key={label}
                                    scope="col"
                                    className={`px-4 py-2.5 font-medium ${index !== 0 && index !== 3 ? 'text-right' : ''}`}
                                  >
                                    {label}
                                  </th>
                                )
                              )}
                            </tr>
                          </thead>
                          <tbody className="divide-y divide-fd-line">
                            {detail.history.map(row => (
                              <tr key={row.purchase_order_id} className="hover:bg-slate-700/10">
                                <td className="px-4 py-3">
                                  <Link
                                    to={`/purchasing?po=${row.purchase_order_id}`}
                                    className="font-mono font-medium text-sky-300 hover:underline"
                                  >
                                    {row.po_number}
                                  </Link>
                                  <p className="mt-1 whitespace-nowrap text-slate-400">{dateLabel(row.order_date)}</p>
                                </td>
                                <td className="whitespace-nowrap px-4 py-3 text-right font-medium text-white tabular-nums">
                                  {money(row.unit_price)}
                                  {row.line_count > 1 && (
                                    <p className="mt-1 text-[10px] font-normal text-slate-400">
                                      {row.line_count} lines averaged
                                    </p>
                                  )}
                                </td>
                                <td className="whitespace-nowrap px-4 py-3 text-right">
                                  <PriceChange
                                    change={row.price_change}
                                    percent={row.price_change_percent}
                                    previous={row.previous_unit_price}
                                  />
                                </td>
                                <td className="max-w-[210px] px-4 py-3">
                                  <p className="text-slate-200 [overflow-wrap:anywhere]">{row.vendor_name}</p>
                                  <p className="mt-1 capitalize text-slate-400">
                                    {row.status === 'partial' ? 'Partially received' : row.status.replace(/_/g, ' ')}
                                  </p>
                                </td>
                                <td className="px-4 py-3 text-right tabular-nums">
                                  <span className="text-slate-200">{quantity(row.quantity_ordered)}</span>
                                  <p className="mt-1 text-slate-400">{row.unit_of_measure || 'unknown unit'}</p>
                                </td>
                                <td className="whitespace-nowrap px-4 py-3 text-right text-slate-200 tabular-nums">
                                  {wholeMoney(row.extended_price)}
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    )}
                    {/* eslint-enable jsx-a11y/no-noninteractive-tabindex */}
                    {!detail.history.length && (
                      <p className="px-4 py-8 text-center text-sm text-slate-400">
                        No purchase orders match these filters.
                      </p>
                    )}
                    {detail.total > HISTORY_PAGE_SIZE && (
                      <div className="flex flex-wrap items-center justify-between gap-2 border-t border-fd-line p-3">
                        <span className="text-xs text-slate-400">
                          {(historyPage - 1) * HISTORY_PAGE_SIZE + 1}–
                          {Math.min(historyPage * HISTORY_PAGE_SIZE, detail.total)} of {detail.total} POs
                        </span>
                        <div className="flex gap-2">
                          <Button
                            variant="secondary"
                            size="sm"
                            disabled={historyPage === 1}
                            onClick={() => updateDetailFilters({ page: historyPage - 1 })}
                          >
                            Previous purchases
                          </Button>
                          <Button
                            variant="secondary"
                            size="sm"
                            disabled={historyPage * HISTORY_PAGE_SIZE >= detail.total}
                            onClick={() => updateDetailFilters({ page: historyPage + 1 })}
                          >
                            Next purchases
                          </Button>
                        </div>
                      </div>
                    )}
                  </div>
                  <p className="text-xs leading-relaxed text-slate-400">
                    PO unit costs exclude tax and freight; ordered value is not an invoice or payment total. Approved,
                    sent, partially received, received, and closed orders are included.
                  </p>
                  <details className="text-xs text-slate-400">
                    <summary className="w-fit cursor-pointer py-1 hover:text-slate-200">
                      How prices are calculated
                    </summary>
                    <ul className="mt-2 list-disc space-y-1 pl-5">
                      {detail.notes.map(note => (
                        <li key={note}>{note}</li>
                      ))}
                      <li>
                        Amounts use the app’s $ display convention. Purchase orders do not record currency; no currency
                        conversion is applied.
                      </li>
                    </ul>
                  </details>
                </>
              )}
            </>
          )}
        </section>
      </div>
    </div>
  );
}
