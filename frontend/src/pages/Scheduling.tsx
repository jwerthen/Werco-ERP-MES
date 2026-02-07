import React, { useEffect, useState, useCallback, useMemo, useRef } from 'react';
import api from '../services/api';
import { format, addDays, startOfWeek, parseISO, isBefore, isAfter, isSameDay } from 'date-fns';
import { useWebSocket } from '../hooks/useWebSocket';
import { buildWsUrl, getAccessToken } from '../services/realtime';
import { usePermissions } from '../hooks/usePermissions';
import { calculateDispatchScore } from '../utils/dispatchScore';
import {
  ChevronLeftIcon,
  ChevronRightIcon,
  CalendarIcon,
  BoltIcon,
  ExclamationTriangleIcon,
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
  pending: 'bg-gray-400',
  ready: 'bg-blue-500',
  in_progress: 'bg-green-500',
  complete: 'bg-emerald-600',
  on_hold: 'bg-yellow-500',
};

const priorityColors: Record<number, string> = {
  1: 'border-l-red-500',
  2: 'border-l-red-400',
  3: 'border-l-orange-500',
  5: 'border-l-blue-500',
  7: 'border-l-gray-400',
  10: 'border-l-gray-300',
};

const heatmapCellClass = (utilization: number) => {
  if (utilization > 100) return 'bg-red-200 text-red-900';
  if (utilization >= 90) return 'bg-amber-200 text-amber-900';
  if (utilization >= 70) return 'bg-yellow-100 text-yellow-800';
  return 'bg-emerald-100 text-emerald-900';
};

export default function Scheduling() {
  const { can } = usePermissions();
  const [workCenters, setWorkCenters] = useState<WorkCenter[]>([]);
  const [jobs, setJobs] = useState<ScheduledJob[]>([]);
  const [capacityHeatmap, setCapacityHeatmap] = useState<CapacityHeatmapResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [weekStart, setWeekStart] = useState(startOfWeek(new Date(), { weekStartsOn: 1 }));
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
  const [dropTargetWc, setDropTargetWc] = useState<number | null>(null);
  const canEditPriority = can('work_orders:edit');

  // Generate days for display: Monday-Saturday only (skip Sundays)
  const days = useMemo(
    () => Array.from({ length: daysToShow }, (_, i) => addDays(weekStart, i)).filter((day) => day.getDay() !== 0),
    [daysToShow, weekStart]
  );
  const visibleStart = days[0] || weekStart;
  const visibleEnd = days[days.length - 1] || addDays(weekStart, daysToShow - 1);

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
        const aDue = a.due_date ? new Date(a.due_date).getTime() : Number.MAX_SAFE_INTEGER;
        const bDue = b.due_date ? new Date(b.due_date).getTime() : Number.MAX_SAFE_INTEGER;
        if (aDue !== bDue) return aDue - bDue;
        return a.work_order_number.localeCompare(b.work_order_number);
      });
  }, [openJobs]);

  const queueRows = useMemo(
    () => (showScheduledRows ? dispatchQueue : dispatchQueue.filter((job) => !job.scheduled_start)),
    [dispatchQueue, showScheduledRows]
  );

  const selectedQueueJobs = useMemo(
    () => dispatchQueue.filter((job) => selectedWorkOrderIds.has(job.work_order_id)),
    [dispatchQueue, selectedWorkOrderIds]
  );

  useEffect(() => {
    setSelectedWorkOrderIds((previous) => {
      const activeIds = new Set(dispatchQueue.map((job) => job.work_order_id));
      return new Set([...previous].filter((id) => activeIds.has(id)));
    });
  }, [dispatchQueue]);

  const heatmapByWorkCenter = useMemo(() => {
    const map = new Map<number, CapacityHeatmapRow>();
    (capacityHeatmap?.work_centers || []).forEach((row) => map.set(row.work_center_id, row));
    return map;
  }, [capacityHeatmap]);

  const loadData = useCallback(async () => {
    try {
      const startDate = format(visibleStart, 'yyyy-MM-dd');
      const endDate = format(visibleEnd, 'yyyy-MM-dd');
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
      
      const jobStart = parseISO(job.scheduled_start);
      const jobEnd = job.scheduled_end ? parseISO(job.scheduled_end) : jobStart;
      
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
    
    const jobStart = parseISO(job.scheduled_start);
    const jobEnd = job.scheduled_end ? parseISO(job.scheduled_end) : jobStart;
    
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
      
      const jobStart = parseISO(job.scheduled_start);
      const jobEnd = parseISO(job.scheduled_end);
      
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
      scheduled_start: job.scheduled_start ? job.scheduled_start.split('T')[0] : format(new Date(), 'yyyy-MM-dd'),
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
    setDropTargetWc(null);
  };

  const handleDragOver = (e: React.DragEvent, wcId: number) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    if (dropTargetWc !== wcId) {
      setDropTargetWc(wcId);
    }
  };

  const handleDragLeave = () => {
    setDropTargetWc(null);
  };

  const handleDrop = async (e: React.DragEvent, targetWcId: number) => {
    e.preventDefault();
    setDropTargetWc(null);
    
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
        work_center_id: selectedJob.work_center_id
      });
      setShowScheduleModal(false);
      await loadData();
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to schedule');
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
      const shiftedDate = addDays(parseISO(job.scheduled_start), bulkShiftDays);
      await api.scheduleWorkOrder(job.work_order_id, {
        scheduled_start: format(shiftedDate, 'yyyy-MM-dd'),
        work_center_id: job.work_center_id,
      });
      return 'success';
    });
  };

  const handleBulkScheduleEarliest = async () => {
    await runBulkAction('earliest', async (job) => {
      if (job.scheduled_start) {
        return 'skipped';
      }
      await api.scheduleWorkOrderEarliest(job.work_order_id, {
        work_center_id: job.work_center_id,
      });
      return 'success';
    });
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
    if (priority <= 2) return 'bg-red-100 text-red-800';
    if (priority <= 5) return 'bg-yellow-100 text-yellow-800';
    return 'bg-gray-100 text-gray-800';
  };

  const navigateWeek = (direction: number) => {
    setWeekStart(addDays(weekStart, direction * 7));
  };

  const goToToday = () => {
    setWeekStart(startOfWeek(new Date(), { weekStartsOn: 1 }));
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-werco-primary"></div>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex justify-between items-center">
        <h1 className="text-2xl font-bold text-gray-900">Production Schedule</h1>
        <div className="flex items-center gap-2">
          <button onClick={() => navigateWeek(-1)} className="p-2 hover:bg-gray-100 rounded">
            <ChevronLeftIcon className="h-5 w-5" />
          </button>
          <button onClick={goToToday} className="btn-secondary flex items-center text-sm">
            <CalendarIcon className="h-4 w-4 mr-1" />
            Today
          </button>
          <button onClick={() => navigateWeek(1)} className="p-2 hover:bg-gray-100 rounded">
            <ChevronRightIcon className="h-5 w-5" />
          </button>
          <span className="ml-4 font-medium">
            {format(visibleStart, 'MMM d')} - {format(visibleEnd, 'MMM d, yyyy')}
          </span>
        </div>
      </div>

      {/* Gantt Chart with Continuous Bars and Drag-Drop */}
      <div className="card overflow-hidden">
        <div className="overflow-x-auto">
          <table className="min-w-full border-collapse" style={{ tableLayout: 'fixed' }}>
            <thead>
              <tr className="bg-gray-50">
                <th className="sticky left-0 bg-gray-50 z-10 px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase w-48 border-r">
                  Work Center
                </th>
                {days.map((day, idx) => (
                  <th
                    key={idx}
                    className={`px-2 py-2 text-center text-xs font-medium w-24 border-r ${
                      format(day, 'yyyy-MM-dd') === format(new Date(), 'yyyy-MM-dd')
                        ? 'bg-blue-50 text-blue-700'
                        : day.getDay() === 6
                        ? 'bg-gray-100 text-gray-500'
                        : 'text-gray-500'
                    }`}
                  >
                    <div>{format(day, 'EEE')}</div>
                    <div className="text-sm font-bold">{format(day, 'd')}</div>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {workCenters.map((wc) => {
                const unscheduled = getUnscheduledJobs(wc.id);
                const isDropTarget = dropTargetWc === wc.id;
                const heatmapRow = heatmapByWorkCenter.get(wc.id);
                const hasOverload = Boolean(heatmapRow?.days.some((day) => day.overloaded));
                
                return (
                  <tr 
                    key={wc.id} 
                    className={`border-b transition-colors ${
                      isDropTarget ? 'bg-blue-100' : 'hover:bg-gray-50'
                    }`}
                    onDragOver={(e) => handleDragOver(e, wc.id)}
                    onDragLeave={handleDragLeave}
                    onDrop={(e) => handleDrop(e, wc.id)}
                  >
                    <td className={`sticky left-0 z-10 px-4 py-3 border-r ${isDropTarget ? 'bg-blue-100' : 'bg-white'}`}>
                      <div className="font-medium text-sm">{wc.code}</div>
                      <div className="text-xs text-gray-500">{wc.name}</div>
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
                      {isDropTarget && dragState.job && (
                        <div className="mt-1 text-xs text-blue-600 font-medium">
                          Drop to move here
                        </div>
                      )}
                    </td>
                    {days.map((day, dayIdx) => {
                      const jobsStartingToday = getJobsStartingOnDay(wc.id, day, dayIdx);
                      const spanningJob = isJobSpanningDay(wc.id, day, dayIdx);
                      const isWeekend = day.getDay() === 6; // Saturday only
                      const isToday = format(day, 'yyyy-MM-dd') === format(new Date(), 'yyyy-MM-dd');
                      
                      // If a job is spanning through this day (but didn't start here), render empty cell
                      // The bar from the start day will cover this cell via colspan
                      if (spanningJob && jobsStartingToday.length === 0) {
                        return null; // Cell is covered by colspan from previous day
                      }
                      
                      return (
                        <td
                          key={dayIdx}
                          colSpan={jobsStartingToday.length > 0 ? 1 : 1}
                          className={`px-1 py-1 border-r align-top h-16 relative ${
                            isToday ? 'bg-blue-50' : isWeekend ? 'bg-gray-50' : ''
                          } ${isDropTarget ? 'bg-blue-100' : ''}`}
                        >
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
                                  title={`${job.work_order_number} - ${job.part_number}\nOp ${job.operations_complete + 1}/${job.total_operations}: ${job.current_operation_name}\n${span > 1 ? `${span} days` : '1 day'}\nDrag to move to another work center`}
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
          Dragging: {dragState.job?.work_order_number} - Drop on a work center row to move
        </div>
      )}

      <div className="card">
        <div className="flex flex-wrap items-center justify-between gap-3 mb-3">
          <h2 className="text-lg font-semibold">Capacity Heatmap</h2>
          <div className="flex items-center gap-2">
            <span className="text-sm text-gray-600">
              {capacityHeatmap?.overload_cells || 0} overloaded slot{capacityHeatmap?.overload_cells === 1 ? '' : 's'}
            </span>
            {(capacityHeatmap?.overload_cells || 0) > 0 && (
              <span className="inline-flex items-center gap-1 px-2 py-1 rounded text-xs font-medium bg-red-100 text-red-700">
                <ExclamationTriangleIcon className="h-3.5 w-3.5" />
                Action Needed
              </span>
            )}
          </div>
        </div>
        <div className="overflow-x-auto">
          <table className="min-w-full border-collapse text-xs">
            <thead>
              <tr className="bg-gray-50">
                <th className="sticky left-0 bg-gray-50 z-10 px-3 py-2 text-left border-r">Work Center</th>
                {days.map((day) => (
                  <th key={`hm-${day.toISOString()}`} className="px-2 py-2 text-center border-r min-w-[84px]">
                    {format(day, 'EEE d')}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {workCenters.map((wc) => {
                const row = heatmapByWorkCenter.get(wc.id);
                return (
                  <tr key={`hm-row-${wc.id}`} className="border-t">
                    <td className="sticky left-0 z-10 bg-white px-3 py-2 border-r">
                      <div className="font-medium">{wc.code}</div>
                      <div className="text-[11px] text-gray-500">{wc.capacity_hours_per_day || 8}h/day</div>
                    </td>
                    {days.map((day) => {
                      const key = format(day, 'yyyy-MM-dd');
                      const dayData = row?.days.find((entry) => entry.date === key);
                      const utilization = dayData?.utilization_pct || 0;
                      return (
                        <td key={`${wc.id}-${key}`} className="px-1.5 py-1.5 border-r">
                          <div className={`rounded px-1.5 py-1 text-center font-medium ${heatmapCellClass(utilization)}`}>
                            {Math.round(utilization)}%
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

      {/* Dispatch Queue and Bulk Actions */}
      <div className="card">
        <div className="flex flex-wrap items-start justify-between gap-3 mb-4">
          <div>
            <h2 className="text-lg font-semibold">Dispatch Queue</h2>
            <p className="text-sm text-gray-600">
              Sorted by dispatch score. {dispatchQueue.filter((job) => !job.scheduled_start).length} unscheduled.
            </p>
          </div>
          {canEditPriority && (
            <div className="w-full sm:w-96">
              <label className="text-xs font-medium text-gray-600 block mb-1">
                Optional Priority Reason
              </label>
              <input
                type="text"
                value={priorityReason}
                onChange={(e) => setPriorityReason(e.target.value)}
                className="input text-sm"
                maxLength={500}
                placeholder="Applied to your next priority update"
              />
            </div>
          )}
        </div>
        <div className="border rounded-lg p-3 mb-4 bg-gray-50">
          <div className="flex flex-wrap items-center gap-2 mb-3">
            <span className="text-sm font-medium">Bulk Actions</span>
            <span className="text-sm text-gray-600">Selected: {selectedQueueJobs.length}</span>
            <button type="button" onClick={selectAllVisibleRows} className="text-xs text-werco-primary hover:underline">
              Select visible
            </button>
            <button type="button" onClick={clearSelections} className="text-xs text-gray-600 hover:underline">
              Clear
            </button>
            <label className="ml-auto text-xs text-gray-600 flex items-center gap-2">
              <input
                type="checkbox"
                checked={showScheduledRows}
                onChange={(e) => setShowScheduledRows(e.target.checked)}
              />
              Include scheduled rows
            </label>
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
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-gray-200">
            <thead className="bg-gray-50">
              <tr>
                <th className="px-3 py-2 text-center">
                  <input
                    type="checkbox"
                    checked={queueRows.length > 0 && queueRows.every((job) => selectedWorkOrderIds.has(job.work_order_id))}
                    onChange={(e) => (e.target.checked ? selectAllVisibleRows() : clearSelections())}
                  />
                </th>
                <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">WO #</th>
                <th className="px-4 py-2 text-center text-xs font-medium text-gray-500 uppercase">Dispatch</th>
                <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Current Op</th>
                <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Progress</th>
                <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Part</th>
                <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Work Center</th>
                <th className="px-4 py-2 text-right text-xs font-medium text-gray-500 uppercase">Hours Left</th>
                <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Due</th>
                <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Scheduled</th>
                <th className="px-4 py-2 text-center text-xs font-medium text-gray-500 uppercase">Priority</th>
                <th className="px-4 py-2 text-center text-xs font-medium text-gray-500 uppercase">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-200">
              {queueRows.map((job) => (
                <tr key={job.work_order_id} className={`hover:bg-gray-50 ${selectedWorkOrderIds.has(job.work_order_id) ? 'bg-blue-50/50' : ''}`}>
                  <td className="px-3 py-2 text-center">
                    <input
                      type="checkbox"
                      checked={selectedWorkOrderIds.has(job.work_order_id)}
                      onChange={() => toggleRowSelection(job.work_order_id)}
                    />
                  </td>
                  <td className="px-4 py-2 font-medium text-werco-primary">{job.work_order_number}</td>
                  <td className="px-4 py-2 text-center">
                    <span className="inline-flex items-center justify-center min-w-[56px] px-2 py-1 rounded text-xs font-semibold bg-blue-100 text-blue-800">
                      {job.dispatchScore}
                    </span>
                  </td>
                  <td className="px-4 py-2 text-sm">{job.current_operation_name}</td>
                  <td className="px-4 py-2">
                    <span className="text-sm font-medium">Op {job.operations_complete + 1}/{job.total_operations}</span>
                  </td>
                  <td className="px-4 py-2">
                    <div className="text-sm">{job.part_number}</div>
                    <div className="text-xs text-gray-500">{job.part_name}</div>
                  </td>
                  <td className="px-4 py-2 text-sm">
                    {workCenters.find(wc => wc.id === job.work_center_id)?.code}
                  </td>
                  <td className="px-4 py-2 text-right text-sm">
                    {job.remaining_hours.toFixed(1)}h
                  </td>
                  <td className="px-4 py-2 text-sm">
                    {job.due_date ? format(parseISO(job.due_date), 'MMM d') : '-'}
                  </td>
                  <td className="px-4 py-2 text-sm">
                    {job.scheduled_start ? format(parseISO(job.scheduled_start), 'MMM d') : 'Unscheduled'}
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
                        className="text-blue-700 hover:underline text-sm disabled:text-gray-400"
                        title="One-click earliest slot"
                      >
                        {schedulingEarliestWorkOrderId === job.work_order_id ? '...' : 'Earliest'}
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {queueRows.length === 0 && (
            <p className="text-center text-gray-500 py-4">No work orders match current filter</p>
          )}
        </div>
      </div>

      {/* Legend */}
      <div className="flex gap-6 text-sm">
        <div className="flex items-center gap-2">
          <span className="font-medium">Status:</span>
          {Object.entries(statusColors).map(([status, color]) => (
            <span key={status} className="flex items-center gap-1">
              <span className={`w-3 h-3 rounded ${color}`}></span>
              <span className="capitalize">{status.replace('_', ' ')}</span>
            </span>
          ))}
        </div>
        <div className="flex items-center gap-2">
          <span className="font-medium">Priority:</span>
          <span className="flex items-center gap-1">
            <span className="w-3 h-3 border-l-4 border-red-500"></span>
            <span>High</span>
          </span>
          <span className="flex items-center gap-1">
            <span className="w-3 h-3 border-l-4 border-blue-500"></span>
            <span>Normal</span>
          </span>
          <span className="flex items-center gap-1">
            <span className="w-3 h-3 border-l-4 border-gray-400"></span>
            <span>Low</span>
          </span>
        </div>
      </div>

      {/* Schedule Modal */}
      {showScheduleModal && selectedJob && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg p-6 max-w-md w-full mx-4">
            <h3 className="text-lg font-semibold mb-4">Schedule Work Order</h3>
            <div className="bg-gray-50 rounded p-3 mb-4">
              <p className="font-medium">{selectedJob.work_order_number}</p>
              <p className="text-sm text-gray-600">{selectedJob.part_number} - {selectedJob.part_name}</p>
              <p className="text-sm text-gray-500 mt-1">
                Progress: Op {selectedJob.operations_complete + 1}/{selectedJob.total_operations} |
                Remaining: {selectedJob.remaining_hours.toFixed(1)}h |
                Qty: {selectedJob.quantity}
              </p>
              <p className="text-sm text-werco-primary mt-1">
                Next Op: {selectedJob.current_operation_name}
              </p>
            </div>
            <form onSubmit={handleSchedule} className="space-y-4">
              <div>
                <label className="label">Start Date *</label>
                <input
                  type="date"
                  value={scheduleForm.scheduled_start}
                  onChange={(e) => setScheduleForm({ ...scheduleForm, scheduled_start: e.target.value })}
                  className="input"
                  required
                />
                <p className="text-xs text-gray-500 mt-1">
                  Scheduling this work order will start the first operation. Subsequent operations will auto-advance when each is completed.
                </p>
              </div>
              <div className="flex justify-between gap-3 pt-4 border-t">
                <button
                  type="button"
                  className="btn-secondary flex items-center text-sm"
                  onClick={() => handleScheduleEarliest(selectedJob)}
                  disabled={schedulingEarliestWorkOrderId === selectedJob.work_order_id}
                >
                  <BoltIcon className="h-4 w-4 mr-1" />
                  Earliest Slot
                </button>
                <div className="flex gap-3">
                  <button type="button" onClick={() => setShowScheduleModal(false)} className="btn-secondary">
                    Cancel
                  </button>
                  <button type="submit" className="btn-primary">Schedule Work Order</button>
                </div>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
