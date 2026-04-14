import React, { useEffect, useState, useCallback, useMemo, useRef } from 'react';
import api from '../services/api';
import { addDays, startOfWeek, isBefore, isAfter, isSameDay } from 'date-fns';
import { useWebSocket } from '../hooks/useWebSocket';
import { buildWsUrl, getAccessToken } from '../services/realtime';
import { usePermissions } from '../hooks/usePermissions';
import { calculateDispatchScore } from '../utils/dispatchScore';
import {
  formatCentralDate,
  formatInCentralTime,
  getCentralDateStamp,
  getCentralTodayDate,
  getCentralTodayISODate,
  getDateSortValue,
  toCentralCalendarDate,
} from '../utils/centralTime';
import {
  ChevronLeftIcon,
  ChevronRightIcon,
  CalendarIcon,
  BoltIcon,
  ExclamationTriangleIcon,
  MagnifyingGlassIcon,
  FunnelIcon,
  ChevronDownIcon,
  XMarkIcon,
} from '@heroicons/react/24/outline';

interface WorkCenter {
  id: number;
  code: string;
  name: string;
  capacity_hours_per_day: number;
}

interface ScheduledJob {
  id: number;
  work_order_id: number;
  work_order_number: string;
  current_operation_id: number;
  current_operation_name: string;
  current_operation_number?: string;
  current_operation_sequence: number;
  part_number: string;
  part_name: string;
  work_center_id: number;
  status: string;
  operation_status: string;
  scheduled_start?: string;
  scheduled_end?: string;
  due_date?: string;
  quantity: number;
  quantity_complete: number;
  priority: number;
  total_operations: number;
  operations_complete: number;
  remaining_hours: number;
  setup_hours: number;
  run_hours: number;
}

interface DragState {
  job: ScheduledJob | null;
  isDragging: boolean;
}

interface DropTarget {
  wcId: number;
  date: string; // ISO date string
}

interface CapacityForDate {
  work_center_id: number;
  work_center_code: string;
  date: string;
  capacity_hours: number;
  used_hours: number;
  available_hours: number;
  utilization_pct: number;
  overloaded: boolean;
  jobs_on_date: { work_order_id: number; work_order_number: string; operation_name: string; hours: number }[];
}

interface DispatchQueueJob extends ScheduledJob {
  dispatchScore: number;
}

interface CapacityHeatmapDay {
  date: string;
  scheduled_hours: number;
  capacity_hours: number;
  utilization_pct: number;
  job_count: number;
  overloaded: boolean;
}

interface CapacityHeatmapRow {
  work_center_id: number;
  work_center_code: string;
  work_center_name: string;
  capacity_hours_per_day: number;
  days: CapacityHeatmapDay[];
}

interface CapacityHeatmapResponse {
  start_date: string;
  end_date: string;
  overload_cells: number;
  overloaded_work_centers: number[];
  work_centers: CapacityHeatmapRow[];
}

const statusColors: Record<string, string> = {
  pending: 'bg-slate-500',
  ready: 'bg-blue-500/100',
  in_progress: 'bg-green-500/100',
  complete: 'bg-emerald-600',
  on_hold: 'bg-yellow-500/100',
};

const priorityColors: Record<number, string> = {
  1: 'border-l-red-500',
  2: 'border-l-red-400',
  3: 'border-l-orange-500',
  5: 'border-l-blue-500',
  7: 'border-l-gray-400',
  10: 'border-l-gray-300',
};


export default function Scheduling() {
  const { can } = usePermissions();
  const [workCenters, setWorkCenters] = useState<WorkCenter[]>([]);
  const [jobs, setJobs] = useState<ScheduledJob[]>([]);
  const [capacityHeatmap, setCapacityHeatmap] = useState<CapacityHeatmapResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [weekStart, setWeekStart] = useState(startOfWeek(getCentralTodayDate(), { weekStartsOn: 1 }));
  const [daysToShow] = useState(7);
  const [selectedJob, setSelectedJob] = useState<ScheduledJob | null>(null);
  const [showScheduleModal, setShowScheduleModal] = useState(false);
  const [scheduleForm, setScheduleForm] = useState({ scheduled_start: '', scheduled_end: '', work_center_id: 0 });
  const [updatingPriorityWorkOrderId, setUpdatingPriorityWorkOrderId] = useState<number | null>(null);
  const [schedulingEarliestWorkOrderId, setSchedulingEarliestWorkOrderId] = useState<number | null>(null);
  const [priorityReason, setPriorityReason] = useState('');
  const [showScheduledRows, setShowScheduledRows] = useState(true);
  const [selectedWorkOrderIds, setSelectedWorkOrderIds] = useState<Set<number>>(new Set());
  const [bulkPriority, setBulkPriority] = useState(5);
  const [bulkWorkCenterId, setBulkWorkCenterId] = useState<number | ''>('');
  const [bulkShiftDays, setBulkShiftDays] = useState(1);
  const [bulkActionRunning, setBulkActionRunning] = useState<string | null>(null);
  const realtimeRefreshRef = useRef<NodeJS.Timeout | null>(null);
  const realtimeUrl = useMemo(() => {
    const token = getAccessToken();
    return buildWsUrl('/ws/updates', token ? { token } : undefined);
  }, []);
  
  // Drag and drop state
  const [dragState, setDragState] = useState<DragState>({ job: null, isDragging: false });
  const [dropTarget, setDropTarget] = useState<DropTarget | null>(null);
  const canEditPriority = can('work_orders:edit');

  // Enhanced schedule modal state
  const [capacityPreview, setCapacityPreview] = useState<CapacityForDate | null>(null);
  const [loadingCapacity, setLoadingCapacity] = useState(false);
  const [forwardSchedule, setForwardSchedule] = useState(true);

  // Inline date editing
  const [inlineEditJobId, setInlineEditJobId] = useState<number | null>(null);
  const [inlineEditDate, setInlineEditDate] = useState('');

  // Auto-schedule state
  const [runningAutoSchedule, setRunningAutoSchedule] = useState(false);

  // Search and filter
  const [searchQuery, setSearchQuery] = useState('');
  const [filterWorkCenter, setFilterWorkCenter] = useState<number | ''>('');
  const [showBulkActions, setShowBulkActions] = useState(false);

  // Generate days for display: Monday-Saturday only (skip Sundays)
  const days = useMemo(
    () =>
      Array.from({ length: daysToShow }, (_, i) => addDays(weekStart, i)).filter(
        (day) => formatInCentralTime(day, { weekday: 'short' }) !== 'Sun'
      ),
    [daysToShow, weekStart]
  );
  const visibleStart = days[0] || weekStart;
  const visibleEnd = days[days.length - 1] || addDays(weekStart, daysToShow - 1);
  const todayStamp = getCentralTodayISODate();

  const openJobs = useMemo(() => jobs.filter((job) => job.status !== 'complete'), [jobs]);

  const dispatchQueue = useMemo<DispatchQueueJob[]>(() => {
    return openJobs
      .map((job) => ({
        ...job,
        dispatchScore: calculateDispatchScore({
          priority: job.priority,
          dueDate: job.due_date || null,
          remainingHours: job.remaining_hours,
          scheduledStart: job.scheduled_start || null,
          status: job.status,
        }),
      }))
      .sort((a, b) => {
        if (a.dispatchScore !== b.dispatchScore) return b.dispatchScore - a.dispatchScore;
        if (a.priority !== b.priority) return a.priority - b.priority;
        const aDue = getDateSortValue(a.due_date);
        const bDue = getDateSortValue(b.due_date);
        if (aDue !== bDue) return aDue - bDue;
        return a.work_order_number.localeCompare(b.work_order_number);
      });
  }, [openJobs]);

  const queueRows = useMemo(
    () => (showScheduledRows ? dispatchQueue : dispatchQueue.filter((job) => !job.scheduled_start)),
    [dispatchQueue, showScheduledRows]
  );

  const filteredQueueRows = useMemo(() => {
    let rows = queueRows;
    if (searchQuery) {
      const q = searchQuery.toLowerCase();
      rows = rows.filter(
        (job) =>
          job.work_order_number.toLowerCase().includes(q) ||
          job.part_number.toLowerCase().includes(q) ||
          job.part_name.toLowerCase().includes(q)
      );
    }
    if (filterWorkCenter) {
      rows = rows.filter((job) => job.work_center_id === filterWorkCenter);
    }
    return rows;
  }, [queueRows, searchQuery, filterWorkCenter]);

  const selectedQueueJobs = useMemo(
    () => dispatchQueue.filter((job) => selectedWorkOrderIds.has(job.work_order_id)),
    [dispatchQueue, selectedWorkOrderIds]
  );

  useEffect(() => {
    setSelectedWorkOrderIds((previous) => {
      const activeIds = new Set(dispatchQueue.map((job) => job.work_order_id));
      return new Set(Array.from(previous).filter((id) => activeIds.has(id)));
    });
  }, [dispatchQueue]);

  const heatmapByWorkCenter = useMemo(() => {
    const map = new Map<number, CapacityHeatmapRow>();
    (capacityHeatmap?.work_centers || []).forEach((row) => map.set(row.work_center_id, row));
    return map;
  }, [capacityHeatmap]);

  const stats = useMemo(() => {
    const unscheduledCount = openJobs.filter((j) => !j.scheduled_start).length;
    const scheduledCount = openJobs.filter((j) => j.scheduled_start).length;
    const overdueCount = openJobs.filter((j) => j.due_date && j.due_date < todayStamp).length;
    const overloadedWcCount = capacityHeatmap?.overloaded_work_centers?.length || 0;
    const totalHoursRemaining = openJobs.reduce((sum, j) => sum + j.remaining_hours, 0);
    return { unscheduledCount, scheduledCount, overdueCount, overloadedWcCount, totalHoursRemaining };
  }, [openJobs, todayStamp, capacityHeatmap]);

  const loadData = useCallback(async () => {
    try {
      const startDate = getCentralDateStamp(visibleStart);
      const endDate = getCentralDateStamp(visibleEnd);
      const [wcRes, jobsRes, heatmapRes] = await Promise.all([
        api.getWorkCenters(),
        api.getSchedulableWorkOrders({
          start_date: startDate,
          end_date: endDate
        }),
        api.getCapacityHeatmap(startDate, endDate),
      ]);
      setWorkCenters(wcRes);
      setJobs(jobsRes);
      setCapacityHeatmap(heatmapRes);
    } catch (err) {
      console.error('Failed to load scheduling data:', err);
    } finally {
      setLoading(false);
    }
  }, [visibleEnd, visibleStart]);

  const scheduleRealtimeRefresh = useCallback(() => {
    if (realtimeRefreshRef.current) return;
    realtimeRefreshRef.current = setTimeout(() => {
      realtimeRefreshRef.current = null;
      loadData();
    }, 800);
  }, [loadData]);

  useWebSocket({
    url: realtimeUrl,
    enabled: true,
    onMessage: (message) => {
      if (message.type === 'connected' || message.type === 'ping') return;
      if (['work_order_update', 'shop_floor_update', 'dashboard_update'].includes(message.type)) {
        scheduleRealtimeRefresh();
      }
    }
  });

  useEffect(() => {
    loadData();
  }, [loadData]);

  useEffect(() => {
    return () => {
      if (realtimeRefreshRef.current) {
        clearTimeout(realtimeRefreshRef.current);
        realtimeRefreshRef.current = null;
      }
    };
  }, []);

  // Get jobs that START on a specific day OR are continuing from a previous week
  const getJobsStartingOnDay = (wcId: number, day: Date, dayIdx: number): ScheduledJob[] => {
    return jobs.filter(job => {
      if (job.work_center_id !== wcId) return false;
      if (!job.scheduled_start) return false;
      
      const jobStart = toCentralCalendarDate(job.scheduled_start);
      const jobEnd = toCentralCalendarDate(job.scheduled_end || job.scheduled_start);
      if (!jobStart || !jobEnd) return false;
      
      // Job starts on this exact day
      if (isSameDay(jobStart, day)) return true;
      
      // Job started before this week but continues into it - show on first visible day
      if (dayIdx === 0 && isBefore(jobStart, day) && (isAfter(jobEnd, day) || isSameDay(jobEnd, day))) {
        return true;
      }
      
      return false;
    });
  };

  // Calculate span of a job in days within the visible range (counting only visible days)
  const getJobSpan = (job: ScheduledJob, day: Date, dayIdx: number): number => {
    if (!job.scheduled_start) return 1;
    
    const jobStart = toCentralCalendarDate(job.scheduled_start);
    const jobEnd = toCentralCalendarDate(job.scheduled_end || job.scheduled_start);
    if (!jobStart || !jobEnd) return 1;
    
    // If job started before current view, calculate from current day
    const effectiveStart = isBefore(jobStart, day) ? day : jobStart;
    
    // Count how many visible days this job spans
    let spanCount = 0;
    for (let i = dayIdx; i < days.length; i++) {
      const checkDay = days[i];
      if (isBefore(checkDay, effectiveStart)) continue;
      if (isAfter(checkDay, jobEnd)) break;
      spanCount++;
    }
    
    return Math.max(1, spanCount);
  };

  // Check if a job spans through a specific day (but doesn't start on it and isn't continuing from prev week)
  const isJobSpanningDay = (wcId: number, day: Date, dayIdx: number): ScheduledJob | null => {
    for (const job of jobs) {
      if (job.work_center_id !== wcId) continue;
      if (!job.scheduled_start || !job.scheduled_end) continue;
      
      const jobStart = toCentralCalendarDate(job.scheduled_start);
      const jobEnd = toCentralCalendarDate(job.scheduled_end);
      if (!jobStart || !jobEnd) continue;
      
      // Skip if this is the first day (those are handled by getJobsStartingOnDay)
      if (dayIdx === 0) continue;
      
      // Check if day is between start and end (exclusive of start day)
      if (isAfter(day, jobStart) && (isBefore(day, jobEnd) || isSameDay(day, jobEnd))) {
        return job;
      }
    }
    return null;
  };

  const getUnscheduledJobs = (wcId: number) => {
    return openJobs.filter((job) => job.work_center_id === wcId && !job.scheduled_start);
  };

  const openScheduleModal = (job: ScheduledJob) => {
    setSelectedJob(job);
    setScheduleForm({
      scheduled_start: job.scheduled_start ? getCentralDateStamp(job.scheduled_start) : getCentralTodayISODate(),
      scheduled_end: job.scheduled_end ? job.scheduled_end.split('T')[0] : '',
      work_center_id: job.work_center_id
    });
    setShowScheduleModal(true);
  };

  // Drag and drop handlers
  const handleDragStart = (e: React.DragEvent, job: ScheduledJob) => {
    setDragState({ job, isDragging: true });
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', job.work_order_id.toString());
  };

  const handleDragEnd = () => {
    setDragState({ job: null, isDragging: false });
    setDropTarget(null);
  };

  const handleDragOverCell = (e: React.DragEvent, wcId: number, dateStr: string) => {
    e.preventDefault();
    e.stopPropagation();
    e.dataTransfer.dropEffect = 'move';
    if (!dropTarget || dropTarget.wcId !== wcId || dropTarget.date !== dateStr) {
      setDropTarget({ wcId, date: dateStr });
    }
  };

  const handleDragLeaveCell = (e: React.DragEvent) => {
    // Only clear if leaving the cell entirely (not entering a child)
    const related = e.relatedTarget as HTMLElement | null;
    if (!related || !e.currentTarget.contains(related)) {
      setDropTarget(null);
    }
  };

  const handleDropOnCell = async (e: React.DragEvent, targetWcId: number, targetDate: string) => {
    e.preventDefault();
    e.stopPropagation();
    setDropTarget(null);

    const job = dragState.job;
    if (!job) {
      setDragState({ job: null, isDragging: false });
      return;
    }

    try {
      // If work center changed, move it first
      if (job.work_center_id !== targetWcId) {
        await api.updateOperationWorkCenter(job.current_operation_id, targetWcId);
      }
      // Schedule to the target date
      await api.scheduleWorkOrder(job.work_order_id, {
        scheduled_start: targetDate,
        work_center_id: targetWcId,
      });
      await loadData();
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to schedule work order');
    }

    setDragState({ job: null, isDragging: false });
  };

  // Drop on work center row header (no specific date - just move work center)
  const handleDropOnRow = async (e: React.DragEvent, targetWcId: number) => {
    e.preventDefault();
    setDropTarget(null);

    const job = dragState.job;
    if (!job || job.work_center_id === targetWcId) {
      setDragState({ job: null, isDragging: false });
      return;
    }

    try {
      await api.updateOperationWorkCenter(job.current_operation_id, targetWcId);
      await loadData();
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to move work order');
    }

    setDragState({ job: null, isDragging: false });
  };

  const handleSchedule = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedJob) return;

    try {
      await api.scheduleWorkOrder(selectedJob.work_order_id, {
        scheduled_start: scheduleForm.scheduled_start,
        work_center_id: scheduleForm.work_center_id || selectedJob.work_center_id,
        forward_schedule: forwardSchedule,
      } as any);
      setShowScheduleModal(false);
      setCapacityPreview(null);
      await loadData();
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to schedule');
    }
  };

  const handleUnschedule = async (job: ScheduledJob) => {
    try {
      await api.unscheduleWorkOrder(job.work_order_id);
      setShowScheduleModal(false);
      setCapacityPreview(null);
      await loadData();
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to unschedule');
    }
  };

  const loadCapacityPreview = useCallback(async (workCenterId: number, dateStr: string) => {
    if (!workCenterId || !dateStr) {
      setCapacityPreview(null);
      return;
    }
    setLoadingCapacity(true);
    try {
      const data = await api.getCapacityForDate(workCenterId, dateStr);
      setCapacityPreview(data);
    } catch {
      setCapacityPreview(null);
    } finally {
      setLoadingCapacity(false);
    }
  }, []);

  // Load capacity when schedule form changes
  useEffect(() => {
    if (showScheduleModal && scheduleForm.scheduled_start && scheduleForm.work_center_id) {
      loadCapacityPreview(scheduleForm.work_center_id, scheduleForm.scheduled_start);
    } else {
      setCapacityPreview(null);
    }
  }, [showScheduleModal, scheduleForm.scheduled_start, scheduleForm.work_center_id, loadCapacityPreview]);

  const handleInlineDateSave = async (job: ScheduledJob) => {
    if (!inlineEditDate) {
      setInlineEditJobId(null);
      return;
    }
    try {
      await api.scheduleWorkOrder(job.work_order_id, {
        scheduled_start: inlineEditDate,
        work_center_id: job.work_center_id,
      });
      setInlineEditJobId(null);
      setInlineEditDate('');
      await loadData();
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to reschedule');
    }
  };

  const handleAutoScheduleAll = async () => {
    const unscheduledIds = dispatchQueue
      .filter((job) => !job.scheduled_start)
      .map((job) => job.work_order_id);

    if (unscheduledIds.length === 0) {
      alert('No unscheduled work orders to schedule.');
      return;
    }

    setRunningAutoSchedule(true);
    try {
      const result = await api.bulkScheduleEarliest(unscheduledIds, { forward_schedule: true });
      await loadData();
      alert(`Auto-scheduled ${result.scheduled_count} work orders. ${result.error_count} errors.`);
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Auto-schedule failed');
    } finally {
      setRunningAutoSchedule(false);
    }
  };

  const handleScheduleEarliest = async (job: ScheduledJob) => {
    setSchedulingEarliestWorkOrderId(job.work_order_id);
    try {
      await api.scheduleWorkOrderEarliest(job.work_order_id, {
        work_center_id: job.work_center_id,
      });
      await loadData();
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to schedule earliest');
    } finally {
      setSchedulingEarliestWorkOrderId(null);
    }
  };

  const handlePriorityChange = async (workOrderId: number, priorityRaw: string) => {
    const priority = parseInt(priorityRaw, 10);
    if (Number.isNaN(priority)) return;

    const existing = jobs.find((job) => job.work_order_id === workOrderId);
    if (!existing || existing.priority === priority) return;

    setUpdatingPriorityWorkOrderId(workOrderId);
    try {
      const reason = priorityReason.trim() || undefined;
      await api.updateWorkOrderPriority(workOrderId, priority, reason);
      setJobs((prev) =>
        prev.map((job) =>
          job.work_order_id === workOrderId ? { ...job, priority } : job
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

  const runBulkAction = async (
    actionKey: string,
    actionRunner: (job: DispatchQueueJob) => Promise<'success' | 'skipped'>
  ) => {
    if (selectedQueueJobs.length === 0) {
      alert('Select at least one work order first.');
      return;
    }

    setBulkActionRunning(actionKey);
    let success = 0;
    let skipped = 0;
    let failed = 0;

    for (const job of selectedQueueJobs) {
      try {
        const result = await actionRunner(job);
        if (result === 'skipped') {
          skipped += 1;
        } else {
          success += 1;
        }
      } catch (err) {
        failed += 1;
        console.error(`Bulk action failed for ${job.work_order_number}`, err);
      }
    }

    setBulkActionRunning(null);
    await loadData();
    alert(`Bulk action complete. Updated: ${success}, skipped: ${skipped}, failed: ${failed}.`);
  };

  const handleBulkSetPriority = async () => {
    const reason = priorityReason.trim() || undefined;
    await runBulkAction('priority', async (job) => {
      await api.updateWorkOrderPriority(job.work_order_id, bulkPriority, reason);
      return 'success';
    });
    if (reason) {
      setPriorityReason('');
    }
  };

  const handleBulkMoveWorkCenter = async () => {
    if (!bulkWorkCenterId) {
      alert('Select a target work center first.');
      return;
    }
    await runBulkAction('work-center', async (job) => {
      await api.updateOperationWorkCenter(job.current_operation_id, bulkWorkCenterId);
      return 'success';
    });
  };

  const handleBulkShiftDates = async () => {
    if (bulkShiftDays === 0) {
      alert('Shift days cannot be zero.');
      return;
    }
    await runBulkAction('shift', async (job) => {
      if (!job.scheduled_start) {
        return 'skipped';
      }
      const scheduledStart = toCentralCalendarDate(job.scheduled_start);
      if (!scheduledStart) {
        return 'skipped';
      }
      const shiftedDate = addDays(scheduledStart, bulkShiftDays);
      await api.scheduleWorkOrder(job.work_order_id, {
        scheduled_start: getCentralDateStamp(shiftedDate),
        work_center_id: job.work_center_id,
      });
      return 'success';
    });
  };

  const handleBulkScheduleEarliest = async () => {
    const unscheduledSelected = selectedQueueJobs.filter((job) => !job.scheduled_start);
    if (unscheduledSelected.length === 0) {
      alert('No unscheduled work orders selected.');
      return;
    }
    setBulkActionRunning('earliest');
    try {
      const ids = unscheduledSelected.map((job) => job.work_order_id);
      const result = await api.bulkScheduleEarliest(ids, { forward_schedule: true });
      await loadData();
      alert(`Scheduled ${result.scheduled_count} work orders. ${result.error_count} errors.`);
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Bulk schedule failed');
    } finally {
      setBulkActionRunning(null);
    }
  };

  const toggleRowSelection = (workOrderId: number) => {
    setSelectedWorkOrderIds((previous) => {
      const next = new Set(previous);
      if (next.has(workOrderId)) {
        next.delete(workOrderId);
      } else {
        next.add(workOrderId);
      }
      return next;
    });
  };

  const selectAllVisibleRows = () => {
    setSelectedWorkOrderIds(new Set(queueRows.map((job) => job.work_order_id)));
  };

  const clearSelections = () => {
    setSelectedWorkOrderIds(new Set());
  };

  const priorityBadgeClasses = (priority: number) => {
    if (priority <= 2) return 'bg-red-500/20 text-red-300';
    if (priority <= 5) return 'bg-yellow-500/20 text-yellow-300';
    return 'bg-slate-800 text-slate-100';
  };

  const navigateWeek = (direction: number) => {
    setWeekStart(addDays(weekStart, direction * 7));
  };

  const goToToday = () => {
    setWeekStart(startOfWeek(getCentralTodayDate(), { weekStartsOn: 1 }));
  };

  if (loading) {
    return (
      <div className="space-y-4">
        <div className="page-header">
          <div className="skeleton-title w-48" />
          <div className="flex gap-2"><div className="skeleton w-24 h-9" /><div className="skeleton w-32 h-9" /></div>
        </div>
        <div className="grid grid-cols-5 gap-3">
          {Array.from({ length: 5 }).map((_, i) => (
            <div key={i} className="stat-card"><div className="skeleton-text w-20" /><div className="skeleton-title w-12 mt-2" /></div>
          ))}
        </div>
        <div className="card p-0"><div className="skeleton w-full h-96" /></div>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {/* Page Header */}
      <div className="flex justify-between items-center">
        <div>
          <h1 className="text-2xl font-bold text-white">Production Schedule</h1>
          <p className="text-sm text-slate-400 mt-0.5">
            {formatCentralDate(visibleStart, { month: 'short', day: 'numeric', year: undefined })} &ndash; {formatCentralDate(visibleEnd)}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={handleAutoScheduleAll}
            disabled={runningAutoSchedule}
            className="btn-primary btn-sm flex items-center disabled:opacity-50"
            title="Auto-schedule all unscheduled work orders to their earliest available capacity"
          >
            <BoltIcon className="h-4 w-4 mr-1" />
            {runningAutoSchedule ? 'Scheduling...' : 'Auto-Schedule All'}
          </button>
          <div className="flex items-center gap-0.5 border-l border-slate-700 pl-2 ml-1">
            <button onClick={() => navigateWeek(-1)} className="p-1.5 hover:bg-slate-800 rounded-md transition-colors">
              <ChevronLeftIcon className="h-4 w-4 text-slate-400" />
            </button>
            <button onClick={goToToday} className="btn-secondary btn-sm flex items-center">
              <CalendarIcon className="h-3.5 w-3.5 mr-1" />
              Today
            </button>
            <button onClick={() => navigateWeek(1)} className="p-1.5 hover:bg-slate-800 rounded-md transition-colors">
              <ChevronRightIcon className="h-4 w-4 text-slate-400" />
            </button>
          </div>
        </div>
      </div>

      {/* Stats Strip */}
      <div className="grid grid-cols-2 sm:grid-cols-5 gap-2">
        <div className="stat-card !py-2 !px-3">
          <div className="stat-label">Unscheduled</div>
          <div className={`stat-value text-lg ${stats.unscheduledCount > 0 ? 'text-orange-600' : 'text-white'}`}>{stats.unscheduledCount}</div>
        </div>
        <div className="stat-card !py-2 !px-3">
          <div className="stat-label">Scheduled</div>
          <div className="stat-value text-lg text-green-400">{stats.scheduledCount}</div>
        </div>
        <div className="stat-card !py-2 !px-3">
          <div className="stat-label">Overdue</div>
          <div className={`stat-value text-lg ${stats.overdueCount > 0 ? 'text-red-600' : 'text-white'}`}>{stats.overdueCount}</div>
        </div>
        <div className="stat-card !py-2 !px-3">
          <div className="stat-label">Overloaded WCs</div>
          <div className={`stat-value text-lg ${stats.overloadedWcCount > 0 ? 'text-red-600' : 'text-white'}`}>{stats.overloadedWcCount}</div>
        </div>
        <div className="stat-card !py-2 !px-3">
          <div className="stat-label">Hours Remaining</div>
          <div className="stat-value text-lg">{stats.totalHoursRemaining.toFixed(0)}h</div>
        </div>
      </div>

      {/* Gantt Chart with Continuous Bars and Drag-Drop */}
      <div className="card overflow-hidden">
        <div className="overflow-x-auto">
          <table className="min-w-full border-collapse" style={{ tableLayout: 'fixed' }}>
            <thead>
              <tr className="bg-slate-800/50">
                <th className="sticky left-0 bg-slate-800/50 z-10 px-4 py-2 text-left text-xs font-medium text-slate-400 uppercase w-48 border-r">
                  Work Center
                </th>
                {days.map((day, idx) => {
                  const dateKey = getCentralDateStamp(day);
                  const isToday = dateKey === todayStamp;
                  const isSat = formatInCentralTime(day, { weekday: 'short' }) === 'Sat';
                  // Aggregate utilization across all work centers for this day
                  let totalUsed = 0;
                  let totalCapacity = 0;
                  capacityHeatmap?.work_centers?.forEach((wc) => {
                    const dayData = wc.days.find((d) => d.date === dateKey);
                    totalUsed += dayData?.scheduled_hours || 0;
                    totalCapacity += wc.capacity_hours_per_day || 8;
                  });
                  const dayUtil = totalCapacity > 0 ? (totalUsed / totalCapacity) * 100 : 0;
                  return (
                    <th
                      key={idx}
                      className={`px-2 py-1.5 text-center text-xs font-medium w-28 border-r ${
                        isToday ? 'bg-blue-500/10 text-blue-400' : isSat ? 'bg-slate-800 text-slate-400' : 'text-slate-400'
                      }`}
                    >
                      <div className="leading-tight">{formatInCentralTime(day, { weekday: 'short' })}</div>
                      <div className="text-sm font-bold leading-tight">{formatInCentralTime(day, { day: 'numeric' })}</div>
                      <div className="mt-1 mx-auto w-full">
                        <div className="w-full bg-slate-700 rounded-full h-1.5">
                          <div
                            className={`h-1.5 rounded-full transition-all ${
                              dayUtil > 100 ? 'bg-red-500/100' : dayUtil >= 90 ? 'bg-amber-500/100' : dayUtil >= 70 ? 'bg-yellow-400' : 'bg-emerald-500/100'
                            }`}
                            style={{ width: `${Math.min(100, dayUtil)}%` }}
                          />
                        </div>
                        <div className={`text-[10px] mt-0.5 ${dayUtil > 100 ? 'text-red-600 font-semibold' : ''}`}>
                          {Math.round(dayUtil)}%
                        </div>
                      </div>
                    </th>
                  );
                })}
              </tr>
            </thead>
            <tbody>
              {workCenters.map((wc) => {
                const unscheduled = getUnscheduledJobs(wc.id);
                const isRowDropTarget = dropTarget?.wcId === wc.id;
                const heatmapRow = heatmapByWorkCenter.get(wc.id);
                const hasOverload = Boolean(heatmapRow?.days.some((day) => day.overloaded));

                return (
                  <tr
                    key={wc.id}
                    className={`border-b transition-colors hover:bg-slate-800/50`}
                  >
                    <td
                      className={`sticky left-0 z-10 px-4 py-3 border-r ${isRowDropTarget ? 'bg-blue-500/20' : 'bg-[#151b28]'}`}
                      onDragOver={(e) => { e.preventDefault(); e.dataTransfer.dropEffect = 'move'; }}
                      onDrop={(e) => handleDropOnRow(e, wc.id)}
                    >
                      <div className="font-medium text-sm">{wc.code}</div>
                      <div className="text-xs text-slate-400">{wc.name}</div>
                      {unscheduled.length > 0 && (
                        <div className="mt-1 text-xs text-orange-600">
                          {unscheduled.length} unscheduled
                        </div>
                      )}
                      {hasOverload && (
                        <div className="mt-1 text-xs text-red-600 flex items-center gap-1">
                          <ExclamationTriangleIcon className="h-3.5 w-3.5" />
                          Overloaded
                        </div>
                      )}
                    </td>
                    {days.map((day, dayIdx) => {
                      const jobsStartingToday = getJobsStartingOnDay(wc.id, day, dayIdx);
                      const spanningJob = isJobSpanningDay(wc.id, day, dayIdx);
                      const isWeekend = formatInCentralTime(day, { weekday: 'short' }) === 'Sat';
                      const isToday = getCentralDateStamp(day) === todayStamp;
                      const cellDateStr = getCentralDateStamp(day);
                      const isCellDropTarget = dropTarget?.wcId === wc.id && dropTarget?.date === cellDateStr;

                      // If a job is spanning through this day (but didn't start here), render empty cell
                      // The bar from the start day will cover this cell via colspan
                      if (spanningJob && jobsStartingToday.length === 0) {
                        return null; // Cell is covered by colspan from previous day
                      }

                      return (
                        <td
                          key={dayIdx}
                          colSpan={jobsStartingToday.length > 0 ? 1 : 1}
                          className={`px-1 py-1 border-r align-top h-16 relative transition-colors ${
                            isCellDropTarget
                              ? 'bg-blue-200 ring-2 ring-inset ring-blue-400'
                              : isToday ? 'bg-blue-500/10' : isWeekend ? 'bg-slate-800/50' : ''
                          }`}
                          onDragOver={(e) => handleDragOverCell(e, wc.id, cellDateStr)}
                          onDragLeave={handleDragLeaveCell}
                          onDrop={(e) => handleDropOnCell(e, wc.id, cellDateStr)}
                        >
                          {isCellDropTarget && dragState.job && (
                            <div className="absolute inset-0 flex items-center justify-center pointer-events-none z-10">
                              <span className="bg-blue-600 text-white text-[10px] font-medium px-1.5 py-0.5 rounded shadow">
                                Drop here
                              </span>
                            </div>
                          )}
                          <div className="space-y-1">
                            {jobsStartingToday.map((job) => {
                              const span = getJobSpan(job, day, dayIdx);
                              // Calculate width: span * cell width (96px) - padding
                              const widthPx = span * 96 - 8;

                              return (
                                <div
                                  key={job.work_order_id}
                                  draggable
                                  onDragStart={(e) => handleDragStart(e, job)}
                                  onDragEnd={handleDragEnd}
                                  onClick={() => openScheduleModal(job)}
                                  className={`text-xs p-1.5 rounded cursor-move hover:opacity-90 border-l-4 shadow-sm ${
                                    priorityColors[job.priority] || 'border-l-gray-400'
                                  } ${statusColors[job.operation_status] || statusColors[job.status]} text-white ${
                                    dragState.job?.work_order_id === job.work_order_id ? 'opacity-50' : ''
                                  }`}
                                  style={{
                                    width: span > 1 ? `${widthPx}px` : 'auto',
                                    position: span > 1 ? 'absolute' : 'relative',
                                    zIndex: span > 1 ? 5 : 1,
                                    minWidth: '88px'
                                  }}
                                  title={`${job.work_order_number} - ${job.part_number}\nOp ${job.operations_complete + 1}/${job.total_operations}: ${job.current_operation_name}\n${span > 1 ? `${span} days` : '1 day'}\nDrag to reschedule or move to another work center`}
                                >
                                  <div className="font-medium truncate">{job.work_order_number}</div>
                                  <div className="truncate opacity-90">Op {job.operations_complete + 1}/{job.total_operations}</div>
                                  {span > 1 && (
                                    <div className="text-[10px] opacity-75 mt-0.5">
                                      {span} days
                                    </div>
                                  )}
                                </div>
                              );
                            })}
                          </div>
                        </td>
                      );
                    })}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
      
      {/* Drag hint */}
      {dragState.isDragging && (
        <div className="fixed bottom-4 left-1/2 transform -translate-x-1/2 bg-gray-800 text-white px-4 py-2 rounded-lg shadow-lg text-sm z-50">
          Dragging: {dragState.job?.work_order_number} - Drop on a date cell to schedule, or row header to move
        </div>
      )}

      {/* Dispatch Queue */}
      <div className="card">
        <div className="flex flex-wrap items-center justify-between gap-3 mb-3">
          <div className="flex items-center gap-3">
            <h2 className="text-lg font-semibold">Dispatch Queue</h2>
            <span className="badge badge-neutral text-xs">{filteredQueueRows.length} jobs</span>
            {stats.unscheduledCount > 0 && (
              <span className="badge badge-warning text-xs">{stats.unscheduledCount} unscheduled</span>
            )}
          </div>
          <div className="flex items-center gap-2">
            <div className="relative">
              <MagnifyingGlassIcon className="h-4 w-4 absolute left-2.5 top-1/2 -translate-y-1/2 text-slate-500" />
              <input
                type="text"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                className="input text-sm pl-8 w-48"
                placeholder="Search WO#, part..."
              />
              {searchQuery && (
                <button onClick={() => setSearchQuery('')} className="absolute right-2 top-1/2 -translate-y-1/2 text-slate-500 hover:text-slate-400">
                  <XMarkIcon className="h-3.5 w-3.5" />
                </button>
              )}
            </div>
            <select
              value={filterWorkCenter}
              onChange={(e) => setFilterWorkCenter(e.target.value ? parseInt(e.target.value, 10) : '')}
              className="input text-sm w-40"
            >
              <option value="">All Work Centers</option>
              {workCenters.map((wc) => (
                <option key={`fwc-${wc.id}`} value={wc.id}>{wc.code}</option>
              ))}
            </select>
            <label className="text-xs text-slate-400 flex items-center gap-1.5 whitespace-nowrap">
              <input
                type="checkbox"
                checked={showScheduledRows}
                onChange={(e) => setShowScheduledRows(e.target.checked)}
              />
              Scheduled
            </label>
            <button
              type="button"
              onClick={() => setShowBulkActions(!showBulkActions)}
              className={`btn-secondary btn-sm flex items-center gap-1 ${showBulkActions ? 'bg-slate-700' : ''}`}
            >
              <FunnelIcon className="h-3.5 w-3.5" />
              Bulk
              <ChevronDownIcon className={`h-3 w-3 transition-transform ${showBulkActions ? 'rotate-180' : ''}`} />
            </button>
          </div>
        </div>
        {showBulkActions && (
        <div className="border rounded-lg p-3 mb-3 bg-slate-800/50 animate-fade-in">
          <div className="flex flex-wrap items-center gap-2 mb-2">
            <span className="text-xs font-medium text-slate-300">Selected: {selectedQueueJobs.length}</span>
            <button type="button" onClick={selectAllVisibleRows} className="text-xs text-werco-primary hover:underline">
              Select visible
            </button>
            <button type="button" onClick={clearSelections} className="text-xs text-slate-400 hover:underline">
              Clear
            </button>
            {canEditPriority && (
              <div className="ml-auto w-full sm:w-64">
                <input
                  type="text"
                  value={priorityReason}
                  onChange={(e) => setPriorityReason(e.target.value)}
                  className="input text-xs py-1"
                  maxLength={500}
                  placeholder="Priority reason (optional)"
                />
              </div>
            )}
          </div>
          <div className="grid grid-cols-1 xl:grid-cols-4 gap-2">
            {canEditPriority && (
              <div className="flex gap-2">
                <select
                  value={bulkPriority}
                  onChange={(e) => setBulkPriority(parseInt(e.target.value, 10))}
                  className="input text-sm"
                >
                  {[1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map((p) => (
                    <option key={`bulk-p-${p}`} value={p}>
                      Set P{p}
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  className="btn-secondary text-sm"
                  disabled={bulkActionRunning !== null}
                  onClick={handleBulkSetPriority}
                >
                  Apply
                </button>
              </div>
            )}
            <div className="flex gap-2">
              <select
                value={bulkWorkCenterId}
                onChange={(e) => setBulkWorkCenterId(e.target.value ? parseInt(e.target.value, 10) : '')}
                className="input text-sm"
              >
                <option value="">Move to work center</option>
                {workCenters.map((wc) => (
                  <option key={`bulk-wc-${wc.id}`} value={wc.id}>
                    {wc.code}
                  </option>
                ))}
              </select>
              <button
                type="button"
                className="btn-secondary text-sm"
                disabled={bulkActionRunning !== null}
                onClick={handleBulkMoveWorkCenter}
              >
                Move
              </button>
            </div>
            <div className="flex gap-2">
              <input
                type="number"
                value={bulkShiftDays}
                onChange={(e) => setBulkShiftDays(parseInt(e.target.value, 10) || 0)}
                className="input text-sm w-24"
                min={-30}
                max={30}
              />
              <button
                type="button"
                className="btn-secondary text-sm"
                disabled={bulkActionRunning !== null}
                onClick={handleBulkShiftDates}
              >
                Shift Dates
              </button>
            </div>
            <button
              type="button"
              className="btn-primary text-sm flex items-center justify-center"
              disabled={bulkActionRunning !== null}
              onClick={handleBulkScheduleEarliest}
            >
              <BoltIcon className="h-4 w-4 mr-1" />
              Schedule Selected Earliest
            </button>
          </div>
        </div>
        )}
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-slate-700">
            <thead className="bg-slate-800/50">
              <tr>
                <th className="px-3 py-2 text-center">
                  <input
                    type="checkbox"
                    checked={filteredQueueRows.length > 0 && filteredQueueRows.every((job) => selectedWorkOrderIds.has(job.work_order_id))}
                    onChange={(e) => (e.target.checked ? selectAllVisibleRows() : clearSelections())}
                  />
                </th>
                <th className="px-4 py-2 text-left text-xs font-medium text-slate-400 uppercase">WO #</th>
                <th className="px-4 py-2 text-center text-xs font-medium text-slate-400 uppercase">Dispatch</th>
                <th className="px-4 py-2 text-left text-xs font-medium text-slate-400 uppercase">Current Op</th>
                <th className="px-4 py-2 text-left text-xs font-medium text-slate-400 uppercase">Progress</th>
                <th className="px-4 py-2 text-left text-xs font-medium text-slate-400 uppercase">Part</th>
                <th className="px-4 py-2 text-left text-xs font-medium text-slate-400 uppercase">Work Center</th>
                <th className="px-4 py-2 text-right text-xs font-medium text-slate-400 uppercase">Hours Left</th>
                <th className="px-4 py-2 text-left text-xs font-medium text-slate-400 uppercase">Due</th>
                <th className="px-4 py-2 text-left text-xs font-medium text-slate-400 uppercase">Scheduled</th>
                <th className="px-4 py-2 text-center text-xs font-medium text-slate-400 uppercase">Priority</th>
                <th className="px-4 py-2 text-center text-xs font-medium text-slate-400 uppercase">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-700">
              {filteredQueueRows.map((job) => (
                <tr key={job.work_order_id} className={`hover:bg-slate-800/50 ${selectedWorkOrderIds.has(job.work_order_id) ? 'bg-blue-500/10/50' : ''}`}>
                  <td className="px-3 py-2 text-center">
                    <input
                      type="checkbox"
                      checked={selectedWorkOrderIds.has(job.work_order_id)}
                      onChange={() => toggleRowSelection(job.work_order_id)}
                    />
                  </td>
                  <td className="px-4 py-2 font-medium text-werco-primary">{job.work_order_number}</td>
                  <td className="px-4 py-2 text-center">
                    <span className="inline-flex items-center justify-center min-w-[56px] px-2 py-1 rounded text-xs font-semibold bg-blue-500/20 text-blue-300">
                      {job.dispatchScore}
                    </span>
                  </td>
                  <td className="px-4 py-2 text-sm">{job.current_operation_name}</td>
                  <td className="px-4 py-2">
                    <span className="text-sm font-medium">Op {job.operations_complete + 1}/{job.total_operations}</span>
                  </td>
                  <td className="px-4 py-2">
                    <div className="text-sm">{job.part_number}</div>
                    <div className="text-xs text-slate-400">{job.part_name}</div>
                  </td>
                  <td className="px-4 py-2 text-sm">
                    {workCenters.find(wc => wc.id === job.work_center_id)?.code}
                  </td>
                  <td className="px-4 py-2 text-right text-sm">
                    {job.remaining_hours.toFixed(1)}h
                  </td>
                  <td className="px-4 py-2 text-sm">
                    {job.due_date ? formatCentralDate(job.due_date, { year: undefined }) : '-'}
                  </td>
                  <td className="px-4 py-2 text-sm">
                    {inlineEditJobId === job.work_order_id ? (
                      <div className="flex items-center gap-1">
                        <input
                          type="date"
                          value={inlineEditDate}
                          onChange={(e) => setInlineEditDate(e.target.value)}
                          className="input text-xs w-32 py-0.5"
                          autoFocus
                          onKeyDown={(e) => {
                            if (e.key === 'Enter') handleInlineDateSave(job);
                            if (e.key === 'Escape') setInlineEditJobId(null);
                          }}
                        />
                        <button
                          onClick={() => handleInlineDateSave(job)}
                          className="text-green-600 hover:text-green-300 text-xs font-medium"
                        >
                          Save
                        </button>
                        <button
                          onClick={() => setInlineEditJobId(null)}
                          className="text-slate-500 hover:text-slate-400 text-xs"
                        >
                          Cancel
                        </button>
                      </div>
                    ) : (
                      <span
                        className={`cursor-pointer hover:underline ${job.scheduled_start ? 'text-white' : 'text-orange-600 italic'}`}
                        onClick={() => {
                          setInlineEditJobId(job.work_order_id);
                          setInlineEditDate(
                            job.scheduled_start ? getCentralDateStamp(job.scheduled_start) : getCentralTodayISODate()
                          );
                        }}
                        title="Click to edit date"
                      >
                        {job.scheduled_start ? formatCentralDate(job.scheduled_start, { year: undefined }) : 'Unscheduled'}
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-2 text-center">
                    {canEditPriority ? (
                      <select
                        value={job.priority}
                        onChange={(e) => handlePriorityChange(job.work_order_id, e.target.value)}
                        disabled={updatingPriorityWorkOrderId === job.work_order_id}
                        className={`px-2 py-1 rounded text-xs font-bold border border-transparent ${priorityBadgeClasses(job.priority)}`}
                        title="Update priority"
                      >
                        {[1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map((p) => (
                          <option key={p} value={p}>
                            P{p}
                          </option>
                        ))}
                      </select>
                    ) : (
                      <span className={`inline-flex items-center justify-center w-6 h-6 rounded-full text-xs font-bold ${priorityBadgeClasses(job.priority)}`}>
                        {job.priority}
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-2 text-center">
                    <div className="flex justify-center gap-2">
                      <button
                        onClick={() => openScheduleModal(job)}
                        className="text-werco-primary hover:underline text-sm"
                      >
                        Schedule
                      </button>
                      <button
                        onClick={() => handleScheduleEarliest(job)}
                        disabled={schedulingEarliestWorkOrderId === job.work_order_id}
                        className="text-blue-400 hover:underline text-sm disabled:text-slate-500"
                        title="One-click earliest slot"
                      >
                        {schedulingEarliestWorkOrderId === job.work_order_id ? '...' : 'Earliest'}
                      </button>
                      {job.scheduled_start && (
                        <button
                          onClick={() => handleUnschedule(job)}
                          className="text-red-500 hover:underline text-sm"
                          title="Clear this work order's schedule"
                        >
                          Clear
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {filteredQueueRows.length === 0 && (
            <p className="text-center text-slate-400 py-6 text-sm">
              {searchQuery || filterWorkCenter ? 'No work orders match your search or filter' : 'No work orders to display'}
            </p>
          )}
        </div>
      </div>

      {/* Compact Legend */}
      <div className="flex flex-wrap gap-4 text-xs text-slate-400 px-1">
        <div className="flex items-center gap-1.5">
          <span className="w-2 h-2 rounded bg-blue-500/100" /> Ready
          <span className="w-2 h-2 rounded bg-green-500/100 ml-1" /> In Progress
          <span className="w-2 h-2 rounded bg-yellow-500/100 ml-1" /> On Hold
        </div>
        <div className="flex items-center gap-1.5">
          <span className="w-2 h-0.5 bg-red-500/100" /> High Priority
          <span className="w-2 h-0.5 bg-blue-500/100 ml-1" /> Normal
          <span className="w-2 h-0.5 bg-slate-500 ml-1" /> Low
        </div>
        <div className="flex items-center gap-1.5">
          <span className="w-4 h-1.5 rounded-full bg-emerald-500/100" /> &lt;70%
          <span className="w-4 h-1.5 rounded-full bg-yellow-400 ml-1" /> 70-90%
          <span className="w-4 h-1.5 rounded-full bg-red-500/100 ml-1" /> &gt;100%
        </div>
      </div>

      {/* Schedule Modal */}
      {showScheduleModal && selectedJob && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50" onClick={() => { setShowScheduleModal(false); setCapacityPreview(null); }}>
          <div className="bg-[#151b28] rounded-lg p-6 max-w-lg w-full mx-4" onClick={(e) => e.stopPropagation()}>
            <h3 className="text-lg font-semibold mb-4">Schedule Work Order</h3>
            <div className="bg-slate-800/50 rounded p-3 mb-4">
              <div className="flex justify-between items-start">
                <div>
                  <p className="font-medium">{selectedJob.work_order_number}</p>
                  <p className="text-sm text-slate-400">{selectedJob.part_number} - {selectedJob.part_name}</p>
                </div>
                {selectedJob.due_date && (
                  <div className="text-right">
                    <span className="text-xs text-slate-400">Due</span>
                    <p className={`text-sm font-medium ${
                      selectedJob.due_date < getCentralTodayISODate() ? 'text-red-600' : 'text-white'
                    }`}>
                      {formatCentralDate(selectedJob.due_date, { year: undefined })}
                    </p>
                  </div>
                )}
              </div>
              <div className="flex gap-4 mt-2 text-sm text-slate-400">
                <span>Op {selectedJob.operations_complete + 1}/{selectedJob.total_operations}</span>
                <span>{selectedJob.remaining_hours.toFixed(1)}h remaining</span>
                <span>Qty: {selectedJob.quantity}</span>
              </div>
              <p className="text-sm text-werco-primary mt-1">
                Next: {selectedJob.current_operation_name}
                ({selectedJob.setup_hours.toFixed(1)}h setup + {selectedJob.run_hours.toFixed(1)}h run)
              </p>
            </div>
            <form onSubmit={handleSchedule} className="space-y-4">
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="label">Start Date *</label>
                  <input
                    type="date"
                    value={scheduleForm.scheduled_start}
                    onChange={(e) => setScheduleForm({ ...scheduleForm, scheduled_start: e.target.value })}
                    className="input"
                    required
                  />
                </div>
                <div>
                  <label className="label">Work Center</label>
                  <select
                    value={scheduleForm.work_center_id}
                    onChange={(e) => setScheduleForm({ ...scheduleForm, work_center_id: parseInt(e.target.value, 10) })}
                    className="input"
                  >
                    {workCenters.map((wc) => (
                      <option key={wc.id} value={wc.id}>{wc.code} - {wc.name}</option>
                    ))}
                  </select>
                </div>
              </div>

              {/* Capacity Preview */}
              {scheduleForm.scheduled_start && scheduleForm.work_center_id > 0 && (
                <div className={`rounded p-3 text-sm ${
                  loadingCapacity ? 'bg-slate-800/50 text-slate-400' :
                  capacityPreview?.overloaded ? 'bg-red-500/10 border border-red-500/30' :
                  capacityPreview ? 'bg-green-500/10 border border-green-500/30' : 'bg-slate-800/50'
                }`}>
                  {loadingCapacity ? (
                    <span>Loading capacity...</span>
                  ) : capacityPreview ? (
                    <div>
                      <div className="flex justify-between items-center mb-1">
                        <span className="font-medium">
                          Capacity on {formatCentralDate(capacityPreview.date, { year: undefined })}
                        </span>
                        <span className={`font-bold ${capacityPreview.overloaded ? 'text-red-600' : 'text-green-400'}`}>
                          {Math.round(capacityPreview.utilization_pct)}% used
                        </span>
                      </div>
                      <div className="w-full bg-slate-700 rounded-full h-2 mb-2">
                        <div
                          className={`h-2 rounded-full ${
                            capacityPreview.overloaded ? 'bg-red-500/100' :
                            capacityPreview.utilization_pct >= 90 ? 'bg-amber-500/100' :
                            capacityPreview.utilization_pct >= 70 ? 'bg-yellow-400' : 'bg-green-500/100'
                          }`}
                          style={{ width: `${Math.min(100, capacityPreview.utilization_pct)}%` }}
                        />
                      </div>
                      <div className="flex justify-between text-xs text-slate-400">
                        <span>{capacityPreview.available_hours.toFixed(1)}h available</span>
                        <span>{capacityPreview.used_hours.toFixed(1)}h / {capacityPreview.capacity_hours}h</span>
                      </div>
                      {capacityPreview.overloaded && (
                        <p className="text-xs text-red-600 mt-1 flex items-center gap-1">
                          <ExclamationTriangleIcon className="h-3.5 w-3.5" />
                          This date is already over capacity
                        </p>
                      )}
                      {capacityPreview.jobs_on_date.length > 0 && (
                        <div className="mt-2 border-t pt-1">
                          <span className="text-xs text-slate-400">{capacityPreview.jobs_on_date.length} jobs on this date:</span>
                          {capacityPreview.jobs_on_date.slice(0, 3).map((j, idx) => (
                            <div key={idx} className="text-xs text-slate-400">
                              {j.work_order_number} - {j.operation_name} ({j.hours.toFixed(1)}h)
                            </div>
                          ))}
                          {capacityPreview.jobs_on_date.length > 3 && (
                            <span className="text-xs text-slate-500">+{capacityPreview.jobs_on_date.length - 3} more</span>
                          )}
                        </div>
                      )}
                    </div>
                  ) : null}
                </div>
              )}

              <label className="flex items-center gap-2 text-sm text-slate-300">
                <input
                  type="checkbox"
                  checked={forwardSchedule}
                  onChange={(e) => setForwardSchedule(e.target.checked)}
                />
                Forward-schedule all remaining operations
                <span className="text-xs text-slate-500">(cascades dates through routing)</span>
              </label>

              <div className="flex justify-between gap-3 pt-4 border-t">
                <div className="flex gap-2">
                  <button
                    type="button"
                    className="btn-secondary flex items-center text-sm"
                    onClick={() => handleScheduleEarliest(selectedJob)}
                    disabled={schedulingEarliestWorkOrderId === selectedJob.work_order_id}
                  >
                    <BoltIcon className="h-4 w-4 mr-1" />
                    Earliest Slot
                  </button>
                  {selectedJob.scheduled_start && (
                    <button
                      type="button"
                      className="text-red-500 hover:text-red-400 text-sm font-medium px-2"
                      onClick={() => handleUnschedule(selectedJob)}
                    >
                      Unschedule
                    </button>
                  )}
                </div>
                <div className="flex gap-3">
                  <button type="button" onClick={() => { setShowScheduleModal(false); setCapacityPreview(null); }} className="btn-secondary">
                    Cancel
                  </button>
                  <button type="submit" className="btn-primary">Schedule</button>
                </div>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
