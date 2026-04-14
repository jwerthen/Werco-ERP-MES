import React, { useEffect, useState, useCallback } from 'react';
import api from '../services/api';
import { formatCentralDate } from '../utils/centralTime';
import {
  PlusIcon,
  XMarkIcon,
  ClockIcon,
  ExclamationTriangleIcon,
  CheckCircleIcon,
  StopIcon,
  ArrowPathIcon,
} from '@heroicons/react/24/outline';
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  Cell,
} from 'recharts';

// ============== Types ==============

interface WorkCenter {
  id: number;
  code: string;
  name: string;
  current_status: string;
  is_active: boolean;
}

interface DowntimeEvent {
  id: number;
  work_center_id: number;
  work_order_id?: number;
  start_time: string;
  end_time?: string;
  duration_minutes?: number;
  category: string;
  planned_type: string;
  reason_code?: string;
  description?: string;
  resolution?: string;
  reported_by: number;
  resolved_by?: number;
  created_at: string;
  updated_at: string;
  work_center?: { id: number; code: string; name: string };
  reporter?: { id: number; username: string; full_name?: string };
  resolver?: { id: number; username: string; full_name?: string };
  work_order?: { id: number; wo_number?: string };
}

interface ReasonCode {
  id: number;
  code: string;
  name: string;
  category: string;
  description?: string;
  is_active: boolean;
  display_order: number;
}

interface DowntimeSummary {
  total_downtime_hours: number;
  planned_hours: number;
  unplanned_hours: number;
  planned_percentage: number;
  unplanned_percentage: number;
  by_category: { category: string; hours: number }[];
  top_reasons: { reason: string; hours: number }[];
  event_count: number;
}

interface WorkCenterDowntime {
  work_center_id: number;
  work_center_code: string;
  work_center_name: string;
  total_hours: number;
  event_count: number;
}

// ============== Constants ==============

const CATEGORIES = [
  { value: 'mechanical', label: 'Mechanical' },
  { value: 'electrical', label: 'Electrical' },
  { value: 'tooling', label: 'Tooling' },
  { value: 'material', label: 'Material' },
  { value: 'operator', label: 'Operator' },
  { value: 'quality', label: 'Quality' },
  { value: 'changeover', label: 'Changeover' },
  { value: 'planned_maintenance', label: 'Planned Maintenance' },
  { value: 'break', label: 'Break' },
  { value: 'meeting', label: 'Meeting' },
  { value: 'no_work', label: 'No Work' },
  { value: 'other', label: 'Other' },
];

const categoryLabel = (cat: string) => {
  const found = CATEGORIES.find((c) => c.value === cat);
  return found ? found.label : cat;
};

function formatDuration(minutes: number): string {
  const hrs = Math.floor(minutes / 60);
  const mins = Math.round(minutes % 60);
  if (hrs > 0) return `${hrs}h ${mins}m`;
  return `${mins}m`;
}

function getElapsedMinutes(startTime: string): number {
  const start = new Date(startTime).getTime();
  const now = Date.now();
  return Math.max(0, (now - start) / 60000);
}

// ============== Component ==============

export default function DowntimeTracking() {
  const [workCenters, setWorkCenters] = useState<WorkCenter[]>([]);
  const [activeEvents, setActiveEvents] = useState<DowntimeEvent[]>([]);
  const [allEvents, setAllEvents] = useState<DowntimeEvent[]>([]);
  const [reasonCodes, setReasonCodes] = useState<ReasonCode[]>([]);
  const [summary, setSummary] = useState<DowntimeSummary | null>(null);
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  const [wcDowntime, setWcDowntime] = useState<WorkCenterDowntime[]>([]);
  const [loading, setLoading] = useState(true);
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  const [tick, setTick] = useState(0);

  // Filters
  const [filterWorkCenter, setFilterWorkCenter] = useState<string>('');
  const [filterCategory, setFilterCategory] = useState<string>('');
  const [filterPlannedType, setFilterPlannedType] = useState<string>('');
  const [filterDateFrom, setFilterDateFrom] = useState<string>('');
  const [filterDateTo, setFilterDateTo] = useState<string>('');

  // Modal
  const [showNewModal, setShowNewModal] = useState(false);
  const [showResolveModal, setShowResolveModal] = useState(false);
  const [resolvingEvent, setResolvingEvent] = useState<DowntimeEvent | null>(null);

  const [newForm, setNewForm] = useState({
    work_center_id: 0,
    category: 'other',
    planned_type: 'unplanned',
    reason_code: '',
    description: '',
  });

  const [resolveForm, setResolveForm] = useState({
    resolution: '',
  });

  // Tick for elapsed timers
  useEffect(() => {
    const interval = setInterval(() => setTick((t: number) => t + 1), 30000);
    return () => clearInterval(interval);
  }, []);

  const loadData = useCallback(async () => {
    try {
      const params: Record<string, any> = {};
      if (filterWorkCenter) params.work_center_id = parseInt(filterWorkCenter);
      if (filterCategory) params.category = filterCategory;
      if (filterPlannedType) params.planned_type = filterPlannedType;
      if (filterDateFrom) params.date_from = filterDateFrom;
      if (filterDateTo) params.date_to = filterDateTo;

      const summaryParams: Record<string, any> = {};
      if (filterDateFrom) summaryParams.date_from = filterDateFrom;
      if (filterDateTo) summaryParams.date_to = filterDateTo;
      if (filterWorkCenter) summaryParams.work_center_id = parseInt(filterWorkCenter);

      const dateParams: Record<string, any> = {};
      if (filterDateFrom) dateParams.date_from = filterDateFrom;
      if (filterDateTo) dateParams.date_to = filterDateTo;

      const [wcRes, activeRes, eventsRes, rcRes, summaryRes, wcDtRes] = await Promise.all([
        api.getWorkCenters(true),
        api.getActiveDowntime(),
        api.getDowntimeEvents(params),
        api.getDowntimeReasonCodes({ active_only: true }),
        api.getDowntimeSummary(summaryParams),
        api.getDowntimeByWorkCenter(dateParams),
      ]);

      setWorkCenters(wcRes);
      setActiveEvents(activeRes as DowntimeEvent[]);
      setAllEvents(eventsRes as DowntimeEvent[]);
      setReasonCodes(rcRes as ReasonCode[]);
      setSummary(summaryRes as DowntimeSummary);
      setWcDowntime(wcDtRes as WorkCenterDowntime[]);
    } catch (err) {
      console.error('Failed to load downtime data:', err);
    } finally {
      setLoading(false);
    }
  }, [filterWorkCenter, filterCategory, filterPlannedType, filterDateFrom, filterDateTo]);

  useEffect(() => {
    loadData();
  }, [loadData]);

  const handleCreateDowntime = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newForm.work_center_id) {
      alert('Please select a work center');
      return;
    }
    try {
      await api.createDowntimeEvent({
        work_center_id: newForm.work_center_id,
        category: newForm.category,
        planned_type: newForm.planned_type,
        reason_code: newForm.reason_code || null,
        description: newForm.description || null,
      });
      setShowNewModal(false);
      setNewForm({ work_center_id: 0, category: 'other', planned_type: 'unplanned', reason_code: '', description: '' });
      loadData();
    } catch (err: any) {
      alert(err.response?.data?.detail || err.message || 'Failed to create downtime event');
    }
  };

  const handleResolve = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!resolvingEvent) return;
    try {
      await api.resolveDowntimeEvent(resolvingEvent.id, {
        resolution: resolveForm.resolution || null,
      });
      setShowResolveModal(false);
      setResolvingEvent(null);
      setResolveForm({ resolution: '' });
      loadData();
    } catch (err: any) {
      alert(err.response?.data?.detail || err.message || 'Failed to resolve downtime event');
    }
  };

  const openResolveModal = (event: DowntimeEvent) => {
    setResolvingEvent(event);
    setResolveForm({ resolution: '' });
    setShowResolveModal(true);
  };

  // Determine which work centers currently have active downtime
  const activeWcIds = new Set(activeEvents.map((e: DowntimeEvent) => e.work_center_id));

  const getWcDisplayStatus = (wc: WorkCenter) => {
    if (activeWcIds.has(wc.id)) return 'down';
    if (wc.current_status === 'maintenance' || wc.current_status === 'offline') return 'idle';
    return 'running';
  };

  const wcStatusColor = (st: string) => {
    if (st === 'down') return 'bg-red-500/100';
    if (st === 'idle') return 'bg-slate-500';
    return 'bg-green-500/100';
  };

  const wcStatusLabel = (st: string) => {
    if (st === 'down') return 'Down';
    if (st === 'idle') return 'Idle';
    return 'Running';
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="loading loading-spinner loading-lg text-primary"></div>
      </div>
    );
  }

  const filteredReasonCodes = newForm.category
    ? reasonCodes.filter((rc: ReasonCode) => rc.category === newForm.category)
    : reasonCodes;

  const paretoData = summary?.top_reasons?.slice(0, 10) || [];

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-white">Downtime Tracking</h1>
          <p className="text-sm text-slate-400 mt-1">Monitor and manage machine downtime events</p>
        </div>
        <div className="flex gap-2">
          <button
            onClick={() => loadData()}
            className="btn btn-ghost btn-sm"
            title="Refresh"
          >
            <ArrowPathIcon className="h-5 w-5" />
          </button>
          <button
            onClick={() => {
              setNewForm({ work_center_id: 0, category: 'other', planned_type: 'unplanned', reason_code: '', description: '' });
              setShowNewModal(true);
            }}
            className="btn btn-primary btn-sm"
          >
            <PlusIcon className="h-5 w-5 mr-1" />
            Log Downtime
          </button>
        </div>
      </div>

      {/* Summary Stats Cards */}
      {summary && (
        <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
          <div className="bg-[#151b28] rounded-lg shadow p-4 border-l-4 border-red-500">
            <div className="text-sm text-slate-400">Total Downtime</div>
            <div className="text-2xl font-bold text-white">{summary.total_downtime_hours}h</div>
            <div className="text-xs text-slate-500">{summary.event_count} events</div>
          </div>
          <div className="bg-[#151b28] rounded-lg shadow p-4 border-l-4 border-blue-500">
            <div className="text-sm text-slate-400">Planned</div>
            <div className="text-2xl font-bold text-blue-400">{summary.planned_hours}h</div>
            <div className="text-xs text-slate-500">{summary.planned_percentage}%</div>
          </div>
          <div className="bg-[#151b28] rounded-lg shadow p-4 border-l-4 border-orange-500">
            <div className="text-sm text-slate-400">Unplanned</div>
            <div className="text-2xl font-bold text-orange-400">{summary.unplanned_hours}h</div>
            <div className="text-xs text-slate-500">{summary.unplanned_percentage}%</div>
          </div>
          <div className="bg-[#151b28] rounded-lg shadow p-4 border-l-4 border-purple-500">
            <div className="text-sm text-slate-400">Top Reason</div>
            <div className="text-lg font-bold text-white truncate">
              {summary.top_reasons?.[0]?.reason || 'N/A'}
            </div>
            <div className="text-xs text-slate-500">
              {summary.top_reasons?.[0]?.hours ? `${summary.top_reasons[0].hours}h` : ''}
            </div>
          </div>
        </div>
      )}

      {/* Work Center Status Board */}
      <div className="bg-[#151b28] rounded-lg shadow p-4">
        <h2 className="text-lg font-semibold text-white mb-3">Work Center Status</h2>
        <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 xl:grid-cols-8 gap-3">
          {workCenters.map((wc) => {
            const st = getWcDisplayStatus(wc);
            return (
              <div
                key={wc.id}
                className={`rounded-lg p-3 text-center border ${
                  st === 'down' ? 'border-red-300 bg-red-500/10' : st === 'idle' ? 'border-slate-600 bg-slate-800/50' : 'border-green-300 bg-green-500/10'
                }`}
              >
                <div className="flex items-center justify-center gap-1 mb-1">
                  <span className={`inline-block w-2.5 h-2.5 rounded-full ${wcStatusColor(st)}`}></span>
                  <span className="text-xs font-medium text-slate-400">{wcStatusLabel(st)}</span>
                </div>
                <div className="font-bold text-sm text-white">{wc.code}</div>
                <div className="text-xs text-slate-400 truncate">{wc.name}</div>
              </div>
            );
          })}
        </div>
      </div>

      {/* Active Downtime Events */}
      {activeEvents.length > 0 && (
        <div className="bg-[#151b28] rounded-lg shadow p-4">
          <h2 className="text-lg font-semibold text-red-400 mb-3 flex items-center gap-2">
            <ExclamationTriangleIcon className="h-5 w-5 text-red-500" />
            Active Downtime ({activeEvents.length})
          </h2>
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {activeEvents.map((evt) => {
              const elapsed = getElapsedMinutes(evt.start_time);
              return (
                <div
                  key={evt.id}
                  className={`rounded-lg border-2 p-4 ${
                    evt.planned_type === 'planned' ? 'border-blue-300 bg-blue-500/10' : 'border-red-300 bg-red-500/10'
                  }`}
                >
                  <div className="flex items-start justify-between">
                    <div>
                      <div className="font-bold text-white">
                        {evt.work_center?.code || `WC-${evt.work_center_id}`}
                      </div>
                      <div className="text-xs text-slate-400">
                        {evt.work_center?.name}
                      </div>
                    </div>
                    <span
                      className={`text-xs px-2 py-0.5 rounded-full font-medium ${
                        evt.planned_type === 'planned'
                          ? 'bg-blue-200 text-blue-300'
                          : 'bg-red-200 text-red-300'
                      }`}
                    >
                      {evt.planned_type === 'planned' ? 'Planned' : 'Unplanned'}
                    </span>
                  </div>
                  <div className="mt-2 flex items-center gap-2">
                    <ClockIcon className="h-4 w-4 text-slate-500" />
                    <span className="text-lg font-mono font-bold text-white">
                      {formatDuration(elapsed)}
                    </span>
                  </div>
                  <div className="mt-1 text-sm text-slate-400">
                    <span className="font-medium">{categoryLabel(evt.category)}</span>
                    {evt.reason_code && <span className="text-slate-500 ml-1">({evt.reason_code})</span>}
                  </div>
                  {evt.description && (
                    <div className="mt-1 text-xs text-slate-400 truncate">{evt.description}</div>
                  )}
                  <div className="mt-1 text-xs text-slate-500">
                    Started: {formatCentralDate(evt.start_time)}
                  </div>
                  <button
                    onClick={() => openResolveModal(evt)}
                    className="btn btn-sm btn-success mt-3 w-full"
                  >
                    <CheckCircleIcon className="h-4 w-4 mr-1" />
                    Resolve
                  </button>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Pareto Chart */}
      {paretoData.length > 0 && (
        <div className="bg-[#151b28] rounded-lg shadow p-4">
          <h2 className="text-lg font-semibold text-white mb-3">Downtime by Reason (Pareto)</h2>
          <ResponsiveContainer width="100%" height={300}>
            <BarChart data={paretoData} margin={{ top: 5, right: 20, left: 10, bottom: 60 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
              <XAxis
                dataKey="reason"
                angle={-35}
                textAnchor="end"
                interval={0}
                height={80}
                tick={{ fontSize: 11, fill: '#94a3b8' }}
                stroke="#334155"
              />
              <YAxis label={{ value: 'Hours', angle: -90, position: 'insideLeft', fill: '#94a3b8' }} tick={{ fill: '#94a3b8' }} stroke="#334155" />
              <Tooltip contentStyle={{ backgroundColor: '#1a1f2e', border: '1px solid #334155', borderRadius: '12px', color: '#e2e8f0' }} formatter={(value: number) => [`${value}h`, 'Downtime']} />
              <Bar dataKey="hours" fill="#ef4444" radius={[4, 4, 0, 0]}>
                {paretoData.map((entry, index) => (
                  <Cell key={`cell-${index}`} fill={index === 0 ? '#ef4444' : index < 3 ? '#f97316' : '#fbbf24'} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}

      {/* Filters */}
      <div className="bg-[#151b28] rounded-lg shadow p-4">
        <h2 className="text-lg font-semibold text-white mb-3">Downtime Log</h2>
        <div className="flex flex-wrap gap-3 mb-4">
          <select
            className="select select-bordered select-sm"
            value={filterWorkCenter}
            onChange={(e) => setFilterWorkCenter(e.target.value)}
          >
            <option value="">All Work Centers</option>
            {workCenters.map((wc) => (
              <option key={wc.id} value={wc.id}>
                {wc.code} - {wc.name}
              </option>
            ))}
          </select>
          <select
            className="select select-bordered select-sm"
            value={filterCategory}
            onChange={(e) => setFilterCategory(e.target.value)}
          >
            <option value="">All Categories</option>
            {CATEGORIES.map((cat) => (
              <option key={cat.value} value={cat.value}>
                {cat.label}
              </option>
            ))}
          </select>
          <select
            className="select select-bordered select-sm"
            value={filterPlannedType}
            onChange={(e) => setFilterPlannedType(e.target.value)}
          >
            <option value="">Planned & Unplanned</option>
            <option value="planned">Planned Only</option>
            <option value="unplanned">Unplanned Only</option>
          </select>
          <input
            type="date"
            className="input input-bordered input-sm"
            value={filterDateFrom}
            onChange={(e) => setFilterDateFrom(e.target.value)}
            placeholder="From"
          />
          <input
            type="date"
            className="input input-bordered input-sm"
            value={filterDateTo}
            onChange={(e) => setFilterDateTo(e.target.value)}
            placeholder="To"
          />
          {(filterWorkCenter || filterCategory || filterPlannedType || filterDateFrom || filterDateTo) && (
            <button
              className="btn btn-ghost btn-sm"
              onClick={() => {
                setFilterWorkCenter('');
                setFilterCategory('');
                setFilterPlannedType('');
                setFilterDateFrom('');
                setFilterDateTo('');
              }}
            >
              Clear Filters
            </button>
          )}
        </div>

        {/* Downtime Log Table */}
        <div className="overflow-x-auto">
          <table className="table table-sm w-full">
            <thead>
              <tr className="bg-slate-800/50">
                <th>Work Center</th>
                <th>Start</th>
                <th>End</th>
                <th>Duration</th>
                <th>Category</th>
                <th>Reason</th>
                <th>Type</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {allEvents.length === 0 ? (
                <tr>
                  <td colSpan={8} className="text-center text-slate-500 py-8">
                    No downtime events found
                  </td>
                </tr>
              ) : (
                allEvents.map((evt) => (
                  <tr key={evt.id} className="hover">
                    <td className="font-medium">
                      {evt.work_center?.code || `WC-${evt.work_center_id}`}
                    </td>
                    <td className="text-sm">{formatCentralDate(evt.start_time)}</td>
                    <td className="text-sm">
                      {evt.end_time ? formatCentralDate(evt.end_time) : '-'}
                    </td>
                    <td className="text-sm font-mono">
                      {evt.duration_minutes
                        ? formatDuration(evt.duration_minutes)
                        : evt.end_time
                        ? '-'
                        : formatDuration(getElapsedMinutes(evt.start_time))}
                    </td>
                    <td>
                      <span className="text-xs">{categoryLabel(evt.category)}</span>
                    </td>
                    <td className="text-sm text-slate-400 max-w-[200px] truncate">
                      {evt.reason_code || evt.description || '-'}
                    </td>
                    <td>
                      <span
                        className={`text-xs px-2 py-0.5 rounded-full font-medium ${
                          evt.planned_type === 'planned'
                            ? 'bg-blue-500/20 text-blue-300'
                            : 'bg-red-500/20 text-red-300'
                        }`}
                      >
                        {evt.planned_type === 'planned' ? 'Planned' : 'Unplanned'}
                      </span>
                    </td>
                    <td>
                      {evt.end_time ? (
                        <span className="text-xs px-2 py-0.5 rounded-full bg-green-500/20 text-green-300">
                          Resolved
                        </span>
                      ) : (
                        <button
                          onClick={() => openResolveModal(evt)}
                          className="text-xs px-2 py-0.5 rounded-full bg-red-500/20 text-red-300 hover:bg-red-200 cursor-pointer"
                        >
                          Active
                        </button>
                      )}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* New Downtime Modal */}
      {showNewModal && (
        <div className="modal modal-open">
          <div className="modal-box max-w-lg">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-lg font-bold">Log Downtime Event</h3>
              <button onClick={() => setShowNewModal(false)} className="btn btn-ghost btn-sm btn-circle">
                <XMarkIcon className="h-5 w-5" />
              </button>
            </div>
            <form onSubmit={handleCreateDowntime} className="space-y-4">
              <div>
                <label className="label"><span className="label-text font-medium">Work Center *</span></label>
                <select
                  className="select select-bordered w-full"
                  value={newForm.work_center_id}
                  onChange={(e) => setNewForm({ ...newForm, work_center_id: parseInt(e.target.value) })}
                  required
                >
                  <option value={0} disabled>Select work center...</option>
                  {workCenters.map((wc) => (
                    <option key={wc.id} value={wc.id}>
                      {wc.code} - {wc.name}
                    </option>
                  ))}
                </select>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="label"><span className="label-text font-medium">Category</span></label>
                  <select
                    className="select select-bordered w-full"
                    value={newForm.category}
                    onChange={(e) => setNewForm({ ...newForm, category: e.target.value, reason_code: '' })}
                  >
                    {CATEGORIES.map((cat) => (
                      <option key={cat.value} value={cat.value}>{cat.label}</option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className="label"><span className="label-text font-medium">Type</span></label>
                  <select
                    className="select select-bordered w-full"
                    value={newForm.planned_type}
                    onChange={(e) => setNewForm({ ...newForm, planned_type: e.target.value })}
                  >
                    <option value="unplanned">Unplanned</option>
                    <option value="planned">Planned</option>
                  </select>
                </div>
              </div>
              <div>
                <label className="label"><span className="label-text font-medium">Reason Code</span></label>
                <select
                  className="select select-bordered w-full"
                  value={newForm.reason_code}
                  onChange={(e) => setNewForm({ ...newForm, reason_code: e.target.value })}
                >
                  <option value="">None</option>
                  {filteredReasonCodes.map((rc) => (
                    <option key={rc.id} value={rc.code}>
                      {rc.code} - {rc.name}
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label className="label"><span className="label-text font-medium">Description</span></label>
                <textarea
                  className="textarea textarea-bordered w-full"
                  rows={3}
                  value={newForm.description}
                  onChange={(e) => setNewForm({ ...newForm, description: e.target.value })}
                  placeholder="Describe the downtime reason..."
                />
              </div>
              <div className="modal-action">
                <button type="button" onClick={() => setShowNewModal(false)} className="btn btn-ghost">
                  Cancel
                </button>
                <button type="submit" className="btn btn-primary">
                  <StopIcon className="h-4 w-4 mr-1" />
                  Start Downtime
                </button>
              </div>
            </form>
          </div>
          <div className="modal-backdrop" onClick={() => setShowNewModal(false)}></div>
        </div>
      )}

      {/* Resolve Downtime Modal */}
      {showResolveModal && resolvingEvent && (
        <div className="modal modal-open">
          <div className="modal-box max-w-md">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-lg font-bold">Resolve Downtime</h3>
              <button onClick={() => setShowResolveModal(false)} className="btn btn-ghost btn-sm btn-circle">
                <XMarkIcon className="h-5 w-5" />
              </button>
            </div>
            <div className="mb-4 p-3 bg-slate-800/50 rounded-lg text-sm">
              <div><strong>Work Center:</strong> {resolvingEvent.work_center?.code} - {resolvingEvent.work_center?.name}</div>
              <div><strong>Category:</strong> {categoryLabel(resolvingEvent.category)}</div>
              <div><strong>Started:</strong> {formatCentralDate(resolvingEvent.start_time)}</div>
              <div><strong>Elapsed:</strong> {formatDuration(getElapsedMinutes(resolvingEvent.start_time))}</div>
            </div>
            <form onSubmit={handleResolve} className="space-y-4">
              <div>
                <label className="label"><span className="label-text font-medium">Resolution Notes</span></label>
                <textarea
                  className="textarea textarea-bordered w-full"
                  rows={3}
                  value={resolveForm.resolution}
                  onChange={(e) => setResolveForm({ ...resolveForm, resolution: e.target.value })}
                  placeholder="Describe what was done to resolve..."
                />
              </div>
              <div className="modal-action">
                <button type="button" onClick={() => setShowResolveModal(false)} className="btn btn-ghost">
                  Cancel
                </button>
                <button type="submit" className="btn btn-success">
                  <CheckCircleIcon className="h-4 w-4 mr-1" />
                  Resolve
                </button>
              </div>
            </form>
          </div>
          <div className="modal-backdrop" onClick={() => setShowResolveModal(false)}></div>
        </div>
      )}
    </div>
  );
}
