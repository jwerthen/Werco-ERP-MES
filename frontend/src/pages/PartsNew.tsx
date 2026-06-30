import React, { useEffect, useState, useMemo, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import api from '../services/api';
import { Part, PartType } from '../types';
import { CustomerNameOption } from '../types/api';
import { partTypeColors } from '../types/engineering';
import { ENGINEERING_PART_TYPE_OPTIONS } from '../utils/catalogGroups';
import { StatusBadge } from '../components/ui/StatusBadge';
import { Modal } from '../components/ui/Modal';
import { FormField } from '../components/ui';
import { useToast } from '../components/ui/Toast';
import useUnsavedChanges from '../hooks/useUnsavedChanges';
import { BOMImportWizard } from '../components/parts/BOMImportWizard';
import { SkeletonTable } from '../components/ui/Skeleton';
import {
  PlusIcon,
  MagnifyingGlassIcon,
  ArrowUpTrayIcon,
  ArrowDownTrayIcon,
  BookmarkIcon,
  ChevronDownIcon,
  ChevronRightIcon,
  ClipboardDocumentListIcon,
  Squares2X2Icon,
  ListBulletIcon as ListIcon,
  TrashIcon,
  XMarkIcon,
} from '@heroicons/react/24/outline';

type ViewMode = 'table' | 'grid';

const SAVED_FILTERS_KEY = 'werco.parts.savedFilters.v1';
const INITIAL_CREATE_FORM = {
  part_number: '',
  name: '',
  part_type: 'manufactured' as PartType,
  description: '',
  revision: 'A',
  standard_cost: 0,
  is_critical: false,
  requires_inspection: true,
  customer_name: '',
  customer_part_number: '',
  drawing_number: '',
};

interface SavedPartFilter {
  id: string;
  name: string;
  search: string;
  typeFilter: string;
  statusFilter: string;
  showBOMComponents: boolean;
  viewMode: ViewMode;
  createdAt: string;
}

interface BOMComponentPart {
  id: number;
  part_number: string;
  name: string;
  revision: string;
  part_type: PartType;
}

interface BOMItemSummary {
  id: number;
  component_part_id: number;
  quantity: number;
  item_number: number;
  component_part?: BOMComponentPart;
}

interface BOMSummary {
  part_id: number;
  items?: BOMItemSummary[];
}

export default function PartsPage() {
  const navigate = useNavigate();
  const { showToast } = useToast();

  const [parts, setParts] = useState<Part[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState('');
  const [debouncedSearch, setDebouncedSearch] = useState('');
  const [typeFilter, setTypeFilter] = useState<string>('');
  const [statusFilter, setStatusFilter] = useState<string>('');
  const [viewMode, setViewMode] = useState<ViewMode>('table');
  const [showImport, setShowImport] = useState(false);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [showBOMComponents, setShowBOMComponents] = useState(false);
  const [expandedParts, setExpandedParts] = useState<Set<number>>(new Set());
  const [bomData, setBomData] = useState<Record<number, BOMItemSummary[]>>({});
  const [componentPartIds, setComponentPartIds] = useState<Set<number>>(new Set());
  const [savedFilters, setSavedFilters] = useState<SavedPartFilter[]>([]);
  const [selectedPartIds, setSelectedPartIds] = useState<Set<number>>(new Set());
  const [customerOptions, setCustomerOptions] = useState<CustomerNameOption[]>([]);
  const [showCustomerDropdown, setShowCustomerDropdown] = useState(false);
  const [highlightedCustomerIndex, setHighlightedCustomerIndex] = useState(-1);
  const [createDrawingPdf, setCreateDrawingPdf] = useState<File | null>(null);
  const [createDrawingInputKey, setCreateDrawingInputKey] = useState(0);
  const [creatingPart, setCreatingPart] = useState(false);

  // Create form
  const [createForm, setCreateForm] = useState(INITIAL_CREATE_FORM);
  // Snapshot of the form values the modal opened with, for unsaved-changes detection.
  const [initialCreateForm, setInitialCreateForm] = useState(INITIAL_CREATE_FORM);

  const isCreateFormDirty = useMemo(
    () =>
      showCreateModal &&
      (JSON.stringify(createForm) !== JSON.stringify(initialCreateForm) || createDrawingPdf !== null),
    [showCreateModal, createForm, initialCreateForm, createDrawingPdf]
  );
  const { confirmDiscard } = useUnsavedChanges(isCreateFormDirty);

  const loadParts = useCallback(async () => {
    try {
      setLoading(true);
      const params: any = { include_bom_components: showBOMComponents };
      if (typeFilter) params.part_type = typeFilter;
      if (debouncedSearch) params.search = debouncedSearch;
      const [partsResult, bomsResult] = await Promise.allSettled([
        api.getParts(params),
        api.getBOMs({ active_only: true, limit: 5000 }),
      ]);

      if (partsResult.status === 'fulfilled') {
        setParts(partsResult.value);
      } else {
        throw partsResult.reason;
      }

      if (bomsResult.status === 'fulfilled') {
        const componentIds = new Set<number>();
        const bomMap: Record<number, BOMItemSummary[]> = {};
        (bomsResult.value as BOMSummary[]).forEach(bom => {
          const items = bom.items || [];
          bomMap[bom.part_id] = items;
          items.forEach(item => componentIds.add(item.component_part_id));
        });
        setBomData(bomMap);
        setComponentPartIds(componentIds);
      } else {
        setBomData({});
        setComponentPartIds(new Set());
      }
    } catch {
      showToast('error', 'Failed to load parts');
    } finally {
      setLoading(false);
    }
  }, [typeFilter, showBOMComponents, debouncedSearch, showToast]);

  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedSearch(search.trim()), 250);
    return () => window.clearTimeout(timer);
  }, [search]);

  useEffect(() => {
    loadParts();
  }, [loadParts]);

  useEffect(() => {
    let mounted = true;

    api.getCustomerNames()
      .then(customers => {
        if (!mounted) return;
        setCustomerOptions([...customers].sort((a, b) => a.name.localeCompare(b.name)));
      })
      .catch(() => {
        if (mounted) setCustomerOptions([]);
      });

    return () => {
      mounted = false;
    };
  }, []);

  useEffect(() => {
    try {
      const stored = window.localStorage.getItem(SAVED_FILTERS_KEY);
      if (stored) setSavedFilters(JSON.parse(stored));
    } catch {
      setSavedFilters([]);
    }
  }, []);

  const filteredParts = useMemo(() => {
    let result = parts;
    if (search) {
      const s = search.toLowerCase();
      result = result.filter(p =>
        p.part_number.toLowerCase().includes(s) ||
        p.name.toLowerCase().includes(s) ||
        (p.customer_name || '').toLowerCase().includes(s) ||
        (p.customer_part_number || '').toLowerCase().includes(s) ||
        (p.description || '').toLowerCase().includes(s)
      );
    }
    if (statusFilter) {
      result = result.filter(p => p.status === statusFilter);
    }
    if (!search && !showBOMComponents && componentPartIds.size > 0) {
      result = result.filter(p => !componentPartIds.has(p.id));
    }
    return result;
  }, [parts, search, statusFilter, showBOMComponents, componentPartIds]);

  const stats = useMemo(() => ({
    total: parts.length,
    active: parts.filter(p => p.status === 'active').length,
    manufactured: parts.filter(p => p.part_type === 'manufactured' || p.part_type === 'assembly').length,
    critical: parts.filter(p => p.is_critical).length,
  }), [parts]);

  const getPartById = useCallback((partId: number) => parts.find(p => p.id === partId), [parts]);
  const selectedParts = useMemo(() => parts.filter(part => selectedPartIds.has(part.id)), [parts, selectedPartIds]);
  const visibleSelectedCount = useMemo(
    () => filteredParts.filter(part => selectedPartIds.has(part.id)).length,
    [filteredParts, selectedPartIds]
  );
  const allVisibleSelected = filteredParts.length > 0 && visibleSelectedCount === filteredParts.length;
  const hasActiveFilters = Boolean(search || typeFilter || statusFilter || showBOMComponents);
  const normalizedCustomerSearch = createForm.customer_name.trim().toLowerCase();
  const matchingCustomers = useMemo(() => {
    if (!normalizedCustomerSearch) return customerOptions;

    return customerOptions.filter(customer =>
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

  useEffect(() => {
    setSelectedPartIds(prev => {
      const validIds = new Set(parts.map(part => part.id));
      const next = new Set(Array.from(prev).filter(id => validIds.has(id)));
      return next.size === prev.size ? prev : next;
    });
  }, [parts]);

  useEffect(() => {
    setHighlightedCustomerIndex(-1);
  }, [createForm.customer_name, showCustomerDropdown]);

  const toggleExpanded = useCallback((partId: number, event: React.MouseEvent) => {
    event.stopPropagation();
    setExpandedParts(prev => {
      const next = new Set(prev);
      if (next.has(partId)) next.delete(partId);
      else next.add(partId);
      return next;
    });
  }, []);

  const persistSavedFilters = (filters: SavedPartFilter[]) => {
    setSavedFilters(filters);
    window.localStorage.setItem(SAVED_FILTERS_KEY, JSON.stringify(filters));
  };

  const saveCurrentFilter = () => {
    const name = window.prompt('Name this parts filter', search || typeFilter || statusFilter || 'Parts filter');
    if (!name?.trim()) return;

    const nextFilter: SavedPartFilter = {
      id: `${Date.now()}`,
      name: name.trim(),
      search,
      typeFilter,
      statusFilter,
      showBOMComponents,
      viewMode,
      createdAt: new Date().toISOString(),
    };
    persistSavedFilters([nextFilter, ...savedFilters].slice(0, 12));
    showToast('success', `Saved filter "${nextFilter.name}"`);
  };

  const applySavedFilter = (filter: SavedPartFilter) => {
    setSearch(filter.search);
    setTypeFilter(filter.typeFilter);
    setStatusFilter(filter.statusFilter);
    setShowBOMComponents(filter.showBOMComponents);
    setViewMode(filter.viewMode);
    showToast('success', `Applied "${filter.name}"`);
  };

  const deleteSavedFilter = (filterId: string, event: React.MouseEvent) => {
    event.stopPropagation();
    persistSavedFilters(savedFilters.filter(filter => filter.id !== filterId));
  };

  const clearFilters = () => {
    setSearch('');
    setTypeFilter('');
    setStatusFilter('');
    setShowBOMComponents(false);
  };

  const toggleSelectedPart = (partId: number, event: React.MouseEvent | React.ChangeEvent<HTMLInputElement>) => {
    event.stopPropagation();
    setSelectedPartIds(prev => {
      const next = new Set(prev);
      if (next.has(partId)) next.delete(partId);
      else next.add(partId);
      return next;
    });
  };

  const toggleAllVisible = (event: React.ChangeEvent<HTMLInputElement>) => {
    event.stopPropagation();
    setSelectedPartIds(prev => {
      const next = new Set(prev);
      if (allVisibleSelected) {
        filteredParts.forEach(part => next.delete(part.id));
      } else {
        filteredParts.forEach(part => next.add(part.id));
      }
      return next;
    });
  };

  const buildPartsCsv = (rows: Part[]) => {
    const headers = [
      'part_number',
      'name',
      'revision',
      'part_type',
      'status',
      'customer_name',
      'customer_part_number',
      'drawing_number',
      'standard_cost',
      'is_critical',
      'requires_inspection',
    ];
    const escapeCell = (value: unknown) => {
      const text = String(value ?? '');
      return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
    };
    return [
      headers.join(','),
      ...rows.map(part => headers.map(header => escapeCell((part as unknown as Record<string, unknown>)[header])).join(',')),
    ].join('\n');
  };

  const exportSelectedParts = () => {
    if (selectedParts.length === 0) return;
    const blob = new Blob([buildPartsCsv(selectedParts)], { type: 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `werco-selected-parts-${new Date().toISOString().slice(0, 10)}.csv`;
    link.click();
    URL.revokeObjectURL(url);
    showToast('success', `Exported ${selectedParts.length} part${selectedParts.length !== 1 ? 's' : ''}`);
  };

  const copySelectedParts = async () => {
    if (selectedParts.length === 0) return;
    const text = selectedParts
      .map(part => `${part.part_number}\t${part.revision}\t${part.name}\t${part.part_type}\t${part.status}`)
      .join('\n');
    try {
      await navigator.clipboard.writeText(text);
      showToast('success', `Copied ${selectedParts.length} part${selectedParts.length !== 1 ? 's' : ''}`);
    } catch {
      showToast('error', 'Could not copy selected parts');
    }
  };

  const selectCustomer = (customerName: string) => {
    setCreateForm(prev => ({ ...prev, customer_name: customerName }));
    setShowCustomerDropdown(false);
    setHighlightedCustomerIndex(-1);
  };

  const handleCustomerKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      if (!showCustomerDropdown) {
        setShowCustomerDropdown(true);
        return;
      }
      if (filteredCustomers.length === 0) return;
      setHighlightedCustomerIndex(prev => {
        const next = prev + 1;
        return next >= filteredCustomers.length ? 0 : next;
      });
      return;
    }

    if (event.key === 'ArrowUp') {
      event.preventDefault();
      if (!showCustomerDropdown) {
        setShowCustomerDropdown(true);
        return;
      }
      if (filteredCustomers.length === 0) return;
      setHighlightedCustomerIndex(prev => {
        const next = prev - 1;
        return next < 0 ? filteredCustomers.length - 1 : next;
      });
      return;
    }

    if (event.key === 'Enter' && showCustomerDropdown && highlightedCustomerIndex >= 0) {
      event.preventDefault();
      const customer = filteredCustomers[highlightedCustomerIndex];
      if (customer) selectCustomer(customer.name);
      return;
    }

    if (event.key === 'Escape') {
      setShowCustomerDropdown(false);
      setHighlightedCustomerIndex(-1);
    }
  };

  const openCreateModal = () => {
    setCreateForm(INITIAL_CREATE_FORM);
    setInitialCreateForm(INITIAL_CREATE_FORM);
    setCreateDrawingPdf(null);
    setShowCreateModal(true);
  };

  const closeCreateModal = () => {
    if (creatingPart) return;
    if (!confirmDiscard()) return;
    setShowCreateModal(false);
    setCreateDrawingPdf(null);
    setCreateDrawingInputKey(key => key + 1);
  };

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (createDrawingPdf) {
      const isPdf = createDrawingPdf.type === 'application/pdf' || createDrawingPdf.name.toLowerCase().endsWith('.pdf');
      if (!isPdf) {
        showToast('error', 'Please choose a PDF drawing file');
        return;
      }
    }

    setCreatingPart(true);
    try {
      const newPart = await api.createPart(createForm);

      if (createDrawingPdf) {
        try {
          const formData = new FormData();
          formData.append('file', createDrawingPdf);
          formData.append('title', createDrawingPdf.name.replace(/\.pdf$/i, '') || `${newPart.part_number} Drawing`);
          formData.append('document_type', 'drawing');
          formData.append('revision', newPart.revision || createForm.revision || 'A');
          formData.append('part_id', String(newPart.id));
          await api.uploadDocument(formData);
          showToast('success', `Part ${newPart.part_number} created with drawing PDF`);
        } catch (uploadErr: any) {
          showToast('error', uploadErr.response?.data?.detail || `Part ${newPart.part_number} was created, but the PDF upload failed`);
        }
      } else {
        showToast('success', `Part ${newPart.part_number} created`);
      }

      setShowCreateModal(false);
      setCreateForm(INITIAL_CREATE_FORM);
      setCreateDrawingPdf(null);
      setCreateDrawingInputKey(key => key + 1);
      navigate(`/parts/${newPart.id}`);
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to create part');
    } finally {
      setCreatingPart(false);
    }
  };

  const removeDeletedParts = (partIds: number[]) => {
    const deletedIds = new Set(partIds);
    setParts(prev => prev.filter(part => !deletedIds.has(part.id)));
    setSelectedPartIds(prev => {
      const next = new Set(prev);
      partIds.forEach(id => next.delete(id));
      return next;
    });
    setExpandedParts(prev => {
      const next = new Set(prev);
      partIds.forEach(id => next.delete(id));
      return next;
    });
    setBomData(prev => {
      const next = { ...prev };
      partIds.forEach(id => delete next[id]);
      return next;
    });
  };

  const handleDeletePart = async (part: Part, event: React.MouseEvent) => {
    event.stopPropagation();
    if (!window.confirm(`Delete part ${part.part_number}? This will remove it from the active parts list.`)) return;

    try {
      await api.deletePart(part.id);
      removeDeletedParts([part.id]);
      showToast('success', `Deleted ${part.part_number}`);
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to delete part');
    }
  };

  const handleDeleteSelectedParts = async () => {
    if (selectedParts.length === 0) return;
    if (!window.confirm(`Delete ${selectedParts.length} selected part${selectedParts.length !== 1 ? 's' : ''}? This will remove them from the active parts list.`)) return;

    const results = await Promise.allSettled(selectedParts.map(part => api.deletePart(part.id)));
    const deletedIds = selectedParts
      .filter((_, index) => results[index].status === 'fulfilled')
      .map(part => part.id);
    if (deletedIds.length > 0) removeDeletedParts(deletedIds);

    const failed = results.length - deletedIds.length;
    if (failed > 0) {
      const firstFailure = results.find((result): result is PromiseRejectedResult => result.status === 'rejected');
      showToast('error', firstFailure?.reason?.response?.data?.detail || `Failed to delete ${failed} part${failed !== 1 ? 's' : ''}`);
    } else {
      showToast('success', `Deleted ${deletedIds.length} part${deletedIds.length !== 1 ? 's' : ''}`);
    }
  };

  if (loading) {
    return (
      <div className="space-y-6">
        <div className="flex justify-between items-center">
          <div className="h-8 w-24 bg-slate-700 rounded animate-pulse" />
          <div className="h-10 w-32 bg-slate-700 rounded animate-pulse" />
        </div>
        <div className="card"><SkeletonTable rows={8} columns={8} /></div>
      </div>
    );
  }

  return (
    <div className="space-y-5" data-tour="eng-parts">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold text-white">Parts</h1>
          <p className="text-sm text-slate-400 mt-0.5 tabular-nums">{stats.total} parts · {stats.active} active · {stats.manufactured} mfg/assembly · {stats.critical} critical</p>
        </div>
        <div className="flex gap-2">
          <button onClick={() => setShowImport(true)} className="btn-secondary flex items-center gap-2">
            <ArrowUpTrayIcon className="h-4 w-4" />
            Import
          </button>
          <button onClick={openCreateModal} className="btn-primary flex items-center gap-2">
            <PlusIcon className="h-4 w-4" />
            New Part
          </button>
        </div>
      </div>

      {/* Filters */}
      <div className="flex flex-col sm:flex-row gap-3 items-start sm:items-center">
        <div className="relative flex-1 max-w-md">
          <MagnifyingGlassIcon className="h-4 w-4 absolute left-3 top-1/2 -translate-y-1/2 text-slate-500" />
          <input
            type="text"
            placeholder="Search parts, customers, descriptions..."
            value={search}
            onChange={e => setSearch(e.target.value)}
            className="input pl-9 py-2 text-sm"
          />
        </div>
        <div className="flex flex-wrap gap-2 items-center">
          <select value={typeFilter} onChange={e => setTypeFilter(e.target.value)} className="input py-2 text-sm w-40">
            <option value="">All Types</option>
            {ENGINEERING_PART_TYPE_OPTIONS.map(option => (
              <option key={option.value} value={option.value}>{option.label}</option>
            ))}
          </select>
          <select value={statusFilter} onChange={e => setStatusFilter(e.target.value)} className="input py-2 text-sm w-32">
            <option value="">All Status</option>
            <option value="active">Active</option>
            <option value="obsolete">Obsolete</option>
            <option value="pending_approval">Pending</option>
          </select>
          <label className="flex items-center gap-2 text-sm text-slate-300 whitespace-nowrap">
            <input
              type="checkbox"
              checked={showBOMComponents}
              onChange={e => setShowBOMComponents(e.target.checked)}
              className="rounded border-slate-600 text-werco-navy-400"
            />
            Show BOM components as top-level parts
          </label>
          {/* View toggle */}
          <div className="flex rounded-sm border border-fd-line overflow-hidden">
            <button
              onClick={() => setViewMode('table')}
              className={`p-2 ${viewMode === 'table' ? 'bg-slate-800' : 'bg-fd-panel hover:bg-slate-800/50'}`}
              title="Table view"
            >
              <ListIcon className="h-4 w-4 text-slate-400" />
            </button>
            <button
              onClick={() => setViewMode('grid')}
              className={`p-2 ${viewMode === 'grid' ? 'bg-slate-800' : 'bg-fd-panel hover:bg-slate-800/50'}`}
              title="Grid view"
            >
              <Squares2X2Icon className="h-4 w-4 text-slate-400" />
            </button>
          </div>
          <button
            type="button"
            onClick={saveCurrentFilter}
            className="btn-secondary flex items-center gap-2 text-sm"
          >
            <BookmarkIcon className="h-4 w-4" />
            Save Filter
          </button>
          {hasActiveFilters && (
            <button type="button" onClick={clearFilters} className="btn-secondary flex items-center gap-2 text-sm">
              <XMarkIcon className="h-4 w-4" />
              Clear
            </button>
          )}
        </div>
      </div>

      {savedFilters.length > 0 && (
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs uppercase tracking-wide text-slate-500">Saved</span>
          {savedFilters.map(filter => (
            <div
              key={filter.id}
              className="inline-flex items-center rounded-sm border border-fd-line bg-fd-sunken text-sm text-slate-200"
            >
              <button
                type="button"
                onClick={() => applySavedFilter(filter)}
                className="px-2.5 py-1 hover:text-white"
                title={`Apply ${filter.name}`}
              >
                {filter.name}
              </button>
              <button
                type="button"
                onClick={event => deleteSavedFilter(filter.id, event)}
                className="mr-1 rounded-sm p-1 text-slate-500 hover:bg-slate-800 hover:text-red-300"
                aria-label={`Delete ${filter.name}`}
                title={`Delete ${filter.name}`}
              >
                <TrashIcon className="h-3.5 w-3.5" />
              </button>
            </div>
          ))}
        </div>
      )}

      {!search && !showBOMComponents && componentPartIds.size > 0 && (
        <div className="text-sm text-amber-300 bg-amber-500/10 border border-amber-500/30 px-3 py-2 rounded-sm tabular-nums">
          {componentPartIds.size} component part{componentPartIds.size !== 1 ? 's are' : ' is'} hidden under assembly rows.
          Check "Show BOM components as top-level parts" to see all parts.
        </div>
      )}

      {selectedParts.length > 0 && (
        <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3 rounded-sm border border-cyan-500/30 bg-cyan-500/10 px-3 py-2">
          <div className="text-sm text-cyan-100 tabular-nums">
            <span className="font-semibold">{selectedParts.length}</span> part{selectedParts.length !== 1 ? 's' : ''} selected
            {visibleSelectedCount !== selectedParts.length && (
              <span className="text-cyan-200/70"> · {visibleSelectedCount} visible in current filter</span>
            )}
          </div>
          <div className="flex flex-wrap gap-2">
            <button type="button" onClick={copySelectedParts} className="btn-secondary flex items-center gap-2 text-sm">
              <ClipboardDocumentListIcon className="h-4 w-4" />
              Copy List
            </button>
            <button type="button" onClick={exportSelectedParts} className="btn-secondary flex items-center gap-2 text-sm">
              <ArrowDownTrayIcon className="h-4 w-4" />
              Export CSV
            </button>
            <button type="button" onClick={handleDeleteSelectedParts} className="btn-danger flex items-center gap-2 text-sm">
              <TrashIcon className="h-4 w-4" />
              Delete Selected
            </button>
            <button type="button" onClick={() => setSelectedPartIds(new Set())} className="btn-secondary flex items-center gap-2 text-sm">
              <XMarkIcon className="h-4 w-4" />
              Clear Selection
            </button>
          </div>
        </div>
      )}

      {/* Table View */}
      {viewMode === 'table' && (
        <div className="card overflow-hidden p-0">
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-slate-700">
              <thead className="bg-slate-800/50">
                <tr>
                  <th className="px-4 py-3 w-10">
                    <input
                      type="checkbox"
                      checked={allVisibleSelected}
                      onChange={toggleAllVisible}
                      className="rounded border-slate-600 bg-slate-900 text-cyan-500"
                      aria-label="Select all visible parts"
                    />
                  </th>
                  <th className="px-4 py-3 w-10" />
                  <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Part #</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Name</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Customer</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Type</th>
                  <th className="px-4 py-3 text-center text-xs font-medium text-slate-400 uppercase">Rev</th>
                  <th className="px-4 py-3 text-right text-xs font-medium text-slate-400 uppercase">Cost</th>
                  <th className="px-4 py-3 text-center text-xs font-medium text-slate-400 uppercase">Status</th>
                  <th className="px-4 py-3 w-10" />
                </tr>
              </thead>
              <tbody className="bg-fd-panel divide-y divide-slate-700">
                {filteredParts.map(part => {
                  const bomItems = bomData[part.id] || [];
                  const isExpanded = expandedParts.has(part.id);
                  return (
                    <React.Fragment key={part.id}>
                      <tr
                        onClick={() => navigate(`/parts/${part.id}`)}
                        className="hover:bg-slate-800/50 cursor-pointer transition-colors"
                      >
                        <td className="px-4 py-3" onClick={event => event.stopPropagation()}>
                          <input
                            type="checkbox"
                            checked={selectedPartIds.has(part.id)}
                            onChange={event => toggleSelectedPart(part.id, event)}
                            className="rounded border-slate-600 bg-slate-900 text-cyan-500"
                            aria-label={`Select part ${part.part_number}`}
                          />
                        </td>
                        <td className="px-4 py-3">
                          {bomItems.length > 0 ? (
                            <button
                              type="button"
                              onClick={event => toggleExpanded(part.id, event)}
                              className="text-slate-500 hover:text-slate-200"
                              title={isExpanded ? 'Collapse BOM' : 'Expand BOM'}
                            >
                              {isExpanded ? (
                                <ChevronDownIcon className="h-4 w-4" />
                              ) : (
                                <ChevronRightIcon className="h-4 w-4" />
                              )}
                            </button>
                          ) : null}
                        </td>
                        <td className="px-4 py-3">
                          <span className="font-medium text-werco-navy-600">{part.part_number}</span>
                        </td>
                        <td className="px-4 py-3">
                          <div className="text-sm">{part.name}</div>
                          {part.customer_part_number && (
                            <div className="text-xs text-slate-500">Cust P/N: {part.customer_part_number}</div>
                          )}
                        </td>
                        <td className="px-4 py-3 text-sm text-slate-400">{part.customer_name || '-'}</td>
                        <td className="px-4 py-3">
                          <span className={`inline-flex px-2 py-0.5 rounded text-xs font-medium ${partTypeColors[part.part_type]}`}>
                            {part.part_type.replace('_', ' ')}
                          </span>
                        </td>
                        <td className="px-4 py-3 text-center text-sm font-medium tabular-nums">{part.revision}</td>
                        <td className="px-4 py-3 text-right text-sm tabular-nums">${Number(part.standard_cost || 0).toFixed(2)}</td>
                        <td className="px-4 py-3 text-center">
                          <div className="flex items-center justify-center gap-1.5">
                            <StatusBadge status={part.status} />
                            {part.is_critical && (
                              <span className="inline-flex px-1.5 py-0.5 rounded bg-red-500/20 text-red-400 text-[10px] font-semibold">
                                CRIT
                              </span>
                            )}
                          </div>
                        </td>
                        <td className="px-4 py-3 text-right">
                          <div className="flex items-center justify-end gap-2">
                            <button
                              type="button"
                              onClick={event => handleDeletePart(part, event)}
                              className="rounded-lg p-1.5 text-slate-500 hover:bg-red-500/10 hover:text-red-400"
                              title={`Delete ${part.part_number}`}
                              aria-label={`Delete ${part.part_number}`}
                            >
                              <TrashIcon className="h-4 w-4" />
                            </button>
                            <ChevronRightIcon className="h-4 w-4 text-slate-500" />
                          </div>
                        </td>
                      </tr>
                      {isExpanded && bomItems.map(item => {
                        const fullComponentPart = getPartById(item.component_part_id);
                        const componentPart = item.component_part || fullComponentPart;
                        const componentCustomer = fullComponentPart?.customer_name;
                        const componentCost = fullComponentPart?.standard_cost || 0;
                        return (
                          <tr
                            key={`${part.id}-${item.id}`}
                            onClick={() => componentPart && navigate(`/parts/${componentPart.id}`)}
                            className="bg-slate-900/30 hover:bg-slate-800/50 cursor-pointer transition-colors"
                          >
                            <td className="px-4 py-2" />
                            <td className="px-4 py-2" />
                            <td className="px-4 py-2 pl-8">
                              <div className="flex items-center gap-2">
                                <span className="text-slate-600">└</span>
                                <span className="text-sm font-medium text-werco-navy-400">
                                  {componentPart?.part_number || `Part #${item.component_part_id}`}
                                </span>
                                <span className="text-[10px] bg-slate-700 text-slate-300 px-1.5 py-0.5 rounded-sm tabular-nums">
                                  x{item.quantity}
                                </span>
                              </div>
                            </td>
                            <td className="px-4 py-2 text-sm text-slate-300">{componentPart?.name || '-'}</td>
                            <td className="px-4 py-2 text-sm text-slate-500">{componentCustomer || '-'}</td>
                            <td className="px-4 py-2">
                              {componentPart && (
                                <span className={`inline-flex px-2 py-0.5 rounded text-xs font-medium ${partTypeColors[componentPart.part_type]}`}>
                                  {componentPart.part_type.replace('_', ' ')}
                                </span>
                            )}
                          </td>
                          <td className="px-4 py-2 text-center text-sm tabular-nums">{componentPart?.revision || '-'}</td>
                            <td className="px-4 py-2 text-right text-sm tabular-nums">${Number(componentCost || 0).toFixed(2)}</td>
                            <td className="px-4 py-2 text-center text-xs text-slate-500">BOM item</td>
                            <td className="px-4 py-2 text-right">
                              {componentPart && <ChevronRightIcon className="h-4 w-4 text-slate-600" />}
                            </td>
                          </tr>
                        );
                      })}
                    </React.Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
          {filteredParts.length === 0 && (
            <div className="text-center py-12 text-slate-400">
              <p className="text-sm">No parts found matching your filters</p>
            </div>
          )}
        </div>
      )}

      {/* Grid View */}
      {viewMode === 'grid' && (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
          {filteredParts.map(part => (
            <div
              key={part.id}
              role="button"
              tabIndex={0}
              onClick={() => navigate(`/parts/${part.id}`)}
              onKeyDown={(e) => {
                if (e.target !== e.currentTarget) return;
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault();
                  navigate(`/parts/${part.id}`);
                }
              }}
              className="card cursor-pointer hover:shadow-md hover:border-werco-navy-200 transition-all border border-slate-700 p-4"
            >
              <div className="flex items-start justify-between mb-2">
                <div className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    checked={selectedPartIds.has(part.id)}
                    onClick={event => event.stopPropagation()}
                    onChange={event => toggleSelectedPart(part.id, event)}
                    className="rounded border-slate-600 bg-slate-900 text-cyan-500"
                    aria-label={`Select part ${part.part_number}`}
                  />
                  <span className="font-semibold text-werco-navy-600 text-sm">{part.part_number}</span>
                </div>
                <div className="flex items-center gap-2">
                  <StatusBadge status={part.status} />
                  <button
                    type="button"
                    onClick={event => handleDeletePart(part, event)}
                    className="rounded-lg p-1.5 text-slate-500 hover:bg-red-500/10 hover:text-red-400"
                    title={`Delete ${part.part_number}`}
                    aria-label={`Delete ${part.part_number}`}
                  >
                    <TrashIcon className="h-4 w-4" />
                  </button>
                </div>
              </div>
              <h3 className="text-sm font-medium text-white mb-1 line-clamp-2">{part.name}</h3>
              {part.customer_name && (
                <p className="text-xs text-slate-400 mb-2">{part.customer_name}</p>
              )}
              <div className="flex items-center justify-between mt-auto pt-2 border-t border-slate-700/30">
                <span className={`inline-flex px-2 py-0.5 rounded text-[10px] font-medium ${partTypeColors[part.part_type]}`}>
                  {part.part_type.replace('_', ' ')}
                </span>
                <span className="text-xs text-slate-400">Rev {part.revision}</span>
              </div>
              {part.is_critical && (
                <div className="mt-2">
                  <span className="inline-flex px-1.5 py-0.5 rounded bg-red-500/20 text-red-400 text-[10px] font-semibold">
                    Critical
                  </span>
                </div>
              )}
            </div>
          ))}
          {filteredParts.length === 0 && (
            <div className="col-span-full text-center py-12 text-slate-400">
              <p className="text-sm">No parts found matching your filters</p>
            </div>
          )}
        </div>
      )}

      {/* Create Part Modal */}
      <Modal open={showCreateModal} onClose={closeCreateModal} size="lg">
        <h3 className="text-lg font-semibold mb-4">New Part</h3>
        <form onSubmit={handleCreate} className="space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <FormField label="Part Number" required>
                  {field => (
                    <input
                      {...field}
                      type="text"
                      value={createForm.part_number}
                      onChange={e => setCreateForm(p => ({ ...p, part_number: e.target.value }))}
                      className="input"
                      required
                      autoFocus
                    />
                  )}
                </FormField>
                <FormField label="Revision" required>
                  {field => (
                    <input
                      {...field}
                      type="text"
                      value={createForm.revision}
                      onChange={e => setCreateForm(p => ({ ...p, revision: e.target.value }))}
                      className="input"
                      required
                    />
                  )}
                </FormField>
              </div>

              <FormField label="Name" required>
                {field => (
                  <input
                    {...field}
                    type="text"
                    value={createForm.name}
                    onChange={e => setCreateForm(p => ({ ...p, name: e.target.value }))}
                    className="input"
                    required
                  />
                )}
              </FormField>

              <div className="grid grid-cols-2 gap-4">
                <FormField label="Type">
                  {field => (
                    <select
                      {...field}
                      value={createForm.part_type}
                      onChange={e => setCreateForm(p => ({ ...p, part_type: e.target.value as PartType }))}
                      className="input"
                    >
                      {ENGINEERING_PART_TYPE_OPTIONS.map(option => (
                        <option key={option.value} value={option.value}>{option.label}</option>
                      ))}
                    </select>
                  )}
                </FormField>
                <FormField label="Standard Cost ($)">
                  {field => (
                    <input
                      {...field}
                      type="number"
                      min="0"
                      step="0.01"
                      value={createForm.standard_cost}
                      onChange={e => setCreateForm(p => ({ ...p, standard_cost: parseFloat(e.target.value) || 0 }))}
                      className="input"
                    />
                  )}
                </FormField>
              </div>

              <FormField label="Description">
                {field => (
                  <textarea
                    {...field}
                    value={createForm.description}
                    onChange={e => setCreateForm(p => ({ ...p, description: e.target.value }))}
                    className="input"
                    rows={2}
                  />
                )}
              </FormField>

              <div className="grid grid-cols-2 gap-4">
                <FormField label="Customer" className="relative">
                  {field => (
                    <>
                      <div className="relative">
                        <input
                          {...field}
                          type="text"
                          value={createForm.customer_name}
                          onChange={e => {
                            setCreateForm(p => ({ ...p, customer_name: e.target.value }));
                            setShowCustomerDropdown(true);
                          }}
                          onFocus={() => setShowCustomerDropdown(true)}
                          onBlur={() => setTimeout(() => setShowCustomerDropdown(false), 200)}
                          onKeyDown={handleCustomerKeyDown}
                          className="input pr-8"
                          placeholder="Select or type customer"
                          role="combobox"
                          aria-expanded={showCustomerDropdown}
                          aria-controls="new-part-customer-results"
                          aria-autocomplete="list"
                        />
                        <ChevronDownIcon
                          className="h-5 w-5 absolute right-2 top-1/2 -translate-y-1/2 text-slate-500 cursor-pointer"
                          onClick={() => setShowCustomerDropdown(!showCustomerDropdown)}
                        />
                      </div>
                      {showCustomerDropdown && (
                        <div
                          id="new-part-customer-results"
                          className="absolute z-20 w-full mt-1 bg-fd-panel border border-slate-700 rounded-md shadow-lg max-h-48 overflow-y-auto"
                          role="listbox"
                        >
                          {filteredCustomers.length > 0 ? (
                            <>
                              {filteredCustomers.map((customer, index) => (
                                <button
                                  key={customer.id}
                                  type="button"
                                  className={`w-full text-left px-3 py-2 text-sm ${
                                    highlightedCustomerIndex === index ? 'bg-cyan-500/10 text-cyan-100' : 'hover:bg-slate-800'
                                  }`}
                                  onMouseEnter={() => setHighlightedCustomerIndex(index)}
                                  onMouseDown={event => {
                                    event.preventDefault();
                                    selectCustomer(customer.name);
                                  }}
                                  role="option"
                                  aria-selected={createForm.customer_name === customer.name}
                                >
                                  {customer.name}
                                </button>
                              ))}
                              {matchingCustomers.length > filteredCustomers.length && (
                                <div className="px-3 py-2 text-xs text-slate-400 border-t border-slate-700/30">
                                  Showing {filteredCustomers.length} of {matchingCustomers.length}. Keep typing to narrow results.
                                </div>
                              )}
                            </>
                          ) : (
                            <div className="px-3 py-2 text-sm text-slate-400">
                              {customerOptions.length === 0 ? 'No customers loaded' : 'No matching customers'}
                            </div>
                          )}
                        </div>
                      )}
                    </>
                  )}
                </FormField>
                <FormField label="Drawing #">
                  {field => (
                    <input
                      {...field}
                      type="text"
                      value={createForm.drawing_number}
                      onChange={e => setCreateForm(p => ({ ...p, drawing_number: e.target.value }))}
                      className="input"
                      placeholder="Optional"
                    />
                  )}
                </FormField>
              </div>

              <FormField label="Drawing PDF">
                {field => (
                  <>
                    <input
                      {...field}
                      key={createDrawingInputKey}
                      type="file"
                      accept=".pdf,application/pdf"
                      onChange={e => setCreateDrawingPdf(e.target.files?.[0] || null)}
                      className="block w-full text-sm text-slate-300 file:mr-3 file:rounded file:border-0 file:bg-slate-700 file:px-3 file:py-2 file:text-sm file:font-semibold file:text-slate-100 hover:file:bg-slate-600"
                    />
                    {createDrawingPdf && (
                      <p className="mt-1 text-xs text-slate-400">
                        {createDrawingPdf.name}
                      </p>
                    )}
                  </>
                )}
              </FormField>

              <FormField label="Flags">
                <div className="flex gap-4">
                  <label className="flex items-center gap-2">
                    <input
                      type="checkbox"
                      checked={createForm.is_critical}
                      onChange={e => setCreateForm(p => ({ ...p, is_critical: e.target.checked }))}
                      className="rounded border-slate-600 text-werco-navy-600"
                    />
                    <span className="text-sm">Critical Characteristic</span>
                  </label>
                  <label className="flex items-center gap-2">
                    <input
                      type="checkbox"
                      checked={createForm.requires_inspection}
                      onChange={e => setCreateForm(p => ({ ...p, requires_inspection: e.target.checked }))}
                      className="rounded border-slate-600 text-werco-navy-600"
                    />
                    <span className="text-sm">Requires Inspection</span>
                  </label>
                </div>
              </FormField>

              <div className="flex justify-end gap-3 pt-2">
                <button type="button" onClick={closeCreateModal} className="btn-secondary" disabled={creatingPart}>
                  Cancel
                </button>
                <button type="submit" className="btn-primary" disabled={creatingPart}>
                  {creatingPart ? 'Creating...' : 'Create Part'}
                </button>
              </div>
            </form>
      </Modal>

      {/* Import Wizard */}
      {showImport && (
        <BOMImportWizard
          onComplete={async () => {
            await loadParts();
            setShowImport(false);
          }}
          onClose={() => setShowImport(false)}
        />
      )}
    </div>
  );
}
