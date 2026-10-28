import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import {
  ArrowPathIcon,
  ArrowLeftIcon,
  CheckCircleIcon,
  ClipboardDocumentListIcon,
  ClockIcon,
  ExclamationTriangleIcon,
  PauseCircleIcon,
  PlayCircleIcon,
} from '@heroicons/react/24/outline';
import api from '../services/api';
import { getAccessToken } from '../services/realtime';
import { EmptyState, ErrorState, StatusBadge, UnitBadge } from '../components/ui';
import { MiniStat, MiniStatStrip } from '../components/cockpit';
import { ResponsiveViews } from '../components/ui/ResponsiveViews';
import { useDebouncedValue } from '../hooks/useDebouncedValue';
import { workspaceIdentity } from '../hooks/useWorkspaceRecords';
import { formatCentralDate, formatCentralDateTime } from '../utils/centralTime';
import { customerPoDashboardUrl } from '../utils/customerPurchaseOrders';
import type {
  CustomerPODashboardResponse,
  CustomerPODashboardParams,
  CustomerPOListParams,
  CustomerPOListResponse,
  CustomerPOStats,
  CustomerPOSummary,
  CustomerPOWorkOrder,
} from '../types/customerPurchaseOrders';

const PAGE_SIZE = 25;
const number = (value: number) => value.toLocaleString('en-US', { maximumFractionDigits: 2 });
const date = (value?: string | null) => (value ? formatCentralDate(value) : '—');
const customer = (value?: string | null) => value || 'No customer assigned';
const linkStyle =
  'font-medium text-fd-link hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-fd-blue';

/** Every response belongs to one query and login; obsolete requests never replace it. */
function usePORead<T>(queryKey: string, read: (signal: AbortSignal) => Promise<T>) {
  const [, authChanged] = useState(0);
  const identity = workspaceIdentity();
  // Platform company switching changes the credential before the company-view
  // marker; the saved user still names their home company. Scope to that exact
  // credential so both displayed rows and in-flight reads change immediately.
  const token = getAccessToken();
  const scope = JSON.stringify([identity, token, queryKey]);
  const currentScope = useRef(scope);
  currentScope.current = scope;
  const sequence = useRef(0);
  const controller = useRef<AbortController | null>(null);
  const [state, setState] = useState<{ scope: string; data?: T; loading: boolean; error: boolean; updated?: Date }>({
    scope,
    loading: true,
    error: false,
  });
  const reload = useCallback(async () => {
    controller.current?.abort();
    const request = new AbortController();
    controller.current = request;
    const seq = ++sequence.current;
    setState(previous => ({
      ...(previous.scope === scope ? previous : {}),
      scope,
      loading: true,
      error: false,
    }));
    try {
      const data = await read(request.signal);
      if (
        sequence.current === seq &&
        currentScope.current === scope &&
        workspaceIdentity() === identity &&
        getAccessToken() === token
      ) {
        setState({ scope, data, loading: false, error: false, updated: new Date() });
      }
    } catch {
      if (
        !request.signal.aborted &&
        sequence.current === seq &&
        currentScope.current === scope &&
        workspaceIdentity() === identity &&
        getAccessToken() === token
      ) {
        setState(previous => ({ ...previous, loading: false, error: true }));
      }
    }
  }, [identity, token, scope, read]);
  useEffect(() => {
    const changed = () => authChanged(value => value + 1);
    window.addEventListener('werco:auth-token-changed', changed);
    window.addEventListener('storage', changed);
    return () => {
      window.removeEventListener('werco:auth-token-changed', changed);
      window.removeEventListener('storage', changed);
    };
  }, []);
  useEffect(() => {
    void reload();
    const refreshVisible = () => {
      if (!document.hidden) void reload();
    };
    const timer = window.setInterval(refreshVisible, 30_000);
    window.addEventListener('focus', refreshVisible);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener('focus', refreshVisible);
      ++sequence.current;
      controller.current?.abort();
    };
  }, [reload]);
  const visible = state.scope === scope ? state : { scope, loading: true, error: false };
  return { ...visible, reload };
}

function Progress({ value, label }: { value: number; label: string }) {
  const bounded = Math.max(0, Math.min(100, value));
  return (
    <div className="min-w-24 space-y-1">
      <div className="flex items-center justify-between gap-3 text-xs text-slate-300">
        <span>{label}</span>
        <span className="font-mono tabular-nums">{Math.round(bounded)}%</span>
      </div>
      <div
        role="progressbar"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={bounded}
        className="h-1.5 overflow-hidden rounded-sm bg-slate-700/60"
      >
        <div className="h-full bg-fd-blue" style={{ width: `${bounded}%` }} />
      </div>
    </div>
  );
}

function Pagination({
  total,
  skip,
  count,
  hasNext,
  loading,
  onPage,
}: {
  total: number;
  skip: number;
  count: number;
  hasNext: boolean;
  loading: boolean;
  onPage: (page: number) => void;
}) {
  const page = Math.floor(skip / PAGE_SIZE) + 1;
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 border-t border-fd-line px-4 py-3 text-xs text-slate-400">
      <span>{total === 0 ? '0 records' : `${count ? skip + 1 : 0}–${skip + count} of ${number(total)}`}</span>
      <nav aria-label="Pagination" className="flex items-center gap-3">
        <button className="btn-secondary btn-sm" disabled={skip === 0 || loading} onClick={() => onPage(page - 1)}>
          Previous
        </button>
        <span>Page {page}</span>
        <button className="btn-secondary btn-sm" disabled={!hasNext || loading} onClick={() => onPage(page + 1)}>
          Next
        </button>
      </nav>
    </div>
  );
}

function POOverview({ items }: { items: CustomerPOSummary[] }) {
  return (
    <ResponsiveViews
      desktop={
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <caption className="sr-only">Customer purchase orders and attached work order progress</caption>
            <thead className="border-b border-fd-line bg-slate-800/40 text-xs text-slate-400">
              <tr>
                {[
                  'Purchase order / customer',
                  'Work orders',
                  'Jobs complete',
                  'In progress',
                  'On hold',
                  'Overdue',
                  'Next due',
                ].map(label => (
                  <th key={label} scope="col" className="px-4 py-3 font-medium">
                    {label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-fd-line">
              {items.map(po => (
                <tr key={JSON.stringify([po.customer_name, po.customer_po])} className="hover:bg-slate-800/30">
                  <td className="max-w-80 px-4 py-4">
                    <Link
                      to={customerPoDashboardUrl(po.customer_name, po.customer_po)}
                      className={`${linkStyle} break-words font-mono`}
                    >
                      {po.customer_po}
                    </Link>
                    <p className="mt-1 break-words text-xs text-slate-400">{customer(po.customer_name)}</p>
                  </td>
                  <td className="px-4 py-4 font-mono text-slate-200">{po.stats.total_work_orders}</td>
                  <td className="min-w-40 px-4 py-4">
                    <Progress
                      value={po.stats.completion_percent}
                      label={`${po.stats.completed_work_orders} complete`}
                    />
                  </td>
                  <td className="px-4 py-4 text-slate-300">{po.stats.in_progress_work_orders}</td>
                  <td className={`px-4 py-4 ${po.stats.on_hold_work_orders ? 'text-amber-400' : 'text-slate-400'}`}>
                    {po.stats.on_hold_work_orders}
                  </td>
                  <td
                    className={`px-4 py-4 ${po.stats.overdue_work_orders ? 'font-semibold text-rose-400' : 'text-slate-400'}`}
                  >
                    {po.stats.overdue_work_orders}
                  </td>
                  <td className="whitespace-nowrap px-4 py-4 text-slate-300">{date(po.stats.next_due_date)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      }
      mobile={
        <div className="divide-y divide-fd-line">
          {items.map(po => (
            <article key={JSON.stringify([po.customer_name, po.customer_po])} className="space-y-3 p-4">
              <div>
                <Link
                  to={customerPoDashboardUrl(po.customer_name, po.customer_po)}
                  className={`${linkStyle} break-words font-mono`}
                >
                  {po.customer_po}
                </Link>
                <p className="mt-1 break-words text-sm text-slate-400">{customer(po.customer_name)}</p>
              </div>
              <Progress
                value={po.stats.completion_percent}
                label={`${po.stats.completed_work_orders} jobs complete · ${po.stats.total_work_orders} total`}
              />
              <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-300">
                <span>{po.stats.in_progress_work_orders} in progress</span>
                <span className={po.stats.on_hold_work_orders ? 'text-amber-400' : ''}>
                  {po.stats.on_hold_work_orders} on hold
                </span>
                <span className={po.stats.overdue_work_orders ? 'text-rose-400' : ''}>
                  {po.stats.overdue_work_orders} overdue
                </span>
                <span>Next due {date(po.stats.next_due_date)}</span>
              </div>
            </article>
          ))}
        </div>
      }
    />
  );
}

function WorkOrderIdentity({ wo }: { wo: CustomerPOWorkOrder }) {
  return (
    <div className="min-w-0 space-y-1 [overflow-wrap:anywhere]">
      <Link to={`/work-orders/${wo.id}`} className={`${linkStyle} font-mono`}>
        {wo.work_order_number}
      </Link>
      {wo.unit_number && (
        <div>
          <UnitBadge unitNumber={wo.unit_number} size="sm" />
        </div>
      )}
      {wo.parent_work_order_id && (
        <p className="text-xs text-slate-400">
          Child job ·{' '}
          <Link className={linkStyle} to={`/work-orders/${wo.parent_work_order_id}`}>
            View parent
          </Link>
        </p>
      )}
      <p className="text-xs text-slate-400">PO line {wo.po_line_item || '—'}</p>
    </div>
  );
}

function Operations({ wo }: { wo: CustomerPOWorkOrder }) {
  if (['complete', 'closed', 'cancelled'].includes(wo.status))
    return <span className="text-xs text-slate-400">{wo.status === 'cancelled' ? 'Cancelled' : 'Work finished'}</span>;
  if (!wo.current_operations.length)
    return (
      <span className="text-xs text-slate-400">{wo.operation_count ? 'No active operation' : 'No operations'}</span>
    );
  return (
    <ul className="space-y-2" aria-label={`Current operations for ${wo.work_order_number}`}>
      {wo.current_operations.map(op => (
        <li key={op.id} className="text-xs">
          <p className="break-words text-slate-200">
            {op.operation_number ? `${op.operation_number} · ` : ''}
            {op.name}
          </p>
          <p className="mt-0.5 break-words text-slate-400">
            {op.work_center_name || 'Unassigned station'} ·{' '}
            <span className={op.status === 'on_hold' ? 'text-amber-400' : ''}>{op.status.replace(/_/g, ' ')}</span>
          </p>
        </li>
      ))}
    </ul>
  );
}

function JobProgress({ wo }: { wo: CustomerPOWorkOrder }) {
  if (wo.status === 'cancelled') return <span className="text-xs text-slate-400">Cancelled</span>;
  const hasOperations = Number(wo.operation_count) > 0;
  const value = hasOperations ? wo.operation_progress_percent || 0 : (wo.quantity_complete / wo.quantity_ordered) * 100;
  return (
    <Progress
      value={value}
      label={hasOperations ? `${wo.operations_complete || 0}/${wo.operation_count} ops complete` : 'Quantity complete'}
    />
  );
}

function JobQuantity({ wo }: { wo: CustomerPOWorkOrder }) {
  return (
    <div className="text-xs text-slate-300">
      <p className="font-mono tabular-nums">
        {number(wo.quantity_complete)} / {number(wo.quantity_ordered)}
      </p>
      <p className="mt-0.5 text-slate-400">
        {wo.work_order_type === 'laser_cutting' ? 'sheet runs complete' : 'units complete'}
      </p>
      {wo.quantity_scrapped > 0 && <p className="mt-0.5 text-rose-400">{number(wo.quantity_scrapped)} scrapped</p>}
    </div>
  );
}

function JobDates({ wo }: { wo: CustomerPOWorkOrder }) {
  return (
    <div className="space-y-1 text-xs">
      <p className={wo.is_overdue ? 'text-rose-400' : 'text-slate-300'}>Due {date(wo.due_date)}</p>
      {wo.must_ship_by && <p className="text-slate-400">Ship by {date(wo.must_ship_by)}</p>}
      {wo.is_overdue && <span className="font-semibold text-rose-400">Overdue</span>}
    </div>
  );
}

function WorkOrderRows({ items }: { items: CustomerPOWorkOrder[] }) {
  return (
    <ResponsiveViews
      desktop={
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <caption className="sr-only">All work orders attached to this customer purchase order</caption>
            <thead className="border-b border-fd-line bg-slate-800/40 text-xs text-slate-400">
              <tr>
                {['Work order', 'Part', 'Status', 'Progress', 'Current operations / station', 'Quantity', 'Dates'].map(
                  label => (
                    <th key={label} scope="col" className="px-4 py-3 font-medium">
                      {label}
                    </th>
                  )
                )}
              </tr>
            </thead>
            <tbody className="divide-y divide-fd-line">
              {items.map(wo => (
                <tr key={wo.id} className="align-top hover:bg-slate-800/30">
                  <td className="min-w-40 px-4 py-4">
                    <WorkOrderIdentity wo={wo} />
                  </td>
                  <td className="max-w-52 px-4 py-4">
                    <p className="break-words font-medium text-slate-200">
                      {wo.part_number || (wo.work_order_type === 'laser_cutting' ? 'Nest package' : '—')}
                    </p>
                    <p className="mt-1 text-xs text-slate-400">{wo.part_name}</p>
                  </td>
                  <td className="px-4 py-4">
                    <StatusBadge status={wo.status} />
                  </td>
                  <td className="min-w-44 px-4 py-4">
                    <JobProgress wo={wo} />
                  </td>
                  <td className="min-w-48 px-4 py-4">
                    <Operations wo={wo} />
                  </td>
                  <td className="px-4 py-4">
                    <JobQuantity wo={wo} />
                  </td>
                  <td className="min-w-40 px-4 py-4">
                    <JobDates wo={wo} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      }
      mobile={
        <div className="divide-y divide-fd-line">
          {items.map(wo => (
            <article key={wo.id} className="space-y-4 p-4">
              <div className="flex items-start justify-between gap-3">
                <WorkOrderIdentity wo={wo} />
                <StatusBadge status={wo.status} className="shrink-0" />
              </div>
              <p className="break-words text-sm text-slate-300">
                {wo.part_number || (wo.work_order_type === 'laser_cutting' ? 'Nest package' : 'No part')}{' '}
                {wo.part_name && `· ${wo.part_name}`}
              </p>
              <JobProgress wo={wo} />
              <Operations wo={wo} />
              <div className="grid grid-cols-2 gap-3">
                <JobQuantity wo={wo} />
                <JobDates wo={wo} />
              </div>
            </article>
          ))}
        </div>
      }
    />
  );
}

function DashboardStats({ stats }: { stats: CustomerPOStats }) {
  return (
    <>
      <MiniStatStrip className="grid grid-cols-2 gap-2 md:grid-cols-3 xl:grid-cols-6">
        <MiniStat
          label="All work orders"
          value={stats.total_work_orders}
          icon={ClipboardDocumentListIcon}
          iconBg="bg-fd-blue/15"
          iconColor="text-fd-blue"
          subtitle={`${stats.active_work_orders} active · ${stats.cancelled_work_orders} cancelled`}
        />
        <MiniStat
          label="Complete / closed"
          value={stats.completed_work_orders}
          icon={CheckCircleIcon}
          iconBg="bg-fd-green/15"
          iconColor="text-fd-green"
        />
        <MiniStat
          label="In progress"
          value={stats.in_progress_work_orders}
          icon={PlayCircleIcon}
          iconBg="bg-fd-blue/15"
          iconColor="text-fd-blue"
        />
        <MiniStat
          label="On hold"
          value={stats.on_hold_work_orders}
          icon={PauseCircleIcon}
          iconBg="bg-amber-500/15"
          iconColor="text-amber-400"
        />
        <MiniStat
          label="Overdue"
          value={stats.overdue_work_orders}
          icon={ExclamationTriangleIcon}
          iconBg="bg-rose-500/15"
          iconColor="text-rose-400"
        />
        <MiniStat
          label="Due today"
          value={stats.due_today_work_orders}
          icon={ClockIcon}
          iconBg="bg-fd-blue/15"
          iconColor="text-fd-blue"
          subtitle={`Next due ${date(stats.next_due_date)}`}
        />
      </MiniStatStrip>
      <div className="card p-4">
        <Progress value={stats.completion_percent} label="Jobs complete" />
        <p className="mt-2 text-xs text-slate-400">
          {stats.completed_work_orders} of {stats.total_work_orders - stats.cancelled_work_orders} non-cancelled work
          orders complete or closed. Includes child jobs. Quantity and operation progress appear on each job below.
        </p>
      </div>
    </>
  );
}

export default function CustomerPurchaseOrders() {
  const [params, setParams] = useSearchParams();
  const selectedPO = params.get('customer_po');
  const selectedCustomer = params.get('customer_name') ?? '';
  const selected = selectedPO !== null;
  const search = params.get('search') ?? '';
  const debouncedSearch = useDebouncedValue(search, 250);
  const rawPage = Number(params.get('page') || '1');
  const page = Number.isSafeInteger(rawPage) && rawPage > 0 && rawPage < 1_000_000 ? rawPage : 1;
  const skip = (page - 1) * PAGE_SIZE;
  const requestParams = useMemo<CustomerPODashboardParams | CustomerPOListParams>(
    () =>
      selected
        ? { customer_name: selectedCustomer, customer_po: selectedPO || '', skip, limit: PAGE_SIZE }
        : { search: debouncedSearch || undefined, skip, limit: PAGE_SIZE },
    [selected, selectedCustomer, selectedPO, skip, debouncedSearch]
  );
  const read = useCallback(
    (signal: AbortSignal): Promise<CustomerPOListResponse | CustomerPODashboardResponse> =>
      'customer_po' in requestParams
        ? api.getCustomerPODashboard(requestParams, signal)
        : api.getCustomerPurchaseOrders(requestParams, signal),
    [requestParams]
  );
  const state = usePORead(JSON.stringify(requestParams), read);
  const dashboard = selected ? (state.data as CustomerPODashboardResponse | undefined) : undefined;
  const overview = selected ? undefined : (state.data as CustomerPOListResponse | undefined);
  const setPage = (next: number) =>
    setParams(previous => {
      const updated = new URLSearchParams(previous);
      if (next <= 1) updated.delete('page');
      else updated.set('page', String(next));
      return updated;
    });
  return (
    <div className="space-y-4">
      <Link
        to={selected ? '/work-orders/customer-pos' : '/work-orders'}
        className={`${linkStyle} inline-flex items-center gap-1.5 text-sm`}
      >
        <ArrowLeftIcon className="h-4 w-4" aria-hidden="true" />
        {selected ? 'All customer POs' : 'Work orders'}
      </Link>
      <div className="page-header mb-0">
        <div className="min-w-0">
          <h1 className="page-title break-words">{selected ? `PO ${selectedPO}` : 'Customer Purchase Orders'}</h1>
          <p className="page-subtitle break-words">
            {selected
              ? `${customer(selectedCustomer)} · Production dashboard`
              : 'Open a customer PO to track every attached work order.'}
          </p>
        </div>
        <button type="button" className="btn-secondary" disabled={state.loading} onClick={() => void state.reload()}>
          <ArrowPathIcon className={`mr-2 h-4 w-4 ${state.loading ? 'animate-spin' : ''}`} aria-hidden="true" />
          Refresh
        </button>
      </div>
      <p className="text-xs text-slate-400">
        {state.updated ? `Updated ${formatCentralDateTime(state.updated)} · ` : ''}Refreshes every 30 seconds. Includes
        completed and cancelled jobs.
      </p>
      {!selected && (
        <div className="card p-3">
          <label htmlFor="customer-po-search" className="mb-1.5 block text-xs font-medium text-slate-300">
            Find a customer purchase order
          </label>
          <input
            id="customer-po-search"
            type="search"
            className="input"
            placeholder="Search by PO number or customer"
            value={search}
            onChange={event => {
              const value = event.target.value;
              setParams(
                previous => {
                  const updated = new URLSearchParams(previous);
                  if (value) updated.set('search', value);
                  else updated.delete('search');
                  updated.delete('page');
                  return updated;
                },
                { replace: true }
              );
            }}
          />
          <p className="mt-2 text-xs text-slate-400">
            POs are grouped by customer and exact PO number. Job completion excludes cancelled work orders.
          </p>
        </div>
      )}
      {state.error && (
        <ErrorState
          title={state.data ? 'Refresh failed' : 'Could not load customer purchase orders'}
          message={
            state.data
              ? 'Showing the last successful update. Retry to get current progress.'
              : 'The purchase order data could not be loaded. Please retry.'
          }
          onRetry={() => void state.reload()}
        />
      )}
      {state.loading && !state.data && (
        <div className="card space-y-4 p-6" role="status">
          <p className="text-sm text-slate-400">
            Loading {selected ? 'purchase order dashboard' : 'customer purchase orders'}…
          </p>
          <div className="h-24 animate-pulse bg-slate-800/50" />
        </div>
      )}
      {dashboard && (
        <>
          <DashboardStats stats={dashboard.stats} />
          <div className="card overflow-hidden !p-0">
            <div className="border-b border-fd-line p-4">
              <h2 className="font-semibold text-slate-200">Attached work orders</h2>
              <p className="mt-1 text-xs text-slate-400">
                Progress reflects completed operations and reported production. Open a work order for its full routing
                and history.
              </p>
            </div>
            {dashboard.items.length ? (
              <WorkOrderRows items={dashboard.items} />
            ) : (
              <EmptyState
                title={dashboard.total ? 'No work orders on this page' : 'No attached work orders'}
                description={
                  dashboard.total
                    ? 'Use Previous to return to earlier work orders.'
                    : 'No work orders match this customer and PO number. Confirm the Customer PO field on the work order.'
                }
              />
            )}
            <Pagination
              total={dashboard.total}
              skip={dashboard.skip}
              count={dashboard.items.length}
              hasNext={dashboard.has_next}
              loading={state.loading}
              onPage={setPage}
            />
          </div>
        </>
      )}
      {overview && (
        <div className="card overflow-hidden !p-0">
          {overview.items.length ? (
            <POOverview items={overview.items} />
          ) : (
            <EmptyState
              title={search ? 'No matching purchase orders' : 'No customer purchase orders'}
              description={
                search
                  ? 'Try a different PO number or customer.'
                  : 'Enter a Customer PO on a work order to track it here.'
              }
            />
          )}
          <Pagination
            total={overview.total}
            skip={overview.skip}
            count={overview.items.length}
            hasNext={overview.has_next}
            loading={state.loading}
            onPage={setPage}
          />
        </div>
      )}
    </div>
  );
}
