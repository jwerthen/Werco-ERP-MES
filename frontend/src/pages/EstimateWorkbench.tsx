import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import {
  CalculatorIcon,
  PlusIcon,
  TrashIcon,
  ArrowPathIcon,
  CheckBadgeIcon,
  ExclamationTriangleIcon,
  ArrowDownTrayIcon,
} from '@heroicons/react/24/outline';
import api from '../services/api';
import { Button, ErrorState, LoadingButton, StatusBadge, useToast } from '../components/ui';
import { useDebouncedValue } from '../hooks/useDebouncedValue';
import {
  AssemblyDraft,
  BidSummary,
  BuyoutLineDraft,
  ExtractFromRfqResponse,
  FabLineDraft,
  MachinedLineDraft,
  RecalcResponse,
  VerificationReport,
  WorkbenchResponse,
  emptyAssembly,
  emptyBuyoutLine,
  emptyFabLine,
  emptyMachinedLine,
  workbenchToDrafts,
} from '../types/estimateWorkbench';

function downloadBlob(blob: Blob, filename: string) {
  const url = window.URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.URL.revokeObjectURL(url);
}

function money(n: number | null | undefined): string {
  const v = Number(n || 0);
  return v.toLocaleString(undefined, { style: 'currency', currency: 'USD' });
}

function hours(n: number | null | undefined): string {
  return `${Number(n || 0).toFixed(2)} hrs`;
}

function numOrNull(raw: string): number | null {
  if (raw.trim() === '') return null;
  const n = Number(raw);
  return Number.isFinite(n) ? n : null;
}

function numOrZero(raw: string): number {
  const n = Number(raw);
  return Number.isFinite(n) ? n : 0;
}

const CONFIDENCE_OPTIONS = [
  { value: 'confirmed', label: 'Confirmed' },
  { value: 'majority', label: 'Majority' },
  { value: 'review', label: 'Review' },
];

const inputCls =
  'w-full min-w-0 bg-fd-sunken border border-fd-line rounded-sm px-1.5 py-1 text-sm text-white tabular-nums focus:outline-none focus:border-werco-navy-500';
const checkCls = 'h-3.5 w-3.5 accent-werco-navy-600';

export default function EstimateWorkbench() {
  const { estimateId: estimateIdParam } = useParams<{ estimateId: string }>();
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const { showToast } = useToast();

  const [estimateId, setEstimateId] = useState<number | null>(
    estimateIdParam && estimateIdParam !== 'new' ? Number(estimateIdParam) : null
  );
  const [rfqPackageId, setRfqPackageId] = useState<string>(
    searchParams.get('rfq_package_id') || ''
  );
  const [version, setVersion] = useState(1);
  const [assemblies, setAssemblies] = useState<AssemblyDraft[]>([emptyAssembly(0)]);
  const [machinedParts, setMachinedParts] = useState<MachinedLineDraft[]>([]);
  const [activeAssemblyIdx, setActiveAssemblyIdx] = useState(0);
  const [bidSummary, setBidSummary] = useState<BidSummary | null>(null);
  const [shopSource, setShopSource] = useState<string>('defaults');
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [finalizing, setFinalizing] = useState(false);
  const [extracting, setExtracting] = useState(false);
  const [extractionPreview, setExtractionPreview] = useState<ExtractFromRfqResponse | null>(null);
  const [exporting, setExporting] = useState<'audit' | 'json' | 'pdf' | null>(null);
  const [recalcing, setRecalcing] = useState(false);
  const [loadError, setLoadError] = useState('');
  const [dirty, setDirty] = useState(false);
  const [verification, setVerification] = useState<VerificationReport | null>(null);
  const [quoteId, setQuoteId] = useState<number | null>(null);
  const [quoteNumber, setQuoteNumber] = useState<string | null>(null);
  const skipNextRecalc = useRef(false);

  const draftKey = useMemo(
    () => JSON.stringify({ assemblies, machinedParts }),
    [assemblies, machinedParts]
  );
  const debouncedDraft = useDebouncedValue(draftKey, 350);

  const applyRecalc = useCallback((resp: RecalcResponse) => {
    setBidSummary(resp.bid_summary);
    setShopSource(resp.shop_data_source || 'defaults');

    setAssemblies((prev) => {
      let fabCursor = 0;
      return prev.map((asm) => {
        const fab_lines = asm.fab_lines.map((fl) => {
          const out = resp.fab_lines[fabCursor++];
          if (!out) return fl;
          return {
            ...fl,
            weight_ea_lb: out.weight_ea_lb,
            material_cost: out.material_cost,
            laser_cost: out.laser_cost,
            laser_hours: out.laser_hours,
            brake_cost: out.brake_cost,
            brake_hours: out.brake_hours,
            weld_cost: out.weld_cost,
            weld_hours: out.weld_hours,
            line_total: out.line_total,
            calc_errors: out.errors?.length ? out.errors : null,
            calc_warnings: out.warnings?.length ? out.warnings : null,
            cut_length_in: fl.cut_length_in ?? (out.cut_length_used || fl.cut_length_in),
          };
        });
        return { ...asm, fab_lines };
      });
    });

    setMachinedParts((prev) =>
      prev.map((mp, i) => {
        const out = resp.machined_parts[i];
        if (!out) return mp;
        return {
          ...mp,
          weight_ea_lb: out.weight_ea_lb,
          material_cost: out.material_cost,
          turning_cost: out.turning_cost,
          turning_hours: out.turning_hours,
          milling_cost: out.milling_cost,
          milling_hours: out.milling_hours,
          line_total: out.line_total,
        };
      })
    );
  }, []);

  const runRecalc = useCallback(
    async (asms: AssemblyDraft[], machined: MachinedLineDraft[]) => {
      setRecalcing(true);
      try {
        const resp: RecalcResponse = await api.recalcEstimateWorkbench({
          assemblies: asms.map((a) => ({
            name: a.name,
            assembly_labor_hrs: a.assembly_labor_hrs,
            electrical_labor_hrs: a.electrical_labor_hrs,
            fab_lines: a.fab_lines.map((f) => ({
              detail_name: f.detail_name,
              part_number: f.part_number,
              material: f.material,
              material_family_override: f.material_family_override,
              qty: f.qty,
              thickness_in: f.thickness_in,
              width_in: f.width_in,
              length_in: f.length_in,
              cut_length_in: f.cut_length_in,
              pierce_count: f.pierce_count,
              bend_count: f.bend_count,
              weld_length_in: f.weld_length_in,
              weld_minutes_ea: f.weld_minutes_ea,
              include_material: f.include_material,
              include_laser: f.include_laser,
              include_brake: f.include_brake,
              include_weld: f.include_weld,
              price_per_lb: f.price_per_lb || 0,
              density_lb_per_in3: f.density_lb_per_in3 || 0.284,
            })),
            buyout_lines: a.buyout_lines.map((b) => ({
              description: b.description,
              qty: b.qty,
              unit_cost: b.unit_cost,
            })),
          })),
          machined_parts: machined.map((m) => ({
            description: m.description,
            material: m.material,
            qty: m.qty,
            stock_dia_in: m.stock_dia_in,
            stock_length_in: m.stock_length_in,
            turning_minutes: m.turning_minutes,
            milling_minutes: m.milling_minutes,
            price_per_lb: m.price_per_lb || 0,
            density_lb_per_in3: m.density_lb_per_in3 || 0.284,
          })),
        });
        applyRecalc(resp);
      } catch (err: any) {
        // Live recalc failures shouldn't block editing — toast lightly
        console.error(err);
      } finally {
        setRecalcing(false);
      }
    },
    [applyRecalc]
  );

  const hydrateFromWorkbench = useCallback((wb: WorkbenchResponse) => {
    const drafts = workbenchToDrafts(wb);
    skipNextRecalc.current = true;
    setEstimateId(wb.estimate_id);
    setVersion(wb.version);
    setAssemblies(drafts.assemblies.length ? drafts.assemblies : [emptyAssembly(0)]);
    setMachinedParts(drafts.machined_parts);
    setActiveAssemblyIdx(0);
    setShopSource(wb.shop_data_source || 'defaults');
    setVerification(wb.verification || null);
    setQuoteId(wb.quote_id ?? null);
    setDirty(false);
    const b = wb.internal_breakdown || {};
    if (b.sell_price != null) {
      setBidSummary({
        fab_material: Number(b.fab_material || 0),
        fab_laser: Number(b.fab_laser || 0),
        fab_brake: Number(b.fab_brake || 0),
        fab_weld: Number(b.fab_weld || 0),
        fab_subtotal:
          Number(b.fab_material || 0) +
          Number(b.fab_laser || 0) +
          Number(b.fab_brake || 0) +
          Number(b.fab_weld || 0),
        buyout_subtotal: Number(b.buyout_subtotal || 0),
        buyout_marked_up: Number(b.buyout_marked_up || 0),
        assembly_labor_cost: Number(b.assembly_labor_cost || 0),
        electrical_labor_cost: Number(b.electrical_labor_cost || 0),
        machined_subtotal: Number(b.machined_subtotal || 0),
        laser_hours: Number(b.laser_hours || 0),
        brake_hours: Number(b.brake_hours || 0),
        weld_hours: Number(b.weld_hours || 0),
        assembly_hours: Number(b.assembly_hours || 0),
        electrical_hours: Number(b.electrical_hours || 0),
        subtotal_before_oh: Number(b.cogs || 0) - Number(b.overhead || 0) - Number(b.consumables || 0),
        overhead: Number(b.overhead || 0),
        consumables: Number(b.consumables || 0),
        cogs: Number(b.cogs || 0),
        sell_price: Number(b.sell_price || wb.grand_total || 0),
        target_margin: Number(b.target_margin || 0.3),
      });
    }
  }, []);

  const loadEstimate = useCallback(
    async (id: number) => {
      setLoading(true);
      setLoadError('');
      try {
        const wb: WorkbenchResponse = await api.getEstimateWorkbench(id);
        hydrateFromWorkbench(wb);
      } catch (err: any) {
        setLoadError(err?.response?.data?.detail || 'Failed to load estimate workbench');
      } finally {
        setLoading(false);
      }
    },
    [hydrateFromWorkbench]
  );

  useEffect(() => {
    if (estimateIdParam && estimateIdParam !== 'new') {
      const id = Number(estimateIdParam);
      if (Number.isFinite(id)) {
        void loadEstimate(id);
      }
    }
  }, [estimateIdParam, loadEstimate]);

  // Auto-create when landing on /new?rfq_package_id=
  useEffect(() => {
    if (estimateIdParam !== 'new') return;
    const pkgId = Number(searchParams.get('rfq_package_id') || 0);
    if (!pkgId) return;
    let cancelled = false;
    (async () => {
      setLoading(true);
      setLoadError('');
      try {
        const wb: WorkbenchResponse = await api.createEstimateWorkbench(pkgId);
        if (cancelled) return;
        hydrateFromWorkbench(wb);
        navigate(`/estimate-workbench/${wb.estimate_id}`, { replace: true });
      } catch (err: any) {
        if (!cancelled) {
          setLoadError(err?.response?.data?.detail || 'Failed to create workbench');
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [estimateIdParam, searchParams, hydrateFromWorkbench, navigate]);

  // Debounced live recalc
  useEffect(() => {
    if (skipNextRecalc.current) {
      skipNextRecalc.current = false;
      return;
    }
    if (!debouncedDraft) return;
    try {
      const parsed = JSON.parse(debouncedDraft) as {
        assemblies: AssemblyDraft[];
        machinedParts: MachinedLineDraft[];
      };
      void runRecalc(parsed.assemblies, parsed.machinedParts);
    } catch {
      /* ignore */
    }
  }, [debouncedDraft, runRecalc]);

  const markDirty = () => setDirty(true);

  const updateAssembly = (idx: number, patch: Partial<AssemblyDraft>) => {
    setAssemblies((prev) => prev.map((a, i) => (i === idx ? { ...a, ...patch } : a)));
    markDirty();
  };

  const updateFab = (asmIdx: number, fabIdx: number, patch: Partial<FabLineDraft>) => {
    setAssemblies((prev) =>
      prev.map((a, i) => {
        if (i !== asmIdx) return a;
        const fab_lines = a.fab_lines.map((f, j) => (j === fabIdx ? { ...f, ...patch } : f));
        return { ...a, fab_lines };
      })
    );
    markDirty();
  };

  const updateBuyout = (asmIdx: number, buyIdx: number, patch: Partial<BuyoutLineDraft>) => {
    setAssemblies((prev) =>
      prev.map((a, i) => {
        if (i !== asmIdx) return a;
        const buyout_lines = a.buyout_lines.map((b, j) =>
          j === buyIdx ? { ...b, ...patch, extended_cost: (patch.qty ?? b.qty) * (patch.unit_cost ?? b.unit_cost) } : b
        );
        return { ...a, buyout_lines };
      })
    );
    markDirty();
  };

  const updateMachined = (idx: number, patch: Partial<MachinedLineDraft>) => {
    setMachinedParts((prev) => prev.map((m, i) => (i === idx ? { ...m, ...patch } : m)));
    markDirty();
  };

  const handleCreate = async () => {
    const pkgId = Number(rfqPackageId);
    if (!pkgId) {
      showToast('error', 'Enter an RFQ package ID');
      return;
    }
    setLoading(true);
    setLoadError('');
    try {
      const wb: WorkbenchResponse = await api.createEstimateWorkbench(pkgId);
      hydrateFromWorkbench(wb);
      navigate(`/estimate-workbench/${wb.estimate_id}`, { replace: true });
      showToast('success', 'Workbench created');
    } catch (err: any) {
      setLoadError(err?.response?.data?.detail || 'Failed to create workbench');
    } finally {
      setLoading(false);
    }
  };

  const handleSave = async () => {
    if (!estimateId) {
      showToast('error', 'Create or open a workbench first');
      return;
    }
    setSaving(true);
    try {
      const wb: WorkbenchResponse = await api.saveEstimateWorkbench(estimateId, {
        version,
        assemblies: assemblies.map((a, i) => ({
          name: a.name,
          sort_order: i,
          assembly_labor_hrs: a.assembly_labor_hrs,
          electrical_labor_hrs: a.electrical_labor_hrs,
          notes: a.notes,
          fab_lines: a.fab_lines.map((f, j) => ({
            sort_order: j,
            detail_name: f.detail_name,
            part_number: f.part_number,
            material: f.material,
            material_family_override: f.material_family_override,
            qty: f.qty,
            thickness_in: f.thickness_in,
            width_in: f.width_in,
            length_in: f.length_in,
            cut_length_in: f.cut_length_in,
            pierce_count: f.pierce_count,
            bend_count: f.bend_count,
            weld_length_in: f.weld_length_in,
            weld_minutes_ea: f.weld_minutes_ea,
            include_material: f.include_material,
            include_laser: f.include_laser,
            include_brake: f.include_brake,
            include_weld: f.include_weld,
            confidence: f.confidence || 'review',
            verification_note: f.verification_note,
          })),
          buyout_lines: a.buyout_lines.map((b, j) => ({
            sort_order: j,
            description: b.description,
            qty: b.qty,
            unit_cost: b.unit_cost,
            category: b.category,
            vendor: b.vendor,
            part_number: b.part_number,
            part_id: b.part_id,
            price_source: b.price_source,
            confidence: b.confidence || 'review',
            verification_note: b.verification_note,
          })),
        })),
        machined_parts: machinedParts.map((m, i) => ({
          sort_order: i,
          description: m.description,
          material: m.material,
          qty: m.qty,
          part_number: m.part_number,
          stock_dia_in: m.stock_dia_in,
          stock_length_in: m.stock_length_in,
          turning_minutes: m.turning_minutes,
          milling_minutes: m.milling_minutes,
          confidence: m.confidence || 'review',
          verification_note: m.verification_note,
        })),
      });
      hydrateFromWorkbench(wb);
      showToast('success', 'Estimate saved');
    } catch (err: any) {
      const detail = err?.response?.data?.detail;
      if (err?.response?.status === 409) {
        const current = detail?.current_version;
        showToast(
          'error',
          `Estimate changed (v${current ?? '?'}) — reload and merge your edits`
        );
      } else {
        showToast(
          'error',
          typeof detail === 'string' ? detail : detail?.message || 'Save failed'
        );
      }
    } finally {
      setSaving(false);
    }
  };

  const handleFinalize = async () => {
    if (!estimateId) return;
    if (dirty) {
      showToast('error', 'Save your changes before finalizing');
      return;
    }
    setFinalizing(true);
    try {
      const result = await api.finalizeEstimateWorkbench(estimateId, { valid_days: 30 });
      setQuoteId(result.quote_id);
      setQuoteNumber(result.quote_number);
      setVerification(result.verification || null);
      showToast('success', `Finalized → quote ${result.quote_number}`);
      // Reload tree so quote_id + rate snapshot are fresh
      const wb: WorkbenchResponse = await api.getEstimateWorkbench(estimateId);
      hydrateFromWorkbench(wb);
    } catch (err: any) {
      const detail = err?.response?.data?.detail;
      if (err?.response?.status === 422) {
        const msg =
          typeof detail === 'string'
            ? detail
            : detail?.message || 'Cannot finalize — resolve Review items first';
        showToast('error', msg);
        if (detail?.blockers) {
          setVerification((prev) =>
            prev
              ? {
                  ...prev,
                  can_finalize: false,
                  blockers: detail.blockers,
                  banner: msg,
                  review_count: detail.blocker_count ?? prev.review_count,
                }
              : prev
          );
        }
      } else {
        showToast(
          'error',
          typeof detail === 'string' ? detail : detail?.message || 'Finalize failed'
        );
      }
    } finally {
      setFinalizing(false);
    }
  };

  const handleExtractFromRfq = async () => {
    if (!estimateId) {
      showToast('error', 'Create or open a workbench first');
      return;
    }
    if (dirty) {
      showToast('error', 'Save or discard edits before extracting');
      return;
    }
    setExtracting(true);
    setExtractionPreview(null);
    try {
      const result: ExtractFromRfqResponse = await api.extractEstimateWorkbenchFromRfq(estimateId, {
        use_llm: true,
        apply: false,
        rfq_package_id: rfqPackageId ? Number(rfqPackageId) : undefined,
      });
      setExtractionPreview(result);
      const s = result.summary;
      showToast(
        'success',
        `Extracted ${s.fab_count} fab / ${s.buyout_count} buyout (${result.mode}) — review then apply`
      );
    } catch (err: any) {
      const detail = err?.response?.data?.detail;
      showToast(
        'error',
        typeof detail === 'string' ? detail : detail?.message || 'Extraction failed'
      );
    } finally {
      setExtracting(false);
    }
  };

  const applyExtractionPreview = (mode: 'replace' | 'merge') => {
    if (!extractionPreview) return;
    const incoming = extractionPreview.assemblies || [];
    if (!incoming.length) {
      showToast('error', 'Nothing to apply');
      return;
    }
    if (mode === 'replace') {
      setAssemblies(
        incoming.map((a, i) => ({
          name: a.name || `Assembly ${i + 1}`,
          assembly_labor_hrs: a.assembly_labor_hrs || 0,
          electrical_labor_hrs: a.electrical_labor_hrs || 0,
          notes: a.notes ?? null,
          sort_order: a.sort_order ?? i,
          fab_lines: (a.fab_lines || []).map((fl, j) => ({
            ...emptyFabLine(j),
            ...fl,
            qty: fl.qty || 1,
            pierce_count: fl.pierce_count || 0,
            bend_count: fl.bend_count || 0,
            include_material: fl.include_material ?? true,
            include_laser: fl.include_laser ?? true,
            include_brake: fl.include_brake ?? true,
            include_weld: fl.include_weld ?? Boolean(fl.weld_length_in),
          })),
          buyout_lines: (a.buyout_lines || []).map((bl) => ({
            ...emptyBuyoutLine(),
            ...bl,
            qty: bl.qty || 1,
            unit_cost: bl.unit_cost || 0,
          })),
        }))
      );
      setMachinedParts(extractionPreview.machined_parts || []);
      setActiveAssemblyIdx(0);
    } else {
      setAssemblies((prev) => {
        if (!prev.length) return incoming as AssemblyDraft[];
        const next = [...prev];
        const target = { ...next[0] };
        const src = incoming[0];
        target.fab_lines = [
          ...target.fab_lines,
          ...(src.fab_lines || []).map((fl, j) => ({
            ...emptyFabLine(target.fab_lines.length + j),
            ...fl,
            qty: fl.qty || 1,
            pierce_count: fl.pierce_count || 0,
            bend_count: fl.bend_count || 0,
            include_material: fl.include_material ?? true,
            include_laser: fl.include_laser ?? true,
            include_brake: fl.include_brake ?? true,
            include_weld: fl.include_weld ?? Boolean(fl.weld_length_in),
          })),
        ];
        target.buyout_lines = [
          ...target.buyout_lines,
          ...(src.buyout_lines || []).map((bl) => ({
            ...emptyBuyoutLine(),
            ...bl,
            qty: bl.qty || 1,
            unit_cost: bl.unit_cost || 0,
          })),
        ];
        next[0] = target;
        return next;
      });
    }
    setDirty(true);
    setVerification(null);
    setExtractionPreview(null);
    showToast('success', mode === 'replace' ? 'Replaced lines from extraction' : 'Merged extraction into assembly');
  };

  const handleExportAuditXlsx = async () => {
    if (!estimateId) return;
    setExporting('audit');
    try {
      const blob = await api.exportEstimateWorkbenchAuditXlsx(estimateId);
      downloadBlob(blob, `EW-${estimateId}_workbench_audit.xlsx`);
      showToast('success', 'Internal audit Excel downloaded');
    } catch (err: any) {
      showToast('error', err?.response?.data?.detail || 'Audit export failed');
    } finally {
      setExporting(null);
    }
  };

  const handleExportAuditJson = async () => {
    if (!estimateId) return;
    setExporting('json');
    try {
      const blob = await api.exportEstimateWorkbenchAuditJson(estimateId);
      downloadBlob(blob, `EW-${estimateId}_workbench_audit.json`);
      showToast('success', 'Internal audit JSON downloaded');
    } catch (err: any) {
      showToast('error', err?.response?.data?.detail || 'JSON export failed');
    } finally {
      setExporting(null);
    }
  };

  const handleExportCustomerPdf = async () => {
    if (!estimateId) return;
    if (dirty) {
      showToast('error', 'Save before exporting customer PDF');
      return;
    }
    setExporting('pdf');
    try {
      const blob = await api.exportEstimateWorkbenchCustomerPdf(estimateId);
      downloadBlob(blob, `EW-${estimateId}_customer_quote.pdf`);
      showToast('success', 'Customer PDF downloaded');
    } catch (err: any) {
      const detail = err?.response?.data?.detail;
      let msg = 'Customer PDF export failed';
      if (typeof detail === 'string') msg = detail;
      else if (detail?.message) msg = detail.message;
      else if (err?.response?.status === 422) msg = 'Resolve Review items before customer PDF';
      showToast('error', msg);
    } finally {
      setExporting(null);
    }
  };

  const jumpToAction = (anchor: string) => {
    // Prefer scrolling to a persisted row id; fall back to first matching category section
    const el = document.getElementById(anchor);
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'center' });
      el.classList.add('ring-2', 'ring-amber-400');
      window.setTimeout(() => el.classList.remove('ring-2', 'ring-amber-400'), 1600);
      return;
    }
    // Client-side drafts may not have ids yet — jump to section
    if (anchor.startsWith('fab')) {
      document.getElementById('section-fab')?.scrollIntoView({ behavior: 'smooth' });
    } else if (anchor.startsWith('buyout')) {
      document.getElementById('section-buyout')?.scrollIntoView({ behavior: 'smooth' });
    } else if (anchor.startsWith('machined')) {
      document.getElementById('section-machined')?.scrollIntoView({ behavior: 'smooth' });
    }
  };

  const reviewCount = useMemo(() => {
    if (verification && !dirty) return verification.review_count;
    let n = 0;
    for (const a of assemblies) {
      for (const f of a.fab_lines) if ((f.confidence || 'review') === 'review') n += 1;
      for (const b of a.buyout_lines) if ((b.confidence || 'review') === 'review') n += 1;
    }
    for (const m of machinedParts) if ((m.confidence || 'review') === 'review') n += 1;
    return n;
  }, [assemblies, machinedParts, verification, dirty]);

  const canFinalize = Boolean(
    estimateId && !dirty && verification?.can_finalize && !quoteId
  );

  const active = assemblies[activeAssemblyIdx] || assemblies[0];

  if (loadError && !estimateId) {
    return (
      <div className="space-y-6">
        <ErrorState title="Estimate Workbench" message={loadError} onRetry={() => setLoadError('')} />
      </div>
    );
  }

  if (!estimateId && estimateIdParam !== 'new') {
    return (
      <div className="space-y-6">
        <div>
          <h1 className="text-2xl font-bold text-white flex items-center gap-2">
            <CalculatorIcon className="h-7 w-7 text-werco-navy-400" />
            Estimate Workbench
          </h1>
          <p className="text-sm text-fd-muted mt-1">
            Spreadsheet-style fab estimating with thickness-banded machine rates.
          </p>
        </div>
        <div className="bg-fd-panel border border-fd-line rounded-sm p-4 space-y-4 max-w-lg">
          <label className="block text-sm text-fd-muted">
            RFQ Package ID
            <input
              className={`${inputCls} mt-1`}
              value={rfqPackageId}
              onChange={(e) => setRfqPackageId(e.target.value)}
              placeholder="e.g. 12"
            />
          </label>
          <div className="flex gap-2">
            <LoadingButton loading={loading} onClick={handleCreate} variant="primary">
              Create workbench
            </LoadingButton>
            <Link to="/rfq-packages/new" className="btn-secondary inline-flex items-center px-3 text-sm">
              Open AI RFQ
            </Link>
          </div>
          {loadError && <p className="text-sm text-red-400">{loadError}</p>}
        </div>
      </div>
    );
  }

  if (loading && !active) {
    return (
      <div className="space-y-6">
        <p className="text-fd-muted">Loading workbench…</p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold text-white flex items-center gap-2">
            <CalculatorIcon className="h-7 w-7 text-werco-navy-400" />
            Estimate Workbench
            {estimateId != null && (
              <span className="text-base font-normal text-fd-muted">#{estimateId}</span>
            )}
          </h1>
          <p className="text-sm text-fd-muted mt-1">
            v{version}
            {dirty ? ' · unsaved' : ''}
            {recalcing ? ' · recalculating…' : ''}
            {' · '}shop data: {shopSource}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <LoadingButton
            loading={extracting}
            onClick={handleExtractFromRfq}
            variant="secondary"
            size="sm"
            disabled={!estimateId || Boolean(quoteId)}
            title="Run triple-pass PDF/BOM extract from the linked RFQ package"
          >
            Extract from RFQ
          </LoadingButton>
          <LoadingButton
            loading={exporting === 'audit'}
            onClick={handleExportAuditXlsx}
            variant="secondary"
            size="sm"
            disabled={!estimateId}
            title="Internal audit Excel (hours, confidence, rate snapshot)"
          >
            <ArrowDownTrayIcon className="h-4 w-4 mr-1" />
            Audit XLSX
          </LoadingButton>
          <LoadingButton
            loading={exporting === 'json'}
            onClick={handleExportAuditJson}
            variant="secondary"
            size="sm"
            disabled={!estimateId}
            title="Internal audit JSON"
          >
            Audit JSON
          </LoadingButton>
          <LoadingButton
            loading={exporting === 'pdf'}
            onClick={handleExportCustomerPdf}
            variant="secondary"
            size="sm"
            disabled={!estimateId || (reviewCount > 0 && !quoteId)}
            title={
              reviewCount > 0 && !quoteId
                ? 'Resolve Review items before customer PDF'
                : 'Customer PDF (no internal rates)'
            }
          >
            Customer PDF
          </LoadingButton>
          <Button
            variant="secondary"
            size="sm"
            onClick={() => void runRecalc(assemblies, machinedParts)}
          >
            <ArrowPathIcon className="h-4 w-4 mr-1" />
            Recalc
          </Button>
          <LoadingButton loading={saving} onClick={handleSave} variant="primary" size="sm">
            Save
          </LoadingButton>
          {quoteId ? (
            <Link
              to="/quotes"
              className="btn-secondary inline-flex items-center px-3 text-sm"
              title={quoteNumber || `Quote #${quoteId}`}
            >
              <CheckBadgeIcon className="h-4 w-4 mr-1" />
              {quoteNumber || `Quote #${quoteId}`}
            </Link>
          ) : (
            <LoadingButton
              loading={finalizing}
              onClick={handleFinalize}
              variant="primary"
              size="sm"
              disabled={!canFinalize}
              title={
                dirty
                  ? 'Save before finalizing'
                  : reviewCount > 0
                    ? 'Resolve Review items first'
                    : 'Create customer quote from this estimate'
              }
            >
              <CheckBadgeIcon className="h-4 w-4 mr-1" />
              Finalize bid
            </LoadingButton>
          )}
        </div>
      </div>

      {(verification?.banner || reviewCount > 0) && !quoteId && (
        <div className="border-l-4 border-red-600 bg-[#F8CBCB]/10 px-3 py-2 text-sm text-[#F8CBCB] flex items-start gap-2">
          <ExclamationTriangleIcon className="h-5 w-5 text-red-400 shrink-0 mt-0.5" />
          <div>
            <strong className="text-red-300">
              {verification?.banner ||
                `${reviewCount} item${reviewCount === 1 ? '' : 's'} need review before this bid can be finalized`}
            </strong>
            <p className="text-xs text-red-200/80 mt-0.5">
              Resolve every Review-flagged line (and add notes on Review buyouts) before Finalize is enabled.
            </p>
          </div>
        </div>
      )}

      {extractionPreview && (
        <div className="bg-fd-panel border border-werco-navy-600/50 rounded-sm p-4 space-y-3">
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div>
              <h2 className="text-sm font-semibold text-white">Extraction staging</h2>
              <p className="text-xs text-fd-muted mt-0.5">
                Mode: {extractionPreview.mode} · {extractionPreview.summary.fab_count} fab ·{' '}
                {extractionPreview.summary.buyout_count} buyout ·{' '}
                {extractionPreview.summary.confirmed_count} confirmed /{' '}
                {extractionPreview.summary.majority_count} majority /{' '}
                {extractionPreview.summary.review_count} review
              </p>
            </div>
            <div className="flex flex-wrap gap-2">
              <Button variant="secondary" size="sm" onClick={() => setExtractionPreview(null)}>
                Dismiss
              </Button>
              <Button variant="secondary" size="sm" onClick={() => applyExtractionPreview('merge')}>
                Merge into assembly
              </Button>
              <Button variant="primary" size="sm" onClick={() => applyExtractionPreview('replace')}>
                Replace lines
              </Button>
            </div>
          </div>
          {extractionPreview.warnings?.length > 0 && (
            <ul className="text-xs text-amber-200/90 list-disc pl-4 space-y-0.5">
              {extractionPreview.warnings.slice(0, 6).map((w) => (
                <li key={w}>{w}</li>
              ))}
            </ul>
          )}
          <div className="overflow-x-auto max-h-48 overflow-y-auto">
            <table className="w-full text-xs text-left">
              <thead className="text-fd-muted border-b border-fd-line">
                <tr>
                  <th className="py-1 pr-2">Type</th>
                  <th className="py-1 pr-2">Name</th>
                  <th className="py-1 pr-2">Material / desc</th>
                  <th className="py-1 pr-2">Thk</th>
                  <th className="py-1 pr-2">Bends</th>
                  <th className="py-1 pr-2">Conf</th>
                  <th className="py-1">Note</th>
                </tr>
              </thead>
              <tbody>
                {(extractionPreview.assemblies[0]?.fab_lines || []).map((fl, i) => (
                  <tr key={`xf-${i}`} className="border-b border-fd-line/40">
                    <td className="py-1 pr-2 text-fd-muted">Fab</td>
                    <td className="py-1 pr-2">{fl.detail_name || fl.part_number}</td>
                    <td className="py-1 pr-2">{fl.material}</td>
                    <td className="py-1 pr-2 tabular-nums">{fl.thickness_in ?? '—'}</td>
                    <td className="py-1 pr-2 tabular-nums">{fl.bend_count}</td>
                    <td className="py-1 pr-2">
                      <StatusBadge status={String(fl.confidence || 'review')} />
                    </td>
                    <td className="py-1 text-fd-muted truncate max-w-xs">
                      {fl.verification_note || ''}
                    </td>
                  </tr>
                ))}
                {(extractionPreview.assemblies[0]?.buyout_lines || []).map((bl, i) => (
                  <tr key={`xb-${i}`} className="border-b border-fd-line/40">
                    <td className="py-1 pr-2 text-fd-muted">Buy</td>
                    <td className="py-1 pr-2">{bl.part_number || '—'}</td>
                    <td className="py-1 pr-2">{bl.description}</td>
                    <td className="py-1 pr-2">—</td>
                    <td className="py-1 pr-2">—</td>
                    <td className="py-1 pr-2">
                      <StatusBadge status={String(bl.confidence || 'review')} />
                    </td>
                    <td className="py-1 text-fd-muted truncate max-w-xs">
                      {bl.verification_note || ''}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {quoteId && (
        <div className="border-l-4 border-green-600 bg-[#C6EFCE]/10 px-3 py-2 text-sm text-green-200">
          Bid finalized{quoteNumber ? ` as ${quoteNumber}` : ''}. Rate snapshot frozen on this estimate.
        </div>
      )}

      <div className="grid grid-cols-1 xl:grid-cols-[1fr_300px] gap-4">
        <div className="space-y-4 min-w-0">
          {verification && (
            <VerificationPanel
              report={verification}
              onJump={jumpToAction}
            />
          )}
          {/* Assembly tabs */}
          <div className="bg-fd-panel border border-fd-line rounded-sm p-3 space-y-3">
            <div className="flex flex-wrap items-center gap-2">
              {assemblies.map((a, i) => (
                <button
                  key={i}
                  type="button"
                  onClick={() => setActiveAssemblyIdx(i)}
                  className={`px-3 py-1 text-sm rounded-sm border ${
                    i === activeAssemblyIdx
                      ? 'bg-werco-navy-600 text-white border-werco-navy-500'
                      : 'bg-fd-sunken text-fd-muted border-fd-line hover:text-white'
                  }`}
                >
                  {a.name || `Assembly ${i + 1}`}
                </button>
              ))}
              <Button
                variant="ghost"
                size="sm"
                onClick={() => {
                  setAssemblies((prev) => [...prev, emptyAssembly(prev.length)]);
                  setActiveAssemblyIdx(assemblies.length);
                  markDirty();
                }}
              >
                <PlusIcon className="h-4 w-4 mr-1" />
                Assembly
              </Button>
            </div>

            {active && (
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
                <label className="text-xs text-fd-muted">
                  Name
                  <input
                    className={`${inputCls} mt-0.5`}
                    value={active.name}
                    onChange={(e) => updateAssembly(activeAssemblyIdx, { name: e.target.value })}
                  />
                </label>
                <label className="text-xs text-fd-muted">
                  Assembly labor (hrs)
                  <input
                    className={`${inputCls} mt-0.5`}
                    type="number"
                    step="0.1"
                    value={active.assembly_labor_hrs}
                    onChange={(e) =>
                      updateAssembly(activeAssemblyIdx, {
                        assembly_labor_hrs: numOrZero(e.target.value),
                      })
                    }
                  />
                </label>
                <label className="text-xs text-fd-muted">
                  Electrical labor (hrs)
                  <input
                    className={`${inputCls} mt-0.5`}
                    type="number"
                    step="0.1"
                    value={active.electrical_labor_hrs}
                    onChange={(e) =>
                      updateAssembly(activeAssemblyIdx, {
                        electrical_labor_hrs: numOrZero(e.target.value),
                      })
                    }
                  />
                </label>
              </div>
            )}
          </div>

          {/* Fab lines */}
          {active && (
            <div id="section-fab">
            <FabTable
              lines={active.fab_lines}
              onChange={(fabIdx, patch) => updateFab(activeAssemblyIdx, fabIdx, patch)}
              onAdd={() => {
                updateAssembly(activeAssemblyIdx, {
                  fab_lines: [...active.fab_lines, emptyFabLine(active.fab_lines.length)],
                });
              }}
              onRemove={(fabIdx) => {
                updateAssembly(activeAssemblyIdx, {
                  fab_lines: active.fab_lines.filter((_, j) => j !== fabIdx),
                });
              }}
            />
            </div>
          )}

          {/* Buyouts */}
          {active && (
            <div id="section-buyout">
            <BuyoutTable
              lines={active.buyout_lines}
              onChange={(buyIdx, patch) => updateBuyout(activeAssemblyIdx, buyIdx, patch)}
              onAdd={() => {
                updateAssembly(activeAssemblyIdx, {
                  buyout_lines: [...active.buyout_lines, emptyBuyoutLine()],
                });
              }}
              onRemove={(buyIdx) => {
                updateAssembly(activeAssemblyIdx, {
                  buyout_lines: active.buyout_lines.filter((_, j) => j !== buyIdx),
                });
              }}
            />
            </div>
          )}

          {/* Machined */}
          <div id="section-machined">
          <MachinedTable
            lines={machinedParts}
            onChange={updateMachined}
            onAdd={() => {
              setMachinedParts((prev) => [...prev, emptyMachinedLine(prev.length)]);
              markDirty();
            }}
            onRemove={(idx) => {
              setMachinedParts((prev) => prev.filter((_, i) => i !== idx));
              markDirty();
            }}
          />
          </div>
        </div>

        {/* Bid summary */}
        <BidSummaryPanel
          summary={bidSummary}
          canFinalize={canFinalize}
          finalizing={finalizing}
          onFinalize={handleFinalize}
          quoteId={quoteId}
          quoteNumber={quoteNumber}
        />
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Subcomponents                                                              */
/* -------------------------------------------------------------------------- */

function VerificationPanel({
  report,
  onJump,
}: {
  report: VerificationReport;
  onJump: (anchor: string) => void;
}) {
  return (
    <div className="bg-fd-panel border border-fd-line rounded-sm p-3 space-y-3">
      <div className="flex items-center justify-between gap-2">
        <h2 className="text-sm font-semibold text-white">Bid Verification</h2>
        <StatusBadge status={report.status === 'ready_to_send' ? 'ready' : 'review'} />
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
        {report.categories.map((cat) => (
          <div
            key={cat.label}
            className="rounded-sm bg-fd-sunken border border-fd-line p-2 text-xs"
          >
            <div className="text-fd-muted">{cat.label}</div>
            <div className="tabular-nums text-white font-medium mt-0.5">{money(cat.total)}</div>
            <div className="text-[10px] text-fd-muted mt-1">
              {cat.count} line{cat.count === 1 ? '' : 's'}
            </div>
            <div className="flex gap-1 mt-1 text-[10px]">
              <span className="text-green-400">{cat.confirmed}✓</span>
              <span className="text-amber-400">{cat.majority}~</span>
              <span className="text-red-400">{cat.review}!</span>
            </div>
          </div>
        ))}
      </div>
      {report.priority_actions.length > 0 && (
        <div>
          <h3 className="text-xs font-semibold text-red-300 mb-1.5 flex items-center gap-1">
            <ExclamationTriangleIcon className="h-3.5 w-3.5" />
            Priority Action Items
          </h3>
          <ul className="space-y-1 max-h-48 overflow-y-auto">
            {report.priority_actions.map((a) => (
              <li key={`${a.category}-${a.line_id}-${a.reason}`}>
                <button
                  type="button"
                  onClick={() => onJump(a.anchor)}
                  className="w-full text-left text-xs px-2 py-1.5 rounded-sm border border-red-900/50 bg-red-950/20 hover:bg-red-950/40 text-red-100"
                >
                  <span className="font-medium text-white">{a.label}</span>
                  {a.assembly_name ? (
                    <span className="text-fd-muted"> · {a.assembly_name}</span>
                  ) : null}
                  <div className="text-red-200/80 mt-0.5">{a.reason}</div>
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
      {report.can_finalize && (
        <p className="text-xs text-green-300">All lines clear — ready to finalize.</p>
      )}
    </div>
  );
}

function ConfidenceSelect({
  value,
  onChange,
}: {
  value?: string;
  onChange: (v: string) => void;
}) {
  return (
    <div className="flex items-center gap-1">
      <StatusBadge status={value || 'review'} />
      <select
        className={`${inputCls} w-[6.5rem]`}
        value={value || 'review'}
        onChange={(e) => onChange(e.target.value)}
      >
        {CONFIDENCE_OPTIONS.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </div>
  );
}

function FabTable({
  lines,
  onChange,
  onAdd,
  onRemove,
}: {
  lines: FabLineDraft[];
  onChange: (idx: number, patch: Partial<FabLineDraft>) => void;
  onAdd: () => void;
  onRemove: (idx: number) => void;
}) {
  return (
    <div className="bg-fd-panel border border-fd-line rounded-sm overflow-hidden">
      <div className="flex items-center justify-between px-3 py-2 border-b border-fd-line">
        <h2 className="text-sm font-semibold text-white">Fab line items</h2>
        <Button variant="ghost" size="sm" onClick={onAdd}>
          <PlusIcon className="h-4 w-4 mr-1" />
          Row
        </Button>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-xs text-left min-w-[1100px]">
          <thead className="bg-fd-sunken text-fd-muted">
            <tr>
              <th className="px-2 py-1.5 font-medium">Conf</th>
              <th className="px-2 py-1.5 font-medium">Detail</th>
              <th className="px-2 py-1.5 font-medium">Material</th>
              <th className="px-2 py-1.5 font-medium">Qty</th>
              <th className="px-2 py-1.5 font-medium">Thk</th>
              <th className="px-2 py-1.5 font-medium">W</th>
              <th className="px-2 py-1.5 font-medium">L</th>
              <th className="px-2 py-1.5 font-medium">Cut</th>
              <th className="px-2 py-1.5 font-medium">Pierce</th>
              <th className="px-2 py-1.5 font-medium">Bends</th>
              <th className="px-2 py-1.5 font-medium" title="Ops in scope">
                M/L/B/W
              </th>
              <th className="px-2 py-1.5 font-medium text-right">Mat $</th>
              <th className="px-2 py-1.5 font-medium text-right">Laser</th>
              <th className="px-2 py-1.5 font-medium text-right">Brake</th>
              <th className="px-2 py-1.5 font-medium text-right">Weld</th>
              <th className="px-2 py-1.5 font-medium text-right">Total</th>
              <th className="px-2 py-1.5" aria-label="Actions" />
            </tr>
          </thead>
          <tbody>
            {lines.map((fl, idx) => {
              const border =
                (fl.confidence || 'review') === 'confirmed'
                  ? 'border-l-4 border-l-green-600'
                  : (fl.confidence || 'review') === 'majority'
                    ? 'border-l-4 border-l-amber-500'
                    : 'border-l-4 border-l-red-600';
              const hasError = (fl.calc_errors || []).length > 0;
              return (
                <tr
                  key={idx}
                  id={fl.id != null ? `fab-${fl.id}` : undefined}
                  className={`border-t border-fd-line hover:bg-fd-sunken/40 ${border} ${
                    hasError ? 'bg-red-950/20' : ''
                  }`}
                >
                  <td className="px-1 py-1 align-top">
                    <ConfidenceSelect
                      value={fl.confidence}
                      onChange={(v) => onChange(idx, { confidence: v })}
                    />
                  </td>
                  <td className="px-1 py-1 align-top min-w-[8rem]">
                    <input
                      className={inputCls}
                      aria-label="Detail name"
                      value={fl.detail_name}
                      onChange={(e) => onChange(idx, { detail_name: e.target.value })}
                    />
                  </td>
                  <td className="px-1 py-1 align-top min-w-[7rem]">
                    <input
                      className={inputCls}
                      aria-label="Material"
                      value={fl.material}
                      onChange={(e) => onChange(idx, { material: e.target.value })}
                    />
                  </td>
                  <td className="px-1 py-1 align-top w-14">
                    <input
                      className={inputCls}
                      type="number"
                      aria-label="Quantity"
                      value={fl.qty}
                      onChange={(e) => onChange(idx, { qty: Math.max(1, numOrZero(e.target.value)) })}
                    />
                  </td>
                  <td className="px-1 py-1 align-top w-16">
                    <input
                      className={inputCls}
                      type="number"
                      step="0.001"
                      aria-label="Thickness (in)"
                      value={fl.thickness_in ?? ''}
                      onChange={(e) => onChange(idx, { thickness_in: numOrNull(e.target.value) })}
                    />
                  </td>
                  <td className="px-1 py-1 align-top w-14">
                    <input
                      className={inputCls}
                      type="number"
                      step="0.01"
                      aria-label="Width (in)"
                      value={fl.width_in ?? ''}
                      onChange={(e) => onChange(idx, { width_in: numOrNull(e.target.value) })}
                    />
                  </td>
                  <td className="px-1 py-1 align-top w-14">
                    <input
                      className={inputCls}
                      type="number"
                      step="0.01"
                      aria-label="Length (in)"
                      value={fl.length_in ?? ''}
                      onChange={(e) => onChange(idx, { length_in: numOrNull(e.target.value) })}
                    />
                  </td>
                  <td className="px-1 py-1 align-top w-16">
                    <input
                      className={inputCls}
                      type="number"
                      step="0.1"
                      aria-label="Cut length (in)"
                      value={fl.cut_length_in ?? ''}
                      onChange={(e) => onChange(idx, { cut_length_in: numOrNull(e.target.value) })}
                    />
                  </td>
                  <td className="px-1 py-1 align-top w-14">
                    <input
                      className={inputCls}
                      type="number"
                      aria-label="Pierce count"
                      value={fl.pierce_count}
                      onChange={(e) => onChange(idx, { pierce_count: numOrZero(e.target.value) })}
                    />
                  </td>
                  <td className="px-1 py-1 align-top w-14">
                    <input
                      className={inputCls}
                      type="number"
                      aria-label="Bend count"
                      value={fl.bend_count}
                      onChange={(e) => onChange(idx, { bend_count: numOrZero(e.target.value) })}
                    />
                  </td>
                  <td className="px-1 py-1 align-top">
                    <div className="flex gap-1 items-center">
                      {(
                        [
                          ['include_material', 'M'],
                          ['include_laser', 'L'],
                          ['include_brake', 'B'],
                          ['include_weld', 'W'],
                        ] as const
                      ).map(([key, label]) => (
                        <label key={key} className="flex items-center gap-0.5 text-fd-muted" title={key}>
                          <input
                            type="checkbox"
                            className={checkCls}
                            checked={Boolean(fl[key])}
                            onChange={(e) => onChange(idx, { [key]: e.target.checked })}
                          />
                          {label}
                        </label>
                      ))}
                    </div>
                  </td>
                  <td className="px-2 py-1 align-top text-right tabular-nums text-fd-muted">
                    {money(fl.material_cost)}
                  </td>
                  <td className="px-2 py-1 align-top text-right tabular-nums text-fd-muted">
                    <div>{money(fl.laser_cost)}</div>
                    <div className="text-[10px] opacity-70">{hours(fl.laser_hours)}</div>
                  </td>
                  <td className="px-2 py-1 align-top text-right tabular-nums text-fd-muted">
                    <div>{money(fl.brake_cost)}</div>
                    <div className="text-[10px] opacity-70">{hours(fl.brake_hours)}</div>
                  </td>
                  <td className="px-2 py-1 align-top text-right tabular-nums text-fd-muted">
                    <div>{money(fl.weld_cost)}</div>
                    <div className="text-[10px] opacity-70">{hours(fl.weld_hours)}</div>
                  </td>
                  <td className="px-2 py-1 align-top text-right tabular-nums text-white font-medium">
                    {money(fl.line_total)}
                  </td>
                  <td className="px-1 py-1 align-top">
                    <button
                      type="button"
                      className="text-fd-muted hover:text-red-400"
                      onClick={() => onRemove(idx)}
                      aria-label="Remove row"
                    >
                      <TrashIcon className="h-4 w-4" />
                    </button>
                  </td>
                </tr>
              );
            })}
            {lines.length === 0 && (
              <tr>
                <td colSpan={17} className="px-3 py-4 text-fd-muted text-center">
                  No fab lines — add a row to start quoting.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      {lines.some((l) => (l.calc_errors || []).length > 0) && (
        <div className="px-3 py-2 text-xs text-red-300 border-t border-fd-line space-y-0.5">
          {lines.flatMap((l, i) =>
            (l.calc_errors || []).map((e, j) => (
              <div key={`${i}-${j}`}>
                Row {i + 1}: {e.message}
              </div>
            ))
          )}
        </div>
      )}
    </div>
  );
}

function BuyoutTable({
  lines,
  onChange,
  onAdd,
  onRemove,
}: {
  lines: BuyoutLineDraft[];
  onChange: (idx: number, patch: Partial<BuyoutLineDraft>) => void;
  onAdd: () => void;
  onRemove: (idx: number) => void;
}) {
  return (
    <div className="bg-fd-panel border border-fd-line rounded-sm overflow-hidden">
      <div className="flex items-center justify-between px-3 py-2 border-b border-fd-line">
        <h2 className="text-sm font-semibold text-white">Buyout line items</h2>
        <Button variant="ghost" size="sm" onClick={onAdd}>
          <PlusIcon className="h-4 w-4 mr-1" />
          Row
        </Button>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-xs text-left min-w-[700px]">
          <thead className="bg-fd-sunken text-fd-muted">
            <tr>
              <th className="px-2 py-1.5 font-medium">Conf</th>
              <th className="px-2 py-1.5 font-medium">Description</th>
              <th className="px-2 py-1.5 font-medium">Vendor</th>
              <th className="px-2 py-1.5 font-medium">Qty</th>
              <th className="px-2 py-1.5 font-medium">Unit $</th>
              <th className="px-2 py-1.5 font-medium text-right">Ext $</th>
              <th className="px-2 py-1.5 font-medium">Source / note</th>
              <th className="px-2 py-1.5" aria-label="Actions" />
            </tr>
          </thead>
          <tbody>
            {lines.map((bl, idx) => (
              <tr
                key={idx}
                id={bl.id != null ? `buyout-${bl.id}` : undefined}
                className="border-t border-fd-line"
              >
                <td className="px-1 py-1">
                  <ConfidenceSelect
                    value={bl.confidence}
                    onChange={(v) => onChange(idx, { confidence: v })}
                  />
                </td>
                <td className="px-1 py-1 min-w-[10rem]">
                  <input
                    className={inputCls}
                    aria-label="Description"
                    value={bl.description}
                    onChange={(e) => onChange(idx, { description: e.target.value })}
                  />
                </td>
                <td className="px-1 py-1 w-28">
                  <input
                    className={inputCls}
                    aria-label="Vendor"
                    value={bl.vendor || ''}
                    onChange={(e) => onChange(idx, { vendor: e.target.value })}
                  />
                </td>
                <td className="px-1 py-1 w-16">
                  <input
                    className={inputCls}
                    type="number"
                    step="0.01"
                    aria-label="Quantity"
                    value={bl.qty}
                    onChange={(e) => onChange(idx, { qty: numOrZero(e.target.value) })}
                  />
                </td>
                <td className="px-1 py-1 w-20">
                  <input
                    className={inputCls}
                    type="number"
                    step="0.01"
                    aria-label="Unit cost"
                    value={bl.unit_cost}
                    onChange={(e) => onChange(idx, { unit_cost: numOrZero(e.target.value) })}
                  />
                </td>
                <td className="px-2 py-1 text-right tabular-nums text-white">
                  {money(bl.qty * bl.unit_cost)}
                </td>
                <td className="px-1 py-1 min-w-[8rem]">
                  <input
                    className={inputCls}
                    aria-label="Price source / note"
                    value={bl.price_source || bl.verification_note || ''}
                    onChange={(e) =>
                      onChange(idx, { price_source: e.target.value, verification_note: e.target.value })
                    }
                    placeholder="Price source / note"
                  />
                </td>
                <td className="px-1 py-1">
                  <button
                    type="button"
                    className="text-fd-muted hover:text-red-400"
                    onClick={() => onRemove(idx)}
                    aria-label="Remove buyout"
                  >
                    <TrashIcon className="h-4 w-4" />
                  </button>
                </td>
              </tr>
            ))}
            {lines.length === 0 && (
              <tr>
                <td colSpan={8} className="px-3 py-3 text-fd-muted text-center">
                  No buyouts.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function MachinedTable({
  lines,
  onChange,
  onAdd,
  onRemove,
}: {
  lines: MachinedLineDraft[];
  onChange: (idx: number, patch: Partial<MachinedLineDraft>) => void;
  onAdd: () => void;
  onRemove: (idx: number) => void;
}) {
  return (
    <div className="bg-fd-panel border border-fd-line rounded-sm overflow-hidden">
      <div className="flex items-center justify-between px-3 py-2 border-b border-fd-line">
        <h2 className="text-sm font-semibold text-white">Machined parts</h2>
        <Button variant="ghost" size="sm" onClick={onAdd}>
          <PlusIcon className="h-4 w-4 mr-1" />
          Row
        </Button>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-xs text-left min-w-[800px]">
          <thead className="bg-fd-sunken text-fd-muted">
            <tr>
              <th className="px-2 py-1.5 font-medium">Conf</th>
              <th className="px-2 py-1.5 font-medium">Description</th>
              <th className="px-2 py-1.5 font-medium">Material</th>
              <th className="px-2 py-1.5 font-medium">Qty</th>
              <th className="px-2 py-1.5 font-medium">Dia</th>
              <th className="px-2 py-1.5 font-medium">Len</th>
              <th className="px-2 py-1.5 font-medium">Turn min</th>
              <th className="px-2 py-1.5 font-medium">Mill min</th>
              <th className="px-2 py-1.5 font-medium text-right">Total</th>
              <th className="px-2 py-1.5" aria-label="Actions" />
            </tr>
          </thead>
          <tbody>
            {lines.map((mp, idx) => (
              <tr
                key={idx}
                id={mp.id != null ? `machined-${mp.id}` : undefined}
                className="border-t border-fd-line"
              >
                <td className="px-1 py-1">
                  <ConfidenceSelect
                    value={mp.confidence}
                    onChange={(v) => onChange(idx, { confidence: v })}
                  />
                </td>
                <td className="px-1 py-1 min-w-[8rem]">
                  <input
                    className={inputCls}
                    aria-label="Description"
                    value={mp.description}
                    onChange={(e) => onChange(idx, { description: e.target.value })}
                  />
                </td>
                <td className="px-1 py-1 w-28">
                  <input
                    className={inputCls}
                    aria-label="Material"
                    value={mp.material}
                    onChange={(e) => onChange(idx, { material: e.target.value })}
                  />
                </td>
                <td className="px-1 py-1 w-14">
                  <input
                    className={inputCls}
                    type="number"
                    aria-label="Quantity"
                    value={mp.qty}
                    onChange={(e) => onChange(idx, { qty: Math.max(1, numOrZero(e.target.value)) })}
                  />
                </td>
                <td className="px-1 py-1 w-16">
                  <input
                    className={inputCls}
                    type="number"
                    step="0.001"
                    aria-label="Stock diameter (in)"
                    value={mp.stock_dia_in ?? ''}
                    onChange={(e) => onChange(idx, { stock_dia_in: numOrNull(e.target.value) })}
                  />
                </td>
                <td className="px-1 py-1 w-16">
                  <input
                    className={inputCls}
                    type="number"
                    step="0.001"
                    aria-label="Stock length (in)"
                    value={mp.stock_length_in ?? ''}
                    onChange={(e) => onChange(idx, { stock_length_in: numOrNull(e.target.value) })}
                  />
                </td>
                <td className="px-1 py-1 w-16">
                  <input
                    className={inputCls}
                    type="number"
                    step="0.1"
                    aria-label="Turning minutes"
                    value={mp.turning_minutes}
                    onChange={(e) => onChange(idx, { turning_minutes: numOrZero(e.target.value) })}
                  />
                </td>
                <td className="px-1 py-1 w-16">
                  <input
                    className={inputCls}
                    type="number"
                    step="0.1"
                    aria-label="Milling minutes"
                    value={mp.milling_minutes}
                    onChange={(e) => onChange(idx, { milling_minutes: numOrZero(e.target.value) })}
                  />
                </td>
                <td className="px-2 py-1 text-right tabular-nums text-white">
                  <div>{money(mp.line_total)}</div>
                  <div className="text-[10px] text-fd-muted">
                    {hours((mp.turning_hours || 0) + (mp.milling_hours || 0))}
                  </div>
                </td>
                <td className="px-1 py-1">
                  <button
                    type="button"
                    className="text-fd-muted hover:text-red-400"
                    onClick={() => onRemove(idx)}
                    aria-label="Remove machined part"
                  >
                    <TrashIcon className="h-4 w-4" />
                  </button>
                </td>
              </tr>
            ))}
            {lines.length === 0 && (
              <tr>
                <td colSpan={10} className="px-3 py-3 text-fd-muted text-center">
                  No machined parts.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function BidSummaryPanel({
  summary,
  canFinalize,
  finalizing,
  onFinalize,
  quoteId,
  quoteNumber,
}: {
  summary: BidSummary | null;
  canFinalize?: boolean;
  finalizing?: boolean;
  onFinalize?: () => void;
  quoteId?: number | null;
  quoteNumber?: string | null;
}) {
  if (!summary) {
    return (
      <aside className="bg-fd-panel border border-fd-line rounded-sm p-3 h-fit sticky top-4">
        <h2 className="text-sm font-semibold text-white mb-2">Bid summary</h2>
        <p className="text-xs text-fd-muted">Enter geometry to see live costs.</p>
      </aside>
    );
  }

  const rows: Array<[string, string, string?]> = [
    ['Material', money(summary.fab_material)],
    ['Laser', money(summary.fab_laser), hours(summary.laser_hours)],
    ['Brake', money(summary.fab_brake), hours(summary.brake_hours)],
    ['Weld', money(summary.fab_weld), hours(summary.weld_hours)],
    ['Buyout (marked up)', money(summary.buyout_marked_up)],
    ['Assembly labor', money(summary.assembly_labor_cost), hours(summary.assembly_hours)],
    ['Electrical labor', money(summary.electrical_labor_cost), hours(summary.electrical_hours)],
    ['Machined', money(summary.machined_subtotal)],
    ['Overhead', money(summary.overhead)],
    ['Consumables', money(summary.consumables)],
  ];

  return (
    <aside className="bg-fd-panel border border-fd-line rounded-sm p-3 h-fit sticky top-4 space-y-3">
      <h2 className="text-sm font-semibold text-white">Bid summary</h2>
      <dl className="space-y-1.5 text-xs">
        {rows.map(([label, val, hrs]) => (
          <div key={label} className="flex justify-between gap-2">
            <dt className="text-fd-muted">{label}</dt>
            <dd className="text-right tabular-nums text-white">
              {val}
              {hrs ? <div className="text-[10px] text-fd-muted">{hrs}</div> : null}
            </dd>
          </div>
        ))}
      </dl>
      <div className="border-t border-fd-line pt-2 space-y-1">
        <div className="flex justify-between text-xs">
          <span className="text-fd-muted">COGS</span>
          <span className="tabular-nums text-white">{money(summary.cogs)}</span>
        </div>
        <div className="flex justify-between text-xs">
          <span className="text-fd-muted">
            Margin {(summary.target_margin * 100).toFixed(0)}%
          </span>
          <span className="tabular-nums text-fd-muted">
            {money(summary.sell_price - summary.cogs)}
          </span>
        </div>
        <div className="flex justify-between items-baseline bg-fd-blue/10 border border-fd-blue/30 rounded-sm px-2 py-1.5 mt-1">
          <span className="text-sm font-semibold text-white">Sell</span>
          <span className="text-lg font-bold tabular-nums text-white">
            {money(summary.sell_price)}
          </span>
        </div>
      </div>
      {quoteId ? (
        <Link
          to="/quotes"
          className="btn-secondary w-full text-center text-sm inline-flex items-center justify-center"
        >
          Open {quoteNumber || `quote #${quoteId}`}
        </Link>
      ) : (
        <LoadingButton
          loading={Boolean(finalizing)}
          onClick={onFinalize}
          variant="primary"
          size="sm"
          disabled={!canFinalize}
          className="w-full"
        >
          Finalize bid
        </LoadingButton>
      )}
    </aside>
  );
}
