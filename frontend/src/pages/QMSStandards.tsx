import React, { useState, useEffect, useCallback } from 'react';
import {
  RectangleStackIcon,
  ListBulletIcon,
  CheckCircleIcon,
  ExclamationTriangleIcon,
  XCircleIcon,
  QuestionMarkCircleIcon,
} from '@heroicons/react/24/outline';
import api from '../services/api';
import { MiniStat, MiniStatStrip } from '../components/cockpit';
import {
  QMSStandardListResponse,
  QMSStandardResponse,
  QMSClauseResponse,
  QMSAuditReadinessSummary,
  ClauseAutoEvidenceResponse,
  AutoLinkSummary,
} from '../types/api';

type ComplianceStatus = 'not_assessed' | 'compliant' | 'partial' | 'non_compliant' | 'not_applicable';

const STATUS_COLORS: Record<string, string> = {
  compliant: 'bg-green-500/20 text-emerald-300',
  partial: 'bg-yellow-500/20 text-yellow-300',
  non_compliant: 'bg-red-500/20 text-red-300',
  not_assessed: 'bg-slate-800/50 text-slate-400',
  not_applicable: 'bg-blue-500/20 text-blue-300',
};

const STATUS_LABELS: Record<string, string> = {
  compliant: 'Compliant',
  partial: 'Partial',
  non_compliant: 'Non-Compliant',
  not_assessed: 'Not Assessed',
  not_applicable: 'N/A',
};

const EVIDENCE_TYPES = [
  { value: 'document', label: 'Document' },
  { value: 'module', label: 'System Module' },
  { value: 'procedure', label: 'Procedure' },
  { value: 'ncr', label: 'NCR Records' },
  { value: 'car', label: 'CAR Records' },
  { value: 'fai', label: 'FAI Records' },
  { value: 'calibration', label: 'Calibration Records' },
  { value: 'training', label: 'Training Records' },
  { value: 'spc', label: 'SPC Data' },
  { value: 'other', label: 'Other' },
];

export default function QMSStandards() {
  const [standards, setStandards] = useState<QMSStandardListResponse[]>([]);
  const [selectedStandard, setSelectedStandard] = useState<QMSStandardResponse | null>(null);
  const [auditSummary, setAuditSummary] = useState<QMSAuditReadinessSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [view, setView] = useState<'overview' | 'detail'>('overview');

  // Modal states
  const [showAddStandard, setShowAddStandard] = useState(false);
  const [showAddClause, setShowAddClause] = useState(false);
  const [showAddEvidence, setShowAddEvidence] = useState(false);
  const [showBulkImport, setShowBulkImport] = useState(false);
  const [evidenceClauseId, setEvidenceClauseId] = useState<number | null>(null);

  // Form states
  const [standardForm, setStandardForm] = useState({ name: '', version: '', description: '', standard_body: '' });
  const [clauseForm, setClauseForm] = useState({ clause_number: '', title: '', description: '' });
  const [evidenceForm, setEvidenceForm] = useState({ evidence_type: 'document', title: '', description: '', module_reference: '' });
  const [bulkText, setBulkText] = useState('');
  const [saving, setSaving] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState('');

  // Auto-link state
  const [autoLinking, setAutoLinking] = useState(false);
  const [autoLinkResult, setAutoLinkResult] = useState<AutoLinkSummary | null>(null);
  const [showAutoLinkResult, setShowAutoLinkResult] = useState(false);

  const fetchData = useCallback(async () => {
    try {
      setLoading(true);
      const [stdData, auditData] = await Promise.all([
        api.getQMSStandards(false),
        api.getQMSAuditReadiness(),
      ]);
      setStandards(stdData);
      setAuditSummary(auditData);
    } catch (err: any) {
      setError(err?.response?.data?.detail || 'Failed to load QMS data');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { fetchData(); }, [fetchData]);

  const loadStandardDetail = async (id: number) => {
    try {
      setLoading(true);
      const data = await api.getQMSStandard(id);
      setSelectedStandard(data);
      setView('detail');
    } catch (err: any) {
      setError(err?.response?.data?.detail || 'Failed to load standard');
    } finally {
      setLoading(false);
    }
  };

  const handleCreateStandard = async () => {
    if (!standardForm.name) return;
    setSaving(true);
    try {
      await api.createQMSStandard(standardForm);
      setShowAddStandard(false);
      setStandardForm({ name: '', version: '', description: '', standard_body: '' });
      fetchData();
    } catch (err: any) {
      setError(err?.response?.data?.detail || 'Failed to create standard');
    } finally {
      setSaving(false);
    }
  };

  const handleCreateClause = async () => {
    if (!selectedStandard || !clauseForm.clause_number || !clauseForm.title) return;
    setSaving(true);
    try {
      await api.createQMSClause(selectedStandard.id, clauseForm);
      setShowAddClause(false);
      setClauseForm({ clause_number: '', title: '', description: '' });
      loadStandardDetail(selectedStandard.id);
    } catch (err: any) {
      setError(err?.response?.data?.detail || 'Failed to create clause');
    } finally {
      setSaving(false);
    }
  };

  const handleUpdateCompliance = async (clauseId: number, compliance_status: ComplianceStatus, compliance_notes?: string) => {
    try {
      await api.updateQMSClause(clauseId, { compliance_status, compliance_notes });
      if (selectedStandard) loadStandardDetail(selectedStandard.id);
    } catch (err: any) {
      setError(err?.response?.data?.detail || 'Failed to update compliance');
    }
  };

  const handleAddEvidence = async () => {
    if (!evidenceClauseId || !evidenceForm.title) return;
    setSaving(true);
    try {
      await api.addQMSEvidence(evidenceClauseId, evidenceForm);
      setShowAddEvidence(false);
      setEvidenceForm({ evidence_type: 'document', title: '', description: '', module_reference: '' });
      setEvidenceClauseId(null);
      if (selectedStandard) loadStandardDetail(selectedStandard.id);
    } catch (err: any) {
      setError(err?.response?.data?.detail || 'Failed to add evidence');
    } finally {
      setSaving(false);
    }
  };

  const handleVerifyEvidence = async (evidenceId: number) => {
    try {
      await api.updateQMSEvidence(evidenceId, { is_verified: true });
      if (selectedStandard) loadStandardDetail(selectedStandard.id);
    } catch (err: any) {
      setError(err?.response?.data?.detail || 'Failed to verify evidence');
    }
  };

  const handleBulkImport = async () => {
    if (!selectedStandard || !bulkText.trim()) return;
    setSaving(true);
    try {
      // Parse text: each line is "clause_number | title | description"
      const lines = bulkText.trim().split('\n').filter(l => l.trim());
      const clauses = lines.map((line, i) => {
        const parts = line.split('|').map(p => p.trim());
        return {
          clause_number: parts[0] || `${i + 1}`,
          title: parts[1] || parts[0] || `Clause ${i + 1}`,
          description: parts[2] || '',
          sort_order: i,
        };
      });
      await api.bulkCreateQMSClauses(selectedStandard.id, clauses);
      setShowBulkImport(false);
      setBulkText('');
      loadStandardDetail(selectedStandard.id);
    } catch (err: any) {
      setError(err?.response?.data?.detail || 'Failed to import clauses');
    } finally {
      setSaving(false);
    }
  };

  const handlePdfUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file || !selectedStandard) return;
    if (!file.name.toLowerCase().endsWith('.pdf')) {
      setError('Please upload a PDF file');
      return;
    }

    setUploading(true);
    setUploadProgress('Uploading PDF and extracting clauses with AI...');
    setError('');

    try {
      const formData = new FormData();
      formData.append('file', file);
      await api.uploadQMSPdf(selectedStandard.id, formData);
      setUploadProgress('');
      loadStandardDetail(selectedStandard.id);
      fetchData();
    } catch (err: any) {
      setError(err?.response?.data?.detail || 'Failed to extract clauses from PDF');
    } finally {
      setUploading(false);
      setUploadProgress('');
      // Reset file input
      e.target.value = '';
    }
  };

  const handleAutoLinkAll = async () => {
    if (!selectedStandard) return;
    setAutoLinking(true);
    setError('');
    try {
      const result = await api.autoLinkStandard(selectedStandard.id);
      setAutoLinkResult(result);
      setShowAutoLinkResult(true);
      // Refresh the standard detail to show new evidence
      loadStandardDetail(selectedStandard.id);
      fetchData();
    } catch (err: any) {
      setError(err?.response?.data?.detail || 'Failed to auto-link evidence');
    } finally {
      setAutoLinking(false);
    }
  };

  const handleDeleteEvidence = async (evidenceId: number) => {
    if (!window.confirm('Remove this evidence link?')) return;
    try {
      await api.deleteQMSEvidence(evidenceId);
      if (selectedStandard) loadStandardDetail(selectedStandard.id);
    } catch (err: any) {
      setError(err?.response?.data?.detail || 'Failed to delete evidence');
    }
  };

  // ===== RENDER =====

  if (loading && !standards.length) {
    return (
      <div className="p-6">
        <div className="animate-pulse space-y-4">
          <div className="h-8 bg-fd-raised rounded-sm w-1/3"></div>
          <div className="h-32 bg-fd-raised rounded-sm"></div>
          <div className="h-64 bg-fd-raised rounded-sm"></div>
        </div>
      </div>
    );
  }

  return (
    <div className="p-6 max-w-7xl mx-auto">
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div>
          {view === 'detail' && selectedStandard ? (
            <div className="flex items-center gap-3">
              <button
                onClick={() => { setView('overview'); setSelectedStandard(null); fetchData(); }}
                className="text-sm text-fd-blue hover:text-fd-cyan"
              >
                &larr; Back to Standards
              </button>
              <h1 className="text-2xl font-bold text-white">
                {selectedStandard.name} {selectedStandard.version && `(${selectedStandard.version})`}
              </h1>
            </div>
          ) : (
            <>
              <h1 className="text-2xl font-bold text-white">QMS Standards & Audit Readiness</h1>
              <p className="text-sm text-slate-400 mt-1">
                Manage AS9100D, ISO 9001 and other QMS standards. Map clauses to system evidence for seamless audit preparation.
              </p>
            </>
          )}
        </div>
        {view === 'overview' && (
          <button
            onClick={() => setShowAddStandard(true)}
            className="btn btn-primary btn-sm"
          >
            + Add Standard
          </button>
        )}
        {view === 'detail' && (
          <div className="flex gap-2">
            <button
              onClick={handleAutoLinkAll}
              disabled={autoLinking}
              className={`btn btn-warning btn-sm ${autoLinking ? 'loading' : ''}`}
              title="Scan ERP/MES records and auto-link evidence to all clauses"
            >
              {autoLinking ? 'Scanning...' : '\u26A1 Auto-Link Evidence'}
            </button>
            <label className={`btn btn-accent btn-sm ${uploading ? 'loading' : ''}`}>
              {uploading ? 'Extracting...' : 'Upload PDF'}
              <input
                type="file"
                accept=".pdf"
                className="hidden"
                onChange={handlePdfUpload}
                disabled={uploading}
              />
            </label>
            <button onClick={() => setShowBulkImport(true)} className="btn btn-outline btn-sm">
              Bulk Import
            </button>
            <button onClick={() => setShowAddClause(true)} className="btn btn-primary btn-sm">
              + Add Clause
            </button>
          </div>
        )}
      </div>

      {uploading && uploadProgress && (
        <div className="alert alert-info mb-4">
          <span className="loading loading-spinner loading-sm"></span>
          <span>{uploadProgress}</span>
        </div>
      )}

      {error && (
        <div className="alert alert-error mb-4">
          <span>{error}</span>
          <button onClick={() => setError('')} className="btn btn-ghost btn-xs">Dismiss</button>
        </div>
      )}

      {/* ===== OVERVIEW VIEW ===== */}
      {view === 'overview' && (
        <>
          {/* Audit Readiness Dashboard — merged KPI strip + compact compliance figure */}
          {auditSummary && (
            <div className="mb-6">
              <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-400 mb-2">Audit Readiness</h2>
              <MiniStatStrip className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-7 gap-2">
                <MiniStat
                  icon={RectangleStackIcon}
                  iconBg="bg-fd-blue/15"
                  iconColor="text-fd-blue"
                  label="Standards"
                  value={auditSummary.total_standards}
                />
                <MiniStat
                  icon={ListBulletIcon}
                  iconBg="bg-fd-blue/15"
                  iconColor="text-fd-blue"
                  label="Total Clauses"
                  value={auditSummary.total_clauses}
                />
                <MiniStat
                  icon={CheckCircleIcon}
                  iconBg="bg-fd-green/15"
                  iconColor="text-fd-green"
                  label="Compliant"
                  value={auditSummary.compliant}
                  valueColor="text-fd-green"
                />
                <MiniStat
                  icon={ExclamationTriangleIcon}
                  iconBg="bg-fd-amber/15"
                  iconColor="text-fd-amber"
                  label="Partial"
                  value={auditSummary.partial}
                  valueColor="text-fd-amber"
                />
                <MiniStat
                  icon={XCircleIcon}
                  iconBg="bg-fd-red/15"
                  iconColor="text-fd-red"
                  label="Non-Compliant"
                  value={auditSummary.non_compliant}
                  valueColor="text-fd-red"
                />
                <MiniStat
                  icon={QuestionMarkCircleIcon}
                  iconBg="bg-fd-blue/15"
                  iconColor="text-fd-mute"
                  label="Not Assessed"
                  value={auditSummary.not_assessed}
                />
                <MiniStat
                  icon={CheckCircleIcon}
                  iconBg="bg-fd-green/15"
                  iconColor="text-fd-green"
                  label="Overall Compliance"
                  value={`${auditSummary.compliance_percentage}%`}
                  valueColor="text-fd-green"
                  subtitle={`Evidence ${auditSummary.verified_evidence}/${auditSummary.total_evidence_links} verified`}
                />
              </MiniStatStrip>

              {/* Compact compliance bar + review flag */}
              <div className="card card-compact !p-3 mt-2">
                <div className="w-full bg-fd-sunken rounded-sm h-2 overflow-hidden">
                  <div
                    className="bg-fd-green h-2 rounded-sm transition-all duration-500"
                    style={{ width: `${auditSummary.compliance_percentage}%` }}
                  />
                </div>
                <div className="flex justify-between mt-2 text-xs text-slate-400 tabular-nums">
                  <span>Evidence: {auditSummary.verified_evidence}/{auditSummary.total_evidence_links} verified</span>
                  {auditSummary.clauses_needing_review > 0 && (
                    <span className="text-fd-amber font-medium">
                      {auditSummary.clauses_needing_review} clause(s) overdue for review
                    </span>
                  )}
                </div>
              </div>
            </div>
          )}

          {/* Standards List */}
          <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-400 mb-2">Registered Standards</h2>
          {standards.length === 0 ? (
            <div className="text-center py-12 card card-compact !p-3">
              <p className="text-slate-400 mb-4">No QMS standards registered yet.</p>
              <button onClick={() => setShowAddStandard(true)} className="btn btn-primary btn-sm">
                Add Your First Standard
              </button>
            </div>
          ) : (
            <div className="grid gap-3 md:grid-cols-2 lg:grid-cols-3">
              {standards.map(std => (
                <div
                  key={std.id}
                  onClick={() => loadStandardDetail(std.id)}
                  className="card card-compact !p-3 min-w-0 cursor-pointer hover:border-fd-line-bright transition-colors"
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                      <h3 className="font-semibold text-white truncate">{std.name}</h3>
                      {std.version && <span className="text-xs text-slate-400">{std.version}</span>}
                    </div>
                    <span className={`badge badge-sm ${std.is_active ? 'badge-success' : 'badge-ghost'}`}>
                      {std.is_active ? 'Active' : 'Inactive'}
                    </span>
                  </div>
                  {std.standard_body && <p className="text-xs text-slate-400 mt-1 truncate">{std.standard_body}</p>}
                  {std.description && <p className="text-sm text-slate-400 mt-2 line-clamp-2">{std.description}</p>}

                  {/* Mini compliance bar */}
                  {std.total_clauses > 0 && (
                    <div className="mt-3">
                      <div className="flex gap-1 h-2 rounded-sm overflow-hidden bg-fd-sunken">
                        {std.compliant_clauses > 0 && (
                          <div className="bg-fd-green" style={{ width: `${(std.compliant_clauses / std.total_clauses) * 100}%` }} />
                        )}
                        {std.partial_clauses > 0 && (
                          <div className="bg-fd-amber" style={{ width: `${(std.partial_clauses / std.total_clauses) * 100}%` }} />
                        )}
                        {std.non_compliant_clauses > 0 && (
                          <div className="bg-fd-red" style={{ width: `${(std.non_compliant_clauses / std.total_clauses) * 100}%` }} />
                        )}
                        {std.not_assessed_clauses > 0 && (
                          <div className="bg-fd-line-bright" style={{ width: `${(std.not_assessed_clauses / std.total_clauses) * 100}%` }} />
                        )}
                      </div>
                      <p className="text-xs text-slate-400 mt-1 tabular-nums">{std.total_clauses} clauses</p>
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </>
      )}

      {/* ===== DETAIL VIEW ===== */}
      {view === 'detail' && selectedStandard && (
        <div>
          {selectedStandard.description && (
            <p className="text-sm text-slate-400 mb-4">{selectedStandard.description}</p>
          )}

          {/* Clauses Table */}
          {selectedStandard.clauses.length === 0 ? (
            <div className="text-center py-12 card card-compact !p-3">
              <p className="text-lg font-medium text-slate-300 mb-2">No clauses added yet</p>
              <p className="text-sm text-slate-400 mb-6">Upload a PDF of your quality manual and clauses will be extracted automatically.</p>
              <div className="flex gap-3 justify-center">
                <label className={`btn btn-accent ${uploading ? 'loading' : ''}`}>
                  {uploading ? 'Extracting clauses...' : 'Upload Quality Manual PDF'}
                  <input
                    type="file"
                    accept=".pdf"
                    className="hidden"
                    onChange={handlePdfUpload}
                    disabled={uploading}
                  />
                </label>
              </div>
              <p className="text-xs text-slate-400 mt-4">Or add clauses manually:</p>
              <div className="flex gap-2 justify-center mt-2">
                <button onClick={() => setShowBulkImport(true)} className="btn btn-outline btn-xs">
                  Bulk Import
                </button>
                <button onClick={() => setShowAddClause(true)} className="btn btn-ghost btn-xs">
                  + Add Single Clause
                </button>
              </div>
            </div>
          ) : (
            <div className="space-y-3">
              {selectedStandard.clauses.map(clause => (
                <ClauseRow
                  key={clause.id}
                  clause={clause}
                  onUpdateCompliance={handleUpdateCompliance}
                  onAddEvidence={(clauseId) => { setEvidenceClauseId(clauseId); setShowAddEvidence(true); }}
                  onVerifyEvidence={handleVerifyEvidence}
                  onDeleteEvidence={handleDeleteEvidence}
                />
              ))}
            </div>
          )}
        </div>
      )}

      {/* ===== MODALS ===== */}

      {/* Add Standard Modal */}
      {showAddStandard && (
        <Modal title="Add QMS Standard" onClose={() => setShowAddStandard(false)}>
          <div className="space-y-4">
            <div>
              <label className="label"><span className="label-text font-medium">Standard Name *</span></label>
              <input
                className="input input-bordered w-full"
                placeholder="e.g. AS9100D, ISO 9001:2015"
                value={standardForm.name}
                onChange={e => setStandardForm(f => ({ ...f, name: e.target.value }))}
              />
            </div>
            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="label"><span className="label-text">Version</span></label>
                <input
                  className="input input-bordered w-full"
                  placeholder="e.g. Rev D, 2015"
                  value={standardForm.version}
                  onChange={e => setStandardForm(f => ({ ...f, version: e.target.value }))}
                />
              </div>
              <div>
                <label className="label"><span className="label-text">Standard Body</span></label>
                <input
                  className="input input-bordered w-full"
                  placeholder="e.g. SAE International, ISO"
                  value={standardForm.standard_body}
                  onChange={e => setStandardForm(f => ({ ...f, standard_body: e.target.value }))}
                />
              </div>
            </div>
            <div>
              <label className="label"><span className="label-text">Description</span></label>
              <textarea
                className="textarea textarea-bordered w-full"
                rows={3}
                placeholder="Brief description of this standard..."
                value={standardForm.description}
                onChange={e => setStandardForm(f => ({ ...f, description: e.target.value }))}
              />
            </div>
            <div className="flex justify-end gap-2">
              <button className="btn btn-ghost btn-sm" onClick={() => setShowAddStandard(false)}>Cancel</button>
              <button className="btn btn-primary btn-sm" onClick={handleCreateStandard} disabled={saving || !standardForm.name}>
                {saving ? 'Creating...' : 'Create Standard'}
              </button>
            </div>
          </div>
        </Modal>
      )}

      {/* Add Clause Modal */}
      {showAddClause && (
        <Modal title="Add Clause" onClose={() => setShowAddClause(false)}>
          <div className="space-y-4">
            <div className="grid grid-cols-3 gap-4">
              <div>
                <label className="label"><span className="label-text font-medium">Clause # *</span></label>
                <input
                  className="input input-bordered w-full"
                  placeholder="e.g. 8.5.2"
                  value={clauseForm.clause_number}
                  onChange={e => setClauseForm(f => ({ ...f, clause_number: e.target.value }))}
                />
              </div>
              <div className="col-span-2">
                <label className="label"><span className="label-text font-medium">Title *</span></label>
                <input
                  className="input input-bordered w-full"
                  placeholder="e.g. Identification and Traceability"
                  value={clauseForm.title}
                  onChange={e => setClauseForm(f => ({ ...f, title: e.target.value }))}
                />
              </div>
            </div>
            <div>
              <label className="label"><span className="label-text">Description / Requirements</span></label>
              <textarea
                className="textarea textarea-bordered w-full"
                rows={4}
                placeholder="Full clause text or summary of requirements..."
                value={clauseForm.description}
                onChange={e => setClauseForm(f => ({ ...f, description: e.target.value }))}
              />
            </div>
            <div className="flex justify-end gap-2">
              <button className="btn btn-ghost btn-sm" onClick={() => setShowAddClause(false)}>Cancel</button>
              <button
                className="btn btn-primary btn-sm"
                onClick={handleCreateClause}
                disabled={saving || !clauseForm.clause_number || !clauseForm.title}
              >
                {saving ? 'Adding...' : 'Add Clause'}
              </button>
            </div>
          </div>
        </Modal>
      )}

      {/* Add Evidence Modal */}
      {showAddEvidence && (
        <Modal title="Link Evidence to Clause" onClose={() => { setShowAddEvidence(false); setEvidenceClauseId(null); }}>
          <div className="space-y-4">
            <div>
              <label className="label"><span className="label-text font-medium">Evidence Type *</span></label>
              <select
                className="select select-bordered w-full"
                value={evidenceForm.evidence_type}
                onChange={e => setEvidenceForm(f => ({ ...f, evidence_type: e.target.value }))}
              >
                {EVIDENCE_TYPES.map(t => (
                  <option key={t.value} value={t.value}>{t.label}</option>
                ))}
              </select>
            </div>
            <div>
              <label className="label"><span className="label-text font-medium">Title *</span></label>
              <input
                className="input input-bordered w-full"
                placeholder="e.g. Lot Traceability Module, QP-042 Calibration Procedure"
                value={evidenceForm.title}
                onChange={e => setEvidenceForm(f => ({ ...f, title: e.target.value }))}
              />
            </div>
            <div>
              <label className="label"><span className="label-text">Description</span></label>
              <textarea
                className="textarea textarea-bordered w-full"
                rows={3}
                placeholder="How does this evidence satisfy the clause requirement?"
                value={evidenceForm.description}
                onChange={e => setEvidenceForm(f => ({ ...f, description: e.target.value }))}
              />
            </div>
            <div>
              <label className="label"><span className="label-text">System Module Link</span></label>
              <input
                className="input input-bordered w-full"
                placeholder="e.g. /traceability, /quality, /calibration"
                value={evidenceForm.module_reference}
                onChange={e => setEvidenceForm(f => ({ ...f, module_reference: e.target.value }))}
              />
            </div>
            <div className="flex justify-end gap-2">
              <button className="btn btn-ghost btn-sm" onClick={() => { setShowAddEvidence(false); setEvidenceClauseId(null); }}>Cancel</button>
              <button
                className="btn btn-primary btn-sm"
                onClick={handleAddEvidence}
                disabled={saving || !evidenceForm.title}
              >
                {saving ? 'Linking...' : 'Link Evidence'}
              </button>
            </div>
          </div>
        </Modal>
      )}

      {/* Auto-Link Result Modal */}
      {showAutoLinkResult && autoLinkResult && (
        <Modal title="Auto-Link Evidence Complete" onClose={() => setShowAutoLinkResult(false)}>
          <div className="space-y-4">
            <div className="grid grid-cols-2 gap-3">
              <div className="bg-green-500/10 rounded-sm p-3 text-center">
                <p className="text-2xl font-bold text-emerald-400 tabular-nums">{autoLinkResult.clauses_with_evidence}</p>
                <p className="text-xs text-emerald-300">Clauses with Evidence</p>
              </div>
              <div className="bg-fd-raised rounded-sm p-3 text-center">
                <p className="text-2xl font-bold text-slate-400 tabular-nums">{autoLinkResult.clauses_without_evidence}</p>
                <p className="text-xs text-slate-400">Clauses without Match</p>
              </div>
              <div className="bg-blue-500/10 rounded-sm p-3 text-center">
                <p className="text-2xl font-bold text-blue-400 tabular-nums">{autoLinkResult.total_evidence_created}</p>
                <p className="text-xs text-blue-300">New Evidence Linked</p>
              </div>
              <div className="bg-amber-500/10 rounded-sm p-3 text-center">
                <p className="text-2xl font-bold text-amber-400 tabular-nums">{autoLinkResult.total_evidence_updated}</p>
                <p className="text-xs text-amber-300">Evidence Updated</p>
              </div>
            </div>

            {Object.keys(autoLinkResult.compliance_summary).length > 0 && (
              <div>
                <h4 className="text-sm font-semibold mb-2">Suggested Compliance</h4>
                <div className="flex gap-2 flex-wrap">
                  {Object.entries(autoLinkResult.compliance_summary).map(([status, count]) => (
                    <span key={status} className={`badge ${STATUS_COLORS[status] || 'badge-ghost'}`}>
                      {STATUS_LABELS[status] || status}: {count}
                    </span>
                  ))}
                </div>
              </div>
            )}

            <p className="text-xs text-slate-400">
              Evidence has been automatically linked from live ERP/MES records. Auto-linked items show a {'\u26A1'} badge and include real-time record counts.
            </p>

            <div className="flex justify-end">
              <button className="btn btn-primary btn-sm" onClick={() => setShowAutoLinkResult(false)}>
                Done
              </button>
            </div>
          </div>
        </Modal>
      )}

      {/* Bulk Import Modal */}
      {showBulkImport && (
        <Modal title="Bulk Import Clauses" onClose={() => setShowBulkImport(false)}>
          <div className="space-y-4">
            <p className="text-sm text-slate-400">
              Paste clause data, one per line. Use pipe (|) to separate fields:<br />
              <code className="text-xs bg-slate-800/50 px-1 rounded">clause_number | title | description</code>
            </p>
            <div className="bg-slate-800 p-3 rounded text-xs font-mono">
              <p>4.1 | Context of the Organization | Understanding the organization and its context</p>
              <p>4.2 | Needs and Expectations | Understanding needs of interested parties</p>
              <p>4.3 | Scope of the QMS | Determining the scope of the quality management system</p>
            </div>
            <textarea
              className="textarea textarea-bordered w-full font-mono text-sm"
              rows={12}
              placeholder="Paste clauses here..."
              value={bulkText}
              onChange={e => setBulkText(e.target.value)}
            />
            <div className="flex justify-between items-center">
              <span className="text-xs text-slate-400">
                {bulkText.trim() ? bulkText.trim().split('\n').filter(l => l.trim()).length : 0} clauses detected
              </span>
              <div className="flex gap-2">
                <button className="btn btn-ghost btn-sm" onClick={() => setShowBulkImport(false)}>Cancel</button>
                <button
                  className="btn btn-primary btn-sm"
                  onClick={handleBulkImport}
                  disabled={saving || !bulkText.trim()}
                >
                  {saving ? 'Importing...' : 'Import Clauses'}
                </button>
              </div>
            </div>
          </div>
        </Modal>
      )}
    </div>
  );
}

// ===== Sub-components =====

function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: React.ReactNode }) {
  return (
    <div className="modal modal-open">
      <div className="modal-box max-w-lg">
        <div className="flex items-center justify-between mb-4">
          <h3 className="font-bold text-lg">{title}</h3>
          <button className="btn btn-ghost btn-sm btn-circle" onClick={onClose}>X</button>
        </div>
        {children}
      </div>
      <div className="modal-backdrop" onClick={onClose}></div>
    </div>
  );
}

function ClauseRow({
  clause,
  onUpdateCompliance,
  onAddEvidence,
  onVerifyEvidence,
  onDeleteEvidence,
}: {
  clause: QMSClauseResponse;
  onUpdateCompliance: (id: number, status: ComplianceStatus, notes?: string) => void;
  onAddEvidence: (clauseId: number) => void;
  onVerifyEvidence: (evidenceId: number) => void;
  onDeleteEvidence: (evidenceId: number) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [autoEvidence, setAutoEvidence] = useState<ClauseAutoEvidenceResponse | null>(null);
  const [discovering, setDiscovering] = useState(false);

  const handleAutoDiscover = async () => {
    setDiscovering(true);
    try {
      const result = await api.getClauseAutoEvidence(clause.id);
      setAutoEvidence(result);
    } catch {
      // silently fail — user can retry
    } finally {
      setDiscovering(false);
    }
  };

  const autoLinkedCount = clause.evidence_links.filter(e => e.is_auto_linked).length;
  const manualCount = clause.evidence_links.length - autoLinkedCount;

  return (
    <div className="card card-compact !p-0 overflow-hidden">
      {/* Clause Header */}
      <div
        className="flex items-center gap-3 p-3 cursor-pointer hover:bg-fd-raised"
        onClick={() => setExpanded(!expanded)}
      >
        <span className="text-xs font-mono font-bold text-slate-400 w-16 shrink-0">
          {clause.clause_number}
        </span>
        <span className="flex-1 font-medium text-sm">{clause.title}</span>

        {/* Compliance Status Dropdown */}
        <select
          className={`select select-bordered select-xs ${STATUS_COLORS[clause.compliance_status] || ''}`}
          value={clause.compliance_status}
          onClick={e => e.stopPropagation()}
          onChange={e => onUpdateCompliance(clause.id, e.target.value as ComplianceStatus)}
        >
          {Object.entries(STATUS_LABELS).map(([val, label]) => (
            <option key={val} value={val}>{label}</option>
          ))}
        </select>

        {/* Evidence count badges */}
        {autoLinkedCount > 0 && (
          <span className="badge badge-sm badge-warning gap-1">{'\u26A1'} {autoLinkedCount}</span>
        )}
        {manualCount > 0 && (
          <span className="badge badge-sm badge-info">{manualCount} manual</span>
        )}

        <span className="text-slate-400 text-xs">{expanded ? '\u25B2' : '\u25BC'}</span>
      </div>

      {/* Expanded Details */}
      {expanded && (
        <div className="border-t border-fd-line px-4 pb-4 pt-3 space-y-3 bg-fd-sunken">
          {clause.description && (
            <p className="text-sm text-slate-400 whitespace-pre-wrap">{clause.description}</p>
          )}

          {/* Evidence Links */}
          <div>
            <div className="flex items-center justify-between mb-2">
              <h4 className="text-sm font-semibold">Evidence Links</h4>
              <div className="flex gap-2">
                <button
                  className={`btn btn-warning btn-xs ${discovering ? 'loading' : ''}`}
                  onClick={handleAutoDiscover}
                  disabled={discovering}
                  title="Discover live ERP/MES evidence for this clause"
                >
                  {discovering ? 'Scanning...' : '\u26A1 Auto-Discover'}
                </button>
                <button
                  className="btn btn-outline btn-xs"
                  onClick={() => onAddEvidence(clause.id)}
                >
                  + Link Evidence
                </button>
              </div>
            </div>

            {clause.evidence_links.length === 0 && !autoEvidence ? (
              <p className="text-xs text-slate-400 italic">No evidence linked. Click Auto-Discover or add evidence manually.</p>
            ) : (
              <div className="space-y-2">
                {clause.evidence_links.map(ev => (
                  <div key={ev.id} className={`flex items-center gap-3 rounded-sm border px-3 py-2 text-sm ${ev.is_auto_linked ? 'bg-amber-500/10 border-amber-500/30' : 'bg-fd-panel border-fd-line'}`}>
                    {ev.is_auto_linked && (
                      <span className="text-amber-500 text-xs font-bold" title="Auto-linked from ERP/MES">{'\u26A1'}</span>
                    )}
                    <span className="badge badge-ghost badge-xs">{ev.evidence_type}</span>
                    <span className="flex-1">
                      {ev.title}
                      {ev.is_auto_linked && ev.live_count != null && (
                        <span className="ml-2 text-xs text-amber-300 font-medium">
                          ({ev.live_count} records{ev.last_refreshed ? ` | Refreshed ${new Date(ev.last_refreshed).toLocaleDateString()}` : ''})
                        </span>
                      )}
                    </span>
                    {ev.module_reference && (
                      <a href={ev.module_reference} className="text-xs text-fd-blue hover:underline">
                        {ev.module_reference}
                      </a>
                    )}
                    {ev.is_verified ? (
                      <span className="badge badge-success badge-xs">Verified</span>
                    ) : (
                      <button
                        className="btn btn-outline btn-success btn-xs"
                        onClick={() => onVerifyEvidence(ev.id)}
                      >
                        Verify
                      </button>
                    )}
                    <button
                      className="btn btn-ghost btn-xs text-red-400"
                      onClick={() => onDeleteEvidence(ev.id)}
                    >
                      Remove
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Auto-Discovered Evidence Preview */}
          {autoEvidence && autoEvidence.discovered_evidence.length > 0 && (
            <div>
              <h4 className="text-sm font-semibold mb-2 text-amber-400">
                {'\u26A1'} Discovered Evidence (Live Preview)
              </h4>
              <div className="space-y-2">
                {autoEvidence.discovered_evidence.map((ev, idx) => (
                  <div key={idx} className="bg-amber-500/10 border border-amber-500/30 rounded-sm p-3">
                    <div className="flex items-center gap-2 mb-1">
                      <HealthDot status={ev.health_status} />
                      <span className="font-medium text-sm">{ev.title}</span>
                      <span className="badge badge-sm badge-warning">{ev.total_count} records</span>
                    </div>
                    <p className="text-xs text-slate-400 mb-2">{ev.description}</p>
                    <p className="text-xs text-slate-400 italic">{ev.health_detail}</p>
                    {ev.examples.length > 0 && (
                      <div className="mt-2 space-y-1">
                        {ev.examples.slice(0, 3).map((ex, exIdx) => (
                          <a
                            key={exIdx}
                            href={ex.module_link}
                            className="flex items-center gap-2 text-xs text-fd-blue hover:text-fd-cyan hover:underline"
                          >
                            <span className="font-mono">{ex.record_identifier}</span>
                            <span className="text-slate-400">{ex.summary}</span>
                            <span className={`badge badge-xs ${ex.status === 'closed' ? 'badge-success' : 'badge-ghost'}`}>
                              {ex.status}
                            </span>
                          </a>
                        ))}
                      </div>
                    )}
                  </div>
                ))}
              </div>
              <p className="text-xs text-slate-400 mt-2">
                Suggested compliance: <span className={`font-medium ${STATUS_COLORS[autoEvidence.overall_suggested_compliance] || ''}`}>
                  {STATUS_LABELS[autoEvidence.overall_suggested_compliance] || autoEvidence.overall_suggested_compliance}
                </span>
              </p>
            </div>
          )}

          {autoEvidence && autoEvidence.discovered_evidence.length === 0 && (
            <p className="text-xs text-slate-400 italic">No matching ERP/MES records found for this clause.</p>
          )}

          {clause.compliance_notes && (
            <div className="text-xs text-slate-400">
              <strong>Notes:</strong> {clause.compliance_notes}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function HealthDot({ status }: { status: string }) {
  const colors: Record<string, string> = {
    healthy: 'bg-fd-green',
    warning: 'bg-fd-amber',
    critical: 'bg-fd-red',
    no_data: 'bg-fd-line-bright',
  };
  return (
    <span
      className={`inline-block w-2.5 h-2.5 rounded-full ${colors[status] || colors.no_data}`}
      title={status}
    />
  );
}
