import React, { useEffect, useMemo, useRef, useState } from 'react';
import { ExclamationTriangleIcon, LinkIcon, TrashIcon } from '@heroicons/react/24/outline';
import { Modal } from '../ui/Modal';
import { ComboBoxOption } from '../ui/ComboBox';
import { SheetPartPicker, sheetPartOptionLabel } from './SheetPartPicker';
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
import { deriveSheetSpec, isSheetLikePart } from '../../utils/sheetPart';

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

/** The two nest fields a tied sheet part can supply. */
type SpecField = 'thickness' | 'sheet_size';

/**
 * A spec value written by the sheet-part pull-through.
 *
 * `replaced` is kept so two things stay possible: telling the planner what the
 * extractor had read (a divergence usually means the wrong part was picked),
 * and putting it back if the tie is cleared before anyone edits the field.
 */
interface DerivedSpec {
  /** Exactly what the pull-through wrote, so a later manual edit is detectable. */
  value: string;
  /** The extractor's value it displaced; `''` when the field was empty. */
  replaced: string;
}

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
  /** Spec fields currently sourced from the tied sheet part (see `DerivedSpec`). */
  derived: Partial<Record<SpecField, DerivedSpec>>;
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

const TH = 'px-2 py-2 text-left text-[11px] font-semibold uppercase tracking-wide text-fd-mute';
// Border/background split from the base so the low-confidence and
// sourced-from-part variants never fight the default colors on Tailwind
// specificity.
const CELL_INPUT_BASE =
  'w-full rounded-none border px-2 py-1 text-sm text-fd-ink focus:border-fd-blue focus:outline-none';
const CELL_INPUT = `${CELL_INPUT_BASE} border-fd-line bg-fd-sunken`;
const CELL_INPUT_VERIFY = `${CELL_INPUT_BASE} border-fd-amber bg-fd-amber/10`;
/** A value the tied sheet part supplied rather than the extractor. */
const CELL_INPUT_DERIVED = `${CELL_INPUT_BASE} border-fd-blue/50 bg-fd-blue/5`;

/**
 * Fixed column widths for the review grid.
 *
 * The grid is `table-fixed` over an explicit `<colgroup>` and scrolls
 * horizontally inside its container, rather than being auto-laid-out and
 * squeezed to fit. Auto layout is what made this unreadable: a `w-20` input in
 * an over-full table shrinks below its declared width, and a CNC number is the
 * field that suffers most — `04886` rendered as `0`, which is worse than
 * useless on the one value an operator keys into the machine.
 */
const COLUMN_WIDTHS = [
  '11rem', // Source (sticky) — page range / file name, confidence, warning
  '7rem', // CNC #
  '15rem', // Sheet part
  '6.5rem', // Material
  '6rem', // Thickness
  '6.5rem', // Sheet size
  '5.5rem', // Sheets/run
  '4.5rem', // Runs
  '9rem', // WC
  '3rem', // Remove
];
/** Sum of `COLUMN_WIDTHS`; the table never renders narrower than this. */
const TABLE_MIN_WIDTH = '74.5rem';

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
    derived: {},
  };
}

const workCenterLabel = (wc: WorkCenter) => wc.name || wc.code;

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

/**
 * Was this row's run count actually read off the sheet?
 *
 * `planned_runs` arrives floored at 1 by the server no matter what happened
 * upstream, so a genuine "1 run" and "neither extraction pass could find a run
 * count" are the SAME number on the wire. The per-field confidence is the only
 * thing that separates them, which is why the wizard counts these out loud
 * instead of letting 42 confident-looking 1s ride into a released work order.
 */
function runsNotRead(row: EditableRow): boolean {
  return !row.edited.planned_runs && row.field_confidence?.planned_runs === 'low';
}

/**
 * Apply (or clear) a nest's sheet-part tie, pulling the part's thickness and
 * sheet size through onto the row.
 *
 * Picking the part is the stronger statement about what will actually be cut:
 * it names a specific stock item with real dimensions, whereas thickness and
 * sheet size are an AI read of a nest report. So the derived values WIN — but
 * visibly, never silently. The row records what was displaced, the cells render
 * in the derived style, and a divergence from a non-empty extractor value gets
 * its own marker, because that divergence is usually the signal that the wrong
 * part was picked.
 *
 * The pull-through is limited to parts `isSheetLikePart` accepts. That is the
 * same predicate behind the picker's default filter, so the one case where the
 * dimension grammar could mis-read — a planner deliberately tying a nest to
 * non-sheet stock through the "show all materials" escape hatch — writes
 * nothing rather than writing angle-iron dimensions onto a sheet.
 *
 * `partId` and `part` are SEPARATE arguments on purpose. A tie can name a part
 * the material list never returned (capped list, deactivated part, failed
 * read), and those ties are the ones a re-import must carry forward — deriving
 * the id from a `Part | undefined` lookup would drop exactly them, which is the
 * silent un-tying this whole path exists to prevent. An absent record costs the
 * spec pull-through and nothing else.
 */
function withSheetPart(
  row: EditableRow,
  partId: number | null,
  part: Part | undefined,
  qtyPerRun?: string
): EditableRow {
  const next: EditableRow = {
    ...row,
    material_part_id: partId,
    qty_per_run: qtyPerRun ?? row.qty_per_run,
    derived: {},
  };

  // Untying (or retying) first restores anything the previous pull-through
  // displaced — but ONLY where the field still holds exactly what was written.
  // If the planner has typed since, their value stands: putting the extractor's
  // read back over it would be a second uncommanded write, which is the whole
  // failure mode this marking exists to prevent.
  const restore = (field: SpecField, current: string): string => {
    const previous = row.derived[field];
    return previous && current === previous.value ? previous.replaced : current;
  };
  next.thickness = restore('thickness', row.thickness);
  next.sheet_size = restore('sheet_size', row.sheet_size);

  if (partId == null || !part || !isSheetLikePart(part)) return next;

  const spec = deriveSheetSpec(part);
  if (spec.thickness) {
    next.derived.thickness = { value: spec.thickness, replaced: next.thickness };
    next.thickness = spec.thickness;
  }
  if (spec.sheetSize) {
    next.derived.sheet_size = { value: spec.sheetSize, replaced: next.sheet_size };
    next.sheet_size = spec.sheetSize;
  }
  return next;
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
  const [extraMaterialOptions, setExtraMaterialOptions] = useState<ComboBoxOption[]>([]);
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
    // Supersede any tie pre-fill still in flight from the previous session, so
    // it cannot write the last package's ties onto a freshly opened wizard.
    previewGenerationRef.current += 1;

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

  /** Loaded material parts by id, for the spec pull-through and option labels. */
  const partsById = useMemo(() => {
    const index = new Map<number, Part>();
    for (const part of materials) index.set(part.id, part);
    return index;
  }, [materials]);

  // Mirror of `partsById` readable from the un-awaited tie pre-fill, which
  // outlives the render that created it (see `prefillTiesFromExistingNests`).
  const partsByIdRef = useRef(partsById);
  partsByIdRef.current = partsById;

  // Bumped on every preview. The tie pre-fill compares against it after each
  // await so a result from a superseded preview can never write over the
  // current one's state.
  const previewGenerationRef = useRef(0);

  const hasPicker = materials.length > 0 || extraMaterialOptions.length > 0;

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
   *
   * IT RUNS UN-AWAITED, BEHIND AN ALREADY-INTERACTIVE SCREEN. The review step
   * renders the moment the preview resolves, while this is still fetching, so
   * two things have to be true and neither is automatic:
   *
   *  - It must never overwrite a pick the planner made in that window. A stale
   *    server tie replacing a deliberate choice is the same end state the whole
   *    feature guards against — importing a part nobody chose, depleting the
   *    wrong heat lot — and unlike the spec pull-through it leaves no marking
   *    and nothing to restore. Hence the `material_part_id != null` skip: the
   *    pre-fill only ever fills a row that is still empty.
   *  - It must not land on a DIFFERENT preview. Back → re-Preview while this is
   *    in flight would otherwise write the old package's part options and its
   *    "N existing ties pre-filled" chip over the new package's reset state.
   *    `generation` is the guard, checked after every await.
   */
  const prefillTiesFromExistingNests = async (previewRows: EditableRow[], generation: number) => {
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
    if (previewGenerationRef.current !== generation) return;

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
        // Never over-write a pick the planner made while this was in flight.
        if (!tie || row.material_part_id != null) return row;
        // Routed through the same helper as a manual pick, so a pre-filled tie
        // pulls its sheet spec through exactly like one the planner chooses.
        // The part record is read from a ref rather than the render-time memo:
        // this function was created by the render that handled the Preview
        // click, and /materials may only have landed after it — a stale empty
        // map would cost a pre-filled tie the spec pull-through that the very
        // same part gets when picked by hand.
        return withSheetPart(row, tie.part_id, partsByIdRef.current.get(tie.part_id), String(tie.qty_per_run ?? 1));
      })
    );
    // Keep every tied part selectable even if it is missing from the material
    // list (capped, deactivated, or a failed load). DEDUPED BY PART: a package
    // cut entirely from one sheet is the ordinary shape, not an exotic one, and
    // `tieBySource` is keyed per NEST — mapping it straight through pushed the
    // same option once per tied nest, which renders duplicate rows in the
    // picker and trips React's duplicate-key warning.
    const optionByPartId = new Map<number, ComboBoxOption>();
    for (const tie of Array.from(tieBySource.values())) {
      if (optionByPartId.has(tie.part_id)) continue;
      optionByPartId.set(tie.part_id, {
        value: String(tie.part_id),
        label: [tie.part_number, tie.part_name].filter(Boolean).join(' — ') || `Part ${tie.part_id}`,
      });
    }
    setExtraMaterialOptions(Array.from(optionByPartId.values()));
    setTiePrefillCount(tieBySource.size);
  };

  const hasInput = Boolean(file) || sourcePath.trim().length > 0;

  const handlePreview = async () => {
    if (!hasInput) {
      setError('Choose a ZIP package or a nest-report PDF (single or multi-page), or enter a folder path.');
      return;
    }
    setLoading(true);
    setError('');
    // Claim this preview before the request goes out, so an in-flight pre-fill
    // from a previous one is already superseded by the time it resolves.
    const generation = ++previewGenerationRef.current;
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
      void prefillTiesFromExistingNests(editable, generation);
    } catch (err: any) {
      setError(err?.response?.data?.detail || 'Failed to preview laser nest package.');
    } finally {
      setLoading(false);
    }
  };

  /** Edit one extracted field: applies the value and marks the field as
   *  planner-touched so its low-confidence highlight clears. A hand-typed spec
   *  also drops its "from sheet part" marking — the value is the planner's now,
   *  and the tie must not silently reclaim it. */
  const updateField = (index: number, field: LaserNestConfidenceField, value: string) => {
    setRows((prev) =>
      prev.map((row, i) => {
        if (i !== index) return row;
        const next = { ...row, [field]: value, edited: { ...row.edited, [field]: true } };
        if (field === 'thickness' || field === 'sheet_size') {
          const derived = { ...row.derived };
          delete derived[field];
          next.derived = derived;
        }
        return next;
      })
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
    const partId = value ? Number(value) : null;
    setRows((prev) =>
      prev.map((row, i) =>
        i === index ? withSheetPart(row, partId, partId != null ? partsById.get(partId) : undefined) : row
      )
    );
  };

  /** Sheets consumed per completed run on a tied row. */
  const updateRowQtyPerRun = (index: number, value: string) => {
    setRows((prev) => prev.map((row, i) => (i === index ? { ...row, qty_per_run: value } : row)));
  };

  /**
   * Package-level default: stamp the picked sheet part + qty onto EVERY row,
   * pulling its thickness / sheet size through with it.
   *
   * Deliberately an explicit action rather than a fuzzy auto-match off the
   * AI-extracted `material` text — a wrong tie depletes the wrong heat lot into
   * an as-built record. Rows stay individually editable afterwards.
   */
  const applyPackageMaterial = () => {
    if (!packageMaterialPartId) return;
    const partId = Number(packageMaterialPartId);
    const part = partsById.get(partId);
    const qty = packageQtyPerRun.trim() || '1';
    setRows((prev) => prev.map((row) => withSheetPart(row, partId, part, qty)));
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
  const runsNotReadCount = rows.filter(runsNotRead).length;
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
      size={step === 'review' ? '7xl' : 'lg'}
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
              : 'Review and correct each nest, then import. AI-extracted values are editable — verify low-confidence rows before importing. Picking a sheet part fills in that nest’s thickness and sheet size from the stock item.'}
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
            {hasPicker && (
              <div className="flex flex-wrap items-end gap-x-4 gap-y-2 rounded-none border border-fd-line bg-fd-sunken px-3 py-2">
                <span className="pb-1.5 text-[11px] font-semibold uppercase tracking-wide text-fd-mute">Sheet</span>
                <div className="min-w-[18rem] flex-1 sm:max-w-[26rem]">
                  <label htmlFor="nest-package-material-part" className="block text-xs font-medium text-fd-mute">
                    Sheet part
                  </label>
                  <div className="mt-1">
                    <SheetPartPicker
                      id="nest-package-material-part"
                      ariaLabel="Sheet part"
                      parts={materials}
                      onHandByPart={onHandByPart}
                      extraOptions={extraMaterialOptions}
                      value={packageMaterialPartId}
                      onChange={setPackageMaterialPartId}
                    />
                  </div>
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
                <p className="basis-full text-xs text-fd-faint">
                  Optional. Stamps every row below — including its thickness and sheet size — and each row stays
                  editable. Tied sheets leave inventory when that nest&apos;s operation completes, not per run.
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
              {/* The run count is the one extracted field whose "not found" is
                  invisible in the value itself — the server floors it at 1. Say
                  so plainly rather than shipping 42 confident-looking 1s. */}
              {runsNotReadCount > 0 && (
                <span className="rounded-none border border-fd-amber/40 bg-fd-amber/10 px-2 py-1 font-semibold text-fd-amber">
                  {runsNotReadCount} {runsNotReadCount === 1 ? 'run count' : 'run counts'} not read — defaulted to 1
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

            {/* Both axes scroll: vertical for the row count, horizontal because
                the grid keeps its declared column widths instead of squeezing
                them. The Source column is sticky so a row stays identifiable
                once it is scrolled sideways. */}
            <div className="max-h-[58vh] overflow-auto border border-fd-line">
              <table className="w-full table-fixed border-collapse" style={{ minWidth: TABLE_MIN_WIDTH }}>
                <colgroup>
                  {COLUMN_WIDTHS.map((width, i) => (
                    <col key={i} style={{ width }} />
                  ))}
                </colgroup>
                <thead className="sticky top-0 z-20 bg-fd-panel">
                  <tr className="border-b border-fd-line">
                    <th className={`${TH} sticky left-0 z-30 bg-fd-panel`}>Source</th>
                    <th className={TH}>CNC #</th>
                    <th className={`${TH} border-l border-fd-line`}>Sheet part</th>
                    <th className={TH}>Material</th>
                    <th className={TH}>Thickness</th>
                    <th className={TH}>Sheet size</th>
                    <th className={`${TH} border-l border-fd-line text-right`}>Sheets/run</th>
                    <th className={`${TH} text-right`}>Runs</th>
                    <th className={`${TH} border-l border-fd-line`}>WC</th>
                    <th className={TH} aria-label="Remove" />
                  </tr>
                </thead>
                <tbody>
                  {rows.length === 0 && (
                    <tr>
                      <td colSpan={10} className="px-3 py-6 text-center text-sm text-fd-mute">
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
                    const tiedPart = row.material_part_id != null ? partsById.get(row.material_part_id) : undefined;

                    // A spec cell sourced from the tied part: derived styling wins
                    // over the amber verify highlight, because the field is no
                    // longer an unverified AI read — a human named the stock it
                    // came from.
                    const specCell = (field: SpecField, needsVerify: boolean) => {
                      const derived = row.derived[field];
                      if (!derived) {
                        return {
                          className: needsVerify ? CELL_INPUT_VERIFY : CELL_INPUT,
                          title: needsVerify ? 'Low confidence — verify' : undefined,
                          diverged: false,
                        };
                      }
                      const partLabel = tiedPart ? sheetPartOptionLabel(tiedPart) : 'the tied sheet part';
                      const diverged = derived.replaced.trim() !== '' && derived.replaced.trim() !== derived.value;
                      return {
                        className: CELL_INPUT_DERIVED,
                        title: diverged
                          ? `From ${partLabel}. The nest report was read as "${derived.replaced}" — check that the right sheet part is tied.`
                          : `From ${partLabel}.`,
                        diverged,
                      };
                    };
                    const thicknessCell = specCell('thickness', verify.thickness);
                    const sheetSizeCell = specCell('sheet_size', verify.sheet_size);

                    return (
                      <tr key={row.source_file} className="border-b border-fd-line align-top">
                        <td className="sticky left-0 z-10 bg-fd-panel px-2 py-2 text-xs text-fd-mute">
                          {/* For PDF uploads the generated file name is noise — show the
                              page range and keep the file name as the tooltip. */}
                          <span className="block truncate font-medium text-fd-body" title={row.source_file}>
                            {pageRange ?? row.source_file}
                          </span>
                          {row.cnc_file_name && (
                            <span className="block truncate text-fd-faint" title={row.cnc_file_name}>
                              {row.cnc_file_name}
                            </span>
                          )}
                          {/* Confidence and the extraction warning are metadata
                              about the row, not editable fields — they live with
                              the row's identity instead of costing two columns
                              the data fields need. */}
                          {(badge || row.warning) && (
                            <span className="mt-1 inline-flex items-center gap-1">
                              {badge && (
                                <span
                                  className={`rounded-none border px-1 py-0.5 text-[10px] font-semibold uppercase ${badge.className}`}
                                  title={`Extraction confidence: ${badge.label}`}
                                >
                                  {badge.label}
                                </span>
                              )}
                              {row.warning && (
                                <span
                                  role="img"
                                  aria-label={`Warning for ${row.source_file}: ${row.warning}`}
                                  title={row.warning}
                                  className="inline-flex cursor-help text-fd-amber"
                                >
                                  <ExclamationTriangleIcon className="h-3.5 w-3.5" aria-hidden="true" />
                                </span>
                              )}
                            </span>
                          )}
                        </td>
                        <td className="px-2 py-2">
                          <input
                            type="text"
                            value={row.cnc_number}
                            onChange={(e) => updateField(index, 'cnc_number', e.target.value)}
                            className={`${verify.cnc_number ? CELL_INPUT_VERIFY : CELL_INPUT} font-mono tracking-tight`}
                            aria-label={verifyLabel(`CNC number for ${row.source_file}`, verify.cnc_number)}
                            title={verify.cnc_number ? 'Low confidence — verify' : row.cnc_number}
                          />
                        </td>
                        <td className="border-l border-fd-line px-2 py-2">
                          <SheetPartPicker
                            ariaLabel={`Sheet part for ${row.source_file}`}
                            parts={materials}
                            onHandByPart={onHandByPart}
                            extraOptions={extraMaterialOptions}
                            value={row.material_part_id != null ? String(row.material_part_id) : ''}
                            onChange={(value) => updateRowMaterialPart(index, value)}
                            disabled={!hasPicker}
                          />
                        </td>
                        <td className="px-2 py-2">
                          <input
                            type="text"
                            value={row.material}
                            onChange={(e) => updateField(index, 'material', e.target.value)}
                            className={verify.material ? CELL_INPUT_VERIFY : CELL_INPUT}
                            aria-label={verifyLabel(`Material for ${row.source_file}`, verify.material)}
                            title={verify.material ? 'Low confidence — verify' : undefined}
                          />
                        </td>
                        <td className="px-2 py-2">
                          <div className="relative">
                            <input
                              type="text"
                              value={row.thickness}
                              onChange={(e) => updateField(index, 'thickness', e.target.value)}
                              className={`${thicknessCell.className} ${row.derived.thickness ? 'pr-6' : ''}`}
                              // The verify suffix is suppressed once the value
                              // comes from the tied part, matching what the
                              // derived styling already tells a sighted user.
                              // Announcing "low confidence, verify" on a field
                              // a human sourced from named stock is the two
                              // channels contradicting each other.
                              aria-label={verifyLabel(
                                `Thickness for ${row.source_file}`,
                                verify.thickness && !row.derived.thickness
                              )}
                              title={thicknessCell.title}
                            />
                            {row.derived.thickness && (
                              <LinkIcon
                                className={`pointer-events-none absolute right-1.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 ${
                                  thicknessCell.diverged ? 'text-fd-amber' : 'text-fd-blue'
                                }`}
                                aria-hidden="true"
                              />
                            )}
                          </div>
                        </td>
                        <td className="px-2 py-2">
                          <div className="relative">
                            <input
                              type="text"
                              value={row.sheet_size}
                              onChange={(e) => updateField(index, 'sheet_size', e.target.value)}
                              className={`${sheetSizeCell.className} ${row.derived.sheet_size ? 'pr-6' : ''}`}
                              aria-label={verifyLabel(
                                `Sheet size for ${row.source_file}`,
                                verify.sheet_size && !row.derived.sheet_size
                              )}
                              title={sheetSizeCell.title}
                            />
                            {row.derived.sheet_size && (
                              <LinkIcon
                                className={`pointer-events-none absolute right-1.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 ${
                                  sheetSizeCell.diverged ? 'text-fd-amber' : 'text-fd-blue'
                                }`}
                                aria-hidden="true"
                              />
                            )}
                          </div>
                        </td>
                        <td className="border-l border-fd-line px-2 py-2">
                          <input
                            type="number"
                            min={0}
                            step="any"
                            value={row.qty_per_run}
                            onChange={(e) => updateRowQtyPerRun(index, e.target.value)}
                            disabled={row.material_part_id == null}
                            className={`${CELL_INPUT} text-right tabular-nums disabled:opacity-50`}
                            aria-label={`Sheets per run for ${row.source_file}`}
                          />
                        </td>
                        <td className="px-2 py-2">
                          <input
                            type="number"
                            min={1}
                            step={1}
                            value={row.planned_runs}
                            onChange={(e) => updateField(index, 'planned_runs', e.target.value)}
                            className={`${verify.planned_runs ? CELL_INPUT_VERIFY : CELL_INPUT} text-right tabular-nums`}
                            aria-label={verifyLabel(`Runs for ${row.source_file}`, verify.planned_runs)}
                            title={
                              runsNotRead(row)
                                ? 'No run count was found on this sheet — defaulted to 1. Verify against the nest report.'
                                : verify.planned_runs
                                  ? 'Low confidence — verify'
                                  : undefined
                            }
                          />
                        </td>
                        <td className="border-l border-fd-line px-2 py-2">
                          <select
                            value={row.work_center_id != null ? String(row.work_center_id) : ''}
                            onChange={(e) => updateRowWorkCenter(index, e.target.value)}
                            className={CELL_INPUT}
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
                        <td className="px-2 py-2 text-right">
                          <button
                            type="button"
                            onClick={() => removeRow(index)}
                            className="text-fd-mute hover:text-fd-red"
                            aria-label={`Remove ${row.source_file}`}
                            title={`Remove ${row.source_file}`}
                          >
                            <TrashIcon className="h-4 w-4" aria-hidden="true" />
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
