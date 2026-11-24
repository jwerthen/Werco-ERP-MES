import { useResumableDraft } from '../hooks/useResumableDraft';
import { DraftRecovery } from '../components/ui/DraftRecovery';
import { PRIORITY_OPTIONS } from '../utils/priority';
import React, { useEffect, useMemo, useState, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import api from '../services/api';
import {
  InformationCircleIcon,
  CheckCircleIcon,
  ExclamationTriangleIcon,
  PlusIcon,
  TrashIcon,
  ChevronDownIcon,
  MagnifyingGlassIcon,
  XMarkIcon,
} from '@heroicons/react/24/outline';
import { Breadcrumbs, SelectField, SelectOption, ErrorState, useToast, FormField, ConfirmDialog } from '../components/ui';
import { formatOperationLabel } from '../utils/operationLabel';
import {
  isRoutingOperationUnmatchedDetail,
  type WorkOrderOperationPreviewRow,
  type WorkOrderOperationsPreview,
} from '../types/workOrderProcessSheet';
import { getBreadcrumbParent } from '../utils/routeMeta';
import useUnsavedChanges from '../hooks/useUnsavedChanges';
import CustomerPOAutocomplete from '../components/workorders/CustomerPOAutocomplete';

// Breadcrumb parent from the shared routeMeta source — no literal fallback:
// if the entry is ever removed there, the crumb visibly disappears rather
// than silently keeping a duplicate.
const woNewParent = getBreadcrumbParent('/work-orders/new');

interface Part {
  id: number;
  part_number: string;
  name: string;
  part_type: string;
  customer_name?: string;
  customer_part_number?: string;
  revision?: string;
  description?: string;
}

interface BOMComponentPart {
  id: number;
  part_number: string;
  name: string;
  revision?: string;
  part_type: string;
  has_bom?: boolean;
}

interface BOMItem {
  component_part_id: number;
  quantity: number;
  item_type?: string;
  line_type?: string;
  component_part?: BOMComponentPart | null;
}

interface BOMSummary {
  id: number;
  part_id: number;
  revision: string;
  part?: {
    id: number;
    part_number: string;
    name: string;
    revision?: string;
    part_type: string;
  } | null;
  items?: BOMItem[];
}

interface ComponentUsage {
  assemblyPartNumber: string;
  assemblyName: string;
  quantity: number;
  itemType?: string;
  lineType?: string;
}

interface WorkCenter {
  id: number;
  code: string;
  name: string;
}

interface RoutingOperation {
  id: number;
  sequence: number;
  operation_number: string;
  name: string;
  description?: string;
  work_center_id: number;
  work_center?: { id: number; code: string; name: string };
  setup_hours: number;
  run_hours_per_unit: number;
  work_instructions?: string;
  setup_instructions?: string;
  is_inspection_point?: boolean;
  process_sheet_id?: number | null;
}

interface Routing {
  id: number;
  part_id: number;
  revision: string;
  status: string;
  operations: RoutingOperation[];
}

interface OperationPreview {
  sequence: number;
  operation_number: string;
  name: string;
  work_center_id: number;
  work_center_name: string;
  setup_time_hours: number;
  run_time_hours: number;
  run_time_per_unit?: number;
  component_part_id?: number;
  component_quantity?: number;
  quantity_per_assembly?: number;
  operation_group?: string;
  setup_instructions?: string;
  run_instructions?: string;
  requires_inspection?: boolean;
  fromRouting: boolean;
  // wo_change/2: the routing operation this row was copied from, sent back on create so
  // the step keeps its process sheet. Matched BY ID on the server, never by sequence or
  // name. Absent on a hand-added row (and on rows from a draft saved before this field).
  routing_operation_id?: number;
  process_sheet_id?: number | null;
  process_sheet_number?: string | null;
  process_sheet_released?: boolean | null;
}

/**
 * One step of the part's released routing as the server would copy it today. Used to
 * tell the planner which sheet-bearing steps a supplied operation list leaves out, and
 * which restored-draft links no longer name a step on the released routing.
 */
interface RoutedStepRef {
  routing_operation_id: number;
  operation_number: string;
  sequence: number;
  name: string;
  component_part_id: number | null;
  process_sheet_id: number | null;
  process_sheet_number: string | null;
}

const routedStepsFromPreview = (preview: WorkOrderOperationsPreview | null | undefined): RoutedStepRef[] =>
  (preview?.operations_preview || [])
    .filter((row): row is WorkOrderOperationPreviewRow & { routing_operation_id: number } =>
      typeof row.routing_operation_id === 'number')
    .map((row, index) => ({
      routing_operation_id: row.routing_operation_id,
      operation_number: row.operation_number ?? String((index + 1) * 10),
      sequence: row.sequence ?? (index + 1) * 10,
      name: row.name,
      component_part_id: row.component_part_id ?? null,
      process_sheet_id: row.process_sheet_id ?? null,
      process_sheet_number: row.process_sheet_number ?? null,
    }));

const sheetLabel = (number: string | null | undefined, id: number | null | undefined) =>
  number ? number : id != null ? `#${id}` : '';

interface CustomerOption {
  id: number;
  name: string;
}

interface PartReadiness {
  ready: boolean;
  blockers: string[];
  warnings: string[];
  checks: Record<string, string>;
}

/** The process sheet a row carries (or why it carries none it should). */
function OperationSheetChip({ op, stale }: { op: OperationPreview; stale: boolean }) {
  if (stale) {
    return (
      <span className="mt-1 inline-block border border-red-500/40 px-1.5 py-0.5 text-[11px] text-red-300">
        Routing link no longer valid
      </span>
    );
  }
  if (op.routing_operation_id == null || (op.process_sheet_id == null && !op.process_sheet_number)) return null;
  const label = sheetLabel(op.process_sheet_number, op.process_sheet_id);
  return (
    <span
      className={`mt-1 inline-block border px-1.5 py-0.5 text-[11px] ${
        op.process_sheet_released === false ? 'border-amber-500/40 text-amber-300' : 'border-slate-600 text-slate-300'
      }`}
    >
      Process sheet {label}
      {op.process_sheet_released === false ? ' — no released revision' : ''}
    </span>
  );
}

export default function WorkOrderNew() {
  const navigate = useNavigate();
  const { showToast } = useToast();
  const [parts, setParts] = useState<Part[]>([]);
  const [activeBOMs, setActiveBOMs] = useState<BOMSummary[]>([]);
  const [workCenters, setWorkCenters] = useState<WorkCenter[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [loadingRouting, setLoadingRouting] = useState(false);
  const [routing, setRouting] = useState<Routing | null>(null);
  const [operations, setOperations] = useState<OperationPreview[]>([]);
  const [showManualEntry, setShowManualEntry] = useState(false);
  const [operationsOverridden, setOperationsOverridden] = useState(false);
  const [routingLoadError, setRoutingLoadError] = useState('');
  // Today's released routing steps for the selected part (null = not loaded/unknown).
  const [routedSteps, setRoutedSteps] = useState<RoutedStepRef[] | null>(null);
  const [linkCheckError, setLinkCheckError] = useState('');
  const [restoredFromDraft, setRestoredFromDraft] = useState(false);
  // Routing-operation ids the server refused (409 ROUTING_OPERATION_UNMATCHED).
  const [serverUnmatchedIds, setServerUnmatchedIds] = useState<Set<number>>(() => new Set());
  const [confirmReload, setConfirmReload] = useState(false);
  const partRequestRef = useRef(0);
  const submitPendingRef = useRef(false);
  const [customerOptions, setCustomerOptions] = useState<CustomerOption[]>([]);
  const [customerSearch, setCustomerSearch] = useState('');
  const [showCustomerDropdown, setShowCustomerDropdown] = useState(false);
  const [creatingCustomer, setCreatingCustomer] = useState(false);
  const [highlightedCustomerIndex, setHighlightedCustomerIndex] = useState(-1);
  const [partReadiness, setPartReadiness] = useState<PartReadiness | null>(null);
  const [partSearch, setPartSearch] = useState('');
  const [showPartDropdown, setShowPartDropdown] = useState(false);
  const [highlightedPartIndex, setHighlightedPartIndex] = useState(-1);

  const [form, setForm] = useState({
    part_id: 0,
    quantity_ordered: 1,
    priority: 5,
    customer_name: '',
    customer_po: '',
    unit_number: '',
    due_date: '',
    notes: ''
  });

  // Serialized WO creation (Process Sheets PR 4): optional serial numbers, one
  // per line. Client-side validation mirrors the server exactly — unique,
  // non-empty, ≤100 chars, count == quantity_ordered when provided.
  const [serialsText, setSerialsText] = useState('');

  // Snapshot of the values the form opened with, so an untouched form is never
  // treated as dirty. This is a create-only page, so the snapshot is the empty
  // form captured on mount.
  const initialForm = useMemo(() => ({
    part_id: 0,
    quantity_ordered: 1,
    priority: 5,
    customer_name: '',
    customer_po: '',
    unit_number: '',
    due_date: '',
    notes: ''
  }), []);

  const isFormDirty = useMemo(
    () =>
      JSON.stringify(form) !== JSON.stringify(initialForm) ||
      operations.length > 0 ||
      serialsText.trim() !== '',
    [form, initialForm, operations, serialsText]
  );
  const { confirmDiscard, markSaved } = useUnsavedChanges(isFormDirty);

  // A restored draft's routing links are checked against TODAY's released routing, never
  // trusted and never silently dropped: a link that no longer names a step is shown to the
  // planner (unlink or reload), and the server would refuse it 409 anyway.
  const checkRoutingLinks = (partId: number, quantity: number, requestId: number) => {
    setLinkCheckError('');
    Promise.resolve().then(() => api.previewWorkOrderOperations(partId, quantity)).then(preview => {
      if (requestId === partRequestRef.current) setRoutedSteps(routedStepsFromPreview(preview));
    }).catch(() => {
      if (requestId === partRequestRef.current) {
        setRoutedSteps(null);
        setLinkCheckError('Could not check the restored operations against the released routing. Retry before creating the work order.');
      }
    });
  };

  const draftValue = { form, operations, operationsOverridden, showManualEntry, serialsText };
  const draft = useResumableDraft({ namespace: 'work-orders', value: draftValue, dirty: isFormDirty,
    enabled: true,
    valid: (data: unknown): data is typeof draftValue => {
      const saved = data as typeof draftValue | null;
      return !!saved && !!saved.form && typeof saved.form.part_id === 'number'
        && typeof saved.form.customer_name === 'string' && typeof saved.serialsText === 'string'
        && Array.isArray(saved.operations) && saved.operations.every(op => !!op && typeof op.name === 'string' && typeof op.work_center_id === 'number');
    },
    restore: saved => {
      const requestId = ++partRequestRef.current;
      setLoadingRouting(false); setRoutingLoadError(''); setRouting(null);
      setRoutedSteps(null); setLinkCheckError(''); setServerUnmatchedIds(new Set()); setRestoredFromDraft(true);
      setForm({ ...initialForm, ...saved.form }); setOperations(saved.operations);
      // Restored operations are an explicit reviewed snapshot. Preserve removed
      // operations instead of silently reloading today's released routing.
      setOperationsOverridden(saved.operationsOverridden || saved.operations.length > 0); setShowManualEntry(saved.showManualEntry || saved.operations.length > 0);
      setSerialsText(saved.serialsText); setCustomerSearch(saved.form.customer_name);
      const part = parts.find(item => item.id === saved.form.part_id);
      setPartSearch(part ? `${part.part_number} - ${part.name}` : `Part #${saved.form.part_id}`);
      setPartReadiness(null);
      if (saved.form.part_id) {
        setLoadingRouting(true);
        api.getPartReadiness(saved.form.part_id).then(readiness => {
          if (requestId === partRequestRef.current) setPartReadiness(readiness);
        }).catch(() => {
          if (requestId === partRequestRef.current) setRoutingLoadError('Could not verify the restored part. Retry before creating the work order.');
        }).finally(() => { if (requestId === partRequestRef.current) setLoadingRouting(false); });
        checkRoutingLinks(saved.form.part_id, saved.form.quantity_ordered || 1, requestId);
      }
    },
  });

  // Parsed serial lines (trimmed, blanks dropped) + the violations the server
  // would 422 on. Providing NO serials is always valid (serials are optional).
  const serialNumbers = useMemo(
    () =>
      serialsText
        .split(/\r?\n/)
        .map((line) => line.trim())
        .filter((line) => line.length > 0),
    [serialsText]
  );
  const serialErrors = useMemo(() => {
    if (serialNumbers.length === 0) return [];
    const errors: string[] = [];
    const duplicates = serialNumbers.filter((sn, i) => serialNumbers.indexOf(sn) !== i);
    if (duplicates.length > 0) {
      errors.push(`Duplicate serial number${duplicates.length === 1 ? '' : 's'}: ${Array.from(new Set(duplicates)).join(', ')}`);
    }
    if (serialNumbers.some((sn) => sn.length > 100)) {
      errors.push('Serial numbers must be 100 characters or fewer.');
    }
    if (serialNumbers.length !== form.quantity_ordered) {
      errors.push(
        `${serialNumbers.length} serial number${serialNumbers.length === 1 ? '' : 's'} entered for quantity ${form.quantity_ordered} — the counts must match.`
      );
    }
    return errors;
  }, [serialNumbers, form.quantity_ordered]);

  useEffect(() => {
    loadInitialData();
  }, []);

  const loadInitialData = async () => {
    setLoading(true);
    setLoadError(false);
    try {
      const [partsRes, bomRes, wcRes] = await Promise.all([
        api.getParts({ active_only: true, include_bom_components: true, limit: 500 }),
        api.getBOMs({ active_only: true, limit: 500 }),
        api.getWorkCenters(),
      ]);
      setParts(partsRes);
      setActiveBOMs(bomRes);
      setWorkCenters(wcRes);
      try {
        const customers = await api.getCustomerNames();
        setCustomerOptions(customers);
      } catch (customerErr) {
        console.error('Failed to load customer names:', customerErr);
      }
    } catch (err) {
      console.error('Failed to load data:', err);
      setLoadError(true);
    } finally {
      setLoading(false);
    }
  };

  const componentUsageByPartId = useMemo(() => {
    const usage = new Map<number, ComponentUsage[]>();

    activeBOMs.forEach((bom) => {
      if (!bom.part || !bom.items) return;

      bom.items.forEach((item) => {
        if (!item.component_part_id) return;
        const existing = usage.get(item.component_part_id) || [];
        existing.push({
          assemblyPartNumber: bom.part?.part_number || '',
          assemblyName: bom.part?.name || '',
          quantity: item.quantity || 1,
          itemType: item.item_type,
          lineType: item.line_type,
        });
        usage.set(item.component_part_id, existing);
      });
    });

    return usage;
  }, [activeBOMs]);

  const selectedPart = useMemo(
    () => parts.find((part) => part.id === form.part_id) || null,
    [parts, form.part_id]
  );
  const bomPartIds = useMemo(
    () => new Set(activeBOMs.map((bom) => bom.part_id)),
    [activeBOMs]
  );

  const normalizedPartSearch = partSearch.trim().toLowerCase();
  const normalizePartType = (partType?: string) => (partType || '').trim().toLowerCase();

  const workOrderPartOptions = useMemo(() => {
    const eligibleParts = parts.filter((part) => ['assembly', 'manufactured'].includes(normalizePartType(part.part_type)));

    const scored = eligibleParts
      .map((part) => {
        const usage = componentUsageByPartId.get(part.id) || [];
        const usageText = usage
          .map((item) => `${item.assemblyPartNumber} ${item.assemblyName}`)
          .join(' ');
        const searchable = [
          part.part_number,
          part.name,
          part.description,
          part.customer_part_number,
          part.customer_name,
          part.part_type,
          usageText,
        ]
          .filter(Boolean)
          .join(' ')
          .toLowerCase();

        if (normalizedPartSearch && !searchable.includes(normalizedPartSearch)) {
          return null;
        }

        const partNumber = part.part_number.toLowerCase();
        const name = part.name.toLowerCase();
        let score = 4;
        if (!normalizedPartSearch) {
          score = normalizePartType(part.part_type) === 'assembly' ? 1 : usage.length > 0 ? 2 : 3;
        } else if (partNumber.startsWith(normalizedPartSearch)) {
          score = 0;
        } else if (name.startsWith(normalizedPartSearch)) {
          score = 1;
        } else if (usageText.toLowerCase().includes(normalizedPartSearch)) {
          score = 2;
        }

        return { part, usage, score };
      })
      .filter((option): option is { part: Part; usage: ComponentUsage[]; score: number } => Boolean(option))
      .sort((a, b) => {
        if (a.score !== b.score) return a.score - b.score;
        const aComponent = a.usage.length > 0 ? 0 : 1;
        const bComponent = b.usage.length > 0 ? 0 : 1;
        if (aComponent !== bComponent) return aComponent - bComponent;
        return a.part.part_number.localeCompare(b.part.part_number);
      });

    return scored.slice(0, 12);
  }, [parts, componentUsageByPartId, normalizedPartSearch]);

  const selectedPartUsage = selectedPart ? componentUsageByPartId.get(selectedPart.id) || [] : [];
  const priorityOptions: SelectOption<number>[] = PRIORITY_OPTIONS;
  const workCenterOptions = useMemo<SelectOption<number>[]>(() => (
    workCenters.map((workCenter) => ({
      value: workCenter.id,
      label: workCenter.name,
      description: workCenter.code,
    }))
  ), [workCenters]);

  const formatPartType = (partType: string) => (
    normalizePartType(partType)
      .split('_')
      .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
      .join(' ')
  );

  const formatQuantity = (quantity: number) => Number.isInteger(quantity) ? quantity.toString() : quantity.toFixed(2);

  const hoursToMinutes = (hours: number) => Math.round(Number(hours || 0) * 60 * 100) / 100;
  const minutesToHours = (minutes: number) => Math.round(Number(minutes || 0) / 60 * 10000) / 10000;
  const isMissingRoutingBlocker = (message: string) => message.toLowerCase().includes('no active routing');

  const partDisplayName = (part: Part) => `${part.part_number} - ${part.name}`;

  const selectPart = (part: Part) => {
    setPartSearch(partDisplayName(part));
    setShowPartDropdown(false);
    setHighlightedPartIndex(-1);
    handlePartChange(part.id);
  };

  const clearPartSelection = () => {
    setPartSearch('');
    setShowPartDropdown(false);
    setHighlightedPartIndex(-1);
    handlePartChange(0);
  };

  const handlePartKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      if (!showPartDropdown) {
        setShowPartDropdown(true);
        return;
      }
      if (workOrderPartOptions.length === 0) return;
      setHighlightedPartIndex((prev) => {
        const next = prev + 1;
        return next >= workOrderPartOptions.length ? 0 : next;
      });
      return;
    }

    if (e.key === 'ArrowUp') {
      e.preventDefault();
      if (!showPartDropdown) {
        setShowPartDropdown(true);
        return;
      }
      if (workOrderPartOptions.length === 0) return;
      setHighlightedPartIndex((prev) => {
        const next = prev - 1;
        return next < 0 ? workOrderPartOptions.length - 1 : next;
      });
      return;
    }

    if (e.key === 'Enter' && showPartDropdown && highlightedPartIndex >= 0) {
      e.preventDefault();
      const option = workOrderPartOptions[highlightedPartIndex];
      if (option) selectPart(option.part);
      return;
    }

    if (e.key === 'Escape') {
      setShowPartDropdown(false);
      setHighlightedPartIndex(-1);
    }
  };

  useEffect(() => {
    setHighlightedPartIndex(-1);
  }, [partSearch, showPartDropdown]);

  const normalizedCustomerSearch = customerSearch.trim().toLowerCase();
  const matchingCustomers = useMemo(() => {
    if (!normalizedCustomerSearch) {
      return customerOptions;
    }

    return customerOptions.filter((customer) =>
      customer.name.toLowerCase().includes(normalizedCustomerSearch)
    );
  }, [customerOptions, normalizedCustomerSearch]);

  const filteredCustomers = useMemo(() => {
    const ranked = [...matchingCustomers].sort((a, b) => {
      const aName = a.name.toLowerCase();
      const bName = b.name.toLowerCase();

      const aStarts = normalizedCustomerSearch ? aName.startsWith(normalizedCustomerSearch) : false;
      const bStarts = normalizedCustomerSearch ? bName.startsWith(normalizedCustomerSearch) : false;
      if (aStarts !== bStarts) return aStarts ? -1 : 1;

      return a.name.localeCompare(b.name);
    });

    return ranked.slice(0, 8);
  }, [matchingCustomers, normalizedCustomerSearch]);

  const hasExactCustomerMatch = normalizedCustomerSearch.length > 0
    && customerOptions.some((customer) => customer.name.trim().toLowerCase() === normalizedCustomerSearch);

  const canCreateCustomer = customerSearch.trim().length > 0 && !hasExactCustomerMatch;

  const getCustomerByName = (nameRaw: string) => {
    const name = nameRaw.trim().toLowerCase();
    if (!name) return null;
    return customerOptions.find((customer) => customer.name.trim().toLowerCase() === name) || null;
  };

  const createCustomerFromSearch = async () => {
    const customerName = customerSearch.trim();
    if (!customerName) return null;

    const existing = getCustomerByName(customerName);
    if (existing) return existing;

    setCreatingCustomer(true);
    try {
      const created = await api.createCustomer({ name: customerName });
      const createdOption = { id: created.id, name: created.name };
      setCustomerOptions((prev) =>
        [...prev, createdOption].sort((a, b) => a.name.localeCompare(b.name))
      );
      return createdOption;
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to create customer');
      return null;
    } finally {
      setCreatingCustomer(false);
    }
  };

  const selectCustomer = (customerName: string) => {
    setCustomerSearch(customerName);
    setForm((prev) => ({ ...prev, customer_name: customerName }));
    setShowCustomerDropdown(false);
    setHighlightedCustomerIndex(-1);
  };

  const createAndSelectCustomer = async () => {
    const created = await createCustomerFromSearch();
    if (!created) return;
    selectCustomer(created.name);
  };

  const handlePartChange = async (partId: number) => {
    const requestId = ++partRequestRef.current;
    const isCurrent = () => requestId === partRequestRef.current;
    setOperationsOverridden(false);
    setRoutingLoadError('');
    setRoutedSteps(null);
    setLinkCheckError('');
    setServerUnmatchedIds(new Set());
    setRestoredFromDraft(false);
    const selectedPart = parts.find(p => p.id === partId);
    const partCustomerName = selectedPart?.customer_name || '';
    setForm((prev) => ({
      ...prev,
      part_id: partId,
      customer_name: partCustomerName || prev.customer_name
    }));
    if (partCustomerName) {
      setCustomerSearch(partCustomerName);
    }
    setRouting(null);
    setOperations([]);
    setShowManualEntry(false);
    setPartReadiness(null);

    if (!partId) { setLoadingRouting(false); return; }

    // Find the selected part to check if it's an assembly
    const isAssembly = normalizePartType(selectedPart?.part_type) === 'assembly' || bomPartIds.has(partId);

    setLoadingRouting(true);
    try {
      try {
        const readiness = await api.getPartReadiness(partId);
        if (!isCurrent()) return;
        setPartReadiness(readiness);
      } catch (readinessErr) {
        if (!isCurrent()) return;
        setRoutingLoadError('Could not verify part readiness. Retry before creating the work order.');
        console.error('Failed to load part readiness:', readinessErr);
        setPartReadiness(null);
      }

      if (isAssembly) {
        // For assemblies, use the preview endpoint to get combined operations from BOM components
        const previewRes = await api.previewWorkOrderOperations(partId, form.quantity_ordered);
        if (!isCurrent()) return;
        if (previewRes && previewRes.operations_preview?.length > 0) {
          // A stand-in routing object to indicate we have operations. An assembly with no
          // BOM previews its own released routing (revision '' picks that banner text).
          setRouting({ id: 0, part_id: partId, revision: previewRes.bom_found ? 'BOM' : '', status: 'released', operations: [] });
          setRoutedSteps(routedStepsFromPreview(previewRes));
          const ops: OperationPreview[] = previewRes.operations_preview.map((op, index) => ({
            quantity_per_assembly: (op.component_quantity || form.quantity_ordered) / form.quantity_ordered,
            // The server reports the sequence and number the routed create would assign
            // (10, 20, ... on this path); the index fallback covers an older API.
            sequence: op.sequence ?? (index + 1) * 10,
            // IDENTIFIER, not a label. This used to mint `Op ${seq}`, so every
            // screen that renders the column under its own `Op #` header printed
            // the prefix twice ("Op Op 10" on the kiosk). The prefix belongs to
            // the view (`formatOperationLabel`), never to the stored value.
            operation_number: op.operation_number ?? String((index + 1) * 10),
            name: op.name,
            work_center_id: op.work_center_id,
            work_center_name: op.work_center_name || '',
            setup_time_hours: op.setup_hours || 0,
            run_time_hours: (op.run_hours_per_unit || 0) * (op.component_quantity || form.quantity_ordered),
            run_time_per_unit: op.run_hours_per_unit || 0,
            setup_instructions: op.setup_instructions || '',
            run_instructions: op.run_instructions || '',
            requires_inspection: Boolean(op.requires_inspection),
            fromRouting: true,
            component_part_id: op.component_part_id ?? undefined,
            component_quantity: op.component_quantity ?? undefined,
            routing_operation_id: op.routing_operation_id,
            process_sheet_id: op.process_sheet_id ?? null,
            process_sheet_number: op.process_sheet_number ?? null,
            process_sheet_released: op.process_sheet_released ?? null,
          }));
          setOperations(ops);
        } else if (previewRes?.bom_found === false) {
          // Assembly has no BOM defined - show manual entry
          setShowManualEntry(true);
        } else {
          // Assembly has BOM but no component routings - show manual entry
          setShowManualEntry(true);
        }
      } else {
        // For non-assemblies, use the standard routing lookup
        // The preview carries each step's sheet number and released flag, which the
        // routing read does not. It is advisory here: if it fails, the rows still link
        // by the routing operation's own id and keep their sheet on create.
        const [routingRes, previewRes] = await Promise.all([
          api.getRoutingByPart(partId),
          Promise.resolve().then(() => api.previewWorkOrderOperations(partId, form.quantity_ordered)).catch(() => null),
        ]);
        if (!isCurrent()) return;
        const previewById = new Map<number, WorkOrderOperationPreviewRow>();
        (previewRes?.operations_preview || []).forEach(row => {
          if (typeof row.routing_operation_id === 'number') previewById.set(row.routing_operation_id, row);
        });
        if (routingRes && routingRes.operations?.length > 0) {
          setRouting(routingRes);
          setRoutedSteps(
            previewById.size > 0
              ? routedStepsFromPreview(previewRes)
              : (routingRes.operations as RoutingOperation[]).map(op => ({
                  routing_operation_id: op.id,
                  operation_number: op.operation_number || String(op.sequence),
                  sequence: op.sequence,
                  name: op.name,
                  component_part_id: null,
                  process_sheet_id: op.process_sheet_id ?? null,
                  process_sheet_number: null,
                }))
          );
          const ops: OperationPreview[] = routingRes.operations
            .filter((op: RoutingOperation) => op.work_center)
            .map((op: RoutingOperation) => ({
              sequence: op.sequence,
              // The routing's own value is copied VERBATIM -- a routing
              // operation and the work-order operation derived from it must
              // agree, and normalizing here would silently rewrite a number the
              // office typed by hand. Only the MINTED fallback drops the prefix.
              operation_number: op.operation_number || String(op.sequence),
              name: op.name,
              work_center_id: op.work_center_id,
              work_center_name: op.work_center?.name || '',
              setup_time_hours: op.setup_hours,
              run_time_hours: op.run_hours_per_unit * form.quantity_ordered,
              run_time_per_unit: op.run_hours_per_unit,
              setup_instructions: op.setup_instructions || '',
              run_instructions: op.work_instructions || '',
              requires_inspection: Boolean(op.is_inspection_point),
              fromRouting: true,
              routing_operation_id: op.id,
              process_sheet_id: previewById.get(op.id)?.process_sheet_id ?? op.process_sheet_id ?? null,
              process_sheet_number: previewById.get(op.id)?.process_sheet_number ?? null,
              process_sheet_released: previewById.get(op.id)?.process_sheet_released ?? null,
            }));
          setOperations(ops);
        } else {
          setShowManualEntry(true);
        }
      }
    } catch (err) {
      if (!isCurrent()) return;
      console.error('Failed to load routing:', err);
      setRoutingLoadError('Could not load operations. Retry to check the released routing.');
    } finally {
      if (isCurrent()) setLoadingRouting(false);
    }
  };

  const handleCustomerKeyDown = async (e: React.KeyboardEvent<HTMLInputElement>) => {
    const actionCount = filteredCustomers.length + (canCreateCustomer ? 1 : 0);

    if (e.key === 'ArrowDown') {
      e.preventDefault();
      if (!showCustomerDropdown) {
        setShowCustomerDropdown(true);
        return;
      }
      if (actionCount === 0) return;
      setHighlightedCustomerIndex((prev) => {
        const next = prev + 1;
        return next >= actionCount ? 0 : next;
      });
      return;
    }

    if (e.key === 'ArrowUp') {
      e.preventDefault();
      if (!showCustomerDropdown) {
        setShowCustomerDropdown(true);
        return;
      }
      if (actionCount === 0) return;
      setHighlightedCustomerIndex((prev) => {
        const next = prev - 1;
        return next < 0 ? actionCount - 1 : next;
      });
      return;
    }

    if (e.key === 'Enter' && showCustomerDropdown) {
      if (highlightedCustomerIndex < 0) return;
      e.preventDefault();

      if (highlightedCustomerIndex < filteredCustomers.length) {
        selectCustomer(filteredCustomers[highlightedCustomerIndex].name);
      } else if (canCreateCustomer && !creatingCustomer) {
        await createAndSelectCustomer();
      }
      return;
    }

    if (e.key === 'Escape') {
      setShowCustomerDropdown(false);
      setHighlightedCustomerIndex(-1);
    }
  };

  useEffect(() => {
    setHighlightedCustomerIndex(-1);
  }, [customerSearch, showCustomerDropdown]);

  const handleQuantityChange = (qty: number) => {
    setForm({ ...form, quantity_ordered: qty });
    if (routing) {
      setOperations(ops => ops.map(op => ({
        ...op,
        component_quantity: op.fromRouting && op.quantity_per_assembly
          ? op.quantity_per_assembly * qty
          : op.component_quantity,
        run_time_hours: op.fromRouting
          ? (op.run_time_per_unit || routing.operations.find(r => r.sequence === op.sequence)?.run_hours_per_unit || 0)
            * (op.quantity_per_assembly ? op.quantity_per_assembly * qty : qty)
          : op.run_time_hours
      })));
    }
  };

  const updateOperation = (index: number, field: keyof OperationPreview, value: any) => {
    setOperations(ops => {
      const updated = [...ops];
      updated[index] = { ...updated[index], [field]: value, fromRouting: false };
      if (field === 'work_center_id') {
        const wc = workCenters.find(w => w.id === value);
        updated[index].work_center_name = wc?.name || '';
      }
      return updated;
    });
  };

  const addManualOperation = () => {
    const nextSeq = operations.length > 0 
      ? Math.max(...operations.map(o => o.sequence)) + 10 
      : 10;
    setOperations([...operations, {
      sequence: nextSeq,
      operation_number: String(nextSeq),
      name: '',
      work_center_id: workCenters[0]?.id || 0,
      work_center_name: workCenters[0]?.name || '',
      setup_time_hours: 0,
      run_time_hours: 0,
      setup_instructions: '',
      run_instructions: '',
      requires_inspection: false,
      fromRouting: false
    }]);
  };

  const removeOperation = (index: number) => {
    setOperationsOverridden(true);
    setOperations(ops => ops.filter((_, i) => i !== index));
  };

  const hasManualOperations = operations.length > 0 && (operationsOverridden || showManualEntry || operations.some(op => !op.fromRouting));
  const manualOperationsAreValid = hasManualOperations
    && operations.every(op => op.name.trim().length > 0 && op.work_center_id > 0);
  // ── Process-sheet links (wo_change/2) ────────────────────────────────────
  // A supplied row keeps its step's process sheet only while it is linked to that
  // routing operation by id. These are derived from the rows and today's routing, so a
  // removal, a restored draft and a hand-added row all land in the same warnings.
  const routedStepById = useMemo(
    () => new Map((routedSteps || []).map(step => [step.routing_operation_id, step])),
    [routedSteps]
  );
  const staleLinkedOperations = useMemo(() => operations.filter(op => {
    if (op.routing_operation_id == null) return false;
    if (serverUnmatchedIds.has(op.routing_operation_id)) return true;
    if (routedSteps === null) return false;
    const step = routedStepById.get(op.routing_operation_id);
    if (!step) return true;
    // A component's step must stay on that component; the job part's own step has none.
    return step.component_part_id !== null && (op.component_part_id ?? null) !== step.component_part_id;
  }), [operations, serverUnmatchedIds, routedSteps, routedStepById]);
  const missingSheetSteps = useMemo(() => {
    if (!hasManualOperations || !routedSteps) return [];
    const carried = new Set(
      operations.map(op => op.routing_operation_id).filter((id): id is number => typeof id === 'number')
    );
    return routedSteps.filter(step => step.process_sheet_id != null && !carried.has(step.routing_operation_id));
  }, [hasManualOperations, routedSteps, operations]);
  const unreleasedSheetOperations = operations.filter(op => op.routing_operation_id != null
    && op.process_sheet_released === false && !staleLinkedOperations.includes(op));

  const unlinkStaleOperations = () => {
    const staleIds = new Set(staleLinkedOperations.map(op => op.routing_operation_id));
    setOperations(ops => ops.map(op => (op.routing_operation_id != null && staleIds.has(op.routing_operation_id)
      ? {
          ...op,
          routing_operation_id: undefined,
          process_sheet_id: null,
          process_sheet_number: null,
          process_sheet_released: null,
          fromRouting: false,
        }
      : op)));
    setServerUnmatchedIds(new Set());
  };

  const readinessBlockers = partReadiness?.blockers || [];
  const blockingReadinessMessages = readinessBlockers.filter((message) => (
    !(isMissingRoutingBlocker(message) && manualOperationsAreValid)
  ));
  const routingWillBeSavedToWorkOrder = manualOperationsAreValid && readinessBlockers.some(isMissingRoutingBlocker);
  const informationalReadinessMessages = [
    ...(manualOperationsAreValid ? readinessBlockers.filter(isMissingRoutingBlocker) : []),
    ...(routingWillBeSavedToWorkOrder ? ['Manual operations will be saved directly to this work order.'] : []),
    ...(partReadiness?.warnings || []),
  ];

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (submitPendingRef.current || loadingRouting || routingLoadError) return;
    if (draft.blocked) { showToast('error', 'Resume or remove your saved draft before creating a work order.'); return; }
    if (operationsOverridden && operations.length === 0) {
      showToast('error', 'Add at least one operation before creating this work order.');
      return;
    }
    if (!form.part_id) {
      showToast('error', 'Please select a part');
      return;
    }
    if (blockingReadinessMessages.length) {
      showToast('error', `This part is not ready for a work order: ${blockingReadinessMessages.join(' ')}`);
      return;
    }
    if (hasManualOperations && !manualOperationsAreValid) {
      showToast('error', 'Please complete every manual operation with an operation name and work center.');
      return;
    }
    if (hasManualOperations && staleLinkedOperations.length > 0) {
      showToast('error', 'Some operations are linked to a routing step that is no longer on the released routing. Unlink them or reload the routing first.');
      return;
    }
    if (hasManualOperations && restoredFromDraft && routedSteps === null) {
      showToast('error', 'The restored operations have not been checked against the released routing yet. Retry the check first.');
      return;
    }
    if (serialErrors.length > 0) {
      // Mirror of the server's 422 rules — block before the round-trip.
      showToast('error', `Serial numbers are not valid: ${serialErrors.join(' ')}`);
      return;
    }

    submitPendingRef.current = true;
    setSubmitting(true);
    try {
      const normalizedCustomerName = form.customer_name.trim();
      let customerNameForPayload = normalizedCustomerName;

      if (normalizedCustomerName) {
        const existing = getCustomerByName(normalizedCustomerName);
        if (existing) {
          customerNameForPayload = existing.name;
        } else {
          const created = await createCustomerFromSearch();
          if (!created) return;
          customerNameForPayload = created.name;
          setCustomerSearch(created.name);
        }
      }

      const payload: any = {
        ...form,
        customer_name: customerNameForPayload,
        due_date: form.due_date || null,
      };

      // Optional build identity — omitted entirely when blank rather than sent as ''.
      const unitNumber = form.unit_number.trim();
      if (unitNumber) {
        payload.unit_number = unitNumber;
      } else {
        delete payload.unit_number;
      }

      // Optional per-unit serials (server validates unique/non-empty/count).
      if (serialNumbers.length > 0) {
        payload.serial_numbers = serialNumbers;
      }

      // If operations were modified or manually entered, include them
      if (hasManualOperations) {
        payload.operations = operations.map(op => ({
          sequence: op.sequence,
          operation_number: op.operation_number,
          name: op.name,
          work_center_id: op.work_center_id,
          setup_time_hours: op.setup_time_hours,
          run_time_hours: op.run_time_hours,
          run_time_per_piece: op.run_time_per_unit || 0,
          setup_instructions: op.setup_instructions || undefined,
          run_instructions: op.run_instructions || undefined,
          requires_inspection: Boolean(op.requires_inspection),
          component_part_id: op.component_part_id,
          component_quantity: op.component_quantity,
          operation_group: op.operation_group,
          // Keeps the step's process sheet. Omitted (undefined) on a hand-added row.
          routing_operation_id: op.routing_operation_id,
          status: 'pending'
        }));
      } else {
        payload.operations = [];
      }

      const result = await api.createWorkOrder(payload);
      if (!await draft.clear()) showToast('warning', 'Work order created, but the recovery draft could not be removed. Check the work order list before resuming it.');
      markSaved();
      navigate(`/work-orders/${result.id}`);
    } catch (err: any) {
      // Surface the server refusal VERBATIM. A 422 arrives as Pydantic's
      // structured array — join its messages instead of showing "[object …]".
      const detail = err.response?.data?.detail;
      if (isRoutingOperationUnmatchedDetail(detail)) {
        // Mark the refused rows so the planner sees which ones to unlink or reload.
        setServerUnmatchedIds(new Set(detail.operations.map(op => op.routing_operation_id)));
      }
      const message =
        typeof detail === 'string' && detail.trim()
          ? detail
          : Array.isArray(detail)
            ? detail
                .map((d: any) => (typeof d?.msg === 'string' ? d.msg : JSON.stringify(d)))
                .join('; ')
            // Structured 409s (ROUTING_OPERATION_UNMATCHED, PROCESS_SHEET_UNAVAILABLE)
            // carry their human text in `detail.detail`.
            : detail && typeof detail === 'object' && typeof detail.detail === 'string' && detail.detail.trim()
              ? detail.detail
              : 'Failed to create work order';
      showToast('error', message);
    } finally {
      submitPendingRef.current = false;
      setSubmitting(false);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="spinner h-12 w-12"></div>
      </div>
    );
  }

  if (loadError) {
    return (
      <div className="max-w-4xl mx-auto min-w-0">
        {/* Breadcrumbs — Work Orders › New Work Order */}
        {woNewParent && <Breadcrumbs crumbs={[woNewParent, { label: 'New Work Order' }]} />}
        <h1 className="text-2xl font-bold text-white mb-6">New Work Order</h1>
        <ErrorState
          message="Could not load parts, BOMs, and work centers needed to create a work order."
          onRetry={loadInitialData}
        />
      </div>
    );
  }

  return (
    <div className="max-w-4xl mx-auto min-w-0">
      {/* Breadcrumbs — Work Orders › New Work Order */}
      {woNewParent && <Breadcrumbs crumbs={[woNewParent, { label: 'New Work Order' }]} />}
      <h1 className="text-2xl font-bold text-white mb-6">New Work Order</h1>

      <DraftRecovery draft={draft} />
      <form onSubmit={handleSubmit} className="space-y-6">
        {/* Basic Info Card */}
        <div className="card">
          <h2 className="text-lg font-semibold text-white mb-4">Work Order Details</h2>
          
          <div className="space-y-4">
            <FormField label="Part" required>
              {(field) => (
              <div className="relative">
                <div className="relative">
                  <MagnifyingGlassIcon className="h-5 w-5 absolute left-3 top-1/2 -translate-y-1/2 text-slate-500 pointer-events-none" />
                  <input
                    {...field}
                    type="text"
                    value={partSearch}
                    onChange={(e) => {
                      setPartSearch(e.target.value);
                      setShowPartDropdown(true);
                      if (form.part_id) {
                        partRequestRef.current += 1;
                        setLoadingRouting(false);
                        setRoutingLoadError('');
                        setOperationsOverridden(false);
                        setForm((prev) => ({ ...prev, part_id: 0 }));
                        setRouting(null);
                        setOperations([]);
                        setShowManualEntry(false);
                        setPartReadiness(null);
                      }
                    }}
                    onFocus={() => setShowPartDropdown(true)}
                    onBlur={() => setTimeout(() => setShowPartDropdown(false), 200)}
                    onKeyDown={handlePartKeyDown}
                    className="input pl-10 pr-20"
                    placeholder="Search parts, assemblies, or BOM components"
                    autoFocus
                    role="combobox"
                    aria-expanded={showPartDropdown}
                    aria-controls="work-order-part-results"
                    aria-autocomplete="list"
                  />
                  {form.part_id > 0 && (
                    <button
                      type="button"
                      onClick={clearPartSelection}
                      className="absolute right-10 top-1/2 -translate-y-1/2 p-1 rounded-md text-slate-500 hover:text-slate-200 hover:bg-slate-800"
                      aria-label="Clear selected part"
                    >
                      <XMarkIcon className="h-4 w-4" />
                    </button>
                  )}
                  <ChevronDownIcon
                    className="h-5 w-5 absolute right-3 top-1/2 -translate-y-1/2 text-slate-500 cursor-pointer"
                    onClick={() => setShowPartDropdown(!showPartDropdown)}
                  />
                </div>

                {showPartDropdown && (
                  <div
                    id="work-order-part-results"
                    className="absolute z-20 w-full mt-1 bg-fd-panel border border-slate-700 rounded-xl shadow-xl max-h-96 overflow-y-auto"
                    role="listbox"
                  >
                    {workOrderPartOptions.length > 0 ? (
                      <>
                        {workOrderPartOptions.map(({ part, usage }, index) => (
                          <button
                            key={part.id}
                            type="button"
                            className={`w-full text-left px-4 py-3 border-b border-slate-700/40 last:border-b-0 ${
                              highlightedPartIndex === index ? 'bg-cyan-500/10' : 'hover:bg-slate-800/80'
                            }`}
                            onMouseEnter={() => setHighlightedPartIndex(index)}
                            onMouseDown={(event) => {
                              event.preventDefault();
                              selectPart(part);
                            }}
                            role="option"
                            aria-selected={form.part_id === part.id}
                          >
                            <div className="flex items-start justify-between gap-3">
                              <div className="min-w-0">
                                <div className="font-semibold text-slate-100 truncate">
                                  {part.part_number} <span className="text-slate-400 font-normal">- {part.name}</span>
                                </div>
                                <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-slate-400">
                                  <span className="rounded-md border border-slate-700 px-2 py-0.5 text-slate-300">
                                    {formatPartType(part.part_type)}
                                  </span>
                                  {part.revision && <span>Rev {part.revision}</span>}
                                  {part.customer_part_number && <span>Customer PN {part.customer_part_number}</span>}
                                  {usage.length > 0 && (
                                    <span className="text-cyan-300">
                                      Component in {usage.slice(0, 2).map((item) => item.assemblyPartNumber).join(', ')}
                                      {usage.length > 2 ? ` +${usage.length - 2}` : ''}
                                    </span>
                                  )}
                                </div>
                              </div>
                              {normalizePartType(part.part_type) === 'assembly' && (
                                <span className="shrink-0 rounded-md bg-blue-500/10 px-2 py-1 text-xs font-medium text-blue-200 border border-blue-500/20">
                                  Assembly
                                </span>
                              )}
                            </div>
                          </button>
                        ))}
                        {parts.filter((part) => ['assembly', 'manufactured'].includes(normalizePartType(part.part_type))).length > workOrderPartOptions.length && (
                          <div className="px-4 py-2 text-xs text-slate-400 border-t border-slate-700/40">
                            Showing {workOrderPartOptions.length} matches
                          </div>
                        )}
                      </>
                    ) : (
                      <div className="px-4 py-3 text-sm text-slate-400">No matching work-order parts found</div>
                    )}
                  </div>
                )}

                {selectedPart && (
                  <div className="mt-3 rounded-xl border border-slate-700 bg-slate-900/40 p-3">
                    <div className="flex flex-wrap items-center gap-2 text-sm">
                      <span className="font-semibold text-white">{selectedPart.part_number}</span>
                      <span className="text-slate-300">{selectedPart.name}</span>
                      <span className="rounded-md border border-slate-700 px-2 py-0.5 text-xs text-slate-300">
                        {formatPartType(selectedPart.part_type)}
                      </span>
                    </div>
                    {selectedPartUsage.length > 0 && (
                      <div className="mt-2 flex flex-wrap gap-2 text-xs text-slate-400">
                        {selectedPartUsage.slice(0, 3).map((usage, index) => (
                          <span
                            key={`${usage.assemblyPartNumber}-${usage.quantity}-${index}`}
                            className="rounded-md bg-cyan-500/10 border border-cyan-500/20 px-2 py-1 text-cyan-200"
                          >
                            {formatQuantity(usage.quantity)} per {usage.assemblyPartNumber}
                          </span>
                        ))}
                        {selectedPartUsage.length > 3 && (
                          <span className="px-2 py-1 text-slate-500">+{selectedPartUsage.length - 3} more assemblies</span>
                        )}
                      </div>
                    )}
                  </div>
                )}
              </div>
              )}
            </FormField>

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              <FormField label="Quantity" required>
                {(field) => (
                  <input
                    {...field}
                    type="number"
                    value={form.quantity_ordered}
                    disabled={loadingRouting}
                  onChange={(e) => handleQuantityChange(parseInt(e.target.value) || 1)}
                    className="input"
                    min={1}
                    required
                  />
                )}
              </FormField>
              <FormField label="Priority">
                <SelectField
                  value={form.priority}
                  onChange={(priority) => setForm({ ...form, priority })}
                  options={priorityOptions}
                  ariaLabel="Priority"
                />
              </FormField>
            </div>

            {form.quantity_ordered >= 1 && (
              <FormField
                label="Serial numbers"
                help="Optional — one per line. When provided, the count must equal the quantity; shop-floor step records are then captured per unit."
                error={serialErrors.length > 0 ? serialErrors.join(' ') : null}
              >
                {(field) => (
                  <>
                    <textarea
                      {...field}
                      data-testid="wo-serial-numbers"
                      value={serialsText}
                      onChange={(e) => setSerialsText(e.target.value)}
                      className="input font-mono"
                      rows={Math.min(8, Math.max(3, serialNumbers.length + 1))}
                      placeholder={'SN-001\nSN-002\n…'}
                      spellCheck={false}
                    />
                    <p
                      data-testid="wo-serials-count"
                      className={`mt-1 text-xs ${
                        serialNumbers.length === 0
                          ? 'text-slate-500'
                          : serialNumbers.length === form.quantity_ordered
                            ? 'text-green-500'
                            : 'text-fd-amber'
                      }`}
                    >
                      {serialNumbers.length === 0
                        ? 'No serial numbers — this work order will not be serialized.'
                        : `${serialNumbers.length} of ${form.quantity_ordered} serial number${form.quantity_ordered === 1 ? '' : 's'} entered`}
                    </p>
                  </>
                )}
              </FormField>
            )}

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              <FormField label="Customer Name" className="relative">
                {(field) => (
                <>
                <div className="relative">
                  <input
                    {...field}
                    type="text"
                    value={customerSearch}
                    onChange={(e) => {
                      const typedValue = e.target.value;
                      setCustomerSearch(typedValue);
                      setForm((prev) => ({ ...prev, customer_name: typedValue }));
                      setShowCustomerDropdown(true);
                    }}
                    onFocus={() => setShowCustomerDropdown(true)}
                    onBlur={() => setTimeout(() => setShowCustomerDropdown(false), 200)}
                    onKeyDown={handleCustomerKeyDown}
                    className="input pr-8"
                    placeholder="Select or type customer"
                  />
                  <ChevronDownIcon
                    className="h-5 w-5 absolute right-2 top-1/2 -translate-y-1/2 text-slate-500 cursor-pointer"
                    onClick={() => setShowCustomerDropdown(!showCustomerDropdown)}
                  />
                </div>
                {showCustomerDropdown && (
                  <div className="absolute z-10 w-full mt-1 bg-fd-panel border border-slate-700 rounded-md shadow-lg max-h-48 overflow-y-auto">
                    {filteredCustomers.length > 0 ? (
                      <>
                      {filteredCustomers.map((customer, index) => (
                        <button
                          key={customer.id}
                          type="button"
                          className={`w-full text-left px-3 py-2 text-sm ${
                            highlightedCustomerIndex === index ? 'bg-blue-500/10' : 'hover:bg-slate-800'
                          }`}
                          onMouseEnter={() => setHighlightedCustomerIndex(index)}
                          onMouseDown={(event) => {
                            event.preventDefault();
                            selectCustomer(customer.name);
                          }}
                        >
                          {customer.name}
                        </button>
                      ))}
                      {matchingCustomers.length > filteredCustomers.length && (
                        <div className="px-3 py-2 text-xs text-slate-400 border-t border-slate-700/30">
                          Showing {filteredCustomers.length} of {matchingCustomers.length}. Keep typing to narrow results.
                        </div>
                      )}
                      {canCreateCustomer && (
                        <button
                          type="button"
                          className={`w-full text-left px-3 py-2 text-sm border-t border-slate-700/30 ${
                            highlightedCustomerIndex === filteredCustomers.length
                              ? 'bg-blue-500/10 text-blue-400'
                              : 'hover:bg-blue-500/100/10 text-blue-600'
                          } disabled:text-slate-500`}
                          disabled={creatingCustomer}
                          onMouseEnter={() => setHighlightedCustomerIndex(filteredCustomers.length)}
                          onMouseDown={async (event) => {
                            event.preventDefault();
                            if (creatingCustomer) return;
                            await createAndSelectCustomer();
                          }}
                        >
                          <PlusIcon className="h-4 w-4 inline mr-1" />
                          {creatingCustomer ? 'Creating customer...' : `Create "${customerSearch.trim()}"`}
                        </button>
                      )}
                      </>
                    ) : canCreateCustomer ? (
                      <button
                        type="button"
                        className="w-full text-left px-3 py-2 hover:bg-blue-500/100/10 text-sm text-blue-600 disabled:text-slate-500"
                        disabled={creatingCustomer}
                        onMouseDown={async (event) => {
                          event.preventDefault();
                          await createAndSelectCustomer();
                        }}
                      >
                        <PlusIcon className="h-4 w-4 inline mr-1" />
                        {creatingCustomer ? 'Creating customer...' : `Create "${customerSearch.trim()}"`}
                      </button>
                    ) : (
                      <div className="px-3 py-2 text-sm text-slate-400">Type to search customer</div>
                    )}
                  </div>
                )}
                </>
                )}
              </FormField>
              <FormField label="Customer PO #">
                {(field) => (
                  <CustomerPOAutocomplete
                    {...field}
                    value={form.customer_po}
                    customerName={form.customer_name}
                    maxLength={50}
                    onChange={value => setForm(previous => ({ ...previous, customer_po: value }))}
                    onSelect={suggestion => {
                      if (!form.customer_name.trim() && suggestion.customer_name) {
                        setCustomerSearch(suggestion.customer_name);
                        setForm(previous => ({ ...previous, customer_name: suggestion.customer_name || '' }));
                      }
                    }}
                    className="input"
                  />
                )}
              </FormField>
            </div>

            <FormField label="Due Date">
              {(field) => (
                <input
                  {...field}
                  type="date"
                  value={form.due_date}
                  onChange={(e) => setForm({ ...form, due_date: e.target.value })}
                  className="input"
                />
              )}
            </FormField>

            <FormField
              label="Unit #"
              help="Optional. The unit this work order builds — shown on the kiosk, the shop TV and the work-order list. Leave blank for jobs that do not track one."
            >
              {(field) => (
                <input
                  {...field}
                  type="text"
                  maxLength={50}
                  value={form.unit_number}
                  onChange={(e) => setForm({ ...form, unit_number: e.target.value })}
                  className="input"
                  placeholder="e.g. 2410048"
                />
              )}
            </FormField>

            <FormField label="Notes">
              {(field) => (
                <textarea
                  {...field}
                  value={form.notes}
                  onChange={(e) => setForm({ ...form, notes: e.target.value })}
                  className="input"
                  rows={2}
                />
              )}
            </FormField>
          </div>
        </div>

        {partReadiness && (blockingReadinessMessages.length > 0 || informationalReadinessMessages.length > 0) && (
          <div className="bg-fd-panel border border-amber-500/30 rounded-lg p-4">
            <div className="flex items-start gap-3">
              <ExclamationTriangleIcon className="h-5 w-5 text-amber-300 mt-0.5" />
              <div>
                <div className="font-semibold text-white">Work order readiness</div>
                <div className="mt-2 space-y-1 text-sm text-slate-300">
                  {[...blockingReadinessMessages, ...informationalReadinessMessages].map((message) => (
                    <div key={message}>{message}</div>
                  ))}
                </div>
              </div>
            </div>
          </div>
        )}

        {partReadiness && blockingReadinessMessages.length === 0 && informationalReadinessMessages.length === 0 && (
          <div className="bg-emerald-500/10 border border-emerald-500/30 rounded-lg p-3 flex items-center gap-2 text-sm text-emerald-200">
            <CheckCircleIcon className="h-5 w-5" />
            Selected part is ready for a work order.
          </div>
        )}

        {routingLoadError && (
          <div role="alert" className="card border border-red-500/40 p-4">
            <p>{routingLoadError}</p>
            <button type="button" className="btn-secondary mt-2" onClick={() => handlePartChange(form.part_id)}>Retry readiness and routing</button>
          </div>
        )}
        {operationsOverridden && operations.length === 0 && (
          <p role="alert" className="text-amber-300">All preview operations were removed. Add an operation to continue.</p>
        )}
        {linkCheckError && (
          <div role="alert" className="card border border-red-500/40 p-4">
            <p>{linkCheckError}</p>
            <button
              type="button"
              className="btn-secondary mt-2"
              onClick={() => checkRoutingLinks(form.part_id, form.quantity_ordered || 1, partRequestRef.current)}
            >
              Retry routing check
            </button>
          </div>
        )}
        {hasManualOperations && staleLinkedOperations.length > 0 && (
          <div role="alert" data-testid="wo-stale-routing-links" className="bg-fd-panel border border-red-500/40 p-4">
            <div className="flex items-start gap-3">
              <ExclamationTriangleIcon className="h-5 w-5 text-red-300 mt-0.5 flex-shrink-0" />
              <div className="min-w-0">
                <div className="font-semibold text-white">Operations linked to a routing step that is no longer released</div>
                <p className="mt-1 text-sm text-slate-300">
                  These operations were copied from a routing step that is not on the part&apos;s released routing today,
                  so the work order cannot be created with them linked. Unlink them to keep them as hand-entered steps
                  (with no process-sheet checks), or reload the operations from the released routing.
                </p>
                <ul className="mt-2 space-y-0.5 text-sm text-slate-200">
                  {staleLinkedOperations.map((op, index) => (
                    <li key={`${op.routing_operation_id}-${index}`}>
                      {formatOperationLabel(op.operation_number, op.sequence)} — {op.name || 'Unnamed operation'}
                    </li>
                  ))}
                </ul>
                <div className="mt-3 flex flex-wrap gap-2">
                  <button type="button" className="btn-secondary btn-sm" onClick={unlinkStaleOperations}>
                    Unlink these operations
                  </button>
                  <button type="button" className="btn-secondary btn-sm" onClick={() => setConfirmReload(true)}>
                    Reload from released routing
                  </button>
                </div>
              </div>
            </div>
          </div>
        )}
        {missingSheetSteps.length > 0 && (
          <div role="alert" data-testid="wo-missing-sheet-steps" className="bg-fd-panel border border-amber-500/30 p-4">
            <div className="flex items-start gap-3">
              <ExclamationTriangleIcon className="h-5 w-5 text-amber-300 mt-0.5 flex-shrink-0" />
              <div className="min-w-0">
                <div className="font-semibold text-white">Process-sheet steps left off this work order</div>
                <p className="mt-1 text-sm text-slate-300">
                  These released routing steps carry process sheets, but no operation below is linked to them, so their
                  required checks will not appear at the kiosk. The work order can still be created; the left-off steps are
                  recorded on its creation record.
                </p>
                <ul className="mt-2 space-y-0.5 text-sm text-slate-200">
                  {missingSheetSteps.map(step => (
                    <li key={step.routing_operation_id}>
                      {formatOperationLabel(step.operation_number, step.sequence)} — {step.name} (sheet{' '}
                      {sheetLabel(step.process_sheet_number, step.process_sheet_id)})
                    </li>
                  ))}
                </ul>
              </div>
            </div>
          </div>
        )}
        {unreleasedSheetOperations.length > 0 && (
          <div role="alert" className="bg-fd-panel border border-amber-500/30 p-4 text-sm text-slate-300">
            <div className="font-semibold text-white">Process sheet not released</div>
            <p className="mt-1">
              Creating this work order will be refused until Quality releases a revision of:{' '}
              {unreleasedSheetOperations
                .map(op => `sheet ${sheetLabel(op.process_sheet_number, op.process_sheet_id)} (${formatOperationLabel(op.operation_number, op.sequence)})`)
                .join(', ')}
              .
            </p>
          </div>
        )}
        {/* Operations Card */}
        <div className="card">
          <div className="flex flex-wrap items-center justify-between gap-3 mb-4">
            <h2 className="text-lg font-semibold text-white">Operations</h2>
            {(operations.length > 0 || operationsOverridden) && (
              <button
                type="button"
                onClick={addManualOperation}
                className="btn-secondary btn-sm"
              >
                <PlusIcon className="h-4 w-4 mr-1" />
                Add Operation
              </button>
            )}
          </div>

          {loadingRouting && (
            <div className="flex items-center justify-center py-8">
              <div className="spinner h-8 w-8"></div>
              <span className="ml-3 text-slate-400">Loading routing...</span>
            </div>
          )}

          {!loadingRouting && form.part_id === 0 && (
            <div className="flex items-center gap-3 p-4 bg-slate-900/40 rounded-xl text-slate-400">
              <InformationCircleIcon className="h-5 w-5 flex-shrink-0" />
              <span>Select a part to see available operations</span>
            </div>
          )}

          {!loadingRouting && form.part_id > 0 && routing && operations.length > 0 && (
            <>
              <div className="flex items-center gap-2 mb-4 p-3 bg-emerald-500/10 border border-emerald-500/30 rounded-xl text-emerald-400">
                <CheckCircleIcon className="h-5 w-5 flex-shrink-0" />
                <span className="text-sm font-medium">
                  {routing.revision === 'BOM'
                    ? `Auto-populated from BOM component routings (${operations.length} operations)`
                    : routing.revision === ''
                      ? `Auto-populated from the part's released routing (${operations.length} operations)`
                      : `Auto-populated from routing Rev ${routing.revision} (${operations.length} operations)`
                  }
                </span>
              </div>

              <div className="overflow-x-auto">
                <table className="table">
                  <thead>
                    <tr>
                      <th className="w-20">Seq</th>
                      <th>Operation</th>
                      <th>Work Center</th>
                      <th className="w-28">Setup (min)</th>
                      <th className="w-28">Run (min)</th>
                      <th className="w-16" aria-label="Actions"></th>
                    </tr>
                  </thead>
                  <tbody>
                    {operations.map((op, index) => (
                      <React.Fragment key={index}>
                        <tr className={!op.fromRouting ? 'bg-amber-500/10' : ''}>
                          <td>
                            <input
                              type="number"
                              value={op.sequence}
                              onChange={(e) => updateOperation(index, 'sequence', parseInt(e.target.value) || 0)}
                              className="input input-sm w-16 text-center"
                              aria-label="Operation sequence"
                            />
                          </td>
                          <td>
                            <input
                              type="text"
                              value={op.name}
                              onChange={(e) => updateOperation(index, 'name', e.target.value)}
                              className="input input-sm"
                              placeholder="Operation name"
                              aria-label="Operation name"
                            />
                            <OperationSheetChip op={op} stale={staleLinkedOperations.includes(op)} />
                          </td>
                          <td>
                            <SelectField
                              value={op.work_center_id}
                              onChange={(workCenterId) => updateOperation(index, 'work_center_id', workCenterId)}
                              options={workCenterOptions}
                              searchable
                              placeholder="Select work center"
                              buttonClassName="input-sm"
                              menuClassName="min-w-72"
                              ariaLabel="Work center"
                            />
                          </td>
                          <td>
                            <input
                              type="number"
                              step="any"
                              min={0}
                              value={hoursToMinutes(op.setup_time_hours)}
                              onChange={(e) => updateOperation(index, 'setup_time_hours', minutesToHours(parseFloat(e.target.value) || 0))}
                              className="input input-sm text-right"
                              aria-label="Setup time in minutes"
                            />
                          </td>
                          <td>
                            <input
                              type="number"
                              step="any"
                              min={0}
                              value={hoursToMinutes(op.run_time_hours)}
                              onChange={(e) => updateOperation(index, 'run_time_hours', minutesToHours(parseFloat(e.target.value) || 0))}
                              className="input input-sm text-right"
                              aria-label="Run time in minutes"
                            />
                          </td>
                          <td>
                            <button
                              type="button"
                              onClick={() => removeOperation(index)}
                              className="p-1.5 rounded-lg text-slate-500 hover:text-red-600 hover:bg-red-500/100/10"
                              aria-label="Remove operation"
                            >
                              <TrashIcon className="h-4 w-4" />
                            </button>
                          </td>
                        </tr>
                        <tr className={!op.fromRouting ? 'bg-amber-500/10' : ''}>
                          <td aria-label="Spacer"></td>
                          <td colSpan={5} className="pb-3">
                            <details><summary className="cursor-pointer text-sm text-slate-300 mb-2">Setup and run instructions</summary><div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                              <div>
                                <label id={`routing-op-${index}-setup-instructions-label`} htmlFor={`routing-op-${index}-setup-instructions`} className="label text-xs">Setup Instructions</label>
                                <textarea
                                  id={`routing-op-${index}-setup-instructions`}
                                  value={op.setup_instructions || ''}
                                  onChange={(e) => updateOperation(index, 'setup_instructions', e.target.value)}
                                  className="input input-sm min-h-16"
                                  aria-labelledby={`routing-op-${index}-setup-instructions-label`}
                                />
                              </div>
                              <div>
                                <label id={`routing-op-${index}-run-instructions-label`} htmlFor={`routing-op-${index}-run-instructions`} className="label text-xs">Run Instructions</label>
                                <textarea
                                  id={`routing-op-${index}-run-instructions`}
                                  value={op.run_instructions || ''}
                                  onChange={(e) => updateOperation(index, 'run_instructions', e.target.value)}
                                  className="input input-sm min-h-16"
                                  aria-labelledby={`routing-op-${index}-run-instructions-label`}
                                />
                              </div>
                            </div></details>
                            <label className="mt-2 inline-flex items-center gap-2 text-xs text-slate-300">
                              <input
                                type="checkbox"
                                checked={Boolean(op.requires_inspection)}
                                onChange={(e) => updateOperation(index, 'requires_inspection', e.target.checked)}
                                className="rounded border-slate-600 bg-slate-800"
                                aria-label="Requires inspection"
                              />
                              Requires inspection
                            </label>
                          </td>
                        </tr>
                      </React.Fragment>
                    ))}
                  </tbody>
                </table>
              </div>

              {operations.some(op => !op.fromRouting) && (
                <p className="text-xs text-amber-600 mt-2">
                  * Yellow rows have been modified from the original routing
                </p>
              )}
            </>
          )}

          {!loadingRouting && !routingLoadError && form.part_id > 0 && !routing && (
            <>
              <div className="flex items-center gap-2 mb-4 p-3 bg-amber-500/10 border border-amber-500/30 rounded-xl text-amber-400">
                <ExclamationTriangleIcon className="h-5 w-5 flex-shrink-0" />
                <span className="text-sm">
                  {restoredFromDraft && operations.length > 0
                    ? 'Operations restored from your saved draft. Their routing links are checked against the part\'s released routing today.'
                    : 'No released routing found for this part. Add operations manually.'}
                </span>
              </div>

              {operations.length === 0 ? (
                <button
                  type="button"
                  onClick={addManualOperation}
                  className="w-full py-8 border-2 border-dashed border-slate-700 rounded-xl text-slate-400 hover:border-werco-400 hover:text-sky-200 transition-colors"
                >
                  <PlusIcon className="h-6 w-6 mx-auto mb-2" />
                  Add First Operation
                </button>
              ) : (
                <div className="overflow-x-auto">
                  <table className="table">
                    <thead>
                      <tr>
                        <th className="w-20">Seq</th>
                        <th>Operation</th>
                        <th>Work Center</th>
                        <th className="w-28">Setup (min)</th>
                        <th className="w-28">Run (min)</th>
                        <th className="w-16" aria-label="Actions"></th>
                      </tr>
                    </thead>
                    <tbody>
                      {operations.map((op, index) => (
                        <React.Fragment key={index}>
                          <tr>
                            <td>
                              <input
                                type="number"
                                value={op.sequence}
                                onChange={(e) => updateOperation(index, 'sequence', parseInt(e.target.value) || 0)}
                                className="input input-sm w-16 text-center"
                                aria-label="Operation sequence"
                              />
                            </td>
                            <td>
                              <input
                                type="text"
                                value={op.name}
                                onChange={(e) => updateOperation(index, 'name', e.target.value)}
                                className="input input-sm"
                                placeholder="Operation name"
                                required
                                aria-label="Operation name"
                              />
                              <OperationSheetChip op={op} stale={staleLinkedOperations.includes(op)} />
                            </td>
                            <td>
                              <SelectField
                                value={op.work_center_id}
                                onChange={(workCenterId) => updateOperation(index, 'work_center_id', workCenterId)}
                                options={workCenterOptions}
                                searchable
                                placeholder="Select work center"
                                buttonClassName="input-sm"
                                menuClassName="min-w-72"
                                ariaLabel="Work center"
                              />
                            </td>
                            <td>
                              <input
                                type="number"
                                step="any"
                                min={0}
                                value={hoursToMinutes(op.setup_time_hours)}
                                onChange={(e) => updateOperation(index, 'setup_time_hours', minutesToHours(parseFloat(e.target.value) || 0))}
                                className="input input-sm text-right"
                                aria-label="Setup time in minutes"
                              />
                            </td>
                            <td>
                              <input
                                type="number"
                                step="any"
                                min={0}
                                value={hoursToMinutes(op.run_time_hours)}
                                onChange={(e) => updateOperation(index, 'run_time_hours', minutesToHours(parseFloat(e.target.value) || 0))}
                                className="input input-sm text-right"
                                aria-label="Run time in minutes"
                              />
                            </td>
                            <td>
                              <button
                                type="button"
                                onClick={() => removeOperation(index)}
                                className="p-1.5 rounded-lg text-slate-500 hover:text-red-600 hover:bg-red-500/100/10"
                                aria-label="Remove operation"
                              >
                                <TrashIcon className="h-4 w-4" />
                              </button>
                            </td>
                          </tr>
                          <tr>
                            <td aria-label="Spacer"></td>
                            <td colSpan={5} className="pb-3">
                              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                                <div>
                                  <label id={`manual-op-${index}-setup-instructions-label`} htmlFor={`manual-op-${index}-setup-instructions`} className="label text-xs">Setup Instructions</label>
                                  <textarea
                                    id={`manual-op-${index}-setup-instructions`}
                                    value={op.setup_instructions || ''}
                                    onChange={(e) => updateOperation(index, 'setup_instructions', e.target.value)}
                                    className="input input-sm min-h-16"
                                    aria-labelledby={`manual-op-${index}-setup-instructions-label`}
                                  />
                                </div>
                                <div>
                                  <label id={`manual-op-${index}-run-instructions-label`} htmlFor={`manual-op-${index}-run-instructions`} className="label text-xs">Run Instructions</label>
                                  <textarea
                                    id={`manual-op-${index}-run-instructions`}
                                    value={op.run_instructions || ''}
                                    onChange={(e) => updateOperation(index, 'run_instructions', e.target.value)}
                                    className="input input-sm min-h-16"
                                    aria-labelledby={`manual-op-${index}-run-instructions-label`}
                                  />
                                </div>
                              </div>
                              <label className="mt-2 inline-flex items-center gap-2 text-xs text-slate-300">
                                <input
                                  type="checkbox"
                                  checked={Boolean(op.requires_inspection)}
                                  onChange={(e) => updateOperation(index, 'requires_inspection', e.target.checked)}
                                  className="rounded border-slate-600 bg-slate-800"
                                  aria-label="Requires inspection"
                                />
                                Requires inspection
                              </label>
                            </td>
                          </tr>
                        </React.Fragment>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </>
          )}
        </div>

        <ConfirmDialog
          open={confirmReload}
          title="Reload operations from the released routing?"
          message="This replaces every operation below with the part's released routing as it is today. Your edits to the operations are discarded."
          confirmLabel="Reload operations"
          variant="warning"
          onCancel={() => setConfirmReload(false)}
          onConfirm={() => {
            setConfirmReload(false);
            void handlePartChange(form.part_id);
          }}
        />

        {/* Actions */}
        <div className="flex flex-wrap justify-end gap-3">
          <button
            type="button"
            onClick={() => {
              if (!confirmDiscard()) return;
              navigate('/work-orders');
            }}
            className="btn-secondary"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={submitting || !form.part_id || loadingRouting || !!routingLoadError || (operationsOverridden && operations.length === 0)}
            className="btn-primary"
          >
            {submitting ? 'Creating...' : 'Create Work Order'}
          </button>
        </div>
      </form>
    </div>
  );
}
