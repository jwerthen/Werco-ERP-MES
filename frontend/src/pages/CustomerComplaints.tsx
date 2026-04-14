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
import { SkeletonTable } from '../components/ui/Skeleton';

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

const todayISO = () => new Date().toISOString().split('T')[0];

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

// ── Component ────────────────────────────────────────────────────

export default function CustomerComplaints() {
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
      const response = await api.get<Complaint[]>('/complaints/', { params });
      setComplaints(response.data);
    } catch (err: any) {
      console.error('Failed to load complaints:', err);
      setError(err?.response?.data?.detail || 'Failed to load complaints');
    } finally {
      setLoading(false);
    }
  }, [statusFilter, severityFilter]);

  const loadDashboard = useCallback(async () => {
    try {
      const response = await api.get<Dashboard>('/complaints/dashboard');
      setDashboard(response.data);
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
        alert('Customer name is required');
        return;
      }
      if (!createForm.title.trim() || createForm.title.trim().length < 3) {
        alert('Title must be at least 3 characters');
        return;
      }
      if (!createForm.description.trim() || createForm.description.trim().length < 5) {
        alert('Description must be at least 5 characters');
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

        await api.post('/complaints/', payload);
        setShowCreateModal(false);
        loadComplaints();
        loadDashboard();
      } catch (err: any) {
        alert(err?.response?.data?.detail || 'Failed to create complaint');
      } finally {
        setCreateLoading(false);
      }
    },
    [createForm, loadComplaints, loadDashboard]
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
        alert('Reason must be at least 5 characters');
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

        await api.post('/rma/', payload);
        setShowRMAModal(false);
        loadComplaints();
        loadDashboard();
      } catch (err: any) {
        alert(err?.response?.data?.detail || 'Failed to create RMA');
      } finally {
        setRmaLoading(false);
      }
    },
    [rmaComplaint, rmaForm, loadComplaints, loadDashboard]
  );

  // ── Render ─────────────────────────────────────────────────────

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
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
          <div className="du-card bg-base-100 shadow-sm border">
            <div className="du-card-body p-4">
              <div className="flex items-center gap-3">
                <div className="p-2 rounded-lg bg-red-500/20">
                  <ExclamationTriangleIcon className="h-6 w-6 text-red-600" />
                </div>
                <div>
                  <p className="text-xs text-slate-400 uppercase tracking-wide">Open Complaints</p>
                  <p className="text-xl font-bold text-white">{dashboard.open_complaints}</p>
                </div>
              </div>
            </div>
          </div>

          <div className="du-card bg-base-100 shadow-sm border">
            <div className="du-card-body p-4">
              <div className="flex items-center gap-3">
                <div className="p-2 rounded-lg bg-blue-500/20">
                  <ClockIcon className="h-6 w-6 text-blue-600" />
                </div>
                <div>
                  <p className="text-xs text-slate-400 uppercase tracking-wide">Avg Resolution Time</p>
                  <p className="text-xl font-bold text-white">
                    {dashboard.avg_resolution_days != null
                      ? `${dashboard.avg_resolution_days} days`
                      : 'N/A'}
                  </p>
                </div>
              </div>
            </div>
          </div>

          <div className="du-card bg-base-100 shadow-sm border">
            <div className="du-card-body p-4">
              <div className="flex items-center gap-3">
                <div className="p-2 rounded-lg bg-purple-500/20">
                  <TruckIcon className="h-6 w-6 text-purple-600" />
                </div>
                <div>
                  <p className="text-xs text-slate-400 uppercase tracking-wide">Open RMAs</p>
                  <p className="text-xl font-bold text-white">{dashboard.open_rmas}</p>
                </div>
              </div>
            </div>
          </div>

          <div className="du-card bg-base-100 shadow-sm border">
            <div className="du-card-body p-4">
              <div className="flex items-center gap-3">
                <div className="p-2 rounded-lg bg-green-500/20">
                  <CheckCircleIcon className="h-6 w-6 text-green-600" />
                </div>
                <div>
                  <p className="text-xs text-slate-400 uppercase tracking-wide">Satisfaction Rate</p>
                  <p className="text-xl font-bold text-white">
                    {dashboard.satisfaction_rate != null
                      ? `${dashboard.satisfaction_rate}%`
                      : 'N/A'}
                  </p>
                </div>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Filters */}
      <div className="flex flex-col sm:flex-row sm:items-center gap-4">
        <div className="relative flex-1 max-w-sm">
          <MagnifyingGlassIcon className="h-4 w-4 absolute left-3 top-1/2 -translate-y-1/2 text-slate-500" />
          <input
            type="text"
            placeholder="Search complaints..."
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

      {/* Error */}
      {error && (
        <div className="du-alert du-alert-error shadow-sm">
          <ExclamationTriangleIcon className="h-5 w-5" />
          <span>{error}</span>
          <button onClick={loadComplaints} className="du-btn du-btn-sm du-btn-ghost">
            Retry
          </button>
        </div>
      )}

      {/* Table */}
      <div className="du-card bg-base-100 shadow-sm border overflow-x-auto">
        <table className="du-table du-table-sm w-full">
          <thead>
            <tr>
              <th className="w-8"></th>
              <th>Complaint #</th>
              <th>Customer</th>
              <th>Date</th>
              <th>Severity</th>
              <th>Status</th>
              <th>Description</th>
              <th>RMAs</th>
              <th className="text-right">Est. Cost</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr>
                <td colSpan={9} className="p-0">
                  <SkeletonTable rows={8} columns={9} showHeader={false} />
                </td>
              </tr>
            ) : filtered.length === 0 ? (
              <tr>
                <td colSpan={9} className="text-center py-8 text-slate-400">
                  {error
                    ? 'Failed to load complaints.'
                    : 'No complaints found. Click "New Complaint" to create one.'}
                </td>
              </tr>
            ) : (
              filtered.map((c) => (
                <React.Fragment key={c.id}>
                  <tr
                    className={`cursor-pointer hover:bg-base-200 ${
                      expandedId === c.id ? 'bg-base-200' : ''
                    }`}
                    onClick={() => toggleExpand(c.id)}
                  >
                    <td>
                      {expandedId === c.id ? (
                        <ChevronUpIcon className="h-4 w-4" />
                      ) : (
                        <ChevronDownIcon className="h-4 w-4" />
                      )}
                    </td>
                    <td className="font-medium text-sm">{c.complaint_number}</td>
                    <td>
                      <div className="text-sm font-medium">{c.customer_name}</div>
                      {c.customer_contact && (
                        <div className="text-xs text-slate-400">{c.customer_contact}</div>
                      )}
                    </td>
                    <td className="text-sm">{formatDate(c.date_received)}</td>
                    <td>
                      <span
                        className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${
                          severityBadge[c.severity]
                        }`}
                      >
                        {severityLabel[c.severity]}
                      </span>
                    </td>
                    <td>
                      <span
                        className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${
                          statusBadge[c.status]
                        }`}
                      >
                        {statusLabel[c.status]}
                      </span>
                    </td>
                    <td className="max-w-xs">
                      <div className="text-sm font-medium truncate">{c.title}</div>
                      <div className="text-xs text-slate-400 truncate">{c.description}</div>
                    </td>
                    <td className="text-center text-sm">{c.rmas.length}</td>
                    <td className="text-right font-mono text-sm">{fmt(c.estimated_cost)}</td>
                  </tr>

                  {/* Expanded detail */}
                  {expandedId === c.id && (
                    <tr>
                      <td colSpan={9} className="bg-base-200 p-0">
                        <div className="p-4 space-y-4">
                          {/* Detail grid */}
                          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                            <div className="space-y-2">
                              <h4 className="font-semibold text-sm text-slate-300">Complaint Details</h4>
                              <dl className="text-sm space-y-1">
                                <div className="flex justify-between">
                                  <dt className="text-slate-400">PO Number:</dt>
                                  <dd>{c.customer_po_number || '-'}</dd>
                                </div>
                                <div className="flex justify-between">
                                  <dt className="text-slate-400">Part:</dt>
                                  <dd>{c.part ? `${c.part.part_number} - ${c.part.name}` : '-'}</dd>
                                </div>
                                <div className="flex justify-between">
                                  <dt className="text-slate-400">Lot #:</dt>
                                  <dd>{c.lot_number || '-'}</dd>
                                </div>
                                <div className="flex justify-between">
                                  <dt className="text-slate-400">Serial #:</dt>
                                  <dd>{c.serial_number || '-'}</dd>
                                </div>
                                <div className="flex justify-between">
                                  <dt className="text-slate-400">Qty Affected:</dt>
                                  <dd>{c.quantity_affected}</dd>
                                </div>
                                <div className="flex justify-between">
                                  <dt className="text-slate-400">Occurrence Date:</dt>
                                  <dd>{formatDate(c.date_of_occurrence)}</dd>
                                </div>
                              </dl>
                            </div>

                            <div className="space-y-2">
                              <h4 className="font-semibold text-sm text-slate-300">Investigation & Resolution</h4>
                              <dl className="text-sm space-y-1">
                                <div>
                                  <dt className="text-slate-400">Investigation Findings:</dt>
                                  <dd className="mt-0.5">{c.investigation_findings || '-'}</dd>
                                </div>
                                <div>
                                  <dt className="text-slate-400">Root Cause:</dt>
                                  <dd className="mt-0.5">{c.root_cause || '-'}</dd>
                                </div>
                                <div>
                                  <dt className="text-slate-400">Containment Action:</dt>
                                  <dd className="mt-0.5">{c.containment_action || '-'}</dd>
                                </div>
                                <div>
                                  <dt className="text-slate-400">Corrective Action:</dt>
                                  <dd className="mt-0.5">{c.corrective_action || '-'}</dd>
                                </div>
                                <div>
                                  <dt className="text-slate-400">Resolution:</dt>
                                  <dd className="mt-0.5">{c.resolution_description || '-'}</dd>
                                </div>
                              </dl>
                            </div>

                            <div className="space-y-2">
                              <h4 className="font-semibold text-sm text-slate-300">Financial & Status</h4>
                              <dl className="text-sm space-y-1">
                                <div className="flex justify-between">
                                  <dt className="text-slate-400">Estimated Cost:</dt>
                                  <dd className="font-mono">{fmt(c.estimated_cost)}</dd>
                                </div>
                                <div className="flex justify-between">
                                  <dt className="text-slate-400">Actual Cost:</dt>
                                  <dd className="font-mono">{fmt(c.actual_cost)}</dd>
                                </div>
                                <div className="flex justify-between">
                                  <dt className="text-slate-400">Resolved Date:</dt>
                                  <dd>{formatDate(c.resolved_date)}</dd>
                                </div>
                                <div className="flex justify-between">
                                  <dt className="text-slate-400">Closed Date:</dt>
                                  <dd>{formatDate(c.closed_date)}</dd>
                                </div>
                                <div className="flex justify-between">
                                  <dt className="text-slate-400">Customer Satisfied:</dt>
                                  <dd>
                                    {c.customer_satisfied === null
                                      ? '-'
                                      : c.customer_satisfied
                                        ? 'Yes'
                                        : 'No'}
                                  </dd>
                                </div>
                                {c.satisfaction_notes && (
                                  <div>
                                    <dt className="text-slate-400">Satisfaction Notes:</dt>
                                    <dd className="mt-0.5">{c.satisfaction_notes}</dd>
                                  </div>
                                )}
                                <div className="flex justify-between">
                                  <dt className="text-slate-400">NCR ID:</dt>
                                  <dd>{c.ncr_id || '-'}</dd>
                                </div>
                                <div className="flex justify-between">
                                  <dt className="text-slate-400">CAR ID:</dt>
                                  <dd>{c.car_id || '-'}</dd>
                                </div>
                              </dl>
                            </div>
                          </div>

                          {/* RMAs table */}
                          {c.rmas.length > 0 && (
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
                                  {c.rmas.map((rma) => (
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
                              onClick={(e) => {
                                e.stopPropagation();
                                openRMAModal(c);
                              }}
                              className="du-btn du-btn-sm du-btn-outline gap-1"
                            >
                              <TruckIcon className="h-4 w-4" />
                              Create RMA
                            </button>
                            <button
                              onClick={(e) => {
                                e.stopPropagation();
                                window.open(`/complaints/${c.id}`, '_self');
                              }}
                              className="du-btn du-btn-sm du-btn-ghost gap-1"
                            >
                              <DocumentTextIcon className="h-4 w-4" />
                              Full Details
                            </button>
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

      {/* Results count */}
      {!loading && (
        <div className="text-sm text-slate-400">
          Showing {filtered.length} of {complaints.length} complaints
        </div>
      )}

      {/* ── Create Complaint Modal ────────────────────────────────── */}
      {showCreateModal && (
        <div className="du-modal du-modal-open">
          <div className="du-modal-box max-w-2xl">
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
                <div>
                  <label className="du-label">
                    <span className="du-label-text">
                      Customer Name <span className="text-red-500">*</span>
                    </span>
                  </label>
                  <input
                    type="text"
                    value={createForm.customer_name}
                    onChange={(e) =>
                      setCreateForm((f) => ({ ...f, customer_name: e.target.value }))
                    }
                    className="du-input du-input-bordered w-full"
                    required
                  />
                </div>
                <div>
                  <label className="du-label">
                    <span className="du-label-text">Customer PO Number</span>
                  </label>
                  <input
                    type="text"
                    value={createForm.customer_po_number}
                    onChange={(e) =>
                      setCreateForm((f) => ({ ...f, customer_po_number: e.target.value }))
                    }
                    className="du-input du-input-bordered w-full"
                  />
                </div>
                <div>
                  <label className="du-label">
                    <span className="du-label-text">Customer Contact</span>
                  </label>
                  <input
                    type="text"
                    value={createForm.customer_contact}
                    onChange={(e) =>
                      setCreateForm((f) => ({ ...f, customer_contact: e.target.value }))
                    }
                    className="du-input du-input-bordered w-full"
                  />
                </div>
                <div>
                  <label className="du-label">
                    <span className="du-label-text">Severity</span>
                  </label>
                  <select
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
                </div>
                <div>
                  <label className="du-label">
                    <span className="du-label-text">Lot Number</span>
                  </label>
                  <input
                    type="text"
                    value={createForm.lot_number}
                    onChange={(e) =>
                      setCreateForm((f) => ({ ...f, lot_number: e.target.value }))
                    }
                    className="du-input du-input-bordered w-full"
                  />
                </div>
                <div>
                  <label className="du-label">
                    <span className="du-label-text">Serial Number</span>
                  </label>
                  <input
                    type="text"
                    value={createForm.serial_number}
                    onChange={(e) =>
                      setCreateForm((f) => ({ ...f, serial_number: e.target.value }))
                    }
                    className="du-input du-input-bordered w-full"
                  />
                </div>
                <div>
                  <label className="du-label">
                    <span className="du-label-text">Quantity Affected</span>
                  </label>
                  <input
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
                </div>
                <div>
                  <label className="du-label">
                    <span className="du-label-text">Estimated Cost ($)</span>
                  </label>
                  <input
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
                </div>
                <div>
                  <label className="du-label">
                    <span className="du-label-text">Date Received</span>
                  </label>
                  <input
                    type="date"
                    value={createForm.date_received}
                    onChange={(e) =>
                      setCreateForm((f) => ({ ...f, date_received: e.target.value }))
                    }
                    className="du-input du-input-bordered w-full"
                  />
                </div>
                <div>
                  <label className="du-label">
                    <span className="du-label-text">Date of Occurrence</span>
                  </label>
                  <input
                    type="date"
                    value={createForm.date_of_occurrence}
                    onChange={(e) =>
                      setCreateForm((f) => ({ ...f, date_of_occurrence: e.target.value }))
                    }
                    className="du-input du-input-bordered w-full"
                  />
                </div>
              </div>

              <div>
                <label className="du-label">
                  <span className="du-label-text">
                    Title <span className="text-red-500">*</span>
                  </span>
                </label>
                <input
                  type="text"
                  value={createForm.title}
                  onChange={(e) =>
                    setCreateForm((f) => ({ ...f, title: e.target.value }))
                  }
                  className="du-input du-input-bordered w-full"
                  required
                  minLength={3}
                />
              </div>

              <div>
                <label className="du-label">
                  <span className="du-label-text">
                    Description <span className="text-red-500">*</span>
                  </span>
                </label>
                <textarea
                  value={createForm.description}
                  onChange={(e) =>
                    setCreateForm((f) => ({ ...f, description: e.target.value }))
                  }
                  className="du-textarea du-textarea-bordered w-full h-24"
                  required
                  minLength={5}
                />
              </div>

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
          </div>
          <div className="du-modal-backdrop" onClick={() => setShowCreateModal(false)} />
        </div>
      )}

      {/* ── Create RMA Modal ──────────────────────────────────────── */}
      {showRMAModal && rmaComplaint && (
        <div className="du-modal du-modal-open">
          <div className="du-modal-box max-w-lg">
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
              <div>
                <label className="du-label">
                  <span className="du-label-text">Customer Name</span>
                </label>
                <input
                  type="text"
                  value={rmaForm.customer_name}
                  onChange={(e) =>
                    setRmaForm((f) => ({ ...f, customer_name: e.target.value }))
                  }
                  className="du-input du-input-bordered w-full"
                  required
                />
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="du-label">
                    <span className="du-label-text">Quantity</span>
                  </label>
                  <input
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
                </div>
                <div>
                  <label className="du-label">
                    <span className="du-label-text">Lot Number</span>
                  </label>
                  <input
                    type="text"
                    value={rmaForm.lot_number}
                    onChange={(e) =>
                      setRmaForm((f) => ({ ...f, lot_number: e.target.value }))
                    }
                    className="du-input du-input-bordered w-full"
                  />
                </div>
              </div>

              <div>
                <label className="du-label">
                  <span className="du-label-text">
                    Reason <span className="text-red-500">*</span>
                  </span>
                </label>
                <textarea
                  value={rmaForm.reason}
                  onChange={(e) =>
                    setRmaForm((f) => ({ ...f, reason: e.target.value }))
                  }
                  className="du-textarea du-textarea-bordered w-full h-20"
                  required
                  minLength={5}
                />
              </div>

              <div>
                <label className="du-label">
                  <span className="du-label-text">Notes</span>
                </label>
                <textarea
                  value={rmaForm.notes}
                  onChange={(e) =>
                    setRmaForm((f) => ({ ...f, notes: e.target.value }))
                  }
                  className="du-textarea du-textarea-bordered w-full h-16"
                />
              </div>

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
          </div>
          <div className="du-modal-backdrop" onClick={() => setShowRMAModal(false)} />
        </div>
      )}
    </div>
  );
}
