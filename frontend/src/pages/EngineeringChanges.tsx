import React, { useEffect, useState, useMemo, useCallback } from 'react';
import api from '../services/api';
import {
  ClockIcon,
  CheckCircleIcon,
  ExclamationTriangleIcon,
  PlusIcon,
  XMarkIcon,
  ChevronDownIcon,
  ChevronUpIcon,
  MagnifyingGlassIcon,
  FunnelIcon,
  DocumentTextIcon,
  ArrowPathIcon,
  WrenchScrewdriverIcon,
} from '@heroicons/react/24/outline';
import { SkeletonTable } from '../components/ui/Skeleton';

// ── Types ────────────────────────────────────────────────────────

type ECOType = 'design' | 'process' | 'material' | 'documentation' | 'other';
type ECOPriority = 'low' | 'medium' | 'high' | 'critical';
type ECOStatus =
  | 'draft'
  | 'submitted'
  | 'under_review'
  | 'approved'
  | 'rejected'
  | 'in_implementation'
  | 'completed'
  | 'cancelled';

interface UserSummary {
  id: number;
  first_name: string;
  last_name: string;
  email: string;
}

interface Approval {
  id: number;
  eco_id: number;
  approver_id: number;
  approver: UserSummary | null;
  role: string;
  status: string;
  comments: string | null;
  decision_date: string | null;
  created_at: string;
}

interface Task {
  id: number;
  eco_id: number;
  task_number: number;
  description: string;
  department: string | null;
  assigned_to: number | null;
  assignee: UserSummary | null;
  status: string;
  due_date: string | null;
  completed_date: string | null;
  notes: string | null;
  created_at: string;
}

interface ECO {
  id: number;
  eco_number: string;
  title: string;
  description: string;
  eco_type: ECOType;
  priority: ECOPriority;
  status: ECOStatus;
  reason_for_change: string;
  proposed_solution: string | null;
  impact_analysis: string | null;
  risk_assessment: string | null;
  affected_parts: string | null;
  affected_work_orders: string | null;
  affected_documents: string | null;
  estimated_cost: number;
  actual_cost: number;
  effectivity_type: string | null;
  effectivity_date: string | null;
  effectivity_serial: string | null;
  requested_by: number;
  requester: UserSummary | null;
  assigned_to: number | null;
  assignee: UserSummary | null;
  approved_by: number | null;
  approver: UserSummary | null;
  approved_date: string | null;
  target_date: string | null;
  completed_date: string | null;
  created_at: string;
  updated_at: string;
  approvals: Approval[];
  implementation_tasks: Task[];
}

interface Dashboard {
  pending_review: number;
  in_implementation: number;
  completed_this_month: number;
  total_active: number;
  by_type: Record<string, number>;
  by_priority: Record<string, number>;
  avg_cycle_time_days: number | null;
}

interface ECOCreateForm {
  title: string;
  eco_type: ECOType;
  priority: ECOPriority;
  description: string;
  reason_for_change: string;
  proposed_solution: string;
  affected_parts: string;
  estimated_cost: number;
  target_date: string;
}

// ── Helpers ──────────────────────────────────────────────────────

const typeBadge: Record<ECOType, string> = { design: 'bg-blue-100 text-blue-800', process: 'bg-purple-100 text-purple-800', material: 'bg-amber-100 text-amber-800', documentation: 'bg-gray-100 text-gray-800', other: 'bg-slate-100 text-slate-800' };
const typeLabel: Record<ECOType, string> = { design: 'Design', process: 'Process', material: 'Material', documentation: 'Documentation', other: 'Other' };
const priorityBadge: Record<ECOPriority, string> = { low: 'bg-gray-100 text-gray-800', medium: 'bg-blue-100 text-blue-800', high: 'bg-orange-100 text-orange-800', critical: 'bg-red-100 text-red-800' };
const priorityLabel: Record<ECOPriority, string> = { low: 'Low', medium: 'Medium', high: 'High', critical: 'Critical' };
const statusBadge: Record<ECOStatus, string> = { draft: 'bg-gray-100 text-gray-800', submitted: 'bg-blue-100 text-blue-800', under_review: 'bg-purple-100 text-purple-800', approved: 'bg-green-100 text-green-800', rejected: 'bg-red-100 text-red-800', in_implementation: 'bg-yellow-100 text-yellow-800', completed: 'bg-emerald-100 text-emerald-800', cancelled: 'bg-slate-100 text-slate-800' };
const statusLabel: Record<ECOStatus, string> = { draft: 'Draft', submitted: 'Submitted', under_review: 'Under Review', approved: 'Approved', rejected: 'Rejected', in_implementation: 'In Implementation', completed: 'Completed', cancelled: 'Cancelled' };
const taskStatusLabel: Record<string, string> = { pending: 'Pending', in_progress: 'In Progress', completed: 'Completed', skipped: 'Skipped' };

const formatDate = (d: string | null) => {
  if (!d) return '-';
  return new Date(d).toLocaleDateString('en-US', {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
  });
};

const fmt = (n: number) =>
  new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(n);

const userName = (u: UserSummary | null) =>
  u ? `${u.first_name} ${u.last_name}` : '-';

// ── Component ────────────────────────────────────────────────────

export default function EngineeringChanges() {
  // Data
  const [ecos, setEcos] = useState<ECO[]>([]);
  const [dashboard, setDashboard] = useState<Dashboard | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Filters
  const [searchTerm, setSearchTerm] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [typeFilter, setTypeFilter] = useState('');
  const [priorityFilter, setPriorityFilter] = useState('');

  // Expand
  const [expandedId, setExpandedId] = useState<number | null>(null);

  // Create ECO modal
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [createForm, setCreateForm] = useState<ECOCreateForm>({
    title: '',
    eco_type: 'design',
    priority: 'medium',
    description: '',
    reason_for_change: '',
    proposed_solution: '',
    affected_parts: '',
    estimated_cost: 0,
    target_date: '',
  });
  const [createLoading, setCreateLoading] = useState(false);

  // Action loading
  const [actionLoading, setActionLoading] = useState<number | null>(null);

  // Reject modal
  const [showRejectModal, setShowRejectModal] = useState(false);
  const [rejectEcoId, setRejectEcoId] = useState<number | null>(null);
  const [rejectComments, setRejectComments] = useState('');

  // ── Data fetching ──────────────────────────────────────────────

  const loadEcos = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);
      const params: Record<string, string> = {};
      if (statusFilter) params.status = statusFilter;
      if (typeFilter) params.eco_type = typeFilter;
      if (priorityFilter) params.priority = priorityFilter;
      const data = await api.getECOs(params);
      setEcos(data);
    } catch (err: any) {
      console.error('Failed to load ECOs:', err);
      setError(err?.response?.data?.detail || 'Failed to load engineering changes');
    } finally {
      setLoading(false);
    }
  }, [statusFilter, typeFilter, priorityFilter]);

  const loadDashboard = useCallback(async () => {
    try {
      const data = await api.getECODashboard();
      setDashboard(data);
    } catch (err) {
      console.error('Failed to load dashboard:', err);
    }
  }, []);

  useEffect(() => {
    loadEcos();
    loadDashboard();
  }, [loadEcos, loadDashboard]);

  // ── Filtered data ──────────────────────────────────────────────

  const filtered = useMemo(() => {
    if (!searchTerm) return ecos;
    const term = searchTerm.toLowerCase();
    return ecos.filter((e) =>
      e.eco_number.toLowerCase().includes(term) ||
      e.title.toLowerCase().includes(term) ||
      e.description.toLowerCase().includes(term) ||
      userName(e.requester).toLowerCase().includes(term)
    );
  }, [ecos, searchTerm]);

  // ── Row expand ─────────────────────────────────────────────────

  const toggleExpand = useCallback((id: number) => {
    setExpandedId((prev) => (prev === id ? null : id));
  }, []);

  // ── Actions ────────────────────────────────────────────────────

  const handleSubmit = useCallback(async (id: number) => {
    try {
      setActionLoading(id);
      await api.submitECO(id);
      loadEcos();
      loadDashboard();
    } catch (err: any) {
      alert(err?.response?.data?.detail || 'Failed to submit ECO');
    } finally {
      setActionLoading(null);
    }
  }, [loadEcos, loadDashboard]);

  const handleApprove = useCallback(async (id: number) => {
    try {
      setActionLoading(id);
      await api.approveECO(id, { status: 'approved', comments: null });
      loadEcos();
      loadDashboard();
    } catch (err: any) {
      alert(err?.response?.data?.detail || 'Failed to approve ECO');
    } finally {
      setActionLoading(null);
    }
  }, [loadEcos, loadDashboard]);

  const openRejectModal = useCallback((id: number) => {
    setRejectEcoId(id);
    setRejectComments('');
    setShowRejectModal(true);
  }, []);

  const handleReject = useCallback(async () => {
    if (!rejectEcoId) return;
    try {
      setActionLoading(rejectEcoId);
      await api.rejectECO(rejectEcoId, { status: 'rejected', comments: rejectComments || null });
      setShowRejectModal(false);
      loadEcos();
      loadDashboard();
    } catch (err: any) {
      alert(err?.response?.data?.detail || 'Failed to reject ECO');
    } finally {
      setActionLoading(null);
    }
  }, [rejectEcoId, rejectComments, loadEcos, loadDashboard]);

  const handleImplement = useCallback(async (id: number) => {
    try {
      setActionLoading(id);
      await api.implementECO(id);
      loadEcos();
      loadDashboard();
    } catch (err: any) {
      alert(err?.response?.data?.detail || 'Failed to start implementation');
    } finally {
      setActionLoading(null);
    }
  }, [loadEcos, loadDashboard]);

  const handleComplete = useCallback(async (id: number) => {
    try {
      setActionLoading(id);
      await api.completeECO(id);
      loadEcos();
      loadDashboard();
    } catch (err: any) {
      alert(err?.response?.data?.detail || 'Failed to complete ECO');
    } finally {
      setActionLoading(null);
    }
  }, [loadEcos, loadDashboard]);

  // ── Create ECO ─────────────────────────────────────────────────

  const openCreateModal = useCallback(() => {
    setCreateForm({
      title: '',
      eco_type: 'design',
      priority: 'medium',
      description: '',
      reason_for_change: '',
      proposed_solution: '',
      affected_parts: '',
      estimated_cost: 0,
      target_date: '',
    });
    setShowCreateModal(true);
  }, []);

  const handleCreate = useCallback(async (e: React.FormEvent) => {
    e.preventDefault();
    if (!createForm.title.trim() || createForm.title.trim().length < 3) {
      alert('Title must be at least 3 characters');
      return;
    }
    if (!createForm.description.trim() || createForm.description.trim().length < 5) {
      alert('Description must be at least 5 characters');
      return;
    }
    if (!createForm.reason_for_change.trim() || createForm.reason_for_change.trim().length < 5) {
      alert('Reason for change must be at least 5 characters');
      return;
    }
    try {
      setCreateLoading(true);
      const payload: Record<string, unknown> = {
        title: createForm.title,
        eco_type: createForm.eco_type,
        priority: createForm.priority,
        description: createForm.description,
        reason_for_change: createForm.reason_for_change,
        estimated_cost: createForm.estimated_cost,
      };
      if (createForm.proposed_solution) payload.proposed_solution = createForm.proposed_solution;
      if (createForm.target_date) payload.target_date = createForm.target_date;
      if (createForm.affected_parts.trim()) {
        const partIds = createForm.affected_parts.split(',').map((s) => parseInt(s.trim(), 10)).filter((n) => !isNaN(n));
        if (partIds.length > 0) payload.affected_parts = partIds;
      }
      await api.createECO(payload);
      setShowCreateModal(false);
      loadEcos();
      loadDashboard();
    } catch (err: any) {
      alert(err?.response?.data?.detail || 'Failed to create ECO');
    } finally {
      setCreateLoading(false);
    }
  }, [createForm, loadEcos, loadDashboard]);

  // ── Render ─────────────────────────────────────────────────────

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Engineering Changes</h1>
          <p className="text-sm text-gray-500 mt-1">Manage Engineering Change Orders (ECO/ECN)</p>
        </div>
        <button
          onClick={openCreateModal}
          className="inline-flex items-center gap-2 rounded-md bg-indigo-600 px-4 py-2 text-sm font-semibold text-white shadow-sm hover:bg-indigo-500"
        >
          <PlusIcon className="h-5 w-5" />
          New ECO
        </button>
      </div>

      {/* Dashboard Cards */}
      {dashboard && (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {[
            { label: 'Total Active', value: dashboard.total_active, icon: DocumentTextIcon, bg: 'bg-blue-100', fg: 'text-blue-600' },
            { label: 'Pending Review', value: dashboard.pending_review, icon: ClockIcon, bg: 'bg-yellow-100', fg: 'text-yellow-600' },
            { label: 'In Implementation', value: dashboard.in_implementation, icon: WrenchScrewdriverIcon, bg: 'bg-purple-100', fg: 'text-purple-600' },
            { label: 'Completed This Month', value: dashboard.completed_this_month, icon: CheckCircleIcon, bg: 'bg-green-100', fg: 'text-green-600' },
          ].map((card) => (
            <div key={card.label} className="rounded-lg border bg-white p-4 shadow-sm">
              <div className="flex items-center gap-3">
                <div className={`rounded-full ${card.bg} p-2`}><card.icon className={`h-5 w-5 ${card.fg}`} /></div>
                <div>
                  <p className="text-sm text-gray-500">{card.label}</p>
                  <p className="text-2xl font-bold text-gray-900">{card.value}</p>
                  {card.label === 'Completed This Month' && dashboard.avg_cycle_time_days != null && (
                    <p className="text-xs text-gray-400 mt-1">Avg cycle: {dashboard.avg_cycle_time_days}d</p>
                  )}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Filters & Search */}
      <div className="flex flex-wrap items-center gap-3">
        <div className="relative flex-1 min-w-[200px]">
          <MagnifyingGlassIcon className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-gray-400" />
          <input
            type="text"
            placeholder="Search ECO number, title, requestor..."
            value={searchTerm}
            onChange={(e) => setSearchTerm(e.target.value)}
            className="w-full rounded-md border border-gray-300 py-2 pl-10 pr-3 text-sm focus:border-indigo-500 focus:ring-indigo-500"
          />
        </div>
        <div className="flex items-center gap-2">
          <FunnelIcon className="h-4 w-4 text-gray-400" />
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            className="rounded-md border border-gray-300 py-2 pl-3 pr-8 text-sm focus:border-indigo-500 focus:ring-indigo-500"
          >
            <option value="">All Statuses</option>
            {Object.entries(statusLabel).map(([k, v]) => (
              <option key={k} value={k}>{v}</option>
            ))}
          </select>
          <select
            value={typeFilter}
            onChange={(e) => setTypeFilter(e.target.value)}
            className="rounded-md border border-gray-300 py-2 pl-3 pr-8 text-sm focus:border-indigo-500 focus:ring-indigo-500"
          >
            <option value="">All Types</option>
            {Object.entries(typeLabel).map(([k, v]) => (
              <option key={k} value={k}>{v}</option>
            ))}
          </select>
          <select
            value={priorityFilter}
            onChange={(e) => setPriorityFilter(e.target.value)}
            className="rounded-md border border-gray-300 py-2 pl-3 pr-8 text-sm focus:border-indigo-500 focus:ring-indigo-500"
          >
            <option value="">All Priorities</option>
            {Object.entries(priorityLabel).map(([k, v]) => (
              <option key={k} value={k}>{v}</option>
            ))}
          </select>
        </div>
      </div>

      {/* Error */}
      {error && (
        <div className="rounded-md bg-red-50 border border-red-200 p-4">
          <div className="flex items-center gap-2">
            <ExclamationTriangleIcon className="h-5 w-5 text-red-500" />
            <p className="text-sm text-red-700">{error}</p>
          </div>
        </div>
      )}

      {/* Table */}
      {loading ? (
        <SkeletonTable rows={6} columns={7} />
      ) : (
        <div className="overflow-x-auto rounded-lg border bg-white shadow-sm">
          <table className="min-w-full divide-y divide-gray-200">
            <thead className="bg-gray-50">
              <tr>
                <th className="w-8 px-3 py-3" />
                <th className="px-4 py-3 text-left text-xs font-medium uppercase text-gray-500">ECO Number</th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase text-gray-500">Title</th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase text-gray-500">Type</th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase text-gray-500">Priority</th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase text-gray-500">Status</th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase text-gray-500">Requestor</th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase text-gray-500">Date</th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase text-gray-500">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-200">
              {filtered.length === 0 ? (
                <tr>
                  <td colSpan={9} className="px-4 py-8 text-center text-sm text-gray-500">
                    No engineering change orders found.
                  </td>
                </tr>
              ) : (
                filtered.map((eco) => (
                  <React.Fragment key={eco.id}>
                    <tr
                      className="hover:bg-gray-50 cursor-pointer"
                      onClick={() => toggleExpand(eco.id)}
                    >
                      <td className="px-3 py-3">
                        {expandedId === eco.id ? (
                          <ChevronUpIcon className="h-4 w-4 text-gray-400" />
                        ) : (
                          <ChevronDownIcon className="h-4 w-4 text-gray-400" />
                        )}
                      </td>
                      <td className="px-4 py-3 text-sm font-mono font-medium text-indigo-600">
                        {eco.eco_number}
                      </td>
                      <td className="px-4 py-3 text-sm text-gray-900 max-w-[200px] truncate">
                        {eco.title}
                      </td>
                      <td className="px-4 py-3">
                        <span className={`inline-flex rounded-full px-2 py-0.5 text-xs font-medium ${typeBadge[eco.eco_type] || 'bg-gray-100 text-gray-800'}`}>
                          {typeLabel[eco.eco_type] || eco.eco_type}
                        </span>
                      </td>
                      <td className="px-4 py-3">
                        <span className={`inline-flex rounded-full px-2 py-0.5 text-xs font-medium ${priorityBadge[eco.priority] || 'bg-gray-100 text-gray-800'}`}>
                          {priorityLabel[eco.priority] || eco.priority}
                        </span>
                      </td>
                      <td className="px-4 py-3">
                        <span className={`inline-flex rounded-full px-2 py-0.5 text-xs font-medium ${statusBadge[eco.status] || 'bg-gray-100 text-gray-800'}`}>
                          {statusLabel[eco.status] || eco.status}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-sm text-gray-700">
                        {userName(eco.requester)}
                      </td>
                      <td className="px-4 py-3 text-sm text-gray-500">
                        {formatDate(eco.created_at)}
                      </td>
                      <td className="px-4 py-3" onClick={(e) => e.stopPropagation()}>
                        <div className="flex items-center gap-1">
                          {eco.status === 'draft' && (
                            <button
                              onClick={() => handleSubmit(eco.id)}
                              disabled={actionLoading === eco.id}
                              className="rounded bg-blue-600 px-2 py-1 text-xs font-medium text-white hover:bg-blue-500 disabled:opacity-50"
                            >
                              Submit
                            </button>
                          )}
                          {(eco.status === 'submitted' || eco.status === 'under_review') && (
                            <>
                              <button
                                onClick={() => handleApprove(eco.id)}
                                disabled={actionLoading === eco.id}
                                className="rounded bg-green-600 px-2 py-1 text-xs font-medium text-white hover:bg-green-500 disabled:opacity-50"
                              >
                                Approve
                              </button>
                              <button
                                onClick={() => openRejectModal(eco.id)}
                                disabled={actionLoading === eco.id}
                                className="rounded bg-red-600 px-2 py-1 text-xs font-medium text-white hover:bg-red-500 disabled:opacity-50"
                              >
                                Reject
                              </button>
                            </>
                          )}
                          {eco.status === 'approved' && (
                            <button
                              onClick={() => handleImplement(eco.id)}
                              disabled={actionLoading === eco.id}
                              className="rounded bg-purple-600 px-2 py-1 text-xs font-medium text-white hover:bg-purple-500 disabled:opacity-50"
                            >
                              Implement
                            </button>
                          )}
                          {eco.status === 'in_implementation' && (
                            <button
                              onClick={() => handleComplete(eco.id)}
                              disabled={actionLoading === eco.id}
                              className="rounded bg-emerald-600 px-2 py-1 text-xs font-medium text-white hover:bg-emerald-500 disabled:opacity-50"
                            >
                              Complete
                            </button>
                          )}
                        </div>
                      </td>
                    </tr>

                    {/* Expanded detail row */}
                    {expandedId === eco.id && (
                      <tr>
                        <td colSpan={9} className="bg-gray-50 px-6 py-4">
                          <div className="space-y-4">
                            {/* Description & Reason */}
                            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                              <div>
                                <h4 className="text-xs font-semibold uppercase text-gray-500 mb-1">Description</h4>
                                <p className="text-sm text-gray-700 whitespace-pre-wrap">{eco.description}</p>
                              </div>
                              <div>
                                <h4 className="text-xs font-semibold uppercase text-gray-500 mb-1">Reason for Change</h4>
                                <p className="text-sm text-gray-700 whitespace-pre-wrap">{eco.reason_for_change}</p>
                              </div>
                            </div>

                            {/* Extra details */}
                            <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
                              {eco.proposed_solution && (
                                <div><span className="text-xs font-semibold uppercase text-gray-500">Proposed Solution</span><p className="text-gray-700 mt-0.5">{eco.proposed_solution}</p></div>
                              )}
                              <div><span className="text-xs font-semibold uppercase text-gray-500">Est. Cost</span><p className="text-gray-700 mt-0.5">{fmt(eco.estimated_cost)}</p></div>
                              {eco.actual_cost > 0 && <div><span className="text-xs font-semibold uppercase text-gray-500">Actual Cost</span><p className="text-gray-700 mt-0.5">{fmt(eco.actual_cost)}</p></div>}
                              {eco.target_date && <div><span className="text-xs font-semibold uppercase text-gray-500">Target Date</span><p className="text-gray-700 mt-0.5">{formatDate(eco.target_date)}</p></div>}
                              {eco.assignee && <div><span className="text-xs font-semibold uppercase text-gray-500">Assigned To</span><p className="text-gray-700 mt-0.5">{userName(eco.assignee)}</p></div>}
                              {eco.completed_date && <div><span className="text-xs font-semibold uppercase text-gray-500">Completed</span><p className="text-gray-700 mt-0.5">{formatDate(eco.completed_date)}</p></div>}
                            </div>

                            {/* Approval Workflow */}
                            {eco.approvals.length > 0 && (
                              <div>
                                <h4 className="text-xs font-semibold uppercase text-gray-500 mb-2">Approval Workflow</h4>
                                <div className="rounded border bg-white divide-y">
                                  {eco.approvals.map((a) => (
                                    <div key={a.id} className="flex items-center justify-between px-4 py-2 text-sm">
                                      <div><span className="font-medium text-gray-900">{userName(a.approver)}</span><span className="ml-2 text-gray-500">({a.role})</span></div>
                                      <div className="flex items-center gap-3">
                                        {a.comments && <span className="text-gray-500 italic text-xs max-w-[200px] truncate">{a.comments}</span>}
                                        <span className={`inline-flex rounded-full px-2 py-0.5 text-xs font-medium ${a.status === 'approved' ? 'bg-green-100 text-green-800' : a.status === 'rejected' ? 'bg-red-100 text-red-800' : 'bg-yellow-100 text-yellow-800'}`}>
                                          {a.status === 'approved' ? 'Approved' : a.status === 'rejected' ? 'Rejected' : 'Pending'}
                                        </span>
                                        {a.decision_date && <span className="text-xs text-gray-400">{formatDate(a.decision_date)}</span>}
                                      </div>
                                    </div>
                                  ))}
                                </div>
                              </div>
                            )}
                            {/* Implementation Tasks */}
                            {eco.implementation_tasks.length > 0 && (
                              <div>
                                <h4 className="text-xs font-semibold uppercase text-gray-500 mb-2">Implementation Tasks</h4>
                                <div className="rounded border bg-white divide-y">
                                  {eco.implementation_tasks.map((t) => (
                                    <div key={t.id} className="flex items-center justify-between px-4 py-2 text-sm">
                                      <div className="flex items-center gap-2">
                                        <span className="text-gray-400 font-mono text-xs">#{t.task_number}</span>
                                        <span className="text-gray-900">{t.description}</span>
                                        {t.department && <span className="text-xs text-gray-500 bg-gray-100 rounded px-1.5 py-0.5">{t.department}</span>}
                                      </div>
                                      <div className="flex items-center gap-3">
                                        {t.assignee && <span className="text-xs text-gray-500">{userName(t.assignee)}</span>}
                                        {t.due_date && <span className="text-xs text-gray-400">Due: {formatDate(t.due_date)}</span>}
                                        <span className={`inline-flex rounded-full px-2 py-0.5 text-xs font-medium ${t.status === 'completed' ? 'bg-green-100 text-green-800' : t.status === 'in_progress' ? 'bg-blue-100 text-blue-800' : t.status === 'skipped' ? 'bg-gray-100 text-gray-800' : 'bg-yellow-100 text-yellow-800'}`}>
                                          {taskStatusLabel[t.status] || t.status}
                                        </span>
                                      </div>
                                    </div>
                                  ))}
                                </div>
                              </div>
                            )}
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
      )}

      {/* Results count */}
      {!loading && (
        <p className="text-xs text-gray-400">
          Showing {filtered.length} of {ecos.length} record{ecos.length !== 1 ? 's' : ''}
        </p>
      )}

      {/* ── Create ECO Modal ──────────────────────────────────────── */}
      {showCreateModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
          <div className="w-full max-w-lg rounded-lg bg-white shadow-xl">
            <div className="flex items-center justify-between border-b px-6 py-4">
              <h2 className="text-lg font-semibold text-gray-900">New Engineering Change Order</h2>
              <button onClick={() => setShowCreateModal(false)}>
                <XMarkIcon className="h-5 w-5 text-gray-400 hover:text-gray-600" />
              </button>
            </div>
            <form onSubmit={handleCreate} className="space-y-4 px-6 py-4 max-h-[70vh] overflow-y-auto">
              <div>
                <label className="block text-sm font-medium text-gray-700">Title *</label>
                <input
                  type="text"
                  required
                  value={createForm.title}
                  onChange={(e) => setCreateForm((f) => ({ ...f, title: e.target.value }))}
                  className="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-indigo-500 focus:ring-indigo-500"
                />
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="block text-sm font-medium text-gray-700">Type *</label>
                  <select
                    value={createForm.eco_type}
                    onChange={(e) => setCreateForm((f) => ({ ...f, eco_type: e.target.value as ECOType }))}
                    className="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-indigo-500 focus:ring-indigo-500"
                  >
                    {Object.entries(typeLabel).map(([k, v]) => (
                      <option key={k} value={k}>{v}</option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700">Priority</label>
                  <select
                    value={createForm.priority}
                    onChange={(e) => setCreateForm((f) => ({ ...f, priority: e.target.value as ECOPriority }))}
                    className="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-indigo-500 focus:ring-indigo-500"
                  >
                    {Object.entries(priorityLabel).map(([k, v]) => (
                      <option key={k} value={k}>{v}</option>
                    ))}
                  </select>
                </div>
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700">Description *</label>
                <textarea
                  required
                  rows={3}
                  value={createForm.description}
                  onChange={(e) => setCreateForm((f) => ({ ...f, description: e.target.value }))}
                  className="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-indigo-500 focus:ring-indigo-500"
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700">Reason for Change *</label>
                <textarea
                  required
                  rows={2}
                  value={createForm.reason_for_change}
                  onChange={(e) => setCreateForm((f) => ({ ...f, reason_for_change: e.target.value }))}
                  className="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-indigo-500 focus:ring-indigo-500"
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700">Proposed Solution</label>
                <textarea
                  rows={2}
                  value={createForm.proposed_solution}
                  onChange={(e) => setCreateForm((f) => ({ ...f, proposed_solution: e.target.value }))}
                  className="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-indigo-500 focus:ring-indigo-500"
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700">Affected Part IDs</label>
                <input
                  type="text"
                  placeholder="e.g. 1, 2, 3"
                  value={createForm.affected_parts}
                  onChange={(e) => setCreateForm((f) => ({ ...f, affected_parts: e.target.value }))}
                  className="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-indigo-500 focus:ring-indigo-500"
                />
                <p className="mt-1 text-xs text-gray-400">Comma-separated part IDs</p>
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="block text-sm font-medium text-gray-700">Estimated Cost</label>
                  <input
                    type="number"
                    step="0.01"
                    min="0"
                    value={createForm.estimated_cost}
                    onChange={(e) => setCreateForm((f) => ({ ...f, estimated_cost: parseFloat(e.target.value) || 0 }))}
                    className="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-indigo-500 focus:ring-indigo-500"
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700">Target Date</label>
                  <input
                    type="date"
                    value={createForm.target_date}
                    onChange={(e) => setCreateForm((f) => ({ ...f, target_date: e.target.value }))}
                    className="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-indigo-500 focus:ring-indigo-500"
                  />
                </div>
              </div>
              <div className="flex justify-end gap-3 border-t pt-4">
                <button
                  type="button"
                  onClick={() => setShowCreateModal(false)}
                  className="rounded-md border border-gray-300 px-4 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={createLoading}
                  className="inline-flex items-center gap-2 rounded-md bg-indigo-600 px-4 py-2 text-sm font-semibold text-white hover:bg-indigo-500 disabled:opacity-50"
                >
                  {createLoading && <ArrowPathIcon className="h-4 w-4 animate-spin" />}
                  Create ECO
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* ── Reject Modal ──────────────────────────────────────────── */}
      {showRejectModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
          <div className="w-full max-w-md rounded-lg bg-white shadow-xl">
            <div className="flex items-center justify-between border-b px-6 py-4">
              <h2 className="text-lg font-semibold text-gray-900">Reject ECO</h2>
              <button onClick={() => setShowRejectModal(false)}>
                <XMarkIcon className="h-5 w-5 text-gray-400 hover:text-gray-600" />
              </button>
            </div>
            <div className="space-y-4 px-6 py-4">
              <div>
                <label className="block text-sm font-medium text-gray-700">Rejection Comments</label>
                <textarea
                  rows={3}
                  value={rejectComments}
                  onChange={(e) => setRejectComments(e.target.value)}
                  placeholder="Reason for rejection..."
                  className="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-indigo-500 focus:ring-indigo-500"
                />
              </div>
              <div className="flex justify-end gap-3">
                <button
                  onClick={() => setShowRejectModal(false)}
                  className="rounded-md border border-gray-300 px-4 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50"
                >
                  Cancel
                </button>
                <button
                  onClick={handleReject}
                  disabled={actionLoading !== null}
                  className="rounded-md bg-red-600 px-4 py-2 text-sm font-semibold text-white hover:bg-red-500 disabled:opacity-50"
                >
                  Reject ECO
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
