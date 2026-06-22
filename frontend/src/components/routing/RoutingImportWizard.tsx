import React, { useState } from 'react';
import { Link } from 'react-router-dom';
import api from '../../services/api';
import { RoutingImportResponse } from '../../types/engineering';
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

// Server template endpoint for the registered "routings" entity. Surfaced as a
// download link so users start from the styled XLSX with the right headers.
const TEMPLATE_ENTITY = 'routings';

// Columns the importer reads. Shown as a hint so users can hand-build a CSV
// without downloading the template. Required columns are flagged separately.
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
const REQUIRED_COLUMNS = ['part_number', 'sequence', 'operation_name', 'work_center_code'];

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
      const formData = new FormData();
      formData.append('file', file);
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
    setErrorMessage(null);
  };

  // Nothing creatable → commit makes no sense.
  const commitBlocked = (preview?.routings_created ?? 0) === 0;

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
          <tbody className="bg-[#151b28] divide-y divide-slate-700">
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
    step === 'upload' ? 'Import Routings' : step === 'preview' ? 'Review Routing Import' : 'Routings Imported';

  return (
    <Modal open onClose={onClose} size={step === 'upload' ? 'lg' : '4xl'} padded={false} scroll={false}>
      <div className="flex flex-col max-h-[90vh]">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-slate-700">
          <div>
            <h3 className="text-lg font-semibold text-white">{heading}</h3>
            {step === 'preview' && (
              <p className="text-sm text-slate-400">Dry run — nothing has been written yet.</p>
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
                  already has one).
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
                The part and every work center referenced must already exist. Rows are grouped into one routing per
                part + revision.
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
                  Required: {REQUIRED_COLUMNS.join(', ')}.
                </p>
              </div>
            </form>
          )}

          {step === 'preview' && preview && (
            <>
              {renderChips(preview)}
              {renderResultsTable(preview)}
              {renderErrorList(preview)}
              {preview.results.length === 0 && preview.errors.length === 0 && (
                <p className="text-sm text-slate-400 text-center py-6">
                  No routings were detected in this file. Check the columns and try again.
                </p>
              )}
            </>
          )}

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
              <button type="button" onClick={resetToUpload} className="btn-secondary">
                Back
              </button>
              <button
                type="button"
                onClick={handleCommit}
                className="btn-primary flex items-center"
                disabled={loading || commitBlocked}
                title={commitBlocked ? 'Nothing would be created — fix the rows above and preview again' : undefined}
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
