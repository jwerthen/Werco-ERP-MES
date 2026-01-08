import React, { useEffect, useState, useCallback } from 'react';
import api from '../services/api';
import { format, addDays, startOfWeek, parseISO, isBefore, isAfter, isSameDay } from 'date-fns';
import {
  ChevronLeftIcon,
  ChevronRightIcon,
  CalendarIcon,
} from '@heroicons/react/24/outline';

interface WorkCenter {
  id: number;
  code: string;
  name: string;
}

interface ScheduledJob {
  id: number;
  work_order_id: number;
  work_order_number: string;
  operation_id: number;
  operation_name: string;
  part_number: string;
  part_name: string;
  work_center_id: number;
  status: string;
  scheduled_start?: string;
  scheduled_end?: string;
  due_date?: string;
  quantity: number;
  priority: number;
  setup_hours: number;
  run_hours: number;
}

interface DragState {
  job: ScheduledJob | null;
  isDragging: boolean;
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

export default function Scheduling() {
  const [workCenters, setWorkCenters] = useState<WorkCenter[]>([]);
  const [jobs, setJobs] = useState<ScheduledJob[]>([]);
  const [loading, setLoading] = useState(true);
  const [weekStart, setWeekStart] = useState(startOfWeek(new Date(), { weekStartsOn: 1 }));
  const [daysToShow] = useState(7);
  const [selectedJob, setSelectedJob] = useState<ScheduledJob | null>(null);
  const [showScheduleModal, setShowScheduleModal] = useState(false);
  const [scheduleForm, setScheduleForm] = useState({ scheduled_start: '', scheduled_end: '', work_center_id: 0 });
  
  // Drag and drop state
  const [dragState, setDragState] = useState<DragState>({ job: null, isDragging: false });
  const [dropTargetWc, setDropTargetWc] = useState<number | null>(null);

  // Generate days for display: Monday-Saturday only (skip Sundays)
  const days = Array.from({ length: daysToShow }, (_, i) => addDays(weekStart, i))
    .filter(day => day.getDay() !== 0); // 0 = Sunday

  const loadData = useCallback(async () => {
    try {
      const [wcRes, jobsRes] = await Promise.all([
        api.getWorkCenters(),
        api.getScheduledJobs({
          start_date: format(weekStart, 'yyyy-MM-dd'),
          end_date: format(addDays(weekStart, daysToShow), 'yyyy-MM-dd')
        })
      ]);
      setWorkCenters(wcRes);
      setJobs(jobsRes);
    } catch (err) {
      console.error('Failed to load scheduling data:', err);
    } finally {
      setLoading(false);
    }
  }, [weekStart, daysToShow]);

  useEffect(() => {
    loadData();
  }, [loadData]);

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
    return jobs.filter(job => job.work_center_id === wcId && !job.scheduled_start);
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
    e.dataTransfer.setData('text/plain', job.operation_id.toString());
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
      // Update work center assignment
      await api.updateOperationWorkCenter(job.operation_id, targetWcId);
      
      // Reload data to reflect changes
      loadData();
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to move operation');
    }
    
    setDragState({ job: null, isDragging: false });
  };

  const handleSchedule = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedJob) return;
    
    try {
      await api.scheduleOperation(selectedJob.operation_id, {
        scheduled_start: scheduleForm.scheduled_start,
        scheduled_end: scheduleForm.scheduled_end || null
      });
      setShowScheduleModal(false);
      loadData();
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to schedule');
    }
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
            {format(weekStart, 'MMM d')} - {format(addDays(weekStart, daysToShow - 1), 'MMM d, yyyy')}
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
                                  key={job.operation_id}
                                  draggable
                                  onDragStart={(e) => handleDragStart(e, job)}
                                  onDragEnd={handleDragEnd}
                                  onClick={() => openScheduleModal(job)}
                                  className={`text-xs p-1.5 rounded cursor-move hover:opacity-90 border-l-4 shadow-sm ${
                                    priorityColors[job.priority] || 'border-l-gray-400'
                                  } ${statusColors[job.status]} text-white ${
                                    dragState.job?.operation_id === job.operation_id ? 'opacity-50' : ''
                                  }`}
                                  style={{
                                    width: span > 1 ? `${widthPx}px` : 'auto',
                                    position: span > 1 ? 'absolute' : 'relative',
                                    zIndex: span > 1 ? 5 : 1,
                                    minWidth: '88px'
                                  }}
                                  title={`${job.work_order_number} - ${job.operation_name}\n${job.part_number}\n${span > 1 ? `${span} days` : '1 day'}\nDrag to move to another work center`}
                                >
                                  <div className="font-medium truncate">{job.work_order_number}</div>
                                  <div className="truncate opacity-90">{job.operation_name}</div>
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

      {/* Unscheduled Jobs Queue */}
      <div className="card">
        <h2 className="text-lg font-semibold mb-4">Unscheduled Operations</h2>
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-gray-200">
            <thead className="bg-gray-50">
              <tr>
                <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">WO #</th>
                <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Operation</th>
                <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Part</th>
                <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Work Center</th>
                <th className="px-4 py-2 text-right text-xs font-medium text-gray-500 uppercase">Hours</th>
                <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Due</th>
                <th className="px-4 py-2 text-center text-xs font-medium text-gray-500 uppercase">Priority</th>
                <th className="px-4 py-2 text-center text-xs font-medium text-gray-500 uppercase">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-200">
              {jobs.filter(j => !j.scheduled_start && j.status !== 'complete').map((job) => (
                <tr key={job.operation_id} className="hover:bg-gray-50">
                  <td className="px-4 py-2 font-medium text-werco-primary">{job.work_order_number}</td>
                  <td className="px-4 py-2">{job.operation_name}</td>
                  <td className="px-4 py-2">
                    <div className="text-sm">{job.part_number}</div>
                    <div className="text-xs text-gray-500">{job.part_name}</div>
                  </td>
                  <td className="px-4 py-2 text-sm">
                    {workCenters.find(wc => wc.id === job.work_center_id)?.code}
                  </td>
                  <td className="px-4 py-2 text-right text-sm">
                    {(job.setup_hours + job.run_hours).toFixed(1)}
                  </td>
                  <td className="px-4 py-2 text-sm">
                    {job.due_date ? format(parseISO(job.due_date), 'MMM d') : '-'}
                  </td>
                  <td className="px-4 py-2 text-center">
                    <span className={`inline-flex items-center justify-center w-6 h-6 rounded-full text-xs font-bold ${
                      job.priority <= 2 ? 'bg-red-100 text-red-800' :
                      job.priority <= 5 ? 'bg-yellow-100 text-yellow-800' :
                      'bg-gray-100 text-gray-800'
                    }`}>
                      {job.priority}
                    </span>
                  </td>
                  <td className="px-4 py-2 text-center">
                    <button
                      onClick={() => openScheduleModal(job)}
                      className="text-werco-primary hover:underline text-sm"
                    >
                      Schedule
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {jobs.filter(j => !j.scheduled_start && j.status !== 'complete').length === 0 && (
            <p className="text-center text-gray-500 py-4">All operations are scheduled</p>
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
            <h3 className="text-lg font-semibold mb-4">Schedule Operation</h3>
            <div className="bg-gray-50 rounded p-3 mb-4">
              <p className="font-medium">{selectedJob.work_order_number} - {selectedJob.operation_name}</p>
              <p className="text-sm text-gray-600">{selectedJob.part_number} - {selectedJob.part_name}</p>
              <p className="text-sm text-gray-500 mt-1">
                Est. Hours: {(selectedJob.setup_hours + selectedJob.run_hours).toFixed(1)} |
                Qty: {selectedJob.quantity}
              </p>
            </div>
            <form onSubmit={handleSchedule} className="space-y-4">
              <div className="grid grid-cols-2 gap-4">
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
                  <label className="label">End Date</label>
                  <input
                    type="date"
                    value={scheduleForm.scheduled_end}
                    onChange={(e) => setScheduleForm({ ...scheduleForm, scheduled_end: e.target.value })}
                    className="input"
                  />
                </div>
              </div>
              <div className="flex justify-end gap-3 pt-4 border-t">
                <button type="button" onClick={() => setShowScheduleModal(false)} className="btn-secondary">
                  Cancel
                </button>
                <button type="submit" className="btn-primary">Schedule</button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
