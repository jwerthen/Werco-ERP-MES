import React, { useEffect, useState, useRef } from 'react';
import api from '../services/api';
import { format, addDays, startOfWeek, differenceInDays, parseISO, isWithinInterval } from 'date-fns';
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
  const [daysToShow] = useState(14);
  const [selectedJob, setSelectedJob] = useState<ScheduledJob | null>(null);
  const [showScheduleModal, setShowScheduleModal] = useState(false);
  const [scheduleForm, setScheduleForm] = useState({ scheduled_start: '', scheduled_end: '' });

  const days = Array.from({ length: daysToShow }, (_, i) => addDays(weekStart, i));

  useEffect(() => {
    loadData();
  }, [weekStart]);

  const loadData = async () => {
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
  };

  const getJobsForWorkCenterAndDay = (wcId: number, day: Date) => {
    return jobs.filter(job => {
      if (job.work_center_id !== wcId) return false;
      if (!job.scheduled_start) return false;
      
      const jobStart = parseISO(job.scheduled_start);
      const jobEnd = job.scheduled_end ? parseISO(job.scheduled_end) : jobStart;
      
      return isWithinInterval(day, { start: jobStart, end: jobEnd }) ||
             format(jobStart, 'yyyy-MM-dd') === format(day, 'yyyy-MM-dd');
    });
  };

  const getUnscheduledJobs = (wcId: number) => {
    return jobs.filter(job => job.work_center_id === wcId && !job.scheduled_start);
  };

  const openScheduleModal = (job: ScheduledJob) => {
    setSelectedJob(job);
    setScheduleForm({
      scheduled_start: job.scheduled_start ? job.scheduled_start.split('T')[0] : format(new Date(), 'yyyy-MM-dd'),
      scheduled_end: job.scheduled_end ? job.scheduled_end.split('T')[0] : ''
    });
    setShowScheduleModal(true);
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

      {/* Gantt Chart */}
      <div className="card overflow-hidden">
        <div className="overflow-x-auto">
          <table className="min-w-full border-collapse">
            <thead>
              <tr className="bg-gray-50">
                <th className="sticky left-0 bg-gray-50 z-10 px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase w-48 border-r">
                  Work Center
                </th>
                {days.map((day, idx) => (
                  <th
                    key={idx}
                    className={`px-2 py-2 text-center text-xs font-medium min-w-24 border-r ${
                      format(day, 'yyyy-MM-dd') === format(new Date(), 'yyyy-MM-dd')
                        ? 'bg-blue-50 text-blue-700'
                        : [0, 6].includes(day.getDay())
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
                return (
                  <tr key={wc.id} className="border-b hover:bg-gray-50">
                    <td className="sticky left-0 bg-white z-10 px-4 py-3 border-r">
                      <div className="font-medium text-sm">{wc.code}</div>
                      <div className="text-xs text-gray-500">{wc.name}</div>
                      {unscheduled.length > 0 && (
                        <div className="mt-1 text-xs text-orange-600">
                          {unscheduled.length} unscheduled
                        </div>
                      )}
                    </td>
                    {days.map((day, dayIdx) => {
                      const dayJobs = getJobsForWorkCenterAndDay(wc.id, day);
                      const isWeekend = [0, 6].includes(day.getDay());
                      const isToday = format(day, 'yyyy-MM-dd') === format(new Date(), 'yyyy-MM-dd');
                      
                      return (
                        <td
                          key={dayIdx}
                          className={`px-1 py-1 border-r align-top min-h-16 ${
                            isToday ? 'bg-blue-50' : isWeekend ? 'bg-gray-50' : ''
                          }`}
                        >
                          <div className="space-y-1">
                            {dayJobs.map((job) => (
                              <div
                                key={job.operation_id}
                                onClick={() => openScheduleModal(job)}
                                className={`text-xs p-1 rounded cursor-pointer hover:opacity-80 border-l-4 ${
                                  priorityColors[job.priority] || 'border-l-gray-400'
                                } ${statusColors[job.status]} text-white`}
                                title={`${job.work_order_number} - ${job.operation_name}\n${job.part_number}`}
                              >
                                <div className="font-medium truncate">{job.work_order_number}</div>
                                <div className="truncate opacity-90">{job.operation_name}</div>
                              </div>
                            ))}
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
