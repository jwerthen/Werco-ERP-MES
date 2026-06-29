import React, { useEffect, useState, useMemo, useCallback, useRef } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import api from '../services/api';
import { WorkOrderSummary, WorkOrderStatus } from '../types';
import { useWebSocket } from '../hooks/useWebSocket';
import { buildWsUrl, getAccessToken } from '../services/realtime';
import { useAuth } from '../context/AuthContext';
import { formatCentralDate, isDateBeforeTodayInCentral, isDateTodayInCentral } from '../utils/centralTime';
import { 
  PlusIcon, 
  MagnifyingGlassIcon, 
  Squares2X2Icon, 
  ListBulletIcon,
  ChevronRightIcon,
  ClockIcon,
  ExclamationTriangleIcon,
  TrashIcon,
  CheckCircleIcon,
} from '@heroicons/react/24/outline';
import { SkeletonTable, SkeletonCard } from '../components/ui/Skeleton';
import { EmptyState, ErrorState, useToast, DataTable, DataTableColumn, StatusBadge, Button } from '../components/ui';
import { MiniStat, MiniStatStrip } from '../components/cockpit';

const priorityConfig: Record<number, { bg: string; text: string; label: string }> = {
  1: { bg: 'bg-red-500/20', text: 'text-red-400', label: 'Critical' },
  2: { bg: 'bg-red-500/10', text: 'text-red-600', label: 'High' },
  3: { bg: 'bg-amber-500/10', text: 'text-amber-400', label: 'Medium' },
  4: { bg: 'bg-blue-500/10', text: 'text-blue-600', label: 'Normal' },
  5: { bg: 'bg-surface-100', text: 'text-surface-600', label: 'Low' },
};

const EXCLUDED_PART_TYPES = ['purchased', 'hardware', 'raw_material'];
const CURRENT_WORK_ORDER_STATUSES = ['released', 'in_progress', 'on_hold'];

type GroupBy = 'none' | 'customer' | 'part' | 'status';

const statusOptions: { value: string; label: string }[] = [
  { value: '', label: 'All Active' },
  { value: 'draft', label: 'Draft' },
  { value: 'released', label: 'Released' },
  { value: 'in_progress', label: 'In Progress' },
  { value: 'on_hold', label: 'On Hold' },
  { value: 'complete', label: 'Complete' },
  { value: 'closed', label: 'Closed' },
];

const groupOptions: { value: GroupBy; label: string }[] = [
  { value: 'none', label: 'No Grouping' },
  { value: 'customer', label: 'By Customer' },
  { value: 'part', label: 'By Part' },
  { value: 'status', label: 'By Status' },
];

const formatStatusLabel = (status: string) => status.replace('_', ' ');

const getWorkOrderProgress = (wo: WorkOrderSummary) => {
  const operationCount = Number(wo.operation_count || 0);
  if (operationCount > 0 && wo.operation_progress_percent !== undefined) {
    return {
      percent: Math.min(100, Math.max(0, Number(wo.operation_progress_percent || 0))),
      label: `${Number(wo.operations_complete || 0)}/${operationCount} ops`,
      title: 'Progress',
    };
  }

  const ordered = Number(wo.quantity_ordered || 0);
  const complete = Number(wo.quantity_complete || 0);
  return {
    percent: ordered > 0 ? Math.min(100, Math.max(0, (complete / ordered) * 100)) : 0,
    label: `${complete}/${ordered}`,
    title: 'Quantity',
  };
};

// Cell renderers — shared by the flat and grouped DataTable views.
function StatusCell({ status }: { status: WorkOrderStatus }) {
  return <StatusBadge status={status} />;
}

function PriorityCell({ priority }: { priority: number }) {
  const cfg = priorityConfig[priority] || priorityConfig[4];
  return <span className={`badge ${cfg.bg} ${cfg.text}`}>P{priority}</span>;
}

function ProgressCell({ wo }: { wo: WorkOrderSummary }) {
  const progress = getWorkOrderProgress(wo);
  return (
    <div className="flex items-center gap-2">
      <div className="flex-1 h-2 bg-surface-200 rounded-full overflow-hidden w-20">
        <div
          className="h-full bg-werco-500 rounded-full transition-all"
          style={{ width: `${progress.percent}%` }}
        />
      </div>
      <span className="text-sm font-medium text-surface-700 tabular-nums">{progress.label}</span>
    </div>
  );
}

function DueDateCell({ wo }: { wo: WorkOrderSummary }) {
  const overdue = isWorkOrderOverdue(wo);
  return (
    <>
      <span className={`text-sm font-medium ${overdue ? 'text-red-600' : 'text-surface-700'}`}>
        {wo.due_date ? formatCentralDate(wo.due_date) : '—'}
      </span>
      {overdue && <span className="ml-2 badge badge-danger text-[10px] py-0.5">OVERDUE</span>}
    </>
  );
}

function RowActionsCell({
  wo,
  onDelete,
  onRelease,
  isReleasing,
}: {
  wo: WorkOrderSummary;
  onDelete?: (wo: WorkOrderSummary) => void;
  onRelease?: (wo: WorkOrderSummary) => void;
  isReleasing: boolean;
}) {
  // Stop propagation so action clicks don't trigger the row click-through.
  return (
    <div className="flex items-center gap-1" onClick={(e) => e.stopPropagation()}>
      {onRelease && wo.status === 'draft' && (
        <button
          onClick={() => onRelease(wo)}
          disabled={isReleasing}
          className="p-2 rounded-lg text-emerald-600 hover:text-emerald-400 hover:bg-emerald-500/10 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          title="Release"
          aria-label={`Release ${wo.work_order_number}`}
        >
          <CheckCircleIcon className="h-4 w-4" aria-hidden="true" />
        </button>
      )}
      {onDelete && (
        <button
          onClick={() => onDelete(wo)}
          className="p-2 rounded-lg text-surface-400 hover:text-red-600 hover:bg-red-500/10 transition-colors"
          title="Delete"
          aria-label={`Delete ${wo.work_order_number}`}
        >
          <TrashIcon className="h-4 w-4" aria-hidden="true" />
        </button>
      )}
      <Link
        to={`/work-orders/${wo.id}`}
        className="p-2 rounded-lg text-surface-400 hover:text-werco-600 hover:bg-werco-50 transition-colors"
        aria-label={`View ${wo.work_order_number}`}
      >
        <ChevronRightIcon className="h-5 w-5" aria-hidden="true" />
      </Link>
    </div>
  );
}

interface WorkOrderColumnOptions {
  hideColumn?: 'customer' | 'part';
  onDelete?: (wo: WorkOrderSummary) => void;
  onRelease?: (wo: WorkOrderSummary) => void;
  releasingIds?: Set<number>;
}

function buildWorkOrderColumns({
  hideColumn,
  onDelete,
  onRelease,
  releasingIds,
}: WorkOrderColumnOptions): Array<DataTableColumn<WorkOrderSummary>> {
  const cols: Array<DataTableColumn<WorkOrderSummary>> = [
    {
      key: 'work_order_number',
      header: 'Work Order',
      sortable: true,
      accessor: (wo) => wo.work_order_number,
      render: (wo) => (
        <Link
          to={`/work-orders/${wo.id}`}
          onClick={(e) => e.stopPropagation()}
          className="font-semibold text-werco-600 hover:text-werco-700 hover:underline"
        >
          {wo.work_order_number}
        </Link>
      ),
    },
  ];

  if (hideColumn !== 'part') {
    cols.push({
      key: 'part',
      header: 'Part',
      sortable: true,
      accessor: (wo) => wo.part_number ?? '',
      csv: (wo) => wo.part_number ?? '',
      render: (wo) => (
        <div>
          <p className="font-medium text-surface-900">{wo.part_number}</p>
          <p className="text-sm text-surface-500 line-clamp-1">{wo.part_name}</p>
        </div>
      ),
    });
  }

  if (hideColumn !== 'customer') {
    cols.push({
      key: 'customer',
      header: 'Customer',
      sortable: true,
      accessor: (wo) => wo.customer_name ?? '',
      className: 'text-surface-600',
      render: (wo) => wo.customer_name || '—',
    });
  }

  cols.push(
    {
      key: 'progress',
      header: 'Progress',
      accessor: (wo) => getWorkOrderProgress(wo).percent,
      csv: (wo) => getWorkOrderProgress(wo).label,
      render: (wo) => <ProgressCell wo={wo} />,
    },
    {
      key: 'due_date',
      header: 'Due Date',
      sortable: true,
      accessor: (wo) => wo.due_date ?? '',
      csv: (wo) => (wo.due_date ? formatCentralDate(wo.due_date) : ''),
      render: (wo) => <DueDateCell wo={wo} />,
    },
    {
      key: 'priority',
      header: 'Priority',
      sortable: true,
      accessor: (wo) => wo.priority,
      render: (wo) => <PriorityCell priority={wo.priority} />,
    },
    {
      key: 'status',
      header: 'Status',
      sortable: true,
      accessor: (wo) => wo.status,
      render: (wo) => <StatusCell status={wo.status} />,
    },
    {
      key: 'actions',
      header: '',
      className: 'w-28',
      render: (wo) => (
        <RowActionsCell
          wo={wo}
          onDelete={onDelete}
          onRelease={onRelease}
          isReleasing={Boolean(releasingIds?.has(wo.id))}
        />
      ),
    }
  );

  return cols;
}

export default function WorkOrders() {
  const { user } = useAuth();
  const navigate = useNavigate();
  const { showToast } = useToast();
  const canDeleteWorkOrders = user?.role === 'admin' || !!user?.is_superuser;
  const [workOrders, setWorkOrders] = useState<WorkOrderSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [search, setSearch] = useState('');
  const [debouncedSearch, setDebouncedSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState<string>('');
  const [customerFilter, setCustomerFilter] = useState<string>('');
  const [hideCOTS, setHideCOTS] = useState(true);
  const [groupBy, setGroupBy] = useState<GroupBy>('none');
  const [releasingIds, setReleasingIds] = useState<Set<number>>(new Set());
  const realtimeRefreshRef = useRef<NodeJS.Timeout | null>(null);
  const loadRequestRef = useRef(0);
  const realtimeUrl = useMemo(() => {
    const token = getAccessToken();
    return buildWsUrl('/ws/updates', token ? { token } : undefined);
  }, [user?.id]);

  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedSearch(search.trim()), 250);
    return () => window.clearTimeout(timer);
  }, [search]);

  const loadWorkOrders = useCallback(async () => {
    const requestId = loadRequestRef.current + 1;
    loadRequestRef.current = requestId;

    try {
      const params: any = {};
      if (statusFilter) params.status = statusFilter;
      if (debouncedSearch) params.search = debouncedSearch;
      const response = await api.getWorkOrders(params);
      if (requestId !== loadRequestRef.current) return;
      setWorkOrders(response);
      setLoadError(false);
    } catch (err) {
      if (requestId !== loadRequestRef.current) return;
      console.error('Failed to load work orders:', err);
      setLoadError(true);
    } finally {
      if (requestId !== loadRequestRef.current) return;
      setLoading(false);
    }
  }, [statusFilter, debouncedSearch]);

  const scheduleRealtimeRefresh = useCallback(() => {
    if (realtimeRefreshRef.current) return;
    realtimeRefreshRef.current = setTimeout(() => {
      realtimeRefreshRef.current = null;
      loadWorkOrders();
    }, 600);
  }, [loadWorkOrders]);

  useWebSocket({
    url: realtimeUrl,
    enabled: true,
    onMessage: (message) => {
      if (message.type === 'connected' || message.type === 'ping') return;
      if (['work_order_update', 'dashboard_update', 'shop_floor_update'].includes(message.type)) {
        scheduleRealtimeRefresh();
      }
    }
  });

  useEffect(() => {
    loadWorkOrders();
  }, [loadWorkOrders]);

  useEffect(() => {
    return () => {
      if (realtimeRefreshRef.current) {
        clearTimeout(realtimeRefreshRef.current);
        realtimeRefreshRef.current = null;
      }
    };
  }, []);

  useEffect(() => {
    const interval = window.setInterval(() => {
      loadWorkOrders();
    }, 30000);

    const refreshWhenVisible = () => {
      if (document.visibilityState === 'visible') {
        loadWorkOrders();
      }
    };

    const refreshOnFocus = () => {
      loadWorkOrders();
    };

    document.addEventListener('visibilitychange', refreshWhenVisible);
    window.addEventListener('focus', refreshOnFocus);

    return () => {
      window.clearInterval(interval);
      document.removeEventListener('visibilitychange', refreshWhenVisible);
      window.removeEventListener('focus', refreshOnFocus);
    };
  }, [loadWorkOrders]);

  const handleDelete = useCallback(async (wo: WorkOrderSummary) => {
    const isCurrent = CURRENT_WORK_ORDER_STATUSES.includes(wo.status);
    const message = isCurrent
      ? `Delete current work order ${wo.work_order_number}?\n\nThis removes it from active lists, scheduling, and shop floor queues while preserving the record for audit/restore.`
      : `Delete work order ${wo.work_order_number}?\n\nThis removes it from active lists while preserving the record for audit/restore.`;
    if (!window.confirm(message)) return;
    try {
      await api.deleteWorkOrder(wo.id);
      loadWorkOrders();
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to delete work order');
    }
  }, [loadWorkOrders, showToast]);

  const handleRelease = useCallback(async (wo: WorkOrderSummary) => {
    if (wo.status !== 'draft') return;
    setReleasingIds((prev) => new Set(prev).add(wo.id));
    try {
      await api.releaseWorkOrder(wo.id);
      loadWorkOrders();
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to release work order');
    } finally {
      setReleasingIds((prev) => {
        const next = new Set(prev);
        next.delete(wo.id);
        return next;
      });
    }
  }, [loadWorkOrders, showToast]);

  const workOrderColumns = useMemo(
    () =>
      buildWorkOrderColumns({
        onDelete: canDeleteWorkOrders ? handleDelete : undefined,
        onRelease: handleRelease,
        releasingIds,
      }),
    [canDeleteWorkOrders, handleDelete, handleRelease, releasingIds]
  );

  const customers = useMemo(() => {
    const unique = new Set(workOrders.map(wo => wo.customer_name).filter(Boolean));
    return Array.from(unique).sort() as string[];
  }, [workOrders]);

  const filteredWorkOrders = useMemo(() => {
    return workOrders.filter(wo => {
      if (hideCOTS && wo.part_type && EXCLUDED_PART_TYPES.includes(wo.part_type)) {
        return false;
      }
      if (customerFilter && wo.customer_name !== customerFilter) {
        return false;
      }
      return true;
    });
  }, [workOrders, customerFilter, hideCOTS]);

  const groupedWorkOrders = useMemo(() => {
    if (groupBy === 'none') return null;
    
    const groups: Record<string, WorkOrderSummary[]> = {};
    filteredWorkOrders.forEach(wo => {
      let key: string;
      switch (groupBy) {
        case 'customer':
          key = wo.customer_name || 'No Customer';
          break;
        case 'part':
          key = wo.part_number || 'No Part';
          break;
        case 'status':
          key = wo.status;
          break;
        default:
          key = 'Unknown';
      }
      if (!groups[key]) groups[key] = [];
      groups[key].push(wo);
    });
    
    return Object.entries(groups).sort(([a], [b]) => a.localeCompare(b));
  }, [filteredWorkOrders, groupBy]);

  // Stats
  const stats = useMemo(() => {
    const overdue = filteredWorkOrders.filter(wo => 
      wo.due_date && isDateBeforeTodayInCentral(wo.due_date) && !['complete', 'closed', 'cancelled'].includes(wo.status)
    ).length;
    const inProgress = filteredWorkOrders.filter(wo => wo.status === 'in_progress').length;
    const dueToday = filteredWorkOrders.filter(wo => Boolean(wo.due_date && isDateTodayInCentral(wo.due_date))).length;
    return { overdue, inProgress, dueToday };
  }, [filteredWorkOrders]);

  if (loading) {
    return (
      <div className="space-y-6">
        {/* Header skeleton */}
        <div className="flex items-center justify-between">
          <div className="space-y-2">
            <div className="h-8 w-48 bg-slate-700 rounded animate-pulse" />
            <div className="h-4 w-72 bg-slate-700 rounded animate-pulse" />
          </div>
          <div className="h-10 w-40 bg-slate-700 rounded animate-pulse" />
        </div>
        
        {/* Stats skeleton */}
        <div className="grid grid-cols-3 gap-4">
          {[1, 2, 3].map((i) => (
            <SkeletonCard key={i} className="h-24" />
          ))}
        </div>
        
        {/* Table skeleton */}
        <div className="card overflow-hidden">
          <SkeletonTable rows={8} columns={8} />
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-5 sm:space-y-6">
      {/* Page Header */}
      <div className="page-header mb-0">
        <div className="min-w-0">
          <h1 className="page-title">Work Orders</h1>
          <p className="page-subtitle">Manage and track manufacturing orders</p>
        </div>
        <div className="page-actions w-full sm:w-auto" data-tour="wo-create">
          <Link to="/work-orders/new" className="btn-primary w-full sm:w-auto">
            <PlusIcon className="h-5 w-5 mr-2 flex-shrink-0" />
            New Work Order
          </Link>
        </div>
      </div>

      {/* Quick Stats */}
      <MiniStatStrip className="grid grid-cols-2 sm:grid-cols-3 gap-2">
        <MiniStat
          label="Overdue"
          value={stats.overdue}
          icon={ExclamationTriangleIcon}
          iconBg={stats.overdue > 0 ? 'bg-red-500/20' : 'bg-fd-green/15'}
          iconColor={stats.overdue > 0 ? 'text-red-500' : 'text-fd-green'}
          valueColor={stats.overdue > 0 ? 'text-red-500' : undefined}
        />
        <MiniStat
          label="In Progress"
          value={stats.inProgress}
          icon={Squares2X2Icon}
          iconBg="bg-fd-blue/15"
          iconColor="text-fd-blue"
        />
        <MiniStat
          label="Due Today"
          value={stats.dueToday}
          icon={ClockIcon}
          iconBg={stats.dueToday > 0 ? 'bg-amber-500/20' : 'bg-slate-800/50'}
          iconColor={stats.dueToday > 0 ? 'text-fd-amber' : 'text-slate-400'}
          valueColor={stats.dueToday > 0 ? 'text-fd-amber' : undefined}
        />
      </MiniStatStrip>

      {/* Filters */}
      <div className="card rounded-sm border-fd-line p-2.5 sm:p-3" data-tour="wo-filters">
        <div className="grid grid-cols-1 xs:grid-cols-2 lg:grid-cols-[minmax(18rem,1fr)_11rem_13rem_11rem] gap-2 sm:gap-3">
          {/* Search */}
          <div className="relative min-w-0 xs:col-span-2 lg:col-span-1">
            <MagnifyingGlassIcon className="h-5 w-5 absolute left-4 top-1/2 transform -translate-y-1/2 text-surface-400" />
            <input
              type="text"
              placeholder="Search by WO#, part, or customer..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="input pl-11"
            />
          </div>

          {/* Status Filter */}
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            className="input px-3 text-sm sm:px-4 sm:text-base"
            aria-label="Status filter"
          >
            {statusOptions.map(option => (
              <option key={option.value || 'all'} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>

          {/* Customer Filter */}
          <select
            value={customerFilter}
            onChange={(e) => setCustomerFilter(e.target.value)}
            className="input px-3 text-sm sm:px-4 sm:text-base"
            aria-label="Customer filter"
          >
            <option value="">All Customers</option>
            {customers.map(c => (
              <option key={c} value={c}>{c}</option>
            ))}
          </select>

          {/* Group By */}
          <select
            value={groupBy}
            onChange={(e) => setGroupBy(e.target.value as GroupBy)}
            className="input px-3 text-sm sm:px-4 sm:text-base xs:col-span-2 lg:col-span-1"
            aria-label="Group work orders"
          >
            {groupOptions.map(option => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </div>

        {/* Toggle + count row */}
        <div className="flex flex-col xs:flex-row xs:items-center xs:justify-between gap-2 mt-2.5 pt-2.5 border-t border-fd-line">
          <label className="flex items-center gap-2 cursor-pointer group">
            <input
              type="checkbox"
              checked={hideCOTS}
              onChange={(e) => setHideCOTS(e.target.checked)}
              className="checkbox"
            />
            <span className="text-sm text-surface-600 group-hover:text-surface-900">Hide COTS/Hardware</span>
          </label>
          <span className="text-xs text-surface-500 tabular-nums xs:text-right">
            <span className="sm:hidden">
              <span className="font-semibold text-surface-700">{filteredWorkOrders.length}</span> of {workOrders.length} shown
            </span>
            <span className="hidden sm:inline">
              Showing <span className="font-semibold text-surface-700">{filteredWorkOrders.length}</span> of {workOrders.length} work orders
            </span>
          </span>
        </div>
      </div>

      {/* Work Orders List */}
      {loadError && workOrders.length === 0 ? (
        <ErrorState
          message="Could not load work orders."
          onRetry={loadWorkOrders}
        />
      ) : groupBy !== 'none' && groupedWorkOrders ? (
        // Grouped View
        <div className="space-y-4" data-tour="wo-list">
          {groupedWorkOrders.map(([groupName, orders]) => (
            <div key={groupName} className="card card-flush overflow-hidden">
              <div className="bg-surface-50 px-4 py-3 sm:px-6 sm:py-4 border-b border-surface-200">
                <div className="flex items-center justify-between">
                  <h3 className="font-semibold text-surface-900 capitalize">
                    {groupBy === 'status' ? formatStatusLabel(groupName) : groupName}
                  </h3>
                  <span className="badge badge-neutral">
                    {orders.length} order{orders.length !== 1 ? 's' : ''}
                  </span>
                </div>
              </div>
              <div className="hidden lg:block">
                <DataTable
                  columns={buildWorkOrderColumns({
                    hideColumn: groupBy === 'customer' ? 'customer' : groupBy === 'part' ? 'part' : undefined,
                    onDelete: canDeleteWorkOrders ? handleDelete : undefined,
                    onRelease: handleRelease,
                    releasingIds,
                  })}
                  data={orders}
                  rowKey={(wo) => wo.id}
                  onRowClick={(wo) => navigate(`/work-orders/${wo.id}`)}
                  className="border-0"
                />
              </div>
              <WorkOrderMobileList
                workOrders={orders}
                onDelete={canDeleteWorkOrders ? handleDelete : undefined}
                onRelease={handleRelease}
                releasingIds={releasingIds}
                className="lg:hidden p-3"
              />
            </div>
          ))}
          {filteredWorkOrders.length === 0 && <WorkOrdersEmptyState />}
        </div>
      ) : (
        // Flat Responsive View
        <div data-tour="wo-list">
          {filteredWorkOrders.length === 0 ? (
            <WorkOrdersEmptyState />
          ) : (
            <>
              <div className="hidden lg:block">
                <DataTable
                  columns={workOrderColumns}
                  data={filteredWorkOrders}
                  rowKey={(wo) => wo.id}
                  onRowClick={(wo) => navigate(`/work-orders/${wo.id}`)}
                  defaultSort={{ key: 'priority', dir: 'asc' }}
                  pageSize={25}
                  csvExport={{ filename: 'work-orders' }}
                />
              </div>

              <WorkOrderMobileList
                workOrders={filteredWorkOrders}
                onDelete={canDeleteWorkOrders ? handleDelete : undefined}
                onRelease={handleRelease}
                releasingIds={releasingIds}
                className="lg:hidden"
              />
            </>
          )}
        </div>
      )}
    </div>
  );
}

function WorkOrdersEmptyState() {
  return (
    <EmptyState
      icon={ListBulletIcon}
      title="No work orders found"
      description="Try adjusting your filters, or create a new work order to get started."
      action={
        <Link to="/work-orders/new" className="btn-primary">
          <PlusIcon className="h-5 w-5 mr-2 flex-shrink-0" />
          New Work Order
        </Link>
      }
    />
  );
}

function isWorkOrderOverdue(wo: WorkOrderSummary) {
  return Boolean(
    wo.due_date &&
    isDateBeforeTodayInCentral(wo.due_date) &&
    !['complete', 'closed', 'cancelled'].includes(wo.status)
  );
}

interface WorkOrderMobileListProps {
  workOrders: WorkOrderSummary[];
  onDelete?: (wo: WorkOrderSummary) => void;
  onRelease?: (wo: WorkOrderSummary) => void;
  releasingIds?: Set<number>;
  className?: string;
}

const WorkOrderMobileList = React.memo(function WorkOrderMobileList({ workOrders, onDelete, onRelease, releasingIds, className = '' }: WorkOrderMobileListProps) {
  if (workOrders.length === 0) return null;

  return (
    <div className={`space-y-3 ${className}`}>
      {workOrders.map((wo) => (
        <WorkOrderMobileCard
          key={wo.id}
          workOrder={wo}
          onDelete={onDelete}
          onRelease={onRelease}
          isReleasing={Boolean(releasingIds?.has(wo.id))}
        />
      ))}
    </div>
  );
});

interface WorkOrderMobileCardProps {
  workOrder: WorkOrderSummary;
  onDelete?: (wo: WorkOrderSummary) => void;
  onRelease?: (wo: WorkOrderSummary) => void;
  isReleasing?: boolean;
}

const WorkOrderMobileCard = React.memo(function WorkOrderMobileCard({ workOrder: wo, onDelete, onRelease, isReleasing }: WorkOrderMobileCardProps) {
  const priority = priorityConfig[wo.priority] || priorityConfig[4];
  const overdue = isWorkOrderOverdue(wo);
  const canRelease = onRelease && wo.status === 'draft';
  const canDelete = Boolean(onDelete);
  const progress = getWorkOrderProgress(wo);

  return (
    <article className={`mobile-card ${overdue ? 'border-red-500/50 bg-red-500/5' : ''}`}>
      <div className="px-4 py-3 border-b border-slate-700/50 flex items-start justify-between gap-3">
        <div className="min-w-0">
          <Link
            to={`/work-orders/${wo.id}`}
            className="block font-semibold text-werco-400 hover:text-werco-300 truncate"
          >
            {wo.work_order_number}
          </Link>
          <p className="text-sm text-surface-500 truncate mt-0.5">{wo.customer_name || 'No Customer'}</p>
        </div>
        <StatusBadge status={wo.status} className="flex-shrink-0" />
      </div>

      <div className="px-4 py-3 space-y-3">
        <div className="min-w-0">
          <p className="text-sm font-medium text-surface-900 truncate">{wo.part_number || 'No part number'}</p>
          <p className="text-sm text-surface-500 line-clamp-2">{wo.part_name || 'No part description'}</p>
        </div>

        <div className="grid grid-cols-2 gap-3">
          <div>
            <p className="text-xs uppercase tracking-wide text-surface-500">Due</p>
            <p className={`text-sm font-semibold mt-0.5 ${overdue ? 'text-red-400' : 'text-surface-800'}`}>
              {wo.due_date ? formatCentralDate(wo.due_date) : 'No date'}
            </p>
          </div>
          <div>
            <p className="text-xs uppercase tracking-wide text-surface-500">Priority</p>
            <span className={`badge mt-1 ${priority.bg} ${priority.text}`}>P{wo.priority}</span>
          </div>
        </div>

        <div>
          <div className="flex items-center justify-between gap-3">
            <p className="text-xs uppercase tracking-wide text-surface-500">{progress.title}</p>
            <p className="text-sm font-semibold text-surface-800 tabular-nums">
              {progress.label}
            </p>
          </div>
          <div className="mt-2 h-2 bg-surface-200 rounded-full overflow-hidden">
            <div
              className="h-full bg-werco-500 rounded-full transition-all"
              style={{ width: `${progress.percent}%` }}
            />
          </div>
        </div>
      </div>

      <div className="px-4 py-3 bg-slate-800/50 border-t border-slate-700/50 flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          {overdue && <span className="badge badge-danger">Overdue</span>}
        </div>
        <div className="flex items-center gap-2">
          {canRelease && (
            <button
              onClick={() => onRelease?.(wo)}
              disabled={isReleasing}
              className="btn-success btn-sm"
            >
              <CheckCircleIcon className="h-4 w-4 mr-1" />
              Release
            </button>
          )}
          {canDelete && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => onDelete?.(wo)}
              className="text-red-300 hover:text-red-200"
            >
              <TrashIcon className="h-4 w-4 mr-1" />
              Delete
            </Button>
          )}
          <Link
            to={`/work-orders/${wo.id}`}
            className="btn-secondary btn-sm"
          >
            Details
            <ChevronRightIcon className="h-4 w-4 ml-1" />
          </Link>
        </div>
      </div>
    </article>
  );
});

