import React, { useEffect, useState } from 'react';
import { ExclamationTriangleIcon } from '@heroicons/react/24/outline';
import { Modal } from '../ui/Modal';
import {
  LaserNestImportRow,
  LaserNestPreviewRow,
  LaserNestExtractionConfidence,
  LaserNestConfidenceField,
  LaserNestFieldConfidence,
} from '../../types';
import api from '../../services/api';

interface LaserNestImportWizardProps {
  open: boolean;
  onClose: () => void;
  /**
   * Work order the import targets: an assembly WO (a laser child WO is created
   * under it) or a laser-cutting WO (nests land on it directly). Omit for
   * STANDALONE mode — the import hits the /standalone endpoints and creates a
   * fresh released laser-cutting work order with no parent and no part.
   */
  workOrderId?: number;
  /**
   * Optional work center to assign the generated laser operations to. Passed
   * straight through to the import call; the backend applies its default when
   * omitted.
   */
  workCenterId?: number | null;
  /**
   * Called after a successful import with the id of the laser WO the nests
   * landed on (the created child / standalone WO, or the target WO itself) so
   * the parent can navigate to it; otherwise the parent refreshes.
   */
  onImported: (childWorkOrderId?: number) => void;
}

type WizardStep = 'pick' | 'review';

/** Local, editable mirror of a preview row. Keeps `source_file` as the stable
 *  key the backend matches PDFs by; everything else the planner can correct.
 *  `source_pages` is carried verbatim so PDF imports can echo it back, and
 *  `edited` tracks which fields the planner has touched (clears the
 *  low-confidence highlight for that field). */
interface EditableRow {
  source_file: string;
  cnc_number: string;
  cnc_file_name: string | null;
  nest_name: string;
  planned_runs: string; // string while editing; coerced to int on import
  material: string;
  thickness: string;
  sheet_size: string;
  confidence: LaserNestExtractionConfidence | null;
  source_pages: number[] | null;
  field_confidence: LaserNestFieldConfidence | null;
  warning: string | null;
  edited: Partial<Record<LaserNestConfidenceField, boolean>>;
}

/** Preview metadata about the uploaded package itself (bare-PDF uploads). */
interface PackageMeta {
  source_page_count: number | null;
  skipped_pages: number[];
  segmentation_warning: string | null;
}

const CONFIDENCE_BADGE: Record<LaserNestExtractionConfidence, { label: string; className: string }> = {
  high: { label: 'High', className: 'border-fd-green/40 bg-fd-green/10 text-fd-green' },
  medium: { label: 'Med', className: 'border-fd-amber/40 bg-fd-amber/10 text-fd-amber' },
  low: { label: 'Low', className: 'border-fd-red/40 bg-fd-red/10 text-fd-red' },
};

const TH = 'px-3 py-2 text-left text-[11px] font-semibold uppercase tracking-wide text-fd-mute';
// Border/background split from the base so the low-confidence variant never
// fights the default colors on Tailwind specificity.
const CELL_INPUT_BASE =
  'w-full rounded-none border px-2 py-1 text-sm text-fd-ink focus:border-fd-blue focus:outline-none';
const CELL_INPUT = `${CELL_INPUT_BASE} border-fd-line bg-fd-sunken`;
const CELL_INPUT_VERIFY = `${CELL_INPUT_BASE} border-fd-amber bg-fd-amber/10`;

function toEditable(row: LaserNestPreviewRow): EditableRow {
  return {
    source_file: row.source_file,
    cnc_number: row.cnc_number ?? '',
    cnc_file_name: row.cnc_file_name ?? null,
    nest_name: row.nest_name ?? '',
    planned_runs: String(row.planned_runs ?? 1),
    material: row.material ?? '',
    thickness: row.thickness ?? '',
    sheet_size: row.sheet_size ?? '',
    confidence: row.confidence ?? null,
    source_pages: row.source_pages ?? null,
    field_confidence: row.field_confidence ?? null,
    warning: row.warning ?? null,
    edited: {},
  };
}

function fieldValue(row: EditableRow, field: LaserNestConfidenceField): string {
  switch (field) {
    case 'cnc_number':
      return row.cnc_number;
    case 'material':
      return row.material;
    case 'thickness':
      return row.thickness;
    case 'sheet_size':
      return row.sheet_size;
    case 'planned_runs':
      return row.planned_runs;
  }
}

/** A field needs the amber verify highlight when the extractor marked it low
 *  confidence, or when a PDF-upload row left it blank — until the planner
 *  edits it. */
function fieldNeedsVerify(row: EditableRow, field: LaserNestConfidenceField): boolean {
  if (row.edited[field]) return false;
  if (row.field_confidence?.[field] === 'low') return true;
  const isPdfRow = row.source_pages != null && row.source_pages.length > 0;
  return isPdfRow && fieldValue(row, field).trim() === '';
}

/** `[3]` → `p. 3`, `[3,4]` → `p. 3–4`, `[3,4,7]` → `p. 3–4, 7`. */
function formatPageRange(pages: number[]): string {
  const sorted = [...pages].sort((a, b) => a - b);
  const parts: string[] = [];
  let start = sorted[0];
  let prev = sorted[0];
  for (const page of sorted.slice(1)) {
    if (page === prev + 1) {
      prev = page;
      continue;
    }
    parts.push(start === prev ? String(start) : `${start}–${prev}`);
    start = page;
    prev = page;
  }
  parts.push(start === prev ? String(start) : `${start}–${prev}`);
  return `p. ${parts.join(', ')}`;
}

/**
 * Two-step wizard for importing a ZIP of laser-nest sheets — or a bare
 * single/multi-page nest-report PDF — onto an assembly WO.
 *
 *   1. Pick a ZIP or PDF (or, when the server supports it, a folder path).
 *   2. Preview runs AI extraction server-side and returns editable rows; the
 *      planner reviews/corrects them, removing any that shouldn't be imported.
 *      Bare-PDF uploads are segmented server-side into one row per nest, each
 *      carrying its `source_pages` plus per-field confidence.
 *   3. Import re-sends the SAME ZIP/PDF plus the confirmed rows — the backend
 *      matches each row to its PDF by `source_file` (re-splitting a bare PDF by
 *      the echoed `source_pages`) and persists the confirmed values without a
 *      second AI call.
 *
 * The same flow handles a ZIP of CNC *program* files: those preview rows carry
 * `cnc_file_name` instead of an AI-read `cnc_number`/`confidence`, and sending
 * them back unchanged preserves the legacy import behavior.
 *
 * With no `workOrderId` the wizard runs in STANDALONE mode: preview/import hit
 * the /work-orders/laser-nest-packages/standalone endpoints and the import
 * creates a fresh released laser-cutting WO (no parent, no part) sized to the
 * total planned sheet runs.
 */
export default function LaserNestImportWizard({
  open,
  onClose,
  workOrderId,
  workCenterId,
  onImported,
}: LaserNestImportWizardProps) {
  const [step, setStep] = useState<WizardStep>('pick');
  const [file, setFile] = useState<File | null>(null);
  const [sourcePath, setSourcePath] = useState('');
  const [fileInputKey, setFileInputKey] = useState(0);

  const [rows, setRows] = useState<EditableRow[]>([]);
  const [packageMeta, setPackageMeta] = useState<PackageMeta | null>(null);

  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  // Reset everything whenever the wizard (re)opens.
  useEffect(() => {
    if (!open) return;
    setStep('pick');
    setFile(null);
    setSourcePath('');
    setRows([]);
    setPackageMeta(null);
    setLoading(false);
    setError('');
    setFileInputKey((k) => k + 1);
  }, [open]);

  const hasInput = Boolean(file) || sourcePath.trim().length > 0;

  const handlePreview = async () => {
    if (!hasInput) {
      setError('Choose a ZIP package or a nest-report PDF (single or multi-page), or enter a folder path.');
      return;
    }
    setLoading(true);
    setError('');
    try {
      const input = { file, source_path: sourcePath.trim() || undefined };
      const result =
        workOrderId != null
          ? await api.previewLaserNestPackage(workOrderId, input)
          : await api.previewLaserNestPackageStandalone(input);
      setRows(result.nests.map(toEditable));
      setPackageMeta({
        source_page_count: result.source_page_count ?? null,
        skipped_pages: result.skipped_pages ?? [],
        segmentation_warning: result.segmentation_warning ?? null,
      });
      setStep('review');
    } catch (err: any) {
      setError(err?.response?.data?.detail || 'Failed to preview laser nest package.');
    } finally {
      setLoading(false);
    }
  };

  /** Edit one extracted field: applies the value and marks the field as
   *  planner-touched so its low-confidence highlight clears. */
  const updateField = (index: number, field: LaserNestConfidenceField, value: string) => {
    setRows((prev) =>
      prev.map((row, i) =>
        i === index ? { ...row, [field]: value, edited: { ...row.edited, [field]: true } } : row
      )
    );
  };

  const removeRow = (index: number) => {
    setRows((prev) => prev.filter((_, i) => i !== index));
  };

  const handleImport = async () => {
    if (rows.length === 0) {
      setError('Add at least one nest to import.');
      return;
    }
    // Each row needs a CNC number (the operator-facing program number) and a
    // whole-sheet run count >= 1; surface the first offender rather than letting
    // the backend reject the whole batch.
    for (const row of rows) {
      if (!row.cnc_number.trim()) {
        setError(`Enter a CNC number for ${row.source_file}.`);
        return;
      }
      const runs = Number(row.planned_runs);
      if (!Number.isInteger(runs) || runs < 1) {
        setError(`Runs for ${row.source_file} must be a whole number of at least 1.`);
        return;
      }
    }

    const confirmed: LaserNestImportRow[] = rows.map((row) => ({
      source_file: row.source_file,
      cnc_number: row.cnc_number.trim(),
      nest_name: row.nest_name.trim() || row.cnc_number.trim(),
      planned_runs: Number(row.planned_runs),
      material: row.material.trim() || null,
      thickness: row.thickness.trim() || null,
      sheet_size: row.sheet_size.trim() || null,
      // PDF uploads: echo the preview's page split back verbatim — the backend
      // re-splits the re-sent PDF by these pages and 400s on a mismatch.
      ...(row.source_pages != null ? { source_pages: row.source_pages } : {}),
    }));

    setLoading(true);
    setError('');
    try {
      const input = {
        file,
        source_path: sourcePath.trim() || undefined,
        work_center_id: workCenterId ?? undefined,
        rows: confirmed,
      };
      const result =
        workOrderId != null
          ? await api.importLaserNestPackage(workOrderId, input)
          : await api.importLaserNestPackageStandalone(input);
      onImported(result?.child_work_order?.id);
    } catch (err: any) {
      setError(err?.response?.data?.detail || 'Failed to import laser nest package.');
      setLoading(false); // keep the wizard open so the planner can retry
    }
  };

  const lowConfidenceCount = rows.filter((r) => r.confidence === 'low').length;
  const totalRuns = rows.reduce((sum, r) => sum + (Number(r.planned_runs) || 0), 0);

  return (
    <Modal
      open={open}
      onClose={onClose}
      size={step === 'review' ? '5xl' : 'lg'}
      ariaLabelledBy="laser-nest-wizard-title"
      closeOnBackdrop={!loading}
    >
      <div className="space-y-4">
        <div>
          <h2 id="laser-nest-wizard-title" className="text-lg font-semibold text-fd-ink">
            Import laser nest package
          </h2>
          <p className="mt-1 text-sm text-fd-mute">
            {step === 'pick'
              ? 'Upload a ZIP package of nest report PDFs (or CNC program files), or a nest-report PDF — single or multi-page. We read the CNC number, material, and size from each sheet so you can review before importing.'
              : 'Review and correct each nest, then import. AI-extracted values are editable — verify low-confidence rows before importing.'}
            {workOrderId == null &&
              ' Importing creates a new released laser cutting work order sized to the total sheet runs — no parent work order or part required.'}
          </p>
        </div>

        {step === 'pick' && (
          <div className="space-y-3">
            <label className="block">
              <span className="text-xs font-medium text-fd-mute">ZIP package or nest-report PDF</span>
              <input
                key={fileInputKey}
                type="file"
                accept=".zip,.pdf"
                aria-label="ZIP package or nest-report PDF"
                onChange={(e) => {
                  setFile(e.target.files?.[0] || null);
                  setError('');
                }}
                className="mt-1 block w-full text-sm text-fd-body file:mr-3 file:rounded-none file:border-0 file:bg-fd-raised file:px-3 file:py-2 file:text-sm file:font-semibold file:text-fd-ink hover:file:bg-fd-line-bright"
              />
            </label>
            <label className="block">
              <span className="text-xs font-medium text-fd-mute">Or server folder path</span>
              <input
                type="text"
                value={sourcePath}
                aria-label="Server folder path"
                onChange={(e) => {
                  setSourcePath(e.target.value);
                  setError('');
                }}
                placeholder="/path/to/ermaksan/nest-folder"
                className="input mt-1 w-full"
              />
            </label>
            <p className="text-xs text-fd-faint">
              AI extraction runs on preview and can take a few seconds per sheet for large packages. A multi-page PDF is
              split into its individual nests automatically.
            </p>
          </div>
        )}

        {step === 'review' && (
          <div className="space-y-3">
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <span className="rounded-none border border-fd-line bg-fd-sunken px-2 py-1 font-semibold text-fd-body">
                {rows.length} {rows.length === 1 ? 'nest' : 'nests'}
              </span>
              <span className="rounded-none border border-fd-line bg-fd-sunken px-2 py-1 font-semibold text-fd-body">
                {totalRuns} total runs
              </span>
              {packageMeta?.source_page_count != null && (
                <span className="rounded-none border border-fd-line bg-fd-sunken px-2 py-1 font-semibold text-fd-body">
                  {packageMeta.source_page_count} {packageMeta.source_page_count === 1 ? 'page' : 'pages'} →{' '}
                  {rows.length} {rows.length === 1 ? 'nest' : 'nests'}
                </span>
              )}
              {lowConfidenceCount > 0 && (
                <span className="rounded-none border border-fd-red/40 bg-fd-red/10 px-2 py-1 font-semibold text-fd-red">
                  {lowConfidenceCount} low-confidence — double-check
                </span>
              )}
              {packageMeta?.segmentation_warning && (
                <span className="rounded-none border border-fd-amber/40 bg-fd-amber/10 px-2 py-1 font-semibold text-fd-amber">
                  {packageMeta.segmentation_warning}
                </span>
              )}
              {packageMeta != null && packageMeta.skipped_pages.length > 0 && (
                <span className="px-1 py-1 text-fd-mute">
                  Pages skipped as non-nest: {packageMeta.skipped_pages.join(', ')}
                </span>
              )}
            </div>

            <div className="max-h-[55vh] overflow-auto border border-fd-line">
              <table className="min-w-full border-collapse">
                <thead className="sticky top-0 z-10 bg-fd-panel">
                  <tr className="border-b border-fd-line">
                    <th className={TH}>Source</th>
                    <th className={TH}>CNC #</th>
                    <th className={TH}>Material</th>
                    <th className={TH}>Thickness</th>
                    <th className={TH}>Sheet size</th>
                    <th className={`${TH} text-right`}>Runs</th>
                    <th className={TH}>Conf.</th>
                    <th className={TH} aria-label="Remove" />
                  </tr>
                </thead>
                <tbody>
                  {rows.length === 0 && (
                    <tr>
                      <td colSpan={8} className="px-3 py-6 text-center text-sm text-fd-mute">
                        No nests left to import. Re-preview a package or close.
                      </td>
                    </tr>
                  )}
                  {rows.map((row, index) => {
                    const badge = row.confidence ? CONFIDENCE_BADGE[row.confidence] : null;
                    // Per-field verify state: amber highlight + "verify" affordance
                    // until the planner edits the field.
                    const verify = {
                      cnc_number: fieldNeedsVerify(row, 'cnc_number'),
                      material: fieldNeedsVerify(row, 'material'),
                      thickness: fieldNeedsVerify(row, 'thickness'),
                      sheet_size: fieldNeedsVerify(row, 'sheet_size'),
                      planned_runs: fieldNeedsVerify(row, 'planned_runs'),
                    };
                    const verifyLabel = (base: string, needsVerify: boolean) =>
                      needsVerify ? `${base} — low confidence, verify` : base;
                    const pageRange =
                      row.source_pages && row.source_pages.length > 0 ? formatPageRange(row.source_pages) : null;
                    return (
                      <tr key={row.source_file} className="border-b border-fd-line align-top">
                        <td className="px-3 py-2 text-xs text-fd-mute">
                          {/* For PDF uploads the generated file name is noise — show the
                              page range and keep the file name as the tooltip. */}
                          <span className="block max-w-[16rem] truncate" title={row.source_file}>
                            {pageRange ?? row.source_file}
                          </span>
                          {row.cnc_file_name && (
                            <span className="block max-w-[16rem] truncate text-fd-faint" title={row.cnc_file_name}>
                              {row.cnc_file_name}
                            </span>
                          )}
                        </td>
                        <td className="px-3 py-2">
                          <input
                            type="text"
                            value={row.cnc_number}
                            onChange={(e) => updateField(index, 'cnc_number', e.target.value)}
                            className={verify.cnc_number ? CELL_INPUT_VERIFY : CELL_INPUT}
                            aria-label={verifyLabel(`CNC number for ${row.source_file}`, verify.cnc_number)}
                            title={verify.cnc_number ? 'Low confidence — verify' : undefined}
                          />
                        </td>
                        <td className="px-3 py-2">
                          <input
                            type="text"
                            value={row.material}
                            onChange={(e) => updateField(index, 'material', e.target.value)}
                            className={verify.material ? CELL_INPUT_VERIFY : CELL_INPUT}
                            aria-label={verifyLabel(`Material for ${row.source_file}`, verify.material)}
                            title={verify.material ? 'Low confidence — verify' : undefined}
                          />
                        </td>
                        <td className="px-3 py-2">
                          <input
                            type="text"
                            value={row.thickness}
                            onChange={(e) => updateField(index, 'thickness', e.target.value)}
                            className={verify.thickness ? CELL_INPUT_VERIFY : CELL_INPUT}
                            aria-label={verifyLabel(`Thickness for ${row.source_file}`, verify.thickness)}
                            title={verify.thickness ? 'Low confidence — verify' : undefined}
                          />
                        </td>
                        <td className="px-3 py-2">
                          <input
                            type="text"
                            value={row.sheet_size}
                            onChange={(e) => updateField(index, 'sheet_size', e.target.value)}
                            className={verify.sheet_size ? CELL_INPUT_VERIFY : CELL_INPUT}
                            aria-label={verifyLabel(`Sheet size for ${row.source_file}`, verify.sheet_size)}
                            title={verify.sheet_size ? 'Low confidence — verify' : undefined}
                          />
                        </td>
                        <td className="px-3 py-2">
                          <input
                            type="number"
                            min={1}
                            step={1}
                            value={row.planned_runs}
                            onChange={(e) => updateField(index, 'planned_runs', e.target.value)}
                            className={`${verify.planned_runs ? CELL_INPUT_VERIFY : CELL_INPUT} w-20 text-right tabular-nums`}
                            aria-label={verifyLabel(`Runs for ${row.source_file}`, verify.planned_runs)}
                            title={verify.planned_runs ? 'Low confidence — verify' : undefined}
                          />
                        </td>
                        <td className="px-3 py-2">
                          <span className="inline-flex items-center gap-1">
                            {badge ? (
                              <span
                                className={`rounded-none border px-1.5 py-0.5 text-[10px] font-semibold uppercase ${badge.className}`}
                              >
                                {badge.label}
                              </span>
                            ) : (
                              <span className="text-xs text-fd-faint">—</span>
                            )}
                            {row.warning && (
                              <span
                                role="img"
                                aria-label={`Warning for ${row.source_file}: ${row.warning}`}
                                title={row.warning}
                                className="inline-flex cursor-help text-fd-amber"
                              >
                                <ExclamationTriangleIcon className="h-4 w-4" aria-hidden="true" />
                              </span>
                            )}
                          </span>
                        </td>
                        <td className="px-3 py-2 text-right">
                          <button
                            type="button"
                            onClick={() => removeRow(index)}
                            className="text-fd-mute hover:text-fd-red"
                            aria-label={`Remove ${row.source_file}`}
                          >
                            Remove
                          </button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {error && (
          <div className="rounded border border-fd-red/40 bg-fd-red/10 px-3 py-2 text-sm text-fd-red">{error}</div>
        )}

        <div className="flex items-center justify-between gap-2 pt-2">
          <div>
            {step === 'review' && (
              <button
                type="button"
                onClick={() => {
                  setStep('pick');
                  setError('');
                }}
                disabled={loading}
                className="btn-ghost btn-sm"
              >
                Back
              </button>
            )}
          </div>
          <div className="flex items-center gap-2">
            <button type="button" onClick={onClose} disabled={loading} className="btn-secondary">
              Cancel
            </button>
            {step === 'pick' ? (
              <button type="button" onClick={handlePreview} disabled={loading || !hasInput} className="btn-primary">
                {loading ? 'Extracting…' : 'Preview'}
              </button>
            ) : (
              <button
                type="button"
                onClick={handleImport}
                disabled={loading || rows.length === 0}
                className="btn-primary"
              >
                {loading ? 'Importing…' : `Import ${rows.length} ${rows.length === 1 ? 'nest' : 'nests'}`}
              </button>
            )}
          </div>
        </div>
      </div>
    </Modal>
  );
}
