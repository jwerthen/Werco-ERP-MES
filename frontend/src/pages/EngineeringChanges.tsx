import React, { useEffect, useState, useMemo, useCallback } from 'react';
import api from '../services/api';
import {
  ClockIcon,
  CheckCircleIcon,
  PlusIcon,
  XMarkIcon,
  MagnifyingGlassIcon,
  FunnelIcon,
  DocumentTextIcon,
  ArrowPathIcon,
  WrenchScrewdriverIcon,
} from '@heroicons/react/24/outline';
import { Modal } from '../components/ui/Modal';
import {
  useToast,
  DataTable,
  DataTableColumn,
  MobileDataCard,
  StatusBadge,
  Button,
} from '../components/ui';
import { MiniStat, MiniStatStrip } from '../components/cockpit';

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

const typeBadge: Record<ECOType, string> = { design: 'bg-blue-500/20 text-blue-300', process: 'bg-purple-500/20 text-purple-300', material: 'bg-amber-500/20 text-amber-300', documentation: 'bg-slate-800 text-slate-100', other: 'bg-slate-800/50 text-slate-100' };
const typeLabel: Record<ECOType, string> = { design: 'Design', process: 'Process', material: 'Material', documentation: 'Documentation', other: 'Other' };
const priorityBadge: Record<ECOPriority, string> = { low: 'bg-slate-800 text-slate-100', medium: 'bg-blue-500/20 text-blue-300', high: 'bg-orange-500/20 text-orange-300', critical: 'bg-red-500/20 text-red-300' };
const priorityLabel: Record<ECOPriority, string> = { low: 'Low', medium: 'Medium', high: 'High', critical: 'Critical' };
// Status colors are NOT declared here — they come from the central statusColors
// source of truth via <StatusBadge>. `statusLabel` is kept only for sort
// accessors and the filter <select> option labels (not coloring).
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

const badge = (cls: string, label: string) => (
  <span className={`inline-flex rounded-full px-2 py-0.5 text-xs font-medium ${cls}`}>
    {label}
  </span>
);

const typeChip = (eco: ECO) =>
  badge(typeBadge[eco.eco_type] || 'bg-slate-800 text-slate-100', typeLabel[eco.eco_type] || eco.eco_type);
const priorityChip = (eco: ECO) =>
  badge(priorityBadge[eco.priority] || 'bg-slate-800 text-slate-100', priorityLabel[eco.priority] || eco.priority);
// Status pills pull their color from the central statusColors map via StatusBadge.
const statusChip = (eco: ECO) => <StatusBadge status={eco.status} />;

interface RowActionHandlers {
  onSubmit: (id: number) => void;
  onApprove: (id: number) => void;
  onReject: (id: number) => void;
  onImplement: (id: number) => void;
  onComplete: (id: number) => void;
  actionLoading: number | null;
}

// Renders the status-appropriate workflow buttons for an ECO. `compact` uses the
// smaller table styling; the mobile card reuses the same set.
function EcoRowActions({ eco, h }: { eco: ECO; h: RowActionHandlers }) {
  const busy = h.actionLoading === eco.id;
  return (
    <div className="flex items-center gap-1" onClick={(e) => e.stopPropagation()}>
      {eco.status === 'draft' && (
        <button
          onClick={() => h.onSubmit(eco.id)}
          disabled={busy}
          className="rounded bg-blue-600 px-2 py-1 text-xs font-medium text-white hover:bg-blue-500 disabled:opacity-50"
        >
          Submit
        </button>
      )}
      {(eco.status === 'submitted' || eco.status === 'under_review') && (
        <>
          <button
            onClick={() => h.onApprove(eco.id)}
            disabled={busy}
            className="rounded bg-green-600 px-2 py-1 text-xs font-medium text-white hover:bg-green-500 disabled:opacity-50"
          >
            Approve
          </button>
          <button
            onClick={() => h.onReject(eco.id)}
            disabled={busy}
            className="rounded bg-red-600 px-2 py-1 text-xs font-medium text-white hover:bg-red-500 disabled:opacity-50"
          >
            Reject
          </button>
        </>
      )}
      {eco.status === 'approved' && (
        <button
          onClick={() => h.onImplement(eco.id)}
          disabled={busy}
          className="rounded bg-purple-600 px-2 py-1 text-xs font-medium text-white hover:bg-purple-500 disabled:opacity-50"
        >
          Implement
        </button>
      )}
      {eco.status === 'in_implementation' && (
        <button
          onClick={() => h.onComplete(eco.id)}
          disabled={busy}
          className="rounded bg-emerald-600 px-2 py-1 text-xs font-medium text-white hover:bg-emerald-500 disabled:opacity-50"
        >
          Complete
        </button>
      )}
    </div>
  );
}

function buildEcoColumns(h: RowActionHandlers): Array<DataTableColumn<ECO>> {
  return [
    {
      key: 'eco_number',
      header: 'ECO Number',
      sortable: true,
      accessor: (e) => e.eco_number,
      render: (e) => <span className="font-mono font-medium text-werco-navy-400">{e.eco_number}</span>,
    },
    {
      key: 'title',
      header: 'Title',
      sortable: true,
      accessor: (e) => e.title,
      className: 'max-w-[220px]',
      render: (e) => <span className="text-white block truncate max-w-[220px]">{e.title}</span>,
    },
    {
      key: 'eco_type',
      header: 'Type',
      sortable: true,
      accessor: (e) => typeLabel[e.eco_type] || e.eco_type,
      render: typeChip,
    },
    {
      key: 'priority',
      header: 'Priority',
      sortable: true,
      accessor: (e) => priorityLabel[e.priority] || e.priority,
      render: priorityChip,
    },
    {
      key: 'status',
      header: 'Status',
      sortable: true,
      accessor: (e) => statusLabel[e.status] || e.status,
      render: statusChip,
    },
    {
      key: 'requester',
      header: 'Requestor',
      sortable: true,
      accessor: (e) => userName(e.requester),
      className: 'text-slate-300',
      render: (e) => userName(e.requester),
    },
    {
      key: 'created_at',
      header: 'Date',
      sortable: true,
      accessor: (e) => e.created_at ?? '',
      csv: (e) => formatDate(e.created_at),
      className: 'text-slate-400',
      render: (e) => formatDate(e.created_at),
    },
    {
      key: 'actions',
      header: 'Actions',
      render: (e) => <EcoRowActions eco={e} h={h} />,
    },
  ];
}

// ── Component ────────────────────────────────────────────────────

export default function EngineeringChanges() {
  const { showToast } = useToast();

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

  // Detail modal (replaces the inline expand row — DataTable rows can't host expansion)
  const [detailEco, setDetailEco] = useState<ECO | null>(null);

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

  // ── Detail modal ───────────────────────────────────────────────

  // Keep the open detail modal in sync with the latest loaded data (so a status
  // change reflected in a reload updates the modal in place).
  const detailEcoLive = useMemo(
    () => (detailEco ? ecos.find((e) => e.id === detailEco.id) ?? detailEco : null),
    [detailEco, ecos]
  );

  // ── Actions ────────────────────────────────────────────────────

  const handleSubmit = useCallback(async (id: number) => {
    try {
      setActionLoading(id);
      await api.submitECO(id);
      loadEcos();
      loadDashboard();
    } catch (err: any) {
      showToast('error', err?.response?.data?.detail || 'Failed to submit ECO');
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
      showToast('error', err?.response?.data?.detail || 'Failed to approve ECO');
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
      showToast('error', err?.response?.data?.detail || 'Failed to reject ECO');
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
      showToast('error', err?.response?.data?.detail || 'Failed to start implementation');
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
      showToast('error', err?.response?.data?.detail || 'Failed to complete ECO');
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
      showToast('error', 'Title must be at least 3 characters');
      return;
    }
    if (!createForm.description.trim() || createForm.description.trim().length < 5) {
      showToast('error', 'Description must be at least 5 characters');
      return;
    }
    if (!createForm.reason_for_change.trim() || createForm.reason_for_change.trim().length < 5) {
      showToast('error', 'Reason for change must be at least 5 characters');
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
      showToast('success', 'Engineering change order created');
      loadEcos();
      loadDashboard();
    } catch (err: any) {
      showToast('error', err?.response?.data?.detail || 'Failed to create ECO');
    } finally {
      setCreateLoading(false);
    }
  }, [createForm, loadEcos, loadDashboard]);

  // ── Table columns + mobile cards ───────────────────────────────

  const columns = useMemo(
    () =>
      buildEcoColumns({
        onSubmit: handleSubmit,
        onApprove: handleApprove,
        onReject: openRejectModal,
        onImplement: handleImplement,
        onComplete: handleComplete,
        actionLoading,
      }),
    [handleSubmit, handleApprove, openRejectModal, handleImplement, handleComplete, actionLoading]
  );

  const renderMobileCard = useCallback(
    (eco: ECO) => (
      <MobileDataCard
        title={eco.eco_number}
        subtitle={eco.title}
        badge={statusChip(eco)}
        onClick={() => setDetailEco(eco)}
        fields={[
          { label: 'Type', value: typeChip(eco) },
          { label: 'Priority', value: priorityChip(eco) },
          { label: 'Requestor', value: userName(eco.requester) },
          { label: 'Date', value: formatDate(eco.created_at) },
        ]}
        actions={
          <EcoRowActions
            eco={eco}
            h={{
              onSubmit: handleSubmit,
              onApprove: handleApprove,
              onReject: openRejectModal,
              onImplement: handleImplement,
              onComplete: handleComplete,
              actionLoading,
            }}
          />
        }
      />
    ),
    [handleSubmit, handleApprove, openRejectModal, handleImplement, handleComplete, actionLoading]
  );

  const hasFilters = !!(searchTerm || statusFilter || typeFilter || priorityFilter);

  // ── Render ─────────────────────────────────────────────────────

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-white">Engineering Changes</h1>
          <p className="text-sm text-slate-400 mt-1">Manage Engineering Change Orders (ECO/ECN)</p>
        </div>
        <Button onClick={openCreateModal} className="inline-flex items-center gap-2">
          <PlusIcon className="h-5 w-5" />
          New ECO
        </Button>
      </div>

      {/* Dashboard Cards */}
      {dashboard && (
        <MiniStatStrip className="grid grid-cols-2 lg:grid-cols-4 gap-2">
          <MiniStat
            icon={DocumentTextIcon}
            iconBg="bg-fd-blue/15"
            iconColor="text-fd-blue"
            label="Total Active"
            value={dashboard.total_active}
          />
          <MiniStat
            icon={ClockIcon}
            iconBg="bg-fd-amber/15"
            iconColor="text-fd-amber"
            label="Pending Review"
            value={dashboard.pending_review}
          />
          <MiniStat
            icon={WrenchScrewdriverIcon}
            iconBg="bg-werco-navy-600/15"
            iconColor="text-werco-navy-600"
            label="In Implementation"
            value={dashboard.in_implementation}
          />
          <MiniStat
            icon={CheckCircleIcon}
            iconBg="bg-fd-green/15"
            iconColor="text-fd-green"
            label="Completed This Month"
            value={dashboard.completed_this_month}
            subtitle={
              dashboard.avg_cycle_time_days != null
                ? `Avg cycle: ${dashboard.avg_cycle_time_days}d`
                : undefined
            }
          />
        </MiniStatStrip>
      )}

      {/* Filters & Search */}
      <div className="flex flex-wrap items-center gap-3">
        <div className="relative flex-1 min-w-[200px]">
          <MagnifyingGlassIcon className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-slate-500" />
          <input
            type="text"
            placeholder="Search ECO number, title, requestor..."
            value={searchTerm}
            onChange={(e) => setSearchTerm(e.target.value)}
            className="w-full rounded-sm border border-fd-line bg-fd-panel py-2 pl-10 pr-3 text-sm focus:border-werco-navy-600 focus:ring-werco-navy-600"
          />
        </div>
        <div className="flex items-center gap-2">
          <FunnelIcon className="h-4 w-4 text-slate-500" />
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            className="rounded-sm border border-fd-line bg-fd-panel py-2 pl-3 pr-8 text-sm focus:border-werco-navy-600 focus:ring-werco-navy-600"
          >
            <option value="">All Statuses</option>
            {Object.entries(statusLabel).map(([k, v]) => (
              <option key={k} value={k}>{v}</option>
            ))}
          </select>
          <select
            value={typeFilter}
            onChange={(e) => setTypeFilter(e.target.value)}
            className="rounded-sm border border-fd-line bg-fd-panel py-2 pl-3 pr-8 text-sm focus:border-werco-navy-600 focus:ring-werco-navy-600"
          >
            <option value="">All Types</option>
            {Object.entries(typeLabel).map(([k, v]) => (
              <option key={k} value={k}>{v}</option>
            ))}
          </select>
          <select
            value={priorityFilter}
            onChange={(e) => setPriorityFilter(e.target.value)}
            className="rounded-sm border border-fd-line bg-fd-panel py-2 pl-3 pr-8 text-sm focus:border-werco-navy-600 focus:ring-werco-navy-600"
          >
            <option value="">All Priorities</option>
            {Object.entries(priorityLabel).map(([k, v]) => (
              <option key={k} value={k}>{v}</option>
            ))}
          </select>
        </div>
      </div>

      {/* Table */}
      <DataTable<ECO>
        columns={columns}
        data={filtered}
        rowKey={(e) => e.id}
        onRowClick={(e) => setDetailEco(e)}
        loading={loading}
        error={error || false}
        onRetry={loadEcos}
        defaultSort={{ key: 'created_at', dir: 'desc' }}
        pageSize={25}
        csvExport={{ filename: 'engineering-changes' }}
        mobileCards={renderMobileCard}
        empty={
          hasFilters
            ? {
                icon: DocumentTextIcon,
                title: 'No matching ECOs',
                description: 'No engineering change orders match the current search or filters.',
              }
            : {
                icon: DocumentTextIcon,
                title: 'No engineering change orders',
                description: 'Engineering change orders you create will appear here.',
                action: { label: 'New ECO', onClick: openCreateModal },
              }
        }
      />

      {/* Results count */}
      {!loading && !error && (
        <p className="text-xs text-slate-500">
          Showing {filtered.length} of {ecos.length} record{ecos.length !== 1 ? 's' : ''}
        </p>
      )}

      {/* ── Create ECO Modal ──────────────────────────────────────── */}
      <Modal open={showCreateModal} onClose={() => setShowCreateModal(false)} size="lg" scroll={false} padded={false} closeOnBackdrop={false}>
            <div className="flex items-center justify-between border-b px-6 py-4">
              <h2 className="text-lg font-semibold text-white">New Engineering Change Order</h2>
              <button onClick={() => setShowCreateModal(false)}>
                <XMarkIcon className="h-5 w-5 text-slate-500 hover:text-slate-400" />
              </button>
            </div>
            <form onSubmit={handleCreate} className="space-y-4 px-6 py-4 max-h-[70vh] overflow-y-auto">
              <div>
                <label className="block text-sm font-medium text-slate-300">Title *</label>
                <input
                  type="text"
                  required
                  value={createForm.title}
                  onChange={(e) => setCreateForm((f) => ({ ...f, title: e.target.value }))}
                  className="mt-1 block w-full rounded-md border border-slate-600 px-3 py-2 text-sm focus:border-indigo-500 focus:ring-indigo-500"
                />
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="block text-sm font-medium text-slate-300">Type *</label>
                  <select
                    value={createForm.eco_type}
                    onChange={(e) => setCreateForm((f) => ({ ...f, eco_type: e.target.value as ECOType }))}
                    className="mt-1 block w-full rounded-md border border-slate-600 px-3 py-2 text-sm focus:border-indigo-500 focus:ring-indigo-500"
                  >
                    {Object.entries(typeLabel).map(([k, v]) => (
                      <option key={k} value={k}>{v}</option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className="block text-sm font-medium text-slate-300">Priority</label>
                  <select
                    value={createForm.priority}
                    onChange={(e) => setCreateForm((f) => ({ ...f, priority: e.target.value as ECOPriority }))}
                    className="mt-1 block w-full rounded-md border border-slate-600 px-3 py-2 text-sm focus:border-indigo-500 focus:ring-indigo-500"
                  >
                    {Object.entries(priorityLabel).map(([k, v]) => (
                      <option key={k} value={k}>{v}</option>
                    ))}
                  </select>
                </div>
              </div>
              <div>
                <label className="block text-sm font-medium text-slate-300">Description *</label>
                <textarea
                  required
                  rows={3}
                  value={createForm.description}
                  onChange={(e) => setCreateForm((f) => ({ ...f, description: e.target.value }))}
                  className="mt-1 block w-full rounded-md border border-slate-600 px-3 py-2 text-sm focus:border-indigo-500 focus:ring-indigo-500"
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-slate-300">Reason for Change *</label>
                <textarea
                  required
                  rows={2}
                  value={createForm.reason_for_change}
                  onChange={(e) => setCreateForm((f) => ({ ...f, reason_for_change: e.target.value }))}
                  className="mt-1 block w-full rounded-md border border-slate-600 px-3 py-2 text-sm focus:border-indigo-500 focus:ring-indigo-500"
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-slate-300">Proposed Solution</label>
                <textarea
                  rows={2}
                  value={createForm.proposed_solution}
                  onChange={(e) => setCreateForm((f) => ({ ...f, proposed_solution: e.target.value }))}
                  className="mt-1 block w-full rounded-md border border-slate-600 px-3 py-2 text-sm focus:border-indigo-500 focus:ring-indigo-500"
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-slate-300">Affected Part IDs</label>
                <input
                  type="text"
                  placeholder="e.g. 1, 2, 3"
                  value={createForm.affected_parts}
                  onChange={(e) => setCreateForm((f) => ({ ...f, affected_parts: e.target.value }))}
                  className="mt-1 block w-full rounded-md border border-slate-600 px-3 py-2 text-sm focus:border-indigo-500 focus:ring-indigo-500"
                />
                <p className="mt-1 text-xs text-slate-500">Comma-separated part IDs</p>
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="block text-sm font-medium text-slate-300">Estimated Cost</label>
                  <input
                    type="number"
                    step="0.01"
                    min="0"
                    value={createForm.estimated_cost}
                    onChange={(e) => setCreateForm((f) => ({ ...f, estimated_cost: parseFloat(e.target.value) || 0 }))}
                    className="mt-1 block w-full rounded-md border border-slate-600 px-3 py-2 text-sm focus:border-indigo-500 focus:ring-indigo-500"
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-slate-300">Target Date</label>
                  <input
                    type="date"
                    value={createForm.target_date}
                    onChange={(e) => setCreateForm((f) => ({ ...f, target_date: e.target.value }))}
                    className="mt-1 block w-full rounded-md border border-slate-600 px-3 py-2 text-sm focus:border-indigo-500 focus:ring-indigo-500"
                  />
                </div>
              </div>
              <div className="flex justify-end gap-3 border-t pt-4">
                <Button variant="secondary" onClick={() => setShowCreateModal(false)}>
                  Cancel
                </Button>
                <Button type="submit" disabled={createLoading} className="inline-flex items-center gap-2">
                  {createLoading && <ArrowPathIcon className="h-4 w-4 animate-spin" />}
                  Create ECO
                </Button>
              </div>
            </form>
      </Modal>

      {/* ── ECO Detail Modal (replaces the inline expand row) ─────── */}
      <Modal open={!!detailEcoLive} onClose={() => setDetailEco(null)} size="lg" scroll={false} padded={false}>
        {detailEcoLive && (
          <>
            <div className="flex items-start justify-between border-b px-6 py-4">
              <div className="min-w-0">
                <div className="flex items-center gap-2 flex-wrap">
                  <h2 className="text-lg font-semibold text-white font-mono">{detailEcoLive.eco_number}</h2>
                  {statusChip(detailEcoLive)}
                  {typeChip(detailEcoLive)}
                  {priorityChip(detailEcoLive)}
                </div>
                <p className="text-sm text-slate-400 mt-1 truncate">{detailEcoLive.title}</p>
              </div>
              <div className="flex items-center gap-3 pl-3">
                <EcoRowActions
                  eco={detailEcoLive}
                  h={{
                    onSubmit: handleSubmit,
                    onApprove: handleApprove,
                    onReject: openRejectModal,
                    onImplement: handleImplement,
                    onComplete: handleComplete,
                    actionLoading,
                  }}
                />
                <button onClick={() => setDetailEco(null)}>
                  <XMarkIcon className="h-5 w-5 text-slate-500 hover:text-slate-400" />
                </button>
              </div>
            </div>
            <div className="space-y-4 px-6 py-4 max-h-[70vh] overflow-y-auto">
              {/* Description & Reason */}
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div>
                  <h4 className="text-xs font-semibold uppercase text-slate-400 mb-1">Description</h4>
                  <p className="text-sm text-slate-300 whitespace-pre-wrap">{detailEcoLive.description}</p>
                </div>
                <div>
                  <h4 className="text-xs font-semibold uppercase text-slate-400 mb-1">Reason for Change</h4>
                  <p className="text-sm text-slate-300 whitespace-pre-wrap">{detailEcoLive.reason_for_change}</p>
                </div>
              </div>

              {/* Extra details */}
              <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
                {detailEcoLive.proposed_solution && (
                  <div><span className="text-xs font-semibold uppercase text-slate-400">Proposed Solution</span><p className="text-slate-300 mt-0.5">{detailEcoLive.proposed_solution}</p></div>
                )}
                <div><span className="text-xs font-semibold uppercase text-slate-400">Est. Cost</span><p className="text-slate-300 mt-0.5">{fmt(detailEcoLive.estimated_cost)}</p></div>
                {detailEcoLive.actual_cost > 0 && <div><span className="text-xs font-semibold uppercase text-slate-400">Actual Cost</span><p className="text-slate-300 mt-0.5">{fmt(detailEcoLive.actual_cost)}</p></div>}
                {detailEcoLive.target_date && <div><span className="text-xs font-semibold uppercase text-slate-400">Target Date</span><p className="text-slate-300 mt-0.5">{formatDate(detailEcoLive.target_date)}</p></div>}
                {detailEcoLive.assignee && <div><span className="text-xs font-semibold uppercase text-slate-400">Assigned To</span><p className="text-slate-300 mt-0.5">{userName(detailEcoLive.assignee)}</p></div>}
                {detailEcoLive.completed_date && <div><span className="text-xs font-semibold uppercase text-slate-400">Completed</span><p className="text-slate-300 mt-0.5">{formatDate(detailEcoLive.completed_date)}</p></div>}
                <div><span className="text-xs font-semibold uppercase text-slate-400">Requestor</span><p className="text-slate-300 mt-0.5">{userName(detailEcoLive.requester)}</p></div>
                <div><span className="text-xs font-semibold uppercase text-slate-400">Created</span><p className="text-slate-300 mt-0.5">{formatDate(detailEcoLive.created_at)}</p></div>
              </div>

              {/* Approval Workflow */}
              {detailEcoLive.approvals.length > 0 && (
                <div>
                  <h4 className="text-xs font-semibold uppercase text-slate-400 mb-2">Approval Workflow</h4>
                  <div className="rounded border bg-fd-panel divide-y">
                    {detailEcoLive.approvals.map((a) => (
                      <div key={a.id} className="flex items-center justify-between px-4 py-2 text-sm">
                        <div><span className="font-medium text-white">{userName(a.approver)}</span><span className="ml-2 text-slate-400">({a.role})</span></div>
                        <div className="flex items-center gap-3">
                          {a.comments && <span className="text-slate-400 italic text-xs max-w-[200px] truncate">{a.comments}</span>}
                          <span className={`inline-flex rounded-full px-2 py-0.5 text-xs font-medium ${a.status === 'approved' ? 'bg-green-500/20 text-green-300' : a.status === 'rejected' ? 'bg-red-500/20 text-red-300' : 'bg-yellow-500/20 text-yellow-300'}`}>
                            {a.status === 'approved' ? 'Approved' : a.status === 'rejected' ? 'Rejected' : 'Pending'}
                          </span>
                          {a.decision_date && <span className="text-xs text-slate-500">{formatDate(a.decision_date)}</span>}
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )}
              {/* Implementation Tasks */}
              {detailEcoLive.implementation_tasks.length > 0 && (
                <div>
                  <h4 className="text-xs font-semibold uppercase text-slate-400 mb-2">Implementation Tasks</h4>
                  <div className="rounded border bg-fd-panel divide-y">
                    {detailEcoLive.implementation_tasks.map((t) => (
                      <div key={t.id} className="flex items-center justify-between px-4 py-2 text-sm">
                        <div className="flex items-center gap-2">
                          <span className="text-slate-500 font-mono text-xs">#{t.task_number}</span>
                          <span className="text-white">{t.description}</span>
                          {t.department && <span className="text-xs text-slate-400 bg-slate-800 rounded px-1.5 py-0.5">{t.department}</span>}
                        </div>
                        <div className="flex items-center gap-3">
                          {t.assignee && <span className="text-xs text-slate-400">{userName(t.assignee)}</span>}
                          {t.due_date && <span className="text-xs text-slate-500">Due: {formatDate(t.due_date)}</span>}
                          <span className={`inline-flex rounded-full px-2 py-0.5 text-xs font-medium ${t.status === 'completed' ? 'bg-green-500/20 text-green-300' : t.status === 'in_progress' ? 'bg-blue-500/20 text-blue-300' : t.status === 'skipped' ? 'bg-slate-800 text-slate-100' : 'bg-yellow-500/20 text-yellow-300'}`}>
                            {taskStatusLabel[t.status] || t.status}
                          </span>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </>
        )}
      </Modal>

      {/* ── Reject Modal ──────────────────────────────────────────── */}
      <Modal open={showRejectModal} onClose={() => setShowRejectModal(false)} size="md" scroll={false} padded={false} closeOnBackdrop={false}>
            <div className="flex items-center justify-between border-b px-6 py-4">
              <h2 className="text-lg font-semibold text-white">Reject ECO</h2>
              <button onClick={() => setShowRejectModal(false)}>
                <XMarkIcon className="h-5 w-5 text-slate-500 hover:text-slate-400" />
              </button>
            </div>
            <div className="space-y-4 px-6 py-4">
              <div>
                <label className="block text-sm font-medium text-slate-300">Rejection Comments</label>
                <textarea
                  rows={3}
                  value={rejectComments}
                  onChange={(e) => setRejectComments(e.target.value)}
                  placeholder="Reason for rejection..."
                  className="mt-1 block w-full rounded-md border border-slate-600 px-3 py-2 text-sm focus:border-indigo-500 focus:ring-indigo-500"
                />
              </div>
              <div className="flex justify-end gap-3">
                <Button variant="secondary" onClick={() => setShowRejectModal(false)}>
                  Cancel
                </Button>
                <Button variant="danger" onClick={handleReject} disabled={actionLoading !== null}>
                  Reject ECO
                </Button>
              </div>
            </div>
      </Modal>
    </div>
  );
}
