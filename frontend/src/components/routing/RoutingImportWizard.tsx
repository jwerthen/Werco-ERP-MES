import React, { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import api from '../../services/api';
import { RoutingImportResponse, RoutingImportResult } from '../../types/engineering';
import { WorkCenter } from '../../types';
import { Modal } from '../ui/Modal';
import { useToast } from '../ui/Toast';
import {
  ArrowDownTrayIcon,
  ArrowUpTrayIcon,
  CheckCircleIcon,
  DocumentMagnifyingGlassIcon,
  ExclamationTriangleIcon,
  XMarkIcon,
} from '@heroicons/react/24/outline';

interface Props {
  onComplete: () => Promise<void>;
  onClose: () => void;
}

type Step = 'upload' | 'preview' | 'done';

/**
 * Per-operation work-center selection keyed by the operation's source row number.
 * Mirrors the `assignments` map the commit endpoint expects (`{ "2": 5, "3": 7 }`),
 * built up from the preview's `operations[]` and edited via the dropdowns. A `null`
 * value means "not yet assigned" — commit stays gated until none remain.
 */
type Assignments = Record<number, number | null>;

// Server template endpoint for the registered "routings" entity. Surfaced as a
// download link so users start from the styled XLSX with the right headers.
const TEMPLATE_ENTITY = 'routings';

// Columns the importer reads. work_center_code is now optional — the wizard lets
// users assign work centers per operation in the preview step after upload.
const CSV_COLUMNS = [
  'part_number',
  'routing_revision',
  'routing_description',
  'sequence',
  'operation_name',
  'work_center_code',
  'setup_hours',
  'run_hours_per_unit',
  'description',
  'is_inspection_point',
  'is_outside_operation',
];
const REQUIRED_COLUMNS = ['part_number', 'sequence', 'operation_name'];

function extractError(err: any, fallback: string): string {
  if (err?.response?.status === 403) {
    return "You don't have permission to import routings.";
  }
  const detail = err?.response?.data?.detail;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail) && detail[0]?.msg) return detail[0].msg;
  return err?.message || fallback;
}

function formatHours(hours: number): string {
  if (!hours) return '0';
  if (hours < 1) return `${Math.round(hours * 60)} min`;
  return `${hours.toFixed(2)} hr`;
}

/** Seed the assignment map from a preview result: every operation row → its file
 *  work_center_id (or null when the file left it blank / unresolved). */
function seedAssignments(preview: RoutingImportResponse): Assignments {
  const map: Assignments = {};
  for (const routing of preview.results) {
    for (const op of routing.operations) {
      map[op.row] = op.work_center_id ?? null;
    }
  }
  return map;
}

export function RoutingImportWizard({ onComplete, onClose }: Props) {
  const { showToast } = useToast();

  const [step, setStep] = useState<Step>('upload');
  const [file, setFile] = useState<File | null>(null);
  const [loading, setLoading] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  // Dry-run result (step "preview") and committed result (step "done").
  const [preview, setPreview] = useState<RoutingImportResponse | null>(null);
  const [committed, setCommitted] = useState<RoutingImportResponse | null>(null);

  // Active work centers for the dropdowns + the live per-row selection state.
  const [workCenters, setWorkCenters] = useState<WorkCenter[]>([]);
  const [assignments, setAssignments] = useState<Assignments>({});

  // Load the company's active work centers once for the assignment dropdowns.
  useEffect(() => {
    let cancelled = false;
    api
      .getWorkCenters(true)
      .then((centers) => {
        if (!cancelled) setWorkCenters(centers);
      })
      .catch(() => {
        /* Non-fatal — dropdowns just render empty; commit stays gated. */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // ── Actions ──────────────────────────────────────────────────────────

  const handleDownloadTemplate = async () => {
    setErrorMessage(null);
    setDownloading(true);
    try {
      const { blob, filename } = await api.downloadImportTemplate(TEMPLATE_ENTITY);
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = filename;
      link.click();
      URL.revokeObjectURL(url);
    } catch (err: any) {
      setErrorMessage(extractError(err, 'Failed to download template'));
    } finally {
      setDownloading(false);
    }
  };

  const handlePreview = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!file) return;
    setLoading(true);
    setErrorMessage(null);
    try {
      const formData = new FormData();
      formData.append('file', file);
      const result = await api.previewRoutingImport(formData);
      setPreview(result);
      setAssignments(seedAssignments(result));
      setStep('preview');
    } catch (err: any) {
      setErrorMessage(extractError(err, 'Failed to generate preview'));
    } finally {
      setLoading(false);
    }
  };

  const handleCommit = async () => {
    if (!file) return;
    setLoading(true);
    setErrorMessage(null);
    try {
      // Build the row → work_center_id map from the live selections. Only rows
      // with a resolved work center are sent; the gate guarantees all are set.
      const map: Record<string, number> = {};
      for (const [row, wcId] of Object.entries(assignments)) {
        if (wcId != null) map[row] = wcId;
      }
      const formData = new FormData();
      formData.append('file', file);
      formData.append('assignments', JSON.stringify(map));
      const result = await api.commitRoutingImport(formData);
      setCommitted(result);
      setStep('done');
      if (result.errors.length > 0) {
        showToast('info', `Imported ${result.routings_created} routing(s); ${result.errors.length} row(s) skipped`);
      } else {
        showToast('success', `Imported ${result.routings_created} routing(s)`);
      }
      await onComplete();
    } catch (err: any) {
      setErrorMessage(extractError(err, 'Failed to import routings'));
    } finally {
      setLoading(false);
    }
  };

  const resetToUpload = () => {
    setStep('upload');
    setPreview(null);
    setCommitted(null);
    setAssignments({});
    setErrorMessage(null);
  };

  // ── Assignment helpers ───────────────────────────────────────────────

  const assignRow = (row: number, value: string) => {
    setAssignments((prev) => ({ ...prev, [row]: value ? Number(value) : null }));
  };

  // Apply one work center to every operation in a routing (or only the ones still
  // unassigned). Empty selection is a no-op.
  const applyToRouting = (routing: RoutingImportResult, value: string, onlyUnassigned: boolean) => {
    if (!value) return;
    const wcId = Number(value);
    setAssignments((prev) => {
      const next = { ...prev };
      for (const op of routing.operations) {
        if (!onlyUnassigned || next[op.row] == null) next[op.row] = wcId;
      }
      return next;
    });
  };

  // Live count of operations still missing a work center, sourced from the
  // selection state (not the preview's initial operations_needing_work_center).
  const unassignedCount = useMemo(() => {
    if (!preview) return 0;
    let count = 0;
    for (const routing of preview.results) {
      for (const op of routing.operations) {
        if (assignments[op.row] == null) count += 1;
      }
    }
    return count;
  }, [preview, assignments]);

  const totalAssignable = useMemo(() => {
    if (!preview) return 0;
    return preview.results.reduce((sum, r) => sum + r.operations.length, 0);
  }, [preview]);

  // Commit is blocked when nothing is creatable or any operation is unassigned.
  const commitBlocked = (preview?.routings_created ?? 0) === 0 || unassignedCount > 0;

  // ── Sub-renders ──────────────────────────────────────────────────────

  const renderChips = (result: RoutingImportResponse) => (
    <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
      <div className="bg-slate-800/50 border border-slate-700 rounded-lg p-3">
        <div className="text-xs text-slate-400">Parts detected</div>
        <div className="text-lg font-semibold text-white">{result.parts_detected}</div>
      </div>
      <div className="bg-slate-800/50 border border-slate-700 rounded-lg p-3">
        <div className="text-xs text-slate-400">{result.dry_run ? 'Routings to create' : 'Routings created'}</div>
        <div className="text-lg font-semibold text-emerald-300">{result.routings_created}</div>
      </div>
      <div className="bg-slate-800/50 border border-slate-700 rounded-lg p-3">
        <div className="text-xs text-slate-400">Total operations</div>
        <div className="text-lg font-semibold text-white">{result.total_operations}</div>
      </div>
      <div className="bg-slate-800/50 border border-slate-700 rounded-lg p-3">
        <div className="text-xs text-slate-400">Skipped rows</div>
        <div className={`text-lg font-semibold ${result.skipped_count > 0 ? 'text-amber-300' : 'text-slate-300'}`}>
          {result.skipped_count}
        </div>
      </div>
    </div>
  );

  // The work-center <select> shared by per-op rows and the apply-to-all control.
  const workCenterSelect = (value: number | null, onChange: (v: string) => void, opts?: { flag?: boolean; placeholder?: string; label?: string }) => (
    <select
      aria-label={opts?.label}
      value={value != null ? String(value) : ''}
      onChange={(e) => onChange(e.target.value)}
      className={`w-full bg-[#0f1420] text-sm text-slate-100 rounded px-2 py-1.5 border focus:outline-none focus:ring-1 ${
        opts?.flag
          ? 'border-accent-500/70 ring-accent-500/40 focus:ring-accent-500'
          : 'border-slate-600 focus:ring-werco-navy-500'
      }`}
    >
      <option value="">{opts?.placeholder ?? 'Select work center'}</option>
      {workCenters.map((wc) => (
        <option key={wc.id} value={wc.id}>
          {wc.code} — {wc.name}
        </option>
      ))}
    </select>
  );

  // The assignment table: one card per routing, one row per operation with its
  // own work-center dropdown plus an apply-to-all control.
  const renderAssignmentStep = (result: RoutingImportResponse) => (
    <>
      {renderChips(result)}

      {unassignedCount > 0 ? (
        <div
          className="flex items-center gap-2 text-sm text-accent-200 bg-accent-500/10 border border-accent-500/40 rounded-lg px-3 py-2"
          data-testid="needs-assignment-banner"
        >
          <ExclamationTriangleIcon className="h-4 w-4 shrink-0" />
          <span>
            {unassignedCount} operation{unassignedCount !== 1 ? 's' : ''} still need
            {unassignedCount === 1 ? 's' : ''} a work center.
          </span>
        </div>
      ) : (
        totalAssignable > 0 && (
          <div className="flex items-center gap-2 text-sm text-emerald-300 bg-emerald-500/10 border border-emerald-500/30 rounded-lg px-3 py-2">
            <CheckCircleIcon className="h-4 w-4 shrink-0" />
            <span>All {totalAssignable} operations have a work center. Ready to commit.</span>
          </div>
        )
      )}

      <div className="space-y-4">
        {result.results.map((routing, rIdx) => (
          <RoutingAssignmentCard
            key={`${routing.part_number}-${routing.routing_revision}-${rIdx}`}
            routing={routing}
            assignments={assignments}
            onAssignRow={assignRow}
            onApply={applyToRouting}
            renderSelect={workCenterSelect}
          />
        ))}
      </div>

      {renderErrorList(result)}
      {result.results.length === 0 && result.errors.length === 0 && (
        <p className="text-sm text-slate-400 text-center py-6">
          No routings were detected in this file. Check the columns and try again.
        </p>
      )}
    </>
  );

  const renderResultsTable = (result: RoutingImportResponse) =>
    result.results.length > 0 && (
      <div className="overflow-x-auto border border-slate-700 rounded-lg max-h-72 overflow-y-auto">
        <table className="min-w-full divide-y divide-slate-700 text-sm">
          <thead className="bg-slate-800 sticky top-0">
            <tr>
              <th className="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">Part #</th>
              <th className="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">Rev</th>
              <th className="px-3 py-2 text-right text-xs font-medium text-slate-400 uppercase">Ops</th>
              <th className="px-3 py-2 text-right text-xs font-medium text-slate-400 uppercase">Setup</th>
              <th className="px-3 py-2 text-right text-xs font-medium text-slate-400 uppercase">Run/Unit</th>
              <th className="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">Status</th>
            </tr>
          </thead>
          <tbody className="bg-fd-panel divide-y divide-slate-700">
            {result.results.map((row, idx) => (
              <tr key={`${row.part_number}-${row.routing_revision}-${idx}`}>
                <td className="px-3 py-2 font-medium text-white whitespace-nowrap">{row.part_number}</td>
                <td className="px-3 py-2 text-slate-300 whitespace-nowrap">{row.routing_revision}</td>
                <td className="px-3 py-2 text-right text-slate-300">{row.operation_count}</td>
                <td className="px-3 py-2 text-right text-slate-300 whitespace-nowrap">
                  {formatHours(row.total_setup_hours)}
                </td>
                <td className="px-3 py-2 text-right text-slate-300 whitespace-nowrap">
                  {formatHours(row.total_run_hours_per_unit)}
                </td>
                <td className="px-3 py-2">
                  <span className="inline-flex px-2 py-0.5 rounded text-xs font-medium bg-yellow-500/20 text-yellow-300">
                    {row.status}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );

  const renderErrorList = (result: RoutingImportResponse) =>
    result.errors.length > 0 && (
      <div className="space-y-2">
        <div className="flex items-center gap-2 text-sm text-red-300">
          <ExclamationTriangleIcon className="h-4 w-4" />
          <span>
            {result.errors.length} row{result.errors.length !== 1 ? 's' : ''} with problems
            {result.dry_run ? ' — their routings will be skipped on commit' : ' — skipped'}
          </span>
        </div>
        <div className="overflow-x-auto border border-red-500/30 rounded-lg max-h-52 overflow-y-auto">
          <table className="min-w-full text-sm">
            <thead className="bg-red-500/10 text-red-200 text-left sticky top-0">
              <tr>
                <th className="px-3 py-2 font-medium">Row</th>
                <th className="px-3 py-2 font-medium">Part #</th>
                <th className="px-3 py-2 font-medium">Problem</th>
              </tr>
            </thead>
            <tbody>
              {result.errors.map((error, idx) => (
                <tr key={`${error.row}-${idx}`} className="border-t border-slate-700 text-slate-300">
                  <td className="px-3 py-2 whitespace-nowrap">{error.row}</td>
                  <td className="px-3 py-2 whitespace-nowrap">{error.part_number || '-'}</td>
                  <td className="px-3 py-2">{error.reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    );

  // ── Render ───────────────────────────────────────────────────────────

  const heading =
    step === 'upload' ? 'Import Routings' : step === 'preview' ? 'Assign Work Centers' : 'Routings Imported';

  return (
    <Modal open onClose={onClose} size={step === 'upload' ? 'lg' : '4xl'} padded={false} scroll={false}>
      <div className="flex flex-col max-h-[90vh]">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-slate-700">
          <div>
            <h3 className="text-lg font-semibold text-white">{heading}</h3>
            {step === 'preview' && (
              <p className="text-sm text-slate-400">
                Dry run — assign a work center to every operation, then commit.
              </p>
            )}
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-200" aria-label="Close">
            <XMarkIcon className="h-5 w-5" />
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto px-6 py-4 space-y-4">
          {errorMessage && (
            <div className="bg-red-500/10 border border-red-500/40 rounded-lg p-3 text-sm text-red-200">
              {errorMessage}
            </div>
          )}

          {step === 'upload' && (
            <form onSubmit={handlePreview} id="routing-import-form" className="space-y-4">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <p className="text-sm text-slate-400">
                  Upload a CSV or XLSX of operations grouped by part. The import creates{' '}
                  <span className="text-slate-200 font-medium">draft</span> routings (a new revision if the part
                  already has one). You'll assign each operation's work center in the next step.
                </p>
                <button
                  type="button"
                  onClick={handleDownloadTemplate}
                  disabled={downloading}
                  className="btn-secondary flex items-center"
                >
                  <ArrowDownTrayIcon className="h-5 w-5 mr-2" />
                  {downloading ? 'Downloading...' : 'Download template'}
                </button>
              </div>

              <div className="bg-amber-500/10 border border-amber-500/30 rounded-lg p-3 text-xs text-amber-200">
                The part referenced must already exist. Rows are grouped into one routing per part + revision. Work
                centers are optional in the file — assign them per operation after upload.
              </div>

              <div>
                <label className="label" htmlFor="routing-import-file">
                  File (.csv or .xlsx)
                </label>
                <input
                  id="routing-import-file"
                  type="file"
                  accept=".csv,.xlsx,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                  aria-label="Routing import file"
                  onChange={(e) => setFile(e.target.files?.[0] || null)}
                  className="input"
                  required
                />
              </div>

              <div className="border border-slate-700 rounded-lg p-3">
                <div className="text-xs uppercase tracking-wide text-slate-500 mb-2">Columns</div>
                <div className="flex flex-wrap gap-1.5">
                  {CSV_COLUMNS.map((column) => (
                    <code
                      key={column}
                      className={`px-2 py-0.5 text-xs rounded border ${
                        REQUIRED_COLUMNS.includes(column)
                          ? 'bg-werco-navy-500/20 border-werco-navy-500/40 text-blue-200'
                          : 'bg-slate-800 border-slate-700 text-slate-300'
                      }`}
                    >
                      {column}
                    </code>
                  ))}
                </div>
                <p className="mt-2 text-xs text-slate-500">
                  Required: {REQUIRED_COLUMNS.join(', ')}. <span className="text-slate-400">work_center_code</span> is
                  optional — assign work centers in the next step.
                </p>
              </div>
            </form>
          )}

          {step === 'preview' && preview && renderAssignmentStep(preview)}

          {step === 'done' && committed && (
            <>
              <div className="flex items-center gap-2 text-emerald-300">
                <CheckCircleIcon className="h-5 w-5" />
                <span className="font-medium">
                  Created {committed.routings_created} routing{committed.routings_created !== 1 ? 's' : ''} (
                  {committed.total_operations} operation{committed.total_operations !== 1 ? 's' : ''})
                  {committed.created_ids.length > 0 ? `, ${committed.created_ids.length} new record(s)` : ''}.
                </span>
              </div>
              {renderChips(committed)}
              {renderResultsTable(committed)}
              {renderErrorList(committed)}
              <p className="text-sm text-slate-400">
                The new draft routing{committed.routings_created !== 1 ? 's are' : ' is'} ready for review on the{' '}
                <Link to="/routing" onClick={onClose} className="text-werco-navy-300 hover:text-blue-200 underline">
                  Routing page
                </Link>
                . Release each routing there once the operations are verified.
              </p>
            </>
          )}
        </div>

        {/* Footer */}
        <div className="flex justify-end gap-3 px-6 py-4 border-t border-slate-700 bg-slate-800/60">
          {step === 'upload' && (
            <>
              <button type="button" onClick={onClose} className="btn-secondary">
                Cancel
              </button>
              <button
                type="submit"
                form="routing-import-form"
                className="btn-primary flex items-center"
                disabled={loading || !file}
              >
                <DocumentMagnifyingGlassIcon className="h-5 w-5 mr-2" />
                {loading ? 'Analyzing...' : 'Preview (dry run)'}
              </button>
            </>
          )}
          {step === 'preview' && (
            <>
              {unassignedCount > 0 && (
                <span className="mr-auto self-center text-sm text-accent-300">
                  {unassignedCount} operation{unassignedCount !== 1 ? 's' : ''} still need
                  {unassignedCount === 1 ? 's' : ''} a work center
                </span>
              )}
              <button type="button" onClick={resetToUpload} className="btn-secondary">
                Back
              </button>
              <button
                type="button"
                onClick={handleCommit}
                className="btn-primary flex items-center"
                disabled={loading || commitBlocked}
                title={
                  (preview?.routings_created ?? 0) === 0
                    ? 'Nothing would be created — fix the rows above and preview again'
                    : unassignedCount > 0
                      ? 'Assign a work center to every operation first'
                      : undefined
                }
              >
                <ArrowUpTrayIcon className="h-5 w-5 mr-2" />
                {loading
                  ? 'Importing...'
                  : `Commit ${preview?.routings_created ?? 0} routing${(preview?.routings_created ?? 0) !== 1 ? 's' : ''}`}
              </button>
            </>
          )}
          {step === 'done' && (
            <button type="button" onClick={onClose} className="btn-primary">
              Done
            </button>
          )}
        </div>
      </div>
    </Modal>
  );
}

// ── Per-routing assignment card ─────────────────────────────────────────────

interface CardProps {
  routing: RoutingImportResult;
  assignments: Assignments;
  onAssignRow: (row: number, value: string) => void;
  onApply: (routing: RoutingImportResult, value: string, onlyUnassigned: boolean) => void;
  renderSelect: (
    value: number | null,
    onChange: (v: string) => void,
    opts?: { flag?: boolean; placeholder?: string; label?: string },
  ) => React.ReactNode;
}

function RoutingAssignmentCard({ routing, assignments, onAssignRow, onApply, renderSelect }: CardProps) {
  // Local "apply to all" picker value — applied, not bound to any operation row.
  const [applyValue, setApplyValue] = useState('');

  const routingUnassigned = routing.operations.filter((op) => assignments[op.row] == null).length;

  return (
    <div className="border border-slate-700 rounded-lg overflow-hidden">
      <div className="flex flex-wrap items-center justify-between gap-2 bg-slate-800/70 px-3 py-2 border-b border-slate-700">
        <div className="flex items-center gap-2 text-sm">
          <span className="font-medium text-white">{routing.part_number}</span>
          <span className="text-slate-400">Rev {routing.routing_revision}</span>
          <span className="inline-flex px-2 py-0.5 rounded text-xs font-medium bg-yellow-500/20 text-yellow-300">
            {routing.status}
          </span>
          {routingUnassigned > 0 && (
            <span className="text-xs text-accent-300">{routingUnassigned} unassigned</span>
          )}
        </div>
        {/* Apply-to-all convenience control. */}
        <div className="flex items-center gap-2">
          <span className="text-xs text-slate-400">Apply to all:</span>
          <div className="w-48">
            {renderSelect(applyValue ? Number(applyValue) : null, (v) => setApplyValue(v), {
              placeholder: 'Pick a work center',
              label: `Apply work center to all operations of ${routing.part_number}`,
            })}
          </div>
          <button
            type="button"
            className="btn-secondary text-xs py-1 px-2"
            disabled={!applyValue}
            onClick={() => onApply(routing, applyValue, false)}
          >
            All
          </button>
          <button
            type="button"
            className="btn-secondary text-xs py-1 px-2"
            disabled={!applyValue}
            title="Apply only to operations that don't have a work center yet"
            onClick={() => onApply(routing, applyValue, true)}
          >
            Unassigned
          </button>
        </div>
      </div>

      <div className="overflow-x-auto">
        <table className="min-w-full text-sm">
          <thead className="bg-slate-800/40 text-left text-xs text-slate-400 uppercase">
            <tr>
              <th className="px-3 py-2 font-medium">Seq</th>
              <th className="px-3 py-2 font-medium">Operation</th>
              <th className="px-3 py-2 font-medium text-right">Setup</th>
              <th className="px-3 py-2 font-medium text-right">Run/Unit</th>
              <th className="px-3 py-2 font-medium">Flags</th>
              <th className="px-3 py-2 font-medium w-72">Work center</th>
            </tr>
          </thead>
          <tbody className="bg-fd-panel divide-y divide-slate-700">
            {routing.operations.map((op) => {
              const selected = assignments[op.row] ?? null;
              const isUnassigned = selected == null;
              return (
                <tr key={op.row} className={isUnassigned ? 'bg-accent-500/5' : undefined}>
                  <td className="px-3 py-2 text-slate-300 whitespace-nowrap">{op.sequence}</td>
                  <td className="px-3 py-2 text-white whitespace-nowrap">{op.operation_name}</td>
                  <td className="px-3 py-2 text-right text-slate-300 whitespace-nowrap">
                    {formatHours(op.setup_hours)}
                  </td>
                  <td className="px-3 py-2 text-right text-slate-300 whitespace-nowrap">
                    {formatHours(op.run_hours_per_unit)}
                  </td>
                  <td className="px-3 py-2 whitespace-nowrap">
                    <div className="flex gap-1">
                      {op.is_inspection_point && (
                        <span className="inline-flex px-1.5 py-0.5 rounded text-[10px] font-medium bg-werco-navy-500/20 text-blue-200">
                          INSP
                        </span>
                      )}
                      {op.is_outside_operation && (
                        <span className="inline-flex px-1.5 py-0.5 rounded text-[10px] font-medium bg-purple-500/20 text-purple-200">
                          OUTSIDE
                        </span>
                      )}
                    </div>
                  </td>
                  <td className="px-3 py-2">
                    {renderSelect(selected, (v) => onAssignRow(op.row, v), {
                      flag: isUnassigned,
                      label: `Work center for operation ${op.operation_name} (row ${op.row})`,
                    })}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
