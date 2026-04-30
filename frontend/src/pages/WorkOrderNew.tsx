import React, { useEffect, useMemo, useState } from 'react';
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
import { SelectField, SelectOption } from '../components/ui';

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
  fromRouting: boolean;
}

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

export default function WorkOrderNew() {
  const navigate = useNavigate();
  const [parts, setParts] = useState<Part[]>([]);
  const [activeBOMs, setActiveBOMs] = useState<BOMSummary[]>([]);
  const [workCenters, setWorkCenters] = useState<WorkCenter[]>([]);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [loadingRouting, setLoadingRouting] = useState(false);
  const [routing, setRouting] = useState<Routing | null>(null);
  const [operations, setOperations] = useState<OperationPreview[]>([]);
  const [showManualEntry, setShowManualEntry] = useState(false);
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
    due_date: '',
    notes: ''
  });

  useEffect(() => {
    loadInitialData();
  }, []);

  const loadInitialData = async () => {
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

  const normalizedPartSearch = partSearch.trim().toLowerCase();
  const workOrderPartOptions = useMemo(() => {
    const eligibleParts = parts.filter((part) => ['assembly', 'manufactured'].includes(part.part_type));

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
          score = part.part_type === 'assembly' ? 1 : usage.length > 0 ? 2 : 3;
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
  const priorityOptions: SelectOption<number>[] = [
    { value: 1, label: '1 - Critical' },
    { value: 2, label: '2 - Urgent' },
    { value: 3, label: '3 - High' },
    { value: 5, label: '5 - Normal' },
    { value: 7, label: '7 - Low' },
    { value: 10, label: '10 - Lowest' },
  ];
  const workCenterOptions = useMemo<SelectOption<number>[]>(() => (
    workCenters.map((workCenter) => ({
      value: workCenter.id,
      label: workCenter.name,
      description: workCenter.code,
    }))
  ), [workCenters]);

  const formatPartType = (partType: string) => (
    partType
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
      alert(err.response?.data?.detail || 'Failed to create customer');
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

    if (!partId) return;

    // Find the selected part to check if it's an assembly
    const isAssembly = selectedPart?.part_type === 'assembly';

    setLoadingRouting(true);
    try {
      try {
        setPartReadiness(await api.getPartReadiness(partId));
      } catch (readinessErr) {
        console.error('Failed to load part readiness:', readinessErr);
        setPartReadiness(null);
      }

      if (isAssembly) {
        // For assemblies, use the preview endpoint to get combined operations from BOM components
        const previewRes = await api.previewWorkOrderOperations(partId, form.quantity_ordered);
        if (previewRes && previewRes.operations_preview?.length > 0) {
          // Create a fake routing object to indicate we have operations
          setRouting({ id: 0, part_id: partId, revision: 'BOM', status: 'released', operations: [] } as any);
          const ops: OperationPreview[] = previewRes.operations_preview.map((op: any, index: number) => ({
            sequence: (index + 1) * 10,
            operation_number: `Op ${(index + 1) * 10}`,
            name: op.name,
            work_center_id: op.work_center_id,
            work_center_name: op.work_center_name || '',
            setup_time_hours: op.setup_hours || 0,
            run_time_hours: (op.run_hours_per_unit || 0) * (op.component_quantity || form.quantity_ordered),
            fromRouting: true,
            component_part_id: op.component_part_id,
            component_quantity: op.component_quantity
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
        const routingRes = await api.getRoutingByPart(partId);
        if (routingRes && routingRes.operations?.length > 0) {
          setRouting(routingRes);
          const ops: OperationPreview[] = routingRes.operations
            .filter((op: RoutingOperation) => op.work_center)
            .map((op: RoutingOperation) => ({
              sequence: op.sequence,
              operation_number: op.operation_number || `Op ${op.sequence}`,
              name: op.name,
              work_center_id: op.work_center_id,
              work_center_name: op.work_center?.name || '',
              setup_time_hours: op.setup_hours,
              run_time_hours: op.run_hours_per_unit * form.quantity_ordered,
              fromRouting: true
            }));
          setOperations(ops);
        } else {
          setShowManualEntry(true);
        }
      }
    } catch (err) {
      console.error('Failed to load routing:', err);
      setShowManualEntry(true);
    } finally {
      setLoadingRouting(false);
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
        run_time_hours: op.fromRouting 
          ? (routing.operations.find(r => r.sequence === op.sequence)?.run_hours_per_unit || 0) * qty
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
      operation_number: `Op ${nextSeq}`,
      name: '',
      work_center_id: workCenters[0]?.id || 0,
      work_center_name: workCenters[0]?.name || '',
      setup_time_hours: 0,
      run_time_hours: 0,
      fromRouting: false
    }]);
  };

  const removeOperation = (index: number) => {
    setOperations(ops => ops.filter((_, i) => i !== index));
  };

  const hasManualOperations = operations.length > 0 && (showManualEntry || operations.some(op => !op.fromRouting));
  const manualOperationsAreValid = hasManualOperations
    && operations.every(op => op.name.trim().length > 0 && op.work_center_id > 0);
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
    if (!form.part_id) {
      alert('Please select a part');
      return;
    }
    if (blockingReadinessMessages.length) {
      alert(`This part is not ready for a work order:\n\n${blockingReadinessMessages.join('\n')}`);
      return;
    }
    if (hasManualOperations && !manualOperationsAreValid) {
      alert('Please complete every manual operation with an operation name and work center.');
      return;
    }

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

      // If operations were modified or manually entered, include them
      if (hasManualOperations) {
        payload.operations = operations.map(op => ({
          sequence: op.sequence,
          operation_number: op.operation_number,
          name: op.name,
          work_center_id: op.work_center_id,
          setup_time_hours: op.setup_time_hours,
          run_time_hours: op.run_time_hours,
          status: 'pending'
        }));
      } else {
        payload.operations = [];
      }

      const result = await api.createWorkOrder(payload);
      navigate(`/work-orders/${result.id}`);
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to create work order');
    } finally {
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

  return (
    <div className="max-w-4xl mx-auto">
      <h1 className="text-2xl font-bold text-white mb-6">New Work Order</h1>

      <form onSubmit={handleSubmit} className="space-y-6">
        {/* Basic Info Card */}
        <div className="card">
          <h2 className="text-lg font-semibold text-white mb-4">Work Order Details</h2>
          
          <div className="space-y-4">
            <div>
              <label className="label">Part *</label>
              <div className="relative">
                <div className="relative">
                  <MagnifyingGlassIcon className="h-5 w-5 absolute left-3 top-1/2 -translate-y-1/2 text-slate-500 pointer-events-none" />
                  <input
                    type="text"
                    value={partSearch}
                    onChange={(e) => {
                      setPartSearch(e.target.value);
                      setShowPartDropdown(true);
                      if (form.part_id) {
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
                    className="absolute z-20 w-full mt-1 bg-[#151b28] border border-slate-700 rounded-xl shadow-xl max-h-96 overflow-y-auto"
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
                              {part.part_type === 'assembly' && (
                                <span className="shrink-0 rounded-md bg-blue-500/10 px-2 py-1 text-xs font-medium text-blue-200 border border-blue-500/20">
                                  Assembly
                                </span>
                              )}
                            </div>
                          </button>
                        ))}
                        {parts.filter((part) => ['assembly', 'manufactured'].includes(part.part_type)).length > workOrderPartOptions.length && (
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
            </div>

            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="label">Quantity *</label>
                <input
                  type="number"
                  value={form.quantity_ordered}
                  onChange={(e) => handleQuantityChange(parseInt(e.target.value) || 1)}
                  className="input"
                  min={1}
                  required
                />
              </div>
              <div>
                <label className="label">Priority</label>
                <SelectField
                  value={form.priority}
                  onChange={(priority) => setForm({ ...form, priority })}
                  options={priorityOptions}
                  ariaLabel="Priority"
                />
              </div>
            </div>

            <div className="grid grid-cols-2 gap-4">
              <div className="relative">
                <label className="label">Customer Name</label>
                <div className="relative">
                  <input
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
                  <div className="absolute z-10 w-full mt-1 bg-[#151b28] border border-slate-700 rounded-md shadow-lg max-h-48 overflow-y-auto">
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
              </div>
              <div>
                <label className="label">Customer PO #</label>
                <input
                  type="text"
                  value={form.customer_po}
                  onChange={(e) => setForm({ ...form, customer_po: e.target.value })}
                  className="input"
                />
              </div>
            </div>

            <div>
              <label className="label">Due Date</label>
              <input
                type="date"
                value={form.due_date}
                onChange={(e) => setForm({ ...form, due_date: e.target.value })}
                className="input"
              />
            </div>

            <div>
              <label className="label">Notes</label>
              <textarea
                value={form.notes}
                onChange={(e) => setForm({ ...form, notes: e.target.value })}
                className="input"
                rows={2}
              />
            </div>
          </div>
        </div>

        {partReadiness && (blockingReadinessMessages.length > 0 || informationalReadinessMessages.length > 0) && (
          <div className="bg-[#151b28] border border-amber-500/30 rounded-lg p-4">
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

        {/* Operations Card */}
        <div className="card">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-lg font-semibold text-white">Operations</h2>
            {operations.length > 0 && (
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
                      <th className="w-16"></th>
                    </tr>
                  </thead>
                  <tbody>
                    {operations.map((op, index) => (
                      <tr key={index} className={!op.fromRouting ? 'bg-amber-500/10' : ''}>
                        <td>
                          <input
                            type="number"
                            value={op.sequence}
                            onChange={(e) => updateOperation(index, 'sequence', parseInt(e.target.value) || 0)}
                            className="input input-sm w-16 text-center"
                          />
                        </td>
                        <td>
                          <input
                            type="text"
                            value={op.name}
                            onChange={(e) => updateOperation(index, 'name', e.target.value)}
                            className="input input-sm"
                            placeholder="Operation name"
                          />
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
                            step="0.1"
                            min={0}
                            value={hoursToMinutes(op.setup_time_hours)}
                            onChange={(e) => updateOperation(index, 'setup_time_hours', minutesToHours(parseFloat(e.target.value) || 0))}
                            className="input input-sm text-right"
                          />
                        </td>
                        <td>
                          <input
                            type="number"
                            step="0.1"
                            min={0}
                            value={hoursToMinutes(op.run_time_hours)}
                            onChange={(e) => updateOperation(index, 'run_time_hours', minutesToHours(parseFloat(e.target.value) || 0))}
                            className="input input-sm text-right"
                          />
                        </td>
                        <td>
                          <button
                            type="button"
                            onClick={() => removeOperation(index)}
                            className="p-1.5 rounded-lg text-slate-500 hover:text-red-600 hover:bg-red-500/100/10"
                          >
                            <TrashIcon className="h-4 w-4" />
                          </button>
                        </td>
                      </tr>
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

          {!loadingRouting && form.part_id > 0 && !routing && (
            <>
              <div className="flex items-center gap-2 mb-4 p-3 bg-amber-500/10 border border-amber-500/30 rounded-xl text-amber-400">
                <ExclamationTriangleIcon className="h-5 w-5 flex-shrink-0" />
                <span className="text-sm">
                  No released routing found for this part. Add operations manually.
                </span>
              </div>

              {operations.length === 0 ? (
                <button
                  type="button"
                  onClick={addManualOperation}
                  className="w-full py-8 border-2 border-dashed border-slate-700 rounded-xl text-slate-400 hover:border-werco-400 hover:text-werco-600 transition-colors"
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
                        <th className="w-16"></th>
                      </tr>
                    </thead>
                    <tbody>
                      {operations.map((op, index) => (
                        <tr key={index}>
                          <td>
                            <input
                              type="number"
                              value={op.sequence}
                              onChange={(e) => updateOperation(index, 'sequence', parseInt(e.target.value) || 0)}
                              className="input input-sm w-16 text-center"
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
                            />
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
                              step="0.1"
                              min={0}
                              value={hoursToMinutes(op.setup_time_hours)}
                              onChange={(e) => updateOperation(index, 'setup_time_hours', minutesToHours(parseFloat(e.target.value) || 0))}
                              className="input input-sm text-right"
                            />
                          </td>
                          <td>
                            <input
                              type="number"
                              step="0.1"
                              min={0}
                              value={hoursToMinutes(op.run_time_hours)}
                              onChange={(e) => updateOperation(index, 'run_time_hours', minutesToHours(parseFloat(e.target.value) || 0))}
                              className="input input-sm text-right"
                            />
                          </td>
                          <td>
                            <button
                              type="button"
                              onClick={() => removeOperation(index)}
                              className="p-1.5 rounded-lg text-slate-500 hover:text-red-600 hover:bg-red-500/100/10"
                            >
                              <TrashIcon className="h-4 w-4" />
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </>
          )}
        </div>

        {/* Actions */}
        <div className="flex justify-end gap-3">
          <button
            type="button"
            onClick={() => navigate('/work-orders')}
            className="btn-secondary"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={submitting || !form.part_id}
            className="btn-primary"
          >
            {submitting ? 'Creating...' : 'Create Work Order'}
          </button>
        </div>
      </form>
    </div>
  );
}
