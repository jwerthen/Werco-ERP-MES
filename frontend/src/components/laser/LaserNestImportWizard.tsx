import React, { useEffect, useState } from 'react';
import { Modal } from '../ui/Modal';
import { LaserNestImportRow, LaserNestPreviewRow, LaserNestExtractionConfidence } from '../../types';
import api from '../../services/api';

interface LaserNestImportWizardProps {
  open: boolean;
  onClose: () => void;
  /** Parent (assembly) work order the laser child WO is created under. */
  workOrderId: number;
  /**
   * Optional work center to assign the generated laser operations to. Passed
   * straight through to the import call; the backend applies its default when
   * omitted.
   */
  workCenterId?: number | null;
  /**
   * Called after a successful import with the created child laser WO id (when
   * present) so the parent can navigate to it; otherwise the parent refreshes.
   */
  onImported: (childWorkOrderId?: number) => void;
}

type WizardStep = 'pick' | 'review';

/** Local, editable mirror of a preview row. Keeps `source_file` as the stable
 *  key the backend matches PDFs by; everything else the planner can correct. */
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
}

const CONFIDENCE_BADGE: Record<LaserNestExtractionConfidence, { label: string; className: string }> = {
  high: { label: 'High', className: 'border-fd-green/40 bg-fd-green/10 text-fd-green' },
  medium: { label: 'Med', className: 'border-fd-amber/40 bg-fd-amber/10 text-fd-amber' },
  low: { label: 'Low', className: 'border-fd-red/40 bg-fd-red/10 text-fd-red' },
};

const TH = 'px-3 py-2 text-left text-[11px] font-semibold uppercase tracking-wide text-fd-mute';
const CELL_INPUT =
  'w-full rounded-none border border-fd-line bg-fd-sunken px-2 py-1 text-sm text-fd-ink focus:border-fd-blue focus:outline-none';

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
  };
}

/**
 * Two-step wizard for importing a ZIP of laser-nest sheets onto an assembly WO.
 *
 *   1. Pick a ZIP (or, when the server supports it, a folder path).
 *   2. Preview runs AI extraction server-side and returns editable rows; the
 *      planner reviews/corrects them, removing any that shouldn't be imported.
 *   3. Import re-sends the SAME ZIP plus the confirmed rows — the backend matches
 *      each row to its PDF by `source_file` and persists the confirmed values
 *      without a second AI call.
 *
 * The same flow handles a ZIP of CNC *program* files: those preview rows carry
 * `cnc_file_name` instead of an AI-read `cnc_number`/`confidence`, and sending
 * them back unchanged preserves the legacy import behavior.
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

  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  // Reset everything whenever the wizard (re)opens.
  useEffect(() => {
    if (!open) return;
    setStep('pick');
    setFile(null);
    setSourcePath('');
    setRows([]);
    setLoading(false);
    setError('');
    setFileInputKey((k) => k + 1);
  }, [open]);

  const hasInput = Boolean(file) || sourcePath.trim().length > 0;

  const handlePreview = async () => {
    if (!hasInput) {
      setError('Choose a ZIP package or enter a folder path.');
      return;
    }
    setLoading(true);
    setError('');
    try {
      const result = await api.previewLaserNestPackage(workOrderId, {
        file,
        source_path: sourcePath.trim() || undefined,
      });
      setRows(result.nests.map(toEditable));
      setStep('review');
    } catch (err: any) {
      setError(err?.response?.data?.detail || 'Failed to preview laser nest package.');
    } finally {
      setLoading(false);
    }
  };

  const updateRow = (index: number, patch: Partial<EditableRow>) => {
    setRows((prev) => prev.map((row, i) => (i === index ? { ...row, ...patch } : row)));
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
    }));

    setLoading(true);
    setError('');
    try {
      const result = await api.importLaserNestPackage(workOrderId, {
        file,
        source_path: sourcePath.trim() || undefined,
        work_center_id: workCenterId ?? undefined,
        rows: confirmed,
      });
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
              ? 'Upload a ZIP of nest report PDFs (or CNC program files). We read the CNC number, material, and size from each sheet so you can review before importing.'
              : 'Review and correct each nest, then import. AI-extracted values are editable — verify low-confidence rows before importing.'}
          </p>
        </div>

        {step === 'pick' && (
          <div className="space-y-3">
            <label className="block">
              <span className="text-xs font-medium text-fd-mute">ZIP package</span>
              <input
                key={fileInputKey}
                type="file"
                accept=".zip"
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
                onChange={(e) => {
                  setSourcePath(e.target.value);
                  setError('');
                }}
                placeholder="/path/to/ermaksan/nest-folder"
                className="input mt-1 w-full"
              />
            </label>
            <p className="text-xs text-fd-faint">
              AI extraction runs on preview and can take a few seconds per sheet for large packages.
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
              {lowConfidenceCount > 0 && (
                <span className="rounded-none border border-fd-red/40 bg-fd-red/10 px-2 py-1 font-semibold text-fd-red">
                  {lowConfidenceCount} low-confidence — double-check
                </span>
              )}
            </div>

            <div className="max-h-[55vh] overflow-auto border border-fd-line">
              <table className="min-w-full border-collapse">
                <thead className="sticky top-0 z-10 bg-fd-panel">
                  <tr className="border-b border-fd-line">
                    <th className={TH}>Source file</th>
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
                    return (
                      <tr key={row.source_file} className="border-b border-fd-line align-top">
                        <td className="px-3 py-2 text-xs text-fd-mute">
                          <span className="block max-w-[16rem] truncate" title={row.source_file}>
                            {row.source_file}
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
                            onChange={(e) => updateRow(index, { cnc_number: e.target.value })}
                            className={CELL_INPUT}
                            aria-label={`CNC number for ${row.source_file}`}
                          />
                        </td>
                        <td className="px-3 py-2">
                          <input
                            type="text"
                            value={row.material}
                            onChange={(e) => updateRow(index, { material: e.target.value })}
                            className={CELL_INPUT}
                            aria-label={`Material for ${row.source_file}`}
                          />
                        </td>
                        <td className="px-3 py-2">
                          <input
                            type="text"
                            value={row.thickness}
                            onChange={(e) => updateRow(index, { thickness: e.target.value })}
                            className={CELL_INPUT}
                            aria-label={`Thickness for ${row.source_file}`}
                          />
                        </td>
                        <td className="px-3 py-2">
                          <input
                            type="text"
                            value={row.sheet_size}
                            onChange={(e) => updateRow(index, { sheet_size: e.target.value })}
                            className={CELL_INPUT}
                            aria-label={`Sheet size for ${row.source_file}`}
                          />
                        </td>
                        <td className="px-3 py-2">
                          <input
                            type="number"
                            min={1}
                            step={1}
                            value={row.planned_runs}
                            onChange={(e) => updateRow(index, { planned_runs: e.target.value })}
                            className={`${CELL_INPUT} w-20 text-right tabular-nums`}
                            aria-label={`Runs for ${row.source_file}`}
                          />
                        </td>
                        <td className="px-3 py-2">
                          {badge ? (
                            <span
                              className={`rounded-none border px-1.5 py-0.5 text-[10px] font-semibold uppercase ${badge.className}`}
                            >
                              {badge.label}
                            </span>
                          ) : (
                            <span className="text-xs text-fd-faint">—</span>
                          )}
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
