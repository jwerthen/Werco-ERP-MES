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
import { getKioskDept, getKioskWorkCenterCode, getKioskWorkCenterId } from '../utils/kiosk';

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

export default function ShopFloor() {
  const { can } = usePermissions();
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
  const [clockOutModal, setClockOutModal] = useState(false);
  const [clockOutJob, setClockOutJob] = useState<ActiveJob | null>(null);
  const [clockOutData, setClockOutData] = useState({ quantity_produced: 0, quantity_scrapped: 0, notes: '' });
  const [clockOutShowMore, setClockOutShowMore] = useState(false);
  const [expandedRows, setExpandedRows] = useState<Set<number>>(new Set());
  const [workOrderDetails, setWorkOrderDetails] = useState<Record<number, WorkOrderDetails>>({});
  const [updatingPriorityWorkOrderId, setUpdatingPriorityWorkOrderId] = useState<number | null>(null);
  const [priorityReason, setPriorityReason] = useState('');
  const realtimeRefreshRef = useRef<NodeJS.Timeout | null>(null);
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

  const loadInitialData = useCallback(async () => {
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
    try {
      const response = await api.getWorkCenterQueue(workCenterId);
      setQueue(response.queue);
    } catch (err) {
      console.error('Failed to load queue:', err);
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
    loadInitialData();
    const interval = setInterval(checkActiveJob, 10000);
    return () => clearInterval(interval);
  }, [checkActiveJob, loadInitialData]);

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
    // eslint-disable-next-line react-hooks/exhaustive-deps
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
        try {
          const response = await api.getWorkOrder(workOrderId);
          setWorkOrderDetails(prev => ({
            ...prev,
            [workOrderId]: response
          }));
        } catch (err) {
          console.error('Failed to load work order details:', err);
        }
      }
    }
    setExpandedRows(newExpanded);
  };

  const handleClockIn = async (item: QueueItem) => {
    try {
      await api.clockIn({
        work_order_id: item.work_order_id,
        operation_id: item.operation_id,
        work_center_id: selectedWorkCenter!,
        entry_type: 'run'
      });
      await checkActiveJob();
      loadQueue(selectedWorkCenter!);
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to clock in');
    }
  };

  const handleClockOut = async () => {
    if (!clockOutJob) return;

    try {
      await api.clockOut(clockOutJob.time_entry_id, {
        quantity_produced: clockOutData.quantity_produced,
        quantity_scrapped: clockOutData.quantity_scrapped,
        notes: clockOutData.notes
      });
      await checkActiveJob();
      setClockOutJob(null);
      setClockOutModal(false);
      setClockOutData({ quantity_produced: 0, quantity_scrapped: 0, notes: '' });
      if (selectedWorkCenter) {
        loadQueue(selectedWorkCenter);
      }
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to clock out');
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
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to update priority');
    } finally {
      setUpdatingPriorityWorkOrderId(null);
    }
  };

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
    <div className="space-y-6">
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
          <button 
            onClick={handleRefresh}
            disabled={refreshing}
            className="btn-secondary"
          >
            <ArrowPathIcon className={`h-5 w-5 mr-2 ${refreshing ? 'animate-spin' : ''}`} />
            Refresh
          </button>
        </div>
      </div>

      {/* Active Job Banner */}
      {activeJobs.length > 0 && (
        <div className="space-y-4">
          {activeJobs.map((job) => (
            <div
              key={job.time_entry_id}
              className="relative overflow-hidden bg-gradient-to-r from-emerald-500 to-emerald-600 rounded-2xl p-6 text-white shadow-lg"
            >
              {/* Animated background pattern */}
              <div className="absolute inset-0 opacity-10">
                <div className="absolute inset-0" style={{
                  backgroundImage: 'repeating-linear-gradient(45deg, transparent, transparent 10px, rgba(255,255,255,0.1) 10px, rgba(255,255,255,0.1) 20px)'
                }} />
              </div>
              
              <div className="relative flex flex-col lg:flex-row lg:items-center justify-between gap-6">
                <div className="flex items-start gap-4">
                  <div className="p-3 bg-[#151b28]/20 rounded-xl">
                    <div className="h-4 w-4 rounded-full bg-[#151b28] animate-pulse"></div>
                  </div>
                  <div>
                    <p className="text-emerald-100 text-sm font-medium uppercase tracking-wide mb-1">
                      Currently Working On
                    </p>
                    <h2 className="text-2xl font-bold mb-1">
                      {job.work_order_number} - {job.operation_name}
                    </h2>
                    <p className="text-emerald-100">
                      {job.part_number} - {job.part_name}
                    </p>
                  </div>
                </div>
                
                <div className="flex flex-col sm:flex-row items-start sm:items-center gap-4">
                  <div className="text-center sm:text-right">
                    <p className="text-emerald-100 text-sm mb-1">
                      Started at {formatClockInTime(job.clock_in)}
                    </p>
                    <div className="flex items-center gap-2 text-3xl font-bold font-mono">
                      <ClockIcon className="h-7 w-7" />
                      {getElapsedTime(job.clock_in)}
                    </div>
                  </div>
                  <button
                    onClick={() => {
                      setClockOutJob(job);
                      const remaining = Math.max(0, (job.quantity_ordered || 0) - (job.quantity_complete || 0));
                      setClockOutData({ quantity_produced: remaining, quantity_scrapped: 0, notes: '' });
                      setClockOutModal(true);
                    }}
                    className="btn bg-[#151b28] text-emerald-400 hover:bg-emerald-500/100/10 shadow-lg"
                  >
                    <StopIcon className="h-5 w-5 mr-2" />
                    Clock Out
                  </button>
                </div>
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
              px-5 py-3 rounded-xl font-semibold transition-all duration-200 
              ${selectedWorkCenter === wc.id
                ? 'bg-werco-600 text-white shadow-md shadow-werco-600/30'
                : 'bg-[#151b28] text-surface-700 border border-surface-200 hover:border-werco-300 hover:bg-werco-50'
              }
            `}
          >
            {wc.name}
          </button>
        ))}
      </div>

      {/* Priority Focus Queue */}
      {priorityFocusQueue.length > 0 && (
        <div className="card" data-tour="sf-complete">
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-lg font-semibold text-surface-900">Priority Focus Queue</h2>
            <span className="text-sm text-surface-500">Top {priorityFocusQueue.length} to run next</span>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-5 gap-3">
            {priorityFocusQueue.map((item, idx) => {
              const overdue = Boolean(item.due_date && isDateBeforeTodayInCentral(item.due_date));
              return (
                <button
                  key={`focus-${item.operation_id}`}
                  type="button"
                  className={`text-left p-3 rounded-xl border transition-colors ${
                    overdue ? 'border-red-500/30 bg-red-500/10 hover:bg-red-500/100/20' : 'border-surface-200 bg-[#151b28] hover:bg-surface-50'
                  }`}
                  onClick={() => toggleRowExpansion(item.work_order_id)}
                >
                  <div className="flex items-center justify-between mb-2">
                    <span className="text-xs font-semibold text-surface-500">#{idx + 1}</span>
                    <span className={`px-2 py-0.5 rounded text-xs font-semibold ${getPriorityClasses(item.priority)}`}>
                      P{item.priority}
                    </span>
                  </div>
                  <div className="text-sm font-semibold text-werco-700">{item.work_order_number}</div>
                  <div className="text-xs text-surface-600 truncate">{item.operation_name}</div>
                  <div className={`text-xs mt-2 ${overdue ? 'text-red-600 font-medium' : 'text-surface-500'}`}>
                    {item.due_date ? `Due ${formatCentralDate(item.due_date, { year: undefined })}` : 'No due date'}
                  </div>
                </button>
              );
            })}
          </div>
        </div>
      )}

      {/* Job Queue */}
      <div className="card card-flush" data-tour="sf-operations">
        <div className="px-6 py-4 border-b border-surface-200 flex items-center justify-between">
          <div>
            <h2 className="text-lg font-semibold text-surface-900">
              Job Queue
            </h2>
            <p className="text-sm text-surface-500">
              {workCenters.find(wc => wc.id === selectedWorkCenter)?.name} â€¢ {sortedQueue.length} job{sortedQueue.length !== 1 ? 's' : ''}
            </p>
          </div>
          {canEditPriority && (
            <div className="w-80 hidden lg:block">
              <label className="text-xs font-medium text-surface-600 block mb-1">
                Optional Priority Reason
              </label>
              <input
                type="text"
                value={priorityReason}
                onChange={(e) => setPriorityReason(e.target.value)}
                className="input text-sm"
                maxLength={500}
                placeholder="Applied to your next priority change"
              />
            </div>
          )}
        </div>
        
        {sortedQueue.length === 0 ? (
          <div className="text-center py-16">
            <div className="p-4 rounded-full bg-surface-100 w-fit mx-auto mb-4">
              <QueueListIcon className="h-8 w-8 text-surface-400" />
            </div>
            <p className="text-surface-600 font-medium">No jobs in queue</p>
            <p className="text-sm text-surface-500 mt-1">Select a different work center or check back later</p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="table">
              <thead>
                <tr>
                  <th className="w-10"></th>
                  <th>Priority</th>
                  <th>Work Order</th>
                  <th>Part</th>
                  <th>Operation</th>
                  <th>Progress</th>
                  <th>Due Date</th>
                  <th>Status</th>
                  <th className="w-32">Action</th>
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
                        className={`${isOverdue ? 'bg-red-500/10/50' : ''} ${isExpanded ? 'bg-werco-50/50' : ''} cursor-pointer hover:bg-surface-50`}
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
                              disabled={updatingPriorityWorkOrderId === item.work_order_id}
                              className={`px-2 py-1 rounded text-xs font-bold border border-transparent ${getPriorityClasses(item.priority)}`}
                              title="Update priority"
                            >
                              {[1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map((p) => (
                                <option key={p} value={p}>
                                  P{p}
                                </option>
                              ))}
                            </select>
                          ) : (
                            <span className={`inline-flex items-center justify-center w-10 h-10 rounded-xl text-sm font-bold ${getPriorityClasses(item.priority)}`}>
                              P{item.priority}
                            </span>
                          )}
                        </td>
                        <td>
                          <span className="font-semibold text-werco-600">{item.work_order_number}</span>
                        </td>
                        <td>
                          <div>
                            <p className="font-medium text-surface-900">{item.part_number}</p>
                            <p className="text-sm text-surface-500 line-clamp-1">{item.part_name}</p>
                          </div>
                        </td>
                        <td>
                          <div>
                            <p className="font-medium text-surface-900">Op {item.operation_number}</p>
                            <p className="text-sm text-surface-500">{item.operation_name}</p>
                          </div>
                        </td>
                        <td>
                          <div className="w-32">
                            <div className="flex items-center justify-between text-sm mb-1">
                              <span className="font-medium text-surface-700 tabular-nums">
                                {item.quantity_complete}/{item.quantity_ordered}
                              </span>
                              <span className="text-surface-500">{Math.round(progress)}%</span>
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
                            {item.due_date ? formatCentralDate(item.due_date, { year: undefined }) : 'â€"'}
                          </span>
                          {isOverdue && (
                            <span className="block text-xs text-red-500 font-medium">OVERDUE</span>
                          )}
                        </td>
                        <td>
                          <span className={`
                            inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full text-xs font-semibold
                            ${item.status === 'in_progress' 
                              ? 'bg-emerald-500/20 text-emerald-400' 
                              : item.status === 'ready' 
                              ? 'bg-blue-500/20 text-blue-400' 
                            : 'bg-surface-100 text-surface-600'
                          }
                        `}>
                          <span className={`w-1.5 h-1.5 rounded-full ${
                            item.status === 'in_progress' ? 'bg-emerald-500/100' : 
                            item.status === 'ready' ? 'bg-blue-500/100' : 'bg-surface-400'
                          }`}></span>
                          {item.status.replace('_', ' ')}
                        </span>
                        </td>
                        <td onClick={(e) => e.stopPropagation()}>
                          {item.status === 'in_progress' ? (
                            <span className="inline-flex items-center gap-1.5 text-emerald-600 font-medium">
                              <CheckCircleIcon className="h-5 w-5" />
                              Active
                            </span>
                          ) : (
                            <button
                              onClick={() => handleClockIn(item)}
                              className="btn-success btn-sm w-full"
                            >
                              <PlayIcon className="h-4 w-4 mr-1.5" />
                              Start
                            </button>
                          )}
                        </td>
                      </tr>
                      
                      {/* Expanded Details Row */}
                      {isExpanded && (
                        <tr className="bg-surface-50">
                          <td colSpan={9} className="p-0">
                            <div className="p-6 border-t border-surface-200">
                              {details ? (
                                <div className="space-y-4">
                                  {/* Header with link */}
                                  <div className="flex items-center justify-between">
                                    <h3 className="text-lg font-semibold text-surface-900">
                                      Work Order Details
                                    </h3>
                                    <button
                                      onClick={(e) => {
                                        e.stopPropagation();
                                        navigate(`/work-orders/${details.id}`);
                                      }}
                                      className="btn-secondary btn-sm"
                                    >
                                      <ArrowTopRightOnSquareIcon className="h-4 w-4 mr-1.5" />
                                      Open Full View
                                    </button>
                                  </div>
                                  
                                  {/* Info Cards */}
                                  <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                                    <div className="bg-[#151b28] rounded-lg p-4 border border-surface-200">
                                      <p className="text-sm text-surface-500">Customer</p>
                                      <p className="font-semibold text-surface-900">{details.customer_name || 'â€"'}</p>
                                    </div>
                                    <div className="bg-[#151b28] rounded-lg p-4 border border-surface-200">
                                      <p className="text-sm text-surface-500">Customer PO</p>
                                      <p className="font-semibold text-surface-900">{details.customer_po || 'â€"'}</p>
                                    </div>
                                    <div className="bg-[#151b28] rounded-lg p-4 border border-surface-200">
                                      <p className="text-sm text-surface-500">Qty Complete / Ordered</p>
                                      <p className="font-semibold text-surface-900">
                                        {details.quantity_complete} / {details.quantity_ordered}
                                      </p>
                                    </div>
                                    <div className="bg-[#151b28] rounded-lg p-4 border border-surface-200">
                                      <p className="text-sm text-surface-500">Due Date</p>
                                      <p className="font-semibold text-surface-900">
                                        {details.due_date ? formatCentralDate(details.due_date) : 'â€"'}
                                      </p>
                                    </div>
                                  </div>
                                  
                                  {/* Notes */}
                                  {details.notes && (
                                    <div className="bg-amber-500/10 border border-amber-500/30 rounded-lg p-4">
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
                                    <h4 className="font-medium text-surface-700 mb-2">All Operations</h4>
                                    <div className="bg-[#151b28] rounded-lg border border-surface-200 overflow-hidden">
                                      <table className="w-full text-sm">
                                        <thead className="bg-surface-100">
                                          <tr>
                                            <th className="px-4 py-2 text-left font-medium text-surface-600">Op #</th>
                                            <th className="px-4 py-2 text-left font-medium text-surface-600">Operation</th>
                                            <th className="px-4 py-2 text-left font-medium text-surface-600">Work Center</th>
                                            <th className="px-4 py-2 text-left font-medium text-surface-600">Status</th>
                                            <th className="px-4 py-2 text-right font-medium text-surface-600">Est. Hrs</th>
                                            <th className="px-4 py-2 text-right font-medium text-surface-600">Actual Hrs</th>
                                          </tr>
                                        </thead>
                                        <tbody>
                                          {details.operations.map((op) => (
                                            <tr 
                                              key={op.id} 
                                              className={`border-t border-surface-100 ${op.id === item.operation_id ? 'bg-werco-50' : ''}`}
                                            >
                                              <td className="px-4 py-2 font-medium">{op.operation_number}</td>
                                              <td className="px-4 py-2">{op.name}</td>
                                              <td className="px-4 py-2">{op.work_center_name}</td>
                                              <td className="px-4 py-2">
                                                <span className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${
                                                  op.status === 'complete' ? 'bg-green-500/20 text-green-400' :
                                                  op.status === 'in_progress' ? 'bg-blue-500/20 text-blue-400' :
                                                  'bg-surface-100 text-surface-600'
                                                }`}>
                                                  {op.status.replace('_', ' ')}
                                                </span>
                                              </td>
                                              <td className="px-4 py-2 text-right tabular-nums">{op.estimated_hours?.toFixed(1) || 'â€"'}</td>
                                              <td className="px-4 py-2 text-right tabular-nums">{op.actual_hours?.toFixed(1) || 'â€"'}</td>
                                            </tr>
                                          ))}
                                        </tbody>
                                      </table>
                                    </div>
                                  </div>
                                </div>
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
      {clockOutModal && (
        <div className="modal-overlay" onClick={closeClockOutModal}>
          <div className="modal" onClick={e => e.stopPropagation()}>
            <div className="modal-header">
              <h3 className="text-lg font-semibold text-surface-900">Clock Out</h3>
              <button 
                onClick={closeClockOutModal}
                className="p-2 rounded-lg text-surface-400 hover:text-surface-600 hover:bg-surface-100"
              >
                <XMarkIcon className="h-5 w-5" />
              </button>
            </div>
            
            <div className="modal-body space-y-4">
              <div className="bg-surface-50 rounded-xl p-4 mb-4">
                <p className="text-sm text-surface-500 mb-1">Completing work on</p>
                <p className="font-semibold text-surface-900">
                  {clockOutJob?.work_order_number} — {clockOutJob?.operation_name}
                </p>
                {clockOutJob?.quantity_ordered ? (
                  <p className="text-xs text-surface-500 mt-1">
                    {clockOutJob.quantity_complete || 0} of {clockOutJob.quantity_ordered} previously completed
                  </p>
                ) : null}
              </div>

              <div>
                <label className="label">Quantity Produced</label>
                <input
                  type="number"
                  min="0"
                  value={clockOutData.quantity_produced}
                  onChange={(e) => setClockOutData({ ...clockOutData, quantity_produced: parseFloat(e.target.value) || 0 })}
                  className="input text-center text-2xl font-semibold h-14"
                  autoFocus
                />
              </div>

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
                  <div>
                    <label className="label">Quantity Scrapped</label>
                    <input
                      type="number"
                      min="0"
                      value={clockOutData.quantity_scrapped}
                      onChange={(e) => setClockOutData({ ...clockOutData, quantity_scrapped: parseFloat(e.target.value) || 0 })}
                      className="input text-center text-lg font-semibold"
                    />
                  </div>
                  <div>
                    <label className="label">Notes</label>
                    <textarea
                      value={clockOutData.notes}
                      onChange={(e) => setClockOutData({ ...clockOutData, notes: e.target.value })}
                      className="input"
                      rows={2}
                      placeholder="Any issues, observations, or notes..."
                    />
                  </div>
                </>
              )}
            </div>
            
            <div className="modal-footer">
              <button
                onClick={closeClockOutModal}
                className="btn-secondary"
              >
                Cancel
              </button>
              <button
                onClick={handleClockOut}
                className="btn-primary"
              >
                <CheckCircleIcon className="h-5 w-5 mr-2" />
                Complete Clock Out
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

