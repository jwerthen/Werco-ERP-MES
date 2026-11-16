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
import { frequencyText } from '../../utils/maintenance';
import type { PmImportResponse, PmImportScheduleResult } from '../../types/maintenance';

interface Props {
  onClose: () => void;
  /** Re-read schedules / assets after a commit that changed something. */
  onCommitted: () => void;
}

type Step = 'upload' | 'preview' | 'done';

/** The import template the Import Center also serves (`maintenance-schedules`). */
const TEMPLATE_ENTITY = 'maintenance-schedules';

const TEMPLATE_COLUMNS = [
  'work_center_code',
  'asset_tag',
  'asset_name',
  'asset_type',
  'schedule_name',
  'task',
  'frequency',
  'frequency_days',
  'next_due_date',
  'estimated_duration_hours',
  'requires_shutdown',
  'priority',
];

const OUTCOME_CLASS: Record<string, string> = {
  create: 'bg-green-500/20 text-emerald-300',
  update: 'bg-blue-500/20 text-blue-300',
  unchanged: 'bg-slate-800/50 text-slate-400',
  skipped: 'bg-amber-500/20 text-amber-300',
  error: 'bg-red-500/20 text-red-300',
};

const CHANGE_LABELS: Record<string, string> = {
  checklist: 'Checklist',
  frequency: 'Frequency',
  frequency_days: 'Every N days',
  next_due_date: 'Next due',
  estimated_duration_hours: 'Est. hours',
  requires_shutdown: 'Requires shutdown',
  priority: 'Priority',
  is_active: 'Active',
};

export function formatPmChangeValue(field: string, value: unknown): string {
  if (value === null || value === undefined || value === '') return '—';
  if (Array.isArray(value)) return `${value.length} task${value.length === 1 ? '' : 's'}`;
  if (field === 'next_due_date') return formatCentralDate(String(value));
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  return String(value).replace(/_/g, ' ');
}

interface CommitRefusal {
  message: string;
  errors: Array<{ row: number; target?: string | null; task?: string | null; error: string }>;
}

const OutcomeBadge = ({ outcome }: { outcome: string }) => (
  <span
    className={`inline-flex rounded-sm px-2 py-0.5 text-xs font-medium ${OUTCOME_CLASS[outcome] ?? OUTCOME_CLASS.unchanged}`}
  >
    {outcome}
  </span>
);

/**
 * PM schedule import: upload → preview → commit.
 *
 * One row per task; rows sharing a target, frequency and schedule name become one schedule
 * whose checklist is their task list. The preview writes nothing; the commit re-sends the
 * same file with the preview's `source_hash`, is all-or-nothing, and an unchanged sheet
 * writes nothing. New asset tags (with an asset_name) are created on commit.
 */
export function PmScheduleImportWizard({ onClose, onCommitted }: Props) {
  const { showToast } = useToast();
  const [step, setStep] = useState<Step>('upload');
  const [file, setFile] = useState<File | null>(null);
  const [loading, setLoading] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState('');
  const [refusal, setRefusal] = useState<CommitRefusal | null>(null);
  const [preview, setPreview] = useState<PmImportResponse | null>(null);
  const [result, setResult] = useState<PmImportResponse | null>(null);

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
      setPreview(await api.previewPmScheduleImport(file));
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
      const committed = await api.commitPmScheduleImport(file, preview.source_hash);
      setResult(committed);
      setStep('done');
      const {
        schedules_to_create: created,
        schedules_to_update: updated,
        assets_to_create: assets,
      } = committed.summary;
      showToast(
        'success',
        `PM schedules imported: ${created} created, ${updated} updated${assets ? `, ${assets} asset(s) added` : ''}.`
      );
      onCommitted();
    } catch (err: any) {
      const detail = err?.response?.data?.detail;
      if (detail && typeof detail === 'object' && !Array.isArray(detail) && Array.isArray(detail.errors)) {
        setRefusal({ message: toDisplayString(detail.message), errors: detail.errors });
      } else {
        setError(toDisplayString(detail) || 'The import could not be committed.');
      }
    } finally {
      setLoading(false);
    }
  };

  const summary = preview?.summary;
  const writes = summary ? summary.schedules_to_create + summary.schedules_to_update + summary.assets_to_create : 0;
  const blocked = !summary || summary.error_count > 0 || writes === 0;
  const issueRows = useMemo(
    () => (preview?.rows ?? []).filter(row => row.error || row.warnings.length > 0 || row.outcome === 'skipped'),
    [preview]
  );

  const chip = (label: string, value: number, tone = 'text-white') => (
    <div className="border border-slate-700 bg-slate-800/50 rounded-sm px-3 py-2">
      <div className="text-xs text-slate-400">{label}</div>
      <div className={`text-lg font-semibold ${tone}`}>{value}</div>
    </div>
  );

  const scheduleChanges = (schedule: PmImportScheduleResult) => {
    const entries = Object.entries(schedule.changes ?? {});
    if (schedule.outcome !== 'update' || entries.length === 0) return null;
    return (
      <ul className="mt-1 space-y-0.5 text-xs">
        {entries.map(([field, change]) => (
          <li key={field}>
            <span className="text-slate-400">{CHANGE_LABELS[field] ?? field}:</span>{' '}
            <span className="text-slate-500 line-through">{formatPmChangeValue(field, change.old)}</span>{' '}
            <span aria-hidden="true">→</span>{' '}
            <span className="text-slate-100">{formatPmChangeValue(field, change.new)}</span>
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
      ariaLabel="Import PM schedules"
    >
      <div className="space-y-4">
        <div>
          <h2 className="text-lg font-semibold text-white">
            {step === 'upload'
              ? 'Import PM Schedules'
              : step === 'preview'
                ? 'Review PM schedules'
                : 'PM schedules imported'}
          </h2>
          {step === 'preview' && (
            <p className="text-sm text-slate-400">
              Dry run — nothing has been saved. Review the schedules, then commit.
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
                  {item.target ? ` (${item.target})` : ''}: {item.error}
                </li>
              ))}
            </ul>
          </div>
        )}

        {step === 'upload' && (
          <form id="pm-import-form" onSubmit={runPreview} className="space-y-3">
            <ul className="list-disc space-y-1 pl-5 text-sm text-slate-400">
              <li>One row per task. Each row targets a work_center_code OR an asset_tag — exactly one.</li>
              <li>
                Rows with the same target, frequency and schedule name become one schedule; its checklist is their tasks
                in order. An existing active schedule with that target and name is updated.
              </li>
              <li>
                “Yearly” reads as annual; “as necessary” rows are skipped. A new asset_tag with an asset_name is
                created.
              </li>
              <li>Any row error stops the whole import; re-running the same sheet changes nothing.</li>
            </ul>
            <div>
              <label className="label" htmlFor="pm-import-file">
                File (.xlsx or .csv)
              </label>
              <input
                id="pm-import-file"
                type="file"
                className="input"
                accept=".xlsx,.csv,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                onChange={e => setFile(e.target.files?.[0] ?? null)}
                required
              />
            </div>
            <div className="border border-slate-700 rounded-sm p-3">
              <div className="mb-2 text-xs uppercase tracking-wide text-slate-500">Columns</div>
              <div className="flex flex-wrap gap-1.5">
                {TEMPLATE_COLUMNS.map(column => (
                  <code
                    key={column}
                    className={`rounded-sm border px-2 py-0.5 text-xs ${
                      column === 'task' || column === 'frequency'
                        ? 'border-werco-navy-500/40 bg-werco-navy-500/20 text-blue-200'
                        : 'border-slate-700 bg-slate-800 text-slate-300'
                    }`}
                  >
                    {column}
                  </code>
                ))}
              </div>
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
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-8">
              {chip('Task rows', summary.task_rows)}
              {chip('Skipped', summary.skipped_rows, summary.skipped_rows ? 'text-amber-300' : 'text-slate-300')}
              {chip('Schedules to create', summary.schedules_to_create, 'text-emerald-300')}
              {chip('Schedules to update', summary.schedules_to_update, 'text-blue-300')}
              {chip('Unchanged', summary.schedules_unchanged, 'text-slate-300')}
              {chip('Assets to create', summary.assets_to_create, 'text-emerald-300')}
              {chip('Warnings', summary.warning_count, summary.warning_count ? 'text-amber-300' : 'text-slate-300')}
              {chip('Errors', summary.error_count, summary.error_count ? 'text-red-300' : 'text-slate-300')}
            </div>
            {summary.error_count > 0 ? (
              <p className="flex items-center gap-2 rounded-sm border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-200">
                <ExclamationTriangleIcon className="h-4 w-4" aria-hidden="true" />
                {summary.error_count} row{summary.error_count === 1 ? ' has an error' : 's have errors'}. Nothing can be
                committed until the file is fixed and previewed again.
              </p>
            ) : writes === 0 ? (
              <p className="flex items-center gap-2 text-sm text-emerald-300">
                <CheckCircleIcon className="h-4 w-4" aria-hidden="true" />
                Every schedule already matches this sheet — there is nothing to commit.
              </p>
            ) : null}

            {preview.assets_to_create.length > 0 && (
              <section aria-label="Assets to create" className="space-y-1">
                <h3 className="text-sm font-semibold text-slate-200">Will create equipment assets</h3>
                <ul className="text-sm text-slate-300">
                  {preview.assets_to_create.map(asset => (
                    <li key={asset.asset_tag}>
                      <span className="font-mono text-werco-navy-300">{asset.asset_tag}</span> · {asset.name}
                      {asset.asset_type ? ` (${asset.asset_type})` : ''}
                      <span className="text-xs text-slate-500"> — rows {asset.rows.join(', ')}</span>
                    </li>
                  ))}
                </ul>
              </section>
            )}

            <section aria-label="Schedules" className="space-y-1">
              <h3 className="text-sm font-semibold text-slate-200">Schedules</h3>
              <div className="max-h-[40vh] overflow-auto border border-slate-700">
                <table className="min-w-full text-sm">
                  <thead className="sticky top-0 bg-slate-800 text-left text-xs uppercase text-slate-400">
                    <tr>
                      <th className="px-3 py-2">Outcome</th>
                      <th className="px-3 py-2">Target</th>
                      <th className="px-3 py-2">Schedule</th>
                      <th className="px-3 py-2">Frequency</th>
                      <th className="px-3 py-2">Checklist</th>
                      <th className="px-3 py-2">Next Due</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-700">
                    {preview.schedules.map(schedule => (
                      <tr key={`${schedule.target_type}-${schedule.target_code}-${schedule.name}`}>
                        <td className="px-3 py-2 align-top">
                          <OutcomeBadge outcome={schedule.outcome} />
                        </td>
                        <td className="px-3 py-2 align-top">
                          <span className="font-mono text-xs text-werco-navy-300">{schedule.target_code}</span>
                          {schedule.target_name && <div className="text-xs text-slate-400">{schedule.target_name}</div>}
                          <div className="text-[10px] uppercase tracking-wide text-slate-500">
                            {schedule.target_type === 'asset' ? 'Asset' : 'Work center'}
                          </div>
                        </td>
                        <td className="px-3 py-2 align-top text-white">
                          {schedule.name}
                          {scheduleChanges(schedule)}
                        </td>
                        <td className="px-3 py-2 align-top text-slate-300">
                          {frequencyText(schedule.frequency, schedule.frequency_days)}
                        </td>
                        <td className="px-3 py-2 align-top text-xs text-slate-300">
                          <ol className="list-decimal pl-4">
                            {schedule.tasks.map(task => (
                              <li key={task}>{task}</li>
                            ))}
                          </ol>
                        </td>
                        <td className="px-3 py-2 align-top text-slate-300">
                          {schedule.next_due_date ? formatCentralDate(schedule.next_due_date) : '—'}
                        </td>
                      </tr>
                    ))}
                    {preview.schedules.length === 0 && (
                      <tr>
                        <td colSpan={6} className="px-3 py-6 text-center text-slate-500">
                          No schedules were found in this file.
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </section>

            {issueRows.length > 0 && (
              <section aria-label="Row issues" className="space-y-1">
                <h3 className="text-sm font-semibold text-slate-200">Rows with notes</h3>
                <div className="max-h-52 overflow-auto border border-slate-700">
                  <table className="min-w-full text-sm">
                    <thead className="sticky top-0 bg-slate-800 text-left text-xs uppercase text-slate-400">
                      <tr>
                        <th className="px-3 py-2">Row</th>
                        <th className="px-3 py-2">Target</th>
                        <th className="px-3 py-2">Task</th>
                        <th className="px-3 py-2">Outcome</th>
                        <th className="px-3 py-2">Message</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-700">
                      {issueRows.map(row => (
                        <tr key={row.row}>
                          <td className="px-3 py-2 text-slate-400">{row.row}</td>
                          <td className="px-3 py-2 font-mono text-xs">{row.target_code || '—'}</td>
                          <td className="px-3 py-2 text-slate-300">{row.task || '—'}</td>
                          <td className="px-3 py-2">
                            <OutcomeBadge outcome={row.outcome} />
                          </td>
                          <td className="px-3 py-2 text-xs">
                            {row.error && <p className="text-red-300">{row.error}</p>}
                            {row.warnings.map(warning => (
                              <p key={warning} className="text-amber-300">
                                {warning}
                              </p>
                            ))}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </section>
            )}
          </>
        )}

        {step === 'done' && result && (
          <div className="space-y-2 text-sm">
            <p className="flex items-center gap-2 text-emerald-300">
              <CheckCircleIcon className="h-5 w-5" aria-hidden="true" />
              {result.summary.schedules_to_create} schedule(s) created, {result.summary.schedules_to_update} updated,{' '}
              {result.summary.assets_to_create} asset(s) added.
            </p>
            <p className="text-slate-400">Each change has its own audit row, plus one summary row for the import.</p>
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
                Commit import
              </LoadingButton>
            </>
          )}
          {step === 'done' && <Button onClick={onClose}>Done</Button>}
        </div>
      </div>
    </Modal>
  );
}

export default PmScheduleImportWizard;
