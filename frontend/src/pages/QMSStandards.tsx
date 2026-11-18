import React, { useState, useEffect, useCallback, useLayoutEffect, useMemo, useRef } from 'react';
import { Link } from 'react-router-dom';
import {
  RectangleStackIcon,
  ListBulletIcon,
  CheckCircleIcon,
  ExclamationTriangleIcon,
  XCircleIcon,
  QuestionMarkCircleIcon,
} from '@heroicons/react/24/outline';
import api from '../services/api';
// The shared dialog primitive, aliased because this page still carries its own
// older local `Modal` helper (below) for its form dialogs.
import { Button, ComboBox, ConfirmDialog, Modal as UiModal, statusColor, useToast } from '../components/ui';
import type { ComboBoxOption } from '../components/ui';
import { FormField } from '../components/ui/FormField';
import { MiniStat, MiniStatStrip } from '../components/cockpit';
import { formatCentralDate } from '../utils/centralTime';
import { useUnsavedChanges } from '../hooks/useUnsavedChanges';
import {
  QMSStandardListResponse,
  QMSStandardResponse,
  QMSClauseResponse,
  QMSAuditReadinessSummary,
  ClauseAutoEvidenceResponse,
  AutoLinkSummary,
} from '../types/api';

type ComplianceStatus = 'not_assessed' | 'compliant' | 'partial' | 'non_compliant' | 'not_applicable';

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

/** A company document offered by the evidence dialog's "Linked Document" picker. */
export interface EvidenceDocumentChoice {
  id: number;
  document_number: string;
  revision: string;
  title: string;
  document_type: string;
  previous_revision_id?: number | null;
}

/** Shipping and receiving paperwork: printed by their own flows, never QMS evidence. */
const NON_EVIDENCE_DOCUMENT_TYPES: ReadonlySet<string> = new Set(['receiving_label', 'shipping_label', 'bill_of_lading']);

/**
 * The documents the evidence picker offers: the CURRENT revision of each document, minus
 * shipping/receiving paperwork. A revision upload is a new row that names its predecessor in
 * `previous_revision_id`, and the predecessor stays `released`, so without this filter an
 * obsolete revision would be offered as an equal choice (AS9100D 7.5.3.2). Sorted by title.
 */
export function currentEvidenceDocuments(docs: EvidenceDocumentChoice[]): EvidenceDocumentChoice[] {
  const superseded = new Set<number>();
  for (const doc of docs) if (doc.previous_revision_id != null) superseded.add(doc.previous_revision_id);
  return docs
    .filter(doc => !superseded.has(doc.id) && !NON_EVIDENCE_DOCUMENT_TYPES.has(doc.document_type))
    .sort((a, b) => a.title.localeCompare(b.title));
}

/** The evidence title the picker fills in for a document. */
const documentEvidenceTitle = (doc: EvidenceDocumentChoice) => `${doc.title}, Rev ${doc.revision}`;

const EMPTY_EVIDENCE_FORM = { evidence_type: 'document', title: '', description: '', module_reference: '', document_id: '' };

/** The Linked Document picker's input id, so focus can return to it after a Retry. */
const EVIDENCE_DOCUMENT_FIELD_ID = 'qms-evidence-linked-document';

/** Where a linked document opens: the Documents page previews the one named in `document`. */
export function linkedDocumentHref(documentId: number): string {
  return `/documents?document=${documentId}`;
}

/**
 * What an auto-link run that could not compute some evidence rules says about
 * them. Nothing was saved for a failed rule, so its existing evidence link (if
 * any) still shows the counts and refresh date of the last run that computed it.
 */
function evidenceNotComputedMessage(failed: number): string {
  const rules = failed === 1 ? 'evidence rule' : 'evidence rules';
  const links = failed === 1 ? 'Its evidence link was' : 'Their evidence links were';
  return `${failed} ${rules} could not be computed this run. ${links} left as last refreshed, not updated.`;
}

/** Why auto-link kept a stale link: the API's `reason`, in words. */
const KEPT_REASON_LABELS: Record<string, string> = {
  verified: 'Verified',
  annotated: 'Document or notes attached',
};

/**
 * What an auto-link run says about stale auto-linked links it did not retire
 * because a person reviewed them. The run succeeded, but each one is left for
 * a person to decide.
 */
function keptReviewedMessage(kept: number): string {
  if (kept === 1) {
    return (
      '1 auto-linked evidence link no longer matches its clause but was kept because a person ' +
      'verified or annotated it. Check whether it still belongs.'
    );
  }
  return (
    `${kept} auto-linked evidence links no longer match their clauses but were kept because a person ` +
    'verified or annotated them. Check whether each still belongs.'
  );
}

export default function QMSStandards() {
  const { showToast } = useToast();
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
  const [evidenceForm, setEvidenceForm] = useState(EMPTY_EVIDENCE_FORM);
  const [evidenceError, setEvidenceError] = useState('');
  // The company's documents, loaded the first time the evidence dialog opens. null = not loaded yet.
  const [evidenceDocs, setEvidenceDocs] = useState<EvidenceDocumentChoice[] | null>(null);
  const [evidenceDocsFailed, setEvidenceDocsFailed] = useState(false);
  const [evidenceDocsReload, setEvidenceDocsReload] = useState(0);
  const [evidenceDocNotice, setEvidenceDocNotice] = useState('');
  // The evidence title the picker last wrote, so choosing another document can replace it.
  const autoEvidenceTitle = useRef<string | null>(null);
  const retryFocusPending = useRef(false);
  const retryButtonRef = useRef<HTMLButtonElement>(null);
  const [bulkText, setBulkText] = useState('');
  const [saving, setSaving] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState('');

  // Auto-link state
  const [autoLinking, setAutoLinking] = useState(false);
  const [deleteEvidenceTarget, setDeleteEvidenceTarget] = useState<number | null>(null);
  const [deleteEvidencePending, setDeleteEvidencePending] = useState(false);
  const [autoLinkResult, setAutoLinkResult] = useState<AutoLinkSummary | null>(null);
  const [showAutoLinkResult, setShowAutoLinkResult] = useState(false);
  const keptForReview = autoLinkResult?.evidence_kept_reviewed ?? [];

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

  // Load the document choices every time the evidence dialog opens, so a document uploaded
  // since the last open is offered. A list from an earlier open stays usable while the new one
  // loads. The picker is optional: the rest of the form works while it loads or if it cannot.
  useEffect(() => {
    if (!showAddEvidence) return;
    let active = true;
    // getDocuments pages through every document; a closed or reopened dialog stops the old read.
    const controller = new AbortController();
    setEvidenceDocsFailed(false);
    api
      .getDocuments(undefined, controller.signal)
      .then((docs: EvidenceDocumentChoice[]) => {
        if (active) setEvidenceDocs(docs);
      })
      .catch(() => {
        if (active) setEvidenceDocsFailed(true);
      });
    return () => {
      active = false;
      controller.abort();
    };
  }, [showAddEvidence, evidenceDocsReload]);

  const evidenceDocOptions = useMemo<ComboBoxOption[]>(
    () =>
      currentEvidenceDocuments(evidenceDocs ?? []).map(doc => ({
        value: String(doc.id),
        label: doc.title,
        hint: `${doc.document_number} Rev ${doc.revision}`,
      })),
    [evidenceDocs]
  );

  // A choice made from an earlier list can stop being offered when the reloaded list arrives:
  // a newer revision superseded it, or it was removed. The field would then show blank while the
  // old id was still sent, linking an obsolete revision. Drop the choice (and a picker-written
  // title, as a manual clear would) and say why.
  useEffect(() => {
    const chosen = evidenceForm.document_id;
    if (!chosen || evidenceDocs === null || evidenceDocOptions.some(o => o.value === chosen)) return;
    const autoTitle = autoEvidenceTitle.current;
    const titleIsAuto = autoTitle !== null && evidenceForm.title === autoTitle;
    if (titleIsAuto) autoEvidenceTitle.current = null;
    setEvidenceForm({ ...evidenceForm, document_id: '', title: titleIsAuto ? '' : evidenceForm.title });
    setEvidenceDocNotice(
      'The document you chose has a newer revision or was removed, so it was cleared. Choose the current one again.'
    );
  }, [evidenceDocOptions, evidenceDocs, evidenceForm]);

  // After a Retry the button unmounts; put focus somewhere useful rather than on <body>.
  useEffect(() => {
    if (!retryFocusPending.current) return;
    if (evidenceDocsFailed) {
      retryFocusPending.current = false;
      retryButtonRef.current?.focus();
    } else if (evidenceDocs !== null) {
      retryFocusPending.current = false;
      document.getElementById(EVIDENCE_DOCUMENT_FIELD_ID)?.focus();
    }
  }, [evidenceDocs, evidenceDocsFailed]);

  const handleEvidenceDocumentChange = (value: string) => {
    setEvidenceDocNotice('');
    const doc = evidenceDocs?.find(d => String(d.id) === value);
    // The title follows the chosen document while it is empty or still the picker's own
    // wording; a title the user typed is never replaced. Clearing the document clears a
    // picker-written title, so a row is never named after a document it does not link.
    const autoTitle = autoEvidenceTitle.current;
    const replaceable = !evidenceForm.title.trim() || (autoTitle !== null && evidenceForm.title === autoTitle);
    const nextTitle = replaceable ? (doc ? documentEvidenceTitle(doc) : '') : evidenceForm.title;
    autoEvidenceTitle.current = replaceable ? (doc ? nextTitle : null) : autoTitle;
    setEvidenceForm({ ...evidenceForm, document_id: value, title: nextTitle });
  };

  const evidenceDirty =
    showAddEvidence &&
    (evidenceForm.document_id !== '' ||
      evidenceForm.evidence_type !== EMPTY_EVIDENCE_FORM.evidence_type ||
      [evidenceForm.title, evidenceForm.description, evidenceForm.module_reference].some(v => v.trim() !== ''));
  const { confirmDiscard: confirmDiscardEvidence, markSaved: markEvidenceSaved } = useUnsavedChanges(evidenceDirty);

  const resetEvidenceDialog = () => {
    setShowAddEvidence(false);
    setEvidenceClauseId(null);
    setEvidenceForm(EMPTY_EVIDENCE_FORM);
    setEvidenceError('');
    setEvidenceDocNotice('');
    autoEvidenceTitle.current = null;
  };

  // Close, Cancel and Escape all come here; anything typed is discarded only after a confirm.
  const closeEvidenceDialog = () => {
    if (saving) return;
    if (!confirmDiscardEvidence()) return;
    resetEvidenceDialog();
  };

  const handleAddEvidence = async () => {
    if (!evidenceClauseId || !evidenceForm.title.trim()) return;
    const { document_id, ...fields } = evidenceForm;
    // Belt and braces for the reload effect above: never send a document the picker no longer offers.
    if (document_id && !evidenceDocOptions.some(o => o.value === document_id)) {
      setEvidenceError('The chosen document is no longer the current revision. Choose it again.');
      return;
    }
    setSaving(true);
    setEvidenceError('');
    try {
      await api.addQMSEvidence(evidenceClauseId, document_id ? { ...fields, document_id: Number(document_id) } : fields);
      markEvidenceSaved();
      resetEvidenceDialog();
      if (selectedStandard) loadStandardDetail(selectedStandard.id);
    } catch (err: any) {
      // Shown in the dialog, which stays open so nothing typed is lost. A document that is not
      // one of this company's answers 404 "Document not found", and nothing is linked.
      const detail = err?.response?.data?.detail;
      setEvidenceError(typeof detail === 'string' && detail ? detail : 'Failed to link evidence. Try again.');
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
      // The run succeeded but did not compute everything it was asked to, or
      // left stale links a person reviewed for a person to check: a warning, not
      // a success (which would hide it) or an error (nothing failed to save).
      const failed = result.total_evidence_failed;
      const kept = result.evidence_kept_reviewed?.length ?? 0;
      const notices: string[] = [];
      if (failed > 0) notices.push(evidenceNotComputedMessage(failed));
      if (kept > 0) notices.push(keptReviewedMessage(kept));
      if (notices.length > 0) {
        const lead = failed > 0 ? 'Auto-link finished with gaps.' : 'Auto-link finished; review needed.';
        showToast('warning', `${lead} ${notices.join(' ')}`);
      }
      // Refresh the standard detail to show new evidence
      loadStandardDetail(selectedStandard.id);
      fetchData();
    } catch (err: any) {
      setError(err?.response?.data?.detail || 'Failed to auto-link evidence');
    } finally {
      setAutoLinking(false);
    }
  };

  const handleDeleteEvidence = (evidenceId: number) => {
    setDeleteEvidenceTarget(evidenceId);
  };

  const handleConfirmDeleteEvidence = async () => {
    if (deleteEvidenceTarget === null || deleteEvidencePending) return;
    setDeleteEvidencePending(true);
    try {
      await api.deleteQMSEvidence(deleteEvidenceTarget);
      if (selectedStandard) loadStandardDetail(selectedStandard.id);
    } catch (err: any) {
      setError(err?.response?.data?.detail || 'Failed to delete evidence');
    } finally {
      setDeleteEvidencePending(false);
      setDeleteEvidenceTarget(null);
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
          <Button size="sm" onClick={() => setShowAddStandard(true)}>
            + Add Standard
          </Button>
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
                aria-label="Upload PDF"
                className="hidden"
                onChange={handlePdfUpload}
                disabled={uploading}
              />
            </label>
            <Button variant="secondary" size="sm" onClick={() => setShowBulkImport(true)}>
              Bulk Import
            </Button>
            <Button size="sm" onClick={() => setShowAddClause(true)}>
              + Add Clause
            </Button>
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
              <Button size="sm" onClick={() => setShowAddStandard(true)}>
                Add Your First Standard
              </Button>
            </div>
          ) : (
            <div className="grid gap-3 md:grid-cols-2 lg:grid-cols-3">
              {standards.map(std => (
                <button
                  type="button"
                  key={std.id}
                  onClick={() => loadStandardDetail(std.id)}
                  className="card card-compact !p-3 min-w-0 cursor-pointer hover:border-fd-line-bright transition-colors text-left w-full block"
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
                </button>
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
                    aria-label="Upload quality manual PDF"
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
            <FormField label={<span className="label-text font-medium">Standard Name</span>} required>
              {field => (
                <input
                  {...field}
                  className="input input-bordered w-full"
                  placeholder="e.g. AS9100D, ISO 9001:2015"
                  value={standardForm.name}
                  onChange={e => setStandardForm(f => ({ ...f, name: e.target.value }))}
                />
              )}
            </FormField>
            <div className="grid grid-cols-2 gap-4">
              <FormField label={<span className="label-text">Version</span>}>
                {field => (
                  <input
                    {...field}
                    className="input input-bordered w-full"
                    placeholder="e.g. Rev D, 2015"
                    value={standardForm.version}
                    onChange={e => setStandardForm(f => ({ ...f, version: e.target.value }))}
                  />
                )}
              </FormField>
              <FormField label={<span className="label-text">Standard Body</span>}>
                {field => (
                  <input
                    {...field}
                    className="input input-bordered w-full"
                    placeholder="e.g. SAE International, ISO"
                    value={standardForm.standard_body}
                    onChange={e => setStandardForm(f => ({ ...f, standard_body: e.target.value }))}
                  />
                )}
              </FormField>
            </div>
            <FormField label={<span className="label-text">Description</span>}>
              {field => (
                <textarea
                  {...field}
                  className="textarea textarea-bordered w-full"
                  rows={3}
                  placeholder="Brief description of this standard..."
                  value={standardForm.description}
                  onChange={e => setStandardForm(f => ({ ...f, description: e.target.value }))}
                />
              )}
            </FormField>
            <div className="flex justify-end gap-2">
              <Button variant="ghost" size="sm" onClick={() => setShowAddStandard(false)}>Cancel</Button>
              <Button size="sm" onClick={handleCreateStandard} disabled={saving || !standardForm.name}>
                {saving ? 'Creating...' : 'Create Standard'}
              </Button>
            </div>
          </div>
        </Modal>
      )}

      {/* Add Clause Modal */}
      {showAddClause && (
        <Modal title="Add Clause" onClose={() => setShowAddClause(false)}>
          <div className="space-y-4">
            <div className="grid grid-cols-3 gap-4">
              <FormField label={<span className="label-text font-medium">Clause #</span>} required>
                {field => (
                  <input
                    {...field}
                    className="input input-bordered w-full"
                    placeholder="e.g. 8.5.2"
                    value={clauseForm.clause_number}
                    onChange={e => setClauseForm(f => ({ ...f, clause_number: e.target.value }))}
                  />
                )}
              </FormField>
              <FormField label={<span className="label-text font-medium">Title</span>} required className="col-span-2">
                {field => (
                  <input
                    {...field}
                    className="input input-bordered w-full"
                    placeholder="e.g. Identification and Traceability"
                    value={clauseForm.title}
                    onChange={e => setClauseForm(f => ({ ...f, title: e.target.value }))}
                  />
                )}
              </FormField>
            </div>
            <FormField label={<span className="label-text">Description / Requirements</span>}>
              {field => (
                <textarea
                  {...field}
                  className="textarea textarea-bordered w-full"
                  rows={4}
                  placeholder="Full clause text or summary of requirements..."
                  value={clauseForm.description}
                  onChange={e => setClauseForm(f => ({ ...f, description: e.target.value }))}
                />
              )}
            </FormField>
            <div className="flex justify-end gap-2">
              <Button variant="ghost" size="sm" onClick={() => setShowAddClause(false)}>Cancel</Button>
              <Button
                size="sm"
                onClick={handleCreateClause}
                disabled={saving || !clauseForm.clause_number || !clauseForm.title}
              >
                {saving ? 'Adding...' : 'Add Clause'}
              </Button>
            </div>
          </div>
        </Modal>
      )}

      {/* Add Evidence Modal: the shared primitive (z-[60]), because the document picker's list
          portals at z-[70] and would open behind this page's older DaisyUI modal (z-index 999). */}
      {showAddEvidence && (
        <UiModal open onClose={closeEvidenceDialog} size="md" closeOnBackdrop={false} ariaLabel="Link Evidence to Clause">
          <div className="flex items-center justify-between mb-4">
            <h3 className="font-bold text-lg">Link Evidence to Clause</h3>
            <button
              type="button"
              className="btn btn-ghost btn-sm btn-circle"
              onClick={closeEvidenceDialog}
              disabled={saving}
              aria-label="Close"
            >
              X
            </button>
          </div>
          {evidenceError && (
            <p role="alert" className="mb-4 text-sm text-red-300">
              {evidenceError}
            </p>
          )}
          {evidenceDocNotice && (
            <p role="status" className="mb-4 text-sm text-amber-300">
              {evidenceDocNotice}
            </p>
          )}
          <div className="space-y-4">
            <FormField label={<span className="label-text font-medium">Evidence Type</span>} required>
              {field => (
                <select
                  {...field}
                  className="select select-bordered w-full"
                  value={evidenceForm.evidence_type}
                  onChange={e => setEvidenceForm(f => ({ ...f, evidence_type: e.target.value }))}
                >
                  {EVIDENCE_TYPES.map(t => (
                    <option key={t.value} value={t.value}>{t.label}</option>
                  ))}
                </select>
              )}
            </FormField>
            <FormField
              label={<span className="label-text font-medium">Linked Document</span>}
              htmlFor={EVIDENCE_DOCUMENT_FIELD_ID}
              help={
                evidenceDocsFailed ? (
                  <>
                    <span role="status">The document list could not be loaded.</span>{' '}
                    <button
                      ref={retryButtonRef}
                      type="button"
                      className="link link-primary"
                      onClick={() => {
                        retryFocusPending.current = true;
                        setEvidenceDocsReload(n => n + 1);
                      }}
                    >
                      Retry
                    </button>{' '}
                    You can still link evidence without a document.
                  </>
                ) : (
                  'Optional. Pick the current revision of an uploaded procedure, form or record so it opens from this clause. Upload new documents on the Documents page.'
                )
              }
            >
              {field => (
                <ComboBox
                  id={field.id}
                  ariaDescribedBy={field['aria-describedby']}
                  options={evidenceDocOptions}
                  value={evidenceForm.document_id}
                  onChange={handleEvidenceDocumentChange}
                  emptyOptionLabel="No document"
                  placeholder={evidenceDocs === null && !evidenceDocsFailed ? 'Loading documents…' : 'Search by title or document number…'}
                  noResultsLabel="No matching documents"
                  disabled={evidenceDocs === null || saving}
                />
              )}
            </FormField>
            <FormField label={<span className="label-text font-medium">Title</span>} required>
              {field => (
                <input
                  {...field}
                  className="input input-bordered w-full"
                  placeholder="e.g. Lot Traceability Module, QP-042 Calibration Procedure"
                  value={evidenceForm.title}
                  onChange={e => setEvidenceForm(f => ({ ...f, title: e.target.value }))}
                />
              )}
            </FormField>
            <FormField label={<span className="label-text">Description</span>}>
              {field => (
                <textarea
                  {...field}
                  className="textarea textarea-bordered w-full"
                  rows={3}
                  placeholder="How does this evidence satisfy the clause requirement?"
                  value={evidenceForm.description}
                  onChange={e => setEvidenceForm(f => ({ ...f, description: e.target.value }))}
                />
              )}
            </FormField>
            <FormField label={<span className="label-text">System Module Link</span>}>
              {field => (
                <input
                  {...field}
                  className="input input-bordered w-full"
                  placeholder="e.g. /traceability, /quality, /calibration"
                  value={evidenceForm.module_reference}
                  onChange={e => setEvidenceForm(f => ({ ...f, module_reference: e.target.value }))}
                />
              )}
            </FormField>
            <div className="flex justify-end gap-2">
              <Button variant="ghost" size="sm" onClick={closeEvidenceDialog} disabled={saving}>Cancel</Button>
              <Button
                size="sm"
                onClick={handleAddEvidence}
                disabled={saving || !evidenceForm.title.trim()}
              >
                {saving ? 'Linking...' : 'Link Evidence'}
              </Button>
            </div>
          </div>
        </UiModal>
      )}

      {/* Auto-Link Result Modal */}
      {autoLinkResult && (
        <UiModal open={showAutoLinkResult} onClose={() => setShowAutoLinkResult(false)} size="lg">
          <div className="flex items-center justify-between mb-4">
            <h3 className="font-bold text-lg">
              {autoLinkResult.total_evidence_failed > 0 ? 'Auto-Link Evidence Finished With Gaps' : 'Auto-Link Evidence Complete'}
            </h3>
            <button
              type="button"
              className="btn btn-ghost btn-sm btn-circle"
              aria-label="Close"
              onClick={() => setShowAutoLinkResult(false)}
            >
              X
            </button>
          </div>
          <div className="space-y-4">
            {autoLinkResult.total_evidence_failed > 0 && (
              <div className="bg-red-500/10 border border-red-500/30 rounded-sm p-3" data-testid="auto-link-not-computed">
                <div className="flex items-baseline gap-3">
                  <p className="text-2xl font-bold text-red-400 tabular-nums">{autoLinkResult.total_evidence_failed}</p>
                  <p className="text-xs font-semibold text-red-300">Evidence Not Computed</p>
                </div>
                <p className="text-xs text-slate-300 mt-1">
                  {evidenceNotComputedMessage(autoLinkResult.total_evidence_failed)}{' '}
                  A clause with an uncomputed rule is not counted as compliant. Run Auto-Link again to retry.
                </p>
              </div>
            )}
            {keptForReview.length > 0 && (
              <div className="bg-amber-500/10 border border-amber-500/30 rounded-sm p-3" data-testid="auto-link-kept-reviewed">
                <div className="flex items-baseline gap-3">
                  <p className="text-2xl font-bold text-amber-400 tabular-nums">{keptForReview.length}</p>
                  <p className="text-xs font-semibold text-amber-300">Kept for Review</p>
                </div>
                <p className="text-xs text-slate-300 mt-1">
                  {keptReviewedMessage(keptForReview.length)} If a link no longer applies, remove it from its clause.
                </p>
                <ul className="mt-2 divide-y divide-amber-500/20" aria-label="Evidence links kept for review">
                  {keptForReview.map(item => (
                    <li key={item.evidence_id} className="flex items-baseline gap-3 py-1 text-xs">
                      <span className="font-mono font-semibold text-slate-200">Clause {item.clause_number}</span>
                      <span className="text-amber-200">{KEPT_REASON_LABELS[item.reason] ?? item.reason}</span>
                      {item.rule_id && <span className="ml-auto font-mono text-slate-500">rule {item.rule_id}</span>}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            <div className="grid grid-cols-2 gap-3">
              <div className="bg-green-500/10 rounded-sm p-3 text-center">
                <p className="text-2xl font-bold text-emerald-400 tabular-nums">{autoLinkResult.clauses_with_evidence}</p>
                <p className="text-xs text-emerald-300">Clauses with Evidence</p>
              </div>
              <div className="bg-fd-raised rounded-sm p-3 text-center">
                <p className="text-2xl font-bold text-slate-400 tabular-nums">{autoLinkResult.clauses_without_evidence}</p>
                {/* Includes a clause whose matched rules all failed: it matched, but nothing was computed. */}
                <p className="text-xs text-slate-400">Clauses without Evidence</p>
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
            {/* Absent is not zero: an API from before retirement existed sends no count. */}
            {typeof autoLinkResult.total_evidence_retired === 'number' && (
              <div className="bg-fd-raised rounded-sm p-3" data-testid="auto-link-retired">
                <div className="flex items-baseline gap-3">
                  <p className="text-2xl font-bold text-slate-300 tabular-nums">{autoLinkResult.total_evidence_retired}</p>
                  <p className="text-xs font-semibold text-slate-300">Stale Links Retired</p>
                </div>
                {autoLinkResult.total_evidence_retired > 0 && (
                  <p className="text-xs text-slate-400 mt-1">
                    Auto-linked evidence links whose rule no longer matches their clause were removed, and each removal
                    is recorded in the audit log. Clause compliance statuses were not changed: reassess any clause that
                    relied on a removed link.
                  </p>
                )}
              </div>
            )}

            {Object.keys(autoLinkResult.compliance_summary).length > 0 && (
              <div>
                <h4 className="text-sm font-semibold mb-2">Suggested Compliance</h4>
                <div className="flex gap-2 flex-wrap">
                  {Object.entries(autoLinkResult.compliance_summary).map(([status, count]) => (
                    <span key={status} className={`px-2 py-0.5 rounded-sm text-xs font-medium ${statusColor(status)}`}>
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
              <Button size="sm" onClick={() => setShowAutoLinkResult(false)}>
                Done
              </Button>
            </div>
          </div>
        </UiModal>
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
              aria-label="Bulk clause import data"
              value={bulkText}
              onChange={e => setBulkText(e.target.value)}
            />
            <div className="flex justify-between items-center">
              <span className="text-xs text-slate-400">
                {bulkText.trim() ? bulkText.trim().split('\n').filter(l => l.trim()).length : 0} clauses detected
              </span>
              <div className="flex gap-2">
                <Button variant="ghost" size="sm" onClick={() => setShowBulkImport(false)}>Cancel</Button>
                <Button
                  size="sm"
                  onClick={handleBulkImport}
                  disabled={saving || !bulkText.trim()}
                >
                  {saving ? 'Importing...' : 'Import Clauses'}
                </Button>
              </div>
            </div>
          </div>
        </Modal>
      )}

      {/* Remove evidence link confirm */}
      <ConfirmDialog
        open={deleteEvidenceTarget !== null}
        title="Remove Evidence Link"
        message="Remove this evidence link?"
        confirmLabel="Remove"
        pending={deleteEvidencePending}
        variant="danger"
        onConfirm={handleConfirmDeleteEvidence}
        onCancel={() => {
          if (!deleteEvidencePending) setDeleteEvidenceTarget(null);
        }}
      />
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
      <div
        className="modal-backdrop"
        role="presentation"
        onClick={e => { if (e.target === e.currentTarget) onClose(); }}
      ></div>
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
        role="button"
        tabIndex={0}
        onClick={() => setExpanded(!expanded)}
        onKeyDown={e => { if (e.target !== e.currentTarget) return; if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setExpanded(!expanded); } }}
      >
        <span className="text-xs font-mono font-bold text-slate-400 w-16 shrink-0">
          {clause.clause_number}
        </span>
        <span className="flex-1 font-medium text-sm">{clause.title}</span>

        {/* Compliance Status Dropdown */}
        <select
          className={`select select-bordered select-xs ${statusColor(clause.compliance_status)}`}
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
                  <div
                    key={ev.id}
                    data-testid="linked-evidence-row"
                    className={`flex flex-wrap items-baseline gap-x-3 gap-y-2 rounded-sm border px-3 py-2 text-sm ${ev.is_auto_linked ? 'bg-amber-500/10 border-amber-500/30' : 'bg-fd-panel border-fd-line'}`}
                  >
                    {/* items-baseline: the title's first line, the link and the 44px buttons' labels share a baseline, with or without a description below. */}
                    {ev.is_auto_linked && (
                      <span className="text-amber-500 text-xs font-bold" title="Auto-linked from ERP/MES">{'\u26A1'}</span>
                    )}
                    <span className="badge badge-ghost badge-xs">{ev.evidence_type}</span>
                    {/* min-w-48: on a narrow screen the link and buttons wrap below the text rather than squeezing it to one character wide. */}
                    <div className="flex-1 min-w-48">
                      <span className="break-words">
                        {ev.title}
                        {ev.is_auto_linked && ev.live_count != null && (
                          <span className="ml-2 text-xs text-amber-300 font-medium">
                            ({ev.live_count} records{ev.last_refreshed ? ` | Refreshed ${formatCentralDate(ev.last_refreshed)}` : ''})
                          </span>
                        )}
                      </span>
                      {ev.description && (
                        <EvidenceDescription
                          id={`evidence-${ev.id}-description`}
                          text={ev.description}
                          autoLinked={ev.is_auto_linked}
                        />
                      )}
                    </div>
                    {ev.document_id != null && (
                      <Link
                        to={linkedDocumentHref(ev.document_id)}
                        className="text-xs text-fd-blue hover:underline"
                        aria-label={`Open document: ${ev.title}`}
                      >
                        Open document
                      </Link>
                    )}
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
                {autoEvidence.discovered_evidence.map((ev, idx) => {
                  // A rule whose query failed comes back as a placeholder with zero
                  // counts. Those zeros are not measurements: showing "0 records" would
                  // read exactly like a module with nothing on file (no_data).
                  const notComputed = ev.health_status === 'not_computed';
                  return (
                    <div
                      key={idx}
                      data-testid="discovered-evidence-row"
                      className={`rounded-sm p-3 border ${notComputed ? 'bg-red-500/10 border-red-500/30' : 'bg-amber-500/10 border-amber-500/30'}`}
                    >
                      <div className="flex items-center gap-2 mb-1">
                        <HealthDot status={ev.health_status} />
                        <span className="font-medium text-sm">{ev.title}</span>
                        {notComputed ? (
                          <span className="badge badge-sm badge-error badge-outline">Not computed</span>
                        ) : (
                          <span className="badge badge-sm badge-warning">{ev.total_count} records</span>
                        )}
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
                  );
                })}
              </div>
              <p className="text-xs text-slate-400 mt-2">
                Suggested compliance: <span className={`font-medium ${statusColor(autoEvidence.overall_suggested_compliance)}`}>
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

/** A manual description taller than this many rendered lines starts clamped behind "Show more". */
const EVIDENCE_DESCRIPTION_CLAMP_LINES = 3;

/**
 * Whether a description's full text is taller than EVIDENCE_DESCRIPTION_CLAMP_LINES lines.
 * `scrollHeight` is the full content height whether or not the element is clamped, so the
 * same reading decides both "start clamping" and "stop clamping". jsdom has no layout (its
 * computed line height is empty and `scrollHeight` is 0), so tests stub `scrollHeight`.
 */
function descriptionOverflows(el: HTMLElement): boolean {
  const style = window.getComputedStyle(el);
  // text-xs: 12px font, 16px line. Fall back to that ratio if the line height is "normal".
  const lineHeight = parseFloat(style.lineHeight) || (parseFloat(style.fontSize) || 12) * (4 / 3);
  return el.scrollHeight > lineHeight * EVIDENCE_DESCRIPTION_CLAMP_LINES + 1;
}

/**
 * A stored evidence link's `description`, under its title in the clause's linked-evidence list.
 *
 * Auto-link stores only `description` from a live evidence row (never `health_status` or
 * `health_detail`), so this is the one place on the page an auditor reads what that row
 * disclosed on its last refresh: a paused audit hash chain, missing audit triggers, and so on.
 * Those disclosures are appended at the END of the text ("…integrity chain enabled, but 12
 * entries were written while it was paused"), so an auto-linked description is never clamped:
 * a clamp could show the claim and hide the qualification after it.
 *
 * A manual description is free text (up to 5,000 characters). It is clamped to three lines only
 * while its measured height says it needs more, and only then is the "Show more" control shown:
 * clamped and having the control are the same state, so no text is ever hidden without a way to
 * reveal it. It is re-measured when the element resizes (window width, fonts loading) or the
 * text changes. The whole text stays in the DOM for screen readers either way.
 */
function EvidenceDescription({ id, text, autoLinked }: { id: string; text: string; autoLinked: boolean }) {
  const ref = useRef<HTMLParagraphElement>(null);
  const [overflows, setOverflows] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const trimmed = text.trim();

  useLayoutEffect(() => {
    const el = ref.current;
    if (autoLinked || !el) return undefined;
    let active = true;
    const measure = () => {
      if (active) setOverflows(descriptionOverflows(el));
    };
    measure();
    // A web font arriving after the first measure can change line wrapping without changing a
    // clamped box's size, which a ResizeObserver would not report.
    document.fonts?.ready.then(measure).catch(() => undefined);
    if (typeof ResizeObserver !== 'undefined') {
      const observer = new ResizeObserver(measure);
      observer.observe(el);
      return () => {
        active = false;
        observer.disconnect();
      };
    }
    window.addEventListener('resize', measure);
    return () => {
      active = false;
      window.removeEventListener('resize', measure);
    };
  }, [autoLinked, trimmed]);

  if (!trimmed) return null;

  const clampable = !autoLinked && overflows;
  const clamped = clampable && !expanded;

  return (
    <>
      <p
        ref={ref}
        id={id}
        data-testid="linked-evidence-description"
        className={`mt-0.5 text-xs text-slate-400 whitespace-pre-wrap break-words${clamped ? ' line-clamp-3' : ''}`}
      >
        {trimmed}
      </p>
      {clampable && (
        <button
          type="button"
          className="mt-0.5 text-xs text-fd-blue hover:underline"
          aria-expanded={expanded}
          aria-controls={id}
          onClick={() => setExpanded(value => !value)}
        >
          {expanded ? 'Show less' : 'Show more'}
        </button>
      )}
    </>
  );
}

function HealthDot({ status }: { status: string }) {
  const colors: Record<string, string> = {
    healthy: 'bg-fd-green',
    warning: 'bg-fd-amber',
    critical: 'bg-fd-red',
    no_data: 'bg-fd-line-bright',
    // Hollow: nothing was measured, which is not the same as nothing on file.
    not_computed: 'border-2 border-fd-red bg-transparent',
  };
  const titles: Record<string, string> = { not_computed: 'Not computed' };
  return (
    <span
      className={`inline-block w-2.5 h-2.5 rounded-full ${colors[status] || colors.no_data}`}
      title={titles[status] || status}
    />
  );
}
