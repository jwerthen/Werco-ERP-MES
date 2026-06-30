import React, { useEffect, useMemo, useState } from 'react';
import api from '../services/api';
import { Modal } from '../components/ui/Modal';
import { LoadingButton } from '../components/ui/LoadingButton';
import { EmptyState, ErrorState, FormField, useToast } from '../components/ui';
import useUnsavedChanges from '../hooks/useUnsavedChanges';
import { Part, PartType } from '../types';
import { isMaterialSupplyPartType } from '../utils/catalogGroups';
import { useNavigate } from 'react-router-dom';
import { 
  PlusIcon, 
  ChevronRightIcon, 
  ChevronDownIcon,
  DocumentDuplicateIcon,
  TrashIcon,
  XMarkIcon
} from '@heroicons/react/24/outline';

type LineType = 'component' | 'hardware' | 'consumable' | 'reference';

interface BOMItem {
  id: number;
  bom_id: number;
  component_part_id: number;
  item_number: number;
  quantity: number;
  item_type: 'make' | 'buy' | 'phantom';
  line_type: LineType;
  unit_of_measure: string;
  find_number?: string;
  reference_designator?: string;
  notes?: string;
  torque_spec?: string;
  installation_notes?: string;
  scrap_factor: number;
  is_optional: boolean;
  is_alternate: boolean;
  component_part?: {
    id: number;
    part_number: string;
    name: string;
    revision: string;
    part_type: string;
    has_bom: boolean;
  };
  children?: BOMItem[];
  level?: number;
  extended_quantity?: number;
}

interface BOM {
  id: number;
  part_id: number;
  revision: string;
  description?: string;
  bom_type: string;
  status: string;
  is_active: boolean;
  part?: {
    id: number;
    part_number: string;
    name: string;
    revision: string;
    part_type: string;
  };
  items: BOMItem[];
}

interface ImportAssembly {
  part_number?: string;
  name?: string;
  revision?: string;
  description?: string;
  drawing_number?: string;
  part_type?: string;
}

interface ImportItem {
  line_number?: number;
  part_number?: string;
  description?: string;
  quantity?: number;
  unit_of_measure?: string;
  item_type?: string;
  line_type?: LineType;
  reference_designator?: string;
  find_number?: string;
  notes?: string;
}

interface ImportPreview {
  document_type: 'bom' | 'part';
  assembly: ImportAssembly;
  items: ImportItem[];
  extraction_confidence?: string;
  warnings?: string[];
  raw_columns?: string[];
  raw_rows?: string[][];
  suggested_mapping?: Record<string, number | null>;
  source_format?: string;
}

const lineTypeColors: Record<string, string> = {
  component: 'bg-blue-500/20 text-blue-300',
  hardware: 'bg-amber-500/20 text-amber-300',
  consumable: 'bg-orange-500/20 text-orange-300',
  reference: 'bg-slate-800 text-slate-400',
};

const lineTypeLabels: Record<string, string> = {
  component: 'Component',
  hardware: 'Hardware',
  consumable: 'Consumable',
  reference: 'Reference',
};

const partTypeLabels: Record<string, string> = {
  manufactured: 'Manufactured',
  assembly: 'Assembly',
  purchased: 'Purchased',
  raw_material: 'Raw Material',
  hardware: 'Hardware',
  consumable: 'Consumable',
};

const partTypeBadge: Record<string, string> = {
  manufactured: 'bg-blue-500/20 text-blue-300',
  assembly: 'bg-indigo-500/20 text-indigo-800',
  purchased: 'bg-emerald-500/20 text-emerald-300',
  raw_material: 'bg-blue-500/20 text-blue-300',
  hardware: 'bg-amber-500/20 text-amber-300',
  consumable: 'bg-orange-500/20 text-orange-300',
};

export default function BOMPage() {
  const navigate = useNavigate();
  const { showToast } = useToast();
  const [boms, setBoms] = useState<BOM[]>([]);
  const [parts, setParts] = useState<Part[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [explodeError, setExplodeError] = useState(false);
  const [selectedBOM, setSelectedBOM] = useState<BOM | null>(null);
  const [explodedView, setExplodedView] = useState<BOMItem[]>([]);
  const [expandedItems, setExpandedItems] = useState<Set<number>>(new Set());
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [creatingBOM, setCreatingBOM] = useState(false);
  const [creatingPart, setCreatingPart] = useState(false);
  const [showImportModal, setShowImportModal] = useState(false);
  const [showPreviewModal, setShowPreviewModal] = useState(false);
  const [importFile, setImportFile] = useState<File | null>(null);
  const [importCreateMissingParts, setImportCreateMissingParts] = useState(true);
  const [importLoading, setImportLoading] = useState(false);
  const [importWarnings, setImportWarnings] = useState<string[]>([]);
  const [importPreview, setImportPreview] = useState<ImportPreview | null>(null);
  const [importColumnMap, setImportColumnMap] = useState<Record<string, number | null>>({});
  const [importDerivedItems, setImportDerivedItems] = useState<ImportItem[]>([]);
  const [showAddItemModal, setShowAddItemModal] = useState(false);
  const [showNewPartModal, setShowNewPartModal] = useState(false);
  const [viewMode, setViewMode] = useState<'single' | 'exploded'>('single');
  const [partSearch, setPartSearch] = useState('');
  const [partTypeFilter, setPartTypeFilter] = useState('all');

  const EMPTY_BOM = { part_id: 0, revision: 'A', description: '', bom_type: 'standard' };
  const EMPTY_PART = {
    part_number: '',
    name: '',
    part_type: 'manufactured' as PartType,
    revision: 'A',
    description: ''
  };

  const [newBOM, setNewBOM] = useState(EMPTY_BOM);
  const [newItem, setNewItem] = useState({
    component_part_id: 0,
    item_number: 10,
    quantity: 1,
    item_type: 'make' as 'make' | 'buy' | 'phantom',
    line_type: 'component' as LineType,
    find_number: '',
    scrap_factor: 0,
    is_optional: false,
    notes: '',
    torque_spec: '',
    installation_notes: ''
  });
  const [newPart, setNewPart] = useState(EMPTY_PART);

  // Snapshots of the values each modal opened with, for unsaved-changes detection.
  const [initialBOM, setInitialBOM] = useState(EMPTY_BOM);
  const [initialPart, setInitialPart] = useState(EMPTY_PART);

  const isCreateBOMDirty = useMemo(
    () => showCreateModal && JSON.stringify(newBOM) !== JSON.stringify(initialBOM),
    [showCreateModal, newBOM, initialBOM]
  );
  const { confirmDiscard: confirmDiscardBOM } = useUnsavedChanges(isCreateBOMDirty);

  const isNewPartDirty = useMemo(
    () => showNewPartModal && JSON.stringify(newPart) !== JSON.stringify(initialPart),
    [showNewPartModal, newPart, initialPart]
  );
  const { confirmDiscard: confirmDiscardPart } = useUnsavedChanges(isNewPartDirty);

  const openCreateBOMModal = () => {
    setNewBOM(EMPTY_BOM);
    setInitialBOM(EMPTY_BOM);
    setShowCreateModal(true);
  };

  const requestCloseCreateBOM = () => {
    if (!confirmDiscardBOM()) return;
    setShowCreateModal(false);
    setNewBOM(EMPTY_BOM);
  };

  const openNewPartModal = () => {
    setNewPart(EMPTY_PART);
    setInitialPart(EMPTY_PART);
    setShowNewPartModal(true);
  };

  const requestCloseNewPart = () => {
    if (!confirmDiscardPart()) return;
    setShowNewPartModal(false);
    setNewPart(EMPTY_PART);
  };

  useEffect(() => {
    loadData();
  }, []);

  useEffect(() => {
    if (selectedBOM && viewMode === 'exploded') {
      loadExplodedBOM(selectedBOM.id);
    }
  }, [selectedBOM, viewMode]);

  const loadData = async () => {
    setLoading(true);
    setLoadError(false);
    try {
      const requestedBOMId = Number(new URLSearchParams(window.location.search).get('id') || 0);

      // Load BOMs and parts separately so one failure doesn't block the other
      const [bomsResult, partsResult] = await Promise.allSettled([
        api.getBOMs({ active_only: true }),
        api.getParts({ active_only: true, item_group: 'all' })
      ]);

      if (bomsResult.status === 'fulfilled') {
        const loadedBOMs = bomsResult.value;
        setBoms(loadedBOMs);

        if (requestedBOMId) {
          const matchingBOM = loadedBOMs.find((bom: BOM) => bom.id === requestedBOMId);
          if (matchingBOM) {
            setSelectedBOM(matchingBOM);
            setViewMode('single');
          } else {
            try {
              const fetchedBOM = await api.getBOM(requestedBOMId);
              setSelectedBOM(fetchedBOM);
              setBoms([...loadedBOMs.filter((bom: BOM) => bom.id !== fetchedBOM.id), fetchedBOM]);
              setViewMode('single');
            } catch (err) {
              console.error('Failed to load requested BOM:', err);
            }
          }
        }
      } else {
        console.error('Failed to load BOMs:', bomsResult.reason);
        setLoadError(true);
      }

      if (partsResult.status === 'fulfilled') {
        setParts(partsResult.value);
      } else {
        console.error('Failed to load parts:', partsResult.reason);
      }
    } catch (err) {
      console.error('Failed to load data:', err);
      setLoadError(true);
    } finally {
      setLoading(false);
    }
  };

  const partTypeOptions = [
    { id: 'all', label: 'All' },
    { id: 'manufactured', label: 'Manufactured' },
    { id: 'assembly', label: 'Assembly' },
    { id: 'purchased', label: 'Purchased' },
    { id: 'raw_material', label: 'Raw Material' },
    { id: 'hardware', label: 'Hardware' },
    { id: 'consumable', label: 'Consumable' },
  ];

  const filteredParts = useMemo(() => {
    const search = partSearch.toLowerCase();
    return parts
      .filter(p => p.id !== selectedBOM?.part_id)
      .filter(p => (partTypeFilter === 'all' ? true : p.part_type === partTypeFilter))
      .filter(p => {
        if (!search) return true;
        return (
          p.part_number.toLowerCase().includes(search) ||
          p.name.toLowerCase().includes(search) ||
          p.description?.toLowerCase().includes(search)
        );
      })
      .sort((a, b) => a.part_number.localeCompare(b.part_number));
  }, [partSearch, partTypeFilter, parts, selectedBOM?.part_id]);

  const handleSelectPart = (partId: number) => {
    const selectedPart = parts.find(p => p.id === partId);
    let lineType = newItem.line_type;
    let itemType = newItem.item_type;
    if (selectedPart) {
      if (selectedPart.part_type === 'hardware') {
        lineType = 'hardware';
        itemType = 'buy';
      } else if (selectedPart.part_type === 'consumable') {
        lineType = 'consumable';
        itemType = 'buy';
      } else if (selectedPart.part_type === 'purchased' || selectedPart.part_type === 'raw_material') {
        itemType = 'buy';
      } else if (selectedPart.part_type === 'manufactured' || selectedPart.part_type === 'assembly') {
        itemType = 'make';
      }
    }
    setNewItem({ ...newItem, component_part_id: partId, line_type: lineType, item_type: itemType });
  };

  const loadExplodedBOM = async (bomId: number) => {
    setExplodeError(false);
    try {
      const response = await api.explodeBOM(bomId);
      setExplodedView(response.items);
    } catch (err) {
      console.error('Failed to explode BOM:', err);
      setExplodeError(true);
    }
  };

  const handleCreateBOM = async (e: React.FormEvent) => {
    e.preventDefault();
    if (creatingBOM) return;
    setCreatingBOM(true);
    try {
      const created = await api.createBOM(newBOM);
      setBoms([...boms, created]);
      setSelectedBOM(created);
      setShowCreateModal(false);
      setNewBOM({ part_id: 0, revision: 'A', description: '', bom_type: 'standard' });
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to create BOM');
    } finally {
      setCreatingBOM(false);
    }
  };

  const handleImport = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!importFile) {
      showToast('error', 'Please select a file to import.');
      return;
    }
    setImportLoading(true);
    setImportWarnings([]);
    try {
      const formData = new FormData();
      formData.append('file', importFile);
      const preview = await api.previewBOMImport(formData);
      setImportPreview(preview);
      setImportWarnings(preview.warnings || []);
      if (preview.raw_columns && preview.raw_columns.length > 0 && preview.raw_rows) {
        const mapping = preview.suggested_mapping || {
          line_number: null,
          part_number: null,
          description: null,
          quantity: null,
          unit_of_measure: null,
          item_type: null,
          line_type: null,
        };
        setImportColumnMap(mapping);
        setImportDerivedItems(buildItemsFromRaw(preview.raw_columns, preview.raw_rows, mapping));
      } else {
        setImportDerivedItems(preview.items || []);
      }
      setShowImportModal(false);
      setShowPreviewModal(true);
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to import BOM');
    } finally {
      setImportLoading(false);
    }
  };

  const handleCommitImport = async () => {
    if (!importPreview) return;
    setImportLoading(true);
    try {
      const commitItems = (importPreview.raw_columns && importPreview.raw_columns.length > 0)
        ? importDerivedItems
        : importPreview.items;
      const result = await api.commitBOMImport({
        document_type: importPreview.document_type,
        assembly: importPreview.assembly,
        items: commitItems,
        create_missing_parts: importCreateMissingParts
      });
      await loadData();
      if (result.bom_id) {
        const createdBOM = await api.getBOM(result.bom_id);
        setSelectedBOM(createdBOM);
        setViewMode('single');
      } else {
        showToast('success', `Part created: ${result.assembly_part_number}`);
      }
      if (result.warnings?.length) {
        showToast('info', `Import completed with warnings:\n- ${result.warnings.join('\n- ')}`);
      }
      setShowPreviewModal(false);
      setImportPreview(null);
      setImportFile(null);
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to create from preview');
    } finally {
      setImportLoading(false);
    }
  };

  const updatePreviewAssembly = (field: keyof ImportAssembly, value: string) => {
    if (!importPreview) return;
    setImportPreview({
      ...importPreview,
      assembly: {
        ...importPreview.assembly,
        [field]: value
      }
    });
  };

  const updatePreviewItem = (index: number, field: keyof ImportItem, value: string | number) => {
    if (!importPreview) return;
    const items = [...importPreview.items];
    items[index] = { ...items[index], [field]: value };
    setImportPreview({ ...importPreview, items });
  };

  const buildItemsFromRaw = (rawColumns: string[], rawRows: string[][], mapping: Record<string, number | null>) => {
    const items: ImportItem[] = [];
    let nextLine = 10;
    rawRows.forEach((row) => {
      const hasData = row.some((cell) => (cell || '').toString().trim() !== '');
      if (!hasData) return;

      const getVal = (field: string) => {
        const idx = mapping[field];
        if (idx === null || idx === undefined || idx >= row.length) return '';
        return (row[idx] || '').toString().trim();
      };

      const lineVal = getVal('line_number');
      const lineNumber = lineVal ? parseInt(lineVal) : nextLine;
      nextLine = (isNaN(lineNumber) ? nextLine : lineNumber) + 10;

      const quantityVal = getVal('quantity');
      const quantity = quantityVal ? parseFloat(quantityVal) : 1;

      items.push({
        line_number: isNaN(lineNumber) ? nextLine : lineNumber,
        part_number: getVal('part_number') || undefined,
        description: getVal('description') || undefined,
        quantity: isNaN(quantity) ? 1 : quantity,
        unit_of_measure: getVal('unit_of_measure') || undefined,
        item_type: getVal('item_type') || undefined,
        line_type: (getVal('line_type') as LineType) || undefined,
      });
    });
    return items;
  };

  const handleAddItem = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedBOM) return;
    if (newItem.component_part_id <= 0) {
      showToast('error', 'Select a component part before adding.');
      return;
    }

    try {
      await api.addBOMItem(selectedBOM.id, newItem);
      // Reload the BOM
      const updated = await api.getBOM(selectedBOM.id);
      setSelectedBOM(updated);
      setBoms(boms.map(b => b.id === updated.id ? updated : b));
      setShowAddItemModal(false);
      setPartSearch('');
      setNewItem({
        component_part_id: 0,
        item_number: (selectedBOM.items.length + 1) * 10,
        quantity: 1,
        item_type: 'make',
        line_type: 'component',
        find_number: '',
        scrap_factor: 0,
        is_optional: false,
        notes: '',
        torque_spec: '',
        installation_notes: ''
      });
    } catch (err: any) {
      console.error('Failed to add BOM item:', err.response?.data || err);
      showToast('error', err.response?.data?.detail || 'Failed to add item');
    }
  };

  const handleCreateNewPart = async (e: React.FormEvent) => {
    e.preventDefault();
    if (creatingPart) return;
    setCreatingPart(true);
    try {
      const createdPart = isMaterialSupplyPartType(newPart.part_type)
        ? await api.createMaterial(newPart)
        : await api.createPart(newPart);
      // Add to parts list and select it
      setParts([...parts, createdPart]);
      handleSelectPart(createdPart.id);
      setShowNewPartModal(false);
      setNewPart({
        part_number: '',
        name: '',
        part_type: 'manufactured',
        revision: 'A',
        description: ''
      });
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to create part');
    } finally {
      setCreatingPart(false);
    }
  };

  const handleDeleteItem = async (itemId: number) => {
    if (!window.confirm('Delete this BOM item?')) return;
    
    try {
      await api.deleteBOMItem(itemId);
      if (selectedBOM) {
        const updated = await api.getBOM(selectedBOM.id);
        setSelectedBOM(updated);
        setBoms(boms.map(b => b.id === updated.id ? updated : b));
      }
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to delete item');
    }
  };

  const handleDeleteBOM = async (bomId: number) => {
    if (!window.confirm('Delete this BOM? This action cannot be undone.')) return;
    
    try {
      await api.deleteBOM(bomId);
      setBoms(boms.filter(b => b.id !== bomId));
      if (selectedBOM?.id === bomId) {
        setSelectedBOM(null);
      }
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to delete BOM');
    }
  };

  const handleReleaseBOM = async () => {
    if (!selectedBOM) return;
    try {
      await api.releaseBOM(selectedBOM.id);
      const updated = await api.getBOM(selectedBOM.id);
      setSelectedBOM(updated);
      setBoms(boms.map(b => b.id === updated.id ? updated : b));
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to release BOM');
    }
  };

  const handleUnreleaseBOM = async () => {
    if (!selectedBOM) return;
    if (!window.confirm('Unrelease this BOM? It will return to draft status and can be edited.')) return;
    try {
      await api.unreleaseBOM(selectedBOM.id);
      const updated = await api.getBOM(selectedBOM.id);
      setSelectedBOM(updated);
      setBoms(boms.map(b => b.id === updated.id ? updated : b));
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to unrelease BOM');
    }
  };

  const toggleExpanded = (itemId: number) => {
    const newExpanded = new Set(expandedItems);
    if (newExpanded.has(itemId)) {
      newExpanded.delete(itemId);
    } else {
      newExpanded.add(itemId);
    }
    setExpandedItems(newExpanded);
  };

  const renderExplodedItem = (item: BOMItem, depth: number = 0): React.ReactNode => {
    const hasChildren = item.children && item.children.length > 0;
    const isExpanded = expandedItems.has(item.id);

    return (
      <React.Fragment key={`${item.id}-${depth}`}>
        <tr className="hover:bg-slate-800/50">
          <td className="px-4 py-3" style={{ paddingLeft: `${16 + depth * 24}px` }}>
            <div className="flex items-center">
              {hasChildren ? (
                <button onClick={() => toggleExpanded(item.id)} className="mr-2">
                  {isExpanded ? (
                    <ChevronDownIcon className="h-4 w-4 text-slate-400" />
                  ) : (
                    <ChevronRightIcon className="h-4 w-4 text-slate-400" />
                  )}
                </button>
              ) : (
                <span className="w-6" />
              )}
              <span className="text-sm text-slate-400">L{(item.level || 0)}</span>
            </div>
          </td>
          <td className="px-4 py-3 font-medium">{item.find_number || item.item_number}</td>
          <td className="px-4 py-3" aria-label={`Component ${item.component_part?.part_number || ''} ${item.component_part?.name || ''}`.trim()}>
            <div>
              <div className="font-medium text-werco-primary">{item.component_part?.part_number}</div>
              <div className="text-sm text-slate-400">{item.component_part?.name}</div>
            </div>
          </td>
          <td className="px-4 py-3">
            <span className={`inline-flex px-2 py-1 rounded text-xs font-medium ${lineTypeColors[item.line_type || 'component']}`}>
              {lineTypeLabels[item.line_type || 'component']}
            </span>
          </td>
          <td className="px-4 py-3 text-right">{item.quantity}</td>
          <td className="px-4 py-3 text-right">{item.extended_quantity?.toFixed(3) || item.quantity}</td>
          <td className="px-4 py-3 text-center">{item.unit_of_measure}</td>
        </tr>
        {hasChildren && isExpanded && item.children!.map(child => renderExplodedItem(child, depth + 1))}
      </React.Fragment>
    );
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-werco-primary"></div>
      </div>
    );
  }

  if (loadError) {
    return (
      <div className="space-y-6">
        <h1 className="text-2xl font-bold text-white">Bill of Materials</h1>
        <ErrorState
          message="Could not load bills of materials."
          onRetry={loadData}
        />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <h1 className="text-2xl font-bold text-white">Bill of Materials</h1>
        <div className="flex gap-2">
          <button onClick={() => setShowImportModal(true)} className="btn-secondary flex items-center">
            <DocumentDuplicateIcon className="h-5 w-5 mr-2" />
            Import BOM/Drawing
          </button>
          <button onClick={openCreateBOMModal} className="btn-primary flex items-center">
            <PlusIcon className="h-5 w-5 mr-2" />
            Create BOM
          </button>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6" data-tour="eng-bom">
        {/* BOM List */}
        <div className="card lg:col-span-1">
          <h2 className="text-lg font-semibold mb-4">BOMs</h2>
          <div className="space-y-2 max-h-96 overflow-y-auto">
            {boms.map(bom => (
              <button
                type="button"
                key={bom.id}
                onClick={() => { setSelectedBOM(bom); setViewMode('single'); }}
                className={`w-full text-left p-3 rounded-lg cursor-pointer border transition-colors ${
                  selectedBOM?.id === bom.id
                    ? 'border-werco-primary bg-blue-500/10'
                    : 'border-slate-700 hover:border-slate-600'
                }`}
              >
                <div className="flex justify-between items-start">
                  <div>
                    <div className="font-medium">{bom.part?.part_number}</div>
                    <div className="text-sm text-slate-400">{bom.part?.name}</div>
                  </div>
                  <span className={`text-xs px-2 py-1 rounded ${
                    bom.status === 'released' ? 'bg-green-500/20 text-green-300' :
                    bom.status === 'draft' ? 'bg-slate-800 text-slate-100' :
                    'bg-yellow-500/20 text-yellow-300'
                  }`}>
                    {bom.status}
                  </span>
                </div>
                <div className="text-xs text-slate-500 mt-1">
                  Rev {bom.revision} | {bom.items.length} items
                </div>
              </button>
            ))}
            {boms.length === 0 && (
              <EmptyState
                title="No BOMs created yet"
                description="Create a bill of materials to define an assembly's components."
                action={{ label: 'Create your first BOM', onClick: openCreateBOMModal }}
              />
            )}
          </div>
        </div>

        {/* BOM Detail */}
        <div className="card lg:col-span-2">
          {selectedBOM ? (
            <>
              <div className="flex justify-between items-start mb-4">
                <div>
                  <h2 className="text-lg font-semibold">{selectedBOM.part?.part_number}</h2>
                  <p className="text-slate-400">{selectedBOM.part?.name}</p>
                  <p className="text-sm text-slate-500">Revision {selectedBOM.revision}</p>
                </div>
                <div className="flex gap-2">
                  <div className="flex rounded-lg border border-slate-600 overflow-hidden">
                    <button
                      onClick={() => setViewMode('single')}
                      className={`px-3 py-1 text-sm ${viewMode === 'single' ? 'bg-werco-primary text-white' : 'bg-fd-panel'}`}
                    >
                      Single Level
                    </button>
                    <button
                      onClick={() => setViewMode('exploded')}
                      className={`px-3 py-1 text-sm ${viewMode === 'exploded' ? 'bg-werco-primary text-white' : 'bg-fd-panel'}`}
                    >
                      Multi-Level
                    </button>
                  </div>
                  {selectedBOM.status === 'draft' && (
                    <>
                      <button onClick={() => setShowAddItemModal(true)} className="btn-secondary flex items-center">
                        <PlusIcon className="h-4 w-4 mr-1" />
                        Add Item
                      </button>
                      <button onClick={handleReleaseBOM} className="btn-success">
                        Release
                      </button>
                      <button 
                        onClick={() => handleDeleteBOM(selectedBOM.id)} 
                        className="btn-danger flex items-center"
                      >
                        <TrashIcon className="h-4 w-4 mr-1" />
                        Delete
                      </button>
                    </>
                  )}
                  {selectedBOM.status === 'released' && (
                    <button onClick={handleUnreleaseBOM} className="btn-warning">
                      Unrelease
                    </button>
                  )}
                </div>
              </div>

              {/* BOM Items Table */}
              {viewMode === 'exploded' && explodeError ? (
                <ErrorState
                  message="Could not load the multi-level explosion for this BOM."
                  onRetry={() => loadExplodedBOM(selectedBOM.id)}
                />
              ) : (
              <div className="overflow-x-auto">
                <table className="min-w-full divide-y divide-slate-700">
                  <thead className="bg-slate-800/50">
                    <tr>
                      {viewMode === 'exploded' && <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Level</th>}
                      <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Item #</th>
                      <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Part</th>
                      <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Category</th>
                      <th className="px-4 py-3 text-right text-xs font-medium text-slate-400 uppercase">Qty</th>
                      {viewMode === 'exploded' && <th className="px-4 py-3 text-right text-xs font-medium text-slate-400 uppercase">Ext Qty</th>}
                      <th className="px-4 py-3 text-center text-xs font-medium text-slate-400 uppercase">UOM</th>
                      {viewMode === 'single' && selectedBOM.status === 'draft' && (
                        <th className="px-4 py-3 text-center text-xs font-medium text-slate-400 uppercase">Actions</th>
                      )}
                      {viewMode === 'single' && (
                        <th className="px-4 py-3 text-right text-xs font-medium text-slate-400 uppercase">Routing</th>
                      )}
                    </tr>
                  </thead>
                  <tbody className="bg-fd-panel divide-y divide-slate-700">
                    {viewMode === 'single' ? (
                      selectedBOM.items.map(item => (
                        <tr key={item.id} className={`hover:bg-slate-800/50 ${item.line_type === 'hardware' ? 'bg-amber-500/10/50' : ''}`}>
                          <td className="px-4 py-3 font-medium">{item.find_number || item.item_number}</td>
                          <td className="px-4 py-3">
                            <div className="flex items-center">
                              <div>
                                <div className="font-medium text-werco-primary">{item.component_part?.part_number}</div>
                                <div className="text-sm text-slate-400">{item.component_part?.name}</div>
                                {item.torque_spec && (
                                  <div className="text-xs text-amber-600">Torque: {item.torque_spec}</div>
                                )}
                                {item.installation_notes && (
                                  <div className="text-xs text-slate-500 italic">{item.installation_notes}</div>
                                )}
                              </div>
                              {item.component_part?.has_bom && (
                                <DocumentDuplicateIcon className="h-4 w-4 ml-2 text-blue-500" title="Has BOM" />
                              )}
                            </div>
                          </td>
                          <td className="px-4 py-3">
                            <span className={`inline-flex px-2 py-0.5 rounded text-xs font-medium ${lineTypeColors[item.line_type || 'component']}`}>
                              {lineTypeLabels[item.line_type || 'component']}
                            </span>
                          </td>
                          <td className="px-4 py-3 text-right">{item.quantity}</td>
                          <td className="px-4 py-3 text-center">{item.unit_of_measure}</td>
                          {selectedBOM.status === 'draft' && (
                            <td className="px-4 py-3 text-center">
                              <button
                                onClick={() => handleDeleteItem(item.id)}
                                className="text-red-500 hover:text-red-400"
                              >
                                <TrashIcon className="h-4 w-4" />
                              </button>
                            </td>
                          )}
                          <td className="px-4 py-3 text-right">
                            {item.component_part?.id && (
                              <button
                                type="button"
                                onClick={() => navigate(`/routing?part_id=${item.component_part?.id}`)}
                                className="text-werco-primary hover:underline text-sm"
                              >
                                Create Routing
                              </button>
                            )}
                          </td>
                        </tr>
                      ))
                    ) : (
                      explodedView.map(item => renderExplodedItem(item))
                    )}
                  </tbody>
                </table>
              </div>
              )}

              {viewMode === 'single' && selectedBOM.items.length === 0 && (
                <EmptyState
                  icon={DocumentDuplicateIcon}
                  title="No items in this BOM"
                  description={
                    selectedBOM.status === 'draft'
                      ? 'Add components, hardware, or consumables to build out this assembly.'
                      : 'This BOM has no line items.'
                  }
                  action={
                    selectedBOM.status === 'draft'
                      ? { label: 'Add Item', onClick: () => setShowAddItemModal(true) }
                      : undefined
                  }
                />
              )}
            </>
          ) : (
            <EmptyState
              icon={DocumentDuplicateIcon}
              title="Select a BOM to view details"
              description="Choose a bill of materials from the list to see its components."
            />
          )}
        </div>
      </div>

      {/* Create BOM Modal */}
      <Modal open={showCreateModal} onClose={requestCloseCreateBOM} size="md" closeOnBackdrop={false}>
            <h3 className="text-lg font-semibold mb-4">Create New BOM</h3>
            <form onSubmit={handleCreateBOM} className="space-y-4">
              <FormField label="Part" required>
                {(field) => (
                  <select
                    {...field}
                    value={newBOM.part_id}
                    onChange={(e) => setNewBOM({ ...newBOM, part_id: parseInt(e.target.value) })}
                    className="input"
                    required
                  >
                    <option value={0}>Select a part...</option>
                    {parts
                      .filter(p => ['assembly', 'manufactured'].includes(p.part_type))
                      .map(part => (
                        <option key={part.id} value={part.id}>
                          {part.part_number} - {part.name} ({part.part_type})
                        </option>
                      ))}
                  </select>
                )}
              </FormField>
              {parts.filter(p => ['assembly', 'manufactured'].includes(p.part_type)).length === 0 && (
                <p className="text-sm text-orange-500 mt-1">
                  No assembly or manufactured parts found. Create parts first.
                </p>
              )}
              <div className="grid grid-cols-2 gap-4">
                <FormField label="Revision" required>
                  {(field) => (
                    <input
                      {...field}
                      type="text"
                      value={newBOM.revision}
                      onChange={(e) => setNewBOM({ ...newBOM, revision: e.target.value })}
                      className="input"
                      required
                    />
                  )}
                </FormField>
                <FormField label="Type">
                  {(field) => (
                    <select
                      {...field}
                      value={newBOM.bom_type}
                      onChange={(e) => setNewBOM({ ...newBOM, bom_type: e.target.value })}
                      className="input"
                    >
                      <option value="standard">Standard</option>
                      <option value="phantom">Phantom</option>
                      <option value="configurable">Configurable</option>
                    </select>
                  )}
                </FormField>
              </div>
              <FormField label="Description">
                {(field) => (
                  <textarea
                    {...field}
                    value={newBOM.description}
                    onChange={(e) => setNewBOM({ ...newBOM, description: e.target.value })}
                    className="input"
                    rows={2}
                  />
                )}
              </FormField>
              <div className="flex justify-end gap-3">
                <button type="button" onClick={requestCloseCreateBOM} className="btn-secondary" disabled={creatingBOM}>
                  Cancel
                </button>
                <LoadingButton type="submit" loading={creatingBOM} loadingText="Creating...">Create</LoadingButton>
              </div>
            </form>
      </Modal>

      {/* Import BOM / Drawing Modal */}
      <Modal open={showImportModal} onClose={() => setShowImportModal(false)} size="lg" closeOnBackdrop={false}>
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-lg font-semibold">Import BOM or Drawing</h3>
              <button onClick={() => setShowImportModal(false)}>
                <XMarkIcon className="h-5 w-5 text-slate-400" />
              </button>
            </div>
            <form onSubmit={handleImport} className="space-y-4">
              <FormField label="PDF, Word, or Excel Document">
                {(field) => (
                  <input
                    {...field}
                    type="file"
                    accept=".pdf,.doc,.docx,.xlsx,.xls"
                    onChange={(e) => setImportFile(e.target.files?.[0] || null)}
                    className="input"
                    required
                  />
                )}
              </FormField>
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={importCreateMissingParts}
                  onChange={(e) => setImportCreateMissingParts(e.target.checked)}
                  className="rounded border-slate-600"
                  aria-label="Create missing parts automatically"
                />
                <span className="text-sm">Create missing parts automatically</span>
              </label>
              {importWarnings.length > 0 && (
                <div className="bg-amber-500/10 border border-amber-500/30 rounded-md p-3 text-sm text-amber-300">
                  {importWarnings.map((w, idx) => (
                    <div key={idx}>{w}</div>
                  ))}
                </div>
              )}
              <div className="flex justify-end gap-3 pt-4 border-t">
                <button type="button" onClick={() => setShowImportModal(false)} className="btn-secondary">
                  Cancel
                </button>
                <button type="submit" className="btn-primary" disabled={importLoading}>
                  {importLoading ? 'Importing...' : 'Import'}
                </button>
              </div>
            </form>
      </Modal>

      {/* Import Preview Modal */}
      <Modal open={showPreviewModal && !!importPreview} onClose={() => setShowPreviewModal(false)} size="6xl" closeOnBackdrop={false}>
        {importPreview && (
          <>
            <div className="flex items-center justify-between mb-4">
              <div>
                <h3 className="text-lg font-semibold">Review Import</h3>
                <p className="text-sm text-slate-400">
                  {importPreview.document_type === 'bom' ? 'Assembly BOM' : 'Single Part'} • Confidence: {importPreview.extraction_confidence || 'low'}
                </p>
              </div>
              <button onClick={() => setShowPreviewModal(false)}>
                <XMarkIcon className="h-5 w-5 text-slate-400" />
              </button>
            </div>

            {importWarnings.length > 0 && (
              <div className="bg-amber-500/10 border border-amber-500/30 rounded-md p-3 text-sm text-amber-300 mb-4">
                {importWarnings.map((w, idx) => (
                  <div key={idx}>{w}</div>
                ))}
              </div>
            )}

            <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 mb-6">
              <FormField label="Part Number">
                {(field) => (
                  <input
                    {...field}
                    className="input"
                    value={importPreview.assembly.part_number || ''}
                    onChange={(e) => updatePreviewAssembly('part_number', e.target.value)}
                  />
                )}
              </FormField>
              <FormField label="Revision">
                {(field) => (
                  <input
                    {...field}
                    className="input"
                    value={importPreview.assembly.revision || ''}
                    onChange={(e) => updatePreviewAssembly('revision', e.target.value)}
                  />
                )}
              </FormField>
              <FormField label="Part Type">
                {(field) => (
                  <select
                    {...field}
                    className="input"
                    value={importPreview.assembly.part_type || (importPreview.document_type === 'bom' ? 'assembly' : 'manufactured')}
                    onChange={(e) => updatePreviewAssembly('part_type', e.target.value)}
                  >
                    <option value="manufactured">Manufactured</option>
                    <option value="assembly">Assembly</option>
                    <option value="purchased">Purchased</option>
                    <option value="raw_material">Raw Material</option>
                    <option value="hardware">Hardware</option>
                    <option value="consumable">Consumable</option>
                  </select>
                )}
              </FormField>
              <FormField label="Name" className="lg:col-span-2">
                {(field) => (
                  <input
                    {...field}
                    className="input"
                    value={importPreview.assembly.name || ''}
                    onChange={(e) => updatePreviewAssembly('name', e.target.value)}
                  />
                )}
              </FormField>
              <FormField label="Drawing #">
                {(field) => (
                  <input
                    {...field}
                    className="input"
                    value={importPreview.assembly.drawing_number || ''}
                    onChange={(e) => updatePreviewAssembly('drawing_number', e.target.value)}
                  />
                )}
              </FormField>
              <FormField label="Description" className="lg:col-span-3">
                {(field) => (
                  <textarea
                    {...field}
                    className="input"
                    rows={2}
                    value={importPreview.assembly.description || ''}
                    onChange={(e) => updatePreviewAssembly('description', e.target.value)}
                  />
                )}
              </FormField>
            </div>

            {importPreview.document_type === 'bom' && (
              <div className="mb-4">
                {importPreview.raw_columns && importPreview.raw_columns.length > 0 && (
                  <div className="mb-4">
                    <p className="text-sm text-slate-400 mb-2">Map your Excel columns:</p>
                    <div className="grid grid-cols-1 md:grid-cols-4 gap-3">
                      {[
                        { key: 'line_number', label: 'Line #' },
                        { key: 'part_number', label: 'Part #' },
                        { key: 'description', label: 'Description' },
                        { key: 'quantity', label: 'Qty' },
                        { key: 'unit_of_measure', label: 'UOM' },
                        { key: 'item_type', label: 'Item Type' },
                        { key: 'line_type', label: 'Line Type' },
                      ].map((field) => (
                        <div key={field.key}>
                          <label className="label">{field.label}</label>
                          <select
                            className="input"
                            value={importColumnMap[field.key] ?? ''}
                            onChange={(e) => {
                              const idx = e.target.value === '' ? null : parseInt(e.target.value);
                              const nextMap = { ...importColumnMap, [field.key]: idx };
                              setImportColumnMap(nextMap);
                              if (importPreview.raw_columns && importPreview.raw_rows) {
                                setImportDerivedItems(buildItemsFromRaw(importPreview.raw_columns, importPreview.raw_rows, nextMap));
                              }
                            }}
                          >
                            <option value="">Not mapped</option>
                            {(importPreview.raw_columns || []).map((col, idx) => (
                              <option key={col + idx} value={idx}>{col || `Column ${idx + 1}`}</option>
                            ))}
                          </select>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            )}

            {importPreview.document_type === 'bom' && (
              <div className="overflow-x-auto mb-6">
                <table className="min-w-full divide-y divide-slate-700">
                  <thead className="bg-slate-800/50">
                    <tr>
                      <th className="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">Line</th>
                      <th className="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">Part #</th>
                      <th className="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">Description</th>
                      <th className="px-3 py-2 text-right text-xs font-medium text-slate-400 uppercase">Qty</th>
                      <th className="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">UOM</th>
                      <th className="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">Item Type</th>
                      <th className="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">Line Type</th>
                    </tr>
                  </thead>
                  <tbody className="bg-fd-panel divide-y divide-slate-700">
                    {(importPreview.raw_columns && importPreview.raw_columns.length > 0 ? importDerivedItems : importPreview.items).map((item, index) => (
                      <tr key={index}>
                        <td className="px-3 py-2 text-sm">
                          <input
                            className="input w-20"
                            type="number"
                            aria-label="Line number"
                            value={item.line_number || (index + 1) * 10}
                            onChange={(e) => {
                              const value = parseInt(e.target.value);
                              if (importPreview.raw_columns && importPreview.raw_columns.length > 0) {
                                const next = [...importDerivedItems];
                                next[index] = { ...next[index], line_number: value };
                                setImportDerivedItems(next);
                              } else {
                                updatePreviewItem(index, 'line_number', value);
                              }
                            }}
                          />
                        </td>
                        <td className="px-3 py-2 text-sm">
                          <textarea
                            className="input min-h-[44px] h-auto leading-snug"
                            rows={2}
                            aria-label="Part number"
                            value={item.part_number || ''}
                            onChange={(e) => {
                              const value = e.target.value;
                              if (importPreview.raw_columns && importPreview.raw_columns.length > 0) {
                                const next = [...importDerivedItems];
                                next[index] = { ...next[index], part_number: value };
                                setImportDerivedItems(next);
                              } else {
                                updatePreviewItem(index, 'part_number', value);
                              }
                            }}
                          />
                        </td>
                        <td className="px-3 py-2 text-sm">
                          <textarea
                            className="input min-h-[44px] h-auto leading-snug"
                            rows={2}
                            aria-label="Description"
                            value={item.description || ''}
                            onChange={(e) => {
                              const value = e.target.value;
                              if (importPreview.raw_columns && importPreview.raw_columns.length > 0) {
                                const next = [...importDerivedItems];
                                next[index] = { ...next[index], description: value };
                                setImportDerivedItems(next);
                              } else {
                                updatePreviewItem(index, 'description', value);
                              }
                            }}
                          />
                        </td>
                        <td className="px-3 py-2 text-sm">
                          <input
                            className="input w-24 text-right"
                            type="number"
                            step="1"
                            aria-label="Quantity"
                            value={item.quantity ?? 1}
                            onChange={(e) => {
                              const value = parseFloat(e.target.value);
                              if (importPreview.raw_columns && importPreview.raw_columns.length > 0) {
                                const next = [...importDerivedItems];
                                next[index] = { ...next[index], quantity: value };
                                setImportDerivedItems(next);
                              } else {
                                updatePreviewItem(index, 'quantity', value);
                              }
                            }}
                          />
                        </td>
                        <td className="px-3 py-2 text-sm">
                          <input
                            className="input w-24"
                            aria-label="Unit of measure"
                            value={item.unit_of_measure || ''}
                            onChange={(e) => {
                              const value = e.target.value;
                              if (importPreview.raw_columns && importPreview.raw_columns.length > 0) {
                                const next = [...importDerivedItems];
                                next[index] = { ...next[index], unit_of_measure: value };
                                setImportDerivedItems(next);
                              } else {
                                updatePreviewItem(index, 'unit_of_measure', value);
                              }
                            }}
                          />
                        </td>
                        <td className="px-3 py-2 text-sm">
                          <select
                            className="input"
                            aria-label="Item type"
                            value={item.item_type || 'buy'}
                            onChange={(e) => {
                              const value = e.target.value;
                              if (importPreview.raw_columns && importPreview.raw_columns.length > 0) {
                                const next = [...importDerivedItems];
                                next[index] = { ...next[index], item_type: value };
                                setImportDerivedItems(next);
                              } else {
                                updatePreviewItem(index, 'item_type', value);
                              }
                            }}
                          >
                            <option value="make">Make</option>
                            <option value="buy">Buy</option>
                            <option value="phantom">Phantom</option>
                          </select>
                        </td>
                        <td className="px-3 py-2 text-sm">
                          <select
                            className="input"
                            aria-label="Line type"
                            value={item.line_type || 'component'}
                            onChange={(e) => {
                              const value = e.target.value;
                              if (importPreview.raw_columns && importPreview.raw_columns.length > 0) {
                                const next = [...importDerivedItems];
                                next[index] = { ...next[index], line_type: value as LineType };
                                setImportDerivedItems(next);
                              } else {
                                updatePreviewItem(index, 'line_type', value);
                              }
                            }}
                          >
                            <option value="component">Component</option>
                            <option value="hardware">Hardware</option>
                            <option value="consumable">Consumable</option>
                            <option value="reference">Reference</option>
                          </select>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {importPreview.items.length === 0 && (
                  <p className="text-sm text-slate-400 py-3">No BOM items detected.</p>
                )}
              </div>
            )}

            <div className="flex justify-end gap-3 pt-4 border-t">
              <button type="button" onClick={() => setShowPreviewModal(false)} className="btn-secondary">
                Cancel
              </button>
              <button type="button" onClick={handleCommitImport} className="btn-primary" disabled={importLoading}>
                {importLoading ? 'Creating...' : 'Create'}
              </button>
            </div>
          </>
        )}
      </Modal>

      {/* Add Item Modal */}
      <Modal
        open={showAddItemModal && !!selectedBOM}
        onClose={() => {
          setShowAddItemModal(false);
          setPartSearch('');
          setPartTypeFilter('all');
        }}
        size="5xl"
        closeOnBackdrop={false}
        className="rounded-2xl"
      >
            <div className="flex items-center justify-between mb-5">
              <div>
                <h3 className="text-lg font-semibold">Add BOM Item</h3>
                <p className="text-sm text-slate-400">Pick a component and configure its usage</p>
              </div>
              <button
                type="button"
                onClick={openNewPartModal}
                className="btn-secondary"
                title="Create a new part"
              >
                <PlusIcon className="h-4 w-4 mr-1 inline" />
                New Part
              </button>
            </div>
            <form onSubmit={handleAddItem} className="space-y-5">
              <div className="grid grid-cols-1 lg:grid-cols-5 gap-6">
                <div className="lg:col-span-2 space-y-3">
                  <div className="relative">
                    <input
                      type="text"
                      placeholder="Search parts..."
                      aria-label="Search parts"
                      value={partSearch}
                      onChange={(e) => setPartSearch(e.target.value)}
                      className="input pr-10"
                    />
                    {partSearch && (
                      <button
                        type="button"
                        onClick={() => setPartSearch('')}
                        className="absolute right-3 top-1/2 -translate-y-1/2 text-slate-500 hover:text-slate-400"
                        aria-label="Clear search"
                      >
                        <XMarkIcon className="h-4 w-4" />
                      </button>
                    )}
                  </div>
                  <div className="flex flex-wrap gap-2 text-xs">
                    {partTypeOptions.map(option => (
                      <button
                        key={option.id}
                        type="button"
                        onClick={() => setPartTypeFilter(option.id)}
                        className={`rounded-full border px-3 py-1 font-medium transition ${
                          partTypeFilter === option.id
                            ? 'border-werco-500 bg-werco-50 text-werco-700'
                            : 'border-slate-700 text-slate-400 hover:border-werco-300'
                        }`}
                      >
                        {option.label}
                      </button>
                    ))}
                  </div>
                  <div className="text-xs text-slate-400">
                    Showing {filteredParts.length} part{filteredParts.length === 1 ? '' : 's'}
                  </div>
                  <div className="max-h-[360px] overflow-y-auto space-y-2 pr-1">
                    {filteredParts.map(part => (
                      <button
                        key={part.id}
                        type="button"
                        onClick={() => handleSelectPart(part.id)}
                        aria-label={`Select part ${part.part_number}`}
                        className={`w-full text-left rounded-xl border px-3 py-2.5 transition ${
                          newItem.component_part_id === part.id
                            ? 'border-werco-500 bg-werco-50 shadow-sm'
                            : 'border-slate-700 hover:border-werco-300 hover:bg-slate-800/50'
                        }`}
                      >
                        <div className="flex items-start justify-between gap-2">
                          <div>
                            <div className="font-semibold text-white">{part.part_number}</div>
                            <div className="text-sm text-slate-400">{part.name}</div>
                          </div>
                          <span className={`inline-flex px-2 py-0.5 rounded-full text-xs font-medium ${partTypeBadge[part.part_type] || 'bg-slate-800 text-slate-400'}`}>
                            {partTypeLabels[part.part_type] || part.part_type}
                          </span>
                        </div>
                      </button>
                    ))}
                    {filteredParts.length === 0 && (
                      <div className="border border-dashed border-slate-700 rounded-xl p-4 text-sm text-slate-400 text-center">
                        No parts match your filters.
                      </div>
                    )}
                  </div>
                </div>
                <div className="lg:col-span-3 space-y-4">
                  <div className="rounded-xl border border-slate-700 bg-slate-800/50 p-4">
                    <div className="text-xs text-slate-400 uppercase tracking-wide mb-2">Selected Component</div>
                    {newItem.component_part_id > 0 ? (
                      <div>
                        <div className="font-semibold text-white">
                          {parts.find(p => p.id === newItem.component_part_id)?.part_number}
                        </div>
                        <div className="text-sm text-slate-400">
                          {parts.find(p => p.id === newItem.component_part_id)?.name}
                        </div>
                      </div>
                    ) : (
                      <div className="text-sm text-slate-400">Select a part to continue.</div>
                    )}
                  </div>
                  <div className="grid grid-cols-2 gap-4">
                    <FormField label="Line Type">
                      {(field) => (
                        <select
                          {...field}
                          value={newItem.line_type}
                          onChange={(e) => setNewItem({ ...newItem, line_type: e.target.value as LineType })}
                          className="input"
                        >
                          <option value="component">Component (Made Part)</option>
                          <option value="hardware">Hardware (Bolts, Nuts, etc.)</option>
                          <option value="consumable">Consumable (Adhesive, etc.)</option>
                          <option value="reference">Reference Only</option>
                        </select>
                      )}
                    </FormField>
                    <FormField label="Make/Buy">
                      {(field) => (
                        <select
                          {...field}
                          value={newItem.item_type}
                          onChange={(e) => setNewItem({ ...newItem, item_type: e.target.value as any })}
                          className="input"
                        >
                          <option value="make">Make</option>
                          <option value="buy">Buy</option>
                          <option value="phantom">Phantom</option>
                        </select>
                      )}
                    </FormField>
                  </div>
                  <div className="grid grid-cols-3 gap-4">
                    <FormField label="Item #">
                      {(field) => (
                        <input
                          {...field}
                          type="number"
                          value={newItem.item_number}
                          onChange={(e) => setNewItem({ ...newItem, item_number: parseInt(e.target.value) })}
                          className="input"
                          required
                        />
                      )}
                    </FormField>
                    <FormField label="Quantity">
                      {(field) => (
                        <input
                          {...field}
                          type="number"
                          step="1"
                          min="0"
                          value={newItem.quantity}
                          onChange={(e) => setNewItem({ ...newItem, quantity: parseFloat(e.target.value) })}
                          className="input"
                          required
                        />
                      )}
                    </FormField>
                    <FormField label="Find Number">
                      {(field) => (
                        <input
                          {...field}
                          type="text"
                          value={newItem.find_number}
                          onChange={(e) => setNewItem({ ...newItem, find_number: e.target.value })}
                          className="input"
                          placeholder="e.g., 1, 2, 3"
                        />
                      )}
                    </FormField>
                  </div>
                </div>
                </div>
                
              {/* Hardware-specific fields */}
              {(newItem.line_type === 'hardware') && (
                <div className="p-3 bg-amber-500/10 rounded-lg space-y-3">
                  <div className="text-sm font-medium text-amber-300">Hardware Details</div>
                  <div className="grid grid-cols-2 gap-4">
                    <FormField label="Torque Spec">
                      {(field) => (
                        <input
                          {...field}
                          type="text"
                          value={newItem.torque_spec}
                          onChange={(e) => setNewItem({ ...newItem, torque_spec: e.target.value })}
                          className="input"
                          placeholder="e.g., 25 ft-lbs"
                        />
                      )}
                    </FormField>
                    <FormField label="Scrap %">
                      {(field) => (
                        <input
                          {...field}
                          type="number"
                          step="0.01"
                          min="0"
                          max="1"
                          value={newItem.scrap_factor}
                          onChange={(e) => setNewItem({ ...newItem, scrap_factor: parseFloat(e.target.value) })}
                          className="input"
                          placeholder="0.05 = 5%"
                        />
                      )}
                    </FormField>
                  </div>
                  <FormField label="Installation Notes">
                    {(field) => (
                      <textarea
                        {...field}
                        value={newItem.installation_notes}
                        onChange={(e) => setNewItem({ ...newItem, installation_notes: e.target.value })}
                        className="input"
                        rows={2}
                        placeholder="Assembly instructions, loctite requirements, etc."
                      />
                    )}
                  </FormField>
                </div>
              )}
              
              {newItem.line_type !== 'hardware' && (
                <FormField label="Scrap %">
                  {(field) => (
                    <input
                      {...field}
                      type="number"
                      step="0.01"
                      min="0"
                      max="1"
                      value={newItem.scrap_factor}
                      onChange={(e) => setNewItem({ ...newItem, scrap_factor: parseFloat(e.target.value) })}
                      className="input"
                      placeholder="0.05 = 5%"
                    />
                  )}
                </FormField>
              )}
              
              <div>
                <label className="flex items-center">
                  <input
                    type="checkbox"
                    checked={newItem.is_optional}
                    onChange={(e) => setNewItem({ ...newItem, is_optional: e.target.checked })}
                    className="mr-2"
                    aria-label="Optional component"
                  />
                  <span className="text-sm">Optional component</span>
                </label>
              </div>
              <div className="flex justify-end gap-3">
                <button
                  type="button"
                  onClick={() => {
                    setShowAddItemModal(false);
                    setPartSearch('');
                    setPartTypeFilter('all');
                  }}
                  className="btn-secondary"
                >
                  Cancel
                </button>
                <button type="submit" className="btn-primary" disabled={newItem.component_part_id <= 0}>
                  Add Item
                </button>
              </div>
            </form>
      </Modal>

      {/* New Part Modal (nested inside Add Item flow) */}
      <Modal open={showNewPartModal} onClose={requestCloseNewPart} size="md" closeOnBackdrop={false}>
            <h3 className="text-lg font-semibold mb-4">Create New Part</h3>
            <form onSubmit={handleCreateNewPart} className="space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <FormField label="Part Number" required>
                  {(field) => (
                    <input
                      {...field}
                      type="text"
                      value={newPart.part_number}
                      onChange={(e) => setNewPart({ ...newPart, part_number: e.target.value.toUpperCase() })}
                      className="input"
                      required
                      placeholder="e.g., PART-001"
                    />
                  )}
                </FormField>
                <FormField label="Revision">
                  {(field) => (
                    <input
                      {...field}
                      type="text"
                      value={newPart.revision}
                      onChange={(e) => setNewPart({ ...newPart, revision: e.target.value.toUpperCase() })}
                      className="input"
                      placeholder="A"
                    />
                  )}
                </FormField>
              </div>
              <FormField label="Name" required>
                {(field) => (
                  <input
                    {...field}
                    type="text"
                    value={newPart.name}
                    onChange={(e) => setNewPart({ ...newPart, name: e.target.value })}
                    className="input"
                    required
                    placeholder="Part name"
                  />
                )}
              </FormField>
              <FormField label="Type" required>
                {(field) => (
                  <select
                    {...field}
                    value={newPart.part_type}
                    onChange={(e) => setNewPart({ ...newPart, part_type: e.target.value as any })}
                    className="input"
                    required
                  >
                    <option value="manufactured">Manufactured</option>
                    <option value="purchased">Purchased</option>
                    <option value="assembly">Assembly</option>
                    <option value="raw_material">Raw Material</option>
                    <option value="hardware">Hardware</option>
                    <option value="consumable">Consumable</option>
                  </select>
                )}
              </FormField>
              <FormField label="Description">
                {(field) => (
                  <textarea
                    {...field}
                    value={newPart.description}
                    onChange={(e) => setNewPart({ ...newPart, description: e.target.value })}
                    className="input"
                    rows={2}
                    placeholder="Optional description"
                  />
                )}
              </FormField>
              <div className="flex justify-end gap-3 pt-2 border-t">
                <button type="button" onClick={requestCloseNewPart} className="btn-secondary" disabled={creatingPart}>
                  Cancel
                </button>
                <LoadingButton type="submit" loading={creatingPart} loadingText="Creating...">Create &amp; Select</LoadingButton>
              </div>
            </form>
      </Modal>
    </div>
  );
}
