'use client';
import React, { useState, useRef, useEffect, useMemo, useId } from 'react';
import { useUnsavedChanges } from '../../hooks/useUnsavedChanges';
import {
  Crosshair,
  Layers3,
  Upload,
  ArrowDownToLine,
  Play,
  Settings2,
  ChevronRight,
  Plus,
  Trash2,
  RotateCw,
  FileJson,
  FolderOpen,
  Check,
  Info,
  CircleHelp,
  ZoomIn,
  ZoomOut,
  Maximize,
  FileText,
  ShoppingCart,
  ArrowRight,
  Ruler,
} from 'lucide-react';
import { Tabs, TabsList, TabsTrigger, TabsContent } from './ui/tabs';
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from './ui/select';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from './ui/dialog';
import { Table, TableHeader, TableBody, TableHead, TableRow, TableCell } from './ui/table';
import { Progress, ProgressLabel, ProgressValue } from './ui/progress';
import { importDXFBatch, MAX_DXF_FILES, type ImportProgress, type ImportResult } from './lib/dxf-batch';
import { Switch } from './ui/switch';
import { Input } from './ui/input';
import { useToast } from '../../components/ui/Toast';
import {
  bounds,
  normalizeLoops,
  rect,
  svgPath,
  transformLoops,
  transformReferencePaths,
  validatePart,
  type Part,
} from './lib/nesting';
import { inToMm, mmToIn, formatIn, parseInches, LB_PER_KG } from './lib/units';
import {
  oversizeParts,
  editSheetOption,
  type Quote,
  type Comparison,
  type SheetOption,
  type OptionResult,
} from './lib/quoting';
import {
  addImportedParts,
  createBlankProject,
  materialThicknessKey,
  projectFromFile,
  projectToFile,
  validateProject,
  type QuoteProject,
} from './lib/quote-project';
import { autoQuotingSpacing } from './lib/spacing';
import { compareSheetsInWorker } from './lib/nesting-worker-client';
import DXFAssignments, { type DXFFileAssignment } from './DXFAssignments';
import MaterialSourcePanel from './MaterialSourcePanel';
import useNestingCatalog from './useNestingCatalog';
import { catalogFamily, clearCatalogPricing, materialGroupLabel, type MaterialBinding } from './lib/material-binding';
import { buildRunManifest } from './lib/run-manifest';
import PartOrientationControls, { SheetGrainControl, orientationSummary, sheetGrainLabel } from './OrientationControls';
import { orientationExplanation } from './lib/orientation';
import LeftoverReview, { LeftoverOverlay, leftoverPath } from './LeftoverReview';
import TeamDrafts from './TeamDrafts';

type CachedComparison = { comparison: Comparison; signature: string };
const legacyFootprintNotice =
  'This saved estimate contains legacy rectangular DXF footprints. Remove those parts and re-import their DXFs to nest actual contours.';
const emptyComparison: Comparison = {
  results: [],
  recommendedId: null,
  reason: 'Add parts, then compare sheets to calculate an order.',
  requested: 0,
};
const referencePath = (points: { x: number; y: number }[]) =>
  points.map((point, index) => `${index ? 'L' : 'M'}${point.x} ${point.y}`).join(' ');
const colors = [
  { fill: '#1e40af', stroke: '#93c5fd' },
  { fill: '#334155', stroke: '#cbd5e1' },
  { fill: '#373067', stroke: '#c4b5fd' },
  { fill: '#513047', stroke: '#f9a8d4' },
];
const fmt = (n: number, d = 1) => n.toLocaleString('en-US', { maximumFractionDigits: d });
const money = (n: number) => n.toLocaleString('en-US', { style: 'currency', currency: 'USD' });
const squareFeet = (mm2: number) => mm2 / 92903.04;
const sheetLabel = (o: SheetOption) => `${formatIn(o.height)} × ${formatIn(o.width)} in`;
const safeName = (s: string) =>
  s
    .replace(/[^a-z0-9-_ ]/gi, '')
    .trim()
    .slice(0, 70) || 'material-estimate';
function download(content: string, name: string, type: string) {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function Picker({
  value,
  onChange,
  items,
  label,
  id,
}: {
  value: string;
  onChange: (v: string) => void;
  items: string[];
  label: string;
  id?: string;
}) {
  return (
    <Select
      value={value}
      onValueChange={v => {
        if (v !== null) onChange(v);
      }}
    >
      <SelectTrigger id={id} className="picker" aria-label={label}>
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {items.map(i => (
          <SelectItem value={i} key={i}>
            {i}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
function NumberField({
  label,
  value,
  onChange,
  unit,
  min = 0,
  max = 20000,
  step = 'any',
}: {
  label: string;
  value: number;
  onChange: (v: number) => void;
  unit?: string;
  min?: number;
  max?: number;
  step?: string;
}) {
  const isLength = unit === 'in';
  const display = () => (Number.isFinite(value) ? String(Number((isLength ? mmToIn(value) : value).toFixed(6))) : '');
  const [draft, setDraft] = useState(display);
  const focused = useRef(false);
  useEffect(() => {
    if (!focused.current) setDraft(display());
  }, [value, isLength]);
  return (
    <label className="field-label">
      <span>
        {label}
        {unit && <small>{unit}</small>}
      </span>
      <Input
        type={isLength ? 'text' : 'number'}
        inputMode="decimal"
        value={draft}
        min={isLength ? mmToIn(min) : min}
        max={isLength ? mmToIn(max) : max}
        step={step}
        aria-invalid={!Number.isFinite(value)}
        placeholder={isLength ? 'Decimal or fraction' : undefined}
        onFocus={() => {
          focused.current = true;
        }}
        onBlur={() => {
          focused.current = false;
          if (Number.isFinite(value)) setDraft(display());
        }}
        onChange={e => {
          const text = e.target.value;
          setDraft(text);
          const n = isLength ? parseInches(text) : text === '' ? NaN : Number(text);
          onChange(isLength ? inToMm(n) : n);
        }}
      />
    </label>
  );
}
export default function NestingWorkspace({
  initialQuote,
  companyId,
  estimatorId,
  canSaveDrafts = false,
}: {
  initialQuote?: Quote;
  companyId?: number;
  estimatorId?: number;
  canSaveDrafts?: boolean;
}) {
  const fieldId = useId();
  const [documentEpoch, setDocumentEpoch] = useState(0);
  const catalog = useNestingCatalog(companyId);
  const [verifiedPricingHashes, setVerifiedPricingHashes] = useState<Set<string>>(() => new Set());
  const { showToast } = useToast();
  const toast = useMemo(
    () => ({
      success: (message: string, _options?: { duration?: number }) => showToast('success', message),
      warning: (message: string) => showToast('warning', message),
      error: (message: string, _options?: { duration?: number }) => showToast('error', message),
    }),
    [showToast]
  );
  const [project, setProject] = useState<QuoteProject>(() => createBlankProject(initialQuote)),
    [tab, setTab] = useState('nest');
  const quote = project.groups.find(group => group.id === project.activeGroupId)!.quote;
  const setQuote = (action: React.SetStateAction<Quote>) =>
    setProject(current => ({
      ...current,
      groups: current.groups.map(group =>
        group.id === current.activeGroupId
          ? { ...group, quote: typeof action === 'function' ? action(group.quote) : action }
          : group
      ),
    }));
  useEffect(() => {
    if (!catalog.items.length && !catalog.complete) return;
    setProject(current => {
      let changed = false;
      const groups = current.groups.map(group => {
        const binding = group.quote.materialBinding;
        const source = binding && catalog.items.find(item => item.id === binding.catalog.id);
        if (
          binding?.acknowledgement &&
          ((source && source.catalog_hash !== binding.catalog.catalog_hash) || (catalog.complete && !source))
        ) {
          changed = true;
          return { ...group, quote: clearCatalogPricing(group.quote) };
        }
        return group;
      });
      return changed ? { ...current, groups } : current;
    });
  }, [catalog.items, catalog.complete]);
  const [savedSignature, setSavedSignature] = useState(() => JSON.stringify(project));
  const [snapshots, setSnapshots] = useState<Record<string, CachedComparison>>({});
  const [compareProgress, setCompareProgress] = useState({ completed: 0, total: 0, name: '' });
  const compareController = useRef<AbortController | null>(null);
  const comparingRef = useRef(false);
  const [previewId, setPreviewId] = useState<string | null>(null),
    [sheet, setSheet] = useState(0),
    [selected, setSelected] = useState<string | null>(null),
    [zoom, setZoom] = useState(1),
    [busy, setBusy] = useState(false),
    [labels, setLabels] = useState(true),
    [showLeftovers, setShowLeftovers] = useState(true),
    [selectedLeftover, setSelectedLeftover] = useState<string | null>(null),
    [unitless, setUnitless] = useState('in'),
    [showAdd, setShowAdd] = useState(false),
    [help, setHelp] = useState(false);
  const [newPart, setNewPart] = useState({
    name: 'New plate',
    shape: 'Rectangle',
    width: inToMm(12),
    height: inToMm(8),
    quantity: 1,
  });
  const [importing, setImporting] = useState(false);
  const [pendingFiles, setPendingFiles] = useState<File[] | null>(null);
  const [importOpen, setImportOpen] = useState(false);
  const [importProgress, setImportProgress] = useState<ImportProgress>({
    completed: 0,
    total: 0,
    name: '',
  });
  const [importResults, setImportResults] = useState<ImportResult[]>([]);
  const [dragging, setDragging] = useState(false);
  const importController = useRef<AbortController | null>(null);
  const importingRef = useRef(false);
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      importController.current?.abort();
      compareController.current?.abort();
    };
  }, []);
  const importRef = useRef<HTMLInputElement>(null),
    loadRef = useRef<HTMLInputElement>(null);
  useUnsavedChanges(JSON.stringify(project) !== savedSignature);
  const snapshot = snapshots[project.activeGroupId];
  const signature = JSON.stringify(quote),
    stale = !!quote.parts.length && signature !== snapshot?.signature,
    comparison = quote.parts.length ? (snapshot?.comparison ?? emptyComparison) : emptyComparison;
  const reviewReady =
    project.groups.some(group => group.quote.parts.length > 0) &&
    project.groups.every(
      group => !group.quote.parts.length || snapshots[group.id]?.signature === JSON.stringify(group.quote)
    );
  const active =
    comparison.results.find(r => r.option.id === previewId) ??
    comparison.results.find(r => r.option.id === comparison.recommendedId) ??
    comparison.results[0];
  const nest = active?.nest,
    stock = active?.option,
    visible = stale ? [] : (nest?.placements.filter(p => p.sheet === sheet) ?? []);
  const leftoverSheet = stale ? undefined : active?.leftovers?.sheets.find(result => result.sheet === sheet);
  useEffect(() => setSelectedLeftover(null), [signature, active?.option.id, active?.leftovers, sheet]);
  const requested = quote.parts.reduce((a, p) => a + p.quantity, 0),
    enabled = quote.options.filter(o => o.enabled).length;
  const error = useMemo(() => {
    try {
      validateProject(project);
      if (project.groups.some(group => group.quote.parts.some(part => part.importMode === 'drawing-bounds')))
        throw new Error(legacyFootprintNotice);
      return '';
    } catch (e) {
      return (e as Error).message;
    }
  }, [project]);
  const density = quote.material === 'Carbon steel' ? 7850 : quote.material === 'Stainless steel' ? 7930 : 2700;
  const catalogDensity = quote.materialBinding?.catalog.density_lb_per_cubic_inch;
  const mass = active
    ? quote.materialBinding
      ? catalogDensity && Number(catalogDensity) > 0
        ? ((active.area * quote.thickness) / 25.4 ** 3) * Number(catalogDensity)
        : null
      : (active.area * quote.thickness * density * LB_PER_KG) / 1e9
    : 0;
  const update = (patch: Partial<Quote>) => {
    if (patch.name !== undefined)
      setProject(current => ({
        ...current,
        name: patch.name!,
        groups: current.groups.map(group => ({ ...group, quote: { ...group.quote, name: patch.name! } })),
      }));
    else
      setQuote(q => {
        const changed = { ...q, ...patch };
        return patch.options && !Object.prototype.hasOwnProperty.call(patch, 'materialBinding')
          ? clearCatalogPricing(changed)
          : changed;
      });
  };
  const changeMaterial = (patch: Partial<Quote>) => {
    const changed = clearCatalogPricing({
      ...quote,
      ...patch,
      options: quote.options.map(option => ({ ...option, price: null })),
    });
    if (
      patch.material !== undefined &&
      patch.material !== quote.material &&
      !Object.prototype.hasOwnProperty.call(patch, 'materialBinding')
    )
      delete changed.materialBinding;
    if (changed.spacingMode === 'auto' && Number.isFinite(changed.thickness) && changed.thickness > 0)
      Object.assign(changed, autoQuotingSpacing(changed.thickness));
    if (!Number.isFinite(changed.thickness) || changed.thickness <= 0 || changed.thickness > 100)
      return setQuote(changed);
    try {
      const key = materialThicknessKey(changed.material, changed.thickness, changed.materialBinding);
      const target = project.groups.find(
        group =>
          group.id !== project.activeGroupId &&
          materialThicknessKey(group.quote.material, group.quote.thickness, group.quote.materialBinding) === key
      );
      const next = target
        ? {
            ...project,
            activeGroupId: target.id,
            groups: project.groups
              .filter(group => group.id !== project.activeGroupId)
              .map(group =>
                group.id === target.id
                  ? {
                      ...group,
                      quote: clearCatalogPricing({
                        ...group.quote,
                        parts: [...group.quote.parts, ...changed.parts],
                        options: group.quote.options.map(option => ({ ...option, price: null })),
                      }),
                    }
                  : group
              ),
          }
        : {
            ...project,
            groups: project.groups.map(group =>
              group.id === project.activeGroupId ? { ...group, quote: changed } : group
            ),
          };
      validateProject(next);
      setProject(next);
      if (target)
        toast.success('Parts combined with the matching material group. Review spacing and re-enter sheet prices.');
    } catch (e) {
      toast.error((e as Error).message);
    }
  };
  const changeOption = (id: string, patch: Partial<SheetOption>) =>
    setQuote(q => ({
      ...q,
      ...(patch.width !== undefined || patch.height !== undefined ? clearCatalogPricing(q) : q),
      options: q.options
        .map(o => (o.id === id ? editSheetOption(o, patch) : o))
        .map(o =>
          q.materialBinding && (patch.width !== undefined || patch.height !== undefined) ? { ...o, price: null } : o
        ),
    }));
  const stateRef = useRef({ project, quote, comparison, stale, snapshots });
  stateRef.current = { project, quote, comparison, stale, snapshots };
  useEffect(() => {
    setPreviewId(null);
    setSheet(0);
    setZoom(1);
    setSelected(null);
  }, [project.activeGroupId]);
  async function calculate() {
    if (comparingRef.current || importingRef.current) return null;
    const controller = new AbortController();
    compareController.current = controller;
    comparingRef.current = true;
    setBusy(true);
    try {
      const current = stateRef.current.project;
      validateProject(current);
      if (current.groups.some(group => group.quote.parts.some(part => part.importMode === 'drawing-bounds')))
        throw new Error(legacyFootprintNotice);
      const groups = current.groups.filter(group => group.quote.parts.length);
      const results: { id: string; material: string; thickness: number; comparison: Comparison }[] = [];
      for (let index = 0; index < groups.length; index++) {
        const group = groups[index];
        setCompareProgress({
          completed: index,
          total: groups.length,
          name: `${materialGroupLabel(group.quote)} · ${formatIn(group.quote.thickness)} in`,
        });
        const next = await compareSheetsInWorker(group.quote, { signal: controller.signal });
        if (!mountedRef.current || controller.signal.aborted) return null;
        setSnapshots(previous => ({
          ...previous,
          [group.id]: { comparison: next, signature: JSON.stringify(group.quote) },
        }));
        results.push({
          id: group.id,
          material: group.quote.material,
          thickness: group.quote.thickness,
          comparison: next,
        });
        setCompareProgress({
          completed: index + 1,
          total: groups.length,
          name: `${materialGroupLabel(group.quote)} · ${formatIn(group.quote.thickness)} in`,
        });
      }
      setPreviewId(null);
      setSheet(0);
      setZoom(1);
      toast.success(`${groups.length} material groups compared. Review each sheet order.`);
      return results;
    } catch (e) {
      if (!controller.signal.aborted && mountedRef.current) toast.error((e as Error).message);
      return null;
    } finally {
      comparingRef.current = false;
      compareController.current = null;
      if (mountedRef.current) setBusy(false);
    }
  }
  function compare() {
    void calculate();
  }
  function preview(result: OptionResult) {
    setPreviewId(result.option.id);
    setSheet(0);
    setZoom(1);
  }
  function importFiles(files: FileList | null) {
    if (!files?.length || importingRef.current || comparingRef.current) return;
    const selectedFiles = Array.from(files);
    if (importRef.current) importRef.current.value = '';
    if (selectedFiles.length > MAX_DXF_FILES) {
      toast.error('Select up to 100 DXF files at a time. No files were imported.', { duration: 9000 });
      return;
    }
    setPendingFiles(selectedFiles);
  }
  async function confirmImport(selectedFiles: File[], assignments: DXFFileAssignment[]) {
    if (importingRef.current || comparingRef.current) return;
    setPendingFiles(null);
    const existing = stateRef.current.project.groups.flatMap(group => group.quote.parts);
    importingRef.current = true;
    const controller = new AbortController();
    importController.current = controller;
    setImporting(true);
    setImportResults([]);
    setImportProgress({
      completed: 0,
      total: selectedFiles.length,
      name: selectedFiles[0].name,
    });
    setImportOpen(true);
    try {
      const result = await importDXFBatch(selectedFiles, existing, {
        units: unitless as 'in' | 'mm',
        unitsByFile: assignments.map(row => row.units ?? (unitless as 'in' | 'mm')),
        signal: controller.signal,
        onProgress: progress => {
          if (mountedRef.current) setImportProgress(progress);
        },
      });
      if (!mountedRef.current) return;
      const rows = result.results.flatMap((file, index) =>
        file.status === 'imported' && file.partIds?.length ? [{ ...assignments[index], partIds: file.partIds }] : []
      );
      const next = addImportedParts(stateRef.current.project, rows, result.parts);
      setProject(next);
      setImportResults(result.results);
      setSelected(result.parts[0]?.id ?? null);
      const successful = result.results.filter(file => file.status === 'imported').length;
      if (successful && result.results.some(file => file.warnings?.length || file.status === 'skipped'))
        toast.warning(`${successful} files imported. Review the import notes and part dimensions.`);
      else if (successful) toast.success(`${successful} files imported · ${result.parts.length} designs added.`);
      else toast.error('No files were imported. Review the file results.');
    } catch (error) {
      if (!mountedRef.current) return;
      toast.error((error as Error).message, { duration: 9000 });
      setImportOpen(false);
    } finally {
      importingRef.current = false;
      importController.current = null;
      if (mountedRef.current) setImporting(false);
    }
  }
  function addPart() {
    try {
      if (
        !Number.isFinite(newPart.width) ||
        newPart.width <= 0 ||
        !Number.isFinite(newPart.height) ||
        newPart.height <= 0
      )
        throw new Error('Enter positive dimensions.');
      const p: Part = {
        id: crypto.randomUUID(),
        name: newPart.name,
        loops: normalizeLoops([
          newPart.shape === 'Circle'
            ? {
                type: 'circle',
                cx: newPart.width / 2,
                cy: newPart.width / 2,
                r: newPart.width / 2,
              }
            : rect(newPart.width, newPart.height),
        ]),
        quantity: newPart.quantity,
        rotate: true,
        color: quote.parts.length % 4,
      };
      validatePart(p);
      const next = {
        ...project,
        groups: project.groups.map(group =>
          group.id === project.activeGroupId ? { ...group, quote: { ...quote, parts: [...quote.parts, p] } } : group
        ),
      };
      validateProject(next);
      setProject(next);
      setShowAdd(false);
      toast.success('Part added. Compare sheets to update the estimate.');
    } catch (e) {
      toast.error((e as Error).message);
    }
  }
  function save() {
    try {
      download(
        JSON.stringify(projectToFile(project), null, 2),
        safeName(project.name) + '.estimate.json',
        'application/json'
      );
      setSavedSignature(JSON.stringify(project));
      toast.success('All material groups saved in inches.');
    } catch (e) {
      toast.error((e as Error).message);
    }
  }
  async function load(file?: File) {
    if (!file) return;
    try {
      if (file.size > 5_000_000) throw new Error('Estimate limit: 5 MB.');
      const stored = projectFromFile(JSON.parse(await file.text()));
      const loaded = {
        ...stored,
        groups: stored.groups.map(group => ({ ...group, quote: clearCatalogPricing(group.quote) })),
      };
      if (!mountedRef.current) return;
      if (importingRef.current || comparingRef.current)
        throw new Error('Finish the current operation before opening another estimate.');
      setDocumentEpoch(value => value + 1);
      applyOpenedProject(loaded);
      toast.success(
        loaded.groups.some(group => group.quote.materialBinding)
          ? 'Estimate loaded. Refresh and review ERP pricing before using saved catalog costs.'
          : 'Estimate loaded. Compare sheets to calculate requirements.'
      );
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      if (loadRef.current) loadRef.current.value = '';
    }
  }
  function applyOpenedProject(loaded: QuoteProject) {
    if (importingRef.current || comparingRef.current)
      throw new Error('Finish the current operation before opening another estimate.');
    setProject(loaded);
    setVerifiedPricingHashes(new Set());
    setSnapshots({});
    setPreviewId(null);
    setSheet(0);
    setSavedSignature(JSON.stringify(loaded));
    setSelected(null);
    setImportResults([]);
  }
  async function exportReviewRecord() {
    try {
      const signature = JSON.stringify(project);
      const record = await buildRunManifest(project, snapshots, {
        companyId: companyId ?? null,
        estimatorId: estimatorId ?? null,
      });
      if (!mountedRef.current || signature !== JSON.stringify(stateRef.current.project))
        throw new Error('The estimate changed during export. Compare the current inputs and export again.');
      download(JSON.stringify(record, null, 2), safeName(project.name) + '-draft-review.json', 'application/json');
      toast.success('Draft review record downloaded. This is not a server-approved audit or material reservation.');
    } catch (e) {
      toast.error((e as Error).message);
    }
  }
  function exportSummary() {
    if (stale || !active?.complete || !nest) return;
    const csv = (v: unknown) => {
      const text = String(v ?? '');
      const safe = typeof v === 'string' && /^[=+@\-\t\r]/.test(text) ? "'" + text : text;
      return '"' + safe.replaceAll('"', '""') + '"';
    };
    const rows = [
      ['MATERIAL REQUIREMENT ESTIMATE'],
      ['Scope', 'Quote layout — not an NC program'],
      ['Job', quote.name],
      ['Material', materialGroupLabel(quote)],
      ['Material family', quote.material],
      ['ERP catalog ID', quote.materialBinding?.catalog.id ?? 'Not selected'],
      ['Price source', quote.materialBinding?.priceBasis ?? 'Manual estimate'],
      ['Source snapshot', quote.materialBinding?.resolution?.content_hash ?? 'None'],
      [
        'Pricing review',
        quote.materialBinding
          ? 'Estimator-assumed USD; catalog metadata remains unresolved; not approved'
          : 'Manually entered USD estimate',
      ],
      ['Selection', active.option.id === comparison.recommendedId ? 'Recommended option' : 'Previewed option'],
      ['Comparison basis', comparison.reason],
      ['Thickness in', mmToIn(quote.thickness)],
      ['Selected stock width in', mmToIn(active.option.height)],
      ['Selected stock length in', mmToIn(active.option.width)],
      ['Sheet grain', sheetGrainLabel(quote.grainAxis)],
      ['Sheets to order', nest.sheets],
      ['Required parts', requested],
      ['Placed parts', nest.placements.length],
      ['Purchased area ft2', squareFeet(active.area)],
      ['Net part area ft2', squareFeet(nest.area)],
      ['Unused area ft2', squareFeet(active.area - nest.area)],
      ['Utilization %', nest.utilization],
      ['Approx stock weight lb', mass ?? 'Unavailable: density not recorded'],
      ['Price per sheet USD', active.option.price ?? 'Not entered'],
      ['Estimated material total USD', active.cost === null ? 'Not entered' : active.cost.toFixed(2)],
      ['Credited remnant value USD', 0],
      [
        'Leftover review status',
        active.leftovers ? 'Predicted geometry; physical review required' : (active.leftoverError ?? 'Not calculated'),
      ],
      [
        'Basis',
        'Actual-contour estimating layout; one material, thickness and stock size per order option. Holes are not used for part placement. Freight, tax, labor and consumables excluded.',
      ],
      [],
      ['Compared sheet size', 'Fits all parts', 'Sheets', 'Purchased ft2', 'Utilization %', 'Material cost USD'],
      ...comparison.results.map(r => [
        sheetLabel(r.option),
        r.complete ? 'Yes' : 'No',
        r.nest?.sheets ?? '',
        r.complete ? squareFeet(r.area) : '',
        r.complete ? r.nest?.utilization : '',
        r.complete && r.cost !== null ? r.cost.toFixed(2) : '',
      ]),
      [],
      ['Part', 'Quantity', 'Width in', 'Height in', 'Rotation allowed', 'Part grain axis', 'Geometry basis'],
      ...quote.parts.map(p => {
        const b = bounds(p.loops[0]);
        return [
          p.name,
          p.quantity,
          mmToIn(b.width),
          mmToIn(b.height),
          orientationSummary(p, quote),
          p.grainAxis ?? 'No grain requirement',
          p.importMode === 'drawing-bounds' ? 'Legacy footprint; re-import DXF before nesting' : 'Closed contours',
        ];
      }),
      [],
      [
        'Sheet',
        'Gross area in2',
        'Edge margin area in2',
        'Nominal part area in2',
        'Reserved cutout area in2',
        'Clearance and protection area in2',
        'Potential leftover area in2',
        'Connected regions',
        'Credited value USD',
      ],
      ...(active.leftovers?.sheets.map(result => [
        result.sheet + 1,
        result.grossArea / 25.4 ** 2,
        result.edgeMarginArea / 25.4 ** 2,
        result.nominalPartArea / 25.4 ** 2,
        result.reservedCutoutArea / 25.4 ** 2,
        result.clearanceAndProtectionArea / 25.4 ** 2,
        result.remainingArea / 25.4 ** 2,
        result.regions.length,
        0,
      ]) ?? []),
    ];
    download(
      rows.map(row => row.map(csv).join(',')).join('\n'),
      safeName(quote.name) + '-material-requirements.csv',
      'text/csv'
    );
    toast.success('Material requirement summary downloaded.');
  }
  function exportPreview() {
    if (stale || !stock || !nest) return;
    const remaining =
      showLeftovers && leftoverSheet
        ? leftoverSheet.regions
            .map(
              region =>
                `<path d="${leftoverPath(region)}" fill="#fef3c7" fill-rule="evenodd" stroke="#d97706" stroke-width="0.5"><title>Potential leftover — review required, no value credited</title></path>`
            )
            .join('')
        : '';
    const paths = visible
      .map(pl => {
        const part = quote.parts.find(p => p.id === pl.partId)!;
        const outline = `<path d="${svgPath(transformLoops(part, pl))}" fill="#dbeafe" fill-rule="evenodd" stroke="#1d4ed8" stroke-width="1"/>`;
        const references = transformReferencePaths(part, pl)
          .map(path => `<path d="${referencePath(path)}" fill="none" stroke="#b45309" stroke-width="0.4"/>`)
          .join('');
        return outline + references;
      })
      .join('');
    download(
      `<svg xmlns="http://www.w3.org/2000/svg" width="${mmToIn(stock.width)}in" height="${mmToIn(stock.height)}in" viewBox="0 0 ${stock.width} ${stock.height}"><title>QUOTE LAYOUT — NOT AN NC PROGRAM</title><desc>Sheet grain: ${sheetGrainLabel(quote.grainAxis)}. Permitted part orientations are recorded in the material summary and draft review export. Mirroring is prohibited. Amber regions are predicted leftovers requiring physical review; no value is credited.</desc><rect width="100%" height="100%" fill="white"/><g transform="translate(0 ${stock.height}) scale(1 -1)">${remaining}${paths}</g></svg>`,
      safeName(quote.name) + `-sheet-${sheet + 1}-preview.svg`,
      'image/svg+xml'
    );
  }
  const actionsRef = useRef(calculate);
  actionsRef.current = calculate;
  useEffect(() => {
    type Context = {
      registerTool: (tool: unknown, options: { signal: AbortSignal }) => void | Promise<void>;
    };
    const context = (document as Document & { modelContext?: Context }).modelContext;
    if (!context) return;
    const lifecycle = new AbortController();
    const schema = {
      type: 'object',
      properties: {},
      additionalProperties: false,
    };
    const check = (p: unknown) => {
      if (!p || typeof p !== 'object' || Object.keys(p).length) throw new Error('No parameters accepted.');
    };
    for (const tool of [
      {
        name: 'read_material_estimate',
        description:
          'Read all material groups, parts and enabled sheet sizes in inches, plus the active calculation state.',
        inputSchema: schema,
        annotations: { readOnlyHint: true, untrustedContentHint: true },
        execute: (p: unknown) => {
          check(p);
          const s = stateRef.current;
          return {
            estimate: projectToFile(s.project),
            stale: s.stale,
            recommendedId: s.comparison.recommendedId,
          };
        },
      },
      {
        name: 'compare_sheet_sizes',
        description:
          'Compare enabled sheet sizes separately for every material and thickness group; update the estimated sheet orders. Does not order stock.',
        inputSchema: schema,
        annotations: { readOnlyHint: false, untrustedContentHint: true },
        execute: async (p: unknown) => {
          check(p);
          const result = await actionsRef.current();
          if (!result) throw new Error('Check estimate inputs.');
          await new Promise<void>(r => requestAnimationFrame(() => r()));
          return {
            groups: result.map(group => ({
              id: group.id,
              material: group.material,
              thicknessInches: mmToIn(group.thickness),
              recommendedId: group.comparison.recommendedId,
              reason: group.comparison.reason,
              options: group.comparison.results.map(r => ({
                size: sheetLabel(r.option),
                sheets: r.nest?.sheets ?? null,
                complete: r.complete,
                purchasedSquareFeet: r.complete ? squareFeet(r.area) : null,
                materialCostUSD: r.complete ? r.cost : null,
              })),
            })),
          };
        },
      },
    ]) {
      try {
        Promise.resolve(context.registerTool(tool, { signal: lifecycle.signal })).catch(() => {});
      } catch {}
    }
    return () => lifecycle.abort();
  }, []);
  return (
    <section className="app-shell quote-app" aria-label="Werco Nest material estimator">
      <input
        ref={importRef}
        hidden
        type="file"
        accept=".dxf"
        multiple
        onChange={e => void importFiles(e.target.files)}
      />
      <input ref={loadRef} hidden type="file" accept=".json" onChange={e => void load(e.target.files?.[0])} />
      <header className="topbar">
        <div className="brand">
          <img
            className="werco-logo"
            src="/nest-assets/werco-logo.webp"
            width="112"
            height="70"
            alt="Werco Manufacturing"
          />
          <div className="brand-name">
            <span>
              WERCO <span className="brand-light">NEST</span>
            </span>
            <small>MATERIAL ESTIMATOR</small>
          </div>
        </div>
        <div className="top-actions">
          <button className="icon-button" aria-label="How to estimate materials" onClick={() => setHelp(true)}>
            <CircleHelp size={19} />
          </button>
          <div className="machine-pill">
            <Ruler size={14} /> IMPERIAL <b>INCHES</b>
          </div>
        </div>
      </header>
      {busy && (
        <div className="compare-progress" role="status">
          <span>
            Comparing group {Math.min(compareProgress.completed + 1, compareProgress.total)} of {compareProgress.total}{' '}
            · {compareProgress.name}
          </span>
          <button className="secondary compact" onClick={() => compareController.current?.abort()}>
            Cancel comparison
          </button>
        </div>
      )}
      <fieldset className="workspace-controls" disabled={importing || busy}>
        <div className="material-groups">
          <label className="field-label" htmlFor={fieldId + '-group'}>
            <span>Material &amp; thickness group</span>
            <select
              id={fieldId + '-group'}
              className="assignment-select"
              value={project.activeGroupId}
              onChange={event => setProject(current => ({ ...current, activeGroupId: event.target.value }))}
            >
              {project.groups.map(group => (
                <option key={group.id} value={group.id}>
                  {materialGroupLabel(group.quote)} · {formatIn(group.quote.thickness)} in ·{' '}
                  {group.quote.parts.reduce((count, part) => count + part.quantity, 0)} parts
                </option>
              ))}
            </select>
          </label>
          <div className="group-orders" aria-label="Sheet orders by material and thickness">
            {project.groups.map(group => {
              const cached = snapshots[group.id];
              const current = cached?.signature === JSON.stringify(group.quote);
              const result = current
                ? cached.comparison.results.find(option => option.option.id === cached.comparison.recommendedId)
                : undefined;
              const count = group.quote.parts.reduce((total, part) => total + part.quantity, 0);
              return (
                <button
                  key={group.id}
                  className={'group-order' + (group.id === project.activeGroupId ? ' selected' : '')}
                  aria-pressed={group.id === project.activeGroupId}
                  onClick={() => setProject(previous => ({ ...previous, activeGroupId: group.id }))}
                >
                  <strong>
                    {materialGroupLabel(group.quote)} · {formatIn(group.quote.thickness)} in
                  </strong>
                  <span>
                    {count} parts ·{' '}
                    {result?.complete && result.nest
                      ? `${result.nest.sheets} × ${sheetLabel(result.option)}`
                      : count
                        ? current
                          ? 'Review stock options'
                          : 'Compare to calculate'
                        : 'Empty group'}
                  </span>
                  {result?.complete && result.cost !== null && <small>{money(result.cost)}</small>}
                </button>
              );
            })}
          </div>
        </div>
        <Tabs value={tab} onValueChange={v => setTab(v as string)}>
          <div className="nav-row">
            <TabsList variant="line">
              <TabsTrigger value="nest">Quote workspace</TabsTrigger>
              <TabsTrigger value="stock">Stock sizes & prices</TabsTrigger>
              <TabsTrigger value="about">How estimates work</TabsTrigger>
            </TabsList>
            <span className="subtle">Actual contours · separate material sheet orders</span>
          </div>
          <TabsContent value="nest">
            <h1 className="sr-only">Sheet material quoting workspace</h1>
            <div className="job-heading">
              <div>
                <div className="eyebrow">NESTING FOR QUOTES / CURRENT ESTIMATE</div>
                <Input
                  aria-label="Estimate name"
                  className="job-name"
                  value={quote.name}
                  maxLength={199}
                  onChange={e => update({ name: e.target.value })}
                />
                <p className="job-subtitle">
                  {materialGroupLabel(quote)}
                  <span>·</span>
                  {formatIn(quote.thickness)} in<span>·</span>
                  {requested} parts
                </p>
              </div>
              <div className="heading-actions">
                <button
                  className="secondary compact"
                  onClick={() => {
                    const next = createBlankProject();
                    setDocumentEpoch(value => value + 1);
                    setProject(next);
                    setSavedSignature(JSON.stringify(next));
                    setSnapshots({});
                    setVerifiedPricingHashes(new Set());
                    setImportResults([]);
                    setPreviewId(null);
                    setSelected(null);
                  }}
                >
                  <Plus size={16} /> New
                </button>
                <button className="secondary compact" onClick={() => loadRef.current?.click()}>
                  <FolderOpen size={16} /> Open
                </button>
                <button
                  className="secondary compact"
                  onClick={() => void exportReviewRecord()}
                  disabled={!reviewReady || busy || importing || !!error}
                  title={
                    reviewReady
                      ? 'Download draft evidence; not a server-approved audit'
                      : 'Compare every material group before exporting'
                  }
                >
                  Export review record
                </button>
                <button className="secondary compact" onClick={save}>
                  <FileJson size={16} /> Save
                </button>
                {companyId && (
                  <TeamDrafts
                    key={`${companyId}:${estimatorId}:${documentEpoch}`}
                    companyId={companyId}
                    project={project}
                    canSave={canSaveDrafts}
                    disabled={busy || importing}
                    dirty={JSON.stringify(project) !== savedSignature}
                    onOpen={applyOpenedProject}
                    onSaved={signature => {
                      if (signature === JSON.stringify(stateRef.current.project)) setSavedSignature(signature);
                    }}
                  />
                )}
                <button
                  className="primary"
                  onClick={compare}
                  disabled={busy || !!error || !project.groups.some(group => group.quote.parts.length)}
                >
                  <Play size={16} fill="currentColor" />
                  {busy ? 'Comparing…' : 'Compare sheets'}
                </button>
              </div>
            </div>
            <div className={`order-banner ${stale ? 'needs-update' : ''}`} aria-live="polite">
              {stale ? (
                <>
                  <div className="order-icon">
                    <RotateCw />
                  </div>
                  <div>
                    <div className="eyebrow">INPUTS CHANGED</div>
                    <h2>Update your material estimate</h2>
                    <p>{error || 'Compare sheets again to refresh quantities and stock requirements.'}</p>
                  </div>
                </>
              ) : active?.complete && nest ? (
                <>
                  <div className="order-icon">
                    <ShoppingCart />
                  </div>
                  <div className="order-main">
                    <div className="eyebrow">
                      {previewId === comparison.recommendedId ? 'RECOMMENDED MATERIAL ORDER' : 'SELECTED STOCK OPTION'}
                    </div>
                    <h2>
                      {nest.sheets} <span>×</span> {sheetLabel(active.option)}{' '}
                      <small>{nest.sheets === 1 ? 'sheet' : 'sheets'}</small>
                    </h2>
                    <p>
                      {quote.material} · {formatIn(quote.thickness)} in thick · {nest.placements.length} / {requested}{' '}
                      parts covered
                    </p>
                  </div>
                  <div className="order-stat">
                    <span>Purchased area</span>
                    <strong>
                      {fmt(squareFeet(active.area), 2)} <small>ft²</small>
                    </strong>
                  </div>
                  <div className="order-stat">
                    <span>Material estimate</span>
                    <strong>{active.cost === null ? 'Not priced' : money(active.cost)}</strong>
                    {active.cost === null && (
                      <button onClick={() => setTab('stock')}>
                        Add sheet prices <ArrowRight size={13} />
                      </button>
                    )}
                  </div>
                  <button className="secondary" onClick={exportSummary}>
                    <ArrowDownToLine size={16} /> Save summary
                  </button>
                </>
              ) : (
                <>
                  <div className="order-icon">
                    <Info />
                  </div>
                  <div>
                    <div className="eyebrow">MATERIAL REQUIREMENTS</div>
                    <h2>{requested ? 'No complete sheet option yet' : 'Add parts to start an estimate'}</h2>
                    <p>{comparison.reason}</p>
                  </div>
                </>
              )}
            </div>
            <div className="work-grid">
              <aside className="panel parts-panel">
                <div className="panel-heading">
                  <h2>
                    <Layers3 size={16} /> Parts to quote
                  </h2>
                  <span>{quote.parts.length} designs</span>
                </div>
                <button
                  className={'upload-zone' + (dragging ? ' dragging' : '')}
                  onClick={() => importRef.current?.click()}
                  onDragOver={event => {
                    event.preventDefault();
                    setDragging(true);
                  }}
                  onDragLeave={() => setDragging(false)}
                  onDrop={event => {
                    event.preventDefault();
                    setDragging(false);
                    void importFiles(event.dataTransfer.files);
                  }}
                >
                  <Upload />
                  <strong>Upload DXF files</strong>
                  <span>Choose or drop up to 100 files</span>
                  <span>Under 5 MB each · inches by default</span>
                </button>
                {importResults.length > 0 && (
                  <button className="import-report-link" onClick={() => setImportOpen(true)}>
                    View last import · {importResults.length} files
                  </button>
                )}
                <div className="import-units">
                  <span>Unitless files use</span>
                  <Picker label="Unitless DXF units" value={unitless} onChange={setUnitless} items={['in', 'mm']} />
                </div>
                <div className="parts-list">
                  {quote.parts.map(p => {
                    const b = bounds(p.loops[0]);
                    return (
                      <div className={`part-row ${selected === p.id ? 'selected' : ''}`} key={p.id}>
                        <button
                          className={'part-icon color-' + p.color}
                          aria-label={`Highlight ${p.name}`}
                          onClick={() => setSelected(selected === p.id ? null : p.id)}
                        >
                          <svg viewBox={`-20 -20 ${b.width + 40} ${b.height + 40}`}>
                            <path d={svgPath(p.loops)} fill="currentColor" fillRule="evenodd" />
                            {p.referencePaths?.map((path, index) => (
                              <path key={index} d={referencePath(path)} fill="none" stroke="#fbbf24" strokeWidth={1} />
                            ))}
                          </svg>
                        </button>
                        <div className="part-details">
                          <button className="part-title" onClick={() => setSelected(selected === p.id ? null : p.id)}>
                            {p.name}
                          </button>
                          <small>
                            {formatIn(b.width)} × {formatIn(b.height)} in
                          </small>
                          <small className={orientationExplanation(p, quote) ? 'orientation-warning' : ''}>
                            {orientationSummary(p, quote)}
                            {p.grainAxis ? ` · grain ${p.grainAxis.toUpperCase()}` : ''}
                          </small>
                          {p.importMode === 'drawing-bounds' && <small>Legacy footprint · re-import DXF</small>}
                          {selected === p.id && (
                            <div className="part-provenance">
                              <PartOrientationControls
                                part={p}
                                stock={quote}
                                onChange={patch =>
                                  update({
                                    parts: quote.parts.map(part => (part.id === p.id ? { ...part, ...patch } : part)),
                                  })
                                }
                              />
                              <label className="field-label" htmlFor={fieldId + '-revision-' + p.id}>
                                <span>Part revision (if known)</span>
                                <Input
                                  id={fieldId + '-revision-' + p.id}
                                  maxLength={80}
                                  value={p.revision ?? ''}
                                  onChange={event =>
                                    update({
                                      parts: quote.parts.map(part =>
                                        part.id === p.id ? { ...part, revision: event.target.value || undefined } : part
                                      ),
                                    })
                                  }
                                />
                              </label>
                              {p.provenance ? (
                                <details>
                                  <summary>Geometry provenance</summary>
                                  <p>{p.provenance.sourceName}</p>
                                  <p>
                                    Source units: {p.provenance.sourceUnits}; resolved: {p.provenance.resolvedUnits} (
                                    {p.provenance.unitDecision}).
                                  </p>
                                  <p className="hash-value">Source SHA-256: {p.provenance.sourceSha256}</p>
                                  <p className="hash-value">Geometry SHA-256: {p.provenance.geometrySha256}</p>
                                  <p>
                                    {p.provenance.importerVersion} · {p.provenance.geometryVersion} ·{' '}
                                    {p.provenance.sourceHashBasis}
                                  </p>
                                  {p.provenance.warnings.map((warning, index) => (
                                    <p key={index}>{warning}</p>
                                  ))}
                                </details>
                              ) : (
                                <small>No imported source-file provenance.</small>
                              )}
                            </div>
                          )}
                          {!!p.referencePaths?.length && (
                            <small>{p.referencePaths.length} internal reference paths · review intent</small>
                          )}
                          <div className="part-controls">
                            <Input
                              aria-label={`Quantity for ${p.name}`}
                              type="number"
                              min={1}
                              max={300}
                              value={Number.isFinite(p.quantity) ? p.quantity : ''}
                              onChange={e =>
                                update({
                                  parts: quote.parts.map(a =>
                                    a.id === p.id
                                      ? {
                                          ...a,
                                          quantity: e.target.value === '' ? NaN : Number(e.target.value),
                                        }
                                      : a
                                  ),
                                })
                              }
                            />
                            <button
                              className={`icon-button ${selected === p.id ? 'active' : ''}`}
                              aria-label={`Edit rotation and grain for ${p.name}`}
                              aria-expanded={selected === p.id}
                              title="Edit permitted rotations and source grain direction"
                              onClick={() => setSelected(selected === p.id ? null : p.id)}
                            >
                              <Settings2 size={15} />
                            </button>
                            <button
                              className="icon-button delete"
                              aria-label={`Remove ${p.name}`}
                              onClick={() => {
                                update({
                                  parts: quote.parts.filter(a => a.id !== p.id),
                                });
                                if (selected === p.id) setSelected(null);
                              }}
                            >
                              <Trash2 size={15} />
                            </button>
                          </div>
                        </div>
                      </div>
                    );
                  })}
                  {!quote.parts.length && (
                    <div className="empty-parts">
                      Add a rectangle or circle for a quick estimate, or import your DXF geometry.
                    </div>
                  )}
                </div>
                <button className="add-part" onClick={() => setShowAdd(true)}>
                  <Plus size={16} /> Add basic shape
                </button>
                <p className="helper">
                  Select a part to set its allowed rotations and grain. Grain requirements need a known sheet grain.
                </p>
              </aside>
              <section className="canvas-panel">
                <div className="canvas-toolbar">
                  <span>
                    <span className="status-dot" />
                    {stock ? sheetLabel(stock) : 'Sheet preview'}
                    <ChevronRight size={14} /> {String(sheet + 1).padStart(2, '0')}
                  </span>
                  <div className="canvas-tools">
                    <button
                      className="icon-button"
                      aria-label="Zoom out"
                      disabled={zoom <= 1}
                      onClick={() => setZoom(Math.max(1, zoom - 0.25))}
                    >
                      <ZoomOut size={16} />
                    </button>
                    <span>{Math.round(zoom * 100)}%</span>
                    <button
                      className="icon-button"
                      aria-label="Zoom in"
                      disabled={zoom >= 2.5}
                      onClick={() => setZoom(Math.min(2.5, zoom + 0.25))}
                    >
                      <ZoomIn size={16} />
                    </button>
                    <button className="icon-button" aria-label="Fit sheet" onClick={() => setZoom(1)}>
                      <Maximize size={15} />
                    </button>
                  </div>
                </div>
                {stale || !stock || !nest || !visible.length ? (
                  <div className="canvas-empty">
                    <Layers3 size={32} />
                    <h2>
                      {stale ? 'Ready to compare' : requested ? 'No placements on this sheet' : 'Your nest preview'}
                    </h2>
                    <p>
                      {error ||
                        active?.error ||
                        (stale
                          ? 'Update the estimate to see a fresh nest.'
                          : requested
                            ? comparison.reason
                            : 'Import parts to compare stock sizes.')}
                    </p>
                    <button className="primary" onClick={compare} disabled={busy || !!error || !quote.parts.length}>
                      Compare {enabled} sheet sizes
                    </button>
                  </div>
                ) : (
                  <>
                    <div className="canvas-wrap">
                      <svg
                        className="nest-svg"
                        style={{
                          width: `${zoom * 100}%`,
                          minWidth: zoom > 1 ? `${zoom * 100}%` : undefined,
                        }}
                        viewBox={`-105 -95 ${stock.width + 210} ${stock.height + 200}`}
                        role="img"
                        aria-label={`Layout on ${sheetLabel(stock)}, sheet ${sheet + 1} of ${nest.sheets}, ${visible.length} parts`}
                      >
                        <defs>
                          <pattern id="grid" width="25.4" height="25.4" patternUnits="userSpaceOnUse">
                            <path d="M25.4 0H0V25.4" fill="none" stroke="#1e293b" strokeWidth="1" />
                          </pattern>
                        </defs>
                        <rect
                          width={stock.width}
                          height={stock.height}
                          fill="url(#grid)"
                          stroke="#64748b"
                          strokeWidth="3"
                        />
                        <rect
                          x={quote.margin}
                          y={quote.margin}
                          width={stock.width - 2 * quote.margin}
                          height={stock.height - 2 * quote.margin}
                          fill="none"
                          stroke="#3b82f6"
                          strokeDasharray="15 10"
                          strokeWidth="2"
                        />
                        <text x={stock.width / 2} y={-36} textAnchor="middle" fill="#94a3b8" fontSize="32">
                          {formatIn(stock.width)} in
                        </text>
                        {quote.grainAxis && (
                          <text
                            x={stock.width / 2}
                            y={stock.height + 60}
                            textAnchor="middle"
                            fill="#93c5fd"
                            fontSize="25"
                          >
                            {quote.grainAxis === 'x'
                              ? '↔ Grain along sheet length (X)'
                              : '↕ Grain along sheet width (Y)'}
                          </text>
                        )}
                        <text
                          transform={`translate(-42 ${stock.height / 2}) rotate(-90)`}
                          textAnchor="middle"
                          fill="#94a3b8"
                          fontSize="32"
                        >
                          {formatIn(stock.height)} in
                        </text>
                        <g transform={`translate(0 ${stock.height}) scale(1 -1)`}>
                          {showLeftovers && leftoverSheet && (
                            <LeftoverOverlay sheet={leftoverSheet} highlightedId={selectedLeftover} />
                          )}
                          {visible.map((pl, i) => {
                            const p = quote.parts.find(a => a.id === pl.partId)!;
                            return (
                              <g
                                key={p.id + '-' + pl.instance}
                                className="placed-part"
                                onClick={() => setSelected(selected === p.id ? null : p.id)}
                              >
                                <title>{`${p.name} #${pl.instance + 1} · ${formatIn(pl.width)} × ${formatIn(pl.height)} in · ${pl.rotation}° · ${orientationSummary(p, quote)} permitted${p.grainAxis ? ` · grain aligned with sheet ${quote.grainAxis?.toUpperCase()}` : ''}`}</title>
                                <path
                                  d={svgPath(transformLoops(p, pl))}
                                  fill={colors[p.color].fill}
                                  fillRule="evenodd"
                                  stroke={selected === p.id ? '#ffffff' : colors[p.color].stroke}
                                  strokeWidth={selected === p.id ? 5 : 2.5}
                                  opacity={selected && selected !== p.id ? 0.4 : 1}
                                />
                                {transformReferencePaths(p, pl).map((path, index) => (
                                  <path
                                    key={index}
                                    d={referencePath(path)}
                                    fill="none"
                                    stroke="#fbbf24"
                                    strokeWidth={0.8}
                                    opacity={0.95}
                                  >
                                    <title>Internal reference path · verify marking or cut intent</title>
                                  </path>
                                ))}
                                {labels && (
                                  <text
                                    transform={`translate(${pl.x + pl.width / 2} ${pl.y + pl.height / 2}) scale(1 -1)`}
                                    textAnchor="middle"
                                    dominantBaseline="middle"
                                    fill="#f1f5f9"
                                    fontSize={Math.min(33, pl.width / 5, pl.height / 5)}
                                    style={{ pointerEvents: 'none' }}
                                  >
                                    {String(i + 1).padStart(2, '0')}
                                  </text>
                                )}
                              </g>
                            );
                          })}
                        </g>
                      </svg>
                    </div>
                    <div className="sheet-tabs">
                      {Array.from({ length: nest.sheets }, (_, i) => (
                        <button key={i} className={i === sheet ? 'active' : ''} onClick={() => setSheet(i)}>
                          Sheet {i + 1}
                          <span>{nest.placements.filter(p => p.sheet === i).length} parts</span>
                        </button>
                      ))}
                    </div>
                  </>
                )}
                <div className="canvas-footer">
                  <span>
                    {selected ? quote.parts.find(p => p.id === selected)?.name : 'One-inch grid · actual part contours'}
                  </span>
                  <label className="switch-inline" htmlFor={fieldId + '-leftovers'}>
                    Potential leftovers
                    <Switch
                      size="sm"
                      id={fieldId + '-leftovers'}
                      checked={showLeftovers}
                      onCheckedChange={setShowLeftovers}
                      aria-label="Show potential leftovers"
                    />
                  </label>
                  <label className="switch-inline" htmlFor={fieldId + '-labels'}>
                    Labels
                    <Switch
                      size="sm"
                      id={fieldId + '-labels'}
                      checked={labels}
                      onCheckedChange={setLabels}
                      aria-label="Show part labels"
                    />
                  </label>
                </div>
              </section>
              <aside className="panel quote-settings">
                <div className="panel-heading">
                  <h2>
                    <Settings2 size={16} /> Estimate setup
                  </h2>
                </div>
                <div className="settings-body">
                  <button className="stock-manager" onClick={() => setTab('stock')}>
                    <span>
                      ERP material source
                      <small>{quote.materialBinding?.catalog.name ?? 'Select an exact catalog record'}</small>
                    </span>
                    <ChevronRight size={16} />
                  </button>
                  <label className="field-label" htmlFor={fieldId + '-material'}>
                    <span>Material</span>
                    <Picker
                      id={fieldId + '-material'}
                      label="Material"
                      value={quote.material}
                      onChange={v => changeMaterial({ material: v })}
                      items={['Carbon steel', 'Stainless steel', 'Aluminum']}
                    />
                  </label>
                  <NumberField
                    label="Thickness"
                    value={quote.thickness}
                    unit="in"
                    max={100}
                    onChange={v => changeMaterial({ thickness: v })}
                  />
                  <SheetGrainControl grainAxis={quote.grainAxis} onChange={grainAxis => update({ grainAxis })} />
                  <p className="helper inset-free">
                    Applies to every stock size in this material group. Set a part’s grain from its DXF orientation.
                  </p>
                  <div className="two-fields">
                    <NumberField
                      label="Edge margin"
                      value={quote.margin}
                      unit="in"
                      onChange={v => update({ margin: v, spacingMode: 'manual' })}
                    />
                    <NumberField
                      label="Part gap"
                      value={quote.gap}
                      unit="in"
                      onChange={v => update({ gap: v, spacingMode: 'manual' })}
                    />
                  </div>
                  <label className="switch-inline" htmlFor={fieldId + '-auto-spacing'}>
                    <span id={fieldId + '-auto-spacing-label'}>Auto quoting allowance</span>
                    <Switch
                      id={fieldId + '-auto-spacing'}
                      aria-labelledby={fieldId + '-auto-spacing-label'}
                      checked={quote.spacingMode === 'auto'}
                      onCheckedChange={auto => {
                        try {
                          update(
                            auto
                              ? { ...autoQuotingSpacing(quote.thickness), spacingMode: 'auto' }
                              : { spacingMode: 'manual' }
                          );
                        } catch (e) {
                          toast.error((e as Error).message);
                        }
                      }}
                    />
                  </label>
                  <p className="helper inset-free">
                    Starting estimate: gap = max(1/8 in, thickness); edge = max(3/8 in, twice thickness). Editable
                    quoting allowances, not machine cutting parameters.
                  </p>
                  <label className="field-label" htmlFor={fieldId + '-priority'}>
                    <span>Compare by</span>
                    <Picker
                      id={fieldId + '-priority'}
                      label="Comparison priority"
                      value={quote.objective === 'area' ? 'Least material to buy' : 'Lowest material cost'}
                      items={['Least material to buy', 'Lowest material cost']}
                      onChange={v =>
                        update({
                          objective: v === 'Least material to buy' ? 'area' : 'cost',
                        })
                      }
                    />
                  </label>
                  <button className="stock-manager" onClick={() => setTab('stock')}>
                    <Layers3 size={16} />
                    <span>
                      {enabled} stock sizes enabled
                      <small>Manage sizes and sheet prices</small>
                    </span>
                    <ChevronRight size={16} />
                  </button>
                  {error && (
                    <div className="inline-error" role="alert">
                      {error}
                    </div>
                  )}
                  <p className="helper inset-free">
                    Measurements accept decimal inches or fractions, such as 1/8. Prices are optional and apply to this
                    material and thickness.
                  </p>
                </div>
                <div className="note">
                  <b>Quote layout · not an NC program</b>
                  <p>Compare sheet quantities before quoting. No cutting recipes or machine setup required.</p>
                </div>
              </aside>
            </div>
            <section className="comparison-section">
              <div className="comparison-heading">
                <div>
                  <div className="eyebrow">STOCK COMPARISON</div>
                  <h2>Which sheets should you order?</h2>
                </div>
                <button className="secondary compact" onClick={() => setTab('stock')}>
                  <Settings2 size={15} /> Edit stock options
                </button>
              </div>
              {stale ? (
                <div className="comparison-stale">Your inputs changed. Compare sheets to update this table.</div>
              ) : (
                <>
                  <p className="comparison-reason">{comparison.reason} Each option uses one stock size.</p>
                  <Table className="comparison-table">
                    <TableHeader>
                      <TableRow>
                        <TableHead>Sheet size</TableHead>
                        <TableHead>Sheets needed</TableHead>
                        <TableHead>Area to buy</TableHead>
                        <TableHead>Utilization</TableHead>
                        <TableHead>Material cost</TableHead>
                        <TableHead>Result</TableHead>
                        <TableHead>
                          <span className="sr-only">Preview</span>
                        </TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {comparison.results.map(r => (
                        <TableRow key={r.option.id} className={previewId === r.option.id ? 'chosen-row' : ''}>
                          <TableCell>
                            <b>{sheetLabel(r.option)}</b>
                            {comparison.recommendedId === r.option.id && (
                              <span className="recommended-chip">
                                <Check size={12} /> Best option
                              </span>
                            )}
                          </TableCell>
                          <TableCell>{r.complete ? r.nest?.sheets : '—'}</TableCell>
                          <TableCell>{r.complete ? `${fmt(squareFeet(r.area), 2)} ft²` : '—'}</TableCell>
                          <TableCell>{r.complete ? `${fmt(r.nest!.utilization)}%` : '—'}</TableCell>
                          <TableCell>{r.complete && r.cost !== null ? money(r.cost) : 'Not priced'}</TableCell>
                          <TableCell>
                            {r.complete ? (
                              <span className="complete-result">
                                <Check size={13} /> All {requested} parts
                              </span>
                            ) : (
                              <span className="incomplete-result">
                                {r.error ||
                                  `${r.nest?.unplaced.reduce((a, u) => a + u.count, 0) ?? requested} parts do not fit`}
                              </span>
                            )}
                          </TableCell>
                          <TableCell>
                            <button
                              className={previewId === r.option.id ? 'preview-btn active' : 'preview-btn'}
                              onClick={() => preview(r)}
                              disabled={!r.nest}
                            >
                              {previewId === r.option.id ? 'Viewing' : 'View nest'}
                              <ChevronRight size={14} />
                            </button>
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </>
              )}
              {!stale && active && !active.complete && quote.parts.length > 0 && (
                <div className="unplaced-panel">
                  <h2>Parts requiring review</h2>
                  {oversizeParts(quote, active.option).map(p => (
                    <p key={p.id}>
                      <b>
                        {p.name} × {p.quantity}
                      </b>
                      <span>
                        {formatIn(bounds(p.loops[0]).width)} × {formatIn(bounds(p.loops[0]).height)} in ·{' '}
                        {orientationExplanation(p, quote) ?? `${orientationSummary(p, quote)} allowed`}
                      </span>
                    </p>
                  ))}
                  {active.error && <p>{active.error}</p>}
                </div>
              )}
              {!stale && active?.complete && nest && (
                <div className="quote-metrics">
                  <div>
                    <span>Net part area</span>
                    <b>{fmt(squareFeet(nest.area), 2)} ft²</b>
                  </div>
                  <div>
                    <span>Unused area</span>
                    <b>{fmt(squareFeet(active.area - nest.area), 2)} ft²</b>
                  </div>
                  <div>
                    <span>Approx. stock weight</span>
                    <b>{mass === null ? 'Unavailable' : `${fmt(mass)} lb`}</b>
                  </div>
                  <div>
                    <span>Part coverage</span>
                    <b>
                      {nest.placements.length} / {requested}
                    </b>
                  </div>
                </div>
              )}
              {!stale && active?.complete && (
                <LeftoverReview
                  sheet={leftoverSheet}
                  error={active.leftoverError}
                  highlightedId={selectedLeftover}
                  onHighlight={id => {
                    setSelectedLeftover(current => (current === id ? null : id));
                    setShowLeftovers(true);
                  }}
                />
              )}
            </section>
            <div className="workspace-bottom">
              <p>
                <Info size={15} /> Actual contours are nested with the selected gap and margins. The search does not
                prove the minimum sheet count, and it does not place parts inside holes.
              </p>
              <div>
                <button disabled={stale || !visible.length} onClick={exportPreview}>
                  <ArrowDownToLine size={14} /> Save nest preview
                </button>
                <button disabled={stale || !active?.complete} onClick={exportSummary}>
                  <FileText size={14} /> Material summary
                </button>
              </div>
            </div>
          </TabsContent>
          <TabsContent value="stock">
            <div className="content-page">
              <div className="page-heading">
                <div className="eyebrow">SHEETS YOU CAN BUY</div>
                <h1>Compare your actual stock options.</h1>
                <p>
                  Enable available sizes and select a pricing source for {materialGroupLabel(quote)},{' '}
                  {formatIn(quote.thickness)} inch thick. Sizes are editable; availability is supplied by you.
                </p>
              </div>
              <MaterialSourcePanel
                quote={quote}
                companyId={companyId}
                catalog={catalog}
                verifiedHashes={verifiedPricingHashes}
                onBindingChange={(binding?: MaterialBinding) =>
                  changeMaterial({
                    materialBinding: binding,
                    ...(binding ? { material: catalogFamily(binding.catalog.category)! } : {}),
                  })
                }
                onResolved={binding => {
                  setVerifiedPricingHashes(
                    current => new Set([...Array.from(current), binding.resolution!.content_hash])
                  );
                  changeMaterial({ materialBinding: binding });
                }}
                onApply={(binding, prices) =>
                  update({
                    materialBinding: binding,
                    options: quote.options.map(option => ({ ...option, price: prices.get(option.id) ?? null })),
                  })
                }
              />
              <div className="stock-intro">
                <div>
                  <b>Prices per sheet · USD</b>
                  <p>
                    Blank means unknown. ERP prices require source resolution and review; clear the source to enter
                    supplier prices manually.
                  </p>
                </div>
                <button
                  className="primary"
                  disabled={quote.options.length >= 12}
                  onClick={() =>
                    update({
                      options: [
                        ...quote.options,
                        {
                          id: crypto.randomUUID(),
                          width: inToMm(120),
                          height: inToMm(60),
                          enabled: true,
                          price: null,
                        },
                      ],
                    })
                  }
                >
                  <Plus size={16} /> Add stock size
                </button>
              </div>
              <div className="stock-grid">
                {quote.options.map(o => (
                  <section className={`section-card stock-card ${o.enabled ? 'enabled' : ''}`} key={o.id}>
                    <div className="section-title">
                      <h2>{sheetLabel(o)}</h2>
                      <Switch
                        checked={o.enabled}
                        aria-label={`Compare ${sheetLabel(o)}`}
                        onCheckedChange={v => changeOption(o.id, { enabled: v })}
                      />
                    </div>
                    <div className="two-fields">
                      <NumberField
                        label="Width"
                        value={o.height}
                        unit="in"
                        onChange={v => changeOption(o.id, { height: v })}
                      />
                      <NumberField
                        label="Length"
                        value={o.width}
                        unit="in"
                        onChange={v => changeOption(o.id, { width: v })}
                      />
                    </div>
                    <label className="field-label" htmlFor={fieldId + '-price-' + o.id}>
                      <span>
                        Price per sheet<small>USD · optional</small>
                      </span>
                      <Input
                        type="number"
                        min={0}
                        step=".01"
                        id={fieldId + '-price-' + o.id}
                        placeholder={quote.materialBinding ? 'Resolve ERP source' : 'Not entered'}
                        disabled={!!quote.materialBinding}
                        value={o.price === null ? '' : o.price}
                        onChange={e =>
                          changeOption(o.id, {
                            price: e.target.value === '' ? null : Number(e.target.value),
                          })
                        }
                      />
                    </label>
                    <div className="stock-card-footer">
                      <span>{fmt(squareFeet(o.width * o.height), 2)} ft² per sheet</span>
                      <button
                        className="icon-button delete"
                        aria-label={`Remove ${sheetLabel(o)} stock option`}
                        disabled={quote.options.length === 1}
                        onClick={() =>
                          update({
                            options: quote.options.filter(s => s.id !== o.id),
                          })
                        }
                      >
                        <Trash2 size={15} />
                      </button>
                    </div>
                  </section>
                ))}
              </div>
              {error && <div className="inline-error">{error}</div>}
              <div className="stock-bottom">
                <p>Each result is a complete order using a single stock size. Mixed-size orders are not compared.</p>
                <button
                  className="primary"
                  disabled={busy || !!error || !project.groups.some(group => group.quote.parts.length)}
                  onClick={() => {
                    setTab('nest');
                    compare();
                  }}
                >
                  <Play size={16} /> Compare enabled sizes
                </button>
              </div>
            </div>
          </TabsContent>
          <TabsContent value="about">
            <div className="content-page">
              <div className="page-heading">
                <div className="eyebrow">NESTING FOR MATERIAL QUOTES</div>
                <h1>Parts in. Sheet requirements out.</h1>
                <p>
                  Use the same parts, quantities, spacing and grain constraints to compare the sheet sizes you can buy.
                </p>
              </div>
              <div className="profile-grid">
                <section className="section-card">
                  <h2>How an option is selected</h2>
                  <ol className="help-list">
                    <li>Each enabled stock size is nested separately against the entire parts list.</li>
                    <li>Only options that fit every requested part can be recommended.</li>
                    <li>“Least material” compares the full area of all sheets you would buy.</li>
                    <li>
                      “Lowest material cost” compares quantity × your entered sheet price. Every complete option needs a
                      price.
                    </li>
                    <li>Select any row to inspect its sheets and export a material summary.</li>
                  </ol>
                </section>
                <section className="section-card">
                  <h2>What the estimate means</h2>
                  <p>
                    Counts come from feasible placements of the actual outer profiles, including concave shapes. Circles
                    remain circles and curves are approximated within the import tolerance. Different orderings and
                    allowed quarter-turn rotations are tried. This search does not prove the smallest possible sheet
                    order.
                  </p>
                  <p>
                    Each part can use fixed orientation, half turns or quarter turns. Part grain is the horizontal (X)
                    or vertical (Y) direction in the source drawing. A required grain must align with the selected sheet
                    grain; unknown or conflicting directions stay unplaced. Parts are never mirrored.
                  </p>
                  <p>
                    Utilization is contour area minus holes, divided by full purchased sheet area. Parts are not nested
                    inside holes. “Unused area” includes margins, spaces and holes. Amber leftover regions are a
                    separate conservative prediction after spacing and curve protection; every region requires review
                    and receives no cost credit. Approximate weight uses the selected source density when available.
                  </p>
                  <p>
                    Optional prices cover sheet material only. They exclude freight, tax, labor, cutting time and
                    consumables. No supplier availability or price is assumed.
                  </p>
                </section>
              </div>
              <section className="section-card">
                <h2>Import and save</h2>
                <p>
                  Upload or drag in up to 100 DXF files per batch, each under 5 MB. A file-by-file report explains any
                  skipped files; successful files remain in your estimate. Connected lines, arcs, circles, and 2D
                  polylines form part outlines automatically. Separate closed outer contours become separate part
                  designs; interior loops remain holes. Inch and millimeter DXFs retain their physical size. Unitless
                  files default to inches unless you select millimeters before import.
                </p>
                <p>
                  Supported splines are converted to their curved profiles. Unclosed paths fully contained by one part
                  are shown as thin reference lines: review whether they are markings or incomplete cuts. Ambiguous,
                  open outer, touching or intersecting outlines are rejected with an explanation. The FORMAT annotation
                  layer is omitted with a warning. Blocks, wide polylines and sloped 3D paths are reported without
                  adding a partial file. Older saved rectangular footprints must be removed and their DXFs re-imported.
                  Check imported dimensions and reference-path intent against your drawing.
                </p>
                <p>
                  Assign material and thickness to selected file rows before import. Each matching group keeps its own
                  stock options, prices and spacing. Save retains all groups and the active selection in inches; older
                  single-material files open as one group. Material Nesting starts with a fresh, empty estimate each
                  time you open the section. Use Save for a local file, or Team drafts to save an ERP revision before
                  leaving, refreshing, signing out, or switching companies. Open a local file with Open, or select a
                  revision in Team drafts. Each team save retains the previous revision. Drafts store inputs; they do
                  not approve a quote or reserve material.
                </p>
              </section>
              <section className="section-card">
                <h2>Current scope</h2>
                <p>
                  Multiple material and thickness groups per estimate, with separate sheet orders. Up to 100 files per
                  upload, 300 designs and total parts across all groups, 12 stock options per group, 2,000 vertices per
                  contour and 20,000 vertices including reference paths across the project. One stock size per order
                  option. There is no machine connection, cutting technology library, postprocessor or purchase
                  submission.
                </p>
                <p>
                  No machine envelope is imposed on the stock comparison. Use sheet sizes your supplier can provide and
                  your shop can process.
                </p>
              </section>
            </div>
          </TabsContent>
        </Tabs>
      </fieldset>
      {pendingFiles && (
        <DXFAssignments
          files={pendingFiles}
          initial={{
            material: quote.material,
            thickness: quote.thickness,
            materialBinding: quote.materialBinding,
            units: unitless as 'in' | 'mm',
          }}
          companyId={companyId}
          catalog={catalog}
          onClose={() => setPendingFiles(null)}
          onConfirm={assignments => void confirmImport(pendingFiles, assignments)}
        />
      )}
      <Dialog
        open={importOpen}
        onOpenChange={open => {
          if (!importing) setImportOpen(open);
        }}
      >
        <DialogContent className="app-dialog import-dialog" showCloseButton={!importing}>
          <DialogHeader>
            <DialogTitle>{importing ? 'Importing DXF files' : 'DXF import results'}</DialogTitle>
            <DialogDescription>
              {importing
                ? 'Files are checked one at a time. Successful files are added to this estimate.'
                : 'Check the imported dimensions and quantities, then compare sheets.'}
            </DialogDescription>
          </DialogHeader>
          {importing ? (
            <>
              <Progress value={importProgress.completed} max={importProgress.total}>
                <ProgressLabel>
                  {importProgress.completed} of {importProgress.total} files checked
                </ProgressLabel>
                <ProgressValue />
              </Progress>
              <p className="import-current" aria-live="polite">
                {importProgress.name}
              </p>
              <button className="secondary" onClick={() => importController.current?.abort()}>
                Stop import · keep completed files
              </button>
            </>
          ) : (
            <>
              <div className="import-summary" role="status">
                <strong>{importResults.filter(r => r.status === 'imported').length} imported</strong>
                <span>{importResults.filter(r => r.status === 'skipped').length} skipped</span>
                <span>{importResults.reduce((n, r) => n + r.designs, 0)} designs added</span>
              </div>
              <div className="import-results" role="region" aria-label="Results for every selected file">
                {importResults.map((result, i) => (
                  <div className={'import-result ' + result.status} key={i}>
                    <div>
                      <strong>{result.name}</strong>
                      <span>{result.status === 'imported' ? 'Imported' : 'Skipped'}</span>
                    </div>
                    <p>{result.message}</p>
                    {result.warnings?.map((warning, index) => (
                      <p key={index}>{warning}</p>
                    ))}
                  </div>
                ))}
              </div>
              <p className="import-footnote">
                New designs start at quantity 1. Closed outer profiles create separate designs in their assigned
                material and thickness groups. Skipped files add no parts.
              </p>
              <button className="primary" onClick={() => setImportOpen(false)}>
                Review parts
              </button>
            </>
          )}
        </DialogContent>
      </Dialog>
      <Dialog open={showAdd} onOpenChange={setShowAdd}>
        <DialogContent className="app-dialog">
          <DialogHeader>
            <DialogTitle>Add a part by size</DialogTitle>
            <DialogDescription>Use inches, including fractions such as 1/8 or 12 3/8.</DialogDescription>
          </DialogHeader>
          <label className="field-label" htmlFor={fieldId + '-part-name'}>
            <span>Part name</span>
            <Input
              id={fieldId + '-part-name'}
              value={newPart.name}
              onChange={e => setNewPart({ ...newPart, name: e.target.value })}
            />
          </label>
          <Picker
            label="Shape"
            value={newPart.shape}
            onChange={v => setNewPart({ ...newPart, shape: v })}
            items={['Rectangle', 'Circle']}
          />
          <div className="two-fields">
            <NumberField
              label={newPart.shape === 'Circle' ? 'Diameter' : 'Width'}
              value={newPart.width}
              unit="in"
              onChange={v => setNewPart({ ...newPart, width: v })}
            />
            {newPart.shape === 'Rectangle' && (
              <NumberField
                label="Height"
                value={newPart.height}
                unit="in"
                onChange={v => setNewPart({ ...newPart, height: v })}
              />
            )}
          </div>
          <NumberField
            label="Quantity"
            value={newPart.quantity}
            min={1}
            max={300}
            step="1"
            onChange={v => setNewPart({ ...newPart, quantity: v })}
          />
          <button className="primary" onClick={addPart}>
            <Plus size={16} /> Add part
          </button>
        </DialogContent>
      </Dialog>
      <Dialog open={help} onOpenChange={setHelp}>
        <DialogContent className="app-dialog">
          <DialogHeader>
            <DialogTitle>Get your sheet order in a few steps</DialogTitle>
            <DialogDescription>Each material and thickness has its own nest and sheet order.</DialogDescription>
          </DialogHeader>
          <ol className="help-list">
            <li>Add part shapes or import DXFs. Assign file materials and thicknesses in inches.</li>
            <li>Set quantities, allowed rotations, part and sheet grain, edge margin and spacing.</li>
            <li>Enable stock sizes you can buy. Add supplier prices if needed.</li>
            <li>Compare sheets and review the recommended size and quantity.</li>
            <li>Save your estimate and material requirement summary.</li>
          </ol>
          <button
            className="secondary"
            onClick={() => {
              setHelp(false);
              setTab('about');
            }}
          >
            How the estimate is calculated
            <ChevronRight size={16} />
          </button>
        </DialogContent>
      </Dialog>
      <footer className="app-footer">
        <span>
          <Crosshair size={14} /> WERCO NEST
        </span>
        <span>
          Quote layout · not an NC program · Imperial units <span className="footer-divider">/</span> Save your estimate
          to keep work
        </span>
      </footer>
    </section>
  );
}
