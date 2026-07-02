import React, { useEffect, useState, useMemo, useCallback } from 'react';
import api from '../services/api';
import {
  ClockIcon,
  ExclamationTriangleIcon,
  CheckCircleIcon,
  PlusIcon,
  XMarkIcon,
  MagnifyingGlassIcon,
  CalendarDaysIcon,
} from '@heroicons/react/24/outline';
import { Modal } from '../components/ui/Modal';
import { FormField } from '../components/ui/FormField';
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
import { formatCentralDate, getCentralTodayISODate } from '../utils/centralTime';

// ── Types ────────────────────────────────────────────────────────
type CertStatus = 'active' | 'expired' | 'suspended' | 'revoked' | 'pending';
type TabKey = 'certifications' | 'training' | 'skill_matrix';

interface Certification {
  id: number;
  user_id: number;
  user_name?: string;
  certification_type: string;
  certification_name: string;
  issuing_authority: string | null;
  certificate_number: string | null;
  issue_date: string | null;
  expiration_date: string | null;
  status: CertStatus;
  level: string | null;
  scope: string | null;
  document_reference: string | null;
  notes: string | null;
  verified_by: number | null;
  verified_date: string | null;
  created_at: string;
  updated_at: string | null;
}

interface TrainingRecord {
  id: number;
  user_id: number;
  user_name?: string;
  training_name: string;
  training_type: string | null;
  description: string | null;
  trainer: string | null;
  training_date: string;
  completion_date: string | null;
  hours: number | null;
  passed: boolean;
  score: number | null;
  certificate_number: string | null;
  expiration_date: string | null;
  work_center_id: number | null;
  work_center_name?: string | null;
  notes: string | null;
  created_at: string;
}

interface SkillMatrixEntry {
  id: number;
  user_id: number;
  user_name?: string;
  work_center_id: number;
  work_center_name?: string;
  skill_level: number;
  qualified_date: string | null;
  last_assessment_date: string | null;
  next_assessment_date: string | null;
  notes: string | null;
}

interface Dashboard {
  total_certified: number;
  expiring_soon: number;
  expired: number;
  training_scheduled: number;
}

interface CertCreateForm {
  user_id: string;
  certification_type: string;
  certification_name: string;
  issuing_authority: string;
  certificate_number: string;
  issue_date: string;
  expiration_date: string;
  status: CertStatus;
  level: string;
  scope: string;
  notes: string;
}

interface TrainingCreateForm {
  user_id: string;
  training_name: string;
  training_type: string;
  description: string;
  trainer: string;
  training_date: string;
  completion_date: string;
  hours: string;
  passed: boolean;
  score: string;
  work_center_id: string;
  notes: string;
}

// ── Helpers ──────────────────────────────────────────────────────
// Central-local "today" (YYYY-MM-DD) so date-only form defaults don't roll to
// tomorrow on a Central evening (UTC midnight).
const todayISO = () => getCentralTodayISODate();

// Shop-local Central date; '-' fallback matches centralTime's default.
const formatDate = (d: string | null | undefined) => formatCentralDate(d);

const statusBadge: Record<CertStatus, string> = {
  active: 'bg-green-500/20 text-emerald-300',
  expired: 'bg-red-500/20 text-red-300',
  suspended: 'bg-yellow-500/20 text-yellow-300',
  revoked: 'bg-slate-800/50 text-slate-100',
  pending: 'bg-blue-500/20 text-blue-300',
};

const statusLabel: Record<CertStatus, string> = {
  active: 'Active',
  expired: 'Expired',
  suspended: 'Suspended',
  revoked: 'Revoked',
  pending: 'Pending',
};

const skillColor = (level: number) => level >= 4 ? 'bg-green-500/100' : level >= 2 ? 'bg-yellow-500/100' : 'bg-red-500/100';
const skillLabel = (level: number) => level >= 4 ? 'Expert' : level >= 3 ? 'Proficient' : level >= 2 ? 'Competent' : level >= 1 ? 'Beginner' : 'Untrained';

const defaultCertForm: CertCreateForm = { user_id: '', certification_type: '', certification_name: '', issuing_authority: '', certificate_number: '', issue_date: todayISO(), expiration_date: '', status: 'active', level: '', scope: '', notes: '' };

const defaultTrainingForm: TrainingCreateForm = { user_id: '', training_name: '', training_type: '', description: '', trainer: '', training_date: todayISO(), completion_date: '', hours: '', passed: true, score: '', work_center_id: '', notes: '' };

// ── Component ────────────────────────────────────────────────────
export default function OperatorCertifications() {
  const { showToast } = useToast();

  // Data
  const [certifications, setCertifications] = useState<Certification[]>([]);
  const [trainingRecords, setTrainingRecords] = useState<TrainingRecord[]>([]);
  const [skillMatrix, setSkillMatrix] = useState<SkillMatrixEntry[]>([]);
  const [dashboard, setDashboard] = useState<Dashboard | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [trainingError, setTrainingError] = useState<string | null>(null);
  const [skillMatrixError, setSkillMatrixError] = useState<string | null>(null);

  // UI
  const [activeTab, setActiveTab] = useState<TabKey>('certifications');
  const [searchTerm, setSearchTerm] = useState('');
  const [statusFilter, setStatusFilter] = useState('');

  // Modals
  const [showCertModal, setShowCertModal] = useState(false);
  const [certForm, setCertForm] = useState<CertCreateForm>({ ...defaultCertForm });
  const [certCreateLoading, setCertCreateLoading] = useState(false);

  const [showTrainingModal, setShowTrainingModal] = useState(false);
  const [trainingForm, setTrainingForm] = useState<TrainingCreateForm>({ ...defaultTrainingForm });
  const [trainingCreateLoading, setTrainingCreateLoading] = useState(false);

  // ── Data fetching ──────────────────────────────────────────────

  const loadDashboard = useCallback(async () => {
    try {
      const data = await api.getCertificationsDashboard();
      setDashboard(data);
    } catch (err) {
      console.error('Failed to load dashboard:', err);
    }
  }, []);

  const loadCertifications = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);
      const params: Record<string, string> = {};
      if (statusFilter) params.status = statusFilter;
      const data = await api.getCertifications(params as any);
      setCertifications(Array.isArray(data) ? data : data.items || []);
    } catch (err: any) {
      console.error('Failed to load certifications:', err);
      setError(err?.response?.data?.detail || 'Failed to load certifications');
    } finally {
      setLoading(false);
    }
  }, [statusFilter]);

  const loadTrainingRecords = useCallback(async () => {
    try {
      setTrainingError(null);
      const data = await api.getTrainingRecords();
      setTrainingRecords(Array.isArray(data) ? data : data.items || []);
    } catch (err: any) {
      console.error('Failed to load training records:', err);
      setTrainingError(err?.response?.data?.detail || 'Failed to load training records');
    }
  }, []);

  const loadSkillMatrix = useCallback(async () => {
    try {
      setSkillMatrixError(null);
      const data = await api.getSkillMatrix();
      setSkillMatrix(Array.isArray(data) ? data : data.items || []);
    } catch (err: any) {
      console.error('Failed to load skill matrix:', err);
      setSkillMatrixError(err?.response?.data?.detail || 'Failed to load skill matrix');
    }
  }, []);

  useEffect(() => {
    loadDashboard();
    loadCertifications();
  }, [loadDashboard, loadCertifications]);

  useEffect(() => {
    if (activeTab === 'training') loadTrainingRecords();
    if (activeTab === 'skill_matrix') loadSkillMatrix();
  }, [activeTab, loadTrainingRecords, loadSkillMatrix]);

  // ── Filtered data ──────────────────────────────────────────────

  const filteredCerts = useMemo(() => {
    if (!searchTerm) return certifications;
    const term = searchTerm.toLowerCase();
    return certifications.filter(
      (c) =>
        (c.user_name || '').toLowerCase().includes(term) ||
        c.certification_name.toLowerCase().includes(term) ||
        c.certification_type.toLowerCase().includes(term) ||
        (c.certificate_number || '').toLowerCase().includes(term)
    );
  }, [certifications, searchTerm]);

  const filteredTraining = useMemo(() => {
    if (!searchTerm) return trainingRecords;
    const term = searchTerm.toLowerCase();
    return trainingRecords.filter(
      (t) =>
        (t.user_name || '').toLowerCase().includes(term) ||
        t.training_name.toLowerCase().includes(term) ||
        (t.trainer || '').toLowerCase().includes(term) ||
        (t.training_type || '').toLowerCase().includes(term)
    );
  }, [trainingRecords, searchTerm]);

  // ── Create certification ───────────────────────────────────────

  const openCertModal = useCallback(() => {
    setCertForm({ ...defaultCertForm, issue_date: todayISO() });
    setShowCertModal(true);
  }, []);

  const handleCreateCert = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();
      if (!certForm.user_id || !certForm.certification_name.trim()) {
        showToast('error', 'User ID and certification name are required');
        return;
      }
      try {
        setCertCreateLoading(true);
        const payload: Record<string, unknown> = {
          user_id: parseInt(certForm.user_id, 10),
          certification_type: certForm.certification_type || 'general',
          certification_name: certForm.certification_name,
          status: certForm.status,
        };
        if (certForm.issuing_authority) payload.issuing_authority = certForm.issuing_authority;
        if (certForm.certificate_number) payload.certificate_number = certForm.certificate_number;
        if (certForm.issue_date) payload.issue_date = certForm.issue_date;
        if (certForm.expiration_date) payload.expiration_date = certForm.expiration_date;
        if (certForm.level) payload.level = certForm.level;
        if (certForm.scope) payload.scope = certForm.scope;
        if (certForm.notes) payload.notes = certForm.notes;

        await api.createCertification(payload);
        setShowCertModal(false);
        showToast('success', 'Certification created');
        loadCertifications();
        loadDashboard();
      } catch (err: any) {
        showToast('error', err?.response?.data?.detail || 'Failed to create certification');
      } finally {
        setCertCreateLoading(false);
      }
    },
    [certForm, loadCertifications, loadDashboard, showToast]
  );

  // ── Create training record ─────────────────────────────────────

  const openTrainingModal = useCallback(() => {
    setTrainingForm({ ...defaultTrainingForm, training_date: todayISO() });
    setShowTrainingModal(true);
  }, []);

  const handleCreateTraining = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();
      if (!trainingForm.user_id || !trainingForm.training_name.trim()) {
        showToast('error', 'User ID and training name are required');
        return;
      }
      try {
        setTrainingCreateLoading(true);
        const payload: Record<string, unknown> = {
          user_id: parseInt(trainingForm.user_id, 10),
          training_name: trainingForm.training_name,
          training_date: trainingForm.training_date,
          passed: trainingForm.passed,
        };
        if (trainingForm.training_type) payload.training_type = trainingForm.training_type;
        if (trainingForm.description) payload.description = trainingForm.description;
        if (trainingForm.trainer) payload.trainer = trainingForm.trainer;
        if (trainingForm.completion_date) payload.completion_date = trainingForm.completion_date;
        if (trainingForm.hours) payload.hours = parseFloat(trainingForm.hours);
        if (trainingForm.score) payload.score = parseFloat(trainingForm.score);
        if (trainingForm.work_center_id) payload.work_center_id = parseInt(trainingForm.work_center_id, 10);
        if (trainingForm.notes) payload.notes = trainingForm.notes;

        await api.createTrainingRecord(payload);
        setShowTrainingModal(false);
        showToast('success', 'Training record created');
        loadTrainingRecords();
        loadDashboard();
      } catch (err: any) {
        showToast('error', err?.response?.data?.detail || 'Failed to create training record');
      } finally {
        setTrainingCreateLoading(false);
      }
    },
    [trainingForm, loadTrainingRecords, loadDashboard, showToast]
  );

  // ── Skill matrix grid computation ──────────────────────────────

  const { operators, workCenters, matrixMap } = useMemo(() => {
    const opsMap = new Map<number, string>();
    const wcsMap = new Map<number, string>();
    const mMap = new Map<string, SkillMatrixEntry>();

    skillMatrix.forEach((entry) => {
      if (!opsMap.has(entry.user_id)) opsMap.set(entry.user_id, entry.user_name || `User ${entry.user_id}`);
      if (!wcsMap.has(entry.work_center_id)) wcsMap.set(entry.work_center_id, entry.work_center_name || `WC ${entry.work_center_id}`);
      mMap.set(`${entry.user_id}-${entry.work_center_id}`, entry);
    });

    return {
      operators: Array.from(opsMap.entries()).map(([id, name]) => ({ id, name })),
      workCenters: Array.from(wcsMap.entries()).map(([id, name]) => ({ id, name })),
      matrixMap: mMap,
    };
  }, [skillMatrix]);

  // ── Table columns + mobile cards ───────────────────────────────

  const certColumns = useMemo<Array<DataTableColumn<Certification>>>(() => [
    {
      key: 'operator',
      header: 'Operator',
      sortable: true,
      className: 'font-medium text-white',
      accessor: (c) => c.user_name || `User #${c.user_id}`,
    },
    {
      key: 'certification',
      header: 'Certification',
      sortable: true,
      className: 'text-slate-300',
      accessor: (c) => c.certification_name,
    },
    {
      key: 'type',
      header: 'Type',
      sortable: true,
      className: 'text-slate-400 capitalize',
      accessor: (c) => c.certification_type,
      render: (c) => c.certification_type.replace(/_/g, ' '),
    },
    {
      key: 'status',
      header: 'Status',
      sortable: true,
      accessor: (c) => c.status,
      csv: (c) => statusLabel[c.status] || c.status,
      render: (c) => <StatusBadge status={c.status} colorMap={statusBadge} className="rounded-full" />,
    },
    {
      key: 'issued',
      header: 'Issued',
      sortable: true,
      className: 'text-slate-400',
      accessor: (c) => c.issue_date ?? '',
      csv: (c) => formatDate(c.issue_date),
      render: (c) => formatDate(c.issue_date),
    },
    {
      key: 'expires',
      header: 'Expires',
      sortable: true,
      className: 'text-slate-400',
      accessor: (c) => c.expiration_date ?? '',
      csv: (c) => formatDate(c.expiration_date),
      render: (c) => formatDate(c.expiration_date),
    },
    {
      key: 'authority',
      header: 'Authority',
      sortable: true,
      className: 'text-slate-400',
      accessor: (c) => c.issuing_authority ?? '',
      render: (c) => c.issuing_authority || '-',
    },
  ], []);

  const renderCertCard = useCallback((c: Certification) => (
    <MobileDataCard
      title={c.user_name || `User #${c.user_id}`}
      subtitle={c.certification_name}
      badge={<StatusBadge status={c.status} colorMap={statusBadge} className="rounded-full" />}
      fields={[
        { label: 'Type', value: <span className="capitalize">{c.certification_type.replace(/_/g, ' ')}</span> },
        { label: 'Authority', value: c.issuing_authority || '-' },
        { label: 'Issued', value: formatDate(c.issue_date) },
        { label: 'Expires', value: formatDate(c.expiration_date) },
      ]}
    />
  ), []);

  const trainingColumns = useMemo<Array<DataTableColumn<TrainingRecord>>>(() => [
    {
      key: 'operator',
      header: 'Operator',
      sortable: true,
      className: 'font-medium text-white',
      accessor: (t) => t.user_name || `User #${t.user_id}`,
    },
    {
      key: 'training',
      header: 'Training',
      sortable: true,
      className: 'text-slate-300',
      accessor: (t) => t.training_name,
    },
    {
      key: 'type',
      header: 'Type',
      sortable: true,
      className: 'text-slate-400 capitalize',
      accessor: (t) => t.training_type ?? '',
      render: (t) => (t.training_type || '-').replace(/_/g, ' '),
    },
    {
      key: 'trainer',
      header: 'Trainer',
      sortable: true,
      className: 'text-slate-400',
      accessor: (t) => t.trainer ?? '',
      render: (t) => t.trainer || '-',
    },
    {
      key: 'date',
      header: 'Date',
      sortable: true,
      className: 'text-slate-400',
      accessor: (t) => t.training_date,
      csv: (t) => formatDate(t.training_date),
      render: (t) => formatDate(t.training_date),
    },
    {
      key: 'hours',
      header: 'Hours',
      sortable: true,
      align: 'right',
      className: 'text-slate-400 tabular-nums',
      accessor: (t) => t.hours ?? null,
      csv: (t) => (t.hours != null ? t.hours : ''),
      render: (t) => (t.hours != null ? `${t.hours}h` : '-'),
    },
    {
      key: 'result',
      header: 'Result',
      sortable: true,
      accessor: (t) => (t.passed ? 'Passed' : 'Failed'),
      render: (t) => (
        <span className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${t.passed ? 'bg-green-500/20 text-emerald-300' : 'bg-red-500/20 text-red-300'}`}>
          {t.passed ? 'Passed' : 'Failed'}
        </span>
      ),
    },
    {
      key: 'score',
      header: 'Score',
      sortable: true,
      align: 'right',
      className: 'text-slate-400 tabular-nums',
      accessor: (t) => t.score ?? null,
      csv: (t) => (t.score != null ? t.score : ''),
      render: (t) => (t.score != null ? `${t.score}%` : '-'),
    },
  ], []);

  const renderTrainingCard = useCallback((t: TrainingRecord) => (
    <MobileDataCard
      title={t.user_name || `User #${t.user_id}`}
      subtitle={t.training_name}
      badge={
        <span className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${t.passed ? 'bg-green-500/20 text-emerald-300' : 'bg-red-500/20 text-red-300'}`}>
          {t.passed ? 'Passed' : 'Failed'}
        </span>
      }
      fields={[
        { label: 'Type', value: <span className="capitalize">{(t.training_type || '-').replace(/_/g, ' ')}</span> },
        { label: 'Trainer', value: t.trainer || '-' },
        { label: 'Date', value: formatDate(t.training_date) },
        { label: 'Hours', value: t.hours != null ? `${t.hours}h` : '-' },
        { label: 'Score', value: t.score != null ? `${t.score}%` : '-' },
      ]}
    />
  ), []);

  // ── Render ─────────────────────────────────────────────────────

  const tabs: { key: TabKey; label: string }[] = [
    { key: 'certifications', label: 'Certifications' },
    { key: 'training', label: 'Training Records' },
    { key: 'skill_matrix', label: 'Skill Matrix' },
  ];

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-white">Operator Certifications & Training</h1>
          <p className="text-sm text-slate-400 mt-1">Manage certifications, training records, and skill assessments</p>
        </div>
        <div className="flex gap-2">
          {activeTab === 'certifications' && (
            <button onClick={openCertModal} className="inline-flex items-center gap-1.5 rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700">
              <PlusIcon className="h-4 w-4" /> New Certification
            </button>
          )}
          {activeTab === 'training' && (
            <button onClick={openTrainingModal} className="inline-flex items-center gap-1.5 rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700">
              <PlusIcon className="h-4 w-4" /> New Training Record
            </button>
          )}
        </div>
      </div>

      {/* Dashboard Cards */}
      {dashboard && (
        <MiniStatStrip className="grid grid-cols-2 lg:grid-cols-4 gap-2">
          <MiniStat
            icon={CheckCircleIcon}
            iconBg="bg-fd-green/15"
            iconColor="text-fd-green"
            label="Certified Operators"
            value={dashboard.total_certified}
          />
          <MiniStat
            icon={ClockIcon}
            iconBg={dashboard.expiring_soon > 0 ? 'bg-fd-amber/15' : 'bg-fd-green/15'}
            iconColor={dashboard.expiring_soon > 0 ? 'text-fd-amber' : 'text-fd-green'}
            label="Expiring Soon"
            value={dashboard.expiring_soon}
            valueColor={dashboard.expiring_soon > 0 ? 'text-fd-amber' : undefined}
          />
          <MiniStat
            icon={ExclamationTriangleIcon}
            iconBg={dashboard.expired > 0 ? 'bg-fd-red/15' : 'bg-fd-green/15'}
            iconColor={dashboard.expired > 0 ? 'text-fd-red' : 'text-fd-green'}
            label="Expired"
            value={dashboard.expired}
            valueColor={dashboard.expired > 0 ? 'text-fd-red' : undefined}
          />
          <MiniStat
            icon={CalendarDaysIcon}
            iconBg="bg-fd-blue/15"
            iconColor="text-fd-blue"
            label="Training Scheduled"
            value={dashboard.training_scheduled}
          />
        </MiniStatStrip>
      )}

      {/* Tabs */}
      <div className="border-b border-slate-700">
        <nav className="-mb-px flex gap-6">
          {tabs.map((tab) => (
            <button
              key={tab.key}
              onClick={() => { setActiveTab(tab.key); setSearchTerm(''); }}
              className={`whitespace-nowrap border-b-2 pb-3 text-sm font-medium transition-colors ${
                activeTab === tab.key
                  ? 'border-blue-600 text-blue-600'
                  : 'border-transparent text-slate-400 hover:border-slate-600 hover:text-slate-300'
              }`}
            >
              {tab.label}
            </button>
          ))}
        </nav>
      </div>

      {/* Search & Filters */}
      {activeTab !== 'skill_matrix' && (
        <div className="flex flex-wrap items-center gap-3">
          <div className="relative flex-1 min-w-[200px]">
            <MagnifyingGlassIcon className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
            <input
              type="text"
              aria-label={activeTab === 'certifications' ? 'Search by operator, certification, type' : 'Search by operator, training, trainer'}
              placeholder={activeTab === 'certifications' ? 'Search by operator, certification, type...' : 'Search by operator, training, trainer...'}
              value={searchTerm}
              onChange={(e) => setSearchTerm(e.target.value)}
              className="w-full rounded-lg border border-slate-600 py-2 pl-9 pr-3 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
            />
          </div>
          {activeTab === 'certifications' && (
            <select
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value)}
              className="rounded-lg border border-slate-600 py-2 pl-3 pr-8 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
            >
              <option value="">All Statuses</option>
              <option value="active">Active</option>
              <option value="expired">Expired</option>
              <option value="suspended">Suspended</option>
              <option value="pending">Pending</option>
              <option value="revoked">Revoked</option>
            </select>
          )}
        </div>
      )}

      {/* Certifications Tab */}
      {activeTab === 'certifications' && (
        <DataTable
          columns={certColumns}
          data={filteredCerts}
          rowKey={(c) => c.id}
          loading={loading}
          error={error ?? false}
          onRetry={loadCertifications}
          defaultSort={{ key: 'operator', dir: 'asc' }}
          pageSize={25}
          csvExport={{ filename: 'operator-certifications' }}
          mobileCards={renderCertCard}
          empty={
            searchTerm
              ? {
                  icon: MagnifyingGlassIcon,
                  title: 'No matching certifications',
                  description: 'No certifications match your search.',
                }
              : {
                  icon: CheckCircleIcon,
                  title: 'No certifications',
                  description: 'Operator certifications will appear here once added.',
                  action: { label: 'New Certification', onClick: openCertModal },
                }
          }
        />
      )}

      {/* Training Records Tab */}
      {activeTab === 'training' && (
        <DataTable
          columns={trainingColumns}
          data={filteredTraining}
          rowKey={(t) => t.id}
          error={trainingError ?? false}
          onRetry={loadTrainingRecords}
          defaultSort={{ key: 'date', dir: 'desc' }}
          pageSize={25}
          csvExport={{ filename: 'training-records' }}
          mobileCards={renderTrainingCard}
          empty={
            searchTerm
              ? {
                  icon: MagnifyingGlassIcon,
                  title: 'No matching training records',
                  description: 'No training records match your search.',
                }
              : {
                  icon: CalendarDaysIcon,
                  title: 'No training records',
                  description: 'Completed and scheduled training will appear here.',
                  action: { label: 'New Training Record', onClick: openTrainingModal },
                }
          }
        />
      )}

      {/* Skill Matrix Tab */}
      {activeTab === 'skill_matrix' && (
        <div className="space-y-4">
          {/* Legend */}
          <div className="flex items-center gap-4 text-xs text-slate-400">
            <span className="flex items-center gap-1"><span className="inline-block h-3 w-3 rounded bg-green-500/100" /> Expert / Proficient</span>
            <span className="flex items-center gap-1"><span className="inline-block h-3 w-3 rounded bg-yellow-500/100" /> Competent</span>
            <span className="flex items-center gap-1"><span className="inline-block h-3 w-3 rounded bg-red-500/100" /> Beginner / Untrained</span>
          </div>

          {skillMatrixError ? (
            <ErrorState message={skillMatrixError} onRetry={loadSkillMatrix} />
          ) : operators.length === 0 ? (
            <EmptyState
              icon={ExclamationTriangleIcon}
              title="No skill matrix data"
              description="Skill assessments by operator and work center will appear here."
            />
          ) : (
            <div className="overflow-x-auto rounded-xl border border-slate-700 bg-fd-panel shadow-sm">
              <table className="min-w-full divide-y divide-slate-700 text-sm">
                <thead className="bg-slate-800">
                  <tr>
                    <th className="sticky left-0 z-10 bg-slate-800 px-4 py-3 text-left font-medium text-slate-400">Operator</th>
                    {workCenters.map((wc) => (
                      <th key={wc.id} className="px-4 py-3 text-center font-medium text-slate-400 whitespace-nowrap">{wc.name}</th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {operators.map((op) => (
                    <tr key={op.id} className="hover:bg-slate-800">
                      <td className="sticky left-0 z-10 bg-fd-panel px-4 py-3 font-medium text-white whitespace-nowrap">{op.name}</td>
                      {workCenters.map((wc) => {
                        const entry = matrixMap.get(`${op.id}-${wc.id}`);
                        const level = entry?.skill_level ?? 0;
                        return (
                          <td key={wc.id} className="px-4 py-3 text-center" aria-label={`${wc.name}: ${skillLabel(level)} (Level ${level})`}>
                            <div className="flex flex-col items-center gap-0.5" title={`${skillLabel(level)} (Level ${level})`}>
                              <span className={`inline-block h-4 w-4 rounded-full ${skillColor(level)}`} />
                              <span className="text-[10px] text-slate-400 tabular-nums">{level}</span>
                            </div>
                          </td>
                        );
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {/* ── Create Certification Modal ────────────────────────────── */}
      <Modal open={showCertModal} onClose={() => setShowCertModal(false)} size="lg" closeOnBackdrop={false}>
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-lg font-semibold text-white">New Certification</h2>
              <button onClick={() => setShowCertModal(false)} className="text-slate-400 hover:text-slate-400">
                <XMarkIcon className="h-5 w-5" />
              </button>
            </div>
            <form onSubmit={handleCreateCert} className="space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <FormField label="User ID" required labelClassName="block text-xs font-medium text-slate-400 mb-1">
                  {(field) => (
                    <input {...field} type="number" required value={certForm.user_id} onChange={(e) => setCertForm({ ...certForm, user_id: e.target.value })}
                      className="w-full rounded-lg border border-slate-600 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500" />
                  )}
                </FormField>
                <FormField label="Status" labelClassName="block text-xs font-medium text-slate-400 mb-1">
                  {(field) => (
                    <select {...field} value={certForm.status} onChange={(e) => setCertForm({ ...certForm, status: e.target.value as CertStatus })}
                      className="w-full rounded-lg border border-slate-600 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500">
                      <option value="active">Active</option>
                      <option value="pending">Pending</option>
                      <option value="suspended">Suspended</option>
                    </select>
                  )}
                </FormField>
              </div>
              <FormField label="Certification Name" required labelClassName="block text-xs font-medium text-slate-400 mb-1">
                {(field) => (
                  <input {...field} type="text" required value={certForm.certification_name} onChange={(e) => setCertForm({ ...certForm, certification_name: e.target.value })}
                    className="w-full rounded-lg border border-slate-600 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500" />
                )}
              </FormField>
              <div className="grid grid-cols-2 gap-4">
                <FormField label="Type" labelClassName="block text-xs font-medium text-slate-400 mb-1">
                  {(field) => (
                    <input {...field} type="text" value={certForm.certification_type} onChange={(e) => setCertForm({ ...certForm, certification_type: e.target.value })}
                      placeholder="e.g. welding, safety"
                      className="w-full rounded-lg border border-slate-600 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500" />
                  )}
                </FormField>
                <FormField label="Level" labelClassName="block text-xs font-medium text-slate-400 mb-1">
                  {(field) => (
                    <input {...field} type="text" value={certForm.level} onChange={(e) => setCertForm({ ...certForm, level: e.target.value })}
                      placeholder="e.g. Level 1, Advanced"
                      className="w-full rounded-lg border border-slate-600 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500" />
                  )}
                </FormField>
              </div>
              <div className="grid grid-cols-2 gap-4">
                <FormField label="Issuing Authority" labelClassName="block text-xs font-medium text-slate-400 mb-1">
                  {(field) => (
                    <input {...field} type="text" value={certForm.issuing_authority} onChange={(e) => setCertForm({ ...certForm, issuing_authority: e.target.value })}
                      className="w-full rounded-lg border border-slate-600 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500" />
                  )}
                </FormField>
                <FormField label="Certificate Number" labelClassName="block text-xs font-medium text-slate-400 mb-1">
                  {(field) => (
                    <input {...field} type="text" value={certForm.certificate_number} onChange={(e) => setCertForm({ ...certForm, certificate_number: e.target.value })}
                      className="w-full rounded-lg border border-slate-600 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500" />
                  )}
                </FormField>
              </div>
              <div className="grid grid-cols-2 gap-4">
                <FormField label="Issue Date" labelClassName="block text-xs font-medium text-slate-400 mb-1">
                  {(field) => (
                    <input {...field} type="date" value={certForm.issue_date} onChange={(e) => setCertForm({ ...certForm, issue_date: e.target.value })}
                      className="w-full rounded-lg border border-slate-600 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500" />
                  )}
                </FormField>
                <FormField label="Expiration Date" labelClassName="block text-xs font-medium text-slate-400 mb-1">
                  {(field) => (
                    <input {...field} type="date" value={certForm.expiration_date} onChange={(e) => setCertForm({ ...certForm, expiration_date: e.target.value })}
                      className="w-full rounded-lg border border-slate-600 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500" />
                  )}
                </FormField>
              </div>
              <FormField label="Notes" labelClassName="block text-xs font-medium text-slate-400 mb-1">
                {(field) => (
                  <textarea {...field} rows={2} value={certForm.notes} onChange={(e) => setCertForm({ ...certForm, notes: e.target.value })}
                    className="w-full rounded-lg border border-slate-600 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500" />
                )}
              </FormField>
              <div className="flex justify-end gap-2 pt-2">
                <button type="button" onClick={() => setShowCertModal(false)} className="rounded-lg border border-slate-600 px-4 py-2 text-sm font-medium text-slate-300 hover:bg-slate-800">Cancel</button>
                <button type="submit" disabled={certCreateLoading} className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50">
                  {certCreateLoading ? 'Creating...' : 'Create Certification'}
                </button>
              </div>
            </form>
      </Modal>

      {/* ── Create Training Record Modal ──────────────────────────── */}
      <Modal open={showTrainingModal} onClose={() => setShowTrainingModal(false)} size="lg" closeOnBackdrop={false}>
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-lg font-semibold text-white">New Training Record</h2>
              <button onClick={() => setShowTrainingModal(false)} className="text-slate-400 hover:text-slate-400">
                <XMarkIcon className="h-5 w-5" />
              </button>
            </div>
            <form onSubmit={handleCreateTraining} className="space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <FormField label="User ID" required labelClassName="block text-xs font-medium text-slate-400 mb-1">
                  {(field) => (
                    <input {...field} type="number" required value={trainingForm.user_id} onChange={(e) => setTrainingForm({ ...trainingForm, user_id: e.target.value })}
                      className="w-full rounded-lg border border-slate-600 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500" />
                  )}
                </FormField>
                <FormField label="Training Type" labelClassName="block text-xs font-medium text-slate-400 mb-1">
                  {(field) => (
                    <input {...field} type="text" value={trainingForm.training_type} onChange={(e) => setTrainingForm({ ...trainingForm, training_type: e.target.value })}
                      placeholder="e.g. safety, technical"
                      className="w-full rounded-lg border border-slate-600 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500" />
                  )}
                </FormField>
              </div>
              <FormField label="Training Name" required labelClassName="block text-xs font-medium text-slate-400 mb-1">
                {(field) => (
                  <input {...field} type="text" required value={trainingForm.training_name} onChange={(e) => setTrainingForm({ ...trainingForm, training_name: e.target.value })}
                    className="w-full rounded-lg border border-slate-600 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500" />
                )}
              </FormField>
              <FormField label="Description" labelClassName="block text-xs font-medium text-slate-400 mb-1">
                {(field) => (
                  <textarea {...field} rows={2} value={trainingForm.description} onChange={(e) => setTrainingForm({ ...trainingForm, description: e.target.value })}
                    className="w-full rounded-lg border border-slate-600 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500" />
                )}
              </FormField>
              <div className="grid grid-cols-2 gap-4">
                <FormField label="Trainer" labelClassName="block text-xs font-medium text-slate-400 mb-1">
                  {(field) => (
                    <input {...field} type="text" value={trainingForm.trainer} onChange={(e) => setTrainingForm({ ...trainingForm, trainer: e.target.value })}
                      className="w-full rounded-lg border border-slate-600 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500" />
                  )}
                </FormField>
                <FormField label="Work Center ID" labelClassName="block text-xs font-medium text-slate-400 mb-1">
                  {(field) => (
                    <input {...field} type="number" value={trainingForm.work_center_id} onChange={(e) => setTrainingForm({ ...trainingForm, work_center_id: e.target.value })}
                      className="w-full rounded-lg border border-slate-600 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500" />
                  )}
                </FormField>
              </div>
              <div className="grid grid-cols-3 gap-4">
                <FormField label="Training Date" required labelClassName="block text-xs font-medium text-slate-400 mb-1">
                  {(field) => (
                    <input {...field} type="date" required value={trainingForm.training_date} onChange={(e) => setTrainingForm({ ...trainingForm, training_date: e.target.value })}
                      className="w-full rounded-lg border border-slate-600 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500" />
                  )}
                </FormField>
                <FormField label="Completion Date" labelClassName="block text-xs font-medium text-slate-400 mb-1">
                  {(field) => (
                    <input {...field} type="date" value={trainingForm.completion_date} onChange={(e) => setTrainingForm({ ...trainingForm, completion_date: e.target.value })}
                      className="w-full rounded-lg border border-slate-600 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500" />
                  )}
                </FormField>
                <FormField label="Hours" labelClassName="block text-xs font-medium text-slate-400 mb-1">
                  {(field) => (
                    <input {...field} type="number" step="0.5" value={trainingForm.hours} onChange={(e) => setTrainingForm({ ...trainingForm, hours: e.target.value })}
                      className="w-full rounded-lg border border-slate-600 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500" />
                  )}
                </FormField>
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div className="flex items-center gap-2">
                  <input type="checkbox" id="passed" aria-label="Passed" checked={trainingForm.passed} onChange={(e) => setTrainingForm({ ...trainingForm, passed: e.target.checked })}
                    className="h-4 w-4 rounded border-slate-600 text-blue-600 focus:ring-blue-500" />
                  <label htmlFor="passed" className="text-sm text-slate-300">Passed</label>
                </div>
                <FormField label="Score (%)" labelClassName="block text-xs font-medium text-slate-400 mb-1">
                  {(field) => (
                    <input {...field} type="number" min="0" max="100" value={trainingForm.score} onChange={(e) => setTrainingForm({ ...trainingForm, score: e.target.value })}
                      className="w-full rounded-lg border border-slate-600 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500" />
                  )}
                </FormField>
              </div>
              <FormField label="Notes" labelClassName="block text-xs font-medium text-slate-400 mb-1">
                {(field) => (
                  <textarea {...field} rows={2} value={trainingForm.notes} onChange={(e) => setTrainingForm({ ...trainingForm, notes: e.target.value })}
                    className="w-full rounded-lg border border-slate-600 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500" />
                )}
              </FormField>
              <div className="flex justify-end gap-2 pt-2">
                <button type="button" onClick={() => setShowTrainingModal(false)} className="rounded-lg border border-slate-600 px-4 py-2 text-sm font-medium text-slate-300 hover:bg-slate-800">Cancel</button>
                <button type="submit" disabled={trainingCreateLoading} className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50">
                  {trainingCreateLoading ? 'Creating...' : 'Create Training Record'}
                </button>
              </div>
            </form>
      </Modal>
    </div>
  );
}
