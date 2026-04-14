import React, { useState, useEffect, useMemo, useCallback } from 'react';
import api from '../services/api';
import {
  PlusIcon,
  MagnifyingGlassIcon,
  XMarkIcon,
  ExclamationTriangleIcon,
  CheckCircleIcon,
  CalendarDaysIcon,
  PlayIcon,
} from '@heroicons/react/24/outline';

interface MaintenanceSchedule {
  id: number;
  work_center_id: number;
  work_center_name?: string;
  maintenance_type: string;
  frequency: string;
  frequency_value?: number;
  description: string;
  checklist?: string;
  estimated_duration_hours: number;
  last_performed_at?: string;
  next_due_date?: string;
  is_active: boolean;
  created_at: string;
}

interface MaintenanceWorkOrder {
  id: number;
  schedule_id?: number;
  work_center_id: number;
  work_center_name?: string;
  maintenance_type: string;
  priority: string;
  status: string;
  title: string;
  description?: string;
  assigned_to?: number;
  assigned_to_name?: string;
  scheduled_date?: string;
  started_at?: string;
  completed_at?: string;
  duration_hours?: number;
  parts_used?: string;
  labor_cost?: number;
  parts_cost?: number;
  notes?: string;
  created_at: string;
}

interface Dashboard {
  scheduled_this_week: number;
  overdue: number;
  completed_this_month: number;
  open_work_orders: number;
  upcoming: any[];
}

type Tab = 'dashboard' | 'schedules' | 'work_orders';

const statusColors: Record<string, { bg: string; text: string }> = {
  open: { bg: 'bg-blue-500/20', text: 'text-blue-300' },
  in_progress: { bg: 'bg-yellow-500/20', text: 'text-yellow-300' },
  completed: { bg: 'bg-green-500/20', text: 'text-emerald-300' },
  cancelled: { bg: 'bg-slate-800/50', text: 'text-slate-100' },
  overdue: { bg: 'bg-red-500/20', text: 'text-red-300' },
};

const priorityColors: Record<string, { bg: string; text: string }> = {
  low: { bg: 'bg-slate-800/50', text: 'text-slate-300' },
  medium: { bg: 'bg-blue-500/20', text: 'text-blue-400' },
  high: { bg: 'bg-orange-500/20', text: 'text-orange-700' },
  critical: { bg: 'bg-red-500/20', text: 'text-red-400' },
  emergency: { bg: 'bg-red-200', text: 'text-red-300' },
};

export default function Maintenance() {
  const [activeTab, setActiveTab] = useState<Tab>('dashboard');
  const [dashboard, setDashboard] = useState<Dashboard | null>(null);
  const [schedules, setSchedules] = useState<MaintenanceSchedule[]>([]);
  const [workOrders, setWorkOrders] = useState<MaintenanceWorkOrder[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [search, setSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [showCreateScheduleModal, setShowCreateScheduleModal] = useState(false);
  const [showCreateWOModal, setShowCreateWOModal] = useState(false);
  const [showCompleteModal, setShowCompleteModal] = useState(false);
  const [selectedWO, setSelectedWO] = useState<MaintenanceWorkOrder | null>(null);

  const [scheduleForm, setScheduleForm] = useState({
    work_center_id: '', maintenance_type: 'preventive', frequency: 'monthly',
    frequency_value: '1', description: '', checklist: '', estimated_duration_hours: '1',
  });
  const [woForm, setWoForm] = useState({
    work_center_id: '', maintenance_type: 'preventive', priority: 'medium',
    title: '', description: '', scheduled_date: '',
  });
  const [completeForm, setCompleteForm] = useState({ notes: '', parts_used: '', labor_cost: '', parts_cost: '' });

  const loadData = useCallback(async () => {
    try {
      setLoading(true);
      setError('');
      const [dashData, schedData, woData] = await Promise.all([
        api.getMaintenanceDashboard().catch(() => null),
        api.getMaintenanceSchedules({}).catch(() => []),
        api.getMaintenanceWorkOrders({}).catch(() => []),
      ]);
      setDashboard(dashData);
      setSchedules(schedData || []);
      setWorkOrders(woData || []);
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to load data');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadData(); }, [loadData]);

  const filteredWOs = useMemo(() => {
    return workOrders.filter(wo => {
      if (statusFilter && wo.status !== statusFilter) return false;
      if (search) {
        const s = search.toLowerCase();
        return wo.title?.toLowerCase().includes(s) || wo.work_center_name?.toLowerCase().includes(s);
      }
      return true;
    });
  }, [workOrders, statusFilter, search]);

  const handleCreateSchedule = async () => {
    try {
      await api.createMaintenanceSchedule({
        ...scheduleForm,
        work_center_id: parseInt(scheduleForm.work_center_id),
        frequency_value: parseInt(scheduleForm.frequency_value),
        estimated_duration_hours: parseFloat(scheduleForm.estimated_duration_hours),
      });
      setShowCreateScheduleModal(false);
      setScheduleForm({ work_center_id: '', maintenance_type: 'preventive', frequency: 'monthly', frequency_value: '1', description: '', checklist: '', estimated_duration_hours: '1' });
      loadData();
    } catch (err: any) { alert(err.response?.data?.detail || 'Failed to create schedule'); }
  };

  const handleCreateWO = async () => {
    try {
      await api.createMaintenanceWorkOrder({
        ...woForm,
        work_center_id: parseInt(woForm.work_center_id),
      });
      setShowCreateWOModal(false);
      setWoForm({ work_center_id: '', maintenance_type: 'preventive', priority: 'medium', title: '', description: '', scheduled_date: '' });
      loadData();
    } catch (err: any) { alert(err.response?.data?.detail || 'Failed to create work order'); }
  };

  const handleStart = async (wo: MaintenanceWorkOrder) => {
    try {
      await api.startMaintenanceWorkOrder(wo.id);
      loadData();
    } catch (err: any) { alert(err.response?.data?.detail || 'Failed to start'); }
  };

  const handleComplete = async () => {
    if (!selectedWO) return;
    try {
      await api.completeMaintenanceWorkOrder(selectedWO.id, {
        notes: completeForm.notes,
        parts_used: completeForm.parts_used,
        labor_cost: completeForm.labor_cost ? parseFloat(completeForm.labor_cost) : undefined,
        parts_cost: completeForm.parts_cost ? parseFloat(completeForm.parts_cost) : undefined,
      });
      setShowCompleteModal(false);
      setCompleteForm({ notes: '', parts_used: '', labor_cost: '', parts_cost: '' });
      loadData();
    } catch (err: any) { alert(err.response?.data?.detail || 'Failed to complete'); }
  };

  if (loading) {
    return <div className="p-6"><div className="animate-pulse space-y-4"><div className="h-8 bg-gray-200 rounded w-1/4" /><div className="grid grid-cols-4 gap-4">{[...Array(4)].map((_, i) => <div key={i} className="h-24 bg-gray-200 rounded" />)}</div><div className="h-64 bg-gray-200 rounded" /></div></div>;
  }

  if (error) {
    return <div className="p-6"><div className="bg-red-500/10 border border-red-500/30 rounded-lg p-4 flex items-center gap-3"><ExclamationTriangleIcon className="w-5 h-5 text-red-500" /><span className="text-red-400">{error}</span><button onClick={loadData} className="ml-auto text-red-600 hover:text-red-300">Retry</button></div></div>;
  }

  return (
    <div className="p-6 space-y-6">
      <div className="flex justify-between items-center">
        <h1 className="text-2xl font-bold text-white">Preventive Maintenance</h1>
        <div className="flex gap-2">
          <button onClick={() => setShowCreateScheduleModal(true)} className="inline-flex items-center px-3 py-2 border border-slate-600 rounded-lg hover:bg-slate-800 text-sm">
            <CalendarDaysIcon className="w-4 h-4 mr-1" />New Schedule
          </button>
          <button onClick={() => setShowCreateWOModal(true)} className="inline-flex items-center px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700">
            <PlusIcon className="w-5 h-5 mr-2" />New Work Order
          </button>
        </div>
      </div>

      {/* Summary Cards */}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
        <div className="bg-[#151b28] rounded-lg shadow p-4 border-l-4 border-blue-500">
          <div className="text-sm text-slate-400">Scheduled This Week</div>
          <div className="text-2xl font-bold">{dashboard?.scheduled_this_week || 0}</div>
        </div>
        <div className="bg-[#151b28] rounded-lg shadow p-4 border-l-4 border-red-500">
          <div className="text-sm text-slate-400">Overdue</div>
          <div className="text-2xl font-bold text-red-600">{dashboard?.overdue || 0}</div>
        </div>
        <div className="bg-[#151b28] rounded-lg shadow p-4 border-l-4 border-green-500">
          <div className="text-sm text-slate-400">Completed This Month</div>
          <div className="text-2xl font-bold text-green-600">{dashboard?.completed_this_month || 0}</div>
        </div>
        <div className="bg-[#151b28] rounded-lg shadow p-4 border-l-4 border-orange-500">
          <div className="text-sm text-slate-400">Open Work Orders</div>
          <div className="text-2xl font-bold text-orange-600">{dashboard?.open_work_orders || 0}</div>
        </div>
      </div>

      {/* Tabs */}
      <div className="border-b border-slate-700">
        <nav className="flex -mb-px space-x-6">
          {(['dashboard', 'schedules', 'work_orders'] as Tab[]).map(tab => (
            <button key={tab} onClick={() => setActiveTab(tab)}
              className={`py-3 px-1 border-b-2 text-sm font-medium capitalize ${activeTab === tab ? 'border-blue-500 text-blue-600' : 'border-transparent text-slate-400 hover:text-slate-300'}`}>
              {tab.replace(/_/g, ' ')}
            </button>
          ))}
        </nav>
      </div>

      {/* Dashboard Tab */}
      {activeTab === 'dashboard' && (
        <div className="space-y-4">
          <h3 className="text-lg font-semibold">Upcoming Maintenance</h3>
          {dashboard?.upcoming && dashboard.upcoming.length > 0 ? (
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
              {dashboard.upcoming.map((item: any, i: number) => (
                <div key={i} className="bg-[#151b28] rounded-lg shadow p-4 border-l-4 border-blue-400">
                  <div className="font-medium">{item.title || item.description}</div>
                  <div className="text-sm text-slate-400 mt-1">{item.work_center_name}</div>
                  <div className="text-sm text-slate-400 mt-1">Due: {item.next_due_date ? new Date(item.next_due_date).toLocaleDateString() : item.scheduled_date ? new Date(item.scheduled_date).toLocaleDateString() : '-'}</div>
                  <div className="mt-2">
                    <span className={`text-xs px-2 py-1 rounded-full ${item.maintenance_type === 'preventive' ? 'bg-blue-500/20 text-blue-400' : 'bg-orange-500/20 text-orange-700'}`}>
                      {item.maintenance_type}
                    </span>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-slate-400">No upcoming maintenance scheduled</p>
          )}

          <h3 className="text-lg font-semibold mt-6">Recent Work Orders</h3>
          <div className="bg-[#151b28] rounded-lg shadow overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-slate-800 text-left text-xs font-medium text-slate-400 uppercase">
                <tr>
                  <th className="px-4 py-3">Title</th>
                  <th className="px-4 py-3">Work Center</th>
                  <th className="px-4 py-3">Type</th>
                  <th className="px-4 py-3">Priority</th>
                  <th className="px-4 py-3">Status</th>
                  <th className="px-4 py-3">Date</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-700">
                {workOrders.slice(0, 10).map(wo => (
                  <tr key={wo.id} className="hover:bg-slate-800">
                    <td className="px-4 py-3 font-medium">{wo.title}</td>
                    <td className="px-4 py-3">{wo.work_center_name || '-'}</td>
                    <td className="px-4 py-3 capitalize">{wo.maintenance_type}</td>
                    <td className="px-4 py-3">
                      <span className={`px-2 py-1 rounded-full text-xs font-medium ${priorityColors[wo.priority]?.bg || 'bg-slate-800/50'} ${priorityColors[wo.priority]?.text || ''}`}>
                        {wo.priority}
                      </span>
                    </td>
                    <td className="px-4 py-3">
                      <span className={`px-2 py-1 rounded-full text-xs font-medium ${statusColors[wo.status]?.bg || 'bg-slate-800/50'} ${statusColors[wo.status]?.text || ''}`}>
                        {wo.status?.replace(/_/g, ' ')}
                      </span>
                    </td>
                    <td className="px-4 py-3">{wo.scheduled_date ? new Date(wo.scheduled_date).toLocaleDateString() : new Date(wo.created_at).toLocaleDateString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Schedules Tab */}
      {activeTab === 'schedules' && (
        <div className="bg-[#151b28] rounded-lg shadow overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-slate-800 text-left text-xs font-medium text-slate-400 uppercase">
              <tr>
                <th className="px-4 py-3">Work Center</th>
                <th className="px-4 py-3">Type</th>
                <th className="px-4 py-3">Frequency</th>
                <th className="px-4 py-3">Description</th>
                <th className="px-4 py-3">Est. Duration</th>
                <th className="px-4 py-3">Last Performed</th>
                <th className="px-4 py-3">Next Due</th>
                <th className="px-4 py-3">Active</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-700">
              {schedules.length === 0 ? (
                <tr><td colSpan={8} className="px-4 py-12 text-center text-slate-400">No schedules configured</td></tr>
              ) : schedules.map(s => (
                <tr key={s.id} className="hover:bg-slate-800">
                  <td className="px-4 py-3 font-medium">{s.work_center_name || `WC #${s.work_center_id}`}</td>
                  <td className="px-4 py-3 capitalize">{s.maintenance_type}</td>
                  <td className="px-4 py-3 capitalize">{s.frequency}{s.frequency_value ? ` (${s.frequency_value})` : ''}</td>
                  <td className="px-4 py-3 max-w-xs truncate">{s.description}</td>
                  <td className="px-4 py-3">{s.estimated_duration_hours}h</td>
                  <td className="px-4 py-3">{s.last_performed_at ? new Date(s.last_performed_at).toLocaleDateString() : '-'}</td>
                  <td className="px-4 py-3">
                    {s.next_due_date ? (
                      <span className={new Date(s.next_due_date) < new Date() ? 'text-red-600 font-medium' : ''}>
                        {new Date(s.next_due_date).toLocaleDateString()}
                      </span>
                    ) : '-'}
                  </td>
                  <td className="px-4 py-3">
                    {s.is_active ? <CheckCircleIcon className="w-5 h-5 text-green-500" /> : <XMarkIcon className="w-5 h-5 text-slate-400" />}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Work Orders Tab */}
      {activeTab === 'work_orders' && (
        <div className="space-y-4">
          <div className="flex gap-3 flex-wrap">
            <div className="relative flex-1 min-w-[200px]">
              <MagnifyingGlassIcon className="absolute left-3 top-2.5 w-5 h-5 text-slate-400" />
              <input type="text" placeholder="Search..." value={search} onChange={e => setSearch(e.target.value)}
                className="w-full pl-10 pr-4 py-2 border border-slate-600 rounded-lg focus:ring-2 focus:ring-blue-500" />
            </div>
            <select value={statusFilter} onChange={e => setStatusFilter(e.target.value)} className="px-3 py-2 border border-slate-600 rounded-lg">
              <option value="">All Statuses</option>
              <option value="open">Open</option>
              <option value="in_progress">In Progress</option>
              <option value="completed">Completed</option>
              <option value="cancelled">Cancelled</option>
            </select>
          </div>

          <div className="bg-[#151b28] rounded-lg shadow overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-slate-800 text-left text-xs font-medium text-slate-400 uppercase">
                <tr>
                  <th className="px-4 py-3">Title</th>
                  <th className="px-4 py-3">Work Center</th>
                  <th className="px-4 py-3">Type</th>
                  <th className="px-4 py-3">Priority</th>
                  <th className="px-4 py-3">Status</th>
                  <th className="px-4 py-3">Scheduled</th>
                  <th className="px-4 py-3">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-700">
                {filteredWOs.length === 0 ? (
                  <tr><td colSpan={7} className="px-4 py-12 text-center text-slate-400">No work orders found</td></tr>
                ) : filteredWOs.map(wo => (
                  <tr key={wo.id} className="hover:bg-slate-800">
                    <td className="px-4 py-3 font-medium">{wo.title}</td>
                    <td className="px-4 py-3">{wo.work_center_name || '-'}</td>
                    <td className="px-4 py-3 capitalize">{wo.maintenance_type}</td>
                    <td className="px-4 py-3">
                      <span className={`px-2 py-1 rounded-full text-xs font-medium ${priorityColors[wo.priority]?.bg || ''} ${priorityColors[wo.priority]?.text || ''}`}>
                        {wo.priority}
                      </span>
                    </td>
                    <td className="px-4 py-3">
                      <span className={`px-2 py-1 rounded-full text-xs font-medium ${statusColors[wo.status]?.bg || ''} ${statusColors[wo.status]?.text || ''}`}>
                        {wo.status?.replace(/_/g, ' ')}
                      </span>
                    </td>
                    <td className="px-4 py-3">{wo.scheduled_date ? new Date(wo.scheduled_date).toLocaleDateString() : '-'}</td>
                    <td className="px-4 py-3">
                      <div className="flex gap-1">
                        {wo.status === 'open' && (
                          <button onClick={() => handleStart(wo)} className="text-xs px-2 py-1 bg-blue-500/100 text-white rounded hover:bg-blue-600" title="Start">
                            <PlayIcon className="w-4 h-4" />
                          </button>
                        )}
                        {wo.status === 'in_progress' && (
                          <button onClick={() => { setSelectedWO(wo); setShowCompleteModal(true); }} className="text-xs px-2 py-1 bg-green-500/100 text-white rounded hover:bg-green-600" title="Complete">
                            <CheckCircleIcon className="w-4 h-4" />
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Create Schedule Modal */}
      {showCreateScheduleModal && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-[#151b28] rounded-lg shadow-xl w-full max-w-lg mx-4 max-h-[90vh] overflow-y-auto">
            <div className="flex justify-between items-center p-4 border-b">
              <h3 className="text-lg font-semibold">New Maintenance Schedule</h3>
              <button onClick={() => setShowCreateScheduleModal(false)}><XMarkIcon className="w-5 h-5" /></button>
            </div>
            <div className="p-4 space-y-3">
              <div>
                <label className="block text-sm font-medium text-slate-300 mb-1">Work Center ID *</label>
                <input type="number" value={scheduleForm.work_center_id} onChange={e => setScheduleForm(f => ({ ...f, work_center_id: e.target.value }))} className="w-full px-3 py-2 border rounded-lg" />
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-sm font-medium text-slate-300 mb-1">Type</label>
                  <select value={scheduleForm.maintenance_type} onChange={e => setScheduleForm(f => ({ ...f, maintenance_type: e.target.value }))} className="w-full px-3 py-2 border rounded-lg">
                    <option value="preventive">Preventive</option>
                    <option value="predictive">Predictive</option>
                    <option value="calibration">Calibration</option>
                    <option value="inspection">Inspection</option>
                    <option value="lubrication">Lubrication</option>
                  </select>
                </div>
                <div>
                  <label className="block text-sm font-medium text-slate-300 mb-1">Frequency</label>
                  <select value={scheduleForm.frequency} onChange={e => setScheduleForm(f => ({ ...f, frequency: e.target.value }))} className="w-full px-3 py-2 border rounded-lg">
                    <option value="daily">Daily</option>
                    <option value="weekly">Weekly</option>
                    <option value="biweekly">Bi-weekly</option>
                    <option value="monthly">Monthly</option>
                    <option value="quarterly">Quarterly</option>
                    <option value="semi_annual">Semi-Annual</option>
                    <option value="annual">Annual</option>
                    <option value="usage_based">Usage Based</option>
                  </select>
                </div>
              </div>
              <div>
                <label className="block text-sm font-medium text-slate-300 mb-1">Description *</label>
                <textarea value={scheduleForm.description} onChange={e => setScheduleForm(f => ({ ...f, description: e.target.value }))} rows={3} className="w-full px-3 py-2 border rounded-lg" />
              </div>
              <div>
                <label className="block text-sm font-medium text-slate-300 mb-1">Checklist</label>
                <textarea value={scheduleForm.checklist} onChange={e => setScheduleForm(f => ({ ...f, checklist: e.target.value }))} rows={3} className="w-full px-3 py-2 border rounded-lg" placeholder="One item per line" />
              </div>
              <div>
                <label className="block text-sm font-medium text-slate-300 mb-1">Est. Duration (hours)</label>
                <input type="number" step="0.5" value={scheduleForm.estimated_duration_hours} onChange={e => setScheduleForm(f => ({ ...f, estimated_duration_hours: e.target.value }))} className="w-full px-3 py-2 border rounded-lg" />
              </div>
            </div>
            <div className="flex justify-end gap-2 p-4 border-t">
              <button onClick={() => setShowCreateScheduleModal(false)} className="px-4 py-2 border rounded-lg">Cancel</button>
              <button onClick={handleCreateSchedule} disabled={!scheduleForm.work_center_id || !scheduleForm.description}
                className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50">Create</button>
            </div>
          </div>
        </div>
      )}

      {/* Create Work Order Modal */}
      {showCreateWOModal && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-[#151b28] rounded-lg shadow-xl w-full max-w-lg mx-4">
            <div className="flex justify-between items-center p-4 border-b">
              <h3 className="text-lg font-semibold">New Maintenance Work Order</h3>
              <button onClick={() => setShowCreateWOModal(false)}><XMarkIcon className="w-5 h-5" /></button>
            </div>
            <div className="p-4 space-y-3">
              <div>
                <label className="block text-sm font-medium text-slate-300 mb-1">Title *</label>
                <input type="text" value={woForm.title} onChange={e => setWoForm(f => ({ ...f, title: e.target.value }))} className="w-full px-3 py-2 border rounded-lg" />
              </div>
              <div>
                <label className="block text-sm font-medium text-slate-300 mb-1">Work Center ID *</label>
                <input type="number" value={woForm.work_center_id} onChange={e => setWoForm(f => ({ ...f, work_center_id: e.target.value }))} className="w-full px-3 py-2 border rounded-lg" />
              </div>
              <div className="grid grid-cols-3 gap-3">
                <div>
                  <label className="block text-sm font-medium text-slate-300 mb-1">Type</label>
                  <select value={woForm.maintenance_type} onChange={e => setWoForm(f => ({ ...f, maintenance_type: e.target.value }))} className="w-full px-3 py-2 border rounded-lg">
                    <option value="preventive">Preventive</option>
                    <option value="corrective">Corrective</option>
                    <option value="predictive">Predictive</option>
                    <option value="emergency">Emergency</option>
                  </select>
                </div>
                <div>
                  <label className="block text-sm font-medium text-slate-300 mb-1">Priority</label>
                  <select value={woForm.priority} onChange={e => setWoForm(f => ({ ...f, priority: e.target.value }))} className="w-full px-3 py-2 border rounded-lg">
                    <option value="low">Low</option>
                    <option value="medium">Medium</option>
                    <option value="high">High</option>
                    <option value="critical">Critical</option>
                    <option value="emergency">Emergency</option>
                  </select>
                </div>
                <div>
                  <label className="block text-sm font-medium text-slate-300 mb-1">Scheduled Date</label>
                  <input type="date" value={woForm.scheduled_date} onChange={e => setWoForm(f => ({ ...f, scheduled_date: e.target.value }))} className="w-full px-3 py-2 border rounded-lg" />
                </div>
              </div>
              <div>
                <label className="block text-sm font-medium text-slate-300 mb-1">Description</label>
                <textarea value={woForm.description} onChange={e => setWoForm(f => ({ ...f, description: e.target.value }))} rows={3} className="w-full px-3 py-2 border rounded-lg" />
              </div>
            </div>
            <div className="flex justify-end gap-2 p-4 border-t">
              <button onClick={() => setShowCreateWOModal(false)} className="px-4 py-2 border rounded-lg">Cancel</button>
              <button onClick={handleCreateWO} disabled={!woForm.title || !woForm.work_center_id}
                className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50">Create</button>
            </div>
          </div>
        </div>
      )}

      {/* Complete Work Order Modal */}
      {showCompleteModal && selectedWO && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-[#151b28] rounded-lg shadow-xl w-full max-w-md mx-4">
            <div className="flex justify-between items-center p-4 border-b">
              <h3 className="text-lg font-semibold">Complete: {selectedWO.title}</h3>
              <button onClick={() => setShowCompleteModal(false)}><XMarkIcon className="w-5 h-5" /></button>
            </div>
            <div className="p-4 space-y-3">
              <div>
                <label className="block text-sm font-medium text-slate-300 mb-1">Completion Notes</label>
                <textarea value={completeForm.notes} onChange={e => setCompleteForm(f => ({ ...f, notes: e.target.value }))} rows={3} className="w-full px-3 py-2 border rounded-lg" />
              </div>
              <div>
                <label className="block text-sm font-medium text-slate-300 mb-1">Parts Used</label>
                <input type="text" value={completeForm.parts_used} onChange={e => setCompleteForm(f => ({ ...f, parts_used: e.target.value }))} className="w-full px-3 py-2 border rounded-lg" />
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-sm font-medium text-slate-300 mb-1">Labor Cost ($)</label>
                  <input type="number" step="0.01" value={completeForm.labor_cost} onChange={e => setCompleteForm(f => ({ ...f, labor_cost: e.target.value }))} className="w-full px-3 py-2 border rounded-lg" />
                </div>
                <div>
                  <label className="block text-sm font-medium text-slate-300 mb-1">Parts Cost ($)</label>
                  <input type="number" step="0.01" value={completeForm.parts_cost} onChange={e => setCompleteForm(f => ({ ...f, parts_cost: e.target.value }))} className="w-full px-3 py-2 border rounded-lg" />
                </div>
              </div>
            </div>
            <div className="flex justify-end gap-2 p-4 border-t">
              <button onClick={() => setShowCompleteModal(false)} className="px-4 py-2 border rounded-lg">Cancel</button>
              <button onClick={handleComplete} className="px-4 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700">Complete</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
