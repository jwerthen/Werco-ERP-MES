import React, { useEffect, useState, useMemo, useCallback } from 'react';
import api from '../services/api';
import {
  ExclamationTriangleIcon,
  ClockIcon,
  ArrowPathIcon,
  CheckCircleIcon,
  PlusIcon,
  XMarkIcon,
  ChevronDownIcon,
  ChevronUpIcon,
  MagnifyingGlassIcon,
  FunnelIcon,
  DocumentTextIcon,
  TruckIcon,
} from '@heroicons/react/24/outline';
import { MiniStat, MiniStatStrip } from '../components/cockpit';
import { Modal } from '../components/ui/Modal';
import {
  useToast,
  DataTable,
  DataTableColumn,
  StatusBadge,
  MobileDataCard,
  FormField,
} from '../components/ui';
import { formatCentralDate, getCentralTodayISODate } from '../utils/centralTime';

// ── Types ────────────────────────────────────────────────────────

type ComplaintSeverity = 'minor' | 'major' | 'critical';
type ComplaintStatus =
  | 'received'
  | 'under_investigation'
  | 'pending_resolution'
  | 'resolved'
  | 'closed'
  | 'rejected';
type RMAStatus =
  | 'requested'
  | 'approved'
  | 'denied'
  | 'material_received'
  | 'under_inspection'
  | 'disposition_decided'
  | 'completed';

interface PartSummary {
  id: number;
  part_number: string;
  name: string;
}

interface CustomerSummary {
  id: number;
  name: string;
}

interface RMABrief {
  id: number;
  rma_number: string;
  status: RMAStatus;
  quantity: number;
  disposition: string | null;
}

interface Complaint {
  id: number;
  complaint_number: string;
  customer_id: number | null;
  customer_name: string;
  customer_po_number: string | null;
  customer_contact: string | null;
  part_id: number | null;
  part: PartSummary | null;
  customer: CustomerSummary | null;
  work_order_id: number | null;
  lot_number: string | null;
  serial_number: string | null;
  quantity_affected: number;
  severity: ComplaintSeverity;
  status: ComplaintStatus;
  title: string;
  description: string;
  date_received: string | null;
  date_of_occurrence: string | null;
  investigation_findings: string | null;
  root_cause: string | null;
  containment_action: string | null;
  corrective_action: string | null;
  preventive_action: string | null;
  resolution_description: string | null;
  ncr_id: number | null;
  car_id: number | null;
  estimated_cost: number;
  actual_cost: number;
  assigned_to: number | null;
  received_by: number | null;
  resolved_date: string | null;
  closed_date: string | null;
  customer_satisfied: boolean | null;
  satisfaction_notes: string | null;
  rmas: RMABrief[];
  created_at: string;
  updated_at: string | null;
}

interface Dashboard {
  open_complaints: number;
  avg_resolution_days: number | null;
  by_severity: Record<string, number>;
  by_customer: { customer: string; count: number }[];
  satisfaction_rate: number | null;
  trend: { year: number; month: number; count: number }[];
  open_rmas: number;
}

interface ComplaintCreateForm {
  customer_name: string;
  customer_po_number: string;
  customer_contact: string;
  lot_number: string;
  serial_number: string;
  quantity_affected: number;
  severity: ComplaintSeverity;
  title: string;
  description: string;
  date_received: string;
  date_of_occurrence: string;
  estimated_cost: number;
}

interface RMACreateForm {
  customer_name: string;
  quantity: number;
  lot_number: string;
  reason: string;
  notes: string;
}

// ── Helpers ──────────────────────────────────────────────────────

// Central-local "today" (YYYY-MM-DD) so date-only form defaults don't roll to
// tomorrow on a Central evening (UTC midnight).
const todayISO = () => getCentralTodayISODate();

const severityBadge: Record<ComplaintSeverity, string> = {
  minor: 'bg-yellow-500/20 text-yellow-300',
  major: 'bg-orange-500/20 text-orange-300',
  critical: 'bg-red-500/20 text-red-300',
};

const severityLabel: Record<ComplaintSeverity, string> = {
  minor: 'Minor',
  major: 'Major',
  critical: 'Critical',
};

const statusBadge: Record<ComplaintStatus, string> = {
  received: 'bg-blue-500/20 text-blue-300',
  under_investigation: 'bg-purple-500/20 text-purple-300',
  pending_resolution: 'bg-yellow-500/20 text-yellow-300',
  resolved: 'bg-green-500/20 text-green-300',
  closed: 'bg-slate-800 text-slate-100',
  rejected: 'bg-red-500/20 text-red-300',
};

const statusLabel: Record<ComplaintStatus, string> = {
  received: 'Received',
  under_investigation: 'Under Investigation',
  pending_resolution: 'Pending Resolution',
  resolved: 'Resolved',
  closed: 'Closed',
  rejected: 'Rejected',
};

const rmaStatusLabel: Record<RMAStatus, string> = {
  requested: 'Requested',
  approved: 'Approved',
  denied: 'Denied',
  material_received: 'Material Received',
  under_inspection: 'Under Inspection',
  disposition_decided: 'Disposition Decided',
  completed: 'Completed',
};

// Shop-local Central date; '-' fallback matches centralTime's default.
const formatDate = (d: string | null) => formatCentralDate(d);

const fmt = (n: number) =>
  new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(n);

// ── Component ────────────────────────────────────────────────────

export default function CustomerComplaints() {
  const { showToast } = useToast();

  // Data
  const [complaints, setComplaints] = useState<Complaint[]>([]);
  const [dashboard, setDashboard] = useState<Dashboard | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Filters
  const [searchTerm, setSearchTerm] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [severityFilter, setSeverityFilter] = useState('');

  // Expand
  const [expandedId, setExpandedId] = useState<number | null>(null);

  // Create complaint modal
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [createForm, setCreateForm] = useState<ComplaintCreateForm>({
    customer_name: '',
    customer_po_number: '',
    customer_contact: '',
    lot_number: '',
    serial_number: '',
    quantity_affected: 1,
    severity: 'minor',
    title: '',
    description: '',
    date_received: todayISO(),
    date_of_occurrence: '',
    estimated_cost: 0,
  });
  const [createLoading, setCreateLoading] = useState(false);

  // RMA modal
  const [showRMAModal, setShowRMAModal] = useState(false);
  const [rmaComplaint, setRmaComplaint] = useState<Complaint | null>(null);
  const [rmaForm, setRmaForm] = useState<RMACreateForm>({
    customer_name: '',
    quantity: 1,
    lot_number: '',
    reason: '',
    notes: '',
  });
  const [rmaLoading, setRmaLoading] = useState(false);

  // ── Data fetching ──────────────────────────────────────────────

  const loadComplaints = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);
      const params: Record<string, string> = {};
      if (statusFilter) params.status = statusFilter;
      if (severityFilter) params.severity = severityFilter;
      const response = await api.getComplaints(params);
      setComplaints(response);
    } catch (err: any) {
      console.error('Failed to load complaints:', err);
      setError(err?.response?.data?.detail || 'Failed to load complaints');
    } finally {
      setLoading(false);
    }
  }, [statusFilter, severityFilter]);

  const loadDashboard = useCallback(async () => {
    try {
      const response = await api.getComplaintsDashboard();
      setDashboard(response);
    } catch (err) {
      console.error('Failed to load dashboard:', err);
    }
  }, []);

  useEffect(() => {
    loadComplaints();
    loadDashboard();
  }, [loadComplaints, loadDashboard]);

  // ── Filtered data ──────────────────────────────────────────────

  const filtered = useMemo(() => {
    if (!searchTerm) return complaints;
    const term = searchTerm.toLowerCase();
    return complaints.filter((c) => {
      return (
        c.complaint_number.toLowerCase().includes(term) ||
        c.customer_name.toLowerCase().includes(term) ||
        c.title.toLowerCase().includes(term) ||
        c.description.toLowerCase().includes(term) ||
        (c.part?.part_number || '').toLowerCase().includes(term) ||
        (c.lot_number || '').toLowerCase().includes(term)
      );
    });
  }, [complaints, searchTerm]);

  // ── Row expand ─────────────────────────────────────────────────

  const toggleExpand = useCallback(
    (id: number) => {
      setExpandedId((prev) => (prev === id ? null : id));
    },
    []
  );

  // ── Create complaint ──────────────────────────────────────────

  const openCreateModal = useCallback(() => {
    setCreateForm({
      customer_name: '',
      customer_po_number: '',
      customer_contact: '',
      lot_number: '',
      serial_number: '',
      quantity_affected: 1,
      severity: 'minor',
      title: '',
      description: '',
      date_received: todayISO(),
      date_of_occurrence: '',
      estimated_cost: 0,
    });
    setShowCreateModal(true);
  }, []);

  const handleCreate = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();
      if (!createForm.customer_name.trim()) {
        showToast('error', 'Customer name is required');
        return;
      }
      if (!createForm.title.trim() || createForm.title.trim().length < 3) {
        showToast('error', 'Title must be at least 3 characters');
        return;
      }
      if (!createForm.description.trim() || createForm.description.trim().length < 5) {
        showToast('error', 'Description must be at least 5 characters');
        return;
      }
      try {
        setCreateLoading(true);
        const payload: Record<string, unknown> = {
          customer_name: createForm.customer_name,
          quantity_affected: createForm.quantity_affected,
          severity: createForm.severity,
          title: createForm.title,
          description: createForm.description,
          estimated_cost: createForm.estimated_cost,
        };
        if (createForm.customer_po_number) payload.customer_po_number = createForm.customer_po_number;
        if (createForm.customer_contact) payload.customer_contact = createForm.customer_contact;
        if (createForm.lot_number) payload.lot_number = createForm.lot_number;
        if (createForm.serial_number) payload.serial_number = createForm.serial_number;
        if (createForm.date_received) payload.date_received = createForm.date_received;
        if (createForm.date_of_occurrence) payload.date_of_occurrence = createForm.date_of_occurrence;

        await api.createComplaint(payload);
        setShowCreateModal(false);
        showToast('success', 'Complaint created');
        loadComplaints();
        loadDashboard();
      } catch (err: any) {
        showToast('error', err?.response?.data?.detail || 'Failed to create complaint');
      } finally {
        setCreateLoading(false);
      }
    },
    [createForm, loadComplaints, loadDashboard, showToast]
  );

  // ── Create RMA from complaint ─────────────────────────────────

  const openRMAModal = useCallback((complaint: Complaint) => {
    setRmaComplaint(complaint);
    setRmaForm({
      customer_name: complaint.customer_name,
      quantity: complaint.quantity_affected,
      lot_number: complaint.lot_number || '',
      reason: complaint.description,
      notes: '',
    });
    setShowRMAModal(true);
  }, []);

  const handleCreateRMA = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();
      if (!rmaComplaint) return;
      if (!rmaForm.reason.trim() || rmaForm.reason.trim().length < 5) {
        showToast('error', 'Reason must be at least 5 characters');
        return;
      }
      try {
        setRmaLoading(true);
        const payload: Record<string, unknown> = {
          complaint_id: rmaComplaint.id,
          customer_name: rmaForm.customer_name,
          quantity: rmaForm.quantity,
          reason: rmaForm.reason,
        };
        if (rmaComplaint.customer_id) payload.customer_id = rmaComplaint.customer_id;
        if (rmaComplaint.part_id) payload.part_id = rmaComplaint.part_id;
        if (rmaForm.lot_number) payload.lot_number = rmaForm.lot_number;
        if (rmaForm.notes) payload.notes = rmaForm.notes;

        await api.createRMA(payload);
        setShowRMAModal(false);
        showToast('success', 'RMA created');
        loadComplaints();
        loadDashboard();
      } catch (err: any) {
        showToast('error', err?.response?.data?.detail || 'Failed to create RMA');
      } finally {
        setRmaLoading(false);
      }
    },
    [rmaComplaint, rmaForm, loadComplaints, loadDashboard, showToast]
  );

  // ── Table columns ──────────────────────────────────────────────

  const columns = useMemo<Array<DataTableColumn<Complaint>>>(
    () => [
      {
        key: 'expand',
        header: '',
        align: 'center',
        headerClassName: 'w-8',
        render: (c) =>
          expandedId === c.id ? (
            <ChevronUpIcon className="h-4 w-4 text-slate-400" />
          ) : (
            <ChevronDownIcon className="h-4 w-4 text-slate-400" />
          ),
      },
      {
        key: 'complaint_number',
        header: 'Complaint #',
        sortable: true,
        className: 'font-medium',
        accessor: (c) => c.complaint_number,
      },
      {
        key: 'customer',
        header: 'Customer',
        sortable: true,
        accessor: (c) => c.customer_name,
        csv: (c) => c.customer_name,
        render: (c) => (
          <div>
            <div className="font-medium">{c.customer_name}</div>
            {c.customer_contact && (
              <div className="text-xs text-slate-400">{c.customer_contact}</div>
            )}
          </div>
        ),
      },
      {
        key: 'date',
        header: 'Date',
        sortable: true,
        accessor: (c) => c.date_received ?? '',
        csv: (c) => formatDate(c.date_received),
        render: (c) => formatDate(c.date_received),
      },
      {
        key: 'severity',
        header: 'Severity',
        sortable: true,
        accessor: (c) => c.severity,
        csv: (c) => severityLabel[c.severity],
        render: (c) => (
          <StatusBadge status={c.severity} colorMap={severityBadge} className="capitalize" />
        ),
      },
      {
        key: 'status',
        header: 'Status',
        sortable: true,
        accessor: (c) => c.status,
        csv: (c) => statusLabel[c.status],
        render: (c) => (
          <span
            className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${statusBadge[c.status]}`}
          >
            {statusLabel[c.status]}
          </span>
        ),
      },
      {
        key: 'description',
        header: 'Description',
        accessor: (c) => c.title,
        csv: (c) => `${c.title} — ${c.description}`,
        className: 'max-w-xs',
        render: (c) => (
          <div className="max-w-xs">
            <div className="text-sm font-medium truncate">{c.title}</div>
            <div className="text-xs text-slate-400 truncate">{c.description}</div>
          </div>
        ),
      },
      {
        key: 'rmas',
        header: 'RMAs',
        sortable: true,
        align: 'center',
        accessor: (c) => c.rmas.length,
      },
      {
        key: 'estimated_cost',
        header: 'Est. Cost',
        sortable: true,
        align: 'right',
        className: 'font-mono',
        accessor: (c) => c.estimated_cost,
        csv: (c) => c.estimated_cost,
        render: (c) => fmt(c.estimated_cost),
      },
    ],
    [expandedId]
  );

  // ── Mobile card renderer ───────────────────────────────────────

  const renderMobileCard = useCallback(
    (c: Complaint) => (
      <MobileDataCard
        title={c.complaint_number}
        subtitle={c.title}
        onClick={() => toggleExpand(c.id)}
        badge={
          <span
            className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${statusBadge[c.status]}`}
          >
            {statusLabel[c.status]}
          </span>
        }
        fields={[
          { label: 'Customer', value: c.customer_name },
          {
            label: 'Severity',
            value: (
              <StatusBadge status={c.severity} colorMap={severityBadge} className="capitalize" />
            ),
          },
          { label: 'Date', value: formatDate(c.date_received) },
          { label: 'RMAs', value: c.rmas.length },
          { label: 'Est. Cost', value: <span className="font-mono">{fmt(c.estimated_cost)}</span> },
        ]}
      />
    ),
    [toggleExpand]
  );

  // ── Render ─────────────────────────────────────────────────────

  const activeFilter = !!(searchTerm || statusFilter || severityFilter);
  const expandedComplaint = useMemo(
    () => (expandedId === null ? null : filtered.find((c) => c.id === expandedId) ?? null),
    [expandedId, filtered]
  );

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div className="flex justify-between items-center">
        <div>
          <h1 className="text-2xl font-bold text-white">
            Customer Complaints & RMA Management
          </h1>
          <p className="text-sm text-slate-400 mt-1">
            Track, investigate, and resolve customer complaints and return material authorizations
          </p>
        </div>
        <button
          onClick={openCreateModal}
          className="du-btn du-btn-primary du-btn-sm gap-1"
        >
          <PlusIcon className="h-4 w-4" />
          New Complaint
        </button>
      </div>

      {/* Summary Cards */}
      {dashboard && (
        <MiniStatStrip className="grid grid-cols-2 lg:grid-cols-4 gap-2">
          <MiniStat
            icon={ExclamationTriangleIcon}
            iconBg="bg-fd-red/15"
            iconColor="text-fd-red"
            label="Open Complaints"
            value={dashboard.open_complaints}
          />
          <MiniStat
            icon={ClockIcon}
            iconBg="bg-fd-blue/15"
            iconColor="text-fd-blue"
            label="Avg Resolution Time"
            value={
              dashboard.avg_resolution_days != null
                ? `${dashboard.avg_resolution_days} days`
                : 'N/A'
            }
          />
          <MiniStat
            icon={TruckIcon}
            iconBg="bg-werco-navy/15"
            iconColor="text-werco-navy"
            label="Open RMAs"
            value={dashboard.open_rmas}
          />
          <MiniStat
            icon={CheckCircleIcon}
            iconBg="bg-fd-green/15"
            iconColor="text-fd-green"
            label="Satisfaction Rate"
            value={
              dashboard.satisfaction_rate != null
                ? `${dashboard.satisfaction_rate}%`
                : 'N/A'
            }
          />
        </MiniStatStrip>
      )}

      {/* Filters */}
      <div className="flex flex-col sm:flex-row sm:items-center gap-3 rounded-sm border border-fd-line bg-fd-panel p-2.5">
        <div className="relative flex-1 max-w-sm">
          <MagnifyingGlassIcon className="h-4 w-4 absolute left-3 top-1/2 -translate-y-1/2 text-slate-500" />
          <input
            type="text"
            placeholder="Search complaints..."
            aria-label="Search complaints"
            value={searchTerm}
            onChange={(e) => setSearchTerm(e.target.value)}
            className="du-input du-input-bordered du-input-sm w-full pl-9"
          />
        </div>

        <div className="flex items-center gap-2">
          <FunnelIcon className="h-4 w-4 text-slate-500" />
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            className="du-select du-select-bordered du-select-sm"
          >
            <option value="">All Statuses</option>
            <option value="received">Received</option>
            <option value="under_investigation">Under Investigation</option>
            <option value="pending_resolution">Pending Resolution</option>
            <option value="resolved">Resolved</option>
            <option value="closed">Closed</option>
            <option value="rejected">Rejected</option>
          </select>

          <select
            value={severityFilter}
            onChange={(e) => setSeverityFilter(e.target.value)}
            className="du-select du-select-bordered du-select-sm"
          >
            <option value="">All Severities</option>
            <option value="minor">Minor</option>
            <option value="major">Major</option>
            <option value="critical">Critical</option>
          </select>

          <button
            onClick={() => {
              loadComplaints();
              loadDashboard();
            }}
            className="du-btn du-btn-ghost du-btn-sm"
            title="Refresh"
          >
            <ArrowPathIcon className="h-4 w-4" />
          </button>
        </div>
      </div>

      {/* Table */}
      <DataTable<Complaint>
        columns={columns}
        data={filtered}
        rowKey={(c) => c.id}
        onRowClick={(c) => toggleExpand(c.id)}
        loading={loading}
        error={error ?? false}
        onRetry={loadComplaints}
        defaultSort={{ key: 'date', dir: 'desc' }}
        pageSize={25}
        csvExport={{ filename: 'customer-complaints' }}
        mobileCards={renderMobileCard}
        empty={{
          icon: ExclamationTriangleIcon,
          title: activeFilter ? 'No matching complaints' : 'No complaints found',
          description: activeFilter
            ? 'Try adjusting your search or filters.'
            : 'Logged customer complaints will appear here.',
          action: activeFilter ? undefined : { label: 'New Complaint', onClick: openCreateModal },
        }}
      />

      {/* Expanded detail panel — shown for the selected complaint, preserving the
          previous inline-expand detail grid, nested RMA table, and row actions. */}
      {expandedComplaint && (
        <div className="rounded-sm border border-fd-line bg-fd-panel">
          <div className="flex items-center justify-between border-b border-fd-line px-4 py-2.5">
            <h3 className="text-sm font-semibold text-white">
              {expandedComplaint.complaint_number} — {expandedComplaint.title}
            </h3>
            <button
              onClick={() => setExpandedId(null)}
              className="du-btn du-btn-xs du-btn-circle du-btn-ghost"
              aria-label="Collapse detail"
            >
              <XMarkIcon className="h-4 w-4" />
            </button>
          </div>
          <div className="p-4 space-y-4">
            {/* Detail grid */}
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              <div className="space-y-2">
                <h4 className="font-semibold text-sm text-slate-300">Complaint Details</h4>
                <dl className="text-sm space-y-1">
                  <div className="flex justify-between">
                    <dt className="text-slate-400">PO Number:</dt>
                    <dd>{expandedComplaint.customer_po_number || '-'}</dd>
                  </div>
                  <div className="flex justify-between">
                    <dt className="text-slate-400">Part:</dt>
                    <dd>
                      {expandedComplaint.part
                        ? `${expandedComplaint.part.part_number} - ${expandedComplaint.part.name}`
                        : '-'}
                    </dd>
                  </div>
                  <div className="flex justify-between">
                    <dt className="text-slate-400">Lot #:</dt>
                    <dd>{expandedComplaint.lot_number || '-'}</dd>
                  </div>
                  <div className="flex justify-between">
                    <dt className="text-slate-400">Serial #:</dt>
                    <dd>{expandedComplaint.serial_number || '-'}</dd>
                  </div>
                  <div className="flex justify-between">
                    <dt className="text-slate-400">Qty Affected:</dt>
                    <dd>{expandedComplaint.quantity_affected}</dd>
                  </div>
                  <div className="flex justify-between">
                    <dt className="text-slate-400">Occurrence Date:</dt>
                    <dd>{formatDate(expandedComplaint.date_of_occurrence)}</dd>
                  </div>
                </dl>
              </div>

              <div className="space-y-2">
                <h4 className="font-semibold text-sm text-slate-300">Investigation & Resolution</h4>
                <dl className="text-sm space-y-1">
                  <div>
                    <dt className="text-slate-400">Investigation Findings:</dt>
                    <dd className="mt-0.5">{expandedComplaint.investigation_findings || '-'}</dd>
                  </div>
                  <div>
                    <dt className="text-slate-400">Root Cause:</dt>
                    <dd className="mt-0.5">{expandedComplaint.root_cause || '-'}</dd>
                  </div>
                  <div>
                    <dt className="text-slate-400">Containment Action:</dt>
                    <dd className="mt-0.5">{expandedComplaint.containment_action || '-'}</dd>
                  </div>
                  <div>
                    <dt className="text-slate-400">Corrective Action:</dt>
                    <dd className="mt-0.5">{expandedComplaint.corrective_action || '-'}</dd>
                  </div>
                  <div>
                    <dt className="text-slate-400">Resolution:</dt>
                    <dd className="mt-0.5">{expandedComplaint.resolution_description || '-'}</dd>
                  </div>
                </dl>
              </div>

              <div className="space-y-2">
                <h4 className="font-semibold text-sm text-slate-300">Financial & Status</h4>
                <dl className="text-sm space-y-1">
                  <div className="flex justify-between">
                    <dt className="text-slate-400">Estimated Cost:</dt>
                    <dd className="font-mono">{fmt(expandedComplaint.estimated_cost)}</dd>
                  </div>
                  <div className="flex justify-between">
                    <dt className="text-slate-400">Actual Cost:</dt>
                    <dd className="font-mono">{fmt(expandedComplaint.actual_cost)}</dd>
                  </div>
                  <div className="flex justify-between">
                    <dt className="text-slate-400">Resolved Date:</dt>
                    <dd>{formatDate(expandedComplaint.resolved_date)}</dd>
                  </div>
                  <div className="flex justify-between">
                    <dt className="text-slate-400">Closed Date:</dt>
                    <dd>{formatDate(expandedComplaint.closed_date)}</dd>
                  </div>
                  <div className="flex justify-between">
                    <dt className="text-slate-400">Customer Satisfied:</dt>
                    <dd>
                      {expandedComplaint.customer_satisfied === null
                        ? '-'
                        : expandedComplaint.customer_satisfied
                          ? 'Yes'
                          : 'No'}
                    </dd>
                  </div>
                  {expandedComplaint.satisfaction_notes && (
                    <div>
                      <dt className="text-slate-400">Satisfaction Notes:</dt>
                      <dd className="mt-0.5">{expandedComplaint.satisfaction_notes}</dd>
                    </div>
                  )}
                  <div className="flex justify-between">
                    <dt className="text-slate-400">NCR ID:</dt>
                    <dd>{expandedComplaint.ncr_id || '-'}</dd>
                  </div>
                  <div className="flex justify-between">
                    <dt className="text-slate-400">CAR ID:</dt>
                    <dd>{expandedComplaint.car_id || '-'}</dd>
                  </div>
                </dl>
              </div>
            </div>

            {/* RMAs table */}
            {expandedComplaint.rmas.length > 0 && (
              <div>
                <h4 className="font-semibold text-sm text-slate-300 mb-2">
                  Return Material Authorizations
                </h4>
                <table className="du-table du-table-sm w-full">
                  <thead>
                    <tr>
                      <th>RMA #</th>
                      <th>Status</th>
                      <th>Quantity</th>
                      <th>Disposition</th>
                    </tr>
                  </thead>
                  <tbody>
                    {expandedComplaint.rmas.map((rma) => (
                      <tr key={rma.id}>
                        <td className="font-medium text-sm">{rma.rma_number}</td>
                        <td>
                          <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-blue-500/20 text-blue-300">
                            {rmaStatusLabel[rma.status] || rma.status}
                          </span>
                        </td>
                        <td>{rma.quantity}</td>
                        <td>{rma.disposition || '-'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {/* Action buttons */}
            <div className="flex gap-2 pt-2 border-t border-base-300">
              <button
                onClick={() => openRMAModal(expandedComplaint)}
                className="du-btn du-btn-sm du-btn-outline gap-1"
              >
                <TruckIcon className="h-4 w-4" />
                Create RMA
              </button>
              <button
                onClick={() => setExpandedId(null)}
                className="du-btn du-btn-sm du-btn-ghost gap-1"
              >
                <DocumentTextIcon className="h-4 w-4" />
                Full Details
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Results count */}
      {!loading && (
        <div className="text-sm text-slate-400">
          Showing {filtered.length} of {complaints.length} complaints
        </div>
      )}

      {/* ── Create Complaint Modal ────────────────────────────────── */}
      <Modal open={showCreateModal} onClose={() => setShowCreateModal(false)} size="2xl">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-lg font-bold">New Customer Complaint</h3>
          <button
            onClick={() => setShowCreateModal(false)}
            className="du-btn du-btn-sm du-btn-circle du-btn-ghost"
          >
            <XMarkIcon className="h-5 w-5" />
          </button>
        </div>

        <form onSubmit={handleCreate} className="space-y-4">
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <FormField
                  label={<span className="du-label-text">Customer Name</span>}
                  required
                  labelClassName="du-label"
                >
                  {(field) => (
                    <input
                      {...field}
                      type="text"
                      value={createForm.customer_name}
                      onChange={(e) =>
                        setCreateForm((f) => ({ ...f, customer_name: e.target.value }))
                      }
                      className="du-input du-input-bordered w-full"
                      required
                    />
                  )}
                </FormField>
                <FormField
                  label={<span className="du-label-text">Customer PO Number</span>}
                  labelClassName="du-label"
                >
                  {(field) => (
                    <input
                      {...field}
                      type="text"
                      value={createForm.customer_po_number}
                      onChange={(e) =>
                        setCreateForm((f) => ({ ...f, customer_po_number: e.target.value }))
                      }
                      className="du-input du-input-bordered w-full"
                    />
                  )}
                </FormField>
                <FormField
                  label={<span className="du-label-text">Customer Contact</span>}
                  labelClassName="du-label"
                >
                  {(field) => (
                    <input
                      {...field}
                      type="text"
                      value={createForm.customer_contact}
                      onChange={(e) =>
                        setCreateForm((f) => ({ ...f, customer_contact: e.target.value }))
                      }
                      className="du-input du-input-bordered w-full"
                    />
                  )}
                </FormField>
                <FormField
                  label={<span className="du-label-text">Severity</span>}
                  labelClassName="du-label"
                >
                  {(field) => (
                    <select
                      {...field}
                      value={createForm.severity}
                      onChange={(e) =>
                        setCreateForm((f) => ({
                          ...f,
                          severity: e.target.value as ComplaintSeverity,
                        }))
                      }
                      className="du-select du-select-bordered w-full"
                    >
                      <option value="minor">Minor</option>
                      <option value="major">Major</option>
                      <option value="critical">Critical</option>
                    </select>
                  )}
                </FormField>
                <FormField
                  label={<span className="du-label-text">Lot Number</span>}
                  labelClassName="du-label"
                >
                  {(field) => (
                    <input
                      {...field}
                      type="text"
                      value={createForm.lot_number}
                      onChange={(e) =>
                        setCreateForm((f) => ({ ...f, lot_number: e.target.value }))
                      }
                      className="du-input du-input-bordered w-full"
                    />
                  )}
                </FormField>
                <FormField
                  label={<span className="du-label-text">Serial Number</span>}
                  labelClassName="du-label"
                >
                  {(field) => (
                    <input
                      {...field}
                      type="text"
                      value={createForm.serial_number}
                      onChange={(e) =>
                        setCreateForm((f) => ({ ...f, serial_number: e.target.value }))
                      }
                      className="du-input du-input-bordered w-full"
                    />
                  )}
                </FormField>
                <FormField
                  label={<span className="du-label-text">Quantity Affected</span>}
                  labelClassName="du-label"
                >
                  {(field) => (
                    <input
                      {...field}
                      type="number"
                      min={1}
                      step="any"
                      value={createForm.quantity_affected}
                      onChange={(e) =>
                        setCreateForm((f) => ({
                          ...f,
                          quantity_affected: parseFloat(e.target.value) || 1,
                        }))
                      }
                      className="du-input du-input-bordered w-full"
                    />
                  )}
                </FormField>
                <FormField
                  label={<span className="du-label-text">Estimated Cost ($)</span>}
                  labelClassName="du-label"
                >
                  {(field) => (
                    <input
                      {...field}
                      type="number"
                      min={0}
                      step="0.01"
                      value={createForm.estimated_cost}
                      onChange={(e) =>
                        setCreateForm((f) => ({
                          ...f,
                          estimated_cost: parseFloat(e.target.value) || 0,
                        }))
                      }
                      className="du-input du-input-bordered w-full"
                    />
                  )}
                </FormField>
                <FormField
                  label={<span className="du-label-text">Date Received</span>}
                  labelClassName="du-label"
                >
                  {(field) => (
                    <input
                      {...field}
                      type="date"
                      value={createForm.date_received}
                      onChange={(e) =>
                        setCreateForm((f) => ({ ...f, date_received: e.target.value }))
                      }
                      className="du-input du-input-bordered w-full"
                    />
                  )}
                </FormField>
                <FormField
                  label={<span className="du-label-text">Date of Occurrence</span>}
                  labelClassName="du-label"
                >
                  {(field) => (
                    <input
                      {...field}
                      type="date"
                      value={createForm.date_of_occurrence}
                      onChange={(e) =>
                        setCreateForm((f) => ({ ...f, date_of_occurrence: e.target.value }))
                      }
                      className="du-input du-input-bordered w-full"
                    />
                  )}
                </FormField>
              </div>

              <FormField
                label={<span className="du-label-text">Title</span>}
                required
                labelClassName="du-label"
              >
                {(field) => (
                  <input
                    {...field}
                    type="text"
                    value={createForm.title}
                    onChange={(e) =>
                      setCreateForm((f) => ({ ...f, title: e.target.value }))
                    }
                    className="du-input du-input-bordered w-full"
                    required
                    minLength={3}
                  />
                )}
              </FormField>

              <FormField
                label={<span className="du-label-text">Description</span>}
                required
                labelClassName="du-label"
              >
                {(field) => (
                  <textarea
                    {...field}
                    value={createForm.description}
                    onChange={(e) =>
                      setCreateForm((f) => ({ ...f, description: e.target.value }))
                    }
                    className="du-textarea du-textarea-bordered w-full h-24"
                    required
                    minLength={5}
                  />
                )}
              </FormField>

              <div className="du-modal-action">
                <button
                  type="button"
                  onClick={() => setShowCreateModal(false)}
                  className="du-btn du-btn-ghost"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  className="du-btn du-btn-primary"
                  disabled={createLoading}
                >
                  {createLoading ? (
                    <span className="du-loading du-loading-spinner du-loading-sm" />
                  ) : (
                    'Create Complaint'
                  )}
                </button>
              </div>
        </form>
      </Modal>

      {/* ── Create RMA Modal ──────────────────────────────────────── */}
      {rmaComplaint && (
        <Modal open={showRMAModal} onClose={() => setShowRMAModal(false)} size="lg">
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-lg font-bold">
              Create RMA from {rmaComplaint.complaint_number}
            </h3>
            <button
              onClick={() => setShowRMAModal(false)}
              className="du-btn du-btn-sm du-btn-circle du-btn-ghost"
            >
              <XMarkIcon className="h-5 w-5" />
            </button>
          </div>

          <form onSubmit={handleCreateRMA} className="space-y-4">
              <FormField
                label={<span className="du-label-text">Customer Name</span>}
                labelClassName="du-label"
              >
                {(field) => (
                  <input
                    {...field}
                    type="text"
                    value={rmaForm.customer_name}
                    onChange={(e) =>
                      setRmaForm((f) => ({ ...f, customer_name: e.target.value }))
                    }
                    className="du-input du-input-bordered w-full"
                    required
                  />
                )}
              </FormField>

              <div className="grid grid-cols-2 gap-4">
                <FormField
                  label={<span className="du-label-text">Quantity</span>}
                  labelClassName="du-label"
                >
                  {(field) => (
                    <input
                      {...field}
                      type="number"
                      min={1}
                      step="any"
                      value={rmaForm.quantity}
                      onChange={(e) =>
                        setRmaForm((f) => ({
                          ...f,
                          quantity: parseFloat(e.target.value) || 1,
                        }))
                      }
                      className="du-input du-input-bordered w-full"
                      required
                    />
                  )}
                </FormField>
                <FormField
                  label={<span className="du-label-text">Lot Number</span>}
                  labelClassName="du-label"
                >
                  {(field) => (
                    <input
                      {...field}
                      type="text"
                      value={rmaForm.lot_number}
                      onChange={(e) =>
                        setRmaForm((f) => ({ ...f, lot_number: e.target.value }))
                      }
                      className="du-input du-input-bordered w-full"
                    />
                  )}
                </FormField>
              </div>

              <FormField
                label={<span className="du-label-text">Reason</span>}
                required
                labelClassName="du-label"
              >
                {(field) => (
                  <textarea
                    {...field}
                    value={rmaForm.reason}
                    onChange={(e) =>
                      setRmaForm((f) => ({ ...f, reason: e.target.value }))
                    }
                    className="du-textarea du-textarea-bordered w-full h-20"
                    required
                    minLength={5}
                  />
                )}
              </FormField>

              <FormField
                label={<span className="du-label-text">Notes</span>}
                labelClassName="du-label"
              >
                {(field) => (
                  <textarea
                    {...field}
                    value={rmaForm.notes}
                    onChange={(e) =>
                      setRmaForm((f) => ({ ...f, notes: e.target.value }))
                    }
                    className="du-textarea du-textarea-bordered w-full h-16"
                  />
                )}
              </FormField>

              <div className="du-modal-action">
                <button
                  type="button"
                  onClick={() => setShowRMAModal(false)}
                  className="du-btn du-btn-ghost"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  className="du-btn du-btn-primary"
                  disabled={rmaLoading}
                >
                  {rmaLoading ? (
                    <span className="du-loading du-loading-spinner du-loading-sm" />
                  ) : (
                    'Create RMA'
                  )}
                </button>
              </div>
          </form>
        </Modal>
      )}
    </div>
  );
}
