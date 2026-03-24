import React, { useEffect, useState, useCallback } from 'react';
import api from '../services/api';
import {
  CurrencyDollarIcon,
  ChartBarIcon,
  ExclamationTriangleIcon,
  CheckCircleIcon,
  PlusIcon,
  TrashIcon,
  CalculatorIcon,
  ChevronDownIcon,
  ChevronUpIcon,
  XMarkIcon,
  DocumentChartBarIcon,
  ArrowPathIcon,
} from '@heroicons/react/24/outline';
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
} from 'recharts';

// ── Types ────────────────────────────────────────────────────────

interface JobCostRecord {
  id: number;
  work_order_id: number;
  estimated_material_cost: number;
  estimated_labor_cost: number;
  estimated_overhead_cost: number;
  estimated_total_cost: number;
  actual_material_cost: number;
  actual_labor_cost: number;
  actual_overhead_cost: number;
  actual_total_cost: number;
  material_variance: number;
  labor_variance: number;
  overhead_variance: number;
  total_variance: number;
  margin_amount: number;
  margin_percent: number;
  revenue: number;
  status: string;
  notes?: string;
  created_at: string;
  updated_at: string;
  work_order_number?: string;
  part_number?: string;
  part_name?: string;
  customer_name?: string;
}

interface CostEntry {
  id: number;
  job_cost_id: number;
  entry_type: string;
  description: string;
  quantity: number;
  unit_cost: number;
  total_cost: number;
  work_order_operation_id?: number;
  source: string;
  reference?: string;
  entry_date: string;
  created_by?: number;
  created_at: string;
}

interface Summary {
  total_wip_value: number;
  average_margin_percent: number;
  jobs_over_budget: number;
  jobs_completed_this_month: number;
  total_jobs: number;
  in_progress_count: number;
  completed_count: number;
  total_actual_cost: number;
  total_estimated_cost: number;
}

interface WorkOrderOption {
  id: number;
  work_order_number: string;
  customer_name?: string;
}

// ── Helpers ──────────────────────────────────────────────────────

const fmt = (n: number) =>
  new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(n);

const pct = (n: number) => `${n >= 0 ? '+' : ''}${n.toFixed(1)}%`;

const varianceColor = (v: number) =>
  v > 0 ? 'text-red-600' : v < 0 ? 'text-green-600' : 'text-gray-600';

const varianceBg = (v: number) =>
  v > 0 ? 'bg-red-50' : v < 0 ? 'bg-green-50' : 'bg-gray-50';

const statusBadge: Record<string, string> = {
  in_progress: 'du-badge du-badge-warning du-badge-sm',
  completed: 'du-badge du-badge-success du-badge-sm',
  reviewed: 'du-badge du-badge-info du-badge-sm',
};

const statusLabel: Record<string, string> = {
  in_progress: 'In Progress',
  completed: 'Completed',
  reviewed: 'Reviewed',
};

const entryTypeLabel: Record<string, string> = {
  material: 'Material',
  labor: 'Labor',
  overhead: 'Overhead',
  other: 'Other',
};

const todayISO = () => new Date().toISOString().split('T')[0];

// ── Component ────────────────────────────────────────────────────

export default function JobCosting() {
  // Data
  const [jobCosts, setJobCosts] = useState<JobCostRecord[]>([]);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [loading, setLoading] = useState(true);

  // Filters
  const [activeTab, setActiveTab] = useState<'active' | 'completed'>('active');
  const [searchTerm, setSearchTerm] = useState('');
  const [statusFilter, setStatusFilter] = useState('');

  // Expanded row + entries
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const [entries, setEntries] = useState<CostEntry[]>([]);
  const [entriesLoading, setEntriesLoading] = useState(false);

  // Create modal
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [workOrders, setWorkOrders] = useState<WorkOrderOption[]>([]);
  const [createForm, setCreateForm] = useState({
    work_order_id: 0,
    estimated_material_cost: 0,
    estimated_labor_cost: 0,
    estimated_overhead_cost: 0,
    revenue: 0,
    notes: '',
  });

  // Add entry modal
  const [showEntryModal, setShowEntryModal] = useState(false);
  const [entryJobCostId, setEntryJobCostId] = useState<number | null>(null);
  const [entryForm, setEntryForm] = useState({
    entry_type: 'material',
    description: '',
    quantity: 1,
    unit_cost: 0,
    source: 'manual',
    reference: '',
    entry_date: todayISO(),
  });

  // ── Data fetching ──────────────────────────────────────────────

  const loadJobCosts = useCallback(async () => {
    try {
      setLoading(true);
      const params: Record<string, string> = {};
      if (statusFilter) params.status = statusFilter;
      const response = await api.get<JobCostRecord[]>('/job-costs/', { params });
      setJobCosts(response.data);
    } catch (err) {
      console.error('Failed to load job costs:', err);
    } finally {
      setLoading(false);
    }
  }, [statusFilter]);

  const loadSummary = useCallback(async () => {
    try {
      const response = await api.get<Summary>('/job-costs/summary');
      setSummary(response.data);
    } catch (err) {
      console.error('Failed to load summary:', err);
    }
  }, []);

  const loadEntries = useCallback(async (jobCostId: number) => {
    try {
      setEntriesLoading(true);
      const response = await api.get<CostEntry[]>(`/job-costs/${jobCostId}/entries`);
      setEntries(response.data);
    } catch (err) {
      console.error('Failed to load entries:', err);
    } finally {
      setEntriesLoading(false);
    }
  }, []);

  const loadWorkOrders = useCallback(async () => {
    try {
      const response = await api.get<WorkOrderOption[]>('/work-orders/', {
        params: { limit: 500 },
      });
      setWorkOrders(
        (response.data as any[]).map((wo: any) => ({
          id: wo.id,
          work_order_number: wo.work_order_number,
          customer_name: wo.customer_name,
        }))
      );
    } catch (err) {
      console.error('Failed to load work orders:', err);
    }
  }, []);

  useEffect(() => {
    loadJobCosts();
    loadSummary();
  }, [loadJobCosts, loadSummary]);

  // ── Filtered data ──────────────────────────────────────────────

  const filtered = jobCosts.filter((jc) => {
    // Tab filter
    if (activeTab === 'active' && (jc.status === 'completed' || jc.status === 'reviewed')) {
      return false;
    }
    if (activeTab === 'completed' && jc.status === 'in_progress') {
      return false;
    }

    // Search
    if (searchTerm) {
      const term = searchTerm.toLowerCase();
      const match =
        (jc.work_order_number || '').toLowerCase().includes(term) ||
        (jc.part_number || '').toLowerCase().includes(term) ||
        (jc.part_name || '').toLowerCase().includes(term) ||
        (jc.customer_name || '').toLowerCase().includes(term);
      if (!match) return false;
    }
    return true;
  });

  // ── Variance chart data ────────────────────────────────────────

  const chartData = filtered.slice(0, 15).map((jc) => ({
    name: jc.work_order_number || `WO-${jc.work_order_id}`,
    Estimated: jc.estimated_total_cost,
    Actual: jc.actual_total_cost,
    Variance: jc.total_variance,
  }));

  // ── Row expand ─────────────────────────────────────────────────

  const toggleExpand = (id: number) => {
    if (expandedId === id) {
      setExpandedId(null);
      setEntries([]);
    } else {
      setExpandedId(id);
      loadEntries(id);
    }
  };

  // ── Create job cost ────────────────────────────────────────────

  const openCreateModal = () => {
    loadWorkOrders();
    setCreateForm({
      work_order_id: 0,
      estimated_material_cost: 0,
      estimated_labor_cost: 0,
      estimated_overhead_cost: 0,
      revenue: 0,
      notes: '',
    });
    setShowCreateModal(true);
  };

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!createForm.work_order_id) {
      alert('Please select a work order');
      return;
    }
    try {
      await api.get('/job-costs/').then(() => {}); // warm up
      const response = await fetch(
        `${process.env.REACT_APP_API_URL || 'http://localhost:8000/api/v1'}/job-costs/`,
        {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${localStorage.getItem('token')}`,
          },
          body: JSON.stringify(createForm),
        }
      );
      if (!response.ok) {
        const err = await response.json();
        throw new Error(err.detail || 'Failed to create job cost');
      }
      setShowCreateModal(false);
      loadJobCosts();
      loadSummary();
    } catch (err: any) {
      alert(err.message || 'Failed to create job cost');
    }
  };

  // ── Add cost entry ─────────────────────────────────────────────

  const openEntryModal = (jobCostId: number) => {
    setEntryJobCostId(jobCostId);
    setEntryForm({
      entry_type: 'material',
      description: '',
      quantity: 1,
      unit_cost: 0,
      source: 'manual',
      reference: '',
      entry_date: todayISO(),
    });
    setShowEntryModal(true);
  };

  const handleAddEntry = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!entryJobCostId) return;
    try {
      const response = await fetch(
        `${process.env.REACT_APP_API_URL || 'http://localhost:8000/api/v1'}/job-costs/${entryJobCostId}/entries`,
        {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${localStorage.getItem('token')}`,
          },
          body: JSON.stringify(entryForm),
        }
      );
      if (!response.ok) {
        const err = await response.json();
        throw new Error(err.detail || 'Failed to add entry');
      }
      setShowEntryModal(false);
      loadEntries(entryJobCostId);
      loadJobCosts();
      loadSummary();
    } catch (err: any) {
      alert(err.message || 'Failed to add entry');
    }
  };

  // ── Delete entry ───────────────────────────────────────────────

  const handleDeleteEntry = async (jobCostId: number, entryId: number) => {
    if (!window.confirm('Are you sure you want to delete this cost entry?')) return;
    try {
      const response = await fetch(
        `${process.env.REACT_APP_API_URL || 'http://localhost:8000/api/v1'}/job-costs/${jobCostId}/entries/${entryId}`,
        {
          method: 'DELETE',
          headers: {
            Authorization: `Bearer ${localStorage.getItem('token')}`,
          },
        }
      );
      if (!response.ok) throw new Error('Failed to delete entry');
      loadEntries(jobCostId);
      loadJobCosts();
      loadSummary();
    } catch (err: any) {
      alert(err.message || 'Failed to delete entry');
    }
  };

  // ── Recalculate ────────────────────────────────────────────────

  const handleRecalculate = async (jobCostId: number) => {
    try {
      const response = await fetch(
        `${process.env.REACT_APP_API_URL || 'http://localhost:8000/api/v1'}/job-costs/${jobCostId}/calculate`,
        {
          method: 'POST',
          headers: {
            Authorization: `Bearer ${localStorage.getItem('token')}`,
          },
        }
      );
      if (!response.ok) throw new Error('Failed to recalculate');
      loadJobCosts();
      loadSummary();
      if (expandedId === jobCostId) {
        loadEntries(jobCostId);
      }
    } catch (err: any) {
      alert(err.message || 'Failed to recalculate');
    }
  };

  // ── Render ─────────────────────────────────────────────────────

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div className="flex justify-between items-center">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Job Costing & Financial Integration</h1>
          <p className="text-sm text-gray-500 mt-1">
            Track estimated vs. actual costs, margins, and variances across work orders
          </p>
        </div>
        <button
          onClick={openCreateModal}
          className="du-btn du-btn-primary du-btn-sm gap-1"
        >
          <PlusIcon className="h-4 w-4" />
          New Job Cost
        </button>
      </div>

      {/* Summary Cards */}
      {summary && (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
          <div className="du-card bg-base-100 shadow-sm border">
            <div className="du-card-body p-4">
              <div className="flex items-center gap-3">
                <div className="p-2 rounded-lg bg-blue-100">
                  <CurrencyDollarIcon className="h-6 w-6 text-blue-600" />
                </div>
                <div>
                  <p className="text-xs text-gray-500 uppercase tracking-wide">Total WIP Value</p>
                  <p className="text-xl font-bold text-gray-900">{fmt(summary.total_wip_value)}</p>
                </div>
              </div>
            </div>
          </div>

          <div className="du-card bg-base-100 shadow-sm border">
            <div className="du-card-body p-4">
              <div className="flex items-center gap-3">
                <div className="p-2 rounded-lg bg-green-100">
                  <ChartBarIcon className="h-6 w-6 text-green-600" />
                </div>
                <div>
                  <p className="text-xs text-gray-500 uppercase tracking-wide">Average Margin</p>
                  <p className="text-xl font-bold text-gray-900">
                    {summary.average_margin_percent.toFixed(1)}%
                  </p>
                </div>
              </div>
            </div>
          </div>

          <div className="du-card bg-base-100 shadow-sm border">
            <div className="du-card-body p-4">
              <div className="flex items-center gap-3">
                <div className="p-2 rounded-lg bg-red-100">
                  <ExclamationTriangleIcon className="h-6 w-6 text-red-600" />
                </div>
                <div>
                  <p className="text-xs text-gray-500 uppercase tracking-wide">Over Budget</p>
                  <p className="text-xl font-bold text-gray-900">{summary.jobs_over_budget}</p>
                </div>
              </div>
            </div>
          </div>

          <div className="du-card bg-base-100 shadow-sm border">
            <div className="du-card-body p-4">
              <div className="flex items-center gap-3">
                <div className="p-2 rounded-lg bg-purple-100">
                  <CheckCircleIcon className="h-6 w-6 text-purple-600" />
                </div>
                <div>
                  <p className="text-xs text-gray-500 uppercase tracking-wide">
                    Completed This Month
                  </p>
                  <p className="text-xl font-bold text-gray-900">
                    {summary.jobs_completed_this_month}
                  </p>
                </div>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Variance Chart */}
      {chartData.length > 0 && (
        <div className="du-card bg-base-100 shadow-sm border">
          <div className="du-card-body p-4">
            <h2 className="text-lg font-semibold mb-4">Estimated vs Actual Cost</h2>
            <ResponsiveContainer width="100%" height={300}>
              <BarChart data={chartData}>
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis dataKey="name" fontSize={12} />
                <YAxis fontSize={12} tickFormatter={(v: number) => `$${(v / 1000).toFixed(0)}k`} />
                <Tooltip
                  formatter={(value: number) => fmt(value)}
                  labelStyle={{ fontWeight: 600 }}
                />
                <Legend />
                <Bar dataKey="Estimated" fill="#60a5fa" radius={[4, 4, 0, 0]} />
                <Bar dataKey="Actual" fill="#f97316" radius={[4, 4, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}

      {/* Tabs + Filters */}
      <div className="flex flex-col sm:flex-row sm:items-center gap-4">
        <div className="du-tabs du-tabs-boxed">
          <button
            className={`du-tab ${activeTab === 'active' ? 'du-tab-active' : ''}`}
            onClick={() => setActiveTab('active')}
          >
            Active Jobs
          </button>
          <button
            className={`du-tab ${activeTab === 'completed' ? 'du-tab-active' : ''}`}
            onClick={() => setActiveTab('completed')}
          >
            Completed Jobs
          </button>
        </div>

        <div className="flex-1" />

        <input
          type="text"
          placeholder="Search WO#, part, customer..."
          value={searchTerm}
          onChange={(e) => setSearchTerm(e.target.value)}
          className="du-input du-input-bordered du-input-sm w-64"
        />

        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          className="du-select du-select-bordered du-select-sm"
        >
          <option value="">All Statuses</option>
          <option value="in_progress">In Progress</option>
          <option value="completed">Completed</option>
          <option value="reviewed">Reviewed</option>
        </select>
      </div>

      {/* Job Costs Table */}
      <div className="du-card bg-base-100 shadow-sm border overflow-x-auto">
        <table className="du-table du-table-sm w-full">
          <thead>
            <tr>
              <th className="w-8"></th>
              <th>WO #</th>
              <th>Part</th>
              <th>Customer</th>
              <th className="text-right">Estimated</th>
              <th className="text-right">Actual</th>
              <th className="text-right">Variance</th>
              <th className="text-right">Margin %</th>
              <th>Status</th>
              <th className="w-24">Actions</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr>
                <td colSpan={10} className="text-center py-8 text-gray-500">
                  Loading...
                </td>
              </tr>
            ) : filtered.length === 0 ? (
              <tr>
                <td colSpan={10} className="text-center py-8 text-gray-500">
                  No job costs found. Click "New Job Cost" to create one.
                </td>
              </tr>
            ) : (
              filtered.map((jc) => (
                <React.Fragment key={jc.id}>
                  <tr
                    className={`cursor-pointer hover:bg-base-200 ${
                      expandedId === jc.id ? 'bg-base-200' : ''
                    }`}
                    onClick={() => toggleExpand(jc.id)}
                  >
                    <td>
                      {expandedId === jc.id ? (
                        <ChevronUpIcon className="h-4 w-4" />
                      ) : (
                        <ChevronDownIcon className="h-4 w-4" />
                      )}
                    </td>
                    <td className="font-medium">{jc.work_order_number || `WO-${jc.work_order_id}`}</td>
                    <td>
                      <div className="text-sm">{jc.part_number || '-'}</div>
                      {jc.part_name && (
                        <div className="text-xs text-gray-500">{jc.part_name}</div>
                      )}
                    </td>
                    <td>{jc.customer_name || '-'}</td>
                    <td className="text-right font-mono text-sm">
                      {fmt(jc.estimated_total_cost)}
                    </td>
                    <td className="text-right font-mono text-sm">{fmt(jc.actual_total_cost)}</td>
                    <td className={`text-right font-mono text-sm font-medium ${varianceColor(jc.total_variance)}`}>
                      {fmt(jc.total_variance)}
                    </td>
                    <td
                      className={`text-right font-mono text-sm font-medium ${
                        jc.margin_percent >= 20
                          ? 'text-green-600'
                          : jc.margin_percent >= 0
                          ? 'text-yellow-600'
                          : 'text-red-600'
                      }`}
                    >
                      {jc.revenue > 0 ? `${jc.margin_percent.toFixed(1)}%` : '-'}
                    </td>
                    <td>
                      <span className={statusBadge[jc.status] || 'du-badge du-badge-ghost du-badge-sm'}>
                        {statusLabel[jc.status] || jc.status}
                      </span>
                    </td>
                    <td onClick={(e) => e.stopPropagation()}>
                      <div className="flex gap-1">
                        <button
                          onClick={() => openEntryModal(jc.id)}
                          className="du-btn du-btn-ghost du-btn-xs"
                          title="Add Cost Entry"
                        >
                          <PlusIcon className="h-3.5 w-3.5" />
                        </button>
                        <button
                          onClick={() => handleRecalculate(jc.id)}
                          className="du-btn du-btn-ghost du-btn-xs"
                          title="Recalculate from Time Entries"
                        >
                          <ArrowPathIcon className="h-3.5 w-3.5" />
                        </button>
                      </div>
                    </td>
                  </tr>

                  {/* Expanded row with entries */}
                  {expandedId === jc.id && (
                    <tr>
                      <td colSpan={10} className="bg-base-200 p-0">
                        <div className="p-4 space-y-4">
                          {/* Cost breakdown cards */}
                          <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                            <div className={`rounded-lg p-3 ${varianceBg(jc.material_variance)}`}>
                              <p className="text-xs font-medium text-gray-500 uppercase">Material</p>
                              <div className="flex justify-between mt-1">
                                <span className="text-sm">Est: {fmt(jc.estimated_material_cost)}</span>
                                <span className="text-sm">Act: {fmt(jc.actual_material_cost)}</span>
                              </div>
                              <p className={`text-sm font-medium mt-1 ${varianceColor(jc.material_variance)}`}>
                                Variance: {fmt(jc.material_variance)}
                              </p>
                            </div>
                            <div className={`rounded-lg p-3 ${varianceBg(jc.labor_variance)}`}>
                              <p className="text-xs font-medium text-gray-500 uppercase">Labor</p>
                              <div className="flex justify-between mt-1">
                                <span className="text-sm">Est: {fmt(jc.estimated_labor_cost)}</span>
                                <span className="text-sm">Act: {fmt(jc.actual_labor_cost)}</span>
                              </div>
                              <p className={`text-sm font-medium mt-1 ${varianceColor(jc.labor_variance)}`}>
                                Variance: {fmt(jc.labor_variance)}
                              </p>
                            </div>
                            <div className={`rounded-lg p-3 ${varianceBg(jc.overhead_variance)}`}>
                              <p className="text-xs font-medium text-gray-500 uppercase">Overhead</p>
                              <div className="flex justify-between mt-1">
                                <span className="text-sm">Est: {fmt(jc.estimated_overhead_cost)}</span>
                                <span className="text-sm">Act: {fmt(jc.actual_overhead_cost)}</span>
                              </div>
                              <p className={`text-sm font-medium mt-1 ${varianceColor(jc.overhead_variance)}`}>
                                Variance: {fmt(jc.overhead_variance)}
                              </p>
                            </div>
                          </div>

                          {/* Entries table */}
                          <div>
                            <div className="flex justify-between items-center mb-2">
                              <h3 className="font-semibold text-sm">Cost Entries</h3>
                              <button
                                onClick={() => openEntryModal(jc.id)}
                                className="du-btn du-btn-primary du-btn-xs gap-1"
                              >
                                <PlusIcon className="h-3 w-3" />
                                Add Entry
                              </button>
                            </div>

                            {entriesLoading ? (
                              <p className="text-sm text-gray-500 py-2">Loading entries...</p>
                            ) : entries.length === 0 ? (
                              <p className="text-sm text-gray-500 py-2">
                                No cost entries yet. Add one manually or recalculate from time entries.
                              </p>
                            ) : (
                              <div className="overflow-x-auto">
                                <table className="du-table du-table-xs w-full">
                                  <thead>
                                    <tr>
                                      <th>Date</th>
                                      <th>Type</th>
                                      <th>Description</th>
                                      <th>Source</th>
                                      <th className="text-right">Qty</th>
                                      <th className="text-right">Unit Cost</th>
                                      <th className="text-right">Total</th>
                                      <th className="w-10"></th>
                                    </tr>
                                  </thead>
                                  <tbody>
                                    {entries.map((entry) => (
                                      <tr key={entry.id}>
                                        <td className="text-xs">{entry.entry_date}</td>
                                        <td>
                                          <span
                                            className={`du-badge du-badge-xs ${
                                              entry.entry_type === 'material'
                                                ? 'du-badge-primary'
                                                : entry.entry_type === 'labor'
                                                ? 'du-badge-secondary'
                                                : entry.entry_type === 'overhead'
                                                ? 'du-badge-accent'
                                                : 'du-badge-ghost'
                                            }`}
                                          >
                                            {entryTypeLabel[entry.entry_type] || entry.entry_type}
                                          </span>
                                        </td>
                                        <td className="text-sm">{entry.description}</td>
                                        <td className="text-xs text-gray-500">
                                          {entry.source}
                                          {entry.reference && ` (${entry.reference})`}
                                        </td>
                                        <td className="text-right font-mono text-xs">
                                          {entry.quantity.toFixed(2)}
                                        </td>
                                        <td className="text-right font-mono text-xs">
                                          {fmt(entry.unit_cost)}
                                        </td>
                                        <td className="text-right font-mono text-xs font-medium">
                                          {fmt(entry.total_cost)}
                                        </td>
                                        <td>
                                          <button
                                            onClick={() => handleDeleteEntry(jc.id, entry.id)}
                                            className="du-btn du-btn-ghost du-btn-xs text-red-500"
                                            title="Delete entry"
                                          >
                                            <TrashIcon className="h-3.5 w-3.5" />
                                          </button>
                                        </td>
                                      </tr>
                                    ))}
                                  </tbody>
                                </table>
                              </div>
                            )}
                          </div>
                        </div>
                      </td>
                    </tr>
                  )}
                </React.Fragment>
              ))
            )}
          </tbody>
        </table>
      </div>

      {/* ── Create Job Cost Modal ───────────────────────────────── */}
      {showCreateModal && (
        <div className="du-modal du-modal-open">
          <div className="du-modal-box max-w-lg">
            <div className="flex justify-between items-center mb-4">
              <h3 className="font-bold text-lg">New Job Cost</h3>
              <button
                onClick={() => setShowCreateModal(false)}
                className="du-btn du-btn-ghost du-btn-sm du-btn-circle"
              >
                <XMarkIcon className="h-5 w-5" />
              </button>
            </div>

            <form onSubmit={handleCreate} className="space-y-4">
              <div className="du-form-control">
                <label className="du-label">
                  <span className="du-label-text">Work Order</span>
                </label>
                <select
                  className="du-select du-select-bordered w-full"
                  value={createForm.work_order_id}
                  onChange={(e) =>
                    setCreateForm({ ...createForm, work_order_id: parseInt(e.target.value) })
                  }
                  required
                >
                  <option value={0} disabled>
                    Select a work order...
                  </option>
                  {workOrders.map((wo) => (
                    <option key={wo.id} value={wo.id}>
                      {wo.work_order_number}
                      {wo.customer_name ? ` - ${wo.customer_name}` : ''}
                    </option>
                  ))}
                </select>
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div className="du-form-control">
                  <label className="du-label">
                    <span className="du-label-text">Est. Material Cost</span>
                  </label>
                  <input
                    type="number"
                    step="0.01"
                    min="0"
                    className="du-input du-input-bordered w-full"
                    value={createForm.estimated_material_cost}
                    onChange={(e) =>
                      setCreateForm({
                        ...createForm,
                        estimated_material_cost: parseFloat(e.target.value) || 0,
                      })
                    }
                  />
                </div>
                <div className="du-form-control">
                  <label className="du-label">
                    <span className="du-label-text">Est. Labor Cost</span>
                  </label>
                  <input
                    type="number"
                    step="0.01"
                    min="0"
                    className="du-input du-input-bordered w-full"
                    value={createForm.estimated_labor_cost}
                    onChange={(e) =>
                      setCreateForm({
                        ...createForm,
                        estimated_labor_cost: parseFloat(e.target.value) || 0,
                      })
                    }
                  />
                </div>
                <div className="du-form-control">
                  <label className="du-label">
                    <span className="du-label-text">Est. Overhead Cost</span>
                  </label>
                  <input
                    type="number"
                    step="0.01"
                    min="0"
                    className="du-input du-input-bordered w-full"
                    value={createForm.estimated_overhead_cost}
                    onChange={(e) =>
                      setCreateForm({
                        ...createForm,
                        estimated_overhead_cost: parseFloat(e.target.value) || 0,
                      })
                    }
                  />
                </div>
                <div className="du-form-control">
                  <label className="du-label">
                    <span className="du-label-text">Revenue / Sell Price</span>
                  </label>
                  <input
                    type="number"
                    step="0.01"
                    min="0"
                    className="du-input du-input-bordered w-full"
                    value={createForm.revenue}
                    onChange={(e) =>
                      setCreateForm({
                        ...createForm,
                        revenue: parseFloat(e.target.value) || 0,
                      })
                    }
                  />
                </div>
              </div>

              <div className="du-form-control">
                <label className="du-label">
                  <span className="du-label-text">Notes</span>
                </label>
                <textarea
                  className="du-textarea du-textarea-bordered w-full"
                  rows={2}
                  value={createForm.notes}
                  onChange={(e) => setCreateForm({ ...createForm, notes: e.target.value })}
                />
              </div>

              <div className="du-modal-action">
                <button type="button" onClick={() => setShowCreateModal(false)} className="du-btn">
                  Cancel
                </button>
                <button type="submit" className="du-btn du-btn-primary">
                  Create Job Cost
                </button>
              </div>
            </form>
          </div>
          <div className="du-modal-backdrop" onClick={() => setShowCreateModal(false)} />
        </div>
      )}

      {/* ── Add Entry Modal ─────────────────────────────────────── */}
      {showEntryModal && (
        <div className="du-modal du-modal-open">
          <div className="du-modal-box max-w-lg">
            <div className="flex justify-between items-center mb-4">
              <h3 className="font-bold text-lg">Add Cost Entry</h3>
              <button
                onClick={() => setShowEntryModal(false)}
                className="du-btn du-btn-ghost du-btn-sm du-btn-circle"
              >
                <XMarkIcon className="h-5 w-5" />
              </button>
            </div>

            <form onSubmit={handleAddEntry} className="space-y-4">
              <div className="grid grid-cols-2 gap-3">
                <div className="du-form-control">
                  <label className="du-label">
                    <span className="du-label-text">Type</span>
                  </label>
                  <select
                    className="du-select du-select-bordered w-full"
                    value={entryForm.entry_type}
                    onChange={(e) => setEntryForm({ ...entryForm, entry_type: e.target.value })}
                  >
                    <option value="material">Material</option>
                    <option value="labor">Labor</option>
                    <option value="overhead">Overhead</option>
                    <option value="other">Other</option>
                  </select>
                </div>
                <div className="du-form-control">
                  <label className="du-label">
                    <span className="du-label-text">Source</span>
                  </label>
                  <select
                    className="du-select du-select-bordered w-full"
                    value={entryForm.source}
                    onChange={(e) => setEntryForm({ ...entryForm, source: e.target.value })}
                  >
                    <option value="manual">Manual</option>
                    <option value="time_entry">Time Entry</option>
                    <option value="material_issue">Material Issue</option>
                    <option value="purchase">Purchase</option>
                  </select>
                </div>
              </div>

              <div className="du-form-control">
                <label className="du-label">
                  <span className="du-label-text">Description</span>
                </label>
                <input
                  type="text"
                  className="du-input du-input-bordered w-full"
                  value={entryForm.description}
                  onChange={(e) => setEntryForm({ ...entryForm, description: e.target.value })}
                  placeholder="e.g., Aluminum 6061 bar stock"
                  required
                />
              </div>

              <div className="grid grid-cols-3 gap-3">
                <div className="du-form-control">
                  <label className="du-label">
                    <span className="du-label-text">Quantity</span>
                  </label>
                  <input
                    type="number"
                    step="0.01"
                    min="0"
                    className="du-input du-input-bordered w-full"
                    value={entryForm.quantity}
                    onChange={(e) =>
                      setEntryForm({ ...entryForm, quantity: parseFloat(e.target.value) || 0 })
                    }
                  />
                </div>
                <div className="du-form-control">
                  <label className="du-label">
                    <span className="du-label-text">Unit Cost</span>
                  </label>
                  <input
                    type="number"
                    step="0.01"
                    min="0"
                    className="du-input du-input-bordered w-full"
                    value={entryForm.unit_cost}
                    onChange={(e) =>
                      setEntryForm({ ...entryForm, unit_cost: parseFloat(e.target.value) || 0 })
                    }
                  />
                </div>
                <div className="du-form-control">
                  <label className="du-label">
                    <span className="du-label-text">Total</span>
                  </label>
                  <input
                    type="text"
                    className="du-input du-input-bordered w-full bg-gray-50"
                    value={fmt(entryForm.quantity * entryForm.unit_cost)}
                    disabled
                  />
                </div>
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div className="du-form-control">
                  <label className="du-label">
                    <span className="du-label-text">Entry Date</span>
                  </label>
                  <input
                    type="date"
                    className="du-input du-input-bordered w-full"
                    value={entryForm.entry_date}
                    onChange={(e) => setEntryForm({ ...entryForm, entry_date: e.target.value })}
                    required
                  />
                </div>
                <div className="du-form-control">
                  <label className="du-label">
                    <span className="du-label-text">Reference (PO#, etc.)</span>
                  </label>
                  <input
                    type="text"
                    className="du-input du-input-bordered w-full"
                    value={entryForm.reference}
                    onChange={(e) => setEntryForm({ ...entryForm, reference: e.target.value })}
                    placeholder="Optional"
                  />
                </div>
              </div>

              <div className="du-modal-action">
                <button type="button" onClick={() => setShowEntryModal(false)} className="du-btn">
                  Cancel
                </button>
                <button type="submit" className="du-btn du-btn-primary">
                  Add Entry
                </button>
              </div>
            </form>
          </div>
          <div className="du-modal-backdrop" onClick={() => setShowEntryModal(false)} />
        </div>
      )}
    </div>
  );
}
