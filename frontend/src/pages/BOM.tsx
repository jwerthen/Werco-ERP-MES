import React, { useEffect, useMemo, useState } from 'react';
import api from '../services/api';
import { Part } from '../types';
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
  component: 'bg-cyan-100 text-cyan-800',
  hardware: 'bg-amber-100 text-amber-800',
  consumable: 'bg-orange-100 text-orange-800',
  reference: 'bg-gray-100 text-gray-600',
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
  manufactured: 'bg-cyan-100 text-cyan-800',
  assembly: 'bg-indigo-100 text-indigo-800',
  purchased: 'bg-emerald-100 text-emerald-800',
  raw_material: 'bg-blue-100 text-blue-800',
  hardware: 'bg-amber-100 text-amber-800',
  consumable: 'bg-orange-100 text-orange-800',
};

export default function BOMPage() {
  const [boms, setBoms] = useState<BOM[]>([]);
  const [parts, setParts] = useState<Part[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedBOM, setSelectedBOM] = useState<BOM | null>(null);
  const [explodedView, setExplodedView] = useState<BOMItem[]>([]);
  const [expandedItems, setExpandedItems] = useState<Set<number>>(new Set());
  const [showCreateModal, setShowCreateModal] = useState(false);
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

  const [newBOM, setNewBOM] = useState({ part_id: 0, revision: 'A', description: '', bom_type: 'standard' });
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
  const [newPart, setNewPart] = useState({
    part_number: '',
    name: '',
    part_type: 'manufactured' as 'manufactured' | 'purchased' | 'assembly' | 'raw_material',
    revision: 'A',
    description: ''
  });

  useEffect(() => {
    loadData();
  }, []);

  useEffect(() => {
    if (selectedBOM && viewMode === 'exploded') {
      loadExplodedBOM(selectedBOM.id);
    }
  }, [selectedBOM, viewMode]);

  const loadData = async () => {
    try {
      // Load BOMs and parts separately so one failure doesn't block the other
      const [bomsResult, partsResult] = await Promise.allSettled([
        api.getBOMs({ active_only: true }),
        api.getParts({ active_only: true })
      ]);
      
      if (bomsResult.status === 'fulfilled') {
        setBoms(bomsResult.value);
      } else {
        console.error('Failed to load BOMs:', bomsResult.reason);
      }
      
      if (partsResult.status === 'fulfilled') {
        setParts(partsResult.value);
      } else {
        console.error('Failed to load parts:', partsResult.reason);
      }
    } catch (err) {
      console.error('Failed to load data:', err);
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
    try {
      const response = await api.explodeBOM(bomId);
      setExplodedView(response.items);
    } catch (err) {
      console.error('Failed to explode BOM:', err);
    }
  };

  const handleCreateBOM = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      const created = await api.createBOM(newBOM);
      setBoms([...boms, created]);
      setSelectedBOM(created);
      setShowCreateModal(false);
      setNewBOM({ part_id: 0, revision: 'A', description: '', bom_type: 'standard' });
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to create BOM');
    }
  };

  const handleImport = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!importFile) {
      alert('Please select a file to import.');
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
      alert(err.response?.data?.detail || 'Failed to import BOM');
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
        alert(`Part created: ${result.assembly_part_number}`);
      }
      if (result.warnings?.length) {
        alert(`Import completed with warnings:\n- ${result.warnings.join('\n- ')}`);
      }
      setShowPreviewModal(false);
      setImportPreview(null);
      setImportFile(null);
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to create from preview');
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
      alert('Select a component part before adding.');
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
      alert(err.response?.data?.detail || 'Failed to add item');
    }
  };

  const handleCreateNewPart = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      const createdPart = await api.createPart(newPart);
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
      alert(err.response?.data?.detail || 'Failed to create part');
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
      alert(err.response?.data?.detail || 'Failed to delete item');
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
      alert(err.response?.data?.detail || 'Failed to delete BOM');
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
      alert(err.response?.data?.detail || 'Failed to release BOM');
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
      alert(err.response?.data?.detail || 'Failed to unrelease BOM');
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
        <tr className="hover:bg-gray-50">
          <td className="px-4 py-3" style={{ paddingLeft: `${16 + depth * 24}px` }}>
            <div className="flex items-center">
              {hasChildren ? (
                <button onClick={() => toggleExpanded(item.id)} className="mr-2">
                  {isExpanded ? (
                    <ChevronDownIcon className="h-4 w-4 text-gray-500" />
                  ) : (
                    <ChevronRightIcon className="h-4 w-4 text-gray-500" />
                  )}
                </button>
              ) : (
                <span className="w-6" />
              )}
              <span className="text-sm text-gray-500">L{(item.level || 0)}</span>
            </div>
          </td>
          <td className="px-4 py-3 font-medium">{item.find_number || item.item_number}</td>
          <td className="px-4 py-3">
            <div>
              <div className="font-medium text-werco-primary">{item.component_part?.part_number}</div>
              <div className="text-sm text-gray-500">{item.component_part?.name}</div>
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

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <h1 className="text-2xl font-bold text-gray-900">Bill of Materials</h1>
        <div className="flex gap-2">
          <button onClick={() => setShowImportModal(true)} className="btn-secondary flex items-center">
            <DocumentDuplicateIcon className="h-5 w-5 mr-2" />
            Import BOM/Drawing
          </button>
          <button onClick={() => setShowCreateModal(true)} className="btn-primary flex items-center">
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
              <div
                key={bom.id}
                onClick={() => { setSelectedBOM(bom); setViewMode('single'); }}
                className={`p-3 rounded-lg cursor-pointer border transition-colors ${
                  selectedBOM?.id === bom.id 
                    ? 'border-werco-primary bg-blue-50' 
                    : 'border-gray-200 hover:border-gray-300'
                }`}
              >
                <div className="flex justify-between items-start">
                  <div>
                    <div className="font-medium">{bom.part?.part_number}</div>
                    <div className="text-sm text-gray-500">{bom.part?.name}</div>
                  </div>
                  <span className={`text-xs px-2 py-1 rounded ${
                    bom.status === 'released' ? 'bg-green-100 text-green-800' :
                    bom.status === 'draft' ? 'bg-gray-100 text-gray-800' :
                    'bg-yellow-100 text-yellow-800'
                  }`}>
                    {bom.status}
                  </span>
                </div>
                <div className="text-xs text-gray-400 mt-1">
                  Rev {bom.revision} | {bom.items.length} items
                </div>
              </div>
            ))}
            {boms.length === 0 && (
              <p className="text-gray-500 text-center py-4">No BOMs created yet</p>
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
                  <p className="text-gray-500">{selectedBOM.part?.name}</p>
                  <p className="text-sm text-gray-400">Revision {selectedBOM.revision}</p>
                </div>
                <div className="flex gap-2">
                  <div className="flex rounded-lg border border-gray-300 overflow-hidden">
                    <button
                      onClick={() => setViewMode('single')}
                      className={`px-3 py-1 text-sm ${viewMode === 'single' ? 'bg-werco-primary text-white' : 'bg-white'}`}
                    >
                      Single Level
                    </button>
                    <button
                      onClick={() => setViewMode('exploded')}
                      className={`px-3 py-1 text-sm ${viewMode === 'exploded' ? 'bg-werco-primary text-white' : 'bg-white'}`}
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
              <div className="overflow-x-auto">
                <table className="min-w-full divide-y divide-gray-200">
                  <thead className="bg-gray-50">
                    <tr>
                      {viewMode === 'exploded' && <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Level</th>}
                      <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Item #</th>
                      <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Part</th>
                      <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Category</th>
                      <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">Qty</th>
                      {viewMode === 'exploded' && <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">Ext Qty</th>}
                      <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase">UOM</th>
                      {viewMode === 'single' && selectedBOM.status === 'draft' && (
                        <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase">Actions</th>
                      )}
                    </tr>
                  </thead>
                  <tbody className="bg-white divide-y divide-gray-200">
                    {viewMode === 'single' ? (
                      selectedBOM.items.map(item => (
                        <tr key={item.id} className={`hover:bg-gray-50 ${item.line_type === 'hardware' ? 'bg-amber-50/50' : ''}`}>
                          <td className="px-4 py-3 font-medium">{item.find_number || item.item_number}</td>
                          <td className="px-4 py-3">
                            <div className="flex items-center">
                              <div>
                                <div className="font-medium text-werco-primary">{item.component_part?.part_number}</div>
                                <div className="text-sm text-gray-500">{item.component_part?.name}</div>
                                {item.torque_spec && (
                                  <div className="text-xs text-amber-600">Torque: {item.torque_spec}</div>
                                )}
                                {item.installation_notes && (
                                  <div className="text-xs text-gray-400 italic">{item.installation_notes}</div>
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
                                className="text-red-500 hover:text-red-700"
                              >
                                <TrashIcon className="h-4 w-4" />
                              </button>
                            </td>
                          )}
                        </tr>
                      ))
                    ) : (
                      explodedView.map(item => renderExplodedItem(item))
                    )}
                  </tbody>
                </table>
              </div>

              {selectedBOM.items.length === 0 && (
                <p className="text-gray-500 text-center py-8">No items in this BOM</p>
              )}
            </>
          ) : (
            <div className="text-center py-12 text-gray-500">
              <DocumentDuplicateIcon className="h-12 w-12 mx-auto mb-4 text-gray-300" />
              <p>Select a BOM to view details</p>
            </div>
          )}
        </div>
      </div>

      {/* Create BOM Modal */}
      {showCreateModal && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg p-6 max-w-md w-full mx-4">
            <h3 className="text-lg font-semibold mb-4">Create New BOM</h3>
            <form onSubmit={handleCreateBOM} className="space-y-4">
              <div>
                <label className="label">Part</label>
                <select
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
                {parts.filter(p => ['assembly', 'manufactured'].includes(p.part_type)).length === 0 && (
                  <p className="text-sm text-orange-500 mt-1">
                    No assembly or manufactured parts found. Create parts first.
                  </p>
                )}
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="label">Revision</label>
                  <input
                    type="text"
                    value={newBOM.revision}
                    onChange={(e) => setNewBOM({ ...newBOM, revision: e.target.value })}
                    className="input"
                    required
                  />
                </div>
                <div>
                  <label className="label">Type</label>
                  <select
                    value={newBOM.bom_type}
                    onChange={(e) => setNewBOM({ ...newBOM, bom_type: e.target.value })}
                    className="input"
                  >
                    <option value="standard">Standard</option>
                    <option value="phantom">Phantom</option>
                    <option value="configurable">Configurable</option>
                  </select>
                </div>
              </div>
              <div>
                <label className="label">Description</label>
                <textarea
                  value={newBOM.description}
                  onChange={(e) => setNewBOM({ ...newBOM, description: e.target.value })}
                  className="input"
                  rows={2}
                />
              </div>
              <div className="flex justify-end gap-3">
                <button type="button" onClick={() => setShowCreateModal(false)} className="btn-secondary">
                  Cancel
                </button>
                <button type="submit" className="btn-primary">Create</button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Import BOM / Drawing Modal */}
      {showImportModal && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg p-6 max-w-lg w-full mx-4">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-lg font-semibold">Import BOM or Drawing</h3>
              <button onClick={() => setShowImportModal(false)}>
                <XMarkIcon className="h-5 w-5 text-gray-500" />
              </button>
            </div>
            <form onSubmit={handleImport} className="space-y-4">
              <div>
                <label className="label">PDF, Word, or Excel Document</label>
                <input
                  type="file"
                  accept=".pdf,.doc,.docx,.xlsx,.xls"
                  onChange={(e) => setImportFile(e.target.files?.[0] || null)}
                  className="input"
                  required
                />
              </div>
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={importCreateMissingParts}
                  onChange={(e) => setImportCreateMissingParts(e.target.checked)}
                  className="rounded border-gray-300"
                />
                <span className="text-sm">Create missing parts automatically</span>
              </label>
              {importWarnings.length > 0 && (
                <div className="bg-amber-50 border border-amber-200 rounded-md p-3 text-sm text-amber-800">
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
          </div>
        </div>
      )}

      {/* Import Preview Modal */}
      {showPreviewModal && importPreview && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg p-6 max-w-6xl w-full mx-4 max-h-[90vh] overflow-y-auto">
            <div className="flex items-center justify-between mb-4">
              <div>
                <h3 className="text-lg font-semibold">Review Import</h3>
                <p className="text-sm text-gray-500">
                  {importPreview.document_type === 'bom' ? 'Assembly BOM' : 'Single Part'} • Confidence: {importPreview.extraction_confidence || 'low'}
                </p>
              </div>
              <button onClick={() => setShowPreviewModal(false)}>
                <XMarkIcon className="h-5 w-5 text-gray-500" />
              </button>
            </div>

            {importWarnings.length > 0 && (
              <div className="bg-amber-50 border border-amber-200 rounded-md p-3 text-sm text-amber-800 mb-4">
                {importWarnings.map((w, idx) => (
                  <div key={idx}>{w}</div>
                ))}
              </div>
            )}

            <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 mb-6">
              <div>
                <label className="label">Part Number</label>
                <input
                  className="input"
                  value={importPreview.assembly.part_number || ''}
                  onChange={(e) => updatePreviewAssembly('part_number', e.target.value)}
                />
              </div>
              <div>
                <label className="label">Revision</label>
                <input
                  className="input"
                  value={importPreview.assembly.revision || ''}
                  onChange={(e) => updatePreviewAssembly('revision', e.target.value)}
                />
              </div>
              <div>
                <label className="label">Part Type</label>
                <select
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
              </div>
              <div className="lg:col-span-2">
                <label className="label">Name</label>
                <input
                  className="input"
                  value={importPreview.assembly.name || ''}
                  onChange={(e) => updatePreviewAssembly('name', e.target.value)}
                />
              </div>
              <div>
                <label className="label">Drawing #</label>
                <input
                  className="input"
                  value={importPreview.assembly.drawing_number || ''}
                  onChange={(e) => updatePreviewAssembly('drawing_number', e.target.value)}
                />
              </div>
              <div className="lg:col-span-3">
                <label className="label">Description</label>
                <textarea
                  className="input"
                  rows={2}
                  value={importPreview.assembly.description || ''}
                  onChange={(e) => updatePreviewAssembly('description', e.target.value)}
                />
              </div>
            </div>

            {importPreview.document_type === 'bom' && (
              <div className="mb-4">
                {importPreview.raw_columns && importPreview.raw_columns.length > 0 && (
                  <div className="mb-4">
                    <p className="text-sm text-gray-600 mb-2">Map your Excel columns:</p>
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
                <table className="min-w-full divide-y divide-gray-200">
                  <thead className="bg-gray-50">
                    <tr>
                      <th className="px-3 py-2 text-left text-xs font-medium text-gray-500 uppercase">Line</th>
                      <th className="px-3 py-2 text-left text-xs font-medium text-gray-500 uppercase">Part #</th>
                      <th className="px-3 py-2 text-left text-xs font-medium text-gray-500 uppercase">Description</th>
                      <th className="px-3 py-2 text-right text-xs font-medium text-gray-500 uppercase">Qty</th>
                      <th className="px-3 py-2 text-left text-xs font-medium text-gray-500 uppercase">UOM</th>
                      <th className="px-3 py-2 text-left text-xs font-medium text-gray-500 uppercase">Item Type</th>
                      <th className="px-3 py-2 text-left text-xs font-medium text-gray-500 uppercase">Line Type</th>
                    </tr>
                  </thead>
                  <tbody className="bg-white divide-y divide-gray-200">
                    {(importPreview.raw_columns && importPreview.raw_columns.length > 0 ? importDerivedItems : importPreview.items).map((item, index) => (
                      <tr key={index}>
                        <td className="px-3 py-2 text-sm">
                          <input
                            className="input w-20"
                            type="number"
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
                          <input
                            className="input"
                            value={item.part_number || ''}
                            title={item.part_number || ''}
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
                            step="0.001"
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
                  <p className="text-sm text-gray-500 py-3">No BOM items detected.</p>
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
          </div>
        </div>
      )}

      {/* Add Item Modal */}
      {showAddItemModal && selectedBOM && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-2xl p-6 max-w-5xl w-full mx-4">
            <div className="flex items-center justify-between mb-5">
              <div>
                <h3 className="text-lg font-semibold">Add BOM Item</h3>
                <p className="text-sm text-gray-500">Pick a component and configure its usage</p>
              </div>
              <button
                type="button"
                onClick={() => setShowNewPartModal(true)}
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
                      value={partSearch}
                      onChange={(e) => setPartSearch(e.target.value)}
                      className="input pr-10"
                    />
                    {partSearch && (
                      <button
                        type="button"
                        onClick={() => setPartSearch('')}
                        className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600"
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
                            : 'border-gray-200 text-gray-600 hover:border-werco-300'
                        }`}
                      >
                        {option.label}
                      </button>
                    ))}
                  </div>
                  <div className="text-xs text-gray-500">
                    Showing {filteredParts.length} part{filteredParts.length === 1 ? '' : 's'}
                  </div>
                  <div className="max-h-[360px] overflow-y-auto space-y-2 pr-1">
                    {filteredParts.map(part => (
                      <button
                        key={part.id}
                        type="button"
                        onClick={() => handleSelectPart(part.id)}
                        className={`w-full text-left rounded-xl border px-3 py-2.5 transition ${
                          newItem.component_part_id === part.id
                            ? 'border-werco-500 bg-werco-50 shadow-sm'
                            : 'border-gray-200 hover:border-werco-300 hover:bg-gray-50'
                        }`}
                      >
                        <div className="flex items-start justify-between gap-2">
                          <div>
                            <div className="font-semibold text-gray-900">{part.part_number}</div>
                            <div className="text-sm text-gray-500">{part.name}</div>
                          </div>
                          <span className={`inline-flex px-2 py-0.5 rounded-full text-xs font-medium ${partTypeBadge[part.part_type] || 'bg-gray-100 text-gray-600'}`}>
                            {partTypeLabels[part.part_type] || part.part_type}
                          </span>
                        </div>
                      </button>
                    ))}
                    {filteredParts.length === 0 && (
                      <div className="border border-dashed border-gray-200 rounded-xl p-4 text-sm text-gray-500 text-center">
                        No parts match your filters.
                      </div>
                    )}
                  </div>
                </div>
                <div className="lg:col-span-3 space-y-4">
                  <div className="rounded-xl border border-gray-200 bg-gray-50 p-4">
                    <div className="text-xs text-gray-500 uppercase tracking-wide mb-2">Selected Component</div>
                    {newItem.component_part_id > 0 ? (
                      <div>
                        <div className="font-semibold text-gray-900">
                          {parts.find(p => p.id === newItem.component_part_id)?.part_number}
                        </div>
                        <div className="text-sm text-gray-600">
                          {parts.find(p => p.id === newItem.component_part_id)?.name}
                        </div>
                      </div>
                    ) : (
                      <div className="text-sm text-gray-500">Select a part to continue.</div>
                    )}
                  </div>
                  <div className="grid grid-cols-2 gap-4">
                    <div>
                      <label className="label">Line Type</label>
                      <select
                        value={newItem.line_type}
                        onChange={(e) => setNewItem({ ...newItem, line_type: e.target.value as LineType })}
                        className="input"
                      >
                        <option value="component">Component (Made Part)</option>
                        <option value="hardware">Hardware (Bolts, Nuts, etc.)</option>
                        <option value="consumable">Consumable (Adhesive, etc.)</option>
                        <option value="reference">Reference Only</option>
                      </select>
                    </div>
                    <div>
                      <label className="label">Make/Buy</label>
                      <select
                        value={newItem.item_type}
                        onChange={(e) => setNewItem({ ...newItem, item_type: e.target.value as any })}
                        className="input"
                      >
                        <option value="make">Make</option>
                        <option value="buy">Buy</option>
                        <option value="phantom">Phantom</option>
                      </select>
                    </div>
                  </div>
                  <div className="grid grid-cols-3 gap-4">
                    <div>
                      <label className="label">Item #</label>
                      <input
                        type="number"
                        value={newItem.item_number}
                        onChange={(e) => setNewItem({ ...newItem, item_number: parseInt(e.target.value) })}
                        className="input"
                        required
                      />
                    </div>
                    <div>
                      <label className="label">Quantity</label>
                      <input
                        type="number"
                        step="0.001"
                        min="0"
                        value={newItem.quantity}
                        onChange={(e) => setNewItem({ ...newItem, quantity: parseFloat(e.target.value) })}
                        className="input"
                        required
                      />
                    </div>
                    <div>
                      <label className="label">Find Number</label>
                      <input
                        type="text"
                        value={newItem.find_number}
                        onChange={(e) => setNewItem({ ...newItem, find_number: e.target.value })}
                        className="input"
                        placeholder="e.g., 1, 2, 3"
                      />
                    </div>
                  </div>
                </div>
                </div>
                
              {/* Hardware-specific fields */}
              {(newItem.line_type === 'hardware') && (
                <div className="p-3 bg-amber-50 rounded-lg space-y-3">
                  <div className="text-sm font-medium text-amber-800">Hardware Details</div>
                  <div className="grid grid-cols-2 gap-4">
                    <div>
                      <label className="label">Torque Spec</label>
                      <input
                        type="text"
                        value={newItem.torque_spec}
                        onChange={(e) => setNewItem({ ...newItem, torque_spec: e.target.value })}
                        className="input"
                        placeholder="e.g., 25 ft-lbs"
                      />
                    </div>
                    <div>
                      <label className="label">Scrap %</label>
                      <input
                        type="number"
                        step="0.01"
                        min="0"
                        max="1"
                        value={newItem.scrap_factor}
                        onChange={(e) => setNewItem({ ...newItem, scrap_factor: parseFloat(e.target.value) })}
                        className="input"
                        placeholder="0.05 = 5%"
                      />
                    </div>
                  </div>
                  <div>
                    <label className="label">Installation Notes</label>
                    <textarea
                      value={newItem.installation_notes}
                      onChange={(e) => setNewItem({ ...newItem, installation_notes: e.target.value })}
                      className="input"
                      rows={2}
                      placeholder="Assembly instructions, loctite requirements, etc."
                    />
                  </div>
                </div>
              )}
              
              {newItem.line_type !== 'hardware' && (
                <div>
                  <label className="label">Scrap %</label>
                  <input
                    type="number"
                    step="0.01"
                    min="0"
                    max="1"
                    value={newItem.scrap_factor}
                    onChange={(e) => setNewItem({ ...newItem, scrap_factor: parseFloat(e.target.value) })}
                    className="input"
                    placeholder="0.05 = 5%"
                  />
                </div>
              )}
              
              <div>
                <label className="flex items-center">
                  <input
                    type="checkbox"
                    checked={newItem.is_optional}
                    onChange={(e) => setNewItem({ ...newItem, is_optional: e.target.checked })}
                    className="mr-2"
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
          </div>
        </div>
      )}

      {/* New Part Modal (nested inside Add Item flow) */}
      {showNewPartModal && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-[60]">
          <div className="bg-white rounded-lg p-6 max-w-md w-full mx-4">
            <h3 className="text-lg font-semibold mb-4">Create New Part</h3>
            <form onSubmit={handleCreateNewPart} className="space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="label">Part Number *</label>
                  <input
                    type="text"
                    value={newPart.part_number}
                    onChange={(e) => setNewPart({ ...newPart, part_number: e.target.value.toUpperCase() })}
                    className="input"
                    required
                    placeholder="e.g., PART-001"
                  />
                </div>
                <div>
                  <label className="label">Revision</label>
                  <input
                    type="text"
                    value={newPart.revision}
                    onChange={(e) => setNewPart({ ...newPart, revision: e.target.value.toUpperCase() })}
                    className="input"
                    placeholder="A"
                  />
                </div>
              </div>
              <div>
                <label className="label">Name *</label>
                <input
                  type="text"
                  value={newPart.name}
                  onChange={(e) => setNewPart({ ...newPart, name: e.target.value })}
                  className="input"
                  required
                  placeholder="Part name"
                />
              </div>
              <div>
                <label className="label">Type *</label>
                <select
                  value={newPart.part_type}
                  onChange={(e) => setNewPart({ ...newPart, part_type: e.target.value as any })}
                  className="input"
                  required
                >
                  <option value="manufactured">Manufactured</option>
                  <option value="purchased">Purchased</option>
                  <option value="assembly">Assembly</option>
                  <option value="raw_material">Raw Material</option>
                </select>
              </div>
              <div>
                <label className="label">Description</label>
                <textarea
                  value={newPart.description}
                  onChange={(e) => setNewPart({ ...newPart, description: e.target.value })}
                  className="input"
                  rows={2}
                  placeholder="Optional description"
                />
              </div>
              <div className="flex justify-end gap-3 pt-2 border-t">
                <button type="button" onClick={() => setShowNewPartModal(false)} className="btn-secondary">
                  Cancel
                </button>
                <button type="submit" className="btn-primary">Create & Select</button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
