import React, { useEffect, useMemo, useState } from 'react';
import { ExclamationTriangleIcon } from '@heroicons/react/24/outline';
import { Modal } from '../ui/Modal';
import {
  LaserNestImportRow,
  LaserNestPreviewRow,
  LaserNestExtractionConfidence,
  LaserNestConfidenceField,
  LaserNestFieldConfidence,
  MaterialAllocation,
  Part,
  WorkCenter,
} from '../../types';
import api from '../../services/api';
import { toDisplayString } from '../../utils/apiError';
import { defaultLaserWorkCenter, sortWorkCentersForLaserDispatch } from '../../utils/laserWorkCenters';
// Shared with the Dispatch Board chip and the kiosk deduction notice, so one
// quantity never renders three ways across the feature.
import { formatTieQty } from '../../utils/materialTie';

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
  /** Per-nest WC override; null = follow the package-level pick / auto-detect. */
  work_center_id: number | null;
  /**
   * Sheet part this nest consumes; null = untied. ALWAYS an explicit pick —
   * never fuzzy-matched from the AI-read `material` / `thickness` free text,
   * because a wrong tie depletes the wrong heat lot into an as-built record.
   */
  material_part_id: number | null;
  /** Sheets per completed run. String while editing, mirroring `planned_runs`. */
  qty_per_run: string;
}

/** Preview metadata about the uploaded package itself (bare-PDF uploads). */
interface PackageMeta {
  source_page_count: number | null;
  skipped_pages: number[];
  segmentation_warning: string | null;
}

/** One row of `GET /inventory/summary` (the endpoint is untyped in the client). */
interface InventorySummaryRow {
  part_id: number;
  total_on_hand: number;
}

/** A sheet-part choice in the pickers: the part id plus its rendered label. */
interface MaterialOption {
  id: number;
  label: string;
}

/**
 * Explicit page size for the material load. `api.getMaterials` (like
 * `getParts`) falls into an unbounded `while (true)` loop pulling 500 rows at a
 * time when `limit` is omitted — a picker never needs that.
 */
const MATERIAL_OPTION_LIMIT = 500;

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
    work_center_id: null,
    material_part_id: null,
    qty_per_run: '1',
  };
}

const workCenterLabel = (wc: WorkCenter) => wc.name || wc.code;

/**
 * Label for one sheet-part option.
 *
 * The stock figure is deliberately called **on hand**, never "available":
 * `quantity_allocated` is never written by anything, so an "available" number
 * would just be on-hand under a name that goes false the day reservations ship.
 * `onHand === undefined` means the stock read didn't land — show no figure at
 * all rather than a fabricated zero.
 */
function materialOptionLabel(part: Part, onHand: number | undefined): string {
  const name = part.part_number ? `${part.part_number} — ${part.name}` : part.name;
  if (onHand == null) return name;
  return `${name} (${formatTieQty(onHand)} ${part.unit_of_measure} on hand)`;
}

/**
 * Await a degradable side-load. Any failure — a real error, or a test double
 * that never stubbed the method — resolves to an empty list, so an optional
 * picker can never block the import flow.
 */
async function loadOrEmpty<T>(load: () => Promise<T[]>): Promise<T[]> {
  try {
    return (await load()) ?? [];
  } catch {
    return [];
  }
}

/**
 * Import failure text.
 *
 * The detail is normalized because 409s on this feature can carry an object
 * rather than a string — and that is ALL this does. The consumed-tie re-import
 * refusal used to be special-cased with a client addendum ("reversing
 * consumption is not available yet"); the RETURN verb has since shipped and the
 * server's refusal is now self-contained (nothing was destroyed, a return does
 * not unlock the rebuild, the remedy is a new work order), so the detail
 * renders verbatim.
 */
function importErrorMessage(err: any): string {
  const detail = toDisplayString(err?.response?.data?.detail).trim();
  return detail || 'Failed to import laser nest package.';
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

  // Dispatch controls: active work centers (laser-first order) for the
  // standalone package-level pick and the per-row overrides, plus the
  // standalone-only due date ('' = none) and package work-center pick
  // ('' = auto-detect).
  const [workCenters, setWorkCenters] = useState<WorkCenter[]>([]);
  const [dispatchDueDate, setDispatchDueDate] = useState('');
  const [dispatchWorkCenterId, setDispatchWorkCenterId] = useState('');

  // Sheet-part tie controls: the material list for the pickers, a part_id ->
  // on-hand map for the option hint (null = the stock read didn't land, so no
  // figure is shown), and the package-level default the planner applies to
  // every row at once.
  const [materials, setMaterials] = useState<Part[]>([]);
  const [onHandByPart, setOnHandByPart] = useState<Record<number, number> | null>(null);
  const [packageMaterialPartId, setPackageMaterialPartId] = useState('');
  const [packageQtyPerRun, setPackageQtyPerRun] = useState('1');
  // Sheet parts a pre-filled tie names that the material load didn't return
  // (capped list, deactivated part, failed read). Appended to the options so an
  // existing tie can never render as an empty picker while still riding on the
  // import payload.
  const [extraMaterialOptions, setExtraMaterialOptions] = useState<MaterialOption[]>([]);
  const [tiePrefillCount, setTiePrefillCount] = useState(0);

  // Reset everything whenever the wizard (re)opens, then load the active work
  // centers for the dispatch picks. In standalone mode the package pick
  // defaults to the caller's workCenterId, else the preferred laser (Ermaksan
  // fiber first — never a tube laser); "(auto-detect)" stays available.
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
    setDispatchDueDate('');
    setDispatchWorkCenterId(workCenterId != null ? String(workCenterId) : '');
    setPackageMaterialPartId('');
    setPackageQtyPerRun('1');
    setExtraMaterialOptions([]);
    setTiePrefillCount(0);

    let cancelled = false;
    (async () => {
      // Every load here is optional chrome for the review step, so each one
      // degrades on its own: a dead /materials must not cost the WC picks, and
      // the stock summary is read exactly ONCE per wizard open (it is
      // unpaginated and returns every stock row in the tenant — never per row,
      // never per keystroke).
      const [centers, materialParts, stock] = await Promise.all([
        loadOrEmpty<WorkCenter>(() => api.getWorkCenters(true)),
        loadOrEmpty<Part>(() => api.getMaterials({ active_only: true, limit: MATERIAL_OPTION_LIMIT })),
        loadOrEmpty<InventorySummaryRow>(() => api.getInventorySummary()),
      ]);
      if (cancelled) return;

      const sorted = sortWorkCentersForLaserDispatch(centers.filter((wc) => wc.is_active));
      setWorkCenters(sorted);
      setMaterials(materialParts);
      // An empty read is treated as "unknown" rather than "everything is zero":
      // a failed load and a tenant with no stock are indistinguishable here, and
      // a fabricated 0 on hand is the worse of the two errors.
      if (stock.length === 0) {
        setOnHandByPart(null);
      } else {
        const onHand: Record<number, number> = {};
        for (const item of stock) {
          if (item && typeof item.part_id === 'number') onHand[item.part_id] = Number(item.total_on_hand) || 0;
        }
        setOnHandByPart(onHand);
      }
      if (workOrderId == null && workCenterId == null) {
        const preferred = defaultLaserWorkCenter(sorted);
        if (preferred) setDispatchWorkCenterId(String(preferred.id));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [open, workOrderId, workCenterId]);

  /**
   * Sheet-part choices: the loaded material parts plus any part a pre-filled
   * tie names that the list didn't include.
   */
  const materialOptions = useMemo<MaterialOption[]>(() => {
    const options: MaterialOption[] = materials.map((part) => ({
      id: part.id,
      label: materialOptionLabel(part, onHandByPart ? (onHandByPart[part.id] ?? 0) : undefined),
    }));
    const known = new Set(options.map((option) => option.id));
    for (const extra of extraMaterialOptions) {
      if (!known.has(extra.id)) options.push(extra);
    }
    return options;
  }, [materials, onHandByPart, extraMaterialOptions]);

  const hasInput = Boolean(file) || sourcePath.trim().length > 0;

  /**
   * Pre-fill each row's sheet-part picker from the tie the matching nest
   * already carries.
   *
   * A re-import CANCELS and DETACHES every existing tie as it rebuilds the
   * operations, so a package that comes back with empty pickers silently
   * un-ties the work order with no signal. Matching is by the nest's stored
   * `cnc_file_path`, which is exactly the preview row's `source_file` (the
   * backend rejects duplicates, so it is unique within a package).
   *
   * Entirely degradable and read-only: no ties, an older server, or a
   * parent-addressed import (whose ties live on the laser CHILD work order,
   * not on the parent) simply leaves the pickers empty.
   */
  const prefillTiesFromExistingNests = async (previewRows: EditableRow[]) => {
    if (workOrderId == null || previewRows.length === 0) return;

    let ties: MaterialAllocation[] = [];
    let workOrder: { operations?: Array<{ id?: number; laser_nest?: { cnc_file_path?: string | null } | null }> } = {};
    try {
      [ties, workOrder] = await Promise.all([
        api.getMaterialAllocations(workOrderId, false),
        api.getWorkOrder(workOrderId),
      ]);
    } catch {
      return; // the pre-fill is a safety net, never a gate on the review step
    }

    const sourceByOperation = new Map<number, string>();
    for (const operation of workOrder?.operations ?? []) {
      const path = operation?.laser_nest?.cnc_file_path;
      if (operation?.id != null && path) sourceByOperation.set(operation.id, path);
    }

    const tieBySource = new Map<string, MaterialAllocation>();
    for (const tie of ties ?? []) {
      // OPEN operation-scoped ties only. `closed` is never written (a fully
      // consumed tie stays `open`), so `open` IS the live set, and a
      // work-order-scoped tie is not a nest tie.
      if (tie.status !== 'open' || tie.work_order_operation_id == null) continue;
      const source = sourceByOperation.get(tie.work_order_operation_id);
      if (source) tieBySource.set(source, tie);
    }
    if (tieBySource.size === 0) return;

    setRows((prev) =>
      prev.map((row) => {
        const tie = tieBySource.get(row.source_file);
        if (!tie) return row;
        return { ...row, material_part_id: tie.part_id, qty_per_run: String(tie.qty_per_run ?? 1) };
      })
    );
    // Keep every tied part selectable even if it is missing from the material
    // list (capped, deactivated, or a failed load).
    setExtraMaterialOptions(
      Array.from(tieBySource.values()).map((tie) => ({
        id: tie.part_id,
        label: [tie.part_number, tie.part_name].filter(Boolean).join(' — ') || `Part ${tie.part_id}`,
      }))
    );
    setTiePrefillCount(tieBySource.size);
  };

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
      const editable = result.nests.map(toEditable);
      setRows(editable);
      setPackageMeta({
        source_page_count: result.source_page_count ?? null,
        skipped_pages: result.skipped_pages ?? [],
        segmentation_warning: result.segmentation_warning ?? null,
      });
      setExtraMaterialOptions([]);
      setTiePrefillCount(0);
      setStep('review');
      void prefillTiesFromExistingNests(editable);
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

  /** Per-row WC override; '' clears back to the package default. */
  const updateRowWorkCenter = (index: number, value: string) => {
    setRows((prev) =>
      prev.map((row, i) => (i === index ? { ...row, work_center_id: value ? Number(value) : null } : row))
    );
  };

  // The two tie fields deliberately do NOT go through `updateField`: that helper
  // is typed to the AI-extracted `LaserNestConfidenceField`s and stamps the
  // confidence `edited` map. There is no AI-read part id — a tie is always an
  // explicit human pick — so neither field gets confidence treatment.

  /** Per-row sheet-part tie; '' leaves the nest untied. */
  const updateRowMaterialPart = (index: number, value: string) => {
    setRows((prev) =>
      prev.map((row, i) => (i === index ? { ...row, material_part_id: value ? Number(value) : null } : row))
    );
  };

  /** Sheets consumed per completed run on a tied row. */
  const updateRowQtyPerRun = (index: number, value: string) => {
    setRows((prev) => prev.map((row, i) => (i === index ? { ...row, qty_per_run: value } : row)));
  };

  /**
   * Package-level default: stamp the picked sheet part + qty onto EVERY row.
   *
   * Deliberately an explicit action rather than a fuzzy auto-match off the
   * AI-extracted `material` text — a wrong tie depletes the wrong heat lot into
   * an as-built record. Rows stay individually editable afterwards.
   */
  const applyPackageMaterial = () => {
    if (!packageMaterialPartId) return;
    const partId = Number(packageMaterialPartId);
    const qty = packageQtyPerRun.trim() || '1';
    setRows((prev) => prev.map((row) => ({ ...row, material_part_id: partId, qty_per_run: qty })));
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
      // A tied row needs a real per-run quantity (the API enforces > 0). A qty
      // is never sent without a part, and a 0 is never sent at all.
      if (row.material_part_id != null) {
        const perRun = Number(row.qty_per_run);
        if (!Number.isFinite(perRun) || perRun <= 0) {
          setError(`Sheets per run for ${row.source_file} must be greater than 0.`);
          return;
        }
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
      // Per-nest WC override rides along only when the planner set one.
      ...(row.work_center_id != null ? { work_center_id: row.work_center_id } : {}),
      // Same spread-only-when-set rule for the sheet-part tie: an untied row's
      // payload stays byte-identical to a pre-feature import.
      ...(row.material_part_id != null
        ? { material_part_id: row.material_part_id, qty_per_run: Number(row.qty_per_run) }
        : {}),
    }));

    setLoading(true);
    setError('');
    try {
      const input = {
        file,
        source_path: sourcePath.trim() || undefined,
        rows: confirmed,
      };
      const result =
        workOrderId != null
          ? await api.importLaserNestPackage(workOrderId, {
              ...input,
              work_center_id: workCenterId ?? undefined,
            })
          : await api.importLaserNestPackageStandalone({
              ...input,
              // Standalone dispatch strip: only send concrete picks.
              due_date: dispatchDueDate || undefined,
              work_center_id: dispatchWorkCenterId ? Number(dispatchWorkCenterId) : undefined,
            });
      onImported(result?.child_work_order?.id);
    } catch (err: any) {
      setError(importErrorMessage(err));
      setLoading(false); // keep the wizard open so the planner can retry
    }
  };

  const lowConfidenceCount = rows.filter((r) => r.confidence === 'low').length;
  const totalRuns = rows.reduce((sum, r) => sum + (Number(r.planned_runs) || 0), 0);
  const tiedRowCount = rows.filter((r) => r.material_part_id != null).length;
  const tiedSheetTotal = rows.reduce(
    (sum, r) =>
      r.material_part_id == null ? sum : sum + (Number(r.qty_per_run) || 0) * (Number(r.planned_runs) || 0),
    0
  );

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
            {' Every nest is ready to run the moment the import lands, and the WC picks let you spread them across lasers.'}
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
            {/* Standalone dispatch strip: due date + package work center for the
                laser WO the import will create. Parented imports inherit the
                target WO's dates, so the strip stays standalone-only. */}
            {workOrderId == null && (
              <div className="flex flex-wrap items-end gap-x-4 gap-y-2 rounded-none border border-fd-line bg-fd-sunken px-3 py-2">
                <span className="pb-1.5 text-[11px] font-semibold uppercase tracking-wide text-fd-mute">Dispatch</span>
                <div>
                  <label htmlFor="nest-dispatch-due-date" className="block text-xs font-medium text-fd-mute">
                    Due date
                  </label>
                  <input
                    id="nest-dispatch-due-date"
                    type="date"
                    value={dispatchDueDate}
                    onChange={(e) => setDispatchDueDate(e.target.value)}
                    className="input mt-1 !py-1 text-sm"
                  />
                </div>
                <div>
                  <label htmlFor="nest-dispatch-work-center" className="block text-xs font-medium text-fd-mute">
                    Work center
                  </label>
                  <select
                    id="nest-dispatch-work-center"
                    value={dispatchWorkCenterId}
                    onChange={(e) => setDispatchWorkCenterId(e.target.value)}
                    className="input mt-1 !py-1 text-sm"
                  >
                    <option value="">(auto-detect)</option>
                    {workCenters.map((wc) => (
                      <option key={wc.id} value={String(wc.id)}>
                        {workCenterLabel(wc)}
                      </option>
                    ))}
                  </select>
                </div>
                <p className="basis-full text-xs text-fd-faint sm:basis-auto sm:pb-1.5">
                  Applies to the new laser work order; per-nest WC overrides win.
                </p>
              </div>
            )}

            {/* Package-level sheet-part default. Optional in every mode: an
                untied package imports exactly as it always has. */}
            {materialOptions.length > 0 && (
              <div className="flex flex-wrap items-end gap-x-4 gap-y-2 rounded-none border border-fd-line bg-fd-sunken px-3 py-2">
                <span className="pb-1.5 text-[11px] font-semibold uppercase tracking-wide text-fd-mute">Sheet</span>
                <div>
                  <label htmlFor="nest-package-material-part" className="block text-xs font-medium text-fd-mute">
                    Sheet part
                  </label>
                  <select
                    id="nest-package-material-part"
                    value={packageMaterialPartId}
                    onChange={(e) => setPackageMaterialPartId(e.target.value)}
                    className="input mt-1 max-w-[22rem] !py-1 text-sm"
                  >
                    <option value="">(none)</option>
                    {materialOptions.map((option) => (
                      <option key={option.id} value={String(option.id)}>
                        {option.label}
                      </option>
                    ))}
                  </select>
                </div>
                <div>
                  <label htmlFor="nest-package-qty-per-run" className="block text-xs font-medium text-fd-mute">
                    Sheets / run
                  </label>
                  <input
                    id="nest-package-qty-per-run"
                    type="number"
                    min={0}
                    step="any"
                    value={packageQtyPerRun}
                    onChange={(e) => setPackageQtyPerRun(e.target.value)}
                    className="input mt-1 w-24 !py-1 text-right text-sm tabular-nums"
                  />
                </div>
                <button
                  type="button"
                  onClick={applyPackageMaterial}
                  disabled={!packageMaterialPartId || rows.length === 0}
                  className="btn-secondary btn-sm mb-0.5"
                >
                  Apply to all rows
                </button>
                <p className="basis-full text-xs text-fd-faint sm:basis-auto sm:pb-1.5">
                  Optional. Stamps every row below — each row stays editable. Tied sheets leave inventory when that nest's
                  operation completes, not per run.
                </p>
              </div>
            )}

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
              {/* Consumption fires when an OPERATION completes, never per run.
                  A laser WO holds one operation per nest, so each nest deducts its
                  own sheets as it closes — but reporting runs on a still-open nest
                  deducts nothing (an IN_PROGRESS operation is still reducible, and
                  consumption never auto-reverses). Do not drift this either way. */}
              {tiedRowCount > 0 && (
                <span className="rounded-none border border-fd-line bg-fd-sunken px-2 py-1 font-semibold text-fd-body">
                  {tiedRowCount} tied — {formatTieQty(tiedSheetTotal)} sheets deducted as each nest completes
                </span>
              )}
              {tiePrefillCount > 0 && (
                <span className="rounded-none border border-fd-blue/40 bg-fd-blue/10 px-2 py-1 font-semibold text-fd-blue">
                  {tiePrefillCount} existing {tiePrefillCount === 1 ? 'tie' : 'ties'} pre-filled — re-importing replaces
                  them
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
                    <th className={TH}>Sheet part</th>
                    <th className={`${TH} text-right`}>Sheets/run</th>
                    <th className={`${TH} text-right`}>Runs</th>
                    <th className={TH}>WC</th>
                    <th className={TH}>Conf.</th>
                    <th className={TH} aria-label="Remove" />
                  </tr>
                </thead>
                <tbody>
                  {rows.length === 0 && (
                    <tr>
                      <td colSpan={11} className="px-3 py-6 text-center text-sm text-fd-mute">
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
                          <select
                            value={row.material_part_id != null ? String(row.material_part_id) : ''}
                            onChange={(e) => updateRowMaterialPart(index, e.target.value)}
                            disabled={materialOptions.length === 0}
                            className={`${CELL_INPUT} min-w-[10rem] max-w-[18rem] disabled:opacity-50`}
                            aria-label={`Sheet part for ${row.source_file}`}
                          >
                            <option value="">(none)</option>
                            {materialOptions.map((option) => (
                              <option key={option.id} value={String(option.id)}>
                                {option.label}
                              </option>
                            ))}
                          </select>
                        </td>
                        <td className="px-3 py-2">
                          <input
                            type="number"
                            min={0}
                            step="any"
                            value={row.qty_per_run}
                            onChange={(e) => updateRowQtyPerRun(index, e.target.value)}
                            disabled={row.material_part_id == null}
                            className={`${CELL_INPUT} w-20 text-right tabular-nums disabled:opacity-50`}
                            aria-label={`Sheets per run for ${row.source_file}`}
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
                          <select
                            value={row.work_center_id != null ? String(row.work_center_id) : ''}
                            onChange={(e) => updateRowWorkCenter(index, e.target.value)}
                            className={`${CELL_INPUT} min-w-[8rem]`}
                            aria-label={`Work center for ${row.source_file}`}
                          >
                            <option value="">package default</option>
                            {workCenters.map((wc) => (
                              <option key={wc.id} value={String(wc.id)}>
                                {workCenterLabel(wc)}
                              </option>
                            ))}
                          </select>
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
