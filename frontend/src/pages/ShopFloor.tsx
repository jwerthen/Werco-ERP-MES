import React, { useEffect, useState, useMemo, useCallback, useRef } from 'react';
import { useNavigate, useLocation } from 'react-router-dom';
import api from '../services/api';
import { WorkCenter, QueueItem, ActiveJob } from '../types';
import { usePermissions } from '../hooks/usePermissions';
import { useWebSocket } from '../hooks/useWebSocket';
import { buildWsUrl, getAccessToken } from '../services/realtime';
import { calculateDispatchScore } from '../utils/dispatchScore';
import {
  formatCentralDate,
  formatCentralTime,
  getDateSortValue,
  isDateBeforeTodayInCentral,
} from '../utils/centralTime';
import { useToast } from '../components/ui/Toast';
import { Button, EmptyState, ErrorState, FormField, StatusBadge, statusColor, statusVariant } from '../components/ui';
import {
  PlayIcon,
  StopIcon,
  ClockIcon,
  CheckCircleIcon,
  XMarkIcon,
  WrenchScrewdriverIcon,
  ArrowPathIcon,
  ChevronDownIcon,
  ChevronRightIcon,
} from '@heroicons/react/24/solid';
import { QueueListIcon, DocumentTextIcon, ArrowTopRightOnSquareIcon } from '@heroicons/react/24/outline';
import { getKioskDept, getKioskWorkCenterCode, getKioskWorkCenterId, isKioskMode } from '../utils/kiosk';
import { Modal } from '../components/ui/Modal';

interface WorkOrderDetails {
  id: number;
  work_order_number: string;
  customer_name?: string;
  customer_po?: string;
  quantity_ordered: number;
  quantity_complete: number;
  quantity_scrapped: number;
  due_date?: string;
  notes?: string;
  operations: {
    id: number;
    operation_number: number;
    name: string;
    work_center_name: string;
    status: string;
    estimated_hours: number;
    actual_hours: number;
  }[];
}

const WORK_CENTER_STORAGE_KEY = 'shop_floor_work_center_id';

// Solid dot color paired with the central status variant, so the queue pill's
// dot stays in lockstep with the canonical bg/text from statusColor().
const STATUS_DOT_CLASS: Record<ReturnType<typeof statusVariant>, string> = {
  green: 'bg-emerald-400',
  blue: 'bg-blue-400',
  amber: 'bg-amber-400',
  red: 'bg-red-400',
  slate: 'bg-slate-400',
};
const dotClass = (status: string) => STATUS_DOT_CLASS[statusVariant(status)];

export default function ShopFloor() {
  const { can } = usePermissions();
  const { showToast } = useToast();
  const navigate = useNavigate();
  const location = useLocation();
  const [workCenters, setWorkCenters] = useState<WorkCenter[]>([]);
  const [selectedWorkCenter, _setSelectedWorkCenter] = useState<number | null>(null);

  const setSelectedWorkCenter = useCallback((id: number | null) => {
    _setSelectedWorkCenter(id);
    if (id) {
      localStorage.setItem(WORK_CENTER_STORAGE_KEY, String(id));
    }
  }, []);
  const [queue, setQueue] = useState<QueueItem[]>([]);
  const [activeJobs, setActiveJobs] = useState<ActiveJob[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [initialError, setInitialError] = useState(false);
  const [queueError, setQueueError] = useState(false);
  const [detailErrors, setDetailErrors] = useState<Set<number>>(new Set());
  const [clockOutModal, setClockOutModal] = useState(false);
  const [clockOutJob, setClockOutJob] = useState<ActiveJob | null>(null);
  const [clockOutData, setClockOutData] = useState({ quantity_produced: 0, quantity_scrapped: 0, notes: '' });
  const [clockOutShowMore, setClockOutShowMore] = useState(false);
  const [expandedRows, setExpandedRows] = useState<Set<number>>(new Set());
  const [workOrderDetails, setWorkOrderDetails] = useState<Record<number, WorkOrderDetails>>({});
  const [updatingPriorityWorkOrderId, setUpdatingPriorityWorkOrderId] = useState<number | null>(null);
  const [reportingBlockerOperationId, setReportingBlockerOperationId] = useState<number | null>(null);
  const [clockingInOperationId, setClockingInOperationId] = useState<number | null>(null);
  const [clockingOut, setClockingOut] = useState(false);
  // Back-entry (offline paper catch-up) mode. When on, clock-in/clock-out send
  // source='backfill' so the rows are excluded from live shop metrics + audited.
  // Ephemeral state on purpose (clears on reload) so it can't be silently left on.
  const [backEntry, setBackEntry] = useState(false);
  const [priorityReason, setPriorityReason] = useState('');
  const [notice, setNotice] = useState<{ type: 'success' | 'error' | 'info'; message: string } | null>(null);
  const realtimeRefreshRef = useRef<NodeJS.Timeout | null>(null);
  const isKiosk = useMemo(() => isKioskMode(location.pathname, location.search), [location.pathname, location.search]);
  const kioskParams = useMemo(() => {
    return {
      dept: getKioskDept(location.search),
      workCenterId: getKioskWorkCenterId(location.search),
      workCenterCode: getKioskWorkCenterCode(location.search),
    };
  }, [location.search]);
  const highlightWO = useMemo(() => new URLSearchParams(location.search).get('wo'), [location.search]);
  const realtimeUrl = useMemo(() => {
    if (!selectedWorkCenter) return null;
    const token = getAccessToken();
    if (!token) return null;
    return buildWsUrl(`/ws/shop-floor/${selectedWorkCenter}`, { token });
  }, [selectedWorkCenter]);
  const canEditPriority = can('work_orders:edit');
  // Back-entry is a supervisor+ (back-entry owner) capability. work_orders:edit is
  // held by supervisor/manager/admin/platform_admin (and superuser short-circuits
  // can()) but NOT operators — so operators can't tag their own live work as
  // backfill to dodge the live-capture metrics.
  const canBackEntry = can('work_orders:edit');

  const notify = useCallback((type: 'success' | 'error' | 'info', message: string) => {
    setNotice({ type, message });
    showToast(type, message);
  }, [showToast]);

  useEffect(() => {
    if (!isKiosk) return;
    navigate(`/shop-floor/operations${location.search || '?kiosk=1'}`, { replace: true });
  }, [isKiosk, location.search, navigate]);

  const loadInitialData = useCallback(async () => {
    setInitialError(false);
    try {
      const [wcResponse, activeResponse] = await Promise.all([
        api.getWorkCenters(),
        api.getMyActiveJob()
      ]);
      setWorkCenters(wcResponse);
      setActiveJobs(activeResponse.active_jobs || (activeResponse.active_job ? [activeResponse.active_job] : []));
      if (wcResponse.length > 0) {
        const deptMatch = kioskParams.dept?.toLowerCase() || null;
        const matched = wcResponse.find((wc) => {
          if (kioskParams.workCenterId && wc.id === kioskParams.workCenterId) return true;
          if (kioskParams.workCenterCode && wc.code.toLowerCase() === kioskParams.workCenterCode.toLowerCase()) return true;
          if (deptMatch) {
            return (
              wc.work_center_type?.toString().toLowerCase().includes(deptMatch) ||
              wc.name.toLowerCase().includes(deptMatch) ||
              wc.code.toLowerCase().includes(deptMatch)
            );
          }
          return false;
        });
        // Use URL params > localStorage > first work center
        const storedId = Number(localStorage.getItem(WORK_CENTER_STORAGE_KEY));
        const storedMatch = storedId ? wcResponse.find((wc) => wc.id === storedId) : null;
        setSelectedWorkCenter(matched?.id ?? storedMatch?.id ?? wcResponse[0].id);
      }
    } catch (err) {
      console.error('Failed to load data:', err);
      setInitialError(true);
    } finally {
      setLoading(false);
    }
  }, [kioskParams.dept, kioskParams.workCenterCode, kioskParams.workCenterId, setSelectedWorkCenter]);

  const checkActiveJob = useCallback(async () => {
    try {
      const response = await api.getMyActiveJob();
      setActiveJobs(response.active_jobs || (response.active_job ? [response.active_job] : []));
    } catch (err) {
      console.error('Failed to check active job:', err);
    }
  }, []);

  const loadQueue = useCallback(async (workCenterId: number) => {
    setQueueError(false);
    try {
      const response = await api.getWorkCenterQueue(workCenterId);
      setQueue(response.queue);
    } catch (err) {
      console.error('Failed to load queue:', err);
      setQueueError(true);
    }
  }, []);

  const scheduleRealtimeRefresh = useCallback(() => {
    if (realtimeRefreshRef.current) return;
    realtimeRefreshRef.current = setTimeout(async () => {
      realtimeRefreshRef.current = null;
      await Promise.all([
        checkActiveJob(),
        selectedWorkCenter ? loadQueue(selectedWorkCenter) : Promise.resolve()
      ]);
    }, 500);
  }, [checkActiveJob, loadQueue, selectedWorkCenter]);

  useWebSocket({
    url: realtimeUrl,
    enabled: Boolean(realtimeUrl),
    onMessage: (message) => {
      if (message.type === 'connected' || message.type === 'ping') return;
      if (!['shop_floor_update', 'work_order_update', 'dashboard_update'].includes(message.type)) return;

      const messageWorkCenterId = message.data?.work_center_id;
      if (messageWorkCenterId && selectedWorkCenter && messageWorkCenterId !== selectedWorkCenter) return;
      scheduleRealtimeRefresh();
    }
  });

  useEffect(() => {
    if (isKiosk) return;
    loadInitialData();
    const interval = setInterval(checkActiveJob, 10000);
    return () => clearInterval(interval);
  }, [checkActiveJob, isKiosk, loadInitialData]);

  useEffect(() => {
    return () => {
      if (realtimeRefreshRef.current) {
        clearTimeout(realtimeRefreshRef.current);
        realtimeRefreshRef.current = null;
      }
    };
  }, []);

  useEffect(() => {
    if (selectedWorkCenter) {
      loadQueue(selectedWorkCenter);
    }
  }, [selectedWorkCenter, loadQueue]);

  // Auto-expand work order from scanner ?wo= param
  useEffect(() => {
    if (highlightWO && queue.length > 0) {
      const match = queue.find((item) => item.work_order_number === highlightWO);
      if (match) {
        toggleRowExpansion(match.work_order_id);
      }
    }
    // Only run when queue first loads with highlight param

  }, [highlightWO, queue.length]);

  const handleRefresh = async () => {
    setRefreshing(true);
    await Promise.all([
      checkActiveJob(),
      selectedWorkCenter ? loadQueue(selectedWorkCenter) : Promise.resolve()
    ]);
    setRefreshing(false);
  };

  const toggleRowExpansion = async (workOrderId: number) => {
    const newExpanded = new Set(expandedRows);
    if (newExpanded.has(workOrderId)) {
      newExpanded.delete(workOrderId);
    } else {
      newExpanded.add(workOrderId);
      // Load work order details if not already loaded
      if (!workOrderDetails[workOrderId]) {
        await loadWorkOrderDetails(workOrderId);
      }
    }
    setExpandedRows(newExpanded);
  };

  const loadWorkOrderDetails = async (workOrderId: number) => {
    setDetailErrors((prev) => {
      if (!prev.has(workOrderId)) return prev;
      const next = new Set(prev);
      next.delete(workOrderId);
      return next;
    });
    try {
      const response = await api.getWorkOrder(workOrderId);
      setWorkOrderDetails(prev => ({
        ...prev,
        [workOrderId]: response
      }));
    } catch (err) {
      console.error('Failed to load work order details:', err);
      setDetailErrors((prev) => new Set(prev).add(workOrderId));
    }
  };

  const handleClockIn = async (item: QueueItem) => {
    if (!selectedWorkCenter || clockingInOperationId) return;

    setClockingInOperationId(item.operation_id);
    try {
      await api.clockIn({
        work_order_id: item.work_order_id,
        operation_id: item.operation_id,
        work_center_id: selectedWorkCenter,
        entry_type: 'run',
        // Only tag when back-entry mode is on; otherwise send no source (NULL default).
        ...(backEntry ? { source: 'backfill' } : {}),
      });
      await checkActiveJob();
      await loadQueue(selectedWorkCenter);
      notify('success', `Clocked in to ${item.work_order_number}`);
    } catch (err: any) {
      notify('error', err.response?.data?.detail || 'Failed to clock in');
    } finally {
      setClockingInOperationId(null);
    }
  };

  const handleClockOut = async () => {
    if (!clockOutJob || clockingOut) return;

    setClockingOut(true);
    try {
      await api.clockOut(clockOutJob.time_entry_id, {
        quantity_produced: clockOutData.quantity_produced,
        quantity_scrapped: clockOutData.quantity_scrapped,
        notes: clockOutData.notes,
        // Only tag when back-entry mode is on; otherwise send no source (NULL default).
        ...(backEntry ? { source: 'backfill' } : {}),
      });
      await checkActiveJob();
      setClockOutJob(null);
      setClockOutModal(false);
      setClockOutData({ quantity_produced: 0, quantity_scrapped: 0, notes: '' });
      if (selectedWorkCenter) {
        await loadQueue(selectedWorkCenter);
      }
      notify('success', `Clocked out of ${clockOutJob.work_order_number}`);
    } catch (err: any) {
      notify('error', err.response?.data?.detail || 'Failed to clock out');
    } finally {
      setClockingOut(false);
    }
  };

  const getElapsedTime = (clockIn: string) => {
    const start = new Date(clockIn);
    const now = new Date();
    const diff = now.getTime() - start.getTime();
    const hours = Math.floor(diff / 3600000);
    const minutes = Math.floor((diff % 3600000) / 60000);
    return `${hours}h ${minutes}m`;
  };

  const formatClockInTime = (clockIn: string) => {
    return formatCentralTime(clockIn);
  };

  const closeClockOutModal = () => {
    setClockOutModal(false);
    setClockOutJob(null);
    setClockOutData({ quantity_produced: 0, quantity_scrapped: 0, notes: '' });
    setClockOutShowMore(false);
  };

  const getPriorityClasses = (priority: number) => {
    if (priority <= 2) return 'bg-red-500/20 text-red-400';
    if (priority <= 5) return 'bg-amber-500/20 text-amber-400';
    return 'bg-surface-100 text-surface-600';
  };

  const sortedQueue = useMemo(() => {
    return [...queue].sort((a, b) => {
      const aScore = calculateDispatchScore({
        priority: a.priority,
        dueDate: a.due_date || null,
        remainingHours: Number(a.setup_time_hours || 0) + Number(a.run_time_hours || 0),
        scheduledStart: null,
        status: a.status,
      });
      const bScore = calculateDispatchScore({
        priority: b.priority,
        dueDate: b.due_date || null,
        remainingHours: Number(b.setup_time_hours || 0) + Number(b.run_time_hours || 0),
        scheduledStart: null,
        status: b.status,
      });
      if (aScore !== bScore) return bScore - aScore;
      if (a.priority !== b.priority) return a.priority - b.priority;
      const aDue = getDateSortValue(a.due_date);
      const bDue = getDateSortValue(b.due_date);
      if (aDue !== bDue) return aDue - bDue;
      return a.work_order_number.localeCompare(b.work_order_number);
    });
  }, [queue]);

  const priorityFocusQueue = useMemo(() => {
    return sortedQueue.slice(0, 5);
  }, [sortedQueue]);

  const handlePriorityChange = async (workOrderId: number, priorityRaw: string) => {
    const priority = parseInt(priorityRaw, 10);
    if (Number.isNaN(priority)) return;

    const existing = queue.find((item) => item.work_order_id === workOrderId);
    if (!existing || existing.priority === priority) return;
    if (updatingPriorityWorkOrderId) return;

    setUpdatingPriorityWorkOrderId(workOrderId);
    try {
      const reason = priorityReason.trim() || undefined;
      await api.updateWorkOrderPriority(workOrderId, priority, reason);
      setQueue((prev) =>
        prev.map((item) =>
          item.work_order_id === workOrderId ? { ...item, priority } : item
        )
      );
      if (reason) {
        setPriorityReason('');
      }
      notify('success', `Updated ${existing.work_order_number} to priority ${priority}`);
    } catch (err: any) {
      notify('error', err.response?.data?.detail || 'Failed to update priority');
    } finally {
      setUpdatingPriorityWorkOrderId(null);
    }
  };

  const handleReportMaterialBlocker = async (item: QueueItem) => {
    const note = window.prompt(
      `Report missing material for ${item.work_order_number}?`,
      'Operator reported material is not available at the work center.'
    );
    if (note === null) return;

    setReportingBlockerOperationId(item.operation_id);
    try {
      await api.createWorkOrderBlocker(item.work_order_id, {
        operation_id: item.operation_id,
        category: 'material_missing',
        severity: 'high',
        note: note.trim() || 'Operator reported material is not available at the work center.',
        put_operation_on_hold: true,
      });
      notify('success', `Reported missing material for ${item.work_order_number}`);
      if (selectedWorkCenter) {
        await loadQueue(selectedWorkCenter);
      }
    } catch (err: any) {
      notify('error', err.response?.data?.detail || 'Failed to report material blocker');
    } finally {
      setReportingBlockerOperationId(null);
    }
  };

  if (isKiosk) {
    return (
      <div className="flex items-center justify-center h-96">
        <div className="text-center">
          <div className="spinner h-12 w-12 mx-auto mb-4"></div>
          <p className="text-surface-500">Opening mobile shop floor...</p>
        </div>
      </div>
    );
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-96">
        <div className="text-center">
          <div className="spinner h-12 w-12 mx-auto mb-4"></div>
          <p className="text-surface-500">Loading shop floor...</p>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {notice && (
        <div
          className={`rounded-sm border px-3 py-2.5 flex items-start justify-between gap-4 ${
            notice.type === 'success'
              ? 'bg-emerald-500/10 border-emerald-500/30 text-emerald-300'
              : notice.type === 'error'
              ? 'bg-red-500/10 border-red-500/30 text-red-300'
              : 'bg-blue-500/10 border-blue-500/30 text-blue-300'
          }`}
          role="status"
        >
          <p className="text-sm font-medium">{notice.message}</p>
          <button
            type="button"
            onClick={() => setNotice(null)}
            className="rounded-sm p-1 text-current opacity-70 hover:opacity-100"
            aria-label="Dismiss notification"
          >
            <XMarkIcon className="h-4 w-4" />
          </button>
        </div>
      )}

      {/* Page Header */}
      <div className="page-header">
        <div>
          <h1 className="page-title flex items-center gap-3">
            <WrenchScrewdriverIcon className="h-8 w-8 text-werco-600" />
            Shop Floor
          </h1>
          <p className="page-subtitle">Clock in/out and manage work center queues</p>
        </div>
        <div className="page-actions">
          {canBackEntry && (
            <label
              className={`flex items-center gap-2.5 rounded-sm border px-3 py-1.5 cursor-pointer transition-colors ${
                backEntry
                  ? 'border-amber-500/60 bg-amber-500/15 text-amber-200'
                  : 'border-fd-line bg-fd-panel text-surface-400 hover:border-amber-400/40 hover:text-surface-200'
              }`}
              title="Marks this entry as offline paper catch-up — excluded from live shop metrics."
            >
              <input
                type="checkbox"
                checked={backEntry}
                onChange={(e) => setBackEntry(e.target.checked)}
                className="checkbox"
                aria-label="Back-entry mode (offline paper catch-up — excluded from live shop metrics)"
              />
              <span className="flex flex-col leading-tight">
                <span className="text-sm font-semibold flex items-center gap-1.5">
                  Back-entry (offline catch-up)
                  {backEntry && (
                    <span className="rounded-sm bg-amber-500/30 px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide text-amber-100">
                      On
                    </span>
                  )}
                </span>
                <span className="text-[11px] font-normal opacity-80">
                  Excluded from live shop metrics
                </span>
              </span>
            </label>
          )}
          <Button
            variant="secondary"
            onClick={handleRefresh}
            disabled={refreshing}
          >
            <ArrowPathIcon className={`h-5 w-5 mr-2 ${refreshing ? 'animate-spin' : ''}`} />
            Refresh
          </Button>
        </div>
      </div>

      {/* Initial load failure */}
      {initialError && (
        <ErrorState
          message="Could not load shop floor data. Check your connection and try again."
          onRetry={loadInitialData}
        />
      )}

      {/* Active Job Banner */}
      {activeJobs.length > 0 && (
        <div className="space-y-2">
          {activeJobs.map((job) => (
            <div
              key={job.time_entry_id}
              className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-sm border border-emerald-500/30 bg-emerald-500/10 px-3 py-2.5"
            >
              <div className="flex items-center gap-2 min-w-0">
                <span className="h-2 w-2 rounded-full bg-emerald-400 animate-pulse shrink-0"></span>
                <span className="text-xs font-medium uppercase tracking-wide text-emerald-300 shrink-0">
                  Working On
                </span>
                <span className="font-semibold text-emerald-100 truncate">
                  {job.work_order_number} - {job.operation_name}
                </span>
                <span className="text-sm text-emerald-300/80 truncate hidden sm:inline">
                  {job.part_number} - {job.part_name}
                </span>
              </div>
              <div className="flex items-center gap-3 ml-auto">
                <span className="text-xs text-emerald-300/80 hidden sm:inline">
                  Started {formatClockInTime(job.clock_in)}
                </span>
                <span className="flex items-center gap-1.5 font-mono font-semibold text-emerald-100 tabular-nums">
                  <ClockIcon className="h-4 w-4" />
                  {getElapsedTime(job.clock_in)}
                </span>
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => {
                    setClockOutJob(job);
                    const remaining = Math.max(0, (job.quantity_ordered || 0) - (job.quantity_complete || 0));
                    setClockOutData({ quantity_produced: remaining, quantity_scrapped: 0, notes: '' });
                    setClockOutModal(true);
                  }}
                >
                  <StopIcon className="h-4 w-4 mr-1.5" />
                  Clock Out
                </Button>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Work Center Selector */}
      <div className="flex flex-wrap gap-2" data-tour="sf-clock">
        {workCenters.map((wc) => (
          <button
            key={wc.id}
            onClick={() => setSelectedWorkCenter(wc.id)}
            className={`
              px-3 py-2 rounded-sm font-semibold transition-colors
              ${selectedWorkCenter === wc.id
                ? 'bg-werco-600 text-white'
                : 'bg-fd-panel text-slate-200 border border-fd-line hover:border-werco-400 hover:bg-werco-500/10'
              }
            `}
          >
            {wc.name}
          </button>
        ))}
      </div>

      {/* Priority Focus Queue — slim cross-linked strip into the table below */}
      {priorityFocusQueue.length > 0 && (
        <div className="card" data-tour="sf-complete">
          <div className="flex items-center justify-between mb-2">
            <h2 className="text-sm font-semibold text-surface-900">Priority Focus Queue</h2>
            <span className="text-xs text-surface-500">Top {priorityFocusQueue.length} to run next</span>
          </div>
          <div className="flex flex-wrap gap-2">
            {priorityFocusQueue.map((item, idx) => {
              const overdue = Boolean(item.due_date && isDateBeforeTodayInCentral(item.due_date));
              return (
                <button
                  key={`focus-${item.operation_id}`}
                  type="button"
                  className={`flex items-center gap-2 px-2.5 py-1.5 rounded-sm border min-w-0 transition-colors ${
                    overdue ? 'border-red-500/30 bg-red-500/10 hover:bg-red-500/20' : 'border-fd-line bg-fd-panel hover:bg-fd-sunken'
                  }`}
                  onClick={() => toggleRowExpansion(item.work_order_id)}
                  title={`${item.work_order_number} - ${item.operation_name}`}
                >
                  <span className="text-xs font-semibold text-surface-500 tabular-nums shrink-0">#{idx + 1}</span>
                  <span className={`px-1.5 py-0.5 rounded-sm text-xs font-semibold tabular-nums shrink-0 ${getPriorityClasses(item.priority)}`}>
                    P{item.priority}
                  </span>
                  <span className="text-sm font-semibold text-werco-700 truncate">{item.work_order_number}</span>
                  <span className={`text-xs shrink-0 tabular-nums ${overdue ? 'text-red-600 font-medium' : 'text-surface-500'}`}>
                    {item.due_date ? formatCentralDate(item.due_date, { year: undefined }) : '—'}
                  </span>
                </button>
              );
            })}
          </div>
        </div>
      )}

      {/* Job Queue */}
      <div className="card card-flush" data-tour="sf-operations">
        <div className="px-3 py-3 border-b border-fd-line flex items-center justify-between">
          <div>
            <h2 className="text-sm font-semibold text-surface-900">
              Job Queue
            </h2>
            <p className="text-xs text-surface-500">
              {workCenters.find(wc => wc.id === selectedWorkCenter)?.name} &bull; <span className="tabular-nums">{sortedQueue.length}</span> job{sortedQueue.length !== 1 ? 's' : ''}
            </p>
          </div>
          {canEditPriority && (
            <FormField
              label="Optional Priority Reason"
              className="w-80 hidden lg:block"
              labelClassName="text-xs font-medium text-surface-600 block mb-1"
            >
              {(field) => (
                <input
                  {...field}
                  type="text"
                  value={priorityReason}
                  onChange={(e) => setPriorityReason(e.target.value)}
                  className="input text-sm"
                  maxLength={500}
                  placeholder="Applied to your next priority change"
                />
              )}
            </FormField>
          )}
        </div>

        {queueError ? (
          <ErrorState
            className="py-12"
            message="Could not load the job queue for this work center."
            onRetry={() => {
              if (selectedWorkCenter) loadQueue(selectedWorkCenter);
            }}
          />
        ) : sortedQueue.length === 0 ? (
          <EmptyState
            className="py-12"
            icon={QueueListIcon}
            title="No jobs in queue"
            description="Select a different work center or check back later."
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="table">
              <thead>
                <tr>
                  <th className="w-10" aria-label="Expand row"></th>
                  <th>Priority</th>
                  <th>Work Order</th>
                  <th>Part</th>
                  <th>Operation</th>
                  <th>Progress</th>
                  <th>Due Date</th>
                  <th>Status</th>
                  <th className="w-40">Action</th>
                </tr>
              </thead>
              <tbody>
                {sortedQueue.map((item) => {
                  const progress = (item.quantity_complete / item.quantity_ordered) * 100;
                  const isOverdue = Boolean(item.due_date && isDateBeforeTodayInCentral(item.due_date));
                  const isExpanded = expandedRows.has(item.work_order_id);
                  const details = workOrderDetails[item.work_order_id];

                  return (
                    <React.Fragment key={item.operation_id}>
                      <tr
                        className={`${isOverdue ? 'bg-red-500/10' : ''} ${isExpanded ? 'bg-werco-50/50' : ''} cursor-pointer hover:bg-surface-50`}
                        onClick={() => toggleRowExpansion(item.work_order_id)}
                      >
                        <td className="w-10">
                          <button className="p-1 rounded hover:bg-surface-200 transition-colors">
                            {isExpanded ? (
                              <ChevronDownIcon className="h-5 w-5 text-surface-500" />
                            ) : (
                              <ChevronRightIcon className="h-5 w-5 text-surface-400" />
                            )}
                          </button>
                        </td>
                        <td>
                          {canEditPriority ? (
                            <select
                              value={item.priority}
                              onClick={(e) => e.stopPropagation()}
                              onChange={(e) => handlePriorityChange(item.work_order_id, e.target.value)}
                              disabled={updatingPriorityWorkOrderId !== null}
                              className={`px-2 py-1 rounded-sm text-xs font-bold border border-transparent tabular-nums ${getPriorityClasses(item.priority)}`}
                              title={updatingPriorityWorkOrderId === item.work_order_id ? 'Updating priority...' : 'Update priority'}
                            >
                              {[1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map((p) => (
                                <option key={p} value={p}>
                                  P{p}
                                </option>
                              ))}
                            </select>
                          ) : (
                            <span className={`inline-flex items-center justify-center w-10 h-10 rounded-sm text-sm font-bold tabular-nums ${getPriorityClasses(item.priority)}`}>
                              P{item.priority}
                            </span>
                          )}
                        </td>
                        <td>
                          <span className="font-semibold text-werco-600">{item.work_order_number}</span>
                        </td>
                        <td aria-label="Part">
                          <div>
                            <p className="font-medium text-surface-900">{item.part_number}</p>
                            <p className="text-sm text-surface-500 line-clamp-1">{item.part_name}</p>
                          </div>
                        </td>
                        <td aria-label="Operation">
                          <div>
                            <p className="font-medium text-surface-900">Op {item.operation_number}</p>
                            <p className="text-sm text-surface-500">{item.operation_name}</p>
                          </div>
                        </td>
                        <td aria-label="Progress">
                          <div className="w-32">
                            <div className="flex items-center justify-between text-sm mb-1">
                              <span className="font-medium text-surface-700 tabular-nums">
                                {item.quantity_complete}/{item.quantity_ordered}
                              </span>
                              <span className="text-surface-500 tabular-nums">{Math.round(progress)}%</span>
                            </div>
                            <div className="h-2 bg-surface-200 rounded-full overflow-hidden">
                              <div
                                className="h-full bg-werco-500 rounded-full transition-all"
                                style={{ width: `${Math.min(100, progress)}%` }}
                              />
                            </div>
                          </div>
                        </td>
                        <td>
                          <span className={`text-sm font-medium ${isOverdue ? 'text-red-600' : 'text-surface-700'}`}>
                            {item.due_date ? formatCentralDate(item.due_date, { year: undefined }) : '\u2014'}
                          </span>
                          {isOverdue && (
                            <span className="block text-xs text-red-500 font-medium">OVERDUE</span>
                          )}
                        </td>
                        <td>
                          <span className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-sm text-xs font-semibold capitalize ${statusColor(item.status)}`}>
                            <span className={`w-1.5 h-1.5 rounded-full ${dotClass(item.status)}`}></span>
                            {item.status.replace('_', ' ')}
                          </span>
                        </td>
                        <td onClick={(e) => e.stopPropagation()}>
                          <div className="flex flex-col gap-2">
                            {item.status === 'in_progress' ? (
                              <span className="inline-flex items-center gap-1.5 text-emerald-600 font-medium">
                                <CheckCircleIcon className="h-5 w-5" />
                                Active
                              </span>
                            ) : (
                              <button
                                onClick={() => handleClockIn(item)}
                                disabled={clockingInOperationId !== null || reportingBlockerOperationId !== null}
                                className="btn-success btn-sm w-full"
                              >
                                {clockingInOperationId === item.operation_id ? (
                                  <ArrowPathIcon className="h-4 w-4 mr-1.5 animate-spin" />
                                ) : (
                                  <PlayIcon className="h-4 w-4 mr-1.5" />
                                )}
                                {clockingInOperationId === item.operation_id ? 'Starting...' : 'Start'}
                              </button>
                            )}
                            <button
                              onClick={() => handleReportMaterialBlocker(item)}
                              disabled={reportingBlockerOperationId !== null || item.status === 'on_hold'}
                              className="btn-secondary btn-sm w-full text-amber-700"
                              title="Report missing material"
                            >
                              {reportingBlockerOperationId === item.operation_id ? (
                                <ArrowPathIcon className="h-4 w-4 mr-1.5 animate-spin" />
                              ) : (
                                <StopIcon className="h-4 w-4 mr-1.5" />
                              )}
                              {reportingBlockerOperationId === item.operation_id ? 'Reporting...' : 'No Material'}
                            </button>
                          </div>
                        </td>
                      </tr>

                      {/* Expanded Details Row */}
                      {isExpanded && (
                        <tr className="bg-fd-sunken">
                          <td colSpan={9} className="p-0">
                            <div className="p-3 border-t border-fd-line">
                              {details ? (
                                <div className="space-y-3">
                                  {/* Header with link */}
                                  <div className="flex items-center justify-between">
                                    <h3 className="text-sm font-semibold text-surface-900">
                                      Work Order Details
                                    </h3>
                                    <Button
                                      variant="secondary"
                                      size="sm"
                                      onClick={(e) => {
                                        e.stopPropagation();
                                        navigate(`/work-orders/${details.id}`);
                                      }}
                                    >
                                      <ArrowTopRightOnSquareIcon className="h-4 w-4 mr-1.5" />
                                      Open Full View
                                    </Button>
                                  </div>

                                  {/* Definition row */}
                                  <dl className="flex flex-wrap gap-x-8 gap-y-2 rounded-sm border border-fd-line bg-fd-panel p-3">
                                    <div className="min-w-0">
                                      <dt className="text-xs text-surface-500">Customer</dt>
                                      <dd className="font-semibold text-surface-900 truncate">{details.customer_name || '\u2014'}</dd>
                                    </div>
                                    <div className="min-w-0">
                                      <dt className="text-xs text-surface-500">Customer PO</dt>
                                      <dd className="font-semibold text-surface-900 truncate">{details.customer_po || '\u2014'}</dd>
                                    </div>
                                    <div className="min-w-0">
                                      <dt className="text-xs text-surface-500">Qty Complete / Ordered</dt>
                                      <dd className="font-semibold text-surface-900 tabular-nums">
                                        {details.quantity_complete} / {details.quantity_ordered}
                                      </dd>
                                    </div>
                                    <div className="min-w-0">
                                      <dt className="text-xs text-surface-500">Due Date</dt>
                                      <dd className="font-semibold text-surface-900 tabular-nums">
                                        {details.due_date ? formatCentralDate(details.due_date) : '\u2014'}
                                      </dd>
                                    </div>
                                  </dl>

                                  {/* Notes */}
                                  {details.notes && (
                                    <div className="bg-amber-500/10 border border-amber-500/30 rounded-sm p-3">
                                      <div className="flex items-start gap-2">
                                        <DocumentTextIcon className="h-5 w-5 text-amber-600 mt-0.5" />
                                        <div>
                                          <p className="font-medium text-amber-300">Notes</p>
                                          <p className="text-sm text-amber-400">{details.notes}</p>
                                        </div>
                                      </div>
                                    </div>
                                  )}

                                  {/* Operations Table */}
                                  <div>
                                    <h4 className="text-xs font-medium text-surface-700 mb-2">All Operations</h4>
                                    <div className="bg-fd-panel rounded-sm border border-fd-line overflow-hidden">
                                      <table className="w-full text-sm">
                                        <thead className="bg-fd-sunken">
                                          <tr>
                                            <th className="px-3 py-2 text-left font-medium text-surface-600">Op #</th>
                                            <th className="px-3 py-2 text-left font-medium text-surface-600">Operation</th>
                                            <th className="px-3 py-2 text-left font-medium text-surface-600">Work Center</th>
                                            <th className="px-3 py-2 text-left font-medium text-surface-600">Status</th>
                                            <th className="px-3 py-2 text-right font-medium text-surface-600">Est. Hrs</th>
                                            <th className="px-3 py-2 text-right font-medium text-surface-600">Actual Hrs</th>
                                          </tr>
                                        </thead>
                                        <tbody>
                                          {details.operations.map((op) => (
                                            <tr
                                              key={op.id}
                                              className={`border-t border-fd-line ${op.id === item.operation_id ? 'bg-werco-50' : ''}`}
                                            >
                                              <td className="px-3 py-2 font-medium tabular-nums">{op.operation_number}</td>
                                              <td className="px-3 py-2">{op.name}</td>
                                              <td className="px-3 py-2">{op.work_center_name}</td>
                                              <td className="px-3 py-2">
                                                <StatusBadge status={op.status} className="rounded-sm" />
                                              </td>
                                              <td className="px-3 py-2 text-right tabular-nums">{op.estimated_hours?.toFixed(1) || '\u2014'}</td>
                                              <td className="px-3 py-2 text-right tabular-nums">{op.actual_hours?.toFixed(1) || '\u2014'}</td>
                                            </tr>
                                          ))}
                                        </tbody>
                                      </table>
                                    </div>
                                  </div>
                                </div>
                              ) : detailErrors.has(item.work_order_id) ? (
                                <ErrorState
                                  className="py-8"
                                  message="Could not load work order details."
                                  onRetry={() => loadWorkOrderDetails(item.work_order_id)}
                                />
                              ) : (
                                <div className="flex items-center justify-center py-8">
                                  <div className="text-center">
                                    <ArrowPathIcon className="h-6 w-6 animate-spin text-surface-400 mx-auto mb-2" />
                                    <p className="text-sm text-surface-500">Loading details...</p>
                                  </div>
                                </div>
                              )}
                            </div>
                          </td>
                        </tr>
                      )}
                    </React.Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Clock Out Modal */}
      <Modal open={clockOutModal} onClose={closeClockOutModal} size="md" closeOnBackdrop={!clockingOut}>
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-lg font-semibold text-surface-900">Clock Out</h3>
          <button
            onClick={closeClockOutModal}
            disabled={clockingOut}
            className="p-2 rounded-sm text-surface-400 hover:text-surface-600 hover:bg-fd-sunken"
          >
            <XMarkIcon className="h-5 w-5" />
          </button>
        </div>

        <div className="space-y-4">
          <div className="bg-fd-sunken rounded-sm p-3 mb-4 border border-fd-line">
            <p className="text-sm text-surface-500 mb-1">Completing work on</p>
            <p className="font-semibold text-surface-900">
              {clockOutJob?.work_order_number} — {clockOutJob?.operation_name}
            </p>
            {clockOutJob?.quantity_ordered ? (
              <p className="text-xs text-surface-500 mt-1 tabular-nums">
                {clockOutJob.quantity_complete || 0} of {clockOutJob.quantity_ordered} previously completed
              </p>
            ) : null}
          </div>

          <FormField label="Quantity Produced">
            {(field) => (
              <input
                {...field}
                type="number"
                min="0"
                value={clockOutData.quantity_produced}
                onChange={(e) => setClockOutData({ ...clockOutData, quantity_produced: parseFloat(e.target.value) || 0 })}
                className="input text-center text-2xl font-semibold h-14 tabular-nums"
                autoFocus
              />
            )}
          </FormField>

          {!clockOutShowMore ? (
            <button
              type="button"
              onClick={() => setClockOutShowMore(true)}
              className="text-sm text-werco-600 hover:text-werco-700 font-medium"
            >
              + Add scrap count or notes
            </button>
          ) : (
            <>
              <FormField label="Quantity Scrapped">
                {(field) => (
                  <input
                    {...field}
                    type="number"
                    min="0"
                    value={clockOutData.quantity_scrapped}
                    onChange={(e) => setClockOutData({ ...clockOutData, quantity_scrapped: parseFloat(e.target.value) || 0 })}
                    className="input text-center text-lg font-semibold tabular-nums"
                  />
                )}
              </FormField>
              <FormField label="Notes">
                {(field) => (
                  <textarea
                    {...field}
                    value={clockOutData.notes}
                    onChange={(e) => setClockOutData({ ...clockOutData, notes: e.target.value })}
                    className="input"
                    rows={2}
                    placeholder="Any issues, observations, or notes..."
                  />
                )}
              </FormField>
            </>
          )}
        </div>

        <div className="flex justify-end gap-3 mt-6">
          <Button
            variant="secondary"
            onClick={closeClockOutModal}
            disabled={clockingOut}
          >
            Cancel
          </Button>
          <Button
            onClick={handleClockOut}
            disabled={clockingOut}
          >
            {clockingOut ? (
              <ArrowPathIcon className="h-5 w-5 mr-2 animate-spin" />
            ) : (
              <CheckCircleIcon className="h-5 w-5 mr-2" />
            )}
            {clockingOut ? 'Saving...' : 'Complete Clock Out'}
          </Button>
        </div>
      </Modal>
    </div>
  );
}
