import React, { useMemo, useState } from 'react';
import {
  ArrowDownTrayIcon,
  ArrowUpTrayIcon,
  CheckCircleIcon,
  DocumentMagnifyingGlassIcon,
  ExclamationTriangleIcon,
} from '@heroicons/react/24/outline';
import api from '../../services/api';
import { Modal } from '../ui/Modal';
import { Button } from '../ui/Button';
import { LoadingButton } from '../ui/LoadingButton';
import { useToast } from '../ui/Toast';
import { saveImportDownload } from '../imports/RecoverableImport';
import { formatCentralDate } from '../../utils/centralTime';
import { toDisplayString } from '../../utils/apiError';
import type { AslImportResponse, AslImportRow } from '../../types/supplierApproval';
import { aslStatusLabel, basisLabels, scopeLabels } from '../../utils/supplierApproval';

interface Props {
  onClose: () => void;
  /** Re-read the ASL after a commit that changed something. */
  onCommitted: () => void;
}

type Step = 'upload' | 'preview' | 'done';

/** The import template the Import Center also serves (`approved-supplier-list`). */
const TEMPLATE_ENTITY = 'approved-supplier-list';

const FIELD_LABELS: Record<string, string> = {
  approval_status: 'Status',
  approved_scopes: 'Scope',
  approval_bases: 'Basis',
  approval_date: 'Approved',
  reevaluation_due: 'Re-eval due',
  last_performance_evaluation: 'Last perf eval',
  approval_notes: 'Notes',
  is_approved: 'Approved flag',
};

const DATE_FIELDS = new Set(['approval_date', 'reevaluation_due', 'last_performance_evaluation']);

/** Render one side of a field change in the same words the ASL screen uses. */
export function formatAslChangeValue(field: string, value: unknown): string {
  if (value === null || value === undefined || value === '' || (Array.isArray(value) && value.length === 0)) {
    return '—';
  }
  if (field === 'approval_status') return aslStatusLabel(String(value));
  if (field === 'approved_scopes') return scopeLabels(value as string[]);
  if (field === 'approval_bases') return basisLabels(value as string[]);
  if (DATE_FIELDS.has(field)) return formatCentralDate(String(value));
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  return String(value);
}

interface CommitRefusal {
  message: string;
  errors: Array<{ row: number; supplier_name?: string; error: string }>;
}

function refusalFrom(err: any): CommitRefusal | null {
  const detail = err?.response?.data?.detail;
  if (detail && typeof detail === 'object' && !Array.isArray(detail) && Array.isArray(detail.errors)) {
    return { message: toDisplayString(detail.message), errors: detail.errors };
  }
  return null;
}

const OUTCOME_CLASS: Record<string, string> = {
  update: 'bg-blue-500/20 text-blue-300',
  unchanged: 'bg-slate-800/50 text-slate-400',
  error: 'bg-red-500/20 text-red-300',
};

/**
 * Approved Supplier List import: upload → preview diff → commit.
 *
 * UPDATE-ONLY onto existing vendors (it never creates one). The preview writes nothing
 * and returns a `source_hash`; the commit sends the same file plus that hash, is
 * all-or-nothing (any row error refuses it), and re-running an unchanged sheet writes
 * nothing. Server-gated end to end, so nothing here is optimistic.
 */
export function AslImportWizard({ onClose, onCommitted }: Props) {
  const { showToast } = useToast();
  const [step, setStep] = useState<Step>('upload');
  const [file, setFile] = useState<File | null>(null);
  const [loading, setLoading] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState('');
  const [refusal, setRefusal] = useState<CommitRefusal | null>(null);
  const [preview, setPreview] = useState<AslImportResponse | null>(null);
  const [result, setResult] = useState<AslImportResponse | null>(null);
  const [showUnchanged, setShowUnchanged] = useState(false);

  const downloadTemplate = async () => {
    setDownloading(true);
    setError('');
    try {
      const { blob, filename } = await api.downloadImportTemplate(TEMPLATE_ENTITY);
      saveImportDownload(blob, filename);
    } catch {
      setError('Template download failed. Try again.');
    } finally {
      setDownloading(false);
    }
  };

  const runPreview = async (event?: React.FormEvent) => {
    event?.preventDefault();
    if (!file || loading) return;
    setLoading(true);
    setError('');
    setRefusal(null);
    try {
      setPreview(await api.previewVendorAslImport(file));
      setStep('preview');
    } catch (err: any) {
      setError(toDisplayString(err?.response?.data?.detail) || 'The file could not be previewed.');
    } finally {
      setLoading(false);
    }
  };

  const commit = async () => {
    if (!file || !preview || loading) return;
    setLoading(true);
    setError('');
    setRefusal(null);
    try {
      const committed = await api.commitVendorAslImport(file, preview.source_hash);
      setResult(committed);
      setStep('done');
      showToast(
        'success',
        `Approved Supplier List updated: ${committed.summary.update_count} vendor${
          committed.summary.update_count === 1 ? '' : 's'
        } changed.`
      );
      onCommitted();
    } catch (err: any) {
      const refused = refusalFrom(err);
      if (refused) setRefusal(refused);
      else setError(toDisplayString(err?.response?.data?.detail) || 'The import could not be committed.');
    } finally {
      setLoading(false);
    }
  };

  const visibleRows = useMemo(
    () => (preview?.rows ?? []).filter(row => showUnchanged || row.outcome !== 'unchanged' || row.warnings.length > 0),
    [preview, showUnchanged]
  );
  const summary = preview?.summary;
  const blocked = !summary || summary.error_count > 0 || summary.update_count === 0;

  const chip = (label: string, value: number, tone = 'text-white') => (
    <div className="border border-slate-700 bg-slate-800/50 rounded-sm px-3 py-2">
      <div className="text-xs text-slate-400">{label}</div>
      <div className={`text-lg font-semibold ${tone}`}>{value}</div>
    </div>
  );

  const changeList = (row: AslImportRow) => {
    const entries = Object.entries(row.changes ?? {});
    if (entries.length === 0) return <span className="text-slate-500">No change</span>;
    return (
      <ul className="space-y-0.5">
        {entries.map(([field, change]) => (
          <li key={field}>
            <span className="text-slate-400">{FIELD_LABELS[field] ?? field}:</span>{' '}
            <span className="text-slate-500 line-through">{formatAslChangeValue(field, change.old)}</span>{' '}
            <span aria-hidden="true">→</span>{' '}
            <span className="text-slate-100">{formatAslChangeValue(field, change.new)}</span>
          </li>
        ))}
      </ul>
    );
  };

  return (
    <Modal
      open
      onClose={loading ? () => undefined : onClose}
      size={step === 'upload' ? 'lg' : '6xl'}
      closeOnBackdrop={false}
      ariaLabel="Import Approved Supplier List"
    >
      <div className="space-y-4">
        <div>
          <h2 className="text-lg font-semibold text-white">
            {step === 'upload'
              ? 'Import Approved Supplier List'
              : step === 'preview'
                ? 'Review ASL changes'
                : 'Approved Supplier List imported'}
          </h2>
          {step === 'preview' && (
            <p className="text-sm text-slate-400">
              Dry run — nothing has been saved. Review every change, then commit.
            </p>
          )}
        </div>

        {error && (
          <p role="alert" className="rounded-sm border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-200">
            {error}
          </p>
        )}
        {refusal && (
          <div
            role="alert"
            className="rounded-sm border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-200"
          >
            <p className="font-medium">{refusal.message}</p>
            <ul className="mt-1 list-disc pl-5">
              {refusal.errors.map(item => (
                <li key={`${item.row}-${item.error}`}>
                  Row {item.row}
                  {item.supplier_name ? ` (${item.supplier_name})` : ''}: {item.error}
                </li>
              ))}
            </ul>
          </div>
        )}

        {step === 'upload' && (
          <form id="asl-import-form" onSubmit={runPreview} className="space-y-3">
            <p className="text-sm text-slate-300">
              Upload the QL 7.4 workbook as it is (the title block above the header row is skipped), the
              approved-supplier-list template, or a CSV.
            </p>
            <ul className="list-disc space-y-1 pl-5 text-sm text-slate-400">
              <li>
                Update-only: each row must match one existing vendor — by vendor_code if filled, else by exact name.
              </li>
              <li>No vendor is ever created; an unmatched or ambiguous name is a row error.</li>
              <li>A blank date never clears one already recorded. Any row error stops the whole import.</li>
            </ul>
            <div>
              <label className="label" htmlFor="asl-import-file">
                File (.xlsx or .csv)
              </label>
              <input
                id="asl-import-file"
                type="file"
                className="input"
                accept=".xlsx,.csv,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                onChange={e => setFile(e.target.files?.[0] ?? null)}
                required
              />
            </div>
            <Button
              variant="secondary"
              onClick={downloadTemplate}
              disabled={downloading}
              className="inline-flex items-center"
            >
              <ArrowDownTrayIcon className="mr-1 h-4 w-4" aria-hidden="true" />
              {downloading ? 'Downloading…' : 'Download template (.xlsx)'}
            </Button>
          </form>
        )}

        {step === 'preview' && preview && summary && (
          <>
            {(preview.document.form_number || preview.document.revision) && (
              <p className="text-xs text-slate-400">
                {preview.document.form_number ? `Form ${preview.document.form_number}` : ''}
                {preview.document.revision ? ` · Rev ${preview.document.revision}` : ''}
                {preview.document.rev_date ? ` · ${preview.document.rev_date}` : ''}
                {preview.filename ? ` · ${preview.filename}` : ''}
              </p>
            )}
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-6">
              {chip('Rows', summary.total_rows)}
              {chip('Vendors to update', summary.update_count, 'text-blue-300')}
              {chip('Unchanged', summary.unchanged_count, 'text-slate-300')}
              {chip('Not on the ASL', summary.not_on_asl_count, 'text-slate-300')}
              {chip('Warnings', summary.warning_count, summary.warning_count ? 'text-amber-300' : 'text-slate-300')}
              {chip('Errors', summary.error_count, summary.error_count ? 'text-red-300' : 'text-slate-300')}
            </div>
            {summary.error_count > 0 ? (
              <p className="flex items-center gap-2 rounded-sm border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-200">
                <ExclamationTriangleIcon className="h-4 w-4" aria-hidden="true" />
                {summary.error_count} row{summary.error_count === 1 ? ' has an error' : 's have errors'}. Nothing can be
                committed until the file is fixed and previewed again.
              </p>
            ) : summary.update_count === 0 ? (
              <p className="flex items-center gap-2 text-sm text-emerald-300">
                <CheckCircleIcon className="h-4 w-4" aria-hidden="true" />
                Every vendor already matches this sheet — there is nothing to commit.
              </p>
            ) : null}
            <label className="flex items-center gap-2 text-sm text-slate-300">
              <input type="checkbox" checked={showUnchanged} onChange={e => setShowUnchanged(e.target.checked)} />
              Show unchanged rows ({summary.unchanged_count})
            </label>
            <div className="max-h-[50vh] overflow-auto border border-slate-700">
              <table className="min-w-full text-sm">
                <thead className="sticky top-0 bg-slate-800 text-left text-xs uppercase text-slate-400">
                  <tr>
                    <th className="px-3 py-2">Row</th>
                    <th className="px-3 py-2">Supplier (sheet)</th>
                    <th className="px-3 py-2">Vendor</th>
                    <th className="px-3 py-2">Outcome</th>
                    <th className="px-3 py-2">Changes</th>
                    <th className="px-3 py-2">Notes</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-700">
                  {visibleRows.map(row => (
                    <tr key={row.row} className={row.outcome === 'error' ? 'bg-red-500/5' : undefined}>
                      <td className="px-3 py-2 align-top text-slate-400">{row.row}</td>
                      <td className="px-3 py-2 align-top text-white">{row.supplier_name}</td>
                      <td className="px-3 py-2 align-top">
                        {row.vendor_id ? (
                          <>
                            <span className="font-mono text-xs text-werco-navy-300">{row.vendor_code}</span>{' '}
                            {row.vendor_name}
                            {row.match_by && <div className="text-xs text-slate-500">matched by {row.match_by}</div>}
                          </>
                        ) : (
                          <span className="text-slate-500">—</span>
                        )}
                      </td>
                      <td className="px-3 py-2 align-top">
                        <span
                          className={`inline-flex rounded-sm px-2 py-0.5 text-xs font-medium ${OUTCOME_CLASS[row.outcome] ?? OUTCOME_CLASS.unchanged}`}
                        >
                          {row.outcome}
                        </span>
                      </td>
                      <td className="px-3 py-2 align-top text-xs">{changeList(row)}</td>
                      <td className="px-3 py-2 align-top text-xs">
                        {row.error && <p className="text-red-300">{row.error}</p>}
                        {row.warnings.map(warning => (
                          <p key={warning} className="text-amber-300">
                            {warning}
                          </p>
                        ))}
                      </td>
                    </tr>
                  ))}
                  {visibleRows.length === 0 && (
                    <tr>
                      <td colSpan={6} className="px-3 py-6 text-center text-slate-500">
                        No rows to show.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </>
        )}

        {step === 'done' && result && (
          <div className="space-y-2 text-sm">
            <p className="flex items-center gap-2 text-emerald-300">
              <CheckCircleIcon className="h-5 w-5" aria-hidden="true" />
              {result.summary.update_count} vendor{result.summary.update_count === 1 ? '' : 's'} updated;{' '}
              {result.summary.unchanged_count} already matched the sheet.
            </p>
            <p className="text-slate-400">
              Each changed vendor has its own audit row (with the file name and row), plus one summary row for the
              import.
            </p>
          </div>
        )}

        <div className="flex justify-end gap-2 border-t border-slate-700 pt-3">
          {step === 'upload' && (
            <>
              <Button variant="secondary" onClick={onClose}>
                Cancel
              </Button>
              <LoadingButton
                type="button"
                onClick={() => void runPreview()}
                loading={loading}
                loadingText="Analyzing…"
                disabled={!file}
                className="inline-flex items-center"
              >
                <DocumentMagnifyingGlassIcon className="mr-1 h-4 w-4" aria-hidden="true" />
                Preview (dry run)
              </LoadingButton>
            </>
          )}
          {step === 'preview' && (
            <>
              <Button
                variant="secondary"
                disabled={loading}
                onClick={() => {
                  setStep('upload');
                  setPreview(null);
                  setRefusal(null);
                }}
              >
                Back
              </Button>
              <LoadingButton
                onClick={commit}
                loading={loading}
                loadingText="Importing…"
                disabled={blocked}
                className="inline-flex items-center"
              >
                <ArrowUpTrayIcon className="mr-1 h-4 w-4" aria-hidden="true" />
                Commit {summary?.update_count ?? 0} vendor update{summary?.update_count === 1 ? '' : 's'}
              </LoadingButton>
            </>
          )}
          {step === 'done' && <Button onClick={onClose}>Done</Button>}
        </div>
      </div>
    </Modal>
  );
}

export default AslImportWizard;
