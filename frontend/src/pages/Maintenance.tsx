import React, { useState, useEffect, useMemo, useCallback } from 'react';
import api from '../services/api';
import { Modal } from '../components/ui/Modal';
import {
  EmptyState,
  ErrorState,
  useToast,
  DataTable,
  DataTableColumn,
  StatusBadge,
  MobileDataCard,
} from '../components/ui';
import { MiniStat, MiniStatStrip } from '../components/cockpit';
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

// StatusBadge colorMap form (single class string per key) derived from the
// {bg,text} maps above, so the badge palette stays identical after migration.
const statusBadgeColors: Record<string, string> = Object.fromEntries(
  Object.entries(statusColors).map(([k, v]) => [k, `${v.bg} ${v.text}`]),
);
const priorityBadgeColors: Record<string, string> = Object.fromEntries(
  Object.entries(priorityColors).map(([k, v]) => [k, `${v.bg} ${v.text}`]),
);

export default function Maintenance() {
  const { showToast } = useToast();
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
    } catch (err: any) { showToast('error', err.response?.data?.detail || 'Failed to create schedule'); }
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
    } catch (err: any) { showToast('error', err.response?.data?.detail || 'Failed to create work order'); }
  };

  const handleStart = async (wo: MaintenanceWorkOrder) => {
    try {
      await api.startMaintenanceWorkOrder(wo.id);
      loadData();
    } catch (err: any) { showToast('error', err.response?.data?.detail || 'Failed to start'); }
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
    } catch (err: any) { showToast('error', err.response?.data?.detail || 'Failed to complete'); }
  };

  const woRowActions = useCallback((wo: MaintenanceWorkOrder) => (
    <div className="flex gap-1">
      {wo.status === 'open' && (
        <button onClick={(e) => { e.stopPropagation(); handleStart(wo); }} className="text-xs px-2 py-1 bg-blue-500/100 text-white rounded hover:bg-blue-600" title="Start">
          <PlayIcon className="w-4 h-4" />
        </button>
      )}
      {wo.status === 'in_progress' && (
        <button onClick={(e) => { e.stopPropagation(); setSelectedWO(wo); setShowCompleteModal(true); }} className="text-xs px-2 py-1 bg-green-500/100 text-white rounded hover:bg-green-600" title="Complete">
          <CheckCircleIcon className="w-4 h-4" />
        </button>
      )}
    </div>
  ), [handleStart]);

  // ---- Work Orders tab columns ----
  const woColumns = useMemo<Array<DataTableColumn<MaintenanceWorkOrder>>>(() => [
    {
      key: 'title',
      header: 'Title',
      sortable: true,
      className: 'font-medium',
      accessor: (wo) => wo.title,
    },
    {
      key: 'work_center',
      header: 'Work Center',
      sortable: true,
      accessor: (wo) => wo.work_center_name || '',
      render: (wo) => wo.work_center_name || '-',
    },
    {
      key: 'type',
      header: 'Type',
      sortable: true,
      className: 'capitalize',
      accessor: (wo) => wo.maintenance_type,
    },
    {
      key: 'priority',
      header: 'Priority',
      sortable: true,
      accessor: (wo) => wo.priority,
      render: (wo) => <StatusBadge status={wo.priority} colorMap={priorityBadgeColors} />,
    },
    {
      key: 'status',
      header: 'Status',
      sortable: true,
      accessor: (wo) => wo.status,
      render: (wo) => <StatusBadge status={wo.status} colorMap={statusBadgeColors} />,
    },
    {
      key: 'scheduled',
      header: 'Scheduled',
      sortable: true,
      accessor: (wo) => wo.scheduled_date || '',
      csv: (wo) => (wo.scheduled_date ? new Date(wo.scheduled_date).toLocaleDateString() : ''),
      render: (wo) => (wo.scheduled_date ? new Date(wo.scheduled_date).toLocaleDateString() : '-'),
    },
    {
      key: 'actions',
      header: 'Actions',
      render: woRowActions,
    },
  ], [woRowActions]);

  const renderWOCard = useCallback((wo: MaintenanceWorkOrder) => (
    <MobileDataCard
      title={wo.title}
      subtitle={wo.work_center_name || undefined}
      badge={<StatusBadge status={wo.status} colorMap={statusBadgeColors} />}
      fields={[
        { label: 'Type', value: <span className="capitalize">{wo.maintenance_type}</span> },
        { label: 'Priority', value: <StatusBadge status={wo.priority} colorMap={priorityBadgeColors} /> },
        { label: 'Scheduled', value: wo.scheduled_date ? new Date(wo.scheduled_date).toLocaleDateString() : '-' },
      ]}
      actions={
        (wo.status === 'open' || wo.status === 'in_progress') ? woRowActions(wo) : undefined
      }
    />
  ), [woRowActions]);

  // ---- Schedules tab columns ----
  const scheduleColumns = useMemo<Array<DataTableColumn<MaintenanceSchedule>>>(() => [
    {
      key: 'work_center',
      header: 'Work Center',
      sortable: true,
      className: 'font-medium',
      accessor: (s) => s.work_center_name || `WC #${s.work_center_id}`,
    },
    {
      key: 'type',
      header: 'Type',
      sortable: true,
      className: 'capitalize',
      accessor: (s) => s.maintenance_type,
    },
    {
      key: 'frequency',
      header: 'Frequency',
      sortable: true,
      className: 'capitalize',
      accessor: (s) => s.frequency,
      csv: (s) => `${s.frequency}${s.frequency_value ? ` (${s.frequency_value})` : ''}`,
      render: (s) => `${s.frequency}${s.frequency_value ? ` (${s.frequency_value})` : ''}`,
    },
    {
      key: 'description',
      header: 'Description',
      accessor: (s) => s.description,
      className: 'max-w-xs truncate',
      render: (s) => <span className="block max-w-xs truncate">{s.description}</span>,
    },
    {
      key: 'duration',
      header: 'Est. Duration',
      sortable: true,
      align: 'right',
      accessor: (s) => s.estimated_duration_hours,
      csv: (s) => `${s.estimated_duration_hours}h`,
      render: (s) => `${s.estimated_duration_hours}h`,
    },
    {
      key: 'last_performed',
      header: 'Last Performed',
      sortable: true,
      accessor: (s) => s.last_performed_at || '',
      csv: (s) => (s.last_performed_at ? new Date(s.last_performed_at).toLocaleDateString() : ''),
      render: (s) => (s.last_performed_at ? new Date(s.last_performed_at).toLocaleDateString() : '-'),
    },
    {
      key: 'next_due',
      header: 'Next Due',
      sortable: true,
      accessor: (s) => s.next_due_date || '',
      csv: (s) => (s.next_due_date ? new Date(s.next_due_date).toLocaleDateString() : ''),
      render: (s) =>
        s.next_due_date ? (
          <span className={new Date(s.next_due_date) < new Date() ? 'text-red-600 font-medium' : ''}>
            {new Date(s.next_due_date).toLocaleDateString()}
          </span>
        ) : (
          '-'
        ),
    },
    {
      key: 'active',
      header: 'Active',
      sortable: true,
      accessor: (s) => (s.is_active ? 1 : 0),
      csv: (s) => (s.is_active ? 'Yes' : 'No'),
      render: (s) =>
        s.is_active ? (
          <CheckCircleIcon className="w-5 h-5 text-green-500" />
        ) : (
          <XMarkIcon className="w-5 h-5 text-slate-400" />
        ),
    },
  ], []);

  const renderScheduleCard = useCallback((s: MaintenanceSchedule) => (
    <MobileDataCard
      title={s.work_center_name || `WC #${s.work_center_id}`}
      subtitle={s.description || undefined}
      badge={
        <StatusBadge
          status={s.is_active ? 'active' : 'inactive'}
          colorMap={{ active: 'bg-green-500/20 text-green-300', inactive: 'bg-slate-800/50 text-slate-400' }}
        />
      }
      fields={[
        { label: 'Type', value: <span className="capitalize">{s.maintenance_type}</span> },
        { label: 'Frequency', value: <span className="capitalize">{s.frequency}{s.frequency_value ? ` (${s.frequency_value})` : ''}</span> },
        { label: 'Est. Duration', value: `${s.estimated_duration_hours}h` },
        {
          label: 'Next Due',
          value: s.next_due_date ? (
            <span className={new Date(s.next_due_date) < new Date() ? 'text-red-600 font-medium' : ''}>
              {new Date(s.next_due_date).toLocaleDateString()}
            </span>
          ) : (
            '-'
          ),
        },
      ]}
    />
  ), []);

  if (loading) {
    return <div className="p-6"><div className="animate-pulse space-y-4"><div className="h-8 bg-gray-200 rounded w-1/4" /><div className="grid grid-cols-4 gap-4">{[...Array(4)].map((_, i) => <div key={i} className="h-24 bg-gray-200 rounded" />)}</div><div className="h-64 bg-gray-200 rounded" /></div></div>;
  }

  if (error) {
    return (
      <div className="p-6">
        <ErrorState message={error} onRetry={loadData} />
      </div>
    );
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

      {/* KPI strip — compact instrument-panel tiles */}
      <MiniStatStrip className="grid grid-cols-2 lg:grid-cols-4 gap-2">
        <MiniStat
          icon={CalendarDaysIcon}
          iconBg="bg-blue-500/20"
          iconColor="text-blue-600"
          label="Scheduled This Week"
          value={dashboard?.scheduled_this_week || 0}
        />
        <MiniStat
          icon={ExclamationTriangleIcon}
          iconBg={dashboard?.overdue ? 'bg-red-500/20' : 'bg-fd-green/15'}
          iconColor={dashboard?.overdue ? 'text-red-600' : 'text-fd-green'}
          label="Overdue"
          value={dashboard?.overdue || 0}
          valueColor={dashboard?.overdue ? 'text-red-600' : undefined}
        />
        <MiniStat
          icon={CheckCircleIcon}
          iconBg="bg-fd-green/15"
          iconColor="text-fd-green"
          label="Completed This Month"
          value={dashboard?.completed_this_month || 0}
          valueColor="text-green-600"
        />
        <MiniStat
          icon={PlayIcon}
          iconBg="bg-orange-500/20"
          iconColor="text-orange-600"
          label="Open Work Orders"
          value={dashboard?.open_work_orders || 0}
          valueColor="text-orange-600"
        />
      </MiniStatStrip>

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
            <EmptyState
              icon={CalendarDaysIcon}
              title="No upcoming maintenance"
              description="Scheduled and upcoming maintenance will appear here."
            />
          )}

          <h3 className="text-lg font-semibold mt-6">Recent Work Orders</h3>
          <div className="bg-[#151b28] rounded-lg shadow overflow-x-auto">
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
        <DataTable
          columns={scheduleColumns}
          data={schedules}
          rowKey={(s) => s.id}
          defaultSort={{ key: 'next_due', dir: 'asc' }}
          pageSize={25}
          csvExport={{ filename: 'maintenance-schedules' }}
          mobileCards={renderScheduleCard}
          empty={{
            icon: CalendarDaysIcon,
            title: 'No schedules configured',
            description: 'Create a maintenance schedule to track recurring upkeep.',
            action: { label: 'New Schedule', onClick: () => setShowCreateScheduleModal(true) },
          }}
        />
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

          <DataTable
            columns={woColumns}
            data={filteredWOs}
            rowKey={(wo) => wo.id}
            defaultSort={{ key: 'scheduled', dir: 'desc' }}
            pageSize={25}
            csvExport={{ filename: 'maintenance-work-orders' }}
            mobileCards={renderWOCard}
            empty={{
              icon: PlusIcon,
              title: 'No work orders found',
              description:
                search || statusFilter
                  ? 'No maintenance work orders match your search or filter.'
                  : 'Create a maintenance work order to get started.',
              action:
                search || statusFilter
                  ? undefined
                  : { label: 'New Work Order', onClick: () => setShowCreateWOModal(true) },
            }}
          />
        </div>
      )}

      {/* Create Schedule Modal */}
      <Modal open={showCreateScheduleModal} onClose={() => setShowCreateScheduleModal(false)} size="lg" scroll={false} padded={false} closeOnBackdrop={false}>
            <div className="flex justify-between items-center p-4 border-b">
              <h3 className="text-lg font-semibold">New Maintenance Schedule</h3>
              <button onClick={() => setShowCreateScheduleModal(false)}><XMarkIcon className="w-5 h-5" /></button>
            </div>
            <div className="flex-1 min-h-0 overflow-y-auto p-4 space-y-3">
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
      </Modal>

      {/* Create Work Order Modal */}
      <Modal open={showCreateWOModal} onClose={() => setShowCreateWOModal(false)} size="lg" scroll={false} padded={false} closeOnBackdrop={false}>
            <div className="flex justify-between items-center p-4 border-b">
              <h3 className="text-lg font-semibold">New Maintenance Work Order</h3>
              <button onClick={() => setShowCreateWOModal(false)}><XMarkIcon className="w-5 h-5" /></button>
            </div>
            <div className="flex-1 min-h-0 overflow-y-auto p-4 space-y-3">
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
      </Modal>

      {/* Complete Work Order Modal */}
      <Modal open={showCompleteModal && !!selectedWO} onClose={() => setShowCompleteModal(false)} size="md" scroll={false} padded={false} closeOnBackdrop={false}>
            <div className="flex justify-between items-center p-4 border-b">
              <h3 className="text-lg font-semibold">Complete: {selectedWO?.title}</h3>
              <button onClick={() => setShowCompleteModal(false)}><XMarkIcon className="w-5 h-5" /></button>
            </div>
            <div className="flex-1 min-h-0 overflow-y-auto p-4 space-y-3">
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
      </Modal>
    </div>
  );
}
