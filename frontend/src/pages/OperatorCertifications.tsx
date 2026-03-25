import React, { useEffect, useState, useMemo, useCallback } from 'react';
import api from '../services/api';
import {
  AcademicCapIcon,
  ClockIcon,
  ExclamationTriangleIcon,
  CheckCircleIcon,
  PlusIcon,
  XMarkIcon,
  MagnifyingGlassIcon,
  CalendarDaysIcon,
  UserGroupIcon,
} from '@heroicons/react/24/outline';
import { SkeletonTable } from '../components/ui/Skeleton';

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
const todayISO = () => new Date().toISOString().split('T')[0];

const formatDate = (d: string | null | undefined) =>
  d ? new Date(d).toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' }) : '-';

const statusBadge: Record<CertStatus, string> = {
  active: 'bg-green-100 text-green-800',
  expired: 'bg-red-100 text-red-800',
  suspended: 'bg-yellow-100 text-yellow-800',
  revoked: 'bg-gray-100 text-gray-800',
  pending: 'bg-blue-100 text-blue-800',
};

const statusLabel: Record<CertStatus, string> = {
  active: 'Active',
  expired: 'Expired',
  suspended: 'Suspended',
  revoked: 'Revoked',
  pending: 'Pending',
};

const skillColor = (level: number) => level >= 4 ? 'bg-green-500' : level >= 2 ? 'bg-yellow-500' : 'bg-red-500';
const skillLabel = (level: number) => level >= 4 ? 'Expert' : level >= 3 ? 'Proficient' : level >= 2 ? 'Competent' : level >= 1 ? 'Beginner' : 'Untrained';

const defaultCertForm: CertCreateForm = { user_id: '', certification_type: '', certification_name: '', issuing_authority: '', certificate_number: '', issue_date: todayISO(), expiration_date: '', status: 'active', level: '', scope: '', notes: '' };

const defaultTrainingForm: TrainingCreateForm = { user_id: '', training_name: '', training_type: '', description: '', trainer: '', training_date: todayISO(), completion_date: '', hours: '', passed: true, score: '', work_center_id: '', notes: '' };

// ── Component ────────────────────────────────────────────────────
export default function OperatorCertifications() {
  // Data
  const [certifications, setCertifications] = useState<Certification[]>([]);
  const [trainingRecords, setTrainingRecords] = useState<TrainingRecord[]>([]);
  const [skillMatrix, setSkillMatrix] = useState<SkillMatrixEntry[]>([]);
  const [dashboard, setDashboard] = useState<Dashboard | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

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
      const data = await api.getTrainingRecords();
      setTrainingRecords(Array.isArray(data) ? data : data.items || []);
    } catch (err) {
      console.error('Failed to load training records:', err);
    }
  }, []);

  const loadSkillMatrix = useCallback(async () => {
    try {
      const data = await api.getSkillMatrix();
      setSkillMatrix(Array.isArray(data) ? data : data.items || []);
    } catch (err) {
      console.error('Failed to load skill matrix:', err);
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
        alert('User ID and certification name are required');
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
        loadCertifications();
        loadDashboard();
      } catch (err: any) {
        alert(err?.response?.data?.detail || 'Failed to create certification');
      } finally {
        setCertCreateLoading(false);
      }
    },
    [certForm, loadCertifications, loadDashboard]
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
        alert('User ID and training name are required');
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
        loadTrainingRecords();
        loadDashboard();
      } catch (err: any) {
        alert(err?.response?.data?.detail || 'Failed to create training record');
      } finally {
        setTrainingCreateLoading(false);
      }
    },
    [trainingForm, loadTrainingRecords, loadDashboard]
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
          <h1 className="text-2xl font-bold text-gray-900">Operator Certifications & Training</h1>
          <p className="text-sm text-gray-500 mt-1">Manage certifications, training records, and skill assessments</p>
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
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <div className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
            <div className="flex items-center gap-3">
              <div className="rounded-lg bg-green-100 p-2"><CheckCircleIcon className="h-5 w-5 text-green-600" /></div>
              <div>
                <p className="text-sm text-gray-500">Certified Operators</p>
                <p className="text-2xl font-bold text-gray-900">{dashboard.total_certified}</p>
              </div>
            </div>
          </div>
          <div className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
            <div className="flex items-center gap-3">
              <div className="rounded-lg bg-yellow-100 p-2"><ClockIcon className="h-5 w-5 text-yellow-600" /></div>
              <div>
                <p className="text-sm text-gray-500">Expiring Soon</p>
                <p className="text-2xl font-bold text-yellow-600">{dashboard.expiring_soon}</p>
              </div>
            </div>
          </div>
          <div className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
            <div className="flex items-center gap-3">
              <div className="rounded-lg bg-red-100 p-2"><ExclamationTriangleIcon className="h-5 w-5 text-red-600" /></div>
              <div>
                <p className="text-sm text-gray-500">Expired</p>
                <p className="text-2xl font-bold text-red-600">{dashboard.expired}</p>
              </div>
            </div>
          </div>
          <div className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
            <div className="flex items-center gap-3">
              <div className="rounded-lg bg-blue-100 p-2"><CalendarDaysIcon className="h-5 w-5 text-blue-600" /></div>
              <div>
                <p className="text-sm text-gray-500">Training Scheduled</p>
                <p className="text-2xl font-bold text-gray-900">{dashboard.training_scheduled}</p>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Tabs */}
      <div className="border-b border-gray-200">
        <nav className="-mb-px flex gap-6">
          {tabs.map((tab) => (
            <button
              key={tab.key}
              onClick={() => { setActiveTab(tab.key); setSearchTerm(''); }}
              className={`whitespace-nowrap border-b-2 pb-3 text-sm font-medium transition-colors ${
                activeTab === tab.key
                  ? 'border-blue-600 text-blue-600'
                  : 'border-transparent text-gray-500 hover:border-gray-300 hover:text-gray-700'
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
            <MagnifyingGlassIcon className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-gray-400" />
            <input
              type="text"
              placeholder={activeTab === 'certifications' ? 'Search by operator, certification, type...' : 'Search by operator, training, trainer...'}
              value={searchTerm}
              onChange={(e) => setSearchTerm(e.target.value)}
              className="w-full rounded-lg border border-gray-300 py-2 pl-9 pr-3 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
            />
          </div>
          {activeTab === 'certifications' && (
            <select
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value)}
              className="rounded-lg border border-gray-300 py-2 pl-3 pr-8 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
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

      {/* Error */}
      {error && (
        <div className="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700">{error}</div>
      )}

      {/* Certifications Tab */}
      {activeTab === 'certifications' && (
        loading ? (
          <SkeletonTable rows={6} cols={7} />
        ) : (
          <div className="overflow-x-auto rounded-xl border border-gray-200 bg-white shadow-sm">
            <table className="min-w-full divide-y divide-gray-200 text-sm">
              <thead className="bg-gray-50">
                <tr>
                  <th className="px-4 py-3 text-left font-medium text-gray-600">Operator</th>
                  <th className="px-4 py-3 text-left font-medium text-gray-600">Certification</th>
                  <th className="px-4 py-3 text-left font-medium text-gray-600">Type</th>
                  <th className="px-4 py-3 text-left font-medium text-gray-600">Status</th>
                  <th className="px-4 py-3 text-left font-medium text-gray-600">Issued</th>
                  <th className="px-4 py-3 text-left font-medium text-gray-600">Expires</th>
                  <th className="px-4 py-3 text-left font-medium text-gray-600">Authority</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {filteredCerts.length === 0 ? (
                  <tr>
                    <td colSpan={7} className="px-4 py-10 text-center text-gray-400">
                      {searchTerm ? 'No certifications match your search' : 'No certifications found'}
                    </td>
                  </tr>
                ) : (
                  filteredCerts.map((cert) => (
                    <tr key={cert.id} className="hover:bg-gray-50 transition-colors">
                      <td className="px-4 py-3 font-medium text-gray-900">{cert.user_name || `User #${cert.user_id}`}</td>
                      <td className="px-4 py-3 text-gray-700">{cert.certification_name}</td>
                      <td className="px-4 py-3 text-gray-500 capitalize">{cert.certification_type.replace(/_/g, ' ')}</td>
                      <td className="px-4 py-3">
                        <span className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${statusBadge[cert.status] || 'bg-gray-100 text-gray-800'}`}>
                          {statusLabel[cert.status] || cert.status}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-gray-500">{formatDate(cert.issue_date)}</td>
                      <td className="px-4 py-3 text-gray-500">{formatDate(cert.expiration_date)}</td>
                      <td className="px-4 py-3 text-gray-500">{cert.issuing_authority || '-'}</td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        )
      )}

      {/* Training Records Tab */}
      {activeTab === 'training' && (
        <div className="overflow-x-auto rounded-xl border border-gray-200 bg-white shadow-sm">
          <table className="min-w-full divide-y divide-gray-200 text-sm">
            <thead className="bg-gray-50">
              <tr>
                <th className="px-4 py-3 text-left font-medium text-gray-600">Operator</th>
                <th className="px-4 py-3 text-left font-medium text-gray-600">Training</th>
                <th className="px-4 py-3 text-left font-medium text-gray-600">Type</th>
                <th className="px-4 py-3 text-left font-medium text-gray-600">Trainer</th>
                <th className="px-4 py-3 text-left font-medium text-gray-600">Date</th>
                <th className="px-4 py-3 text-left font-medium text-gray-600">Hours</th>
                <th className="px-4 py-3 text-left font-medium text-gray-600">Result</th>
                <th className="px-4 py-3 text-left font-medium text-gray-600">Score</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {filteredTraining.length === 0 ? (
                <tr>
                  <td colSpan={8} className="px-4 py-10 text-center text-gray-400">
                    {searchTerm ? 'No training records match your search' : 'No training records found'}
                  </td>
                </tr>
              ) : (
                filteredTraining.map((tr) => (
                  <tr key={tr.id} className="hover:bg-gray-50 transition-colors">
                    <td className="px-4 py-3 font-medium text-gray-900">{tr.user_name || `User #${tr.user_id}`}</td>
                    <td className="px-4 py-3 text-gray-700">{tr.training_name}</td>
                    <td className="px-4 py-3 text-gray-500 capitalize">{(tr.training_type || '-').replace(/_/g, ' ')}</td>
                    <td className="px-4 py-3 text-gray-500">{tr.trainer || '-'}</td>
                    <td className="px-4 py-3 text-gray-500">{formatDate(tr.training_date)}</td>
                    <td className="px-4 py-3 text-gray-500">{tr.hours != null ? `${tr.hours}h` : '-'}</td>
                    <td className="px-4 py-3">
                      <span className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${tr.passed ? 'bg-green-100 text-green-800' : 'bg-red-100 text-red-800'}`}>
                        {tr.passed ? 'Passed' : 'Failed'}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-gray-500">{tr.score != null ? `${tr.score}%` : '-'}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      )}

      {/* Skill Matrix Tab */}
      {activeTab === 'skill_matrix' && (
        <div className="space-y-4">
          {/* Legend */}
          <div className="flex items-center gap-4 text-xs text-gray-500">
            <span className="flex items-center gap-1"><span className="inline-block h-3 w-3 rounded bg-green-500" /> Expert / Proficient</span>
            <span className="flex items-center gap-1"><span className="inline-block h-3 w-3 rounded bg-yellow-500" /> Competent</span>
            <span className="flex items-center gap-1"><span className="inline-block h-3 w-3 rounded bg-red-500" /> Beginner / Untrained</span>
          </div>

          {operators.length === 0 ? (
            <div className="rounded-xl border border-gray-200 bg-white p-10 text-center text-gray-400 shadow-sm">
              No skill matrix data available
            </div>
          ) : (
            <div className="overflow-x-auto rounded-xl border border-gray-200 bg-white shadow-sm">
              <table className="min-w-full divide-y divide-gray-200 text-sm">
                <thead className="bg-gray-50">
                  <tr>
                    <th className="sticky left-0 z-10 bg-gray-50 px-4 py-3 text-left font-medium text-gray-600">Operator</th>
                    {workCenters.map((wc) => (
                      <th key={wc.id} className="px-4 py-3 text-center font-medium text-gray-600 whitespace-nowrap">{wc.name}</th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {operators.map((op) => (
                    <tr key={op.id} className="hover:bg-gray-50">
                      <td className="sticky left-0 z-10 bg-white px-4 py-3 font-medium text-gray-900 whitespace-nowrap">{op.name}</td>
                      {workCenters.map((wc) => {
                        const entry = matrixMap.get(`${op.id}-${wc.id}`);
                        const level = entry?.skill_level ?? 0;
                        return (
                          <td key={wc.id} className="px-4 py-3 text-center">
                            <div className="flex flex-col items-center gap-0.5" title={`${skillLabel(level)} (Level ${level})`}>
                              <span className={`inline-block h-4 w-4 rounded-full ${skillColor(level)}`} />
                              <span className="text-[10px] text-gray-400">{level}</span>
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
      {showCertModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
          <div className="w-full max-w-lg rounded-xl bg-white p-6 shadow-xl">
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-lg font-semibold text-gray-900">New Certification</h2>
              <button onClick={() => setShowCertModal(false)} className="text-gray-400 hover:text-gray-600">
                <XMarkIcon className="h-5 w-5" />
              </button>
            </div>
            <form onSubmit={handleCreateCert} className="space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="block text-xs font-medium text-gray-600 mb-1">User ID *</label>
                  <input type="number" required value={certForm.user_id} onChange={(e) => setCertForm({ ...certForm, user_id: e.target.value })}
                    className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500" />
                </div>
                <div>
                  <label className="block text-xs font-medium text-gray-600 mb-1">Status</label>
                  <select value={certForm.status} onChange={(e) => setCertForm({ ...certForm, status: e.target.value as CertStatus })}
                    className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500">
                    <option value="active">Active</option>
                    <option value="pending">Pending</option>
                    <option value="suspended">Suspended</option>
                  </select>
                </div>
              </div>
              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">Certification Name *</label>
                <input type="text" required value={certForm.certification_name} onChange={(e) => setCertForm({ ...certForm, certification_name: e.target.value })}
                  className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500" />
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="block text-xs font-medium text-gray-600 mb-1">Type</label>
                  <input type="text" value={certForm.certification_type} onChange={(e) => setCertForm({ ...certForm, certification_type: e.target.value })}
                    placeholder="e.g. welding, safety"
                    className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500" />
                </div>
                <div>
                  <label className="block text-xs font-medium text-gray-600 mb-1">Level</label>
                  <input type="text" value={certForm.level} onChange={(e) => setCertForm({ ...certForm, level: e.target.value })}
                    placeholder="e.g. Level 1, Advanced"
                    className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500" />
                </div>
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="block text-xs font-medium text-gray-600 mb-1">Issuing Authority</label>
                  <input type="text" value={certForm.issuing_authority} onChange={(e) => setCertForm({ ...certForm, issuing_authority: e.target.value })}
                    className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500" />
                </div>
                <div>
                  <label className="block text-xs font-medium text-gray-600 mb-1">Certificate Number</label>
                  <input type="text" value={certForm.certificate_number} onChange={(e) => setCertForm({ ...certForm, certificate_number: e.target.value })}
                    className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500" />
                </div>
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="block text-xs font-medium text-gray-600 mb-1">Issue Date</label>
                  <input type="date" value={certForm.issue_date} onChange={(e) => setCertForm({ ...certForm, issue_date: e.target.value })}
                    className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500" />
                </div>
                <div>
                  <label className="block text-xs font-medium text-gray-600 mb-1">Expiration Date</label>
                  <input type="date" value={certForm.expiration_date} onChange={(e) => setCertForm({ ...certForm, expiration_date: e.target.value })}
                    className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500" />
                </div>
              </div>
              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">Notes</label>
                <textarea rows={2} value={certForm.notes} onChange={(e) => setCertForm({ ...certForm, notes: e.target.value })}
                  className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500" />
              </div>
              <div className="flex justify-end gap-2 pt-2">
                <button type="button" onClick={() => setShowCertModal(false)} className="rounded-lg border border-gray-300 px-4 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50">Cancel</button>
                <button type="submit" disabled={certCreateLoading} className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50">
                  {certCreateLoading ? 'Creating...' : 'Create Certification'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* ── Create Training Record Modal ──────────────────────────── */}
      {showTrainingModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
          <div className="w-full max-w-lg rounded-xl bg-white p-6 shadow-xl">
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-lg font-semibold text-gray-900">New Training Record</h2>
              <button onClick={() => setShowTrainingModal(false)} className="text-gray-400 hover:text-gray-600">
                <XMarkIcon className="h-5 w-5" />
              </button>
            </div>
            <form onSubmit={handleCreateTraining} className="space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="block text-xs font-medium text-gray-600 mb-1">User ID *</label>
                  <input type="number" required value={trainingForm.user_id} onChange={(e) => setTrainingForm({ ...trainingForm, user_id: e.target.value })}
                    className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500" />
                </div>
                <div>
                  <label className="block text-xs font-medium text-gray-600 mb-1">Training Type</label>
                  <input type="text" value={trainingForm.training_type} onChange={(e) => setTrainingForm({ ...trainingForm, training_type: e.target.value })}
                    placeholder="e.g. safety, technical"
                    className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500" />
                </div>
              </div>
              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">Training Name *</label>
                <input type="text" required value={trainingForm.training_name} onChange={(e) => setTrainingForm({ ...trainingForm, training_name: e.target.value })}
                  className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500" />
              </div>
              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">Description</label>
                <textarea rows={2} value={trainingForm.description} onChange={(e) => setTrainingForm({ ...trainingForm, description: e.target.value })}
                  className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500" />
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="block text-xs font-medium text-gray-600 mb-1">Trainer</label>
                  <input type="text" value={trainingForm.trainer} onChange={(e) => setTrainingForm({ ...trainingForm, trainer: e.target.value })}
                    className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500" />
                </div>
                <div>
                  <label className="block text-xs font-medium text-gray-600 mb-1">Work Center ID</label>
                  <input type="number" value={trainingForm.work_center_id} onChange={(e) => setTrainingForm({ ...trainingForm, work_center_id: e.target.value })}
                    className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500" />
                </div>
              </div>
              <div className="grid grid-cols-3 gap-4">
                <div>
                  <label className="block text-xs font-medium text-gray-600 mb-1">Training Date *</label>
                  <input type="date" required value={trainingForm.training_date} onChange={(e) => setTrainingForm({ ...trainingForm, training_date: e.target.value })}
                    className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500" />
                </div>
                <div>
                  <label className="block text-xs font-medium text-gray-600 mb-1">Completion Date</label>
                  <input type="date" value={trainingForm.completion_date} onChange={(e) => setTrainingForm({ ...trainingForm, completion_date: e.target.value })}
                    className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500" />
                </div>
                <div>
                  <label className="block text-xs font-medium text-gray-600 mb-1">Hours</label>
                  <input type="number" step="0.5" value={trainingForm.hours} onChange={(e) => setTrainingForm({ ...trainingForm, hours: e.target.value })}
                    className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500" />
                </div>
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div className="flex items-center gap-2">
                  <input type="checkbox" id="passed" checked={trainingForm.passed} onChange={(e) => setTrainingForm({ ...trainingForm, passed: e.target.checked })}
                    className="h-4 w-4 rounded border-gray-300 text-blue-600 focus:ring-blue-500" />
                  <label htmlFor="passed" className="text-sm text-gray-700">Passed</label>
                </div>
                <div>
                  <label className="block text-xs font-medium text-gray-600 mb-1">Score (%)</label>
                  <input type="number" min="0" max="100" value={trainingForm.score} onChange={(e) => setTrainingForm({ ...trainingForm, score: e.target.value })}
                    className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500" />
                </div>
              </div>
              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">Notes</label>
                <textarea rows={2} value={trainingForm.notes} onChange={(e) => setTrainingForm({ ...trainingForm, notes: e.target.value })}
                  className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500" />
              </div>
              <div className="flex justify-end gap-2 pt-2">
                <button type="button" onClick={() => setShowTrainingModal(false)} className="rounded-lg border border-gray-300 px-4 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50">Cancel</button>
                <button type="submit" disabled={trainingCreateLoading} className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50">
                  {trainingCreateLoading ? 'Creating...' : 'Create Training Record'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
